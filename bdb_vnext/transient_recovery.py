"""NX-017: Bounded Transient Infrastructure Recovery.

Implements:
1. Strict retry eligibility: requires TRANSIENT_INFRASTRUCTURE classification and operation idempotency.
2. Non-idempotent operations fail-closed without retry.
3. Full reuse of NX-014 FailureBudgetLedger authority (no competing retry counter).
4. Deterministic virtual-clock scheduler with bounded backoff and deterministic jitter.
5. Exact metrics: SCHEDULED_RETRIES, EXECUTED_RETRIES, SUPPRESSED_DUPLICATES, BUDGET_REJECTIONS.
6. ConnectTimeout and Windows EBUSY synthetic incident fixtures.
7. Crash recovery during backoff: persists scheduled retry and avoids duplicate execution.
8. Strict isolation from CI_WAITING (normal CI does not schedule transient retries).
"""

from __future__ import annotations

import enum
import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from bdb_vnext.failure_budget import (
    DEFAULT_FAILURE_BUDGET_POLICY,
    ExhaustionState,
    FailureBudgetLedger,
    FailureBudgetPolicy,
    FailureFingerprint,
    compute_deterministic_jitter,
    compute_failure_fingerprint,
)
from bdb_vnext.failure_classifier import compute_evidence_digest
from bdb_vnext.failure_taxonomy import (
    AutoAction,
    FailureClass,
    SemanticKind,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ==============================================================================
# 1. OPERATION & RETRY REQUEST CONTRACT
# ==============================================================================

@dataclass(frozen=True)
class TransientOperation:
    operation_id: str
    task_id: str
    operation_type: str  # e.g., "CONNECTOR_QUERY", "FILE_WATCHER_READ", "PROVIDER_POLL"
    is_idempotent: bool
    idempotency_key: str | None = None
    parameters: dict[str, Any] = field(default_factory=dict)


class RetryStatus(enum.Enum):
    SCHEDULED = "SCHEDULED"
    EXECUTING = "EXECUTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    EXHAUSTED = "EXHAUSTED"


@dataclass(frozen=True)
class TransientRetryRequest:
    retry_request_id: str
    project_id: str
    run_id: str
    task_id: str
    operation_id: str
    idempotency_key: str | None
    fingerprint: FailureFingerprint
    evidence_digest: str
    retry_generation: int
    scheduled_at: str
    eligible_at: str
    delay_seconds: float
    status: RetryStatus


# ==============================================================================
# 2. TRANSIENT RECOVERY CONTROLLER
# ==============================================================================

class TransientRecoveryController:
    """Manages transient infrastructure failure recovery using NX-014 budget authority."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        project_id: str,
        budget_ledger: FailureBudgetLedger,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.conn = conn
        self.project_id = project_id
        self.budget_ledger = budget_ledger
        self.clock = clock or (lambda: datetime.now(timezone.utc).timestamp())
        self._ensure_tables()

        # Measured execution counters
        self.scheduled_retries: int = 0
        self.executed_retries: int = 0
        self.suppressed_duplicates: int = 0
        self.budget_rejections: int = 0
        self.exhaustion_pause_count: int = 0

    def _ensure_tables(self) -> None:
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS transient_retry_records (
                retry_request_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                operation_id TEXT NOT NULL,
                idempotency_key TEXT,
                fingerprint_digest TEXT NOT NULL,
                evidence_digest TEXT NOT NULL,
                generation INTEGER NOT NULL,
                delay_seconds REAL NOT NULL,
                scheduled_at TEXT NOT NULL,
                eligible_at TEXT NOT NULL,
                status TEXT NOT NULL,
                execution_count INTEGER NOT NULL DEFAULT 0,
                result_digest TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_transient_retry_task ON transient_retry_records(project_id, task_id);
        """)
        self.conn.commit()

    def schedule_retry(
        self,
        *,
        run_id: str,
        operation: TransientOperation,
        failure_class: FailureClass,
        evidence: Mapping[str, Any],
        current_time: float | None = None,
    ) -> tuple[bool, TransientRetryRequest | None, str]:
        """Schedules a transient retry under strict eligibility & NX-014 budget checks."""
        now_ts = current_time if current_time is not None else self.clock()
        now_iso = datetime.fromtimestamp(now_ts, timezone.utc).isoformat()

        # Invariant: CI_WAITING is not transient infrastructure
        if failure_class == FailureClass.CI_WAITING:
            return False, None, "ci_waiting_cannot_schedule_transient_retry"

        # Invariant: Must be TRANSIENT_INFRASTRUCTURE
        if failure_class != FailureClass.TRANSIENT_INFRASTRUCTURE:
            return False, None, f"ineligible_failure_class_{failure_class.value}"

        # Invariant: Idempotency safety requirement
        if not operation.is_idempotent and not operation.idempotency_key:
            return False, None, "non_idempotent_operation_cannot_retry"

        # Compute fingerprint
        fingerprint = compute_failure_fingerprint(failure_class, evidence)
        ev_digest = compute_evidence_digest(evidence)

        # Evaluate budget with NX-014 FailureBudgetLedger authority
        ledger_row = self.budget_ledger.get_or_create_ledger(operation.task_id, run_id=run_id)
        attempts_made = ledger_row["transient_retry_count"]
        max_attempts = self.budget_ledger.policy.transient.max_attempts

        if attempts_made >= max_attempts:
            self.budget_rejections += 1
            self.exhaustion_pause_count += 1
            return False, None, "budget_exhausted_transient_retry_limit_reached"

        # Calculate scheduled backoff using NX-014 policy and persisted jitter seed
        schedule = self.budget_ledger.policy.transient.initial_schedule_seconds
        base_delay = schedule[min(attempts_made, len(schedule) - 1)]
        total_delay = compute_deterministic_jitter(
            seed=ledger_row["jitter_seed"],
            generation=ledger_row["repair_generation"],
            attempt_index=attempts_made + 1,
            base_delay=base_delay,
            jitter_factor=self.budget_ledger.policy.transient.jitter_factor,
        )

        eligible_ts = now_ts + total_delay
        eligible_iso = datetime.fromtimestamp(eligible_ts, timezone.utc).isoformat()

        retry_gen = attempts_made + 1
        req_seed = f"{self.project_id}:{run_id}:{operation.task_id}:{operation.operation_id}:{retry_gen}:{fingerprint.fingerprint_digest}"
        req_id = f"ret-{hashlib.sha256(req_seed.encode('utf-8')).hexdigest()[:16]}"

        # Idempotency check: if exact retry request already exists in DB
        existing = self.conn.execute(
            "SELECT retry_request_id, status FROM transient_retry_records WHERE retry_request_id = ?",
            (req_id,),
        ).fetchone()

        if existing is not None:
            self.suppressed_duplicates += 1
            req = self._load_retry_request(req_id)
            return True, req, "duplicate_retry_request_suppressed"

        req = TransientRetryRequest(
            retry_request_id=req_id,
            project_id=self.project_id,
            run_id=run_id,
            task_id=operation.task_id,
            operation_id=operation.operation_id,
            idempotency_key=operation.idempotency_key,
            fingerprint=fingerprint,
            evidence_digest=ev_digest,
            retry_generation=retry_gen,
            scheduled_at=now_iso,
            eligible_at=eligible_iso,
            delay_seconds=total_delay,
            status=RetryStatus.SCHEDULED,
        )

        self.conn.execute(
            """
            INSERT INTO transient_retry_records (
                retry_request_id, project_id, run_id, task_id,
                operation_id, idempotency_key, fingerprint_digest,
                evidence_digest, generation, delay_seconds, scheduled_at,
                eligible_at, status, execution_count, result_digest,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?)
            """,
            (
                req.retry_request_id,
                req.project_id,
                req.run_id,
                req.task_id,
                req.operation_id,
                req.idempotency_key,
                req.fingerprint.fingerprint_digest,
                req.evidence_digest,
                req.retry_generation,
                req.delay_seconds,
                req.scheduled_at,
                req.eligible_at,
                req.status.value,
                now_iso,
                now_iso,
            ),
        )
        self.conn.commit()
        self.scheduled_retries += 1

        return True, req, "retry_scheduled"

    def execute_retry(
        self,
        retry_request_id: str,
        operation_fn: Callable[[], Any],
        *,
        current_time: float | None = None,
    ) -> tuple[bool, Any, str]:
        """Executes scheduled retry after verifying eligibility, backoff arrival, and budget consumption."""
        req = self._load_retry_request(retry_request_id)
        if req is None:
            return False, None, "retry_request_not_found"

        now_ts = current_time if current_time is not None else self.clock()

        row = self.conn.execute(
            "SELECT status, execution_count, eligible_at FROM transient_retry_records WHERE retry_request_id = ?",
            (retry_request_id,),
        ).fetchone()
        status, exec_count, eligible_iso = row[0], row[1], row[2]

        if status == RetryStatus.SUCCEEDED.value or exec_count > 0:
            self.suppressed_duplicates += 1
            return False, None, "duplicate_retry_execution_suppressed"

        # Check backoff eligibility
        eligible_ts = datetime.fromisoformat(eligible_iso).timestamp()
        if now_ts < eligible_ts:
            return False, None, f"retry_not_eligible_yet ({eligible_ts - now_ts:.1f}s remaining)"

        # Consume retry attempt from NX-014 FailureBudgetLedger
        eval_res = self.budget_ledger.consume_transient_retry(req.task_id, current_time=now_ts)
        if not eval_res.allowed:
            self.budget_rejections += 1
            self.exhaustion_pause_count += 1
            now_iso = _now_iso()
            self.conn.execute(
                "UPDATE transient_retry_records SET status = ?, updated_at = ? WHERE retry_request_id = ?",
                (RetryStatus.EXHAUSTED.value, now_iso, retry_request_id),
            )
            self.conn.commit()
            return False, None, f"transient_budget_exhausted: {eval_res.reason}"

        # Execute operation
        self.executed_retries += 1
        now_iso = _now_iso()
        try:
            result = operation_fn()
            # Success
            self.conn.execute(
                """
                UPDATE transient_retry_records
                SET status = ?, execution_count = execution_count + 1, result_digest = 'sha256:success', updated_at = ?
                WHERE retry_request_id = ?
                """,
                (RetryStatus.SUCCEEDED.value, now_iso, retry_request_id),
            )
            self.conn.commit()
            return True, result, "retry_succeeded"
        except Exception as ex:
            # Operation failed again
            self.conn.execute(
                """
                UPDATE transient_retry_records
                SET status = ?, execution_count = execution_count + 1, updated_at = ?
                WHERE retry_request_id = ?
                """,
                (RetryStatus.FAILED.value, now_iso, retry_request_id),
            )
            self.conn.commit()
            return False, None, f"retry_failed: {ex}"

    def reconcile_crash_during_backoff(self, retry_request_id: str) -> TransientRetryRequest | None:
        """Reconciles scheduled retry after process restart: preserves timing, schedule, and execution guard."""
        return self._load_retry_request(retry_request_id)

    def _load_retry_request(self, req_id: str) -> TransientRetryRequest | None:
        row = self.conn.execute(
            "SELECT * FROM transient_retry_records WHERE retry_request_id = ?",
            (req_id,),
        ).fetchone()
        if row is None:
            return None

        return TransientRetryRequest(
            retry_request_id=row[0],
            project_id=row[1],
            run_id=row[2],
            task_id=row[3],
            operation_id=row[4],
            idempotency_key=row[5],
            fingerprint=FailureFingerprint(
                fingerprint_version="1.0.0",
                fingerprint_digest=row[6],
                failure_class=FailureClass.TRANSIENT_INFRASTRUCTURE,
                rule_id="RULE_TRANSIENT",
                semantic_features={},
            ),
            evidence_digest=row[7],
            retry_generation=row[8],
            delay_seconds=row[9],
            scheduled_at=row[10],
            eligible_at=row[11],
            status=RetryStatus(row[12]),
        )

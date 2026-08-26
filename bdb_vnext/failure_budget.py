"""NX-014: Failure Fingerprint and Persisted Bounded Budgets.

Implements:
1. Versioned deterministic failure fingerprinting (v1).
2. Durable budget ledger persisted under Project Memory v2 SQLite store.
3. Policy-configurable budgets conforming to D-018 defaults.
4. Exact duplicate suppression and distinct-fingerprint accounting.
5. Persistent logical wall-time accounting and deterministic jitter replay.
6. Audited manual overrides and terminal exhaustion states.
7. Bounded executable model checker proving termination.
"""

from __future__ import annotations

import collections
import enum
import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from bdb_vnext.failure_classifier import (
    ClassificationResult,
    compute_evidence_digest,
)
from bdb_vnext.failure_taxonomy import (
    AutoAction,
    FailureClass,
    SemanticKind,
    TRANSITION_MATRIX,
)

FINGERPRINT_VERSION: str = "1.0.0"

# Non-semantic/volatile fields to exclude from fingerprint derivation
VOLATILE_FIELDS: frozenset[str] = frozenset({
    "timestamp",
    "created_at",
    "updated_at",
    "time",
    "date",
    "random_id",
    "attempt_id",
    "execution_binding_id",
    "run_id",
    "launch_id",
    "retry_generation",
    "generation",
    "nonce",
    "uuid",
    "guid",
    "pid",
    "process_id",
    "thread_id",
    "session_id",
})


# ==============================================================================
# 1. FAILURE FINGERPRINT V1
# ==============================================================================

@dataclass(frozen=True)
class FailureFingerprint:
    fingerprint_version: str
    fingerprint_digest: str
    failure_class: FailureClass
    rule_id: str
    semantic_features: dict[str, Any]


def _normalize_semantic_value(val: Any) -> Any:
    """Recursively normalizes values for canonical JSON serialization."""
    if isinstance(val, (dict, Mapping)):
        return {
            str(k): _normalize_semantic_value(v)
            for k, v in sorted(val.items(), key=lambda item: str(item[0]))
            if not str(k).startswith("_") and str(k).lower() not in VOLATILE_FIELDS
        }
    if isinstance(val, (list, tuple, set, frozenset)):
        return [_normalize_semantic_value(v) for v in val]
    if isinstance(val, float):
        return round(val, 6)
    return val


def compute_failure_fingerprint(
    classification_or_class: ClassificationResult | FailureClass,
    evidence: Mapping[str, Any],
    *,
    rule_id: str | None = None,
) -> FailureFingerprint:
    """Derives a versioned deterministic failure fingerprint from stable semantic identity.

    Excludes volatile fields (timestamps, random IDs, attempt numbers, etc.).
    Equivalent JSON key ordering yields identical fingerprints.
    Different semantic failures yield different fingerprints.
    """
    if isinstance(classification_or_class, ClassificationResult):
        f_class = classification_or_class.failure_class
        r_id = rule_id or classification_or_class.rule_id
    elif isinstance(classification_or_class, FailureClass):
        f_class = classification_or_class
        r_id = rule_id or "RULE_EXPLICIT"
    else:
        raise ValueError(f"Invalid classification: {classification_or_class}")

    # Extract stable semantic features from evidence
    semantic_data = _normalize_semantic_value(evidence)

    # Canonical payload
    payload = {
        "version": FINGERPRINT_VERSION,
        "class": f_class.value,
        "rule_id": r_id,
        "semantic_data": semantic_data,
    }

    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = f"fp1:{hashlib.sha256(serialized.encode('utf-8')).hexdigest()}"

    return FailureFingerprint(
        fingerprint_version=FINGERPRINT_VERSION,
        fingerprint_digest=digest,
        failure_class=f_class,
        rule_id=r_id,
        semantic_features=semantic_data,
    )


# ==============================================================================
# 2. BUDGET POLICIES (D-018)
# ==============================================================================

@dataclass(frozen=True)
class TransientRetryPolicy:
    max_attempts: int = 3
    initial_schedule_seconds: tuple[float, ...] = (2.0, 10.0, 30.0)
    jitter_factor: float = 0.1
    backoff_multiplier: float = 2.0


@dataclass(frozen=True)
class RepairBudgetPolicy:
    max_same_fingerprint_repairs: int = 2
    max_total_repair_attempts: int = 4
    max_total_repair_wall_time_seconds: float = 1800.0  # 30 minutes default


@dataclass(frozen=True)
class CIPollingPolicy:
    poll_interval_seconds: float = 15.0
    max_poll_wall_time_seconds: float = 3600.0
    provider: str = "default"


@dataclass(frozen=True)
class FailureBudgetPolicy:
    transient: TransientRetryPolicy = field(default_factory=TransientRetryPolicy)
    repair: RepairBudgetPolicy = field(default_factory=RepairBudgetPolicy)
    ci: CIPollingPolicy = field(default_factory=CIPollingPolicy)


DEFAULT_FAILURE_BUDGET_POLICY = FailureBudgetPolicy()


# ==============================================================================
# 3. DETERMINISTIC JITTER
# ==============================================================================

def compute_deterministic_jitter(
    seed: int,
    generation: int,
    attempt_index: int,
    base_delay: float,
    jitter_factor: float = 0.1,
) -> float:
    """Computes reproducible deterministic jitter from persisted seed and generation.

    Never uses nondeterministic unseeded random state.
    """
    key = f"{seed}:{generation}:{attempt_index}".encode("utf-8")
    hex_digest = hashlib.sha256(key).hexdigest()
    # Map to fraction in [0.0, 1.0)
    fraction = int(hex_digest[:8], 16) / 0xFFFFFFFF
    # Jitter variation in [-jitter_factor, +jitter_factor]
    jitter_offset = base_delay * jitter_factor * (2.0 * fraction - 1.0)
    return max(0.0, round(base_delay + jitter_offset, 4))


# ==============================================================================
# 4. EXHAUSTION STATES & EVALUATION
# ==============================================================================

class ExhaustionState(enum.Enum):
    NOT_EXHAUSTED = "NOT_EXHAUSTED"
    SAME_FINGERPRINT_EXHAUSTED = "SAME_FINGERPRINT_EXHAUSTED"
    TOTAL_REPAIR_EXHAUSTED = "TOTAL_REPAIR_EXHAUSTED"
    WALL_TIME_EXHAUSTED = "WALL_TIME_EXHAUSTED"
    TRANSIENT_RETRY_EXHAUSTED = "TRANSIENT_RETRY_EXHAUSTED"
    REPAIR_LOOP_EXHAUSTED = "REPAIR_LOOP_EXHAUSTED"


@dataclass(frozen=True)
class BudgetEvaluationResult:
    allowed: bool
    exhaustion_state: ExhaustionState
    disposition: str
    retry_delay_seconds: float | None
    remaining_transient_attempts: int
    remaining_same_fingerprint_repairs: int
    remaining_total_repairs: int
    remaining_wall_time_seconds: float
    reason: str


@dataclass(frozen=True)
class ManualOverrideAudit:
    override_id: str
    project_id: str
    task_id: str
    actor_class: str
    affected_budget: str
    previous_value: Any
    new_value: Any
    reason: str
    created_at: str


# ==============================================================================
# 5. PERSISTED BUDGET LEDGER
# ==============================================================================

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class FailureBudgetLedger:
    """Manages persisted retry/repair budget accounting in SQLite under Project Memory v2."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        project_id: str,
        policy: FailureBudgetPolicy | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.conn = conn
        self.project_id = project_id
        self.policy = policy or DEFAULT_FAILURE_BUDGET_POLICY
        self.clock = clock or (lambda: datetime.now(timezone.utc).timestamp())
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        """Ensures budget ledger tables and triggers exist."""
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS budget_ledgers (
                ledger_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                run_id TEXT,
                total_repair_count INTEGER NOT NULL DEFAULT 0,
                transient_retry_count INTEGER NOT NULL DEFAULT 0,
                repair_generation INTEGER NOT NULL DEFAULT 0,
                wall_time_start TEXT,
                jitter_seed INTEGER NOT NULL DEFAULT 42,
                exhausted_status TEXT NOT NULL DEFAULT 'ACTIVE',
                last_fingerprint TEXT,
                fingerprint_counts_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_budget_ledgers_task ON budget_ledgers(project_id, task_id);

            CREATE TABLE IF NOT EXISTS budget_overrides (
                override_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                actor_class TEXT NOT NULL,
                affected_budget TEXT NOT NULL,
                previous_value_json TEXT NOT NULL,
                new_value_json TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TRIGGER IF NOT EXISTS trg_budget_overrides_no_delete
            BEFORE DELETE ON budget_overrides
            BEGIN
                SELECT RAISE(FAIL, 'budget_overrides is append-only: deletes are prohibited');
            END;
        """)
        self.conn.commit()

    def _load_ledger_row(self, task_id: str) -> dict[str, Any] | None:
        cursor = self.conn.execute(
            "SELECT * FROM budget_ledgers WHERE project_id = ? AND task_id = ?",
            (self.project_id, task_id),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        col_names = [d[0] for d in cursor.description]
        return dict(zip(col_names, row))

    def get_or_create_ledger(
        self,
        task_id: str,
        run_id: str | None = None,
        *,
        jitter_seed: int = 42,
    ) -> dict[str, Any]:
        """Loads or creates a persistent ledger record for (project_id, task_id)."""
        existing = self._load_ledger_row(task_id)
        if existing is not None:
            return existing

        now = _now_iso()
        ledger_id = f"bl-{hashlib.sha256(f'{self.project_id}:{task_id}'.encode()).hexdigest()[:16]}"
        self.conn.execute(
            """
            INSERT INTO budget_ledgers (
                ledger_id, project_id, task_id, run_id, total_repair_count,
                transient_retry_count, repair_generation, wall_time_start,
                jitter_seed, exhausted_status, last_fingerprint,
                fingerprint_counts_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 0, 0, 0, NULL, ?, 'ACTIVE', NULL, '{}', ?, ?)
            """,
            (ledger_id, self.project_id, task_id, run_id, jitter_seed, now, now),
        )
        self.conn.commit()
        loaded = self._load_ledger_row(task_id)
        assert loaded is not None
        return loaded

    def evaluate_failure(
        self,
        task_id: str,
        classification_or_class: ClassificationResult | FailureClass,
        evidence: Mapping[str, Any],
        *,
        rule_id: str | None = None,
        run_id: str | None = None,
        current_time: float | None = None,
    ) -> tuple[FailureFingerprint, BudgetEvaluationResult]:
        """Evaluates whether failure response (retry/repair) is permitted under budget policy."""
        now_ts = current_time if current_time is not None else self.clock()
        fingerprint = compute_failure_fingerprint(
            classification_or_class, evidence, rule_id=rule_id
        )

        ledger = self.get_or_create_ledger(task_id, run_id=run_id)
        f_class = fingerprint.failure_class
        spec = TRANSITION_MATRIX.get(f_class)

        # CI_WAITING policy: does NOT consume short transient retry budget (independent polling)
        if f_class == FailureClass.CI_WAITING:
            return fingerprint, BudgetEvaluationResult(
                allowed=True,
                exhaustion_state=ExhaustionState.NOT_EXHAUSTED,
                disposition="AUTO_POLL",
                retry_delay_seconds=self.policy.ci.poll_interval_seconds,
                remaining_transient_attempts=self.policy.transient.max_attempts - ledger["transient_retry_count"],
                remaining_same_fingerprint_repairs=self.policy.repair.max_same_fingerprint_repairs,
                remaining_total_repairs=self.policy.repair.max_total_repair_attempts,
                remaining_wall_time_seconds=self.policy.ci.max_poll_wall_time_seconds,
                reason="CI_WAITING monitored under independent CI polling policy",
            )

        # Non-retryable / non-repairable classes according to NX-012 transition table
        if spec and not spec.retry_allowed and not spec.repair_allowed:
            return fingerprint, BudgetEvaluationResult(
                allowed=False,
                exhaustion_state=ExhaustionState.NOT_EXHAUSTED,
                disposition="FAIL_CLOSED",
                retry_delay_seconds=None,
                remaining_transient_attempts=0,
                remaining_same_fingerprint_repairs=0,
                remaining_total_repairs=0,
                remaining_wall_time_seconds=0.0,
                reason=f"Failure class {f_class.value} does not permit automatic retry or repair",
            )

        # Transient Infrastructure failure evaluation
        if f_class == FailureClass.TRANSIENT_INFRASTRUCTURE:
            attempts_made = ledger["transient_retry_count"]
            max_attempts = self.policy.transient.max_attempts
            if attempts_made >= max_attempts:
                return fingerprint, BudgetEvaluationResult(
                    allowed=False,
                    exhaustion_state=ExhaustionState.TRANSIENT_RETRY_EXHAUSTED,
                    disposition="PAUSED",
                    retry_delay_seconds=None,
                    remaining_transient_attempts=0,
                    remaining_same_fingerprint_repairs=0,
                    remaining_total_repairs=0,
                    remaining_wall_time_seconds=0.0,
                    reason=f"Transient retry limit exhausted ({attempts_made}/{max_attempts})",
                )

            # Compute schedule delay with deterministic jitter
            sched = self.policy.transient.initial_schedule_seconds
            base_delay = sched[min(attempts_made, len(sched) - 1)]
            delay = compute_deterministic_jitter(
                seed=ledger["jitter_seed"],
                generation=ledger["repair_generation"],
                attempt_index=attempts_made,
                base_delay=base_delay,
                jitter_factor=self.policy.transient.jitter_factor,
            )
            return fingerprint, BudgetEvaluationResult(
                allowed=True,
                exhaustion_state=ExhaustionState.NOT_EXHAUSTED,
                disposition="AUTO_RETRY_BACKOFF",
                retry_delay_seconds=delay,
                remaining_transient_attempts=max_attempts - attempts_made - 1,
                remaining_same_fingerprint_repairs=0,
                remaining_total_repairs=0,
                remaining_wall_time_seconds=0.0,
                reason=f"Transient retry allowed ({attempts_made + 1}/{max_attempts})",
            )

        # Repairable failure evaluation (PROJECT_REPAIRABLE, PHASE_SCOPE_VIOLATION, EARLY_IMPLEMENTATION)
        # 1. Wall-time check
        wall_start_iso = ledger["wall_time_start"]
        max_wall_sec = self.policy.repair.max_total_repair_wall_time_seconds
        remaining_wall = max_wall_sec
        if wall_start_iso:
            try:
                start_dt = datetime.fromisoformat(wall_start_iso)
                elapsed = now_ts - start_dt.timestamp()
                remaining_wall = max(0.0, max_wall_sec - elapsed)
                if elapsed > max_wall_sec:
                    return fingerprint, BudgetEvaluationResult(
                        allowed=False,
                        exhaustion_state=ExhaustionState.WALL_TIME_EXHAUSTED,
                        disposition="REPAIR_LOOP_EXHAUSTED",
                        retry_delay_seconds=None,
                        remaining_transient_attempts=0,
                        remaining_same_fingerprint_repairs=0,
                        remaining_total_repairs=0,
                        remaining_wall_time_seconds=0.0,
                        reason=f"Total repair wall time exceeded ({elapsed:.1f}s > {max_wall_sec}s)",
                    )
            except Exception:
                pass

        # 2. Total repair attempts check
        total_repairs = ledger["total_repair_count"]
        max_total = self.policy.repair.max_total_repair_attempts
        if total_repairs >= max_total:
            return fingerprint, BudgetEvaluationResult(
                allowed=False,
                exhaustion_state=ExhaustionState.TOTAL_REPAIR_EXHAUSTED,
                disposition="REPAIR_LOOP_EXHAUSTED",
                retry_delay_seconds=None,
                remaining_transient_attempts=0,
                remaining_same_fingerprint_repairs=0,
                remaining_total_repairs=0,
                remaining_wall_time_seconds=remaining_wall,
                reason=f"Total repair attempt budget exhausted ({total_repairs}/{max_total})",
            )

        # 3. Same-fingerprint repairs check
        fp_counts: dict[str, int] = json.loads(ledger["fingerprint_counts_json"])
        same_fp_count = fp_counts.get(fingerprint.fingerprint_digest, 0)
        max_same_fp = self.policy.repair.max_same_fingerprint_repairs
        if same_fp_count >= max_same_fp:
            return fingerprint, BudgetEvaluationResult(
                allowed=False,
                exhaustion_state=ExhaustionState.SAME_FINGERPRINT_EXHAUSTED,
                disposition="REPAIR_LOOP_EXHAUSTED",
                retry_delay_seconds=None,
                remaining_transient_attempts=0,
                remaining_same_fingerprint_repairs=0,
                remaining_total_repairs=max_total - total_repairs,
                remaining_wall_time_seconds=remaining_wall,
                reason=f"Same-fingerprint repair limit exhausted ({same_fp_count}/{max_same_fp})",
            )

        return fingerprint, BudgetEvaluationResult(
            allowed=True,
            exhaustion_state=ExhaustionState.NOT_EXHAUSTED,
            disposition="PROCEED_REPAIR",
            retry_delay_seconds=None,
            remaining_transient_attempts=0,
            remaining_same_fingerprint_repairs=max_same_fp - same_fp_count - 1,
            remaining_total_repairs=max_total - total_repairs - 1,
            remaining_wall_time_seconds=remaining_wall,
            reason="Repair attempt permitted within budget limits",
        )

    def record_failure_observation(
        self,
        task_id: str,
        fingerprint: FailureFingerprint,
        *,
        current_time: float | None = None,
    ) -> bool:
        """Records initial failure observation.

        Exact duplicate suppression:
        If identical fingerprint is repeatedly observed without consumption:
        - do not create new generation
        - do not reset counters
        - do not reset wall time
        Returns True if fresh failure, False if duplicate observation suppressed.
        """
        ledger = self.get_or_create_ledger(task_id)
        now_iso = _now_iso() if current_time is None else datetime.fromtimestamp(current_time, timezone.utc).isoformat()

        # Set wall-time start on first failure observation if not set
        updates: list[str] = ["updated_at = ?"]
        params: list[Any] = [now_iso]

        if not ledger["wall_time_start"]:
            updates.append("wall_time_start = ?")
            params.append(now_iso)

        if ledger["last_fingerprint"] == fingerprint.fingerprint_digest:
            # Exact duplicate observed without retry/repair step: suppress generation advance
            if len(updates) > 1:  # Need to set wall_time_start
                params.extend([self.project_id, task_id])
                sql = f"UPDATE budget_ledgers SET {', '.join(updates)} WHERE project_id = ? AND task_id = ?"
                self.conn.execute(sql, params)
                self.conn.commit()
            return False

        updates.append("last_fingerprint = ?")
        params.append(fingerprint.fingerprint_digest)
        params.extend([self.project_id, task_id])
        sql = f"UPDATE budget_ledgers SET {', '.join(updates)} WHERE project_id = ? AND task_id = ?"
        self.conn.execute(sql, params)
        self.conn.commit()
        return True

    def consume_transient_retry(
        self,
        task_id: str,
        *,
        current_time: float | None = None,
    ) -> BudgetEvaluationResult:
        """Durably records consumption of a transient retry attempt."""
        ledger = self.get_or_create_ledger(task_id)
        current_retries = ledger["transient_retry_count"]
        max_retries = self.policy.transient.max_attempts

        if current_retries >= max_retries:
            return BudgetEvaluationResult(
                allowed=False,
                exhaustion_state=ExhaustionState.TRANSIENT_RETRY_EXHAUSTED,
                disposition="PAUSED",
                retry_delay_seconds=None,
                remaining_transient_attempts=0,
                remaining_same_fingerprint_repairs=0,
                remaining_total_repairs=0,
                remaining_wall_time_seconds=0.0,
                reason="Transient retry budget already exhausted",
            )

        new_count = current_retries + 1
        now_iso = _now_iso() if current_time is None else datetime.fromtimestamp(current_time, timezone.utc).isoformat()
        exhausted_str = "EXHAUSTED" if new_count >= max_retries else "ACTIVE"

        self.conn.execute(
            """
            UPDATE budget_ledgers
            SET transient_retry_count = ?,
                exhausted_status = ?,
                updated_at = ?
            WHERE project_id = ? AND task_id = ?
            """,
            (new_count, exhausted_str, now_iso, self.project_id, task_id),
        )
        self.conn.commit()

        return BudgetEvaluationResult(
            allowed=True,
            exhaustion_state=ExhaustionState.NOT_EXHAUSTED if new_count < max_retries else ExhaustionState.TRANSIENT_RETRY_EXHAUSTED,
            disposition="AUTO_RETRY_BACKOFF",
            retry_delay_seconds=None,
            remaining_transient_attempts=max_retries - new_count,
            remaining_same_fingerprint_repairs=0,
            remaining_total_repairs=0,
            remaining_wall_time_seconds=0.0,
            reason=f"Transient attempt {new_count} recorded",
        )

    def consume_repair_attempt(
        self,
        task_id: str,
        fingerprint: FailureFingerprint,
        *,
        current_time: float | None = None,
    ) -> BudgetEvaluationResult:
        """Durably records consumption of a repair attempt for a specific failure fingerprint."""
        ledger = self.get_or_create_ledger(task_id)
        now_ts = current_time if current_time is not None else self.clock()
        now_iso = _now_iso() if current_time is None else datetime.fromtimestamp(current_time, timezone.utc).isoformat()

        # Set wall-time start if absent
        wall_start_iso = ledger["wall_time_start"]
        if not wall_start_iso:
            wall_start_iso = now_iso

        fp_counts: dict[str, int] = json.loads(ledger["fingerprint_counts_json"])
        fp_digest = fingerprint.fingerprint_digest
        same_fp = fp_counts.get(fp_digest, 0)
        total_repairs = ledger["total_repair_count"]

        max_same_fp = self.policy.repair.max_same_fingerprint_repairs
        max_total = self.policy.repair.max_total_repair_attempts

        # Check limits
        if same_fp >= max_same_fp:
            return BudgetEvaluationResult(
                allowed=False,
                exhaustion_state=ExhaustionState.SAME_FINGERPRINT_EXHAUSTED,
                disposition="REPAIR_LOOP_EXHAUSTED",
                retry_delay_seconds=None,
                remaining_transient_attempts=0,
                remaining_same_fingerprint_repairs=0,
                remaining_total_repairs=max(0, max_total - total_repairs),
                remaining_wall_time_seconds=0.0,
                reason="Same fingerprint repair budget exhausted",
            )
        if total_repairs >= max_total:
            return BudgetEvaluationResult(
                allowed=False,
                exhaustion_state=ExhaustionState.TOTAL_REPAIR_EXHAUSTED,
                disposition="REPAIR_LOOP_EXHAUSTED",
                retry_delay_seconds=None,
                remaining_transient_attempts=0,
                remaining_same_fingerprint_repairs=0,
                remaining_total_repairs=0,
                remaining_wall_time_seconds=0.0,
                reason="Total repair budget exhausted",
            )

        # Advance durable counts
        fp_counts[fp_digest] = same_fp + 1
        new_total = total_repairs + 1
        new_gen = ledger["repair_generation"] + 1

        is_exhausted = (fp_counts[fp_digest] >= max_same_fp) or (new_total >= max_total)
        status_str = "REPAIR_LOOP_EXHAUSTED" if is_exhausted else "ACTIVE"

        self.conn.execute(
            """
            UPDATE budget_ledgers
            SET total_repair_count = ?,
                repair_generation = ?,
                wall_time_start = ?,
                exhausted_status = ?,
                last_fingerprint = ?,
                fingerprint_counts_json = ?,
                updated_at = ?
            WHERE project_id = ? AND task_id = ?
            """,
            (
                new_total,
                new_gen,
                wall_start_iso,
                status_str,
                fp_digest,
                json.dumps(fp_counts, sort_keys=True),
                now_iso,
                self.project_id,
                task_id,
            ),
        )
        self.conn.commit()

        return BudgetEvaluationResult(
            allowed=True,
            exhaustion_state=ExhaustionState.NOT_EXHAUSTED if not is_exhausted else ExhaustionState.REPAIR_LOOP_EXHAUSTED,
            disposition="PROCEED_REPAIR",
            retry_delay_seconds=None,
            remaining_transient_attempts=0,
            remaining_same_fingerprint_repairs=max_same_fp - fp_counts[fp_digest],
            remaining_total_repairs=max_total - new_total,
            remaining_wall_time_seconds=max(0.0, self.policy.repair.max_total_repair_wall_time_seconds),
            reason=f"Repair attempt {new_total} consumed (same_fp={fp_counts[fp_digest]})",
        )

    def record_manual_override(
        self,
        task_id: str,
        actor_class: str,
        affected_budget: str,
        new_value: Any,
        reason: str,
        *,
        current_time: float | None = None,
    ) -> ManualOverrideAudit:
        """Explicit, audited manual budget override.

        Unaudited resets or resets missing provenance/reason fail closed.
        """
        if not actor_class or not isinstance(actor_class, str) or len(actor_class.strip()) == 0:
            raise ValueError("actor_class is required and cannot be empty")
        if not affected_budget or not isinstance(affected_budget, str):
            raise ValueError("affected_budget is required")
        if not reason or not isinstance(reason, str) or len(reason.strip()) == 0:
            raise ValueError("reason is required and cannot be empty")

        ledger = self.get_or_create_ledger(task_id)
        now_iso = _now_iso() if current_time is None else datetime.fromtimestamp(current_time, timezone.utc).isoformat()
        override_id = f"ovr-{hashlib.sha256(f'{self.project_id}:{task_id}:{now_iso}:{affected_budget}'.encode()).hexdigest()[:16]}"

        previous_val: Any = None
        if affected_budget == "transient_retry_count":
            previous_val = ledger["transient_retry_count"]
            self.conn.execute(
                "UPDATE budget_ledgers SET transient_retry_count = ?, updated_at = ? WHERE project_id = ? AND task_id = ?",
                (int(new_value), now_iso, self.project_id, task_id),
            )
        elif affected_budget == "total_repair_count":
            previous_val = ledger["total_repair_count"]
            self.conn.execute(
                "UPDATE budget_ledgers SET total_repair_count = ?, updated_at = ? WHERE project_id = ? AND task_id = ?",
                (int(new_value), now_iso, self.project_id, task_id),
            )
        elif affected_budget == "same_fingerprint_count":
            previous_val = json.loads(ledger["fingerprint_counts_json"])
            new_json = json.dumps(new_value if isinstance(new_value, dict) else {}, sort_keys=True)
            self.conn.execute(
                "UPDATE budget_ledgers SET fingerprint_counts_json = ?, updated_at = ? WHERE project_id = ? AND task_id = ?",
                (new_json, now_iso, self.project_id, task_id),
            )
        elif affected_budget == "all":
            previous_val = {
                "transient_retry_count": ledger["transient_retry_count"],
                "total_repair_count": ledger["total_repair_count"],
                "fingerprint_counts": json.loads(ledger["fingerprint_counts_json"]),
                "exhausted_status": ledger["exhausted_status"],
            }
            self.conn.execute(
                """
                UPDATE budget_ledgers
                SET transient_retry_count = 0,
                    total_repair_count = 0,
                    fingerprint_counts_json = '{}',
                    exhausted_status = 'ACTIVE',
                    updated_at = ?
                WHERE project_id = ? AND task_id = ?
                """,
                (now_iso, self.project_id, task_id),
            )
        else:
            raise ValueError(f"Unknown affected_budget: {affected_budget}")

        # Persist audit record in append-only table
        self.conn.execute(
            """
            INSERT INTO budget_overrides (
                override_id, project_id, task_id, actor_class,
                affected_budget, previous_value_json, new_value_json,
                reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                override_id,
                self.project_id,
                task_id,
                actor_class,
                affected_budget,
                json.dumps(previous_val),
                json.dumps(new_value),
                reason,
                now_iso,
            ),
        )
        self.conn.commit()

        return ManualOverrideAudit(
            override_id=override_id,
            project_id=self.project_id,
            task_id=task_id,
            actor_class=actor_class,
            affected_budget=affected_budget,
            previous_value=previous_val,
            new_value=new_value,
            reason=reason,
            created_at=now_iso,
        )


# ==============================================================================
# 6. BOUNDED MODEL CHECKER
# ==============================================================================

@dataclass(frozen=True)
class ModelGraphNode:
    failure_class: str
    fingerprint: str
    same_fp_count: int
    total_repair_count: int
    transient_retry_count: int
    wall_time_exhausted: bool
    paused: bool
    repair_generation: int
    disposition: str


class BudgetModelChecker:
    """Explores the bounded state graph of failure retry/repair transitions to verify termination."""

    def __init__(self, policy: FailureBudgetPolicy | None = None) -> None:
        self.policy = policy or DEFAULT_FAILURE_BUDGET_POLICY

    def explore(self, max_depth: int = 20) -> dict[str, Any]:
        """Performs BFS graph exploration of legal transitions."""
        initial_nodes = [
            # Transient infrastructure root
            ModelGraphNode(
                failure_class=FailureClass.TRANSIENT_INFRASTRUCTURE.value,
                fingerprint="fp1:transient",
                same_fp_count=0,
                total_repair_count=0,
                transient_retry_count=0,
                wall_time_exhausted=False,
                paused=False,
                repair_generation=0,
                disposition="ACTIVE",
            ),
            # Project repairable root
            ModelGraphNode(
                failure_class=FailureClass.PROJECT_REPAIRABLE.value,
                fingerprint="fp1:repair_A",
                same_fp_count=0,
                total_repair_count=0,
                transient_retry_count=0,
                wall_time_exhausted=False,
                paused=False,
                repair_generation=0,
                disposition="ACTIVE",
            ),
            # CI waiting root
            ModelGraphNode(
                failure_class=FailureClass.CI_WAITING.value,
                fingerprint="fp1:ci",
                same_fp_count=0,
                total_repair_count=0,
                transient_retry_count=0,
                wall_time_exhausted=False,
                paused=False,
                repair_generation=0,
                disposition="ACTIVE",
            ),
            # Security violation root (non-retryable)
            ModelGraphNode(
                failure_class=FailureClass.SECURITY_VIOLATION.value,
                fingerprint="fp1:sec",
                same_fp_count=0,
                total_repair_count=0,
                transient_retry_count=0,
                wall_time_exhausted=False,
                paused=False,
                repair_generation=0,
                disposition="ACTIVE",
            ),
        ]

        visited: set[ModelGraphNode] = set()
        queue: collections.deque[tuple[ModelGraphNode, int, list[ModelGraphNode]]] = collections.deque()
        for node in initial_nodes:
            queue.append((node, 0, [node]))

        cycles_without_budget_decrease = 0
        unbounded_paths = 0
        terminal_states: set[ModelGraphNode] = set()

        max_transient = self.policy.transient.max_attempts
        max_same_fp = self.policy.repair.max_same_fingerprint_repairs
        max_total_rep = self.policy.repair.max_total_repair_attempts

        while queue:
            node, depth, path = queue.popleft()
            if node in visited:
                continue
            visited.add(node)

            if depth >= max_depth:
                unbounded_paths += 1
                continue

            # Terminal / paused nodes have no automatic outgoing transitions
            if node.paused or node.disposition in {"TERMINAL_PASS", "TERMINAL_FAIL", "REPAIR_LOOP_EXHAUSTED", "PAUSED", "AUTO_FREEZE_FAIL_CLOSED"}:
                terminal_states.add(node)
                continue

            successors: list[ModelGraphNode] = []

            if node.failure_class == FailureClass.TRANSIENT_INFRASTRUCTURE.value:
                if node.transient_retry_count < max_transient:
                    # Legal retry transition: strictly increments retry count
                    succ = ModelGraphNode(
                        failure_class=node.failure_class,
                        fingerprint=node.fingerprint,
                        same_fp_count=node.same_fp_count,
                        total_repair_count=node.total_repair_count,
                        transient_retry_count=node.transient_retry_count + 1,
                        wall_time_exhausted=node.wall_time_exhausted,
                        paused=False,
                        repair_generation=node.repair_generation + 1,
                        disposition="AUTO_RETRY_BACKOFF",
                    )
                    successors.append(succ)
                else:
                    # Exhausted: terminal pause
                    succ = ModelGraphNode(
                        failure_class=node.failure_class,
                        fingerprint=node.fingerprint,
                        same_fp_count=node.same_fp_count,
                        total_repair_count=node.total_repair_count,
                        transient_retry_count=node.transient_retry_count,
                        wall_time_exhausted=node.wall_time_exhausted,
                        paused=True,
                        repair_generation=node.repair_generation,
                        disposition="PAUSED",
                    )
                    successors.append(succ)

            elif node.failure_class == FailureClass.PROJECT_REPAIRABLE.value:
                if node.wall_time_exhausted or node.same_fp_count >= max_same_fp or node.total_repair_count >= max_total_rep:
                    succ = ModelGraphNode(
                        failure_class=node.failure_class,
                        fingerprint=node.fingerprint,
                        same_fp_count=node.same_fp_count,
                        total_repair_count=node.total_repair_count,
                        transient_retry_count=node.transient_retry_count,
                        wall_time_exhausted=node.wall_time_exhausted,
                        paused=True,
                        repair_generation=node.repair_generation,
                        disposition="REPAIR_LOOP_EXHAUSTED",
                    )
                    successors.append(succ)
                else:
                    # Transition A: Repeated same-fingerprint repair
                    succ_same = ModelGraphNode(
                        failure_class=node.failure_class,
                        fingerprint=node.fingerprint,
                        same_fp_count=node.same_fp_count + 1,
                        total_repair_count=node.total_repair_count + 1,
                        transient_retry_count=node.transient_retry_count,
                        wall_time_exhausted=node.wall_time_exhausted,
                        paused=False,
                        repair_generation=node.repair_generation + 1,
                        disposition="PROCEED_REPAIR",
                    )
                    successors.append(succ_same)

                    # Transition B: Different fingerprint repair appears
                    succ_diff = ModelGraphNode(
                        failure_class=node.failure_class,
                        fingerprint="fp1:repair_B",
                        same_fp_count=1,
                        total_repair_count=node.total_repair_count + 1,
                        transient_retry_count=node.transient_retry_count,
                        wall_time_exhausted=node.wall_time_exhausted,
                        paused=False,
                        repair_generation=node.repair_generation + 1,
                        disposition="PROCEED_REPAIR",
                    )
                    successors.append(succ_diff)

                    # Transition C: Success / Terminal PASS
                    succ_pass = ModelGraphNode(
                        failure_class=node.failure_class,
                        fingerprint=node.fingerprint,
                        same_fp_count=node.same_fp_count,
                        total_repair_count=node.total_repair_count,
                        transient_retry_count=node.transient_retry_count,
                        wall_time_exhausted=node.wall_time_exhausted,
                        paused=True,
                        repair_generation=node.repair_generation,
                        disposition="TERMINAL_PASS",
                    )
                    successors.append(succ_pass)

            elif node.failure_class == FailureClass.CI_WAITING.value:
                # CI poll: transitions to TERMINAL_PASS (success) or terminal failure
                succ_pass = ModelGraphNode(
                    failure_class=node.failure_class,
                    fingerprint=node.fingerprint,
                    same_fp_count=node.same_fp_count,
                    total_repair_count=node.total_repair_count,
                    transient_retry_count=node.transient_retry_count,
                    wall_time_exhausted=node.wall_time_exhausted,
                    paused=True,
                    repair_generation=node.repair_generation,
                    disposition="TERMINAL_PASS",
                )
                successors.append(succ_pass)

            elif node.failure_class == FailureClass.SECURITY_VIOLATION.value:
                # Security violation: immediate freeze fail-closed, no retry
                succ_freeze = ModelGraphNode(
                    failure_class=node.failure_class,
                    fingerprint=node.fingerprint,
                    same_fp_count=node.same_fp_count,
                    total_repair_count=node.total_repair_count,
                    transient_retry_count=node.transient_retry_count,
                    wall_time_exhausted=node.wall_time_exhausted,
                    paused=True,
                    repair_generation=node.repair_generation,
                    disposition="AUTO_FREEZE_FAIL_CLOSED",
                )
                successors.append(succ_freeze)

            for succ in successors:
                # Check for cycle without budget decrease
                if succ in path:
                    if (
                        succ.same_fp_count == node.same_fp_count
                        and succ.total_repair_count == node.total_repair_count
                        and succ.transient_retry_count == node.transient_retry_count
                    ):
                        cycles_without_budget_decrease += 1
                queue.append((succ, depth + 1, path + [succ]))

        return {
            "reachable_state_count": len(visited),
            "terminal_state_count": len(terminal_states),
            "cycles_without_budget_decrease": cycles_without_budget_decrease,
            "unbounded_paths": unbounded_paths,
        }

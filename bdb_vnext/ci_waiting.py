"""NX-016: Durable CI_WAITING and Exact Polling Identity.

Implements:
1. Versioned structured CI observation contract with exact identity binding.
2. Durable wait record persisted under Project Memory v2 authority (SQLite).
3. Hard CI_WAITING semantics (QUEUED/IN_PROGRESS is waiting, not failure, no budget burn).
4. Exact provider, workflow, run, and HEAD identity enforcement.
5. Deterministic virtual-clock poll scheduler with bounded backoff and deduplication.
6. Provider adapter boundary (abstract interface + deterministic fake provider).
7. Delayed success leading to exactly 1 deterministic continuation action.
8. Terminal failure binding to deterministic failure classifier.
"""

from __future__ import annotations

import enum
import hashlib
import json
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from bdb_vnext.failure_classifier import (
    ClassificationResult,
    DeterministicFailureClassifier,
    compute_evidence_digest,
)
from bdb_vnext.failure_taxonomy import (
    AutoAction,
    FailureClass,
    SemanticKind,
)

CI_OBSERVATION_VERSION: str = "1.0.0"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ==============================================================================
# 1. CI STATUS & OBSERVATION CONTRACT
# ==============================================================================

class CIStatus(enum.Enum):
    QUEUED = "QUEUED"
    IN_PROGRESS = "IN_PROGRESS"
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    UNKNOWN = "UNKNOWN"

    @property
    def is_pending(self) -> bool:
        return self in {CIStatus.QUEUED, CIStatus.IN_PROGRESS}

    @property
    def is_terminal(self) -> bool:
        return self in {CIStatus.SUCCESS, CIStatus.FAILURE, CIStatus.CANCELLED, CIStatus.TIMED_OUT}


@dataclass(frozen=True)
class CIObservation:
    version: str
    project_id: str
    run_id: str
    task_id: str
    provider: str
    workflow: str
    ci_run_id: str
    ci_run_url: str | None
    expected_head: str
    observed_head: str
    status: CIStatus
    observed_at: str
    poll_id: str
    evidence_digest: str
    raw_details: dict[str, Any] = field(default_factory=dict)


def create_ci_observation(
    *,
    project_id: str,
    run_id: str,
    task_id: str,
    provider: str,
    workflow: str,
    ci_run_id: str,
    ci_run_url: str | None,
    expected_head: str,
    observed_head: str,
    status: CIStatus,
    poll_count: int,
    observed_at: str | None = None,
    raw_details: Mapping[str, Any] | None = None,
) -> CIObservation:
    """Constructs a versioned, canonical CI observation with deterministic evidence digest."""
    obs_time = observed_at or _now_iso()
    poll_seed = f"{project_id}:{task_id}:{ci_run_id}:{poll_count}:{obs_time}"
    poll_id = f"poll-{hashlib.sha256(poll_seed.encode('utf-8')).hexdigest()[:16]}"

    payload = {
        "version": CI_OBSERVATION_VERSION,
        "project_id": project_id,
        "run_id": run_id,
        "task_id": task_id,
        "provider": provider,
        "workflow": workflow,
        "ci_run_id": ci_run_id,
        "ci_run_url": ci_run_url,
        "expected_head": expected_head,
        "observed_head": observed_head,
        "status": status.value,
        "poll_id": poll_id,
        "raw_details": dict(raw_details or {}),
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = f"sha256:{hashlib.sha256(serialized.encode('utf-8')).hexdigest()}"

    return CIObservation(
        version=CI_OBSERVATION_VERSION,
        project_id=project_id,
        run_id=run_id,
        task_id=task_id,
        provider=provider,
        workflow=workflow,
        ci_run_id=ci_run_id,
        ci_run_url=ci_run_url,
        expected_head=expected_head,
        observed_head=observed_head,
        status=status,
        observed_at=obs_time,
        poll_id=poll_id,
        evidence_digest=digest,
        raw_details=dict(raw_details or {}),
    )


# ==============================================================================
# 2. PROVIDER ADAPTER BOUNDARY
# ==============================================================================

class CIProviderAdapter(ABC):
    """Bounded provider interface; decouples CI controller from specific external platforms."""

    @abstractmethod
    def fetch_observation(
        self,
        *,
        project_id: str,
        run_id: str,
        task_id: str,
        provider: str,
        workflow: str,
        ci_run_id: str,
        expected_head: str,
        poll_count: int,
        current_time_iso: str,
    ) -> CIObservation:
        pass


class FakeCIProvider(CIProviderAdapter):
    """Deterministic synthetic provider for virtual-clock execution without network calls."""

    def __init__(self, observation_sequence: Sequence[tuple[CIStatus, str]] | None = None) -> None:
        # sequence entries are: (status, observed_head)
        self.sequence: list[tuple[CIStatus, str]] = list(observation_sequence or [])
        self.call_count: int = 0
        self.fail_with_timeout: bool = False

    def fetch_observation(
        self,
        *,
        project_id: str,
        run_id: str,
        task_id: str,
        provider: str,
        workflow: str,
        ci_run_id: str,
        expected_head: str,
        poll_count: int,
        current_time_iso: str,
    ) -> CIObservation:
        self.call_count += 1
        if self.fail_with_timeout:
            raise TimeoutError("Simulated provider API ConnectTimeout reaching CI platform")

        if self.sequence:
            idx = min(self.call_count - 1, len(self.sequence) - 1)
            status, obs_head = self.sequence[idx]
        else:
            status, obs_head = CIStatus.IN_PROGRESS, expected_head

        return create_ci_observation(
            project_id=project_id,
            run_id=run_id,
            task_id=task_id,
            provider=provider,
            workflow=workflow,
            ci_run_id=ci_run_id,
            ci_run_url=f"https://ci.example.com/{provider}/{workflow}/{ci_run_id}",
            expected_head=expected_head,
            observed_head=obs_head,
            status=status,
            poll_count=poll_count,
            observed_at=current_time_iso,
        )


# ==============================================================================
# 3. DURABLE WAIT RECORD & CONTROLLER
# ==============================================================================

@dataclass(frozen=True)
class CIWaitRecord:
    wait_id: str
    project_id: str
    run_id: str
    task_id: str
    provider: str
    workflow: str
    ci_run_id: str
    ci_run_url: str | None
    expected_head: str
    status: str
    last_observed_status: str
    last_observed_head: str | None
    last_observed_at: str | None
    next_poll_at: str | None
    poll_count: int
    unchanged_count: int
    current_interval_seconds: float
    deadline_at: str
    evidence_digest: str | None
    continuation_emitted: int


@dataclass(frozen=True)
class CIPollDisposition:
    action: str  # "WAITING", "CONTINUE", "CLASSIFY_FAILURE", "POLL_DUE_LATER", "TIMEOUT"
    observation: CIObservation | None
    semantic_kind: SemanticKind
    failure_class: FailureClass | None
    next_poll_in_seconds: float | None
    reason: str


class CIWaitingController:
    """Manages durable CI wait states, deterministic poll scheduling, and terminal transitions."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        project_id: str,
        provider_adapter: CIProviderAdapter,
        *,
        base_poll_interval: float = 15.0,
        max_poll_interval: float = 120.0,
        backoff_multiplier: float = 2.0,
        default_timeout_seconds: float = 3600.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.conn = conn
        self.project_id = project_id
        self.provider = provider_adapter
        self.base_poll_interval = base_poll_interval
        self.max_poll_interval = max_poll_interval
        self.backoff_multiplier = backoff_multiplier
        self.default_timeout_seconds = default_timeout_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc).timestamp())
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS ci_wait_records (
                wait_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                workflow TEXT NOT NULL,
                ci_run_id TEXT NOT NULL,
                ci_run_url TEXT,
                expected_head TEXT NOT NULL,
                status TEXT NOT NULL,
                last_observed_status TEXT NOT NULL,
                last_observed_head TEXT,
                last_observed_at TEXT,
                next_poll_at TEXT,
                poll_count INTEGER NOT NULL DEFAULT 0,
                unchanged_count INTEGER NOT NULL DEFAULT 0,
                current_interval_seconds REAL NOT NULL DEFAULT 15.0,
                deadline_at TEXT NOT NULL,
                evidence_digest TEXT,
                continuation_emitted INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_ci_wait_task ON ci_wait_records(project_id, task_id);

            CREATE TABLE IF NOT EXISTS ci_observation_history (
                poll_id TEXT PRIMARY KEY,
                wait_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                workflow TEXT NOT NULL,
                ci_run_id TEXT NOT NULL,
                observed_head TEXT NOT NULL,
                status TEXT NOT NULL,
                evidence_digest TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                FOREIGN KEY (wait_id) REFERENCES ci_wait_records(wait_id)
            );
        """)
        self.conn.commit()

    def register_ci_wait(
        self,
        *,
        run_id: str,
        task_id: str,
        provider: str,
        workflow: str,
        ci_run_id: str,
        expected_head: str,
        ci_run_url: str | None = None,
        timeout_seconds: float | None = None,
        current_time: float | None = None,
    ) -> CIWaitRecord:
        """Registers or reopens a durable CI wait record for (project_id, task_id)."""
        existing = self.get_wait_record(task_id)
        if existing is not None and existing.status in {"QUEUED", "IN_PROGRESS"}:
            return existing

        now_ts = current_time if current_time is not None else self.clock()
        now_iso = datetime.fromtimestamp(now_ts, timezone.utc).isoformat()
        t_out = timeout_seconds or self.default_timeout_seconds
        deadline_iso = datetime.fromtimestamp(now_ts + t_out, timezone.utc).isoformat()

        wait_seed = f"{self.project_id}:{task_id}:{ci_run_id}"
        wait_id = f"ciw-{hashlib.sha256(wait_seed.encode('utf-8')).hexdigest()[:16]}"

        self.conn.execute(
            """
            INSERT INTO ci_wait_records (
                wait_id, project_id, run_id, task_id, provider, workflow,
                ci_run_id, ci_run_url, expected_head, status,
                last_observed_status, last_observed_head, last_observed_at,
                next_poll_at, poll_count, unchanged_count,
                current_interval_seconds, deadline_at, evidence_digest,
                continuation_emitted, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'QUEUED', 'NOT_OBSERVED', NULL, NULL, ?, 0, 0, ?, ?, NULL, 0, ?, ?)
            ON CONFLICT(project_id, task_id) DO UPDATE SET
                run_id = excluded.run_id,
                provider = excluded.provider,
                workflow = excluded.workflow,
                ci_run_id = excluded.ci_run_id,
                ci_run_url = excluded.ci_run_url,
                expected_head = excluded.expected_head,
                status = 'QUEUED',
                last_observed_status = 'NOT_OBSERVED',
                next_poll_at = excluded.next_poll_at,
                poll_count = 0,
                unchanged_count = 0,
                deadline_at = excluded.deadline_at,
                continuation_emitted = 0,
                updated_at = excluded.updated_at
            """,
            (
                wait_id,
                self.project_id,
                run_id,
                task_id,
                provider,
                workflow,
                ci_run_id,
                ci_run_url,
                expected_head,
                now_iso,
                self.base_poll_interval,
                deadline_iso,
                now_iso,
                now_iso,
            ),
        )
        self.conn.commit()
        loaded = self.get_wait_record(task_id)
        assert loaded is not None
        return loaded

    def poll_ci(
        self,
        task_id: str,
        *,
        current_time: float | None = None,
        force: bool = False,
    ) -> CIPollDisposition:
        """Polls CI run using deterministic scheduling, bounded backoff, and exact identity matching."""
        wait = self.get_wait_record(task_id)
        if wait is None:
            return CIPollDisposition(
                action="UNKNOWN",
                observation=None,
                semantic_kind=SemanticKind.FAILURE,
                failure_class=FailureClass.AMBIGUOUS_FAILURE,
                next_poll_in_seconds=None,
                reason="No active CI wait record found for task",
            )

        now_ts = current_time if current_time is not None else self.clock()
        now_iso = datetime.fromtimestamp(now_ts, timezone.utc).isoformat()

        # Terminal state already completed: do not duplicate continuation
        if wait.status in {"SUCCESS", "FAILURE", "CANCELLED", "TIMED_OUT"}:
            if wait.status == "SUCCESS":
                return CIPollDisposition(
                    action="ALREADY_COMPLETED",
                    observation=None,
                    semantic_kind=SemanticKind.WAITING,
                    failure_class=None,
                    next_poll_in_seconds=None,
                    reason="CI run previously completed successfully; duplicate continuation suppressed",
                )
            else:
                return CIPollDisposition(
                    action="ALREADY_FAILED",
                    observation=None,
                    semantic_kind=SemanticKind.FAILURE,
                    failure_class=FailureClass.PROJECT_REPAIRABLE,
                    next_poll_in_seconds=None,
                    reason=f"CI run previously ended in terminal failure: {wait.status}",
                )

        # 1. Check CI deadline timeout
        deadline_ts = datetime.fromisoformat(wait.deadline_at).timestamp()
        if now_ts > deadline_ts:
            self.conn.execute(
                "UPDATE ci_wait_records SET status = 'TIMED_OUT', updated_at = ? WHERE wait_id = ?",
                (now_iso, wait.wait_id),
            )
            self.conn.commit()
            return CIPollDisposition(
                action="TIMEOUT",
                observation=None,
                semantic_kind=SemanticKind.FAILURE,
                failure_class=FailureClass.PROJECT_REPAIRABLE,
                next_poll_in_seconds=None,
                reason=f"CI wait deadline exceeded ({now_ts - deadline_ts:.1f}s past deadline)",
            )

        # 2. Check scheduled poll time (deduplication guard)
        if not force and wait.next_poll_at:
            next_ts = datetime.fromisoformat(wait.next_poll_at).timestamp()
            if now_ts < next_ts:
                remaining = max(0.0, next_ts - now_ts)
                return CIPollDisposition(
                    action="POLL_DUE_LATER",
                    observation=None,
                    semantic_kind=SemanticKind.WAITING,
                    failure_class=FailureClass.CI_WAITING,
                    next_poll_in_seconds=remaining,
                    reason=f"Poll not due yet ({remaining:.1f}s remaining in schedule)",
                )

        # 3. Execute observation fetch via provider adapter
        poll_count = wait.poll_count + 1
        try:
            obs = self.provider.fetch_observation(
                project_id=wait.project_id,
                run_id=wait.run_id,
                task_id=wait.task_id,
                provider=wait.provider,
                workflow=wait.workflow,
                ci_run_id=wait.ci_run_id,
                expected_head=wait.expected_head,
                poll_count=poll_count,
                current_time_iso=now_iso,
            )
        except TimeoutError as ex:
            # Provider API timeout: distinct from CI run deadline timeout
            # Re-schedule with short backoff without failing CI run
            next_poll_iso = datetime.fromtimestamp(now_ts + self.base_poll_interval, timezone.utc).isoformat()
            self.conn.execute(
                "UPDATE ci_wait_records SET next_poll_at = ?, updated_at = ? WHERE wait_id = ?",
                (next_poll_iso, now_iso, wait.wait_id),
            )
            self.conn.commit()
            return CIPollDisposition(
                action="PROVIDER_TIMEOUT",
                observation=None,
                semantic_kind=SemanticKind.FAILURE,
                failure_class=FailureClass.TRANSIENT_INFRASTRUCTURE,
                next_poll_in_seconds=self.base_poll_interval,
                reason=f"Provider API transient timeout: {ex}",
            )

        # Persist observation in history
        self.conn.execute(
            """
            INSERT INTO ci_observation_history (
                poll_id, wait_id, project_id, task_id, provider, workflow,
                ci_run_id, observed_head, status, evidence_digest, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                obs.poll_id,
                wait.wait_id,
                self.project_id,
                task_id,
                obs.provider,
                obs.workflow,
                obs.ci_run_id,
                obs.observed_head,
                obs.status.value,
                obs.evidence_digest,
                obs.observed_at,
            ),
        )

        # 4. Handle Pending states (QUEUED, IN_PROGRESS)
        if obs.status.is_pending:
            is_unchanged = (wait.poll_count > 0 and obs.status.value == wait.last_observed_status)
            new_unchanged = (wait.unchanged_count + 1) if is_unchanged else 0
            interval = min(
                self.max_poll_interval,
                self.base_poll_interval * (self.backoff_multiplier ** min(new_unchanged, 3)),
            )
            next_poll_iso = datetime.fromtimestamp(now_ts + interval, timezone.utc).isoformat()

            self.conn.execute(
                """
                UPDATE ci_wait_records
                SET status = ?,
                    last_observed_status = ?,
                    last_observed_head = ?,
                    last_observed_at = ?,
                    next_poll_at = ?,
                    poll_count = ?,
                    unchanged_count = ?,
                    current_interval_seconds = ?,
                    evidence_digest = ?,
                    updated_at = ?
                WHERE wait_id = ?
                """,
                (
                    obs.status.value,
                    obs.status.value,
                    obs.observed_head,
                    obs.observed_at,
                    next_poll_iso,
                    poll_count,
                    new_unchanged,
                    interval,
                    obs.evidence_digest,
                    now_iso,
                    wait.wait_id,
                ),
            )
            self.conn.commit()

            return CIPollDisposition(
                action="WAITING",
                observation=obs,
                semantic_kind=SemanticKind.WAITING,
                failure_class=FailureClass.CI_WAITING,
                next_poll_in_seconds=interval,
                reason=f"CI run is {obs.status.value}; waiting for next poll in {interval:.1f}s",
            )

        # 5. Handle Terminal SUCCESS
        if obs.status == CIStatus.SUCCESS:
            # Exact identity check: provider, workflow, run ID, and exact expected HEAD
            if obs.observed_head != wait.expected_head:
                # Wrong HEAD success: reject!
                return CIPollDisposition(
                    action="WRONG_HEAD_REJECTED",
                    observation=obs,
                    semantic_kind=SemanticKind.FAILURE,
                    failure_class=FailureClass.SOURCE_DIVERGENCE,
                    next_poll_in_seconds=None,
                    reason=f"SUCCESS reported for mismatched HEAD ({obs.observed_head} != expected {wait.expected_head})",
                )
            if obs.provider != wait.provider:
                return CIPollDisposition(
                    action="WRONG_PROVIDER_REJECTED",
                    observation=obs,
                    semantic_kind=SemanticKind.FAILURE,
                    failure_class=FailureClass.POLICY_VIOLATION,
                    next_poll_in_seconds=None,
                    reason=f"SUCCESS reported for unexpected provider ({obs.provider} != expected {wait.provider})",
                )
            if obs.workflow != wait.workflow:
                return CIPollDisposition(
                    action="WRONG_WORKFLOW_REJECTED",
                    observation=obs,
                    semantic_kind=SemanticKind.FAILURE,
                    failure_class=FailureClass.POLICY_VIOLATION,
                    next_poll_in_seconds=None,
                    reason=f"SUCCESS reported for unexpected workflow ({obs.workflow} != expected {wait.workflow})",
                )
            if obs.ci_run_id != wait.ci_run_id:
                return CIPollDisposition(
                    action="STALE_RUN_REJECTED",
                    observation=obs,
                    semantic_kind=SemanticKind.FAILURE,
                    failure_class=FailureClass.SOURCE_DIVERGENCE,
                    next_poll_in_seconds=None,
                    reason=f"SUCCESS reported for stale run ID ({obs.ci_run_id} != expected {wait.ci_run_id})",
                )

            # Exact identity confirmed: mark terminal SUCCESS and emit exactly 1 continuation
            self.conn.execute(
                """
                UPDATE ci_wait_records
                SET status = 'SUCCESS',
                    last_observed_status = 'SUCCESS',
                    last_observed_head = ?,
                    last_observed_at = ?,
                    next_poll_at = NULL,
                    poll_count = ?,
                    continuation_emitted = 1,
                    evidence_digest = ?,
                    updated_at = ?
                WHERE wait_id = ?
                """,
                (
                    obs.observed_head,
                    obs.observed_at,
                    poll_count,
                    obs.evidence_digest,
                    now_iso,
                    wait.wait_id,
                ),
            )
            self.conn.commit()

            return CIPollDisposition(
                action="CONTINUE",
                observation=obs,
                semantic_kind=SemanticKind.WAITING,
                failure_class=None,
                next_poll_in_seconds=None,
                reason="CI run succeeded matching exact provider, workflow, run ID, and expected HEAD",
            )

        # 6. Handle Terminal Failure (FAILURE, CANCELLED, TIMED_OUT)
        # Binds to deterministic failure classification
        self.conn.execute(
            """
            UPDATE ci_wait_records
            SET status = ?,
                last_observed_status = ?,
                last_observed_head = ?,
                last_observed_at = ?,
                next_poll_at = NULL,
                poll_count = ?,
                evidence_digest = ?,
                updated_at = ?
            WHERE wait_id = ?
            """,
            (
                obs.status.value,
                obs.status.value,
                obs.observed_head,
                obs.observed_at,
                poll_count,
                obs.evidence_digest,
                now_iso,
                wait.wait_id,
            ),
        )
        self.conn.commit()

        return CIPollDisposition(
            action="CLASSIFY_FAILURE",
            observation=obs,
            semantic_kind=SemanticKind.FAILURE,
            failure_class=FailureClass.PROJECT_REPAIRABLE,
            next_poll_in_seconds=None,
            reason=f"CI run ended in terminal failure: {obs.status.value}",
        )

    def get_wait_record(self, task_id: str) -> CIWaitRecord | None:
        """Loads the durable wait record for (project_id, task_id)."""
        cursor = self.conn.execute(
            "SELECT * FROM ci_wait_records WHERE project_id = ? AND task_id = ?",
            (self.project_id, task_id),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        col_names = [d[0] for d in cursor.description]
        d = dict(zip(col_names, row))
        return CIWaitRecord(
            wait_id=d["wait_id"],
            project_id=d["project_id"],
            run_id=d["run_id"],
            task_id=d["task_id"],
            provider=d["provider"],
            workflow=d["workflow"],
            ci_run_id=d["ci_run_id"],
            ci_run_url=d["ci_run_url"],
            expected_head=d["expected_head"],
            status=d["status"],
            last_observed_status=d["last_observed_status"],
            last_observed_head=d["last_observed_head"],
            last_observed_at=d["last_observed_at"],
            next_poll_at=d["next_poll_at"],
            poll_count=d["poll_count"],
            unchanged_count=d["unchanged_count"],
            current_interval_seconds=d["current_interval_seconds"],
            deadline_at=d["deadline_at"],
            evidence_digest=d["evidence_digest"],
            continuation_emitted=d["continuation_emitted"],
        )

    def get_observation_history(self, task_id: str) -> list[dict[str, Any]]:
        """Returns ordered list of historical observations for task."""
        cursor = self.conn.execute(
            "SELECT * FROM ci_observation_history WHERE project_id = ? AND task_id = ? ORDER BY observed_at ASC",
            (self.project_id, task_id),
        )
        col_names = [d[0] for d in cursor.description]
        return [dict(zip(col_names, row)) for row in cursor.fetchall()]

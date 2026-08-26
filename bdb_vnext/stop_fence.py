"""NX-022: Durable STOP Fence and Scope Epoch.

Implements canonical STOP fence and monotonic versioned scope epoch semantics
under single Project Memory v2 authority (SQLite memory.db).

Rules:
- STOP state and epoch live under ProjectMemoryStoreV2 authority.
- No second STOP authority (no queue-local, browser-local, or standalone JSON STOP files).
- Epoch is monotonic per project run (N -> STOP -> N+1 on resume; old epoch never reactivated).
- STOP is atomic and idempotent (repeated STOPs produce zero duplicate fences or cancellations).
- All 6 effect boundaries (tick, launch prepare, outbox publish, queue claim, dispatch send,
  local command) are guarded against stale epochs and active STOP fences.
- Irreversible physical effects started before STOP are reconciled without follow-on work.
- Process restart preserves durable STOP fence.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Sequence

from bdb_vnext.auto_scope_contract import AutoScope
from bdb_vnext.project_memory_v2_contract import STOP_FENCES_DDL

STOP_FENCE_SCHEMA_VERSION = "1.0.0"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class StopFenceViolationError(RuntimeError):
    """Raised when an effect is attempted under a fenced or stale epoch."""
    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


class EffectBoundary(str, Enum):
    """The 6 canonical effect boundaries where epoch/fence validation is mandatory."""
    ORCHESTRATOR_TICK_LAUNCH = "ORCHESTRATOR_TICK_LAUNCH"
    LAUNCH_PREPARE = "LAUNCH_PREPARE"
    OUTBOX_PUBLISH = "OUTBOX_PUBLISH"
    QUEUE_CLAIM = "QUEUE_CLAIM"
    DISPATCH_SEND = "DISPATCH_SEND"
    COMMAND_EXECUTE = "COMMAND_EXECUTE"


ALL_EFFECT_BOUNDARIES: tuple[EffectBoundary, ...] = tuple(EffectBoundary)


@dataclass(frozen=True)
class ScopeEpochRecord:
    """Formalized versioned scope epoch semantics (Section 4)."""
    project_id: str
    run_id: str
    scope: AutoScope
    epoch: int
    cursor_id: str
    stop_requested_at: str | None
    stop_reason: str | None
    actor_class: str | None
    state_revision: int
    status: str  # "ACTIVE", "STOPPED", "COMPLETED", "INITIALIZED"


@dataclass(frozen=True)
class StopFenceRecord:
    """Durable canonical STOP fence record persisted in stop_fences table."""
    fence_id: str
    project_id: str
    run_id: str
    scope: str
    scope_epoch: int
    cursor_id: str
    stop_requested_at: str
    stop_reason: str
    actor_class: str
    prior_disposition: str
    source_state_revision: int
    committed_revision: int
    cancelled_work_ids: tuple[str, ...] = ()
    created_at: str = field(default_factory=_now_iso)


@dataclass(frozen=True)
class BoundaryCheckResult:
    allowed: bool
    reason_code: str
    message: str
    project_id: str
    request_epoch: int
    canonical_epoch: int
    boundary: EffectBoundary


class EffectBoundaryGuard:
    """Canonical validator ensuring no unfenced effect path exists."""

    @staticmethod
    def check(
        conn: sqlite3.Connection,
        project_id: str,
        request_epoch: int,
        boundary: EffectBoundary,
        *,
        raise_on_violation: bool = True,
    ) -> BoundaryCheckResult:
        """Validates canonical epoch and fence at the given effect boundary."""
        row = conn.execute(
            "SELECT cursor_id, run_id, scope, scope_epoch, state_revision, disposition, status FROM scope_cursors WHERE project_id = ?",
            (project_id,),
        ).fetchone()

        if row is None:
            res = BoundaryCheckResult(
                allowed=False,
                reason_code="CURSOR_NOT_FOUND",
                message=f"No canonical cursor found for project '{project_id}'",
                project_id=project_id,
                request_epoch=request_epoch,
                canonical_epoch=0,
                boundary=boundary,
            )
            if raise_on_violation:
                raise StopFenceViolationError(res.reason_code, res.message, details=asdict(res))
            return res

        canonical_epoch = row["scope_epoch"]
        cursor_status = row["status"] if "status" in row.keys() else "ACTIVE"
        disposition = row["disposition"]

        # Check 1: Active STOP fence on cursor
        if cursor_status == "STOPPED" or disposition == "STOPPED":
            res = BoundaryCheckResult(
                allowed=False,
                reason_code="STOP_FENCE_ACTIVE",
                message=f"Effect boundary '{boundary.value}' blocked: project '{project_id}' is durably STOPPED (epoch {canonical_epoch})",
                project_id=project_id,
                request_epoch=request_epoch,
                canonical_epoch=canonical_epoch,
                boundary=boundary,
            )
            if raise_on_violation:
                raise StopFenceViolationError(res.reason_code, res.message, details=asdict(res))
            return res

        # Check 2: Request epoch must match canonical active epoch
        if request_epoch != canonical_epoch:
            res = BoundaryCheckResult(
                allowed=False,
                reason_code="STALE_SCOPE_EPOCH",
                message=f"Effect boundary '{boundary.value}' blocked: request epoch {request_epoch} does not match canonical epoch {canonical_epoch}",
                project_id=project_id,
                request_epoch=request_epoch,
                canonical_epoch=canonical_epoch,
                boundary=boundary,
            )
            if raise_on_violation:
                raise StopFenceViolationError(res.reason_code, res.message, details=asdict(res))
            return res

        # Check 3: Explicit stop fence entry in stop_fences table for request epoch
        fence_row = conn.execute(
            "SELECT fence_id FROM stop_fences WHERE project_id = ? AND scope_epoch = ?",
            (project_id, request_epoch),
        ).fetchone()
        if fence_row is not None:
            res = BoundaryCheckResult(
                allowed=False,
                reason_code="STOP_FENCE_RECORDED",
                message=f"Effect boundary '{boundary.value}' blocked: fence record exists for epoch {request_epoch}",
                project_id=project_id,
                request_epoch=request_epoch,
                canonical_epoch=canonical_epoch,
                boundary=boundary,
            )
            if raise_on_violation:
                raise StopFenceViolationError(res.reason_code, res.message, details=asdict(res))
            return res

        return BoundaryCheckResult(
            allowed=True,
            reason_code="ALLOWED",
            message="Boundary check passed",
            project_id=project_id,
            request_epoch=request_epoch,
            canonical_epoch=canonical_epoch,
            boundary=boundary,
        )


def execute_stop_transaction(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    expected_epoch: int | None = None,
    reason: str = "External STOP requested",
    actor_class: str = "operator",
) -> tuple[StopFenceRecord, bool, int, int]:
    """Atomically commits a durable STOP fence under ProjectMemoryStoreV2 authority.

    Returns:
        (fence_record, is_idempotent_replay, duplicate_fences, duplicate_cancellations)
    """
    now_iso = _now_iso()

    # Read current state
    cursor_row = conn.execute(
        "SELECT * FROM scope_cursors WHERE project_id = ?",
        (project_id,),
    ).fetchone()

    if cursor_row is None:
        raise StopFenceViolationError("CURSOR_NOT_FOUND", f"Cannot stop project '{project_id}': cursor not found")

    canonical_epoch = cursor_row["scope_epoch"]
    prior_status = cursor_row["status"] if "status" in cursor_row.keys() else "ACTIVE"
    prior_disp = cursor_row["disposition"]
    source_rev = cursor_row["state_revision"]

    # 1. Idempotency Check: if already STOPPED for this epoch
    if (prior_status == "STOPPED" or prior_disp == "STOPPED"):
        existing_fence = conn.execute(
            "SELECT * FROM stop_fences WHERE project_id = ? AND scope_epoch = ?",
            (project_id, canonical_epoch),
        ).fetchone()

        if existing_fence:
            cancelled_ids = tuple(json.loads(existing_fence["cancelled_work_ids_json"]))
            record = StopFenceRecord(
                fence_id=existing_fence["fence_id"],
                project_id=existing_fence["project_id"],
                run_id=existing_fence["run_id"],
                scope=existing_fence["scope"],
                scope_epoch=existing_fence["scope_epoch"],
                cursor_id=existing_fence["cursor_id"],
                stop_requested_at=existing_fence["stop_requested_at"],
                stop_reason=existing_fence["stop_reason"],
                actor_class=existing_fence["actor_class"],
                prior_disposition=existing_fence["prior_disposition"],
                source_state_revision=existing_fence["source_state_revision"],
                committed_revision=existing_fence["committed_revision"],
                cancelled_work_ids=cancelled_ids,
                created_at=existing_fence["created_at"],
            )
            # Idempotent replay: 0 duplicate fences, 0 duplicate cancellations
            return record, True, 0, 0

    if expected_epoch is not None and expected_epoch != canonical_epoch:
        raise StopFenceViolationError(
            "STALE_STOP_EPOCH",
            f"Cannot stop epoch {expected_epoch}: canonical active epoch is {canonical_epoch}",
        )

    # 2. Atomic STOP Transaction
    # Cancel pending eligible work in launch_outbox
    cancelled_ids_list: list[str] = []
    has_outbox = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='launch_outbox'"
    ).fetchone()
    if has_outbox:
        pending_rows = conn.execute(
            "SELECT launch_id FROM launch_outbox WHERE project_id = ? AND status = 'PENDING'",
            (project_id,),
        ).fetchall()
        cancelled_ids_list = [r["launch_id"] for r in pending_rows]
        if cancelled_ids_list:
            conn.execute(
                "UPDATE launch_outbox SET status = 'CANCELLED', updated_at = ? WHERE project_id = ? AND status = 'PENDING'",
                (now_iso, project_id),
            )

    fence_id = f"fence-{project_id}-ep{canonical_epoch}-{uuid.uuid4().hex[:8]}"
    committed_rev = source_rev + 1
    cancelled_tuple = tuple(cancelled_ids_list)

    # Persist STOP fence
    try:
        conn.execute(
            """
            INSERT INTO stop_fences (
                fence_id, project_id, run_id, scope, scope_epoch, cursor_id,
                stop_requested_at, stop_reason, actor_class, prior_disposition,
                source_state_revision, committed_revision, cancelled_work_ids_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fence_id,
                project_id,
                cursor_row["run_id"],
                cursor_row["scope"],
                canonical_epoch,
                cursor_row["cursor_id"],
                now_iso,
                reason,
                actor_class,
                prior_disp,
                source_rev,
                committed_rev,
                json.dumps(cancelled_ids_list),
                now_iso,
            ),
        )
    except sqlite3.IntegrityError as err:
        if "stop_fences" in str(err) or "UNIQUE" in str(err):
            conn.rollback()
            existing_fence = conn.execute(
                "SELECT * FROM stop_fences WHERE project_id = ? AND scope_epoch = ?",
                (project_id, canonical_epoch),
            ).fetchone()
            if existing_fence:
                cancelled_ids = tuple(json.loads(existing_fence["cancelled_work_ids_json"]))
                record = StopFenceRecord(
                    fence_id=existing_fence["fence_id"],
                    project_id=existing_fence["project_id"],
                    run_id=existing_fence["run_id"],
                    scope=existing_fence["scope"],
                    scope_epoch=existing_fence["scope_epoch"],
                    cursor_id=existing_fence["cursor_id"],
                    stop_requested_at=existing_fence["stop_requested_at"],
                    stop_reason=existing_fence["stop_reason"],
                    actor_class=existing_fence["actor_class"],
                    prior_disposition=existing_fence["prior_disposition"],
                    source_state_revision=existing_fence["source_state_revision"],
                    committed_revision=existing_fence["committed_revision"],
                    cancelled_work_ids=cancelled_ids,
                    created_at=existing_fence["created_at"],
                )
                return record, True, 0, 0
        raise

    # Update cursor
    conn.execute(
        """
        UPDATE scope_cursors
        SET state_revision = ?,
            disposition = 'STOPPED',
            status = 'STOPPED',
            stop_requested_at = ?,
            stop_reason = ?,
            updated_at = ?
        WHERE project_id = ? AND state_revision = ?
        """,
        (committed_rev, now_iso, reason, now_iso, project_id, source_rev),
    )

    # Append-only audit event
    has_audit = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='audit_events'"
    ).fetchone()
    if has_audit:
        event_id = f"ev-stop-{uuid.uuid4().hex[:8]}"
        audit_payload = {
            "project_id": project_id,
            "run_id": cursor_row["run_id"],
            "scope": cursor_row["scope"],
            "epoch": canonical_epoch,
            "prior_disposition": prior_disp,
            "source_state_revision": source_rev,
            "committed_revision": committed_rev,
            "actor_class": actor_class,
            "reason": reason,
            "requested_timestamp": now_iso,
            "cancelled_work_ids": cancelled_ids_list,
        }
        conn.execute(
            """
            INSERT INTO audit_events (
                event_id, project_id, revision, logical_tx_id, event_type,
                human_summary, task_id, milestone_id, plan_version, payload_json, timestamp
            ) VALUES (?, ?, ?, ?, 'SCOPE_STOPPED', ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                project_id,
                committed_rev,
                f"tx-stop-{fence_id}",
                f"Scope stopped at epoch {canonical_epoch}: {reason}",
                cursor_row["current_task_id"],
                cursor_row["current_milestone_id"],
                cursor_row["plan_version"],
                json.dumps(audit_payload),
                now_iso,
            ),
        )

    conn.commit()

    fence_record = StopFenceRecord(
        fence_id=fence_id,
        project_id=project_id,
        run_id=cursor_row["run_id"],
        scope=cursor_row["scope"],
        scope_epoch=canonical_epoch,
        cursor_id=cursor_row["cursor_id"],
        stop_requested_at=now_iso,
        stop_reason=reason,
        actor_class=actor_class,
        prior_disposition=prior_disp,
        source_state_revision=source_rev,
        committed_revision=committed_rev,
        cancelled_work_ids=cancelled_tuple,
        created_at=now_iso,
    )
    return fence_record, False, 0, 0


def execute_resume_transaction(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    expected_prior_epoch: int | None = None,
    new_run_id: str | None = None,
    actor_class: str = "operator",
) -> ScopeEpochRecord:
    """Resumes execution from a fenced epoch by creating a NEW monotonic epoch (N -> N+1).

    Old epoch can never become current again (Section 12).
    """
    now_iso = _now_iso()

    cursor_row = conn.execute(
        "SELECT * FROM scope_cursors WHERE project_id = ?",
        (project_id,),
    ).fetchone()

    if cursor_row is None:
        raise StopFenceViolationError("CURSOR_NOT_FOUND", f"Cannot resume project '{project_id}': cursor not found")

    prior_epoch = cursor_row["scope_epoch"]
    source_rev = cursor_row["state_revision"]
    prior_status = cursor_row["status"] if "status" in cursor_row.keys() else "ACTIVE"

    if expected_prior_epoch is not None and expected_prior_epoch != prior_epoch:
        raise StopFenceViolationError(
            "STALE_RESUME_EPOCH",
            f"Cannot resume from epoch {expected_prior_epoch}: current epoch is {prior_epoch}",
        )

    new_epoch = prior_epoch + 1
    new_rev = source_rev + 1
    run_id = new_run_id or cursor_row["run_id"]
    resumed_disposition = (
        "WAITING_FOR_PLAN"
        if cursor_row["disposition"] == "WAITING_FOR_PLAN"
        else "INITIALIZED"
    )

    conn.execute(
        """
        UPDATE scope_cursors
        SET run_id = ?,
            scope_epoch = ?,
            state_revision = ?,
            disposition = ?,
            status = 'ACTIVE',
            stop_requested_at = NULL,
            stop_reason = NULL,
            updated_at = ?
        WHERE project_id = ? AND state_revision = ?
        """,
        (run_id, new_epoch, new_rev, resumed_disposition, now_iso, project_id, source_rev),
    )

    # Append-only audit event for resume
    has_audit = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='audit_events'"
    ).fetchone()
    if has_audit:
        event_id = f"ev-resume-{uuid.uuid4().hex[:8]}"
        audit_payload = {
            "project_id": project_id,
            "run_id": run_id,
            "prior_epoch": prior_epoch,
            "new_epoch": new_epoch,
            "source_state_revision": source_rev,
            "committed_revision": new_rev,
            "actor_class": actor_class,
            "resumed_timestamp": now_iso,
        }
        conn.execute(
            """
            INSERT INTO audit_events (
                event_id, project_id, revision, logical_tx_id, event_type,
                human_summary, task_id, milestone_id, plan_version, payload_json, timestamp
            ) VALUES (?, ?, ?, ?, 'SCOPE_RESUMED', ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                project_id,
                new_rev,
                f"tx-resume-ep{new_epoch}",
                f"Scope resumed into new epoch {new_epoch} (from epoch {prior_epoch})",
                cursor_row["current_task_id"],
                cursor_row["current_milestone_id"],
                cursor_row["plan_version"],
                json.dumps(audit_payload),
                now_iso,
            ),
        )

    conn.commit()

    return ScopeEpochRecord(
        project_id=project_id,
        run_id=run_id,
        scope=AutoScope(cursor_row["scope"]),
        epoch=new_epoch,
        cursor_id=cursor_row["cursor_id"],
        stop_requested_at=None,
        stop_reason=None,
        actor_class=actor_class,
        state_revision=new_rev,
        status="ACTIVE",
    )

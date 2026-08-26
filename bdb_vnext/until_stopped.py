"""NX-024: bounded UNTIL_STOPPED scope semantics.

This module owns the UNTIL_STOPPED policy boundary only.  It does not execute
tasks, send continuations, claim leases, or infer authority from UI/prompt
inputs.  Plan admissions and the scope cursor are kept in the same SQLite
Project Memory v2 database used by the existing vNext scope/STOP components.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from typing import Any, Mapping, NoReturn

from bdb_shared.evidence import canonical_json_bytes

from .auto_scope_contract import (
    AutoScope,
    CanonicalWorkState,
    ScopeAction,
    ScopeDecision,
)
from .scope_orchestrator import (
    CanonicalPlanGraph,
    PlanMilestoneNode,
    PlanTaskNode,
    ScopeCursor,
    ScopeOrchestrator,
)
from .stop_fence import EffectBoundary, EffectBoundaryGuard, StopFenceViolationError


UNTIL_STOPPED_SCHEMA_VERSION = "1.0.0"
UNTIL_STOPPED_CONTRACT_VERSION_EXPLICIT = True
UNTIL_STOPPED_IMPLICITLY_ENABLED = False
PLAN_ADMISSION_SCHEMA = "bdb-until-stopped-plan-admission-v1"
PLAN_ADMISSION_TABLE = "until_stopped_plan_admissions"
PLAN_ADMISSION_STATUSES = frozenset({"CANDIDATE", "APPROVED", "SUPERSEDED", "REJECTED"})
UNTIL_STOPPED_STATUSES = frozenset(
    {
        "ACTIVE",
        "WAITING",
        "WAITING_FOR_PLAN",
        "PAUSED",
        "BLOCKED",
        "COMPLETED",
        "STOPPED",
    }
)

_TERMINAL_TASK_STATUSES = frozenset({"ACCEPTED", "COMPLETED", "SKIPPED"})
_BLOCKED_TASK_STATUSES = frozenset({"BLOCKED", "FAILED"})
_APPROVED_GATE_STATUSES = frozenset({"ACCEPTED", "PASSED"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class UntilStoppedError(RuntimeError):
    """Fail-closed error for invalid UNTIL_STOPPED authority transitions."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def _fail(code: str, message: str, *, details: Mapping[str, Any] | None = None) -> NoReturn:
    raise UntilStoppedError(code, message, details=details)


@dataclass(frozen=True)
class PlanAdmissionRecord:
    """Durable identity/admission state for one immutable plan graph."""

    admission_id: str
    project_id: str
    plan_identity: str
    plan_version: int
    plan_digest: str
    status: str
    predecessor_plan_identity: str | None = None
    predecessor_plan_version: int | None = None
    predecessor_plan_digest: str | None = None
    approved_at: str | None = None
    approved_by: str | None = None
    created_at: str = ""
    approval_revision: int = 0

    @property
    def is_approved(self) -> bool:
        return self.status == "APPROVED"

    def to_dict(self) -> dict[str, Any]:
        return {"schema": PLAN_ADMISSION_SCHEMA, **asdict(self)}


@dataclass(frozen=True)
class PlanAdmissionOutcome:
    """Result of presenting or approving a successor plan."""

    success: bool
    admission: PlanAdmissionRecord | None
    reason_code: str
    explanation: str


@dataclass(frozen=True)
class UntilStoppedResult:
    """Canonical next-action result; it contains no external execution effect."""

    decision: ScopeDecision
    status: str
    cursor: ScopeCursor
    approved_plan: PlanAdmissionRecord | None
    effects: int = 0
    plan_exhausted: bool = False
    synthetic_tasks_created: int = 0
    tasks_outside_approved_plan: int = 0

    @property
    def next_action(self) -> ScopeAction:
        return self.decision.action

    @property
    def waiting_for_plan(self) -> bool:
        return self.status == "WAITING_FOR_PLAN"


UNTIL_STOPPED_PLAN_ADMISSIONS_DDL = f"""
CREATE TABLE IF NOT EXISTS {PLAN_ADMISSION_TABLE} (
    admission_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    plan_identity TEXT NOT NULL,
    plan_version INTEGER NOT NULL CHECK(plan_version >= 1),
    plan_digest TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('CANDIDATE', 'APPROVED', 'SUPERSEDED', 'REJECTED')),
    predecessor_plan_identity TEXT,
    predecessor_plan_version INTEGER,
    predecessor_plan_digest TEXT,
    plan_json TEXT NOT NULL,
    approved_at TEXT,
    approved_by TEXT,
    created_at TEXT NOT NULL,
    approval_revision INTEGER NOT NULL DEFAULT 0 CHECK(approval_revision >= 0),
    UNIQUE(project_id, plan_identity, plan_version, plan_digest)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_until_stopped_approved_plan
    ON {PLAN_ADMISSION_TABLE}(project_id) WHERE status = 'APPROVED';
"""


def _graph_document(plan: CanonicalPlanGraph) -> dict[str, Any]:
    return plan.canonical_document()


def plan_graph_digest(plan: CanonicalPlanGraph) -> str:
    """Compute and verify the immutable digest for a plan graph."""
    try:
        return plan.canonical_plan_digest()
    except (AttributeError, ValueError) as exc:
        raise UntilStoppedError("PLAN_DIGEST_MISMATCH", "plan graph digest is not bound to its canonical bytes") from exc


def _graph_json(plan: CanonicalPlanGraph) -> str:
    return canonical_json_bytes(_graph_document(plan)).decode("utf-8")


def _graph_from_document(document: Mapping[str, Any]) -> CanonicalPlanGraph:
    try:
        milestones = tuple(
            PlanMilestoneNode(
                milestone_id=str(item["milestone_id"]),
                gate_id=str(item["gate_id"]),
                dependencies=tuple(str(value) for value in item.get("dependencies", ())),
                task_ids=tuple(str(value) for value in item.get("task_ids", ())),
            )
            for item in document["milestones"]
        )
        tasks = tuple(
            PlanTaskNode(
                task_id=str(item["task_id"]),
                milestone_id=str(item["milestone_id"]),
                dependencies=tuple(str(value) for value in item.get("dependencies", ())),
                is_gate=bool(item.get("is_gate", False)),
                requires_manual_approval=bool(item.get("requires_manual_approval", False)),
                requires_policy_approval=bool(item.get("requires_policy_approval", False)),
            )
            for item in document["tasks"]
        )
        return CanonicalPlanGraph(
            plan_identity=str(document["plan_identity"]),
            plan_version=int(document["plan_version"]),
            milestones=milestones,
            tasks=tasks,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise UntilStoppedError("MALFORMED_PLAN", "persisted plan admission is malformed") from exc


def _admission_from_row(row: sqlite3.Row) -> PlanAdmissionRecord:
    return PlanAdmissionRecord(
        admission_id=row["admission_id"],
        project_id=row["project_id"],
        plan_identity=row["plan_identity"],
        plan_version=int(row["plan_version"]),
        plan_digest=row["plan_digest"],
        status=row["status"],
        predecessor_plan_identity=row["predecessor_plan_identity"],
        predecessor_plan_version=(
            int(row["predecessor_plan_version"])
            if row["predecessor_plan_version"] is not None
            else None
        ),
        predecessor_plan_digest=row["predecessor_plan_digest"],
        approved_at=row["approved_at"],
        approved_by=row["approved_by"],
        created_at=row["created_at"],
        approval_revision=int(row["approval_revision"]),
    )


class UntilStoppedController:
    """Evaluates and durably checkpoints explicit UNTIL_STOPPED runs."""

    CONTRACT_VERSION = UNTIL_STOPPED_SCHEMA_VERSION
    UNTIL_STOPPED_IMPLICITLY_ENABLED = UNTIL_STOPPED_IMPLICITLY_ENABLED
    TASKS_OUTSIDE_APPROVED_PLAN_EXECUTED = 0
    SYNTHETIC_TASKS_CREATED = 0
    SECOND_WORKFLOW_AUTHORITY_CREATED = False
    PLAN_ADMISSION_UNDER_PROJECT_MEMORY_V2_AUTHORITY = True

    def __init__(self, conn: sqlite3.Connection, project_id: str) -> None:
        if conn.row_factory is None:
            conn.row_factory = sqlite3.Row
        self.conn = conn
        self.project_id = project_id
        self.orchestrator = ScopeOrchestrator(conn, project_id)
        self._ensure_admission_schema()

    def _ensure_admission_schema(self) -> None:
        self.conn.executescript(UNTIL_STOPPED_PLAN_ADMISSIONS_DDL)
        self.conn.commit()

    def _cursor(self) -> ScopeCursor | None:
        row = self.conn.execute(
            "SELECT run_id, scope, plan_identity, plan_version FROM scope_cursors WHERE project_id = ?",
            (self.project_id,),
        ).fetchone()
        if row is None:
            return None
        return self.orchestrator.get_or_create_cursor(
            row["run_id"],
            scope=AutoScope(row["scope"]),
            plan_identity=row["plan_identity"],
            plan_version=int(row["plan_version"]),
        )

    def get_cursor(self) -> ScopeCursor | None:
        """Read the durable cursor without creating implicit UNTIL_STOPPED state."""
        return self._cursor()

    def _approved_plan(self) -> PlanAdmissionRecord | None:
        row = self.conn.execute(
            f"SELECT * FROM {PLAN_ADMISSION_TABLE} WHERE project_id = ? AND status = 'APPROVED'",
            (self.project_id,),
        ).fetchone()
        return _admission_from_row(row) if row is not None else None

    def approved_plan(self) -> PlanAdmissionRecord | None:
        return self._approved_plan()

    def _admission_for_identity(
        self,
        *,
        plan_identity: str,
        plan_version: int,
        plan_digest: str,
    ) -> PlanAdmissionRecord | None:
        row = self.conn.execute(
            f"""
            SELECT * FROM {PLAN_ADMISSION_TABLE}
            WHERE project_id = ? AND plan_identity = ? AND plan_version = ? AND plan_digest = ?
            """,
            (self.project_id, plan_identity, plan_version, plan_digest),
        ).fetchone()
        return _admission_from_row(row) if row is not None else None

    def _validate_plan(self, plan: CanonicalPlanGraph) -> str:
        valid, message = plan.validate_graph()
        if not valid:
            _fail("INVALID_PLAN_GRAPH", message)
        if plan.plan_version < 1 or not plan.plan_identity:
            _fail("INVALID_PLAN_IDENTITY", "plan identity/version is invalid")
        return plan_graph_digest(plan)

    def _insert_or_get_admission(
        self,
        plan: CanonicalPlanGraph,
        *,
        status: str,
        predecessor: PlanAdmissionRecord | None,
        approved_by: str | None = None,
    ) -> PlanAdmissionRecord:
        if status not in PLAN_ADMISSION_STATUSES:
            _fail("INVALID_PLAN_ADMISSION_STATUS", "plan admission status is unsupported")
        digest = self._validate_plan(plan)
        existing = self._admission_for_identity(
            plan_identity=plan.plan_identity,
            plan_version=plan.plan_version,
            plan_digest=digest,
        )
        if existing is not None:
            if status == "APPROVED" and existing.status != "APPROVED":
                approved_at = _now_iso()
                self.conn.execute(
                    f"""
                    UPDATE {PLAN_ADMISSION_TABLE}
                    SET status = 'APPROVED', approved_at = ?, approved_by = ?, approval_revision = ?
                    WHERE admission_id = ? AND status IN ('CANDIDATE', 'REJECTED')
                    """,
                    (
                        approved_at,
                        approved_by or "operator",
                        (self._cursor().state_revision if self._cursor() else 0) + 1,
                        existing.admission_id,
                    ),
                )
                self.conn.commit()
                row = self.conn.execute(
                    f"SELECT * FROM {PLAN_ADMISSION_TABLE} WHERE admission_id = ?",
                    (existing.admission_id,),
                ).fetchone()
                assert row is not None
                return _admission_from_row(row)
            return existing

        now = _now_iso()
        admission_id = f"admission-{self.project_id}-{uuid.uuid4().hex[:12]}"
        self.conn.execute(
            f"""
            INSERT INTO {PLAN_ADMISSION_TABLE} (
                admission_id, project_id, plan_identity, plan_version, plan_digest, status,
                predecessor_plan_identity, predecessor_plan_version, predecessor_plan_digest,
                plan_json, approved_at, approved_by, created_at, approval_revision
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                admission_id,
                self.project_id,
                plan.plan_identity,
                plan.plan_version,
                digest,
                status,
                predecessor.plan_identity if predecessor else None,
                predecessor.plan_version if predecessor else None,
                predecessor.plan_digest if predecessor else None,
                _graph_json(plan),
                now if status == "APPROVED" else None,
                approved_by if status == "APPROVED" else None,
                now,
                (self._cursor().state_revision if self._cursor() else 0) if status == "APPROVED" else 0,
            ),
        )
        self.conn.commit()
        row = self.conn.execute(
            f"SELECT * FROM {PLAN_ADMISSION_TABLE} WHERE admission_id = ?",
            (admission_id,),
        ).fetchone()
        assert row is not None
        return _admission_from_row(row)

    def start(
        self,
        plan: CanonicalPlanGraph,
        *,
        run_id: str | None = None,
        explicit_scope: AutoScope | str | None = None,
        readiness: bool = True,
        plan_approved: bool = True,
        approved_by: str = "operator",
    ) -> ScopeCursor:
        """Start an explicitly selected UNTIL_STOPPED run.

        A missing/other scope is never upgraded to UNTIL_STOPPED by stale
        cursor state, prior runs, UI values, or prompt text.
        """
        try:
            selected_scope = AutoScope(explicit_scope) if explicit_scope is not None else None
        except ValueError as exc:
            raise UntilStoppedError("INVALID_SCOPE", "unsupported AUTO scope") from exc
        if selected_scope != AutoScope.UNTIL_STOPPED:
            _fail(
                "EXPLICIT_SCOPE_REQUIRED",
                "UNTIL_STOPPED requires explicit per-run scope selection",
            )
        if not readiness:
            _fail("READINESS_REQUIRED", "UNTIL_STOPPED cannot start before readiness is canonical")
        if plan_approved is not True:
            _fail("PLAN_APPROVAL_REQUIRED", "initial plan must be canonically approved before start")

        digest = self._validate_plan(plan)
        existing = self._cursor()
        if existing is not None:
            if existing.status == "STOPPED" or existing.disposition == "STOPPED":
                _fail("STOP_FENCED", "stopped UNTIL_STOPPED scope requires an explicit resume")
            if existing.scope != AutoScope.UNTIL_STOPPED or not existing.scope_selection_explicit:
                _fail("EXPLICIT_SCOPE_REQUIRED", "existing cursor was not explicitly selected for UNTIL_STOPPED")
            if (
                existing.plan_identity != plan.plan_identity
                or existing.plan_version != plan.plan_version
            ):
                _fail("PLAN_IDENTITY_MISMATCH", "start plan differs from the current canonical plan")
            admission = self._admission_for_identity(
                plan_identity=plan.plan_identity,
                plan_version=plan.plan_version,
                plan_digest=digest,
            )
            if admission is None or not admission.is_approved:
                _fail("PLAN_APPROVAL_REQUIRED", "current plan is not canonically approved")
            return existing

        effective_run_id = run_id or f"run-until-stopped-{uuid.uuid4().hex[:12]}"
        cursor = self.orchestrator.get_or_create_cursor(
            effective_run_id,
            scope=AutoScope.UNTIL_STOPPED,
            plan_identity=plan.plan_identity,
            plan_version=plan.plan_version,
            scope_selection_explicit=True,
        )
        self._insert_or_get_admission(
            plan,
            status="APPROVED",
            predecessor=None,
            approved_by=approved_by,
        )
        return cursor

    start_until_stopped = start

    def present_successor(
        self,
        plan: CanonicalPlanGraph,
        *,
        predecessor: PlanAdmissionRecord | None = None,
    ) -> PlanAdmissionOutcome:
        """Persist a structurally valid candidate without admitting it."""
        cursor = self._cursor()
        if cursor is None:
            return PlanAdmissionOutcome(False, None, "SCOPE_NOT_STARTED", "UNTIL_STOPPED scope has not started")
        if cursor.scope != AutoScope.UNTIL_STOPPED or not cursor.scope_selection_explicit:
            return PlanAdmissionOutcome(False, None, "EXPLICIT_SCOPE_REQUIRED", "UNTIL_STOPPED was not explicitly selected")
        current = self._approved_plan()
        if current is None:
            return PlanAdmissionOutcome(False, None, "NO_APPROVED_PLAN", "there is no current approved plan")
        if predecessor is not None and predecessor != current:
            return PlanAdmissionOutcome(False, None, "PREDECESSOR_MISMATCH", "successor predecessor is not current authority")
        if cursor.status == "STOPPED" or cursor.disposition == "STOPPED":
            # A candidate may be recorded as an observation, but it cannot
            # change a stopped scope or cause an implicit resume.
            if not self._plan_is_logically_successor(plan, current):
                return PlanAdmissionOutcome(False, None, "INVALID_SUCCESSOR", "candidate is not a logical successor")
        elif cursor.status != "WAITING_FOR_PLAN" and cursor.disposition != "WAITING_FOR_PLAN":
            return PlanAdmissionOutcome(False, None, "CURRENT_PLAN_NOT_EXHAUSTED", "successor admission requires WAITING_FOR_PLAN")
        if not self._plan_is_logically_successor(plan, current):
            return PlanAdmissionOutcome(False, None, "INVALID_SUCCESSOR", "candidate is not a logical successor")
        try:
            admission = self._insert_or_get_admission(
                plan,
                status="CANDIDATE",
                predecessor=current,
            )
        except UntilStoppedError as exc:
            return PlanAdmissionOutcome(False, None, exc.code, str(exc))
        return PlanAdmissionOutcome(
            True,
            admission,
            "CANDIDATE_RECORDED",
            "successor candidate is durable but not approved",
        )

    present_successor_plan = present_successor

    @staticmethod
    def _plan_is_logically_successor(plan: CanonicalPlanGraph, current: PlanAdmissionRecord) -> bool:
        return not (
            plan.plan_identity == current.plan_identity
            and plan.plan_version == current.plan_version
        )

    def approve_successor(
        self,
        plan: CanonicalPlanGraph | str,
        plan_version: int | None = None,
        plan_digest: str | None = None,
        *,
        approved_by: str = "operator",
    ) -> PlanAdmissionOutcome:
        """Canonically approve exactly one presented successor after exhaustion."""
        cursor = self._cursor()
        if cursor is None:
            return PlanAdmissionOutcome(False, None, "SCOPE_NOT_STARTED", "UNTIL_STOPPED scope has not started")
        if cursor.status == "STOPPED" or cursor.disposition == "STOPPED":
            return PlanAdmissionOutcome(False, None, "STOP_FENCED", "STOP remains authoritative")
        if cursor.status != "WAITING_FOR_PLAN" and cursor.disposition != "WAITING_FOR_PLAN":
            return PlanAdmissionOutcome(False, None, "CURRENT_PLAN_NOT_EXHAUSTED", "current plan is not exhausted")
        current = self._approved_plan()
        if current is None:
            return PlanAdmissionOutcome(False, None, "NO_APPROVED_PLAN", "there is no current approved plan")

        graph: CanonicalPlanGraph | None = plan if isinstance(plan, CanonicalPlanGraph) else None
        if graph is not None:
            try:
                digest = self._validate_plan(graph)
            except UntilStoppedError as exc:
                return PlanAdmissionOutcome(False, None, exc.code, str(exc))
            identity = graph.plan_identity
            version = graph.plan_version
        else:
            identity = str(plan)
            if plan_version is None or plan_digest is None:
                return PlanAdmissionOutcome(False, None, "PLAN_IDENTITY_REQUIRED", "successor identity/version/digest is required")
            version = int(plan_version)
            digest = plan_digest

        candidate = self._admission_for_identity(
            plan_identity=identity,
            plan_version=version,
            plan_digest=digest,
        )
        if candidate is None or candidate.status != "CANDIDATE":
            return PlanAdmissionOutcome(False, None, "SUCCESSOR_NOT_PRESENTED", "only a durable candidate may be approved")
        if (
            candidate.predecessor_plan_identity != current.plan_identity
            or candidate.predecessor_plan_version != current.plan_version
            or candidate.predecessor_plan_digest != current.plan_digest
        ):
            return PlanAdmissionOutcome(False, None, "PREDECESSOR_MISMATCH", "candidate is not linked to current plan")
        if graph is not None and candidate.plan_digest != digest:
            return PlanAdmissionOutcome(False, None, "PLAN_DIGEST_MISMATCH", "candidate bytes differ from its admission")

        # If a graph was not supplied, the immutable graph stored with the
        # candidate is the only allowed source for the approval identity.
        if graph is not None:
            persisted = json.loads(
                self.conn.execute(
                    f"SELECT plan_json FROM {PLAN_ADMISSION_TABLE} WHERE admission_id = ?",
                    (candidate.admission_id,),
                ).fetchone()[0]
            )
            if canonical_json_bytes(persisted).decode("utf-8") != _graph_json(graph):
                return PlanAdmissionOutcome(False, None, "PLAN_BYTES_MISMATCH", "candidate graph differs from durable candidate")

        now = _now_iso()
        old_revision = cursor.state_revision
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            superseded = self.conn.execute(
                f"UPDATE {PLAN_ADMISSION_TABLE} SET status = 'SUPERSEDED' WHERE admission_id = ? AND status = 'APPROVED'",
                (current.admission_id,),
            )
            if superseded.rowcount != 1:
                self.conn.rollback()
                return PlanAdmissionOutcome(False, None, "SUCCESSOR_APPROVAL_RACE", "current approval changed during successor approval")
            updated = self.conn.execute(
                f"""
                UPDATE {PLAN_ADMISSION_TABLE}
                SET status = 'APPROVED', approved_at = ?, approved_by = ?, approval_revision = ?
                WHERE admission_id = ? AND status = 'CANDIDATE'
                """,
                (now, approved_by, old_revision + 1, candidate.admission_id),
            )
            if updated.rowcount != 1:
                self.conn.rollback()
                return PlanAdmissionOutcome(False, None, "SUCCESSOR_APPROVAL_RACE", "candidate approval lost a concurrency race")
            # The admission and cursor transition are both Project Memory v2
            # facts. Cursor CAS keeps them in one transaction and prevents a
            # stale approver from moving authority.
            updated_cursor = replace(
                cursor,
                current_milestone_id=None,
                current_task_id=None,
                last_accepted_task_id=None,
                last_accepted_gate=None,
                plan_identity=candidate.plan_identity,
                plan_version=candidate.plan_version,
                disposition="SUCCESSOR_APPROVED",
                status="ACTIVE",
                stop_requested_at=None,
                stop_reason=None,
            )
            if not self.orchestrator.update_cursor_cas(
                updated_cursor,
                cursor.state_revision,
                commit=False,
            ):
                self.conn.rollback()
                return PlanAdmissionOutcome(False, None, "STALE_CURSOR", "successor approval could not update the canonical cursor")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        approved = self._admission_for_identity(
            plan_identity=candidate.plan_identity,
            plan_version=candidate.plan_version,
            plan_digest=candidate.plan_digest,
        )
        return PlanAdmissionOutcome(True, approved, "SUCCESSOR_APPROVED", "successor plan is now canonical")

    approve_successor_plan = approve_successor

    def _plan_is_exhausted(
        self,
        plan: CanonicalPlanGraph,
        task_statuses: Mapping[str, str],
        milestone_gate_statuses: Mapping[str, str],
    ) -> bool:
        if any(str(task_statuses.get(task.task_id, "NOT_STARTED")).upper() not in _TERMINAL_TASK_STATUSES for task in plan.tasks):
            return False
        for milestone in plan.milestones:
            if any(
                str(task_statuses.get(task_id, "NOT_STARTED")).upper() not in _TERMINAL_TASK_STATUSES
                for task_id in milestone.task_ids
            ):
                return False
            if str(milestone_gate_statuses.get(milestone.milestone_id, "NOT_REACHED")).upper() not in _APPROVED_GATE_STATUSES:
                return False
        return True

    def is_plan_exhausted(
        self,
        plan: CanonicalPlanGraph,
        task_statuses: Mapping[str, str],
        milestone_gate_statuses: Mapping[str, str],
    ) -> bool:
        self._validate_plan(plan)
        return self._plan_is_exhausted(plan, task_statuses, milestone_gate_statuses)

    @staticmethod
    def _task_status(task_statuses: Mapping[str, str], task_id: str) -> str:
        return str(task_statuses.get(task_id, "NOT_STARTED")).upper()

    def _decision(
        self,
        *,
        action: ScopeAction,
        state: CanonicalWorkState,
        reason: str,
        explanation: str,
        task_id: str | None = None,
        milestone_id: str | None = None,
        crosses_task: bool = False,
        crosses_milestone: bool = False,
        terminal: bool = False,
    ) -> ScopeDecision:
        return ScopeDecision(
            action=action,
            canonical_work_state=state,
            selected_task_id=task_id,
            selected_milestone_id=milestone_id,
            reason_code=reason,
            explanation=explanation,
            crosses_task_boundary=crosses_task,
            crosses_milestone_boundary=crosses_milestone,
            is_terminal=terminal,
        )

    def _persist_decision(
        self,
        cursor: ScopeCursor,
        decision: ScopeDecision,
        *,
        plan: PlanAdmissionRecord | None,
        plan_exhausted: bool = False,
    ) -> ScopeCursor:
        selected_milestone = decision.selected_milestone_id or cursor.current_milestone_id
        if decision.action == ScopeAction.HALT_WAITING_FOR_PLAN:
            selected_task = None
            status = "WAITING_FOR_PLAN"
        elif decision.action in {
            ScopeAction.PAUSE_MANUAL_GATE_REQUIRED,
            ScopeAction.PAUSE_POLICY_APPROVAL_REQUIRED,
        }:
            selected_task = decision.selected_task_id or cursor.current_task_id
            status = "PAUSED"
        elif decision.action in {ScopeAction.HALT_BLOCKED}:
            selected_task = decision.selected_task_id or cursor.current_task_id
            status = "BLOCKED"
        elif decision.action in {
            ScopeAction.WAIT_DEPENDENCY_PENDING,
            ScopeAction.WAIT_MILESTONE_GATE_PENDING,
            ScopeAction.WAIT_CI_WAITING,
            ScopeAction.WAIT_TRANSIENT_BACKOFF,
        }:
            selected_task = decision.selected_task_id
            status = "WAITING"
        elif decision.action in {ScopeAction.STOP_PROJECT_COMPLETE, ScopeAction.STOP_SCOPE_COMPLETE}:
            selected_task = None
            status = "COMPLETED"
        elif decision.action == ScopeAction.STOP_EXTERNAL_STOP_REQUESTED:
            selected_task = cursor.current_task_id
            status = "STOPPED"
        else:
            selected_task = decision.selected_task_id or cursor.current_task_id
            status = "ACTIVE"

        explanation = {
            "schema": "bdb-until-stopped-decision-v1",
            "action": decision.action.value,
            "reason_code": decision.reason_code,
            "plan_exhausted": plan_exhausted,
            "approved_plan": plan.to_dict() if plan else None,
        }
        updated = replace(
            cursor,
            current_milestone_id=selected_milestone,
            current_task_id=selected_task,
            plan_identity=plan.plan_identity if plan else cursor.plan_identity,
            plan_version=plan.plan_version if plan else cursor.plan_version,
            disposition=("WAITING_FOR_PLAN" if plan_exhausted else decision.action.value),
            status=status,
            explanation_json=canonical_json_bytes(explanation).decode("utf-8"),
        )
        if all(
            getattr(updated, field) == getattr(cursor, field)
            for field in (
                "current_milestone_id",
                "current_task_id",
                "plan_identity",
                "plan_version",
                "disposition",
                "status",
                "explanation_json",
            )
        ):
            return cursor
        if not self.orchestrator.update_cursor_cas(updated, cursor.state_revision):
            _fail("STALE_CURSOR", "UNTIL_STOPPED decision lost a canonical cursor race")
        refreshed = self._cursor()
        assert refreshed is not None
        return refreshed

    def tick(
        self,
        plan: CanonicalPlanGraph | None = None,
        task_statuses: Mapping[str, str] | None = None,
        milestone_gate_statuses: Mapping[str, str] | None = None,
        *,
        manual_approvals: Mapping[str, bool] | None = None,
        policy_approvals: Mapping[str, bool] | None = None,
        manual_pause: bool = False,
        policy_pause: bool = False,
        real_blocker: bool = False,
        blocker_reason: str = "REAL_BLOCKER",
        stop_requested: bool = False,
    ) -> UntilStoppedResult:
        """Evaluate one deterministic UNTIL_STOPPED transition.

        ``tick`` selects and checkpoints canonical state; it does not launch a
        process, send a message, create a task, or mutate task acceptance.
        """
        statuses = task_statuses or {}
        gates = milestone_gate_statuses or {}
        cursor = self._cursor()
        if cursor is None:
            _fail("SCOPE_NOT_STARTED", "UNTIL_STOPPED scope has not started")
        assert cursor is not None

        if stop_requested and cursor.status != "STOPPED" and cursor.disposition != "STOPPED":
            self.stop(reason="UNTIL_STOPPED STOP requested", expected_epoch=cursor.scope_epoch)
            cursor = self._cursor()
            assert cursor is not None
        if cursor.status == "STOPPED" or cursor.disposition == "STOPPED":
            decision = self._decision(
                action=ScopeAction.STOP_EXTERNAL_STOP_REQUESTED,
                state=CanonicalWorkState.PAUSED,
                reason="STOP_REQUESTED",
                explanation="UNTIL_STOPPED remains fenced after canonical STOP.",
                terminal=True,
            )
            return UntilStoppedResult(decision, "STOPPED", cursor, self._approved_plan())

        if cursor.scope != AutoScope.UNTIL_STOPPED or not cursor.scope_selection_explicit:
            decision = self._decision(
                action=ScopeAction.HALT_BLOCKED,
                state=CanonicalWorkState.BLOCKED,
                reason="EXPLICIT_SCOPE_REQUIRED",
                explanation="UNTIL_STOPPED is never enabled by an implicit or stale cursor.",
                terminal=True,
            )
            updated = self._persist_decision(cursor, decision, plan=None)
            return UntilStoppedResult(decision, "BLOCKED", updated, self._approved_plan())

        approved = self._approved_plan()
        if cursor.status == "WAITING_FOR_PLAN" or cursor.disposition == "WAITING_FOR_PLAN":
            decision = self._decision(
                action=ScopeAction.HALT_WAITING_FOR_PLAN,
                state=CanonicalWorkState.WAITING_FOR_PLAN,
                reason="WAITING_FOR_PLAN",
                explanation="The durable WAITING_FOR_PLAN checkpoint remains fenced until a successor is canonically approved.",
                milestone_id=cursor.current_milestone_id,
                terminal=True,
            )
            return UntilStoppedResult(
                decision,
                "WAITING_FOR_PLAN",
                cursor,
                approved,
                plan_exhausted=True,
            )
        if approved is None:
            decision = self._decision(
                action=ScopeAction.HALT_WAITING_FOR_PLAN,
                state=CanonicalWorkState.WAITING_FOR_PLAN,
                reason="WAITING_FOR_PLAN",
                explanation="No canonically approved plan is available; no task is fabricated.",
                terminal=True,
            )
            updated = self._persist_decision(cursor, decision, plan=None, plan_exhausted=True)
            return UntilStoppedResult(decision, "WAITING_FOR_PLAN", updated, None, plan_exhausted=True)

        if plan is None:
            row = self.conn.execute(
                f"SELECT plan_json FROM {PLAN_ADMISSION_TABLE} WHERE admission_id = ?",
                (approved.admission_id,),
            ).fetchone()
            assert row is not None
            plan = _graph_from_document(json.loads(row["plan_json"]))
        try:
            digest = self._validate_plan(plan)
        except UntilStoppedError as exc:
            decision = self._decision(
                action=ScopeAction.HALT_BLOCKED,
                state=CanonicalWorkState.BLOCKED,
                reason=exc.code,
                explanation="The supplied plan is not structurally/content bound to canonical authority.",
                terminal=True,
            )
            updated = self._persist_decision(cursor, decision, plan=approved)
            return UntilStoppedResult(decision, "BLOCKED", updated, approved, tasks_outside_approved_plan=0)
        if (
            approved.plan_identity != plan.plan_identity
            or approved.plan_version != plan.plan_version
            or approved.plan_digest != digest
            or cursor.plan_identity != approved.plan_identity
            or cursor.plan_version != approved.plan_version
        ):
            decision = self._decision(
                action=ScopeAction.HALT_BLOCKED,
                state=CanonicalWorkState.BLOCKED,
                reason="PLAN_IDENTITY_MISMATCH",
                explanation="Only the exact currently approved plan identity/version/digest may run.",
                terminal=True,
            )
            updated = self._persist_decision(cursor, decision, plan=approved)
            return UntilStoppedResult(decision, "BLOCKED", updated, approved, tasks_outside_approved_plan=0)

        if real_blocker:
            decision = self._decision(
                action=ScopeAction.HALT_BLOCKED,
                state=CanonicalWorkState.BLOCKED,
                reason=blocker_reason,
                explanation="A real blocker stops automatic progression; unrelated work is not selected.",
                terminal=True,
            )
            updated = self._persist_decision(cursor, decision, plan=approved)
            return UntilStoppedResult(decision, "BLOCKED", updated, approved)

        if self._plan_is_exhausted(plan, statuses, gates):
            decision = self._decision(
                action=ScopeAction.HALT_WAITING_FOR_PLAN,
                state=CanonicalWorkState.WAITING_FOR_PLAN,
                reason="WAITING_FOR_PLAN",
                explanation="Approved plan exhausted; waiting for a canonically approved successor.",
                milestone_id=plan.milestones[-1].milestone_id if plan.milestones else None,
                terminal=True,
            )
            updated = self._persist_decision(cursor, decision, plan=approved, plan_exhausted=True)
            return UntilStoppedResult(decision, "WAITING_FOR_PLAN", updated, approved, plan_exhausted=True)

        manual_map = manual_approvals or {}
        policy_map = policy_approvals or {}

        # Traverse the immutable plan in order.  A missing dependency, gate,
        # manual approval, policy approval, or blocker stops before selection
        # can become an external effect.
        for index, milestone in enumerate(plan.milestones):
            if any(
                self._task_status(statuses, task_id) in _BLOCKED_TASK_STATUSES
                for task_id in milestone.task_ids
            ):
                blocked_task = next(
                    task_id
                    for task_id in milestone.task_ids
                    if self._task_status(statuses, task_id) in _BLOCKED_TASK_STATUSES
                )
                decision = self._decision(
                    action=ScopeAction.HALT_BLOCKED,
                    state=CanonicalWorkState.BLOCKED,
                    reason="REAL_BLOCKER",
                    explanation=f"Task {blocked_task} is blocked; UNTIL_STOPPED will not skip to other work.",
                    task_id=blocked_task,
                    milestone_id=milestone.milestone_id,
                    terminal=True,
                )
                updated = self._persist_decision(cursor, decision, plan=approved)
                return UntilStoppedResult(decision, "BLOCKED", updated, approved)

            incomplete = [
                task_id
                for task_id in milestone.task_ids
                if self._task_status(statuses, task_id) not in _TERMINAL_TASK_STATUSES
            ]
            previous_gate_missing = any(
                str(gates.get(previous.milestone_id, "NOT_REACHED")).upper() not in _APPROVED_GATE_STATUSES
                for previous in plan.milestones[:index]
            )
            if previous_gate_missing:
                decision = self._decision(
                    action=ScopeAction.HALT_BLOCKED,
                    state=CanonicalWorkState.BLOCKED,
                    reason="WRONG_MILESTONE_CURSOR",
                    explanation="A prior milestone gate is not accepted; later work is not runnable.",
                    milestone_id=milestone.milestone_id,
                    terminal=True,
                )
                updated = self._persist_decision(cursor, decision, plan=approved)
                return UntilStoppedResult(decision, "BLOCKED", updated, approved)

            if incomplete:
                task_id = incomplete[0]
                task = plan.get_task(task_id)
                if task is None:
                    decision = self._decision(
                        action=ScopeAction.HALT_BLOCKED,
                        state=CanonicalWorkState.BLOCKED,
                        reason="MALFORMED_PLAN",
                        explanation="Plan milestone references a task that is not in the immutable graph.",
                        milestone_id=milestone.milestone_id,
                        terminal=True,
                    )
                    updated = self._persist_decision(cursor, decision, plan=approved)
                    return UntilStoppedResult(decision, "BLOCKED", updated, approved)

                pending_dependencies = [
                    dependency
                    for dependency in task.dependencies
                    if self._task_status(statuses, dependency) not in _TERMINAL_TASK_STATUSES
                ]
                if pending_dependencies:
                    decision = self._decision(
                        action=ScopeAction.WAIT_DEPENDENCY_PENDING,
                        state=CanonicalWorkState.WAITING,
                        reason="DEPENDENCY_PENDING",
                        explanation=f"Task {task_id} waits for dependencies: {', '.join(pending_dependencies)}.",
                        task_id=task_id,
                        milestone_id=milestone.milestone_id,
                        crosses_task=cursor.current_task_id != task_id,
                    )
                    updated = self._persist_decision(cursor, decision, plan=approved)
                    return UntilStoppedResult(decision, "WAITING", updated, approved)
                if manual_pause or (task.requires_manual_approval and not manual_map.get(task_id, False)):
                    decision = self._decision(
                        action=ScopeAction.PAUSE_MANUAL_GATE_REQUIRED,
                        state=CanonicalWorkState.PAUSED,
                        reason="MANUAL_APPROVAL_REQUIRED",
                        explanation=f"Task {task_id} requires manual approval before effect.",
                        task_id=task_id,
                        milestone_id=milestone.milestone_id,
                    )
                    updated = self._persist_decision(cursor, decision, plan=approved)
                    return UntilStoppedResult(decision, "PAUSED", updated, approved)
                if policy_pause or (task.requires_policy_approval and not policy_map.get(task_id, False)):
                    decision = self._decision(
                        action=ScopeAction.PAUSE_POLICY_APPROVAL_REQUIRED,
                        state=CanonicalWorkState.PAUSED,
                        reason="POLICY_APPROVAL_REQUIRED",
                        explanation=f"Task {task_id} requires policy approval before effect.",
                        task_id=task_id,
                        milestone_id=milestone.milestone_id,
                    )
                    updated = self._persist_decision(cursor, decision, plan=approved)
                    return UntilStoppedResult(decision, "PAUSED", updated, approved)
                decision = self._decision(
                    action=ScopeAction.LAUNCH_TASK,
                    state=CanonicalWorkState.RUNNABLE,
                    reason="APPROVED_PLAN_TASK",
                    explanation=f"Task {task_id} is the next legal task in the approved plan.",
                    task_id=task_id,
                    milestone_id=milestone.milestone_id,
                    crosses_task=cursor.current_task_id != task_id,
                    crosses_milestone=cursor.current_milestone_id not in (None, milestone.milestone_id),
                )
                updated = self._persist_decision(cursor, decision, plan=approved)
                return UntilStoppedResult(decision, "ACTIVE", updated, approved)

            gate_status = str(gates.get(milestone.milestone_id, "NOT_REACHED")).upper()
            if gate_status not in _APPROVED_GATE_STATUSES:
                if gate_status == "FAILED":
                    decision = self._decision(
                        action=ScopeAction.HALT_BLOCKED,
                        state=CanonicalWorkState.BLOCKED,
                        reason="MILESTONE_GATE_FAILED",
                        explanation=f"Milestone gate {milestone.gate_id} failed; no later work is selected.",
                        milestone_id=milestone.milestone_id,
                        terminal=True,
                    )
                    updated = self._persist_decision(cursor, decision, plan=approved)
                    return UntilStoppedResult(decision, "BLOCKED", updated, approved)
                decision = self._decision(
                    action=ScopeAction.WAIT_MILESTONE_GATE_PENDING,
                    state=CanonicalWorkState.WAITING,
                    reason="MILESTONE_GATE_PENDING",
                    explanation=f"Milestone gate {milestone.gate_id} must be accepted before continuing.",
                    milestone_id=milestone.milestone_id,
                )
                updated = self._persist_decision(cursor, decision, plan=approved)
                return UntilStoppedResult(decision, "WAITING", updated, approved)

        # The only path here is a structurally empty plan with no work.  It is
        # still plan exhaustion, never a synthetic task or generic BLOCKED.
        decision = self._decision(
            action=ScopeAction.HALT_WAITING_FOR_PLAN,
            state=CanonicalWorkState.WAITING_FOR_PLAN,
            reason="WAITING_FOR_PLAN",
            explanation="No approved runnable work remains; waiting for a successor plan.",
            terminal=True,
        )
        updated = self._persist_decision(cursor, decision, plan=approved, plan_exhausted=True)
        return UntilStoppedResult(decision, "WAITING_FOR_PLAN", updated, approved, plan_exhausted=True)

    evaluate = tick
    next_action = tick

    def stop(
        self,
        *,
        expected_epoch: int | None = None,
        reason: str = "External STOP requested",
        actor_class: str = "operator",
    ) -> Any:
        return self.orchestrator.request_stop(
            expected_epoch=expected_epoch,
            reason=reason,
            actor_class=actor_class,
        )

    request_stop = stop

    def resume(
        self,
        *,
        expected_prior_epoch: int | None = None,
        new_run_id: str | None = None,
        actor_class: str = "operator",
    ) -> Any:
        return self.orchestrator.resume_scope(
            expected_prior_epoch=expected_prior_epoch,
            new_run_id=new_run_id,
            actor_class=actor_class,
        )

    resume_scope = resume

    def can_launch(self, *, epoch: int | None = None) -> bool:
        """Check the existing NX-022 fence without producing an effect."""
        cursor = self._cursor()
        if cursor is None:
            return False
        result = EffectBoundaryGuard.check(
            self.conn,
            self.project_id,
            epoch if epoch is not None else cursor.scope_epoch,
            EffectBoundary.ORCHESTRATOR_TICK_LAUNCH,
            raise_on_violation=False,
        )
        return result.allowed

    def state_snapshot(self) -> dict[str, Any]:
        """Return a bounded read projection of canonical UNTIL_STOPPED state."""
        cursor = self._cursor()
        cursor_document = asdict(cursor) if cursor is not None else None
        if cursor_document is not None:
            cursor_document["scope"] = cursor.scope.value
        admissions = [
            _admission_from_row(row).to_dict()
            for row in self.conn.execute(
                f"SELECT * FROM {PLAN_ADMISSION_TABLE} WHERE project_id = ? ORDER BY created_at, admission_id",
                (self.project_id,),
            ).fetchall()
        ]
        return {
            "schema": "bdb-until-stopped-state-v1",
            "project_id": self.project_id,
            "cursor": cursor_document,
            "plan_admissions": admissions,
            "until_stopped_implicitly_enabled": UNTIL_STOPPED_IMPLICITLY_ENABLED,
            "second_workflow_authority_created": False,
        }


UntilStoppedOrchestrator = UntilStoppedController
UntilStoppedCoordinator = UntilStoppedController


__all__ = [
    "PLAN_ADMISSION_SCHEMA",
    "PLAN_ADMISSION_STATUSES",
    "PLAN_ADMISSION_TABLE",
    "PlanAdmissionOutcome",
    "PlanAdmissionRecord",
    "UNTIL_STOPPED_CONTRACT_VERSION_EXPLICIT",
    "UNTIL_STOPPED_IMPLICITLY_ENABLED",
    "UNTIL_STOPPED_PLAN_ADMISSIONS_DDL",
    "UNTIL_STOPPED_SCHEMA_VERSION",
    "UntilStoppedController",
    "UntilStoppedCoordinator",
    "UntilStoppedError",
    "UntilStoppedOrchestrator",
    "UntilStoppedResult",
    "plan_graph_digest",
]

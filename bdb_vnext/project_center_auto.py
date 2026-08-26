"""Canonical AUTO projection and command boundary for Project Center.

The Project Center is a projection of canonical state.  This module keeps the
UI-facing selection separate from the durable scope cursor and exposes only
bounded commands to the canonical Project Memory v2 authority.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .auto_scope_contract import AutoScope, DEFAULT_AUTO_SCOPE
from .project_memory_v2_store import ProjectMemoryStoreV2, ProjectMemoryV2Error
from .scope_orchestrator import (
    CanonicalPlanGraph,
    PlanMilestoneNode,
    PlanTaskNode,
    ScopeOrchestrator,
)
from .stop_fence import execute_resume_transaction, execute_stop_transaction


PROJECT_CENTER_AUTO_UI_VERSION = "1.0.0"
AUTO_SCOPE_OPTIONS: tuple[AutoScope, ...] = (
    AutoScope.TASK,
    AutoScope.MILESTONE,
    AutoScope.PROJECT,
    AutoScope.UNTIL_STOPPED,
)

AUTO_STATUS_REASON_TEXT: dict[str, str] = {
    "READY": "AUTO jest gotowe do uruchomienia po potwierdzeniu wybranego scope.",
    "WAITING": "AUTO czeka na zakończenie trwałego oczekiwania.",
    "WAITING_FOR_PLAN": "Brak zatwierdzonego planu lub plan został wyczerpany.",
    "PAUSED": "AUTO jest wstrzymane do czasu rozstrzygnięcia checkpointu.",
    "BLOCKED": "AUTO jest zablokowane przez kanoniczny blocker.",
    "STOPPED": "AUTO jest zatrzymane przez kanoniczny STOP fence.",
    "CI_WAITING": "AUTO czeka na wynik CI zapisany w stanie kanonicznym.",
    "DELIVERY_UNCERTAIN": "Dostarczenie jest niepewne; wymagane jest kanoniczne uzgodnienie.",
    "OPERATOR_CHECKPOINT": "Wymagana jest decyzja operatora w kanonicznym checkpointcie.",
    "COMPLETED": "AUTO zakończyło dozwolony zakres.",
    "PROJECT_NOT_SELECTED": "Wybierz projekt, aby odczytać kanoniczny stan AUTO.",
    "AUTO_START_AVAILABLE": "Kanoniczny plan jest dostępny; start wymaga jawnego potwierdzenia.",
}


@dataclass(frozen=True)
class AutoControlSpec:
    """Accessibility contract for controls rendered by Project Center."""

    control_id: str
    accessible_name: str
    keyboard_focusable: bool = True
    exposes_disabled_reason: bool = True


AUTO_UI_CONTROL_CONTRACT: tuple[AutoControlSpec, ...] = (
    AutoControlSpec("scope_selector", "AUTO scope"),
    AutoControlSpec("start", "Uruchom AUTO"),
    AutoControlSpec("stop", "STOP"),
    AutoControlSpec("continue", "Kontynuuj"),
    AutoControlSpec("resume", "Wznów"),
)


@dataclass(frozen=True)
class CanonicalAutoState:
    """Read-only canonical state used to build the Project Center projection."""

    project_id: str = ""
    scope: AutoScope = DEFAULT_AUTO_SCOPE
    scope_epoch: int = 0
    run_id: str | None = None
    current_milestone_id: str | None = None
    current_task_id: str | None = None
    scope_status: str = "WAITING_FOR_PLAN"
    continuation_status: str = "NONE"
    reentry_status: str = "NONE"
    reason_code: str = "WAITING_FOR_PLAN"
    reason: str = AUTO_STATUS_REASON_TEXT["WAITING_FOR_PLAN"]
    plan_available: bool = False
    plan_version: int | None = None
    canonical_revision: int = 0
    stop_fenced: bool = False
    p2_completed: bool = False
    p3_started: bool = False
    authority: str = "ProjectMemoryStoreV2"

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", AutoScope(self.scope))
        if self.scope_epoch < 0 or self.canonical_revision < 0:
            raise ValueError("canonical AUTO counters must be non-negative")
        if not self.reason:
            object.__setattr__(
                self,
                "reason",
                AUTO_STATUS_REASON_TEXT.get(self.scope_status, self.scope_status),
            )

    @property
    def premium_p2_completed(self) -> bool:
        return self.p2_completed

    @property
    def premium_p3_started(self) -> bool:
        return self.p3_started

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "scope": self.scope.value,
            "scope_epoch": self.scope_epoch,
            "run_id": self.run_id,
            "current_milestone_id": self.current_milestone_id,
            "current_task_id": self.current_task_id,
            "scope_status": self.scope_status,
            "continuation_status": self.continuation_status,
            "reentry_status": self.reentry_status,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "plan_available": self.plan_available,
            "plan_version": self.plan_version,
            "canonical_revision": self.canonical_revision,
            "stop_fenced": self.stop_fenced,
            "p2_completed": self.p2_completed,
            "p3_started": self.p3_started,
            "authority": self.authority,
        }


@dataclass(frozen=True)
class ProjectCenterAutoViewModel:
    """Deterministic GUI projection; ``selected_scope`` is UI-only state."""

    canonical: CanonicalAutoState
    selected_scope: AutoScope = DEFAULT_AUTO_SCOPE
    scope_options: tuple[AutoScope, ...] = AUTO_SCOPE_OPTIONS

    @classmethod
    def from_canonical(
        cls,
        canonical: CanonicalAutoState,
        *,
        selected_scope: AutoScope | str | None = None,
        browser_local_state: Mapping[str, Any] | None = None,
    ) -> "ProjectCenterAutoViewModel":
        # Browser/local state is intentionally accepted only as an ignored
        # observation.  It cannot select a scope or change a command.
        del browser_local_state
        selected = AutoScope(selected_scope) if selected_scope is not None else DEFAULT_AUTO_SCOPE
        return cls(canonical=canonical, selected_scope=selected)

    def select_scope(self, scope: AutoScope | str) -> "ProjectCenterAutoViewModel":
        """Return a new projection without mutating canonical authority."""
        return ProjectCenterAutoViewModel.from_canonical(self.canonical, selected_scope=scope)

    @property
    def current_milestone(self) -> str:
        return self.canonical.current_milestone_id or "—"

    @property
    def current_task(self) -> str:
        return self.canonical.current_task_id or "—"

    @property
    def scope_status(self) -> str:
        return self.canonical.scope_status

    @property
    def continuation_status(self) -> str:
        return self.canonical.continuation_status

    @property
    def reentry_status(self) -> str:
        return self.canonical.reentry_status

    @property
    def blocker_reason(self) -> str:
        return self.canonical.reason or AUTO_STATUS_REASON_TEXT.get(self.scope_status, self.scope_status)

    @property
    def selected_scope_is_pending(self) -> bool:
        return self.selected_scope != self.canonical.scope

    @property
    def can_start(self) -> bool:
        return bool(
            self.canonical.plan_available
            and self.scope_status in {"READY", "AUTO_START_AVAILABLE"}
            and not self.canonical.stop_fenced
        )

    @property
    def can_stop(self) -> bool:
        return self.scope_status in {
            "ACTIVE",
            "RUNNABLE",
            "WAITING",
            "PAUSED",
            "CI_WAITING",
            "DELIVERY_UNCERTAIN",
            "OPERATOR_CHECKPOINT",
        } and not self.canonical.stop_fenced

    @property
    def can_continue(self) -> bool:
        return self.scope_status in {
            "ACTIVE",
            "RUNNABLE",
            "WAITING",
            "PAUSED",
            "CI_WAITING",
            "DELIVERY_UNCERTAIN",
            "OPERATOR_CHECKPOINT",
        } and not self.canonical.stop_fenced

    @property
    def can_resume(self) -> bool:
        return self.scope_status in {
            "STOPPED",
            "PAUSED",
            "DELIVERY_UNCERTAIN",
            "OPERATOR_CHECKPOINT",
        } or self.reentry_status in {"PENDING", "OPERATOR_CHECKPOINT", "DELIVERY_UNCERTAIN"}

    def disabled_reason(self, action: str) -> str:
        enabled = {
            "start": self.can_start,
            "stop": self.can_stop,
            "continue": self.can_continue,
            "resume": self.can_resume,
        }.get(action)
        if enabled is None:
            raise ValueError(f"unknown AUTO action: {action}")
        if enabled:
            return ""
        return AUTO_STATUS_REASON_TEXT.get(self.scope_status, self.blocker_reason)

    def start_intent(self) -> dict[str, Any]:
        return {
            "command": "START_AUTO",
            "project_id": self.canonical.project_id,
            "scope": self.selected_scope.value,
            "explicit_confirmation_required": True,
        }

    def continue_intent(self) -> dict[str, Any]:
        # The orchestrator selects the next task/milestone.  The GUI sends no
        # task or milestone suggestion.
        return {"command": "CONTINUE_AUTO", "project_id": self.canonical.project_id}

    def resume_intent(self) -> dict[str, Any]:
        return {"command": "RESUME_AUTO", "project_id": self.canonical.project_id}

    def stop_intent(self) -> dict[str, Any]:
        return {"command": "STOP_AUTO", "project_id": self.canonical.project_id}


class ProjectCenterAutoCommandError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AutoCommandReceipt:
    command: str
    project_id: str
    accepted: bool
    reason_code: str
    explanation: str
    scope: AutoScope | None = None
    scope_epoch: int | None = None
    current_milestone_id: str | None = None
    current_task_id: str | None = None
    idempotent: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "project_id": self.project_id,
            "accepted": self.accepted,
            "reason_code": self.reason_code,
            "explanation": self.explanation,
            "scope": self.scope.value if self.scope else None,
            "scope_epoch": self.scope_epoch,
            "current_milestone_id": self.current_milestone_id,
            "current_task_id": self.current_task_id,
            "idempotent": self.idempotent,
        }


class ProjectCenterAutoCommands(Protocol):
    def snapshot(self, *, plan_available: bool = False, plan_version: str | None = None) -> CanonicalAutoState:
        ...

    def start_auto(self, scope: AutoScope, *, confirmed: bool) -> AutoCommandReceipt:
        ...

    def continue_auto(self) -> AutoCommandReceipt:
        ...

    def resume_auto(self) -> AutoCommandReceipt:
        ...

    def stop_auto(self) -> AutoCommandReceipt:
        ...


class CanonicalProjectCenterAutoCommands:
    """Adapter that routes Project Center commands to Project Memory v2."""

    STORAGE_DATABASE = "control/project-memory-v2/<project_id>.db"
    SCHEMA_OWNER = "ProjectMemoryStoreV2"
    TRANSACTION_AUTHORITY = "ProjectMemoryStoreV2._transaction"
    STOP_COMMAND_AUTHORITY = "ProjectMemoryStoreV2.request_stop"
    RESUME_COMMAND_AUTHORITY = "ProjectMemoryStoreV2.resume_scope"
    SECOND_AUTHORITY_CREATED = False

    def __init__(
        self,
        runtime_root: str | Path,
        project_id: str,
        *,
        project_provider: Callable[[], Any] | None = None,
        plan_provider: Callable[[], Any | None] | None = None,
    ) -> None:
        self.runtime_root = Path(runtime_root).expanduser().absolute()
        self.project_id = project_id
        self._project_provider = project_provider
        self._plan_provider = plan_provider

    @property
    def db_path(self) -> Path:
        return self.runtime_root / "control" / "project-memory-v2" / f"{self.project_id}.db"

    def _plan(self) -> Any | None:
        return self._plan_provider() if self._plan_provider is not None else None

    def snapshot(
        self,
        *,
        plan_available: bool = False,
        plan_version: str | None = None,
    ) -> CanonicalAutoState:
        """Read canonical state without creating a database or a cursor."""
        db_path = self.db_path
        if not db_path.is_file():
            status = "AUTO_START_AVAILABLE" if plan_available else "WAITING_FOR_PLAN"
            reason_code = "AUTO_START_AVAILABLE" if plan_available else "WAITING_FOR_PLAN"
            return CanonicalAutoState(
                project_id=self.project_id,
                scope=DEFAULT_AUTO_SCOPE,
                scope_status=status,
                reason_code=reason_code,
                reason=AUTO_STATUS_REASON_TEXT[reason_code],
                plan_available=plan_available,
                plan_version=int(plan_version) if plan_version and str(plan_version).isdigit() else None,
            )

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            project_row = (
                conn.execute(
                    "SELECT revision FROM projects WHERE project_id = ?",
                    (self.project_id,),
                ).fetchone()
                if "projects" in tables
                else None
            )
            cursor = (
                conn.execute(
                    "SELECT * FROM scope_cursors WHERE project_id = ?",
                    (self.project_id,),
                ).fetchone()
                if "scope_cursors" in tables
                else None
            )
            if cursor is None:
                status = "AUTO_START_AVAILABLE" if plan_available else "WAITING_FOR_PLAN"
                code = "AUTO_START_AVAILABLE" if plan_available else "WAITING_FOR_PLAN"
                return CanonicalAutoState(
                    project_id=self.project_id,
                    scope=DEFAULT_AUTO_SCOPE,
                    scope_status=status,
                    reason_code=code,
                    reason=AUTO_STATUS_REASON_TEXT[code],
                    plan_available=plan_available,
                    plan_version=int(plan_version) if plan_version and str(plan_version).isdigit() else None,
                    canonical_revision=int(project_row[0]) if project_row else 0,
                )

            scope = AutoScope(cursor["scope"])
            raw_status = str(cursor["status"] if "status" in cursor.keys() else "ACTIVE")
            disposition = str(cursor["disposition"] or "ACTIVE")
            task_status = None
            if "task_execution_states" in tables and cursor["current_task_id"]:
                task_row = conn.execute(
                    "SELECT status FROM task_execution_states WHERE project_id = ? AND task_id = ?",
                    (self.project_id, cursor["current_task_id"]),
                ).fetchone()
                task_status = str(task_row[0]).upper() if task_row else None

            send_status = None
            if "send_intents" in tables:
                row = conn.execute(
                    "SELECT status FROM send_intents WHERE project_id = ? ORDER BY updated_at DESC LIMIT 1",
                    (self.project_id,),
                ).fetchone()
                send_status = str(row[0]) if row else None

            reentry_status = "NONE"
            if "session_reentries" in tables:
                row = conn.execute(
                    "SELECT liveness_state FROM session_reentries WHERE project_id = ? ORDER BY updated_at DESC LIMIT 1",
                    (self.project_id,),
                ).fetchone()
                reentry_status = str(row[0]) if row else "NONE"

            stop_fenced = raw_status == "STOPPED" or disposition == "STOPPED"
            if stop_fenced:
                status = "STOPPED"
                reason_code = "STOPPED"
            elif send_status == "UNCERTAIN":
                status = "DELIVERY_UNCERTAIN"
                reason_code = "DELIVERY_UNCERTAIN"
            elif reentry_status == "OPERATOR_CHECKPOINT":
                status = "OPERATOR_CHECKPOINT"
                reason_code = "OPERATOR_CHECKPOINT"
            elif disposition == "WAITING_FOR_PLAN":
                status = "WAITING_FOR_PLAN"
                reason_code = "WAITING_FOR_PLAN"
            elif task_status == "BLOCKED" or disposition == "BLOCKED":
                status = "BLOCKED"
                reason_code = "BLOCKED"
            elif disposition in {"PAUSED", "PAUSE_MANUAL_GATE_REQUIRED", "PAUSE_POLICY_APPROVAL_REQUIRED"}:
                status = "PAUSED"
                reason_code = "PAUSED"
            elif disposition in {"WAIT_CI_WAITING", "CI_WAITING"}:
                status = "CI_WAITING"
                reason_code = "CI_WAITING"
            elif disposition in {"WAITING", "WAIT_DEPENDENCY_PENDING", "WAIT_MILESTONE_GATE_PENDING"}:
                status = "WAITING"
                reason_code = "WAITING"
            elif disposition in {"COMPLETED", "STOP_SCOPE_COMPLETE", "STOP_PROJECT_COMPLETE"}:
                status = "COMPLETED"
                reason_code = "COMPLETED"
            else:
                status = "ACTIVE"
                reason_code = "ACTIVE"

            continuation_status = send_status or "NONE"
            reason = AUTO_STATUS_REASON_TEXT.get(reason_code, f"Kanoniczny status: {reason_code}.")
            return CanonicalAutoState(
                project_id=self.project_id,
                scope=scope,
                scope_epoch=int(cursor["scope_epoch"]),
                run_id=cursor["run_id"],
                current_milestone_id=cursor["current_milestone_id"],
                current_task_id=cursor["current_task_id"],
                scope_status=status,
                continuation_status=continuation_status,
                reentry_status=reentry_status,
                reason_code=reason_code,
                reason=reason,
                plan_available=plan_available,
                plan_version=int(cursor["plan_version"] or 0) or None,
                canonical_revision=int(cursor["state_revision"] or (project_row[0] if project_row else 0)),
                stop_fenced=stop_fenced,
            )
        finally:
            conn.close()

    def _store_for_write(self) -> ProjectMemoryStoreV2:
        store = ProjectMemoryStoreV2(self.runtime_root, self.project_id)
        project = self._project_provider() if self._project_provider is not None else None
        if project is None:
            raise ProjectCenterAutoCommandError("project_not_available", "canonical project record is unavailable")
        brief = project.brief.to_dict() if hasattr(project.brief, "to_dict") else dict(project.brief)
        store.ensure_project(
            project.display_name,
            project.repo_alias,
            project.local_repo_path,
            brief,
        )
        plan = self._plan()
        if plan is None:
            raise ProjectCenterAutoCommandError("waiting_for_plan", "no canonical project plan is available")
        try:
            store.ensure_initial_plan(plan)
        except ProjectMemoryV2Error as exc:
            if exc.code != "plan_already_exists":
                raise ProjectCenterAutoCommandError(exc.code, str(exc)) from exc
        return store

    @staticmethod
    def _plan_graph(plan: Any) -> CanonicalPlanGraph:
        milestones = tuple(
            PlanMilestoneNode(
                milestone_id=item.milestone_id,
                gate_id=f"GATE:{item.milestone_id}",
                task_ids=tuple(task.task_id for task in plan.tasks if task.milestone_id == item.milestone_id),
            )
            for item in plan.milestones
        )
        tasks = tuple(
            PlanTaskNode(
                task_id=item.task_id,
                milestone_id=item.milestone_id,
                dependencies=tuple(item.dependencies),
            )
            for item in plan.tasks
        )
        return CanonicalPlanGraph(
            plan_identity=f"{plan.project_id}:plan:v{plan.plan_version}",
            plan_version=int(str(plan.plan_version).split(".", 1)[0]),
            milestones=milestones,
            tasks=tasks,
        )

    @staticmethod
    def _plan_statuses(plan: Any) -> dict[str, str]:
        mapping = {
            "completed": "ACCEPTED",
            "skipped": "ACCEPTED",
            "active": "IN_PROGRESS",
            "review": "IN_PROGRESS",
            "blocked": "BLOCKED",
            "pending": "NOT_STARTED",
        }
        return {item.task_id: mapping.get(item.status, "NOT_STARTED") for item in plan.tasks}

    def _receipt_from_state(
        self,
        command: str,
        state: CanonicalAutoState,
        *,
        reason_code: str | None = None,
        explanation: str | None = None,
        idempotent: bool = False,
    ) -> AutoCommandReceipt:
        return AutoCommandReceipt(
            command=command,
            project_id=self.project_id,
            accepted=True,
            reason_code=reason_code or state.reason_code,
            explanation=explanation or state.reason,
            scope=state.scope,
            scope_epoch=state.scope_epoch,
            current_milestone_id=state.current_milestone_id,
            current_task_id=state.current_task_id,
            idempotent=idempotent,
        )

    def start_auto(self, scope: AutoScope, *, confirmed: bool) -> AutoCommandReceipt:
        if not confirmed:
            raise ProjectCenterAutoCommandError(
                "explicit_confirmation_required",
                "AUTO start requires explicit confirmation of the selected scope",
            )
        try:
            requested_scope = AutoScope(scope)
        except ValueError as exc:
            raise ProjectCenterAutoCommandError("invalid_scope", "AUTO scope is invalid") from exc

        plan = self._plan()
        if plan is None:
            raise ProjectCenterAutoCommandError("waiting_for_plan", "no canonical project plan is available")
        store = self._store_for_write()
        with store._transaction() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM scope_cursors WHERE project_id = ?",
                (self.project_id,),
            ).fetchone()
            if row is not None:
                status = str(row["status"] if "status" in row.keys() else "ACTIVE")
                explicit = bool(row["scope_selection_explicit"] if "scope_selection_explicit" in row.keys() else False)
                if not explicit:
                    raise ProjectCenterAutoCommandError(
                        "stale_scope_requires_explicit_start",
                        "an old cursor cannot silently authorize a new GUI scope",
                    )
                if status == "STOPPED" or row["disposition"] == "STOPPED":
                    raise ProjectCenterAutoCommandError(
                        "stopped_scope_requires_resume",
                        "the canonical STOP fence must be resumed with Wznów",
                    )
                if AutoScope(row["scope"]) != requested_scope:
                    raise ProjectCenterAutoCommandError(
                        "active_scope_cannot_change",
                        "an active canonical scope cannot be replaced by GUI selection",
                    )
                state = self.snapshot(plan_available=True, plan_version=str(plan.plan_version))
                return self._receipt_from_state(
                    "START_AUTO",
                    state,
                    reason_code="ALREADY_ACTIVE",
                    explanation="The requested canonical AUTO scope is already active.",
                    idempotent=True,
                )

            orchestrator = ScopeOrchestrator(conn, self.project_id)
            cursor = orchestrator.get_or_create_cursor(
                run_id=f"run:project-center:{uuid.uuid4().hex}",
                scope=requested_scope,
                plan_identity=f"{plan.project_id}:plan:v{plan.plan_version}",
                plan_version=int(str(plan.plan_version).split(".", 1)[0]),
                scope_selection_explicit=True,
            )
            state = CanonicalAutoState(
                project_id=self.project_id,
                scope=cursor.scope,
                scope_epoch=cursor.scope_epoch,
                run_id=cursor.run_id,
                scope_status="ACTIVE",
                continuation_status="NONE",
                reentry_status="NONE",
                reason_code="AUTO_STARTED",
                reason=f"AUTO uruchomione jawnie w scope {cursor.scope.value}.",
                plan_available=True,
                plan_version=int(str(plan.plan_version).split(".", 1)[0]),
                canonical_revision=cursor.state_revision,
            )
            return self._receipt_from_state("START_AUTO", state, reason_code="AUTO_STARTED")

    def continue_auto(self) -> AutoCommandReceipt:
        plan = self._plan()
        if plan is None:
            raise ProjectCenterAutoCommandError("waiting_for_plan", "no canonical project plan is available")
        if not self.db_path.is_file():
            raise ProjectCenterAutoCommandError("scope_not_started", "AUTO has not been started canonically")
        store = ProjectMemoryStoreV2(self.runtime_root, self.project_id)
        store.initialize()
        with store._transaction() as conn:
            conn.row_factory = sqlite3.Row
            orchestrator = ScopeOrchestrator(conn, self.project_id)
            row = conn.execute(
                "SELECT * FROM scope_cursors WHERE project_id = ?",
                (self.project_id,),
            ).fetchone()
            if row is None:
                raise ProjectCenterAutoCommandError("scope_not_started", "AUTO has not been started canonically")
            cursor = orchestrator.get_or_create_cursor(
                run_id=row["run_id"],
                scope=AutoScope(row["scope"]),
                plan_identity=row["plan_identity"],
                plan_version=int(row["plan_version"]),
                scope_selection_explicit=bool(row["scope_selection_explicit"]),
            )
            statuses = self._plan_statuses(plan)
            if "task_execution_states" in {
                item[0] for item in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }:
                for task_id, raw in conn.execute(
                    "SELECT task_id, status FROM task_execution_states WHERE project_id = ?",
                    (self.project_id,),
                ).fetchall():
                    statuses[str(task_id)] = {
                        "completed": "ACCEPTED",
                        "skipped": "ACCEPTED",
                        "active": "IN_PROGRESS",
                        "review": "IN_PROGRESS",
                        "blocked": "BLOCKED",
                    }.get(str(raw), str(raw).upper())
            gates = {f"GATE:{item.milestone_id}": "NOT_REACHED" for item in plan.milestones}
            decision, explanation, updated = orchestrator.tick(
                self._plan_graph(plan),
                cursor,
                statuses,
                gates,
                ui_suggested_task=None,
                prompt_suggested_task=None,
            )
            if not orchestrator.update_cursor_cas(updated, cursor.state_revision):
                raise ProjectCenterAutoCommandError("stale_cursor", "canonical AUTO continuation lost a cursor race")
            state = self.snapshot(plan_available=True, plan_version=str(plan.plan_version))
            return self._receipt_from_state(
                "CONTINUE_AUTO",
                state,
                reason_code=decision.reason_code,
                explanation=explanation.explanation,
            )

    def resume_auto(self) -> AutoCommandReceipt:
        if not self.db_path.is_file():
            raise ProjectCenterAutoCommandError("scope_not_started", "there is no canonical scope to resume")
        store = ProjectMemoryStoreV2(self.runtime_root, self.project_id)
        state_before = self.snapshot(plan_available=self._plan() is not None)
        try:
            with store._transaction() as conn:
                conn.row_factory = sqlite3.Row
                execute_resume_transaction(
                    conn,
                    self.project_id,
                    expected_prior_epoch=state_before.scope_epoch,
                    actor_class="project_center",
                )
        except Exception as exc:
            code = getattr(exc, "code", "resume_rejected")
            raise ProjectCenterAutoCommandError(code, str(exc)) from exc
        state = self.snapshot(plan_available=self._plan() is not None)
        return self._receipt_from_state(
            "RESUME_AUTO",
            state,
            reason_code="AUTO_RESUMED",
            explanation="AUTO wznowione z kanonicznego Project Memory v2.",
        )

    def stop_auto(self) -> AutoCommandReceipt:
        if not self.db_path.is_file():
            raise ProjectCenterAutoCommandError("scope_not_started", "there is no canonical scope to stop")
        state_before = self.snapshot(plan_available=self._plan() is not None)
        store = ProjectMemoryStoreV2(self.runtime_root, self.project_id)
        try:
            with store._transaction() as conn:
                conn.row_factory = sqlite3.Row
                execute_stop_transaction(
                    conn,
                    self.project_id,
                    expected_epoch=state_before.scope_epoch,
                    reason="Project Center STOP",
                    actor_class="project_center",
                )
        except Exception as exc:
            code = getattr(exc, "code", "stop_rejected")
            raise ProjectCenterAutoCommandError(code, str(exc)) from exc
        state = self.snapshot(plan_available=self._plan() is not None)
        return self._receipt_from_state(
            "STOP_AUTO",
            state,
            reason_code="STOPPED",
            explanation="STOP zapisany przez kanoniczny NX-022 STOP fence.",
        )


__all__ = [
    "AUTO_SCOPE_OPTIONS",
    "AUTO_STATUS_REASON_TEXT",
    "AUTO_UI_CONTROL_CONTRACT",
    "AutoCommandReceipt",
    "AutoControlSpec",
    "CanonicalAutoState",
    "CanonicalProjectCenterAutoCommands",
    "PROJECT_CENTER_AUTO_UI_VERSION",
    "ProjectCenterAutoCommandError",
    "ProjectCenterAutoCommands",
    "ProjectCenterAutoViewModel",
]

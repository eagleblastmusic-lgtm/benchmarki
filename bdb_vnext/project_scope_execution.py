"""NX-023: PROJECT Scope Cross-Milestone Execution.

Allows explicitly authorized AUTO execution to progress through milestone
boundaries only after accepted prior gates:
- Explicit per-run PROJECT scope authorization (default remains MILESTONE).
- Immutable canonical ProjectRunIdentity bound to start HEAD and tree.
- Prior milestone final gate must be ACCEPTED under the current run before next milestone can start.
- Boundary transitions verify exact expected source HEAD (stale HEAD fails closed).
- Manual and policy gates pause execution before creating effects or bindings.
- Gate failures stop progression without marking prior milestones complete.
- Wrong-run gate evidence strictly rejected.
- Full integration with NX-022 STOP fence and scope epochs.
- Project completion after final milestone gate without synthetic work fabrication.
- Minimum bounded GUI/API command boundary validating intents through canonical workflow.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from bdb_vnext.auto_scope_contract import (
    AutoScope,
    CanonicalWorkState,
    DEFAULT_AUTO_SCOPE,
    ScopeAction,
    ScopeDecision,
    ScopeInputSnapshot,
    evaluate_scope_transition,
)
from bdb_vnext.scope_orchestrator import (
    CanonicalPlanGraph,
    PlanMilestoneNode,
    PlanTaskNode,
    ScopeCursor,
    ScopeOrchestrator,
)
from bdb_vnext.stop_fence import (
    EffectBoundary,
    EffectBoundaryGuard,
    StopFenceViolationError,
)

PROJECT_SCOPE_SCHEMA_VERSION = "1.0.0"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class ProjectScopeExecutionError(RuntimeError):
    """Raised when cross-milestone transition constraints are violated."""
    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


@dataclass(frozen=True)
class ProjectRunIdentity:
    """Immutable canonical run identity bound to exact source state (Section 20)."""
    project_id: str
    run_id: str
    scope: AutoScope
    plan_identity: str
    plan_version: int
    start_head: str
    start_tree: str
    scope_epoch: int
    cursor_id: str
    created_revision: int
    created_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CrossMilestoneTransitionResult:
    """Result of cross-milestone boundary transition."""
    success: bool
    from_milestone_id: str
    to_milestone_id: str | None
    next_task_id: str | None
    decision: ScopeDecision
    bound_head_verified: bool
    prior_gate_accepted: bool
    reason_code: str
    explanation: str


class ProjectScopeCoordinator:
    """Coordinates PROJECT scope cross-milestone execution."""

    PROJECT_SCOPE_IMPLICITLY_ENABLED: bool = False
    GUI_API_CAN_BYPASS_PROJECT_SCOPE_POLICY: bool = False
    UI_BECOMES_WORKFLOW_AUTHORITY: bool = False

    def __init__(self, conn: sqlite3.Connection, project_id: str, repo_root: Path | None = None) -> None:
        self.conn = conn
        self.project_id = project_id
        self.repo_root = repo_root or Path(__file__).resolve().parent.parent

    def get_source_state(self) -> tuple[str, str, bool]:
        """Reads exact current git HEAD, tree SHA, and worktree clean status."""
        try:
            head_proc = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                check=True,
            )
            head_sha = head_proc.stdout.strip()
            tree_proc = subprocess.run(
                ["git", "rev-parse", "HEAD^{tree}"],
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                check=True,
            )
            tree_sha = tree_proc.stdout.strip()
            diff_proc = subprocess.run(["git", "diff", "--quiet"], cwd=str(self.repo_root))
            cached_proc = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=str(self.repo_root))
            status_proc = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                check=True,
            )
            clean = (
                diff_proc.returncode == 0
                and cached_proc.returncode == 0
                and len(status_proc.stdout.strip()) == 0
            )
            return head_sha, tree_sha, clean
        except Exception:
            return "unknown", "unknown", False

    def create_run_identity(
        self,
        *,
        explicit_scope: AutoScope | None = None,
        plan_identity: str = "plan:v1",
        plan_version: int = 1,
        expected_head: str | None = None,
        expected_tree: str | None = None,
    ) -> ProjectRunIdentity:
        """Explicitly selects and authorizes run identity (Section 19 & 20).

        Default is MILESTONE; PROJECT requires explicit selection.
        """
        # Section 19: Default remains MILESTONE; PROJECT requires explicit per-run selection
        effective_scope = explicit_scope or DEFAULT_AUTO_SCOPE

        head_sha, tree_sha, _ = self.get_source_state()
        bound_head = expected_head or head_sha
        bound_tree = expected_tree or tree_sha

        orch = ScopeOrchestrator(self.conn, self.project_id)
        cursor = orch.get_or_create_cursor(
            run_id=f"run-{self.project_id}-{uuid.uuid4().hex[:8]}",
            scope=effective_scope,
            plan_identity=plan_identity,
            plan_version=plan_version,
        )

        run_id = f"run-proj-{uuid.uuid4().hex[:8]}" if effective_scope == AutoScope.PROJECT else cursor.run_id
        identity = ProjectRunIdentity(
            project_id=self.project_id,
            run_id=run_id,
            scope=effective_scope,
            plan_identity=plan_identity,
            plan_version=plan_version,
            start_head=bound_head,
            start_tree=bound_tree,
            scope_epoch=cursor.scope_epoch,
            cursor_id=cursor.cursor_id,
            created_revision=cursor.state_revision,
        )

        # Update cursor to bind run identity
        updated_cursor = ScopeCursor(
            cursor_id=cursor.cursor_id,
            project_id=cursor.project_id,
            run_id=identity.run_id,
            scope=identity.scope,
            scope_epoch=identity.scope_epoch,
            current_milestone_id=cursor.current_milestone_id,
            current_task_id=cursor.current_task_id,
            last_accepted_task_id=cursor.last_accepted_task_id,
            last_accepted_gate=cursor.last_accepted_gate,
            plan_identity=identity.plan_identity,
            plan_version=identity.plan_version,
            state_revision=cursor.state_revision,
            disposition=cursor.disposition,
            status=cursor.status,
            stop_requested_at=cursor.stop_requested_at,
            stop_reason=cursor.stop_reason,
            explanation_json=cursor.explanation_json,
            updated_at=_now_iso(),
            scope_selection_explicit=cursor.scope_selection_explicit,
        )
        orch.update_cursor_cas(updated_cursor, cursor.state_revision)

        return identity

    def execute_cross_milestone_transition(
        self,
        plan: CanonicalPlanGraph,
        run_identity: ProjectRunIdentity,
        current_cursor: ScopeCursor,
        milestone_gate_evidence: Mapping[str, Mapping[str, Any]],
        task_statuses: Mapping[str, str],
        *,
        current_repo_head: str,
        expected_boundary_head: str,
        manual_approvals: Mapping[str, bool] | None = None,
        policy_approvals: Mapping[str, bool] | None = None,
    ) -> CrossMilestoneTransitionResult:
        """Executes a bounded cross-milestone transition under PROJECT scope (Section 21-27)."""
        orch = ScopeOrchestrator(self.conn, self.project_id)
        cur_ms_id = current_cursor.current_milestone_id or plan.milestones[0].milestone_id
        cur_ms = plan.get_milestone(cur_ms_id)
        assert cur_ms is not None

        # Section 27: Check STOP fence first
        try:
            EffectBoundaryGuard.check(
                self.conn,
                self.project_id,
                current_cursor.scope_epoch,
                EffectBoundary.ORCHESTRATOR_TICK_LAUNCH,
            )
        except StopFenceViolationError as fence_err:
            dec = ScopeDecision(
                action=ScopeAction.STOP_EXTERNAL_STOP_REQUESTED,
                canonical_work_state=CanonicalWorkState.PAUSED,
                reason_code="STOP_FENCED",
                explanation=f"Cross-milestone transition blocked by STOP fence: {fence_err.message}",
                is_terminal=True,
            )
            return CrossMilestoneTransitionResult(
                success=False,
                from_milestone_id=cur_ms_id,
                to_milestone_id=None,
                next_task_id=None,
                decision=dec,
                bound_head_verified=False,
                prior_gate_accepted=False,
                reason_code=dec.reason_code,
                explanation=dec.explanation,
            )

        # Section 22 & 26: Prior Gate Acceptance & Run Identity Verification
        gate_info = milestone_gate_evidence.get(cur_ms_id, {})
        gate_status = gate_info.get("status", "NOT_REACHED")
        gate_run_id = gate_info.get("run_id")

        if gate_run_id is not None and gate_run_id != run_identity.run_id:
            # Wrong run evidence!
            dec = ScopeDecision(
                action=ScopeAction.HALT_BLOCKED,
                canonical_work_state=CanonicalWorkState.BLOCKED,
                reason_code="WRONG_RUN_GATE_EVIDENCE",
                explanation=f"Gate evidence belongs to run '{gate_run_id}', but current run is '{run_identity.run_id}'.",
                is_terminal=True,
            )
            return CrossMilestoneTransitionResult(
                success=False,
                from_milestone_id=cur_ms_id,
                to_milestone_id=None,
                next_task_id=None,
                decision=dec,
                bound_head_verified=False,
                prior_gate_accepted=False,
                reason_code=dec.reason_code,
                explanation=dec.explanation,
            )

        if gate_status != "ACCEPTED":
            # Section 22 & 25: Next milestone can become runnable ONLY if prior gate is ACCEPTED
            dec = ScopeDecision(
                action=ScopeAction.WAIT_MILESTONE_GATE_PENDING if gate_status != "FAILED" else ScopeAction.HALT_BLOCKED,
                canonical_work_state=CanonicalWorkState.WAITING if gate_status != "FAILED" else CanonicalWorkState.BLOCKED,
                reason_code="PRIOR_GATE_NOT_ACCEPTED" if gate_status != "FAILED" else "PRIOR_GATE_FAILED",
                explanation=f"Cannot cross to next milestone: gate for milestone {cur_ms_id} is {gate_status}.",
                is_terminal=(gate_status == "FAILED"),
            )
            return CrossMilestoneTransitionResult(
                success=False,
                from_milestone_id=cur_ms_id,
                to_milestone_id=None,
                next_task_id=None,
                decision=dec,
                bound_head_verified=False,
                prior_gate_accepted=False,
                reason_code=dec.reason_code,
                explanation=dec.explanation,
            )

        # Section 21: Source / HEAD Binding Verification at Boundary
        if current_repo_head != expected_boundary_head:
            dec = ScopeDecision(
                action=ScopeAction.HALT_BLOCKED,
                canonical_work_state=CanonicalWorkState.BLOCKED,
                reason_code="STALE_HEAD_DIVERGENCE",
                explanation=f"Stale HEAD at milestone boundary: current={current_repo_head}, expected={expected_boundary_head}.",
                is_terminal=True,
            )
            return CrossMilestoneTransitionResult(
                success=False,
                from_milestone_id=cur_ms_id,
                to_milestone_id=None,
                next_task_id=None,
                decision=dec,
                bound_head_verified=False,
                prior_gate_accepted=True,
                reason_code=dec.reason_code,
                explanation=dec.explanation,
            )

        # Determine next milestone
        ms_index = [m.milestone_id for m in plan.milestones].index(cur_ms_id)
        if ms_index + 1 >= len(plan.milestones):
            # Section 28: Project Completion
            dec = ScopeDecision(
                action=ScopeAction.STOP_PROJECT_COMPLETE,
                canonical_work_state=CanonicalWorkState.COMPLETED,
                reason_code="PROJECT_COMPLETED",
                explanation="All milestones and milestone gates in project are accepted. Project is complete.",
                is_terminal=True,
            )
            return CrossMilestoneTransitionResult(
                success=True,
                from_milestone_id=cur_ms_id,
                to_milestone_id=None,
                next_task_id=None,
                decision=dec,
                bound_head_verified=True,
                prior_gate_accepted=True,
                reason_code=dec.reason_code,
                explanation=dec.explanation,
            )

        next_ms = plan.milestones[ms_index + 1]
        next_ms_id = next_ms.milestone_id

        # First task in next milestone
        first_tid, deps_ok, pending_deps = orch.resolve_next_runnable_task(plan, next_ms_id, task_statuses)
        if not first_tid:
            dec = ScopeDecision(
                action=ScopeAction.HALT_NO_RUNNABLE_WORK,
                canonical_work_state=CanonicalWorkState.NO_RUNNABLE_WORK,
                reason_code="NO_RUNNABLE_WORK",
                explanation=f"No runnable tasks in milestone {next_ms_id}.",
                is_terminal=True,
            )
            return CrossMilestoneTransitionResult(
                success=False,
                from_milestone_id=cur_ms_id,
                to_milestone_id=next_ms_id,
                next_task_id=None,
                decision=dec,
                bound_head_verified=True,
                prior_gate_accepted=True,
                reason_code=dec.reason_code,
                explanation=dec.explanation,
            )

        # Section 24: Manual & Policy Boundary Checks
        task_node = plan.get_task(first_tid)
        manual_app = manual_approvals or {}
        policy_app = policy_approvals or {}

        if task_node and task_node.requires_manual_approval and not manual_app.get(first_tid, False):
            dec = ScopeDecision(
                action=ScopeAction.PAUSE_MANUAL_GATE_REQUIRED,
                canonical_work_state=CanonicalWorkState.PAUSED,
                selected_task_id=first_tid,
                selected_milestone_id=next_ms_id,
                reason_code="MANUAL_APPROVAL_REQUIRED",
                explanation=f"Task {first_tid} in milestone {next_ms_id} requires manual operator approval.",
                is_terminal=False,
            )
            return CrossMilestoneTransitionResult(
                success=False,
                from_milestone_id=cur_ms_id,
                to_milestone_id=next_ms_id,
                next_task_id=first_tid,
                decision=dec,
                bound_head_verified=True,
                prior_gate_accepted=True,
                reason_code=dec.reason_code,
                explanation=dec.explanation,
            )

        if task_node and task_node.requires_policy_approval and not policy_app.get(first_tid, False):
            dec = ScopeDecision(
                action=ScopeAction.PAUSE_POLICY_APPROVAL_REQUIRED,
                canonical_work_state=CanonicalWorkState.PAUSED,
                selected_task_id=first_tid,
                selected_milestone_id=next_ms_id,
                reason_code="POLICY_APPROVAL_REQUIRED",
                explanation=f"Task {first_tid} in milestone {next_ms_id} requires policy approval.",
                is_terminal=False,
            )
            return CrossMilestoneTransitionResult(
                success=False,
                from_milestone_id=cur_ms_id,
                to_milestone_id=next_ms_id,
                next_task_id=first_tid,
                decision=dec,
                bound_head_verified=True,
                prior_gate_accepted=True,
                reason_code=dec.reason_code,
                explanation=dec.explanation,
            )

        # Section 23: Successful transition to next milestone
        dec = ScopeDecision(
            action=ScopeAction.LAUNCH_TASK,
            canonical_work_state=CanonicalWorkState.RUNNABLE,
            selected_task_id=first_tid,
            selected_milestone_id=next_ms_id,
            reason_code="CROSS_MILESTONE_LAUNCH",
            explanation=f"Successfully crossed milestone boundary from {cur_ms_id} to {next_ms_id}; ready to launch {first_tid}.",
            crosses_task_boundary=True,
            crosses_milestone_boundary=True,
        )

        # Advance cursor to next milestone
        new_cursor = ScopeCursor(
            cursor_id=current_cursor.cursor_id,
            project_id=self.project_id,
            run_id=run_identity.run_id,
            scope=AutoScope.PROJECT,
            scope_epoch=current_cursor.scope_epoch,
            current_milestone_id=next_ms_id,
            current_task_id=first_tid,
            last_accepted_task_id=current_cursor.last_accepted_task_id,
            last_accepted_gate=cur_ms.gate_id,
            plan_identity=current_cursor.plan_identity,
            plan_version=current_cursor.plan_version,
            state_revision=current_cursor.state_revision,
            disposition=dec.action.value,
            status=current_cursor.status,
            stop_requested_at=current_cursor.stop_requested_at,
            stop_reason=current_cursor.stop_reason,
            explanation_json=json.dumps({"transition": f"{cur_ms_id}->{next_ms_id}", "task": first_tid}),
            updated_at=_now_iso(),
            scope_selection_explicit=current_cursor.scope_selection_explicit,
        )
        orch.update_cursor_cas(new_cursor, current_cursor.state_revision)

        return CrossMilestoneTransitionResult(
            success=True,
            from_milestone_id=cur_ms_id,
            to_milestone_id=next_ms_id,
            next_task_id=first_tid,
            decision=dec,
            bound_head_verified=True,
            prior_gate_accepted=True,
            reason_code=dec.reason_code,
            explanation=dec.explanation,
        )

    def submit_gui_scope_command(
        self,
        command_intent: Mapping[str, Any],
    ) -> tuple[bool, str, ProjectRunIdentity | None]:
        """Section 30: GUI/API Command Boundary.

        Submits an intent validated by canonical workflow policy.
        GUI/API cannot bypass policy or mutate status directly.
        """
        project_id = command_intent.get("project_id")
        if project_id != self.project_id:
            return False, "Project ID mismatch", None

        requested_scope = command_intent.get("scope")
        if not requested_scope:
            return False, "Scope must be explicitly specified", None

        try:
            scope_enum = AutoScope(requested_scope)
        except ValueError:
            return False, f"Invalid scope '{requested_scope}'", None

        # Policy validation: only authorized project runs can use PROJECT scope
        if scope_enum == AutoScope.PROJECT:
            explicit_confirm = command_intent.get("explicit_project_authorization", False)
            if not explicit_confirm:
                return False, "PROJECT scope requires explicit authorization confirmation", None

        identity = self.create_run_identity(
            explicit_scope=scope_enum,
            plan_identity=command_intent.get("plan_identity", "plan:v1"),
            plan_version=command_intent.get("plan_version", 1),
        )

        return True, "Authorized", identity

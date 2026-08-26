"""NX-020: AUTO Scope Contract and Transition Model.

Defines four canonical AUTO scopes:
- TASK: Exactly one accepted task per run, then stop. Never crosses task boundary.
- MILESTONE: Backward-compatible default. Executes within milestone, stops at final gate.
             Never crosses into next milestone.
- PROJECT: Crosses milestone boundaries ONLY after previous milestone gate is accepted.
           Respects all dependencies, gates, and policy approvals.
- UNTIL_STOPPED: Same correctness rules as PROJECT, continuing approved runnable work
                 until explicitly stopped or project completed. Never bypasses gates/dependencies.

All outcomes derive from an executable table-driven boundary contract.
Zero ambiguous legal scope decisions. Zero illegal scope transitions accepted.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Sequence

AUTO_SCOPE_SCHEMA_VERSION = "1.0.0"


class AutoScope(str, Enum):
    """The four canonical AUTO scopes."""
    TASK = "TASK"
    MILESTONE = "MILESTONE"
    PROJECT = "PROJECT"
    UNTIL_STOPPED = "UNTIL_STOPPED"


DEFAULT_AUTO_SCOPE = AutoScope.MILESTONE


class CanonicalWorkState(str, Enum):
    """Deterministic distinction among non-runnable and workflow states."""
    COMPLETED = "COMPLETED"
    WAITING = "WAITING"
    PAUSED = "PAUSED"
    BLOCKED = "BLOCKED"
    NO_RUNNABLE_WORK = "NO_RUNNABLE_WORK"
    WAITING_FOR_PLAN = "WAITING_FOR_PLAN"
    RUNNABLE = "RUNNABLE"


class ScopeAction(str, Enum):
    """Deterministic actions emitted by the scope contract."""
    LAUNCH_TASK = "LAUNCH_TASK"
    CONTINUE_TASK_RETRY = "CONTINUE_TASK_RETRY"
    CONTINUE_TASK_REPAIR = "CONTINUE_TASK_REPAIR"
    STOP_SCOPE_COMPLETE = "STOP_SCOPE_COMPLETE"
    STOP_PROJECT_COMPLETE = "STOP_PROJECT_COMPLETE"
    STOP_EXTERNAL_STOP_REQUESTED = "STOP_EXTERNAL_STOP_REQUESTED"
    PAUSE_MANUAL_GATE_REQUIRED = "PAUSE_MANUAL_GATE_REQUIRED"
    PAUSE_POLICY_APPROVAL_REQUIRED = "PAUSE_POLICY_APPROVAL_REQUIRED"
    WAIT_DEPENDENCY_PENDING = "WAIT_DEPENDENCY_PENDING"
    WAIT_MILESTONE_GATE_PENDING = "WAIT_MILESTONE_GATE_PENDING"
    WAIT_CI_WAITING = "WAIT_CI_WAITING"
    WAIT_TRANSIENT_BACKOFF = "WAIT_TRANSIENT_BACKOFF"
    HALT_NO_RUNNABLE_WORK = "HALT_NO_RUNNABLE_WORK"
    HALT_WAITING_FOR_PLAN = "HALT_WAITING_FOR_PLAN"
    HALT_BLOCKED = "HALT_BLOCKED"


@dataclass(frozen=True)
class ScopeInputSnapshot:
    """Canonical authority snapshot presented to the scope evaluator."""
    current_scope: AutoScope
    current_task_id: str | None = None
    current_task_status: str | None = None  # "ACCEPTED", "IN_PROGRESS", "FAILED", "BLOCKED", None
    task_needs_retry: bool = False
    task_needs_repair: bool = False
    task_attempts_exhausted: bool = False
    accepted_tasks_in_current_scope: int = 0
    current_milestone_id: str | None = None
    all_milestone_tasks_accepted: bool = False
    current_milestone_gate_status: str | None = None  # "NOT_REACHED", "IN_PROGRESS", "ACCEPTED", "FAILED", "PENDING_APPROVAL", None
    next_task_in_milestone_id: str | None = None
    next_task_dependencies_satisfied: bool = True
    next_milestone_id: str | None = None
    first_task_in_next_milestone_id: str | None = None
    next_milestone_dependencies_satisfied: bool = True
    all_project_milestones_completed: bool = False
    manual_gate_required: bool = False
    manual_gate_approved: bool = False
    policy_gate_required: bool = False
    policy_gate_approved: bool = False
    stop_requested: bool = False
    ci_waiting: bool = False
    transient_backoff: bool = False
    approved_plan_exhausted: bool = False


@dataclass(frozen=True)
class ScopeDecision:
    """Deterministic evaluation outcome from the scope contract."""
    action: ScopeAction
    canonical_work_state: CanonicalWorkState
    selected_task_id: str | None = None
    selected_milestone_id: str | None = None
    reason_code: str = ""
    explanation: str = ""
    crosses_task_boundary: bool = False
    crosses_milestone_boundary: bool = False
    bypasses_gate: bool = False
    bypasses_dependency: bool = False
    bypasses_policy: bool = False
    is_terminal: bool = False


def evaluate_scope_transition(snapshot: ScopeInputSnapshot) -> ScopeDecision:
    """Evaluates scope boundary transition rules deterministically."""
    # 1. Explicit STOP requested
    if snapshot.stop_requested:
        return ScopeDecision(
            action=ScopeAction.STOP_EXTERNAL_STOP_REQUESTED,
            canonical_work_state=CanonicalWorkState.PAUSED,
            reason_code="STOP_REQUESTED",
            explanation="Execution halted because external STOP was explicitly requested.",
            is_terminal=True,
        )

    # 2. Approved plan exhausted / waiting for plan
    if snapshot.approved_plan_exhausted:
        return ScopeDecision(
            action=ScopeAction.HALT_WAITING_FOR_PLAN,
            canonical_work_state=CanonicalWorkState.WAITING_FOR_PLAN,
            reason_code="WAITING_FOR_PLAN",
            explanation="Approved project plan exhausted; waiting for plan extension. Never fabricating tasks.",
            is_terminal=True,
        )

    # 3. Whole Project already completed (applicable to PROJECT and UNTIL_STOPPED)
    if snapshot.all_project_milestones_completed and snapshot.current_scope in (AutoScope.PROJECT, AutoScope.UNTIL_STOPPED):
        return ScopeDecision(
            action=ScopeAction.STOP_PROJECT_COMPLETE,
            canonical_work_state=CanonicalWorkState.COMPLETED,
            reason_code="PROJECT_COMPLETED",
            explanation="All milestones and milestone gates in project are accepted. Project is complete.",
            is_terminal=True,
        )

    # 4. Manual / Policy Gate required and unapproved
    if snapshot.manual_gate_required and not snapshot.manual_gate_approved:
        return ScopeDecision(
            action=ScopeAction.PAUSE_MANUAL_GATE_REQUIRED,
            canonical_work_state=CanonicalWorkState.PAUSED,
            reason_code="MANUAL_APPROVAL_REQUIRED",
            explanation="A manual operator checkpoint is required before proceeding.",
            is_terminal=False,
        )

    if snapshot.policy_gate_required and not snapshot.policy_gate_approved:
        return ScopeDecision(
            action=ScopeAction.PAUSE_POLICY_APPROVAL_REQUIRED,
            canonical_work_state=CanonicalWorkState.PAUSED,
            reason_code="POLICY_APPROVAL_REQUIRED",
            explanation="A policy gate approval is required before proceeding.",
            is_terminal=False,
        )

    # 5. In-flight durable waiting / backoff
    if snapshot.ci_waiting:
        return ScopeDecision(
            action=ScopeAction.WAIT_CI_WAITING,
            canonical_work_state=CanonicalWorkState.WAITING,
            selected_task_id=snapshot.current_task_id,
            reason_code="CI_WAITING_DURABLE",
            explanation="Task is durably waiting for CI pipeline completion.",
            is_terminal=False,
        )

    if snapshot.transient_backoff:
        return ScopeDecision(
            action=ScopeAction.WAIT_TRANSIENT_BACKOFF,
            canonical_work_state=CanonicalWorkState.WAITING,
            selected_task_id=snapshot.current_task_id,
            reason_code="TRANSIENT_BACKOFF",
            explanation="Transient recovery is in exponential backoff before next attempt.",
            is_terminal=False,
        )

    # 6. Current task in-progress, needing retry, or needing repair
    if snapshot.current_task_status in ("IN_PROGRESS", "FAILED"):
        if snapshot.task_attempts_exhausted:
            return ScopeDecision(
                action=ScopeAction.HALT_BLOCKED,
                canonical_work_state=CanonicalWorkState.BLOCKED,
                selected_task_id=snapshot.current_task_id,
                reason_code="BUDGET_EXHAUSTED",
                explanation=f"Task {snapshot.current_task_id} exhausted its failure/repair budget.",
                is_terminal=True,
            )
        if snapshot.task_needs_repair:
            return ScopeDecision(
                action=ScopeAction.CONTINUE_TASK_REPAIR,
                canonical_work_state=CanonicalWorkState.RUNNABLE,
                selected_task_id=snapshot.current_task_id,
                reason_code="TASK_REPAIR_LOOP",
                explanation=f"Continuing repair loop attempt for task {snapshot.current_task_id}.",
                crosses_task_boundary=False,
            )
        if snapshot.task_needs_retry:
            return ScopeDecision(
                action=ScopeAction.CONTINUE_TASK_RETRY,
                canonical_work_state=CanonicalWorkState.RUNNABLE,
                selected_task_id=snapshot.current_task_id,
                reason_code="TASK_TRANSIENT_RETRY",
                explanation=f"Continuing transient retry attempt for task {snapshot.current_task_id}.",
                crosses_task_boundary=False,
            )
        return ScopeDecision(
            action=ScopeAction.LAUNCH_TASK,
            canonical_work_state=CanonicalWorkState.RUNNABLE,
            selected_task_id=snapshot.current_task_id,
            selected_milestone_id=snapshot.current_milestone_id,
            reason_code="TASK_IN_PROGRESS",
            explanation=f"Current task {snapshot.current_task_id} is in progress.",
            crosses_task_boundary=False,
        )

    # 7. Scope Boundaries when current task is ACCEPTED (or no task started yet)
    task_just_accepted = bool(snapshot.current_task_status == "ACCEPTED")

    # --- TASK SCOPE ---
    if snapshot.current_scope == AutoScope.TASK:
        if task_just_accepted or snapshot.accepted_tasks_in_current_scope >= 1:
            return ScopeDecision(
                action=ScopeAction.STOP_SCOPE_COMPLETE,
                canonical_work_state=CanonicalWorkState.COMPLETED,
                reason_code="TASK_SCOPE_COMPLETED",
                explanation="TASK scope stops after exactly 1 accepted task. Will not launch next task.",
                crosses_task_boundary=False,
                is_terminal=True,
            )
        if snapshot.next_task_in_milestone_id:
            if not snapshot.next_task_dependencies_satisfied:
                return ScopeDecision(
                    action=ScopeAction.WAIT_DEPENDENCY_PENDING,
                    canonical_work_state=CanonicalWorkState.WAITING,
                    selected_task_id=snapshot.next_task_in_milestone_id,
                    reason_code="TASK_DEPENDENCY_PENDING",
                    explanation=f"Task {snapshot.next_task_in_milestone_id} dependencies are not yet satisfied.",
                )
            return ScopeDecision(
                action=ScopeAction.LAUNCH_TASK,
                canonical_work_state=CanonicalWorkState.RUNNABLE,
                selected_task_id=snapshot.next_task_in_milestone_id,
                selected_milestone_id=snapshot.current_milestone_id,
                reason_code="TASK_SCOPE_START",
                explanation=f"Starting task {snapshot.next_task_in_milestone_id} under TASK scope.",
                crosses_task_boundary=False,
            )
        return ScopeDecision(
            action=ScopeAction.HALT_NO_RUNNABLE_WORK,
            canonical_work_state=CanonicalWorkState.NO_RUNNABLE_WORK,
            reason_code="NO_RUNNABLE_WORK",
            explanation="No runnable task found in TASK scope.",
            is_terminal=True,
        )

    # --- MILESTONE SCOPE ---
    if snapshot.current_scope == AutoScope.MILESTONE:
        if snapshot.next_task_in_milestone_id:
            if not snapshot.next_task_dependencies_satisfied:
                return ScopeDecision(
                    action=ScopeAction.WAIT_DEPENDENCY_PENDING,
                    canonical_work_state=CanonicalWorkState.WAITING,
                    selected_task_id=snapshot.next_task_in_milestone_id,
                    reason_code="MILESTONE_TASK_DEPENDENCY_PENDING",
                    explanation=f"Task {snapshot.next_task_in_milestone_id} dependencies not yet satisfied.",
                )
            return ScopeDecision(
                action=ScopeAction.LAUNCH_TASK,
                canonical_work_state=CanonicalWorkState.RUNNABLE,
                selected_task_id=snapshot.next_task_in_milestone_id,
                selected_milestone_id=snapshot.current_milestone_id,
                reason_code="MILESTONE_NEXT_TASK",
                explanation=f"Advancing to next task {snapshot.next_task_in_milestone_id} within milestone.",
                crosses_task_boundary=True,
                crosses_milestone_boundary=False,
            )

        if snapshot.all_milestone_tasks_accepted:
            if snapshot.current_milestone_gate_status != "ACCEPTED":
                return ScopeDecision(
                    action=ScopeAction.WAIT_MILESTONE_GATE_PENDING,
                    canonical_work_state=CanonicalWorkState.WAITING,
                    selected_milestone_id=snapshot.current_milestone_id,
                    reason_code="MILESTONE_GATE_PENDING",
                    explanation=f"All tasks accepted; milestone gate for {snapshot.current_milestone_id} is pending.",
                )
            return ScopeDecision(
                action=ScopeAction.STOP_SCOPE_COMPLETE,
                canonical_work_state=CanonicalWorkState.COMPLETED,
                selected_milestone_id=snapshot.current_milestone_id,
                reason_code="MILESTONE_SCOPE_COMPLETED",
                explanation=f"Milestone {snapshot.current_milestone_id} gate accepted. MILESTONE scope complete; will not cross to next milestone.",
                crosses_milestone_boundary=False,
                is_terminal=True,
            )

        return ScopeDecision(
            action=ScopeAction.HALT_NO_RUNNABLE_WORK,
            canonical_work_state=CanonicalWorkState.NO_RUNNABLE_WORK,
            reason_code="NO_RUNNABLE_WORK",
            explanation="No runnable task found within current milestone.",
            is_terminal=True,
        )

    # --- PROJECT & UNTIL_STOPPED SCOPES ---
    if snapshot.current_scope in (AutoScope.PROJECT, AutoScope.UNTIL_STOPPED):
        if snapshot.next_task_in_milestone_id:
            if not snapshot.next_task_dependencies_satisfied:
                return ScopeDecision(
                    action=ScopeAction.WAIT_DEPENDENCY_PENDING,
                    canonical_work_state=CanonicalWorkState.WAITING,
                    selected_task_id=snapshot.next_task_in_milestone_id,
                    reason_code="TASK_DEPENDENCY_PENDING",
                    explanation=f"Task {snapshot.next_task_in_milestone_id} dependencies not yet satisfied.",
                )
            return ScopeDecision(
                action=ScopeAction.LAUNCH_TASK,
                canonical_work_state=CanonicalWorkState.RUNNABLE,
                selected_task_id=snapshot.next_task_in_milestone_id,
                selected_milestone_id=snapshot.current_milestone_id,
                reason_code="PROJECT_NEXT_TASK",
                explanation=f"Launching task {snapshot.next_task_in_milestone_id}.",
                crosses_task_boundary=True,
                crosses_milestone_boundary=False,
            )

        if snapshot.all_milestone_tasks_accepted:
            if snapshot.current_milestone_gate_status != "ACCEPTED":
                return ScopeDecision(
                    action=ScopeAction.WAIT_MILESTONE_GATE_PENDING,
                    canonical_work_state=CanonicalWorkState.WAITING,
                    selected_milestone_id=snapshot.current_milestone_id,
                    reason_code="MILESTONE_GATE_REQUIRED_BEFORE_ADVANCING",
                    explanation=f"Milestone {snapshot.current_milestone_id} gate must be accepted before advancing to next milestone.",
                )

            if snapshot.next_milestone_id:
                if not snapshot.next_milestone_dependencies_satisfied:
                    return ScopeDecision(
                        action=ScopeAction.WAIT_DEPENDENCY_PENDING,
                        canonical_work_state=CanonicalWorkState.WAITING,
                        selected_milestone_id=snapshot.next_milestone_id,
                        reason_code="NEXT_MILESTONE_DEPENDENCY_PENDING",
                        explanation=f"Next milestone {snapshot.next_milestone_id} dependencies are pending.",
                    )
                return ScopeDecision(
                    action=ScopeAction.LAUNCH_TASK,
                    canonical_work_state=CanonicalWorkState.RUNNABLE,
                    selected_task_id=snapshot.first_task_in_next_milestone_id or snapshot.next_task_in_milestone_id,
                    selected_milestone_id=snapshot.next_milestone_id,
                    reason_code="ADVANCE_TO_NEXT_MILESTONE",
                    explanation=f"Previous milestone gate passed. Advancing to next milestone {snapshot.next_milestone_id}.",
                    crosses_task_boundary=True,
                    crosses_milestone_boundary=True,
                )

            return ScopeDecision(
                action=ScopeAction.STOP_PROJECT_COMPLETE,
                canonical_work_state=CanonicalWorkState.COMPLETED,
                reason_code="PROJECT_COMPLETED",
                explanation="All milestones and gates completed.",
                is_terminal=True,
            )

        return ScopeDecision(
            action=ScopeAction.HALT_NO_RUNNABLE_WORK,
            canonical_work_state=CanonicalWorkState.NO_RUNNABLE_WORK,
            reason_code="NO_RUNNABLE_WORK",
            explanation="No runnable work available in project.",
            is_terminal=True,
        )

    return ScopeDecision(
        action=ScopeAction.HALT_BLOCKED,
        canonical_work_state=CanonicalWorkState.BLOCKED,
        reason_code="UNKNOWN_SCOPE",
        explanation="Unknown or invalid scope specified.",
        is_terminal=True,
    )


# ==============================================================================
# CANONICAL BOUNDARY MATRIX FIXTURES
# ==============================================================================

@dataclass(frozen=True)
class ScopeBoundaryFixture:
    fixture_id: str
    description: str
    snapshot: ScopeInputSnapshot
    expected_action: ScopeAction
    expected_state: CanonicalWorkState
    expected_terminal: bool = False


CANONICAL_BOUNDARY_FIXTURES: tuple[ScopeBoundaryFixture, ...] = (
    # TASK Scope
    ScopeBoundaryFixture(
        fixture_id="F01_TASK_START",
        description="TASK scope initial task ready",
        snapshot=ScopeInputSnapshot(
            current_scope=AutoScope.TASK,
            current_milestone_id="M1",
            next_task_in_milestone_id="T1",
            next_task_dependencies_satisfied=True,
        ),
        expected_action=ScopeAction.LAUNCH_TASK,
        expected_state=CanonicalWorkState.RUNNABLE,
    ),
    ScopeBoundaryFixture(
        fixture_id="F02_TASK_IN_PROGRESS",
        description="TASK scope current task in progress",
        snapshot=ScopeInputSnapshot(
            current_scope=AutoScope.TASK,
            current_milestone_id="M1",
            current_task_id="T1",
            current_task_status="IN_PROGRESS",
        ),
        expected_action=ScopeAction.LAUNCH_TASK,
        expected_state=CanonicalWorkState.RUNNABLE,
    ),
    ScopeBoundaryFixture(
        fixture_id="F03_TASK_RETRY",
        description="TASK scope current task needs transient retry",
        snapshot=ScopeInputSnapshot(
            current_scope=AutoScope.TASK,
            current_milestone_id="M1",
            current_task_id="T1",
            current_task_status="FAILED",
            task_needs_retry=True,
        ),
        expected_action=ScopeAction.CONTINUE_TASK_RETRY,
        expected_state=CanonicalWorkState.RUNNABLE,
    ),
    ScopeBoundaryFixture(
        fixture_id="F04_TASK_REPAIR",
        description="TASK scope current task needs repair loop",
        snapshot=ScopeInputSnapshot(
            current_scope=AutoScope.TASK,
            current_milestone_id="M1",
            current_task_id="T1",
            current_task_status="FAILED",
            task_needs_repair=True,
        ),
        expected_action=ScopeAction.CONTINUE_TASK_REPAIR,
        expected_state=CanonicalWorkState.RUNNABLE,
    ),
    ScopeBoundaryFixture(
        fixture_id="F05_TASK_EXHAUSTED",
        description="TASK scope task budget exhausted",
        snapshot=ScopeInputSnapshot(
            current_scope=AutoScope.TASK,
            current_milestone_id="M1",
            current_task_id="T1",
            current_task_status="FAILED",
            task_attempts_exhausted=True,
        ),
        expected_action=ScopeAction.HALT_BLOCKED,
        expected_state=CanonicalWorkState.BLOCKED,
        expected_terminal=True,
    ),
    ScopeBoundaryFixture(
        fixture_id="F06_TASK_ACCEPTED_STOPS",
        description="TASK scope stops immediately after 1 accepted task",
        snapshot=ScopeInputSnapshot(
            current_scope=AutoScope.TASK,
            current_milestone_id="M1",
            current_task_id="T1",
            current_task_status="ACCEPTED",
            accepted_tasks_in_current_scope=1,
            next_task_in_milestone_id="T2",
        ),
        expected_action=ScopeAction.STOP_SCOPE_COMPLETE,
        expected_state=CanonicalWorkState.COMPLETED,
        expected_terminal=True,
    ),
    ScopeBoundaryFixture(
        fixture_id="F07_TASK_DEP_PENDING",
        description="TASK scope initial task dependencies pending",
        snapshot=ScopeInputSnapshot(
            current_scope=AutoScope.TASK,
            current_milestone_id="M1",
            next_task_in_milestone_id="T2",
            next_task_dependencies_satisfied=False,
        ),
        expected_action=ScopeAction.WAIT_DEPENDENCY_PENDING,
        expected_state=CanonicalWorkState.WAITING,
    ),
    # MILESTONE Scope
    ScopeBoundaryFixture(
        fixture_id="F08_MILESTONE_START",
        description="MILESTONE scope launches first task",
        snapshot=ScopeInputSnapshot(
            current_scope=AutoScope.MILESTONE,
            current_milestone_id="M1",
            next_task_in_milestone_id="T1",
            next_task_dependencies_satisfied=True,
        ),
        expected_action=ScopeAction.LAUNCH_TASK,
        expected_state=CanonicalWorkState.RUNNABLE,
    ),
    ScopeBoundaryFixture(
        fixture_id="F09_MILESTONE_ADVANCE_TASK",
        description="MILESTONE scope advances to next task in same milestone",
        snapshot=ScopeInputSnapshot(
            current_scope=AutoScope.MILESTONE,
            current_milestone_id="M1",
            current_task_id="T1",
            current_task_status="ACCEPTED",
            accepted_tasks_in_current_scope=1,
            next_task_in_milestone_id="T2",
            next_task_dependencies_satisfied=True,
        ),
        expected_action=ScopeAction.LAUNCH_TASK,
        expected_state=CanonicalWorkState.RUNNABLE,
    ),
    ScopeBoundaryFixture(
        fixture_id="F10_MILESTONE_TASK_DEP_PENDING",
        description="MILESTONE scope next task dependency pending",
        snapshot=ScopeInputSnapshot(
            current_scope=AutoScope.MILESTONE,
            current_milestone_id="M1",
            current_task_id="T1",
            current_task_status="ACCEPTED",
            accepted_tasks_in_current_scope=1,
            next_task_in_milestone_id="T2",
            next_task_dependencies_satisfied=False,
        ),
        expected_action=ScopeAction.WAIT_DEPENDENCY_PENDING,
        expected_state=CanonicalWorkState.WAITING,
    ),
    ScopeBoundaryFixture(
        fixture_id="F11_MILESTONE_GATE_PENDING",
        description="MILESTONE scope all tasks done, gate pending",
        snapshot=ScopeInputSnapshot(
            current_scope=AutoScope.MILESTONE,
            current_milestone_id="M1",
            all_milestone_tasks_accepted=True,
            current_milestone_gate_status="NOT_REACHED",
            next_milestone_id="M2",
        ),
        expected_action=ScopeAction.WAIT_MILESTONE_GATE_PENDING,
        expected_state=CanonicalWorkState.WAITING,
    ),
    ScopeBoundaryFixture(
        fixture_id="F12_MILESTONE_GATE_ACCEPTED_STOPS",
        description="MILESTONE scope gate accepted -> stops, does not enter M2",
        snapshot=ScopeInputSnapshot(
            current_scope=AutoScope.MILESTONE,
            current_milestone_id="M1",
            all_milestone_tasks_accepted=True,
            current_milestone_gate_status="ACCEPTED",
            next_milestone_id="M2",
        ),
        expected_action=ScopeAction.STOP_SCOPE_COMPLETE,
        expected_state=CanonicalWorkState.COMPLETED,
        expected_terminal=True,
    ),
    # PROJECT Scope
    ScopeBoundaryFixture(
        fixture_id="F13_PROJECT_NEXT_TASK",
        description="PROJECT scope advances task in milestone",
        snapshot=ScopeInputSnapshot(
            current_scope=AutoScope.PROJECT,
            current_milestone_id="M1",
            current_task_id="T1",
            current_task_status="ACCEPTED",
            next_task_in_milestone_id="T2",
            next_task_dependencies_satisfied=True,
        ),
        expected_action=ScopeAction.LAUNCH_TASK,
        expected_state=CanonicalWorkState.RUNNABLE,
    ),
    ScopeBoundaryFixture(
        fixture_id="F14_PROJECT_GATE_PENDING",
        description="PROJECT scope waits for milestone gate before crossing",
        snapshot=ScopeInputSnapshot(
            current_scope=AutoScope.PROJECT,
            current_milestone_id="M1",
            all_milestone_tasks_accepted=True,
            current_milestone_gate_status="IN_PROGRESS",
            next_milestone_id="M2",
        ),
        expected_action=ScopeAction.WAIT_MILESTONE_GATE_PENDING,
        expected_state=CanonicalWorkState.WAITING,
    ),
    ScopeBoundaryFixture(
        fixture_id="F15_PROJECT_CROSSES_MILESTONE",
        description="PROJECT scope gate accepted -> crosses into next milestone",
        snapshot=ScopeInputSnapshot(
            current_scope=AutoScope.PROJECT,
            current_milestone_id="M1",
            all_milestone_tasks_accepted=True,
            current_milestone_gate_status="ACCEPTED",
            next_milestone_id="M2",
            next_milestone_dependencies_satisfied=True,
        ),
        expected_action=ScopeAction.LAUNCH_TASK,
        expected_state=CanonicalWorkState.RUNNABLE,
    ),
    ScopeBoundaryFixture(
        fixture_id="F16_PROJECT_NEXT_MILESTONE_DEP_PENDING",
        description="PROJECT scope gate accepted but next milestone deps pending",
        snapshot=ScopeInputSnapshot(
            current_scope=AutoScope.PROJECT,
            current_milestone_id="M1",
            all_milestone_tasks_accepted=True,
            current_milestone_gate_status="ACCEPTED",
            next_milestone_id="M2",
            next_milestone_dependencies_satisfied=False,
        ),
        expected_action=ScopeAction.WAIT_DEPENDENCY_PENDING,
        expected_state=CanonicalWorkState.WAITING,
    ),
    ScopeBoundaryFixture(
        fixture_id="F17_PROJECT_COMPLETE",
        description="PROJECT scope all milestones complete -> stops project",
        snapshot=ScopeInputSnapshot(
            current_scope=AutoScope.PROJECT,
            current_milestone_id="M4",
            all_milestone_tasks_accepted=True,
            current_milestone_gate_status="ACCEPTED",
            all_project_milestones_completed=True,
        ),
        expected_action=ScopeAction.STOP_PROJECT_COMPLETE,
        expected_state=CanonicalWorkState.COMPLETED,
        expected_terminal=True,
    ),
    # UNTIL_STOPPED Scope
    ScopeBoundaryFixture(
        fixture_id="F18_UNTIL_STOPPED_GATE_PENDING",
        description="UNTIL_STOPPED scope waits for gate like PROJECT",
        snapshot=ScopeInputSnapshot(
            current_scope=AutoScope.UNTIL_STOPPED,
            current_milestone_id="M1",
            all_milestone_tasks_accepted=True,
            current_milestone_gate_status="NOT_REACHED",
            next_milestone_id="M2",
        ),
        expected_action=ScopeAction.WAIT_MILESTONE_GATE_PENDING,
        expected_state=CanonicalWorkState.WAITING,
    ),
    ScopeBoundaryFixture(
        fixture_id="F19_UNTIL_STOPPED_ADVANCES",
        description="UNTIL_STOPPED advances to M2 after gate accepted",
        snapshot=ScopeInputSnapshot(
            current_scope=AutoScope.UNTIL_STOPPED,
            current_milestone_id="M1",
            all_milestone_tasks_accepted=True,
            current_milestone_gate_status="ACCEPTED",
            next_milestone_id="M2",
            next_milestone_dependencies_satisfied=True,
        ),
        expected_action=ScopeAction.LAUNCH_TASK,
        expected_state=CanonicalWorkState.RUNNABLE,
    ),
    # Manual & Policy Gate Fixtures
    ScopeBoundaryFixture(
        fixture_id="F20_MANUAL_GATE_REQUIRED",
        description="Manual checkpoint blocks automatic launch across any scope",
        snapshot=ScopeInputSnapshot(
            current_scope=AutoScope.PROJECT,
            current_milestone_id="M1",
            next_task_in_milestone_id="T1",
            manual_gate_required=True,
            manual_gate_approved=False,
        ),
        expected_action=ScopeAction.PAUSE_MANUAL_GATE_REQUIRED,
        expected_state=CanonicalWorkState.PAUSED,
    ),
    ScopeBoundaryFixture(
        fixture_id="F21_POLICY_GATE_REQUIRED",
        description="Policy approval required blocks automatic launch",
        snapshot=ScopeInputSnapshot(
            current_scope=AutoScope.UNTIL_STOPPED,
            current_milestone_id="M1",
            next_task_in_milestone_id="T1",
            policy_gate_required=True,
            policy_gate_approved=False,
        ),
        expected_action=ScopeAction.PAUSE_POLICY_APPROVAL_REQUIRED,
        expected_state=CanonicalWorkState.PAUSED,
    ),
    # STOP and Waiting / Exhaustion Fixtures
    ScopeBoundaryFixture(
        fixture_id="F22_STOP_REQUESTED",
        description="Explicit STOP halts execution immediately",
        snapshot=ScopeInputSnapshot(
            current_scope=AutoScope.PROJECT,
            current_milestone_id="M1",
            next_task_in_milestone_id="T1",
            stop_requested=True,
        ),
        expected_action=ScopeAction.STOP_EXTERNAL_STOP_REQUESTED,
        expected_state=CanonicalWorkState.PAUSED,
        expected_terminal=True,
    ),
    ScopeBoundaryFixture(
        fixture_id="F23_CI_WAITING",
        description="Durable CI wait blocks launch and reports WAITING",
        snapshot=ScopeInputSnapshot(
            current_scope=AutoScope.MILESTONE,
            current_milestone_id="M1",
            current_task_id="T1",
            ci_waiting=True,
        ),
        expected_action=ScopeAction.WAIT_CI_WAITING,
        expected_state=CanonicalWorkState.WAITING,
    ),
    ScopeBoundaryFixture(
        fixture_id="F24_TRANSIENT_BACKOFF",
        description="Transient recovery backoff blocks launch and reports WAITING",
        snapshot=ScopeInputSnapshot(
            current_scope=AutoScope.TASK,
            current_milestone_id="M1",
            current_task_id="T1",
            transient_backoff=True,
        ),
        expected_action=ScopeAction.WAIT_TRANSIENT_BACKOFF,
        expected_state=CanonicalWorkState.WAITING,
    ),
    ScopeBoundaryFixture(
        fixture_id="F25_WAITING_FOR_PLAN",
        description="Approved plan exhausted halts and never fabricates tasks",
        snapshot=ScopeInputSnapshot(
            current_scope=AutoScope.PROJECT,
            current_milestone_id="M1",
            approved_plan_exhausted=True,
        ),
        expected_action=ScopeAction.HALT_WAITING_FOR_PLAN,
        expected_state=CanonicalWorkState.WAITING_FOR_PLAN,
        expected_terminal=True,
    ),
    ScopeBoundaryFixture(
        fixture_id="F26_NO_RUNNABLE_WORK",
        description="No runnable work halts without fabricating task",
        snapshot=ScopeInputSnapshot(
            current_scope=AutoScope.MILESTONE,
            current_milestone_id="M1",
            next_task_in_milestone_id=None,
            all_milestone_tasks_accepted=False,
        ),
        expected_action=ScopeAction.HALT_NO_RUNNABLE_WORK,
        expected_state=CanonicalWorkState.NO_RUNNABLE_WORK,
        expected_terminal=True,
    ),
)

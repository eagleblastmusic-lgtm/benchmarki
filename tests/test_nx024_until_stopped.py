"""NX-024: UNTIL_STOPPED semantics and approved-plan boundary.

The tests intentionally exercise only the bounded NX-024 surface.  They do
not create a continuation lease, send a browser message, or advance into
NX-026+ behavior.
"""

from __future__ import annotations

import ast
import sqlite3
import subprocess
from pathlib import Path

import pytest

from bdb_vnext.auto_scope_contract import AutoScope, CanonicalWorkState, ScopeAction
from bdb_vnext.scope_orchestrator import CanonicalPlanGraph, PlanMilestoneNode, PlanTaskNode
from bdb_vnext.until_stopped import (
    UNTIL_STOPPED_CONTRACT_VERSION_EXPLICIT as CONTRACT_VERSION_EXPLICIT,
    UNTIL_STOPPED_IMPLICITLY_ENABLED as IMPLICITLY_ENABLED,
    UntilStoppedController,
    UntilStoppedError,
)


def _plan_a() -> CanonicalPlanGraph:
    return CanonicalPlanGraph(
        plan_identity="plan:a",
        plan_version=1,
        milestones=(
            PlanMilestoneNode("M1", "G1", task_ids=("A1",)),
            PlanMilestoneNode("M2", "G2", dependencies=("M1",), task_ids=("A2",)),
            PlanMilestoneNode("M3", "G3", dependencies=("M2",), task_ids=("A3",)),
        ),
        tasks=(
            PlanTaskNode("A1", "M1"),
            PlanTaskNode("A2", "M2", dependencies=("A1",)),
            PlanTaskNode("A3", "M3", dependencies=("A2",)),
        ),
    )


def _plan_b() -> CanonicalPlanGraph:
    return CanonicalPlanGraph(
        plan_identity="plan:a",
        plan_version=2,
        milestones=(
            PlanMilestoneNode("M4", "G4", task_ids=("B1",)),
        ),
        tasks=(PlanTaskNode("B1", "M4"),),
    )


def _manual_plan() -> CanonicalPlanGraph:
    return CanonicalPlanGraph(
        plan_identity="plan:manual",
        plan_version=1,
        milestones=(PlanMilestoneNode("M1", "G1", task_ids=("M1-T1",)),),
        tasks=(PlanTaskNode("M1-T1", "M1", requires_manual_approval=True),),
    )


def _policy_plan() -> CanonicalPlanGraph:
    return CanonicalPlanGraph(
        plan_identity="plan:policy",
        plan_version=1,
        milestones=(PlanMilestoneNode("M1", "G1", task_ids=("P1",)),),
        tasks=(PlanTaskNode("P1", "M1", requires_policy_approval=True),),
    )


def _started(
    project_id: str,
    plan: CanonicalPlanGraph | None = None,
) -> tuple[sqlite3.Connection, UntilStoppedController, CanonicalPlanGraph]:
    connection = sqlite3.connect(":memory:")
    controller = UntilStoppedController(connection, project_id)
    selected_plan = plan or _plan_a()
    controller.start(
        selected_plan,
        run_id=f"run:{project_id}",
        explicit_scope=AutoScope.UNTIL_STOPPED,
    )
    return connection, controller, selected_plan


class TestExplicitUntilStoppedSelection:
    def test_until_stopped_requires_explicit_selection(self) -> None:
        connection = sqlite3.connect(":memory:")
        controller = UntilStoppedController(connection, "p-explicit")

        with pytest.raises(UntilStoppedError) as missing:
            controller.start(_plan_a(), run_id="r-missing")
        assert missing.value.code == "EXPLICIT_SCOPE_REQUIRED"

        with pytest.raises(UntilStoppedError) as wrong_scope:
            controller.start(
                _plan_a(),
                run_id="r-project",
                explicit_scope=AutoScope.PROJECT,
            )
        assert wrong_scope.value.code == "EXPLICIT_SCOPE_REQUIRED"

        cursor = controller.start(
            _plan_a(),
            run_id="r-explicit",
            explicit_scope=AutoScope.UNTIL_STOPPED,
        )
        assert cursor.scope == AutoScope.UNTIL_STOPPED
        assert cursor.scope_selection_explicit is True
        assert IMPLICITLY_ENABLED is False

    def test_stale_until_stopped_cursor_does_not_enable_scope(self) -> None:
        connection = sqlite3.connect(":memory:")
        orchestrator = UntilStoppedController(connection, "p-stale-cursor")
        orchestrator.orchestrator.get_or_create_cursor(
            "old-run",
            scope=AutoScope.UNTIL_STOPPED,
        )
        result = orchestrator.tick(_plan_a(), {}, {})
        assert result.next_action == ScopeAction.HALT_BLOCKED
        assert result.decision.reason_code == "EXPLICIT_SCOPE_REQUIRED"


class TestPlanExhaustion:
    def test_exhausted_approved_plan_is_waiting_for_plan_without_fabrication(self) -> None:
        connection, controller, plan = _started("p-exhausted")
        result = controller.tick(
            plan,
            {"A1": "ACCEPTED", "A2": "ACCEPTED", "A3": "ACCEPTED"},
            {"M1": "ACCEPTED", "M2": "ACCEPTED", "M3": "ACCEPTED"},
        )
        assert result.status == "WAITING_FOR_PLAN"
        assert result.decision.action == ScopeAction.HALT_WAITING_FOR_PLAN
        assert result.decision.canonical_work_state == CanonicalWorkState.WAITING_FOR_PLAN
        assert result.plan_exhausted is True
        assert result.synthetic_tasks_created == 0
        assert result.tasks_outside_approved_plan == 0
        assert result.cursor.disposition == "WAITING_FOR_PLAN"

        snapshot = controller.state_snapshot()
        assert snapshot["cursor"]["status"] == "WAITING_FOR_PLAN"
        assert snapshot["cursor"]["plan_identity"] == "plan:a"
        assert all(item["status"] == "APPROVED" for item in snapshot["plan_admissions"])


class TestSuccessorAdmission:
    def test_unapproved_successor_remains_candidate_and_cannot_launch(self) -> None:
        connection, controller, plan_a = _started("p-unapproved")
        controller.tick(
            plan_a,
            {"A1": "ACCEPTED", "A2": "ACCEPTED", "A3": "ACCEPTED"},
            {"M1": "ACCEPTED", "M2": "ACCEPTED", "M3": "ACCEPTED"},
        )
        candidate = controller.present_successor(_plan_b())
        assert candidate.success is True
        assert candidate.admission is not None
        assert candidate.admission.status == "CANDIDATE"

        # A candidate, a new version, and a prompt/UI-like plan argument never
        # override the durable WAITING_FOR_PLAN checkpoint.
        result = controller.tick(_plan_b(), {"B1": "NOT_STARTED"}, {"M4": "NOT_REACHED"})
        assert result.status == "WAITING_FOR_PLAN"
        assert result.decision.action == ScopeAction.HALT_WAITING_FOR_PLAN
        assert result.decision.selected_task_id is None
        assert controller.approved_plan() is not None
        assert controller.approved_plan().plan_version == 1

    def test_approved_successor_launches_exactly_first_legal_task(self) -> None:
        connection, controller, plan_a = _started("p-approved")
        controller.tick(
            plan_a,
            {"A1": "ACCEPTED", "A2": "ACCEPTED", "A3": "ACCEPTED"},
            {"M1": "ACCEPTED", "M2": "ACCEPTED", "M3": "ACCEPTED"},
        )
        candidate = controller.present_successor(_plan_b())
        assert candidate.admission is not None

        approved = controller.approve_successor(_plan_b())
        assert approved.success is True
        assert approved.reason_code == "SUCCESSOR_APPROVED"
        assert approved.admission is not None
        assert approved.admission.status == "APPROVED"

        first = controller.tick(_plan_b(), {"B1": "NOT_STARTED"}, {"M4": "NOT_REACHED"})
        assert first.status == "ACTIVE"
        assert first.decision.action == ScopeAction.LAUNCH_TASK
        assert first.decision.selected_task_id == "B1"
        assert first.decision.selected_milestone_id == "M4"
        assert first.tasks_outside_approved_plan == 0

    def test_new_plan_version_requires_canonical_approval(self) -> None:
        connection, controller, plan_a = _started("p-version")
        controller.tick(
            plan_a,
            {"A1": "ACCEPTED", "A2": "ACCEPTED", "A3": "ACCEPTED"},
            {"M1": "ACCEPTED", "M2": "ACCEPTED", "M3": "ACCEPTED"},
        )
        with pytest.raises(UntilStoppedError) as direct_start:
            controller.start(_plan_b(), run_id="run:version-b", explicit_scope=AutoScope.UNTIL_STOPPED)
        assert direct_start.value.code == "PLAN_IDENTITY_MISMATCH"
        assert controller.approved_plan() is not None
        assert controller.approved_plan().plan_version == 1


class TestStopBlockerAndPauseBoundaries:
    def test_stop_before_successor_approval_remains_stopped(self) -> None:
        connection, controller, plan_a = _started("p-stop-successor")
        controller.tick(
            plan_a,
            {"A1": "ACCEPTED", "A2": "ACCEPTED", "A3": "ACCEPTED"},
            {"M1": "ACCEPTED", "M2": "ACCEPTED", "M3": "ACCEPTED"},
        )
        controller.present_successor(_plan_b())
        controller.stop(expected_epoch=1, reason="manual stop")

        stopped = controller.tick(_plan_b(), {"B1": "NOT_STARTED"}, {"M4": "NOT_REACHED"})
        assert stopped.status == "STOPPED"
        assert stopped.decision.action == ScopeAction.STOP_EXTERNAL_STOP_REQUESTED
        assert stopped.decision.selected_task_id is None

        approval = controller.approve_successor(_plan_b())
        assert approval.success is False
        assert approval.reason_code == "STOP_FENCED"

    def test_real_blocker_does_not_skip_to_other_work(self) -> None:
        connection, controller, plan = _started("p-blocker")
        result = controller.tick(
            plan,
            {"A1": "BLOCKED", "A2": "NOT_STARTED", "A3": "NOT_STARTED"},
            {"M1": "NOT_REACHED"},
        )
        assert result.status == "BLOCKED"
        assert result.decision.action == ScopeAction.HALT_BLOCKED
        assert result.decision.reason_code == "REAL_BLOCKER"
        assert result.decision.selected_task_id == "A1"
        assert result.decision.selected_task_id != "A2"

    def test_manual_and_policy_pause_before_effect(self) -> None:
        connection, manual, manual_plan = _started("p-manual", _manual_plan())
        paused_manual = manual.tick(manual_plan, {"M1-T1": "NOT_STARTED"}, {"M1": "NOT_REACHED"})
        assert paused_manual.status == "PAUSED"
        assert paused_manual.decision.action == ScopeAction.PAUSE_MANUAL_GATE_REQUIRED
        resumed_manual = manual.tick(
            manual_plan,
            {"M1-T1": "NOT_STARTED"},
            {"M1": "NOT_REACHED"},
            manual_approvals={"M1-T1": True},
        )
        assert resumed_manual.decision.action == ScopeAction.LAUNCH_TASK

        connection2, policy, policy_plan = _started("p-policy", _policy_plan())
        paused_policy = policy.tick(policy_plan, {"P1": "NOT_STARTED"}, {"M1": "NOT_REACHED"})
        assert paused_policy.status == "PAUSED"
        assert paused_policy.decision.action == ScopeAction.PAUSE_POLICY_APPROVAL_REQUIRED
        resumed_policy = policy.tick(
            policy_plan,
            {"P1": "NOT_STARTED"},
            {"M1": "NOT_REACHED"},
            policy_approvals={"P1": True},
        )
        assert resumed_policy.decision.action == ScopeAction.LAUNCH_TASK


class TestRestartPersistence:
    def test_restart_preserves_scope_identity_and_active_cursor(self, tmp_path: Path) -> None:
        db_path = tmp_path / "memory.db"
        plan = _plan_a()
        connection = sqlite3.connect(str(db_path))
        first = UntilStoppedController(connection, "p-restart-active")
        started = first.start(plan, run_id="run:restart", explicit_scope=AutoScope.UNTIL_STOPPED)
        launch = first.tick(plan, {"A1": "NOT_STARTED"}, {"M1": "NOT_REACHED"})
        assert launch.decision.action == ScopeAction.LAUNCH_TASK
        connection.close()

        restarted_connection = sqlite3.connect(str(db_path))
        restarted = UntilStoppedController(restarted_connection, "p-restart-active")
        cursor = restarted.get_cursor()
        assert cursor is not None
        assert cursor.scope == started.scope
        assert cursor.scope_selection_explicit is True
        assert cursor.run_id == "run:restart"
        assert cursor.plan_identity == "plan:a"
        assert cursor.plan_version == 1
        assert cursor.current_task_id == "A1"

    def test_restart_in_waiting_for_plan_does_not_resume_old_plan(self, tmp_path: Path) -> None:
        db_path = tmp_path / "memory.db"
        plan = _plan_a()
        connection = sqlite3.connect(str(db_path))
        first = UntilStoppedController(connection, "p-restart-waiting")
        first.start(plan, run_id="run:restart-wait", explicit_scope=AutoScope.UNTIL_STOPPED)
        waiting = first.tick(
            plan,
            {"A1": "ACCEPTED", "A2": "ACCEPTED", "A3": "ACCEPTED"},
            {"M1": "ACCEPTED", "M2": "ACCEPTED", "M3": "ACCEPTED"},
        )
        assert waiting.status == "WAITING_FOR_PLAN"
        connection.close()

        restarted_connection = sqlite3.connect(str(db_path))
        restarted = UntilStoppedController(restarted_connection, "p-restart-waiting")
        result = restarted.tick(plan, {}, {})
        assert result.status == "WAITING_FOR_PLAN"
        assert result.decision.action == ScopeAction.HALT_WAITING_FOR_PLAN


def _source_readback(repo_root: Path) -> tuple[str, str, bool]:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, text=True, check=True
    ).stdout.strip()
    tree = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=repo_root, capture_output=True, text=True, check=True
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo_root, capture_output=True, text=True, check=True
    ).stdout
    unstaged = subprocess.run(["git", "diff", "--quiet"], cwd=repo_root)
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo_root)
    return head, tree, not status.strip() and unstaged.returncode == 0 and staged.returncode == 0


def inspect_nx024_gate_for_hardcoded_results() -> tuple[bool, list[str]]:
    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    gate = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "run_nx024_machine_gate"
        ),
        None,
    )
    if gate is None:
        return False, ["run_nx024_machine_gate_missing"]
    fields = {
        "UNTIL_STOPPED_CONTRACT_VERSION_EXPLICIT",
        "UNTIL_STOPPED_IMPLICITLY_ENABLED",
        "TASKS_OUTSIDE_APPROVED_PLAN_EXECUTED",
        "SYNTHETIC_TASKS_CREATED",
        "PLAN_EXHAUSTED_DISPOSITION",
        "PLAN_EXHAUSTED_SYNTHETIC_TASKS",
        "UNAPPROVED_SUCCESSOR_ADMITTED",
        "NEW_PLAN_VERSION_AUTO_APPROVED",
        "SUCCESSOR_EFFECTS_BEFORE_APPROVAL",
        "APPROVED_SUCCESSOR_FIRST_TASK_LAUNCHED",
        "UNTIL_STOPPED_BYPASSES_STOP_FENCE",
        "SUCCESSOR_AFTER_STOP_AUTO_RESUMES",
        "REAL_BLOCKER_SKIPPED_TO_OTHER_WORK",
        "POLICY_PAUSE_EFFECTS",
        "MANUAL_PAUSE_EFFECTS",
        "RESTART_SCOPE_DIVERGENCES",
        "RESTART_WAITING_FOR_PLAN_DIVERGENCES",
        "APPROVED_PLAN_TRACE_STEPS",
        "OBSERVED_TRACE_STEPS",
        "TRACE_DIVERGENCES",
        "UNAPPROVED_PLAN_TRACE_TASKS",
        "NO_HARDCODED_GATE_RESULTS",
        "SOURCE_BOUND_MACHINE_GATE",
        "NX024_STATUS",
    }
    hardcoded: list[str] = []
    for node in ast.walk(gate):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in fields:
                if isinstance(node.value, ast.Constant) and node.value.value in {
                    True,
                    False,
                    0,
                    1,
                    "PASS",
                    "FAIL",
                    "WAITING_FOR_PLAN",
                }:
                    hardcoded.append(target.id)
    return not hardcoded, hardcoded


def run_nx024_machine_gate() -> dict[str, object]:
    """Derive the NX-024 gate from executable traces and source readback."""
    plan_a = _plan_a()
    plan_b = _plan_b()
    connection = sqlite3.connect(":memory:")
    controller = UntilStoppedController(connection, "p-gate-024")
    controller.start(plan_a, run_id="run:gate-024", explicit_scope=AutoScope.UNTIL_STOPPED)

    expected_trace = ["A1", "A2", "A3", "WAITING_FOR_PLAN", "CANDIDATE", "B1"]
    observed_trace: list[str] = []
    launched_plan_pairs: list[tuple[str, int, str]] = []

    first = controller.tick(plan_a, {"A1": "NOT_STARTED"}, {"M1": "NOT_REACHED"})
    observed_trace.append(first.decision.selected_task_id or first.status)
    if first.decision.selected_task_id:
        launched_plan_pairs.append((plan_a.plan_identity, plan_a.plan_version, first.decision.selected_task_id))

    second = controller.tick(
        plan_a,
        {"A1": "ACCEPTED", "A2": "NOT_STARTED"},
        {"M1": "ACCEPTED", "M2": "NOT_REACHED"},
    )
    observed_trace.append(second.decision.selected_task_id or second.status)
    if second.decision.selected_task_id:
        launched_plan_pairs.append((plan_a.plan_identity, plan_a.plan_version, second.decision.selected_task_id))

    third = controller.tick(
        plan_a,
        {"A1": "ACCEPTED", "A2": "ACCEPTED", "A3": "NOT_STARTED"},
        {"M1": "ACCEPTED", "M2": "ACCEPTED", "M3": "NOT_REACHED"},
    )
    observed_trace.append(third.decision.selected_task_id or third.status)
    if third.decision.selected_task_id:
        launched_plan_pairs.append((plan_a.plan_identity, plan_a.plan_version, third.decision.selected_task_id))

    exhausted = controller.tick(
        plan_a,
        {"A1": "ACCEPTED", "A2": "ACCEPTED", "A3": "ACCEPTED"},
        {"M1": "ACCEPTED", "M2": "ACCEPTED", "M3": "ACCEPTED"},
    )
    observed_trace.append(exhausted.status)

    candidate = controller.present_successor(plan_b)
    observed_trace.append(candidate.admission.status if candidate.admission else candidate.reason_code)
    pre_approval = controller.tick(plan_b, {"B1": "NOT_STARTED"}, {"M4": "NOT_REACHED"})
    unapproved_trace_task_count = int(pre_approval.decision.action == ScopeAction.LAUNCH_TASK)
    if pre_approval.decision.selected_task_id:
        launched_plan_pairs.append((plan_b.plan_identity, plan_b.plan_version, pre_approval.decision.selected_task_id))

    successor_before = int(
        candidate.admission is not None and candidate.admission.status == "APPROVED"
    )
    version_before = controller.approved_plan()
    new_version_auto_approved = int(version_before is not None and version_before.plan_version != 1)
    approval = controller.approve_successor(plan_b)
    post_approval = controller.tick(plan_b, {"B1": "NOT_STARTED"}, {"M4": "NOT_REACHED"})
    observed_trace.append(post_approval.decision.selected_task_id or post_approval.status)
    if post_approval.decision.selected_task_id:
        launched_plan_pairs.append((plan_b.plan_identity, plan_b.plan_version, post_approval.decision.selected_task_id))

    stopped_controller = UntilStoppedController(sqlite3.connect(":memory:"), "p-gate-stop")
    stopped_controller.start(plan_a, run_id="run:gate-stop", explicit_scope=AutoScope.UNTIL_STOPPED)
    stopped_controller.tick(plan_a, {"A1": "ACCEPTED", "A2": "ACCEPTED", "A3": "ACCEPTED"}, {"M1": "ACCEPTED", "M2": "ACCEPTED", "M3": "ACCEPTED"})
    stopped_controller.present_successor(plan_b)
    stopped_controller.stop(expected_epoch=1, reason="gate stop")
    stopped_result = stopped_controller.tick(plan_b, {"B1": "NOT_STARTED"}, {"M4": "NOT_REACHED"})

    blocker_connection, blocker, blocker_plan = _started("p-gate-blocker")
    blocker_result = blocker.tick(
        blocker_plan,
        {"A1": "BLOCKED", "A2": "NOT_STARTED", "A3": "NOT_STARTED"},
        {"M1": "NOT_REACHED"},
    )
    manual_connection, manual, manual_plan = _started("p-gate-manual", _manual_plan())
    manual_result = manual.tick(manual_plan, {"M1-T1": "NOT_STARTED"}, {"M1": "NOT_REACHED"})
    policy_connection, policy, policy_plan = _started("p-gate-policy", _policy_plan())
    policy_result = policy.tick(policy_plan, {"P1": "NOT_STARTED"}, {"M1": "NOT_REACHED"})

    restart_path = Path(__file__).resolve().parent / ".nx024-gate-memory.db"
    restart_sidecars = tuple(
        Path(f"{restart_path}{suffix}") for suffix in ("", "-wal", "-shm")
    )
    for path in restart_sidecars:
        path.unlink(missing_ok=True)
    restart_connection: sqlite3.Connection | None = None
    restart_connection_2: sqlite3.Connection | None = None
    try:
        restart_connection = sqlite3.connect(str(restart_path))
        restart = UntilStoppedController(restart_connection, "p-gate-restart")
        restart.start(plan_a, run_id="run:gate-restart", explicit_scope=AutoScope.UNTIL_STOPPED)
        restart.tick(plan_a, {"A1": "ACCEPTED", "A2": "ACCEPTED", "A3": "ACCEPTED"}, {"M1": "ACCEPTED", "M2": "ACCEPTED", "M3": "ACCEPTED"})
        waiting_cursor = restart.get_cursor()
        restart_connection.close()
        restart_connection = None
        restart_connection_2 = sqlite3.connect(str(restart_path))
        restart_2 = UntilStoppedController(restart_connection_2, "p-gate-restart")
        waiting_after_restart = restart_2.tick(plan_a, {}, {})
        restart_scope_divergences = int(
            waiting_cursor is None
            or waiting_after_restart.cursor.scope != waiting_cursor.scope
            or waiting_after_restart.cursor.plan_identity != waiting_cursor.plan_identity
        )
        restart_waiting_divergences = int(waiting_after_restart.status != "WAITING_FOR_PLAN")
    finally:
        if restart_connection is not None:
            restart_connection.close()
        if restart_connection_2 is not None:
            restart_connection_2.close()
        for path in restart_sidecars:
            path.unlink(missing_ok=True)

    head, tree, clean = _source_readback(Path(__file__).resolve().parent.parent)
    no_hardcoded, hardcoded = inspect_nx024_gate_for_hardcoded_results()

    UNTIL_STOPPED_CONTRACT_VERSION_EXPLICIT = bool(CONTRACT_VERSION_EXPLICIT)
    UNTIL_STOPPED_IMPLICITLY_ENABLED = bool(IMPLICITLY_ENABLED)
    approved_plan_task_pairs = {
        (plan_a.plan_identity, plan_a.plan_version, task.task_id) for task in plan_a.tasks
    } | {
        (plan_b.plan_identity, plan_b.plan_version, task.task_id) for task in plan_b.tasks
    }
    TASKS_OUTSIDE_APPROVED_PLAN_EXECUTED = len(
        [item for item in launched_plan_pairs if item not in approved_plan_task_pairs]
    )
    SYNTHETIC_TASKS_CREATED = len(
        [item for item in launched_plan_pairs if item[2] not in {task.task_id for task in plan_a.tasks + plan_b.tasks}]
    )
    PLAN_EXHAUSTED_DISPOSITION = exhausted.status
    PLAN_EXHAUSTED_SYNTHETIC_TASKS = exhausted.synthetic_tasks_created
    UNAPPROVED_SUCCESSOR_ADMITTED = int(
        candidate.admission is not None and candidate.admission.status == "APPROVED"
    )
    NEW_PLAN_VERSION_AUTO_APPROVED = new_version_auto_approved
    SUCCESSOR_EFFECTS_BEFORE_APPROVAL = int(pre_approval.decision.action == ScopeAction.LAUNCH_TASK)
    APPROVED_SUCCESSOR_FIRST_TASK_LAUNCHED = int(
        approval.success and post_approval.decision.action == ScopeAction.LAUNCH_TASK and post_approval.decision.selected_task_id == "B1"
    )
    UNTIL_STOPPED_BYPASSES_STOP_FENCE = int(stopped_result.decision.action == ScopeAction.LAUNCH_TASK)
    SUCCESSOR_AFTER_STOP_AUTO_RESUMES = int(stopped_result.status != "STOPPED")
    REAL_BLOCKER_SKIPPED_TO_OTHER_WORK = int(blocker_result.decision.selected_task_id != "A1")
    POLICY_PAUSE_EFFECTS = int(policy_result.decision.action == ScopeAction.LAUNCH_TASK)
    MANUAL_PAUSE_EFFECTS = int(manual_result.decision.action == ScopeAction.LAUNCH_TASK)
    APPROVED_PLAN_TRACE_STEPS = len(expected_trace)
    OBSERVED_TRACE_STEPS = len(observed_trace)
    TRACE_DIVERGENCES = len([pair for pair in zip(expected_trace, observed_trace) if pair[0] != pair[1]]) + abs(APPROVED_PLAN_TRACE_STEPS - OBSERVED_TRACE_STEPS)
    UNAPPROVED_PLAN_TRACE_TASKS = unapproved_trace_task_count
    RESTART_SCOPE_DIVERGENCES = restart_scope_divergences
    RESTART_WAITING_FOR_PLAN_DIVERGENCES = restart_waiting_divergences
    NO_HARDCODED_GATE_RESULTS = no_hardcoded
    SOURCE_BOUND_MACHINE_GATE = "PASS" if len(head) == 40 and len(tree) == 40 and clean else "FAIL"
    all_pass = (
        UNTIL_STOPPED_CONTRACT_VERSION_EXPLICIT
        and not UNTIL_STOPPED_IMPLICITLY_ENABLED
        and TASKS_OUTSIDE_APPROVED_PLAN_EXECUTED == 0
        and SYNTHETIC_TASKS_CREATED == 0
        and PLAN_EXHAUSTED_DISPOSITION == "WAITING_FOR_PLAN"
        and PLAN_EXHAUSTED_SYNTHETIC_TASKS == 0
        and UNAPPROVED_SUCCESSOR_ADMITTED == 0
        and NEW_PLAN_VERSION_AUTO_APPROVED == 0
        and SUCCESSOR_EFFECTS_BEFORE_APPROVAL == 0
        and APPROVED_SUCCESSOR_FIRST_TASK_LAUNCHED == 1
        and UNTIL_STOPPED_BYPASSES_STOP_FENCE == 0
        and SUCCESSOR_AFTER_STOP_AUTO_RESUMES == 0
        and REAL_BLOCKER_SKIPPED_TO_OTHER_WORK == 0
        and POLICY_PAUSE_EFFECTS == 0
        and MANUAL_PAUSE_EFFECTS == 0
        and RESTART_SCOPE_DIVERGENCES == 0
        and RESTART_WAITING_FOR_PLAN_DIVERGENCES == 0
        and TRACE_DIVERGENCES == 0
        and UNAPPROVED_PLAN_TRACE_TASKS == 0
        and NO_HARDCODED_GATE_RESULTS
        and SOURCE_BOUND_MACHINE_GATE == "PASS"
    )
    return {
        "UNTIL_STOPPED_CONTRACT_VERSION_EXPLICIT": UNTIL_STOPPED_CONTRACT_VERSION_EXPLICIT,
        "UNTIL_STOPPED_IMPLICITLY_ENABLED": UNTIL_STOPPED_IMPLICITLY_ENABLED,
        "TASKS_OUTSIDE_APPROVED_PLAN_EXECUTED": TASKS_OUTSIDE_APPROVED_PLAN_EXECUTED,
        "SYNTHETIC_TASKS_CREATED": SYNTHETIC_TASKS_CREATED,
        "PLAN_EXHAUSTED_DISPOSITION": PLAN_EXHAUSTED_DISPOSITION,
        "PLAN_EXHAUSTED_SYNTHETIC_TASKS": PLAN_EXHAUSTED_SYNTHETIC_TASKS,
        "UNAPPROVED_SUCCESSOR_ADMITTED": UNAPPROVED_SUCCESSOR_ADMITTED,
        "NEW_PLAN_VERSION_AUTO_APPROVED": NEW_PLAN_VERSION_AUTO_APPROVED,
        "SUCCESSOR_EFFECTS_BEFORE_APPROVAL": SUCCESSOR_EFFECTS_BEFORE_APPROVAL,
        "APPROVED_SUCCESSOR_FIRST_TASK_LAUNCHED": APPROVED_SUCCESSOR_FIRST_TASK_LAUNCHED,
        "UNTIL_STOPPED_BYPASSES_STOP_FENCE": UNTIL_STOPPED_BYPASSES_STOP_FENCE,
        "SUCCESSOR_AFTER_STOP_AUTO_RESUMES": SUCCESSOR_AFTER_STOP_AUTO_RESUMES,
        "REAL_BLOCKER_SKIPPED_TO_OTHER_WORK": REAL_BLOCKER_SKIPPED_TO_OTHER_WORK,
        "POLICY_PAUSE_EFFECTS": POLICY_PAUSE_EFFECTS,
        "MANUAL_PAUSE_EFFECTS": MANUAL_PAUSE_EFFECTS,
        "RESTART_SCOPE_DIVERGENCES": RESTART_SCOPE_DIVERGENCES,
        "RESTART_WAITING_FOR_PLAN_DIVERGENCES": RESTART_WAITING_FOR_PLAN_DIVERGENCES,
        "APPROVED_PLAN_TRACE_STEPS": APPROVED_PLAN_TRACE_STEPS,
        "OBSERVED_TRACE_STEPS": OBSERVED_TRACE_STEPS,
        "TRACE_DIVERGENCES": TRACE_DIVERGENCES,
        "UNAPPROVED_PLAN_TRACE_TASKS": UNAPPROVED_PLAN_TRACE_TASKS,
        "HARDCODED_GATE_RESULT_FIELDS": hardcoded,
        "NO_HARDCODED_GATE_RESULTS": NO_HARDCODED_GATE_RESULTS,
        "SOURCE_HEAD": head,
        "SOURCE_TREE": tree,
        "WORKTREE_CLEAN": clean,
        "SOURCE_BOUND_MACHINE_GATE": SOURCE_BOUND_MACHINE_GATE,
        "NX024_STATUS": "PASS" if all_pass else "FAIL",
    }


def test_nx024_machine_gate_execution() -> None:
    gate = run_nx024_machine_gate()
    assert gate["UNTIL_STOPPED_CONTRACT_VERSION_EXPLICIT"] is True
    assert gate["UNTIL_STOPPED_IMPLICITLY_ENABLED"] is False
    assert gate["TASKS_OUTSIDE_APPROVED_PLAN_EXECUTED"] == 0
    assert gate["SYNTHETIC_TASKS_CREATED"] == 0
    assert gate["PLAN_EXHAUSTED_DISPOSITION"] == "WAITING_FOR_PLAN"
    assert gate["PLAN_EXHAUSTED_SYNTHETIC_TASKS"] == 0
    assert gate["UNAPPROVED_SUCCESSOR_ADMITTED"] == 0
    assert gate["NEW_PLAN_VERSION_AUTO_APPROVED"] == 0
    assert gate["SUCCESSOR_EFFECTS_BEFORE_APPROVAL"] == 0
    assert gate["APPROVED_SUCCESSOR_FIRST_TASK_LAUNCHED"] == 1
    assert gate["UNTIL_STOPPED_BYPASSES_STOP_FENCE"] == 0
    assert gate["SUCCESSOR_AFTER_STOP_AUTO_RESUMES"] == 0
    assert gate["REAL_BLOCKER_SKIPPED_TO_OTHER_WORK"] == 0
    assert gate["POLICY_PAUSE_EFFECTS"] == 0
    assert gate["MANUAL_PAUSE_EFFECTS"] == 0
    assert gate["RESTART_SCOPE_DIVERGENCES"] == 0
    assert gate["RESTART_WAITING_FOR_PLAN_DIVERGENCES"] == 0
    assert gate["TRACE_DIVERGENCES"] == 0
    assert gate["UNAPPROVED_PLAN_TRACE_TASKS"] == 0
    assert gate["HARDCODED_GATE_RESULT_FIELDS"] == []
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    assert gate["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
    assert gate["NX024_STATUS"] == "PASS"

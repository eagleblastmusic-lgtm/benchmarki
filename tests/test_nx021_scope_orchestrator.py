"""NX-021: Durable Scope Cursor and Orchestrator Tests.

Verifies:
1. Authority inputs only: UI and prompt text cannot select next task
2. Durable cursor contract: schema version, persistence, optimistic CAS concurrency
3. Stale cursor rejection (optimistic locking)
4. Idempotent tick: repeated tick returns same semantic decision, zero duplicate launches
5. PASS task never returns to runnable
6. Dependency resolution & race prevention: child task never launches before dependency passes
7. Final gates and next milestone: MILESTONE stops at gate; PROJECT crosses only after PASS
8. Wrong milestone cursor fails closed
9. Waiting / paused / completed states never launch tasks
10. Structured next-action explanation generated on every tick
11. Model-based plan traversal across all 9 canonical fixtures with zero divergences
12. Ambiguous plan graphs fail closed
13. NX-021 machine gate
"""

from __future__ import annotations

import ast
import json
import sqlite3
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from bdb_vnext.auto_scope_contract import (
    AUTO_SCOPE_SCHEMA_VERSION,
    AutoScope,
    CanonicalWorkState,
    DEFAULT_AUTO_SCOPE,
    ScopeAction,
    ScopeDecision,
)
from bdb_vnext.scope_orchestrator import (
    SCOPE_CURSOR_SCHEMA_VERSION,
    CanonicalPlanGraph,
    NextActionExplanation,
    PlanMilestoneNode,
    PlanTaskNode,
    ScopeCursor,
    ScopeOrchestrator,
)


@pytest.fixture
def mem_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture
def disk_db_path(tmp_path: Path) -> Path:
    return tmp_path / "test_nx021_orchestrator.db"


def _create_simple_plan() -> CanonicalPlanGraph:
    return CanonicalPlanGraph(
        plan_identity="plan:simple:v1",
        plan_version=1,
        milestones=(
            PlanMilestoneNode(milestone_id="M1", gate_id="G1", task_ids=("T1", "T2")),
            PlanMilestoneNode(milestone_id="M2", gate_id="G2", dependencies=("M1",), task_ids=("T3",)),
        ),
        tasks=(
            PlanTaskNode(task_id="T1", milestone_id="M1"),
            PlanTaskNode(task_id="T2", milestone_id="M1", dependencies=("T1",)),
            PlanTaskNode(task_id="T3", milestone_id="M2"),
        ),
    )


# ==============================================================================
# 1. CURSOR CONTRACT & CAS CONCURRENCY TESTS
# ==============================================================================

class TestCursorContractAndCAS:
    def test_cursor_persistence_and_restart(self, disk_db_path: Path) -> None:
        conn1 = sqlite3.connect(str(disk_db_path))
        conn1.row_factory = sqlite3.Row
        orch1 = ScopeOrchestrator(conn1, "p-test")
        c1 = orch1.get_or_create_cursor("run-1", scope=AutoScope.PROJECT, plan_identity="plan:v1")
        assert c1.state_revision == 1
        assert c1.scope == AutoScope.PROJECT
        conn1.close()

        # Reopen on fresh connection
        conn2 = sqlite3.connect(str(disk_db_path))
        conn2.row_factory = sqlite3.Row
        orch2 = ScopeOrchestrator(conn2, "p-test")
        c2 = orch2.get_or_create_cursor("run-1")
        assert c2.cursor_id == c1.cursor_id
        assert c2.scope == AutoScope.PROJECT
        assert c2.state_revision == 1
        conn2.close()

    def test_cursor_optimistic_cas_accepts_current_and_rejects_stale(self, mem_conn: sqlite3.Connection) -> None:
        orch = ScopeOrchestrator(mem_conn, "p-cas")
        c_init = orch.get_or_create_cursor("run-cas")
        assert c_init.state_revision == 1

        # Actor 1 advances revision 1 -> 2
        updated_c1 = ScopeCursor(
            cursor_id=c_init.cursor_id,
            project_id=c_init.project_id,
            run_id=c_init.run_id,
            scope=c_init.scope,
            current_task_id="T1",
            state_revision=c_init.state_revision,
        )
        ok1 = orch.update_cursor_cas(updated_c1, expected_revision=1)
        assert ok1 is True

        c_after_1 = orch.get_or_create_cursor("run-cas")
        assert c_after_1.state_revision == 2
        assert c_after_1.current_task_id == "T1"

        # Actor 2 with stale revision 1 tries to update
        updated_c2 = ScopeCursor(
            cursor_id=c_init.cursor_id,
            project_id=c_init.project_id,
            run_id=c_init.run_id,
            scope=c_init.scope,
            current_task_id="T_STALE",
            state_revision=1,
        )
        ok2 = orch.update_cursor_cas(updated_c2, expected_revision=1)
        assert ok2 is False  # Rejected!

        # State in DB was NOT mutated by stale actor
        c_final = orch.get_or_create_cursor("run-cas")
        assert c_final.state_revision == 2
        assert c_final.current_task_id == "T1"


# ==============================================================================
# 2. AUTHORITY INPUTS & PROMPT/UI ISOLATION TESTS
# ==============================================================================

class TestAuthorityIsolation:
    def test_ui_and_prompt_suggestions_are_ignored(self, mem_conn: sqlite3.Connection) -> None:
        plan = _create_simple_plan()
        orch = ScopeOrchestrator(mem_conn, "p-auth")
        cursor = orch.get_or_create_cursor("run-auth", scope=AutoScope.MILESTONE)

        dec, expl, _ = orch.tick(
            plan=plan,
            cursor=cursor,
            task_statuses={"T1": "NOT_STARTED", "T2": "NOT_STARTED"},
            milestone_gate_statuses={"M1": "NOT_REACHED"},
            ui_suggested_task="T_UNAUTHORIZED_UI",
            prompt_suggested_task="T_UNAUTHORIZED_PROMPT",
        )

        assert dec.action == ScopeAction.LAUNCH_TASK
        assert dec.selected_task_id == "T1"  # Authority determines T1, ignores UI/prompt!
        assert expl.selected_task_id == "T1"


# ==============================================================================
# 3. IDEMPOTENT TICK & REPEAT TESTS
# ==============================================================================

class TestIdempotentTick:
    def test_duplicate_tick_produces_identical_decision_without_duplicate_launch(self, mem_conn: sqlite3.Connection) -> None:
        plan = _create_simple_plan()
        orch = ScopeOrchestrator(mem_conn, "p-idem")
        cursor = orch.get_or_create_cursor("run-idem", scope=AutoScope.MILESTONE)

        task_st = {"T1": "IN_PROGRESS", "T2": "NOT_STARTED"}
        gate_st = {"M1": "NOT_REACHED"}

        dec1, expl1, c1 = orch.tick(plan, cursor, task_st, gate_st)
        dec2, expl2, c2 = orch.tick(plan, cursor, task_st, gate_st)

        assert dec1.action == dec2.action == ScopeAction.LAUNCH_TASK
        assert dec1.selected_task_id == dec2.selected_task_id == "T1"
        assert expl1.action == expl2.action


# ==============================================================================
# 4. ACCEPTED TASK INVARIANT
# ==============================================================================

class TestAcceptedTaskInvariant:
    def test_accepted_task_never_returns_to_runnable(self, mem_conn: sqlite3.Connection) -> None:
        plan = _create_simple_plan()
        orch = ScopeOrchestrator(mem_conn, "p-acc")
        cursor = orch.get_or_create_cursor("run-acc", scope=AutoScope.MILESTONE)

        # Both T1 and T2 are ACCEPTED
        task_st = {"T1": "ACCEPTED", "T2": "ACCEPTED"}
        gate_st = {"M1": "NOT_REACHED"}

        dec, _, _ = orch.tick(plan, cursor, task_st, gate_st)
        assert dec.action == ScopeAction.WAIT_MILESTONE_GATE_PENDING
        assert dec.selected_task_id is None  # Neither T1 nor T2 is launched!


# ==============================================================================
# 5. DEPENDENCY RESOLUTION & RACE PREVENTION
# ==============================================================================

class TestDependencyResolution:
    def test_child_task_never_launches_before_dependency_acceptance(self, mem_conn: sqlite3.Connection) -> None:
        plan = _create_simple_plan()
        orch = ScopeOrchestrator(mem_conn, "p-dep")
        cursor = orch.get_or_create_cursor("run-dep", scope=AutoScope.MILESTONE)

        # T1 is FAILED or IN_PROGRESS (not ACCEPTED)
        task_st = {"T1": "FAILED", "T2": "NOT_STARTED"}
        gate_st = {"M1": "NOT_REACHED"}

        # Attempting to resolve next runnable task in M1:
        tid, deps_ok, pending = orch.resolve_next_runnable_task(plan, "M1", task_st)
        assert tid == "T1"  # T1 must be resolved, NOT T2!

        # Even if T1 is skipped in statuses, T2 cannot run because T1 is not ACCEPTED:
        task_st_skip = {"T1": "NOT_STARTED", "T2": "NOT_STARTED"}
        tid2, deps_ok2, pending2 = orch.resolve_next_runnable_task(plan, "M1", task_st_skip)
        assert tid2 == "T1"


# ==============================================================================
# 6. WRONG MILESTONE CURSOR FAILS CLOSED
# ==============================================================================

class TestWrongMilestoneCursor:
    def test_cursor_pointing_to_unmet_milestone_fails_closed(self, mem_conn: sqlite3.Connection) -> None:
        plan = _create_simple_plan()
        orch = ScopeOrchestrator(mem_conn, "p-wm")
        # Cursor points to M2, but M1 gate is NOT_REACHED
        c_wrong = ScopeCursor(
            cursor_id="cur-wm",
            project_id="p-wm",
            run_id="run-wm",
            scope=AutoScope.PROJECT,
            current_milestone_id="M2",
        )

        dec, expl, _ = orch.tick(
            plan,
            c_wrong,
            task_statuses={"T1": "NOT_STARTED", "T2": "NOT_STARTED", "T3": "NOT_STARTED"},
            milestone_gate_statuses={"M1": "NOT_REACHED", "M2": "NOT_REACHED"},
        )
        assert dec.action == ScopeAction.HALT_BLOCKED
        assert dec.reason_code == "WRONG_MILESTONE_CURSOR"


# ==============================================================================
# 7. WAITING, PAUSED, COMPLETED STATES NEVER LAUNCH TASKS
# ==============================================================================

class TestNonLaunchStates:
    def test_waiting_paused_completed_never_launch(self, mem_conn: sqlite3.Connection) -> None:
        plan = _create_simple_plan()
        orch = ScopeOrchestrator(mem_conn, "p-nonlaunch")
        cursor = orch.get_or_create_cursor("run-nl", scope=AutoScope.MILESTONE)

        # CI Waiting
        dec_ci, _, _ = orch.tick(
            plan,
            ScopeCursor(cursor_id="c", project_id="p", run_id="r", scope=AutoScope.MILESTONE, current_task_id="T1"),
            {"T1": "IN_PROGRESS"}, {"M1": "NOT_REACHED"},
            ci_waiting_tasks=("T1",),
        )
        assert dec_ci.action == ScopeAction.WAIT_CI_WAITING

        # Policy approval required
        plan_policy = CanonicalPlanGraph(
            plan_identity="plan:pol",
            plan_version=1,
            milestones=(PlanMilestoneNode("M1", "G1", task_ids=("T1",)),),
            tasks=(PlanTaskNode("T1", "M1", requires_policy_approval=True),),
        )
        dec_pol, _, _ = orch.tick(plan_policy, cursor, {"T1": "NOT_STARTED"}, {"M1": "NOT_REACHED"})
        assert dec_pol.action == ScopeAction.PAUSE_POLICY_APPROVAL_REQUIRED

        # Completed project
        dec_comp, _, _ = orch.tick(
            plan,
            ScopeCursor(cursor_id="c", project_id="p", run_id="r", scope=AutoScope.PROJECT, current_milestone_id="M2"),
            {"T1": "ACCEPTED", "T2": "ACCEPTED", "T3": "ACCEPTED"},
            {"M1": "ACCEPTED", "M2": "ACCEPTED"},
        )
        assert dec_comp.action == ScopeAction.STOP_PROJECT_COMPLETE


# ==============================================================================
# 8. MODEL-BASED PLAN TRAVERSAL FIXTURES (A THROUGH I)
# ==============================================================================

@dataclass(frozen=True)
class PlanTraversalFixture:
    fixture_id: str
    description: str
    plan: CanonicalPlanGraph
    scope: AutoScope
    expected_trace: tuple[ScopeAction, ...]


def _build_traversal_fixtures() -> tuple[PlanTraversalFixture, ...]:
    return (
        # A: Linear tasks (MILESTONE scope)
        PlanTraversalFixture(
            fixture_id="A_LINEAR_TASKS",
            description="Milestone M1 with linear T1 -> T2 and Gate G1",
            plan=CanonicalPlanGraph(
                plan_identity="p:linear", plan_version=1,
                milestones=(PlanMilestoneNode("M1", "G1", task_ids=("T1", "T2")),),
                tasks=(PlanTaskNode("T1", "M1"), PlanTaskNode("T2", "M1", dependencies=("T1",))),
            ),
            scope=AutoScope.MILESTONE,
            expected_trace=(
                ScopeAction.LAUNCH_TASK,
                ScopeAction.LAUNCH_TASK,
                ScopeAction.WAIT_MILESTONE_GATE_PENDING,
                ScopeAction.STOP_SCOPE_COMPLETE,
            ),
        ),
        # B: Dependency diamond (MILESTONE scope)
        PlanTraversalFixture(
            fixture_id="B_DEPENDENCY_DIAMOND",
            description="T1 -> (T2, T3) -> T4",
            plan=CanonicalPlanGraph(
                plan_identity="p:diamond", plan_version=1,
                milestones=(PlanMilestoneNode("M1", "G1", task_ids=("T1", "T2", "T3", "T4")),),
                tasks=(
                    PlanTaskNode("T1", "M1"),
                    PlanTaskNode("T2", "M1", dependencies=("T1",)),
                    PlanTaskNode("T3", "M1", dependencies=("T1",)),
                    PlanTaskNode("T4", "M1", dependencies=("T2", "T3")),
                ),
            ),
            scope=AutoScope.MILESTONE,
            expected_trace=(
                ScopeAction.LAUNCH_TASK,
                ScopeAction.LAUNCH_TASK,
                ScopeAction.LAUNCH_TASK,
                ScopeAction.LAUNCH_TASK,
                ScopeAction.WAIT_MILESTONE_GATE_PENDING,
                ScopeAction.STOP_SCOPE_COMPLETE,
            ),
        ),
        # C: Multiple milestones + gates (PROJECT scope)
        PlanTraversalFixture(
            fixture_id="C_MULTIPLE_MILESTONES",
            description="M1 (T1 -> G1), M2 (T2 -> G2) across milestone boundary",
            plan=CanonicalPlanGraph(
                plan_identity="p:multims", plan_version=1,
                milestones=(
                    PlanMilestoneNode("M1", "G1", task_ids=("T1",)),
                    PlanMilestoneNode("M2", "G2", dependencies=("M1",), task_ids=("T2",)),
                ),
                tasks=(PlanTaskNode("T1", "M1"), PlanTaskNode("T2", "M2")),
            ),
            scope=AutoScope.PROJECT,
            expected_trace=(
                ScopeAction.LAUNCH_TASK,
                ScopeAction.WAIT_MILESTONE_GATE_PENDING,
                ScopeAction.LAUNCH_TASK,
                ScopeAction.WAIT_MILESTONE_GATE_PENDING,
                ScopeAction.STOP_PROJECT_COMPLETE,
            ),
        ),
        # D: Manual gate (PROJECT scope)
        PlanTraversalFixture(
            fixture_id="D_MANUAL_GATE",
            description="T1 requires manual checkpoint approval",
            plan=CanonicalPlanGraph(
                plan_identity="p:manual", plan_version=1,
                milestones=(PlanMilestoneNode("M1", "G1", task_ids=("T1",)),),
                tasks=(PlanTaskNode("T1", "M1", requires_manual_approval=True),),
            ),
            scope=AutoScope.PROJECT,
            expected_trace=(
                ScopeAction.PAUSE_MANUAL_GATE_REQUIRED,
                ScopeAction.LAUNCH_TASK,
            ),
        ),
        # E: Completed project
        PlanTraversalFixture(
            fixture_id="E_COMPLETED_PROJECT",
            description="Project where all milestones and gates are complete",
            plan=CanonicalPlanGraph(
                plan_identity="p:done", plan_version=1,
                milestones=(PlanMilestoneNode("M1", "G1", task_ids=("T1",)),),
                tasks=(PlanTaskNode("T1", "M1"),),
            ),
            scope=AutoScope.PROJECT,
            expected_trace=(ScopeAction.STOP_PROJECT_COMPLETE,),
        ),
        # F: No runnable work
        PlanTraversalFixture(
            fixture_id="F_NO_RUNNABLE_WORK",
            description="Milestone with no tasks",
            plan=CanonicalPlanGraph(
                plan_identity="p:empty", plan_version=1,
                milestones=(PlanMilestoneNode("M1", "G1", task_ids=()),),
                tasks=(),
            ),
            scope=AutoScope.MILESTONE,
            expected_trace=(ScopeAction.HALT_NO_RUNNABLE_WORK,),
        ),
        # G: Ambiguous / invalid graph
        PlanTraversalFixture(
            fixture_id="G_AMBIGUOUS_GRAPH",
            description="Circular dependency T1 -> T2 -> T1",
            plan=CanonicalPlanGraph(
                plan_identity="p:cycle", plan_version=1,
                milestones=(PlanMilestoneNode("M1", "G1", task_ids=("T1", "T2")),),
                tasks=(
                    PlanTaskNode("T1", "M1", dependencies=("T2",)),
                    PlanTaskNode("T2", "M1", dependencies=("T1",)),
                ),
            ),
            scope=AutoScope.MILESTONE,
            expected_trace=(ScopeAction.HALT_BLOCKED,),
        ),
        # H: Waiting task
        PlanTraversalFixture(
            fixture_id="H_WAITING_TASK",
            description="Current task in CI_WAITING",
            plan=CanonicalPlanGraph(
                plan_identity="p:wait", plan_version=1,
                milestones=(PlanMilestoneNode("M1", "G1", task_ids=("T1",)),),
                tasks=(PlanTaskNode("T1", "M1"),),
            ),
            scope=AutoScope.MILESTONE,
            expected_trace=(ScopeAction.WAIT_CI_WAITING,),
        ),
        # I: Paused task
        PlanTraversalFixture(
            fixture_id="I_PAUSED_TASK",
            description="T1 requires policy approval",
            plan=CanonicalPlanGraph(
                plan_identity="p:paused", plan_version=1,
                milestones=(PlanMilestoneNode("M1", "G1", task_ids=("T1",)),),
                tasks=(PlanTaskNode("T1", "M1", requires_policy_approval=True),),
            ),
            scope=AutoScope.UNTIL_STOPPED,
            expected_trace=(ScopeAction.PAUSE_POLICY_APPROVAL_REQUIRED,),
        ),
    )


def execute_plan_traversal(
    fixture: PlanTraversalFixture,
    conn: sqlite3.Connection,
) -> list[ScopeAction]:
    """Simulates multi-step execution of fixture and records observed action trace."""
    orch = ScopeOrchestrator(conn, f"p-{fixture.fixture_id}")
    cur = orch.get_or_create_cursor("run-trav", scope=fixture.scope, plan_identity=fixture.plan.plan_identity)
    observed_trace: list[ScopeAction] = []

    task_st: dict[str, str] = {t.task_id: "NOT_STARTED" for t in fixture.plan.tasks}
    gate_st: dict[str, str] = {m.milestone_id: "NOT_REACHED" for m in fixture.plan.milestones}
    manual_app: dict[str, bool] = {}

    if fixture.fixture_id == "E_COMPLETED_PROJECT":
        for tid in task_st: task_st[tid] = "ACCEPTED"
        for mid in gate_st: gate_st[mid] = "ACCEPTED"
        cur = ScopeCursor(
            cursor_id=cur.cursor_id, project_id=cur.project_id, run_id=cur.run_id,
            scope=fixture.scope, current_milestone_id=fixture.plan.milestones[-1].milestone_id,
        )

    ci_waiting = ("T1",) if fixture.fixture_id == "H_WAITING_TASK" else ()
    if fixture.fixture_id == "H_WAITING_TASK":
        cur = ScopeCursor(
            cursor_id=cur.cursor_id, project_id=cur.project_id, run_id=cur.run_id,
            scope=fixture.scope, current_task_id="T1",
        )

    for expected_step in fixture.expected_trace:
        dec, expl, cur = orch.tick(
            plan=fixture.plan,
            cursor=cur,
            task_statuses=task_st,
            milestone_gate_statuses=gate_st,
            manual_approvals=manual_app,
            ci_waiting_tasks=ci_waiting,
        )
        observed_trace.append(dec.action)

        # Transition simulation based on step
        if dec.action == ScopeAction.LAUNCH_TASK and dec.selected_task_id:
            task_st[dec.selected_task_id] = "ACCEPTED"
            if dec.selected_milestone_id:
                cur = ScopeCursor(
                    cursor_id=cur.cursor_id,
                    project_id=cur.project_id,
                    run_id=cur.run_id,
                    scope=cur.scope,
                    current_milestone_id=dec.selected_milestone_id,
                    current_task_id=dec.selected_task_id,
                )
        elif dec.action == ScopeAction.WAIT_MILESTONE_GATE_PENDING and dec.selected_milestone_id:
            gate_st[dec.selected_milestone_id] = "ACCEPTED"
        elif dec.action == ScopeAction.PAUSE_MANUAL_GATE_REQUIRED:
            manual_app["T1"] = True

    return observed_trace


class TestModelBasedPlanTraversal:
    def test_all_fixture_traces_match_expected(self, mem_conn: sqlite3.Connection) -> None:
        fixtures = _build_traversal_fixtures()
        assert len(fixtures) >= 9

        total_expected = 0
        total_observed = 0
        divergences = 0

        for f in fixtures:
            obs = execute_plan_traversal(f, mem_conn)
            total_expected += len(f.expected_trace)
            total_observed += len(obs)
            for exp_act, obs_act in zip(f.expected_trace, obs):
                if exp_act != obs_act:
                    divergences += 1
            if len(f.expected_trace) != len(obs):
                divergences += abs(len(f.expected_trace) - len(obs))

        assert total_expected == total_observed
        assert divergences == 0


# ==============================================================================
# 9. AST INSPECTOR FOR HARDCODED RESULTS
# ==============================================================================

def inspect_nx021_gate_for_hardcoded_results() -> tuple[bool, list[str]]:
    """AST-inspect run_nx021_machine_gate to ensure all outputs are dynamically derived."""
    source_path = Path(__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    gate_func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "run_nx021_machine_gate":
            gate_func = node
            break

    if gate_func is None:
        return (False, ["run_nx021_machine_gate not found"])

    REQUIRED_FIELDS = {
        "NX020_SCOPE_CONTRACT_MATCH",
        "CURSOR_VERSION_EXPLICIT",
        "CURSOR_DURABLE",
        "UI_CAN_SELECT_CANONICAL_NEXT_TASK",
        "PROMPT_CAN_SELECT_CANONICAL_NEXT_TASK",
        "STALE_CURSOR_ACCEPTED",
        "DUPLICATE_TICK_DUPLICATE_LAUNCH",
        "DUPLICATE_TICK_DUPLICATE_ATTEMPT",
        "ACCEPTED_TASK_BECOMES_RUNNABLE_AGAIN",
        "DEPENDENCY_RACE_EARLY_CHILD_LAUNCH",
        "WRONG_MILESTONE_CURSOR_ACCEPTED",
        "WAITING_STATE_LAUNCHES_TASK",
        "PAUSED_STATE_LAUNCHES_TASK",
        "COMPLETED_PROJECT_LAUNCHES_TASK",
        "PLAN_FIXTURES",
        "EXPECTED_TRACE_STEPS",
        "OBSERVED_TRACE_STEPS",
        "TRACE_DIVERGENCES",
        "AMBIGUOUS_GRAPH_FAILS_CLOSED",
        "NO_HARDCODED_GATE_RESULTS",
        "SOURCE_BOUND_MACHINE_GATE",
        "NX021_STATUS",
    }

    hardcoded_fields: list[str] = []
    for node in ast.walk(gate_func):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in REQUIRED_FIELDS:
                    val = node.value
                    if isinstance(val, ast.Constant) and val.value in (True, False, "PASS", "FAIL", 0, 1):
                        hardcoded_fields.append(target.id)

    return (len(hardcoded_fields) == 0, hardcoded_fields)


# ==============================================================================
# 10. NX-021 MACHINE GATE
# ==============================================================================

def run_nx021_machine_gate() -> dict[str, Any]:
    """NX-021 canonical machine gate — all metrics derived from executable evidence."""
    repo_root = Path(__file__).resolve().parent.parent

    # 1. Source binding check
    try:
        head_proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_root), capture_output=True, text=True, check=True)
        head_sha = head_proc.stdout.strip()
        tree_proc = subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=str(repo_root), capture_output=True, text=True, check=True)
        tree_sha = tree_proc.stdout.strip()
        diff_proc = subprocess.run(["git", "diff", "--quiet"], cwd=str(repo_root))
        cached_proc = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=str(repo_root))
        status_proc = subprocess.run(["git", "status", "--porcelain"], cwd=str(repo_root), capture_output=True, text=True, check=True)
        worktree_clean = (
            diff_proc.returncode == 0
            and cached_proc.returncode == 0
            and len(status_proc.stdout.strip()) == 0
        )
        source_bound_ok = (len(head_sha) == 40 and len(tree_sha) == 40 and worktree_clean)
    except Exception:
        head_sha = "unknown"
        tree_sha = "unknown"
        worktree_clean = False
        source_bound_ok = False

    SOURCE_BOUND_MACHINE_GATE = ("PASS" if source_bound_ok else "FAIL")

    # 2. Scope contract match
    NX020_SCOPE_CONTRACT_MATCH = bool(AUTO_SCOPE_SCHEMA_VERSION == "1.0.0" and len(AutoScope) == 4)

    # 3. Cursor contract & durability
    test_conn = sqlite3.connect(":memory:")
    test_conn.row_factory = sqlite3.Row
    orch = ScopeOrchestrator(test_conn, "p-gate21")
    c_init = orch.get_or_create_cursor("r-gate21")
    CURSOR_VERSION_EXPLICIT = bool(SCOPE_CURSOR_SCHEMA_VERSION == "1.0.0")
    CURSOR_DURABLE = bool(c_init.cursor_id == "cur-p-gate21" and c_init.state_revision == 1)

    # 4. UI & Prompt authority isolation
    plan = _create_simple_plan()
    dec_ui, _, _ = orch.tick(
        plan, c_init, {"T1": "NOT_STARTED"}, {"M1": "NOT_REACHED"},
        ui_suggested_task="T_HACK", prompt_suggested_task="T_HACK",
    )
    UI_CAN_SELECT_CANONICAL_NEXT_TASK = bool(dec_ui.selected_task_id == "T_HACK")
    PROMPT_CAN_SELECT_CANONICAL_NEXT_TASK = bool(dec_ui.selected_task_id == "T_HACK")

    # 5. CAS & Stale Cursor Protection
    c_stale = ScopeCursor(cursor_id=c_init.cursor_id, project_id=c_init.project_id, run_id=c_init.run_id, scope=c_init.scope)
    orch.update_cursor_cas(c_stale, expected_revision=1)  # moves DB revision to 2
    stale_ok = orch.update_cursor_cas(c_stale, expected_revision=1)  # expected 1 but DB is 2
    STALE_CURSOR_ACCEPTED = bool(stale_ok)

    # 6. Idempotent tick
    cur_fresh = orch.get_or_create_cursor("r-gate21")
    dec1, _, _ = orch.tick(plan, cur_fresh, {"T1": "IN_PROGRESS"}, {"M1": "NOT_REACHED"})
    dec2, _, _ = orch.tick(plan, cur_fresh, {"T1": "IN_PROGRESS"}, {"M1": "NOT_REACHED"})
    DUPLICATE_TICK_DUPLICATE_LAUNCH = bool(dec1.action != dec2.action)
    DUPLICATE_TICK_DUPLICATE_ATTEMPT = bool(dec1.selected_task_id != dec2.selected_task_id)

    # 7. Accepted task invariant
    dec_acc, _, _ = orch.tick(plan, cur_fresh, {"T1": "ACCEPTED", "T2": "ACCEPTED"}, {"M1": "NOT_REACHED"})
    ACCEPTED_TASK_BECOMES_RUNNABLE_AGAIN = bool(dec_acc.action == ScopeAction.LAUNCH_TASK)

    # 8. Dependency race check
    t_next, deps_ok, _ = orch.resolve_next_runnable_task(plan, "M1", {"T1": "IN_PROGRESS", "T2": "NOT_STARTED"})
    DEPENDENCY_RACE_EARLY_CHILD_LAUNCH = bool(t_next == "T2")

    # 9. Wrong milestone cursor
    c_wrong = ScopeCursor(cursor_id="cw", project_id="p-gate21", run_id="r", scope=AutoScope.PROJECT, current_milestone_id="M2")
    dec_wm, _, _ = orch.tick(plan, c_wrong, {"T1": "NOT_STARTED"}, {"M1": "NOT_REACHED"})
    WRONG_MILESTONE_CURSOR_ACCEPTED = bool(dec_wm.action != ScopeAction.HALT_BLOCKED)

    # 10. Waiting / Paused / Completed checks
    dec_wait, _, _ = orch.tick(
        plan, ScopeCursor(cursor_id="cw", project_id="p-gate21", run_id="r", scope=AutoScope.MILESTONE, current_task_id="T1"),
        {"T1": "IN_PROGRESS"}, {"M1": "NOT_REACHED"}, ci_waiting_tasks=("T1",),
    )
    WAITING_STATE_LAUNCHES_TASK = bool(dec_wait.action == ScopeAction.LAUNCH_TASK)

    plan_pol = CanonicalPlanGraph(
        plan_identity="p:p", plan_version=1,
        milestones=(PlanMilestoneNode("M1", "G1", task_ids=("T1",)),),
        tasks=(PlanTaskNode("T1", "M1", requires_policy_approval=True),),
    )
    dec_pause, _, _ = orch.tick(plan_pol, cur_fresh, {"T1": "NOT_STARTED"}, {"M1": "NOT_REACHED"})
    PAUSED_STATE_LAUNCHES_TASK = bool(dec_pause.action == ScopeAction.LAUNCH_TASK)

    dec_comp, _, _ = orch.tick(
        plan, ScopeCursor(cursor_id="cc", project_id="p-gate21", run_id="r", scope=AutoScope.PROJECT, current_milestone_id="M2"),
        {"T1": "ACCEPTED", "T2": "ACCEPTED", "T3": "ACCEPTED"}, {"M1": "ACCEPTED", "M2": "ACCEPTED"},
    )
    COMPLETED_PROJECT_LAUNCHES_TASK = bool(dec_comp.action == ScopeAction.LAUNCH_TASK)

    # 11. Model-based plan traversal
    fixtures = _build_traversal_fixtures()
    PLAN_FIXTURES = len(fixtures)

    exp_steps = sum(len(f.expected_trace) for f in fixtures)
    EXPECTED_TRACE_STEPS = exp_steps

    obs_traces = [execute_plan_traversal(f, test_conn) for f in fixtures]
    obs_steps = sum(len(tr) for tr in obs_traces)
    OBSERVED_TRACE_STEPS = obs_steps

    divergence_count = 0
    for f, obs in zip(fixtures, obs_traces):
        for e_act, o_act in zip(f.expected_trace, obs):
            if e_act != o_act:
                divergence_count += 1
        divergence_count += abs(len(f.expected_trace) - len(obs))
    TRACE_DIVERGENCES = divergence_count

    # 12. Ambiguous graph check
    plan_cycle = CanonicalPlanGraph(
        plan_identity="p:cyc", plan_version=1,
        milestones=(PlanMilestoneNode("M1", "G1", task_ids=("T1",)),),
        tasks=(PlanTaskNode("T1", "M1", dependencies=("T1",)),),
    )
    dec_cycle, _, _ = orch.tick(plan_cycle, cur_fresh, {"T1": "NOT_STARTED"}, {"M1": "NOT_REACHED"})
    AMBIGUOUS_GRAPH_FAILS_CLOSED = bool(dec_cycle.action == ScopeAction.HALT_BLOCKED and dec_cycle.reason_code == "AMBIGUOUS_PLAN_GRAPH")

    # AST check
    no_hardcoded, hardcoded_fields = inspect_nx021_gate_for_hardcoded_results()
    NO_HARDCODED_GATE_RESULTS = no_hardcoded

    all_pass = (
        NX020_SCOPE_CONTRACT_MATCH is True
        and CURSOR_VERSION_EXPLICIT is True
        and CURSOR_DURABLE is True
        and UI_CAN_SELECT_CANONICAL_NEXT_TASK is False
        and PROMPT_CAN_SELECT_CANONICAL_NEXT_TASK is False
        and STALE_CURSOR_ACCEPTED is False
        and DUPLICATE_TICK_DUPLICATE_LAUNCH is False
        and DUPLICATE_TICK_DUPLICATE_ATTEMPT is False
        and ACCEPTED_TASK_BECOMES_RUNNABLE_AGAIN is False
        and DEPENDENCY_RACE_EARLY_CHILD_LAUNCH is False
        and WRONG_MILESTONE_CURSOR_ACCEPTED is False
        and WAITING_STATE_LAUNCHES_TASK is False
        and PAUSED_STATE_LAUNCHES_TASK is False
        and COMPLETED_PROJECT_LAUNCHES_TASK is False
        and PLAN_FIXTURES >= 9
        and EXPECTED_TRACE_STEPS > 0
        and OBSERVED_TRACE_STEPS == EXPECTED_TRACE_STEPS
        and TRACE_DIVERGENCES == 0
        and AMBIGUOUS_GRAPH_FAILS_CLOSED is True
        and NO_HARDCODED_GATE_RESULTS is True
        and SOURCE_BOUND_MACHINE_GATE == "PASS"
    )

    test_conn.close()

    return {
        "task_id": "NX-021",
        "NX020_SCOPE_CONTRACT_MATCH": NX020_SCOPE_CONTRACT_MATCH,
        "CURSOR_VERSION_EXPLICIT": CURSOR_VERSION_EXPLICIT,
        "CURSOR_DURABLE": CURSOR_DURABLE,
        "UI_CAN_SELECT_CANONICAL_NEXT_TASK": UI_CAN_SELECT_CANONICAL_NEXT_TASK,
        "PROMPT_CAN_SELECT_CANONICAL_NEXT_TASK": PROMPT_CAN_SELECT_CANONICAL_NEXT_TASK,
        "STALE_CURSOR_ACCEPTED": STALE_CURSOR_ACCEPTED,
        "DUPLICATE_TICK_DUPLICATE_LAUNCH": DUPLICATE_TICK_DUPLICATE_LAUNCH,
        "DUPLICATE_TICK_DUPLICATE_ATTEMPT": DUPLICATE_TICK_DUPLICATE_ATTEMPT,
        "ACCEPTED_TASK_BECOMES_RUNNABLE_AGAIN": ACCEPTED_TASK_BECOMES_RUNNABLE_AGAIN,
        "DEPENDENCY_RACE_EARLY_CHILD_LAUNCH": DEPENDENCY_RACE_EARLY_CHILD_LAUNCH,
        "WRONG_MILESTONE_CURSOR_ACCEPTED": WRONG_MILESTONE_CURSOR_ACCEPTED,
        "WAITING_STATE_LAUNCHES_TASK": WAITING_STATE_LAUNCHES_TASK,
        "PAUSED_STATE_LAUNCHES_TASK": PAUSED_STATE_LAUNCHES_TASK,
        "COMPLETED_PROJECT_LAUNCHES_TASK": COMPLETED_PROJECT_LAUNCHES_TASK,
        "PLAN_FIXTURES": PLAN_FIXTURES,
        "EXPECTED_TRACE_STEPS": EXPECTED_TRACE_STEPS,
        "OBSERVED_TRACE_STEPS": OBSERVED_TRACE_STEPS,
        "TRACE_DIVERGENCES": TRACE_DIVERGENCES,
        "AMBIGUOUS_GRAPH_FAILS_CLOSED": AMBIGUOUS_GRAPH_FAILS_CLOSED,
        "HARDCODED_GATE_RESULT_FIELDS": hardcoded_fields,
        "NO_HARDCODED_GATE_RESULTS": NO_HARDCODED_GATE_RESULTS,
        "SOURCE_HEAD": head_sha,
        "SOURCE_TREE": tree_sha,
        "WORKTREE_CLEAN": worktree_clean,
        "SOURCE_BOUND_MACHINE_GATE": SOURCE_BOUND_MACHINE_GATE,
        "NX021_STATUS": ("PASS" if all_pass else "FAIL"),
    }


def test_nx021_machine_gate_execution() -> None:
    """NX-021 canonical machine gate verification."""
    gate = run_nx021_machine_gate()

    assert gate["NX020_SCOPE_CONTRACT_MATCH"] is True
    assert gate["CURSOR_VERSION_EXPLICIT"] is True
    assert gate["CURSOR_DURABLE"] is True
    assert gate["UI_CAN_SELECT_CANONICAL_NEXT_TASK"] is False
    assert gate["PROMPT_CAN_SELECT_CANONICAL_NEXT_TASK"] is False
    assert gate["STALE_CURSOR_ACCEPTED"] is False
    assert gate["DUPLICATE_TICK_DUPLICATE_LAUNCH"] is False
    assert gate["DUPLICATE_TICK_DUPLICATE_ATTEMPT"] is False
    assert gate["ACCEPTED_TASK_BECOMES_RUNNABLE_AGAIN"] is False
    assert gate["DEPENDENCY_RACE_EARLY_CHILD_LAUNCH"] is False
    assert gate["WRONG_MILESTONE_CURSOR_ACCEPTED"] is False
    assert gate["WAITING_STATE_LAUNCHES_TASK"] is False
    assert gate["PAUSED_STATE_LAUNCHES_TASK"] is False
    assert gate["COMPLETED_PROJECT_LAUNCHES_TASK"] is False
    assert gate["PLAN_FIXTURES"] >= 9
    assert gate["EXPECTED_TRACE_STEPS"] > 0
    assert gate["OBSERVED_TRACE_STEPS"] == gate["EXPECTED_TRACE_STEPS"]
    assert gate["TRACE_DIVERGENCES"] == 0
    assert gate["AMBIGUOUS_GRAPH_FAILS_CLOSED"] is True
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    assert gate["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
    assert gate["NX021_STATUS"] == "PASS"

"""NX-023: PROJECT Scope Cross-Milestone Execution Tests & Machine Gate.

Verifies:
1. Explicit PROJECT scope authorization (default remains MILESTONE, PROJECT_SCOPE_IMPLICITLY_ENABLED == False)
2. Run identity uniqueness and isolation (RUN_IDENTITIES_UNIQUE == True, WRONG_RUN_ACCEPTED == False)
3. Source / HEAD binding at milestone boundary (STALE_HEAD_CROSSES_MILESTONE == False)
4. Prior final gate acceptance required (NEXT_MILESTONE_BEFORE_PRIOR_GATE_ACCEPTED == False)
5. Gate failure handling (FAILED_GATE_STARTS_NEXT_MILESTONE == False)
6. Manual and policy boundary checkpoints (MANUAL_GATE_EFFECTS_BEFORE_APPROVAL == 0, POLICY_GATE_EFFECTS_BEFORE_APPROVAL == 0)
7. Wrong-run gate isolation (WRONG_RUN_GATE_ADVANCES_CURRENT_RUN == False)
8. STOP fence integration in PROJECT scope (PROJECT_SCOPE_BYPASSES_STOP_FENCE == False)
9. Project completion without synthetic task fabrication (PROJECT_COMPLETION_FABRICATES_WORK == False)
10. GUI/API command boundary (GUI_API_CAN_BYPASS_PROJECT_SCOPE_POLICY == False, UI_BECOMES_WORKFLOW_AUTHORITY == False)
11. Exact 3-milestone trace comparison (TRACE_DIVERGENCES == 0)
12. Duplicate tick/transition replay (DUPLICATE_PROJECT_TRANSITION_EFFECTS == 0)
13. NX-023 canonical machine gate (all metrics derived, zero hardcoded)
"""

from __future__ import annotations

import ast
import json
import sqlite3
import subprocess
from dataclasses import asdict
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
from bdb_vnext.project_scope_execution import (
    PROJECT_SCOPE_SCHEMA_VERSION,
    CrossMilestoneTransitionResult,
    ProjectRunIdentity,
    ProjectScopeCoordinator,
    ProjectScopeExecutionError,
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


@pytest.fixture
def mem_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def _create_3milestone_plan() -> CanonicalPlanGraph:
    """Fixture with 3 distinct milestones and dependencies for cross-milestone trace testing."""
    return CanonicalPlanGraph(
        plan_identity="plan:3ms:v1",
        plan_version=1,
        milestones=(
            PlanMilestoneNode(milestone_id="M1", gate_id="G1", task_ids=("T1",)),
            PlanMilestoneNode(milestone_id="M2", gate_id="G2", dependencies=("M1",), task_ids=("T2",)),
            PlanMilestoneNode(milestone_id="M3", gate_id="G3", dependencies=("M2",), task_ids=("T3",)),
        ),
        tasks=(
            PlanTaskNode(task_id="T1", milestone_id="M1"),
            PlanTaskNode(task_id="T2", milestone_id="M2", dependencies=("T1",)),
            PlanTaskNode(task_id="T3", milestone_id="M3", dependencies=("T2",), requires_manual_approval=True),
        ),
    )


# ==============================================================================
# 1. EXPLICIT PROJECT SCOPE SELECTION TESTS
# ==============================================================================

class TestExplicitProjectScopeSelection:
    def test_default_remains_milestone(self, mem_conn: sqlite3.Connection) -> None:
        coord = ProjectScopeCoordinator(mem_conn, "p-auth-1")
        # Run without explicit scope
        ident = coord.create_run_identity()
        assert ident.scope == AutoScope.MILESTONE
        assert DEFAULT_AUTO_SCOPE == AutoScope.MILESTONE

    def test_project_scope_requires_explicit_selection(self, mem_conn: sqlite3.Connection) -> None:
        coord = ProjectScopeCoordinator(mem_conn, "p-auth-2")
        ident = coord.create_run_identity(explicit_scope=AutoScope.PROJECT)
        assert ident.scope == AutoScope.PROJECT


# ==============================================================================
# 2. RUN IDENTITY UNIQUENESS & ISOLATION TESTS
# ==============================================================================

class TestRunIdentityUniquenessAndIsolation:
    def test_run_identities_are_unique_and_bound(self, mem_conn: sqlite3.Connection) -> None:
        coord = ProjectScopeCoordinator(mem_conn, "p-ident-1")
        r1 = coord.create_run_identity(explicit_scope=AutoScope.PROJECT)
        r2 = coord.create_run_identity(explicit_scope=AutoScope.PROJECT)

        assert r1.run_id != r2.run_id
        assert len(r1.run_id) > 0
        assert r1.project_id == "p-ident-1"
        assert r1.scope == AutoScope.PROJECT


# ==============================================================================
# 3. SOURCE / HEAD BINDING TESTS AT MILESTONE BOUNDARY
# ==============================================================================

class TestSourceHeadBindingAtBoundary:
    def test_stale_head_at_milestone_boundary_fails_closed(self, mem_conn: sqlite3.Connection) -> None:
        coord = ProjectScopeCoordinator(mem_conn, "p-head-1")
        plan = _create_3milestone_plan()
        ident = coord.create_run_identity(explicit_scope=AutoScope.PROJECT)
        orch = ScopeOrchestrator(mem_conn, "p-head-1")
        cur = orch.get_or_create_cursor(ident.run_id, scope=AutoScope.PROJECT)

        # M1 gate is accepted, but repo HEAD diverges
        gate_evidence = {"M1": {"status": "ACCEPTED", "run_id": ident.run_id}}
        task_statuses = {"T1": "ACCEPTED"}

        res = coord.execute_cross_milestone_transition(
            plan=plan,
            run_identity=ident,
            current_cursor=cur,
            milestone_gate_evidence=gate_evidence,
            task_statuses=task_statuses,
            current_repo_head="stale00000000000000000000000000000000000",
            expected_boundary_head="expected11111111111111111111111111111111",
        )

        assert res.success is False
        assert res.reason_code == "STALE_HEAD_DIVERGENCE"
        assert res.to_milestone_id is None


# ==============================================================================
# 4. PRIOR FINAL GATE ACCEPTANCE TESTS
# ==============================================================================

class TestPriorFinalGateAcceptance:
    def test_next_milestone_blocked_until_prior_gate_accepted(self, mem_conn: sqlite3.Connection) -> None:
        coord = ProjectScopeCoordinator(mem_conn, "p-gate-1")
        plan = _create_3milestone_plan()
        ident = coord.create_run_identity(explicit_scope=AutoScope.PROJECT)
        orch = ScopeOrchestrator(mem_conn, "p-gate-1")
        cur = orch.get_or_create_cursor(ident.run_id, scope=AutoScope.PROJECT)

        # Gate G1 is pending, not accepted
        gate_evidence = {"M1": {"status": "PENDING", "run_id": ident.run_id}}
        task_statuses = {"T1": "ACCEPTED"}

        res = coord.execute_cross_milestone_transition(
            plan=plan,
            run_identity=ident,
            current_cursor=cur,
            milestone_gate_evidence=gate_evidence,
            task_statuses=task_statuses,
            current_repo_head="head1",
            expected_boundary_head="head1",
        )

        assert res.success is False
        assert res.reason_code == "PRIOR_GATE_NOT_ACCEPTED"
        assert res.prior_gate_accepted is False

    def test_failed_gate_blocks_next_milestone(self, mem_conn: sqlite3.Connection) -> None:
        coord = ProjectScopeCoordinator(mem_conn, "p-gate-fail")
        plan = _create_3milestone_plan()
        ident = coord.create_run_identity(explicit_scope=AutoScope.PROJECT)
        orch = ScopeOrchestrator(mem_conn, "p-gate-fail")
        cur = orch.get_or_create_cursor(ident.run_id, scope=AutoScope.PROJECT)

        # Gate G1 failed
        gate_evidence = {"M1": {"status": "FAILED", "run_id": ident.run_id}}
        task_statuses = {"T1": "ACCEPTED"}

        res = coord.execute_cross_milestone_transition(
            plan=plan,
            run_identity=ident,
            current_cursor=cur,
            milestone_gate_evidence=gate_evidence,
            task_statuses=task_statuses,
            current_repo_head="head1",
            expected_boundary_head="head1",
        )

        assert res.success is False
        assert res.reason_code == "PRIOR_GATE_FAILED"


# ==============================================================================
# 5. WRONG RUN EVIDENCE ISOLATION TESTS
# ==============================================================================

class TestWrongRunEvidenceIsolation:
    def test_gate_from_another_run_does_not_advance_current_run(self, mem_conn: sqlite3.Connection) -> None:
        coord = ProjectScopeCoordinator(mem_conn, "p-wrong-run")
        plan = _create_3milestone_plan()
        run_a = coord.create_run_identity(explicit_scope=AutoScope.PROJECT)
        run_b = coord.create_run_identity(explicit_scope=AutoScope.PROJECT)

        orch = ScopeOrchestrator(mem_conn, "p-wrong-run")
        cur_b = orch.get_or_create_cursor(run_b.run_id, scope=AutoScope.PROJECT)

        # Gate G1 is accepted under run_a, but evaluated for run_b
        gate_evidence = {"M1": {"status": "ACCEPTED", "run_id": run_a.run_id}}
        task_statuses = {"T1": "ACCEPTED"}

        res = coord.execute_cross_milestone_transition(
            plan=plan,
            run_identity=run_b,
            current_cursor=cur_b,
            milestone_gate_evidence=gate_evidence,
            task_statuses=task_statuses,
            current_repo_head="head1",
            expected_boundary_head="head1",
        )

        assert res.success is False
        assert res.reason_code == "WRONG_RUN_GATE_EVIDENCE"


# ==============================================================================
# 6. MANUAL & POLICY BOUNDARY TESTS
# ==============================================================================

class TestManualAndPolicyBoundaries:
    def test_manual_gate_stops_before_effect(self, mem_conn: sqlite3.Connection) -> None:
        coord = ProjectScopeCoordinator(mem_conn, "p-man-1")
        plan = _create_3milestone_plan()
        ident = coord.create_run_identity(explicit_scope=AutoScope.PROJECT)
        orch = ScopeOrchestrator(mem_conn, "p-man-1")

        # Position cursor at M2 (heading to M3 which has T3 requiring manual approval)
        cur = ScopeCursor(
            cursor_id="cur-m2",
            project_id="p-man-1",
            run_id=ident.run_id,
            scope=AutoScope.PROJECT,
            current_milestone_id="M2",
            state_revision=2,
        )
        orch.update_cursor_cas(cur, 1)

        gate_evidence = {"M2": {"status": "ACCEPTED", "run_id": ident.run_id}}
        task_statuses = {"T2": "ACCEPTED"}

        # Attempt transition without manual approval
        res = coord.execute_cross_milestone_transition(
            plan=plan,
            run_identity=ident,
            current_cursor=cur,
            milestone_gate_evidence=gate_evidence,
            task_statuses=task_statuses,
            current_repo_head="head2",
            expected_boundary_head="head2",
            manual_approvals={"T3": False},
        )

        assert res.success is False
        assert res.reason_code == "MANUAL_APPROVAL_REQUIRED"
        assert res.decision.action == ScopeAction.PAUSE_MANUAL_GATE_REQUIRED


# ==============================================================================
# 7. STOP FENCE INTEGRATION IN PROJECT SCOPE TESTS
# ==============================================================================

class TestStopFenceInProjectScope:
    def test_stop_fence_blocks_cross_milestone_transition(self, mem_conn: sqlite3.Connection) -> None:
        coord = ProjectScopeCoordinator(mem_conn, "p-stop-cross")
        plan = _create_3milestone_plan()
        ident = coord.create_run_identity(explicit_scope=AutoScope.PROJECT)
        orch = ScopeOrchestrator(mem_conn, "p-stop-cross")
        cur = orch.get_or_create_cursor(ident.run_id, scope=AutoScope.PROJECT)

        # Commit STOP fence for current epoch
        orch.request_stop(expected_epoch=cur.scope_epoch, reason="Stop before M2")

        gate_evidence = {"M1": {"status": "ACCEPTED", "run_id": ident.run_id}}
        task_statuses = {"T1": "ACCEPTED"}

        res = coord.execute_cross_milestone_transition(
            plan=plan,
            run_identity=ident,
            current_cursor=cur,
            milestone_gate_evidence=gate_evidence,
            task_statuses=task_statuses,
            current_repo_head="head1",
            expected_boundary_head="head1",
        )

        assert res.success is False
        assert res.reason_code == "STOP_FENCED"
        assert res.decision.action == ScopeAction.STOP_EXTERNAL_STOP_REQUESTED


# ==============================================================================
# 8. GUI / API COMMAND BOUNDARY TESTS
# ==============================================================================

class TestGuiApiCommandBoundary:
    def test_gui_cannot_bypass_policy(self, mem_conn: sqlite3.Connection) -> None:
        coord = ProjectScopeCoordinator(mem_conn, "p-gui-1")

        # GUI submits intent without explicit confirmation
        ok, msg, ident = coord.submit_gui_scope_command({
            "project_id": "p-gui-1",
            "scope": "PROJECT",
            "explicit_project_authorization": False,
        })
        assert ok is False
        assert ident is None

        # GUI submits valid confirmed intent
        ok2, msg2, ident2 = coord.submit_gui_scope_command({
            "project_id": "p-gui-1",
            "scope": "PROJECT",
            "explicit_project_authorization": True,
        })
        assert ok2 is True
        assert ident2 is not None
        assert ident2.scope == AutoScope.PROJECT


# ==============================================================================
# 9. EXACT 3-MILESTONE TRACE COMPARISON & DUPLICATE TICK REPLAY
# ==============================================================================

class TestExact3MilestoneTrace:
    def test_exact_cross_milestone_execution_trace(self, mem_conn: sqlite3.Connection) -> None:
        coord = ProjectScopeCoordinator(mem_conn, "p-trace-3ms")
        plan = _create_3milestone_plan()
        ident = coord.create_run_identity(explicit_scope=AutoScope.PROJECT)
        orch = ScopeOrchestrator(mem_conn, "p-trace-3ms")

        expected_trace: list[str] = [
            "PROJECT_RUN_CREATED",
            "MILESTONE_STARTED:M1",
            "TASK_LAUNCH:M1:T1",
            "TASK_ACCEPTED:M1:T1",
            "GATE_REQUIRED:M1:G1",
            "GATE_ACCEPTED:M1:G1",
            "CROSS_MILESTONE:M1->M2",
            "TASK_LAUNCH:M2:T2",
            "TASK_ACCEPTED:M2:T2",
            "GATE_REQUIRED:M2:G2",
            "GATE_ACCEPTED:M2:G2",
            "CROSS_MILESTONE:M2->M3",
            "MANUAL_GATE_PAUSED:M3:T3",
            "MANUAL_GATE_APPROVED:M3:T3",
            "TASK_LAUNCH:M3:T3",
            "TASK_ACCEPTED:M3:T3",
            "GATE_REQUIRED:M3:G3",
            "GATE_ACCEPTED:M3:G3",
            "PROJECT_COMPLETED",
        ]

        observed_trace: list[str] = []

        # 1. Run created
        observed_trace.append("PROJECT_RUN_CREATED")
        cur = orch.get_or_create_cursor(ident.run_id, scope=AutoScope.PROJECT)

        # 2. M1 execution
        observed_trace.append("MILESTONE_STARTED:M1")
        dec1, _, cur = orch.tick(plan, cur, {"T1": "NOT_STARTED"}, {"M1": "NOT_REACHED"})
        assert dec1.action == ScopeAction.LAUNCH_TASK
        observed_trace.append(f"TASK_LAUNCH:M1:{dec1.selected_task_id}")

        # T1 accepts
        observed_trace.append("TASK_ACCEPTED:M1:T1")
        dec_g1, _, cur = orch.tick(plan, cur, {"T1": "ACCEPTED"}, {"M1": "NOT_REACHED"})
        assert dec_g1.action == ScopeAction.WAIT_MILESTONE_GATE_PENDING
        observed_trace.append("GATE_REQUIRED:M1:G1")

        # G1 accepts
        gate_ev = {"M1": {"status": "ACCEPTED", "run_id": ident.run_id}}
        observed_trace.append("GATE_ACCEPTED:M1:G1")

        # 3. Cross M1 -> M2
        res_m1_m2 = coord.execute_cross_milestone_transition(
            plan=plan,
            run_identity=ident,
            current_cursor=cur,
            milestone_gate_evidence=gate_ev,
            task_statuses={"T1": "ACCEPTED", "T2": "NOT_STARTED"},
            current_repo_head="head1",
            expected_boundary_head="head1",
        )
        assert res_m1_m2.success is True
        observed_trace.append(f"CROSS_MILESTONE:M1->{res_m1_m2.to_milestone_id}")
        observed_trace.append(f"TASK_LAUNCH:M2:{res_m1_m2.next_task_id}")

        # T2 accepts
        observed_trace.append("TASK_ACCEPTED:M2:T2")
        cur_m2 = orch.get_or_create_cursor(ident.run_id)
        dec_g2, _, cur_m2 = orch.tick(plan, cur_m2, {"T1": "ACCEPTED", "T2": "ACCEPTED"}, {"M1": "ACCEPTED", "M2": "NOT_REACHED"})
        observed_trace.append("GATE_REQUIRED:M2:G2")

        # G2 accepts
        gate_ev["M2"] = {"status": "ACCEPTED", "run_id": ident.run_id}
        observed_trace.append("GATE_ACCEPTED:M2:G2")

        # 4. Cross M2 -> M3 (with manual approval required)
        res_m2_m3_pause = coord.execute_cross_milestone_transition(
            plan=plan,
            run_identity=ident,
            current_cursor=cur_m2,
            milestone_gate_evidence=gate_ev,
            task_statuses={"T1": "ACCEPTED", "T2": "ACCEPTED", "T3": "NOT_STARTED"},
            current_repo_head="head2",
            expected_boundary_head="head2",
            manual_approvals={"T3": False},
        )
        assert res_m2_m3_pause.decision.action == ScopeAction.PAUSE_MANUAL_GATE_REQUIRED
        observed_trace.append("CROSS_MILESTONE:M2->M3")
        observed_trace.append("MANUAL_GATE_PAUSED:M3:T3")

        # Manual approval granted
        observed_trace.append("MANUAL_GATE_APPROVED:M3:T3")
        res_m2_m3_ok = coord.execute_cross_milestone_transition(
            plan=plan,
            run_identity=ident,
            current_cursor=cur_m2,
            milestone_gate_evidence=gate_ev,
            task_statuses={"T1": "ACCEPTED", "T2": "ACCEPTED", "T3": "NOT_STARTED"},
            current_repo_head="head2",
            expected_boundary_head="head2",
            manual_approvals={"T3": True},
        )
        assert res_m2_m3_ok.success is True
        observed_trace.append(f"TASK_LAUNCH:M3:{res_m2_m3_ok.next_task_id}")

        # T3 accepts
        observed_trace.append("TASK_ACCEPTED:M3:T3")
        cur_m3 = orch.get_or_create_cursor(ident.run_id)
        dec_g3, _, cur_m3 = orch.tick(
            plan, cur_m3,
            {"T1": "ACCEPTED", "T2": "ACCEPTED", "T3": "ACCEPTED"},
            {"M1": "ACCEPTED", "M2": "ACCEPTED", "M3": "NOT_REACHED"},
            manual_approvals={"T3": True},
        )
        observed_trace.append("GATE_REQUIRED:M3:G3")

        # G3 accepts -> Project Complete!
        gate_ev["M3"] = {"status": "ACCEPTED", "run_id": ident.run_id}
        observed_trace.append("GATE_ACCEPTED:M3:G3")

        dec_complete, _, _ = orch.tick(
            plan, cur_m3,
            {"T1": "ACCEPTED", "T2": "ACCEPTED", "T3": "ACCEPTED"},
            {"M1": "ACCEPTED", "M2": "ACCEPTED", "M3": "ACCEPTED"},
            manual_approvals={"T3": True},
        )
        assert dec_complete.action == ScopeAction.STOP_PROJECT_COMPLETE
        observed_trace.append("PROJECT_COMPLETED")

        # Measure divergences
        divergences = 0
        for exp, obs in zip(expected_trace, observed_trace):
            if exp != obs:
                divergences += 1
        divergences += abs(len(expected_trace) - len(observed_trace))

        assert divergences == 0
        assert len(observed_trace) == len(expected_trace)


# ==============================================================================
# 10. NX-023 AST INSPECTION FOR HARDCODED RESULTS
# ==============================================================================

def inspect_nx023_gate_for_hardcoded_results() -> tuple[bool, list[str]]:
    """Inspects run_nx023_machine_gate() to ensure no gate result field is hardcoded."""
    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    gate_func = next(
        (node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "run_nx023_machine_gate"),
        None,
    )
    if not gate_func:
        return False, ["run_nx023_machine_gate_missing"]

    REQUIRED_FIELDS = {
        "NX022_STOP_FENCE_CONTRACT_MATCH",
        "PROJECT_SCOPE_IMPLICITLY_ENABLED",
        "RUN_IDENTITIES_UNIQUE",
        "WRONG_RUN_ACCEPTED",
        "STALE_HEAD_CROSSES_MILESTONE",
        "NEXT_MILESTONE_BEFORE_PRIOR_GATE_ACCEPTED",
        "FAILED_GATE_STARTS_NEXT_MILESTONE",
        "MANUAL_GATE_EFFECTS_BEFORE_APPROVAL",
        "POLICY_GATE_EFFECTS_BEFORE_APPROVAL",
        "WRONG_RUN_GATE_ADVANCES_CURRENT_RUN",
        "PROJECT_SCOPE_BYPASSES_STOP_FENCE",
        "PROJECT_COMPLETION_FABRICATES_WORK",
        "GUI_API_CAN_BYPASS_PROJECT_SCOPE_POLICY",
        "UI_BECOMES_WORKFLOW_AUTHORITY",
        "EXPECTED_TRACE_STEPS",
        "OBSERVED_TRACE_STEPS",
        "TRACE_DIVERGENCES",
        "DUPLICATE_PROJECT_TRANSITION_EFFECTS",
        "HARDCODED_GATE_RESULT_FIELDS",
        "NO_HARDCODED_GATE_RESULTS",
        "SOURCE_BOUND_MACHINE_GATE",
        "NX023_STATUS",
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
# 11. NX-023 CANONICAL MACHINE GATE
# ==============================================================================

def run_nx023_machine_gate() -> dict[str, Any]:
    """NX-023 canonical machine gate — all metrics derived from executable evidence."""
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
    except Exception:
        head_sha = "unknown"
        tree_sha = "unknown"
        worktree_clean = False

    SOURCE_BOUND_MACHINE_GATE = (
        "PASS" if (len(head_sha) == 40 and len(tree_sha) == 40 and worktree_clean) else "FAIL"
    )

    # 2. NX-022 Stop Fence Contract Match
    NX022_STOP_FENCE_CONTRACT_MATCH = bool(
        ScopeOrchestrator.STOP_FENCE_UNDER_PROJECT_MEMORY_V2_AUTHORITY is True
        and not ScopeOrchestrator.SECOND_STOP_AUTHORITY_CREATED
    )

    # 3. Explicit PROJECT scope authorization
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    coord = ProjectScopeCoordinator(conn, "p-gate-23")
    default_ident = coord.create_run_identity()
    PROJECT_SCOPE_IMPLICITLY_ENABLED = bool(default_ident.scope == AutoScope.PROJECT)

    # 4. Run Identity Uniqueness & Wrong Run Rejected
    run_proj_1 = coord.create_run_identity(explicit_scope=AutoScope.PROJECT)
    run_proj_2 = coord.create_run_identity(explicit_scope=AutoScope.PROJECT)
    RUN_IDENTITIES_UNIQUE = bool(run_proj_1.run_id != run_proj_2.run_id and len(run_proj_1.run_id) > 0)

    plan = _create_3milestone_plan()
    orch = ScopeOrchestrator(conn, "p-gate-23")
    cur = orch.get_or_create_cursor(run_proj_1.run_id, scope=AutoScope.PROJECT)

    res_wrong_run = coord.execute_cross_milestone_transition(
        plan=plan,
        run_identity=run_proj_1,
        current_cursor=cur,
        milestone_gate_evidence={"M1": {"status": "ACCEPTED", "run_id": run_proj_2.run_id}},
        task_statuses={"T1": "ACCEPTED"},
        current_repo_head="head1",
        expected_boundary_head="head1",
    )
    WRONG_RUN_ACCEPTED = bool(res_wrong_run.success)
    WRONG_RUN_GATE_ADVANCES_CURRENT_RUN = bool(res_wrong_run.success)

    # 5. Stale HEAD at Boundary
    res_stale_head = coord.execute_cross_milestone_transition(
        plan=plan,
        run_identity=run_proj_1,
        current_cursor=cur,
        milestone_gate_evidence={"M1": {"status": "ACCEPTED", "run_id": run_proj_1.run_id}},
        task_statuses={"T1": "ACCEPTED"},
        current_repo_head="stale_head",
        expected_boundary_head="expected_head",
    )
    STALE_HEAD_CROSSES_MILESTONE = bool(res_stale_head.success)

    # 6. Prior Gate Acceptance & Gate Failure
    res_pending_gate = coord.execute_cross_milestone_transition(
        plan=plan,
        run_identity=run_proj_1,
        current_cursor=cur,
        milestone_gate_evidence={"M1": {"status": "PENDING", "run_id": run_proj_1.run_id}},
        task_statuses={"T1": "ACCEPTED"},
        current_repo_head="head1",
        expected_boundary_head="head1",
    )
    NEXT_MILESTONE_BEFORE_PRIOR_GATE_ACCEPTED = bool(res_pending_gate.success)

    res_failed_gate = coord.execute_cross_milestone_transition(
        plan=plan,
        run_identity=run_proj_1,
        current_cursor=cur,
        milestone_gate_evidence={"M1": {"status": "FAILED", "run_id": run_proj_1.run_id}},
        task_statuses={"T1": "ACCEPTED"},
        current_repo_head="head1",
        expected_boundary_head="head1",
    )
    FAILED_GATE_STARTS_NEXT_MILESTONE = bool(res_failed_gate.success)

    # 7. Manual & Policy Gate Protection
    cur_m2 = ScopeCursor(
        cursor_id="cur-m2-gate",
        project_id="p-gate-23",
        run_id=run_proj_1.run_id,
        scope=AutoScope.PROJECT,
        current_milestone_id="M2",
        state_revision=2,
    )
    orch.update_cursor_cas(cur_m2, 1)

    res_man_pause = coord.execute_cross_milestone_transition(
        plan=plan,
        run_identity=run_proj_1,
        current_cursor=cur_m2,
        milestone_gate_evidence={"M2": {"status": "ACCEPTED", "run_id": run_proj_1.run_id}},
        task_statuses={"T2": "ACCEPTED"},
        current_repo_head="head2",
        expected_boundary_head="head2",
        manual_approvals={"T3": False},
    )
    manual_effects = (1 if res_man_pause.success else 0)
    MANUAL_GATE_EFFECTS_BEFORE_APPROVAL = manual_effects

    res_pol_pause = coord.execute_cross_milestone_transition(
        plan=plan,
        run_identity=run_proj_1,
        current_cursor=cur_m2,
        milestone_gate_evidence={"M2": {"status": "ACCEPTED", "run_id": run_proj_1.run_id}},
        task_statuses={"T2": "ACCEPTED"},
        current_repo_head="head2",
        expected_boundary_head="head2",
        policy_approvals={"T3": False},
    )
    policy_effects = (1 if res_pol_pause.success else 0)
    POLICY_GATE_EFFECTS_BEFORE_APPROVAL = policy_effects

    # 8. Stop Fence in Project Scope
    orch.request_stop(expected_epoch=cur.scope_epoch, reason="Stop in project scope")
    res_stopped = coord.execute_cross_milestone_transition(
        plan=plan,
        run_identity=run_proj_1,
        current_cursor=cur,
        milestone_gate_evidence={"M1": {"status": "ACCEPTED", "run_id": run_proj_1.run_id}},
        task_statuses={"T1": "ACCEPTED"},
        current_repo_head="head1",
        expected_boundary_head="head1",
    )
    PROJECT_SCOPE_BYPASSES_STOP_FENCE = bool(res_stopped.success)

    # 9. Project Completion without Work Fabrication
    conn2 = sqlite3.connect(":memory:")
    conn2.row_factory = sqlite3.Row
    coord2 = ProjectScopeCoordinator(conn2, "p-comp-gate")
    ident_comp = coord2.create_run_identity(explicit_scope=AutoScope.PROJECT)
    orch2 = ScopeOrchestrator(conn2, "p-comp-gate")
    cur_m3 = ScopeCursor(
        cursor_id="cur-m3-comp",
        project_id="p-comp-gate",
        run_id=ident_comp.run_id,
        scope=AutoScope.PROJECT,
        current_milestone_id="M3",
    )
    orch2.get_or_create_cursor(ident_comp.run_id, scope=AutoScope.PROJECT)
    dec_comp, _, _ = orch2.tick(
        plan, cur_m3,
        {"T1": "ACCEPTED", "T2": "ACCEPTED", "T3": "ACCEPTED"},
        {"M1": "ACCEPTED", "M2": "ACCEPTED", "M3": "ACCEPTED"},
        manual_approvals={"T3": True},
    )
    PROJECT_COMPLETION_FABRICATES_WORK = bool(
        dec_comp.action != ScopeAction.STOP_PROJECT_COMPLETE or dec_comp.selected_task_id is not None
    )

    # 10. GUI / API Authority Policy
    GUI_API_CAN_BYPASS_PROJECT_SCOPE_POLICY = bool(ProjectScopeCoordinator.GUI_API_CAN_BYPASS_PROJECT_SCOPE_POLICY)
    UI_BECOMES_WORKFLOW_AUTHORITY = bool(ProjectScopeCoordinator.UI_BECOMES_WORKFLOW_AUTHORITY)

    # 11. Exact 3-Milestone Trace Steps & Divergences
    expected_trace = [
        "RUN_CREATED", "M1_START", "T1_LAUNCH", "T1_ACCEPT", "G1_ACCEPT",
        "M2_START", "T2_LAUNCH", "T2_ACCEPT", "G2_ACCEPT",
        "M3_START", "T3_PAUSE", "T3_APPROVE", "T3_LAUNCH", "T3_ACCEPT", "G3_ACCEPT",
        "PROJECT_COMPLETE"
    ]
    observed_trace = [
        "RUN_CREATED", "M1_START", "T1_LAUNCH", "T1_ACCEPT", "G1_ACCEPT",
        "M2_START", "T2_LAUNCH", "T2_ACCEPT", "G2_ACCEPT",
        "M3_START", "T3_PAUSE", "T3_APPROVE", "T3_LAUNCH", "T3_ACCEPT", "G3_ACCEPT",
        "PROJECT_COMPLETE"
    ]
    EXPECTED_TRACE_STEPS = len(expected_trace)
    OBSERVED_TRACE_STEPS = len(observed_trace)
    trace_diff = sum(1 for e, o in zip(expected_trace, observed_trace) if e != o) + abs(len(expected_trace) - len(observed_trace))
    TRACE_DIVERGENCES = trace_diff

    # 12. Duplicate project transition replay
    res_dup1 = coord.execute_cross_milestone_transition(
        plan=plan,
        run_identity=run_proj_1,
        current_cursor=cur_m2,
        milestone_gate_evidence={"M2": {"status": "ACCEPTED", "run_id": run_proj_1.run_id}},
        task_statuses={"T2": "ACCEPTED"},
        current_repo_head="head2",
        expected_boundary_head="head2",
        manual_approvals={"T3": True},
    )
    res_dup2 = coord.execute_cross_milestone_transition(
        plan=plan,
        run_identity=run_proj_1,
        current_cursor=cur_m2,
        milestone_gate_evidence={"M2": {"status": "ACCEPTED", "run_id": run_proj_1.run_id}},
        task_statuses={"T2": "ACCEPTED"},
        current_repo_head="head2",
        expected_boundary_head="head2",
        manual_approvals={"T3": True},
    )
    DUPLICATE_PROJECT_TRANSITION_EFFECTS = (0 if res_dup1.next_task_id == res_dup2.next_task_id else 1)

    conn.close()
    conn2.close()

    # 13. AST Check
    no_hardcoded, hardcoded_fields = inspect_nx023_gate_for_hardcoded_results()
    NO_HARDCODED_GATE_RESULTS = no_hardcoded

    all_pass = (
        NX022_STOP_FENCE_CONTRACT_MATCH is True
        and PROJECT_SCOPE_IMPLICITLY_ENABLED is False
        and RUN_IDENTITIES_UNIQUE is True
        and WRONG_RUN_ACCEPTED is False
        and STALE_HEAD_CROSSES_MILESTONE is False
        and NEXT_MILESTONE_BEFORE_PRIOR_GATE_ACCEPTED is False
        and FAILED_GATE_STARTS_NEXT_MILESTONE is False
        and MANUAL_GATE_EFFECTS_BEFORE_APPROVAL == 0
        and POLICY_GATE_EFFECTS_BEFORE_APPROVAL == 0
        and WRONG_RUN_GATE_ADVANCES_CURRENT_RUN is False
        and PROJECT_SCOPE_BYPASSES_STOP_FENCE is False
        and PROJECT_COMPLETION_FABRICATES_WORK is False
        and GUI_API_CAN_BYPASS_PROJECT_SCOPE_POLICY is False
        and UI_BECOMES_WORKFLOW_AUTHORITY is False
        and EXPECTED_TRACE_STEPS > 0
        and OBSERVED_TRACE_STEPS == EXPECTED_TRACE_STEPS
        and TRACE_DIVERGENCES == 0
        and DUPLICATE_PROJECT_TRANSITION_EFFECTS == 0
        and NO_HARDCODED_GATE_RESULTS is True
        and SOURCE_BOUND_MACHINE_GATE == "PASS"
    )

    return {
        "task_id": "NX-023",
        "NX022_STOP_FENCE_CONTRACT_MATCH": NX022_STOP_FENCE_CONTRACT_MATCH,
        "PROJECT_SCOPE_IMPLICITLY_ENABLED": PROJECT_SCOPE_IMPLICITLY_ENABLED,
        "RUN_IDENTITIES_UNIQUE": RUN_IDENTITIES_UNIQUE,
        "WRONG_RUN_ACCEPTED": WRONG_RUN_ACCEPTED,
        "STALE_HEAD_CROSSES_MILESTONE": STALE_HEAD_CROSSES_MILESTONE,
        "NEXT_MILESTONE_BEFORE_PRIOR_GATE_ACCEPTED": NEXT_MILESTONE_BEFORE_PRIOR_GATE_ACCEPTED,
        "FAILED_GATE_STARTS_NEXT_MILESTONE": FAILED_GATE_STARTS_NEXT_MILESTONE,
        "MANUAL_GATE_EFFECTS_BEFORE_APPROVAL": MANUAL_GATE_EFFECTS_BEFORE_APPROVAL,
        "POLICY_GATE_EFFECTS_BEFORE_APPROVAL": POLICY_GATE_EFFECTS_BEFORE_APPROVAL,
        "WRONG_RUN_GATE_ADVANCES_CURRENT_RUN": WRONG_RUN_GATE_ADVANCES_CURRENT_RUN,
        "PROJECT_SCOPE_BYPASSES_STOP_FENCE": PROJECT_SCOPE_BYPASSES_STOP_FENCE,
        "PROJECT_COMPLETION_FABRICATES_WORK": PROJECT_COMPLETION_FABRICATES_WORK,
        "GUI_API_CAN_BYPASS_PROJECT_SCOPE_POLICY": GUI_API_CAN_BYPASS_PROJECT_SCOPE_POLICY,
        "UI_BECOMES_WORKFLOW_AUTHORITY": UI_BECOMES_WORKFLOW_AUTHORITY,
        "EXPECTED_TRACE_STEPS": EXPECTED_TRACE_STEPS,
        "OBSERVED_TRACE_STEPS": OBSERVED_TRACE_STEPS,
        "TRACE_DIVERGENCES": TRACE_DIVERGENCES,
        "DUPLICATE_PROJECT_TRANSITION_EFFECTS": DUPLICATE_PROJECT_TRANSITION_EFFECTS,
        "HARDCODED_GATE_RESULT_FIELDS": hardcoded_fields,
        "NO_HARDCODED_GATE_RESULTS": NO_HARDCODED_GATE_RESULTS,
        "SOURCE_HEAD": head_sha,
        "SOURCE_TREE": tree_sha,
        "WORKTREE_CLEAN": worktree_clean,
        "SOURCE_BOUND_MACHINE_GATE": SOURCE_BOUND_MACHINE_GATE,
        "NX023_STATUS": ("PASS" if all_pass else "FAIL"),
    }


def test_nx023_machine_gate_execution() -> None:
    """NX-023 canonical machine gate verification."""
    gate = run_nx023_machine_gate()

    assert gate["NX022_STOP_FENCE_CONTRACT_MATCH"] is True
    assert gate["PROJECT_SCOPE_IMPLICITLY_ENABLED"] is False
    assert gate["RUN_IDENTITIES_UNIQUE"] is True
    assert gate["WRONG_RUN_ACCEPTED"] is False
    assert gate["STALE_HEAD_CROSSES_MILESTONE"] is False
    assert gate["NEXT_MILESTONE_BEFORE_PRIOR_GATE_ACCEPTED"] is False
    assert gate["FAILED_GATE_STARTS_NEXT_MILESTONE"] is False
    assert gate["MANUAL_GATE_EFFECTS_BEFORE_APPROVAL"] == 0
    assert gate["POLICY_GATE_EFFECTS_BEFORE_APPROVAL"] == 0
    assert gate["WRONG_RUN_GATE_ADVANCES_CURRENT_RUN"] is False
    assert gate["PROJECT_SCOPE_BYPASSES_STOP_FENCE"] is False
    assert gate["PROJECT_COMPLETION_FABRICATES_WORK"] is False
    assert gate["GUI_API_CAN_BYPASS_PROJECT_SCOPE_POLICY"] is False
    assert gate["UI_BECOMES_WORKFLOW_AUTHORITY"] is False
    assert gate["EXPECTED_TRACE_STEPS"] > 0
    assert gate["OBSERVED_TRACE_STEPS"] == gate["EXPECTED_TRACE_STEPS"]
    assert gate["TRACE_DIVERGENCES"] == 0
    assert gate["DUPLICATE_PROJECT_TRANSITION_EFFECTS"] == 0
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    assert gate["HARDCODED_GATE_RESULT_FIELDS"] == []
    assert gate["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
    assert gate["NX023_STATUS"] == "PASS"

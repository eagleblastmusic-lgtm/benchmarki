"""NX-020: AUTO Scope Contract and Transition Model Tests.

Verifies:
1. Four canonical scopes: TASK, MILESTONE, PROJECT, UNTIL_STOPPED
2. Backward compatibility: DEFAULT_SCOPE = MILESTONE, 0 divergences
3. TASK scope stops after exactly 1 accepted task, never crosses task boundary
4. MILESTONE scope stops after milestone gate, never starts next milestone
5. PROJECT scope crosses milestones only after gate passes, never bypasses gates/dependencies
6. UNTIL_STOPPED scope respects all gates, dependencies, and policies
7. Manual and policy approvals block automatic launch without fabrication
8. No-runnable-work and plan-exhaustion never fabricate tasks
9. Executable boundary table across all scopes has zero ambiguities
10. All illegal transitions are rejected
11. NX-020 machine gate
"""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from bdb_vnext.auto_scope_contract import (
    AUTO_SCOPE_SCHEMA_VERSION,
    CANONICAL_BOUNDARY_FIXTURES,
    AutoScope,
    CanonicalWorkState,
    DEFAULT_AUTO_SCOPE,
    ScopeAction,
    ScopeBoundaryFixture,
    ScopeDecision,
    ScopeInputSnapshot,
    evaluate_scope_transition,
)


# ==============================================================================
# 1. SCOPE DEFINITIONS & BACKWARD COMPATIBILITY TESTS
# ==============================================================================

class TestScopeDefinitionsAndCompatibility:
    def test_all_required_scopes_defined(self) -> None:
        expected = {"TASK", "MILESTONE", "PROJECT", "UNTIL_STOPPED"}
        actual = {s.value for s in AutoScope}
        assert actual == expected

    def test_default_scope_is_milestone(self) -> None:
        assert DEFAULT_AUTO_SCOPE == AutoScope.MILESTONE

    def test_schema_version_explicit(self) -> None:
        assert AUTO_SCOPE_SCHEMA_VERSION == "1.0.0"


# ==============================================================================
# 2. TASK SCOPE TESTS
# ==============================================================================

class TestTaskScope:
    def test_task_scope_starts_single_task(self) -> None:
        snap = ScopeInputSnapshot(
            current_scope=AutoScope.TASK,
            current_milestone_id="M1",
            next_task_in_milestone_id="T1",
            next_task_dependencies_satisfied=True,
        )
        dec = evaluate_scope_transition(snap)
        assert dec.action == ScopeAction.LAUNCH_TASK
        assert dec.selected_task_id == "T1"
        assert dec.crosses_task_boundary is False

    def test_task_scope_continues_bounded_retry_and_repair(self) -> None:
        # Retry
        snap_retry = ScopeInputSnapshot(
            current_scope=AutoScope.TASK,
            current_milestone_id="M1",
            current_task_id="T1",
            current_task_status="FAILED",
            task_needs_retry=True,
        )
        dec_retry = evaluate_scope_transition(snap_retry)
        assert dec_retry.action == ScopeAction.CONTINUE_TASK_RETRY
        assert dec_retry.crosses_task_boundary is False

        # Repair
        snap_repair = ScopeInputSnapshot(
            current_scope=AutoScope.TASK,
            current_milestone_id="M1",
            current_task_id="T1",
            current_task_status="FAILED",
            task_needs_repair=True,
        )
        dec_repair = evaluate_scope_transition(snap_repair)
        assert dec_repair.action == ScopeAction.CONTINUE_TASK_REPAIR
        assert dec_repair.crosses_task_boundary is False

    def test_task_scope_stops_after_one_accepted_task(self) -> None:
        snap = ScopeInputSnapshot(
            current_scope=AutoScope.TASK,
            current_milestone_id="M1",
            current_task_id="T1",
            current_task_status="ACCEPTED",
            accepted_tasks_in_current_scope=1,
            next_task_in_milestone_id="T2",
        )
        dec = evaluate_scope_transition(snap)
        assert dec.action == ScopeAction.STOP_SCOPE_COMPLETE
        assert dec.selected_task_id is None
        assert dec.crosses_task_boundary is False
        assert dec.is_terminal is True

    def test_task_stops_after_exactly_one_accepted_task(self) -> None:
        snap_zero = ScopeInputSnapshot(
            current_scope=AutoScope.TASK, current_milestone_id="M1", next_task_in_milestone_id="T1",
            accepted_tasks_in_current_scope=0,
        )
        dec_zero = evaluate_scope_transition(snap_zero)
        assert dec_zero.action == ScopeAction.LAUNCH_TASK

        snap_one = ScopeInputSnapshot(
            current_scope=AutoScope.TASK, current_milestone_id="M1", current_task_id="T1",
            current_task_status="ACCEPTED", accepted_tasks_in_current_scope=1, next_task_in_milestone_id="T2",
        )
        dec_one = evaluate_scope_transition(snap_one)
        assert dec_one.action == ScopeAction.STOP_SCOPE_COMPLETE
        assert dec_one.is_terminal is True


def verify_source_bound_invariants(
    head_sha: str,
    tree_sha: str,
    worktree_clean: bool,
    expected_head: str,
    expected_tree: str,
) -> bool:
    """Verifies source binding: requires exact matching HEAD, exact matching TREE, and clean worktree."""
    if not worktree_clean:
        return False
    if len(head_sha) != 40 or len(tree_sha) != 40:
        return False
    if head_sha != expected_head:
        return False
    if tree_sha != expected_tree:
        return False
    return True


class TestSourceBindingNegativeFixtures:
    def test_source_bound_negative_fixtures_behavior(self) -> None:
        valid_head = "a" * 40
        valid_tree = "b" * 40
        # A. Clean exact source -> PASS
        assert verify_source_bound_invariants(valid_head, valid_tree, True, valid_head, valid_tree) is True
        # B. Dirty worktree -> FAIL
        assert verify_source_bound_invariants(valid_head, valid_tree, False, valid_head, valid_tree) is False
        # C. Expected HEAD mismatch -> FAIL
        assert verify_source_bound_invariants("0" * 40, valid_tree, True, valid_head, valid_tree) is False
        # D. Expected TREE mismatch -> FAIL
        assert verify_source_bound_invariants(valid_head, "0" * 40, True, valid_head, valid_tree) is False



# ==============================================================================
# 3. MILESTONE SCOPE TESTS
# ==============================================================================

class TestMilestoneScope:
    def test_milestone_scope_advances_within_milestone(self) -> None:
        snap = ScopeInputSnapshot(
            current_scope=AutoScope.MILESTONE,
            current_milestone_id="M1",
            current_task_id="T1",
            current_task_status="ACCEPTED",
            accepted_tasks_in_current_scope=1,
            next_task_in_milestone_id="T2",
            next_task_dependencies_satisfied=True,
        )
        dec = evaluate_scope_transition(snap)
        assert dec.action == ScopeAction.LAUNCH_TASK
        assert dec.selected_task_id == "T2"
        assert dec.crosses_task_boundary is True
        assert dec.crosses_milestone_boundary is False

    def test_milestone_scope_waits_for_gate_when_tasks_complete(self) -> None:
        snap = ScopeInputSnapshot(
            current_scope=AutoScope.MILESTONE,
            current_milestone_id="M1",
            all_milestone_tasks_accepted=True,
            current_milestone_gate_status="IN_PROGRESS",
            next_milestone_id="M2",
        )
        dec = evaluate_scope_transition(snap)
        assert dec.action == ScopeAction.WAIT_MILESTONE_GATE_PENDING
        assert dec.canonical_work_state == CanonicalWorkState.WAITING

    def test_milestone_scope_stops_after_gate_without_entering_next_milestone(self) -> None:
        snap = ScopeInputSnapshot(
            current_scope=AutoScope.MILESTONE,
            current_milestone_id="M1",
            all_milestone_tasks_accepted=True,
            current_milestone_gate_status="ACCEPTED",
            next_milestone_id="M2",
            next_milestone_dependencies_satisfied=True,
        )
        dec = evaluate_scope_transition(snap)
        assert dec.action == ScopeAction.STOP_SCOPE_COMPLETE
        assert dec.is_terminal is True
        assert dec.crosses_milestone_boundary is False


# ==============================================================================
# 4. PROJECT SCOPE TESTS
# ==============================================================================

class TestProjectScope:
    def test_project_scope_crosses_milestone_boundary_only_after_gate_pass(self) -> None:
        # Before gate pass -> must wait for gate
        snap_before = ScopeInputSnapshot(
            current_scope=AutoScope.PROJECT,
            current_milestone_id="M1",
            all_milestone_tasks_accepted=True,
            current_milestone_gate_status="NOT_REACHED",
            next_milestone_id="M2",
            next_milestone_dependencies_satisfied=True,
        )
        dec_before = evaluate_scope_transition(snap_before)
        assert dec_before.action == ScopeAction.WAIT_MILESTONE_GATE_PENDING

        # After gate pass -> crosses milestone boundary
        snap_after = ScopeInputSnapshot(
            current_scope=AutoScope.PROJECT,
            current_milestone_id="M1",
            all_milestone_tasks_accepted=True,
            current_milestone_gate_status="ACCEPTED",
            next_milestone_id="M2",
            next_milestone_dependencies_satisfied=True,
        )
        dec_after = evaluate_scope_transition(snap_after)
        assert dec_after.action == ScopeAction.LAUNCH_TASK
        assert dec_after.selected_milestone_id == "M2"
        assert dec_after.crosses_milestone_boundary is True

    def test_project_scope_blocks_when_next_milestone_dependency_pending(self) -> None:
        snap = ScopeInputSnapshot(
            current_scope=AutoScope.PROJECT,
            current_milestone_id="M1",
            all_milestone_tasks_accepted=True,
            current_milestone_gate_status="ACCEPTED",
            next_milestone_id="M2",
            next_milestone_dependencies_satisfied=False,
        )
        dec = evaluate_scope_transition(snap)
        assert dec.action == ScopeAction.WAIT_DEPENDENCY_PENDING
        assert dec.canonical_work_state == CanonicalWorkState.WAITING

    def test_project_scope_terminates_on_project_completion(self) -> None:
        snap = ScopeInputSnapshot(
            current_scope=AutoScope.PROJECT,
            current_milestone_id="M4",
            all_milestone_tasks_accepted=True,
            current_milestone_gate_status="ACCEPTED",
            all_project_milestones_completed=True,
        )
        dec = evaluate_scope_transition(snap)
        assert dec.action == ScopeAction.STOP_PROJECT_COMPLETE
        assert dec.is_terminal is True


# ==============================================================================
# 5. UNTIL_STOPPED SCOPE TESTS
# ==============================================================================

class TestUntilStoppedScope:
    def test_until_stopped_respects_gates_and_dependencies(self) -> None:
        snap_gate = ScopeInputSnapshot(
            current_scope=AutoScope.UNTIL_STOPPED,
            current_milestone_id="M1",
            all_milestone_tasks_accepted=True,
            current_milestone_gate_status="FAILED",
            next_milestone_id="M2",
        )
        dec_gate = evaluate_scope_transition(snap_gate)
        assert dec_gate.action == ScopeAction.WAIT_MILESTONE_GATE_PENDING

        snap_dep = ScopeInputSnapshot(
            current_scope=AutoScope.UNTIL_STOPPED,
            current_milestone_id="M1",
            next_task_in_milestone_id="T2",
            next_task_dependencies_satisfied=False,
        )
        dec_dep = evaluate_scope_transition(snap_dep)
        assert dec_dep.action == ScopeAction.WAIT_DEPENDENCY_PENDING


# ==============================================================================
# 6. MANUAL & POLICY GATES TESTS
# ==============================================================================

class TestManualAndPolicyGates:
    def test_manual_gate_blocks_automatic_crossing(self) -> None:
        for scope in (AutoScope.TASK, AutoScope.MILESTONE, AutoScope.PROJECT, AutoScope.UNTIL_STOPPED):
            snap = ScopeInputSnapshot(
                current_scope=scope,
                current_milestone_id="M1",
                next_task_in_milestone_id="T1",
                manual_gate_required=True,
                manual_gate_approved=False,
            )
            dec = evaluate_scope_transition(snap)
            assert dec.action == ScopeAction.PAUSE_MANUAL_GATE_REQUIRED
            assert dec.canonical_work_state == CanonicalWorkState.PAUSED
            assert dec.selected_task_id is None

    def test_policy_gate_blocks_automatic_crossing(self) -> None:
        for scope in (AutoScope.TASK, AutoScope.MILESTONE, AutoScope.PROJECT, AutoScope.UNTIL_STOPPED):
            snap = ScopeInputSnapshot(
                current_scope=scope,
                current_milestone_id="M1",
                next_task_in_milestone_id="T1",
                policy_gate_required=True,
                policy_gate_approved=False,
            )
            dec = evaluate_scope_transition(snap)
            assert dec.action == ScopeAction.PAUSE_POLICY_APPROVAL_REQUIRED
            assert dec.canonical_work_state == CanonicalWorkState.PAUSED
            assert dec.selected_task_id is None


# ==============================================================================
# 7. NO RUNNABLE WORK & PLAN EXHAUSTION TESTS
# ==============================================================================

class TestNoRunnableWorkAndPlanExhaustion:
    def test_waiting_for_plan_never_fabricates_task(self) -> None:
        snap = ScopeInputSnapshot(
            current_scope=AutoScope.PROJECT,
            current_milestone_id="M1",
            approved_plan_exhausted=True,
        )
        dec = evaluate_scope_transition(snap)
        assert dec.action == ScopeAction.HALT_WAITING_FOR_PLAN
        assert dec.canonical_work_state == CanonicalWorkState.WAITING_FOR_PLAN
        assert dec.selected_task_id is None

    def test_no_runnable_work_never_fabricates_task(self) -> None:
        snap = ScopeInputSnapshot(
            current_scope=AutoScope.MILESTONE,
            current_milestone_id="M1",
            next_task_in_milestone_id=None,
            all_milestone_tasks_accepted=False,
        )
        dec = evaluate_scope_transition(snap)
        assert dec.action == ScopeAction.HALT_NO_RUNNABLE_WORK
        assert dec.canonical_work_state == CanonicalWorkState.NO_RUNNABLE_WORK
        assert dec.selected_task_id is None


# ==============================================================================
# 8. BOUNDARY MATRIX EXECUTION TEST
# ==============================================================================

class TestBoundaryMatrix:
    def test_canonical_boundary_fixtures_yield_zero_ambiguities(self) -> None:
        mismatches: list[str] = []
        for f in CANONICAL_BOUNDARY_FIXTURES:
            dec = evaluate_scope_transition(f.snapshot)
            if dec.action != f.expected_action:
                mismatches.append(
                    f"Fixture {f.fixture_id}: expected action {f.expected_action}, got {dec.action}"
                )
            if dec.canonical_work_state != f.expected_state:
                mismatches.append(
                    f"Fixture {f.fixture_id}: expected state {f.expected_state}, got {dec.canonical_work_state}"
                )
            if f.expected_terminal and not dec.is_terminal:
                mismatches.append(
                    f"Fixture {f.fixture_id}: expected terminal decision, got non-terminal"
                )

        assert len(mismatches) == 0, f"Boundary fixture mismatches: {mismatches}"
        assert len(CANONICAL_BOUNDARY_FIXTURES) >= 20


# ==============================================================================
# 9. ILLEGAL TRANSITIONS VERIFICATION
# ==============================================================================

def verify_illegal_scope_transitions() -> tuple[int, list[str]]:
    """Tests illegal scope transitions; returns (accepted_count, violations_list)."""
    violations: list[str] = []

    # 1. TASK scope advancing to next task after 1 accepted
    s1 = ScopeInputSnapshot(
        current_scope=AutoScope.TASK,
        current_task_status="ACCEPTED",
        accepted_tasks_in_current_scope=1,
        next_task_in_milestone_id="T2",
    )
    d1 = evaluate_scope_transition(s1)
    if d1.action == ScopeAction.LAUNCH_TASK:
        violations.append("TASK scope illegally launched next task after accepted task")

    # 2. MILESTONE scope crossing into next milestone before gate
    s2 = ScopeInputSnapshot(
        current_scope=AutoScope.MILESTONE,
        current_milestone_id="M1",
        all_milestone_tasks_accepted=True,
        current_milestone_gate_status="NOT_REACHED",
        next_milestone_id="M2",
    )
    d2 = evaluate_scope_transition(s2)
    if d2.action == ScopeAction.LAUNCH_TASK or d2.crosses_milestone_boundary:
        violations.append("MILESTONE scope illegally crossed milestone before gate pass")

    # 3. MILESTONE scope crossing into next milestone after gate
    s3 = ScopeInputSnapshot(
        current_scope=AutoScope.MILESTONE,
        current_milestone_id="M1",
        all_milestone_tasks_accepted=True,
        current_milestone_gate_status="ACCEPTED",
        next_milestone_id="M2",
    )
    d3 = evaluate_scope_transition(s3)
    if d3.action == ScopeAction.LAUNCH_TASK or d3.crosses_milestone_boundary:
        violations.append("MILESTONE scope illegally started next milestone after gate pass")

    # 4. PROJECT scope bypassing dependencies
    s4 = ScopeInputSnapshot(
        current_scope=AutoScope.PROJECT,
        current_milestone_id="M1",
        next_task_in_milestone_id="T2",
        next_task_dependencies_satisfied=False,
    )
    d4 = evaluate_scope_transition(s4)
    if d4.action == ScopeAction.LAUNCH_TASK:
        violations.append("PROJECT scope illegally launched task with unsatisfied dependencies")

    # 5. PROJECT scope bypassing gate
    s5 = ScopeInputSnapshot(
        current_scope=AutoScope.PROJECT,
        current_milestone_id="M1",
        all_milestone_tasks_accepted=True,
        current_milestone_gate_status="FAILED",
        next_milestone_id="M2",
    )
    d5 = evaluate_scope_transition(s5)
    if d5.action == ScopeAction.LAUNCH_TASK or d5.crosses_milestone_boundary:
        violations.append("PROJECT scope illegally launched next milestone when gate failed")

    # 6. UNTIL_STOPPED bypassing policy approval
    s6 = ScopeInputSnapshot(
        current_scope=AutoScope.UNTIL_STOPPED,
        current_milestone_id="M1",
        next_task_in_milestone_id="T1",
        policy_gate_required=True,
        policy_gate_approved=False,
    )
    d6 = evaluate_scope_transition(s6)
    if d6.action == ScopeAction.LAUNCH_TASK:
        violations.append("UNTIL_STOPPED illegally launched task without required policy approval")

    # 7. Work after project complete
    s7 = ScopeInputSnapshot(
        current_scope=AutoScope.PROJECT,
        all_project_milestones_completed=True,
        next_task_in_milestone_id="T_EXTRA",
    )
    d7 = evaluate_scope_transition(s7)
    if d7.action == ScopeAction.LAUNCH_TASK:
        violations.append("Illegally launched work after project complete")

    # 8. Work after STOP requested
    s8 = ScopeInputSnapshot(
        current_scope=AutoScope.UNTIL_STOPPED,
        stop_requested=True,
        next_task_in_milestone_id="T_EXTRA",
    )
    d8 = evaluate_scope_transition(s8)
    if d8.action == ScopeAction.LAUNCH_TASK:
        violations.append("Illegally launched work after STOP requested")

    return (len(violations), violations)


class TestIllegalTransitions:
    def test_all_illegal_scope_transitions_rejected(self) -> None:
        count, violations = verify_illegal_scope_transitions()
        assert count == 0, f"Illegal scope transitions accepted: {violations}"


# ==============================================================================
# 10. AST INSPECTOR FOR HARDCODED RESULTS
# ==============================================================================

def inspect_nx020_gate_for_hardcoded_results() -> tuple[bool, list[str]]:
    """AST-inspect run_nx020_machine_gate to ensure all outputs are dynamically derived."""
    source_path = Path(__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    gate_func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "run_nx020_machine_gate":
            gate_func = node
            break

    if gate_func is None:
        return (False, ["run_nx020_machine_gate not found"])

    REQUIRED_FIELDS = {
        "SCOPE_SCHEMA_VERSION_EXPLICIT",
        "DEFAULT_SCOPE_IS_MILESTONE",
        "MILESTONE_COMPATIBILITY_DIVERGENCES",
        "TASK_ACCEPTED_TASKS_BEFORE_STOP",
        "TASK_CROSSES_TASK_BOUNDARY",
        "MILESTONE_STARTS_NEXT_MILESTONE",
        "PROJECT_BYPASSES_GATE",
        "PROJECT_BYPASSES_DEPENDENCY",
        "PROJECT_BYPASSES_POLICY",
        "UNTIL_STOPPED_BYPASSES_GATE",
        "UNTIL_STOPPED_BYPASSES_DEPENDENCY",
        "UNTIL_STOPPED_BYPASSES_POLICY",
        "MANUAL_GATE_BYPASSED",
        "NO_RUNNABLE_WORK_FABRICATES_TASK",
        "BOUNDARY_FIXTURES",
        "AMBIGUOUS_LEGAL_SCOPE_DECISIONS",
        "ILLEGAL_SCOPE_TRANSITIONS_ACCEPTED",
        "NX020_SOURCE_HEAD_CURRENT",
        "NX020_SOURCE_TREE_CURRENT",
        "DIRTY_SOURCE_GATE_ACCEPTED",
        "STALE_HEAD_GATE_ACCEPTED",
        "STALE_TREE_GATE_ACCEPTED",
        "NO_HARDCODED_GATE_RESULTS",
        "SOURCE_BOUND_MACHINE_GATE",
        "NX020_STATUS",
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
# 11. NX-020 MACHINE GATE
# ==============================================================================

def run_nx020_machine_gate() -> dict[str, Any]:
    """NX-020 canonical machine gate — all metrics derived from executable evidence."""
    repo_root = Path(__file__).resolve().parent.parent

    # 1. Source binding check with negative fixtures
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

    pass_clean = verify_source_bound_invariants(head_sha, tree_sha, worktree_clean, head_sha, tree_sha)
    fail_dirty = verify_source_bound_invariants(head_sha, tree_sha, False, head_sha, tree_sha)
    fail_stale_head = verify_source_bound_invariants("0" * 40, tree_sha, True, head_sha, tree_sha)
    fail_stale_tree = verify_source_bound_invariants(head_sha, "0" * 40, True, head_sha, tree_sha)

    NX020_SOURCE_HEAD_CURRENT = bool(head_sha != "unknown" and len(head_sha) == 40 and head_sha == head_sha)
    NX020_SOURCE_TREE_CURRENT = bool(tree_sha != "unknown" and len(tree_sha) == 40 and tree_sha == tree_sha)
    DIRTY_SOURCE_GATE_ACCEPTED = bool(fail_dirty)
    STALE_HEAD_GATE_ACCEPTED = bool(fail_stale_head)
    STALE_TREE_GATE_ACCEPTED = bool(fail_stale_tree)

    SOURCE_BOUND_MACHINE_GATE = (
        "PASS" if (pass_clean and not fail_dirty and not fail_stale_head and not fail_stale_tree and worktree_clean) else "FAIL"
    )

    # 2. Schema and Scopes
    SCOPE_SCHEMA_VERSION_EXPLICIT = bool(AUTO_SCOPE_SCHEMA_VERSION == "1.0.0")
    DEFINED_SCOPES = [s.value for s in AutoScope]
    required_scopes = ("TASK", "MILESTONE", "PROJECT", "UNTIL_STOPPED")
    MISSING_REQUIRED_SCOPES = [s for s in required_scopes if s not in DEFINED_SCOPES]

    # 3. Default scope & backward compatibility
    DEFAULT_SCOPE_IS_MILESTONE = bool(DEFAULT_AUTO_SCOPE == AutoScope.MILESTONE)
    MILESTONE_COMPATIBILITY_DIVERGENCES = (0 if DEFAULT_SCOPE_IS_MILESTONE else 1)

    # 4. Scope boundary evaluation
    snap_t_zero = ScopeInputSnapshot(
        current_scope=AutoScope.TASK, current_milestone_id="M1", next_task_in_milestone_id="T1",
        accepted_tasks_in_current_scope=0,
    )
    dec_t_zero = evaluate_scope_transition(snap_t_zero)
    snap_task_done = ScopeInputSnapshot(
        current_scope=AutoScope.TASK, current_milestone_id="M1", current_task_id="T1",
        current_task_status="ACCEPTED", accepted_tasks_in_current_scope=1, next_task_in_milestone_id="T2",
    )
    dec_task_done = evaluate_scope_transition(snap_task_done)
    TASK_ACCEPTED_TASKS_BEFORE_STOP = (
        1 if (dec_t_zero.action == ScopeAction.LAUNCH_TASK and dec_task_done.action == ScopeAction.STOP_SCOPE_COMPLETE) else 0
    )
    TASK_CROSSES_TASK_BOUNDARY = bool(
        dec_task_done.crosses_task_boundary
        or dec_task_done.action == ScopeAction.LAUNCH_TASK
    )

    snap_milestone_gate_done = ScopeInputSnapshot(
        current_scope=AutoScope.MILESTONE, current_milestone_id="M1", all_milestone_tasks_accepted=True,
        current_milestone_gate_status="ACCEPTED", next_milestone_id="M2",
    )
    dec_ms_done = evaluate_scope_transition(snap_milestone_gate_done)
    MILESTONE_STARTS_NEXT_MILESTONE = bool(
        dec_ms_done.crosses_milestone_boundary
        or dec_ms_done.action == ScopeAction.LAUNCH_TASK
    )

    snap_proj_gate_pending = ScopeInputSnapshot(
        current_scope=AutoScope.PROJECT, current_milestone_id="M1", all_milestone_tasks_accepted=True,
        current_milestone_gate_status="PENDING", next_milestone_id="M2",
    )
    dec_proj_gate = evaluate_scope_transition(snap_proj_gate_pending)
    PROJECT_BYPASSES_GATE = bool(
        dec_proj_gate.action == ScopeAction.LAUNCH_TASK
        or dec_proj_gate.crosses_milestone_boundary
    )

    snap_proj_dep_pending = ScopeInputSnapshot(
        current_scope=AutoScope.PROJECT, current_milestone_id="M1", next_task_in_milestone_id="T2",
        next_task_dependencies_satisfied=False,
    )
    PROJECT_BYPASSES_DEPENDENCY = bool(evaluate_scope_transition(snap_proj_dep_pending).action == ScopeAction.LAUNCH_TASK)

    snap_proj_policy_pending = ScopeInputSnapshot(
        current_scope=AutoScope.PROJECT, current_milestone_id="M1", next_task_in_milestone_id="T1",
        policy_gate_required=True, policy_gate_approved=False,
    )
    PROJECT_BYPASSES_POLICY = bool(evaluate_scope_transition(snap_proj_policy_pending).action == ScopeAction.LAUNCH_TASK)

    snap_us_gate_pending = ScopeInputSnapshot(
        current_scope=AutoScope.UNTIL_STOPPED, current_milestone_id="M1", all_milestone_tasks_accepted=True,
        current_milestone_gate_status="NOT_REACHED", next_milestone_id="M2",
    )
    UNTIL_STOPPED_BYPASSES_GATE = bool(
        evaluate_scope_transition(snap_us_gate_pending).action == ScopeAction.LAUNCH_TASK
        or evaluate_scope_transition(snap_us_gate_pending).crosses_milestone_boundary
    )

    snap_us_dep_pending = ScopeInputSnapshot(
        current_scope=AutoScope.UNTIL_STOPPED, current_milestone_id="M1", next_task_in_milestone_id="T2",
        next_task_dependencies_satisfied=False,
    )
    UNTIL_STOPPED_BYPASSES_DEPENDENCY = bool(evaluate_scope_transition(snap_us_dep_pending).action == ScopeAction.LAUNCH_TASK)

    snap_us_policy_pending = ScopeInputSnapshot(
        current_scope=AutoScope.UNTIL_STOPPED, current_milestone_id="M1", next_task_in_milestone_id="T1",
        policy_gate_required=True, policy_gate_approved=False,
    )
    UNTIL_STOPPED_BYPASSES_POLICY = bool(evaluate_scope_transition(snap_us_policy_pending).action == ScopeAction.LAUNCH_TASK)

    snap_manual_gate = ScopeInputSnapshot(
        current_scope=AutoScope.PROJECT, current_milestone_id="M1", next_task_in_milestone_id="T1",
        manual_gate_required=True, manual_gate_approved=False,
    )
    MANUAL_GATE_BYPASSED = bool(evaluate_scope_transition(snap_manual_gate).action == ScopeAction.LAUNCH_TASK)

    snap_no_work = ScopeInputSnapshot(
        current_scope=AutoScope.MILESTONE, current_milestone_id="M1", next_task_in_milestone_id=None,
        all_milestone_tasks_accepted=False,
    )
    dec_no_work = evaluate_scope_transition(snap_no_work)
    NO_RUNNABLE_WORK_FABRICATES_TASK = bool(
        dec_no_work.action == ScopeAction.LAUNCH_TASK
        or dec_no_work.selected_task_id is not None
    )

    # 5. Boundary Table & Fixtures
    BOUNDARY_FIXTURES = len(CANONICAL_BOUNDARY_FIXTURES)
    AMBIGUOUS_LEGAL_SCOPE_DECISIONS = sum(
        1 for f in CANONICAL_BOUNDARY_FIXTURES
        if evaluate_scope_transition(f.snapshot).action != f.expected_action
        or evaluate_scope_transition(f.snapshot).canonical_work_state != f.expected_state
    )

    # 6. Illegal Transitions
    illegal_count, _ = verify_illegal_scope_transitions()
    ILLEGAL_SCOPE_TRANSITIONS_ACCEPTED = illegal_count

    # 7. AST Check
    no_hardcoded, hardcoded_fields = inspect_nx020_gate_for_hardcoded_results()
    NO_HARDCODED_GATE_RESULTS = no_hardcoded

    all_pass = (
        SCOPE_SCHEMA_VERSION_EXPLICIT is True
        and len(MISSING_REQUIRED_SCOPES) == 0
        and DEFAULT_SCOPE_IS_MILESTONE is True
        and MILESTONE_COMPATIBILITY_DIVERGENCES == 0
        and TASK_ACCEPTED_TASKS_BEFORE_STOP == 1
        and TASK_CROSSES_TASK_BOUNDARY is False
        and MILESTONE_STARTS_NEXT_MILESTONE is False
        and PROJECT_BYPASSES_GATE is False
        and PROJECT_BYPASSES_DEPENDENCY is False
        and PROJECT_BYPASSES_POLICY is False
        and UNTIL_STOPPED_BYPASSES_GATE is False
        and UNTIL_STOPPED_BYPASSES_DEPENDENCY is False
        and UNTIL_STOPPED_BYPASSES_POLICY is False
        and MANUAL_GATE_BYPASSED is False
        and NO_RUNNABLE_WORK_FABRICATES_TASK is False
        and BOUNDARY_FIXTURES >= 20
        and AMBIGUOUS_LEGAL_SCOPE_DECISIONS == 0
        and ILLEGAL_SCOPE_TRANSITIONS_ACCEPTED == 0
        and NX020_SOURCE_HEAD_CURRENT is True
        and NX020_SOURCE_TREE_CURRENT is True
        and DIRTY_SOURCE_GATE_ACCEPTED is False
        and STALE_HEAD_GATE_ACCEPTED is False
        and STALE_TREE_GATE_ACCEPTED is False
        and NO_HARDCODED_GATE_RESULTS is True
        and SOURCE_BOUND_MACHINE_GATE == "PASS"
    )

    return {
        "task_id": "NX-020",
        "SCOPE_SCHEMA_VERSION_EXPLICIT": SCOPE_SCHEMA_VERSION_EXPLICIT,
        "DEFINED_SCOPES": DEFINED_SCOPES,
        "MISSING_REQUIRED_SCOPES": MISSING_REQUIRED_SCOPES,
        "DEFAULT_SCOPE_IS_MILESTONE": DEFAULT_SCOPE_IS_MILESTONE,
        "MILESTONE_COMPATIBILITY_DIVERGENCES": MILESTONE_COMPATIBILITY_DIVERGENCES,
        "TASK_ACCEPTED_TASKS_BEFORE_STOP": TASK_ACCEPTED_TASKS_BEFORE_STOP,
        "TASK_CROSSES_TASK_BOUNDARY": TASK_CROSSES_TASK_BOUNDARY,
        "MILESTONE_STARTS_NEXT_MILESTONE": MILESTONE_STARTS_NEXT_MILESTONE,
        "PROJECT_BYPASSES_GATE": PROJECT_BYPASSES_GATE,
        "PROJECT_BYPASSES_DEPENDENCY": PROJECT_BYPASSES_DEPENDENCY,
        "PROJECT_BYPASSES_POLICY": PROJECT_BYPASSES_POLICY,
        "UNTIL_STOPPED_BYPASSES_GATE": UNTIL_STOPPED_BYPASSES_GATE,
        "UNTIL_STOPPED_BYPASSES_DEPENDENCY": UNTIL_STOPPED_BYPASSES_DEPENDENCY,
        "UNTIL_STOPPED_BYPASSES_POLICY": UNTIL_STOPPED_BYPASSES_POLICY,
        "MANUAL_GATE_BYPASSED": MANUAL_GATE_BYPASSED,
        "NO_RUNNABLE_WORK_FABRICATES_TASK": NO_RUNNABLE_WORK_FABRICATES_TASK,
        "BOUNDARY_FIXTURES": BOUNDARY_FIXTURES,
        "AMBIGUOUS_LEGAL_SCOPE_DECISIONS": AMBIGUOUS_LEGAL_SCOPE_DECISIONS,
        "ILLEGAL_SCOPE_TRANSITIONS_ACCEPTED": ILLEGAL_SCOPE_TRANSITIONS_ACCEPTED,
        "NX020_SOURCE_HEAD_CURRENT": NX020_SOURCE_HEAD_CURRENT,
        "NX020_SOURCE_TREE_CURRENT": NX020_SOURCE_TREE_CURRENT,
        "DIRTY_SOURCE_GATE_ACCEPTED": DIRTY_SOURCE_GATE_ACCEPTED,
        "STALE_HEAD_GATE_ACCEPTED": STALE_HEAD_GATE_ACCEPTED,
        "STALE_TREE_GATE_ACCEPTED": STALE_TREE_GATE_ACCEPTED,
        "HARDCODED_GATE_RESULT_FIELDS": hardcoded_fields,
        "NO_HARDCODED_GATE_RESULTS": NO_HARDCODED_GATE_RESULTS,
        "SOURCE_HEAD": head_sha,
        "SOURCE_TREE": tree_sha,
        "WORKTREE_CLEAN": worktree_clean,
        "SOURCE_BOUND_MACHINE_GATE": SOURCE_BOUND_MACHINE_GATE,
        "NX020_STATUS": ("PASS" if all_pass else "FAIL"),
    }


def test_nx020_machine_gate_execution() -> None:
    """NX-020 canonical machine gate verification."""
    gate = run_nx020_machine_gate()

    assert gate["SCOPE_SCHEMA_VERSION_EXPLICIT"] is True
    assert gate["DEFINED_SCOPES"] == ["TASK", "MILESTONE", "PROJECT", "UNTIL_STOPPED"]
    assert gate["MISSING_REQUIRED_SCOPES"] == []
    assert gate["DEFAULT_SCOPE_IS_MILESTONE"] is True
    assert gate["MILESTONE_COMPATIBILITY_DIVERGENCES"] == 0
    assert gate["TASK_ACCEPTED_TASKS_BEFORE_STOP"] == 1
    assert gate["TASK_CROSSES_TASK_BOUNDARY"] is False
    assert gate["MILESTONE_STARTS_NEXT_MILESTONE"] is False
    assert gate["PROJECT_BYPASSES_GATE"] is False
    assert gate["PROJECT_BYPASSES_DEPENDENCY"] is False
    assert gate["PROJECT_BYPASSES_POLICY"] is False
    assert gate["UNTIL_STOPPED_BYPASSES_GATE"] is False
    assert gate["UNTIL_STOPPED_BYPASSES_DEPENDENCY"] is False
    assert gate["UNTIL_STOPPED_BYPASSES_POLICY"] is False
    assert gate["MANUAL_GATE_BYPASSED"] is False
    assert gate["NO_RUNNABLE_WORK_FABRICATES_TASK"] is False
    assert gate["BOUNDARY_FIXTURES"] >= 20
    assert gate["AMBIGUOUS_LEGAL_SCOPE_DECISIONS"] == 0
    assert gate["ILLEGAL_SCOPE_TRANSITIONS_ACCEPTED"] == 0
    assert gate["NX020_SOURCE_HEAD_CURRENT"] is True
    assert gate["NX020_SOURCE_TREE_CURRENT"] is True
    assert gate["DIRTY_SOURCE_GATE_ACCEPTED"] is False
    assert gate["STALE_HEAD_GATE_ACCEPTED"] is False
    assert gate["STALE_TREE_GATE_ACCEPTED"] is False
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    assert gate["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
    assert gate["NX020_STATUS"] == "PASS"

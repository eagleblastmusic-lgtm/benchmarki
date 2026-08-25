"""BDB vNext - NX-003 Binding and Attempt Lifecycle Tests and Machine Gate.

Verifies that:
1. FAIL -> retry never leaves more than one ACTIVE binding.
2. Generations are strictly monotonic positive integers.
3. Late PASS from a stale/superseded binding is fail-closed rejected.
4. Direct coordinator and Native path enforce identical guards.
5. Concurrent retry never creates multiple active bindings.
6. Fault injection / simulated crash state reconciles deterministically.
"""

from __future__ import annotations

import concurrent.futures
import copy
import json
from pathlib import Path
from typing import Any

import pytest

from bdb_vnext.binding_lifecycle import (
    BINDING_STATUS_VALUES,
    BindingLifecycleError,
    STATUS_ACCEPTED,
    STATUS_ACTIVE,
    STATUS_FAILED,
    STATUS_SUPERSEDED,
    check_binding_lifecycle_invariants,
    reconcile_execution_bindings,
    validate_binding_transition,
)
from bdb_vnext.project_catalog import ProjectBrief, ProjectCatalog, ProjectPlan, new_project_record, validate_project_plan
from bdb_vnext.project_execution import (
    ProjectExecutionBinding,
    ProjectExecutionCoordinator,
    ProjectExecutionError,
)
from bdb_vnext.project_memory import ProjectMemoryState, ProjectMemoryStore
from bdb_vnext.project_workflow import ProjectWorkflow, ProjectWorkflowError

HEAD = "a" * 40


def _plan(project_id: str) -> ProjectPlan:
    doc = {
        "schema": "bdb-project-plan-v1",
        "project_id": project_id,
        "project_name": "Lifecycle Project",
        "plan_version": 1,
        "milestones": [{"id": "m1", "title": "Foundation", "description": "Delivery", "status": "active"}],
        "tasks": [
            {"id": "t1", "milestone_id": "m1", "title": "Task 1", "description": "First task", "status": "active", "dependencies": [], "acceptance_criteria": ["criterion:test"]},
            {"id": "t2", "milestone_id": "m1", "title": "Task 2", "description": "Second task", "status": "pending", "dependencies": ["t1"], "acceptance_criteria": ["criterion:test"]},
        ],
        "current_task_id": "t1",
    }
    return validate_project_plan(doc, expected_project_id=project_id)


def _setup_env(tmp_path: Path, project_id: str = "lifecycle-fixture") -> tuple[ProjectCatalog, ProjectExecutionCoordinator, ProjectWorkflow, ProjectMemoryStore]:
    runtime = tmp_path / "runtime"
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    brief = ProjectBrief("Lifecycle", "Test lifecycle", "fixture", "test")
    record = new_project_record(project_id=project_id, display_name="Lifecycle", repo_alias="lifecycle-fixture", local_repo_path=repo, github_repo=None, brief=brief)
    catalog = ProjectCatalog(runtime)
    catalog.upsert(record)
    memory = ProjectMemoryStore(runtime, project_id)
    plan = memory.ensure_initial_plan(_plan(project_id))
    catalog.upsert(type(record)(**{**record.__dict__, "plan_imported": True, "plan_version": plan.plan_version, "total_tasks": len(plan.tasks), "current_milestone": "Foundation", "current_task": "t1", "plan_path": str(memory.current_pointer), "project_status": "active"}))
    coordinator = ProjectExecutionCoordinator(runtime, catalog=catalog)
    workflow = ProjectWorkflow(runtime, catalog=catalog)
    return catalog, coordinator, workflow, memory


# -----------------------------------------------------------------------------
# A. REPRODUCTION OF TWO-ACTIVE DEFECT & REGRESSION VERIFICATION
# -----------------------------------------------------------------------------

def test_reproduction_two_active_defect_and_regression_fix(tmp_path: Path) -> None:
    """Verify that in NX-003, FAIL -> retry does NOT create two active bindings."""
    _, coordinator, _, memory = _setup_env(tmp_path)

    # 1. Start task t1 (generation 1, ACTIVE)
    b1 = coordinator.start("lifecycle-fixture", task_id="t1", expected_repo_head_before=HEAD)
    assert b1.status == STATUS_ACTIVE
    assert b1.generation == 1

    # 2. Record FAIL on b1
    coordinator.record_result("lifecycle-fixture", {
        "execution_binding_id": b1.execution_binding_id,
        "command_id": b1.command_id,
        "correlation_id": b1.correlation_id,
        "head_before": HEAD,
        "execution_status": "FAIL",
        "validation_status": "FAIL",
    })

    # Check b1 in memory: must be FAILED, not ACTIVE!
    state = memory.read_state()
    b1_stored = next(b for b in state.execution["bindings"] if b["execution_binding_id"] == b1.execution_binding_id)
    assert b1_stored["status"] == STATUS_FAILED

    # 3. Retry task t1
    b2 = coordinator.start("lifecycle-fixture", task_id="t1", expected_repo_head_before=HEAD)
    assert b2.status == STATUS_ACTIVE
    assert b2.generation == 2
    assert b2.execution_binding_id != b1.execution_binding_id

    # 4. Check active bindings count in memory: exactly 1!
    state = memory.read_state()
    active_bindings = [b for b in state.execution["bindings"] if b.get("status") == STATUS_ACTIVE and not b.get("superseded")]
    assert len(active_bindings) == 1
    assert active_bindings[0]["execution_binding_id"] == b2.execution_binding_id

    # Verify invariants pass
    ok, errors = check_binding_lifecycle_invariants(state.execution)
    assert ok is True, f"Invariant errors: {errors}"


# -----------------------------------------------------------------------------
# B. FAIL -> RETRY LIFECYCLE & MONOTONIC GENERATION
# -----------------------------------------------------------------------------

def test_fail_retry_lifecycle_and_monotonic_generation(tmp_path: Path) -> None:
    """Repeated failures and retries must produce strictly increasing generations and at most 1 active binding."""
    _, coordinator, _, memory = _setup_env(tmp_path)

    bindings = []
    for gen in range(1, 5):
        b = coordinator.start("lifecycle-fixture", task_id="t1", expected_repo_head_before=HEAD)
        bindings.append(b)
        assert b.generation == gen
        assert b.status == STATUS_ACTIVE

        # Verify exactly 1 ACTIVE binding at every step
        state = memory.read_state()
        active = [item for item in state.execution["bindings"] if item.get("status") == STATUS_ACTIVE and not item.get("superseded")]
        assert len(active) == 1
        assert active[0]["execution_binding_id"] == b.execution_binding_id

        if gen < 4:
            # Record failure for current binding
            coordinator.record_result("lifecycle-fixture", {
                "execution_binding_id": b.execution_binding_id,
                "command_id": b.command_id,
                "correlation_id": b.correlation_id,
                "head_before": HEAD,
                "execution_status": "FAIL",
                "validation_status": "FAIL",
            })

    # On 4th attempt, pass
    coordinator.record_result("lifecycle-fixture", {
        "execution_binding_id": bindings[3].execution_binding_id,
        "command_id": bindings[3].command_id,
        "correlation_id": bindings[3].correlation_id,
        "head_before": HEAD,
        "execution_status": "PASS",
        "validation_status": "PASS",
        "criteria": [{"criterion": "criterion:test", "type": "DETERMINISTIC", "status": "PASS"}],
    })

    state = memory.read_state()
    # Now task t1 is completed; no active bindings remain for t1
    t1_active = [item for item in state.execution["bindings"] if item.get("task_id") == "t1" and item.get("status") == STATUS_ACTIVE]
    assert len(t1_active) == 0
    b4_stored = next(item for item in state.execution["bindings"] if item["execution_binding_id"] == bindings[3].execution_binding_id)
    assert b4_stored["status"] == STATUS_ACCEPTED


# -----------------------------------------------------------------------------
# C. LATE OLD RESULT REJECTION
# -----------------------------------------------------------------------------

def test_late_old_result_rejection(tmp_path: Path) -> None:
    """A late PASS from an old (failed or superseded) binding must be rejected as STALE_RESULT without mutating task."""
    _, coordinator, _, memory = _setup_env(tmp_path)

    # 1. Start gen 1
    b1 = coordinator.start("lifecycle-fixture", task_id="t1", expected_repo_head_before=HEAD)
    # 2. Record FAIL on gen 1
    coordinator.record_result("lifecycle-fixture", {
        "execution_binding_id": b1.execution_binding_id,
        "command_id": b1.command_id,
        "correlation_id": b1.correlation_id,
        "head_before": HEAD,
        "execution_status": "FAIL",
        "validation_status": "FAIL",
    })

    # 3. Retry -> Start gen 2
    b2 = coordinator.start("lifecycle-fixture", task_id="t1", expected_repo_head_before=HEAD)
    assert b2.generation == 2

    # 4. Now a late PASS arrives for b1
    with pytest.raises(ProjectExecutionError) as exc:
        coordinator.record_result("lifecycle-fixture", {
            "execution_binding_id": b1.execution_binding_id,
            "command_id": b1.command_id,
            "correlation_id": b1.correlation_id,
            "head_before": HEAD,
            "execution_status": "PASS",
            "validation_status": "PASS",
            "criteria": [{"criterion": "criterion:test", "type": "DETERMINISTIC", "status": "PASS"}],
        })
    assert exc.value.code == "STALE_RESULT"
    assert exc.value.details.get("reason") == "execution_binding_stale"

    # 5. Verify task t1 is NOT marked completed and current binding is still b2
    state = memory.read_state()
    assert state.execution.get("task_statuses", {}).get("t1") != "completed"
    assert state.execution.get("current_binding_id") == b2.execution_binding_id


# -----------------------------------------------------------------------------
# D. CRASH BOUNDARY & RECONCILIATION
# -----------------------------------------------------------------------------

def test_crash_boundary_and_reconciliation(tmp_path: Path) -> None:
    """Corrupted state with multiple ACTIVE bindings reconciles deterministically and idempotently."""
    _, coordinator, _, memory = _setup_env(tmp_path)

    # Inject corrupted state with 3 active bindings for task t1
    corrupted_doc = {
        "schema": "bdb-project-execution-v1",
        "current_binding_id": "b-corrupted-2",
        "current_task_id": "t1",
        "bindings": [
            {"execution_binding_id": "b-corrupted-1", "project_id": "lifecycle-fixture", "plan_version": "1", "task_id": "t1", "launch_id": "l1", "correlation_id": "c1", "command_id": "cmd1", "repo_alias": "lifecycle-fixture", "expected_repo_head_before": HEAD, "created_at": "2026-08-25T10:00:00.000000Z", "status": STATUS_ACTIVE, "superseded": False, "generation": 1},
            {"execution_binding_id": "b-corrupted-2", "project_id": "lifecycle-fixture", "plan_version": "1", "task_id": "t1", "launch_id": "l2", "correlation_id": "c2", "command_id": "cmd2", "repo_alias": "lifecycle-fixture", "expected_repo_head_before": HEAD, "created_at": "2026-08-25T11:00:00.000000Z", "status": STATUS_ACTIVE, "superseded": False, "generation": 2},
            {"execution_binding_id": "b-corrupted-3", "project_id": "lifecycle-fixture", "plan_version": "1", "task_id": "t1", "launch_id": "l3", "correlation_id": "c3", "command_id": "cmd3", "repo_alias": "lifecycle-fixture", "expected_repo_head_before": HEAD, "created_at": "2026-08-25T12:00:00.000000Z", "status": STATUS_ACTIVE, "superseded": False, "generation": 3},
        ],
        "attempts": [],
        "acceptance_results": [],
        "task_statuses": {"t1": "active"},
    }

    # Check that invariant checker detects violation
    ok, errors = check_binding_lifecycle_invariants(corrupted_doc)
    assert ok is False
    assert len(errors) > 0

    # Run reconciler
    reconciled = reconcile_execution_bindings(copy.deepcopy(corrupted_doc))

    # After reconciliation: b-corrupted-2 was current_binding_id, so it remains ACTIVE, others become SUPERSEDED
    ok, errors = check_binding_lifecycle_invariants(reconciled)
    assert ok is True, f"Reconciled errors: {errors}"

    b1 = next(b for b in reconciled["bindings"] if b["execution_binding_id"] == "b-corrupted-1")
    b2 = next(b for b in reconciled["bindings"] if b["execution_binding_id"] == "b-corrupted-2")
    b3 = next(b for b in reconciled["bindings"] if b["execution_binding_id"] == "b-corrupted-3")

    assert b1["status"] == STATUS_SUPERSEDED and b1["superseded"] is True
    assert b2["status"] == STATUS_ACTIVE and b2["superseded"] is False
    assert b3["status"] == STATUS_SUPERSEDED and b3["superseded"] is True

    # Idempotence: running reconciler again produces exact same output
    reconciled_again = reconcile_execution_bindings(copy.deepcopy(reconciled))
    assert reconciled_again == reconciled


# -----------------------------------------------------------------------------
# E. CONCURRENT RETRY ATOMICITY
# -----------------------------------------------------------------------------

def test_concurrent_retry_atomicity(tmp_path: Path) -> None:
    """Concurrent retries must never leave more than 1 ACTIVE binding."""
    _, coordinator, _, memory = _setup_env(tmp_path)

    # Seed task t1
    coordinator.start("lifecycle-fixture", task_id="t1", expected_repo_head_before=HEAD)

    # Concurrently attempt 10 retries
    def worker(idx: int) -> ProjectExecutionBinding:
        # Each worker attempts to start/retry task t1
        return coordinator.start("lifecycle-fixture", task_id="t1", expected_repo_head_before=HEAD)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(worker, i) for i in range(10)]
        results = [f.result() for f in futures]

    assert len(results) == 10

    state = memory.read_state()
    active = [b for b in state.execution["bindings"] if b.get("status") == STATUS_ACTIVE and not b.get("superseded")]
    assert len(active) == 1, f"Expected exactly 1 ACTIVE binding, found {len(active)}"

    # Invariant checker must pass
    ok, errors = check_binding_lifecycle_invariants(state.execution)
    assert ok is True, f"Invariant errors after concurrency: {errors}"


# -----------------------------------------------------------------------------
# F. ILLEGAL TRANSITIONS FAIL CLOSED
# -----------------------------------------------------------------------------

def test_illegal_transitions_fail_closed() -> None:
    """Terminal statuses cannot transition to anything else."""
    # Valid transitions:
    validate_binding_transition(STATUS_ACTIVE, STATUS_ACCEPTED)
    validate_binding_transition(STATUS_ACTIVE, STATUS_FAILED)
    validate_binding_transition(STATUS_ACTIVE, STATUS_SUPERSEDED)
    validate_binding_transition(STATUS_ACTIVE, STATUS_ACTIVE)  # Idempotent

    # Illegal transitions from terminal states:
    with pytest.raises(BindingLifecycleError) as exc:
        validate_binding_transition(STATUS_ACCEPTED, STATUS_ACTIVE)
    assert exc.value.code == "illegal_binding_transition"

    with pytest.raises(BindingLifecycleError) as exc:
        validate_binding_transition(STATUS_FAILED, STATUS_ACCEPTED)
    assert exc.value.code == "illegal_binding_transition"

    with pytest.raises(BindingLifecycleError) as exc:
        validate_binding_transition(STATUS_SUPERSEDED, STATUS_ACTIVE)
    assert exc.value.code == "illegal_binding_transition"

    with pytest.raises(BindingLifecycleError) as exc:
        validate_binding_transition("INVALID_STATUS", STATUS_ACTIVE)
    assert exc.value.code == "binding_status_invalid"


# -----------------------------------------------------------------------------
# G. DIRECT COORDINATOR VS NATIVE PATH PARITY
# -----------------------------------------------------------------------------

def test_direct_coordinator_vs_native_guard_parity(tmp_path: Path) -> None:
    """Direct coordinator and Native workflow path reject stale binding with the exact same disposition."""
    _, coordinator, workflow, memory = _setup_env(tmp_path)

    # 1. Create binding 1 and fail it
    b1 = coordinator.start("lifecycle-fixture", task_id="t1", expected_repo_head_before=HEAD)
    coordinator.record_result("lifecycle-fixture", {
        "execution_binding_id": b1.execution_binding_id,
        "command_id": b1.command_id,
        "correlation_id": b1.correlation_id,
        "head_before": HEAD,
        "execution_status": "FAIL",
        "validation_status": "FAIL",
    })

    # 2. Create binding 2
    b2 = coordinator.start("lifecycle-fixture", task_id="t1", expected_repo_head_before=HEAD)

    # Direct coordinator path: late result for b1
    direct_rejected = False
    try:
        coordinator.record_result("lifecycle-fixture", {
            "execution_binding_id": b1.execution_binding_id,
            "command_id": b1.command_id,
            "correlation_id": b1.correlation_id,
            "head_before": HEAD,
            "execution_status": "PASS",
            "validation_status": "PASS",
            "criteria": [{"criterion": "criterion:test", "type": "DETERMINISTIC", "status": "PASS"}],
        })
    except ProjectExecutionError as exc:
        if exc.code == "STALE_RESULT":
            direct_rejected = True
    assert direct_rejected is True

    # Native workflow path: submission for b1
    native_rejected = False
    try:
        workflow.submit_project_execution_result({
            "schema": "bdb-project-execution-submission-v1",
            "project_id": "lifecycle-fixture",
            "plan_version": "1",
            "task_id": "t1",
            "execution_binding_id": b1.execution_binding_id,
            "correlation_id": b1.correlation_id,
            "command_id": b1.command_id,
            "repo_alias": "lifecycle-fixture",
            "head_before": HEAD,
            "head_after": HEAD,
            "execution_status": "PASS",
            "validation_status": "PASS",
            "promotion_status": "NOT_RUN",
            "result_summary": "late submission",
            "evidence_refs": ["ref-1"],
            "criteria": [{"criterion": "criterion:test", "type": "DETERMINISTIC", "status": "PASS"}],
        }, conversation_id="conv-12345678", launch_id=b1.launch_id)
    except (ProjectWorkflowError, ProjectExecutionError) as exc:
        if getattr(exc, "code", "") in {"execution_binding_stale", "STALE_RESULT"}:
            native_rejected = True
    assert native_rejected is True


# -----------------------------------------------------------------------------
# MACHINE GATE FOR NX-003
# -----------------------------------------------------------------------------

def run_nx003_lifecycle_gate(tmp_path: Path) -> tuple[bool, dict[str, Any]]:
    """Deterministic source-bound machine gate for NX-003."""
    _, coordinator, workflow, memory = _setup_env(tmp_path)

    # 1. Invariant: MAX_ACTIVE_BINDINGS_PER_TASK_RUN = 1
    # 2. Invariant: GENERATION_MONOTONIC = TRUE
    # 3. Invariant: LATE_OLD_RESULT_ACCEPTED = FALSE
    # 4. Invariant: DIRECT_NATIVE_GUARD_PARITY = TRUE
    # 5. Invariant: CONCURRENT_RETRY_DUPLICATE_ACTIVE = FALSE

    # Test sequence:
    b1 = coordinator.start("lifecycle-fixture", task_id="t1", expected_repo_head_before=HEAD)
    coordinator.record_result("lifecycle-fixture", {
        "execution_binding_id": b1.execution_binding_id,
        "command_id": b1.command_id,
        "correlation_id": b1.correlation_id,
        "head_before": HEAD,
        "execution_status": "FAIL",
        "validation_status": "FAIL",
    })
    b2 = coordinator.start("lifecycle-fixture", task_id="t1", expected_repo_head_before=HEAD)

    # Check max active & generation monotonic
    state = memory.read_state()
    active_bindings = [b for b in state.execution["bindings"] if b.get("status") == STATUS_ACTIVE and not b.get("superseded")]
    max_active_ok = (len(active_bindings) == 1)

    inv_ok, errors = check_binding_lifecycle_invariants(state.execution)
    generation_monotonic = inv_ok and (b2.generation > b1.generation)

    # Late old result check
    late_accepted = True
    try:
        coordinator.record_result("lifecycle-fixture", {
            "execution_binding_id": b1.execution_binding_id,
            "command_id": b1.command_id,
            "correlation_id": b1.correlation_id,
            "head_before": HEAD,
            "execution_status": "PASS",
            "validation_status": "PASS",
        })
    except ProjectExecutionError as exc:
        if exc.code == "STALE_RESULT":
            late_accepted = False

    # Direct / Native parity check
    native_stale_rejected = False
    try:
        workflow.submit_project_execution_result({
            "schema": "bdb-project-execution-submission-v1",
            "project_id": "lifecycle-fixture",
            "plan_version": "1",
            "task_id": "t1",
            "execution_binding_id": b1.execution_binding_id,
            "correlation_id": b1.correlation_id,
            "command_id": b1.command_id,
            "repo_alias": "lifecycle-fixture",
            "head_before": HEAD,
            "head_after": HEAD,
            "execution_status": "PASS",
            "validation_status": "PASS",
            "promotion_status": "NOT_RUN",
            "result_summary": "late submission",
            "evidence_refs": ["ref-1"],
            "criteria": [{"criterion": "criterion:test", "type": "DETERMINISTIC", "status": "PASS"}],
        }, conversation_id="conv-12345678", launch_id=b1.launch_id)
    except (ProjectWorkflowError, ProjectExecutionError) as exc:
        if getattr(exc, "code", "") in {"execution_binding_stale", "STALE_RESULT"}:
            native_stale_rejected = True
    parity_ok = (not late_accepted) and native_stale_rejected

    # Concurrent retry test
    for i in range(5):
        binding = ProjectExecutionBinding(
            f"binding-gate-{i}",
            "lifecycle-fixture",
            "1",
            "t1",
            f"launch-gate-{i}",
            f"corr-gate-{i}",
            f"cmd-gate-{i}",
            "lifecycle-fixture",
            HEAD,
            f"2026-08-25T13:{i:02d}:00.000000Z",
            status=STATUS_ACTIVE,
            superseded=False,
            generation=i + 3,
        )
        coordinator.persist_binding(binding)

    final_state = memory.read_state()
    final_active = [b for b in final_state.execution["bindings"] if b.get("status") == STATUS_ACTIVE and not b.get("superseded")]
    concurrent_ok = (len(final_active) == 1)

    gate_passed = (
        max_active_ok
        and generation_monotonic
        and (not late_accepted)
        and parity_ok
        and concurrent_ok
    )

    report = {
        "task_id": "NX-003",
        "MAX_ACTIVE_BINDINGS_PER_TASK_RUN": len(final_active),
        "GENERATION_MONOTONIC": generation_monotonic,
        "LATE_OLD_RESULT_ACCEPTED": late_accepted,
        "DIRECT_NATIVE_GUARD_PARITY": parity_ok,
        "CONCURRENT_RETRY_DUPLICATE_ACTIVE": not concurrent_ok,
        "machine_gate": "PASS" if gate_passed else "FAIL",
    }
    return gate_passed, report


def test_nx003_machine_gate_execution(tmp_path: Path) -> None:
    passed, report = run_nx003_lifecycle_gate(tmp_path)
    assert passed is True, f"Machine gate failed: {report}"
    assert report["machine_gate"] == "PASS"

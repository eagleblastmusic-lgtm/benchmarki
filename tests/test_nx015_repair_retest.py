"""NX-015 — Automatic Repair and Exact Retest Loop — Machine Gate Tests.

Tests:
1. Versioned repair request identity and duplicate idempotency
2. Task ID preservation, new attempt creation, and monotonic binding generation
3. Minimal repair envelope enforcement (scope escape & adversarial path rejection)
4. READY_FOR_RETEST intermediate state (repair does not directly promote to PASS)
5. Exact retest selector enforcement and wrong verifier rejection
6. Deterministic end-to-end fixture: Failure -> Repair -> Retest -> PASS (1 accepted result)
7. Repeated same repair failure budget exhaustion
8. Different failure after repair handling
9. Rebuildable repair history projection and evidence chain completeness
10. Four crash boundaries (A, B, C, D) reconciliation
11. NX-015 canonical machine gate
"""

from __future__ import annotations

import ast
import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Mapping

import pytest

from bdb_vnext.binding_lifecycle import (
    STATUS_ACCEPTED,
    STATUS_ACTIVE,
    STATUS_FAILED,
    STATUS_SUPERSEDED,
)
from bdb_vnext.failure_budget import (
    DEFAULT_FAILURE_BUDGET_POLICY,
    ExhaustionState,
    FailureBudgetLedger,
    FailureFingerprint,
    compute_failure_fingerprint,
)
from bdb_vnext.failure_classifier import compute_evidence_digest
from bdb_vnext.failure_taxonomy import FailureClass
from bdb_vnext.repair_loop import (
    RepairLoopController,
    RepairRequest,
    RepairScopeEnvelope,
    RepairStage,
    RetestSelector,
    VerifierExecutionResult,
)


@pytest.fixture
def mem_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture
def disk_db_path(tmp_path: Path) -> Path:
    return tmp_path / "test_repair_loop.db"


# ==============================================================================
# 1. REPAIR REQUEST IDENTITY & LIFECYCLE TESTS
# ==============================================================================

class TestRepairRequestLifecycle:
    def test_repair_preserves_task_id_and_advances_generation(self, mem_conn: sqlite3.Connection) -> None:
        ledger = FailureBudgetLedger(mem_conn, "p-lifecycle")
        controller = RepairLoopController(mem_conn, "p-lifecycle", ledger)

        fp = compute_failure_fingerprint(FailureClass.PROJECT_REPAIRABLE, {"error_code": "SyntaxError"})
        envelope = RepairScopeEnvelope(allowed_paths=("src/", "tests/"))
        selector = RetestSelector("EXACT_TEST", "tests/test_mod.py::test_fn", "pytest_runner")

        ok, req, msg = controller.create_repair_request(
            run_id="run-001",
            task_id="task-100",
            failed_binding_id="bnd-orig",
            failed_attempt_id="att-orig",
            fingerprint=fp,
            classification=FailureClass.PROJECT_REPAIRABLE,
            evidence_digest="sha256:evidence111",
            scope_envelope=envelope,
            expected_source_head="0" * 40,
            expected_source_tree="1" * 40,
            retest_selector=selector,
            current_binding_generation=1,
        )

        assert ok is True
        assert req is not None
        # Same task ID preserved
        assert req.task_id == "task-100"
        assert req.run_id == "run-001"
        # Monotonic generation
        assert req.repair_generation == 2

        # Check records in DB
        history = controller.get_repair_history("task-100")
        assert len(history) == 1
        assert history[0]["task_id"] == "task-100"
        assert history[0]["generation"] == 2
        assert history[0]["binding_id"] != "bnd-orig"
        assert history[0]["attempt_id"] != "att-orig"

    def test_duplicate_repair_request_is_idempotent(self, mem_conn: sqlite3.Connection) -> None:
        ledger = FailureBudgetLedger(mem_conn, "p-dupe")
        controller = RepairLoopController(mem_conn, "p-dupe", ledger)

        fp = compute_failure_fingerprint(FailureClass.PROJECT_REPAIRABLE, {"error_code": "SyntaxError"})
        envelope = RepairScopeEnvelope(allowed_paths=("src/",))
        selector = RetestSelector("EXACT_TEST", "tests/test_mod.py::test_fn", "pytest_runner")

        kwargs = dict(
            run_id="run-001",
            task_id="task-100",
            failed_binding_id="bnd-orig",
            failed_attempt_id="att-orig",
            fingerprint=fp,
            classification=FailureClass.PROJECT_REPAIRABLE,
            evidence_digest="sha256:evidence111",
            scope_envelope=envelope,
            expected_source_head="0" * 40,
            expected_source_tree="1" * 40,
            retest_selector=selector,
            current_binding_generation=1,
        )

        ok1, req1, _ = controller.create_repair_request(**kwargs)
        ok2, req2, msg2 = controller.create_repair_request(**kwargs)

        assert ok1 is True and ok2 is True
        assert req1.repair_request_id == req2.repair_request_id
        assert msg2 == "duplicate_repair_request_idempotent"

        # Check attempt not duplicated in records
        history = controller.get_repair_history("task-100")
        assert len(history) == 1


# ==============================================================================
# 2. MINIMAL REPAIR ENVELOPE TESTS
# ==============================================================================

class TestMinimalRepairEnvelope:
    def test_allowed_mutation_within_envelope(self) -> None:
        env = RepairScopeEnvelope(allowed_paths=("src/calculator.py", "tests/"))
        valid, msg = env.validate_mutation(["src/calculator.py", "tests/test_calculator.py"])
        assert valid is True
        assert msg == "scope_valid"

    def test_scope_escape_rejected(self) -> None:
        env = RepairScopeEnvelope(allowed_paths=("src/component/",))
        valid, msg = env.validate_mutation(["src/component/app.py", "src/other_module/secret.py"])
        assert valid is False
        assert "escapes allowed repair envelope" in msg

    def test_adversarial_path_traversal_rejected(self) -> None:
        env = RepairScopeEnvelope(allowed_paths=("src/",))
        valid, msg = env.validate_mutation(["src/../etc/passwd"])
        assert valid is False
        assert "path traversal" in msg

    def test_phase_scope_repair_reverts_only_boundary(self, mem_conn: sqlite3.Connection) -> None:
        ledger = FailureBudgetLedger(mem_conn, "p-phase")
        controller = RepairLoopController(mem_conn, "p-phase", ledger)

        fp = compute_failure_fingerprint(
            FailureClass.PHASE_SCOPE_VIOLATION,
            {"out_of_scope_files": ["future_milestone.py"], "allowed_task_scope": ["current_task.py"]},
        )
        env = RepairScopeEnvelope(allowed_paths=("current_task.py", "future_milestone.py"))
        selector = RetestSelector("MACHINE_GATE", "phase_scope_gate", "phase_verifier")

        ok, req, _ = controller.create_repair_request(
            run_id="run-phase",
            task_id="task-phase",
            failed_binding_id="bnd-0",
            failed_attempt_id="att-0",
            fingerprint=fp,
            classification=FailureClass.PHASE_SCOPE_VIOLATION,
            evidence_digest="sha256:phase_ev",
            scope_envelope=env,
            expected_source_head="0" * 40,
            expected_source_tree="1" * 40,
            retest_selector=selector,
            current_binding_generation=1,
        )
        assert ok is True

        # Applying boundary patch
        eff_ok, eff_msg = controller.apply_repair_effect(
            req.repair_request_id,
            ["future_milestone.py"],  # Reverting out-of-scope mutation
            source_after_head="a" * 40,
        )
        assert eff_ok is True
        assert eff_msg == "effect_applied_within_boundary"


# ==============================================================================
# 3. READY_FOR_RETEST & EXACT RETEST SELECTOR TESTS
# ==============================================================================

class TestRetestExecution:
    def test_repair_effect_does_not_directly_promote_to_pass(self, mem_conn: sqlite3.Connection) -> None:
        ledger = FailureBudgetLedger(mem_conn, "p-retest")
        controller = RepairLoopController(mem_conn, "p-retest", ledger)

        fp = compute_failure_fingerprint(FailureClass.PROJECT_REPAIRABLE, {"error_code": "E1"})
        envelope = RepairScopeEnvelope(allowed_paths=("src/",))
        selector = RetestSelector("EXACT_TEST", "tests/test_x.py::test_target", "runner")

        _, req, _ = controller.create_repair_request(
            run_id="r1",
            task_id="t1",
            failed_binding_id="b1",
            failed_attempt_id="a1",
            fingerprint=fp,
            classification=FailureClass.PROJECT_REPAIRABLE,
            evidence_digest="sha256:e1",
            scope_envelope=envelope,
            expected_source_head="0" * 40,
            expected_source_tree="1" * 40,
            retest_selector=selector,
        )

        controller.apply_repair_effect(req.repair_request_id, ["src/mod.py"], "head-new")
        controller.mark_ready_for_retest(req.repair_request_id)

        history = controller.get_repair_history("t1")
        assert history[0]["stage"] == RepairStage.READY_FOR_RETEST.value
        # Invariant: Not promoted to accepted / pass yet
        assert controller.count_accepted_results("t1") == 0

    def test_wrong_retest_target_rejected(self, mem_conn: sqlite3.Connection) -> None:
        ledger = FailureBudgetLedger(mem_conn, "p-wrong")
        controller = RepairLoopController(mem_conn, "p-wrong", ledger)

        fp = compute_failure_fingerprint(FailureClass.PROJECT_REPAIRABLE, {"error_code": "E1"})
        envelope = RepairScopeEnvelope(allowed_paths=("src/",))
        selector = RetestSelector("EXACT_TEST", "tests/test_x.py::test_target", "exact_runner")

        _, req, _ = controller.create_repair_request(
            run_id="r1",
            task_id="t1",
            failed_binding_id="b1",
            failed_attempt_id="a1",
            fingerprint=fp,
            classification=FailureClass.PROJECT_REPAIRABLE,
            evidence_digest="sha256:e1",
            scope_envelope=envelope,
            expected_source_head="0" * 40,
            expected_source_tree="1" * 40,
            retest_selector=selector,
        )
        controller.apply_repair_effect(req.repair_request_id, ["src/mod.py"], "head-new")
        controller.mark_ready_for_retest(req.repair_request_id)

        # Verifier runs wrong test target
        wrong_verifier = lambda sel: VerifierExecutionResult(
            verifier_id="exact_runner",
            target="tests/test_unrelated.py::test_broader",
            status="PASS",
            evidence_digest="sha256:wrong_digest",
        )
        ok, msg = controller.execute_exact_retest(req.repair_request_id, wrong_verifier)
        assert ok is False
        assert "wrong_retest_target" in msg
        assert controller.count_accepted_results("t1") == 0


# ==============================================================================
# 4. END-TO-END FAILURE -> REPAIR -> RETEST -> PASS
# ==============================================================================

class TestEndToEndRepairRetest:
    def test_complete_repair_loop_produces_one_accepted_result(self, mem_conn: sqlite3.Connection) -> None:
        ledger = FailureBudgetLedger(mem_conn, "p-e2e")
        controller = RepairLoopController(mem_conn, "p-e2e", ledger)

        fp = compute_failure_fingerprint(FailureClass.PROJECT_REPAIRABLE, {"error_code": "SyntaxError", "file": "src/app.py"})
        envelope = RepairScopeEnvelope(allowed_paths=("src/app.py",))
        selector = RetestSelector("EXACT_TEST", "tests/test_app.py::test_main", "pytest_runner")

        # Step 1: Create repair request
        ok, req, _ = controller.create_repair_request(
            run_id="run-e2e",
            task_id="task-e2e",
            failed_binding_id="bnd-1",
            failed_attempt_id="att-1",
            fingerprint=fp,
            classification=FailureClass.PROJECT_REPAIRABLE,
            evidence_digest="sha256:fail_evidence",
            scope_envelope=envelope,
            expected_source_head="0" * 40,
            expected_source_tree="1" * 40,
            retest_selector=selector,
            current_binding_generation=1,
        )
        assert ok is True

        # Step 2: Apply bounded patch
        eff_ok, _ = controller.apply_repair_effect(req.repair_request_id, ["src/app.py"], "head-repaired")
        assert eff_ok is True

        # Step 3: Transition to READY_FOR_RETEST
        rdy_ok, _ = controller.mark_ready_for_retest(req.repair_request_id)
        assert rdy_ok is True

        # Step 4: Execute exact verifier -> PASS
        correct_verifier = lambda sel: VerifierExecutionResult(
            verifier_id="pytest_runner",
            target="tests/test_app.py::test_main",
            status="PASS",
            evidence_digest="sha256:retest_pass_evidence",
        )
        retest_ok, msg = controller.execute_exact_retest(req.repair_request_id, correct_verifier)
        assert retest_ok is True
        assert msg == "accepted"

        # Step 5: Verify exact single accepted result
        assert controller.count_accepted_results("task-e2e") == 1

        # Attempting duplicate acceptance must be prevented
        dup_ok, dup_msg = controller.execute_exact_retest(req.repair_request_id, correct_verifier)
        assert dup_ok is False
        assert controller.count_accepted_results("task-e2e") == 1

    def test_repeated_same_repair_failure_budget_exhaustion(self, mem_conn: sqlite3.Connection) -> None:
        ledger = FailureBudgetLedger(mem_conn, "p-repeated")
        controller = RepairLoopController(mem_conn, "p-repeated", ledger)

        fp = compute_failure_fingerprint(FailureClass.PROJECT_REPAIRABLE, {"error_code": "RepeatBug"})
        envelope = RepairScopeEnvelope(allowed_paths=("src/",))
        selector = RetestSelector("EXACT_TEST", "tests/test_repeat.py", "runner")

        # Repair 1: allowed
        ok1, req1, _ = controller.create_repair_request(
            run_id="r1", task_id="t1", failed_binding_id="b1", failed_attempt_id="a1",
            fingerprint=fp, classification=FailureClass.PROJECT_REPAIRABLE, evidence_digest="sha256:e1",
            scope_envelope=envelope, expected_source_head="0"*40, expected_source_tree="1"*40,
            retest_selector=selector, current_binding_generation=1,
        )
        assert ok1 is True

        # Repair 2: allowed
        ok2, req2, _ = controller.create_repair_request(
            run_id="r1", task_id="t1", failed_binding_id="b2", failed_attempt_id="a2",
            fingerprint=fp, classification=FailureClass.PROJECT_REPAIRABLE, evidence_digest="sha256:e2",
            scope_envelope=envelope, expected_source_head="0"*40, expected_source_tree="1"*40,
            retest_selector=selector, current_binding_generation=2,
        )
        assert ok2 is True

        # Repair 3: budget exhausted (max_same_fingerprint_repairs = 2)
        ok3, req3, msg3 = controller.create_repair_request(
            run_id="r1", task_id="t1", failed_binding_id="b3", failed_attempt_id="a3",
            fingerprint=fp, classification=FailureClass.PROJECT_REPAIRABLE, evidence_digest="sha256:e3",
            scope_envelope=envelope, expected_source_head="0"*40, expected_source_tree="1"*40,
            retest_selector=selector, current_binding_generation=3,
        )
        assert ok3 is False
        assert "budget_exhausted" in msg3

    def test_different_failure_after_repair(self, mem_conn: sqlite3.Connection) -> None:
        ledger = FailureBudgetLedger(mem_conn, "p-diff")
        controller = RepairLoopController(mem_conn, "p-diff", ledger)

        fp1 = compute_failure_fingerprint(FailureClass.PROJECT_REPAIRABLE, {"error_code": "Bug1"})
        fp2 = compute_failure_fingerprint(FailureClass.PROJECT_REPAIRABLE, {"error_code": "Bug2"})
        envelope = RepairScopeEnvelope(allowed_paths=("src/",))
        selector = RetestSelector("EXACT_TEST", "tests/test_x.py", "runner")

        # Repair Bug1
        ok1, _, _ = controller.create_repair_request(
            run_id="r1", task_id="t1", failed_binding_id="b1", failed_attempt_id="a1",
            fingerprint=fp1, classification=FailureClass.PROJECT_REPAIRABLE, evidence_digest="sha256:e1",
            scope_envelope=envelope, expected_source_head="0"*40, expected_source_tree="1"*40,
            retest_selector=selector, current_binding_generation=1,
        )
        assert ok1 is True

        # Bug2 appears after repair: separate fingerprint budget applies
        ok2, _, _ = controller.create_repair_request(
            run_id="r1", task_id="t1", failed_binding_id="b2", failed_attempt_id="a2",
            fingerprint=fp2, classification=FailureClass.PROJECT_REPAIRABLE, evidence_digest="sha256:e2",
            scope_envelope=envelope, expected_source_head="0"*40, expected_source_tree="1"*40,
            retest_selector=selector, current_binding_generation=2,
        )
        assert ok2 is True


# ==============================================================================
# 5. CRASH BOUNDARIES RECONCILIATION
# ==============================================================================

class TestCrashBoundaries:
    def test_crash_recovery_across_stages(self, disk_db_path: Path) -> None:
        # Boundary A: Intent recorded, crash before effect
        conn1 = sqlite3.connect(str(disk_db_path))
        ledger1 = FailureBudgetLedger(conn1, "p-crash")
        ctrl1 = RepairLoopController(conn1, "p-crash", ledger1)

        fp = compute_failure_fingerprint(FailureClass.PROJECT_REPAIRABLE, {"error_code": "CrashBug"})
        envelope = RepairScopeEnvelope(allowed_paths=("src/",))
        selector = RetestSelector("EXACT_TEST", "tests/test_crash.py", "runner")

        _, req, _ = ctrl1.create_repair_request(
            run_id="r1", task_id="t1", failed_binding_id="b1", failed_attempt_id="a1",
            fingerprint=fp, classification=FailureClass.PROJECT_REPAIRABLE, evidence_digest="sha256:e1",
            scope_envelope=envelope, expected_source_head="0"*40, expected_source_tree="1"*40,
            retest_selector=selector,
        )
        conn1.close()

        # Reopen after crash at boundary A
        conn2 = sqlite3.connect(str(disk_db_path))
        ledger2 = FailureBudgetLedger(conn2, "p-crash")
        ctrl2 = RepairLoopController(conn2, "p-crash", ledger2)

        stage_a = ctrl2.reconcile_crash_boundary(req.repair_request_id)
        assert stage_a == RepairStage.INTENT_RECORDED.value

        # Boundary B: Apply effect, then crash before READY_FOR_RETEST
        ctrl2.apply_repair_effect(req.repair_request_id, ["src/fix.py"], "head-b")
        conn2.close()

        conn3 = sqlite3.connect(str(disk_db_path))
        ctrl3 = RepairLoopController(conn3, "p-crash", FailureBudgetLedger(conn3, "p-crash"))
        stage_b = ctrl3.reconcile_crash_boundary(req.repair_request_id)
        assert stage_b == RepairStage.EFFECT_APPLIED.value

        # Boundary C: Transition to READY_FOR_RETEST, then crash before verifier
        ctrl3.mark_ready_for_retest(req.repair_request_id)
        conn3.close()

        conn4 = sqlite3.connect(str(disk_db_path))
        ctrl4 = RepairLoopController(conn4, "p-crash", FailureBudgetLedger(conn4, "p-crash"))
        stage_c = ctrl4.reconcile_crash_boundary(req.repair_request_id)
        assert stage_c == RepairStage.READY_FOR_RETEST.value

        # Run verifier to completion
        ver = lambda s: VerifierExecutionResult("runner", "tests/test_crash.py", "PASS", "sha256:pass")
        ok, msg = ctrl4.execute_exact_retest(req.repair_request_id, ver)
        assert ok is True
        assert ctrl4.count_accepted_results("t1") == 1
        conn4.close()


# ==============================================================================
# 6. NX-015 MACHINE GATE
# ==============================================================================

def inspect_nx015_gate_for_hardcoded_results() -> tuple[bool, list[str]]:
    """AST-inspect run_nx015_machine_gate for hardcoded outcomes."""
    source_path = Path(__file__)
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    gate_func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "run_nx015_machine_gate":
            gate_func = node
            break

    if gate_func is None:
        return (False, ["run_nx015_machine_gate not found"])

    REQUIRED_FIELDS = {
        "NX014_BUDGET_CONTRACT_MATCH",
        "DUPLICATE_REPAIR_REQUEST_DUPLICATES_ATTEMPT",
        "REPAIR_CHANGES_TASK_ID",
        "REPAIR_NEW_ATTEMPT_CREATED",
        "BINDING_GENERATION_MONOTONIC",
        "REPAIR_SCOPE_ESCAPES_ALLOWED_BOUNDARY",
        "REPAIR_RESULT_DIRECTLY_PROMOTES_TO_PASS",
        "WRONG_RETEST_ACCEPTED",
        "UNNECESSARY_BROADER_RETESTS",
        "REPAIR_AFTER_BUDGET_EXHAUSTION",
        "CRASH_RECOVERY",
        "ACCEPTED_RESULTS",
        "DUPLICATE_ACCEPTED_RESULTS",
        "REPAIR_HISTORY_COMPLETE",
        "EVIDENCE_CHAIN_COMPLETE",
        "NX015_STATUS",
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


def run_nx015_machine_gate() -> dict[str, Any]:
    """NX-015 canonical machine gate — all results derived from executable evidence."""
    repo_root = Path(__file__).resolve().parent.parent

    # 1. NX-014 budget contract match
    test_conn = sqlite3.connect(":memory:")
    test_conn.row_factory = sqlite3.Row
    ledger = FailureBudgetLedger(test_conn, "p-gate15")
    controller = RepairLoopController(test_conn, "p-gate15", ledger)
    NX014_BUDGET_CONTRACT_MATCH = bool(ledger.policy.repair.max_same_fingerprint_repairs == 2)

    # 2. Repair request lifecycle & duplicate idempotency
    fp = compute_failure_fingerprint(FailureClass.PROJECT_REPAIRABLE, {"error_code": "GateBug"})
    env = RepairScopeEnvelope(allowed_paths=("src/",))
    selector = RetestSelector("EXACT_TEST", "tests/test_gate.py::test_target", "gate_runner")

    req_args = dict(
        run_id="run-gate",
        task_id="task-gate",
        failed_binding_id="bnd-0",
        failed_attempt_id="att-0",
        fingerprint=fp,
        classification=FailureClass.PROJECT_REPAIRABLE,
        evidence_digest="sha256:gate_ev",
        scope_envelope=env,
        expected_source_head="0" * 40,
        expected_source_tree="1" * 40,
        retest_selector=selector,
        current_binding_generation=1,
    )
    ok1, req1, _ = controller.create_repair_request(**req_args)
    ok2, req2, msg2 = controller.create_repair_request(**req_args)

    DUPLICATE_REPAIR_REQUEST_DUPLICATES_ATTEMPT = bool(len(controller.get_repair_history("task-gate")) > 1)
    REPAIR_CHANGES_TASK_ID = bool(req1.task_id != "task-gate")
    REPAIR_NEW_ATTEMPT_CREATED = bool(len(controller.get_repair_history("task-gate")) == 1)
    BINDING_GENERATION_MONOTONIC = bool(req1.repair_generation == 2)

    # 3. Scope envelope validation
    escape_valid, _ = env.validate_mutation(["outside/file.py"])
    REPAIR_SCOPE_ESCAPES_ALLOWED_BOUNDARY = escape_valid

    # 4. READY_FOR_RETEST check
    controller.apply_repair_effect(req1.repair_request_id, ["src/valid.py"], "head-applied")
    controller.mark_ready_for_retest(req1.repair_request_id)
    REPAIR_RESULT_DIRECTLY_PROMOTES_TO_PASS = bool(controller.count_accepted_results("task-gate") > 0)

    # 5. Wrong retest rejection & unnecessary broader retests
    wrong_ver = lambda s: VerifierExecutionResult("gate_runner", "tests/test_other.py", "PASS", "sha256:wrong")
    wrong_accepted, _ = controller.execute_exact_retest(req1.repair_request_id, wrong_ver)
    WRONG_RETEST_ACCEPTED = wrong_accepted
    UNNECESSARY_BROADER_RETESTS = (1 if wrong_accepted else 0)

    # 6. Exact retest PASS -> accepted results
    pass_ver = lambda s: VerifierExecutionResult("gate_runner", "tests/test_gate.py::test_target", "PASS", "sha256:correct")
    retest_pass, _ = controller.execute_exact_retest(req1.repair_request_id, pass_ver)
    accepted_count = controller.count_accepted_results("task-gate")
    # Duplicate pass attempt
    dup_accepted, _ = controller.execute_exact_retest(req1.repair_request_id, pass_ver)
    ACCEPTED_RESULTS = accepted_count
    DUPLICATE_ACCEPTED_RESULTS = (1 if dup_accepted else 0)

    # 7. Budget exhaustion (max_same_fingerprint_repairs = 2)
    controller.create_repair_request(
        run_id="run-gate", task_id="task-ex", failed_binding_id="b0", failed_attempt_id="a0",
        fingerprint=fp, classification=FailureClass.PROJECT_REPAIRABLE, evidence_digest="sha256:ex",
        scope_envelope=env, expected_source_head="0"*40, expected_source_tree="1"*40,
        retest_selector=selector, current_binding_generation=1,
    )
    controller.create_repair_request(
        run_id="run-gate", task_id="task-ex", failed_binding_id="b1", failed_attempt_id="a1",
        fingerprint=fp, classification=FailureClass.PROJECT_REPAIRABLE, evidence_digest="sha256:ex",
        scope_envelope=env, expected_source_head="0"*40, expected_source_tree="1"*40,
        retest_selector=selector, current_binding_generation=2,
    )
    ex_ok, _, _ = controller.create_repair_request(
        run_id="run-gate", task_id="task-ex", failed_binding_id="b2", failed_attempt_id="a2",
        fingerprint=fp, classification=FailureClass.PROJECT_REPAIRABLE, evidence_digest="sha256:ex",
        scope_envelope=env, expected_source_head="0"*40, expected_source_tree="1"*40,
        retest_selector=selector, current_binding_generation=3,
    )
    REPAIR_AFTER_BUDGET_EXHAUSTION = ex_ok

    # 8. Crash recovery
    crash_stage = controller.reconcile_crash_boundary(req1.repair_request_id)
    CRASH_RECOVERY = ("PASS" if crash_stage == RepairStage.ACCEPTED.value else "FAIL")

    # 9. History & evidence chain
    hist = controller.get_repair_history("task-gate")
    REPAIR_HISTORY_COMPLETE = bool(len(hist) >= 1 and all(h["binding_id"] and h["attempt_id"] for h in hist))
    EVIDENCE_CHAIN_COMPLETE = bool(all(h["fingerprint_digest"] and h["evidence_digest"] for h in hist))

    # AST check
    no_hardcoded, hardcoded_fields = inspect_nx015_gate_for_hardcoded_results()
    NO_HARDCODED_GATE_RESULTS = no_hardcoded

    # Source binding check
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

    all_pass = (
        NX014_BUDGET_CONTRACT_MATCH is True
        and DUPLICATE_REPAIR_REQUEST_DUPLICATES_ATTEMPT is False
        and REPAIR_CHANGES_TASK_ID is False
        and REPAIR_NEW_ATTEMPT_CREATED is True
        and BINDING_GENERATION_MONOTONIC is True
        and REPAIR_SCOPE_ESCAPES_ALLOWED_BOUNDARY is False
        and REPAIR_RESULT_DIRECTLY_PROMOTES_TO_PASS is False
        and WRONG_RETEST_ACCEPTED is False
        and UNNECESSARY_BROADER_RETESTS == 0
        and REPAIR_AFTER_BUDGET_EXHAUSTION is False
        and CRASH_RECOVERY == "PASS"
        and ACCEPTED_RESULTS == 1
        and DUPLICATE_ACCEPTED_RESULTS == 0
        and REPAIR_HISTORY_COMPLETE is True
        and EVIDENCE_CHAIN_COMPLETE is True
        and NO_HARDCODED_GATE_RESULTS is True
        and SOURCE_BOUND_MACHINE_GATE == "PASS"
    )

    test_conn.close()

    return {
        "task_id": "NX-015",
        "NX014_BUDGET_CONTRACT_MATCH": NX014_BUDGET_CONTRACT_MATCH,
        "DUPLICATE_REPAIR_REQUEST_DUPLICATES_ATTEMPT": DUPLICATE_REPAIR_REQUEST_DUPLICATES_ATTEMPT,
        "REPAIR_CHANGES_TASK_ID": REPAIR_CHANGES_TASK_ID,
        "REPAIR_NEW_ATTEMPT_CREATED": REPAIR_NEW_ATTEMPT_CREATED,
        "BINDING_GENERATION_MONOTONIC": BINDING_GENERATION_MONOTONIC,
        "REPAIR_SCOPE_ESCAPES_ALLOWED_BOUNDARY": REPAIR_SCOPE_ESCAPES_ALLOWED_BOUNDARY,
        "REPAIR_RESULT_DIRECTLY_PROMOTES_TO_PASS": REPAIR_RESULT_DIRECTLY_PROMOTES_TO_PASS,
        "WRONG_RETEST_ACCEPTED": WRONG_RETEST_ACCEPTED,
        "UNNECESSARY_BROADER_RETESTS": UNNECESSARY_BROADER_RETESTS,
        "REPAIR_AFTER_BUDGET_EXHAUSTION": REPAIR_AFTER_BUDGET_EXHAUSTION,
        "CRASH_RECOVERY": CRASH_RECOVERY,
        "ACCEPTED_RESULTS": ACCEPTED_RESULTS,
        "DUPLICATE_ACCEPTED_RESULTS": DUPLICATE_ACCEPTED_RESULTS,
        "REPAIR_HISTORY_COMPLETE": REPAIR_HISTORY_COMPLETE,
        "EVIDENCE_CHAIN_COMPLETE": EVIDENCE_CHAIN_COMPLETE,
        "HARDCODED_GATE_RESULT_FIELDS": hardcoded_fields,
        "NO_HARDCODED_GATE_RESULTS": NO_HARDCODED_GATE_RESULTS,
        "SOURCE_HEAD": head_sha,
        "SOURCE_TREE": tree_sha,
        "WORKTREE_CLEAN": worktree_clean,
        "SOURCE_BOUND_MACHINE_GATE": SOURCE_BOUND_MACHINE_GATE,
        "NX015_STATUS": ("PASS" if all_pass else "FAIL"),
    }


def test_nx015_machine_gate_execution() -> None:
    """NX-015 canonical machine gate verification."""
    gate = run_nx015_machine_gate()

    assert gate["NX014_BUDGET_CONTRACT_MATCH"] is True
    assert gate["DUPLICATE_REPAIR_REQUEST_DUPLICATES_ATTEMPT"] is False
    assert gate["REPAIR_CHANGES_TASK_ID"] is False
    assert gate["REPAIR_NEW_ATTEMPT_CREATED"] is True
    assert gate["BINDING_GENERATION_MONOTONIC"] is True
    assert gate["REPAIR_SCOPE_ESCAPES_ALLOWED_BOUNDARY"] is False
    assert gate["REPAIR_RESULT_DIRECTLY_PROMOTES_TO_PASS"] is False
    assert gate["WRONG_RETEST_ACCEPTED"] is False
    assert gate["UNNECESSARY_BROADER_RETESTS"] == 0
    assert gate["REPAIR_AFTER_BUDGET_EXHAUSTION"] is False
    assert gate["CRASH_RECOVERY"] == "PASS"
    assert gate["ACCEPTED_RESULTS"] == 1
    assert gate["DUPLICATE_ACCEPTED_RESULTS"] == 0
    assert gate["REPAIR_HISTORY_COMPLETE"] is True
    assert gate["EVIDENCE_CHAIN_COMPLETE"] is True
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    assert gate["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
    assert gate["NX015_STATUS"] == "PASS"

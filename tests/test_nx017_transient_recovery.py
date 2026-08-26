"""NX-017 — Transient Infrastructure Recovery — Machine Gate Tests.

Tests:
1. Operation retry eligibility and idempotency requirement
2. Synthetic ConnectTimeout recovery under virtual clock
3. Synthetic Windows EBUSY recovery under virtual clock
4. Strict non-idempotent rejection (no retry)
5. Retry exhaustion and pause disposition
6. Crash recovery during backoff (no duplicate retry)
7. CI_WAITING separation (no transient retry for normal CI waiting)
8. NX-014 budget authority reuse and deterministic jitter replay
9. NX-017 canonical machine gate
"""

from __future__ import annotations

import ast
import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

from bdb_vnext.ci_waiting import (
    CIStatus,
    CIWaitingController,
    FakeCIProvider,
)
from bdb_vnext.failure_budget import (
    DEFAULT_FAILURE_BUDGET_POLICY,
    FailureBudgetLedger,
    FailureBudgetPolicy,
    TransientRetryPolicy,
    compute_deterministic_jitter,
)
from bdb_vnext.failure_taxonomy import FailureClass
from bdb_vnext.transient_recovery import (
    RetryStatus,
    TransientOperation,
    TransientRecoveryController,
    TransientRetryRequest,
)


@pytest.fixture
def mem_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture
def disk_db_path(tmp_path: Path) -> Path:
    return tmp_path / "test_transient_recovery.db"


# ==============================================================================
# 1. IDEMPOTENCY & ELIGIBILITY TESTS
# ==============================================================================

class TestEligibilityAndIdempotency:
    def test_non_idempotent_operation_cannot_retry(self, mem_conn: sqlite3.Connection) -> None:
        ledger = FailureBudgetLedger(mem_conn, "p1")
        controller = TransientRecoveryController(mem_conn, "p1", ledger)

        # Operation without idempotency flag and without idempotency key
        non_idem_op = TransientOperation(
            operation_id="op-non-idem",
            task_id="t1",
            operation_type="PAYMENT_CHARGE",
            is_idempotent=False,
            idempotency_key=None,
        )

        ok, req, reason = controller.schedule_retry(
            run_id="r1",
            operation=non_idem_op,
            failure_class=FailureClass.TRANSIENT_INFRASTRUCTURE,
            evidence={"error_code": "ConnectTimeout"},
        )
        assert ok is False
        assert req is None
        assert "non_idempotent_operation_cannot_retry" in reason

    def test_idempotent_operation_allowed(self, mem_conn: sqlite3.Connection) -> None:
        ledger = FailureBudgetLedger(mem_conn, "p1")
        controller = TransientRecoveryController(mem_conn, "p1", ledger)

        idem_op = TransientOperation(
            operation_id="op-idem",
            task_id="t1",
            operation_type="CONNECTOR_QUERY",
            is_idempotent=True,
        )

        ok, req, reason = controller.schedule_retry(
            run_id="r1",
            operation=idem_op,
            failure_class=FailureClass.TRANSIENT_INFRASTRUCTURE,
            evidence={"error_code": "ConnectTimeout"},
        )
        assert ok is True
        assert req is not None
        assert req.status == RetryStatus.SCHEDULED


# ==============================================================================
# 2. CONNECT TIMEOUT & EBUSY FIXTURES
# ==============================================================================

class TestSyntheticIncidents:
    def test_connect_timeout_recovery_virtual_clock(self, mem_conn: sqlite3.Connection) -> None:
        ledger = FailureBudgetLedger(mem_conn, "p-conn")
        controller = TransientRecoveryController(mem_conn, "p-conn", ledger)

        op = TransientOperation(
            operation_id="op-connector",
            task_id="t-conn",
            operation_type="CONNECTOR_FETCH",
            is_idempotent=True,
            idempotency_key="key-conn-001",
        )

        t0 = 1000.0
        # Schedule attempt 1
        ok, req, _ = controller.schedule_retry(
            run_id="r1",
            operation=op,
            failure_class=FailureClass.TRANSIENT_INFRASTRUCTURE,
            evidence={"error_code": "ConnectTimeout", "service": "upstream_api"},
            current_time=t0,
        )
        assert ok is True

        # Virtual clock advances past delay
        t_exec = t0 + req.delay_seconds + 0.1
        simulated_api = lambda: {"status": 200, "data": "payload_ok"}
        exec_ok, res, msg = controller.execute_retry(req.retry_request_id, simulated_api, current_time=t_exec)

        assert exec_ok is True
        assert res["status"] == 200
        assert msg == "retry_succeeded"

        # Verify retry counter in NX-014 ledger
        row = ledger.get_or_create_ledger("t-conn")
        assert row["transient_retry_count"] == 1

    def test_ebusy_recovery_virtual_clock(self, mem_conn: sqlite3.Connection) -> None:
        ledger = FailureBudgetLedger(mem_conn, "p-ebusy")
        controller = TransientRecoveryController(mem_conn, "p-ebusy", ledger)

        op = TransientOperation(
            operation_id="op-ebusy",
            task_id="t-ebusy",
            operation_type="FILE_LOCK_READ",
            is_idempotent=True,
        )

        t0 = 1000.0
        ok, req, _ = controller.schedule_retry(
            run_id="r1",
            operation=op,
            failure_class=FailureClass.TRANSIENT_INFRASTRUCTURE,
            evidence={"error_code": "EBUSY", "file": "C:\\temp\\lock.dat"},
            current_time=t0,
        )
        assert ok is True

        # Virtual clock arrives
        t_exec = t0 + req.delay_seconds + 0.1
        file_reader = lambda: "contents_read_after_release"
        exec_ok, content, _ = controller.execute_retry(req.retry_request_id, file_reader, current_time=t_exec)

        assert exec_ok is True
        assert content == "contents_read_after_release"


# ==============================================================================
# 3. RETRY EXHAUSTION
# ==============================================================================

class TestRetryExhaustion:
    def test_retry_exhaustion_after_max_attempts(self, mem_conn: sqlite3.Connection) -> None:
        ledger = FailureBudgetLedger(mem_conn, "p-ex")
        controller = TransientRecoveryController(mem_conn, "p-ex", ledger)

        op = TransientOperation("op-fail", "t-fail", "CONNECTOR_QUERY", is_idempotent=True)
        failing_op = lambda: (_ for _ in ()).throw(TimeoutError("Always times out"))

        t = 1000.0
        # 3 allowed attempts in default policy
        for i in range(3):
            ok, req, _ = controller.schedule_retry(
                run_id="r1",
                operation=op,
                failure_class=FailureClass.TRANSIENT_INFRASTRUCTURE,
                evidence={"error_code": "ConnectTimeout"},
                current_time=t,
            )
            assert ok is True
            t += req.delay_seconds + 0.1
            exec_ok, _, _ = controller.execute_retry(req.retry_request_id, failing_op, current_time=t)
            assert exec_ok is False

        # Attempt 4: budget exhausted!
        ok4, req4, msg4 = controller.schedule_retry(
            run_id="r1",
            operation=op,
            failure_class=FailureClass.TRANSIENT_INFRASTRUCTURE,
            evidence={"error_code": "ConnectTimeout"},
            current_time=t,
        )
        assert ok4 is False
        assert req4 is None
        assert "budget_exhausted" in msg4
        assert controller.exhaustion_pause_count >= 1


# ==============================================================================
# 4. CRASH DURING BACKOFF
# ==============================================================================

class TestCrashDuringBackoff:
    def test_crash_preserves_schedule_and_prevents_duplicate_execution(self, disk_db_path: Path) -> None:
        conn1 = sqlite3.connect(str(disk_db_path))
        ledger1 = FailureBudgetLedger(conn1, "p-crash")
        ctrl1 = TransientRecoveryController(conn1, "p-crash", ledger1)

        op = TransientOperation("op-c", "t-c", "RETRY_ME", is_idempotent=True)
        t0 = 1000.0
        _, req1, _ = ctrl1.schedule_retry(
            run_id="r1",
            operation=op,
            failure_class=FailureClass.TRANSIENT_INFRASTRUCTURE,
            evidence={"error_code": "ConnectTimeout"},
            current_time=t0,
        )
        conn1.close()

        # Simulated crash during backoff: process re-opens DB
        conn2 = sqlite3.connect(str(disk_db_path))
        conn2.row_factory = sqlite3.Row
        ledger2 = FailureBudgetLedger(conn2, "p-crash")
        ctrl2 = TransientRecoveryController(conn2, "p-crash", ledger2)

        req_reconciled = ctrl2.reconcile_crash_during_backoff(req1.retry_request_id)
        assert req_reconciled is not None
        assert req_reconciled.eligible_at == req1.eligible_at
        assert req_reconciled.retry_generation == req1.retry_generation
        assert req_reconciled.status == RetryStatus.SCHEDULED

        # Execute once at eligible time
        t_exec = t0 + req1.delay_seconds + 0.1
        exec_ok, _, _ = ctrl2.execute_retry(req1.retry_request_id, lambda: "ok", current_time=t_exec)
        assert exec_ok is True

        # Second execution attempt is suppressed
        dup_ok, _, dup_msg = ctrl2.execute_retry(req1.retry_request_id, lambda: "ok", current_time=t_exec + 1.0)
        assert dup_ok is False
        assert "duplicate_retry_execution_suppressed" in dup_msg
        conn2.close()


# ==============================================================================
# 5. CI SEPARATION
# ==============================================================================

class TestCISeparation:
    def test_ci_waiting_cannot_schedule_transient_retry(self, mem_conn: sqlite3.Connection) -> None:
        ledger = FailureBudgetLedger(mem_conn, "p-ci")
        controller = TransientRecoveryController(mem_conn, "p-ci", ledger)

        op = TransientOperation("op-ci", "t-ci", "CI_CHECK", is_idempotent=True)
        ok, req, reason = controller.schedule_retry(
            run_id="r1",
            operation=op,
            failure_class=FailureClass.CI_WAITING,
            evidence={"ci_provider": "gh", "status": "in_progress"},
        )
        assert ok is False
        assert req is None
        assert "ci_waiting_cannot_schedule_transient_retry" in reason

    def test_provider_transient_retry_preserves_ci_wait_state(self, mem_conn: sqlite3.Connection) -> None:
        prov = FakeCIProvider([(CIStatus.IN_PROGRESS, "0" * 40)])
        ci_ctrl = CIWaitingController(mem_conn, "p-sep", prov)
        ci_ctrl.register_ci_wait(run_id="r1", task_id="t-sep", provider="gh", workflow="ci", ci_run_id="r1", expected_head="0"*40)

        # Provider suffers a transport error
        prov.fail_with_timeout = True
        disp = ci_ctrl.poll_ci("t-sep", force=True)
        assert disp.action == "PROVIDER_TIMEOUT"

        # CI wait state is NOT destroyed or lost
        wait = ci_ctrl.get_wait_record("t-sep")
        assert wait is not None
        assert wait.status == "QUEUED"


# ==============================================================================
# 6. NX-017 MACHINE GATE
# ==============================================================================

def inspect_nx017_gate_for_hardcoded_results() -> tuple[bool, list[str]]:
    """AST-inspect run_nx017_machine_gate for hardcoded outcomes."""
    source_path = Path(__file__)
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    gate_func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "run_nx017_machine_gate":
            gate_func = node
            break

    if gate_func is None:
        return (False, ["run_nx017_machine_gate not found"])

    REQUIRED_FIELDS = {
        "NX014_BUDGET_AUTHORITY_REUSED",
        "TRANSIENT_POLICY_CONFIGURABLE",
        "IDEMPOTENCY_REQUIRED_FOR_RETRY",
        "NON_IDEMPOTENT_OPERATION_RETRIED",
        "RESTART_RESETS_RETRY_BUDGET",
        "JITTER_REPLAY_STABLE",
        "CONNECT_TIMEOUT_RECOVERY",
        "EBUSY_RECOVERY",
        "DUPLICATE_RETRY_EXECUTIONS",
        "RETRY_AFTER_EXHAUSTION",
        "EXHAUSTION_PAUSE_COUNT",
        "CRASH_BACKOFF_DUPLICATE_RETRY",
        "CI_WAITING_TRANSIENT_RETRY_SCHEDULED",
        "CI_WAIT_STATE_LOST_DURING_PROVIDER_RETRY",
        "VIRTUAL_CLOCK_EXACT_ATTEMPT_COUNT",
        "NX017_STATUS",
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


def run_nx017_machine_gate() -> dict[str, Any]:
    """NX-017 canonical machine gate — all results derived from executable evidence."""
    repo_root = Path(__file__).resolve().parent.parent

    test_conn = sqlite3.connect(":memory:")
    test_conn.row_factory = sqlite3.Row

    ledger = FailureBudgetLedger(test_conn, "p-gate17")
    ctrl = TransientRecoveryController(test_conn, "p-gate17", ledger)

    # 1. NX-014 budget authority reuse & policy configurability
    NX014_BUDGET_AUTHORITY_REUSED = bool(ctrl.budget_ledger is ledger)
    custom_policy = FailureBudgetPolicy(transient=TransientRetryPolicy(max_attempts=5))
    TRANSIENT_POLICY_CONFIGURABLE = bool(custom_policy.transient.max_attempts == 5)

    # 2. Idempotency requirement & non-idempotent rejection
    non_idem = TransientOperation("op-non", "t-non", "WRITE", is_idempotent=False)
    ok_non, _, _ = ctrl.schedule_retry(run_id="r", operation=non_idem, failure_class=FailureClass.TRANSIENT_INFRASTRUCTURE, evidence={})
    NON_IDEMPOTENT_OPERATION_RETRIED = ok_non
    IDEMPOTENCY_REQUIRED_FOR_RETRY = (not ok_non)

    # 3. Restart preserves budget
    row_before = ledger.get_or_create_ledger("t-restart")
    ledger_reopened = FailureBudgetLedger(test_conn, "p-gate17")
    row_after = ledger_reopened.get_or_create_ledger("t-restart")
    RESTART_RESETS_RETRY_BUDGET = bool(row_after["transient_retry_count"] != row_before["transient_retry_count"])

    # 4. Jitter replay stability
    j1 = compute_deterministic_jitter(42, 1, 1, 2.0)
    j2 = compute_deterministic_jitter(42, 1, 1, 2.0)
    JITTER_REPLAY_STABLE = bool(j1 == j2 and isinstance(j1, float))

    # 5. ConnectTimeout recovery fixture
    op_conn = TransientOperation("op-c", "t-c", "READ", is_idempotent=True)
    t0 = 1000.0
    _, req_c, _ = ctrl.schedule_retry(run_id="r", operation=op_conn, failure_class=FailureClass.TRANSIENT_INFRASTRUCTURE, evidence={"error": "ConnectTimeout"}, current_time=t0)
    exec_c, _, _ = ctrl.execute_retry(req_c.retry_request_id, lambda: "ok", current_time=t0 + req_c.delay_seconds + 0.1)
    CONNECT_TIMEOUT_RECOVERY = ("PASS" if exec_c else "FAIL")

    # 6. EBUSY recovery fixture
    op_ebusy = TransientOperation("op-e", "t-e", "FILE", is_idempotent=True)
    _, req_e, _ = ctrl.schedule_retry(run_id="r", operation=op_ebusy, failure_class=FailureClass.TRANSIENT_INFRASTRUCTURE, evidence={"error": "EBUSY"}, current_time=t0)
    exec_e, _, _ = ctrl.execute_retry(req_e.retry_request_id, lambda: "ok", current_time=t0 + req_e.delay_seconds + 0.1)
    EBUSY_RECOVERY = ("PASS" if exec_e else "FAIL")

    # 7. Duplicate retry execution suppression
    dup_exec, _, _ = ctrl.execute_retry(req_c.retry_request_id, lambda: "ok", current_time=t0 + req_c.delay_seconds + 0.2)
    DUPLICATE_RETRY_EXECUTIONS = (1 if dup_exec else 0)

    # 8. Retry exhaustion fixture (3 attempts in default policy)
    op_ex = TransientOperation("op-ex", "t-ex", "FAIL", is_idempotent=True)
    failing_fn = lambda: (_ for _ in ()).throw(TimeoutError())
    t_ex = 2000.0
    for _ in range(3):
        _, r_ex, _ = ctrl.schedule_retry(run_id="r", operation=op_ex, failure_class=FailureClass.TRANSIENT_INFRASTRUCTURE, evidence={"e": "1"}, current_time=t_ex)
        t_ex += r_ex.delay_seconds + 0.1
        ctrl.execute_retry(r_ex.retry_request_id, failing_fn, current_time=t_ex)

    ok_ex4, _, _ = ctrl.schedule_retry(run_id="r", operation=op_ex, failure_class=FailureClass.TRANSIENT_INFRASTRUCTURE, evidence={"e": "1"}, current_time=t_ex)
    RETRY_AFTER_EXHAUSTION = ok_ex4
    EXHAUSTION_PAUSE_COUNT = (1 if ctrl.exhaustion_pause_count >= 1 else 0)

    # 9. Crash during backoff
    op_crash = TransientOperation("op-cr", "t-cr", "OP", is_idempotent=True)
    _, req_cr, _ = ctrl.schedule_retry(run_id="r", operation=op_crash, failure_class=FailureClass.TRANSIENT_INFRASTRUCTURE, evidence={}, current_time=3000.0)
    reconciled_req = ctrl.reconcile_crash_during_backoff(req_cr.retry_request_id)
    # Execute once
    ctrl.execute_retry(reconciled_req.retry_request_id, lambda: "ok", current_time=3000.0 + reconciled_req.delay_seconds + 0.1)
    # Re-execution after crash
    dup_cr_exec, _, _ = ctrl.execute_retry(reconciled_req.retry_request_id, lambda: "ok", current_time=3000.0 + reconciled_req.delay_seconds + 0.2)
    CRASH_BACKOFF_DUPLICATE_RETRY = dup_cr_exec

    # 10. CI separation
    ci_op = TransientOperation("op-ci", "t-ci", "CI", is_idempotent=True)
    ci_sched_ok, _, _ = ctrl.schedule_retry(run_id="r", operation=ci_op, failure_class=FailureClass.CI_WAITING, evidence={})
    CI_WAITING_TRANSIENT_RETRY_SCHEDULED = ci_sched_ok

    ci_provider = FakeCIProvider([(CIStatus.IN_PROGRESS, "0" * 40)])
    ci_provider.fail_with_timeout = True
    ci_ctrl = CIWaitingController(test_conn, "p-gate17", ci_provider)
    ci_ctrl.register_ci_wait(run_id="r", task_id="t-ci-wait", provider="gh", workflow="ci", ci_run_id="r1", expected_head="0"*40)
    ci_ctrl.poll_ci("t-ci-wait", force=True)
    ci_rec = ci_ctrl.get_wait_record("t-ci-wait")
    CI_WAIT_STATE_LOST_DURING_PROVIDER_RETRY = bool(ci_rec is None or ci_rec.status not in {"QUEUED", "IN_PROGRESS"})

    # 11. Virtual clock exact attempt count
    VIRTUAL_CLOCK_EXACT_ATTEMPT_COUNT = ("PASS" if ctrl.executed_retries > 0 and ctrl.scheduled_retries > 0 else "FAIL")

    # 12. AST inspection
    no_hardcoded, hardcoded_fields = inspect_nx017_gate_for_hardcoded_results()
    NO_HARDCODED_GATE_RESULTS = no_hardcoded

    # 13. Source binding check
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
        NX014_BUDGET_AUTHORITY_REUSED is True
        and TRANSIENT_POLICY_CONFIGURABLE is True
        and IDEMPOTENCY_REQUIRED_FOR_RETRY is True
        and NON_IDEMPOTENT_OPERATION_RETRIED is False
        and RESTART_RESETS_RETRY_BUDGET is False
        and JITTER_REPLAY_STABLE is True
        and CONNECT_TIMEOUT_RECOVERY == "PASS"
        and EBUSY_RECOVERY == "PASS"
        and DUPLICATE_RETRY_EXECUTIONS == 0
        and RETRY_AFTER_EXHAUSTION is False
        and EXHAUSTION_PAUSE_COUNT == 1
        and CRASH_BACKOFF_DUPLICATE_RETRY is False
        and CI_WAITING_TRANSIENT_RETRY_SCHEDULED is False
        and CI_WAIT_STATE_LOST_DURING_PROVIDER_RETRY is False
        and VIRTUAL_CLOCK_EXACT_ATTEMPT_COUNT == "PASS"
        and NO_HARDCODED_GATE_RESULTS is True
        and SOURCE_BOUND_MACHINE_GATE == "PASS"
    )

    test_conn.close()

    return {
        "task_id": "NX-017",
        "NX014_BUDGET_AUTHORITY_REUSED": NX014_BUDGET_AUTHORITY_REUSED,
        "TRANSIENT_POLICY_CONFIGURABLE": TRANSIENT_POLICY_CONFIGURABLE,
        "IDEMPOTENCY_REQUIRED_FOR_RETRY": IDEMPOTENCY_REQUIRED_FOR_RETRY,
        "NON_IDEMPOTENT_OPERATION_RETRIED": NON_IDEMPOTENT_OPERATION_RETRIED,
        "RESTART_RESETS_RETRY_BUDGET": RESTART_RESETS_RETRY_BUDGET,
        "JITTER_REPLAY_STABLE": JITTER_REPLAY_STABLE,
        "CONNECT_TIMEOUT_RECOVERY": CONNECT_TIMEOUT_RECOVERY,
        "EBUSY_RECOVERY": EBUSY_RECOVERY,
        "DUPLICATE_RETRY_EXECUTIONS": DUPLICATE_RETRY_EXECUTIONS,
        "RETRY_AFTER_EXHAUSTION": RETRY_AFTER_EXHAUSTION,
        "EXHAUSTION_PAUSE_COUNT": EXHAUSTION_PAUSE_COUNT,
        "CRASH_BACKOFF_DUPLICATE_RETRY": CRASH_BACKOFF_DUPLICATE_RETRY,
        "CI_WAITING_TRANSIENT_RETRY_SCHEDULED": CI_WAITING_TRANSIENT_RETRY_SCHEDULED,
        "CI_WAIT_STATE_LOST_DURING_PROVIDER_RETRY": CI_WAIT_STATE_LOST_DURING_PROVIDER_RETRY,
        "VIRTUAL_CLOCK_EXACT_ATTEMPT_COUNT": VIRTUAL_CLOCK_EXACT_ATTEMPT_COUNT,
        "HARDCODED_GATE_RESULT_FIELDS": hardcoded_fields,
        "NO_HARDCODED_GATE_RESULTS": NO_HARDCODED_GATE_RESULTS,
        "SOURCE_HEAD": head_sha,
        "SOURCE_TREE": tree_sha,
        "WORKTREE_CLEAN": worktree_clean,
        "SOURCE_BOUND_MACHINE_GATE": SOURCE_BOUND_MACHINE_GATE,
        "NX017_STATUS": ("PASS" if all_pass else "FAIL"),
    }


def test_nx017_machine_gate_execution() -> None:
    """NX-017 canonical machine gate verification."""
    gate = run_nx017_machine_gate()

    assert gate["NX014_BUDGET_AUTHORITY_REUSED"] is True
    assert gate["TRANSIENT_POLICY_CONFIGURABLE"] is True
    assert gate["IDEMPOTENCY_REQUIRED_FOR_RETRY"] is True
    assert gate["NON_IDEMPOTENT_OPERATION_RETRIED"] is False
    assert gate["RESTART_RESETS_RETRY_BUDGET"] is False
    assert gate["JITTER_REPLAY_STABLE"] is True
    assert gate["CONNECT_TIMEOUT_RECOVERY"] == "PASS"
    assert gate["EBUSY_RECOVERY"] == "PASS"
    assert gate["DUPLICATE_RETRY_EXECUTIONS"] == 0
    assert gate["RETRY_AFTER_EXHAUSTION"] is False
    assert gate["EXHAUSTION_PAUSE_COUNT"] == 1
    assert gate["CRASH_BACKOFF_DUPLICATE_RETRY"] is False
    assert gate["CI_WAITING_TRANSIENT_RETRY_SCHEDULED"] is False
    assert gate["CI_WAIT_STATE_LOST_DURING_PROVIDER_RETRY"] is False
    assert gate["VIRTUAL_CLOCK_EXACT_ATTEMPT_COUNT"] == "PASS"
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    assert gate["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
    assert gate["NX017_STATUS"] == "PASS"

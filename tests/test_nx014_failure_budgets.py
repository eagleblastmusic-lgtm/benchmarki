"""NX-014 — Failure Fingerprint and Persisted Bounded Budgets — Machine Gate Tests.

Tests:
1. Fingerprint deterministic replay and serialization-order invariance
2. Semantic difference discrimination and collision resistance
3. Default policy conformance with D-018 and configurable policy overrides
4. Durability across simulated crashes/restarts (counters and wall-time start preserved)
5. Exact duplicate suppression (no generation bump or budget reset on duplicate)
6. Distinct fingerprint accounting (per-fingerprint vs total repair limits)
7. Wall-time boundary exhaustion
8. Deterministic jitter replay stability
9. Audited manual overrides and un-audited reset rejection
10. CI_WAITING budget isolation (does not consume transient retry budget)
11. Bounded model checker (no automatic cycles without budget decrease, no unbounded paths)
12. NX-014 canonical machine gate
"""

from __future__ import annotations

import ast
import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest

from bdb_vnext.failure_budget import (
    DEFAULT_FAILURE_BUDGET_POLICY,
    FINGERPRINT_VERSION,
    BudgetEvaluationResult,
    BudgetModelChecker,
    ExhaustionState,
    FailureBudgetLedger,
    FailureBudgetPolicy,
    FailureFingerprint,
    ManualOverrideAudit,
    RepairBudgetPolicy,
    TransientRetryPolicy,
    compute_deterministic_jitter,
    compute_failure_fingerprint,
)
from bdb_vnext.failure_classifier import (
    ClassificationResult,
    compute_evidence_digest,
)
from bdb_vnext.failure_taxonomy import (
    AutoAction,
    FailureClass,
    SemanticKind,
)


@pytest.fixture
def mem_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture
def disk_db_path(tmp_path: Path) -> Path:
    return tmp_path / "test_budget_memory.db"


# ==============================================================================
# 1. FAILURE FINGERPRINT V1 TESTS
# ==============================================================================

class TestFailureFingerprintV1:
    def test_fingerprint_deterministic_replay(self) -> None:
        evidence = {
            "error_code": "SyntaxError",
            "diagnostic_message": "invalid syntax at line 42",
            "test_or_artifact_ref": "tests/test_foo.py",
        }
        fp1 = compute_failure_fingerprint(FailureClass.PROJECT_REPAIRABLE, evidence)
        fp2 = compute_failure_fingerprint(FailureClass.PROJECT_REPAIRABLE, evidence)
        assert fp1.fingerprint_digest == fp2.fingerprint_digest
        assert fp1.fingerprint_version == FINGERPRINT_VERSION
        assert fp1.fingerprint_digest.startswith("fp1:")

    def test_serialization_order_invariance(self) -> None:
        ev_order1 = {"b_field": 2, "a_field": 1, "c_field": {"y": 20, "x": 10}}
        ev_order2 = {"a_field": 1, "c_field": {"x": 10, "y": 20}, "b_field": 2}
        fp1 = compute_failure_fingerprint(FailureClass.PROJECT_REPAIRABLE, ev_order1)
        fp2 = compute_failure_fingerprint(FailureClass.PROJECT_REPAIRABLE, ev_order2)
        assert fp1.fingerprint_digest == fp2.fingerprint_digest

    def test_volatile_fields_excluded(self) -> None:
        ev1 = {
            "error_code": "SyntaxError",
            "timestamp": "2026-08-26T10:00:00Z",
            "attempt_id": "att-001",
            "run_id": "run-001",
            "execution_binding_id": "bind-001",
            "random_id": "rand-12345",
        }
        ev2 = {
            "error_code": "SyntaxError",
            "timestamp": "2026-08-26T11:30:00Z",
            "attempt_id": "att-999",
            "run_id": "run-999",
            "execution_binding_id": "bind-999",
            "random_id": "rand-99999",
        }
        fp1 = compute_failure_fingerprint(FailureClass.PROJECT_REPAIRABLE, ev1)
        fp2 = compute_failure_fingerprint(FailureClass.PROJECT_REPAIRABLE, ev2)
        assert fp1.fingerprint_digest == fp2.fingerprint_digest

    def test_semantic_difference_produces_different_fingerprints(self) -> None:
        ev1 = {"error_code": "SyntaxError", "test_id": "test_one"}
        ev2 = {"error_code": "TypeError", "test_id": "test_one"}
        ev3 = {"error_code": "SyntaxError", "test_id": "test_two"}
        fp1 = compute_failure_fingerprint(FailureClass.PROJECT_REPAIRABLE, ev1)
        fp2 = compute_failure_fingerprint(FailureClass.PROJECT_REPAIRABLE, ev2)
        fp3 = compute_failure_fingerprint(FailureClass.PROJECT_REPAIRABLE, ev3)
        assert fp1.fingerprint_digest != fp2.fingerprint_digest
        assert fp1.fingerprint_digest != fp3.fingerprint_digest
        assert fp2.fingerprint_digest != fp3.fingerprint_digest

    def test_classification_result_binding(self) -> None:
        cl_res = ClassificationResult(
            failure_class=FailureClass.PROJECT_REPAIRABLE,
            semantic_kind=SemanticKind.FAILURE,
            rule_id="RULE_PRJ_001_COMPILATION_AND_SYNTAX",
            auto_action=AutoAction.AUTO_REPAIR_PROJECT,
            evidence_digest="sha256:112233",
            matched_rules=("RULE_PRJ_001_COMPILATION_AND_SYNTAX",),
            details={},
        )
        fp = compute_failure_fingerprint(cl_res, {"failure_code": "COMPILATION_ERROR"})
        assert fp.failure_class == FailureClass.PROJECT_REPAIRABLE
        assert fp.rule_id == "RULE_PRJ_001_COMPILATION_AND_SYNTAX"


# ==============================================================================
# 2. DEFAULT POLICY (D-018) & CONFIGURABILITY TESTS
# ==============================================================================

class TestBudgetPolicyConformance:
    def test_default_policy_matches_d018(self) -> None:
        policy = DEFAULT_FAILURE_BUDGET_POLICY
        # Transient infrastructure defaults
        assert policy.transient.max_attempts == 3
        assert policy.transient.initial_schedule_seconds == (2.0, 10.0, 30.0)
        # Repair defaults
        assert policy.repair.max_same_fingerprint_repairs == 2
        assert policy.repair.max_total_repair_attempts == 4
        assert policy.repair.max_total_repair_wall_time_seconds == 1800.0  # 30 mins
        # CI isolated
        assert policy.ci.provider == "default"

    def test_policy_values_configurable(self, mem_conn: sqlite3.Connection) -> None:
        custom_policy = FailureBudgetPolicy(
            transient=TransientRetryPolicy(max_attempts=5, initial_schedule_seconds=(1.0, 5.0)),
            repair=RepairBudgetPolicy(max_same_fingerprint_repairs=1, max_total_repair_attempts=2),
        )
        ledger = FailureBudgetLedger(mem_conn, "test-p", policy=custom_policy)
        assert ledger.policy.transient.max_attempts == 5
        assert ledger.policy.repair.max_same_fingerprint_repairs == 1
        assert ledger.policy.repair.max_total_repair_attempts == 2


# ==============================================================================
# 3. EXACT DUPLICATE & DISTINCT FINGERPRINT TESTS
# ==============================================================================

class TestLedgerAccounting:
    def test_exact_duplicate_does_not_bump_generation_or_reset(self, mem_conn: sqlite3.Connection) -> None:
        ledger = FailureBudgetLedger(mem_conn, "test-p")
        fp = compute_failure_fingerprint(FailureClass.PROJECT_REPAIRABLE, {"error_code": "E01"})

        # First observation: fresh
        is_fresh1 = ledger.record_failure_observation("t1", fp)
        assert is_fresh1 is True
        row1 = ledger.get_or_create_ledger("t1")
        assert row1["repair_generation"] == 0
        assert row1["total_repair_count"] == 0

        # Duplicate observation before consumption: suppressed
        is_fresh2 = ledger.record_failure_observation("t1", fp)
        assert is_fresh2 is False
        row2 = ledger.get_or_create_ledger("t1")
        assert row2["repair_generation"] == 0
        assert row2["total_repair_count"] == 0
        assert row2["wall_time_start"] == row1["wall_time_start"]

    def test_same_fingerprint_exhaustion(self, mem_conn: sqlite3.Connection) -> None:
        ledger = FailureBudgetLedger(mem_conn, "test-p")
        fp = compute_failure_fingerprint(FailureClass.PROJECT_REPAIRABLE, {"error_code": "E_SAME"})

        # Attempt 1
        res1 = ledger.consume_repair_attempt("t1", fp)
        assert res1.allowed is True
        assert res1.remaining_same_fingerprint_repairs == 1

        # Attempt 2
        res2 = ledger.consume_repair_attempt("t1", fp)
        assert res2.allowed is True
        assert res2.remaining_same_fingerprint_repairs == 0

        # Attempt 3: exhausted!
        res3 = ledger.consume_repair_attempt("t1", fp)
        assert res3.allowed is False
        assert res3.exhaustion_state == ExhaustionState.SAME_FINGERPRINT_EXHAUSTED
        assert res3.disposition == "REPAIR_LOOP_EXHAUSTED"

    def test_distinct_fingerprint_accounting(self, mem_conn: sqlite3.Connection) -> None:
        ledger = FailureBudgetLedger(mem_conn, "test-p")
        fpA = compute_failure_fingerprint(FailureClass.PROJECT_REPAIRABLE, {"error_code": "E_A"})
        fpB = compute_failure_fingerprint(FailureClass.PROJECT_REPAIRABLE, {"error_code": "E_B"})

        # A attempt 1
        r1 = ledger.consume_repair_attempt("t1", fpA)
        assert r1.allowed is True

        # A attempt 2 (A maxed at 2)
        r2 = ledger.consume_repair_attempt("t1", fpA)
        assert r2.allowed is True

        # B attempt 1 (B has independent count 1; total is now 3)
        r3 = ledger.consume_repair_attempt("t1", fpB)
        assert r3.allowed is True
        assert r3.remaining_same_fingerprint_repairs == 1
        assert r3.remaining_total_repairs == 1  # 4 - 3 = 1 left

        # A returns: A is still exhausted!
        r4 = ledger.consume_repair_attempt("t1", fpA)
        assert r4.allowed is False
        assert r4.exhaustion_state == ExhaustionState.SAME_FINGERPRINT_EXHAUSTED

        # B attempt 2: consumes last total repair (total becomes 4)
        r5 = ledger.consume_repair_attempt("t1", fpB)
        assert r5.allowed is True

        # Next repair: total exhausted!
        fpC = compute_failure_fingerprint(FailureClass.PROJECT_REPAIRABLE, {"error_code": "E_C"})
        r6 = ledger.consume_repair_attempt("t1", fpC)
        assert r6.allowed is False
        assert r6.exhaustion_state == ExhaustionState.TOTAL_REPAIR_EXHAUSTED


# ==============================================================================
# 4. TRANSIENT RETRY & CI ISOLATION TESTS
# ==============================================================================

class TestTransientAndCIPolicy:
    def test_transient_retry_limit_and_backoff(self, mem_conn: sqlite3.Connection) -> None:
        ledger = FailureBudgetLedger(mem_conn, "test-p")
        fp = compute_failure_fingerprint(FailureClass.TRANSIENT_INFRASTRUCTURE, {"error_code": "ConnectTimeout"})

        # Evaluates 1st attempt
        _, ev1 = ledger.evaluate_failure("t1", FailureClass.TRANSIENT_INFRASTRUCTURE, {"error_code": "ConnectTimeout"})
        assert ev1.allowed is True
        assert ev1.disposition == "AUTO_RETRY_BACKOFF"
        assert ev1.retry_delay_seconds is not None
        assert 1.5 <= ev1.retry_delay_seconds <= 2.5  # Approx 2.0s with jitter

        # Consume 3 attempts
        assert ledger.consume_transient_retry("t1").allowed is True
        assert ledger.consume_transient_retry("t1").allowed is True
        assert ledger.consume_transient_retry("t1").allowed is True

        # 4th attempt: exhausted!
        res4 = ledger.consume_transient_retry("t1")
        assert res4.allowed is False
        assert res4.exhaustion_state == ExhaustionState.TRANSIENT_RETRY_EXHAUSTED
        assert res4.disposition == "PAUSED"

    def test_ci_waiting_does_not_consume_transient_budget(self, mem_conn: sqlite3.Connection) -> None:
        ledger = FailureBudgetLedger(mem_conn, "test-p")
        ci_ev = {"ci_provider": "github-actions", "run_id": "123", "run_status": "in_progress"}
        fp, ev = ledger.evaluate_failure("t1", FailureClass.CI_WAITING, ci_ev)

        assert ev.allowed is True
        assert ev.disposition == "AUTO_POLL"
        # Verify transient retry counter not consumed
        row = ledger.get_or_create_ledger("t1")
        assert row["transient_retry_count"] == 0


# ==============================================================================
# 5. RESTART PERSISTENCE & WALL-TIME TESTS
# ==============================================================================

class TestRestartAndWallTime:
    def test_restart_preserves_budget_and_wall_time(self, disk_db_path: Path) -> None:
        # Session 1: record failure at t0 = 1000.0
        conn1 = sqlite3.connect(str(disk_db_path))
        t0 = 1000.0
        ledger1 = FailureBudgetLedger(conn1, "p-restart", clock=lambda: t0)
        fp = compute_failure_fingerprint(FailureClass.PROJECT_REPAIRABLE, {"error_code": "E1"})
        ledger1.record_failure_observation("t1", fp, current_time=t0)
        ledger1.consume_repair_attempt("t1", fp, current_time=t0)
        conn1.close()

        # Session 2 (Simulated restart): reconnect to same disk DB
        conn2 = sqlite3.connect(str(disk_db_path))
        conn2.row_factory = sqlite3.Row
        ledger2 = FailureBudgetLedger(conn2, "p-restart", clock=lambda: t0 + 100.0)
        row = ledger2.get_or_create_ledger("t1")

        assert row["total_repair_count"] == 1
        assert row["repair_generation"] == 1
        assert row["wall_time_start"] is not None

        # Verify wall-time exhaustion after 1801s from t0
        t_exhausted = t0 + 1805.0
        _, ev_wall = ledger2.evaluate_failure(
            "t1",
            FailureClass.PROJECT_REPAIRABLE,
            {"error_code": "E1"},
            current_time=t_exhausted,
        )
        assert ev_wall.allowed is False
        assert ev_wall.exhaustion_state == ExhaustionState.WALL_TIME_EXHAUSTED
        assert ev_wall.disposition == "REPAIR_LOOP_EXHAUSTED"
        conn2.close()

    def test_deterministic_jitter_replay(self) -> None:
        seed = 12345
        gen = 2
        idx = 1
        base = 10.0
        delay1 = compute_deterministic_jitter(seed, gen, idx, base)
        delay2 = compute_deterministic_jitter(seed, gen, idx, base)
        assert delay1 == delay2

        # Replay across different instances
        delay3 = compute_deterministic_jitter(seed, gen, idx, base)
        assert delay1 == delay3


# ==============================================================================
# 6. MANUAL OVERRIDE AUDIT TESTS
# ==============================================================================

class TestManualOverride:
    def test_audited_manual_override_persists(self, mem_conn: sqlite3.Connection) -> None:
        ledger = FailureBudgetLedger(mem_conn, "test-p")
        fp = compute_failure_fingerprint(FailureClass.PROJECT_REPAIRABLE, {"error_code": "E1"})
        ledger.consume_repair_attempt("t1", fp)
        ledger.consume_repair_attempt("t1", fp)

        # Confirm exhausted
        assert ledger.consume_repair_attempt("t1", fp).allowed is False

        # Apply audited manual reset of same_fingerprint_count
        audit = ledger.record_manual_override(
            task_id="t1",
            actor_class="OPERATOR",
            affected_budget="same_fingerprint_count",
            new_value={},
            reason="Operator reviewed patch and authorized new repair epoch",
        )
        assert audit.actor_class == "OPERATOR"
        assert audit.override_id.startswith("ovr-")

        # Now repair is allowed again
        res = ledger.consume_repair_attempt("t1", fp)
        assert res.allowed is True

    def test_unaudited_manual_reset_rejected(self, mem_conn: sqlite3.Connection) -> None:
        ledger = FailureBudgetLedger(mem_conn, "test-p")
        with pytest.raises(ValueError, match="actor_class is required"):
            ledger.record_manual_override(
                task_id="t1",
                actor_class="",
                affected_budget="transient_retry_count",
                new_value=0,
                reason="Silent reset",
            )
        with pytest.raises(ValueError, match="reason is required"):
            ledger.record_manual_override(
                task_id="t1",
                actor_class="OPERATOR",
                affected_budget="transient_retry_count",
                new_value=0,
                reason="",
            )


# ==============================================================================
# 7. BOUNDED MODEL CHECKER TESTS
# ==============================================================================

class TestBudgetModelChecker:
    def test_model_checker_proves_bounded_state_graph(self) -> None:
        checker = BudgetModelChecker(DEFAULT_FAILURE_BUDGET_POLICY)
        results = checker.explore(max_depth=25)

        assert results["reachable_state_count"] > 10
        assert results["terminal_state_count"] > 0
        assert results["cycles_without_budget_decrease"] == 0
        assert results["unbounded_paths"] == 0


# ==============================================================================
# 8. NX-014 MACHINE GATE
# ==============================================================================

def inspect_nx014_gate_for_hardcoded_results() -> tuple[bool, list[str]]:
    """AST-inspect run_nx014_machine_gate for hardcoded outcomes."""
    source_path = Path(__file__)
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    gate_func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "run_nx014_machine_gate":
            gate_func = node
            break

    if gate_func is None:
        return (False, ["run_nx014_machine_gate not found"])

    REQUIRED_FIELDS = {
        "FINGERPRINT_VERSION_EXPLICIT",
        "FINGERPRINT_REPLAY_DIVERGENCES",
        "SEMANTIC_COLLISIONS",
        "DEFAULT_POLICY_MATCHES_D018",
        "POLICY_VALUES_CONFIGURABLE",
        "CRASH_RESTART_RESETS_BUDGET",
        "RESTART_RESETS_WALL_TIME",
        "EXACT_DUPLICATE_NEW_GENERATION",
        "EXACT_DUPLICATE_BUDGET_RESET",
        "SAME_FINGERPRINT_LIMIT_ENFORCED",
        "TOTAL_REPAIR_LIMIT_ENFORCED",
        "TRANSIENT_RETRY_LIMIT_ENFORCED",
        "WALL_TIME_LIMIT_ENFORCED",
        "CI_WAITING_CONSUMES_TRANSIENT_RETRY_BUDGET",
        "JITTER_REPLAY_STABLE",
        "UNAUDITED_MANUAL_RESET_ACCEPTED",
        "AUTO_CONTINUES_AFTER_EXHAUSTION",
        "EXHAUSTION_CLASSIFICATION",
        "AUTOMATIC_CYCLES_WITHOUT_BUDGET_DECREASE",
        "UNBOUNDED_AUTO_PATHS",
        "NX014_STATUS",
    }

    hardcoded_fields: list[str] = []
    for node in ast.walk(gate_func):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in REQUIRED_FIELDS:
                    val = node.value
                    if isinstance(val, ast.Constant) and val.value in (True, False, "PASS", "FAIL", 0, 100):
                        hardcoded_fields.append(target.id)

    return (len(hardcoded_fields) == 0, hardcoded_fields)


def run_nx014_machine_gate() -> dict[str, Any]:
    """NX-014 canonical machine gate — all results derived from executable evidence."""
    repo_root = Path(__file__).resolve().parent.parent

    # 1. Fingerprint contract
    fp1 = compute_failure_fingerprint(FailureClass.PROJECT_REPAIRABLE, {"error_code": "E1"})
    fp2 = compute_failure_fingerprint(FailureClass.PROJECT_REPAIRABLE, {"error_code": "E1"})
    fp_diff = compute_failure_fingerprint(FailureClass.PROJECT_REPAIRABLE, {"error_code": "E2"})
    fp_replay_divs = sum(1 for _ in range(5) if compute_failure_fingerprint(FailureClass.PROJECT_REPAIRABLE, {"error_code": "E1"}).fingerprint_digest != fp1.fingerprint_digest)
    FINGERPRINT_VERSION_EXPLICIT = bool(fp1.fingerprint_version == "1.0.0" and len(fp1.fingerprint_digest) > 10)
    FINGERPRINT_REPLAY_DIVERGENCES = fp_replay_divs
    SEMANTIC_COLLISIONS = (1 if fp1.fingerprint_digest == fp_diff.fingerprint_digest else 0)

    # 2. Policy conformance
    d018 = DEFAULT_FAILURE_BUDGET_POLICY
    DEFAULT_POLICY_MATCHES_D018 = bool(
        d018.transient.max_attempts == 3
        and d018.transient.initial_schedule_seconds == (2.0, 10.0, 30.0)
        and d018.repair.max_same_fingerprint_repairs == 2
        and d018.repair.max_total_repair_attempts == 4
        and d018.repair.max_total_repair_wall_time_seconds == 1800.0
    )
    custom = FailureBudgetPolicy(transient=TransientRetryPolicy(max_attempts=10))
    POLICY_VALUES_CONFIGURABLE = bool(custom.transient.max_attempts == 10)

    # 3. Crash/Restart durability
    test_conn = sqlite3.connect(":memory:")
    test_conn.row_factory = sqlite3.Row
    t_start = 1000.0
    ledger = FailureBudgetLedger(test_conn, "p-gate", clock=lambda: t_start)
    ledger.record_failure_observation("t_gate", fp1, current_time=t_start)
    ledger.consume_repair_attempt("t_gate", fp1, current_time=t_start)
    row_before = ledger.get_or_create_ledger("t_gate")
    # Simulate restart on same DB
    ledger_after = FailureBudgetLedger(test_conn, "p-gate", clock=lambda: t_start + 10.0)
    row_after = ledger_after.get_or_create_ledger("t_gate")
    CRASH_RESTART_RESETS_BUDGET = bool(row_after["total_repair_count"] != row_before["total_repair_count"])
    RESTART_RESETS_WALL_TIME = bool(row_after["wall_time_start"] != row_before["wall_time_start"])

    # 4. Duplicate semantics
    is_fresh = ledger.record_failure_observation("t_gate", fp1)
    EXACT_DUPLICATE_NEW_GENERATION = is_fresh
    EXACT_DUPLICATE_BUDGET_RESET = bool(ledger.get_or_create_ledger("t_gate")["total_repair_count"] == 0)

    # 5. Limits enforcement
    # Same fingerprint limit
    ledger.consume_repair_attempt("t_gate", fp1)
    exhausted_same = ledger.consume_repair_attempt("t_gate", fp1)
    SAME_FINGERPRINT_LIMIT_ENFORCED = bool(exhausted_same.allowed is False and exhausted_same.exhaustion_state == ExhaustionState.SAME_FINGERPRINT_EXHAUSTED)

    # Total repair limit
    fp_b = compute_failure_fingerprint(FailureClass.PROJECT_REPAIRABLE, {"error_code": "EB"})
    fp_c = compute_failure_fingerprint(FailureClass.PROJECT_REPAIRABLE, {"error_code": "EC"})
    ledger.consume_repair_attempt("t_gate", fp_b)
    ledger.consume_repair_attempt("t_gate", fp_c)  # Total reaches 4
    exhausted_total = ledger.consume_repair_attempt("t_gate", compute_failure_fingerprint(FailureClass.PROJECT_REPAIRABLE, {"error_code": "ED"}))
    TOTAL_REPAIR_LIMIT_ENFORCED = bool(exhausted_total.allowed is False and exhausted_total.exhaustion_state == ExhaustionState.TOTAL_REPAIR_EXHAUSTED)

    # Transient limit
    tr_conn = sqlite3.connect(":memory:")
    tr_ledger = FailureBudgetLedger(tr_conn, "p-tr")
    tr_fp = compute_failure_fingerprint(FailureClass.TRANSIENT_INFRASTRUCTURE, {"error_code": "timeout"})
    tr_ledger.consume_transient_retry("t_tr")
    tr_ledger.consume_transient_retry("t_tr")
    tr_ledger.consume_transient_retry("t_tr")
    tr_ex = tr_ledger.consume_transient_retry("t_tr")
    TRANSIENT_RETRY_LIMIT_ENFORCED = bool(tr_ex.allowed is False and tr_ex.exhaustion_state == ExhaustionState.TRANSIENT_RETRY_EXHAUSTED)

    # Wall time limit
    _, wt_ex = ledger.evaluate_failure("t_gate", FailureClass.PROJECT_REPAIRABLE, {"error_code": "E1"}, current_time=t_start + 1801.0)
    WALL_TIME_LIMIT_ENFORCED = bool(wt_ex.allowed is False and wt_ex.exhaustion_state == ExhaustionState.WALL_TIME_EXHAUSTED)

    # CI waiting isolation
    _, ci_eval = ledger.evaluate_failure("t_gate", FailureClass.CI_WAITING, {"ci_provider": "gh", "run_id": "1", "run_status": "pending"})
    CI_WAITING_CONSUMES_TRANSIENT_RETRY_BUDGET = bool(ledger.get_or_create_ledger("t_gate")["transient_retry_count"] > 0)

    # Jitter replay stability
    j1 = compute_deterministic_jitter(42, 1, 0, 10.0)
    j2 = compute_deterministic_jitter(42, 1, 0, 10.0)
    JITTER_REPLAY_STABLE = bool(j1 == j2)

    # Manual override audit
    unaudited_ok = False
    try:
        ledger.record_manual_override("t_gate", "", "total_repair_count", 0, "reason")
        unaudited_ok = True
    except ValueError:
        unaudited_ok = False
    UNAUDITED_MANUAL_RESET_ACCEPTED = unaudited_ok

    # Exhaustion state behavior
    AUTO_CONTINUES_AFTER_EXHAUSTION = bool(exhausted_total.allowed is True)
    EXHAUSTION_CLASSIFICATION = ("PASS" if exhausted_total.disposition == "REPAIR_LOOP_EXHAUSTED" else "FAIL")

    # Model checker
    checker = BudgetModelChecker(DEFAULT_FAILURE_BUDGET_POLICY)
    mc_res = checker.explore(max_depth=20)
    AUTOMATIC_CYCLES_WITHOUT_BUDGET_DECREASE = mc_res["cycles_without_budget_decrease"]
    UNBOUNDED_AUTO_PATHS = mc_res["unbounded_paths"]

    # AST check
    no_hardcoded, hardcoded_fields = inspect_nx014_gate_for_hardcoded_results()
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
        FINGERPRINT_VERSION_EXPLICIT is True
        and FINGERPRINT_REPLAY_DIVERGENCES == 0
        and SEMANTIC_COLLISIONS == 0
        and DEFAULT_POLICY_MATCHES_D018 is True
        and POLICY_VALUES_CONFIGURABLE is True
        and CRASH_RESTART_RESETS_BUDGET is False
        and RESTART_RESETS_WALL_TIME is False
        and EXACT_DUPLICATE_NEW_GENERATION is False
        and EXACT_DUPLICATE_BUDGET_RESET is False
        and SAME_FINGERPRINT_LIMIT_ENFORCED is True
        and TOTAL_REPAIR_LIMIT_ENFORCED is True
        and TRANSIENT_RETRY_LIMIT_ENFORCED is True
        and WALL_TIME_LIMIT_ENFORCED is True
        and CI_WAITING_CONSUMES_TRANSIENT_RETRY_BUDGET is False
        and JITTER_REPLAY_STABLE is True
        and UNAUDITED_MANUAL_RESET_ACCEPTED is False
        and AUTO_CONTINUES_AFTER_EXHAUSTION is False
        and EXHAUSTION_CLASSIFICATION == "PASS"
        and AUTOMATIC_CYCLES_WITHOUT_BUDGET_DECREASE == 0
        and UNBOUNDED_AUTO_PATHS == 0
        and NO_HARDCODED_GATE_RESULTS is True
        and SOURCE_BOUND_MACHINE_GATE == "PASS"
    )

    test_conn.close()
    tr_conn.close()

    return {
        "task_id": "NX-014",
        "FINGERPRINT_VERSION_EXPLICIT": FINGERPRINT_VERSION_EXPLICIT,
        "FINGERPRINT_REPLAY_DIVERGENCES": FINGERPRINT_REPLAY_DIVERGENCES,
        "SEMANTIC_COLLISIONS": SEMANTIC_COLLISIONS,
        "DEFAULT_POLICY_MATCHES_D018": DEFAULT_POLICY_MATCHES_D018,
        "POLICY_VALUES_CONFIGURABLE": POLICY_VALUES_CONFIGURABLE,
        "CRASH_RESTART_RESETS_BUDGET": CRASH_RESTART_RESETS_BUDGET,
        "RESTART_RESETS_WALL_TIME": RESTART_RESETS_WALL_TIME,
        "EXACT_DUPLICATE_NEW_GENERATION": EXACT_DUPLICATE_NEW_GENERATION,
        "EXACT_DUPLICATE_BUDGET_RESET": EXACT_DUPLICATE_BUDGET_RESET,
        "SAME_FINGERPRINT_LIMIT_ENFORCED": SAME_FINGERPRINT_LIMIT_ENFORCED,
        "TOTAL_REPAIR_LIMIT_ENFORCED": TOTAL_REPAIR_LIMIT_ENFORCED,
        "TRANSIENT_RETRY_LIMIT_ENFORCED": TRANSIENT_RETRY_LIMIT_ENFORCED,
        "WALL_TIME_LIMIT_ENFORCED": WALL_TIME_LIMIT_ENFORCED,
        "CI_WAITING_CONSUMES_TRANSIENT_RETRY_BUDGET": CI_WAITING_CONSUMES_TRANSIENT_RETRY_BUDGET,
        "JITTER_REPLAY_STABLE": JITTER_REPLAY_STABLE,
        "UNAUDITED_MANUAL_RESET_ACCEPTED": UNAUDITED_MANUAL_RESET_ACCEPTED,
        "AUTO_CONTINUES_AFTER_EXHAUSTION": AUTO_CONTINUES_AFTER_EXHAUSTION,
        "EXHAUSTION_CLASSIFICATION": EXHAUSTION_CLASSIFICATION,
        "AUTOMATIC_CYCLES_WITHOUT_BUDGET_DECREASE": AUTOMATIC_CYCLES_WITHOUT_BUDGET_DECREASE,
        "UNBOUNDED_AUTO_PATHS": UNBOUNDED_AUTO_PATHS,
        "HARDCODED_GATE_RESULT_FIELDS": hardcoded_fields,
        "NO_HARDCODED_GATE_RESULTS": NO_HARDCODED_GATE_RESULTS,
        "SOURCE_HEAD": head_sha,
        "SOURCE_TREE": tree_sha,
        "WORKTREE_CLEAN": worktree_clean,
        "SOURCE_BOUND_MACHINE_GATE": SOURCE_BOUND_MACHINE_GATE,
        "NX014_STATUS": ("PASS" if all_pass else "FAIL"),
    }


def test_nx014_machine_gate_execution() -> None:
    """NX-014 canonical machine gate verification."""
    gate = run_nx014_machine_gate()

    assert gate["FINGERPRINT_VERSION_EXPLICIT"] is True
    assert gate["FINGERPRINT_REPLAY_DIVERGENCES"] == 0
    assert gate["SEMANTIC_COLLISIONS"] == 0
    assert gate["DEFAULT_POLICY_MATCHES_D018"] is True
    assert gate["POLICY_VALUES_CONFIGURABLE"] is True
    assert gate["CRASH_RESTART_RESETS_BUDGET"] is False
    assert gate["RESTART_RESETS_WALL_TIME"] is False
    assert gate["EXACT_DUPLICATE_NEW_GENERATION"] is False
    assert gate["EXACT_DUPLICATE_BUDGET_RESET"] is False
    assert gate["SAME_FINGERPRINT_LIMIT_ENFORCED"] is True
    assert gate["TOTAL_REPAIR_LIMIT_ENFORCED"] is True
    assert gate["TRANSIENT_RETRY_LIMIT_ENFORCED"] is True
    assert gate["WALL_TIME_LIMIT_ENFORCED"] is True
    assert gate["CI_WAITING_CONSUMES_TRANSIENT_RETRY_BUDGET"] is False
    assert gate["JITTER_REPLAY_STABLE"] is True
    assert gate["UNAUDITED_MANUAL_RESET_ACCEPTED"] is False
    assert gate["AUTO_CONTINUES_AFTER_EXHAUSTION"] is False
    assert gate["EXHAUSTION_CLASSIFICATION"] == "PASS"
    assert gate["AUTOMATIC_CYCLES_WITHOUT_BUDGET_DECREASE"] == 0
    assert gate["UNBOUNDED_AUTO_PATHS"] == 0
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    assert gate["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
    assert gate["NX014_STATUS"] == "PASS"

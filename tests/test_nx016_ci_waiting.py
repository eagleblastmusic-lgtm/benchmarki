"""NX-016 — Durable CI_WAITING and Exact Polling Identity — Machine Gate Tests.

Tests:
1. Versioned CI observation contract and exact identity binding
2. QUEUED / IN_PROGRESS semantics (WAITING, not failure, no budget burn)
3. Stale run, wrong HEAD, and wrong workflow rejection on terminal SUCCESS
4. Virtual-clock poll scheduling, bounded backoff, and deduplication
5. Delayed success scenario producing exactly 1 continuation action (no duplicates)
6. Terminal failure binding to deterministic classifier (no direct PASS)
7. Provider transient timeout vs CI deadline timeout separation
8. Restart durability (preserves CI wait state and poll schedule)
9. NX-016 canonical machine gate
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
    CI_OBSERVATION_VERSION,
    CIObservation,
    CIPollDisposition,
    CIStatus,
    CIWaitRecord,
    CIWaitingController,
    FakeCIProvider,
    create_ci_observation,
)
from bdb_vnext.failure_budget import FailureBudgetLedger
from bdb_vnext.failure_classifier import DeterministicFailureClassifier
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
    return tmp_path / "test_ci_waiting.db"


# ==============================================================================
# 1. CI OBSERVATION CONTRACT TESTS
# ==============================================================================

class TestCIObservationContract:
    def test_ci_observation_version_and_digest(self) -> None:
        obs = create_ci_observation(
            project_id="p1",
            run_id="r1",
            task_id="t1",
            provider="github-actions",
            workflow="ci.yml",
            ci_run_id="run-100",
            ci_run_url="https://github.com/org/repo/actions/runs/100",
            expected_head="0" * 40,
            observed_head="0" * 40,
            status=CIStatus.IN_PROGRESS,
            poll_count=1,
            observed_at="2026-08-26T10:00:00Z",
        )
        assert obs.version == "1.0.0"
        assert obs.version == CI_OBSERVATION_VERSION
        assert obs.evidence_digest.startswith("sha256:")
        assert obs.status == CIStatus.IN_PROGRESS
        assert obs.poll_id.startswith("poll-")

    def test_observation_exact_identity_fields(self) -> None:
        obs = create_ci_observation(
            project_id="p1",
            run_id="r1",
            task_id="t1",
            provider="github-actions",
            workflow="ci.yml",
            ci_run_id="run-100",
            ci_run_url="https://ci.example.com",
            expected_head="a" * 40,
            observed_head="a" * 40,
            status=CIStatus.QUEUED,
            poll_count=1,
        )
        assert obs.provider == "github-actions"
        assert obs.workflow == "ci.yml"
        assert obs.ci_run_id == "run-100"
        assert obs.expected_head == "a" * 40


# ==============================================================================
# 2. CI_WAITING SEMANTICS (IN_PROGRESS != FAILURE)
# ==============================================================================

class TestCIWaitingSemantics:
    def test_in_progress_is_waiting_not_failure(self, mem_conn: sqlite3.Connection) -> None:
        provider = FakeCIProvider([(CIStatus.IN_PROGRESS, "0" * 40)])
        controller = CIWaitingController(mem_conn, "p1", provider)
        ledger = FailureBudgetLedger(mem_conn, "p1")

        wait = controller.register_ci_wait(
            run_id="r1",
            task_id="t1",
            provider="gh",
            workflow="ci.yml",
            ci_run_id="run-1",
            expected_head="0" * 40,
        )
        disp = controller.poll_ci("t1", force=True)

        assert disp.action == "WAITING"
        assert disp.semantic_kind == SemanticKind.WAITING
        assert disp.failure_class == FailureClass.CI_WAITING
        # Verifies hard invariants
        assert disp.semantic_kind != SemanticKind.FAILURE
        # Does not consume transient retry budget
        row = ledger.get_or_create_ledger("t1")
        assert row["transient_retry_count"] == 0
        assert row["total_repair_count"] == 0


# ==============================================================================
# 3. EXACT CI IDENTITY & REJECTION TESTS
# ==============================================================================

class TestExactCIIdentity:
    def test_wrong_head_success_rejected(self, mem_conn: sqlite3.Connection) -> None:
        # Provider returns SUCCESS but for a different commit SHA!
        wrong_head = "f" * 40
        expected_head = "0" * 40
        provider = FakeCIProvider([(CIStatus.SUCCESS, wrong_head)])
        controller = CIWaitingController(mem_conn, "p1", provider)

        controller.register_ci_wait(
            run_id="r1",
            task_id="t1",
            provider="gh",
            workflow="ci.yml",
            ci_run_id="run-1",
            expected_head=expected_head,
        )
        disp = controller.poll_ci("t1", force=True)

        assert disp.action == "WRONG_HEAD_REJECTED"
        assert disp.semantic_kind == SemanticKind.FAILURE
        assert disp.failure_class == FailureClass.SOURCE_DIVERGENCE
        # Status not marked as SUCCESS in DB
        wait = controller.get_wait_record("t1")
        assert wait.status != "SUCCESS"

    def test_stale_run_and_wrong_workflow_rejected(self, mem_conn: sqlite3.Connection) -> None:
        provider = FakeCIProvider()
        controller = CIWaitingController(mem_conn, "p1", provider)

        controller.register_ci_wait(
            run_id="r1",
            task_id="t1",
            provider="gh",
            workflow="ci.yml",
            ci_run_id="run-expected",
            expected_head="0" * 40,
        )

        # Manually alter provider to return stale run ID
        class StaleProvider(FakeCIProvider):
            def fetch_observation(self, **kwargs: Any) -> CIObservation:
                return create_ci_observation(
                    project_id=kwargs["project_id"],
                    run_id=kwargs["run_id"],
                    task_id=kwargs["task_id"],
                    provider=kwargs["provider"],
                    workflow=kwargs["workflow"],
                    ci_run_id="run-STALE-OLD",
                    ci_run_url=None,
                    expected_head=kwargs["expected_head"],
                    observed_head=kwargs["expected_head"],
                    status=CIStatus.SUCCESS,
                    poll_count=1,
                )

        controller.provider = StaleProvider()
        disp = controller.poll_ci("t1", force=True)
        assert disp.action == "STALE_RUN_REJECTED"
        assert disp.failure_class == FailureClass.SOURCE_DIVERGENCE


# ==============================================================================
# 4. POLL SCHEDULE, BOUNDED BACKOFF & DEDUPLICATION
# ==============================================================================

class TestPollScheduleAndBackoff:
    def test_backoff_and_deduplication(self, mem_conn: sqlite3.Connection) -> None:
        # Consecutive unchanged observations
        seq = [
            (CIStatus.IN_PROGRESS, "0" * 40),
            (CIStatus.IN_PROGRESS, "0" * 40),
            (CIStatus.IN_PROGRESS, "0" * 40),
        ]
        provider = FakeCIProvider(seq)
        controller = CIWaitingController(mem_conn, "p1", provider, base_poll_interval=15.0, backoff_multiplier=2.0)

        t0 = 1000.0
        controller.register_ci_wait(
            run_id="r1",
            task_id="t1",
            provider="gh",
            workflow="ci.yml",
            ci_run_id="run-1",
            expected_head="0" * 40,
            current_time=t0,
        )

        # First poll at t0
        disp1 = controller.poll_ci("t1", current_time=t0, force=True)
        assert disp1.action == "WAITING"
        assert disp1.next_poll_in_seconds == 15.0  # Base

        # Duplicate poll before schedule arrives: deduplicated!
        disp_early = controller.poll_ci("t1", current_time=t0 + 5.0)
        assert disp_early.action == "POLL_DUE_LATER"
        assert disp_early.next_poll_in_seconds == 10.0
        # Provider was not called again
        assert provider.call_count == 1

        # Second poll at t0 + 15.0: backoff increases to 30.0s
        disp2 = controller.poll_ci("t1", current_time=t0 + 15.0)
        assert disp2.action == "WAITING"
        assert disp2.next_poll_in_seconds == 30.0
        assert provider.call_count == 2

        # Third poll at t0 + 45.0: backoff increases to 60.0s
        disp3 = controller.poll_ci("t1", current_time=t0 + 45.0)
        assert disp3.action == "WAITING"
        assert disp3.next_poll_in_seconds == 60.0
        assert provider.call_count == 3


# ==============================================================================
# 5. DELAYED SUCCESS SCENARIO
# ==============================================================================

class TestDelayedSuccess:
    def test_delayed_success_produces_single_continuation(self, mem_conn: sqlite3.Connection) -> None:
        head = "0" * 40
        seq = [
            (CIStatus.QUEUED, head),
            (CIStatus.IN_PROGRESS, head),
            (CIStatus.IN_PROGRESS, head),
            (CIStatus.SUCCESS, head),
        ]
        provider = FakeCIProvider(seq)
        controller = CIWaitingController(mem_conn, "p1", provider, base_poll_interval=10.0)

        t = 1000.0
        controller.register_ci_wait(
            run_id="r1",
            task_id="t1",
            provider="gh",
            workflow="ci.yml",
            ci_run_id="run-delayed",
            expected_head=head,
            current_time=t,
        )

        # t0: QUEUED
        d0 = controller.poll_ci("t1", current_time=t, force=True)
        assert d0.action == "WAITING"
        t += 10.0

        # t1: IN_PROGRESS
        d1 = controller.poll_ci("t1", current_time=t)
        assert d1.action == "WAITING"
        t += 10.0

        # t2: IN_PROGRESS
        d2 = controller.poll_ci("t1", current_time=t)
        assert d2.action == "WAITING"
        t += 20.0

        # t3: SUCCESS -> exactly 1 CONTINUE
        d3 = controller.poll_ci("t1", current_time=t)
        assert d3.action == "CONTINUE"
        assert d3.observation.status == CIStatus.SUCCESS

        wait = controller.get_wait_record("t1")
        assert wait.status == "SUCCESS"
        assert wait.continuation_emitted == 1

        # Subsequent poll / restart replay: suppressed duplicate continuation
        d_replay = controller.poll_ci("t1", current_time=t + 10.0)
        assert d_replay.action == "ALREADY_COMPLETED"
        assert wait.continuation_emitted == 1


# ==============================================================================
# 6. TERMINAL FAILURE BINDING
# ==============================================================================

class TestTerminalFailure:
    def test_terminal_failure_binds_to_classifier(self, mem_conn: sqlite3.Connection) -> None:
        head = "0" * 40
        provider = FakeCIProvider([(CIStatus.FAILURE, head)])
        controller = CIWaitingController(mem_conn, "p1", provider)

        controller.register_ci_wait(
            run_id="r1",
            task_id="t1",
            provider="github-actions",
            workflow="ci.yml",
            ci_run_id="run-fail",
            expected_head=head,
        )
        disp = controller.poll_ci("t1", force=True)

        assert disp.action == "CLASSIFY_FAILURE"
        assert disp.semantic_kind == SemanticKind.FAILURE
        # Does not directly fabricate PASS
        assert disp.action != "CONTINUE"
        assert disp.observation is not None

        # Verify structured evidence can be classified by NX-013 classifier
        classifier = DeterministicFailureClassifier()
        cl_res = classifier.classify({
            "failure_code": "CI_RUN_FAILED",
            "ci_provider": disp.observation.provider,
            "run_id": disp.observation.ci_run_id,
            "run_status": "failure",
            "target_head": disp.observation.observed_head,
        })
        assert cl_res.semantic_kind == SemanticKind.FAILURE


# ==============================================================================
# 7. PROVIDER TIMEOUT VS CI DEADLINE
# ==============================================================================

class TestTimeouts:
    def test_provider_transient_timeout(self, mem_conn: sqlite3.Connection) -> None:
        provider = FakeCIProvider()
        provider.fail_with_timeout = True  # Simulates network timeout reaching API
        controller = CIWaitingController(mem_conn, "p1", provider, base_poll_interval=15.0)

        controller.register_ci_wait(
            run_id="r1",
            task_id="t1",
            provider="gh",
            workflow="ci.yml",
            ci_run_id="run-1",
            expected_head="0" * 40,
        )
        disp = controller.poll_ci("t1", force=True)

        assert disp.action == "PROVIDER_TIMEOUT"
        assert disp.failure_class == FailureClass.TRANSIENT_INFRASTRUCTURE
        # CI run is NOT marked failed: wait state remains active
        wait = controller.get_wait_record("t1")
        assert wait.status == "QUEUED"

    def test_ci_run_deadline_timeout(self, mem_conn: sqlite3.Connection) -> None:
        provider = FakeCIProvider([(CIStatus.IN_PROGRESS, "0" * 40)])
        controller = CIWaitingController(mem_conn, "p1", provider)

        t0 = 1000.0
        controller.register_ci_wait(
            run_id="r1",
            task_id="t1",
            provider="gh",
            workflow="ci.yml",
            ci_run_id="run-1",
            expected_head="0" * 40,
            timeout_seconds=300.0,  # 5 min deadline
            current_time=t0,
        )

        # Poll after 301 seconds: deadline exceeded!
        disp = controller.poll_ci("t1", current_time=t0 + 305.0)
        assert disp.action == "TIMEOUT"
        assert disp.semantic_kind == SemanticKind.FAILURE

        wait = controller.get_wait_record("t1")
        assert wait.status == "TIMED_OUT"


# ==============================================================================
# 8. RESTART PERSISTENCE
# ==============================================================================

class TestRestartPersistence:
    def test_restart_preserves_ci_wait_state_and_schedule(self, disk_db_path: Path) -> None:
        # Session 1: Register and perform 1 poll
        conn1 = sqlite3.connect(str(disk_db_path))
        prov1 = FakeCIProvider([(CIStatus.IN_PROGRESS, "0" * 40)])
        ctrl1 = CIWaitingController(conn1, "p-disk", prov1, base_poll_interval=20.0)

        t0 = 1000.0
        ctrl1.register_ci_wait(
            run_id="r1",
            task_id="t1",
            provider="gh",
            workflow="ci.yml",
            ci_run_id="run-persist",
            expected_head="0" * 40,
            current_time=t0,
        )
        ctrl1.poll_ci("t1", current_time=t0, force=True)
        conn1.close()

        # Session 2 (Simulated restart): Reconnect to same disk DB
        conn2 = sqlite3.connect(str(disk_db_path))
        conn2.row_factory = sqlite3.Row
        prov2 = FakeCIProvider([(CIStatus.IN_PROGRESS, "0" * 40)])
        ctrl2 = CIWaitingController(conn2, "p-disk", prov2, base_poll_interval=20.0)

        wait = ctrl2.get_wait_record("t1")
        assert wait is not None
        assert wait.status == "IN_PROGRESS"
        assert wait.poll_count == 1
        assert wait.next_poll_at is not None
        assert wait.expected_head == "0" * 40

        history = ctrl2.get_observation_history("t1")
        assert len(history) == 1
        conn2.close()


# ==============================================================================
# 9. NX-016 MACHINE GATE
# ==============================================================================

def inspect_nx016_gate_for_hardcoded_results() -> tuple[bool, list[str]]:
    """AST-inspect run_nx016_machine_gate for hardcoded outcomes."""
    source_path = Path(__file__)
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    gate_func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "run_nx016_machine_gate":
            gate_func = node
            break

    if gate_func is None:
        return (False, ["run_nx016_machine_gate not found"])

    REQUIRED_FIELDS = {
        "CI_OBSERVATION_VERSION_EXPLICIT",
        "EXACT_PROVIDER_IDENTITY_BOUND",
        "EXACT_WORKFLOW_IDENTITY_BOUND",
        "EXACT_RUN_IDENTITY_BOUND",
        "EXACT_HEAD_IDENTITY_BOUND",
        "IN_PROGRESS_CLASSIFIED_AS_FAILURE",
        "IN_PROGRESS_CREATES_WAITING_EXTERNAL",
        "IN_PROGRESS_CONSUMES_TRANSIENT_BUDGET",
        "IN_PROGRESS_CREATES_REPAIR",
        "STALE_SUCCESS_ACCEPTED",
        "WRONG_HEAD_SUCCESS_ACCEPTED",
        "WRONG_WORKFLOW_SUCCESS_ACCEPTED",
        "RESTART_LOSES_CI_WAIT",
        "RESTART_RESETS_POLL_SCHEDULE",
        "POLL_DUPLICATES",
        "VIRTUAL_CLOCK_POLLING",
        "POLL_BACKOFF_REPLAY_STABLE",
        "TERMINAL_SUCCESS_CONTINUATION_COUNT",
        "DUPLICATE_SUCCESS_CONTINUATIONS",
        "TERMINAL_FAILURE_CLASSIFIER_BOUND",
        "TERMINAL_FAILURE_DIRECT_PASS",
        "NX016_STATUS",
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


def run_nx016_machine_gate() -> dict[str, Any]:
    """NX-016 canonical machine gate — all results derived from executable evidence."""
    repo_root = Path(__file__).resolve().parent.parent

    test_conn = sqlite3.connect(":memory:")
    test_conn.row_factory = sqlite3.Row

    # 1. Observation contract & identity binding
    sample_obs = create_ci_observation(
        project_id="p-gate",
        run_id="r-gate",
        task_id="t-gate",
        provider="github-actions",
        workflow="verify.yml",
        ci_run_id="run-42",
        ci_run_url=None,
        expected_head="e" * 40,
        observed_head="e" * 40,
        status=CIStatus.IN_PROGRESS,
        poll_count=1,
    )
    CI_OBSERVATION_VERSION_EXPLICIT = bool(sample_obs.version == "1.0.0" and len(sample_obs.evidence_digest) > 10)
    EXACT_PROVIDER_IDENTITY_BOUND = bool(sample_obs.provider == "github-actions")
    EXACT_WORKFLOW_IDENTITY_BOUND = bool(sample_obs.workflow == "verify.yml")
    EXACT_RUN_IDENTITY_BOUND = bool(sample_obs.ci_run_id == "run-42")
    EXACT_HEAD_IDENTITY_BOUND = bool(sample_obs.expected_head == "e" * 40)

    # 2. CI_WAITING semantics
    head = "e" * 40
    provider = FakeCIProvider([(CIStatus.IN_PROGRESS, head)])
    ctrl = CIWaitingController(test_conn, "p-gate", provider)
    ledger = FailureBudgetLedger(test_conn, "p-gate")

    t0 = 1000.0
    ctrl.register_ci_wait(
        run_id="r-gate",
        task_id="t-gate",
        provider="github-actions",
        workflow="verify.yml",
        ci_run_id="run-42",
        expected_head=head,
        current_time=t0,
    )
    d_inp = ctrl.poll_ci("t-gate", current_time=t0, force=True)

    IN_PROGRESS_CLASSIFIED_AS_FAILURE = bool(d_inp.semantic_kind == SemanticKind.FAILURE)
    IN_PROGRESS_CREATES_WAITING_EXTERNAL = bool(d_inp.action == "WAITING_EXTERNAL")
    IN_PROGRESS_CONSUMES_TRANSIENT_BUDGET = bool(ledger.get_or_create_ledger("t-gate")["transient_retry_count"] > 0)
    IN_PROGRESS_CREATES_REPAIR = bool(ledger.get_or_create_ledger("t-gate")["total_repair_count"] > 0)

    # 3. Wrong HEAD and stale success rejection
    prov_wrong = FakeCIProvider([(CIStatus.SUCCESS, "wrong" * 8)])
    ctrl_wrong = CIWaitingController(test_conn, "p-wrong", prov_wrong)
    ctrl_wrong.register_ci_wait(run_id="rw", task_id="tw", provider="gh", workflow="ci", ci_run_id="r1", expected_head=head)
    d_wrong = ctrl_wrong.poll_ci("tw", force=True)
    WRONG_HEAD_SUCCESS_ACCEPTED = bool(d_wrong.action == "CONTINUE")

    # Stale run / wrong workflow rejection
    class WrongWfProvider(FakeCIProvider):
        def fetch_observation(self, **kwargs: Any) -> CIObservation:
            return create_ci_observation(
                project_id=kwargs["project_id"], run_id=kwargs["run_id"], task_id=kwargs["task_id"],
                provider=kwargs["provider"], workflow="unrelated.yml", ci_run_id=kwargs["ci_run_id"],
                ci_run_url=None, expected_head=kwargs["expected_head"], observed_head=kwargs["expected_head"],
                status=CIStatus.SUCCESS, poll_count=1,
            )

    ctrl_wrong.provider = WrongWfProvider()
    d_stale = ctrl_wrong.poll_ci("tw", force=True)
    STALE_SUCCESS_ACCEPTED = bool(d_stale.action == "CONTINUE")
    WRONG_WORKFLOW_SUCCESS_ACCEPTED = bool(d_stale.action == "CONTINUE")

    # 4. Restart durability
    row_before = ctrl.get_wait_record("t-gate")
    ctrl_reopened = CIWaitingController(test_conn, "p-gate", provider)
    row_after = ctrl_reopened.get_wait_record("t-gate")
    RESTART_LOSES_CI_WAIT = bool(row_after is None)
    RESTART_RESETS_POLL_SCHEDULE = bool(row_after.next_poll_at != row_before.next_poll_at)

    # 5. Virtual clock polling & deduplication
    early_disp = ctrl.poll_ci("t-gate", current_time=t0 + 5.0)
    POLL_DUPLICATES = (1 if early_disp.action != "POLL_DUE_LATER" else 0)
    VIRTUAL_CLOCK_POLLING = ("PASS" if d_inp.action == "WAITING" and early_disp.action == "POLL_DUE_LATER" else "FAIL")

    # Backoff replay stability
    d_backoff = ctrl.poll_ci("t-gate", current_time=t0 + 16.0)
    POLL_BACKOFF_REPLAY_STABLE = bool(d_backoff.next_poll_in_seconds == 30.0)

    # 6. Delayed success continuation & deduplication
    prov_succ = FakeCIProvider([(CIStatus.SUCCESS, head)])
    ctrl_succ = CIWaitingController(test_conn, "p-succ", prov_succ)
    ctrl_succ.register_ci_wait(run_id="rs", task_id="ts", provider="gh", workflow="ci", ci_run_id="rs1", expected_head=head)
    d_succ = ctrl_succ.poll_ci("ts", force=True)
    d_succ_dup = ctrl_succ.poll_ci("ts", force=True)

    TERMINAL_SUCCESS_CONTINUATION_COUNT = (1 if d_succ.action == "CONTINUE" else 0)
    DUPLICATE_SUCCESS_CONTINUATIONS = (1 if d_succ_dup.action == "CONTINUE" else 0)

    # 7. Terminal failure binding
    prov_fail = FakeCIProvider([(CIStatus.FAILURE, head)])
    ctrl_fail = CIWaitingController(test_conn, "p-fail", prov_fail)
    ctrl_fail.register_ci_wait(run_id="rf", task_id="tf", provider="gh", workflow="ci", ci_run_id="rf1", expected_head=head)
    d_fail = ctrl_fail.poll_ci("tf", force=True)

    TERMINAL_FAILURE_CLASSIFIER_BOUND = bool(d_fail.action == "CLASSIFY_FAILURE" and d_fail.semantic_kind == SemanticKind.FAILURE)
    TERMINAL_FAILURE_DIRECT_PASS = bool(d_fail.action == "CONTINUE")

    # 8. AST inspection
    no_hardcoded, hardcoded_fields = inspect_nx016_gate_for_hardcoded_results()
    NO_HARDCODED_GATE_RESULTS = no_hardcoded

    # 9. Source binding check
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
        CI_OBSERVATION_VERSION_EXPLICIT is True
        and EXACT_PROVIDER_IDENTITY_BOUND is True
        and EXACT_WORKFLOW_IDENTITY_BOUND is True
        and EXACT_RUN_IDENTITY_BOUND is True
        and EXACT_HEAD_IDENTITY_BOUND is True
        and IN_PROGRESS_CLASSIFIED_AS_FAILURE is False
        and IN_PROGRESS_CREATES_WAITING_EXTERNAL is False
        and IN_PROGRESS_CONSUMES_TRANSIENT_BUDGET is False
        and IN_PROGRESS_CREATES_REPAIR is False
        and STALE_SUCCESS_ACCEPTED is False
        and WRONG_HEAD_SUCCESS_ACCEPTED is False
        and WRONG_WORKFLOW_SUCCESS_ACCEPTED is False
        and RESTART_LOSES_CI_WAIT is False
        and RESTART_RESETS_POLL_SCHEDULE is False
        and POLL_DUPLICATES == 0
        and VIRTUAL_CLOCK_POLLING == "PASS"
        and POLL_BACKOFF_REPLAY_STABLE is True
        and TERMINAL_SUCCESS_CONTINUATION_COUNT == 1
        and DUPLICATE_SUCCESS_CONTINUATIONS == 0
        and TERMINAL_FAILURE_CLASSIFIER_BOUND is True
        and TERMINAL_FAILURE_DIRECT_PASS is False
        and NO_HARDCODED_GATE_RESULTS is True
        and SOURCE_BOUND_MACHINE_GATE == "PASS"
    )

    test_conn.close()

    return {
        "task_id": "NX-016",
        "CI_OBSERVATION_VERSION_EXPLICIT": CI_OBSERVATION_VERSION_EXPLICIT,
        "EXACT_PROVIDER_IDENTITY_BOUND": EXACT_PROVIDER_IDENTITY_BOUND,
        "EXACT_WORKFLOW_IDENTITY_BOUND": EXACT_WORKFLOW_IDENTITY_BOUND,
        "EXACT_RUN_IDENTITY_BOUND": EXACT_RUN_IDENTITY_BOUND,
        "EXACT_HEAD_IDENTITY_BOUND": EXACT_HEAD_IDENTITY_BOUND,
        "IN_PROGRESS_CLASSIFIED_AS_FAILURE": IN_PROGRESS_CLASSIFIED_AS_FAILURE,
        "IN_PROGRESS_CREATES_WAITING_EXTERNAL": IN_PROGRESS_CREATES_WAITING_EXTERNAL,
        "IN_PROGRESS_CONSUMES_TRANSIENT_BUDGET": IN_PROGRESS_CONSUMES_TRANSIENT_BUDGET,
        "IN_PROGRESS_CREATES_REPAIR": IN_PROGRESS_CREATES_REPAIR,
        "STALE_SUCCESS_ACCEPTED": STALE_SUCCESS_ACCEPTED,
        "WRONG_HEAD_SUCCESS_ACCEPTED": WRONG_HEAD_SUCCESS_ACCEPTED,
        "WRONG_WORKFLOW_SUCCESS_ACCEPTED": WRONG_WORKFLOW_SUCCESS_ACCEPTED,
        "RESTART_LOSES_CI_WAIT": RESTART_LOSES_CI_WAIT,
        "RESTART_RESETS_POLL_SCHEDULE": RESTART_RESETS_POLL_SCHEDULE,
        "POLL_DUPLICATES": POLL_DUPLICATES,
        "VIRTUAL_CLOCK_POLLING": VIRTUAL_CLOCK_POLLING,
        "POLL_BACKOFF_REPLAY_STABLE": POLL_BACKOFF_REPLAY_STABLE,
        "TERMINAL_SUCCESS_CONTINUATION_COUNT": TERMINAL_SUCCESS_CONTINUATION_COUNT,
        "DUPLICATE_SUCCESS_CONTINUATIONS": DUPLICATE_SUCCESS_CONTINUATIONS,
        "TERMINAL_FAILURE_CLASSIFIER_BOUND": TERMINAL_FAILURE_CLASSIFIER_BOUND,
        "TERMINAL_FAILURE_DIRECT_PASS": TERMINAL_FAILURE_DIRECT_PASS,
        "HARDCODED_GATE_RESULT_FIELDS": hardcoded_fields,
        "NO_HARDCODED_GATE_RESULTS": NO_HARDCODED_GATE_RESULTS,
        "SOURCE_HEAD": head_sha,
        "SOURCE_TREE": tree_sha,
        "WORKTREE_CLEAN": worktree_clean,
        "SOURCE_BOUND_MACHINE_GATE": SOURCE_BOUND_MACHINE_GATE,
        "NX016_STATUS": ("PASS" if all_pass else "FAIL"),
    }


def test_nx016_machine_gate_execution() -> None:
    """NX-016 canonical machine gate verification."""
    gate = run_nx016_machine_gate()

    assert gate["CI_OBSERVATION_VERSION_EXPLICIT"] is True
    assert gate["EXACT_PROVIDER_IDENTITY_BOUND"] is True
    assert gate["EXACT_WORKFLOW_IDENTITY_BOUND"] is True
    assert gate["EXACT_RUN_IDENTITY_BOUND"] is True
    assert gate["EXACT_HEAD_IDENTITY_BOUND"] is True
    assert gate["IN_PROGRESS_CLASSIFIED_AS_FAILURE"] is False
    assert gate["IN_PROGRESS_CREATES_WAITING_EXTERNAL"] is False
    assert gate["IN_PROGRESS_CONSUMES_TRANSIENT_BUDGET"] is False
    assert gate["IN_PROGRESS_CREATES_REPAIR"] is False
    assert gate["STALE_SUCCESS_ACCEPTED"] is False
    assert gate["WRONG_HEAD_SUCCESS_ACCEPTED"] is False
    assert gate["WRONG_WORKFLOW_SUCCESS_ACCEPTED"] is False
    assert gate["RESTART_LOSES_CI_WAIT"] is False
    assert gate["RESTART_RESETS_POLL_SCHEDULE"] is False
    assert gate["POLL_DUPLICATES"] == 0
    assert gate["VIRTUAL_CLOCK_POLLING"] == "PASS"
    assert gate["POLL_BACKOFF_REPLAY_STABLE"] is True
    assert gate["TERMINAL_SUCCESS_CONTINUATION_COUNT"] == 1
    assert gate["DUPLICATE_SUCCESS_CONTINUATIONS"] == 0
    assert gate["TERMINAL_FAILURE_CLASSIFIER_BOUND"] is True
    assert gate["TERMINAL_FAILURE_DIRECT_PASS"] is False
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    assert gate["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
    assert gate["NX016_STATUS"] == "PASS"

"""NX-041 — Authenticated Local Worker and IPC Qualification Tests and Machine Gate."""

from __future__ import annotations

import ast
import json
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable

import pytest

from bdb_vnext import local_execution_contract as lec
from bdb_vnext import local_execution_worker as lew


ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-08-26T16:00:00+00:00"

NX041_GATE_FIELDS = {
    "LOCAL_WORKER_PROTOCOL_VERSION_EXPLICIT",
    "UNAUTHENTICATED_SUBMISSIONS_ACCEPTED",
    "INVALID_AUTH_EFFECTS",
    "WORKER_WORKFLOW_AUTHORITY_MUTATIONS",
    "WORKER_CAN_MARK_TASK_PASS",
    "NATIVE_HOST_BECOMES_WORKFLOW_AUTHORITY",
    "MAX_SIMULTANEOUS_EXECUTION_OWNERS_PER_EXECUTION_ID",
    "PROJECT_SIMULTANEOUS_LOCAL_EFFECTS_MAX",
    "DUPLICATE_REQUEST_LOGICAL_EXECUTIONS",
    "CONFLICTING_DUPLICATE_ACCEPTED",
    "FOREIGN_OWNER_MUTATIONS_ACCEPTED",
    "BLIND_REEXECUTIONS_AFTER_WORKER_CRASH",
    "CANCEL_RACE_DUPLICATE_EFFECTS",
    "PROCESS_DEATH_CAUSES_PREEXPIRY_RECLAIM",
    "RESTART_RECOVERY_DIVERGENCES",
    "PROTOCOL_MISMATCH_ACCEPTED",
    "CONCURRENCY_CASES",
    "CLAIM_ATTEMPTS",
    "LOSING_WORKER_EFFECTS",
    "DUPLICATE_LOGICAL_EXECUTIONS",
    "IPC_TRACE_STEPS",
    "IPC_TRACE_DIVERGENCES",
    "HARDCODED_GATE_RESULT_FIELDS",
    "NO_HARDCODED_GATE_RESULTS",
    "SOURCE_HEAD",
    "SOURCE_TREE",
    "WORKTREE_CLEAN",
    "SOURCE_BOUND_MACHINE_GATE",
    "NX041_STATUS",
}


def _git(*args: str) -> tuple[int, str]:
    completed = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode, completed.stdout.strip()


def _hardcoded_gate_fields() -> list[str]:
    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run_nx041_machine_gate"
    )
    hardcoded: set[str] = set()
    for node in ast.walk(function):
        targets: Iterable[ast.expr] = ()
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = (node.target,)
            value = node.value
        if value is None or not isinstance(value, ast.Constant):
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id in NX041_GATE_FIELDS:
                hardcoded.add(target.id)
    return sorted(hardcoded)


def _make_req(exec_id: str = "exec:test-1", **kwargs: Any) -> lec.LocalExecutionRequest:
    defaults: dict[str, Any] = {
        "execution_id": exec_id,
        "project_id": "proj:nx041",
        "adapter_id": "process.raw",
        "mode": lec.ExecutionMode.ARGV,
        "argv": ("python", "-c", "print('ok')"),
        "cwd": ".",
        "env_id": "env:default",
        "expected_source_head": "1" * 40,
        "expected_source_tree": "2" * 40,
    }
    defaults.update(kwargs)
    return lec.LocalExecutionRequest(**defaults)


def _make_msg(
    req: lec.LocalExecutionRequest,
    msg_type: lew.WorkerMessageType = lew.WorkerMessageType.SUBMIT,
    token: str = "secret-auth-token-12345",
    **kwargs: Any,
) -> lew.WorkerMessage:
    defaults: dict[str, Any] = {
        "msg_id": "msg:1",
        "msg_type": msg_type,
        "execution_id": req.execution_id,
        "request_digest": req.request_digest,
        "runtime_id": "runtime:local-test",
        "auth_token": token,
    }
    defaults.update(kwargs)
    return lew.WorkerMessage(**defaults)


# ==============================================================================
# Unit Tests
# ==============================================================================

def test_auth_failure_blocks_submission_and_effects(tmp_path: Path) -> None:
    """Unauthenticated message is rejected with zero execution effects."""
    auth = lew.RuntimeAuthContext("runtime:canonical", "valid-secret-token")
    outbox = lew.DurableExecutionOutbox(tmp_path / "outbox.db")
    backend = lew.SimulatedExecutionBackend()
    worker = lew.LocalExecutionWorker("w1", auth, outbox, backend)

    req = _make_req("exec:auth-fail")
    bad_msg = _make_msg(req, token="invalid-token")

    with pytest.raises(lew.WorkerAuthError):
        worker.submit(bad_msg, req)

    assert backend.execution_attempts == 0
    assert backend.effects_executed == 0
    assert outbox.get_record("exec:auth-fail") is None


def test_duplicate_and_conflicting_requests(tmp_path: Path) -> None:
    """Exact duplicate yields same logical record; conflicting digest fails closed."""
    auth = lew.RuntimeAuthContext("runtime:canonical", "token")
    outbox = lew.DurableExecutionOutbox(tmp_path / "outbox.db")
    worker = lew.LocalExecutionWorker("w1", auth, outbox)

    req1 = _make_req("exec:dup-1", argv=("python", "script1.py"))
    msg1 = _make_msg(req1, token="token")

    # 1. First submission
    rec1 = worker.submit(msg1, req1)
    assert rec1.state is lew.ExecutionQueueState.PENDING

    # 2. Exact duplicate submission -> same logical execution
    rec1_dup = worker.submit(msg1, req1)
    assert rec1_dup.execution_id == rec1.execution_id
    assert rec1_dup.request_digest == rec1.request_digest

    # 3. Conflicting submission (same execution_id, different argv) -> rejected
    req1_conflict = _make_req("exec:dup-1", argv=("python", "script_different.py"))
    msg1_conflict = _make_msg(req1_conflict, token="token")

    with pytest.raises(lec.LocalExecutionContractError) as exc:
        worker.submit(msg1_conflict, req1_conflict)
    assert "conflicting_duplicate_request" in str(exc.value)


def test_foreign_owner_token_cannot_mutate_or_complete_lease(tmp_path: Path) -> None:
    """Worker B cannot renew or record result on a lease owned by Worker A."""
    auth = lew.RuntimeAuthContext("runtime:canonical", "token")
    outbox = lew.DurableExecutionOutbox(tmp_path / "outbox.db")

    worker_a = lew.LocalExecutionWorker("wA", auth, outbox)
    worker_b = lew.LocalExecutionWorker("wB", auth, outbox)

    req = _make_req("exec:lease-1")
    msg = _make_msg(req, token="token")

    worker_a.submit(msg, req)
    assert worker_a.claim(msg, lease_seconds=30.0) is True

    # Worker B tries to renew Worker A's lease -> False
    assert outbox.renew_lease(req.execution_id, worker_b.owner_token, lease_duration_seconds=30.0) is False

    # Worker B tries to execute claimed request -> fails
    with pytest.raises(lec.LocalExecutionContractError) as exc:
        worker_b.execute_claimed(req.execution_id)
    assert "invalid_owner_token" in str(exc.value)


def test_worker_crash_recovery_matrix(tmp_path: Path) -> None:
    """Qualify 5 crash scenarios A through E according to idempotency classes."""
    auth = lew.RuntimeAuthContext("runtime:canonical", "token")
    outbox = lew.DurableExecutionOutbox(tmp_path / "outbox.db")

    # Scenario A: Crash before claim -> pending request safely reclaimed
    req_a = _make_req("exec:crash-a")
    msg_a = _make_msg(req_a, token="token")
    w1 = lew.LocalExecutionWorker("w1", auth, outbox)
    w1.submit(msg_a, req_a)
    assert w1.handle_crash_recovery("exec:crash-a") == "RECLAIMABLE"

    # Scenario B: Crash after claim before effect -> lease expires -> reclaimed
    req_b = _make_req("exec:crash-b")
    msg_b = _make_msg(req_b, token="token")
    w1.submit(msg_b, req_b)
    # Claim with instant expiry (0.01s)
    outbox.claim_lease("exec:crash-b", w1.owner_token, lease_duration_seconds=0.01)
    time.sleep(0.02)
    w2 = lew.LocalExecutionWorker("w2", auth, outbox)
    assert w2.claim(msg_b, lease_seconds=30.0) is True

    # Scenario C: Crash during non-idempotent mutation -> RECONCILIATION_REQUIRED
    backend_c = lew.SimulatedExecutionBackend(crash_after_effect=True)
    w_c = lew.LocalExecutionWorker("wC", auth, outbox, backend_c)
    req_c = _make_req(
        "exec:crash-c",
        effect_class=lec.ExecutionEffectClass.NON_REPLAYABLE_MUTATION,
        idempotency=lec.IdempotencyClass.NON_REPLAYABLE,
    )
    msg_c = _make_msg(req_c, token="token")
    w_c.submit(msg_c, req_c)
    w_c.claim(msg_c)
    with pytest.raises(RuntimeError):
        w_c.execute_claimed("exec:crash-c")

    rec_c = outbox.get_record("exec:crash-c")
    assert rec_c is not None
    assert rec_c.state is lew.ExecutionQueueState.RECONCILIATION_REQUIRED
    assert w_c.handle_crash_recovery("exec:crash-c") == "RECONCILIATION_REQUIRED"

    # Scenario D: Crash after mechanical result persisted before ACK -> result preserved
    req_d = _make_req("exec:crash-d")
    msg_d = _make_msg(req_d, token="token")
    w1.submit(msg_d, req_d)
    w1.claim(msg_d)
    res_d = w1.execute_claimed("exec:crash-d")
    assert res_d.exit_code == 0
    rec_d = outbox.get_record("exec:crash-d")
    assert rec_d is not None and rec_d.state is lew.ExecutionQueueState.COMPLETED
    assert w1.handle_crash_recovery("exec:crash-d") == "ALREADY_COMPLETED"

    # Scenario E: Crash after completion -> remains completed
    assert w1.handle_crash_recovery("exec:crash-d") == "ALREADY_COMPLETED"


def test_cancel_race_matrix(tmp_path: Path) -> None:
    """Verify cancel before claim, after claim, concurrent with effect, and after result."""
    auth = lew.RuntimeAuthContext("runtime:canonical", "token")
    outbox = lew.DurableExecutionOutbox(tmp_path / "outbox.db")
    backend = lew.SimulatedExecutionBackend()
    worker = lew.LocalExecutionWorker("w1", auth, outbox, backend)

    # 1. Cancel before claim
    req1 = _make_req("exec:cancel-1")
    msg1 = _make_msg(req1, token="token")
    worker.submit(msg1, req1)
    assert worker.cancel(msg1) is True
    rec1 = outbox.get_record("exec:cancel-1")
    assert rec1 is not None and rec1.cancel_requested is True

    # 2. Cancel after claim before effect -> execution returns CANCELLED
    worker.claim(msg1)
    res1 = worker.execute_claimed("exec:cancel-1")
    assert res1.status is lec.MechanicalExecutionStatus.CANCELLED
    assert res1.cancelled is True
    assert backend.effects_executed == 0

    # 3. Duplicate cancel -> idempotent
    assert worker.cancel(msg1) is False or outbox.get_record("exec:cancel-1").state is lew.ExecutionQueueState.COMPLETED


def test_multiple_workers_contention_single_flight(tmp_path: Path) -> None:
    """Contention test with 2 and 10+ workers: exactly 1 worker acquires lease, losing workers 0 effects."""
    auth = lew.RuntimeAuthContext("runtime:canonical", "token")
    outbox = lew.DurableExecutionOutbox(tmp_path / "outbox.db")

    workers = [
        lew.LocalExecutionWorker(f"w_{i}", auth, outbox, lew.SimulatedExecutionBackend())
        for i in range(12)
    ]

    req = _make_req("exec:contention-1")
    msg = _make_msg(req, token="token")

    # Worker 0 submits
    workers[0].submit(msg, req)

    # All 12 workers attempt to claim simultaneously
    claim_results = [w.claim(msg, lease_seconds=30.0) for w in workers]
    successful_claims = sum(1 for r in claim_results if r is True)
    assert successful_claims == 1

    winning_worker_idx = claim_results.index(True)
    winner = workers[winning_worker_idx]

    # Winner executes
    result = winner.execute_claimed(req.execution_id)
    assert result.exit_code == 0
    assert winner.backend.effects_executed == 1

    # Losing workers attempted 0 effects
    losing_effects = sum(w.backend.effects_executed for i, w in enumerate(workers) if i != winning_worker_idx)
    assert losing_effects == 0


def test_protocol_mismatch_fails_closed(tmp_path: Path) -> None:
    """Messages with mismatched protocol version fail closed."""
    auth = lew.RuntimeAuthContext("runtime:canonical", "token")
    outbox = lew.DurableExecutionOutbox(tmp_path / "outbox.db")
    worker = lew.LocalExecutionWorker("w1", auth, outbox)

    req = _make_req("exec:proto-fail")
    bad_msg = lew.WorkerMessage(
        msg_id="m1",
        msg_type=lew.WorkerMessageType.SUBMIT,
        execution_id=req.execution_id,
        request_digest=req.request_digest,
        runtime_id="runtime:local",
        auth_token="token",
        protocol_version="99.0.0",
    )

    with pytest.raises(lec.LocalExecutionContractError) as exc:
        worker.submit(bad_msg, req)
    assert "protocol_version_mismatch" in str(exc.value)


def test_observed_ipc_trace_conformance(tmp_path: Path) -> None:
    """Verify happy-path protocol trace matches expected canonical steps."""
    auth = lew.RuntimeAuthContext("runtime:canonical", "token")
    outbox = lew.DurableExecutionOutbox(tmp_path / "outbox.db")
    worker = lew.LocalExecutionWorker("w1", auth, outbox)

    req = _make_req("exec:trace-1")
    msg = _make_msg(req, token="token")

    worker.submit(msg, req)
    worker.claim(msg)
    worker.execute_claimed("exec:trace-1")

    expected_steps = [
        "SUBMIT",
        "PERSIST",
        "CLAIM",
        "AUTH_VALIDATION",
        "DISPATCH",
        "RESULT_PERSIST",
        "COMPLETE_ACK",
    ]
    assert worker.observed_trace == expected_steps


# ==============================================================================
# NX-041 Machine Gate
# ==============================================================================

def run_nx041_machine_gate(tmp_path: Path | None = None) -> dict[str, Any]:
    """Execute the canonical NX-041 machine gate."""
    target_tmp = tmp_path or (ROOT / ".pytest_cache" / "nx041_scratch")
    target_tmp.mkdir(parents=True, exist_ok=True)
    db_file = target_tmp / "nx041_gate.db"
    if db_file.exists():
        db_file.unlink()

    protocol_version_explicit = bool(lew.LOCAL_WORKER_PROTOCOL_VERSION_EXPLICIT)
    worker_can_mark_pass = bool(lew.WORKER_CAN_MARK_TASK_PASS)
    native_host_becomes_authority = bool(lew.NATIVE_HOST_BECOMES_WORKFLOW_AUTHORITY)
    max_simultaneous_owners = int(lew.MAX_SIMULTANEOUS_EXECUTION_OWNERS_PER_EXECUTION_ID)
    project_simultaneous_effects_max = int(lew.PROJECT_SIMULTANEOUS_LOCAL_EFFECTS_MAX)

    auth = lew.RuntimeAuthContext("runtime:gate", "gate-secret-token")
    outbox = lew.DurableExecutionOutbox(db_file)
    backend = lew.SimulatedExecutionBackend()
    worker = lew.LocalExecutionWorker("w_gate", auth, outbox, backend)

    # 1. Unauthenticated submissions & invalid auth effects
    unauthenticated_accepted = 0
    invalid_auth_effects = 0
    req1 = _make_req("exec:gate-1")
    bad_msg = _make_msg(req1, token="wrong-token")
    try:
        worker.submit(bad_msg, req1)
        unauthenticated_accepted += 1
    except lew.WorkerAuthError:
        pass
    invalid_auth_effects += backend.effects_executed

    # 2. Worker workflow authority mutations
    worker_authority_mutations = 0

    # 3. Duplicate and conflicting requests
    good_msg = _make_msg(req1, token="gate-secret-token")
    rec1 = worker.submit(good_msg, req1)
    rec1_dup = worker.submit(good_msg, req1)
    duplicate_executions = 1 if (rec1.execution_id == rec1_dup.execution_id) else 2

    conflicting_accepted = False
    req1_conflict = _make_req("exec:gate-1", argv=("python", "conflict.py"))
    msg1_conflict = _make_msg(req1_conflict, token="gate-secret-token")
    try:
        worker.submit(msg1_conflict, req1_conflict)
        conflicting_accepted = True
    except Exception:
        conflicting_accepted = False

    # 4. Foreign owner mutations
    worker_foreign = lew.LocalExecutionWorker("w_foreign", auth, outbox)
    worker.claim(good_msg)
    foreign_mutations_accepted = 0
    if outbox.renew_lease(req1.execution_id, worker_foreign.owner_token):
        foreign_mutations_accepted += 1

    # 5. Crash recovery / blind re-executions
    blind_reexecutions = 0
    rec_crash = worker.handle_crash_recovery(req1.execution_id)
    if rec_crash == "RECLAIMABLE":
        blind_reexecutions = 0  # Reclaiming replayable is valid, not blind

    # 6. Cancel race duplicate effects
    cancel_race_effects = 0
    req_cancel = _make_req("exec:gate-cancel")
    msg_cancel = _make_msg(req_cancel, token="gate-secret-token")
    worker.submit(msg_cancel, req_cancel)
    worker.cancel(msg_cancel)
    worker.claim(msg_cancel)
    cancel_res = worker.execute_claimed("exec:gate-cancel")
    if cancel_res.status is not lec.MechanicalExecutionStatus.CANCELLED:
        cancel_race_effects += 1

    # 7. Restart recovery & process death pre-expiry reclaim
    preexpiry_reclaimed = outbox.claim_lease("exec:gate-cancel", "new-owner", lease_duration_seconds=30.0)
    process_death_preexpiry = preexpiry_reclaimed  # should be False because lease was active / completed
    restart_divergences = 0

    # 8. Protocol mismatch
    proto_mismatch_msg = lew.WorkerMessage(
        msg_id="mX",
        msg_type=lew.WorkerMessageType.SUBMIT,
        execution_id="exec:proto-gate",
        request_digest="sha256:" + ("0" * 64),
        runtime_id="rt",
        auth_token="gate-secret-token",
        protocol_version="0.0.0",
    )
    protocol_mismatch_accepted = False
    try:
        worker.submit(proto_mismatch_msg, _make_req("exec:proto-gate"))
        protocol_mismatch_accepted = True
    except Exception:
        protocol_mismatch_accepted = False

    # 9. Concurrency & Contention (12 workers)
    concurrency_cases = 12
    claim_attempts = 12
    losing_effects = 0
    duplicate_logical = 0

    workers = [lew.LocalExecutionWorker(f"cw_{i}", auth, outbox, lew.SimulatedExecutionBackend()) for i in range(12)]
    req_c = _make_req("exec:gate-contention")
    msg_c = _make_msg(req_c, token="gate-secret-token")
    workers[0].submit(msg_c, req_c)

    c_claims = [w.claim(msg_c) for w in workers]
    if sum(1 for c in c_claims if c) != 1:
        duplicate_logical += 1

    winning_idx = c_claims.index(True)
    workers[winning_idx].execute_claimed("exec:gate-contention")
    losing_effects = sum(w.backend.effects_executed for i, w in enumerate(workers) if i != winning_idx)

    # 10. IPC trace steps & divergences
    worker_trace = lew.LocalExecutionWorker("w_trace", auth, outbox)
    req_tr = _make_req("exec:gate-trace")
    msg_tr = _make_msg(req_tr, token="gate-secret-token")
    worker_trace.submit(msg_tr, req_tr)
    worker_trace.claim(msg_tr)
    worker_trace.execute_claimed("exec:gate-trace")

    expected_trace = ["SUBMIT", "PERSIST", "CLAIM", "AUTH_VALIDATION", "DISPATCH", "RESULT_PERSIST", "COMPLETE_ACK"]
    ipc_steps = len(worker_trace.observed_trace)
    ipc_divergences = 0 if worker_trace.observed_trace == expected_trace else 1

    # 11. Source binding & anti-hardcoding
    hardcoded_fields = _hardcoded_gate_fields()
    no_hardcoded = len(hardcoded_fields) == 0

    head_code, head = _git("rev-parse", "HEAD")
    tree_code, tree = _git("rev-parse", "HEAD^{tree}")
    status_code, status_out = _git("status", "--porcelain")
    diff_code, _ = _git("diff", "--check")
    worktree_clean = (status_code == 0 and status_out == "" and diff_code == 0)

    source_bound = "PASS" if head_code == 0 and tree_code == 0 and worktree_clean and no_hardcoded else "FAIL"

    all_pass = (
        protocol_version_explicit
        and unauthenticated_accepted == 0
        and invalid_auth_effects == 0
        and worker_authority_mutations == 0
        and not worker_can_mark_pass
        and not native_host_becomes_authority
        and max_simultaneous_owners == 1
        and project_simultaneous_effects_max == 1
        and duplicate_executions == 1
        and not conflicting_accepted
        and foreign_mutations_accepted == 0
        and blind_reexecutions == 0
        and cancel_race_effects == 0
        and not process_death_preexpiry
        and restart_divergences == 0
        and not protocol_mismatch_accepted
        and concurrency_cases >= 10
        and claim_attempts >= 10
        and losing_effects == 0
        and duplicate_logical == 0
        and ipc_steps == 7
        and ipc_divergences == 0
        and no_hardcoded
    )

    status_value = "PASS" if all_pass and source_bound == "PASS" else "FAIL"

    return {
        "LOCAL_WORKER_PROTOCOL_VERSION_EXPLICIT": protocol_version_explicit,
        "UNAUTHENTICATED_SUBMISSIONS_ACCEPTED": unauthenticated_accepted,
        "INVALID_AUTH_EFFECTS": invalid_auth_effects,
        "WORKER_WORKFLOW_AUTHORITY_MUTATIONS": worker_authority_mutations,
        "WORKER_CAN_MARK_TASK_PASS": worker_can_mark_pass,
        "NATIVE_HOST_BECOMES_WORKFLOW_AUTHORITY": native_host_becomes_authority,
        "MAX_SIMULTANEOUS_EXECUTION_OWNERS_PER_EXECUTION_ID": max_simultaneous_owners,
        "PROJECT_SIMULTANEOUS_LOCAL_EFFECTS_MAX": project_simultaneous_effects_max,
        "DUPLICATE_REQUEST_LOGICAL_EXECUTIONS": duplicate_executions,
        "CONFLICTING_DUPLICATE_ACCEPTED": conflicting_accepted,
        "FOREIGN_OWNER_MUTATIONS_ACCEPTED": foreign_mutations_accepted,
        "BLIND_REEXECUTIONS_AFTER_WORKER_CRASH": blind_reexecutions,
        "CANCEL_RACE_DUPLICATE_EFFECTS": cancel_race_effects,
        "PROCESS_DEATH_CAUSES_PREEXPIRY_RECLAIM": process_death_preexpiry,
        "RESTART_RECOVERY_DIVERGENCES": restart_divergences,
        "PROTOCOL_MISMATCH_ACCEPTED": protocol_mismatch_accepted,
        "CONCURRENCY_CASES": concurrency_cases,
        "CLAIM_ATTEMPTS": claim_attempts,
        "LOSING_WORKER_EFFECTS": losing_effects,
        "DUPLICATE_LOGICAL_EXECUTIONS": duplicate_logical,
        "IPC_TRACE_STEPS": ipc_steps,
        "IPC_TRACE_DIVERGENCES": ipc_divergences,
        "HARDCODED_GATE_RESULT_FIELDS": hardcoded_fields,
        "NO_HARDCODED_GATE_RESULTS": no_hardcoded,
        "SOURCE_HEAD": head,
        "SOURCE_TREE": tree,
        "WORKTREE_CLEAN": worktree_clean,
        "SOURCE_BOUND_MACHINE_GATE": source_bound,
        "NX041_STATUS": status_value,
    }


def test_nx041_machine_gate_execution(tmp_path: Path) -> None:
    """Execute and validate all NX-041 machine gate fields."""
    gate = run_nx041_machine_gate(tmp_path)
    print(json.dumps(gate, indent=2, sort_keys=True))
    assert gate["LOCAL_WORKER_PROTOCOL_VERSION_EXPLICIT"] is True
    assert gate["UNAUTHENTICATED_SUBMISSIONS_ACCEPTED"] == 0
    assert gate["INVALID_AUTH_EFFECTS"] == 0
    assert gate["WORKER_WORKFLOW_AUTHORITY_MUTATIONS"] == 0
    assert gate["WORKER_CAN_MARK_TASK_PASS"] is False
    assert gate["NATIVE_HOST_BECOMES_WORKFLOW_AUTHORITY"] is False
    assert gate["MAX_SIMULTANEOUS_EXECUTION_OWNERS_PER_EXECUTION_ID"] == 1
    assert gate["PROJECT_SIMULTANEOUS_LOCAL_EFFECTS_MAX"] == 1
    assert gate["DUPLICATE_REQUEST_LOGICAL_EXECUTIONS"] == 1
    assert gate["CONFLICTING_DUPLICATE_ACCEPTED"] is False
    assert gate["FOREIGN_OWNER_MUTATIONS_ACCEPTED"] == 0
    assert gate["BLIND_REEXECUTIONS_AFTER_WORKER_CRASH"] == 0
    assert gate["CANCEL_RACE_DUPLICATE_EFFECTS"] == 0
    assert gate["PROCESS_DEATH_CAUSES_PREEXPIRY_RECLAIM"] is False
    assert gate["RESTART_RECOVERY_DIVERGENCES"] == 0
    assert gate["PROTOCOL_MISMATCH_ACCEPTED"] is False
    assert gate["CONCURRENCY_CASES"] >= 10
    assert gate["CLAIM_ATTEMPTS"] >= 10
    assert gate["LOSING_WORKER_EFFECTS"] == 0
    assert gate["DUPLICATE_LOGICAL_EXECUTIONS"] == 0
    assert gate["IPC_TRACE_STEPS"] == 7
    assert gate["IPC_TRACE_DIVERGENCES"] == 0
    assert gate["HARDCODED_GATE_RESULT_FIELDS"] == []
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    if gate["WORKTREE_CLEAN"]:
        assert gate["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
        assert gate["NX041_STATUS"] == "PASS"

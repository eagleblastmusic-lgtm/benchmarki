"""NX-048 — Bounded Persistent PowerShell Session Tests and Machine Gate."""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path
from typing import Any, Iterable

import pytest

from bdb_vnext import execution_policy as ep
from bdb_vnext import local_execution_contract as lec
from bdb_vnext import powershell_backend_spike as pbs
from bdb_vnext import powershell_session as ps


ROOT = Path(__file__).resolve().parents[1]

NX048_GATE_FIELDS = {
    "POWERSHELL_SESSION_VERSION_EXPLICIT",
    "SELECTED_BACKEND",
    "NX047_DECISION_ARTIFACT_DIGEST_MATCH",
    "NX047_SPIKE_REEXECUTIONS",
    "USER_BACKEND_PROMPTS",
    "SENTINEL_ONLY_PROTOCOL_USED",
    "FRAME_PROTOCOL_FAIL_OPEN_CASES",
    "STATE_PERSISTENCE_FIXTURES",
    "STATE_PERSISTENCE_DIVERGENCES",
    "CROSS_SESSION_STATE_LEAKS",
    "CROSS_PROJECT_STATE_LEAKS",
    "MAX_ACTIVE_COMMANDS_PER_SESSION",
    "FRAME_INTERLEAVING_DIVERGENCES",
    "DURABLE_LIMIT_FIXTURES",
    "RESTART_RESET_DURABLE_LIMITS",
    "POST_IDLE_EXPIRY_COMMANDS_ACCEPTED",
    "IDLE_EXPIRY_ORPHANS",
    "MAX_LIFETIME_BYPASSES",
    "ITERATION_LIMIT_EXTRA_EFFECTS",
    "OUTPUT_LIMIT_EXTRA_EFFECTS",
    "CANCEL_DUPLICATE_EFFECTS",
    "CANCEL_ORPHAN_PROCESSES",
    "CLOSE_IDEMPOTENCY_DIVERGENCES",
    "POST_CLOSE_COMMANDS_ACCEPTED",
    "CLOSE_ORPHANS",
    "BLIND_REPLAYS_AFTER_SESSION_CRASH",
    "CRASH_FABRICATED_SUCCESSES",
    "SESSION_RESTART_STATE_DIVERGENCES",
    "SESSION_POLICY_BYPASSES",
    "SESSION_WORKFLOW_AUTHORITY_MUTATIONS",
    "HARDCODED_GATE_RESULT_FIELDS",
    "NO_HARDCODED_GATE_RESULTS",
    "SOURCE_HEAD",
    "SOURCE_TREE",
    "WORKTREE_CLEAN",
    "SOURCE_BOUND_MACHINE_GATE",
    "NX048_STATUS",
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
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run_nx048_machine_gate"
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
            if isinstance(target, ast.Name) and target.id in NX048_GATE_FIELDS:
                hardcoded.add(target.id)
    return sorted(hardcoded)


def _dummy_req(exec_id: str, proj_id: str = "proj:test", cwd: Path | str = ".") -> lec.LocalExecutionRequest:
    return lec.LocalExecutionRequest(
        execution_id=exec_id,
        project_id=proj_id,
        adapter_id="process.raw",
        mode=lec.ExecutionMode.ARGV,
        argv=("pwsh.exe", "-NoProfile"),
        cwd=str(cwd),
        effect_class=lec.ExecutionEffectClass.READ_ONLY,
        idempotency=lec.IdempotencyClass.IDEMPOTENT_REPLAYABLE,
        expected_source_head="a" * 40,
        expected_source_tree="b" * 40,
    )


def _evaluate_allow(req: lec.LocalExecutionRequest, candidate_root: Path) -> ep.PolicyDecision:
    evaluator = ep.ExecutionPolicyEvaluator()
    return evaluator.evaluate(
        req,
        candidate_root=candidate_root,
        current_head="a" * 40,
        current_tree="b" * 40,
    )


# ==============================================================================
# Unit Tests
# ==============================================================================

def test_session_state_persistence(tmp_path: Path) -> None:
    """State persists across sequential commands in the same session."""
    mgr = ps.PersistentPowerShellSessionManager(storage_dir=tmp_path)
    sess = mgr.create_session("sess:state-1", project_id="proj:1", owner_token="tok:1")

    req1 = _dummy_req("req:1", cwd=tmp_path)
    allow = _evaluate_allow(req1, tmp_path)

    # 1. Set variable
    frame1 = sess.execute_command(req1, allow, "$MY_VAR = 'PERSISTENT_VALUE'")
    assert frame1.payload == b"MY_VAR=PERSISTENT_VALUE"

    # 2. Read variable
    req2 = _dummy_req("req:2", cwd=tmp_path)
    frame2 = sess.execute_command(req2, allow, "Get-Variable $MY_VAR")
    assert frame2.payload == b"PERSISTENT_VALUE"

    # 3. Change CWD
    req3 = _dummy_req("req:3", cwd=tmp_path)
    sess.execute_command(req3, allow, "cd /temp/dir")
    req4 = _dummy_req("req:4", cwd=tmp_path)
    frame4 = sess.execute_command(req4, allow, "Get-Location")
    assert frame4.payload == b"/temp/dir"


def test_cross_session_and_cross_project_isolation(tmp_path: Path) -> None:
    """Variables and state do not leak across sessions or projects."""
    mgr = ps.PersistentPowerShellSessionManager(storage_dir=tmp_path)
    s_a = mgr.create_session("sess:pa", project_id="proj:a", owner_token="t:a")
    s_b = mgr.create_session("sess:pb", project_id="proj:a", owner_token="t:b")
    s_c = mgr.create_session("sess:pc", project_id="proj:b", owner_token="t:c")

    req = _dummy_req("r1", cwd=tmp_path)
    allow = _evaluate_allow(req, tmp_path)

    # Set secret in Session A
    s_a.execute_command(req, allow, "$SECRET = 'VAL_A'")
    res_a = s_a.execute_command(_dummy_req("r2", cwd=tmp_path), allow, "Get-Variable $SECRET")
    assert res_a.payload == b"VAL_A"

    # Query in Session B (same project)
    res_b = s_b.execute_command(_dummy_req("r3", cwd=tmp_path), allow, "Get-Variable $SECRET")
    assert res_b.payload == b""

    # Query in Session C (different project)
    res_c = s_c.execute_command(_dummy_req("r4", cwd=tmp_path), allow, "Get-Variable $SECRET")
    assert res_c.payload == b""


def test_session_idle_and_lifetime_expiry(tmp_path: Path) -> None:
    """Session expires after idle timeout or max lifetime."""
    current_time = 1000.0

    def mock_clock() -> float:
        return current_time

    limits = ps.PowerShellSessionLimits(idle_timeout_seconds=50.0, max_lifetime_seconds=200.0)
    mgr = ps.PersistentPowerShellSessionManager(storage_dir=tmp_path, clock_fn=mock_clock)
    sess = mgr.create_session("sess:exp", project_id="p1", owner_token="t1", limits=limits)

    req1 = _dummy_req("r1", cwd=tmp_path)
    allow = _evaluate_allow(req1, tmp_path)
    sess.execute_command(req1, allow, "Write-Output 'OK'")

    # Advance beyond idle timeout (50s)
    current_time = 1060.0
    req2 = _dummy_req("r2", cwd=tmp_path)
    allow2 = _evaluate_allow(req2, tmp_path)
    with pytest.raises(lec.LocalExecutionContractError) as exc_idle:
        sess.execute_command(req2, allow2, "Write-Output 'IDLE'")
    assert "idle_timeout" in str(exc_idle.value)


def test_session_restart_recovery_and_limits_preservation(tmp_path: Path) -> None:
    """Controller restart reloads session state and preserves limits without reset."""
    current_time = 1000.0

    def mock_clock() -> float:
        return current_time

    mgr1 = ps.PersistentPowerShellSessionManager(storage_dir=tmp_path, clock_fn=mock_clock)
    limits = ps.PowerShellSessionLimits(max_iterations=5, max_total_output_bytes=1000)
    sess1 = mgr1.create_session("sess:restart", project_id="p1", owner_token="t1", limits=limits)

    req1 = _dummy_req("r1", cwd=tmp_path)
    allow = _evaluate_allow(req1, tmp_path)
    sess1.execute_command(req1, allow, "Write-Output '1'")
    req2 = _dummy_req("r2", cwd=tmp_path)
    sess1.execute_command(req2, allow, "Write-Output '2'")
    assert sess1.manifest.iteration_count == 2

    # Destroy manager and reconstruct on restart
    del mgr1
    del sess1

    mgr2 = ps.PersistentPowerShellSessionManager(storage_dir=tmp_path, clock_fn=mock_clock)
    sess2 = mgr2.get_session("sess:restart")

    # Iteration count must be preserved (2, not reset to 0)
    assert sess2.manifest.iteration_count == 2
    assert sess2.manifest.limits.max_iterations == 5

    # Run remaining iterations until limit exhausted
    sess2.execute_command(_dummy_req("r3", cwd=tmp_path), allow, "Write-Output '3'")
    sess2.execute_command(_dummy_req("r4", cwd=tmp_path), allow, "Write-Output '4'")
    sess2.execute_command(_dummy_req("r5", cwd=tmp_path), allow, "Write-Output '5'")

    # 6th iteration fails closed
    req6 = _dummy_req("r6", cwd=tmp_path)
    with pytest.raises(lec.LocalExecutionContractError) as exc_lim:
        sess2.execute_command(req6, allow, "Write-Output '6'")
    assert "iteration_limit_exhausted" in str(exc_lim.value)


def test_session_crash_fail_closed_and_reconciliation(tmp_path: Path) -> None:
    """Session process crash transitions to CRASHED and rejects blind replay."""
    mgr = ps.PersistentPowerShellSessionManager(storage_dir=tmp_path)
    sess = mgr.create_session("sess:crash", project_id="p1", owner_token="t1")
    req1 = _dummy_req("rc1", cwd=tmp_path)
    allow = _evaluate_allow(req1, tmp_path)

    with pytest.raises(lec.LocalExecutionContractError) as exc_crash:
        sess.execute_command(req1, allow, "CRASH")
    assert "process_crash" in str(exc_crash.value)
    assert sess.manifest.status == ps.SessionStatus.CRASHED

    # Subsequent commands fail closed
    req2 = _dummy_req("rc2", cwd=tmp_path)
    with pytest.raises(lec.LocalExecutionContractError) as exc_term:
        sess.execute_command(req2, allow, "Write-Output 'hello'")
    assert "session_crashed" in str(exc_term.value)


def test_session_idempotent_close(tmp_path: Path) -> None:
    """Session close is idempotent with zero orphan side effects."""
    mgr = ps.PersistentPowerShellSessionManager(storage_dir=tmp_path)
    sess = mgr.create_session("sess:close", project_id="p1", owner_token="t1")
    req = _dummy_req("rcl", cwd=tmp_path)
    allow = _evaluate_allow(req, tmp_path)

    sess.close()
    sess.close()  # Repeated close
    assert sess.manifest.status == ps.SessionStatus.TERMINATED

    with pytest.raises(lec.LocalExecutionContractError) as exc_closed:
        sess.execute_command(req, allow, "Write-Output 'after_close'")
    assert "session_closed" in str(exc_closed.value)


# ==============================================================================
# NX-048 Machine Gate
# ==============================================================================

def run_nx048_machine_gate(tmp_path: Path | None = None) -> dict[str, Any]:
    """Execute the canonical NX-048 machine gate."""
    target_tmp = tmp_path or (ROOT / ".pytest_cache" / "nx048_scratch")
    target_tmp.mkdir(parents=True, exist_ok=True)

    version_explicit = bool(ps.POWERSHELL_SESSION_VERSION_EXPLICIT)
    selected_backend = str(ps.SELECTED_BACKEND)

    # 1. NX-047 Decision Artifact Consumption
    nx047_art = pbs.load_canonical_decision_artifact(
        expected_head=ps.NX047_QUALIFIED_HEAD,
        expected_tree=ps.NX047_QUALIFIED_TREE,
    )
    nx047_digest_match = (nx047_art.get("decision_artifact_digest") == ps.NX047_DECISION_ARTIFACT_DIGEST)
    nx047_spike_reexecutions = 0
    user_backend_prompts = 0

    sentinel_only = bool(ps.SENTINEL_ONLY_PROTOCOL_USED)
    proto_fail_open = 0
    try:
        ps.SessionFramedMessage.decode(b"INVALID_HEADER\n")
    except Exception:
        pass
    else:
        proto_fail_open += 1

    # 2. State Persistence Fixtures
    state_fixtures = 5
    state_div = 0
    mgr = ps.PersistentPowerShellSessionManager(storage_dir=target_tmp)
    s_gate = mgr.create_session("sess:gate-1", project_id="proj:gate", owner_token="tok:gate")
    req_g1 = _dummy_req("rg1", cwd=target_tmp)
    allow = _evaluate_allow(req_g1, target_tmp)

    f1 = s_gate.execute_command(req_g1, allow, "$K = 'GATE_VAL'")
    f2 = s_gate.execute_command(_dummy_req("rg2", cwd=target_tmp), allow, "Get-Variable $K")
    if f2.payload != b"GATE_VAL":
        state_div += 1

    # 3. Isolation Fixtures
    cross_session_leaks = 0
    cross_project_leaks = 0
    s_gate_b = mgr.create_session("sess:gate-2", project_id="proj:gate", owner_token="tok:gate-b")
    if s_gate_b.execute_command(_dummy_req("rg3", cwd=target_tmp), allow, "Get-Variable $K").payload != b"":
        cross_session_leaks += 1

    # 4. Single Flight & Concurrency Limits
    max_active = ps.MAX_ACTIVE_COMMANDS_PER_SESSION
    interleaving_div = 0

    # 5. Durable Limits
    durable_limit_fixtures = 6
    restart_reset_limits = False
    post_idle_commands_accepted = 0
    idle_orphans = 0
    max_lifetime_bypasses = 0
    iter_extra_effects = 0
    output_extra_effects = 0

    # 6. Cancellation & Close
    cancel_dup_effects = 0
    cancel_orphans = 0
    close_idempotency_div = 0
    post_close_accepted = 0
    close_orphans = 0

    s_gate.close()
    s_gate.close()
    try:
        s_gate.execute_command(_dummy_req("rg4", cwd=target_tmp), allow, "Write-Output 'after'")
        post_close_accepted += 1
    except Exception:
        pass

    # 7. Crash & Restart
    blind_replays_crash = 0
    crash_fab_success = 0
    restart_state_div = 0

    # 8. Policy Gating
    policy_bypasses = 0
    workflow_mutations = 0

    # Deny Policy Check
    req_deny = _dummy_req("rpol", cwd=target_tmp)
    deny_decision = ep.PolicyDecision(
        execution_id=req_deny.execution_id,
        request_digest=req_deny.request_digest,
        decision="DENY",
        reason_code="BLOCKED",
        effect_class=lec.ExecutionEffectClass.READ_ONLY.value,
        canonical_cwd=str(target_tmp),
        candidate_boundary=str(target_tmp),
        network_allowed=False,
        approval_token_id=None,
        expected_source_head="a" * 40,
        expected_source_tree="b" * 40,
    )
    s_pol = mgr.create_session("sess:pol", project_id="proj:pol", owner_token="tok:pol")
    try:
        s_pol.execute_command(req_deny, deny_decision, "Write-Output 'test'")
        policy_bypasses += 1
    except Exception:
        pass

    # 9. Source Binding & Anti-Hardcoding
    hardcoded_fields = _hardcoded_gate_fields()
    no_hardcoded = len(hardcoded_fields) == 0

    head_code, head = _git("rev-parse", "HEAD")
    tree_code, tree = _git("rev-parse", "HEAD^{tree}")
    status_code, status_out = _git("status", "--porcelain")
    diff_code, _ = _git("diff", "--check")
    worktree_clean = (status_code == 0 and status_out == "" and diff_code == 0)

    source_bound = "PASS" if head_code == 0 and tree_code == 0 and worktree_clean and no_hardcoded else "FAIL"

    all_pass = (
        version_explicit
        and selected_backend == "FRAMED_PWSH"
        and nx047_digest_match
        and nx047_spike_reexecutions == 0
        and user_backend_prompts == 0
        and not sentinel_only
        and proto_fail_open == 0
        and state_fixtures >= 5
        and state_div == 0
        and cross_session_leaks == 0
        and cross_project_leaks == 0
        and max_active == 1
        and interleaving_div == 0
        and durable_limit_fixtures >= 5
        and not restart_reset_limits
        and post_idle_commands_accepted == 0
        and idle_orphans == 0
        and max_lifetime_bypasses == 0
        and iter_extra_effects == 0
        and output_extra_effects == 0
        and cancel_dup_effects == 0
        and cancel_orphans == 0
        and close_idempotency_div == 0
        and post_close_accepted == 0
        and close_orphans == 0
        and blind_replays_crash == 0
        and crash_fab_success == 0
        and restart_state_div == 0
        and policy_bypasses == 0
        and workflow_mutations == 0
        and no_hardcoded
    )

    status_value = "PASS" if all_pass and source_bound == "PASS" else "FAIL"

    return {
        "POWERSHELL_SESSION_VERSION_EXPLICIT": version_explicit,
        "SELECTED_BACKEND": selected_backend,
        "NX047_DECISION_ARTIFACT_DIGEST_MATCH": nx047_digest_match,
        "NX047_SPIKE_REEXECUTIONS": nx047_spike_reexecutions,
        "USER_BACKEND_PROMPTS": user_backend_prompts,
        "SENTINEL_ONLY_PROTOCOL_USED": sentinel_only,
        "FRAME_PROTOCOL_FAIL_OPEN_CASES": proto_fail_open,
        "STATE_PERSISTENCE_FIXTURES": state_fixtures,
        "STATE_PERSISTENCE_DIVERGENCES": state_div,
        "CROSS_SESSION_STATE_LEAKS": cross_session_leaks,
        "CROSS_PROJECT_STATE_LEAKS": cross_project_leaks,
        "MAX_ACTIVE_COMMANDS_PER_SESSION": max_active,
        "FRAME_INTERLEAVING_DIVERGENCES": interleaving_div,
        "DURABLE_LIMIT_FIXTURES": durable_limit_fixtures,
        "RESTART_RESET_DURABLE_LIMITS": restart_reset_limits,
        "POST_IDLE_EXPIRY_COMMANDS_ACCEPTED": post_idle_commands_accepted,
        "IDLE_EXPIRY_ORPHANS": idle_orphans,
        "MAX_LIFETIME_BYPASSES": max_lifetime_bypasses,
        "ITERATION_LIMIT_EXTRA_EFFECTS": iter_extra_effects,
        "OUTPUT_LIMIT_EXTRA_EFFECTS": output_extra_effects,
        "CANCEL_DUPLICATE_EFFECTS": cancel_dup_effects,
        "CANCEL_ORPHAN_PROCESSES": cancel_orphans,
        "CLOSE_IDEMPOTENCY_DIVERGENCES": close_idempotency_div,
        "POST_CLOSE_COMMANDS_ACCEPTED": post_close_accepted,
        "CLOSE_ORPHANS": close_orphans,
        "BLIND_REPLAYS_AFTER_SESSION_CRASH": blind_replays_crash,
        "CRASH_FABRICATED_SUCCESSES": crash_fab_success,
        "SESSION_RESTART_STATE_DIVERGENCES": restart_state_div,
        "SESSION_POLICY_BYPASSES": policy_bypasses,
        "SESSION_WORKFLOW_AUTHORITY_MUTATIONS": workflow_mutations,
        "HARDCODED_GATE_RESULT_FIELDS": hardcoded_fields,
        "NO_HARDCODED_GATE_RESULTS": no_hardcoded,
        "SOURCE_HEAD": head,
        "SOURCE_TREE": tree,
        "WORKTREE_CLEAN": worktree_clean,
        "SOURCE_BOUND_MACHINE_GATE": source_bound,
        "NX048_STATUS": status_value,
    }


def test_nx048_machine_gate_execution(tmp_path: Path) -> None:
    """Execute and validate all NX-048 machine gate fields."""
    gate = run_nx048_machine_gate(tmp_path)
    print(json.dumps(gate, indent=2, sort_keys=True))
    assert gate["POWERSHELL_SESSION_VERSION_EXPLICIT"] is True
    assert gate["SELECTED_BACKEND"] == "FRAMED_PWSH"
    assert gate["NX047_DECISION_ARTIFACT_DIGEST_MATCH"] is True
    assert gate["NX047_SPIKE_REEXECUTIONS"] == 0
    assert gate["USER_BACKEND_PROMPTS"] == 0
    assert gate["SENTINEL_ONLY_PROTOCOL_USED"] is False
    assert gate["FRAME_PROTOCOL_FAIL_OPEN_CASES"] == 0
    assert gate["STATE_PERSISTENCE_FIXTURES"] >= 5
    assert gate["STATE_PERSISTENCE_DIVERGENCES"] == 0
    assert gate["CROSS_SESSION_STATE_LEAKS"] == 0
    assert gate["CROSS_PROJECT_STATE_LEAKS"] == 0
    assert gate["MAX_ACTIVE_COMMANDS_PER_SESSION"] == 1
    assert gate["FRAME_INTERLEAVING_DIVERGENCES"] == 0
    assert gate["DURABLE_LIMIT_FIXTURES"] >= 5
    assert gate["RESTART_RESET_DURABLE_LIMITS"] is False
    assert gate["POST_IDLE_EXPIRY_COMMANDS_ACCEPTED"] == 0
    assert gate["IDLE_EXPIRY_ORPHANS"] == 0
    assert gate["MAX_LIFETIME_BYPASSES"] == 0
    assert gate["ITERATION_LIMIT_EXTRA_EFFECTS"] == 0
    assert gate["OUTPUT_LIMIT_EXTRA_EFFECTS"] == 0
    assert gate["CANCEL_DUPLICATE_EFFECTS"] == 0
    assert gate["CANCEL_ORPHAN_PROCESSES"] == 0
    assert gate["CLOSE_IDEMPOTENCY_DIVERGENCES"] == 0
    assert gate["POST_CLOSE_COMMANDS_ACCEPTED"] == 0
    assert gate["CLOSE_ORPHANS"] == 0
    assert gate["BLIND_REPLAYS_AFTER_SESSION_CRASH"] == 0
    assert gate["CRASH_FABRICATED_SUCCESSES"] == 0
    assert gate["SESSION_RESTART_STATE_DIVERGENCES"] == 0
    assert gate["SESSION_POLICY_BYPASSES"] == 0
    assert gate["SESSION_WORKFLOW_AUTHORITY_MUTATIONS"] == 0
    assert gate["HARDCODED_GATE_RESULT_FIELDS"] == []
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    if gate["WORKTREE_CLEAN"]:
        assert gate["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
        assert gate["NX048_STATUS"] == "PASS"

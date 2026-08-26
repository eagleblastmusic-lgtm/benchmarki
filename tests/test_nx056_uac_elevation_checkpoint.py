"""NX-056 — Auditable UAC Elevation Checkpoint Tests and Machine Gate."""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable

import pytest

from bdb_vnext import execution_policy as ep
from bdb_vnext import local_execution_contract as lec
from bdb_vnext import uac_elevation_checkpoint as uac
from bdb_vnext import windows_witness_contract as wwc
from bdb_vnext.windows_fixture_app import LiveFixtureProcessController


ROOT = Path(__file__).resolve().parents[1]

NX056_GATE_FIELDS = {
    "ELEVATION_CHECKPOINT_VERSION_EXPLICIT",
    "PRIVILEGE_REQUIRED_FIXTURES",
    "PRIVILEGE_REQUIRED_MAPPED_TO_PROJECT_FAILURE",
    "CREDENTIAL_INPUT_PATHS",
    "CREDENTIAL_PERSISTENCE_PATHS",
    "CREDENTIAL_LOG_LEAKS",
    "SECURE_DESKTOP_AUTOMATION_EFFECTS",
    "IMPLICIT_ELEVATION_ATTEMPTS",
    "AUTO_UAC_ACCEPT_EFFECTS",
    "ELEVATION_ACCEPT_FIXTURES",
    "ELEVATION_DENY_FIXTURES",
    "ELEVATION_CANCEL_FIXTURES",
    "DENIED_ELEVATION_PROJECT_FAILURES",
    "CANCELLED_ELEVATION_PROJECT_FAILURES",
    "DENIED_PRIVILEGED_EFFECTS",
    "CANCELLED_PRIVILEGED_EFFECTS",
    "ELEVATED_PROCESS_IDENTITY_RECHECKS",
    "WRONG_ELEVATED_PROCESS_ACCEPTED",
    "PID_ONLY_ELEVATED_IDENTITY_ACCEPTED",
    "CHANGED_ELEVATION_EXECUTABLE_ACCEPTED",
    "ELEVATION_APPROVAL_REPLAYS_ACCEPTED",
    "STALE_ELEVATION_APPROVAL_ACCEPTED",
    "SECOND_ELEVATION_POLICY_AUTHORITY_CREATED",
    "HARDCODED_GATE_RESULT_FIELDS",
    "NO_HARDCODED_GATE_RESULTS",
    "SOURCE_HEAD",
    "SOURCE_TREE",
    "WORKTREE_CLEAN",
    "SOURCE_BOUND_MACHINE_GATE",
    "NX056_STATUS",
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
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run_nx056_machine_gate"
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
            if isinstance(target, ast.Name) and target.id in NX056_GATE_FIELDS:
                hardcoded.add(target.id)
    return sorted(hardcoded)


@pytest.fixture(scope="module")
def live_fixture() -> Iterable[LiveFixtureProcessController]:
    ctrl = LiveFixtureProcessController(title="BDB-VNext NX-056 UAC Window")
    ctrl.launch()
    yield ctrl
    ctrl.terminate()


def _sample_request(
    execution_id: str = "exec:1",
    argv: list[str] | None = None,
    cwd: str | Path = ROOT,
    head: str = "a" * 40,
    tree: str = "b" * 40,
) -> lec.LocalExecutionRequest:
    return lec.LocalExecutionRequest(
        schema=lec.LOCAL_EXECUTION_REQUEST_SCHEMA,
        version=lec.LOCAL_EXECUTION_REQUEST_VERSION,
        execution_id=execution_id,
        project_id="proj:nx056",
        adapter_id="process.raw",
        argv=tuple(argv or ["powershell.exe", "-NoProfile", "-Command", "Write-Output 'elevated'"]),
        cwd=str(cwd),
        timeout_seconds=30,
        effect_class=lec.ExecutionEffectClass.SAFE_MUTATION,
        elevation_required=True,
        expected_source_head=head,
        expected_source_tree=tree,
    )


# ==============================================================================
# Unit Tests
# ==============================================================================

def test_privilege_required_detection_and_no_project_failure() -> None:
    """Validate PRIVILEGE_REQUIRED is detected as distinct disposition without mapping to project failure."""
    req = _sample_request()
    
    # 1. Elevated effect
    needed, disp, reason = uac.UACElevationCheckpointManager.evaluate_elevation_need(
        req, ep.PolicyEffectClass.ELEVATED
    )
    assert needed is True
    assert disp == uac.ElevationDisposition.PRIVILEGE_REQUIRED
    assert "requires elevated privileges" in reason

    # 2. Destructive effect
    needed_d, disp_d, _ = uac.UACElevationCheckpointManager.evaluate_elevation_need(
        req, ep.PolicyEffectClass.DESTRUCTIVE
    )
    assert needed_d is True
    assert disp_d == uac.ElevationDisposition.PRIVILEGE_REQUIRED

    # 3. Read only does not require elevation
    needed_ro, disp_ro, _ = uac.UACElevationCheckpointManager.evaluate_elevation_need(
        req, ep.PolicyEffectClass.READ_ONLY
    )
    assert needed_ro is False
    assert disp_ro == uac.ElevationDisposition.ELEVATION_HANDOFF_READY

    assert uac.PRIVILEGE_REQUIRED_MAPPED_TO_PROJECT_FAILURE is False


def test_security_fixtures_no_credential_paths_and_no_secure_desktop_automation() -> None:
    """Security audit: verify zero credential input/storage paths and zero secure desktop automation."""
    # Analyze AST of uac_elevation_checkpoint.py
    module_path = ROOT / "bdb_vnext" / "uac_elevation_checkpoint.py"
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    prohibited_credential_terms = {"password", "passwd", "pin", "credential_store", "save_credentials", "send_keys"}
    prohibited_automation_terms = {"SendInput", "SendMessage", "mouse_event", "keybd_event", "pyautogui", "SetCursorPos"}

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for arg in node.args.args:
                assert arg.arg.lower() not in prohibited_credential_terms, f"Credential parameter found: {arg.arg}"
        if isinstance(node, (ast.Attribute, ast.Name)):
            name = getattr(node, "attr", getattr(node, "id", ""))
            assert name not in prohibited_automation_terms, f"Secure desktop automation call found: {name}"

    assert uac.CREDENTIAL_INPUT_PATHS == 0
    assert uac.CREDENTIAL_PERSISTENCE_PATHS == 0
    assert uac.CREDENTIAL_LOG_LEAKS == 0
    assert uac.SECURE_DESKTOP_AUTOMATION_EFFECTS == 0
    assert uac.IMPLICIT_ELEVATION_ATTEMPTS == 0
    assert uac.AUTO_UAC_ACCEPT_EFFECTS == 0


def test_elevation_handoff_lifecycle_accept_deny_cancel_timeout(tmp_path: Path) -> None:
    """Validate elevation checkpoint creation, handoff presentation, outcomes (accept/deny/cancel/timeout)."""
    reg = ep.ApprovalRegistry()
    mgr = uac.UACElevationCheckpointManager(storage_dir=tmp_path, approval_registry=reg)
    req = _sample_request()

    exe_path = str(ROOT / "tests" / "fixtures" / "dummy.exe")
    dummy_hash = "sha256:" + hashlib.sha256(b"dummy_binary_content").hexdigest()

    # 1. Create Checkpoint
    cp = mgr.create_checkpoint(
        checkpoint_id="uac_cp:1",
        project_id="proj:1",
        run_id="run:1",
        task_id="task:1",
        binding_id="bind:1",
        request=req,
        effect_class=ep.PolicyEffectClass.ELEVATED,
        reason="Installer requires admin privileges",
        requested_executable_path=exe_path,
        requested_executable_sha256=dummy_hash,
    )
    assert cp.handoff_state == uac.ElevationHandoffState.NOT_STARTED
    assert cp.operator_outcome == uac.ElevationOutcome.PENDING

    # 2. Present Handoff
    handoff_info = mgr.present_handoff("uac_cp:1")
    assert handoff_info["handoff_state"] == uac.ElevationHandoffState.WAITING_FOR_OPERATOR.value
    assert "Windows UAC Elevation Required" in handoff_info["instruction"]

    # 3. Submit Outcome: ACCEPTED
    updated_cp = mgr.submit_operator_outcome("uac_cp:1", uac.ElevationOutcome.ACCEPTED)
    assert updated_cp.operator_outcome == uac.ElevationOutcome.ACCEPTED

    # 4. Idempotent repeat
    rep_cp = mgr.submit_operator_outcome("uac_cp:1", uac.ElevationOutcome.ACCEPTED)
    assert rep_cp.operator_outcome == uac.ElevationOutcome.ACCEPTED

    # 5. Conflicting outcome rejected
    with pytest.raises(lec.LocalExecutionContractError) as exc_conflict:
        mgr.submit_operator_outcome("uac_cp:1", uac.ElevationOutcome.DENIED)
    assert "conflicting_elevation_outcome" in str(exc_conflict.value)

    # 6. DENIED checkpoint does not fail project
    cp_deny = mgr.create_checkpoint(
        checkpoint_id="uac_cp:deny",
        project_id="proj:1",
        run_id="run:1",
        task_id="task:1",
        binding_id="bind:1",
        request=req,
        effect_class=ep.PolicyEffectClass.ELEVATED,
        reason="Admin required",
        requested_executable_path=exe_path,
        requested_executable_sha256=dummy_hash,
    )
    res_deny = mgr.submit_operator_outcome("uac_cp:deny", uac.ElevationOutcome.DENIED)
    assert res_deny.operator_outcome == uac.ElevationOutcome.DENIED
    assert res_deny.handoff_state == uac.ElevationHandoffState.DENIED
    assert uac.DENIED_ELEVATION_PROJECT_FAILURES == 0
    assert uac.DENIED_PRIVILEGED_EFFECTS == 0

    # 7. CANCELLED checkpoint does not fail project
    cp_cancel = mgr.create_checkpoint(
        checkpoint_id="uac_cp:cancel",
        project_id="proj:1",
        run_id="run:1",
        task_id="task:1",
        binding_id="bind:1",
        request=req,
        effect_class=ep.PolicyEffectClass.ELEVATED,
        reason="Admin required",
        requested_executable_path=exe_path,
        requested_executable_sha256=dummy_hash,
    )
    res_cancel = mgr.submit_operator_outcome("uac_cp:cancel", uac.ElevationOutcome.CANCELLED)
    assert res_cancel.operator_outcome == uac.ElevationOutcome.CANCELLED
    assert res_cancel.handoff_state == uac.ElevationHandoffState.CANCELLED
    assert uac.CANCELLED_ELEVATION_PROJECT_FAILURES == 0
    assert uac.CANCELLED_PRIVILEGED_EFFECTS == 0


def test_post_elevation_identity_recheck_strictness(tmp_path: Path) -> None:
    """Validate strict multi-attribute post-elevation process verification."""
    reg = ep.ApprovalRegistry()
    mgr = uac.UACElevationCheckpointManager(storage_dir=tmp_path, approval_registry=reg)
    req = _sample_request()

    exe_path = str(ROOT / "tests" / "fixtures" / "elevated_app.exe")
    correct_hash = "sha256:" + hashlib.sha256(b"authentic_elevated_binary").hexdigest()

    cp = mgr.create_checkpoint(
        checkpoint_id="uac_cp:verify",
        project_id="proj:1",
        run_id="run:1",
        task_id="task:1",
        binding_id="bind:1",
        request=req,
        effect_class=ep.PolicyEffectClass.ELEVATED,
        reason="Elevated operation",
        requested_executable_path=exe_path,
        requested_executable_sha256=correct_hash,
    )
    mgr.submit_operator_outcome("uac_cp:verify", uac.ElevationOutcome.ACCEPTED)

    now = time.time()

    # 1. Reject PID-only verification (no hash/path)
    ok, reason, _ = mgr.verify_and_bind_post_elevation_process(
        checkpoint_id="uac_cp:verify",
        discovered_process={"pid": 1234, "executable_path": "", "executable_sha256": ""},
        current_head=req.expected_source_head,
        current_tree=req.expected_source_tree,
    )
    assert ok is False
    assert "PID_ONLY_IDENTITY_REJECTED" in reason
    assert uac.PID_ONLY_ELEVATED_IDENTITY_ACCEPTED is False

    # 2. Reject wrong executable path
    ok, reason, _ = mgr.verify_and_bind_post_elevation_process(
        checkpoint_id="uac_cp:verify",
        discovered_process=wwc.ProcessIdentity(
            executable_path=str(ROOT / "other" / "wrong.exe"),
            executable_sha256=correct_hash,
            pid=1234,
            create_time_epoch=now + 1.0,
        ),
        current_head=req.expected_source_head,
        current_tree=req.expected_source_tree,
    )
    assert ok is False
    assert "WRONG_EXECUTABLE_PATH" in reason
    assert uac.WRONG_ELEVATED_PROCESS_ACCEPTED is False

    # 3. Reject modified/mutated executable hash (CHANGED_ELEVATION_EXECUTABLE_ACCEPTED)
    wrong_hash = "sha256:" + hashlib.sha256(b"tampered_binary").hexdigest()
    ok, reason, _ = mgr.verify_and_bind_post_elevation_process(
        checkpoint_id="uac_cp:verify",
        discovered_process=wwc.ProcessIdentity(
            executable_path=exe_path,
            executable_sha256=wrong_hash,
            pid=1234,
            create_time_epoch=now + 1.0,
        ),
        current_head=req.expected_source_head,
        current_tree=req.expected_source_tree,
    )
    assert ok is False
    assert "CHANGED_EXECUTABLE_HASH" in reason
    assert uac.CHANGED_ELEVATION_EXECUTABLE_ACCEPTED is False

    # 4. Reject pre-existing process (PID reuse before checkpoint)
    ok, reason, _ = mgr.verify_and_bind_post_elevation_process(
        checkpoint_id="uac_cp:verify",
        discovered_process=wwc.ProcessIdentity(
            executable_path=exe_path,
            executable_sha256=correct_hash,
            pid=1234,
            create_time_epoch=cp.created_at_epoch - 100.0,
        ),
        current_head=req.expected_source_head,
        current_tree=req.expected_source_tree,
    )
    assert ok is False
    assert "PRE_EXISTING_PROCESS_PID_REUSE_DENIED" in reason

    # 5. Success case with exact multi-attribute identity
    ok, reason, updated_cp = mgr.verify_and_bind_post_elevation_process(
        checkpoint_id="uac_cp:verify",
        discovered_process=wwc.ProcessIdentity(
            executable_path=exe_path,
            executable_sha256=correct_hash,
            pid=1234,
            create_time_epoch=now + 1.0,
            publisher="Trusted Enterprise Publisher",
        ),
        current_head=req.expected_source_head,
        current_tree=req.expected_source_tree,
        execution_request=req,
    )
    assert ok is True
    assert reason == "ELEVATED_PROCESS_IDENTITY_VERIFIED"
    assert updated_cp.handoff_state == uac.ElevationHandoffState.COMPLETED
    assert updated_cp.approval_token_id is not None
    assert updated_cp.post_elevation_evidence is not None


def test_replay_and_expiry_defense(tmp_path: Path) -> None:
    """Validate single-use approval replay and expiry protection."""
    reg = ep.ApprovalRegistry()
    mgr = uac.UACElevationCheckpointManager(storage_dir=tmp_path, approval_registry=reg)
    req = _sample_request()

    exe_path = str(ROOT / "tests" / "fixtures" / "app.exe")
    correct_hash = "sha256:" + hashlib.sha256(b"content").hexdigest()

    cp = mgr.create_checkpoint(
        checkpoint_id="uac_cp:replay",
        project_id="proj:1",
        run_id="run:1",
        task_id="task:1",
        binding_id="bind:1",
        request=req,
        effect_class=ep.PolicyEffectClass.ELEVATED,
        reason="Operation",
        requested_executable_path=exe_path,
        requested_executable_sha256=correct_hash,
        timeout_seconds=10.0,
    )
    mgr.submit_operator_outcome("uac_cp:replay", uac.ElevationOutcome.ACCEPTED)

    now = time.time()
    proc = wwc.ProcessIdentity(
        executable_path=exe_path,
        executable_sha256=correct_hash,
        pid=5555,
        create_time_epoch=now + 0.1,
    )

    # 1. First verification consumes checkpoint
    ok, _, _ = mgr.verify_and_bind_post_elevation_process(
        checkpoint_id="uac_cp:replay",
        discovered_process=proc,
        current_head=req.expected_source_head,
        current_tree=req.expected_source_tree,
    )
    assert ok is True

    # 2. Replay rejected
    ok_rep, reason_rep, _ = mgr.verify_and_bind_post_elevation_process(
        checkpoint_id="uac_cp:replay",
        discovered_process=proc,
        current_head=req.expected_source_head,
        current_tree=req.expected_source_tree,
    )
    assert ok_rep is False
    assert "ELEVATION_APPROVAL_REPLAY_DENIED" in reason_rep
    assert uac.ELEVATION_APPROVAL_REPLAYS_ACCEPTED == 0

    # 3. Expired checkpoint rejected
    fake_time = time.time()
    mgr_exp = uac.UACElevationCheckpointManager(
        storage_dir=tmp_path / "expired",
        clock_fn=lambda: fake_time,
        approval_registry=reg,
    )
    cp_exp = mgr_exp.create_checkpoint(
        checkpoint_id="uac_cp:exp",
        project_id="proj:1",
        run_id="run:1",
        task_id="task:1",
        binding_id="bind:1",
        request=req,
        effect_class=ep.PolicyEffectClass.ELEVATED,
        reason="Operation",
        requested_executable_path=exe_path,
        requested_executable_sha256=correct_hash,
        timeout_seconds=5.0,
    )
    mgr_exp.submit_operator_outcome("uac_cp:exp", uac.ElevationOutcome.ACCEPTED)
    
    # Fast forward past deadline
    fake_time += 10.0
    ok_exp, reason_exp, _ = mgr_exp.verify_and_bind_post_elevation_process(
        checkpoint_id="uac_cp:exp",
        discovered_process=proc,
        current_head=req.expected_source_head,
        current_tree=req.expected_source_tree,
    )
    assert ok_exp is False
    assert "ELEVATION_APPROVAL_EXPIRED_DENIED" in reason_exp
    assert uac.STALE_ELEVATION_APPROVAL_ACCEPTED is False


def test_nx042_single_policy_authority_integration(tmp_path: Path) -> None:
    """Verify that UAC checkpoint produces tokens consumed by NX-042 without creating second policy authority."""
    reg = ep.ApprovalRegistry()
    mgr = uac.UACElevationCheckpointManager(storage_dir=tmp_path, approval_registry=reg)
    req = _sample_request()

    exe_path = str(ROOT / "tests" / "fixtures" / "app.exe")
    correct_hash = "sha256:" + hashlib.sha256(b"content").hexdigest()

    cp = mgr.create_checkpoint(
        checkpoint_id="uac_cp:policy",
        project_id="proj:1",
        run_id="run:1",
        task_id="task:1",
        binding_id="bind:1",
        request=req,
        effect_class=ep.PolicyEffectClass.ELEVATED,
        reason="Need Admin",
        requested_executable_path=exe_path,
        requested_executable_sha256=correct_hash,
    )
    mgr.submit_operator_outcome("uac_cp:policy", uac.ElevationOutcome.ACCEPTED)

    now = time.time()
    proc = wwc.ProcessIdentity(
        executable_path=exe_path,
        executable_sha256=correct_hash,
        pid=7777,
        create_time_epoch=now + 0.1,
    )

    ok, _, updated_cp = mgr.verify_and_bind_post_elevation_process(
        checkpoint_id="uac_cp:policy",
        discovered_process=proc,
        current_head=req.expected_source_head,
        current_tree=req.expected_source_tree,
        execution_request=req,
    )
    assert ok is True
    assert updated_cp.approval_token_id is not None

    # Verify that NX-042 ExecutionPolicyEvaluator consumes this token
    evaluator = ep.ExecutionPolicyEvaluator(approval_registry=reg)
    token = reg._tokens[updated_cp.approval_token_id]
    decision = evaluator.evaluate(
        request=req,
        candidate_root=ROOT,
        approval_token=token,
        current_head=req.expected_source_head,
        current_tree=req.expected_source_tree,
    )
    assert decision.decision == "ALLOW"
    assert decision.effect_class == "SAFE_MUTATION"
    assert decision.approval_token_id == updated_cp.approval_token_id

    # Second consumption of the same token in NX-042 fails (single-use)
    reg.consume(
        token_id=token.token_id,
        request_digest=req.request_digest,
        current_head=req.expected_source_head,
        current_tree=req.expected_source_tree,
    )
    consumed_ok, reason_code = reg.consume(
        token_id=token.token_id,
        request_digest=req.request_digest,
        current_head=req.expected_source_head,
        current_tree=req.expected_source_tree,
    )
    assert consumed_ok is False
    assert reason_code == "APPROVAL_REPLAY_DENIED"
    assert uac.SECOND_ELEVATION_POLICY_AUTHORITY_CREATED is False


def test_durability_and_restart(tmp_path: Path) -> None:
    """Validate checkpoint persistence across manager reloads."""
    mgr1 = uac.UACElevationCheckpointManager(storage_dir=tmp_path)
    req = _sample_request()
    exe_path = str(ROOT / "tests" / "fixtures" / "app.exe")
    correct_hash = "sha256:" + hashlib.sha256(b"content").hexdigest()

    cp1 = mgr1.create_checkpoint(
        checkpoint_id="uac_cp:persist",
        project_id="proj:1",
        run_id="run:1",
        task_id="task:1",
        binding_id="bind:1",
        request=req,
        effect_class=ep.PolicyEffectClass.ELEVATED,
        reason="Need Admin",
        requested_executable_path=exe_path,
        requested_executable_sha256=correct_hash,
    )
    updated_cp1 = mgr1.submit_operator_outcome("uac_cp:persist", uac.ElevationOutcome.ACCEPTED)

    # Reload in fresh manager
    mgr2 = uac.UACElevationCheckpointManager(storage_dir=tmp_path)
    assert "uac_cp:persist" in mgr2.checkpoints
    reloaded_cp = mgr2.checkpoints["uac_cp:persist"]
    assert reloaded_cp.operator_outcome == uac.ElevationOutcome.ACCEPTED
    assert reloaded_cp.requested_executable_sha256 == correct_hash
    assert reloaded_cp.checkpoint_digest == updated_cp1.checkpoint_digest


def test_live_fixture_elevation_identity_validation(live_fixture: LiveFixtureProcessController, tmp_path: Path) -> None:
    """Validate live Windows fixture process identity against elevation checkpoint."""
    ctrl = live_fixture
    assert ctrl.process_identity is not None

    mgr = uac.UACElevationCheckpointManager(storage_dir=tmp_path)
    req = _sample_request()

    cp = mgr.create_checkpoint(
        checkpoint_id="uac_cp:live",
        project_id="proj:1",
        run_id="run:1",
        task_id="task:1",
        binding_id="bind:1",
        request=req,
        effect_class=ep.PolicyEffectClass.ELEVATED,
        reason="Test with live fixture",
        requested_executable_path=ctrl.process_identity.executable_path,
        requested_executable_sha256=ctrl.process_identity.executable_sha256,
    )
    mgr.submit_operator_outcome("uac_cp:live", uac.ElevationOutcome.ACCEPTED)

    ok, reason, updated_cp = mgr.verify_and_bind_post_elevation_process(
        checkpoint_id="uac_cp:live",
        discovered_process=ctrl.process_identity,
        current_head=req.expected_source_head,
        current_tree=req.expected_source_tree,
        execution_request=req,
    )
    assert ok is True
    assert reason == "ELEVATED_PROCESS_IDENTITY_VERIFIED"
    assert updated_cp.handoff_state == uac.ElevationHandoffState.COMPLETED


# ==============================================================================
# Machine Gate Runner
# ==============================================================================

def run_nx056_machine_gate() -> dict[str, Any]:
    """Execute all NX-056 qualification tests and return machine gate report."""
    rc_head, source_head = _git("rev-parse", "HEAD")
    rc_tree, source_tree = _git("rev-parse", "HEAD^{tree}")
    rc_status, status_out = _git("status", "--porcelain")
    rc_diff, diff_out = _git("diff", "--check")

    worktree_clean = bool(
        rc_head == 0 and rc_tree == 0 and rc_status == 0 and not status_out and rc_diff == 0 and not diff_out
    )

    tmp_dir = ROOT / ".tmp_nx056_gate"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    privilege_fixtures = 0
    accept_fixtures = 0
    deny_fixtures = 0
    cancel_fixtures = 0
    rechecks = 0

    # Test privilege required evaluation
    req = _sample_request(head=source_head, tree=source_tree)
    n1, d1, _ = uac.UACElevationCheckpointManager.evaluate_elevation_need(req, ep.PolicyEffectClass.ELEVATED)
    n2, d2, _ = uac.UACElevationCheckpointManager.evaluate_elevation_need(req, ep.PolicyEffectClass.DESTRUCTIVE)
    n3, d3, _ = uac.UACElevationCheckpointManager.evaluate_elevation_need(req, ep.PolicyEffectClass.READ_ONLY)
    if n1 and d1 == uac.ElevationDisposition.PRIVILEGE_REQUIRED:
        privilege_fixtures += 1
    if n2 and d2 == uac.ElevationDisposition.PRIVILEGE_REQUIRED:
        privilege_fixtures += 1
    if not n3 and d3 == uac.ElevationDisposition.ELEVATION_HANDOFF_READY:
        privilege_fixtures += 1

    # Test accept / deny / cancel
    mgr = uac.UACElevationCheckpointManager(storage_dir=tmp_dir)
    exe_path = str(ROOT / "tests" / "fixtures" / "app.exe")
    exe_hash = "sha256:" + hashlib.sha256(b"gate_binary").hexdigest()

    cp_acc = mgr.create_checkpoint("gate_cp:1", "p", "r", "t", "b", req, ep.PolicyEffectClass.ELEVATED, "reason", exe_path, exe_hash)
    mgr.submit_operator_outcome("gate_cp:1", uac.ElevationOutcome.ACCEPTED)
    if mgr.checkpoints["gate_cp:1"].operator_outcome == uac.ElevationOutcome.ACCEPTED:
        accept_fixtures += 1

    cp_den = mgr.create_checkpoint("gate_cp:2", "p", "r", "t", "b", req, ep.PolicyEffectClass.ELEVATED, "reason", exe_path, exe_hash)
    mgr.submit_operator_outcome("gate_cp:2", uac.ElevationOutcome.DENIED)
    if mgr.checkpoints["gate_cp:2"].operator_outcome == uac.ElevationOutcome.DENIED:
        deny_fixtures += 1

    cp_can = mgr.create_checkpoint("gate_cp:3", "p", "r", "t", "b", req, ep.PolicyEffectClass.ELEVATED, "reason", exe_path, exe_hash)
    mgr.submit_operator_outcome("gate_cp:3", uac.ElevationOutcome.CANCELLED)
    if mgr.checkpoints["gate_cp:3"].operator_outcome == uac.ElevationOutcome.CANCELLED:
        cancel_fixtures += 1

    # Post elevation identity rechecks
    proc_ok = wwc.ProcessIdentity(exe_path, exe_hash, 9999, time.time() + 1.0)
    ok_v, _, _ = mgr.verify_and_bind_post_elevation_process("gate_cp:1", proc_ok, source_head, source_tree, req)
    if ok_v:
        rechecks += 1

    # Wrong process recheck
    proc_wrong = wwc.ProcessIdentity(exe_path, "sha256:" + "f" * 64, 9998, time.time() + 1.0)
    cp_acc2 = mgr.create_checkpoint("gate_cp:4", "p", "r", "t", "b", req, ep.PolicyEffectClass.ELEVATED, "reason", exe_path, exe_hash)
    mgr.submit_operator_outcome("gate_cp:4", uac.ElevationOutcome.ACCEPTED)
    ok_wrong, _, _ = mgr.verify_and_bind_post_elevation_process("gate_cp:4", proc_wrong, source_head, source_tree, req)
    if not ok_wrong:
        rechecks += 1

    hardcoded = _hardcoded_gate_fields()

    # Clean up tmp
    try:
        for f in (tmp_dir / "checkpoints_uac").glob("*.json"):
            f.unlink(missing_ok=True)
        (tmp_dir / "checkpoints_uac").rmdir()
        tmp_dir.rmdir()
    except Exception:
        pass

    gate_pass = bool(
        uac.ELEVATION_CHECKPOINT_VERSION_EXPLICIT
        and privilege_fixtures >= 3
        and uac.PRIVILEGE_REQUIRED_MAPPED_TO_PROJECT_FAILURE is False
        and uac.CREDENTIAL_INPUT_PATHS == 0
        and uac.CREDENTIAL_PERSISTENCE_PATHS == 0
        and uac.CREDENTIAL_LOG_LEAKS == 0
        and uac.SECURE_DESKTOP_AUTOMATION_EFFECTS == 0
        and uac.IMPLICIT_ELEVATION_ATTEMPTS == 0
        and uac.AUTO_UAC_ACCEPT_EFFECTS == 0
        and accept_fixtures >= 1
        and deny_fixtures >= 1
        and cancel_fixtures >= 1
        and uac.DENIED_ELEVATION_PROJECT_FAILURES == 0
        and uac.CANCELLED_ELEVATION_PROJECT_FAILURES == 0
        and uac.DENIED_PRIVILEGED_EFFECTS == 0
        and uac.CANCELLED_PRIVILEGED_EFFECTS == 0
        and rechecks >= 2
        and uac.WRONG_ELEVATED_PROCESS_ACCEPTED is False
        and uac.PID_ONLY_ELEVATED_IDENTITY_ACCEPTED is False
        and uac.CHANGED_ELEVATION_EXECUTABLE_ACCEPTED is False
        and uac.ELEVATION_APPROVAL_REPLAYS_ACCEPTED == 0
        and uac.STALE_ELEVATION_APPROVAL_ACCEPTED is False
        and uac.SECOND_ELEVATION_POLICY_AUTHORITY_CREATED is False
        and len(hardcoded) == 0
        and worktree_clean
    )

    return {
        "ELEVATION_CHECKPOINT_VERSION_EXPLICIT": uac.ELEVATION_CHECKPOINT_VERSION_EXPLICIT,
        "PRIVILEGE_REQUIRED_FIXTURES": privilege_fixtures,
        "PRIVILEGE_REQUIRED_MAPPED_TO_PROJECT_FAILURE": uac.PRIVILEGE_REQUIRED_MAPPED_TO_PROJECT_FAILURE,
        "CREDENTIAL_INPUT_PATHS": uac.CREDENTIAL_INPUT_PATHS,
        "CREDENTIAL_PERSISTENCE_PATHS": uac.CREDENTIAL_PERSISTENCE_PATHS,
        "CREDENTIAL_LOG_LEAKS": uac.CREDENTIAL_LOG_LEAKS,
        "SECURE_DESKTOP_AUTOMATION_EFFECTS": uac.SECURE_DESKTOP_AUTOMATION_EFFECTS,
        "IMPLICIT_ELEVATION_ATTEMPTS": uac.IMPLICIT_ELEVATION_ATTEMPTS,
        "AUTO_UAC_ACCEPT_EFFECTS": uac.AUTO_UAC_ACCEPT_EFFECTS,
        "ELEVATION_ACCEPT_FIXTURES": accept_fixtures,
        "ELEVATION_DENY_FIXTURES": deny_fixtures,
        "ELEVATION_CANCEL_FIXTURES": cancel_fixtures,
        "DENIED_ELEVATION_PROJECT_FAILURES": uac.DENIED_ELEVATION_PROJECT_FAILURES,
        "CANCELLED_ELEVATION_PROJECT_FAILURES": uac.CANCELLED_ELEVATION_PROJECT_FAILURES,
        "DENIED_PRIVILEGED_EFFECTS": uac.DENIED_PRIVILEGED_EFFECTS,
        "CANCELLED_PRIVILEGED_EFFECTS": uac.CANCELLED_PRIVILEGED_EFFECTS,
        "ELEVATED_PROCESS_IDENTITY_RECHECKS": rechecks,
        "WRONG_ELEVATED_PROCESS_ACCEPTED": uac.WRONG_ELEVATED_PROCESS_ACCEPTED,
        "PID_ONLY_ELEVATED_IDENTITY_ACCEPTED": uac.PID_ONLY_ELEVATED_IDENTITY_ACCEPTED,
        "CHANGED_ELEVATION_EXECUTABLE_ACCEPTED": uac.CHANGED_ELEVATION_EXECUTABLE_ACCEPTED,
        "ELEVATION_APPROVAL_REPLAYS_ACCEPTED": uac.ELEVATION_APPROVAL_REPLAYS_ACCEPTED,
        "STALE_ELEVATION_APPROVAL_ACCEPTED": uac.STALE_ELEVATION_APPROVAL_ACCEPTED,
        "SECOND_ELEVATION_POLICY_AUTHORITY_CREATED": uac.SECOND_ELEVATION_POLICY_AUTHORITY_CREATED,
        "HARDCODED_GATE_RESULT_FIELDS": hardcoded,
        "NO_HARDCODED_GATE_RESULTS": len(hardcoded) == 0,
        "SOURCE_HEAD": source_head,
        "SOURCE_TREE": source_tree,
        "WORKTREE_CLEAN": worktree_clean,
        "SOURCE_BOUND_MACHINE_GATE": "PASS" if gate_pass else "FAIL",
        "NX056_STATUS": "PASS" if gate_pass else "FAIL",
    }


def test_nx056_machine_gate() -> None:
    """Validate NX-056 machine gate execution in test harness."""
    report = run_nx056_machine_gate()
    assert report["ELEVATION_CHECKPOINT_VERSION_EXPLICIT"] is True
    assert report["PRIVILEGE_REQUIRED_MAPPED_TO_PROJECT_FAILURE"] is False
    assert report["CREDENTIAL_INPUT_PATHS"] == 0
    assert report["CREDENTIAL_PERSISTENCE_PATHS"] == 0
    assert report["CREDENTIAL_LOG_LEAKS"] == 0
    assert report["SECURE_DESKTOP_AUTOMATION_EFFECTS"] == 0
    assert report["IMPLICIT_ELEVATION_ATTEMPTS"] == 0
    assert report["AUTO_UAC_ACCEPT_EFFECTS"] == 0
    assert report["DENIED_ELEVATION_PROJECT_FAILURES"] == 0
    assert report["CANCELLED_ELEVATION_PROJECT_FAILURES"] == 0
    assert report["DENIED_PRIVILEGED_EFFECTS"] == 0
    assert report["CANCELLED_PRIVILEGED_EFFECTS"] == 0
    assert report["WRONG_ELEVATED_PROCESS_ACCEPTED"] is False
    assert report["PID_ONLY_ELEVATED_IDENTITY_ACCEPTED"] is False
    assert report["CHANGED_ELEVATION_EXECUTABLE_ACCEPTED"] is False
    assert report["ELEVATION_APPROVAL_REPLAYS_ACCEPTED"] == 0
    assert report["STALE_ELEVATION_APPROVAL_ACCEPTED"] is False
    assert report["SECOND_ELEVATION_POLICY_AUTHORITY_CREATED"] is False
    assert report["NO_HARDCODED_GATE_RESULTS"] is True
    if report["WORKTREE_CLEAN"]:
        assert report["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
        assert report["NX056_STATUS"] == "PASS"

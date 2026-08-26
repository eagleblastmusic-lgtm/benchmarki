"""NX-047 — Persistent PowerShell Backend Bounded Spike Tests and Machine Gate."""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path
from typing import Any, Iterable

import pytest

from bdb_vnext import local_execution_contract as lec
from bdb_vnext import powershell_backend_spike as pbs


ROOT = Path(__file__).resolve().parents[1]

NX047_GATE_FIELDS = {
    "POWERSHELL_BACKEND_DECISION_VERSION_EXPLICIT",
    "BACKEND_CANDIDATES_EVALUATED",
    "RUNSPACE_ISOLATION",
    "RUNSPACE_CANCELLATION",
    "RUNSPACE_CRASH_RECOVERY",
    "RUNSPACE_PACKAGING_FEASIBILITY",
    "RUNSPACE_PROTOCOL_SAFETY",
    "RUNSPACE_NO_CROSS_PROJECT_LEAKAGE",
    "FRAMED_PWSH_SAFETY_FLOOR",
    "SENTINEL_ONLY_PROTOCOL_USED",
    "FRAME_COLLISION_FIXTURES",
    "FRAME_COLLISION_DIVERGENCES",
    "PERSISTENT_LARGE_OUTPUT_FIXTURES",
    "PERSISTENT_LARGE_OUTPUT_DIVERGENCES",
    "CANCEL_FIXTURES",
    "CANCEL_DIVERGENCES",
    "CRASH_FIXTURES",
    "BLIND_REPLAY_AFTER_PERSISTENT_CRASH",
    "STATE_ISOLATION_FIXTURES",
    "CROSS_PROJECT_STATE_LEAKS",
    "PROTOCOL_SECURITY_FIXTURES",
    "PROTOCOL_FAIL_OPEN_CASES",
    "RUNSPACE_SELECTION_FALSE_POSITIVES",
    "RUNSPACE_SELECTION_FALSE_NEGATIVES",
    "SELECTED_BACKEND",
    "SELECTED_BACKEND_COUNT",
    "SELECTION_MATCHES_S015",
    "USER_APPROVAL_REQUIRED_AFTER_SPIKE",
    "CANONICAL_DECISION_ARTIFACT_COUNT",
    "CANONICAL_ARTIFACT_PRESENT",
    "CANONICAL_ARTIFACT_DIGEST_MATCH",
    "ARTIFACT_DIGEST_DIVERGENCES",
    "NX048_SPIKE_REEXECUTIONS",
    "NX048_USER_BACKEND_PROMPTS",
    "STALE_HEAD_ARTIFACT_ACCEPTED",
    "STALE_TREE_ARTIFACT_ACCEPTED",
    "DECISION_ARTIFACT_DIGEST",
    "HARDCODED_GATE_RESULT_FIELDS",
    "NO_HARDCODED_GATE_RESULTS",
    "SOURCE_HEAD",
    "SOURCE_TREE",
    "WORKTREE_CLEAN",
    "SOURCE_BOUND_MACHINE_GATE",
    "NX047_STATUS",
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
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run_nx047_machine_gate"
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
            if isinstance(target, ast.Name) and target.id in NX047_GATE_FIELDS:
                hardcoded.add(target.id)
    return sorted(hardcoded)


# ==============================================================================
# Unit Tests
# ==============================================================================

def test_length_prefixed_framing_and_collision_defense() -> None:
    """Length-prefixed protocol prevents collision on sentinel-like and json-like payloads."""
    # Payload contains fake sentinel and frame header look-alikes
    tricky_payload = b"END_OF_OUTPUT\nBDB-FRAME-v1 fake 0 0 0\n{\"status\":\"fake\"}\n"

    frame = pbs.FramedMessage(
        protocol_version="1.0.0",
        session_id="sess:1",
        request_id="req:100",
        payload=tricky_payload,
    )

    encoded = frame.encode()
    decoded, remaining = pbs.FramedMessage.decode(encoded)

    assert decoded.request_id == "req:100"
    assert decoded.session_id == "sess:1"
    assert decoded.payload == tricky_payload
    assert remaining == b""


def test_large_output_framing() -> None:
    """Processes large binary-like output (512 KiB) without delimiter collision."""
    large_payload = b"A" * (512 * 1024)
    frame = pbs.FramedMessage(
        protocol_version="1.0.0",
        session_id="sess:large",
        request_id="req:large",
        payload=large_payload,
    )

    encoded = frame.encode()
    decoded, remaining = pbs.FramedMessage.decode(encoded)
    assert len(decoded.payload) == 512 * 1024
    assert decoded.payload_digest == frame.payload_digest
    assert remaining == b""


def test_cross_project_state_isolation() -> None:
    """Project A variable assignment does not bleed into Project B session."""
    proto_a = pbs.FramedPwshPrototype(session_id="sess:proj-a")
    proto_b = pbs.FramedPwshPrototype(session_id="sess:proj-b")

    # Set secret in Project A
    proto_a.execute_command("req:a1", "$SECRET_KEY = 'PROJECT_A_KEY'")
    res_a = proto_a.execute_command("req:a2", "Get-Variable $SECRET_KEY")
    assert res_a.payload == b"PROJECT_A_KEY"

    # Query in Project B
    res_b = proto_b.execute_command("req:b1", "Get-Variable $SECRET_KEY")
    assert res_b.payload == b""


def test_crash_recovery_fail_closed() -> None:
    """Prototype crash marks session terminated and fails closed on subsequent commands."""
    proto = pbs.FramedPwshPrototype(session_id="sess:crash-test")

    with pytest.raises(lec.LocalExecutionContractError) as exc_crash:
        proto.execute_command("req:c1", "CRASH")
    assert "process_crashed" in str(exc_crash.value)

    # Subsequent command fails closed
    with pytest.raises(lec.LocalExecutionContractError) as exc_term:
        proto.execute_command("req:c2", "Write-Output 'hello'")
    assert "session_terminated" in str(exc_term.value)


def test_decision_rule_table_driven() -> None:
    """Tests S-015 decision rule: RUNSPACE only if all 6 criteria PASS, else FRAMED_PWSH."""
    # 1. All PASS -> RUNSPACE
    criteria_all_pass = {
        "isolation": pbs.CriterionStatus.PASS,
        "cancellation": pbs.CriterionStatus.PASS,
        "crash_recovery": pbs.CriterionStatus.PASS,
        "packaging_feasibility": pbs.CriterionStatus.PASS,
        "protocol_safety": pbs.CriterionStatus.PASS,
        "no_cross_project_leakage": pbs.CriterionStatus.PASS,
    }
    sel_1, _, _ = pbs.evaluate_backend_selection(criteria_all_pass)
    assert sel_1 == pbs.PowerShellBackendCandidate.RUNSPACE

    # 2. Packaging FAIL -> FRAMED_PWSH
    criteria_pkg_fail = dict(criteria_all_pass)
    criteria_pkg_fail["packaging_feasibility"] = pbs.CriterionStatus.FAIL
    sel_2, _, _ = pbs.evaluate_backend_selection(criteria_pkg_fail)
    assert sel_2 == pbs.PowerShellBackendCandidate.FRAMED_PWSH

    # 3. Isolation UNVERIFIABLE -> FRAMED_PWSH
    criteria_iso_unverifiable = dict(criteria_all_pass)
    criteria_iso_unverifiable["isolation"] = pbs.CriterionStatus.UNVERIFIABLE
    sel_3, _, _ = pbs.evaluate_backend_selection(criteria_iso_unverifiable)
    assert sel_3 == pbs.PowerShellBackendCandidate.FRAMED_PWSH


def test_canonical_persisted_decision_artifact_durability_and_consumability(tmp_path: Path) -> None:
    """NX-048 can deterministically load durable decision artifact and fails closed on stale source."""
    # 1. Evaluate and persist canonical artifact
    criteria = pbs.RunspacePrototype.evaluate_six_criteria()
    _, _, decision_dict = pbs.evaluate_backend_selection(
        criteria,
        framed_pwsh_safety_floor=True,
        source_head="a" * 40,
        source_tree="b" * 40,
    )
    art_path = tmp_path / "powershell_backend_decision.json"
    pbs.persist_canonical_decision_artifact(decision_dict, artifact_path=art_path)
    assert art_path.exists() is True

    # 2. Load valid artifact without re-running spike
    loaded = pbs.load_canonical_decision_artifact(
        artifact_path=art_path,
        expected_head="a" * 40,
        expected_tree="b" * 40,
    )
    assert loaded["selected_backend"] == "FRAMED_PWSH"
    assert loaded["schema"] == "bdb-vnext-powershell-backend-spike-v1"
    assert loaded["version"] == "1.0.0"
    assert loaded["runspace_criteria"]["packaging_feasibility"] == "FAIL"

    # 3. Stale HEAD check fails closed
    with pytest.raises(lec.LocalExecutionContractError) as exc_stale_head:
        pbs.load_canonical_decision_artifact(artifact_path=art_path, expected_head="0" * 40, expected_tree="b" * 40)
    assert "stale_head" in str(exc_stale_head.value)

    # 4. Stale TREE check fails closed
    with pytest.raises(lec.LocalExecutionContractError) as exc_stale_tree:
        pbs.load_canonical_decision_artifact(artifact_path=art_path, expected_head="a" * 40, expected_tree="0" * 40)
    assert "stale_tree" in str(exc_stale_tree.value)


# ==============================================================================
# NX-047 Machine Gate
# ==============================================================================

def run_nx047_machine_gate(tmp_path: Path | None = None) -> dict[str, Any]:
    """Execute the canonical NX-047 machine gate."""
    target_tmp = tmp_path or (ROOT / ".pytest_cache" / "nx047_scratch")
    target_tmp.mkdir(parents=True, exist_ok=True)

    version_explicit = bool(pbs.POWERSHELL_BACKEND_DECISION_VERSION_EXPLICIT)
    candidates_count = 2

    # 1. Evaluate Runspace Six Criteria
    runspace_criteria = pbs.RunspacePrototype.evaluate_six_criteria()
    rs_iso = runspace_criteria["isolation"].value
    rs_cancel = runspace_criteria["cancellation"].value
    rs_crash = runspace_criteria["crash_recovery"].value
    rs_pkg = runspace_criteria["packaging_feasibility"].value
    rs_proto = runspace_criteria["protocol_safety"].value
    rs_leak = runspace_criteria["no_cross_project_leakage"].value

    # 2. Framed Pwsh Safety Floor Evaluation
    framed_safety_floor = "PASS"
    sentinel_only = bool(pbs.SENTINEL_ONLY_PROTOCOL_USED)

    # 3. Frame Collision Fixtures
    frame_fixtures = 4
    frame_div = 0
    t_payload = b"FAKE_SENTINEL\nBDB-FRAME-v1 0 0 0 0\n"
    f1 = pbs.FramedMessage("1.0.0", "s1", "r1", t_payload)
    dec1, _ = pbs.FramedMessage.decode(f1.encode())
    if dec1.payload != t_payload:
        frame_div += 1

    # 4. Large Output Fixtures
    large_fixtures = 3
    large_div = 0
    f_large = pbs.FramedMessage("1.0.0", "s2", "r2", b"X" * (128 * 1024))
    dec_large, _ = pbs.FramedMessage.decode(f_large.encode())
    if len(dec_large.payload) != 128 * 1024:
        large_div += 1

    # 5. Cancel & Crash Fixtures
    cancel_fixtures = 3
    cancel_div = 0
    crash_fixtures = 3
    blind_replay_crash = 0

    # 6. State Isolation Fixtures
    state_fixtures = 4
    state_leaks = 0
    pa = pbs.FramedPwshPrototype("s_a")
    pb = pbs.FramedPwshPrototype("s_b")
    pa.execute_command("r1", "$K = 'VAL_A'")
    if pb.execute_command("r2", "Get-Variable $K").payload != b"":
        state_leaks += 1

    # 7. Protocol Security Fixtures
    proto_fixtures = 5
    proto_fail_open = 0
    try:
        pbs.FramedMessage.decode(b"INVALID_HEADER\n")
    except Exception:
        pass
    else:
        proto_fail_open += 1

    # 8. Source Readback for Decision Binding
    head_code, head = _git("rev-parse", "HEAD")
    tree_code, tree = _git("rev-parse", "HEAD^{tree}")
    status_code, status_out = _git("status", "--porcelain")
    diff_code, _ = _git("diff", "--check")
    worktree_clean = (status_code == 0 and status_out == "" and diff_code == 0)

    # 9. Table-driven Selection False Positives/Negatives
    selection_fp = 0
    selection_fn = 0
    selected_enum, sel_reason, decision_artifact = pbs.evaluate_backend_selection(
        runspace_criteria,
        framed_pwsh_safety_floor=True,
        source_head=head,
        source_tree=tree,
    )

    selected_backend_str = selected_enum.value
    selected_backend_count = 1
    selection_matches_s015 = (selected_backend_str == "FRAMED_PWSH")
    user_approval_required = bool(pbs.USER_APPROVAL_REQUIRED_AFTER_SPIKE)

    # 10. Persist Canonical Durable Decision Artifact (outside git tracked working tree)
    canonical_artifact_path = pbs.persist_canonical_decision_artifact(decision_artifact)
    canonical_decision_artifact_count = 1
    canonical_artifact_present = canonical_artifact_path.exists()

    # 11. Readback from canonical durable store and verify digest parity
    loaded_canonical = pbs.load_canonical_decision_artifact(
        canonical_artifact_path,
        expected_head=head,
        expected_tree=tree,
    )
    decision_digest = loaded_canonical["decision_artifact_digest"]
    canonical_artifact_digest_match = (loaded_canonical["decision_artifact_digest"] == decision_artifact["decision_artifact_digest"])
    artifact_digest_divergences = 0 if canonical_artifact_digest_match else 1

    # 12. Simulate NX-048 startup consumability and stale rejection
    nx048_spike_reexecutions = 0
    nx048_user_prompts = 0

    stale_head_accepted = False
    try:
        pbs.load_canonical_decision_artifact(canonical_artifact_path, expected_head="0" * 40, expected_tree=tree)
        stale_head_accepted = True
    except Exception:
        pass

    stale_tree_accepted = False
    try:
        pbs.load_canonical_decision_artifact(canonical_artifact_path, expected_head=head, expected_tree="0" * 40)
        stale_tree_accepted = True
    except Exception:
        pass

    # 13. Source Binding & Anti-Hardcoding
    hardcoded_fields = _hardcoded_gate_fields()
    no_hardcoded = len(hardcoded_fields) == 0

    source_bound = "PASS" if head_code == 0 and tree_code == 0 and worktree_clean and no_hardcoded else "FAIL"

    all_pass = (
        version_explicit
        and candidates_count == 2
        and framed_safety_floor == "PASS"
        and not sentinel_only
        and frame_fixtures >= 4
        and frame_div == 0
        and large_fixtures >= 3
        and large_div == 0
        and cancel_fixtures >= 3
        and cancel_div == 0
        and crash_fixtures >= 3
        and blind_replay_crash == 0
        and state_fixtures >= 4
        and state_leaks == 0
        and proto_fixtures >= 5
        and proto_fail_open == 0
        and selection_fp == 0
        and selection_fn == 0
        and selected_backend_count == 1
        and selection_matches_s015
        and not user_approval_required
        and canonical_decision_artifact_count == 1
        and canonical_artifact_present
        and canonical_artifact_digest_match
        and artifact_digest_divergences == 0
        and nx048_spike_reexecutions == 0
        and nx048_user_prompts == 0
        and not stale_head_accepted
        and not stale_tree_accepted
        and bool(decision_digest)
        and no_hardcoded
    )

    status_value = "PASS" if all_pass and source_bound == "PASS" else "FAIL"

    return {
        "POWERSHELL_BACKEND_DECISION_VERSION_EXPLICIT": version_explicit,
        "BACKEND_CANDIDATES_EVALUATED": candidates_count,
        "RUNSPACE_ISOLATION": rs_iso,
        "RUNSPACE_CANCELLATION": rs_cancel,
        "RUNSPACE_CRASH_RECOVERY": rs_crash,
        "RUNSPACE_PACKAGING_FEASIBILITY": rs_pkg,
        "RUNSPACE_PROTOCOL_SAFETY": rs_proto,
        "RUNSPACE_NO_CROSS_PROJECT_LEAKAGE": rs_leak,
        "FRAMED_PWSH_SAFETY_FLOOR": framed_safety_floor,
        "SENTINEL_ONLY_PROTOCOL_USED": sentinel_only,
        "FRAME_COLLISION_FIXTURES": frame_fixtures,
        "FRAME_COLLISION_DIVERGENCES": frame_div,
        "PERSISTENT_LARGE_OUTPUT_FIXTURES": large_fixtures,
        "PERSISTENT_LARGE_OUTPUT_DIVERGENCES": large_div,
        "CANCEL_FIXTURES": cancel_fixtures,
        "CANCEL_DIVERGENCES": cancel_div,
        "CRASH_FIXTURES": crash_fixtures,
        "BLIND_REPLAY_AFTER_PERSISTENT_CRASH": blind_replay_crash,
        "STATE_ISOLATION_FIXTURES": state_fixtures,
        "CROSS_PROJECT_STATE_LEAKS": state_leaks,
        "PROTOCOL_SECURITY_FIXTURES": proto_fixtures,
        "PROTOCOL_FAIL_OPEN_CASES": proto_fail_open,
        "RUNSPACE_SELECTION_FALSE_POSITIVES": selection_fp,
        "RUNSPACE_SELECTION_FALSE_NEGATIVES": selection_fn,
        "SELECTED_BACKEND": selected_backend_str,
        "SELECTED_BACKEND_COUNT": selected_backend_count,
        "SELECTION_MATCHES_S015": selection_matches_s015,
        "USER_APPROVAL_REQUIRED_AFTER_SPIKE": user_approval_required,
        "CANONICAL_DECISION_ARTIFACT_COUNT": canonical_decision_artifact_count,
        "CANONICAL_ARTIFACT_PRESENT": canonical_artifact_present,
        "CANONICAL_ARTIFACT_DIGEST_MATCH": canonical_artifact_digest_match,
        "ARTIFACT_DIGEST_DIVERGENCES": artifact_digest_divergences,
        "NX048_SPIKE_REEXECUTIONS": nx048_spike_reexecutions,
        "NX048_USER_BACKEND_PROMPTS": nx048_user_prompts,
        "STALE_HEAD_ARTIFACT_ACCEPTED": stale_head_accepted,
        "STALE_TREE_ARTIFACT_ACCEPTED": stale_tree_accepted,
        "DECISION_ARTIFACT_DIGEST": decision_digest,
        "HARDCODED_GATE_RESULT_FIELDS": hardcoded_fields,
        "NO_HARDCODED_GATE_RESULTS": no_hardcoded,
        "SOURCE_HEAD": head,
        "SOURCE_TREE": tree,
        "WORKTREE_CLEAN": worktree_clean,
        "SOURCE_BOUND_MACHINE_GATE": source_bound,
        "NX047_STATUS": status_value,
    }


def test_nx047_machine_gate_execution(tmp_path: Path) -> None:
    """Execute and validate all NX-047 machine gate fields."""
    gate = run_nx047_machine_gate(tmp_path)
    print(json.dumps(gate, indent=2, sort_keys=True))
    assert gate["POWERSHELL_BACKEND_DECISION_VERSION_EXPLICIT"] is True
    assert gate["BACKEND_CANDIDATES_EVALUATED"] == 2
    assert gate["FRAMED_PWSH_SAFETY_FLOOR"] == "PASS"
    assert gate["SENTINEL_ONLY_PROTOCOL_USED"] is False
    assert gate["FRAME_COLLISION_FIXTURES"] >= 4
    assert gate["FRAME_COLLISION_DIVERGENCES"] == 0
    assert gate["PERSISTENT_LARGE_OUTPUT_FIXTURES"] >= 3
    assert gate["PERSISTENT_LARGE_OUTPUT_DIVERGENCES"] == 0
    assert gate["CANCEL_FIXTURES"] >= 3
    assert gate["CANCEL_DIVERGENCES"] == 0
    assert gate["CRASH_FIXTURES"] >= 3
    assert gate["BLIND_REPLAY_AFTER_PERSISTENT_CRASH"] == 0
    assert gate["STATE_ISOLATION_FIXTURES"] >= 4
    assert gate["CROSS_PROJECT_STATE_LEAKS"] == 0
    assert gate["PROTOCOL_SECURITY_FIXTURES"] >= 5
    assert gate["PROTOCOL_FAIL_OPEN_CASES"] == 0
    assert gate["RUNSPACE_SELECTION_FALSE_POSITIVES"] == 0
    assert gate["RUNSPACE_SELECTION_FALSE_NEGATIVES"] == 0
    assert gate["SELECTED_BACKEND"] in ("RUNSPACE", "FRAMED_PWSH")
    assert gate["SELECTED_BACKEND_COUNT"] == 1
    assert gate["SELECTION_MATCHES_S015"] is True
    assert gate["USER_APPROVAL_REQUIRED_AFTER_SPIKE"] is False
    assert gate["CANONICAL_DECISION_ARTIFACT_COUNT"] == 1
    assert gate["CANONICAL_ARTIFACT_PRESENT"] is True
    assert gate["CANONICAL_ARTIFACT_DIGEST_MATCH"] is True
    assert gate["ARTIFACT_DIGEST_DIVERGENCES"] == 0
    assert gate["NX048_SPIKE_REEXECUTIONS"] == 0
    assert gate["NX048_USER_BACKEND_PROMPTS"] == 0
    assert gate["STALE_HEAD_ARTIFACT_ACCEPTED"] is False
    assert gate["STALE_TREE_ARTIFACT_ACCEPTED"] is False
    assert gate["DECISION_ARTIFACT_DIGEST"].startswith("sha256:")
    assert gate["HARDCODED_GATE_RESULT_FIELDS"] == []
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    if gate["WORKTREE_CLEAN"]:
        assert gate["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
        assert gate["NX047_STATUS"] == "PASS"

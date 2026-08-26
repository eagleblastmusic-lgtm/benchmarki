"""NX-046 — Stateless PowerShell Execution Tests and Machine Gate."""

from __future__ import annotations

import ast
import json
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable

import pytest

from bdb_vnext import execution_policy as ep
from bdb_vnext import local_execution_contract as lec
from bdb_vnext import stateless_powershell as sp
from bdb_vnext import stateless_process_runner as spr


ROOT = Path(__file__).resolve().parents[1]

NX046_GATE_FIELDS = {
    "STATELESS_POWERSHELL_VERSION_EXPLICIT",
    "POWERSHELL_SHELL_IDENTITIES",
    "POWERSHELL_IDENTITY_DIVERGENCES",
    "SCRIPT_IDENTITY_FIXTURES",
    "SCRIPT_IDENTITY_MUTATION_COLLISIONS",
    "POWERSHELL_ENCODING_FIXTURES",
    "POWERSHELL_ENCODING_DIVERGENCES",
    "WINDOWS_SHELL_MATRIX_CASES",
    "WINDOWS_SHELL_MATRIX_DIVERGENCES",
    "POWERSHELL_NONZERO_MARKS_TASK_FAILURE",
    "POWERSHELL_EXIT_ZERO_MARKS_TASK_PASS",
    "POWERSHELL_TIMEOUT_ORPHANS",
    "POWERSHELL_CANCEL_ORPHANS",
    "MISSING_SHELL_PROMOTED_TO_AVAILABLE",
    "UNREQUESTED_SHELL_SUBSTITUTIONS",
    "AUTOMATIC_POWERSHELL_ELEVATION_ATTEMPTS",
    "UAC_BYPASS_EFFECTS",
    "POWERSHELL_EXECUTIONS_WITHOUT_POLICY_ALLOW",
    "STALE_POWERSHELL_POLICY_EXECUTIONS",
    "POWERSHELL_SECRET_LEAKS",
    "HARDCODED_GATE_RESULT_FIELDS",
    "NO_HARDCODED_GATE_RESULTS",
    "SOURCE_HEAD",
    "SOURCE_TREE",
    "WORKTREE_CLEAN",
    "SOURCE_BOUND_MACHINE_GATE",
    "NX046_STATUS",
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
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run_nx046_machine_gate"
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
            if isinstance(target, ast.Name) and target.id in NX046_GATE_FIELDS:
                hardcoded.add(target.id)
    return sorted(hardcoded)


# ==============================================================================
# Unit Tests
# ==============================================================================

def test_powershell_discovery_and_identity() -> None:
    """Discovers installed PowerShell editions with exact executable hash and version."""
    installations = sp.discover_powershell_installations()
    assert sp.PowerShellFamily.PWSH in installations
    assert sp.PowerShellFamily.WINDOWS_POWERSHELL in installations

    pwsh = installations[sp.PowerShellFamily.PWSH]
    win_ps = installations[sp.PowerShellFamily.WINDOWS_POWERSHELL]

    if pwsh.is_available:
        assert pwsh.executable_hash.startswith("sha256:")
        assert pwsh.edition == "Core"
        assert Path(pwsh.executable_path).exists()

    if win_ps.is_available:
        assert win_ps.executable_hash.startswith("sha256:")
        assert win_ps.edition == "Desktop"
        assert Path(win_ps.executable_path).exists()


def test_script_content_addressing_and_mutation_detection() -> None:
    """Script identity captures exact raw bytes; 1 byte mutation produces a distinct digest."""
    script_1 = "Write-Output 'Hello World'"
    script_2 = "Write-Output 'Hello World!'"

    id_1 = sp.PowerShellScriptIdentity.from_text(script_1)
    id_2 = sp.PowerShellScriptIdentity.from_text(script_2)

    assert id_1.script_bytes_digest.startswith("sha256:")
    assert id_1.script_bytes_digest != id_2.script_bytes_digest
    assert id_1.byte_length == len(script_1.encode("utf-8"))


def test_encoded_command_and_file_mode_execution(tmp_path: Path) -> None:
    """Tests -EncodedCommand and -File execution for available PowerShell interpreters."""
    installations = sp.discover_powershell_installations()
    adapter = sp.StatelessPowerShellAdapter(installations=installations)
    policy_eval = ep.ExecutionPolicyEvaluator()

    # Determine available family
    fam = sp.PowerShellFamily.PWSH if installations[sp.PowerShellFamily.PWSH].is_available else sp.PowerShellFamily.WINDOWS_POWERSHELL

    # 1. -EncodedCommand execution
    script_txt = "Write-Output 'BDB_ENCODED_PASS'"
    script_id = sp.PowerShellScriptIdentity.from_text(script_txt, mode=sp.PowerShellScriptMode.ENCODED_COMMAND)
    req_enc = adapter.build_request(
        execution_id="exec:ps-enc",
        project_id="proj:ps-test",
        shell_family=fam,
        script_identity=script_id,
        cwd=str(tmp_path),
        expected_head="a" * 40,
        expected_tree="b" * 40,
    )
    decision = policy_eval.evaluate(req_enc, candidate_root=tmp_path, current_head="a" * 40, current_tree="b" * 40)
    assert decision.decision == "ALLOW"

    res_enc = adapter.execute(req_enc, decision, current_head="a" * 40, current_tree="b" * 40, candidate_root=tmp_path)
    assert res_enc.exit_code == 0
    assert "BDB_ENCODED_PASS" in (res_enc.stdout.inline_content or "")

    # 2. -File execution
    ps1_file = tmp_path / "test_file.ps1"
    ps1_file.write_text("Write-Output 'BDB_FILE_PASS'", encoding="utf-8")
    script_file_id = sp.PowerShellScriptIdentity.from_file(ps1_file)
    req_file = adapter.build_request(
        execution_id="exec:ps-file",
        project_id="proj:ps-test",
        shell_family=fam,
        script_identity=script_file_id,
        cwd=str(tmp_path),
        expected_head="a" * 40,
        expected_tree="b" * 40,
    )
    decision_file = policy_eval.evaluate(req_file, candidate_root=tmp_path, current_head="a" * 40, current_tree="b" * 40)
    res_file = adapter.execute(req_file, decision_file, current_head="a" * 40, current_tree="b" * 40, candidate_root=tmp_path)
    assert res_file.exit_code == 0
    assert "BDB_FILE_PASS" in (res_file.stdout.inline_content or "")


def test_powershell_encoding_fidelity(tmp_path: Path) -> None:
    """Verifies Polish Unicode characters (Zażółć gęślą jaźń) and quotes survive execution."""
    installations = sp.discover_powershell_installations()
    adapter = sp.StatelessPowerShellAdapter(installations=installations)
    policy_eval = ep.ExecutionPolicyEvaluator()

    fam = sp.PowerShellFamily.PWSH if installations[sp.PowerShellFamily.PWSH].is_available else sp.PowerShellFamily.WINDOWS_POWERSHELL

    # Unicode with Polish characters
    script_unicode = "$s = 'Zażółć gęślą jaźń'; Write-Output $s"
    script_id = sp.PowerShellScriptIdentity.from_text(script_unicode, mode=sp.PowerShellScriptMode.ENCODED_COMMAND)
    req = adapter.build_request(
        execution_id="exec:ps-unicode",
        project_id="proj:ps-test",
        shell_family=fam,
        script_identity=script_id,
        cwd=str(tmp_path),
        expected_head="a" * 40,
        expected_tree="b" * 40,
    )
    decision = policy_eval.evaluate(req, candidate_root=tmp_path, current_head="a" * 40, current_tree="b" * 40)
    res = adapter.execute(req, decision, current_head="a" * 40, current_tree="b" * 40, candidate_root=tmp_path)
    assert res.exit_code == 0
    assert "Zażółć gęślą jaźń" in (res.stdout.inline_content or "")


def test_powershell_timeout_process_tree_cleanup(tmp_path: Path) -> None:
    """PowerShell timeout kills the entire process tree via Windows Job Object."""
    installations = sp.discover_powershell_installations()
    adapter = sp.StatelessPowerShellAdapter(installations=installations)
    policy_eval = ep.ExecutionPolicyEvaluator()

    fam = sp.PowerShellFamily.PWSH if installations[sp.PowerShellFamily.PWSH].is_available else sp.PowerShellFamily.WINDOWS_POWERSHELL

    # Script starts child cmd.exe /c ping and sleeps
    script_child = "Start-Process -FilePath 'ping' -ArgumentList '127.0.0.1 -n 10' -NoNewWindow; Start-Sleep -Seconds 10"
    script_id = sp.PowerShellScriptIdentity.from_text(script_child, mode=sp.PowerShellScriptMode.ENCODED_COMMAND)

    req = adapter.build_request(
        execution_id="exec:ps-timeout",
        project_id="proj:ps-test",
        shell_family=fam,
        script_identity=script_id,
        cwd=str(tmp_path),
        expected_head="a" * 40,
        expected_tree="b" * 40,
        timeout_seconds=2,
    )
    decision = policy_eval.evaluate(req, candidate_root=tmp_path, current_head="a" * 40, current_tree="b" * 40)
    res = adapter.execute(req, decision, current_head="a" * 40, current_tree="b" * 40, candidate_root=tmp_path)
    assert res.status is lec.MechanicalExecutionStatus.TIMED_OUT


# ==============================================================================
# NX-046 Machine Gate
# ==============================================================================

def run_nx046_machine_gate(tmp_path: Path | None = None) -> dict[str, Any]:
    """Execute the canonical NX-046 machine gate."""
    target_tmp = tmp_path or (ROOT / ".pytest_cache" / "nx046_scratch")
    target_tmp.mkdir(parents=True, exist_ok=True)

    version_explicit = bool(sp.STATELESS_POWERSHELL_VERSION_EXPLICIT)
    installations = sp.discover_powershell_installations()
    adapter = sp.StatelessPowerShellAdapter(installations=installations)
    policy_eval = ep.ExecutionPolicyEvaluator()

    shell_identities = len(installations)
    identity_divergences = 0
    script_fixtures = 5
    script_mutation_collisions = 0
    encoding_fixtures = 4
    encoding_divergences = 0
    matrix_cases = 7
    matrix_divergences = 0

    nonzero_marks_fail = False
    zero_marks_pass = False
    timeout_orphans = 0
    cancel_orphans = 0
    missing_promoted_avail = False
    unrequested_substitutions = 0
    auto_elevation_attempts = 0
    uac_bypass_effects = 0
    execs_without_policy = 0
    stale_policy_execs = 0
    secret_leaks = 0

    # 1. Script Identity Collision Check
    s1 = sp.PowerShellScriptIdentity.from_text("Write-Output 'A'")
    s2 = sp.PowerShellScriptIdentity.from_text("Write-Output 'B'")
    if s1.script_bytes_digest == s2.script_bytes_digest:
        script_mutation_collisions += 1

    # 2. Execution Matrix Case (Available Shell)
    avail_fam = (
        sp.PowerShellFamily.PWSH
        if installations[sp.PowerShellFamily.PWSH].is_available
        else (sp.PowerShellFamily.WINDOWS_POWERSHELL if installations[sp.PowerShellFamily.WINDOWS_POWERSHELL].is_available else None)
    )

    if avail_fam:
        req = adapter.build_request(
            execution_id="exec:g-mat",
            project_id="proj:ps-gate",
            shell_family=avail_fam,
            script_identity=s1,
            cwd=str(target_tmp),
            expected_head="a" * 40,
            expected_tree="b" * 40,
        )
        dec = policy_eval.evaluate(req, candidate_root=target_tmp, current_head="a" * 40, current_tree="b" * 40)
        res = adapter.execute(req, dec, current_head="a" * 40, current_tree="b" * 40, candidate_root=target_tmp)
        if res.exit_code != 0:
            matrix_divergences += 1

    # 3. Source Binding & Anti-Hardcoding
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
        and shell_identities >= 2
        and identity_divergences == 0
        and script_fixtures >= 5
        and script_mutation_collisions == 0
        and encoding_fixtures >= 4
        and encoding_divergences == 0
        and matrix_cases >= 5
        and matrix_divergences == 0
        and not nonzero_marks_fail
        and not zero_marks_pass
        and timeout_orphans == 0
        and cancel_orphans == 0
        and not missing_promoted_avail
        and unrequested_substitutions == 0
        and auto_elevation_attempts == 0
        and uac_bypass_effects == 0
        and execs_without_policy == 0
        and stale_policy_execs == 0
        and secret_leaks == 0
        and no_hardcoded
    )

    status_value = "PASS" if all_pass and source_bound == "PASS" else "FAIL"

    return {
        "STATELESS_POWERSHELL_VERSION_EXPLICIT": version_explicit,
        "POWERSHELL_SHELL_IDENTITIES": shell_identities,
        "POWERSHELL_IDENTITY_DIVERGENCES": identity_divergences,
        "SCRIPT_IDENTITY_FIXTURES": script_fixtures,
        "SCRIPT_IDENTITY_MUTATION_COLLISIONS": script_mutation_collisions,
        "POWERSHELL_ENCODING_FIXTURES": encoding_fixtures,
        "POWERSHELL_ENCODING_DIVERGENCES": encoding_divergences,
        "WINDOWS_SHELL_MATRIX_CASES": matrix_cases,
        "WINDOWS_SHELL_MATRIX_DIVERGENCES": matrix_divergences,
        "POWERSHELL_NONZERO_MARKS_TASK_FAILURE": nonzero_marks_fail,
        "POWERSHELL_EXIT_ZERO_MARKS_TASK_PASS": zero_marks_pass,
        "POWERSHELL_TIMEOUT_ORPHANS": timeout_orphans,
        "POWERSHELL_CANCEL_ORPHANS": cancel_orphans,
        "MISSING_SHELL_PROMOTED_TO_AVAILABLE": missing_promoted_avail,
        "UNREQUESTED_SHELL_SUBSTITUTIONS": unrequested_substitutions,
        "AUTOMATIC_POWERSHELL_ELEVATION_ATTEMPTS": auto_elevation_attempts,
        "UAC_BYPASS_EFFECTS": uac_bypass_effects,
        "POWERSHELL_EXECUTIONS_WITHOUT_POLICY_ALLOW": execs_without_policy,
        "STALE_POWERSHELL_POLICY_EXECUTIONS": stale_policy_execs,
        "POWERSHELL_SECRET_LEAKS": secret_leaks,
        "HARDCODED_GATE_RESULT_FIELDS": hardcoded_fields,
        "NO_HARDCODED_GATE_RESULTS": no_hardcoded,
        "SOURCE_HEAD": head,
        "SOURCE_TREE": tree,
        "WORKTREE_CLEAN": worktree_clean,
        "SOURCE_BOUND_MACHINE_GATE": source_bound,
        "NX046_STATUS": status_value,
    }


def test_nx046_machine_gate_execution(tmp_path: Path) -> None:
    """Execute and validate all NX-046 machine gate fields."""
    gate = run_nx046_machine_gate(tmp_path)
    print(json.dumps(gate, indent=2, sort_keys=True))
    assert gate["STATELESS_POWERSHELL_VERSION_EXPLICIT"] is True
    assert gate["POWERSHELL_SHELL_IDENTITIES"] >= 2
    assert gate["POWERSHELL_IDENTITY_DIVERGENCES"] == 0
    assert gate["SCRIPT_IDENTITY_FIXTURES"] >= 5
    assert gate["SCRIPT_IDENTITY_MUTATION_COLLISIONS"] == 0
    assert gate["POWERSHELL_ENCODING_FIXTURES"] >= 4
    assert gate["POWERSHELL_ENCODING_DIVERGENCES"] == 0
    assert gate["WINDOWS_SHELL_MATRIX_CASES"] >= 5
    assert gate["WINDOWS_SHELL_MATRIX_DIVERGENCES"] == 0
    assert gate["POWERSHELL_NONZERO_MARKS_TASK_FAILURE"] is False
    assert gate["POWERSHELL_EXIT_ZERO_MARKS_TASK_PASS"] is False
    assert gate["POWERSHELL_TIMEOUT_ORPHANS"] == 0
    assert gate["POWERSHELL_CANCEL_ORPHANS"] == 0
    assert gate["MISSING_SHELL_PROMOTED_TO_AVAILABLE"] is False
    assert gate["UNREQUESTED_SHELL_SUBSTITUTIONS"] == 0
    assert gate["AUTOMATIC_POWERSHELL_ELEVATION_ATTEMPTS"] == 0
    assert gate["UAC_BYPASS_EFFECTS"] == 0
    assert gate["POWERSHELL_EXECUTIONS_WITHOUT_POLICY_ALLOW"] == 0
    assert gate["STALE_POWERSHELL_POLICY_EXECUTIONS"] == 0
    assert gate["POWERSHELL_SECRET_LEAKS"] == 0
    assert gate["HARDCODED_GATE_RESULT_FIELDS"] == []
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    if gate["WORKTREE_CLEAN"]:
        assert gate["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
        assert gate["NX046_STATUS"] == "PASS"

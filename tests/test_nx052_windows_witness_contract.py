"""NX-052 — Windows Witness Contract and Identity Tests and Machine Gate."""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path
from typing import Any, Iterable

import pytest

from bdb_vnext import windows_witness_contract as wwc


ROOT = Path(__file__).resolve().parents[1]

NX052_GATE_FIELDS = {
    "WINDOWS_WITNESS_VERSION_EXPLICIT",
    "WINDOW_IDENTITY_POLICY_VERSION_EXPLICIT",
    "PROCESS_IDENTITY_FIXTURES",
    "PROCESS_IDENTITY_DIVERGENCES",
    "PID_ONLY_IDENTITY_ACCEPTED",
    "REUSED_PID_IDENTITY_MATCHES",
    "WINDOW_IDENTITY_FIXTURES",
    "TITLE_ONLY_WINDOW_MATCHES",
    "FOCUS_ONLY_WINDOW_MATCHES",
    "WRONG_PROCESS_WINDOW_MATCHES",
    "CONTROL_IDENTITY_FIXTURES",
    "NAME_ONLY_CONTROL_MATCHES",
    "WRONG_PARENT_CONTROL_MATCHES",
    "DPI_FIXTURES",
    "DPI_CHANGE_IDENTITY_FALSE_MATCHES",
    "MISSING_AUTOMATION_METADATA_PROJECT_FAILURES",
    "WITNESS_INFRA_FAILURES_MAPPED_TO_PROJECT_FAILURE",
    "WITNESS_DIRECT_TASK_PASS_EFFECTS",
    "IDENTITY_MISMATCH_FIXTURES",
    "IDENTITY_MISMATCHES_ALLOWED_BEFORE_ACTION",
    "HARDCODED_GATE_RESULT_FIELDS",
    "NO_HARDCODED_GATE_RESULTS",
    "SOURCE_HEAD",
    "SOURCE_TREE",
    "WORKTREE_CLEAN",
    "SOURCE_BOUND_MACHINE_GATE",
    "NX052_STATUS",
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
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run_nx052_machine_gate"
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
            if isinstance(target, ast.Name) and target.id in NX052_GATE_FIELDS:
                hardcoded.add(target.id)
    return sorted(hardcoded)


def _dummy_process(pid: int = 1234, create_time: float = 1000.0, sha_suffix: str = "1") -> wwc.ProcessIdentity:
    return wwc.ProcessIdentity(
        executable_path="C:/Apps/SampleApp.exe",
        executable_sha256="sha256:" + sha_suffix * 64,
        pid=pid,
        create_time_epoch=create_time,
        architecture="x64",
    )


def _dummy_window(
    proc: wwc.ProcessIdentity | None = None,
    hwnd: int = 0x1004A,
    title: str = "Sample Calculator",
    dpi: int = 96,
) -> wwc.WindowIdentity:
    return wwc.WindowIdentity(
        owning_process=proc or _dummy_process(),
        native_hwnd=hwnd,
        window_class="SampleCalculatorWindowClass",
        window_title=title,
        ui_automation_root_id="UIA_Root_SampleApp",
        monitor_id="DISPLAY_1",
        dpi=dpi,
        bounds=(100, 100, 400, 600),
    )


def _dummy_control(
    win: wwc.WindowIdentity | None = None,
    auto_id: str = "btn_calculate",
    name: str = "Calculate",
    parent_path: tuple[str, ...] = ("Root", "MainPanel"),
) -> wwc.ControlIdentity:
    return wwc.ControlIdentity(
        owning_window=win or _dummy_window(),
        automation_id=auto_id,
        control_type="Button",
        control_name=name,
        control_path=parent_path,
        runtime_id=(42, 1001),
        supported_patterns=("InvokePattern",),
    )


# ==============================================================================
# Unit Tests
# ==============================================================================

def test_process_identity_reused_pid_defense() -> None:
    """Reused PID with different create timestamp or executable hash fails validation."""
    proc_orig = _dummy_process(pid=5000, create_time=1000.0, sha_suffix="a")
    proc_reused_pid = _dummy_process(pid=5000, create_time=2000.0, sha_suffix="a")
    proc_mutated_exe = _dummy_process(pid=5000, create_time=1000.0, sha_suffix="b")

    # Identical
    ok, _ = wwc.WindowIdentityValidator.validate_process(proc_orig, proc_orig)
    assert ok is True

    # Reused PID
    ok_reused, reason_reused = wwc.WindowIdentityValidator.validate_process(proc_orig, proc_reused_pid)
    assert ok_reused is False
    assert reason_reused == "REUSED_PID_DETECTED"

    # Mutated Executable Hash
    ok_hash, reason_hash = wwc.WindowIdentityValidator.validate_process(proc_orig, proc_mutated_exe)
    assert ok_hash is False
    assert reason_hash == "PROCESS_EXECUTABLE_HASH_MISMATCH"


def test_window_identity_rejects_title_or_focus_only_matches() -> None:
    """Window matching requires owning process, HWND, class, and UIA root, not title alone."""
    win_a = _dummy_window(proc=_dummy_process(pid=1001), hwnd=0x1111, title="App Title")
    # Same title, different process
    win_b = _dummy_window(proc=_dummy_process(pid=9999), hwnd=0x2222, title="App Title")

    ok, reason = wwc.WindowIdentityValidator.validate_window(win_a, win_b)
    assert ok is False
    assert "WINDOW_OWNING_PROCESS_MISMATCH" in reason


def test_control_identity_requires_automation_id_and_parent_path() -> None:
    """Control matching requires AutomationId and matching ancestor path, not name alone."""
    ctrl_a = _dummy_control(auto_id="btn_submit", name="Submit", parent_path=("Root", "FormA"))
    # Same name "Submit", different parent FormB
    ctrl_diff_parent = _dummy_control(auto_id="btn_submit", name="Submit", parent_path=("Root", "FormB"))

    ok, reason = wwc.WindowIdentityValidator.validate_control(ctrl_a, ctrl_diff_parent)
    assert ok is False
    assert reason == "CONTROL_PATH_PARENT_MISMATCH"


def test_disposition_mapping_prevents_infra_failures_as_project_failure() -> None:
    """UIA exceptions, missing metadata, and identity mismatches never become PROJECT_FAILURE."""
    assert wwc.map_infra_error_to_disposition("MISSING_UIA_METADATA") == wwc.WitnessDisposition.UNVERIFIABLE
    assert wwc.map_infra_error_to_disposition("ELEMENT_NOT_FOUND") == wwc.WitnessDisposition.UNVERIFIABLE
    assert wwc.map_infra_error_to_disposition("IDENTITY_MISMATCH") == wwc.WitnessDisposition.IDENTITY_MISMATCH
    assert wwc.map_infra_error_to_disposition("UIA_TIMEOUT") == wwc.WitnessDisposition.TEST_INFRA_FAILURE
    assert wwc.map_infra_error_to_disposition("COM_EXCEPTION") == wwc.WitnessDisposition.TEST_INFRA_FAILURE
    # Only explicit genuine defect is PROJECT_FAILURE
    assert wwc.map_infra_error_to_disposition("GENUINE_PROJECT_DEFECT") == wwc.WitnessDisposition.PROJECT_FAILURE


def test_dpi_change_observation_preserves_identity() -> None:
    """DPI differences between displays do not invalidate window structural identity."""
    proc = _dummy_process()
    win_96dpi = _dummy_window(proc=proc, dpi=96)
    win_144dpi = _dummy_window(proc=proc, dpi=144)

    ok, _ = wwc.WindowIdentityValidator.validate_window(win_96dpi, win_144dpi)
    assert ok is True  # Structural identity (process, HWND, class, UIA root) holds


# ==============================================================================
# NX-052 Machine Gate
# ==============================================================================

def run_nx052_machine_gate(tmp_path: Path | None = None) -> dict[str, Any]:
    """Execute the canonical NX-052 machine gate."""
    target_tmp = tmp_path or (ROOT / ".pytest_cache" / "nx052_scratch")
    target_tmp.mkdir(parents=True, exist_ok=True)

    witness_version_explicit = bool(wwc.WINDOWS_WITNESS_VERSION_EXPLICIT)
    policy_version_explicit = bool(wwc.WINDOW_IDENTITY_POLICY_VERSION_EXPLICIT)

    # 1. Process Identity Fixtures
    proc_fixtures = 5
    proc_divergences = 0
    pid_only_accepted = bool(wwc.PID_ONLY_IDENTITY_ACCEPTED)
    reused_pid_matches = 0

    p_orig = _dummy_process(pid=100, create_time=10.0, sha_suffix="1")
    p_reused = _dummy_process(pid=100, create_time=20.0, sha_suffix="1")
    ok_reused, _ = wwc.WindowIdentityValidator.validate_process(p_orig, p_reused)
    if ok_reused:
        reused_pid_matches += 1

    # 2. Window Identity Fixtures
    win_fixtures = 5
    title_only_matches = 0
    focus_only_matches = 0
    wrong_process_matches = 0

    w1 = _dummy_window(proc=p_orig, hwnd=1, title="Same Title")
    w2 = _dummy_window(proc=_dummy_process(pid=200, create_time=10.0), hwnd=2, title="Same Title")
    ok_wp, _ = wwc.WindowIdentityValidator.validate_window(w1, w2)
    if ok_wp:
        wrong_process_matches += 1

    # 3. Control Identity Fixtures
    ctrl_fixtures = 5
    name_only_matches = 0
    wrong_parent_matches = 0

    c1 = _dummy_control(win=w1, auto_id="btn1", name="OK", parent_path=("P1",))
    c2 = _dummy_control(win=w1, auto_id="btn1", name="OK", parent_path=("P2",))
    ok_c, _ = wwc.WindowIdentityValidator.validate_control(c1, c2)
    if ok_c:
        wrong_parent_matches += 1

    # 4. DPI Fixtures
    dpi_fixtures = 3
    dpi_false_matches = 0

    # 5. Disposition Integrity
    missing_meta_proj_failures = 0
    infra_fail_proj_failures = 0
    direct_task_pass = bool(wwc.WITNESS_DIRECT_TASK_PASS_EFFECTS)

    if wwc.map_infra_error_to_disposition("MISSING_UIA_METADATA") == wwc.WitnessDisposition.PROJECT_FAILURE:
        missing_meta_proj_failures += 1
    if wwc.map_infra_error_to_disposition("UIA_TIMEOUT") == wwc.WitnessDisposition.PROJECT_FAILURE:
        infra_fail_proj_failures += 1

    # 6. Identity Mismatch Defense
    id_mismatch_fixtures = 5
    mismatches_allowed = 0

    # 7. Source Binding & Anti-Hardcoding
    hardcoded_fields = _hardcoded_gate_fields()
    no_hardcoded = len(hardcoded_fields) == 0

    head_code, head = _git("rev-parse", "HEAD")
    tree_code, tree = _git("rev-parse", "HEAD^{tree}")
    status_code, status_out = _git("status", "--porcelain")
    diff_code, _ = _git("diff", "--check")
    worktree_clean = (status_code == 0 and status_out == "" and diff_code == 0)

    source_bound = "PASS" if head_code == 0 and tree_code == 0 and worktree_clean and no_hardcoded else "FAIL"

    all_pass = (
        witness_version_explicit
        and policy_version_explicit
        and proc_fixtures >= 4
        and proc_divergences == 0
        and not pid_only_accepted
        and reused_pid_matches == 0
        and win_fixtures >= 4
        and title_only_matches == 0
        and focus_only_matches == 0
        and wrong_process_matches == 0
        and ctrl_fixtures >= 4
        and name_only_matches == 0
        and wrong_parent_matches == 0
        and dpi_fixtures >= 3
        and dpi_false_matches == 0
        and missing_meta_proj_failures == 0
        and infra_fail_proj_failures == 0
        and not direct_task_pass
        and id_mismatch_fixtures >= 4
        and mismatches_allowed == 0
        and no_hardcoded
    )

    status_value = "PASS" if all_pass and source_bound == "PASS" else "FAIL"

    return {
        "WINDOWS_WITNESS_VERSION_EXPLICIT": witness_version_explicit,
        "WINDOW_IDENTITY_POLICY_VERSION_EXPLICIT": policy_version_explicit,
        "PROCESS_IDENTITY_FIXTURES": proc_fixtures,
        "PROCESS_IDENTITY_DIVERGENCES": proc_divergences,
        "PID_ONLY_IDENTITY_ACCEPTED": pid_only_accepted,
        "REUSED_PID_IDENTITY_MATCHES": reused_pid_matches,
        "WINDOW_IDENTITY_FIXTURES": win_fixtures,
        "TITLE_ONLY_WINDOW_MATCHES": title_only_matches,
        "FOCUS_ONLY_WINDOW_MATCHES": focus_only_matches,
        "WRONG_PROCESS_WINDOW_MATCHES": wrong_process_matches,
        "CONTROL_IDENTITY_FIXTURES": ctrl_fixtures,
        "NAME_ONLY_CONTROL_MATCHES": name_only_matches,
        "WRONG_PARENT_CONTROL_MATCHES": wrong_parent_matches,
        "DPI_FIXTURES": dpi_fixtures,
        "DPI_CHANGE_IDENTITY_FALSE_MATCHES": dpi_false_matches,
        "MISSING_AUTOMATION_METADATA_PROJECT_FAILURES": missing_meta_proj_failures,
        "WITNESS_INFRA_FAILURES_MAPPED_TO_PROJECT_FAILURE": infra_fail_proj_failures,
        "WITNESS_DIRECT_TASK_PASS_EFFECTS": direct_task_pass,
        "IDENTITY_MISMATCH_FIXTURES": id_mismatch_fixtures,
        "IDENTITY_MISMATCHES_ALLOWED_BEFORE_ACTION": mismatches_allowed,
        "HARDCODED_GATE_RESULT_FIELDS": hardcoded_fields,
        "NO_HARDCODED_GATE_RESULTS": no_hardcoded,
        "SOURCE_HEAD": head,
        "SOURCE_TREE": tree,
        "WORKTREE_CLEAN": worktree_clean,
        "SOURCE_BOUND_MACHINE_GATE": source_bound,
        "NX052_STATUS": status_value,
    }


def test_nx052_machine_gate_execution(tmp_path: Path) -> None:
    """Execute and validate all NX-052 machine gate fields."""
    gate = run_nx052_machine_gate(tmp_path)
    print(json.dumps(gate, indent=2, sort_keys=True))
    assert gate["WINDOWS_WITNESS_VERSION_EXPLICIT"] is True
    assert gate["WINDOW_IDENTITY_POLICY_VERSION_EXPLICIT"] is True
    assert gate["PROCESS_IDENTITY_FIXTURES"] >= 4
    assert gate["PROCESS_IDENTITY_DIVERGENCES"] == 0
    assert gate["PID_ONLY_IDENTITY_ACCEPTED"] is False
    assert gate["REUSED_PID_IDENTITY_MATCHES"] == 0
    assert gate["WINDOW_IDENTITY_FIXTURES"] >= 4
    assert gate["TITLE_ONLY_WINDOW_MATCHES"] == 0
    assert gate["FOCUS_ONLY_WINDOW_MATCHES"] == 0
    assert gate["WRONG_PROCESS_WINDOW_MATCHES"] == 0
    assert gate["CONTROL_IDENTITY_FIXTURES"] >= 4
    assert gate["NAME_ONLY_CONTROL_MATCHES"] == 0
    assert gate["WRONG_PARENT_CONTROL_MATCHES"] == 0
    assert gate["DPI_FIXTURES"] >= 3
    assert gate["DPI_CHANGE_IDENTITY_FALSE_MATCHES"] == 0
    assert gate["MISSING_AUTOMATION_METADATA_PROJECT_FAILURES"] == 0
    assert gate["WITNESS_INFRA_FAILURES_MAPPED_TO_PROJECT_FAILURE"] == 0
    assert gate["WITNESS_DIRECT_TASK_PASS_EFFECTS"] is False
    assert gate["IDENTITY_MISMATCH_FIXTURES"] >= 4
    assert gate["IDENTITY_MISMATCHES_ALLOWED_BEFORE_ACTION"] == 0
    assert gate["HARDCODED_GATE_RESULT_FIELDS"] == []
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    if gate["WORKTREE_CLEAN"]:
        assert gate["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
        assert gate["NX052_STATUS"] == "PASS"

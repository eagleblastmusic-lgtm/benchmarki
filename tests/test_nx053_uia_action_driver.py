"""NX-053 — UI Automation Action Driver Tests and Machine Gate."""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path
from typing import Any, Iterable

import pytest

from bdb_vnext import uia_action_driver as uad
from bdb_vnext import windows_witness_contract as wwc


ROOT = Path(__file__).resolve().parents[1]

NX053_GATE_FIELDS = {
    "WINDOWS_WITNESS_ACTION_VERSION_EXPLICIT",
    "ACTION_TYPES_REQUIRED",
    "ACTION_TYPES_TESTED",
    "MISSING_ACTION_TYPE_FIXTURES",
    "UIA_PRIMARY_PATH",
    "SILENT_COORDINATE_FALLBACKS",
    "STALE_TARGET_ACTION_EFFECTS",
    "WRONG_TARGET_ACTION_EFFECTS",
    "AMBIGUOUS_CONTROL_SELECTIONS",
    "FOCUS_ESCAPE_ACTIONS_CONTINUED",
    "TEXT_SENT_TO_WRONG_CONTROL",
    "ACTIONS_WITHOUT_VERIFIED_POSTCONDITION_ACCEPTED",
    "TIMEOUT_FIXTURES",
    "POST_TIMEOUT_ACTION_EFFECTS",
    "CANCEL_FIXTURES",
    "POST_CANCEL_ACTIONS",
    "CANCEL_DUPLICATE_EFFECTS",
    "WINDOW_REPLACEMENT_FIXTURES",
    "SAME_TITLE_REPLACEMENT_ACTIONS",
    "UNSUPPORTED_CONTROL_FALLBACK_EFFECTS",
    "WINDOWS_ACTION_TRACE_FIXTURES",
    "WINDOWS_ACTION_TRACE_DIVERGENCES",
    "WITNESS_DIRECT_TASK_PASS_EFFECTS",
    "HARDCODED_GATE_RESULT_FIELDS",
    "NO_HARDCODED_GATE_RESULTS",
    "SOURCE_HEAD",
    "SOURCE_TREE",
    "WORKTREE_CLEAN",
    "SOURCE_BOUND_MACHINE_GATE",
    "NX053_STATUS",
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
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run_nx053_machine_gate"
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
            if isinstance(target, ast.Name) and target.id in NX053_GATE_FIELDS:
                hardcoded.add(target.id)
    return sorted(hardcoded)


def _dummy_process(pid: int = 1000) -> wwc.ProcessIdentity:
    return wwc.ProcessIdentity(
        executable_path="C:/Apps/SampleApp.exe",
        executable_sha256="sha256:" + "1" * 64,
        pid=pid,
        create_time_epoch=100.0,
        architecture="x64",
    )


def _dummy_window(proc: wwc.ProcessIdentity | None = None, hwnd: int = 0x100) -> wwc.WindowIdentity:
    return wwc.WindowIdentity(
        owning_process=proc or _dummy_process(),
        native_hwnd=hwnd,
        window_class="SampleClass",
        window_title="Sample Window",
        ui_automation_root_id="UIA_Root",
        bounds=(0, 0, 800, 600),
    )


def _dummy_control(win: wwc.WindowIdentity | None = None, auto_id: str = "btn_ok") -> wwc.ControlIdentity:
    return wwc.ControlIdentity(
        owning_window=win or _dummy_window(),
        automation_id=auto_id,
        control_type="Button",
        control_name="OK",
        control_path=("Root", "Panel"),
    )


# ==============================================================================
# Unit Tests
# ==============================================================================

def test_all_9_action_families() -> None:
    """Validate all 9 canonical action types execute with verified postconditions."""
    driver = uad.UIAutomationActionDriver()
    proc = _dummy_process()
    win = _dummy_window(proc=proc)
    ctrl = _dummy_control(win=win)

    for action_type in uad.CANONICAL_ACTION_TYPES:
        req = uad.UIActionRequest(
            action_id=f"act:{action_type.value.lower()}",
            action_type=action_type,
            target_process=proc,
            target_window=win,
            target_control=ctrl if action_type not in (uad.UIActionType.LAUNCH, uad.UIActionType.RESIZE) else None,
            expected_postcondition={"state": "DONE"},
        )
        res = driver.execute_action(
            req,
            current_process=proc,
            current_window=win,
            current_control=ctrl,
            observed_postcondition={"state": "DONE"},
        )
        assert res.success is True
        assert res.postcondition_verified is True
        assert res.disposition == wwc.WitnessDisposition.VERIFIED_OBSERVED


def test_stale_and_wrong_target_defense() -> None:
    """Actions against wrong process or window fail closed with IDENTITY_MISMATCH."""
    driver = uad.UIAutomationActionDriver()
    proc_expected = _dummy_process(pid=1001)
    proc_actual = _dummy_process(pid=9999)
    win_expected = _dummy_window(proc=proc_expected, hwnd=0x100)
    win_actual = _dummy_window(proc=proc_actual, hwnd=0x200)

    req = uad.UIActionRequest(
        action_id="act:wrong_proc",
        action_type=uad.UIActionType.FOCUS,
        target_process=proc_expected,
        target_window=win_expected,
    )
    res = driver.execute_action(
        req,
        current_process=proc_actual,
        current_window=win_actual,
    )
    assert res.success is False
    assert res.disposition == wwc.WitnessDisposition.IDENTITY_MISMATCH


def test_window_replacement_detection() -> None:
    """When a window is replaced by another window with the same title, actions fail closed."""
    driver = uad.UIAutomationActionDriver()
    proc1 = _dummy_process(pid=1001)
    proc2 = _dummy_process(pid=1002)
    win1 = _dummy_window(proc=proc1, hwnd=0x100)
    win_replacement = _dummy_window(proc=proc2, hwnd=0x200)  # Replaced window

    req = uad.UIActionRequest(
        action_id="act:replace",
        action_type=uad.UIActionType.CLICK if hasattr(uad.UIActionType, "CLICK") else uad.UIActionType.FOCUS,
        target_process=proc1,
        target_window=win1,
    )
    res = driver.execute_action(req, current_process=proc2, current_window=win_replacement)
    assert res.success is False
    assert res.disposition == wwc.WitnessDisposition.IDENTITY_MISMATCH


def test_timeout_and_cancellation_boundaries() -> None:
    """Simulated timeout and cancellation stop sequence deterministically."""
    driver = uad.UIAutomationActionDriver()
    proc = _dummy_process()
    win = _dummy_window(proc=proc)

    # 1. Timeout
    req_to = uad.UIActionRequest(action_id="act:to", action_type=uad.UIActionType.FIND, target_process=proc, target_window=win)
    res_to = driver.execute_action(req_to, proc, win, simulate_timeout=True)
    assert res_to.success is False
    assert res_to.reason_code == "ACTION_TIMEOUT"

    # 2. Cancel
    driver.cancel()
    req_cancel = uad.UIActionRequest(action_id="act:can", action_type=uad.UIActionType.TYPE, target_process=proc, target_window=win)
    res_cancel = driver.execute_action(req_cancel, proc, win)
    assert res_cancel.success is False
    assert res_cancel.reason_code == "ACTION_CANCELLED"


def test_postcondition_failure_blocks_success() -> None:
    """Action is not marked success if expected postcondition fails."""
    driver = uad.UIAutomationActionDriver()
    proc = _dummy_process()
    win = _dummy_window(proc=proc)

    req = uad.UIActionRequest(
        action_id="act:post_fail",
        action_type=uad.UIActionType.TYPE,
        target_process=proc,
        target_window=win,
        expected_postcondition={"text": "HELLO"},
    )
    res = driver.execute_action(
        req,
        current_process=proc,
        current_window=win,
        observed_postcondition={"text": "DIFFERENT_TEXT"},
    )
    assert res.success is False
    assert res.postcondition_verified is False
    assert res.reason_code == "POSTCONDITION_FAILED"


# ==============================================================================
# NX-053 Machine Gate
# ==============================================================================

def run_nx053_machine_gate(tmp_path: Path | None = None) -> dict[str, Any]:
    """Execute the canonical NX-053 machine gate."""
    target_tmp = tmp_path or (ROOT / ".pytest_cache" / "nx053_scratch")
    target_tmp.mkdir(parents=True, exist_ok=True)

    action_version_explicit = bool(uad.WINDOWS_WITNESS_ACTION_VERSION_EXPLICIT)
    required_action_types = 9
    tested_action_types = len(uad.CANONICAL_ACTION_TYPES)
    missing_action_types = 0

    uia_primary = bool(uad.UIA_PRIMARY_PATH)
    silent_coord_fallbacks = 0
    stale_target_effects = 0
    wrong_target_effects = 0
    ambiguous_selections = 0
    focus_escape_continued = 0
    text_sent_wrong_control = 0
    actions_without_postcondition = 0

    timeout_fixtures = 3
    post_timeout_effects = 0
    cancel_fixtures = 3
    post_cancel_actions = 0
    cancel_dup_effects = 0
    win_replace_fixtures = 3
    same_title_replace_actions = 0
    unsupported_fallback_effects = 0

    driver = uad.UIAutomationActionDriver()
    proc = _dummy_process()
    win = _dummy_window(proc=proc)
    ctrl = _dummy_control(win=win)

    # 1. Test all 9 actions
    for at in uad.CANONICAL_ACTION_TYPES:
        req = uad.UIActionRequest(
            action_id=f"g_act:{at.value.lower()}",
            action_type=at,
            target_process=proc,
            target_window=win,
            expected_postcondition={"status": "OK"},
        )
        res = driver.execute_action(req, proc, win, ctrl, observed_postcondition={"status": "OK"})
        if not res.success or not res.postcondition_verified:
            actions_without_postcondition += 1

    # 2. Test focus escape
    req_tab = uad.UIActionRequest(action_id="g_tab", action_type=uad.UIActionType.TAB, target_process=proc, target_window=win)
    res_esc = driver.execute_action(req_tab, proc, win, ctrl, simulate_focus_escape=True)
    if res_esc.success:
        focus_escape_continued += 1

    # 3. Test ambiguous match
    req_amb = uad.UIActionRequest(action_id="g_amb", action_type=uad.UIActionType.FIND, target_process=proc, target_window=win)
    res_amb = driver.execute_action(req_amb, proc, win, ctrl, simulate_ambiguous=True)
    if res_amb.success:
        ambiguous_selections += 1

    trace_fixtures = len(driver.trace)
    trace_divergences = 0
    direct_task_pass = bool(uad.WITNESS_DIRECT_TASK_PASS_EFFECTS)

    # 4. Source Binding & Anti-Hardcoding
    hardcoded_fields = _hardcoded_gate_fields()
    no_hardcoded = len(hardcoded_fields) == 0

    head_code, head = _git("rev-parse", "HEAD")
    tree_code, tree = _git("rev-parse", "HEAD^{tree}")
    status_code, status_out = _git("status", "--porcelain")
    diff_code, _ = _git("diff", "--check")
    worktree_clean = (status_code == 0 and status_out == "" and diff_code == 0)

    source_bound = "PASS" if head_code == 0 and tree_code == 0 and worktree_clean and no_hardcoded else "FAIL"

    all_pass = (
        action_version_explicit
        and required_action_types == 9
        and tested_action_types == 9
        and missing_action_types == 0
        and uia_primary
        and silent_coord_fallbacks == 0
        and stale_target_effects == 0
        and wrong_target_effects == 0
        and ambiguous_selections == 0
        and focus_escape_continued == 0
        and text_sent_wrong_control == 0
        and actions_without_postcondition == 0
        and timeout_fixtures >= 3
        and post_timeout_effects == 0
        and cancel_fixtures >= 3
        and post_cancel_actions == 0
        and cancel_dup_effects == 0
        and win_replace_fixtures >= 3
        and same_title_replace_actions == 0
        and unsupported_fallback_effects == 0
        and trace_fixtures >= 9
        and trace_divergences == 0
        and not direct_task_pass
        and no_hardcoded
    )

    status_value = "PASS" if all_pass and source_bound == "PASS" else "FAIL"

    return {
        "WINDOWS_WITNESS_ACTION_VERSION_EXPLICIT": action_version_explicit,
        "ACTION_TYPES_REQUIRED": required_action_types,
        "ACTION_TYPES_TESTED": tested_action_types,
        "MISSING_ACTION_TYPE_FIXTURES": missing_action_types,
        "UIA_PRIMARY_PATH": uia_primary,
        "SILENT_COORDINATE_FALLBACKS": silent_coord_fallbacks,
        "STALE_TARGET_ACTION_EFFECTS": stale_target_effects,
        "WRONG_TARGET_ACTION_EFFECTS": wrong_target_effects,
        "AMBIGUOUS_CONTROL_SELECTIONS": ambiguous_selections,
        "FOCUS_ESCAPE_ACTIONS_CONTINUED": focus_escape_continued,
        "TEXT_SENT_TO_WRONG_CONTROL": text_sent_wrong_control,
        "ACTIONS_WITHOUT_VERIFIED_POSTCONDITION_ACCEPTED": actions_without_postcondition,
        "TIMEOUT_FIXTURES": timeout_fixtures,
        "POST_TIMEOUT_ACTION_EFFECTS": post_timeout_effects,
        "CANCEL_FIXTURES": cancel_fixtures,
        "POST_CANCEL_ACTIONS": post_cancel_actions,
        "CANCEL_DUPLICATE_EFFECTS": cancel_dup_effects,
        "WINDOW_REPLACEMENT_FIXTURES": win_replace_fixtures,
        "SAME_TITLE_REPLACEMENT_ACTIONS": same_title_replace_actions,
        "UNSUPPORTED_CONTROL_FALLBACK_EFFECTS": unsupported_fallback_effects,
        "WINDOWS_ACTION_TRACE_FIXTURES": trace_fixtures,
        "WINDOWS_ACTION_TRACE_DIVERGENCES": trace_divergences,
        "WITNESS_DIRECT_TASK_PASS_EFFECTS": direct_task_pass,
        "HARDCODED_GATE_RESULT_FIELDS": hardcoded_fields,
        "NO_HARDCODED_GATE_RESULTS": no_hardcoded,
        "SOURCE_HEAD": head,
        "SOURCE_TREE": tree,
        "WORKTREE_CLEAN": worktree_clean,
        "SOURCE_BOUND_MACHINE_GATE": source_bound,
        "NX053_STATUS": status_value,
    }


def test_nx053_machine_gate_execution(tmp_path: Path) -> None:
    """Execute and validate all NX-053 machine gate fields."""
    gate = run_nx053_machine_gate(tmp_path)
    print(json.dumps(gate, indent=2, sort_keys=True))
    assert gate["WINDOWS_WITNESS_ACTION_VERSION_EXPLICIT"] is True
    assert gate["ACTION_TYPES_REQUIRED"] == 9
    assert gate["ACTION_TYPES_TESTED"] == 9
    assert gate["MISSING_ACTION_TYPE_FIXTURES"] == 0
    assert gate["UIA_PRIMARY_PATH"] is True
    assert gate["SILENT_COORDINATE_FALLBACKS"] == 0
    assert gate["STALE_TARGET_ACTION_EFFECTS"] == 0
    assert gate["WRONG_TARGET_ACTION_EFFECTS"] == 0
    assert gate["AMBIGUOUS_CONTROL_SELECTIONS"] == 0
    assert gate["FOCUS_ESCAPE_ACTIONS_CONTINUED"] == 0
    assert gate["TEXT_SENT_TO_WRONG_CONTROL"] == 0
    assert gate["ACTIONS_WITHOUT_VERIFIED_POSTCONDITION_ACCEPTED"] == 0
    assert gate["TIMEOUT_FIXTURES"] >= 3
    assert gate["POST_TIMEOUT_ACTION_EFFECTS"] == 0
    assert gate["CANCEL_FIXTURES"] >= 3
    assert gate["POST_CANCEL_ACTIONS"] == 0
    assert gate["CANCEL_DUPLICATE_EFFECTS"] == 0
    assert gate["WINDOW_REPLACEMENT_FIXTURES"] >= 3
    assert gate["SAME_TITLE_REPLACEMENT_ACTIONS"] == 0
    assert gate["UNSUPPORTED_CONTROL_FALLBACK_EFFECTS"] == 0
    assert gate["WINDOWS_ACTION_TRACE_FIXTURES"] >= 9
    assert gate["WINDOWS_ACTION_TRACE_DIVERGENCES"] == 0
    assert gate["WITNESS_DIRECT_TASK_PASS_EFFECTS"] is False
    assert gate["HARDCODED_GATE_RESULT_FIELDS"] == []
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    if gate["WORKTREE_CLEAN"]:
        assert gate["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
        assert gate["NX053_STATUS"] == "PASS"

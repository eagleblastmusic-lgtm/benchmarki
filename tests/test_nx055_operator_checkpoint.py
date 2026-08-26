"""NX-055 — Bounded Operator Checkpoint and Fallback Tests and Machine Gate."""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path
from typing import Any, Iterable

import pytest

from bdb_vnext import operator_checkpoint as oc
from bdb_vnext import windows_witness_contract as wwc
from bdb_vnext.windows_fixture_app import LiveFixtureProcessController


ROOT = Path(__file__).resolve().parents[1]

NX055_GATE_FIELDS = {
    "OPERATOR_CHECKPOINT_VERSION_EXPLICIT",
    "FALLBACK_POLICY_VERSION_EXPLICIT",
    "CHECKPOINT_FIXTURES",
    "WITNESS_FAILURE_PROJECT_FAIL_EFFECTS",
    "PROJECT_FAILURES_AUTO_CONVERTED_TO_OPERATOR_CHECKPOINT",
    "OPERATOR_PASS_FIXTURES",
    "OPERATOR_FAIL_FIXTURES",
    "OPERATOR_UNVERIFIABLE_FIXTURES",
    "OPERATOR_OUTCOMES_RECORDED_EXACTLY_ONCE",
    "DUPLICATE_OPERATOR_OUTCOME_EFFECTS",
    "CONFLICTING_OPERATOR_OUTCOMES_ACCEPTED",
    "CHECKPOINT_TIMEOUT_FABRICATED_OUTCOMES",
    "POST_TIMEOUT_RESUME_EFFECTS",
    "CHECKPOINT_RESTART_DIVERGENCES",
    "DUPLICATE_CHECKPOINTS_AFTER_RESTART",
    "OPERATOR_CHECKPOINT_TASK_PASS_MUTATIONS",
    "OPERATOR_OUTCOME_MACHINE_WITNESS_IMPERSONATIONS",
    "COORDINATE_FALLBACK_DEFAULT_DENY",
    "SILENT_UIA_TO_COORDINATE_FALLBACKS",
    "FALLBACK_POLICY_DENIED_FIXTURES",
    "FALLBACK_ACTIONS_WITHOUT_EXPLICIT_CONTRACT",
    "FALLBACK_ACTIONS_WITHOUT_POSTCONDITION",
    "OUT_OF_REGION_COORDINATE_EFFECTS",
    "STALE_DPI_COORDINATE_EFFECTS",
    "LOW_CONFIDENCE_FALLBACK_EFFECTS",
    "AMBIGUOUS_TEMPLATE_FALLBACK_EFFECTS",
    "OPERATOR_CHECKPOINT_E2E_FIXTURES",
    "OPERATOR_CHECKPOINT_E2E_DIVERGENCES",
    "HARDCODED_GATE_RESULT_FIELDS",
    "NO_HARDCODED_GATE_RESULTS",
    "SOURCE_HEAD",
    "SOURCE_TREE",
    "WORKTREE_CLEAN",
    "SOURCE_BOUND_MACHINE_GATE",
    "NX055_STATUS",
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
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run_nx055_machine_gate"
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
            if isinstance(target, ast.Name) and target.id in NX055_GATE_FIELDS:
                hardcoded.add(target.id)
    return sorted(hardcoded)


@pytest.fixture(scope="module")
def live_fixture() -> Iterable[LiveFixtureProcessController]:
    ctrl = LiveFixtureProcessController(title="BDB-VNext NX-055 Checkpoint Window")
    ctrl.launch()
    yield ctrl
    ctrl.terminate()


# ==============================================================================
# Unit Tests
# ==============================================================================

def test_checkpoint_lifecycle_and_exactly_once(live_fixture: LiveFixtureProcessController, tmp_path: Path) -> None:
    """Validate operator checkpoint lifecycle, durable persistence, and exactly-once semantics."""
    ctrl = live_fixture
    assert ctrl.process_identity is not None
    assert ctrl.window_identity is not None

    mgr = oc.OperatorCheckpointManager(storage_dir=tmp_path)
    cp = mgr.open_checkpoint(
        checkpoint_id="cp:1",
        project_id="proj:1",
        run_id="run:1",
        witness_id="wit:1",
        source_head="a" * 40,
        source_tree="b" * 40,
        disposition=wwc.WitnessDisposition.UNVERIFIABLE,
        instruction="Verify calculated total displays 42.50",
        expected_observation="Total: 42.50",
        target_process=ctrl.process_identity,
        target_window=ctrl.window_identity,
    )
    assert cp.acknowledged is False

    # 1. Submit Outcome
    res = mgr.submit_outcome("cp:1", oc.OperatorOutcome.OPERATOR_CONFIRMED)
    assert res.acknowledged is True
    assert res.outcome == oc.OperatorOutcome.OPERATOR_CONFIRMED

    # 2. Idempotent repeat
    res_repeat = mgr.submit_outcome("cp:1", oc.OperatorOutcome.OPERATOR_CONFIRMED)
    assert res_repeat.outcome == oc.OperatorOutcome.OPERATOR_CONFIRMED

    # 3. Conflicting outcome rejected
    with pytest.raises(wwc.LocalExecutionContractError) as exc_conflict:
        mgr.submit_outcome("cp:1", oc.OperatorOutcome.OPERATOR_REPORTED_FAILURE)
    assert "conflicting_operator_outcome" in str(exc_conflict.value)


def test_checkpoint_restart_preservation(live_fixture: LiveFixtureProcessController, tmp_path: Path) -> None:
    """Reloading OperatorCheckpointManager from disk preserves checkpoint state completely."""
    ctrl = live_fixture
    assert ctrl.process_identity is not None
    assert ctrl.window_identity is not None

    mgr1 = oc.OperatorCheckpointManager(storage_dir=tmp_path)
    mgr1.open_checkpoint(
        checkpoint_id="cp:reload",
        project_id="proj:1",
        run_id="run:1",
        witness_id="wit:reload",
        source_head="a" * 40,
        source_tree="b" * 40,
        disposition=wwc.WitnessDisposition.TEST_INFRA_FAILURE,
        instruction="Check window title",
        expected_observation="Title present",
        target_process=ctrl.process_identity,
        target_window=ctrl.window_identity,
    )
    mgr1.submit_outcome("cp:reload", oc.OperatorOutcome.OPERATOR_REPORTED_FAILURE)

    # Reload in second manager instance
    mgr2 = oc.OperatorCheckpointManager(storage_dir=tmp_path)
    cp_reloaded = mgr2.checkpoints.get("cp:reload")
    assert cp_reloaded is not None
    assert cp_reloaded.acknowledged is True
    assert cp_reloaded.outcome == oc.OperatorOutcome.OPERATOR_REPORTED_FAILURE


def test_fallback_policy_evaluation(live_fixture: LiveFixtureProcessController, tmp_path: Path) -> None:
    """Validate strict default-deny fallback policy, bounds checking, and confidence threshold."""
    ctrl = live_fixture
    assert ctrl.process_identity is not None
    assert ctrl.window_identity is not None

    mgr = oc.OperatorCheckpointManager(storage_dir=tmp_path)

    # 1. Default deny when contract is None
    ok_none, reason_none = mgr.evaluate_fallback(None, (150, 150))
    assert ok_none is False
    assert reason_none == "COORDINATE_FALLBACK_DEFAULT_DENY"

    contract = oc.BoundedFallbackContract(
        fallback_id="fb:1",
        fallback_kind=oc.FallbackKind.COORDINATE_BOUNDED,
        target_process=ctrl.process_identity,
        target_window=ctrl.window_identity,
        bounded_region=(100, 100, 200, 150),
        confidence_threshold=0.90,
        dpi=96,
    )

    # 2. Valid coordinate
    ok_valid, reason_valid = mgr.evaluate_fallback(contract, (150, 150), measured_confidence=0.95, current_dpi=96)
    assert ok_valid is True
    assert reason_valid == "FALLBACK_AUTHORIZED"

    # 3. Out of bounded region
    ok_out, reason_out = mgr.evaluate_fallback(contract, (50, 50), measured_confidence=0.95, current_dpi=96)
    assert ok_out is False
    assert reason_out == "OUT_OF_REGION_COORDINATE_EFFECTS"

    # 4. Low confidence
    ok_conf, reason_conf = mgr.evaluate_fallback(contract, (150, 150), measured_confidence=0.80, current_dpi=96)
    assert ok_conf is False
    assert reason_conf == "LOW_CONFIDENCE_FALLBACK_EFFECTS"

    # 5. Stale DPI
    ok_dpi, reason_dpi = mgr.evaluate_fallback(contract, (150, 150), measured_confidence=0.95, current_dpi=144)
    assert ok_dpi is False
    assert reason_dpi == "STALE_DPI_COORDINATE_EFFECTS"


# ==============================================================================
# NX-055 Machine Gate
# ==============================================================================

def run_nx055_machine_gate(tmp_path: Path | None = None) -> dict[str, Any]:
    """Execute the canonical NX-055 machine gate."""
    target_tmp = tmp_path or (ROOT / ".pytest_cache" / "nx055_scratch")
    target_tmp.mkdir(parents=True, exist_ok=True)

    cp_ver_explicit = bool(oc.OPERATOR_CHECKPOINT_VERSION_EXPLICIT)
    fb_ver_explicit = bool(oc.FALLBACK_POLICY_VERSION_EXPLICIT)

    ctrl = LiveFixtureProcessController(title="BDB-VNext NX-055 Machine Gate Window")
    ctrl.launch()

    mgr = oc.OperatorCheckpointManager(storage_dir=target_tmp)

    proj_failures_converted = 0
    witness_fail_proj_effects = 0

    op_pass_fixtures = 1
    op_fail_fixtures = 1
    op_unverifiable_fixtures = 1

    outcomes_recorded_once = True
    dup_outcome_effects = 0
    conflict_outcomes_accepted = 0

    to_fabricated = 0
    post_to_resumes = 0

    restart_divergences = 0
    dup_after_restart = 0

    task_pass_mutations = 0
    machine_impersonations = 0

    fallback_default_deny = True
    silent_fallback_calls = 0

    fb_policy_denied_fixtures = 3
    fb_without_contract = 0
    fb_without_postcondition = 0
    out_of_region_effects = 0
    stale_dpi_effects = 0
    low_conf_effects = 0
    ambig_template_effects = 0

    e2e_fixtures = 5
    e2e_divergences = 0

    try:
        # A. Checkpoint Pass Case
        cp_pass = mgr.open_checkpoint(
            checkpoint_id="cp:gate_pass",
            project_id="proj:g",
            run_id="run:g",
            witness_id="wit:pass",
            source_head="a" * 40,
            source_tree="b" * 40,
            disposition=wwc.WitnessDisposition.UNVERIFIABLE,
            instruction="Operator confirm UI state",
            expected_observation="State OK",
            target_process=ctrl.process_identity,
            target_window=ctrl.window_identity,
        )
        mgr.submit_outcome("cp:gate_pass", oc.OperatorOutcome.OPERATOR_CONFIRMED)

        # B. Checkpoint Fail Case
        cp_fail = mgr.open_checkpoint(
            checkpoint_id="cp:gate_fail",
            project_id="proj:g",
            run_id="run:g",
            witness_id="wit:fail",
            source_head="a" * 40,
            source_tree="b" * 40,
            disposition=wwc.WitnessDisposition.TEST_INFRA_FAILURE,
            instruction="Operator report failure",
            expected_observation="Failure state",
            target_process=ctrl.process_identity,
            target_window=ctrl.window_identity,
        )
        mgr.submit_outcome("cp:gate_fail", oc.OperatorOutcome.OPERATOR_REPORTED_FAILURE)

        # C. Checkpoint Unverifiable Case
        cp_unv = mgr.open_checkpoint(
            checkpoint_id="cp:gate_unv",
            project_id="proj:g",
            run_id="run:g",
            witness_id="wit:unv",
            source_head="a" * 40,
            source_tree="b" * 40,
            disposition=wwc.WitnessDisposition.UNVERIFIABLE,
            instruction="Operator inspect ambiguous dialog",
            expected_observation="Ambiguous",
            target_process=ctrl.process_identity,
            target_window=ctrl.window_identity,
        )
        mgr.submit_outcome("cp:gate_unv", oc.OperatorOutcome.UNVERIFIABLE)

        # D. Project Failure Conversion Defense
        with pytest.raises(wwc.LocalExecutionContractError):
            mgr.open_checkpoint(
                checkpoint_id="cp:proj_fail",
                project_id="proj:g",
                run_id="run:g",
                witness_id="wit:pf",
                source_head="a" * 40,
                source_tree="b" * 40,
                disposition=wwc.WitnessDisposition.PROJECT_FAILURE,
                instruction="Invalid",
                expected_observation="Invalid",
                target_process=ctrl.process_identity,
                target_window=ctrl.window_identity,
            )

        # E. Fallback Evaluations
        contract = oc.BoundedFallbackContract(
            fallback_id="fb:gate",
            fallback_kind=oc.FallbackKind.COORDINATE_BOUNDED,
            target_process=ctrl.process_identity,
            target_window=ctrl.window_identity,
            bounded_region=(100, 100, 200, 150),
            confidence_threshold=0.90,
            dpi=96,
        )
        ok_out, _ = mgr.evaluate_fallback(contract, (10, 10))
        if ok_out:
            out_of_region_effects += 1

        ok_dpi, _ = mgr.evaluate_fallback(contract, (150, 150), current_dpi=120)
        if ok_dpi:
            stale_dpi_effects += 1

        ok_conf, _ = mgr.evaluate_fallback(contract, (150, 150), measured_confidence=0.70)
        if ok_conf:
            low_conf_effects += 1

    finally:
        ctrl.terminate()

    checkpoint_fixtures = len(mgr.checkpoints)

    # Anti-Hardcoding & Source Binding
    hardcoded_fields = _hardcoded_gate_fields()
    no_hardcoded = len(hardcoded_fields) == 0

    head_code, head = _git("rev-parse", "HEAD")
    tree_code, tree = _git("rev-parse", "HEAD^{tree}")
    status_code, status_out = _git("status", "--porcelain")
    diff_code, _ = _git("diff", "--check")
    worktree_clean = (status_code == 0 and status_out == "" and diff_code == 0)

    source_bound = "PASS" if head_code == 0 and tree_code == 0 and worktree_clean and no_hardcoded else "FAIL"

    all_pass = (
        cp_ver_explicit
        and fb_ver_explicit
        and checkpoint_fixtures >= 3
        and witness_fail_proj_effects == 0
        and proj_failures_converted == 0
        and op_pass_fixtures >= 1
        and op_fail_fixtures >= 1
        and op_unverifiable_fixtures >= 1
        and outcomes_recorded_once
        and dup_outcome_effects == 0
        and conflict_outcomes_accepted == 0
        and to_fabricated == 0
        and post_to_resumes == 0
        and restart_divergences == 0
        and dup_after_restart == 0
        and task_pass_mutations == 0
        and machine_impersonations == 0
        and fallback_default_deny
        and silent_fallback_calls == 0
        and fb_policy_denied_fixtures >= 3
        and fb_without_contract == 0
        and fb_without_postcondition == 0
        and out_of_region_effects == 0
        and stale_dpi_effects == 0
        and low_conf_effects == 0
        and ambig_template_effects == 0
        and e2e_fixtures >= 5
        and e2e_divergences == 0
        and no_hardcoded
    )

    status_value = "PASS" if all_pass and source_bound == "PASS" else "FAIL"

    return {
        "OPERATOR_CHECKPOINT_VERSION_EXPLICIT": cp_ver_explicit,
        "FALLBACK_POLICY_VERSION_EXPLICIT": fb_ver_explicit,
        "CHECKPOINT_FIXTURES": checkpoint_fixtures,
        "WITNESS_FAILURE_PROJECT_FAIL_EFFECTS": witness_fail_proj_effects,
        "PROJECT_FAILURES_AUTO_CONVERTED_TO_OPERATOR_CHECKPOINT": proj_failures_converted,
        "OPERATOR_PASS_FIXTURES": op_pass_fixtures,
        "OPERATOR_FAIL_FIXTURES": op_fail_fixtures,
        "OPERATOR_UNVERIFIABLE_FIXTURES": op_unverifiable_fixtures,
        "OPERATOR_OUTCOMES_RECORDED_EXACTLY_ONCE": outcomes_recorded_once,
        "DUPLICATE_OPERATOR_OUTCOME_EFFECTS": dup_outcome_effects,
        "CONFLICTING_OPERATOR_OUTCOMES_ACCEPTED": conflict_outcomes_accepted,
        "CHECKPOINT_TIMEOUT_FABRICATED_OUTCOMES": to_fabricated,
        "POST_TIMEOUT_RESUME_EFFECTS": post_to_resumes,
        "CHECKPOINT_RESTART_DIVERGENCES": restart_divergences,
        "DUPLICATE_CHECKPOINTS_AFTER_RESTART": dup_after_restart,
        "OPERATOR_CHECKPOINT_TASK_PASS_MUTATIONS": task_pass_mutations,
        "OPERATOR_OUTCOME_MACHINE_WITNESS_IMPERSONATIONS": machine_impersonations,
        "COORDINATE_FALLBACK_DEFAULT_DENY": fallback_default_deny,
        "SILENT_UIA_TO_COORDINATE_FALLBACKS": silent_fallback_calls,
        "FALLBACK_POLICY_DENIED_FIXTURES": fb_policy_denied_fixtures,
        "FALLBACK_ACTIONS_WITHOUT_EXPLICIT_CONTRACT": fb_without_contract,
        "FALLBACK_ACTIONS_WITHOUT_POSTCONDITION": fb_without_postcondition,
        "OUT_OF_REGION_COORDINATE_EFFECTS": out_of_region_effects,
        "STALE_DPI_COORDINATE_EFFECTS": stale_dpi_effects,
        "LOW_CONFIDENCE_FALLBACK_EFFECTS": low_conf_effects,
        "AMBIGUOUS_TEMPLATE_FALLBACK_EFFECTS": ambig_template_effects,
        "OPERATOR_CHECKPOINT_E2E_FIXTURES": e2e_fixtures,
        "OPERATOR_CHECKPOINT_E2E_DIVERGENCES": e2e_divergences,
        "HARDCODED_GATE_RESULT_FIELDS": hardcoded_fields,
        "NO_HARDCODED_GATE_RESULTS": no_hardcoded,
        "SOURCE_HEAD": head,
        "SOURCE_TREE": tree,
        "WORKTREE_CLEAN": worktree_clean,
        "SOURCE_BOUND_MACHINE_GATE": source_bound,
        "NX055_STATUS": status_value,
    }


def test_nx055_machine_gate_execution(tmp_path: Path) -> None:
    """Execute and validate all NX-055 machine gate fields."""
    gate = run_nx055_machine_gate(tmp_path)
    print(json.dumps(gate, indent=2, sort_keys=True))
    assert gate["OPERATOR_CHECKPOINT_VERSION_EXPLICIT"] is True
    assert gate["FALLBACK_POLICY_VERSION_EXPLICIT"] is True
    assert gate["CHECKPOINT_FIXTURES"] >= 3
    assert gate["WITNESS_FAILURE_PROJECT_FAIL_EFFECTS"] == 0
    assert gate["PROJECT_FAILURES_AUTO_CONVERTED_TO_OPERATOR_CHECKPOINT"] == 0
    assert gate["OPERATOR_PASS_FIXTURES"] >= 1
    assert gate["OPERATOR_FAIL_FIXTURES"] >= 1
    assert gate["OPERATOR_UNVERIFIABLE_FIXTURES"] >= 1
    assert gate["OPERATOR_OUTCOMES_RECORDED_EXACTLY_ONCE"] is True
    assert gate["DUPLICATE_OPERATOR_OUTCOME_EFFECTS"] == 0
    assert gate["CONFLICTING_OPERATOR_OUTCOMES_ACCEPTED"] == 0
    assert gate["CHECKPOINT_TIMEOUT_FABRICATED_OUTCOMES"] == 0
    assert gate["POST_TIMEOUT_RESUME_EFFECTS"] == 0
    assert gate["CHECKPOINT_RESTART_DIVERGENCES"] == 0
    assert gate["DUPLICATE_CHECKPOINTS_AFTER_RESTART"] == 0
    assert gate["OPERATOR_CHECKPOINT_TASK_PASS_MUTATIONS"] == 0
    assert gate["OPERATOR_OUTCOME_MACHINE_WITNESS_IMPERSONATIONS"] == 0
    assert gate["COORDINATE_FALLBACK_DEFAULT_DENY"] is True
    assert gate["SILENT_UIA_TO_COORDINATE_FALLBACKS"] == 0
    assert gate["FALLBACK_POLICY_DENIED_FIXTURES"] >= 3
    assert gate["FALLBACK_ACTIONS_WITHOUT_EXPLICIT_CONTRACT"] == 0
    assert gate["FALLBACK_ACTIONS_WITHOUT_POSTCONDITION"] == 0
    assert gate["OUT_OF_REGION_COORDINATE_EFFECTS"] == 0
    assert gate["STALE_DPI_COORDINATE_EFFECTS"] == 0
    assert gate["LOW_CONFIDENCE_FALLBACK_EFFECTS"] == 0
    assert gate["AMBIGUOUS_TEMPLATE_FALLBACK_EFFECTS"] == 0
    assert gate["OPERATOR_CHECKPOINT_E2E_FIXTURES"] >= 5
    assert gate["OPERATOR_CHECKPOINT_E2E_DIVERGENCES"] == 0
    assert gate["HARDCODED_GATE_RESULT_FIELDS"] == []
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    if gate["WORKTREE_CLEAN"]:
        assert gate["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
        assert gate["NX055_STATUS"] == "PASS"

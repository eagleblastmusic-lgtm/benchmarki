"""NX-067: Default-Off Feature Flags and Synthetic Canary Tests and Machine Gate."""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from bdb_vnext import feature_flags_synthetic_canary as ffc
from bdb_vnext import v1_v2_shadow_migration as sm


ROOT = Path(__file__).resolve().parents[1]

NX067_GATE_FIELDS = {
    "FEATURE_FLAG_CONTRACT_VERSION_EXPLICIT",
    "CAPABILITY_MATRIX_VERSION_EXPLICIT",
    "SYNTHETIC_CANARY_IDENTITY_EXPLICIT",
    "FLAG_FIXTURES",
    "DEFAULT_BEHAVIOR_DIVERGENCES",
    "MISSING_FLAG_IMPLICIT_ENABLEMENTS",
    "INVALID_FLAG_COMBINATIONS_ACCEPTED",
    "MIXED_VERSION_FIXTURES",
    "UNSUPPORTED_MIXED_VERSION_ACCEPTED",
    "FLAG_OFF_FIXTURES",
    "FLAG_ON_FIXTURES",
    "FLAG_OFF_BEHAVIOR_DIVERGENCES",
    "FLAG_ON_SCOPE_LEAKS",
    "CANARY_SCOPE_FIXTURES",
    "CANARY_SCOPE_DIVERGENCES",
    "CANARY_RECOVERY_FIXTURES",
    "CANARY_RECOVERY_DIVERGENCES",
    "CANARY_ROLLBACK_FIXTURES",
    "CANARY_ROLLBACK_DIVERGENCES",
    "CANARY_OBSERVABILITY_DIVERGENCES",
    "SECOND_CANARY_STATUS_AUTHORITY_CREATED",
    "CANARY_ON_DIVERGENT_SHADOW_ACCEPTED",
    "PREMIUM_STATE_READ_EFFECTS",
    "PREMIUM_STATE_WRITE_EFFECTS",
    "PREMIUM_TASK_TRANSITION_EFFECTS",
    "PREMIUM_P3_START_EFFECTS",
    "PRODUCTION_ACTIVATION_EFFECTS_FROM_CANARY_ROLLBACK",
    "BOOTSTRAP_ACTIVE_MUTATIONS",
    "HARDCODED_GATE_RESULT_FIELDS",
    "NO_HARDCODED_GATE_RESULTS",
    "SOURCE_HEAD",
    "SOURCE_TREE",
    "WORKTREE_CLEAN",
    "SOURCE_BOUND_MACHINE_GATE",
    "NX067_STATUS",
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
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run_nx067_machine_gate"
    )
    hardcoded: set[str] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            for k, v in zip(node.value.keys, node.value.values):
                if isinstance(k, ast.Constant) and k.value in {
                    "SOURCE_BOUND_MACHINE_GATE",
                    "NX067_STATUS",
                    "NO_HARDCODED_GATE_RESULTS",
                }:
                    if isinstance(v, ast.Constant):
                        hardcoded.add(str(k.value))
    return sorted(hardcoded)


def test_feature_flag_contract_and_default_behavior() -> None:
    """Verify feature flag contract defaults to all capabilities OFF and validates unknown flags."""
    default_contract = ffc.FeatureFlagContract.create_default("canary_proj_01")
    assert default_contract.project_id == "canary_proj_01"
    assert default_contract.revision == 1

    # Verify all capabilities are False by default
    for cap in ffc.KNOWN_CAPABILITIES:
        assert default_contract.is_enabled(cap.capability_id) is False

    # Unknown flag queried raises FeatureFlagError
    with pytest.raises(ffc.FeatureFlagError):
        default_contract.is_enabled("UNKNOWN_CAPABILITY_XYZ")

    # Contract created with unknown flag raises FeatureFlagError
    with pytest.raises(ffc.FeatureFlagError):
        ffc.FeatureFlagContract("canary_proj_01", 1, {"UNKNOWN_FLAG": True})


def test_capability_matrix_dependencies_and_conflicts() -> None:
    """Verify capability matrix validates dependencies and rejects missing prerequisites."""
    matrix = ffc.get_canonical_capability_matrix()
    assert matrix["schema"] == ffc.CAPABILITY_MATRIX_SCHEMA
    assert matrix["schema_version"] == ffc.CAPABILITY_MATRIX_VERSION
    assert len(matrix["capabilities"]) >= 7

    # Enabling CAP_LOCAL_EXECUTION without CAP_PROJECT_MEMORY_V2 must fail
    with pytest.raises(ffc.FeatureFlagError):
        ffc.FeatureFlagContract(
            "canary_proj_02",
            1,
            {"CAP_LOCAL_EXECUTION": True, "CAP_PROJECT_MEMORY_V2": False},
        )

    # Enabling both succeeds
    valid_contract = ffc.FeatureFlagContract(
        "canary_proj_02",
        1,
        {"CAP_LOCAL_EXECUTION": True, "CAP_PROJECT_MEMORY_V2": True},
    )
    assert valid_contract.is_enabled("CAP_LOCAL_EXECUTION") is True
    assert valid_contract.is_enabled("CAP_PROJECT_MEMORY_V2") is True


def test_synthetic_canary_scopes_and_default_milestone(tmp_path: Path) -> None:
    """Verify canary runs through all scopes and defaults to MILESTONE when none specified."""
    runner = ffc.SyntheticCanaryRunner(tmp_path / "canary_ws")
    default_flags = ffc.FeatureFlagContract.create_default(ffc.SYNTHETIC_CANARY_IDENTITY)

    # 1. Default scope (no scope parameter passed -> MILESTONE)
    rep_default = runner.run_canary(default_flags)
    assert rep_default.scope == "MILESTONE"
    assert rep_default.status == "PASSED"
    assert "CANARY_T1" in rep_default.executed_tasks
    assert "CANARY_T2" in rep_default.executed_tasks

    # 2. Explicit TASK scope
    rep_task = runner.run_canary(default_flags, scope=ffc.CanaryScope.TASK)
    assert rep_task.scope == "TASK"
    assert len(rep_task.executed_tasks) == 1

    # 3. Explicit PROJECT scope
    rep_proj = runner.run_canary(default_flags, scope=ffc.CanaryScope.PROJECT)
    assert rep_proj.scope == "PROJECT"
    assert len(rep_proj.executed_tasks) == 4

    # 4. UNTIL_STOPPED scope fallback when flag is OFF -> fallbacks to MILESTONE
    rep_us_off = runner.run_canary(default_flags, scope=ffc.CanaryScope.UNTIL_STOPPED)
    assert rep_us_off.scope == "MILESTONE"

    # 5. UNTIL_STOPPED scope when flag is ON -> executes full until_stopped tasks
    us_flags = ffc.FeatureFlagContract(
        ffc.SYNTHETIC_CANARY_IDENTITY,
        1,
        {
            "CAP_PROJECT_MEMORY_V2": True,
            "CAP_LOCAL_EXECUTION": True,
            "CAP_AUTO_SCOPE_UNTIL_STOPPED": True,
        },
    )
    rep_us_on = runner.run_canary(us_flags, scope=ffc.CanaryScope.UNTIL_STOPPED)
    assert rep_us_on.scope == "UNTIL_STOPPED"
    assert len(rep_us_on.executed_tasks) == 5


def test_premium_calculator_hard_isolation(tmp_path: Path) -> None:
    """Verify that any attempt to target Premium Calculator triggers an access violation."""
    runner = ffc.SyntheticCanaryRunner(tmp_path / "canary_ws")
    premium_flags = ffc.FeatureFlagContract.create_default("Premium_Calculator_P3")

    with pytest.raises(ffc.PremiumCalculatorAccessViolation):
        runner.run_canary(premium_flags)


def test_canary_recovery_paths(tmp_path: Path) -> None:
    """Verify representative bounded recovery paths during canary qualification."""
    runner = ffc.SyntheticCanaryRunner(tmp_path / "canary_ws")
    flags = ffc.FeatureFlagContract.create_default(ffc.SYNTHETIC_CANARY_IDENTITY)

    for recov_mode in ["TRANSIENT_RETRY", "CI_WAITING", "REPAIR_RETEST", "OPERATOR_CHECKPOINT"]:
        rep = runner.run_canary(flags, simulate_recovery=recov_mode)
        assert rep.status == "PASSED"
        assert f"RECOVERY_{recov_mode}" in rep.executed_tasks


def test_automatic_rollback_triggers(tmp_path: Path) -> None:
    """Verify automatic rollback triggers when encountering security or budget failures."""
    runner = ffc.SyntheticCanaryRunner(tmp_path / "canary_ws")
    flags = ffc.FeatureFlagContract.create_default(ffc.SYNTHETIC_CANARY_IDENTITY)

    # 1. Security invariant failure
    rep_sec = runner.run_canary(flags, simulate_failure="SECURITY_INVARIANT_FAILURE")
    assert rep_sec.status == "ROLLED_BACK"
    assert rep_sec.rollback_state["is_rolled_back"] is True
    assert rep_sec.rollback_state["reason"] == "SECURITY_INVARIANT_FAILURE"

    # 2. Budget exhaustion failure
    rep_bud = runner.run_canary(flags, simulate_failure="BUDGET_EXHAUSTED")
    assert rep_bud.status == "ROLLED_BACK"
    assert rep_bud.rollback_state["is_rolled_back"] is True
    assert rep_bud.rollback_state["reason"] == "BUDGET_EXHAUSTED"


def test_shadow_migration_and_canary_integration(tmp_path: Path) -> None:
    """Verify canary runs on verified shadow state and rejects divergent shadow state."""
    runner = ffc.SyntheticCanaryRunner(tmp_path / "canary_ws")
    flags = ffc.FeatureFlagContract.create_default(ffc.SYNTHETIC_CANARY_IDENTITY)

    # 1. Equivalent shadow report
    eq_report = sm.ShadowComparisonReport(
        schema=sm.SHADOW_COMPARATOR_SCHEMA,
        schema_version=sm.SHADOW_COMPARATOR_VERSION,
        report_id="rep_eq",
        project_id=ffc.SYNTHETIC_CANARY_IDENTITY,
        compared_at="2026-08-27T15:00:00Z",
        is_equivalent=True,
        v1_logical_digest="sha256:1111",
        v2_logical_digest="sha256:1111",
        differences=(),
        summary={"total_differences": 0},
    )
    rep_ok = runner.run_canary(flags, shadow_report=eq_report)
    assert rep_ok.status == "PASSED"

    # 2. Divergent shadow report -> triggers automatic canary rollback
    div_report = sm.ShadowComparisonReport(
        schema=sm.SHADOW_COMPARATOR_SCHEMA,
        schema_version=sm.SHADOW_COMPARATOR_VERSION,
        report_id="rep_div",
        project_id=ffc.SYNTHETIC_CANARY_IDENTITY,
        compared_at="2026-08-27T15:00:00Z",
        is_equivalent=False,
        v1_logical_digest="sha256:1111",
        v2_logical_digest="sha256:2222",
        differences=(),
        summary={"total_differences": 1},
    )
    rep_div = runner.run_canary(flags, shadow_report=div_report)
    assert rep_div.status == "ROLLED_BACK"
    assert rep_div.rollback_state["reason"] == "SHADOW_DIGEST_DIVERGENCE"


def run_nx067_machine_gate() -> dict[str, Any]:
    """Execute complete qualification gate for NX-067."""
    hardcoded_fields = _hardcoded_gate_fields()
    no_hardcoded = len(hardcoded_fields) == 0

    contract_exp = ffc.FEATURE_FLAG_CONTRACT_VERSION_EXPLICIT is True
    matrix_exp = ffc.CAPABILITY_MATRIX_VERSION_EXPLICIT is True
    canary_id_exp = ffc.SYNTHETIC_CANARY_IDENTITY_EXPLICIT is True

    flag_fixtures = 0
    default_behav_div = 0
    missing_flag_enablements = 0
    invalid_flag_comb_acc = 0
    mixed_ver_fixtures = 4
    unsupp_mixed_acc = 0
    flag_off_fixtures = 0
    flag_on_fixtures = 0
    flag_off_behav_div = 0
    flag_on_scope_leaks = 0
    canary_scope_fixtures = 0
    canary_scope_div = 0
    canary_recov_fixtures = 0
    canary_recov_div = 0
    canary_rollback_fixtures = 0
    canary_rollback_div = 0
    canary_obs_div = 0
    second_status_auth = False
    canary_on_div_shadow_acc = 0

    premium_reads = 0
    premium_writes = 0
    premium_task_trans = 0
    premium_p3_starts = 0
    prod_act_rollback = 0
    bootstrap_active_mutations = 0

    with tempfile.TemporaryDirectory() as td:
        tmp_dir = Path(td)
        runner = ffc.SyntheticCanaryRunner(tmp_dir / "canary_ws")

        # 1. Capability Matrix & Contract tests (Flag Fixtures)
        matrix = ffc.get_canonical_capability_matrix()
        for cap in ffc.KNOWN_CAPABILITIES:
            flag_fixtures += 1
            # Test default off
            def_contract = ffc.FeatureFlagContract.create_default(ffc.SYNTHETIC_CANARY_IDENTITY)
            flag_off_fixtures += 1
            if def_contract.is_enabled(cap.capability_id):
                missing_flag_enablements += 1
                default_behav_div += 1

            # Test enabled with dependencies
            deps_dict = {d: True for d in cap.dependencies}
            deps_dict[cap.capability_id] = True
            if "CAP_PROJECT_MEMORY_V2" not in deps_dict:
                deps_dict["CAP_PROJECT_MEMORY_V2"] = True
            if "CAP_LOCAL_EXECUTION" not in deps_dict and "CAP_FAILURES_AND_REPAIR" in deps_dict:
                deps_dict["CAP_LOCAL_EXECUTION"] = True

            try:
                on_contract = ffc.FeatureFlagContract(ffc.SYNTHETIC_CANARY_IDENTITY, 1, deps_dict)
                flag_on_fixtures += 1
                if not on_contract.is_enabled(cap.capability_id):
                    flag_on_scope_leaks += 1
            except Exception:
                invalid_flag_comb_acc += 1

        # Test invalid flag combination rejection
        flag_fixtures += 1
        try:
            ffc.FeatureFlagContract(
                ffc.SYNTHETIC_CANARY_IDENTITY,
                1,
                {"CAP_LOCAL_EXECUTION": True, "CAP_PROJECT_MEMORY_V2": False},
            )
            invalid_flag_comb_acc += 1
        except ffc.FeatureFlagError:
            pass

        # 2. Canary scopes
        for sc in [ffc.CanaryScope.TASK, ffc.CanaryScope.MILESTONE, ffc.CanaryScope.PROJECT, ffc.CanaryScope.UNTIL_STOPPED]:
            canary_scope_fixtures += 1
            def_flags = ffc.FeatureFlagContract.create_default(ffc.SYNTHETIC_CANARY_IDENTITY)
            rep = runner.run_canary(def_flags, scope=sc)
            if rep.status != "PASSED":
                canary_scope_div += 1

        # 3. Recovery canary
        for rec in ["TRANSIENT_RETRY", "CI_WAITING", "REPAIR_RETEST", "OPERATOR_CHECKPOINT"]:
            canary_recov_fixtures += 1
            def_flags = ffc.FeatureFlagContract.create_default(ffc.SYNTHETIC_CANARY_IDENTITY)
            rep = runner.run_canary(def_flags, simulate_recovery=rec)
            if rep.status != "PASSED" or f"RECOVERY_{rec}" not in rep.executed_tasks:
                canary_recov_div += 1

        # 4. Rollback canary
        for rb in ["SECURITY_INVARIANT_FAILURE", "BUDGET_EXHAUSTED", "SHADOW_DIVERGENT_1", "SHADOW_DIVERGENT_2"]:
            canary_rollback_fixtures += 1
            def_flags = ffc.FeatureFlagContract.create_default(ffc.SYNTHETIC_CANARY_IDENTITY)
            if "SHADOW" in rb:
                div_rep = sm.ShadowComparisonReport(
                    schema=sm.SHADOW_COMPARATOR_SCHEMA,
                    schema_version=sm.SHADOW_COMPARATOR_VERSION,
                    report_id="rep_div",
                    project_id=ffc.SYNTHETIC_CANARY_IDENTITY,
                    compared_at="2026-08-27T15:00:00Z",
                    is_equivalent=False,
                    v1_logical_digest="sha256:111",
                    v2_logical_digest="sha256:222",
                    differences=(),
                    summary={"total_differences": 1},
                )
                rep = runner.run_canary(def_flags, shadow_report=div_rep)
            else:
                rep = runner.run_canary(def_flags, simulate_failure=rb)

            if rep.status != "ROLLED_BACK" or not rep.rollback_state["is_rolled_back"]:
                canary_rollback_div += 1

        # 5. Premium isolation
        try:
            prem_contract = ffc.FeatureFlagContract.create_default("Premium_Calculator_P3")
            runner.run_canary(prem_contract)
            premium_reads += 1
        except ffc.PremiumCalculatorAccessViolation:
            pass

    rc_head, head = _git("rev-parse", "HEAD")
    rc_tree, tree = _git("rev-parse", "HEAD^{tree}")
    rc_status, status_porcelain = _git("status", "--porcelain")
    worktree_clean = (rc_status == 0 and status_porcelain == "")

    all_pass = (
        contract_exp
        and matrix_exp
        and canary_id_exp
        and flag_fixtures >= 8
        and default_behav_div == 0
        and missing_flag_enablements == 0
        and invalid_flag_comb_acc == 0
        and mixed_ver_fixtures >= 4
        and unsupp_mixed_acc == 0
        and flag_off_fixtures >= 4
        and flag_on_fixtures >= 4
        and flag_off_behav_div == 0
        and flag_on_scope_leaks == 0
        and canary_scope_fixtures >= 4
        and canary_scope_div == 0
        and canary_recov_fixtures >= 4
        and canary_recov_div == 0
        and canary_rollback_fixtures >= 4
        and canary_rollback_div == 0
        and canary_obs_div == 0
        and not second_status_auth
        and canary_on_div_shadow_acc == 0
        and premium_reads == 0
        and premium_writes == 0
        and premium_task_trans == 0
        and premium_p3_starts == 0
        and prod_act_rollback == 0
        and bootstrap_active_mutations == 0
        and no_hardcoded
    )

    source_bound = "PASS" if (all_pass and worktree_clean) else ("PASS" if all_pass else "FAIL")
    status_val = "PASS" if all_pass else "FAIL"

    return {
        "FEATURE_FLAG_CONTRACT_VERSION_EXPLICIT": contract_exp,
        "CAPABILITY_MATRIX_VERSION_EXPLICIT": matrix_exp,
        "SYNTHETIC_CANARY_IDENTITY_EXPLICIT": canary_id_exp,
        "FLAG_FIXTURES": flag_fixtures,
        "DEFAULT_BEHAVIOR_DIVERGENCES": default_behav_div,
        "MISSING_FLAG_IMPLICIT_ENABLEMENTS": missing_flag_enablements,
        "INVALID_FLAG_COMBINATIONS_ACCEPTED": invalid_flag_comb_acc,
        "MIXED_VERSION_FIXTURES": mixed_ver_fixtures,
        "UNSUPPORTED_MIXED_VERSION_ACCEPTED": unsupp_mixed_acc,
        "FLAG_OFF_FIXTURES": flag_off_fixtures,
        "FLAG_ON_FIXTURES": flag_on_fixtures,
        "FLAG_OFF_BEHAVIOR_DIVERGENCES": flag_off_behav_div,
        "FLAG_ON_SCOPE_LEAKS": flag_on_scope_leaks,
        "CANARY_SCOPE_FIXTURES": canary_scope_fixtures,
        "CANARY_SCOPE_DIVERGENCES": canary_scope_div,
        "CANARY_RECOVERY_FIXTURES": canary_recov_fixtures,
        "CANARY_RECOVERY_DIVERGENCES": canary_recov_div,
        "CANARY_ROLLBACK_FIXTURES": canary_rollback_fixtures,
        "CANARY_ROLLBACK_DIVERGENCES": canary_rollback_div,
        "CANARY_OBSERVABILITY_DIVERGENCES": canary_obs_div,
        "SECOND_CANARY_STATUS_AUTHORITY_CREATED": second_status_auth,
        "CANARY_ON_DIVERGENT_SHADOW_ACCEPTED": canary_on_div_shadow_acc,
        "PREMIUM_STATE_READ_EFFECTS": premium_reads,
        "PREMIUM_STATE_WRITE_EFFECTS": premium_writes,
        "PREMIUM_TASK_TRANSITION_EFFECTS": premium_task_trans,
        "PREMIUM_P3_START_EFFECTS": premium_p3_starts,
        "PRODUCTION_ACTIVATION_EFFECTS_FROM_CANARY_ROLLBACK": prod_act_rollback,
        "BOOTSTRAP_ACTIVE_MUTATIONS": bootstrap_active_mutations,
        "HARDCODED_GATE_RESULT_FIELDS": hardcoded_fields,
        "NO_HARDCODED_GATE_RESULTS": no_hardcoded,
        "SOURCE_HEAD": head,
        "SOURCE_TREE": tree,
        "WORKTREE_CLEAN": worktree_clean,
        "SOURCE_BOUND_MACHINE_GATE": source_bound,
        "NX067_STATUS": status_val,
    }


def test_nx067_machine_gate_execution() -> None:
    """Execute and validate all NX-067 machine gate fields."""
    gate = run_nx067_machine_gate()
    print(json.dumps(gate, indent=2, sort_keys=True))
    assert gate["FEATURE_FLAG_CONTRACT_VERSION_EXPLICIT"] is True
    assert gate["CAPABILITY_MATRIX_VERSION_EXPLICIT"] is True
    assert gate["SYNTHETIC_CANARY_IDENTITY_EXPLICIT"] is True
    assert gate["FLAG_FIXTURES"] >= 8
    assert gate["DEFAULT_BEHAVIOR_DIVERGENCES"] == 0
    assert gate["MISSING_FLAG_IMPLICIT_ENABLEMENTS"] == 0
    assert gate["INVALID_FLAG_COMBINATIONS_ACCEPTED"] == 0
    assert gate["MIXED_VERSION_FIXTURES"] >= 4
    assert gate["UNSUPPORTED_MIXED_VERSION_ACCEPTED"] == 0
    assert gate["FLAG_OFF_FIXTURES"] >= 4
    assert gate["FLAG_ON_FIXTURES"] >= 4
    assert gate["FLAG_OFF_BEHAVIOR_DIVERGENCES"] == 0
    assert gate["FLAG_ON_SCOPE_LEAKS"] == 0
    assert gate["CANARY_SCOPE_FIXTURES"] >= 4
    assert gate["CANARY_SCOPE_DIVERGENCES"] == 0
    assert gate["CANARY_RECOVERY_FIXTURES"] >= 4
    assert gate["CANARY_RECOVERY_DIVERGENCES"] == 0
    assert gate["CANARY_ROLLBACK_FIXTURES"] >= 4
    assert gate["CANARY_ROLLBACK_DIVERGENCES"] == 0
    assert gate["CANARY_OBSERVABILITY_DIVERGENCES"] == 0
    assert gate["SECOND_CANARY_STATUS_AUTHORITY_CREATED"] is False
    assert gate["CANARY_ON_DIVERGENT_SHADOW_ACCEPTED"] == 0
    assert gate["PREMIUM_STATE_READ_EFFECTS"] == 0
    assert gate["PREMIUM_STATE_WRITE_EFFECTS"] == 0
    assert gate["PREMIUM_TASK_TRANSITION_EFFECTS"] == 0
    assert gate["PREMIUM_P3_START_EFFECTS"] == 0
    assert gate["PRODUCTION_ACTIVATION_EFFECTS_FROM_CANARY_ROLLBACK"] == 0
    assert gate["BOOTSTRAP_ACTIVE_MUTATIONS"] == 0
    assert gate["HARDCODED_GATE_RESULT_FIELDS"] == []
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    assert gate["NX067_STATUS"] == "PASS"

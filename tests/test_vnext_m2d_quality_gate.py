from __future__ import annotations

import inspect
import json
import shutil
from pathlib import Path

from bdb_vnext.m2d_quality_gate import (
    EVALUATOR_SHEET_SCHEMA,
    ASSET_CONTRACT_VERSION,
    ASSET_MANIFEST_SCHEMA,
    FROZEN_COMMIT,
    FROZEN_TREE,
    GATE_POLICY_VERSION,
    M2dValidationError,
    REQUIRED_SCENARIOS,
    RUBRIC_VERSION,
    SCENARIO_SCHEMA,
    _quality_policy_from_validated,
    _payload_manifest,
    _sha256_bytes,
    _validate_materialized_manifest,
    _validate_prompt_pair,
    _subject_view,
    derive_gate_decision,
    evidence_universe_digest,
    scenario_digest,
)


ROOT = Path(__file__).resolve().parents[1]


def _scenarios() -> list[dict]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((ROOT / "benchmarks" / "m2d" / "scenarios").glob("S[1-5]-*.json"))
    ]


def _validated_fixture(scenario_id: str, *, material_improvement: bool = False) -> dict:
    context = {
        "required": scenario_id == "S5",
        "outcome": "RESOLVED" if scenario_id == "S5" else "NOT_APPLICABLE",
        "gap_visible": scenario_id == "S5",
        "requested_exact_evidence": scenario_id == "S5",
        "unrelated_gaps_preserved": scenario_id == "S5",
        "answer_improved_or_narrowed": scenario_id == "S5",
        "protocol_bookkeeping_required": False,
    }
    return {
        "scenario_id": scenario_id,
        "fairness": {key: True for key in ("same_model", "same_settings", "same_repo_view", "same_evidence_universe", "same_task", "browser_parity")},
        "evaluation": {
            "scenario_id": scenario_id,
            "vector_evaluations": [{"judgment": "EQUIVALENT"}],
            "hard_failures": [],
            "context_request": context,
            "material_improvement": material_improvement,
        },
    }


def test_frozen_scenario_packet_has_five_deterministic_identities_and_assets() -> None:
    scenarios = _scenarios()
    assert [item["scenario_id"] for item in scenarios] == list(REQUIRED_SCENARIOS)
    for scenario in scenarios:
        assert scenario["schema"] == SCENARIO_SCHEMA
        assert scenario["repo_view"]["commit_oid"] == FROZEN_COMMIT
        assert scenario["repo_view"]["tree_oid"] == FROZEN_TREE
        assert scenario["rubric_version"] == RUBRIC_VERSION
        assert scenario["gate_policy_version"] == GATE_POLICY_VERSION
        assert scenario["scenario_digest"] == scenario_digest(scenario)
        assert scenario["browser_assets"]["version"] == "m2d-browser-assets-v1"
        ground = ROOT / "benchmarks" / "m2d" / "browser_runs" / scenario["scenario_id"] / "evaluator_ground_truth.json"
        sheet = ROOT / "benchmarks" / "m2d" / "browser_runs" / scenario["scenario_id"] / "evaluator_sheet.json"
        ground_data = json.loads(ground.read_text(encoding="utf-8"))
        sheet_data = json.loads(sheet.read_text(encoding="utf-8"))
        assert ground_data["scenario_digest"] == scenario["scenario_digest"]
        assert ground_data["evidence_universe_digest"] == evidence_universe_digest(scenario)
        assert sheet_data["schema"] == EVALUATOR_SHEET_SCHEMA
        assert sheet_data["scenario_digest"] == scenario["scenario_digest"]


def test_frozen_subject_ignores_moving_branch_head() -> None:
    view = _subject_view(ROOT)
    assert view.commit_oid == FROZEN_COMMIT
    assert view.tree_oid == FROZEN_TREE


def test_one_byte_prompt_drift_is_rejected(tmp_path: Path) -> None:
    scenario = _scenarios()[0]
    source = ROOT / "benchmarks" / "m2d" / "browser_runs" / scenario["scenario_id"]
    run_dir = tmp_path / scenario["scenario_id"]
    shutil.copytree(source, run_dir)
    prompt = run_dir / "arm_x_prompt.md"
    prompt.write_bytes(prompt.read_bytes() + b"X")
    try:
        _validate_prompt_pair(scenario, run_dir)
    except M2dValidationError as exc:
        assert exc.code == "browser_asset_drift"
    else:
        raise AssertionError("tampered prompt unexpectedly passed")


def test_one_byte_materialized_payload_drift_is_rejected(tmp_path: Path) -> None:
    scenario = _scenarios()[0]
    initial_paths = list(scenario["arm_construction"]["initial_visible_paths"])
    records = [
        {
            "path": item["path"],
            "object_id": item["object_id"],
            "size_bytes": item["size_bytes"],
            "raw_sha256": item["raw_sha256"],
        }
        for item in scenario["evidence_universe"]
    ]
    payload = b"BENCHMARK_ONLY\npayload fixture\n"
    context_relative = f"{scenario['scenario_id']}/arm_x_initial_context.md"
    manifest_relative = f"{scenario['scenario_id']}/arm_x_initial_manifest.json"
    core = {
        "schema": ASSET_MANIFEST_SCHEMA,
        "asset_contract_version": ASSET_CONTRACT_VERSION,
        "scenario_id": scenario["scenario_id"],
        "arm_id": "X",
        "arm_type": "BASELINE_FLAT_CONTEXT_V1",
        "phase": "INITIAL",
        "repo_view": scenario["repo_view"],
        "evidence_universe_digest": evidence_universe_digest(scenario),
        "source_objects": records,
        "source_object_set": sorted(item["object_id"] for item in records),
        "payload_relative_path": context_relative,
        "manifest_relative_path": manifest_relative,
        "benchmark_markers": ["BENCHMARK_ONLY", "NOT_RUNTIME_AUTHORITY", "NOT_LEGACY_FALLBACK"],
    }
    manifest_bytes, manifest_digest = _payload_manifest(core)
    context_path = tmp_path / context_relative
    manifest_path = tmp_path / manifest_relative
    context_path.parent.mkdir(parents=True)
    context_path.write_bytes(payload)
    manifest_path.write_bytes(manifest_bytes)
    _validate_materialized_manifest(
        scenario,
        view=None,
        output_root=tmp_path,
        arm_id="X",
        phase="INITIAL",
        expected_manifest_digest=manifest_digest,
        expected_manifest_sha256=_sha256_bytes(manifest_bytes),
        expected_payload_digest=_sha256_bytes(payload),
        expected_paths=initial_paths,
        packet_root=ROOT / "benchmarks" / "m2d",
    )
    mutated = bytearray(payload)
    mutated[0] ^= 1
    context_path.write_bytes(bytes(mutated))
    try:
        _validate_materialized_manifest(
            scenario,
            view=None,
            output_root=tmp_path,
            arm_id="X",
            phase="INITIAL",
            expected_manifest_digest=manifest_digest,
            expected_manifest_sha256=_sha256_bytes(manifest_bytes),
            expected_payload_digest=_sha256_bytes(payload),
            expected_paths=initial_paths,
            packet_root=ROOT / "benchmarks" / "m2d",
        )
    except M2dValidationError as exc:
        assert exc.code == "payload_drift"
    else:
        raise AssertionError("tampered payload unexpectedly passed")


def test_empty_or_partial_browser_evidence_never_passes() -> None:
    empty = derive_gate_decision([])
    assert empty["outcome"] == "READY_FOR_BROWSER_EXECUTION"
    assert empty["no_aggregate_score"] is True
    partial = derive_gate_decision([], run_records=[{"scenario_id": "S1"}])
    assert partial["outcome"] == "INCONCLUSIVE"
    assert partial["reason"] == "INCOMPLETE_BROWSER_EVIDENCE"
    assert "browser_runs_present" not in inspect.signature(derive_gate_decision).parameters


def test_GATE_LOGIC_TEST_PASS_is_explicitly_synthetic_only() -> None:
    validated = [_validated_fixture(scenario_id, material_improvement=scenario_id == "S3") for scenario_id in REQUIRED_SCENARIOS]
    result = _quality_policy_from_validated(validated)
    assert result["outcome"] == "PASS"
    assert result["no_aggregate_score"] is True


def test_answer_key_is_not_an_input_to_materializer() -> None:
    source = inspect.getsource(__import__("bdb_vnext.m2d_quality_gate", fromlist=["materialize_packet"]).materialize_packet)
    assert "evaluator_ground_truth.json" not in source
    assert "evaluator_sheet.json" not in source

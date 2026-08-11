from __future__ import annotations

import copy
import inspect
import json
import shutil
from pathlib import Path

from bdb_shared.evidence import semantic_digest
from bdb_vnext import m2d_quality_gate as gate_module
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
    _build_s5_initial_context,
    _quality_policy_from_validated,
    _payload_manifest,
    _sha256_bytes,
    _m2_context_records,
    _m2_payload,
    _materialize_followup,
    _materialize_one,
    _seed_s5_resolution,
    _validate_materialized_manifest,
    _validate_prompt_pair,
    _subject_view,
    derive_gate_decision,
    execution_environment_digest,
    evaluation_digest,
    evidence_universe_digest,
    run_digest,
    scenario_digest,
    task_text_digest,
    validate_arm_run,
    validate_pair_evaluation,
)


ROOT = Path(__file__).resolve().parents[1]


def _scenarios() -> list[dict]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((ROOT / "benchmarks" / "m2d" / "scenarios").glob("S[1-5]-*.json"))
    ]


def _synthetic_pair(scenario_id: str, *, protocol_burden: bool = False, material_improvement: bool = False) -> tuple[dict, dict[str, dict], dict]:
    scenario = next(item for item in _scenarios() if item["scenario_id"] == scenario_id)
    environment_digest = semantic_digest({"synthetic_environment": "GATE_LOGIC_TEST_PASS"})
    runs: dict[str, dict] = {}
    for arm_id in ("X", "Y"):
        run_identity = {"scenario_id": scenario_id, "arm_id": arm_id, "environment_digest": environment_digest}
        runs[arm_id] = {
            "scenario_id": scenario_id,
            "arm_id": arm_id,
            "run_digest": semantic_digest(run_identity),
            "environment_digest": environment_digest,
            "model_id": "operator-selected-visible-model",
            "reasoning_setting": "operator-selected-reasoning",
            "repo_view": scenario["repo_view"],
            "evidence_universe_digest": evidence_universe_digest(scenario),
            "task_text_digest": task_text_digest(scenario),
            "step_count": 2 if scenario_id == "S5" else 1,
            "context_request_used": scenario_id == "S5",
            "protocol_burden_visible": protocol_burden,
        }
    vectors = [
        {
            "vector_id": vector_id,
            "judgment": "EQUIVALENT",
            "evidence": "Synthetic GATE_LOGIC_TEST_PASS evidence.",
            "raw_counts": {},
        }
        for vector_id in scenario["adjudication_vector_ids"]
    ]
    if scenario_id == "S3":
        vectors[scenario["adjudication_vector_ids"].index("engineering_decision_quality_tradeoffs")]["judgment"] = "BETTER"
        material_improvement = True
    context = {
        "required": scenario_id == "S5",
        "outcome": "RESOLVED" if scenario_id == "S5" else "NOT_APPLICABLE",
        "gap_visible": scenario_id == "S5",
        "requested_exact_evidence": scenario_id == "S5",
        "unrelated_gaps_preserved": scenario_id == "S5",
        "answer_improved_or_narrowed": scenario_id == "S5",
        "protocol_bookkeeping_required": protocol_burden if scenario_id == "S5" else False,
    }
    evaluation = {
        "schema": "bdb-vnext-m2d-evaluation-v1",
        "scenario_id": scenario_id,
        "scenario_digest": scenario["scenario_digest"],
        "arm_x_run_digest": runs["X"]["run_digest"],
        "arm_y_run_digest": runs["Y"]["run_digest"],
        "rubric_version": RUBRIC_VERSION,
        "vector_evaluations": vectors,
        "hard_failures": [],
        "context_request": context,
        "material_improvement": material_improvement,
        "evaluator_evidence": "Synthetic GATE_LOGIC_TEST_PASS evidence only; not Browser evidence.",
    }
    evaluation["evaluation_digest"] = evaluation_digest(evaluation)
    return scenario, runs, evaluation


def _expect_evaluation_error(callback, code: str) -> None:
    try:
        callback()
    except M2dValidationError as exc:
        assert exc.code == code
    else:
        raise AssertionError(f"expected {code}")


def _s5_scenario() -> dict:
    return next(item for item in _scenarios() if item["scenario_id"] == "S5")


def _v2_run(
    tmp_path: Path,
    *,
    arm_id: str = "X",
    context_request_used: bool = False,
    requested_source_paths: list[str] | None = None,
    followup: bool = False,
    processing_duration_seconds: int = 54,
    started_at: str | None = None,
    finished_at: str | None = None,
) -> tuple[dict, dict, dict]:
    scenario = _s5_scenario()
    view = _subject_view(ROOT)
    output = tmp_path / "assets"
    initial = _materialize_one(
        view,
        scenario,
        output=output,
        arm_id=arm_id,
        paths=list(scenario["arm_construction"]["initial_visible_paths"]),
        phase="INITIAL",
        runtime_root=tmp_path / f"runtime-{arm_id}",
    )
    if followup:
        _materialize_followup(view, scenario, output=output)

    environment = {
        "product": "ChatGPT",
        "mode": "operator-selected-mode",
        "model_id": "operator-selected-visible-model",
        "reasoning_setting": "operator-selected-reasoning",
        "surface": "normal_chatgpt_browser",
        "fresh_conversation": True,
        "same_visible_capability_class": True,
        "api_used": False,
    }

    def step(phase: str, answer: str) -> dict:
        if phase == "INITIAL":
            prefix = "arm_x" if arm_id == "X" else "arm_y"
            prompt_digest = scenario["browser_assets"][f"{prefix}_prompt_sha256"]
            manifest_digest = scenario["browser_assets"][f"{prefix}_initial_payload_manifest_digest"]
            payload_digest = scenario["browser_assets"][f"{prefix}_initial_payload_digest"]
            operator_message_digest = None
        else:
            prompt_digest = None
            manifest_digest = scenario["browser_assets"]["s5_followup_payload_manifest_digest"]
            payload_digest = scenario["browser_assets"]["s5_followup_payload_digest"]
            operator_message_digest = scenario["browser_assets"]["s5_followup_operator_message_sha256"]
        return {
            "phase": phase,
            "processing_duration_seconds": processing_duration_seconds,
            "started_at": started_at,
            "finished_at": finished_at,
            "prompt_digest": prompt_digest,
            "payload_manifest_digest": manifest_digest,
            "payload_digest": payload_digest,
            "operator_message_digest": operator_message_digest,
            "assistant_answer_markdown": answer,
            "assistant_answer_sha256": _sha256_bytes(answer.encode("utf-8")),
        }

    steps = [step("INITIAL", "initial answer")]
    if followup:
        steps.append(step("FOLLOWUP", "follow-up answer"))
    run = {
        "schema": gate_module.RUN_SCHEMA,
        "scenario_id": scenario["scenario_id"],
        "scenario_digest": scenario["scenario_digest"],
        "arm_id": arm_id,
        "arm_type": "BASELINE_FLAT_CONTEXT_V1" if arm_id == "X" else "M2_VNEXT_CONTEXT_PACKAGE_V1",
        "repo_view": scenario["repo_view"],
        "evidence_universe_digest": evidence_universe_digest(scenario),
        "task_text_digest": task_text_digest(scenario),
        "environment": environment,
        "environment_digest": execution_environment_digest(environment),
        "conversation_steps": steps,
        "context_request_used": context_request_used,
        "requested_source_paths": [] if requested_source_paths is None else requested_source_paths,
        "protocol_burden_visible": False,
    }
    run["run_digest"] = run_digest(run)
    return run, scenario, output


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


def test_all_frozen_prompts_use_exact_full_repo_view_token() -> None:
    for scenario in _scenarios():
        view_id = scenario["repo_view"]["view_id"]
        run_dir = ROOT / "benchmarks" / "m2d" / "browser_runs" / scenario["scenario_id"]
        for arm_id in ("x", "y"):
            prompt = (run_dir / f"arm_{arm_id}_prompt.md").read_text(encoding="utf-8")
            assert view_id in gate_module._EXACT_SHA256_TOKEN_RE.findall(prompt)


def test_truncated_repo_view_token_is_rejected_fail_closed(tmp_path: Path) -> None:
    scenario = _scenarios()[0]
    source = ROOT / "benchmarks" / "m2d" / "browser_runs" / scenario["scenario_id"]
    run_dir = tmp_path / scenario["scenario_id"]
    shutil.copytree(source, run_dir)
    prompt = run_dir / "arm_x_prompt.md"
    original = prompt.read_text(encoding="utf-8")
    canonical = scenario["repo_view"]["view_id"]
    assert original.count(canonical) == 1
    prompt.write_text(original.replace(canonical, canonical[:-1], 1), encoding="utf-8")
    _expect_evaluation_error(
        lambda: _validate_prompt_pair(scenario, run_dir),
        "prompt_basis_mismatch",
    )


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


def test_treatment_inputs_are_source_grounded_and_s5_initial_request_is_not_prebuilt(tmp_path: Path) -> None:
    source = inspect.getsource(_m2_context_records)
    for forbidden in ("known_unknowns", "must_see_ground_truth", "source_inference_distinctions", "evaluator_ground_truth", "evaluator_sheet"):
        assert forbidden not in source
    view = _subject_view(ROOT)
    s1_s4 = [item for item in _scenarios() if item["scenario_id"] in {"S1", "S2", "S3", "S4"}]
    for scenario in s1_s4:
        payload, _extra = _m2_payload(
            scenario,
            view,
            scenario["arm_construction"]["initial_visible_paths"],
            phase="INITIAL",
            runtime_root=tmp_path / scenario["scenario_id"],
        )
        text = payload.decode("utf-8")
        assert "visible_unknowns: 0" in text
        assert "known_unknowns" not in text
    s5 = next(item for item in _scenarios() if item["scenario_id"] == "S5")
    payload, extra = _m2_payload(
        s5,
        view,
        s5["arm_construction"]["initial_visible_paths"],
        phase="INITIAL",
        runtime_root=tmp_path / "S5-runtime",
    )
    text = payload.decode("utf-8")
    assert extra["context_request"] is False
    assert "coverage_status: PARTIAL" in text
    assert "package-grounding" in text
    assert "decision-applicability" in text
    assert "### Natural-language context request" not in text
    assert not any(path in text for path in s5["context_seed"]["requested_source_paths"])
    assert "Here is the additional exact source context available" not in text
    followup = _materialize_followup(view, s5, output=tmp_path / "assets")["context_path"]
    followup_text = (tmp_path / "assets" / followup).read_text(encoding="utf-8")
    assert all(path in followup_text for path in s5["context_seed"]["requested_source_paths"])


def test_s5_initial_package_identity_continues_into_request_and_resolution(tmp_path: Path) -> None:
    s5 = next(item for item in _scenarios() if item["scenario_id"] == "S5")
    view = _subject_view(ROOT)
    initial_paths = list(s5["arm_construction"]["initial_visible_paths"])
    output = tmp_path / "assets"
    materialized = _materialize_one(
        view,
        s5,
        output=output,
        arm_id="Y",
        paths=initial_paths,
        phase="INITIAL",
        runtime_root=tmp_path / "materialize-runtime",
    )
    manifest = json.loads((output / materialized["manifest_path"]).read_text(encoding="utf-8"))
    canonical = _build_s5_initial_context(view, s5)
    fixture = _seed_s5_resolution(view, s5, temp_parent=tmp_path)

    assert manifest["m2_context"]["understanding_id"] == canonical["understanding"].understanding_id
    assert manifest["m2_context"]["package_id"] == canonical["package"].package_id
    assert fixture["initial_understanding_id"] == canonical["understanding"].understanding_id
    assert fixture["initial_package_id"] == canonical["package"].package_id
    assert fixture["request_source_package_id"] == fixture["initial_package_id"]
    assert fixture["resolution_prior_package_id"] == fixture["request_source_package_id"]
    assert fixture["resolution_resulting_package_id"] is not None
    assert fixture["initial_gap_id"] in fixture["resolved_gap_ids"]
    assert fixture["initial_gap_id"] not in fixture["unresolved_gap_ids"]
    assert fixture["unrelated_gap_id"] in fixture["unresolved_gap_ids"]


def test_evaluation_vector_and_integrity_contract_rejects_malformed_records() -> None:
    scenario, runs, valid = _synthetic_pair("S3")
    missing = copy.deepcopy(valid)
    missing["vector_evaluations"] = missing["vector_evaluations"][:-1]
    _expect_evaluation_error(lambda: validate_pair_evaluation(missing, scenario, validated_runs=runs), "evaluation_vector_set_invalid")
    duplicate = copy.deepcopy(valid)
    duplicate["vector_evaluations"][-1]["vector_id"] = duplicate["vector_evaluations"][0]["vector_id"]
    _expect_evaluation_error(lambda: validate_pair_evaluation(duplicate, scenario, validated_runs=runs), "evaluation_vector_duplicate")
    extra = copy.deepcopy(valid)
    extra["vector_evaluations"].append(copy.deepcopy(extra["vector_evaluations"][0]))
    _expect_evaluation_error(lambda: validate_pair_evaluation(extra, scenario, validated_runs=runs), "evaluation_vector_set_invalid")
    missing_evidence = copy.deepcopy(valid)
    missing_evidence["vector_evaluations"][0].pop("evidence")
    _expect_evaluation_error(lambda: validate_pair_evaluation(missing_evidence, scenario, validated_runs=runs), "evaluation_vector_field_set")
    invalid_judgment = copy.deepcopy(valid)
    invalid_judgment["vector_evaluations"][0]["judgment"] = "INVALID"
    _expect_evaluation_error(lambda: validate_pair_evaluation(invalid_judgment, scenario, validated_runs=runs), "evaluation_judgment_invalid")
    _s4, s4_runs, unsupported_base = _synthetic_pair("S4")
    unsupported_improvement = copy.deepcopy(unsupported_base)
    unsupported_improvement["material_improvement"] = True
    _expect_evaluation_error(lambda: validate_pair_evaluation(unsupported_improvement, _s4, validated_runs=s4_runs), "evaluation_material_improvement_unsupported")
    wrong_link = copy.deepcopy(valid)
    wrong_link["arm_x_run_digest"] = semantic_digest({"wrong": "run"})
    _expect_evaluation_error(lambda: validate_pair_evaluation(wrong_link, scenario, validated_runs=runs), "evaluation_run_link_mismatch")
    tampered_digest = copy.deepcopy(valid)
    tampered_digest["evaluation_digest"] = semantic_digest({"tampered": True})
    _expect_evaluation_error(lambda: validate_pair_evaluation(tampered_digest, scenario, validated_runs=runs), "evaluation_digest_mismatch")
    s5, protocol_runs, protocol_eval = _synthetic_pair("S5", protocol_burden=True)
    protocol_eval["context_request"]["protocol_bookkeeping_required"] = False
    protocol_eval["evaluation_digest"] = evaluation_digest(protocol_eval)
    _expect_evaluation_error(lambda: validate_pair_evaluation(protocol_eval, s5, validated_runs=protocol_runs), "evaluation_protocol_burden_mismatch")


def _complete_validated_synthetic(*, protocol_scenario: str | None = None) -> list[dict]:
    validated = []
    for scenario_id in REQUIRED_SCENARIOS:
        scenario, runs, evaluation = _synthetic_pair(
            scenario_id,
            protocol_burden=scenario_id == protocol_scenario,
            material_improvement=scenario_id == "S3",
        )
        if scenario_id == "S1" and protocol_scenario == "S1":
            runs["X"]["protocol_burden_visible"] = False
            runs["Y"]["protocol_burden_visible"] = True
        validated.append(validate_pair_evaluation(evaluation, scenario, validated_runs=runs))
    return validated


def test_s1_protocol_burden_derived_failure_cannot_be_hidden_by_equivalent_vector() -> None:
    result = _quality_policy_from_validated(_complete_validated_synthetic(protocol_scenario="S1"))
    assert result["outcome"] == "FAIL"
    assert result["effective_hard_failures"] == ["protocol_bookkeeping_required"]


def test_s3_material_improvement_cannot_override_derived_protocol_failure() -> None:
    result = _quality_policy_from_validated(_complete_validated_synthetic(protocol_scenario="S3"))
    assert result["outcome"] == "FAIL"
    assert result["effective_hard_failures"] == ["protocol_bookkeeping_required"]


def test_s5_consistent_protocol_burden_is_still_a_derived_hard_failure() -> None:
    result = _quality_policy_from_validated(_complete_validated_synthetic(protocol_scenario="S5"))
    assert result["outcome"] == "FAIL"
    assert result["effective_hard_failures"] == ["protocol_bookkeeping_required"]


def test_complete_synthetic_evaluation_fixture_is_gate_logic_only() -> None:
    validated = []
    for scenario_id in REQUIRED_SCENARIOS:
        scenario, runs, evaluation = _synthetic_pair(scenario_id)
        validated.append(validate_pair_evaluation(evaluation, scenario, validated_runs=runs))
    result = _quality_policy_from_validated(validated)
    assert result["outcome"] == "PASS"
    assert result["no_aggregate_score"] is True


def test_gate_rejects_environment_drift_before_policy(monkeypatch, tmp_path: Path) -> None:
    scenarios = {scenario_id: {"scenario_id": scenario_id} for scenario_id in REQUIRED_SCENARIOS}
    first_environment = "sha256:" + "0" * 64
    second_environment = "sha256:" + "1" * 64

    def fake_validate(run, _scenario, **_kwargs):
        return {
            "scenario_id": run["scenario_id"],
            "arm_id": run["arm_id"],
            "run_digest": semantic_digest(run),
            "environment_digest": first_environment if run["scenario_id"] != "S5" else second_environment,
        }

    monkeypatch.setattr(gate_module, "validate_arm_run", fake_validate)
    records = [{"scenario_id": scenario_id, "arm_id": arm_id} for scenario_id in REQUIRED_SCENARIOS for arm_id in ("X", "Y")]
    result = gate_module.derive_gate_decision(
        [],
        run_records=records,
        scenarios=scenarios,
        packet_root=tmp_path,
        materialized_root=tmp_path,
    )
    assert result["outcome"] == "INCONCLUSIVE"
    assert result["reason"] == "INVALID_BROWSER_EVIDENCE"
    assert "environment_drift" in result["details"]


def test_empty_or_partial_browser_evidence_never_passes() -> None:
    empty = derive_gate_decision([])
    assert empty["outcome"] == "READY_FOR_BROWSER_EXECUTION"
    assert empty["no_aggregate_score"] is True
    partial = derive_gate_decision([], run_records=[{"scenario_id": "S1"}])
    assert partial["outcome"] == "INCONCLUSIVE"
    assert partial["reason"] == "INCOMPLETE_BROWSER_EVIDENCE"
    assert "browser_runs_present" not in inspect.signature(derive_gate_decision).parameters


def test_answer_key_is_not_an_input_to_materializer() -> None:
    source = inspect.getsource(__import__("bdb_vnext.m2d_quality_gate", fromlist=["materialize_packet"]).materialize_packet)
    assert "evaluator_ground_truth.json" not in source
    assert "evaluator_sheet.json" not in source


def test_run_v2_s5_subset_request_with_frozen_followup_validates(tmp_path: Path) -> None:
    requested = ["bdb_vnext/content_store.py"]
    run, scenario, output = _v2_run(
        tmp_path,
        context_request_used=True,
        requested_source_paths=requested,
        followup=True,
    )
    validated = validate_arm_run(
        run,
        scenario,
        packet_root=ROOT / "benchmarks" / "m2d",
        materialized_root=output,
    )
    assert validated["step_count"] == 2
    assert run["requested_source_paths"] == requested
    assert run["requested_source_paths"] != scenario["context_seed"]["requested_source_paths"]


def test_run_v2_s5_y_out_of_universe_request_is_recordable_without_followup(tmp_path: Path) -> None:
    requested = ["bdb_vnext/content_store.py", "bdb_vnext/context_transport.py"]
    run, scenario, output = _v2_run(
        tmp_path,
        arm_id="Y",
        context_request_used=True,
        requested_source_paths=requested,
    )
    validated = validate_arm_run(
        run,
        scenario,
        packet_root=ROOT / "benchmarks" / "m2d",
        materialized_root=output,
    )
    assert validated["step_count"] == 1
    assert run["requested_source_paths"] == requested


def test_run_v2_s5_y_out_of_universe_request_rejects_admitted_followup(tmp_path: Path) -> None:
    run, scenario, output = _v2_run(
        tmp_path,
        arm_id="Y",
        context_request_used=True,
        requested_source_paths=["bdb_vnext/content_store.py", "bdb_vnext/context_transport.py"],
        followup=True,
    )
    _expect_evaluation_error(
        lambda: validate_arm_run(
            run,
            scenario,
            packet_root=ROOT / "benchmarks" / "m2d",
            materialized_root=output,
        ),
        "s5_followup_request_outside_universe",
    )


def test_run_v2_s5_no_request_remains_one_step_false_and_empty(tmp_path: Path) -> None:
    run, scenario, output = _v2_run(tmp_path)
    validated = validate_arm_run(
        run,
        scenario,
        packet_root=ROOT / "benchmarks" / "m2d",
        materialized_root=output,
    )
    assert validated["step_count"] == 1
    assert run["context_request_used"] is False
    assert run["requested_source_paths"] == []


def test_run_v2_s5_full_exact_request_with_followup_remains_valid(tmp_path: Path) -> None:
    requested = list(_s5_scenario()["context_seed"]["requested_source_paths"])
    run, scenario, output = _v2_run(
        tmp_path,
        context_request_used=True,
        requested_source_paths=requested,
        followup=True,
    )
    validated = validate_arm_run(
        run,
        scenario,
        packet_root=ROOT / "benchmarks" / "m2d",
        materialized_root=output,
    )
    assert validated["step_count"] == 2
    assert run["requested_source_paths"] == requested


def test_requested_exact_evidence_remains_evaluator_adjudication(tmp_path: Path) -> None:
    scenario, runs, evaluation = _synthetic_pair("S5")
    evaluation["context_request"]["outcome"] = "INCONCLUSIVE"
    evaluation["context_request"]["requested_exact_evidence"] = False
    evaluation["evaluation_digest"] = evaluation_digest(evaluation)
    validated = validate_pair_evaluation(evaluation, scenario, validated_runs=runs)
    assert validated["evaluation"]["context_request"]["requested_exact_evidence"] is False


def test_run_v2_processing_duration_is_required_timing_without_absolute_timestamps(tmp_path: Path) -> None:
    run, scenario, output = _v2_run(tmp_path, processing_duration_seconds=81)
    validated = validate_arm_run(
        run,
        scenario,
        packet_root=ROOT / "benchmarks" / "m2d",
        materialized_root=output,
    )
    assert validated["step_count"] == 1
    assert run["conversation_steps"][0]["processing_duration_seconds"] == 81
    assert run["conversation_steps"][0]["started_at"] is None
    assert run["conversation_steps"][0]["finished_at"] is None


def test_run_v2_rejects_missing_or_invalid_processing_duration(tmp_path: Path) -> None:
    run, scenario, output = _v2_run(tmp_path)
    run["conversation_steps"][0]["processing_duration_seconds"] = None
    run["run_digest"] = run_digest(run)
    _expect_evaluation_error(
        lambda: validate_arm_run(
            run,
            scenario,
            packet_root=ROOT / "benchmarks" / "m2d",
            materialized_root=output,
        ),
        "run_processing_duration_invalid",
    )


def test_run_v2_optional_timestamps_fail_closed_when_malformed_or_reversed(tmp_path: Path) -> None:
    malformed, scenario, output = _v2_run(
        tmp_path / "malformed",
        started_at="not-a-timestamp",
        finished_at="2026-08-11T12:00:01Z",
    )
    _expect_evaluation_error(
        lambda: validate_arm_run(
            malformed,
            scenario,
            packet_root=ROOT / "benchmarks" / "m2d",
            materialized_root=output,
        ),
        "run_timestamp_invalid",
    )

    reversed_run, scenario, output = _v2_run(
        tmp_path / "reversed",
        started_at="2026-08-11T12:00:02Z",
        finished_at="2026-08-11T12:00:01Z",
    )
    _expect_evaluation_error(
        lambda: validate_arm_run(
            reversed_run,
            scenario,
            packet_root=ROOT / "benchmarks" / "m2d",
            materialized_root=output,
        ),
        "run_timestamp_order_invalid",
    )


def test_run_v2_rejects_v1_schema_without_relaxing_v1_contract(tmp_path: Path) -> None:
    run, scenario, output = _v2_run(tmp_path)
    run["schema"] = "bdb-vnext-m2d-run-v1"
    run["run_digest"] = run_digest(run)
    _expect_evaluation_error(
        lambda: validate_arm_run(
            run,
            scenario,
            packet_root=ROOT / "benchmarks" / "m2d",
            materialized_root=output,
        ),
        "run_schema_mismatch",
    )

from __future__ import annotations

import ast
import json
import tempfile
from pathlib import Path

import pytest

from bdb_vnext import m2d_quality_gate as gate
from bdb_vnext.m2d_rq1_automation import (
    CAPTURE_SCHEMA,
    M2dRq1AutomationError,
    abort_capture,
    evaluator_input,
    finalize_capture,
    prepare_attempt,
    prepare_run,
    validate_capture_record,
)


ROOT = Path(__file__).resolve().parents[1]
PACKET = ROOT / "benchmarks" / "m2d"


@pytest.fixture(scope="session")
def materialized_root() -> Path:
    canonical_candidates = sorted(Path(tempfile.gettempdir()).glob("bdb-m2d-browser-attempt2-*/S5/followup_manifest.json"))
    if canonical_candidates:
        return canonical_candidates[-1].parent.parent
    pytest.skip("accepted frozen M2d materialization is required; this test must not synthesize Browser evidence")


def _observation(answer: str = "A raw Browser answer.", *, model: str | None = None, reasoning: str | None = None) -> dict:
    return {
        "browser_available": True,
        "conversation_id": "conversation-test-1",
        "navigation_state": "EXPECTED_CONVERSATION",
        "prompt_submission_status": "SUBMITTED",
        "response_status": "COMPLETED",
        "product": "ChatGPT",
        "surface": "normal_chatgpt_browser",
        "api_used": False,
        "answer_source": "normal_chatgpt_browser",
        "synthetic": False,
        "fresh_conversation": True,
        "same_visible_capability_class": True,
        "model_id": model,
        "reasoning_setting": reasoning,
        "timing": {
            "run_started_at": "2026-08-11T10:00:00Z",
            "prompt_submitted_at": "2026-08-11T10:00:01Z",
            "answer_completed_at": "2026-08-11T10:00:03Z",
            "capture_finalized_at": "2026-08-11T10:00:04Z",
            "source": "browser_observed_utc",
        },
        "steps": [{
            "phase": "INITIAL",
            "assistant_answer_markdown": answer,
            "started_at": "2026-08-11T10:00:01Z",
            "finished_at": "2026-08-11T10:00:03Z",
            "processing_duration_seconds": 2,
        }],
        "context_request_used": False,
        "requested_source_paths": [],
        "protocol_burden_visible": False,
    }


def test_prepare_attempt_binds_all_exact_runs_without_repo_writes(tmp_path: Path, materialized_root: Path) -> None:
    materialized = materialized_root
    result = prepare_attempt(ROOT, PACKET, materialized, tmp_path / "plans", attempt_id="dry-1")
    assert result["manifest"]["status"] == "PREPARED_NOT_EXECUTED"
    assert len(result["plans"]) == 10
    s5_y = next(item for item in result["plans"] if item["scenario_id"] == "S5" and item["arm_id"] == "Y")
    assert s5_y["repo_view"] == gate.frozen_repo_view_dict()
    assert s5_y["initial_context"]["context_package"]["status"] == "BOUND"
    assert s5_y["initial_context"]["context_package"]["package_id"].startswith("sha256:")
    assert s5_y["followup_context"]["source_paths"] == [
        "bdb_vnext/content_store.py",
        "schemas/bdb-vnext-context-package-v1.schema.json",
        "tests/test_vnext_engineering_intelligence.py",
    ]
    assert not list(ROOT.glob("rq1_execution_manifest.json"))


def test_finalize_preserves_raw_answer_and_reuses_existing_gate(tmp_path: Path, materialized_root: Path) -> None:
    materialized = materialized_root
    prepared = prepare_run(ROOT, PACKET, materialized, tmp_path / "plans", scenario_id="S1", arm_id="X", attempt_id="dry-2")
    finalized = finalize_capture(
        prepared["plan"],
        _observation("Exact\nraw answer."),
        output_root=tmp_path / "captures",
        repo_root=ROOT,
        packet_root=PACKET,
        materialized_root=materialized,
    )
    record = finalized["record"]
    assert record["schema"] == CAPTURE_SCHEMA
    assert record["run_record"]["conversation_steps"][0]["assistant_answer_markdown"] == "Exact\nraw answer."
    assert record["evaluation_linkage"]["status"] == "PENDING"
    checked = validate_capture_record(finalized["path"], repo_root=ROOT, packet_root=PACKET, materialized_root=materialized)
    assert checked["evaluator_eligible"] is True
    assert evaluator_input(finalized["path"])["run_digest"] == record["run_record"]["run_digest"]
    with pytest.raises(M2dRq1AutomationError, match="immutable_record_exists"):
        finalize_capture(
            prepared["plan"],
            _observation("different"),
            output_root=tmp_path / "captures",
            repo_root=ROOT,
            packet_root=PACKET,
            materialized_root=materialized,
        )


def test_s5_subset_request_and_context_package_binding_are_gate_compatible(tmp_path: Path, materialized_root: Path) -> None:
    materialized = materialized_root
    prepared = prepare_run(ROOT, PACKET, materialized, tmp_path / "plans", scenario_id="S5", arm_id="Y", attempt_id="dry-3")
    observation = _observation("Initial answer")
    observation["context_request_used"] = True
    observation["requested_source_paths"] = ["bdb_vnext/content_store.py"]
    observation["steps"].append({
        "phase": "FOLLOWUP",
        "assistant_answer_markdown": "Repaired answer",
        "started_at": "2026-08-11T10:00:04Z",
        "finished_at": "2026-08-11T10:00:06Z",
        "processing_duration_seconds": 2,
    })
    finalized = finalize_capture(
        prepared["plan"],
        observation,
        output_root=tmp_path / "captures",
        repo_root=ROOT,
        packet_root=PACKET,
        materialized_root=materialized,
    )
    assert finalized["record"]["context_package"]["status"] == "BOUND"
    assert finalized["record"]["run_record"]["requested_source_paths"] == ["bdb_vnext/content_store.py"]
    assert finalized["record"]["run_record"]["conversation_steps"][1]["prompt_digest"] is None


def test_browser_api_synthetic_and_moving_checkout_substitutions_fail_closed(tmp_path: Path, materialized_root: Path) -> None:
    materialized = materialized_root
    prepared = prepare_run(ROOT, PACKET, materialized, tmp_path / "plans", scenario_id="S1", arm_id="X", attempt_id="dry-4")
    for field, value, code in (
        ("api_used", True, "api_fallback_forbidden"),
        ("synthetic", True, "synthetic_answer_forbidden"),
        ("surface", "mock_browser", "browser_surface_invalid"),
    ):
        observation = _observation()
        observation[field] = value
        with pytest.raises(M2dRq1AutomationError, match=code):
            finalize_capture(prepared["plan"], observation, output_root=tmp_path / f"captures-{field}", repo_root=ROOT, packet_root=PACKET, materialized_root=materialized)
    with pytest.raises(M2dRq1AutomationError, match="tracked_output_forbidden"):
        prepare_run(ROOT, PACKET, materialized, ROOT / "forbidden", scenario_id="S1", arm_id="X")


def test_visible_model_and_reasoning_expectations_are_enforced(tmp_path: Path, materialized_root: Path) -> None:
    prepared = prepare_run(
        ROOT,
        PACKET,
        materialized_root,
        tmp_path / "plans",
        scenario_id="S1",
        arm_id="X",
        attempt_id="dry-attestation",
        expected_model_id="visible-model",
        expected_reasoning_setting="visible-reasoning",
    )
    observation = _observation(model="different-model", reasoning="visible-reasoning")
    with pytest.raises(M2dRq1AutomationError, match="unexpected_model"):
        finalize_capture(
            prepared["plan"],
            observation,
            output_root=tmp_path / "captures",
            repo_root=ROOT,
            packet_root=PACKET,
            materialized_root=materialized_root,
        )


def test_abort_is_explicit_and_never_evaluator_eligible(tmp_path: Path, materialized_root: Path) -> None:
    materialized = materialized_root
    prepared = prepare_run(ROOT, PACKET, materialized, tmp_path / "plans", scenario_id="S1", arm_id="X", attempt_id="dry-5")
    result = abort_capture(
        prepared["plan"],
        output_root=tmp_path / "captures",
        repo_root=ROOT,
        reason="Browser service worker restarted",
        observed_failure="browser_restart",
    )
    assert result["record"]["status"] == "ABORTED"
    assert result["record"]["run_record"] is None
    assert validate_capture_record(result["path"], repo_root=ROOT, packet_root=PACKET, materialized_root=materialized)["evaluator_eligible"] is False
    with pytest.raises(M2dRq1AutomationError, match="evaluator_input_invalid"):
        evaluator_input(result["path"])


def test_module_is_legacy_free_and_schema_is_strict() -> None:
    source = (ROOT / "bdb_vnext" / "m2d_rq1_automation.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    imported.update(
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    assert "bdb_bridge" not in imported
    assert "bdb_legacy" not in imported
    schema = json.loads((ROOT / "schemas" / "bdb-vnext-m2d-rq1-capture-v1.schema.json").read_text(encoding="utf-8"))
    assert schema["additionalProperties"] is False
    assert schema["properties"]["schema"]["const"] == CAPTURE_SCHEMA

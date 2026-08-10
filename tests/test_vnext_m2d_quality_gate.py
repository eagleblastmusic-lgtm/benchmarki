from __future__ import annotations

import json
from pathlib import Path

from bdb_vnext.m2d_quality_gate import (
    EVALUATOR_SHEET_SCHEMA,
    FROZEN_COMMIT,
    GATE_POLICY_VERSION,
    REQUIRED_SCENARIOS,
    RUBRIC_VERSION,
    SCENARIO_SCHEMA,
    derive_gate_decision,
    evidence_universe_digest,
    scenario_digest,
)


ROOT = Path(__file__).resolve().parents[1]


def _scenarios() -> list[dict]:
    result = []
    for path in sorted((ROOT / "benchmarks" / "m2d" / "scenarios").glob("S[1-5]-*.json")):
        result.append(json.loads(path.read_text(encoding="utf-8")))
    return result


def test_frozen_scenario_packet_has_five_deterministic_identities() -> None:
    scenarios = _scenarios()
    assert [item["scenario_id"] for item in scenarios] == list(REQUIRED_SCENARIOS)
    for scenario in scenarios:
        assert scenario["schema"] == SCENARIO_SCHEMA
        assert scenario["repo_view"]["commit_oid"] == FROZEN_COMMIT
        assert scenario["rubric_version"] == RUBRIC_VERSION
        assert scenario["gate_policy_version"] == GATE_POLICY_VERSION
        assert scenario["scenario_digest"] == scenario_digest(scenario)
        ground = ROOT / "benchmarks" / "m2d" / "browser_runs" / scenario["scenario_id"] / "evaluator_ground_truth.json"
        sheet = ROOT / "benchmarks" / "m2d" / "browser_runs" / scenario["scenario_id"] / "evaluator_sheet.json"
        ground_data = json.loads(ground.read_text(encoding="utf-8"))
        sheet_data = json.loads(sheet.read_text(encoding="utf-8"))
        assert ground_data["scenario_digest"] == scenario["scenario_digest"]
        assert ground_data["evidence_universe_digest"] == evidence_universe_digest(scenario)
        assert sheet_data["schema"] == EVALUATOR_SHEET_SCHEMA
        assert sheet_data["scenario_digest"] == scenario["scenario_digest"]


def test_gate_stays_ready_until_browser_pairs_exist() -> None:
    decision = derive_gate_decision([], browser_runs_present=False)
    assert decision["outcome"] == "READY_FOR_BROWSER_EXECUTION"
    assert decision["no_aggregate_score"] is True
    assert decision["missing_scenarios"] == list(REQUIRED_SCENARIOS)


def test_gate_rejects_fairness_drift_before_quality_judgment() -> None:
    evaluations = [
        {
            "scenario_id": scenario_id,
            "fairness": {
                "same_model": True,
                "same_settings": True,
                "same_repo_view": True,
                "same_evidence_universe": True,
                "same_task": True,
                "browser_parity": scenario_id != "S3",
            },
            "vector_evaluations": [{"judgment": "EQUIVALENT"}],
            "hard_failures": [],
            "material_improvement": True,
            "context_request": {
                "outcome": "RESOLVED" if scenario_id == "S5" else "NOT_APPLICABLE",
                "gap_visible": scenario_id == "S5",
                "requested_exact_evidence": scenario_id == "S5",
                "unrelated_gaps_preserved": scenario_id == "S5",
                "answer_improved_or_narrowed": scenario_id == "S5",
                "protocol_bookkeeping_required": False,
            },
        }
        for scenario_id in REQUIRED_SCENARIOS
    ]
    decision = derive_gate_decision(evaluations)
    assert decision["outcome"] == "INCONCLUSIVE"


def test_gate_accepts_only_a_complete_synthetic_fixture() -> None:
    evaluations = []
    for scenario_id in REQUIRED_SCENARIOS:
        evaluations.append(
            {
                "scenario_id": scenario_id,
                "fairness": {
                    "same_model": True,
                    "same_settings": True,
                    "same_repo_view": True,
                    "same_evidence_universe": True,
                    "same_task": True,
                    "browser_parity": True,
                },
                "vector_evaluations": [{"judgment": "EQUIVALENT"}],
                "hard_failures": [],
                "material_improvement": scenario_id == "S3",
                "context_request": {
                    "outcome": "RESOLVED" if scenario_id == "S5" else "NOT_APPLICABLE",
                    "gap_visible": scenario_id == "S5",
                    "requested_exact_evidence": scenario_id == "S5",
                    "unrelated_gaps_preserved": scenario_id == "S5",
                    "answer_improved_or_narrowed": scenario_id == "S5",
                    "protocol_bookkeeping_required": False,
                },
            }
        )
    decision = derive_gate_decision(evaluations)
    assert decision["outcome"] == "PASS"
    assert decision["no_aggregate_score"] is True

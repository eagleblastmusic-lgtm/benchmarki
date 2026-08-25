"""Tests for NX-002: Project Plan Schema and Runtime Loader Parity."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bdb_vnext.project_catalog import ProjectCatalogError, validate_project_plan
from bdb_vnext.project_plan_conformance_corpus import (
    build_conformance_corpus,
    evaluate_corpus_case,
    run_nx002_parity_gate,
)
from bdb_vnext.project_plan_contract import (
    MAX_TEXT_LIST_64_STRING_LENGTH,
    PROJECT_PLAN_SCHEMA,
    validate_project_plan_schema,
)

ROOT = Path(__file__).resolve().parents[1]


def test_conformance_corpus_parity_matrix() -> None:
    """All cases in conformance corpus must achieve 100% agreement between Schema and Loader."""
    passed, report = run_nx002_parity_gate()
    assert passed is True, f"Parity divergences found: {report.get('failing_case_ids')}"
    assert report["parity_divergences_count"] == 0
    assert report["unexpected_outcomes_count"] == 0
    assert report["machine_gate"] == "PASS"


def test_unknown_key_rejections_across_all_nesting_levels() -> None:
    """Verify that unknown keys are rejected at root, milestone, task, and planning_context sub-objects."""
    corpus = build_conformance_corpus()
    unknown_key_cases = [c for c in corpus if c.category == "unknown_keys"]
    assert len(unknown_key_cases) >= 12, "Should cover root, milestone, task, and all context sub-objects"

    for case in unknown_key_cases:
        res = evaluate_corpus_case(case)
        assert res["schema_outcome"] == "REJECT", f"Schema should reject unknown key in {case.case_id}: {case.description}"
        assert res["loader_outcome"] == "REJECT", f"Loader should reject unknown key in {case.case_id}: {case.description}"
        assert res["parity_match"] is True


def test_milestone_unknown_keys_core_drift_fix() -> None:
    """Explicitly verify the audited drift: unknown milestone keys are now rejected by BOTH."""
    case_doc = {
        "schema": PROJECT_PLAN_SCHEMA,
        "project_id": "test-drift-milestone",
        "project_name": "Test Drift",
        "plan_version": 1,
        "milestones": [
            {
                "id": "M01",
                "title": "Milestone 1",
                "description": "Desc",
                "status": "pending",
                "unexpected_milestone_field": "DRIFT",
            }
        ],
        "tasks": [
            {
                "id": "T01",
                "milestone_id": "M01",
                "title": "Task 1",
                "description": "Desc",
                "status": "pending",
            }
        ],
    }
    schema_ok, _ = validate_project_plan_schema(case_doc)
    assert not schema_ok, "Schema must reject unexpected_milestone_field"

    with pytest.raises(ProjectCatalogError) as exc:
        validate_project_plan(case_doc)
    assert exc.value.code == "plan_field_unknown"
    assert "unexpected_milestone_field" in str(exc.value)


def test_boundary_lengths_acceptance_criteria() -> None:
    """Boundary test for acceptance criteria at limit - 1 (7999), limit (8000), limit + 1 (8001)."""
    base_doc = {
        "schema": PROJECT_PLAN_SCHEMA,
        "project_id": "test-ac-boundary",
        "project_name": "Test AC Boundary",
        "plan_version": 1,
        "milestones": [{"id": "M1", "title": "M1", "description": "D1"}],
        "tasks": [{"id": "T1", "milestone_id": "M1", "title": "T1", "description": "D1", "acceptance_criteria": []}],
    }

    # Limit - 1: 7999 chars -> ACCEPT
    doc_7999 = json.loads(json.dumps(base_doc))
    doc_7999["tasks"][0]["acceptance_criteria"] = ["X" * 7999]
    schema_ok, _ = validate_project_plan_schema(doc_7999)
    assert schema_ok is True
    plan_7999 = validate_project_plan(doc_7999)
    assert len(plan_7999.tasks[0].acceptance_criteria[0]) == 7999

    # Limit: 8000 chars -> ACCEPT
    doc_8000 = json.loads(json.dumps(base_doc))
    doc_8000["tasks"][0]["acceptance_criteria"] = ["X" * MAX_TEXT_LIST_64_STRING_LENGTH]
    schema_ok, _ = validate_project_plan_schema(doc_8000)
    assert schema_ok is True
    plan_8000 = validate_project_plan(doc_8000)
    assert len(plan_8000.tasks[0].acceptance_criteria[0]) == 8000

    # Limit + 1: 8001 chars -> REJECT
    doc_8001 = json.loads(json.dumps(base_doc))
    doc_8001["tasks"][0]["acceptance_criteria"] = ["X" * (MAX_TEXT_LIST_64_STRING_LENGTH + 1)]
    schema_ok, _ = validate_project_plan_schema(doc_8001)
    assert schema_ok is False
    with pytest.raises(ProjectCatalogError) as exc:
        validate_project_plan(doc_8001)
    assert exc.value.code == "project_field_too_large"


def test_boundary_lengths_risk_severity() -> None:
    """Boundary test for risk severity at limit (32) and limit + 1 (33)."""
    base_doc = {
        "schema": PROJECT_PLAN_SCHEMA,
        "project_id": "test-risk-boundary",
        "project_name": "Test Risk Boundary",
        "plan_version": 1,
        "milestones": [{"id": "M1", "title": "M1", "description": "D1"}],
        "tasks": [{"id": "T1", "milestone_id": "M1", "title": "T1", "description": "D1", "risk_ids": ["R1"]}],
        "planning_context": {
            "risks": [
                {
                    "id": "R1",
                    "title": "Risk 1",
                    "description": "Desc",
                    "severity": "S" * 32,
                    "mitigation": "Mitigation",
                }
            ]
        },
    }

    # Limit: 32 chars -> ACCEPT
    schema_ok, _ = validate_project_plan_schema(base_doc)
    assert schema_ok is True
    plan = validate_project_plan(base_doc)
    assert plan.planning_context["risks"][0]["severity"] == "S" * 32

    # Limit + 1: 33 chars -> REJECT
    doc_33 = json.loads(json.dumps(base_doc))
    doc_33["planning_context"]["risks"][0]["severity"] = "S" * 33
    schema_ok, _ = validate_project_plan_schema(doc_33)
    assert schema_ok is False
    with pytest.raises(ProjectCatalogError) as exc:
        validate_project_plan(doc_33)
    assert exc.value.code == "project_field_too_large"


def test_existing_canonical_plan_file_in_repository_validates() -> None:
    """Verify that existing plan files in repo validate under both Schema and Loader."""
    plan_path = ROOT / "runtime" / "control" / "project-memory" / "0c62f1b8-2ce1-48d3-bae9-c3c32b9a84b6" / "plans" / "plan-v1.json"
    if plan_path.exists():
        with open(plan_path, "r", encoding="utf-8") as f:
            plan_doc = json.load(f)
        schema_ok, errors = validate_project_plan_schema(plan_doc)
        assert schema_ok is True, f"Schema validation failed on existing plan: {errors}"
        plan = validate_project_plan(plan_doc)
        assert plan.project_id == plan_doc["project_id"]

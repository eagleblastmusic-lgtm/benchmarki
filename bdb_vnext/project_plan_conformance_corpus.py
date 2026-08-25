"""BDB vNext - NX-002 Conformance Corpus for Project Plan v1 Parity.

This module builds a comprehensive, deterministically generated corpus of valid
and invalid Project Plan documents to verify exact contract parity between
schemas/bdb-project-plan-v1.schema.json and validate_project_plan().
"""

from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass
from typing import Any

from .project_catalog import ProjectCatalogError, validate_project_plan
from .project_plan_contract import (
    DECISION_CLASSIFICATIONS,
    MAX_ARCHITECTURE_SUMMARY_LENGTH,
    MAX_CREATED_AT_LENGTH,
    MAX_ID_LENGTH,
    MAX_ID_LIST_64_ITEMS,
    MAX_MILESTONE_DESCRIPTION_LENGTH,
    MAX_MILESTONE_TITLE_LENGTH,
    MAX_MILESTONES_COUNT,
    MAX_PLANNING_OBJECTIVE_LENGTH,
    MAX_PROJECT_ID_LENGTH,
    MAX_PROJECT_NAME_LENGTH,
    MAX_RECORD_STRING_LENGTH,
    MAX_REVISION_REASON_LENGTH,
    MAX_REVISION_SUMMARY_LENGTH,
    MAX_RISK_SEVERITY_LENGTH,
    MAX_TASK_DESCRIPTION_LENGTH,
    MAX_TASK_TITLE_LENGTH,
    MAX_TASKS_COUNT,
    MAX_TEXT_LIST_128_ITEMS,
    MAX_TEXT_LIST_128_STRING_LENGTH,
    MAX_TEXT_LIST_64_ITEMS,
    MAX_TEXT_LIST_64_STRING_LENGTH,
    PLAN_STATUS_VALUES,
    PLANNING_CONTEXT_KEYS,
    PLANNING_SPECIFICATION_CATEGORIES,
    PROJECT_PLAN_SCHEMA,
    validate_project_plan_schema,
)


@dataclass(frozen=True)
class CorpusCase:
    case_id: str
    category: str
    description: str
    document: dict[str, Any]
    expected_outcome: str  # "ACCEPT" or "REJECT"
    expected_reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _base_valid_document() -> dict[str, Any]:
    return {
        "schema": PROJECT_PLAN_SCHEMA,
        "project_id": "test-project",
        "project_name": "Test Project",
        "plan_version": 1,
        "created_at": "2026-08-25T12:00:00.000000Z",
        "current_task_id": "T01",
        "milestones": [
            {
                "id": "M01",
                "title": "Milestone 1",
                "description": "First milestone",
                "status": "active",
            }
        ],
        "tasks": [
            {
                "id": "T01",
                "milestone_id": "M01",
                "title": "Task 1",
                "description": "First task",
                "status": "active",
                "dependencies": [],
                "acceptance_criteria": ["Acceptance 1"],
                "deliverables": ["Deliverable 1"],
                "verification": ["Verification 1"],
                "tests": ["Test 1"],
                "decision_ids": ["D01"],
                "specification_ids": ["S01"],
                "risk_ids": ["R01"],
            }
        ],
        "planning_context": {
            "objective": "Achieve complete parity",
            "requirements": {
                "functional": ["Func req 1"],
                "quality": ["Qual req 1"],
            },
            "scope": {
                "in_scope": ["In scope item"],
                "out_of_scope": ["Out of scope item"],
            },
            "assumptions": ["Assumption 1"],
            "decisions": [
                {
                    "id": "D01",
                    "title": "Decision 1",
                    "decision": "Use central contract",
                    "rationale": "Avoid drift",
                    "classification": "architectural_decision",
                }
            ],
            "open_questions": [
                {
                    "id": "OQ01",
                    "question": "Question 1?",
                    "recommended_default": "Default answer",
                    "owner": "Team",
                    "deadline": "2026-09-01",
                    "blocking_effect": "None",
                }
            ],
            "specifications": [
                {
                    "id": "S01",
                    "category": "domain",
                    "title": "Spec 1",
                    "body": "Specification body",
                }
            ],
            "architecture": {
                "summary": "Architecture summary",
                "components": [
                    {
                        "id": "C01",
                        "name": "Component 1",
                        "responsibility": "Handle tasks",
                        "interfaces": ["IF01"],
                    }
                ],
                "interfaces": [
                    {
                        "id": "I01",
                        "name": "Interface 1",
                        "responsibility": "Contract boundary",
                    }
                ],
                "patterns": ["Pattern 1"],
            },
            "test_strategy": {
                "unit": ["Unit tests"],
                "integration": ["Integration tests"],
                "e2e": ["E2E tests"],
                "manual": ["Manual checks"],
                "automation": ["CI automation"],
            },
            "risks": [
                {
                    "id": "R01",
                    "title": "Risk 1",
                    "description": "Risk description",
                    "severity": "high",
                    "mitigation": "Mitigation step",
                }
            ],
            "gates": [
                {
                    "id": "G01",
                    "title": "Gate 1",
                    "criteria": "All tests pass",
                }
            ],
            "acceptance_scenarios": [
                {
                    "id": "AS01",
                    "title": "Scenario 1",
                    "given": "Given state",
                    "when": "When action",
                    "then": "Then outcome",
                }
            ],
            "definition_of_done": ["DoD item 1"],
        },
    }


def build_conformance_corpus() -> list[CorpusCase]:
    cases: list[CorpusCase] = []

    def add(case_id: str, category: str, description: str, doc: dict[str, Any], expected: str, reason: str) -> None:
        cases.append(CorpusCase(case_id, category, description, copy.deepcopy(doc), expected, reason))

    # --- 1. VALID BASELINES ---
    base = _base_valid_document()
    add("CC-001", "valid_baseline", "Full rich valid project plan", base, "ACCEPT", "All fields populated and valid")

    minimal = {
        "schema": PROJECT_PLAN_SCHEMA,
        "project_id": "minimal-proj",
        "project_name": "Minimal Project",
        "plan_version": 1,
        "milestones": [{"id": "M1", "title": "Milestone 1", "description": "Desc"}],
        "tasks": [{"id": "T1", "milestone_id": "M1", "title": "Task 1", "description": "Desc"}],
    }
    add("CC-002", "valid_baseline", "Minimal valid project plan without optional fields", minimal, "ACCEPT", "Required fields only")

    with_supersedes = copy.deepcopy(base)
    with_supersedes["plan_version"] = 2
    with_supersedes["supersedes_version"] = 1
    with_supersedes["revision_reason"] = "Updated tasks"
    with_supersedes["revision_summary"] = "Summary of revisions"
    add("CC-003", "valid_baseline", "Valid plan update with supersedes and revision metadata", with_supersedes, "ACCEPT", "Valid revision fields")

    null_current_task = copy.deepcopy(base)
    null_current_task["current_task_id"] = None
    add("CC-004", "valid_baseline", "Valid plan with null current_task_id", null_current_task, "ACCEPT", "null current_task_id is valid")

    # --- 2. UNKNOWN KEYS (FAIL CLOSED AT ALL LEVELS) ---
    doc_unk_root = copy.deepcopy(base)
    doc_unk_root["unknown_root_key"] = "extra"
    add("CC-010", "unknown_keys", "Unknown top-level property", doc_unk_root, "REJECT", "Top level additionalProperties: false")

    doc_unk_ms = copy.deepcopy(base)
    doc_unk_ms["milestones"][0]["unknown_milestone_key"] = "extra"
    add("CC-011", "unknown_keys", "Unknown milestone property (NX-002 core drift fix)", doc_unk_ms, "REJECT", "Milestone additionalProperties: false")

    doc_unk_task = copy.deepcopy(base)
    doc_unk_task["tasks"][0]["unknown_task_key"] = "extra"
    add("CC-012", "unknown_keys", "Unknown task property", doc_unk_task, "REJECT", "Task additionalProperties: false")

    doc_unk_ctx = copy.deepcopy(base)
    doc_unk_ctx["planning_context"]["unknown_context_key"] = "extra"
    add("CC-013", "unknown_keys", "Unknown planning_context property", doc_unk_ctx, "REJECT", "PlanningContext additionalProperties: false")

    doc_unk_req = copy.deepcopy(base)
    doc_unk_req["planning_context"]["requirements"]["unknown_req_key"] = ["x"]
    add("CC-014", "unknown_keys", "Unknown requirements property", doc_unk_req, "REJECT", "Requirements additionalProperties: false")

    doc_unk_scope = copy.deepcopy(base)
    doc_unk_scope["planning_context"]["scope"]["unknown_scope_key"] = ["x"]
    add("CC-015", "unknown_keys", "Unknown scope property", doc_unk_scope, "REJECT", "Scope additionalProperties: false")

    doc_unk_dec = copy.deepcopy(base)
    doc_unk_dec["planning_context"]["decisions"][0]["unknown_dec_key"] = "extra"
    add("CC-016", "unknown_keys", "Unknown decision property", doc_unk_dec, "REJECT", "Decision additionalProperties: false")

    doc_unk_oq = copy.deepcopy(base)
    doc_unk_oq["planning_context"]["open_questions"][0]["unknown_oq_key"] = "extra"
    add("CC-017", "unknown_keys", "Unknown open_question property", doc_unk_oq, "REJECT", "OpenQuestion additionalProperties: false")

    doc_unk_spec = copy.deepcopy(base)
    doc_unk_spec["planning_context"]["specifications"][0]["unknown_spec_key"] = "extra"
    add("CC-018", "unknown_keys", "Unknown specification property", doc_unk_spec, "REJECT", "Specification additionalProperties: false")

    doc_unk_arch = copy.deepcopy(base)
    doc_unk_arch["planning_context"]["architecture"]["unknown_arch_key"] = "extra"
    add("CC-019", "unknown_keys", "Unknown architecture property", doc_unk_arch, "REJECT", "Architecture additionalProperties: false")

    doc_unk_comp = copy.deepcopy(base)
    doc_unk_comp["planning_context"]["architecture"]["components"][0]["unknown_comp_key"] = "extra"
    add("CC-020", "unknown_keys", "Unknown architectureItem property", doc_unk_comp, "REJECT", "ArchitectureItem additionalProperties: false")

    doc_unk_strat = copy.deepcopy(base)
    doc_unk_strat["planning_context"]["test_strategy"]["unknown_strat_key"] = ["x"]
    add("CC-021", "unknown_keys", "Unknown test_strategy property", doc_unk_strat, "REJECT", "TestStrategy additionalProperties: false")

    doc_unk_risk = copy.deepcopy(base)
    doc_unk_risk["planning_context"]["risks"][0]["unknown_risk_key"] = "extra"
    add("CC-022", "unknown_keys", "Unknown risk property", doc_unk_risk, "REJECT", "Risk additionalProperties: false")

    doc_unk_gate = copy.deepcopy(base)
    doc_unk_gate["planning_context"]["gates"][0]["unknown_gate_key"] = "extra"
    add("CC-023", "unknown_keys", "Unknown gate property", doc_unk_gate, "REJECT", "Gate additionalProperties: false")

    doc_unk_as = copy.deepcopy(base)
    doc_unk_as["planning_context"]["acceptance_scenarios"][0]["unknown_as_key"] = "extra"
    add("CC-024", "unknown_keys", "Unknown acceptanceScenario property", doc_unk_as, "REJECT", "AcceptanceScenario additionalProperties: false")

    # --- 3. BOUNDARY LENGTHS (limit - 1, limit, limit + 1) ---
    # Acceptance Criteria (limit: 8000 chars)
    doc_ac_limit_minus_1 = copy.deepcopy(base)
    doc_ac_limit_minus_1["tasks"][0]["acceptance_criteria"] = ["A" * 7999]
    add("CC-030", "boundary_lengths", "Acceptance criteria length = 7999 (limit - 1)", doc_ac_limit_minus_1, "ACCEPT", "Length within bound")

    doc_ac_limit = copy.deepcopy(base)
    doc_ac_limit["tasks"][0]["acceptance_criteria"] = ["A" * 8000]
    add("CC-031", "boundary_lengths", "Acceptance criteria length = 8000 (limit)", doc_ac_limit, "ACCEPT", "Exact canonical limit")

    doc_ac_limit_plus_1 = copy.deepcopy(base)
    doc_ac_limit_plus_1["tasks"][0]["acceptance_criteria"] = ["A" * 8001]
    add("CC-032", "boundary_lengths", "Acceptance criteria length = 8001 (limit + 1)", doc_ac_limit_plus_1, "REJECT", "Exceeds 8000 chars limit")

    # Risk severity (limit: 32 chars)
    doc_sev_limit = copy.deepcopy(base)
    doc_sev_limit["planning_context"]["risks"][0]["severity"] = "S" * 32
    add("CC-033", "boundary_lengths", "Risk severity length = 32 (limit)", doc_sev_limit, "ACCEPT", "Exact canonical limit")

    doc_sev_limit_plus_1 = copy.deepcopy(base)
    doc_sev_limit_plus_1["planning_context"]["risks"][0]["severity"] = "S" * 33
    add("CC-034", "boundary_lengths", "Risk severity length = 33 (limit + 1)", doc_sev_limit_plus_1, "REJECT", "Exceeds 32 chars limit")

    # Milestone title (limit: 300 chars)
    doc_mt_limit = copy.deepcopy(base)
    doc_mt_limit["milestones"][0]["title"] = "M" * 300
    add("CC-035", "boundary_lengths", "Milestone title length = 300 (limit)", doc_mt_limit, "ACCEPT", "Exact canonical limit")

    doc_mt_limit_plus_1 = copy.deepcopy(base)
    doc_mt_limit_plus_1["milestones"][0]["title"] = "M" * 301
    add("CC-036", "boundary_lengths", "Milestone title length = 301 (limit + 1)", doc_mt_limit_plus_1, "REJECT", "Exceeds 300 chars limit")

    # Task title (limit: 300 chars)
    doc_tt_limit = copy.deepcopy(base)
    doc_tt_limit["tasks"][0]["title"] = "T" * 300
    add("CC-037", "boundary_lengths", "Task title length = 300 (limit)", doc_tt_limit, "ACCEPT", "Exact canonical limit")

    doc_tt_limit_plus_1 = copy.deepcopy(base)
    doc_tt_limit_plus_1["tasks"][0]["title"] = "T" * 301
    add("CC-038", "boundary_lengths", "Task title length = 301 (limit + 1)", doc_tt_limit_plus_1, "REJECT", "Exceeds 300 chars limit")

    # Project name (limit: 200 chars)
    doc_pn_limit = copy.deepcopy(base)
    doc_pn_limit["project_name"] = "P" * 200
    add("CC-039", "boundary_lengths", "Project name length = 200 (limit)", doc_pn_limit, "ACCEPT", "Exact canonical limit")

    doc_pn_limit_plus_1 = copy.deepcopy(base)
    doc_pn_limit_plus_1["project_name"] = "P" * 201
    add("CC-040", "boundary_lengths", "Project name length = 201 (limit + 1)", doc_pn_limit_plus_1, "REJECT", "Exceeds 200 chars limit")

    # --- 4. ARRAY LIMITS ---
    # Deliverables textList64 maxItems: 64
    doc_deliv_64 = copy.deepcopy(base)
    doc_deliv_64["tasks"][0]["deliverables"] = [f"Deliv {i}" for i in range(64)]
    add("CC-050", "array_limits", "Task deliverables count = 64 (limit)", doc_deliv_64, "ACCEPT", "Exact maxItems 64")

    doc_deliv_65 = copy.deepcopy(base)
    doc_deliv_65["tasks"][0]["deliverables"] = [f"Deliv {i}" for i in range(65)]
    add("CC-051", "array_limits", "Task deliverables count = 65 (limit + 1)", doc_deliv_65, "REJECT", "Exceeds maxItems 64")

    # Assumptions textList128 maxItems: 128
    doc_assump_128 = copy.deepcopy(base)
    doc_assump_128["planning_context"]["assumptions"] = [f"Assump {i}" for i in range(128)]
    add("CC-052", "array_limits", "Assumptions count = 128 (limit)", doc_assump_128, "ACCEPT", "Exact maxItems 128")

    doc_assump_129 = copy.deepcopy(base)
    doc_assump_129["planning_context"]["assumptions"] = [f"Assump {i}" for i in range(129)]
    add("CC-053", "array_limits", "Assumptions count = 129 (limit + 1)", doc_assump_129, "REJECT", "Exceeds maxItems 128")

    # --- 5. ENUMS ---
    doc_inv_status = copy.deepcopy(base)
    doc_inv_status["tasks"][0]["status"] = "in_progress"
    add("CC-060", "enums", "Invalid task status enum value", doc_inv_status, "REJECT", "Not in status enum")

    doc_inv_ms_status = copy.deepcopy(base)
    doc_inv_ms_status["milestones"][0]["status"] = "done"
    add("CC-061", "enums", "Invalid milestone status enum value", doc_inv_ms_status, "REJECT", "Not in status enum")

    doc_inv_dec_class = copy.deepcopy(base)
    doc_inv_dec_class["planning_context"]["decisions"][0]["classification"] = "unsupported_classification"
    add("CC-062", "enums", "Invalid decision classification enum", doc_inv_dec_class, "REJECT", "Not in DECISION_CLASSIFICATIONS")

    doc_inv_spec_cat = copy.deepcopy(base)
    doc_inv_spec_cat["planning_context"]["specifications"][0]["category"] = "unsupported_category"
    add("CC-063", "enums", "Invalid specification category enum", doc_inv_spec_cat, "REJECT", "Not in PLANNING_SPECIFICATION_CATEGORIES")

    # --- 6. ID FORMAT & UNIQUENESS ---
    doc_inv_id_space = copy.deepcopy(base)
    doc_inv_id_space["tasks"][0]["id"] = "Invalid ID with spaces"
    doc_inv_id_space["current_task_id"] = "Invalid ID with spaces"
    add("CC-070", "id_format", "Invalid ID containing spaces", doc_inv_id_space, "REJECT", "ID does not match pattern")

    doc_inv_id_colon = copy.deepcopy(base)
    doc_inv_id_colon["tasks"][0]["id"] = "task:sub-1.0_ready"
    doc_inv_id_colon["current_task_id"] = "task:sub-1.0_ready"
    add("CC-071", "id_format", "Valid ID with colons, dots, dashes and underscores", doc_inv_id_colon, "ACCEPT", "Matches ID pattern")

    doc_inv_id_dup_dec = copy.deepcopy(base)
    doc_inv_id_dup_dec["tasks"][0]["decision_ids"] = ["D01", "D01"]
    add("CC-072", "id_format", "Duplicate IDs in task.decision_ids", doc_inv_id_dup_dec, "REJECT", "Unique items required")

    # --- 7. MISSING REQUIRED FIELDS & TYPES ---
    doc_missing_proj_id = copy.deepcopy(base)
    del doc_missing_proj_id["project_id"]
    add("CC-080", "types", "Missing required top-level project_id", doc_missing_proj_id, "REJECT", "Missing required property")

    doc_missing_ms = copy.deepcopy(base)
    del doc_missing_ms["milestones"]
    add("CC-081", "types", "Missing required top-level milestones", doc_missing_ms, "REJECT", "Missing required property")

    doc_wrong_type_ver = copy.deepcopy(base)
    doc_wrong_type_ver["plan_version"] = True
    add("CC-082", "types", "plan_version as boolean", doc_wrong_type_ver, "REJECT", "Boolean is not valid integer/string version")

    doc_wrong_type_tasks = copy.deepcopy(base)
    doc_wrong_type_tasks["tasks"] = "not-a-list"
    add("CC-083", "types", "tasks as string instead of array", doc_wrong_type_tasks, "REJECT", "Expected array")

    return cases


def evaluate_corpus_case(case: CorpusCase) -> dict[str, Any]:
    # 1. Evaluate against JSON Schema
    schema_ok, schema_errs = validate_project_plan_schema(case.document)
    schema_outcome = "ACCEPT" if schema_ok else "REJECT"

    # 2. Evaluate against Runtime Loader
    loader_ok = True
    loader_err_msg = ""
    try:
        validate_project_plan(case.document)
    except (ProjectCatalogError, Exception) as exc:
        loader_ok = False
        loader_err_msg = str(exc)
    loader_outcome = "ACCEPT" if loader_ok else "REJECT"

    parity_match = (schema_outcome == loader_outcome)
    expected_match = (schema_outcome == case.expected_outcome and loader_outcome == case.expected_outcome)

    return {
        "case_id": case.case_id,
        "category": case.category,
        "description": case.description,
        "expected_outcome": case.expected_outcome,
        "schema_outcome": schema_outcome,
        "loader_outcome": loader_outcome,
        "parity_match": parity_match,
        "expected_match": expected_match,
        "schema_errors": schema_errs,
        "loader_error": loader_err_msg,
    }


def run_nx002_parity_gate() -> tuple[bool, dict[str, Any]]:
    corpus = build_conformance_corpus()
    schema_accepts = 0
    schema_rejects = 0
    loader_accepts = 0
    loader_rejects = 0
    divergences: list[str] = []
    unexpected_outcomes: list[str] = []
    evaluations: list[dict[str, Any]] = []

    for case in corpus:
        res = evaluate_corpus_case(case)
        evaluations.append(res)
        if res["schema_outcome"] == "ACCEPT":
            schema_accepts += 1
        else:
            schema_rejects += 1

        if res["loader_outcome"] == "ACCEPT":
            loader_accepts += 1
        else:
            loader_rejects += 1

        if not res["parity_match"]:
            divergences.append(f"{case.case_id} ({case.description}): Schema={res['schema_outcome']}, Loader={res['loader_outcome']}")
        if not res["expected_match"]:
            unexpected_outcomes.append(f"{case.case_id}: expected {case.expected_outcome}, got Schema={res['schema_outcome']}, Loader={res['loader_outcome']}")

    gate_passed = (len(divergences) == 0 and len(unexpected_outcomes) == 0)
    report = {
        "task_id": "NX-002",
        "corpus_cases_count": len(corpus),
        "schema_accepts": schema_accepts,
        "schema_rejects": schema_rejects,
        "loader_accepts": loader_accepts,
        "loader_rejects": loader_rejects,
        "parity_divergences_count": len(divergences),
        "failing_case_ids": divergences,
        "unexpected_outcomes_count": len(unexpected_outcomes),
        "unexpected_outcomes": unexpected_outcomes,
        "machine_gate": "PASS" if gate_passed else "FAIL",
    }
    return gate_passed, report

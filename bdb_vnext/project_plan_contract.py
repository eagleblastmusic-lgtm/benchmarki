"""BDB vNext - Central Project Plan v1 Contract and Parity Enforcement.

This module defines the canonical constraints, bounds, enums, and validation
primitives shared between schemas/bdb-project-plan-v1.schema.json and the
runtime loader validate_project_plan().
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

# Canonical Schema Identifiers
PROJECT_PLAN_SCHEMA = "bdb-project-plan-v1"

# ID & Identifier Constraints
ID_REGEX_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$"
ID_RE = re.compile(ID_REGEX_PATTERN)
MAX_ID_LENGTH = 96

# Top-level text constraints
MAX_PROJECT_ID_LENGTH = 96
MAX_PROJECT_NAME_LENGTH = 200
MAX_REVISION_REASON_LENGTH = 1_000
MAX_REVISION_SUMMARY_LENGTH = 4_000
MAX_CREATED_AT_LENGTH = 64

# Array count limits
MAX_MILESTONES_COUNT = 512
MAX_TASKS_COUNT = 2_048
MAX_TEXT_LIST_64_ITEMS = 64
MAX_TEXT_LIST_128_ITEMS = 128
MAX_ID_LIST_64_ITEMS = 64
MAX_CONTEXT_RECORDS_COUNT = 128

# Text length limits
MAX_MILESTONE_TITLE_LENGTH = 300
MAX_MILESTONE_DESCRIPTION_LENGTH = 4_000
MAX_TASK_TITLE_LENGTH = 300
MAX_TASK_DESCRIPTION_LENGTH = 4_000

MAX_TEXT_LIST_64_STRING_LENGTH = 8_000
MAX_TEXT_LIST_128_STRING_LENGTH = 2_000

MAX_PLANNING_OBJECTIVE_LENGTH = 8_000
MAX_ARCHITECTURE_SUMMARY_LENGTH = 8_000
MAX_RECORD_STRING_LENGTH = 4_000
MAX_RISK_SEVERITY_LENGTH = 32

# Enums
PLAN_STATUS_VALUES = frozenset({"pending", "active", "review", "completed", "blocked", "skipped"})

PLANNING_SPECIFICATION_CATEGORIES = frozenset({
    "domain", "data", "ui", "ux", "validation", "accessibility",
    "performance", "security", "testing", "release", "operations", "other",
})

DECISION_CLASSIFICATIONS = frozenset({
    "architectural_decision", "product_decision", "scope_decision", "design_decision",
    "architecture_requirement", "recommended_default", "domain_contract", "interaction_decision", "other",
})

PLANNING_CONTEXT_KEYS = frozenset({
    "objective", "requirements", "scope", "assumptions", "decisions", "open_questions", "specifications",
    "architecture", "test_strategy", "risks", "gates", "acceptance_scenarios", "definition_of_done",
})

TASK_OPTIONAL_KEYS = frozenset({"deliverables", "verification", "tests", "decision_ids", "specification_ids", "risk_ids"})

# Allowed & Required Keys per Entity
TOP_LEVEL_REQUIRED_KEYS = frozenset({"schema", "project_id", "project_name", "plan_version", "milestones", "tasks"})
TOP_LEVEL_ALLOWED_KEYS = frozenset({
    "schema", "project_id", "project_name", "plan_version", "supersedes_version", "created_at",
    "revision_reason", "revision_summary", "milestones", "tasks", "current_task_id", "planning_context",
})

MILESTONE_REQUIRED_KEYS = frozenset({"id", "title", "description"})
MILESTONE_ALLOWED_KEYS = frozenset({"id", "title", "description", "status"})

TASK_REQUIRED_KEYS = frozenset({"id", "milestone_id", "title", "description"})
TASK_ALLOWED_KEYS = frozenset({
    "id", "milestone_id", "title", "description", "status", "dependencies", "acceptance_criteria",
    *TASK_OPTIONAL_KEYS,
})

REQUIREMENTS_ALLOWED_KEYS = frozenset({"functional", "quality"})
SCOPE_ALLOWED_KEYS = frozenset({"in_scope", "out_of_scope"})

DECISION_REQUIRED_KEYS = frozenset({"id", "title", "decision"})
DECISION_ALLOWED_KEYS = frozenset({"id", "title", "decision", "rationale", "classification"})

OPEN_QUESTION_REQUIRED_KEYS = frozenset({"id", "question"})
OPEN_QUESTION_ALLOWED_KEYS = frozenset({"id", "question", "recommended_default", "owner", "deadline", "blocking_effect"})

SPECIFICATION_REQUIRED_KEYS = frozenset({"id", "category", "title", "body"})
SPECIFICATION_ALLOWED_KEYS = frozenset({"id", "category", "title", "body"})

ARCHITECTURE_ALLOWED_KEYS = frozenset({"summary", "components", "interfaces", "patterns"})
ARCHITECTURE_ITEM_REQUIRED_KEYS = frozenset({"id", "name", "responsibility"})
ARCHITECTURE_ITEM_ALLOWED_KEYS = frozenset({"id", "name", "responsibility", "interfaces"})

TEST_STRATEGY_ALLOWED_KEYS = frozenset({"unit", "integration", "e2e", "manual", "automation"})

RISK_REQUIRED_KEYS = frozenset({"id", "title", "description", "severity", "mitigation"})
RISK_ALLOWED_KEYS = frozenset({"id", "title", "description", "severity", "mitigation"})

GATE_REQUIRED_KEYS = frozenset({"id", "title", "criteria"})
GATE_ALLOWED_KEYS = frozenset({"id", "title", "criteria"})

ACCEPTANCE_SCENARIO_REQUIRED_KEYS = frozenset({"id", "title", "given", "when", "then"})
ACCEPTANCE_SCENARIO_ALLOWED_KEYS = frozenset({"id", "title", "given", "when", "then"})


class JSONSchemaValidationError(ValueError):
    def __init__(self, message: str, path: str = "") -> None:
        super().__init__(f"{path}: {message}" if path else message)
        self.path = path


def validate_project_plan_schema(
    document: Any,
    schema: Mapping[str, Any] | None = None,
) -> tuple[bool, list[str]]:
    """Validate a document against bdb-project-plan-v1 JSON Schema.

    Provides a self-contained, exact Draft 2020-12 evaluator for the project plan schema.
    """
    if schema is None:
        schema_path = Path(__file__).resolve().parents[1] / "schemas" / "bdb-project-plan-v1.schema.json"
        with open(schema_path, "r", encoding="utf-8") as f:
            schema = json.load(f)

    errors: list[str] = []

    def _resolve_ref(ref: str) -> Mapping[str, Any]:
        if ref.startswith("#/$defs/"):
            def_name = ref[len("#/$defs/"):]
            return schema.get("$defs", {}).get(def_name, {})
        return {}

    def _check(val: Any, rule: Mapping[str, Any], path: str) -> None:
        if "$ref" in rule:
            target = _resolve_ref(rule["$ref"])
            _check(val, target, path)
            return

        if "oneOf" in rule:
            match_count = 0
            sub_errors: list[str] = []
            for option in rule["oneOf"]:
                local_errs: list[str] = []
                # temporary check
                try:
                    _check_direct(val, option, path, local_errs)
                    if not local_errs:
                        match_count += 1
                except Exception:
                    pass
            if match_count != 1:
                errors.append(f"{path}: value does not match exactly oneOf branch")
            return

        _check_direct(val, rule, path, errors)

    def _check_direct(val: Any, rule: Mapping[str, Any], path: str, err_list: list[str]) -> None:
        expected_type = rule.get("type")
        if expected_type == "null":
            if val is not None:
                err_list.append(f"{path}: expected null, got {type(val).__name__}")
            return
        if expected_type == "string":
            if not isinstance(val, str) or isinstance(val, bool):
                err_list.append(f"{path}: expected string, got {type(val).__name__}")
                return
            if "minLength" in rule and len(val) < rule["minLength"]:
                err_list.append(f"{path}: string shorter than minLength {rule['minLength']}")
            if "maxLength" in rule and len(val) > rule["maxLength"]:
                err_list.append(f"{path}: string longer than maxLength {rule['maxLength']}")
            if "pattern" in rule and not re.search(rule["pattern"], val):
                err_list.append(f"{path}: string does not match pattern {rule['pattern']}")
            if "enum" in rule and val not in rule["enum"]:
                err_list.append(f"{path}: string not in enum {rule['enum']}")
            if "const" in rule and val != rule["const"]:
                err_list.append(f"{path}: string != const {rule['const']}")
            return

        if expected_type == "integer":
            if not isinstance(val, int) or isinstance(val, bool):
                err_list.append(f"{path}: expected integer, got {type(val).__name__}")
                return
            if "minimum" in rule and val < rule["minimum"]:
                err_list.append(f"{path}: integer < minimum {rule['minimum']}")
            if "maximum" in rule and val > rule["maximum"]:
                err_list.append(f"{path}: integer > maximum {rule['maximum']}")
            return

        if expected_type == "array":
            if not isinstance(val, list):
                err_list.append(f"{path}: expected array, got {type(val).__name__}")
                return
            if "maxItems" in rule and len(val) > rule["maxItems"]:
                err_list.append(f"{path}: array item count {len(val)} > maxItems {rule['maxItems']}")
            if "minItems" in rule and len(val) < rule["minItems"]:
                err_list.append(f"{path}: array item count {len(val)} < minItems {rule['minItems']}")
            if rule.get("uniqueItems") is True:
                # Check uniqueness for primitives or dicts
                seen = []
                for item in val:
                    if item in seen:
                        err_list.append(f"{path}: duplicate items found where uniqueItems is required")
                        break
                    seen.append(item)
            if "items" in rule:
                item_rule = rule["items"]
                for idx, item in enumerate(val):
                    _check(item, item_rule, f"{path}[{idx}]")
            return

        if expected_type == "object":
            if not isinstance(val, Mapping):
                err_list.append(f"{path}: expected object, got {type(val).__name__}")
                return
            req = rule.get("required", [])
            for r in req:
                if r not in val:
                    err_list.append(f"{path}: missing required property '{r}'")
            props = rule.get("properties", {})
            if rule.get("additionalProperties") is False:
                for k in val:
                    if k not in props:
                        err_list.append(f"{path}: unknown additional property '{k}' not allowed")
            for k, v in val.items():
                if k in props:
                    _check(v, props[k], f"{path}.{k}" if path else k)
            return

    _check(document, schema, "")
    return (len(errors) == 0, errors)

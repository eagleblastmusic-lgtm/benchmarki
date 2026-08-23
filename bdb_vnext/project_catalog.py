"""Bounded canonical project catalog and Project Plan v1.

This module owns only project metadata and imported plan identity.  It is a
small vNext authority under the existing runtime root; it never reads Legacy
workspace/session state and never executes GitHub or Browser operations.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from bdb_shared.evidence import canonical_json_bytes, semantic_digest


PROJECT_CATALOG_SCHEMA = "bdb-vnext-project-catalog-v1"
PROJECT_PLAN_SCHEMA = "bdb-project-plan-v1"
PROJECT_CATALOG_RELATIVE_PATH = Path("control") / "project-catalog.json"
PROJECT_CATALOG_MAX_BYTES = 2 * 1024 * 1024
PROJECT_PLAN_MAX_BYTES = 1024 * 1024
PROJECT_STATUS_VALUES = frozenset({"new", "active", "paused", "blocked", "completed", "archived", "unknown"})
PLAN_STATUS_VALUES = frozenset({"pending", "active", "review", "completed", "blocked", "skipped"})
PLANNING_SPECIFICATION_CATEGORIES = frozenset({"domain", "data", "ui", "ux", "validation", "accessibility", "performance", "security", "testing", "release", "operations", "other"})
DECISION_CLASSIFICATIONS = frozenset({
    "architectural_decision", "product_decision", "scope_decision", "design_decision",
    "architecture_requirement", "recommended_default", "domain_contract", "interaction_decision", "other",
})
PLANNING_CONTEXT_KEYS = frozenset({
    "objective", "requirements", "scope", "assumptions", "decisions", "open_questions", "specifications",
    "architecture", "test_strategy", "risks", "gates", "acceptance_scenarios", "definition_of_done",
})
TASK_OPTIONAL_KEYS = frozenset({"deliverables", "verification", "tests", "decision_ids", "specification_ids", "risk_ids"})
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$")
_ALIAS_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_GITHUB_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")


class ProjectCatalogError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> None:
    raise ProjectCatalogError(code, message)


def _text(value: object, field_name: str, *, max_length: int = 8_000, required: bool = True) -> str:
    if not isinstance(value, str):
        _fail("project_field_invalid", f"{field_name} must be text")
    result = value.strip()
    if required and not result:
        _fail("project_field_invalid", f"{field_name} must not be empty")
    if len(result) > max_length:
        _fail("project_field_too_large", f"{field_name} exceeds its bound")
    return result


def _list_of_text(value: object, field_name: str, *, max_items: int = 128) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > max_items:
        _fail("project_field_invalid", f"{field_name} must be a bounded list")
    return tuple(_text(item, f"{field_name}[]", max_length=1_000) for item in value)


def _require_mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail("plan_field_invalid", f"{field_name} must be an object")
    return value


def _reject_unknown_keys(value: Mapping[str, Any], allowed: set[str], field_name: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        _fail("plan_field_unknown", f"{field_name} contains unsupported fields: {', '.join(unknown)}")


def _bounded_text_list(value: object, field_name: str, *, max_items: int = 128, max_length: int = 2_000) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > max_items:
        _fail("plan_field_invalid", f"{field_name} must be a bounded list")
    return [_text(item, f"{field_name}[]", max_length=max_length) for item in value]


def _record_list(value: object, field_name: str, *, required: set[str], optional: set[str], max_items: int = 128) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > max_items:
        _fail("plan_field_invalid", f"{field_name} must be a bounded list")
    records: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    allowed = required | optional
    for index, raw in enumerate(value):
        item = _require_mapping(raw, f"{field_name}[{index}]")
        _reject_unknown_keys(item, allowed, f"{field_name}[{index}]")
        missing = required - set(item)
        if missing:
            _fail("plan_field_invalid", f"{field_name}[{index}] is missing: {', '.join(sorted(missing))}")
        identifier = _text(item.get("id"), f"{field_name}[{index}].id", max_length=96)
        if not _ID_RE.fullmatch(identifier) or identifier in identifiers:
            _fail("plan_field_invalid", f"{field_name} IDs must be unique and bounded")
        identifiers.add(identifier)
        normalized: dict[str, Any] = {"id": identifier}
        for key in sorted(set(item) - {"id"}):
            if key == "interfaces":
                normalized[key] = _bounded_text_list(item[key], f"{field_name}[{index}].interfaces", max_items=64, max_length=8_000)
            else:
                normalized[key] = _text(item[key], f"{field_name}[{index}].{key}", max_length=4_000)
        records.append(normalized)
    return records


def _planning_context(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    context = _require_mapping(value, "planning_context")
    _reject_unknown_keys(context, set(PLANNING_CONTEXT_KEYS), "planning_context")
    normalized: dict[str, Any] = {}
    if "objective" in context:
        normalized["objective"] = _text(context["objective"], "planning_context.objective", max_length=8_000)
    for key in ("requirements", "scope"):
        if key not in context:
            continue
        section = _require_mapping(context[key], f"planning_context.{key}")
        names = ("functional", "quality") if key == "requirements" else ("in_scope", "out_of_scope")
        _reject_unknown_keys(section, set(names), f"planning_context.{key}")
        normalized[key] = {name: _bounded_text_list(section.get(name), f"planning_context.{key}.{name}") for name in names if name in section}
    if "assumptions" in context:
        normalized["assumptions"] = _bounded_text_list(context["assumptions"], "planning_context.assumptions")
    if "decisions" in context:
        decisions = _record_list(context["decisions"], "planning_context.decisions", required={"id", "title", "decision"}, optional={"rationale", "classification"})
        for item in decisions:
            classification = item.get("classification")
            if classification is not None and classification not in DECISION_CLASSIFICATIONS:
                _fail("plan_field_invalid", "planning_context.decisions classification is unsupported")
        normalized["decisions"] = decisions
    if "open_questions" in context:
        normalized["open_questions"] = _record_list(
            context["open_questions"],
            "planning_context.open_questions",
            required={"id", "question"},
            optional={"recommended_default", "owner", "deadline", "blocking_effect"},
            max_items=128,
        )
    if "specifications" in context:
        specifications = _record_list(context["specifications"], "planning_context.specifications", required={"id", "category", "title", "body"}, optional=set())
        for item in specifications:
            if item["category"] not in PLANNING_SPECIFICATION_CATEGORIES:
                _fail("plan_field_invalid", "planning_context.specifications category is unsupported")
        normalized["specifications"] = specifications
    if "architecture" in context:
        architecture = _require_mapping(context["architecture"], "planning_context.architecture")
        _reject_unknown_keys(architecture, {"summary", "components", "interfaces", "patterns"}, "planning_context.architecture")
        normalized_architecture: dict[str, Any] = {}
        if "summary" in architecture:
            normalized_architecture["summary"] = _text(architecture["summary"], "planning_context.architecture.summary", max_length=8_000)
        if "components" in architecture:
            normalized_architecture["components"] = _record_list(architecture["components"], "planning_context.architecture.components", required={"id", "name", "responsibility"}, optional={"interfaces"})
        if "interfaces" in architecture:
            normalized_architecture["interfaces"] = _record_list(architecture["interfaces"], "planning_context.architecture.interfaces", required={"id", "name", "responsibility"}, optional=set())
        if "patterns" in architecture:
            normalized_architecture["patterns"] = _bounded_text_list(architecture["patterns"], "planning_context.architecture.patterns")
        normalized["architecture"] = normalized_architecture
    if "test_strategy" in context:
        strategy = _require_mapping(context["test_strategy"], "planning_context.test_strategy")
        _reject_unknown_keys(strategy, {"unit", "integration", "e2e", "manual", "automation"}, "planning_context.test_strategy")
        normalized["test_strategy"] = {name: _bounded_text_list(strategy.get(name), f"planning_context.test_strategy.{name}") for name in ("unit", "integration", "e2e", "manual", "automation") if name in strategy}
    if "risks" in context:
        normalized["risks"] = _record_list(context["risks"], "planning_context.risks", required={"id", "title", "description", "severity", "mitigation"}, optional=set())
    if "gates" in context:
        normalized["gates"] = _record_list(context["gates"], "planning_context.gates", required={"id", "title", "criteria"}, optional=set())
    if "acceptance_scenarios" in context:
        normalized["acceptance_scenarios"] = _record_list(context["acceptance_scenarios"], "planning_context.acceptance_scenarios", required={"id", "title", "given", "when", "then"}, optional=set())
    if "definition_of_done" in context:
        normalized["definition_of_done"] = _bounded_text_list(context["definition_of_done"], "planning_context.definition_of_done")
    return normalized


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _timestamp(value: object, field_name: str) -> str:
    text = _text(value, field_name, max_length=64)
    if not text.endswith("Z"):
        _fail("project_timestamp_invalid", f"{field_name} must use UTC Z form")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        _fail("project_timestamp_invalid", f"{field_name} is not a timestamp")
        raise AssertionError from exc
    if parsed.tzinfo is None:
        _fail("project_timestamp_invalid", f"{field_name} must include timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class ProjectBrief:
    name: str
    goal: str
    description: str
    project_type: str
    technologies: tuple[str, ...] = ()
    features: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    pinned_files: tuple[str, ...] = ()
    environment_hints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.name, "brief.name", max_length=200)
        _text(self.goal, "brief.goal", max_length=4_000)
        _text(self.description, "brief.description", max_length=8_000)
        _text(self.project_type, "brief.project_type", max_length=120)
        for name, values in (("technologies", self.technologies), ("features", self.features), ("constraints", self.constraints), ("environment_hints", self.environment_hints)):
            if len(values) > 128 or any(not isinstance(item, str) or not item.strip() or len(item) > 1_000 for item in values):
                _fail("brief_invalid", f"brief.{name} is invalid")
        if len(self.pinned_files) > 64 or any(not isinstance(item, str) or not item.strip() or len(item) > 260 or Path(item).is_absolute() or ".." in item.replace("\\", "/").split("/") for item in self.pinned_files):
            _fail("brief_pinned_files_invalid", "brief.pinned_files must contain bounded repo-relative paths")

    def to_dict(self) -> dict[str, Any]:
        document = {
            "name": self.name,
            "goal": self.goal,
            "description": self.description,
            "project_type": self.project_type,
            "technologies": list(self.technologies),
            "features": list(self.features),
            "constraints": list(self.constraints),
        }
        if self.pinned_files:
            document["pinned_files"] = list(self.pinned_files)
        if self.environment_hints:
            document["environment_hints"] = list(self.environment_hints)
        return document

    @classmethod
    def from_dict(cls, value: object) -> "ProjectBrief":
        if not isinstance(value, Mapping):
            _fail("brief_invalid", "brief must be an object")
        return cls(
            name=_text(value.get("name"), "brief.name", max_length=200),
            goal=_text(value.get("goal"), "brief.goal", max_length=4_000),
            description=_text(value.get("description"), "brief.description", max_length=8_000),
            project_type=_text(value.get("project_type"), "brief.project_type", max_length=120),
            technologies=_list_of_text(value.get("technologies"), "brief.technologies"),
            features=_list_of_text(value.get("features"), "brief.features"),
            constraints=_list_of_text(value.get("constraints"), "brief.constraints"),
            pinned_files=_list_of_text(value.get("pinned_files"), "brief.pinned_files", max_items=64),
            environment_hints=_list_of_text(value.get("environment_hints"), "brief.environment_hints"),
        )


@dataclass(frozen=True)
class ProjectTask:
    task_id: str
    milestone_id: str
    title: str
    description: str
    status: str = "pending"
    dependencies: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    deliverables: tuple[str, ...] = ()
    verification: tuple[str, ...] = ()
    tests: tuple[str, ...] = ()
    decision_ids: tuple[str, ...] = ()
    specification_ids: tuple[str, ...] = ()
    risk_ids: tuple[str, ...] = ()
    # Preserve explicit empty rich fields on round-trip without changing the
    # digest/shape of legacy plans that never carried them.
    optional_fields: frozenset[str] = frozenset()

    def to_dict(self) -> dict[str, Any]:
        document = {
            "id": self.task_id,
            "milestone_id": self.milestone_id,
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "dependencies": list(self.dependencies),
            "acceptance_criteria": list(self.acceptance_criteria),
        }
        for key, value in (("deliverables", self.deliverables), ("verification", self.verification), ("tests", self.tests), ("decision_ids", self.decision_ids), ("specification_ids", self.specification_ids), ("risk_ids", self.risk_ids)):
            if value or key in self.optional_fields:
                document[key] = list(value)
        return document


@dataclass(frozen=True)
class ProjectMilestone:
    milestone_id: str
    title: str
    description: str
    status: str = "pending"

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.milestone_id, "title": self.title, "description": self.description, "status": self.status}


@dataclass(frozen=True)
class ProjectPlan:
    project_id: str
    project_name: str
    plan_version: str
    milestones: tuple[ProjectMilestone, ...]
    tasks: tuple[ProjectTask, ...]
    current_task_id: str | None = None
    schema: str = PROJECT_PLAN_SCHEMA
    # These fields were appended so positional construction from Slice 2
    # remains source-compatible.  A missing value is a legacy v1 plan.
    supersedes_version: str | None = None
    created_at: str | None = None
    revision_reason: str | None = None
    revision_summary: str | None = None
    planning_context: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        document = {
            "schema": self.schema,
            "project_id": self.project_id,
            "project_name": self.project_name,
            "plan_version": self.plan_version,
            "milestones": [item.to_dict() for item in self.milestones],
            "tasks": [item.to_dict() for item in self.tasks],
            "current_task_id": self.current_task_id,
        }
        # Do not add absent optional keys to old imported plans: this keeps a
        # legacy plan's digest stable while allowing new versions to carry the
        # complete immutable-history metadata.
        for key, value in (("supersedes_version", self.supersedes_version), ("created_at", self.created_at), ("revision_reason", self.revision_reason), ("revision_summary", self.revision_summary)):
            if value is not None:
                document[key] = value
        if self.planning_context is not None:
            document["planning_context"] = json.loads(json.dumps(self.planning_context, ensure_ascii=False, sort_keys=True))
        return document

    @property
    def completed_tasks(self) -> int:
        return sum(task.status == "completed" for task in self.tasks)

    @property
    def current_task(self) -> ProjectTask | None:
        if self.current_task_id is None:
            return None
        return next((task for task in self.tasks if task.task_id == self.current_task_id), None)

    @property
    def current_milestone(self) -> ProjectMilestone | None:
        task = self.current_task
        if task is None:
            return None
        return next((milestone for milestone in self.milestones if milestone.milestone_id == task.milestone_id), None)


def validate_project_plan(value: object, *, expected_project_id: str | None = None) -> ProjectPlan:
    if not isinstance(value, Mapping) or value.get("schema") != PROJECT_PLAN_SCHEMA:
        _fail("plan_schema_invalid", "project plan schema must be bdb-project-plan-v1")
    _reject_unknown_keys(value, {"schema", "project_id", "project_name", "plan_version", "supersedes_version", "created_at", "revision_reason", "revision_summary", "milestones", "tasks", "current_task_id", "planning_context"}, "project plan")
    project_id = _text(value.get("project_id"), "project_id", max_length=96)
    if not _ID_RE.fullmatch(project_id):
        _fail("plan_project_id_invalid", "project_id has an unsafe format")
    if expected_project_id is not None and project_id != expected_project_id:
        _fail("plan_project_mismatch", "plan project_id does not match the selected project")
    project_name = _text(value.get("project_name"), "project_name", max_length=200)
    raw_version = value.get("plan_version")
    if isinstance(raw_version, bool) or not isinstance(raw_version, (str, int)):
        _fail("plan_version_invalid", "plan_version must be an integer version")
    plan_version = str(raw_version).strip()
    if not re.fullmatch(r"\d+(?:\.0+)?", plan_version):
        _fail("plan_version_invalid", "plan_version must be an integer version")
    supersedes = value.get("supersedes_version")
    if supersedes is not None:
        if isinstance(supersedes, bool) or not isinstance(supersedes, (str, int)):
            _fail("plan_supersedes_invalid", "supersedes_version must be an integer version")
        supersedes = str(supersedes).strip()
        if not re.fullmatch(r"\d+(?:\.0+)?", supersedes):
            _fail("plan_supersedes_invalid", "supersedes_version must be an integer version")
    created_at = value.get("created_at")
    if created_at is not None:
        created_at = _timestamp(created_at, "created_at")
    revision_reason = value.get("revision_reason")
    if revision_reason is not None:
        revision_reason = _text(revision_reason, "revision_reason", max_length=1_000)
    revision_summary = value.get("revision_summary")
    if revision_summary is not None:
        revision_summary = _text(revision_summary, "revision_summary", max_length=4_000)
    milestones_raw = value.get("milestones")
    tasks_raw = value.get("tasks")
    if not isinstance(milestones_raw, list) or not isinstance(tasks_raw, list) or len(milestones_raw) > 512 or len(tasks_raw) > 2_048:
        _fail("plan_size_invalid", "milestones/tasks exceed bounded limits")
    milestones: list[ProjectMilestone] = []
    milestone_ids: set[str] = set()
    for raw in milestones_raw:
        if not isinstance(raw, Mapping):
            _fail("plan_milestone_invalid", "milestone must be an object")
        identifier = _text(raw.get("id"), "milestone.id", max_length=96)
        if not _ID_RE.fullmatch(identifier) or identifier in milestone_ids:
            _fail("plan_milestone_invalid", "milestone IDs must be unique and bounded")
        status = _text(raw.get("status", "pending"), "milestone.status", max_length=16)
        if status not in PLAN_STATUS_VALUES:
            _fail("plan_status_invalid", "milestone status is unsupported")
        milestone_ids.add(identifier)
        milestones.append(ProjectMilestone(identifier, _text(raw.get("title"), "milestone.title", max_length=300), _text(raw.get("description"), "milestone.description", max_length=4_000), status))
    planning_context = _planning_context(value.get("planning_context"))
    gate_ids = {item["id"] for item in (planning_context or {}).get("gates", [])}
    open_question_ids = {item["id"] for item in (planning_context or {}).get("open_questions", [])}
    tasks: list[ProjectTask] = []
    task_ids: set[str] = set()
    for raw in tasks_raw:
        if not isinstance(raw, Mapping):
            _fail("plan_task_invalid", "task must be an object")
        _reject_unknown_keys(raw, {"id", "milestone_id", "title", "description", "status", "dependencies", "acceptance_criteria", *TASK_OPTIONAL_KEYS}, "task")
        identifier = _text(raw.get("id"), "task.id", max_length=96)
        milestone_id = _text(raw.get("milestone_id"), "task.milestone_id", max_length=96)
        if not _ID_RE.fullmatch(identifier) or identifier in task_ids or milestone_id not in milestone_ids:
            _fail("plan_task_invalid", "task IDs and milestone references must be valid")
        status = _text(raw.get("status", "pending"), "task.status", max_length=16)
        if status not in PLAN_STATUS_VALUES:
            _fail("plan_status_invalid", "task status is unsupported")
        dependencies = _list_of_text(raw.get("dependencies"), "task.dependencies", max_items=64)
        acceptance = _list_of_text(raw.get("acceptance_criteria"), "task.acceptance_criteria", max_items=64)
        if any(not _ID_RE.fullmatch(item) for item in dependencies) or len(set(dependencies)) != len(dependencies):
            _fail("plan_dependency_invalid", "task dependency IDs must be safe and unique")
        deliverables = _bounded_text_list(raw.get("deliverables"), "task.deliverables", max_items=64, max_length=8_000)
        verification = _bounded_text_list(raw.get("verification"), "task.verification", max_items=64, max_length=8_000)
        tests = _bounded_text_list(raw.get("tests"), "task.tests", max_items=64, max_length=8_000)
        decision_ids = _list_of_text(raw.get("decision_ids"), "task.decision_ids", max_items=64)
        specification_ids = _list_of_text(raw.get("specification_ids"), "task.specification_ids", max_items=64)
        risk_ids = _list_of_text(raw.get("risk_ids"), "task.risk_ids", max_items=64)
        optional_fields = frozenset(key for key in TASK_OPTIONAL_KEYS if key in raw)
        for field_name, identifiers in (("decision_ids", decision_ids), ("specification_ids", specification_ids), ("risk_ids", risk_ids)):
            if any(not _ID_RE.fullmatch(item) for item in identifiers) or len(set(identifiers)) != len(identifiers):
                _fail("plan_reference_invalid", f"task.{field_name} contains unsafe or duplicate IDs")
        task_ids.add(identifier)
        tasks.append(ProjectTask(identifier, milestone_id, _text(raw.get("title"), "task.title", max_length=300), _text(raw.get("description"), "task.description", max_length=4_000), status, dependencies, acceptance, tuple(deliverables), tuple(verification), tuple(tests), tuple(decision_ids), tuple(specification_ids), tuple(risk_ids), optional_fields))
    dependency_kinds: dict[str, set[str]] = {}
    for task in tasks:
        for dependency in task.dependencies:
            kinds = dependency_kinds.setdefault(dependency, set())
            if dependency in task_ids:
                kinds.add("task")
            if dependency in gate_ids:
                kinds.add("gate")
            if dependency in open_question_ids:
                kinds.add("open_question")
            if not kinds:
                _fail("plan_dependency_missing", f"task dependency target does not exist: {dependency}")
            if len(kinds) > 1:
                _fail("plan_dependency_ambiguous", f"task dependency target has multiple namespaces: {dependency}")
    graph = {task.task_id: {dependency for dependency in task.dependencies if dependency in task_ids} for task in tasks}
    visiting: set[str] = set()
    visited: set[str] = set()
    def visit(identifier: str) -> None:
        if identifier in visiting:
            _fail("plan_dependency_cycle", "task dependency graph contains a cycle")
        if identifier in visited:
            return
        visiting.add(identifier)
        for dependency in graph[identifier]:
            visit(dependency)
        visiting.remove(identifier)
        visited.add(identifier)
    for identifier in graph:
        visit(identifier)
    active = [task.task_id for task in tasks if task.status == "active"]
    if len(active) > 1:
        _fail("plan_active_task_ambiguous", "at most one task may be active")
    current_task_id = value.get("current_task_id")
    if current_task_id is not None:
        current_task_id = _text(current_task_id, "current_task_id", max_length=96)
        if current_task_id not in task_ids:
            _fail("plan_current_task_missing", "current_task_id does not exist")
    elif active:
        current_task_id = active[0]
    if planning_context is not None:
        decision_ids = {item["id"] for item in planning_context.get("decisions", [])}
        specification_ids = {item["id"] for item in planning_context.get("specifications", [])}
        risk_ids = {item["id"] for item in planning_context.get("risks", [])}
        for task in tasks:
            if any(item not in decision_ids for item in task.decision_ids):
                _fail("plan_reference_missing", f"task {task.task_id} references an unknown decision")
            if any(item not in specification_ids for item in task.specification_ids):
                _fail("plan_reference_missing", f"task {task.task_id} references an unknown specification")
            if any(item not in risk_ids for item in task.risk_ids):
                _fail("plan_reference_missing", f"task {task.task_id} references an unknown risk")
    elif any(task.decision_ids or task.specification_ids or task.risk_ids for task in tasks):
        _fail("plan_reference_missing", "task rich references require planning_context")
    return ProjectPlan(project_id, project_name, plan_version, tuple(milestones), tuple(tasks), current_task_id, PROJECT_PLAN_SCHEMA, supersedes, created_at, revision_reason, revision_summary, planning_context)


def classify_dependency_targets(plan: ProjectPlan) -> dict[str, str]:
    """Return the canonical namespace for each dependency target in a validated plan."""
    task_ids = {task.task_id for task in plan.tasks}
    context = plan.planning_context or {}
    gate_ids = {item["id"] for item in context.get("gates", [])}
    open_question_ids = {item["id"] for item in context.get("open_questions", [])}
    result: dict[str, str] = {}
    for identifier in sorted(task_ids | gate_ids | open_question_ids):
        kinds = [kind for kind, identifiers in (("task", task_ids), ("gate", gate_ids), ("open_question", open_question_ids)) if identifier in identifiers]
        if len(kinds) == 1:
            result[identifier] = kinds[0]
    return result


@dataclass(frozen=True)
class ProjectRecord:
    project_id: str
    display_name: str
    repo_alias: str
    local_repo_path: str
    github_repo: str | None
    created_at: str
    project_status: str
    brief: ProjectBrief
    plan_imported: bool = False
    plan_version: str | None = None
    total_tasks: int = 0
    completed_tasks: int = 0
    current_milestone: str | None = None
    current_task: str | None = None
    plan_path: str | None = None
    last_launch_id: str | None = None
    last_session_id: str | None = None
    last_correlation_id: str | None = None

    def __post_init__(self) -> None:
        if not _ID_RE.fullmatch(self.project_id):
            _fail("project_id_invalid", "project_id is unsafe")
        if _ALIAS_RE.fullmatch(self.repo_alias) is None:
            _fail("repo_alias_invalid", "repo_alias is unsafe")
        _text(self.display_name, "display_name", max_length=200)
        local = Path(self.local_repo_path).expanduser()
        if not local.is_absolute():
            _fail("project_path_invalid", "local_repo_path must be absolute")
        if self.github_repo is not None and _GITHUB_RE.fullmatch(self.github_repo) is None:
            _fail("github_repo_invalid", "github_repo must be owner/name")
        if self.project_status not in PROJECT_STATUS_VALUES:
            _fail("project_status_invalid", "project_status is unsupported")
        if self.total_tasks < 0 or self.completed_tasks < 0 or self.completed_tasks > self.total_tasks:
            _fail("project_progress_invalid", "project progress is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "display_name": self.display_name,
            "repo_alias": self.repo_alias,
            "local_repo_path": self.local_repo_path,
            "github_repo": self.github_repo,
            "created_at": self.created_at,
            "project_status": self.project_status,
            "brief": self.brief.to_dict(),
            "plan": {
                "imported": self.plan_imported,
                "path": self.plan_path,
                "version": self.plan_version,
                "total_tasks": self.total_tasks,
                "completed_tasks": self.completed_tasks,
                "current_milestone": self.current_milestone,
                "current_task": self.current_task,
            },
            "conversation": {
                "last_launch_id": self.last_launch_id,
                "last_session_id": self.last_session_id,
                "last_correlation_id": self.last_correlation_id,
            },
        }

    @classmethod
    def from_dict(cls, value: object) -> "ProjectRecord":
        if not isinstance(value, Mapping):
            _fail("catalog_project_invalid", "catalog project must be an object")
        plan = value.get("plan") if isinstance(value.get("plan"), Mapping) else {}
        conversation = value.get("conversation") if isinstance(value.get("conversation"), Mapping) else {}
        return cls(
            project_id=_text(value.get("project_id"), "project_id", max_length=96),
            display_name=_text(value.get("display_name"), "display_name", max_length=200),
            repo_alias=_text(value.get("repo_alias"), "repo_alias", max_length=32),
            local_repo_path=_text(value.get("local_repo_path"), "local_repo_path", max_length=2_000),
            github_repo=value.get("github_repo") if value.get("github_repo") is None else _text(value.get("github_repo"), "github_repo", max_length=240),
            created_at=_timestamp(value.get("created_at"), "created_at"),
            project_status=_text(value.get("project_status", "new"), "project_status", max_length=16),
            brief=ProjectBrief.from_dict(value.get("brief")),
            plan_imported=bool(plan.get("imported", False)),
            plan_version=plan.get("version") if plan.get("version") is None else _text(plan.get("version"), "plan.version", max_length=64),
            total_tasks=int(plan.get("total_tasks", 0)),
            completed_tasks=int(plan.get("completed_tasks", 0)),
            current_milestone=plan.get("current_milestone") if plan.get("current_milestone") is None else _text(plan.get("current_milestone"), "plan.current_milestone", max_length=200),
            current_task=plan.get("current_task") if plan.get("current_task") is None else _text(plan.get("current_task"), "plan.current_task", max_length=96),
            plan_path=plan.get("path") if plan.get("path") is None else _text(plan.get("path"), "plan.path", max_length=2_000),
            last_launch_id=conversation.get("last_launch_id") if conversation.get("last_launch_id") is None else _text(conversation.get("last_launch_id"), "conversation.last_launch_id", max_length=96),
            last_session_id=conversation.get("last_session_id") if conversation.get("last_session_id") is None else _text(conversation.get("last_session_id"), "conversation.last_session_id", max_length=200),
            last_correlation_id=conversation.get("last_correlation_id") if conversation.get("last_correlation_id") is None else _text(conversation.get("last_correlation_id"), "conversation.last_correlation_id", max_length=200),
        )


def catalog_path(runtime_root: str | Path) -> Path:
    root = Path(runtime_root).expanduser().absolute()
    if root.exists() and root.is_symlink():
        _fail("catalog_root_invalid", "project catalog runtime root must not be a symlink")
    return root / PROJECT_CATALOG_RELATIVE_PATH


def _atomic_write(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json_bytes(dict(document))
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


class ProjectCatalog:
    """Single bounded writer for vNext project metadata."""

    def __init__(self, runtime_root: str | Path) -> None:
        self.runtime_root = Path(runtime_root).expanduser().absolute()
        self.path = catalog_path(self.runtime_root)

    def read(self) -> tuple[ProjectRecord, ...]:
        if not self.path.exists():
            return ()
        if self.path.is_symlink() or not self.path.is_file():
            _fail("catalog_path_invalid", "project catalog must be a regular file")
        payload = self.path.read_bytes()
        if len(payload) > PROJECT_CATALOG_MAX_BYTES:
            _fail("catalog_too_large", "project catalog exceeds its bound")
        try:
            document = json.loads(payload.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProjectCatalogError("catalog_corrupt", "project catalog is not valid JSON") from exc
        if not isinstance(document, Mapping) or document.get("schema") != PROJECT_CATALOG_SCHEMA:
            _fail("catalog_schema_invalid", "project catalog schema is unsupported")
        projects_raw = document.get("projects")
        if not isinstance(projects_raw, list) or len(projects_raw) > 256:
            _fail("catalog_shape_invalid", "project catalog projects must be bounded")
        projects = tuple(ProjectRecord.from_dict(item) for item in projects_raw)
        if len({project.project_id for project in projects}) != len(projects):
            _fail("catalog_duplicate_project", "project IDs must be unique")
        supplied_digest = document.get("catalog_digest")
        without_digest = {"schema": PROJECT_CATALOG_SCHEMA, "projects": [project.to_dict() for project in projects]}
        if supplied_digest != semantic_digest(without_digest):
            _fail("catalog_digest_mismatch", "project catalog digest differs")
        return projects

    def write(self, projects: Iterable[ProjectRecord]) -> None:
        ordered = tuple(sorted(projects, key=lambda item: (item.display_name.casefold(), item.project_id)))
        if len(ordered) > 256:
            _fail("catalog_size_invalid", "project catalog is bounded to 256 projects")
        base = {"schema": PROJECT_CATALOG_SCHEMA, "projects": [project.to_dict() for project in ordered]}
        _atomic_write(self.path, {**base, "catalog_digest": semantic_digest(base)})

    def get(self, project_id: str) -> ProjectRecord | None:
        return next((project for project in self.read() if project.project_id == project_id), None)

    def upsert(self, project: ProjectRecord) -> ProjectRecord:
        projects = [item for item in self.read() if item.project_id != project.project_id]
        projects.append(project)
        self.write(projects)
        return project

    def import_plan(self, project_id: str, plan_path: str | Path) -> tuple[ProjectRecord, ProjectPlan]:
        project = self.get(project_id)
        if project is None:
            _fail("project_not_found", "project is not in the canonical catalog")
        source = Path(plan_path).expanduser().absolute()
        if source.is_symlink() or not source.is_file():
            _fail("plan_path_invalid", "project-plan.json must be a regular file")
        payload = source.read_bytes()
        if len(payload) > PROJECT_PLAN_MAX_BYTES:
            _fail("plan_too_large", "project plan exceeds its bound")
        try:
            document = json.loads(payload.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProjectCatalogError("plan_json_invalid", "project plan is not valid JSON") from exc
        plan = validate_project_plan(document, expected_project_id=project_id)
        # Project Memory is the canonical writer for immutable plan history.
        # The lazy import avoids a module cycle while preserving the Slice 2
        # catalog API for callers that only need the summary record.
        try:
            from .project_memory import ProjectMemoryError, ProjectMemoryStore

            memory = ProjectMemoryStore(self.runtime_root, project_id)
            current = memory.current_plan()
            if current is None and project.plan_imported:
                # Explicitly seed legacy Slice 2 metadata once.  This is a
                # migration from the old external plan file, never a silent
                # overwrite of history.
                legacy_source = Path(project.plan_path).expanduser() if project.plan_path else None
                if legacy_source is None or not legacy_source.is_file() or legacy_source.resolve() == source.resolve():
                    if str(plan.plan_version).split(".", 1)[0] != "1":
                        _fail("plan_history_seed_missing", "legacy project history cannot seed a non-v1 successor")
                    memory.ensure_initial_plan(plan)
                    current = memory.current_plan()
                else:
                    legacy_document = json.loads(legacy_source.read_text(encoding="utf-8-sig"))
                    legacy_plan = validate_project_plan(legacy_document, expected_project_id=project_id)
                    memory.ensure_initial_plan(legacy_plan)
                    current = memory.current_plan()
            if current is None:
                canonical_plan = memory.ensure_initial_plan(plan)
            else:
                preview = memory.preview_update(plan)
                if not preview.accepted:
                    _fail(preview.reason_code or "plan_update_rejected", "plan update was not accepted")
                canonical_plan = memory.apply_update(plan, preview)
        except ProjectMemoryError as exc:
            raise ProjectCatalogError(exc.code, str(exc)) from exc
        except OSError as exc:
            raise ProjectCatalogError("plan_history_io_failed", str(exc)) from exc
        # Summary remains in the catalog for fast list rendering; immutable
        # bytes live under the project-memory canonical root.
        plan = canonical_plan
        current_milestone = plan.current_milestone.title if plan.current_milestone else None
        updated = ProjectRecord(
            **{**project.__dict__, "plan_imported": True, "plan_version": plan.plan_version, "total_tasks": len(plan.tasks), "completed_tasks": plan.completed_tasks, "current_milestone": current_milestone, "current_task": plan.current_task_id, "plan_path": str(memory.current_pointer), "project_status": "active"}
        )
        self.upsert(updated)
        return updated, plan


def new_project_record(*, project_id: str | None, display_name: str, repo_alias: str, local_repo_path: str | Path, github_repo: str | None, brief: ProjectBrief) -> ProjectRecord:
    identifier = project_id or str(uuid.uuid4())
    return ProjectRecord(identifier, display_name.strip(), repo_alias.strip().lower(), str(Path(local_repo_path).expanduser().absolute()), github_repo, _utc_now(), "new", brief)


__all__ = [
    "PROJECT_CATALOG_SCHEMA",
    "PROJECT_PLAN_SCHEMA",
    "DECISION_CLASSIFICATIONS",
    "PLANNING_CONTEXT_KEYS",
    "PLANNING_SPECIFICATION_CATEGORIES",
    "TASK_OPTIONAL_KEYS",
    "ProjectBrief",
    "ProjectCatalog",
    "ProjectCatalogError",
    "ProjectMilestone",
    "ProjectPlan",
    "ProjectRecord",
    "ProjectTask",
    "catalog_path",
    "classify_dependency_targets",
    "new_project_record",
    "validate_project_plan",
]

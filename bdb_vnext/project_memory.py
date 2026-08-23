"""Canonical project memory, immutable plan history and deterministic next action.

This is metadata authority only.  It deliberately does not observe Git logs,
execute repository work, or infer task completion from runtime results.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence, TypeVar

from bdb_shared.evidence import canonical_json_bytes, semantic_digest

from .project_catalog import ProjectPlan, ProjectRecord, ProjectTask, classify_dependency_targets, validate_project_plan


PROJECT_MEMORY_SCHEMA = "bdb-vnext-project-memory-v1"
PROJECT_PLAN_POINTER_SCHEMA = "bdb-project-current-plan-v1"
PROJECT_PLAN_UPDATE_PREVIEW_SCHEMA = "bdb-project-plan-update-preview-v1"
PROJECT_EVENT_SCHEMA = "bdb-project-event-v1"
PROJECT_DECISION_SCHEMA = "bdb-project-decision-v1"
PROJECT_INBOX_SCHEMA = "bdb-project-inbox-v1"
PROJECT_RISK_SCHEMA = "bdb-project-risk-v1"
PROJECT_DEBT_SCHEMA = "bdb-project-debt-v1"
PROJECT_ATTENTION_SCHEMA = "bdb-project-attention-v1"
PROJECT_CHECKPOINT_SCHEMA = "bdb-project-checkpoint-v1"
MAX_MEMORY_BYTES = 4 * 1024 * 1024
MAX_EVENTS = 2_048
MAX_ITEMS = 512
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$")
_PLAN_VERSION_RE = re.compile(r"^(\d+)(?:\.0+)?$")
_STATUS_VALUES = frozenset({"pending", "active", "review", "completed", "blocked", "skipped"})
GATE_STATUS_VALUES = frozenset({"pending", "passed"})
OPEN_QUESTION_STATUS_VALUES = frozenset({"open", "resolved"})
_EVENT_TYPES = frozenset({
    "PROJECT_CREATED", "PLAN_IMPORTED", "PLAN_UPDATED", "TASK_STARTED", "TASK_REVIEW", "TASK_COMPLETED", "TASK_BLOCKED",
    "DECISION_ADDED", "DECISION_SUPERSEDED", "INBOX_ITEM_ADDED", "INBOX_ITEM_RESOLVED", "RISK_ADDED", "RISK_RESOLVED",
    "TECH_DEBT_ADDED", "TECH_DEBT_RESOLVED", "ATTENTION_ADDED", "ATTENTION_RESOLVED", "CHECKPOINT_CREATED", "HANDOFF_CREATED",
    "EXECUTION_BOUND", "EXECUTION_STARTED", "EXECUTION_COMPLETED", "TASK_REVIEW", "TASK_REVIEW_ACCEPTED", "TASK_REVIEW_CHANGES_REQUESTED",
    "TASK_COMPLETED", "TASK_BLOCKED", "EXECUTION_STALE_RESULT", "EXECUTION_REPLAYED", "EXECUTION_CONVERSATION_BOUND", "EXECUTION_CHECKPOINT", "MILESTONE_COMPLETED", "MILESTONE_AUTO_STARTED", "MILESTONE_AUTO_STOPPED", "MILESTONE_AUTO_COMPLETED", "PROJECT_REVIEW_REQUESTED",
    "GATE_PASSED", "GATE_REOPENED", "OPEN_QUESTION_RESOLVED", "OPEN_QUESTION_REOPENED",
})
HANDOFF_MODES = (
    "CONTINUE_IMPLEMENTATION", "NEW_CHAT_PROJECT_HANDOFF", "ARCHITECTURE_REVIEW", "PROJECT_REVIEW", "DEBUGGING",
    "PLAN_REVIEW", "DISCUSS_IDEA", "SECOND_OPINION",
)


class ProjectMemoryError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> None:
    raise ProjectMemoryError(code, message)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _text(value: object, field: str, *, max_length: int = 8_000, required: bool = True) -> str:
    if not isinstance(value, str):
        _fail("memory_field_invalid", f"{field} must be text")
    result = value.strip()
    if required and not result:
        _fail("memory_field_invalid", f"{field} must not be empty")
    if len(result) > max_length:
        _fail("memory_field_too_large", f"{field} exceeds its bound")
    return result


def _project_id(project_id: str) -> str:
    value = _text(project_id, "project_id", max_length=96)
    if not _ID_RE.fullmatch(value):
        _fail("project_id_invalid", "project_id is unsafe")
    return value


def _version(value: object, field: str = "plan_version") -> str:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        _fail("plan_version_invalid", f"{field} must be an integer version")
    text = str(value).strip()
    if _PLAN_VERSION_RE.fullmatch(text) is None:
        _fail("plan_version_invalid", f"{field} must be an integer version")
    return str(int(text.split(".", 1)[0]))


def plan_version_number(value: object) -> int:
    text = _version(value)
    return int(_PLAN_VERSION_RE.fullmatch(text).group(1))  # type: ignore[union-attr]


def _atomic_write(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json_bytes(dict(document))
    if len(payload) > MAX_MEMORY_BYTES:
        _fail("memory_too_large", "project memory document exceeds its bound")
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


def memory_root(runtime_root: str | Path, project_id: str) -> Path:
    root = Path(runtime_root).expanduser().absolute()
    if root.exists() and root.is_symlink():
        _fail("memory_root_invalid", "project memory runtime root must not be a symlink")
    identifier = _project_id(project_id)
    return root / "control" / "project-memory" / identifier


def _plan_digest(plan: ProjectPlan) -> str:
    return semantic_digest(plan.to_dict())


@dataclass(frozen=True)
class ProjectEvent:
    event_id: str
    project_id: str
    event_type: str
    timestamp: str
    human_summary: str
    task_id: str | None = None
    milestone_id: str | None = None
    plan_version: str | None = None
    git_head: str | None = None
    correlation_id: str | None = None
    prerequisite_id: str | None = None
    prerequisite_kind: str | None = None
    schema: str = PROJECT_EVENT_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in {
            "schema": self.schema, "event_id": self.event_id, "project_id": self.project_id,
            "event_type": self.event_type, "timestamp": self.timestamp, "human_summary": self.human_summary,
            "task_id": self.task_id, "milestone_id": self.milestone_id, "plan_version": self.plan_version,
            "git_head": self.git_head, "correlation_id": self.correlation_id,
            "prerequisite_id": self.prerequisite_id, "prerequisite_kind": self.prerequisite_kind,
        }.items() if value is not None}


@dataclass(frozen=True)
class DecisionRecord:
    decision_id: str
    project_id: str
    title: str
    decision: str
    reason: str
    status: str
    created_at: str
    related_task_ids: tuple[str, ...] = ()
    related_plan_version: str | None = None
    supersedes_decision_id: str | None = None
    schema: str = PROJECT_DECISION_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "decision_id": self.decision_id, "project_id": self.project_id, "title": self.title, "decision": self.decision, "reason": self.reason, "status": self.status, "created_at": self.created_at, "related_task_ids": list(self.related_task_ids), "related_plan_version": self.related_plan_version, "supersedes_decision_id": self.supersedes_decision_id}


@dataclass(frozen=True)
class InboxItem:
    inbox_id: str
    project_id: str
    title: str
    description: str
    created_at: str
    status: str = "new"
    schema: str = PROJECT_INBOX_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "inbox_id": self.inbox_id, "project_id": self.project_id, "title": self.title, "description": self.description, "created_at": self.created_at, "status": self.status}


@dataclass(frozen=True)
class RiskRecord:
    risk_id: str
    project_id: str
    title: str
    description: str
    severity: str
    status: str
    created_at: str
    schema: str = PROJECT_RISK_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "risk_id": self.risk_id, "project_id": self.project_id, "title": self.title, "description": self.description, "severity": self.severity, "status": self.status, "created_at": self.created_at}


@dataclass(frozen=True)
class DebtRecord:
    debt_id: str
    project_id: str
    title: str
    description: str
    created_at: str
    status: str
    related_task_ids: tuple[str, ...] = ()
    suggested_review_milestone: str | None = None
    schema: str = PROJECT_DEBT_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "debt_id": self.debt_id, "project_id": self.project_id, "title": self.title, "description": self.description, "created_at": self.created_at, "status": self.status, "related_task_ids": list(self.related_task_ids), "suggested_review_milestone": self.suggested_review_milestone}


@dataclass(frozen=True)
class AttentionItem:
    attention_id: str
    project_id: str
    type: str
    title: str
    description: str
    created_at: str
    status: str = "open"
    schema: str = PROJECT_ATTENTION_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "attention_id": self.attention_id, "project_id": self.project_id, "type": self.type, "title": self.title, "description": self.description, "created_at": self.created_at, "status": self.status}


@dataclass(frozen=True)
class Checkpoint:
    checkpoint_id: str
    project_id: str
    created_at: str
    label: str
    plan_version: str | None
    git_head: str | None
    completed_task_ids: tuple[str, ...]
    current_task_id: str | None
    active_decision_ids: tuple[str, ...]
    open_blocker_ids: tuple[str, ...]
    human_summary: str | None = None
    schema: str = PROJECT_CHECKPOINT_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "checkpoint_id": self.checkpoint_id, "project_id": self.project_id, "created_at": self.created_at, "label": self.label, "plan_version": self.plan_version, "git_head": self.git_head, "completed_task_ids": list(self.completed_task_ids), "current_task_id": self.current_task_id, "active_decision_ids": list(self.active_decision_ids), "open_blocker_ids": list(self.open_blocker_ids), "human_summary": self.human_summary}


@dataclass(frozen=True)
class ProjectMemoryState:
    project_id: str
    events: tuple[ProjectEvent, ...] = ()
    decisions: tuple[DecisionRecord, ...] = ()
    inbox: tuple[InboxItem, ...] = ()
    risks: tuple[RiskRecord, ...] = ()
    technical_debt: tuple[DebtRecord, ...] = ()
    attention: tuple[AttentionItem, ...] = ()
    checkpoints: tuple[Checkpoint, ...] = ()
    # Execution is a bounded sub-document of Project Memory, not a second
    # project state.  The execution coordinator owns its shape and keeps
    # large receipts out of this document.
    execution: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"schema": PROJECT_MEMORY_SCHEMA, "project_id": self.project_id, "events": [item.to_dict() for item in self.events], "decisions": [item.to_dict() for item in self.decisions], "inbox": [item.to_dict() for item in self.inbox], "risks": [item.to_dict() for item in self.risks], "technical_debt": [item.to_dict() for item in self.technical_debt], "attention": [item.to_dict() for item in self.attention], "checkpoints": [item.to_dict() for item in self.checkpoints], "execution": dict(self.execution)}


@dataclass(frozen=True)
class PlanDiffItem:
    kind: str
    subject: str
    subject_id: str
    before: Mapping[str, Any] | None
    after: Mapping[str, Any] | None


@dataclass(frozen=True)
class PlanDiff:
    milestones: tuple[PlanDiffItem, ...]
    tasks: tuple[PlanDiffItem, ...]
    dependencies: tuple[PlanDiffItem, ...]
    acceptance_criteria: tuple[PlanDiffItem, ...]
    current_task: tuple[PlanDiffItem, ...]

    @property
    def all_items(self) -> tuple[PlanDiffItem, ...]:
        return self.milestones + self.tasks + self.dependencies + self.acceptance_criteria + self.current_task

    def summary_lines(self) -> tuple[str, ...]:
        labels = {"ADDED": "+", "REMOVED": "-", "MODIFIED": "~", "UNCHANGED": "="}
        return tuple(f"{labels[item.kind]} {item.subject} {item.subject_id}" for item in self.all_items if item.kind != "UNCHANGED")


@dataclass(frozen=True)
class PlanUpdatePreview:
    project_id: str
    accepted: bool
    reason_code: str | None
    current_version: str | None
    next_version: str
    current_plan_digest: str | None
    candidate_plan_digest: str
    diff: PlanDiff
    completed_protection: tuple[str, ...] = ()
    schema: str = PROJECT_PLAN_UPDATE_PREVIEW_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "project_id": self.project_id, "accepted": self.accepted, "reason_code": self.reason_code, "current_version": self.current_version, "next_version": self.next_version, "current_plan_digest": self.current_plan_digest, "candidate_plan_digest": self.candidate_plan_digest, "diff": [{"kind": item.kind, "subject": item.subject, "subject_id": item.subject_id, "before": item.before, "after": item.after} for item in self.diff.all_items], "completed_protection": list(self.completed_protection)}


@dataclass(frozen=True)
class NextAction:
    code: str
    title: str
    detail: str
    priority: int


def _item_map(items: Sequence[Mapping[str, Any]], key: str) -> dict[str, Mapping[str, Any]]:
    return {str(item[key]): item for item in items}


def _diff_by_id(old: Sequence[Mapping[str, Any]], new: Sequence[Mapping[str, Any]], *, key: str, subject: str) -> tuple[PlanDiffItem, ...]:
    def normalized(item: Mapping[str, Any]) -> Mapping[str, Any]:
        value = dict(item)
        for list_key in ("dependencies", "acceptance_criteria"):
            if isinstance(value.get(list_key), list):
                value[list_key] = sorted(value[list_key])
        return value
    before = {identifier: normalized(item) for identifier, item in _item_map(old, key).items()}; after = {identifier: normalized(item) for identifier, item in _item_map(new, key).items()}
    return tuple(PlanDiffItem("ADDED" if identifier not in before else "MODIFIED" if before[identifier] != after[identifier] else "UNCHANGED", subject, identifier, before.get(identifier), after.get(identifier)) for identifier in sorted(after)) + tuple(PlanDiffItem("REMOVED", subject, identifier, value, None) for identifier, value in sorted(before.items()) if identifier not in after)


def semantic_plan_diff(old: ProjectPlan, new: ProjectPlan) -> PlanDiff:
    old_doc, new_doc = old.to_dict(), new.to_dict()
    milestones = _diff_by_id(old_doc["milestones"], new_doc["milestones"], key="id", subject="milestone")
    tasks = _diff_by_id(old_doc["tasks"], new_doc["tasks"], key="id", subject="task")
    old_tasks = _item_map(old_doc["tasks"], "id"); new_tasks = _item_map(new_doc["tasks"], "id")
    dependency_items: list[PlanDiffItem] = []
    acceptance_items: list[PlanDiffItem] = []
    for identifier in sorted(set(old_tasks) | set(new_tasks)):
        if identifier not in old_tasks or identifier not in new_tasks:
            continue
        old_dependencies = sorted(old_tasks[identifier].get("dependencies", [])); new_dependencies = sorted(new_tasks[identifier].get("dependencies", []))
        old_acceptance = sorted(old_tasks[identifier].get("acceptance_criteria", [])); new_acceptance = sorted(new_tasks[identifier].get("acceptance_criteria", []))
        if old_dependencies != new_dependencies:
            dependency_items.append(PlanDiffItem("MODIFIED", "dependency", identifier, {"dependencies": old_dependencies}, {"dependencies": new_dependencies}))
        else:
            dependency_items.append(PlanDiffItem("UNCHANGED", "dependency", identifier, {"dependencies": old_dependencies}, {"dependencies": new_dependencies}))
        if old_acceptance != new_acceptance:
            acceptance_items.append(PlanDiffItem("MODIFIED", "acceptance_criteria", identifier, {"acceptance_criteria": old_acceptance}, {"acceptance_criteria": new_acceptance}))
        else:
            acceptance_items.append(PlanDiffItem("UNCHANGED", "acceptance_criteria", identifier, {"acceptance_criteria": old_acceptance}, {"acceptance_criteria": new_acceptance}))
    current = (PlanDiffItem("UNCHANGED" if old.current_task_id == new.current_task_id else "MODIFIED", "current_task", "current_task_id", {"current_task_id": old.current_task_id}, {"current_task_id": new.current_task_id}),)
    return PlanDiff(tuple(milestones), tuple(tasks), tuple(dependency_items), tuple(acceptance_items), current)


def _completed_protection(old: ProjectPlan, new: ProjectPlan, *, execution_completed: Iterable[str] = ()) -> tuple[str, ...]:
    completed_ids = set(execution_completed)
    old_tasks = {task.task_id: task for task in old.tasks if task.status == "completed" or task.task_id in completed_ids}
    new_tasks = {task.task_id: task for task in new.tasks}
    blocked: list[str] = []
    for identifier, task in old_tasks.items():
        candidate = new_tasks.get(identifier)
        if candidate is None:
            blocked.append(f"completed_task_removed:{identifier}")
            continue
        if candidate.status != "completed":
            blocked.append(f"completed_task_downgrade:{identifier}")
        if (candidate.milestone_id != task.milestone_id or candidate.title != task.title or candidate.description != task.description or sorted(candidate.acceptance_criteria) != sorted(task.acceptance_criteria) or sorted(candidate.dependencies) != sorted(task.dependencies)):
            blocked.append(f"completed_task_meaning_changed:{identifier}")
    return tuple(blocked)


def _plan_prerequisite_ids(plan: ProjectPlan) -> tuple[set[str], set[str]]:
    context = plan.planning_context or {}
    return ({item["id"] for item in context.get("gates", [])}, {item["id"] for item in context.get("open_questions", [])})


def _bounded_runtime_statuses(execution: Mapping[str, Any], key: str, identifiers: set[str], default: str, allowed: frozenset[str]) -> dict[str, str]:
    raw = execution.get(key, {})
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping) or len(raw) > MAX_ITEMS:
        _fail("memory_execution_shape_invalid", f"execution.{key} is invalid")
    return {identifier: str(raw.get(identifier, default)) if str(raw.get(identifier, default)) in allowed else default for identifier in sorted(identifiers)}


def _synchronize_prerequisite_statuses(plan: ProjectPlan, execution: Mapping[str, Any], *, previous_plan: ProjectPlan | None = None) -> dict[str, Any]:
    """Carry only same-kind prerequisite statuses into the current plan."""
    result = dict(execution)
    gate_ids, open_question_ids = _plan_prerequisite_ids(plan)
    old_gate_ids, old_open_question_ids = _plan_prerequisite_ids(previous_plan) if previous_plan is not None else (set(), set())
    raw_gates = execution.get("gate_statuses", {})
    raw_questions = execution.get("open_question_statuses", {})
    if not isinstance(raw_gates, Mapping) or not isinstance(raw_questions, Mapping):
        _fail("memory_execution_shape_invalid", "prerequisite status maps are invalid")
    result["gate_statuses"] = {
        identifier: str(raw_gates.get(identifier, "pending")) if identifier in old_gate_ids and str(raw_gates.get(identifier, "pending")) in GATE_STATUS_VALUES else "pending"
        for identifier in sorted(gate_ids)
    }
    result["open_question_statuses"] = {
        identifier: str(raw_questions.get(identifier, "open")) if identifier in old_open_question_ids and str(raw_questions.get(identifier, "open")) in OPEN_QUESTION_STATUS_VALUES else "open"
        for identifier in sorted(open_question_ids)
    }
    return result


def _runtime_prerequisite_statuses(plan: ProjectPlan, state: ProjectMemoryState) -> tuple[dict[str, str], dict[str, str]]:
    execution = state.execution if isinstance(state.execution, Mapping) else {}
    gate_ids, open_question_ids = _plan_prerequisite_ids(plan)
    return (
        _bounded_runtime_statuses(execution, "gate_statuses", gate_ids, "pending", GATE_STATUS_VALUES),
        _bounded_runtime_statuses(execution, "open_question_statuses", open_question_ids, "open", OPEN_QUESTION_STATUS_VALUES),
    )


def task_prerequisite_blockers(plan: ProjectPlan | None, state: ProjectMemoryState, task: ProjectTask) -> tuple[dict[str, str], ...]:
    """Return deterministic unsatisfied prerequisite records for one task."""
    if plan is None:
        return ()
    kinds = classify_dependency_targets(plan)
    task_statuses = _execution_task_statuses(state, plan)
    gate_statuses, open_question_statuses = _runtime_prerequisite_statuses(plan, state)
    blockers: list[dict[str, str]] = []
    for dependency in task.dependencies:
        kind = kinds.get(dependency)
        if kind == "task":
            status = task_statuses.get(dependency, "pending")
            satisfied = status in {"completed", "skipped"}
        elif kind == "gate":
            status = gate_statuses.get(dependency, "pending")
            satisfied = status == "passed"
        elif kind == "open_question":
            status = open_question_statuses.get(dependency, "open")
            satisfied = status == "resolved"
        else:
            status = "unknown"
            satisfied = False
        if not satisfied:
            blockers.append({"id": dependency, "kind": kind or "unknown", "status": status})
    return tuple(blockers)


class ProjectMemoryStore:
    """Canonical metadata writer for one project, with immutable plan files."""

    def __init__(self, runtime_root: str | Path, project_id: str) -> None:
        self.project_id = _project_id(project_id)
        self.root = memory_root(runtime_root, self.project_id)
        self.plans = self.root / "plans"
        self.memory_path = self.root / "memory.json"
        self.current_pointer = self.plans / "current-plan.json"
        if self.root.exists() and self.root.is_symlink():
            _fail("memory_path_invalid", "project memory root must be a regular directory")

    def read_state(self) -> ProjectMemoryState:
        if not self.memory_path.exists():
            return ProjectMemoryState(self.project_id)
        if self.memory_path.is_symlink() or not self.memory_path.is_file():
            _fail("memory_path_invalid", "project memory must be a regular file")
        payload = self.memory_path.read_bytes()
        if len(payload) > MAX_MEMORY_BYTES:
            _fail("memory_too_large", "project memory exceeds its bound")
        try:
            document = json.loads(payload.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProjectMemoryError("memory_corrupt", "project memory is not valid JSON") from exc
        if not isinstance(document, Mapping) or document.get("schema") != PROJECT_MEMORY_SCHEMA or document.get("project_id") != self.project_id:
            _fail("memory_schema_invalid", "project memory schema or project identity differs")
        return self._state_from_dict(document)

    def _state_from_dict(self, document: Mapping[str, Any]) -> ProjectMemoryState:
        # The persisted representation is intentionally strict enough to reject
        # accidental cross-project or unbounded metadata, while keeping the
        # read model small for the GUI.
        collections = {}
        for key in ("events", "decisions", "inbox", "risks", "technical_debt", "attention", "checkpoints"):
            value = document.get(key, [])
            if not isinstance(value, list) or len(value) > MAX_ITEMS * 4:
                _fail("memory_shape_invalid", f"{key} is outside its bound")
            if any(not isinstance(item, Mapping) for item in value):
                _fail("memory_shape_invalid", f"{key} contains a non-object item")
            collections[key] = tuple(dict(item) for item in value)
        events = tuple(ProjectEvent(**{key: item[key] for key in ("event_id", "project_id", "event_type", "timestamp", "human_summary")}, task_id=item.get("task_id"), milestone_id=item.get("milestone_id"), plan_version=item.get("plan_version"), git_head=item.get("git_head"), correlation_id=item.get("correlation_id"), prerequisite_id=item.get("prerequisite_id"), prerequisite_kind=item.get("prerequisite_kind")) for item in collections["events"])
        decisions = tuple(DecisionRecord(item["decision_id"], item["project_id"], item["title"], item["decision"], item["reason"], item["status"], item["created_at"], tuple(item.get("related_task_ids", [])), item.get("related_plan_version"), item.get("supersedes_decision_id")) for item in collections["decisions"])
        inbox = tuple(InboxItem(item["inbox_id"], item["project_id"], item["title"], item["description"], item["created_at"], item.get("status", "new")) for item in collections["inbox"])
        risks = tuple(RiskRecord(item["risk_id"], item["project_id"], item["title"], item["description"], item["severity"], item["status"], item["created_at"]) for item in collections["risks"])
        debt = tuple(DebtRecord(item["debt_id"], item["project_id"], item["title"], item["description"], item["created_at"], item["status"], tuple(item.get("related_task_ids", [])), item.get("suggested_review_milestone")) for item in collections["technical_debt"])
        attention = tuple(AttentionItem(item["attention_id"], item["project_id"], item["type"], item["title"], item["description"], item["created_at"], item.get("status", "open")) for item in collections["attention"])
        checkpoints = tuple(Checkpoint(item["checkpoint_id"], item["project_id"], item["created_at"], item["label"], item.get("plan_version"), item.get("git_head"), tuple(item.get("completed_task_ids", [])), item.get("current_task_id"), tuple(item.get("active_decision_ids", [])), tuple(item.get("open_blocker_ids", [])), item.get("human_summary")) for item in collections["checkpoints"])
        execution = document.get("execution", {})
        if not isinstance(execution, Mapping) or len(canonical_json_bytes(dict(execution))) > 512 * 1024:
            _fail("memory_execution_shape_invalid", "execution state is outside its bound")
        execution = dict(execution)
        if any(event.project_id != self.project_id for event in events) or any(event.event_id != f"{self.project_id}:e{index:06d}" for index, event in enumerate(events, 1)):
            _fail("memory_event_order_invalid", "project events must form one append-only sequence")
        return ProjectMemoryState(self.project_id, events, decisions, inbox, risks, debt, attention, checkpoints, execution)

    def _write_state(self, state: ProjectMemoryState) -> None:
        _atomic_write(self.memory_path, state.to_dict())

    @contextmanager
    def _execution_lock(self) -> Iterator[None]:
        """Bounded cross-process lock for execution transitions.

        A live lock is never overwritten.  A lock older than the bounded
        transition lease is treated as a crashed writer marker and reclaimed;
        the state document itself is never deleted or repaired implicitly.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        lock = self.root / "execution.lock"
        descriptor: int | None = None
        deadline = time.monotonic() + 3.0
        while descriptor is None:
            try:
                descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                try:
                    if time.time() - lock.stat().st_mtime > 120:
                        lock.unlink()
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() >= deadline:
                    _fail("memory_busy", "project execution memory is busy")
                time.sleep(0.01)
        try:
            yield
        finally:
            os.close(descriptor)
            lock.unlink(missing_ok=True)

    _T = TypeVar("_T")

    def execution_transaction(self, operation: Callable[[ProjectMemoryState], tuple[ProjectMemoryState, _T]]) -> _T:
        """Commit one bounded execution transition atomically with its events."""
        with self._execution_lock():
            current = self.read_state()
            updated, result = operation(current)
            if updated.project_id != self.project_id:
                _fail("memory_project_mismatch", "execution transition changed project identity")
            self._write_state(updated)
            return result

    def _append_event(self, state: ProjectMemoryState, event_type: str, summary: str, *, task_id: str | None = None, milestone_id: str | None = None, plan_version: str | None = None, git_head: str | None = None, correlation_id: str | None = None, prerequisite_id: str | None = None, prerequisite_kind: str | None = None) -> ProjectMemoryState:
        if event_type not in _EVENT_TYPES:
            _fail("event_type_invalid", "event_type is unsupported")
        summary = _text(summary, "human_summary", max_length=2_000)
        if len(state.events) >= MAX_EVENTS:
            _fail("event_log_bounded", "project event log reached its bound")
        event_id = f"{self.project_id}:e{len(state.events) + 1:06d}"
        if prerequisite_id is not None:
            prerequisite_id = _text(prerequisite_id, "prerequisite_id", max_length=96)
        if prerequisite_kind is not None and prerequisite_kind not in {"gate", "open_question"}:
            _fail("prerequisite_kind_invalid", "prerequisite kind is unsupported")
        event = ProjectEvent(event_id, self.project_id, event_type, _now(), summary, task_id, milestone_id, plan_version, git_head, correlation_id, prerequisite_id, prerequisite_kind)
        return replace(state, events=state.events + (event,))

    def append_event(self, event_type: str, human_summary: str, **bindings: str | None) -> ProjectEvent:
        state = self.read_state()
        updated = self._append_event(state, event_type, human_summary, **bindings)
        self._write_state(updated)
        return updated.events[-1]

    def add_decision(self, *, title: str, decision: str, reason: str, plan_version: str | None = None, related_task_ids: Iterable[str] = (), supersedes_decision_id: str | None = None) -> DecisionRecord:
        state = self.read_state(); identifier = f"D-{len(state.decisions) + 1:03d}"
        if len(state.decisions) >= MAX_ITEMS: _fail("memory_collection_bounded", "decision history reached its bound")
        if supersedes_decision_id and not any(item.decision_id == supersedes_decision_id for item in state.decisions):
            _fail("decision_supersedes_missing", "superseded decision does not exist")
        record = DecisionRecord(identifier, self.project_id, _text(title, "decision.title", max_length=300), _text(decision, "decision.decision", max_length=4_000), _text(reason, "decision.reason", max_length=4_000), "active", _now(), tuple(related_task_ids), plan_version, supersedes_decision_id)
        decisions = tuple(replace(item, status="superseded") if item.decision_id == supersedes_decision_id else item for item in state.decisions) + (record,)
        updated = replace(state, decisions=decisions); updated = self._append_event(updated, "DECISION_ADDED", f"Dodano decyzję: {record.title}", plan_version=plan_version)
        if supersedes_decision_id:
            updated = self._append_event(updated, "DECISION_SUPERSEDED", f"Decyzja {supersedes_decision_id} została zastąpiona przez {identifier}")
        self._write_state(updated); return record

    def add_inbox(self, *, title: str, description: str) -> InboxItem:
        state = self.read_state()
        if len(state.inbox) >= MAX_ITEMS: _fail("memory_collection_bounded", "inbox reached its bound")
        record = InboxItem(f"I-{len(state.inbox) + 1:03d}", self.project_id, _text(title, "inbox.title", max_length=300), _text(description, "inbox.description", max_length=4_000), _now())
        updated = replace(state, inbox=state.inbox + (record,)); updated = self._append_event(updated, "INBOX_ITEM_ADDED", f"Dodano pomysł: {record.title}"); self._write_state(updated); return record

    def update_inbox(self, inbox_id: str, status: str) -> InboxItem:
        state = self.read_state(); allowed = {"new", "discuss", "later", "accepted", "rejected", "resolved"}
        if status not in allowed: _fail("inbox_status_invalid", "inbox status is unsupported")
        found = next((item for item in state.inbox if item.inbox_id == inbox_id), None)
        if found is None: _fail("inbox_not_found", "inbox item does not exist")
        updated_item = replace(found, status=status); updated = replace(state, inbox=tuple(updated_item if item.inbox_id == inbox_id else item for item in state.inbox)); updated = self._append_event(updated, "INBOX_ITEM_RESOLVED" if status == "resolved" else "INBOX_ITEM_ADDED", f"Inbox {inbox_id}: {status}"); self._write_state(updated); return updated_item

    def add_risk(self, *, title: str, description: str, severity: str = "medium") -> RiskRecord:
        if severity not in {"low", "medium", "high"}: _fail("risk_severity_invalid", "risk severity is unsupported")
        state = self.read_state()
        if len(state.risks) >= MAX_ITEMS: _fail("memory_collection_bounded", "risk history reached its bound")
        record = RiskRecord(f"R-{len(state.risks) + 1:03d}", self.project_id, _text(title, "risk.title", max_length=300), _text(description, "risk.description", max_length=4_000), severity, "open", _now()); updated = replace(state, risks=state.risks + (record,)); updated = self._append_event(updated, "RISK_ADDED", f"Dodano ryzyko: {record.title}"); self._write_state(updated); return record

    def resolve_risk(self, risk_id: str, status: str = "resolved") -> RiskRecord:
        if status not in {"mitigated", "resolved", "accepted"}: _fail("risk_status_invalid", "risk status is unsupported")
        state = self.read_state(); found = next((item for item in state.risks if item.risk_id == risk_id), None)
        if found is None: _fail("risk_not_found", "risk does not exist")
        result = replace(found, status=status); updated = replace(state, risks=tuple(result if item.risk_id == risk_id else item for item in state.risks)); updated = self._append_event(updated, "RISK_RESOLVED", f"Ryzyko {risk_id}: {status}"); self._write_state(updated); return result

    def add_debt(self, *, title: str, description: str, related_task_ids: Iterable[str] = (), suggested_review_milestone: str | None = None) -> DebtRecord:
        state = self.read_state()
        if len(state.technical_debt) >= MAX_ITEMS: _fail("memory_collection_bounded", "technical debt history reached its bound")
        record = DebtRecord(f"TD-{len(state.technical_debt) + 1:03d}", self.project_id, _text(title, "debt.title", max_length=300), _text(description, "debt.description", max_length=4_000), _now(), "open", tuple(related_task_ids), suggested_review_milestone); updated = replace(state, technical_debt=state.technical_debt + (record,)); updated = self._append_event(updated, "TECH_DEBT_ADDED", f"Dodano dług techniczny: {record.title}"); self._write_state(updated); return record

    def resolve_debt(self, debt_id: str, status: str = "resolved") -> DebtRecord:
        if status not in {"planned", "resolved", "accepted"}: _fail("debt_status_invalid", "technical debt status is unsupported")
        state = self.read_state(); found = next((item for item in state.technical_debt if item.debt_id == debt_id), None)
        if found is None: _fail("debt_not_found", "technical debt does not exist")
        result = replace(found, status=status); updated = replace(state, technical_debt=tuple(result if item.debt_id == debt_id else item for item in state.technical_debt)); updated = self._append_event(updated, "TECH_DEBT_RESOLVED", f"Dług {debt_id}: {status}"); self._write_state(updated); return result

    def add_attention(self, *, type: str, title: str, description: str) -> AttentionItem:
        if type not in {"decision_required", "blocked", "review_required", "plan_review_required"}: _fail("attention_type_invalid", "attention type is unsupported")
        state = self.read_state()
        if len(state.attention) >= MAX_ITEMS: _fail("memory_collection_bounded", "attention history reached its bound")
        record = AttentionItem(f"A-{len(state.attention) + 1:03d}", self.project_id, type, _text(title, "attention.title", max_length=300), _text(description, "attention.description", max_length=4_000), _now()); updated = replace(state, attention=state.attention + (record,)); updated = self._append_event(updated, "ATTENTION_ADDED", f"Dodano uwagę: {record.title}"); self._write_state(updated); return record

    def resolve_attention(self, attention_id: str) -> AttentionItem:
        state = self.read_state(); found = next((item for item in state.attention if item.attention_id == attention_id), None)
        if found is None: _fail("attention_not_found", "attention item does not exist")
        result = replace(found, status="resolved"); updated = replace(state, attention=tuple(result if item.attention_id == attention_id else item for item in state.attention)); updated = self._append_event(updated, "ATTENTION_RESOLVED", f"Uwaga {attention_id} rozwiązana"); self._write_state(updated); return result

    def create_checkpoint(self, *, label: str, plan_version: str | None, git_head: str | None, completed_task_ids: Iterable[str], current_task_id: str | None, active_decision_ids: Iterable[str] = (), open_blocker_ids: Iterable[str] = (), human_summary: str | None = None) -> Checkpoint:
        state = self.read_state()
        if len(state.checkpoints) >= MAX_ITEMS: _fail("memory_collection_bounded", "checkpoint history reached its bound")
        record = Checkpoint(f"CP-{len(state.checkpoints) + 1:03d}", self.project_id, _now(), _text(label, "checkpoint.label", max_length=300), plan_version, git_head, tuple(completed_task_ids), current_task_id, tuple(active_decision_ids), tuple(open_blocker_ids), human_summary); updated = replace(state, checkpoints=state.checkpoints + (record,)); updated = self._append_event(updated, "CHECKPOINT_CREATED", f"Utworzono checkpoint: {record.label}", plan_version=plan_version, git_head=git_head); self._write_state(updated); return record

    def ensure_initial_plan(self, plan: ProjectPlan) -> ProjectPlan:
        if plan_version_number(plan.plan_version) != 1: _fail("plan_initial_version_invalid", "the first plan must be v1")
        if self.current_plan() is not None: _fail("plan_already_exists", "project already has a current plan")
        canonical = replace(plan, project_id=self.project_id, plan_version=_version(plan.plan_version), supersedes_version=None, created_at=plan.created_at or _now(), revision_reason=plan.revision_reason or "initial", revision_summary=plan.revision_summary or "Initial project plan")
        self._write_immutable_plan(canonical); self._activate_pointer(canonical)
        def transition(state: ProjectMemoryState) -> tuple[ProjectMemoryState, None]:
            execution = _synchronize_prerequisite_statuses(canonical, state.execution if isinstance(state.execution, Mapping) else {})
            updated = replace(state, execution=execution)
            updated = self._append_event(updated, "PLAN_IMPORTED", f"Zaimportowano plan v{canonical.plan_version}", plan_version=canonical.plan_version)
            return updated, None
        self.execution_transaction(transition)
        return canonical

    def current_plan(self) -> ProjectPlan | None:
        if not self.current_pointer.exists(): return None
        if self.current_pointer.is_symlink() or not self.current_pointer.is_file(): _fail("plan_pointer_invalid", "current plan pointer must be a regular file")
        pointer = json.loads(self.current_pointer.read_text(encoding="utf-8"))
        if not isinstance(pointer, Mapping) or pointer.get("schema") != PROJECT_PLAN_POINTER_SCHEMA or pointer.get("project_id") != self.project_id: _fail("plan_pointer_invalid", "current plan pointer schema differs")
        version = _version(pointer.get("plan_version")); path = self.plans / f"plan-v{version}.json"
        if not path.is_file() or path.is_symlink(): _fail("plan_missing", "current plan file is missing")
        document = json.loads(path.read_text(encoding="utf-8")); plan = validate_project_plan(document, expected_project_id=self.project_id)
        if _plan_digest(plan) != pointer.get("plan_digest"): _fail("plan_digest_mismatch", "current plan digest differs")
        return plan

    def plan_versions(self) -> tuple[ProjectPlan, ...]:
        if not self.plans.exists(): return ()
        result: list[ProjectPlan] = []
        for path in sorted(self.plans.glob("plan-v*.json")):
            if path.name == "current-plan.json" or path.is_symlink(): continue
            result.append(validate_project_plan(json.loads(path.read_text(encoding="utf-8")), expected_project_id=self.project_id))
        return tuple(sorted(result, key=lambda item: plan_version_number(item.plan_version)))

    def preview_update(self, candidate: ProjectPlan) -> PlanUpdatePreview:
        candidate = validate_project_plan(candidate.to_dict(), expected_project_id=self.project_id)
        current = self.current_plan()
        if current is None:
            diff = semantic_plan_diff(candidate, candidate)
            return PlanUpdatePreview(self.project_id, plan_version_number(candidate.plan_version) == 1, None if plan_version_number(candidate.plan_version) == 1 else "plan_initial_version_invalid", None, _version(candidate.plan_version), None, _plan_digest(candidate), diff)
        current_number = plan_version_number(current.plan_version); next_number = plan_version_number(candidate.plan_version)
        current_state = self.read_state()
        execution_statuses = current_state.execution.get("task_statuses", {}) if isinstance(current_state.execution, Mapping) else {}
        protection = _completed_protection(current, candidate, execution_completed=(task_id for task_id, status in execution_statuses.items() if status in {"completed", "skipped"}))
        reason: str | None = None
        if next_number != current_number + 1: reason = "plan_successor_required"
        elif candidate.supersedes_version is None or plan_version_number(candidate.supersedes_version) != current_number: reason = "plan_supersedes_mismatch"
        elif protection: reason = "completed_task_reconciliation_required"
        return PlanUpdatePreview(self.project_id, reason is None, reason, _version(current.plan_version), _version(candidate.plan_version), _plan_digest(current), _plan_digest(candidate), semantic_plan_diff(current, candidate), protection)

    def apply_update(self, candidate: ProjectPlan, preview: PlanUpdatePreview | None = None) -> ProjectPlan:
        candidate = validate_project_plan(candidate.to_dict(), expected_project_id=self.project_id)
        check = preview or self.preview_update(candidate)
        if check.project_id != self.project_id or check.candidate_plan_digest != _plan_digest(candidate):
            _fail("plan_preview_mismatch", "plan preview does not match the candidate bytes")
        if not check.accepted: _fail(check.reason_code or "plan_update_rejected", "plan update is not accepted")
        current = self.current_plan()
        if current is None: return self.ensure_initial_plan(candidate)
        if check.current_plan_digest != _plan_digest(current): _fail("plan_current_changed", "current plan changed after preview")
        canonical = replace(candidate, project_id=self.project_id, plan_version=_version(candidate.plan_version), supersedes_version=_version(current.plan_version), created_at=candidate.created_at or _now(), revision_reason=candidate.revision_reason or "plan update", revision_summary=candidate.revision_summary or "Updated project plan")
        self._write_immutable_plan(canonical); self._activate_pointer(canonical)
        def transition(state: ProjectMemoryState) -> tuple[ProjectMemoryState, None]:
            execution = _synchronize_prerequisite_statuses(canonical, state.execution if isinstance(state.execution, Mapping) else {}, previous_plan=current)
            updated = replace(state, execution=execution)
            updated = self._append_event(updated, "PLAN_UPDATED", f"Zaktualizowano plan v{current.plan_version} → v{canonical.plan_version}: {canonical.revision_reason}", plan_version=canonical.plan_version)
            return updated, None
        self.execution_transaction(transition)
        return canonical

    def _set_prerequisite_status(self, identifier: str, *, kind: str, status: str) -> str:
        plan = self.current_plan()
        if plan is None:
            _fail("project_plan_required", "prerequisite status requires an imported plan")
        identifier = _text(identifier, "prerequisite_id", max_length=96)
        status = _text(status, "prerequisite_status", max_length=32)
        gate_ids, open_question_ids = _plan_prerequisite_ids(plan)
        if kind == "gate":
            if identifier not in gate_ids:
                _fail("prerequisite_not_found", f"gate does not exist in the current plan: {identifier}")
            allowed, map_key, event_type = GATE_STATUS_VALUES, "gate_statuses", "GATE_PASSED" if status == "passed" else "GATE_REOPENED"
        elif kind == "open_question":
            if identifier not in open_question_ids:
                _fail("prerequisite_not_found", f"open question does not exist in the current plan: {identifier}")
            allowed, map_key, event_type = OPEN_QUESTION_STATUS_VALUES, "open_question_statuses", "OPEN_QUESTION_RESOLVED" if status == "resolved" else "OPEN_QUESTION_REOPENED"
        else:
            _fail("prerequisite_kind_invalid", "prerequisite kind is unsupported")
        if status not in allowed:
            _fail("prerequisite_status_invalid", "prerequisite status is unsupported")
        def transition(state: ProjectMemoryState) -> tuple[ProjectMemoryState, str]:
            execution = _synchronize_prerequisite_statuses(plan, state.execution if isinstance(state.execution, Mapping) else {}, previous_plan=plan)
            statuses = dict(execution[map_key])
            if statuses.get(identifier) == status:
                return state, status
            statuses[identifier] = status
            execution[map_key] = statuses
            updated = replace(state, execution=execution)
            updated = self._append_event(updated, event_type, f"{kind} {identifier}: {status}", plan_version=plan.plan_version, prerequisite_id=identifier, prerequisite_kind=kind)
            return updated, status
        return self.execution_transaction(transition)

    def set_gate_status(self, gate_id: str, status: str) -> str:
        return self._set_prerequisite_status(gate_id, kind="gate", status=status)

    def pass_gate(self, gate_id: str) -> str:
        return self.set_gate_status(gate_id, "passed")

    def reopen_gate(self, gate_id: str) -> str:
        return self.set_gate_status(gate_id, "pending")

    def mark_gate_passed(self, gate_id: str) -> str:
        return self.pass_gate(gate_id)

    def set_open_question_status(self, question_id: str, status: str) -> str:
        return self._set_prerequisite_status(question_id, kind="open_question", status=status)

    def resolve_open_question(self, question_id: str) -> str:
        return self.set_open_question_status(question_id, "resolved")

    def reopen_open_question(self, question_id: str) -> str:
        return self.set_open_question_status(question_id, "open")

    def _write_immutable_plan(self, plan: ProjectPlan) -> None:
        self.plans.mkdir(parents=True, exist_ok=True); path = self.plans / f"plan-v{_version(plan.plan_version)}.json"; document = plan.to_dict(); digest = _plan_digest(plan)
        if path.exists():
            existing = validate_project_plan(json.loads(path.read_text(encoding="utf-8")), expected_project_id=self.project_id)
            if _plan_digest(existing) != digest: _fail("plan_version_conflict", "immutable plan version already contains different bytes")
            return
        _atomic_write(path, document)

    def _activate_pointer(self, plan: ProjectPlan) -> None:
        _atomic_write(self.current_pointer, {"schema": PROJECT_PLAN_POINTER_SCHEMA, "project_id": self.project_id, "plan_version": _version(plan.plan_version), "plan_digest": _plan_digest(plan)})


def resolve_next_action(project: ProjectRecord, plan: ProjectPlan | None, state: ProjectMemoryState, *, plan_update_pending: bool = False) -> NextAction:
    if not project.plan_imported or plan is None: return NextAction("IMPORT_PLAN", "Wczytaj plan projektu", "Projekt nie ma aktywnego planu.", 1)
    if plan_update_pending: return NextAction("REVIEW_PLAN_UPDATE", "Przejrzyj aktualizację planu", "Nowa wersja planu czeka na zatwierdzenie.", 2)
    statuses = _execution_task_statuses(state, plan)
    if state.execution.get("stale_result"):
        return NextAction("RECONCILIATION_REQUIRED", "Wymaga reconciliacji", "Późny wynik wykonania nie pasuje do aktualnego planu lub repozytorium.", 3)
    review_task = next((task for task in plan.tasks if statuses.get(task.task_id, task.status) == "review"), None)
    if review_task is not None:
        return NextAction("REVIEW_REQUIRED", f"Przejrzyj: {review_task.task_id} — {review_task.title}", "Zadanie ma wynik wymagający ręcznej akceptacji.", 4)
    blocked_task = next((task for task in plan.tasks if statuses.get(task.task_id, task.status) == "blocked"), None)
    if blocked_task is not None:
        return NextAction("RESOLVE_BLOCKER", f"Rozwiąż blocker: {blocked_task.task_id}", "Ostatnia próba wykonania nie przeszła walidacji.", 5)
    open_attention = [item for item in state.attention if item.status == "open"]
    for item in open_attention:
        if item.type == "decision_required": return NextAction("USER_DECISION_REQUIRED", "Wymaga Twojej decyzji", item.title, 3)
    if any(item.status == "open" and item.type == "review_required" for item in open_attention): return NextAction("REVIEW_REQUIRED", "Wymaga przeglądu", "Otwarty element wymaga przeglądu.", 4)
    if any(item.status == "open" and item.type == "blocked" for item in open_attention): return NextAction("RESOLVE_BLOCKER", "Rozwiąż blocker", "Projekt jest zablokowany.", 5)
    current = next((task for task in plan.tasks if task.task_id == state.execution.get("current_task_id")), None) or plan.current_task
    if current is not None and statuses.get(current.task_id, current.status) == "review":
        return NextAction("REVIEW_REQUIRED", f"Przejrzyj: {current.task_id} — {current.title}", "Zadanie ma wynik wymagający ręcznej akceptacji.", 4)
    available = available_project_tasks(plan, state)
    if len(available) > 1:
        return NextAction("CHOOSE_TASK", f"{len(available)} zadań jest gotowych", "Wybierz zadanie; plan nie narzuca kolejności.", 6)
    if available:
        task = available[0]
        return NextAction("CONTINUE_TASK", f"Kontynuuj: {task.task_id} — {task.title}", task.description, 6)
    for candidate in plan.tasks:
        if statuses.get(candidate.task_id, candidate.status) in {"completed", "skipped"}:
            continue
        blockers = task_prerequisite_blockers(plan, state, candidate)
        if not blockers:
            continue
        first = blockers[0]
        rendered = "; ".join(f"{item['id']} ({item['kind']}, {item['status']})" for item in blockers)
        if first["kind"] == "gate":
            gate = next((item for item in (plan.planning_context or {}).get("gates", []) if item["id"] == first["id"]), {})
            label = f"{first['id']} — {gate.get('title', 'wymagany gate')}"
            return NextAction("GATE_REQUIRED", f"Zalicz gate: {label}", f"Zadanie {candidate.task_id} jest zablokowane przez: {rendered}.", 3)
        if first["kind"] == "open_question":
            question = next((item for item in (plan.planning_context or {}).get("open_questions", []) if item["id"] == first["id"]), {})
            label = f"{first['id']} — {question.get('question', 'otwarte pytanie')}"
            return NextAction("OPEN_QUESTION_REQUIRED", f"Rozstrzygnij pytanie: {label}", f"Zadanie {candidate.task_id} jest zablokowane przez: {rendered}.", 3)
        return NextAction("PREREQUISITE_REQUIRED", f"Uzupełnij prerequisites dla {candidate.task_id}", f"Zablokowane: {rendered}.", 3)
    if plan.tasks and all(statuses.get(task.task_id, task.status) in {"completed", "skipped"} for task in plan.tasks):
        return NextAction("PROJECT_REVIEW", "Przejrzyj projekt", "Wszystkie zadania planu są zakończone.", 7)
    return NextAction("CONTINUE_TASK", "Kontynuuj projekt", "Wybierz następne zadanie z planu.", 6)


def project_health(state: ProjectMemoryState, plan: ProjectPlan | None) -> str:
    if plan is None or any(item.status == "open" and item.type == "blocked" for item in state.attention): return "BLOCKED"
    execution = state.execution
    if execution.get("stale_result") or any(item.get("status") == "STALE_RESULT" for item in execution.get("attempts", []) if isinstance(item, Mapping)): return "BLOCKED"
    if any(status == "blocked" for status in _execution_task_statuses(state, plan).values()): return "BLOCKED"
    statuses = _execution_task_statuses(state, plan)
    if not available_project_tasks(plan, state) and any(task_prerequisite_blockers(plan, state, task) for task in plan.tasks if statuses.get(task.task_id, task.status) not in {"completed", "skipped"}): return "BLOCKED"
    if any(item.status == "open" and item.type in {"decision_required", "review_required", "plan_review_required"} for item in state.attention) or any(item.status == "open" and item.severity == "high" for item in state.risks): return "ATTENTION"
    return "OK"


def project_status_sentence(project: ProjectRecord, plan: ProjectPlan | None, state: ProjectMemoryState) -> str:
    if plan is None: return f"Projekt {project.display_name} nie ma jeszcze zaimportowanego planu."
    statuses = _execution_task_statuses(state, plan)
    current = str(state.execution.get("current_task_id") or plan.current_task_id or "brak bieżącego zadania"); current_task = next((item for item in plan.tasks if item.task_id == current), None); milestone = current_task.milestone_id if current_task else (plan.current_milestone.milestone_id if plan.current_milestone else "brak etapu"); blocker_count = sum(item.status == "open" and item.type == "blocked" for item in state.attention) + sum(status == "blocked" for status in statuses.values())
    suffix = f"; praca czeka na {blocker_count} blocker" if blocker_count else "; brak blockerów"
    completed = sum(status in {"completed", "skipped"} for status in statuses.values())
    return f"Projekt jest na {milestone}/{current}, wykonano {completed}/{len(plan.tasks)} zadań, plan v{plan.plan_version}{suffix}."


def _execution_task_statuses(state: ProjectMemoryState, plan: ProjectPlan | None) -> dict[str, str]:
    if plan is None:
        return {}
    raw = state.execution.get("task_statuses", {}) if isinstance(state.execution, Mapping) else {}
    if not isinstance(raw, Mapping):
        return {task.task_id: task.status for task in plan.tasks}
    return {task.task_id: str(raw.get(task.task_id, task.status)) for task in plan.tasks}


def available_project_tasks(plan: ProjectPlan | None, state: ProjectMemoryState, milestone_id: str | None = None) -> tuple[ProjectTask, ...]:
    if plan is None:
        return ()
    statuses = _execution_task_statuses(state, plan)
    result = []
    for task in plan.tasks:
        if milestone_id is not None and task.milestone_id != milestone_id:
            continue
        if statuses.get(task.task_id, task.status) not in {"pending", "active"}:
            continue
        if not task_prerequisite_blockers(plan, state, task):
            result.append(task)
    return tuple(result)


def milestone_auto_progress(plan: ProjectPlan | None, state: ProjectMemoryState, milestone_id: str | None = None) -> dict[str, object]:
    """Return the canonical, deterministic one-at-a-time AUTO milestone cursor.

    This is read-only. Project Memory/Execution owns task status and
    prerequisites; Browser AUTO may transport the returned cursor but may not
    choose a task or advance a status by itself.
    """
    if plan is None:
        return {"schema": "bdb-milestone-progress-v1", "status": "PROJECT_PLAN_REQUIRED", "milestone_id": None, "runnable_task_ids": []}
    statuses = _execution_task_statuses(state, plan)
    execution = state.execution if isinstance(state.execution, Mapping) else {}
    active = execution.get("active_milestone_run") if isinstance(execution.get("active_milestone_run"), Mapping) else {}
    selected = milestone_id or active.get("milestone_id")
    if selected is None:
        current_id = execution.get("current_task_id")
        current = next((item for item in plan.tasks if item.task_id == current_id), None)
        selected = current.milestone_id if current is not None else (plan.current_milestone.milestone_id if plan.current_milestone else None)
    milestone = next((item for item in plan.milestones if item.milestone_id == selected), None)
    if milestone is None:
        return {"schema": "bdb-milestone-progress-v1", "status": "MILESTONE_REQUIRED", "milestone_id": selected, "runnable_task_ids": []}
    tasks = tuple(item for item in plan.tasks if item.milestone_id == milestone.milestone_id)
    completed = tuple(item for item in tasks if statuses.get(item.task_id, item.status) in {"completed", "skipped"})
    incomplete = tuple(item for item in tasks if item not in completed)
    runnable = available_project_tasks(plan, state, milestone.milestone_id)
    base = {
        "schema": "bdb-milestone-progress-v1",
        "milestone_id": milestone.milestone_id,
        "completed_tasks": len(completed),
        "total_tasks": len(tasks),
        "completed_task_ids": [item.task_id for item in completed],
        "runnable_task_ids": [item.task_id for item in runnable],
    }
    if not incomplete:
        return {**base, "status": "MILESTONE_COMPLETED", "next_task_id": None, "blocker": None}
    review = next((item for item in incomplete if statuses.get(item.task_id, item.status) == "review"), None)
    if review is not None:
        return {**base, "status": "REVIEW_REQUIRED", "next_task_id": review.task_id, "blocker": {"id": review.task_id, "kind": "review", "status": "review"}}
    if runnable:
        return {**base, "status": "RUNNABLE", "next_task_id": runnable[0].task_id, "blocker": None}
    blocked = next((item for item in incomplete if statuses.get(item.task_id, item.status) == "blocked"), None)
    if blocked is not None:
        return {**base, "status": "BLOCKED", "next_task_id": blocked.task_id, "blocker": {"id": blocked.task_id, "kind": "task", "status": "blocked"}}
    blockers: list[dict[str, str]] = []
    for task in incomplete:
        blockers.extend(dict(item) for item in task_prerequisite_blockers(plan, state, task))
    first = blockers[0] if blockers else {"id": milestone.milestone_id, "kind": "prerequisite", "status": "blocked"}
    code = {"gate": "GATE_REQUIRED", "open_question": "OPEN_QUESTION_REQUIRED", "task": "PREREQUISITE_REQUIRED"}.get(first.get("kind"), "PREREQUISITE_REQUIRED")
    return {**base, "status": code, "next_task_id": None, "blocker": first}


def resolve_auto_next_action(plan: ProjectPlan | None, state: ProjectMemoryState, milestone_id: str | None = None) -> NextAction:
    progress = milestone_auto_progress(plan, state, milestone_id)
    status = progress.get("status")
    if status == "RUNNABLE":
        task_id = progress.get("next_task_id")
        task = next((item for item in (plan.tasks if plan else ()) if item.task_id == task_id), None)
        return NextAction("CONTINUE_TASK", f"Kontynuuj AUTO: {task_id}", task.description if task else "Uruchom następne zadanie milestone'u.", 6)
    if status == "MILESTONE_COMPLETED":
        return NextAction("MILESTONE_COMPLETED", f"Ukończono milestone: {progress.get('milestone_id')}", "Dalszy milestone wymaga jawnego uruchomienia przez użytkownika.", 7)
    if status in {"GATE_REQUIRED", "OPEN_QUESTION_REQUIRED", "PREREQUISITE_REQUIRED", "REVIEW_REQUIRED", "BLOCKED"}:
        blocker = progress.get("blocker") or {}
        detail = "Wymagane działanie użytkownika przed dalszym AUTO." if status in {"REVIEW_REQUIRED", "BLOCKED"} else "Brak runnable tasku w bieżącym milestone; wymagane działanie użytkownika."
        return NextAction(str(status), f"{status}: {blocker.get('id', 'brak')}", detail, 3)
    return NextAction(str(status or "MILESTONE_REQUIRED"), "Milestone AUTO niedostępny", "Nie można ustalić canonical milestone'u.", 3)


def changes_since(state: ProjectMemoryState, *, after_event_id: str | None = None, limit: int = 20) -> tuple[ProjectEvent, ...]:
    events = state.events
    if after_event_id:
        positions = [index for index, event in enumerate(events) if event.event_id == after_event_id]
        if positions: events = events[positions[-1] + 1 :]
    return events[-max(1, min(limit, 100)) :]


def bounded_history_summary(project: ProjectRecord, plan: ProjectPlan | None, state: ProjectMemoryState, *, after_event_id: str | None = None, limit: int = 12) -> str:
    lines = [f"Projekt: {project.display_name}", f"Project ID: {project.project_id}", f"Plan: v{plan.plan_version if plan else 'none'}", project_status_sentence(project, plan, state), "", "Ostatnie zmiany:"]
    lines.extend(f"- {event.event_type}: {event.human_summary}" for event in changes_since(state, after_event_id=after_event_id, limit=limit))
    if state.decisions: lines.extend(["", "Aktywne decyzje:", *[f"- {item.title}: {item.decision}" for item in state.decisions[-5:] if item.status == "active"]])
    if state.risks: lines.extend(["", "Otwarte ryzyka:", *[f"- {item.title} ({item.severity})" for item in state.risks[-5:] if item.status == "open"]])
    return "\n".join(lines)[:20_000]


def build_handoff_prompt(project: ProjectRecord, plan: ProjectPlan | None, state: ProjectMemoryState, *, mode: str, git_head: str | None = None) -> str:
    if mode not in HANDOFF_MODES: _fail("handoff_mode_invalid", "handoff mode is unsupported")
    cursor = state.execution.get("last_handoff_event_id") if isinstance(state.execution, Mapping) else None
    summary = bounded_history_summary(project, plan, state, after_event_id=cursor)
    if cursor:
        summary = "Od ostatniego handoffu (event cursor " + str(cursor) + "):\n" + summary
    instruction = {
        "CONTINUE_IMPLEMENTATION": "Kontynuuj aktualne zadanie po pobraniu bounded contextu przez BDB.",
        "NEW_CHAT_PROJECT_HANDOFF": "To jest nowa rozmowa projektu; najpierw potwierdź canonical stan i nie powtarzaj ukończonych prac.",
        "ARCHITECTURE_REVIEW": "Nie implementuj; wykonaj niezależny przegląd architektury i decyzji.",
        "PROJECT_REVIEW": "Oceń postęp, zgodność kodu z planem, ryzyka i dług techniczny; nie zmieniaj planu automatycznie.",
        "DEBUGGING": "Skup się na diagnostyce aktualnego blokera, bez rozszerzania zakresu.",
        "PLAN_REVIEW": "Przeanalizuj potrzebę aktualizacji planu; ewentualny wynik ma być vN+1 z supersedes_version=vN.",
        "DISCUSS_IDEA": "Omów pomysł bez automatycznego dodawania go do planu.",
        "SECOND_OPINION": "Nie implementuj; wystąp jako niezależny reviewer i wskaż ryzyka tylko na podstawie danych.",
    }[mode]
    pinned = ", ".join(project.brief.pinned_files) if project.brief.pinned_files else "none"
    environment = ", ".join(project.brief.environment_hints) if project.brief.environment_hints else "none"
    task = None
    if plan is not None:
        current_task_id = state.execution.get("current_task_id") if isinstance(state.execution, Mapping) else None
        task = next((item for item in plan.tasks if item.task_id == current_task_id), None) if current_task_id else None
        if task is None:
            available = available_project_tasks(plan, state)
            if len(available) == 1:
                task = available[0]
            elif not available and (not isinstance(state.execution, Mapping) or state.execution.get("task_statuses", {}).get(plan.current_task_id, plan.current_task.status if plan.current_task else "pending") not in {"completed", "skipped"}):
                task = plan.current_task
    task_lines = ""
    if task:
        task_lines = "\n".join((f"Current task goal: {task.description}", f"Task dependencies: {', '.join(task.dependencies) or 'none'}", "Acceptance criteria:", *[f"- {criterion}" for criterion in task.acceptance_criteria]))
    active_decisions = "\n".join(f"- {item.title}: {item.decision}" for item in state.decisions if item.status == "active") or "none"
    attention = "\n".join(f"- {item.type}: {item.title} — {item.description}" for item in state.attention if item.status == "open") or "none"
    binding = state.execution.get("current_binding_id", "none") if isinstance(state.execution, Mapping) else "none"
    return (f"Tryb handoffu: {mode}\nProject ID: {project.project_id}\nRepo alias: {project.repo_alias}\nGitHub repo: {project.github_repo or 'not configured'}\nPlan version: {plan.plan_version if plan else 'none'}\nCurrent Git HEAD: {git_head or 'unknown'}\nExecution binding: {binding}\nWażne pliki (repo-relative): {pinned}\nŚrodowisko (wskazówki): {environment}\n\n{summary}\n\n{task_lines}\n\nAktywne decyzje:\n{active_decisions}\n\nOtwarte uwagi/blockery:\n{attention}\n\nInstrukcja: najpierw potwierdź aktualne zadanie; nie powtarzaj ukończonych prac; używaj BDB do bounded repo contextu; zachowaj correlation.\n{instruction}\nNie wysyłaj nic automatycznie i nie kopiuj całego repozytorium, diffów ani sekretów.")[:30_000]


__all__ = [
    "HANDOFF_MODES", "GATE_STATUS_VALUES", "OPEN_QUESTION_STATUS_VALUES", "AttentionItem", "Checkpoint", "DebtRecord", "DecisionRecord", "InboxItem", "NextAction", "PlanDiff", "PlanDiffItem", "PlanUpdatePreview", "ProjectEvent", "ProjectMemoryError", "ProjectMemoryState", "ProjectMemoryStore", "RiskRecord", "available_project_tasks", "milestone_auto_progress", "resolve_auto_next_action", "bounded_history_summary", "build_handoff_prompt", "changes_since", "memory_root", "plan_version_number", "project_health", "project_status_sentence", "resolve_next_action", "semantic_plan_diff", "task_prerequisite_blockers",
]

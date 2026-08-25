"""Canonical project execution binding and bounded result projection.

This module deliberately does not execute commands.  The existing BDB command,
Work Kernel, Candidate, Evidence and promotion authorities remain responsible
for execution.  It binds their machine-readable result to one Project Memory
task and applies one idempotent, stale-safe project transition.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from bdb_shared.evidence import semantic_digest

from .binding_lifecycle import (
    BINDING_STATUS_VALUES,
    BindingLifecycleError,
    STATUS_ACCEPTED,
    STATUS_ACTIVE,
    STATUS_FAILED,
    STATUS_SUPERSEDED,
    check_binding_lifecycle_invariants,
    reconcile_execution_bindings,
    validate_binding_transition,
)
from .project_catalog import ProjectCatalog, ProjectPlan, ProjectRecord, ProjectTask
from .project_memory import ProjectMemoryState, ProjectMemoryStore, available_project_tasks, milestone_auto_progress, task_prerequisite_blockers


PROJECT_EXECUTION_SCHEMA = "bdb-project-execution-v1"
PROJECT_EXECUTION_SUBMISSION_SCHEMA = "bdb-project-execution-submission-v1"
PROJECT_EXECUTION_CHECKPOINT_SCHEMA = "bdb-project-execution-checkpoint-v1"
PROJECT_LAUNCH_HANDOFF_SCHEMA = "bdb-project-launch-handoff-v1"
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_HEAD_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_CONVERSATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_REPO_ALIAS_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


class ProjectExecutionError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def _fail(code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
    raise ProjectExecutionError(code, message, details=details)


def _text(value: object, field: str, *, max_length: int = 512, required: bool = True) -> str:
    if not isinstance(value, str):
        _fail("execution_field_invalid", f"{field} must be text")
    value = value.strip()
    if required and not value:
        _fail("execution_field_invalid", f"{field} must not be empty")
    if len(value) > max_length:
        _fail("execution_field_too_large", f"{field} exceeds its bound")
    return value


def _identifier(value: object, field: str) -> str:
    value = _text(value, field, max_length=128)
    if _ID_RE.fullmatch(value) is None:
        _fail("execution_identity_invalid", f"{field} has an unsafe format")
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_checkpoint_time(value: object) -> datetime:
    text = _text(value, "last_progress_at", max_length=64)
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError as exc:
        _fail("checkpoint_time_invalid", "last_progress_at must be an ISO-8601 timestamp")
    if parsed.tzinfo is None:
        _fail("checkpoint_time_invalid", "last_progress_at must include timezone")
    return parsed.astimezone(timezone.utc)


def _status(value: object, field: str) -> str:
    value = _text(value, field, max_length=32).upper()
    return value


def _head(value: object, field: str, *, allow_unknown: bool = False) -> str | None:
    if value is None and not allow_unknown:
        _fail("execution_field_invalid", f"{field} is required")
    if value is None:
        return None
    text = _text(value, field, max_length=128)
    if allow_unknown and text == "unknown":
        return text
    if _HEAD_RE.fullmatch(text.lower()) is None:
        _fail("repo_head_invalid", f"{field} is not a Git object identity")
    return text.lower()


def _conversation(value: object, field: str = "conversation_id") -> str:
    text = _text(value, field, max_length=128)
    if _CONVERSATION_RE.fullmatch(text) is None:
        _fail("execution_conversation_invalid", f"{field} has an unsafe format")
    return text


@dataclass(frozen=True)
class ProjectExecutionSubmission:
    """Strict machine result emitted by Work for one canonical launch binding."""

    project_id: str
    plan_version: str
    task_id: str
    execution_binding_id: str
    correlation_id: str
    command_id: str
    repo_alias: str
    head_before: str
    head_after: str | None
    execution_status: str
    validation_status: str
    promotion_status: str
    result_summary: str
    evidence_refs: tuple[str, ...] = ()
    criteria: tuple[Mapping[str, Any], ...] = ()
    canonical_refs: Mapping[str, Any] | None = None
    failure_code: str | None = None
    schema: str = PROJECT_EXECUTION_SUBMISSION_SCHEMA

    @classmethod
    def from_mapping(cls, value: object) -> "ProjectExecutionSubmission":
        if not isinstance(value, Mapping) or value.get("schema") != PROJECT_EXECUTION_SUBMISSION_SCHEMA:
            _fail("execution_schema_invalid", "project execution result schema differs")
        required = {
            "schema", "project_id", "plan_version", "task_id", "execution_binding_id",
            "correlation_id", "command_id", "repo_alias", "head_before", "head_after",
            "execution_status", "validation_status", "promotion_status", "result_summary",
            "evidence_refs", "criteria",
        }
        missing = sorted(required - set(value))
        if missing:
            _fail("execution_field_required", f"project execution result is missing: {', '.join(missing)}")
        allowed = {
            "schema", "project_id", "plan_version", "task_id", "execution_binding_id",
            "correlation_id", "command_id", "repo_alias", "head_before", "head_after",
            "execution_status", "validation_status", "promotion_status", "result_summary",
            "evidence_refs", "criteria", "canonical_refs", "failure_code",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            _fail("execution_field_unknown", f"project execution result contains unsupported fields: {', '.join(unknown)}")
        refs = value.get("evidence_refs", [])
        if not isinstance(refs, list) or len(refs) > 128:
            _fail("execution_shape_invalid", "evidence_refs must be a bounded list")
        criteria = value.get("criteria", [])
        if not isinstance(criteria, list) or len(criteria) > 128 or any(not isinstance(item, Mapping) for item in criteria):
            _fail("execution_shape_invalid", "criteria must be a bounded list of objects")
        normalized_criteria: list[Mapping[str, Any]] = []
        for item in criteria:
            extra = sorted(set(item) - {"criterion", "type", "status", "evidence_ref"})
            if extra:
                _fail("execution_field_unknown", f"criteria contains unsupported fields: {', '.join(extra)}")
            if "criterion" not in item:
                _fail("execution_field_invalid", "criteria[].criterion is required")
            normalized: dict[str, Any] = {
                "criterion": _text(item.get("criterion"), "criteria[].criterion", max_length=2_000),
            }
            for field in ("type", "status"):
                if field in item:
                    normalized[field] = _text(item.get(field), f"criteria[].{field}", max_length=32)
            if "evidence_ref" in item:
                evidence_ref = item.get("evidence_ref")
                normalized["evidence_ref"] = None if evidence_ref is None else _text(evidence_ref, "criteria[].evidence_ref", max_length=512)
            normalized_criteria.append(normalized)
        raw_refs = value.get("canonical_refs")
        canonical_refs: dict[str, Any] | None = None
        if raw_refs is not None:
            if not isinstance(raw_refs, Mapping):
                _fail("execution_shape_invalid", "canonical_refs must be an object")
            allowed_refs = {"task_id", "work_id", "candidate_id", "candidate_view_id", "candidate_tree_digest", "base_commit_oid", "validation_id", "evidence_id", "evaluation_id", "publication_id"}
            extra_refs = sorted(set(raw_refs) - allowed_refs)
            if extra_refs:
                _fail("execution_field_unknown", f"canonical_refs contains unsupported fields: {', '.join(extra_refs)}")
            canonical_refs = {}
            for key, raw_ref in raw_refs.items():
                canonical_refs[key] = None if raw_ref is None else _text(raw_ref, f"canonical_refs.{key}", max_length=128)
        failure = value.get("failure_code")
        if failure is not None:
            failure = _text(failure, "failure_code", max_length=128)
        repo_alias = _text(value.get("repo_alias"), "repo_alias", max_length=64)
        if _REPO_ALIAS_RE.fullmatch(repo_alias) is None:
            _fail("repo_alias_invalid", "repo_alias has an unsafe format")
        return cls(
            project_id=_identifier(value.get("project_id"), "project_id"),
            plan_version=_text(value.get("plan_version"), "plan_version", max_length=32),
            task_id=_identifier(value.get("task_id"), "task_id"),
            execution_binding_id=_identifier(value.get("execution_binding_id"), "execution_binding_id"),
            correlation_id=_identifier(value.get("correlation_id"), "correlation_id"),
            command_id=_identifier(value.get("command_id"), "command_id"),
            repo_alias=repo_alias,
            head_before=_head(value.get("head_before"), "head_before", allow_unknown=True) or "unknown",
            head_after=_head(value.get("head_after"), "head_after", allow_unknown=True),
            execution_status=_status(value.get("execution_status"), "execution_status"),
            validation_status=_status(value.get("validation_status"), "validation_status"),
            promotion_status=_status(value.get("promotion_status"), "promotion_status"),
            result_summary=_text(value.get("result_summary", ""), "result_summary", max_length=4_000, required=False),
            evidence_refs=tuple(_text(item, "evidence_refs[]", max_length=512) for item in refs),
            criteria=tuple(normalized_criteria),
            canonical_refs=canonical_refs,
            failure_code=failure,
        )

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema": self.schema,
            "project_id": self.project_id,
            "plan_version": self.plan_version,
            "task_id": self.task_id,
            "execution_binding_id": self.execution_binding_id,
            "correlation_id": self.correlation_id,
            "command_id": self.command_id,
            "repo_alias": self.repo_alias,
            "head_before": self.head_before,
            "head_after": self.head_after,
            "execution_status": self.execution_status,
            "validation_status": self.validation_status,
            "promotion_status": self.promotion_status,
            "result_summary": self.result_summary,
            "evidence_refs": list(self.evidence_refs),
            "criteria": [dict(item) for item in self.criteria],
        }
        if self.canonical_refs is not None:
            value["canonical_refs"] = dict(self.canonical_refs)
        if self.failure_code is not None:
            value["failure_code"] = self.failure_code
        return value


def _result_identity(binding: "ProjectExecutionBinding", result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "execution_binding_id": binding.execution_binding_id,
        "command_id": binding.command_id,
        "correlation_id": binding.correlation_id,
        "project_id": binding.project_id,
        "task_id": binding.task_id,
        "plan_version": binding.plan_version,
        "repo_alias": binding.repo_alias,
        "result_project_id": result.get("project_id"),
        "result_task_id": result.get("task_id"),
        "result_plan_version": result.get("plan_version"),
        "head_before": result.get("head_before"),
        "head_after": result.get("head_after"),
        "execution_status": result.get("execution_status"),
        "validation_status": result.get("validation_status"),
        "promotion_status": result.get("promotion_status"),
        "summary": result.get("result_summary", ""),
        "evidence_refs": list(result.get("evidence_refs", [])),
        "criteria": result.get("criteria", []),
        "canonical_refs": result.get("canonical_refs", {}),
    }


def execution_result_digest(binding: "ProjectExecutionBinding", result: Mapping[str, Any]) -> str:
    return semantic_digest(_result_identity(binding, result))


def _execution_document(state: ProjectMemoryState) -> dict[str, Any]:
    raw = state.execution if isinstance(state.execution, Mapping) else {}
    if not raw:
        return {"schema": PROJECT_EXECUTION_SCHEMA, "bindings": [], "attempts": [], "acceptance_results": [], "checkpoints": {}, "task_statuses": {}, "gate_statuses": {}, "open_question_statuses": {}, "milestones_completed": [], "milestone_runs": {}, "launch_handoffs": {}}
    if raw.get("schema", PROJECT_EXECUTION_SCHEMA) != PROJECT_EXECUTION_SCHEMA:
        _fail("execution_schema_invalid", "project execution state schema differs")
    result = dict(raw)
    result.setdefault("bindings", [])
    result.setdefault("attempts", [])
    result.setdefault("acceptance_results", [])
    result.setdefault("checkpoints", {})
    result.setdefault("task_statuses", {})
    result.setdefault("gate_statuses", {})
    result.setdefault("open_question_statuses", {})
    result.setdefault("milestones_completed", [])
    result.setdefault("milestone_runs", {})
    result.setdefault("launch_handoffs", {})
    for key in ("bindings", "attempts", "acceptance_results"):
        if not isinstance(result[key], list) or len(result[key]) > 512 or any(not isinstance(item, Mapping) for item in result[key]):
            _fail("execution_shape_invalid", f"execution.{key} is invalid")
    if not isinstance(result["task_statuses"], Mapping) or len(result["task_statuses"]) > 2_048:
        _fail("execution_shape_invalid", "execution.task_statuses is invalid")
    if not isinstance(result["milestone_runs"], Mapping) or len(result["milestone_runs"]) > 128:
        _fail("execution_shape_invalid", "execution.milestone_runs is invalid")
    if not isinstance(result["checkpoints"], Mapping) or len(result["checkpoints"]) > 512:
        _fail("execution_shape_invalid", "execution.checkpoints is invalid")
    for key in ("gate_statuses", "open_question_statuses", "launch_handoffs"):
        if not isinstance(result[key], Mapping) or len(result[key]) > 512:
            _fail("execution_shape_invalid", f"execution.{key} is invalid")
    return result


@dataclass(frozen=True)
class ProjectExecutionBinding:
    execution_binding_id: str
    project_id: str
    execution_binding_id: str
    project_id: str
    plan_version: str
    task_id: str
    launch_id: str
    correlation_id: str
    command_id: str
    repo_alias: str
    expected_repo_head_before: str
    created_at: str
    status: str = "ACTIVE"
    superseded: bool = False
    generation: int = 1
    conversation_id: str | None = None
    finished_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema": PROJECT_EXECUTION_SCHEMA,
            "execution_binding_id": self.execution_binding_id,
            "project_id": self.project_id,
            "plan_version": self.plan_version,
            "task_id": self.task_id,
            "launch_id": self.launch_id,
            "correlation_id": self.correlation_id,
            "command_id": self.command_id,
            "repo_alias": self.repo_alias,
            "expected_repo_head_before": self.expected_repo_head_before,
            "created_at": self.created_at,
            "status": self.status,
            "superseded": self.superseded,
            "generation": self.generation,
        }
        if self.conversation_id is not None:
            value["conversation_id"] = self.conversation_id
        if self.finished_at is not None:
            value["finished_at"] = self.finished_at
        return value


@dataclass(frozen=True)
class TaskAcceptanceResult:
    project_id: str
    plan_version: str
    task_id: str
    attempt_id: str
    criteria: tuple[Mapping[str, Any], ...]
    overall: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {"schema": "bdb-task-acceptance-result-v1", "project_id": self.project_id, "plan_version": self.plan_version, "task_id": self.task_id, "attempt_id": self.attempt_id, "criteria": [dict(item) for item in self.criteria], "overall": self.overall, "created_at": self.created_at}


@dataclass(frozen=True)
class ProjectExecutionAttempt:
    attempt_id: str
    project_id: str
    plan_version: str
    task_id: str
    execution_binding_id: str
    command_id: str
    started_at: str
    finished_at: str | None
    head_before: str
    head_after: str | None
    execution_status: str
    validation_status: str
    promotion_status: str
    result_status: str
    result_summary: str
    evidence_refs: tuple[str, ...]
    failure_code: str | None
    result_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {"schema": "bdb-project-execution-attempt-v1", "attempt_id": self.attempt_id, "project_id": self.project_id, "plan_version": self.plan_version, "task_id": self.task_id, "execution_binding_id": self.execution_binding_id, "command_id": self.command_id, "started_at": self.started_at, "finished_at": self.finished_at, "head_before": self.head_before, "head_after": self.head_after, "execution_status": self.execution_status, "validation_status": self.validation_status, "promotion_status": self.promotion_status, "result_status": self.result_status, "result_summary": self.result_summary, "evidence_refs": list(self.evidence_refs), "failure_code": self.failure_code, "result_digest": self.result_digest}


def _binding_from_dict(value: Mapping[str, Any]) -> ProjectExecutionBinding:
    conversation_id = value.get("conversation_id")
    raw_gen = value.get("generation", 1)
    try:
        generation = int(raw_gen)
    except (ValueError, TypeError):
        generation = 1
    return ProjectExecutionBinding(
        _identifier(value.get("execution_binding_id"), "execution_binding_id"),
        _identifier(value.get("project_id"), "project_id"),
        _text(value.get("plan_version"), "plan_version", max_length=32),
        _identifier(value.get("task_id"), "task_id"),
        _identifier(value.get("launch_id"), "launch_id"),
        _identifier(value.get("correlation_id"), "correlation_id"),
        _identifier(value.get("command_id"), "command_id"),
        _text(value.get("repo_alias"), "repo_alias", max_length=64),
        _text(value.get("expected_repo_head_before"), "expected_repo_head_before", max_length=128),
        _text(value.get("created_at"), "created_at", max_length=64),
        _status(value.get("status", STATUS_ACTIVE), "status"),
        bool(value.get("superseded", False)),
        generation=max(1, generation),
        conversation_id=_conversation(conversation_id) if conversation_id is not None else None,
        finished_at=_text(value.get("finished_at"), "finished_at", max_length=64, required=False) if value.get("finished_at") else None,
    )


def _attempt_from_dict(value: Mapping[str, Any]) -> ProjectExecutionAttempt:
    return ProjectExecutionAttempt(
        _identifier(value.get("attempt_id"), "attempt_id"),
        _identifier(value.get("project_id"), "project_id"),
        _text(value.get("plan_version"), "plan_version", max_length=32),
        _identifier(value.get("task_id"), "task_id"),
        _identifier(value.get("execution_binding_id"), "execution_binding_id"),
        _identifier(value.get("command_id"), "command_id"),
        _text(value.get("started_at"), "started_at", max_length=64),
        value.get("finished_at"),
        _text(value.get("head_before"), "head_before", max_length=128),
        value.get("head_after"),
        _status(value.get("execution_status"), "execution_status"),
        _status(value.get("validation_status"), "validation_status"),
        _status(value.get("promotion_status"), "promotion_status"),
        _status(value.get("result_status"), "result_status"),
        _text(value.get("result_summary", ""), "result_summary", max_length=4_000, required=False),
        tuple(_text(item, "evidence_ref", max_length=512) for item in value.get("evidence_refs", [])),
        value.get("failure_code"),
        _text(value.get("result_digest"), "result_digest", max_length=128),
    )


class ProjectExecutionCoordinator:
    """The sole project execution binding writer."""

    def __init__(self, runtime_root: str, *, catalog: ProjectCatalog | None = None, memory_factory: Any = ProjectMemoryStore) -> None:
        self.runtime_root = runtime_root
        self.catalog = catalog or ProjectCatalog(runtime_root)
        self._memory_factory = memory_factory

    def _project(self, project_id: str) -> tuple[ProjectRecord, ProjectPlan, ProjectMemoryStore]:
        project = self.catalog.get(project_id)
        if project is None:
            _fail("project_not_found", "project is not in the canonical catalog")
        memory = self._memory_factory(self.runtime_root, project_id)
        plan = memory.current_plan()
        if plan is None:
            _fail("project_plan_required", "project execution requires an imported plan")
        return project, plan, memory

    def new_binding(self, project_id: str, *, task_id: str | None = None, expected_repo_head_before: str = "unknown", launch_id: str | None = None, correlation_id: str | None = None, command_id: str | None = None) -> ProjectExecutionBinding:
        project, plan, memory = self._project(project_id)
        state = memory.read_state()
        execution = _execution_document(state)
        selected = task_id or execution.get("current_task_id") or plan.current_task_id
        if selected is None:
            available = available_project_tasks(plan, state)
            if len(available) == 1:
                selected = available[0].task_id
            elif len(available) > 1:
                _fail(
                    "task_selection_required",
                    "multiple execution tasks are runnable; choose one explicitly",
                    details={"task_ids": [item.task_id for item in available]},
                )
        task = next((item for item in plan.tasks if item.task_id == selected), None)
        if task is None:
            _fail("task_not_found", "execution task does not exist")
        statuses = execution.get("task_statuses", {})
        if statuses.get(task.task_id, task.status) in {"completed", "skipped"}:
            _fail("task_already_complete", "completed task cannot start a new binding")
        blockers = task_prerequisite_blockers(plan, state, task)
        if blockers:
            _fail(
                "execution_prerequisites_blocked",
                "task prerequisites are not satisfied",
                details={"task_id": task.task_id, "blocking_dependencies": [dict(item) for item in blockers]},
            )

        task_bindings = [
            b for b in execution.get("bindings", [])
            if b.get("task_id") == task.task_id and str(b.get("plan_version")) == str(plan.plan_version)
        ]
        active_for_task = [b for b in task_bindings if b.get("status") == STATUS_ACTIVE and not b.get("superseded")]
        current_binding_id = execution.get("current_binding_id")
        if current_binding_id and active_for_task:
            current = next((b for b in active_for_task if b.get("execution_binding_id") == current_binding_id), None)
            if current is not None:
                return _binding_from_dict(current)

        max_existing_gen = max((int(b.get("generation", 1)) for b in task_bindings), default=0)
        next_gen = max_existing_gen + 1

        head = _text(expected_repo_head_before, "expected_repo_head_before", max_length=128)
        if head != "unknown" and _HEAD_RE.fullmatch(head) is None:
            _fail("repo_head_invalid", "expected_repo_head_before is not a Git object identity")
        suffix = uuid.uuid4().hex
        return ProjectExecutionBinding(
            f"binding-{suffix}",
            project.project_id,
            plan.plan_version,
            task.task_id,
            launch_id or str(uuid.uuid4()),
            correlation_id or f"corr-{suffix}",
            command_id or f"command-{suffix}",
            project.repo_alias,
            head,
            _utc_now(),
            status=STATUS_ACTIVE,
            superseded=False,
            generation=next_gen,
        )

    def persist_binding(self, binding: ProjectExecutionBinding) -> ProjectExecutionBinding:
        _identifier(binding.project_id, "project_id")
        project, plan, memory = self._project(binding.project_id)
        if binding.plan_version != plan.plan_version or binding.repo_alias != project.repo_alias:
            _fail("execution_binding_stale", "binding no longer matches current project authority")

        def transition(state: ProjectMemoryState) -> tuple[ProjectMemoryState, ProjectExecutionBinding]:
            execution = _execution_document(state)
            task = next((item for item in plan.tasks if item.task_id == binding.task_id), None)
            if task is None:
                _fail("task_not_found", "execution task does not exist")
            existing = next((item for item in execution["bindings"] if item.get("execution_binding_id") == binding.execution_binding_id), None)
            if existing is not None:
                if semantic_digest(existing) != semantic_digest(binding.to_dict()):
                    _fail("execution_binding_conflict", "binding identity already contains different bytes")
                return state, _binding_from_dict(existing)
            blockers = task_prerequisite_blockers(plan, state, task)
            if blockers:
                _fail(
                    "execution_prerequisites_blocked",
                    "task prerequisites are not satisfied",
                    details={"task_id": task.task_id, "blocking_dependencies": [dict(item) for item in blockers]},
                )
            # Monotonic generation enforcement and atomic supersede of existing active bindings for this task/run
            task_bindings = [
                b for b in execution["bindings"]
                if b.get("task_id") == binding.task_id and str(b.get("plan_version")) == str(binding.plan_version)
            ]
            max_existing_gen = max((int(b.get("generation", 1)) for b in task_bindings), default=0)
            effective_gen = max(binding.generation, max_existing_gen + 1)
            now_iso = _utc_now()

            for b in execution["bindings"]:
                if b.get("task_id") == binding.task_id and str(b.get("plan_version")) == str(binding.plan_version):
                    if b.get("status") == STATUS_ACTIVE and not b.get("superseded"):
                        validate_binding_transition(STATUS_ACTIVE, STATUS_SUPERSEDED)
                        b["status"] = STATUS_SUPERSEDED
                        b["superseded"] = True
                        if not b.get("finished_at"):
                            b["finished_at"] = now_iso

            persisted_binding = replace(binding, generation=effective_gen, status=STATUS_ACTIVE, superseded=False)
            execution["bindings"].append(persisted_binding.to_dict())
            statuses = dict(execution.get("task_statuses", {}))
            statuses.setdefault(binding.task_id, "active")
            execution["task_statuses"] = statuses
            execution["current_task_id"] = binding.task_id
            execution["current_binding_id"] = binding.execution_binding_id

            updated = replace(state, execution=execution)
            updated = memory._append_event(
                updated,
                "EXECUTION_BOUND",
                f"Powiązano wykonanie z zadaniem {binding.task_id} (gen={effective_gen})",
                task_id=binding.task_id,
                plan_version=binding.plan_version,
                correlation_id=binding.correlation_id,
            )
            updated = memory._append_event(
                updated,
                "EXECUTION_STARTED",
                f"Rozpoczęto próbę zadania {binding.task_id} (gen={effective_gen})",
                task_id=binding.task_id,
                plan_version=binding.plan_version,
                correlation_id=binding.correlation_id,
            )
            return updated, persisted_binding
        return memory.execution_transaction(transition)

    def start(self, project_id: str, **kwargs: Any) -> ProjectExecutionBinding:
        return self.persist_binding(self.new_binding(project_id, **kwargs))

    def binding(self, project_id: str, execution_binding_id: str) -> ProjectExecutionBinding:
        """Read one canonical binding without creating or selecting another task."""
        _identifier(project_id, "project_id")
        binding_id = _identifier(execution_binding_id, "execution_binding_id")
        _project, _plan, memory = self._project(project_id)
        execution = _execution_document(memory.read_state())
        raw = next((item for item in execution["bindings"] if item.get("execution_binding_id") == binding_id), None)
        if raw is None:
            _fail("execution_binding_not_found", "execution binding does not exist")
        binding = _binding_from_dict(raw)
        if binding.project_id != project_id:
            _fail("execution_binding_stale", "execution binding belongs to another project")
        return binding

    def bind_conversation(self, project_id: str, execution_binding_id: str, conversation_id: str) -> ProjectExecutionBinding:
        """Bind a claimed Browser conversation exactly once to the launch binding."""
        conversation = _conversation(conversation_id)
        binding_id = _identifier(execution_binding_id, "execution_binding_id")
        project, plan, memory = self._project(project_id)

        def transition(state: ProjectMemoryState) -> tuple[ProjectMemoryState, ProjectExecutionBinding]:
            execution = _execution_document(state)
            raw = next((item for item in execution["bindings"] if item.get("execution_binding_id") == binding_id), None)
            if raw is None:
                _fail("execution_binding_not_found", "execution binding does not exist")
            current = _binding_from_dict(raw)
            if current.project_id != project.project_id or current.plan_version != plan.plan_version or current.superseded or current.status != "ACTIVE":
                _fail("execution_binding_stale", "execution binding is not active")
            if current.conversation_id not in (None, conversation):
                _fail("execution_conversation_mismatch", "execution binding is owned by another conversation")
            if current.conversation_id == conversation:
                return state, current
            updated_binding = replace(current, conversation_id=conversation)
            bindings = [updated_binding.to_dict() if item.get("execution_binding_id") == binding_id else item for item in execution["bindings"]]
            execution["bindings"] = bindings
            updated = replace(state, execution=execution)
            updated = memory._append_event(updated, "EXECUTION_CONVERSATION_BOUND", f"Powiązano rozmowę z zadaniem {current.task_id}", task_id=current.task_id, plan_version=current.plan_version, correlation_id=current.correlation_id)
            return updated, updated_binding

        return memory.execution_transaction(transition)

    def current_task_binding(self, project_id: str, task_id: str) -> ProjectExecutionBinding | None:
        """Return the one active binding for a task, or fail closed if ambiguous."""
        task = _identifier(task_id, "task_id")
        _project, plan, memory = self._project(project_id)
        execution = _execution_document(memory.read_state())
        matches = [
            _binding_from_dict(item)
            for item in execution.get("bindings", [])
            if item.get("project_id") == project_id
            and item.get("plan_version") == plan.plan_version
            and item.get("task_id") == task
            and item.get("status", "ACTIVE") == "ACTIVE"
            and item.get("superseded") is not True
        ]
        if len(matches) > 1:
            _fail("execution_binding_ambiguous", "more than one active binding matches the current task", details={"project_id": project_id, "task_id": task})
        return matches[0] if matches else None

    def restore_current_binding(self, project_id: str, execution_binding_id: str) -> ProjectExecutionBinding:
        """Re-establish the canonical cursor for one already-recorded active binding."""
        binding_id = _identifier(execution_binding_id, "execution_binding_id")
        project, plan, memory = self._project(project_id)

        def transition(state: ProjectMemoryState) -> tuple[ProjectMemoryState, ProjectExecutionBinding]:
            execution = _execution_document(state)
            raw = next((item for item in execution.get("bindings", []) if item.get("execution_binding_id") == binding_id), None)
            if raw is None:
                _fail("execution_binding_not_found", "execution binding does not exist")
            binding = _binding_from_dict(raw)
            if binding.project_id != project_id or binding.plan_version != plan.plan_version or binding.status != "ACTIVE" or binding.superseded:
                _fail("execution_binding_stale", "execution binding is not active")
            if execution.get("current_binding_id") not in (None, binding_id):
                _fail("execution_binding_stale", "another binding is the current canonical binding")
            task = next((item for item in plan.tasks if item.task_id == binding.task_id), None)
            if task is None:
                _fail("task_not_found", "execution task does not exist")
            if execution.get("task_statuses", {}).get(binding.task_id, task.status) in {"completed", "skipped"}:
                _fail("task_already_complete", "completed task cannot become the current binding")
            execution["current_task_id"] = binding.task_id
            execution["current_binding_id"] = binding_id
            return replace(state, execution=execution), binding

        return memory.execution_transaction(transition)

    @staticmethod
    def _launch_handoff_from_dict(value: Mapping[str, Any]) -> dict[str, Any]:
        if value.get("schema") != PROJECT_LAUNCH_HANDOFF_SCHEMA:
            _fail("launch_handoff_schema_invalid", "launch handoff schema differs")
        status = _text(value.get("status"), "launch_handoff.status", max_length=16)
        if status not in {"PENDING", "SENT"}:
            _fail("launch_handoff_status_invalid", "launch handoff status is unsupported")
        normalized = {
            "schema": PROJECT_LAUNCH_HANDOFF_SCHEMA,
            "project_id": _identifier(value.get("project_id"), "launch_handoff.project_id"),
            "execution_binding_id": _identifier(value.get("execution_binding_id"), "launch_handoff.execution_binding_id"),
            "task_id": _identifier(value.get("task_id"), "launch_handoff.task_id"),
            "launch_id": _identifier(value.get("launch_id"), "launch_handoff.launch_id"),
            "status": status,
            "updated_at": _text(value.get("updated_at"), "launch_handoff.updated_at", max_length=64),
        }
        conversation = value.get("conversation_id")
        if conversation is not None:
            normalized["conversation_id"] = _conversation(conversation, "launch_handoff.conversation_id")
        return normalized

    def launch_handoff(self, project_id: str, execution_binding_id: str) -> dict[str, Any] | None:
        binding_id = _identifier(execution_binding_id, "execution_binding_id")
        _project, _plan, memory = self._project(project_id)
        execution = _execution_document(memory.read_state())
        raw = execution.get("launch_handoffs", {}).get(binding_id)
        if raw is None:
            return None
        handoff = self._launch_handoff_from_dict(raw)
        if handoff["project_id"] != project_id or handoff["execution_binding_id"] != binding_id:
            _fail("launch_handoff_stale", "launch handoff is bound to another project or binding")
        return handoff

    def mark_launch_handoff_pending(self, project_id: str, binding: ProjectExecutionBinding) -> dict[str, Any]:
        """Durably record that an AUTO launch exists and still needs Send."""
        _project, plan, memory = self._project(project_id)
        if binding.project_id != project_id or binding.plan_version != plan.plan_version:
            _fail("launch_handoff_stale", "launch handoff binding is not current project authority")
        now = _utc_now()

        def transition(state: ProjectMemoryState) -> tuple[ProjectMemoryState, dict[str, Any]]:
            execution = _execution_document(state)
            handoffs = dict(execution.get("launch_handoffs", {}))
            existing = handoffs.get(binding.execution_binding_id)
            if existing is not None:
                current = self._launch_handoff_from_dict(existing)
                if any(current.get(key) != value for key, value in (("project_id", project_id), ("execution_binding_id", binding.execution_binding_id), ("task_id", binding.task_id), ("launch_id", binding.launch_id))):
                    _fail("launch_handoff_conflict", "launch handoff identity already contains different bytes")
                return state, current
            handoff = {
                "schema": PROJECT_LAUNCH_HANDOFF_SCHEMA,
                "project_id": project_id,
                "execution_binding_id": binding.execution_binding_id,
                "task_id": binding.task_id,
                "launch_id": binding.launch_id,
                "status": "PENDING",
                "updated_at": now,
            }
            handoffs[binding.execution_binding_id] = handoff
            execution["launch_handoffs"] = handoffs
            return replace(state, execution=execution), handoff

        return memory.execution_transaction(transition)

    def mark_launch_handoff_sent(self, project_id: str, *, execution_binding_id: str, launch_id: str, conversation_id: str) -> dict[str, Any]:
        """Commit the post-Send handoff exactly once; never advances task state."""
        binding_id = _identifier(execution_binding_id, "execution_binding_id")
        conversation = _conversation(conversation_id)
        _project, plan, memory = self._project(project_id)
        now = _utc_now()

        def transition(state: ProjectMemoryState) -> tuple[ProjectMemoryState, dict[str, Any]]:
            execution = _execution_document(state)
            handoffs = dict(execution.get("launch_handoffs", {}))
            existing = handoffs.get(binding_id)
            if existing is not None:
                current = self._launch_handoff_from_dict(existing)
                if current["project_id"] != project_id or current["execution_binding_id"] != binding_id or current["launch_id"] != launch_id or current.get("conversation_id") not in (None, conversation):
                    _fail("launch_handoff_conflict", "launch handoff identity does not match the canonical request")
                if current["status"] == "SENT":
                    return state, current
            raw = next((item for item in execution.get("bindings", []) if item.get("execution_binding_id") == binding_id), None)
            if raw is None:
                _fail("execution_binding_not_found", "launch handoff binding does not exist")
            binding = _binding_from_dict(raw)
            if binding.project_id != project_id or binding.plan_version != plan.plan_version or binding.launch_id != launch_id or binding.status != "ACTIVE" or binding.superseded:
                _fail("execution_binding_stale", "launch handoff binding is not active")
            if binding.conversation_id not in (None, conversation):
                _fail("execution_conversation_mismatch", "launch handoff conversation differs")
            if execution.get("current_binding_id") != binding_id:
                _fail("execution_binding_stale", "launch handoff binding is not the current canonical binding")
            handoff = {
                "schema": PROJECT_LAUNCH_HANDOFF_SCHEMA,
                "project_id": project_id,
                "execution_binding_id": binding_id,
                "task_id": binding.task_id,
                "launch_id": launch_id,
                "status": "SENT",
                "conversation_id": conversation,
                "updated_at": now,
            }
            handoffs[binding_id] = handoff
            execution["launch_handoffs"] = handoffs
            return replace(state, execution=execution), handoff

        return memory.execution_transaction(transition)

    def record_checkpoint(self, project_id: str, execution_binding_id: str, *, status: str, progress_summary: str = "", external_reference: str | None = None, last_progress_at: str | None = None) -> dict[str, Any]:
        binding = self.binding(project_id, execution_binding_id)
        normalized_status = _status(status, "checkpoint_status")
        if normalized_status not in {"ACTIVE", "WAITING_EXTERNAL", "RESUMABLE"}:
            _fail("checkpoint_status_invalid", "checkpoint status is unsupported")
        if external_reference is not None:
            external_reference = _text(external_reference, "external_reference", max_length=512)
        checkpoint = {
            "schema": PROJECT_EXECUTION_CHECKPOINT_SCHEMA,
            "execution_binding_id": binding.execution_binding_id,
            "project_id": binding.project_id,
            "task_id": binding.task_id,
            "plan_version": binding.plan_version,
            "status": normalized_status,
            "progress_summary": _text(progress_summary, "progress_summary", max_length=4_000, required=False),
            "external_reference": external_reference,
            "last_progress_at": _text(last_progress_at or _utc_now(), "last_progress_at", max_length=64),
        }
        _parse_checkpoint_time(checkpoint["last_progress_at"])
        _project, plan, memory = self._project(project_id)

        def transition(state: ProjectMemoryState) -> tuple[ProjectMemoryState, dict[str, Any]]:
            execution = _execution_document(state)
            current = _binding_from_dict(next(item for item in execution["bindings"] if item.get("execution_binding_id") == binding.execution_binding_id))
            if current.status != "ACTIVE" or current.superseded:
                _fail("execution_binding_stale", "checkpoint binding is not active")
            checkpoints = dict(execution.get("checkpoints", {})); checkpoints[binding.execution_binding_id] = checkpoint; execution["checkpoints"] = checkpoints
            updated = replace(state, execution=execution)
            updated = memory._append_event(updated, "EXECUTION_CHECKPOINT", f"Checkpoint {normalized_status} dla {binding.task_id}", task_id=binding.task_id, plan_version=plan.plan_version, correlation_id=binding.correlation_id)
            return updated, checkpoint

        return memory.execution_transaction(transition)

    def watchdog(self, project_id: str, *, now: datetime | None = None, inactivity_seconds: float = 300.0) -> dict[str, Any]:
        """Return an inactivity projection; it never marks a task failed/completed."""
        _project, _plan, memory = self._project(project_id)
        state = memory.read_state(); execution = _execution_document(state)
        binding_id = execution.get("current_binding_id")
        if not binding_id:
            return {"state": "IDLE", "resume_available": False, "execution_binding_id": None}
        binding = self.binding(project_id, str(binding_id))
        checkpoint = execution.get("checkpoints", {}).get(binding.execution_binding_id, {})
        checkpoint_status = str(checkpoint.get("status") or "ACTIVE")
        last_text = checkpoint.get("last_progress_at") or binding.created_at
        last_at = _parse_checkpoint_time(last_text)
        observed = now or datetime.now(timezone.utc)
        age = max(0.0, (observed.astimezone(timezone.utc) - last_at).total_seconds())
        if checkpoint_status == "WAITING_EXTERNAL":
            state_name = "WAITING_EXTERNAL"
        elif age >= inactivity_seconds:
            state_name = "STALLED"
        else:
            state_name = "ACTIVE"
        return {
            "state": state_name,
            "resume_available": state_name == "STALLED" or checkpoint_status == "RESUMABLE",
            "execution_binding_id": binding.execution_binding_id,
            "task_id": binding.task_id,
            "last_progress_at": last_text,
            "inactivity_seconds": age,
            "external_reference": checkpoint.get("external_reference"),
            "progress_summary": checkpoint.get("progress_summary", ""),
        }

    def resume_binding(self, project_id: str, execution_binding_id: str) -> ProjectExecutionBinding:
        binding = self.binding(project_id, execution_binding_id)
        if binding.status != "ACTIVE" or binding.superseded:
            _fail("execution_binding_stale", "same-binding resume is no longer safe")
        return binding

    def existing_result(self, project_id: str, result: Mapping[str, Any]) -> ProjectExecutionAttempt | None:
        """Find an exact replay before mutation; used only to label receipts."""
        binding = self.binding(project_id, _identifier(result.get("execution_binding_id"), "execution_binding_id"))
        digest = execution_result_digest(binding, result)
        execution = _execution_document(self._project(project_id)[2].read_state())
        existing = next((item for item in execution["attempts"] if item.get("execution_binding_id") == binding.execution_binding_id and item.get("result_digest") == digest), None)
        return _attempt_from_dict(existing) if existing is not None else None

    def begin_milestone_auto(self, project_id: str, *, milestone_id: str | None = None, milestone_run_id: str | None = None) -> dict[str, Any]:
        """Start or resume one canonical milestone run without executing work."""
        project, plan, memory = self._project(project_id)
        initial_state = memory.read_state()
        progress = milestone_auto_progress(plan, initial_state, milestone_id)
        if progress.get("status") not in {"RUNNABLE", "MILESTONE_COMPLETED"}:
            _fail(str(progress.get("status") or "MILESTONE_REQUIRED"), "milestone AUTO cannot start", details=progress)
        selected = str(progress.get("milestone_id"))
        initial_execution = _execution_document(initial_state)
        initial_active = initial_execution.get("active_milestone_run") if isinstance(initial_execution.get("active_milestone_run"), Mapping) else None
        inherited_run_id = initial_active.get("milestone_run_id") if initial_active and initial_active.get("milestone_id") == selected else None
        run_id = _identifier(milestone_run_id or inherited_run_id or f"milestone-run-{uuid.uuid4().hex}", "milestone_run_id")
        now = _utc_now()

        def transition(state: ProjectMemoryState) -> tuple[ProjectMemoryState, None]:
            execution = _execution_document(state)
            runs = dict(execution.get("milestone_runs", {}))
            active = execution.get("active_milestone_run") if isinstance(execution.get("active_milestone_run"), Mapping) else None
            if active and active.get("status") in {"running", "review", "blocked"} and active.get("milestone_id") != selected:
                _fail("milestone_run_active", "another milestone AUTO run is already active")
            existing = runs.get(run_id)
            if existing is not None and (existing.get("milestone_id") != selected or existing.get("project_id") != project_id):
                _fail("milestone_run_conflict", "milestone run identity is already bound to another milestone")
            run = {
                **(dict(existing) if isinstance(existing, Mapping) else {}),
                "schema": "bdb-milestone-run-v1",
                "milestone_run_id": run_id,
                "project_id": project_id,
                "plan_version": plan.plan_version,
                "milestone_id": selected,
                "status": "completed" if progress.get("status") == "MILESTONE_COMPLETED" else "running",
                "started_at": (existing or {}).get("started_at", now),
                "updated_at": now,
                "current_task_id": progress.get("next_task_id"),
            }
            runs[run_id] = run
            execution["milestone_runs"] = runs
            execution["active_milestone_run"] = run
            execution["current_task_id"] = progress.get("next_task_id")
            if existing is None:
                execution["current_binding_id"] = None
            updated = replace(state, execution=execution)
            if existing is None:
                updated = memory._append_event(updated, "MILESTONE_AUTO_STARTED", f"Uruchomiono AUTO dla milestone {selected}", milestone_id=selected, plan_version=plan.plan_version)
            return updated, None

        memory.execution_transaction(transition)
        return self.milestone_auto_snapshot(project_id, run_id=run_id)

    def stop_milestone_auto(self, project_id: str, *, run_id: str, reason: str = "stopped_by_user") -> dict[str, Any]:
        project, plan, memory = self._project(project_id)
        run_id = _identifier(run_id, "milestone_run_id")

        def transition(state: ProjectMemoryState) -> tuple[ProjectMemoryState, None]:
            execution = _execution_document(state)
            runs = dict(execution.get("milestone_runs", {}))
            run = runs.get(run_id)
            if not isinstance(run, Mapping):
                _fail("milestone_run_not_found", "milestone run does not exist")
            updated_run = {**dict(run), "status": "stopped", "stop_reason": _text(reason, "stop_reason", max_length=256), "updated_at": _utc_now()}
            runs[run_id] = updated_run
            execution["milestone_runs"] = runs
            if isinstance(execution.get("active_milestone_run"), Mapping) and execution["active_milestone_run"].get("milestone_run_id") == run_id:
                execution["active_milestone_run"] = updated_run
            updated = replace(state, execution=execution)
            return memory._append_event(updated, "MILESTONE_AUTO_STOPPED", f"Zatrzymano AUTO milestone {updated_run.get('milestone_id')}: {reason}", milestone_id=updated_run.get("milestone_id"), plan_version=plan.plan_version), None

        memory.execution_transaction(transition)
        return self.milestone_auto_snapshot(project_id, run_id=run_id)

    def milestone_auto_snapshot(self, project_id: str, *, run_id: str | None = None) -> dict[str, Any]:
        project, plan, memory = self._project(project_id)
        state = memory.read_state()
        execution = _execution_document(state)
        active = execution.get("active_milestone_run") if isinstance(execution.get("active_milestone_run"), Mapping) else None
        selected_run = run_id or (active.get("milestone_run_id") if active else None)
        run = execution.get("milestone_runs", {}).get(selected_run) if selected_run else active
        progress = self._milestone_auto_projection(plan, state, run)
        return {
            "schema": "bdb-milestone-auto-v1",
            "project_id": project_id,
            "plan_version": plan.plan_version,
            "milestone_run_id": selected_run,
            "milestone_id": progress.get("milestone_id"),
            "status": progress.get("status"),
            "current_task_id": progress.get("next_task_id"),
            "completed_tasks": progress.get("completed_tasks", 0),
            "total_tasks": progress.get("total_tasks", 0),
            "runnable_task_ids": list(progress.get("runnable_task_ids", [])),
            "blocker": progress.get("blocker"),
            "task_statuses": dict(execution.get("task_statuses", {})),
        }

    @staticmethod
    def _milestone_auto_projection(plan: ProjectPlan, state: ProjectMemoryState, run: Mapping[str, Any] | None) -> dict[str, Any]:
        """Project AUTO state without allowing a stopped run to look runnable.

        The durable run state is authoritative for whether Browser AUTO may
        continue.  Progress calculation remains authoritative for the
        deterministic task cursor only while the run is actually running.
        """
        milestone_id = run.get("milestone_id") if isinstance(run, Mapping) else None
        progress = milestone_auto_progress(plan, state, str(milestone_id) if milestone_id is not None else None)
        if not isinstance(run, Mapping):
            return progress
        run_status = str(run.get("status") or "")
        if run_status == "completed":
            return {**progress, "status": "MILESTONE_COMPLETED", "next_task_id": None}
        if run_status == "stopped":
            return {**progress, "status": "STOPPED", "next_task_id": run.get("current_task_id")}
        if run_status in {"blocked", "review"}:
            status = "BLOCKED" if run_status == "blocked" else "REVIEW_REQUIRED"
            current_task_id = run.get("current_task_id") or progress.get("next_task_id")
            blocker = progress.get("blocker")
            if not blocker and current_task_id:
                blocker = {"id": current_task_id, "kind": "task" if run_status == "blocked" else "review", "status": run_status}
            return {**progress, "status": status, "next_task_id": current_task_id, "blocker": blocker, "runnable_task_ids": []}
        if run_status == "running":
            return progress
        # Unknown run states are never a Browser AUTO admission.
        return {**progress, "status": "BLOCKED", "next_task_id": run.get("current_task_id") or progress.get("next_task_id"), "runnable_task_ids": []}

    @staticmethod
    def _criterion_type(criterion: str) -> str:
        lowered = criterion.strip().lower()
        if lowered.startswith(("manual:", "review:", "visual:")):
            return "MANUAL_REVIEW"
        if lowered.startswith("external:"):
            return "EXTERNAL"
        if lowered.startswith(("unknown:", "tbd:")):
            return "UNKNOWN"
        return "DETERMINISTIC"

    def _evaluate_acceptance(self, task: ProjectTask, *, project_id: str, plan_version: str, attempt_id: str, validation_ok: bool, criteria: Iterable[Mapping[str, Any]] | None) -> TaskAcceptanceResult:
        supplied = {str(item.get("criterion")): item for item in (criteria or ()) if isinstance(item, Mapping)}
        normalized: list[Mapping[str, Any]] = []
        deterministic_failure = False; review_required = False; unknown = False
        for criterion in task.acceptance_criteria:
            item = supplied.get(criterion, {})
            kind = str(item.get("type") or self._criterion_type(criterion)).upper()
            if kind not in {"DETERMINISTIC", "MANUAL_REVIEW", "EXTERNAL", "UNKNOWN"}:
                kind = "UNKNOWN"
            status = str(item.get("status") or ("PASS" if validation_ok and kind == "DETERMINISTIC" else "REVIEW_REQUIRED" if kind in {"MANUAL_REVIEW", "EXTERNAL"} else "UNKNOWN")).upper()
            if status not in {"PASS", "FAIL", "REVIEW_REQUIRED", "UNKNOWN"}:
                status = "UNKNOWN"
            evidence_ref = item.get("evidence_ref")
            normalized.append({"criterion": criterion, "type": kind, "status": status, "evidence_ref": evidence_ref})
            if kind == "DETERMINISTIC" and status == "FAIL": deterministic_failure = True
            elif kind in {"MANUAL_REVIEW", "EXTERNAL"} and status != "PASS": review_required = True
            elif kind == "UNKNOWN" or status == "UNKNOWN": unknown = True
        overall = "FAIL" if deterministic_failure or not validation_ok else "UNKNOWN" if unknown else "REVIEW_REQUIRED" if review_required else "PASS"
        return TaskAcceptanceResult(project_id, plan_version, task.task_id, attempt_id, tuple(normalized), overall, _utc_now())

    def record_result(self, project_id: str, result: Mapping[str, Any]) -> ProjectExecutionAttempt:
        project, plan, memory = self._project(project_id)
        binding_id = _identifier(result.get("execution_binding_id"), "execution_binding_id")
        binding: ProjectExecutionBinding | None = None
        stale = {"value": False, "code": None}

        def transition(state: ProjectMemoryState) -> tuple[ProjectMemoryState, ProjectExecutionAttempt]:
            nonlocal binding
            execution = _execution_document(state)
            raw_binding = next((item for item in execution["bindings"] if item.get("execution_binding_id") == binding_id), None)
            if raw_binding is None:
                _fail("execution_binding_not_found", "execution binding does not exist")
            binding = _binding_from_dict(raw_binding)
            result_digest = execution_result_digest(binding, result)
            existing = next((item for item in execution["attempts"] if item.get("execution_binding_id") == binding_id and item.get("result_digest") == result_digest), None)
            if existing is not None:
                replay = _attempt_from_dict(existing)
                if replay.result_status == "STALE_RESULT":
                    stale["value"] = True
                    stale["code"] = replay.failure_code or "execution_binding_stale"
                return state, replay

            stale_code: str | None = None
            current_binding_id = execution.get("current_binding_id")
            if (
                binding.project_id != project.project_id
                or binding.repo_alias != project.repo_alias
                or binding.plan_version != plan.plan_version
                or binding.superseded
                or binding.status != STATUS_ACTIVE
                or (current_binding_id is not None and current_binding_id != binding.execution_binding_id)
            ):
                stale_code = "execution_binding_stale"
            if result.get("command_id") != binding.command_id or result.get("correlation_id") != binding.correlation_id:
                stale_code = "execution_identity_mismatch"
            if result.get("project_id") not in (None, binding.project_id) or result.get("task_id") not in (None, binding.task_id) or result.get("plan_version") not in (None, binding.plan_version):
                stale_code = "execution_subject_mismatch"
            if result.get("repo_alias") not in (None, binding.repo_alias):
                stale_code = "repo_identity_mismatch"
            if result.get("head_before") not in (None, binding.expected_repo_head_before):
                stale_code = "repo_head_mismatch"
            task = next((item for item in plan.tasks if item.task_id == binding.task_id), None)
            if task is None:
                stale_code = "task_superseded"
            attempt_id = _identifier(result.get("attempt_id") or f"attempt-{uuid.uuid4().hex}", "attempt_id")
            if stale_code:
                stale["value"] = True
                stale["code"] = stale_code
                attempt = ProjectExecutionAttempt(
                    attempt_id,
                    project.project_id,
                    binding.plan_version,
                    binding.task_id,
                    binding_id,
                    binding.command_id,
                    binding.created_at,
                    _utc_now(),
                    str(result.get("head_before") or binding.expected_repo_head_before),
                    result.get("head_after"),
                    _status(result.get("execution_status", "UNKNOWN"), "execution_status"),
                    _status(result.get("validation_status", "UNKNOWN"), "validation_status"),
                    _status(result.get("promotion_status", "NOT_RUN"), "promotion_status"),
                    "STALE_RESULT",
                    _text(result.get("result_summary", "stale execution result"), "result_summary", max_length=4_000, required=False),
                    tuple(_text(item, "evidence_ref", max_length=512) for item in result.get("evidence_refs", [])),
                    stale_code,
                    result_digest,
                )
                attempt_document = attempt.to_dict()
                attempt_document["canonical_refs"] = dict(result.get("canonical_refs", {})) if isinstance(result.get("canonical_refs", {}), Mapping) else {}
                execution["attempts"].append(attempt_document)
                execution["stale_result"] = True
                updated = replace(state, execution=execution)
                updated = memory._append_event(
                    updated,
                    "EXECUTION_STALE_RESULT",
                    f"Późny wynik zadania {binding.task_id} wymaga reconciliacji ({stale_code})",
                    task_id=binding.task_id,
                    plan_version=binding.plan_version,
                    correlation_id=binding.correlation_id,
                )
                return updated, attempt

            if task is None:
                _fail("task_not_found", "bound task does not exist")
            validation_ok = _status(result.get("validation_status", "UNKNOWN"), "validation_status") in {"PASS", "SUCCEEDED", "SUCCESS"}
            acceptance = self._evaluate_acceptance(task, project_id=project.project_id, plan_version=plan.plan_version, attempt_id=attempt_id, validation_ok=validation_ok, criteria=result.get("criteria"))
            execution_ok = _status(result.get("execution_status", "UNKNOWN"), "execution_status") in {"PASS", "SUCCEEDED", "SUCCESS"}
            overall = acceptance.overall if execution_ok else "FAIL"
            attempt = ProjectExecutionAttempt(
                attempt_id,
                project.project_id,
                binding.plan_version,
                binding.task_id,
                binding_id,
                binding.command_id,
                binding.created_at,
                _utc_now(),
                str(result.get("head_before") or binding.expected_repo_head_before),
                result.get("head_after"),
                _status(result.get("execution_status", "UNKNOWN"), "execution_status"),
                _status(result.get("validation_status", "UNKNOWN"), "validation_status"),
                _status(result.get("promotion_status", "NOT_RUN"), "promotion_status"),
                overall,
                _text(result.get("result_summary", ""), "result_summary", max_length=4_000, required=False),
                tuple(_text(item, "evidence_ref", max_length=512) for item in result.get("evidence_refs", [])),
                result.get("failure_code"),
                result_digest,
            )
            attempt_document = attempt.to_dict()
            attempt_document["canonical_refs"] = dict(result.get("canonical_refs", {})) if isinstance(result.get("canonical_refs", {}), Mapping) else {}
            execution["attempts"].append(attempt_document)
            execution["acceptance_results"].append(acceptance.to_dict())

            # Terminalize the current binding (ACTIVE -> ACCEPTED if PASS, ACTIVE -> FAILED if not PASS)
            terminal_status = STATUS_ACCEPTED if overall == "PASS" else STATUS_FAILED
            validate_binding_transition(raw_binding.get("status", STATUS_ACTIVE), terminal_status)
            raw_binding["status"] = terminal_status
            raw_binding["finished_at"] = _utc_now()

            statuses = dict(execution.get("task_statuses", {}))
            previous = statuses.get(task.task_id, task.status)
            new_status = "completed" if overall == "PASS" else "review" if overall in {"REVIEW_REQUIRED", "UNKNOWN"} else "blocked" if result.get("failure_code") or not validation_ok else "active"
            if previous == "completed" and new_status != "completed":
                _fail("task_completed_downgrade", "completed task cannot be downgraded by an execution result")
            statuses[task.task_id] = new_status
            execution["task_statuses"] = statuses
            if new_status == "completed":
                updated = replace(state, execution=execution)
                updated = memory._append_event(updated, "TASK_COMPLETED", f"Zakończono zadanie {task.task_id}; acceptance {overall}", task_id=task.task_id, plan_version=plan.plan_version, correlation_id=binding.correlation_id)
            elif new_status == "review":
                updated = replace(state, execution=execution)
                updated = memory._append_event(updated, "TASK_REVIEW", f"Zadanie {task.task_id} gotowe do przeglądu", task_id=task.task_id, plan_version=plan.plan_version, correlation_id=binding.correlation_id)
            elif new_status == "blocked":
                updated = replace(state, execution=execution)
                updated = memory._append_event(updated, "TASK_BLOCKED", f"Zadanie {task.task_id} zablokowane: {result.get('failure_code') or 'validation_failed'}", task_id=task.task_id, plan_version=plan.plan_version, correlation_id=binding.correlation_id)
            else:
                updated = replace(state, execution=execution)
                updated = memory._append_event(updated, "EXECUTION_COMPLETED", f"Próba zadania {task.task_id} zakończona: {overall}", task_id=task.task_id, plan_version=plan.plan_version, correlation_id=binding.correlation_id)
            active_run = execution.get("active_milestone_run") if isinstance(execution.get("active_milestone_run"), Mapping) else None
            if active_run and active_run.get("status") in {"running", "review", "blocked"}:
                progress = milestone_auto_progress(plan, updated, str(active_run.get("milestone_id")))
                execution = dict(updated.execution)
                execution["current_binding_id"] = None
                runs = dict(execution.get("milestone_runs", {}))
                run_id = active_run.get("milestone_run_id")
                run = dict(runs.get(run_id, active_run))
                progress_status = str(progress.get("status") or "RUNNABLE")
                if new_status == "completed":
                    run_status = "completed" if progress_status == "MILESTONE_COMPLETED" else "running"
                    next_task_id = progress.get("next_task_id")
                elif new_status in {"review", "blocked"}:
                    run_status = "review" if new_status == "review" else "blocked"
                    next_task_id = task.task_id
                else:
                    progress_status = "RUNNABLE"
                    run_status = "running"
                    next_task_id = task.task_id
                run.update({
                    "updated_at": _utc_now(),
                    "current_task_id": next_task_id,
                    "completed_tasks": progress.get("completed_tasks", 0),
                    "total_tasks": progress.get("total_tasks", 0),
                    "status": run_status,
                    "progress_status": progress_status,
                    "blocker": progress.get("blocker"),
                })
                runs[run_id] = run
                execution["milestone_runs"] = runs
                execution["active_milestone_run"] = run
                execution["current_task_id"] = next_task_id
                if progress.get("status") == "MILESTONE_COMPLETED":
                    updated = memory._append_event(updated, "MILESTONE_AUTO_COMPLETED", f"AUTO ukończył milestone {active_run.get('milestone_id')}", milestone_id=active_run.get("milestone_id"), plan_version=plan.plan_version, correlation_id=binding.correlation_id)
            else:
                available = available_project_tasks(plan, updated)
                execution = dict(updated.execution)
                execution["current_task_id"] = available[0].task_id if len(available) == 1 else None
                execution["current_binding_id"] = None
            completed_milestones = set(execution.get("milestones_completed", []))
            for milestone in plan.milestones:
                required = [item for item in plan.tasks if item.milestone_id == milestone.milestone_id]
                if required and all(statuses.get(item.task_id, item.status) in {"completed", "skipped"} for item in required) and milestone.milestone_id not in completed_milestones:
                    completed_milestones.add(milestone.milestone_id)
                    updated = memory._append_event(updated, "MILESTONE_COMPLETED", f"Zakończono milestone {milestone.milestone_id}", milestone_id=milestone.milestone_id, plan_version=plan.plan_version, correlation_id=binding.correlation_id)
            execution["milestones_completed"] = sorted(completed_milestones)
            updated = replace(updated, execution=execution)
            return updated, attempt

        attempt = memory.execution_transaction(transition)
        if stale["value"]:
            raise ProjectExecutionError("STALE_RESULT", "execution result is stale and requires reconciliation", details={"attempt_id": attempt.attempt_id, "reason": stale["code"]})
        self.reconcile(project_id)
        return attempt

    def reconcile_project_bindings(self, project_id: str) -> None:
        _project, _plan, memory = self._project(project_id)

        def transition(state: ProjectMemoryState) -> tuple[ProjectMemoryState, None]:
            execution = _execution_document(state)
            reconciled = reconcile_execution_bindings(execution)
            updated = replace(state, execution=reconciled)
            updated = memory._append_event(
                updated,
                "BINDINGS_RECONCILED",
                f"Zrekoncyliowano powiązania wykonania projektu {project_id}",
            )
            return updated, None

        memory.execution_transaction(transition)

    def check_invariants(self, project_id: str) -> tuple[bool, list[str]]:
        _project, _plan, memory = self._project(project_id)
        execution = _execution_document(memory.read_state())
        return check_binding_lifecycle_invariants(execution)

    def record_bdb_finalization(self, project_id: str, binding: ProjectExecutionBinding, finalization: Any, *, criteria: Iterable[Mapping[str, Any]] | None = None, promotion_status: str = "NOT_RUN") -> ProjectExecutionAttempt:
        """Adapt an existing EngineeringLoop finalization into this binding.

        No execution is performed here; Candidate/Evidence/Publication objects
        are read and their immutable IDs are carried into the Project Memory
        attempt for exact lineage and replay checks.
        """
        validation = getattr(finalization, "validation", None)
        candidate = getattr(finalization, "candidate", None)
        candidate_view = getattr(finalization, "candidate_view", None)
        evaluation = getattr(finalization, "evaluation", None)
        publication = getattr(finalization, "publication", None)
        result = getattr(validation, "result", None)
        if validation is None or result is None or candidate is None or candidate_view is None:
            _fail("bdb_finalization_invalid", "EngineeringLoop finalization lacks canonical Candidate/validation records")
        candidate_task_id = getattr(candidate, "task_id", None)
        view_task_id = getattr(candidate_view, "task_id", None)
        if candidate_task_id not in (None, binding.task_id) or view_task_id not in (None, binding.task_id):
            _fail("bdb_finalization_binding_mismatch", "Candidate finalization is bound to a different project task")
        evidence_id = getattr(validation, "evidence_id", None)
        refs = [item for item in (evidence_id, getattr(validation, "validation_id", None), getattr(candidate, "candidate_id", None), getattr(evaluation, "evaluation_id", None), getattr(publication, "publication_id", None)) if item]
        canonical_refs = {"task_id": getattr(candidate, "task_id", None), "work_id": getattr(candidate, "work_id", None), "candidate_id": getattr(candidate, "candidate_id", None), "candidate_view_id": getattr(candidate_view, "view_id", None), "candidate_tree_digest": getattr(candidate_view, "candidate_tree_digest", None), "base_commit_oid": getattr(candidate_view, "base_commit_oid", None), "validation_id": getattr(validation, "validation_id", None), "evidence_id": evidence_id, "evaluation_id": getattr(evaluation, "evaluation_id", None), "publication_id": getattr(publication, "publication_id", None)}
        return self.record_result(project_id, {"execution_binding_id": binding.execution_binding_id, "command_id": binding.command_id, "correlation_id": binding.correlation_id, "head_before": binding.expected_repo_head_before, "head_after": getattr(candidate_view, "base_commit_oid", None) or binding.expected_repo_head_before, "execution_status": "PASS" if getattr(candidate, "state", "") in {"SEALED", "OBSERVED"} else "FAIL", "validation_status": getattr(result, "status", "UNKNOWN"), "promotion_status": promotion_status, "result_summary": "Canonical EngineeringLoop finalization", "evidence_refs": refs, "canonical_refs": canonical_refs, "criteria": list(criteria or ())})

    def approve_review(self, project_id: str, task_id: str, *, reason: str) -> None:
        project, plan, memory = self._project(project_id)
        def transition(state: ProjectMemoryState) -> tuple[ProjectMemoryState, None]:
            execution = _execution_document(state); statuses = dict(execution.get("task_statuses", {}));
            if statuses.get(task_id) != "review": _fail("task_review_required", "task is not awaiting manual review")
            acceptance = next((item for item in reversed(execution["acceptance_results"]) if item.get("task_id") == task_id), None)
            if acceptance is None or acceptance.get("overall") != "REVIEW_REQUIRED": _fail("task_review_invalid", "task has no reviewable acceptance result")
            if any(item.get("type") == "DETERMINISTIC" and item.get("status") == "FAIL" for item in acceptance.get("criteria", [])):
                _fail("deterministic_acceptance_failed", "manual approval cannot override deterministic failure")
            statuses[task_id] = "completed"; execution["task_statuses"] = statuses
            candidate_state = replace(state, execution=execution)
            active_run = execution.get("active_milestone_run") if isinstance(execution.get("active_milestone_run"), Mapping) else None
            if active_run and active_run.get("status") in {"running", "review", "blocked"}:
                progress = milestone_auto_progress(plan, candidate_state, str(active_run.get("milestone_id")))
                execution["current_task_id"] = progress.get("next_task_id")
                run_id = active_run.get("milestone_run_id")
                runs = dict(execution.get("milestone_runs", {})); run = dict(runs.get(run_id, active_run))
                run.update({"current_task_id": progress.get("next_task_id"), "completed_tasks": progress.get("completed_tasks", 0), "total_tasks": progress.get("total_tasks", 0), "status": "completed" if progress.get("status") == "MILESTONE_COMPLETED" else "running", "progress_status": progress.get("status"), "updated_at": _utc_now()})
                runs[run_id] = run; execution["milestone_runs"] = runs; execution["active_milestone_run"] = run
            else:
                available = available_project_tasks(plan, candidate_state); execution["current_task_id"] = available[0].task_id if len(available) == 1 else None
            updated = replace(candidate_state, execution=execution); updated = memory._append_event(updated, "TASK_REVIEW_ACCEPTED", f"Zatwierdzono ręczny przegląd zadania {task_id}: {_text(reason, 'review_reason', max_length=2_000)}", task_id=task_id, plan_version=plan.plan_version); return updated, None
        memory.execution_transaction(transition); self.reconcile(project_id)

    def request_changes(self, project_id: str, task_id: str, *, reason: str) -> None:
        project, plan, memory = self._project(project_id)
        def transition(state: ProjectMemoryState) -> tuple[ProjectMemoryState, None]:
            execution = _execution_document(state); statuses = dict(execution.get("task_statuses", {}));
            if statuses.get(task_id) != "review": _fail("task_review_required", "task is not awaiting manual review")
            statuses[task_id] = "active"; execution["task_statuses"] = statuses; execution["current_task_id"] = task_id; execution["current_binding_id"] = None
            active_run = execution.get("active_milestone_run") if isinstance(execution.get("active_milestone_run"), Mapping) else None
            if active_run:
                run_id = active_run.get("milestone_run_id"); runs = dict(execution.get("milestone_runs", {})); run = dict(runs.get(run_id, active_run)); run.update({"status": "running", "current_task_id": task_id, "updated_at": _utc_now()}); runs[run_id] = run; execution["milestone_runs"] = runs; execution["active_milestone_run"] = run
            updated = replace(state, execution=execution); updated = memory._append_event(updated, "TASK_REVIEW_CHANGES_REQUESTED", f"Wymagane poprawki dla {task_id}: {_text(reason, 'review_reason', max_length=2_000)}", task_id=task_id, plan_version=plan.plan_version); return updated, None
        memory.execution_transaction(transition); self.reconcile(project_id)

    def request_project_review(self, project_id: str, *, reason: str = "review requested") -> None:
        project, plan, memory = self._project(project_id)
        memory.append_event("PROJECT_REVIEW_REQUESTED", _text(reason, "review_reason", max_length=2_000), plan_version=plan.plan_version)

    def reconcile(self, project_id: str) -> ProjectRecord:
        project, plan, memory = self._project(project_id)
        state = memory.read_state(); execution = _execution_document(state); statuses = dict(execution.get("task_statuses", {}))
        completed = sum(statuses.get(task.task_id, task.status) in {"completed", "skipped"} for task in plan.tasks)
        active_run = execution.get("active_milestone_run") if isinstance(execution.get("active_milestone_run"), Mapping) else None
        run_status = str(active_run.get("status") or "") if active_run else None
        auto_progress = milestone_auto_progress(plan, state, str(active_run.get("milestone_id"))) if run_status == "running" else None
        available = available_project_tasks(plan, state, str(active_run.get("milestone_id"))) if run_status == "running" and active_run else available_project_tasks(plan, state)
        if "current_task_id" in execution:
            current_id = execution.get("current_task_id")
            if run_status == "running":
                current_id = auto_progress.get("next_task_id") if auto_progress else current_id
            elif run_status in {"review", "blocked"}:
                current_id = active_run.get("current_task_id") or current_id
            elif current_id is None and not (active_run and active_run.get("status") in {"completed", "stopped"}) and len(available) == 1:
                current_id = available[0].task_id
        else:
            current_id = plan.current_task_id if completed < len(plan.tasks) else None
        current = next((item for item in plan.tasks if item.task_id == current_id), None)
        has_blocked = any(statuses.get(task.task_id, task.status) == "blocked" for task in plan.tasks)
        has_review = any(statuses.get(task.task_id, task.status) == "review" for task in plan.tasks)
        project_status = "completed" if completed == len(plan.tasks) and plan.tasks else "blocked" if has_blocked else "active"
        if has_review and project_status == "active":
            project_status = "active"
        if execution.get("current_task_id") != current_id:
            def set_pointer(state: ProjectMemoryState) -> tuple[ProjectMemoryState, None]:
                current_execution = _execution_document(state); current_execution["current_task_id"] = current_id
                return replace(state, execution=current_execution), None
            memory.execution_transaction(set_pointer)
        updated = ProjectRecord(**{**project.__dict__, "project_status": project_status, "plan_imported": True, "plan_version": plan.plan_version, "total_tasks": len(plan.tasks), "completed_tasks": completed, "current_milestone": current.milestone_id if current else None, "current_task": current_id})
        return self.catalog.upsert(updated)

    def snapshot(self, project_id: str) -> dict[str, Any]:
        project, plan, memory = self._project(project_id); state = memory.read_state(); execution = _execution_document(state)
        active_run = execution.get("active_milestone_run") if isinstance(execution.get("active_milestone_run"), Mapping) else None
        auto_progress = self._milestone_auto_projection(plan, state, active_run) if active_run else None
        return {"schema": PROJECT_EXECUTION_SCHEMA, "project_id": project_id, "plan_version": plan.plan_version, "task_statuses": dict(execution.get("task_statuses", {})), "gate_statuses": dict(execution.get("gate_statuses", {})), "open_question_statuses": dict(execution.get("open_question_statuses", {})), "bindings": list(execution["bindings"]), "attempts": list(execution["attempts"]), "acceptance_results": list(execution["acceptance_results"]), "checkpoints": dict(execution.get("checkpoints", {})), "launch_handoffs": dict(execution.get("launch_handoffs", {})), "current_binding_id": execution.get("current_binding_id"), "current_task_id": execution.get("current_task_id"), "available_tasks": [task.task_id for task in (available_project_tasks(plan, state, str(active_run.get("milestone_id"))) if active_run else available_project_tasks(plan, state))], "milestone_auto": {**(auto_progress or {}), "milestone_run_id": active_run.get("milestone_run_id")} if active_run and auto_progress else None, "watchdog": self.watchdog(project_id), "stale_result": bool(execution.get("stale_result", False))}


__all__ = ["PROJECT_EXECUTION_SCHEMA", "PROJECT_EXECUTION_SUBMISSION_SCHEMA", "PROJECT_EXECUTION_CHECKPOINT_SCHEMA", "PROJECT_LAUNCH_HANDOFF_SCHEMA", "ProjectExecutionAttempt", "ProjectExecutionBinding", "ProjectExecutionSubmission", "ProjectExecutionCoordinator", "ProjectExecutionError", "TaskAcceptanceResult", "execution_result_digest"]

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

from .project_catalog import ProjectCatalog, ProjectPlan, ProjectRecord, ProjectTask
from .project_memory import ProjectMemoryState, ProjectMemoryStore, available_project_tasks, task_prerequisite_blockers


PROJECT_EXECUTION_SCHEMA = "bdb-project-execution-v1"
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_HEAD_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


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


def _status(value: object, field: str) -> str:
    value = _text(value, field, max_length=32).upper()
    return value


def _execution_document(state: ProjectMemoryState) -> dict[str, Any]:
    raw = state.execution if isinstance(state.execution, Mapping) else {}
    if not raw:
        return {"schema": PROJECT_EXECUTION_SCHEMA, "bindings": [], "attempts": [], "acceptance_results": [], "task_statuses": {}, "gate_statuses": {}, "open_question_statuses": {}, "milestones_completed": []}
    if raw.get("schema", PROJECT_EXECUTION_SCHEMA) != PROJECT_EXECUTION_SCHEMA:
        _fail("execution_schema_invalid", "project execution state schema differs")
    result = dict(raw)
    result.setdefault("bindings", [])
    result.setdefault("attempts", [])
    result.setdefault("acceptance_results", [])
    result.setdefault("task_statuses", {})
    result.setdefault("milestones_completed", [])
    for key in ("bindings", "attempts", "acceptance_results"):
        if not isinstance(result[key], list) or len(result[key]) > 512 or any(not isinstance(item, Mapping) for item in result[key]):
            _fail("execution_shape_invalid", f"execution.{key} is invalid")
    if not isinstance(result["task_statuses"], Mapping) or len(result["task_statuses"]) > 2_048:
        _fail("execution_shape_invalid", "execution.task_statuses is invalid")
    for key in ("gate_statuses", "open_question_statuses"):
        if not isinstance(result[key], Mapping) or len(result[key]) > 512:
            _fail("execution_shape_invalid", f"execution.{key} is invalid")
    return result


@dataclass(frozen=True)
class ProjectExecutionBinding:
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

    def to_dict(self) -> dict[str, Any]:
        return {"schema": PROJECT_EXECUTION_SCHEMA, "execution_binding_id": self.execution_binding_id, "project_id": self.project_id, "plan_version": self.plan_version, "task_id": self.task_id, "launch_id": self.launch_id, "correlation_id": self.correlation_id, "command_id": self.command_id, "repo_alias": self.repo_alias, "expected_repo_head_before": self.expected_repo_head_before, "created_at": self.created_at, "status": self.status, "superseded": self.superseded}


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
    return ProjectExecutionBinding(_identifier(value.get("execution_binding_id"), "execution_binding_id"), _identifier(value.get("project_id"), "project_id"), _text(value.get("plan_version"), "plan_version", max_length=32), _identifier(value.get("task_id"), "task_id"), _identifier(value.get("launch_id"), "launch_id"), _identifier(value.get("correlation_id"), "correlation_id"), _identifier(value.get("command_id"), "command_id"), _text(value.get("repo_alias"), "repo_alias", max_length=64), _text(value.get("expected_repo_head_before"), "expected_repo_head_before", max_length=128), _text(value.get("created_at"), "created_at", max_length=64), _text(value.get("status", "ACTIVE"), "status", max_length=32), bool(value.get("superseded", False)))


def _attempt_from_dict(value: Mapping[str, Any]) -> ProjectExecutionAttempt:
    return ProjectExecutionAttempt(_identifier(value.get("attempt_id"), "attempt_id"), _identifier(value.get("project_id"), "project_id"), _text(value.get("plan_version"), "plan_version", max_length=32), _identifier(value.get("task_id"), "task_id"), _identifier(value.get("execution_binding_id"), "execution_binding_id"), _identifier(value.get("command_id"), "command_id"), _text(value.get("started_at"), "started_at", max_length=64), value.get("finished_at"), _text(value.get("head_before"), "head_before", max_length=128), value.get("head_after"), _status(value.get("execution_status"), "execution_status"), _status(value.get("validation_status"), "validation_status"), _status(value.get("promotion_status"), "promotion_status"), _status(value.get("result_status"), "result_status"), _text(value.get("result_summary", ""), "result_summary", max_length=4_000, required=False), tuple(_text(item, "evidence_ref", max_length=512) for item in value.get("evidence_refs", [])), value.get("failure_code"), _text(value.get("result_digest"), "result_digest", max_length=128))


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
        state = memory.read_state(); execution = _execution_document(state)
        selected = task_id or execution.get("current_task_id") or plan.current_task_id
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
        current_binding_id = execution.get("current_binding_id")
        if current_binding_id:
            current = next((item for item in execution.get("bindings", []) if item.get("execution_binding_id") == current_binding_id), None)
            if current is not None and current.get("status") == "ACTIVE" and current.get("task_id") == task.task_id and current.get("plan_version") == plan.plan_version:
                return _binding_from_dict(current)
        head = _text(expected_repo_head_before, "expected_repo_head_before", max_length=128)
        if head != "unknown" and _HEAD_RE.fullmatch(head) is None:
            _fail("repo_head_invalid", "expected_repo_head_before is not a Git object identity")
        suffix = uuid.uuid4().hex
        # The Browser/Native launch contract requires launch_id to be a UUID.
        # Keep the vNext binding/correlation/command identifiers namespaced as
        # before, but generate a browser-compatible launch identity by default.
        return ProjectExecutionBinding(f"binding-{suffix}", project.project_id, plan.plan_version, task.task_id, launch_id or str(uuid.uuid4()), correlation_id or f"corr-{suffix}", command_id or f"command-{suffix}", project.repo_alias, head, _utc_now())

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
            execution["bindings"].append(binding.to_dict())
            statuses = dict(execution.get("task_statuses", {})); statuses.setdefault(binding.task_id, "active"); execution["task_statuses"] = statuses; execution["current_task_id"] = binding.task_id; execution["current_binding_id"] = binding.execution_binding_id
            updated = replace(state, execution=execution)
            updated = memory._append_event(updated, "EXECUTION_BOUND", f"Powiązano wykonanie z zadaniem {binding.task_id}", task_id=binding.task_id, plan_version=binding.plan_version, correlation_id=binding.correlation_id)
            updated = memory._append_event(updated, "EXECUTION_STARTED", f"Rozpoczęto próbę zadania {binding.task_id}", task_id=binding.task_id, plan_version=binding.plan_version, correlation_id=binding.correlation_id)
            return updated, binding
        return memory.execution_transaction(transition)

    def start(self, project_id: str, **kwargs: Any) -> ProjectExecutionBinding:
        return self.persist_binding(self.new_binding(project_id, **kwargs))

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
            base_identity = {"execution_binding_id": binding_id, "command_id": binding.command_id, "correlation_id": binding.correlation_id, "project_id": binding.project_id, "task_id": binding.task_id, "plan_version": binding.plan_version, "repo_alias": binding.repo_alias, "result_project_id": result.get("project_id"), "result_task_id": result.get("task_id"), "result_plan_version": result.get("plan_version"), "head_before": result.get("head_before"), "head_after": result.get("head_after"), "execution_status": result.get("execution_status"), "validation_status": result.get("validation_status"), "promotion_status": result.get("promotion_status"), "summary": result.get("result_summary", ""), "evidence_refs": list(result.get("evidence_refs", [])), "criteria": result.get("criteria", [])}
            base_identity["canonical_refs"] = result.get("canonical_refs", {})
            result_digest = semantic_digest(base_identity)
            existing = next((item for item in execution["attempts"] if item.get("execution_binding_id") == binding_id and item.get("result_digest") == result_digest), None)
            if existing is not None:
                replay = _attempt_from_dict(existing)
                if replay.result_status == "STALE_RESULT":
                    stale["value"] = True
                    stale["code"] = replay.failure_code or "execution_binding_stale"
                return state, replay
            stale_code: str | None = None
            if binding.project_id != project.project_id or binding.repo_alias != project.repo_alias or binding.plan_version != plan.plan_version or binding.superseded or binding.status != "ACTIVE":
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
                stale["value"] = True; stale["code"] = stale_code
                attempt = ProjectExecutionAttempt(attempt_id, project.project_id, binding.plan_version, binding.task_id, binding_id, binding.command_id, binding.created_at, _utc_now(), str(result.get("head_before") or binding.expected_repo_head_before), result.get("head_after"), _status(result.get("execution_status", "UNKNOWN"), "execution_status"), _status(result.get("validation_status", "UNKNOWN"), "validation_status"), _status(result.get("promotion_status", "NOT_RUN"), "promotion_status"), "STALE_RESULT", _text(result.get("result_summary", "stale execution result"), "result_summary", max_length=4_000, required=False), tuple(_text(item, "evidence_ref", max_length=512) for item in result.get("evidence_refs", [])), stale_code, result_digest)
                attempt_document = attempt.to_dict(); attempt_document["canonical_refs"] = dict(result.get("canonical_refs", {})) if isinstance(result.get("canonical_refs", {}), Mapping) else {}
                execution["attempts"].append(attempt_document); execution["stale_result"] = True
                updated = replace(state, execution=execution); updated = memory._append_event(updated, "EXECUTION_STALE_RESULT", f"Późny wynik zadania {binding.task_id} wymaga reconciliacji ({stale_code})", task_id=binding.task_id, plan_version=binding.plan_version, correlation_id=binding.correlation_id)
                return updated, attempt
            if task is None:
                _fail("task_not_found", "bound task does not exist")
            validation_ok = _status(result.get("validation_status", "UNKNOWN"), "validation_status") in {"PASS", "SUCCEEDED", "SUCCESS"}
            acceptance = self._evaluate_acceptance(task, project_id=project.project_id, plan_version=plan.plan_version, attempt_id=attempt_id, validation_ok=validation_ok, criteria=result.get("criteria"))
            execution_ok = _status(result.get("execution_status", "UNKNOWN"), "execution_status") in {"PASS", "SUCCEEDED", "SUCCESS"}
            overall = acceptance.overall if execution_ok else "FAIL"
            attempt = ProjectExecutionAttempt(attempt_id, project.project_id, binding.plan_version, binding.task_id, binding_id, binding.command_id, binding.created_at, _utc_now(), str(result.get("head_before") or binding.expected_repo_head_before), result.get("head_after"), _status(result.get("execution_status", "UNKNOWN"), "execution_status"), _status(result.get("validation_status", "UNKNOWN"), "validation_status"), _status(result.get("promotion_status", "NOT_RUN"), "promotion_status"), overall, _text(result.get("result_summary", ""), "result_summary", max_length=4_000, required=False), tuple(_text(item, "evidence_ref", max_length=512) for item in result.get("evidence_refs", [])), result.get("failure_code"), result_digest)
            attempt_document = attempt.to_dict(); attempt_document["canonical_refs"] = dict(result.get("canonical_refs", {})) if isinstance(result.get("canonical_refs", {}), Mapping) else {}
            execution["attempts"].append(attempt_document); execution["acceptance_results"].append(acceptance.to_dict())
            statuses = dict(execution.get("task_statuses", {})); previous = statuses.get(task.task_id, task.status)
            new_status = "completed" if overall == "PASS" else "review" if overall in {"REVIEW_REQUIRED", "UNKNOWN"} else "blocked" if result.get("failure_code") or not validation_ok else "active"
            if previous == "completed" and new_status != "completed":
                _fail("task_completed_downgrade", "completed task cannot be downgraded by an execution result")
            statuses[task.task_id] = new_status; execution["task_statuses"] = statuses
            if new_status == "completed":
                updated = replace(state, execution=execution); updated = memory._append_event(updated, "TASK_COMPLETED", f"Zakończono zadanie {task.task_id}; acceptance {overall}", task_id=task.task_id, plan_version=plan.plan_version, correlation_id=binding.correlation_id)
            elif new_status == "review":
                updated = replace(state, execution=execution); updated = memory._append_event(updated, "TASK_REVIEW", f"Zadanie {task.task_id} gotowe do przeglądu", task_id=task.task_id, plan_version=plan.plan_version, correlation_id=binding.correlation_id)
            elif new_status == "blocked":
                updated = replace(state, execution=execution); updated = memory._append_event(updated, "TASK_BLOCKED", f"Zadanie {task.task_id} zablokowane: {result.get('failure_code') or 'validation_failed'}", task_id=task.task_id, plan_version=plan.plan_version, correlation_id=binding.correlation_id)
            else:
                updated = replace(state, execution=execution); updated = memory._append_event(updated, "EXECUTION_COMPLETED", f"Próba zadania {task.task_id} zakończona: {overall}", task_id=task.task_id, plan_version=plan.plan_version, correlation_id=binding.correlation_id)
            available = available_project_tasks(plan, updated)
            execution = dict(updated.execution); execution["current_task_id"] = available[0].task_id if len(available) == 1 else None; execution["current_binding_id"] = None
            completed_milestones = set(execution.get("milestones_completed", []))
            for milestone in plan.milestones:
                required = [item for item in plan.tasks if item.milestone_id == milestone.milestone_id]
                if required and all(statuses.get(item.task_id, item.status) in {"completed", "skipped"} for item in required) and milestone.milestone_id not in completed_milestones:
                    completed_milestones.add(milestone.milestone_id); updated = memory._append_event(updated, "MILESTONE_COMPLETED", f"Zakończono milestone {milestone.milestone_id}", milestone_id=milestone.milestone_id, plan_version=plan.plan_version, correlation_id=binding.correlation_id)
            execution["milestones_completed"] = sorted(completed_milestones); updated = replace(updated, execution=execution)
            return updated, attempt
        attempt = memory.execution_transaction(transition)
        if stale["value"]:
            raise ProjectExecutionError("STALE_RESULT", "execution result is stale and requires reconciliation", details={"attempt_id": attempt.attempt_id, "reason": stale["code"]})
        self.reconcile(project_id)
        return attempt

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
            available = available_project_tasks(plan, candidate_state); execution["current_task_id"] = available[0].task_id if len(available) == 1 else None
            updated = replace(candidate_state, execution=execution); updated = memory._append_event(updated, "TASK_REVIEW_ACCEPTED", f"Zatwierdzono ręczny przegląd zadania {task_id}: {_text(reason, 'review_reason', max_length=2_000)}", task_id=task_id, plan_version=plan.plan_version); return updated, None
        memory.execution_transaction(transition); self.reconcile(project_id)

    def request_changes(self, project_id: str, task_id: str, *, reason: str) -> None:
        project, plan, memory = self._project(project_id)
        def transition(state: ProjectMemoryState) -> tuple[ProjectMemoryState, None]:
            execution = _execution_document(state); statuses = dict(execution.get("task_statuses", {}));
            if statuses.get(task_id) != "review": _fail("task_review_required", "task is not awaiting manual review")
            statuses[task_id] = "active"; execution["task_statuses"] = statuses; execution["current_task_id"] = task_id; execution["current_binding_id"] = None
            updated = replace(state, execution=execution); updated = memory._append_event(updated, "TASK_REVIEW_CHANGES_REQUESTED", f"Wymagane poprawki dla {task_id}: {_text(reason, 'review_reason', max_length=2_000)}", task_id=task_id, plan_version=plan.plan_version); return updated, None
        memory.execution_transaction(transition); self.reconcile(project_id)

    def request_project_review(self, project_id: str, *, reason: str = "review requested") -> None:
        project, plan, memory = self._project(project_id)
        memory.append_event("PROJECT_REVIEW_REQUESTED", _text(reason, "review_reason", max_length=2_000), plan_version=plan.plan_version)

    def reconcile(self, project_id: str) -> ProjectRecord:
        project, plan, memory = self._project(project_id)
        state = memory.read_state(); execution = _execution_document(state); statuses = dict(execution.get("task_statuses", {}))
        completed = sum(statuses.get(task.task_id, task.status) in {"completed", "skipped"} for task in plan.tasks)
        available = available_project_tasks(plan, state)
        if "current_task_id" in execution:
            current_id = execution.get("current_task_id")
            if current_id is None and len(available) == 1:
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
        return {"schema": PROJECT_EXECUTION_SCHEMA, "project_id": project_id, "plan_version": plan.plan_version, "task_statuses": dict(execution.get("task_statuses", {})), "gate_statuses": dict(execution.get("gate_statuses", {})), "open_question_statuses": dict(execution.get("open_question_statuses", {})), "bindings": list(execution["bindings"]), "attempts": list(execution["attempts"]), "acceptance_results": list(execution["acceptance_results"]), "current_task_id": execution.get("current_task_id"), "available_tasks": [task.task_id for task in available_project_tasks(plan, state)], "stale_result": bool(execution.get("stale_result", False))}


__all__ = ["PROJECT_EXECUTION_SCHEMA", "ProjectExecutionAttempt", "ProjectExecutionBinding", "ProjectExecutionCoordinator", "ProjectExecutionError", "TaskAcceptanceResult"]

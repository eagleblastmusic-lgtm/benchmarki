from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from bdb_vnext.project_catalog import (
    ProjectBrief,
    ProjectCatalog,
    ProjectCatalogError,
    classify_dependency_targets,
    new_project_record,
    validate_project_plan,
)
from bdb_vnext.project_execution import ProjectExecutionBinding, ProjectExecutionCoordinator, ProjectExecutionError
from bdb_vnext.project_memory import (
    ProjectMemoryStore,
    ProjectMemoryState,
    available_project_tasks,
    project_health,
    resolve_next_action,
    task_prerequisite_blockers,
)
from bdb_vnext.project_workflow import ProjectWorkflow, ProjectWorkflowError
from bdb_vnext.work_planning import WorkPlanningPromptBuilder


PROJECT_ID = "prerequisite-fixture"
HEAD_A = "a" * 40
HEAD_B = "b" * 40


def _document(*, project_id: str = PROJECT_ID, tasks: list[dict[str, object]] | None = None, context: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "schema": "bdb-project-plan-v1",
        "project_id": project_id,
        "project_name": "Prerequisite Fixture",
        "plan_version": 1,
        "milestones": [
            {"id": "P0", "title": "Foundation", "description": "Foundation", "status": "completed"},
            {"id": "P1", "title": "Build", "description": "Build", "status": "active"},
            {"id": "P7", "title": "Release", "description": "Release", "status": "pending"},
        ],
        "tasks": tasks or [
            {"id": "P0-01", "milestone_id": "P0", "title": "Foundation", "description": "Foundation done", "status": "completed", "dependencies": [], "acceptance_criteria": ["test:foundation"]},
            {"id": "P1-01", "milestone_id": "P1", "title": "Build", "description": "Build the feature", "status": "pending", "dependencies": ["G0"], "acceptance_criteria": ["test:build"]},
            {"id": "P7-01", "milestone_id": "P7", "title": "Verify", "description": "Verify the feature", "status": "pending", "dependencies": ["P1-01"], "acceptance_criteria": ["test:verify"]},
            {"id": "P7-02", "milestone_id": "P7", "title": "Release", "description": "Release the feature", "status": "pending", "dependencies": ["P7-01", "OQ-001"], "acceptance_criteria": ["test:release"]},
        ],
        "current_task_id": "P1-01",
        "planning_context": context or {
            "gates": [{"id": "G0", "title": "Foundation gate", "criteria": "Foundation is approved"}],
            "open_questions": [{"id": "OQ-001", "question": "Which release cadence?"}],
        },
    }


def _plan(**kwargs):
    return validate_project_plan(_document(**kwargs), expected_project_id=kwargs.get("project_id", PROJECT_ID))


def _fixture(tmp_path: Path):
    runtime = tmp_path / "runtime"
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    brief = ProjectBrief("Prerequisite Fixture", "Exercise prerequisite semantics", "Bounded fixture", "test")
    project = new_project_record(project_id=PROJECT_ID, display_name="Prerequisite Fixture", repo_alias="prerequisite-fixture", local_repo_path=repo, github_repo=None, brief=brief)
    catalog = ProjectCatalog(runtime)
    catalog.upsert(project)
    memory = ProjectMemoryStore(runtime, PROJECT_ID)
    plan = memory.ensure_initial_plan(_plan())
    project = replace(project, plan_imported=True, plan_version=plan.plan_version, total_tasks=len(plan.tasks), current_milestone=plan.current_milestone.title if plan.current_milestone else None, current_task=plan.current_task_id, plan_path=str(memory.current_pointer), project_status="active")
    catalog.upsert(project)
    return catalog, memory, ProjectExecutionCoordinator(runtime, catalog=catalog), project, plan


def _record_pass(coordinator: ProjectExecutionCoordinator, project_id: str, binding: ProjectExecutionBinding, before: str, after: str) -> None:
    coordinator.record_result(project_id, {"execution_binding_id": binding.execution_binding_id, "command_id": binding.command_id, "correlation_id": binding.correlation_id, "head_before": before, "head_after": after, "execution_status": "PASS", "validation_status": "PASS", "promotion_status": "NOT_RUN"})


def test_dependency_targets_are_classified_and_task_cycles_ignore_non_tasks() -> None:
    plan = _plan()
    assert classify_dependency_targets(plan) == {"G0": "gate", "OQ-001": "open_question", "P0-01": "task", "P1-01": "task", "P7-01": "task", "P7-02": "task"}
    no_task_cycle = _document(tasks=[
        {"id": "P0-01", "milestone_id": "P0", "title": "Foundation", "description": "Foundation done", "status": "completed", "dependencies": [], "acceptance_criteria": ["test"]},
        {"id": "P1-01", "milestone_id": "P1", "title": "Build", "description": "Build", "status": "pending", "dependencies": ["G0"], "acceptance_criteria": ["test"]},
    ])
    validate_project_plan(no_task_cycle, expected_project_id=PROJECT_ID)


@pytest.mark.parametrize(
    ("mutator", "code"),
    [
        (lambda document: document["tasks"][1].update({"dependencies": ["missing"]}), "plan_dependency_missing"),
        (lambda document: document["tasks"][1].update({"dependencies": ["G0", "G0"]}), "plan_dependency_invalid"),
        (lambda document: document["planning_context"]["gates"].append({"id": "P1-01", "title": "Collision", "criteria": "ambiguous"}), "plan_dependency_ambiguous"),
    ],
)
def test_invalid_dependency_targets_fail_closed(mutator, code: str) -> None:
    document = _document()
    mutator(document)
    with pytest.raises(ProjectCatalogError) as error:
        validate_project_plan(document, expected_project_id=PROJECT_ID)
    assert error.value.code == code


def test_task_cycle_is_still_rejected() -> None:
    document = _document(tasks=[
        {"id": "P0-01", "milestone_id": "P0", "title": "A", "description": "A", "status": "completed", "dependencies": [], "acceptance_criteria": ["test"]},
        {"id": "P1-01", "milestone_id": "P1", "title": "B", "description": "B", "status": "pending", "dependencies": ["P7-01"], "acceptance_criteria": ["test"]},
        {"id": "P7-01", "milestone_id": "P7", "title": "C", "description": "C", "status": "pending", "dependencies": ["P1-01"], "acceptance_criteria": ["test"]},
    ])
    with pytest.raises(ProjectCatalogError) as error:
        validate_project_plan(document, expected_project_id=PROJECT_ID)
    assert error.value.code == "plan_dependency_cycle"


def test_import_status_transitions_and_all_prerequisites_are_enforced(tmp_path: Path) -> None:
    catalog, memory, coordinator, project, plan = _fixture(tmp_path)
    state = memory.read_state()
    assert state.execution["gate_statuses"] == {"G0": "pending"}
    assert state.execution["open_question_statuses"] == {"OQ-001": "open"}
    assert available_project_tasks(plan, state) == ()
    assert resolve_next_action(project, plan, state).code == "GATE_REQUIRED"
    assert project_health(state, plan) == "BLOCKED"
    with pytest.raises(ProjectExecutionError) as blocked:
        coordinator.start(PROJECT_ID, task_id="P1-01", expected_repo_head_before=HEAD_A)
    assert blocked.value.code == "execution_prerequisites_blocked"
    assert blocked.value.details == {"task_id": "P1-01", "blocking_dependencies": [{"id": "G0", "kind": "gate", "status": "pending"}]}

    memory.pass_gate("G0")
    state = memory.read_state()
    assert state.execution["gate_statuses"] == {"G0": "passed"}
    assert [task.task_id for task in available_project_tasks(plan, state)] == ["P1-01"]
    assert resolve_next_action(project, plan, state).code == "CONTINUE_TASK"
    first = coordinator.start(PROJECT_ID, task_id="P1-01", expected_repo_head_before=HEAD_A)
    _record_pass(coordinator, PROJECT_ID, first, HEAD_A, HEAD_B)
    second = coordinator.start(PROJECT_ID, task_id="P7-01", expected_repo_head_before=HEAD_B)
    _record_pass(coordinator, PROJECT_ID, second, HEAD_B, HEAD_A)
    assert available_project_tasks(plan, memory.read_state()) == ()
    action = resolve_next_action(project, plan, memory.read_state())
    assert action.code == "OPEN_QUESTION_REQUIRED"
    assert "OQ-001" in action.detail
    memory.resolve_open_question("OQ-001")
    assert [task.task_id for task in available_project_tasks(plan, memory.read_state())] == ["P7-02"]
    assert memory.read_state().execution["task_statuses"]["P1-01"] == "completed"
    events = memory.read_state().events
    assert any(event.event_type == "GATE_PASSED" and event.prerequisite_id == "G0" and event.prerequisite_kind == "gate" for event in events)
    assert any(event.event_type == "OPEN_QUESTION_RESOLVED" and event.prerequisite_id == "OQ-001" and event.prerequisite_kind == "open_question" for event in events)
    memory.reopen_gate("G0")
    memory.reopen_open_question("OQ-001")
    reopened = memory.read_state()
    assert reopened.execution["gate_statuses"] == {"G0": "pending"}
    assert reopened.execution["open_question_statuses"] == {"OQ-001": "open"}
    assert any(event.event_type == "GATE_REOPENED" and event.prerequisite_id == "G0" for event in reopened.events)
    assert any(event.event_type == "OPEN_QUESTION_REOPENED" and event.prerequisite_id == "OQ-001" for event in reopened.events)


def test_workflow_exposes_project_scoped_prerequisite_actions(tmp_path: Path) -> None:
    catalog, memory, _coordinator, _project, _plan = _fixture(tmp_path)
    workflow = ProjectWorkflow(catalog.runtime_root, catalog=catalog)
    assert workflow.pass_gate(PROJECT_ID, "G0") == "passed"
    assert workflow.resolve_open_question(PROJECT_ID, "OQ-001") == "resolved"
    assert memory.read_state().execution["gate_statuses"]["G0"] == "passed"
    assert memory.read_state().execution["open_question_statuses"]["OQ-001"] == "resolved"
    with pytest.raises(ProjectWorkflowError) as error:
        workflow.pass_gate(PROJECT_ID, "missing-gate")
    assert getattr(error.value, "code", None) == "prerequisite_not_found"


def test_missing_runtime_prerequisite_entries_fail_closed(tmp_path: Path) -> None:
    _catalog, memory, _coordinator, project, plan = _fixture(tmp_path)
    state = replace(memory.read_state(), execution={"task_statuses": {"P0-01": "completed"}})
    assert available_project_tasks(plan, state) == ()
    assert resolve_next_action(project, plan, state).code == "GATE_REQUIRED"
    assert task_prerequisite_blockers(plan, state, plan.tasks[1]) == ({"id": "G0", "kind": "gate", "status": "pending"},)


def test_plan_update_preserves_same_kind_statuses_and_drops_removed_ids(tmp_path: Path) -> None:
    _catalog, memory, _coordinator, _project, plan = _fixture(tmp_path)
    memory.pass_gate("G0")
    memory.resolve_open_question("OQ-001")
    memory.execution_transaction(lambda state: (replace(state, execution={**state.execution, "task_statuses": {"P0-01": "completed", "P1-01": "pending", "P7-01": "pending", "P7-02": "pending"}}), None))
    document = plan.to_dict()
    document["plan_version"] = 2
    document["supersedes_version"] = 1
    document["planning_context"] = {"gates": [*document["planning_context"]["gates"], {"id": "G1", "title": "New gate", "criteria": "new"}], "open_questions": [{"id": "OQ-002", "question": "New question?"}]}
    document["tasks"][-1]["dependencies"] = ["P7-01", "OQ-002"]
    candidate = validate_project_plan(document, expected_project_id=PROJECT_ID)
    preview = memory.preview_update(candidate)
    assert preview.accepted
    memory.apply_update(candidate, preview)
    execution = memory.read_state().execution
    assert execution["gate_statuses"] == {"G0": "passed", "G1": "pending"}
    assert execution["open_question_statuses"] == {"OQ-002": "open"}
    assert execution["task_statuses"] == {"P0-01": "completed", "P1-01": "pending", "P7-01": "pending", "P7-02": "pending"}
    assert task_prerequisite_blockers(candidate, memory.read_state(), candidate.tasks[-1]) == (
        {"id": "P7-01", "kind": "task", "status": "pending"},
        {"id": "OQ-002", "kind": "open_question", "status": "open"},
    )


def test_direct_persist_binding_rechecks_prerequisites(tmp_path: Path) -> None:
    _catalog, _memory, coordinator, _project, plan = _fixture(tmp_path)
    binding = ProjectExecutionBinding("binding-manual", PROJECT_ID, plan.plan_version, "P1-01", "launch-manual", "corr-manual", "command-manual", "prerequisite-fixture", HEAD_A, "2026-08-23T00:00:00Z")
    with pytest.raises(ProjectExecutionError) as error:
        coordinator.persist_binding(binding)
    assert error.value.code == "execution_prerequisites_blocked"
    assert error.value.details["task_id"] == "P1-01"
    assert error.value.details["blocking_dependencies"][0]["kind"] == "gate"


def test_work_prompt_explains_task_gate_and_question_dependencies(tmp_path: Path) -> None:
    project = new_project_record(project_id=PROJECT_ID, display_name="Prerequisite Fixture", repo_alias="prerequisite-fixture", local_repo_path=tmp_path / "repo", github_repo=None, brief=ProjectBrief("Prerequisite Fixture", "Goal", "Description", "test"))
    result = WorkPlanningPromptBuilder().build(mode="CREATE_PROJECT_PLAN", project=project, brief=project.brief, current_plan=None, planning_directive="Prepare the canonical plan.")
    assert "task.dependencies may reference an existing task, gate, or open question defined in the same canonical plan" in result.prompt


def test_legacy_task_only_plan_remains_valid() -> None:
    document = _document(context=None)
    document.pop("planning_context")
    document["tasks"][1]["dependencies"] = ["P0-01"]
    document["tasks"][-1]["dependencies"] = ["P7-01"]
    plan = validate_project_plan(document, expected_project_id=PROJECT_ID)
    assert classify_dependency_targets(plan)["P0-01"] == "task"

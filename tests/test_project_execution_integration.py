from __future__ import annotations

import json
from types import SimpleNamespace
from pathlib import Path

import pytest
from bdb_bridge.project_launch import ProjectLaunch as BrowserProjectLaunch

from bdb_vnext.project_catalog import ProjectBrief, ProjectCatalog, ProjectPlan, new_project_record, validate_project_plan
from bdb_vnext.project_execution import ProjectExecutionCoordinator, ProjectExecutionError
from bdb_vnext.project_memory import ProjectMemoryStore, project_health, resolve_next_action
from bdb_vnext.project_launch import ProjectLaunchQueueAdapter
from bdb_vnext.project_workflow import CommandResult, ProjectWorkflow


HEAD = "a" * 40


def _plan(project_id: str, version: int = 1, *, statuses: tuple[str, str, str] = ("active", "pending", "pending"), supersedes: int | None = None, all_deterministic: bool = False, include_next_milestone: bool = False) -> ProjectPlan:
    document = {
        "schema": "bdb-project-plan-v1",
        "project_id": project_id,
        "project_name": "Execution Fixture",
        "plan_version": version,
        **({"supersedes_version": supersedes} if supersedes is not None else {}),
        "milestones": [{"id": "m1", "title": "Delivery", "description": "bounded delivery", "status": "active"}] + ([{"id": "m2", "title": "Next", "description": "next milestone", "status": "pending"}] if include_next_milestone else []),
        "tasks": [
            {"id": "t1", "milestone_id": "m1", "title": "Deterministic", "description": "first", "status": statuses[0], "dependencies": [], "acceptance_criteria": ["test:fixture"]},
            {"id": "t2", "milestone_id": "m1", "title": "Review", "description": "second", "status": statuses[1], "dependencies": ["t1"], "acceptance_criteria": ["test:fixture" if all_deterministic else "manual:visual review"]},
            {"id": "t3", "milestone_id": "m1", "title": "Retry", "description": "third", "status": statuses[2], "dependencies": ["t2"], "acceptance_criteria": ["test:fixture"]},
        ],
        "current_task_id": "t1",
    }
    if include_next_milestone:
        document["tasks"].append({"id": "t4", "milestone_id": "m2", "title": "Next", "description": "next", "status": "pending", "dependencies": ["t3"], "acceptance_criteria": ["test:fixture"]})
    return validate_project_plan(document, expected_project_id=project_id)


def _fixture(tmp_path: Path, *, all_deterministic: bool = False, include_next_milestone: bool = False) -> tuple[ProjectCatalog, ProjectExecutionCoordinator, str]:
    runtime = tmp_path / "runtime"
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    project_id = "execution-fixture"
    brief = ProjectBrief("Execution Fixture", "exercise bounded execution", "fixture", "test")
    project = new_project_record(project_id=project_id, display_name="Execution Fixture", repo_alias="execution-fixture", local_repo_path=repo, github_repo=None, brief=brief)
    catalog = ProjectCatalog(runtime); catalog.upsert(project)
    memory = ProjectMemoryStore(runtime, project_id); plan = memory.ensure_initial_plan(_plan(project_id, all_deterministic=all_deterministic, include_next_milestone=include_next_milestone))
    catalog.upsert(type(project)(**{**project.__dict__, "plan_imported": True, "plan_version": plan.plan_version, "total_tasks": len(plan.tasks), "current_milestone": plan.current_milestone.title if plan.current_milestone else None, "current_task": plan.current_task_id, "plan_path": str(memory.current_pointer), "project_status": "active"}))
    coordinator = ProjectExecutionCoordinator(runtime, catalog=catalog)
    return catalog, coordinator, project_id


def _record_pass(coordinator: ProjectExecutionCoordinator, project_id: str, binding: ProjectExecutionBinding, before: str, after: str) -> None:
    coordinator.record_result(project_id, {"execution_binding_id": binding.execution_binding_id, "command_id": binding.command_id, "correlation_id": binding.correlation_id, "head_before": before, "head_after": after, "execution_status": "PASS", "validation_status": "PASS", "promotion_status": "NOT_RUN"})


def test_execution_binding_acceptance_progress_and_idempotent_replay(tmp_path: Path) -> None:
    catalog, coordinator, project_id = _fixture(tmp_path)
    binding = coordinator.start(project_id, expected_repo_head_before=HEAD)
    result = {
        "execution_binding_id": binding.execution_binding_id,
        "command_id": binding.command_id,
        "correlation_id": binding.correlation_id,
        "head_before": HEAD,
        "head_after": "b" * 40,
        "execution_status": "PASS",
        "validation_status": "PASS",
        "promotion_status": "NOT_RUN",
        "result_summary": "T1 passed",
        "evidence_refs": ["evidence:t1"],
    }
    first = coordinator.record_result(project_id, result)
    replay = coordinator.record_result(project_id, result)
    assert replay.attempt_id == first.attempt_id
    snapshot = coordinator.snapshot(project_id)
    assert len(snapshot["attempts"]) == 1
    assert snapshot["task_statuses"]["t1"] == "completed"
    assert snapshot["available_tasks"] == ["t2"]
    assert catalog.get(project_id).completed_tasks == 1


def test_milestone_auto_advances_in_plan_order_without_global_limits_and_stops_at_boundary(tmp_path: Path) -> None:
    catalog, coordinator, project_id = _fixture(tmp_path, all_deterministic=True, include_next_milestone=True)
    started = coordinator.begin_milestone_auto(project_id, milestone_id="m1", milestone_run_id="milestone-run-fixture")
    assert started["status"] == "RUNNABLE"
    assert started["current_task_id"] == "t1"
    assert started["total_tasks"] == 3

    first = coordinator.start(project_id, expected_repo_head_before=HEAD)
    _record_pass(coordinator, project_id, first, HEAD, "b" * 40)
    second_snapshot = coordinator.milestone_auto_snapshot(project_id)
    assert second_snapshot["status"] == "RUNNABLE"
    assert second_snapshot["current_task_id"] == "t2"
    assert second_snapshot["runnable_task_ids"] == ["t2"]

    second = coordinator.start(project_id, expected_repo_head_before="b" * 40)
    _record_pass(coordinator, project_id, second, "b" * 40, "c" * 40)
    third_snapshot = coordinator.milestone_auto_snapshot(project_id)
    assert third_snapshot["current_task_id"] == "t3"
    assert third_snapshot["runnable_task_ids"] == ["t3"]

    third = coordinator.start(project_id, expected_repo_head_before="c" * 40)
    _record_pass(coordinator, project_id, third, "c" * 40, "d" * 40)
    completed = coordinator.milestone_auto_snapshot(project_id)
    assert completed["status"] == "MILESTONE_COMPLETED"
    assert completed["current_task_id"] is None
    assert coordinator.snapshot(project_id)["available_tasks"] == []
    assert coordinator.snapshot(project_id)["current_task_id"] is None
    assert catalog.get(project_id).project_status == "active"

    restarted = ProjectExecutionCoordinator(catalog.runtime_root, catalog=catalog)
    assert restarted.milestone_auto_snapshot(project_id)["status"] == "MILESTONE_COMPLETED"


def test_milestone_auto_stops_for_review_and_can_resume_same_run(tmp_path: Path) -> None:
    _catalog, coordinator, project_id = _fixture(tmp_path)
    coordinator.begin_milestone_auto(project_id, milestone_id="m1", milestone_run_id="milestone-review-run")
    first = coordinator.start(project_id, expected_repo_head_before=HEAD)
    _record_pass(coordinator, project_id, first, HEAD, "b" * 40)
    review = coordinator.start(project_id, expected_repo_head_before="b" * 40)
    _record_pass(coordinator, project_id, review, "b" * 40, "b" * 40)
    waiting = coordinator.milestone_auto_snapshot(project_id)
    assert waiting["status"] == "REVIEW_REQUIRED"
    assert waiting["current_task_id"] == "t2"
    assert waiting["blocker"] == {"id": "t2", "kind": "review", "status": "review"}

    coordinator.approve_review(project_id, "t2", reason="visual review passed")
    resumed = coordinator.milestone_auto_snapshot(project_id)
    assert resumed["status"] == "RUNNABLE"
    assert resumed["current_task_id"] == "t3"
    stopped = coordinator.stop_milestone_auto(project_id, run_id="milestone-review-run", reason="user_pause")
    assert stopped["status"] == "STOPPED"
    resumed_again = coordinator.begin_milestone_auto(project_id, milestone_id="m1")
    assert resumed_again["status"] == "RUNNABLE"
    assert resumed_again["current_task_id"] == "t3"
    assert resumed_again["milestone_run_id"] == "milestone-review-run"


def test_manual_review_and_retry_then_project_completion(tmp_path: Path) -> None:
    catalog, coordinator, project_id = _fixture(tmp_path)
    first = coordinator.start(project_id, expected_repo_head_before=HEAD)
    coordinator.record_result(project_id, {"execution_binding_id": first.execution_binding_id, "command_id": first.command_id, "correlation_id": first.correlation_id, "head_before": HEAD, "head_after": "b" * 40, "execution_status": "PASS", "validation_status": "PASS", "promotion_status": "NOT_RUN"})
    second = coordinator.start(project_id, task_id="t2", expected_repo_head_before="b" * 40)
    coordinator.record_result(project_id, {"execution_binding_id": second.execution_binding_id, "command_id": second.command_id, "correlation_id": second.correlation_id, "head_before": "b" * 40, "head_after": "b" * 40, "execution_status": "PASS", "validation_status": "PASS", "promotion_status": "NOT_RUN"})
    assert coordinator.snapshot(project_id)["task_statuses"]["t2"] == "review"
    coordinator.approve_review(project_id, "t2", reason="manual visual review passed")
    third = coordinator.start(project_id, task_id="t3", expected_repo_head_before="b" * 40)
    coordinator.record_result(project_id, {"execution_binding_id": third.execution_binding_id, "command_id": third.command_id, "correlation_id": third.correlation_id, "head_before": "b" * 40, "head_after": "c" * 40, "execution_status": "FAIL", "validation_status": "FAIL", "promotion_status": "NOT_RUN", "failure_code": "VALIDATION_FAILED"})
    assert coordinator.snapshot(project_id)["task_statuses"]["t3"] == "blocked"
    retry = coordinator.start(project_id, task_id="t3", expected_repo_head_before="b" * 40)
    coordinator.record_result(project_id, {"execution_binding_id": retry.execution_binding_id, "command_id": retry.command_id, "correlation_id": retry.correlation_id, "head_before": "b" * 40, "head_after": "d" * 40, "execution_status": "PASS", "validation_status": "PASS", "promotion_status": "NOT_RUN"})
    assert coordinator.snapshot(project_id)["task_statuses"]["t3"] == "completed"
    assert coordinator.snapshot(project_id)["available_tasks"] == []
    assert catalog.get(project_id).project_status == "completed"


def test_stale_plan_result_is_durable_and_fail_closed(tmp_path: Path) -> None:
    catalog, coordinator, project_id = _fixture(tmp_path)
    binding = coordinator.start(project_id, expected_repo_head_before=HEAD)
    memory = ProjectMemoryStore(catalog.runtime_root, project_id)
    plan2 = _plan(project_id, version=2, supersedes=1, statuses=("active", "pending", "pending"))
    preview = memory.preview_update(plan2)
    assert preview.accepted
    memory.apply_update(plan2, preview)
    with pytest.raises(ProjectExecutionError) as error:
        coordinator.record_result(project_id, {"execution_binding_id": binding.execution_binding_id, "command_id": binding.command_id, "correlation_id": binding.correlation_id, "head_before": HEAD, "head_after": "b" * 40, "execution_status": "PASS", "validation_status": "PASS", "promotion_status": "NOT_RUN"})
    assert error.value.code == "STALE_RESULT"
    snapshot = coordinator.snapshot(project_id)
    assert snapshot["stale_result"] is True
    assert snapshot["task_statuses"]["t1"] == "active"
    assert project_health(memory.read_state(), plan2) == "BLOCKED"


def test_wrong_subject_or_head_result_is_rejected_without_completion(tmp_path: Path) -> None:
    _catalog, coordinator, project_id = _fixture(tmp_path)
    binding = coordinator.start(project_id, expected_repo_head_before=HEAD)
    with pytest.raises(ProjectExecutionError) as subject_error:
        coordinator.record_result(project_id, {"execution_binding_id": binding.execution_binding_id, "command_id": binding.command_id, "correlation_id": binding.correlation_id, "project_id": "other-project", "task_id": binding.task_id, "plan_version": binding.plan_version, "head_before": HEAD, "execution_status": "PASS", "validation_status": "PASS"})
    assert subject_error.value.code == "STALE_RESULT"
    with pytest.raises(ProjectExecutionError) as head_error:
        coordinator.record_result(project_id, {"execution_binding_id": binding.execution_binding_id, "command_id": binding.command_id, "correlation_id": binding.correlation_id, "head_before": "c" * 40, "execution_status": "PASS", "validation_status": "PASS"})
    assert head_error.value.code == "STALE_RESULT"
    assert coordinator.snapshot(project_id)["task_statuses"]["t1"] == "active"


def test_stale_result_replay_remains_fail_closed(tmp_path: Path) -> None:
    _catalog, coordinator, project_id = _fixture(tmp_path)
    binding = coordinator.start(project_id, expected_repo_head_before=HEAD)
    stale = {"execution_binding_id": binding.execution_binding_id, "command_id": binding.command_id, "correlation_id": binding.correlation_id, "head_before": "c" * 40, "execution_status": "PASS", "validation_status": "PASS"}
    with pytest.raises(ProjectExecutionError) as first:
        coordinator.record_result(project_id, stale)
    with pytest.raises(ProjectExecutionError) as replay:
        coordinator.record_result(project_id, stale)
    assert first.value.code == replay.value.code == "STALE_RESULT"
    assert len(coordinator.snapshot(project_id)["attempts"]) == 1


def test_bdb_finalization_adapter_keeps_candidate_evidence_publication_lineage(tmp_path: Path) -> None:
    catalog, coordinator, project_id = _fixture(tmp_path)
    binding = coordinator.start(project_id, expected_repo_head_before=HEAD)
    finalization = SimpleNamespace(
        candidate=SimpleNamespace(candidate_id="candidate:fixture", work_id="work:fixture", task_id="t1", state="SEALED"),
        candidate_view=SimpleNamespace(view_id="candidate-view:fixture", candidate_tree_digest="tree:after", base_commit_oid="b" * 40, task_id="t1"),
        validation=SimpleNamespace(validation_id="validation:fixture", evidence_id="evidence:fixture", result=SimpleNamespace(status="PASS")),
        evaluation=SimpleNamespace(evaluation_id="evaluation:fixture"),
        publication=SimpleNamespace(publication_id="publication:fixture"),
    )
    attempt = coordinator.record_bdb_finalization(project_id, binding, finalization)
    snapshot = coordinator.snapshot(project_id)
    assert attempt.result_status == "PASS"
    assert snapshot["task_statuses"]["t1"] == "completed"
    refs = snapshot["attempts"][0]["canonical_refs"]
    assert refs["candidate_id"] == "candidate:fixture"
    assert refs["work_id"] == "work:fixture"
    assert refs["base_commit_oid"] == "b" * 40
    assert refs["evidence_id"] == "evidence:fixture"
    assert refs["publication_id"] == "publication:fixture"


def test_bad_plan_v3_completed_downgrade_is_rejected(tmp_path: Path) -> None:
    catalog, coordinator, project_id = _fixture(tmp_path)
    binding = coordinator.start(project_id, expected_repo_head_before=HEAD)
    coordinator.record_result(project_id, {"execution_binding_id": binding.execution_binding_id, "command_id": binding.command_id, "correlation_id": binding.correlation_id, "head_before": HEAD, "head_after": "b" * 40, "execution_status": "PASS", "validation_status": "PASS", "promotion_status": "NOT_RUN"})
    memory = ProjectMemoryStore(catalog.runtime_root, project_id)
    bad = _plan(project_id, version=2, supersedes=1, statuses=("pending", "pending", "pending"))
    preview = memory.preview_update(bad)
    assert not preview.accepted
    assert any("completed_task_downgrade:t1" == item for item in preview.completed_protection)


def test_plan_v2_preview_accepts_future_change_after_execution_progress(tmp_path: Path) -> None:
    catalog, coordinator, project_id = _fixture(tmp_path)
    binding = coordinator.start(project_id, expected_repo_head_before=HEAD)
    coordinator.record_result(project_id, {"execution_binding_id": binding.execution_binding_id, "command_id": binding.command_id, "correlation_id": binding.correlation_id, "head_before": HEAD, "head_after": "b" * 40, "execution_status": "PASS", "validation_status": "PASS", "promotion_status": "NOT_RUN"})
    memory = ProjectMemoryStore(catalog.runtime_root, project_id)
    document = _plan(project_id, version=2, supersedes=1, statuses=("completed", "active", "pending")).to_dict()
    document["tasks"].append({"id": "t4", "milestone_id": "m1", "title": "Future", "description": "future", "status": "pending", "dependencies": ["t1"], "acceptance_criteria": ["test:fixture"]})
    candidate = validate_project_plan(document, expected_project_id=project_id)
    preview = memory.preview_update(candidate)
    assert preview.accepted is True
    memory.apply_update(candidate, preview)
    assert coordinator.snapshot(project_id)["plan_version"] == "2"


def test_execution_state_survives_restart_and_handoff_contains_binding(tmp_path: Path) -> None:
    catalog, coordinator, project_id = _fixture(tmp_path)
    binding = coordinator.start(project_id, expected_repo_head_before=HEAD)
    restarted = ProjectExecutionCoordinator(catalog.runtime_root, catalog=catalog)
    assert restarted.snapshot(project_id)["bindings"][0]["execution_binding_id"] == binding.execution_binding_id
    state = ProjectMemoryStore(catalog.runtime_root, project_id).read_state()
    assert any(event.event_type == "EXECUTION_STARTED" for event in state.events)


def test_continue_uses_dag_current_task_after_previous_task_completed(tmp_path: Path) -> None:
    catalog, coordinator, project_id = _fixture(tmp_path)
    binding = coordinator.start(project_id, expected_repo_head_before=HEAD)
    coordinator.record_result(project_id, {"execution_binding_id": binding.execution_binding_id, "command_id": binding.command_id, "correlation_id": binding.correlation_id, "head_before": HEAD, "head_after": "b" * 40, "execution_status": "PASS", "validation_status": "PASS", "promotion_status": "NOT_RUN"})

    class Runner:
        def run(self, args, *, cwd=None, timeout_seconds=120.0):
            return CommandResult(tuple(args), 0, ("b" * 40) + "\n", "")

    queue = ProjectLaunchQueueAdapter(tmp_path / "continue-launch.json")
    workflow = ProjectWorkflow(catalog.runtime_root, catalog=catalog, command_runner=Runner(), queue=queue)
    launch = workflow.queue_continue_prompt(project_id)
    assert launch.task_id == "t2"
    assert "Aktualne zadanie: t2" in launch.prompt


def test_start_prompt_carries_exact_binding_and_no_auto_send(tmp_path: Path) -> None:
    catalog, _coordinator, project_id = _fixture(tmp_path)

    class Runner:
        def run(self, args, *, cwd=None, timeout_seconds=120.0):
            return CommandResult(tuple(args), 0, HEAD + "\n", "")

    queue = ProjectLaunchQueueAdapter(tmp_path / "launch.json")
    workflow = ProjectWorkflow(catalog.runtime_root, catalog=catalog, command_runner=Runner(), queue=queue)
    launch = workflow.queue_start_prompt(project_id)
    assert launch.auto_send is False
    assert launch.project_id == project_id
    assert launch.task_id == "t1"
    assert launch.execution_binding_id
    assert launch.expected_repo_head_before == HEAD
    assert "Acceptance criteria" in launch.prompt
    assert "naciśnij" not in launch.prompt.casefold()
    assert workflow.execution.snapshot(project_id)["bindings"][0]["launch_id"] == launch.launch_id
    replay = workflow.queue_start_prompt(project_id)
    assert replay.launch_id == launch.launch_id
    assert len(workflow.execution.snapshot(project_id)["bindings"]) == 1


def test_start_prompt_launch_is_compatible_with_browser_native_contract(tmp_path: Path) -> None:
    catalog, _coordinator, project_id = _fixture(tmp_path)

    class Runner:
        def run(self, args, *, cwd=None, timeout_seconds=120.0):
            return CommandResult(tuple(args), 0, HEAD + "\n", "")

    queue_path = tmp_path / "launch.json"
    queue = ProjectLaunchQueueAdapter(queue_path)
    workflow = ProjectWorkflow(catalog.runtime_root, catalog=catalog, command_runner=Runner(), queue=queue)
    launch = workflow.queue_start_prompt(project_id)

    document = json.loads(queue_path.read_text(encoding="utf-8"))
    browser_launch = BrowserProjectLaunch.from_dict(document["pending"])
    assert browser_launch.launch_id == launch.launch_id
    assert browser_launch.auto_send is False
    assert document["pending"]["project_id"] == project_id
    assert document["pending"]["task_id"] == launch.task_id

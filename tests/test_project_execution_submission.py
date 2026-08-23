from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import uuid

import pytest

from bdb_vnext.project_execution import (
    ProjectExecutionError,
    ProjectExecutionSubmission,
)
from bdb_vnext.project_workflow import CommandResult, ProjectWorkflow
from bdb_vnext.composition import BROWSER_EXTENSION_ID, PROTOCOL_GENERATION
from bdb_vnext.m9b_native_host import M9B_NATIVE_REQUEST_SCHEMA, VNextNativeConfig, handle_message

from test_project_execution_integration import HEAD, _fixture


def _result(project_id: str, binding, *, head_before: str = HEAD, head_after: str = "b" * 40) -> dict[str, object]:
    return {
        "schema": "bdb-project-execution-submission-v1",
        "project_id": project_id,
        "plan_version": "1",
        "task_id": binding.task_id,
        "execution_binding_id": binding.execution_binding_id,
        "correlation_id": binding.correlation_id,
        "command_id": binding.command_id,
        "repo_alias": "execution-fixture",
        "head_before": head_before,
        "head_after": head_after,
        "execution_status": "PASS",
        "validation_status": "PASS",
        "promotion_status": "NOT_RUN",
        "result_summary": "bounded task passed",
        "evidence_refs": ["evidence:fixture"],
        "criteria": [{"criterion": "test:fixture", "type": "DETERMINISTIC", "status": "PASS", "evidence_ref": "evidence:fixture"}],
    }


def test_project_execution_submission_is_strict_json_contract() -> None:
    binding = type("B", (), {"task_id": "t1", "execution_binding_id": "binding-1", "correlation_id": "corr-1", "command_id": "command-1"})()
    parsed = ProjectExecutionSubmission.from_mapping(_result("execution-fixture", binding))
    assert parsed.to_dict()["schema"] == "bdb-project-execution-submission-v1"
    with pytest.raises(ProjectExecutionError):
        ProjectExecutionSubmission.from_mapping({"schema": "bdb-project-execution-submission-v1", "yaml": "BDB_SUBMISSION:"})
    with pytest.raises(ProjectExecutionError):
        ProjectExecutionSubmission.from_mapping({**parsed.to_dict(), "unexpected": True})
    with_refs = ProjectExecutionSubmission.from_mapping({**parsed.to_dict(), "canonical_refs": {"candidate_id": "candidate-1", "evidence_id": None}})
    assert with_refs.to_dict()["canonical_refs"] == {"candidate_id": "candidate-1", "evidence_id": None}
    with pytest.raises(ProjectExecutionError):
        ProjectExecutionSubmission.from_mapping({**parsed.to_dict(), "canonical_refs": {"unexpected": "ref"}})
    with pytest.raises(ProjectExecutionError):
        ProjectExecutionSubmission.from_mapping({key: value for key, value in parsed.to_dict().items() if key != "criteria"})
    with pytest.raises(ProjectExecutionError):
        ProjectExecutionSubmission.from_mapping({**parsed.to_dict(), "repo_alias": "Not-An-Alias"})


def test_project_execution_auto_accepts_once_and_queues_next_task(tmp_path: Path) -> None:
    catalog, coordinator, project_id = _fixture(tmp_path, all_deterministic=True)
    coordinator.begin_milestone_auto(project_id, milestone_id="m1", milestone_run_id="milestone-run-submit")

    class Runner:
        def run(self, args, *, cwd=None, timeout_seconds=120.0):
            return CommandResult(tuple(args), 0, HEAD + "\n", "")

    queue = tmp_path / "launch.json"
    from bdb_vnext.project_launch import ProjectLaunchQueueAdapter

    queue_adapter = ProjectLaunchQueueAdapter(queue)
    workflow = ProjectWorkflow(catalog.runtime_root, catalog=catalog, command_runner=Runner(), queue=queue_adapter)
    launch = workflow.queue_continue_prompt(project_id)
    assert launch.auto_send is True
    assert queue_adapter.peek() is not None and queue_adapter.peek().auto_send is True
    claim_id = str(uuid.uuid4())
    assert queue_adapter.claim(launch_id=launch.launch_id, claim_id=claim_id) == launch
    assert queue_adapter.acknowledge(launch_id=launch.launch_id, claim_id=claim_id) is True
    binding = coordinator.binding(project_id, launch.execution_binding_id)
    coordinator.bind_conversation(project_id, binding.execution_binding_id, "chatgpt-conversation-1")
    receipt = workflow.submit_project_execution_result(_result(project_id, binding), conversation_id="chatgpt-conversation-1", launch_id=launch.launch_id)
    assert receipt["accepted"] is True
    assert receipt["replayed"] is False
    assert receipt["task_id"] == "t1"
    assert receipt["current_task_id"] == "t2"
    assert receipt["milestone_run_id"] == "milestone-run-submit"
    assert receipt["next_launch"]["task_id"] == "t2"

    replay = workflow.submit_project_execution_result(_result(project_id, binding), conversation_id="chatgpt-conversation-1", launch_id=launch.launch_id)
    assert replay["replayed"] is True
    assert len(coordinator.snapshot(project_id)["attempts"]) == 1
    assert len(coordinator.snapshot(project_id)["bindings"]) == 2


def test_native_project_execution_status_exposes_only_current_auto_gate(tmp_path: Path) -> None:
    catalog, coordinator, project_id = _fixture(tmp_path, all_deterministic=True)
    coordinator.begin_milestone_auto(project_id, milestone_id="m1", milestone_run_id="milestone-run-status")

    class Runner:
        def run(self, args, *, cwd=None, timeout_seconds=120.0):
            return CommandResult(tuple(args), 0, HEAD + "\n", "")

    from bdb_vnext.project_launch import ProjectLaunchQueueAdapter

    workflow = ProjectWorkflow(catalog.runtime_root, catalog=catalog, command_runner=Runner(), queue=ProjectLaunchQueueAdapter(tmp_path / "status-launch.json"))
    launch = workflow.queue_continue_prompt(project_id)
    binding = coordinator.binding(project_id, launch.execution_binding_id)
    coordinator.bind_conversation(project_id, binding.execution_binding_id, "chatgpt-conversation-status")
    config = VNextNativeConfig(runtime_root=catalog.runtime_root, legacy_runtime_root=tmp_path / "legacy", bootstrap_authority_root=tmp_path / "bootstrap")
    response = handle_message(config, {
        "schema": M9B_NATIVE_REQUEST_SCHEMA,
        "request_id": "native-project-status-1",
        "action": "project_execution_status",
        "protocol_generation": PROTOCOL_GENERATION,
        "browser_extension_id": BROWSER_EXTENSION_ID,
        "conversation_id": "chatgpt-conversation-status",
        "project_id": project_id,
        "execution_binding_id": binding.execution_binding_id,
    })
    assert response["status"] == "project_execution_status"
    assert response["current_binding_id"] == binding.execution_binding_id
    assert response["current_task_id"] == binding.task_id
    assert response["binding"]["conversation_id"] == "chatgpt-conversation-status"
    assert response["milestone_auto"]["status"] == "RUNNABLE"
    assert response["milestone_auto"]["milestone_run_id"] == "milestone-run-status"

    coordinator.stop_milestone_auto(project_id, run_id="milestone-run-status", reason="test_stop")
    stopped = handle_message(config, {
        "schema": M9B_NATIVE_REQUEST_SCHEMA,
        "request_id": "native-project-status-2",
        "action": "project_execution_status",
        "protocol_generation": PROTOCOL_GENERATION,
        "browser_extension_id": BROWSER_EXTENSION_ID,
        "conversation_id": "chatgpt-conversation-status",
        "project_id": project_id,
        "execution_binding_id": binding.execution_binding_id,
    })
    assert stopped["milestone_auto"]["status"] == "STOPPED"


def test_project_execution_wrong_conversation_and_stale_binding_fail_closed(tmp_path: Path) -> None:
    catalog, coordinator, project_id = _fixture(tmp_path)
    binding = coordinator.start(project_id, expected_repo_head_before=HEAD)
    coordinator.bind_conversation(project_id, binding.execution_binding_id, "chatgpt-conversation-1")

    class Runner:
        def run(self, args, *, cwd=None, timeout_seconds=120.0):
            return CommandResult(tuple(args), 0, HEAD + "\n", "")

    from bdb_vnext.project_launch import ProjectLaunchQueueAdapter

    workflow = ProjectWorkflow(catalog.runtime_root, catalog=catalog, command_runner=Runner(), queue=ProjectLaunchQueueAdapter(tmp_path / "launch.json"))
    with pytest.raises(Exception) as error:
        workflow.submit_project_execution_result(_result(project_id, binding), conversation_id="chatgpt-conversation-2", launch_id=binding.launch_id)
    assert getattr(error.value, "code", None) == "execution_conversation_mismatch"
    stale = dict(_result(project_id, binding)); stale["head_before"] = "c" * 40
    with pytest.raises(Exception) as stale_error:
        workflow.submit_project_execution_result(stale, conversation_id="chatgpt-conversation-1", launch_id=binding.launch_id)
    assert getattr(stale_error.value, "code", None) == "STALE_RESULT"


def test_legacy_binding_recovery_binds_current_conversation_once(tmp_path: Path) -> None:
    catalog, coordinator, project_id = _fixture(tmp_path)
    binding = coordinator.start(project_id, expected_repo_head_before=HEAD)

    class Runner:
        def run(self, args, *, cwd=None, timeout_seconds=120.0):
            return CommandResult(tuple(args), 0, HEAD + "\n", "")

    from bdb_vnext.project_launch import ProjectLaunchQueueAdapter

    workflow = ProjectWorkflow(catalog.runtime_root, catalog=catalog, command_runner=Runner(), queue=ProjectLaunchQueueAdapter(tmp_path / "legacy-launch.json"))
    receipt = workflow.submit_project_execution_result(_result(project_id, binding), conversation_id="chatgpt-conversation-1", launch_id=binding.launch_id)
    assert receipt["accepted"] is True
    assert coordinator.binding(project_id, binding.execution_binding_id).conversation_id == "chatgpt-conversation-1"


def test_watchdog_distinguishes_external_wait_from_stall_and_resume_keeps_binding(tmp_path: Path) -> None:
    _catalog, coordinator, project_id = _fixture(tmp_path)
    binding = coordinator.start(project_id, expected_repo_head_before=HEAD)
    old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    coordinator.record_checkpoint(project_id, binding.execution_binding_id, status="WAITING_EXTERNAL", progress_summary="CI is running", external_reference="run-123", last_progress_at=old)
    waiting = coordinator.watchdog(project_id, now=datetime.now(timezone.utc))
    assert waiting["state"] == "WAITING_EXTERNAL"
    assert waiting["resume_available"] is False
    coordinator.record_checkpoint(project_id, binding.execution_binding_id, status="ACTIVE", progress_summary="no new output", last_progress_at=old)
    stalled = coordinator.watchdog(project_id, now=datetime.now(timezone.utc))
    assert stalled["state"] == "STALLED"
    assert stalled["resume_available"] is True
    assert coordinator.resume_binding(project_id, binding.execution_binding_id).execution_binding_id == binding.execution_binding_id


def test_native_project_execution_submit_uses_canonical_coordinator_and_replays(tmp_path: Path) -> None:
    catalog, coordinator, project_id = _fixture(tmp_path)
    binding = coordinator.start(project_id, expected_repo_head_before=HEAD)
    coordinator.bind_conversation(project_id, binding.execution_binding_id, "chatgpt-conversation-1")
    config = VNextNativeConfig(runtime_root=catalog.runtime_root, legacy_runtime_root=tmp_path / "legacy", bootstrap_authority_root=tmp_path / "bootstrap")
    message = {
        "schema": M9B_NATIVE_REQUEST_SCHEMA,
        "request_id": "native-project-result-1",
        "action": "project_execution_submit",
        "protocol_generation": PROTOCOL_GENERATION,
        "browser_extension_id": BROWSER_EXTENSION_ID,
        "conversation_id": "chatgpt-conversation-1",
        "launch_id": binding.launch_id,
        "result": _result(project_id, binding),
    }
    first = handle_message(config, message)
    assert first["status"] == "project_execution"
    assert first["receipt"]["accepted"] is True
    replay = handle_message(config, {**message, "request_id": "native-project-result-2"})
    assert replay["receipt"]["replayed"] is True
    assert len(coordinator.snapshot(project_id)["attempts"]) == 1


def test_native_project_execution_submit_recovers_canonical_binding_without_launch_hint(tmp_path: Path) -> None:
    catalog, coordinator, project_id = _fixture(tmp_path)
    binding = coordinator.start(project_id, expected_repo_head_before=HEAD)
    coordinator.bind_conversation(project_id, binding.execution_binding_id, "chatgpt-conversation-1")
    config = VNextNativeConfig(runtime_root=catalog.runtime_root, legacy_runtime_root=tmp_path / "legacy", bootstrap_authority_root=tmp_path / "bootstrap")
    message = {
        "schema": M9B_NATIVE_REQUEST_SCHEMA,
        "request_id": "native-project-result-recovery",
        "action": "project_execution_submit",
        "protocol_generation": PROTOCOL_GENERATION,
        "browser_extension_id": BROWSER_EXTENSION_ID,
        "conversation_id": "chatgpt-conversation-1",
        "result": _result(project_id, binding),
    }
    response = handle_message(config, message)
    assert response["status"] == "project_execution"
    assert response["receipt"]["accepted"] is True
    assert len(coordinator.snapshot(project_id)["attempts"]) == 1


def test_native_project_execution_submit_rejects_mismatched_launch_hint(tmp_path: Path) -> None:
    catalog, coordinator, project_id = _fixture(tmp_path)
    binding = coordinator.start(project_id, expected_repo_head_before=HEAD)
    coordinator.bind_conversation(project_id, binding.execution_binding_id, "chatgpt-conversation-1")
    config = VNextNativeConfig(runtime_root=catalog.runtime_root, legacy_runtime_root=tmp_path / "legacy", bootstrap_authority_root=tmp_path / "bootstrap")
    message = {
        "schema": M9B_NATIVE_REQUEST_SCHEMA,
        "request_id": "native-project-result-mismatch",
        "action": "project_execution_submit",
        "protocol_generation": PROTOCOL_GENERATION,
        "browser_extension_id": BROWSER_EXTENSION_ID,
        "conversation_id": "chatgpt-conversation-1",
        "launch_id": "foreign-launch-id",
        "result": _result(project_id, binding),
    }
    with pytest.raises(Exception) as error:
        handle_message(config, message)
    assert getattr(error.value, "code", None) == "execution_launch_mismatch"
    assert len(coordinator.snapshot(project_id)["attempts"]) == 0


def test_native_project_execution_submit_missing_canonical_binding_fails_closed(tmp_path: Path) -> None:
    catalog, _coordinator, project_id = _fixture(tmp_path)
    binding = type("B", (), {"task_id": "t1", "execution_binding_id": "missing-binding", "correlation_id": "corr-1", "command_id": "command-1"})()
    config = VNextNativeConfig(runtime_root=catalog.runtime_root, legacy_runtime_root=tmp_path / "legacy", bootstrap_authority_root=tmp_path / "bootstrap")
    message = {
        "schema": M9B_NATIVE_REQUEST_SCHEMA,
        "request_id": "native-project-result-missing",
        "action": "project_execution_submit",
        "protocol_generation": PROTOCOL_GENERATION,
        "browser_extension_id": BROWSER_EXTENSION_ID,
        "conversation_id": "chatgpt-conversation-1",
        "result": _result(project_id, binding),
    }
    with pytest.raises(Exception) as error:
        handle_message(config, message)
    assert getattr(error.value, "code", None) == "execution_binding_not_found"


def test_native_project_execution_submit_rejects_binding_owned_by_other_conversation(tmp_path: Path) -> None:
    catalog, coordinator, project_id = _fixture(tmp_path)
    binding = coordinator.start(project_id, expected_repo_head_before=HEAD)
    coordinator.bind_conversation(project_id, binding.execution_binding_id, "chatgpt-conversation-owner")
    config = VNextNativeConfig(runtime_root=catalog.runtime_root, legacy_runtime_root=tmp_path / "legacy", bootstrap_authority_root=tmp_path / "bootstrap")
    message = {
        "schema": M9B_NATIVE_REQUEST_SCHEMA,
        "request_id": "native-project-result-conversation",
        "action": "project_execution_submit",
        "protocol_generation": PROTOCOL_GENERATION,
        "browser_extension_id": BROWSER_EXTENSION_ID,
        "conversation_id": "chatgpt-conversation-other",
        "result": _result(project_id, binding),
    }
    with pytest.raises(Exception) as error:
        handle_message(config, message)
    assert getattr(error.value, "code", None) == "execution_conversation_mismatch"
    assert len(coordinator.snapshot(project_id)["attempts"]) == 0


def test_execution_prompt_requires_one_versioned_json_result_and_cost_aware_policy(tmp_path: Path) -> None:
    catalog, coordinator, project_id = _fixture(tmp_path)

    class Runner:
        def run(self, args, *, cwd=None, timeout_seconds=120.0):
            return CommandResult(tuple(args), 0, HEAD + "\n", "")

    from bdb_vnext.project_launch import ProjectLaunchQueueAdapter

    workflow = ProjectWorkflow(catalog.runtime_root, catalog=catalog, command_runner=Runner(), queue=ProjectLaunchQueueAdapter(tmp_path / "prompt.json"))
    launch = workflow.queue_start_prompt(project_id)
    binding = coordinator.binding(project_id, launch.execution_binding_id)
    assert "bdb-project-execution-submission-v1" in launch.prompt
    assert "dokładnie jeden blok JSON" in launch.prompt
    assert "Nie YAML" in launch.prompt
    assert "lokalnie" in launch.prompt
    assert "Nie ma globalnego limitu czasu taska" in launch.prompt
    assert "trzech kolejnych status polls" in launch.prompt
    assert launch.execution_binding_id in launch.prompt
    assert binding.task_id in launch.prompt
    assert binding.correlation_id in launch.prompt
    assert binding.command_id in launch.prompt

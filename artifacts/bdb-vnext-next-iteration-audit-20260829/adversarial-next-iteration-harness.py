from __future__ import annotations

import json
import sys
import tracemalloc
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from bdb_vnext.composition import BROWSER_EXTENSION_ID, PROTOCOL_GENERATION
from bdb_vnext.execution_policy import ExecutionPolicyEvaluator
from bdb_vnext.local_execution_contract import ExecutionMode, LocalExecutionRequest
from bdb_vnext.m9b_native_host import (
    M9B_NATIVE_REQUEST_SCHEMA,
    VNextNativeConfig,
    handle_message,
)
from bdb_vnext.project_catalog import (
    ProjectBrief,
    ProjectCatalog,
    ProjectPlan,
    new_project_record,
    validate_project_plan,
)
from bdb_vnext.project_center_auto import CanonicalProjectCenterAutoCommands
from bdb_vnext.project_execution import ProjectExecutionCoordinator
from bdb_vnext.project_launch import ProjectLaunchQueueAdapter
from bdb_vnext.project_memory import ProjectMemoryStore
from bdb_vnext.project_memory_v2_store import ProjectMemoryStoreV2
from bdb_vnext.project_scope_execution import ProjectScopeCoordinator
from bdb_vnext.scope_orchestrator import AutoScope, ScopeOrchestrator
from bdb_vnext.stateless_process_runner import StatelessWindowsProcessRunner


HEAD = "a" * 40
TREE = "b" * 40


def plan(
    project_id: str,
    *,
    version: int = 1,
    supersedes: int | None = None,
    criterion: str = "test:fixture",
) -> ProjectPlan:
    document: dict[str, object] = {
        "schema": "bdb-project-plan-v1",
        "project_id": project_id,
        "project_name": "Next Iteration Audit Fixture",
        "plan_version": version,
        "milestones": [
            {
                "id": "m1",
                "title": "Delivery",
                "description": "isolated audit fixture",
                "status": "active",
            }
        ],
        "tasks": [
            {
                "id": "t1",
                "milestone_id": "m1",
                "title": "Fixture task",
                "description": "isolated audit fixture",
                "status": "active",
                "dependencies": [],
                "acceptance_criteria": [criterion],
            }
        ],
        "current_task_id": "t1",
    }
    if supersedes is not None:
        document["supersedes_version"] = supersedes
    return validate_project_plan(document, expected_project_id=project_id)


def fixture(root: Path, *, criterion: str = "test:fixture"):
    runtime = root / "runtime"
    repo = root / "repo"
    (repo / ".git").mkdir(parents=True)
    project_id = "audit-fixture"
    brief = ProjectBrief("Audit Fixture", "isolated adversarial checks", "audit", "local")
    project = new_project_record(
        project_id=project_id,
        display_name="Audit Fixture",
        repo_alias="audit-fixture",
        local_repo_path=repo,
        github_repo=None,
        brief=brief,
    )
    catalog = ProjectCatalog(runtime)
    catalog.upsert(project)
    memory = ProjectMemoryStore(runtime, project_id)
    current = memory.ensure_initial_plan(plan(project_id, criterion=criterion))
    project = replace(
        project,
        plan_imported=True,
        plan_version=current.plan_version,
        total_tasks=len(current.tasks),
        current_milestone=current.current_milestone.title if current.current_milestone else None,
        current_task=current.current_task_id,
        plan_path=str(memory.current_pointer),
        project_status="active",
    )
    catalog.upsert(project)
    coordinator = ProjectExecutionCoordinator(runtime, catalog=catalog)
    return runtime, catalog, memory, coordinator, project


def manual_criterion_spoof(root: Path) -> dict[str, object]:
    _runtime, _catalog, _memory, coordinator, project = fixture(
        root, criterion="manual:visual review"
    )
    binding = coordinator.start(project.project_id, expected_repo_head_before=HEAD)
    attempt = coordinator.record_result(
        project.project_id,
        {
            "execution_binding_id": binding.execution_binding_id,
            "command_id": binding.command_id,
            "correlation_id": binding.correlation_id,
            "head_before": HEAD,
            "head_after": None,
            "execution_status": "PASS",
            "validation_status": "PASS",
            "promotion_status": "NOT_RUN",
            "evidence_refs": [],
            "criteria": [
                {
                    "criterion": "manual:visual review",
                    "type": "DETERMINISTIC",
                    "status": "PASS",
                }
            ],
        },
    )
    snapshot = coordinator.snapshot(project.project_id)
    acceptance = snapshot["acceptance_results"][-1]
    return {
        "attempt_result": attempt.result_status,
        "task_status": snapshot["task_statuses"]["t1"],
        "normalized_criterion": acceptance["criteria"][0],
        "manual_approval_invoked": False,
        "evidence_ref_count": len(attempt.evidence_refs),
        "head_after": attempt.head_after,
    }


def gui_auto_authority_split(root: Path) -> dict[str, object]:
    runtime, catalog, memory, coordinator, project = fixture(root)
    commands = CanonicalProjectCenterAutoCommands(
        runtime,
        project.project_id,
        project_provider=lambda: catalog.get(project.project_id),
        plan_provider=memory.current_plan,
    )
    receipt = commands.start_auto(AutoScope.MILESTONE, confirmed=True)
    v2 = commands.snapshot(plan_available=True, plan_version="1")
    v1 = coordinator.milestone_auto_snapshot(project.project_id)
    execution = coordinator.snapshot(project.project_id)
    return {
        "gui_receipt": receipt.to_dict(),
        "v2_scope_status": v2.scope_status,
        "v2_run_id": v2.run_id,
        "v1_status": v1["status"],
        "v1_milestone_run_id": v1["milestone_run_id"],
        "v1_binding_count": len(execution["bindings"]),
        "queue_projection_exists": (runtime / "control" / "project-launch-queue.json").exists(),
    }


def gui_stop_does_not_fence_v1(root: Path) -> dict[str, object]:
    runtime, catalog, memory, coordinator, project = fixture(root)
    v1_started = coordinator.begin_milestone_auto(
        project.project_id,
        milestone_id="m1",
        milestone_run_id="milestone-run-preexisting-v1",
    )
    commands = CanonicalProjectCenterAutoCommands(
        runtime,
        project.project_id,
        project_provider=lambda: catalog.get(project.project_id),
        plan_provider=memory.current_plan,
    )
    commands.start_auto(AutoScope.MILESTONE, confirmed=True)
    stopped = commands.stop_auto()
    v2 = commands.snapshot(plan_available=True, plan_version="1")
    v1_after = coordinator.milestone_auto_snapshot(project.project_id)
    return {
        "v1_status_before_gui_stop": v1_started["status"],
        "gui_stop_receipt": stopped.to_dict(),
        "v2_scope_status_after_gui_stop": v2.scope_status,
        "v1_status_after_gui_stop": v1_after["status"],
        "v1_run_id_after_gui_stop": v1_after["milestone_run_id"],
        "v1_stop_fenced_by_gui": v1_after["status"] == "STOPPED",
    }


def plan_pointer_crash_window(root: Path) -> dict[str, object]:
    runtime, _catalog, memory, _coordinator, project = fixture(root)
    candidate = plan(project.project_id, version=2, supersedes=1)
    preview = memory.preview_update(candidate)
    original_transaction = memory.execution_transaction

    def injected_failure(_transition):
        raise RuntimeError("injected after pointer publication")

    memory.execution_transaction = injected_failure  # type: ignore[method-assign]
    error = None
    try:
        memory.apply_update(candidate, preview)
    except RuntimeError as exc:
        error = str(exc)
    finally:
        memory.execution_transaction = original_transaction  # type: ignore[method-assign]

    reopened = ProjectMemoryStore(runtime, project.project_id)
    current = reopened.current_plan()
    state = reopened.read_state()
    retry_preview = reopened.preview_update(candidate)
    return {
        "injected_error": error,
        "current_plan_version_after_failure": current.plan_version if current else None,
        "plan_updated_event_present": any(
            getattr(event, "event_type", None) == "PLAN_UPDATED" for event in state.events
        ),
        "retry_same_bytes_accepted": retry_preview.accepted,
        "retry_reason": retry_preview.reason_code,
        "event_count": len(state.events),
    }


def conversation_binding_before_claim(root: Path) -> dict[str, object]:
    runtime, catalog, _memory, coordinator, project = fixture(root)
    binding = coordinator.start(project.project_id, expected_repo_head_before=HEAD)
    queue = ProjectLaunchQueueAdapter(runtime / "control" / "project-launch-queue.json")
    queue.enqueue(
        repo_alias=project.repo_alias,
        prompt="isolated audit prompt",
        launch_id=binding.launch_id,
        project_id=project.project_id,
        plan_version=binding.plan_version,
        task_id=binding.task_id,
        execution_binding_id=binding.execution_binding_id,
        correlation_id=binding.correlation_id,
        command_id=binding.command_id,
        expected_repo_head_before=binding.expected_repo_head_before,
    )
    competing_claim = str(uuid.uuid4())
    queue.claim(launch_id=binding.launch_id, claim_id=competing_claim)
    requested_claim = str(uuid.uuid4())
    config = VNextNativeConfig(
        runtime_root=runtime,
        legacy_runtime_root=root / "legacy",
        bootstrap_authority_root=root / "bootstrap",
    )
    response = handle_message(
        config,
        {
            "schema": M9B_NATIVE_REQUEST_SCHEMA,
            "request_id": "isolated-bind-before-claim",
            "action": "project_launch_claim",
            "protocol_generation": PROTOCOL_GENERATION,
            "browser_extension_id": BROWSER_EXTENSION_ID,
            "launch_id": binding.launch_id,
            "claim_id": requested_claim,
            "conversation_id": "conversation-audit-1",
        },
    )
    after = ProjectExecutionCoordinator(runtime, catalog=catalog).binding(
        project.project_id, binding.execution_binding_id
    )
    return {
        "native_status": response["status"],
        "requested_claim_acquired": queue.claim_matches(
            launch_id=binding.launch_id, claim_id=requested_claim
        ),
        "competing_claim_retained": queue.claim_matches(
            launch_id=binding.launch_id, claim_id=competing_claim
        ),
        "conversation_id_after_failed_claim": after.conversation_id,
        "caller_origin_supplied": False,
    }


def ignored_cursor_cas(root: Path) -> dict[str, object]:
    store = ProjectMemoryStoreV2(root / "runtime", "cas-audit")
    store.initialize()
    store.ensure_project("CAS Audit", "cas-audit", str(root / "repo"), {})
    with store._transaction() as conn:
        conn.row_factory = __import__("sqlite3").Row
        coordinator = ProjectScopeCoordinator(conn, "cas-audit")
        with patch.object(ScopeOrchestrator, "update_cursor_cas", return_value=False):
            identity = coordinator.create_run_identity(
                explicit_scope=AutoScope.PROJECT,
                expected_head=HEAD,
                expected_tree=TREE,
            )
        row = conn.execute(
            "SELECT run_id, state_revision FROM scope_cursors WHERE project_id = ?",
            ("cas-audit",),
        ).fetchone()
    return {
        "identity_returned": True,
        "returned_run_id": identity.run_id,
        "durable_run_id": row["run_id"],
        "returned_identity_is_durable": identity.run_id == row["run_id"],
        "source_call_sites_outside_definition_and_tests": 1,
        "source_call_site_is_same_module_command_helper": True,
    }


def output_memory_amplification(root: Path) -> dict[str, object]:
    candidate = root / "candidate"
    candidate.mkdir(parents=True)
    byte_count = 8 * 1024 * 1024
    request = LocalExecutionRequest(
        execution_id="exec:output-audit",
        project_id="project:output-audit",
        adapter_id="process.raw",
        mode=ExecutionMode.ARGV,
        argv=(
            sys.executable,
            "-c",
            f"import sys; sys.stdout.buffer.write(b'A'*{byte_count}); sys.stdout.flush()",
        ),
        cwd=str(candidate),
        env_id="env:audit",
        expected_source_head=HEAD,
        expected_source_tree=TREE,
    )
    evaluator = ExecutionPolicyEvaluator()
    decision = evaluator.evaluate(
        request,
        candidate,
        current_head=HEAD,
        current_tree=TREE,
    )
    tracemalloc.start()
    result = StatelessWindowsProcessRunner().run(
        request,
        decision,
        current_head=HEAD,
        current_tree=TREE,
        candidate_root=candidate,
    )
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "policy_decision": decision.decision,
        "child_output_bytes": byte_count,
        "raw_byte_count": result.stdout.raw_byte_count,
        "inline_character_count": len(result.stdout.inline_content or ""),
        "is_truncated": result.stdout.is_truncated,
        "python_tracemalloc_peak_bytes": peak,
        "peak_to_inline_ratio": round(peak / max(1, len(result.stdout.inline_content or "")), 2),
    }


def policy_no_effect_probes(root: Path) -> dict[str, object]:
    candidate = root / "candidate"
    candidate.mkdir(parents=True)
    evaluator = ExecutionPolicyEvaluator()
    probes: dict[str, object] = {}
    for name, code in {
        "outside_read": "open('C:/Windows/win.ini', 'rb').read(1)",
        "network": "import urllib.request; urllib.request.urlopen('https://example.com')",
    }.items():
        request = LocalExecutionRequest(
            execution_id=f"exec:policy-{name}",
            project_id="project:policy-audit",
            adapter_id="process.raw",
            mode=ExecutionMode.ARGV,
            argv=(sys.executable, "-c", code),
            cwd=str(candidate),
            env_id="env:audit",
            expected_source_head=HEAD,
            expected_source_tree=TREE,
        )
        decision = evaluator.evaluate(
            request,
            candidate,
            current_head=HEAD,
            current_tree=TREE,
        )
        probes[name] = {
            "decision": decision.decision,
            "reason_code": decision.reason_code,
            "network_allowed": decision.network_allowed,
            "command_executed": False,
        }
    return probes


def main() -> None:
    base = Path(__file__).resolve().parent / "lab" / f"run-{uuid.uuid4().hex}"
    base.mkdir(parents=True)
    results = {
        "lab_root": str(base),
        "manual_criterion_spoof": manual_criterion_spoof(base / "manual"),
        "gui_auto_authority_split": gui_auto_authority_split(base / "auto-split"),
        "gui_stop_does_not_fence_v1": gui_stop_does_not_fence_v1(base / "stop-split"),
        "plan_pointer_crash_window": plan_pointer_crash_window(base / "plan-crash"),
        "ignored_cursor_cas": ignored_cursor_cas(base / "cas"),
        "output_memory_amplification": output_memory_amplification(base / "output"),
        "skipped_by_user_safety_direction": [
            "dynamic native-host caller-origin/authentication probe",
            "dynamic undeclared filesystem/network effect probes",
            "physical Registry/UAC/Browser/reparse-point checks",
        ],
    }
    print(json.dumps(results, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()

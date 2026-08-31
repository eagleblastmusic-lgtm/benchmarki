from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from bdb_vnext import environment_cache, project_launch, shared_resources
from bdb_vnext.composition import BROWSER_EXTENSION_ID, PROTOCOL_GENERATION
from bdb_vnext.local_execution_contract import ExecutionEffectClass, ExecutionMode, LocalExecutionRequest
from bdb_vnext.local_execution_worker import DurableExecutionOutbox
from bdb_vnext.m9b_native_host import M9B_NATIVE_REQUEST_SCHEMA, VNextNativeConfig, handle_message
from bdb_vnext.project_catalog import ProjectCatalog
from bdb_vnext.project_execution import ProjectExecutionCoordinator
from bdb_vnext.project_memory import ProjectMemoryStore
from bdb_vnext.project_workflow import CommandResult, ProjectWorkflow
from test_project_execution_integration import HEAD, _fixture


def request(execution_id: str) -> LocalExecutionRequest:
    return LocalExecutionRequest(
        execution_id=execution_id,
        project_id="project:one",
        adapter_id="process.raw",
        mode=ExecutionMode.ARGV,
        argv=("python", "-c", "print('fixture')"),
        cwd=".",
        env_id="env:fixture",
        effect_class=ExecutionEffectClass.PROJECT_MUTATION,
        expected_source_head="1" * 40,
        expected_source_tree="2" * 40,
    )


results: dict[str, object] = {}
root = Path(__file__).resolve().parent / "lab" / f"run-{uuid.uuid4().hex}"
root.mkdir(parents=True, exist_ok=False)
if True:

    catalog_root = root / "catalog"
    catalog = ProjectCatalog(catalog_root)
    catalog.path.parent.mkdir(parents=True, exist_ok=True)
    catalog_lock = catalog.path.parent / "project-catalog.json.lock"
    catalog_lock.write_text(f"{os.getpid()}:foreign-live-owner", encoding="ascii")
    old = time.time() - 180
    os.utime(catalog_lock, (old, old))
    t0 = time.monotonic()
    catalog.write(())
    results["catalog_age_only_reclaimed_live_pid"] = {
        "operation_succeeded": True,
        "elapsed_seconds": round(time.monotonic() - t0, 4),
        "foreign_pid_was_current_process": True,
    }

    memory_root = root / "memory"
    memory = ProjectMemoryStore(memory_root, "project-one")
    memory.root.mkdir(parents=True, exist_ok=True)
    memory_lock = memory.root / "execution.lock"
    memory_lock.write_text(f"{os.getpid()}:foreign-live-owner", encoding="ascii")
    os.utime(memory_lock, (old, old))
    t0 = time.monotonic()
    observed = memory.write_transaction(lambda state: (state, "entered"))
    results["memory_age_only_reclaimed_live_pid"] = {
        "operation_result": observed,
        "elapsed_seconds": round(time.monotonic() - t0, 4),
        "foreign_pid_was_current_process": True,
    }

    launch_root = root / "launch-empty" / "queue.json"
    launch = project_launch.ProjectLaunchQueueAdapter(launch_root)
    old_timeout = project_launch._LOCK_TIMEOUT_SECONDS
    try:
        project_launch._LOCK_TIMEOUT_SECONDS = 0.03
        with patch.object(project_launch.os, "write", side_effect=OSError("injected short/publication failure")):
            try:
                with launch._lock():
                    pass
            except OSError:
                pass
        empty_exists = launch.lock_path.exists()
        empty_size = launch.lock_path.stat().st_size if empty_exists else None
        second_error = None
        try:
            with launch._lock():
                pass
        except Exception as exc:  # exact externally visible fail-closed result
            second_error = f"{type(exc).__name__}:{getattr(exc, 'code', '')}"
        results["launch_publication_before_metadata_wedges_lock"] = {
            "final_path_exists_after_failed_metadata_write": empty_exists,
            "final_path_size": empty_size,
            "subsequent_acquire_result": second_error,
        }
    finally:
        project_launch._LOCK_TIMEOUT_SECONDS = old_timeout

    replacement_root = root / "launch-replacement" / "queue.json"
    replacement = project_launch.ProjectLaunchQueueAdapter(replacement_root)
    replacement.lock_path.parent.mkdir(parents=True, exist_ok=True)
    stale_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    stale = project_launch.ProjectLaunchLockInfo(
        owner_token="a" * 32,
        pid=2_147_483_647,
        acquired_at=project_launch._utc_text(stale_time),
        stale_after_seconds=0.001,
    )
    foreign = project_launch.ProjectLaunchLockInfo(
        owner_token="b" * 32,
        pid=os.getpid(),
        acquired_at=project_launch._utc_text(datetime.now(timezone.utc)),
        stale_after_seconds=10.0,
    )
    replacement.lock_path.write_text(json.dumps(stale.to_dict()), encoding="utf-8")
    original_read = replacement._read_lock_info_safe
    race_state = {"read_count": 0, "foreign_installed": False}

    def racing_read():
        race_state["read_count"] += 1
        observed_info = original_read()
        if race_state["read_count"] == 2:
            replacement.lock_path.write_text(json.dumps(foreign.to_dict()), encoding="utf-8")
            race_state["foreign_installed"] = True
        return observed_info

    replacement._read_lock_info_safe = racing_read  # type: ignore[method-assign]
    acquired_after_replacement = False
    with replacement._lock():
        acquired_after_replacement = True
    results["launch_compare_then_unlink_deletes_replacement"] = {
        "foreign_live_replacement_installed": race_state["foreign_installed"],
        "contender_acquired_after_unlinking_replacement": acquired_after_replacement,
        "read_calls": race_state["read_count"],
    }

    shared_path = root / "shared.lock"
    shared = shared_resources.OwnedSharedLock(shared_path, owner_token="a" * 32)
    assert shared.acquire(timeout_seconds=0.1)
    original_read_regular = shared_resources._read_regular
    foreign_payload = json.dumps({"schema": "bdb-vnext-owned-shared-lock-v1", "owner_token": "b" * 32}).encode()

    def shared_racing_read(path: Path, *, field_name: str):
        prior = original_read_regular(path, field_name=field_name)
        path.write_bytes(foreign_payload)
        return prior

    with patch.object(shared_resources, "_read_regular", side_effect=shared_racing_read):
        shared_release = shared.release()
    results["shared_lock_compare_then_unlink_deletes_replacement"] = {
        "release_returned": shared_release,
        "foreign_replacement_survived": shared_path.exists(),
    }

    cache_path = root / "cache.lock"
    cache = environment_cache.OwnedCacheLock(cache_path, owner_token="a" * 32)
    assert cache.acquire(timeout_seconds=0.1)
    original_json_loads = environment_cache.json.loads

    def cache_racing_loads(raw: str):
        document = original_json_loads(raw)
        cache_path.write_text(json.dumps({"schema": "bdb-vnext-environment-cache-lock-v1", "owner_token": "b" * 32}), encoding="utf-8")
        return document

    with patch.object(environment_cache.json, "loads", side_effect=cache_racing_loads):
        cache_release = cache.release()
    results["cache_lock_compare_then_unlink_deletes_replacement"] = {
        "release_returned": cache_release,
        "foreign_replacement_survived": cache_path.exists(),
    }

    outbox = DurableExecutionOutbox(root / "outbox.db")
    first = request("execution:first")
    second = request("execution:second")
    first_submitted, _ = outbox.submit_request(first)
    second_submitted, _ = outbox.submit_request(second)
    first_claimed = outbox.claim_lease(first.execution_id, "owner:first")
    second_claimed = outbox.claim_lease(second.execution_id, "owner:second")
    results["project_single_flight_not_closed_across_execution_ids"] = {
        "same_project": first.project_id == second.project_id,
        "both_mutating": first.effect_class is not ExecutionEffectClass.READ_ONLY and second.effect_class is not ExecutionEffectClass.READ_ONLY,
        "both_submitted": first_submitted and second_submitted,
        "both_claimed": first_claimed and second_claimed,
    }

    bind_root = root / "claim-bind-order"
    bind_catalog, bind_coordinator, bind_project_id = _fixture(bind_root, all_deterministic=True)
    bind_coordinator.begin_milestone_auto(
        bind_project_id,
        milestone_id="m1",
        milestone_run_id="milestone-run-audit",
    )

    class Runner:
        def run(self, args, *, cwd=None, timeout_seconds=120.0):
            return CommandResult(tuple(args), 0, HEAD + "\n", "")

    bind_queue = project_launch.ProjectLaunchQueueAdapter(
        bind_catalog.runtime_root / "control" / "project-launch-queue.json"
    )
    bind_workflow = ProjectWorkflow(
        bind_catalog.runtime_root,
        catalog=bind_catalog,
        command_runner=Runner(),
        queue=bind_queue,
    )
    bind_launch = bind_workflow.queue_continue_prompt(bind_project_id)
    bind_config = VNextNativeConfig(
        runtime_root=bind_catalog.runtime_root,
        legacy_runtime_root=bind_root / "legacy",
        bootstrap_authority_root=bind_root / "bootstrap",
    )
    losing_claim_id = str(uuid.uuid4())
    losing_conversation = "chatgpt-conversation-loser"
    with patch.object(project_launch.ProjectLaunchQueueAdapter, "claim", return_value=None):
        losing_response = handle_message(
            bind_config,
            {
                "schema": M9B_NATIVE_REQUEST_SCHEMA,
                "request_id": "audit-losing-claim",
                "action": "project_launch_claim",
                "protocol_generation": PROTOCOL_GENERATION,
                "browser_extension_id": BROWSER_EXTENSION_ID,
                "launch_id": bind_launch.launch_id,
                "claim_id": losing_claim_id,
                "conversation_id": losing_conversation,
            },
        )
    bound_after_loss = ProjectExecutionCoordinator(
        bind_catalog.runtime_root,
        catalog=bind_catalog,
    ).binding(bind_project_id, bind_launch.execution_binding_id)
    results["losing_launch_claim_binds_conversation_before_ownership"] = {
        "queue_claim_cas_was_forced_to_lose_after_preview": True,
        "losing_claim_status": losing_response["status"],
        "losing_conversation_was_persisted": bound_after_loss.conversation_id == losing_conversation,
    }

    expiry_root = root / "published-expiry"
    expiry_catalog, expiry_coordinator, expiry_project_id = _fixture(
        expiry_root,
        all_deterministic=True,
    )
    expiry_clock = [datetime(2026, 8, 30, tzinfo=timezone.utc)]
    expiry_queue = project_launch.ProjectLaunchQueueAdapter(
        expiry_catalog.runtime_root / "control" / "project-launch-queue.json",
        now_fn=lambda: expiry_clock[0],
    )
    expiry_workflow = ProjectWorkflow(
        expiry_catalog.runtime_root,
        catalog=expiry_catalog,
        command_runner=Runner(),
        queue=expiry_queue,
    )
    expiry_launch = expiry_workflow.queue_start_prompt(expiry_project_id)
    published_before = expiry_coordinator.launch_outbox_record(
        expiry_project_id,
        expiry_launch.launch_id,
    )
    expiry_clock[0] += timedelta(minutes=11)
    queue_after_expiry = expiry_queue.peek()
    reconciliation = expiry_workflow.reconcile_launch_outbox(expiry_project_id)
    published_after = expiry_coordinator.launch_outbox_record(
        expiry_project_id,
        expiry_launch.launch_id,
    )
    results["published_outbox_does_not_reproject_after_queue_ttl"] = {
        "outbox_status_before_expiry": published_before.status if published_before else None,
        "queue_empty_after_expiry": queue_after_expiry is None,
        "reconciled_count": reconciliation["reconciled_count"],
        "queue_remains_empty": expiry_queue.peek() is None,
        "outbox_status_after_reconcile": published_after.status if published_after else None,
    }

print(json.dumps(results, indent=2, sort_keys=True))

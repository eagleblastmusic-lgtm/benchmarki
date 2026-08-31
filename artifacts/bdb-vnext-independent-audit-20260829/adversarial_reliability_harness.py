"""Local deterministic reliability challenges for the independent audit.

The harness writes only beneath an audit-owned fixture directory supplied on
the command line. It does not import or mutate the repository runtime.
"""

from __future__ import annotations

import json
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from bdb_vnext.environment_cache import OwnedCacheLock
from bdb_vnext.project_catalog import ProjectCatalog
from bdb_vnext.project_launch import (
    ProjectLaunchLockInfo,
    ProjectLaunchQueueAdapter,
    _utc_text,
)
from bdb_vnext.project_memory import ProjectMemoryStore
from bdb_vnext.project_workflow import ProjectWorkflow
from tests.test_nx006_launch_outbox import _setup_project


def main(root_arg: str) -> None:
    root = Path(root_arg).absolute()
    root.mkdir(parents=True, exist_ok=False)
    results: dict[str, object] = {}

    # Liveness after a crash between O_EXCL creation and metadata publication.
    corrupt_queue = ProjectLaunchQueueAdapter(root / "corrupt-lock" / "queue.json")
    corrupt_queue.lock_path.parent.mkdir(parents=True, exist_ok=True)
    corrupt_queue.lock_path.write_bytes(b"")
    old = time.time() - 3_600
    os.utime(corrupt_queue.lock_path, (old, old))
    corrupt_info, corrupt_mtime = corrupt_queue._read_lock_info_safe()
    results["project_launch_partial_create"] = {
        "parsed_metadata": corrupt_info is not None,
        "age_seconds": round(time.time() - (corrupt_mtime or time.time()), 1),
        "reclaimable": corrupt_queue._is_lock_stale(corrupt_info, corrupt_mtime),
        "lock_survives": corrupt_queue.lock_path.exists(),
    }

    # Exact ABA window: a stale record A is re-read, then replaced by live B
    # before the implementation unlinks the pathname. Successful acquisition
    # demonstrates that B was removed rather than preserved.
    replacement = ProjectLaunchLockInfo(
        owner_token="replacement-live-owner",
        pid=os.getpid(),
        acquired_at=_utc_text(datetime.now(timezone.utc)),
        stale_after_seconds=30.0,
    )

    class ABAQueue(ProjectLaunchQueueAdapter):
        def __init__(self, path: Path) -> None:
            super().__init__(path)
            self.read_count = 0
            self.replacement_installed = False

        def _read_lock_info_safe(self):
            self.read_count += 1
            observed = super()._read_lock_info_safe()
            if self.read_count == 2:
                self.lock_path.unlink()
                self.lock_path.write_text(json.dumps(replacement.to_dict()), encoding="utf-8")
                self.replacement_installed = True
            return observed

    aba_queue = ABAQueue(root / "aba-lock" / "queue.json")
    aba_queue.lock_path.parent.mkdir(parents=True, exist_ok=True)
    stale = ProjectLaunchLockInfo(
        owner_token="stale-dead-owner",
        pid=999_999,
        acquired_at="2020-01-01T00:00:00.000000Z",
        stale_after_seconds=1.0,
    )
    aba_queue.lock_path.write_text(json.dumps(stale.to_dict()), encoding="utf-8")
    acquired_token = None
    with aba_queue._lock() as token:
        acquired_token = token
        on_disk = json.loads(aba_queue.lock_path.read_text(encoding="utf-8"))
        replacement_preserved = on_disk.get("owner_token") == replacement.owner_token
    results["project_launch_aba"] = {
        "replacement_installed_between_compare_and_unlink": aba_queue.replacement_installed,
        "replacement_preserved": replacement_preserved,
        "challenger_acquired_after_deleting_replacement": bool(acquired_token),
    }

    # Both current canonical JSON owners reclaim by age alone, even when the
    # on-disk PID is the demonstrably live current process.
    memory = ProjectMemoryStore(root / "memory-runtime", "project-live-lock")
    memory.root.mkdir(parents=True, exist_ok=True)
    memory_lock = memory.root / "execution.lock"
    memory_original = f"{os.getpid()}:original-live-owner"
    memory_lock.write_text(memory_original, encoding="ascii")
    os.utime(memory_lock, (time.time() - 121, time.time() - 121))
    with memory._execution_lock():
        memory_replacement = memory_lock.read_text(encoding="ascii")
    results["project_memory_age_only_reclaim"] = {
        "owner_pid_was_current_process": True,
        "original_live_lock_preserved": memory_replacement == memory_original,
        "new_owner_entered_critical_section": memory_replacement != memory_original,
    }

    catalog = ProjectCatalog(root / "catalog-runtime")
    catalog_lock = catalog.path.parent / "project-catalog.json.lock"
    catalog_lock.parent.mkdir(parents=True, exist_ok=True)
    catalog_original = f"{os.getpid()}:original-live-owner"
    catalog_lock.write_text(catalog_original, encoding="ascii")
    os.utime(catalog_lock, (time.time() - 121, time.time() - 121))
    with catalog._lock():
        catalog_replacement = catalog_lock.read_text(encoding="ascii")
    results["project_catalog_age_only_reclaim"] = {
        "owner_pid_was_current_process": True,
        "original_live_lock_preserved": catalog_replacement == catalog_original,
        "new_owner_entered_critical_section": catalog_replacement != catalog_original,
    }

    # The reachable environment-cache pathname lock has ownership-safe release
    # but no dead-owner/crash recovery at all.
    cache_path = root / "cache" / "environment.lock"
    first_cache_lock = OwnedCacheLock(cache_path, owner_token="owner-a")
    second_cache_lock = OwnedCacheLock(cache_path, owner_token="owner-b")
    first_acquired = first_cache_lock.acquire(timeout_seconds=0)
    second_acquired = second_cache_lock.acquire(timeout_seconds=0)
    results["environment_cache_crash_recovery"] = {
        "first_owner_acquired": first_acquired,
        "second_owner_can_recover_existing_record": second_acquired,
        "recovery_metadata_present": False,
    }
    first_cache_lock.release()

    # Reproduce the orphan check -> clear race against the actual workflow.
    orphan_a = SimpleNamespace(
        launch_id="orphan-a",
        project_id=None,
        execution_binding_id=None,
    )
    valid_b = SimpleNamespace(
        launch_id="valid-b",
        project_id="project-b",
        execution_binding_id="binding-b",
    )

    class InterleavingQueue:
        def __init__(self) -> None:
            self.pending = orphan_a
            self.erased_launch_id = None

        def peek(self):
            return self.pending

        @contextmanager
        def _lock(self):
            # Another process replaces A after the workflow's orphan decision
            # but before its clearing critical section begins.
            self.pending = valid_b
            yield

        def _write_state_unlocked(self, pending, claim):
            self.erased_launch_id = getattr(self.pending, "launch_id", None)
            self.pending = pending

    interleaving_queue = InterleavingQueue()
    empty_catalog = ProjectCatalog(root / "outbox-race-runtime")
    race_workflow = ProjectWorkflow(empty_catalog.runtime_root, catalog=empty_catalog, queue=interleaving_queue)
    race_report = race_workflow.reconcile_launch_outbox()
    results["orphan_projection_check_mutation_race"] = {
        "checked_launch_id": orphan_a.launch_id,
        "replacement_before_clear": valid_b.launch_id,
        "actually_erased_launch_id": interleaving_queue.erased_launch_id,
        "reported_orphans_cleared": race_report["orphans_cleared"],
    }

    # Natural expiry of a downstream queue projection leaves a durable
    # PUBLISHED record. The reconciler selects PENDING only and does not rebuild
    # or explicitly block this state.
    ttl_workflow, project_id = _setup_project(root / "published-expiry")
    coordinator = ttl_workflow.execution
    binding = coordinator.new_binding(project_id, task_id="T1-01")
    _, outbox = coordinator.prepare_launch(
        project_id,
        binding=binding,
        prompt="Expiry recovery challenge",
        auto_send=True,
    )
    ttl_workflow.publish_outbox_launch(project_id, outbox.launch_id)
    status_before = coordinator.launch_outbox_record(project_id, outbox.launch_id).status
    ttl_workflow.queue.now_fn = lambda: datetime.now(timezone.utc) + timedelta(minutes=11)
    expired_projection = ttl_workflow.queue.peek() is None
    recovery_report = ttl_workflow.reconcile_launch_outbox(project_id)
    status_after = coordinator.launch_outbox_record(project_id, outbox.launch_id).status
    results["published_projection_expiry_recovery"] = {
        "outbox_status_before_expiry": status_before,
        "queue_projection_expired": expired_projection,
        "reconciled_count": recovery_report["reconciled_count"],
        "queue_rebuilt": ttl_workflow.queue.peek() is not None,
        "outbox_status_after_reconcile": status_after,
    }

    print(json.dumps(results, indent=2, sort_keys=True))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: adversarial_reliability_harness.py AUDIT_FIXTURE_ROOT")
    main(sys.argv[1])

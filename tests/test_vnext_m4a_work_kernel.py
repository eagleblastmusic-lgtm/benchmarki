from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from bdb_vnext.composition import WORK_KERNEL_PROVIDER_ID, build_vnext_composition_manifest
from bdb_vnext.m3a_submission import ShadowSubmissionRequest
from bdb_vnext.m3c_admission import open_vnext_admission_composition
from bdb_vnext.m4a_work_kernel import (
    M4A_QUERY_SCHEMA,
    M4A_SCHEMA,
    M4A_WRITER_ID,
    M4aError,
    WorkKernelStore,
    scan_supported_workitem_writers,
)


ROOT = Path(__file__).resolve().parents[1]


def request(key: str = "browser:m4a-task") -> ShadowSubmissionRequest:
    return ShadowSubmissionRequest(
        submission_key=key,
        intent_revision="r1",
        intent={"operation": "inspect", "path": "bdb_vnext/repo_view.py"},
        conversation_binding={"conversation_id": "m4a-conversation"},
        consumer_binding={"consumer_id": "m4a-browser", "kind": "browser"},
    )


@contextmanager
def stack(tmp_path: Path):
    runtime = tmp_path / "vnext"
    legacy = tmp_path / "legacy"
    composition = open_vnext_admission_composition(runtime, legacy_root=legacy)
    receipt = composition.authority.admit(request())
    assert receipt.task_id is not None
    kernel = WorkKernelStore.open(
        runtime,
        task_authority=composition.authority,
        legacy_root=legacy,
        clock=lambda: 100.0,
    )
    try:
        yield composition, kernel, receipt.task_id
    finally:
        kernel.close()
        composition.close()


def test_task_binding_and_initial_current_query_are_canonical(tmp_path: Path) -> None:
    with stack(tmp_path) as (_composition, kernel, task_id):
        item = kernel.create_work_item("work:m4a-1", task_id, kind="inspect")
        query = kernel.query(item.work_id)
        assert query is not None
        assert query.work.disposition == "READY"
        assert query.work.state_version == 0
        assert query.work.task_id == task_id
        assert [fact.kind for fact in query.recent_facts] == ["work_created"]
        assert query.as_dict()["authority"] == "devmaster.bdb.vnext.work-kernel"

        with pytest.raises(M4aError) as caught:
            kernel.create_work_item("work:foreign", "task:not-accepted")
        assert caught.value.code == "task_not_found"


def test_full_minimal_lifecycle_keeps_dimensions_orthogonal(tmp_path: Path) -> None:
    with stack(tmp_path) as (_composition, kernel, task_id):
        item = kernel.create_work_item("work:lifecycle", task_id)
        lease = kernel.acquire_lease(item.work_id, "lease:one", "worker:one")
        claim = kernel.claim_resource(item.work_id, "resource:repo", lease.lease_id, lease.fence)
        run = kernel.start_run(item.work_id, "run:one", lease.lease_id, lease.fence, 0)
        assert run.status == "ACTIVE"
        waiting = kernel.enter_wait(item.work_id, "wait:one", "resource", lease.lease_id, lease.fence, 1)
        assert waiting.status == "OPEN"
        assert kernel.query(item.work_id).work.disposition == "WAITING"  # type: ignore[union-attr]
        resolved = kernel.resolve_wait(item.work_id, waiting.wait_id, lease.lease_id, lease.fence, 2)
        assert resolved.status == "RESOLVED"
        finished = kernel.finish_run(
            item.work_id,
            run.run_id,
            lease.lease_id,
            lease.fence,
            3,
            outcome="FAILED",
            effect_certainty="POSSIBLE",
        )
        assert finished.outcome == "FAILED"
        query = kernel.query(item.work_id)
        assert query is not None
        assert query.work.disposition == "FINISHED"
        assert query.last_run is not None and query.last_run.effect_certainty == "POSSIBLE"
        assert query.resource_claim == claim
        assert len(kernel.facts(item.work_id)) == 5


def test_duplicate_run_and_wait_requests_are_replay_not_second_authority(tmp_path: Path) -> None:
    with stack(tmp_path) as (_composition, kernel, task_id):
        item = kernel.create_work_item("work:replay", task_id)
        lease = kernel.acquire_lease(item.work_id, "lease:replay", "worker:replay")
        first = kernel.start_run(item.work_id, "run:replay", lease.lease_id, lease.fence, 0)
        replay = kernel.start_run(item.work_id, "run:replay", lease.lease_id, lease.fence, 99)
        assert replay == first
        wait = kernel.enter_wait(item.work_id, "wait:replay", "dependency", lease.lease_id, lease.fence, 1)
        assert kernel.enter_wait(item.work_id, wait.wait_id, wait.reason, lease.lease_id, lease.fence, 99) == wait
        resolved = kernel.resolve_wait(item.work_id, wait.wait_id, lease.lease_id, lease.fence, 2)
        assert kernel.resolve_wait(item.work_id, wait.wait_id, lease.lease_id, lease.fence, 99) == resolved
        assert kernel.resolve_wait(item.work_id, wait.wait_id, lease.lease_id, lease.fence, 2) == resolved


def test_stale_state_version_is_rejected_without_partial_fact(tmp_path: Path) -> None:
    with stack(tmp_path) as (_composition, kernel, task_id):
        item = kernel.create_work_item("work:version", task_id)
        lease = kernel.acquire_lease(item.work_id, "lease:version", "worker:version")
        kernel.start_run(item.work_id, "run:version", lease.lease_id, lease.fence, 0)
        before = kernel.counts()
        with pytest.raises(M4aError) as caught:
            kernel.enter_wait(item.work_id, "wait:version", "dependency", lease.lease_id, lease.fence, 0)
        assert caught.value.code == "stale_state_version"
        assert kernel.counts() == before


def test_lease_handoff_advances_fence_and_invalidates_stale_worker(tmp_path: Path) -> None:
    with stack(tmp_path) as (_composition, kernel, task_id):
        item = kernel.create_work_item("work:fence", task_id)
        old = kernel.acquire_lease(item.work_id, "lease:old", "worker:old", ttl_seconds=1, now=0)
        new = kernel.acquire_lease(item.work_id, "lease:new", "worker:new", ttl_seconds=30, now=2)
        assert new.fence == old.fence + 1
        with pytest.raises(M4aError) as stale:
            kernel.start_run(item.work_id, "run:stale", old.lease_id, old.fence, 0, now=2)
        assert stale.value.code == "stale_lease"
        run = kernel.start_run(item.work_id, "run:new", new.lease_id, new.fence, 0, now=2)
        assert run.fence == new.fence


def test_adopt_active_run_rejects_stale_reverse_fence(tmp_path: Path) -> None:
    with stack(tmp_path) as (_composition, kernel, task_id):
        item = kernel.create_work_item("work:adopt-fence", task_id)
        old = kernel.acquire_lease(item.work_id, "lease:adopt-old", "worker:old", ttl_seconds=1, now=0)
        run = kernel.start_run(item.work_id, "run:adopt-fence", old.lease_id, old.fence, 0, now=0)
        newer = kernel.acquire_lease(item.work_id, "lease:adopt-new", "worker:new", ttl_seconds=30, now=2)
        adopted = kernel.adopt_active_run(item.work_id, run.run_id, newer.lease_id, newer.fence, 1, now=2)
        assert adopted.fence == newer.fence
        with pytest.raises(M4aError) as stale:
            kernel.adopt_active_run(item.work_id, run.run_id, old.lease_id, old.fence, 2, now=2)
        assert stale.value.code == "stale_lease"
        current = kernel.query(item.work_id, now=2)
        assert current is not None and current.active_run is not None
        assert current.active_run.fence == newer.fence


def test_lease_handoff_rebinds_held_resource_to_the_new_fence(tmp_path: Path) -> None:
    with stack(tmp_path) as (_composition, kernel, task_id):
        item = kernel.create_work_item("work:claim-handoff", task_id)
        old = kernel.acquire_lease(item.work_id, "lease:claim-old", "worker:claim-old", ttl_seconds=1, now=0)
        original = kernel.claim_resource(item.work_id, "resource:handoff", old.lease_id, old.fence, now=0)

        new = kernel.acquire_lease(item.work_id, "lease:claim-new", "worker:claim-new", ttl_seconds=30, now=2)
        query = kernel.query(item.work_id, now=2)
        assert query is not None and query.resource_claim is not None
        assert query.resource_claim.resource_key == original.resource_key
        assert query.resource_claim.lease_id == new.lease_id
        assert query.resource_claim.fence == new.fence

        with pytest.raises(M4aError) as stale:
            kernel.release_resource(item.work_id, original.resource_key, old.lease_id, old.fence, now=2)
        assert stale.value.code == "stale_lease"
        assert kernel.release_resource(item.work_id, original.resource_key, new.lease_id, new.fence, now=2).state == "RELEASED"


def test_lease_release_requires_explicit_resource_release(tmp_path: Path) -> None:
    with stack(tmp_path) as (_composition, kernel, task_id):
        item = kernel.create_work_item("work:claim-release", task_id)
        lease = kernel.acquire_lease(item.work_id, "lease:claim-release", "worker:claim-release")
        kernel.claim_resource(item.work_id, "resource:release", lease.lease_id, lease.fence)

        with pytest.raises(M4aError) as held:
            kernel.release_lease(item.work_id, lease.lease_id, lease.fence)
        assert held.value.code == "resource_claim_held"

        kernel.release_resource(item.work_id, "resource:release", lease.lease_id, lease.fence)
        assert kernel.release_lease(item.work_id, lease.lease_id, lease.fence).state == "RELEASED"


def test_workitem_has_at_most_one_held_resource_claim(tmp_path: Path) -> None:
    with stack(tmp_path) as (_composition, kernel, task_id):
        item = kernel.create_work_item("work:one-claim", task_id)
        lease = kernel.acquire_lease(item.work_id, "lease:one-claim", "worker:one-claim")
        kernel.claim_resource(item.work_id, "resource:first", lease.lease_id, lease.fence)

        with pytest.raises(M4aError) as conflict:
            kernel.claim_resource(item.work_id, "resource:second", lease.lease_id, lease.fence)
        assert conflict.value.code == "work_resource_conflict"
        assert kernel.query(item.work_id).resource_claim.resource_key == "resource:first"  # type: ignore[union-attr]


def test_new_fence_cannot_complete_or_wait_an_old_fence_run(tmp_path: Path) -> None:
    with stack(tmp_path) as (_composition, kernel, task_id):
        item = kernel.create_work_item("work:run-owner", task_id)
        old = kernel.acquire_lease(item.work_id, "lease:run-old", "worker:run-old", ttl_seconds=1, now=0)
        kernel.claim_resource(item.work_id, "resource:run-owner", old.lease_id, old.fence, now=0)
        run = kernel.start_run(item.work_id, "run:owned-old", old.lease_id, old.fence, 0, now=0)
        new = kernel.acquire_lease(item.work_id, "lease:run-new", "worker:run-new", ttl_seconds=30, now=2)

        with pytest.raises(M4aError) as finish:
            kernel.finish_run(item.work_id, run.run_id, new.lease_id, new.fence, 1, outcome="CANCELLED", now=2)
        assert finish.value.code == "run_ownership_mismatch"

        with pytest.raises(M4aError) as waiting:
            kernel.enter_wait(item.work_id, "wait:new-owner", "reconciliation", new.lease_id, new.fence, 1, now=2)
        assert waiting.value.code == "run_ownership_mismatch"

        with pytest.raises(M4aError) as release:
            kernel.release_resource(item.work_id, "resource:run-owner", new.lease_id, new.fence, now=2)
        assert release.value.code == "resource_reconciliation_required"
        query = kernel.query(item.work_id, now=2)
        assert query is not None and query.work.disposition == "RUNNING"
        assert query.active_run == run


def test_two_claimers_and_resource_claim_are_deterministic(tmp_path: Path) -> None:
    with stack(tmp_path) as (_composition, kernel, task_id):
        first = kernel.create_work_item("work:claim-a", task_id)
        second = kernel.create_work_item("work:claim-b", task_id, kind="other")
        lease_a = kernel.acquire_lease(first.work_id, "lease:a", "worker:a")
        lease_b = kernel.acquire_lease(second.work_id, "lease:b", "worker:b")
        kernel.claim_resource(first.work_id, "resource:exclusive", lease_a.lease_id, lease_a.fence)
        with pytest.raises(M4aError) as conflict:
            kernel.claim_resource(second.work_id, "resource:exclusive", lease_b.lease_id, lease_b.fence)
        assert conflict.value.code == "resource_conflict"


def test_wait_survives_reopen_and_query_remains_canonical(tmp_path: Path) -> None:
    runtime = tmp_path / "vnext"
    legacy = tmp_path / "legacy"
    composition = open_vnext_admission_composition(runtime, legacy_root=legacy)
    receipt = composition.authority.admit(request("browser:m4a-reopen"))
    assert receipt.task_id
    kernel = WorkKernelStore.open(runtime, task_authority=composition.authority, legacy_root=legacy, clock=lambda: 50.0)
    item = kernel.create_work_item("work:reopen", receipt.task_id)
    lease = kernel.acquire_lease(item.work_id, "lease:reopen", "worker:reopen")
    kernel.start_run(item.work_id, "run:reopen", lease.lease_id, lease.fence, 0)
    kernel.enter_wait(item.work_id, "wait:reopen", "operator", lease.lease_id, lease.fence, 1)
    before = kernel.query(item.work_id)
    assert before is not None
    kernel.close()
    reopened = WorkKernelStore.open(runtime, task_authority=composition.authority, legacy_root=legacy, clock=lambda: 50.0)
    try:
        after = reopened.query(item.work_id)
        assert after is not None
        assert after.as_dict() == before.as_dict()
    finally:
        reopened.close()
        composition.close()


def test_transaction_faults_rollback_or_preserve_committed_truth(tmp_path: Path) -> None:
    with stack(tmp_path) as (_composition, kernel, task_id):
        with pytest.raises(M4aError) as before:
            kernel.create_work_item("work:before", task_id, failpoint="before_transaction")
        assert before.value.code == "simulated_crash_before_transaction"
        assert kernel.query("work:before") is None

        with pytest.raises(M4aError) as during:
            kernel.create_work_item("work:during", task_id, failpoint="during_transaction")
        assert during.value.code == "simulated_crash_during_transaction"
        assert kernel.query("work:during") is None

        with pytest.raises(M4aError) as after:
            kernel.create_work_item("work:after", task_id, failpoint="after_commit")
        assert after.value.code == "simulated_response_loss_after_commit"
        committed = kernel.query("work:after")
        assert committed is not None and committed.work.state_version == 0


def test_database_busy_is_explicit_and_does_not_fake_transition(tmp_path: Path) -> None:
    with stack(tmp_path) as (composition, kernel, task_id):
        item = kernel.create_work_item("work:busy", task_id)
        holder = WorkKernelStore.open(
            tmp_path / "vnext",
            task_authority=composition.authority,
            legacy_root=tmp_path / "legacy",
            busy_timeout_ms=25,
            clock=lambda: 100.0,
        )
        try:
            with holder.hold_write_lock():
                with pytest.raises(M4aError) as caught:
                    kernel.acquire_lease(item.work_id, "lease:busy", "worker:busy")
                assert caught.value.code == "database_busy"
            assert kernel.query(item.work_id).lease is None  # type: ignore[union-attr]
        finally:
            holder.close()


def test_query_schema_digest_store_boundary_and_provider_are_explicit(tmp_path: Path) -> None:
    with stack(tmp_path) as (composition, kernel, task_id):
        item = kernel.create_work_item("work:schema", task_id)
        query = kernel.query(item.work_id)
        assert query is not None
        payload = query.as_dict()
        assert payload["schema"] == M4A_QUERY_SCHEMA
        assert payload["query_digest"].startswith("sha256:")
        schema = json.loads((ROOT / "schemas" / "bdb-vnext-m4a-work-query-v1.schema.json").read_text(encoding="utf-8"))
        assert schema["$id"] == M4A_QUERY_SCHEMA
        marker = json.loads((tmp_path / "vnext" / "control" / "m4a-work-kernel.json").read_text(encoding="utf-8"))
        assert marker["writer_id"] == M4A_WRITER_ID
        assert marker["production_writer"] is False
        with sqlite3.connect(kernel.database_path) as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            assert tables == {
                "vnext_control_metadata",
                "m3a_submissions",
                "m3a_tasks",
                "m3a_intent_revisions",
                "m3a_consumer_bindings",
                "m3c_kill_switch",
                "m4a_sequence",
                "m4a_work_items",
                "m4a_runs",
                "m4a_waits",
                "m4a_leases",
                "m4a_resource_claims",
                "m4a_transition_facts",
            }
        assert not any("command" in name or "session" in name or "legacy" in name for name in tables)
        manifest = build_vnext_composition_manifest(source_commit="4" * 40, runtime_root=tmp_path / "manifest-vnext", legacy_runtime_root=tmp_path / "manifest-legacy", forbidden_roots=[ROOT])
        provider = next(item for item in manifest["composition"]["providers"] if item["provider_id"] == WORK_KERNEL_PROVIDER_ID)
        assert provider["state"] == "reserved_disabled"
        assert provider["writer_enabled"] is False


def test_canonical_query_cannot_mix_rows_across_concurrent_commit(tmp_path: Path) -> None:
    with stack(tmp_path) as (composition, kernel, task_id):
        item = kernel.create_work_item("work:query-snapshot", task_id)
        lease = kernel.acquire_lease(item.work_id, "lease:query-snapshot", "worker:query-snapshot")
        writer = WorkKernelStore.open(
            tmp_path / "vnext",
            task_authority=composition.authority,
            legacy_root=tmp_path / "legacy",
            clock=lambda: 100.0,
        )
        original_work_row = kernel._work_row
        transition_done = threading.Event()
        transition_errors: list[Exception] = []
        transition_thread: threading.Thread | None = None
        first_read = True

        def transition() -> None:
            try:
                writer.start_run(item.work_id, "run:query-snapshot", lease.lease_id, lease.fence, 0)
            except Exception as exc:  # pragma: no cover - asserted below
                transition_errors.append(exc)
            finally:
                transition_done.set()

        def pause_after_current_row(work_id: str):
            nonlocal first_read, transition_thread
            row = original_work_row(work_id)
            if first_read:
                first_read = False
                transition_thread = threading.Thread(target=transition)
                transition_thread.start()
                assert transition_done.wait(timeout=5)
            return row

        kernel._work_row = pause_after_current_row  # type: ignore[method-assign]
        try:
            snapshot = kernel.query(item.work_id)
        finally:
            kernel._work_row = original_work_row  # type: ignore[method-assign]
            if transition_thread is not None:
                transition_thread.join(timeout=5)
            writer.close()

        assert not transition_errors
        assert snapshot is not None
        assert snapshot.work.disposition == "READY"
        assert snapshot.active_run is None
        current = kernel.query(item.work_id)
        assert current is not None and current.work.disposition == "RUNNING"
        assert current.active_run is not None and current.active_run.run_id == "run:query-snapshot"


def test_no_legacy_tables_or_alternate_workitem_writer_are_supported() -> None:
    proof = scan_supported_workitem_writers()
    assert proof["pass"] is True
    assert proof["canonical_writer"] == M4A_WRITER_ID
    assert proof["alternate_writers"] == []
    source_files = [path for path in (ROOT / "bdb_vnext").glob("*.py") if path.name not in {"m4a_work_kernel.py", "control_store.py"}]
    assert not any("m4a_work_items" in path.read_text(encoding="utf-8") for path in source_files)


def test_invalid_runtime_overlap_and_non_shadow_mode_fail_closed(tmp_path: Path) -> None:
    runtime = tmp_path / "vnext"
    legacy = tmp_path / "legacy"
    composition = open_vnext_admission_composition(runtime, legacy_root=legacy)
    try:
        with pytest.raises(M4aError) as mode:
            WorkKernelStore(runtime, task_authority=composition.authority, shadow=False, legacy_root=legacy)
        assert mode.value.code == "shadow_mode_required"
        with pytest.raises(M4aError) as overlap:
            WorkKernelStore.open(runtime, task_authority=composition.authority, legacy_root=runtime / "nested-legacy")
        assert overlap.value.code == "foreign_state_overlap"
    finally:
        composition.close()

from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from bdb_vnext.m3a_submission import (
    M3A_CANONICALIZATION_VERSION,
    M3A_SCHEMA,
    M3aError,
    ShadowSubmissionRequest,
    ShadowSubmissionStore,
)


def make_request(
    key: str = "browser:submission-1",
    *,
    task_id: str | None = None,
    revision: str = "r1",
    intent: dict[str, object] | None = None,
    expected_revision: str | None = None,
    request_digest: str | None = None,
) -> ShadowSubmissionRequest:
    return ShadowSubmissionRequest(
        submission_key=key,
        task_id=task_id,
        intent_revision=revision,
        intent=intent or {"operation": "inspect", "path": "bdb_vnext/repo_view.py"},
        conversation_binding={"conversation_id": "conversation-shadow-1"},
        consumer_binding={"consumer_id": "browser-shadow-1", "kind": "browser"},
        expected_intent_revision_id=expected_revision,
        request_digest=request_digest,
    )


def open_store(tmp_path: Path) -> ShadowSubmissionStore:
    return ShadowSubmissionStore(tmp_path / "m3a", shadow=True, legacy_root=tmp_path / "legacy")


def test_request_digest_is_deterministic_and_versioned() -> None:
    first = make_request()
    second = make_request()
    assert first.canonicalization_version == M3A_CANONICALIZATION_VERSION
    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.computed_digest() == second.computed_digest()
    assert first.as_dict()["schema"] == M3A_SCHEMA


def test_unsupported_version_is_explicit() -> None:
    with pytest.raises(M3aError, match="frozen canonical") as caught:
        ShadowSubmissionRequest(
            "browser:version",
            "r1",
            {"operation": "inspect"},
            {"conversation_id": "c"},
            {"consumer_id": "u"},
            canonicalization_version="bdb-vnext-canonical-request-v0",
        )
    assert caught.value.code == "unsupported_canonical_version"


def test_invalid_canonical_request_and_digest_mismatch_fail_closed() -> None:
    with pytest.raises(M3aError) as invalid:
        make_request(intent={"not-json": object()})
    assert invalid.value.code == "invalid_canonical_request"

    request = make_request(request_digest="sha256:" + "0" * 64)
    with pytest.raises(M3aError) as mismatch:
        request.validated_digest()
    assert mismatch.value.code == "digest_mismatch"


def test_same_key_same_digest_replays_and_maps_exactly_one_task_revision(tmp_path: Path) -> None:
    with open_store(tmp_path) as store:
        request = make_request()
        first = store.admit(request)
        replay = store.admit(request)
        assert first.outcome == "published"
        assert replay.outcome == "replay"
        assert replay.task_id == first.task_id
        assert replay.intent_revision_id == first.intent_revision_id
        assert store.counts() == {"submissions": 1, "tasks": 1, "intent_revisions": 1, "consumer_bindings": 1}
        task = store.task(first.task_id or "")
        assert task is not None
        assert task.intent_revision_id == first.intent_revision_id


def test_same_key_different_digest_is_conflict(tmp_path: Path) -> None:
    with open_store(tmp_path) as store:
        store.admit(make_request())
        with pytest.raises(M3aError) as caught:
            store.admit(make_request(intent={"operation": "write", "path": "other.py"}))
        assert caught.value.code == "submission_conflict"
        assert store.counts()["tasks"] == 1


def test_tombstone_is_retained_and_cannot_resurrect(tmp_path: Path) -> None:
    with open_store(tmp_path) as store:
        request = make_request("browser:tombstone")
        tombstone = store.tombstone(request, reason="operator denied in shadow fixture")
        assert tombstone.status == "TOMBSTONED"
        assert store.lookup(request.submission_key).status == "TOMBSTONED"  # type: ignore[union-attr]
        with pytest.raises(M3aError) as caught:
            store.admit(request)
        assert caught.value.code == "tombstone_conflict"
        assert store.counts() == {"submissions": 1, "tasks": 0, "intent_revisions": 0, "consumer_bindings": 0}


def test_stale_intent_revision_is_rejected_before_new_task(tmp_path: Path) -> None:
    with open_store(tmp_path) as store:
        first = store.admit(make_request("browser:shared-1", task_id="task-shared"))
        with pytest.raises(M3aError) as caught:
            store.admit(
                make_request(
                    "browser:shared-2",
                    task_id="task-shared",
                    expected_revision="sha256:" + "f" * 64,
                )
            )
        assert caught.value.code == "stale_intent_revision"
        assert store.task(first.task_id or "") is not None


def test_fail_before_commit_rolls_back_and_after_commit_replays(tmp_path: Path) -> None:
    before_root = tmp_path / "before"
    with ShadowSubmissionStore(before_root, shadow=True) as store:
        with pytest.raises(M3aError) as caught:
            store.admit(make_request("browser:crash-before"), failpoint="before_commit")
        assert caught.value.code == "simulated_crash_before_commit"
        assert store.lookup("browser:crash-before") is None

    after_root = tmp_path / "after"
    request = make_request("browser:crash-after")
    with ShadowSubmissionStore(after_root, shadow=True) as store:
        with pytest.raises(M3aError) as caught:
            store.admit(request, failpoint="after_commit")
        assert caught.value.code == "simulated_response_loss_after_commit"
    with ShadowSubmissionStore(after_root, shadow=True) as reopened:
        replay = reopened.admit(request)
        assert replay.outcome == "replay"


def test_concurrent_same_key_same_digest_creates_one_task(tmp_path: Path) -> None:
    root = tmp_path / "concurrent-same"
    request = make_request("browser:concurrent-same")

    def admit_once() -> tuple[str, str | None]:
        with ShadowSubmissionStore(root, shadow=True) as store:
            receipt = store.admit(request)
            return receipt.outcome, receipt.task_id

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: admit_once(), range(8)))
    assert {task_id for _, task_id in results}.__len__() == 1
    assert sorted(outcome for outcome, _ in results).count("published") == 1
    with ShadowSubmissionStore(root, shadow=True) as store:
        assert store.counts()["tasks"] == 1


def test_concurrent_same_key_different_digest_has_one_winner(tmp_path: Path) -> None:
    root = tmp_path / "concurrent-conflict"
    requests = [make_request("browser:concurrent-conflict", intent={"value": index}) for index in range(8)]

    def admit_once(request: ShadowSubmissionRequest) -> str:
        with ShadowSubmissionStore(root, shadow=True) as store:
            try:
                return store.admit(request).outcome
            except M3aError as exc:
                return exc.code

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(admit_once, requests))
    assert results.count("published") == 1
    assert results.count("submission_conflict") == 7
    with ShadowSubmissionStore(root, shadow=True) as store:
        assert store.counts()["submissions"] == 1


def test_database_busy_is_explicit_when_shadow_writer_is_locked(tmp_path: Path) -> None:
    root = tmp_path / "busy"
    with ShadowSubmissionStore(root, shadow=True, busy_timeout_ms=25) as holder:
        with holder.hold_write_lock():
            with ShadowSubmissionStore(root, shadow=True, busy_timeout_ms=25) as contender:
                with pytest.raises(M3aError) as caught:
                    contender.admit(make_request("browser:busy"))
                assert caught.value.code == "database_busy"


def test_shadow_mode_and_legacy_overlap_are_hard_guards(tmp_path: Path) -> None:
    with pytest.raises(M3aError) as mode:
        ShadowSubmissionStore(tmp_path / "m3a", shadow=False)
    assert mode.value.code == "shadow_mode_required"
    with pytest.raises(M3aError) as overlap:
        ShadowSubmissionStore(tmp_path / "legacy" / "nested", shadow=True, legacy_root=tmp_path / "legacy")
    assert overlap.value.code == "foreign_state_overlap"


def test_shadow_store_has_no_legacy_receipt_or_spool_tables(tmp_path: Path) -> None:
    with open_store(tmp_path) as store:
        names = {
            row[0]
            for row in store._connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert names == {
            "vnext_control_metadata",
            "m3a_submissions",
            "m3a_tasks",
            "m3a_intent_revisions",
            "m3a_consumer_bindings",
        }
        assert not any("receipt" in name or "spool" in name or "session" in name or "command" in name for name in names)

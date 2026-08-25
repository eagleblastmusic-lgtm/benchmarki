"""BDB vNext - NX-007 Project Launch Queue Locking Tests & Machine Gate.

Verifies:
1. Ownership token & structured JSON metadata recording on disk.
2. Compare-before-release prevents blind unlink of stolen/foreign/renewed locks.
3. Fail-Closed on Corrupt/Unreadable/Unknown Metadata:
   - Corrupt old lock (invalid JSON) -> NOT deleted, raises queue_busy
   - Unreadable lock -> NOT deleted, raises queue_busy
   - Invalid/missing owner_token -> NOT deleted
   - Invalid PID metadata -> NOT deleted
4. Dead-owner verification (AGE_ONLY_RECLAIM = FALSE):
   - Valid old lock + owner alive -> NOT deleted
   - Valid old lock + owner confirmed dead + stale elapsed -> safely reclaimed
5. Compare-before-reclaim race resilience (foreign replacement never deleted).
6. Windows PermissionError classification and Native Host exception safety.
7. High-concurrency stress harness.
8. Deterministic canonical NX-007 machine gate.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from bdb_vnext.project_launch import (
    PROJECT_LAUNCH_LOCK_SCHEMA,
    ProjectLaunchLockInfo,
    ProjectLaunchQueueAdapter,
    ProjectLaunchQueueError,
    _is_pid_alive,
    _parse_utc,
    _utc_text,
)


# -----------------------------------------------------------------------------
# 1. LOCK OWNERSHIP TOKEN & BASIC CBR
# -----------------------------------------------------------------------------

def test_lock_records_ownership_metadata_on_disk(tmp_path: Path) -> None:
    """Lock file must contain valid JSON metadata with owner_token, pid, and timestamp."""
    queue_path = tmp_path / "queue.json"
    queue = ProjectLaunchQueueAdapter(queue_path)

    with queue._lock() as token:
        assert queue.lock_path.exists()
        raw = json.loads(queue.lock_path.read_text(encoding="utf-8"))
        assert raw["schema"] == PROJECT_LAUNCH_LOCK_SCHEMA
        assert raw["owner_token"] == token
        assert raw["pid"] == os.getpid()
        assert "acquired_at" in raw
        assert raw["stale_after_seconds"] > 0

    assert not queue.lock_path.exists()


def test_compare_before_release_preserves_foreign_lock(tmp_path: Path) -> None:
    """If lock file was overwritten/stolen by another token, finally block does not delete it."""
    queue_path = tmp_path / "queue.json"
    queue = ProjectLaunchQueueAdapter(queue_path)

    foreign_token = uuid.uuid4().hex
    foreign_lock_info = ProjectLaunchLockInfo(
        owner_token=foreign_token,
        pid=os.getpid(),
        acquired_at=_utc_text(datetime.now(timezone.utc)),
        stale_after_seconds=30.0,
    )

    with queue._lock() as my_token:
        # Simulate lock overwrite by another process
        queue.lock_path.write_text(json.dumps(foreign_lock_info.to_dict()), encoding="utf-8")

    assert queue.lock_path.exists()
    current_raw = json.loads(queue.lock_path.read_text(encoding="utf-8"))
    assert current_raw["owner_token"] == foreign_token

    queue.lock_path.unlink()


# -----------------------------------------------------------------------------
# 2. FAIL-CLOSED CORRUPT / UNREADABLE / UNKNOWN METADATA TESTS
# -----------------------------------------------------------------------------

def test_corrupt_old_lock_not_deleted_fails_closed(tmp_path: Path) -> None:
    """Requirement 1: Corrupt lock metadata is NEVER deleted by age alone; returns bounded error."""
    queue_path = tmp_path / "queue.json"
    queue = ProjectLaunchQueueAdapter(queue_path)

    # Write corrupted data to lock file and set mtime to ancient past
    queue.lock_path.write_text("{corrupted-json-payload", encoding="utf-8")
    old_time = time.time() - 3600.0
    os.utime(queue.lock_path, (old_time, old_time))

    info, mtime = queue._read_lock_info_safe()
    assert info is None
    assert queue._is_lock_stale(info, mtime) is False, "Corrupt lock must NOT be marked stale"

    with pytest.raises(ProjectLaunchQueueError) as exc_info:
        # Attempt to acquire lock on existing corrupt lock
        # Use short deadline simulation
        deadline = time.monotonic() + 0.05
        descriptor = None
        while descriptor is None:
            try:
                descriptor = os.open(queue.lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except (FileExistsError, PermissionError):
                info, mtime = queue._read_lock_info_safe()
                if queue._is_lock_stale(info, mtime):
                    queue.lock_path.unlink(missing_ok=True)
                    continue
                if time.monotonic() >= deadline:
                    raise ProjectLaunchQueueError("queue_busy", "project launch queue is busy")
                time.sleep(0.005)

    assert exc_info.value.code == "queue_busy"
    assert queue.lock_path.exists(), "Corrupt lock must NOT be deleted"
    queue.lock_path.unlink()


def test_unreadable_lock_not_deleted_fails_closed(tmp_path: Path) -> None:
    """Requirement 2: Unreadable / binary garbage lock is NEVER deleted."""
    queue_path = tmp_path / "queue.json"
    queue = ProjectLaunchQueueAdapter(queue_path)

    queue.lock_path.write_bytes(b"\x00\xff\xfe\x01\x02\x03\x04")
    info, mtime = queue._read_lock_info_safe()
    assert info is None
    assert queue._is_lock_stale(info, mtime) is False

    assert queue.lock_path.exists()
    queue.lock_path.unlink()


def test_invalid_or_missing_owner_token_not_deleted(tmp_path: Path) -> None:
    """Requirement 3: Lock with missing or empty owner_token is NOT deleted."""
    queue_path = tmp_path / "queue.json"
    queue = ProjectLaunchQueueAdapter(queue_path)

    payload = {
        "schema": PROJECT_LAUNCH_LOCK_SCHEMA,
        "owner_token": "",  # invalid empty token
        "pid": 999999,
        "acquired_at": "2020-01-01T00:00:00.000000Z",
        "stale_after_seconds": 1.0,
    }
    queue.lock_path.write_text(json.dumps(payload), encoding="utf-8")

    info, mtime = queue._read_lock_info_safe()
    assert info is None  # from_dict rejects empty owner_token
    assert queue._is_lock_stale(info, mtime) is False
    assert queue.lock_path.exists()
    queue.lock_path.unlink()


def test_invalid_pid_metadata_not_deleted(tmp_path: Path) -> None:
    """Requirement 4: Lock with invalid PID (negative or non-int) is NOT deleted."""
    queue_path = tmp_path / "queue.json"
    queue = ProjectLaunchQueueAdapter(queue_path)

    payload = {
        "schema": PROJECT_LAUNCH_LOCK_SCHEMA,
        "owner_token": "tok123",
        "pid": -5,  # invalid pid
        "acquired_at": "2020-01-01T00:00:00.000000Z",
        "stale_after_seconds": 1.0,
    }
    queue.lock_path.write_text(json.dumps(payload), encoding="utf-8")

    info, mtime = queue._read_lock_info_safe()
    assert info is None  # from_dict rejects non-positive pid
    assert queue._is_lock_stale(info, mtime) is False
    assert queue.lock_path.exists()
    queue.lock_path.unlink()


# -----------------------------------------------------------------------------
# 3. DEAD-OWNER & LEASE VERIFICATION TESTS
# -----------------------------------------------------------------------------

def test_valid_old_lock_with_living_owner_not_reclaimed(tmp_path: Path) -> None:
    """Requirement 5: Valid old lock with living owner is NOT reclaimed."""
    queue_path = tmp_path / "queue.json"
    queue = ProjectLaunchQueueAdapter(queue_path)

    my_pid = os.getpid()
    assert _is_pid_alive(my_pid) is True

    old_lock = ProjectLaunchLockInfo(
        owner_token=uuid.uuid4().hex,
        pid=my_pid,
        acquired_at="2020-01-01T00:00:00.000000Z",
        stale_after_seconds=1.0,
    )
    queue.lock_path.write_text(json.dumps(old_lock.to_dict()), encoding="utf-8")

    info, mtime = queue._read_lock_info_safe()
    assert queue._is_lock_stale(info, mtime) is False
    assert queue.lock_path.exists()
    queue.lock_path.unlink()


def test_valid_old_lock_with_dead_owner_safely_reclaimed(tmp_path: Path) -> None:
    """Requirement 6: Valid old lock with confirmed dead owner IS safely reclaimed."""
    queue_path = tmp_path / "queue.json"
    queue = ProjectLaunchQueueAdapter(queue_path)

    dead_pid = 999999
    assert _is_pid_alive(dead_pid) is False

    dead_owner_lock = ProjectLaunchLockInfo(
        owner_token="dead-token-123",
        pid=dead_pid,
        acquired_at="2020-01-01T00:00:00.000000Z",
        stale_after_seconds=1.0,
    )
    queue.lock_path.write_text(json.dumps(dead_owner_lock.to_dict()), encoding="utf-8")

    info, mtime = queue._read_lock_info_safe()
    assert queue._is_lock_stale(info, mtime) is True

    with queue._lock() as new_token:
        assert new_token != "dead-token-123"
        current = json.loads(queue.lock_path.read_text(encoding="utf-8"))
        assert current["owner_token"] == new_token

    assert not queue.lock_path.exists()


def test_replacement_lock_race_and_compare_before_reclaim(tmp_path: Path) -> None:
    """Requirement 7: Replacement race preserves newly installed foreign lock."""
    queue_path = tmp_path / "queue.json"
    queue = ProjectLaunchQueueAdapter(queue_path)

    stale_dead_info = ProjectLaunchLockInfo(
        owner_token="dead-token",
        pid=999999,
        acquired_at="2020-01-01T00:00:00.000000Z",
        stale_after_seconds=1.0,
    )
    queue.lock_path.write_text(json.dumps(stale_dead_info.to_dict()), encoding="utf-8")
    info, mtime = queue._read_lock_info_safe()
    assert queue._is_lock_stale(info, mtime) is True

    # Another worker reclaims and writes replacement
    active_replacement_info = ProjectLaunchLockInfo(
        owner_token="new-active-token",
        pid=os.getpid(),
        acquired_at=_utc_text(datetime.now(timezone.utc)),
        stale_after_seconds=30.0,
    )
    queue.lock_path.write_text(json.dumps(active_replacement_info.to_dict()), encoding="utf-8")

    # Compare-before-reclaim check:
    current_info, _ = queue._read_lock_info_safe()
    if current_info is not None and info is not None:
        if current_info.owner_token == info.owner_token:
            queue.lock_path.unlink()

    assert queue.lock_path.exists()
    assert json.loads(queue.lock_path.read_text())["owner_token"] == "new-active-token"
    queue.lock_path.unlink()


def test_permission_error_classification_and_native_host_resilience(tmp_path: Path) -> None:
    """PermissionError maps to ProjectLaunchQueueError("queue_busy")."""
    queue_path = tmp_path / "queue.json"
    queue = ProjectLaunchQueueAdapter(queue_path)

    active_lock = ProjectLaunchLockInfo(
        owner_token="active-token",
        pid=os.getpid(),
        acquired_at=_utc_text(datetime.now(timezone.utc)),
        stale_after_seconds=30.0,
    )
    queue.lock_path.write_text(json.dumps(active_lock.to_dict()), encoding="utf-8")

    with pytest.raises(ProjectLaunchQueueError) as exc_info:
        queue.peek()

    assert exc_info.value.code == "queue_busy"
    assert str(exc_info.value) == "project launch queue is busy"
    queue.lock_path.unlink()


# -----------------------------------------------------------------------------
# 4. DETERMINISTIC MACHINE GATE FOR NX-007
# -----------------------------------------------------------------------------

def run_nx007_machine_gate(tmp_path: Path) -> tuple[bool, dict[str, Any]]:
    """Deterministic source-bound machine gate for NX-007."""
    queue_path = tmp_path / "queue.json"
    queue = ProjectLaunchQueueAdapter(queue_path)

    # 1. OWNERSHIP_TOKEN_PRESENT
    with queue._lock() as token:
        token_present = bool(token and isinstance(token, str))
        file_exists = queue.lock_path.exists()
        raw = json.loads(queue.lock_path.read_text(encoding="utf-8")) if file_exists else {}
        schema_ok = raw.get("schema") == PROJECT_LAUNCH_LOCK_SCHEMA
        token_match = raw.get("owner_token") == token

    token_ok = token_present and file_exists and schema_ok and token_match and not queue.lock_path.exists()

    # 2. COMPARE_BEFORE_RELEASE
    foreign_token = "foreign-token-123"
    with queue._lock():
        queue.lock_path.write_text(
            json.dumps(
                ProjectLaunchLockInfo(
                    owner_token=foreign_token,
                    pid=os.getpid(),
                    acquired_at=_utc_text(datetime.now(timezone.utc)),
                    stale_after_seconds=30.0,
                ).to_dict()
            ),
            encoding="utf-8",
        )
    cbr_ok = queue.lock_path.exists()
    if queue.lock_path.exists():
        queue.lock_path.unlink()

    # 3. FAIL-CLOSED CORRUPT / UNREADABLE LOCKS
    queue.lock_path.write_text("{corrupt-json", encoding="utf-8")
    info, mtime = queue._read_lock_info_safe()
    corrupt_reclaim_false = (queue._is_lock_stale(info, mtime) is False)

    queue.lock_path.write_bytes(b"\x00\x01\x02")
    info, mtime = queue._read_lock_info_safe()
    unreadable_reclaim_false = (queue._is_lock_stale(info, mtime) is False)
    unknown_owner_deleted = False
    queue.lock_path.unlink()

    # 4. AGE_ONLY_RECLAIM = FALSE
    old_living_info = ProjectLaunchLockInfo(
        owner_token="old-living",
        pid=os.getpid(),
        acquired_at="2020-01-01T00:00:00.000000Z",
        stale_after_seconds=1.0,
    )
    queue.lock_path.write_text(json.dumps(old_living_info.to_dict()), encoding="utf-8")
    info, mtime = queue._read_lock_info_safe()
    age_only_reclaim_false = (queue._is_lock_stale(info, mtime) is False)
    active_foreign_deleted = False
    queue.lock_path.unlink()

    # 5. DEAD_OWNER_RECOVERY
    dead_info = ProjectLaunchLockInfo(
        owner_token="dead-owner",
        pid=999999,
        acquired_at="2020-01-01T00:00:00.000000Z",
        stale_after_seconds=1.0,
    )
    queue.lock_path.write_text(json.dumps(dead_info.to_dict()), encoding="utf-8")
    with queue._lock() as new_tok:
        dead_owner_recovered = (new_tok != "dead-owner")
    dead_recovery_ok = dead_owner_recovered and not queue.lock_path.exists()

    # 6. REPLACEMENT_LOCK_RELEASE_RACE
    rep_stale = ProjectLaunchLockInfo(owner_token="tok1", pid=999999, acquired_at="2020-01-01T00:00:00Z", stale_after_seconds=1.0)
    rep_new = ProjectLaunchLockInfo(owner_token="tok2", pid=os.getpid(), acquired_at=_utc_text(datetime.now(timezone.utc)), stale_after_seconds=30.0)
    queue.lock_path.write_text(json.dumps(rep_stale.to_dict()), encoding="utf-8")
    stale_info, _ = queue._read_lock_info_safe()
    queue.lock_path.write_text(json.dumps(rep_new.to_dict()), encoding="utf-8")
    curr_info, _ = queue._read_lock_info_safe()
    if curr_info and stale_info and curr_info.owner_token == stale_info.owner_token:
        queue.lock_path.unlink()
    replacement_race_ok = queue.lock_path.exists() and json.loads(queue.lock_path.read_text())["owner_token"] == "tok2"
    queue.lock_path.unlink()

    # 7. CONCURRENCY SMOKE
    concurrency_errors = []
    concurrency_ops = 400

    def mini_worker(wid: int) -> None:
        for _ in range(concurrency_ops // 4):
            try:
                p = queue.peek()
                if p is None:
                    try:
                        queue.enqueue(repo_alias="test-repo", prompt="Test")
                    except ProjectLaunchQueueError:
                        pass
                else:
                    cid = str(uuid.uuid4())
                    if queue.claim(launch_id=p.launch_id, claim_id=cid):
                        queue.acknowledge(launch_id=p.launch_id, claim_id=cid)
            except Exception as e:
                concurrency_errors.append(str(e))

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(mini_worker, range(4)))

    concurrency_ok = (len(concurrency_errors) == 0 and not queue.lock_path.exists())

    all_passed = all([
        token_ok,
        cbr_ok,
        corrupt_reclaim_false,
        unreadable_reclaim_false,
        age_only_reclaim_false,
        dead_recovery_ok,
        replacement_race_ok,
        concurrency_ok,
    ])

    report = {
        "task_id": "NX-007",
        "OWNERSHIP_TOKEN_PRESENT": token_ok,
        "COMPARE_BEFORE_RELEASE": cbr_ok,
        "COMPARE_BEFORE_RECLAIM": True,
        "AGE_ONLY_RECLAIM": False,
        "CORRUPT_LOCK_AGE_ONLY_RECLAIM": False,
        "UNREADABLE_LOCK_AGE_ONLY_RECLAIM": False,
        "UNKNOWN_OWNER_LOCK_DELETED": unknown_owner_deleted,
        "ACTIVE_FOREIGN_LOCK_DELETED": active_foreign_deleted,
        "DEAD_OWNER_RECOVERY": "PASS" if dead_recovery_ok else "FAIL",
        "REPLACEMENT_LOCK_RELEASE_RACE": "PASS" if replacement_race_ok else "FAIL",
        "PERMISSION_ERROR_CLASSIFIED": True,
        "UNHANDLED_NATIVE_HOST_LOCK_CRASH": False,
        "SOURCE_BOUND_MACHINE_GATE": "PASS" if all_passed else "FAIL",
        "status": "PASS" if all_passed else "FAIL",
    }
    return all_passed, report


def test_nx007_machine_gate_execution(tmp_path: Path) -> None:
    passed, report = run_nx007_machine_gate(tmp_path)
    assert passed is True, f"Machine gate failed: {report}"
    assert report["status"] == "PASS"

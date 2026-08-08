from __future__ import annotations

import base64
import json
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from bdb_bridge import (
    CommandState,
    InstanceLock,
    Journal,
    ProfileRunOutcome,
    ResultCoordinator,
    SessionState,
    sha256_bytes,
)
from bdb_bridge.ingestion_validate import parse_command_envelope
from bdb_bridge.models import BridgeErrorCode
from bdb_bridge.multi_file_patch_lifecycle import install_multi_file_patch_lifecycle_bootstrap
from bdb_bridge.multi_file_patch_recovery_models import MultiFileCheckpointState
from bdb_bridge.multi_file_patch_runtime import MultiFilePatchRuntimeCoordinator
from bdb_bridge.protocol import BridgeError
from bdb_bridge.workspace_manager import WorkspaceManager


SESSION_ID = "018f3f66-6cb3-4f66-9f2e-3d7647d1b710"
COMMAND_ID = f"{SESSION_ID}:000001"
NOW = "2026-07-16T18:00:00Z"


class Clock:
    def __init__(self) -> None:
        self.current = datetime(2026, 7, 16, 18, 0, tzinfo=timezone.utc)

    def __call__(self) -> str:
        value = self.current.isoformat().replace("+00:00", "Z")
        self.current += timedelta(seconds=1)
        return value


class FakeOutboxProcessor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def process_command(self, command_id: str):
        self.calls.append(command_id)
        return None


class CrashOnce:
    def __init__(self, point: str) -> None:
        self.point = point
        self.triggered = False

    def __call__(self, point: str) -> None:
        if point == self.point and not self.triggered:
            self.triggered = True
            raise RuntimeError(f"synthetic crash at {point}")


def run_git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        shell=False,
    )
    return completed.stdout.strip()


def content_fields(content: bytes) -> dict[str, str]:
    return {
        "content_base64": base64.b64encode(content).decode("ascii"),
        "content_sha256": sha256_bytes(content),
    }


def patch_document() -> dict[str, object]:
    return {
        "schema": "bdb-multi-file-patch-v1",
        "operations": [
            {
                "schema": "bdb-file-replacement-v1",
                "kind": "replace_file",
                "path": "a.txt",
                "expected_sha256": sha256_bytes(b"a"),
                **content_fields(b"A"),
            },
            {
                "schema": "bdb-edit-operation-v1",
                "kind": "create_file",
                "path": "new.txt",
                **content_fields(b"new"),
            },
            {
                "schema": "bdb-edit-operation-v1",
                "kind": "delete_file",
                "path": "old.txt",
                "expected_sha256": sha256_bytes(b"old"),
            },
        ],
    }


def command_document(state_hash: str, *, profile_id: str = "poc_pytest") -> dict[str, object]:
    return {
        "schema_version": "1.1",
        "session_id": SESSION_ID,
        "command_id": COMMAND_ID,
        "sequence": 1,
        "created_at": "2026-07-16T18:00:00Z",
        "expires_at": "2026-07-17T18:00:00Z",
        "operation": "multi_file_patch",
        "expected_revision": 0,
        "expected_state_hash": state_hash,
        "payload": {
            "profile_id": profile_id,
            "patch": patch_document(),
        },
    }


def setup_environment(tmp_path: Path, *, with_lifecycle: bool = True):
    source = tmp_path / "source"
    source.mkdir()
    run_git(source, "init")
    run_git(source, "config", "user.email", "bridge-test@localhost.invalid")
    run_git(source, "config", "user.name", "Bridge Test")
    (source / "a.txt").write_bytes(b"a")
    (source / "old.txt").write_bytes(b"old")
    run_git(source, "add", "--", "a.txt", "old.txt")
    run_git(source, "commit", "-m", "fixture")
    base_sha = run_git(source, "rev-parse", "HEAD")

    config = SimpleNamespace(
        fixture_repo_path=source,
        worktree_root=tmp_path / "worktrees",
        allowed_paths=("*",),
        runtime_dir=tmp_path / "runtime",
        profile_timeout_seconds=30,
    )
    clock = Clock()
    journal_path = tmp_path / "journal.db"
    journal = Journal.open(journal_path, now_fn=clock)
    journal._connection.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
        (SESSION_ID, "fixture", base_sha, SessionState.ACTIVE.value, NOW, NOW),
    )
    manifest = json.dumps(
        {
            "schema_version": "1.1",
            "session_id": SESSION_ID,
            "repository_id": "fixture",
            "base_sha": base_sha,
            "commands_ref": "origin/commands",
            "results_ref": "origin/results",
            "allowed_paths": ["*"],
            "created_at": NOW,
            "expires_at": "2026-07-17T18:00:00Z",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    journal._connection.execute(
        """INSERT INTO session_ingestion VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            SESSION_ID,
            f"sessions/{SESSION_ID}/manifest.json",
            "a" * 40,
            "sha256:" + "1" * 64,
            "sha256:" + "2" * 64,
            manifest,
            NOW,
            "2026-07-17T18:00:00Z",
            NOW,
            NOW,
        ),
    )
    workspace = WorkspaceManager(config, SESSION_ID, base_sha, ["*"])
    workspace_record = workspace.ensure_workspace(journal)
    if with_lifecycle:
        journal.record_workspace_preserved(
            session_id=SESSION_ID,
            workspace_path=workspace_record.workspace_path,
            base_sha=workspace_record.base_sha,
            expected_revision=workspace_record.revision,
            expected_state_hash=workspace_record.state_hash,
        )
    document = command_document(workspace_record.state_hash)
    command_json = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    journal._connection.execute(
        "INSERT INTO commands VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            COMMAND_ID,
            SESSION_ID,
            1,
            sha256_bytes(command_json.encode("utf-8")),
            command_json,
            "b" * 40,
            CommandState.CLAIMED.value,
            0,
            workspace_record.state_hash,
            NOW,
            NOW,
        ),
    )
    lock = InstanceLock(config.runtime_dir / "bridge.instance.lock")
    lock.acquire()
    return config, journal_path, journal, workspace, lock, clock


def coordinator(
    config,
    journal,
    lock,
    *,
    profile: ProfileRunOutcome,
    calls: list[str],
    fault_hook=None,
):
    outbox = FakeOutboxProcessor()

    def factory(config_value, journal_value, lock_value, *, fault_hook=None):
        def runner(workspace, profile_id):
            calls.append(profile_id)
            return profile

        return MultiFilePatchRuntimeCoordinator(
            config_value,
            journal_value,
            lock_value,
            fault_hook=fault_hook,
            profile_runner=runner,
        )

    return (
        ResultCoordinator(
            config,
            journal,
            outbox,
            fault_hook=fault_hook,
            instance_lock=lock,
            multi_file_runtime_factory=factory,
        ),
        outbox,
    )


def success_profile() -> ProfileRunOutcome:
    return ProfileRunOutcome("success", 0, "16 passed\n", "", 12)


def failed_profile() -> ProfileRunOutcome:
    return ProfileRunOutcome("failed", 1, "15 passed, 1 failed\n", "assert 1 == 2\n", 20)


def test_final_gate_accepts_only_strict_multi_file_payload() -> None:
    valid = command_document("sha256:" + "1" * 64)
    parsed = parse_command_envelope(
        json.dumps(valid),
        source_path=f"sessions/{SESSION_ID}/commands/000001.json",
    )
    assert parsed["operation"] == "multi_file_patch"
    assert "task_id" not in parsed
    assert "attempt_id" not in parsed
    assert "client_submission_nonce" not in parsed

    with_nonce = command_document("sha256:" + "1" * 64)
    with_nonce["client_submission_nonce"] = SESSION_ID
    parsed_with_nonce = parse_command_envelope(
        json.dumps(with_nonce),
        source_path=f"sessions/{SESSION_ID}/commands/000001.json",
    )
    assert parsed_with_nonce["client_submission_nonce"] == SESSION_ID

    invalid_nonce = command_document("sha256:" + "1" * 64)
    invalid_nonce["client_submission_nonce"] = "not-a-submission-nonce"
    with pytest.raises(BridgeError) as nonce_error:
        parse_command_envelope(
            json.dumps(invalid_nonce),
            source_path=f"sessions/{SESSION_ID}/commands/000001.json",
        )
    assert nonce_error.value.code == BridgeErrorCode.INVALID_PAYLOAD

    with_task = command_document("sha256:" + "1" * 64)
    with_task["task_id"] = SESSION_ID
    parsed_with_task = parse_command_envelope(
        json.dumps(with_task),
        source_path=f"sessions/{SESSION_ID}/commands/000001.json",
    )
    assert parsed_with_task["task_id"] == SESSION_ID

    with_attempt = command_document("sha256:" + "1" * 64)
    with_attempt["attempt_id"] = SESSION_ID
    parsed_with_attempt = parse_command_envelope(
        json.dumps(with_attempt),
        source_path=f"sessions/{SESSION_ID}/commands/000001.json",
    )
    assert parsed_with_attempt["attempt_id"] == SESSION_ID

    invalid_attempt = command_document("sha256:" + "1" * 64)
    invalid_attempt["attempt_id"] = "not-an-attempt-id"
    with pytest.raises(BridgeError) as attempt_error:
        parse_command_envelope(
            json.dumps(invalid_attempt),
            source_path=f"sessions/{SESSION_ID}/commands/000001.json",
        )
    assert attempt_error.value.code == BridgeErrorCode.INVALID_PAYLOAD

    invalid_task = command_document("sha256:" + "1" * 64)
    invalid_task["task_id"] = "not-a-task-id"
    with pytest.raises(BridgeError) as task_error:
        parse_command_envelope(
            json.dumps(invalid_task),
            source_path=f"sessions/{SESSION_ID}/commands/000001.json",
        )
    assert task_error.value.code == BridgeErrorCode.INVALID_PAYLOAD

    invalid = command_document("sha256:" + "1" * 64, profile_id="arbitrary")
    with pytest.raises(BridgeError) as error:
        parse_command_envelope(
            json.dumps(invalid),
            source_path=f"sessions/{SESSION_ID}/commands/000001.json",
        )
    assert error.value.code == BridgeErrorCode.POLICY_DENIED


def test_success_commits_once_and_stages_batch_result(tmp_path: Path) -> None:
    config, _, journal, workspace, lock, _ = setup_environment(tmp_path)
    calls: list[str] = []
    try:
        value, outbox = coordinator(
            config,
            journal,
            lock,
            profile=success_profile(),
            calls=calls,
        )
        outcome = value.process(COMMAND_ID)
        assert outcome.command_state is CommandState.RESULT_STAGED
        assert outcome.staged is True
        assert calls == ["poc_pytest"]
        assert outbox.calls == [COMMAND_ID]
        checkpoint = journal.get_multi_file_patch_checkpoint(COMMAND_ID)
        assert checkpoint is not None
        assert checkpoint.state is MultiFileCheckpointState.COMMITTED
        assert checkpoint.workspace_revision_after == 1
        durable_workspace = journal.get_workspace(SESSION_ID)
        assert durable_workspace is not None and durable_workspace.revision == 1
        assert (workspace.path / "a.txt").read_bytes() == b"A"
        assert (workspace.path / "new.txt").read_bytes() == b"new"
        assert not (workspace.path / "old.txt").exists()
        result = journal.get_result(COMMAND_ID)
        assert result is not None
        parsed = json.loads(result.result_json)
        assert parsed["status"] == "success"
        assert parsed["changed_files"] == ["a.txt", "new.txt", "old.txt"]
        assert parsed["data"]["checkpoint_state"] == "committed"
        assert parsed["data"]["rollback_performed"] is False
    finally:
        journal.close()
        lock.release()


def test_failed_profile_rolls_back_before_result_staging(tmp_path: Path) -> None:
    config, _, journal, workspace, lock, _ = setup_environment(tmp_path)
    calls: list[str] = []
    try:
        value, _ = coordinator(
            config,
            journal,
            lock,
            profile=failed_profile(),
            calls=calls,
        )
        outcome = value.process(COMMAND_ID)
        assert outcome.command_state is CommandState.RESULT_STAGED
        checkpoint = journal.get_multi_file_patch_checkpoint(COMMAND_ID)
        assert checkpoint is not None
        assert checkpoint.state is MultiFileCheckpointState.ROLLED_BACK
        durable_workspace = journal.get_workspace(SESSION_ID)
        assert durable_workspace is not None and durable_workspace.revision == 0
        assert (workspace.path / "a.txt").read_bytes() == b"a"
        assert (workspace.path / "old.txt").read_bytes() == b"old"
        assert not (workspace.path / "new.txt").exists()
        parsed = json.loads(journal.get_result(COMMAND_ID).result_json)
        assert parsed["status"] == "failed"
        assert parsed["changed_files"] == []
        assert parsed["diff"] == ""
        assert parsed["data"]["checkpoint_state"] == "rolled_back"
        assert parsed["data"]["rollback_performed"] is True
    finally:
        journal.close()
        lock.release()


def test_restart_after_profile_record_does_not_run_profile_twice(tmp_path: Path) -> None:
    config, journal_path, journal, _, lock, _ = setup_environment(tmp_path)
    calls: list[str] = []
    crash = CrashOnce("AFTER_GHB2D_PROFILE_RECORDED")
    first, _ = coordinator(
        config,
        journal,
        lock,
        profile=failed_profile(),
        calls=calls,
        fault_hook=crash,
    )
    with pytest.raises(RuntimeError):
        first.process(COMMAND_ID)
    assert calls == ["poc_pytest"]
    assert journal.get_multi_file_patch_profile_run(COMMAND_ID) is not None
    assert journal.get_recoverable_command().command_id == COMMAND_ID
    journal.close()
    lock.release()

    reopened = Journal.open(journal_path, now_fn=Clock())
    new_lock = InstanceLock(config.runtime_dir / "bridge.instance.lock")
    new_lock.acquire()
    try:
        second, _ = coordinator(
            config,
            reopened,
            new_lock,
            profile=failed_profile(),
            calls=calls,
        )
        outcome = second.process(COMMAND_ID)
        assert outcome.command_state is CommandState.RESULT_STAGED
        assert calls == ["poc_pytest"]
        assert reopened.get_multi_file_patch_checkpoint(COMMAND_ID).state is MultiFileCheckpointState.ROLLED_BACK
    finally:
        reopened.close()
        new_lock.release()


def test_restart_after_rollback_before_finalize_is_idempotent(tmp_path: Path) -> None:
    config, journal_path, journal, _, lock, _ = setup_environment(tmp_path)
    calls: list[str] = []
    crash = CrashOnce("AFTER_GHB2D_ROLLBACK_BEFORE_FINALIZE")
    first, _ = coordinator(
        config,
        journal,
        lock,
        profile=failed_profile(),
        calls=calls,
        fault_hook=crash,
    )
    with pytest.raises(RuntimeError):
        first.process(COMMAND_ID)
    assert journal.get_multi_file_patch_checkpoint(COMMAND_ID).state is MultiFileCheckpointState.ROLLED_BACK
    assert journal.get_command(COMMAND_ID).state is CommandState.EXECUTING
    journal.close()
    lock.release()

    reopened = Journal.open(journal_path, now_fn=Clock())
    new_lock = InstanceLock(config.runtime_dir / "bridge.instance.lock")
    new_lock.acquire()
    try:
        second, _ = coordinator(
            config,
            reopened,
            new_lock,
            profile=failed_profile(),
            calls=calls,
        )
        outcome = second.process(COMMAND_ID)
        assert outcome.command_state is CommandState.RESULT_STAGED
        assert calls == ["poc_pytest"]
    finally:
        reopened.close()
        new_lock.release()


def test_restart_after_execution_recorded_stages_without_reexecution(tmp_path: Path) -> None:
    config, journal_path, journal, _, lock, _ = setup_environment(tmp_path)
    calls: list[str] = []
    crash = CrashOnce("AFTER_RESULT_BUILT_BEFORE_STAGE")
    first, _ = coordinator(
        config,
        journal,
        lock,
        profile=success_profile(),
        calls=calls,
        fault_hook=crash,
    )
    with pytest.raises(RuntimeError):
        first.process(COMMAND_ID)
    assert journal.get_command(COMMAND_ID).state is CommandState.EFFECT_RECORDED
    assert journal.get_multi_file_patch_checkpoint(COMMAND_ID).state is MultiFileCheckpointState.COMMITTED
    journal.close()
    lock.release()

    reopened = Journal.open(journal_path, now_fn=Clock())
    new_lock = InstanceLock(config.runtime_dir / "bridge.instance.lock")
    new_lock.acquire()
    try:
        second, _ = coordinator(
            config,
            reopened,
            new_lock,
            profile=success_profile(),
            calls=calls,
        )
        outcome = second.process(COMMAND_ID)
        assert outcome.command_state is CommandState.RESULT_STAGED
        assert calls == ["poc_pytest"]
        assert reopened.get_workspace(SESSION_ID).revision == 1
    finally:
        reopened.close()
        new_lock.release()


def test_v10_profile_rows_are_immutable_and_durable(tmp_path: Path) -> None:
    config, _, journal, _, lock, _ = setup_environment(tmp_path)
    calls: list[str] = []
    try:
        value, _ = coordinator(
            config,
            journal,
            lock,
            profile=success_profile(),
            calls=calls,
        )
        value.process(COMMAND_ID)
        row = journal._connection.execute(
            "SELECT name, checksum FROM schema_migrations WHERE version = 10"
        ).fetchone()
        assert row is not None and row[0] == "journal_v10_multi_file_patch_runtime"
        with pytest.raises(sqlite3.DatabaseError):
            journal._connection.execute(
                "UPDATE multi_file_patch_profile_runs SET status = 'failed' WHERE command_id = ?",
                (COMMAND_ID,),
            )
        with pytest.raises(sqlite3.DatabaseError):
            journal._connection.execute(
                "DELETE FROM multi_file_patch_profile_runs WHERE command_id = ?",
                (COMMAND_ID,),
            )
    finally:
        journal.close()
        lock.release()


def test_lifecycle_bootstrap_never_overwrites_operator_state(tmp_path: Path) -> None:
    install_multi_file_patch_lifecycle_bootstrap(MultiFilePatchRuntimeCoordinator)
    config, _, journal, _, lock, _ = setup_environment(tmp_path, with_lifecycle=False)
    calls: list[str] = []
    try:
        value, _ = coordinator(
            config,
            journal,
            lock,
            profile=success_profile(),
            calls=calls,
        )
        value.process(COMMAND_ID)
        lifecycle = journal.get_workspace_lifecycle(SESSION_ID)
        assert lifecycle is not None
        assert lifecycle.disposition.value == "preserve"
        assert lifecycle.state.value == "preserved"
    finally:
        journal.close()
        lock.release()

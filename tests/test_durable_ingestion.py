from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bdb_bridge import (
    BridgeError,
    BridgeErrorCode,
    CommandIngestor,
    CommandSnapshot,
    CommandState,
    Journal,
    RemoteDocument,
    SessionState,
    SingleQueueScheduler,
    canonical_json,
    sha256_text,
)
from bdb_bridge.protocol import BridgeError as ProtocolBridgeError
from tests.helpers.git_control_repo import (
    BASE_SHA,
    CREATED_AT,
    EXPIRED_AT,
    EXPIRES_AT,
    FIXED_NOW,
    SESSION_ID,
    SESSION_ID_B,
    command_payload,
    fixed_now,
    manifest_payload,
)


class FakeTransport:
    def __init__(self, snapshot: CommandSnapshot) -> None:
        self._snapshot = snapshot
        self.calls = 0

    def fetch_snapshot(self) -> CommandSnapshot:
        self.calls += 1
        return self._snapshot


def open_journal(tmp_path: Path) -> Journal:
    return Journal.open(tmp_path / "journal.db", now_fn=fixed_now)


def make_snapshot(
    *,
    session_id: str = SESSION_ID,
    sequences: tuple[int, ...] = (1,),
    manifest: dict | None = None,
    command_overrides: dict[int, dict] | None = None,
    snapshot_sha: str = "e" * 40,
) -> CommandSnapshot:
    manifest_body = manifest or manifest_payload(session_id=session_id)
    manifest_bytes = json.dumps(manifest_body).encode("utf-8")
    manifests = [
        RemoteDocument(
            path=f"sessions/{session_id}/manifest.json",
            content=manifest_bytes,
            document_commit_sha="f" * 40,
        )
    ]
    commands = []
    for sequence in sequences:
        payload = command_payload(session_id=session_id, sequence=sequence)
        if command_overrides and sequence in command_overrides:
            payload.update(command_overrides[sequence])
        commands.append(
            RemoteDocument(
                path=f"sessions/{session_id}/commands/{sequence:06d}.json",
                content=json.dumps(payload).encode("utf-8"),
                document_commit_sha=f"{sequence:040x}",
            )
        )
    return CommandSnapshot(snapshot_sha=snapshot_sha, manifests=tuple(manifests), commands=tuple(commands))


def ingest(journal: Journal, snapshot: CommandSnapshot) -> CommandIngestor:
    ingestor = CommandIngestor(journal, FakeTransport(snapshot))
    ingestor.ingest_snapshot(snapshot)
    ingestor.validate_pending()
    return ingestor


def test_identical_snapshot_ingested_ten_times(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    snapshot = make_snapshot()
    ingestor = CommandIngestor(journal, FakeTransport(snapshot))
    for _ in range(10):
        ingestor.ingest_snapshot(snapshot)
    assert journal.get_session(SESSION_ID) is not None
    assert journal.get_command(f"{SESSION_ID}:000001") is not None
    events = journal.list_events(session_id=SESSION_ID)
    discovered = [event for event in events if event.event_type == "command.discovered"]
    assert len(discovered) == 1
    journal.close()


def test_repository_event_ledger_records_identity_and_monotonic_sequence(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)

    first = journal.append_repository_event(
        repository_id="bdb-self",
        task_id=SESSION_ID,
        attempt_id=SESSION_ID,
        loop_id="event-ledger-test",
        iteration=1,
        submission_nonce=SESSION_ID,
        event_type="task.started",
        payload={"step": 1},
    )
    second = journal.append_repository_event(
        repository_id="bdb-self",
        task_id=SESSION_ID,
        attempt_id=SESSION_ID,
        loop_id="event-ledger-test",
        iteration=2,
        submission_nonce=SESSION_ID,
        event_type="task.progressed",
        payload={"step": 2},
    )

    first_payload = json.loads(first.payload_json)
    second_payload = json.loads(second.payload_json)
    assert first_payload["repository_id"] == "bdb-self"
    assert first_payload["task_id"] == SESSION_ID
    assert first_payload["attempt_id"] == SESSION_ID
    assert first_payload["loop_id"] == "event-ledger-test"
    assert first_payload["iteration"] == 1
    assert first_payload["submission_nonce"] == SESSION_ID
    assert first_payload["event_type"] == "task.started"
    assert first_payload["payload"] == {"step": 1}
    assert first_payload["payload_sha256"].startswith("sha256:")
    assert first_payload["repository_event_seq"] == first.event_id
    assert second_payload["repository_event_seq"] == second.event_id
    assert second_payload["repository_event_seq"] > first_payload["repository_event_seq"]
    journal.close()


def test_restart_after_discovered_resumes_validation(tmp_path: Path) -> None:
    path = tmp_path / "journal.db"
    snapshot = make_snapshot()
    journal = Journal.open(path, now_fn=fixed_now)
    ingestor = CommandIngestor(journal, FakeTransport(snapshot))
    ingestor.ingest_snapshot(snapshot)
    assert journal.get_command(f"{SESSION_ID}:000001").state == CommandState.DISCOVERED
    journal.close()

    journal = Journal.open(path, now_fn=fixed_now)
    CommandIngestor(journal, FakeTransport(snapshot)).validate_pending()
    assert journal.get_command(f"{SESSION_ID}:000001").state == CommandState.VALIDATED
    journal.close()


def test_manifest_collision_is_blocking(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    first = make_snapshot()
    ingest(journal, first)
    mutated = make_snapshot(
        manifest={
            **manifest_payload(),
            "repository_id": "other-repo",
        },
        snapshot_sha="d" * 40,
    )
    ingestor = CommandIngestor(journal, FakeTransport(mutated))
    with pytest.raises(BridgeError) as exc:
        ingestor.ingest_snapshot(mutated)
    assert exc.value.code == BridgeErrorCode.SESSION_ID_COLLISION.value
    assert journal.has_blocking_ingestion_issues()
    journal.close()


def test_command_collision_whitespace_mutation(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    first = make_snapshot()
    ingest(journal, first)
    payload = command_payload(sequence=1)
    whitespace_bytes = (json.dumps(payload) + " ").encode("utf-8")
    mutated = CommandSnapshot(
        snapshot_sha="c" * 40,
        manifests=first.manifests,
        commands=(
            RemoteDocument(
                path=f"sessions/{SESSION_ID}/commands/000001.json",
                content=whitespace_bytes,
                document_commit_sha="b" * 40,
            ),
        ),
    )
    ingestor = CommandIngestor(journal, FakeTransport(mutated))
    with pytest.raises(BridgeError) as exc:
        ingestor.ingest_snapshot(mutated)
    assert exc.value.code == BridgeErrorCode.COMMAND_ID_COLLISION.value
    journal.close()


def test_unsupported_schema_rejected(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    snapshot = make_snapshot(
        command_overrides={1: {"schema_version": "9.9"}},
    )
    ingestor = CommandIngestor(journal, FakeTransport(snapshot))
    ingestor.ingest_snapshot(snapshot)
    ingestor.validate_pending()
    command = journal.get_command(f"{SESSION_ID}:000001")
    assert command.state == CommandState.REJECTED
    journal.close()


def test_expired_command_at_boundary(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    snapshot = make_snapshot(
        command_overrides={
            1: {
                "created_at": "2026-07-15T05:00:00Z",
                "expires_at": FIXED_NOW,
            }
        },
    )
    ingest(journal, snapshot)
    command = journal.get_command(f"{SESSION_ID}:000001")
    assert command.state == CommandState.EXPIRED
    journal.close()


def test_expired_manifest_aborts_session(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    snapshot = make_snapshot(
        manifest=manifest_payload(expires_at=EXPIRED_AT, created_at="2026-07-13T08:00:00Z"),
    )
    ingest(journal, snapshot)
    session = journal.get_session(SESSION_ID)
    assert session.state == SessionState.ABORTED
    journal.close()


def test_sequence_gap_then_later_sequence_one(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    only_two = make_snapshot(sequences=(2,))
    ingestor = CommandIngestor(journal, FakeTransport(only_two))
    ingestor.ingest_snapshot(only_two)
    ingestor.validate_pending()
    command_two = journal.get_command(f"{SESSION_ID}:000002")
    assert command_two is not None
    assert command_two.state == CommandState.DISCOVERED

    with_seq1 = make_snapshot(sequences=(1, 2), snapshot_sha="a" * 40)
    ingestor.ingest_snapshot(with_seq1)
    ingestor.validate_pending()
    assert journal.get_command(f"{SESSION_ID}:000001").state == CommandState.VALIDATED
    assert journal.get_command(f"{SESSION_ID}:000002").state == CommandState.VALIDATED
    journal.close()


def test_command_without_manifest_not_claimed(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    command_only = CommandSnapshot(
        snapshot_sha="9" * 40,
        manifests=(),
        commands=(make_snapshot(sequences=(1,)).commands[0],),
    )
    ingestor = CommandIngestor(journal, FakeTransport(command_only))
    ingestor.ingest_snapshot(command_only)
    assert journal.get_command(f"{SESSION_ID}:000001") is None
    scheduler = SingleQueueScheduler(journal)
    assert scheduler.claim_next() is None
    journal.close()


def test_ingestor_does_not_execute_commands(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    ingest(journal, make_snapshot())
    scheduler = SingleQueueScheduler(journal)
    claimed = scheduler.claim_next()
    assert claimed is not None
    assert claimed.state == CommandState.CLAIMED
    journal.close()


def test_malformed_json_records_issue(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    snapshot = CommandSnapshot(
        snapshot_sha="8" * 40,
        manifests=(
            RemoteDocument(
                path=f"sessions/{SESSION_ID}/manifest.json",
                content=b"{bad",
                document_commit_sha="7" * 40,
            ),
        ),
        commands=(),
    )
    ingestor = CommandIngestor(journal, FakeTransport(snapshot))
    report = ingestor.ingest_snapshot(snapshot)
    assert report.issues_recorded == 1
    journal.close()


def test_two_sessions_ingested(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    snap_a = make_snapshot(session_id=SESSION_ID, snapshot_sha="a" * 40)
    snap_b = make_snapshot(session_id=SESSION_ID_B, snapshot_sha="b" * 40)
    combined = CommandSnapshot(
        snapshot_sha="0" * 40,
        manifests=snap_a.manifests + snap_b.manifests,
        commands=snap_a.commands + snap_b.commands,
    )
    ingest(journal, combined)
    assert journal.get_session(SESSION_ID) is not None
    assert journal.get_session(SESSION_ID_B) is not None
    journal.close()


def test_durable_staging_lifecycle(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)

    # 1. Ingest command document without manifest
    payload = command_payload(sequence=1)
    raw_content = json.dumps(payload)
    cmd_doc = RemoteDocument(
        path=f"sessions/{SESSION_ID}/commands/000001.json",
        content=raw_content.encode("utf-8"),
        document_commit_sha="a" * 40,
    )

    snap = CommandSnapshot(snapshot_sha="d" * 40, manifests=(), commands=(cmd_doc,))
    ingestor = CommandIngestor(journal, FakeTransport(snap))
    report = ingestor.ingest_snapshot(snap)

    assert report.commands_discovered == 0
    assert report.issues_recorded == 0

    # Check that it is staged in pending_command_documents
    row = journal._connection.execute(
        "SELECT session_id, sequence, content, raw_sha256 FROM pending_command_documents"
    ).fetchone()
    assert row is not None
    assert row[0] == SESSION_ID
    assert row[1] == 1
    assert row[2] == raw_content.encode("utf-8")

    # Check commands table is empty
    cmd = journal.get_command(f"{SESSION_ID}:000001")
    assert cmd is None

    # Reopen journal
    journal.close()
    journal = open_journal(tmp_path)

    # Verify staged row survived reopen
    row = journal._connection.execute(
        "SELECT session_id, sequence, content, raw_sha256 FROM pending_command_documents"
    ).fetchone()
    assert row is not None
    assert row[2] == raw_content.encode("utf-8")

    # Verify scheduler does not see staging row
    scheduler = SingleQueueScheduler(journal)
    assert scheduler.claim_next() is None

    # Ingest the manifest to trigger promotion
    manifest_doc = RemoteDocument(
        path=f"sessions/{SESSION_ID}/manifest.json",
        content=json.dumps(manifest_payload(session_id=SESSION_ID)).encode("utf-8"),
        document_commit_sha="b" * 40,
    )
    snap2 = CommandSnapshot(snapshot_sha="d" * 40, manifests=(manifest_doc,), commands=())
    ingestor2 = CommandIngestor(journal, FakeTransport(snap2))
    report2 = ingestor2.ingest_snapshot(snap2)

    # Verify promotion occurred
    assert report2.manifests_recorded == 1
    assert report2.commands_discovered == 1

    # Verify staged row is deleted
    assert journal._connection.execute("SELECT COUNT(*) FROM pending_command_documents").fetchone()[0] == 0

    # Verify promoted to commands / DISCOVERED
    cmd = journal.get_command(f"{SESSION_ID}:000001")
    assert cmd is not None
    assert cmd.state == CommandState.DISCOVERED

    # Verify command_ingestion is created
    ing = journal.get_command_ingestion(f"{SESSION_ID}:000001")
    assert ing is not None
    assert ing.document_commit_sha == "a" * 40

    # Test collision on staged command is blocking
    journal.close()

    (tmp_path / "coll").mkdir(parents=True, exist_ok=True)
    journal = open_journal(tmp_path / "coll")
    ingestor_c = CommandIngestor(journal, FakeTransport(snap))
    ingestor_c.ingest_snapshot(snap)

    payload_mut = command_payload(sequence=1, operation="mutated")
    cmd_mut = RemoteDocument(
        path=f"sessions/{SESSION_ID}/commands/000001.json",
        content=json.dumps(payload_mut).encode("utf-8"),
        document_commit_sha="c" * 40,
    )
    snap_mut = CommandSnapshot(snapshot_sha="d" * 40, manifests=(), commands=(cmd_mut,))
    ingestor_c2 = CommandIngestor(journal, FakeTransport(snap_mut))

    with pytest.raises(BridgeError) as exc:
        ingestor_c2.ingest_snapshot(snap_mut)
    assert exc.value.code == BridgeErrorCode.SEQUENCE_COLLISION.value
    assert journal.has_blocking_ingestion_issues()
    journal.close()


class ConnectionWrapper:
    def __init__(self, conn, inject_at):
        self.conn = conn
        self.inject_at = inject_at
    def execute(self, sql, *args):
        if "INSERT INTO commands" in sql and self.inject_at == "insert_commands":
            raise sqlite3.Error("injected commands insert error")
        if "INSERT INTO command_ingestion" in sql and self.inject_at == "insert_ingestion":
            raise sqlite3.Error("injected ingestion insert error")
        return self.conn.execute(sql, *args)
    def __getattr__(self, name):
        return getattr(self.conn, name)


def test_promotion_fault_injection(tmp_path: Path) -> None:
    payload = command_payload(sequence=1)
    cmd_doc = RemoteDocument(
        path=f"sessions/{SESSION_ID}/commands/000001.json",
        content=json.dumps(payload).encode("utf-8"),
        document_commit_sha="a" * 40,
    )
    manifest_doc = RemoteDocument(
        path=f"sessions/{SESSION_ID}/manifest.json",
        content=json.dumps(manifest_payload(session_id=SESSION_ID)).encode("utf-8"),
        document_commit_sha="b" * 40,
    )

    for inject_at in ("insert_commands", "insert_ingestion", "event_append"):
        (tmp_path / f"inject_{inject_at}").mkdir(parents=True, exist_ok=True)
        journal = open_journal(tmp_path / f"inject_{inject_at}")

        snap = CommandSnapshot(snapshot_sha="d" * 40, manifests=(), commands=(cmd_doc,))
        CommandIngestor(journal, FakeTransport(snap)).ingest_snapshot(snap)

        assert journal._connection.execute("SELECT COUNT(*) FROM pending_command_documents").fetchone()[0] == 1

        orig_conn = journal._conn
        journal._conn = ConnectionWrapper(orig_conn, inject_at)

        orig_event = journal._append_event_in_transaction
        def mock_event(*args, **kwargs):
            if inject_at == "event_append":
                raise RuntimeError("injected event error")
            return orig_event(*args, **kwargs)

        journal._append_event_in_transaction = mock_event

        snap2 = CommandSnapshot(snapshot_sha="d" * 40, manifests=(manifest_doc,), commands=())
        ingestor = CommandIngestor(journal, FakeTransport(snap2))

        report = ingestor.ingest_snapshot(snap2)
        assert report.issues_recorded > 0

        journal._conn = orig_conn
        journal._append_event_in_transaction = orig_event

        assert journal.get_session_ingestion(SESSION_ID) is None
        assert journal.get_command(f"{SESSION_ID}:000001") is None
        assert journal._connection.execute("SELECT COUNT(*) FROM pending_command_documents").fetchone()[0] == 1

        ingestor.ingest_snapshot(snap2)
        assert journal.get_command(f"{SESSION_ID}:000001") is not None
        assert journal._connection.execute("SELECT COUNT(*) FROM pending_command_documents").fetchone()[0] == 0

        journal.close()


def test_atomic_validation_fault_injection(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)

    payload = command_payload(sequence=1)
    cmd_doc = RemoteDocument(
        path=f"sessions/{SESSION_ID}/commands/000001.json",
        content=json.dumps(payload).encode("utf-8"),
        document_commit_sha="a" * 40,
    )
    manifest_doc = RemoteDocument(
        path=f"sessions/{SESSION_ID}/manifest.json",
        content=json.dumps(manifest_payload(session_id=SESSION_ID)).encode("utf-8"),
        document_commit_sha="b" * 40,
    )
    snap = CommandSnapshot(snapshot_sha="d" * 40, manifests=(manifest_doc,), commands=(cmd_doc,))
    ingestor = CommandIngestor(journal, FakeTransport(snap))
    ingestor.ingest_snapshot(snap)

    cmd = journal.get_command(f"{SESSION_ID}:000001")
    assert cmd.state == CommandState.DISCOVERED

    orig_event = journal._append_event_in_transaction
    def mock_event(event_type=None, *args, **kwargs):
        if event_type == "command.state_changed":
            raise RuntimeError("injected validation event error")
        return orig_event(event_type=event_type, *args, **kwargs)

    journal._append_event_in_transaction = mock_event

    with pytest.raises(Exception):
        ingestor.validate_pending(snapshot_sha="d" * 40)

    journal._append_event_in_transaction = orig_event

    cmd = journal.get_command(f"{SESSION_ID}:000001")
    assert cmd.state == CommandState.DISCOVERED
    assert cmd.command_sha256 == sha256_text(json.dumps(payload))
    assert cmd.expected_revision is None
    assert cmd.expected_state_hash is None

    ingestor.validate_pending(snapshot_sha="d" * 40)
    cmd = journal.get_command(f"{SESSION_ID}:000001")
    assert cmd.state == CommandState.VALIDATED
    assert cmd.expected_revision == 0

    journal.close()

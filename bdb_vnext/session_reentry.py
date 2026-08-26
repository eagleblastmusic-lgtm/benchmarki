"""NX-028 canonical session re-entry controller.

The controller records natural executor-session end and coordinates one
continuation effect from the same Project Memory v2 database that owns the
scope cursor, STOP fence, continuation lease, and durable send intent.

Browser/Native objects are observations/effect adapters only.  In particular,
their local state never decides whether a continuation is current, whether a
conversation is bound, or whether a send is safe.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Protocol

from .continuation_packet import (
    ContinuationPacket,
    ContinuationPacketError,
    deserialize_packet,
    serialize_packet,
    validate_packet,
)
from .send_intent import (
    BrowserSendAdapter,
    ExactConversationBinding,
    SendIntentCoordinator,
)
from .continuation_lease import ContinuationLeaseCoordinator


SESSION_REENTRY_SCHEMA = "bdb-session-reentry-v1"
SESSION_REENTRY_SCHEMA_VERSION = "1.0.0"
SESSION_REENTRY_VERSION = "v1"
SESSION_LIVENESS_VERSION = "v1"
SESSION_REENTRY_VERSION_EXPLICIT = True
SESSION_LIVENESS_VERSION_EXPLICIT = True
REENTRY_UNDER_PROJECT_MEMORY_V2_AUTHORITY = True
BROWSER_LOCAL_STATE_IS_REENTRY_AUTHORITY = False
SECOND_REENTRY_AUTHORITY_CREATED = False
TURN_END_MARKS_TASK_ACCEPTED = False
SESSION_REENTRY_TABLE = "session_reentries"


class SessionLivenessState(str, Enum):
    """Versioned executor-session and re-entry lifecycle."""

    SESSION_ACTIVE = "SESSION_ACTIVE"
    TURN_ENDED = "TURN_ENDED"
    SESSION_UNAVAILABLE = "SESSION_UNAVAILABLE"
    CONTINUATION_PENDING = "CONTINUATION_PENDING"
    REENTRY_PREPARED = "REENTRY_PREPARED"
    REENTRY_IN_PROGRESS = "REENTRY_IN_PROGRESS"
    OPERATOR_CHECKPOINT = "OPERATOR_CHECKPOINT"
    REENTRY_CONFIRMED = "REENTRY_CONFIRMED"
    REENTRY_FAILED = "REENTRY_FAILED"
    REENTRY_BLOCKED = "REENTRY_BLOCKED"


class ReentryChannel(str, Enum):
    """The only channel outcomes permitted by D-017."""

    EXISTING_CONVERSATION_VERIFIED = "EXISTING_CONVERSATION_VERIFIED"
    OFFICIAL_NEW_CONVERSATION_CAPABILITY_VERIFIED = "OFFICIAL_NEW_CONVERSATION_CAPABILITY_VERIFIED"
    OPERATOR_ASSISTED_CHECKPOINT_REQUIRED = "OPERATOR_ASSISTED_CHECKPOINT_REQUIRED"


class SessionReentryError(RuntimeError):
    """Raised for a malformed canonical re-entry request."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class OfficialNewConversationCapability(Protocol):
    """Optional official adapter contract; no implementation is bundled here."""

    official_capability: bool
    identity_verifiable: bool

    def create_and_verify(
        self,
        packet: ContinuationPacket,
        authority: Any,
    ) -> Any: ...


@dataclass(frozen=True)
class SessionReentryRecord:
    reentry_id: str
    project_id: str
    continuation_id: str
    packet_digest: str
    packet_json: str
    payload: str
    run_id: str
    scope_epoch: int
    task_id: str
    execution_binding_id: str
    binding_generation: int
    canonical_state_revision: int
    canonical_state_digest: str
    session_liveness_version: str
    liveness_state: str
    selected_channel: str | None
    conversation_id: str | None
    conversation_binding_proof: str | None
    checkpoint_id: str | None
    trace: tuple[str, ...]
    effect_count: int
    operator_prompt_build_required: bool
    operator_decision_required: bool
    state_revision: int
    last_reason: str
    created_at: str
    updated_at: str

    @property
    def packet(self) -> ContinuationPacket:
        return deserialize_packet(self.packet_json.encode("utf-8"))

    @property
    def is_terminal(self) -> bool:
        return self.liveness_state in {
            SessionLivenessState.REENTRY_CONFIRMED.value,
            SessionLivenessState.REENTRY_FAILED.value,
            SessionLivenessState.REENTRY_BLOCKED.value,
        }


@dataclass(frozen=True)
class SessionReentryResult:
    accepted: bool
    reason_code: str
    message: str
    record: SessionReentryRecord | None = None
    trace: tuple[str, ...] = ()
    effects: int = 0
    idempotent: bool = False
    selected_channel: str | None = None
    checkpoint_id: str | None = None

    @property
    def continuation_effects(self) -> int:
        return self.effects

    @property
    def manual_user_prompts(self) -> int:
        return 0

    @property
    def channel(self) -> str | None:
        return self.selected_channel

    def __bool__(self) -> bool:
        return self.accepted


@dataclass(frozen=True)
class _ChannelSelection:
    channel: ReentryChannel
    binding: ExactConversationBinding | None = None
    browser: BrowserSendAdapter | None = None
    reason: str = ""


_TERMINAL_STATES = {
    SessionLivenessState.REENTRY_CONFIRMED.value,
    SessionLivenessState.REENTRY_FAILED.value,
    SessionLivenessState.REENTRY_BLOCKED.value,
}
_UNSET = object()
_OFFICIAL_NEW_CHAT_POLICIES = {
    "OFFICIAL_NEW_CONVERSATION_ALLOWED",
    "EXISTING_OR_OFFICIAL_NEW_CHAT",
    "EXISTING_OR_NEW_CONVERSATION",
    "OFFICIAL_NEW_CHAT_ALLOWED",
}


SESSION_REENTRY_DDL = """
CREATE TABLE IF NOT EXISTS session_reentries (
    reentry_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    continuation_id TEXT NOT NULL,
    packet_digest TEXT NOT NULL,
    packet_json TEXT NOT NULL,
    payload TEXT NOT NULL,
    run_id TEXT NOT NULL,
    scope_epoch INTEGER NOT NULL CHECK(scope_epoch >= 1),
    task_id TEXT NOT NULL,
    execution_binding_id TEXT NOT NULL,
    binding_generation INTEGER NOT NULL CHECK(binding_generation >= 1),
    canonical_state_revision INTEGER NOT NULL CHECK(canonical_state_revision >= 1),
    canonical_state_digest TEXT NOT NULL,
    session_liveness_version TEXT NOT NULL,
    liveness_state TEXT NOT NULL CHECK(liveness_state IN (
        'SESSION_ACTIVE', 'TURN_ENDED', 'SESSION_UNAVAILABLE',
        'CONTINUATION_PENDING', 'REENTRY_PREPARED', 'REENTRY_IN_PROGRESS',
        'OPERATOR_CHECKPOINT', 'REENTRY_CONFIRMED', 'REENTRY_FAILED',
        'REENTRY_BLOCKED'
    )),
    selected_channel TEXT CHECK(selected_channel IS NULL OR selected_channel IN (
        'EXISTING_CONVERSATION_VERIFIED',
        'OFFICIAL_NEW_CONVERSATION_CAPABILITY_VERIFIED',
        'OPERATOR_ASSISTED_CHECKPOINT_REQUIRED'
    )),
    conversation_id TEXT,
    conversation_binding_proof TEXT,
    checkpoint_id TEXT,
    trace_json TEXT NOT NULL DEFAULT '[]',
    effect_count INTEGER NOT NULL DEFAULT 0 CHECK(effect_count >= 0),
    operator_prompt_build_required INTEGER NOT NULL DEFAULT 0 CHECK(operator_prompt_build_required IN (0, 1)),
    operator_decision_required INTEGER NOT NULL DEFAULT 0 CHECK(operator_decision_required IN (0, 1)),
    state_revision INTEGER NOT NULL DEFAULT 1 CHECK(state_revision >= 1),
    last_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE RESTRICT,
    UNIQUE (project_id, continuation_id, packet_digest, scope_epoch)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_session_reentry_checkpoint
    ON session_reentries(checkpoint_id) WHERE checkpoint_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_session_reentries_project_state
    ON session_reentries(project_id, liveness_state);
CREATE INDEX IF NOT EXISTS idx_session_reentries_binding
    ON session_reentries(project_id, execution_binding_id, scope_epoch);
"""


def _parse_utc(value: datetime | str | None, *, field: str) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SessionReentryError("INVALID_CLOCK", f"{field} is not an ISO-8601 timestamp") from exc
    else:
        raise SessionReentryError("INVALID_CLOCK", f"{field} must be a datetime or ISO-8601 string")
    if result.tzinfo is None:
        raise SessionReentryError("INVALID_CLOCK", f"{field} must be timezone-aware")
    return result.astimezone(timezone.utc)


def _iso(value: datetime | str | None, *, field: str) -> str:
    return _parse_utc(value, field=field).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _authority_value(authority: Any, name: str, default: Any = None) -> Any:
    if isinstance(authority, Mapping):
        return authority.get(name, default)
    return getattr(authority, name, default)


def _canonical_packet(value: ContinuationPacket | Mapping[str, Any] | bytes) -> ContinuationPacket:
    try:
        if isinstance(value, bytes):
            return deserialize_packet(value)
        if isinstance(value, ContinuationPacket):
            return value
        return ContinuationPacket.from_mapping(value)
    except ContinuationPacketError as exc:
        raise SessionReentryError(exc.code, str(exc)) from exc


def _require_payload(payload: str) -> str:
    if not isinstance(payload, str) or not payload.strip():
        raise SessionReentryError("PAYLOAD_REQUIRED", "canonical re-entry requires a non-empty payload")
    if len(payload.encode("utf-8")) > 64 * 1024:
        raise SessionReentryError("PAYLOAD_TOO_LARGE", "canonical re-entry payload exceeds the bounded size")
    return payload


def _reentry_id(packet: ContinuationPacket) -> str:
    raw = "\x00".join(
        (
            packet["project_id"],
            packet.continuation_id,
            packet["packet_digest"],
            str(packet["scope_epoch"]),
        )
    )
    return "session-reentry-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


class SessionContinuationController:
    """Durably coordinates one natural-session continuation."""

    SESSION_REENTRY_VERSION = SESSION_REENTRY_VERSION
    SESSION_LIVENESS_VERSION = SESSION_LIVENESS_VERSION
    SESSION_REENTRY_VERSION_EXPLICIT = SESSION_REENTRY_VERSION_EXPLICIT
    SESSION_LIVENESS_VERSION_EXPLICIT = SESSION_LIVENESS_VERSION_EXPLICIT
    REENTRY_UNDER_PROJECT_MEMORY_V2_AUTHORITY = REENTRY_UNDER_PROJECT_MEMORY_V2_AUTHORITY
    BROWSER_LOCAL_STATE_IS_REENTRY_AUTHORITY = BROWSER_LOCAL_STATE_IS_REENTRY_AUTHORITY
    SECOND_REENTRY_AUTHORITY_CREATED = SECOND_REENTRY_AUTHORITY_CREATED
    TURN_END_MARKS_TASK_ACCEPTED = TURN_END_MARKS_TASK_ACCEPTED

    def __init__(
        self,
        authority: Any,
        project_id: str | None = None,
        *,
        lease_coordinator: ContinuationLeaseCoordinator | None = None,
        send_coordinator: SendIntentCoordinator | None = None,
        clock: Callable[[], datetime | str] | None = None,
        lease_seconds: int = 30,
    ) -> None:
        self._connection: sqlite3.Connection | None = None
        self._db_path: Path | None = None
        if isinstance(authority, sqlite3.Connection):
            self._connection = authority
            self.project_id = project_id or ""
            self._authority_handle: Any = authority
        elif hasattr(authority, "db_path"):
            self._db_path = Path(authority.db_path)
            self.project_id = project_id or str(getattr(authority, "project_id", ""))
            initializer = getattr(authority, "initialize", None)
            if callable(initializer):
                initializer()
            self._authority_handle = authority
        else:
            self._db_path = Path(authority)
            self.project_id = project_id or ""
            self._authority_handle = authority
        if not self.project_id:
            raise SessionReentryError("PROJECT_ID_REQUIRED", "project_id is required for session re-entry")
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._ensure_schema()
        self.lease = lease_coordinator or ContinuationLeaseCoordinator(
            self._authority_handle,
            self.project_id,
            lease_seconds=lease_seconds,
            clock=self.clock,
        )
        self.send = send_coordinator or SendIntentCoordinator(
            self._authority_handle,
            self.project_id,
            lease_coordinator=self.lease,
            clock=self.clock,
        )

    def _open_connection(self) -> sqlite3.Connection:
        if self._connection is not None:
            self._connection.row_factory = sqlite3.Row
            return self._connection
        if self._db_path is None:
            raise SessionReentryError("PM_V2_DATABASE_MISSING", "no PM v2 database handle was supplied")
        conn = sqlite3.connect(str(self._db_path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def _close_connection(self, conn: sqlite3.Connection) -> None:
        if self._connection is None:
            conn.close()

    def _ensure_schema(self) -> None:
        conn = self._open_connection()
        try:
            conn.executescript(SESSION_REENTRY_DDL)
            conn.commit()
        finally:
            self._close_connection(conn)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._open_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            self._close_connection(conn)

    def _ensure_project(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT project_id FROM projects WHERE project_id = ?", (self.project_id,)
        ).fetchone()
        if row is None:
            raise SessionReentryError("PROJECT_NOT_FOUND", f"canonical PM v2 project '{self.project_id}' does not exist")

    @staticmethod
    def _row_to_record(row: sqlite3.Row | Mapping[str, Any] | None) -> SessionReentryRecord | None:
        if row is None:
            return None
        try:
            trace_value = json.loads(str(row["trace_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SessionReentryError("REENTRY_TRACE_CORRUPT", "canonical re-entry trace is not valid JSON") from exc
        if not isinstance(trace_value, list) or not all(isinstance(item, str) for item in trace_value):
            raise SessionReentryError("REENTRY_TRACE_CORRUPT", "canonical re-entry trace is malformed")
        return SessionReentryRecord(
            reentry_id=str(row["reentry_id"]),
            project_id=str(row["project_id"]),
            continuation_id=str(row["continuation_id"]),
            packet_digest=str(row["packet_digest"]),
            packet_json=str(row["packet_json"]),
            payload=str(row["payload"]),
            run_id=str(row["run_id"]),
            scope_epoch=int(row["scope_epoch"]),
            task_id=str(row["task_id"]),
            execution_binding_id=str(row["execution_binding_id"]),
            binding_generation=int(row["binding_generation"]),
            canonical_state_revision=int(row["canonical_state_revision"]),
            canonical_state_digest=str(row["canonical_state_digest"]),
            session_liveness_version=str(row["session_liveness_version"]),
            liveness_state=str(row["liveness_state"]),
            selected_channel=None if row["selected_channel"] is None else str(row["selected_channel"]),
            conversation_id=None if row["conversation_id"] is None else str(row["conversation_id"]),
            conversation_binding_proof=None
            if row["conversation_binding_proof"] is None
            else str(row["conversation_binding_proof"]),
            checkpoint_id=None if row["checkpoint_id"] is None else str(row["checkpoint_id"]),
            trace=tuple(trace_value),
            effect_count=int(row["effect_count"]),
            operator_prompt_build_required=bool(row["operator_prompt_build_required"]),
            operator_decision_required=bool(row["operator_decision_required"]),
            state_revision=int(row["state_revision"]),
            last_reason=str(row["last_reason"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def _read_in_transaction(self, conn: sqlite3.Connection, reentry_id: str) -> SessionReentryRecord | None:
        return self._row_to_record(
            conn.execute(
                "SELECT * FROM session_reentries WHERE project_id = ? AND reentry_id = ?",
                (self.project_id, reentry_id),
            ).fetchone()
        )

    def read(self, reentry_id: str) -> SessionReentryRecord | None:
        if not isinstance(reentry_id, str) or not reentry_id:
            return None
        conn = self._open_connection()
        try:
            return self._read_in_transaction(conn, reentry_id)
        finally:
            self._close_connection(conn)

    def read_for_packet(self, packet: ContinuationPacket | Mapping[str, Any] | bytes) -> SessionReentryRecord | None:
        typed = _canonical_packet(packet)
        conn = self._open_connection()
        try:
            return self._row_to_record(
                conn.execute(
                    """
                    SELECT * FROM session_reentries
                    WHERE project_id = ? AND continuation_id = ? AND packet_digest = ? AND scope_epoch = ?
                    """,
                    (self.project_id, typed.continuation_id, typed["packet_digest"], typed["scope_epoch"]),
                ).fetchone()
            )
        finally:
            self._close_connection(conn)

    def read_checkpoint(self, checkpoint_id: str) -> SessionReentryRecord | None:
        if not isinstance(checkpoint_id, str) or not checkpoint_id:
            return None
        conn = self._open_connection()
        try:
            return self._row_to_record(
                conn.execute(
                    "SELECT * FROM session_reentries WHERE project_id = ? AND checkpoint_id = ?",
                    (self.project_id, checkpoint_id),
                ).fetchone()
            )
        finally:
            self._close_connection(conn)

    def _read_binding(self, conn: sqlite3.Connection, packet: ContinuationPacket) -> tuple[int, str | None, str] | None:
        row = conn.execute(
            """
            SELECT generation, conversation_id, status, task_id, expected_repo_head_before
            FROM execution_bindings
            WHERE project_id = ? AND execution_binding_id = ?
            """,
            (self.project_id, packet["execution_binding_id"]),
        ).fetchone()
        if row is None:
            return None
        if str(row["task_id"]) != str(packet["current_task_id"]):
            return None
        if str(row["expected_repo_head_before"]) != str(packet["expected_repo_head_before"]):
            return None
        if str(row["status"]).upper() in {"FAILED", "SUPERSEDED", "CANCELLED"}:
            return None
        return int(row["generation"]), None if row["conversation_id"] is None else str(row["conversation_id"]), str(row["status"])

    def _bump_project_revision(self, conn: sqlite3.Connection, now_iso: str) -> int:
        row = conn.execute(
            "SELECT revision FROM projects WHERE project_id = ?", (self.project_id,)
        ).fetchone()
        if row is None:
            raise SessionReentryError("PROJECT_NOT_FOUND", f"canonical PM v2 project '{self.project_id}' does not exist")
        revision = int(row["revision"])
        updated = conn.execute(
            "UPDATE projects SET revision = ?, updated_at = ? WHERE project_id = ? AND revision = ?",
            (revision + 1, now_iso, self.project_id, revision),
        )
        if updated.rowcount != 1:
            raise SessionReentryError("PROJECT_REVISION_CAS_LOST", "canonical project revision CAS was lost")
        return revision + 1

    def _append_event(
        self,
        conn: sqlite3.Connection,
        record: SessionReentryRecord,
        event_type: str,
        reason: str,
        now_iso: str,
        committed_revision: int,
    ) -> None:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='audit_events'"
        ).fetchone()
        if exists is None:
            return
        payload = {
            "schema": SESSION_REENTRY_SCHEMA,
            "session_liveness_version": SESSION_LIVENESS_VERSION,
            "reentry_id": record.reentry_id,
            "continuation_id": record.continuation_id,
            "packet_digest": record.packet_digest,
            "scope_epoch": record.scope_epoch,
            "binding_generation": record.binding_generation,
            "liveness_state": record.liveness_state,
            "selected_channel": record.selected_channel,
            "reason": reason,
            "trace": list(record.trace),
        }
        event_id = f"ev-session-reentry-{record.reentry_id}-s{record.state_revision}"
        conn.execute(
            """
            INSERT OR IGNORE INTO audit_events (
                event_id, project_id, revision, logical_tx_id, event_type,
                human_summary, task_id, milestone_id, plan_version,
                payload_json, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                self.project_id,
                committed_revision,
                f"tx-session-reentry-{record.reentry_id}-s{record.state_revision}",
                event_type,
                f"Session re-entry {record.reentry_id}: {reason}",
                record.task_id,
                None,
                None,
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                now_iso,
            ),
        )

    def _validate_request(
        self,
        packet: ContinuationPacket | Mapping[str, Any] | bytes,
        authority: Any,
        now: datetime | str | None,
    ) -> tuple[ContinuationPacket | None, datetime, str | None, str | None]:
        try:
            typed = _canonical_packet(packet)
            current = _parse_utc(now if now is not None else self.clock(), field="now")
        except SessionReentryError as exc:
            return None, datetime.now(timezone.utc), exc.code, str(exc)
        validation = validate_packet(typed, authority, now=current)
        if not validation.valid:
            return typed, current, validation.code, validation.message
        return typed, current, None, None

    def _create_or_read(
        self,
        packet: ContinuationPacket,
        authority: Any,
        payload: str,
        now: datetime,
        *,
        initial_trace: tuple[str, ...],
    ) -> tuple[SessionReentryRecord | None, str | None, str | None, bool]:
        reentry_id = _reentry_id(packet)
        now_iso = _iso(now, field="now")
        serialized = serialize_packet(packet).decode("utf-8")
        with self._transaction() as conn:
            self._ensure_project(conn)
            existing = self._row_to_record(
                conn.execute(
                    """
                    SELECT * FROM session_reentries
                    WHERE project_id = ? AND continuation_id = ? AND packet_digest = ? AND scope_epoch = ?
                    """,
                    (self.project_id, packet.continuation_id, packet["packet_digest"], packet["scope_epoch"]),
                ).fetchone()
            )
            if existing is not None:
                if existing.payload != payload:
                    return None, "REENTRY_IDENTITY_COLLISION", "the durable re-entry identity already has a different payload", False
                return existing, None, None, False
            binding = self._read_binding(conn, packet)
            if binding is None:
                return None, "BINDING_NOT_FOUND", "canonical execution binding is missing, stale, or terminal", False
            generation, _, _ = binding
            trace_json = json.dumps(list(initial_trace), separators=(",", ":"))
            conn.execute(
                """
                INSERT INTO session_reentries (
                    reentry_id, project_id, continuation_id, packet_digest,
                    packet_json, payload, run_id, scope_epoch, task_id,
                    execution_binding_id, binding_generation,
                    canonical_state_revision, canonical_state_digest,
                    session_liveness_version, liveness_state, trace_json,
                    last_reason, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reentry_id,
                    self.project_id,
                    packet.continuation_id,
                    packet["packet_digest"],
                    serialized,
                    payload,
                    packet["run_id"],
                    packet["scope_epoch"],
                    packet["current_task_id"],
                    packet["execution_binding_id"],
                    generation,
                    packet["state_revision"],
                    packet["state_digest"],
                    SESSION_LIVENESS_VERSION,
                    SessionLivenessState.CONTINUATION_PENDING.value,
                    trace_json,
                    "natural executor turn ended",
                    now_iso,
                    now_iso,
                ),
            )
            committed_revision = self._bump_project_revision(conn, now_iso)
            created = self._read_in_transaction(conn, reentry_id)
            if created is None:
                raise SessionReentryError("REENTRY_READBACK_FAILED", "created canonical re-entry was not readable")
            self._append_event(conn, created, "SESSION_REENTRY_PENDING", "CONTINUATION_PENDING", now_iso, committed_revision)
            return created, None, None, True

    def _persist_conversation_binding(
        self,
        conn: sqlite3.Connection,
        packet: ContinuationPacket,
        binding: ExactConversationBinding,
        *,
        expected_generation: int | None = None,
    ) -> None:
        if not binding.is_exact:
            raise SessionReentryError("GUESSED_CONVERSATION_IDENTITY", "conversation binding is not identity-verifiable")
        row = conn.execute(
            """
            SELECT generation, conversation_id
            FROM execution_bindings
            WHERE project_id = ? AND execution_binding_id = ?
            """,
            (self.project_id, packet["execution_binding_id"]),
        ).fetchone()
        if row is None:
            raise SessionReentryError("BINDING_NOT_FOUND", "canonical execution binding is missing")
        if expected_generation is not None and int(row["generation"]) != expected_generation:
            raise SessionReentryError(
                "BINDING_GENERATION_DIVERGED",
                "canonical execution binding generation changed during re-entry",
            )
        current = None if row["conversation_id"] is None else str(row["conversation_id"])
        if current is not None and current != binding.conversation_id:
            raise SessionReentryError("CONVERSATION_BINDING_CONFLICT", "canonical execution binding has a different conversation identity")
        if current is None:
            conn.execute(
                """
                UPDATE execution_bindings SET conversation_id = ?
                WHERE project_id = ? AND execution_binding_id = ? AND conversation_id IS NULL
                """,
                (binding.conversation_id, self.project_id, packet["execution_binding_id"]),
            )

    def _create_checkpoint(self, conn: sqlite3.Connection, record: SessionReentryRecord, now_iso: str) -> str:
        checkpoint_id = f"checkpoint-{record.reentry_id}"
        conn.execute(
            """
            INSERT OR IGNORE INTO checkpoints (
                checkpoint_id, project_id, label, plan_version, git_head,
                completed_task_ids_json, current_task_id, active_decision_ids_json,
                open_blocker_ids_json, human_summary, created_at
            ) VALUES (?, ?, ?, ?, ?, '[]', ?, ?, ?, ?, ?)
            """,
            (
                checkpoint_id,
                self.project_id,
                "OPERATOR_ASSISTED_REENTRY",
                record.packet["plan_version"],
                record.packet["expected_repo_head_before"],
                record.task_id,
                json.dumps([f"reentry:{record.reentry_id}"]),
                json.dumps(["conversation_identity_unavailable"]),
                "Prepared canonical continuation requires one operator-authorized handoff; no prompt or task reconstruction is required.",
                now_iso,
            ),
        )
        return checkpoint_id

    def _transition(
        self,
        reentry_id: str,
        *,
        state: SessionLivenessState | str,
        reason: str,
        trace: tuple[str, ...] = (),
        expected_states: tuple[str, ...] | None = None,
        channel: ReentryChannel | str | None | object = _UNSET,
        binding: ExactConversationBinding | None | object = _UNSET,
        effect_count: int | object = _UNSET,
        checkpoint: bool = False,
    ) -> SessionReentryRecord | None:
        state_value = state.value if isinstance(state, SessionLivenessState) else str(state)
        now_iso = _iso(self.clock(), field="now")
        with self._transaction() as conn:
            self._ensure_project(conn)
            current = self._read_in_transaction(conn, reentry_id)
            if current is None:
                return None
            if expected_states is not None and current.liveness_state not in expected_states:
                return current
            if current.liveness_state in _TERMINAL_STATES and state_value != current.liveness_state:
                return current
            if binding is not _UNSET and binding is not None:
                self._persist_conversation_binding(
                    conn,
                    current.packet,
                    binding,
                    expected_generation=current.binding_generation,
                )
            checkpoint_id = current.checkpoint_id
            if checkpoint:
                checkpoint_id = self._create_checkpoint(conn, current, now_iso)
            trace_values = list(current.trace)
            for item in trace:
                if not isinstance(item, str) or not item:
                    raise SessionReentryError("INVALID_TRACE_STEP", "re-entry trace steps must be non-empty strings")
                trace_values.append(item)
            next_revision = current.state_revision + 1
            channel_value = current.selected_channel
            if channel is not _UNSET:
                channel_value = None if channel is None else (channel.value if isinstance(channel, ReentryChannel) else str(channel))
            effect_value = current.effect_count if effect_count is _UNSET else int(effect_count)
            if effect_value < 0:
                raise SessionReentryError("INVALID_EFFECT_COUNT", "re-entry effect count cannot be negative")
            updated = conn.execute(
                """
                UPDATE session_reentries
                SET liveness_state = ?, selected_channel = ?,
                    conversation_id = COALESCE(?, conversation_id),
                    conversation_binding_proof = COALESCE(?, conversation_binding_proof),
                    checkpoint_id = ?, trace_json = ?, effect_count = ?,
                    state_revision = ?, last_reason = ?, updated_at = ?
                WHERE project_id = ? AND reentry_id = ? AND state_revision = ?
                """,
                (
                    state_value,
                    channel_value,
                    None if binding is _UNSET or binding is None else binding.conversation_id,
                    None if binding is _UNSET or binding is None else binding.binding_proof,
                    checkpoint_id,
                    json.dumps(trace_values, separators=(",", ":")),
                    effect_value,
                    next_revision,
                    reason[:256],
                    now_iso,
                    self.project_id,
                    reentry_id,
                    current.state_revision,
                ),
            )
            if updated.rowcount != 1:
                return self._read_in_transaction(conn, reentry_id)
            committed_revision = self._bump_project_revision(conn, now_iso)
            result = self._read_in_transaction(conn, reentry_id)
            if result is None:
                raise SessionReentryError("REENTRY_READBACK_FAILED", "transitioned canonical re-entry was not readable")
            self._append_event(conn, result, "SESSION_REENTRY_STATE", reason, now_iso, committed_revision)
            return result

    @staticmethod
    def _binding_from_capability_result(value: Any) -> tuple[ExactConversationBinding | None, BrowserSendAdapter | None]:
        binding: Any = value
        browser: Any = None
        if isinstance(value, tuple) and len(value) == 2:
            binding, browser = value
        elif isinstance(value, Mapping):
            binding = value.get("binding")
            browser = value.get("browser")
        if not isinstance(binding, ExactConversationBinding) or not binding.is_exact:
            return None, None
        return binding, browser

    def _resolve_channel(
        self,
        packet: ContinuationPacket,
        authority: Any,
        *,
        existing_binding: ExactConversationBinding | None,
        browser: BrowserSendAdapter | None,
        official_capability: OfficialNewConversationCapability | None,
        policy_allowed: bool,
    ) -> _ChannelSelection:
        if policy_allowed and existing_binding is not None and existing_binding.is_exact and browser is not None:
            try:
                dom = browser.snapshot()
            except Exception:
                dom = None
            if dom is not None and dom.conversation_id == existing_binding.conversation_id:
                return _ChannelSelection(
                    ReentryChannel.EXISTING_CONVERSATION_VERIFIED,
                    existing_binding,
                    browser,
                    "existing conversation identity and DOM identity match",
                )

        policy = str(_authority_value(authority, "conversation_binding_policy", "EXISTING_CHAT_ONLY")).upper()
        if policy_allowed and policy in _OFFICIAL_NEW_CHAT_POLICIES and official_capability is not None:
            official = bool(
                getattr(official_capability, "official_capability", getattr(official_capability, "official", False))
            )
            identity_verifiable = bool(getattr(official_capability, "identity_verifiable", False))
            creator = getattr(official_capability, "create_and_verify", None)
            if official and identity_verifiable and callable(creator):
                try:
                    new_binding, new_browser = self._binding_from_capability_result(creator(packet, authority))
                except Exception:
                    new_binding, new_browser = None, None
                if new_binding is not None and new_browser is not None:
                    try:
                        dom = new_browser.snapshot()
                    except Exception:
                        dom = None
                    if dom is not None and dom.conversation_id == new_binding.conversation_id:
                        return _ChannelSelection(
                            ReentryChannel.OFFICIAL_NEW_CONVERSATION_CAPABILITY_VERIFIED,
                            new_binding,
                            new_browser,
                            "official capability returned an exact verifiable conversation identity",
                        )

        reason = "policy denied or no safe identity-verifiable automated conversation channel is available"
        return _ChannelSelection(ReentryChannel.OPERATOR_ASSISTED_CHECKPOINT_REQUIRED, reason=reason)

    def _checkpoint_result(self, record: SessionReentryRecord, *, idempotent: bool = False) -> SessionReentryResult:
        return SessionReentryResult(
            True,
            "ALREADY_OPERATOR_CHECKPOINT" if idempotent else "OPERATOR_CHECKPOINT_CREATED",
            "one-click operator-assisted re-entry checkpoint is durable and contains the exact continuation intent",
            record,
            record.trace,
            0,
            idempotent,
            record.selected_channel,
            record.checkpoint_id,
        )

    def mark_turn_ended(
        self,
        packet: ContinuationPacket | Mapping[str, Any] | bytes,
        authority: Any,
        *,
        payload: str = "Continue the next approved task in this existing chat.",
        now: datetime | str | None = None,
    ) -> SessionReentryResult:
        """Record a natural turn end without accepting or executing a task."""

        try:
            safe_payload = _require_payload(payload)
        except SessionReentryError as exc:
            return SessionReentryResult(False, exc.code, str(exc))
        typed, current, code, message = self._validate_request(packet, authority, now)
        if typed is None or code is not None:
            return SessionReentryResult(False, code or "INVALID_PACKET", message or "packet is invalid")
        record, create_code, create_message, created = self._create_or_read(
            typed,
            authority,
            safe_payload,
            current,
            initial_trace=("SESSION_END", "TURN_ENDED", "CONTINUATION_PENDING"),
        )
        if record is None:
            return SessionReentryResult(False, create_code or "REENTRY_CREATE_FAILED", create_message or "re-entry was not persisted")
        return SessionReentryResult(
            True,
            "ALREADY_PENDING" if record.trace.count("TURN_ENDED") else "CONTINUATION_PENDING",
            "natural turn end is durably pending; task acceptance and binding generation were not changed",
            record,
            record.trace,
            0,
            not created,
            record.selected_channel,
            record.checkpoint_id,
        )

    signal_turn_end = mark_turn_ended
    record_session_end = mark_turn_ended

    def _existing_intent_for_record(self, record: SessionReentryRecord) -> Any:
        conn = self._open_connection()
        try:
            row = conn.execute(
                """
                SELECT * FROM send_intents
                WHERE project_id = ? AND continuation_id = ? AND scope_epoch = ?
                ORDER BY state_revision DESC LIMIT 1
                """,
                (self.project_id, record.continuation_id, record.scope_epoch),
            ).fetchone()
            return None if row is None else str(row["status"])
        finally:
            self._close_connection(conn)

    def _idempotent_or_in_progress(self, record: SessionReentryRecord) -> SessionReentryResult | None:
        if record.liveness_state == SessionLivenessState.REENTRY_CONFIRMED.value:
            return SessionReentryResult(
                True,
                "ALREADY_REENTERED",
                "the canonical continuation effect is already confirmed",
                record,
                record.trace,
                0,
                True,
                record.selected_channel,
                record.checkpoint_id,
            )
        if record.liveness_state == SessionLivenessState.OPERATOR_CHECKPOINT.value:
            return self._checkpoint_result(record, idempotent=True)
        if record.liveness_state in {SessionLivenessState.REENTRY_PREPARED.value, SessionLivenessState.REENTRY_IN_PROGRESS.value}:
            intent_status = self._existing_intent_for_record(record)
            if intent_status == "ACKNOWLEDGED":
                confirmed = self._transition(
                    record.reentry_id,
                    state=SessionLivenessState.REENTRY_CONFIRMED,
                    reason="durable send intent was already acknowledged",
                    trace=("REENTRY_CONFIRMED",),
                    expected_states=(record.liveness_state,),
                    effect_count=1,
                )
                return SessionReentryResult(True, "ALREADY_REENTERED", "durable acknowledgement recovered without a physical resend", confirmed, confirmed.trace if confirmed else record.trace, 0, True, record.selected_channel, record.checkpoint_id)
            if intent_status in {"UNCERTAIN", "RECONCILIATION_REQUIRED", "PHYSICAL_SEND_ATTEMPTED"}:
                return SessionReentryResult(False, "DELIVERY_UNCERTAIN", "durable send intent is unresolved; blind re-entry send is forbidden", record, record.trace, 0, True, record.selected_channel, record.checkpoint_id)
            return SessionReentryResult(False, "REENTRY_IN_PROGRESS", "another canonical re-entry attempt owns this continuation", record, record.trace, 0, True, record.selected_channel, record.checkpoint_id)
        if record.liveness_state in _TERMINAL_STATES:
            return SessionReentryResult(False, "REENTRY_TERMINAL", record.last_reason, record, record.trace, 0, True, record.selected_channel, record.checkpoint_id)
        return None

    def _execute_automated(
        self,
        record: SessionReentryRecord,
        packet: ContinuationPacket,
        authority: Any,
        *,
        owner_id: str,
        payload: str,
        selection: _ChannelSelection,
        now: datetime,
    ) -> SessionReentryResult:
        if selection.binding is None or selection.browser is None:
            return SessionReentryResult(False, "NO_SAFE_AUTOMATED_CHANNEL", "no exact conversation binding and browser effect adapter are available", record, record.trace, 0, False, selection.channel.value, record.checkpoint_id)
        claim = self.lease.claim(packet, authority, owner_id=owner_id, now=now)
        if not claim.claimed or claim.owner_token is None:
            blocked = self._transition(
                record.reentry_id,
                state=SessionLivenessState.REENTRY_BLOCKED,
                reason=claim.message,
                trace=("LEASE_CLAIM_REJECTED",),
                expected_states=(record.liveness_state,),
                channel=selection.channel,
            )
            return SessionReentryResult(False, claim.reason_code, claim.message, blocked, blocked.trace if blocked else record.trace, 0, False, selection.channel.value, record.checkpoint_id)

        try:
            prepared = self._transition(
                record.reentry_id,
                state=SessionLivenessState.REENTRY_PREPARED,
                reason=selection.reason,
                trace=("LIVE_AUTHORITY_VALIDATED", "LEASE_CLAIMED", selection.channel.value),
                expected_states=(record.liveness_state,),
                channel=selection.channel,
                binding=selection.binding,
            )
        except SessionReentryError as exc:
            try:
                self.lease.release(packet, claim.owner_token, now=now)
            except Exception:
                pass
            blocked = self._transition(
                record.reentry_id,
                state=SessionLivenessState.REENTRY_BLOCKED,
                reason=str(exc),
                trace=("REENTRY_BLOCKED",),
                expected_states=(record.liveness_state,),
            )
            return SessionReentryResult(
                False,
                exc.code,
                str(exc),
                blocked,
                blocked.trace if blocked else record.trace,
                0,
                False,
                selection.channel.value,
                record.checkpoint_id,
            )
        if prepared is None:
            return SessionReentryResult(False, "REENTRY_READBACK_FAILED", "prepared re-entry was not readable")
        if prepared.liveness_state != SessionLivenessState.REENTRY_PREPARED.value:
            try:
                self.lease.release(packet, claim.owner_token, now=now)
            except Exception:
                pass
            return SessionReentryResult(
                False,
                "REENTRY_STATE_CAS_LOST",
                "canonical re-entry state changed before preparation completed",
                prepared,
                prepared.trace,
                0,
                True,
                prepared.selected_channel,
                prepared.checkpoint_id,
            )
        in_progress = self._transition(
            prepared.reentry_id,
            state=SessionLivenessState.REENTRY_IN_PROGRESS,
            reason="re-entry effect is beginning after canonical preparation",
            expected_states=(SessionLivenessState.REENTRY_PREPARED.value,),
        )
        if in_progress is None:
            return SessionReentryResult(False, "REENTRY_READBACK_FAILED", "in-progress re-entry was not readable")
        if in_progress.liveness_state != SessionLivenessState.REENTRY_IN_PROGRESS.value:
            try:
                self.lease.release(packet, claim.owner_token, now=now)
            except Exception:
                pass
            return SessionReentryResult(
                False,
                "REENTRY_STATE_CAS_LOST",
                "canonical re-entry state changed before the effect boundary",
                in_progress,
                in_progress.trace,
                0,
                True,
                in_progress.selected_channel,
                in_progress.checkpoint_id,
            )

        try:
            flow = self.send.continue_same_chat(
                packet,
                authority,
                claim,
                selection.binding,
                selection.browser,
                payload,
                now=now,
            )
        except Exception as exc:
            effects = int(getattr(selection.browser, "visible_send_count", 0))
            failed = self._transition(
                in_progress.reentry_id,
                state=SessionLivenessState.REENTRY_FAILED,
                reason=f"adapter failure: {exc}",
                trace=("REENTRY_FAILED",),
                expected_states=(SessionLivenessState.REENTRY_IN_PROGRESS.value,),
                effect_count=effects,
            )
            return SessionReentryResult(False, "REENTRY_EFFECT_FAILED", "re-entry effect failed closed", failed, failed.trace if failed else in_progress.trace, effects, False, selection.channel.value, in_progress.checkpoint_id)

        effects = int(getattr(selection.browser, "visible_send_count", flow.visible_sends))
        flow_trace = tuple(flow.trace)
        if flow.accepted and flow.intent is not None and effects == 1:
            confirmed = self._transition(
                in_progress.reentry_id,
                state=SessionLivenessState.REENTRY_CONFIRMED,
                reason="same-chat continuation send and acknowledgement completed",
                trace=flow_trace + ("REENTRY_CONFIRMED",),
                expected_states=(SessionLivenessState.REENTRY_IN_PROGRESS.value,),
                effect_count=effects,
            )
            return SessionReentryResult(True, "REENTRY_CONFIRMED", "canonical same-chat continuation completed", confirmed, confirmed.trace if confirmed else in_progress.trace, effects, False, selection.channel.value, in_progress.checkpoint_id)

        reason_code = flow.send_result.reason_code if flow.send_result is not None else "REENTRY_EFFECT_FAILED"
        reason = flow.send_result.message if flow.send_result is not None else "canonical re-entry effect failed closed"
        if effects == 0:
            try:
                self.lease.release(packet, claim.owner_token, now=now)
            except Exception:
                pass
        failed = self._transition(
            in_progress.reentry_id,
            state=SessionLivenessState.REENTRY_FAILED,
            reason=reason,
            trace=flow_trace + ("REENTRY_FAILED",),
            expected_states=(SessionLivenessState.REENTRY_IN_PROGRESS.value,),
            effect_count=effects,
        )
        return SessionReentryResult(False, reason_code, reason, failed, failed.trace if failed else in_progress.trace, effects, False, selection.channel.value, in_progress.checkpoint_id)

    def reenter(
        self,
        packet: ContinuationPacket | Mapping[str, Any] | bytes,
        authority: Any,
        *,
        owner_id: str,
        payload: str,
        existing_binding: ExactConversationBinding | None = None,
        browser: BrowserSendAdapter | None = None,
        official_new_conversation_capability: OfficialNewConversationCapability | None = None,
        policy_allowed: bool = True,
        now: datetime | str | None = None,
    ) -> SessionReentryResult:
        """Record session end, choose one policy channel, and continue safely."""

        try:
            safe_payload = _require_payload(payload)
        except SessionReentryError as exc:
            return SessionReentryResult(False, exc.code, str(exc))
        typed, current, code, message = self._validate_request(packet, authority, now)
        if typed is None or code is not None:
            return SessionReentryResult(False, code or "INVALID_PACKET", message or "packet is invalid")

        record, create_code, create_message, _ = self._create_or_read(
            typed,
            authority,
            safe_payload,
            current,
            initial_trace=("SESSION_END", "TURN_ENDED", "CONTINUATION_PENDING"),
        )
        if record is None:
            return SessionReentryResult(False, create_code or "REENTRY_CREATE_FAILED", create_message or "re-entry was not persisted")
        existing = self._idempotent_or_in_progress(record)
        if existing is not None:
            return existing

        live_again = validate_packet(typed, authority, now=current)
        if not live_again.valid:
            blocked = self._transition(
                record.reentry_id,
                state=SessionLivenessState.REENTRY_BLOCKED,
                reason=live_again.message,
                trace=("LIVE_AUTHORITY_REVALIDATION_FAILED",),
                expected_states=(record.liveness_state,),
            )
            return SessionReentryResult(False, live_again.code, live_again.message, blocked, blocked.trace if blocked else record.trace, 0, False, record.selected_channel, record.checkpoint_id)

        selection = self._resolve_channel(
            typed,
            authority,
            existing_binding=existing_binding,
            browser=browser,
            official_capability=official_new_conversation_capability,
            policy_allowed=policy_allowed,
        )
        if selection.channel == ReentryChannel.OPERATOR_ASSISTED_CHECKPOINT_REQUIRED:
            unavailable = self._transition(
                record.reentry_id,
                state=SessionLivenessState.SESSION_UNAVAILABLE,
                reason=selection.reason,
                trace=("LIVE_AUTHORITY_VALIDATED", "SESSION_UNAVAILABLE", "NO_SAFE_AUTOMATED_CHANNEL"),
                expected_states=(record.liveness_state,),
                channel=selection.channel,
            )
            if unavailable is None:
                return SessionReentryResult(False, "REENTRY_READBACK_FAILED", "session-unavailable re-entry was not readable")
            checkpoint = self._transition(
                unavailable.reentry_id,
                state=SessionLivenessState.OPERATOR_CHECKPOINT,
                reason="D-017 requires deterministic operator-assisted fallback",
                trace=("OPERATOR_CHECKPOINT_CREATED",),
                expected_states=(SessionLivenessState.SESSION_UNAVAILABLE.value,),
                channel=selection.channel,
                checkpoint=True,
            )
            if checkpoint is None:
                return SessionReentryResult(False, "REENTRY_READBACK_FAILED", "operator checkpoint was not readable")
            return self._checkpoint_result(checkpoint)

        return self._execute_automated(
            record,
            typed,
            authority,
            owner_id=owner_id,
            payload=safe_payload,
            selection=selection,
            now=current,
        )

    handle_session_end = reenter
    process_session_end = reenter

    def continue_from_operator_checkpoint(
        self,
        checkpoint_id: str,
        authority: Any,
        *,
        owner_id: str,
        binding: ExactConversationBinding,
        browser: BrowserSendAdapter,
        payload: str | None = None,
        now: datetime | str | None = None,
    ) -> SessionReentryResult:
        """Execute a checkpoint using an externally verified exact chat identity."""

        record = self.read_checkpoint(checkpoint_id)
        if record is None:
            return SessionReentryResult(False, "CHECKPOINT_NOT_FOUND", "operator re-entry checkpoint does not exist")
        if record.liveness_state == SessionLivenessState.REENTRY_CONFIRMED.value:
            return self._idempotent_or_in_progress(record) or SessionReentryResult(True, "ALREADY_REENTERED", "re-entry already confirmed", record, record.trace, 0, True, record.selected_channel, record.checkpoint_id)
        if record.liveness_state != SessionLivenessState.OPERATOR_CHECKPOINT.value:
            existing = self._idempotent_or_in_progress(record)
            return existing or SessionReentryResult(False, "CHECKPOINT_NOT_OPEN", "operator checkpoint is no longer open", record, record.trace, 0, True, record.selected_channel, record.checkpoint_id)
        safe_payload = record.payload if payload is None else _require_payload(payload)
        if safe_payload != record.payload:
            return SessionReentryResult(False, "CHECKPOINT_PAYLOAD_MISMATCH", "operator checkpoint payload is canonical and cannot be replaced", record, record.trace, 0, False, record.selected_channel, record.checkpoint_id)
        typed, current, code, message = self._validate_request(record.packet, authority, now)
        if typed is None or code is not None:
            return SessionReentryResult(False, code or "INVALID_PACKET", message or "checkpoint packet is stale", record, record.trace, 0, False, record.selected_channel, record.checkpoint_id)
        selection = self._resolve_channel(
            typed,
            authority,
            existing_binding=binding,
            browser=browser,
            official_capability=None,
            policy_allowed=True,
        )
        if selection.channel != ReentryChannel.EXISTING_CONVERSATION_VERIFIED:
            return SessionReentryResult(False, "CONVERSATION_IDENTITY_UNVERIFIED", "operator action did not provide a verified exact existing conversation", record, record.trace, 0, False, record.selected_channel, record.checkpoint_id)
        try:
            reopened = self._transition(
                record.reentry_id,
                state=SessionLivenessState.CONTINUATION_PENDING,
                reason="operator authorized the bounded handoff with an exact existing conversation",
                trace=("OPERATOR_AUTHORIZED_HANDOFF",),
                expected_states=(SessionLivenessState.OPERATOR_CHECKPOINT.value,),
                channel=selection.channel,
                binding=selection.binding,
            )
        except SessionReentryError as exc:
            blocked = self._transition(
                record.reentry_id,
                state=SessionLivenessState.REENTRY_BLOCKED,
                reason=str(exc),
                trace=("REENTRY_BLOCKED",),
                expected_states=(SessionLivenessState.OPERATOR_CHECKPOINT.value,),
            )
            return SessionReentryResult(
                False,
                exc.code,
                str(exc),
                blocked,
                blocked.trace if blocked else record.trace,
                0,
                False,
                record.selected_channel,
                record.checkpoint_id,
            )
        if reopened is None:
            return SessionReentryResult(False, "REENTRY_READBACK_FAILED", "checkpoint handoff was not readable")
        if reopened.liveness_state != SessionLivenessState.CONTINUATION_PENDING.value:
            return SessionReentryResult(
                False,
                "REENTRY_STATE_CAS_LOST",
                "operator checkpoint state changed before the handoff completed",
                reopened,
                reopened.trace,
                0,
                True,
                reopened.selected_channel,
                reopened.checkpoint_id,
            )
        return self._execute_automated(
            reopened,
            typed,
            authority,
            owner_id=owner_id,
            payload=safe_payload,
            selection=selection,
            now=current,
        )

    approve_operator_checkpoint = continue_from_operator_checkpoint
    on_session_end = reenter
    continue_reentry = reenter


__all__ = [
    "BROWSER_LOCAL_STATE_IS_REENTRY_AUTHORITY",
    "OfficialNewConversationCapability",
    "REENTRY_UNDER_PROJECT_MEMORY_V2_AUTHORITY",
    "ReentryChannel",
    "SECOND_REENTRY_AUTHORITY_CREATED",
    "SESSION_LIVENESS_VERSION",
    "SESSION_LIVENESS_VERSION_EXPLICIT",
    "SESSION_REENTRY_DDL",
    "SESSION_REENTRY_SCHEMA",
    "SESSION_REENTRY_SCHEMA_VERSION",
    "SESSION_REENTRY_TABLE",
    "SESSION_REENTRY_VERSION",
    "SESSION_REENTRY_VERSION_EXPLICIT",
    "SessionContinuationController",
    "SessionLivenessState",
    "SessionReentryError",
    "SessionReentryRecord",
    "SessionReentryResult",
    "TURN_END_MARKS_TASK_ACCEPTED",
]

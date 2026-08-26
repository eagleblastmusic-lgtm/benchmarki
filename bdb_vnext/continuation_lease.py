"""NX-026 durable continuation lease/claim/idempotency protocol.

Continuation leases deliberately reuse the canonical Project Memory v2
``leases`` table.  Browser and Native adapters receive an opaque owner token,
but the token is only persisted as a digest.  All mutations are short
``BEGIN IMMEDIATE`` transactions so the AVAILABLE -> CLAIMED compare-and-set
is performed by the same authority that stores the packet's identity.

This module is a coordination primitive, not a scheduler.  It never selects a
task and it never performs a Browser/Native effect.  Callers must provide the
current NX-025 authority snapshot on every claim/reclaim/effect check.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .continuation_packet import (
    ContinuationAuthoritySnapshot,
    ContinuationPacket,
    ContinuationPacketError,
    ContinuationValidationResult,
    deserialize_packet,
    validate_packet,
)
from .stop_fence import EffectBoundary, EffectBoundaryGuard


CONTINUATION_LEASE_VERSION = "v1"
CONTINUATION_LEASE_VERSION_EXPLICIT = True
CONTINUATION_LEASE_UNDER_PROJECT_MEMORY_V2 = True
SECOND_LEASE_AUTHORITY_CREATED = False
CONTINUATION_LEASE_RESOURCE_TYPE = "CONTINUATION"
DEFAULT_CONTINUATION_LEASE_SECONDS = 30
_BUSY_TIMEOUT_SECONDS = 5.0

_CONTINUATION_COLUMNS = frozenset(
    {
        "lease_kind",
        "continuation_id",
        "packet_digest",
        "run_id",
        "scope_epoch",
        "task_id",
        "execution_binding_id",
        "owner_id",
        "owner_token_hash",
        "generation",
        "state_revision",
        "last_transition_reason",
        "completed_at",
        "abandoned_at",
    }
)


class ContinuationLeaseError(RuntimeError):
    """Raised when the supplied PM v2 authority cannot support this protocol."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _parse_utc(value: datetime | str | None, *, field: str) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ContinuationLeaseError("INVALID_CLOCK", f"{field} is not an ISO-8601 timestamp") from exc
    else:
        raise ContinuationLeaseError("INVALID_CLOCK", f"{field} must be a datetime or ISO-8601 string")
    if parsed.tzinfo is None:
        raise ContinuationLeaseError("INVALID_CLOCK", f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | str | None, *, field: str) -> str:
    return _parse_utc(value, field=field).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _token_digest(token: str) -> str:
    if not isinstance(token, str) or len(token) < 20:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _canonical_packet(packet: ContinuationPacket | Mapping[str, Any] | bytes) -> ContinuationPacket:
    try:
        if isinstance(packet, ContinuationPacket):
            return packet
        if isinstance(packet, bytes):
            return deserialize_packet(packet)
        return ContinuationPacket.from_mapping(packet)
    except ContinuationPacketError as exc:
        raise ContinuationLeaseError(exc.code, str(exc)) from exc


def _authority_value(authority: ContinuationAuthoritySnapshot | Mapping[str, Any], name: str, default: Any = None) -> Any:
    if isinstance(authority, Mapping):
        return authority.get(name, default)
    return getattr(authority, name, default)


@dataclass(frozen=True)
class ContinuationLeaseRecord:
    """Durable continuation lease state read from PM v2."""

    lease_id: str
    project_id: str
    continuation_id: str
    packet_digest: str
    run_id: str
    scope_epoch: int
    task_id: str
    execution_binding_id: str
    owner_id: str | None
    owner_token_hash: str | None
    generation: int
    acquired_at: str
    expires_at: str
    state_revision: int
    status: str
    last_transition_reason: str | None
    completed_at: str | None
    abandoned_at: str | None
    lease_kind: str = CONTINUATION_LEASE_RESOURCE_TYPE

    @property
    def packet_id(self) -> str:
        return self.continuation_id

    @property
    def token_hash(self) -> str | None:
        return self.owner_token_hash

    @property
    def is_claimed(self) -> bool:
        return self.status == "CLAIMED"

    @property
    def is_terminal(self) -> bool:
        return self.status in {"COMPLETED", "ABANDONED"}


@dataclass(frozen=True)
class ContinuationLeaseResult:
    """Result of a lease mutation or effect authorization check."""

    accepted: bool
    reason_code: str
    message: str
    lease: ContinuationLeaseRecord | None = None
    owner_token: str | None = None
    mutated: bool = False
    idempotent: bool = False

    @property
    def claimed(self) -> bool:
        return self.accepted and self.lease is not None and self.lease.status == "CLAIMED"

    @property
    def won(self) -> bool:
        return self.claimed

    @property
    def allowed(self) -> bool:
        return self.accepted

    @property
    def effects_allowed(self) -> int:
        return int(self.accepted)

    @property
    def duplicate_effects(self) -> int:
        return 0

    def __bool__(self) -> bool:
        return self.accepted


@dataclass(frozen=True)
class ContinuationLeaseSnapshot:
    """Stable, serializable diagnostic projection of a lease record."""

    lease_id: str
    project_id: str
    continuation_id: str
    packet_digest: str
    run_id: str
    scope_epoch: int
    task_id: str
    execution_binding_id: str
    owner_id: str | None
    generation: int
    acquired_at: str
    expires_at: str
    state_revision: int
    status: str
    last_transition_reason: str | None
    completed_at: str | None
    abandoned_at: str | None

    @classmethod
    def from_record(cls, record: ContinuationLeaseRecord) -> "ContinuationLeaseSnapshot":
        return cls(
            lease_id=record.lease_id,
            project_id=record.project_id,
            continuation_id=record.continuation_id,
            packet_digest=record.packet_digest,
            run_id=record.run_id,
            scope_epoch=record.scope_epoch,
            task_id=record.task_id,
            execution_binding_id=record.execution_binding_id,
            owner_id=record.owner_id,
            generation=record.generation,
            acquired_at=record.acquired_at,
            expires_at=record.expires_at,
            state_revision=record.state_revision,
            status=record.status,
            last_transition_reason=record.last_transition_reason,
            completed_at=record.completed_at,
            abandoned_at=record.abandoned_at,
        )


class ContinuationLeaseCoordinator:
    """Coordinates continuation claims in the canonical PM v2 database.

    ``authority`` may be a :class:`ProjectMemoryStoreV2`, a PM-v2 SQLite
    connection, or the path of an already-created PM-v2 database.  A path is
    only a transport handle to the canonical database; this class never
    creates a second database or a second authority.
    """

    def __init__(
        self,
        authority: Any,
        project_id: str | None = None,
        *,
        lease_seconds: int = DEFAULT_CONTINUATION_LEASE_SECONDS,
        clock: Callable[[], datetime | str] | None = None,
        enforce_stop_fence: bool = True,
    ) -> None:
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or lease_seconds <= 0:
            raise ContinuationLeaseError("INVALID_LEASE_DURATION", "lease_seconds must be a positive integer")

        self._connection: sqlite3.Connection | None = None
        self._db_path: Path | None = None
        self._owns_connection = False
        if isinstance(authority, sqlite3.Connection):
            self._connection = authority
            self.project_id = project_id or ""
        elif hasattr(authority, "db_path"):
            self._db_path = Path(authority.db_path)
            self.project_id = project_id or str(getattr(authority, "project_id", ""))
            initializer = getattr(authority, "initialize", None)
            if callable(initializer):
                initializer()
        else:
            self._db_path = Path(authority)
            self.project_id = project_id or ""

        if not self.project_id:
            raise ContinuationLeaseError("PROJECT_ID_REQUIRED", "project_id is required for a continuation lease")

        self.lease_seconds = lease_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.enforce_stop_fence = enforce_stop_fence
        self._validate_schema()

    @classmethod
    def from_store(cls, store: Any, **kwargs: Any) -> "ContinuationLeaseCoordinator":
        """Create a coordinator without changing the PM v2 authority choice."""

        return cls(store, **kwargs)

    @staticmethod
    def resource_id_for(packet: ContinuationPacket | Mapping[str, Any] | bytes) -> str:
        typed = _canonical_packet(packet)
        return f"{typed.continuation_id}|{typed['packet_digest']}|epoch:{typed['scope_epoch']}"

    @staticmethod
    def lease_id_for(project_id: str, resource_id: str) -> str:
        identity = f"{project_id}\x00{resource_id}".encode("utf-8")
        return f"continuation-lease-{hashlib.sha256(identity).hexdigest()[:40]}"

    def _validate_schema(self) -> None:
        conn = self._open_connection()
        try:
            table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='leases'"
            ).fetchone()
            if table is None:
                raise ContinuationLeaseError("PM_V2_LEASE_TABLE_MISSING", "canonical PM v2 leases table is missing")
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(leases)").fetchall()}
            missing = sorted(_CONTINUATION_COLUMNS - columns)
            if missing:
                raise ContinuationLeaseError(
                    "PM_V2_LEASE_SCHEMA_MISMATCH",
                    f"canonical PM v2 leases table lacks NX-026 columns: {', '.join(missing)}",
                )
        finally:
            self._close_connection(conn)

    def _open_connection(self) -> sqlite3.Connection:
        if self._connection is not None:
            conn = self._connection
            conn.row_factory = sqlite3.Row
            return conn
        if self._db_path is None:
            raise ContinuationLeaseError("PM_V2_DATABASE_MISSING", "no PM v2 database handle was supplied")
        conn = sqlite3.connect(str(self._db_path), timeout=_BUSY_TIMEOUT_SECONDS, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def _close_connection(self, conn: sqlite3.Connection) -> None:
        if self._connection is None:
            conn.close()

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

    def _now(self, value: datetime | str | None) -> datetime:
        return _parse_utc(value if value is not None else self.clock(), field="now")

    def _ensure_project(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT revision FROM projects WHERE project_id = ?",
            (self.project_id,),
        ).fetchone()
        if row is None:
            raise ContinuationLeaseError(
                "PROJECT_NOT_FOUND",
                f"canonical PM v2 project '{self.project_id}' does not exist",
            )

    def _bump_project_revision(self, conn: sqlite3.Connection, now_iso: str) -> int:
        row = conn.execute(
            "SELECT revision FROM projects WHERE project_id = ?",
            (self.project_id,),
        ).fetchone()
        if row is None:
            raise ContinuationLeaseError("PROJECT_NOT_FOUND", f"project '{self.project_id}' does not exist")
        next_revision = int(row["revision"]) + 1
        updated = conn.execute(
            "UPDATE projects SET revision = ?, updated_at = ? WHERE project_id = ? AND revision = ?",
            (next_revision, now_iso, self.project_id, int(row["revision"])),
        )
        if updated.rowcount != 1:
            raise ContinuationLeaseError("PROJECT_REVISION_CAS_LOST", "project revision CAS did not update")
        return next_revision

    def _append_event(
        self,
        conn: sqlite3.Connection,
        record: ContinuationLeaseRecord,
        event_type: str,
        reason: str,
        now_iso: str,
    ) -> None:
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='audit_events'"
        ).fetchone()
        if table is None:
            return
        next_revision = int(
            conn.execute(
                "SELECT COALESCE(MAX(revision), 0) + 1 FROM audit_events WHERE project_id = ?",
                (self.project_id,),
            ).fetchone()[0]
        )
        event_id = f"{self.project_id}:continuation-lease:{record.lease_id}:s{record.state_revision}"
        payload = {
            "lease_version": CONTINUATION_LEASE_VERSION,
            "lease_id": record.lease_id,
            "continuation_id": record.continuation_id,
            "packet_digest": record.packet_digest,
            "scope_epoch": record.scope_epoch,
            "generation": record.generation,
            "status": record.status,
            "reason": reason,
        }
        try:
            conn.execute(
                """
                INSERT INTO audit_events (
                    event_id, project_id, revision, logical_tx_id, event_type,
                    human_summary, task_id, milestone_id, plan_version,
                    payload_json, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    self.project_id,
                    next_revision,
                    f"tx-continuation-lease-{record.lease_id}-s{record.state_revision}",
                    event_type,
                    f"Continuation lease {record.lease_id}: {reason}",
                    record.task_id,
                    None,
                    None,
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                    now_iso,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ContinuationLeaseError("LEASE_AUDIT_CONFLICT", "continuation lease audit event identity conflicted") from exc

    @staticmethod
    def _record(row: sqlite3.Row | Mapping[str, Any] | None) -> ContinuationLeaseRecord | None:
        if row is None:
            return None
        return ContinuationLeaseRecord(
            lease_id=str(row["lease_id"]),
            project_id=str(row["project_id"]),
            continuation_id=str(row["continuation_id"]),
            packet_digest=str(row["packet_digest"]),
            run_id=str(row["run_id"]),
            scope_epoch=int(row["scope_epoch"]),
            task_id=str(row["task_id"]),
            execution_binding_id=str(row["execution_binding_id"]),
            owner_id=None if row["owner_id"] is None else str(row["owner_id"]),
            owner_token_hash=None if row["owner_token_hash"] is None else str(row["owner_token_hash"]),
            generation=int(row["generation"]),
            acquired_at=str(row["acquired_at"]),
            expires_at=str(row["expires_at"]),
            state_revision=int(row["state_revision"]),
            status=str(row["status"]),
            last_transition_reason=None
            if row["last_transition_reason"] is None
            else str(row["last_transition_reason"]),
            completed_at=None if row["completed_at"] is None else str(row["completed_at"]),
            abandoned_at=None if row["abandoned_at"] is None else str(row["abandoned_at"]),
            lease_kind=str(row["lease_kind"]),
        )

    def _select_for_packet(self, conn: sqlite3.Connection, packet: ContinuationPacket) -> ContinuationLeaseRecord | None:
        row = conn.execute(
            """
            SELECT * FROM leases
            WHERE project_id = ? AND resource_type = ? AND resource_id = ?
            """,
            (self.project_id, CONTINUATION_LEASE_RESOURCE_TYPE, self.resource_id_for(packet)),
        ).fetchone()
        return self._record(row)

    def _packet_matches_record(self, packet: ContinuationPacket, record: ContinuationLeaseRecord) -> bool:
        return (
            record.project_id == self.project_id
            and record.lease_kind == CONTINUATION_LEASE_RESOURCE_TYPE
            and record.continuation_id == packet.continuation_id
            and record.packet_digest == packet["packet_digest"]
            and record.run_id == packet["run_id"]
            and record.scope_epoch == packet["scope_epoch"]
            and record.task_id == packet["current_task_id"]
            and record.execution_binding_id == packet["execution_binding_id"]
        )

    def _stop_check(
        self,
        conn: sqlite3.Connection,
        authority: ContinuationAuthoritySnapshot | Mapping[str, Any],
        epoch: int,
        boundary: EffectBoundary,
    ) -> tuple[bool, str, str]:
        if bool(_authority_value(authority, "stop_requested", False)):
            return False, "STOP_CANONICAL", "live authority reports a canonical STOP request"
        status = str(_authority_value(authority, "status", "ACTIVE")).upper()
        if status in {"STOPPED", "COMPLETED"}:
            return False, "STOP_CANONICAL", f"live authority status '{status}' blocks continuation"
        if not self.enforce_stop_fence:
            return True, "ALLOWED", "STOP-fence enforcement was explicitly disabled by the caller"

        has_cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='scope_cursors'"
        ).fetchone()
        if has_cursor is None:
            return False, "STOP_FENCE_SCHEMA_MISSING", "canonical scope cursor table is missing"
        result = EffectBoundaryGuard.check(
            conn,
            self.project_id,
            int(epoch),
            boundary,
            raise_on_violation=False,
        )
        return result.allowed, result.reason_code, result.message

    @staticmethod
    def _invalid_result(code: str, message: str) -> ContinuationLeaseResult:
        return ContinuationLeaseResult(False, code, message)

    def _validate(
        self,
        packet: ContinuationPacket | Mapping[str, Any] | bytes,
        authority: ContinuationAuthoritySnapshot | Mapping[str, Any],
        now: datetime,
    ) -> tuple[ContinuationPacket | None, ContinuationValidationResult | None]:
        try:
            typed = _canonical_packet(packet)
        except ContinuationLeaseError as exc:
            return None, ContinuationValidationResult(False, exc.code, str(exc), None)
        result = validate_packet(typed, authority, now=now)
        return typed, result

    def _insert_available(self, conn: sqlite3.Connection, packet: ContinuationPacket, now_iso: str) -> ContinuationLeaseRecord:
        resource_id = self.resource_id_for(packet)
        lease_id = self.lease_id_for(self.project_id, resource_id)
        conn.execute(
            """
            INSERT INTO leases (
                lease_id, project_id, resource_type, resource_id, holder_token,
                status, acquired_at, expires_at, fence, lease_kind,
                continuation_id, packet_digest, run_id, scope_epoch, task_id,
                execution_binding_id, owner_id, owner_token_hash, generation,
                state_revision, last_transition_reason, completed_at, abandoned_at
            ) VALUES (?, ?, ?, ?, '', 'AVAILABLE', ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 0, 1, 'INITIALIZED', NULL, NULL)
            """,
            (
                lease_id,
                self.project_id,
                CONTINUATION_LEASE_RESOURCE_TYPE,
                resource_id,
                now_iso,
                now_iso,
                CONTINUATION_LEASE_RESOURCE_TYPE,
                packet.continuation_id,
                packet["packet_digest"],
                packet["run_id"],
                packet["scope_epoch"],
                packet["current_task_id"],
                packet["execution_binding_id"],
            ),
        )
        record = self._select_for_packet(conn, packet)
        if record is None:
            raise ContinuationLeaseError("LEASE_INSERT_READBACK_FAILED", "new continuation lease was not readable")
        return record

    def claim(
        self,
        packet: ContinuationPacket | Mapping[str, Any] | bytes,
        authority: ContinuationAuthoritySnapshot | Mapping[str, Any],
        *,
        owner_id: str,
        now: datetime | str | None = None,
    ) -> ContinuationLeaseResult:
        """Atomically claim one AVAILABLE continuation for the current epoch."""

        if not isinstance(owner_id, str) or not owner_id.strip():
            return self._invalid_result("OWNER_ID_REQUIRED", "claim requires a non-empty owner id")
        current = self._now(now)
        typed, validation = self._validate(packet, authority, current)
        if typed is None or validation is None or not validation.valid:
            return self._invalid_result(
                validation.code if validation is not None else "INVALID_PACKET",
                validation.message if validation is not None else "packet could not be validated",
            )
        now_iso = _iso(current, field="now")

        try:
            with self._transaction() as conn:
                self._ensure_project(conn)
                # Revalidate from the same caller-supplied live snapshot inside
                # the writer transaction, before any lease row can change.
                inner_validation = validate_packet(typed, authority, now=current)
                if not inner_validation.valid:
                    return self._invalid_result(inner_validation.code, inner_validation.message)
                allowed, reason_code, message = self._stop_check(
                    conn, authority, int(typed["scope_epoch"]), EffectBoundary.QUEUE_CLAIM
                )
                if not allowed:
                    return self._invalid_result(reason_code, message)

                record = self._select_for_packet(conn, typed)
                if record is None:
                    record = self._insert_available(conn, typed, now_iso)
                if not self._packet_matches_record(typed, record):
                    return self._invalid_result("LEASE_IDENTITY_MISMATCH", "stored lease identity differs from packet")
                if record.status == "COMPLETED":
                    return ContinuationLeaseResult(False, "COMPLETED_CONTINUATION", "completed continuation cannot be claimed", record)
                if record.status == "ABANDONED":
                    return ContinuationLeaseResult(False, "ABANDONED_CONTINUATION", "abandoned continuation cannot be claimed", record)
                if record.status == "CLAIMED":
                    if _parse_utc(record.expires_at, field="lease.expires_at") > current:
                        return ContinuationLeaseResult(False, "LEASE_HELD", "continuation is already claimed", record)
                    return ContinuationLeaseResult(
                        False,
                        "EXPIRED_RECLAIM_REQUIRED",
                        "expired continuation requires explicit deterministic reclaim",
                        record,
                    )
                if record.status not in {"AVAILABLE", "RELEASED", "EXPIRED"}:
                    return ContinuationLeaseResult(False, "LEASE_NOT_CLAIMABLE", f"lease status '{record.status}' is not claimable", record)

                owner_token = secrets.token_urlsafe(32)
                owner_hash = _token_digest(owner_token)
                next_generation = max(1, record.generation + 1)
                next_state_revision = record.state_revision + 1
                expires_iso = _iso(current + timedelta(seconds=self.lease_seconds), field="expires_at")
                updated = conn.execute(
                    """
                    UPDATE leases
                    SET holder_token = ?, status = 'CLAIMED', acquired_at = ?,
                        expires_at = ?, fence = ?, owner_id = ?,
                        owner_token_hash = ?, generation = ?, state_revision = ?,
                        last_transition_reason = 'CLAIMED', completed_at = NULL,
                        abandoned_at = NULL
                    WHERE lease_id = ? AND project_id = ?
                      AND status IN ('AVAILABLE', 'RELEASED', 'EXPIRED')
                      AND state_revision = ?
                    """,
                    (
                        owner_hash,
                        now_iso,
                        expires_iso,
                        next_generation,
                        owner_id,
                        owner_hash,
                        next_generation,
                        next_state_revision,
                        record.lease_id,
                        self.project_id,
                        record.state_revision,
                    ),
                )
                if updated.rowcount != 1:
                    return self._invalid_result("CLAIM_CAS_LOST", "AVAILABLE -> CLAIMED compare-and-set lost")
                self._bump_project_revision(conn, now_iso)
                claimed = self._select_for_packet(conn, typed)
                if claimed is None:
                    raise ContinuationLeaseError("LEASE_CLAIM_READBACK_FAILED", "claimed lease was not readable")
                self._append_event(conn, claimed, "CONTINUATION_LEASE_CLAIMED", "CLAIMED", now_iso)
                return ContinuationLeaseResult(True, "CLAIMED", "continuation lease claimed", claimed, owner_token, True)
        except (sqlite3.OperationalError, sqlite3.IntegrityError) as exc:
            return self._invalid_result("CLAIM_TRANSACTION_FAILED", f"continuation claim failed closed: {exc}")

    claim_continuation = claim

    def reclaim(
        self,
        packet: ContinuationPacket | Mapping[str, Any] | bytes,
        authority: ContinuationAuthoritySnapshot | Mapping[str, Any],
        *,
        owner_id: str,
        reason: str,
        now: datetime | str | None = None,
    ) -> ContinuationLeaseResult:
        """Reclaim only an expired, still-live continuation with a new token."""

        if not isinstance(owner_id, str) or not owner_id.strip():
            return self._invalid_result("OWNER_ID_REQUIRED", "reclaim requires a non-empty owner id")
        if not isinstance(reason, str) or not reason.strip():
            return self._invalid_result("RECLAIM_REASON_REQUIRED", "reclaim requires an explicit reason")
        current = self._now(now)
        typed, validation = self._validate(packet, authority, current)
        if typed is None or validation is None or not validation.valid:
            return self._invalid_result(
                validation.code if validation is not None else "INVALID_PACKET",
                validation.message if validation is not None else "packet could not be validated",
            )
        now_iso = _iso(current, field="now")

        try:
            with self._transaction() as conn:
                self._ensure_project(conn)
                inner_validation = validate_packet(typed, authority, now=current)
                if not inner_validation.valid:
                    return self._invalid_result(inner_validation.code, inner_validation.message)
                allowed, reason_code, message = self._stop_check(
                    conn, authority, int(typed["scope_epoch"]), EffectBoundary.QUEUE_CLAIM
                )
                if not allowed:
                    return self._invalid_result(reason_code, message)
                record = self._select_for_packet(conn, typed)
                if record is None:
                    return self._invalid_result("LEASE_NOT_FOUND", "no continuation lease exists to reclaim")
                if record.status == "COMPLETED":
                    return ContinuationLeaseResult(False, "COMPLETED_CONTINUATION", "completed continuation cannot be reclaimed", record)
                if record.status == "ABANDONED":
                    return ContinuationLeaseResult(False, "ABANDONED_CONTINUATION", "abandoned continuation cannot be reclaimed", record)
                if record.status != "CLAIMED":
                    return ContinuationLeaseResult(False, "LEASE_NOT_EXPIRED", "only a claimed lease can be reclaimed", record)
                if _parse_utc(record.expires_at, field="lease.expires_at") > current:
                    return ContinuationLeaseResult(False, "PRE_EXPIRY_RECLAIM", "lease has not expired at the supplied virtual clock", record)

                owner_token = secrets.token_urlsafe(32)
                owner_hash = _token_digest(owner_token)
                next_generation = record.generation + 1
                next_state_revision = record.state_revision + 1
                expires_iso = _iso(current + timedelta(seconds=self.lease_seconds), field="expires_at")
                bounded_reason = " ".join(reason.split())[:256]
                updated = conn.execute(
                    """
                    UPDATE leases
                    SET holder_token = ?, status = 'CLAIMED', acquired_at = ?,
                        expires_at = ?, fence = ?, owner_id = ?,
                        owner_token_hash = ?, generation = ?, state_revision = ?,
                        last_transition_reason = ?, completed_at = NULL,
                        abandoned_at = NULL
                    WHERE lease_id = ? AND project_id = ? AND status = 'CLAIMED'
                      AND state_revision = ? AND expires_at = ?
                    """,
                    (
                        owner_hash,
                        now_iso,
                        expires_iso,
                        next_generation,
                        owner_id,
                        owner_hash,
                        next_generation,
                        next_state_revision,
                        f"RECLAIM:{bounded_reason}",
                        record.lease_id,
                        self.project_id,
                        record.state_revision,
                        record.expires_at,
                    ),
                )
                if updated.rowcount != 1:
                    return self._invalid_result("RECLAIM_CAS_LOST", "expired lease reclaim compare-and-set lost")
                self._bump_project_revision(conn, now_iso)
                reclaimed = self._select_for_packet(conn, typed)
                if reclaimed is None:
                    raise ContinuationLeaseError("LEASE_RECLAIM_READBACK_FAILED", "reclaimed lease was not readable")
                self._append_event(conn, reclaimed, "CONTINUATION_LEASE_RECLAIMED", bounded_reason, now_iso)
                return ContinuationLeaseResult(True, "RECLAIMED", "expired continuation lease reclaimed", reclaimed, owner_token, True)
        except (sqlite3.OperationalError, sqlite3.IntegrityError) as exc:
            return self._invalid_result("RECLAIM_TRANSACTION_FAILED", f"continuation reclaim failed closed: {exc}")

    reclaim_expired = reclaim

    def renew(
        self,
        packet: ContinuationPacket | Mapping[str, Any] | bytes,
        authority: ContinuationAuthoritySnapshot | Mapping[str, Any],
        owner_token: str,
        *,
        now: datetime | str | None = None,
    ) -> ContinuationLeaseResult:
        """Renew only the current owner before expiry; never reclaim implicitly."""

        current = self._now(now)
        typed, validation = self._validate(packet, authority, current)
        if typed is None or validation is None or not validation.valid:
            return self._invalid_result(validation.code if validation else "INVALID_PACKET", validation.message if validation else "packet invalid")
        token_hash = _token_digest(owner_token)
        if not token_hash:
            return self._invalid_result("OWNER_TOKEN_INVALID", "renew requires the opaque owner token")
        now_iso = _iso(current, field="now")
        try:
            with self._transaction() as conn:
                self._ensure_project(conn)
                inner_validation = validate_packet(typed, authority, now=current)
                if not inner_validation.valid:
                    return self._invalid_result(inner_validation.code, inner_validation.message)
                allowed, reason_code, message = self._stop_check(
                    conn, authority, int(typed["scope_epoch"]), EffectBoundary.QUEUE_CLAIM
                )
                if not allowed:
                    return self._invalid_result(reason_code, message)
                record = self._select_for_packet(conn, typed)
                if record is None:
                    return self._invalid_result("LEASE_NOT_FOUND", "continuation lease does not exist")
                if record.owner_token_hash != token_hash:
                    return ContinuationLeaseResult(False, "FOREIGN_OWNER_TOKEN", "renew rejected for a foreign owner token", record)
                if record.status != "CLAIMED":
                    return ContinuationLeaseResult(False, "LEASE_NOT_CLAIMED", "only a claimed lease can be renewed", record)
                if _parse_utc(record.expires_at, field="lease.expires_at") <= current:
                    return ContinuationLeaseResult(False, "LATE_RENEW_AFTER_EXPIRY", "late renew cannot revive an expired lease", record)
                expires_iso = _iso(current + timedelta(seconds=self.lease_seconds), field="expires_at")
                next_state_revision = record.state_revision + 1
                updated = conn.execute(
                    """
                    UPDATE leases
                    SET expires_at = ?, state_revision = ?, last_transition_reason = 'RENEWED'
                    WHERE lease_id = ? AND project_id = ? AND status = 'CLAIMED'
                      AND owner_token_hash = ? AND state_revision = ?
                    """,
                    (expires_iso, next_state_revision, record.lease_id, self.project_id, token_hash, record.state_revision),
                )
                if updated.rowcount != 1:
                    return self._invalid_result("RENEW_CAS_LOST", "lease renew compare-and-set lost")
                self._bump_project_revision(conn, now_iso)
                renewed = self._select_for_packet(conn, typed)
                if renewed is None:
                    raise ContinuationLeaseError("LEASE_RENEW_READBACK_FAILED", "renewed lease was not readable")
                self._append_event(conn, renewed, "CONTINUATION_LEASE_RENEWED", "RENEWED", now_iso)
                return ContinuationLeaseResult(True, "RENEWED", "continuation lease renewed", renewed, mutated=True)
        except (sqlite3.OperationalError, sqlite3.IntegrityError) as exc:
            return self._invalid_result("RENEW_TRANSACTION_FAILED", f"continuation renew failed closed: {exc}")

    renew_lease = renew

    def release(
        self,
        packet: ContinuationPacket | Mapping[str, Any] | bytes,
        owner_token: str,
        *,
        reason: str = "RELEASED",
        now: datetime | str | None = None,
    ) -> ContinuationLeaseResult:
        """Return a claimed continuation to AVAILABLE with exact-token CAS."""

        typed = _canonical_packet(packet)
        token_hash = _token_digest(owner_token)
        if not token_hash:
            return self._invalid_result("OWNER_TOKEN_INVALID", "release requires the opaque owner token")
        now_iso = _iso(self._now(now), field="now")
        try:
            with self._transaction() as conn:
                self._ensure_project(conn)
                record = self._select_for_packet(conn, typed)
                if record is None:
                    return self._invalid_result("LEASE_NOT_FOUND", "continuation lease does not exist")
                if record.owner_token_hash != token_hash:
                    return ContinuationLeaseResult(False, "FOREIGN_OWNER_TOKEN", "release rejected for a foreign owner token", record)
                if record.status == "AVAILABLE":
                    return ContinuationLeaseResult(True, "ALREADY_AVAILABLE", "continuation lease is already available", record, mutated=False, idempotent=True)
                if record.status in {"COMPLETED", "ABANDONED"}:
                    return ContinuationLeaseResult(False, "TERMINAL_LEASE", f"terminal lease cannot be released from {record.status}", record)
                if record.status != "CLAIMED":
                    return ContinuationLeaseResult(False, "LEASE_NOT_CLAIMED", "only a claimed lease can be released", record)
                if _parse_utc(record.expires_at, field="lease.expires_at") <= self._now(now):
                    return ContinuationLeaseResult(False, "EXPIRED_LEASE", "expired owner cannot release a lease", record)
                next_state_revision = record.state_revision + 1
                bounded_reason = " ".join(str(reason).split())[:256] or "RELEASED"
                updated = conn.execute(
                    """
                    UPDATE leases
                    SET holder_token = '', status = 'AVAILABLE', owner_id = NULL,
                        owner_token_hash = NULL, state_revision = ?,
                        last_transition_reason = ?, expires_at = acquired_at
                    WHERE lease_id = ? AND project_id = ? AND status = 'CLAIMED'
                      AND owner_token_hash = ? AND state_revision = ?
                    """,
                    (next_state_revision, f"RELEASE:{bounded_reason}", record.lease_id, self.project_id, token_hash, record.state_revision),
                )
                if updated.rowcount != 1:
                    return self._invalid_result("RELEASE_CAS_LOST", "lease release compare-and-set lost")
                self._bump_project_revision(conn, now_iso)
                released = self._select_for_packet(conn, typed)
                if released is None:
                    raise ContinuationLeaseError("LEASE_RELEASE_READBACK_FAILED", "released lease was not readable")
                self._append_event(conn, released, "CONTINUATION_LEASE_RELEASED", bounded_reason, now_iso)
                return ContinuationLeaseResult(True, "RELEASED", "continuation lease released", released, mutated=True)
        except (sqlite3.OperationalError, sqlite3.IntegrityError) as exc:
            return self._invalid_result("RELEASE_TRANSACTION_FAILED", f"continuation release failed closed: {exc}")

    release_lease = release

    def complete(
        self,
        packet: ContinuationPacket | Mapping[str, Any] | bytes,
        owner_token: str,
        *,
        now: datetime | str | None = None,
    ) -> ContinuationLeaseResult:
        """Complete a lease exactly once; same-token replay is idempotent."""

        typed = _canonical_packet(packet)
        token_hash = _token_digest(owner_token)
        if not token_hash:
            return self._invalid_result("OWNER_TOKEN_INVALID", "complete requires the opaque owner token")
        current = self._now(now)
        now_iso = _iso(current, field="now")
        try:
            with self._transaction() as conn:
                self._ensure_project(conn)
                record = self._select_for_packet(conn, typed)
                if record is None:
                    return self._invalid_result("LEASE_NOT_FOUND", "continuation lease does not exist")
                if record.owner_token_hash != token_hash:
                    return ContinuationLeaseResult(False, "FOREIGN_OWNER_TOKEN", "complete rejected for a foreign owner token", record)
                if record.status == "COMPLETED":
                    return ContinuationLeaseResult(True, "ALREADY_COMPLETED", "continuation completion is idempotent", record, mutated=False, idempotent=True)
                if record.status != "CLAIMED":
                    return ContinuationLeaseResult(False, "LEASE_NOT_CLAIMED", "only a claimed lease can be completed", record)
                if _parse_utc(record.expires_at, field="lease.expires_at") <= current:
                    return ContinuationLeaseResult(False, "EXPIRED_LEASE", "expired owner cannot complete a lease", record)
                next_state_revision = record.state_revision + 1
                updated = conn.execute(
                    """
                    UPDATE leases
                    SET status = 'COMPLETED', state_revision = ?,
                        last_transition_reason = 'COMPLETED', completed_at = ?
                    WHERE lease_id = ? AND project_id = ? AND status = 'CLAIMED'
                      AND owner_token_hash = ? AND state_revision = ?
                    """,
                    (next_state_revision, now_iso, record.lease_id, self.project_id, token_hash, record.state_revision),
                )
                if updated.rowcount != 1:
                    return self._invalid_result("COMPLETE_CAS_LOST", "lease completion compare-and-set lost")
                self._bump_project_revision(conn, now_iso)
                completed = self._select_for_packet(conn, typed)
                if completed is None:
                    raise ContinuationLeaseError("LEASE_COMPLETE_READBACK_FAILED", "completed lease was not readable")
                self._append_event(conn, completed, "CONTINUATION_LEASE_COMPLETED", "COMPLETED", now_iso)
                return ContinuationLeaseResult(True, "COMPLETED", "continuation lease completed", completed, mutated=True)
        except (sqlite3.OperationalError, sqlite3.IntegrityError) as exc:
            return self._invalid_result("COMPLETE_TRANSACTION_FAILED", f"continuation completion failed closed: {exc}")

    complete_lease = complete

    def abandon(
        self,
        packet: ContinuationPacket | Mapping[str, Any] | bytes,
        owner_token: str,
        *,
        reason: str = "ABANDONED",
        now: datetime | str | None = None,
    ) -> ContinuationLeaseResult:
        """Persist a terminal abandon decision under exact owner-token CAS."""

        typed = _canonical_packet(packet)
        token_hash = _token_digest(owner_token)
        if not token_hash:
            return self._invalid_result("OWNER_TOKEN_INVALID", "abandon requires the opaque owner token")
        current = self._now(now)
        now_iso = _iso(current, field="now")
        try:
            with self._transaction() as conn:
                self._ensure_project(conn)
                record = self._select_for_packet(conn, typed)
                if record is None:
                    return self._invalid_result("LEASE_NOT_FOUND", "continuation lease does not exist")
                if record.owner_token_hash != token_hash:
                    return ContinuationLeaseResult(False, "FOREIGN_OWNER_TOKEN", "abandon rejected for a foreign owner token", record)
                if record.status == "ABANDONED":
                    return ContinuationLeaseResult(True, "ALREADY_ABANDONED", "continuation abandon is idempotent", record, mutated=False, idempotent=True)
                if record.status == "COMPLETED":
                    return ContinuationLeaseResult(False, "TERMINAL_LEASE", "completed lease cannot be abandoned", record)
                if record.status != "CLAIMED":
                    return ContinuationLeaseResult(False, "LEASE_NOT_CLAIMED", "only a claimed lease can be abandoned", record)
                if _parse_utc(record.expires_at, field="lease.expires_at") <= current:
                    return ContinuationLeaseResult(False, "EXPIRED_LEASE", "expired owner cannot abandon a lease", record)
                next_state_revision = record.state_revision + 1
                bounded_reason = " ".join(str(reason).split())[:256] or "ABANDONED"
                updated = conn.execute(
                    """
                    UPDATE leases
                    SET status = 'ABANDONED', state_revision = ?,
                        last_transition_reason = ?, abandoned_at = ?
                    WHERE lease_id = ? AND project_id = ? AND status = 'CLAIMED'
                      AND owner_token_hash = ? AND state_revision = ?
                    """,
                    (next_state_revision, f"ABANDON:{bounded_reason}", now_iso, record.lease_id, self.project_id, token_hash, record.state_revision),
                )
                if updated.rowcount != 1:
                    return self._invalid_result("ABANDON_CAS_LOST", "lease abandon compare-and-set lost")
                self._bump_project_revision(conn, now_iso)
                abandoned = self._select_for_packet(conn, typed)
                if abandoned is None:
                    raise ContinuationLeaseError("LEASE_ABANDON_READBACK_FAILED", "abandoned lease was not readable")
                self._append_event(conn, abandoned, "CONTINUATION_LEASE_ABANDONED", bounded_reason, now_iso)
                return ContinuationLeaseResult(True, "ABANDONED", "continuation lease abandoned", abandoned, mutated=True)
        except (sqlite3.OperationalError, sqlite3.IntegrityError) as exc:
            return self._invalid_result("ABANDON_TRANSACTION_FAILED", f"continuation abandon failed closed: {exc}")

    abandon_lease = abandon

    def authorize_effect(
        self,
        packet: ContinuationPacket | Mapping[str, Any] | bytes,
        authority: ContinuationAuthoritySnapshot | Mapping[str, Any],
        owner_token: str,
        *,
        now: datetime | str | None = None,
    ) -> ContinuationLeaseResult:
        """Revalidate packet, lease, epoch and STOP fence immediately before an effect."""

        current = self._now(now)
        typed, validation = self._validate(packet, authority, current)
        if typed is None or validation is None or not validation.valid:
            return self._invalid_result(validation.code if validation else "INVALID_PACKET", validation.message if validation else "packet invalid")
        token_hash = _token_digest(owner_token)
        if not token_hash:
            return self._invalid_result("OWNER_TOKEN_INVALID", "effect authorization requires the opaque owner token")
        try:
            with self._transaction() as conn:
                self._ensure_project(conn)
                # This is intentionally repeated inside the writer transaction;
                # a caller may not authorize from an earlier packet readback.
                inner_validation = validate_packet(typed, authority, now=current)
                if not inner_validation.valid:
                    return self._invalid_result(inner_validation.code, inner_validation.message)
                allowed, reason_code, message = self._stop_check(
                    conn, authority, int(typed["scope_epoch"]), EffectBoundary.DISPATCH_SEND
                )
                if not allowed:
                    return self._invalid_result(reason_code, message)
                record = self._select_for_packet(conn, typed)
                if record is None:
                    return self._invalid_result("LEASE_NOT_FOUND", "continuation lease does not exist")
                if not self._packet_matches_record(typed, record):
                    return ContinuationLeaseResult(False, "LEASE_IDENTITY_MISMATCH", "stored lease identity differs from packet", record)
                if record.owner_token_hash != token_hash:
                    return ContinuationLeaseResult(False, "FOREIGN_OWNER_TOKEN", "effect rejected for a foreign owner token", record)
                if record.status != "CLAIMED":
                    return ContinuationLeaseResult(False, "LEASE_NOT_CLAIMED", "effect requires a live claimed lease", record)
                if _parse_utc(record.expires_at, field="lease.expires_at") <= current:
                    return ContinuationLeaseResult(False, "EXPIRED_LEASE", "effect rejected after lease expiry", record)
                return ContinuationLeaseResult(True, "EFFECT_ALLOWED", "continuation effect is authorized", record)
        except (sqlite3.OperationalError, sqlite3.IntegrityError) as exc:
            return self._invalid_result("EFFECT_AUTHORIZATION_FAILED", f"effect authorization failed closed: {exc}")

    effect_allowed = authorize_effect
    can_effect = authorize_effect

    def read(
        self,
        packet: ContinuationPacket | Mapping[str, Any] | bytes,
    ) -> ContinuationLeaseRecord | None:
        """Read durable lease state without reclaiming or changing it."""

        typed = _canonical_packet(packet)
        conn = self._open_connection()
        try:
            return self._select_for_packet(conn, typed)
        finally:
            self._close_connection(conn)

    get = read
    snapshot = read

    def diagnostic_snapshot(self, packet: ContinuationPacket | Mapping[str, Any] | bytes) -> ContinuationLeaseSnapshot | None:
        record = self.read(packet)
        return None if record is None else ContinuationLeaseSnapshot.from_record(record)


__all__ = [
    "CONTINUATION_LEASE_RESOURCE_TYPE",
    "CONTINUATION_LEASE_UNDER_PROJECT_MEMORY_V2",
    "CONTINUATION_LEASE_VERSION",
    "CONTINUATION_LEASE_VERSION_EXPLICIT",
    "DEFAULT_CONTINUATION_LEASE_SECONDS",
    "SECOND_LEASE_AUTHORITY_CREATED",
    "ContinuationLeaseCoordinator",
    "ContinuationLeaseError",
    "ContinuationLeaseRecord",
    "ContinuationLeaseResult",
    "ContinuationLeaseSnapshot",
]

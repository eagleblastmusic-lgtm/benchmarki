"""NX-027 durable pre-click send intent and same-chat continuation.

The durable record lives in the canonical Project Memory v2 ``send_intents``
relation.  Browser/Native adapters are effect and observation providers only:
their local flags never decide whether a send happened.  A physical send is
allowed only after the intent, packet, lease, epoch, STOP fence, exact chat
binding and DOM preconditions have all been revalidated.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, MutableSequence, Protocol

from .continuation_lease import (
    ContinuationLeaseCoordinator,
    ContinuationLeaseRecord,
)
from .continuation_packet import (
    ContinuationAuthoritySnapshot,
    ContinuationPacket,
    ContinuationPacketError,
    deserialize_packet,
    validate_packet,
)
from .stop_fence import EffectBoundary, EffectBoundaryGuard


SEND_INTENT_VERSION = "v1"
SEND_INTENT_VERSION_EXPLICIT = True
SEND_INTENT_UNDER_CANONICAL_AUTHORITY = True
BROWSER_LOCAL_STATE_IS_SEND_AUTHORITY = False
SEND_INTENT_TABLE = "send_intents"

SEND_STATUS_PREPARED = "PREPARED"
SEND_STATUS_ALLOWED = "SEND_ALLOWED"
SEND_STATUS_PHYSICAL_ATTEMPTED = "PHYSICAL_SEND_ATTEMPTED"
SEND_STATUS_CONFIRMED = "SEND_CONFIRMED"
SEND_STATUS_ACKNOWLEDGED = "ACKNOWLEDGED"
SEND_STATUS_UNCERTAIN = "UNCERTAIN"
SEND_STATUS_RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
SEND_STATUS_CANCELLED = "CANCELLED"
SEND_STATUS_FENCED = "FENCED"

_TERMINAL_SEND_STATUSES = frozenset({SEND_STATUS_ACKNOWLEDGED, SEND_STATUS_CANCELLED, SEND_STATUS_FENCED})
_SEND_INTENT_COLUMNS = frozenset(
    {
        "intent_id",
        "intent_key",
        "continuation_id",
        "packet_digest",
        "lease_id",
        "lease_owner_token_hash",
        "lease_generation",
        "scope_epoch",
        "run_id",
        "task_id",
        "execution_binding_id",
        "expected_repo_head_before",
        "conversation_binding_id",
        "conversation_binding_proof",
        "message_digest",
        "payload",
        "intent_generation",
        "state_revision",
        "status",
        "prepared_at",
        "updated_at",
        "physical_attempted_at",
        "confirmed_at",
        "acknowledged_at",
        "delivery_evidence_json",
        "uncertainty_reason",
    }
)


class SendIntentError(RuntimeError):
    """Raised when the canonical PM v2 send-intent schema is unavailable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SendDeliveryUncertain(RuntimeError):
    """The adapter cannot prove whether the physical send reached the chat."""


def _parse_utc(value: datetime | str | None, *, field: str) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SendIntentError("INVALID_CLOCK", f"{field} is not an ISO-8601 timestamp") from exc
    else:
        raise SendIntentError("INVALID_CLOCK", f"{field} must be datetime or ISO-8601")
    if parsed.tzinfo is None:
        raise SendIntentError("INVALID_CLOCK", f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | str | None, *, field: str) -> str:
    return _parse_utc(value, field=field).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical_packet(packet: ContinuationPacket | Mapping[str, Any] | bytes) -> ContinuationPacket:
    try:
        if isinstance(packet, ContinuationPacket):
            return packet
        if isinstance(packet, bytes):
            return deserialize_packet(packet)
        return ContinuationPacket.from_mapping(packet)
    except ContinuationPacketError as exc:
        raise SendIntentError(exc.code, str(exc)) from exc


def _token_hash(token: str) -> str:
    if not isinstance(token, str) or len(token) < 20:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return _digest(encoded)


@dataclass(frozen=True)
class ExactConversationBinding:
    """Identity-verifiable binding to an already existing chat."""

    conversation_id: str
    binding_proof: str
    source: str = "canonical"
    verified: bool = True

    @classmethod
    def from_verified(cls, conversation_id: str) -> "ExactConversationBinding":
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise SendIntentError("CONVERSATION_ID_REQUIRED", "an existing conversation id is required")
        return cls(
            conversation_id=conversation_id,
            binding_proof=_digest(f"existing-chat:{conversation_id}"),
        )

    @property
    def is_exact(self) -> bool:
        return bool(
            self.verified
            and self.source == "canonical"
            and self.conversation_id
            and self.binding_proof == _digest(f"existing-chat:{self.conversation_id}")
        )


@dataclass(frozen=True)
class DomPreconditions:
    conversation_id: str | None
    composer_present: bool
    composer_enabled: bool
    composer_focused: bool = False

    @property
    def valid(self) -> bool:
        return bool(self.conversation_id and self.composer_present and self.composer_enabled)


@dataclass(frozen=True)
class StructuredSendEvidence:
    """Positive, structured proof that one message is visible in the chat."""

    intent_id: str
    continuation_id: str
    packet_digest: str
    execution_binding_id: str
    conversation_id: str
    message_digest: str
    visible_message_id: str
    evidence_type: str = "STRUCTURED_DOM_MESSAGE"
    structured: bool = True
    focus_only: bool = False
    composer_empty_only: bool = False

    @classmethod
    def focus_only_observation(cls, intent_id: str) -> "StructuredSendEvidence":
        return cls(
            intent_id=intent_id,
            continuation_id="",
            packet_digest="",
            execution_binding_id="",
            conversation_id="",
            message_digest="",
            visible_message_id="",
            evidence_type="FOCUS_ONLY",
            structured=False,
            focus_only=True,
        )

    @classmethod
    def composer_empty_observation(cls, intent_id: str) -> "StructuredSendEvidence":
        return cls(
            intent_id=intent_id,
            continuation_id="",
            packet_digest="",
            execution_binding_id="",
            conversation_id="",
            message_digest="",
            visible_message_id="",
            evidence_type="COMPOSER_EMPTY_ONLY",
            structured=False,
            composer_empty_only=True,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "continuation_id": self.continuation_id,
            "packet_digest": self.packet_digest,
            "execution_binding_id": self.execution_binding_id,
            "conversation_id": self.conversation_id,
            "message_digest": self.message_digest,
            "visible_message_id": self.visible_message_id,
            "evidence_type": self.evidence_type,
            "structured": self.structured,
            "focus_only": self.focus_only,
            "composer_empty_only": self.composer_empty_only,
        }


@dataclass(frozen=True)
class NoDeliveryProof:
    """Deterministic negative evidence that permits a safe retry."""

    intent_id: str
    conversation_id: str
    observed_effect_count: int
    probe_id: str
    probe_complete: bool = True
    proof_type: str = "NO_DELIVERY_CONFIRMED"

    @property
    def valid(self) -> bool:
        return bool(
            self.proof_type == "NO_DELIVERY_CONFIRMED"
            and self.probe_complete
            and self.observed_effect_count == 0
            and self.conversation_id
            and self.probe_id
        )


@dataclass(frozen=True)
class SendAck:
    intent_id: str
    continuation_id: str
    packet_digest: str
    execution_binding_id: str
    conversation_id: str


@dataclass(frozen=True)
class SendIntentRecord:
    intent_id: str
    intent_key: str
    project_id: str
    continuation_id: str
    packet_digest: str
    lease_id: str
    lease_owner_token_hash: str
    lease_generation: int
    scope_epoch: int
    run_id: str
    task_id: str
    execution_binding_id: str
    expected_repo_head_before: str
    conversation_binding_id: str | None
    conversation_binding_proof: str | None
    message_digest: str
    payload: str
    intent_generation: int
    state_revision: int
    status: str
    prepared_at: str
    updated_at: str
    physical_attempted_at: str | None
    confirmed_at: str | None
    acknowledged_at: str | None
    delivery_evidence: Mapping[str, Any]
    uncertainty_reason: str | None

    @property
    def conversation_id(self) -> str | None:
        return self.conversation_binding_id

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_SEND_STATUSES


@dataclass(frozen=True)
class SendIntentResult:
    accepted: bool
    reason_code: str
    message: str
    intent: SendIntentRecord | None = None
    evidence: StructuredSendEvidence | None = None
    mutated: bool = False
    idempotent: bool = False

    @property
    def allowed(self) -> bool:
        return self.accepted

    @property
    def physical_effects(self) -> int:
        return int(self.accepted and self.reason_code in {"SENT", "SEND_CONFIRMED", "ACKNOWLEDGED"})

    def __bool__(self) -> bool:
        return self.accepted


@dataclass(frozen=True)
class SameChatFlowResult:
    accepted: bool
    trace: tuple[str, ...]
    manual_user_prompts: int
    visible_sends: int
    created_conversations: int
    intent: SendIntentRecord | None
    send_result: SendIntentResult | None
    ack_result: SendIntentResult | None


class BrowserSendAdapter(Protocol):
    def snapshot(self) -> DomPreconditions: ...

    def physical_send(self, intent: SendIntentRecord) -> StructuredSendEvidence: ...


class FakeBrowser:
    """Deterministic Browser/Native adapter used by the NX-027 fault suite."""

    def __init__(
        self,
        conversation_id: str | None,
        *,
        composer_present: bool = True,
        composer_enabled: bool = True,
    ) -> None:
        self.dom = DomPreconditions(conversation_id, composer_present, composer_enabled)
        self.trace: list[str] = []
        self.local_send_attempted: set[str] = set()
        self._visible: list[StructuredSendEvidence] = []
        self.created_conversations = 0
        self.crash_after_physical = False

    @property
    def visible_send_count(self) -> int:
        return len(self._visible)

    @property
    def visible_evidence(self) -> tuple[StructuredSendEvidence, ...]:
        return tuple(self._visible)

    def snapshot(self) -> DomPreconditions:
        return self.dom

    def focus_composer(self) -> bool:
        self.dom = DomPreconditions(
            self.dom.conversation_id,
            self.dom.composer_present,
            self.dom.composer_enabled,
            True,
        )
        return self.dom.composer_focused

    def reload(self) -> None:
        self.trace.append("DOM_RELOADED")

    def physical_send(self, intent: SendIntentRecord) -> StructuredSendEvidence:
        if not self.dom.valid:
            raise RuntimeError("DOM preconditions are not satisfied")
        if self.dom.conversation_id != intent.conversation_binding_id:
            raise RuntimeError("conversation binding changed before physical send")
        existing = next((item for item in self._visible if item.intent_id == intent.intent_id), None)
        self.local_send_attempted.add(intent.intent_id)
        if existing is not None:
            return existing
        self.trace.append("PHYSICAL_SEND")
        evidence = StructuredSendEvidence(
            intent_id=intent.intent_id,
            continuation_id=intent.continuation_id,
            packet_digest=intent.packet_digest,
            execution_binding_id=intent.execution_binding_id,
            conversation_id=self.dom.conversation_id,
            message_digest=intent.message_digest,
            visible_message_id=f"visible-message-{len(self._visible) + 1}",
        )
        self._visible.append(evidence)
        if self.crash_after_physical:
            self.crash_after_physical = False
            raise SendDeliveryUncertain("adapter crashed after the physical send boundary")
        return evidence

    def no_delivery_proof(self, intent_id: str) -> NoDeliveryProof:
        observed = sum(item.intent_id == intent_id for item in self._visible)
        conversation_id = self.dom.conversation_id or ""
        return NoDeliveryProof(
            intent_id=intent_id,
            conversation_id=conversation_id,
            observed_effect_count=observed,
            probe_id=_digest(f"probe:{intent_id}:{observed}"),
        )


class SendIntentCoordinator:
    """Durable send-intent state machine bound to one PM v2 database."""

    def __init__(
        self,
        authority: Any,
        project_id: str | None = None,
        *,
        lease_coordinator: ContinuationLeaseCoordinator | None = None,
        clock: Callable[[], datetime | str] | None = None,
    ) -> None:
        self._connection: sqlite3.Connection | None = None
        self._db_path: Path | None = None
        if isinstance(authority, sqlite3.Connection):
            self._connection = authority
            self.project_id = project_id or ""
        elif isinstance(authority, ContinuationLeaseCoordinator):
            self._db_path = authority._db_path  # type: ignore[attr-defined]
            self.project_id = project_id or authority.project_id
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
            raise SendIntentError("PROJECT_ID_REQUIRED", "project_id is required for a send intent")
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lease = lease_coordinator or ContinuationLeaseCoordinator(
            self._connection if self._connection is not None else self._db_path,
            self.project_id,
            clock=self.clock,
        )
        self._validate_schema()

    def _open_connection(self) -> sqlite3.Connection:
        if self._connection is not None:
            self._connection.row_factory = sqlite3.Row
            return self._connection
        if self._db_path is None:
            raise SendIntentError("PM_V2_DATABASE_MISSING", "no PM v2 database handle was supplied")
        conn = sqlite3.connect(str(self._db_path), timeout=5.0, isolation_level=None)
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

    def _validate_schema(self) -> None:
        conn = self._open_connection()
        try:
            table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='send_intents'"
            ).fetchone()
            if table is None:
                raise SendIntentError("PM_V2_SEND_INTENT_TABLE_MISSING", "canonical PM v2 send_intents table is missing")
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(send_intents)").fetchall()}
            missing = sorted(_SEND_INTENT_COLUMNS - columns)
            if missing:
                raise SendIntentError(
                    "PM_V2_SEND_INTENT_SCHEMA_MISMATCH",
                    f"send_intents lacks required columns: {', '.join(missing)}",
                )
        finally:
            self._close_connection(conn)

    def _now(self, value: datetime | str | None) -> datetime:
        return _parse_utc(value if value is not None else self.clock(), field="now")

    def _ensure_project(self, conn: sqlite3.Connection) -> None:
        if conn.execute("SELECT 1 FROM projects WHERE project_id = ?", (self.project_id,)).fetchone() is None:
            raise SendIntentError("PROJECT_NOT_FOUND", f"project '{self.project_id}' does not exist")

    def _bump_project_revision(self, conn: sqlite3.Connection, now_iso: str) -> None:
        row = conn.execute("SELECT revision FROM projects WHERE project_id = ?", (self.project_id,)).fetchone()
        if row is None:
            raise SendIntentError("PROJECT_NOT_FOUND", f"project '{self.project_id}' does not exist")
        updated = conn.execute(
            "UPDATE projects SET revision = ?, updated_at = ? WHERE project_id = ? AND revision = ?",
            (int(row["revision"]) + 1, now_iso, self.project_id, int(row["revision"])),
        )
        if updated.rowcount != 1:
            raise SendIntentError("PROJECT_REVISION_CAS_LOST", "send-intent project revision CAS lost")

    def _append_event(self, conn: sqlite3.Connection, record: SendIntentRecord, event_type: str, reason: str, now_iso: str) -> None:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_events'").fetchone() is None:
            return
        revision = int(
            conn.execute(
                "SELECT COALESCE(MAX(revision), 0) + 1 FROM audit_events WHERE project_id = ?",
                (self.project_id,),
            ).fetchone()[0]
        )
        payload = {
            "send_intent_version": SEND_INTENT_VERSION,
            "intent_id": record.intent_id,
            "intent_key": record.intent_key,
            "continuation_id": record.continuation_id,
            "packet_digest": record.packet_digest,
            "status": record.status,
            "reason": reason,
        }
        conn.execute(
            """
            INSERT INTO audit_events (
                event_id, project_id, revision, logical_tx_id, event_type,
                human_summary, task_id, milestone_id, plan_version,
                payload_json, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"{self.project_id}:send-intent:{record.intent_id}:s{record.state_revision}",
                self.project_id,
                revision,
                f"tx-send-intent-{record.intent_id}-s{record.state_revision}",
                event_type,
                f"Send intent {record.intent_id}: {reason}",
                record.task_id,
                None,
                None,
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                now_iso,
            ),
        )

    @staticmethod
    def _record(row: sqlite3.Row | Mapping[str, Any] | None) -> SendIntentRecord | None:
        if row is None:
            return None
        try:
            evidence = json.loads(row["delivery_evidence_json"])
        except (TypeError, ValueError):
            evidence = {}
        if not isinstance(evidence, Mapping):
            evidence = {}
        return SendIntentRecord(
            intent_id=str(row["intent_id"]),
            intent_key=str(row["intent_key"]),
            project_id=str(row["project_id"]),
            continuation_id=str(row["continuation_id"]),
            packet_digest=str(row["packet_digest"]),
            lease_id=str(row["lease_id"]),
            lease_owner_token_hash=str(row["lease_owner_token_hash"]),
            lease_generation=int(row["lease_generation"]),
            scope_epoch=int(row["scope_epoch"]),
            run_id=str(row["run_id"]),
            task_id=str(row["task_id"]),
            execution_binding_id=str(row["execution_binding_id"]),
            expected_repo_head_before=str(row["expected_repo_head_before"]),
            conversation_binding_id=None if row["conversation_binding_id"] is None else str(row["conversation_binding_id"]),
            conversation_binding_proof=None if row["conversation_binding_proof"] is None else str(row["conversation_binding_proof"]),
            message_digest=str(row["message_digest"]),
            payload=str(row["payload"]),
            intent_generation=int(row["intent_generation"]),
            state_revision=int(row["state_revision"]),
            status=str(row["status"]),
            prepared_at=str(row["prepared_at"]),
            updated_at=str(row["updated_at"]),
            physical_attempted_at=None if row["physical_attempted_at"] is None else str(row["physical_attempted_at"]),
            confirmed_at=None if row["confirmed_at"] is None else str(row["confirmed_at"]),
            acknowledged_at=None if row["acknowledged_at"] is None else str(row["acknowledged_at"]),
            delivery_evidence=evidence,
            uncertainty_reason=None if row["uncertainty_reason"] is None else str(row["uncertainty_reason"]),
        )

    def _select(self, conn: sqlite3.Connection, intent_id: str) -> SendIntentRecord | None:
        row = conn.execute(
            "SELECT * FROM send_intents WHERE project_id = ? AND intent_id = ?",
            (self.project_id, intent_id),
        ).fetchone()
        return self._record(row)

    def read(self, intent_id: str) -> SendIntentRecord | None:
        conn = self._open_connection()
        try:
            return self._select(conn, intent_id)
        finally:
            self._close_connection(conn)

    get = read

    @staticmethod
    def intent_key_for(
        packet: ContinuationPacket,
        lease: ContinuationLeaseRecord,
        payload: str,
        binding: ExactConversationBinding | None,
        intent_generation: int,
    ) -> str:
        identity = {
            "project_id": packet["project_id"],
            "continuation_id": packet.continuation_id,
            "packet_digest": packet["packet_digest"],
            "lease_id": lease.lease_id,
            "lease_generation": lease.generation,
            "scope_epoch": packet["scope_epoch"],
            "run_id": packet["run_id"],
            "task_id": packet["current_task_id"],
            "execution_binding_id": packet["execution_binding_id"],
            "expected_repo_head_before": packet["expected_repo_head_before"],
            "conversation_binding_id": binding.conversation_id if binding is not None else None,
            "conversation_binding_proof": binding.binding_proof if binding is not None else None,
            "message_digest": _digest(payload),
            "intent_generation": intent_generation,
        }
        return _json_digest(identity)

    def _live_lease_check(
        self,
        conn: sqlite3.Connection,
        packet: ContinuationPacket,
        owner_token: str,
        now: datetime,
        *,
        boundary: EffectBoundary,
    ) -> tuple[bool, str, str, ContinuationLeaseRecord | None]:
        token_hash = _token_hash(owner_token)
        if not token_hash:
            return False, "OWNER_TOKEN_INVALID", "opaque owner token is missing", None
        row = conn.execute(
            """
            SELECT * FROM leases
            WHERE project_id = ? AND resource_type = 'CONTINUATION'
              AND resource_id = ?
            """,
            (self.project_id, ContinuationLeaseCoordinator.resource_id_for(packet)),
        ).fetchone()
        lease = ContinuationLeaseCoordinator._record(row)  # type: ignore[attr-defined]
        if lease is None:
            return False, "LEASE_NOT_FOUND", "continuation lease does not exist", None
        if lease.owner_token_hash != token_hash:
            return False, "STALE_LEASE_OWNER", "lease owner token is no longer current", lease
        if lease.status != "CLAIMED":
            return False, "LEASE_NOT_CLAIMED", "physical send requires a claimed continuation lease", lease
        if _parse_utc(lease.expires_at, field="lease.expires_at") <= now:
            return False, "LEASE_EXPIRED", "continuation lease has expired", lease
        if lease.continuation_id != packet.continuation_id or lease.packet_digest != packet["packet_digest"]:
            return False, "LEASE_IDENTITY_MISMATCH", "lease identity does not match packet", lease
        if self._connection is not None:
            conn.row_factory = sqlite3.Row
        fence = EffectBoundaryGuard.check(
            conn,
            self.project_id,
            int(packet["scope_epoch"]),
            boundary,
            raise_on_violation=False,
        )
        if not fence.allowed:
            return False, fence.reason_code, fence.message, lease
        return True, "ALLOWED", "lease and STOP fence are valid", lease

    @staticmethod
    def _binding_matches(record: SendIntentRecord, binding: ExactConversationBinding | None) -> bool:
        if binding is None or not binding.is_exact:
            return False
        return (
            record.conversation_binding_id == binding.conversation_id
            and record.conversation_binding_proof == binding.binding_proof
        )

    @staticmethod
    def _dom_check(record: SendIntentRecord, dom: DomPreconditions) -> tuple[bool, str, str]:
        if not record.conversation_binding_id or not record.conversation_binding_proof:
            return False, "MISSING_CONVERSATION_BINDING", "same-chat send requires an exact conversation binding"
        if record.conversation_binding_proof != _digest(f"existing-chat:{record.conversation_binding_id}"):
            return False, "GUESSED_CONVERSATION_IDENTITY", "conversation identity proof is not canonical"
        if dom.conversation_id != record.conversation_binding_id:
            return False, "CONVERSATION_BINDING_MISMATCH", "DOM conversation does not equal the exact binding"
        if not dom.composer_present:
            return False, "MISSING_COMPOSER", "composer is not present; no click or send is allowed"
        if not dom.composer_enabled:
            return False, "COMPOSER_DISABLED", "composer is disabled; no click or send is allowed"
        return True, "DOM_VALID", "exact conversation and composer preconditions are valid"

    @staticmethod
    def _invalid(code: str, message: str, intent: SendIntentRecord | None = None) -> SendIntentResult:
        return SendIntentResult(False, code, message, intent)

    def prepare(
        self,
        packet: ContinuationPacket | Mapping[str, Any] | bytes,
        authority: ContinuationAuthoritySnapshot | Mapping[str, Any],
        owner_token: str,
        payload: str,
        *,
        conversation_binding: ExactConversationBinding | None = None,
        intent_generation: int | None = None,
        now: datetime | str | None = None,
        trace: MutableSequence[str] | None = None,
    ) -> SendIntentResult:
        """Persist PREPARED before any Browser/Native physical operation."""

        try:
            typed = _canonical_packet(packet)
        except SendIntentError as exc:
            return self._invalid(exc.code, str(exc))
        current = self._now(now)
        validation = validate_packet(typed, authority, now=current)
        if not validation.valid:
            return self._invalid(validation.code, validation.message)
        if not isinstance(payload, str) or not payload:
            return self._invalid("PAYLOAD_REQUIRED", "durable send intent requires a non-empty payload")
        if len(payload.encode("utf-8")) > 64 * 1024:
            return self._invalid("PAYLOAD_TOO_LARGE", "send intent payload exceeds the bounded size")
        token_hash = _token_hash(owner_token)
        if not token_hash:
            return self._invalid("OWNER_TOKEN_INVALID", "prepare requires the opaque lease owner token")
        if conversation_binding is not None and not conversation_binding.is_exact:
            return self._invalid("GUESSED_CONVERSATION_IDENTITY", "conversation binding is not identity-verifiable")

        lease = self.lease.read(typed)
        if lease is None:
            return self._invalid("LEASE_NOT_FOUND", "a claimed NX-026 lease is required before preparing a send")
        if lease.owner_token_hash != token_hash or lease.status != "CLAIMED":
            return self._invalid("STALE_LEASE_OWNER", "send intent owner is not the current claimed lease")
        if _parse_utc(lease.expires_at, field="lease.expires_at") <= current:
            return self._invalid("LEASE_EXPIRED", "send intent cannot be prepared after lease expiry")
        generation = lease.generation if intent_generation is None else intent_generation
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            return self._invalid("INVALID_INTENT_GENERATION", "intent_generation must be a positive integer")
        key = self.intent_key_for(typed, lease, payload, conversation_binding, generation)
        intent_id = f"send-intent-{key.split(':', 1)[1][:40]}"
        now_iso = _iso(current, field="now")

        try:
            with self._transaction() as conn:
                self._ensure_project(conn)
                inner_validation = validate_packet(typed, authority, now=current)
                if not inner_validation.valid:
                    return self._invalid(inner_validation.code, inner_validation.message)
                allowed, reason_code, message, current_lease = self._live_lease_check(
                    conn, typed, owner_token, current, boundary=EffectBoundary.QUEUE_CLAIM
                )
                if not allowed or current_lease is None:
                    return self._invalid(reason_code, message)
                existing_row = conn.execute(
                    "SELECT * FROM send_intents WHERE project_id = ? AND intent_key = ?",
                    (self.project_id, key),
                ).fetchone()
                existing = self._record(existing_row)
                if existing is not None:
                    if existing.intent_id != intent_id or existing.payload != payload:
                        return self._invalid("INTENT_KEY_COLLISION", "logical send intent identity collided", existing)
                    return SendIntentResult(True, "ALREADY_PREPARED", "duplicate prepare returned the existing durable intent", existing, mutated=False, idempotent=True)
                conn.execute(
                    """
                    INSERT INTO send_intents (
                        intent_id, intent_key, project_id, continuation_id,
                        packet_digest, lease_id, lease_owner_token_hash,
                        lease_generation, scope_epoch, run_id, task_id,
                        execution_binding_id, expected_repo_head_before,
                        conversation_binding_id, conversation_binding_proof,
                        message_digest, payload, intent_generation, state_revision,
                        status, prepared_at, updated_at, delivery_evidence_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'PREPARED', ?, ?, '{}')
                    """,
                    (
                        intent_id,
                        key,
                        self.project_id,
                        typed.continuation_id,
                        typed["packet_digest"],
                        current_lease.lease_id,
                        token_hash,
                        current_lease.generation,
                        typed["scope_epoch"],
                        typed["run_id"],
                        typed["current_task_id"],
                        typed["execution_binding_id"],
                        typed["expected_repo_head_before"],
                        conversation_binding.conversation_id if conversation_binding is not None else None,
                        conversation_binding.binding_proof if conversation_binding is not None else None,
                        _digest(payload),
                        payload,
                        generation,
                        now_iso,
                        now_iso,
                    ),
                )
                self._bump_project_revision(conn, now_iso)
                record = self._select(conn, intent_id)
                if record is None:
                    raise SendIntentError("INTENT_READBACK_FAILED", "prepared send intent was not readable")
                self._append_event(conn, record, "SEND_INTENT_PREPARED", "PREPARED", now_iso)
                if trace is not None:
                    trace.append("INTENT_PREPARED")
                return SendIntentResult(True, "PREPARED", "durable send intent prepared", record, mutated=True)
        except (sqlite3.OperationalError, sqlite3.IntegrityError) as exc:
            return self._invalid("PREPARE_TRANSACTION_FAILED", f"send intent prepare failed closed: {exc}")

    prepare_send_intent = prepare

    def _fence_intent(self, intent_id: str, reason: str, now: datetime) -> SendIntentRecord | None:
        now_iso = _iso(now, field="now")
        try:
            with self._transaction() as conn:
                self._ensure_project(conn)
                record = self._select(conn, intent_id)
                return self._fence_on_conn(conn, record, reason, now_iso)
        except (sqlite3.OperationalError, sqlite3.IntegrityError):
            return self.read(intent_id)

    def _fence_on_conn(
        self,
        conn: sqlite3.Connection,
        record: SendIntentRecord | None,
        reason: str,
        now_iso: str,
    ) -> SendIntentRecord | None:
        if record is None or record.status in _TERMINAL_SEND_STATUSES or record.status == SEND_STATUS_PHYSICAL_ATTEMPTED:
            return record
        next_revision = record.state_revision + 1
        updated = conn.execute(
            """
            UPDATE send_intents
            SET status = 'FENCED', state_revision = ?, updated_at = ?,
                uncertainty_reason = ?
            WHERE project_id = ? AND intent_id = ? AND state_revision = ?
              AND status IN ('PREPARED', 'SEND_ALLOWED', 'UNCERTAIN', 'RECONCILIATION_REQUIRED')
            """,
            (next_revision, now_iso, reason[:256], self.project_id, record.intent_id, record.state_revision),
        )
        if updated.rowcount != 1:
            return record
        self._bump_project_revision(conn, now_iso)
        fenced = self._select(conn, record.intent_id)
        if fenced is not None:
            self._append_event(conn, fenced, "SEND_INTENT_FENCED", reason, now_iso)
        return fenced

    def allow_send(
        self,
        intent_id: str,
        packet: ContinuationPacket | Mapping[str, Any] | bytes,
        authority: ContinuationAuthoritySnapshot | Mapping[str, Any],
        owner_token: str,
        dom: DomPreconditions,
        *,
        now: datetime | str | None = None,
        trace: MutableSequence[str] | None = None,
    ) -> SendIntentResult:
        """Move PREPARED -> SEND_ALLOWED only with exact DOM and authority proof."""

        current = self._now(now)
        try:
            typed = _canonical_packet(packet)
        except SendIntentError as exc:
            return self._invalid(exc.code, str(exc))
        validation = validate_packet(typed, authority, now=current)
        if not validation.valid:
            fenced = self._fence_intent(intent_id, validation.code, current)
            return self._invalid(validation.code, validation.message, fenced)
        readable = self.read(intent_id)
        dom_ok, dom_code, dom_message = (self._dom_check(readable, dom) if readable is not None else (False, "INTENT_NOT_FOUND", "send intent does not exist"))
        if not dom_ok:
            return self._invalid(dom_code, dom_message, readable)
        now_iso = _iso(current, field="now")
        try:
            with self._transaction() as conn:
                self._ensure_project(conn)
                record = self._select(conn, intent_id)
                if record is None:
                    return self._invalid("INTENT_NOT_FOUND", "send intent does not exist")
                if not self._packet_matches(record, typed):
                    return self._invalid("STALE_AUTHORITY", "packet does not match durable send intent", record)
                if record.lease_owner_token_hash != _token_hash(owner_token):
                    return self._invalid("STALE_INTENT_OWNER", "send intent owner token is not current", record)
                if record.status == SEND_STATUS_ALLOWED:
                    return SendIntentResult(True, "SEND_ALLOWED", "physical send was already allowed", record, mutated=False, idempotent=True)
                if record.status != SEND_STATUS_PREPARED:
                    return self._invalid("INTENT_NOT_SENDABLE", f"intent state '{record.status}' cannot enter SEND_ALLOWED", record)
                allowed, reason_code, message, _ = self._live_lease_check(
                    conn, typed, owner_token, current, boundary=EffectBoundary.DISPATCH_SEND
                )
                if not allowed:
                    fenced = self._fence_on_conn(conn, record, reason_code, now_iso)
                    return self._invalid(reason_code, message, fenced or record)
                next_revision = record.state_revision + 1
                updated = conn.execute(
                    """
                    UPDATE send_intents
                    SET status = 'SEND_ALLOWED', state_revision = ?, updated_at = ?
                    WHERE project_id = ? AND intent_id = ? AND state_revision = ?
                      AND status = 'PREPARED'
                    """,
                    (next_revision, now_iso, self.project_id, intent_id, record.state_revision),
                )
                if updated.rowcount != 1:
                    return self._invalid("SEND_ALLOWED_CAS_LOST", "send intent allow CAS lost", record)
                self._bump_project_revision(conn, now_iso)
                allowed_record = self._select(conn, intent_id)
                if allowed_record is None:
                    raise SendIntentError("INTENT_READBACK_FAILED", "SEND_ALLOWED intent was not readable")
                self._append_event(conn, allowed_record, "SEND_INTENT_ALLOWED", "SEND_ALLOWED", now_iso)
                if trace is not None:
                    trace.append("DOM_PRECONDITIONS_VALID")
                return SendIntentResult(True, "SEND_ALLOWED", "DOM and authority preconditions validated", allowed_record, mutated=True)
        except (sqlite3.OperationalError, sqlite3.IntegrityError) as exc:
            return self._invalid("SEND_ALLOWED_TRANSACTION_FAILED", f"send intent allow failed closed: {exc}")

    authorize_send = allow_send

    @staticmethod
    def _packet_matches(record: SendIntentRecord, packet: ContinuationPacket) -> bool:
        return bool(
            record.project_id == packet["project_id"]
            and record.continuation_id == packet.continuation_id
            and record.packet_digest == packet["packet_digest"]
            and record.scope_epoch == packet["scope_epoch"]
            and record.run_id == packet["run_id"]
            and record.task_id == packet["current_task_id"]
            and record.execution_binding_id == packet["execution_binding_id"]
            and record.expected_repo_head_before == packet["expected_repo_head_before"]
        )

    def begin_physical_send(
        self,
        intent_id: str,
        packet: ContinuationPacket | Mapping[str, Any] | bytes,
        authority: ContinuationAuthoritySnapshot | Mapping[str, Any],
        owner_token: str,
        dom: DomPreconditions,
        *,
        now: datetime | str | None = None,
    ) -> SendIntentResult:
        """Persist PHYSICAL_SEND_ATTEMPTED immediately before adapter invocation."""

        current = self._now(now)
        try:
            typed = _canonical_packet(packet)
        except SendIntentError as exc:
            return self._invalid(exc.code, str(exc))
        validation = validate_packet(typed, authority, now=current)
        if not validation.valid:
            fenced = self._fence_intent(intent_id, validation.code, current)
            return self._invalid(validation.code, validation.message, fenced)
        readable = self.read(intent_id)
        dom_ok, dom_code, dom_message = (self._dom_check(readable, dom) if readable is not None else (False, "INTENT_NOT_FOUND", "send intent does not exist"))
        if not dom_ok:
            return self._invalid(dom_code, dom_message, readable)
        now_iso = _iso(current, field="now")
        try:
            with self._transaction() as conn:
                self._ensure_project(conn)
                record = self._select(conn, intent_id)
                if record is None:
                    return self._invalid("INTENT_NOT_FOUND", "send intent does not exist")
                if not self._packet_matches(record, typed):
                    return self._invalid("STALE_AUTHORITY", "packet does not match durable send intent", record)
                if record.lease_owner_token_hash != _token_hash(owner_token):
                    return self._invalid("STALE_INTENT_OWNER", "send intent owner token is not current", record)
                if record.status in {SEND_STATUS_PHYSICAL_ATTEMPTED, SEND_STATUS_UNCERTAIN, SEND_STATUS_RECONCILIATION_REQUIRED, SEND_STATUS_CONFIRMED, SEND_STATUS_ACKNOWLEDGED}:
                    return self._invalid("PHYSICAL_SEND_ALREADY_ATTEMPTED", "physical send must not be repeated without a no-delivery proof", record)
                if record.status != SEND_STATUS_ALLOWED:
                    return self._invalid("INTENT_NOT_ALLOWED", f"intent state '{record.status}' is not SEND_ALLOWED", record)
                allowed, reason_code, message, _ = self._live_lease_check(
                    conn, typed, owner_token, current, boundary=EffectBoundary.DISPATCH_SEND
                )
                if not allowed:
                    fenced = self._fence_on_conn(conn, record, reason_code, now_iso)
                    return self._invalid(reason_code, message, fenced or record)
                next_revision = record.state_revision + 1
                updated = conn.execute(
                    """
                    UPDATE send_intents
                    SET status = 'PHYSICAL_SEND_ATTEMPTED', state_revision = ?,
                        updated_at = ?, physical_attempted_at = ?
                    WHERE project_id = ? AND intent_id = ? AND state_revision = ?
                      AND status = 'SEND_ALLOWED'
                    """,
                    (next_revision, now_iso, now_iso, self.project_id, intent_id, record.state_revision),
                )
                if updated.rowcount != 1:
                    return self._invalid("PHYSICAL_ATTEMPT_CAS_LOST", "physical attempt CAS lost", record)
                self._bump_project_revision(conn, now_iso)
                attempted = self._select(conn, intent_id)
                if attempted is None:
                    raise SendIntentError("INTENT_READBACK_FAILED", "physical-attempt intent was not readable")
                self._append_event(conn, attempted, "PHYSICAL_SEND_ATTEMPTED", "PHYSICAL_SEND_ATTEMPTED", now_iso)
                return SendIntentResult(True, "PHYSICAL_SEND_ATTEMPTED", "physical send boundary was durably entered", attempted, mutated=True)
        except (sqlite3.OperationalError, sqlite3.IntegrityError) as exc:
            return self._invalid("PHYSICAL_ATTEMPT_TRANSACTION_FAILED", f"physical attempt failed closed: {exc}")

    mark_physical_send_attempted = begin_physical_send

    def mark_uncertain(
        self,
        intent_id: str,
        owner_token: str,
        *,
        reason: str,
        now: datetime | str | None = None,
    ) -> SendIntentResult:
        current = self._now(now)
        now_iso = _iso(current, field="now")
        token_hash = _token_hash(owner_token)
        if not token_hash:
            return self._invalid("OWNER_TOKEN_INVALID", "uncertain transition requires the opaque owner token")
        try:
            with self._transaction() as conn:
                self._ensure_project(conn)
                record = self._select(conn, intent_id)
                if record is None:
                    return self._invalid("INTENT_NOT_FOUND", "send intent does not exist")
                if record.lease_owner_token_hash != token_hash:
                    return self._invalid("STALE_INTENT_OWNER", "uncertain transition owner token is not current", record)
                if record.status in {SEND_STATUS_UNCERTAIN, SEND_STATUS_RECONCILIATION_REQUIRED}:
                    return SendIntentResult(True, "UNCERTAIN", "delivery remains uncertain", record, mutated=False, idempotent=True)
                if record.status != SEND_STATUS_PHYSICAL_ATTEMPTED:
                    return self._invalid("NOT_PHYSICAL_ATTEMPTED", "only a physical-attempted intent can become uncertain", record)
                next_revision = record.state_revision + 1
                bounded = " ".join(str(reason).split())[:256]
                updated = conn.execute(
                    """
                    UPDATE send_intents
                    SET status = 'UNCERTAIN', state_revision = ?, updated_at = ?,
                        uncertainty_reason = ?
                    WHERE project_id = ? AND intent_id = ? AND state_revision = ?
                      AND status = 'PHYSICAL_SEND_ATTEMPTED'
                    """,
                    (next_revision, now_iso, bounded, self.project_id, intent_id, record.state_revision),
                )
                if updated.rowcount != 1:
                    return self._invalid("UNCERTAINTY_CAS_LOST", "uncertainty transition CAS lost", record)
                self._bump_project_revision(conn, now_iso)
                uncertain = self._select(conn, intent_id)
                if uncertain is None:
                    raise SendIntentError("INTENT_READBACK_FAILED", "uncertain intent was not readable")
                self._append_event(conn, uncertain, "SEND_DELIVERY_UNCERTAIN", bounded, now_iso)
                return SendIntentResult(False, "UNCERTAIN_DELIVERY", "delivery is uncertain; blind resend is forbidden", uncertain, mutated=True)
        except (sqlite3.OperationalError, sqlite3.IntegrityError) as exc:
            return self._invalid("UNCERTAINTY_TRANSACTION_FAILED", f"uncertainty transition failed closed: {exc}")

    def confirm(
        self,
        intent_id: str,
        packet: ContinuationPacket | Mapping[str, Any] | bytes,
        owner_token: str,
        evidence: StructuredSendEvidence,
        *,
        now: datetime | str | None = None,
        trace: MutableSequence[str] | None = None,
    ) -> SendIntentResult:
        """Accept only structured visible-message evidence, never focus/timeout."""

        try:
            typed = _canonical_packet(packet)
        except SendIntentError as exc:
            return self._invalid(exc.code, str(exc))
        current = self._now(now)
        token_hash = _token_hash(owner_token)
        if not token_hash:
            return self._invalid("OWNER_TOKEN_INVALID", "confirmation requires the opaque owner token")
        try:
            with self._transaction() as conn:
                self._ensure_project(conn)
                record = self._select(conn, intent_id)
                if record is None:
                    return self._invalid("INTENT_NOT_FOUND", "send intent does not exist")
                if not self._packet_matches(record, typed):
                    return self._invalid("STALE_AUTHORITY", "packet does not match durable send intent", record)
                if record.lease_owner_token_hash != token_hash:
                    return self._invalid("STALE_INTENT_OWNER", "confirmation owner token is not current", record)
                if not evidence.structured or evidence.focus_only or evidence.composer_empty_only or evidence.evidence_type != "STRUCTURED_DOM_MESSAGE":
                    return self._invalid("NON_POSITIVE_SEND_EVIDENCE", "focus, composer-empty, timeout, or generic UI is not send confirmation", record)
                expected = (
                    evidence.intent_id == record.intent_id
                    and evidence.continuation_id == record.continuation_id
                    and evidence.packet_digest == record.packet_digest
                    and evidence.execution_binding_id == record.execution_binding_id
                    and evidence.conversation_id == record.conversation_binding_id
                    and evidence.message_digest == record.message_digest
                    and bool(evidence.visible_message_id)
                )
                if not expected:
                    return self._invalid("SEND_EVIDENCE_MISMATCH", "structured send evidence does not match intent identity", record)
                if record.status == SEND_STATUS_CONFIRMED or record.status == SEND_STATUS_ACKNOWLEDGED:
                    if dict(record.delivery_evidence) == evidence.as_dict():
                        return SendIntentResult(True, "SEND_CONFIRMED", "send confirmation replay is idempotent", record, evidence, idempotent=True)
                    return self._invalid("CONFIRMATION_CONFLICT", "a different confirmation cannot replace durable evidence", record)
                if record.status not in {SEND_STATUS_PHYSICAL_ATTEMPTED, SEND_STATUS_UNCERTAIN, SEND_STATUS_RECONCILIATION_REQUIRED}:
                    return self._invalid("NOT_CONFIRMABLE", f"intent state '{record.status}' cannot be confirmed", record)
                now_iso = _iso(current, field="now")
                next_revision = record.state_revision + 1
                updated = conn.execute(
                    """
                    UPDATE send_intents
                    SET status = 'SEND_CONFIRMED', state_revision = ?,
                        updated_at = ?, confirmed_at = ?,
                        delivery_evidence_json = ?, uncertainty_reason = NULL
                    WHERE project_id = ? AND intent_id = ? AND state_revision = ?
                    """,
                    (
                        next_revision,
                        now_iso,
                        now_iso,
                        json.dumps(evidence.as_dict(), sort_keys=True, separators=(",", ":")),
                        self.project_id,
                        intent_id,
                        record.state_revision,
                    ),
                )
                if updated.rowcount != 1:
                    return self._invalid("CONFIRM_CAS_LOST", "send confirmation CAS lost", record)
                self._bump_project_revision(conn, now_iso)
                confirmed = self._select(conn, intent_id)
                if confirmed is None:
                    raise SendIntentError("INTENT_READBACK_FAILED", "confirmed intent was not readable")
                self._append_event(conn, confirmed, "SEND_CONFIRMED", "SEND_CONFIRMED", now_iso)
                if trace is not None:
                    trace.append("SEND_CONFIRMED")
                return SendIntentResult(True, "SEND_CONFIRMED", "structured send evidence confirmed delivery", confirmed, evidence, mutated=True)
        except (sqlite3.OperationalError, sqlite3.IntegrityError) as exc:
            return self._invalid("CONFIRM_TRANSACTION_FAILED", f"send confirmation failed closed: {exc}")

    confirm_send = confirm

    def reconcile(
        self,
        intent_id: str,
        packet: ContinuationPacket | Mapping[str, Any] | bytes,
        owner_token: str,
        *,
        evidence: StructuredSendEvidence | None = None,
        no_delivery_proof: NoDeliveryProof | None = None,
        now: datetime | str | None = None,
        trace: MutableSequence[str] | None = None,
    ) -> SendIntentResult:
        """Resolve crash uncertainty; retry is possible only with exact no-delivery proof."""

        try:
            typed = _canonical_packet(packet)
        except SendIntentError as exc:
            return self._invalid(exc.code, str(exc))
        record = self.read(intent_id)
        if record is None:
            return self._invalid("INTENT_NOT_FOUND", "send intent does not exist")
        if not self._packet_matches(record, typed):
            return self._invalid("STALE_AUTHORITY", "packet does not match durable send intent", record)
        if record.lease_owner_token_hash != _token_hash(owner_token):
            return self._invalid("STALE_INTENT_OWNER", "reconciliation owner token is not current", record)
        if evidence is not None:
            return self.confirm(intent_id, packet, owner_token, evidence, now=now, trace=trace)
        if no_delivery_proof is None or no_delivery_proof.intent_id != intent_id or not no_delivery_proof.valid:
            current = self._now(now)
            now_iso = _iso(current, field="now")
            try:
                with self._transaction() as conn:
                    self._ensure_project(conn)
                    current_record = self._select(conn, intent_id)
                    if current_record is None:
                        return self._invalid("INTENT_NOT_FOUND", "send intent does not exist")
                    if current_record.status in {SEND_STATUS_UNCERTAIN, SEND_STATUS_PHYSICAL_ATTEMPTED}:
                        next_revision = current_record.state_revision + 1
                        conn.execute(
                            """
                            UPDATE send_intents
                            SET status = 'RECONCILIATION_REQUIRED', state_revision = ?,
                                updated_at = ?, uncertainty_reason = 'NO_DETERMINISTIC_DELIVERY_PROOF'
                            WHERE project_id = ? AND intent_id = ? AND state_revision = ?
                            """,
                            (next_revision, now_iso, self.project_id, intent_id, current_record.state_revision),
                        )
                        self._bump_project_revision(conn, now_iso)
                        reconciled = self._select(conn, intent_id)
                        if reconciled is not None:
                            self._append_event(conn, reconciled, "SEND_RECONCILIATION_REQUIRED", "NO_DETERMINISTIC_DELIVERY_PROOF", now_iso)
                        return self._invalid("UNCERTAIN_DELIVERY", "delivery remains uncertain; no resend was attempted", reconciled)
            except (sqlite3.OperationalError, sqlite3.IntegrityError) as exc:
                return self._invalid("RECONCILE_TRANSACTION_FAILED", f"reconciliation failed closed: {exc}")
            return self._invalid("NO_DELIVERY_PROOF_REQUIRED", "safe retry requires deterministic no-delivery proof", record)

        current = self._now(now)
        now_iso = _iso(current, field="now")
        try:
            with self._transaction() as conn:
                self._ensure_project(conn)
                current_record = self._select(conn, intent_id)
                if current_record is None:
                    return self._invalid("INTENT_NOT_FOUND", "send intent does not exist")
                if current_record.status in {SEND_STATUS_CONFIRMED, SEND_STATUS_ACKNOWLEDGED}:
                    return SendIntentResult(True, "ALREADY_CONFIRMED", "confirmed delivery cannot be retried", current_record, idempotent=True)
                if current_record.status not in {SEND_STATUS_UNCERTAIN, SEND_STATUS_RECONCILIATION_REQUIRED, SEND_STATUS_PHYSICAL_ATTEMPTED}:
                    return self._invalid("NOT_RECONCILABLE", f"intent state '{current_record.status}' is not awaiting reconciliation", current_record)
                next_revision = current_record.state_revision + 1
                updated = conn.execute(
                    """
                    UPDATE send_intents
                    SET status = 'SEND_ALLOWED', state_revision = ?, updated_at = ?,
                        uncertainty_reason = 'NO_DELIVERY_PROVEN_RETRY_ALLOWED'
                    WHERE project_id = ? AND intent_id = ? AND state_revision = ?
                    """,
                    (next_revision, now_iso, self.project_id, intent_id, current_record.state_revision),
                )
                if updated.rowcount != 1:
                    return self._invalid("RECONCILE_CAS_LOST", "no-delivery reconciliation CAS lost", current_record)
                self._bump_project_revision(conn, now_iso)
                retryable = self._select(conn, intent_id)
                if retryable is None:
                    raise SendIntentError("INTENT_READBACK_FAILED", "reconciled intent was not readable")
                self._append_event(conn, retryable, "SEND_RECONCILED_NO_DELIVERY", "NO_DELIVERY_PROVEN_RETRY_ALLOWED", now_iso)
                return SendIntentResult(True, "RETRY_ALLOWED", "retry is allowed by deterministic no-delivery proof", retryable, mutated=True)
        except (sqlite3.OperationalError, sqlite3.IntegrityError) as exc:
            return self._invalid("RECONCILE_TRANSACTION_FAILED", f"reconciliation failed closed: {exc}")

    reconcile_delivery = reconcile

    def send_once(
        self,
        intent_id: str,
        packet: ContinuationPacket | Mapping[str, Any] | bytes,
        authority: ContinuationAuthoritySnapshot | Mapping[str, Any],
        owner_token: str,
        browser: BrowserSendAdapter,
        *,
        now: datetime | str | None = None,
    ) -> SendIntentResult:
        """Run one physical send attempt; adapter exceptions fail closed."""

        trace = getattr(browser, "trace", None)
        dom = browser.snapshot()
        allowed = self.allow_send(intent_id, packet, authority, owner_token, dom, now=now, trace=trace)
        if not allowed.accepted:
            return allowed
        attempted = self.begin_physical_send(intent_id, packet, authority, owner_token, dom, now=now)
        if not attempted.accepted:
            return attempted
        intent = attempted.intent
        if intent is None:
            return self._invalid("INTENT_READBACK_FAILED", "physical attempt did not return an intent")
        try:
            evidence = browser.physical_send(intent)
        except SendDeliveryUncertain as exc:
            return self.mark_uncertain(intent_id, owner_token, reason=str(exc), now=now)
        except Exception as exc:
            return self.mark_uncertain(intent_id, owner_token, reason=f"adapter failure: {exc}", now=now)
        return self.confirm(intent_id, packet, owner_token, evidence, now=now, trace=trace)

    send = send_once

    def ack(
        self,
        intent_id: str,
        packet: ContinuationPacket | Mapping[str, Any] | bytes,
        owner_token: str,
        acknowledgement: SendAck,
        *,
        now: datetime | str | None = None,
        trace: MutableSequence[str] | None = None,
    ) -> SendIntentResult:
        """Persist ACK only for the exact intent/continuation/binding identity."""

        try:
            typed = _canonical_packet(packet)
        except SendIntentError as exc:
            return self._invalid(exc.code, str(exc))
        record = self.read(intent_id)
        if record is None:
            return self._invalid("INTENT_NOT_FOUND", "send intent does not exist")
        expected_ack = (
            acknowledgement.intent_id == record.intent_id == intent_id
            and acknowledgement.continuation_id == record.continuation_id == typed.continuation_id
            and acknowledgement.packet_digest == record.packet_digest == typed["packet_digest"]
            and acknowledgement.execution_binding_id == record.execution_binding_id == typed["execution_binding_id"]
            and acknowledgement.conversation_id == record.conversation_binding_id
        )
        if not expected_ack:
            return self._invalid("WRONG_ACK", "acknowledgement identity does not match the durable intent", record)
        token_hash = _token_hash(owner_token)
        if record.lease_owner_token_hash != token_hash:
            return self._invalid("STALE_INTENT_OWNER", "acknowledgement owner token is not current", record)
        if record.status == SEND_STATUS_ACKNOWLEDGED:
            return SendIntentResult(True, "ALREADY_ACKNOWLEDGED", "acknowledgement replay is idempotent", record, idempotent=True)
        if record.status != SEND_STATUS_CONFIRMED:
            return self._invalid("NOT_CONFIRMED", "only SEND_CONFIRMED intent can be acknowledged", record)

        # Completion is a durable NX-026 state transition.  It is safe after a
        # STOP fence because it records/reconciles an effect that already has
        # positive evidence; it never authorizes a new physical send.
        lease_completed = self.lease.complete(typed, owner_token, now=now)
        if not lease_completed.accepted:
            return self._invalid("LEASE_COMPLETION_FAILED", lease_completed.message, record)
        current = self._now(now)
        now_iso = _iso(current, field="now")
        try:
            with self._transaction() as conn:
                self._ensure_project(conn)
                current_record = self._select(conn, intent_id)
                if current_record is None:
                    return self._invalid("INTENT_NOT_FOUND", "send intent does not exist")
                if current_record.status == SEND_STATUS_ACKNOWLEDGED:
                    return SendIntentResult(True, "ALREADY_ACKNOWLEDGED", "acknowledgement replay is idempotent", current_record, idempotent=True)
                if current_record.status != SEND_STATUS_CONFIRMED:
                    return self._invalid("NOT_CONFIRMED", "intent changed before acknowledgement", current_record)
                next_revision = current_record.state_revision + 1
                updated = conn.execute(
                    """
                    UPDATE send_intents
                    SET status = 'ACKNOWLEDGED', state_revision = ?,
                        updated_at = ?, acknowledged_at = ?
                    WHERE project_id = ? AND intent_id = ? AND state_revision = ?
                      AND status = 'SEND_CONFIRMED'
                    """,
                    (next_revision, now_iso, now_iso, self.project_id, intent_id, current_record.state_revision),
                )
                if updated.rowcount != 1:
                    return self._invalid("ACK_CAS_LOST", "acknowledgement CAS lost", current_record)
                self._bump_project_revision(conn, now_iso)
                acknowledged = self._select(conn, intent_id)
                if acknowledged is None:
                    raise SendIntentError("INTENT_READBACK_FAILED", "acknowledged intent was not readable")
                self._append_event(conn, acknowledged, "SEND_ACKNOWLEDGED", "ACKNOWLEDGED", now_iso)
                if trace is not None:
                    trace.append("ACK")
                return SendIntentResult(True, "ACKNOWLEDGED", "send intent acknowledged", acknowledged, mutated=True)
        except (sqlite3.OperationalError, sqlite3.IntegrityError) as exc:
            return self._invalid("ACK_TRANSACTION_FAILED", f"acknowledgement failed closed: {exc}")

    acknowledge = ack

    def cancel(
        self,
        intent_id: str,
        owner_token: str,
        *,
        reason: str = "CANCELLED",
        now: datetime | str | None = None,
    ) -> SendIntentResult:
        current = self._now(now)
        token_hash = _token_hash(owner_token)
        if not token_hash:
            return self._invalid("OWNER_TOKEN_INVALID", "cancel requires the opaque owner token")
        now_iso = _iso(current, field="now")
        try:
            with self._transaction() as conn:
                self._ensure_project(conn)
                record = self._select(conn, intent_id)
                if record is None:
                    return self._invalid("INTENT_NOT_FOUND", "send intent does not exist")
                if record.lease_owner_token_hash != token_hash:
                    return self._invalid("STALE_INTENT_OWNER", "cancel owner token is not current", record)
                if record.status == SEND_STATUS_CANCELLED:
                    return SendIntentResult(True, "ALREADY_CANCELLED", "cancel replay is idempotent", record, idempotent=True)
                if record.status not in {SEND_STATUS_PREPARED, SEND_STATUS_ALLOWED, SEND_STATUS_UNCERTAIN, SEND_STATUS_RECONCILIATION_REQUIRED}:
                    return self._invalid("NOT_CANCELLABLE", f"intent state '{record.status}' cannot be cancelled", record)
                next_revision = record.state_revision + 1
                bounded = " ".join(str(reason).split())[:256]
                conn.execute(
                    """
                    UPDATE send_intents
                    SET status = 'CANCELLED', state_revision = ?, updated_at = ?,
                        uncertainty_reason = ?
                    WHERE project_id = ? AND intent_id = ? AND state_revision = ?
                    """,
                    (next_revision, now_iso, bounded, self.project_id, intent_id, record.state_revision),
                )
                self._bump_project_revision(conn, now_iso)
                cancelled = self._select(conn, intent_id)
                if cancelled is None:
                    raise SendIntentError("INTENT_READBACK_FAILED", "cancelled intent was not readable")
                self._append_event(conn, cancelled, "SEND_INTENT_CANCELLED", bounded, now_iso)
                return SendIntentResult(True, "CANCELLED", "send intent cancelled", cancelled, mutated=True)
        except (sqlite3.OperationalError, sqlite3.IntegrityError) as exc:
            return self._invalid("CANCEL_TRANSACTION_FAILED", f"cancel failed closed: {exc}")

    def continue_same_chat(
        self,
        packet: ContinuationPacket | Mapping[str, Any] | bytes,
        authority: ContinuationAuthoritySnapshot | Mapping[str, Any],
        lease_claim: Any,
        binding: ExactConversationBinding,
        browser: BrowserSendAdapter,
        payload: str,
        *,
        now: datetime | str | None = None,
    ) -> SameChatFlowResult:
        """Task -> task continuation within one exact existing conversation."""

        trace = getattr(browser, "trace", [])
        token = getattr(lease_claim, "owner_token", None)
        if not getattr(lease_claim, "claimed", False) or not isinstance(token, str):
            return SameChatFlowResult(False, tuple(trace), 0, getattr(browser, "visible_send_count", 0), getattr(browser, "created_conversations", 0), None, None, None)
        prepared = self.prepare(packet, authority, token, payload, conversation_binding=binding, now=now, trace=trace)
        if not prepared.accepted or prepared.intent is None:
            return SameChatFlowResult(False, tuple(trace), 0, getattr(browser, "visible_send_count", 0), getattr(browser, "created_conversations", 0), prepared.intent, None, None)
        sent = self.send_once(prepared.intent.intent_id, packet, authority, token, browser, now=now)
        if not sent.accepted or sent.intent is None:
            return SameChatFlowResult(False, tuple(trace), 0, getattr(browser, "visible_send_count", 0), getattr(browser, "created_conversations", 0), sent.intent, sent, None)
        ack = self.ack(
            prepared.intent.intent_id,
            packet,
            token,
            SendAck(
                intent_id=prepared.intent.intent_id,
                continuation_id=prepared.intent.continuation_id,
                packet_digest=prepared.intent.packet_digest,
                execution_binding_id=prepared.intent.execution_binding_id,
                conversation_id=prepared.intent.conversation_binding_id or "",
            ),
            now=now,
            trace=trace,
        )
        return SameChatFlowResult(
            bool(ack.accepted),
            tuple(trace),
            0,
            getattr(browser, "visible_send_count", 0),
            getattr(browser, "created_conversations", 0),
            ack.intent,
            sent,
            ack,
        )


__all__ = [
    "BROWSER_LOCAL_STATE_IS_SEND_AUTHORITY",
    "BrowserSendAdapter",
    "DomPreconditions",
    "ExactConversationBinding",
    "FakeBrowser",
    "NoDeliveryProof",
    "SEND_INTENT_UNDER_CANONICAL_AUTHORITY",
    "SEND_INTENT_VERSION",
    "SEND_INTENT_VERSION_EXPLICIT",
    "SameChatFlowResult",
    "SendAck",
    "SendDeliveryUncertain",
    "SendIntentCoordinator",
    "SendIntentError",
    "SendIntentRecord",
    "SendIntentResult",
    "StructuredSendEvidence",
]

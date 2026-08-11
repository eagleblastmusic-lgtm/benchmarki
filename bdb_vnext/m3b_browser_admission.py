"""M3b restart-safe Browser/Native admission mechanics (shadow only).

The classes here are a bounded Browser MV3/Native Host test harness.  They
persist client recovery state before transport, bind every message to the
vNext protocol generation and delegate canonical acceptance/lookup to the
M3a shadow store.  The Browser outbox never allocates a Task and the Native
bridge never owns a second database writer.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NoReturn

from bdb_shared.evidence import canonical_json_bytes, semantic_digest
from bdb_vnext.m3a_submission import AdmissionReceipt, M3aError, ShadowSubmissionRequest, ShadowSubmissionStore


M3B_SCHEMA = "bdb-vnext-m3b-browser-admission-v1"
M3B_OUTBOX_SCHEMA = "bdb-vnext-m3b-browser-outbox-v1"
M3B_PROTOCOL_GENERATION = "bdb-vnext-protocol-v1"
M3B_BROWSER_COMPONENT_ID = "devmaster.bdb.vnext.browser-transport"
M3B_NATIVE_HOST_ID = "com.bartosz.dev_bridge.vnext"
M3B_OUTBOX_WRITER_ID = "m3b-shadow-browser-outbox"
M3B_BUSY_TIMEOUT_MS = 250
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}$")

OutboxState = Literal["PENDING", "SENT", "ACKED"]


class M3bError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def _fail(code: str, message: str, *, details: Mapping[str, Any] | None = None) -> NoReturn:
    raise M3bError(code, message, details=details)


def _digest(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        _fail("invalid_digest", f"{field} must be a lowercase sha256 digest")
    return value


def _key(value: object, *, field: str = "submission_key") -> str:
    if not isinstance(value, str) or _KEY.fullmatch(value) is None:
        _fail("invalid_submission_key", f"{field} is not a valid opaque submission key")
    return value


@dataclass(frozen=True)
class AdmissionEnvelope:
    request: ShadowSubmissionRequest
    protocol_generation: str = M3B_PROTOCOL_GENERATION

    def __post_init__(self) -> None:
        if not isinstance(self.request, ShadowSubmissionRequest):
            _fail("invalid_envelope", "admission envelope requires an M3a request")
        if self.protocol_generation != M3B_PROTOCOL_GENERATION:
            _fail("unsupported_protocol", "envelope protocol generation is not supported")

    @property
    def submission_key(self) -> str:
        return self.request.submission_key

    @property
    def request_digest(self) -> str:
        return self.request.validated_digest()

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": M3B_SCHEMA,
            "protocol_generation": self.protocol_generation,
            "submission_key": self.submission_key,
            "request_digest": self.request_digest,
            "request": self.request.as_dict(),
        }


@dataclass(frozen=True)
class ProtocolCapability:
    protocol_generation: str
    browser_component_id: str
    native_host_id: str
    canonical_lookup: bool
    production_acceptance: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": M3B_SCHEMA,
            "protocol_generation": self.protocol_generation,
            "browser_component_id": self.browser_component_id,
            "native_host_id": self.native_host_id,
            "canonical_lookup": self.canonical_lookup,
            "production_acceptance": self.production_acceptance,
        }


@dataclass(frozen=True)
class OutboxEntry:
    submission_key: str
    request_digest: str
    protocol_generation: str
    request_json: bytes
    state: OutboxState
    receipt_json: bytes | None

    def request(self) -> ShadowSubmissionRequest:
        try:
            value = json.loads(self.request_json.decode("utf-8"))
            return ShadowSubmissionRequest(
                submission_key=value["submission_key"],
                intent_revision=value["intent_revision"],
                intent=value["intent"],
                conversation_binding=value["conversation_binding"],
                consumer_binding=value["consumer_binding"],
                canonicalization_version=value["canonicalization_version"],
                task_id=value.get("task_id"),
                expected_intent_revision_id=value.get("expected_intent_revision_id"),
                request_digest=value.get("request_digest"),
            )
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            _fail("outbox_corrupt", "durable outbox request is not a valid M3a request")

    def receipt(self) -> AdmissionReceipt | None:
        if self.receipt_json is None:
            return None
        try:
            value = json.loads(self.receipt_json.decode("utf-8"))
            return AdmissionReceipt(
                submission_key=value["submission_key"],
                request_digest=value["request_digest"],
                status=value["status"],
                disposition=value["disposition"],
                task_id=value.get("task_id"),
                intent_revision_id=value.get("intent_revision_id"),
                outcome=value["outcome"],
                tombstone_reason=value.get("tombstone_reason"),
            )
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError, M3aError) as exc:
            _fail("outbox_corrupt", "durable outbox receipt is not a valid canonical receipt")


class BrowserAdmissionOutbox:
    """Durable client recovery state; never an admission authority."""

    def __init__(
        self,
        root: str | Path,
        *,
        shadow: bool = False,
        existing: bool = False,
        legacy_root: str | Path | None = None,
        max_entries: int = 128,
        busy_timeout_ms: int = M3B_BUSY_TIMEOUT_MS,
    ) -> None:
        if shadow is not True:
            _fail("shadow_mode_required", "M3b outbox requires an explicit shadow=True assertion")
        if not isinstance(max_entries, int) or max_entries < 1:
            _fail("invalid_outbox_quota", "outbox quota must be positive")
        self.root = Path(os.path.abspath(Path(root).expanduser()))
        if legacy_root is not None and _overlaps(self.root, Path(os.path.abspath(Path(legacy_root).expanduser()))):
            _fail("foreign_state_overlap", "M3b outbox root overlaps the frozen legacy root")
        self.state_root = self.root / "browser" / "outbox"
        self.database_path = self.state_root / "outbox.db"
        self.anchor_path = self.state_root / "anchor.json"
        self.config_path = self.state_root / "config.json"
        self.max_entries = max_entries
        self._busy_timeout_ms = busy_timeout_ms
        if existing:
            if not self.anchor_path.is_file() or not self.database_path.is_file() or not self.config_path.is_file():
                _fail("anchor_lost", "existing Browser outbox anchor or database is missing")
        else:
            if self.anchor_path.exists() or self.database_path.exists() or self.config_path.exists():
                _fail("existing_open_required", "an existing outbox must be reopened through open_existing")
            self.state_root.mkdir(parents=True, exist_ok=True)
            self._write_identity_files()
        try:
            self._connection = sqlite3.connect(
                str(self.database_path),
                timeout=busy_timeout_ms / 1000,
                check_same_thread=False,
                isolation_level=None,
            )
            self._lock = threading.RLock()
            self._configure()
            self._validate_identity_files()
            self._ensure_schema()
            self._validate_rows()
        except M3bError:
            raise
        except (OSError, sqlite3.DatabaseError) as exc:
            _fail("outbox_corrupt", "durable Browser outbox cannot be opened")

    @classmethod
    def open_existing(cls, root: str | Path, **kwargs: Any) -> "BrowserAdmissionOutbox":
        kwargs["existing"] = True
        return cls(root, **kwargs)

    def _write_identity_files(self) -> None:
        config = {
            "schema": M3B_OUTBOX_SCHEMA,
            "mode": "SHADOW_ONLY",
            "writer_id": M3B_OUTBOX_WRITER_ID,
            "protocol_generation": M3B_PROTOCOL_GENERATION,
            "production_acceptance": False,
        }
        anchor = {
            "schema": M3B_OUTBOX_SCHEMA,
            "anchor_id": semantic_digest({"schema": M3B_OUTBOX_SCHEMA, "root": str(self.root)}),
            "protocol_generation": M3B_PROTOCOL_GENERATION,
        }
        self.config_path.write_bytes(canonical_json_bytes(config))
        self.anchor_path.write_bytes(canonical_json_bytes(anchor))

    def _configure(self) -> None:
        try:
            self._connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
            self._connection.execute("PRAGMA foreign_keys=ON")
            mode = str(self._connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
            self._connection.execute("PRAGMA synchronous=FULL")
        except sqlite3.DatabaseError as exc:
            _fail("outbox_corrupt", "outbox SQLite settings could not be verified")
        if mode != "wal":
            _fail("outbox_corrupt", "outbox must use WAL journaling")

    def _validate_identity_files(self) -> None:
        expected_config = {
            "schema": M3B_OUTBOX_SCHEMA,
            "mode": "SHADOW_ONLY",
            "writer_id": M3B_OUTBOX_WRITER_ID,
            "protocol_generation": M3B_PROTOCOL_GENERATION,
            "production_acceptance": False,
        }
        expected_anchor = {
            "schema": M3B_OUTBOX_SCHEMA,
            "anchor_id": semantic_digest({"schema": M3B_OUTBOX_SCHEMA, "root": str(self.root)}),
            "protocol_generation": M3B_PROTOCOL_GENERATION,
        }
        try:
            config = json.loads(self.config_path.read_bytes().decode("utf-8"))
            anchor = json.loads(self.anchor_path.read_bytes().decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            _fail("anchor_corrupt", "outbox identity files are not valid canonical JSON")
        if config != expected_config:
            _fail("outbox_config_mismatch", "outbox config is not the frozen vNext identity")
        if anchor != expected_anchor:
            _fail("anchor_corrupt", "outbox anchor identity differs")

    def _ensure_schema(self) -> None:
        try:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS m3b_outbox (
                    submission_key TEXT PRIMARY KEY,
                    request_digest TEXT NOT NULL,
                    protocol_generation TEXT NOT NULL,
                    request_json BLOB NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('PENDING','SENT','ACKED')),
                    receipt_json BLOB,
                    created_order INTEGER NOT NULL
                );
                """
            )
        except sqlite3.DatabaseError as exc:
            _fail("outbox_corrupt", "outbox schema could not be initialized")

    def _validate_rows(self) -> None:
        with self._lock:
            try:
                rows = self._connection.execute(
                    "SELECT submission_key,request_digest,protocol_generation,request_json,state,receipt_json FROM m3b_outbox"
                ).fetchall()
                for row in rows:
                    entry = self._entry(row)
                    if entry.request().validated_digest() != entry.request_digest:
                        _fail("outbox_corrupt", "outbox request digest does not match canonical request")
                    if entry.state == "ACKED" and entry.receipt() is None:
                        _fail("outbox_corrupt", "ACKED outbox entry has no canonical ACK")
            except M3bError:
                raise
            except (sqlite3.DatabaseError, UnicodeError, ValueError, TypeError) as exc:
                _fail("outbox_corrupt", "outbox row validation failed")

    def _entry(self, row: tuple[Any, ...]) -> OutboxEntry:
        key = _key(row[0])
        digest = _digest(row[1], field="outbox.request_digest")
        generation = row[2]
        if generation != M3B_PROTOCOL_GENERATION:
            _fail("unsupported_protocol", "outbox entry uses an unsupported protocol generation")
        state = row[4]
        if state not in {"PENDING", "SENT", "ACKED"}:
            _fail("outbox_corrupt", "outbox state is invalid")
        request_json = bytes(row[3]) if isinstance(row[3], (bytes, bytearray, memoryview)) else str(row[3]).encode("utf-8")
        receipt_json = None if row[5] is None else (bytes(row[5]) if isinstance(row[5], (bytes, bytearray, memoryview)) else str(row[5]).encode("utf-8"))
        return OutboxEntry(key, digest, generation, request_json, state, receipt_json)  # type: ignore[arg-type]

    def prepare(self, envelope: AdmissionEnvelope) -> OutboxEntry:
        if not isinstance(envelope, AdmissionEnvelope):
            _fail("invalid_envelope", "outbox prepare requires an admission envelope")
        request_json = canonical_json_bytes(envelope.request.as_dict())
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                row = self._connection.execute(
                    "SELECT submission_key,request_digest,protocol_generation,request_json,state,receipt_json FROM m3b_outbox WHERE submission_key = ?",
                    (envelope.submission_key,),
                ).fetchone()
                if row is not None:
                    existing = self._entry(row)
                    if existing.request_digest != envelope.request_digest:
                        _fail("submission_conflict", "outbox key is bound to a different canonical digest")
                    self._connection.commit()
                    return existing
                count = int(self._connection.execute("SELECT COUNT(*) FROM m3b_outbox").fetchone()[0])
                if count >= self.max_entries:
                    _fail("outbox_quota", "Browser outbox quota is full")
                self._connection.execute(
                    "INSERT INTO m3b_outbox(submission_key,request_digest,protocol_generation,request_json,state,receipt_json,created_order) VALUES (?,?,?,?,?,?,?)",
                    (envelope.submission_key, envelope.request_digest, envelope.protocol_generation, request_json, "PENDING", None, count + 1),
                )
                self._connection.commit()
                return OutboxEntry(envelope.submission_key, envelope.request_digest, envelope.protocol_generation, request_json, "PENDING", None)
            except M3bError:
                if self._connection.in_transaction:
                    self._connection.rollback()
                raise
            except sqlite3.OperationalError as exc:
                if self._connection.in_transaction:
                    self._connection.rollback()
                if "busy" in str(exc).lower() or "locked" in str(exc).lower():
                    _fail("outbox_busy", "Browser outbox is busy")
                _fail("outbox_write_failed", "Browser outbox could not be durably prepared")

    def mark_sent(self, submission_key: str) -> OutboxEntry:
        return self._transition(submission_key, from_states={"PENDING", "SENT"}, state="SENT")

    def mark_acked(self, submission_key: str, receipt: AdmissionReceipt) -> OutboxEntry:
        if receipt.submission_key != submission_key:
            _fail("ack_identity_mismatch", "canonical ACK key differs from outbox key")
        with self._lock:
            row = self._row(submission_key)
            if row is None:
                _fail("outbox_missing", "cannot acknowledge a missing outbox entry")
            entry = self._entry(row)
            if entry.request_digest != receipt.request_digest:
                _fail("ack_digest_mismatch", "canonical ACK digest differs from outbox digest")
            payload = canonical_json_bytes(receipt.as_dict())
            self._connection.execute(
                "UPDATE m3b_outbox SET state='ACKED', receipt_json=? WHERE submission_key=?",
                (payload, submission_key),
            )
            self._connection.commit()
            return OutboxEntry(entry.submission_key, entry.request_digest, entry.protocol_generation, entry.request_json, "ACKED", payload)

    def _transition(self, submission_key: str, *, from_states: set[str], state: OutboxState) -> OutboxEntry:
        _key(submission_key)
        with self._lock:
            row = self._row(submission_key)
            if row is None:
                _fail("outbox_missing", "submission has no durable outbox entry")
            entry = self._entry(row)
            if entry.state not in from_states:
                return entry
            self._connection.execute("UPDATE m3b_outbox SET state=? WHERE submission_key=?", (state, submission_key))
            self._connection.commit()
            return OutboxEntry(entry.submission_key, entry.request_digest, entry.protocol_generation, entry.request_json, state, entry.receipt_json)

    def _row(self, submission_key: str) -> tuple[Any, ...] | None:
        return self._connection.execute(
            "SELECT submission_key,request_digest,protocol_generation,request_json,state,receipt_json FROM m3b_outbox WHERE submission_key=?",
            (submission_key,),
        ).fetchone()

    def get(self, submission_key: str) -> OutboxEntry | None:
        _key(submission_key)
        with self._lock:
            row = self._row(submission_key)
        return self._entry(row) if row is not None else None

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "BrowserAdmissionOutbox":
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()


class ShadowNativeAdmissionBridge:
    """Test-only transport delegating acceptance and lookup to M3a."""

    def __init__(self, store: ShadowSubmissionStore, *, protocol_generation: str = M3B_PROTOCOL_GENERATION) -> None:
        if not isinstance(store, ShadowSubmissionStore):
            _fail("invalid_native_bridge", "Native bridge requires the M3a shadow store")
        self.store = store
        self.protocol_generation = protocol_generation

    def handshake(self, client_protocol_generation: str) -> ProtocolCapability:
        if client_protocol_generation != self.protocol_generation or client_protocol_generation != M3B_PROTOCOL_GENERATION:
            _fail(
                "unsupported_protocol",
                "Browser and Native Host do not share the supported vNext protocol generation",
                details={"client": client_protocol_generation, "host": self.protocol_generation},
            )
        return ProtocolCapability(
            M3B_PROTOCOL_GENERATION,
            M3B_BROWSER_COMPONENT_ID,
            M3B_NATIVE_HOST_ID,
            canonical_lookup=True,
            production_acceptance=False,
        )

    def send(self, envelope: AdmissionEnvelope, *, lose_ack: bool = False) -> AdmissionReceipt:
        self.handshake(envelope.protocol_generation)
        try:
            receipt = self.store.admit(envelope.request)
        except M3aError as exc:
            _fail(exc.code, str(exc), details=exc.details)
        if lose_ack:
            _fail("ack_lost", "Native transport lost the ACK after canonical admission")
        return receipt

    def lookup(self, submission_key: str, request_digest: str, *, protocol_generation: str) -> AdmissionReceipt | None:
        self.handshake(protocol_generation)
        _digest(request_digest, field="request_digest")
        receipt = self.store.lookup(submission_key)
        if receipt is not None and receipt.request_digest != request_digest:
            _fail("submission_conflict", "canonical lookup found a different digest for the same key")
        return receipt


class BrowserAdmissionClient:
    """Outbox-first Browser client; no local acceptance or fallback path."""

    def __init__(self, outbox: BrowserAdmissionOutbox, bridge: ShadowNativeAdmissionBridge) -> None:
        self.outbox = outbox
        self.bridge = bridge
        self.capability = self.bridge.handshake(M3B_PROTOCOL_GENERATION)

    def submit(
        self,
        request: ShadowSubmissionRequest,
        *,
        crash_point: Literal["after_outbox_before_send", "after_send_before_ack"] | None = None,
    ) -> AdmissionReceipt:
        envelope = AdmissionEnvelope(request)
        prepared = self.outbox.prepare(envelope)
        if prepared.state == "ACKED":
            receipt = prepared.receipt()
            if receipt is None:
                _fail("outbox_corrupt", "ACKED outbox entry has no receipt")
            return receipt
        self.outbox.mark_sent(envelope.submission_key)
        if crash_point == "after_outbox_before_send":
            _fail("simulated_browser_crash_before_send", "fault injected after durable outbox write")
        try:
            receipt = self.bridge.send(envelope, lose_ack=crash_point == "after_send_before_ack")
        except M3bError:
            raise
        self.outbox.mark_acked(envelope.submission_key, receipt)
        return receipt

    def recover(self, submission_key: str) -> AdmissionReceipt | None:
        entry = self.outbox.get(submission_key)
        if entry is None:
            _fail("anchor_lost", "Browser cannot recover a submission without its durable outbox anchor")
        receipt = entry.receipt()
        if receipt is not None:
            return receipt
        request = entry.request()
        receipt = self.bridge.lookup(
            request.submission_key,
            entry.request_digest,
            protocol_generation=entry.protocol_generation,
        )
        if receipt is None:
            return None
        self.outbox.mark_acked(submission_key, receipt)
        return receipt

    def retry(self, submission_key: str) -> AdmissionReceipt:
        entry = self.outbox.get(submission_key)
        if entry is None:
            _fail("anchor_lost", "Browser cannot retry without a durable submission anchor")
        recovered = self.recover(submission_key)
        if recovered is not None:
            return recovered
        request = entry.request()
        envelope = AdmissionEnvelope(request, protocol_generation=entry.protocol_generation)
        self.outbox.mark_sent(submission_key)
        receipt = self.bridge.send(envelope)
        self.outbox.mark_acked(submission_key, receipt)
        return receipt


def _overlaps(left: Path, right: Path) -> bool:
    left_value = os.path.normcase(str(left))
    right_value = os.path.normcase(str(right))
    try:
        return os.path.commonpath((left_value, right_value)) in {left_value, right_value}
    except ValueError:
        return False


__all__ = [
    "AdmissionEnvelope",
    "BrowserAdmissionClient",
    "BrowserAdmissionOutbox",
    "M3B_BROWSER_COMPONENT_ID",
    "M3B_NATIVE_HOST_ID",
    "M3B_OUTBOX_SCHEMA",
    "M3B_PROTOCOL_GENERATION",
    "M3B_SCHEMA",
    "M3bError",
    "ProtocolCapability",
    "ShadowNativeAdmissionBridge",
]

"""M3a shadow Submission/Task admission substrate.

This module is deliberately a disposable vNext fixture.  It is not imported
by the production composition root and it cannot be opened without an explicit
``shadow=True`` assertion.  The store owns only its dedicated SQLite file and
never reads or writes legacy Journal, receipt, spool, Session, or Command
state.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Literal, NoReturn

from bdb_shared.evidence import canonical_json_bytes, semantic_digest
from bdb_vnext.control_store import ControlStoreError, assert_database_path, begin_control_write, commit_control_write, ensure_identity


M3A_SCHEMA = "bdb-vnext-m3a-submission-v1"
M3A_STORE_SCHEMA = "bdb-vnext-m3a-shadow-store-v1"
M3A_CANONICALIZATION_VERSION = "bdb-vnext-canonical-request-v1"
M3A_WRITER_ID = "m3a-shadow-test-writer"
M3A_JOURNAL_MODE = "wal"
M3A_BUSY_TIMEOUT_MS = 250
M3A_MAX_REQUEST_BYTES = 256 * 1024
M3A_MAX_BINDING_BYTES = 32 * 1024

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}$")

AdmissionStatus = Literal["ACCEPTED", "TOMBSTONED"]
AdmissionDisposition = Literal["ACCEPTED", "REJECTED"]
AdmissionOutcome = Literal["published", "replay"]


class M3aError(RuntimeError):
    """Bounded, machine-readable M3a failure."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def _fail(code: str, message: str, *, details: Mapping[str, Any] | None = None) -> NoReturn:
    raise M3aError(code, message, details=details)


def _mapping(value: object, *, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _fail("invalid_canonical_request", f"{field} must be an object")
    return {str(key): item for key, item in value.items()}


def _text(value: object, *, field: str, pattern: re.Pattern[str] = _IDENTIFIER) -> str:
    if not isinstance(value, str) or not value or pattern.fullmatch(value) is None:
        _fail("invalid_canonical_request", f"{field} is not a valid bounded identifier")
    return value


def _digest(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        _fail("invalid_digest", f"{field} must be a lowercase sha256 digest")
    return value


def _canonical_bytes(value: Mapping[str, Any], *, field: str) -> bytes:
    try:
        encoded = canonical_json_bytes(value)
    except (TypeError, ValueError, OverflowError) as exc:
        _fail("invalid_canonical_request", f"{field} cannot be canonically encoded")
    if len(encoded) > M3A_MAX_REQUEST_BYTES:
        _fail("canonical_request_too_large", f"{field} exceeds the bounded request size")
    return encoded


def _digest_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


@dataclass(frozen=True)
class ShadowSubmissionRequest:
    """Caller-provided immutable identity and intent envelope.

    ``submission_key`` is intentionally opaque.  The store never derives it
    from a legacy Session, Command, receipt, spool, nonce, or Browser state.
    ``task_id`` is optional for a new task; when omitted it is deterministically
    derived from this key inside the shadow namespace.
    """

    submission_key: str
    intent_revision: str
    intent: Mapping[str, Any]
    conversation_binding: Mapping[str, Any]
    consumer_binding: Mapping[str, Any]
    canonicalization_version: str = M3A_CANONICALIZATION_VERSION
    task_id: str | None = None
    expected_intent_revision_id: str | None = None
    request_digest: str | None = None

    def __post_init__(self) -> None:
        _text(self.submission_key, field="submission_key", pattern=_KEY)
        _text(self.intent_revision, field="intent_revision")
        if self.canonicalization_version != M3A_CANONICALIZATION_VERSION:
            _fail(
                "unsupported_canonical_version",
                "M3a accepts only the frozen canonical request generation",
                details={"received": self.canonicalization_version, "expected": M3A_CANONICALIZATION_VERSION},
            )
        if self.task_id is not None:
            _text(self.task_id, field="task_id")
        if self.expected_intent_revision_id is not None:
            _digest(self.expected_intent_revision_id, field="expected_intent_revision_id")
        object.__setattr__(self, "intent", _mapping(self.intent, field="intent"))
        object.__setattr__(self, "conversation_binding", _mapping(self.conversation_binding, field="conversation_binding"))
        object.__setattr__(self, "consumer_binding", _mapping(self.consumer_binding, field="consumer_binding"))
        if self.request_digest is not None:
            _digest(self.request_digest, field="request_digest")
        _canonical_bytes(self.canonical_payload(), field="canonical request")

    def canonical_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": M3A_SCHEMA,
            "canonicalization_version": self.canonicalization_version,
            "submission_key": self.submission_key,
            "intent_revision": self.intent_revision,
            "intent": dict(self.intent),
            "conversation_binding": dict(self.conversation_binding),
            "consumer_binding": dict(self.consumer_binding),
        }
        if self.task_id is not None:
            payload["task_id"] = self.task_id
        if self.expected_intent_revision_id is not None:
            payload["expected_intent_revision_id"] = self.expected_intent_revision_id
        return payload

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.canonical_payload(), field="canonical request")

    def computed_digest(self) -> str:
        return semantic_digest(self.canonical_payload())

    def validated_digest(self) -> str:
        computed = self.computed_digest()
        if self.request_digest is not None and self.request_digest != computed:
            _fail(
                "digest_mismatch",
                "claimed request digest does not match canonical request bytes",
                details={"claimed": self.request_digest, "computed": computed},
            )
        return computed

    def as_dict(self) -> dict[str, Any]:
        value = self.canonical_payload()
        value["request_digest"] = self.validated_digest()
        return value


@dataclass(frozen=True)
class AdmissionReceipt:
    submission_key: str
    request_digest: str
    status: AdmissionStatus
    disposition: AdmissionDisposition
    task_id: str | None
    intent_revision_id: str | None
    outcome: AdmissionOutcome
    tombstone_reason: str | None = None

    def __post_init__(self) -> None:
        _text(self.submission_key, field="submission_key", pattern=_KEY)
        _digest(self.request_digest, field="request_digest")
        if self.status == "ACCEPTED" and (
            self.disposition != "ACCEPTED" or self.task_id is None or self.intent_revision_id is None
        ):
            _fail("corrupt_admission", "accepted receipt must bind one Task and one intent revision")
        if self.status == "TOMBSTONED" and (
            self.disposition != "REJECTED" or self.task_id is not None or self.intent_revision_id is not None
        ):
            _fail("corrupt_admission", "tombstone receipt must not bind a Task")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": M3A_SCHEMA,
            "submission_key": self.submission_key,
            "request_digest": self.request_digest,
            "status": self.status,
            "disposition": self.disposition,
            "task_id": self.task_id,
            "intent_revision_id": self.intent_revision_id,
            "outcome": self.outcome,
            "tombstone_reason": self.tombstone_reason,
        }


@dataclass(frozen=True)
class ShadowTask:
    task_id: str
    submission_key: str
    intent_revision_id: str
    intent_revision: str
    intent_digest: str
    conversation_binding: Mapping[str, Any]
    consumer_binding: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": M3A_SCHEMA,
            "task_id": self.task_id,
            "submission_key": self.submission_key,
            "intent_revision_id": self.intent_revision_id,
            "intent_revision": self.intent_revision,
            "intent_digest": self.intent_digest,
            "conversation_binding": dict(self.conversation_binding),
            "consumer_binding": dict(self.consumer_binding),
        }


class ShadowSubmissionStore:
    """An explicitly isolated, test-only SQLite writer for the M3a namespace."""

    def __init__(
        self,
        root: str | Path,
        *,
        shadow: bool = False,
        legacy_root: str | Path | None = None,
        busy_timeout_ms: int = M3A_BUSY_TIMEOUT_MS,
    ) -> None:
        if shadow is not True:
            _fail("shadow_mode_required", "M3a store requires an explicit shadow=True assertion")
        if not isinstance(busy_timeout_ms, int) or not 1 <= busy_timeout_ms <= 10_000:
            _fail("invalid_busy_timeout", "busy timeout must be bounded")
        self.root = Path(os.path.abspath(Path(root).expanduser()))
        if not self.root.is_absolute():
            _fail("relative_path", "M3a store root must be absolute")
        if legacy_root is not None:
            legacy = Path(os.path.abspath(Path(legacy_root).expanduser()))
            if _overlaps(self.root, legacy):
                _fail("foreign_state_overlap", "M3a shadow root overlaps the frozen legacy root")
        self.root.mkdir(parents=True, exist_ok=True)
        self.control_root = self.root / "control"
        self.config_root = self.root / "config"
        self.control_root.mkdir(parents=True, exist_ok=True)
        self.config_root.mkdir(parents=True, exist_ok=True)
        # M3 owns only its table namespace; the physical DB is shared.
        self.database_path = assert_database_path(self.root, self.control_root / "control.db")
        self.config_path = self.config_root / "m3a-shadow.json"
        self._busy_timeout_ms = busy_timeout_ms
        self._connection = sqlite3.connect(
            str(self.database_path),
            timeout=busy_timeout_ms / 1000,
            check_same_thread=False,
            isolation_level=None,
        )
        self._connection_lock = threading.RLock()
        self._configure()
        try:
            ensure_identity(self._connection)
        except Exception as exc:
            self._connection.close()
            if hasattr(exc, "code"):
                _fail(str(getattr(exc, "code")), str(exc))
            raise
        self._ensure_config()
        self._ensure_schema()

    def _configure(self) -> None:
        try:
            self._connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
            self._connection.execute("PRAGMA foreign_keys=ON")
            deadline = time.monotonic() + (self._busy_timeout_ms / 1000) + 0.5
            while True:
                try:
                    mode = str(self._connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
                    if mode != M3A_JOURNAL_MODE:
                        mode = str(self._connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
                    self._connection.execute("PRAGMA synchronous=FULL")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                        raise
                    if time.monotonic() >= deadline:
                        _fail("sqlite_settings_busy", "M3a SQLite settings remained busy during concurrent open")
                    time.sleep(0.005)
        except M3aError:
            raise
        except sqlite3.DatabaseError as exc:
            _fail("sqlite_settings_failed", "M3a SQLite settings could not be applied")
        if mode != M3A_JOURNAL_MODE:
            _fail("wal_unavailable", "M3a shadow store requires WAL journaling")

    def _ensure_config(self) -> None:
        document = {
            "schema": M3A_STORE_SCHEMA,
            "mode": "SHADOW_ONLY",
            "writer_id": M3A_WRITER_ID,
            "production_admission": False,
            "legacy_import": False,
            "legacy_dual_write": False,
        }
        expected = canonical_json_bytes(document)
        if self.config_path.exists():
            try:
                actual = self.config_path.read_bytes()
            except OSError as exc:
                _fail("shadow_config_unavailable", "M3a shadow config could not be read")
            if actual != expected:
                _fail("shadow_config_mismatch", "M3a shadow config identity differs")
            return
        self.config_path.write_bytes(expected)

    def _ensure_schema(self) -> None:
        with self._connection_lock:
            schema_sql = """
                CREATE TABLE IF NOT EXISTS m3a_submissions (
                    submission_key TEXT PRIMARY KEY,
                    request_digest TEXT NOT NULL,
                    canonical_request BLOB NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('ACCEPTED','TOMBSTONED')),
                    disposition TEXT NOT NULL CHECK(disposition IN ('ACCEPTED','REJECTED')),
                    task_id TEXT,
                    intent_revision_id TEXT,
                    tombstone_reason TEXT,
                    created_order INTEGER NOT NULL,
                    UNIQUE(submission_key, request_digest)
                );
                CREATE TABLE IF NOT EXISTS m3a_tasks (
                    task_id TEXT PRIMARY KEY,
                    submission_key TEXT NOT NULL UNIQUE,
                    intent_revision_id TEXT NOT NULL,
                    intent_revision TEXT NOT NULL,
                    intent_digest TEXT NOT NULL,
                    conversation_binding BLOB NOT NULL,
                    consumer_binding BLOB NOT NULL,
                    FOREIGN KEY(submission_key) REFERENCES m3a_submissions(submission_key)
                );
                CREATE TABLE IF NOT EXISTS m3a_intent_revisions (
                    intent_revision_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL UNIQUE,
                    intent_revision TEXT NOT NULL,
                    intent_digest TEXT NOT NULL,
                    canonical_intent BLOB NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES m3a_tasks(task_id)
                );
                CREATE TABLE IF NOT EXISTS m3a_consumer_bindings (
                    submission_key TEXT PRIMARY KEY,
                    conversation_binding BLOB NOT NULL,
                    consumer_binding BLOB NOT NULL,
                    FOREIGN KEY(submission_key) REFERENCES m3a_submissions(submission_key)
                );
                """
            deadline = time.monotonic() + (self._busy_timeout_ms / 1000) + 0.5
            while True:
                try:
                    self._connection.executescript(schema_sql)
                    return
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                        _fail("schema_init_failed", "M3a shadow schema could not be initialized")
                    if time.monotonic() >= deadline:
                        _fail("schema_init_busy", "M3a shadow schema remained busy during concurrent open")
                    time.sleep(0.005)
                except sqlite3.DatabaseError as exc:
                    _fail("schema_init_failed", "M3a shadow schema could not be initialized")

    @property
    def writer_id(self) -> str:
        return M3A_WRITER_ID

    @property
    def control_connection(self) -> sqlite3.Connection:
        """Internal M3 control-plane connection; callers do not get raw SQL APIs."""

        return self._connection

    def _task_id(self, request: ShadowSubmissionRequest) -> str:
        if request.task_id is not None:
            return request.task_id
        digest = hashlib.sha256(request.submission_key.encode("utf-8")).hexdigest()[:48]
        return f"task-{digest}"

    def _intent_digest(self, request: ShadowSubmissionRequest) -> str:
        return semantic_digest(
            {
                "schema": "bdb-vnext-intent-v1",
                "intent_revision": request.intent_revision,
                "intent": dict(request.intent),
            }
        )

    def _intent_revision_id(self, task_id: str, request: ShadowSubmissionRequest, intent_digest: str) -> str:
        return semantic_digest(
            {
                "schema": "bdb-vnext-intent-revision-v1",
                "task_id": task_id,
                "intent_revision": request.intent_revision,
                "intent_digest": intent_digest,
            }
        )

    def _receipt_from_row(self, row: tuple[Any, ...], *, outcome: AdmissionOutcome) -> AdmissionReceipt:
        return AdmissionReceipt(
            submission_key=str(row[0]),
            request_digest=str(row[1]),
            status=str(row[2]),  # type: ignore[arg-type]
            disposition=str(row[3]),  # type: ignore[arg-type]
            task_id=str(row[4]) if row[4] is not None else None,
            intent_revision_id=str(row[5]) if row[5] is not None else None,
            outcome=outcome,
            tombstone_reason=str(row[6]) if row[6] is not None else None,
        )

    def _existing(self, submission_key: str) -> tuple[Any, ...] | None:
        return self._connection.execute(
            "SELECT submission_key,request_digest,status,disposition,task_id,intent_revision_id,tombstone_reason "
            "FROM m3a_submissions WHERE submission_key = ?",
            (submission_key,),
        ).fetchone()

    def _check_existing(self, request: ShadowSubmissionRequest, existing: tuple[Any, ...]) -> AdmissionReceipt:
        stored_digest = str(existing[1])
        computed = request.validated_digest()
        if stored_digest != computed:
            _fail(
                "submission_conflict",
                "submission key is already bound to a different canonical request",
                details={"submission_key": request.submission_key, "stored_digest": stored_digest, "received_digest": computed},
            )
        if str(existing[2]) == "TOMBSTONED":
            _fail("tombstone_conflict", "effectful tombstone is retained for this submission key")
        return self._receipt_from_row(existing, outcome="replay")

    def admit(
        self,
        request: ShadowSubmissionRequest,
        *,
        failpoint: Literal["before_commit", "after_commit"] | None = None,
        admission_guard: Callable[[], None] | None = None,
    ) -> AdmissionReceipt:
        if not isinstance(request, ShadowSubmissionRequest):
            _fail("invalid_canonical_request", "admit requires ShadowSubmissionRequest")
        canonical = request.canonical_bytes()
        request_digest = request.validated_digest()
        task_id = self._task_id(request)
        intent_digest = self._intent_digest(request)
        intent_revision_id = self._intent_revision_id(task_id, request, intent_digest)
        conversation = _canonical_bytes(request.conversation_binding, field="conversation_binding")
        consumer = _canonical_bytes(request.consumer_binding, field="consumer_binding")
        intent_bytes = _canonical_bytes(request.intent, field="intent")
        with self._connection_lock:
            try:
                begin_control_write(self._connection)
                if admission_guard is not None:
                    admission_guard()
                existing = self._existing(request.submission_key)
                if existing is not None:
                    receipt = self._check_existing(request, existing)
                    commit_control_write(self._connection)
                    return receipt
                task = self._connection.execute(
                    "SELECT task_id,intent_revision_id,intent_revision,intent_digest FROM m3a_tasks WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                if task is not None:
                    current_revision = str(task[1])
                    if request.expected_intent_revision_id != current_revision:
                        _fail(
                            "stale_intent_revision",
                            "request expected an intent revision other than the current Task revision",
                            details={"task_id": task_id, "expected": request.expected_intent_revision_id, "current": current_revision},
                        )
                    _fail("task_conflict", "Task identity is already owned by another submission")
                if request.expected_intent_revision_id is not None:
                    _fail(
                        "stale_intent_revision",
                        "expected intent revision cannot be satisfied for a new Task",
                        details={"expected": request.expected_intent_revision_id},
                    )
                self._connection.execute(
                    "INSERT INTO m3a_submissions(submission_key,request_digest,canonical_request,status,disposition,task_id,intent_revision_id,tombstone_reason,created_order) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        request.submission_key,
                        request_digest,
                        canonical,
                        "ACCEPTED",
                        "ACCEPTED",
                        task_id,
                        intent_revision_id,
                        None,
                        _next_order(self._connection),
                    ),
                )
                self._connection.execute(
                    "INSERT INTO m3a_tasks(task_id,submission_key,intent_revision_id,intent_revision,intent_digest,conversation_binding,consumer_binding) VALUES (?,?,?,?,?,?,?)",
                    (task_id, request.submission_key, intent_revision_id, request.intent_revision, intent_digest, conversation, consumer),
                )
                self._connection.execute(
                    "INSERT INTO m3a_intent_revisions(intent_revision_id,task_id,intent_revision,intent_digest,canonical_intent) VALUES (?,?,?,?,?)",
                    (intent_revision_id, task_id, request.intent_revision, intent_digest, intent_bytes),
                )
                self._connection.execute(
                    "INSERT INTO m3a_consumer_bindings(submission_key,conversation_binding,consumer_binding) VALUES (?,?,?)",
                    (request.submission_key, conversation, consumer),
                )
                if failpoint == "before_commit":
                    _fail("simulated_crash_before_commit", "fault injected before transaction commit")
                receipt = AdmissionReceipt(
                    request.submission_key,
                    request_digest,
                    "ACCEPTED",
                    "ACCEPTED",
                    task_id,
                    intent_revision_id,
                    "published",
                )
                commit_control_write(self._connection)
                if failpoint == "after_commit":
                    _fail("simulated_response_loss_after_commit", "fault injected after durable commit")
                return receipt
            except ControlStoreError as exc:
                if self._connection.in_transaction:
                    self._connection.rollback()
                _fail(exc.code, str(exc))
            except M3aError:
                if self._connection.in_transaction:
                    self._connection.rollback()
                raise
            except sqlite3.IntegrityError as exc:
                if self._connection.in_transaction:
                    self._connection.rollback()
                _fail("admission_conflict", "atomic admission encountered a uniqueness conflict")
            except sqlite3.OperationalError as exc:
                if self._connection.in_transaction:
                    self._connection.rollback()
                text = str(exc).lower()
                if "locked" in text or "busy" in text:
                    _fail("database_busy", "M3a shadow database is busy")
                _fail("sqlite_write_failed", "M3a shadow admission could not commit")
            except sqlite3.DatabaseError:
                if self._connection.in_transaction:
                    self._connection.rollback()
                _fail("sqlite_write_failed", "M3a shadow admission could not commit")
            except Exception:
                if self._connection.in_transaction:
                    self._connection.rollback()
                raise

    def tombstone(self, request: ShadowSubmissionRequest, *, reason: str) -> AdmissionReceipt:
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 256:
            _fail("invalid_tombstone", "tombstone reason must be bounded and non-empty")
        canonical = request.canonical_bytes()
        digest = request.validated_digest()
        with self._connection_lock:
            try:
                begin_control_write(self._connection)
                existing = self._existing(request.submission_key)
                if existing is not None:
                    if str(existing[1]) != digest:
                        _fail("submission_conflict", "tombstone key is already bound to another digest")
                    if str(existing[2]) == "TOMBSTONED":
                        commit_control_write(self._connection)
                        return self._receipt_from_row(existing, outcome="replay")
                    _fail("tombstone_conflict", "an accepted submission cannot be rewritten as a tombstone")
                self._connection.execute(
                    "INSERT INTO m3a_submissions(submission_key,request_digest,canonical_request,status,disposition,task_id,intent_revision_id,tombstone_reason,created_order) VALUES (?,?,?,?,?,?,?,?,?)",
                    (request.submission_key, digest, canonical, "TOMBSTONED", "REJECTED", None, None, reason, _next_order(self._connection)),
                )
                commit_control_write(self._connection)
                return AdmissionReceipt(request.submission_key, digest, "TOMBSTONED", "REJECTED", None, None, "published", reason)
            except M3aError:
                if self._connection.in_transaction:
                    self._connection.rollback()
                raise
            except sqlite3.DatabaseError as exc:
                if self._connection.in_transaction:
                    self._connection.rollback()
                _fail("sqlite_write_failed", "M3a tombstone could not commit")

    def lookup(self, submission_key: str) -> AdmissionReceipt | None:
        _text(submission_key, field="submission_key", pattern=_KEY)
        with self._connection_lock:
            row = self._existing(submission_key)
        return self._receipt_from_row(row, outcome="replay") if row is not None else None

    def task(self, task_id: str) -> ShadowTask | None:
        _text(task_id, field="task_id")
        with self._connection_lock:
            row = self._connection.execute(
                "SELECT t.task_id,t.submission_key,t.intent_revision_id,t.intent_revision,t.intent_digest,t.conversation_binding,t.consumer_binding "
                "FROM m3a_tasks t WHERE t.task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        return ShadowTask(
            str(row[0]),
            str(row[1]),
            str(row[2]),
            str(row[3]),
            str(row[4]),
            json.loads(bytes(row[5]).decode("utf-8")),
            json.loads(bytes(row[6]).decode("utf-8")),
        )

    def find_tasks(self, *, conversation_id: str, intent_revision: str) -> tuple[dict[str, Any], ...]:
        """Return exact admitted Task identities for a canonical recovery lookup.

        This is deliberately read-only and returns all matches.  Callers must
        reject zero or multiple matches rather than selecting by recency.
        Canonical intent is read from the immutable intent-revision row so a
        Browser projection never becomes an admission authority.
        """
        _text(conversation_id, field="conversation_id")
        _text(intent_revision, field="intent_revision")
        with self._connection_lock:
            rows = self._connection.execute(
                "SELECT t.task_id,t.submission_key,t.intent_revision_id,t.intent_revision,t.intent_digest,"
                "t.conversation_binding,t.consumer_binding,s.request_digest,r.canonical_intent "
                "FROM m3a_tasks t "
                "JOIN m3a_submissions s ON s.submission_key=t.submission_key "
                "JOIN m3a_intent_revisions r ON r.intent_revision_id=t.intent_revision_id "
                "WHERE t.intent_revision=? ORDER BY s.created_order",
                (intent_revision,),
            ).fetchall()
        matches: list[dict[str, Any]] = []
        for row in rows:
            try:
                task_binding = json.loads(bytes(row[5]).decode("utf-8"))
                consumer_binding = json.loads(bytes(row[6]).decode("utf-8"))
                canonical_intent = json.loads(bytes(row[8]).decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError, TypeError) as exc:
                _fail("corrupt_admission", "canonical recovery encountered malformed Task identity")
            if not isinstance(task_binding, Mapping) or task_binding.get("conversation_id") != conversation_id:
                continue
            if not isinstance(consumer_binding, Mapping) or not isinstance(canonical_intent, Mapping):
                _fail("corrupt_admission", "canonical recovery encountered malformed Task binding")
            matches.append(
                {
                    "task": {
                        "schema": M3A_SCHEMA,
                        "task_id": str(row[0]),
                        "submission_key": str(row[1]),
                        "intent_revision_id": str(row[2]),
                        "intent_revision": str(row[3]),
                        "intent_digest": str(row[4]),
                        "conversation_binding": dict(task_binding),
                        "consumer_binding": dict(consumer_binding),
                    },
                    "request_digest": str(row[7]),
                    "canonical_intent": dict(canonical_intent),
                }
            )
        return tuple(matches)

    def counts(self) -> dict[str, int]:
        with self._connection_lock:
            return {
                "submissions": int(self._connection.execute("SELECT COUNT(*) FROM m3a_submissions").fetchone()[0]),
                "tasks": int(self._connection.execute("SELECT COUNT(*) FROM m3a_tasks").fetchone()[0]),
                "intent_revisions": int(self._connection.execute("SELECT COUNT(*) FROM m3a_intent_revisions").fetchone()[0]),
                "consumer_bindings": int(self._connection.execute("SELECT COUNT(*) FROM m3a_consumer_bindings").fetchone()[0]),
            }

    @contextmanager
    def hold_write_lock(self) -> Iterator[None]:
        """Hold a transaction for the bounded DB-busy test."""

        with self._connection_lock:
            try:
                begin_control_write(self._connection)
            except ControlStoreError as exc:
                _fail(exc.code, str(exc))
            except sqlite3.OperationalError as exc:
                text = str(exc).lower()
                if "locked" in text or "busy" in text:
                    _fail("database_busy", "M3a shadow database is busy")
                _fail("sqlite_write_failed", "M3a shadow write transaction could not begin")
            try:
                yield
            finally:
                if self._connection.in_transaction:
                    self._connection.rollback()

    def close(self) -> None:
        with self._connection_lock:
            self._connection.close()

    def __enter__(self) -> "ShadowSubmissionStore":
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()


def _next_order(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT COALESCE(MAX(created_order), 0) + 1 FROM m3a_submissions").fetchone()
    return int(row[0])


def _overlaps(left: Path, right: Path) -> bool:
    left_value = os.path.normcase(str(left))
    right_value = os.path.normcase(str(right))
    try:
        return os.path.commonpath((left_value, right_value)) in {left_value, right_value}
    except ValueError:
        return False


__all__ = [
    "AdmissionReceipt",
    "M3A_CANONICALIZATION_VERSION",
    "M3A_SCHEMA",
    "M3A_STORE_SCHEMA",
    "M3aError",
    "ShadowSubmissionRequest",
    "ShadowSubmissionStore",
    "ShadowTask",
]

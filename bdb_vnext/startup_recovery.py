"""NX-029 deterministic startup recovery from Project Memory v2.

This module is deliberately a read-side reconciler.  A BDB, Native, or
Browser restart destroys process-local objects, but it does not create a new
source of workflow truth.  ``StartupReconciler`` opens the canonical Project
Memory v2 database, takes one consistent read snapshot, computes a semantic
digest, and selects the first applicable rule in a total precedence order.

The result is a next-action decision, not an effect executor.  In particular,
startup recovery never clicks a Browser, sends a message, reclaims a lease,
changes a cursor, or trusts Browser/local cache state.  Effectful callers must
revalidate the returned identity through the existing NX-021/NX-022,
NX-025/NX-026, and NX-027 boundaries before doing anything.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence


STARTUP_RECOVERY_SCHEMA = "bdb-startup-recovery-v1"
STARTUP_RECOVERY_VERSION = "v1"
STARTUP_RECONCILER_VERSION_EXPLICIT = True
CANONICAL_STATE_IS_RECOVERY_AUTHORITY = True
BROWSER_LOCAL_STATE_IS_RECOVERY_AUTHORITY = False
RECOVERY_STORAGE_DATABASE = "memory.db"
RECOVERY_SCHEMA_OWNER = "ProjectMemoryStoreV2"
RECOVERY_TRANSACTION_AUTHORITY = "ProjectMemoryStoreV2._transaction"


class RecoveryRule(str, Enum):
    """Executable startup-recovery precedence rules, highest first."""

    STOPPED = "STOPPED"
    SECURITY_DATA_CORRUPTION_FREEZE = "SECURITY_DATA_CORRUPTION_FREEZE"
    DELIVERY_UNCERTAIN = "DELIVERY_UNCERTAIN"
    ACTIVE_SEND_INTENT = "ACTIVE_SEND_INTENT"
    CONTINUATION_REENTRY = "CONTINUATION_REENTRY"
    OPERATOR_CHECKPOINT = "OPERATOR_CHECKPOINT"
    ACTIVE_CONTINUATION_LEASE = "ACTIVE_CONTINUATION_LEASE"
    EXPIRED_ORPHAN_LEASE = "EXPIRED_ORPHAN_LEASE"
    CI_WAITING = "CI_WAITING"
    REPAIR_RETEST = "REPAIR_RETEST"
    WAITING_FOR_PLAN = "WAITING_FOR_PLAN"
    MANUAL_POLICY_CHECKPOINT = "MANUAL_POLICY_CHECKPOINT"
    CURRENT_RUNNABLE_TASK = "CURRENT_RUNNABLE_TASK"
    PROJECT_SCOPE_COMPLETE = "PROJECT_SCOPE_COMPLETE"
    IDLE = "IDLE"


class RecoveryAction(str, Enum):
    """Bounded next actions returned by the reconciler."""

    STOPPED = "STOPPED"
    FREEZE_AND_REQUIRE_RECONCILIATION = "FREEZE_AND_REQUIRE_RECONCILIATION"
    RECONCILE_UNCERTAIN_DELIVERY = "RECONCILE_UNCERTAIN_DELIVERY"
    ACK_SEND_INTENT = "ACK_SEND_INTENT"
    RESUME_SEND_INTENT = "RESUME_SEND_INTENT"
    RESUME_SESSION_REENTRY = "RESUME_SESSION_REENTRY"
    OPERATOR_CHECKPOINT = "OPERATOR_CHECKPOINT"
    WAIT_FOR_ACTIVE_CONTINUATION_LEASE = "WAIT_FOR_ACTIVE_CONTINUATION_LEASE"
    RECLAIM_EXPIRED_CONTINUATION_LEASE = "RECLAIM_EXPIRED_CONTINUATION_LEASE"
    CI_WAITING = "CI_WAITING"
    REPAIR_RETEST = "REPAIR_RETEST"
    WAITING_FOR_PLAN = "WAITING_FOR_PLAN"
    MANUAL_POLICY_CHECKPOINT = "MANUAL_POLICY_CHECKPOINT"
    RESUME_CURRENT_TASK = "RESUME_CURRENT_TASK"
    RUN_CURRENT_TASK = "RUN_CURRENT_TASK"
    ADVANCE_SCOPE = "ADVANCE_SCOPE"
    PROJECT_SCOPE_COMPLETE = "PROJECT_SCOPE_COMPLETE"
    IDLE = "IDLE"


class RecoveryError(RuntimeError):
    """Fail-closed canonical-state read error."""

    def __init__(self, code: str, message: str, *, table: str | None = None, row_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.table = table
        self.row_id = row_id


@dataclass(frozen=True)
class CanonicalRecoverySnapshot:
    """Consistent, redacted semantic projection of Project Memory v2."""

    project_id: str
    canonical_revision: int
    snapshot_digest: str
    data: Mapping[str, Any]

    @property
    def digest(self) -> str:
        return self.snapshot_digest

    @property
    def state_digest(self) -> str:
        return self.snapshot_digest

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": STARTUP_RECOVERY_SCHEMA,
            "version": STARTUP_RECOVERY_VERSION,
            "project_id": self.project_id,
            "canonical_revision": self.canonical_revision,
            "snapshot_digest": self.snapshot_digest,
            "data": json.loads(json.dumps(self.data, sort_keys=True)),
        }


@dataclass(frozen=True)
class RecoveryDecision:
    """Structured, effect-free startup recovery decision."""

    accepted: bool
    rule: str
    action: str
    reason_code: str
    explanation: str
    project_id: str
    run_id: str | None
    scope: str | None
    scope_epoch: int | None
    current_milestone_id: str | None
    current_task_id: str | None
    canonical_revision: int
    snapshot_digest: str
    trace: tuple[str, ...] = ()
    diagnostics: Mapping[str, Any] = field(default_factory=dict)
    rejected_competing_actions: Mapping[str, str] = field(default_factory=dict)
    effects: int = 0

    @property
    def next_action(self) -> str:
        return self.action

    @property
    def selected_next_action(self) -> str:
        return self.action

    @property
    def precedence_rule(self) -> str:
        return self.rule

    @property
    def canonical_state_digest(self) -> str:
        return self.snapshot_digest

    @property
    def auto_effects(self) -> int:
        return self.effects

    @property
    def ambiguous(self) -> bool:
        return self.rule == RecoveryRule.SECURITY_DATA_CORRUPTION_FREEZE.value

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": STARTUP_RECOVERY_SCHEMA,
            "version": STARTUP_RECOVERY_VERSION,
            "accepted": self.accepted,
            "rule": self.rule,
            "precedence_rule": self.rule,
            "action": self.action,
            "selected_next_action": self.action,
            "reason_code": self.reason_code,
            "explanation": self.explanation,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "scope": self.scope,
            "scope_epoch": self.scope_epoch,
            "current_milestone_id": self.current_milestone_id,
            "current_task_id": self.current_task_id,
            "canonical_revision": self.canonical_revision,
            "snapshot_digest": self.snapshot_digest,
            "trace": list(self.trace),
            "diagnostics": json.loads(json.dumps(self.diagnostics, sort_keys=True)),
            "rejected_competing_actions": dict(sorted(self.rejected_competing_actions.items())),
            "effects": self.effects,
        }

    def __bool__(self) -> bool:
        return self.accepted


@dataclass(frozen=True)
class _Candidate:
    rule: RecoveryRule
    action: RecoveryAction
    reason_code: str
    explanation: str
    detail: Mapping[str, Any] = field(default_factory=dict)


_PRECEDENCE: tuple[RecoveryRule, ...] = (
    RecoveryRule.STOPPED,
    RecoveryRule.SECURITY_DATA_CORRUPTION_FREEZE,
    RecoveryRule.DELIVERY_UNCERTAIN,
    RecoveryRule.ACTIVE_SEND_INTENT,
    RecoveryRule.CONTINUATION_REENTRY,
    RecoveryRule.OPERATOR_CHECKPOINT,
    RecoveryRule.ACTIVE_CONTINUATION_LEASE,
    RecoveryRule.EXPIRED_ORPHAN_LEASE,
    RecoveryRule.CI_WAITING,
    RecoveryRule.REPAIR_RETEST,
    RecoveryRule.WAITING_FOR_PLAN,
    RecoveryRule.MANUAL_POLICY_CHECKPOINT,
    RecoveryRule.CURRENT_RUNNABLE_TASK,
    RecoveryRule.PROJECT_SCOPE_COMPLETE,
    RecoveryRule.IDLE,
)
RECOVERY_PRECEDENCE_TABLE: tuple[str, ...] = tuple(item.value for item in _PRECEDENCE)
RECOVERY_PRECEDENCE_AMBIGUITIES = 0

_SEND_ACTIVE = frozenset({"PREPARED", "SEND_ALLOWED", "PHYSICAL_SEND_ATTEMPTED", "SEND_CONFIRMED"})
_SEND_UNCERTAIN = frozenset({"UNCERTAIN", "RECONCILIATION_REQUIRED"})
_LEASE_ACTIVE = frozenset({"CLAIMED", "ACTIVE"})
_LEASE_TERMINAL = frozenset({"COMPLETED", "ABANDONED", "RELEASED", "EXPIRED", "AVAILABLE"})
_REENTRY_PENDING = frozenset({
    "TURN_ENDED",
    "SESSION_UNAVAILABLE",
    "CONTINUATION_PENDING",
    "REENTRY_PREPARED",
    "REENTRY_IN_PROGRESS",
})
_REENTRY_BLOCKED = frozenset({"REENTRY_FAILED", "REENTRY_BLOCKED"})
_KNOWN_SEND_STATUSES = _SEND_ACTIVE | _SEND_UNCERTAIN | frozenset({"ACKNOWLEDGED", "SEND_CONFIRMED", "CANCELLED", "FENCED"})
_KNOWN_LEASE_STATUSES = _LEASE_ACTIVE | _LEASE_TERMINAL
_KNOWN_REENTRY_STATES = frozenset({
    "SESSION_ACTIVE",
    "TURN_ENDED",
    "SESSION_UNAVAILABLE",
    "CONTINUATION_PENDING",
    "REENTRY_PREPARED",
    "REENTRY_IN_PROGRESS",
    "OPERATOR_CHECKPOINT",
    "REENTRY_CONFIRMED",
    "REENTRY_FAILED",
    "REENTRY_BLOCKED",
})


def _parse_utc(value: datetime | str | None, *, field: str) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise RecoveryError("INVALID_RECOVERY_CLOCK", f"{field} is not ISO-8601") from exc
    else:
        raise RecoveryError("INVALID_RECOVERY_CLOCK", f"{field} must be a datetime or ISO-8601 string")
    if result.tzinfo is None or result.utcoffset() is None:
        raise RecoveryError("INVALID_RECOVERY_CLOCK", f"{field} must be timezone-aware")
    return result.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def compute_recovery_digest(value: Mapping[str, Any]) -> str:
    """Compute the deterministic digest for a canonical recovery projection."""

    return _digest(value)


semantic_recovery_digest = compute_recovery_digest


def _json_object(value: Any, *, table: str, column: str, row_id: str | None = None) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str):
        raise RecoveryError("CANONICAL_JSON_CORRUPT", f"{table}.{column} is not JSON", table=table, row_id=row_id)
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RecoveryError("CANONICAL_JSON_CORRUPT", f"{table}.{column} is malformed JSON", table=table, row_id=row_id) from exc
    if not isinstance(parsed, Mapping):
        raise RecoveryError("CANONICAL_JSON_CORRUPT", f"{table}.{column} must be a JSON object", table=table, row_id=row_id)
    return dict(parsed)


def _json_array(value: Any, *, table: str, column: str, row_id: str | None = None) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    if not isinstance(value, str):
        raise RecoveryError("CANONICAL_JSON_CORRUPT", f"{table}.{column} is not JSON", table=table, row_id=row_id)
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RecoveryError("CANONICAL_JSON_CORRUPT", f"{table}.{column} is malformed JSON", table=table, row_id=row_id) from exc
    if not isinstance(parsed, list):
        raise RecoveryError("CANONICAL_JSON_CORRUPT", f"{table}.{column} must be a JSON array", table=table, row_id=row_id)
    return list(parsed)


def _text(value: Any, *, table: str, column: str, row_id: str | None = None, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not value:
        raise RecoveryError("CANONICAL_IDENTITY_CORRUPT", f"{table}.{column} is not a non-empty text identity", table=table, row_id=row_id)
    return value


def _row_dict(row: sqlite3.Row, fields: Sequence[str]) -> dict[str, Any]:
    keys = set(row.keys())
    return {field: row[field] for field in fields if field in keys}


class StartupReconciler:
    """Read-only deterministic recovery entrypoint for one project."""

    STARTUP_RECOVERY_SCHEMA = STARTUP_RECOVERY_SCHEMA
    STARTUP_RECOVERY_VERSION = STARTUP_RECOVERY_VERSION
    STARTUP_RECONCILER_VERSION_EXPLICIT = STARTUP_RECONCILER_VERSION_EXPLICIT
    CANONICAL_STATE_IS_RECOVERY_AUTHORITY = CANONICAL_STATE_IS_RECOVERY_AUTHORITY
    BROWSER_LOCAL_STATE_IS_RECOVERY_AUTHORITY = BROWSER_LOCAL_STATE_IS_RECOVERY_AUTHORITY
    RECOVERY_STORAGE_DATABASE = RECOVERY_STORAGE_DATABASE
    RECOVERY_SCHEMA_OWNER = RECOVERY_SCHEMA_OWNER
    RECOVERY_TRANSACTION_AUTHORITY = RECOVERY_TRANSACTION_AUTHORITY
    RECOVERY_PRECEDENCE_TABLE = RECOVERY_PRECEDENCE_TABLE
    RECOVERY_PRECEDENCE_AMBIGUITIES = RECOVERY_PRECEDENCE_AMBIGUITIES

    def __init__(
        self,
        authority: Any,
        project_id: str | None = None,
        *,
        clock: Callable[[], datetime | str] | None = None,
        browser_state: Any = None,
        native_state: Any = None,
    ) -> None:
        self._connection: sqlite3.Connection | None = None
        self._db_path: Path | None = None
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
        if not isinstance(self.project_id, str) or not self.project_id:
            raise RecoveryError("PROJECT_ID_REQUIRED", "project_id is required for startup recovery")
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        # These are explicitly observational inputs.  They are retained only
        # so a caller can report disagreement; no decision reads them.
        self.browser_state = browser_state
        self.native_state = native_state

    def _open_connection(self) -> sqlite3.Connection:
        if self._connection is not None:
            self._connection.row_factory = sqlite3.Row
            return self._connection
        if self._db_path is None:
            raise RecoveryError("PM_V2_DATABASE_MISSING", "no Project Memory v2 database was supplied")
        try:
            conn = sqlite3.connect(str(self._db_path), timeout=5.0, isolation_level=None)
        except sqlite3.Error as exc:
            raise RecoveryError("PM_V2_DATABASE_UNAVAILABLE", "canonical Project Memory v2 database cannot be opened") from exc
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def _close_connection(self, conn: sqlite3.Connection) -> None:
        if self._connection is None:
            conn.close()

    @contextmanager
    def _read_transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._open_connection()
        own_transaction = self._connection is None or not conn.in_transaction
        try:
            if own_transaction:
                conn.execute("BEGIN")
            yield conn
        except Exception:
            if own_transaction:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
            raise
        else:
            if own_transaction:
                conn.rollback()
        finally:
            self._close_connection(conn)

    @staticmethod
    def _has_table(conn: sqlite3.Connection, table: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone() is not None

    def _rows(self, conn: sqlite3.Connection, table: str, *, project_column: bool = True) -> list[sqlite3.Row]:
        if not self._has_table(conn, table):
            return []
        try:
            if project_column:
                return list(conn.execute(f"SELECT * FROM {table} WHERE project_id = ?", (self.project_id,)).fetchall())
            return list(conn.execute(f"SELECT * FROM {table}").fetchall())
        except sqlite3.Error as exc:
            raise RecoveryError("CANONICAL_READ_FAILED", f"canonical table {table} could not be read", table=table) from exc

    @staticmethod
    def _sort_rows(rows: Sequence[Mapping[str, Any]], keys: Sequence[str]) -> list[dict[str, Any]]:
        return sorted(
            [dict(row) for row in rows],
            key=lambda row: tuple(str(row.get(key, "")) for key in keys),
        )

    def _validate_rows(
        self,
        *,
        send_intents: Sequence[sqlite3.Row],
        leases: Sequence[sqlite3.Row],
        session_reentries: Sequence[sqlite3.Row],
        cursors: Sequence[sqlite3.Row],
        bindings: Sequence[sqlite3.Row],
    ) -> None:
        for row in send_intents:
            row_id = str(row["intent_id"])
            _text(row["intent_id"], table="send_intents", column="intent_id", row_id=row_id)
            status = str(row["status"])
            if status not in _KNOWN_SEND_STATUSES:
                raise RecoveryError("CANONICAL_STATUS_CORRUPT", "send intent status is unsupported", table="send_intents", row_id=row_id)
            _text(row["continuation_id"], table="send_intents", column="continuation_id", row_id=row_id)
            _text(row["packet_digest"], table="send_intents", column="packet_digest", row_id=row_id)
            _text(row["execution_binding_id"], table="send_intents", column="execution_binding_id", row_id=row_id)
            _json_object(row["delivery_evidence_json"], table="send_intents", column="delivery_evidence_json", row_id=row_id)

        for row in leases:
            row_id = str(row["lease_id"])
            status = str(row["status"])
            if status not in _KNOWN_LEASE_STATUSES:
                raise RecoveryError("CANONICAL_STATUS_CORRUPT", "lease status is unsupported", table="leases", row_id=row_id)
            if row["lease_kind"] == "CONTINUATION":
                _text(row["continuation_id"], table="leases", column="continuation_id", row_id=row_id)
                _text(row["packet_digest"], table="leases", column="packet_digest", row_id=row_id)
                _text(row["resource_id"], table="leases", column="resource_id", row_id=row_id)
            try:
                if int(row["generation"]) < 0 or int(row["state_revision"]) < 1:
                    raise ValueError
                _parse_utc(str(row["expires_at"]), field="lease.expires_at")
            except (TypeError, ValueError, RecoveryError) as exc:
                if isinstance(exc, RecoveryError):
                    raise RecoveryError("CANONICAL_LEASE_CORRUPT", "lease expiry is invalid", table="leases", row_id=row_id) from exc
                raise RecoveryError("CANONICAL_LEASE_CORRUPT", "lease generation or revision is invalid", table="leases", row_id=row_id) from exc

        for row in session_reentries:
            row_id = str(row["reentry_id"])
            state = str(row["liveness_state"])
            if state not in _KNOWN_REENTRY_STATES:
                raise RecoveryError("CANONICAL_STATUS_CORRUPT", "session re-entry state is unsupported", table="session_reentries", row_id=row_id)
            _text(row["continuation_id"], table="session_reentries", column="continuation_id", row_id=row_id)
            _text(row["packet_digest"], table="session_reentries", column="packet_digest", row_id=row_id)
            _text(row["execution_binding_id"], table="session_reentries", column="execution_binding_id", row_id=row_id)
            _json_array(row["trace_json"], table="session_reentries", column="trace_json", row_id=row_id)
            if int(row["effect_count"]) < 0 or int(row["binding_generation"]) < 1:
                raise RecoveryError("CANONICAL_REENTRY_CORRUPT", "session re-entry counters are invalid", table="session_reentries", row_id=row_id)
            if state == "REENTRY_CONFIRMED" and int(row["effect_count"]) > 1:
                raise RecoveryError("CANONICAL_EFFECT_COUNT_CORRUPT", "confirmed session re-entry has more than one effect", table="session_reentries", row_id=row_id)

        for row in cursors:
            row_id = str(row["cursor_id"])
            try:
                if int(row["scope_epoch"]) < 1 or int(row["state_revision"]) < 1:
                    raise ValueError
            except (TypeError, ValueError) as exc:
                raise RecoveryError("CANONICAL_CURSOR_CORRUPT", "scope cursor epoch or revision is invalid", table="scope_cursors", row_id=row_id) from exc
            if "explanation_json" in row.keys():
                _json_object(row["explanation_json"], table="scope_cursors", column="explanation_json", row_id=row_id)

        active_bindings: set[tuple[str, str]] = set()
        for row in bindings:
            row_id = str(row["execution_binding_id"])
            status = str(row["status"]).upper()
            if status == "ACTIVE":
                key = (str(row["task_id"]), str(row["project_id"]))
                if key in active_bindings:
                    raise RecoveryError("AMBIGUOUS_ACTIVE_BINDING", "more than one active binding exists for a task", table="execution_bindings", row_id=row_id)
                active_bindings.add(key)
            if int(row["generation"]) < 1:
                raise RecoveryError("CANONICAL_BINDING_CORRUPT", "execution binding generation is invalid", table="execution_bindings", row_id=row_id)

        unresolved_by_continuation: dict[tuple[str, int], list[str]] = {}
        for row in send_intents:
            if str(row["status"]) in (_SEND_ACTIVE | _SEND_UNCERTAIN):
                key = (str(row["continuation_id"]), int(row["scope_epoch"]))
                unresolved_by_continuation.setdefault(key, []).append(str(row["intent_id"]))
        if any(len(set(ids)) > 1 for ids in unresolved_by_continuation.values()):
            raise RecoveryError("AMBIGUOUS_SEND_INTENTS", "multiple unresolved send intents share one continuation identity", table="send_intents")

        leases_by_resource: dict[str, list[str]] = {}
        for row in leases:
            if str(row["lease_kind"]) == "CONTINUATION" and str(row["status"]) in _LEASE_ACTIVE:
                leases_by_resource.setdefault(str(row["resource_id"]), []).append(str(row["lease_id"]))
        if any(len(set(ids)) > 1 for ids in leases_by_resource.values()):
            raise RecoveryError("AMBIGUOUS_ACTIVE_LEASES", "multiple active continuation leases share one resource", table="leases")

    def _snapshot(self) -> CanonicalRecoverySnapshot:
        with self._read_transaction() as conn:
            if not self._has_table(conn, "projects"):
                raise RecoveryError("PM_V2_SCHEMA_MISSING", "canonical projects table is missing", table="projects")
            project = conn.execute(
                "SELECT * FROM projects WHERE project_id = ?", (self.project_id,)
            ).fetchone()
            if project is None:
                raise RecoveryError("PROJECT_NOT_FOUND", "canonical project does not exist", table="projects", row_id=self.project_id)

            cursors = self._rows(conn, "scope_cursors")
            if len(cursors) > 1:
                raise RecoveryError("AMBIGUOUS_SCOPE_CURSOR", "more than one canonical scope cursor exists", table="scope_cursors")
            cursor = cursors[0] if cursors else None
            run_id = str(cursor["run_id"]) if cursor is not None else None
            runs = self._rows(conn, "runs")
            scopes = self._rows(conn, "scopes")
            project_plans = self._rows(conn, "project_plans")
            task_states = self._rows(conn, "task_execution_states")
            bindings = self._rows(conn, "execution_bindings")
            attempts = self._rows(conn, "attempts")
            checkpoints = self._rows(conn, "checkpoints")
            outbox = self._rows(conn, "launch_outbox")
            send_intents = self._rows(conn, "send_intents")
            leases = self._rows(conn, "leases")
            stop_fences = self._rows(conn, "stop_fences")
            session_reentries = self._rows(conn, "session_reentries")
            ci_waits = self._rows(conn, "ci_wait_records")
            retries = self._rows(conn, "transient_retry_records")
            failures = self._rows(conn, "failures")
            attention = self._rows(conn, "attention_items")
            audit_events = self._rows(conn, "audit_events")
            self._validate_rows(
                send_intents=send_intents,
                leases=leases,
                session_reentries=session_reentries,
                cursors=cursors,
                bindings=bindings,
            )

            def cursor_projection(row: sqlite3.Row | None) -> dict[str, Any] | None:
                if row is None:
                    return None
                result = _row_dict(
                    row,
                    (
                        "cursor_id", "project_id", "run_id", "scope", "scope_epoch",
                        "current_milestone_id", "current_task_id", "last_accepted_task_id",
                        "last_accepted_gate", "plan_identity", "plan_version", "state_revision",
                        "disposition", "status", "stop_requested_at", "stop_reason",
                        "scope_selection_explicit",
                    ),
                )
                return result

            canonical: dict[str, Any] = {
                "schema": STARTUP_RECOVERY_SCHEMA,
                "version": STARTUP_RECOVERY_VERSION,
                "project": _row_dict(project, ("project_id", "revision", "updated_at")),
                "cursor": cursor_projection(cursor),
                "plans": self._sort_rows(project_plans, ("plan_version", "plan_digest")),
                "runs": self._sort_rows(runs, ("run_id",)),
                "scopes": self._sort_rows(scopes, ("scope_id",)),
                "task_states": self._sort_rows(task_states, ("task_id",)),
                "bindings": self._sort_rows(bindings, ("task_id", "generation", "execution_binding_id")),
                "attempts": self._sort_rows(attempts, ("attempt_id",)),
                "checkpoints": self._sort_rows(checkpoints, ("checkpoint_id",)),
                "launch_outbox": self._sort_rows(outbox, ("launch_id",)),
                "send_intents": self._sort_rows(
                    (
                        _row_dict(row, (
                            "intent_id", "intent_key", "continuation_id", "packet_digest", "lease_id",
                            "lease_generation", "scope_epoch", "run_id", "task_id", "execution_binding_id",
                            "expected_repo_head_before", "conversation_binding_id", "conversation_binding_proof",
                            "message_digest", "intent_generation", "state_revision", "status",
                            "prepared_at", "updated_at", "physical_attempted_at", "confirmed_at",
                            "acknowledged_at", "delivery_evidence_json", "uncertainty_reason",
                        ))
                        for row in send_intents
                    ),
                    ("continuation_id", "scope_epoch", "intent_id"),
                ),
                "leases": self._sort_rows(
                    (
                        _row_dict(row, (
                            "lease_id", "resource_type", "resource_id", "status", "acquired_at",
                            "expires_at", "fence", "lease_kind", "continuation_id", "packet_digest",
                            "run_id", "scope_epoch", "task_id", "execution_binding_id", "owner_id",
                            "generation", "state_revision", "last_transition_reason", "completed_at",
                            "abandoned_at",
                        ))
                        for row in leases
                    ),
                    ("resource_id", "generation", "lease_id"),
                ),
                "stop_fences": self._sort_rows(stop_fences, ("scope_epoch", "fence_id")),
                "session_reentries": self._sort_rows(
                    (
                        _row_dict(row, (
                            "reentry_id", "continuation_id", "packet_digest", "run_id", "scope_epoch",
                            "task_id", "execution_binding_id", "binding_generation", "canonical_state_revision",
                            "canonical_state_digest", "session_liveness_version", "liveness_state",
                            "selected_channel", "conversation_id", "conversation_binding_proof", "checkpoint_id",
                            "trace_json", "effect_count", "operator_prompt_build_required",
                            "operator_decision_required", "state_revision", "last_reason", "created_at", "updated_at",
                        ))
                        for row in session_reentries
                    ),
                    ("continuation_id", "scope_epoch", "reentry_id"),
                ),
                "ci_waits": self._sort_rows(
                    (
                        _row_dict(row, (
                            "wait_id", "run_id", "task_id", "provider", "workflow", "ci_run_id",
                            "expected_head", "status", "last_observed_status", "last_observed_head",
                            "next_poll_at", "poll_count", "deadline_at", "evidence_digest", "continuation_emitted",
                        ))
                        for row in ci_waits
                    ),
                    ("task_id", "wait_id"),
                ),
                "retries": self._sort_rows(
                    (
                        _row_dict(row, (
                            "retry_request_id", "run_id", "task_id", "operation_id", "fingerprint_digest",
                            "evidence_digest", "generation", "eligible_at", "status", "execution_count",
                            "result_digest",
                        ))
                        for row in retries
                    ),
                    ("task_id", "generation", "retry_request_id"),
                ),
                "failures": self._sort_rows(failures, ("task_id", "failure_id")),
                "attention": self._sort_rows(attention, ("attention_id",)),
                "security_events": self._sort_rows(
                    (
                        _row_dict(row, ("event_id", "event_type", "task_id", "revision"))
                        for row in audit_events
                        if str(row["event_type"]).upper() in {"SECURITY_FREEZE", "DATA_CORRUPTION", "SECURITY_DATA_CORRUPTION"}
                    ),
                    ("revision", "event_id"),
                ),
            }
            revision = int(project["revision"])
            digest = _digest(canonical)
            return CanonicalRecoverySnapshot(self.project_id, revision, digest, canonical)

    @staticmethod
    def _row_list(data: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
        value = data.get(key, [])
        return [item for item in value if isinstance(item, Mapping)]

    @staticmethod
    def _current_cursor(data: Mapping[str, Any]) -> Mapping[str, Any]:
        value = data.get("cursor")
        return value if isinstance(value, Mapping) else {}

    @staticmethod
    def _current_run(data: Mapping[str, Any], cursor: Mapping[str, Any]) -> Mapping[str, Any]:
        run_id = cursor.get("run_id")
        for row in StartupReconciler._row_list(data, "runs"):
            if row.get("run_id") == run_id:
                return row
        return {}

    @staticmethod
    def _current_scope(data: Mapping[str, Any], cursor: Mapping[str, Any]) -> Mapping[str, Any]:
        for row in StartupReconciler._row_list(data, "scopes"):
            if row.get("scope_id") == cursor.get("scope") or row.get("mode") == cursor.get("scope"):
                return row
        return {}

    @staticmethod
    def _projected_status(row: Mapping[str, Any], field: str) -> str:
        value = row.get(field)
        return "" if value is None else str(value).upper()

    def _candidate(self, data: Mapping[str, Any], now: datetime) -> _Candidate:
        cursor = self._current_cursor(data)
        run = self._current_run(data, cursor)
        scope = self._current_scope(data, cursor)
        cursor_status = self._projected_status(cursor, "status")
        cursor_disposition = self._projected_status(cursor, "disposition")
        run_status = self._projected_status(run, "status")
        scope_status = self._projected_status(scope, "status")
        epoch = int(cursor.get("scope_epoch", 0) or 0)

        stop_fences = self._row_list(data, "stop_fences")
        if cursor_status == "STOPPED" or cursor_disposition == "STOPPED" or run_status == "STOPPED" or scope_status == "STOPPED" or any(
            int(row.get("scope_epoch", 0) or 0) == epoch for row in stop_fences
        ):
            return _Candidate(
                RecoveryRule.STOPPED,
                RecoveryAction.STOPPED,
                "STOP_CANONICAL",
                "canonical STOP state or STOP fence remains authoritative after restart",
                {"epoch": epoch},
            )

        security_events = self._row_list(data, "security_events")
        failures = self._row_list(data, "failures")
        attention = self._row_list(data, "attention")
        security_failure = [
            row for row in failures
            if self._projected_status(row, "failure_class") in {"SECURITY", "DATA_CORRUPTION", "SECURITY_DATA_CORRUPTION"}
        ]
        security_attention = [
            row for row in attention
            if self._projected_status(row, "type") in {"SECURITY_FREEZE", "DATA_CORRUPTION", "SECURITY_DATA_CORRUPTION"}
        ]
        if security_events or security_failure or security_attention:
            return _Candidate(
                RecoveryRule.SECURITY_DATA_CORRUPTION_FREEZE,
                RecoveryAction.FREEZE_AND_REQUIRE_RECONCILIATION,
                "SECURITY_OR_DATA_CORRUPTION_FREEZE",
                "canonical security or data-corruption evidence blocks automatic recovery",
                {"security_event_count": len(security_events) + len(security_failure) + len(security_attention)},
            )

        send_intents = self._row_list(data, "send_intents")
        uncertain = [row for row in send_intents if self._projected_status(row, "status") in _SEND_UNCERTAIN or self._projected_status(row, "status") == "PHYSICAL_SEND_ATTEMPTED"]
        if uncertain:
            return _Candidate(
                RecoveryRule.DELIVERY_UNCERTAIN,
                RecoveryAction.RECONCILE_UNCERTAIN_DELIVERY,
                "DELIVERY_UNCERTAIN",
                "durable send delivery is unresolved; startup must reconcile evidence and must not resend blindly",
                {"intent_ids": sorted(str(row.get("intent_id")) for row in uncertain)},
            )

        active_sends = [row for row in send_intents if self._projected_status(row, "status") in _SEND_ACTIVE]
        if active_sends:
            confirmed = [row for row in active_sends if self._projected_status(row, "status") == "SEND_CONFIRMED"]
            action = RecoveryAction.ACK_SEND_INTENT if confirmed else RecoveryAction.RESUME_SEND_INTENT
            reason = "confirmed send evidence awaits durable acknowledgement" if confirmed else "durable send intent has not crossed a terminal acknowledgement and may be resumed only after live revalidation"
            return _Candidate(
                RecoveryRule.ACTIVE_SEND_INTENT,
                action,
                "SEND_ACK_PENDING" if confirmed else "SEND_INTENT_ACTIVE",
                reason,
                {"intent_ids": sorted(str(row.get("intent_id")) for row in active_sends)},
            )

        reentries = self._row_list(data, "session_reentries")
        pending_reentries = [row for row in reentries if self._projected_status(row, "liveness_state") in _REENTRY_PENDING]
        operator_reentries = [row for row in reentries if self._projected_status(row, "liveness_state") == "OPERATOR_CHECKPOINT"]
        blocked_reentries = [row for row in reentries if self._projected_status(row, "liveness_state") in _REENTRY_BLOCKED]
        if pending_reentries:
            return _Candidate(
                RecoveryRule.CONTINUATION_REENTRY,
                RecoveryAction.RESUME_SESSION_REENTRY,
                "CONTINUATION_PENDING",
                "canonical session re-entry remains pending and is reconstructed from its stored packet identity",
                {"reentry_ids": sorted(str(row.get("reentry_id")) for row in pending_reentries)},
            )
        if operator_reentries:
            return _Candidate(
                RecoveryRule.OPERATOR_CHECKPOINT,
                RecoveryAction.OPERATOR_CHECKPOINT,
                "OPERATOR_CHECKPOINT_PENDING",
                "canonical re-entry requires the already-prepared bounded operator checkpoint",
                {"checkpoint_ids": sorted(str(row.get("checkpoint_id")) for row in operator_reentries)},
            )
        if blocked_reentries:
            return _Candidate(
                RecoveryRule.MANUAL_POLICY_CHECKPOINT,
                RecoveryAction.MANUAL_POLICY_CHECKPOINT,
                "REENTRY_BLOCKED",
                "a previous canonical re-entry is blocked or failed; startup must not guess or create a new continuation",
                {"reentry_ids": sorted(str(row.get("reentry_id")) for row in blocked_reentries)},
            )

        leases = [
            row for row in self._row_list(data, "leases")
            if self._projected_status(row, "lease_kind") == "CONTINUATION"
            and self._projected_status(row, "status") in _LEASE_ACTIVE
        ]
        active_leases: list[Mapping[str, Any]] = []
        expired_leases: list[Mapping[str, Any]] = []
        for row in leases:
            expires = _parse_utc(str(row.get("expires_at")), field="lease.expires_at")
            (active_leases if expires > now else expired_leases).append(row)
        if active_leases:
            return _Candidate(
                RecoveryRule.ACTIVE_CONTINUATION_LEASE,
                RecoveryAction.WAIT_FOR_ACTIVE_CONTINUATION_LEASE,
                "ACTIVE_CONTINUATION_LEASE",
                "an unexpired canonical continuation lease is preserved across process death; disappearance alone cannot reclaim it",
                {"lease_ids": sorted(str(row.get("lease_id")) for row in active_leases)},
            )
        if expired_leases:
            return _Candidate(
                RecoveryRule.EXPIRED_ORPHAN_LEASE,
                RecoveryAction.RECLAIM_EXPIRED_CONTINUATION_LEASE,
                "EXPIRED_ORPHAN_LEASE",
                "the canonical continuation lease is expired and may be reclaimed only through NX-026 deterministic rules",
                {"lease_ids": sorted(str(row.get("lease_id")) for row in expired_leases)},
            )

        ci_waits = [
            row for row in self._row_list(data, "ci_waits")
            if self._projected_status(row, "status") in {"QUEUED", "IN_PROGRESS"}
        ]
        if ci_waits:
            return _Candidate(
                RecoveryRule.CI_WAITING,
                RecoveryAction.CI_WAITING,
                "CI_WAITING",
                "durable CI wait state outranks lower-priority task selection and remains waiting after restart",
                {"wait_ids": sorted(str(row.get("wait_id")) for row in ci_waits)},
            )

        retries = [
            row for row in self._row_list(data, "retries")
            if self._projected_status(row, "status") in {"SCHEDULED", "EXECUTING"}
        ]
        repair_failures = [
            row for row in failures
            if self._projected_status(row, "failure_class") in {"PROJECT_REPAIRABLE", "REPAIRABLE", "RETEST", "PROJECT_RETEST"}
        ]
        if retries or repair_failures:
            return _Candidate(
                RecoveryRule.REPAIR_RETEST,
                RecoveryAction.REPAIR_RETEST,
                "REPAIR_OR_RETEST_PENDING",
                "durable repair/retest state outranks a fresh runnable task",
                {"retry_ids": sorted(str(row.get("retry_request_id")) for row in retries)},
            )

        if cursor_disposition == "WAITING_FOR_PLAN" or cursor_status == "WAITING_FOR_PLAN" or run_status == "WAITING_FOR_PLAN":
            return _Candidate(
                RecoveryRule.WAITING_FOR_PLAN,
                RecoveryAction.WAITING_FOR_PLAN,
                "WAITING_FOR_PLAN",
                "canonical workflow is waiting for an approved plan; startup cannot infer or invent one from UI state",
            )

        checkpoints = [
            row for row in self._row_list(data, "checkpoints")
            if self._projected_status(row, "label") in {"MANUAL_CHECKPOINT", "POLICY_CHECKPOINT", "OPERATOR_CHECKPOINT"}
        ]
        if cursor_disposition in {"MANUAL_CHECKPOINT", "POLICY_CHECKPOINT", "OPERATOR_CHECKPOINT"} or checkpoints:
            return _Candidate(
                RecoveryRule.MANUAL_POLICY_CHECKPOINT,
                RecoveryAction.MANUAL_POLICY_CHECKPOINT,
                "MANUAL_POLICY_CHECKPOINT",
                "canonical manual or policy checkpoint remains the next legal transition",
                {"checkpoint_ids": sorted(str(row.get("checkpoint_id")) for row in checkpoints)},
            )

        # Completion is terminal.  Evaluate it before the current-task
        # projection so a stale task row cannot make a completed scope look
        # runnable after a process restart.
        scope_complete = (
            scope_status in {"COMPLETED", "FAILED"}
            or run_status in {"COMPLETED", "FAILED"}
            or cursor_status == "COMPLETED"
            or cursor_disposition == "COMPLETED"
        )
        if scope_complete:
            return _Candidate(
                RecoveryRule.PROJECT_SCOPE_COMPLETE,
                RecoveryAction.PROJECT_SCOPE_COMPLETE,
                "PROJECT_SCOPE_COMPLETE",
                "canonical project or scope completion is terminal and survives restart",
            )

        task_id = cursor.get("current_task_id")
        task_states = self._row_list(data, "task_states")
        state = next((row for row in task_states if row.get("task_id") == task_id), None)
        if task_id and state is not None:
            task_status = self._projected_status(state, "status")
            if task_status == "ACTIVE":
                return _Candidate(
                    RecoveryRule.CURRENT_RUNNABLE_TASK,
                    RecoveryAction.RESUME_CURRENT_TASK,
                    "CURRENT_TASK_IN_PROGRESS",
                    "canonical current task is in progress and can be reconstructed without a new binding",
                    {"task_id": task_id},
                )
            if task_status in {"PENDING", "REVIEW"}:
                return _Candidate(
                    RecoveryRule.CURRENT_RUNNABLE_TASK,
                    RecoveryAction.RUN_CURRENT_TASK if task_status == "PENDING" else RecoveryAction.RESUME_CURRENT_TASK,
                    "CURRENT_TASK_RUNNABLE" if task_status == "PENDING" else "CURRENT_TASK_REVIEW",
                    "canonical current task remains the next runnable task after restart",
                    {"task_id": task_id, "task_status": task_status},
                )
            if task_status in {"COMPLETED", "SKIPPED"}:
                return _Candidate(
                    RecoveryRule.CURRENT_RUNNABLE_TASK,
                    RecoveryAction.ADVANCE_SCOPE,
                    "CURRENT_TASK_ALREADY_TERMINAL",
                    "canonical current task is terminal; scope advancement remains the next workflow action",
                    {"task_id": task_id, "task_status": task_status},
                )

        return _Candidate(
            RecoveryRule.IDLE,
            RecoveryAction.IDLE,
            "NO_RECOVERY_ACTION",
            "no higher-priority canonical recovery condition or runnable task is present",
        )

    def _decision_from_snapshot(self, snapshot: CanonicalRecoverySnapshot, now: datetime) -> RecoveryDecision:
        data = snapshot.data
        cursor = self._current_cursor(data)
        run = self._current_run(data, cursor)
        candidate = self._candidate(data, now)
        selected_index = _PRECEDENCE.index(candidate.rule)
        rejected = {
            rule.value: f"lower precedence than {candidate.rule.value}"
            for rule in _PRECEDENCE[selected_index + 1:]
            if rule != RecoveryRule.IDLE and candidate.rule != RecoveryRule.IDLE
        }
        diagnostics = {
            "canonical_revision": snapshot.canonical_revision,
            "snapshot_digest": snapshot.snapshot_digest,
            "precedence_index": selected_index,
            "canonical_authority": RECOVERY_SCHEMA_OWNER,
            "browser_state_consulted": False,
            "native_state_consulted": False,
            "effects": 0,
            "recovery_precedence_ambiguities": RECOVERY_PRECEDENCE_AMBIGUITIES,
            **dict(candidate.detail),
        }
        trace = (
            "STARTUP_READ_CANONICAL_STATE",
            f"RECOVERY_RULE:{candidate.rule.value}",
            f"NEXT_ACTION:{candidate.action.value}",
        )
        return RecoveryDecision(
            True,
            candidate.rule.value,
            candidate.action.value,
            candidate.reason_code,
            candidate.explanation,
            snapshot.project_id,
            str(cursor.get("run_id")) if cursor.get("run_id") is not None else None,
            str(cursor.get("scope")) if cursor.get("scope") is not None else None,
            int(cursor["scope_epoch"]) if cursor.get("scope_epoch") is not None else None,
            str(cursor.get("current_milestone_id")) if cursor.get("current_milestone_id") is not None else None,
            str(cursor.get("current_task_id")) if cursor.get("current_task_id") is not None else None,
            snapshot.canonical_revision,
            snapshot.snapshot_digest,
            trace,
            diagnostics,
            rejected,
            0,
        )

    @staticmethod
    def _error_digest(project_id: str, error: RecoveryError) -> str:
        return _digest({
            "schema": STARTUP_RECOVERY_SCHEMA,
            "version": STARTUP_RECOVERY_VERSION,
            "project_id": project_id,
            "error_code": error.code,
            "table": error.table,
            "row_id": error.row_id,
        })

    def reconcile(
        self,
        *,
        now: datetime | str | None = None,
        browser_state: Any = None,
        native_state: Any = None,
        local_cache: Any = None,
    ) -> RecoveryDecision:
        """Read canonical state and return one deterministic recovery action.

        ``browser_state``, ``native_state``, and ``local_cache`` are accepted
        only for disagreement diagnostics at the call boundary.  They are
        intentionally not inspected for task, binding, lease, or send
        authority.
        """

        current = _parse_utc(now if now is not None else self.clock(), field="now")
        try:
            snapshot = self._snapshot()
        except RecoveryError as exc:
            digest = self._error_digest(self.project_id, exc)
            return RecoveryDecision(
                False,
                RecoveryRule.SECURITY_DATA_CORRUPTION_FREEZE.value,
                RecoveryAction.FREEZE_AND_REQUIRE_RECONCILIATION.value,
                exc.code,
                "canonical recovery state is unavailable or corrupt; automatic restart recovery is frozen",
                self.project_id,
                None,
                None,
                None,
                None,
                None,
                0,
                digest,
                (
                    "CANONICAL_RECOVERY_READ_FAILED",
                    "NEXT_ACTION:FREEZE_AND_REQUIRE_RECONCILIATION",
                ),
                {
                    "canonical_revision": 0,
                    "snapshot_digest": digest,
                    "canonical_authority": RECOVERY_SCHEMA_OWNER,
                    "browser_state_consulted": False,
                    "native_state_consulted": False,
                    "effects": 0,
                    "error_code": exc.code,
                    "error_table": exc.table,
                    "error_row_id": exc.row_id,
                    "recovery_precedence_ambiguities": 0,
                },
                {},
                0,
            )
        # Deliberately keep the unused observations out of both decision and
        # semantic digest.  This makes stale/malformed local cache irrelevant.
        _ = (browser_state, native_state, local_cache, current)
        return self._decision_from_snapshot(snapshot, current)

    recover = reconcile
    startup_recover = reconcile
    reconcile_startup = reconcile

    def read_canonical_snapshot(self) -> CanonicalRecoverySnapshot:
        """Return the canonical semantic snapshot without making a decision."""

        return self._snapshot()


# Names used by callers that describe the same one-entrypoint reconciler.
StartupRecoveryReconciler = StartupReconciler
RestartRecoveryReconciler = StartupReconciler
CanonicalStartupReconciler = StartupReconciler


def reconcile_startup(
    authority: Any,
    project_id: str | None = None,
    *,
    now: datetime | str | None = None,
    clock: Callable[[], datetime | str] | None = None,
    browser_state: Any = None,
    native_state: Any = None,
    local_cache: Any = None,
) -> RecoveryDecision:
    """Run one bounded startup reconciliation from canonical state."""

    return StartupReconciler(
        authority,
        project_id,
        clock=clock,
        browser_state=browser_state,
        native_state=native_state,
    ).reconcile(
        now=now,
        browser_state=browser_state,
        native_state=native_state,
        local_cache=local_cache,
    )


startup_recover = reconcile_startup
recover_after_restart = reconcile_startup


__all__ = [
    "BROWSER_LOCAL_STATE_IS_RECOVERY_AUTHORITY",
    "CANONICAL_STATE_IS_RECOVERY_AUTHORITY",
    "CanonicalRecoverySnapshot",
    "CanonicalStartupReconciler",
    "RECOVERY_PRECEDENCE_AMBIGUITIES",
    "RECOVERY_PRECEDENCE_TABLE",
    "RECOVERY_SCHEMA_OWNER",
    "RECOVERY_STORAGE_DATABASE",
    "RECOVERY_TRANSACTION_AUTHORITY",
    "RecoveryAction",
    "RecoveryDecision",
    "RecoveryError",
    "RecoveryRule",
    "RestartRecoveryReconciler",
    "recover_after_restart",
    "reconcile_startup",
    "STARTUP_RECONCILER_VERSION_EXPLICIT",
    "STARTUP_RECOVERY_SCHEMA",
    "STARTUP_RECOVERY_VERSION",
    "StartupRecoveryReconciler",
    "StartupReconciler",
    "startup_recover",
    "compute_recovery_digest",
    "semantic_recovery_digest",
]

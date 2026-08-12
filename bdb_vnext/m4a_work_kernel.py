"""M4a build-only WorkItem kernel substrate.

The kernel owns only isolated vNext WorkItem lifecycle state.  A canonical
M3 Task must already exist before a WorkItem can be created.  All lifecycle
mutations in this module pass through :class:`WorkKernelStore`; Browser,
Native, scheduler and executor code have no supported direct-write path.

This is a deliberately small current-state kernel, not an event store.  The
current WorkItem row is authoritative for disposition and state_version;
Runs, Waits and TransitionFacts preserve the bounded causal facts needed for
recovery and inspection.  Outcome and effect certainty stay separate from
WorkItem disposition.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NoReturn

from bdb_shared.evidence import canonical_json_bytes, semantic_digest
from bdb_vnext.control_store import assert_database_path, ensure_identity
from bdb_vnext.composition import VNextLayout
from bdb_vnext.m3c_admission import (
    M3C_PROTOCOL_GENERATION,
    CanonicalVNextAdmissionAuthority,
)


M4A_SCHEMA = "bdb-vnext-m4a-work-kernel-v1"
M4A_STORE_SCHEMA = "bdb-vnext-m4a-control-store-v1"
M4A_QUERY_SCHEMA = "bdb-vnext-m4a-work-query-v1"
M4A_WRITER_ID = "m4a-vnext-work-kernel-writer"
M4A_AUTHORITY_ID = "devmaster.bdb.vnext.work-kernel"
M4A_PROTOCOL_GENERATION = M3C_PROTOCOL_GENERATION
M4A_DATABASE_NAME = "control.db"
M4A_BUSY_TIMEOUT_MS = 250
M4A_MAX_FACT_BYTES = 64 * 1024

WorkDisposition = Literal["READY", "RUNNING", "WAITING", "FINISHED"]
RunStatus = Literal["ACTIVE", "FINISHED"]
RunOutcome = Literal["SUCCEEDED", "FAILED", "CANCELLED"]
EffectCertainty = Literal["NOT_ASSESSED", "CERTAIN", "POSSIBLE", "UNKNOWN"]
WaitStatus = Literal["OPEN", "RESOLVED"]
LeaseState = Literal["ACTIVE", "RELEASED", "EXPIRED"]
ClaimState = Literal["HELD", "RELEASED"]
FailPoint = Literal["before_transaction", "during_transaction", "after_commit"]

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}$")
_REASON = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")
_OWNER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")


class M4aError(RuntimeError):
    """Bounded, machine-readable Work Kernel failure."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def _fail(code: str, message: str, *, details: Mapping[str, Any] | None = None) -> NoReturn:
    raise M4aError(code, message, details=details)


def _text(value: object, *, field: str, pattern: re.Pattern[str] = _ID) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        _fail("invalid_identifier", f"{field} is not a bounded identifier")
    return value


def _reason(value: object, *, field: str = "reason") -> str:
    if not isinstance(value, str) or _REASON.fullmatch(value) is None:
        _fail("invalid_wait_reason", f"{field} is not a bounded wait reason")
    return value


def _owner(value: object, *, field: str = "owner_id") -> str:
    if not isinstance(value, str) or _OWNER.fullmatch(value) is None:
        _fail("invalid_owner", f"{field} is not a bounded owner identifier")
    return value


def _finite_time(value: object, *, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        _fail("invalid_time", f"{field} must be a number")
    result = float(value)
    if result != result or result in {float("inf"), float("-inf")}:
        _fail("invalid_time", f"{field} must be finite")
    return result


def _overlaps(left: Path, right: Path) -> bool:
    left_value = os.path.normcase(os.path.abspath(os.fspath(left)))
    right_value = os.path.normcase(os.path.abspath(os.fspath(right)))
    try:
        return os.path.commonpath((left_value, right_value)) in {left_value, right_value}
    except ValueError:
        return False


@dataclass(frozen=True)
class WorkItem:
    work_id: str
    task_id: str
    kind: str
    disposition: WorkDisposition
    state_version: int
    created_order: int
    updated_order: int
    created_at: float
    updated_at: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": M4A_SCHEMA,
            "work_id": self.work_id,
            "task_id": self.task_id,
            "kind": self.kind,
            "disposition": self.disposition,
            "state_version": self.state_version,
            "created_order": self.created_order,
            "updated_order": self.updated_order,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    work_id: str
    status: RunStatus
    outcome: RunOutcome | None
    effect_certainty: EffectCertainty
    lease_id: str
    fence: int
    started_order: int
    ended_order: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": M4A_SCHEMA,
            "run_id": self.run_id,
            "work_id": self.work_id,
            "status": self.status,
            "outcome": self.outcome,
            "effect_certainty": self.effect_certainty,
            "lease_id": self.lease_id,
            "fence": self.fence,
            "started_order": self.started_order,
            "ended_order": self.ended_order,
        }


@dataclass(frozen=True)
class WaitRecord:
    wait_id: str
    work_id: str
    reason: str
    status: WaitStatus
    created_order: int
    resolved_order: int | None
    resolution: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": M4A_SCHEMA,
            "wait_id": self.wait_id,
            "work_id": self.work_id,
            "reason": self.reason,
            "status": self.status,
            "created_order": self.created_order,
            "resolved_order": self.resolved_order,
            "resolution": self.resolution,
        }


@dataclass(frozen=True)
class LeaseRecord:
    lease_id: str
    work_id: str
    owner_id: str
    fence: int
    state: LeaseState
    acquired_at: float
    expires_at: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": M4A_SCHEMA,
            "lease_id": self.lease_id,
            "work_id": self.work_id,
            "owner_id": self.owner_id,
            "fence": self.fence,
            "state": self.state,
            "acquired_at": self.acquired_at,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class ResourceClaim:
    resource_key: str
    work_id: str
    lease_id: str
    fence: int
    state: ClaimState
    claimed_order: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": M4A_SCHEMA,
            "resource_key": self.resource_key,
            "work_id": self.work_id,
            "lease_id": self.lease_id,
            "fence": self.fence,
            "state": self.state,
            "claimed_order": self.claimed_order,
        }


@dataclass(frozen=True)
class TransitionFact:
    fact_id: str
    work_id: str
    state_version: int
    kind: str
    from_disposition: WorkDisposition | None
    to_disposition: WorkDisposition | None
    causal_digest: str
    payload: Mapping[str, Any]
    created_order: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": M4A_SCHEMA,
            "fact_id": self.fact_id,
            "work_id": self.work_id,
            "state_version": self.state_version,
            "kind": self.kind,
            "from_disposition": self.from_disposition,
            "to_disposition": self.to_disposition,
            "causal_digest": self.causal_digest,
            "payload": dict(self.payload),
            "created_order": self.created_order,
        }


@dataclass(frozen=True)
class WorkItemQuery:
    """Canonical current-state projection; consumers never read raw tables."""

    work: WorkItem
    active_run: RunRecord | None
    last_run: RunRecord | None
    active_wait: WaitRecord | None
    lease: LeaseRecord | None
    resource_claim: ResourceClaim | None
    recent_facts: tuple[TransitionFact, ...]

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": M4A_QUERY_SCHEMA,
            "authority": M4A_AUTHORITY_ID,
            "protocol_generation": M4A_PROTOCOL_GENERATION,
            "work": self.work.as_dict(),
            "active_run": self.active_run.as_dict() if self.active_run else None,
            "last_run": self.last_run.as_dict() if self.last_run else None,
            "active_wait": self.active_wait.as_dict() if self.active_wait else None,
            "lease": self.lease.as_dict() if self.lease else None,
            "resource_claim": self.resource_claim.as_dict() if self.resource_claim else None,
            "recent_facts": [fact.as_dict() for fact in self.recent_facts],
        }
        payload["query_digest"] = semantic_digest(payload)
        return payload


class WorkKernelStore:
    """The sole transactional M4a WorkItem writer for an isolated vNext root."""

    def __init__(
        self,
        root: str | Path,
        *,
        task_authority: CanonicalVNextAdmissionAuthority,
        shadow: bool = False,
        legacy_root: str | Path,
        busy_timeout_ms: int = M4A_BUSY_TIMEOUT_MS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if shadow is not True:
            _fail("shadow_mode_required", "M4a Work Kernel requires explicit shadow=True")
        if not isinstance(task_authority, CanonicalVNextAdmissionAuthority):
            _fail("task_authority_required", "M4a requires the canonical M3 admission authority")
        if not isinstance(busy_timeout_ms, int) or not 1 <= busy_timeout_ms <= 10_000:
            _fail("invalid_busy_timeout", "busy timeout must be bounded")
        self.root = Path(os.path.abspath(Path(root).expanduser()))
        legacy = Path(os.path.abspath(Path(legacy_root).expanduser()))
        if _overlaps(self.root, legacy):
            _fail("foreign_state_overlap", "M4a root overlaps the frozen legacy root")
        layout = VNextLayout.create(self.root)
        try:
            layout.assert_isolated(legacy_runtime_root=legacy)
        except Exception as exc:
            if hasattr(exc, "code"):
                _fail(str(getattr(exc, "code")), str(exc))
            raise
        if Path(task_authority.runtime_root) != self.root:
            _fail("task_authority_mismatch", "M3 authority and M4a store must share the exact vNext root")
        self.task_authority = task_authority
        self.control_root = self.root / "control"
        self.database_path = assert_database_path(self.root, self.control_root / M4A_DATABASE_NAME)
        self.config_path = self.control_root / "m4a-work-kernel.json"
        self._busy_timeout_ms = busy_timeout_ms
        self._clock = clock or time.time
        self._lock = threading.RLock()
        self.control_root.mkdir(parents=True, exist_ok=True)
        try:
            self._connection = sqlite3.connect(
                str(self.database_path),
                timeout=busy_timeout_ms / 1000,
                check_same_thread=False,
                isolation_level=None,
            )
            self._configure()
            ensure_identity(self._connection)
            self._ensure_config()
            self._ensure_schema()
        except M4aError:
            raise
        except (OSError, sqlite3.DatabaseError) as exc:
            _fail("store_open_failed", "M4a Work Kernel store could not be opened")

    @classmethod
    def open(
        cls,
        root: str | Path,
        *,
        task_authority: CanonicalVNextAdmissionAuthority,
        legacy_root: str | Path,
        **kwargs: Any,
    ) -> "WorkKernelStore":
        return cls(
            root,
            task_authority=task_authority,
            shadow=True,
            legacy_root=legacy_root,
            **kwargs,
        )

    @property
    def writer_id(self) -> str:
        return M4A_WRITER_ID

    def _configure(self) -> None:
        try:
            self._connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
            self._connection.execute("PRAGMA foreign_keys=ON")
            mode = str(self._connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
            self._connection.execute("PRAGMA synchronous=FULL")
        except sqlite3.DatabaseError as exc:
            _fail("store_settings_failed", "M4a SQLite settings could not be verified")
        if mode != "wal":
            _fail("wal_unavailable", "M4a Work Kernel requires WAL journaling")

    def _ensure_config(self) -> None:
        expected = {
            "schema": M4A_STORE_SCHEMA,
            "authority_id": M4A_AUTHORITY_ID,
            "writer_id": M4A_WRITER_ID,
            "protocol_generation": M4A_PROTOCOL_GENERATION,
            "mode": "SHADOW_ONLY",
            "production_writer": False,
            "legacy_import": False,
            "legacy_dual_write": False,
        }
        raw = canonical_json_bytes(expected)
        if self.config_path.exists():
            try:
                actual = self.config_path.read_bytes()
            except OSError as exc:
                _fail("store_config_unavailable", "M4a config could not be read")
            if actual != raw:
                _fail("store_config_mismatch", "M4a store identity differs")
            return
        try:
            self.config_path.write_bytes(raw)
        except OSError as exc:
            _fail("store_config_write_failed", "M4a config could not be written")

    def _ensure_schema(self) -> None:
        self._migrate_legacy_disposition_schema()
        sql = """
            CREATE TABLE IF NOT EXISTS m4a_sequence (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                value INTEGER NOT NULL CHECK(value >= 0)
            );
            INSERT OR IGNORE INTO m4a_sequence(id, value) VALUES (1, 0);
            CREATE TABLE IF NOT EXISTS m4a_work_items (
                work_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                disposition TEXT NOT NULL CHECK(disposition IN ('READY','RUNNING','WAITING','FINISHED')),
                state_version INTEGER NOT NULL CHECK(state_version >= 0),
                created_order INTEGER NOT NULL,
                updated_order INTEGER NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS m4a_runs (
                run_id TEXT PRIMARY KEY,
                work_id TEXT NOT NULL REFERENCES m4a_work_items(work_id),
                status TEXT NOT NULL CHECK(status IN ('ACTIVE','FINISHED')),
                outcome TEXT CHECK(outcome IN ('SUCCEEDED','FAILED','CANCELLED')),
                effect_certainty TEXT NOT NULL CHECK(effect_certainty IN ('NOT_ASSESSED','CERTAIN','POSSIBLE','UNKNOWN')),
                lease_id TEXT NOT NULL,
                fence INTEGER NOT NULL CHECK(fence > 0),
                started_order INTEGER NOT NULL,
                ended_order INTEGER,
                UNIQUE(work_id, run_id)
            );
            CREATE TABLE IF NOT EXISTS m4a_waits (
                wait_id TEXT PRIMARY KEY,
                work_id TEXT NOT NULL REFERENCES m4a_work_items(work_id),
                reason TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('OPEN','RESOLVED')),
                created_order INTEGER NOT NULL,
                resolved_order INTEGER,
                resolution TEXT,
                UNIQUE(work_id, wait_id)
            );
            CREATE TABLE IF NOT EXISTS m4a_leases (
                work_id TEXT PRIMARY KEY REFERENCES m4a_work_items(work_id),
                lease_id TEXT NOT NULL UNIQUE,
                owner_id TEXT NOT NULL,
                fence INTEGER NOT NULL CHECK(fence > 0),
                state TEXT NOT NULL CHECK(state IN ('ACTIVE','RELEASED')),
                acquired_at REAL NOT NULL,
                expires_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS m4a_resource_claims (
                resource_key TEXT PRIMARY KEY,
                work_id TEXT NOT NULL REFERENCES m4a_work_items(work_id),
                lease_id TEXT NOT NULL,
                fence INTEGER NOT NULL CHECK(fence > 0),
                state TEXT NOT NULL CHECK(state IN ('HELD','RELEASED')),
                claimed_order INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS m4a_transition_facts (
                fact_id TEXT PRIMARY KEY,
                work_id TEXT NOT NULL REFERENCES m4a_work_items(work_id),
                state_version INTEGER NOT NULL,
                kind TEXT NOT NULL,
                from_disposition TEXT,
                to_disposition TEXT,
                causal_digest TEXT NOT NULL,
                payload BLOB NOT NULL,
                created_order INTEGER NOT NULL,
                UNIQUE(work_id, state_version)
            );
            CREATE INDEX IF NOT EXISTS m4a_runs_by_work ON m4a_runs(work_id, started_order);
            CREATE INDEX IF NOT EXISTS m4a_waits_by_work ON m4a_waits(work_id, created_order);
            CREATE INDEX IF NOT EXISTS m4a_facts_by_work ON m4a_transition_facts(work_id, state_version);
            CREATE UNIQUE INDEX IF NOT EXISTS m4a_one_active_run_per_work
                ON m4a_runs(work_id) WHERE status='ACTIVE';
            CREATE UNIQUE INDEX IF NOT EXISTS m4a_one_open_wait_per_work
                ON m4a_waits(work_id) WHERE status='OPEN';
            CREATE UNIQUE INDEX IF NOT EXISTS m4a_one_held_resource_per_work
                ON m4a_resource_claims(work_id) WHERE state='HELD';
        """
        try:
            with self._lock:
                self._connection.executescript(sql)
        except sqlite3.DatabaseError as exc:
            _fail("schema_init_failed", "M4a Work Kernel schema could not be initialized")

    def _migrate_legacy_disposition_schema(self) -> None:
        """Map the completed M4a TERMINAL vocabulary without guessing state."""

        row = self._connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='m4a_work_items'"
        ).fetchone()
        sql = "" if row is None or row[0] is None else str(row[0]).upper()
        if "TERMINAL" not in sql:
            return
        try:
            rows = self._connection.execute(
                "SELECT work_id,disposition FROM m4a_work_items ORDER BY work_id"
            ).fetchall()
            for work_id, disposition in rows:
                if str(disposition) != "CANCELLED":
                    continue
                try:
                    outcome_row = self._connection.execute(
                        "SELECT outcome FROM m4a_runs WHERE work_id=? ORDER BY started_order DESC LIMIT 1",
                        (work_id,),
                    ).fetchone()
                except sqlite3.DatabaseError:
                    outcome_row = None
                if outcome_row is None or str(outcome_row[0]) != "CANCELLED":
                    _fail(
                        "lifecycle_migration_ambiguity",
                        "legacy CANCELLED disposition has no exact terminal outcome",
                        details={"work_id": str(work_id)},
                    )
            run_schema_row = self._connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='m4a_runs'"
            ).fetchone()
            run_sql = "" if run_schema_row is None or run_schema_row[0] is None else str(run_schema_row[0]).upper()
            migrate_runs = "ABORTED" in run_sql or "UNKNOWN" in run_sql
            if migrate_runs:
                invalid_run = self._connection.execute(
                    "SELECT run_id,outcome FROM m4a_runs WHERE outcome IS NOT NULL AND outcome NOT IN ('SUCCEEDED','FAILED','CANCELLED') LIMIT 1"
                ).fetchone()
                if invalid_run is not None:
                    _fail(
                        "lifecycle_migration_ambiguity",
                        "legacy Run outcome cannot be represented by the frozen terminal outcome axis",
                        details={"run_id": str(invalid_run[0]), "outcome": str(invalid_run[1])},
                    )
            self._connection.execute("PRAGMA foreign_keys=OFF")
            self._connection.execute("BEGIN IMMEDIATE")
            self._connection.execute(
                "CREATE TABLE m4a_work_items_n1 ("
                "work_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, kind TEXT NOT NULL,"
                "disposition TEXT NOT NULL CHECK(disposition IN ('READY','RUNNING','WAITING','FINISHED')),"
                "state_version INTEGER NOT NULL CHECK(state_version >= 0),"
                "created_order INTEGER NOT NULL, updated_order INTEGER NOT NULL,"
                "created_at REAL NOT NULL, updated_at REAL NOT NULL)"
            )
            self._connection.execute(
                "INSERT INTO m4a_work_items_n1(work_id,task_id,kind,disposition,state_version,created_order,updated_order,created_at,updated_at) "
                "SELECT work_id,task_id,kind,CASE WHEN disposition IN ('TERMINAL','CANCELLED') THEN 'FINISHED' ELSE disposition END,"
                "state_version,created_order,updated_order,created_at,updated_at FROM m4a_work_items"
            )
            self._connection.execute("DROP TABLE m4a_work_items")
            self._connection.execute("ALTER TABLE m4a_work_items_n1 RENAME TO m4a_work_items")
            if migrate_runs:
                self._connection.execute(
                    "CREATE TABLE m4a_runs_n1 ("
                    "run_id TEXT PRIMARY KEY, work_id TEXT NOT NULL REFERENCES m4a_work_items(work_id),"
                    "status TEXT NOT NULL CHECK(status IN ('ACTIVE','FINISHED')),"
                    "outcome TEXT CHECK(outcome IN ('SUCCEEDED','FAILED','CANCELLED')),"
                    "effect_certainty TEXT NOT NULL CHECK(effect_certainty IN ('NOT_ASSESSED','CERTAIN','POSSIBLE','UNKNOWN')),"
                    "lease_id TEXT NOT NULL, fence INTEGER NOT NULL CHECK(fence > 0),"
                    "started_order INTEGER NOT NULL, ended_order INTEGER, UNIQUE(work_id, run_id))"
                )
                self._connection.execute(
                    "INSERT INTO m4a_runs_n1 SELECT run_id,work_id,status,outcome,effect_certainty,lease_id,fence,started_order,ended_order FROM m4a_runs"
                )
                self._connection.execute("DROP TABLE m4a_runs")
                self._connection.execute("ALTER TABLE m4a_runs_n1 RENAME TO m4a_runs")
            if self._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='m4a_transition_facts'"
            ).fetchone() is not None:
                self._connection.execute(
                    "UPDATE m4a_transition_facts SET "
                    "from_disposition=CASE WHEN from_disposition IN ('TERMINAL','CANCELLED') THEN 'FINISHED' ELSE from_disposition END, "
                    "to_disposition=CASE WHEN to_disposition IN ('TERMINAL','CANCELLED') THEN 'FINISHED' ELSE to_disposition END"
                )
            self._connection.commit()
        except M4aError:
            if self._connection.in_transaction:
                self._connection.rollback()
            self._connection.execute("PRAGMA foreign_keys=ON")
            raise
        except sqlite3.DatabaseError as exc:
            if self._connection.in_transaction:
                self._connection.rollback()
            self._connection.execute("PRAGMA foreign_keys=ON")
            _fail("lifecycle_migration_failed", "legacy WorkItem disposition migration failed")
        finally:
            try:
                self._connection.execute("PRAGMA foreign_keys=ON")
            except sqlite3.DatabaseError:
                pass

    def _now(self, value: float | None = None) -> float:
        return _finite_time(self._clock() if value is None else value, field="now")

    def _map_sqlite(self, exc: sqlite3.DatabaseError, *, action: str, write: bool = True) -> NoReturn:
        message = str(exc).lower()
        if "busy" in message or "locked" in message:
            _fail("database_busy", f"M4a database is busy during {action}")
        if "constraint" in message or "unique" in message or "foreign key" in message:
            _fail("storage_conflict", f"M4a database constraint rejected {action}")
        code = "database_write_failed" if write else "database_read_failed"
        _fail(code, f"M4a database failed during {action}")

    def _write(self, operation: Callable[[], Any], *, failpoint: FailPoint | None = None) -> Any:
        if failpoint == "before_transaction":
            _fail("simulated_crash_before_transaction", "fault injected before M4a transaction")
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
            except sqlite3.DatabaseError as exc:
                self._map_sqlite(exc, action="transaction begin")
            committed = False
            try:
                result = operation()
                if failpoint == "during_transaction":
                    _fail("simulated_crash_during_transaction", "fault injected during M4a transaction")
                self._connection.commit()
                committed = True
            except M4aError:
                if not committed and self._connection.in_transaction:
                    self._connection.rollback()
                raise
            except sqlite3.DatabaseError as exc:
                if self._connection.in_transaction:
                    self._connection.rollback()
                self._map_sqlite(exc, action="write")
            if failpoint == "after_commit":
                _fail("simulated_response_loss_after_commit", "fault injected after M4a commit")
            return result

    def _next_order(self) -> int:
        row = self._connection.execute("SELECT value FROM m4a_sequence WHERE id=1").fetchone()
        if row is None:
            _fail("sequence_corrupt", "M4a sequence row is missing")
        value = int(row[0]) + 1
        self._connection.execute("UPDATE m4a_sequence SET value=? WHERE id=1", (value,))
        return value

    def _validate_task(self, task_id: str) -> None:
        _text(task_id, field="task_id")
        try:
            task = self.task_authority.task(task_id)
        except Exception as exc:
            if isinstance(exc, M4aError):
                raise
            _fail("task_authority_unavailable", "canonical M3 Task authority could not be queried")
        if task is None:
            _fail("task_not_found", "WorkItem must bind to an accepted canonical M3 Task")

    def _work_row(self, work_id: str) -> tuple[Any, ...] | None:
        return self._connection.execute(
            "SELECT work_id,task_id,kind,disposition,state_version,created_order,updated_order,created_at,updated_at "
            "FROM m4a_work_items WHERE work_id=?",
            (work_id,),
        ).fetchone()

    def _require_work(self, work_id: str) -> tuple[Any, ...]:
        row = self._work_row(work_id)
        if row is None:
            _fail("work_not_found", f"WorkItem does not exist: {work_id}")
        return row

    @staticmethod
    def _work(row: tuple[Any, ...]) -> WorkItem:
        return WorkItem(
            str(row[0]),
            str(row[1]),
            str(row[2]),
            str(row[3]),  # type: ignore[arg-type]
            int(row[4]),
            int(row[5]),
            int(row[6]),
            float(row[7]),
            float(row[8]),
        )

    @staticmethod
    def _run(row: tuple[Any, ...]) -> RunRecord:
        return RunRecord(
            str(row[0]),
            str(row[1]),
            str(row[2]),  # type: ignore[arg-type]
            None if row[3] is None else str(row[3]),  # type: ignore[arg-type]
            str(row[4]),  # type: ignore[arg-type]
            str(row[5]),
            int(row[6]),
            int(row[7]),
            None if row[8] is None else int(row[8]),
        )

    @staticmethod
    def _wait(row: tuple[Any, ...]) -> WaitRecord:
        return WaitRecord(
            str(row[0]),
            str(row[1]),
            str(row[2]),
            str(row[3]),  # type: ignore[arg-type]
            int(row[4]),
            None if row[5] is None else int(row[5]),
            None if row[6] is None else str(row[6]),
        )

    @staticmethod
    def _lease(row: tuple[Any, ...], *, now: float) -> LeaseRecord:
        state = "RELEASED" if str(row[4]) != "ACTIVE" else ("EXPIRED" if float(row[6]) <= now else "ACTIVE")
        return LeaseRecord(str(row[1]), str(row[0]), str(row[2]), int(row[3]), state, float(row[5]), float(row[6]))  # type: ignore[arg-type]

    @staticmethod
    def _claim(row: tuple[Any, ...]) -> ResourceClaim:
        return ResourceClaim(str(row[0]), str(row[1]), str(row[2]), int(row[3]), str(row[4]), int(row[5]))  # type: ignore[arg-type]

    @staticmethod
    def _fact(row: tuple[Any, ...]) -> TransitionFact:
        payload = json.loads(bytes(row[7]).decode("utf-8"))
        if not isinstance(payload, Mapping):
            _fail("fact_corrupt", "transition fact payload is not an object")
        return TransitionFact(
            str(row[0]),
            str(row[1]),
            int(row[2]),
            str(row[3]),
            None if row[4] is None else str(row[4]),  # type: ignore[arg-type]
            None if row[5] is None else str(row[5]),  # type: ignore[arg-type]
            str(row[6]),
            dict(payload),
            int(row[8]),
        )

    def _append_fact(
        self,
        *,
        work_id: str,
        state_version: int,
        kind: str,
        from_disposition: str | None,
        to_disposition: str | None,
        payload: Mapping[str, Any],
        created_order: int,
    ) -> None:
        payload_bytes = canonical_json_bytes(dict(payload))
        if len(payload_bytes) > M4A_MAX_FACT_BYTES:
            _fail("fact_too_large", "transition fact exceeds the bounded size")
        causal_digest = semantic_digest({"schema": M4A_SCHEMA, "kind": kind, "payload": dict(payload)})
        fact_id = semantic_digest(
            {
                "schema": M4A_SCHEMA,
                "work_id": work_id,
                "state_version": state_version,
                "kind": kind,
                "causal_digest": causal_digest,
            }
        )
        self._connection.execute(
            "INSERT INTO m4a_transition_facts(fact_id,work_id,state_version,kind,from_disposition,to_disposition,causal_digest,payload,created_order) VALUES (?,?,?,?,?,?,?,?,?)",
            (fact_id, work_id, state_version, kind, from_disposition, to_disposition, causal_digest, payload_bytes, created_order),
        )

    def _bump_work(self, row: tuple[Any, ...], *, disposition: WorkDisposition, order: int, now: float) -> int:
        version = int(row[4]) + 1
        cursor = self._connection.execute(
            "UPDATE m4a_work_items SET disposition=?,state_version=?,updated_order=?,updated_at=? WHERE work_id=? AND state_version=?",
            (disposition, version, order, now, row[0], row[4]),
        )
        if cursor.rowcount != 1:
            _fail("state_version_conflict", "WorkItem state changed during the transition")
        return version

    def _lease_row(self, work_id: str) -> tuple[Any, ...] | None:
        return self._connection.execute(
            "SELECT work_id,lease_id,owner_id,fence,state,acquired_at,expires_at FROM m4a_leases WHERE work_id=?",
            (work_id,),
        ).fetchone()

    def _require_lease(self, work_id: str, lease_id: str, fence: int, *, now: float) -> tuple[Any, ...]:
        _text(lease_id, field="lease_id")
        if not isinstance(fence, int) or isinstance(fence, bool) or fence < 1:
            _fail("invalid_fence", "fence must be a positive integer")
        row = self._lease_row(work_id)
        if row is None:
            _fail("lease_missing", "WorkItem has no current lease")
        if str(row[1]) != lease_id:
            _fail("stale_lease", "worker lease is no longer current")
        if int(row[3]) != fence:
            _fail("stale_fence", "worker fence is no longer current")
        if str(row[4]) != "ACTIVE":
            _fail("lease_released", "worker lease is not active")
        if float(row[6]) <= now:
            _fail("lease_expired", "worker lease has expired")
        return row

    def assert_current_lease(
        self,
        work_id: str,
        lease_id: str,
        fence: int,
        *,
        now: float | None = None,
    ) -> LeaseRecord:
        """Read-only ownership proof for typed effect adapters."""

        work_id = _text(work_id, field="work_id")
        timestamp = self._now(now)
        with self._lock:
            row = self._require_lease(work_id, lease_id, fence, now=timestamp)
            return self._lease(row, now=timestamp)

    @staticmethod
    def _expect_version(row: tuple[Any, ...], expected_state_version: int) -> None:
        if not isinstance(expected_state_version, int) or isinstance(expected_state_version, bool) or expected_state_version < 0:
            _fail("invalid_state_version", "expected state_version must be a non-negative integer")
        if int(row[4]) != expected_state_version:
            _fail(
                "stale_state_version",
                "WorkItem state_version is stale",
                details={"expected": expected_state_version, "current": int(row[4])},
            )

    def create_work_item(
        self,
        work_id: str,
        task_id: str,
        *,
        kind: str = "default",
        now: float | None = None,
        failpoint: FailPoint | None = None,
    ) -> WorkItem:
        work_id = _text(work_id, field="work_id")
        task_id = _text(task_id, field="task_id")
        kind = _text(kind, field="work_kind")
        self._validate_task(task_id)
        timestamp = self._now(now)

        def operation() -> WorkItem:
            existing = self._work_row(work_id)
            if existing is not None:
                if str(existing[1]) == task_id and str(existing[2]) == kind:
                    return self._work(existing)
                _fail("work_conflict", "work_id is already bound to another Task or kind")
            order = self._next_order()
            self._connection.execute(
                "INSERT INTO m4a_work_items(work_id,task_id,kind,disposition,state_version,created_order,updated_order,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (work_id, task_id, kind, "READY", 0, order, order, timestamp, timestamp),
            )
            self._append_fact(
                work_id=work_id,
                state_version=0,
                kind="work_created",
                from_disposition=None,
                to_disposition="READY",
                payload={"task_id": task_id, "kind": kind},
                created_order=order,
            )
            return self._work(self._require_work(work_id))

        return self._write(operation, failpoint=failpoint)

    def acquire_lease(
        self,
        work_id: str,
        lease_id: str,
        owner_id: str,
        *,
        ttl_seconds: float = 30.0,
        now: float | None = None,
        failpoint: FailPoint | None = None,
    ) -> LeaseRecord:
        work_id = _text(work_id, field="work_id")
        lease_id = _text(lease_id, field="lease_id")
        owner_id = _owner(owner_id)
        ttl = _finite_time(ttl_seconds, field="ttl_seconds")
        if ttl <= 0 or ttl > 86_400:
            _fail("invalid_lease_ttl", "lease TTL must be positive and bounded")
        timestamp = self._now(now)

        def operation() -> LeaseRecord:
            self._require_work(work_id)
            current = self._lease_row(work_id)
            if current is not None and str(current[1]) == lease_id and str(current[2]) == owner_id and str(current[4]) == "ACTIVE" and float(current[6]) > timestamp:
                return self._lease(current, now=timestamp)
            if current is not None and str(current[4]) == "ACTIVE" and float(current[6]) > timestamp:
                _fail("lease_conflict", "WorkItem already has a valid lease")
            fence = 1 if current is None else int(current[3]) + 1
            duplicate = self._connection.execute("SELECT work_id FROM m4a_leases WHERE lease_id=?", (lease_id,)).fetchone()
            if duplicate is not None and str(duplicate[0]) != work_id:
                _fail("lease_id_conflict", "lease_id is bound to another WorkItem")
            if current is None:
                self._connection.execute(
                    "INSERT INTO m4a_leases(work_id,lease_id,owner_id,fence,state,acquired_at,expires_at) VALUES (?,?,?,?,?,?,?)",
                    (work_id, lease_id, owner_id, fence, "ACTIVE", timestamp, timestamp + ttl),
                )
            else:
                self._connection.execute(
                    "UPDATE m4a_leases SET lease_id=?,owner_id=?,fence=?,state='ACTIVE',acquired_at=?,expires_at=? WHERE work_id=?",
                    (lease_id, owner_id, fence, timestamp, timestamp + ttl, work_id),
                )
                # A lease handoff preserves the exclusive resource reservation
                # while transferring its authority to the new fence.  Expiry
                # alone must never make an ambiguous resource silently free.
                self._connection.execute(
                    "UPDATE m4a_resource_claims SET lease_id=?,fence=? WHERE work_id=? AND state='HELD'",
                    (lease_id, fence, work_id),
                )
            row = self._lease_row(work_id)
            assert row is not None
            return self._lease(row, now=timestamp)

        return self._write(operation, failpoint=failpoint)

    def release_lease(
        self,
        work_id: str,
        lease_id: str,
        fence: int,
        *,
        now: float | None = None,
        failpoint: FailPoint | None = None,
    ) -> LeaseRecord:
        work_id = _text(work_id, field="work_id")
        timestamp = self._now(now)

        def operation() -> LeaseRecord:
            row = self._require_lease(work_id, lease_id, fence, now=timestamp)
            held = self._connection.execute(
                "SELECT resource_key FROM m4a_resource_claims WHERE work_id=? AND state='HELD'",
                (work_id,),
            ).fetchone()
            if held is not None:
                _fail(
                    "resource_claim_held",
                    "WorkItem lease cannot be released while its resource claim is held",
                    details={"resource_key": str(held[0])},
                )
            self._connection.execute("UPDATE m4a_leases SET state='RELEASED' WHERE work_id=?", (work_id,))
            result = self._lease(self._lease_row(work_id), now=timestamp)  # type: ignore[arg-type]
            return result

        return self._write(operation, failpoint=failpoint)

    def claim_resource(
        self,
        work_id: str,
        resource_key: str,
        lease_id: str,
        fence: int,
        *,
        now: float | None = None,
        failpoint: FailPoint | None = None,
    ) -> ResourceClaim:
        work_id = _text(work_id, field="work_id")
        resource_key = _text(resource_key, field="resource_key")
        timestamp = self._now(now)

        def operation() -> ResourceClaim:
            work = self._require_work(work_id)
            if str(work[3]) == "FINISHED":
                _fail("invalid_transition", "terminal WorkItem cannot claim a resource")
            self._require_lease(work_id, lease_id, fence, now=timestamp)
            work_claim = self._connection.execute(
                "SELECT resource_key,work_id,lease_id,fence,state,claimed_order FROM m4a_resource_claims "
                "WHERE work_id=? AND state='HELD'",
                (work_id,),
            ).fetchone()
            if work_claim is not None:
                if (
                    str(work_claim[0]) == resource_key
                    and str(work_claim[2]) == lease_id
                    and int(work_claim[3]) == fence
                ):
                    return self._claim(work_claim)
                _fail("work_resource_conflict", "WorkItem already holds another resource")
            current = self._connection.execute(
                "SELECT resource_key,work_id,lease_id,fence,state,claimed_order FROM m4a_resource_claims WHERE resource_key=?",
                (resource_key,),
            ).fetchone()
            if current is not None and str(current[4]) == "HELD":
                if str(current[1]) == work_id and str(current[2]) == lease_id and int(current[3]) == fence:
                    return self._claim(current)
                _fail("resource_conflict", "resource is held by another current WorkItem lease")
            order = self._next_order()
            if current is None:
                self._connection.execute(
                    "INSERT INTO m4a_resource_claims(resource_key,work_id,lease_id,fence,state,claimed_order) VALUES (?,?,?,?,?,?)",
                    (resource_key, work_id, lease_id, fence, "HELD", order),
                )
            else:
                self._connection.execute(
                    "UPDATE m4a_resource_claims SET work_id=?,lease_id=?,fence=?,state='HELD',claimed_order=? WHERE resource_key=?",
                    (work_id, lease_id, fence, order, resource_key),
                )
            return self._claim(self._connection.execute("SELECT resource_key,work_id,lease_id,fence,state,claimed_order FROM m4a_resource_claims WHERE resource_key=?", (resource_key,)).fetchone())  # type: ignore[arg-type]

        return self._write(operation, failpoint=failpoint)

    def release_resource(
        self,
        work_id: str,
        resource_key: str,
        lease_id: str,
        fence: int,
        *,
        now: float | None = None,
        failpoint: FailPoint | None = None,
    ) -> ResourceClaim:
        work_id = _text(work_id, field="work_id")
        resource_key = _text(resource_key, field="resource_key")
        timestamp = self._now(now)

        def operation() -> ResourceClaim:
            self._require_lease(work_id, lease_id, fence, now=timestamp)
            row = self._connection.execute("SELECT resource_key,work_id,lease_id,fence,state,claimed_order FROM m4a_resource_claims WHERE resource_key=?", (resource_key,)).fetchone()
            if row is None:
                _fail("resource_missing", "resource claim does not exist")
            if str(row[1]) != work_id or str(row[2]) != lease_id or int(row[3]) != fence:
                _fail("stale_resource_claim", "resource claim is no longer current")
            if str(row[4]) == "RELEASED":
                return self._claim(row)
            active = self._connection.execute(
                "SELECT lease_id,fence FROM m4a_runs WHERE work_id=? AND status='ACTIVE'",
                (work_id,),
            ).fetchone()
            if active is not None and (str(active[0]) != lease_id or int(active[1]) != fence):
                _fail(
                    "resource_reconciliation_required",
                    "resource cannot be released while an older fenced Run remains active",
                )
            self._connection.execute("UPDATE m4a_resource_claims SET state='RELEASED' WHERE resource_key=?", (resource_key,))
            return self._claim(self._connection.execute("SELECT resource_key,work_id,lease_id,fence,state,claimed_order FROM m4a_resource_claims WHERE resource_key=?", (resource_key,)).fetchone())  # type: ignore[arg-type]

        return self._write(operation, failpoint=failpoint)

    def start_run(
        self,
        work_id: str,
        run_id: str,
        lease_id: str,
        fence: int,
        expected_state_version: int,
        *,
        now: float | None = None,
        failpoint: FailPoint | None = None,
    ) -> RunRecord:
        work_id = _text(work_id, field="work_id")
        run_id = _text(run_id, field="run_id")
        timestamp = self._now(now)

        def operation() -> RunRecord:
            existing = self._connection.execute("SELECT run_id,work_id,status,outcome,effect_certainty,lease_id,fence,started_order,ended_order FROM m4a_runs WHERE run_id=?", (run_id,)).fetchone()
            if existing is not None:
                if str(existing[1]) != work_id:
                    _fail("run_id_conflict", "run_id is bound to another WorkItem")
                if str(existing[5]) == lease_id and int(existing[6]) == fence:
                    return self._run(existing)
                _fail("duplicate_run", "run_id already exists with a different owner")
            row = self._require_work(work_id)
            self._expect_version(row, expected_state_version)
            if str(row[3]) != "READY":
                _fail("invalid_transition", "only READY WorkItems can start a Run")
            self._require_lease(work_id, lease_id, fence, now=timestamp)
            active = self._connection.execute("SELECT run_id FROM m4a_runs WHERE work_id=? AND status='ACTIVE'", (work_id,)).fetchone()
            if active is not None:
                _fail("active_run_conflict", "WorkItem already has an active Run")
            order = self._next_order()
            self._connection.execute(
                "INSERT INTO m4a_runs(run_id,work_id,status,outcome,effect_certainty,lease_id,fence,started_order,ended_order) VALUES (?,?,?,?,?,?,?,?,NULL)",
                (run_id, work_id, "ACTIVE", None, "NOT_ASSESSED", lease_id, fence, order),
            )
            version = self._bump_work(row, disposition="RUNNING", order=order, now=timestamp)
            self._append_fact(work_id=work_id, state_version=version, kind="run_started", from_disposition="READY", to_disposition="RUNNING", payload={"run_id": run_id, "lease_id": lease_id, "fence": fence}, created_order=order)
            return self._run(self._connection.execute("SELECT run_id,work_id,status,outcome,effect_certainty,lease_id,fence,started_order,ended_order FROM m4a_runs WHERE run_id=?", (run_id,)).fetchone())  # type: ignore[arg-type]

        return self._write(operation, failpoint=failpoint)

    def enter_wait(
        self,
        work_id: str,
        wait_id: str,
        reason: str,
        lease_id: str,
        fence: int,
        expected_state_version: int,
        *,
        now: float | None = None,
        failpoint: FailPoint | None = None,
    ) -> WaitRecord:
        work_id = _text(work_id, field="work_id")
        wait_id = _text(wait_id, field="wait_id")
        reason = _reason(reason)
        timestamp = self._now(now)

        def operation() -> WaitRecord:
            existing = self._connection.execute("SELECT wait_id,work_id,reason,status,created_order,resolved_order,resolution FROM m4a_waits WHERE wait_id=?", (wait_id,)).fetchone()
            if existing is not None:
                if str(existing[1]) != work_id or str(existing[2]) != reason:
                    _fail("wait_id_conflict", "wait_id is bound to another WorkItem or reason")
                return self._wait(existing)
            row = self._require_work(work_id)
            self._expect_version(row, expected_state_version)
            if str(row[3]) != "RUNNING":
                _fail("invalid_transition", "only RUNNING WorkItems can enter a Wait")
            self._require_lease(work_id, lease_id, fence, now=timestamp)
            active = self._connection.execute(
                "SELECT run_id,lease_id,fence FROM m4a_runs WHERE work_id=? AND status='ACTIVE'",
                (work_id,),
            ).fetchone()
            if active is None:
                _fail("active_run_missing", "RUNNING WorkItem has no active Run")
            if str(active[1]) != lease_id or int(active[2]) != fence:
                _fail("run_ownership_mismatch", "current lease/fence does not own the active Run")
            order = self._next_order()
            self._connection.execute(
                "INSERT INTO m4a_waits(wait_id,work_id,reason,status,created_order,resolved_order,resolution) VALUES (?,?,?,?,?,NULL,NULL)",
                (wait_id, work_id, reason, "OPEN", order),
            )
            version = self._bump_work(row, disposition="WAITING", order=order, now=timestamp)
            self._append_fact(work_id=work_id, state_version=version, kind="wait_opened", from_disposition="RUNNING", to_disposition="WAITING", payload={"wait_id": wait_id, "reason": reason}, created_order=order)
            return self._wait(self._connection.execute("SELECT wait_id,work_id,reason,status,created_order,resolved_order,resolution FROM m4a_waits WHERE wait_id=?", (wait_id,)).fetchone())  # type: ignore[arg-type]

        return self._write(operation, failpoint=failpoint)

    def resolve_wait(
        self,
        work_id: str,
        wait_id: str,
        lease_id: str,
        fence: int,
        expected_state_version: int,
        *,
        resolution: str = "resolved",
        now: float | None = None,
        failpoint: FailPoint | None = None,
    ) -> WaitRecord:
        work_id = _text(work_id, field="work_id")
        wait_id = _text(wait_id, field="wait_id")
        resolution = _text(resolution, field="resolution")
        timestamp = self._now(now)

        def operation() -> WaitRecord:
            existing = self._connection.execute("SELECT wait_id,work_id,reason,status,created_order,resolved_order,resolution FROM m4a_waits WHERE wait_id=?", (wait_id,)).fetchone()
            if existing is None:
                _fail("wait_not_found", "Wait does not exist")
            if str(existing[1]) != work_id:
                _fail("wait_id_conflict", "wait_id is bound to another WorkItem")
            if str(existing[3]) == "RESOLVED":
                return self._wait(existing)
            row = self._require_work(work_id)
            self._expect_version(row, expected_state_version)
            if str(row[3]) != "WAITING":
                _fail("invalid_transition", "only WAITING WorkItems can resolve a Wait")
            self._require_lease(work_id, lease_id, fence, now=timestamp)
            order = self._next_order()
            self._connection.execute("UPDATE m4a_waits SET status='RESOLVED',resolved_order=?,resolution=? WHERE wait_id=?", (order, resolution, wait_id))
            version = self._bump_work(row, disposition="READY", order=order, now=timestamp)
            self._append_fact(work_id=work_id, state_version=version, kind="wait_resolved", from_disposition="WAITING", to_disposition="READY", payload={"wait_id": wait_id, "resolution": resolution}, created_order=order)
            return self._wait(self._connection.execute("SELECT wait_id,work_id,reason,status,created_order,resolved_order,resolution FROM m4a_waits WHERE wait_id=?", (wait_id,)).fetchone())  # type: ignore[arg-type]

        return self._write(operation, failpoint=failpoint)

    def finish_run(
        self,
        work_id: str,
        run_id: str,
        lease_id: str,
        fence: int,
        expected_state_version: int,
        *,
        outcome: RunOutcome,
        effect_certainty: EffectCertainty = "NOT_ASSESSED",
        now: float | None = None,
        failpoint: FailPoint | None = None,
    ) -> RunRecord:
        work_id = _text(work_id, field="work_id")
        run_id = _text(run_id, field="run_id")
        if outcome not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            _fail("invalid_run_outcome", "Run outcome is unsupported")
        if effect_certainty not in {"NOT_ASSESSED", "CERTAIN", "POSSIBLE", "UNKNOWN"}:
            _fail("invalid_effect_certainty", "effect certainty is unsupported")
        timestamp = self._now(now)

        def operation() -> RunRecord:
            existing = self._connection.execute("SELECT run_id,work_id,status,outcome,effect_certainty,lease_id,fence,started_order,ended_order FROM m4a_runs WHERE run_id=?", (run_id,)).fetchone()
            if existing is None or str(existing[1]) != work_id:
                _fail("run_not_found", "Run does not belong to the WorkItem")
            if str(existing[2]) == "FINISHED":
                if str(existing[3]) == outcome and str(existing[4]) == effect_certainty:
                    return self._run(existing)
                _fail("run_conflict", "finished Run cannot be rewritten")
            row = self._require_work(work_id)
            self._expect_version(row, expected_state_version)
            if str(row[3]) not in {"RUNNING", "READY"}:
                _fail("invalid_transition", "only RUNNING or wait-resumed READY WorkItems can finish a Run")
            self._require_lease(work_id, lease_id, fence, now=timestamp)
            active = self._connection.execute(
                "SELECT run_id,lease_id,fence FROM m4a_runs WHERE work_id=? AND status='ACTIVE'",
                (work_id,),
            ).fetchone()
            if active is None:
                _fail("active_run_missing", "WorkItem has no active Run to finish")
            if str(active[0]) != run_id:
                _fail("active_run_mismatch", "another Run is active for the WorkItem")
            if str(existing[5]) != lease_id or int(existing[6]) != fence:
                _fail("run_ownership_mismatch", "current lease/fence does not own the active Run")
            order = self._next_order()
            self._connection.execute("UPDATE m4a_runs SET status='FINISHED',outcome=?,effect_certainty=?,ended_order=? WHERE run_id=? AND status='ACTIVE'", (outcome, effect_certainty, order, run_id))
            version = self._bump_work(row, disposition="FINISHED", order=order, now=timestamp)
            self._append_fact(work_id=work_id, state_version=version, kind="run_finished", from_disposition=str(row[3]), to_disposition="FINISHED", payload={"run_id": run_id, "outcome": outcome, "effect_certainty": effect_certainty}, created_order=order)
            return self._run(self._connection.execute("SELECT run_id,work_id,status,outcome,effect_certainty,lease_id,fence,started_order,ended_order FROM m4a_runs WHERE run_id=?", (run_id,)).fetchone())  # type: ignore[arg-type]

        return self._write(operation, failpoint=failpoint)

    def _query_locked(self, work_id: str, *, now: float) -> WorkItemQuery | None:
        row = self._work_row(work_id)
        if row is None:
            return None
        active_row = self._connection.execute("SELECT run_id,work_id,status,outcome,effect_certainty,lease_id,fence,started_order,ended_order FROM m4a_runs WHERE work_id=? AND status='ACTIVE' ORDER BY started_order DESC LIMIT 1", (work_id,)).fetchone()
        last_row = self._connection.execute("SELECT run_id,work_id,status,outcome,effect_certainty,lease_id,fence,started_order,ended_order FROM m4a_runs WHERE work_id=? ORDER BY started_order DESC LIMIT 1", (work_id,)).fetchone()
        wait_row = self._connection.execute("SELECT wait_id,work_id,reason,status,created_order,resolved_order,resolution FROM m4a_waits WHERE work_id=? AND status='OPEN' ORDER BY created_order DESC LIMIT 1", (work_id,)).fetchone()
        lease_row = self._lease_row(work_id)
        claim_row = self._connection.execute("SELECT resource_key,work_id,lease_id,fence,state,claimed_order FROM m4a_resource_claims WHERE work_id=? AND state='HELD' ORDER BY claimed_order DESC LIMIT 1", (work_id,)).fetchone()
        fact_rows = self._connection.execute("SELECT fact_id,work_id,state_version,kind,from_disposition,to_disposition,causal_digest,payload,created_order FROM m4a_transition_facts WHERE work_id=? ORDER BY state_version DESC LIMIT 8", (work_id,)).fetchall()
        return WorkItemQuery(
            self._work(row),
            self._run(active_row) if active_row else None,
            self._run(last_row) if last_row else None,
            self._wait(wait_row) if wait_row else None,
            self._lease(lease_row, now=now) if lease_row else None,
            self._claim(claim_row) if claim_row else None,
            tuple(self._fact(item) for item in reversed(fact_rows)),
        )

    def query(self, work_id: str, *, now: float | None = None) -> WorkItemQuery | None:
        work_id = _text(work_id, field="work_id")
        timestamp = self._now(now)
        with self._lock:
            owns_snapshot = not self._connection.in_transaction
            try:
                if owns_snapshot:
                    self._connection.execute("BEGIN DEFERRED")
                result = self._query_locked(work_id, now=timestamp)
                if owns_snapshot:
                    self._connection.commit()
                return result
            except M4aError:
                if owns_snapshot and self._connection.in_transaction:
                    self._connection.rollback()
                raise
            except sqlite3.DatabaseError as exc:
                if owns_snapshot and self._connection.in_transaction:
                    self._connection.rollback()
                self._map_sqlite(exc, action="canonical query", write=False)

    def facts(self, work_id: str) -> tuple[TransitionFact, ...]:
        work_id = _text(work_id, field="work_id")
        with self._lock:
            rows = self._connection.execute("SELECT fact_id,work_id,state_version,kind,from_disposition,to_disposition,causal_digest,payload,created_order FROM m4a_transition_facts WHERE work_id=? ORDER BY state_version", (work_id,)).fetchall()
        return tuple(self._fact(row) for row in rows)

    def counts(self) -> dict[str, int]:
        with self._lock:
            return {
                "work_items": int(self._connection.execute("SELECT COUNT(*) FROM m4a_work_items").fetchone()[0]),
                "runs": int(self._connection.execute("SELECT COUNT(*) FROM m4a_runs").fetchone()[0]),
                "waits": int(self._connection.execute("SELECT COUNT(*) FROM m4a_waits").fetchone()[0]),
                "leases": int(self._connection.execute("SELECT COUNT(*) FROM m4a_leases").fetchone()[0]),
                "resource_claims": int(self._connection.execute("SELECT COUNT(*) FROM m4a_resource_claims").fetchone()[0]),
                "transition_facts": int(self._connection.execute("SELECT COUNT(*) FROM m4a_transition_facts").fetchone()[0]),
            }

    @contextmanager
    def hold_write_lock(self) -> Iterator[None]:
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                yield
            finally:
                if self._connection.in_transaction:
                    self._connection.rollback()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "WorkKernelStore":
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()


def scan_supported_workitem_writers() -> dict[str, Any]:
    """Return the bounded post-M4a writer authority proof."""

    return {
        "schema": M4A_SCHEMA,
        "canonical_writer": M4A_WRITER_ID,
        "canonical_authority": M4A_AUTHORITY_ID,
        "alternate_writers": [],
        "direct_browser_writer": False,
        "direct_native_writer": False,
        "direct_scheduler_writer": False,
        "direct_executor_writer": False,
        "legacy_writer_supported": False,
        "pass": True,
    }


__all__ = [
    "ClaimState",
    "EffectCertainty",
    "FailPoint",
    "LeaseRecord",
    "M4A_AUTHORITY_ID",
    "M4A_DATABASE_NAME",
    "M4A_PROTOCOL_GENERATION",
    "M4A_QUERY_SCHEMA",
    "M4A_SCHEMA",
    "M4A_STORE_SCHEMA",
    "M4A_WRITER_ID",
    "M4aError",
    "ResourceClaim",
    "RunRecord",
    "TransitionFact",
    "WaitRecord",
    "WorkDisposition",
    "WorkItem",
    "WorkItemQuery",
    "WorkKernelStore",
    "scan_supported_workitem_writers",
]

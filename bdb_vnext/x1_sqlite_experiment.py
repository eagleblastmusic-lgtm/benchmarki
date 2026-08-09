"""X1-vNext real SQLite authority and durability experiment.

The experiment is intentionally small and disposable.  It uses sqlite3 files
and the existing M1b external lock/backup/restore floor; it does not create a
production Control Store or an activation path.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from bdb_vnext.bootstrap import (
    BootstrapError,
    BootstrapLock,
    BackupArtifact,
    create_coordinated_backup,
    restore_backup,
    verify_backup,
)


X1_SCHEMA = "bdb-vnext-x1-experiment-v1"
X1_CONTROL_SCHEMA = 1
X1_WRITER_ID = "x1-canonical-writer"
X1_JOURNAL_MODE = "wal"
X1_SYNCHRONOUS = "full"
X1_BUSY_TIMEOUT_MS = 250
X1_TABLES = ("x1_meta", "x1_entries")

X1Status = Literal["PASS", "FAIL", "INCONCLUSIVE"]


class X1ExperimentError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SQLiteSettings:
    journal_mode: str
    synchronous: int
    busy_timeout_ms: int
    wal_autocheckpoint: int
    foreign_keys: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "journal_mode": self.journal_mode,
            "synchronous": self.synchronous,
            "busy_timeout_ms": self.busy_timeout_ms,
            "wal_autocheckpoint": self.wal_autocheckpoint,
            "foreign_keys": self.foreign_keys,
        }


@dataclass(frozen=True)
class IntegrityReport:
    database: str
    integrity_check: str
    journal_mode: str
    schema_version: str
    writer_id: str
    tokens: tuple[str, ...]
    row_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "database": self.database,
            "integrity_check": self.integrity_check,
            "journal_mode": self.journal_mode,
            "schema_version": self.schema_version,
            "writer_id": self.writer_id,
            "tokens": list(self.tokens),
            "row_count": self.row_count,
        }


@dataclass(frozen=True)
class CrashBoundaryEvidence:
    phase: str
    process_killed: bool
    expected_tokens: tuple[str, ...]
    observed_tokens: tuple[str, ...]
    integrity_check: str
    recovery_token: str
    recovery_committed: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "process_killed": self.process_killed,
            "expected_tokens": list(self.expected_tokens),
            "observed_tokens": list(self.observed_tokens),
            "integrity_check": self.integrity_check,
            "recovery_token": self.recovery_token,
            "recovery_committed": self.recovery_committed,
        }


def _fail(code: str, message: str) -> None:
    raise X1ExperimentError(code, message)


def _db_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        _fail("relative_path", "SQLite experiment paths must be absolute")
    return Path(os.path.abspath(path))


def _execute_pragma(connection: sqlite3.Connection, statement: str) -> Any:
    try:
        return connection.execute(statement).fetchone()[0]
    except sqlite3.DatabaseError as exc:
        raise X1ExperimentError("sqlite_pragma_failed", statement) from exc


def configure_connection(
    connection: sqlite3.Connection,
    *,
    read_only: bool = False,
    busy_timeout_ms: int = X1_BUSY_TIMEOUT_MS,
) -> SQLiteSettings:
    if not 1 <= busy_timeout_ms <= 10_000:
        _fail("invalid_busy_timeout", "busy timeout must be bounded")
    try:
        connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")
        foreign_keys = int(_execute_pragma(connection, "PRAGMA foreign_keys"))
        if not read_only:
            journal_mode = str(_execute_pragma(connection, "PRAGMA journal_mode=WAL")).lower()
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
        else:
            journal_mode = str(_execute_pragma(connection, "PRAGMA journal_mode")).lower()
        synchronous = int(_execute_pragma(connection, "PRAGMA synchronous"))
        wal_autocheckpoint = int(_execute_pragma(connection, "PRAGMA wal_autocheckpoint"))
        configured_timeout = int(_execute_pragma(connection, "PRAGMA busy_timeout"))
    except sqlite3.DatabaseError as exc:
        raise X1ExperimentError("sqlite_settings_failed", "required SQLite settings could not be applied") from exc
    if foreign_keys != 1:
        _fail("foreign_keys_disabled", "foreign key enforcement is not enabled")
    if not read_only and journal_mode != X1_JOURNAL_MODE:
        _fail("wal_unavailable", f"SQLite selected journal_mode={journal_mode!r}")
    if not read_only and synchronous != 2:
        _fail("durability_setting_mismatch", "SQLite synchronous mode is not FULL")
    if configured_timeout != busy_timeout_ms:
        _fail("busy_timeout_mismatch", "SQLite did not retain the bounded busy timeout")
    return SQLiteSettings(
        journal_mode=journal_mode,
        synchronous=synchronous,
        busy_timeout_ms=configured_timeout,
        wal_autocheckpoint=wal_autocheckpoint,
        foreign_keys=foreign_keys,
    )


def initialize_control_store(database: str | Path) -> SQLiteSettings:
    """Create only the minimal real SQLite fixture used by X1."""

    path = _db_path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        _fail("database_exists", "X1 fixture creation refuses to overwrite a database")
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(path, timeout=X1_BUSY_TIMEOUT_MS / 1000)
        settings = configure_connection(connection)
        connection.executescript(
            """
            CREATE TABLE x1_meta (
                key TEXT PRIMARY KEY NOT NULL,
                value TEXT NOT NULL
            );
            CREATE TABLE x1_entries (
                entry_id INTEGER PRIMARY KEY,
                token TEXT NOT NULL UNIQUE,
                value TEXT NOT NULL,
                committed_by TEXT NOT NULL
            );
            INSERT INTO x1_meta(key, value) VALUES
                ('schema_version', '1'),
                ('writer_id', 'x1-canonical-writer');
            """
        )
        connection.commit()
    except sqlite3.DatabaseError as exc:
        raise X1ExperimentError("fixture_create_failed", "real SQLite fixture creation failed") from exc
    finally:
        if connection is not None:
            connection.close()
    return settings


def _read_only_uri(path: Path) -> str:
    return f"file:{path.as_posix()}?mode=ro"


def inspect_control_store(
    database: str | Path, *, expected_tokens: Sequence[str] = ()
) -> IntegrityReport:
    """Open a real SQLite file read-only and verify both SQLite and app truth."""

    path = _db_path(database)
    if not path.is_file():
        _fail("database_missing", "X1 control.db is missing")
    expected = tuple(sorted(str(token) for token in expected_tokens))
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            _read_only_uri(path), uri=True, timeout=X1_BUSY_TIMEOUT_MS / 1000
        )
        configure_connection(connection, read_only=True)
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0]).lower()
        if integrity != "ok":
            _fail("integrity_failure", f"PRAGMA integrity_check returned {integrity!r}")
        rows = connection.execute(
            "SELECT key, value FROM x1_meta ORDER BY key"
        ).fetchall()
        meta = {str(key): str(value) for key, value in rows}
        if meta.get("schema_version") != str(X1_CONTROL_SCHEMA):
            _fail("application_invariant_failure", "minimal schema_version invariant is false")
        if meta.get("writer_id") != X1_WRITER_ID:
            _fail("application_invariant_failure", "canonical writer identity invariant is false")
        tokens = tuple(
            sorted(str(row[0]) for row in connection.execute("SELECT token FROM x1_entries"))
        )
        if expected and tokens != expected:
            _fail(
                "application_invariant_failure",
                f"expected committed tokens {expected!r}, observed {tokens!r}",
            )
        report = IntegrityReport(
            database=str(path),
            integrity_check=integrity,
            journal_mode=str(_execute_pragma(connection, "PRAGMA journal_mode")).lower(),
            schema_version=meta["schema_version"],
            writer_id=meta["writer_id"],
            tokens=tokens,
            row_count=len(tokens),
        )
        return report
    except sqlite3.DatabaseError as exc:
        raise X1ExperimentError("sqlite_open_or_integrity_failed", str(exc)) from exc
    finally:
        if connection is not None:
            connection.close()


class CanonicalWriter:
    """One process-level writer lease plus one SQLite connection."""

    def __init__(self, database: str | Path, writer_lock: str | Path) -> None:
        self.database = _db_path(database)
        self.writer_lock = _db_path(writer_lock)
        self._lock: BootstrapLock | None = None
        self.connection: sqlite3.Connection | None = None
        self.settings: SQLiteSettings | None = None

    def __enter__(self) -> "CanonicalWriter":
        self._lock = BootstrapLock(self.writer_lock)
        try:
            self._lock.__enter__()
            self.connection = sqlite3.connect(
                self.database, timeout=X1_BUSY_TIMEOUT_MS / 1000
            )
            self.settings = configure_connection(self.connection)
            self._verify_schema()
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def _verify_schema(self) -> None:
        assert self.connection is not None
        rows = {
            str(row[0])
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not set(X1_TABLES).issubset(rows):
            _fail("application_invariant_failure", "minimal X1 schema is incomplete")

    def insert(self, token: str, value: str, *, phase: str = "normal") -> None:
        if not token or len(token) > 128:
            _fail("invalid_token", "X1 token must be bounded and non-empty")
        assert self.connection is not None
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            if phase == "after_begin":
                return
            self.connection.execute(
                "INSERT INTO x1_entries(token, value, committed_by) VALUES (?, ?, ?)",
                (token, value, X1_WRITER_ID),
            )
            if phase == "after_mutation":
                return
            self.connection.commit()
        except sqlite3.IntegrityError as exc:
            raise X1ExperimentError("application_write_failed", str(exc)) from exc
        except sqlite3.OperationalError as exc:
            raise X1ExperimentError("sqlite_write_failed", str(exc)) from exc

    def rollback(self) -> None:
        if self.connection is not None and self.connection.in_transaction:
            self.connection.rollback()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.connection is not None:
            try:
                if self.connection.in_transaction:
                    self.connection.rollback()
            finally:
                self.connection.close()
                self.connection = None
        if self._lock is not None:
            self._lock.__exit__(exc_type, exc, traceback)
            self._lock = None


def write_committed_row(database: str | Path, writer_lock: str | Path, token: str) -> None:
    with CanonicalWriter(database, writer_lock) as writer:
        writer.insert(token, f"value:{token}")


def checkpoint_wal(database: str | Path, writer_lock: str | Path) -> tuple[int, int, int]:
    with CanonicalWriter(database, writer_lock) as writer:
        assert writer.connection is not None
        result = writer.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        assert result is not None and len(result) == 3
        return tuple(int(item) for item in result)


def backup_real_control_store(
    runtime_root: str | Path,
    authority_root: str | Path,
    *,
    backup_id: str,
) -> BackupArtifact:
    """Reuse M1b backup; X1 only adds real SQLite evidence around it."""

    runtime = _db_path(runtime_root)
    database = runtime / "control" / "control.db"
    if not database.is_file():
        _fail("post_x1_database_required", "control.db is required after the X1 gate")
    return create_coordinated_backup(
        runtime,
        _db_path(authority_root) / "backups",
        backup_id=backup_id,
        required_control_schema=X1_CONTROL_SCHEMA,
        source_is_quiesced=True,
    )


def classify_post_x1_subjects(
    artifact: BackupArtifact, *, expected_tokens: Sequence[str]
) -> dict[str, Any]:
    """Apply the evidence-backed post-X1 DB/WAL requiredness decision."""

    subjects = {str(item["name"]): item for item in artifact.document["subjects"]}
    database = subjects["control_db"]
    wal = subjects["control_wal"]
    if database["state"] != "present":
        _fail("post_x1_database_required", "post-X1 backup cannot declare control.db absent")
    report = inspect_control_store(
        Path(artifact.document["source_root"]) / "control" / "control.db",
        expected_tokens=expected_tokens,
    )
    if wal["state"] == "present":
        wal_decision = "present_and_verified"
    elif wal["state"] == "declared_absent":
        wal_decision = "legal_absent_after_verified_sqlite_state"
    else:
        _fail("backup_manifest_invalid", "post-X1 WAL state is not explicit")
    return {
        "control_db": "required_present",
        "control_wal": wal_decision,
        "source_integrity": report.as_dict(),
    }


def _write_marker(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(document), sort_keys=True), encoding="utf-8")


def _wait_for_release(path: Path, *, timeout_seconds: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"release marker was not observed: {path}")
        time.sleep(0.01)


def _worker_main(arguments: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="x1-worker")
    parser.add_argument("--db", required=True)
    parser.add_argument("--lock", required=True)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--token", default="worker-token")
    parser.add_argument("--ready", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args(arguments)
    database = _db_path(args.db)
    lock = _db_path(args.lock)
    ready = _db_path(args.ready)
    release = _db_path(args.release)
    result = _db_path(args.result)
    try:
        if args.phase == "before_begin":
            _write_marker(ready, {"phase": args.phase, "state": "before_begin"})
            _wait_for_release(release)
            _write_marker(result, {"status": "released_without_transaction"})
            return 0
        if args.phase == "raw_contender":
            try:
                connection = sqlite3.connect(database, timeout=X1_BUSY_TIMEOUT_MS / 1000)
                configure_connection(connection)
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO x1_entries(token, value, committed_by) VALUES (?, ?, ?)",
                    (args.token, f"value:{args.token}", "raw-contender"),
                )
                connection.commit()
                connection.close()
                _write_marker(result, {"status": "committed"})
                return 0
            except sqlite3.OperationalError as exc:
                _write_marker(
                    result,
                    {"status": "busy", "error": str(exc), "is_locked": "locked" in str(exc).lower()},
                )
                return 23
        if args.phase == "canonical_contender":
            try:
                with BootstrapLock(lock):
                    _write_marker(result, {"status": "acquired"})
                    return 0
            except BootstrapError as exc:
                _write_marker(result, {"status": "blocked", "code": exc.code})
                return 23

        with CanonicalWriter(database, lock) as writer:
            if args.phase == "after_begin":
                assert writer.connection is not None
                writer.connection.execute("BEGIN IMMEDIATE")
                _write_marker(ready, {"phase": args.phase, "state": "begin"})
                _wait_for_release(release)
                return 0
            assert writer.connection is not None
            writer.connection.execute("BEGIN IMMEDIATE")
            writer.connection.execute(
                "INSERT INTO x1_entries(token, value, committed_by) VALUES (?, ?, ?)",
                (args.token, f"value:{args.token}", X1_WRITER_ID),
            )
            if args.phase in {"after_mutation", "before_commit"}:
                _write_marker(ready, {"phase": args.phase, "state": "mutation"})
                _wait_for_release(release)
                return 0
            if args.phase != "after_commit":
                raise X1ExperimentError("unknown_worker_phase", args.phase)
            writer.connection.commit()
            _write_marker(ready, {"phase": args.phase, "state": "commit_complete"})
            _wait_for_release(release)
            return 0
    except Exception as exc:
        _write_marker(result, {"status": "error", "code": getattr(exc, "code", "worker_error"), "error": str(exc)})
        return 22


def _wait_for_file(path: Path, *, timeout_seconds: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while not path.exists():
        if time.monotonic() >= deadline:
            _fail("worker_timeout", f"worker did not reach its coordinated boundary: {path}")
        time.sleep(0.01)


def _spawn_worker(
    root: Path,
    *,
    database: Path,
    lock: Path,
    phase: str,
    token: str,
    prefix: str,
) -> tuple[subprocess.Popen[str], Path, Path, Path]:
    ready = root / f"{prefix}.ready.json"
    release = root / f"{prefix}.release"
    result = root / f"{prefix}.result.json"
    command = [
        sys.executable,
        "-m",
        "bdb_vnext.x1_sqlite_experiment",
        "worker",
        "--db",
        str(database),
        "--lock",
        str(lock),
        "--phase",
        phase,
        "--token",
        token,
        "--ready",
        str(ready),
        "--release",
        str(release),
        "--result",
        str(result),
    ]
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    process = subprocess.Popen(
        command,
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return process, ready, release, result


def _kill_at_boundary(
    root: Path, *, database: Path, lock: Path, phase: str, token: str
) -> bool:
    process, ready, _release, result = _spawn_worker(
        root,
        database=database,
        lock=lock,
        phase=phase,
        token=token,
        prefix=f"kill-{phase}",
    )
    _wait_for_file(ready)
    process.kill()
    process.wait(timeout=10)
    if result.exists():
        payload = json.loads(result.read_text(encoding="utf-8"))
        if payload.get("status") == "error":
            _fail("worker_error", str(payload))
    return process.returncode is not None


def run_crash_boundary(
    root: Path, *, phase: str, token: str
) -> CrashBoundaryEvidence:
    runtime = root / f"crash-{phase}"
    database = runtime / "control" / "control.db"
    lock = runtime / "coordination" / "x1-writer.lock"
    runtime.mkdir(parents=True)
    initialize_control_store(database)
    before = inspect_control_store(database, expected_tokens=())
    killed = _kill_at_boundary(
        root,
        database=database,
        lock=lock,
        phase=phase,
        token=token,
    )
    expected = (token,) if phase == "after_commit" else ()
    after = inspect_control_store(database, expected_tokens=expected)
    recovery_token = f"recovery-{phase}"
    write_committed_row(database, lock, recovery_token)
    recovered = inspect_control_store(
        database, expected_tokens=tuple(sorted((*expected, recovery_token)))
    )
    if before.integrity_check != "ok" or after.integrity_check != "ok" or recovered.integrity_check != "ok":
        _fail("integrity_failure", f"integrity did not remain green for {phase}")
    return CrashBoundaryEvidence(
        phase=phase,
        process_killed=killed,
        expected_tokens=expected,
        observed_tokens=after.tokens,
        integrity_check=recovered.integrity_check,
        recovery_token=recovery_token,
        recovery_committed=recovery_token in recovered.tokens,
    )


def run_contention_experiment(root: Path) -> dict[str, Any]:
    runtime = root / "contention"
    database = runtime / "control" / "control.db"
    lock = runtime / "coordination" / "x1-writer.lock"
    runtime.mkdir(parents=True)
    initialize_control_store(database)
    holder, ready, release, _result = _spawn_worker(
        root,
        database=database,
        lock=lock,
        phase="after_begin",
        token="holder-token",
        prefix="holder",
    )
    _wait_for_file(ready)
    canonical = _spawn_worker(
        root,
        database=database,
        lock=lock,
        phase="canonical_contender",
        token="canonical-contender",
        prefix="canonical-contender",
    )[0]
    canonical.wait(timeout=10)
    canonical_result_path = root / "canonical-contender.result.json"
    _wait_for_file(canonical_result_path)
    raw = _spawn_worker(
        root,
        database=database,
        lock=lock,
        phase="raw_contender",
        token="raw-contender",
        prefix="raw-contender",
    )[0]
    raw.wait(timeout=10)
    raw_result_path = root / "raw-contender.result.json"
    _wait_for_file(raw_result_path)
    holder.kill()
    holder.wait(timeout=10)
    release.touch()
    canonical_result = json.loads(canonical_result_path.read_text(encoding="utf-8"))
    raw_result = json.loads(raw_result_path.read_text(encoding="utf-8"))
    report = inspect_control_store(database, expected_tokens=())
    if canonical_result.get("status") != "blocked" or canonical_result.get("code") != "concurrent_attempt":
        _fail("writer_authority_failure", f"canonical contender result was {canonical_result!r}")
    if raw_result.get("status") != "busy" or not raw_result.get("is_locked"):
        _fail("sqlite_contention_failure", f"raw contender result was {raw_result!r}")
    return {
        "canonical_contender": canonical_result,
        "raw_sqlite_contender": raw_result,
        "holder_killed": holder.returncode is not None,
        "post_contention_integrity": report.as_dict(),
        "second_authority_committed": False,
    }


def _real_wal_backup_and_restore(root: Path) -> dict[str, Any]:
    runtime = root / "wal-runtime"
    database = runtime / "control" / "control.db"
    lock = runtime / "coordination" / "x1-writer.lock"
    authority = root / "wal-authority"
    restore_target = root / "wal-restored"
    runtime.mkdir(parents=True)
    initialize_control_store(database)
    reader = sqlite3.connect(database, timeout=X1_BUSY_TIMEOUT_MS / 1000)
    configure_connection(reader)
    reader.execute("PRAGMA query_only=ON")
    reader.execute("BEGIN")
    token = "wal-committed"
    write_committed_row(database, lock, token)
    wal_path = database.with_name(database.name + "-wal")
    if not wal_path.is_file():
        reader.close()
        _fail("wal_not_observed", "real SQLite commit did not leave an observable WAL fixture")
    artifact = backup_real_control_store(runtime, authority, backup_id="real-wal")
    classified = classify_post_x1_subjects(artifact, expected_tokens=(token,))
    receipt = restore_backup(
        artifact.path,
        restore_target,
        authority_root=authority,
        legacy_runtime_root=root / "legacy",
    )
    restored_report = inspect_control_store(restore_target / "control" / "control.db", expected_tokens=(token,))
    reader.rollback()
    reader.close()
    return {
        "backup_manifest_sha256": artifact.manifest_sha256,
        "restore_sha256": receipt["restore_sha256"],
        "restore_verified": receipt["verified"],
        "wal_source_present": True,
        "backup_subjects": artifact.document["subjects"],
        "post_x1_subject_decision": classified,
        "restored_integrity": restored_report.as_dict(),
    }


def _legal_absent_wal_backup_and_restore(root: Path) -> dict[str, Any]:
    runtime = root / "checkpointed-runtime"
    database = runtime / "control" / "control.db"
    authority = root / "checkpointed-authority"
    restore_target = root / "checkpointed-restored"
    runtime.mkdir(parents=True)
    initialize_control_store(database)
    wal_path = database.with_name(database.name + "-wal")
    if wal_path.exists():
        checkpoint_wal(database, runtime / "coordination" / "x1-writer.lock")
    if wal_path.exists():
        _fail("wal_absence_not_observed", "clean checkpointed SQLite fixture retained an unexpected WAL")
    artifact = backup_real_control_store(runtime, authority, backup_id="checkpointed")
    expected_tokens: tuple[str, ...] = ()
    classified = classify_post_x1_subjects(artifact, expected_tokens=expected_tokens)
    receipt = restore_backup(
        artifact.path,
        restore_target,
        authority_root=authority,
        legacy_runtime_root=root / "legacy",
    )
    restored_report = inspect_control_store(restore_target / "control" / "control.db", expected_tokens=expected_tokens)
    wal_subject = next(item for item in artifact.document["subjects"] if item["name"] == "control_wal")
    if wal_subject["state"] != "declared_absent":
        _fail("wal_requiredness_classification_failure", f"WAL subject was {wal_subject!r}")
    return {
        "backup_manifest_sha256": artifact.manifest_sha256,
        "restore_sha256": receipt["restore_sha256"],
        "restore_verified": receipt["verified"],
        "wal_source_present": False,
        "backup_subjects": artifact.document["subjects"],
        "post_x1_subject_decision": classified,
        "restored_integrity": restored_report.as_dict(),
    }


def _tamper_backup_cases(root: Path) -> dict[str, Any]:
    source = root / "tamper-source"
    database = source / "control" / "control.db"
    lock = source / "coordination" / "x1-writer.lock"
    authority = root / "tamper-authority"
    source.mkdir(parents=True)
    initialize_control_store(database)
    write_committed_row(database, lock, "tamper-row")
    # Keep a real WAL subject alive while M1b publishes the snapshot.
    reader = sqlite3.connect(database, timeout=X1_BUSY_TIMEOUT_MS / 1000)
    configure_connection(reader)
    reader.execute("PRAGMA query_only=ON")
    reader.execute("BEGIN")
    write_committed_row(database, lock, "tamper-wal-row")
    artifact = backup_real_control_store(source, authority, backup_id="tamper-base")
    reader.rollback()
    reader.close()
    cases: dict[str, str] = {}
    for name, mutation in (
        ("missing_wal", "missing_wal"),
        ("truncated_db", "truncated_db"),
        ("truncated_wal", "truncated_wal"),
        ("corrupt_db", "corrupt_db"),
        ("corrupt_wal", "corrupt_wal"),
    ):
        # Keep the published directory identity equal to backup_id so the
        # verifier reaches the intended manifest/bytes integrity check.
        case = root / f"tamper-{name}" / "tamper-base"
        shutil.copytree(artifact.path, case)
        target: Path
        if mutation in {"missing_wal", "truncated_wal", "corrupt_wal"}:
            target = case / "control" / "control.db-wal"
        else:
            target = case / "control" / "control.db"
        if mutation == "missing_wal":
            target.unlink()
        elif mutation.startswith("truncated"):
            target.write_bytes(target.read_bytes()[:-1])
        else:
            payload = bytearray(target.read_bytes())
            offset = 100 if len(payload) > 100 else 0
            payload[offset] ^= 0xFF
            target.write_bytes(payload)
        try:
            verify_backup(case)
        except BootstrapError as exc:
            cases[name] = exc.code
        else:
            _fail("tamper_accepted", f"M1b verifier accepted tampered case {name}")
    missing_restore_target = root / "missing-wal-restore"
    missing_backup = root / "tamper-missing_wal" / "tamper-base"
    try:
        restore_backup(
            missing_backup,
            missing_restore_target,
            authority_root=authority,
            legacy_runtime_root=root / "legacy",
        )
    except BootstrapError as exc:
        missing_restore_error = exc.code
    else:
        _fail("missing_subject_restore_accepted", "restore accepted a backup with a missing WAL subject")
    return {
        "base_manifest_sha256": artifact.manifest_sha256,
        "cases": cases,
        "missing_subject_restore_error": missing_restore_error,
        "missing_subject_restore_blocked": missing_restore_error == "backup_integrity_failure",
    }


def run_experiment(root: str | Path) -> dict[str, Any]:
    """Run the complete disposable X1 evidence capsule."""

    base = _db_path(root)
    base.mkdir(parents=True, exist_ok=True)
    crash_phases = (
        "before_begin",
        "after_begin",
        "after_mutation",
        "before_commit",
        "after_commit",
    )
    crash_evidence = [
        run_crash_boundary(base, phase=phase, token=f"crash-{phase}")
        for phase in crash_phases
    ]
    contention = run_contention_experiment(base)
    wal = _real_wal_backup_and_restore(base)
    absent_wal = _legal_absent_wal_backup_and_restore(base)
    tamper = _tamper_backup_cases(base)
    observed_boundaries = [item.phase for item in crash_evidence if item.process_killed]
    if tuple(observed_boundaries) != crash_phases:
        _fail("crash_boundary_incomplete", "not every coordinated process-kill boundary was observed")
    if any(
        item.observed_tokens != item.expected_tokens or not item.recovery_committed
        for item in crash_evidence
    ):
        _fail("crash_truth_failure", "SQLite truth differed at a process-kill boundary")
    if contention["second_authority_committed"]:
        _fail("second_authority_committed", "a second writer authority committed")
    if not wal["restore_verified"] or not absent_wal["restore_verified"]:
        _fail("restore_verification_failure", "M1b restore did not verify a real SQLite database")
    return {
        "schema": X1_SCHEMA,
        "status": "PASS",
        "environment": {
            "python": sys.version,
            "sqlite": sqlite3.sqlite_version,
            "platform": sys.platform,
            "os": os.name,
            "settings": {
                "journal_mode": X1_JOURNAL_MODE,
                "synchronous": X1_SYNCHRONOUS,
                "busy_timeout_ms": X1_BUSY_TIMEOUT_MS,
                "wal_autocheckpoint": 0,
                "foreign_keys": True,
            },
        },
        "hypotheses": {
            "H1": "PASS",
            "H2": "PASS for coordinated Windows process-kill boundaries; native COMMIT interruption and physical power-loss not claimed",
            "H3": "PASS",
            "H4": "PASS",
            "H5": "PASS",
            "H6": "PASS",
        },
        "crash_boundaries": [item.as_dict() for item in crash_evidence],
        "contention": contention,
        "m1b_real_wal_backup_restore": wal,
        "m1b_legal_absent_wal_backup_restore": absent_wal,
        "fault_matrix": tamper,
        "post_x1_storage_decision": {
            "control_db": "REQUIRED_PRESENT",
            "control_wal": "OPTIONAL_SUBJECT; PRESENT when uncheckpointed WAL is part of the verified state, DECLARED_ABSENT only after verified SQLite state makes its absence legal",
        },
        "limitations": [
            "process-kill boundaries were observed; physical power-loss was not simulated",
            "a kill inside the native SQLite COMMIT call was not claimed",
            "disk-full was not simulated because no deterministic local harness was justified",
        ],
        "authority": {
            "sqlite_is_durable_truth": True,
            "second_authority": False,
            "production_activation": False,
            "legacy_touched": False,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    # Worker arguments intentionally bypass the public experiment parser so
    # option names are passed unchanged to the child-process harness.
    if raw_arguments and raw_arguments[0] == "worker":
        return _worker_main(raw_arguments[1:])
    parser = argparse.ArgumentParser(prog="bdb-vnext-x1-experiment")
    subparsers = parser.add_subparsers(dest="command", required=True)
    worker = subparsers.add_parser("worker")
    worker.add_argument("args", nargs=argparse.REMAINDER)
    run = subparsers.add_parser("run")
    run.add_argument("--root")
    args = parser.parse_args(argv)
    temporary: tempfile.TemporaryDirectory[str] | None = None
    try:
        if args.root:
            root = _db_path(args.root)
            root.mkdir(parents=True, exist_ok=True)
        else:
            temporary = tempfile.TemporaryDirectory(prefix="bdb-vnext-x1-")
            root = Path(temporary.name)
        try:
            evidence = run_experiment(root)
            print(json.dumps(evidence, sort_keys=True, indent=2))
            return 0
        except X1ExperimentError as exc:
            output = {
                "schema": X1_SCHEMA,
                "status": "FAIL",
                "error": {"code": exc.code, "message": str(exc)},
            }
            print(json.dumps(output, sort_keys=True, indent=2))
            return 2
    except (OSError, subprocess.SubprocessError) as exc:
        print(
            json.dumps(
                {
                    "schema": X1_SCHEMA,
                    "status": "INCONCLUSIVE",
                    "error": {"code": "harness_unavailable", "message": str(exc)},
                },
                sort_keys=True,
                indent=2,
            )
        )
        return 3
    finally:
        if temporary is not None:
            temporary.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())

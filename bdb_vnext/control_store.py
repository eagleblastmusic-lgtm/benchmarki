"""Shared identity and connection-floor helpers for the vNext Control DB.

The database is physically shared by the M2, M3 and M4 repositories, but
those repositories retain typed ownership of their own tables.  This module
only owns the small cross-domain identity table and SQLite safety floor; it is
not a generic SQL repository and exposes no lifecycle mutation API.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NoReturn

from bdb_shared.evidence import canonical_json_bytes, semantic_digest
from bdb_vnext.composition import CONFIG_GENERATION, GENERATION_ID, CONTROL_STORE_SCHEMA


CONTROL_IDENTITY_SCHEMA = "bdb-vnext-control-identity-v1"
CONTROL_MIGRATION_ID = "n1-unified-control-v1"
CONTROL_METADATA_TABLE = "vnext_control_metadata"
CONTROL_LAYOUT_TABLE = "vnext_control_layout"
CONTROL_DB_USER_VERSION = 3
CONTROL_PREVIOUS_USER_VERSION = 2
CONTROL_DATABASE_RELATIVE_PATH = Path("control") / "control.db"
CONTROL_BUSY_TIMEOUT_MS = 250
CONTROL_WRITE_RETRIES = 1
CONTROL_SEAL_SCHEMA = "bdb-vnext-control-seal-v1"
CONTROL_SEAL_FILENAME = "control.db.seal.json"

_IDENTITY_KEYS = (
    "identity_schema",
    "store_id",
    "generation_id",
    "config_generation",
    "migration_id",
    "schema_checksum",
)


class ControlStoreError(RuntimeError):
    """Fail-closed identity/configuration error for the shared Control DB."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> NoReturn:
    raise ControlStoreError(code, message)


def is_sqlite_busy(exc: BaseException) -> bool:
    """Recognise SQLite contention without relying on one locale's message."""

    if not isinstance(exc, sqlite3.OperationalError):
        return False
    code = getattr(exc, "sqlite_errorcode", None)
    if code in {
        getattr(sqlite3, "SQLITE_BUSY", 5),
        getattr(sqlite3, "SQLITE_LOCKED", 6),
        getattr(sqlite3, "SQLITE_BUSY_RECOVERY", 261),
        getattr(sqlite3, "SQLITE_BUSY_SNAPSHOT", 517),
        getattr(sqlite3, "SQLITE_BUSY_TIMEOUT", 773),
        getattr(sqlite3, "SQLITE_LOCKED_SHAREDCACHE", 262),
        getattr(sqlite3, "SQLITE_LOCKED_VTAB", 518),
    }:
        return True
    text = str(exc).lower()
    return "database is locked" in text or "database table is locked" in text or "database busy" in text


def _busy(message: str = "vNext Control DB is busy") -> NoReturn:
    raise ControlStoreError("database_busy", message)


def begin_control_write(
    connection: sqlite3.Connection,
    *,
    retries: int = CONTROL_WRITE_RETRIES,
    retry_delay_seconds: float = 0.005,
) -> None:
    """Acquire the one shared writer boundary with bounded pre-mutation retry."""

    if not isinstance(retries, int) or retries < 0 or retries > 3:
        _fail("invalid_write_retries", "Control DB write retries are out of bounds")
    for attempt in range(retries + 1):
        try:
            connection.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError as exc:
            if not is_sqlite_busy(exc):
                raise ControlStoreError("control_sqlite_write_failed", "Control DB write transaction could not begin") from exc
            if connection.in_transaction:
                connection.rollback()
            if attempt >= retries:
                _busy()
            if retry_delay_seconds > 0:
                time.sleep(retry_delay_seconds)
        except sqlite3.DatabaseError as exc:
            raise ControlStoreError("control_sqlite_write_failed", "Control DB write transaction could not begin") from exc


def commit_control_write(connection: sqlite3.Connection) -> None:
    """Commit a write and turn contention into a typed, bounded failure."""

    try:
        connection.commit()
    except sqlite3.OperationalError as exc:
        if is_sqlite_busy(exc):
            # SQLite leaves a busy commit transaction open.  Normalise it so a
            # caller cannot accidentally continue with a half-owned writer.
            if connection.in_transaction:
                connection.rollback()
            _busy("vNext Control DB remained busy during commit")
        raise ControlStoreError("control_sqlite_write_failed", "Control DB write transaction could not commit") from exc
    except sqlite3.DatabaseError as exc:
        raise ControlStoreError("control_sqlite_write_failed", "Control DB write transaction could not commit") from exc


def rollback_control_write(connection: sqlite3.Connection) -> None:
    try:
        if connection.in_transaction:
            connection.rollback()
    except sqlite3.DatabaseError as exc:
        raise ControlStoreError("control_sqlite_rollback_failed", "Control DB write transaction could not roll back") from exc


def _database_path(connection: sqlite3.Connection) -> Path:
    try:
        row = connection.execute("PRAGMA database_list").fetchone()
    except sqlite3.DatabaseError as exc:
        raise ControlStoreError("control_identity_read_failed", "Control DB path could not be verified") from exc
    if row is None or not row[2]:
        _fail("control_database_path_unavailable", "Control DB must be a file-backed SQLite database")
    return Path(str(row[2])).expanduser().absolute()


def seal_path_for_database(database_path: str | Path) -> Path:
    return Path(database_path).expanduser().absolute().with_name(CONTROL_SEAL_FILENAME)


def _read_seal(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        document = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControlStoreError("control_seal_corrupt", "Control DB external seal is not valid canonical JSON") from exc
    if not isinstance(document, dict) or document.get("schema") != CONTROL_SEAL_SCHEMA:
        _fail("control_seal_mismatch", "Control DB external seal schema differs")
    return document


def _write_seal(path: Path, document: Mapping[str, Any]) -> None:
    payload = canonical_json_bytes(dict(document))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_bytes(payload)
        temporary.replace(path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ControlStoreError("control_seal_write_failed", "Control DB external seal could not be written") from exc


def _seal_document(*, state: str, instance_id: str, user_version: int, layout_digest: str | None) -> dict[str, Any]:
    if state not in {"INITIALIZING", "SEALED"}:
        _fail("control_seal_state_invalid", "Control DB external seal state is unsupported")
    document: dict[str, Any] = {
        "schema": CONTROL_SEAL_SCHEMA,
        "state": state,
        "instance_id": instance_id,
        "store_id": expected_identity()["store_id"],
        "generation_id": expected_identity()["generation_id"],
        "config_generation": expected_identity()["config_generation"],
        "schema_checksum": CONTROL_SCHEMA_CHECKSUM,
        "user_version": user_version,
    }
    if layout_digest is not None:
        document["layout_digest"] = layout_digest
    document["seal_digest"] = semantic_digest(document)
    return document


def _validate_seal(document: Mapping[str, Any], *, state: str, user_version: int, layout_digest: str | None = None) -> str:
    supplied = dict(document)
    seal_digest = supplied.pop("seal_digest", None)
    if seal_digest != semantic_digest(supplied):
        _fail("control_seal_integrity_failure", "Control DB external seal digest differs")
    if supplied.get("schema") != CONTROL_SEAL_SCHEMA or supplied.get("state") != state:
        _fail("control_seal_mismatch", "Control DB external seal state differs")
    if supplied.get("store_id") != expected_identity()["store_id"] or supplied.get("generation_id") != expected_identity()["generation_id"] or supplied.get("config_generation") != expected_identity()["config_generation"] or supplied.get("schema_checksum") != CONTROL_SCHEMA_CHECKSUM:
        _fail("control_seal_mismatch", "Control DB external seal identity differs")
    if supplied.get("user_version") != user_version:
        _fail("control_seal_mismatch", "Control DB external seal user_version differs")
    if state == "SEALED" and layout_digest is not None and supplied.get("layout_digest") != layout_digest:
        _fail("control_seal_mismatch", "Control DB external seal layout differs")
    instance_id = supplied.get("instance_id")
    if not isinstance(instance_id, str) or not instance_id:
        _fail("control_seal_mismatch", "Control DB external seal instance identity is missing")
    return instance_id


def prepare_control_store(root: str | Path) -> Path:
    """Open the canonical server DB through an explicit lifecycle boundary.

    A missing DB is the only state eligible for INITIALIZE_NEW.  The durable
    external INITIALIZING seal makes a crash during composition observable;
    the next OPEN_EXISTING refuses to reinterpret it as a fresh store.
    """

    root_path = Path(root).expanduser().absolute()
    database_path = assert_database_path(root_path, root_path / CONTROL_DATABASE_RELATIVE_PATH)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    seal = seal_path_for_database(database_path)
    existed = database_path.exists()
    connection = sqlite3.connect(str(database_path), timeout=CONTROL_BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    try:
        configure_connection(connection)
        if not existed:
            identity = ensure_identity(
                connection,
                lifecycle="INITIALIZE_NEW",
                seal_path=seal,
                initialize=True,
            )
            document = _seal_document(
                state="INITIALIZING",
                instance_id="control-" + uuid.uuid4().hex,
                user_version=0,
                layout_digest=None,
            )
            _write_seal(seal, document)
        else:
            ensure_identity(connection, lifecycle="OPEN_EXISTING", seal_path=seal)
        return database_path
    finally:
        try:
            connection.close()
        except sqlite3.DatabaseError:
            pass


def finalize_control_store(connection: sqlite3.Connection) -> str:
    """Seal a fully composed Control DB after every typed schema is present."""

    layout_digest = validate_current_layout_identity(connection)
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version != CONTROL_DB_USER_VERSION:
        _fail("control_user_version_mismatch", "Control DB cannot be sealed at an unsupported version")
    seal = seal_path_for_database(_database_path(connection))
    document = _read_seal(seal)
    if document is None:
        # Direct typed stores remain usable in focused unit fixtures; the
        # canonical composition root always creates the seal first.
        return layout_digest
    instance_id = _validate_seal(document, state="INITIALIZING", user_version=0)
    _write_seal(
        seal,
        _seal_document(
            state="SEALED",
            instance_id=instance_id,
            user_version=CONTROL_DB_USER_VERSION,
            layout_digest=layout_digest,
        ),
    )
    return layout_digest


def _schema_checksum(*, include_m4b: bool = True, include_m4c: bool = True, include_n4: bool = True) -> str:
    tables = {
        "m2": ["m2b_accepted_bindings"],
        "m3": [
            "m3a_submissions",
            "m3a_tasks",
            "m3a_intent_revisions",
            "m3a_consumer_bindings",
            "m3c_kill_switch",
        ],
        "m4": [
            "m4a_sequence",
            "m4a_work_items",
            "m4a_runs",
            "m4a_waits",
            "m4a_leases",
            "m4a_resource_claims",
            "m4a_transition_facts",
        ],
    }
    if include_m4b:
        tables["m4b"] = [
            "m4b_candidate_effects",
            "m4b_candidate_paths",
        ]
    if include_m4c:
        tables["m4c"] = [
            "m4c_evidence_records",
            "m4c_evaluations",
            "m4c_dispositions",
            "m4c_disposition_heads",
            "m4c_evidence_gaps",
        ]
    if include_n4:
        tables["n4"] = [
            "n4_publications",
            "n4_consumer_bindings",
            "n4_consumer_cursors",
            "n4_presentation_witnesses",
            "n4_resume_capsules",
        ]
    return semantic_digest(
        {
            "schema": CONTROL_IDENTITY_SCHEMA,
            "tables": tables,
        }
    )


CONTROL_SCHEMA_CHECKSUM = _schema_checksum()
CONTROL_PRE_N4_SCHEMA_CHECKSUM = _schema_checksum(include_n4=False)
CONTROL_PRE_N3_SCHEMA_CHECKSUM = _schema_checksum(include_m4c=False, include_n4=False)
CONTROL_PRE_N2_SCHEMA_CHECKSUM = _schema_checksum(include_m4b=False, include_m4c=False, include_n4=False)


def expected_identity() -> dict[str, str]:
    return {
        "identity_schema": CONTROL_IDENTITY_SCHEMA,
        "store_id": "devmaster.bdb.vnext.control-store",
        "generation_id": GENERATION_ID,
        "config_generation": CONFIG_GENERATION,
        "migration_id": CONTROL_MIGRATION_ID,
        "schema_checksum": CONTROL_SCHEMA_CHECKSUM,
    }


def assert_database_path(root: str | Path, database_path: str | Path) -> Path:
    """Require the canonical physical path; old milestone DBs are retired."""

    root_path = Path(root).expanduser().absolute()
    expected = (root_path / CONTROL_DATABASE_RELATIVE_PATH).absolute()
    actual = Path(database_path).expanduser().absolute()
    if actual != expected:
        _fail(
            "retired_control_store_path",
            "vNext server writers may open only control/control.db",
        )
    return expected


def configure_connection(connection: sqlite3.Connection, *, busy_timeout_ms: int = CONTROL_BUSY_TIMEOUT_MS) -> None:
    if not isinstance(busy_timeout_ms, int) or not 1 <= busy_timeout_ms <= 10_000:
        _fail("invalid_busy_timeout", "Control DB busy timeout is out of bounds")
    try:
        connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")
        mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
        connection.execute("PRAGMA synchronous=FULL")
    except sqlite3.DatabaseError as exc:
        raise ControlStoreError("control_sqlite_settings_failed", "Control DB settings could not be applied") from exc
    if mode != "wal":
        _fail("control_wal_unavailable", "the vNext Control DB requires WAL journaling")


def ensure_identity(
    connection: sqlite3.Connection,
    *,
    lifecycle: str = "DIRECT",
    seal_path: str | Path | None = None,
    initialize: bool = False,
) -> dict[str, str]:
    """Initialize a new DB or validate a sealed current DB before mutation.

    ``user_version=0`` is the only initialization/migration state in which the
    domain stores may create their owned tables. A sealed v2/v3 database is
    validated *before* any ``CREATE IF NOT EXISTS`` statement can hide missing
    canonical state. The v2 -> v3 migration changes only this opening contract:
    the prior layout is proven intact before the version is advanced.
    """

    expected = expected_identity()
    if lifecycle not in {"DIRECT", "INITIALIZE_NEW", "OPEN_EXISTING"}:
        _fail("control_lifecycle_invalid", "Control DB lifecycle is unsupported")
    seal = Path(seal_path).expanduser().absolute() if seal_path is not None else None
    if seal is None:
        seal = seal_path_for_database(_database_path(connection))
    direct_seal = _read_seal(seal) if lifecycle == "DIRECT" else None
    if lifecycle == "OPEN_EXISTING" and seal is None:
        _fail("control_seal_required", "OPEN_EXISTING requires an external Control DB seal")
    if lifecycle == "INITIALIZE_NEW" and not initialize:
        _fail("control_initialization_required", "fresh Control DB initialization requires explicit authority")
    if lifecycle == "OPEN_EXISTING":
        document = _read_seal(seal)  # type: ignore[arg-type]
        if document is None:
            _fail("control_seal_missing", "existing Control DB has no external seal")
        if document.get("state") != "SEALED":
            _fail("control_initialization_incomplete", "existing Control DB has an incomplete initialization seal")
        # The user_version/layout checks below are deliberately still run;
        # the seal is an independent prerequisite, not a replacement for DB
        # structural verification.
        seal_user_version = document.get("user_version")
        if seal_user_version not in {CONTROL_PREVIOUS_USER_VERSION, CONTROL_DB_USER_VERSION}:
            _fail("control_seal_mismatch", "existing Control DB seal has an unsupported version")
        _validate_seal(document, state="SEALED", user_version=int(seal_user_version))
    elif lifecycle == "INITIALIZE_NEW":
        existing = _read_seal(seal) if seal is not None else None
        if existing is not None:
            _fail("control_seal_mismatch", "fresh Control DB cannot reuse an existing external seal")
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in {0, CONTROL_PREVIOUS_USER_VERSION, CONTROL_DB_USER_VERSION}:
            _fail("control_user_version_mismatch", "Control DB user_version is not supported")
    except sqlite3.DatabaseError as exc:
        raise ControlStoreError("control_user_version_read_failed", "Control DB user_version could not be verified") from exc

    if lifecycle == "OPEN_EXISTING" and version == 0:
        _fail("control_user_version_mismatch", "an existing sealed Control DB cannot be downgraded to user_version=0")
    if lifecycle == "DIRECT" and direct_seal is not None and direct_seal.get("state") == "SEALED" and version == 0:
        legacy_shape = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='m4a_work_items'"
        ).fetchone()
        legacy_sql = "" if legacy_shape is None or legacy_shape[0] is None else str(legacy_shape[0]).upper()
        # The only direct version-0 exception is the explicit, bounded N1
        # lifecycle migration shape. A current schema forced to zero has no
        # legacy vocabulary and remains fail-closed.
        if "TERMINAL" not in legacy_sql and "CANCELLED" not in legacy_sql:
            _fail("control_user_version_mismatch", "sealed Control DB cannot be downgraded to user_version=0")
    if version in {CONTROL_PREVIOUS_USER_VERSION, CONTROL_DB_USER_VERSION}:
        # Intentionally read-only and before any store-owned CREATE statement.
        layout_digest = validate_current_layout_identity(connection)
        if lifecycle == "OPEN_EXISTING":
            document = _read_seal(seal)  # type: ignore[arg-type]
            if document is None:
                _fail("control_seal_missing", "existing Control DB has no external seal")
            _validate_seal(document, state="SEALED", user_version=int(document["user_version"]), layout_digest=layout_digest)
        try:
            rows = {
                str(row[0]): str(row[1])
                for row in connection.execute(
                    f"SELECT key,value FROM {CONTROL_METADATA_TABLE} ORDER BY key"
                ).fetchall()
            }
        except sqlite3.DatabaseError as exc:
            raise ControlStoreError("control_identity_read_failed", "Control DB identity could not be read") from exc
        if rows != expected:
            _fail("control_identity_mismatch", "Control DB identity differs from the canonical vNext generation")
        if version == CONTROL_PREVIOUS_USER_VERSION:
            try:
                begin_control_write(connection)
                validate_current_layout_identity(connection)
                connection.execute(f"PRAGMA user_version={CONTROL_DB_USER_VERSION}")
                commit_control_write(connection)
                if seal is not None:
                    document = _read_seal(seal)
                    if document is None:
                        _fail("control_seal_missing", "Control DB migration lost its external seal")
                    updated = _seal_document(
                        state="SEALED",
                        instance_id=str(document["instance_id"]),
                        user_version=CONTROL_DB_USER_VERSION,
                        layout_digest=validate_current_layout_identity(connection),
                    )
                    _write_seal(seal, updated)
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise
        return expected

    if lifecycle == "OPEN_EXISTING":
        _fail("control_user_version_mismatch", "existing Control DB is not a supported sealed version")
    # version 0 remains visibly unsealed while all typed stores install their
    # schemas.  Identity installation itself must be one atomic write: with
    # autocommit, a concurrent opener could observe a partially inserted
    # metadata set and incorrectly classify the DB as foreign.
    # A fully initialized identity is read-only on open; this fast path is
    # important for callers that attach while another process holds a domain
    # write lock.  Only missing/legacy identity requires the initialization
    # writer below.
    try:
        metadata_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (CONTROL_METADATA_TABLE,),
        ).fetchone() is not None
        if metadata_exists:
            observed = {
                str(row[0]): str(row[1])
                for row in connection.execute(
                    f"SELECT key,value FROM {CONTROL_METADATA_TABLE} ORDER BY key"
                ).fetchall()
            }
            if observed == expected:
                return expected
    except sqlite3.DatabaseError as exc:
        raise ControlStoreError("control_identity_read_failed", "Control DB identity could not be read") from exc

    write_started = False
    try:
        begin_control_write(connection, retries=3, retry_delay_seconds=0.01)
        write_started = True
        connection.execute(
            f"CREATE TABLE IF NOT EXISTS {CONTROL_METADATA_TABLE} ("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        rows = {
            str(row[0]): str(row[1])
            for row in connection.execute(
                f"SELECT key,value FROM {CONTROL_METADATA_TABLE} ORDER BY key"
            ).fetchall()
        }
        if rows and rows != expected:
            legacy_n3 = dict(expected)
            legacy_n3["schema_checksum"] = CONTROL_PRE_N3_SCHEMA_CHECKSUM
            legacy_n4 = dict(expected)
            legacy_n4["schema_checksum"] = CONTROL_PRE_N4_SCHEMA_CHECKSUM
            legacy_n2 = dict(expected)
            legacy_n2["schema_checksum"] = CONTROL_PRE_N2_SCHEMA_CHECKSUM
            if rows != legacy_n4 and rows != legacy_n3 and rows != legacy_n2:
                _fail("control_identity_mismatch", "Control DB identity differs from the canonical vNext generation")
            connection.execute(
                f"UPDATE {CONTROL_METADATA_TABLE} SET value=? WHERE key='schema_checksum'",
                (CONTROL_SCHEMA_CHECKSUM,),
            )
            rows["schema_checksum"] = CONTROL_SCHEMA_CHECKSUM
        if not rows:
            connection.executemany(
                f"INSERT INTO {CONTROL_METADATA_TABLE}(key,value) VALUES (?,?)",
                tuple(expected.items()),
            )
        commit_control_write(connection)
        write_started = False
    except ControlStoreError:
        if write_started and connection.in_transaction:
            connection.rollback()
        raise
    except sqlite3.DatabaseError as exc:
        if write_started and connection.in_transaction:
            connection.rollback()
        raise ControlStoreError("control_identity_write_failed", "Control DB identity could not be initialized") from exc
    if lifecycle == "DIRECT" and _read_seal(seal) is None:
        _write_seal(
            seal,
            _seal_document(
                state="INITIALIZING",
                instance_id="control-" + uuid.uuid4().hex,
                user_version=0,
                layout_digest=None,
            ),
        )
    return expected


_REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "m3a_tasks": ("task_id", "submission_key", "intent_revision_id"),
    "m4a_work_items": ("work_id", "task_id", "disposition", "state_version"),
    "m4a_runs": ("run_id", "work_id", "outcome", "effect_certainty"),
    "m4b_candidate_effects": ("candidate_id", "base_view_json", "workspace_root", "manifest_digest"),
    "m4c_evidence_records": ("evidence_id", "candidate_view_id", "raw_ref_json"),
    "m4c_evaluations": ("evaluation_id", "evidence_id", "applicability"),
    "n4_publications": ("publication_id", "task_id", "result_ref_json", "sequence"),
}


def _layout_digest(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        "SELECT type,name,COALESCE(sql,'') FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' AND name<>? ORDER BY type,name",
        (CONTROL_LAYOUT_TABLE,),
    ).fetchall()
    return semantic_digest({"schema": CONTROL_IDENTITY_SCHEMA, "objects": [list(row) for row in rows]})


def validate_current_layout_identity(connection: sqlite3.Connection) -> str:
    """Read-only validation for an already sealed current-version database."""

    try:
        metadata = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (CONTROL_METADATA_TABLE,),
        ).fetchone()
        layout = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (CONTROL_LAYOUT_TABLE,),
        ).fetchone()
        if metadata is None:
            _fail("control_identity_missing", "Control DB identity table is missing")
        if layout is None:
            _fail("control_layout_missing", "Control DB layout identity table is missing")
        for table, required in _REQUIRED_COLUMNS.items():
            row = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if row is None:
                _fail("control_layout_missing", f"Control DB table is missing: {table}")
            columns = {str(item[1]) for item in connection.execute(f"PRAGMA table_info({table})").fetchall()}
            if not set(required) <= columns:
                _fail("control_layout_mismatch", f"Control DB table columns differ: {table}")
        digest = _layout_digest(connection)
        existing = connection.execute(
            f"SELECT digest FROM {CONTROL_LAYOUT_TABLE} WHERE layout_id=1"
        ).fetchone()
        if existing is None or str(existing[0]) != digest:
            _fail("control_layout_mismatch", "Control DB structural fingerprint differs")
        return digest
    except sqlite3.DatabaseError as exc:
        if isinstance(exc, ControlStoreError):
            raise
        raise ControlStoreError("control_layout_read_failed", "Control DB layout could not be verified") from exc


def ensure_layout_identity(connection: sqlite3.Connection) -> str:
    """Seal a version-0 initialization or validate an existing sealed layout."""

    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version in {CONTROL_PREVIOUS_USER_VERSION, CONTROL_DB_USER_VERSION}:
            ensure_identity(connection)
            return validate_current_layout_identity(connection)
        if version != 0:
            _fail("control_user_version_mismatch", "Control DB user_version is not supported")
        connection.execute(
            f"CREATE TABLE IF NOT EXISTS {CONTROL_LAYOUT_TABLE} "
            "(layout_id INTEGER PRIMARY KEY CHECK(layout_id=1), digest TEXT NOT NULL)"
        )
        for table, required in _REQUIRED_COLUMNS.items():
            row = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if row is None:
                _fail("control_layout_missing", f"Control DB table is missing: {table}")
            columns = {str(item[1]) for item in connection.execute(f"PRAGMA table_info({table})").fetchall()}
            if not set(required) <= columns:
                _fail("control_layout_mismatch", f"Control DB table columns differ: {table}")
        digest = _layout_digest(connection)
        existing = connection.execute(f"SELECT digest FROM {CONTROL_LAYOUT_TABLE} WHERE layout_id=1").fetchone()
        if existing is None:
            begin_control_write(connection)
            try:
                connection.execute(f"INSERT INTO {CONTROL_LAYOUT_TABLE}(layout_id,digest) VALUES (1,?)", (digest,))
                connection.execute(f"PRAGMA user_version={CONTROL_DB_USER_VERSION}")
                commit_control_write(connection)
            except Exception:
                rollback_control_write(connection)
                raise
        elif str(existing[0]) != digest:
            _fail("control_layout_mismatch", "Control DB structural fingerprint differs")
        else:
            connection.execute(f"PRAGMA user_version={CONTROL_DB_USER_VERSION}")
            commit_control_write(connection)
        return finalize_control_store(connection)
    except sqlite3.DatabaseError as exc:
        if isinstance(exc, ControlStoreError):
            raise
        raise ControlStoreError("control_layout_read_failed", "Control DB layout could not be verified") from exc


def read_identity(connection: sqlite3.Connection) -> dict[str, str]:
    try:
        rows = {
            str(row[0]): str(row[1])
            for row in connection.execute(
                f"SELECT key,value FROM {CONTROL_METADATA_TABLE} ORDER BY key"
            ).fetchall()
        }
    except sqlite3.DatabaseError as exc:
        raise ControlStoreError("control_identity_read_failed", "Control DB identity could not be read") from exc
    expected = expected_identity()
    if rows != expected:
        _fail("control_identity_mismatch", "Control DB identity differs from the canonical vNext generation")
    return rows


def config_digest(config_path: str | Path) -> str:
    path = Path(config_path).expanduser().absolute()
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ControlStoreError("control_config_missing", "vNext Control DB config identity is missing") from exc
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def backup_identity(connection: sqlite3.Connection, *, config_path: str | Path) -> dict[str, Any]:
    identity = read_identity(connection)
    document: dict[str, Any] = {
        "schema": CONTROL_IDENTITY_SCHEMA,
        "store_id": identity["store_id"],
        "generation_id": identity["generation_id"],
        "config_generation": identity["config_generation"],
        "migration_id": identity["migration_id"],
        "schema_checksum": identity["schema_checksum"],
        "config_sha256": config_digest(config_path),
    }
    document["identity_sha256"] = semantic_digest(document)
    return document


def validate_backup_identity(value: Mapping[str, Any], *, connection: sqlite3.Connection, config_path: str | Path) -> None:
    if not isinstance(value, Mapping):
        _fail("backup_control_identity_invalid", "backup Control DB identity is not an object")
    required = {
        "schema",
        "store_id",
        "generation_id",
        "config_generation",
        "migration_id",
        "schema_checksum",
        "config_sha256",
        "identity_sha256",
    }
    if set(value) != required or value.get("schema") != CONTROL_IDENTITY_SCHEMA:
        _fail("backup_control_identity_invalid", "backup Control DB identity fields differ")
    supplied = dict(value)
    digest = supplied.pop("identity_sha256")
    if digest != semantic_digest(supplied):
        _fail("backup_control_identity_mismatch", "backup Control DB identity digest differs")
    expected = backup_identity(connection, config_path=config_path)
    if dict(value) != expected:
        _fail("backup_control_identity_mismatch", "restored Control DB/config identity differs")


def validate_identity_document(value: Mapping[str, Any], *, config_sha256: str) -> None:
    """Validate a v2 manifest identity without opening (and changing) its WAL pair."""

    if not isinstance(value, Mapping):
        _fail("backup_control_identity_invalid", "backup Control DB identity is not an object")
    required = {
        "schema",
        "store_id",
        "generation_id",
        "config_generation",
        "migration_id",
        "schema_checksum",
        "config_sha256",
        "identity_sha256",
    }
    if set(value) != required or value.get("schema") != CONTROL_IDENTITY_SCHEMA:
        _fail("backup_control_identity_invalid", "backup Control DB identity fields differ")
    supplied = dict(value)
    digest = supplied.pop("identity_sha256")
    if digest != semantic_digest(supplied):
        _fail("backup_control_identity_mismatch", "backup Control DB identity digest differs")
    expected = expected_identity()
    expected_document = {
        "schema": CONTROL_IDENTITY_SCHEMA,
        "store_id": expected["store_id"],
        "generation_id": expected["generation_id"],
        "config_generation": expected["config_generation"],
        "migration_id": expected["migration_id"],
        "schema_checksum": expected["schema_checksum"],
    }
    if any(supplied.get(key) != expected_value for key, expected_value in expected_document.items()):
        _fail("backup_control_identity_mismatch", "backup Control DB identity differs")
    if supplied.get("config_sha256") != config_sha256:
        _fail("backup_control_identity_mismatch", "backup config identity differs")


__all__ = [
    "CONTROL_BUSY_TIMEOUT_MS",
    "CONTROL_WRITE_RETRIES",
    "CONTROL_SEAL_SCHEMA",
    "CONTROL_SEAL_FILENAME",
    "CONTROL_DATABASE_RELATIVE_PATH",
    "CONTROL_IDENTITY_SCHEMA",
    "CONTROL_METADATA_TABLE",
    "CONTROL_LAYOUT_TABLE",
    "CONTROL_DB_USER_VERSION",
    "CONTROL_PREVIOUS_USER_VERSION",
    "CONTROL_MIGRATION_ID",
    "CONTROL_SCHEMA_CHECKSUM",
    "CONTROL_PRE_N4_SCHEMA_CHECKSUM",
    "CONTROL_PRE_N2_SCHEMA_CHECKSUM",
    "CONTROL_PRE_N3_SCHEMA_CHECKSUM",
    "ControlStoreError",
    "begin_control_write",
    "commit_control_write",
    "finalize_control_store",
    "is_sqlite_busy",
    "prepare_control_store",
    "rollback_control_write",
    "seal_path_for_database",
    "assert_database_path",
    "backup_identity",
    "config_digest",
    "configure_connection",
    "ensure_identity",
    "ensure_layout_identity",
    "validate_current_layout_identity",
    "expected_identity",
    "read_identity",
    "validate_backup_identity",
    "validate_identity_document",
]

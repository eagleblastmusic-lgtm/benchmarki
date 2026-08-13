"""Shared identity and connection-floor helpers for the vNext Control DB.

The database is physically shared by the M2, M3 and M4 repositories, but
those repositories retain typed ownership of their own tables.  This module
only owns the small cross-domain identity table and SQLite safety floor; it is
not a generic SQL repository and exposes no lifecycle mutation API.
"""

from __future__ import annotations

import hashlib
import sqlite3
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


def ensure_identity(connection: sqlite3.Connection) -> dict[str, str]:
    """Initialize a new DB or validate a sealed current DB before mutation.

    ``user_version=0`` is the only initialization/migration state in which the
    domain stores may create their owned tables. A sealed v2/v3 database is
    validated *before* any ``CREATE IF NOT EXISTS`` statement can hide missing
    canonical state. The v2 -> v3 migration changes only this opening contract:
    the prior layout is proven intact before the version is advanced.
    """

    expected = expected_identity()
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in {0, CONTROL_PREVIOUS_USER_VERSION, CONTROL_DB_USER_VERSION}:
            _fail("control_user_version_mismatch", "Control DB user_version is not supported")
    except sqlite3.DatabaseError as exc:
        raise ControlStoreError("control_user_version_read_failed", "Control DB user_version could not be verified") from exc

    if version in {CONTROL_PREVIOUS_USER_VERSION, CONTROL_DB_USER_VERSION}:
        # Intentionally read-only and before any store-owned CREATE statement.
        validate_current_layout_identity(connection)
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
                connection.execute("BEGIN IMMEDIATE")
                validate_current_layout_identity(connection)
                connection.execute(f"PRAGMA user_version={CONTROL_DB_USER_VERSION}")
                connection.commit()
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise
        return expected

    # version 0 remains visibly unsealed while all typed stores install their
    # schemas. ensure_layout_identity is the single finalization point.
    try:
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
    except sqlite3.DatabaseError as exc:
        raise ControlStoreError("control_identity_read_failed", "Control DB identity could not be read") from exc
    if rows and rows != expected:
        legacy_n3 = dict(expected)
        legacy_n3["schema_checksum"] = CONTROL_PRE_N3_SCHEMA_CHECKSUM
        legacy_n4 = dict(expected)
        legacy_n4["schema_checksum"] = CONTROL_PRE_N4_SCHEMA_CHECKSUM
        legacy_n2 = dict(expected)
        legacy_n2["schema_checksum"] = CONTROL_PRE_N2_SCHEMA_CHECKSUM
        if rows != legacy_n4 and rows != legacy_n3 and rows != legacy_n2:
            _fail("control_identity_mismatch", "Control DB identity differs from the canonical vNext generation")
        try:
            connection.execute(
                f"UPDATE {CONTROL_METADATA_TABLE} SET value=? WHERE key='schema_checksum'",
                (CONTROL_SCHEMA_CHECKSUM,),
            )
            connection.commit()
            rows["schema_checksum"] = CONTROL_SCHEMA_CHECKSUM
        except sqlite3.DatabaseError as exc:
            raise ControlStoreError("control_identity_write_failed", "Control DB schema identity could not be upgraded") from exc
    if not rows:
        try:
            connection.executemany(
                f"INSERT INTO {CONTROL_METADATA_TABLE}(key,value) VALUES (?,?)",
                tuple(expected.items()),
            )
            connection.commit()
        except sqlite3.DatabaseError as exc:
            raise ControlStoreError("control_identity_write_failed", "Control DB identity could not be initialized") from exc
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
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(f"INSERT INTO {CONTROL_LAYOUT_TABLE}(layout_id,digest) VALUES (1,?)", (digest,))
                connection.execute(f"PRAGMA user_version={CONTROL_DB_USER_VERSION}")
                connection.commit()
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise
        elif str(existing[0]) != digest:
            _fail("control_layout_mismatch", "Control DB structural fingerprint differs")
        else:
            connection.execute(f"PRAGMA user_version={CONTROL_DB_USER_VERSION}")
            connection.commit()
        return digest
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

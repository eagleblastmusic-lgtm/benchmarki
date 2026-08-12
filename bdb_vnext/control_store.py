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


def _schema_checksum(*, include_m4b: bool = True) -> str:
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
    return semantic_digest(
        {
            "schema": CONTROL_IDENTITY_SCHEMA,
            "tables": tables,
        }
    )


CONTROL_SCHEMA_CHECKSUM = _schema_checksum()
CONTROL_PRE_N2_SCHEMA_CHECKSUM = _schema_checksum(include_m4b=False)


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
    """Create/validate only the fixed cross-domain identity table."""

    expected = expected_identity()
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
        legacy = dict(expected)
        legacy["schema_checksum"] = CONTROL_PRE_N2_SCHEMA_CHECKSUM
        if rows != legacy:
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
    "CONTROL_MIGRATION_ID",
    "CONTROL_SCHEMA_CHECKSUM",
    "CONTROL_PRE_N2_SCHEMA_CHECKSUM",
    "ControlStoreError",
    "assert_database_path",
    "backup_identity",
    "config_digest",
    "configure_connection",
    "ensure_identity",
    "expected_identity",
    "read_identity",
    "validate_backup_identity",
    "validate_identity_document",
]

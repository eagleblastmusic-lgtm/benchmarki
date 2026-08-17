"""Read-only canonical vNext projection boundary for Control Center CC1.

This module is intentionally a projection/client boundary, not a writer.  It
observes only an already-existing, externally sealed vNext Control DB.  A
missing DB is represented as the expected build-only OFF state.  Corrupt or
partial identity fails closed.  No schema creation, migration, lifecycle
transition, resume, retry or external effect is available from this surface.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, NoReturn

from bdb_shared.evidence import semantic_digest
from bdb_vnext.composition import GENERATION_ID, default_vnext_runtime_root
from bdb_vnext.control_store import (
    CONTROL_BUSY_TIMEOUT_MS,
    CONTROL_DATABASE_RELATIVE_PATH,
    ControlStoreError,
    _preflight_existing_control_store,
    _read_seal,
    seal_path_for_database,
)


CC1_QUERY_SCHEMA = "bdb-vnext-cc1-control-center-query-v1"
CC1_WORK_SCHEMA = "bdb-vnext-cc1-work-projection-v1"
CC1_AUTHORITY_ID = "devmaster.bdb.vnext.control-center-query"
CC1_ACTION_REASON = "cc1_read_only"
CC1_MAX_WORK_ITEMS = 500
SYSTEM_STATES = frozenset({"OFF", "ON", "PAUSED", "DEGRADED"})


class ControlCenterQueryError(RuntimeError):
    """Typed, sanitized CC1 read failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> NoReturn:
    raise ControlCenterQueryError(code, message)


def _json_mapping(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    try:
        raw = bytes(value) if isinstance(value, (bytes, bytearray, memoryview)) else str(value).encode("utf-8")
        document = json.loads(raw.decode("utf-8"))
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        _fail("projection_corrupt", "canonical Control DB contains an invalid JSON projection")
    if not isinstance(document, dict):
        _fail("projection_corrupt", "canonical Control DB projection must be an object")
    return {str(key): item for key, item in document.items()}


def _read_only_connection(database_path: Path) -> sqlite3.Connection:
    wal_present = database_path.with_name(database_path.name + "-wal").exists()
    shm_present = database_path.with_name(database_path.name + "-shm").exists()
    query = "mode=ro" if wal_present or shm_present else "mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(
            f"{database_path.as_uri()}?{query}",
            uri=True,
            timeout=CONTROL_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute(f"PRAGMA busy_timeout={CONTROL_BUSY_TIMEOUT_MS}")
        return connection
    except sqlite3.DatabaseError as exc:
        raise ControlCenterQueryError(
            "control_store_read_failed",
            "vNext Control DB could not be opened for read-only Control Center projection",
        ) from exc


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {str(key): row[key] for key in row.keys()}


def _base_repository_projection(candidate: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if candidate is None:
        return None
    base = _json_mapping(candidate.get("base_view_json"))
    candidate_view = _json_mapping(candidate.get("candidate_view_json"))
    if base is None and candidate_view is None:
        return None
    return {
        "base": base,
        "candidate": candidate_view,
        "base_tree_digest": candidate.get("base_tree_digest"),
        "planned_tree_digest": candidate.get("planned_tree_digest"),
        "observed_tree_digest": candidate.get("observed_tree_digest"),
        "manifest_digest": candidate.get("manifest_digest"),
    }


@dataclass(frozen=True)
class ActionPredicate:
    action: str
    enabled: bool = False
    reason_code: str = CC1_ACTION_REASON
    explanation: str = "CC1 is a read-only canonical vNext projection; mutation is unavailable."

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "enabled": self.enabled,
            "reason_code": self.reason_code,
            "explanation": self.explanation,
        }


@dataclass(frozen=True)
class ControlCenterWorkProjection:
    work: Mapping[str, Any]
    effect: Mapping[str, Any] | None
    evidence: Mapping[str, Any] | None
    repository: Mapping[str, Any] | None
    publication: Mapping[str, Any] | None

    @property
    def work_id(self) -> str:
        return str(self.work["work_id"])

    @property
    def task_id(self) -> str:
        return str(self.work["task_id"])

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "schema": CC1_WORK_SCHEMA,
            "authority": CC1_AUTHORITY_ID,
            "work": dict(self.work),
            "effect": dict(self.effect) if self.effect is not None else None,
            "evidence": dict(self.evidence) if self.evidence is not None else None,
            "repository": dict(self.repository) if self.repository is not None else None,
            "publication": dict(self.publication) if self.publication is not None else None,
        }
        payload["projection_digest"] = semantic_digest(payload)
        return payload


@dataclass(frozen=True)
class ControlCenterSnapshot:
    runtime_root: str
    system_state: str
    writer_state: str
    activation_state: str
    store_state: str
    store_instance_id: str | None
    works: tuple[ControlCenterWorkProjection, ...]
    action_predicates: tuple[ActionPredicate, ...]
    reason_code: str | None = None
    schema: str = CC1_QUERY_SCHEMA
    authority: str = CC1_AUTHORITY_ID
    generation: str = GENERATION_ID

    def __post_init__(self) -> None:
        if self.system_state not in SYSTEM_STATES:
            raise ValueError("unsupported Control Center system state")

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "schema": self.schema,
            "authority": self.authority,
            "generation": self.generation,
            "runtime_root": self.runtime_root,
            "status_vector": {
                "system": self.system_state,
                "writer": self.writer_state,
                "activation": self.activation_state,
                "control_store": self.store_state,
            },
            "store_instance_id": self.store_instance_id,
            "reason_code": self.reason_code,
            "works": [item.as_dict() for item in self.works],
            "actions": [item.as_dict() for item in self.action_predicates],
            "read_only": True,
            "legacy_fallback": False,
            "mutation_operations_invoked": 0,
        }
        payload["projection_digest"] = semantic_digest(payload)
        return payload


def _off_snapshot(root: Path) -> ControlCenterSnapshot:
    return ControlCenterSnapshot(
        runtime_root=str(root),
        system_state="OFF",
        writer_state="OFF",
        activation_state="OFF",
        store_state="ABSENT",
        store_instance_id=None,
        works=(),
        action_predicates=_read_only_actions(),
        reason_code="control_store_absent",
    )


def _read_only_actions() -> tuple[ActionPredicate, ...]:
    return tuple(
        ActionPredicate(action=action)
        for action in ("resume", "apply_effect", "publish", "activate", "deploy")
    )


def _candidate_for_work(connection: sqlite3.Connection, work_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT candidate_id,effect_id,work_id,task_id,state,effect_certainty,
               base_view_json,workspace_generation,config_digest,lease_id,fence,
               base_tree_digest,planned_tree_digest,observed_tree_digest,
               candidate_view_json,manifest_digest
        FROM m4b_candidate_effects
        WHERE work_id=?
        ORDER BY rowid DESC
        LIMIT 1
        """,
        (work_id,),
    ).fetchone()
    return _row_dict(row)


def _effect_projection(candidate: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if candidate is None:
        return None
    return {
        "candidate_id": candidate.get("candidate_id"),
        "effect_id": candidate.get("effect_id"),
        "state": candidate.get("state"),
        "effect_certainty": candidate.get("effect_certainty"),
        "lease_id": candidate.get("lease_id"),
        "fence": candidate.get("fence"),
        "workspace_generation": candidate.get("workspace_generation"),
        "config_digest": candidate.get("config_digest"),
    }


def _evidence_projection(
    connection: sqlite3.Connection,
    candidate: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if candidate is None:
        return None
    candidate_view_id = candidate.get("manifest_digest")
    if not candidate_view_id:
        candidate_view = _json_mapping(candidate.get("candidate_view_json"))
        if candidate_view is not None:
            candidate_view_id = candidate_view.get("view_id") or candidate_view.get("manifest_digest")
    if not isinstance(candidate_view_id, str) or not candidate_view_id:
        return None
    evidence = connection.execute(
        """
        SELECT evidence_id,request_id,primary_subject_kind,candidate_view_id,
               raw_digest,checker_id,checker_version,checker_code_digest,
               observation_started_at,observation_finished_at,completeness,
               applicability,status,created_at
        FROM m4c_evidence_records
        WHERE candidate_view_id=?
        ORDER BY created_at DESC,evidence_id DESC
        LIMIT 1
        """,
        (candidate_view_id,),
    ).fetchone()
    if evidence is None:
        return None
    record = _row_dict(evidence)
    assert record is not None
    evaluation = connection.execute(
        """
        SELECT evaluation_id,evidence_id,evaluator_id,evaluator_version,
               evaluator_code_digest,config_digest,result,applicability,created_at
        FROM m4c_evaluations
        WHERE evidence_id=?
        ORDER BY created_at DESC,evaluation_id DESC
        LIMIT 1
        """,
        (record["evidence_id"],),
    ).fetchone()
    result: dict[str, Any] = {"record": record, "evaluation": _row_dict(evaluation)}
    return result


def _publication_projection(connection: sqlite3.Connection, work_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT publication_id,request_id,task_id,work_id,intent_revision_id,
               result_digest,candidate_id,candidate_view_id,evidence_id,
               evaluation_id,disposition_id,consumer_id,consumer_kind,
               conversation_id,profile_id,generation,sequence,created_at
        FROM n4_publications
        WHERE work_id=?
        ORDER BY sequence DESC
        LIMIT 1
        """,
        (work_id,),
    ).fetchone()
    return _row_dict(row)


def _work_projection(connection: sqlite3.Connection, row: sqlite3.Row) -> ControlCenterWorkProjection:
    work = _row_dict(row)
    assert work is not None
    work_id = str(work["work_id"])
    candidate = _candidate_for_work(connection, work_id)
    return ControlCenterWorkProjection(
        work=work,
        effect=_effect_projection(candidate),
        evidence=_evidence_projection(connection, candidate),
        repository=_base_repository_projection(candidate),
        publication=_publication_projection(connection, work_id),
    )


def read_control_center_snapshot(root: str | Path | None = None) -> ControlCenterSnapshot:
    """Read one bounded canonical CC1 snapshot without mutating runtime state."""

    runtime_root = (
        Path(root).expanduser().absolute()
        if root is not None
        else default_vnext_runtime_root().expanduser().absolute()
    )
    database_path = runtime_root / CONTROL_DATABASE_RELATIVE_PATH
    seal_path = seal_path_for_database(database_path)
    if not database_path.exists() and not seal_path.exists():
        return _off_snapshot(runtime_root)
    if not database_path.exists():
        _fail("control_store_partial", "vNext Control DB seal exists without its database")
    try:
        seal = _read_seal(seal_path)
        if seal is None:
            _fail("control_seal_missing", "existing vNext Control DB has no external seal")
        if seal.get("state") != "SEALED":
            _fail("control_store_partial", "vNext Control DB initialization is incomplete")
        _preflight_existing_control_store(database_path, seal)
    except ControlStoreError as exc:
        raise ControlCenterQueryError(exc.code, str(exc)) from exc

    connection = _read_only_connection(database_path)
    before_changes = connection.total_changes
    try:
        rows = connection.execute(
            """
            SELECT work_id,task_id,kind,disposition,state_version,
                   created_order,updated_order,created_at,updated_at
            FROM m4a_work_items
            ORDER BY updated_order DESC,work_id
            LIMIT ?
            """,
            (CC1_MAX_WORK_ITEMS,),
        ).fetchall()
        works = tuple(_work_projection(connection, row) for row in rows)
        if connection.total_changes != before_changes:
            _fail("read_only_violation", "Control Center query unexpectedly mutated the Control DB")
        instance_id = str(seal.get("instance_id")) if seal.get("instance_id") else None
        return ControlCenterSnapshot(
            runtime_root=str(runtime_root),
            system_state="OFF",
            writer_state="OFF",
            activation_state="OFF",
            store_state="SEALED",
            store_instance_id=instance_id,
            works=works,
            action_predicates=_read_only_actions(),
            reason_code="cc1_build_only",
        )
    except sqlite3.DatabaseError as exc:
        raise ControlCenterQueryError(
            "control_store_read_failed",
            "vNext Control DB could not produce a canonical Control Center projection",
        ) from exc
    finally:
        try:
            connection.close()
        except sqlite3.DatabaseError:
            pass


__all__ = [
    "ActionPredicate",
    "CC1_ACTION_REASON",
    "CC1_AUTHORITY_ID",
    "CC1_QUERY_SCHEMA",
    "ControlCenterQueryError",
    "ControlCenterSnapshot",
    "ControlCenterWorkProjection",
    "SYSTEM_STATES",
    "read_control_center_snapshot",
]

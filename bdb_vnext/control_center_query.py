"""Read-only canonical vNext projection boundary for Control Center CC1.

CC1 observes only an already-existing, externally sealed vNext Control DB.
Missing state is explicit OFF; partial/corrupt state fails closed. WorkItem
lifecycle is consumed through the canonical M4a WorkItemQuery DTO rather than
being reinterpreted by the GUI or this projection adapter.
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
from bdb_vnext.m4a_read_query import M4aReadQueryError, ReadOnlyWorkKernelQuery

CC1_QUERY_SCHEMA = "bdb-vnext-cc1-control-center-query-v1"
CC1_WORK_SCHEMA = "bdb-vnext-cc1-work-projection-v1"
CC1_AUTHORITY_ID = "devmaster.bdb.vnext.control-center-query"
CC1_ACTION_REASON = "cc1_read_only"
CC1_MAX_WORK_ITEMS = 500
CC1_MAX_RELATED_RECORDS = 50
SYSTEM_STATES = frozenset({"OFF", "ON", "PAUSED", "DEGRADED"})

class ControlCenterQueryError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message); self.code = code

def _fail(code: str, message: str) -> NoReturn:
    raise ControlCenterQueryError(code, message)

def _json_mapping(value: object) -> dict[str, Any] | None:
    if value is None: return None
    try:
        raw = bytes(value) if isinstance(value, (bytes, bytearray, memoryview)) else str(value).encode("utf-8")
        document = json.loads(raw.decode("utf-8"))
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ControlCenterQueryError("projection_corrupt", "canonical Control DB contains an invalid JSON projection") from exc
    if not isinstance(document, dict): _fail("projection_corrupt", "canonical Control DB projection must be an object")
    return {str(key): item for key, item in document.items()}

def _read_only_connection(database_path: Path) -> sqlite3.Connection:
    wal = database_path.with_name(database_path.name + "-wal").exists()
    shm = database_path.with_name(database_path.name + "-shm").exists()
    query = "mode=ro" if wal or shm else "mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(f"{database_path.as_uri()}?{query}", uri=True, timeout=CONTROL_BUSY_TIMEOUT_MS / 1000, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute(f"PRAGMA busy_timeout={CONTROL_BUSY_TIMEOUT_MS}")
        return connection
    except sqlite3.DatabaseError as exc:
        raise ControlCenterQueryError("control_store_read_failed", "vNext Control DB could not be opened for read-only Control Center projection") from exc

def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else {str(key): row[key] for key in row.keys()}

def _rows_dict(rows: list[sqlite3.Row]) -> tuple[dict[str, Any], ...]:
    return tuple(item for item in (_row_dict(row) for row in rows) if item is not None)

@dataclass(frozen=True)
class ActionPredicate:
    action: str; enabled: bool = False; reason_code: str = CC1_ACTION_REASON
    explanation: str = "CC1 is read-only; canonical mutation authority is unavailable."
    def as_dict(self) -> dict[str, Any]:
        return {"action": self.action, "enabled": self.enabled, "reason_code": self.reason_code, "explanation": self.explanation}

@dataclass(frozen=True)
class ControlCenterWorkProjection:
    work: Mapping[str, Any]; effect: Mapping[str, Any] | None; evidence: Mapping[str, Any] | None; repository: Mapping[str, Any] | None; publication: Mapping[str, Any] | None
    @property
    def work_record(self) -> Mapping[str, Any]:
        value = self.work.get("work"); return value if isinstance(value, Mapping) else {}
    @property
    def work_id(self) -> str: return str(self.work_record["work_id"])
    @property
    def task_id(self) -> str: return str(self.work_record["task_id"])
    def as_dict(self) -> dict[str, Any]:
        payload = {"schema": CC1_WORK_SCHEMA, "authority": CC1_AUTHORITY_ID, "work": dict(self.work), "effect": dict(self.effect) if self.effect is not None else None, "evidence": dict(self.evidence) if self.evidence is not None else None, "repository": dict(self.repository) if self.repository is not None else None, "publication": dict(self.publication) if self.publication is not None else None}
        payload["projection_digest"] = semantic_digest(payload); return payload

@dataclass(frozen=True)
class ControlCenterSnapshot:
    runtime_root: str; system_state: str; writer_state: str; activation_state: str; store_state: str; store_instance_id: str | None; works: tuple[ControlCenterWorkProjection, ...]; action_predicates: tuple[ActionPredicate, ...]; reason_code: str | None = None; schema: str = CC1_QUERY_SCHEMA; authority: str = CC1_AUTHORITY_ID; generation: str = GENERATION_ID
    def __post_init__(self) -> None:
        if self.system_state not in SYSTEM_STATES: raise ValueError("unsupported Control Center system state")
    def as_dict(self) -> dict[str, Any]:
        payload = {"schema": self.schema, "authority": self.authority, "generation": self.generation, "runtime_root": self.runtime_root, "status_vector": {"system": self.system_state, "writer": self.writer_state, "activation": self.activation_state, "control_store": self.store_state}, "store_instance_id": self.store_instance_id, "reason_code": self.reason_code, "works": [item.as_dict() for item in self.works], "actions": [item.as_dict() for item in self.action_predicates], "read_only": True, "legacy_fallback": False, "mutation_operations_invoked": 0}
        payload["projection_digest"] = semantic_digest(payload); return payload

def _read_only_actions() -> tuple[ActionPredicate, ...]:
    return tuple(ActionPredicate(action=action) for action in ("resume", "apply_effect", "publish", "activate"))

def _off_snapshot(root: Path) -> ControlCenterSnapshot:
    return ControlCenterSnapshot(str(root), "OFF", "OFF", "OFF", "ABSENT", None, (), _read_only_actions(), "control_store_absent")

def _candidates_for_work(connection: sqlite3.Connection, work_id: str) -> tuple[dict[str, Any], ...]:
    rows = connection.execute("SELECT candidate_id,effect_id,work_id,task_id,state,effect_certainty,base_view_json,workspace_generation,config_digest,lease_id,fence,base_tree_digest,planned_tree_digest,observed_tree_digest,candidate_view_json,manifest_digest FROM m4b_candidate_effects WHERE work_id=? ORDER BY candidate_id LIMIT ?", (work_id, CC1_MAX_RELATED_RECORDS)).fetchall()
    return _rows_dict(rows)

def _effect_projection(candidates: tuple[dict[str, Any], ...]) -> dict[str, Any] | None:
    if not candidates: return None
    keys = ("candidate_id", "effect_id", "state", "effect_certainty", "lease_id", "fence", "workspace_generation", "config_digest")
    return {"selection": "ALL_CANONICAL_CANDIDATES", "items": [{key: candidate.get(key) for key in keys} for candidate in candidates]}

def _repository_projection(candidates: tuple[dict[str, Any], ...]) -> dict[str, Any] | None:
    if not candidates: return None
    return {"selection": "ALL_CANONICAL_CANDIDATES", "items": [{"candidate_id": candidate.get("candidate_id"), "base": _json_mapping(candidate.get("base_view_json")), "candidate": _json_mapping(candidate.get("candidate_view_json")), "base_tree_digest": candidate.get("base_tree_digest"), "planned_tree_digest": candidate.get("planned_tree_digest"), "observed_tree_digest": candidate.get("observed_tree_digest"), "manifest_digest": candidate.get("manifest_digest")} for candidate in candidates]}

def _candidate_view_id(candidate: Mapping[str, Any]) -> str | None:
    value = candidate.get("manifest_digest")
    if isinstance(value, str) and value: return value
    view = _json_mapping(candidate.get("candidate_view_json"))
    if view is None: return None
    value = view.get("view_id") or view.get("manifest_digest")
    return value if isinstance(value, str) and value else None

def _evidence_projection(connection: sqlite3.Connection, candidates: tuple[dict[str, Any], ...]) -> dict[str, Any] | None:
    items: list[dict[str, Any]] = []
    for candidate in candidates:
        view_id = _candidate_view_id(candidate)
        if view_id is None: continue
        evidence_rows = connection.execute("SELECT evidence_id,request_id,primary_subject_kind,candidate_view_id,raw_digest,checker_id,checker_version,checker_code_digest,observation_started_at,observation_finished_at,completeness,applicability,status,created_at FROM m4c_evidence_records WHERE candidate_view_id=? ORDER BY evidence_id LIMIT ?", (view_id, CC1_MAX_RELATED_RECORDS)).fetchall()
        for evidence in _rows_dict(evidence_rows):
            evaluations = connection.execute("SELECT evaluation_id,evidence_id,evaluator_id,evaluator_version,evaluator_code_digest,config_digest,result,applicability,created_at FROM m4c_evaluations WHERE evidence_id=? ORDER BY evaluation_id LIMIT ?", (evidence["evidence_id"], CC1_MAX_RELATED_RECORDS)).fetchall()
            items.append({"candidate_id": candidate.get("candidate_id"), "record": evidence, "evaluations": list(_rows_dict(evaluations))})
    return {"selection": "ALL_CANONICAL_EVIDENCE", "items": items} if items else None

def _publication_projection(connection: sqlite3.Connection, work_id: str) -> dict[str, Any] | None:
    rows = connection.execute("SELECT publication_id,request_id,task_id,work_id,intent_revision_id,result_digest,candidate_id,candidate_view_id,evidence_id,evaluation_id,disposition_id,consumer_id,consumer_kind,conversation_id,profile_id,generation,sequence,created_at FROM n4_publications WHERE work_id=? ORDER BY sequence DESC LIMIT ?", (work_id, CC1_MAX_RELATED_RECORDS)).fetchall()
    items = _rows_dict(rows); return {"selection": "ALL_CANONICAL_PUBLICATIONS", "items": list(items)} if items else None

def _work_projection(connection: sqlite3.Connection, work_query: Mapping[str, Any]) -> ControlCenterWorkProjection:
    record = work_query.get("work")
    if not isinstance(record, Mapping) or not isinstance(record.get("work_id"), str): _fail("projection_corrupt", "canonical M4a Work query has no WorkItem identity")
    work_id = str(record["work_id"]); candidates = _candidates_for_work(connection, work_id)
    return ControlCenterWorkProjection(work_query, _effect_projection(candidates), _evidence_projection(connection, candidates), _repository_projection(candidates), _publication_projection(connection, work_id))

def read_control_center_snapshot(root: str | Path | None = None) -> ControlCenterSnapshot:
    runtime_root = Path(root).expanduser().absolute() if root is not None else default_vnext_runtime_root().expanduser().absolute()
    database_path = runtime_root / CONTROL_DATABASE_RELATIVE_PATH; seal_path = seal_path_for_database(database_path)
    if not database_path.exists() and not seal_path.exists(): return _off_snapshot(runtime_root)
    if not database_path.exists(): _fail("control_store_partial", "vNext Control DB seal exists without its database")
    try:
        seal = _read_seal(seal_path)
        if seal is None: _fail("control_seal_missing", "existing vNext Control DB has no external seal")
        if seal.get("state") != "SEALED": _fail("control_store_partial", "vNext Control DB initialization is incomplete")
        _preflight_existing_control_store(database_path, seal)
    except ControlStoreError as exc:
        raise ControlCenterQueryError(exc.code, str(exc)) from exc
    connection = _read_only_connection(database_path); before_changes = connection.total_changes
    try:
        queries = ReadOnlyWorkKernelQuery(connection).catalog(limit=CC1_MAX_WORK_ITEMS)
        works = tuple(_work_projection(connection, item.as_dict()) for item in queries)
        if connection.total_changes != before_changes: _fail("read_only_violation", "Control Center query unexpectedly mutated the Control DB")
        instance_id = str(seal.get("instance_id")) if seal.get("instance_id") else None
        return ControlCenterSnapshot(str(runtime_root), "OFF", "OFF", "OFF", "SEALED", instance_id, works, _read_only_actions(), "cc1_build_only")
    except M4aReadQueryError as exc:
        raise ControlCenterQueryError(exc.code, str(exc)) from exc
    except sqlite3.DatabaseError as exc:
        raise ControlCenterQueryError("control_store_read_failed", "vNext Control DB could not produce a canonical Control Center projection") from exc
    finally:
        try: connection.close()
        except sqlite3.DatabaseError: pass

__all__ = ["ActionPredicate", "CC1_ACTION_REASON", "CC1_AUTHORITY_ID", "CC1_QUERY_SCHEMA", "ControlCenterQueryError", "ControlCenterSnapshot", "ControlCenterWorkProjection", "SYSTEM_STATES", "read_control_center_snapshot"]

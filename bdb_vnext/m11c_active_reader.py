"""Minimal read-only reader for the post-M11c external Bootstrap authority.

This module deliberately owns no activation, preparation, migration, route, or
writer effect.  It exists so the production Native Host can verify the one
external ACTIVE pointer without importing the broad M11c cutover implementation
and its migration-only dependencies.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NoReturn

from bdb_shared.evidence import canonical_json_bytes
from bdb_vnext.bootstrap import _absolute_path, _load_json
from bdb_vnext.composition import GENERATION_ID, RUNTIME_ID
from bdb_vnext.m11a_bootstrap_slots import (
    SLOT_STATE_SCHEMA,
    _load_manifest,
    _reobserve,
    _state_path,
    _supports,
    query_slot_authority,
)


SLOT_STATE_V2_SCHEMA = "bdb-vnext-bootstrap-slot-state-v2"
M11C_ACTIVATION_AUTHORITY = "m11c-external-bootstrap"
M11C_ROLLBACK_MODE = "ROLL_FORWARD_ONLY"


class M11cActiveReadError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> NoReturn:
    raise M11cActiveReadError(code, message)


def _digest(value: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(dict(value))).hexdigest()


def _digest_field(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 71 or not value.startswith("sha256:"):
        _fail("postcutover_state_invalid", f"{field} is not an exact sha256 digest")
    try:
        int(value[7:], 16)
    except ValueError:
        _fail("postcutover_state_invalid", f"{field} is not an exact sha256 digest")
    return value


def _v2_state(document: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
        "schema",
        "runtime_id",
        "generation_id",
        "authority_boundary",
        "activation_authority",
        "activation_id",
        "legacy_runtime_root",
        "active_manifest_sha256",
        "previous_manifest_sha256",
        "candidate_manifest_sha256",
        "required_control_schema",
        "required_capabilities",
        "candidate_may_write_active_pointer",
        "production_activation_performed",
        "source_preparation_sha256",
        "cutover_plan_sha256",
        "rollback_mode",
        "state_sha256",
    }
    if set(document) != expected or (
        document.get("schema") != SLOT_STATE_V2_SCHEMA
        or document.get("runtime_id") != RUNTIME_ID
        or document.get("generation_id") != GENERATION_ID
        or document.get("authority_boundary") != "external_bootstrap_root"
        or document.get("activation_authority") != M11C_ACTIVATION_AUTHORITY
        or document.get("candidate_manifest_sha256") is not None
        or document.get("candidate_may_write_active_pointer") is not False
        or document.get("production_activation_performed") is not True
        or document.get("rollback_mode") != M11C_ROLLBACK_MODE
    ):
        _fail("postcutover_state_invalid", "external post-cutover slot state identity differs")
    activation_id = document.get("activation_id")
    if not isinstance(activation_id, str) or not activation_id.startswith("m11c-") or len(activation_id) > 96:
        _fail("postcutover_state_invalid", "M11c activation identity differs")
    for field in (
        "active_manifest_sha256",
        "previous_manifest_sha256",
        "source_preparation_sha256",
        "cutover_plan_sha256",
        "state_sha256",
    ):
        _digest_field(document.get(field), field)
    supplied = document["state_sha256"]
    payload = dict(document)
    payload.pop("state_sha256", None)
    if _digest(payload) != supplied:
        _fail("postcutover_state_digest_mismatch", "external post-cutover state digest differs")
    return dict(document)


def observe_bootstrap_activation(*, authority_root: str | Path) -> dict[str, Any]:
    """Observe the one external Bootstrap pointer without any mutation surface."""

    authority = _absolute_path(authority_root, field="authority_root")
    state_path = _state_path(authority)
    if not state_path.exists():
        return {
            "schema": "bdb-vnext-m11c-active-read-v1",
            "status": "OFF",
            "production_activation_performed": False,
            "state": None,
            "slots": None,
        }
    raw = _load_json(state_path, field="slot_state")
    if raw.get("schema") == SLOT_STATE_SCHEMA:
        prepared = query_slot_authority(authority_root=authority)
        return {
            "schema": "bdb-vnext-m11c-active-read-v1",
            "status": "PREPARED",
            "production_activation_performed": False,
            "state": prepared["state"],
            "slots": prepared["slots"],
        }

    state = _v2_state(raw)
    legacy = _absolute_path(state["legacy_runtime_root"], field="legacy_runtime_root")
    active = _load_manifest(authority, state["active_manifest_sha256"], "ACTIVE")
    previous = _load_manifest(authority, state["previous_manifest_sha256"], "PREVIOUS")
    for item in (active, previous):
        _reobserve(item, legacy)
        if not _supports(item, state["required_control_schema"], tuple(state["required_capabilities"])):
            _fail("postcutover_slot_incompatible", f"{item['slot']} no longer satisfies compatibility")
    return {
        "schema": "bdb-vnext-m11c-active-read-v1",
        "status": "ACTIVE",
        "production_activation_performed": True,
        "state": state,
        "slots": {"ACTIVE": active, "PREVIOUS": previous, "CANDIDATE": None},
    }


def require_bootstrap_active(
    authority_root: str | Path,
    *,
    expected_source_head: str | None = None,
) -> dict[str, Any]:
    observed = observe_bootstrap_activation(authority_root=authority_root)
    if observed["status"] != "ACTIVE" or observed["production_activation_performed"] is not True:
        _fail("bootstrap_not_active", "external Bootstrap has not activated BDB Next")
    active = observed["slots"]["ACTIVE"]
    if expected_source_head is not None and active["source_commit"] != expected_source_head:
        _fail("bootstrap_source_mismatch", "external ACTIVE source differs from the client gate")
    return observed


__all__ = [
    "M11C_ACTIVATION_AUTHORITY",
    "M11cActiveReadError",
    "SLOT_STATE_V2_SCHEMA",
    "observe_bootstrap_activation",
    "require_bootstrap_active",
]

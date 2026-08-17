"""M11a build-only prepared activation preflight.

This module binds a staged external ACTIVE/PREVIOUS/CANDIDATE slot state to an
exact coordinated runtime backup and an independently observed PREVIOUS health
witness.  It writes immutable preparation evidence outside the Control DB.
It deliberately exposes no operation that can change ACTIVE; final activation
remains M11c authority.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from pathlib import Path
from typing import Any, Mapping

from bdb_shared.evidence import canonical_json_bytes
from bdb_vnext.bootstrap import (
    BootstrapError,
    BootstrapLock,
    _absolute_path,
    _assert_no_reparse_components,
    _load_json,
    _overlaps,
    create_coordinated_backup,
    inspect_runtime_bundle,
    run_health_check,
    verify_backup,
)
from bdb_vnext.composition import GENERATION_ID, RUNTIME_ID
from bdb_vnext.m11a_bootstrap_slots import query_slot_authority


PREPARED_ACTIVATION_SCHEMA = "bdb-vnext-bootstrap-prepared-activation-v1"
PREPARED_QUERY_SCHEMA = "bdb-vnext-bootstrap-prepared-query-v1"

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class M11aPreparationError(BootstrapError):
    pass


def _fail(code: str, message: str) -> None:
    raise M11aPreparationError(code, message)


def _digest(value: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _check_digest(value: object, field: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        _fail("invalid_digest", f"{field} must be an exact sha256 digest")
    return value


def _prepared_path(authority: Path, preparation_id: str) -> Path:
    if not isinstance(preparation_id, str) or _ID.fullmatch(preparation_id) is None:
        _fail("invalid_preparation_id", "preparation_id is invalid")
    return authority / "prepared-activations" / f"{preparation_id}.json"


def _write_immutable(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if _load_json(path, field="prepared_activation") != document:
            _fail("prepared_activation_conflict", "preparation_id already binds different evidence")
        return
    staging = path.parent / f".{path.name}.partial-{uuid.uuid4().hex}"
    try:
        with staging.open("xb") as handle:
            handle.write(canonical_json_bytes(document))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, path)
    except OSError as exc:
        raise M11aPreparationError("authority_write_failed", "prepared activation publication failed") from exc


def _validate_topology(authority: Path, runtime: Path, recovery_target: Path, legacy: Path) -> None:
    for path, field in (
        (authority, "authority_root"),
        (runtime, "runtime_root"),
        (recovery_target, "recovery_target"),
        (legacy, "legacy_runtime_root"),
    ):
        _assert_no_reparse_components(path, field=field)
    if _overlaps(authority, runtime) or _overlaps(authority, recovery_target):
        _fail("authority_overlap", "prepared-activation authority overlaps runtime/recovery target")
    if _overlaps(runtime, recovery_target):
        _fail("recovery_overlap", "recovery target overlaps runtime")
    if any(_overlaps(legacy, path) for path in (authority, runtime, recovery_target)):
        _fail("legacy_overlap", "M11a preparation overlaps Legacy state")


def _exact_slot_binding(query: Mapping[str, Any]) -> dict[str, str]:
    state = query["state"]
    previous = state.get("previous_manifest_sha256")
    candidate = state.get("candidate_manifest_sha256")
    if previous is None:
        _fail("previous_required", "prepared activation requires an exact PREVIOUS recovery slot")
    if candidate is None:
        _fail("candidate_required", "prepared activation requires a staged CANDIDATE")
    return {
        "slot_state_sha256": _check_digest(state.get("state_sha256"), "slot_state_sha256"),
        "active_manifest_sha256": _check_digest(state.get("active_manifest_sha256"), "active_manifest_sha256"),
        "previous_manifest_sha256": _check_digest(previous, "previous_manifest_sha256"),
        "candidate_manifest_sha256": _check_digest(candidate, "candidate_manifest_sha256"),
    }


def prepare_candidate_activation(
    *,
    authority_root: str | Path,
    runtime_root: str | Path,
    recovery_target: str | Path,
    preparation_id: str,
    source_is_quiesced: bool,
    health_timeout_seconds: float = 10.0,
    include_control_identity: bool = False,
) -> dict[str, Any]:
    """Create immutable M11a preparation evidence without changing ACTIVE."""

    authority = _absolute_path(authority_root, field="authority_root")
    runtime = _absolute_path(runtime_root, field="runtime_root")
    recovery = _absolute_path(recovery_target, field="recovery_target")
    prepared_path = _prepared_path(authority, preparation_id)

    with BootstrapLock(authority / "bootstrap.lock"):
        query = query_slot_authority(authority_root=authority)
        state = query["state"]
        legacy = _absolute_path(state["legacy_runtime_root"], field="legacy_runtime_root")
        _validate_topology(authority, runtime, recovery, legacy)
        binding = _exact_slot_binding(query)

        previous_doc = query["slots"]["PREVIOUS"]
        assert previous_doc is not None
        previous_bundle = inspect_runtime_bundle(
            previous_doc["bundle_root"],
            expected_role=previous_doc["bundle_role"],
            expected_sha256=previous_doc["bundle_sha256"],
            legacy_runtime_root=legacy,
        )
        previous_health = run_health_check(
            previous_bundle,
            required_control_schema=state["required_control_schema"],
            legacy_runtime_root=legacy,
            timeout_seconds=health_timeout_seconds,
        )

        backup = create_coordinated_backup(
            runtime,
            authority / "prepared-backups",
            backup_id=preparation_id,
            required_control_schema=state["required_control_schema"],
            source_is_quiesced=source_is_quiesced,
            include_control_identity=include_control_identity,
        )
        verified = verify_backup(backup.path)
        if verified.manifest_sha256 != backup.manifest_sha256:
            _fail("backup_identity_mismatch", "prepared backup changed after publication")

        post_query = query_slot_authority(authority_root=authority)
        if _exact_slot_binding(post_query) != binding:
            _fail("slot_state_changed", "slot authority changed during preparation")
        if post_query["state"]["active_manifest_sha256"] != query["state"]["active_manifest_sha256"]:
            _fail("active_pointer_changed", "M11a preparation changed ACTIVE")

        payload = {
            "schema": PREPARED_ACTIVATION_SCHEMA,
            "runtime_id": RUNTIME_ID,
            "generation_id": GENERATION_ID,
            "preparation_id": preparation_id,
            "authority_boundary": "external_bootstrap_root",
            "slot_binding": binding,
            "required_control_schema": state["required_control_schema"],
            "required_capabilities": list(state["required_capabilities"]),
            "backup": {
                "path": str(backup.path),
                "manifest_sha256": backup.manifest_sha256,
            },
            "recovery": {
                "target_root": str(recovery),
                "previous_bundle_sha256": previous_doc["bundle_sha256"],
                "previous_health": previous_health,
            },
            "candidate_may_write_active_pointer": False,
            "production_activation_performed": False,
            "activation_deferred_to": "M11c",
        }
        document = {**payload, "preparation_sha256": _digest(payload)}
        _write_immutable(prepared_path, document)

    return query_prepared_activation(authority_root=authority, preparation_id=preparation_id)


def _load_prepared(authority: Path, preparation_id: str) -> dict[str, Any]:
    document = _load_json(_prepared_path(authority, preparation_id), field="prepared_activation")
    if (
        document.get("schema") != PREPARED_ACTIVATION_SCHEMA
        or document.get("runtime_id") != RUNTIME_ID
        or document.get("generation_id") != GENERATION_ID
        or document.get("preparation_id") != preparation_id
        or document.get("authority_boundary") != "external_bootstrap_root"
        or document.get("candidate_may_write_active_pointer") is not False
        or document.get("production_activation_performed") is not False
        or document.get("activation_deferred_to") != "M11c"
    ):
        _fail("prepared_activation_identity_mismatch", "prepared activation identity differs")
    supplied = _check_digest(document.get("preparation_sha256"), "preparation_sha256")
    payload = dict(document)
    payload.pop("preparation_sha256", None)
    if _digest(payload) != supplied:
        _fail("prepared_activation_digest_mismatch", "prepared activation payload differs")
    binding = document.get("slot_binding")
    if not isinstance(binding, Mapping):
        _fail("prepared_activation_identity_mismatch", "slot_binding is missing")
    for field in (
        "slot_state_sha256",
        "active_manifest_sha256",
        "previous_manifest_sha256",
        "candidate_manifest_sha256",
    ):
        _check_digest(binding.get(field), field)
    return document


def query_prepared_activation(*, authority_root: str | Path, preparation_id: str) -> dict[str, Any]:
    """Revalidate immutable preparation evidence and current exact slot/backup subjects."""

    authority = _absolute_path(authority_root, field="authority_root")
    document = _load_prepared(authority, preparation_id)
    slots = query_slot_authority(authority_root=authority)
    current_binding = _exact_slot_binding(slots)
    if current_binding != document["slot_binding"]:
        _fail("prepared_activation_stale", "current slot authority differs from prepared binding")
    backup = verify_backup(document["backup"]["path"])
    if backup.manifest_sha256 != document["backup"]["manifest_sha256"]:
        _fail("prepared_backup_stale", "prepared backup identity differs")
    return {
        "schema": PREPARED_QUERY_SCHEMA,
        "runtime_id": RUNTIME_ID,
        "generation_id": GENERATION_ID,
        "prepared": document,
        "slots": slots,
        "backup_verified": True,
        "actions": {
            "activate_candidate": False,
            "activation_deferred_to": "M11c",
        },
        "authority": {
            "boundary": "external_bootstrap_root",
            "candidate_may_write_active_pointer": False,
            "production_activation_performed": False,
            "control_db_activation_authority": False,
        },
    }


__all__ = [
    "M11aPreparationError",
    "PREPARED_ACTIVATION_SCHEMA",
    "PREPARED_QUERY_SCHEMA",
    "prepare_candidate_activation",
    "query_prepared_activation",
]

"""M11a external ACTIVE/PREVIOUS/CANDIDATE slot authority, build-only tranche.

This module extends the M1b bootstrap floor with exact external slot identity
and compatibility.  It can initialize, stage, query, and discard a candidate.
It deliberately cannot promote CANDIDATE to ACTIVE; that authority is M11c.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from bdb_shared.evidence import canonical_json_bytes
from bdb_vnext.bootstrap import (
    BootstrapError,
    BootstrapLock,
    _absolute_path,
    _assert_no_reparse_components,
    _load_json,
    _overlaps,
    inspect_runtime_bundle,
)
from bdb_vnext.composition import (
    CONTROL_STORE_SCHEMA,
    GENERATION_ID,
    PROTOCOL_GENERATION,
    RUNTIME_ID,
)
from bdb_vnext.content_store import CONTENT_STORE_SCHEMA


SLOT_MANIFEST_SCHEMA = "bdb-vnext-bootstrap-slot-manifest-v1"
SLOT_STATE_SCHEMA = "bdb-vnext-bootstrap-slot-state-v1"
SLOT_QUERY_SCHEMA = "bdb-vnext-bootstrap-slot-query-v1"

SlotName = Literal["ACTIVE", "PREVIOUS", "CANDIDATE"]
BundleRole = Literal["candidate", "recovery"]
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_CAPABILITY = re.compile(r"^[a-z0-9][a-z0-9._:+/-]{0,127}$")


@dataclass(frozen=True)
class SlotSource:
    slot: SlotName
    bundle_root: Path
    expected_sha256: str
    bundle_role: BundleRole
    capabilities: tuple[str, ...] = ()


class M11aSlotError(BootstrapError):
    pass


def _fail(code: str, message: str) -> None:
    raise M11aSlotError(code, message)


def _digest(value: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _caps(values: Sequence[str]) -> tuple[str, ...]:
    result = tuple(sorted(values))
    if len(result) != len(set(result)) or any(
        not isinstance(value, str) or _CAPABILITY.fullmatch(value) is None for value in result
    ):
        _fail("invalid_capabilities", "capabilities must be unique bounded lowercase identifiers")
    return result


def _check_digest(value: object, field: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        _fail("invalid_digest", f"{field} must be an exact sha256 digest")
    return value


def _manifest_path(root: Path, digest: str) -> Path:
    value = _check_digest(digest, "manifest_sha256")
    return root / "slot-manifests" / f"{value[7:]}.json"


def _state_path(root: Path) -> Path:
    return root / "slot-state.json"


def _write_immutable(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if _load_json(path, field="slot_manifest") != document:
            _fail("immutable_manifest_conflict", "content-addressed manifest path has different bytes")
        return
    staging = path.parent / f".{path.name}.partial-{uuid.uuid4().hex}"
    try:
        with staging.open("xb") as handle:
            handle.write(canonical_json_bytes(document))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, path)
    except OSError as exc:
        raise M11aSlotError("authority_write_failed", "slot manifest publication failed") from exc


def _replace_state(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.parent / f".{path.name}.partial-{uuid.uuid4().hex}"
    try:
        with staging.open("xb") as handle:
            handle.write(canonical_json_bytes(document))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, path)
    except OSError as exc:
        raise M11aSlotError("authority_write_failed", "slot state publication failed") from exc


def _topology(
    authority_root: str | Path,
    legacy_runtime_root: str | Path,
    bundle_roots: Sequence[str | Path],
) -> tuple[Path, Path]:
    authority = _absolute_path(authority_root, field="authority_root")
    legacy = _absolute_path(legacy_runtime_root, field="legacy_runtime_root")
    _assert_no_reparse_components(authority, field="authority_root")
    if _overlaps(authority, legacy):
        _fail("legacy_overlap", "slot authority overlaps Legacy runtime")
    roots: list[Path] = []
    for index, value in enumerate(bundle_roots):
        root = _absolute_path(value, field=f"bundle_root[{index}]")
        if _overlaps(authority, root):
            _fail("authority_overlap", "slot authority overlaps bundle bytes")
        if _overlaps(legacy, root):
            _fail("legacy_overlap", "slot bundle overlaps Legacy runtime")
        if any(_overlaps(root, other) for other in roots):
            _fail("bundle_overlap", "slot bundle roots overlap")
        roots.append(root)
    return authority, legacy


def _inspect(source: SlotSource, legacy: Path) -> dict[str, Any]:
    if source.slot not in {"ACTIVE", "PREVIOUS", "CANDIDATE"}:
        _fail("invalid_slot", "slot identity is invalid")
    if source.bundle_role not in {"candidate", "recovery"}:
        _fail("invalid_bundle_role", "bundle role is invalid")
    if source.slot == "CANDIDATE" and source.bundle_role != "candidate":
        _fail("candidate_role_mismatch", "CANDIDATE requires a candidate bundle")
    bundle = inspect_runtime_bundle(
        source.bundle_root,
        expected_role=source.bundle_role,
        expected_sha256=source.expected_sha256,
        legacy_runtime_root=legacy,
    )
    if source.slot in {"ACTIVE", "PREVIOUS"} and not bundle.known_good:
        _fail("known_good_required", f"{source.slot} must be explicitly known-good")
    payload = {
        "schema": SLOT_MANIFEST_SCHEMA,
        "runtime_id": RUNTIME_ID,
        "generation_id": GENERATION_ID,
        "slot": source.slot,
        "bundle_root": str(bundle.root),
        "bundle_id": bundle.bundle_id,
        "bundle_role": bundle.role,
        "source_commit": bundle.source_commit,
        "bundle_sha256": bundle.sha256,
        "known_good": bundle.known_good,
        "compatibility": {
            "protocol_generation": PROTOCOL_GENERATION,
            "control_store_schema": CONTROL_STORE_SCHEMA,
            "supported_control_schema": {"min": bundle.schema_min, "max": bundle.schema_max},
            "content_store_schema": CONTENT_STORE_SCHEMA,
            "capabilities": list(_caps(source.capabilities)),
        },
    }
    return {**payload, "manifest_sha256": _digest(payload)}


def _publish(root: Path, document: Mapping[str, Any]) -> str:
    digest = _check_digest(document.get("manifest_sha256"), "manifest_sha256")
    payload = dict(document)
    payload.pop("manifest_sha256")
    if _digest(payload) != digest:
        _fail("manifest_digest_mismatch", "slot manifest digest differs")
    _write_immutable(_manifest_path(root, digest), document)
    return digest


def _load_manifest(root: Path, digest: str, slot: SlotName) -> dict[str, Any]:
    value = _check_digest(digest, "manifest_sha256")
    document = _load_json(_manifest_path(root, value), field=f"{slot.lower()}_manifest")
    if (
        document.get("schema") != SLOT_MANIFEST_SCHEMA
        or document.get("runtime_id") != RUNTIME_ID
        or document.get("generation_id") != GENERATION_ID
        or document.get("slot") != slot
        or document.get("manifest_sha256") != value
    ):
        _fail("slot_identity_mismatch", "slot manifest identity differs")
    payload = dict(document)
    payload.pop("manifest_sha256", None)
    if _digest(payload) != value:
        _fail("manifest_digest_mismatch", "slot manifest payload differs")
    compatibility = document.get("compatibility")
    if not isinstance(compatibility, Mapping) or (
        compatibility.get("protocol_generation") != PROTOCOL_GENERATION
        or compatibility.get("control_store_schema") != CONTROL_STORE_SCHEMA
        or compatibility.get("content_store_schema") != CONTENT_STORE_SCHEMA
    ):
        _fail("compatibility_identity_mismatch", "protocol/schema/content identity differs")
    if slot in {"ACTIVE", "PREVIOUS"} and document.get("known_good") is not True:
        _fail("known_good_required", f"{slot} is not known-good")
    return document


def _supports(document: Mapping[str, Any], schema: int, capabilities: Sequence[str]) -> bool:
    compatibility = document["compatibility"]
    supported = compatibility["supported_control_schema"]
    return (
        supported["min"] <= schema <= supported["max"]
        and set(capabilities).issubset(compatibility["capabilities"])
    )


def _reobserve(document: Mapping[str, Any], legacy: Path) -> None:
    bundle = inspect_runtime_bundle(
        document["bundle_root"],
        expected_role=document["bundle_role"],
        expected_sha256=document["bundle_sha256"],
        legacy_runtime_root=legacy,
    )
    supported = document["compatibility"]["supported_control_schema"]
    if (
        bundle.bundle_id != document["bundle_id"]
        or bundle.source_commit != document["source_commit"]
        or bundle.known_good != document["known_good"]
        or (bundle.schema_min, bundle.schema_max) != (supported["min"], supported["max"])
    ):
        _fail("bundle_identity_mismatch", "slot no longer matches exact bundle bytes")


def _state(
    *,
    legacy: Path,
    active: str,
    previous: str | None,
    candidate: str | None,
    required_control_schema: int,
    required_capabilities: Sequence[str],
) -> dict[str, Any]:
    payload = {
        "schema": SLOT_STATE_SCHEMA,
        "runtime_id": RUNTIME_ID,
        "generation_id": GENERATION_ID,
        "authority_boundary": "external_bootstrap_root",
        "legacy_runtime_root": str(legacy),
        "active_manifest_sha256": active,
        "previous_manifest_sha256": previous,
        "candidate_manifest_sha256": candidate,
        "required_control_schema": required_control_schema,
        "required_capabilities": list(_caps(required_capabilities)),
        "candidate_may_write_active_pointer": False,
        "production_activation_performed": False,
    }
    return {**payload, "state_sha256": _digest(payload)}


def _load_state(root: Path) -> dict[str, Any]:
    document = _load_json(_state_path(root), field="slot_state")
    if (
        document.get("schema") != SLOT_STATE_SCHEMA
        or document.get("runtime_id") != RUNTIME_ID
        or document.get("generation_id") != GENERATION_ID
        or document.get("authority_boundary") != "external_bootstrap_root"
        or document.get("candidate_may_write_active_pointer") is not False
        or document.get("production_activation_performed") is not False
    ):
        _fail("slot_state_identity_mismatch", "external slot state identity differs")
    supplied = _check_digest(document.get("state_sha256"), "state_sha256")
    payload = dict(document)
    payload.pop("state_sha256", None)
    if _digest(payload) != supplied:
        _fail("slot_state_digest_mismatch", "external slot state payload differs")
    _check_digest(document.get("active_manifest_sha256"), "active_manifest_sha256")
    for field in ("previous_manifest_sha256", "candidate_manifest_sha256"):
        if document.get(field) is not None:
            _check_digest(document[field], field)
    return document


def initialize_slot_authority(
    *,
    authority_root: str | Path,
    legacy_runtime_root: str | Path,
    active: SlotSource,
    previous: SlotSource | None = None,
    required_control_schema: int,
    required_capabilities: Sequence[str] = (),
) -> dict[str, Any]:
    """Record external ACTIVE/PREVIOUS identities without starting or switching runtime."""

    if active.slot != "ACTIVE" or (previous is not None and previous.slot != "PREVIOUS"):
        _fail("slot_role_mismatch", "initialize requires ACTIVE and optional PREVIOUS")
    if isinstance(required_control_schema, bool) or not isinstance(required_control_schema, int) or required_control_schema < 0:
        _fail("invalid_control_schema", "required control schema must be non-negative")
    required = _caps(required_capabilities)
    bundle_roots = [active.bundle_root] + ([] if previous is None else [previous.bundle_root])
    authority, legacy = _topology(authority_root, legacy_runtime_root, bundle_roots)
    with BootstrapLock(authority / "bootstrap.lock"):
        if _state_path(authority).exists():
            _fail("slot_authority_exists", "external slot authority already exists")
        active_doc = _inspect(active, legacy)
        if not _supports(active_doc, required_control_schema, required):
            _fail("active_incompatible", "ACTIVE cannot satisfy required compatibility")
        active_digest = _publish(authority, active_doc)
        previous_digest = None
        if previous is not None:
            previous_doc = _inspect(previous, legacy)
            if previous_doc["bundle_sha256"] == active_doc["bundle_sha256"]:
                _fail("slot_identity_collision", "ACTIVE and PREVIOUS reference the same bundle")
            if not _supports(previous_doc, required_control_schema, required):
                _fail("previous_incompatible", "PREVIOUS cannot satisfy required compatibility")
            previous_digest = _publish(authority, previous_doc)
        _replace_state(
            _state_path(authority),
            _state(
                legacy=legacy,
                active=active_digest,
                previous=previous_digest,
                candidate=None,
                required_control_schema=required_control_schema,
                required_capabilities=required,
            ),
        )
    return query_slot_authority(authority_root=authority)


def stage_candidate_slot(*, authority_root: str | Path, candidate: SlotSource) -> dict[str, Any]:
    """Stage compatible CANDIDATE while preserving the exact ACTIVE pointer."""

    if candidate.slot != "CANDIDATE":
        _fail("slot_role_mismatch", "stage requires a CANDIDATE source")
    authority = _absolute_path(authority_root, field="authority_root")
    with BootstrapLock(authority / "bootstrap.lock"):
        current = _load_state(authority)
        if current["candidate_manifest_sha256"] is not None:
            _fail("candidate_already_staged", "a CANDIDATE is already staged")
        legacy = _absolute_path(current["legacy_runtime_root"], field="legacy_runtime_root")
        _topology(authority, legacy, (candidate.bundle_root,))
        active_doc = _load_manifest(authority, current["active_manifest_sha256"], "ACTIVE")
        _reobserve(active_doc, legacy)
        candidate_doc = _inspect(candidate, legacy)
        if candidate_doc["bundle_sha256"] == active_doc["bundle_sha256"]:
            _fail("slot_identity_collision", "CANDIDATE references ACTIVE bytes")
        if not _supports(
            candidate_doc,
            current["required_control_schema"],
            tuple(current["required_capabilities"]),
        ):
            _fail("candidate_incompatible", "CANDIDATE cannot satisfy required compatibility")
        candidate_digest = _publish(authority, candidate_doc)
        next_state = _state(
            legacy=legacy,
            active=current["active_manifest_sha256"],
            previous=current["previous_manifest_sha256"],
            candidate=candidate_digest,
            required_control_schema=current["required_control_schema"],
            required_capabilities=tuple(current["required_capabilities"]),
        )
        if next_state["active_manifest_sha256"] != current["active_manifest_sha256"]:
            _fail("active_pointer_changed", "M11a attempted to change ACTIVE")
        _replace_state(_state_path(authority), next_state)
    return query_slot_authority(authority_root=authority)


def discard_candidate_slot(*, authority_root: str | Path) -> dict[str, Any]:
    """Clear only CANDIDATE pointer; content-addressed manifests remain retained."""

    authority = _absolute_path(authority_root, field="authority_root")
    with BootstrapLock(authority / "bootstrap.lock"):
        current = _load_state(authority)
        legacy = _absolute_path(current["legacy_runtime_root"], field="legacy_runtime_root")
        _replace_state(
            _state_path(authority),
            _state(
                legacy=legacy,
                active=current["active_manifest_sha256"],
                previous=current["previous_manifest_sha256"],
                candidate=None,
                required_control_schema=current["required_control_schema"],
                required_capabilities=tuple(current["required_capabilities"]),
            ),
        )
    return query_slot_authority(authority_root=authority)


def query_slot_authority(*, authority_root: str | Path) -> dict[str, Any]:
    """Read and re-observe exact external slot authority; Control DB is not consulted."""

    authority = _absolute_path(authority_root, field="authority_root")
    current = _load_state(authority)
    legacy = _absolute_path(current["legacy_runtime_root"], field="legacy_runtime_root")
    active = _load_manifest(authority, current["active_manifest_sha256"], "ACTIVE")
    previous = (
        None
        if current["previous_manifest_sha256"] is None
        else _load_manifest(authority, current["previous_manifest_sha256"], "PREVIOUS")
    )
    candidate = (
        None
        if current["candidate_manifest_sha256"] is None
        else _load_manifest(authority, current["candidate_manifest_sha256"], "CANDIDATE")
    )
    for document in (active, previous, candidate):
        if document is not None:
            _reobserve(document, legacy)
            if not _supports(
                document,
                current["required_control_schema"],
                tuple(current["required_capabilities"]),
            ):
                _fail("slot_incompatible", f"{document['slot']} no longer satisfies compatibility")
    return {
        "schema": SLOT_QUERY_SCHEMA,
        "runtime_id": RUNTIME_ID,
        "generation_id": GENERATION_ID,
        "state": current,
        "slots": {"ACTIVE": active, "PREVIOUS": previous, "CANDIDATE": candidate},
        "actions": {
            "stage_candidate": candidate is None,
            "discard_candidate": candidate is not None,
            "activate_candidate": False,
            "activation_deferred_to": "M11c",
        },
        "authority": {
            "boundary": "external_bootstrap_root",
            "candidate_may_write_active_pointer": False,
            "production_activation_performed": False,
            "control_db_consulted": False,
        },
    }


__all__ = [
    "M11aSlotError",
    "SLOT_MANIFEST_SCHEMA",
    "SLOT_QUERY_SCHEMA",
    "SLOT_STATE_SCHEMA",
    "SlotSource",
    "discard_candidate_slot",
    "initialize_slot_authority",
    "query_slot_authority",
    "stage_candidate_slot",
]

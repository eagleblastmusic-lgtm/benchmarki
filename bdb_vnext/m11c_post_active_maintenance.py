"""Content-addressed post-ACTIVE maintenance for the external Bootstrap.

The initial M11c cutover is intentionally pre-ACTIVE only.  This module is a
small, separate maintenance boundary for an already ACTIVE v2 authority.  It
stages immutable candidate evidence and an immutable exact plan first; only
``apply_post_active_maintenance`` may replace the existing v2 state under the
same Bootstrap lock.  The candidate never writes ``slot-state.json``.

This module is operator/build tooling, not part of the production Native Host
import graph.  It has no Legacy fallback and does not expose a best-effort or
latest-by-time activation path.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, NoReturn

from bdb_shared.evidence import canonical_json_bytes
from bdb_vnext.bootstrap import (
    BootstrapError,
    BootstrapLock,
    _absolute_path,
    _load_json,
    _valid_schema_version,
    inspect_runtime_bundle,
    run_health_check,
)
from bdb_vnext.composition import (
    CONTROL_STORE_SCHEMA,
    GENERATION_ID,
    PROTOCOL_GENERATION,
    RUNTIME_ID,
)
from bdb_vnext.m11a_bootstrap_slots import (
    SlotSource,
    _inspect,
    _load_manifest,
    _publish,
    _reobserve,
    _replace_state,
    _supports,
)
from bdb_vnext.m11c_active_reader import (
    M11C_ACTIVATION_AUTHORITY,
    M11C_ROLLBACK_MODE,
    SLOT_STATE_V2_SCHEMA,
    observe_bootstrap_activation,
)
from bdb_vnext.m11c_windows_clients import (
    M11cClientError,
    observe_windows_native_routes,
    query_client_plan,
    require_client_verification,
)


MAINTENANCE_CANDIDATE_SCHEMA = "bdb-vnext-m11c-maintenance-candidate-v1"
MAINTENANCE_PLAN_SCHEMA = "bdb-vnext-m11c-maintenance-plan-v1"
MAINTENANCE_QUERY_SCHEMA = "bdb-vnext-m11c-maintenance-query-v1"

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,90}$")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class M11cMaintenanceError(RuntimeError):
    """Typed, fail-closed maintenance error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class MaintenanceFault(RuntimeError):
    """Test-only injected crash marker; never translated into success."""

    def __init__(self, stage: str) -> None:
        super().__init__(stage)
        self.stage = stage


def _fail(code: str, message: str) -> NoReturn:
    raise M11cMaintenanceError(code, message)


def _digest(value: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(dict(value))).hexdigest()


def _check_digest(value: object, field: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        _fail("invalid_digest", f"{field} must be an exact sha256 digest")
    return value


def _check_sha40(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA40.fullmatch(value) is None:
        _fail("invalid_source_identity", f"{field} must be an exact Git SHA")
    return value


def _check_id(value: object, field: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        _fail("invalid_identifier", f"{field} is invalid")
    return value


def _write_immutable(path: Path, document: Mapping[str, Any], *, code: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if _load_json(path, field=code) != dict(document):
            _fail("immutable_maintenance_conflict", f"{code} already binds different bytes")
        return
    staging = path.parent / f".{path.name}.partial-{uuid.uuid4().hex}"
    try:
        with staging.open("xb") as handle:
            handle.write(canonical_json_bytes(dict(document)))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, path)
    except OSError as exc:
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            pass
        raise M11cMaintenanceError("maintenance_authority_write_failed", f"{code} publication failed") from exc


def _maintenance_dir(authority: Path, name: str) -> Path:
    return authority / "maintenance" / name


def _candidate_path(authority: Path, digest: str) -> Path:
    return _maintenance_dir(authority, "candidates") / f"{_check_digest(digest, 'candidate_manifest_sha256')[7:]}.json"


def _plan_path(authority: Path, maintenance_id: str) -> Path:
    return _maintenance_dir(authority, "plans") / f"{_check_id(maintenance_id, 'maintenance_id')}.json"


def _load_document(path: Path, *, field: str) -> dict[str, Any]:
    value = _load_json(path, field=field)
    if not isinstance(value, Mapping):
        _fail("maintenance_document_invalid", f"{field} must be an object")
    return dict(value)


def _load_candidate(authority: Path, digest: str) -> dict[str, Any]:
    value = _check_digest(digest, "candidate_manifest_sha256")
    document = _load_document(_candidate_path(authority, value), field="maintenance_candidate")
    expected = {
        "schema", "runtime_id", "generation_id", "maintenance_id", "authority_boundary",
        "activation_authority", "candidate_may_write_active_pointer", "production_activation_performed",
        "candidate_bundle_root", "candidate_bundle_sha256", "source_head", "source_tree",
        "candidate_client_runtime_root", "client_plan_sha256", "browser_bundle_digest",
        "native_manifest_digest", "native_artifact_manifest_sha256", "protocol_generation",
        "control_store_schema", "required_control_schema", "required_capabilities",
        "current_state_sha256", "current_active_manifest_sha256", "current_previous_manifest_sha256",
        "recovery_mode", "preparation_sha256", "preflight_evidence", "candidate_manifest_sha256",
    }
    if set(document) != expected or document.get("schema") != MAINTENANCE_CANDIDATE_SCHEMA:
        _fail("maintenance_candidate_invalid", "maintenance candidate fields differ")
    if document.get("runtime_id") != RUNTIME_ID or document.get("generation_id") != GENERATION_ID:
        _fail("maintenance_candidate_identity_mismatch", "maintenance candidate runtime identity differs")
    if document.get("authority_boundary") != "external_bootstrap_root" or document.get("activation_authority") != M11C_ACTIVATION_AUTHORITY:
        _fail("maintenance_candidate_authority_mismatch", "maintenance candidate authority differs")
    if document.get("candidate_may_write_active_pointer") is not False or document.get("production_activation_performed") is not False:
        _fail("candidate_self_activation_requested", "maintenance candidate requests activation authority")
    if document.get("protocol_generation") != PROTOCOL_GENERATION or document.get("control_store_schema") != CONTROL_STORE_SCHEMA:
        _fail("maintenance_candidate_identity_mismatch", "protocol or Control Store identity differs")
    if document.get("recovery_mode") != M11C_ROLLBACK_MODE:
        _fail("maintenance_candidate_identity_mismatch", "maintenance recovery mode differs")
    _check_id(document.get("maintenance_id"), "maintenance_id")
    _check_sha40(document.get("source_head"), "source_head")
    _check_sha40(document.get("source_tree"), "source_tree")
    for field in (
        "candidate_bundle_sha256", "client_plan_sha256", "browser_bundle_digest", "native_manifest_digest",
        "native_artifact_manifest_sha256", "current_state_sha256", "current_active_manifest_sha256",
        "current_previous_manifest_sha256", "preparation_sha256", "candidate_manifest_sha256",
    ):
        _check_digest(document.get(field), field)
    if not isinstance(document.get("required_capabilities"), list) or sorted(document["required_capabilities"]) != document["required_capabilities"]:
        _fail("maintenance_candidate_invalid", "required capabilities are not canonical")
    if not isinstance(document.get("preflight_evidence"), Mapping):
        _fail("maintenance_candidate_invalid", "preflight evidence is missing")
    supplied = document["candidate_manifest_sha256"]
    payload = dict(document)
    payload.pop("candidate_manifest_sha256")
    if _digest(payload) != supplied or supplied != value:
        _fail("maintenance_candidate_digest_mismatch", "maintenance candidate digest differs")
    return document


def _load_plan(authority: Path, maintenance_id: str) -> dict[str, Any]:
    document = _load_document(_plan_path(authority, maintenance_id), field="maintenance_plan")
    expected = {
        "schema", "runtime_id", "generation_id", "maintenance_id", "authority_boundary",
        "activation_authority", "operator_approval_required", "candidate_may_write_active_pointer",
        "production_activation_performed", "candidate_manifest_sha256", "candidate_source_head",
        "candidate_source_tree", "candidate_bundle_sha256", "client_plan_sha256", "browser_bundle_digest",
        "native_manifest_digest", "native_artifact_manifest_sha256", "current_state_sha256",
        "current_active_manifest_sha256", "current_previous_manifest_sha256", "required_control_schema",
        "required_capabilities", "protocol_generation", "recovery_mode", "preflight_evidence",
        "plan_sha256",
    }
    if set(document) != expected or document.get("schema") != MAINTENANCE_PLAN_SCHEMA:
        _fail("maintenance_plan_invalid", "maintenance plan fields differ")
    if document.get("runtime_id") != RUNTIME_ID or document.get("generation_id") != GENERATION_ID:
        _fail("maintenance_plan_identity_mismatch", "maintenance plan runtime identity differs")
    if document.get("authority_boundary") != "external_bootstrap_root" or document.get("activation_authority") != M11C_ACTIVATION_AUTHORITY:
        _fail("maintenance_plan_authority_mismatch", "maintenance plan authority differs")
    if document.get("operator_approval_required") is not True or document.get("candidate_may_write_active_pointer") is not False or document.get("production_activation_performed") is not False:
        _fail("maintenance_plan_invalid", "maintenance plan approval/activation flags differ")
    if document.get("protocol_generation") != PROTOCOL_GENERATION or document.get("recovery_mode") != M11C_ROLLBACK_MODE:
        _fail("maintenance_plan_identity_mismatch", "maintenance plan protocol/recovery identity differs")
    _check_id(document.get("maintenance_id"), "maintenance_id")
    _check_sha40(document.get("candidate_source_head"), "candidate_source_head")
    _check_sha40(document.get("candidate_source_tree"), "candidate_source_tree")
    for field in (
        "candidate_manifest_sha256", "candidate_bundle_sha256", "client_plan_sha256", "browser_bundle_digest",
        "native_manifest_digest", "native_artifact_manifest_sha256", "current_state_sha256",
        "current_active_manifest_sha256", "current_previous_manifest_sha256",
    ):
        _check_digest(document.get(field), field)
    supplied = document["plan_sha256"]
    payload = dict(document)
    payload.pop("plan_sha256")
    if _digest(payload) != supplied:
        _fail("maintenance_plan_digest_mismatch", "maintenance plan digest differs")
    return document


def _active_observation(authority: Path) -> dict[str, Any]:
    try:
        observed = observe_bootstrap_activation(authority_root=authority)
    except Exception as exc:
        if isinstance(exc, M11cMaintenanceError):
            raise
        code = getattr(exc, "code", "bootstrap_observation_failed")
        raise M11cMaintenanceError(code, str(exc)) from exc
    if observed.get("status") != "ACTIVE" or observed.get("production_activation_performed") is not True:
        _fail("active_bootstrap_required", "post-ACTIVE maintenance requires the exact v2 ACTIVE state")
    state = observed.get("state")
    slots = observed.get("slots")
    if not isinstance(state, Mapping) or not isinstance(slots, Mapping):
        _fail("active_bootstrap_invalid", "ACTIVE Bootstrap observation is incomplete")
    if state.get("schema") != SLOT_STATE_V2_SCHEMA or state.get("candidate_manifest_sha256") is not None:
        _fail("active_bootstrap_invalid", "ACTIVE Bootstrap state is not the expected v2 shape")
    if state.get("candidate_may_write_active_pointer") is not False:
        _fail("candidate_self_activation_requested", "ACTIVE state permits candidate pointer mutation")
    if state.get("activation_authority") != M11C_ACTIVATION_AUTHORITY:
        _fail("active_bootstrap_invalid", "ACTIVE activation authority differs")
    if state.get("rollback_mode") != M11C_ROLLBACK_MODE:
        _fail("active_bootstrap_invalid", "ACTIVE rollback mode differs")
    active = slots.get("ACTIVE")
    previous = slots.get("PREVIOUS")
    if not isinstance(active, Mapping) or not isinstance(previous, Mapping):
        _fail("recovery_subject_missing", "ACTIVE and PREVIOUS must both be known-good")
    if active.get("known_good") is not True or previous.get("known_good") is not True:
        _fail("recovery_subject_missing", "ACTIVE and PREVIOUS must both be known-good")
    return {"state": dict(state), "active": dict(active), "previous": dict(previous), "observed": observed}


def _client_identity(runtime_root: Path, *, source_head: str, source_tree: str) -> dict[str, Any]:
    try:
        queried = query_client_plan(runtime_root=runtime_root)
        plan = queried["plan"]
        require_client_verification(runtime_root=runtime_root, expected_client_plan_sha256=plan["client_plan_sha256"])
    except (M11cClientError, BootstrapError, KeyError) as exc:
        raise M11cMaintenanceError(getattr(exc, "code", "client_identity_unavailable"), str(exc)) from exc
    if plan.get("source_head") != source_head or plan.get("source_tree") != source_tree:
        _fail("client_source_mismatch", "candidate clients bind a different source subject")
    return {
        "client_plan_sha256": plan["client_plan_sha256"],
        "browser_bundle_digest": plan["browser_bundle_digest"],
        "native_manifest_digest": plan["native_manifest_sha256"],
    }


def _observe_candidate_bundle(*, root: Path, expected_sha256: str, legacy: Path, required_schema: int, required_capabilities: tuple[str, ...]) -> dict[str, Any]:
    try:
        bundle = inspect_runtime_bundle(root, expected_role="candidate", expected_sha256=expected_sha256, legacy_runtime_root=legacy)
        health = run_health_check(bundle, required_control_schema=required_schema, legacy_runtime_root=legacy, timeout_seconds=10.0)
    except (BootstrapError, OSError) as exc:
        raise M11cMaintenanceError(getattr(exc, "code", "candidate_observation_failed"), str(exc)) from exc
    if bundle.known_good is not True or health.get("status") != "READY":
        _fail("candidate_health_failed", "candidate bundle is not independently READY")
    if bundle.source_commit is None or not _supports({"compatibility": {"supported_control_schema": {"min": bundle.schema_min, "max": bundle.schema_max}, "capabilities": list(required_capabilities)}}, required_schema, required_capabilities):
        _fail("candidate_incompatible", "candidate bundle cannot satisfy current compatibility")
    return {"bundle": bundle, "health": dict(health)}


def prepare_post_active_maintenance(
    *,
    authority_root: str | Path,
    candidate_bundle_root: str | Path,
    candidate_bundle_sha256: str,
    candidate_client_runtime_root: str | Path,
    source_head: str,
    source_tree: str,
    native_artifact_manifest_sha256: str,
    maintenance_id: str,
    preflight_evidence: Mapping[str, Any] | None = None,
    fault_hook: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Prepare candidate evidence and an exact plan without changing ACTIVE."""

    authority = _absolute_path(authority_root, field="authority_root")
    candidate_root = _absolute_path(candidate_bundle_root, field="candidate_bundle_root")
    client_root = _absolute_path(candidate_client_runtime_root, field="candidate_client_runtime_root")
    source_head = _check_sha40(source_head, "source_head")
    source_tree = _check_sha40(source_tree, "source_tree")
    bundle_sha = _check_digest(candidate_bundle_sha256, "candidate_bundle_sha256")
    native_artifact_sha = _check_digest(native_artifact_manifest_sha256, "native_artifact_manifest_sha256")
    maintenance_id = _check_id(maintenance_id, "maintenance_id")
    evidence = dict(preflight_evidence or {})
    if any(not isinstance(key, str) or not isinstance(value, str) or _DIGEST.fullmatch(value) is None for key, value in evidence.items()):
        _fail("preflight_evidence_invalid", "preflight evidence must be a mapping of names to digests")
    with BootstrapLock(authority / "bootstrap.lock"):
        current = _active_observation(authority)
        state = current["state"]
        legacy = _absolute_path(state["legacy_runtime_root"], field="legacy_runtime_root")
        required_control_schema = _valid_schema_version(state.get("required_control_schema"), field="required_control_schema")
        required = tuple(state["required_capabilities"])
        _observe_candidate_bundle(root=candidate_root, expected_sha256=bundle_sha, legacy=legacy, required_schema=required_control_schema, required_capabilities=required)
        client = _client_identity(client_root, source_head=source_head, source_tree=source_tree)
        if fault_hook:
            fault_hook("before_candidate_publication")
        preparation_payload = {
            "maintenance_id": maintenance_id,
            "source_head": source_head,
            "source_tree": source_tree,
            "candidate_bundle_sha256": bundle_sha,
            "client_plan_sha256": client["client_plan_sha256"],
            "native_artifact_manifest_sha256": native_artifact_sha,
            "current_state_sha256": state["state_sha256"],
        }
        preparation_sha = _digest(preparation_payload)
        payload = {
            "schema": MAINTENANCE_CANDIDATE_SCHEMA,
            "runtime_id": RUNTIME_ID,
            "generation_id": GENERATION_ID,
            "maintenance_id": maintenance_id,
            "authority_boundary": "external_bootstrap_root",
            "activation_authority": M11C_ACTIVATION_AUTHORITY,
            "candidate_may_write_active_pointer": False,
            "production_activation_performed": False,
            "candidate_bundle_root": str(candidate_root),
            "candidate_bundle_sha256": bundle_sha,
            "source_head": source_head,
            "source_tree": source_tree,
            "candidate_client_runtime_root": str(client_root),
            "client_plan_sha256": client["client_plan_sha256"],
            "browser_bundle_digest": client["browser_bundle_digest"],
            "native_manifest_digest": client["native_manifest_digest"],
            "native_artifact_manifest_sha256": native_artifact_sha,
            "protocol_generation": PROTOCOL_GENERATION,
            "control_store_schema": CONTROL_STORE_SCHEMA,
            "required_control_schema": state["required_control_schema"],
            "required_capabilities": list(required),
            "current_state_sha256": state["state_sha256"],
            "current_active_manifest_sha256": state["active_manifest_sha256"],
            "current_previous_manifest_sha256": state["previous_manifest_sha256"],
            "recovery_mode": M11C_ROLLBACK_MODE,
            "preparation_sha256": preparation_sha,
            "preflight_evidence": evidence,
        }
        candidate_digest = _digest(payload)
        candidate = {**payload, "candidate_manifest_sha256": candidate_digest}
        _write_immutable(_candidate_path(authority, candidate_digest), candidate, code="maintenance_candidate")
        if fault_hook:
            fault_hook("after_candidate_publication")
        plan_payload = {
            "schema": MAINTENANCE_PLAN_SCHEMA,
            "runtime_id": RUNTIME_ID,
            "generation_id": GENERATION_ID,
            "maintenance_id": maintenance_id,
            "authority_boundary": "external_bootstrap_root",
            "activation_authority": M11C_ACTIVATION_AUTHORITY,
            "operator_approval_required": True,
            "candidate_may_write_active_pointer": False,
            "production_activation_performed": False,
            "candidate_manifest_sha256": candidate_digest,
            "candidate_source_head": source_head,
            "candidate_source_tree": source_tree,
            "candidate_bundle_sha256": bundle_sha,
            "client_plan_sha256": client["client_plan_sha256"],
            "browser_bundle_digest": client["browser_bundle_digest"],
            "native_manifest_digest": client["native_manifest_digest"],
            "native_artifact_manifest_sha256": native_artifact_sha,
            "current_state_sha256": state["state_sha256"],
            "current_active_manifest_sha256": state["active_manifest_sha256"],
            "current_previous_manifest_sha256": state["previous_manifest_sha256"],
            "required_control_schema": state["required_control_schema"],
            "required_capabilities": list(required),
            "protocol_generation": PROTOCOL_GENERATION,
            "recovery_mode": M11C_ROLLBACK_MODE,
            "preflight_evidence": evidence,
        }
        plan_digest = _digest(plan_payload)
        plan = {**plan_payload, "plan_sha256": plan_digest}
        if fault_hook:
            fault_hook("before_plan_publication")
        _write_immutable(_plan_path(authority, maintenance_id), plan, code="maintenance_plan")
        if fault_hook:
            fault_hook("after_plan_publication")
    return query_post_active_maintenance(authority_root=authority, maintenance_id=maintenance_id)


def query_post_active_maintenance(*, authority_root: str | Path, maintenance_id: str) -> dict[str, Any]:
    authority = _absolute_path(authority_root, field="authority_root")
    maintenance_id = _check_id(maintenance_id, "maintenance_id")
    plan = _load_plan(authority, maintenance_id)
    candidate = _load_candidate(authority, plan["candidate_manifest_sha256"])
    if candidate["maintenance_id"] != maintenance_id:
        _fail("maintenance_candidate_identity_mismatch", "candidate and plan maintenance IDs differ")
    return {
        "schema": MAINTENANCE_QUERY_SCHEMA,
        "plan": plan,
        "candidate": candidate,
        "production_activation_performed": False,
        "actions": {"apply_requires_exact_plan_sha256": True, "candidate_self_activation": False},
    }


def _verify_routes(runtime_root: Path) -> None:
    if os.name != "nt":
        return
    try:
        routes = observe_windows_native_routes(runtime_root=runtime_root)
    except M11cClientError as exc:
        raise M11cMaintenanceError(exc.code, str(exc)) from exc
    if routes.get("target_conflict") or routes.get("target_registered") is not True or routes.get("legacy_route_present"):
        _fail("native_route_not_exclusive", "candidate Native route is not exact and Legacy-free")


def apply_post_active_maintenance(
    *,
    authority_root: str | Path,
    maintenance_id: str,
    expected_plan_sha256: str,
    operator_approved: bool,
    fault_hook: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Apply one exact plan to an already ACTIVE v2 authority."""

    if operator_approved is not True:
        _fail("operator_approval_required", "maintenance apply requires explicit operator approval")
    authority = _absolute_path(authority_root, field="authority_root")
    maintenance_id = _check_id(maintenance_id, "maintenance_id")
    expected = _check_digest(expected_plan_sha256, "expected_plan_sha256")
    with BootstrapLock(authority / "bootstrap.lock"):
        plan = _load_plan(authority, maintenance_id)
        if plan["plan_sha256"] != expected:
            _fail("maintenance_plan_stale", "operator approval is bound to a different maintenance plan")
        current = _active_observation(authority)
        state = current["state"]
        for field in ("state_sha256", "active_manifest_sha256", "previous_manifest_sha256"):
            if state[field] != plan[f"current_{field}"]:
                _fail("maintenance_plan_stale", f"current {field} differs from the approved plan")
        candidate = _load_candidate(authority, plan["candidate_manifest_sha256"])
        if "plan_sha256" in candidate:
            _fail("maintenance_candidate_invalid", "candidate unexpectedly contains a plan binding")
        if any(candidate[field] != plan[plan_field] for field, plan_field in (
            ("candidate_manifest_sha256", "candidate_manifest_sha256"),
            ("source_head", "candidate_source_head"),
            ("source_tree", "candidate_source_tree"),
            ("candidate_bundle_sha256", "candidate_bundle_sha256"),
            ("client_plan_sha256", "client_plan_sha256"),
            ("browser_bundle_digest", "browser_bundle_digest"),
            ("native_manifest_digest", "native_manifest_digest"),
        )):
            _fail("maintenance_plan_binding_mismatch", "candidate differs from the approved plan")
        legacy = _absolute_path(state["legacy_runtime_root"], field="legacy_runtime_root")
        _observe_candidate_bundle(
            root=Path(candidate["candidate_bundle_root"]),
            expected_sha256=plan["candidate_bundle_sha256"],
            legacy=legacy,
            required_schema=int(plan["required_control_schema"]),
            required_capabilities=tuple(plan["required_capabilities"]),
        )
        client = _client_identity(Path(candidate["candidate_client_runtime_root"]), source_head=plan["candidate_source_head"], source_tree=plan["candidate_source_tree"])
        if any(client[field] != plan[field] for field in ("client_plan_sha256", "browser_bundle_digest", "native_manifest_digest")):
            _fail("maintenance_client_binding_mismatch", "observed candidate client differs from the approved plan")
        _verify_routes(Path(candidate["candidate_client_runtime_root"]))
        if fault_hook:
            fault_hook("after_revalidation_before_switch")

        old_active = current["active"]
        old_previous = current["previous"]
        candidate_doc = _inspect(
            SlotSource("ACTIVE", Path(candidate["candidate_bundle_root"]), plan["candidate_bundle_sha256"], "candidate", tuple(plan["required_capabilities"])),
            legacy,
        )
        previous_doc = _inspect(
            SlotSource("PREVIOUS", Path(old_active["bundle_root"]), old_active["bundle_sha256"], old_active["bundle_role"], tuple(plan["required_capabilities"])),
            legacy,
        )
        if fault_hook:
            fault_hook("after_manifest_inspection")
        active_digest = _publish(authority, candidate_doc)
        previous_digest = _publish(authority, previous_doc)
        if fault_hook:
            fault_hook("after_manifest_publication")
        payload = {
            "schema": SLOT_STATE_V2_SCHEMA,
            "runtime_id": RUNTIME_ID,
            "generation_id": GENERATION_ID,
            "authority_boundary": "external_bootstrap_root",
            "activation_authority": M11C_ACTIVATION_AUTHORITY,
            "activation_id": f"m11c-maint-{maintenance_id}",
            "legacy_runtime_root": state["legacy_runtime_root"],
            "active_manifest_sha256": active_digest,
            "previous_manifest_sha256": previous_digest,
            "candidate_manifest_sha256": None,
            "required_control_schema": state["required_control_schema"],
            "required_capabilities": list(state["required_capabilities"]),
            "candidate_may_write_active_pointer": False,
            "production_activation_performed": True,
            "source_preparation_sha256": candidate["preparation_sha256"],
            "cutover_plan_sha256": expected,
            "rollback_mode": M11C_ROLLBACK_MODE,
        }
        next_state = {**payload, "state_sha256": _digest(payload)}
        if fault_hook:
            fault_hook("before_state_publication")
        _replace_state(authority / "slot-state.json", next_state)
        if fault_hook:
            fault_hook("after_state_publication")
        final = _active_observation(authority)
        if final["active"]["source_commit"] != plan["candidate_source_head"]:
            _fail("maintenance_readback_mismatch", "ACTIVE readback source differs from the approved plan")
        if final["state"]["candidate_manifest_sha256"] is not None:
            _fail("maintenance_readback_mismatch", "CANDIDATE pointer remains after maintenance")
    return {
        "schema": MAINTENANCE_QUERY_SCHEMA,
        "status": "ACTIVE",
        "maintenance_id": maintenance_id,
        "plan_sha256": expected,
        "bootstrap": final,
        "old_active_source": old_active["source_commit"],
        "previous_source": final["previous"]["source_commit"],
        "production_activation_performed": True,
    }


__all__ = [
    "MAINTENANCE_CANDIDATE_SCHEMA",
    "MAINTENANCE_PLAN_SCHEMA",
    "MAINTENANCE_QUERY_SCHEMA",
    "MaintenanceFault",
    "M11cMaintenanceError",
    "apply_post_active_maintenance",
    "prepare_post_active_maintenance",
    "query_post_active_maintenance",
]

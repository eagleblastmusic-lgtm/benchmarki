"""M11c external Bootstrap cutover authority for BDB Next.

M11a deliberately cannot promote CANDIDATE. M11c is the one source module
allowed to replace the external ``slot-state.json`` pre-activation v1 document
with the post-cutover v2 document. The same ProgramData Bootstrap root remains
the physical activation authority; no second ACTIVE pointer is introduced.

The M9b activation record is only a subordinate Browser/Native client gate.
Production admission must require the M11c external ACTIVE state, the M9b
ACTIVE gate, the canonical M3c intake switch and an exact Browser->Native
client verification bound to the immutable M11c client plan.

Importing this module is inert. The public effectful entrypoint is explicitly
Windows/operator scoped and re-observes the real ProgramData ACL and Native
Messaging route before any product activation write.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NoReturn

from bdb_shared.evidence import canonical_json_bytes, semantic_digest
from bdb_vnext.bootstrap import (
    BootstrapLock,
    _absolute_path,
    _load_json,
    inspect_runtime_bundle,
    run_health_check,
    verify_backup,
)
from bdb_vnext.composition import GENERATION_ID, RUNTIME_ID
from bdb_vnext.m11a_bootstrap_slots import (
    SLOT_STATE_SCHEMA,
    SlotSource,
    _inspect,
    _load_manifest,
    _publish,
    _reobserve,
    _replace_state,
    _state_path,
    _supports,
    query_slot_authority,
)
from bdb_vnext.m11a_prepared_activation import query_prepared_activation
from bdb_vnext.m11a_windows_tcb import WINDOWS_TCB_SCHEMA, build_windows_tcb_witness
from bdb_vnext.m11c_windows_clients import (
    M11cClientError,
    disable_windows_legacy_native_route,
    observe_windows_native_routes,
    query_client_plan,
    require_client_verification,
)
from bdb_vnext.m3c_admission import CanonicalVNextAdmissionAuthority
from bdb_vnext.m9b_activation import (
    ActivationRecord,
    M9bActivationError,
    _begin_bootstrap_client_gate,
    _finalize_bootstrap_client_gate,
    read_activation,
    record_clients_verified,
    validate_m9a_freeze_report,
)


CUTOVER_PLAN_SCHEMA = "bdb-vnext-m11c-cutover-plan-v1"
CUTOVER_QUERY_SCHEMA = "bdb-vnext-m11c-cutover-query-v1"
SLOT_STATE_V2_SCHEMA = "bdb-vnext-bootstrap-slot-state-v2"
M11C_ACTIVATION_AUTHORITY = "m11c-external-bootstrap"
M11C_ROLLBACK_MODE = "ROLL_FORWARD_ONLY"

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,90}$")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class M11cCutoverError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> NoReturn:
    raise M11cCutoverError(code, message)


def _digest(value: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _check_digest(value: object, field: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        _fail("invalid_digest", f"{field} must be an exact sha256 digest")
    return value


def _check_sha40(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA40.fullmatch(value) is None:
        _fail("invalid_source_identity", f"{field} must be an exact 40-character Git SHA")
    return value


def _check_id(value: object, field: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        _fail("invalid_identifier", f"{field} is invalid")
    return value


def _plan_path(authority: Path, cutover_id: str) -> Path:
    return authority / "cutover-plans" / f"{_check_id(cutover_id, 'cutover_id')}.json"


def _write_immutable(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if _load_json(path, field="m11c_cutover_plan") != document:
            _fail("cutover_plan_conflict", "cutover_id already binds different evidence")
        return
    staging = path.parent / f".{path.name}.partial-{uuid.uuid4().hex}"
    try:
        with staging.open("xb") as handle:
            handle.write(canonical_json_bytes(document))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, path)
    except OSError as exc:
        raise M11cCutoverError("authority_write_failed", "cutover plan publication failed") from exc


def _validate_tcb_witness(
    witness: Mapping[str, Any],
    *,
    authority: Path,
    runtime: Path,
    legacy: Path,
) -> str:
    if witness.get("schema") != WINDOWS_TCB_SCHEMA:
        _fail("tcb_witness_invalid", "Windows TCB witness schema differs")
    if witness.get("runtime_id") != RUNTIME_ID or witness.get("generation_id") != GENERATION_ID:
        _fail("tcb_witness_invalid", "Windows TCB witness generation differs")
    if witness.get("candidate_may_write_authority") is not False:
        _fail("candidate_write_authority", "candidate may mutate Bootstrap authority")
    if witness.get("activation_operation_available") is not False:
        _fail("m11a_activation_surface_present", "M11a unexpectedly exposes activation")
    if witness.get("activation_deferred_to") != "M11c":
        _fail("tcb_witness_invalid", "Windows TCB witness does not defer activation to M11c")
    topology = witness.get("topology")
    if not isinstance(topology, Mapping) or (
        topology.get("authority_root") != str(authority)
        or topology.get("runtime_root") != str(runtime)
        or topology.get("legacy_runtime_root") != str(legacy)
    ):
        _fail("tcb_topology_mismatch", "Windows TCB witness is bound to different roots")
    supplied = _check_digest(witness.get("witness_sha256"), "tcb_witness_sha256")
    payload = dict(witness)
    payload.pop("witness_sha256", None)
    if semantic_digest(payload) != supplied:
        _fail("tcb_witness_digest_mismatch", "Windows TCB witness digest differs")
    return supplied


def _load_plan(authority: Path, cutover_id: str) -> dict[str, Any]:
    document = _load_json(_plan_path(authority, cutover_id), field="m11c_cutover_plan")
    if (
        document.get("schema") != CUTOVER_PLAN_SCHEMA
        or document.get("runtime_id") != RUNTIME_ID
        or document.get("generation_id") != GENERATION_ID
        or document.get("cutover_id") != cutover_id
        or document.get("authority_boundary") != "external_bootstrap_root"
        or document.get("operator_approval_required") is not True
        or document.get("candidate_may_write_active_pointer") is not False
        or document.get("production_activation_performed") is not False
        or document.get("rollback_mode") != M11C_ROLLBACK_MODE
    ):
        _fail("cutover_plan_identity_mismatch", "M11c cutover plan identity differs")
    supplied = _check_digest(document.get("cutover_plan_sha256"), "cutover_plan_sha256")
    payload = dict(document)
    payload.pop("cutover_plan_sha256", None)
    if _digest(payload) != supplied:
        _fail("cutover_plan_digest_mismatch", "M11c cutover plan digest differs")
    _check_sha40(document.get("source_head"), "source_head")
    _check_sha40(document.get("source_tree"), "source_tree")
    for field in (
        "preparation_sha256",
        "candidate_bundle_sha256",
        "m9a_freeze_digest",
        "client_plan_sha256",
        "browser_bundle_digest",
        "native_manifest_digest",
        "tcb_witness_sha256",
    ):
        _check_digest(document.get(field), field)
    binding = document.get("slot_binding")
    if not isinstance(binding, Mapping):
        _fail("cutover_plan_identity_mismatch", "slot binding is missing")
    for field in (
        "slot_state_sha256",
        "active_manifest_sha256",
        "previous_manifest_sha256",
        "candidate_manifest_sha256",
    ):
        _check_digest(binding.get(field), field)
    return document


def prepare_cutover_plan(
    *,
    authority_root: str | Path,
    runtime_root: str | Path,
    legacy_runtime_root: str | Path,
    preparation_id: str,
    cutover_id: str,
    source_head: str,
    source_tree: str,
    m9a_report: Mapping[str, Any],
    browser_bundle_digest: str,
    native_manifest_digest: str,
    tcb_witness: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind exact pre-cutover subjects without changing any ACTIVE pointer."""

    authority = _absolute_path(authority_root, field="authority_root")
    runtime = _absolute_path(runtime_root, field="runtime_root")
    legacy = _absolute_path(legacy_runtime_root, field="legacy_runtime_root")
    cutover_id = _check_id(cutover_id, "cutover_id")
    source_head = _check_sha40(source_head, "source_head")
    source_tree = _check_sha40(source_tree, "source_tree")
    freeze_digest = validate_m9a_freeze_report(m9a_report)
    browser_digest = _check_digest(browser_bundle_digest, "browser_bundle_digest")
    native_digest = _check_digest(native_manifest_digest, "native_manifest_digest")
    try:
        client_plan = query_client_plan(runtime_root=runtime)["plan"]
    except M11cClientError as exc:
        raise M11cCutoverError(exc.code, str(exc)) from exc
    if client_plan["source_head"] != source_head or client_plan["source_tree"] != source_tree:
        _fail("client_plan_source_mismatch", "staged Browser/Native clients bind a different source subject")
    if client_plan["browser_bundle_digest"] != browser_digest:
        _fail("browser_bundle_mismatch", "Browser digest differs from the staged client plan")
    if client_plan["native_manifest_sha256"] != native_digest:
        _fail("native_manifest_mismatch", "Native manifest digest differs from the staged client plan")
    tcb_digest = _validate_tcb_witness(
        tcb_witness,
        authority=authority,
        runtime=runtime,
        legacy=legacy,
    )

    with BootstrapLock(authority / "bootstrap.lock"):
        prepared_query = query_prepared_activation(
            authority_root=authority,
            preparation_id=preparation_id,
        )
        prepared = prepared_query["prepared"]
        slots = prepared_query["slots"]
        state = slots["state"]
        if state.get("schema") != SLOT_STATE_SCHEMA or state.get("production_activation_performed") is not False:
            _fail("preactivation_state_required", "M11c plan requires the exact M11a pre-activation state")
        if state.get("legacy_runtime_root") != str(legacy):
            _fail("legacy_root_mismatch", "prepared activation is bound to a different Legacy root")
        candidate = slots["slots"].get("CANDIDATE")
        if not isinstance(candidate, Mapping):
            _fail("candidate_required", "M11c requires the exact staged CANDIDATE")
        if candidate.get("known_good") is not True:
            _fail("candidate_not_certified", "final M11c CANDIDATE must be an explicitly known-good bundle")
        if candidate.get("source_commit") != source_head:
            _fail("candidate_source_mismatch", "candidate source commit differs from the cutover source HEAD")

        candidate_bundle = inspect_runtime_bundle(
            candidate["bundle_root"],
            expected_role=candidate["bundle_role"],
            expected_sha256=candidate["bundle_sha256"],
            legacy_runtime_root=legacy,
        )
        candidate_health = run_health_check(
            candidate_bundle,
            required_control_schema=state["required_control_schema"],
            legacy_runtime_root=legacy,
            timeout_seconds=10.0,
        )
        if candidate_health.get("status") != "READY":
            _fail("candidate_health_failed", "candidate health is not READY")

        payload = {
            "schema": CUTOVER_PLAN_SCHEMA,
            "runtime_id": RUNTIME_ID,
            "generation_id": GENERATION_ID,
            "cutover_id": cutover_id,
            "authority_boundary": "external_bootstrap_root",
            "authority_root": str(authority),
            "runtime_root": str(runtime),
            "legacy_runtime_root": str(legacy),
            "preparation_id": preparation_id,
            "preparation_sha256": prepared["preparation_sha256"],
            "slot_binding": dict(prepared["slot_binding"]),
            "candidate_source_commit": candidate["source_commit"],
            "candidate_bundle_sha256": candidate["bundle_sha256"],
            "source_head": source_head,
            "source_tree": source_tree,
            "m9a_freeze_digest": freeze_digest,
            "client_plan_sha256": client_plan["client_plan_sha256"],
            "browser_bundle_digest": browser_digest,
            "native_manifest_digest": native_digest,
            "tcb_witness_sha256": tcb_digest,
            "operator_approval_required": True,
            "candidate_may_write_active_pointer": False,
            "production_activation_performed": False,
            "rollback_mode": M11C_ROLLBACK_MODE,
        }
        document = {**payload, "cutover_plan_sha256": _digest(payload)}
        _write_immutable(_plan_path(authority, cutover_id), document)

    return query_cutover_plan(authority_root=authority, cutover_id=cutover_id)


def query_cutover_plan(*, authority_root: str | Path, cutover_id: str) -> dict[str, Any]:
    authority = _absolute_path(authority_root, field="authority_root")
    plan = _load_plan(authority, _check_id(cutover_id, "cutover_id"))
    return {
        "schema": CUTOVER_QUERY_SCHEMA,
        "plan": plan,
        "production_activation_performed": False,
        "actions": {"apply_requires_explicit_operator": True, "candidate_self_activation": False},
    }


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
        _check_digest(document.get(field), field)
    supplied = document["state_sha256"]
    payload = dict(document)
    payload.pop("state_sha256", None)
    if _digest(payload) != supplied:
        _fail("postcutover_state_digest_mismatch", "external post-cutover state digest differs")
    return dict(document)


def observe_bootstrap_activation(*, authority_root: str | Path) -> dict[str, Any]:
    """Observe the one external Bootstrap pointer before or after M11c."""

    authority = _absolute_path(authority_root, field="authority_root")
    state_path = _state_path(authority)
    if not state_path.exists():
        return {
            "schema": CUTOVER_QUERY_SCHEMA,
            "status": "OFF",
            "production_activation_performed": False,
            "state": None,
            "slots": None,
        }
    raw = _load_json(state_path, field="slot_state")
    if raw.get("schema") == SLOT_STATE_SCHEMA:
        prepared = query_slot_authority(authority_root=authority)
        return {
            "schema": CUTOVER_QUERY_SCHEMA,
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
        "schema": CUTOVER_QUERY_SCHEMA,
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
    if expected_source_head is not None and active["source_commit"] != _check_sha40(expected_source_head, "expected_source_head"):
        _fail("bootstrap_source_mismatch", "external ACTIVE source differs from the client gate")
    return observed


def _publish_external_activation(
    *,
    authority: Path,
    plan: Mapping[str, Any],
    prepared_query: Mapping[str, Any],
) -> dict[str, Any]:
    state = prepared_query["slots"]["state"]
    slots = prepared_query["slots"]["slots"]
    candidate = slots["CANDIDATE"]
    old_active = slots["ACTIVE"]
    if not isinstance(candidate, Mapping) or not isinstance(old_active, Mapping):
        _fail("cutover_subject_missing", "M11c requires ACTIVE and CANDIDATE subjects")
    if candidate.get("known_good") is not True:
        _fail("candidate_not_certified", "M11c cannot activate a non-certified candidate")

    required = tuple(state["required_capabilities"])
    legacy = _absolute_path(state["legacy_runtime_root"], field="legacy_runtime_root")
    active_document = _inspect(
        SlotSource(
            "ACTIVE",
            Path(candidate["bundle_root"]),
            candidate["bundle_sha256"],
            candidate["bundle_role"],
            required,
        ),
        legacy,
    )
    previous_document = _inspect(
        SlotSource(
            "PREVIOUS",
            Path(old_active["bundle_root"]),
            old_active["bundle_sha256"],
            old_active["bundle_role"],
            required,
        ),
        legacy,
    )
    active_digest = _publish(authority, active_document)
    previous_digest = _publish(authority, previous_document)
    activation_id = f"m11c-{plan['cutover_id']}"
    payload = {
        "schema": SLOT_STATE_V2_SCHEMA,
        "runtime_id": RUNTIME_ID,
        "generation_id": GENERATION_ID,
        "authority_boundary": "external_bootstrap_root",
        "activation_authority": M11C_ACTIVATION_AUTHORITY,
        "activation_id": activation_id,
        "legacy_runtime_root": str(legacy),
        "active_manifest_sha256": active_digest,
        "previous_manifest_sha256": previous_digest,
        "candidate_manifest_sha256": None,
        "required_control_schema": state["required_control_schema"],
        "required_capabilities": list(required),
        "candidate_may_write_active_pointer": False,
        "production_activation_performed": True,
        "source_preparation_sha256": plan["preparation_sha256"],
        "cutover_plan_sha256": plan["cutover_plan_sha256"],
        "rollback_mode": M11C_ROLLBACK_MODE,
    }
    document = {**payload, "state_sha256": _digest(payload)}
    _replace_state(_state_path(authority), document)
    return require_bootstrap_active(authority, expected_source_head=plan["source_head"])


def _matching_client_gate(record: ActivationRecord, plan: Mapping[str, Any]) -> bool:
    return (
        record.activation_id == f"m9b-{plan['cutover_id']}"
        and record.source_head == plan["source_head"]
        and record.source_tree == plan["source_tree"]
        and record.m9a_freeze_digest == plan["m9a_freeze_digest"]
        and record.browser_bundle_digest == plan["browser_bundle_digest"]
        and record.native_manifest_digest == plan["native_manifest_digest"]
    )


def _minimal_freeze_report(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": "bdb-vnext-m9a-freeze-report-v1",
        "status": "PASS_CLOSED",
        "legacy_ingress_frozen": True,
        "legacy_writer_frozen": True,
        "archive_created": True,
        "zero_new_write_observed": True,
        "vnext_activation_allowed": False,
        "m9b_allowed": False,
        "partial_freeze_requires_roll_forward": False,
        "freeze_digest": plan["m9a_freeze_digest"],
    }


def _ensure_client_gate(runtime: Path, plan: Mapping[str, Any]) -> ActivationRecord:
    try:
        require_client_verification(
            runtime_root=runtime,
            expected_client_plan_sha256=plan["client_plan_sha256"],
        )
    except M11cClientError as exc:
        raise M11cCutoverError(exc.code, str(exc)) from exc
    current = read_activation(runtime)
    if current is None:
        return record_clients_verified(
            runtime,
            m9a_report=_minimal_freeze_report(plan),
            source_head=plan["source_head"],
            source_tree=plan["source_tree"],
            browser_bundle_digest=plan["browser_bundle_digest"],
            native_manifest_digest=plan["native_manifest_digest"],
            activation_id=f"m9b-{plan['cutover_id']}",
        )
    if not _matching_client_gate(current, plan):
        _fail("client_gate_identity_mismatch", "M9b client gate differs from the M11c cutover plan")
    return current


def _verify_active_health(*, active: Mapping[str, Any], legacy: Path) -> None:
    active_doc = active["slots"]["ACTIVE"]
    bundle = inspect_runtime_bundle(
        active_doc["bundle_root"],
        expected_role=active_doc["bundle_role"],
        expected_sha256=active_doc["bundle_sha256"],
        legacy_runtime_root=legacy,
    )
    health = run_health_check(
        bundle,
        required_control_schema=active["state"]["required_control_schema"],
        legacy_runtime_root=legacy,
        timeout_seconds=10.0,
    )
    if health.get("status") != "READY":
        _fail("active_health_failed", "external ACTIVE bundle is not READY")


def _begin_client_gate(runtime: Path, activation_id: str) -> ActivationRecord:
    try:
        return _begin_bootstrap_client_gate(runtime, expected_activation_id=activation_id)
    except M9bActivationError as exc:
        raise M11cCutoverError(exc.code, str(exc)) from exc


def _finalize_client_gate(
    runtime: Path,
    activation_id: str,
    authority: CanonicalVNextAdmissionAuthority,
) -> ActivationRecord:
    try:
        return _finalize_bootstrap_client_gate(
            runtime,
            expected_activation_id=activation_id,
            canonical_intake_is_enabled=lambda: authority.admission_enabled,
        )
    except M9bActivationError as exc:
        raise M11cCutoverError(exc.code, str(exc)) from exc


def _apply_cutover(
    *,
    authority_root: str | Path,
    cutover_id: str,
    expected_plan_sha256: str,
    operator_approved: bool,
    tcb_witness: Mapping[str, Any],
) -> dict[str, Any]:
    """Core M11c transition used by the Windows entrypoint and isolated tests."""

    if operator_approved is not True:
        _fail("operator_approval_required", "M11c cutover requires explicit operator approval")
    authority = _absolute_path(authority_root, field="authority_root")
    cutover_id = _check_id(cutover_id, "cutover_id")
    expected_plan_sha256 = _check_digest(expected_plan_sha256, "expected_plan_sha256")

    with BootstrapLock(authority / "bootstrap.lock"):
        plan = _load_plan(authority, cutover_id)
        if plan["cutover_plan_sha256"] != expected_plan_sha256:
            _fail("cutover_plan_stale", "operator approval is bound to a different cutover plan")
        runtime = _absolute_path(plan["runtime_root"], field="runtime_root")
        legacy = _absolute_path(plan["legacy_runtime_root"], field="legacy_runtime_root")
        try:
            require_client_verification(
                runtime_root=runtime,
                expected_client_plan_sha256=plan["client_plan_sha256"],
            )
        except M11cClientError as exc:
            raise M11cCutoverError(exc.code, str(exc)) from exc
        if _validate_tcb_witness(tcb_witness, authority=authority, runtime=runtime, legacy=legacy) != plan["tcb_witness_sha256"]:
            _fail("tcb_witness_stale", "current Windows TCB differs from the approved plan")

        observed = observe_bootstrap_activation(authority_root=authority)
        client_gate = _ensure_client_gate(runtime, plan)
        m3c = CanonicalVNextAdmissionAuthority.open(runtime, legacy_root=legacy)
        try:
            if observed["status"] == "ACTIVE":
                active = require_bootstrap_active(authority, expected_source_head=plan["source_head"])
                _verify_active_health(active=active, legacy=legacy)
                if client_gate.state == "ACTIVATING":
                    if m3c.admission_enabled is not True:
                        _fail("canonical_intake_not_enabled", "external ACTIVE exists but M3c intake is not enabled")
                    client_gate = _finalize_client_gate(runtime, client_gate.activation_id, m3c)
                if client_gate.state != "ACTIVE" or m3c.admission_enabled is not True:
                    _fail("cutover_incomplete", "external ACTIVE exists but subordinate gates are not ACTIVE")
                return {
                    "schema": CUTOVER_QUERY_SCHEMA,
                    "status": "ACTIVE",
                    "bootstrap": active,
                    "client_gate": client_gate.as_dict(),
                    "m3c_intake_enabled": True,
                    "production_activation_performed": True,
                }

            if observed["status"] != "PREPARED":
                _fail("preactivation_state_required", "M11c cutover requires PREPARED or exact ACTIVE state")
            prepared_query = query_prepared_activation(
                authority_root=authority,
                preparation_id=plan["preparation_id"],
            )
            prepared = prepared_query["prepared"]
            if prepared["preparation_sha256"] != plan["preparation_sha256"] or prepared["slot_binding"] != plan["slot_binding"]:
                _fail("prepared_activation_stale", "M11a preparation differs from the approved cutover plan")
            backup = verify_backup(prepared["backup"]["path"])
            if backup.manifest_sha256 != prepared["backup"]["manifest_sha256"]:
                _fail("prepared_backup_stale", "prepared backup identity differs")

            if client_gate.state == "CLIENTS_VERIFIED":
                m3c.disable_intake()
                client_gate = _begin_client_gate(runtime, client_gate.activation_id)
                m3c.enable_intake()
                active = _publish_external_activation(
                    authority=authority,
                    plan=plan,
                    prepared_query=prepared_query,
                )
                _verify_active_health(active=active, legacy=legacy)
                client_gate = _finalize_client_gate(runtime, client_gate.activation_id, m3c)
            elif client_gate.state == "ACTIVATING":
                if m3c.admission_enabled is not True:
                    m3c.enable_intake()
                active = _publish_external_activation(
                    authority=authority,
                    plan=plan,
                    prepared_query=prepared_query,
                )
                _verify_active_health(active=active, legacy=legacy)
                client_gate = _finalize_client_gate(runtime, client_gate.activation_id, m3c)
            else:
                _fail("client_gate_state_conflict", "M9b client gate is ACTIVE before external Bootstrap cutover")

            final = require_bootstrap_active(authority, expected_source_head=plan["source_head"])
            if client_gate.state != "ACTIVE" or m3c.admission_enabled is not True:
                _fail("cutover_incomplete", "M11c did not close all production admission gates")
            return {
                "schema": CUTOVER_QUERY_SCHEMA,
                "status": "ACTIVE",
                "bootstrap": final,
                "client_gate": client_gate.as_dict(),
                "m3c_intake_enabled": True,
                "production_activation_performed": True,
            }
        finally:
            m3c.close()


def prepare_windows_cutover_plan(
    *,
    authority_root: str | Path,
    runtime_root: str | Path,
    legacy_runtime_root: str | Path,
    preparation_id: str,
    cutover_id: str,
    source_head: str,
    source_tree: str,
    m9a_report: Mapping[str, Any],
    browser_bundle_digest: str,
    native_manifest_digest: str,
) -> dict[str, Any]:
    """Windows/operator preflight using a fresh real ProgramData ACL witness."""

    if os.name != "nt":
        _fail("windows_required", "production M11c preparation requires Windows")
    program_data = os.environ.get("PROGRAMDATA")
    if not program_data:
        _fail("programdata_unavailable", "PROGRAMDATA is required for production M11c")
    prepared = query_prepared_activation(authority_root=authority_root, preparation_id=preparation_id)
    candidate = prepared["slots"]["slots"]["CANDIDATE"]
    if not isinstance(candidate, Mapping):
        _fail("candidate_required", "M11c requires a staged candidate")
    witness = build_windows_tcb_witness(
        authority_root=authority_root,
        program_data=program_data,
        runtime_root=runtime_root,
        legacy_runtime_root=legacy_runtime_root,
        mutable_roots=(candidate["bundle_root"],),
    )
    return prepare_cutover_plan(
        authority_root=authority_root,
        runtime_root=runtime_root,
        legacy_runtime_root=legacy_runtime_root,
        preparation_id=preparation_id,
        cutover_id=cutover_id,
        source_head=source_head,
        source_tree=source_tree,
        m9a_report=m9a_report,
        browser_bundle_digest=browser_bundle_digest,
        native_manifest_digest=native_manifest_digest,
        tcb_witness=witness,
    )


def apply_windows_cutover(
    *,
    authority_root: str | Path,
    cutover_id: str,
    expected_plan_sha256: str,
    operator_approved: bool,
) -> dict[str, Any]:
    """The only production-scoped M11c effect entrypoint; never runs off Windows."""

    if os.name != "nt":
        _fail("windows_required", "production M11c cutover requires Windows")
    authority = _absolute_path(authority_root, field="authority_root")
    plan = _load_plan(authority, _check_id(cutover_id, "cutover_id"))
    program_data = os.environ.get("PROGRAMDATA")
    if not program_data:
        _fail("programdata_unavailable", "PROGRAMDATA is required for production M11c")
    witness = build_windows_tcb_witness(
        authority_root=authority,
        program_data=program_data,
        runtime_root=plan["runtime_root"],
        legacy_runtime_root=plan["legacy_runtime_root"],
    )
    try:
        routes = observe_windows_native_routes(runtime_root=plan["runtime_root"])
        if routes["target_conflict"] or not routes["target_registered"]:
            _fail("target_native_route_unverified", "exact vNext Native route is not registered")
        disable_windows_legacy_native_route(runtime_root=plan["runtime_root"])
    except M11cClientError as exc:
        raise M11cCutoverError(exc.code, str(exc)) from exc
    return _apply_cutover(
        authority_root=authority,
        cutover_id=cutover_id,
        expected_plan_sha256=expected_plan_sha256,
        operator_approved=operator_approved,
        tcb_witness=witness,
    )


__all__ = [
    "CUTOVER_PLAN_SCHEMA",
    "CUTOVER_QUERY_SCHEMA",
    "M11C_ACTIVATION_AUTHORITY",
    "M11cCutoverError",
    "SLOT_STATE_V2_SCHEMA",
    "apply_windows_cutover",
    "observe_bootstrap_activation",
    "prepare_cutover_plan",
    "prepare_windows_cutover_plan",
    "query_cutover_plan",
    "require_bootstrap_active",
]

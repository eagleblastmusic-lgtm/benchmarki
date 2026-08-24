"""Canonical recovery of a missing vNext M9b client gate.

M9b is a subordinate client gate.  This module can reconstruct it only when
the current ACTIVE Bootstrap, verified client, canonical M3c state and a
validated historical M9a lineage agree exactly.  It never changes Bootstrap,
Native routes or Legacy state and it is deliberately roll-forward-only.
"""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, NoReturn

from bdb_shared.evidence import canonical_json_bytes, semantic_digest
from bdb_vnext.bootstrap import BootstrapLock, _absolute_path
from bdb_vnext.m11c_active_reader import observe_bootstrap_activation
from bdb_vnext.m11c_windows_clients import (
    M11cClientError,
    observe_windows_native_routes,
    query_client_plan,
    require_client_verification,
)
from bdb_vnext.m9b_activation import ActivationRecord, read_activation, write_activation
from bdb_vnext.m9b_reconciliation import query_post_active_reconciliation


M9B_RECOVERY_PLAN_SCHEMA = "bdb-vnext-m9b-missing-recovery-plan-v1"
M9B_RECOVERY_STATE_SCHEMA = "bdb-vnext-m9b-missing-recovery-state-v1"
M9B_RECOVERY_RESULT_SCHEMA = "bdb-vnext-m9b-missing-recovery-result-v1"
M9B_RECOVERY_MODE = "MISSING_M9B_ROLL_FORWARD_ONLY"
_RECOVERY_DIR = "maintenance/m9b-recovery"
_SHA40 = set("0123456789abcdef")
_SHA256 = set("0123456789abcdef")


class M9bRecoveryError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def _fail(code: str, message: str, *, details: Mapping[str, Any] | None = None) -> NoReturn:
    raise M9bRecoveryError(code, message, details=details)


def _sha40(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 40 or set(value) - _SHA40:
        _fail("recovery_identity_invalid", f"{field} must be a lowercase Git SHA")
    return value


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:") or len(value) != 71 or set(value[7:]) - _SHA256:
        _fail("recovery_identity_invalid", f"{field} must be an exact sha256 digest")
    return value


def _id(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128 or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_." for ch in value):
        _fail("recovery_identity_invalid", f"{field} is invalid")
    return value


def _semantic_digest(value: Mapping[str, Any]) -> str:
    return semantic_digest(dict(value))


def _stable_json(path: Path, *, field: str) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise M9bRecoveryError("recovery_record_invalid", f"{field} cannot be read") from exc
    if not isinstance(value, Mapping):
        _fail("recovery_record_invalid", f"{field} must contain an object")
    return {str(key): item for key, item in value.items()}


def _atomic_json(path: Path, value: Mapping[str, Any], *, fault_hook: Callable[[str], None] | None = None) -> None:
    payload = canonical_json_bytes(dict(value))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            if fault_hook:
                fault_hook("recovery_state_after_fsync")
        os.replace(temporary, path)
    except Exception as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise M9bRecoveryError("recovery_state_write_failed", "M9b recovery record could not be written") from exc


def _plan_path(authority: Path, recovery_id: str) -> Path:
    return authority / _RECOVERY_DIR / "plans" / f"{_id(recovery_id, 'recovery_id')}.json"


def _state_path(authority: Path, recovery_id: str) -> Path:
    return authority / _RECOVERY_DIR / "states" / f"{_id(recovery_id, 'recovery_id')}.json"


def _write_immutable(path: Path, value: Mapping[str, Any]) -> None:
    payload = canonical_json_bytes(dict(value))
    if path.exists():
        try:
            if path.read_bytes() != payload:
                _fail("recovery_record_conflict", f"immutable record {path.name} differs")
        except OSError as exc:
            raise M9bRecoveryError("recovery_record_read_failed", "immutable recovery record cannot be read") from exc
        return
    _atomic_json(path, value)


def _write_state(
    authority: Path,
    *,
    recovery_id: str,
    plan_sha256: str,
    phase: str,
    bootstrap_state_sha256: str,
    target_record_sha256: str,
    fault_hook: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    payload = {
        "schema": M9B_RECOVERY_STATE_SCHEMA,
        "recovery_id": recovery_id,
        "plan_sha256": plan_sha256,
        "phase": phase,
        "bootstrap_state_sha256": bootstrap_state_sha256,
        "target_record_sha256": target_record_sha256,
    }
    document = {**payload, "state_sha256": _semantic_digest(payload)}
    _atomic_json(_state_path(authority, recovery_id), document, fault_hook=fault_hook)
    return document


def _fault(fault_hook: Callable[[str], None] | None, point: str) -> None:
    if fault_hook is None:
        return
    try:
        fault_hook(point)
    except M9bRecoveryError:
        raise
    except Exception as exc:
        raise M9bRecoveryError("recovery_fault_injected", f"recovery fault at {point}") from exc


def _load_plan(authority: Path, recovery_id: str, expected_plan_sha256: str | None = None) -> dict[str, Any]:
    value = _stable_json(_plan_path(authority, recovery_id), field="m9b recovery plan")
    required = {
        "schema", "recovery_id", "recovery_mode", "bootstrap_state_sha256", "active_manifest_sha256",
        "previous_manifest_sha256", "source_head", "source_tree", "client_plan_sha256",
        "browser_bundle_digest", "native_manifest_digest", "native_executable_sha256", "native_config_sha256",
        "native_manifest_path", "client_verification_sha256",
        "m3c_control_digest", "m3c_kill_switch_digest", "legacy_route_present", "protocol_generation",
        "generation_id", "browser_extension_id", "native_host_name", "historical_reconciliation_id",
        "historical_reconciliation_plan_sha256", "historical_target_m9b_record_sha256", "m9a_freeze_digest",
        "activation_id", "clients_verified_record", "clients_verified_record_sha256", "activating_record",
        "activating_record_sha256", "target_m9b_record", "target_m9b_record_sha256", "deployed_runtime_root",
        "plan_sha256",
    }
    if set(value) != required or value.get("schema") != M9B_RECOVERY_PLAN_SCHEMA or value.get("recovery_id") != recovery_id:
        _fail("recovery_plan_invalid", "M9b recovery plan fields differ")
    if value.get("recovery_mode") != M9B_RECOVERY_MODE or value.get("legacy_route_present") is not False:
        _fail("recovery_plan_invalid", "M9b recovery plan permits an unsafe state")
    plan_sha = _digest(value.get("plan_sha256"), "plan_sha256")
    payload = dict(value)
    payload.pop("plan_sha256")
    if _semantic_digest(payload) != plan_sha:
        _fail("recovery_plan_digest_mismatch", "M9b recovery plan digest differs")
    if expected_plan_sha256 is not None and plan_sha != _digest(expected_plan_sha256, "expected_plan_sha256"):
        _fail("recovery_plan_stale", "operator subject is bound to another M9b recovery plan")
    for field in (
        "bootstrap_state_sha256", "active_manifest_sha256", "previous_manifest_sha256", "client_plan_sha256",
        "browser_bundle_digest", "native_manifest_digest", "client_verification_sha256", "m3c_control_digest",
        "native_executable_sha256", "native_config_sha256", "m3c_kill_switch_digest",
        "historical_reconciliation_plan_sha256", "historical_target_m9b_record_sha256",
        "m9a_freeze_digest", "clients_verified_record_sha256", "activating_record_sha256", "target_m9b_record_sha256",
    ):
        _digest(value.get(field), field)
    _sha40(value.get("source_head"), "source_head")
    _sha40(value.get("source_tree"), "source_tree")
    for name in ("clients_verified_record", "activating_record", "target_m9b_record"):
        record = ActivationRecord.from_mapping(value[name])
        if record.as_dict() != value[name]:
            _fail("recovery_plan_invalid", f"{name} is not canonical")
    return value


def _load_state(authority: Path, recovery_id: str, plan_sha256: str) -> dict[str, Any]:
    value = _stable_json(_state_path(authority, recovery_id), field="m9b recovery state")
    required = {"schema", "recovery_id", "plan_sha256", "phase", "bootstrap_state_sha256", "target_record_sha256", "state_sha256"}
    if set(value) != required or value.get("schema") != M9B_RECOVERY_STATE_SCHEMA or value.get("recovery_id") != recovery_id:
        _fail("recovery_state_invalid", "M9b recovery state fields differ")
    if value.get("plan_sha256") != plan_sha256:
        _fail("recovery_state_stale", "M9b recovery state binds another plan")
    supplied = _digest(value.get("state_sha256"), "state_sha256")
    payload = dict(value)
    payload.pop("state_sha256")
    if _semantic_digest(payload) != supplied:
        _fail("recovery_state_digest_mismatch", "M9b recovery state digest differs")
    if value.get("phase") not in {"PREPARED", "CLIENTS_VERIFIED", "ACTIVATING", "ACTIVE", "COMPLETED"}:
        _fail("recovery_state_invalid", "M9b recovery state phase is unsupported")
    return value


def _historical_lineage(*, authority: Path, maintenance_id: str, plan_sha256: str) -> tuple[dict[str, Any], ActivationRecord]:
    try:
        result = query_post_active_reconciliation(
            authority_root=authority,
            maintenance_id=_id(maintenance_id, "historical_reconciliation_id"),
            expected_plan_sha256=_digest(plan_sha256, "historical_reconciliation_plan_sha256"),
        )
    except Exception as exc:
        _fail(getattr(exc, "code", "historical_recovery_evidence_invalid"), str(exc))
    if result.get("status") != "COMPLETED" or result.get("state", {}).get("phase") != "COMPLETED":
        _fail("historical_recovery_evidence_invalid", "historical M9b reconciliation is not completed")
    plan = result.get("plan")
    if not isinstance(plan, Mapping):
        _fail("historical_recovery_evidence_invalid", "historical reconciliation plan is missing")
    target_value = plan.get("target_m9b_record")
    if not isinstance(target_value, Mapping):
        _fail("historical_recovery_evidence_invalid", "historical target M9b record is missing")
    target = ActivationRecord.from_mapping(target_value)
    if target.as_dict() != dict(target_value) or target.as_dict()["record_digest"] != plan.get("target_m9b_record_sha256"):
        _fail("historical_recovery_evidence_invalid", "historical target M9b record is not exact")
    if plan.get("m9a_freeze_digest") != target.m9a_freeze_digest:
        _fail("historical_recovery_evidence_invalid", "historical M9a freeze lineage differs")
    return dict(plan), target


def _m3c_state(runtime: Path) -> dict[str, Any]:
    from bdb_vnext.m9b_reconciliation import _m3c_state as read_m3c_state

    return read_m3c_state(runtime)


def _subject(
    *,
    authority: Path,
    deployed: Path,
    historical_reconciliation_id: str,
    historical_reconciliation_plan_sha256: str,
    allow_records: tuple[ActivationRecord, ...] = (),
) -> dict[str, Any]:
    bootstrap = observe_bootstrap_activation(authority_root=authority)
    if bootstrap.get("status") != "ACTIVE" or bootstrap.get("production_activation_performed") is not True:
        _fail("bootstrap_not_active", "M9b recovery requires ACTIVE Bootstrap")
    state = bootstrap.get("state")
    slots = bootstrap.get("slots")
    if not isinstance(state, Mapping) or not isinstance(slots, Mapping):
        _fail("bootstrap_subject_invalid", "Bootstrap ACTIVE state is incomplete")
    active = slots.get("ACTIVE")
    previous = slots.get("PREVIOUS")
    if not isinstance(active, Mapping) or not isinstance(previous, Mapping) or active.get("known_good") is not True or previous.get("known_good") is not True:
        _fail("bootstrap_subject_invalid", "Bootstrap ACTIVE/PREVIOUS are not known-good")
    try:
        queried = query_client_plan(runtime_root=deployed)
        client = queried["plan"]
        verification = require_client_verification(runtime_root=deployed, expected_client_plan_sha256=client["client_plan_sha256"])
        routes = observe_windows_native_routes(runtime_root=deployed)
    except (M11cClientError, KeyError) as exc:
        _fail(getattr(exc, "code", "client_subject_unavailable"), str(exc))
    if active.get("source_commit") != client.get("source_head") or active.get("source_tree") != client.get("source_tree"):
        _fail("bootstrap_client_source_mismatch", "Bootstrap ACTIVE and verified client source differ")
    if routes.get("target_registered") is not True or routes.get("target_conflict") or routes.get("legacy_route_present"):
        _fail("native_route_not_exclusive", "verified client route is not exact and Legacy-free")
    try:
        m3c = _m3c_state(deployed)
    except Exception as exc:
        _fail(getattr(exc, "code", "m3c_subject_unavailable"), str(exc))
    current = read_activation(deployed)
    if current is not None and all(current.as_dict() != record.as_dict() for record in allow_records):
        _fail("recovery_foreign_m9b", "deployed M9b record is foreign to the recovery subject")
    historical_plan, historical_target = _historical_lineage(
        authority=authority,
        maintenance_id=historical_reconciliation_id,
        plan_sha256=historical_reconciliation_plan_sha256,
    )
    return {
        "bootstrap": dict(state),
        "active": dict(active),
        "previous": dict(previous),
        "client": dict(client),
        "verification": dict(verification),
        "routes": dict(routes),
        "m3c": dict(m3c),
        "current": current,
        "historical_plan": historical_plan,
        "historical_target": historical_target,
    }


def _transition_record(target: ActivationRecord, state: str) -> ActivationRecord:
    return ActivationRecord(
        activation_id=target.activation_id,
        state=state,
        source_head=target.source_head,
        source_tree=target.source_tree,
        m9a_freeze_digest=target.m9a_freeze_digest,
        browser_bundle_digest=target.browser_bundle_digest,
        native_manifest_digest=target.native_manifest_digest,
        writer_enabled=state == "ACTIVE",
        intake_enabled=state == "ACTIVE",
    )


def _revalidate_subject(*, authority: Path, deployed: Path, plan: Mapping[str, Any], allow_records: tuple[ActivationRecord, ...]) -> dict[str, Any]:
    """Re-read every immutable identity immediately before a semantic write."""

    subject = _subject(
        authority=authority,
        deployed=deployed,
        historical_reconciliation_id=plan["historical_reconciliation_id"],
        historical_reconciliation_plan_sha256=plan["historical_reconciliation_plan_sha256"],
        allow_records=allow_records,
    )
    bootstrap = subject["bootstrap"]
    client = subject["client"]
    verification = subject["verification"]
    m3c = subject["m3c"]
    expected = {
        "state_sha256": plan["bootstrap_state_sha256"],
        "active_manifest_sha256": plan["active_manifest_sha256"],
        "previous_manifest_sha256": plan["previous_manifest_sha256"],
    }
    for field, value in expected.items():
        if bootstrap.get(field) != value:
            _fail("recovery_subject_changed", f"Bootstrap subject differs in {field}")
    client_expected = {
        "source_head": plan["source_head"],
        "source_tree": plan["source_tree"],
        "client_plan_sha256": plan["client_plan_sha256"],
        "browser_bundle_digest": plan["browser_bundle_digest"],
        "native_manifest_sha256": plan["native_manifest_digest"],
        "native_manifest_path": plan["native_manifest_path"],
        "native_host_executable_sha256": plan["native_executable_sha256"],
        "native_config_sha256": plan["native_config_sha256"],
        "protocol_generation": plan["protocol_generation"],
        "generation_id": plan["generation_id"],
        "browser_extension_id": plan["browser_extension_id"],
        "native_host_name": plan["native_host_name"],
    }
    for field, value in client_expected.items():
        if client.get(field) != value:
            _fail("recovery_subject_changed", f"client subject differs in {field}")
    if verification.get("verification_sha256") != plan["client_verification_sha256"]:
        _fail("recovery_subject_changed", "Browser client verification differs")
    if m3c.get("control_digest") != plan["m3c_control_digest"] or m3c.get("kill_switch_digest") != plan["m3c_kill_switch_digest"]:
        _fail("recovery_subject_changed", "M3c subject differs")
    return subject


def prepare_missing_m9b_recovery(
    *,
    authority_root: str | Path,
    deployed_runtime_root: str | Path,
    recovery_id: str,
    historical_reconciliation_id: str,
    historical_reconciliation_plan_sha256: str,
) -> dict[str, Any]:
    """Prepare an immutable missing-M9b recovery subject; never writes M9b."""

    authority = _absolute_path(authority_root, field="authority_root")
    deployed = _absolute_path(deployed_runtime_root, field="deployed_runtime_root")
    recovery_id = _id(recovery_id, "recovery_id")
    with BootstrapLock(authority / "bootstrap.lock"):
        subject = _subject(
            authority=authority,
            deployed=deployed,
            historical_reconciliation_id=historical_reconciliation_id,
            historical_reconciliation_plan_sha256=historical_reconciliation_plan_sha256,
        )
        if subject["current"] is not None:
            _fail("m9b_already_present", "missing-M9b recovery requires exact absence")
        historical_target: ActivationRecord = subject["historical_target"]
        client = subject["client"]
        target = ActivationRecord(
            activation_id=historical_target.activation_id,
            state="ACTIVE",
            source_head=_sha40(client.get("source_head"), "source_head"),
            source_tree=_sha40(client.get("source_tree"), "source_tree"),
            m9a_freeze_digest=historical_target.m9a_freeze_digest,
            browser_bundle_digest=_digest(client.get("browser_bundle_digest"), "browser_bundle_digest"),
            native_manifest_digest=_digest(client.get("native_manifest_sha256"), "native_manifest_digest"),
            writer_enabled=True,
            intake_enabled=True,
        )
        clients = _transition_record(target, "CLIENTS_VERIFIED")
        activating = _transition_record(target, "ACTIVATING")
        payload = {
            "schema": M9B_RECOVERY_PLAN_SCHEMA,
            "recovery_id": recovery_id,
            "recovery_mode": M9B_RECOVERY_MODE,
            "bootstrap_state_sha256": _digest(subject["bootstrap"].get("state_sha256"), "bootstrap_state_sha256"),
            "active_manifest_sha256": _digest(subject["bootstrap"].get("active_manifest_sha256"), "active_manifest_sha256"),
            "previous_manifest_sha256": _digest(subject["bootstrap"].get("previous_manifest_sha256"), "previous_manifest_sha256"),
            "source_head": target.source_head,
            "source_tree": target.source_tree,
            "client_plan_sha256": _digest(client.get("client_plan_sha256"), "client_plan_sha256"),
            "browser_bundle_digest": target.browser_bundle_digest,
            "native_manifest_digest": target.native_manifest_digest,
            "native_executable_sha256": _digest(client.get("native_host_executable_sha256"), "native_executable_sha256"),
            "native_config_sha256": _digest(client.get("native_config_sha256"), "native_config_sha256"),
            "native_manifest_path": str(client.get("native_manifest_path")),
            "client_verification_sha256": _digest(subject["verification"].get("verification_sha256"), "client_verification_sha256"),
            "m3c_control_digest": _digest(subject["m3c"].get("control_digest"), "m3c_control_digest"),
            "m3c_kill_switch_digest": _digest(subject["m3c"].get("kill_switch_digest"), "m3c_kill_switch_digest"),
            "legacy_route_present": False,
            "protocol_generation": client.get("protocol_generation"),
            "generation_id": client.get("generation_id"),
            "browser_extension_id": client.get("browser_extension_id"),
            "native_host_name": client.get("native_host_name"),
            "historical_reconciliation_id": _id(historical_reconciliation_id, "historical_reconciliation_id"),
            "historical_reconciliation_plan_sha256": _digest(historical_reconciliation_plan_sha256, "historical_reconciliation_plan_sha256"),
            "historical_target_m9b_record_sha256": _digest(subject["historical_plan"].get("target_m9b_record_sha256"), "historical_target_m9b_record_sha256"),
            "m9a_freeze_digest": historical_target.m9a_freeze_digest,
            "activation_id": target.activation_id,
            "clients_verified_record": clients.as_dict(),
            "clients_verified_record_sha256": clients.as_dict()["record_digest"],
            "activating_record": activating.as_dict(),
            "activating_record_sha256": activating.as_dict()["record_digest"],
            "target_m9b_record": target.as_dict(),
            "target_m9b_record_sha256": target.as_dict()["record_digest"],
            "deployed_runtime_root": str(deployed),
        }
        plan = {**payload, "plan_sha256": _semantic_digest(payload)}
        _write_immutable(_plan_path(authority, recovery_id), plan)
        _write_immutable(
            _state_path(authority, recovery_id),
            {**{
                "schema": M9B_RECOVERY_STATE_SCHEMA,
                "recovery_id": recovery_id,
                "plan_sha256": plan["plan_sha256"],
                "phase": "PREPARED",
                "bootstrap_state_sha256": plan["bootstrap_state_sha256"],
                "target_record_sha256": plan["target_m9b_record_sha256"],
            }, "state_sha256": _semantic_digest({
                "schema": M9B_RECOVERY_STATE_SCHEMA,
                "recovery_id": recovery_id,
                "plan_sha256": plan["plan_sha256"],
                "phase": "PREPARED",
                "bootstrap_state_sha256": plan["bootstrap_state_sha256"],
                "target_record_sha256": plan["target_m9b_record_sha256"],
            })},
        )
    return query_missing_m9b_recovery(authority_root=authority, recovery_id=recovery_id, expected_plan_sha256=plan["plan_sha256"], deployed_runtime_root=deployed)


def query_missing_m9b_recovery(
    *,
    authority_root: str | Path,
    recovery_id: str,
    expected_plan_sha256: str | None = None,
    deployed_runtime_root: str | Path | None = None,
) -> dict[str, Any]:
    authority = _absolute_path(authority_root, field="authority_root")
    recovery_id = _id(recovery_id, "recovery_id")
    plan = _load_plan(authority, recovery_id, expected_plan_sha256)
    state = _load_state(authority, recovery_id, plan["plan_sha256"])
    result: dict[str, Any] = {
        "schema": M9B_RECOVERY_RESULT_SCHEMA,
        "status": "COMPLETED" if state["phase"] == "COMPLETED" else state["phase"],
        "recovery_id": recovery_id,
        "plan_sha256": plan["plan_sha256"],
        "state": state,
        "plan": plan,
        "production_mutation_performed": False,
    }
    if deployed_runtime_root is not None:
        current = read_activation(_absolute_path(deployed_runtime_root, field="deployed_runtime_root"))
        target = ActivationRecord.from_mapping(plan["target_m9b_record"])
        result["m9b_record"] = current.as_dict() if current is not None else None
        result["target_matches"] = current is not None and current.as_dict() == target.as_dict()
        if state["phase"] == "COMPLETED" and result["target_matches"] is not True:
            _fail("recovery_readback_mismatch", "completed M9b recovery does not match deployed record")
    return result


def recover_missing_m9b(
    *,
    authority_root: str | Path,
    recovery_id: str,
    expected_plan_sha256: str,
    deployed_runtime_root: str | Path,
    operator_approved: bool,
    fault_hook: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Apply one exact missing-M9b plan with replay-safe roll-forward semantics."""

    if operator_approved is not True:
        _fail("operator_approval_required", "M9b recovery requires explicit operator approval")
    authority = _absolute_path(authority_root, field="authority_root")
    deployed = _absolute_path(deployed_runtime_root, field="deployed_runtime_root")
    recovery_id = _id(recovery_id, "recovery_id")
    expected = _digest(expected_plan_sha256, "expected_plan_sha256")
    with BootstrapLock(authority / "bootstrap.lock"):
        plan = _load_plan(authority, recovery_id, expected)
        state = _load_state(authority, recovery_id, plan["plan_sha256"])
        clients = ActivationRecord.from_mapping(plan["clients_verified_record"])
        activating = ActivationRecord.from_mapping(plan["activating_record"])
        target = ActivationRecord.from_mapping(plan["target_m9b_record"])
        current = read_activation(deployed)
        if current is not None and all(current.as_dict() != record.as_dict() for record in (clients, activating, target)):
            _fail("recovery_foreign_m9b", "deployed M9b record is foreign to the immutable recovery plan")
        # Full subject revalidation happens immediately before every semantic write.
        _revalidate_subject(authority=authority, deployed=deployed, plan=plan, allow_records=(clients, activating, target))
        if state["phase"] == "COMPLETED":
            if current is None or current.as_dict() != target.as_dict():
                _fail("recovery_foreign_m9b", "completed recovery points at a foreign or missing M9b record")
            return query_missing_m9b_recovery(authority_root=authority, recovery_id=recovery_id, expected_plan_sha256=expected, deployed_runtime_root=deployed)

        if state["phase"] == "PREPARED":
            if current is None:
                _revalidate_subject(authority=authority, deployed=deployed, plan=plan, allow_records=(clients, activating, target))
                _fault(fault_hook, "before_clients_verified_write")
                write_activation(deployed, clients)
                _fault(fault_hook, "after_clients_verified_commit_before_journal")
                current = read_activation(deployed)
            if current is None or current.as_dict() != clients.as_dict():
                _fail("recovery_readback_mismatch", "CLIENTS_VERIFIED record did not read back exactly")
            state = _write_state(
                authority, recovery_id=recovery_id, plan_sha256=plan["plan_sha256"], phase="CLIENTS_VERIFIED",
                bootstrap_state_sha256=plan["bootstrap_state_sha256"], target_record_sha256=plan["target_m9b_record_sha256"],
                fault_hook=fault_hook,
            )

        if state["phase"] == "CLIENTS_VERIFIED":
            current = read_activation(deployed)
            if current is None or all(current.as_dict() != record.as_dict() for record in (clients, activating)):
                _fail("recovery_foreign_m9b", "M9b is not the planned CLIENTS_VERIFIED/ACTIVATING record")
            if current.as_dict() == clients.as_dict():
                _revalidate_subject(authority=authority, deployed=deployed, plan=plan, allow_records=(clients, activating, target))
                _fault(fault_hook, "before_activating_write")
                write_activation(deployed, activating)
                _fault(fault_hook, "after_activating_commit_before_journal")
                current = read_activation(deployed)
            if current is None or current.as_dict() != activating.as_dict():
                _fail("recovery_readback_mismatch", "ACTIVATING record did not read back exactly")
            state = _write_state(
                authority, recovery_id=recovery_id, plan_sha256=plan["plan_sha256"], phase="ACTIVATING",
                bootstrap_state_sha256=plan["bootstrap_state_sha256"], target_record_sha256=plan["target_m9b_record_sha256"],
                fault_hook=fault_hook,
            )

        if state["phase"] == "ACTIVATING":
            current = read_activation(deployed)
            if current is None or all(current.as_dict() != record.as_dict() for record in (activating, target)):
                _fail("recovery_foreign_m9b", "M9b is not the planned ACTIVATING/ACTIVE record")
            if current.as_dict() == activating.as_dict():
                _revalidate_subject(authority=authority, deployed=deployed, plan=plan, allow_records=(clients, activating, target))
                _fault(fault_hook, "before_active_write")
                write_activation(deployed, target)
                _fault(fault_hook, "after_active_commit_before_journal")
                current = read_activation(deployed)
            if current is None or current.as_dict() != target.as_dict():
                _fail("recovery_readback_mismatch", "ACTIVE record did not read back exactly")
            state = _write_state(
                authority, recovery_id=recovery_id, plan_sha256=plan["plan_sha256"], phase="ACTIVE",
                bootstrap_state_sha256=plan["bootstrap_state_sha256"], target_record_sha256=plan["target_m9b_record_sha256"],
                fault_hook=fault_hook,
            )

        if state["phase"] == "ACTIVE":
            current = read_activation(deployed)
            if current is None or current.as_dict() != target.as_dict():
                _fail("recovery_foreign_m9b", "ACTIVE recovery record is missing or foreign")
            state = _write_state(
                authority, recovery_id=recovery_id, plan_sha256=plan["plan_sha256"], phase="COMPLETED",
                bootstrap_state_sha256=plan["bootstrap_state_sha256"], target_record_sha256=plan["target_m9b_record_sha256"],
                fault_hook=fault_hook,
            )
        result = query_missing_m9b_recovery(authority_root=authority, recovery_id=recovery_id, expected_plan_sha256=expected, deployed_runtime_root=deployed)
        result["state"] = state
        result["production_mutation_performed"] = True
        return result


__all__ = [
    "M9B_RECOVERY_MODE",
    "M9B_RECOVERY_PLAN_SCHEMA",
    "M9B_RECOVERY_RESULT_SCHEMA",
    "M9B_RECOVERY_STATE_SCHEMA",
    "M9bRecoveryError",
    "prepare_missing_m9b_recovery",
    "query_missing_m9b_recovery",
    "recover_missing_m9b",
]

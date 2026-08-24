"""Canonical post-M11c M9b client-gate reconciliation.

M11c changes the external Bootstrap and Native route persistence domains in a
recoverable transaction.  The Native entry point still reads its M9b record
from the deployed runtime root, so a successful Bootstrap cutover must be
followed by this exact, lock-protected reconciliation before production
acceptance can be reported.  This module owns only that narrow transition.
"""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, NoReturn

from bdb_shared.evidence import canonical_json_bytes, semantic_digest
from bdb_vnext.bootstrap import BootstrapLock, _absolute_path, _load_json
from bdb_vnext.m11c_active_reader import observe_bootstrap_activation
from bdb_vnext.m11c_post_active_maintenance import query_post_active_maintenance
from bdb_vnext.m11c_windows_clients import (
    M11cClientError,
    observe_windows_native_routes,
    query_client_plan,
    require_client_verification,
)
from bdb_vnext.m9b_activation import ActivationRecord, M9bActivationError, read_activation, write_activation


M9B_RECONCILIATION_PLAN_SCHEMA = "bdb-vnext-m9b-post-maintenance-reconciliation-plan-v1"
M9B_RECONCILIATION_PLAN_SCHEMA_V2 = "bdb-vnext-m9b-post-maintenance-reconciliation-plan-v2"
M9B_RECONCILIATION_STATE_SCHEMA = "bdb-vnext-m9b-post-maintenance-reconciliation-state-v1"
M9B_RECONCILIATION_RESULT_SCHEMA = "bdb-vnext-m9b-post-maintenance-reconciliation-result-v1"
_RECONCILIATION_DIR = "maintenance/m9b-reconciliation"


class M9bReconciliationError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def _fail(code: str, message: str, *, details: Mapping[str, Any] | None = None) -> NoReturn:
    raise M9bReconciliationError(code, message, details=details)


def _digest(value: Mapping[str, Any]) -> str:
    return semantic_digest(dict(value))


def _digest_field(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:") or len(value) != 71:
        _fail("reconciliation_identity_invalid", f"{field} must be an exact sha256 digest")
    try:
        int(value[7:], 16)
    except ValueError:
        _fail("reconciliation_identity_invalid", f"{field} must be an exact sha256 digest")
    return value


def _id(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128 or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_." for ch in value):
        _fail("reconciliation_identity_invalid", f"{field} is invalid")
    return value


def _stable_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise M9bReconciliationError("reconciliation_record_invalid", f"cannot read {path.name}") from exc
    if not isinstance(value, Mapping):
        _fail("reconciliation_record_invalid", f"{path.name} must contain an object")
    return {str(key): item for key, item in value.items()}


def _atomic_json(path: Path, value: Mapping[str, Any], *, fault_hook: Callable[[str], None] | None = None) -> None:
    payload = canonical_json_bytes(dict(value))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            if fault_hook:
                fault_hook("journal_during_temp_write")
            handle.flush()
            os.fsync(handle.fileno())
            if fault_hook:
                fault_hook("journal_after_fsync")
        os.replace(temporary, path)
    except Exception as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise M9bReconciliationError("reconciliation_state_write_failed", "reconciliation state could not be written") from exc


def _plan_path(authority: Path, maintenance_id: str) -> Path:
    return authority / _RECONCILIATION_DIR / "plans" / f"{_id(maintenance_id, 'maintenance_id')}.json"


def _state_path(authority: Path, maintenance_id: str) -> Path:
    return authority / _RECONCILIATION_DIR / "states" / f"{_id(maintenance_id, 'maintenance_id')}.json"


def _write_immutable(path: Path, value: Mapping[str, Any]) -> None:
    payload = canonical_json_bytes(dict(value))
    if path.exists():
        try:
            if path.read_bytes() != payload:
                _fail("reconciliation_record_conflict", f"immutable record {path.name} differs")
        except OSError as exc:
            raise M9bReconciliationError("reconciliation_record_read_failed", "immutable record cannot be read") from exc
        return
    _atomic_json(path, value)


def _write_state(
    authority: Path,
    *,
    maintenance_id: str,
    plan_sha256: str,
    phase: str,
    m9b_phase: str,
    bootstrap_state_sha256: str,
    old_record_sha256: str,
    target_record_sha256: str,
    fault_hook: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    payload = {
        "schema": M9B_RECONCILIATION_STATE_SCHEMA,
        "maintenance_id": maintenance_id,
        "plan_sha256": plan_sha256,
        "phase": phase,
        "m9b_phase": m9b_phase,
        "bootstrap_state_sha256": bootstrap_state_sha256,
        "old_record_sha256": old_record_sha256,
        "target_record_sha256": target_record_sha256,
    }
    value = {**payload, "state_sha256": _digest(payload)}
    _atomic_json(_state_path(authority, maintenance_id), value, fault_hook=fault_hook)
    return value


def _load_plan(authority: Path, maintenance_id: str, expected_plan_sha256: str | None = None) -> dict[str, Any]:
    value = _stable_json(_plan_path(authority, maintenance_id))
    required_v1 = {
        "schema", "maintenance_id", "maintenance_plan_sha256", "route_transition_plan_sha256",
        "bootstrap_state_sha256", "active_manifest_sha256", "previous_manifest_sha256",
        "candidate_source_head", "candidate_source_tree", "candidate_manifest_sha256",
        "candidate_bundle_sha256", "candidate_client_plan_sha256", "candidate_native_manifest_path",
        "browser_bundle_digest", "native_manifest_digest", "m9a_freeze_digest",
        "m3c_control_digest", "m3c_kill_switch_digest", "old_m9b_record", "target_m9b_record",
        "old_m9b_record_sha256", "target_m9b_record_sha256", "deployed_runtime_root",
        "candidate_client_runtime_root", "legacy_route_present", "recovery_mode", "plan_sha256",
    }
    required = required_v1
    if value.get("schema") == M9B_RECONCILIATION_PLAN_SCHEMA_V2:
        required = required_v1 | {"route_rebind_id", "route_rebind_plan_sha256"}
    if set(value) != required or value.get("schema") not in {M9B_RECONCILIATION_PLAN_SCHEMA, M9B_RECONCILIATION_PLAN_SCHEMA_V2}:
        _fail("reconciliation_plan_invalid", "reconciliation plan fields differ")
    plan_sha = _digest_field(value.get("plan_sha256"), "plan_sha256")
    payload = dict(value)
    payload.pop("plan_sha256")
    if _digest(payload) != plan_sha:
        _fail("reconciliation_plan_digest_mismatch", "reconciliation plan digest differs")
    if expected_plan_sha256 is not None and plan_sha != _digest_field(expected_plan_sha256, "expected_plan_sha256"):
        _fail("reconciliation_plan_stale", "operator subject is bound to a different reconciliation plan")
    if value.get("schema") == M9B_RECONCILIATION_PLAN_SCHEMA_V2:
        _id(value.get("route_rebind_id"), "route_rebind_id")
        _digest_field(value.get("route_rebind_plan_sha256"), "route_rebind_plan_sha256")
    return value


def _load_state(authority: Path, maintenance_id: str, plan_sha256: str) -> dict[str, Any]:
    value = _stable_json(_state_path(authority, maintenance_id))
    required = {
        "schema", "maintenance_id", "plan_sha256", "phase", "m9b_phase", "bootstrap_state_sha256",
        "old_record_sha256", "target_record_sha256", "state_sha256",
    }
    if set(value) != required or value.get("schema") != M9B_RECONCILIATION_STATE_SCHEMA or value.get("maintenance_id") != maintenance_id:
        _fail("reconciliation_state_invalid", "reconciliation state fields differ")
    if value.get("plan_sha256") != plan_sha256:
        _fail("reconciliation_state_stale", "reconciliation state binds another plan")
    supplied = _digest_field(value.get("state_sha256"), "state_sha256")
    payload = dict(value)
    payload.pop("state_sha256")
    if _digest(payload) != supplied:
        _fail("reconciliation_state_digest_mismatch", "reconciliation state digest differs")
    if value.get("phase") not in {"PREPARED", "COMPLETED"} or value.get("m9b_phase") not in {"OLD", "COMMITTED", "COMPLETED"}:
        _fail("reconciliation_state_invalid", "reconciliation state phase is unsupported")
    return value


def _m3c_state(runtime: Path) -> dict[str, Any]:
    control_root = runtime / "control"
    try:
        kill = _load_json(control_root / "m3c-kill-switch.json", field="m3c_kill_switch")
        control = _load_json(control_root / "m3c-control.json", field="m3c_control")
    except Exception as exc:
        _fail("m3c_state_unavailable", "canonical M3c state cannot be observed")
    if (
        kill.get("schema") != "bdb-vnext-m3c-kill-switch-v1"
        or kill.get("admission_enabled") is not True
        or control.get("schema") != "bdb-vnext-m3c-control-v2"
        or control.get("alternate_admission") is not False
        or control.get("legacy_import") is not False
    ):
        _fail("m3c_not_canonical", "M3c is not enabled in the canonical-only mode")
    return {
        "kill_switch": dict(kill),
        "control": dict(control),
        "kill_switch_digest": _digest(kill),
        "control_digest": _digest(control),
    }


def _subject(
    *,
    authority: Path,
    deployed_runtime: Path,
    client_runtime: Path,
    maintenance_id: str,
    maintenance_plan_sha256: str,
    route_rebind_id: str | None = None,
    route_rebind_plan_sha256: str | None = None,
) -> dict[str, Any]:
    try:
        maintenance = query_post_active_maintenance(authority_root=authority, maintenance_id=maintenance_id)
    except Exception as exc:
        _fail(getattr(exc, "code", "maintenance_subject_unavailable"), str(exc))
    plan = maintenance.get("plan")
    route_plan = maintenance.get("route_transition_plan")
    route_state = maintenance.get("route_transition_state")
    if not isinstance(plan, Mapping) or not isinstance(route_plan, Mapping) or not isinstance(route_state, Mapping):
        _fail("maintenance_subject_invalid", "maintenance plan/route state is unavailable")
    if plan.get("plan_sha256") != maintenance_plan_sha256 or plan.get("maintenance_id") != maintenance_id:
        _fail("maintenance_plan_stale", "maintenance plan differs from the requested subject")
    if route_plan.get("route_transition_plan_sha256") != plan.get("route_transition_plan_sha256"):
        _fail("route_transition_plan_stale", "route transition plan differs from maintenance plan")
    if route_state.get("phase") != "COMPLETED" or route_state.get("bootstrap_phase") != "NEW":
        _fail("route_transition_incomplete", "post-active route transition is not complete")

    route_rebind: dict[str, Any] | None = None
    if (route_rebind_id is None) != (route_rebind_plan_sha256 is None):
        _fail("route_rebind_binding_invalid", "route rebind identity must be supplied as an exact pair")
    if route_rebind_id is not None and route_rebind_plan_sha256 is not None:
        try:
            from bdb_vnext.m11c_client_route_rebind import query_client_route_rebind

            route_rebind = query_client_route_rebind(
                authority_root=authority,
                rebind_id=route_rebind_id,
                expected_plan_sha256=route_rebind_plan_sha256,
            )
        except Exception as exc:
            _fail(getattr(exc, "code", "route_rebind_subject_unavailable"), str(exc))
        route_plan = route_rebind.get("plan")
        route_state_doc = route_rebind.get("state")
        if not isinstance(route_plan, Mapping) or not isinstance(route_state_doc, Mapping):
            _fail("route_rebind_subject_invalid", "route rebind plan/state is incomplete")
        if route_state_doc.get("phase") != "COMPLETED" or route_plan.get("original_maintenance_id") != maintenance_id or route_plan.get("original_maintenance_plan_sha256") != maintenance_plan_sha256:
            _fail("route_rebind_lineage_mismatch", "route rebind is not bound to the exact maintenance lineage")
        if Path(str(route_plan.get("target_client_runtime_root"))).absolute() != client_runtime.absolute():
            _fail("route_rebind_client_mismatch", "route rebind target client root differs")

    bootstrap = observe_bootstrap_activation(authority_root=authority)
    if bootstrap.get("status") != "ACTIVE" or bootstrap.get("production_activation_performed") is not True:
        _fail("bootstrap_not_active", "Bootstrap is not ACTIVE")
    state = bootstrap.get("state")
    slots = bootstrap.get("slots")
    if not isinstance(state, Mapping) or not isinstance(slots, Mapping):
        _fail("bootstrap_subject_invalid", "Bootstrap ACTIVE state is incomplete")
    if state.get("state_sha256") != route_state.get("bootstrap_state_sha256"):
        _fail("bootstrap_route_mismatch", "route transition is bound to another Bootstrap state")
    if state.get("cutover_plan_sha256") != maintenance_plan_sha256:
        _fail("bootstrap_plan_mismatch", "Bootstrap ACTIVE is not bound to the maintenance plan")
    active = slots.get("ACTIVE")
    if not isinstance(active, Mapping) or active.get("source_commit") != plan.get("candidate_source_head"):
        _fail("bootstrap_source_mismatch", "Bootstrap ACTIVE source differs from candidate")
    if active.get("source_tree") is not None and active.get("source_tree") != plan.get("candidate_source_tree"):
        _fail("bootstrap_source_mismatch", "Bootstrap ACTIVE tree differs from candidate")

    try:
        queried_client = query_client_plan(runtime_root=client_runtime)
        client_plan = queried_client["plan"]
        verification = require_client_verification(runtime_root=client_runtime, expected_client_plan_sha256=client_plan["client_plan_sha256"])
        routes = observe_windows_native_routes(runtime_root=client_runtime)
    except M11cClientError as exc:
        _fail(exc.code, str(exc))
    expected_client_plan_sha256 = plan.get("client_plan_sha256")
    expected_browser_bundle_digest = plan.get("browser_bundle_digest")
    expected_native_manifest_digest = plan.get("native_manifest_digest")
    expected_native_manifest_path = plan.get("candidate_native_manifest_path")
    if route_rebind is not None:
        route_plan = route_rebind["plan"]
        expected_client_plan_sha256 = route_plan["target_client_plan_sha256"]
        expected_browser_bundle_digest = route_plan["browser_bundle_digest"]
        expected_native_manifest_digest = route_plan["target_native_manifest_sha256"]
        expected_native_manifest_path = route_plan["target_native_manifest_path"]
        if route_plan.get("target_source_head") != plan.get("candidate_source_head") or route_plan.get("target_source_tree") != plan.get("candidate_source_tree"):
            _fail("route_rebind_source_mismatch", "route rebind source differs from maintenance source")
    if (
        client_plan.get("client_plan_sha256") != expected_client_plan_sha256
        or client_plan.get("source_head") != plan.get("candidate_source_head")
        or client_plan.get("source_tree") != plan.get("candidate_source_tree")
        or client_plan.get("native_manifest_sha256") != expected_native_manifest_digest
        or client_plan.get("browser_bundle_digest") != expected_browser_bundle_digest
        or client_plan.get("native_manifest_path") != expected_native_manifest_path
    ):
        _fail("client_subject_mismatch", "candidate client plan differs from maintenance subject")
    if routes.get("target_registered") is not True or routes.get("target_conflict") or routes.get("legacy_route_present"):
        _fail("native_route_not_exclusive", "candidate Native route is not exclusive and Legacy-free")

    current_m9b = read_activation(deployed_runtime)
    if current_m9b is None or current_m9b.state != "ACTIVE" or current_m9b.writer_enabled is not True or current_m9b.intake_enabled is not True:
        _fail("m9b_not_active", "deployed M9b record is not ACTIVE")
    m3c = _m3c_state(deployed_runtime)
    return {
        "maintenance": dict(plan),
        "route_plan": dict(route_plan),
        "route_state": dict(route_state),
        "bootstrap": dict(state),
        "active": dict(active),
        "client_plan": dict(client_plan),
        "client_verification": dict(verification),
        "routes": dict(routes),
        "m9b": current_m9b,
        "m3c": m3c,
        "route_rebind": route_rebind,
    }


def _target_record(subject: Mapping[str, Any]) -> ActivationRecord:
    current = subject["m9b"]
    plan = subject["maintenance"]
    client = subject["client_plan"]
    return ActivationRecord(
        activation_id=current.activation_id,
        state="ACTIVE",
        source_head=str(client.get("source_head", plan["candidate_source_head"])),
        source_tree=str(client.get("source_tree", plan["candidate_source_tree"])),
        m9a_freeze_digest=current.m9a_freeze_digest,
        browser_bundle_digest=str(client.get("browser_bundle_digest", plan["browser_bundle_digest"])),
        native_manifest_digest=str(client.get("native_manifest_sha256", plan["native_manifest_digest"])),
        writer_enabled=True,
        intake_enabled=True,
    )


def prepare_post_active_reconciliation(
    *,
    authority_root: str | Path,
    deployed_runtime_root: str | Path,
    candidate_client_runtime_root: str | Path,
    maintenance_id: str,
    maintenance_plan_sha256: str,
    route_rebind_id: str | None = None,
    route_rebind_plan_sha256: str | None = None,
) -> dict[str, Any]:
    """Create an immutable exact post-maintenance M9b reconciliation subject."""

    authority = _absolute_path(authority_root, field="authority_root")
    deployed = _absolute_path(deployed_runtime_root, field="deployed_runtime_root")
    client = _absolute_path(candidate_client_runtime_root, field="candidate_client_runtime_root")
    maintenance_id = _id(maintenance_id, "maintenance_id")
    maintenance_sha = _digest_field(maintenance_plan_sha256, "maintenance_plan_sha256")
    with BootstrapLock(authority / "bootstrap.lock"):
        subject = _subject(
            authority=authority,
            deployed_runtime=deployed,
            client_runtime=client,
            maintenance_id=maintenance_id,
            maintenance_plan_sha256=maintenance_sha,
            route_rebind_id=route_rebind_id,
            route_rebind_plan_sha256=route_rebind_plan_sha256,
        )
        target = _target_record(subject)
        old = subject["m9b"]
        route_rebind = subject.get("route_rebind")
        client_subject = subject["client_plan"]
        plan_schema = M9B_RECONCILIATION_PLAN_SCHEMA_V2 if route_rebind is not None else M9B_RECONCILIATION_PLAN_SCHEMA
        plan_payload = {
            "schema": plan_schema,
            "maintenance_id": maintenance_id,
            "maintenance_plan_sha256": maintenance_sha,
            "route_transition_plan_sha256": subject["maintenance"]["route_transition_plan_sha256"],
            "bootstrap_state_sha256": subject["bootstrap"]["state_sha256"],
            "active_manifest_sha256": subject["bootstrap"]["active_manifest_sha256"],
            "previous_manifest_sha256": subject["bootstrap"]["previous_manifest_sha256"],
            "candidate_source_head": subject["maintenance"]["candidate_source_head"],
            "candidate_source_tree": subject["maintenance"]["candidate_source_tree"],
            "candidate_manifest_sha256": subject["maintenance"]["candidate_manifest_sha256"],
            "candidate_bundle_sha256": subject["maintenance"]["candidate_bundle_sha256"],
            "candidate_client_plan_sha256": client_subject.get("client_plan_sha256", subject["maintenance"]["client_plan_sha256"]),
            "candidate_native_manifest_path": client_subject.get("native_manifest_path", subject["maintenance"]["candidate_native_manifest_path"]),
            "browser_bundle_digest": client_subject.get("browser_bundle_digest", subject["maintenance"]["browser_bundle_digest"]),
            "native_manifest_digest": client_subject.get("native_manifest_sha256", subject["maintenance"]["native_manifest_digest"]),
            "m9a_freeze_digest": old.m9a_freeze_digest,
            "m3c_control_digest": subject["m3c"]["control_digest"],
            "m3c_kill_switch_digest": subject["m3c"]["kill_switch_digest"],
            "old_m9b_record": old.as_dict(),
            "target_m9b_record": target.as_dict(),
            "old_m9b_record_sha256": old.as_dict()["record_digest"],
            "target_m9b_record_sha256": target.as_dict()["record_digest"],
            "deployed_runtime_root": str(deployed),
            "candidate_client_runtime_root": str(client),
            "legacy_route_present": False,
            "recovery_mode": "NO_ROLLBACK_AFTER_BOOTSTRAP_ACTIVE_ROLL_FORWARD_ONLY",
        }
        if route_rebind is not None:
            plan_payload["route_rebind_id"] = route_rebind["rebind_id"]
            plan_payload["route_rebind_plan_sha256"] = route_rebind["plan"]["route_rebind_plan_sha256"]
        plan = {**plan_payload, "plan_sha256": _digest(plan_payload)}
        _write_immutable(_plan_path(authority, maintenance_id), plan)
        _write_state(
            authority,
            maintenance_id=maintenance_id,
            plan_sha256=plan["plan_sha256"],
            phase="PREPARED",
            m9b_phase="OLD",
            bootstrap_state_sha256=subject["bootstrap"]["state_sha256"],
            old_record_sha256=plan["old_m9b_record_sha256"],
            target_record_sha256=plan["target_m9b_record_sha256"],
        )
    return query_post_active_reconciliation(
        authority_root=authority,
        maintenance_id=maintenance_id,
        expected_plan_sha256=plan["plan_sha256"],
        deployed_runtime_root=deployed,
    )


def query_post_active_reconciliation(
    *,
    authority_root: str | Path,
    maintenance_id: str,
    expected_plan_sha256: str | None = None,
    deployed_runtime_root: str | Path | None = None,
) -> dict[str, Any]:
    authority = _absolute_path(authority_root, field="authority_root")
    maintenance_id = _id(maintenance_id, "maintenance_id")
    plan = _load_plan(authority, maintenance_id, expected_plan_sha256)
    state = _load_state(authority, maintenance_id, plan["plan_sha256"])
    result: dict[str, Any] = {
        "schema": M9B_RECONCILIATION_RESULT_SCHEMA,
        "status": "COMPLETED" if state["phase"] == "COMPLETED" else "PREPARED",
        "maintenance_id": maintenance_id,
        "plan_sha256": plan["plan_sha256"],
        "state": state,
        "plan": plan,
        "production_mutation_performed": False,
    }
    if deployed_runtime_root is not None:
        deployed = _absolute_path(deployed_runtime_root, field="deployed_runtime_root")
        observed = read_activation(deployed)
        target = ActivationRecord.from_mapping(plan["target_m9b_record"])
        result["m9b_record"] = observed.as_dict() if observed is not None else None
        result["target_matches"] = observed is not None and observed.as_dict() == target.as_dict()
        if state["phase"] == "COMPLETED" and result["target_matches"] is not True:
            _fail("reconciliation_readback_mismatch", "completed reconciliation does not match deployed M9b")
        if plan.get("schema") == M9B_RECONCILIATION_PLAN_SCHEMA_V2:
            try:
                from bdb_vnext.m11c_client_route_rebind import query_client_route_rebind

                route_rebind = query_client_route_rebind(
                    authority_root=authority,
                    rebind_id=plan["route_rebind_id"],
                    expected_plan_sha256=plan["route_rebind_plan_sha256"],
                )
            except Exception as exc:
                _fail(getattr(exc, "code", "route_rebind_subject_unavailable"), str(exc))
            if route_rebind["state"].get("phase") != "COMPLETED":
                _fail("route_rebind_incomplete", "M9b reconciliation requires completed route rebind")
            result["route_rebind"] = route_rebind
    return result


def _revalidate_subject_for_plan(
    *,
    subject: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> None:
    """Re-read the complete subject while the Bootstrap lock is held."""

    maintenance = subject.get("maintenance")
    bootstrap = subject.get("bootstrap")
    route_state = subject.get("route_state")
    m3c = subject.get("m3c")
    client = subject.get("client_plan")
    if not all(isinstance(value, Mapping) for value in (maintenance, bootstrap, route_state, m3c, client)):
        _fail("reconciliation_subject_changed", "current maintenance subject is incomplete")
    route_rebind = subject.get("route_rebind")
    if plan.get("schema") == M9B_RECONCILIATION_PLAN_SCHEMA_V2:
        if not isinstance(route_rebind, Mapping):
            _fail("reconciliation_subject_changed", "route rebind lineage is unavailable")
        if route_rebind.get("rebind_id") != plan.get("route_rebind_id") or route_rebind.get("plan", {}).get("route_rebind_plan_sha256") != plan.get("route_rebind_plan_sha256"):
            _fail("reconciliation_subject_changed", "route rebind lineage differs")
        if route_rebind.get("state", {}).get("phase") != "COMPLETED":
            _fail("reconciliation_subject_changed", "route rebind is not completed")
    expected = {
        "maintenance_plan_sha256": maintenance.get("plan_sha256"),
        "route_transition_plan_sha256": maintenance.get("route_transition_plan_sha256"),
        "bootstrap_state_sha256": bootstrap.get("state_sha256"),
        "active_manifest_sha256": bootstrap.get("active_manifest_sha256"),
        "previous_manifest_sha256": bootstrap.get("previous_manifest_sha256"),
        "candidate_source_head": maintenance.get("candidate_source_head"),
        "candidate_source_tree": maintenance.get("candidate_source_tree"),
        "candidate_manifest_sha256": maintenance.get("candidate_manifest_sha256"),
        "candidate_bundle_sha256": maintenance.get("candidate_bundle_sha256"),
        "candidate_client_plan_sha256": client.get("client_plan_sha256", maintenance.get("client_plan_sha256")),
        "candidate_native_manifest_path": client.get("native_manifest_path", maintenance.get("candidate_native_manifest_path")),
        "browser_bundle_digest": client.get("browser_bundle_digest", maintenance.get("browser_bundle_digest")),
        "native_manifest_digest": client.get("native_manifest_sha256", maintenance.get("native_manifest_digest")),
        "m3c_control_digest": m3c.get("control_digest"),
        "m3c_kill_switch_digest": m3c.get("kill_switch_digest"),
    }
    for field, value in expected.items():
        if value != plan.get(field):
            _fail("reconciliation_subject_changed", f"current subject differs in {field}")
    if route_state.get("phase") != "COMPLETED" or route_state.get("bootstrap_phase") != "NEW":
        _fail("reconciliation_subject_changed", "route transition is no longer complete")
    target = _target_record(subject).as_dict()
    if target != plan.get("target_m9b_record"):
        _fail("reconciliation_subject_changed", "current target M9b record differs from the immutable plan")
    current = subject.get("m9b")
    if not isinstance(current, ActivationRecord) or current.as_dict() not in (
        plan.get("old_m9b_record"),
        plan.get("target_m9b_record"),
    ):
        _fail("reconciliation_subject_changed", "current M9b record is not the planned old or target record")
def reconcile_post_active_m9b(
    *,
    authority_root: str | Path,
    deployed_runtime_root: str | Path,
    maintenance_id: str,
    expected_plan_sha256: str,
    fault_hook: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Commit the exact M9b target, with replay-safe roll-forward semantics."""

    authority = _absolute_path(authority_root, field="authority_root")
    deployed = _absolute_path(deployed_runtime_root, field="deployed_runtime_root")
    maintenance_id = _id(maintenance_id, "maintenance_id")
    expected = _digest_field(expected_plan_sha256, "expected_plan_sha256")
    with BootstrapLock(authority / "bootstrap.lock"):
        plan = _load_plan(authority, maintenance_id, expected)
        state = _load_state(authority, maintenance_id, plan["plan_sha256"])
        target = ActivationRecord.from_mapping(plan["target_m9b_record"])
        current = read_activation(deployed)
        if current is None:
            _fail("m9b_not_active", "deployed M9b record disappeared")
        subject = _subject(
            authority=authority,
            deployed_runtime=deployed,
            client_runtime=_absolute_path(plan["candidate_client_runtime_root"], field="candidate_client_runtime_root"),
            maintenance_id=maintenance_id,
            maintenance_plan_sha256=plan["maintenance_plan_sha256"],
            route_rebind_id=plan.get("route_rebind_id"),
            route_rebind_plan_sha256=plan.get("route_rebind_plan_sha256"),
        )
        _revalidate_subject_for_plan(subject=subject, plan=plan)
        if state["phase"] == "COMPLETED":
            if current.as_dict() != target.as_dict():
                _fail("reconciliation_foreign_m9b", "completed reconciliation points at a foreign M9b record")
            return query_post_active_reconciliation(
                authority_root=authority,
                maintenance_id=maintenance_id,
                expected_plan_sha256=expected,
                deployed_runtime_root=deployed,
            )
        if current.as_dict() == target.as_dict():
            completed = _write_state(
                authority,
                maintenance_id=maintenance_id,
                plan_sha256=plan["plan_sha256"],
                phase="COMPLETED",
                m9b_phase="COMPLETED",
                bootstrap_state_sha256=plan["bootstrap_state_sha256"],
                old_record_sha256=plan["old_m9b_record_sha256"],
                target_record_sha256=plan["target_m9b_record_sha256"],
            )
            result = query_post_active_reconciliation(authority_root=authority, maintenance_id=maintenance_id, expected_plan_sha256=expected, deployed_runtime_root=deployed)
            result["state"] = completed
            result["replayed_after_record_commit"] = True
            return result
        if current.as_dict() != plan["old_m9b_record"]:
            _fail("reconciliation_foreign_m9b", "deployed M9b record is neither the exact old nor target record")
        if fault_hook:
            fault_hook("before_m9b_write")
        write_activation(deployed, target, fault_hook=fault_hook)
        observed = read_activation(deployed)
        if observed is None or observed.as_dict() != target.as_dict():
            _fail("reconciliation_readback_mismatch", "M9b record readback differs from target")
        if fault_hook:
            fault_hook("after_m9b_commit_before_journal")
        completed = _write_state(
            authority,
            maintenance_id=maintenance_id,
            plan_sha256=plan["plan_sha256"],
            phase="COMPLETED",
            m9b_phase="COMPLETED",
            bootstrap_state_sha256=plan["bootstrap_state_sha256"],
            old_record_sha256=plan["old_m9b_record_sha256"],
            target_record_sha256=plan["target_m9b_record_sha256"],
        )
        result = query_post_active_reconciliation(authority_root=authority, maintenance_id=maintenance_id, expected_plan_sha256=expected, deployed_runtime_root=deployed)
        result["state"] = completed
        result["replayed_after_record_commit"] = False
        return result


def verify_post_active_reconciliation(
    *,
    authority_root: str | Path,
    deployed_runtime_root: str | Path,
    maintenance_id: str,
    expected_plan_sha256: str,
) -> dict[str, Any]:
    """Read-only exact verification used by M12a and post-apply preflights."""

    return query_post_active_reconciliation(
        authority_root=authority_root,
        maintenance_id=maintenance_id,
        expected_plan_sha256=expected_plan_sha256,
        deployed_runtime_root=deployed_runtime_root,
    )


__all__ = [
    "M9B_RECONCILIATION_PLAN_SCHEMA",
    "M9B_RECONCILIATION_PLAN_SCHEMA_V2",
    "M9B_RECONCILIATION_RESULT_SCHEMA",
    "M9B_RECONCILIATION_STATE_SCHEMA",
    "M9bReconciliationError",
    "prepare_post_active_reconciliation",
    "query_post_active_reconciliation",
    "reconcile_post_active_m9b",
    "verify_post_active_reconciliation",
]

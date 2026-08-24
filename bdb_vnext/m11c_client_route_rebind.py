"""Canonical post-ACTIVE subordinate Native route rebind.

Bootstrap ACTIVE/PREVIOUS are deliberately not changed by this operation.  A
route rebind only repairs the physical HKCU Native Messaging views after a
valid Bootstrap activation, and records an immutable, exact subject under the
same external Bootstrap lock.  The old route is never a recovery target: once
the Bootstrap subject is ACTIVE, recovery is roll-forward to the stable target
or fail closed.
"""

from __future__ import annotations

import os
import re
import secrets
import subprocess
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
    set_windows_target_native_route_view,
)
from bdb_vnext.m9b_activation import read_activation


ROUTE_REBIND_PLAN_SCHEMA = "bdb-vnext-m11c-client-route-rebind-plan-v1"
ROUTE_REBIND_STATE_SCHEMA = "bdb-vnext-m11c-client-route-rebind-state-v1"
ROUTE_REBIND_RESULT_SCHEMA = "bdb-vnext-m11c-client-route-rebind-result-v1"
ROUTE_REBIND_RECOVERY_MODE = "STABLE_TARGET_ROLL_FORWARD_NO_DISPOSABLE_ROLLBACK"
_ROUTE_REBIND_DIR = "maintenance/client-route-rebind"
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ROUTE_VIEWS = ("32", "64")


class ClientRouteRebindError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


class ClientRouteRebindFault(RuntimeError):
    def __init__(self, stage: str) -> None:
        super().__init__(stage)
        self.stage = stage


def _fail(code: str, message: str, *, details: Mapping[str, Any] | None = None) -> NoReturn:
    raise ClientRouteRebindError(code, message, details=details)


def _digest(value: Mapping[str, Any]) -> str:
    return semantic_digest(dict(value))


def _digest_field(value: object, field: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        _fail("route_rebind_identity_invalid", f"{field} must be an exact sha256 digest")
    return value


def _sha40(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA40.fullmatch(value) is None:
        _fail("route_rebind_identity_invalid", f"{field} must be a lowercase 40-character Git SHA")
    return value


def _id(value: object, field: str) -> str:
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._"
    if not isinstance(value, str) or not value or len(value) > 128 or any(ch not in allowed for ch in value):
        _fail("route_rebind_identity_invalid", f"{field} is invalid")
    return value


def _absolute(value: str | Path, field: str) -> Path:
    path = _absolute_path(value, field=field)
    if not path.is_absolute():
        _fail("route_rebind_path_invalid", f"{field} must be absolute")
    return path


def _current_sid() -> str:
    if os.name != "nt":
        return "non-windows-test-subject"
    try:
        output = subprocess.check_output(
            ["whoami", "/user", "/fo", "csv", "/nh"],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ClientRouteRebindError("subject_identity_unavailable", "Windows subject SID cannot be observed") from exc
    match = re.search(r"S-1-[0-9-]+", output)
    if match is None:
        _fail("subject_identity_unavailable", "Windows subject SID is unavailable")
    return match.group(0)


def _plan_path(authority: Path, rebind_id: str) -> Path:
    return authority / _ROUTE_REBIND_DIR / "plans" / f"{_id(rebind_id, 'rebind_id')}.json"


def _state_path(authority: Path, rebind_id: str) -> Path:
    return authority / _ROUTE_REBIND_DIR / "states" / f"{_id(rebind_id, 'rebind_id')}.json"


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(canonical_json_bytes(dict(value)))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ClientRouteRebindError("route_rebind_state_write_failed", "route rebind evidence could not be written") from exc


def _write_immutable(path: Path, value: Mapping[str, Any]) -> None:
    payload = canonical_json_bytes(dict(value))
    if path.exists():
        try:
            if path.read_bytes() != payload:
                _fail("route_rebind_record_conflict", f"immutable record {path.name} differs")
        except OSError as exc:
            raise ClientRouteRebindError("route_rebind_record_read_failed", "immutable rebind record cannot be read") from exc
        return
    _atomic_json(path, value)


def _write_state(
    authority: Path,
    *,
    rebind_id: str,
    plan_sha256: str,
    phase: str,
    mutation_phase: str,
    bootstrap_state_sha256: str,
    target_manifest_sha256: str,
    target_manifest_path: str,
    subject_sid: str,
) -> dict[str, Any]:
    payload = {
        "schema": ROUTE_REBIND_STATE_SCHEMA,
        "rebind_id": rebind_id,
        "plan_sha256": plan_sha256,
        "phase": phase,
        "mutation_phase": mutation_phase,
        "bootstrap_state_sha256": bootstrap_state_sha256,
        "target_manifest_sha256": target_manifest_sha256,
        "target_manifest_path": target_manifest_path,
        "subject_sid": subject_sid,
        "production_activation_performed": False,
    }
    state = {**payload, "state_sha256": _digest(payload)}
    _atomic_json(_state_path(authority, rebind_id), state)
    return state


def _load_json_object(path: Path, field: str) -> dict[str, Any]:
    try:
        value = _load_json(path, field=field)
    except Exception as exc:
        raise ClientRouteRebindError("route_rebind_record_invalid", f"{field} cannot be read") from exc
    if not isinstance(value, Mapping):
        _fail("route_rebind_record_invalid", f"{field} must contain an object")
    return {str(key): item for key, item in value.items()}


def _load_plan(authority: Path, rebind_id: str, expected_plan_sha256: str | None = None) -> dict[str, Any]:
    plan = _load_json_object(_plan_path(authority, rebind_id), "route_rebind_plan")
    required = {
        "schema", "rebind_id", "authority_boundary", "recovery_mode", "production_activation_performed",
        "candidate_may_write_active_pointer", "bootstrap_state_sha256", "active_manifest_sha256",
        "previous_manifest_sha256", "active_source_head", "active_source_tree", "activation_id",
        "original_maintenance_id", "original_maintenance_plan_sha256", "physical_route_before",
        "legacy_route_present", "target_client_runtime_root", "target_client_plan_sha256",
        "target_native_manifest_path", "target_native_manifest_sha256", "target_native_executable_sha256",
        "browser_bundle_digest", "target_source_head", "target_source_tree", "m9b_record_digest",
        "m9b_source_head", "m3c_control_digest", "m3c_kill_switch_digest", "subject_sid",
        "route_rebind_plan_sha256",
    }
    if set(plan) != required or plan.get("schema") != ROUTE_REBIND_PLAN_SCHEMA:
        _fail("route_rebind_plan_invalid", "route rebind plan fields differ")
    if plan.get("rebind_id") != rebind_id or plan.get("authority_boundary") != "external_bootstrap_root":
        _fail("route_rebind_plan_invalid", "route rebind identity differs")
    if plan.get("recovery_mode") != ROUTE_REBIND_RECOVERY_MODE or plan.get("production_activation_performed") is not False:
        _fail("route_rebind_plan_invalid", "route rebind recovery/activation policy differs")
    if plan.get("candidate_may_write_active_pointer") is not False or plan.get("legacy_route_present") is not False:
        _fail("route_rebind_plan_invalid", "route rebind cannot own Bootstrap or Legacy state")
    for field in (
        "bootstrap_state_sha256", "active_manifest_sha256", "previous_manifest_sha256",
        "original_maintenance_plan_sha256", "target_client_plan_sha256", "target_native_manifest_sha256",
        "target_native_executable_sha256", "browser_bundle_digest", "m3c_control_digest",
        "m3c_kill_switch_digest", "route_rebind_plan_sha256",
    ):
        _digest_field(plan.get(field), field)
    for field in ("active_source_head", "active_source_tree", "target_source_head", "target_source_tree"):
        _sha40(plan.get(field), field)
    if plan.get("target_source_head") != plan.get("active_source_head") or plan.get("target_source_tree") != plan.get("active_source_tree"):
        _fail("route_rebind_plan_invalid", "target client source is not the exact ACTIVE source")
    if not isinstance(plan.get("physical_route_before"), Mapping):
        _fail("route_rebind_plan_invalid", "physical route BEFORE witness is missing")
    payload = dict(plan)
    payload.pop("route_rebind_plan_sha256")
    if _digest(payload) != plan["route_rebind_plan_sha256"]:
        _fail("route_rebind_plan_digest_mismatch", "route rebind plan digest differs")
    if expected_plan_sha256 is not None and plan["route_rebind_plan_sha256"] != _digest_field(expected_plan_sha256, "expected_plan_sha256"):
        _fail("route_rebind_plan_stale", "operator subject is bound to another route rebind plan")
    return plan


def _load_state(authority: Path, rebind_id: str, plan_sha256: str) -> dict[str, Any]:
    state = _load_json_object(_state_path(authority, rebind_id), "route_rebind_state")
    required = {
        "schema", "rebind_id", "plan_sha256", "phase", "mutation_phase", "bootstrap_state_sha256",
        "target_manifest_sha256", "target_manifest_path", "subject_sid", "production_activation_performed",
        "state_sha256",
    }
    if set(state) != required or state.get("schema") != ROUTE_REBIND_STATE_SCHEMA or state.get("rebind_id") != rebind_id:
        _fail("route_rebind_state_invalid", "route rebind state fields differ")
    if state.get("plan_sha256") != plan_sha256 or state.get("production_activation_performed") is not False:
        _fail("route_rebind_state_stale", "route rebind state binds another plan")
    if state.get("phase") not in {"PREPARED", "COMPLETED"} or state.get("mutation_phase") not in {"BEFORE", "HKCU32", "HKCU64", "READBACK", "COMPLETED"}:
        _fail("route_rebind_state_invalid", "route rebind phase is unsupported")
    supplied = _digest_field(state.get("state_sha256"), "state_sha256")
    payload = dict(state)
    payload.pop("state_sha256")
    if _digest(payload) != supplied:
        _fail("route_rebind_state_digest_mismatch", "route rebind state digest differs")
    return state


def _m3c_state(runtime: Path) -> dict[str, str]:
    try:
        control = _load_json(runtime / "control" / "m3c-control.json", field="m3c_control")
        kill = _load_json(runtime / "control" / "m3c-kill-switch.json", field="m3c_kill_switch")
    except Exception as exc:
        raise ClientRouteRebindError("m3c_state_unavailable", "canonical M3c state cannot be observed") from exc
    if (
        control.get("schema") != "bdb-vnext-m3c-control-v2"
        or control.get("alternate_admission") is not False
        or control.get("legacy_import") is not False
        or kill.get("schema") != "bdb-vnext-m3c-kill-switch-v1"
        or kill.get("admission_enabled") is not True
    ):
        _fail("m3c_not_canonical", "M3c is not in canonical-only mode")
    return {"control_digest": _digest(control), "kill_switch_digest": _digest(kill)}


def _route_phase(observation: Mapping[str, Any], target_manifest_path: str) -> str:
    target = observation.get("target")
    legacy = observation.get("legacy")
    if not isinstance(target, list) or not isinstance(legacy, list):
        return "FOREIGN"
    if legacy:
        return "LEGACY"
    if not target:
        return "ABSENT"
    seen: dict[str, str] = {}
    for item in target:
        if not isinstance(item, Mapping) or item.get("root") != "HKCU" or item.get("view") not in _ROUTE_VIEWS:
            return "FOREIGN"
        view = str(item["view"])
        if view in seen or not isinstance(item.get("value"), str) or not Path(item["value"]).is_absolute():
            return "FOREIGN"
        seen[view] = str(Path(item["value"]).absolute())
    target_path = str(Path(target_manifest_path).absolute())
    if any(value.casefold() != target_path.casefold() for value in seen.values()):
        return "FOREIGN"
    return "TARGET" if set(seen) == set(_ROUTE_VIEWS) else "PARTIAL"


def _route_snapshot(observation: Mapping[str, Any]) -> dict[str, Any]:
    target = observation.get("target")
    legacy = observation.get("legacy")
    if not isinstance(target, list) or not isinstance(legacy, list):
        _fail("route_rebind_observation_invalid", "physical route observation is incomplete")
    return {"target": [dict(item) for item in target], "legacy": [dict(item) for item in legacy]}


def _active_subject(
    *,
    authority: Path,
    deployed_runtime: Path,
    target_client_runtime: Path,
    rebind_id: str,
    operator_sid: str | None,
    require_absent: bool = True,
) -> dict[str, Any]:
    bootstrap = observe_bootstrap_activation(authority_root=authority)
    if bootstrap.get("status") != "ACTIVE" or bootstrap.get("production_activation_performed") is not True:
        _fail("bootstrap_not_active", "client route rebind requires Bootstrap ACTIVE")
    state = bootstrap.get("state")
    slots = bootstrap.get("slots")
    if not isinstance(state, Mapping) or not isinstance(slots, Mapping):
        _fail("bootstrap_subject_invalid", "Bootstrap ACTIVE observation is incomplete")
    active = slots.get("ACTIVE")
    previous = slots.get("PREVIOUS")
    if not isinstance(active, Mapping) or not isinstance(previous, Mapping):
        _fail("bootstrap_subject_invalid", "ACTIVE/PREVIOUS recovery subjects are required")
    activation_id = state.get("activation_id")
    cutover_plan_sha = state.get("cutover_plan_sha256")
    if not isinstance(activation_id, str) or not activation_id.startswith("m11c-maint-"):
        _fail("route_rebind_lineage_unsupported", "route rebind requires post-active maintenance lineage")
    original_maintenance_id = activation_id[len("m11c-maint-"):]
    original = query_post_active_maintenance(authority_root=authority, maintenance_id=original_maintenance_id)
    original_plan = original.get("plan")
    if not isinstance(original_plan, Mapping) or original_plan.get("plan_sha256") != cutover_plan_sha:
        _fail("route_rebind_lineage_mismatch", "Bootstrap cutover does not bind the original maintenance plan")
    active_head = _sha40(active.get("source_commit"), "active_source_head")
    active_tree = _sha40(original_plan.get("candidate_source_tree"), "active_source_tree")
    if original_plan.get("candidate_source_head") != active_head:
        _fail("route_rebind_source_mismatch", "original maintenance source differs from Bootstrap ACTIVE")
    try:
        client_query = query_client_plan(runtime_root=target_client_runtime)
        client = client_query["plan"]
        require_client_verification(runtime_root=target_client_runtime, expected_client_plan_sha256=client["client_plan_sha256"])
        routes = observe_windows_native_routes(runtime_root=target_client_runtime)
    except (M11cClientError, KeyError) as exc:
        raise ClientRouteRebindError(getattr(exc, "code", "client_subject_unavailable"), str(exc)) from exc
    if client.get("source_head") != active_head or client.get("source_tree") != active_tree:
        _fail("route_rebind_source_mismatch", "stable client source differs from Bootstrap ACTIVE")
    route_phase = _route_phase(routes, str(client["native_manifest_path"]))
    if require_absent and route_phase != "ABSENT":
        _fail("route_rebind_before_not_absent", "route rebind requires an exact absent physical target route")
    m9b = read_activation(deployed_runtime)
    if m9b is None:
        _fail("m9b_record_unavailable", "current M9b record is required for route lineage")
    m3c = _m3c_state(deployed_runtime)
    sid = operator_sid or _current_sid()
    if not isinstance(sid, str) or not sid:
        _fail("subject_identity_invalid", "route rebind subject SID is missing")
    return {
        "bootstrap": {"state": dict(state), "active": dict(active), "previous": dict(previous)},
        "original_plan": dict(original_plan),
        "client": dict(client),
        "physical_route_before": _route_snapshot(routes),
        "physical_route_phase": route_phase,
        "m9b": m9b.as_dict(),
        "m3c": m3c,
        "subject_sid": sid,
        "activation_id": activation_id,
        "original_maintenance_id": original_maintenance_id,
        "target_client_runtime_root": str(target_client_runtime),
        "rebind_id": rebind_id,
    }


def prepare_client_route_rebind(
    *,
    authority_root: str | Path,
    deployed_runtime_root: str | Path,
    target_client_runtime_root: str | Path,
    rebind_id: str,
    operator_sid: str | None = None,
) -> dict[str, Any]:
    """Prepare immutable exact stable-route rebind evidence without registry writes."""

    authority = _absolute(authority_root, "authority_root")
    deployed = _absolute(deployed_runtime_root, "deployed_runtime_root")
    client_root = _absolute(target_client_runtime_root, "target_client_runtime_root")
    rebind_id = _id(rebind_id, "rebind_id")
    with BootstrapLock(authority / "bootstrap.lock"):
        subject = _active_subject(
            authority=authority,
            deployed_runtime=deployed,
            target_client_runtime=client_root,
            rebind_id=rebind_id,
            operator_sid=operator_sid,
        )
        state = subject["bootstrap"]["state"]
        client = subject["client"]
        m9b = subject["m9b"]
        payload = {
            "schema": ROUTE_REBIND_PLAN_SCHEMA,
            "rebind_id": rebind_id,
            "authority_boundary": "external_bootstrap_root",
            "recovery_mode": ROUTE_REBIND_RECOVERY_MODE,
            "production_activation_performed": False,
            "candidate_may_write_active_pointer": False,
            "bootstrap_state_sha256": state["state_sha256"],
            "active_manifest_sha256": subject["bootstrap"]["active"]["manifest_sha256"],
            "previous_manifest_sha256": subject["bootstrap"]["previous"]["manifest_sha256"],
            "active_source_head": subject["bootstrap"]["active"]["source_commit"],
            "active_source_tree": subject["original_plan"]["candidate_source_tree"],
            "activation_id": subject["activation_id"],
            "original_maintenance_id": subject["original_maintenance_id"],
            "original_maintenance_plan_sha256": subject["original_plan"]["plan_sha256"],
            "physical_route_before": subject["physical_route_before"],
            "legacy_route_present": False,
            "target_client_runtime_root": str(client_root),
            "target_client_plan_sha256": client["client_plan_sha256"],
            "target_native_manifest_path": client["native_manifest_path"],
            "target_native_manifest_sha256": client["native_manifest_sha256"],
            "target_native_executable_sha256": client["native_host_executable_sha256"],
            "browser_bundle_digest": client["browser_bundle_digest"],
            "target_source_head": client["source_head"],
            "target_source_tree": client["source_tree"],
            "m9b_record_digest": m9b["record_digest"],
            "m9b_source_head": m9b["source_head"],
            "m3c_control_digest": subject["m3c"]["control_digest"],
            "m3c_kill_switch_digest": subject["m3c"]["kill_switch_digest"],
            "subject_sid": subject["subject_sid"],
        }
        plan = {**payload, "route_rebind_plan_sha256": _digest(payload)}
        _write_immutable(_plan_path(authority, rebind_id), plan)
        state_doc = _write_state(
            authority,
            rebind_id=rebind_id,
            plan_sha256=plan["route_rebind_plan_sha256"],
            phase="PREPARED",
            mutation_phase="BEFORE",
            bootstrap_state_sha256=plan["bootstrap_state_sha256"],
            target_manifest_sha256=plan["target_native_manifest_sha256"],
            target_manifest_path=plan["target_native_manifest_path"],
            subject_sid=plan["subject_sid"],
        )
    return query_client_route_rebind(authority_root=authority, rebind_id=rebind_id, expected_plan_sha256=plan["route_rebind_plan_sha256"])


def query_client_route_rebind(*, authority_root: str | Path, rebind_id: str, expected_plan_sha256: str | None = None) -> dict[str, Any]:
    authority = _absolute(authority_root, "authority_root")
    rebind_id = _id(rebind_id, "rebind_id")
    plan = _load_plan(authority, rebind_id, expected_plan_sha256)
    state = _load_state(authority, rebind_id, plan["route_rebind_plan_sha256"])
    return {
        "schema": ROUTE_REBIND_RESULT_SCHEMA,
        "rebind_id": rebind_id,
        "plan": plan,
        "state": state,
        "production_mutation_performed": False,
    }


def verify_client_route_rebind_current(
    *,
    authority_root: str | Path,
    deployed_runtime_root: str | Path,
    rebind_id: str,
    expected_plan_sha256: str,
) -> dict[str, Any]:
    """Verify one explicitly supplied route-repair subject against current authorities."""

    authority = _absolute(authority_root, "authority_root")
    deployed = _absolute(deployed_runtime_root, "deployed_runtime_root")
    rebind_id = _id(rebind_id, "rebind_id")
    expected = _digest_field(expected_plan_sha256, "expected_plan_sha256")
    plan = _load_plan(authority, rebind_id, expected)
    state = _load_state(authority, rebind_id, expected)
    if state["phase"] != "COMPLETED":
        _fail("route_rebind_incomplete", "current route-repair subject is not COMPLETED")
    routes, phase = _revalidate(
        authority=authority,
        deployed=deployed,
        plan=plan,
        state=state,
        allow_partial=False,
    )
    if phase != "TARGET":
        _fail("route_rebind_readback_mismatch", "current route-repair subject did not read back as TARGET")
    return {
        "schema": ROUTE_REBIND_RESULT_SCHEMA,
        "rebind_id": rebind_id,
        "plan": plan,
        "state": state,
        "routes": routes,
        "production_mutation_performed": False,
    }


def _revalidate(
    *,
    authority: Path,
    deployed: Path,
    plan: Mapping[str, Any],
    state: Mapping[str, Any],
    allow_partial: bool,
) -> tuple[dict[str, Any], str]:
    if _current_sid() != plan["subject_sid"]:
        _fail("route_rebind_subject_mismatch", "current Windows SID differs from the immutable subject")
    subject = _active_subject(
        authority=authority,
        deployed_runtime=deployed,
        target_client_runtime=Path(plan["target_client_runtime_root"]),
        rebind_id=plan["rebind_id"],
        operator_sid=plan["subject_sid"],
        require_absent=False,
    )
    bootstrap = subject["bootstrap"]
    if bootstrap["state"].get("state_sha256") != plan["bootstrap_state_sha256"]:
        _fail("route_rebind_bootstrap_changed", "Bootstrap ACTIVE state changed after route rebind preparation")
    manifest_bindings = {
        "active_manifest_sha256": bootstrap["active"]["manifest_sha256"],
        "previous_manifest_sha256": bootstrap["previous"]["manifest_sha256"],
    }
    for field, actual in manifest_bindings.items():
        if actual != plan[field]:
            _fail("route_rebind_bootstrap_changed", f"Bootstrap {field} changed")
    if subject["original_plan"]["plan_sha256"] != plan["original_maintenance_plan_sha256"]:
        _fail("route_rebind_lineage_mismatch", "original maintenance plan changed")
    client = subject["client"]
    bindings = {
        "target_client_plan_sha256": client["client_plan_sha256"],
        "target_native_manifest_path": client["native_manifest_path"],
        "target_native_manifest_sha256": client["native_manifest_sha256"],
        "target_native_executable_sha256": client["native_host_executable_sha256"],
        "browser_bundle_digest": client["browser_bundle_digest"],
        "target_source_head": client["source_head"],
        "target_source_tree": client["source_tree"],
    }
    for field, actual in bindings.items():
        if actual != plan[field]:
            _fail("route_rebind_target_changed", f"stable client {field} changed")
    m9b = subject["m9b"]
    if m9b["record_digest"] != plan["m9b_record_digest"] or m9b["source_head"] != plan["m9b_source_head"]:
        _fail("route_rebind_m9b_changed", "current M9b record changed before route rebind completion")
    if subject["m3c"]["control_digest"] != plan["m3c_control_digest"] or subject["m3c"]["kill_switch_digest"] != plan["m3c_kill_switch_digest"]:
        _fail("route_rebind_m3c_changed", "M3c canonical state changed during route rebind")
    routes = observe_windows_native_routes(runtime_root=Path(plan["target_client_runtime_root"]))
    phase = _route_phase(routes, plan["target_native_manifest_path"])
    if phase in {"FOREIGN", "LEGACY"}:
        _fail("route_rebind_route_changed", "physical route is foreign or Legacy-bearing")
    if allow_partial and phase not in {"ABSENT", "PARTIAL", "TARGET"}:
        _fail("route_rebind_route_changed", "physical route is outside the planned recovery states")
    if not allow_partial and phase != "TARGET":
        _fail("route_rebind_route_changed", "physical route is foreign, Legacy-bearing, or no longer the planned BEFORE state")
    return routes, phase


def rebind_client_route(
    *,
    authority_root: str | Path,
    deployed_runtime_root: str | Path,
    rebind_id: str,
    expected_plan_sha256: str,
    fault_hook: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Apply/recover one exact stable route rebind under BootstrapLock."""

    authority = _absolute(authority_root, "authority_root")
    deployed = _absolute(deployed_runtime_root, "deployed_runtime_root")
    rebind_id = _id(rebind_id, "rebind_id")
    expected = _digest_field(expected_plan_sha256, "expected_plan_sha256")
    with BootstrapLock(authority / "bootstrap.lock"):
        plan = _load_plan(authority, rebind_id, expected)
        state = _load_state(authority, rebind_id, expected)
        routes, phase = _revalidate(authority=authority, deployed=deployed, plan=plan, state=state, allow_partial=True)
        recovered_completed_drift = False
        if state["phase"] == "COMPLETED":
            if phase == "TARGET":
                result = query_client_route_rebind(authority_root=authority, rebind_id=rebind_id, expected_plan_sha256=expected)
                result["replayed"] = True
                result["route_phase"] = phase
                return result
            if phase not in {"ABSENT", "PARTIAL"}:
                _fail("route_rebind_completed_mismatch", "completed rebind no longer has an exact recoverable target route")
            # The immutable plan remains authoritative; only the mutable journal
            # is reopened so a lost HKCU route can roll forward under the same
            # Bootstrap-bound subject. The old route is never restored.
            state = _write_state(
                authority,
                rebind_id=rebind_id,
                plan_sha256=expected,
                phase="PREPARED",
                mutation_phase="RECOVERY",
                bootstrap_state_sha256=plan["bootstrap_state_sha256"],
                target_manifest_sha256=plan["target_native_manifest_sha256"],
                target_manifest_path=plan["target_native_manifest_path"],
                subject_sid=plan["subject_sid"],
            )
            recovered_completed_drift = True
        if fault_hook:
            fault_hook("before_first_registry_mutation")
        target = plan["target_native_manifest_path"]
        try:
            if phase == "ABSENT":
                set_windows_target_native_route_view(runtime_root=Path(plan["target_client_runtime_root"]), view="32", manifest_path=target)
                phase = "PARTIAL"
                _write_state(authority, rebind_id=rebind_id, plan_sha256=expected, phase="PREPARED", mutation_phase="HKCU32", bootstrap_state_sha256=plan["bootstrap_state_sha256"], target_manifest_sha256=plan["target_native_manifest_sha256"], target_manifest_path=target, subject_sid=plan["subject_sid"])
                if fault_hook:
                    fault_hook("after_hkcu32")
            routes, phase = _revalidate(authority=authority, deployed=deployed, plan=plan, state=state, allow_partial=True)
            if phase == "PARTIAL":
                if fault_hook:
                    fault_hook("before_hkcu64")
                set_windows_target_native_route_view(runtime_root=Path(plan["target_client_runtime_root"]), view="64", manifest_path=target)
                _write_state(authority, rebind_id=rebind_id, plan_sha256=expected, phase="PREPARED", mutation_phase="HKCU64", bootstrap_state_sha256=plan["bootstrap_state_sha256"], target_manifest_sha256=plan["target_native_manifest_sha256"], target_manifest_path=target, subject_sid=plan["subject_sid"])
                if fault_hook:
                    fault_hook("after_hkcu64")
            if fault_hook:
                fault_hook("before_exact_readback")
            _routes, phase = _revalidate(authority=authority, deployed=deployed, plan=plan, state=state, allow_partial=False)
            if phase != "TARGET":
                _fail("route_rebind_readback_mismatch", "stable target route did not read back exactly")
            _write_state(authority, rebind_id=rebind_id, plan_sha256=expected, phase="PREPARED", mutation_phase="READBACK", bootstrap_state_sha256=plan["bootstrap_state_sha256"], target_manifest_sha256=plan["target_native_manifest_sha256"], target_manifest_path=target, subject_sid=plan["subject_sid"])
            if fault_hook:
                fault_hook("after_exact_readback_before_journal_completion")
            completed = _write_state(authority, rebind_id=rebind_id, plan_sha256=expected, phase="COMPLETED", mutation_phase="COMPLETED", bootstrap_state_sha256=plan["bootstrap_state_sha256"], target_manifest_sha256=plan["target_native_manifest_sha256"], target_manifest_path=target, subject_sid=plan["subject_sid"])
            result = query_client_route_rebind(authority_root=authority, rebind_id=rebind_id, expected_plan_sha256=expected)
            result["state"] = completed
            result["replayed"] = False
            result["recovered_completed_drift"] = recovered_completed_drift
            result["route_phase"] = "TARGET"
            return result
        except M11cClientError as exc:
            raise ClientRouteRebindError(exc.code, str(exc)) from exc


__all__ = [
    "ClientRouteRebindError",
    "ClientRouteRebindFault",
    "ROUTE_REBIND_PLAN_SCHEMA",
    "ROUTE_REBIND_RESULT_SCHEMA",
    "ROUTE_REBIND_STATE_SCHEMA",
    "ROUTE_REBIND_RECOVERY_MODE",
    "prepare_client_route_rebind",
    "query_client_route_rebind",
    "verify_client_route_rebind_current",
    "rebind_client_route",
]

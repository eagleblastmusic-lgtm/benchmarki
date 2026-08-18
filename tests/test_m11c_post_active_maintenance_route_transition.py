from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import bdb_vnext.m9b_native_host as native
import bdb_vnext.m11c_post_active_maintenance as maintenance
from test_m11c_post_active_maintenance import HEAD, SHA, TREE, _active, _patch_common, _prepare
from bdb_vnext.m11c_active_reader import M11cActiveReadError
from bdb_vnext.m11c_route_transition import (
    RouteTransitionError,
    RouteTransitionFault,
    classify_route,
    restore_old_route,
    transition_to_candidate,
)


def _observation(values: dict[str, str], *, legacy: list[dict[str, str]] | None = None) -> dict[str, object]:
    return {
        "target": [{"root": "HKCU", "view": view, "value": values[view]} for view in ("32", "64")],
        "legacy": list(legacy or []),
    }


@pytest.mark.parametrize("stage", ["before_first_registry_change", "after_hkcu_32", "after_hkcu_64", "after_route_readback"])
def test_route_fault_before_bootstrap_can_restore_exact_old_views(tmp_path: Path, stage: str) -> None:
    old = {"32": str((tmp_path / "old-32.json").resolve()), "64": str((tmp_path / "old-64.json").resolve())}
    candidate = str((tmp_path / "candidate.json").resolve())
    values = dict(old)

    def observe() -> dict[str, object]:
        return _observation(values)

    def write(view: str, path: str) -> None:
        values[view] = path

    def fault(name: str) -> None:
        if name == stage:
            raise RouteTransitionFault(name)

    with pytest.raises(RouteTransitionFault):
        transition_to_candidate(old_routes=[{"root": "HKCU", "view": k, "value": old[k]} for k in ("32", "64")], candidate_manifest_path=candidate, observe=observe, write_view=write, fault_hook=fault)
    assert classify_route(observe(), old_routes=[{"root": "HKCU", "view": k, "value": old[k]} for k in ("32", "64")], candidate_manifest_path=candidate) in {"OLD", "PARTIAL", "CANDIDATE"}
    restore_old_route(old_routes=[{"root": "HKCU", "view": k, "value": old[k]} for k in ("32", "64")], candidate_manifest_path=candidate, observe=observe, write_view=write)
    assert {k: os.path.normcase(v) for k, v in values.items()} == {k: os.path.normcase(v) for k, v in old.items()}


def test_legacy_or_foreign_route_is_fail_closed(tmp_path: Path) -> None:
    old = [{"root": "HKCU", "view": "32", "value": str((tmp_path / "old32.json").resolve())}, {"root": "HKCU", "view": "64", "value": str((tmp_path / "old64.json").resolve())}]
    observation = {"target": old, "legacy": [{"root": "HKCU", "view": "32", "value": str((tmp_path / "legacy.json").resolve())}]}
    with pytest.raises(RouteTransitionError) as caught:
        restore_old_route(old_routes=old, candidate_manifest_path=str((tmp_path / "candidate.json").resolve()), observe=lambda: observation, write_view=lambda *_: None)
    assert caught.value.code == "route_recovery_foreign"

def test_route_transition_replay_rejects_candidate_as_new_old_subject(tmp_path: Path) -> None:
    old = {"32": str((tmp_path / "old-32.json").resolve()), "64": str((tmp_path / "old-64.json").resolve())}
    candidate = str((tmp_path / "candidate.json").resolve())
    values = {"32": candidate, "64": candidate}
    routes = [{"root": "HKCU", "view": k, "value": old[k]} for k in ("32", "64")]
    with pytest.raises(RouteTransitionError) as caught:
        transition_to_candidate(old_routes=routes, candidate_manifest_path=candidate, observe=lambda: _observation(values), write_view=lambda *_: None)
    assert caught.value.code == "route_plan_stale"



def test_post_publication_recovery_rolls_forward_partial_route_only(tmp_path: Path) -> None:
    old = [{"root": "HKCU", "view": "32", "value": str((tmp_path / "old32.json").resolve())}, {"root": "HKCU", "view": "64", "value": str((tmp_path / "old64.json").resolve())}]
    candidate = str((tmp_path / "candidate.json").resolve())
    values = {"32": candidate, "64": old[1]["value"]}
    def observe() -> dict[str, object]:
        return _observation(values)
    def write(view: str, path: str) -> None:
        values[view] = path
    from bdb_vnext.m11c_route_transition import roll_forward_to_candidate
    roll_forward_to_candidate(old_routes=old, candidate_manifest_path=candidate, observe=observe, write_view=write)
    assert os.path.normcase(values["32"]) == os.path.normcase(candidate)
    assert os.path.normcase(values["64"]) == os.path.normcase(candidate)

def test_exact_apply_replay_is_idempotent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    current = _active(tmp_path)
    _patch_common(monkeypatch, tmp_path)
    prepared = _prepare(monkeypatch, tmp_path, "route-replay")
    candidate_doc = {
        "schema": "bdb-vnext-bootstrap-slot-manifest-v1", "slot": "ACTIVE", "bundle_root": str(tmp_path / "bundle"),
        "bundle_sha256": SHA, "bundle_role": "candidate", "source_commit": HEAD, "known_good": True,
        "bundle_id": "candidate", "manifest_sha256": SHA,
        "compatibility": {"supported_control_schema": {"min": 1, "max": 1}, "capabilities": ["canonical-admission-v1"]},
    }
    monkeypatch.setattr(maintenance, "_inspect", lambda *_args, **_kwargs: candidate_doc)
    publishes: list[object] = []
    monkeypatch.setattr(maintenance, "_publish", lambda *_args, **_kwargs: publishes.append(True) or SHA)
    monkeypatch.setattr(maintenance, "_replace_state", lambda *_args, **_kwargs: None)
    after = _active(tmp_path)
    after["active"] = {**current["active"], "source_commit": HEAD, "bundle_sha256": SHA}
    observations = iter((current, after))
    monkeypatch.setattr(maintenance, "_active_observation", lambda _authority: next(observations))
    first = maintenance.apply_post_active_maintenance(authority_root=tmp_path / "authority", maintenance_id="route-replay", expected_plan_sha256=prepared["plan"]["plan_sha256"], operator_approved=True)
    assert first["status"] == "ACTIVE"
    monkeypatch.setattr(maintenance, "_active_observation", lambda _authority: after)
    second = maintenance.apply_post_active_maintenance(authority_root=tmp_path / "authority", maintenance_id="route-replay", expected_plan_sha256=prepared["plan"]["plan_sha256"], operator_approved=True)
    assert second["status"] == "ACTIVE"
    assert len(publishes) == 2


@pytest.mark.parametrize("stage", ["before_bootstrap_publication", "after_bootstrap_publication", "before_final_verification"])
def test_apply_faults_preserve_old_before_publish_and_roll_forward_after(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stage: str) -> None:
    current = _active(tmp_path)
    _patch_common(monkeypatch, tmp_path)
    prepared = _prepare(monkeypatch, tmp_path, "route-fault-" + stage.replace("_", "-"))
    candidate_doc = {
        "schema": "bdb-vnext-bootstrap-slot-manifest-v1", "slot": "ACTIVE", "bundle_root": str(tmp_path / "bundle"),
        "bundle_sha256": SHA, "bundle_role": "candidate", "source_commit": HEAD, "known_good": True,
        "bundle_id": "candidate", "manifest_sha256": SHA,
        "compatibility": {"supported_control_schema": {"min": 1, "max": 1}, "capabilities": ["canonical-admission-v1"]},
    }
    monkeypatch.setattr(maintenance, "_inspect", lambda *_args, **_kwargs: candidate_doc)
    monkeypatch.setattr(maintenance, "_publish", lambda *_args, **_kwargs: SHA)
    monkeypatch.setattr(maintenance, "_replace_state", lambda *_args, **_kwargs: None)
    after = _active(tmp_path)
    after["active"] = {**current["active"], "source_commit": HEAD, "bundle_sha256": SHA}
    observations = iter((current, after))
    monkeypatch.setattr(maintenance, "_active_observation", lambda _authority: next(observations))
    def fault(name: str) -> None:
        if name == stage:
            raise maintenance.MaintenanceFault(name)
    with pytest.raises(maintenance.MaintenanceFault):
        maintenance.apply_post_active_maintenance(authority_root=tmp_path / "authority", maintenance_id="route-fault-" + stage.replace("_", "-"), expected_plan_sha256=prepared["plan"]["plan_sha256"], operator_approved=True, fault_hook=fault)
    observed = maintenance._route_observation(tmp_path / "client")
    phase = classify_route(observed, old_routes=prepared["plan"]["old_native_routes"], candidate_manifest_path=prepared["plan"]["candidate_native_manifest_path"])
    assert phase == ("OLD" if stage == "before_bootstrap_publication" else "CANDIDATE")


def test_recovery_fault_is_bounded_before_route_write(tmp_path: Path) -> None:
    old = [{"root": "HKCU", "view": "32", "value": str((tmp_path / "old32.json").resolve())}, {"root": "HKCU", "view": "64", "value": str((tmp_path / "old64.json").resolve())}]
    candidate = str((tmp_path / "candidate.json").resolve())
    values = {"32": candidate, "64": old[1]["value"]}
    def observe() -> dict[str, object]:
        return _observation(values)
    def write(view: str, path: str) -> None:
        values[view] = path
    def fault(name: str) -> None:
        if name == "during_recovery":
            raise RouteTransitionFault(name)
    with pytest.raises(RouteTransitionFault):
        from bdb_vnext.m11c_route_transition import roll_forward_to_candidate
        roll_forward_to_candidate(old_routes=old, candidate_manifest_path=candidate, observe=observe, write_view=write, fault_hook=fault)
    assert os.path.normcase(values["32"]) == os.path.normcase(candidate)
    assert os.path.normcase(values["64"]) == os.path.normcase(old[1]["value"])

def test_admission_route_bootstrap_mismatch_fails_before_canonical_open(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = native.VNextNativeConfig(runtime_root=tmp_path / "runtime", legacy_runtime_root=tmp_path / "legacy", bootstrap_authority_root=tmp_path / "bootstrap")
    opened = False

    monkeypatch.setattr(native, "require_active", lambda *_: SimpleNamespace(source_head="1" * 40))
    monkeypatch.setattr(native, "require_bootstrap_active", lambda *_args, **_kwargs: (_ for _ in ()).throw(M11cActiveReadError("route_bootstrap_mismatch", "route/bootstrap mismatch")))
    def forbidden_open(*_args, **_kwargs):
        nonlocal opened
        opened = True
        raise AssertionError("canonical admission must not open during route mismatch")
    monkeypatch.setattr(native.CanonicalVNextAdmissionAuthority, "open", forbidden_open)
    message = {
        "schema": native.M9B_NATIVE_REQUEST_SCHEMA,
        "request_id": "route-mismatch",
        "action": "admission.lookup",
        "protocol_generation": config.protocol_generation,
        "browser_extension_id": config.browser_extension_id,
        "submission_key": "submission-key",
        "request_digest": "sha256:" + "a" * 64,
    }
    with pytest.raises(native.M9bNativeError) as caught:
        native.handle_message(config, message)
    assert caught.value.code == "route_bootstrap_mismatch"
    assert opened is False
def test_recovery_uses_published_bootstrap_over_stale_old_journal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    old = [{"root": "HKCU", "view": "32", "value": str((tmp_path / "old32.json").resolve())}, {"root": "HKCU", "view": "64", "value": str((tmp_path / "old64.json").resolve())}]
    candidate = str((tmp_path / "candidate.json").resolve())
    values = {"32": candidate, "64": old[1]["value"]}
    def observe() -> dict[str, object]:
        return _observation(values)
    def write(view: str, path: str) -> None:
        values[view] = path
    monkeypatch.setattr(maintenance, "_route_backend", lambda _root: (observe, write))
    recorded: list[dict[str, object]] = []
    monkeypatch.setattr(maintenance, "_write_transition_state", lambda *_args, **kwargs: recorded.append(kwargs) or kwargs)
    plan = {
        "plan_sha256": "sha256:" + "a" * 64,
        "maintenance_id": "published-before-journal",
        "route_transition_plan_sha256": "sha256:" + "b" * 64,
        "old_native_routes": old,
        "candidate_native_manifest_path": candidate,
        "candidate_source_head": "c" * 40,
    }
    current = {
        "state": {"cutover_plan_sha256": plan["plan_sha256"], "state_sha256": "sha256:" + "d" * 64},
        "active": {"source_commit": plan["candidate_source_head"]},
    }
    recovered, route = maintenance._recover_route_if_needed(
        authority=tmp_path, plan=plan, current=current, client_root=tmp_path,
        transition_state={"phase": "BOOTSTRAP_PUBLISHING", "bootstrap_phase": "OLD"}, fault_hook=None,
    )
    assert recovered == current
    assert classify_route(route, old_routes=old, candidate_manifest_path=candidate) == "CANDIDATE"
    assert os.path.normcase(values["32"]) == os.path.normcase(candidate)
    assert os.path.normcase(values["64"]) == os.path.normcase(candidate)
    assert recorded and recorded[-1]["bootstrap_phase"] == "NEW"

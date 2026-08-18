from __future__ import annotations

from pathlib import Path

import pytest

import bdb_vnext.m11c_post_active_maintenance as maintenance


SHA = "sha256:" + "a" * 64
HEAD = "1" * 40
TREE = "2" * 40
OLD = "3" * 40
PREVIOUS = "4" * 40


def _active(tmp_path: Path) -> dict[str, object]:
    return {
        "state": {
            "schema": maintenance.SLOT_STATE_V2_SCHEMA,
            "runtime_id": maintenance.RUNTIME_ID,
            "generation_id": maintenance.GENERATION_ID,
            "activation_authority": maintenance.M11C_ACTIVATION_AUTHORITY,
            "authority_boundary": "external_bootstrap_root",
            "candidate_manifest_sha256": None,
            "candidate_may_write_active_pointer": False,
            "production_activation_performed": True,
            "rollback_mode": maintenance.M11C_ROLLBACK_MODE,
            "legacy_runtime_root": str(tmp_path / "legacy"),
            "required_control_schema": 1,
            "required_capabilities": ["canonical-admission-v1"],
            "state_sha256": SHA,
            "active_manifest_sha256": "sha256:" + "b" * 64,
            "previous_manifest_sha256": "sha256:" + "c" * 64,
        },
        "active": {
            "source_commit": OLD,
            "bundle_root": str(tmp_path / "old"),
            "bundle_sha256": "sha256:" + "d" * 64,
            "bundle_role": "candidate",
            "known_good": True,
        },
        "previous": {
            "source_commit": PREVIOUS,
            "bundle_root": str(tmp_path / "previous"),
            "bundle_sha256": "sha256:" + "e" * 64,
            "bundle_role": "recovery",
            "known_good": True,
        },
    }


def _patch_common(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, object]:
    current = _active(tmp_path)
    authority = tmp_path / "authority"
    for root in (authority, tmp_path / "bundle", tmp_path / "client", tmp_path / "legacy"):
        root.mkdir(exist_ok=True)
    old32 = str((tmp_path / "old32.json").resolve())
    old64 = str((tmp_path / "old64.json").resolve())
    candidate_manifest = str((tmp_path / "client-manifest.json").resolve())
    route_state = {"values": {"32": old32, "64": old64}}
    def route_observation(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "target": [{"root": "HKCU", "view": view, "value": route_state["values"][view]} for view in ("32", "64")],
            "legacy": [],
            "target_conflict": False,
            "target_registered": True,
            "legacy_route_present": False,
        }
    def route_write(*, runtime_root: Path, view: str, manifest_path: str) -> None:
        route_state["values"][view] = manifest_path
    monkeypatch.setattr(maintenance, "_active_observation", lambda _authority: current)
    monkeypatch.setattr(maintenance, "_observe_candidate_bundle", lambda **_: {"health": {"status": "READY"}})
    monkeypatch.setattr(
        maintenance,
        "_client_identity",
        lambda *_args, **_kwargs: {"client_plan_sha256": SHA, "browser_bundle_digest": SHA, "native_manifest_digest": SHA, "native_manifest_path": candidate_manifest},
    )
    monkeypatch.setattr(maintenance, "_route_observation", route_observation)
    monkeypatch.setattr(maintenance, "set_windows_target_native_route_view", route_write)
    monkeypatch.setattr(maintenance, "_verify_routes", lambda *_args: None)
    return current

def _prepare(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, maintenance_id: str = "m12a-test") -> dict[str, object]:
    _patch_common(monkeypatch, tmp_path)
    return maintenance.prepare_post_active_maintenance(
        authority_root=tmp_path / "authority",
        candidate_bundle_root=tmp_path / "bundle",
        candidate_bundle_sha256=SHA,
        candidate_client_runtime_root=tmp_path / "client",
        source_head=HEAD,
        source_tree=TREE,
        native_artifact_manifest_sha256=SHA,
        maintenance_id=maintenance_id,
    )


def test_prepare_is_immutable_and_never_moves_active_pointer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    result = _prepare(monkeypatch, tmp_path)
    assert result["production_activation_performed"] is False
    assert result["plan"]["operator_approval_required"] is True
    assert result["plan"]["candidate_may_write_active_pointer"] is False
    assert not (tmp_path / "authority" / "slot-state.json").exists()
    assert len(list((tmp_path / "authority" / "maintenance" / "candidates").glob("*.json"))) == 1
    assert len(list((tmp_path / "authority" / "maintenance" / "plans").glob("*.json"))) == 1


def test_prepare_replays_same_identity_and_rejects_plan_conflict(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    first = _prepare(monkeypatch, tmp_path, "m12a-replay")
    second = maintenance.prepare_post_active_maintenance(
        authority_root=tmp_path / "authority",
        candidate_bundle_root=tmp_path / "bundle",
        candidate_bundle_sha256=SHA,
        candidate_client_runtime_root=tmp_path / "client",
        source_head=HEAD,
        source_tree=TREE,
        native_artifact_manifest_sha256=SHA,
        maintenance_id="m12a-replay",
    )
    assert first["plan"]["plan_sha256"] == second["plan"]["plan_sha256"]
    with pytest.raises(maintenance.M11cMaintenanceError) as caught:
        maintenance.prepare_post_active_maintenance(
            authority_root=tmp_path / "authority",
            candidate_bundle_root=tmp_path / "bundle",
            candidate_bundle_sha256=SHA,
            candidate_client_runtime_root=tmp_path / "client",
            source_head=HEAD,
            source_tree="9" * 40,
            native_artifact_manifest_sha256=SHA,
            maintenance_id="m12a-replay",
        )
    assert caught.value.code == "immutable_maintenance_conflict"


def test_wrong_plan_digest_is_blocked_before_switch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    prepared = _prepare(monkeypatch, tmp_path, "m12a-stale")
    with pytest.raises(maintenance.M11cMaintenanceError) as caught:
        maintenance.apply_post_active_maintenance(
            authority_root=tmp_path / "authority",
            maintenance_id="m12a-stale",
            expected_plan_sha256="sha256:" + "f" * 64,
            operator_approved=True,
        )
    assert caught.value.code == "maintenance_plan_stale"
    assert prepared["production_activation_performed"] is False


def test_apply_switches_one_v2_pointer_and_retains_previous(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    current = _active(tmp_path)
    _patch_common(monkeypatch, tmp_path)
    prepared = maintenance.prepare_post_active_maintenance(
        authority_root=tmp_path / "authority", candidate_bundle_root=tmp_path / "bundle", candidate_bundle_sha256=SHA,
        candidate_client_runtime_root=tmp_path / "client", source_head=HEAD, source_tree=TREE,
        native_artifact_manifest_sha256=SHA, maintenance_id="m12a-apply",
    )
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
    result = maintenance.apply_post_active_maintenance(
        authority_root=tmp_path / "authority", maintenance_id="m12a-apply", expected_plan_sha256=prepared["plan"]["plan_sha256"], operator_approved=True,
    )
    assert result["status"] == "ACTIVE"
    assert result["production_activation_performed"] is True
    assert result["old_active_source"] == OLD


@pytest.mark.parametrize("stage", [
    "before_candidate_publication", "after_candidate_publication", "before_plan_publication", "after_plan_publication",
])
def test_prepare_fault_matrix_is_non_activating(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stage: str) -> None:
    _patch_common(monkeypatch, tmp_path)
    def fault(value: str) -> None:
        if value == stage:
            raise maintenance.MaintenanceFault(value)
    with pytest.raises(maintenance.MaintenanceFault):
        maintenance.prepare_post_active_maintenance(
            authority_root=tmp_path / "authority", candidate_bundle_root=tmp_path / "bundle", candidate_bundle_sha256=SHA,
            candidate_client_runtime_root=tmp_path / "client", source_head=HEAD, source_tree=TREE,
            native_artifact_manifest_sha256=SHA, maintenance_id=f"m12a-fault-{stage}", fault_hook=fault,
        )
    assert not (tmp_path / "authority" / "slot-state.json").exists()


def test_apply_rejects_non_approved_effect(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    prepared = _prepare(monkeypatch, tmp_path, "m12a-approval")
    with pytest.raises(maintenance.M11cMaintenanceError) as caught:
        maintenance.apply_post_active_maintenance(
            authority_root=tmp_path / "authority", maintenance_id="m12a-approval", expected_plan_sha256=prepared["plan"]["plan_sha256"], operator_approved=False,
        )
    assert caught.value.code == "operator_approval_required"

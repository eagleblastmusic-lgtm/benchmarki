from __future__ import annotations

from pathlib import Path

import pytest

import bdb_vnext.m11c_client_route_rebind as rebind


HEAD = "a" * 40
TREE = "b" * 40
BOOT = "sha256:" + "1" * 64
ACTIVE = "sha256:" + "2" * 64
PREVIOUS = "sha256:" + "3" * 64
MAINTENANCE = "sha256:" + "4" * 64
CLIENT = "sha256:" + "5" * 64
MANIFEST = "sha256:" + "6" * 64
EXECUTABLE = "sha256:" + "7" * 64
BROWSER = "sha256:" + "8" * 64
M3C_CONTROL = "sha256:" + "9" * 64
M3C_KILL = "sha256:" + "a" * 64
M9B = "sha256:" + "b" * 64


def _subject(client_root: Path, manifest: Path, route: dict[str, object]) -> dict[str, object]:
    return {
        "bootstrap": {
            "state": {"state_sha256": BOOT, "activation_id": "m11c-maint-maintenance"},
            "active": {"manifest_sha256": ACTIVE, "source_commit": HEAD},
            "previous": {"manifest_sha256": PREVIOUS},
        },
        "original_plan": {
            "plan_sha256": MAINTENANCE,
            "candidate_source_head": HEAD,
            "candidate_source_tree": TREE,
        },
        "client": {
            "client_plan_sha256": CLIENT,
            "native_manifest_path": str(manifest),
            "native_manifest_sha256": MANIFEST,
            "native_host_executable_sha256": EXECUTABLE,
            "browser_bundle_digest": BROWSER,
            "source_head": HEAD,
            "source_tree": TREE,
        },
        "physical_route_before": route,
        "physical_route_phase": rebind._route_phase(route, str(manifest)),
        "m9b": {"record_digest": M9B, "source_head": HEAD},
        "m3c": {"control_digest": M3C_CONTROL, "kill_switch_digest": M3C_KILL},
        "subject_sid": "test-sid",
        "activation_id": "m11c-maint-maintenance",
        "original_maintenance_id": "maintenance",
        "target_client_runtime_root": str(client_root),
        "rebind_id": "stable-client-route",
    }


def _fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path, Path, dict[str, list[dict[str, object]]], str]:
    authority = tmp_path / "authority"
    deployed = tmp_path / "deployed"
    client = tmp_path / "client"
    authority.mkdir()
    deployed.mkdir()
    client.mkdir()
    manifest = client / "native-host.json"
    manifest.write_text("{}", encoding="utf-8")
    route: dict[str, list[dict[str, object]]] = {"target": [], "legacy": []}
    rebind_id = "stable-client-route"

    def observe(**_: object) -> dict[str, object]:
        return {"target": [dict(item) for item in route["target"]], "legacy": [dict(item) for item in route["legacy"]]}

    def write_route(*, runtime_root: Path, view: str, manifest_path: str) -> None:
        del runtime_root
        route["target"] = [item for item in route["target"] if item.get("view") != view]
        route["target"].append({"root": "HKCU", "view": view, "value": manifest_path})

    def active_subject(**_: object) -> dict[str, object]:
        snapshot = {"target": [dict(item) for item in route["target"]], "legacy": [dict(item) for item in route["legacy"]]}
        return _subject(client, manifest, snapshot)

    monkeypatch.setattr(rebind, "_active_subject", active_subject)
    monkeypatch.setattr(rebind, "_current_sid", lambda: "test-sid")
    monkeypatch.setattr(rebind, "observe_windows_native_routes", observe)
    monkeypatch.setattr(rebind, "set_windows_target_native_route_view", write_route)
    monkeypatch.setattr(rebind, "_m3c_state", lambda _runtime: {"control_digest": M3C_CONTROL, "kill_switch_digest": M3C_KILL})
    return authority, deployed, client, route, rebind_id


def _prepare(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[dict[str, object], Path, Path, Path, dict[str, list[dict[str, object]]], str]:
    authority, deployed, client, route, rebind_id = _fixture(monkeypatch, tmp_path)
    prepared = rebind.prepare_client_route_rebind(
        authority_root=authority,
        deployed_runtime_root=deployed,
        target_client_runtime_root=client,
        rebind_id=rebind_id,
        operator_sid="test-sid",
    )
    return prepared, authority, deployed, client, route, rebind_id


def test_prepare_binds_exact_subject_and_rebind_is_idempotent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    prepared, authority, deployed, _client, route, rebind_id = _prepare(monkeypatch, tmp_path)
    plan_sha = prepared["plan"]["route_rebind_plan_sha256"]
    assert prepared["plan"]["candidate_may_write_active_pointer"] is False
    assert prepared["plan"]["physical_route_before"] == {"target": [], "legacy": []}

    first = rebind.rebind_client_route(
        authority_root=authority,
        deployed_runtime_root=deployed,
        rebind_id=rebind_id,
        expected_plan_sha256=plan_sha,
    )
    second = rebind.rebind_client_route(
        authority_root=authority,
        deployed_runtime_root=deployed,
        rebind_id=rebind_id,
        expected_plan_sha256=plan_sha,
    )
    assert first["state"]["phase"] == "COMPLETED"
    assert second["replayed"] is True
    assert {item["view"] for item in route["target"]} == {"32", "64"}
    assert route["legacy"] == []


def test_completed_route_drift_recovers_forward_under_same_plan(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    prepared, authority, deployed, _client, route, rebind_id = _prepare(monkeypatch, tmp_path)
    plan_sha = prepared["plan"]["route_rebind_plan_sha256"]
    first = rebind.rebind_client_route(
        authority_root=authority,
        deployed_runtime_root=deployed,
        rebind_id=rebind_id,
        expected_plan_sha256=plan_sha,
    )
    assert first["state"]["phase"] == "COMPLETED"
    route["target"] = []
    recovered = rebind.rebind_client_route(
        authority_root=authority,
        deployed_runtime_root=deployed,
        rebind_id=rebind_id,
        expected_plan_sha256=plan_sha,
    )
    assert recovered["state"]["phase"] == "COMPLETED"
    assert recovered["recovered_completed_drift"] is True
    assert {item["view"] for item in route["target"]} == {"32", "64"}


def test_partial_route_fault_replays_forward_without_old_route(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    prepared, authority, deployed, _client, route, rebind_id = _prepare(monkeypatch, tmp_path)
    plan_sha = prepared["plan"]["route_rebind_plan_sha256"]

    def fault(stage: str) -> None:
        if stage == "after_hkcu32":
            raise rebind.ClientRouteRebindFault(stage)

    with pytest.raises(rebind.ClientRouteRebindFault):
        rebind.rebind_client_route(
            authority_root=authority,
            deployed_runtime_root=deployed,
            rebind_id=rebind_id,
            expected_plan_sha256=plan_sha,
            fault_hook=fault,
        )
    assert {item["view"] for item in route["target"]} == {"32"}
    replay = rebind.rebind_client_route(
        authority_root=authority,
        deployed_runtime_root=deployed,
        rebind_id=rebind_id,
        expected_plan_sha256=plan_sha,
    )
    assert replay["state"]["phase"] == "COMPLETED"
    assert {item["view"] for item in route["target"]} == {"32", "64"}
    assert all(".codex" not in str(item["value"]) for item in route["target"])


@pytest.mark.parametrize(
    "fault_stage",
    ["before_hkcu64", "after_hkcu64", "before_exact_readback", "after_exact_readback_before_journal_completion"],
)
def test_all_late_fault_boundaries_replay_forward(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fault_stage: str,
) -> None:
    prepared, authority, deployed, _client, route, rebind_id = _prepare(monkeypatch, tmp_path)
    plan_sha = prepared["plan"]["route_rebind_plan_sha256"]
    raised = False

    def fault(stage: str) -> None:
        nonlocal raised
        if stage == fault_stage and not raised:
            raised = True
            raise rebind.ClientRouteRebindFault(stage)

    with pytest.raises(rebind.ClientRouteRebindFault):
        rebind.rebind_client_route(
            authority_root=authority,
            deployed_runtime_root=deployed,
            rebind_id=rebind_id,
            expected_plan_sha256=plan_sha,
            fault_hook=fault,
        )
    replay = rebind.rebind_client_route(
        authority_root=authority,
        deployed_runtime_root=deployed,
        rebind_id=rebind_id,
        expected_plan_sha256=plan_sha,
    )
    assert replay["state"]["phase"] == "COMPLETED"
    assert {item["view"] for item in route["target"]} == {"32", "64"}


def test_foreign_or_legacy_route_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    prepared, authority, deployed, _client, route, rebind_id = _prepare(monkeypatch, tmp_path)
    plan_sha = prepared["plan"]["route_rebind_plan_sha256"]
    route["legacy"].append({"root": "HKCU", "view": "32", "value": "C:/legacy.json"})
    with pytest.raises(rebind.ClientRouteRebindError) as caught:
        rebind.rebind_client_route(
            authority_root=authority,
            deployed_runtime_root=deployed,
            rebind_id=rebind_id,
            expected_plan_sha256=plan_sha,
        )
    assert caught.value.code == "route_rebind_route_changed"


def test_bootstrap_or_target_drift_and_stale_plan_fail_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    prepared, authority, deployed, _client, _route, rebind_id = _prepare(monkeypatch, tmp_path)
    plan_sha = prepared["plan"]["route_rebind_plan_sha256"]
    with pytest.raises(rebind.ClientRouteRebindError) as caught:
        rebind.rebind_client_route(
            authority_root=authority,
            deployed_runtime_root=deployed,
            rebind_id=rebind_id,
            expected_plan_sha256="sha256:" + "f" * 64,
        )
    assert caught.value.code == "route_rebind_plan_stale"

    original_active_subject = rebind._active_subject

    def drifted(**kwargs: object) -> dict[str, object]:
        value = original_active_subject(**kwargs)
        value["bootstrap"] = dict(value["bootstrap"])
        value["bootstrap"]["state"] = {"state_sha256": "sha256:" + "e" * 64, "activation_id": "m11c-maint-maintenance"}
        return value

    monkeypatch.setattr(rebind, "_active_subject", drifted)
    with pytest.raises(rebind.ClientRouteRebindError) as caught:
        rebind.rebind_client_route(
            authority_root=authority,
            deployed_runtime_root=deployed,
            rebind_id=rebind_id,
            expected_plan_sha256=plan_sha,
        )
    assert caught.value.code == "route_rebind_bootstrap_changed"

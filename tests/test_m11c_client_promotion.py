from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import bdb_vnext.m11c_client_promotion as promotion
from bdb_vnext.m11c_windows_clients import M11cClientError, query_client_plan, stage_client_plan


ROOT = Path(__file__).resolve().parents[1]
BROWSER_SOURCE = ROOT / "browser_extension_vnext"


def _browser_variant(tmp_path: Path) -> Path:
    variant = tmp_path / "browser-variant"
    shutil.copytree(BROWSER_SOURCE, variant)
    popup = variant / "popup.js"
    popup.write_bytes(popup.read_bytes() + b"\n// promotion-fixture-variant\n")
    return variant


def _stage(
    root: Path,
    *,
    browser_source: Path,
    executable_bytes: bytes,
    source_head: str,
    source_tree: str,
    runtime_root: Path | None = None,
) -> tuple[Path, dict[str, object]]:
    executable = root / "artifacts" / "BDB-vNext-NativeHost.exe"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(executable_bytes)
    runtime = root / "stage" if runtime_root is None else runtime_root
    result = stage_client_plan(
        runtime_root=runtime,
        legacy_runtime_root=root / "legacy",
        bootstrap_authority_root=root / "ProgramData" / "BartoszDevBridge-Next" / "bootstrap",
        browser_source_root=browser_source,
        native_host_executable=executable,
        source_head=source_head,
        source_tree=source_tree,
    )
    return runtime, result


def _routes(runtime_root: str | Path) -> dict[str, object]:
    plan = query_client_plan(runtime_root=runtime_root)["plan"]
    manifest = str(Path(plan["native_manifest_path"]))
    target = [
        {"root": "HKCU", "view": "32", "value": manifest},
        {"root": "HKCU", "view": "64", "value": manifest},
    ]
    return {
        "target": target,
        "legacy": [],
        "target_conflict": False,
        "target_registered": True,
        "target_registered_views": ["32", "64"],
        "legacy_route_present": False,
        "production_activation_performed": False,
    }


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path, dict[str, object]]:
    monkeypatch.setattr(promotion, "observe_windows_native_routes", _routes)
    old_root = tmp_path / "old"
    production = tmp_path / "production"
    old_stage, _old_result = _stage(
        old_root,
        browser_source=BROWSER_SOURCE,
        executable_bytes=b"old-native",
        source_head="a" * 40,
        source_tree="b" * 40,
        runtime_root=production,
    )
    new_root = tmp_path / "new"
    new_stage, new_result = _stage(
        new_root,
        browser_source=_browser_variant(tmp_path),
        executable_bytes=b"new-native",
        source_head="c" * 40,
        source_tree="d" * 40,
    )
    return new_stage, production, old_stage, new_result


def test_valid_stage_promotes_path_bound_production_set_and_preserves_previous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage, production, old_stage, result = _fixture(tmp_path, monkeypatch)

    promoted = promotion.promote_client_plan(staged_runtime_root=stage, production_runtime_root=production)

    assert promoted["status"] == "COMMITTED"
    live = query_client_plan(runtime_root=production)["plan"]
    assert live["source_head"] == "c" * 40
    assert live["source_tree"] == "d" * 40
    assert Path(live["browser_bundle_root"]) == production / "clients" / "browser-extension"
    assert Path(live["native_manifest_path"]) == production / "clients" / "native-host" / "com.bartosz.dev_bridge.vnext.json"
    assert Path(live["native_config_path"]) == production / "config" / "native-host.json"
    assert (production / "clients" / "native-host" / "BDB-vNext-NativeHost.exe").read_bytes() == b"new-native"
    assert (production / "clients" / "browser-extension" / "popup.js").read_bytes() == (
        (stage / "clients" / "browser-extension" / "popup.js").read_bytes()
    )
    assert live["production_activation_performed"] is False

    transaction = production / "recovery" / "client-promotions" / promoted["transaction_id"]
    state = json.loads((transaction / "transaction.json").read_text(encoding="utf-8"))
    assert state["state"] == "COMMITTED"
    assert (transaction / "previous" / "clients" / "client-plan.json").is_file()
    assert json.loads((transaction / "previous" / "clients" / "client-plan.json").read_text(encoding="utf-8"))["source_head"] == "a" * 40
    assert old_stage.exists()


def test_same_stage_replay_is_idempotent_without_duplicate_backup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stage, production, _old_stage, _result = _fixture(tmp_path, monkeypatch)
    first = promotion.promote_client_plan(staged_runtime_root=stage, production_runtime_root=production)
    second = promotion.promote_client_plan(staged_runtime_root=stage, production_runtime_root=production)
    assert first["status"] == "COMMITTED"
    assert second["status"] == "IDEMPOTENT_COMMITTED"
    transactions = list((production / "recovery" / "client-promotions").iterdir())
    assert [item for item in transactions if item.is_dir()] == [
        production / "recovery" / "client-promotions" / first["transaction_id"]
    ]


def test_corrupt_stage_fails_before_live_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stage, production, _old_stage, _result = _fixture(tmp_path, monkeypatch)
    popup = stage / "clients" / "browser-extension" / "popup.js"
    popup.write_bytes(popup.read_bytes() + b"\n// stale-stage\n")
    with pytest.raises(M11cClientError) as exc:
        promotion.promote_client_plan(staged_runtime_root=stage, production_runtime_root=production)
    assert exc.value.code == "browser_bundle_stale"
    assert query_client_plan(runtime_root=production)["plan"]["source_head"] == "a" * 40
    assert not (production / "recovery" / "client-promotions").exists()


def test_registry_mismatch_fails_before_live_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stage, production, _old_stage, _result = _fixture(tmp_path, monkeypatch)

    def conflicting_routes(*, runtime_root: str | Path) -> dict[str, object]:
        routes = _routes(production)
        routes["target_conflict"] = True
        routes["target_registered"] = False
        return routes

    monkeypatch.setattr(promotion, "observe_windows_native_routes", conflicting_routes)
    with pytest.raises(M11cClientError) as exc:
        promotion.promote_client_plan(staged_runtime_root=stage, production_runtime_root=production)
    assert exc.value.code == "promotion_registry_mismatch"
    assert query_client_plan(runtime_root=production)["plan"]["source_head"] == "a" * 40


@pytest.mark.parametrize("fault_point", ["after_install_clients", "after_install_config", "before_verify"])
def test_live_fault_rolls_back_previous_coherent_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault_point: str
) -> None:
    stage, production, _old_stage, _result = _fixture(tmp_path, monkeypatch)

    def inject(point: str) -> None:
        if point == fault_point:
            raise RuntimeError(fault_point)

    with pytest.raises(M11cClientError):
        promotion.promote_client_plan(
            staged_runtime_root=stage,
            production_runtime_root=production,
            fault_injector=inject,
        )
    assert query_client_plan(runtime_root=production)["plan"]["source_head"] == "a" * 40
    transactions = list((production / "recovery" / "client-promotions").iterdir())
    state = json.loads((transactions[0] / "transaction.json").read_text(encoding="utf-8"))
    assert state["state"] == "ROLLED_BACK"


def test_interrupted_after_backup_recovers_same_transaction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stage, production, _old_stage, _result = _fixture(tmp_path, monkeypatch)

    def crash(point: str) -> None:
        if point == "after_backup":
            raise KeyboardInterrupt("simulated power loss")

    with pytest.raises(KeyboardInterrupt):
        promotion.promote_client_plan(
            staged_runtime_root=stage,
            production_runtime_root=production,
            fault_injector=crash,
        )
    recovered = promotion.promote_client_plan(staged_runtime_root=stage, production_runtime_root=production)
    assert recovered["status"] == "COMMITTED"
    assert query_client_plan(runtime_root=production)["plan"]["source_head"] == "c" * 40


@pytest.mark.parametrize("fault_point", [
    "before_backup",
    "after_backup",
    "after_install_clients",
    "after_install_config",
    "before_verify",
    "after_verify",
])
def test_interrupted_transaction_replays_after_any_mutation_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault_point: str
) -> None:
    stage, production, _old_stage, _result = _fixture(tmp_path, monkeypatch)

    def crash(point: str) -> None:
        if point == fault_point:
            raise KeyboardInterrupt(f"simulated power loss at {point}")

    with pytest.raises(KeyboardInterrupt):
        promotion.promote_client_plan(
            staged_runtime_root=stage,
            production_runtime_root=production,
            fault_injector=crash,
        )
    recovered = promotion.promote_client_plan(staged_runtime_root=stage, production_runtime_root=production)
    assert recovered["status"] == "COMMITTED"
    assert query_client_plan(runtime_root=production)["plan"]["source_head"] == "c" * 40

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import bdb_vnext.m11c_windows_clients as clients


def _runtime(tmp_path: Path) -> Path:
    runtime = tmp_path / "runtime"
    (runtime / "clients").mkdir(parents=True)
    return runtime


def test_target_route_conflict_blocks_registration_before_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime(tmp_path)
    monkeypatch.setattr(
        clients,
        "query_client_plan",
        lambda **_: {"plan": {"native_manifest_path": str(tmp_path / "expected.json")}},
    )
    monkeypatch.setattr(
        clients,
        "observe_windows_native_routes",
        lambda **_: {
            "target_conflict": True,
            "target_registered": False,
            "legacy_route_present": False,
            "target": [{"root": "HKCU", "view": "64", "value": str(tmp_path / "foreign.json")}],
            "legacy": [],
        },
    )
    with pytest.raises(clients.M11cClientError) as exc:
        clients.register_windows_target_native_host(runtime_root=runtime)
    assert exc.value.code == "target_native_route_conflict"
    assert not (runtime / "clients" / "prior-target-native-route-backup.json").exists()


def test_explicit_hkcu_target_replacement_backs_up_and_writes_both_registry_views(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime(tmp_path)
    expected = str(tmp_path / "expected.json")
    old32 = str(tmp_path / "old32.json")
    old64 = str(tmp_path / "old64.json")
    monkeypatch.setattr(
        clients,
        "query_client_plan",
        lambda **_: {"plan": {"native_manifest_path": expected}},
    )
    before = {
        "target_conflict": True,
        "target_registered": False,
        "target_registered_views": [],
        "legacy_route_present": False,
        "target": [
            {"root": "HKCU", "view": "32", "value": old32},
            {"root": "HKCU", "view": "64", "value": old64},
        ],
        "legacy": [],
        "production_activation_performed": False,
    }
    after = {
        "target_conflict": False,
        "target_registered": True,
        "target_registered_views": ["32", "64"],
        "legacy_route_present": False,
        "target": [
            {"root": "HKCU", "view": "32", "value": expected},
            {"root": "HKCU", "view": "64", "value": expected},
        ],
        "legacy": [],
        "production_activation_performed": False,
    }
    observations = iter((before, after))
    monkeypatch.setattr(clients, "observe_windows_native_routes", lambda **_: next(observations))

    writes: list[tuple[int, str]] = []

    class FakeKey:
        def __init__(self, view: int) -> None:
            self.view = view

        def __enter__(self):
            return self

        def __exit__(self, *_: object) -> None:
            return None

    fake = SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        KEY_WOW64_32KEY=0x0200,
        KEY_WOW64_64KEY=0x0100,
        KEY_SET_VALUE=0x0002,
        REG_SZ=1,
    )

    def create_key_ex(root: object, subkey: str, reserved: int, access: int):
        assert root is fake.HKEY_CURRENT_USER
        assert subkey == clients.TARGET_REGISTRY_SUBKEY
        assert reserved == 0
        view = access & (fake.KEY_WOW64_32KEY | fake.KEY_WOW64_64KEY)
        return FakeKey(view)

    def set_value_ex(key: FakeKey, name: object, reserved: int, kind: int, value: str) -> None:
        assert name is None and reserved == 0 and kind == fake.REG_SZ
        writes.append((key.view, value))

    fake.CreateKeyEx = create_key_ex
    fake.SetValueEx = set_value_ex
    monkeypatch.setattr(clients, "_winreg_module", lambda: fake)

    result = clients.register_windows_target_native_host(
        runtime_root=runtime,
        replace_existing_target=True,
    )
    assert result["target_registered"] is True
    assert {view for view, _ in writes} == {0x0200, 0x0100}
    assert all(value == expected for _, value in writes)
    backup = runtime / "clients" / "prior-target-native-route-backup.json"
    assert backup.is_file()
    document = json.loads(backup.read_text(encoding="utf-8"))
    assert document["replacement_manifest"] == expected
    assert {item["value"] for item in document["target"]} == {old32, old64}
    assert document["backup_sha256"].startswith("sha256:")


def test_hklm_target_conflict_never_replaced_even_with_explicit_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime(tmp_path)
    monkeypatch.setattr(
        clients,
        "query_client_plan",
        lambda **_: {"plan": {"native_manifest_path": str(tmp_path / "expected.json")}},
    )
    monkeypatch.setattr(
        clients,
        "observe_windows_native_routes",
        lambda **_: {
            "target_conflict": True,
            "target_registered": False,
            "target_registered_views": [],
            "legacy_route_present": False,
            "target": [{"root": "HKLM", "view": "64", "value": str(tmp_path / "managed.json")}],
            "legacy": [],
        },
    )
    with pytest.raises(clients.M11cClientError) as exc:
        clients.register_windows_target_native_host(runtime_root=runtime, replace_existing_target=True)
    assert exc.value.code == "target_native_route_requires_admin"
    assert not (runtime / "clients" / "prior-target-native-route-backup.json").exists()


def test_hklm_legacy_route_is_never_silently_deleted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime(tmp_path)
    monkeypatch.setattr(
        clients,
        "observe_windows_native_routes",
        lambda **_: {
            "target_conflict": False,
            "target_registered": True,
            "legacy_route_present": True,
            "target": [{"root": "HKCU", "view": "64", "value": str(tmp_path / "target.json")}],
            "legacy": [{"root": "HKLM", "view": "64", "value": str(tmp_path / "legacy.json")}],
        },
    )
    with pytest.raises(clients.M11cClientError) as exc:
        clients.disable_windows_legacy_native_route(runtime_root=runtime)
    assert exc.value.code == "legacy_native_route_requires_admin"
    assert not (runtime / "clients" / "legacy-native-route-backup.json").exists()


def test_hkcu_legacy_route_is_backed_up_then_removed_and_reobserved_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime(tmp_path)
    before = {
        "target_conflict": False,
        "target_registered": True,
        "legacy_route_present": True,
        "target": [{"root": "HKCU", "view": "64", "value": str(tmp_path / "target.json")}],
        "legacy": [
            {"root": "HKCU", "view": "32", "value": str(tmp_path / "legacy32.json")},
            {"root": "HKCU", "view": "64", "value": str(tmp_path / "legacy64.json")},
        ],
        "production_activation_performed": False,
    }
    after = {
        "target_conflict": False,
        "target_registered": True,
        "legacy_route_present": False,
        "target": before["target"],
        "legacy": [],
        "production_activation_performed": False,
    }
    observations = iter((before, after))
    monkeypatch.setattr(clients, "observe_windows_native_routes", lambda **_: next(observations))

    deleted: list[tuple[object, str, int, int]] = []
    fake = SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        KEY_WOW64_32KEY=0x0200,
        KEY_WOW64_64KEY=0x0100,
    )

    def delete_key_ex(root: object, subkey: str, view: int, reserved: int) -> None:
        deleted.append((root, subkey, view, reserved))

    fake.DeleteKeyEx = delete_key_ex
    monkeypatch.setattr(clients, "_winreg_module", lambda: fake)

    result = clients.disable_windows_legacy_native_route(runtime_root=runtime)
    assert result["legacy_route_present"] is False
    assert {item[2] for item in deleted} == {0x0200, 0x0100}
    backup = runtime / "clients" / "legacy-native-route-backup.json"
    assert backup.is_file()
    text = backup.read_text(encoding="utf-8")
    assert "legacy32.json" in text and "legacy64.json" in text
    assert "backup_sha256" in text


def test_no_legacy_route_is_idempotent_and_keeps_target_exact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime(tmp_path)
    observed = {
        "target_conflict": False,
        "target_registered": True,
        "legacy_route_present": False,
        "target": [{"root": "HKCU", "view": "64", "value": str(tmp_path / "target.json")}],
        "legacy": [],
        "production_activation_performed": False,
    }
    monkeypatch.setattr(clients, "observe_windows_native_routes", lambda **_: observed)
    assert clients.disable_windows_legacy_native_route(runtime_root=runtime) == observed

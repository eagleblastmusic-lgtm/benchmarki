from __future__ import annotations

import json
from pathlib import Path

import pytest

from bdb_vnext.m11c_legacy_recovery import (
    M11cLegacyRecoveryError,
    _parser,
    inspect_legacy_recovery,
    restore_legacy_native_config,
)
from bdb_vnext.m9a_handoff import _legacy_profiles


ORIGIN = "chrome-extension://" + ("a" * 32) + "/"


def _bridge_config(root: Path, *, name: str = "bdb-self", repository_id: str = "bartosz-dev-bridge") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "bridge-config.json"
    document = {
        "schema_version": "1.1",
        "control_repo_path": str(root.parent / f"{name}-control"),
        "fixture_repo_path": str(root.parent / f"{name}-fixture"),
        "worktree_root": str(root.parent / f"{name}-worktrees"),
        "runtime_dir": str(root / "runtime"),
        "repository_id": repository_id,
        "allowed_paths": ["README.md"],
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _native_document(legacy: Path, bridge: Path, *, alias: str = "bdb-self", wait: int = 30) -> dict[str, object]:
    return {
        "schema": "bdb-native-host-config-v1",
        "repositories": {alias: {"bridge_config_path": str(bridge)}},
        "allowed_origins": [ORIGIN],
        "state_path": str(legacy / "native-host-arm.json"),
        "session_store_path": str(legacy / "native-host-sessions.json"),
        "request_store_path": str(legacy / "native-host-requests.json"),
        "max_wait_seconds": wait,
        "max_message_bytes": 1048576,
    }


def _backup(legacy: Path, name: str, document: dict[str, object]) -> Path:
    path = legacy / name
    path.write_text(json.dumps(document, sort_keys=True, indent=2), encoding="utf-8")
    return path


def test_unique_valid_backup_is_restored_byte_exact_without_effect_surfaces(tmp_path: Path) -> None:
    legacy = tmp_path / "BartoszDevBridge"
    legacy.mkdir()
    bridge = _bridge_config(legacy / "workspaces" / "bdb-self")
    backup = _backup(
        legacy,
        "native-host.json.pre-calculator-benchmark.20260717T212213Z.bak",
        _native_document(legacy, bridge),
    )
    expected = backup.read_bytes()

    observed = inspect_legacy_recovery(legacy_runtime_root=legacy)
    assert observed["status"] == "RECOVERY_REQUIRED"
    assert len(observed["valid_backup_identities"]) == 1

    result = restore_legacy_native_config(legacy_runtime_root=legacy)

    target = legacy / "native-host.json"
    assert result["status"] == "RECOVERED_CONFIG_ONLY"
    assert result["restored"] is True
    assert result["repository_aliases"] == ["bdb-self"]
    assert result["registry_mutation_performed"] is False
    assert result["process_mutation_performed"] is False
    assert result["production_activation_performed"] is False
    assert target.read_bytes() == expected
    assert _legacy_profiles(legacy) == (("bdb-self", bridge.resolve()),)


def test_multiple_identical_valid_backups_are_one_identity(tmp_path: Path) -> None:
    legacy = tmp_path / "BartoszDevBridge"
    legacy.mkdir()
    bridge = _bridge_config(legacy / "workspaces" / "bdb-self")
    document = _native_document(legacy, bridge)
    first = _backup(legacy, "native-host.json.first.bak", document)
    second = legacy / "native-host.json.second.bak"
    second.write_bytes(first.read_bytes())

    inspected = inspect_legacy_recovery(legacy_runtime_root=legacy)
    assert len(inspected["valid_backup_identities"]) == 1
    assert len(inspected["valid_backup_identities"][0]["copies"]) == 2

    result = restore_legacy_native_config(legacy_runtime_root=legacy)
    assert result["status"] == "RECOVERED_CONFIG_ONLY"
    assert (legacy / "native-host.json").read_bytes() == first.read_bytes()


def test_different_valid_backup_identities_block_instead_of_guessing(tmp_path: Path) -> None:
    legacy = tmp_path / "BartoszDevBridge"
    legacy.mkdir()
    bridge = _bridge_config(legacy / "workspaces" / "bdb-self")
    _backup(legacy, "native-host.json.first.bak", _native_document(legacy, bridge, wait=20))
    _backup(legacy, "native-host.json.second.bak", _native_document(legacy, bridge, wait=30))

    with pytest.raises(M11cLegacyRecoveryError) as caught:
        restore_legacy_native_config(legacy_runtime_root=legacy)

    assert caught.value.code == "legacy_native_config_backup_ambiguous"
    assert not (legacy / "native-host.json").exists()


def test_backup_with_missing_or_invalid_bridge_subject_is_not_eligible(tmp_path: Path) -> None:
    legacy = tmp_path / "BartoszDevBridge"
    legacy.mkdir()
    missing = legacy / "workspaces" / "gone" / "bridge-config.json"
    _backup(legacy, "native-host.json.stale.bak", _native_document(legacy, missing))

    inspected = inspect_legacy_recovery(legacy_runtime_root=legacy)
    assert inspected["valid_backup_identities"] == []

    with pytest.raises(M11cLegacyRecoveryError) as caught:
        restore_legacy_native_config(legacy_runtime_root=legacy)
    assert caught.value.code == "legacy_native_config_backup_missing"
    assert not (legacy / "native-host.json").exists()


def test_existing_valid_target_is_idempotent_and_never_replaced(tmp_path: Path) -> None:
    legacy = tmp_path / "BartoszDevBridge"
    legacy.mkdir()
    bridge = _bridge_config(legacy / "workspaces" / "bdb-self")
    target = legacy / "native-host.json"
    target.write_text(json.dumps(_native_document(legacy, bridge)), encoding="utf-8")
    before = target.read_bytes()

    result = restore_legacy_native_config(legacy_runtime_root=legacy)

    assert result["status"] == "ALREADY_READY"
    assert result["restored"] is False
    assert target.read_bytes() == before


def test_invalid_existing_target_blocks_and_is_not_overwritten(tmp_path: Path) -> None:
    legacy = tmp_path / "BartoszDevBridge"
    legacy.mkdir()
    bridge = _bridge_config(legacy / "workspaces" / "bdb-self")
    _backup(legacy, "native-host.json.good.bak", _native_document(legacy, bridge))
    target = legacy / "native-host.json"
    target.write_text("{}", encoding="utf-8")

    with pytest.raises(M11cLegacyRecoveryError):
        restore_legacy_native_config(legacy_runtime_root=legacy)

    assert target.read_text(encoding="utf-8") == "{}"


def test_operator_surface_has_no_route_or_activation_verbs() -> None:
    parser = _parser()
    actions = parser._subparsers._group_actions[0].choices
    assert set(actions) == {"status", "restore-config"}
    forbidden = {"apply", "activate", "switch", "register", "install", "start", "stop", "disable"}
    assert forbidden.isdisjoint(actions)

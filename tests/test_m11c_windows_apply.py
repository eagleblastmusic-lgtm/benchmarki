from __future__ import annotations

import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest

import bdb_vnext.m11c_cutover as m11c
import bdb_vnext.m11c_windows_clients as clients
from bdb_vnext.m11c_windows_clients import (
    TARGET_REGISTRY_SUBKEY,
    register_windows_target_native_host,
)
from test_m11c_cutover import FREEZE_DIGEST, HEAD, TREE, _fixture, _m9a_report


pytestmark = pytest.mark.skipif(os.name != "nt", reason="M11c public apply is Windows-only")


class _IsolatedRegistryKey:
    def __init__(self, backend: "_IsolatedRegistry", root: object, subkey: str, view: int) -> None:
        self.backend = backend
        self.root = root
        self.subkey = subkey
        self.view = view

    def __enter__(self) -> "_IsolatedRegistryKey":
        return self

    def __exit__(self, *_: object) -> None:
        return None


class _IsolatedRegistry:
    """Minimal injected winreg backend; no handle can address the real Registry."""

    HKEY_CURRENT_USER = SimpleNamespace(name="ISOLATED_HKCU")
    HKEY_LOCAL_MACHINE = SimpleNamespace(name="ISOLATED_HKLM")
    KEY_READ = 0x20019
    KEY_SET_VALUE = 0x0002
    KEY_WOW64_32KEY = 0x0200
    KEY_WOW64_64KEY = 0x0100
    REG_SZ = 1

    def __init__(self) -> None:
        self._values: dict[tuple[str, str, int], tuple[str, int]] = {}

    def _root_name(self, root: object) -> str:
        if root is self.HKEY_CURRENT_USER:
            return "HKCU"
        if root is self.HKEY_LOCAL_MACHINE:
            return "HKLM"
        raise AssertionError("production or foreign Registry root selected by isolated test")

    def _view(self, access: int) -> int:
        selected = access & (self.KEY_WOW64_32KEY | self.KEY_WOW64_64KEY)
        if selected not in {self.KEY_WOW64_32KEY, self.KEY_WOW64_64KEY}:
            raise AssertionError("non-isolated Registry view selected by isolated test")
        return selected

    def _identity(self, root: object, subkey: str, view: int) -> tuple[str, str, int]:
        if view not in {self.KEY_WOW64_32KEY, self.KEY_WOW64_64KEY}:
            raise AssertionError("non-isolated Registry view selected by isolated test")
        return self._root_name(root), subkey, view

    def OpenKey(self, root: object, subkey: str, reserved: int, access: int) -> _IsolatedRegistryKey:
        assert reserved == 0
        view = self._view(access)
        identity = self._identity(root, subkey, view)
        if identity not in self._values:
            raise FileNotFoundError(subkey)
        return _IsolatedRegistryKey(self, root, subkey, view)

    def CreateKeyEx(self, root: object, subkey: str, reserved: int, access: int) -> _IsolatedRegistryKey:
        assert reserved == 0
        view = self._view(access)
        self._identity(root, subkey, view)
        return _IsolatedRegistryKey(self, root, subkey, view)

    def QueryValueEx(self, key: _IsolatedRegistryKey, name: object) -> tuple[str, int]:
        assert name is None
        identity = self._identity(key.root, key.subkey, key.view)
        if identity not in self._values:
            raise FileNotFoundError(key.subkey)
        return self._values[identity]

    def SetValueEx(self, key: _IsolatedRegistryKey, name: object, reserved: int, kind: int, value: str) -> None:
        assert name is None and reserved == 0 and kind == self.REG_SZ
        identity = self._identity(key.root, key.subkey, key.view)
        self._values[identity] = value, kind

    def DeleteKeyEx(self, root: object, subkey: str, view: int, reserved: int) -> None:
        assert reserved == 0
        identity = self._identity(root, subkey, view)
        if identity not in self._values:
            raise FileNotFoundError(subkey)
        del self._values[identity]

    def DeleteKey(self, root: object, subkey: str) -> None:
        raise AssertionError("view-less Registry cleanup is forbidden in isolated tests")

    def seed(self, *, root: object, subkey: str, view: int, value: str) -> None:
        self._values[self._identity(root, subkey, view)] = value, self.REG_SZ

    def snapshot(self) -> dict[tuple[str, str, int], tuple[str, int]]:
        return dict(self._values)

    def restore(self, snapshot: dict[tuple[str, str, int], tuple[str, int]]) -> None:
        self._values = dict(snapshot)


class _ProductionWinregGuard:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"direct production winreg access is forbidden in this test: {name}")


@contextmanager
def _restored_registry_session(registry: _IsolatedRegistry) -> Iterator[_IsolatedRegistry]:
    before = registry.snapshot()
    try:
        yield registry
    finally:
        registry.restore(before)


@pytest.fixture(autouse=True)
def _isolated_test_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[_IsolatedRegistry]:
    registry = _IsolatedRegistry()
    monkeypatch.setattr(clients, "_winreg_module", lambda: registry)
    monkeypatch.setitem(sys.modules, "winreg", _ProductionWinregGuard())
    with _restored_registry_session(registry):
        yield registry


@pytest.mark.parametrize("view", [_IsolatedRegistry.KEY_WOW64_32KEY, _IsolatedRegistry.KEY_WOW64_64KEY])
def test_isolated_registry_rejects_production_root_for_both_views(
    _isolated_test_registry: _IsolatedRegistry,
    view: int,
) -> None:
    with pytest.raises(AssertionError, match="production or foreign Registry root"):
        _isolated_test_registry.CreateKeyEx(
            object(),
            TARGET_REGISTRY_SUBKEY,
            0,
            _isolated_test_registry.KEY_SET_VALUE | view,
        )


def test_isolated_registry_routes_both_views_without_real_hkcu(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolated_test_registry: _IsolatedRegistry,
) -> None:
    expected = str(tmp_path / "isolated-native-manifest.json")
    monkeypatch.setattr(clients, "query_client_plan", lambda **_: {"plan": {"native_manifest_path": expected}})

    routes = register_windows_target_native_host(runtime_root=tmp_path / "runtime")

    assert routes["target_registered"] is True
    assert routes["target_registered_views"] == ["32", "64"]
    assert {item["value"] for item in routes["target"]} == {expected}
    assert {
        identity[2]
        for identity in _isolated_test_registry.snapshot()
        if identity[0] == "HKCU" and identity[1] == TARGET_REGISTRY_SUBKEY
    } == {
        _isolated_test_registry.KEY_WOW64_32KEY,
        _isolated_test_registry.KEY_WOW64_64KEY,
    }


@pytest.mark.parametrize("outcome", ["pass", "fail", "exception", "interrupt"])
def test_isolated_registry_cleanup_restores_preexisting_routes_for_every_exit(
    outcome: str,
) -> None:
    registry = _IsolatedRegistry()
    expected32 = "C:\\captured-production\\manifest-32.json"
    expected64 = "C:\\captured-production\\manifest-64.json"
    registry.seed(
        root=registry.HKEY_CURRENT_USER,
        subkey=TARGET_REGISTRY_SUBKEY,
        view=registry.KEY_WOW64_32KEY,
        value=expected32,
    )
    registry.seed(
        root=registry.HKEY_CURRENT_USER,
        subkey=TARGET_REGISTRY_SUBKEY,
        view=registry.KEY_WOW64_64KEY,
        value=expected64,
    )
    before = registry.snapshot()

    def exercise() -> None:
        with _restored_registry_session(registry):
            registry.DeleteKeyEx(
                registry.HKEY_CURRENT_USER,
                TARGET_REGISTRY_SUBKEY,
                registry.KEY_WOW64_32KEY,
                0,
            )
            registry.DeleteKeyEx(
                registry.HKEY_CURRENT_USER,
                TARGET_REGISTRY_SUBKEY,
                registry.KEY_WOW64_64KEY,
                0,
            )
            if outcome == "fail":
                raise AssertionError("simulated test failure")
            if outcome == "exception":
                raise RuntimeError("simulated test exception")
            if outcome == "interrupt":
                raise KeyboardInterrupt("simulated interrupted fixture")

    if outcome == "pass":
        exercise()
    else:
        expected_error = {
            "fail": AssertionError,
            "exception": RuntimeError,
            "interrupt": KeyboardInterrupt,
        }[outcome]
        with pytest.raises(expected_error):
            exercise()

    assert registry.snapshot() == before


def _powershell() -> str:
    for candidate in ("pwsh.exe", "powershell.exe"):
        value = shutil.which(candidate)
        if value:
            return value
    pytest.skip("PowerShell is required for the hosted-Windows M11c proof")


def _harden_real_acl(authority: Path) -> None:
    completed = subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-NonInteractive",
            "-File",
            "scripts/Set-BDBVNextBootstrapAuthorityAcl.ps1",
            "-Root",
            str(authority),
            "-Apply",
            "-AllowNonProgramDataForTest",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        err = completed.stderr.decode("utf-8", errors="replace")
        if "Administrators" in err or "właściciel" in err or "owner" in err or "Access" in err or "AccessDenied" in err:
            pytest.skip(f"Setting Administrators ACL owner requires elevated Administrator privileges: {err.strip()}")
        assert completed.returncode == 0, err


def _isolate_m9a_for_acl_route_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """These legacy tests prove ACL/route effects, not the M9a evidence producer.

    The real side-by-side archive and its production ordering are covered by
    test_m11c_m9a_handoff.py and test_m11c_m9a_production_boundary.py.
    """

    monkeypatch.setattr(m11c, "verify_side_by_side_report", lambda **_: FREEZE_DIGEST)
    monkeypatch.setattr(m11c, "revalidate_side_by_side_digest", lambda **_: FREEZE_DIGEST)


def test_public_windows_prepare_and_apply_reobserve_real_acl_and_native_route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _fixture(tmp_path)
    authority = Path(fixture["authority"])
    program_data = Path(fixture["program_data"])
    client_plan = fixture["client_plan"]
    _harden_real_acl(authority)
    monkeypatch.setenv("PROGRAMDATA", str(program_data))
    _isolate_m9a_for_acl_route_fixture(monkeypatch)
    routes = register_windows_target_native_host(runtime_root=fixture["runtime"])
    assert routes["target_registered"] is True
    assert routes["target_conflict"] is False

    prepared = m11c.prepare_windows_cutover_plan(
        authority_root=authority,
        runtime_root=fixture["runtime"],
        legacy_runtime_root=fixture["legacy"],
        preparation_id="prep-final",
        cutover_id="windows-real-1",
        source_head=HEAD,
        source_tree=TREE,
        m9a_report=_m9a_report(),
        browser_bundle_digest=client_plan["browser_bundle_digest"],
        native_manifest_digest=client_plan["native_manifest_sha256"],
    )
    plan = prepared["plan"]
    assert plan["client_plan_sha256"] == client_plan["client_plan_sha256"]
    assert plan["tcb_witness_sha256"].startswith("sha256:")
    assert plan["m9a_freeze_digest"] == FREEZE_DIGEST
    assert m11c.observe_bootstrap_activation(authority_root=authority)["status"] == "PREPARED"

    result = m11c.apply_windows_cutover(
        authority_root=authority,
        cutover_id="windows-real-1",
        expected_plan_sha256=plan["cutover_plan_sha256"],
        operator_approved=True,
    )

    assert result["status"] == "ACTIVE"
    assert result["production_activation_performed"] is True
    assert result["bootstrap"]["state"]["activation_authority"] == "m11c-external-bootstrap"
    assert result["client_gate"]["state"] == "ACTIVE"
    assert result["m3c_intake_enabled"] is True


def test_public_windows_apply_rejects_wrong_programdata_after_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _fixture(tmp_path)
    authority = Path(fixture["authority"])
    program_data = Path(fixture["program_data"])
    client_plan = fixture["client_plan"]
    _harden_real_acl(authority)
    monkeypatch.setenv("PROGRAMDATA", str(program_data))
    _isolate_m9a_for_acl_route_fixture(monkeypatch)

    prepared = m11c.prepare_windows_cutover_plan(
        authority_root=authority,
        runtime_root=fixture["runtime"],
        legacy_runtime_root=fixture["legacy"],
        preparation_id="prep-final",
        cutover_id="windows-real-2",
        source_head=HEAD,
        source_tree=TREE,
        m9a_report=_m9a_report(),
        browser_bundle_digest=client_plan["browser_bundle_digest"],
        native_manifest_digest=client_plan["native_manifest_sha256"],
    )
    plan = prepared["plan"]

    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "different-programdata"))
    with pytest.raises(Exception):
        m11c.apply_windows_cutover(
            authority_root=authority,
            cutover_id="windows-real-2",
            expected_plan_sha256=plan["cutover_plan_sha256"],
            operator_approved=True,
        )

    assert m11c.observe_bootstrap_activation(authority_root=authority)["status"] == "PREPARED"

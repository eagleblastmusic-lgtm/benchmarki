from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

import bdb_vnext.m11c_cutover as m11c
from bdb_vnext.m11c_windows_clients import (
    TARGET_REGISTRY_SUBKEY,
    register_windows_target_native_host,
)
from test_m11c_cutover import FREEZE_DIGEST, HEAD, TREE, _fixture, _m9a_report


pytestmark = pytest.mark.skipif(os.name != "nt", reason="M11c public apply is Windows-only")


@pytest.fixture(autouse=True)
def _cleanup_test_target_native_route():
    yield
    if os.name != "nt":
        return
    import winreg

    for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
        try:
            winreg.DeleteKeyEx(winreg.HKEY_CURRENT_USER, TARGET_REGISTRY_SUBKEY, view, 0)
        except (FileNotFoundError, OSError):
            pass


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
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")


def test_public_windows_prepare_and_apply_reobserve_real_acl_and_native_route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _fixture(tmp_path)
    authority = Path(fixture["authority"])
    program_data = Path(fixture["program_data"])
    client_plan = fixture["client_plan"]
    _harden_real_acl(authority)
    monkeypatch.setenv("PROGRAMDATA", str(program_data))
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

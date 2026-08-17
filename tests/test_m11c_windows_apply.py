from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

import bdb_vnext.m11c_cutover as m11c
from test_m11c_cutover import (
    BROWSER_DIGEST,
    FREEZE_DIGEST,
    HEAD,
    NATIVE_DIGEST,
    TREE,
    _fixture,
    _m9a_report,
)


pytestmark = pytest.mark.skipif(os.name != "nt", reason="M11c public apply is Windows-only")


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


def test_public_windows_prepare_and_apply_reobserve_real_acl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _fixture(tmp_path)
    authority = Path(fixture["authority"])
    program_data = Path(fixture["program_data"])
    _harden_real_acl(authority)
    monkeypatch.setenv("PROGRAMDATA", str(program_data))

    prepared = m11c.prepare_windows_cutover_plan(
        authority_root=authority,
        runtime_root=fixture["runtime"],
        legacy_runtime_root=fixture["legacy"],
        preparation_id="prep-final",
        cutover_id="windows-real-1",
        source_head=HEAD,
        source_tree=TREE,
        m9a_report=_m9a_report(),
        browser_bundle_digest=BROWSER_DIGEST,
        native_manifest_digest=NATIVE_DIGEST,
    )
    plan = prepared["plan"]
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
        browser_bundle_digest=BROWSER_DIGEST,
        native_manifest_digest=NATIVE_DIGEST,
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

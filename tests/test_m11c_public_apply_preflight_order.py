from __future__ import annotations

import os
from pathlib import Path

import pytest

import bdb_vnext.m11c_cutover as m11c
from test_m11c_cutover import _fixture, _plan


pytestmark = pytest.mark.skipif(os.name != "nt", reason="public M11c apply is Windows-only")


def _pretend_target_route_is_ready(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    disabled: list[str] = []
    monkeypatch.setattr(
        m11c,
        "observe_windows_native_routes",
        lambda **_: {
            "target_conflict": False,
            "target_registered": True,
            "legacy_route_present": True,
            "target": [{"root": "HKCU", "view": "64", "value": "target-manifest.json"}],
            "legacy": [{"root": "HKCU", "view": "64", "value": "legacy-manifest.json"}],
            "production_activation_performed": False,
        },
    )

    def forbidden_disable(**_: object):
        disabled.append("called")
        raise AssertionError("Legacy Native route must not be disabled by a blocked cutover")

    monkeypatch.setattr(m11c, "disable_windows_legacy_native_route", forbidden_disable)
    return disabled


def test_wrong_approval_sha_is_rejected_before_legacy_route_disable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path)
    plan = _plan(fixture)
    monkeypatch.setenv("PROGRAMDATA", str(fixture["program_data"]))
    disabled = _pretend_target_route_is_ready(monkeypatch)

    with pytest.raises(m11c.M11cCutoverError) as exc:
        m11c.apply_windows_cutover(
            authority_root=fixture["authority"],
            cutover_id="final-1",
            expected_plan_sha256="sha256:" + "f" * 64,
            operator_approved=True,
        )

    assert exc.value.code == "cutover_plan_stale"
    assert disabled == []
    assert m11c.observe_bootstrap_activation(authority_root=fixture["authority"])["status"] == "PREPARED"


def test_missing_browser_native_witness_is_rejected_before_legacy_route_disable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path)
    plan = _plan(fixture)
    monkeypatch.setenv("PROGRAMDATA", str(fixture["program_data"]))
    verification = Path(fixture["runtime"]) / "clients" / "browser-client-verification.json"
    verification.unlink()
    disabled = _pretend_target_route_is_ready(monkeypatch)

    with pytest.raises((m11c.M11cCutoverError, FileNotFoundError)):
        m11c.apply_windows_cutover(
            authority_root=fixture["authority"],
            cutover_id="final-1",
            expected_plan_sha256=plan["cutover_plan_sha256"],
            operator_approved=True,
        )

    assert disabled == []
    assert m11c.observe_bootstrap_activation(authority_root=fixture["authority"])["status"] == "PREPARED"

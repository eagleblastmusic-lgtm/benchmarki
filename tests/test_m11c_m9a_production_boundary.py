from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import bdb_vnext.m11c_cutover as m11c
import bdb_vnext.m9a_handoff_cli as handoff_cli
from bdb_vnext.m9a_handoff import M9aHandoffError


SHA = "sha256:" + "a" * 64


def test_windows_prepare_rejects_unverified_m9a_before_tcb_or_preparation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(m11c.os, "name", "nt")
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "ProgramData"))
    calls: list[str] = []

    def reject(**_: object) -> str:
        calls.append("verify-m9a")
        raise M9aHandoffError("m9a_evidence_invalid", "blocked")

    monkeypatch.setattr(m11c, "verify_side_by_side_report", reject)
    monkeypatch.setattr(m11c, "query_prepared_activation", lambda **_: (_ for _ in ()).throw(AssertionError("preparation observed before M9a gate")))
    monkeypatch.setattr(m11c, "build_windows_tcb_witness", lambda **_: (_ for _ in ()).throw(AssertionError("TCB observed before M9a gate")))

    with pytest.raises(m11c.M11cCutoverError) as exc:
        m11c.prepare_windows_cutover_plan(
            authority_root=tmp_path / "authority",
            runtime_root=tmp_path / "runtime",
            legacy_runtime_root=tmp_path / "legacy",
            preparation_id="prep-final",
            cutover_id="final-1",
            source_head="1" * 40,
            source_tree="2" * 40,
            m9a_report={},
            browser_bundle_digest=SHA,
            native_manifest_digest=SHA,
        )
    assert exc.value.code == "m9a_evidence_invalid"
    assert calls == ["verify-m9a"]


def test_windows_apply_revalidates_m9a_before_bootstrap_tcb_or_route_effect(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    authority = tmp_path / "authority"
    runtime = tmp_path / "runtime"
    legacy = tmp_path / "legacy"
    monkeypatch.setattr(m11c.os, "name", "nt")
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "ProgramData"))

    exact_paths = {
        "authority_root": authority,
        "runtime_root": runtime,
        "legacy_runtime_root": legacy,
    }
    monkeypatch.setattr(m11c, "_absolute_path", lambda _value, *, field: exact_paths[field])

    plan = {
        "cutover_plan_sha256": SHA,
        "runtime_root": str(runtime),
        "legacy_runtime_root": str(legacy),
        "client_plan_sha256": SHA,
        "m9a_freeze_digest": SHA,
    }
    calls: list[str] = []
    monkeypatch.setattr(m11c, "_load_plan", lambda *_: plan)
    monkeypatch.setattr(m11c, "require_client_verification", lambda **_: calls.append("client"))

    def reject(**_: object) -> str:
        calls.append("revalidate-m9a")
        raise M9aHandoffError("m9a_legacy_drift", "blocked")

    monkeypatch.setattr(m11c, "revalidate_side_by_side_digest", reject)
    monkeypatch.setattr(m11c, "observe_bootstrap_activation", lambda **_: (_ for _ in ()).throw(AssertionError("bootstrap observed before M9a gate")))
    monkeypatch.setattr(m11c, "build_windows_tcb_witness", lambda **_: (_ for _ in ()).throw(AssertionError("TCB observed before M9a gate")))
    monkeypatch.setattr(m11c, "disable_windows_legacy_native_route", lambda **_: (_ for _ in ()).throw(AssertionError("route disabled before M9a gate")))

    with pytest.raises(m11c.M11cCutoverError) as exc:
        m11c.apply_windows_cutover(
            authority_root=authority,
            cutover_id="final-1",
            expected_plan_sha256=SHA,
            operator_approved=True,
        )
    assert exc.value.code == "m9a_legacy_drift"
    assert calls == ["client", "revalidate-m9a"]


def test_m9a_operator_cli_is_installed_and_has_no_activation_or_legacy_disable_verb() -> None:
    assert shutil.which("bdb-vnext-m9a-handoff") or shutil.which("bdb-vnext-m9a-handoff.exe")
    parser = handoff_cli._parser()
    for forbidden in ("activate", "apply", "switch", "disable", "freeze-legacy", "install", "start", "stop"):
        with pytest.raises(SystemExit):
            parser.parse_args([forbidden])

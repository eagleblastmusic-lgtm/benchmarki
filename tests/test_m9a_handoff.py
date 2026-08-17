from __future__ import annotations

import json
from pathlib import Path

import pytest

import bdb_vnext.m9a_handoff as handoff


SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64
HEAD = "1" * 40
TREE = "2" * 40


def _plan() -> dict[str, object]:
    return {
        "client_plan_sha256": SHA_A,
        "source_head": HEAD,
        "source_tree": TREE,
        "browser_bundle_digest": SHA_B,
        "native_manifest_sha256": SHA_C,
    }


def _routes(*, legacy: bool = False, conflict: bool = False, registered: bool = True) -> dict[str, object]:
    return {
        "target": ([{"root": "HKCU", "view": "32", "value": r"C:\\vnext.json"}, {"root": "HKCU", "view": "64", "value": r"C:\\vnext.json"}] if registered else []),
        "legacy": ([{"root": "HKCU", "view": "64", "value": r"C:\\legacy.json"}] if legacy else []),
        "target_registered": registered,
        "target_conflict": conflict,
        "legacy_route_present": legacy,
        "production_activation_performed": False,
    }


def _install_stubs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, first: str = SHA_A, second: str | None = None) -> tuple[Path, Path]:
    runtime = tmp_path / "runtime"
    legacy = tmp_path / "legacy"
    runtime.mkdir()
    legacy.mkdir()
    profile = tmp_path / "bridge.json"
    profile.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(handoff, "query_client_plan", lambda **_: {"plan": _plan()})
    monkeypatch.setattr(
        handoff,
        "require_client_verification",
        lambda **_: {"verification_sha256": "sha256:" + "d" * 64},
    )
    monkeypatch.setattr(handoff, "_legacy_profiles", lambda _: (("default", profile),))
    calls = {"count": 0}

    def fake_probe(**_: object) -> dict[str, dict[str, object]]:
        calls["count"] += 1
        digest = first if calls["count"] == 1 or second is None else second
        return {"default": {"schema": "test-probe", "probe_digest": digest}}

    monkeypatch.setattr(handoff, "_probe_once", fake_probe)
    return runtime, legacy


def test_capture_pass_closed_is_side_by_side_and_content_addressed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime, legacy = _install_stubs(monkeypatch, tmp_path)
    result = handoff.capture_side_by_side_handoff(
        runtime_root=runtime,
        legacy_runtime_root=legacy,
        observation_seconds=0,
        route_observer=lambda **_: _routes(),
    )
    report = result["report"]
    assert result["status"] == "PASS_CLOSED"
    assert report["status"] == "PASS_CLOSED"
    assert report["legacy_product_globally_disabled"] is False
    assert result["legacy_mutation_performed"] is False
    assert result["production_activation_performed"] is False
    assert report["duplicate_routes"] == 0
    assert report["legacy_store_drift"] == 0
    assert report["ownership_collisions"] == 0
    assert report["archive_readable"] is True
    assert len(report["evidence_refs"]) == 5
    assert handoff.verify_side_by_side_report(runtime_root=runtime, report=report) == report["freeze_digest"]


def test_capture_blocks_when_legacy_route_still_owns_takeover_subject(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime, legacy = _install_stubs(monkeypatch, tmp_path)
    result = handoff.capture_side_by_side_handoff(
        runtime_root=runtime,
        legacy_runtime_root=legacy,
        observation_seconds=0,
        route_observer=lambda **_: _routes(legacy=True),
    )
    assert result["status"] == "BLOCKED"
    assert result["report"]["duplicate_routes"] == 1
    assert result["report"]["legacy_ingress_frozen"] is False
    assert result["report"]["legacy_product_globally_disabled"] is False


def test_capture_blocks_on_takeover_sensitive_probe_drift(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime, legacy = _install_stubs(monkeypatch, tmp_path, first=SHA_A, second=SHA_B)
    result = handoff.capture_side_by_side_handoff(
        runtime_root=runtime,
        legacy_runtime_root=legacy,
        observation_seconds=0,
        route_observer=lambda **_: _routes(),
    )
    assert result["status"] == "BLOCKED"
    assert result["report"]["legacy_store_drift"] == 1
    assert result["report"]["legacy_writer_frozen"] is False


def test_verify_detects_tampered_content_addressed_archive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime, legacy = _install_stubs(monkeypatch, tmp_path)
    result = handoff.capture_side_by_side_handoff(
        runtime_root=runtime,
        legacy_runtime_root=legacy,
        observation_seconds=0,
        route_observer=lambda **_: _routes(),
    )
    report = result["report"]
    archive = Path(result["archive_path"])
    archive.write_text(json.dumps({"tampered": True}), encoding="utf-8")
    with pytest.raises(handoff.M9aHandoffError) as exc:
        handoff.verify_side_by_side_report(runtime_root=runtime, report=report)
    assert exc.value.code == "evidence_digest_mismatch"


def test_revalidate_blocks_if_legacy_route_returns(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime, legacy = _install_stubs(monkeypatch, tmp_path)
    result = handoff.capture_side_by_side_handoff(
        runtime_root=runtime,
        legacy_runtime_root=legacy,
        observation_seconds=0,
        route_observer=lambda **_: _routes(),
    )
    with pytest.raises(handoff.M9aHandoffError) as exc:
        handoff.revalidate_side_by_side_digest(
            runtime_root=runtime,
            legacy_runtime_root=legacy,
            freeze_digest=result["report"]["freeze_digest"],
            route_observer=lambda **_: _routes(legacy=True),
        )
    assert exc.value.code == "m9a_route_fence_stale"


def test_revalidate_blocks_if_legacy_probe_drifted_after_capture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime, legacy = _install_stubs(monkeypatch, tmp_path)
    result = handoff.capture_side_by_side_handoff(
        runtime_root=runtime,
        legacy_runtime_root=legacy,
        observation_seconds=0,
        route_observer=lambda **_: _routes(),
    )
    monkeypatch.setattr(
        handoff,
        "_probe_once",
        lambda **_: {"default": {"schema": "test-probe", "probe_digest": SHA_C}},
    )
    with pytest.raises(handoff.M9aHandoffError) as exc:
        handoff.revalidate_side_by_side_digest(
            runtime_root=runtime,
            legacy_runtime_root=legacy,
            freeze_digest=result["report"]["freeze_digest"],
            route_observer=lambda **_: _routes(),
        )
    assert exc.value.code == "m9a_legacy_drift"

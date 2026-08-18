from __future__ import annotations

from pathlib import Path

import pytest

import bdb_vnext.m9a_handoff as handoff
from test_m9a_handoff import _install_stubs, _routes


def test_full_m9a_archive_verifier_reads_all_bound_objects(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime, legacy = _install_stubs(monkeypatch, tmp_path)
    result = handoff.capture_side_by_side_handoff(
        runtime_root=runtime,
        legacy_runtime_root=legacy,
        observation_seconds=0,
        route_observer=lambda **_: _routes(),
    )
    freeze = result["report"]["freeze_digest"]
    verified = handoff.verify_side_by_side_archive(
        runtime_root=runtime,
        freeze_digest=freeze,
    )
    assert verified["archive_readable"] is True
    assert verified["freeze_digest"] == freeze
    assert verified["evidence_object_count"] == 5
    assert verified["evidence_refs"] == result["report"]["evidence_refs"]
    assert verified["legacy_mutation_performed"] is False
    assert verified["production_activation_performed"] is False


def test_full_m9a_archive_verifier_detects_tampered_child_evidence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime, legacy = _install_stubs(monkeypatch, tmp_path)
    result = handoff.capture_side_by_side_handoff(
        runtime_root=runtime,
        legacy_runtime_root=legacy,
        observation_seconds=0,
        route_observer=lambda **_: _routes(),
    )
    freeze = result["report"]["freeze_digest"]
    child = result["report"]["evidence_refs"][1]
    path = runtime / "evidence" / "m9a-side-by-side" / "objects" / f"{child[7:]}.json"
    path.write_text('{"tampered":true}', encoding="utf-8")

    with pytest.raises(handoff.M9aHandoffError) as caught:
        handoff.verify_side_by_side_archive(
            runtime_root=runtime,
            freeze_digest=freeze,
        )
    assert caught.value.code == "evidence_digest_mismatch"

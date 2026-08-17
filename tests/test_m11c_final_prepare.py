from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import bdb_vnext.m11c_final_prepare as final


SHA = "sha256:" + "a" * 64
HEAD = "1" * 40
TREE = "2" * 40


def _client_plan() -> dict[str, object]:
    return {
        "source_head": HEAD,
        "source_tree": TREE,
        "client_plan_sha256": SHA,
        "browser_bundle_digest": "sha256:" + "b" * 64,
        "native_manifest_sha256": "sha256:" + "c" * 64,
    }


def _prepared(preparation_id: str) -> dict[str, object]:
    return {
        "prepared": {
            "preparation_id": preparation_id,
            "preparation_sha256": "sha256:" + "d" * 64,
            "slot_binding": {
                "candidate_manifest_sha256": "sha256:" + "e" * 64,
            },
            "backup": {
                "path": "C:/backup",
                "manifest_sha256": "sha256:" + "f" * 64,
            },
        },
        "slots": {
            "state": {
                "candidate_manifest_sha256": "sha256:" + "e" * 64,
            }
        },
    }


def test_ids_are_deterministic_and_derived_from_staged_subject() -> None:
    preparation_id, cutover_id = final._ids(HEAD, SHA)
    assert preparation_id == "final-prep-111111111111-aaaaaaaaaaaa"
    assert cutover_id == "final-cutover-111111111111-aaaaaaaaaaaa"


def test_operator_parser_has_no_activation_or_legacy_disable_verb() -> None:
    parser = final._parser()
    for forbidden in ("apply", "activate", "switch", "disable", "freeze-legacy", "install", "start", "stop"):
        with pytest.raises(SystemExit):
            parser.parse_args([forbidden])
    assert parser.parse_args(["prepare", "--authority-root", "A", "--runtime-root", "R", "--legacy-runtime-root", "L"]).command == "prepare"
    assert parser.parse_args(["status", "--authority-root", "A", "--runtime-root", "R", "--legacy-runtime-root", "L"]).command == "status"


def test_final_prepare_blocks_before_m9a_if_client_gate_already_exists(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(final, "os", SimpleNamespace(name="nt"))
    runtime = tmp_path / "runtime"
    authority = tmp_path / "authority"
    legacy = tmp_path / "legacy"
    monkeypatch.setattr(final, "_client_identity", lambda *_: (_client_plan(), HEAD, TREE))
    monkeypatch.setattr(
        final,
        "observe_bootstrap_activation",
        lambda **_: {"status": "PREPARED", "production_activation_performed": False},
    )
    monkeypatch.setattr(final, "read_activation", lambda *_: object())
    monkeypatch.setattr(
        final,
        "capture_side_by_side_handoff",
        lambda **_: (_ for _ in ()).throw(AssertionError("M9a capture ran after client-gate conflict")),
    )

    with pytest.raises(final.M11cFinalPreparationError) as exc:
        final.prepare_final_cutover(
            authority_root=authority,
            runtime_root=runtime,
            legacy_runtime_root=legacy,
        )
    assert exc.value.code == "client_gate_already_present"


def test_final_prepare_orders_m9a_backup_then_immutable_plan(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(final, "os", SimpleNamespace(name="nt"))
    runtime = tmp_path / "runtime"
    authority = tmp_path / "authority"
    legacy = tmp_path / "legacy"
    calls: list[str] = []
    preparation_id, cutover_id = final._ids(HEAD, SHA)
    prepared = _prepared(preparation_id)
    report = {
        "schema": "bdb-vnext-m9a-freeze-report-v1",
        "status": "PASS_CLOSED",
        "freeze_digest": "sha256:" + "9" * 64,
    }

    monkeypatch.setattr(final, "_client_identity", lambda *_: (_client_plan(), HEAD, TREE))
    monkeypatch.setattr(final, "_assert_pre_activation", lambda *_: calls.append("preactivation") or {})
    monkeypatch.setattr(final, "_existing_plan", lambda *_: None)
    monkeypatch.setattr(
        final,
        "capture_side_by_side_handoff",
        lambda **_: calls.append("m9a-capture") or {"status": "PASS_CLOSED", "report": report},
    )
    monkeypatch.setattr(
        final,
        "verify_side_by_side_report",
        lambda **_: calls.append("m9a-verify") or report["freeze_digest"],
    )
    monkeypatch.setattr(
        final,
        "revalidate_side_by_side_digest",
        lambda **_: calls.append("m9a-revalidate") or report["freeze_digest"],
    )
    monkeypatch.setattr(final, "_existing_preparation", lambda *_: None)
    monkeypatch.setattr(
        final,
        "prepare_candidate_activation",
        lambda **kwargs: calls.append("m11a-prepare") or prepared,
    )
    monkeypatch.setattr(
        final,
        "prepare_windows_cutover_plan",
        lambda **kwargs: calls.append("m11c-plan") or {"plan": {"cutover_plan_sha256": SHA}},
    )
    monkeypatch.setattr(
        final,
        "_final_result",
        lambda **kwargs: calls.append("final-revalidate")
        or {
            "schema": final.FINAL_PREP_SCHEMA,
            "status": "PREPARED_NOT_ACTIVATED",
            "preparation_id": preparation_id,
            "cutover_id": cutover_id,
            "production_activation_performed": False,
        },
    )

    result = final.prepare_final_cutover(
        authority_root=authority,
        runtime_root=runtime,
        legacy_runtime_root=legacy,
        observation_seconds=0,
    )
    assert result["status"] == "PREPARED_NOT_ACTIVATED"
    assert result["production_activation_performed"] is False
    assert calls == [
        "preactivation",
        "m9a-capture",
        "m9a-verify",
        "m9a-revalidate",
        "m11a-prepare",
        "m11c-plan",
        "final-revalidate",
    ]


def test_existing_immutable_plan_is_revalidated_without_new_capture_or_backup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(final, "os", SimpleNamespace(name="nt"))
    runtime = tmp_path / "runtime"
    authority = tmp_path / "authority"
    legacy = tmp_path / "legacy"
    _preparation_id, cutover_id = final._ids(HEAD, SHA)
    freeze = "sha256:" + "9" * 64
    monkeypatch.setattr(final, "_client_identity", lambda *_: (_client_plan(), HEAD, TREE))
    monkeypatch.setattr(final, "_assert_pre_activation", lambda *_: {})
    monkeypatch.setattr(
        final,
        "_existing_plan",
        lambda *_: {"plan": {"cutover_id": cutover_id, "m9a_freeze_digest": freeze}},
    )
    monkeypatch.setattr(
        final,
        "capture_side_by_side_handoff",
        lambda **_: (_ for _ in ()).throw(AssertionError("existing plan must not recapture M9a")),
    )
    monkeypatch.setattr(
        final,
        "prepare_candidate_activation",
        lambda **_: (_ for _ in ()).throw(AssertionError("existing plan must not create a second backup")),
    )
    monkeypatch.setattr(
        final,
        "_final_result",
        lambda **kwargs: {
            "schema": final.FINAL_PREP_SCHEMA,
            "status": "PREPARED_NOT_ACTIVATED",
            "m9a_freeze_digest": kwargs["freeze_digest"],
            "production_activation_performed": False,
        },
    )

    result = final.prepare_final_cutover(
        authority_root=authority,
        runtime_root=runtime,
        legacy_runtime_root=legacy,
    )
    assert result["m9a_freeze_digest"] == freeze
    assert result["production_activation_performed"] is False

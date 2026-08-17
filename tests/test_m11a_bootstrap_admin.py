from __future__ import annotations

import argparse
from pathlib import Path

import pytest

import bdb_vnext.m11a_bootstrap_admin as admin
from bdb_vnext.m11a_windows_tcb import M11aWindowsTcbError


def _slots(tmp_path: Path, *, candidate: bool = True) -> dict[str, object]:
    return {
        "state": {"legacy_runtime_root": str((tmp_path / "legacy").absolute())},
        "slots": {
            "ACTIVE": {"bundle_root": str(tmp_path / "active")},
            "PREVIOUS": {"bundle_root": str(tmp_path / "previous")},
            "CANDIDATE": {"bundle_root": str(tmp_path / "candidate")} if candidate else None,
        },
        "actions": {"activate_candidate": False},
    }


def _args(command: str, **values: object) -> argparse.Namespace:
    defaults: dict[str, object] = {
        "command": command,
        "authority_root": "authority",
        "runtime_root": "runtime",
        "legacy_runtime_root": "legacy",
        "mutable_root": [],
        "recovery_target": "recovery",
        "preparation_id": "prep-1",
        "health_timeout": 1.0,
        "source_quiesced": True,
        "include_control_identity": False,
    }
    defaults.update(values)
    return argparse.Namespace(**defaults)


def test_parser_has_no_activation_command() -> None:
    parser = admin._parser()
    action = next(item for item in parser._actions if isinstance(item, argparse._SubParsersAction))
    assert set(action.choices) == {"status", "verify-tcb", "prepare"}
    assert "activate" not in action.choices
    assert "switch" not in action.choices


def test_status_is_read_only_and_reports_activation_unavailable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    slots = _slots(tmp_path)
    monkeypatch.setattr(admin, "query_slot_authority", lambda **_: slots)

    result = admin._execute(_args("status", authority_root=str(tmp_path / "authority"), preparation_id=None))

    assert result["operation"] == "STATUS"
    assert result["slots"] is slots
    assert result["prepared"] is None
    assert result["activation_operation_available"] is False
    assert result["activation_deferred_to"] == "M11c"


def test_verify_tcb_routes_current_candidate_as_mutable_subject(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    slots = _slots(tmp_path)
    observed: dict[str, object] = {}
    monkeypatch.setattr(admin, "query_slot_authority", lambda **_: slots)
    monkeypatch.setattr(admin, "_assert_legacy_binding", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(admin, "_program_data", lambda: tmp_path / "ProgramData")

    def witness(**kwargs: object) -> dict[str, object]:
        observed.update(kwargs)
        return {"candidate_may_write_authority": False, "activation_operation_available": False}

    monkeypatch.setattr(admin, "build_windows_tcb_witness", witness)
    result = admin._execute(
        _args(
            "verify-tcb",
            authority_root=str(tmp_path / "ProgramData" / "BartoszDevBridge-Next" / "bootstrap"),
            runtime_root=str(tmp_path / "runtime"),
            legacy_runtime_root=str(tmp_path / "legacy"),
            mutable_root=[str(tmp_path / "extra")],
        )
    )

    assert result["operation"] == "VERIFY_TCB"
    assert result["activation_operation_available"] is False
    mutable = tuple(observed["mutable_roots"])  # type: ignore[arg-type]
    assert str(tmp_path / "candidate") in mutable
    assert str(tmp_path / "extra") in mutable


def test_prepare_verifies_tcb_before_writing_preparation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    slots = _slots(tmp_path)
    order: list[str] = []

    def tcb(**_: object):
        order.append("tcb")
        return slots, {"candidate_may_write_authority": False, "activation_operation_available": False}

    def prepare(**kwargs: object):
        order.append("prepare")
        assert kwargs["source_is_quiesced"] is True
        return {"actions": {"activate_candidate": False}}

    monkeypatch.setattr(admin, "_tcb_for_current_slots", tcb)
    monkeypatch.setattr(admin, "prepare_candidate_activation", prepare)

    result = admin._execute(
        _args(
            "prepare",
            authority_root=str(tmp_path / "authority"),
            runtime_root=str(tmp_path / "runtime"),
            legacy_runtime_root=str(tmp_path / "legacy"),
            recovery_target=str(tmp_path / "recovery"),
        )
    )

    assert order == ["tcb", "prepare"]
    assert result["operation"] == "PREPARE"
    assert result["activation_operation_available"] is False
    assert result["activation_deferred_to"] == "M11c"


def test_prepare_without_candidate_fails_before_preparation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    slots = _slots(tmp_path, candidate=False)
    monkeypatch.setattr(
        admin,
        "_tcb_for_current_slots",
        lambda **_: (slots, {"candidate_may_write_authority": False}),
    )
    called = {"prepare": False}

    def prepare(**_: object):
        called["prepare"] = True
        return {}

    monkeypatch.setattr(admin, "prepare_candidate_activation", prepare)

    with pytest.raises(M11aWindowsTcbError) as caught:
        admin._execute(_args("prepare"))

    assert caught.value.code == "candidate_required"
    assert called["prepare"] is False


def test_legacy_binding_is_exact(tmp_path: Path) -> None:
    slots = _slots(tmp_path)
    admin._assert_legacy_binding(slots, tmp_path / "legacy")

    with pytest.raises(M11aWindowsTcbError) as caught:
        admin._assert_legacy_binding(slots, tmp_path / "other-legacy")

    assert caught.value.code == "legacy_runtime_mismatch"

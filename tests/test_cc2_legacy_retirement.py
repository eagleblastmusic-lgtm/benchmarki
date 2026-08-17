from __future__ import annotations

import builtins
import json
from pathlib import Path

import pytest

from bdb_gui import app
from bdb_vnext.control_center_query import _off_snapshot


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ACTIVE_CONTROL_CENTER_SOURCES = (
    REPOSITORY_ROOT / "bdb_gui" / "app.py",
    REPOSITORY_ROOT / "bdb_gui" / "vnext_control_center.py",
    REPOSITORY_ROOT / "bdb_vnext" / "control_center_query.py",
)


def test_legacy_control_center_flag_is_fail_closed_before_runtime_imports(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original_import = builtins.__import__
    forbidden_runtime_imports = (
        "PySide6",
        "bdb_gui.bootstrap",
        "bdb_gui.operations",
        "bdb_gui.session_history_window",
        "bdb_gui.tray",
        "bdb_gui.current_operation",
        "bdb_gui.dashboard",
        "bdb_bridge",
    )

    def guarded_import(name: str, *args: object, **kwargs: object) -> object:
        if name == forbidden_runtime_imports or name.startswith(forbidden_runtime_imports):
            raise AssertionError(f"CC2 retired route attempted runtime import: {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    exit_code = app.main(["--legacy-control-center"])
    report = json.loads(capsys.readouterr().out)

    assert exit_code == 3
    assert report["status"] == "failed"
    assert report["error_code"] == app.LEGACY_CONTROL_CENTER_RETIRED_CODE
    assert report["legacy_control_center"] is True
    assert report["legacy_active_interpretation"] is False
    assert report["legacy_fallback"] is False
    assert report["archive_only"] is True
    assert report["read_only"] is True
    assert report["mutation_operations_invoked"] == 0
    assert report["vnext_activation_allowed"] is False


def test_active_control_center_sources_have_no_legacy_runtime_semantics() -> None:
    forbidden = (
        "ObservabilityReader",
        "OperatorApi",
        "from bdb_bridge",
        "import bdb_bridge",
        "_legacy_window",
        "SessionProjectControlCenterWindow",
        "SessionTrayProjectControlCenterWindow",
        "ProjectOperationsService",
        "BootstrapService",
        "TrayController",
    )
    for path in ACTIVE_CONTROL_CENTER_SOURCES:
        source = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in source, f"active CC2 source {path} contains retired semantic: {token}"


def test_canonical_off_projection_is_explicitly_no_legacy_fallback(tmp_path: Path) -> None:
    document = _off_snapshot(tmp_path).as_dict()

    assert document["status_vector"] == {
        "system": "OFF",
        "writer": "OFF",
        "activation": "OFF",
        "control_store": "ABSENT",
    }
    assert document["legacy_fallback"] is False
    assert document["mutation_operations_invoked"] == 0
    assert document["read_only"] is True
    assert all(action["enabled"] is False for action in document["actions"])


def test_query_failure_path_is_vnext_degraded_without_legacy_fallback() -> None:
    source = (REPOSITORY_ROOT / "bdb_gui" / "vnext_control_center.py").read_text(encoding="utf-8")

    assert '"legacy_fallback": False' in source
    assert "DEGRADED" in source
    assert "ObservabilityReader" not in source
    assert "OperatorApi" not in source
    assert "bdb_bridge" not in source


def test_cc2_retirement_does_not_physically_delete_legacy_history_code() -> None:
    # CC2 removes active interpretation, not historical source bytes. Physical
    # cleanup belongs to later cutover/cleanup milestones.
    assert (REPOSITORY_ROOT / "bdb_gui" / "current_operation.py").is_file()
    assert (REPOSITORY_ROOT / "bdb_gui" / "dashboard.py").is_file()

    entrypoint = (REPOSITORY_ROOT / "bdb_gui" / "app.py").read_text(encoding="utf-8")
    assert "current_operation" not in entrypoint
    assert "dashboard" not in entrypoint


def test_cc2_tombstone_cannot_enable_vnext() -> None:
    report = app._retired_legacy_report()
    snapshot = _off_snapshot(REPOSITORY_ROOT / "does-not-exist")

    assert report["vnext_activation_allowed"] is False
    assert snapshot.writer_state == "OFF"
    assert snapshot.activation_state == "OFF"
    assert all(predicate.enabled is False for predicate in snapshot.action_predicates)

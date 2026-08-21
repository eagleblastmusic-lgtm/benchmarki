from __future__ import annotations

import os
from pathlib import Path

import pytest

from bdb_vnext.control_center_query import (
    ActionPredicate,
    ControlCenterQueryError,
    ControlCenterSnapshot,
    read_control_center_authority_summary,
)


def _snapshot() -> ControlCenterSnapshot:
    return ControlCenterSnapshot(
        "C:/cc3-runtime",
        "OFF",
        "OFF",
        "OFF",
        "SEALED",
        "cc3-store",
        (),
        (ActionPredicate("resume"), ActionPredicate("publish")),
        "cc3_build_only",
        authority_summary={
            "schema": "bdb-vnext-cc3-authority-summary-v1",
            "bootstrap": {"status": "ACTIVE", "state_sha256": "sha256:bootstrap"},
            "m9b": {"status": "ACTIVE", "writer_enabled": True, "intake_enabled": True},
            "m3c": {"status": "CANONICAL", "admission_enabled": True},
            "native_route": {"target_registered": True, "target_conflict": False, "legacy_route_present": False},
            "production_acceptance": {"status": "PASS", "value": True},
            "warnings": [],
            "legacy_fallback": False,
        },
    )


def test_authority_summary_missing_root_is_explicit_and_non_mutating(tmp_path: Path) -> None:
    root = tmp_path / "cc3-runtime"
    summary = read_control_center_authority_summary(root)
    assert summary["schema"] == "bdb-vnext-cc3-authority-summary-v1"
    assert summary["bootstrap"]["status"] == "UNAVAILABLE"
    assert summary["legacy_fallback"] is False
    assert not root.exists()


def test_cc3_shell_pages_dashboard_and_disabled_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from bdb_gui.vnext_control_center import PAGE_NAMES, VNextControlCenterWindow

    application = QApplication.instance() or QApplication(["cc3-test"])
    window = VNextControlCenterWindow(runtime_root="C:/cc3-runtime", snapshot_loader=lambda _root: _snapshot())
    window.start_bootstrap()
    report = window.smoke_report()
    assert tuple(report["page_names"]) == PAGE_NAMES
    assert report["page_count"] == 6
    assert report["actions_enabled"] is False
    assert report["mutation_operations_invoked"] == 0
    assert report["authority_summary"]["production_acceptance"]["status"] == "PASS"
    window.select_page("Diagnostics")
    assert window.smoke_report()["selected_page"] == "Diagnostics"
    assert window._pages.currentIndex() == PAGE_NAMES.index("Diagnostics")
    window.close()
    application.processEvents()


def test_cc3_degraded_query_has_no_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from bdb_gui.vnext_control_center import VNextControlCenterWindow

    application = QApplication.instance() or QApplication(["cc3-degraded-test"])

    def fail(_root: object) -> ControlCenterSnapshot:
        raise ControlCenterQueryError("cc3_test_degraded", "synthetic degraded state")

    window = VNextControlCenterWindow(snapshot_loader=fail)
    window.start_bootstrap()
    report = window.smoke_report()
    assert report["bootstrap_ok"] is False
    assert report["bootstrap_error_code"] == "cc3_test_degraded"
    assert report["legacy_fallback"] is False
    assert report["actions_enabled"] is False
    window.close()
    application.processEvents()


def test_cc3_active_source_has_no_legacy_semantic_imports() -> None:
    source = Path(__file__).resolve().parents[1] / "bdb_gui" / "vnext_control_center.py"
    text = source.read_text(encoding="utf-8")
    assert "bdb_operator" not in text
    assert "bdb_bridge" not in text
    assert '"legacy_fallback": False' in text

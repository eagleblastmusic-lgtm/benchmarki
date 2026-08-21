"""PySide6 Control Center shell over the canonical vNext CC1 projection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QAbstractItemView, QHBoxLayout, QLabel, QMainWindow, QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget
from bdb_vnext.control_center_query import ControlCenterQueryError, ControlCenterSnapshot, read_control_center_snapshot

SnapshotLoader = Callable[[str | Path | None], ControlCenterSnapshot]

class VNextControlCenterWindow(QMainWindow):
    dashboard_ready = Signal()
    def __init__(self, *, runtime_root: str | Path | None = None, snapshot_loader: SnapshotLoader = read_control_center_snapshot) -> None:
        super().__init__(); self.setObjectName("BdbControlCenterWindow"); self.setWindowTitle("BDB Control Center — vNext"); self.resize(1180, 760)
        self._runtime_root = runtime_root; self._snapshot_loader = snapshot_loader; self._snapshot: ControlCenterSnapshot | None = None; self._bootstrap_completed = False; self._bootstrap_ok = False; self._bootstrap_error_code: str | None = None
        host = QWidget(self); layout = QVBoxLayout(host); top = QHBoxLayout(); self._status = QLabel("vNext: loading"); self._status.setObjectName("VNextSystemStatus"); self._refresh = QPushButton("Refresh"); self._refresh.setObjectName("VNextRefreshButton"); self._refresh.setToolTip("Read a fresh canonical projection; no mutation or resume is performed."); self._refresh.clicked.connect(self.start_bootstrap); top.addWidget(self._status); top.addStretch(1); top.addWidget(self._refresh); layout.addLayout(top)
        split = QSplitter(); self._works = QTableWidget(0, 5); self._works.setHorizontalHeaderLabels(("Work", "Task", "Kind", "Disposition", "Version")); self._works.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows); self._works.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers); self._works.itemSelectionChanged.connect(self._render_selection); split.addWidget(self._works)
        self._detail = QTextEdit(); self._detail.setReadOnly(True); self._detail.setObjectName("VNextProjectionDetail"); split.addWidget(self._detail); split.setStretchFactor(0, 2); split.setStretchFactor(1, 3); layout.addWidget(split, 1)
        actions = QHBoxLayout(); self._action_buttons: list[QPushButton] = []
        for label in ("Resume", "Apply effect", "Publish", "Activate"):
            button = QPushButton(label); button.setEnabled(False); button.setToolTip("Unavailable: cc1_read_only"); actions.addWidget(button); self._action_buttons.append(button)
        actions.addStretch(1); layout.addLayout(actions); self.setCentralWidget(host)
    def start_bootstrap(self) -> None:
        self._bootstrap_completed = False; self._bootstrap_ok = False; self._bootstrap_error_code = None
        try: snapshot = self._snapshot_loader(self._runtime_root)
        except ControlCenterQueryError as exc:
            self._snapshot = None; self._bootstrap_completed = True; self._bootstrap_error_code = exc.code; self._status.setText(f"vNext: DEGRADED — {exc.code}"); self._works.setRowCount(0); self._detail.setPlainText(json.dumps({"status": "DEGRADED", "error": {"code": exc.code, "message": str(exc)}, "read_only": True, "legacy_fallback": False}, sort_keys=True, indent=2)); self.dashboard_ready.emit(); return
        self._snapshot = snapshot; self._bootstrap_completed = True; self._bootstrap_ok = True; self._status.setText("vNext: " f"{snapshot.system_state} | writer={snapshot.writer_state} | " f"activation={snapshot.activation_state} | store={snapshot.store_state}"); self._render_snapshot(snapshot); self.dashboard_ready.emit()
    def _render_snapshot(self, snapshot: ControlCenterSnapshot) -> None:
        self._works.setRowCount(len(snapshot.works))
        for index, projection in enumerate(snapshot.works):
            work = projection.work_record; values = (projection.work_id, projection.task_id, str(work.get("kind", "N/A")), str(work.get("disposition", "N/A")), str(work.get("state_version", "N/A")))
            for column, value in enumerate(values): self._works.setItem(index, column, QTableWidgetItem(value))
        self._works.resizeColumnsToContents()
        if snapshot.works: self._works.selectRow(0)
        else: self._detail.setPlainText(json.dumps(snapshot.as_dict(), sort_keys=True, indent=2, default=str))
    def _render_selection(self) -> None:
        if self._snapshot is None: return
        row = self._works.currentRow()
        if row < 0 or row >= len(self._snapshot.works): return
        document = self._snapshot.works[row].as_dict(); document["actions"] = [item.as_dict() for item in self._snapshot.action_predicates]; self._detail.setPlainText(json.dumps(document, sort_keys=True, indent=2, default=str))
    def smoke_report(self) -> dict[str, object]:
        snapshot = self._snapshot
        return {"window_object_name": self.objectName(), "window_constructed": True, "read_only_startup": True, "bootstrap_completed": self._bootstrap_completed, "bootstrap_ok": self._bootstrap_ok, "bootstrap_error_code": self._bootstrap_error_code, "semantic_source": "bdb_vnext.control_center_query", "legacy_fallback": False, "work_count": len(snapshot.works) if snapshot is not None else 0, "project_count": 0, "status_vector": ({"system": snapshot.system_state, "writer": snapshot.writer_state, "activation": snapshot.activation_state, "control_store": snapshot.store_state} if snapshot is not None else {"system": "DEGRADED"}), "actions_enabled": any(button.isEnabled() for button in self._action_buttons), "mutation_operations_invoked": 0, "auto_resume_invoked": False, "operator_network_listener": None}

__all__ = ["VNextControlCenterWindow"]


# CC3 replaces the earlier CC1 table shell at import time.  The old class is
# intentionally retained above as historical source material; this concrete
# class is the only exported/active GUI implementation.
from PySide6.QtCore import Qt as _Qt
from PySide6.QtWidgets import (
    QFrame as _QFrame,
    QGridLayout as _QGridLayout,
    QListWidget as _QListWidget,
    QListWidgetItem as _QListWidgetItem,
    QStackedWidget as _QStackedWidget,
)

from .style import CONTROL_CENTER_STYLESHEET as _CONTROL_CENTER_STYLESHEET

PAGE_NAMES = (
    "Dashboard",
    "Projects",
    "Current operation",
    "History",
    "Diagnostics",
    "Settings / System",
)


class _Cc3StatusCard(_QFrame):
    def __init__(self, title: str, object_name: str) -> None:
        super().__init__()
        self.setObjectName(object_name)
        layout = QVBoxLayout(self)
        self.title = QLabel(title)
        self.title.setObjectName(f"{object_name}Title")
        self.value = QLabel("Ładowanie")
        self.value.setObjectName(f"{object_name}Value")
        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        self.detail.setObjectName(f"{object_name}Detail")
        layout.addWidget(self.title)
        layout.addWidget(self.value)
        layout.addWidget(self.detail)

    def update(self, value: object, detail: object = "") -> None:
        self.value.setText(str(value))
        self.detail.setText(str(detail))


class VNextControlCenterWindow(QMainWindow):
    """Final CC3 navigation shell; all pages are canonical and read-only."""

    dashboard_ready = Signal()

    def __init__(self, *, runtime_root: str | Path | None = None, snapshot_loader: SnapshotLoader = read_control_center_snapshot) -> None:
        super().__init__()
        self.setObjectName("BdbControlCenterWindow")
        self.setWindowTitle("BDB Control Center — vNext")
        self.resize(1280, 820)
        self.setStyleSheet(_CONTROL_CENTER_STYLESHEET)
        self._runtime_root = runtime_root
        self._snapshot_loader = snapshot_loader
        self._snapshot: ControlCenterSnapshot | None = None
        self._bootstrap_completed = False
        self._bootstrap_ok = False
        self._bootstrap_error_code: str | None = None
        self._mutation_operations_invoked = 0
        self._page_names = PAGE_NAMES
        self._build_cc3_shell()

    def _build_cc3_shell(self) -> None:
        host = QWidget(self)
        root = QVBoxLayout(host)
        top = QHBoxLayout()
        self._status = QLabel("vNext: loading")
        self._status.setObjectName("VNextSystemStatus")
        self._refresh = QPushButton("Refresh")
        self._refresh.setObjectName("VNextRefreshButton")
        self._refresh.setToolTip("Odczytaj świeży canonical snapshot; bez mutacji.")
        self._refresh.clicked.connect(self.start_bootstrap)
        top.addWidget(self._status)
        top.addStretch(1)
        top.addWidget(self._refresh)
        root.addLayout(top)
        body = QHBoxLayout()
        self._sidebar = _QListWidget()
        self._sidebar.setObjectName("VNextSidebar")
        self._sidebar.setAccessibleName("Control Center navigation")
        self._sidebar.setFixedWidth(215)
        for name in self._page_names:
            item = _QListWidgetItem(name)
            item.setData(_Qt.ItemDataRole.UserRole, name)
            self._sidebar.addItem(item)
        self._sidebar.currentRowChanged.connect(self._select_page_index)
        body.addWidget(self._sidebar)
        self._pages = _QStackedWidget()
        self._pages.setObjectName("VNextPages")
        self._dashboard_page = self._make_dashboard_page()
        self._projects_page = self._make_projects_page()
        self._operation_page = self._make_text_page("Current operation — read-only", "VNextOperationDetail")
        self._history_page = self._make_text_page("History — canonical facts", "VNextHistoryDetail")
        self._diagnostics_page = self._make_text_page("Diagnostics — read-only", "VNextDiagnosticsDetail")
        self._settings_page = self._make_text_page("Settings / System — identity only", "VNextSettingsDetail")
        for page in (self._dashboard_page, self._projects_page, self._operation_page, self._history_page, self._diagnostics_page, self._settings_page):
            self._pages.addWidget(page)
        body.addWidget(self._pages, 1)
        root.addLayout(body, 1)
        actions = QHBoxLayout()
        self._action_buttons: list[QPushButton] = []
        for label, reason in (("Resume", "cc3_read_only_resume_unavailable"), ("Apply effect", "cc3_read_only_apply_unavailable"), ("Publish", "cc3_read_only_publish_unavailable"), ("Activate", "cc3_read_only_activate_unavailable")):
            button = QPushButton(label)
            button.setEnabled(False)
            button.setProperty("reason_code", reason)
            button.setToolTip(f"Unavailable: {reason}")
            self._action_buttons.append(button)
            actions.addWidget(button)
        actions.addStretch(1)
        root.addLayout(actions)
        self.setCentralWidget(host)
        self._sidebar.setCurrentRow(0)

    def _make_text_page(self, title: str, object_name: str) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        heading = QLabel(title)
        heading.setObjectName(f"{object_name}Heading")
        layout.addWidget(heading)
        editor = QTextEdit()
        editor.setObjectName(object_name)
        editor.setReadOnly(True)
        layout.addWidget(editor, 1)
        attribute = {
            "VNextOperationDetail": "_current_operation",
            "VNextHistoryDetail": "_history",
            "VNextDiagnosticsDetail": "_diagnostics",
            "VNextSettingsDetail": "_settings",
        }.get(object_name, f"_{object_name.lower()}")
        setattr(self, attribute, editor)
        return page

    def _make_dashboard_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        heading = QLabel("Dashboard")
        heading.setObjectName("VNextDashboardHeading")
        layout.addWidget(heading)
        self._dashboard_cards: dict[str, _Cc3StatusCard] = {}
        grid = _QGridLayout()
        for index, (key, title) in enumerate((("system", "System state"), ("store", "Control Store"), ("bootstrap", "Bootstrap ACTIVE"), ("source", "Current source"), ("m9b", "M9b"), ("writer", "Writer"), ("intake", "Intake"), ("m3c", "M3c admission"), ("native_route", "Native route"), ("production", "Production acceptance"), ("operation", "Current operation"), ("warning", "Warnings"))):
            card = _Cc3StatusCard(title, f"VNextCard_{key}")
            self._dashboard_cards[key] = card
            grid.addWidget(card, index // 3, index % 3)
        layout.addLayout(grid)
        self._dashboard_empty = QLabel("Oczekiwanie na canonical snapshot…")
        self._dashboard_empty.setObjectName("VNextDashboardEmpty")
        self._dashboard_empty.setWordWrap(True)
        layout.addWidget(self._dashboard_empty)
        layout.addStretch(1)
        return page

    def _make_projects_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        heading = QLabel("Projects — read-only")
        heading.setObjectName("VNextProjectsHeading")
        layout.addWidget(heading)
        self._works = QTableWidget(0, 5)
        self._works.setObjectName("VNextProjectsTable")
        self._works.setHorizontalHeaderLabels(("Work", "Task", "Kind", "Disposition", "Version"))
        self._works.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._works.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._works.itemSelectionChanged.connect(self._render_selection)
        layout.addWidget(self._works, 1)
        self._projects_empty = QLabel("Brak canonical WorkItemów.")
        self._projects_empty.setObjectName("VNextProjectsEmpty")
        layout.addWidget(self._projects_empty)
        return page

    def _select_page_index(self, index: int) -> None:
        if 0 <= index < self._pages.count():
            self._pages.setCurrentIndex(index)

    def select_page(self, name: str) -> None:
        if name not in self._page_names:
            raise ValueError(f"unknown CC3 page: {name}")
        self._sidebar.setCurrentRow(self._page_names.index(name))

    @staticmethod
    def _section(snapshot: ControlCenterSnapshot, key: str) -> dict[str, Any]:
        value = snapshot.authority_summary.get(key, {}) if snapshot.authority_summary else {}
        return dict(value) if isinstance(value, dict) else {}

    @staticmethod
    def _short(value: object, length: int = 18) -> str:
        text = str(value)
        return text if len(text) <= length else f"{text[:length]}…"

    def _render_dashboard(self, snapshot: ControlCenterSnapshot) -> None:
        bootstrap = self._section(snapshot, "bootstrap")
        m9b = self._section(snapshot, "m9b")
        m3c = self._section(snapshot, "m3c")
        route = self._section(snapshot, "native_route")
        production = self._section(snapshot, "production_acceptance")
        current_operation = str(snapshot.works[0].work_record.get("disposition", "UNKNOWN")) if snapshot.works else "EMPTY"
        self._dashboard_cards["system"].update(snapshot.system_state, snapshot.reason_code or "canonical snapshot")
        self._dashboard_cards["store"].update(snapshot.store_state, snapshot.store_instance_id or "no instance id")
        self._dashboard_cards["bootstrap"].update(bootstrap.get("status", "UNAVAILABLE"), bootstrap.get("state_sha256", bootstrap.get("reason_code", "")))
        self._dashboard_cards["source"].update(self._short(bootstrap.get("source_commit", "UNAVAILABLE")), bootstrap.get("source_tree", ""))
        self._dashboard_cards["m9b"].update(m9b.get("status", "UNAVAILABLE"), m9b.get("record_digest", m9b.get("reason_code", "")))
        self._dashboard_cards["writer"].update("ON" if m9b.get("writer_enabled") is True else "OFF/UNKNOWN", "canonical M9b")
        self._dashboard_cards["intake"].update("ON" if m9b.get("intake_enabled") is True else "OFF/UNKNOWN", "canonical M9b")
        self._dashboard_cards["m3c"].update(m3c.get("status", "UNAVAILABLE"), str(m3c.get("admission_enabled", m3c.get("reason_code", ""))))
        route_value = "READY" if route.get("target_registered") is True and route.get("target_conflict") is False else route.get("status", "DEGRADED")
        self._dashboard_cards["native_route"].update(route_value, "Legacy absent" if route.get("legacy_route_present") is False else "Legacy/route warning")
        self._dashboard_cards["production"].update(production.get("status", "UNAVAILABLE"), str(production.get("value", production.get("reason_code", ""))))
        self._dashboard_cards["operation"].update(current_operation, f"{len(snapshot.works)} canonical WorkItem(s)")
        warnings = list(snapshot.authority_summary.get("warnings", [])) if snapshot.authority_summary else []
        self._dashboard_cards["warning"].update("NONE" if not warnings else "DEGRADED", ", ".join(map(str, warnings)) or "No warnings")
        self._dashboard_empty.setText("Canonical snapshot loaded. Actions remain disabled in CC3 read-only slice.")

    def start_bootstrap(self) -> None:
        self._bootstrap_completed = False
        self._bootstrap_ok = False
        self._bootstrap_error_code = None
        self._status.setText("vNext: loading canonical snapshot…")
        try:
            snapshot = self._snapshot_loader(self._runtime_root)
        except ControlCenterQueryError as exc:
            self._snapshot = None
            self._bootstrap_completed = True
            self._bootstrap_error_code = exc.code
            self._status.setText(f"vNext: DEGRADED — {exc.code}")
            self._works.setRowCount(0)
            self._projects_empty.setText(f"Brak projekcji: {exc.code}")
            rendered = json.dumps({"status": "DEGRADED", "error": {"code": exc.code, "message": str(exc)}, "read_only": True, "legacy_fallback": False}, sort_keys=True, indent=2)
            self._current_operation.setPlainText(rendered)
            self._history.setPlainText(rendered)
            self._diagnostics.setPlainText(rendered)
            self._settings.setPlainText(rendered)
            self._dashboard_empty.setText(f"Stan zdegradowany: {exc.code}")
            self.dashboard_ready.emit()
            return
        self._snapshot = snapshot
        self._bootstrap_completed = True
        self._bootstrap_ok = True
        self._status.setText(f"vNext: {snapshot.system_state} | store={snapshot.store_state} | read-only")
        self._render_snapshot(snapshot)
        self.dashboard_ready.emit()

    def _render_snapshot(self, snapshot: ControlCenterSnapshot) -> None:
        self._render_dashboard(snapshot)
        self._works.setRowCount(len(snapshot.works))
        for index, projection in enumerate(snapshot.works):
            work = projection.work_record
            for column, value in enumerate((projection.work_id, projection.task_id, str(work.get("kind", "N/A")), str(work.get("disposition", "N/A")), str(work.get("state_version", "N/A")))):
                self._works.setItem(index, column, QTableWidgetItem(value))
        self._works.resizeColumnsToContents()
        self._projects_empty.setVisible(not bool(snapshot.works))
        if snapshot.works:
            self._works.selectRow(0)
        else:
            self._current_operation.setPlainText(json.dumps({"status": "EMPTY", "message": "Brak canonical WorkItemów.", "read_only": True, "legacy_fallback": False}, sort_keys=True, indent=2))
        self._render_history(snapshot)
        self._render_diagnostics(snapshot)
        self._render_settings(snapshot)

    def _render_selection(self) -> None:
        if self._snapshot is None:
            return
        row = self._works.currentRow()
        if 0 <= row < len(self._snapshot.works):
            document = self._snapshot.works[row].as_dict()
            document["actions"] = [item.as_dict() for item in self._snapshot.action_predicates]
            self._current_operation.setPlainText(json.dumps(document, sort_keys=True, indent=2, default=str))

    def _render_history(self, snapshot: ControlCenterSnapshot) -> None:
        facts: list[dict[str, Any]] = []
        for projection in snapshot.works:
            recent = projection.work_record.get("recent_facts", [])
            if isinstance(recent, list):
                facts.extend(item for item in recent if isinstance(item, dict))
        self._history.setPlainText(json.dumps({"status": "EMPTY" if not facts else "READY", "facts": facts, "read_only": True}, sort_keys=True, indent=2, default=str))

    def _render_diagnostics(self, snapshot: ControlCenterSnapshot) -> None:
        self._diagnostics.setPlainText(json.dumps({"schema": snapshot.schema, "authority": snapshot.authority, "generation": snapshot.generation, "reason_code": snapshot.reason_code, "authority_summary": dict(snapshot.authority_summary), "read_only": True, "legacy_fallback": False}, sort_keys=True, indent=2, default=str))

    def _render_settings(self, snapshot: ControlCenterSnapshot) -> None:
        self._settings.setPlainText(json.dumps({"runtime_root": snapshot.runtime_root, "generation": snapshot.generation, "query_authority": snapshot.authority, "query_schema": snapshot.schema, "actions": [item.as_dict() for item in snapshot.action_predicates], "read_only": True, "legacy_fallback": False}, sort_keys=True, indent=2, default=str))

    def smoke_report(self) -> dict[str, object]:
        snapshot = self._snapshot
        return {"window_object_name": self.objectName(), "window_constructed": True, "read_only_startup": True, "bootstrap_completed": self._bootstrap_completed, "bootstrap_ok": self._bootstrap_ok, "bootstrap_error_code": self._bootstrap_error_code, "semantic_source": "bdb_vnext.control_center_query", "legacy_fallback": False, "page_names": list(self._page_names), "page_count": self._pages.count(), "selected_page": self._page_names[self._pages.currentIndex()], "work_count": len(snapshot.works) if snapshot is not None else 0, "project_count": len(snapshot.works) if snapshot is not None else 0, "status_vector": ({"system": snapshot.system_state, "writer": snapshot.writer_state, "activation": snapshot.activation_state, "control_store": snapshot.store_state} if snapshot is not None else {"system": "DEGRADED"}), "authority_summary": dict(snapshot.authority_summary) if snapshot is not None else {}, "actions_enabled": any(button.isEnabled() for button in self._action_buttons), "action_reason_codes": [str(button.property("reason_code")) for button in self._action_buttons], "mutation_operations_invoked": self._mutation_operations_invoked, "auto_resume_invoked": False, "operator_network_listener": None}


__all__ = ["PAGE_NAMES", "VNextControlCenterWindow"]

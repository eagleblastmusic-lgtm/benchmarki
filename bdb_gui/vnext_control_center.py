"""PySide6 Control Center shell over the canonical vNext CC1 projection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from bdb_vnext.control_center_query import (
    ControlCenterQueryError,
    ControlCenterSnapshot,
    read_control_center_snapshot,
)


SnapshotLoader = Callable[[str | Path | None], ControlCenterSnapshot]


class VNextControlCenterWindow(QMainWindow):
    """Thin, read-only CC1 UI.  It owns no domain state or lifecycle decisions."""

    dashboard_ready = Signal()

    def __init__(
        self,
        *,
        runtime_root: str | Path | None = None,
        snapshot_loader: SnapshotLoader = read_control_center_snapshot,
    ) -> None:
        super().__init__()
        self.setObjectName("BdbControlCenterWindow")
        self.setWindowTitle("BDB Control Center — vNext")
        self.resize(1180, 760)
        self._runtime_root = runtime_root
        self._snapshot_loader = snapshot_loader
        self._snapshot: ControlCenterSnapshot | None = None
        self._bootstrap_completed = False
        self._bootstrap_ok = False
        self._bootstrap_error_code: str | None = None

        host = QWidget(self)
        layout = QVBoxLayout(host)

        top = QHBoxLayout()
        self._status = QLabel("vNext: loading")
        self._status.setObjectName("VNextSystemStatus")
        self._refresh = QPushButton("Refresh")
        self._refresh.setObjectName("VNextRefreshButton")
        self._refresh.setToolTip("Read a fresh canonical projection; no mutation or resume is performed.")
        self._refresh.clicked.connect(self.start_bootstrap)
        top.addWidget(self._status)
        top.addStretch(1)
        top.addWidget(self._refresh)
        layout.addLayout(top)

        split = QSplitter()
        self._works = QTableWidget(0, 5)
        self._works.setHorizontalHeaderLabels(("Work", "Task", "Kind", "Disposition", "Version"))
        self._works.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._works.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._works.itemSelectionChanged.connect(self._render_selection)
        split.addWidget(self._works)

        self._detail = QTextEdit()
        self._detail.setReadOnly(True)
        self._detail.setObjectName("VNextProjectionDetail")
        split.addWidget(self._detail)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        layout.addWidget(split, 1)

        actions = QHBoxLayout()
        self._action_buttons: list[QPushButton] = []
        for label in ("Resume", "Apply effect", "Publish", "Activate"):
            button = QPushButton(label)
            button.setEnabled(False)
            button.setToolTip("Unavailable: cc1_read_only")
            actions.addWidget(button)
            self._action_buttons.append(button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.setCentralWidget(host)

    def start_bootstrap(self) -> None:
        """Load exactly one read-only projection; never auto-resume or fallback."""

        self._bootstrap_completed = False
        self._bootstrap_ok = False
        self._bootstrap_error_code = None
        try:
            snapshot = self._snapshot_loader(self._runtime_root)
        except ControlCenterQueryError as exc:
            self._snapshot = None
            self._bootstrap_completed = True
            self._bootstrap_error_code = exc.code
            self._status.setText(f"vNext: DEGRADED — {exc.code}")
            self._works.setRowCount(0)
            self._detail.setPlainText(
                json.dumps(
                    {
                        "status": "DEGRADED",
                        "error": {"code": exc.code, "message": str(exc)},
                        "read_only": True,
                        "legacy_fallback": False,
                    },
                    sort_keys=True,
                    indent=2,
                )
            )
            self.dashboard_ready.emit()
            return

        self._snapshot = snapshot
        self._bootstrap_completed = True
        self._bootstrap_ok = True
        self._status.setText(
            "vNext: "
            f"{snapshot.system_state} | writer={snapshot.writer_state} | "
            f"activation={snapshot.activation_state} | store={snapshot.store_state}"
        )
        self._render_snapshot(snapshot)
        self.dashboard_ready.emit()

    def _render_snapshot(self, snapshot: ControlCenterSnapshot) -> None:
        self._works.setRowCount(len(snapshot.works))
        for index, projection in enumerate(snapshot.works):
            work = projection.work
            values = (
                projection.work_id,
                projection.task_id,
                str(work.get("kind", "N/A")),
                str(work.get("disposition", "N/A")),
                str(work.get("state_version", "N/A")),
            )
            for column, value in enumerate(values):
                self._works.setItem(index, column, QTableWidgetItem(value))
        self._works.resizeColumnsToContents()
        if snapshot.works:
            self._works.selectRow(0)
        else:
            self._detail.setPlainText(
                json.dumps(snapshot.as_dict(), sort_keys=True, indent=2, default=str)
            )

    def _render_selection(self) -> None:
        if self._snapshot is None:
            return
        row = self._works.currentRow()
        if row < 0 or row >= len(self._snapshot.works):
            return
        document = self._snapshot.works[row].as_dict()
        document["actions"] = [item.as_dict() for item in self._snapshot.action_predicates]
        self._detail.setPlainText(json.dumps(document, sort_keys=True, indent=2, default=str))

    def smoke_report(self) -> dict[str, object]:
        snapshot = self._snapshot
        return {
            "window_object_name": self.objectName(),
            "window_constructed": True,
            "read_only_startup": True,
            "bootstrap_completed": self._bootstrap_completed,
            "bootstrap_ok": self._bootstrap_ok,
            "bootstrap_error_code": self._bootstrap_error_code,
            "semantic_source": "bdb_vnext.control_center_query",
            "legacy_fallback": False,
            "work_count": len(snapshot.works) if snapshot is not None else 0,
            "project_count": 0,
            "status_vector": (
                {
                    "system": snapshot.system_state,
                    "writer": snapshot.writer_state,
                    "activation": snapshot.activation_state,
                    "control_store": snapshot.store_state,
                }
                if snapshot is not None
                else {"system": "DEGRADED"}
            ),
            "actions_enabled": any(button.isEnabled() for button in self._action_buttons),
            "mutation_operations_invoked": 0,
            "auto_resume_invoked": False,
            "operator_network_listener": None,
        }


__all__ = ["VNextControlCenterWindow"]

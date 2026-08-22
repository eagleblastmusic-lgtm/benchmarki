"""Project-centric CC3 Slice 2 GUI.

The start/projects/current-project surface is intentionally simple.  The
technical CC3 window remains available as an explicit Advanced view.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QInputDialog,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from bdb_vnext.control_center_query import ControlCenterQueryError, ControlCenterSnapshot, read_control_center_snapshot
from bdb_vnext.project_catalog import ProjectBrief, ProjectCatalog, ProjectCatalogError, ProjectRecord
from bdb_vnext.project_workflow import ProjectWorkflow, ProjectWorkflowError
from bdb_vnext.project_memory import HANDOFF_MODES, ProjectMemoryError, bounded_history_summary, project_health, project_status_sentence, resolve_next_action

from .style import CONTROL_CENTER_STYLESHEET
from .vnext_control_center import VNextControlCenterWindow


PROJECT_PAGE_NAMES = ("Start", "My projects", "Current project", "Advanced")
_ALIAS_RE = re.compile(r"[^a-z0-9-]+")


def slugify_project_alias(value: str) -> str:
    slug = _ALIAS_RE.sub("-", value.strip().casefold()).strip("-")
    slug = slug[:32].strip("-")
    return slug if slug and slug[0].isalpha() else "project"


def validate_wizard_payload(payload: dict[str, Any]) -> ProjectBrief:
    """Validate the seven bounded wizard answers without performing I/O."""

    return ProjectBrief(
        name=str(payload.get("name", "")).strip(),
        goal=str(payload.get("goal", "")).strip(),
        description=str(payload.get("description", "")).strip(),
        project_type=str(payload.get("project_type", "")).strip(),
        technologies=tuple(item.strip() for item in str(payload.get("technologies", "")).split(",") if item.strip()),
        features=tuple(item.strip() for item in str(payload.get("features", "")).splitlines() if item.strip()),
        constraints=tuple(item.strip() for item in str(payload.get("constraints", "")).splitlines() if item.strip()),
    )


class _NewProjectDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Nowy projekt")
        self.setObjectName("NewProjectWizard")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(); self.name.setObjectName("ProjectNameInput")
        self.goal = QLineEdit(); self.goal.setObjectName("ProjectGoalInput")
        self.description = QTextEdit(); self.description.setObjectName("ProjectDescriptionInput"); self.description.setMaximumHeight(90)
        self.project_type = QComboBox(); self.project_type.setObjectName("ProjectTypeInput"); self.project_type.addItems(("aplikacja desktopowa", "web", "CLI", "rozszerzenie", "biblioteka", "inny", "jeszcze nie wiem"))
        self.technologies = QLineEdit(); self.technologies.setObjectName("ProjectTechnologiesInput")
        self.features = QTextEdit(); self.features.setObjectName("ProjectFeaturesInput"); self.features.setMaximumHeight(90)
        self.constraints = QTextEdit(); self.constraints.setObjectName("ProjectConstraintsInput"); self.constraints.setMaximumHeight(90)
        form.addRow("Nazwa projektu", self.name); form.addRow("Co chcesz zbudować?", self.goal); form.addRow("Krótki opis", self.description); form.addRow("Typ projektu", self.project_type); form.addRow("Preferowane technologie", self.technologies); form.addRow("Najważniejsze funkcje", self.features); form.addRow("Wymagania / ograniczenia", self.constraints)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept_if_valid); buttons.rejected.connect(self.reject); layout.addWidget(buttons)
        self.validation = QLabel(""); self.validation.setObjectName("ProjectWizardValidation"); self.validation.setWordWrap(True); layout.addWidget(self.validation)

    def payload(self) -> dict[str, Any]:
        return {"name": self.name.text(), "goal": self.goal.text(), "description": self.description.toPlainText(), "project_type": self.project_type.currentText(), "technologies": self.technologies.text(), "features": self.features.toPlainText(), "constraints": self.constraints.toPlainText()}

    def brief(self) -> ProjectBrief:
        return validate_wizard_payload(self.payload())

    def _accept_if_valid(self) -> None:
        try:
            self.brief()
        except ProjectCatalogError as exc:
            self.validation.setText(f"Nie można utworzyć projektu: {exc}")
            return
        self.accept()


class ProjectCenterWindow(QMainWindow):
    dashboard_ready = Signal()

    def __init__(self, *, runtime_root: str | Path | None = None, snapshot_loader: Callable[[str | Path | None], ControlCenterSnapshot] = read_control_center_snapshot, catalog: ProjectCatalog | None = None, workflow: ProjectWorkflow | None = None) -> None:
        super().__init__()
        self.setObjectName("BdbControlCenterWindow")
        self.setWindowTitle("Bartosz Dev Bridge")
        self.resize(1280, 820)
        self.setStyleSheet(CONTROL_CENTER_STYLESHEET)
        self._runtime_root = Path(runtime_root).expanduser().absolute() if runtime_root is not None else None
        self._snapshot_loader = snapshot_loader
        self._catalog = catalog or ProjectCatalog(self._runtime_root or Path.home() / "AppData" / "Local" / "BartoszDevBridge-vNext")
        self._workflow = workflow or ProjectWorkflow(self._catalog.runtime_root, catalog=self._catalog)
        self._snapshot: ControlCenterSnapshot | None = None
        self._projects: tuple[ProjectRecord, ...] = ()
        self._current_project_id: str | None = None
        self._advanced: VNextControlCenterWindow | None = None
        self._bootstrap_completed = False
        self._bootstrap_ok = False
        self._bootstrap_error_code: str | None = None
        self._mutation_operations_invoked = 0
        self._pending_plan_preview: Any | None = None
        self._pending_plan_path: str | None = None
        self._build_shell()

    def _build_shell(self) -> None:
        host = QWidget(self); root = QVBoxLayout(host)
        header = QHBoxLayout(); self._status = QLabel("BDB: ładowanie"); self._status.setObjectName("VNextSystemStatus"); header.addWidget(self._status); header.addStretch(1)
        refresh = QPushButton("Odśwież"); refresh.setObjectName("VNextRefreshButton"); refresh.setToolTip("Odczytaj canonical state; bez mutacji"); refresh.clicked.connect(self.start_bootstrap); header.addWidget(refresh); root.addLayout(header)
        body = QHBoxLayout(); self._sidebar = QListWidget(); self._sidebar.setObjectName("ProjectCenterSidebar"); self._sidebar.setFixedWidth(220); self._sidebar.setAccessibleName("Project Center navigation")
        for name in PROJECT_PAGE_NAMES: self._sidebar.addItem(QListWidgetItem(name))
        self._sidebar.currentRowChanged.connect(self._pages_changed); body.addWidget(self._sidebar)
        self._pages = QStackedWidget(); self._pages.setObjectName("ProjectCenterPages"); self._start_page = self._make_start_page(); self._projects_page = self._make_projects_page(); self._current_page = self._make_current_page(); self._advanced_page = self._make_advanced_page()
        for page in (self._start_page, self._projects_page, self._current_page, self._advanced_page): self._pages.addWidget(page)
        body.addWidget(self._pages, 1); root.addLayout(body, 1); self.setCentralWidget(host); self._sidebar.setCurrentRow(0)

    def _make_start_page(self) -> QWidget:
        page = QWidget(); layout = QVBoxLayout(page)
        title = QLabel("Bartosz Dev Bridge"); title.setObjectName("ProjectCenterTitle"); layout.addWidget(title)
        prompt = QLabel("Co chcesz zrobić?"); prompt.setObjectName("ProjectCenterPrompt"); layout.addWidget(prompt)
        buttons = QHBoxLayout(); new_button = QPushButton("＋ Nowy projekt"); new_button.setObjectName("NewProjectButton"); new_button.clicked.connect(self._new_project); open_button = QPushButton("Otwórz istniejący projekt"); open_button.setObjectName("OpenProjectButton"); open_button.clicked.connect(self._open_existing_project); buttons.addWidget(new_button); buttons.addWidget(open_button); buttons.addStretch(1); layout.addLayout(buttons)
        layout.addWidget(QLabel("Ostatnie projekty")); self._recent = QListWidget(); self._recent.setObjectName("RecentProjectsList"); self._recent.itemDoubleClicked.connect(self._open_recent); layout.addWidget(self._recent, 1); return page

    def _make_projects_page(self) -> QWidget:
        page = QWidget(); layout = QVBoxLayout(page); layout.addWidget(QLabel("Moje projekty")); self._project_table = QTableWidget(0, 5); self._project_table.setObjectName("ProjectCatalogTable"); self._project_table.setHorizontalHeaderLabels(("Nazwa", "Status", "Postęp", "Etap", "Zadanie")); self._project_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows); self._project_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers); self._project_table.itemSelectionChanged.connect(self._select_project_row); layout.addWidget(self._project_table, 1); return page

    def _make_current_page(self) -> QWidget:
        page = QWidget(); layout = QVBoxLayout(page); self._project_title = QLabel("Wybierz projekt"); self._project_title.setObjectName("CurrentProjectTitle"); layout.addWidget(self._project_title); self._project_progress = QLabel("Plan nie został zaimportowany."); self._project_progress.setObjectName("CurrentProjectProgress"); layout.addWidget(self._project_progress); self._execution_status = QLabel("Wykonanie: brak aktywnej próby"); self._execution_status.setObjectName("ProjectExecutionStatus"); self._execution_status.setWordWrap(True); layout.addWidget(self._execution_status)
        self._project_detail = QTextEdit(); self._project_detail.setReadOnly(True); self._project_detail.setObjectName("CurrentProjectDetail"); layout.addWidget(self._project_detail, 1)
        self._memory_tabs = QTabWidget(); self._memory_tabs.setObjectName("ProjectMemoryTabs")
        for name, object_name in (("Plan", "ProjectPlanView"), ("Historia", "ProjectHistoryView"), ("Decyzje / Inbox", "ProjectDecisionsInboxView"), ("Ryzyka / dług / checkpointy", "ProjectRisksDebtCheckpointsView")):
            view = QTextEdit(); view.setReadOnly(True); view.setObjectName(object_name); self._memory_tabs.addTab(view, name)
        layout.addWidget(self._memory_tabs, 1)
        actions = QHBoxLayout(); self._import_plan_button = QPushButton("Wczytaj plan"); self._import_plan_button.setObjectName("ImportPlanButton"); self._import_plan_button.clicked.connect(self._import_plan); self._start_button = QPushButton("Rozpocznij w ChatGPT"); self._start_button.setObjectName("StartProjectButton"); self._start_button.clicked.connect(lambda: self._queue_prompt("start")); self._continue_button = QPushButton("Kontynuuj w ChatGPT"); self._continue_button.setObjectName("ContinueProjectButton"); self._continue_button.clicked.connect(lambda: self._queue_prompt("continue")); self._plan_prompt_button = QPushButton("Wstaw prompt planu"); self._plan_prompt_button.setObjectName("PlanPromptButton"); self._plan_prompt_button.clicked.connect(lambda: self._queue_prompt("plan")); self._handoff_mode = QComboBox(); self._handoff_mode.setObjectName("ProjectHandoffMode"); self._handoff_mode.addItems(HANDOFF_MODES); self._handoff_button = QPushButton("Nowa rozmowa / Handoff"); self._handoff_button.setObjectName("ProjectHandoffButton"); self._handoff_button.clicked.connect(self._queue_handoff); self._approve_review_button = QPushButton("Zatwierdź review"); self._approve_review_button.setObjectName("ApproveProjectReviewButton"); self._approve_review_button.clicked.connect(self._approve_review); self._changes_review_button = QPushButton("Wymaga poprawki"); self._changes_review_button.setObjectName("RequestProjectChangesButton"); self._changes_review_button.clicked.connect(self._request_changes); self._project_review_button = QPushButton("Przegląd projektu"); self._project_review_button.setObjectName("ProjectReviewButton"); self._project_review_button.clicked.connect(self._request_project_review)
        for button in (self._import_plan_button, self._plan_prompt_button, self._start_button, self._continue_button, self._handoff_mode, self._handoff_button, self._approve_review_button, self._changes_review_button, self._project_review_button): actions.addWidget(button)
        actions.addStretch(1); layout.addLayout(actions); self._set_project_action_state(); return page

    def _make_advanced_page(self) -> QWidget:
        page = QWidget(); layout = QVBoxLayout(page); layout.addWidget(QLabel("Zaawansowane / System / Diagnostyka")); detail = QLabel("Techniczne widoki CC3 są zachowane. Otwórz je osobno, aby zobaczyć canonical authority status."); detail.setWordWrap(True); layout.addWidget(detail); button = QPushButton("Otwórz techniczny Control Center"); button.setObjectName("OpenAdvancedControlCenterButton"); button.clicked.connect(self._open_advanced); layout.addWidget(button); layout.addStretch(1); return page

    def _pages_changed(self, index: int) -> None:
        self._pages.setCurrentIndex(index)

    def select_page(self, name: str) -> None:
        if name not in PROJECT_PAGE_NAMES: raise ValueError(f"unknown project page: {name}")
        self._sidebar.setCurrentRow(PROJECT_PAGE_NAMES.index(name))

    def start_bootstrap(self) -> None:
        self._bootstrap_completed = False; self._bootstrap_ok = False; self._bootstrap_error_code = None; self._status.setText("BDB: odczyt canonical state…")
        try:
            self._snapshot = self._snapshot_loader(self._runtime_root)
            self._projects = self._catalog.read()
        except (ControlCenterQueryError, ProjectCatalogError) as exc:
            self._bootstrap_completed = True; self._bootstrap_error_code = getattr(exc, "code", "project_catalog_unavailable"); self._status.setText(f"BDB: DEGRADED — {self._bootstrap_error_code}"); self._render_catalog(()); self.dashboard_ready.emit(); return
        self._bootstrap_completed = True; self._bootstrap_ok = True; self._status.setText(f"BDB: {self._snapshot.system_state} | {len(self._projects)} projekt(ów) | read-only"); self._render_catalog(self._projects); self.dashboard_ready.emit()

    def _render_catalog(self, projects: tuple[ProjectRecord, ...]) -> None:
        self._recent.clear(); self._project_table.setRowCount(len(projects))
        for row, project in enumerate(projects):
            progress = f"{project.completed_tasks}/{project.total_tasks}" if project.plan_imported else "—"
            values = (project.display_name, project.project_status, progress, project.current_milestone or "—", project.current_task or "—")
            for column, value in enumerate(values): self._project_table.setItem(row, column, QTableWidgetItem(str(value)))
            recent = QListWidgetItem(f"{project.display_name} · {progress}"); recent.setData(32, project.project_id); self._recent.addItem(recent)
        self._project_table.resizeColumnsToContents()
        if projects and self._current_project_id is None: self._select_project(projects[0].project_id)
        elif not projects: self._select_project(None)

    def _select_project_row(self) -> None:
        row = self._project_table.currentRow()
        if 0 <= row < len(self._projects): self._select_project(self._projects[row].project_id)

    def _open_recent(self, item: QListWidgetItem) -> None:
        self._select_project(str(item.data(32))); self.select_page("Current project")

    def _select_project(self, project_id: str | None) -> None:
        self._current_project_id = project_id; project = next((item for item in self._projects if item.project_id == project_id), None)
        if project is None:
            self._project_title.setText("Wybierz projekt"); self._project_progress.setText("Nie zaimportowano planu projektu."); self._project_detail.setPlainText("{}" if not self._projects else "Wybierz projekt z listy."); self._render_memory(None); self._render_execution(None); self._set_project_action_state(); return
        self._project_title.setText(project.display_name); self._project_progress.setText(f"Postęp: {project.completed_tasks}/{project.total_tasks}" if project.plan_imported else "Nie zaimportowano planu projektu."); self._project_detail.setPlainText(json.dumps(project.to_dict(), ensure_ascii=False, sort_keys=True, indent=2)); self._render_memory(project); self._render_execution(project); self._set_project_action_state()

    def _set_project_action_state(self) -> None:
        project = next((item for item in self._projects if item.project_id == self._current_project_id), None)
        has_project = project is not None; has_plan = bool(project and project.plan_imported)
        self._import_plan_button.setEnabled(has_project); self._import_plan_button.setText("Wczytaj aktualizację planu" if has_plan else "Wczytaj plan"); self._plan_prompt_button.setEnabled(has_project); self._start_button.setEnabled(has_plan); self._continue_button.setEnabled(has_plan); self._handoff_mode.setEnabled(has_project); self._handoff_button.setEnabled(has_project)
        self._start_button.setToolTip("Wymagany import bdb-project-plan-v1" if not has_plan else "Wstaw bounded prompt do pustego composera ChatGPT")
        self._continue_button.setToolTip(self._start_button.toolTip())
        review = False
        if has_project:
            try:
                review = self._workflow.execution.snapshot(project.project_id).get("task_statuses", {}).get(project.current_task or "") == "review"
            except Exception:
                review = False
        self._approve_review_button.setEnabled(review); self._changes_review_button.setEnabled(review); self._project_review_button.setEnabled(has_project)

    def _render_execution(self, project: ProjectRecord | None) -> None:
        if project is None:
            self._execution_status.setText("Wykonanie: brak aktywnej próby")
            return
        try:
            snapshot = self._workflow.execution.snapshot(project.project_id)
            statuses = snapshot.get("task_statuses", {})
            attempts = snapshot.get("attempts", [])
            last = attempts[-1] if attempts else None
            current = snapshot.get("current_task_id") or project.current_task or "brak"
            if last and last.get("result_status") == "REVIEW_REQUIRED":
                text = f"Wykonanie: {current} — Gotowe do przeglądu\nAcceptance: REVIEW_REQUIRED\nAttempt: {last.get('attempt_id')}"
            elif last and last.get("result_status") == "FAIL":
                text = f"Wykonanie: {current} — Wymaga poprawki\n{last.get('failure_code') or 'validation failed'}"
            elif statuses and all(value in {"completed", "skipped"} for value in statuses.values()) and project.total_tasks:
                text = "Wykonanie: zakończone\nAcceptance: PASS"
            else:
                text = f"Wykonanie: {current} — {statuses.get(current, 'pending')}"
            self._execution_status.setText(text)
        except Exception as exc:
            self._execution_status.setText(f"Wykonanie: stan niedostępny ({getattr(exc, 'code', 'unavailable')})")

    def _approve_review(self) -> None:
        if self._current_project_id is None: return
        project = next((item for item in self._projects if item.project_id == self._current_project_id), None)
        if project is None or not project.current_task: return
        reason, accepted = QInputDialog.getText(self, "Zatwierdź review", "Uzasadnienie:")
        if not accepted: return
        try:
            self._workflow.execution.approve_review(project.project_id, project.current_task, reason=reason or "approved by user")
        except Exception as exc:
            self._status.setText(f"BDB: review zatrzymany — {getattr(exc, 'code', 'review_failed')}"); return
        self.start_bootstrap(); self._select_project(project.project_id)

    def _request_changes(self) -> None:
        if self._current_project_id is None: return
        project = next((item for item in self._projects if item.project_id == self._current_project_id), None)
        if project is None or not project.current_task: return
        reason, accepted = QInputDialog.getText(self, "Wymaga poprawki", "Co należy poprawić?")
        if not accepted: return
        try:
            self._workflow.execution.request_changes(project.project_id, project.current_task, reason=reason or "changes requested")
        except Exception as exc:
            self._status.setText(f"BDB: poprawka zatrzymana — {getattr(exc, 'code', 'review_failed')}"); return
        self.start_bootstrap(); self._select_project(project.project_id)

    def _request_project_review(self) -> None:
        if self._current_project_id is None: return
        try:
            self._workflow.execution.request_project_review(self._current_project_id)
        except Exception as exc:
            self._status.setText(f"BDB: review projektu zatrzymany — {getattr(exc, 'code', 'review_failed')}"); return
        self._status.setText("BDB: przegląd projektu zapisany w Project Memory")

    def _render_memory(self, project: ProjectRecord | None) -> None:
        views = [self._memory_tabs.widget(index) for index in range(self._memory_tabs.count())]
        if project is None:
            for view in views: view.setPlainText("")
            return
        try:
            memory = self._workflow.memory(project.project_id)
            state = memory.read_state(); plan = memory.current_plan()
            next_action = resolve_next_action(project, plan, state, plan_update_pending=self._pending_plan_preview is not None)
            health = project_health(state, plan)
            sentence = project_status_sentence(project, plan, state)
            plan_lines = [sentence, f"Health: {health}", f"Co teraz?: {next_action.title} — {next_action.detail}", "", f"Aktywny plan: v{plan.plan_version}" if plan else "Plan: brak", "Historia wersji: " + ", ".join(f"v{item.plan_version}" for item in memory.plan_versions())]
            if self._pending_plan_preview is not None:
                plan_lines.extend(["", "Oczekuje aktualizacja planu:", *self._pending_plan_preview.diff.summary_lines()])
            views[0].setPlainText("\n".join(plan_lines))
            views[1].setPlainText(bounded_history_summary(project, plan, state))
            views[2].setPlainText("Decyzje:\n" + "\n".join(f"- {item.title}: {item.decision} ({item.status})" for item in state.decisions) + "\n\nInbox:\n" + "\n".join(f"- {item.title} ({item.status})" for item in state.inbox))
            views[3].setPlainText("Ryzyka:\n" + "\n".join(f"- {item.title} ({item.severity}/{item.status})" for item in state.risks) + "\n\nDług techniczny:\n" + "\n".join(f"- {item.title} ({item.status})" for item in state.technical_debt) + "\n\nCheckpointy:\n" + "\n".join(f"- {item.checkpoint_id} — {item.label} — HEAD {item.git_head or 'unknown'} — plan v{item.plan_version or 'unknown'}" for item in state.checkpoints))
        except (ProjectMemoryError, ProjectWorkflowError) as exc:
            for view in views: view.setPlainText(f"Project Memory niedostępna: {getattr(exc, 'code', 'memory_unavailable')}")

    def _queue_handoff(self) -> None:
        if self._current_project_id is None: return
        try:
            launch = self._workflow.queue_handoff_prompt(self._current_project_id, self._handoff_mode.currentText())
        except ProjectWorkflowError as exc:
            self._status.setText(f"BDB: handoff zatrzymany — {exc.code}"); return
        self._status.setText(f"BDB: handoff oczekuje w ChatGPT ({launch.launch_id}); Send pozostaje ręczny")
        self._projects = self._catalog.read(); self._render_catalog(self._projects)

    def _new_project(self) -> None:
        dialog = _NewProjectDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted: return
        brief = dialog.brief(); alias = slugify_project_alias(brief.name); result = self._workflow.create_new(display_name=brief.name, repo_alias=alias, projects_root=Path.home() / "BDB Projects", brief=brief, github_name=alias)
        if not result.ok:
            self._status.setText(f"BDB: projekt zatrzymany — {result.error_code}"); return
        self.start_bootstrap(); self._select_project(result.project.project_id if result.project else None); self.select_page("Current project")

    def _open_existing_project(self) -> None:
        source = QFileDialog.getExistingDirectory(self, "Wybierz istniejący Git checkout")
        if not source: return
        name = Path(source).name or "Projekt"; brief = ProjectBrief(name, "Kontynuacja istniejącego projektu", "Projekt zarejestrowany przez BDB vNext.", "jeszcze nie wiem")
        try: project = self._workflow.register_existing(display_name=name, repo_alias=slugify_project_alias(name), local_repo_path=source, brief=brief)
        except ProjectWorkflowError as exc: self._status.setText(f"BDB: rejestracja zatrzymana — {exc.code}"); return
        self.start_bootstrap(); self._select_project(project.project_id); self.select_page("Current project")

    def _import_plan(self) -> None:
        if self._current_project_id is None: return
        path, _ = QFileDialog.getOpenFileName(self, "Wybierz project-plan.json", "", "Project Plan (*.json)")
        if not path: return
        try:
            selected = next((item for item in self._projects if item.project_id == self._current_project_id), None)
            if selected is not None and selected.plan_imported:
                preview = self._workflow.preview_plan_update(self._current_project_id, path)
                self._pending_plan_preview, self._pending_plan_path = preview, path
                self._render_memory(selected)
                if not preview.accepted:
                    self._status.setText(f"BDB: aktualizacja planu zablokowana — {preview.reason_code}"); return
                summary = "\n".join(preview.diff.summary_lines()) or "Brak zmian semantycznych"
                answer = QMessageBox.question(self, "Podgląd aktualizacji planu", f"Plan v{preview.current_version} → v{preview.next_version}\n\n{summary}\n\nZastosować aktualizację?", QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Ok, QMessageBox.StandardButton.Cancel)
                if answer != QMessageBox.StandardButton.Ok:
                    self._pending_plan_preview = None; self._pending_plan_path = None; self._render_memory(selected); self._status.setText("BDB: aktualizacja planu anulowana"); return
                project, _plan = self._workflow.apply_plan_update(self._current_project_id, path, preview)
            else:
                project, _plan = self._workflow.import_plan(self._current_project_id, path)
            self._pending_plan_preview = None; self._pending_plan_path = None
        except ProjectWorkflowError as exc: self._status.setText(f"BDB: import planu zatrzymany — {exc.code}"); return
        self.start_bootstrap(); self._select_project(project.project_id); self.select_page("Current project")

    def _queue_prompt(self, kind: str) -> None:
        if self._current_project_id is None: return
        try:
            launch = {"plan": self._workflow.queue_plan_prompt, "start": self._workflow.queue_start_prompt, "continue": self._workflow.queue_continue_prompt}[kind](self._current_project_id)
        except ProjectWorkflowError as exc: self._status.setText(f"BDB: prompt zatrzymany — {exc.code}"); return
        self._status.setText(f"BDB: prompt oczekuje w ChatGPT ({launch.launch_id}); Send pozostaje ręczny")
        self._projects = self._catalog.read(); self._render_catalog(self._projects)

    def _open_advanced(self) -> None:
        if self._advanced is None: self._advanced = VNextControlCenterWindow(runtime_root=self._runtime_root)
        self._advanced.show(); self._advanced.raise_(); self._advanced.activateWindow(); self._advanced.start_bootstrap()

    def smoke_report(self) -> dict[str, object]:
        snapshot = self._snapshot
        status_vector = (
            {"system": snapshot.system_state, "writer": snapshot.writer_state, "activation": snapshot.activation_state, "control_store": snapshot.store_state}
            if snapshot is not None
            else {"system": "DEGRADED", "writer": "OFF", "activation": "OFF", "control_store": "UNAVAILABLE"}
        )
        project = next((item for item in self._projects if item.project_id == self._current_project_id), None)
        health = "UNKNOWN"; next_action = "UNKNOWN"
        if project is not None:
            try:
                memory = self._workflow.memory(project.project_id); state = memory.read_state(); plan = memory.current_plan(); health = project_health(state, plan); next_action = resolve_next_action(project, plan, state, plan_update_pending=self._pending_plan_preview is not None).code
            except (ProjectMemoryError, ProjectWorkflowError):
                health = "UNAVAILABLE"
                next_action = "UNAVAILABLE"
        execution = None
        if project is not None:
            try:
                execution = self._workflow.execution.snapshot(project.project_id)
            except Exception:
                execution = {"status": "UNAVAILABLE"}
        return {"window_object_name": self.objectName(), "window_constructed": True, "read_only_startup": True, "bootstrap_completed": self._bootstrap_completed, "bootstrap_ok": self._bootstrap_ok, "bootstrap_error_code": self._bootstrap_error_code, "semantic_source": "bdb_vnext.control_center_query", "project_catalog_source": "bdb_vnext.project_catalog", "project_memory_source": "bdb_vnext.project_memory", "project_execution_source": "bdb_vnext.project_execution", "legacy_fallback": False, "page_names": list(PROJECT_PAGE_NAMES), "page_count": self._pages.count(), "project_count": len(self._projects), "work_count": len(snapshot.works) if snapshot is not None else 0, "current_project_id": self._current_project_id, "auto_send": False, "send_operations_invoked": 0, "mutation_operations_invoked": self._mutation_operations_invoked, "advanced_available": True, "snapshot_source": "bdb_vnext.control_center_query", "status_vector": status_vector, "health": health, "next_action": next_action, "execution": execution, "pending_plan_update": self._pending_plan_preview is not None, "actions_enabled": False, "auto_resume_invoked": False}


__all__ = ["PROJECT_PAGE_NAMES", "ProjectCenterWindow", "slugify_project_alias", "validate_wizard_payload"]

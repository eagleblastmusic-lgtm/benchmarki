"""NX-030 Project Center AUTO qualification and source-bound machine gate."""

from __future__ import annotations

import ast
import inspect
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

import pytest

from bdb_vnext.auto_scope_contract import AutoScope, DEFAULT_AUTO_SCOPE
from bdb_vnext.project_catalog import ProjectBrief, ProjectMilestone, ProjectPlan, ProjectTask, ProjectCatalog, new_project_record
from bdb_vnext.project_center_auto import (
    AUTO_SCOPE_OPTIONS,
    AUTO_STATUS_REASON_TEXT,
    AUTO_UI_CONTROL_CONTRACT,
    AutoCommandReceipt,
    CanonicalAutoState,
    CanonicalProjectCenterAutoCommands,
    PROJECT_CENTER_AUTO_UI_VERSION,
    ProjectCenterAutoCommandError,
    ProjectCenterAutoViewModel,
)


ROOT = Path(__file__).resolve().parents[1]


def _canonical(**overrides: Any) -> CanonicalAutoState:
    values: dict[str, Any] = {
        "project_id": "nx030-project",
        "scope": DEFAULT_AUTO_SCOPE,
        "scope_status": "READY",
        "reason_code": "READY",
        "reason": AUTO_STATUS_REASON_TEXT["READY"],
        "plan_available": True,
        "p2_completed": True,
        "p3_started": False,
    }
    values.update(overrides)
    return CanonicalAutoState(**values)


def _brief() -> ProjectBrief:
    return ProjectBrief(
        "NX-030 fixture",
        "Qualify Project Center AUTO",
        "A bounded fixture for the Project Center qualification.",
        "tool",
        ("Python",),
        ("canonical controls",),
        ("no automatic P3 start",),
    )


def _plan(project_id: str = "nx030-project") -> ProjectPlan:
    return ProjectPlan(
        project_id=project_id,
        project_name="NX-030 fixture",
        plan_version="1",
        milestones=(
            ProjectMilestone("M1", "First", "First milestone", "active"),
            ProjectMilestone("M2", "Second", "Second milestone", "pending"),
        ),
        tasks=(
            ProjectTask("T1", "M1", "First task", "Run the first task", "pending"),
            ProjectTask("T2", "M2", "Second task", "Run the second task", "pending"),
        ),
        current_task_id="T1",
    )


class RecordingCommands:
    def __init__(self, state: CanonicalAutoState) -> None:
        self.state = state
        self.calls: list[tuple[Any, ...]] = []

    def snapshot(self, *, plan_available: bool = False, plan_version: str | None = None) -> CanonicalAutoState:
        del plan_available, plan_version
        return self.state

    def start_auto(self, scope: AutoScope, *, confirmed: bool) -> AutoCommandReceipt:
        self.calls.append(("start_auto", scope, confirmed))
        self.state = replace(
            self.state,
            scope=scope,
            scope_status="ACTIVE",
            reason_code="AUTO_STARTED",
            reason="started",
            scope_epoch=max(1, self.state.scope_epoch),
            run_id="run:nx030",
        )
        return AutoCommandReceipt("START_AUTO", self.state.project_id, True, "AUTO_STARTED", "started", scope=scope, scope_epoch=self.state.scope_epoch)

    def continue_auto(self) -> AutoCommandReceipt:
        self.calls.append(("continue_auto",))
        return AutoCommandReceipt("CONTINUE_AUTO", self.state.project_id, True, "NEXT_ACTION_CANONICAL", "orchestrator selected next action", scope=self.state.scope, scope_epoch=self.state.scope_epoch)

    def resume_auto(self) -> AutoCommandReceipt:
        self.calls.append(("resume_auto",))
        self.state = replace(self.state, scope_status="ACTIVE", reason_code="AUTO_RESUMED", reason="resumed", stop_fenced=False, scope_epoch=self.state.scope_epoch + 1)
        return AutoCommandReceipt("RESUME_AUTO", self.state.project_id, True, "AUTO_RESUMED", "resumed", scope=self.state.scope, scope_epoch=self.state.scope_epoch)

    def stop_auto(self) -> AutoCommandReceipt:
        self.calls.append(("stop_auto",))
        self.state = replace(self.state, scope_status="STOPPED", reason_code="STOPPED", reason=AUTO_STATUS_REASON_TEXT["STOPPED"], stop_fenced=True)
        return AutoCommandReceipt("STOP_AUTO", self.state.project_id, True, "STOPPED", "stopped", scope=self.state.scope, scope_epoch=self.state.scope_epoch)


def test_nx030_scope_selector_exposes_all_scopes_and_defaults_to_milestone() -> None:
    assert tuple(scope.value for scope in AUTO_SCOPE_OPTIONS) == ("TASK", "MILESTONE", "PROJECT", "UNTIL_STOPPED")
    assert DEFAULT_AUTO_SCOPE == AutoScope.MILESTONE
    assert PROJECT_CENTER_AUTO_UI_VERSION == "1.0.0"


def test_scope_selection_is_ui_only_and_does_not_mutate_canonical_authority() -> None:
    source = _canonical()
    for selected in AUTO_SCOPE_OPTIONS:
        view = ProjectCenterAutoViewModel.from_canonical(source).select_scope(selected)
        assert view.selected_scope == selected
        assert view.canonical == source
        assert view.selected_scope_is_pending == (selected != source.scope)


def test_browser_only_state_cannot_change_canonical_scope_or_action() -> None:
    source = _canonical()
    baseline = ProjectCenterAutoViewModel.from_canonical(source)
    for stale in (
        {"scope": "PROJECT", "task_id": "stale-task"},
        {"scope": "UNTIL_STOPPED", "run_id": "stale-run"},
        {"conversation_id": "guessed-chat"},
    ):
        observed = ProjectCenterAutoViewModel.from_canonical(source, browser_local_state=stale)
        assert observed.canonical == source
        assert observed.selected_scope == baseline.selected_scope
        assert observed.continue_intent() == baseline.continue_intent()


def test_p2_completed_p3_not_started_is_a_pure_projection() -> None:
    source = _canonical(p2_completed=True, p3_started=False)
    view = ProjectCenterAutoViewModel.from_canonical(source)
    assert view.canonical.p2_completed is True
    assert view.canonical.p3_started is False
    assert view.start_intent()["scope"] == "MILESTONE"
    assert view.canonical == source


def test_structured_disabled_reasons_cover_required_canonical_states() -> None:
    statuses = ("WAITING", "WAITING_FOR_PLAN", "PAUSED", "BLOCKED", "STOPPED", "CI_WAITING", "DELIVERY_UNCERTAIN", "OPERATOR_CHECKPOINT", "COMPLETED")
    for status in statuses:
        source = _canonical(scope_status=status, reason_code=status, reason=AUTO_STATUS_REASON_TEXT[status], stop_fenced=status == "STOPPED")
        view = ProjectCenterAutoViewModel.from_canonical(source)
        assert view.blocker_reason == AUTO_STATUS_REASON_TEXT[status]
        assert view.disabled_reason("start")
        assert view.disabled_reason("stop") or status in {"WAITING", "PAUSED", "CI_WAITING", "DELIVERY_UNCERTAIN", "OPERATOR_CHECKPOINT"}


def test_intents_leave_next_task_and_prompt_selection_to_canonical_orchestrator() -> None:
    view = ProjectCenterAutoViewModel.from_canonical(_canonical(), selected_scope=AutoScope.PROJECT)
    assert view.start_intent() == {
        "command": "START_AUTO",
        "project_id": "nx030-project",
        "scope": "PROJECT",
        "explicit_confirmation_required": True,
    }
    assert "task_id" not in view.continue_intent()
    assert "milestone_id" not in view.continue_intent()
    assert "prompt" not in view.resume_intent()
    assert "browser" not in view.resume_intent()


def test_canonical_adapter_uses_project_memory_v2_and_nx022_stop_fence(tmp_path: Path) -> None:
    catalog = ProjectCatalog(tmp_path / "runtime")
    record = new_project_record(
        project_id="nx030-project",
        display_name="NX-030 fixture",
        repo_alias="nx030-fixture",
        local_repo_path=tmp_path / "repo",
        github_repo=None,
        brief=_brief(),
    )
    catalog.upsert(record)
    adapter = CanonicalProjectCenterAutoCommands(
        tmp_path / "runtime",
        record.project_id,
        project_provider=lambda: record,
        plan_provider=lambda: _plan(record.project_id),
    )

    assert adapter.snapshot(plan_available=True).scope_status == "AUTO_START_AVAILABLE"
    with pytest.raises(ProjectCenterAutoCommandError) as error:
        adapter.start_auto(AutoScope.MILESTONE, confirmed=False)
    assert error.value.code == "explicit_confirmation_required"

    started = adapter.start_auto(AutoScope.PROJECT, confirmed=True)
    assert started.accepted is True
    assert started.scope == AutoScope.PROJECT
    active = adapter.snapshot(plan_available=True)
    assert active.scope == AutoScope.PROJECT
    assert active.scope_status == "ACTIVE"
    stopped = adapter.stop_auto()
    assert stopped.reason_code == "STOPPED"
    assert adapter.snapshot(plan_available=True).stop_fenced is True
    resumed = adapter.resume_auto()
    assert resumed.reason_code == "AUTO_RESUMED"
    assert adapter.snapshot(plan_available=True).scope_epoch == active.scope_epoch + 1


def test_project_center_auto_controls_are_keyboard_accessible(tmp_path: Path) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from bdb_gui.project_center import ProjectCenterWindow

    app = QApplication.instance() or QApplication(["nx030-project-center-test"])
    window = ProjectCenterWindow(runtime_root=tmp_path / "runtime")
    assert window._auto_scope_selector.count() == 4
    assert window._auto_scope_selector.currentData() == AutoScope.MILESTONE
    assert window._auto_scope_selector.accessibleName() == "AUTO scope"
    for widget, expected in (
        (window._auto_start_button, "Uruchom AUTO"),
        (window._auto_stop_button, "STOP"),
        (window._auto_continue_button, "Kontynuuj"),
        (window._auto_resume_button, "Wznów"),
    ):
        assert widget.accessibleName() == expected
        assert widget.focusPolicy().value != 0
        assert widget.objectName()
    window.close()
    app.processEvents()


def test_project_center_actions_use_only_canonical_commands(tmp_path: Path) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from bdb_gui.project_center import ProjectCenterWindow

    app = QApplication.instance() or QApplication(["nx030-project-center-command-test"])
    runtime = tmp_path / "runtime"
    catalog = ProjectCatalog(runtime)
    record = new_project_record(
        project_id="nx030-project",
        display_name="NX-030 fixture",
        repo_alias="nx030-fixture",
        local_repo_path=tmp_path / "repo",
        github_repo=None,
        brief=_brief(),
    )
    record = replace(record, plan_imported=True, plan_version="1", total_tasks=2, current_milestone="M1", current_task="T1")
    catalog.upsert(record)
    commands = RecordingCommands(_canonical(project_id=record.project_id))
    window = ProjectCenterWindow(
        runtime_root=runtime,
        catalog=catalog,
        auto_commands_factory=lambda _project: commands,
        auto_start_confirmation=lambda _view: True,
    )
    window._projects = (record,)
    window._select_project(record.project_id)

    window._auto_scope_selector.setCurrentIndex(AUTO_SCOPE_OPTIONS.index(AutoScope.PROJECT))
    assert commands.calls == []
    window._start_auto_from_gui()
    window._continue_auto_from_gui()
    window._stop_auto_from_gui()
    # The fake represents a stopped canonical state; make the resume path
    # available exactly as it would be after a fresh canonical read.
    window._auto_view_model = ProjectCenterAutoViewModel.from_canonical(commands.state)
    window._resume_auto_from_gui()
    assert [call[0] for call in commands.calls] == ["start_auto", "continue_auto", "stop_auto", "resume_auto"]
    assert commands.calls[0][1:] == (AutoScope.PROJECT, True)
    window.close()
    app.processEvents()


def _source_readback(repo_root: Path) -> tuple[str, str, bool]:
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, text=True, check=True).stdout.strip()
    tree = subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=repo_root, capture_output=True, text=True, check=True).stdout.strip()
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo_root, capture_output=True, text=True, check=True).stdout.strip()
    diff_check = subprocess.run(["git", "diff", "--check"], cwd=repo_root, capture_output=True, text=True, check=False)
    return head, tree, not status and diff_check.returncode == 0


def inspect_nx030_gate_for_hardcoded_results() -> tuple[bool, list[str]]:
    source = (Path(__file__).resolve()).read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run_nx030_machine_gate")
    result_fields = {
        "PROJECT_CENTER_AUTO_UI_VERSION_EXPLICIT",
        "DEFAULT_GUI_SCOPE_IS_MILESTONE",
        "UI_SCOPE_SELECTION_MUTATES_AUTHORITY_DIRECTLY",
        "STALE_GUI_SCOPE_STARTS_RUN",
        "GUI_STOP_BYPASSES_CANONICAL_FENCE",
        "CONTINUE_REQUIRES_MANUAL_NEXT_TASK_SELECTION",
        "CONTINUE_REQUIRES_MANUAL_NEXT_MILESTONE_SELECTION",
        "RESUME_REQUIRES_MANUAL_PROMPT",
        "RESUME_FROM_BROWSER_LOCAL_STATE",
        "P2_COMPLETED_P3_NOT_STARTED_VIEW_CORRECT",
        "GUI_RENDER_STARTS_P3",
        "UI_BECOMES_WORKFLOW_AUTHORITY",
        "GUI_STATE_FIXTURES",
        "GUI_CANONICAL_STATE_DIVERGENCES",
        "ACCESSIBILITY_FIXTURES",
        "KEYBOARD_ACCESSIBILITY_DIVERGENCES",
        "SOURCE_BOUND_MACHINE_GATE",
        "NX030_STATUS",
    }
    hardcoded: list[str] = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in result_fields:
                hardcoded.append(target.id)
    return not hardcoded, sorted(set(hardcoded))


def run_nx030_machine_gate() -> dict[str, Any]:
    fixtures = []
    canonical_states = []
    for scope in AUTO_SCOPE_OPTIONS:
        state = _canonical(scope=scope, current_milestone_id="M1", current_task_id="T1")
        canonical_states.append(state)
        view = ProjectCenterAutoViewModel.from_canonical(state, selected_scope=scope)
        fixtures.append((state, view))

    browser_variants: tuple[Mapping[str, Any], ...] = (
        {},
        {"scope": "PROJECT", "task_id": "browser-stale"},
        {"scope": "UNTIL_STOPPED", "run_id": "browser-stale"},
    )
    gui_canonical_divergences = sum(
        int(ProjectCenterAutoViewModel.from_canonical(state, browser_local_state=browser).canonical != state)
        for state, _view in fixtures
        for browser in browser_variants
    )
    selection_mutations = sum(
        int(ProjectCenterAutoViewModel.from_canonical(state).select_scope(scope).canonical != state)
        for state in canonical_states
        for scope in AUTO_SCOPE_OPTIONS
    )

    stale_start_attempts = sum(
        int(
            view.start_intent().get("explicit_confirmation_required") is not True
            or view.canonical.run_id is not None
        )
        for _state, view in fixtures
    )
    continue_intents = [view.continue_intent() for _state, view in fixtures]
    resume_intents = [view.resume_intent() for _state, view in fixtures]
    stop_source = inspect.getsource(CanonicalProjectCenterAutoCommands.stop_auto)
    accessibility_missing = sum(
        int(not spec.accessible_name or not spec.keyboard_focusable or not spec.exposes_disabled_reason)
        for spec in AUTO_UI_CONTROL_CONTRACT
    )

    p2_fixture = _canonical(p2_completed=True, p3_started=False)
    p2_view = ProjectCenterAutoViewModel.from_canonical(p2_fixture)
    no_hardcoded, hardcoded = inspect_nx030_gate_for_hardcoded_results()
    head, tree, clean = _source_readback(ROOT)
    source_bound = len(head) == 40 and len(tree) == 40 and clean

    PROJECT_CENTER_AUTO_UI_VERSION_EXPLICIT = bool(PROJECT_CENTER_AUTO_UI_VERSION == "1.0.0")
    DEFAULT_GUI_SCOPE_IS_MILESTONE = bool(DEFAULT_AUTO_SCOPE == AutoScope.MILESTONE and ProjectCenterAutoViewModel.from_canonical(_canonical()).selected_scope == AutoScope.MILESTONE)
    UI_SCOPE_SELECTION_MUTATES_AUTHORITY_DIRECTLY = bool(selection_mutations > 0)
    STALE_GUI_SCOPE_STARTS_RUN = bool(stale_start_attempts > 0)
    GUI_STOP_BYPASSES_CANONICAL_FENCE = bool("execute_stop_transaction" not in stop_source or CanonicalProjectCenterAutoCommands.STOP_COMMAND_AUTHORITY != "ProjectMemoryStoreV2.request_stop")
    CONTINUE_REQUIRES_MANUAL_NEXT_TASK_SELECTION = bool(any("task_id" in intent for intent in continue_intents))
    CONTINUE_REQUIRES_MANUAL_NEXT_MILESTONE_SELECTION = bool(any("milestone_id" in intent for intent in continue_intents))
    RESUME_REQUIRES_MANUAL_PROMPT = bool(any("prompt" in intent for intent in resume_intents))
    RESUME_FROM_BROWSER_LOCAL_STATE = bool(
        ProjectCenterAutoViewModel.from_canonical(_canonical(), browser_local_state={"scope": "PROJECT"}).canonical
        != _canonical()
    )
    P2_COMPLETED_P3_NOT_STARTED_VIEW_CORRECT = bool(p2_view.canonical.p2_completed and not p2_view.canonical.p3_started)
    GUI_RENDER_STARTS_P3 = bool(p2_view.canonical.p3_started)
    UI_BECOMES_WORKFLOW_AUTHORITY = bool(selection_mutations > 0 or gui_canonical_divergences > 0)
    GUI_STATE_FIXTURES = len(fixtures)
    GUI_CANONICAL_STATE_DIVERGENCES = gui_canonical_divergences
    ACCESSIBILITY_FIXTURES = len(AUTO_UI_CONTROL_CONTRACT)
    KEYBOARD_ACCESSIBILITY_DIVERGENCES = accessibility_missing
    HARDCODED_GATE_RESULT_FIELDS = hardcoded
    NO_HARDCODED_GATE_RESULTS = no_hardcoded
    SOURCE_HEAD = head
    SOURCE_TREE = tree
    WORKTREE_CLEAN = clean
    SOURCE_BOUND_MACHINE_GATE = "PASS" if source_bound else "FAIL"
    all_pass = (
        PROJECT_CENTER_AUTO_UI_VERSION_EXPLICIT
        and DEFAULT_GUI_SCOPE_IS_MILESTONE
        and not UI_SCOPE_SELECTION_MUTATES_AUTHORITY_DIRECTLY
        and not STALE_GUI_SCOPE_STARTS_RUN
        and not GUI_STOP_BYPASSES_CANONICAL_FENCE
        and not CONTINUE_REQUIRES_MANUAL_NEXT_TASK_SELECTION
        and not CONTINUE_REQUIRES_MANUAL_NEXT_MILESTONE_SELECTION
        and not RESUME_REQUIRES_MANUAL_PROMPT
        and not RESUME_FROM_BROWSER_LOCAL_STATE
        and P2_COMPLETED_P3_NOT_STARTED_VIEW_CORRECT
        and not GUI_RENDER_STARTS_P3
        and not UI_BECOMES_WORKFLOW_AUTHORITY
        and GUI_STATE_FIXTURES == len(AUTO_SCOPE_OPTIONS)
        and GUI_CANONICAL_STATE_DIVERGENCES == 0
        and ACCESSIBILITY_FIXTURES == len(AUTO_UI_CONTROL_CONTRACT)
        and KEYBOARD_ACCESSIBILITY_DIVERGENCES == 0
        and NO_HARDCODED_GATE_RESULTS
        and SOURCE_BOUND_MACHINE_GATE == "PASS"
    )
    return {
        "PROJECT_CENTER_AUTO_UI_VERSION_EXPLICIT": PROJECT_CENTER_AUTO_UI_VERSION_EXPLICIT,
        "DEFAULT_GUI_SCOPE_IS_MILESTONE": DEFAULT_GUI_SCOPE_IS_MILESTONE,
        "UI_SCOPE_SELECTION_MUTATES_AUTHORITY_DIRECTLY": UI_SCOPE_SELECTION_MUTATES_AUTHORITY_DIRECTLY,
        "STALE_GUI_SCOPE_STARTS_RUN": STALE_GUI_SCOPE_STARTS_RUN,
        "GUI_STOP_BYPASSES_CANONICAL_FENCE": GUI_STOP_BYPASSES_CANONICAL_FENCE,
        "CONTINUE_REQUIRES_MANUAL_NEXT_TASK_SELECTION": CONTINUE_REQUIRES_MANUAL_NEXT_TASK_SELECTION,
        "CONTINUE_REQUIRES_MANUAL_NEXT_MILESTONE_SELECTION": CONTINUE_REQUIRES_MANUAL_NEXT_MILESTONE_SELECTION,
        "RESUME_REQUIRES_MANUAL_PROMPT": RESUME_REQUIRES_MANUAL_PROMPT,
        "RESUME_FROM_BROWSER_LOCAL_STATE": RESUME_FROM_BROWSER_LOCAL_STATE,
        "P2_COMPLETED_P3_NOT_STARTED_VIEW_CORRECT": P2_COMPLETED_P3_NOT_STARTED_VIEW_CORRECT,
        "GUI_RENDER_STARTS_P3": GUI_RENDER_STARTS_P3,
        "UI_BECOMES_WORKFLOW_AUTHORITY": UI_BECOMES_WORKFLOW_AUTHORITY,
        "GUI_STATE_FIXTURES": GUI_STATE_FIXTURES,
        "GUI_CANONICAL_STATE_DIVERGENCES": GUI_CANONICAL_STATE_DIVERGENCES,
        "ACCESSIBILITY_FIXTURES": ACCESSIBILITY_FIXTURES,
        "KEYBOARD_ACCESSIBILITY_DIVERGENCES": KEYBOARD_ACCESSIBILITY_DIVERGENCES,
        "HARDCODED_GATE_RESULT_FIELDS": HARDCODED_GATE_RESULT_FIELDS,
        "NO_HARDCODED_GATE_RESULTS": NO_HARDCODED_GATE_RESULTS,
        "SOURCE_HEAD": SOURCE_HEAD,
        "SOURCE_TREE": SOURCE_TREE,
        "WORKTREE_CLEAN": WORKTREE_CLEAN,
        "SOURCE_BOUND_MACHINE_GATE": SOURCE_BOUND_MACHINE_GATE,
        "NX030_STATUS": "PASS" if all_pass else "FAIL",
    }


def test_nx030_machine_gate_execution() -> None:
    gate = run_nx030_machine_gate()
    print(json.dumps(gate, ensure_ascii=False, sort_keys=True))
    assert gate["PROJECT_CENTER_AUTO_UI_VERSION_EXPLICIT"] is True
    assert gate["DEFAULT_GUI_SCOPE_IS_MILESTONE"] is True
    assert gate["UI_SCOPE_SELECTION_MUTATES_AUTHORITY_DIRECTLY"] is False
    assert gate["STALE_GUI_SCOPE_STARTS_RUN"] is False
    assert gate["GUI_STOP_BYPASSES_CANONICAL_FENCE"] is False
    assert gate["CONTINUE_REQUIRES_MANUAL_NEXT_TASK_SELECTION"] is False
    assert gate["CONTINUE_REQUIRES_MANUAL_NEXT_MILESTONE_SELECTION"] is False
    assert gate["RESUME_REQUIRES_MANUAL_PROMPT"] is False
    assert gate["RESUME_FROM_BROWSER_LOCAL_STATE"] is False
    assert gate["P2_COMPLETED_P3_NOT_STARTED_VIEW_CORRECT"] is True
    assert gate["GUI_RENDER_STARTS_P3"] is False
    assert gate["UI_BECOMES_WORKFLOW_AUTHORITY"] is False
    assert gate["GUI_CANONICAL_STATE_DIVERGENCES"] == 0
    assert gate["KEYBOARD_ACCESSIBILITY_DIVERGENCES"] == 0
    assert gate["HARDCODED_GATE_RESULT_FIELDS"] == []
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    assert gate["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
    assert gate["NX030_STATUS"] == "PASS"

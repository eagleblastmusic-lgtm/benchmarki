from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUI = ROOT / "bdb_gui"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_p11_artifacts_exist() -> None:
    expected = (
        GUI / "projects.py",
        GUI / "projects_view.py",
        GUI / "project_workers.py",
        GUI / "project_window.py",
        ROOT / "schemas" / "bdb-gui-prepare-plan-v1.schema.json",
        ROOT / "schemas" / "bdb-gui-prepare-result-v1.schema.json",
        ROOT / "docs" / "BDB_CONTROL_CENTER_PROJECT_WIZARD.md",
        ROOT / "docs" / "adr" / "0012-two-gate-project-prepare-wizard.md",
    )
    for path in expected:
        assert path.is_file(), f"Missing P11 artifact: {path.relative_to(ROOT)}"
        assert path.stat().st_size > 0


def test_prepare_service_uses_only_public_operator_prepare() -> None:
    source = read(GUI / "projects.py")
    assert "from bdb_operator import OperatorApi, OperatorResponse" in source
    assert "self._operator.prepare(" in source
    assert "existing_prepare_workspace_loop" in source
    for forbidden in (
        "subprocess",
        "git.exe",
        "git -C",
        "shell=True",
        "os.system",
        "shutil.rmtree",
        "Remove-Item",
    ):
        assert forbidden not in source


def test_plan_and_prepare_are_separate_workers_and_buttons() -> None:
    view = read(GUI / "projects_view.py")
    workers = read(GUI / "project_workers.py")
    window = read(GUI / "project_window.py")

    assert 'QPushButton("Zbuduj plan")' in view
    assert 'QPushButton("Przygotuj projekt")' in view
    assert "PrepareAckCheckbox" in view
    assert "self._invalidate_plan" in view
    assert "class PlanWorker" in workers
    assert "class PrepareWorker" in workers
    assert "prepare_confirmation_provider" in window
    assert "QMessageBox.question" in window
    assert "self._project_prepare_service" in window
    assert "self.start_bootstrap()" in window


def test_window_constructor_never_builds_plan_or_runs_prepare() -> None:
    source = read(GUI / "project_window.py")
    tree = ast.parse(source)
    window = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ProjectControlCenterWindow"
    )
    constructor = next(
        node for node in window.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    calls = {
        node.func.attr
        for node in ast.walk(constructor)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "build_plan" not in calls
    assert "execute" not in calls
    assert "prepare" not in calls
    assert "_start_prepare_plan" not in calls
    assert "_request_prepare" not in calls


def test_prepare_validation_prevents_path_escape_and_unbounded_inputs() -> None:
    source = read(GUI / "projects.py")
    assert "MAX_ALLOWED_PATHS = 100" in source
    assert "workspace_root.relative_to(workspace_parent)" in source
    assert 'if workspace_root.exists()' in source
    assert 'if not source.is_dir() or not source.joinpath(".git").exists()' in source
    assert 'value.startswith(("/", "../"))' in source
    assert 'or ":" in value' in source


def test_product_entrypoint_composes_project_wizard_with_read_only_session_history() -> None:
    app = read(GUI / "app.py")
    assert "VNextControlCenterWindow" in app or "ProjectCenterWindow" in app or "SessionProjectControlCenterWindow" in app
    assert "window = VNextControlCenterWindow(" in app or "window = SessionProjectControlCenterWindow(" in app
    assert "_start_prepare_plan" not in app
    assert "_request_prepare" not in app
    assert "_start_session_history_read" not in app

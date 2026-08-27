from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUI = ROOT / "bdb_gui"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def python_sources() -> list[Path]:
    return sorted(GUI.rglob("*.py"))


def attribute_calls(tree: ast.AST) -> set[str]:
    calls: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        calls.add(node.func.attr)
    return calls


def test_production_gui_packages_and_entrypoint_exist() -> None:
    expected = (
        GUI / "__init__.py",
        GUI / "state.py",
        GUI / "bootstrap.py",
        GUI / "operations.py",
        GUI / "current_operation.py",
        GUI / "current_operation_view.py",
        GUI / "history.py",
        GUI / "history_view.py",
        GUI / "workers.py",
        GUI / "dashboard.py",
        GUI / "main_window.py",
        GUI / "app.py",
        ROOT / "docs" / "BDB_CONTROL_CENTER_SKELETON.md",
        ROOT / "docs" / "BDB_CONTROL_CENTER_PROCESS_CONTROLS.md",
        ROOT / "docs" / "BDB_CONTROL_CENTER_CURRENT_OPERATION.md",
        ROOT / "docs" / "BDB_CONTROL_CENTER_HISTORY.md",
        ROOT / "docs" / "adr" / "0007-read-only-asynchronous-gui-bootstrap.md",
        ROOT / "docs" / "adr" / "0008-explicit-serialized-process-controls.md",
        ROOT / "docs" / "adr" / "0009-read-only-current-operation-view.md",
        ROOT / "docs" / "adr" / "0010-bounded-manual-journal-history.md",
    )
    for path in expected:
        assert path.is_file(), f"Missing GUI artifact: {path.relative_to(ROOT)}"
        assert path.stat().st_size > 0

    pyproject = read(ROOT / "pyproject.toml")
    assert 'bdb-control-center = "bdb_gui.app:main"' in pyproject
    assert 'gui = ["PySide6-Essentials>=6.10,<6.12"]' in pyproject
    for package in ("bdb_bridge*", "bdb_operator*", "bdb_gui*", "bdb_poc*"):
        assert f'"{package}"' in pyproject
    assert "dependencies = []" in pyproject


def test_gui_depends_only_on_public_operator_boundary() -> None:
    for path in python_sources():
        tree = ast.parse(read(path), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            assert all(not name.startswith("bdb_bridge") for name in names), (
                f"GUI imports BDB Core directly in {path.relative_to(ROOT)}: {names}"
            )

    for path in (GUI / "bootstrap.py", GUI / "operations.py", GUI / "current_operation.py", GUI / "history.py"):
        assert "from bdb_operator import OperatorApi, OperatorResponse" in read(path)


def test_gui_has_no_process_network_or_git_execution_surface() -> None:
    forbidden_import_roots = {
        "subprocess", "socket", "socketserver", "http", "urllib", "requests",
        "aiohttp", "websockets", "fastapi", "flask",
    }
    for path in python_sources():
        tree = ast.parse(read(path), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".", 1)[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".", 1)[0]]
            else:
                continue
            assert forbidden_import_roots.isdisjoint(names), (
                f"Forbidden execution/network import in {path.relative_to(ROOT)}: {names}"
            )

    combined = "\n".join(read(path) for path in python_sources()).lower()
    for token in ("powershell.exe", "git.exe", "shell=true", "listen(", "bind("):
        assert token not in combined


def test_bootstrap_calls_only_capabilities_and_list_projects() -> None:
    tree = ast.parse(read(GUI / "bootstrap.py"))
    calls = attribute_calls(tree)
    operator_operations = {
        "capabilities", "list_projects", "status", "events", "current_operation",
        "logs", "prepare", "start", "stop", "rearm",
    }
    assert calls & operator_operations == {"capabilities", "list_projects"}


def test_window_constructor_does_not_start_io_or_mutate() -> None:
    source = read(GUI / "main_window.py")
    tree = ast.parse(source)
    window_class = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ControlCenterWindow"
    )
    constructor = next(
        node for node in window_class.body if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    constructor_calls = attribute_calls(constructor)
    assert "start_bootstrap" not in constructor_calls
    assert constructor_calls.isdisjoint({"read", "read_status", "execute", "prepare", "stop", "rearm"})

    for forbidden in (
        "self._bootstrap_service.prepare(",
        "self._bootstrap_service.start(",
        "self._bootstrap_service.stop(",
        "self._bootstrap_service.rearm(",
        "self._operations_service.execute(",
        "self._operations_service.read_status(",
        "self._current_operation_service.read(",
        "self._history_service.read(",
    ):
        assert forbidden not in source
    assert "self._thread_pool.start(worker)" in source
    for worker in ("BootstrapWorker", "StatusWorker", "ControlWorker", "CurrentOperationWorker", "HistoryWorker"):
        assert worker in source


def test_p07_process_controls_are_closed_explicit_and_confirmed() -> None:
    dashboard = read(GUI / "dashboard.py")
    operations = read(GUI / "operations.py")
    window = read(GUI / "main_window.py")
    for label in ('QPushButton("Start")', 'QPushButton("Stop")', 'QPushButton("Re-arm")'):
        assert label in dashboard
    assert 'QPushButton("Odśwież status")' in dashboard
    assert 'action not in {"start", "stop", "rearm"}' in operations
    assert "QMessageBox.question" in window
    assert "confirmation_provider" in window
    assert "self._has_active_task()" in window
    assert "self._start_status_read()" in window
    assert "EXPLICIT MUTATIONS" in window


def test_p08_current_operation_is_read_only_and_uses_existing_projection() -> None:
    service = read(GUI / "current_operation.py")
    view = read(GUI / "current_operation_view.py")
    window = read(GUI / "main_window.py")
    assert "self._operator.current_operation(workspace_root)" in service
    for forbidden in (".start(", ".stop(", ".rearm(", ".prepare(", ".events(", ".logs("):
        assert forbidden not in service
    assert 'QPushButton("Odśwież operację")' in view
    assert "READ-ONLY JOURNAL PROJECTION" in view
    assert "CurrentOperationWidget" in window
    assert "CurrentOperationWorker" in window


def test_p09_history_is_bounded_manual_and_read_only() -> None:
    service = read(GUI / "history.py")
    view = read(GUI / "history_view.py")
    window = read(GUI / "main_window.py")
    assert "self._operator.events(" in service
    assert "MAX_HISTORY_LIMIT = 500" in service
    assert "after_event_id" in service
    assert "next_after_event_id" in service
    assert 'QPushButton("Odśwież historię")' in view
    assert 'QPushButton("Wczytaj więcej")' in view
    assert "HistoryWidget" in window
    assert "HistoryWorker" in window
    for forbidden in ("sqlite3", "SELECT ", "INSERT ", "UPDATE ", "DELETE "):
        assert forbidden not in service
        assert forbidden not in view


def test_gui_contains_no_background_polling_loop() -> None:
    combined = "\n".join(read(path) for path in python_sources())
    assert "QTimer.start" not in combined
    assert "while True" not in combined
    assert "threading.Thread" not in combined
    assert "setInterval" not in combined


def test_gui_package_is_valid_python_without_importing_optional_qt() -> None:
    for path in python_sources():
        ast.parse(read(path), filename=str(path))

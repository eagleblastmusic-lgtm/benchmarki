from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUI = ROOT / "bdb_gui"
OPERATOR = ROOT / "bdb_operator"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_session_history_artifacts_exist() -> None:
    expected = (
        OPERATOR / "session_projection.py",
        GUI / "session_history.py",
        GUI / "session_history_view.py",
        GUI / "session_history_worker.py",
        GUI / "session_history_window.py",
        ROOT / "schemas" / "bdb-control-center-smoke-v1.schema.json",
    )
    for path in expected:
        assert path.is_file(), f"Missing session history artifact: {path.relative_to(ROOT)}"
        assert path.stat().st_size > 0


def test_gui_session_service_uses_only_public_operator_sessions() -> None:
    source = read(GUI / "session_history.py")
    assert "from bdb_operator import OperatorApi, OperatorResponse" in source
    assert "self._operator.sessions(workspace_root, limit=limit)" in source
    for forbidden in (
        "sqlite3",
        "SELECT ",
        "INSERT ",
        "UPDATE ",
        "DELETE ",
        "subprocess",
        "os.system",
        "shell=True",
    ):
        assert forbidden not in source


def test_operator_projection_is_bounded_and_read_only() -> None:
    source = read(OPERATOR / "session_projection.py")
    assert '"?mode=ro"' in source
    assert 'connection.execute("PRAGMA query_only = ON")' in source
    assert "MAX_SESSION_LIMIT = 100" in source
    assert "MAX_ATTEMPTS_PER_SESSION = 20" in source
    assert "MAX_RESULT_BYTES = 64 * 1024" in source
    assert "MAX_RECEIPT_BYTES = 2 * 1024 * 1024" in source
    assert '"repair_relationships_inferred": False' in source
    assert "path.is_symlink()" in source
    assert "_contained(candidate, self.direct_result_dir)" in source
    assert "_contained(candidate, self.receipt_root)" in source


def test_view_requires_explicit_user_actions_to_open_paths() -> None:
    source = read(GUI / "session_history_view.py")
    assert 'QPushButton("Otwórz wynik")' in source
    assert 'QPushButton("Otwórz receipt")' in source
    assert 'QPushButton("Otwórz katalog")' in source
    assert "clicked.connect(self._open_result)" in source
    assert "clicked.connect(self._open_receipt)" in source
    assert "clicked.connect(self._open_folder)" in source
    assert "QDesktopServices.openUrl" in source
    assert "self._open_result()" not in source
    assert "self._open_receipt()" not in source
    assert "self._open_folder()" not in source


def test_session_window_uses_existing_single_worker_gate_without_correlation_logic() -> None:
    source = read(GUI / "session_history_window.py")
    assert "if self._has_active_task():" in source
    assert "self._thread_pool.start(worker)" in source
    assert "super()._has_active_task() or self._session_history_worker is not None" in source
    assert "self.session_history_view.set_busy(busy, message)" in source
    for forbidden in ("repair_group_id", "correlation_id", "infer_repair", "guess_repair"):
        assert forbidden not in source


def test_product_entrypoint_does_not_auto_read_sessions_or_open_files() -> None:
    app = read(GUI / "app.py")
    assert "VNextControlCenterWindow" in app or "ProjectCenterWindow" in app or "SessionProjectControlCenterWindow" in app
    assert ".sessions(" not in app
    assert "_start_session_history_read" not in app
    assert "openUrl" not in app

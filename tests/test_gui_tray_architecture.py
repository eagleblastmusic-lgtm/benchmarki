from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUI = ROOT / "bdb_gui"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_p12_artifacts_exist() -> None:
    for path in (
        GUI / "tray.py",
        GUI / "tray_window.py",
        ROOT / "docs" / "BDB_CONTROL_CENTER_TRAY_NOTIFICATIONS.md",
        ROOT / "docs" / "adr" / "0013-event-driven-local-tray.md",
    ):
        assert path.is_file(), f"Missing P12 artifact: {path.relative_to(ROOT)}"
        assert path.stat().st_size > 0


def test_tray_is_event_driven_and_has_no_polling_or_backend_access() -> None:
    combined = read(GUI / "tray.py") + read(GUI / "tray_window.py")
    for forbidden in (
        "QTimer",
        "while True",
        "sqlite3",
        "subprocess",
        "socket",
        "requests",
        "OperatorApi(",
        "git.exe",
        "powershell.exe",
    ):
        assert forbidden not in combined
    assert "control_finished.connect" in combined
    assert "prepare_finished.connect" in combined
    assert "diagnostics_export_finished.connect" in combined


def test_close_to_tray_and_exit_are_distinct_paths() -> None:
    tray = read(GUI / "tray.py")
    window = read(GUI / "tray_window.py")
    assert "event.ignore()" in tray
    assert 'ExitChoice = Literal["leave", "stop", "cancel"]' in tray
    assert "request_confirmed_stop_for_exit" in tray
    assert "request_confirmed_stop_for_exit" in window
    assert "force_close" in tray
    assert "_force_close_requested" in window


def test_headless_smoke_never_creates_tray_and_uses_composed_explicit_windows() -> None:
    app = read(GUI / "app.py")
    session_window = read(GUI / "session_history_window.py")
    assert "if args.headless_smoke:" in app
    assert "window = VNextControlCenterWindow(runtime_root=runtime_root)" in app
    assert "SessionProjectControlCenterWindow" not in app
    assert "SessionTrayProjectControlCenterWindow" not in app
    assert "class SessionTrayProjectControlCenterWindow" in session_window
    assert "auto_load_status" not in app
    assert '"tray_created": False' in app
    assert "tray_controller.start()" not in app


def test_tray_modules_are_valid_python() -> None:
    for name in ("tray.py", "tray_window.py"):
        ast.parse(read(GUI / name), filename=name)

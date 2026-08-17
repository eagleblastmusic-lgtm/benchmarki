from __future__ import annotations

import argparse
import json
import os
import platform
from pathlib import Path
from typing import Any, Sequence

from bdb_vnext.composition import default_vnext_runtime_root

from .version import APPLICATION_VERSION


SMOKE_SCHEMA = "bdb-control-center-smoke-v1"
LEGACY_CONTROL_CENTER_RETIRED_CODE = "legacy_control_center_retired_cc2"


def _default_workspaces_root() -> Path:
    """Retained CLI-compatibility value; CC2 never reads it as active state."""

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "BartoszDevBridge" / "workspaces"
    return Path.home() / ".local" / "share" / "BartoszDevBridge" / "workspaces"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BDB Control Center — canonical vNext projection")
    parser.add_argument(
        "--runtime-root",
        default=str(default_vnext_runtime_root()),
        help="Existing vNext runtime root. Control Center never creates or repairs it.",
    )
    parser.add_argument(
        "--workspaces-root",
        default=str(_default_workspaces_root()),
        help=(
            "Retired legacy CLI compatibility argument. CC2 ignores this path and "
            "never interprets it as active/current state."
        ),
    )
    parser.add_argument(
        "--legacy-control-center",
        action="store_true",
        help=(
            "Retired by CC2. The flag is retained only as a fail-closed CLI tombstone; "
            "it never imports or launches legacy Control Center semantics."
        ),
    )
    parser.add_argument(
        "--headless-smoke",
        action="store_true",
        help="Run the canonical vNext GUI shell through an offscreen bootstrap and exit",
    )
    parser.add_argument("--json-out", help="Optional path for the headless/fail-closed report")
    parser.add_argument("--smoke-timeout-ms", type=int, default=15_000)
    return parser


def _render_report(report: dict[str, Any], output_path: str | None) -> None:
    report.setdefault("application_version", APPLICATION_VERSION)
    rendered = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
    print(rendered)
    if output_path:
        path = Path(output_path).expanduser().resolve(strict=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n", encoding="utf-8")


def _retired_legacy_report() -> dict[str, Any]:
    return {
        "schema": SMOKE_SCHEMA,
        "status": "failed",
        "application_version": APPLICATION_VERSION,
        "error_code": LEGACY_CONTROL_CENTER_RETIRED_CODE,
        "error": (
            "CC2 retired active legacy Control Center interpretation after M9a. "
            "Legacy state is archive-only/non-authority and cannot be launched as current state."
        ),
        "legacy_control_center": True,
        "legacy_active_interpretation": False,
        "legacy_fallback": False,
        "archive_only": True,
        "read_only": True,
        "mutation_operations_invoked": 0,
        "vnext_activation_allowed": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 1_000 <= args.smoke_timeout_ms <= 120_000:
        report = {
            "schema": SMOKE_SCHEMA,
            "status": "failed",
            "application_version": APPLICATION_VERSION,
            "error_code": "invalid_smoke_timeout",
            "error": "smoke-timeout-ms must be between 1000 and 120000",
        }
        _render_report(report, args.json_out)
        return 2

    # CC2 is a hard semantic boundary: the historical switch may remain parseable
    # for CLI compatibility, but it must fail before PySide or any legacy GUI module
    # can be imported. There is no active legacy fallback after M9a PASS_CLOSED.
    if args.legacy_control_center:
        _render_report(_retired_legacy_report(), args.json_out)
        return 3

    if args.headless_smoke:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    try:
        import PySide6
        from PySide6.QtCore import QTimer, qVersion
        from PySide6.QtWidgets import QApplication

        from .vnext_control_center import VNextControlCenterWindow
    except ImportError as error:
        report = {
            "schema": SMOKE_SCHEMA,
            "status": "failed",
            "application_version": APPLICATION_VERSION,
            "error_code": "pyside6_missing",
            "error": str(error),
            "install_hint": 'python -m pip install -e ".[gui]"',
            "legacy_fallback": False,
        }
        _render_report(report, args.json_out)
        return 2

    runtime_root = str(Path(args.runtime_root).expanduser().absolute())
    workspaces_root = str(Path(args.workspaces_root).expanduser().resolve(strict=False))
    application = QApplication.instance() or QApplication(["bdb-control-center"])
    application.setApplicationName("BDB Control Center")
    application.setApplicationVersion(APPLICATION_VERSION)
    application.setOrganizationName("Bartosz Dev Bridge")
    application.setQuitOnLastWindowClosed(True)

    window = VNextControlCenterWindow(runtime_root=runtime_root)

    report: dict[str, Any] = {}
    timed_out = False

    def finish_smoke() -> None:
        if not args.headless_smoke or report:
            return
        report.update(window.smoke_report())
        report.update(
            {
                "schema": SMOKE_SCHEMA,
                "status": "success" if report["bootstrap_ok"] else "failed",
                "application_version": APPLICATION_VERSION,
                "runtime_root": runtime_root,
                "workspaces_root": workspaces_root,
                "legacy_control_center": False,
                "legacy_active_interpretation": False,
                "qt_version": qVersion(),
                "pyside_version": PySide6.__version__,
                "python_version": platform.python_version(),
                "qt_platform": os.environ.get("QT_QPA_PLATFORM") or "native",
                "tray_created": False,
            }
        )
        window.close()
        QTimer.singleShot(0, application.quit)

    def fail_timeout() -> None:
        nonlocal timed_out
        if not args.headless_smoke or report:
            return
        timed_out = True
        report.update(
            {
                "schema": SMOKE_SCHEMA,
                "status": "failed",
                "application_version": APPLICATION_VERSION,
                "error_code": "bootstrap_timeout",
                "runtime_root": runtime_root,
                "bootstrap_completed": False,
                "mutation_operations_invoked": 0,
                "legacy_fallback": False,
                "legacy_active_interpretation": False,
                "tray_created": False,
            }
        )
        window.close()
        application.quit()

    if args.headless_smoke:
        window.dashboard_ready.connect(finish_smoke)
        QTimer.singleShot(args.smoke_timeout_ms, fail_timeout)

    window.show()
    window.start_bootstrap()
    exit_code = int(application.exec())

    if args.headless_smoke:
        report.setdefault("schema", SMOKE_SCHEMA)
        report.setdefault("status", "failed")
        report.setdefault("application_version", APPLICATION_VERSION)
        report.setdefault("legacy_fallback", False)
        report.setdefault("legacy_active_interpretation", False)
        report["event_loop_exit_code"] = exit_code
        report["timed_out"] = timed_out
        _render_report(report, args.json_out)
        return 0 if report.get("status") == "success" and exit_code == 0 else 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

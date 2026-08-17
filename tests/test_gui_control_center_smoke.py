from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def run_smoke(runtime_root: Path, report_path: Path) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["QT_QPA_PLATFORM"] = "offscreen"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "bdb_gui.app",
            "--runtime-root",
            str(runtime_root),
            "--headless-smoke",
            "--smoke-timeout-ms",
            "15000",
            "--json-out",
            str(report_path),
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_cc1_missing_runtime_is_safe_off_headless_smoke(tmp_path: Path) -> None:
    pytest.importorskip("PySide6")
    runtime_root = tmp_path / "vnext-not-deployed"
    report_path = tmp_path / "control-center-smoke.json"

    completed = run_smoke(runtime_root, report_path)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["schema"] == "bdb-control-center-smoke-v1"
    assert report["status"] == "success"
    assert report["application_version"] == "0.3.1"
    assert report["window_object_name"] == "BdbControlCenterWindow"
    assert report["window_constructed"] is True
    assert report["read_only_startup"] is True
    assert report["bootstrap_completed"] is True
    assert report["bootstrap_ok"] is True
    assert report["semantic_source"] == "bdb_vnext.control_center_query"
    assert report["legacy_fallback"] is False
    assert report["work_count"] == 0
    assert report["status_vector"]["system"] == "OFF"
    assert report["status_vector"]["control_store"] == "ABSENT"
    assert report["actions_enabled"] is False
    assert report["mutation_operations_invoked"] == 0
    assert report["auto_resume_invoked"] is False
    assert report["qt_platform"] == "offscreen"
    assert report["event_loop_exit_code"] == 0
    assert report["timed_out"] is False
    assert not runtime_root.exists()


def test_cc1_partial_runtime_is_degraded_without_repair(tmp_path: Path) -> None:
    pytest.importorskip("PySide6")
    runtime_root = tmp_path / "partial-vnext"
    seal = runtime_root / "control" / "control.db.seal.json"
    seal.parent.mkdir(parents=True)
    seal.write_text("{}", encoding="utf-8")
    report_path = tmp_path / "partial-runtime-smoke.json"

    completed = run_smoke(runtime_root, report_path)

    assert completed.returncode == 1, completed.stdout + completed.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["bootstrap_completed"] is True
    assert report["bootstrap_ok"] is False
    assert report["bootstrap_error_code"] == "control_store_partial"
    assert report["legacy_fallback"] is False
    assert report["mutation_operations_invoked"] == 0
    assert not (runtime_root / "control" / "control.db").exists()

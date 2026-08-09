from __future__ import annotations

import ast
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from bdb_vnext.x1_sqlite_experiment import (
    X1_CONTROL_SCHEMA,
    X1ExperimentError,
    X1_SCHEMA,
    backup_real_control_store,
    initialize_control_store,
    inspect_control_store,
    run_contention_experiment,
    run_crash_boundary,
    run_experiment,
    write_committed_row,
)


@pytest.mark.parametrize(
    "phase,expected",
    [
        ("before_begin", ()),
        ("after_begin", ()),
        ("after_mutation", ()),
        ("before_commit", ()),
        ("after_commit", ("crash-after_commit",)),
    ],
)
def test_real_windows_process_kill_boundary_reopens_with_expected_sqlite_truth(
    tmp_path: Path, phase: str, expected: tuple[str, ...]
) -> None:
    evidence = run_crash_boundary(
        tmp_path,
        phase=phase,
        token=f"crash-{phase}",
    )

    assert evidence.process_killed is True
    assert evidence.expected_tokens == expected
    assert evidence.observed_tokens == expected
    assert evidence.integrity_check == "ok"
    assert evidence.recovery_committed is True


def test_single_writer_lease_and_raw_sqlite_contention_are_bounded(tmp_path: Path) -> None:
    evidence = run_contention_experiment(tmp_path)

    assert evidence["canonical_contender"] == {
        "code": "concurrent_attempt",
        "status": "blocked",
    }
    assert evidence["raw_sqlite_contender"]["status"] == "busy"
    assert evidence["raw_sqlite_contender"]["is_locked"] is True
    assert evidence["second_authority_committed"] is False
    assert evidence["post_contention_integrity"]["integrity_check"] == "ok"


def test_post_x1_requires_real_control_db_and_does_not_promote_declared_absence(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()

    with pytest.raises(X1ExperimentError) as raised:
        backup_real_control_store(runtime, tmp_path / "authority", backup_id="missing-db")

    assert raised.value.code == "post_x1_database_required"


def test_real_sqlite_fixture_has_minimal_application_and_integrity_invariants(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime" / "control" / "control.db"
    lock = tmp_path / "runtime" / "coordination" / "x1-writer.lock"
    settings = initialize_control_store(database)
    write_committed_row(database, lock, "fixture-row")

    report = inspect_control_store(database, expected_tokens=("fixture-row",))

    assert settings.journal_mode == "wal"
    assert settings.synchronous == 2
    assert settings.wal_autocheckpoint == 0
    assert settings.foreign_keys == 1
    assert report.integrity_check == "ok"
    assert report.journal_mode == "wal"
    assert report.schema_version == str(X1_CONTROL_SCHEMA)
    assert report.tokens == ("fixture-row",)
    assert report.writer_id == "x1-canonical-writer"


def test_complete_x1_capsule_is_pass_with_explicit_limitations(tmp_path: Path) -> None:
    evidence = run_experiment(tmp_path / "capsule")

    assert evidence["schema"] == X1_SCHEMA
    assert evidence["status"] == "PASS"
    assert all(value in {"PASS", "PASS for coordinated Windows process-kill boundaries; native COMMIT interruption and physical power-loss not claimed"} for value in evidence["hypotheses"].values())
    assert evidence["fault_matrix"]["missing_subject_restore_blocked"] is True
    assert evidence["fault_matrix"]["cases"] == {
        "missing_wal": "backup_integrity_failure",
        "truncated_db": "backup_integrity_failure",
        "truncated_wal": "backup_integrity_failure",
        "corrupt_db": "backup_integrity_failure",
        "corrupt_wal": "backup_integrity_failure",
    }
    assert evidence["fault_matrix"]["missing_subject_restore_error"] == "backup_integrity_failure"
    assert evidence["m1b_real_wal_backup_restore"]["restore_verified"] is True
    assert evidence["m1b_real_wal_backup_restore"]["restored_integrity"]["integrity_check"] == "ok"
    assert evidence["m1b_legal_absent_wal_backup_restore"]["restore_verified"] is True
    assert evidence["post_x1_storage_decision"]["control_db"] == "REQUIRED_PRESENT"
    assert evidence["authority"]["second_authority"] is False
    assert evidence["authority"]["production_activation"] is False
    assert evidence["authority"]["legacy_touched"] is False


def test_x1_module_has_no_legacy_or_activation_dependency() -> None:
    root = Path(__file__).resolve().parents[1]
    source_path = root / "bdb_vnext" / "x1_sqlite_experiment.py"
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert "bdb_bridge" not in imported
    assert "bdb_release" not in imported
    assert "openai" not in imported
    assert "active_slot" not in source
    assert "previous_slot" not in source
    assert "production_activation" in source


def test_x1_artifact_and_governance_plan_parse() -> None:
    root = Path(__file__).resolve().parents[1]
    plan = (root / "docs" / "x1-vnext-experiment.md").read_text(encoding="utf-8")
    assert "H1" in plan and "H6" in plan
    assert "Falsifier" in plan
    module = ast.parse((root / "bdb_vnext" / "x1_sqlite_experiment.py").read_text(encoding="utf-8"))
    assert module.body
    assert json.loads(
        json.dumps(
            {
                "sqlite_version": sqlite3.sqlite_version,
                "python": sys.version_info[:2],
            }
        )
    )["sqlite_version"]

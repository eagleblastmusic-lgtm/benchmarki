from __future__ import annotations

from pathlib import Path

import pytest

from bdb_vnext.control_center_query import (
    ActionPredicate,
    ControlCenterSnapshot,
    ControlCenterWorkProjection,
    read_control_center_snapshot,
)


def test_missing_runtime_is_explicit_off_and_never_created(tmp_path: Path) -> None:
    root = tmp_path / "missing-vnext"
    snapshot = read_control_center_snapshot(root)
    assert snapshot.system_state == "OFF"
    assert snapshot.writer_state == "OFF"
    assert snapshot.activation_state == "OFF"
    assert snapshot.store_state == "ABSENT"
    assert snapshot.reason_code == "control_store_absent"
    assert snapshot.works == ()
    assert all(not action.enabled for action in snapshot.action_predicates)
    assert not root.exists()


def test_partial_external_seal_fails_closed_without_database(tmp_path: Path) -> None:
    root = tmp_path / "partial-vnext"
    seal = root / "control" / "control.db.seal.json"
    seal.parent.mkdir(parents=True)
    seal.write_text("{}", encoding="utf-8")
    with pytest.raises(Exception) as captured:
        read_control_center_snapshot(root)
    assert getattr(captured.value, "code", None) == "control_store_partial"
    assert not (root / "control" / "control.db").exists()


@pytest.mark.parametrize("state", ["OFF", "ON", "PAUSED", "DEGRADED"])
def test_status_vector_accepts_control_center_states(state: str) -> None:
    snapshot = ControlCenterSnapshot(
        "C:/example",
        state,
        "OFF",
        "OFF",
        "SEALED",
        "control-test",
        (),
        (ActionPredicate("resume"),),
    )
    document = snapshot.as_dict()
    assert document["status_vector"]["system"] == state
    assert document["read_only"] is True
    assert document["legacy_fallback"] is False
    assert document["mutation_operations_invoked"] == 0


def test_work_projection_keeps_canonical_m4a_query_intact() -> None:
    work_query = {
        "schema": "bdb-vnext-m4a-work-query-v1",
        "authority": "devmaster.bdb.vnext.work-kernel",
        "protocol_generation": "bdb-vnext-protocol-v1",
        "work": {
            "work_id": "work-1",
            "task_id": "task-1",
            "kind": "engineering",
            "disposition": "WAITING",
            "state_version": 7,
        },
        "active_run": None,
        "last_run": None,
        "active_wait": {"wait_id": "wait-1"},
        "lease": None,
        "resource_claim": None,
        "recent_facts": [],
        "query_digest": "sha256:" + "1" * 64,
    }
    projection = ControlCenterWorkProjection(
        work_query,
        {
            "selection": "ALL_CANONICAL_CANDIDATES",
            "items": [{"effect_id": "effect-1", "effect_certainty": "POSSIBLE"}],
        },
        None,
        None,
        None,
    )
    document = projection.as_dict()
    assert projection.work_id == "work-1"
    assert projection.task_id == "task-1"
    assert document["work"]["authority"] == "devmaster.bdb.vnext.work-kernel"
    assert document["work"]["work"]["disposition"] == "WAITING"
    assert document["work"]["active_wait"]["wait_id"] == "wait-1"
    assert "projection_digest" in document


def test_projection_never_invents_a_current_candidate() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "bdb_vnext"
        / "control_center_query.py"
    ).read_text(encoding="utf-8")
    assert "ORDER BY rowid" not in source
    assert "latest candidate" not in source.lower()
    assert "ALL_CANONICAL_CANDIDATES" in source


def test_cc1_consumes_m4a_read_adapter_not_raw_work_lifecycle() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "bdb_vnext"
        / "control_center_query.py"
    ).read_text(encoding="utf-8")
    assert "ReadOnlyWorkKernelQuery" in source
    for token in (
        "FROM m4a_work_items",
        "FROM m4a_runs",
        "FROM m4a_waits",
        "FROM m4a_leases",
    ):
        assert token not in source


def test_query_module_has_no_mutation_vocabulary() -> None:
    import inspect

    import bdb_vnext.control_center_query as module

    source = inspect.getsource(module)
    for token in (
        "INSERT INTO",
        "UPDATE m4",
        "DELETE FROM",
        "CREATE TABLE",
        "ALTER TABLE",
        "BEGIN IMMEDIATE",
        ".apply(",
        ".resume(",
        ".publish(",
    ):
        assert token not in source


def test_gui_default_is_vnext_and_legacy_is_explicit_only() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "bdb_gui" / "vnext_control_center.py").read_text(encoding="utf-8")
    app = (root / "bdb_gui" / "app.py").read_text(encoding="utf-8")

    assert "bdb_operator" not in source
    assert "sqlite3" not in source
    assert "sqlite3" not in app
    assert '"--legacy-control-center"' in app
    assert "if args.legacy_control_center:" in app
    assert "window = VNextControlCenterWindow(runtime_root=runtime_root)" in app
    assert "SessionProjectControlCenterWindow" not in app
    assert "ProjectOperationsService" not in app
    assert "PAGE_NAMES" in source
    for page in ("Dashboard", "Projects", "Current operation", "History", "Diagnostics", "Settings / System"):
        assert page in source
    assert "legacy_fallback" in app

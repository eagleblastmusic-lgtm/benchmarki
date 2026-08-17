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
        runtime_root="C:/example",
        system_state=state,
        writer_state="OFF",
        activation_state="OFF",
        store_state="SEALED",
        store_instance_id="control-test",
        works=(),
        action_predicates=(ActionPredicate("resume"),),
    )

    document = snapshot.as_dict()

    assert document["status_vector"]["system"] == state
    assert document["read_only"] is True
    assert document["legacy_fallback"] is False
    assert document["mutation_operations_invoked"] == 0


def test_work_projection_preserves_domain_vectors_without_action_authority() -> None:
    projection = ControlCenterWorkProjection(
        work={"work_id": "work-1", "task_id": "task-1", "kind": "engineering", "disposition": "WAITING"},
        effect={"effect_id": "effect-1", "state": "POSSIBLE", "effect_certainty": "POSSIBLE"},
        evidence={"record": {"evidence_id": "ev-1"}, "evaluation": {"result": "INCONCLUSIVE"}},
        repository={"base": {"view_id": "sha256:" + "1" * 64}, "candidate": None},
        publication=None,
    )

    document = projection.as_dict()

    assert document["work"]["disposition"] == "WAITING"
    assert document["effect"]["effect_certainty"] == "POSSIBLE"
    assert document["evidence"]["evaluation"]["result"] == "INCONCLUSIVE"
    assert document["repository"]["base"]["view_id"].startswith("sha256:")
    assert document["publication"] is None
    assert "projection_digest" in document


def test_query_module_has_no_mutation_vocabulary() -> None:
    import inspect
    import bdb_vnext.control_center_query as module

    source = inspect.getsource(module)

    forbidden = (
        "INSERT INTO",
        "UPDATE m4",
        "DELETE FROM",
        "CREATE TABLE",
        "ALTER TABLE",
        "BEGIN IMMEDIATE",
        ".apply(",
        ".resume(",
        ".publish(",
    )
    for token in forbidden:
        assert token not in source


def test_gui_boundary_has_no_legacy_operator_or_sqlite_dependency() -> None:
    source = (Path(__file__).resolve().parents[1] / "bdb_gui" / "vnext_control_center.py").read_text(encoding="utf-8")
    app = (Path(__file__).resolve().parents[1] / "bdb_gui" / "app.py").read_text(encoding="utf-8")

    assert "bdb_operator" not in source
    assert "bdb_operator" not in app
    assert "sqlite3" not in source
    assert "sqlite3" not in app
    assert "SessionProjectControlCenterWindow" not in app
    assert "ProjectOperationsService" not in app

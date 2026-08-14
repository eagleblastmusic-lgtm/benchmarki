from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from bdb_vnext.composition import build_vnext_composition_manifest
from bdb_vnext.control_store import ControlStoreError, seal_path_for_database
from bdb_vnext.m3a_submission import ShadowSubmissionRequest
from bdb_vnext.m4c_evidence import EvidenceError, EvidenceStore
from bdb_vnext.m4a_work_kernel import M4aError
from bdb_vnext.n4_publication import N4Error
from bdb_vnext.provider_root import VNextCompositionRoot


ROOT = Path(__file__).resolve().parents[1]


def _root(tmp_path: Path) -> VNextCompositionRoot:
    runtime = tmp_path / "runtime"
    legacy = tmp_path / "legacy"
    return VNextCompositionRoot.from_manifest(build_vnext_composition_manifest(
        source_commit="4" * 40,
        runtime_root=runtime,
        legacy_runtime_root=legacy,
        forbidden_roots=[ROOT],
    ))


def _request(key: str) -> ShadowSubmissionRequest:
    return ShadowSubmissionRequest(
        submission_key=key,
        intent_revision="r1",
        intent={"operation": "p0"},
        conversation_binding={"conversation_id": key},
        consumer_binding={"consumer_id": key, "kind": "browser"},
    )


def test_control_db_seal_rejects_downgrade_missing_seal_and_missing_db(tmp_path: Path) -> None:
    root = _root(tmp_path)
    with root.open_control_plane() as plane:
        database = plane.work_kernel.database_path
    seal = seal_path_for_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version=0")
        connection.commit()
    with pytest.raises(ControlStoreError) as downgraded:
        root.open_control_plane()
    assert downgraded.value.code in {"control_user_version_mismatch", "control_seal_mismatch"}

    root2 = _root(tmp_path / "missing-seal")
    with root2.open_control_plane() as plane:
        database2 = plane.work_kernel.database_path
    seal_path_for_database(database2).unlink()
    with pytest.raises(ControlStoreError) as missing_seal:
        root2.open_control_plane()
    assert missing_seal.value.code == "control_seal_missing"

    root3 = _root(tmp_path / "missing-db")
    with root3.open_control_plane() as plane:
        database3 = plane.work_kernel.database_path
    seal3 = seal_path_for_database(database3)
    database3.unlink()
    with pytest.raises(ControlStoreError) as missing_db:
        root3.open_control_plane()
    assert missing_db.value.code == "control_seal_mismatch"
    assert seal3.exists()
    assert not database3.exists()

    root4 = _root(tmp_path / "missing-seal-db")
    with root4.open_control_plane() as plane:
        database4 = plane.work_kernel.database_path
    before = database4.read_bytes()
    seal4 = seal_path_for_database(database4)
    seal4.unlink()
    with pytest.raises(ControlStoreError) as db_only:
        root4.open_control_plane()
    assert db_only.value.code == "control_seal_missing"
    assert database4.read_bytes() == before

    root5 = _root(tmp_path / "altered-seal")
    with root5.open_control_plane() as plane:
        database5 = plane.work_kernel.database_path
    seal5 = seal_path_for_database(database5)
    original_files = {item.name for item in database5.parent.iterdir()}
    document5 = json.loads(seal5.read_text(encoding="utf-8"))
    document5["store_id"] = "foreign-control-store"
    seal5.write_text(json.dumps(document5), encoding="utf-8")
    before5 = database5.read_bytes()
    with pytest.raises(ControlStoreError) as altered:
        root5.open_control_plane()
    assert altered.value.code in {"control_seal_integrity_failure", "control_seal_mismatch"}
    assert database5.read_bytes() == before5
    assert {item.name for item in database5.parent.iterdir()} == original_files | {seal5.name}


def test_evidence_replay_binds_raw_and_timing_inputs(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    with EvidenceStore(runtime) as evidence:
        common = dict(
            request_id="p0:evidence",
            primary_subject_kind="N6_BROWSER_RUN",
            primary_subject_identity={"run_id": "p0:run"},
            candidate_view_id=None,
            checker_id="p0-checker",
            checker_version="1",
            checker_code_digest="sha256:" + "1" * 64,
            environment={"surface": "test"},
            observation_started_at="2026-08-13T00:00:00Z",
            observation_finished_at="2026-08-13T00:00:01Z",
            completeness="COMPLETE",
            applicability="APPLICABLE",
            status="CAPTURED",
        )
        first = evidence.record_observation(raw_observation={"answer": "one"}, **common)
        assert evidence.record_observation(raw_observation={"answer": "one"}, **common).evidence_id == first.evidence_id
        with pytest.raises(EvidenceError) as raw_conflict:
            evidence.record_observation(raw_observation={"answer": "two"}, **common)
        assert raw_conflict.value.code == "evidence_request_conflict"
        changed = {**common, "observation_started_at": "2026-08-13T00:00:02Z"}
        with pytest.raises(EvidenceError) as time_conflict:
            evidence.record_observation(raw_observation={"answer": "one"}, **changed)
        assert time_conflict.value.code == "evidence_request_conflict"


def test_shared_busy_floor_is_typed_and_leaves_no_evidence_row(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    with EvidenceStore(runtime) as evidence:
        blocker = sqlite3.connect(evidence.database_path, timeout=0.01, isolation_level=None)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            with pytest.raises(EvidenceError) as busy:
                evidence.record_observation(
                    request_id="p0:busy",
                    primary_subject_kind="N6_BROWSER_RUN",
                    primary_subject_identity={"run_id": "p0:busy"},
                    candidate_view_id=None,
                    raw_observation={"answer": "busy"},
                    checker_id="p0-checker",
                    checker_version="1",
                    checker_code_digest="sha256:" + "2" * 64,
                    environment={"surface": "test"},
                    observation_started_at="2026-08-13T00:00:00Z",
                    observation_finished_at="2026-08-13T00:00:01Z",
                    completeness="COMPLETE",
                    applicability="APPLICABLE",
                    status="CAPTURED",
                )
            assert busy.value.code == "database_busy"
            assert evidence._connection.execute("SELECT COUNT(*) FROM m4c_evidence_records WHERE request_id='p0:busy'").fetchone()[0] == 0
        finally:
            blocker.rollback()
            blocker.close()
        assert evidence.record_observation(
            request_id="p0:busy",
            primary_subject_kind="N6_BROWSER_RUN",
            primary_subject_identity={"run_id": "p0:busy"},
            candidate_view_id=None,
            raw_observation={"answer": "busy"},
            checker_id="p0-checker",
            checker_version="1",
            checker_code_digest="sha256:" + "2" * 64,
            environment={"surface": "test"},
            observation_started_at="2026-08-13T00:00:00Z",
            observation_finished_at="2026-08-13T00:00:01Z",
            completeness="COMPLETE",
            applicability="APPLICABLE",
            status="CAPTURED",
        ).request_id == "p0:busy"


def test_evidence_gap_writer_uses_the_shared_busy_floor(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    with EvidenceStore(runtime) as evidence:
        blocker = sqlite3.connect(evidence.database_path, timeout=0.01, isolation_level=None)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            with pytest.raises(EvidenceError) as busy:
                evidence.record_gap(
                    primary_subject_kind="P0",
                    primary_subject_identity={"id": "busy"},
                    reason="unavailable",
                    details={"source": "test"},
                )
            assert busy.value.code == "database_busy"
            assert evidence._connection.execute("SELECT COUNT(*) FROM m4c_evidence_gaps").fetchone()[0] == 0
        finally:
            blocker.rollback()
            blocker.close()


def test_work_and_publication_busy_floor_are_typed(tmp_path: Path) -> None:
    root = _root(tmp_path)
    with root.open_control_plane() as plane:
        receipt = plane.admission.authority.admit(_request("p0:work"))
        work = plane.work_kernel.create_work_item("p0:work:item", receipt.task_id)
        publication = plane.publication.publish(
            request_id="p0:publication",
            task_id=receipt.task_id,
            work_id=work.work_id,
            intent_revision_id=receipt.intent_revision_id,
            result_payload={"p0": True},
            consumer_id="p0-browser",
            consumer_kind="BROWSER",
            conversation_id="p0-conversation",
        )
        blocker = sqlite3.connect(plane.publication.database_path, timeout=0.01, isolation_level=None)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            with pytest.raises(M4aError) as work_busy:
                plane.work_kernel.acquire_lease(work.work_id, "p0:lease", "p0:worker")
            assert getattr(work_busy.value, "code", None) == "database_busy"
            with pytest.raises(N4Error) as publication_busy:
                plane.publication.bind_consumer(
                    publication_id=publication.publication_id,
                    consumer_id="p0-operator",
                    consumer_kind="OPERATOR",
                )
            assert publication_busy.value.code == "database_busy"
            assert plane.publication.get_binding(publication.publication_id, "p0-operator") is None
        finally:
            blocker.rollback()
            blocker.close()

from __future__ import annotations

import json
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from bdb_vnext.candidate import CandidateError, CandidateStore
from bdb_vnext.m3a_submission import ShadowSubmissionRequest
from bdb_vnext.m3c_admission import open_vnext_admission_composition
from bdb_vnext.m4a_work_kernel import WorkKernelStore
from bdb_vnext.m4c_evidence import EvidenceError, EvidenceStore, MinimumCandidateChecker
from bdb_vnext.repo_view import RepositoryResource


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _subject(root: Path) -> Path:
    repo = root / "subject"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "M4c Test")
    _git(repo, "config", "user.email", "m4c@example.invalid")
    (repo / "one.txt").write_bytes(b"one\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return repo


@contextmanager
def _stack(tmp_path: Path):
    runtime = tmp_path / "runtime"
    legacy = tmp_path / "legacy"
    subject = _subject(tmp_path)
    admission = open_vnext_admission_composition(runtime, legacy_root=legacy)
    receipt = admission.authority.admit(ShadowSubmissionRequest(
        submission_key="m4c:submission",
        intent_revision="r1",
        intent={"operation": "candidate-check"},
        conversation_binding={"conversation_id": "m4c"},
        consumer_binding={"consumer_id": "m4c", "kind": "browser"},
    ))
    kernel = WorkKernelStore.open(runtime, task_authority=admission.authority, legacy_root=legacy, clock=lambda: 100.0)
    candidate_store = CandidateStore(runtime, work_kernel=kernel)
    evidence = EvidenceStore(runtime, content_store=candidate_store.content_store, candidate_store=candidate_store)
    view = RepositoryResource.from_path(subject, repository_id="m4c-subject").resolve_committed("HEAD")
    work = kernel.create_work_item("work:m4c", receipt.task_id)
    lease = kernel.acquire_lease(work.work_id, "lease:m4c", "worker:m4c")
    workspace = candidate_store.create_workspace(candidate_id="candidate:m4c", base_view=view)
    prepared = candidate_store.prepare(
        candidate_id="candidate:m4c", work_id=work.work_id, task_id=receipt.task_id,
        lease_id=lease.lease_id, fence=lease.fence, base_view=view,
        workspace_root=workspace, replacements={"one.txt": b"checked\n"},
    )
    candidate_store.apply(prepared.candidate_id)
    _sealed, candidate = candidate_store.seal(prepared.candidate_id, base_view=view)
    try:
        yield runtime, subject, candidate_store, evidence, candidate
    finally:
        evidence.close()
        candidate_store.close()
        kernel.close()
        admission.close()
        _git(subject, "worktree", "remove", "--force", str(workspace)) if workspace.exists() else None


def test_exact_candidate_checker_persists_raw_evaluation_and_disposition(tmp_path: Path) -> None:
    with _stack(tmp_path) as (runtime, _subject_path, candidate_store, evidence, candidate):
        checker = MinimumCandidateChecker(Path(__file__).parents[1], evidence)
        evaluation = checker.check(candidate, request_id="m4c:check:one")
        query = evidence.query(evaluation.evidence_id)
        assert evaluation.result == "PASS"
        assert query["effective_disposition"] == "PASS"
        assert query["applicability"]["applicable"] is True
        assert evidence.raw_observation(evaluation.evidence_id)
        replay = checker.check(candidate, request_id="m4c:check:one")
        assert replay.evaluation_id == evaluation.evaluation_id
        assert len(evidence.evaluations(evaluation.evidence_id)) == 1
        assert evidence.get(evaluation.evidence_id).candidate_view_id == candidate.view_id  # type: ignore[union-attr]


def test_replayed_request_cannot_cross_subject_or_result_identity(tmp_path: Path) -> None:
    with _stack(tmp_path) as (_runtime, _subject_path, _candidate_store, evidence, candidate):
        checker = MinimumCandidateChecker(Path(__file__).parents[1], evidence)
        first = checker.check(candidate, request_id="m4c:check:identity")
        record = evidence.get(first.evidence_id)
        assert record is not None
        with pytest.raises(EvidenceError) as caught:
            evidence.record_observation(
                request_id="m4c:check:identity",
                primary_subject_kind="CANDIDATE",
                primary_subject_identity={"candidate_id": "foreign", "view_id": candidate.view_id},
                candidate_view_id=candidate.view_id,
                raw_observation={"schema": "m4c-raw-observation-v1", "subject": "foreign"},
                checker_id=record.checker_id,
                checker_version=record.checker_version,
                checker_code_digest=record.checker_code_digest,
                environment=record.environment,
                observation_started_at=record.observation_started_at,
                observation_finished_at=record.observation_finished_at,
                completeness="COMPLETE",
                applicability="APPLICABLE",
                status="CHECKED",
            )
        assert caught.value.code == "evidence_request_conflict"
        with pytest.raises(EvidenceError) as caught:
            evidence.evaluate(
                evidence_id=first.evidence_id,
                evaluator_id=first.evaluator_id,
                evaluator_version=first.evaluator_version,
                evaluator_code_digest=first.evaluator_code_digest,
                config_digest=first.config_digest,
                result="FAIL",
                applicability="APPLICABLE",
                detail={"changed": True},
            )
        assert caught.value.code == "evaluation_identity_conflict"


def test_environment_or_checker_failure_is_inconclusive_not_pass(tmp_path: Path) -> None:
    with _stack(tmp_path) as (_runtime, _subject_path, _candidate_store, evidence, candidate):
        checker = MinimumCandidateChecker(Path(__file__).parents[1], evidence)
        evaluation = checker.check(candidate, request_id="m4c:check:environment", fault="dependency_failure")
        assert evaluation.result == "INCONCLUSIVE"
        assert evidence.query(evaluation.evidence_id)["effective_disposition"] == "INCONCLUSIVE"
        with pytest.raises(EvidenceError) as caught:
            checker.check(candidate, request_id="m4c:check:spawn", fault="before_spawn")
        assert caught.value.code == "checker_not_started"


def test_lost_response_replays_committed_identity_and_stale_candidate_cannot_pass(tmp_path: Path) -> None:
    with _stack(tmp_path) as (_runtime, subject, candidate_store, evidence, candidate):
        checker = MinimumCandidateChecker(Path(__file__).parents[1], evidence)
        with pytest.raises(EvidenceError) as caught:
            checker.check(candidate, request_id="m4c:check:lost", fault="lost_response")
        assert caught.value.code == "evaluation_response_lost"
        query = evidence.query(caught.value.details["evaluation_id"] if False else evidence.evaluations(next(iter(evidence._connection.execute("SELECT evidence_id FROM m4c_evidence_records WHERE request_id='m4c:check:lost'")))[0])[0].evidence_id)
        assert query["effective_disposition"] == "PASS"
        workspace = Path(candidate_store.get("candidate:m4c").workspace_root)  # type: ignore[union-attr]
        (workspace / "one.txt").write_bytes(b"tampered\n")
        candidate_store.invalidate_if_changed("candidate:m4c")
        assert evidence.query(next(iter(evidence._connection.execute("SELECT evidence_id FROM m4c_evidence_records WHERE request_id='m4c:check:lost'")))[0])["effective_disposition"] == "INCONCLUSIVE"
        assert _git(subject, "status", "--short", "--branch")


def test_raw_cas_integrity_gap_cannot_remain_current_pass(tmp_path: Path) -> None:
    with _stack(tmp_path) as (_runtime, _subject_path, _candidate_store, evidence, candidate):
        checker = MinimumCandidateChecker(Path(__file__).parents[1], evidence)
        evaluation = checker.check(candidate, request_id="m4c:check:raw-tamper")
        record = evidence.get(evaluation.evidence_id)
        assert record is not None
        record.raw_ref  # keep the immutable reference part of the assertion surface
        evidence.content_store.object_path(record.raw_ref).write_bytes(b"tampered raw evidence")
        query = evidence.query(evaluation.evidence_id)
        assert query["effective_disposition"] == "INCONCLUSIVE"
        assert query["applicability"] == {"applicable": False, "reason": "raw_evidence_integrity_failure"}


def test_evaluator_versions_are_immutable_and_superseded_explicitly(tmp_path: Path) -> None:
    with _stack(tmp_path) as (_runtime, _subject_path, _candidate_store, evidence, candidate):
        checker = MinimumCandidateChecker(Path(__file__).parents[1], evidence)
        first = checker.check(candidate, request_id="m4c:check:supersede")
        second = evidence.evaluate(evidence_id=first.evidence_id, evaluator_id="m4c-evaluator", evaluator_version="2", evaluator_code_digest="sha256:" + "1" * 64, config_digest="sha256:" + "2" * 64, result="FAIL", applicability="APPLICABLE", detail={"reason": "new evaluator"})
        history = evidence.query(first.evidence_id)
        assert len(history["evaluations"]) == 2
        assert history["current_disposition"]["evaluation_id"] == second.evaluation_id
        assert history["current_disposition"]["supersedes"] == first.evaluation_id
        assert history["evaluations"][0]["result"] == "PASS"


def test_evaluation_rechecks_candidate_applicability_inside_writer_transaction(tmp_path: Path, monkeypatch) -> None:
    with _stack(tmp_path) as (_runtime, _subject_path, candidate_store, evidence, candidate):
        original = candidate_store.verify_current_applicability
        calls = 0

        def invalidate_after_preflight(candidate_id: str, *, connection=None):
            nonlocal calls
            calls += 1
            record = original(candidate_id, connection=connection)
            if calls == 1:
                workspace = Path(candidate_store.get(candidate_id).workspace_root)  # type: ignore[union-attr]
                (workspace / "one.txt").write_bytes(b"stale during evaluation\n")
                invalidated = candidate_store.invalidate_if_changed(candidate_id)
                assert invalidated.state == "INVALIDATED"
            return record

        monkeypatch.setattr(candidate_store, "verify_current_applicability", invalidate_after_preflight)
        checker = MinimumCandidateChecker(Path(__file__).parents[1], evidence)
        evaluation = checker.check(candidate, request_id="m4c:check:interleaved-applicability")
        assert calls >= 2
        assert evaluation.result == "INCONCLUSIVE"
        assert evaluation.applicability == "INCONCLUSIVE"
        current = evidence.current_disposition(evaluation.evidence_id)
        assert current is not None and current.disposition == "INCONCLUSIVE"
        assert evidence.query(evaluation.evidence_id)["effective_disposition"] == "INCONCLUSIVE"


def test_missing_rq2_raw_is_a_typed_gap_not_reconstructed_evidence(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    with EvidenceStore(runtime) as evidence:
        gap = evidence.record_gap(primary_subject_kind="M2D-RQ2", primary_subject_identity={"basis": "m2d-rq2-repaired-treatment-v2"}, reason="raw_observation_unavailable", details={"historical_artifact": "preserved", "canonical_ingestion": False})
        assert gap.as_dict()["reason"] == "raw_observation_unavailable"
        assert evidence._connection.execute("SELECT COUNT(*) FROM m4c_evidence_records").fetchone()[0] == 0


def test_m4c_schema_documents_parse_and_control_db_is_shared(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    for name in ("bdb-vnext-m4c-evidence-v1.schema.json", "bdb-vnext-m4c-evaluation-v1.schema.json", "bdb-vnext-m4c-disposition-v1.schema.json"):
        document = json.loads((root / "schemas" / name).read_text(encoding="utf-8"))
        assert document["additionalProperties"] is False
    runtime = tmp_path / "runtime"
    with EvidenceStore(runtime) as evidence:
        tables = {row[0] for row in evidence._connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "m4c_evidence_records" in tables
        assert evidence.database_path == runtime / "control" / "control.db"

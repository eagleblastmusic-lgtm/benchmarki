from __future__ import annotations

from pathlib import Path

import pytest

from bdb_vnext.composition import build_vnext_composition_manifest
from bdb_vnext.provider_root import VNextCompositionRoot
from bdb_vnext.m3a_submission import ShadowSubmissionRequest
from bdb_vnext.n4_publication import N4Error, PRESENTED, UNKNOWN
from bdb_vnext.repo_view import RepositoryResource
from bdb_vnext.m4c_evidence import MinimumCandidateChecker
from bdb_vnext.n4_browser import BrowserPublicationClient


def _root(tmp_path: Path) -> VNextCompositionRoot:
    runtime = tmp_path / "runtime"
    legacy = tmp_path / "legacy"
    manifest = build_vnext_composition_manifest(
        source_commit="4" * 40,
        runtime_root=runtime,
        legacy_runtime_root=legacy,
        forbidden_roots=[Path(__file__).resolve().parents[1]],
    )
    return VNextCompositionRoot.from_manifest(manifest)


def _task(plane, key: str = "n4:request"):
    receipt = plane.admission.authority.admit(
        ShadowSubmissionRequest(
            submission_key=key,
            intent_revision="r1",
            intent={"operation": "publish"},
            conversation_binding={"conversation_id": "n4-conversation"},
            consumer_binding={"consumer_id": "n4-browser", "kind": "browser"},
        )
    )
    work = plane.work_kernel.create_work_item(f"{key}:work", receipt.task_id)
    return receipt, work


def _publish(plane, key: str = "n4:publication"):
    receipt, work = _task(plane, key.replace("publication", "request"))
    publication = plane.publication.publish(
        request_id=key,
        task_id=receipt.task_id,
        work_id=work.work_id,
        intent_revision_id=receipt.intent_revision_id,
        result_payload={"result": "sealed", "task_id": receipt.task_id},
        consumer_id="n4-browser",
        consumer_kind="BROWSER",
        conversation_id="n4-conversation",
    )
    return receipt, work, publication


def test_publication_atomic_idempotent_and_lost_ack_replays(tmp_path: Path) -> None:
    root = _root(tmp_path)
    with root.open_control_plane() as plane:
        receipt, work, publication = _publish(plane)
        assert plane.publication.get(publication.publication_id) == publication
        assert plane.publication.publish(
            request_id="n4:publication",
            task_id=receipt.task_id,
            work_id=work.work_id,
            intent_revision_id=receipt.intent_revision_id,
            result_payload={"result": "sealed", "task_id": receipt.task_id},
            consumer_id="n4-browser",
            consumer_kind="BROWSER",
            conversation_id="n4-conversation",
        ) == publication
        with pytest.raises(N4Error, match="committed") as caught:
            plane.publication.publish(
                request_id="n4:lost-ack",
                task_id=receipt.task_id,
                work_id=work.work_id,
                intent_revision_id=receipt.intent_revision_id,
                result_payload={"result": "lost"},
                consumer_id="n4-browser",
                consumer_kind="BROWSER",
                conversation_id="n4-conversation",
                fault="after_commit",
            )
        publication_id = caught.value.details["publication_id"]
        assert plane.publication.get(publication_id) is not None
        assert plane.publication.get_binding(publication_id, "n4-browser") is not None
        with pytest.raises(N4Error, match="interrupted"):
            plane.publication.publish(
                request_id="n4:rollback",
                task_id=receipt.task_id,
                work_id=work.work_id,
                intent_revision_id=receipt.intent_revision_id,
                result_payload={"result": "rolled-back"},
                consumer_id="n4-browser",
                consumer_kind="BROWSER",
                conversation_id="n4-conversation",
                fault="before_commit",
            )
        assert len(plane.publication.publications_for_task(receipt.task_id)) == 2


def test_one_immutable_publication_can_bind_multiple_named_consumers(tmp_path: Path) -> None:
    root = _root(tmp_path)
    with root.open_control_plane() as plane:
        receipt, work, publication = _publish(plane)
        replay = plane.publication.publish(
            request_id="n4:publication:operator",
            task_id=receipt.task_id,
            work_id=work.work_id,
            intent_revision_id=receipt.intent_revision_id,
            result_payload={"result": "sealed", "task_id": receipt.task_id},
            consumer_id="n4-operator",
            consumer_kind="OPERATOR",
        )
        assert replay.publication_id == publication.publication_id
        assert {item.consumer_id for item in plane.publication.bindings_for_publication(publication.publication_id)} == {"n4-browser", "n4-operator"}


def test_consumer_cursor_presentation_unknown_and_witness_are_separate(tmp_path: Path) -> None:
    root = _root(tmp_path)
    with root.open_control_plane() as plane:
        _receipt, _work, publication = _publish(plane)
        browser = BrowserPublicationClient(plane.publication, "n4-browser", "n4-conversation")
        next_publication = browser.receive_next()
        assert next_publication == publication
        with pytest.raises(N4Error, match="canonical consumer cursor"):
            browser.receive_from_cursor(99)
        with pytest.raises(N4Error, match="committed"):
            browser.acknowledge(publication.publication_id, fault="after_commit")
        acknowledged = browser.acknowledge(publication.publication_id)
        assert acknowledged.cursor_sequence == publication.sequence
        unknown = browser.mark_unknown(publication.publication_id, reason="dom_not_observed")
        assert unknown.presentation == UNKNOWN
        witness = {
            "source": "chatgpt-dom-exact-publication",
            "observation": "EXACT_RESULT_VISIBLE",
            "observed_publication_id": publication.publication_id,
            "observed_conversation_id": "n4-conversation",
            "observed_marker": "pre-ack-dom-witness",
            "observed_result_digest": publication.result_digest,
        }
        with pytest.raises(N4Error, match="positive exact-result DOM"):
            browser.observe_dom(publication, marker="pre-ack-dom-witness")
        witnessed_before_ack = browser.observe_dom(publication, marker="pre-ack-dom-witness", witness=witness)
        assert witnessed_before_ack.presentation == PRESENTED
        presented = browser.observe_dom(publication, marker="pre-ack-dom-witness", witness=witness)
        assert presented.presentation == PRESENTED
        assert presented.witness_id
        with pytest.raises(N4Error, match="composer"):
            plane.publication.observe_presentation(
                publication_id=publication.publication_id,
                consumer_id="n4-browser",
                conversation_id="n4-conversation",
                marker="would-clear-composer",
                result_digest=publication.result_digest,
                composer_preserved=False,
            )
        replay = plane.publication.observe_presentation(
            publication_id=publication.publication_id,
            consumer_id="n4-browser",
            conversation_id="n4-conversation",
            marker="pre-ack-dom-witness",
            result_digest=publication.result_digest,
            witness=witness,
        )
        assert replay.witness_id == presented.witness_id
        with pytest.raises(N4Error, match="canonical consumer binding"):
            plane.publication.observe_presentation(
                publication_id=publication.publication_id,
                consumer_id="n4-browser",
                conversation_id="other-conversation",
                marker="wrong",
                result_digest=publication.result_digest,
            )


def test_resume_capsule_is_durable_and_new_consumer_does_not_inherit_presentation(tmp_path: Path) -> None:
    root = _root(tmp_path)
    with root.open_control_plane() as plane:
        _receipt, _work, publication = _publish(plane)
        plane.publication.acknowledge(consumer_id="n4-browser", publication_id=publication.publication_id)
        plane.publication.observe_presentation(
            publication_id=publication.publication_id,
            consumer_id="n4-browser",
            conversation_id="n4-conversation",
            marker="old-dom-marker",
            result_digest=publication.result_digest,
            witness={
                "source": "chatgpt-dom-exact-publication",
                "observation": "EXACT_RESULT_VISIBLE",
                "observed_publication_id": publication.publication_id,
                "observed_conversation_id": "n4-conversation",
                "observed_marker": "old-dom-marker",
                "observed_result_digest": publication.result_digest,
            },
        )
        target = plane.publication.bind_consumer(
            publication_id=publication.publication_id,
            consumer_id="n4-new-chat",
            consumer_kind="BROWSER",
            conversation_id="new-conversation",
        )
        assert target.presentation == UNKNOWN
        capsule = plane.publication.create_resume_capsule(
            publication_id=publication.publication_id,
            source_consumer_id="n4-browser",
            target_consumer_id="n4-new-chat",
            payload={"repo_subject": {"kind": "COMMITTED", "view_id": "sha256:" + "a" * 64}},
        )
        assert plane.publication.resume(capsule.capsule_id) == capsule
        assert plane.publication.resume_payload(capsule.capsule_id)["target_consumer_id"] == "n4-new-chat"
        with pytest.raises(N4Error, match="hidden reasoning"):
            plane.publication.create_resume_capsule(
                publication_id=publication.publication_id,
                source_consumer_id="n4-browser",
                target_consumer_id="n4-new-chat",
                payload={"chain_of_thought": "forbidden"},
            )


def test_operator_query_requires_exact_repo_subject_and_is_read_only(tmp_path: Path) -> None:
    root = _root(tmp_path)
    with root.open_control_plane() as plane:
        receipt, work, publication = _publish(plane)
        with pytest.raises(N4Error, match="explicit COMMITTED"):
            plane.query.task_view(task_id=receipt.task_id, work_id=work.work_id, repo_view_kind=None)
        live = plane.query.task_view(task_id=receipt.task_id, work_id=work.work_id, repo_view_kind="LIVE")
        assert live["subject"] == {"kind": "LIVE", "state": "UNAVAILABLE", "reason": "honest_live_capture_not_implemented"}
        assert live["publications"][0]["publication_id"] == publication.publication_id
        before = plane.work_kernel.query(work.work_id).as_dict()
        again = plane.query.task_view(task_id=receipt.task_id, work_id=work.work_id, repo_view_kind="LIVE")
        assert again["work"] == before
        assert again["freshness"]["watermark"].startswith("publication-sequence:")


def test_operator_query_committed_candidate_and_mixed_subjects_fail_closed(tmp_path: Path) -> None:
    root = _root(tmp_path)
    subject = tmp_path / "subject"
    subject.mkdir()
    import subprocess
    subprocess.run(["git", "init", "-q", "-b", "main", str(subject)], check=True)
    subprocess.run(["git", "-C", str(subject), "config", "user.name", "N4"], check=True)
    subprocess.run(["git", "-C", str(subject), "config", "user.email", "n4@example.invalid"], check=True)
    (subject / "one.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(subject), "add", "one.txt"], check=True)
    subprocess.run(["git", "-C", str(subject), "commit", "-qm", "base"], check=True)
    committed = RepositoryResource.from_path(subject, repository_id="n4-subject").resolve_committed("HEAD")
    with root.open_control_plane() as plane:
        receipt, work, _publication = _publish(plane)
        view = plane.query.task_view(task_id=receipt.task_id, work_id=work.work_id, repo_view_kind="COMMITTED", committed=committed)
        assert view["subject"]["view_id"] == committed.view_id
        with pytest.raises(N4Error, match="combine"):
            plane.query.task_view(task_id=receipt.task_id, work_id=work.work_id, repo_view_kind="COMMITTED", committed=committed, candidate=object())


def test_control_db_reopen_preserves_publication_and_query(tmp_path: Path) -> None:
    root = _root(tmp_path)
    with root.open_control_plane() as plane:
        receipt, work, publication = _publish(plane)
        assert plane.publication.get(publication.publication_id)
    with root.open_control_plane(existing_outbox=True) as reopened:
        assert reopened.publication.get(publication.publication_id) == publication
        assert reopened.query.task_view(task_id=receipt.task_id, work_id=work.work_id, repo_view_kind="LIVE")["publications"]


def test_integrated_task_candidate_evidence_publication_query_flow(tmp_path: Path) -> None:
    root = _root(tmp_path)
    subject = tmp_path / "subject"
    subject.mkdir()
    import subprocess
    subprocess.run(["git", "init", "-q", "-b", "main", str(subject)], check=True)
    subprocess.run(["git", "-C", str(subject), "config", "user.name", "N4"], check=True)
    subprocess.run(["git", "-C", str(subject), "config", "user.email", "n4@example.invalid"], check=True)
    (subject / "one.txt").write_bytes(b"one\n")
    subprocess.run(["git", "-C", str(subject), "add", "one.txt"], check=True)
    subprocess.run(["git", "-C", str(subject), "commit", "-qm", "base"], check=True)
    view = RepositoryResource.from_path(subject, repository_id="n4-subject").resolve_committed("HEAD")
    with root.open_control_plane() as plane:
        receipt, work = _task(plane, "n4:integrated:request")
        lease = plane.work_kernel.acquire_lease(work.work_id, "n4:integrated:lease", "n4:integrated:worker")
        workspace = plane.candidate.create_workspace(candidate_id="n4:integrated:candidate", base_view=view)
        try:
            prepared = plane.candidate.prepare(
                candidate_id="n4:integrated:candidate",
                work_id=work.work_id,
                task_id=receipt.task_id,
                lease_id=lease.lease_id,
                fence=lease.fence,
                base_view=view,
                workspace_root=workspace,
                replacements={"one.txt": b"checked\n"},
            )
            plane.candidate.apply(prepared.candidate_id)
            _sealed, candidate = plane.candidate.seal(prepared.candidate_id, base_view=view)
            evaluation = MinimumCandidateChecker(Path(__file__).parents[1], plane.evidence).check(candidate, request_id="n4:integrated:evidence")
            evidence_query = plane.evidence.query(evaluation.evidence_id)
            assert evidence_query["effective_disposition"] == "PASS"
            current = plane.evidence.current_disposition(evaluation.evidence_id)
            assert current is not None
            publication = plane.publication.publish(
                request_id="n4:integrated:publication",
                task_id=receipt.task_id,
                work_id=work.work_id,
                intent_revision_id=receipt.intent_revision_id,
                result_payload={"candidate_view_id": candidate.view_id, "evidence_id": evaluation.evidence_id, "disposition": current.disposition},
                consumer_id="n4-browser",
                consumer_kind="BROWSER",
                conversation_id="n4-conversation",
                candidate_id=candidate.candidate_id,
                candidate_view_id=candidate.view_id,
                evidence_id=evaluation.evidence_id,
                evaluation_id=evaluation.evaluation_id,
                disposition_id=current.disposition_id,
            )
            query = plane.query.task_view(task_id=receipt.task_id, work_id=work.work_id, repo_view_kind="CANDIDATE", candidate=candidate, evidence_id=evaluation.evidence_id)
            assert query["subject"]["view_id"] == candidate.view_id
            assert query["evidence"]["effective_disposition"] == "PASS"
            assert query["publications"][0]["publication_id"] == publication.publication_id
        finally:
            if workspace.exists():
                subprocess.run(["git", "-C", str(subject), "worktree", "remove", "--force", str(workspace)], check=False)


def test_publication_revalidates_current_candidate_and_evidence_applicability(tmp_path: Path) -> None:
    root = _root(tmp_path)
    subject = tmp_path / "subject-applicability"
    subject.mkdir()
    import subprocess
    subprocess.run(["git", "init", "-q", "-b", "main", str(subject)], check=True)
    subprocess.run(["git", "-C", str(subject), "config", "user.name", "N4"], check=True)
    subprocess.run(["git", "-C", str(subject), "config", "user.email", "n4@example.invalid"], check=True)
    (subject / "one.txt").write_bytes(b"one\n")
    subprocess.run(["git", "-C", str(subject), "add", "one.txt"], check=True)
    subprocess.run(["git", "-C", str(subject), "commit", "-qm", "base"], check=True)
    view = RepositoryResource.from_path(subject, repository_id="n4-applicability-subject").resolve_committed("HEAD")
    with root.open_control_plane() as plane:
        receipt, work = _task(plane, "n4:applicability:request")
        lease = plane.work_kernel.acquire_lease(work.work_id, "n4:applicability:lease", "n4:applicability:worker")
        workspace = plane.candidate.create_workspace(candidate_id="n4:applicability:candidate", base_view=view)
        try:
            prepared = plane.candidate.prepare(
                candidate_id="n4:applicability:candidate", work_id=work.work_id, task_id=receipt.task_id,
                lease_id=lease.lease_id, fence=lease.fence, base_view=view, workspace_root=workspace,
                replacements={"one.txt": b"checked\n"},
            )
            plane.candidate.apply(prepared.candidate_id)
            _sealed, candidate = plane.candidate.seal(prepared.candidate_id, base_view=view)
            evaluation = MinimumCandidateChecker(Path(__file__).parents[1], plane.evidence).check(
                candidate, request_id="n4:applicability:evidence"
            )
            current = plane.evidence.current_disposition(evaluation.evidence_id)
            assert current is not None and current.disposition == "PASS"

            (workspace / "one.txt").write_bytes(b"tampered after seal\n")
            query = plane.evidence.query(evaluation.evidence_id)
            assert query["applicability"]["applicable"] is False
            assert query["effective_disposition"] == "INCONCLUSIVE"
            historical = plane.evidence.evaluations(evaluation.evidence_id)
            dispositions = plane.evidence.dispositions(evaluation.evidence_id)

            with pytest.raises(N4Error) as caught:
                plane.publication.publish(
                    request_id="n4:applicability:publication", task_id=receipt.task_id,
                    work_id=work.work_id, intent_revision_id=receipt.intent_revision_id,
                    result_payload={"candidate_view_id": candidate.view_id}, consumer_id="n4-browser",
                    consumer_kind="BROWSER", conversation_id="n4-conversation",
                    candidate_id=candidate.candidate_id, candidate_view_id=candidate.view_id,
                    evidence_id=evaluation.evidence_id, evaluation_id=evaluation.evaluation_id,
                    disposition_id=current.disposition_id,
                )
            assert caught.value.code in {"candidate_not_applicable", "evidence_not_applicable"}
            assert plane.publication.publications_for_task(receipt.task_id) == ()
            assert plane.publication._connection.execute("SELECT COUNT(*) FROM n4_consumer_bindings").fetchone()[0] == 0
            assert plane.evidence.evaluations(evaluation.evidence_id) == historical
            assert plane.evidence.dispositions(evaluation.evidence_id) == dispositions
        finally:
            if workspace.exists():
                subprocess.run(["git", "-C", str(subject), "worktree", "remove", "--force", str(workspace)], check=False)


def test_unchanged_current_candidate_still_publishes(tmp_path: Path) -> None:
    root = _root(tmp_path)
    subject = tmp_path / "subject-current"
    subject.mkdir()
    import subprocess
    subprocess.run(["git", "init", "-q", "-b", "main", str(subject)], check=True)
    subprocess.run(["git", "-C", str(subject), "config", "user.name", "N4"], check=True)
    subprocess.run(["git", "-C", str(subject), "config", "user.email", "n4@example.invalid"], check=True)
    (subject / "one.txt").write_bytes(b"one\n")
    subprocess.run(["git", "-C", str(subject), "add", "one.txt"], check=True)
    subprocess.run(["git", "-C", str(subject), "commit", "-qm", "base"], check=True)
    view = RepositoryResource.from_path(subject, repository_id="n4-current-subject").resolve_committed("HEAD")
    with root.open_control_plane() as plane:
        receipt, work = _task(plane, "n4:current:request")
        lease = plane.work_kernel.acquire_lease(work.work_id, "n4:current:lease", "n4:current:worker")
        workspace = plane.candidate.create_workspace(candidate_id="n4:current:candidate", base_view=view)
        try:
            prepared = plane.candidate.prepare(candidate_id="n4:current:candidate", work_id=work.work_id, task_id=receipt.task_id, lease_id=lease.lease_id, fence=lease.fence, base_view=view, workspace_root=workspace, replacements={"one.txt": b"current\n"})
            plane.candidate.apply(prepared.candidate_id)
            _sealed, candidate = plane.candidate.seal(prepared.candidate_id, base_view=view)
            evaluation = MinimumCandidateChecker(Path(__file__).parents[1], plane.evidence).check(candidate, request_id="n4:current:evidence")
            current = plane.evidence.current_disposition(evaluation.evidence_id)
            assert current is not None
            publication = plane.publication.publish(request_id="n4:current:publication", task_id=receipt.task_id, work_id=work.work_id, intent_revision_id=receipt.intent_revision_id, result_payload={"candidate_view_id": candidate.view_id}, consumer_id="n4-browser", consumer_kind="BROWSER", conversation_id="n4-conversation", candidate_id=candidate.candidate_id, candidate_view_id=candidate.view_id, evidence_id=evaluation.evidence_id, evaluation_id=evaluation.evaluation_id, disposition_id=current.disposition_id)
            assert publication.candidate_view_id == candidate.view_id
        finally:
            if workspace.exists():
                subprocess.run(["git", "-C", str(subject), "worktree", "remove", "--force", str(workspace)], check=False)

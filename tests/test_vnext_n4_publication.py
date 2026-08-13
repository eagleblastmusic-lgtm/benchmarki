from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import pytest

from bdb_vnext.composition import build_vnext_composition_manifest
from bdb_vnext.provider_root import VNextCompositionRoot
from bdb_vnext.m3a_submission import ShadowSubmissionRequest
from bdb_vnext.n4_publication import N4Error, PRESENTED, UNKNOWN, PublicationStore
from bdb_vnext.repo_view import RepositoryResource
from bdb_vnext.m4c_evidence import MinimumCandidateChecker
from bdb_vnext.n4_browser import BrowserPublicationClient
from bdb_shared.evidence import semantic_digest
from bdb_vnext.content_store import ImmutableContentStore


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


def _assistant_witness(plane, publication, *, conversation_id: str = "n4-conversation", answer: str = "actual assistant result"):
    answer_digest = "sha256:" + hashlib.sha256(answer.encode("utf-8")).hexdigest()
    raw = {
        "schema": "bdb-vnext-n6-browser-event-v1",
        "event": "assistant_capture",
        "publication_id": publication.publication_id,
        "conversation_id": conversation_id,
        "completion_observation": "DOM_TEXT_STABLE_AFTER_STREAM_END",
        "raw_answer": answer,
        "raw_answer_digest": answer_digest,
    }
    evidence = plane.evidence.record_observation(
        request_id=f"n4:assistant-capture:{publication.publication_id}:{answer_digest}",
        primary_subject_kind="N6_BROWSER_RUN",
        primary_subject_identity={
            "task_id": publication.task_id,
            "work_id": publication.work_id,
            "publication_id": publication.publication_id,
        },
        candidate_view_id=publication.candidate_view_id,
        raw_observation=raw,
        checker_id="bdb-vnext-n6-browser-capture",
        checker_version="1",
        checker_code_digest=semantic_digest({"checker": "n4-assistant-fixture-v1"}),
        environment={"surface": "normal-chatgpt-browser-fixture"},
        observation_started_at="2026-08-13T00:00:00Z",
        observation_finished_at="2026-08-13T00:00:01Z",
        completeness="COMPLETE",
        applicability="APPLICABLE",
        status="CAPTURED",
    )
    return {
        "source": "chatgpt-assistant-dom-capture",
        "observation": "EXACT_CAPTURED_ASSISTANT_RESULT_VISIBLE",
        "observed_publication_id": publication.publication_id,
        "observed_conversation_id": conversation_id,
        "observed_result_digest": publication.result_digest,
        "capture_evidence_id": evidence.evidence_id,
        "observed_answer_digest": answer_digest,
        "dom_author_role": "assistant",
        "completion_observation": "DOM_TEXT_STABLE_AFTER_STREAM_END",
        "extension_ui_ancestor": False,
    }


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
        witness = _assistant_witness(plane, publication)
        with pytest.raises(N4Error, match="captured assistant Evidence"):
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
            witness=_assistant_witness(plane, publication),
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


def test_publication_revalidates_current_candidate_and_evidence_applicability(tmp_path: Path, monkeypatch) -> None:
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

            historical = plane.evidence.evaluations(evaluation.evidence_id)
            dispositions = plane.evidence.dispositions(evaluation.evidence_id)
            original_publish = plane.publication.content_store.publish
            interleaved = False

            def publish_then_invalidate(ref, raw):
                nonlocal interleaved
                result = original_publish(ref, raw)
                if not interleaved:
                    interleaved = True
                    (workspace / "one.txt").write_bytes(b"tampered during publication\n")
                    invalidated = plane.candidate.invalidate_if_changed(candidate.candidate_id, base_view=view)
                    assert invalidated.state == "INVALIDATED"
                return result

            monkeypatch.setattr(plane.publication.content_store, "publish", publish_then_invalidate)

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
            assert interleaved is True
            query = plane.evidence.query(evaluation.evidence_id)
            assert query["applicability"]["applicable"] is False
            assert query["effective_disposition"] == "INCONCLUSIVE"
            assert plane.publication.publications_for_task(receipt.task_id) == ()
            assert plane.publication._connection.execute("SELECT COUNT(*) FROM n4_consumer_bindings").fetchone()[0] == 0
            assert plane.evidence.evaluations(evaluation.evidence_id) == historical
            assert plane.evidence.dispositions(evaluation.evidence_id) == dispositions
            with pytest.raises(N4Error) as stale:
                plane.publication.publish(
                    request_id="n4:applicability:stale", task_id=receipt.task_id,
                    work_id=work.work_id, intent_revision_id=receipt.intent_revision_id,
                    result_payload={"candidate_view_id": candidate.view_id}, consumer_id="n4-browser",
                    consumer_kind="BROWSER", conversation_id="n4-conversation",
                    candidate_id=candidate.candidate_id, candidate_view_id=candidate.view_id,
                    evidence_id=evaluation.evidence_id, evaluation_id=evaluation.evaluation_id,
                    disposition_id=current.disposition_id,
                )
            assert stale.value.code in {"candidate_not_applicable", "evidence_not_applicable"}
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


def test_presentation_rejects_extension_ui_unrelated_stale_and_injected_witnesses(tmp_path: Path) -> None:
    root = _root(tmp_path)
    with root.open_control_plane() as plane:
        _receipt, _work, publication = _publish(plane, "n4:witness:publication")
        exact = _assistant_witness(plane, publication)
        variants = []
        for key, value in (
            ("extension_ui_ancestor", True),
            ("dom_author_role", "user"),
            ("observed_publication_id", "sha256:" + "0" * 64),
            ("observed_result_digest", "sha256:" + "1" * 64),
            ("observed_answer_digest", "sha256:" + "2" * 64),
        ):
            variant = dict(exact)
            variant[key] = value
            variants.append(variant)
        variants.append({
            "source": "chatgpt-dom-exact-publication",
            "observation": "EXACT_RESULT_VISIBLE",
            "observed_publication_id": publication.publication_id,
            "observed_conversation_id": "n4-conversation",
            "observed_marker": "injected extension marker",
            "observed_result_digest": publication.result_digest,
        })
        for witness in variants:
            with pytest.raises(N4Error) as caught:
                plane.publication.observe_presentation(
                    publication_id=publication.publication_id,
                    consumer_id="n4-browser",
                    conversation_id="n4-conversation",
                    marker="n4-marker",
                    result_digest=publication.result_digest,
                    witness=witness,
                )
            assert caught.value.code == "presentation_not_observed"
        with pytest.raises(N4Error) as wrong_conversation:
            plane.publication.observe_presentation(
                publication_id=publication.publication_id,
                consumer_id="n4-browser",
                conversation_id="wrong-conversation",
                marker="n4-marker",
                result_digest=publication.result_digest,
                witness=exact,
            )
        assert wrong_conversation.value.code == "wrong_conversation"
        presented = plane.publication.observe_presentation(
            publication_id=publication.publication_id,
            consumer_id="n4-browser",
            conversation_id="n4-conversation",
            marker="n4-marker",
            result_digest=publication.result_digest,
            witness=exact,
        )
        assert presented.presentation == PRESENTED


def test_concurrent_publication_replay_is_typed_and_idempotent(tmp_path: Path) -> None:
    root = _root(tmp_path)
    with root.open_control_plane() as plane:
        receipt, work = _task(plane, "n4:concurrent:request")
        def run_pair(request_id: str, payloads: tuple[dict[str, str], dict[str, str]]):
            barrier = threading.Barrier(2)
            results = []
            failures = []
            lock = threading.Lock()

            def worker(payload: dict[str, str]) -> None:
                store = None
                try:
                    content_store = ImmutableContentStore(plane.publication.root)
                    store = PublicationStore(
                        plane.publication.root,
                        content_store=content_store,
                        task_authority=plane.admission.authority,
                        work_kernel=plane.work_kernel,
                        candidate_store=plane.candidate,
                        evidence_store=plane.evidence,
                    )
                    original = content_store.publish

                    def synchronized_publish(ref, raw):
                        result = original(ref, raw)
                        barrier.wait(timeout=10)
                        return result

                    content_store.publish = synchronized_publish
                    result = store.publish(
                        request_id=request_id,
                        task_id=receipt.task_id,
                        work_id=work.work_id,
                        intent_revision_id=receipt.intent_revision_id,
                        result_payload=payload,
                        consumer_id="n4-browser",
                        consumer_kind="BROWSER",
                        conversation_id="n4-conversation",
                    )
                    with lock:
                        results.append(result)
                except Exception as exc:  # assertions below verify the typed boundary
                    with lock:
                        failures.append(exc)
                finally:
                    if store is not None:
                        store.close()

            threads = [threading.Thread(target=worker, args=(payload,)) for payload in payloads]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
                assert not thread.is_alive()
            return results, failures

        identical, identical_failures = run_pair(
            "n4:concurrent:same",
            ({"result": "same"}, {"result": "same"}),
        )
        assert identical_failures == []
        assert len(identical) == 2
        assert identical[0].publication_id == identical[1].publication_id

        conflicting, conflict_failures = run_pair(
            "n4:concurrent:conflict",
            ({"result": "left"}, {"result": "right"}),
        )
        assert len(conflicting) == 1
        assert len(conflict_failures) == 1
        assert isinstance(conflict_failures[0], N4Error)
        assert conflict_failures[0].code == "publication_request_conflict"
        publications = plane.publication.publications_for_task(receipt.task_id)
        assert len(publications) == 2
        assert plane.publication._connection.execute("SELECT COUNT(*) FROM n4_consumer_bindings").fetchone()[0] == 2


def test_concurrent_unknown_cannot_downgrade_presented(tmp_path: Path) -> None:
    root = _root(tmp_path)
    with root.open_control_plane() as plane:
        _receipt, _work, publication = _publish(plane, "n4:presentation-race:publication")
        witness = _assistant_witness(plane, publication)
        stale_read_complete = threading.Event()
        presented_committed = threading.Event()
        failures = []

        def stale_unknown_writer() -> None:
            store = PublicationStore(
                plane.publication.root,
                content_store=ImmutableContentStore(plane.publication.root),
                task_authority=plane.admission.authority,
                work_kernel=plane.work_kernel,
                candidate_store=plane.candidate,
                evidence_store=plane.evidence,
            )
            try:
                stale = store.get_binding(publication.publication_id, "n4-browser")
                assert stale is not None and stale.presentation == UNKNOWN
                stale_read_complete.set()
                assert presented_committed.wait(timeout=10)
                retained = store.mark_unknown(
                    publication_id=publication.publication_id,
                    consumer_id="n4-browser",
                    reason="concurrent_dom_uncertainty",
                )
                assert retained.presentation == PRESENTED
            except Exception as exc:
                failures.append(exc)
            finally:
                store.close()

        thread = threading.Thread(target=stale_unknown_writer)
        thread.start()
        assert stale_read_complete.wait(timeout=10)
        plane.publication.observe_presentation(
            publication_id=publication.publication_id,
            consumer_id="n4-browser",
            conversation_id="n4-conversation",
            marker="n4-race-marker",
            result_digest=publication.result_digest,
            witness=witness,
        )
        presented_committed.set()
        thread.join(timeout=30)
        assert not thread.is_alive()
        assert failures == []
        current = plane.publication.get_binding(publication.publication_id, "n4-browser")
        assert current is not None and current.presentation == PRESENTED
        retained = plane.publication.mark_unknown(
            publication_id=publication.publication_id,
            consumer_id="n4-browser",
            reason="serial_after_presented",
        )
        assert retained.presentation == PRESENTED

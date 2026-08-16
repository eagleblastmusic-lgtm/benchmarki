from __future__ import annotations

import inspect
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from bdb_shared.evidence import semantic_digest
from bdb_vnext.candidate import CandidateStore
from bdb_vnext.m3a_submission import ShadowSubmissionRequest
from bdb_vnext.m3c_admission import open_vnext_admission_composition
from bdb_vnext.m4a_work_kernel import WorkKernelStore
from bdb_vnext.m4c_evidence import EvidenceStore, MinimumCandidateChecker
from bdb_vnext.m6a_evidence_policy import EvidencePolicyGate
from bdb_vnext.m6b_check_plan import DeterministicCheckPlanSelector
from bdb_vnext.m7a_git_cas import (
    CommitMetadataPolicy,
    PreparedGitCasAdapter,
    candidate_subject_identity,
)
from bdb_vnext.m7b_checkout_sync import CheckoutSyncAdapter, M7bError
from bdb_vnext.repo_view import RepositoryResource


def _run(
    repo: Path,
    *args: str,
    check: bool = True,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
        env=env,
        input=input_text,
    )


def _git(repo: Path, *args: str) -> str:
    return _run(repo, *args).stdout.strip()


def _subject(root: Path) -> Path:
    repo = root / "subject"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "M7b Test")
    _git(repo, "config", "user.email", "m7b@example.invalid")
    (repo / "one.txt").write_bytes(b"one\n")
    (repo / "nested").mkdir()
    (repo / "nested" / "two.txt").write_bytes(b"two\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return repo


def _metadata() -> CommitMetadataPolicy:
    return CommitMetadataPolicy(
        message="bdb-vnext: prepared candidate",
        author_name="BDB vNext",
        author_email="bdb-vnext@localhost.invalid",
        committer_name="BDB vNext",
        committer_email="bdb-vnext@localhost.invalid",
        timestamp="2026-08-17T00:00:00Z",
    )


@contextmanager
def _stack(tmp_path: Path):
    runtime = tmp_path / "runtime"
    legacy = tmp_path / "legacy"
    subject = _subject(tmp_path)

    admission = open_vnext_admission_composition(runtime, legacy_root=legacy)
    receipt = admission.authority.admit(
        ShadowSubmissionRequest(
            submission_key="m7b:submission",
            intent_revision="r1",
            intent={"operation": "checkout-sync"},
            conversation_binding={"conversation_id": "m7b"},
            consumer_binding={"consumer_id": "m7b", "kind": "browser"},
        )
    )
    kernel = WorkKernelStore.open(
        runtime,
        task_authority=admission.authority,
        legacy_root=legacy,
        clock=lambda: 100.0,
    )
    candidate_store = CandidateStore(runtime, work_kernel=kernel)
    evidence = EvidenceStore(
        runtime,
        content_store=candidate_store.content_store,
        candidate_store=candidate_store,
    )
    gate = EvidencePolicyGate(evidence)

    resource = RepositoryResource.from_path(subject, repository_id="m7b-subject")
    base = resource.resolve_committed("HEAD")

    candidate_work = kernel.create_work_item("work:m7b:candidate", receipt.task_id)
    candidate_lease = kernel.acquire_lease(
        candidate_work.work_id,
        "lease:m7b:candidate",
        "worker:m7b:candidate",
        ttl_seconds=3600,
    )
    workspace = candidate_store.create_workspace(candidate_id="candidate:m7b", base_view=base)
    prepared_candidate = candidate_store.prepare(
        candidate_id="candidate:m7b",
        work_id=candidate_work.work_id,
        task_id=receipt.task_id,
        lease_id=candidate_lease.lease_id,
        fence=candidate_lease.fence,
        base_view=base,
        workspace_root=workspace,
        replacements={
            "one.txt": b"checked\n",
            "nested/two.txt": b"checked-two\n",
        },
    )
    candidate_store.apply(prepared_candidate.candidate_id)
    _sealed, candidate = candidate_store.seal(prepared_candidate.candidate_id, base_view=base)

    checker = MinimumCandidateChecker(Path(__file__).parents[1], evidence)
    evaluation = checker.check(candidate, request_id="m7b:evidence")
    evidence_record = evidence.get(evaluation.evidence_id)
    assert evidence_record is not None

    obligation = gate.create_obligation(
        subject_kind="CANDIDATE",
        subject_identity=candidate_subject_identity(candidate),
        requirement="promotion-grade-candidate-check",
        evidence_contract={
            "evidence_type": "candidate-check",
            "coverage": "exact",
            "freshness": "CURRENT",
            "checker_id": evidence_record.checker_id,
            "checker_version": evidence_record.checker_version,
            "checker_code_digest": evidence_record.checker_code_digest,
        },
        waivability="NEVER",
        risk="HIGH",
    )
    assessment = gate.assess_obligation(obligation.obligation_id, evaluation.evidence_id)
    assert assessment.verdict == "PASS"

    plan = DeterministicCheckPlanSelector().plan(
        required_capabilities=("python.pytest",),
        executable_bindings={"python": sys.executable},
    )
    validation_policy_digest = semantic_digest(
        {
            "schema": "bdb-vnext-m7b-test-policy-v1",
            "check_plan_digest": plan.plan_digest,
        }
    )

    promotion_work = kernel.create_work_item(
        "work:m7b:promotion",
        receipt.task_id,
        kind="git-promotion",
    )
    promotion_lease = kernel.acquire_lease(
        promotion_work.work_id,
        "lease:m7b:promotion",
        "worker:m7b:promotion",
        ttl_seconds=3600,
    )
    promotion_run = kernel.start_run(
        promotion_work.work_id,
        "run:m7b:promotion",
        promotion_lease.lease_id,
        promotion_lease.fence,
        promotion_work.state_version,
    )

    target_ref = "refs/bdb-vnext/test/promotion"
    _git(subject, "update-ref", target_ref, base.commit_oid)
    m7a = PreparedGitCasAdapter(
        candidate_store=candidate_store,
        work_kernel=kernel,
        evidence_policy_gate=gate,
    )
    prepared_promotion = m7a.prepare(
        candidate=candidate,
        repository=resource,
        work_id=promotion_work.work_id,
        run_id=promotion_run.run_id,
        target_ref=target_ref,
        expected_old_oid=base.commit_oid,
        metadata=_metadata(),
        intent_revision_id="r1",
        validation_policy_digest=validation_policy_digest,
        check_plan_digest=plan.plan_digest,
        obligation_ids=(obligation.obligation_id,),
        scope="isolated-vnext-ref",
    )
    approval = gate.create_approval(
        subject_digest=prepared_promotion.subject_digest,
        intent_revision_id=prepared_promotion.intent_revision_id,
        effect_digest=prepared_promotion.effect_id,
        policy_digest=prepared_promotion.validation_policy_digest,
        actor="m7b-test-user",
        authority="USER",
        scope=prepared_promotion.scope,
        expires_at="2099-01-01T00:00:00Z",
    )
    promoted = m7a.apply_if_safe(
        effect_id=prepared_promotion.effect_id,
        approval_id=approval.approval_id,
        now="2026-08-17T01:00:00Z",
    )
    assert promoted.effect_certainty == "AFTER"

    # Attach HEAD to the already-promoted isolated ref without synchronizing
    # index/worktree. This is the exact M7a-AFTER / M7b-BEFORE crash window.
    _git(subject, "symbolic-ref", "HEAD", target_ref)
    assert _git(subject, "rev-parse", "HEAD") == prepared_promotion.prepared_commit_oid
    assert _git(subject, "write-tree") == base.tree_oid

    checkout_work = kernel.create_work_item(
        "work:m7b:checkout",
        receipt.task_id,
        kind="checkout-sync",
    )
    checkout_lease = kernel.acquire_lease(
        checkout_work.work_id,
        "lease:m7b:checkout",
        "worker:m7b:checkout",
        ttl_seconds=3600,
    )
    checkout_run = kernel.start_run(
        checkout_work.work_id,
        "run:m7b:checkout",
        checkout_lease.lease_id,
        checkout_lease.fence,
        checkout_work.state_version,
    )
    m7b = CheckoutSyncAdapter(m7a_adapter=m7a, work_kernel=kernel)

    context = {
        "runtime": runtime,
        "subject": subject,
        "resource": resource,
        "base": base,
        "candidate_store": candidate_store,
        "candidate": candidate,
        "evidence": evidence,
        "gate": gate,
        "kernel": kernel,
        "target_ref": target_ref,
        "m7a": m7a,
        "promotion": prepared_promotion,
        "checkout_work": checkout_work,
        "checkout_run": checkout_run,
        "m7b": m7b,
    }
    try:
        yield context
    finally:
        try:
            evidence._connection.close()
        except Exception:
            pass
        try:
            candidate_store._connection.close()
        except Exception:
            pass
        try:
            kernel.close()
        except Exception:
            pass
        try:
            admission.close()
        except Exception:
            pass


def _prepare_checkout(ctx: dict[str, Any], *, fault: str | None = None):
    return ctx["m7b"].prepare(
        source_promotion_effect_id=ctx["promotion"].effect_id,
        work_id=ctx["checkout_work"].work_id,
        run_id=ctx["checkout_run"].run_id,
        fault=fault,
    )


def test_m7a_after_and_m7b_before_are_separate_truths_then_sync_exactly(tmp_path: Path) -> None:
    with _stack(tmp_path) as ctx:
        subject = ctx["subject"]
        promotion = ctx["promotion"]
        assert ctx["m7a"].get(effect_id=promotion.effect_id).effect_certainty == "AFTER"
        assert _git(subject, "show-ref", "--verify", "--hash", ctx["target_ref"]) == promotion.prepared_commit_oid
        assert _git(subject, "write-tree") == ctx["base"].tree_oid
        assert (subject / "one.txt").read_bytes() == b"one\n"

        prepared = _prepare_checkout(ctx)
        assert prepared.effect_certainty == "BEFORE"
        assert prepared.safe_next_action == "SAFE_TO_APPLY"
        assert prepared.old_tree_oid == ctx["base"].tree_oid
        assert prepared.new_tree_oid == promotion.prepared_tree_oid

        final = ctx["m7b"].apply_if_safe(effect_id=prepared.effect_id)
        assert final.effect_certainty == "AFTER"
        assert final.state == "AFTER"
        assert final.safe_next_action == "COMPLETE_ALLOWED"
        assert _git(subject, "write-tree") == promotion.prepared_tree_oid
        assert (subject / "one.txt").read_bytes() == b"checked\n"
        assert (subject / "nested" / "two.txt").read_bytes() == b"checked-two\n"
        assert _git(subject, "show-ref", "--verify", "--hash", ctx["target_ref"]) == promotion.prepared_commit_oid
        assert ctx["m7a"].get(effect_id=promotion.effect_id).effect_certainty == "AFTER"


def test_dirty_staged_or_unstaged_checkout_blocks_prepare_without_effect_or_claim(tmp_path: Path) -> None:
    with _stack(tmp_path) as ctx:
        subject = ctx["subject"]
        (subject / "one.txt").write_bytes(b"foreign staged\n")
        _git(subject, "add", "one.txt")
        (subject / "one.txt").write_bytes(b"foreign unstaged\n")
        before_status = _git(subject, "status", "--porcelain=v1", "--untracked-files=all")

        with pytest.raises(M7bError) as caught:
            _prepare_checkout(ctx)
        assert caught.value.code == "checkout_precondition_mismatch"
        assert ctx["m7b"].get(work_id=ctx["checkout_work"].work_id) is None
        query = ctx["kernel"].query(ctx["checkout_work"].work_id)
        assert query is not None and query.resource_claim is None
        assert _git(subject, "status", "--porcelain=v1", "--untracked-files=all") == before_status


def test_untracked_checkout_blocks_prepare_and_preserves_file(tmp_path: Path) -> None:
    with _stack(tmp_path) as ctx:
        subject = ctx["subject"]
        local = subject / "local-only.txt"
        local.write_bytes(b"do not delete\n")

        with pytest.raises(M7bError) as caught:
            _prepare_checkout(ctx)
        assert caught.value.code == "checkout_precondition_mismatch"
        assert local.read_bytes() == b"do not delete\n"
        assert ctx["m7b"].get(work_id=ctx["checkout_work"].work_id) is None


def test_wrong_symbolic_head_blocks_prepare_without_checkout_mutation(tmp_path: Path) -> None:
    with _stack(tmp_path) as ctx:
        subject = ctx["subject"]
        _git(subject, "symbolic-ref", "HEAD", "refs/heads/main")
        before = (subject / "one.txt").read_bytes()

        with pytest.raises(M7bError) as caught:
            _prepare_checkout(ctx)
        assert caught.value.code == "checkout_precondition_mismatch"
        assert (subject / "one.txt").read_bytes() == before
        assert ctx["m7b"].get(work_id=ctx["checkout_work"].work_id) is None


def test_crash_after_possible_observes_before_then_performs_one_physical_sync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _stack(tmp_path) as ctx:
        prepared = _prepare_checkout(ctx)
        adapter = ctx["m7b"]

        with pytest.raises(M7bError) as caught:
            adapter.apply_if_safe(effect_id=prepared.effect_id, fault="after_possible")
        assert caught.value.code == "simulated_crash_after_possible"
        possible = adapter.get(effect_id=prepared.effect_id)
        assert possible is not None and possible.state == "POSSIBLE" and possible.effect_certainty == "POSSIBLE"
        assert _git(ctx["subject"], "write-tree") == ctx["base"].tree_oid

        observed = adapter.reconcile(effect_id=prepared.effect_id)
        assert observed.effect_certainty == "BEFORE"
        assert observed.state == "POSSIBLE"

        calls = 0
        original = adapter._apply_checkout

        def counted(record):
            nonlocal calls
            calls += 1
            return original(record)

        monkeypatch.setattr(adapter, "_apply_checkout", counted)
        replay = adapter.apply_if_safe(effect_id=prepared.effect_id)
        assert replay.effect_certainty == "AFTER"
        assert calls == 1


def test_crash_after_checkout_update_reconciles_after_without_second_sync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _stack(tmp_path) as ctx:
        prepared = _prepare_checkout(ctx)
        adapter = ctx["m7b"]

        with pytest.raises(M7bError) as caught:
            adapter.apply_if_safe(effect_id=prepared.effect_id, fault="after_checkout_update")
        assert caught.value.code == "simulated_crash_after_checkout_update"
        assert _git(ctx["subject"], "write-tree") == ctx["promotion"].prepared_tree_oid
        persisted = adapter.get(effect_id=prepared.effect_id)
        assert persisted is not None and persisted.effect_certainty == "POSSIBLE"

        calls = 0
        original = adapter._apply_checkout

        def counted(record):
            nonlocal calls
            calls += 1
            return original(record)

        monkeypatch.setattr(adapter, "_apply_checkout", counted)
        replay = adapter.apply_if_safe(effect_id=prepared.effect_id)
        assert replay.effect_certainty == "AFTER"
        assert calls == 0


def test_duplicate_after_does_not_issue_second_physical_checkout_sync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _stack(tmp_path) as ctx:
        prepared = _prepare_checkout(ctx)
        adapter = ctx["m7b"]
        first = adapter.apply_if_safe(effect_id=prepared.effect_id)
        assert first.effect_certainty == "AFTER"

        calls = 0
        original = adapter._apply_checkout

        def counted(record):
            nonlocal calls
            calls += 1
            return original(record)

        monkeypatch.setattr(adapter, "_apply_checkout", counted)
        replay = adapter.apply_if_safe(effect_id=prepared.effect_id)
        assert replay.effect_certainty == "AFTER"
        assert calls == 0


def test_foreign_change_after_prepare_diverges_and_never_runs_checkout_sync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _stack(tmp_path) as ctx:
        prepared = _prepare_checkout(ctx)
        subject = ctx["subject"]
        (subject / "one.txt").write_bytes(b"foreign after prepare\n")

        calls = 0
        original = ctx["m7b"]._apply_checkout

        def counted(record):
            nonlocal calls
            calls += 1
            return original(record)

        monkeypatch.setattr(ctx["m7b"], "_apply_checkout", counted)
        with pytest.raises(M7bError) as caught:
            ctx["m7b"].apply_if_safe(effect_id=prepared.effect_id)
        assert caught.value.code == "checkout_diverged"
        assert calls == 0
        assert (subject / "one.txt").read_bytes() == b"foreign after prepare\n"


def test_lost_prepare_response_replays_same_effect_identity(tmp_path: Path) -> None:
    with _stack(tmp_path) as ctx:
        with pytest.raises(M7bError) as caught:
            _prepare_checkout(ctx, fault="after_prepare_commit")
        assert caught.value.code == "simulated_response_loss_after_prepare"
        effect_id = caught.value.details["effect_id"]
        replay = _prepare_checkout(ctx)
        assert replay.effect_id == effect_id
        assert replay.effect_certainty == "BEFORE"


def test_query_keeps_source_promotion_and_checkout_certainty_separate(tmp_path: Path) -> None:
    with _stack(tmp_path) as ctx:
        prepared = _prepare_checkout(ctx)
        query = ctx["m7b"].query(prepared.effect_id)
        assert query["source_promotion"]["effect_certainty"] == "AFTER"
        assert query["checkout_sync"]["effect_certainty"] == "BEFORE"
        final = ctx["m7b"].apply_if_safe(effect_id=prepared.effect_id)
        query = ctx["m7b"].query(final.effect_id)
        assert query["source_promotion"]["effect_certainty"] == "AFTER"
        assert query["checkout_sync"]["effect_certainty"] == "AFTER"


def test_m7b_uses_same_control_db_and_has_no_ref_push_or_broad_reset_authority(tmp_path: Path) -> None:
    import bdb_vnext.m7b_checkout_sync as module

    with _stack(tmp_path) as ctx:
        assert ctx["m7b"]._connection is ctx["candidate_store"]._connection
        tables = {
            str(row[0])
            for row in ctx["m7b"]._connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "m7b_checkout_effects" in tables
        source = inspect.getsource(module)
        assert "bdb_bridge" not in source
        assert '["push"' not in source
        assert '["reset"' not in source
        assert '["update-ref"' not in source
        assert "promotion_receipt" not in source.lower()
        assert "read-tree" in source

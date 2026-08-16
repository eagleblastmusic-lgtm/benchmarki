from __future__ import annotations

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
    M7aError,
    PreparedGitCasAdapter,
    candidate_subject_identity,
)
from bdb_vnext.repo_view import RepositoryResource


def _run(repo: Path, *args: str, check: bool = True, env: dict[str, str] | None = None, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
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
    _git(repo, "config", "user.name", "M7a Test")
    _git(repo, "config", "user.email", "m7a@example.invalid")
    (repo / "one.txt").write_bytes(b"one\n")
    (repo / "nested").mkdir()
    (repo / "nested" / "two.txt").write_bytes(b"two\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return repo


def _third_commit(repo: Path, parent: str) -> str:
    tree = _git(repo, "rev-parse", f"{parent}^{{tree}}")
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "Concurrent",
            "GIT_AUTHOR_EMAIL": "concurrent@example.invalid",
            "GIT_COMMITTER_NAME": "Concurrent",
            "GIT_COMMITTER_EMAIL": "concurrent@example.invalid",
            "GIT_AUTHOR_DATE": "2026-08-17T00:00:00Z",
            "GIT_COMMITTER_DATE": "2026-08-17T00:00:00Z",
        }
    )
    completed = _run(
        repo,
        "-c",
        "commit.gpgsign=false",
        "commit-tree",
        tree,
        "-p",
        parent,
        env=env,
        input_text="concurrent ref value\n",
    )
    return completed.stdout.strip().lower()


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
def _stack(tmp_path: Path, *, create_target_ref: bool = True):
    runtime = tmp_path / "runtime"
    legacy = tmp_path / "legacy"
    subject = _subject(tmp_path)

    admission = open_vnext_admission_composition(runtime, legacy_root=legacy)
    receipt = admission.authority.admit(
        ShadowSubmissionRequest(
            submission_key="m7a:submission",
            intent_revision="r1",
            intent={"operation": "prepared-git-cas"},
            conversation_binding={"conversation_id": "m7a"},
            consumer_binding={"consumer_id": "m7a", "kind": "browser"},
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

    resource = RepositoryResource.from_path(subject, repository_id="m7a-subject")
    base = resource.resolve_committed("HEAD")

    candidate_work = kernel.create_work_item("work:m7a:candidate", receipt.task_id)
    candidate_lease = kernel.acquire_lease(
        candidate_work.work_id,
        "lease:m7a:candidate",
        "worker:m7a:candidate",
        ttl_seconds=3600,
    )
    workspace = candidate_store.create_workspace(candidate_id="candidate:m7a", base_view=base)
    prepared_candidate = candidate_store.prepare(
        candidate_id="candidate:m7a",
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
    evaluation = checker.check(candidate, request_id="m7a:evidence")
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
            "schema": "bdb-vnext-m7a-test-policy-v1",
            "check_plan_digest": plan.plan_digest,
        }
    )

    promotion_work = kernel.create_work_item(
        "work:m7a:promotion",
        receipt.task_id,
        kind="git-promotion",
    )
    promotion_lease = kernel.acquire_lease(
        promotion_work.work_id,
        "lease:m7a:promotion",
        "worker:m7a:promotion",
        ttl_seconds=3600,
    )
    promotion_run = kernel.start_run(
        promotion_work.work_id,
        "run:m7a:promotion",
        promotion_lease.lease_id,
        promotion_lease.fence,
        promotion_work.state_version,
    )

    target_ref = "refs/bdb-vnext/test/promotion"
    if create_target_ref:
        _git(subject, "update-ref", target_ref, base.commit_oid)

    adapter = PreparedGitCasAdapter(
        candidate_store=candidate_store,
        work_kernel=kernel,
        evidence_policy_gate=gate,
    )

    context = {
        "runtime": runtime,
        "subject": subject,
        "resource": resource,
        "base": base,
        "candidate_store": candidate_store,
        "candidate": candidate,
        "evidence": evidence,
        "gate": gate,
        "obligation": obligation,
        "plan": plan,
        "validation_policy_digest": validation_policy_digest,
        "promotion_work": promotion_work,
        "promotion_lease": promotion_lease,
        "promotion_run": promotion_run,
        "target_ref": target_ref,
        "adapter": adapter,
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


def _prepare(ctx: dict[str, Any], *, fault: str | None = None, expected_old_oid: str | None = None):
    return ctx["adapter"].prepare(
        candidate=ctx["candidate"],
        repository=ctx["resource"],
        work_id=ctx["promotion_work"].work_id,
        run_id=ctx["promotion_run"].run_id,
        target_ref=ctx["target_ref"],
        expected_old_oid=expected_old_oid or ctx["base"].commit_oid,
        metadata=_metadata(),
        intent_revision_id="r1",
        validation_policy_digest=ctx["validation_policy_digest"],
        check_plan_digest=ctx["plan"].plan_digest,
        obligation_ids=(ctx["obligation"].obligation_id,),
        scope="isolated-vnext-ref",
        fault=fault,
    )


def _approval(ctx: dict[str, Any], prepared, *, effect_digest: str | None = None, expires_at: str = "2099-01-01T00:00:00Z"):
    return ctx["gate"].create_approval(
        subject_digest=prepared.subject_digest,
        intent_revision_id=prepared.intent_revision_id,
        effect_digest=effect_digest or prepared.effect_id,
        policy_digest=prepared.validation_policy_digest,
        actor="m7a-test-user",
        authority="USER",
        scope=prepared.scope,
        expires_at=expires_at,
    )


def test_prepare_materializes_exact_commit_without_checkout_index_or_ref_mutation(tmp_path: Path) -> None:
    with _stack(tmp_path) as ctx:
        subject = ctx["subject"]
        before_head = _git(subject, "rev-parse", "HEAD")
        before_status = _git(subject, "status", "--porcelain=v1", "--untracked-files=all")
        before_index = _git(subject, "ls-files", "-s")
        before_ref = _git(subject, "show-ref", "--verify", "--hash", ctx["target_ref"])

        prepared = _prepare(ctx)

        assert prepared.effect_certainty == "BEFORE"
        assert _git(subject, "rev-parse", "HEAD") == before_head
        assert _git(subject, "status", "--porcelain=v1", "--untracked-files=all") == before_status
        assert _git(subject, "ls-files", "-s") == before_index
        assert _git(subject, "show-ref", "--verify", "--hash", ctx["target_ref"]) == before_ref
        assert _git(subject, "rev-parse", f"{prepared.prepared_commit_oid}^{{tree}}") == prepared.prepared_tree_oid
        assert _git(subject, "show", "-s", "--format=%P", prepared.prepared_commit_oid) == ctx["base"].commit_oid
        assert prepared.candidate_tree_digest == ctx["candidate"].candidate_tree_digest
        assert prepared.resource_key.startswith("git-ref:")
        assert ctx["adapter"].query(prepared.effect_id)["safe_next_action"] == "SAFE_TO_APPLY"


def test_prepare_preserves_preexisting_staged_unstaged_and_untracked_state(tmp_path: Path) -> None:
    with _stack(tmp_path) as ctx:
        subject = ctx["subject"]
        (subject / "one.txt").write_bytes(b"locally staged\n")
        _git(subject, "add", "one.txt")
        (subject / "one.txt").write_bytes(b"locally unstaged\n")
        (subject / "local-only.txt").write_bytes(b"untracked\n")
        before_status = _git(subject, "status", "--porcelain=v1", "--untracked-files=all")
        before_index = _git(subject, "ls-files", "-s")
        before_head = _git(subject, "rev-parse", "HEAD")

        prepared = _prepare(ctx)

        assert prepared.effect_certainty == "BEFORE"
        assert _git(subject, "status", "--porcelain=v1", "--untracked-files=all") == before_status
        assert _git(subject, "ls-files", "-s") == before_index
        assert _git(subject, "rev-parse", "HEAD") == before_head
        assert (subject / "one.txt").read_bytes() == b"locally unstaged\n"
        assert (subject / "local-only.txt").read_bytes() == b"untracked\n"


def test_exact_cas_moves_only_target_ref_and_reconciles_after(tmp_path: Path) -> None:
    with _stack(tmp_path) as ctx:
        subject = ctx["subject"]
        before_head = _git(subject, "rev-parse", "HEAD")
        prepared = _prepare(ctx)
        approval = _approval(ctx, prepared)

        final = ctx["adapter"].apply_if_safe(
            effect_id=prepared.effect_id,
            approval_id=approval.approval_id,
            now="2026-08-17T01:00:00Z",
        )

        assert final.effect_certainty == "AFTER"
        assert final.state == "AFTER"
        assert final.safe_next_action == "COMPLETE_ALLOWED"
        assert _git(subject, "show-ref", "--verify", "--hash", ctx["target_ref"]) == prepared.prepared_commit_oid
        assert _git(subject, "rev-parse", "HEAD") == before_head


def test_duplicate_apply_after_after_does_not_issue_second_ref_update(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _stack(tmp_path) as ctx:
        prepared = _prepare(ctx)
        approval = _approval(ctx, prepared)
        adapter = ctx["adapter"]
        first = adapter.apply_if_safe(
            effect_id=prepared.effect_id,
            approval_id=approval.approval_id,
            now="2026-08-17T01:00:00Z",
        )
        assert first.effect_certainty == "AFTER"

        calls = 0
        original = adapter._cas_update

        def counted(*args, **kwargs):
            nonlocal calls
            calls += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(adapter, "_cas_update", counted)
        replay = adapter.apply_if_safe(
            effect_id=prepared.effect_id,
            approval_id=approval.approval_id,
            now="2026-08-17T01:00:01Z",
        )
        assert replay.effect_certainty == "AFTER"
        assert calls == 0


def test_crash_after_possible_observes_old_before_retry_and_applies_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _stack(tmp_path) as ctx:
        prepared = _prepare(ctx)
        approval = _approval(ctx, prepared)
        adapter = ctx["adapter"]

        with pytest.raises(M7aError) as caught:
            adapter.apply_if_safe(
                effect_id=prepared.effect_id,
                approval_id=approval.approval_id,
                now="2026-08-17T01:00:00Z",
                fault="after_possible",
            )
        assert caught.value.code == "simulated_crash_after_possible"
        possible = adapter.get(effect_id=prepared.effect_id)
        assert possible is not None and possible.state == "POSSIBLE" and possible.effect_certainty == "POSSIBLE"
        assert _git(ctx["subject"], "show-ref", "--verify", "--hash", ctx["target_ref"]) == ctx["base"].commit_oid

        observed = adapter.reconcile(effect_id=prepared.effect_id)
        assert observed.effect_certainty == "BEFORE"
        assert observed.state == "POSSIBLE"

        calls = 0
        original = adapter._cas_update

        def counted(*args, **kwargs):
            nonlocal calls
            calls += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(adapter, "_cas_update", counted)
        final = adapter.apply_if_safe(
            effect_id=prepared.effect_id,
            approval_id=approval.approval_id,
            now="2026-08-17T01:00:01Z",
        )
        assert final.effect_certainty == "AFTER"
        assert calls == 1


def test_crash_after_ref_update_reconciles_without_second_cas(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _stack(tmp_path) as ctx:
        prepared = _prepare(ctx)
        approval = _approval(ctx, prepared)
        adapter = ctx["adapter"]

        with pytest.raises(M7aError) as caught:
            adapter.apply_if_safe(
                effect_id=prepared.effect_id,
                approval_id=approval.approval_id,
                now="2026-08-17T01:00:00Z",
                fault="after_ref_update",
            )
        assert caught.value.code == "simulated_crash_after_ref_update"
        assert _git(ctx["subject"], "show-ref", "--verify", "--hash", ctx["target_ref"]) == prepared.prepared_commit_oid
        persisted = adapter.get(effect_id=prepared.effect_id)
        assert persisted is not None and persisted.effect_certainty == "POSSIBLE"

        calls = 0
        original = adapter._cas_update

        def counted(*args, **kwargs):
            nonlocal calls
            calls += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(adapter, "_cas_update", counted)
        replay = adapter.apply_if_safe(
            effect_id=prepared.effect_id,
            approval_id=approval.approval_id,
            now="2026-08-17T01:00:01Z",
        )
        assert replay.effect_certainty == "AFTER"
        assert calls == 0


def test_concurrent_third_oid_is_diverged_and_never_overwritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _stack(tmp_path) as ctx:
        prepared = _prepare(ctx)
        approval = _approval(ctx, prepared)
        third = _third_commit(ctx["subject"], ctx["base"].commit_oid)
        _git(ctx["subject"], "update-ref", ctx["target_ref"], third, ctx["base"].commit_oid)

        calls = 0
        original = ctx["adapter"]._cas_update

        def counted(*args, **kwargs):
            nonlocal calls
            calls += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(ctx["adapter"], "_cas_update", counted)
        with pytest.raises(M7aError) as caught:
            ctx["adapter"].apply_if_safe(
                effect_id=prepared.effect_id,
                approval_id=approval.approval_id,
                now="2026-08-17T01:00:00Z",
            )
        assert caught.value.code == "git_ref_diverged"
        assert calls == 0
        assert _git(ctx["subject"], "show-ref", "--verify", "--hash", ctx["target_ref"]) == third
        state = ctx["adapter"].get(effect_id=prepared.effect_id)
        assert state is not None and state.effect_certainty == "DIVERGED"


def test_update_ref_race_to_third_oid_is_observed_not_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _stack(tmp_path) as ctx:
        prepared = _prepare(ctx)
        approval = _approval(ctx, prepared)
        adapter = ctx["adapter"]
        third = _third_commit(ctx["subject"], ctx["base"].commit_oid)
        calls = 0

        def racing(repository, record):
            nonlocal calls
            calls += 1
            _git(ctx["subject"], "update-ref", record.target_ref, third, record.expected_old_oid)
            return subprocess.CompletedProcess(args=["git", "update-ref"], returncode=1, stdout=b"", stderr=b"race")

        monkeypatch.setattr(adapter, "_cas_update", racing)
        with pytest.raises(M7aError) as caught:
            adapter.apply_if_safe(
                effect_id=prepared.effect_id,
                approval_id=approval.approval_id,
                now="2026-08-17T01:00:00Z",
            )
        assert caught.value.code == "git_ref_diverged"
        assert calls == 1
        assert _git(ctx["subject"], "show-ref", "--verify", "--hash", ctx["target_ref"]) == third


def test_absent_ref_cas_uses_null_old_oid_and_creates_only_isolated_ref(tmp_path: Path) -> None:
    with _stack(tmp_path, create_target_ref=False) as ctx:
        null_oid = "0" * (40 if ctx["resource"].object_format == "sha1" else 64)
        prepared = _prepare(ctx, expected_old_oid=null_oid)
        approval = _approval(ctx, prepared)
        final = ctx["adapter"].apply_if_safe(
            effect_id=prepared.effect_id,
            approval_id=approval.approval_id,
            now="2026-08-17T01:00:00Z",
        )
        assert final.effect_certainty == "AFTER"
        assert _git(ctx["subject"], "show-ref", "--verify", "--hash", ctx["target_ref"]) == prepared.prepared_commit_oid


def test_ref_outside_vnext_namespace_is_fail_closed(tmp_path: Path) -> None:
    with _stack(tmp_path) as ctx:
        ctx["target_ref"] = "refs/heads/main"
        with pytest.raises(M7aError) as caught:
            _prepare(ctx)
        assert caught.value.code == "production_ref_forbidden"
        assert _git(ctx["subject"], "rev-parse", "HEAD") == ctx["base"].commit_oid


def test_stale_candidate_blocks_ref_effect(tmp_path: Path) -> None:
    with _stack(tmp_path) as ctx:
        prepared = _prepare(ctx)
        approval = _approval(ctx, prepared)
        workspace = Path(ctx["candidate_store"].get(ctx["candidate"].candidate_id).workspace_root)
        (workspace / "one.txt").write_bytes(b"tampered after prepare\n")
        ctx["candidate_store"].invalidate_if_changed(ctx["candidate"].candidate_id)

        with pytest.raises(M7aError) as caught:
            ctx["adapter"].apply_if_safe(
                effect_id=prepared.effect_id,
                approval_id=approval.approval_id,
                now="2026-08-17T01:00:00Z",
            )
        assert caught.value.code == "candidate_not_current"
        assert _git(ctx["subject"], "show-ref", "--verify", "--hash", ctx["target_ref"]) == ctx["base"].commit_oid


def test_wrong_or_expired_approval_blocks_before_possible_and_ref(tmp_path: Path) -> None:
    with _stack(tmp_path) as ctx:
        prepared = _prepare(ctx)
        wrong = _approval(ctx, prepared, effect_digest="sha256:" + "f" * 64)
        with pytest.raises(M7aError) as caught:
            ctx["adapter"].apply_if_safe(
                effect_id=prepared.effect_id,
                approval_id=wrong.approval_id,
                now="2026-08-17T01:00:00Z",
            )
        assert caught.value.code == "promotion_not_authorized"
        current = ctx["adapter"].get(effect_id=prepared.effect_id)
        assert current is not None and current.effect_certainty == "BEFORE"
        assert _git(ctx["subject"], "show-ref", "--verify", "--hash", ctx["target_ref"]) == ctx["base"].commit_oid

        expired = _approval(ctx, prepared, expires_at="2026-08-16T00:00:00Z")
        with pytest.raises(M7aError) as caught:
            ctx["adapter"].apply_if_safe(
                effect_id=prepared.effect_id,
                approval_id=expired.approval_id,
                now="2026-08-17T01:00:00Z",
            )
        assert caught.value.code == "promotion_not_authorized"


def test_fault_after_objects_has_no_durable_git_effect_and_no_ref_move(tmp_path: Path) -> None:
    with _stack(tmp_path) as ctx:
        with pytest.raises(M7aError) as caught:
            _prepare(ctx, fault="after_objects")
        assert caught.value.code == "simulated_crash_after_objects"
        assert ctx["adapter"].get(work_id=ctx["promotion_work"].work_id) is None
        assert _git(ctx["subject"], "show-ref", "--verify", "--hash", ctx["target_ref"]) == ctx["base"].commit_oid
        # Repeat is safe: immutable objects may remain, but exact prepare succeeds.
        prepared = _prepare(ctx)
        assert prepared.effect_certainty == "BEFORE"


def test_lost_prepare_response_replays_same_effect_identity(tmp_path: Path) -> None:
    with _stack(tmp_path) as ctx:
        with pytest.raises(M7aError) as caught:
            _prepare(ctx, fault="after_prepare_commit")
        assert caught.value.code == "simulated_response_loss_after_prepare"
        effect_id = caught.value.details["effect_id"]
        replay = _prepare(ctx)
        assert replay.effect_id == effect_id
        assert replay.effect_certainty == "BEFORE"


def test_m7a_uses_same_control_db_and_no_legacy_receipt_authority(tmp_path: Path) -> None:
    import inspect
    import bdb_vnext.m7a_git_cas as module

    with _stack(tmp_path) as ctx:
        assert ctx["adapter"]._connection is ctx["candidate_store"]._connection
        tables = {
            str(row[0])
            for row in ctx["adapter"]._connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "m7a_git_effects" in tables
        source = inspect.getsource(module)
        assert "bdb_bridge" not in source
        assert "promotion_receipt" not in source.lower()
        assert "repository_event_seq" not in source.lower()
        assert "merge --ff-only" not in source.lower()
        assert '["checkout"' not in source
        assert '["merge"' not in source
        assert '["push"' not in source
        assert '["reset"' not in source

from __future__ import annotations

import inspect
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
from bdb_vnext.m4c_evidence import EvidenceStore
from bdb_vnext.m6a_evidence_policy import EvidencePolicyGate
from bdb_vnext.m6c_validation_authority import CanonicalValidationAuthority
from bdb_vnext.m7a_git_cas import (
    CommitMetadataPolicy,
    M7aError,
    PreparedGitCasAdapter,
    candidate_subject_identity,
)
from bdb_vnext.m7b_checkout_sync import CheckoutSyncAdapter
from bdb_vnext.m7c_promotion_authority import CanonicalGitPromotionAuthority, M7cError
from bdb_vnext.repo_view import RepositoryResource


def _run(
    repo: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _git(repo: Path, *args: str) -> str:
    return _run(repo, *args).stdout.strip()


def _subject(root: Path) -> Path:
    repo = root / "subject"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "M7c Test")
    _git(repo, "config", "user.email", "m7c@example.invalid")
    (repo / "one.txt").write_bytes(b"one\n")
    (repo / "nested").mkdir()
    (repo / "nested" / "two.txt").write_bytes(b"two\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return repo


def _metadata() -> CommitMetadataPolicy:
    return CommitMetadataPolicy(
        message="bdb-vnext: canonical M7c candidate",
        author_name="BDB vNext",
        author_email="bdb-vnext@localhost.invalid",
        committer_name="BDB vNext",
        committer_email="bdb-vnext@localhost.invalid",
        timestamp="2026-08-17T00:00:00Z",
    )


def _record_validation(ctx: dict[str, Any], *, suffix: str = "pass") -> tuple[Any, Any, Any]:
    candidate = ctx["candidate"]
    flow = ctx["flow"]
    check = flow.plan["checks"][0]
    environment_fingerprint = semantic_digest(
        {"schema": "bdb-vnext-m7c-test-environment-v1", "suffix": suffix}
    )
    subject_identity = candidate_subject_identity(candidate)
    evidence = ctx["evidence"].record_observation(
        request_id=f"m7c:evidence:{suffix}",
        primary_subject_kind="CANDIDATE",
        primary_subject_identity=subject_identity,
        candidate_view_id=candidate.view_id,
        raw_observation={
            "schema": "m4c-raw-observation-v1",
            "result": "PASS",
            "capability_id": "python.pytest",
            "plan_digest": flow.plan_digest,
        },
        checker_id=str(check["checker_id"]),
        checker_version=str(check["checker_version"]),
        checker_code_digest=str(check["checker_code_digest"]),
        environment={"fingerprint": environment_fingerprint},
        observation_started_at="2026-08-17T00:00:00Z",
        observation_finished_at="2026-08-17T00:00:01Z",
        completeness="COMPLETE",
        applicability="APPLICABLE",
        status="PASS",
    )
    config_digest = semantic_digest(
        {"schema": "bdb-vnext-m7c-test-evaluator-v1", "flow": flow.policy_digest}
    )
    ctx["evidence"].evaluate(
        evidence_id=evidence.evidence_id,
        evaluator_id="m7c-test-evaluator",
        evaluator_version="1",
        evaluator_code_digest=str(check["checker_code_digest"]),
        config_digest=config_digest,
        result="PASS",
        applicability="APPLICABLE",
        detail={"capability_id": "python.pytest"},
    )
    obligation = ctx["gate"].create_obligation(
        subject_kind="CANDIDATE",
        subject_identity=subject_identity,
        requirement="canonical-M7c-promotion-validation",
        evidence_contract={
            "evidence_type": "validation-check",
            "coverage": "exact",
            "freshness": "CURRENT",
            "checker_id": str(check["checker_id"]),
            "checker_version": str(check["checker_version"]),
            "checker_code_digest": str(check["checker_code_digest"]),
            "environment_fingerprint": environment_fingerprint,
            "evaluation_config_digest": config_digest,
        },
        waivability="NEVER",
        risk="HIGH",
    )
    assessment = ctx["gate"].assess_obligation(
        obligation.obligation_id,
        evidence_id=evidence.evidence_id,
    )
    assert assessment.verdict == "PASS"
    return evidence, obligation, assessment


@contextmanager
def _stack(tmp_path: Path):
    runtime = tmp_path / "runtime"
    legacy = tmp_path / "legacy"
    subject = _subject(tmp_path)
    admission = open_vnext_admission_composition(runtime, legacy_root=legacy)
    receipt = admission.authority.admit(
        ShadowSubmissionRequest(
            submission_key="m7c:submission",
            intent_revision="r1",
            intent={"operation": "canonical-git-promotion"},
            conversation_binding={"conversation_id": "m7c"},
            consumer_binding={"consumer_id": "m7c", "kind": "browser"},
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
    m6c = CanonicalValidationAuthority(evidence_policy_gate=gate)
    flow = m6c.activate_flow(
        flow_id="git-promotion",
        policy_revision="m7c-test-v1",
        generation="bdb-vnext-g1",
        scope="isolated-vnext-ref",
        required_capabilities=("python.pytest",),
        executable_bindings={"python": sys.executable},
    )

    resource = RepositoryResource.from_path(subject, repository_id="m7c-subject")
    base = resource.resolve_committed("HEAD")
    candidate_work = kernel.create_work_item("work:m7c:candidate", receipt.task_id)
    candidate_lease = kernel.acquire_lease(
        candidate_work.work_id,
        "lease:m7c:candidate",
        "worker:m7c:candidate",
        ttl_seconds=3600,
    )
    workspace = candidate_store.create_workspace(candidate_id="candidate:m7c", base_view=base)
    prepared_candidate = candidate_store.prepare(
        candidate_id="candidate:m7c",
        work_id=candidate_work.work_id,
        task_id=receipt.task_id,
        lease_id=candidate_lease.lease_id,
        fence=candidate_lease.fence,
        base_view=base,
        workspace_root=workspace,
        replacements={
            "one.txt": b"canonical\n",
            "nested/two.txt": b"canonical-two\n",
        },
    )
    candidate_store.apply(prepared_candidate.candidate_id)
    _sealed, candidate = candidate_store.seal(prepared_candidate.candidate_id, base_view=base)

    m7a = PreparedGitCasAdapter(
        candidate_store=candidate_store,
        work_kernel=kernel,
        evidence_policy_gate=gate,
    )
    m7b = CheckoutSyncAdapter(m7a_adapter=m7a, work_kernel=kernel)
    m7c = CanonicalGitPromotionAuthority(
        validation_authority=m6c,
        m7a_adapter=m7a,
        m7b_adapter=m7b,
    )
    cutover = m7c.activate_cutover(flow_id=flow.flow_id)

    ctx: dict[str, Any] = {
        "runtime": runtime,
        "legacy": legacy,
        "subject": subject,
        "receipt": receipt,
        "admission": admission,
        "kernel": kernel,
        "candidate_store": candidate_store,
        "evidence": evidence,
        "gate": gate,
        "m6c": m6c,
        "flow": flow,
        "resource": resource,
        "base": base,
        "candidate": candidate,
        "m7a": m7a,
        "m7b": m7b,
        "m7c": m7c,
        "cutover": cutover,
    }
    evidence_record, obligation, assessment = _record_validation(ctx)
    ctx.update({"evidence_record": evidence_record, "obligation": obligation, "assessment": assessment})

    promotion_work = kernel.create_work_item(
        "work:m7c:promotion",
        receipt.task_id,
        kind="git-promotion",
    )
    promotion_lease = kernel.acquire_lease(
        promotion_work.work_id,
        "lease:m7c:promotion",
        "worker:m7c:promotion",
        ttl_seconds=3600,
    )
    promotion_run = kernel.start_run(
        promotion_work.work_id,
        "run:m7c:promotion",
        promotion_lease.lease_id,
        promotion_lease.fence,
        promotion_work.state_version,
    )
    target_ref = "refs/bdb-vnext/test/m7c"
    _git(subject, "update-ref", target_ref, base.commit_oid)
    promotion = m7c.prepare(
        flow_id=flow.flow_id,
        capability_bindings={
            "python.pytest": {
                "obligation_id": obligation.obligation_id,
                "evidence_id": evidence_record.evidence_id,
            }
        },
        candidate=candidate,
        repository=resource,
        work_id=promotion_work.work_id,
        run_id=promotion_run.run_id,
        target_ref=target_ref,
        expected_old_oid=base.commit_oid,
        metadata=_metadata(),
        intent_revision_id="r1",
    )
    approval = gate.create_approval(
        subject_digest=promotion.subject_digest,
        intent_revision_id=promotion.intent_revision_id,
        effect_digest=promotion.effect_id,
        policy_digest=flow.policy_digest,
        actor="m7c-test-user",
        authority="USER",
        scope=flow.scope,
        expires_at="2099-01-01T00:00:00Z",
    )
    ctx.update(
        {
            "promotion_work": promotion_work,
            "promotion_run": promotion_run,
            "target_ref": target_ref,
            "promotion": promotion,
            "approval": approval,
        }
    )
    try:
        yield ctx
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


def _apply(ctx: dict[str, Any], *, fault: str | None = None):
    return ctx["m7c"].apply_if_safe(
        effect_id=ctx["promotion"].effect_id,
        approval_id=ctx["approval"].approval_id,
        now="2026-08-17T01:00:00Z",
        fault=fault,
    )


def test_m7c_prepare_derives_policy_plan_and_exact_evidence_binding(tmp_path: Path) -> None:
    with _stack(tmp_path) as ctx:
        effect = ctx["promotion"]
        binding = ctx["m7c"].get_binding(effect.effect_id)
        assert binding is not None
        assert effect.validation_policy_digest == ctx["flow"].policy_digest
        assert effect.check_plan_digest == ctx["flow"].plan_digest
        assert effect.scope == ctx["flow"].scope
        assert binding.flow_revision_id == ctx["flow"].revision_id
        assert binding.capability_bindings["python.pytest"]["obligation_id"] == ctx["obligation"].obligation_id
        assert binding.capability_bindings["python.pytest"]["evidence_id"] == ctx["evidence_record"].evidence_id


def test_exact_m6c_gate_then_m7a_cas_reaches_after(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _stack(tmp_path) as ctx:
        calls = 0
        original = ctx["m6c"].authorize

        def counted(**kwargs):
            nonlocal calls
            calls += 1
            return original(**kwargs)

        monkeypatch.setattr(ctx["m6c"], "authorize", counted)
        final = _apply(ctx)
        assert final.effect_certainty == "AFTER"
        assert calls == 1
        assert _git(ctx["subject"], "show-ref", "--verify", "--hash", ctx["target_ref"]) == final.prepared_commit_oid


def test_direct_unwired_m7a_cannot_bypass_active_m7c_cutover(tmp_path: Path) -> None:
    with _stack(tmp_path) as ctx:
        direct = PreparedGitCasAdapter(
            candidate_store=ctx["candidate_store"],
            work_kernel=ctx["kernel"],
            evidence_policy_gate=ctx["gate"],
        )
        with pytest.raises(M7aError) as caught:
            direct.apply_if_safe(
                effect_id=ctx["promotion"].effect_id,
                approval_id=ctx["approval"].approval_id,
                now="2026-08-17T01:00:00Z",
            )
        assert caught.value.code == "m7c_authority_required"
        assert _git(ctx["subject"], "show-ref", "--verify", "--hash", ctx["target_ref"]) == ctx["base"].commit_oid


def test_paused_cutover_blocks_m7a_instead_of_falling_back_to_m6a(tmp_path: Path) -> None:
    with _stack(tmp_path) as ctx:
        ctx["m7c"].pause_cutover(ctx["flow"].flow_id)
        with pytest.raises(M7aError) as caught:
            ctx["m7a"].apply_if_safe(
                effect_id=ctx["promotion"].effect_id,
                approval_id=ctx["approval"].approval_id,
                now="2026-08-17T01:00:00Z",
            )
        assert caught.value.code == "m7c_authority_paused"
        assert _git(ctx["subject"], "show-ref", "--verify", "--hash", ctx["target_ref"]) == ctx["base"].commit_oid


def test_missing_capability_binding_blocks_before_new_git_effect(tmp_path: Path) -> None:
    with _stack(tmp_path) as ctx:
        with pytest.raises(M7cError) as caught:
            ctx["m7c"].prepare(
                flow_id=ctx["flow"].flow_id,
                capability_bindings={},
                candidate=ctx["candidate"],
                repository=ctx["resource"],
                work_id="unused-work",
                run_id="unused-run",
                target_ref="refs/bdb-vnext/test/unused",
                expected_old_oid=ctx["base"].commit_oid,
                metadata=_metadata(),
                intent_revision_id="r1",
            )
        assert caught.value.code == "promotion_evidence_coverage_mismatch"


def test_policy_revision_change_after_prepare_blocks_before_cas(tmp_path: Path) -> None:
    with _stack(tmp_path) as ctx:
        new_flow = ctx["m6c"].activate_flow(
            flow_id="git-promotion",
            policy_revision="m7c-test-v2",
            generation="bdb-vnext-g1",
            scope="isolated-vnext-ref",
            required_capabilities=("python.pytest",),
            executable_bindings={"python": sys.executable},
        )
        assert new_flow.revision_id != ctx["flow"].revision_id
        with pytest.raises(M7cError) as caught:
            _apply(ctx)
        assert caught.value.code == "promotion_policy_stale"
        assert _git(ctx["subject"], "show-ref", "--verify", "--hash", ctx["target_ref"]) == ctx["base"].commit_oid


def test_stale_candidate_after_pass_cannot_reach_cas(tmp_path: Path) -> None:
    with _stack(tmp_path) as ctx:
        workspace = Path(ctx["candidate_store"].get(ctx["candidate"].candidate_id).workspace_root)
        (workspace / "one.txt").write_bytes(b"foreign\n")
        ctx["candidate_store"].invalidate_if_changed(ctx["candidate"].candidate_id)
        with pytest.raises(M7aError) as caught:
            _apply(ctx)
        assert caught.value.code == "candidate_not_current"
        assert _git(ctx["subject"], "show-ref", "--verify", "--hash", ctx["target_ref"]) == ctx["base"].commit_oid


def test_expired_approval_blocks_canonical_cas(tmp_path: Path) -> None:
    with _stack(tmp_path) as ctx:
        expired = ctx["gate"].create_approval(
            subject_digest=ctx["promotion"].subject_digest,
            intent_revision_id=ctx["promotion"].intent_revision_id,
            effect_digest=ctx["promotion"].effect_id,
            policy_digest=ctx["flow"].policy_digest,
            actor="m7c-expired-user",
            authority="USER",
            scope=ctx["flow"].scope,
            expires_at="2026-08-17T00:30:00Z",
        )
        with pytest.raises(M7aError) as caught:
            ctx["m7c"].apply_if_safe(
                effect_id=ctx["promotion"].effect_id,
                approval_id=expired.approval_id,
                now="2026-08-17T01:00:00Z",
            )
        assert caught.value.code == "promotion_not_authorized"
        assert _git(ctx["subject"], "show-ref", "--verify", "--hash", ctx["target_ref"]) == ctx["base"].commit_oid


def test_crash_after_possible_reobserves_before_then_completes_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _stack(tmp_path) as ctx:
        with pytest.raises(M7aError) as caught:
            _apply(ctx, fault="after_possible")
        assert caught.value.code == "simulated_crash_after_possible"
        possible = ctx["m7a"].get(effect_id=ctx["promotion"].effect_id)
        assert possible is not None and possible.effect_certainty == "POSSIBLE"
        assert _git(ctx["subject"], "show-ref", "--verify", "--hash", ctx["target_ref"]) == ctx["base"].commit_oid

        calls = 0
        original = ctx["m7a"]._cas_update

        def counted(repository, record):
            nonlocal calls
            calls += 1
            return original(repository, record)

        monkeypatch.setattr(ctx["m7a"], "_cas_update", counted)
        final = _apply(ctx)
        assert final.effect_certainty == "AFTER"
        assert calls == 1


def test_third_ref_oid_diverges_and_is_never_overwritten(tmp_path: Path) -> None:
    with _stack(tmp_path) as ctx:
        subject = ctx["subject"]
        (subject / "one.txt").write_bytes(b"third\n")
        _git(subject, "add", "one.txt")
        _git(subject, "commit", "-qm", "third")
        third = _git(subject, "rev-parse", "HEAD")
        _git(subject, "update-ref", ctx["target_ref"], third)
        with pytest.raises(M7aError) as caught:
            _apply(ctx)
        assert caught.value.code == "git_ref_diverged"
        assert _git(subject, "show-ref", "--verify", "--hash", ctx["target_ref"]) == third


def test_ref_after_does_not_imply_checkout_after_then_separate_m7b_sync(tmp_path: Path) -> None:
    with _stack(tmp_path) as ctx:
        promotion = _apply(ctx)
        query = ctx["m7c"].query(promotion.effect_id)
        assert query["source_promotion"]["effect_certainty"] == "AFTER"
        assert query["checkout_sync"]["state"] == "NOT_PREPARED"
        assert query["checkout_sync"]["effect_certainty"] == "NOT_ASSESSED"

        # Attach HEAD to the already-promoted isolated ref without changing the
        # index/worktree. M7b then owns the separate physical synchronization.
        _git(ctx["subject"], "symbolic-ref", "HEAD", ctx["target_ref"])
        checkout_work = ctx["kernel"].create_work_item(
            "work:m7c:checkout",
            ctx["receipt"].task_id,
            kind="checkout-sync",
        )
        checkout_lease = ctx["kernel"].acquire_lease(
            checkout_work.work_id,
            "lease:m7c:checkout",
            "worker:m7c:checkout",
            ttl_seconds=3600,
        )
        checkout_run = ctx["kernel"].start_run(
            checkout_work.work_id,
            "run:m7c:checkout",
            checkout_lease.lease_id,
            checkout_lease.fence,
            checkout_work.state_version,
        )
        checkout = ctx["m7b"].prepare(
            source_promotion_effect_id=promotion.effect_id,
            work_id=checkout_work.work_id,
            run_id=checkout_run.run_id,
        )
        checkout = ctx["m7b"].apply_if_safe(effect_id=checkout.effect_id)
        assert checkout.effect_certainty == "AFTER"
        query = ctx["m7c"].query(promotion.effect_id)
        assert query["source_promotion"]["effect_certainty"] == "AFTER"
        assert query["checkout_sync"]["effect_certainty"] == "AFTER"


def test_m7c_is_same_db_policy_binding_not_second_git_writer(tmp_path: Path) -> None:
    import bdb_vnext.m7c_promotion_authority as module

    with _stack(tmp_path) as ctx:
        def main_db_path(connection):
            return next(
                str(Path(str(row[2])).resolve())
                for row in connection.execute("PRAGMA database_list").fetchall()
                if str(row[1]) == "main"
            )

        assert ctx["m7c"]._connection is ctx["m7a"]._connection
        assert {
            main_db_path(ctx["m7c"]._connection),
            main_db_path(ctx["m6c"]._connection),
            main_db_path(ctx["m7b"]._connection),
        } == {main_db_path(ctx["m7a"]._connection)}
        query = ctx["m7c"].query(ctx["promotion"].effect_id)
        assert query["production_activation"] is False
        source = inspect.getsource(module)
        assert "subprocess" not in source
        assert "update-ref" not in source
        assert "git push" not in source.lower()
        assert "git reset" not in source.lower()
        assert "profile_id" not in source
        assert "fixed_test_profiles" not in source
        assert "watcher" not in source.lower()
        assert "receipt" not in source.lower()
        assert "seen" not in source.lower()
        tables = {
            str(row[0])
            for row in ctx["m7c"]._connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "m7c_promotion_cutovers" in tables
        assert "m7c_promotion_bindings" in tables
        assert not any("proof" in name.lower() for name in tables if name.startswith("m7c_"))

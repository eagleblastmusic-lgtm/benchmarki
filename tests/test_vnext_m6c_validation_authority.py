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
from bdb_vnext.m6a_evidence_policy import EvidencePolicyGate, compute_subject_digest
from bdb_vnext.m6c_validation_authority import CanonicalValidationAuthority, M6cError
from bdb_vnext.repo_view import RepositoryResource


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _subject(root: Path) -> Path:
    repo = root / "subject"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "M6c Test")
    _git(repo, "config", "user.email", "m6c@example.invalid")
    (repo / "one.txt").write_bytes(b"one\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return repo


def _candidate_subject(candidate: Any) -> dict[str, Any]:
    return {
        "candidate_id": candidate.candidate_id,
        "view_id": candidate.view_id,
        "manifest_digest": candidate.manifest_digest,
        "candidate_tree_digest": candidate.candidate_tree_digest,
        "base_view_id": candidate.base_view_id,
        "repository_id": candidate.repository_id,
    }


@contextmanager
def _stack(tmp_path: Path):
    runtime = tmp_path / "runtime"
    legacy = tmp_path / "legacy"
    subject = _subject(tmp_path)
    admission = open_vnext_admission_composition(runtime, legacy_root=legacy)
    receipt = admission.authority.admit(
        ShadowSubmissionRequest(
            submission_key="m6c:submission",
            intent_revision="r1",
            intent={"operation": "validation-authority"},
            conversation_binding={"conversation_id": "m6c"},
            consumer_binding={"consumer_id": "m6c", "kind": "browser"},
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
    authority = CanonicalValidationAuthority(evidence_policy_gate=gate)

    view = RepositoryResource.from_path(subject, repository_id="m6c-subject").resolve_committed("HEAD")
    work = kernel.create_work_item("work:m6c", receipt.task_id)
    lease = kernel.acquire_lease(work.work_id, "lease:m6c", "worker:m6c")
    workspace = candidate_store.create_workspace(candidate_id="candidate:m6c", base_view=view)
    prepared = candidate_store.prepare(
        candidate_id="candidate:m6c",
        work_id=work.work_id,
        task_id=receipt.task_id,
        lease_id=lease.lease_id,
        fence=lease.fence,
        base_view=view,
        workspace_root=workspace,
        replacements={"one.txt": b"checked\n"},
    )
    candidate_store.apply(prepared.candidate_id)
    _sealed, candidate = candidate_store.seal(prepared.candidate_id, base_view=view)

    flow = authority.activate_flow(
        flow_id="git-promotion",
        policy_revision="m6c-test-v1",
        generation="bdb-vnext-g1",
        scope="isolated-vnext-ref",
        required_capabilities=("python.pytest",),
        executable_bindings={"python": sys.executable},
    )
    try:
        yield {
            "runtime": runtime,
            "subject": subject,
            "admission": admission,
            "kernel": kernel,
            "candidate_store": candidate_store,
            "candidate": candidate,
            "evidence": evidence,
            "gate": gate,
            "authority": authority,
            "flow": flow,
        }
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


def _record(
    ctx: dict[str, Any],
    *,
    suffix: str,
    result: str = "PASS",
    checker_id: str | None = None,
    checker_version: str | None = None,
    checker_code_digest: str | None = None,
    include_environment_contract: bool = True,
    waivability: str = "NEVER",
):
    candidate = ctx["candidate"]
    evidence = ctx["evidence"]
    gate = ctx["gate"]
    flow = ctx["flow"]
    check = flow.plan["checks"][0]
    environment_fingerprint = semantic_digest(
        {"schema": "bdb-vnext-m6c-test-environment-v1", "suffix": suffix}
    )
    subject_identity = _candidate_subject(candidate)
    rec = evidence.record_observation(
        request_id=f"m6c:evidence:{suffix}",
        primary_subject_kind="CANDIDATE",
        primary_subject_identity=subject_identity,
        candidate_view_id=candidate.view_id,
        raw_observation={
            "schema": "m4c-raw-observation-v1",
            "result": result,
            "capability_id": "python.pytest",
            "plan_digest": flow.plan_digest,
        },
        checker_id=checker_id or str(check["checker_id"]),
        checker_version=checker_version or str(check["checker_version"]),
        checker_code_digest=checker_code_digest or str(check["checker_code_digest"]),
        environment={"fingerprint": environment_fingerprint},
        observation_started_at="2026-08-17T00:00:00Z",
        observation_finished_at="2026-08-17T00:00:01Z",
        completeness="COMPLETE",
        applicability="APPLICABLE",
        status="PASS" if result == "PASS" else "FAILED",
    )
    config_digest = semantic_digest(
        {"schema": "bdb-vnext-m6c-test-evaluator-v1", "flow": flow.policy_digest}
    )
    evidence.evaluate(
        evidence_id=rec.evidence_id,
        evaluator_id="m6c-test-evaluator",
        evaluator_version="1",
        evaluator_code_digest=str(check["checker_code_digest"]),
        config_digest=config_digest,
        result=result,
        applicability="APPLICABLE",
        detail={"capability_id": "python.pytest"},
    )
    contract: dict[str, Any] = {
        "evidence_type": "validation-check",
        "coverage": "exact",
        "freshness": "CURRENT",
        "checker_id": str(check["checker_id"]),
        "checker_version": str(check["checker_version"]),
        "checker_code_digest": str(check["checker_code_digest"]),
        "evaluation_config_digest": config_digest,
    }
    if include_environment_contract:
        contract["environment_fingerprint"] = environment_fingerprint
    obligation = gate.create_obligation(
        subject_kind="CANDIDATE",
        subject_identity=subject_identity,
        requirement="exact-runtime-selected-validation",
        evidence_contract=contract,
        waivability=waivability,
        risk="HIGH",
    )
    assessment = gate.assess_obligation(obligation.obligation_id, evidence_id=rec.evidence_id)
    return rec, obligation, assessment


def _approval(ctx: dict[str, Any], obligation: Any, *, effect: str, suffix: str = "1"):
    return ctx["gate"].create_approval(
        subject_digest=obligation.subject_digest,
        intent_revision_id="r1",
        effect_digest=effect,
        policy_digest=ctx["flow"].policy_digest,
        actor=f"m6c-user-{suffix}",
        authority="USER",
        scope=ctx["flow"].scope,
        expires_at="2099-01-01T00:00:00Z",
    )


def _authorize(ctx: dict[str, Any], rec: Any, obligation: Any, approval: Any, *, effect: str):
    return ctx["authority"].authorize(
        flow_id=ctx["flow"].flow_id,
        obligation_by_capability={"python.pytest": obligation.obligation_id},
        evidence_by_capability={"python.pytest": rec.evidence_id},
        approval_id=approval.approval_id,
        subject={
            "subject_kind": "CANDIDATE",
            "subject_identity": _candidate_subject(ctx["candidate"]),
        },
        intent_revision_id="r1",
        effect_digest=effect,
        scope=ctx["flow"].scope,
        now="2026-08-17T01:00:00Z",
    )


def test_exact_runtime_selected_plan_evidence_and_approval_allow(tmp_path: Path) -> None:
    with _stack(tmp_path) as ctx:
        effect = "sha256:" + "a" * 64
        rec, obligation, assessment = _record(ctx, suffix="pass")
        assert assessment.verdict == "PASS"
        approval = _approval(ctx, obligation, effect=effect)
        decision = _authorize(ctx, rec, obligation, approval, effect=effect)
        assert decision["decision"] == "ALLOW"
        assert decision["allowed"] is True
        assert decision["policy_digest"] == ctx["flow"].policy_digest
        assert decision["plan_digest"] == ctx["flow"].plan_digest
        assert decision["coverage"][0]["covered"] is True


def test_missing_required_capability_evidence_blocks_without_fallback(tmp_path: Path) -> None:
    with _stack(tmp_path) as ctx:
        effect = "sha256:" + "b" * 64
        rec, obligation, _assessment = _record(ctx, suffix="missing")
        approval = _approval(ctx, obligation, effect=effect)
        decision = ctx["authority"].authorize(
            flow_id=ctx["flow"].flow_id,
            obligation_by_capability={"python.pytest": obligation.obligation_id},
            evidence_by_capability={},
            approval_id=approval.approval_id,
            subject={"subject_kind": "CANDIDATE", "subject_identity": _candidate_subject(ctx["candidate"])},
            intent_revision_id="r1",
            effect_digest=effect,
            scope=ctx["flow"].scope,
            now="2026-08-17T01:00:00Z",
        )
        assert decision["decision"] == "BLOCK"
        assert "evidence_capability_set_mismatch" in decision["reasons"]


def test_checker_identity_not_selected_by_plan_cannot_authorize(tmp_path: Path) -> None:
    with _stack(tmp_path) as ctx:
        effect = "sha256:" + "c" * 64
        rec, obligation, _assessment = _record(
            ctx,
            suffix="checker-mismatch",
            checker_id="different-checker",
        )
        approval = _approval(ctx, obligation, effect=effect)
        decision = _authorize(ctx, rec, obligation, approval, effect=effect)
        assert decision["decision"] == "BLOCK"
        assert "evidence_checker_id_mismatch" in decision["coverage"][0]["reasons"]


def test_promotion_grade_flow_requires_exact_environment_contract(tmp_path: Path) -> None:
    with _stack(tmp_path) as ctx:
        effect = "sha256:" + "d" * 64
        rec, obligation, _assessment = _record(
            ctx,
            suffix="env-missing",
            include_environment_contract=False,
        )
        approval = _approval(ctx, obligation, effect=effect)
        decision = _authorize(ctx, rec, obligation, approval, effect=effect)
        assert decision["decision"] == "BLOCK"
        assert "environment_contract_missing" in decision["coverage"][0]["reasons"]


def test_candidate_change_after_pass_blocks_current_authorization(tmp_path: Path) -> None:
    with _stack(tmp_path) as ctx:
        effect = "sha256:" + "e" * 64
        rec, obligation, _assessment = _record(ctx, suffix="stale")
        approval = _approval(ctx, obligation, effect=effect)
        workspace = Path(ctx["candidate_store"].get(ctx["candidate"].candidate_id).workspace_root)
        (workspace / "one.txt").write_bytes(b"foreign\n")
        ctx["candidate_store"].invalidate_if_changed(ctx["candidate"].candidate_id)
        decision = _authorize(ctx, rec, obligation, approval, effect=effect)
        assert decision["decision"] == "BLOCK"
        assert any("candidate" in reason or "applicability" in reason for reason in decision["coverage"][0]["reasons"])


def test_policy_revision_change_makes_old_approval_stale(tmp_path: Path) -> None:
    with _stack(tmp_path) as ctx:
        effect = "sha256:" + "f" * 64
        rec, obligation, _assessment = _record(ctx, suffix="policy")
        approval = _approval(ctx, obligation, effect=effect)
        old_policy = ctx["flow"].policy_digest
        new_flow = ctx["authority"].activate_flow(
            flow_id="git-promotion",
            policy_revision="m6c-test-v2",
            generation="bdb-vnext-g1",
            scope="isolated-vnext-ref",
            required_capabilities=("python.pytest",),
            executable_bindings={"python": sys.executable},
        )
        ctx["flow"] = new_flow
        assert new_flow.policy_digest != old_policy
        decision = _authorize(ctx, rec, obligation, approval, effect=effect)
        assert decision["decision"] == "BLOCK"
        assert "approval_policy_mismatch" in decision["reasons"]


def test_unavailable_required_checker_fails_closed_at_flow_activation(tmp_path: Path) -> None:
    with _stack(tmp_path) as ctx:
        with pytest.raises(M6cError) as caught:
            ctx["authority"].activate_flow(
                flow_id="missing-checker",
                policy_revision="1",
                generation="bdb-vnext-g1",
                scope="isolated-vnext-ref",
                required_capabilities=("python.pytest",),
                executable_bindings={},
            )
        assert caught.value.code == "validation_capability_unavailable"


def test_expired_waiver_cannot_turn_failed_assessment_into_authorization(tmp_path: Path) -> None:
    with _stack(tmp_path) as ctx:
        effect = "sha256:" + "1" * 64
        rec, obligation, assessment = _record(
            ctx,
            suffix="expired-waiver",
            result="FAIL",
            waivability="AUTHORIZED_USER",
        )
        assert assessment.verdict == "FAIL"
        ctx["gate"].create_waiver(
            obligation_id=obligation.obligation_id,
            subject_digest=obligation.subject_digest,
            risk=obligation.risk,
            actor="m6c-user",
            authority="USER",
            rationale="bounded test exception",
            scope=ctx["flow"].scope,
            expires_at="2026-08-17T00:30:00Z",
        )
        approval = _approval(ctx, obligation, effect=effect)
        decision = _authorize(ctx, rec, obligation, approval, effect=effect)
        assert decision["decision"] == "BLOCK"
        assert assessment.status == "UNSATISFIED"


def test_valid_exact_waiver_may_authorize_without_rewriting_failed_assessment(tmp_path: Path) -> None:
    with _stack(tmp_path) as ctx:
        effect = "sha256:" + "2" * 64
        rec, obligation, assessment = _record(
            ctx,
            suffix="valid-waiver",
            result="FAIL",
            waivability="AUTHORIZED_USER",
        )
        ctx["gate"].create_waiver(
            obligation_id=obligation.obligation_id,
            subject_digest=obligation.subject_digest,
            risk=obligation.risk,
            actor="m6c-user",
            authority="USER",
            rationale="exact authorized exception",
            scope=ctx["flow"].scope,
            expires_at="2099-01-01T00:00:00Z",
        )
        approval = _approval(ctx, obligation, effect=effect)
        decision = _authorize(ctx, rec, obligation, approval, effect=effect)
        assert decision["decision"] == "ALLOW"
        assert assessment.status == "UNSATISFIED"
        assert assessment.verdict == "FAIL"


def test_validation_commands_come_only_from_active_deterministic_plan(tmp_path: Path) -> None:
    with _stack(tmp_path) as ctx:
        commands = ctx["authority"].validation_commands("git-promotion")
        assert len(commands) == 1
        assert commands[0].checker_id == "python-pytest"
        assert commands[0].argv[0] == sys.executable
        assert commands[0].argv[1:] == ("-m", "pytest", "-q")


def test_query_and_source_expose_no_legacy_or_model_selected_profile_authority(tmp_path: Path) -> None:
    import bdb_vnext.m6c_validation_authority as module

    with _stack(tmp_path) as ctx:
        query = ctx["authority"].query("git-promotion")
        assert query["mode"] == "ACTIVE_CANONICAL"
        assert query["legacy_selector_authority"] is False
        assert query["model_selected_validation"] is False
        assert query["production_activation"] is False
        assert ctx["authority"]._connection is ctx["gate"]._connection
        source = inspect.getsource(module)
        assert "fixed_test_profiles" not in source
        assert "LegacyFixedProfileAdapter" not in source
        assert "profile_id" not in source
        tables = {
            str(row[0])
            for row in ctx["authority"]._connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "m6c_validation_flow_revisions" in tables
        assert "m6c_validation_flow_heads" in tables
        assert not any("proof" in name.lower() for name in tables if name.startswith("m6c_"))

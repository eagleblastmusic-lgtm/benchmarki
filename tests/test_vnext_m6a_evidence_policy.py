"""Focused tests proving M6a promotion-grade evidence core and semantic policy gate."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from bdb_vnext.candidate import CandidateStore
from bdb_vnext.m3a_submission import ShadowSubmissionRequest
from bdb_vnext.m3c_admission import open_vnext_admission_composition
from bdb_vnext.m4a_work_kernel import WorkKernelStore
from bdb_vnext.m4c_evidence import EvidenceStore, MinimumCandidateChecker
from bdb_vnext.m6a_evidence_policy import (
    APPROVAL_SCHEMA,
    ASSESSMENT_SCHEMA,
    EvidencePolicyGate,
    M6aError,
    OBLIGATION_QUERY_SCHEMA,
    OBLIGATION_SCHEMA,
    WAIVER_DECISION_SCHEMA,
    compute_subject_digest,
)
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
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "M6a Test")
    _git(repo, "config", "user.email", "m6a@example.invalid")
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
    receipt = admission.authority.admit(
        ShadowSubmissionRequest(
            submission_key="m6a:submission",
            intent_revision="r1",
            intent={"operation": "candidate-check"},
            conversation_binding={"conversation_id": "m6a"},
            consumer_binding={"consumer_id": "m6a", "kind": "browser"},
        )
    )
    kernel = WorkKernelStore.open(runtime, task_authority=admission.authority, legacy_root=legacy, clock=lambda: 100.0)
    candidate_store = CandidateStore(runtime, work_kernel=kernel)
    evidence = EvidenceStore(runtime, content_store=candidate_store.content_store, candidate_store=candidate_store)
    gate = EvidencePolicyGate(evidence)
    view = RepositoryResource.from_path(subject, repository_id="m6a-subject").resolve_committed("HEAD")
    work = kernel.create_work_item("work:m6a", receipt.task_id)
    lease = kernel.acquire_lease(work.work_id, "lease:m6a", "worker:m6a")
    workspace = candidate_store.create_workspace(candidate_id="candidate:m6a", base_view=view)
    prepared = candidate_store.prepare(
        candidate_id="candidate:m6a",
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
    try:
        yield runtime, subject, candidate_store, evidence, gate, candidate, view
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


def _candidate_subject_identity(candidate) -> dict[str, Any]:
    return {
        "candidate_id": candidate.candidate_id,
        "view_id": candidate.view_id,
        "manifest_digest": candidate.manifest_digest,
        "candidate_tree_digest": candidate.candidate_tree_digest,
        "base_view_id": candidate.base_view_id,
        "repository_id": candidate.repository_id,
    }


def test_1_exact_subject_and_checker_and_env_with_pass_evidence_yields_satisfied_pass(tmp_path: Path) -> None:
    """1. exact subject + exact checker + exact environment + current Candidate + PASS Evidence = SATISFIED / APPLICABLE / PASS."""
    with _stack(tmp_path) as (_runtime, _subject, _candidate_store, evidence, gate, candidate, _view):
        checker = MinimumCandidateChecker(Path(__file__).parents[1], evidence)
        evaluation = checker.check(candidate, request_id="m6a:check:1")
        evidence_rec = evidence.get(evaluation.evidence_id)
        assert evidence_rec is not None

        subject_identity = _candidate_subject_identity(candidate)
        obligation = gate.create_obligation(
            subject_kind="CANDIDATE",
            subject_identity=subject_identity,
            requirement="lint-and-typecheck",
            evidence_contract={
                "evidence_type": "unit-test",
                "coverage": "full",
                "freshness": "CURRENT",
                "checker_id": evidence_rec.checker_id,
                "checker_version": evidence_rec.checker_version,
                "checker_code_digest": evidence_rec.checker_code_digest,
            },
            waivability="AUTHORIZED_USER",
            risk="LOW",
        )

        assessment = gate.assess_obligation(obligation.obligation_id, evidence_id=evaluation.evidence_id)
        assert assessment.status == "SATISFIED"
        assert assessment.applicability == "APPLICABLE"
        assert assessment.verdict == "PASS"


def test_2_different_exact_subject_yields_stale_unknown(tmp_path: Path) -> None:
    """2. different exact subject = STALE / UNKNOWN."""
    with _stack(tmp_path) as (_runtime, _subject, _candidate_store, evidence, gate, candidate, _view):
        checker = MinimumCandidateChecker(Path(__file__).parents[1], evidence)
        evaluation = checker.check(candidate, request_id="m6a:check:2")

        # Obligation for a different subject
        obligation = gate.create_obligation(
            subject_kind="CANDIDATE",
            subject_identity={"candidate_id": "other-candidate", "view_id": "sha256:" + "0" * 64},
            requirement="lint-check",
            evidence_contract={"evidence_type": "unit-test", "coverage": "full", "freshness": "CURRENT"},
            waivability="NEVER",
            risk="HIGH",
        )

        assessment = gate.assess_obligation(obligation.obligation_id, evidence_id=evaluation.evidence_id)
        assert assessment.status == "STALE"
        assert assessment.applicability == "UNKNOWN"
        assert assessment.verdict == "UNKNOWN"


def test_3_different_checker_code_or_version_yields_stale_unknown(tmp_path: Path) -> None:
    """3. different checker code/version when contract requires it = STALE / UNKNOWN."""
    with _stack(tmp_path) as (_runtime, _subject, _candidate_store, evidence, gate, candidate, _view):
        checker = MinimumCandidateChecker(Path(__file__).parents[1], evidence)
        evaluation = checker.check(candidate, request_id="m6a:check:3")

        obligation = gate.create_obligation(
            subject_kind="CANDIDATE",
            subject_identity=_candidate_subject_identity(candidate),
            requirement="strict-checker-check",
            evidence_contract={
                "evidence_type": "unit-test",
                "coverage": "full",
                "freshness": "CURRENT",
                "checker_version": "99.0",  # Different version
                "checker_code_digest": "sha256:" + "f" * 64,  # Different code digest
            },
            waivability="NEVER",
            risk="HIGH",
        )

        assessment = gate.assess_obligation(obligation.obligation_id, evidence_id=evaluation.evidence_id)
        assert assessment.status == "STALE"
        assert assessment.applicability == "UNKNOWN"
        assert assessment.verdict == "UNKNOWN"


def test_4_different_environment_fingerprint_yields_stale_unknown(tmp_path: Path) -> None:
    """4. different environment fingerprint = STALE / UNKNOWN."""
    with _stack(tmp_path) as (_runtime, _subject, _candidate_store, evidence, gate, candidate, _view):
        checker = MinimumCandidateChecker(Path(__file__).parents[1], evidence)
        evaluation = checker.check(candidate, request_id="m6a:check:4")

        obligation = gate.create_obligation(
            subject_kind="CANDIDATE",
            subject_identity=_candidate_subject_identity(candidate),
            requirement="env-check",
            evidence_contract={
                "evidence_type": "unit-test",
                "coverage": "full",
                "freshness": "CURRENT",
                "environment_fingerprint": "sha256:" + "e" * 64,  # Different env fingerprint
            },
            waivability="NEVER",
            risk="HIGH",
        )

        assessment = gate.assess_obligation(obligation.obligation_id, evidence_id=evaluation.evidence_id)
        assert assessment.status == "STALE"
        assert assessment.applicability == "UNKNOWN"
        assert assessment.verdict == "UNKNOWN"


def test_5_different_evaluation_config_digest_yields_stale_unknown(tmp_path: Path) -> None:
    """5. different evaluation config digest = STALE / UNKNOWN."""
    with _stack(tmp_path) as (_runtime, _subject, _candidate_store, evidence, gate, candidate, _view):
        checker = MinimumCandidateChecker(Path(__file__).parents[1], evidence)
        evaluation = checker.check(candidate, request_id="m6a:check:5")

        obligation = gate.create_obligation(
            subject_kind="CANDIDATE",
            subject_identity=_candidate_subject_identity(candidate),
            requirement="config-check",
            evidence_contract={
                "evidence_type": "unit-test",
                "coverage": "full",
                "freshness": "CURRENT",
                "evaluation_config_digest": "sha256:" + "c" * 64,  # Different config digest
            },
            waivability="NEVER",
            risk="HIGH",
        )

        assessment = gate.assess_obligation(obligation.obligation_id, evidence_id=evaluation.evidence_id)
        assert assessment.status == "STALE"
        assert assessment.applicability == "UNKNOWN"
        assert assessment.verdict == "UNKNOWN"


def test_6_explicit_not_applicable_yields_satisfied_and_not_applicable_verdict(tmp_path: Path) -> None:
    """6. explicit NOT_APPLICABLE = Assessment SATISFIED, applicability NOT_APPLICABLE, verdict NOT_APPLICABLE, WAIVED is not status."""
    with _stack(tmp_path) as (_runtime, _subject, _candidate_store, _evidence, gate, candidate, _view):
        obligation = gate.create_obligation(
            subject_kind="CANDIDATE",
            subject_identity=_candidate_subject_identity(candidate),
            requirement="optional-check",
            evidence_contract={"evidence_type": "perf", "coverage": "partial", "freshness": "CURRENT"},
            waivability="NEVER",
            risk="LOW",
        )

        assessment = gate.assess_obligation(
            obligation.obligation_id,
            not_applicable=True,
            not_applicable_reason="platform_not_supported",
        )
        assert assessment.status == "SATISFIED"
        assert assessment.applicability == "NOT_APPLICABLE"
        assert assessment.verdict == "NOT_APPLICABLE"
        assert assessment.status != "WAIVED"


def test_7_tampering_or_invalidating_candidate_yields_stale_unknown(tmp_path: Path) -> None:
    """7. invalidate/tamper sealed Candidate after prior PASS = STALE / UNKNOWN."""
    with _stack(tmp_path) as (_runtime, _subject, candidate_store, evidence, gate, candidate, _view):
        checker = MinimumCandidateChecker(Path(__file__).parents[1], evidence)
        evaluation = checker.check(candidate, request_id="m6a:check:7")

        obligation = gate.create_obligation(
            subject_kind="CANDIDATE",
            subject_identity=_candidate_subject_identity(candidate),
            requirement="tamper-check",
            evidence_contract={"evidence_type": "unit-test", "coverage": "full", "freshness": "CURRENT"},
            waivability="NEVER",
            risk="HIGH",
        )

        # Tamper workspace and invalidate candidate
        workspace = Path(candidate_store.get(candidate.candidate_id).workspace_root)  # type: ignore[union-attr]
        (workspace / "one.txt").write_bytes(b"tampered\n")
        candidate_store.invalidate_if_changed(candidate.candidate_id)

        assessment = gate.assess_obligation(obligation.obligation_id, evidence_id=evaluation.evidence_id)
        assert assessment.status == "STALE"
        assert assessment.applicability == "UNKNOWN"
        assert assessment.verdict == "UNKNOWN"


def test_8_m4c_fail_disposition_yields_unsatisfied_applicable_fail(tmp_path: Path) -> None:
    """8. create a current M4c FAIL disposition = UNSATISFIED / APPLICABLE / FAIL."""
    with _stack(tmp_path) as (_runtime, _subject, _candidate_store, evidence, gate, candidate, _view):
        subject_identity = _candidate_subject_identity(candidate)
        rec = evidence.record_observation(
            request_id="m6a:check:8",
            primary_subject_kind="CANDIDATE",
            primary_subject_identity=subject_identity,
            candidate_view_id=candidate.view_id,
            raw_observation={"schema": "m4c-raw-observation-v1", "result": "failed_tests"},
            checker_id="checker:fail",
            checker_version="1",
            checker_code_digest="sha256:" + "1" * 64,
            environment={"fingerprint": "env1"},
            observation_started_at="2026-08-17T00:00:00Z",
            observation_finished_at="2026-08-17T00:00:01Z",
            completeness="COMPLETE",
            applicability="APPLICABLE",
            status="FAILED",
        )
        evidence.evaluate(
            evidence_id=rec.evidence_id,
            evaluator_id="evaluator:fail",
            evaluator_version="1",
            evaluator_code_digest="sha256:" + "1" * 64,
            config_digest="sha256:" + "2" * 64,
            result="FAIL",
            applicability="APPLICABLE",
            detail={"failed": True},
        )

        obligation = gate.create_obligation(
            subject_kind="CANDIDATE",
            subject_identity=subject_identity,
            requirement="pass-tests",
            evidence_contract={"evidence_type": "unit-test", "coverage": "full", "freshness": "CURRENT"},
            waivability="AUTHORIZED_USER",
            risk="MEDIUM",
        )

        assessment = gate.assess_obligation(obligation.obligation_id, evidence_id=rec.evidence_id)
        assert assessment.status == "UNSATISFIED"
        assert assessment.applicability == "APPLICABLE"
        assert assessment.verdict == "FAIL"


def test_9_authorized_user_waiver_does_not_change_unsatisfied_status_but_allows_gate(tmp_path: Path) -> None:
    """9. create valid AUTHORIZED_USER waiver for that FAIL: Assessment MUST remain UNSATISFIED, gate reports allowed_by_waiver = true."""
    with _stack(tmp_path) as (_runtime, _subject, _candidate_store, evidence, gate, candidate, _view):
        subject_identity = _candidate_subject_identity(candidate)
        rec = evidence.record_observation(
            request_id="m6a:check:9",
            primary_subject_kind="CANDIDATE",
            primary_subject_identity=subject_identity,
            candidate_view_id=candidate.view_id,
            raw_observation={"schema": "m4c-raw-observation-v1", "result": "failed"},
            checker_id="checker:fail",
            checker_version="1",
            checker_code_digest="sha256:" + "1" * 64,
            environment={},
            observation_started_at="2026-08-17T00:00:00Z",
            observation_finished_at="2026-08-17T00:00:01Z",
            completeness="COMPLETE",
            applicability="APPLICABLE",
            status="FAILED",
        )
        evidence.evaluate(
            evidence_id=rec.evidence_id,
            evaluator_id="evaluator:fail",
            evaluator_version="1",
            evaluator_code_digest="sha256:" + "1" * 64,
            config_digest="sha256:" + "2" * 64,
            result="FAIL",
            applicability="APPLICABLE",
            detail={},
        )

        obligation = gate.create_obligation(
            subject_kind="CANDIDATE",
            subject_identity=subject_identity,
            requirement="pass-tests",
            evidence_contract={"evidence_type": "unit-test", "coverage": "full", "freshness": "CURRENT"},
            waivability="AUTHORIZED_USER",
            risk="MEDIUM",
        )

        assessment = gate.assess_obligation(obligation.obligation_id, evidence_id=rec.evidence_id)
        assert assessment.status == "UNSATISFIED"
        assert assessment.verdict == "FAIL"

        # Create waiver
        waiver = gate.create_waiver(
            obligation_id=obligation.obligation_id,
            subject_digest=obligation.subject_digest,
            risk="MEDIUM",
            actor="dev_user",
            authority="USER",
            rationale="known issue in staging",
            scope="promote:staging",
            expires_at="2099-01-01T00:00:00Z",
        )

        # Assessment is unchanged!
        latest = gate.get_latest_assessment(obligation.obligation_id)
        assert latest is not None
        assert latest.status == "UNSATISFIED"
        assert latest.verdict == "FAIL"

        # Create valid approval
        approval = gate.create_approval(
            subject_digest=obligation.subject_digest,
            intent_revision_id="r1",
            effect_digest="sha256:" + "a" * 64,
            policy_digest="sha256:" + "b" * 64,
            actor="approver",
            authority="LEAD",
            scope="promote:staging",
            expires_at="2099-01-01T00:00:00Z",
        )

        # Promotion gate allows via waiver
        gate_res = gate.promotion_gate(
            obligation_ids=[obligation.obligation_id],
            approval_id=approval.approval_id,
            subject={"subject_kind": "CANDIDATE", "subject_identity": subject_identity},
            intent_revision_id="r1",
            effect_digest="sha256:" + "a" * 64,
            policy_digest="sha256:" + "b" * 64,
            scope="promote:staging",
            now="2026-08-17T00:00:00Z",
        )

        assert gate_res["allowed"] is True
        assert gate_res["decision"] == "ALLOW"
        assert gate_res["obligation_results"][0]["allowed_by_waiver"] is True
        assert gate_res["obligation_results"][0]["waiver_id"] == waiver.waiver_id


def test_10_never_obligation_rejects_all_waivers(tmp_path: Path) -> None:
    """10. NEVER obligation rejects all waivers."""
    with _stack(tmp_path) as (_runtime, _subject, _candidate_store, _evidence, gate, candidate, _view):
        subject_identity = _candidate_subject_identity(candidate)
        obligation = gate.create_obligation(
            subject_kind="CANDIDATE",
            subject_identity=subject_identity,
            requirement="critical-security",
            evidence_contract={"evidence_type": "sec", "coverage": "full", "freshness": "CURRENT"},
            waivability="NEVER",
            risk="CRITICAL",
        )

        gate.assess_obligation(obligation.obligation_id, evidence_id=None)  # Status UNKNOWN

        # Even with an admin waiver created
        gate.create_waiver(
            obligation_id=obligation.obligation_id,
            subject_digest=obligation.subject_digest,
            risk="CRITICAL",
            actor="admin",
            authority="ADMIN",
            rationale="attempt waiver",
            scope="prod",
            expires_at="2099-01-01T00:00:00Z",
        )

        approval = gate.create_approval(
            subject_digest=obligation.subject_digest,
            intent_revision_id="r1",
            effect_digest="sha256:" + "a" * 64,
            policy_digest="sha256:" + "b" * 64,
            actor="approver",
            authority="LEAD",
            scope="prod",
            expires_at="2099-01-01T00:00:00Z",
        )

        gate_res = gate.promotion_gate(
            obligation_ids=[obligation.obligation_id],
            approval_id=approval.approval_id,
            subject={"subject_kind": "CANDIDATE", "subject_identity": subject_identity},
            intent_revision_id="r1",
            effect_digest="sha256:" + "a" * 64,
            policy_digest="sha256:" + "b" * 64,
            scope="prod",
            now="2026-08-17T00:00:00Z",
        )

        assert gate_res["allowed"] is False
        assert gate_res["decision"] == "BLOCK"
        assert gate_res["obligation_results"][0]["allowed_by_waiver"] is False


def test_11_admin_only_rejects_user_waiver_and_accepts_admin(tmp_path: Path) -> None:
    """11. ADMIN_ONLY rejects USER waiver and accepts ADMIN."""
    with _stack(tmp_path) as (_runtime, _subject, _candidate_store, _evidence, gate, candidate, _view):
        subject_identity = _candidate_subject_identity(candidate)
        obligation = gate.create_obligation(
            subject_kind="CANDIDATE",
            subject_identity=subject_identity,
            requirement="admin-level-check",
            evidence_contract={"evidence_type": "audit", "coverage": "full", "freshness": "CURRENT"},
            waivability="ADMIN_ONLY",
            risk="HIGH",
        )

        gate.assess_obligation(obligation.obligation_id, evidence_id=None)

        approval = gate.create_approval(
            subject_digest=obligation.subject_digest,
            intent_revision_id="r1",
            effect_digest="sha256:" + "a" * 64,
            policy_digest="sha256:" + "b" * 64,
            actor="approver",
            authority="LEAD",
            scope="prod",
            expires_at="2099-01-01T00:00:00Z",
        )

        # 1. USER waiver fails
        gate.create_waiver(
            obligation_id=obligation.obligation_id,
            subject_digest=obligation.subject_digest,
            risk="HIGH",
            actor="normal_user",
            authority="USER",
            rationale="user waiver",
            scope="prod",
            expires_at="2099-01-01T00:00:00Z",
        )

        blocked = gate.promotion_gate(
            obligation_ids=[obligation.obligation_id],
            approval_id=approval.approval_id,
            subject={"subject_kind": "CANDIDATE", "subject_identity": subject_identity},
            intent_revision_id="r1",
            effect_digest="sha256:" + "a" * 64,
            policy_digest="sha256:" + "b" * 64,
            scope="prod",
            now="2026-08-17T00:00:00Z",
        )
        assert blocked["allowed"] is False

        # 2. ADMIN waiver passes
        admin_w = gate.create_waiver(
            obligation_id=obligation.obligation_id,
            subject_digest=obligation.subject_digest,
            risk="HIGH",
            actor="super_admin",
            authority="ADMIN",
            rationale="admin waiver",
            scope="prod",
            expires_at="2099-01-01T00:00:00Z",
        )

        allowed = gate.promotion_gate(
            obligation_ids=[obligation.obligation_id],
            approval_id=approval.approval_id,
            subject={"subject_kind": "CANDIDATE", "subject_identity": subject_identity},
            intent_revision_id="r1",
            effect_digest="sha256:" + "a" * 64,
            policy_digest="sha256:" + "b" * 64,
            scope="prod",
            now="2026-08-17T00:00:00Z",
        )
        assert allowed["allowed"] is True
        assert allowed["obligation_results"][0]["allowed_by_waiver"] is True
        assert allowed["obligation_results"][0]["waiver_id"] == admin_w.waiver_id


def test_12_expired_waiver_does_not_authorize(tmp_path: Path) -> None:
    """12. expired waiver does not authorize."""
    with _stack(tmp_path) as (_runtime, _subject, _candidate_store, _evidence, gate, candidate, _view):
        subject_identity = _candidate_subject_identity(candidate)
        obligation = gate.create_obligation(
            subject_kind="CANDIDATE",
            subject_identity=subject_identity,
            requirement="some-check",
            evidence_contract={"evidence_type": "unit", "coverage": "full", "freshness": "CURRENT"},
            waivability="AUTHORIZED_USER",
            risk="LOW",
        )
        gate.assess_obligation(obligation.obligation_id, evidence_id=None)

        # Expired waiver (expired at 2026-01-01)
        gate.create_waiver(
            obligation_id=obligation.obligation_id,
            subject_digest=obligation.subject_digest,
            risk="LOW",
            actor="user1",
            authority="USER",
            rationale="expired waiver",
            scope="prod",
            expires_at="2026-01-01T00:00:00Z",
        )

        approval = gate.create_approval(
            subject_digest=obligation.subject_digest,
            intent_revision_id="r1",
            effect_digest="sha256:" + "a" * 64,
            policy_digest="sha256:" + "b" * 64,
            actor="approver",
            authority="LEAD",
            scope="prod",
            expires_at="2099-01-01T00:00:00Z",
        )

        res = gate.promotion_gate(
            obligation_ids=[obligation.obligation_id],
            approval_id=approval.approval_id,
            subject={"subject_kind": "CANDIDATE", "subject_identity": subject_identity},
            intent_revision_id="r1",
            effect_digest="sha256:" + "a" * 64,
            policy_digest="sha256:" + "b" * 64,
            scope="prod",
            now="2026-08-17T00:00:00Z",
        )
        assert res["allowed"] is False
        assert res["decision"] == "BLOCK"


def test_13_exact_approval_allows_only_exact_fields(tmp_path: Path) -> None:
    """13. exact Approval allows only exact: subject, intent, effect digest, policy digest, scope."""
    with _stack(tmp_path) as (_runtime, _subject, _candidate_store, evidence, gate, candidate, _view):
        checker = MinimumCandidateChecker(Path(__file__).parents[1], evidence)
        evaluation = checker.check(candidate, request_id="m6a:check:13")
        subject_identity = _candidate_subject_identity(candidate)

        obligation = gate.create_obligation(
            subject_kind="CANDIDATE",
            subject_identity=subject_identity,
            requirement="check",
            evidence_contract={"evidence_type": "unit-test", "coverage": "full", "freshness": "CURRENT"},
            waivability="NEVER",
            risk="LOW",
        )
        gate.assess_obligation(obligation.obligation_id, evidence_id=evaluation.evidence_id)

        approval = gate.create_approval(
            subject_digest=obligation.subject_digest,
            intent_revision_id="r1",
            effect_digest="sha256:" + "a" * 64,
            policy_digest="sha256:" + "b" * 64,
            actor="lead",
            authority="LEAD",
            scope="promote:prod",
            expires_at="2099-01-01T00:00:00Z",
        )

        res = gate.promotion_gate(
            obligation_ids=[obligation.obligation_id],
            approval_id=approval.approval_id,
            subject={"subject_kind": "CANDIDATE", "subject_identity": subject_identity},
            intent_revision_id="r1",
            effect_digest="sha256:" + "a" * 64,
            policy_digest="sha256:" + "b" * 64,
            scope="promote:prod",
            now="2026-08-17T00:00:00Z",
        )
        assert res["allowed"] is True
        assert res["decision"] == "ALLOW"


def test_14_changed_effect_digest_blocks(tmp_path: Path) -> None:
    """14. changed effect digest blocks."""
    with _stack(tmp_path) as (_runtime, _subject, _candidate_store, evidence, gate, candidate, _view):
        checker = MinimumCandidateChecker(Path(__file__).parents[1], evidence)
        evaluation = checker.check(candidate, request_id="m6a:check:14")
        subject_identity = _candidate_subject_identity(candidate)

        obligation = gate.create_obligation(
            subject_kind="CANDIDATE",
            subject_identity=subject_identity,
            requirement="check",
            evidence_contract={"evidence_type": "unit-test", "coverage": "full", "freshness": "CURRENT"},
            waivability="NEVER",
            risk="LOW",
        )
        gate.assess_obligation(obligation.obligation_id, evidence_id=evaluation.evidence_id)

        approval = gate.create_approval(
            subject_digest=obligation.subject_digest,
            intent_revision_id="r1",
            effect_digest="sha256:" + "a" * 64,
            policy_digest="sha256:" + "b" * 64,
            actor="lead",
            authority="LEAD",
            scope="promote:prod",
            expires_at="2099-01-01T00:00:00Z",
        )

        # Gate requested with different effect digest
        res = gate.promotion_gate(
            obligation_ids=[obligation.obligation_id],
            approval_id=approval.approval_id,
            subject={"subject_kind": "CANDIDATE", "subject_identity": subject_identity},
            intent_revision_id="r1",
            effect_digest="sha256:" + "9" * 64,  # Changed!
            policy_digest="sha256:" + "b" * 64,
            scope="promote:prod",
            now="2026-08-17T00:00:00Z",
        )
        assert res["allowed"] is False
        assert "approval_effect_mismatch" in res["reasons"]


def test_15_changed_policy_digest_blocks(tmp_path: Path) -> None:
    """15. changed policy digest blocks."""
    with _stack(tmp_path) as (_runtime, _subject, _candidate_store, evidence, gate, candidate, _view):
        checker = MinimumCandidateChecker(Path(__file__).parents[1], evidence)
        evaluation = checker.check(candidate, request_id="m6a:check:15")
        subject_identity = _candidate_subject_identity(candidate)

        obligation = gate.create_obligation(
            subject_kind="CANDIDATE",
            subject_identity=subject_identity,
            requirement="check",
            evidence_contract={"evidence_type": "unit-test", "coverage": "full", "freshness": "CURRENT"},
            waivability="NEVER",
            risk="LOW",
        )
        gate.assess_obligation(obligation.obligation_id, evidence_id=evaluation.evidence_id)

        approval = gate.create_approval(
            subject_digest=obligation.subject_digest,
            intent_revision_id="r1",
            effect_digest="sha256:" + "a" * 64,
            policy_digest="sha256:" + "b" * 64,
            actor="lead",
            authority="LEAD",
            scope="promote:prod",
            expires_at="2099-01-01T00:00:00Z",
        )

        # Gate requested with different policy digest
        res = gate.promotion_gate(
            obligation_ids=[obligation.obligation_id],
            approval_id=approval.approval_id,
            subject={"subject_kind": "CANDIDATE", "subject_identity": subject_identity},
            intent_revision_id="r1",
            effect_digest="sha256:" + "a" * 64,
            policy_digest="sha256:" + "8" * 64,  # Changed!
            scope="promote:prod",
            now="2026-08-17T00:00:00Z",
        )
        assert res["allowed"] is False
        assert "approval_policy_mismatch" in res["reasons"]


def test_16_expired_approval_blocks(tmp_path: Path) -> None:
    """16. expired approval blocks."""
    with _stack(tmp_path) as (_runtime, _subject, _candidate_store, evidence, gate, candidate, _view):
        checker = MinimumCandidateChecker(Path(__file__).parents[1], evidence)
        evaluation = checker.check(candidate, request_id="m6a:check:16")
        subject_identity = _candidate_subject_identity(candidate)

        obligation = gate.create_obligation(
            subject_kind="CANDIDATE",
            subject_identity=subject_identity,
            requirement="check",
            evidence_contract={"evidence_type": "unit-test", "coverage": "full", "freshness": "CURRENT"},
            waivability="NEVER",
            risk="LOW",
        )
        gate.assess_obligation(obligation.obligation_id, evidence_id=evaluation.evidence_id)

        # Expired approval
        approval = gate.create_approval(
            subject_digest=obligation.subject_digest,
            intent_revision_id="r1",
            effect_digest="sha256:" + "a" * 64,
            policy_digest="sha256:" + "b" * 64,
            actor="lead",
            authority="LEAD",
            scope="promote:prod",
            expires_at="2026-01-01T00:00:00Z",
        )

        res = gate.promotion_gate(
            obligation_ids=[obligation.obligation_id],
            approval_id=approval.approval_id,
            subject={"subject_kind": "CANDIDATE", "subject_identity": subject_identity},
            intent_revision_id="r1",
            effect_digest="sha256:" + "a" * 64,
            policy_digest="sha256:" + "b" * 64,
            scope="promote:prod",
            now="2026-08-17T00:00:00Z",
        )
        assert res["allowed"] is False
        assert "approval_expired" in res["reasons"]


def test_17_query_exposes_obligation_current_assessment_and_waiver_history(tmp_path: Path) -> None:
    """17. query() exposes obligation/current assessment/waiver history."""
    with _stack(tmp_path) as (_runtime, _subject, _candidate_store, evidence, gate, candidate, _view):
        checker = MinimumCandidateChecker(Path(__file__).parents[1], evidence)
        evaluation = checker.check(candidate, request_id="m6a:check:17")
        subject_identity = _candidate_subject_identity(candidate)

        obligation = gate.create_obligation(
            subject_kind="CANDIDATE",
            subject_identity=subject_identity,
            requirement="query-test",
            evidence_contract={"evidence_type": "unit-test", "coverage": "full", "freshness": "CURRENT"},
            waivability="AUTHORIZED_USER",
            risk="LOW",
        )

        gate.assess_obligation(obligation.obligation_id, evidence_id=evaluation.evidence_id)

        w1 = gate.create_waiver(
            obligation_id=obligation.obligation_id,
            subject_digest=obligation.subject_digest,
            risk="LOW",
            actor="user1",
            authority="USER",
            rationale="first waiver",
            scope="scope1",
            expires_at="2099-01-01T00:00:00Z",
        )

        q = gate.query(obligation.obligation_id)
        assert q["schema"] == OBLIGATION_QUERY_SCHEMA
        assert q["obligation"]["obligation_id"] == obligation.obligation_id
        assert q["current_assessment"]["verdict"] == "PASS"
        assert len(q["waiver_history"]) == 1
        assert q["waiver_history"][0]["waiver_id"] == w1.waiver_id
        assert q["query_digest"].startswith("sha256:")


def test_18_all_four_json_schemas_parse_and_use_additional_properties_false() -> None:
    """18. all four JSON schemas parse and use: additionalProperties = false."""
    schemas_dir = Path(__file__).parents[1] / "schemas"
    schema_names = [
        "bdb-vnext-m6a-obligation-v1.schema.json",
        "bdb-vnext-m6a-assessment-v1.schema.json",
        "bdb-vnext-m6a-waiver-decision-v1.schema.json",
        "bdb-vnext-m6a-approval-v1.schema.json",
    ]

    for name in schema_names:
        schema_path = schemas_dir / name
        assert schema_path.exists(), f"Missing schema file: {name}"
        data = json.loads(schema_path.read_text(encoding="utf-8"))
        assert data.get("additionalProperties") is False, f"Schema {name} must have additionalProperties: false"

    # Assessment schema must NOT allow WAIVED
    assessment_schema = json.loads((schemas_dir / "bdb-vnext-m6a-assessment-v1.schema.json").read_text(encoding="utf-8"))
    status_enums = assessment_schema["properties"]["status"]["enum"]
    assert "WAIVED" not in status_enums


def test_19_m6a_uses_exactly_the_same_sqlite_connection_as_m4c_evidence_store(tmp_path: Path) -> None:
    """19. M6a uses exactly the same SQLite connection as M4c EvidenceStore."""
    with _stack(tmp_path) as (_runtime, _subject, _candidate_store, evidence, gate, _candidate, _view):
        assert gate._connection is evidence._connection


def test_20_no_m6a_table_name_contains_proof(tmp_path: Path) -> None:
    """20. No M6a table name contains 'proof'."""
    with _stack(tmp_path) as (_runtime, _subject, _candidate_store, evidence, _gate, _candidate, _view):
        tables = [
            row[0]
            for row in evidence._connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        ]
        m6a_tables = [t for t in tables if t.startswith("m6a_")]
        assert len(m6a_tables) >= 4
        for table in m6a_tables:
            assert "proof" not in table.lower()

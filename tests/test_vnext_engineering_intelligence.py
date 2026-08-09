from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from bdb_shared.evidence import semantic_digest
from bdb_vnext.content_store import DurableBindingStore, ImmutableContentStore
from bdb_vnext.engineering_intelligence import (
    ClaimContradiction,
    ContextPackage,
    ContextRequest,
    ContextResolution,
    DecisionOption,
    EngineeringDecision,
    EngineeringIntelligenceError,
    IntentBasis,
    Omission,
    RepositoryUnderstandingView,
    UnderstandingClaim,
    Unknown,
    publish_semantic_record,
    reconstruct_semantic_record,
    transport_semantic_record,
    validate_decision_applicability,
)
from bdb_vnext.repo_view import RepositoryResource


ROOT = Path(__file__).resolve().parents[1]


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _fixture(tmp_path: Path) -> tuple[Path, object, object, IntentBasis]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.name", "M2c Test")
    _git(repo, "config", "user.email", "m2c@example.invalid")
    (repo / "README.md").write_text("M2c fixture\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "service.py").write_text("OWNER = 'A'\n", encoding="utf-8")
    _git(repo, "add", "--all")
    _git(repo, "commit", "-qm", "M2c fixture")
    resource = RepositoryResource.from_path(repo, repository_id="m2c-fixture")
    view = resource.resolve_committed("refs/heads/main", observed_at="2026-08-10T00:00:00Z")
    intent = IntentBasis(
        "external-task:m2c-1",
        "r1",
        semantic_digest({"intent": "understand ownership and recovery", "constraints": ["read-only"]}),
    )
    return repo, resource, view, intent


def _source_ref(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


def _seeded(tmp_path: Path) -> tuple[Path, object, IntentBasis, RepositoryUnderstandingView, ContextPackage, Unknown]:
    repo, resource, view, intent = _fixture(tmp_path)
    ownership = UnderstandingClaim.create(
        view,
        subject="src/service.py",
        dimension="ownership",
        kind="FACT",
        statement="component ownership is explicit in the committed source",
        evidence_refs=[_source_ref("src/service.py")],
    )
    recovery_gap = Unknown.create(
        view,
        subject="repository recovery boundary",
        dimension="recovery",
        reason="no exact recovery evidence was requested in this initial package",
    )
    understanding = RepositoryUnderstandingView.create(
        intent,
        view,
        claims=[ownership],
        requested_dimensions=["ownership", "recovery"],
        covered_dimensions=["ownership"],
        must_see_categories=["recovery"],
        covered_must_see=[],
        unknowns=[recovery_gap],
    )
    package = ContextPackage.from_understanding(
        understanding,
        horizon="COMPONENT",
        included_fragment_ids=[ownership.evidence_refs[0]],
    )
    return repo, resource, intent, understanding, package, recovery_gap


def test_understanding_claims_separate_fact_inference_and_require_exact_basis(tmp_path: Path) -> None:
    _repo, _resource, _intent, _understanding, _package, _gap = _seeded(tmp_path)
    with pytest.raises(EngineeringIntelligenceError) as failure:
        UnderstandingClaim(
            "sha256:" + "0" * 64,
            _understanding.repo_view,
            "src/service.py",
            "ownership",
            "FACT",
            "DERIVED",
            "source-looking claim",
            (_source_ref("bad"),),
            (),
        )
    assert failure.value.code == "claim_authority_mismatch"


def test_conflicting_source_and_inference_remain_visible_and_source_wins(tmp_path: Path) -> None:
    _repo, _resource, _intent, seeded, _package, _gap = _seeded(tmp_path)
    source = seeded.claims[0]
    inference = UnderstandingClaim.create(
        seeded.repo_view,
        subject=source.subject,
        dimension=source.dimension,
        kind="INFERENCE",
        statement="component ownership is inferred as B",
        evidence_refs=[source.claim_id],
        basis_refs=[source.claim_id],
    )
    contradiction = ClaimContradiction.create(
        seeded.repo_view,
        subject=source.subject,
        dimension=source.dimension,
        claim_ids=[source.claim_id, inference.claim_id],
        source_claim_ids=[source.claim_id],
        derived_claim_ids=[inference.claim_id],
        reason="derived ownership conflicts with exact source claim",
    )
    combined = RepositoryUnderstandingView.create(
        seeded.intent_basis,
        seeded.repo_view,
        claims=[source, inference],
        requested_dimensions=["ownership"],
        covered_dimensions=["ownership"],
        contradictions=[contradiction],
    )
    assert combined.contradictions[0].source_authority == "EXACT_SOURCE_WINS"
    assert combined.coverage_status == "PARTIAL"

    with pytest.raises(EngineeringIntelligenceError) as missing_contradiction:
        RepositoryUnderstandingView.create(
            seeded.intent_basis,
            seeded.repo_view,
            claims=[source, inference],
            requested_dimensions=["ownership"],
            covered_dimensions=["ownership"],
        )
    assert missing_contradiction.value.code == "contradiction_required"


def test_coverage_status_cannot_overclaim_uncovered_must_see(tmp_path: Path) -> None:
    _repo, _resource, _intent, understanding, _package, _gap = _seeded(tmp_path)
    assert understanding.coverage_status == "BLOCKED"
    assert understanding.gap_ids == (_gap.unknown_id,)
    with pytest.raises(EngineeringIntelligenceError) as overclaim:
        ContextPackage.create(
            understanding.intent_basis,
            understanding.repo_view,
            understanding_id=understanding.understanding_id,
            horizon="COMPONENT",
            requested_dimensions=["ownership"],
            covered_dimensions=["recovery"],
        )
    assert overclaim.value.code == "coverage_overclaim"


def test_context_request_resolution_repairs_only_seeded_gap_and_denial_keeps_it_visible(tmp_path: Path) -> None:
    repo, resource, intent, seeded, prior_package, gap = _seeded(tmp_path)
    view = resource.resolve_committed("refs/heads/main", observed_at="2026-08-10T00:00:00Z")
    request = ContextRequest.create(
        prior_package,
        gap_ids=[gap.unknown_id],
        horizon="COMPONENT",
        requested_dimensions=["recovery"],
        requested_evidence=["committed recovery ownership and backup boundary"],
        question="Which exact committed component owns recovery?",
        reason="repair the visible recovery gap",
        counterexample="a summary that omits the backup boundary",
    )
    request.validate_source_package(prior_package)

    recovery = UnderstandingClaim.create(
        view,
        subject="repository recovery boundary",
        dimension="recovery",
        kind="FACT",
        statement="recovery evidence is supplied by the exact committed M2c record",
        evidence_refs=[_source_ref("recovery-evidence")],
    )
    expanded = RepositoryUnderstandingView.create(
        intent,
        view,
        claims=[*seeded.claims, recovery],
        requested_dimensions=["ownership", "recovery"],
        covered_dimensions=["ownership", "recovery"],
        must_see_categories=["recovery"],
        covered_must_see=["recovery"],
    )
    runtime = tmp_path / "runtime"
    content_store = ImmutableContentStore(runtime)
    binding_store = DurableBindingStore(runtime, content_store=content_store)
    added_fragment, reconstructed = transport_semantic_record(expanded, view, content_store, binding_store)
    assert reconstructed == expanded
    repaired_package = ContextPackage.from_understanding(
        expanded,
        horizon="COMPONENT",
        included_fragment_ids=[*prior_package.included_fragment_ids, added_fragment.fragment_id],
    )
    resolution = ContextResolution.create(
        request,
        prior_package,
        resulting_package=repaired_package,
        added_fragments=[added_fragment],
        binding_store=binding_store,
        resolved_gap_ids=[gap.unknown_id],
    )
    assert resolution.outcome == "RESOLVED"
    assert gap.unknown_id not in repaired_package.gap_ids
    assert set(repaired_package.gap_ids) == set(prior_package.gap_ids) - {gap.unknown_id}
    assert prior_package.package_id != repaired_package.package_id
    assert prior_package.gap_ids == (gap.unknown_id,)
    binding_store.close()

    denied = ContextResolution.create(
        request,
        prior_package,
        outcome="DENIED",
        denial_reason="policy denied the requested source evidence",
    )
    assert denied.unresolved_gap_ids == prior_package.gap_ids
    assert denied.resulting_package_id is None
    assert ContextRequest.from_mapping(request.as_dict()) == request
    assert ContextPackage.from_mapping(prior_package.as_dict()) == prior_package
    assert ContextPackage.from_mapping(repaired_package.as_dict()) == repaired_package
    assert ContextResolution.from_mapping(resolution.as_dict()) == resolution
    assert RepositoryUnderstandingView.from_mapping(expanded.as_dict()) == expanded


def test_decision_is_deterministic_and_rejects_stale_repo_intent_and_package_basis(tmp_path: Path) -> None:
    repo, resource, intent, understanding, package, _gap = _seeded(tmp_path)
    option_zero = DecisionOption("OPTION_ZERO", "retain the current read-only path", ("no new writer",), ("known gap remains",))
    option_one = DecisionOption("OPTION_ONE", "expand exact recovery context", ("more evidence",), ("larger package",))
    decision = EngineeringDecision.create(
        intent,
        [understanding.repo_view],
        [package],
        established_fact_claim_ids=[understanding.claims[0].claim_id],
        alternatives=[option_zero, option_one],
        chosen_option_id="OPTION_ONE",
        must_preserve=["exact RepoView authority", "runtime OFF"],
        must_not=["Task lifecycle", "private chain-of-thought"],
        expected_effect_scope=["M2c semantic records only"],
        acceptance_obligations=["seeded gap repair"],
        evidence_obligations=["M2b exact roundtrip"],
        uncertainty=["recovery evidence is initially unknown"],
        requested_context_ids=[],
        architecture_consequences=["rebuildable projection"],
        revisit_triggers=["RepoView or intent basis changes"],
    )
    same = EngineeringDecision.create(
        intent,
        [understanding.repo_view],
        [package],
        established_fact_claim_ids=[understanding.claims[0].claim_id],
        alternatives=[option_zero, option_one],
        chosen_option_id="OPTION_ONE",
        must_preserve=["exact RepoView authority", "runtime OFF"],
        must_not=["Task lifecycle", "private chain-of-thought"],
        expected_effect_scope=["M2c semantic records only"],
        acceptance_obligations=["seeded gap repair"],
        evidence_obligations=["M2b exact roundtrip"],
        uncertainty=["recovery evidence is initially unknown"],
        architecture_consequences=["rebuildable projection"],
        revisit_triggers=["RepoView or intent basis changes"],
    )
    assert same.decision_id == decision.decision_id
    assert EngineeringDecision.from_mapping(decision.as_dict()) == decision
    validate_decision_applicability(decision, intent_basis=intent, repo_views=[understanding.repo_view], context_packages=[package])

    with pytest.raises(EngineeringIntelligenceError) as stale_intent:
        validate_decision_applicability(
            decision,
            intent_basis=IntentBasis(intent.task_id, "r2", intent.intent_digest),
            repo_views=[understanding.repo_view],
            context_packages=[package],
        )
    assert stale_intent.value.code == "stale_intent_basis"

    (repo / "README.md").write_text("M2c changed\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "changed view")
    changed_view = resource.resolve_committed("refs/heads/main", observed_at="2026-08-10T00:01:00Z")
    with pytest.raises(EngineeringIntelligenceError) as stale_view:
        validate_decision_applicability(decision, intent_basis=intent, repo_views=[changed_view], context_packages=[package])
    assert stale_view.value.code == "stale_decision_basis"

    changed_package = ContextPackage.from_understanding(
        understanding,
        horizon="COMPONENT",
        included_fragment_ids=[*package.included_fragment_ids, _source_ref("new-fragment")],
    )
    with pytest.raises(EngineeringIntelligenceError) as stale_package:
        validate_decision_applicability(decision, intent_basis=intent, repo_views=[understanding.repo_view], context_packages=[changed_package])
    assert stale_package.value.code == "stale_decision_basis"


def test_semantic_record_roundtrip_and_import_side_effect_contract(tmp_path: Path) -> None:
    _repo, _resource, view, intent = _fixture(tmp_path)
    unknown = Unknown.create(view, subject="network boundary", dimension="network", reason="not requested")
    understanding = RepositoryUnderstandingView.create(
        intent,
        view,
        requested_dimensions=["network"],
        covered_dimensions=[],
        unknowns=[unknown],
    )
    package = ContextPackage.from_understanding(understanding, horizon="LOCAL")
    request = ContextRequest.create(
        package,
        gap_ids=[unknown.unknown_id],
        horizon="LOCAL",
        requested_dimensions=["network"],
        requested_evidence=["network boundary"],
        question="What is the network boundary?",
        reason="unknown boundary",
    )
    raw = request.to_json_bytes()
    assert reconstruct_semantic_record(raw) == request
    assert raw == request.to_json_bytes()

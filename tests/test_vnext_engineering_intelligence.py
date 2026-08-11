from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from bdb_shared.evidence import semantic_digest
from bdb_vnext.content_store import DurableBindingStore, ImmutableContentStore, TypedContextFragment, make_content_ref
from bdb_vnext.engineering_intelligence import (
    ClaimContradiction,
    ContextAffordance,
    ContextPackage,
    ContextRequest,
    ContextResolution,
    CoverageBinding,
    DecisionOption,
    EngineeringDecision,
    EngineeringIntelligenceError,
    GapResolutionEvidence,
    IntentBasis,
    Omission,
    RepoSourceEvidence,
    RepositoryUnderstandingView,
    SourceEvidenceRef,
    UnderstandingClaim,
    Unknown,
    publish_repo_source_evidence,
    reconstruct_semantic_record,
    transport_semantic_record,
    validate_decision_applicability,
)
from bdb_vnext.repo_view import RepositoryResource


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)
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
    (repo / "docs").mkdir()
    (repo / "docs" / "recovery.txt").write_text("RECOVERY = 'A'\n", encoding="utf-8")
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


def _accepted_fragment(tmp_path: Path, view: object, label: str) -> tuple[ImmutableContentStore, DurableBindingStore, TypedContextFragment, SourceEvidenceRef]:
    runtime = tmp_path / f"runtime-{label}"
    content_store = ImmutableContentStore(runtime)
    binding_store = DurableBindingStore(runtime, content_store=content_store)
    raw = f"source evidence: {label}\n".encode("utf-8")
    content_ref = make_content_ref("text/plain", "m2c-source-v1", raw)
    content_store.publish(content_ref, raw)
    fragment = TypedContextFragment.create(
        view,
        content_ref,
        fragment_type="text/plain",
        fragment_schema="m2c-source-v1",
        payload_size_bytes=len(raw),
    )
    binding_store.accept(fragment, view=view)
    evidence = SourceEvidenceRef.create_verified(view, fragment, binding_store)
    return content_store, binding_store, fragment, evidence


def _repo_source(
    tmp_path: Path,
    view: object,
    path: str,
    *,
    content_store: ImmutableContentStore | None = None,
    binding_store: DurableBindingStore | None = None,
) -> tuple[ImmutableContentStore, DurableBindingStore, TypedContextFragment, RepoSourceEvidence]:
    if content_store is None or binding_store is None:
        runtime = tmp_path / "runtime-repo-source"
        content_store = ImmutableContentStore(runtime)
        binding_store = DurableBindingStore(runtime, content_store=content_store)
    evidence = publish_repo_source_evidence(
        view,
        path,
        content_store,
        binding_store,
        fragment_type="text/plain",
        fragment_schema="m2c-repo-source-v1",
    )
    fragment = binding_store.resolve_accepted(evidence.fragment_id, expected_view=view).fragment
    return content_store, binding_store, fragment, evidence


def _seeded(tmp_path: Path):
    repo, resource, view, intent = _fixture(tmp_path)
    content_store, binding_store, ownership_fragment, ownership_evidence = _repo_source(tmp_path, view, "src/service.py")
    ownership = UnderstandingClaim.create(
        view,
        subject="src/service.py",
        dimension="ownership",
        kind="FACT",
        statement="component ownership is explicit in the committed source",
        source_evidence=[ownership_evidence],
        binding_store=binding_store,
    )
    ownership_coverage = CoverageBinding.create(
        view,
        target_kind="DIMENSION",
        target="ownership",
        supporting_claim_ids=[ownership.claim_id],
        supporting_fragment_ids=[ownership_fragment.fragment_id],
    )
    recovery_gap = Unknown.create(
        view,
        subject="repository recovery boundary",
        dimension="recovery",
        reason="no exact recovery evidence was requested in this initial package",
    )
    unrelated_gap = Unknown.create(
        view,
        subject="dependency boundary",
        dimension="dependencies",
        reason="dependency evidence was intentionally not requested",
    )
    understanding = RepositoryUnderstandingView.create(
        intent,
        view,
        claims=[ownership],
        requested_dimensions=["ownership", "recovery", "dependencies"],
        covered_dimensions=["ownership"],
        must_see_categories=["recovery"],
        covered_must_see=[],
        coverage_bindings=[ownership_coverage],
        unknowns=[recovery_gap, unrelated_gap],
        binding_store=binding_store,
    )
    package = ContextPackage.from_understanding(
        understanding,
        horizon="COMPONENT",
        included_fragment_ids=[ownership_fragment.fragment_id],
    )
    return (
        repo,
        resource,
        view,
        intent,
        content_store,
        binding_store,
        ownership_fragment,
        ownership,
        understanding,
        package,
        recovery_gap,
        unrelated_gap,
    )


def _digest(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


def test_fact_requires_verified_source_evidence_and_exact_repo_content_binding(tmp_path: Path) -> None:
    _repo, _resource, view, _intent = _fixture(tmp_path)
    with pytest.raises(EngineeringIntelligenceError) as arbitrary:
        UnderstandingClaim.create(
            view,
            subject="src/service.py",
            dimension="ownership",
            kind="FACT",
            statement="untrusted digest is not source authority",
            evidence_refs=[_digest("arbitrary")],
        )
    assert arbitrary.value.code == "repository_source_evidence_required"

    generic_content, generic_bindings, generic_fragment, generic_evidence = _accepted_fragment(tmp_path, view, "accepted")
    with pytest.raises(EngineeringIntelligenceError) as generic:
        UnderstandingClaim.create(
            view,
            subject="src/service.py",
            dimension="ownership",
            kind="FACT",
            statement="a generic accepted fragment is not repository source authority",
            source_evidence=[generic_evidence],
            binding_store=generic_bindings,
        )
    assert generic.value.code == "repository_source_evidence_required"

    content, bindings, fragment, evidence = _repo_source(tmp_path, view, "src/service.py")
    claim = UnderstandingClaim.create(
        view,
        subject="src/service.py",
        dimension="ownership",
        kind="FACT",
        statement="accepted source is exact",
        source_evidence=[evidence],
        binding_store=bindings,
    )
    assert claim.source_evidence[0].fragment_id == fragment.fragment_id
    foreign_root = tmp_path / "foreign"
    foreign_root.mkdir()
    _foreign_repo, _foreign_resource, foreign_view, _foreign_intent = _fixture(foreign_root)
    foreign_content, foreign_bindings, foreign_fragment, foreign_evidence = _repo_source(foreign_root, foreign_view, "src/service.py")
    content.publish(foreign_evidence.content_ref, foreign_content.resolve(foreign_evidence.content_ref))
    bindings.accept(foreign_fragment, view=foreign_view)
    with pytest.raises(EngineeringIntelligenceError) as foreign_repo:
        UnderstandingClaim.create(
            view,
            subject="src/service.py",
            dimension="ownership",
            kind="FACT",
            statement="foreign RepoView cannot ground this claim",
            source_evidence=[foreign_evidence],
            binding_store=bindings,
        )
    assert foreign_repo.value.code == "repository_source_repo_mismatch"
    with pytest.raises(EngineeringIntelligenceError) as unaccepted:
        UnderstandingClaim.create(
            view,
            subject="src/service.py",
            dimension="ownership",
            kind="FACT",
            statement="descriptor must be accepted",
            source_evidence=[evidence],
            binding_store=DurableBindingStore(tmp_path / "empty-runtime"),
        )
    assert unaccepted.value.code == "repository_source_binding_failure"
    bindings.close()
    generic_bindings.close()
    foreign_bindings.close()


def test_repo_source_evidence_proves_committed_bytes_and_rejects_fabricated_accepted_bytes(tmp_path: Path) -> None:
    repo, _resource, view, _intent = _fixture(tmp_path)
    content, bindings, fragment, evidence = _repo_source(tmp_path, view, "src/service.py")
    assert view.query().read_bytes("src/service.py") == b"OWNER = 'A'\n"
    assert bindings.resolve_accepted(evidence.fragment_id, expected_view=view).raw == b"OWNER = 'A'\n"
    assert evidence.source_object_id == view.query().get_entry("src/service.py").object_oid

    # The checkout may become dirty after the exact committed RepoView is built;
    # source evidence still resolves the immutable committed blob.
    (repo / "src" / "service.py").write_text("OWNER = 'WORKTREE'\n", encoding="utf-8")
    assert view.read_text("src/service.py") == "OWNER = 'A'\n"
    assert bindings.resolve_accepted(evidence.fragment_id, expected_view=view).raw == b"OWNER = 'A'\n"

    claim = UnderstandingClaim.create(
        view,
        subject="src/service.py",
        dimension="ownership",
        kind="FACT",
        statement="ownership is grounded in the exact committed bytes",
        source_evidence=[evidence],
        binding_store=bindings,
    )
    assert claim.authority == "EXACT_SOURCE"

    fabricated_raw = b"OWNER = 'EVIL'\n"
    fabricated_ref = make_content_ref(evidence.content_ref.type, evidence.content_ref.schema, fabricated_raw)
    content.publish(fabricated_ref, fabricated_raw)
    fabricated_fragment = TypedContextFragment.create(
        view,
        fabricated_ref,
        fragment_type=evidence.fragment_type,
        fragment_schema=evidence.fragment_schema,
        payload_size_bytes=len(fabricated_raw),
    )
    bindings.accept(fabricated_fragment, view=view)
    fabricated_evidence = RepoSourceEvidence.from_fragment(
        view=view,
        source_path=evidence.source_path,
        source_object_id=evidence.source_object_id,
        fragment=fabricated_fragment,
    )
    with pytest.raises(EngineeringIntelligenceError) as mismatch:
        fabricated_evidence.validate(view, bindings)
    assert mismatch.value.code == "repository_source_mismatch"
    bindings.close()


def test_repo_source_evidence_fails_closed_for_foreign_stale_and_missing_sources(tmp_path: Path) -> None:
    repo, resource, view, _intent = _fixture(tmp_path)
    content, bindings, _fragment, evidence = _repo_source(tmp_path, view, "src/service.py")

    foreign_root = tmp_path / "foreign-source"
    foreign_root.mkdir()
    _foreign_repo, _foreign_resource, foreign_view, _foreign_intent = _fixture(foreign_root)
    _foreign_content, foreign_bindings, _foreign_fragment, foreign_evidence = _repo_source(
        foreign_root,
        foreign_view,
        "src/service.py",
    )
    with pytest.raises(EngineeringIntelligenceError) as foreign:
        foreign_evidence.validate(view, bindings)
    assert foreign.value.code == "repository_source_repo_mismatch"

    (repo / "src" / "service.py").write_text("OWNER = 'B'\n", encoding="utf-8")
    _git(repo, "add", "src/service.py")
    _git(repo, "commit", "-qm", "stale source fixture")
    stale_view = resource.resolve_committed("refs/heads/main", observed_at="2026-08-10T00:02:00Z")
    with pytest.raises(EngineeringIntelligenceError) as stale:
        evidence.validate(stale_view, bindings)
    assert stale.value.code == "repository_source_repo_mismatch"

    with pytest.raises(EngineeringIntelligenceError) as missing:
        publish_repo_source_evidence(
            view,
            "missing/source.txt",
            content,
            bindings,
            fragment_type="text/plain",
            fragment_schema="m2c-repo-source-v1",
        )
    assert missing.value.code == "repository_source_not_found"
    with pytest.raises(EngineeringIntelligenceError) as unsafe:
        publish_repo_source_evidence(
            view,
            "../src/service.py",
            content,
            bindings,
            fragment_type="text/plain",
            fragment_schema="m2c-repo-source-v1",
        )
    assert unsafe.value.code == "unsafe_source_path"
    bindings.close()
    foreign_bindings.close()


def test_understanding_claims_separate_fact_inference_and_require_exact_basis(tmp_path: Path) -> None:
    (_repo, _resource, _view, _intent, _content, bindings, _fragment, _ownership, understanding, _package, _gap, _other) = _seeded(tmp_path)
    with pytest.raises(EngineeringIntelligenceError) as failure:
        UnderstandingClaim(
            _digest("bad-claim"),
            understanding.repo_view,
            "src/service.py",
            "ownership",
            "FACT",
            "DERIVED",
            "source-looking claim",
            (_digest("bad"),),
            (),
        )
    assert failure.value.code == "claim_authority_mismatch"
    bindings.close()


def test_coverage_requires_explicit_grounded_bindings_and_rejects_random_support(tmp_path: Path) -> None:
    (_repo, _resource, view, intent, _content, bindings, _fragment, ownership, _understanding, _package, gap, _other) = _seeded(tmp_path)
    with pytest.raises(EngineeringIntelligenceError) as missing:
        RepositoryUnderstandingView.create(
            intent,
            view,
            claims=[ownership],
            requested_dimensions=["ownership"],
            covered_dimensions=["ownership"],
            binding_store=bindings,
        )
    assert missing.value.code == "coverage_grounding_required"
    random_binding = CoverageBinding.create(
        view,
        target_kind="DIMENSION",
        target="ownership",
        supporting_claim_ids=[_digest("foreign-claim")],
    )
    with pytest.raises(EngineeringIntelligenceError) as foreign:
        RepositoryUnderstandingView.create(
            intent,
            view,
            claims=[ownership],
            requested_dimensions=["ownership"],
            covered_dimensions=["ownership"],
            coverage_bindings=[random_binding],
            binding_store=bindings,
        )
    assert foreign.value.code == "coverage_claim_missing"
    assert gap.unknown_id not in ownership.claim_id
    bindings.close()


def test_conflicting_source_and_inference_remain_visible_and_source_wins(tmp_path: Path) -> None:
    (_repo, _resource, view, _intent, _content, bindings, _fragment, source, seeded, _package, _gap, _other) = _seeded(tmp_path)
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
        view,
        claims=[source, inference],
        requested_dimensions=["ownership"],
        covered_dimensions=["ownership"],
        coverage_bindings=[CoverageBinding.create(view, target_kind="DIMENSION", target="ownership", supporting_claim_ids=[source.claim_id])],
        contradictions=[contradiction],
        binding_store=bindings,
    )
    assert combined.contradictions[0].source_authority == "EXACT_SOURCE_WINS"
    assert combined.coverage_status == "PARTIAL"
    with pytest.raises(EngineeringIntelligenceError) as missing_contradiction:
        RepositoryUnderstandingView.create(
            seeded.intent_basis,
            view,
            claims=[source, inference],
            requested_dimensions=["ownership"],
            covered_dimensions=["ownership"],
            coverage_bindings=[CoverageBinding.create(view, target_kind="DIMENSION", target="ownership", supporting_claim_ids=[source.claim_id])],
            binding_store=bindings,
        )
    assert missing_contradiction.value.code == "contradiction_required"
    bindings.close()


def test_context_resolution_preserves_unrelated_gaps_and_denial_keeps_all_visible(tmp_path: Path) -> None:
    (
        _repo,
        _resource,
        view,
        intent,
        content_store,
        bindings,
        ownership_fragment,
        ownership,
        seeded,
        prior_package,
        recovery_gap,
        unrelated_gap,
    ) = _seeded(tmp_path)
    recovery_affordance = ContextAffordance.create(
        gap_ids=[recovery_gap.unknown_id],
        dimension="recovery",
        horizon="COMPONENT",
        evidence_type="repository-source",
        evidence_requirements=["live-binding-authority", "focused-validation"],
        reason="repair only the visible recovery gap with declared semantic evidence",
    )
    unrelated_affordance = ContextAffordance.create(
        gap_ids=[unrelated_gap.unknown_id],
        dimension="dependencies",
        horizon="COMPONENT",
        evidence_type="repository-source",
        evidence_requirements=["dependency-contract"],
        reason="dependency evidence is separate from recovery evidence",
    )
    prior_package = ContextPackage.from_understanding(
        seeded,
        horizon="COMPONENT",
        included_fragment_ids=[ownership_fragment.fragment_id],
        affordances=[recovery_affordance, unrelated_affordance],
    )
    request = ContextRequest.create(
        prior_package,
        gap_ids=[recovery_gap.unknown_id],
        selected_affordance_ids=[recovery_affordance.affordance_id],
        horizon="COMPONENT",
        requested_dimensions=["recovery"],
        requested_evidence_requirements=["live-binding-authority", "focused-validation"],
        requested_evidence=["committed recovery boundary"],
        question="Which exact committed component owns recovery?",
        reason="repair only the visible recovery gap",
    )
    _recovery_content, _recovery_bindings, recovery_fragment, recovery_evidence = _repo_source(
        tmp_path,
        view,
        "docs/recovery.txt",
        content_store=content_store,
        binding_store=bindings,
    )
    recovery = UnderstandingClaim.create(
        view,
        subject="repository recovery boundary",
        dimension="recovery",
        kind="FACT",
        statement="recovery evidence is supplied by the exact committed source",
        source_evidence=[recovery_evidence],
        binding_store=bindings,
    )
    recovery_dimension = CoverageBinding.create(
        view,
        target_kind="DIMENSION",
        target="recovery",
        supporting_claim_ids=[recovery.claim_id],
        supporting_fragment_ids=[recovery_fragment.fragment_id],
    )
    recovery_must_see = CoverageBinding.create(
        view,
        target_kind="MUST_SEE",
        target="recovery",
        supporting_claim_ids=[recovery.claim_id],
        supporting_fragment_ids=[recovery_fragment.fragment_id],
    )
    expanded = RepositoryUnderstandingView.create(
        intent,
        view,
        claims=[ownership, recovery],
        requested_dimensions=["ownership", "recovery", "dependencies"],
        covered_dimensions=["ownership", "recovery"],
        must_see_categories=["recovery"],
        covered_must_see=["recovery"],
        coverage_bindings=[seeded.coverage_bindings[0], recovery_dimension, recovery_must_see],
        unknowns=[unrelated_gap],
        binding_store=bindings,
    )
    repaired_package = ContextPackage.from_understanding(
        expanded,
        horizon="COMPONENT",
        included_fragment_ids=[ownership_fragment.fragment_id, recovery_fragment.fragment_id],
        affordances=[unrelated_affordance],
    )
    evidence = GapResolutionEvidence.create(
        gap_id=recovery_gap.unknown_id,
        added_fragment_ids=[recovery_fragment.fragment_id],
        supporting_claim_ids=[recovery.claim_id],
        coverage_binding_ids=[recovery_dimension.coverage_binding_id],
    )
    resolution = ContextResolution.create(
        request,
        prior_package,
        resulting_package=repaired_package,
        added_fragments=[recovery_fragment],
        binding_store=bindings,
        resolved_gap_ids=[recovery_gap.unknown_id],
        gap_resolution_evidence=[evidence],
    )
    assert resolution.outcome == "RESOLVED"
    assert set(repaired_package.gap_ids) == {unrelated_gap.unknown_id}
    assert resolution.unresolved_gap_ids == (unrelated_gap.unknown_id,)
    with pytest.raises(EngineeringIntelligenceError) as silent_drop:
        ContextResolution.create(
            request,
            prior_package,
            resulting_package=repaired_package,
            added_fragments=[recovery_fragment],
            binding_store=bindings,
            resolved_gap_ids=[recovery_gap.unknown_id],
            unresolved_gap_ids=[],
            gap_resolution_evidence=[evidence],
        )
    assert silent_drop.value.code == "resolution_gap_mismatch"
    denied = ContextResolution.create(request, prior_package, outcome="DENIED", denial_reason="policy denied")
    assert denied.unresolved_gap_ids == prior_package.gap_ids
    assert ContextResolution.from_mapping(resolution.as_dict()) == resolution
    bindings.close()


def test_context_affordance_request_precision_is_generic_and_fail_closed(tmp_path: Path) -> None:
    (
        _repo,
        _resource,
        view,
        _intent,
        _content,
        bindings,
        ownership_fragment,
        _ownership,
        understanding,
        package,
        recovery_gap,
        unrelated_gap,
    ) = _seeded(tmp_path)
    recovery_affordance = ContextAffordance.create(
        gap_ids=[recovery_gap.unknown_id],
        dimension="recovery",
        horizon="COMPONENT",
        evidence_type="repository-source",
        evidence_requirements=["live-binding-authority", "focused-validation"],
        reason="the visible recovery gap has a declared source-backed evidence policy",
    )
    unrelated_affordance = ContextAffordance.create(
        gap_ids=[unrelated_gap.unknown_id],
        dimension="dependencies",
        horizon="COMPONENT",
        evidence_type="repository-source",
        evidence_requirements=["dependency-contract"],
        reason="dependency evidence is a separate visible gap",
    )
    precise_package = ContextPackage.from_understanding(
        understanding,
        horizon="COMPONENT",
        included_fragment_ids=[ownership_fragment.fragment_id],
        affordances=[recovery_affordance, unrelated_affordance],
    )
    assert set(precise_package.gap_ids) == {recovery_gap.unknown_id, unrelated_gap.unknown_id}
    request = ContextRequest.create(
        precise_package,
        gap_ids=[recovery_gap.unknown_id],
        selected_affordance_ids=[recovery_affordance.affordance_id],
        horizon="COMPONENT",
        requested_dimensions=["recovery"],
        requested_evidence_requirements=["live-binding-authority", "focused-validation"],
        requested_evidence=["exact committed recovery evidence"],
        question="Which accepted source evidence establishes recovery?",
        reason="repair only the selected visible recovery gap",
    )
    assert request.selected_affordance_ids == (recovery_affordance.affordance_id,)
    assert request.requested_evidence_requirements == ("live-binding-authority", "focused-validation")
    assert ContextRequest.from_mapping(request.as_dict()) == request

    foreign_gap_affordance = ContextAffordance.create(
        gap_ids=[_digest("foreign-gap")],
        dimension="recovery",
        horizon="COMPONENT",
        evidence_type="repository-source",
        evidence_requirements=["live-binding-authority"],
        reason="foreign gap must not enter this package",
    )
    with pytest.raises(EngineeringIntelligenceError) as foreign_gap:
        ContextPackage.from_understanding(
            understanding,
            horizon="COMPONENT",
            included_fragment_ids=[ownership_fragment.fragment_id],
            affordances=[foreign_gap_affordance],
        )
    assert foreign_gap.value.code == "affordance_gap_not_visible"

    wrong_dimension_affordance = ContextAffordance.create(
        gap_ids=[recovery_gap.unknown_id],
        dimension="dependencies",
        horizon="COMPONENT",
        evidence_type="repository-source",
        evidence_requirements=["dependency-contract"],
        reason="a dimension mismatch must fail closed",
    )
    with pytest.raises(EngineeringIntelligenceError) as wrong_dimension:
        ContextPackage.from_understanding(
            understanding,
            horizon="COMPONENT",
            included_fragment_ids=[ownership_fragment.fragment_id],
            affordances=[wrong_dimension_affordance],
        )
    assert wrong_dimension.value.code == "affordance_dimension_mismatch"

    with pytest.raises(EngineeringIntelligenceError) as foreign_selection:
        ContextRequest.create(
            precise_package,
            gap_ids=[recovery_gap.unknown_id],
            selected_affordance_ids=[_digest("foreign-affordance")],
            horizon="COMPONENT",
            requested_dimensions=["recovery"],
            requested_evidence_requirements=["live-binding-authority"],
            requested_evidence=["exact committed recovery evidence"],
            question="Which accepted source evidence establishes recovery?",
            reason="foreign affordance must not broaden the request",
        )
    assert foreign_selection.value.code == "affordance_not_in_source_package"

    with pytest.raises(EngineeringIntelligenceError) as wrong_gap:
        ContextRequest.create(
            precise_package,
            gap_ids=[recovery_gap.unknown_id],
            selected_affordance_ids=[unrelated_affordance.affordance_id],
            horizon="COMPONENT",
            requested_dimensions=["dependencies"],
            requested_evidence_requirements=["dependency-contract"],
            requested_evidence=["dependency evidence"],
            question="Which dependency evidence is relevant?",
            reason="an unrelated affordance cannot repair recovery",
        )
    assert wrong_gap.value.code == "affordance_gap_mismatch"

    with pytest.raises(EngineeringIntelligenceError) as not_afforded:
        ContextRequest.create(
            precise_package,
            gap_ids=[recovery_gap.unknown_id],
            selected_affordance_ids=[recovery_affordance.affordance_id],
            horizon="COMPONENT",
            requested_dimensions=["recovery"],
            requested_evidence_requirements=["serialization-contract"],
            requested_evidence=["serialization evidence"],
            question="Which serialization evidence is relevant?",
            reason="unrepresented evidence must not be silently accepted",
        )
    assert not_afforded.value.code == "requested_evidence_not_afforded"

    with pytest.raises(EngineeringIntelligenceError) as duplicate_affordance:
        ContextRequest.create(
            precise_package,
            gap_ids=[recovery_gap.unknown_id],
            selected_affordance_ids=[recovery_affordance.affordance_id, recovery_affordance.affordance_id],
            horizon="COMPONENT",
            requested_dimensions=["recovery"],
            requested_evidence_requirements=["live-binding-authority"],
            requested_evidence=["exact committed recovery evidence"],
            question="Which accepted source evidence establishes recovery?",
            reason="duplicate selection must fail closed",
        )
    assert duplicate_affordance.value.code == "duplicate_m2c_value"

    with pytest.raises(EngineeringIntelligenceError) as duplicate_requirement:
        ContextRequest.create(
            precise_package,
            gap_ids=[recovery_gap.unknown_id],
            selected_affordance_ids=[recovery_affordance.affordance_id],
            horizon="COMPONENT",
            requested_dimensions=["recovery"],
            requested_evidence_requirements=["live-binding-authority", "live-binding-authority"],
            requested_evidence=["exact committed recovery evidence"],
            question="Which accepted source evidence establishes recovery?",
            reason="duplicate evidence selection must fail closed",
        )
    assert duplicate_requirement.value.code == "duplicate_m2c_value"

    with pytest.raises(EngineeringIntelligenceError) as stale_package:
        request.validate_source_package(package)
    assert stale_package.value.code == "stale_request_basis"
    foreign_root = tmp_path / "foreign-repo"
    foreign_root.mkdir()
    _foreign_repo, _foreign_resource, foreign_view, foreign_intent = _fixture(foreign_root)
    foreign_gap = Unknown.create(foreign_view, subject="recovery", dimension="recovery", reason="foreign basis")
    foreign_understanding = RepositoryUnderstandingView.create(
        foreign_intent,
        foreign_view,
        requested_dimensions=["recovery"],
        unknowns=[foreign_gap],
    )
    foreign_package = ContextPackage.from_understanding(foreign_understanding, horizon="COMPONENT")
    with pytest.raises(EngineeringIntelligenceError) as stale:
        request.validate_source_package(foreign_package)
    assert stale.value.code == "stale_request_basis"

    legacy_affordance = recovery_affordance.as_dict()
    for field in ("gap_ids", "evidence_requirements", "contract_version"):
        legacy_affordance.pop(field)
    with pytest.raises(EngineeringIntelligenceError) as legacy_affordance_error:
        ContextAffordance.from_mapping(legacy_affordance)
    assert legacy_affordance_error.value.code == "malformed_m2c_record"

    legacy_package = precise_package.as_dict()
    legacy_package.pop("contract_version")
    with pytest.raises(EngineeringIntelligenceError) as legacy_package_error:
        ContextPackage.from_mapping(legacy_package)
    assert legacy_package_error.value.code == "malformed_m2c_record"

    legacy_request = request.as_dict()
    for field in ("selected_affordance_ids", "requested_evidence_requirements", "contract_version"):
        legacy_request.pop(field)
    with pytest.raises(EngineeringIntelligenceError) as legacy_request_error:
        ContextRequest.from_mapping(legacy_request)
    assert legacy_request_error.value.code == "malformed_m2c_record"
    bindings.close()


def test_request_guidance_projection_separates_covered_and_unresolved_evidence(tmp_path: Path) -> None:
    (
        _repo,
        _resource,
        view,
        _intent,
        _content,
        bindings,
        ownership_fragment,
        _ownership,
        understanding,
        original_package,
        recovery_gap,
        dependency_gap,
    ) = _seeded(tmp_path)
    recovery_affordance = ContextAffordance.create(
        gap_ids=[recovery_gap.unknown_id],
        dimension="recovery",
        horizon="COMPONENT",
        evidence_type="repository-source",
        evidence_requirements=["live-accepted-binding"],
        reason="only the live recovery binding remains unresolved",
    )
    dependency_affordance = ContextAffordance.create(
        gap_ids=[dependency_gap.unknown_id],
        dimension="dependencies",
        horizon="COMPONENT",
        evidence_type="repository-source",
        evidence_requirements=["dependency-contract"],
        reason="the independent dependency contract also remains unresolved",
    )
    package = ContextPackage.from_understanding(
        understanding,
        horizon="COMPONENT",
        included_fragment_ids=[ownership_fragment.fragment_id],
        affordances=[recovery_affordance, dependency_affordance],
    )

    projection = package.request_guidance_projection()
    assert projection["projection_version"] == "m2c-request-guidance-v1"
    assert projection["source_package_id"] == package.package_id
    assert projection["repo_view_binding_status"] == "EXACT_COMMITTED_IDENTITY_BOUND"
    assert projection["repo_view_basis"] == package.repo_view.as_dict()
    assert projection["covered_dimensions"] == ["ownership"]
    gaps = {item["dimension"]: item for item in projection["unresolved_gaps"]}
    assert set(gaps) == {"recovery", "dependencies"}
    assert gaps["recovery"]["affordances"][0]["evidence_requirements"] == ["live-accepted-binding"]
    assert gaps["dependencies"]["affordances"][0]["evidence_requirements"] == ["dependency-contract"]
    assert package.coverage_status == original_package.coverage_status == "BLOCKED"
    assert set(package.gap_ids) == {recovery_gap.unknown_id, dependency_gap.unknown_id}

    multi_gap_request = ContextRequest.create(
        package,
        gap_ids=[recovery_gap.unknown_id, dependency_gap.unknown_id],
        selected_affordance_ids=[recovery_affordance.affordance_id, dependency_affordance.affordance_id],
        horizon="COMPONENT",
        requested_dimensions=["recovery", "dependencies"],
        requested_evidence_requirements=["live-accepted-binding", "dependency-contract"],
        requested_evidence=["live recovery binding", "dependency contract"],
        question="Which exact evidence satisfies the two still-visible gaps?",
        reason="request both genuinely unresolved dimensions without reopening ownership",
    )
    multi_gap_request.validate_source_package(package)

    parsed = ContextPackage.from_mapping(package.as_dict())
    assert parsed.request_guidance_projection() == projection
    assert parsed.to_json_bytes() == package.to_json_bytes()
    bindings.close()


def test_unbound_affordance_cannot_shadow_a_visible_gap_dimension(tmp_path: Path) -> None:
    (
        _repo,
        _resource,
        _view,
        _intent,
        _content,
        bindings,
        ownership_fragment,
        _ownership,
        understanding,
        _package,
        _recovery_gap,
        _dependency_gap,
    ) = _seeded(tmp_path)
    ambiguous = ContextAffordance.create(
        dimension="recovery",
        horizon="COMPONENT",
        evidence_type="repository-source",
        reason="generic source selection would hide the exact recovery evidence gap",
    )
    with pytest.raises(EngineeringIntelligenceError) as failure:
        ContextPackage.from_understanding(
            understanding,
            horizon="COMPONENT",
            included_fragment_ids=[ownership_fragment.fragment_id],
            affordances=[ambiguous],
        )
    assert failure.value.code == "affordance_binding_required"

    compatibility_only = ContextAffordance.create(
        dimension="future-source-context",
        horizon="COMPONENT",
        evidence_type="repository-source",
        reason="unbound compatibility guidance has no matching visible gap",
    )
    compatible_package = ContextPackage.from_understanding(
        understanding,
        horizon="COMPONENT",
        included_fragment_ids=[ownership_fragment.fragment_id],
        affordances=[compatibility_only],
    )
    assert compatible_package.affordances == (compatibility_only,)
    assert compatibility_only.gap_ids == ()
    assert compatibility_only.evidence_requirements == ()
    bindings.close()


def test_decision_claim_membership_and_exact_basis_sets(tmp_path: Path) -> None:
    (repo, resource, view, intent, _content, bindings, ownership_fragment, ownership, understanding, package, _gap, _other) = _seeded(tmp_path)
    option_zero = DecisionOption("OPTION_ZERO", "retain the read-only path", ("no writer",), ("gap remains",))
    option_one = DecisionOption("OPTION_ONE", "expand exact context", ("more evidence",), ("larger package",))
    kwargs = dict(
        established_fact_claim_ids=[ownership.claim_id],
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
        understanding_bases=[understanding],
        binding_store=bindings,
    )
    decision = EngineeringDecision.create(intent, [view], [package], **kwargs)
    same = EngineeringDecision.create(intent, [view], [package], **kwargs)
    assert same.decision_id == decision.decision_id
    package.validate_source_grounding(understanding, view, bindings)
    parsed_understanding = RepositoryUnderstandingView.from_mapping(understanding.as_dict())
    parsed_package = ContextPackage.from_mapping(package.as_dict())
    parsed_package.validate_source_grounding(parsed_understanding, view, bindings)
    _semantic_fragment, transported_understanding = transport_semantic_record(
        understanding,
        view,
        _content,
        bindings,
    )
    assert transported_understanding == understanding
    validate_decision_applicability(decision, intent_basis=intent, repo_views=[view], context_packages=[package])
    with pytest.raises(EngineeringIntelligenceError) as random_claim:
        EngineeringDecision.create(intent, [view], [package], established_fact_claim_ids=[_digest("random")], **{k: v for k, v in kwargs.items() if k != "established_fact_claim_ids"})
    assert random_claim.value.code == "decision_claim_basis_mismatch"
    changed_package = ContextPackage.from_understanding(understanding, horizon="COMPONENT", included_fragment_ids=[ownership_fragment.fragment_id, _digest("new")])
    with pytest.raises(EngineeringIntelligenceError) as old_new:
        validate_decision_applicability(decision, intent_basis=intent, repo_views=[view], context_packages=[package, changed_package])
    assert old_new.value.code == "stale_decision_basis"
    with pytest.raises(EngineeringIntelligenceError) as new_only:
        validate_decision_applicability(decision, intent_basis=intent, repo_views=[view], context_packages=[changed_package])
    assert new_only.value.code == "stale_decision_basis"
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "changed view")
    changed_view = resource.resolve_committed("refs/heads/main", observed_at="2026-08-10T00:01:00Z")
    with pytest.raises(EngineeringIntelligenceError) as stale_view:
        validate_decision_applicability(decision, intent_basis=intent, repo_views=[changed_view], context_packages=[package])
    assert stale_view.value.code == "stale_decision_basis"
    bindings.close()


def test_semantic_record_roundtrip_and_import_side_effect_contract(tmp_path: Path) -> None:
    _repo, _resource, view, intent = _fixture(tmp_path)
    unknown = Unknown.create(view, subject="network boundary", dimension="network", reason="not requested")
    understanding = RepositoryUnderstandingView.create(intent, view, requested_dimensions=["network"], unknowns=[unknown])
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
    content_store = ImmutableContentStore(tmp_path / "request-transport-runtime")
    with DurableBindingStore(tmp_path / "request-transport-runtime", content_store=content_store) as bindings:
        _fragment, transported = transport_semantic_record(request, view, content_store, bindings)
    assert transported == request

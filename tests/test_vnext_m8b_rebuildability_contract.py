from __future__ import annotations

import inspect
import subprocess
from pathlib import Path

import pytest

from bdb_shared.evidence import semantic_digest
from bdb_vnext.code_intelligence import CodeIntelligenceError, FallbackCodeFactProvider
from bdb_vnext.content_store import DurableBindingStore, ImmutableContentStore
from bdb_vnext.engineering_intelligence import (
    CoverageBinding,
    EngineeringIntelligenceError,
    IntentBasis,
    RepositoryUnderstandingView,
    UnderstandingClaim,
    publish_repo_source_evidence,
)
from bdb_vnext.repo_view import RepositoryResource


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _fixture(tmp_path: Path):
    repo = tmp_path / "subject"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "M8b Test")
    _git(repo, "config", "user.email", "m8b@example.invalid")
    (repo / "src").mkdir()
    (repo / "src" / "service.py").write_text(
        "def owner():\n    return 'A'\n",
        encoding="utf-8",
    )
    _git(repo, "add", "--all")
    _git(repo, "commit", "-qm", "M8b base")

    resource = RepositoryResource.from_path(repo, repository_id="m8b-subject")
    view = resource.resolve_committed("refs/heads/main", observed_at="2026-08-17T00:00:00Z")
    intent = IntentBasis(
        "task:m8b-rebuildability",
        "r1",
        semantic_digest({"intent": "understand exact source ownership"}),
    )
    runtime = tmp_path / "runtime"
    content_store = ImmutableContentStore(runtime)
    bindings = DurableBindingStore(runtime, content_store=content_store)
    return repo, resource, view, intent, content_store, bindings


def _build_understanding(view, intent, content_store, bindings):
    evidence = publish_repo_source_evidence(
        view,
        "src/service.py",
        content_store,
        bindings,
        fragment_type="text/plain",
        fragment_schema="m8b-exact-source-v1",
    )
    fragment = bindings.resolve_accepted(evidence.fragment_id, expected_view=view).fragment
    claim = UnderstandingClaim.create(
        view,
        subject="src/service.py",
        dimension="ownership",
        kind="FACT",
        statement="ownership is grounded in exact committed source",
        source_evidence=[evidence],
        binding_store=bindings,
    )
    coverage = CoverageBinding.create(
        view,
        target_kind="DIMENSION",
        target="ownership",
        supporting_claim_ids=[claim.claim_id],
        supporting_fragment_ids=[fragment.fragment_id],
    )
    understanding = RepositoryUnderstandingView.create(
        intent,
        view,
        claims=[claim],
        requested_dimensions=["ownership"],
        covered_dimensions=["ownership"],
        coverage_bindings=[coverage],
        binding_store=bindings,
    )
    return evidence, fragment, claim, coverage, understanding


def test_same_exact_repo_view_rebuilds_identical_understanding_identity(tmp_path: Path) -> None:
    repo, _resource, view, intent, content_store, bindings = _fixture(tmp_path)
    try:
        first = _build_understanding(view, intent, content_store, bindings)
        second = _build_understanding(view, intent, content_store, bindings)

        assert first[0].evidence_id == second[0].evidence_id
        assert first[1].fragment_id == second[1].fragment_id
        assert first[2].claim_id == second[2].claim_id
        assert first[3].coverage_binding_id == second[3].coverage_binding_id
        assert first[4].understanding_id == second[4].understanding_id
        assert first[4].to_json_bytes() == second[4].to_json_bytes()
        assert RepositoryUnderstandingView.from_mapping(first[4].as_dict()) == first[4]
        first[4].validate_source_grounding(bindings, view)
        assert repo.exists()
    finally:
        bindings.close()


def test_dirty_physical_checkout_cannot_change_exact_repoview_rebuild(tmp_path: Path) -> None:
    repo, _resource, view, intent, content_store, bindings = _fixture(tmp_path)
    try:
        first = _build_understanding(view, intent, content_store, bindings)
        provider = FallbackCodeFactProvider()
        facts_before = provider.analyze(view, ["src/service.py"])

        # Mutate the physical checkout without committing it.  Both M8b
        # projections must continue to use the immutable committed RepoView.
        (repo / "src" / "service.py").write_text(
            "def owner():\n    return 'WORKTREE'\n",
            encoding="utf-8",
        )
        assert view.read_text("src/service.py") == "def owner():\n    return 'A'\n"

        second = _build_understanding(view, intent, content_store, bindings)
        facts_after = provider.analyze(view, ["src/service.py"])

        assert second[4].understanding_id == first[4].understanding_id
        assert second[4].to_json_bytes() == first[4].to_json_bytes()
        assert facts_after.as_dict() == facts_before.as_dict()
        facts_after.validate_against(view)
    finally:
        bindings.close()


def test_new_commit_invalidates_old_understanding_and_old_provider_projection(tmp_path: Path) -> None:
    repo, resource, old_view, intent, content_store, bindings = _fixture(tmp_path)
    try:
        _evidence, _fragment, _claim, _coverage, old_understanding = _build_understanding(
            old_view,
            intent,
            content_store,
            bindings,
        )
        provider = FallbackCodeFactProvider()
        old_facts = provider.analyze(old_view, ["src/service.py"])
        old_facts.validate_against(old_view)

        (repo / "src" / "service.py").write_text(
            "def owner():\n    return 'B'\n",
            encoding="utf-8",
        )
        _git(repo, "add", "src/service.py")
        _git(repo, "commit", "-qm", "M8b changed source")
        new_view = resource.resolve_committed(
            "refs/heads/main",
            observed_at="2026-08-17T00:01:00Z",
        )
        assert new_view.view_id != old_view.view_id

        with pytest.raises(EngineeringIntelligenceError) as stale_understanding:
            old_understanding.validate_source_grounding(bindings, new_view)
        assert stale_understanding.value.code == "understanding_basis_mismatch"

        with pytest.raises(CodeIntelligenceError) as stale_provider:
            old_facts.validate_against(new_view)
        assert stale_provider.value.code == "stale_provider_result"

        new_evidence, _new_fragment, _new_claim, _new_coverage, new_understanding = _build_understanding(
            new_view,
            intent,
            content_store,
            bindings,
        )
        new_facts = provider.analyze(new_view, ["src/service.py"])
        new_facts.validate_against(new_view)

        assert new_evidence.repo_view.view_id == new_view.view_id
        assert new_understanding.repo_view.view_id == new_view.view_id
        assert new_understanding.understanding_id != old_understanding.understanding_id
        assert new_facts.repo_view.view_id == new_view.view_id
        assert new_facts.as_dict() != old_facts.as_dict()
    finally:
        bindings.close()


def test_m8b_projection_modules_have_no_lifecycle_or_repository_writer_authority() -> None:
    import bdb_vnext.code_intelligence as code_intelligence
    import bdb_vnext.engineering_intelligence as engineering_intelligence

    engineering_source = inspect.getsource(engineering_intelligence)
    code_source = inspect.getsource(code_intelligence)

    # These are projection/provider modules.  They may publish immutable
    # content/bindings, but they must not become Task/Work lifecycle or Git/FS
    # mutation authorities merely to satisfy M8b rebuildability.
    for source in (engineering_source, code_source):
        assert "WorkKernelStore" not in source
        assert "CanonicalVNextAdmissionAuthority" not in source
        assert "PreparedGitCasAdapter" not in source
        assert "CheckoutSyncAdapter" not in source
        assert "git update-ref" not in source.lower()
        assert "subprocess.run([\"git\", \"-c\"" not in source

    assert "Rebuildable exact-RepoView claim projection" in engineering_source
    assert "no writer, cache, daemon, lifecycle dependency, or legacy import" in code_source

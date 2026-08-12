from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from bdb_vnext.code_intelligence import (
    CodeFact,
    CodeIntelligenceError,
    FallbackCodeFactProvider,
    LspCodeFactProvider,
    ProviderUnavailableError,
    TreeSitterPythonProvider,
    project_provider_facts,
    provider_status,
)
from bdb_vnext.engineering_intelligence import IntentBasis
from bdb_vnext.repo_view import RepositoryResource
from bdb_vnext.composition import build_vnext_composition_manifest
from bdb_vnext.provider_root import VNextCompositionRoot
from bdb_shared.evidence import semantic_digest


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, object, object]:
    repo = tmp_path / "subject"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.name", "N5 Test")
    _git(repo, "config", "user.email", "n5@example.invalid")
    (repo / "src").mkdir()
    (repo / "src" / "service.py").write_text(
        "from src.helpers import helper\n\ndef run(value):\n    return helper(value)\n",
        encoding="utf-8",
    )
    (repo / "src" / "helpers.py").write_text("def helper(value):\n    return value\n", encoding="utf-8")
    (repo / "README.md").write_text("N5 fixture\n", encoding="utf-8")
    _git(repo, "add", "--all")
    _git(repo, "commit", "-qm", "N5 fixture")
    resource = RepositoryResource.from_path(repo, repository_id="n5-fixture")
    view = resource.resolve_committed("refs/heads/main", observed_at="2026-08-12T00:00:00Z")
    return repo, resource, view


def test_fallback_is_exactly_bound_and_roundtrips(tmp_path: Path) -> None:
    _repo_root, _resource, view = _repo(tmp_path)
    result = FallbackCodeFactProvider().analyze(view, ["src/service.py"])
    assert result.repo_view.view_id == view.view_id
    assert result.covered_dimensions == ("definitions", "imports")
    assert "semantic_definition_resolution" in result.gaps
    restored = [CodeFact.from_mapping(item.as_dict()) for item in result.facts]
    assert restored == list(result.facts)
    assert json.loads(json.dumps(result.as_dict()))["schema"] == "bdb-vnext-code-fact-provider-v1"


def test_stale_repo_view_and_missing_path_fail_closed(tmp_path: Path) -> None:
    repo, resource, view = _repo(tmp_path)
    result = FallbackCodeFactProvider().analyze(view, ["src/service.py"])
    (repo / "src" / "service.py").write_text("def changed():\n    return 1\n", encoding="utf-8")
    _git(repo, "add", "--all")
    _git(repo, "commit", "-qm", "changed")
    newer = resource.resolve_committed("refs/heads/main", observed_at="2026-08-12T00:01:00Z")
    with pytest.raises(CodeIntelligenceError, match="requested RepoView"):
        result.validate_against(newer)
    with pytest.raises(Exception) as missing:
        FallbackCodeFactProvider().analyze(view, ["src/missing.py"])
    assert getattr(missing.value, "code", None) in {"missing_path", "git_read_failed", "provider_path_missing", "unsupported_path"}


def test_tree_sitter_adds_syntax_and_reference_facts_when_available(tmp_path: Path) -> None:
    pytest.importorskip("tree_sitter")
    pytest.importorskip("tree_sitter_python")
    _repo_root, _resource, view = _repo(tmp_path)
    result = TreeSitterPythonProvider().analyze(view, ["src/service.py"])
    assert "syntax_structure" in result.covered_dimensions
    assert any(fact.kind == "call" for fact in result.facts)
    result.validate_against(view)


def test_tree_sitter_unsupported_language_fails_closed(tmp_path: Path) -> None:
    _repo_root, _resource, view = _repo(tmp_path)
    with pytest.raises(CodeIntelligenceError) as error:
        TreeSitterPythonProvider().analyze(view, ["README.md"])
    assert error.value.code == "unsupported_language"


def test_tree_sitter_parse_error_is_explicit_gap(tmp_path: Path) -> None:
    repo, resource, _view = _repo(tmp_path)
    (repo / "src" / "broken.py").write_text("def broken(:\n    return 1\n", encoding="utf-8")
    _git(repo, "add", "--all")
    _git(repo, "commit", "-qm", "malformed source")
    view = resource.resolve_committed("refs/heads/main", observed_at="2026-08-12T00:02:00Z")
    pytest.importorskip("tree_sitter")
    pytest.importorskip("tree_sitter_python")
    result = TreeSitterPythonProvider().analyze(view, ["src/broken.py"])
    assert "parse_error:src/broken.py" in result.gaps


def test_provider_treatment_improves_a_complex_reference_case(tmp_path: Path) -> None:
    pytest.importorskip("tree_sitter")
    pytest.importorskip("tree_sitter_python")
    _repo_root, _resource, view = _repo(tmp_path)
    baseline = FallbackCodeFactProvider().analyze(view, ["src/service.py"])
    treatment = TreeSitterPythonProvider().analyze(view, ["src/service.py"])
    question = ["definitions", "references"]
    baseline_understanding = project_provider_facts(
        view,
        IntentBasis("task:n5-complex", "r1", semantic_digest({"question": "which helper is called"})),
        baseline,
        requested_dimensions=question,
    )
    treatment_understanding = project_provider_facts(
        view,
        IntentBasis("task:n5-complex", "r1", semantic_digest({"question": "which helper is called"})),
        treatment,
        requested_dimensions=question,
    )
    assert "references" not in baseline_understanding.covered_dimensions
    assert "references" in treatment_understanding.covered_dimensions
    assert any(fact.name == "helper" and fact.kind == "call" for fact in treatment.facts)


def test_provider_projection_keeps_unresolved_dimensions_visible(tmp_path: Path) -> None:
    _repo_root, _resource, view = _repo(tmp_path)
    result = FallbackCodeFactProvider().analyze(view, ["src/service.py"])
    understanding = project_provider_facts(
        view,
        IntentBasis("task:n5", "r1", semantic_digest({"question": "definitions and references"})),
        result,
        requested_dimensions=["definitions", "references", "architecture_constraints"],
    )
    assert "definitions" in understanding.covered_dimensions
    assert {unknown.dimension for unknown in understanding.unknowns} >= {"references", "architecture_constraints"}
    assert all(claim.authority == "DERIVED" for claim in understanding.claims)


def test_lsp_is_explicit_read_only_and_unavailable_fails_closed(tmp_path: Path) -> None:
    _repo_root, _resource, view = _repo(tmp_path)
    with pytest.raises(ProviderUnavailableError):
        LspCodeFactProvider(("definitely-not-a-language-server",), server_identity="missing-v1").analyze(
            view, ["src/service.py"]
        )
    status = provider_status(tree_sitter=False, lsp=False)
    assert status["authority"] == "NONE"
    assert status["writer_enabled"] is False
    assert status["cache_authority"] is False


def test_provider_adapters_are_constructed_through_composition_root(tmp_path: Path) -> None:
    manifest = build_vnext_composition_manifest(
        source_commit="e674aa5ae6c23f3b45012ebb5d234ed939f27f04",
        runtime_root=tmp_path / "vnext-runtime",
        legacy_runtime_root=tmp_path / "legacy-runtime",
        forbidden_roots=[Path(__file__).resolve().parents[1]],
    )
    root = VNextCompositionRoot.from_manifest(manifest)
    assert root.fallback_code_fact_provider().provider_id.startswith("bdb-vnext.provider.")
    assert root.tree_sitter_code_fact_provider().provider_id.startswith("bdb-vnext.provider.")

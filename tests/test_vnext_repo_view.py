from __future__ import annotations

import inspect
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from bdb_vnext.repo_view import (
    COMMITTED_KIND,
    REPO_VIEW_SCHEMA,
    RepoViewError,
    RepositoryResource,
)


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _make_repo(root: Path, *, name: str = "fixture") -> tuple[Path, str, str]:
    repo = root / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.name", "M2a Test")
    _git(repo, "config", "user.email", "m2a@example.invalid")
    (repo / "README.md").write_text("first\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "--all")
    _git(repo, "commit", "-qm", "first")
    first = _git(repo, "rev-parse", "HEAD")
    (repo / "README.md").write_text("second\n", encoding="utf-8")
    (repo / "src" / "sample.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(repo, "add", "--all")
    _git(repo, "commit", "-qm", "second")
    second = _git(repo, "rev-parse", "HEAD")
    _git(repo, "reset", "--hard", first)
    return repo, first, second


def test_resource_and_committed_view_bind_exact_repository_commit_tree_and_provenance(tmp_path: Path) -> None:
    repo, first, _second = _make_repo(tmp_path)
    resource = RepositoryResource.from_path(repo, repository_id="m2a-fixture")

    view = resource.resolve_committed("refs/heads/main", observed_at="2026-08-09T00:00:00Z")
    assert view.kind == COMMITTED_KIND
    assert view.schema == REPO_VIEW_SCHEMA
    assert view.repository_id == "m2a-fixture"
    assert view.commit_oid == first
    assert view.tree_oid == view.provenance["tree_oid"]
    assert view.provenance["source_authority"] == "git-object-database"
    assert view.provenance["observed_ref"] == "refs/heads/main"
    assert view.to_dict()["repository"]["identity_digest"].startswith("sha256:")

    query = resource.query(view)
    assert query.commit_oid == first
    assert query.get_entry("README.md").is_regular_file
    assert query.read_text("README.md") == "first\n"
    assert query.read_text("src/sample.py") == "VALUE = 1\n"
    assert [entry.path for entry in query.list_entries()] == ["README.md", "src/sample.py"]


def test_committed_view_is_immutable_when_observed_ref_moves(tmp_path: Path) -> None:
    repo, first, second = _make_repo(tmp_path)
    resource = RepositoryResource.from_path(repo, repository_id="moving-ref")
    view = resource.resolve_committed("refs/heads/main", observed_at="2026-08-09T00:00:00Z")

    _git(repo, "reset", "--hard", second)
    freshness = view.freshness("refs/heads/main")
    assert freshness.is_stale
    assert freshness.current_commit_oid == second
    assert view.commit_oid == first
    assert view.read_text("README.md") == "first\n"

    current = resource.resolve_committed("refs/heads/main", observed_at="2026-08-09T00:01:00Z")
    assert current.commit_oid == second
    assert current.read_text("README.md") == "second\n"
    assert current.view_id != view.view_id


def test_wrong_repository_binding_is_rejected(tmp_path: Path) -> None:
    repo_a, _first_a, _second_a = _make_repo(tmp_path, name="repo-a")
    repo_b, _first_b, _second_b = _make_repo(tmp_path, name="repo-b")
    resource_a = RepositoryResource.from_path(repo_a, repository_id="same-logical-alias")
    resource_b = RepositoryResource.from_path(repo_b, repository_id="same-logical-alias")
    view_a = resource_a.resolve_committed("refs/heads/main", observed_at="2026-08-09T00:00:00Z")

    with pytest.raises(RepoViewError, match="binding does not match") as exc:
        resource_b.query(view_a)
    assert exc.value.code == "wrong_repository"


def test_mismatched_cached_view_descriptor_is_rejected(tmp_path: Path) -> None:
    repo, first, second = _make_repo(tmp_path)
    resource = RepositoryResource.from_path(repo, repository_id="cache-check")
    view = resource.resolve_committed("refs/heads/main", observed_at="2026-08-09T00:00:00Z")
    with pytest.raises(RepoViewError) as exc:
        replace(view, commit_oid=second)
    assert exc.value.code == "view_integrity_mismatch"
    assert view.commit_oid == first


def test_ref_is_explicit_and_unsafe_refs_fail_closed(tmp_path: Path) -> None:
    repo, _first, _second = _make_repo(tmp_path)
    resource = RepositoryResource.from_path(repo, repository_id="explicit-ref")
    assert inspect.signature(resource.resolve_committed).parameters["ref"].default is inspect.Parameter.empty
    with pytest.raises(RepoViewError) as exc:
        resource.resolve_committed("--help")
    assert exc.value.code == "invalid_ref"


def test_missing_path_and_non_text_reads_are_typed(tmp_path: Path) -> None:
    repo, _first, _second = _make_repo(tmp_path)
    (repo / "binary.bin").write_bytes(b"\xff\x00")
    _git(repo, "add", "--all")
    _git(repo, "commit", "-qm", "binary")
    resource = RepositoryResource.from_path(repo, repository_id="typed-errors")
    view = resource.resolve_committed("refs/heads/main", observed_at="2026-08-09T00:00:00Z")

    with pytest.raises(RepoViewError) as missing:
        view.entry("missing.txt")
    assert missing.value.code == "missing_path"
    with pytest.raises(RepoViewError) as binary:
        view.read_text("binary.bin")
    assert binary.value.code == "not_text"


def test_repo_view_schema_and_serialization_are_exact_and_side_effect_free(tmp_path: Path) -> None:
    repo, _first, _second = _make_repo(tmp_path)
    resource = RepositoryResource.from_path(repo, repository_id="serialization")
    view = resource.resolve_committed("refs/heads/main", observed_at="2026-08-09T00:00:00Z")
    document = view.to_dict()
    schema_path = Path(__file__).parents[1] / "schemas" / "bdb-vnext-repo-view-v1.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert document["schema"] == schema["properties"]["schema"]["const"]
    assert document["kind"] == schema["properties"]["kind"]["const"]
    assert document["view_id"].startswith("sha256:")
    assert view.to_json_bytes().endswith(b"\n")
    assert not (repo / ".bdb-vnext").exists()


def test_vnext_repo_view_has_no_legacy_import_dependency(tmp_path: Path) -> None:
    script = (
        "import sys; import bdb_vnext.repo_view; "
        "assert not any(name == 'bdb_bridge' or name.startswith('bdb_bridge.') for name in sys.modules)"
    )
    completed = subprocess.run([sys.executable, "-c", script], check=False, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr

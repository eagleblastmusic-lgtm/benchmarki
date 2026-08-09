from __future__ import annotations

import inspect
import json
import re
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import bdb_vnext.repo_view as repo_view_module
from bdb_vnext.repo_view import (
    COMMITTED_KIND,
    DEFAULT_MAX_BLOB_BYTES,
    REPO_VIEW_SCHEMA,
    RepoTreeEntry,
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


def _git_natural(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "--no-replace-objects", "-C", str(repo), *args],
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


def _load_schema() -> dict[str, object]:
    schema_path = Path(__file__).parents[1] / "schemas" / "bdb-vnext-repo-view-v1.schema.json"
    return json.loads(schema_path.read_text(encoding="utf-8"))


def _schema_accepts_git_oid(schema: dict[str, object], value: str) -> bool:
    definitions = schema["$defs"]
    assert isinstance(definitions, dict)
    return any(
        re.fullmatch(definitions[name]["pattern"], value) is not None
        for name in ("sha1Oid", "sha256Oid")
    )


def _assert_descriptor_matches_schema(document: dict[str, object], schema: dict[str, object]) -> None:
    required = schema["required"]
    properties = schema["properties"]
    assert isinstance(required, list)
    assert isinstance(properties, dict)
    assert set(document) == set(required) == set(properties)
    assert document["schema"] == properties["schema"]["const"]
    assert document["kind"] == properties["kind"]["const"]
    assert re.fullmatch(properties["view_id"]["pattern"], document["view_id"]) is not None

    repository = document["repository"]
    provenance = document["provenance"]
    assert isinstance(repository, dict)
    assert isinstance(provenance, dict)
    repository_schema = properties["repository"]
    provenance_schema = properties["provenance"]
    assert set(repository) == set(repository_schema["required"])
    assert set(provenance_schema["required"]) <= set(provenance) <= set(provenance_schema["properties"])
    assert provenance["source_authority"] == provenance_schema["properties"]["source_authority"]["const"]

    oid_definition = "sha1Oid" if repository["object_format"] == "sha1" else "sha256Oid"
    oid_pattern = schema["$defs"][oid_definition]["pattern"]
    oid_values = [
        document["commit_oid"],
        document["tree_oid"],
        provenance["commit_oid"],
        provenance["tree_oid"],
        *provenance["parent_oids"],
    ]
    assert all(isinstance(value, str) and re.fullmatch(oid_pattern, value) is not None for value in oid_values)


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


def test_two_refs_for_same_commit_and_tree_have_same_view_id(tmp_path: Path) -> None:
    repo, first, _second = _make_repo(tmp_path)
    _git(repo, "branch", "same-target", first)
    resource = RepositoryResource.from_path(repo, repository_id="same-tree-two-refs")

    main_view = resource.resolve_committed("refs/heads/main", observed_at="2026-08-09T00:00:00Z")
    alias_view = resource.resolve_committed("refs/heads/same-target", observed_at="2026-08-09T00:01:00Z")

    assert main_view.commit_oid == alias_view.commit_oid == first
    assert main_view.tree_oid == alias_view.tree_oid
    assert main_view.view_id == alias_view.view_id
    assert main_view.provenance["observed_ref"] == "refs/heads/main"
    assert alias_view.provenance["observed_ref"] == "refs/heads/same-target"


def test_wrong_repository_binding_is_rejected(tmp_path: Path) -> None:
    repo_a, _first_a, _second_a = _make_repo(tmp_path, name="repo-a")
    repo_b, _first_b, _second_b = _make_repo(tmp_path, name="repo-b")
    resource_a = RepositoryResource.from_path(repo_a, repository_id="same-logical-alias")
    resource_b = RepositoryResource.from_path(repo_b, repository_id="same-logical-alias")
    view_a = resource_a.resolve_committed("refs/heads/main", observed_at="2026-08-09T00:00:00Z")

    with pytest.raises(RepoViewError, match="binding does not match") as exc:
        resource_b.query(view_a)
    assert exc.value.code == "wrong_repository"


def test_forged_cached_entry_cannot_authorize_bytes_outside_bound_tree(tmp_path: Path) -> None:
    repo, first, second = _make_repo(tmp_path)
    resource = RepositoryResource.from_path(repo, repository_id="forged-cache")
    view = resource.resolve_committed("refs/heads/main", observed_at="2026-08-09T00:00:00Z")
    second_blob = _git(repo, "rev-parse", f"{second}:README.md")
    forged_entry = RepoTreeEntry(
        path="README.md",
        mode="100644",
        object_type="blob",
        object_oid=second_blob,
        size_bytes=len(b"second\n"),
        file_kind="regular",
    )

    forged_view = replace(view, entries=(forged_entry,))
    authoritative_entry = forged_view.entry("README.md")
    assert forged_view.entries == (forged_entry,)
    assert authoritative_entry.object_oid == _git(repo, "rev-parse", f"{first}:README.md")
    assert authoritative_entry.object_oid != second_blob
    assert forged_view.read_text("README.md") == "first\n"


def test_forged_commit_tree_binding_fails_closed_against_git_authority(tmp_path: Path) -> None:
    repo, _first, second = _make_repo(tmp_path)
    resource = RepositoryResource.from_path(repo, repository_id="forged-tree-binding")
    view = resource.resolve_committed("refs/heads/main", observed_at="2026-08-09T00:00:00Z")
    second_tree = _git(repo, "rev-parse", f"{second}^{{tree}}")
    forged_identity = view._identity_payload()
    forged_identity["tree_oid"] = second_tree
    forged_provenance = dict(view.provenance)
    forged_provenance["tree_oid"] = second_tree
    forged_view = replace(
        view,
        tree_oid=second_tree,
        view_id=repo_view_module.semantic_digest(forged_identity),
        provenance=forged_provenance,
    )

    with pytest.raises(RepoViewError) as mismatch:
        resource.query(forged_view)
    assert mismatch.value.code == "commit_tree_binding_mismatch"


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


def test_committed_read_is_independent_from_working_tree(tmp_path: Path) -> None:
    repo, _first, _second = _make_repo(tmp_path)
    resource = RepositoryResource.from_path(repo, repository_id="working-tree-independent")
    view = resource.resolve_committed("refs/heads/main", observed_at="2026-08-09T00:00:00Z")

    (repo / "README.md").write_text("mutable checkout bytes\n", encoding="utf-8")
    assert view.read_text("README.md") == "first\n"


def test_repo_view_ignores_git_replace_objects_for_resolution_and_later_reads(tmp_path: Path) -> None:
    repo, first, second = _make_repo(tmp_path, name="replace-ref")
    natural_tree = _git_natural(repo, "rev-parse", f"{first}^{{tree}}")
    replacement_tree = _git_natural(repo, "rev-parse", f"{second}^{{tree}}")
    _git(repo, "replace", first, second)

    # Sanity-check the real Git behavior that previously poisoned RepoView.
    assert _git(repo, "rev-parse", f"{first}^{{tree}}") == replacement_tree
    assert _git(repo, "show", f"{first}:README.md") == "second"

    resource = RepositoryResource.from_path(repo, repository_id="replace-ref")
    view = resource.resolve_committed("refs/heads/main", observed_at="2026-08-09T00:00:00Z")

    assert view.commit_oid == first
    assert view.tree_oid == natural_tree
    assert view.tree_oid != replacement_tree
    assert view.read_text("README.md") == "first\n"
    view._authoritative_reader()
    assert view.read_text("README.md") == "first\n"


def test_repo_view_strips_poisoned_git_authority_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bound, bound_first, _bound_second = _make_repo(tmp_path, name="bound-repository")
    foreign, _foreign_first, _foreign_second = _make_repo(tmp_path, name="foreign-repository")
    (foreign / "README.md").write_text("FOREIGN\n", encoding="utf-8")
    _git(foreign, "add", "README.md")
    _git(foreign, "commit", "-qm", "foreign-only")
    foreign_head = _git(foreign, "rev-parse", "HEAD")

    monkeypatch.setenv("GIT_DIR", str(foreign / ".git"))
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(foreign / ".git" / "objects"))

    poisoned = subprocess.run(
        ["git", "-C", str(bound), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert poisoned.stdout.strip() == foreign_head
    assert poisoned.stdout.strip() != bound_first

    resource = RepositoryResource.from_path(bound, repository_id="poisoned-environment")
    view = resource.resolve_committed("refs/heads/main", observed_at="2026-08-09T00:00:00Z")

    assert view.commit_oid == bound_first
    assert view.read_text("README.md") == "first\n"
    assert resource.current_commit("refs/heads/main") == bound_first


def test_repo_view_git_environment_contract_removes_inherited_git_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_DIR", "foreign-repository")
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", "foreign-objects")
    monkeypatch.setenv("GIT_NAMESPACE", "foreign-namespace")
    monkeypatch.setenv("GIT_REPLACE_REF_BASE", "refs/foreign-replace/")

    environment = repo_view_module._git_environment()

    assert environment["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert all(not key.upper().startswith("GIT_") or key == "GIT_NO_REPLACE_OBJECTS" for key in environment)
    assert "PATH" in environment


def test_resolve_is_lazy_and_blob_reads_are_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, _first, _second = _make_repo(tmp_path)
    resource = RepositoryResource.from_path(repo, repository_id="lazy-bounded")
    calls = 0
    original = repo_view_module._GitReader.list_tree

    def tracking_list_tree(*args: object, **kwargs: object) -> tuple[RepoTreeEntry, ...]:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(repo_view_module._GitReader, "list_tree", tracking_list_tree)
    view = resource.resolve_committed("refs/heads/main", observed_at="2026-08-09T00:00:00Z")
    assert calls == 0
    assert view.entries == ()

    with pytest.raises(RepoViewError) as too_large:
        view.read_bytes("README.md", max_bytes=5)
    assert too_large.value.code == "blob_too_large"
    assert view.read_bytes("README.md", max_bytes=6) == b"first\n"
    with pytest.raises(RepoViewError) as invalid_limit:
        view.read_bytes("README.md", max_bytes=DEFAULT_MAX_BLOB_BYTES + 1)
    assert invalid_limit.value.code == "invalid_read_limit"


def test_repo_view_schema_and_serialization_are_exact_and_side_effect_free(tmp_path: Path) -> None:
    repo, _first, _second = _make_repo(tmp_path)
    resource = RepositoryResource.from_path(repo, repository_id="serialization")
    view = resource.resolve_committed("refs/heads/main", observed_at="2026-08-09T00:00:00Z")
    document = view.to_dict()
    schema = _load_schema()

    _assert_descriptor_matches_schema(document, schema)
    assert "tree_entries" not in document
    assert view.to_json_bytes().endswith(b"\n")
    assert not (repo / ".bdb-vnext").exists()


def test_schema_rejects_git_oid_lengths_between_sha1_and_sha256() -> None:
    schema = _load_schema()
    assert _schema_accepts_git_oid(schema, "a" * 40)
    assert _schema_accepts_git_oid(schema, "a" * 64)
    assert all(not _schema_accepts_git_oid(schema, "a" * length) for length in range(41, 64))


def test_vnext_repo_view_has_no_legacy_import_dependency(tmp_path: Path) -> None:
    script = (
        "import sys; import bdb_vnext.repo_view; "
        "assert not any(name == 'bdb_bridge' or name.startswith('bdb_bridge.') for name in sys.modules)"
    )
    completed = subprocess.run([sys.executable, "-c", script], check=False, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr

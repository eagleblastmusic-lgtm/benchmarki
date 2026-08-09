from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import textwrap
from dataclasses import replace
from pathlib import Path

import pytest

from bdb_shared.evidence import canonical_json_bytes
from bdb_vnext.composition import (
    BROWSER_PROVIDER_ID,
    COMPOSITION_PROVIDER_ID,
    CONTROL_CENTER_PROVIDER_ID,
    CONTROL_PROVIDER_ID,
    NATIVE_PROVIDER_ID,
    REPO_VIEW_PROVIDER_ID,
    build_vnext_composition_manifest,
)
from bdb_vnext.provider_root import (
    CompositionDiagnosticProvider,
    PRODUCT_ID,
    PRODUCT_TOPOLOGY,
    PROVIDER_ROOT_SCHEMA,
    ProviderBinding,
    ProviderRootError,
    RepoViewProvider,
    ROOT_GENERATION,
    VNextCompositionRoot,
    default_provider_bindings,
)


ROOT = Path(__file__).resolve().parents[1]
BASIS = "e674aa5ae6c23f3b45012ebb5d234ed939f27f04"
RESERVED_IDS = {
    CONTROL_PROVIDER_ID,
    NATIVE_PROVIDER_ID,
    BROWSER_PROVIDER_ID,
    CONTROL_CENTER_PROVIDER_ID,
}


def _manifest(tmp_path: Path) -> dict[str, object]:
    return build_vnext_composition_manifest(
        source_commit=BASIS,
        runtime_root=tmp_path / "vnext-runtime",
        legacy_runtime_root=tmp_path / "legacy-runtime",
        forbidden_roots=[ROOT],
    )


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _make_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo-fixture"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.name", "M1c Test")
    _git(repo, "config", "user.email", "m1c@example.invalid")
    (repo / "README.md").write_text("committed-v1\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "value.txt").write_text("exact-tree\n", encoding="utf-8")
    _git(repo, "add", "--all")
    _git(repo, "commit", "-qm", "M1c fixture")
    return repo, _git(repo, "rev-parse", "HEAD")


def _failure(callable_object: object, *args: object, **kwargs: object) -> ProviderRootError:
    assert callable(callable_object)
    with pytest.raises(ProviderRootError) as failure:
        callable_object(*args, **kwargs)  # type: ignore[operator]
    return failure.value


def test_root_is_explicit_deterministic_and_build_only(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    before = set(tmp_path.iterdir())

    first = VNextCompositionRoot.from_manifest(manifest)
    second = VNextCompositionRoot.from_manifest(manifest)

    assert set(tmp_path.iterdir()) == before
    assert first.fingerprint == second.fingerprint
    assert first.status() == second.status()
    assert first.status() == {
        "schema": PROVIDER_ROOT_SCHEMA,
        "generation": ROOT_GENERATION,
        "product_id": PRODUCT_ID,
        "product_topology": PRODUCT_TOPOLOGY,
        "manifest": first.manifest_identity,
        "providers": first.status()["providers"],
        "provider_ids": sorted(first.provider_ids),
        "bound_provider_ids": sorted([COMPOSITION_PROVIDER_ID, REPO_VIEW_PROVIDER_ID]),
        "unavailable_provider_ids": [],
        "reserved_provider_ids": sorted(RESERVED_IDS),
        "runtime_state": "OFF",
        "writer_state": "OFF",
        "activation_state": "OFF",
        "writer_enabled": False,
        "fingerprint": first.fingerprint,
    }
    assert set(first.provider_ids) == {
        COMPOSITION_PROVIDER_ID,
        REPO_VIEW_PROVIDER_ID,
        *RESERVED_IDS,
    }
    assert isinstance(first.composition_diagnostic(), CompositionDiagnosticProvider)
    assert isinstance(first.repo_view_provider(), RepoViewProvider)
    assert "0x" not in repr(first)
    assert "bdb_bridge" not in json.dumps(first.status())


def test_root_routes_caller_to_exact_committed_repo_view(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    repo, commit_oid = _make_repo(tmp_path)
    root = VNextCompositionRoot.from_manifest(manifest)

    resource = root.repository_resource(repo, repository_id="m1c-fixture")
    view = root.resolve_committed(resource, "refs/heads/main", observed_at="2026-08-09T00:00:00Z")
    query = root.query(resource, view)

    assert view.commit_oid == commit_oid
    assert view.tree_oid == view.provenance["tree_oid"]
    assert view.kind == "COMMITTED"
    assert query.repository_id == "m1c-fixture"
    assert query.commit_oid == commit_oid
    assert query.read_text("README.md") == "committed-v1\n"
    assert query.read_text("src/value.txt") == "exact-tree\n"
    assert root.provider(REPO_VIEW_PROVIDER_ID) is root.repo_view_provider()


@pytest.mark.parametrize("provider_id", sorted(RESERVED_IDS))
def test_reserved_provider_requests_fail_closed(tmp_path: Path, provider_id: str) -> None:
    root = VNextCompositionRoot.from_manifest(_manifest(tmp_path))

    failure = _failure(root.provider, provider_id)

    assert failure.code == "provider_unavailable"
    assert failure.details == {"state": "RESERVED"}


def test_provider_binding_validation_is_fail_closed(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    defaults = list(default_provider_bindings(manifest))
    repo_index = next(index for index, item in enumerate(defaults) if item.provider_id == REPO_VIEW_PROVIDER_ID)

    duplicate = defaults + [defaults[0]]
    assert _failure(VNextCompositionRoot.from_manifest, manifest, bindings=duplicate).code == "duplicate_provider_id"

    unknown = [replace(defaults[0], provider_id="devmaster.bdb.vnext.unknown")]
    unknown.extend(defaults[1:])
    assert _failure(VNextCompositionRoot.from_manifest, manifest, bindings=unknown).code == "unknown_provider"

    wrong_generation = [replace(defaults[0], generation="bdb-legacy-g1")]
    wrong_generation.extend(defaults[1:])
    assert (
        _failure(VNextCompositionRoot.from_manifest, manifest, bindings=wrong_generation).code
        == "provider_generation_mismatch"
    )

    wrong_id = [replace(defaults[1], provider_id=CONTROL_PROVIDER_ID)]
    wrong_id.extend(defaults[2:])
    assert (
        _failure(VNextCompositionRoot.from_manifest, manifest, bindings=wrong_id).code
        == "malformed_provider_declaration"
    )

    wrong_implementation = [
        replace(defaults[0], implementation=RepoViewProvider()),
        *defaults[1:],
    ]
    assert (
        _failure(VNextCompositionRoot.from_manifest, manifest, bindings=wrong_implementation).code
        == "provider_binding_mismatch"
    )

    missing_implementation = [
        replace(defaults[0], implementation=None),
        *defaults[1:],
    ]
    assert (
        _failure(VNextCompositionRoot.from_manifest, manifest, bindings=missing_implementation).code
        == "provider_binding_mismatch"
    )

    repo_view_disabled = [
        *defaults[:repo_index],
        replace(defaults[repo_index], state="RESERVED", implementation=None),
        *defaults[repo_index + 1 :],
    ]
    assert (
        _failure(VNextCompositionRoot.from_manifest, manifest, bindings=repo_view_disabled).code
        == "provider_binding_mismatch"
    )

    wrong_repo_generation = [
        *defaults[:repo_index],
        replace(defaults[repo_index], implementation=RepoViewProvider(generation="bdb-legacy-g1")),
        *defaults[repo_index + 1 :],
    ]
    assert (
        _failure(VNextCompositionRoot.from_manifest, manifest, bindings=wrong_repo_generation).code
        == "provider_generation_mismatch"
    )

    diagnostic = defaults[0].implementation
    assert isinstance(diagnostic, CompositionDiagnosticProvider)
    wrong_diagnostic_identity = [
        replace(
            defaults[0],
            implementation=replace(diagnostic, manifest_digest="sha256:" + "0" * 64),
        ),
        *defaults[1:],
    ]
    assert (
        _failure(VNextCompositionRoot.from_manifest, manifest, bindings=wrong_diagnostic_identity).code
        == "provider_binding_mismatch"
    )

    malformed = [replace(defaults[0], state="BOUND", component_id="")]
    malformed.extend(defaults[1:])
    assert (
        _failure(VNextCompositionRoot.from_manifest, manifest, bindings=malformed).code
        == "malformed_provider_declaration"
    )

    assert (
        _failure(
            VNextCompositionRoot.from_manifest,
            manifest,
            expected_manifest_digest="sha256:" + "0" * 64,
        ).code
        == "manifest_identity_mismatch"
    )
    assert (
        _failure(
            VNextCompositionRoot.from_manifest,
            manifest,
            expected_source_commit="0" * 40,
        ).code
        == "manifest_identity_mismatch"
    )

    tampered_manifest = dict(manifest)
    tampered_manifest["semantic_digest"] = "sha256:" + "0" * 64
    assert _failure(VNextCompositionRoot.from_manifest, tampered_manifest).code == "manifest_identity_mismatch"


def test_import_order_is_independent_in_fresh_subprocesses(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_bytes(canonical_json_bytes(_manifest(tmp_path)))
    repo, _commit_oid = _make_repo(tmp_path)
    script = textwrap.dedent(
        """
        import json
        import sys
        from pathlib import Path

        sequence = sys.argv[1]
        if sequence == "composition-repo-root":
            import bdb_vnext.composition
            import bdb_vnext.repo_view
            import bdb_vnext.provider_root as provider_root
        elif sequence == "repo-root-composition":
            import bdb_vnext.repo_view
            import bdb_vnext.provider_root as provider_root
            import bdb_vnext.composition
        else:
            raise AssertionError(sequence)

        manifest = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        root = provider_root.VNextCompositionRoot.from_manifest(manifest)
        resource = root.repository_resource(Path(sys.argv[3]), repository_id="subprocess-fixture")
        view = root.resolve_committed(resource, "refs/heads/main", observed_at="2026-08-09T00:00:00Z")
        result = {
            "fingerprint": root.fingerprint,
            "status": root.status(),
            "bound_provider_ids": root.bound_provider_ids,
            "commit_oid": view.commit_oid,
            "tree_oid": view.tree_oid,
            "readme": root.query(resource, view).read_text("README.md"),
            "legacy_loaded": any(
                name == "bdb_bridge" or name.startswith("bdb_bridge.")
                for name in sys.modules
            ),
            "experiments_loaded": any(
                name.startswith("bdb_vnext.x1_sqlite_experiment")
                or name.startswith("bdb_vnext.x2_typed_content_experiment")
                for name in sys.modules
            ),
        }
        assert result["legacy_loaded"] is False
        assert result["experiments_loaded"] is False
        print(json.dumps(result, sort_keys=True))
        """
    )
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    results: list[dict[str, object]] = []
    for sequence in ("composition-repo-root", "repo-root-composition"):
        completed = subprocess.run(
            [sys.executable, "-c", script, sequence, str(manifest_path), str(repo)],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert completed.returncode == 0, completed.stderr
        results.append(json.loads(completed.stdout))

    assert results[0] == results[1]
    assert results[0]["bound_provider_ids"] == sorted([COMPOSITION_PROVIDER_ID, REPO_VIEW_PROVIDER_ID])


def test_import_and_constructor_are_side_effect_free_and_legacy_free(tmp_path: Path) -> None:
    before = set(tmp_path.iterdir())
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import bdb_vnext; import bdb_vnext.provider_root; "
                "assert not any(name == 'bdb_bridge' or name.startswith('bdb_bridge.') for name in sys.modules); "
                "assert not any(name.startswith('bdb_vnext.x1_sqlite_experiment') or "
                "name.startswith('bdb_vnext.x2_typed_content_experiment') for name in sys.modules)"
            ),
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    root = VNextCompositionRoot.from_manifest(_manifest(tmp_path))
    assert root.fingerprint
    assert set(tmp_path.iterdir()) == before
    assert not (tmp_path / "vnext-runtime").exists()
    assert not (tmp_path / "legacy-runtime").exists()


def test_provider_root_source_has_no_dynamic_or_legacy_composition_mechanism() -> None:
    source_path = ROOT / "bdb_vnext" / "provider_root.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    imported_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_from = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert "importlib" not in imported_names
    assert all(not name.startswith("bdb_bridge") for name in imported_names | imported_from)
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "setattr"
        for node in ast.walk(tree)
    )

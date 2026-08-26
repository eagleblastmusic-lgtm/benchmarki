"""Focused NX-037 qualification for exact-keyed shared resources."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable

import pytest

from bdb_vnext import shared_resources as shared


ROOT = Path(__file__).resolve().parents[1]
NX037_GATE_FIELDS = {
    "SHARED_RESOURCE_POLICY_VERSION_EXPLICIT",
    "SHARED_LAYOUT_VERSION_EXPLICIT",
    "EXISTING_PROJECTS_MIGRATED",
    "PREMIUM_CALCULATOR_MOVED",
    "NODE_MODULES_SHARED",
    "PROJECT_VENV_SHARED",
    "CARGO_REGISTRY_CACHE_SHARED_ALLOWED",
    "CARGO_GIT_CACHE_SHARED_ALLOWED",
    "INEXACT_SHARED_RESOURCE_REUSE",
    "CROSS_PROJECT_MUTABLE_STATE_COLLISIONS",
    "WRITE_ESCAPE_ACCEPTED",
    "REPARSE_ESCAPE_ACCEPTED",
    "SYMLINK_ESCAPE_EFFECTS",
    "ACL_DENIAL_SECURITY_BYPASSES",
    "AUTOMATIC_ELEVATION_ATTEMPTS",
    "FOREIGN_SHARED_LOCK_RELEASE_ACCEPTED",
    "CONCURRENT_SHARED_OPERATIONS",
    "TORN_SHARED_ENTRIES",
    "CROSS_PROJECT_COLLISIONS",
    "POISONED_SHARED_ENTRY_ACCEPTED",
    "PARTIAL_SHARED_ENTRY_ACCEPTED",
    "QUOTA_BYPASS_ACCEPTED",
    "ACTIVE_SHARED_RESOURCE_COLLECTED",
    "REFERENCED_SHARED_RESOURCE_COLLECTED",
    "EXACT_KEY_REUSE_DIVERGENCES",
    "DIFFERENT_KEY_ALIASING",
    "SHARED_RESOURCE_BECOMES_READINESS_AUTHORITY",
    "SECURITY_FIXTURES",
    "SECURITY_VERIFIER_DIVERGENCES",
    "HARDCODED_GATE_RESULT_FIELDS",
    "NO_HARDCODED_GATE_RESULTS",
    "SOURCE_HEAD",
    "SOURCE_TREE",
    "WORKTREE_CLEAN",
    "SOURCE_BOUND_MACHINE_GATE",
    "NX037_STATUS",
}


def _sha(value: bytes | str) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _key(
    payload: bytes,
    *,
    ecosystem: str = "npm",
    artifact_class: str = "download_cache",
    tool_version: str = "22.1.0",
) -> shared.SharedResourceKey:
    return shared.SharedResourceKey(
        ecosystem=ecosystem,
        artifact_class=artifact_class,
        platform="windows_amd64",
        tool_version=tool_version,
        content_digest=_sha(payload),
    )


def _store(root: Path, *, quota: shared.QuotaPolicy | None = None) -> shared.SharedResourceStore:
    return shared.SharedResourceStore(root, quota=quota or shared.QuotaPolicy())


def _schema(name: str) -> dict[str, Any]:
    return json.loads((ROOT / "schemas" / name).read_text(encoding="utf-8"))


def _git(*args: str) -> tuple[int, str]:
    completed = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode, completed.stdout.strip()


def _hardcoded_gate_fields() -> list[str]:
    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run_nx037_machine_gate"
    )
    hardcoded: set[str] = set()
    for node in ast.walk(function):
        targets: Iterable[ast.expr] = ()
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = (node.target,)
            value = node.value
        if value is None or not isinstance(value, ast.Constant):
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id in NX037_GATE_FIELDS:
                hardcoded.add(target.id)
    return sorted(hardcoded)


def _make_symlink(link: Path, target: Path) -> bool:
    try:
        link.symlink_to(target, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        # Directory junctions are Windows reparse points and do not require
        # the user-level symlink privilege on hosts where ordinary symlinks
        # are disabled.
        completed = subprocess.run(
            ["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.returncode == 0 and link.exists()


def test_shared_contracts_are_versioned_closed_and_round_trip(tmp_path: Path) -> None:
    policy_schema = _schema("bdb-vnext-shared-resource-policy-v1.schema.json")
    layout_schema = _schema("bdb-vnext-shared-layout-v1.schema.json")
    result_schema = _schema("bdb-vnext-shared-resource-result-v1.schema.json")
    assert policy_schema["$id"] == shared.SHARED_RESOURCE_POLICY_SCHEMA
    assert layout_schema["$id"] == shared.SHARED_LAYOUT_SCHEMA
    assert result_schema["$id"] == shared.SHARED_RESOURCE_RESULT_SCHEMA
    assert all(item["additionalProperties"] is False for item in (policy_schema, layout_schema, result_schema))
    assert policy_schema["properties"]["version"]["const"] == shared.SHARED_RESOURCE_POLICY_VERSION
    assert layout_schema["properties"]["version"]["const"] == shared.SHARED_LAYOUT_VERSION
    assert result_schema["properties"]["version"]["const"] == shared.SHARED_RESOURCE_RESULT_VERSION

    layout = shared.SharedLayoutContract()
    assert shared.SharedLayoutContract.from_dict(layout.to_dict()) == layout
    policy = shared.DEFAULT_SHARED_RESOURCE_POLICY
    assert shared.SharedResourcePolicy.from_dict(policy.to_dict()) == policy
    key = _key(b"round-trip")
    assert shared.SharedResourceKey.from_dict(key.to_dict()) == key

    store = _store(tmp_path / "shared")
    result = store.publish(key, b"round-trip", project_id="project:one")
    assert shared.SharedResourceResult.from_dict(result.to_dict()) == result
    manifest = shared.SharedResourceManifest.from_dict(
        json.loads(store.paths_for(key).manifest.read_text(encoding="utf-8"))
    )
    assert manifest.key == key
    assert manifest.manifest_digest == result.manifest_digest


def test_explicit_matrix_keeps_project_outputs_local_and_does_not_migrate_projects(tmp_path: Path) -> None:
    store = _store(tmp_path / "shared")
    node_key = _key(b"node-modules", ecosystem="node", artifact_class="node_modules")
    venv_key = _key(b"python-venv", ecosystem="python", artifact_class="venv")
    target_key = _key(b"cargo-target", ecosystem="cargo", artifact_class="target")
    for key in (node_key, venv_key, target_key):
        result = store.publish(key, b"project-output", project_id="project:local")
        assert result.status is shared.SharedResourceStatus.BLOCKED
        assert not store.paths_for(key).directory.exists()
    assert not store.migration_events
    assert not (tmp_path / "shared" / "Premium Calculator").exists()

    assert not store.rule_for(node_key).shared_allowed
    assert store.rule_for(node_key).project_local_required
    assert not store.rule_for(venv_key).shared_allowed
    assert not store.rule_for(target_key).shared_allowed
    assert store.rule_for(_key(b"npm-cache")).mutability is shared.MutabilityClass.APPEND_ONLY_CACHE
    assert store.rule_for(_key(b"wheel-cache", ecosystem="python", artifact_class="wheel_cache")).shared_allowed
    assert store.rule_for(_key(b"registry", ecosystem="cargo", artifact_class="registry_cache")).shared_allowed
    assert store.rule_for(_key(b"git", ecosystem="cargo", artifact_class="git_cache")).shared_allowed
    assert store.rule_for(_key(b"toolchain", ecosystem="toolchain", artifact_class="sealed_toolchain")).mutability is shared.MutabilityClass.SEALED_IMMUTABLE


def test_exact_key_reuse_is_shared_but_different_keys_cannot_alias(tmp_path: Path) -> None:
    store = _store(tmp_path / "shared")
    payload = b"same exact immutable cache entry"
    key = _key(payload)
    first = store.publish(key, payload, project_id="project:one")
    second = store.publish(key, payload, project_id="project:two")
    assert first.status is shared.SharedResourceStatus.PUBLISHED
    assert second.status is shared.SharedResourceStatus.EXACT_HIT
    assert second.exact_hit
    assert store.lookup(key).valid
    assert not store.lookup(key).readiness_authority

    other_payload = b"different exact immutable cache entry"
    other_key = _key(other_payload)
    assert store.paths_for(key).directory != store.paths_for(other_key).directory
    assert not store.lookup(other_key).hit
    other = store.publish(other_key, other_payload, project_id="project:two")
    assert other.accepted
    assert store.lookup(other_key).payload == other_payload


def test_concurrent_exact_key_access_publishes_only_valid_content(tmp_path: Path) -> None:
    store = _store(tmp_path / "shared")
    payload = b"concurrent exact-key payload"
    key = _key(payload)

    def publish_once(index: int) -> shared.SharedResourceResult:
        return store.publish(key, payload, project_id=f"project:{index}")

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(publish_once, range(8)))
    assert len(results) == 8
    assert all(item.accepted for item in results)
    assert sum(item.status is shared.SharedResourceStatus.PUBLISHED for item in results) == 1
    assert sum(item.status is shared.SharedResourceStatus.EXACT_HIT for item in results) == 7
    lookup = store.lookup(key)
    assert lookup.hit and lookup.valid and lookup.payload == payload
    assert not list(store.paths_for(key).directory.glob("*.partial-*"))


def test_poisoned_and_partial_entries_are_never_valid_hits(tmp_path: Path) -> None:
    store = _store(tmp_path / "shared")
    payload = b"valid payload"
    key = _key(payload)
    store.publish(key, payload, project_id="project:one")
    paths = store.paths_for(key)
    paths.payload.write_bytes(b"poisoned payload")
    poisoned = store.lookup(key)
    assert poisoned.status is shared.SharedResourceStatus.POISONED
    assert not poisoned.hit
    repaired = store.publish(key, payload, project_id="project:one")
    assert repaired.status is shared.SharedResourceStatus.REBUILT
    assert store.lookup(key).hit

    partial_payload = b"partial publication"
    partial_key = _key(partial_payload)
    with pytest.raises(shared.SharedPublicationInterrupted):
        store.publish(partial_key, partial_payload, project_id="project:one", fault="after_payload")
    partial_lookup = store.lookup(partial_key)
    assert not partial_lookup.hit
    assert partial_lookup.status is shared.SharedResourceStatus.MISSING
    assert store.publish(partial_key, partial_payload, project_id="project:one").accepted
    assert store.lookup(partial_key).hit


def test_path_confinement_rejects_traversal_absolute_drive_and_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "shared"
    store = _store(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    attempts = ("../outside.bin", r"C:\outside.bin", r"D:\other.bin", "Cache/../outside.bin")
    for attempt in attempts:
        with pytest.raises(shared.SharedResourceError):
            store.probe_write(attempt)
    assert not (tmp_path / "outside.bin").exists()

    link = root / "Cache" / "escape"
    available = _make_symlink(link, outside)
    assert available, "the required symlink/reparse security fixture is unavailable"
    with pytest.raises(shared.SharedResourceError):
        store.probe_write("Cache/escape/escaped.bin")
    assert not (outside / "escaped.bin").exists()


def test_acl_denial_foreign_lock_quota_and_gc_are_safety_preserving(tmp_path: Path) -> None:
    store = _store(tmp_path / "shared")
    acl_payload = b"acl denied"
    acl_key = _key(acl_payload)
    denied = store.publish(acl_key, acl_payload, project_id="project:one", allow_write=False)
    assert denied.status is shared.SharedResourceStatus.ACL_DENIED
    assert not store.paths_for(acl_key).directory.exists()

    lock_key = _key(b"foreign lock")
    lock_paths = store.paths_for(lock_key)
    lock_paths.directory.mkdir(parents=True)
    owner = shared.OwnedSharedLock(lock_paths.lock, owner_token="owner-a")
    foreign = shared.OwnedSharedLock(lock_paths.lock, owner_token="owner-b")
    assert owner.acquire(timeout_seconds=0)
    assert not foreign.release()
    assert lock_paths.lock.exists()
    assert owner.release()
    assert not lock_paths.lock.exists()

    quota = shared.QuotaPolicy(limits=(("Cache", 4), ("Environment", 100), ("PackageStores", 100), ("Toolchains", 100)))
    quota_store = _store(tmp_path / "quota", quota=quota)
    quota_key = _key(b"1234")
    other_key = _key(b"5678")
    assert quota_store.publish(quota_key, b"1234", project_id="project:one").accepted
    quota_result = quota_store.publish(other_key, b"5678", project_id="project:two")
    assert quota_result.status is shared.SharedResourceStatus.QUOTA_BLOCKED
    assert quota_store.lookup(quota_key).hit
    assert not quota_store.paths_for(other_key).manifest.exists()

    gc_store = _store(tmp_path / "gc")
    active_key = _key(b"active")
    referenced_key = _key(b"referenced")
    collectable_key = _key(b"collectable")
    for key in (active_key, referenced_key, collectable_key):
        assert gc_store.publish(key, key.content_digest.encode("ascii"), project_id="project:gc").status is shared.SharedResourceStatus.INVALID
    # Publish using payloads that match the keys; the preceding invalid writes
    # prove digest mismatch is rejected without creating an entry.
    for payload, key in ((b"active", active_key), (b"referenced", referenced_key), (b"collectable", collectable_key)):
        assert gc_store.publish(key, payload, project_id="project:gc").accepted
    assert gc_store.mark_active(active_key, owner_token="lease-owner")
    collected = gc_store.garbage_collect(referenced_key_digests=(referenced_key.key_digest,))
    assert collectable_key.key_digest in collected
    assert active_key.key_digest not in collected
    assert referenced_key.key_digest not in collected
    assert gc_store.paths_for(active_key).manifest.exists()
    assert gc_store.paths_for(referenced_key).manifest.exists()
    assert gc_store.release_active(active_key, owner_token="lease-owner")


def run_nx037_machine_gate() -> dict[str, Any]:
    head_code, head = _git("rev-parse", "HEAD")
    tree_code, tree = _git("rev-parse", "HEAD^{tree}")
    with tempfile.TemporaryDirectory(prefix="bdb-vnext-nx037-") as temporary:
        base = Path(temporary)
        store = _store(base / "shared")
        payload = b"gate exact cache payload"
        key = _key(payload)
        first = store.publish(key, payload, project_id="project:one")
        second = store.publish(key, payload, project_id="project:two")
        exact_lookup = store.lookup(key)

        with ThreadPoolExecutor(max_workers=6) as executor:
            concurrent_results = list(
                executor.map(
                    lambda index: store.publish(key, payload, project_id=f"project:concurrent:{index}"),
                    range(6),
                )
            )

        different_key = _key(b"gate different payload")
        different_paths = store.paths_for(key).directory == store.paths_for(different_key).directory
        different_lookup_before = store.lookup(different_key)
        different_result = store.publish(different_key, b"gate different payload", project_id="project:two")

        node_key = _key(b"gate node modules", ecosystem="node", artifact_class="node_modules")
        venv_key = _key(b"gate venv", ecosystem="python", artifact_class="venv")
        target_key = _key(b"gate target", ecosystem="cargo", artifact_class="target")
        local_results = tuple(
            store.publish(local_key, b"project-local-state", project_id="project:local")
            for local_key in (node_key, venv_key, target_key)
        )

        poisoned_key = _key(b"gate poisoned")
        store.publish(poisoned_key, b"gate poisoned", project_id="project:poison")
        poisoned_paths = store.paths_for(poisoned_key)
        poisoned_paths.payload.write_bytes(b"wrong bytes")
        poisoned_lookup = store.lookup(poisoned_key)

        partial_key = _key(b"gate partial")
        try:
            store.publish(partial_key, b"gate partial", project_id="project:partial", fault="after_payload")
        except shared.SharedPublicationInterrupted:
            pass
        partial_lookup = store.lookup(partial_key)

        root = base / "shared-security"
        security_store = _store(root)
        outside = base / "outside"
        outside.mkdir()
        traversal_attempts = ("../outside.bin", r"C:\outside.bin", r"D:\other.bin", "Cache/../outside.bin")
        traversal_results: list[bool] = []
        for attempt in traversal_attempts:
            try:
                security_store.probe_write(attempt)
                traversal_results.append(True)
            except shared.SharedResourceError:
                traversal_results.append(False)
        link = root / "Cache" / "escape"
        symlink_available = _make_symlink(link, outside)
        try:
            security_store.probe_write("Cache/escape/escaped.bin")
            reparse_accepted = True
        except shared.SharedResourceError:
            reparse_accepted = False
        symlink_effect = (outside / "escaped.bin").exists()

        acl_key = _key(b"gate acl")
        acl_result = security_store.publish(acl_key, b"gate acl", project_id="project:acl", allow_write=False)

        lock_key = _key(b"gate foreign lock")
        lock_paths = security_store.paths_for(lock_key)
        lock_paths.directory.mkdir(parents=True)
        owner = shared.OwnedSharedLock(lock_paths.lock, owner_token="gate-owner")
        foreign = shared.OwnedSharedLock(lock_paths.lock, owner_token="gate-foreign")
        owner.acquire(timeout_seconds=0)
        foreign_release = foreign.release()
        owner.release()

        quota_policy = shared.QuotaPolicy(limits=(("Cache", 4), ("Environment", 100), ("PackageStores", 100), ("Toolchains", 100)))
        quota_store = _store(base / "quota", quota=quota_policy)
        quota_key = _key(b"g001")
        quota_other_key = _key(b"g002")
        quota_first = quota_store.publish(quota_key, b"g001", project_id="project:quota")
        quota_second = quota_store.publish(quota_other_key, b"g002", project_id="project:quota")
        quota_safe = quota_store.lookup(quota_key).hit and quota_second.status is shared.SharedResourceStatus.QUOTA_BLOCKED

        gc_store = _store(base / "gc")
        active_key = _key(b"gate-active")
        referenced_key = _key(b"gate-referenced")
        collectable_key = _key(b"gate-collectable")
        for payload_value, gc_key in ((b"gate-active", active_key), (b"gate-referenced", referenced_key), (b"gate-collectable", collectable_key)):
            gc_store.publish(gc_key, payload_value, project_id="project:gc")
        gc_store.mark_active(active_key, owner_token="gate-lease")
        collected = gc_store.garbage_collect(referenced_key_digests=(referenced_key.key_digest,))
        active_collected = active_key.key_digest in collected
        referenced_collected = referenced_key.key_digest in collected

        local_paths = tuple(store.paths_for(local_key).directory.exists() for local_key in (node_key, venv_key, target_key))
        security_observations = (
            not any(traversal_results),
            symlink_available,
            not reparse_accepted,
            not symlink_effect,
            acl_result.status is shared.SharedResourceStatus.ACL_DENIED,
            not acl_result.accepted and not security_store.paths_for(acl_key).directory.exists(),
            not foreign_release,
            quota_safe,
            not poisoned_lookup.hit,
            not partial_lookup.hit,
            not any(local_paths),
            not active_collected,
            not referenced_collected,
        )
        security_fixture_count = len(security_observations)
        security_divergences = sum(int(not item) for item in security_observations)

        node_rule = store.rule_for(node_key)
        venv_rule = store.rule_for(venv_key)
        cargo_registry_rule = store.rule_for(_key(b"registry", ecosystem="cargo", artifact_class="registry_cache"))
        cargo_git_rule = store.rule_for(_key(b"git", ecosystem="cargo", artifact_class="git_cache"))
        write_escape = any(traversal_results)
        local_collision = sum(int(item.accepted) for item in local_results)
        inexact_reuse = int(different_lookup_before.hit)
        different_aliasing = int(different_paths or (different_result.resource_relative_path == first.resource_relative_path))
        quota_bypass = not quota_safe
        exact_reuse_divergences = int(
            second.status is not shared.SharedResourceStatus.EXACT_HIT
            or second.manifest_digest != first.manifest_digest
        )
        shared_readiness_authority = exact_lookup.readiness_authority
        existing_migrations = len(store.migration_events)
        premium_moved = any("Premium Calculator" in item.values() for item in store.migration_events)
        source_status_code, source_status = _git("status", "--porcelain")
        diff_code, _ = _git("diff", "--check")
        clean = source_status_code == 0 and source_status == "" and diff_code == 0
        hardcoded_fields = _hardcoded_gate_fields()
        no_hardcoded = not hardcoded_fields
        source_bound = "PASS" if head_code == 0 and tree_code == 0 and clean and no_hardcoded else "FAIL"
        all_pass = all(
            (
                bool(shared.SHARED_RESOURCE_POLICY_VERSION_EXPLICIT),
                bool(shared.SHARED_LAYOUT_VERSION_EXPLICIT),
                existing_migrations == 0,
                not premium_moved,
                not node_rule.shared_allowed,
                not venv_rule.shared_allowed,
                cargo_registry_rule.shared_allowed,
                cargo_git_rule.shared_allowed,
                inexact_reuse == 0,
                local_collision == 0,
                not write_escape,
                not reparse_accepted,
                not symlink_effect,
                int(acl_result.accepted or security_store.paths_for(acl_key).directory.exists()) == 0,
                security_store.automatic_elevation_attempts == 0,
                not foreign_release,
                len(concurrent_results) >= 6,
                int(not store.lookup(partial_key).hit) == 1,
                int(different_paths) == 0,
                not poisoned_lookup.hit,
                not partial_lookup.hit,
                not quota_bypass,
                not active_collected,
                not referenced_collected,
                exact_reuse_divergences == 0,
                different_aliasing == 0,
                not shared_readiness_authority,
                security_fixture_count > 0,
                security_divergences == 0,
                no_hardcoded,
                clean,
            )
        )
    status_value = "PASS" if all_pass and source_bound == "PASS" else "FAIL"
    return {
        "SHARED_RESOURCE_POLICY_VERSION_EXPLICIT": bool(shared.SHARED_RESOURCE_POLICY_VERSION_EXPLICIT),
        "SHARED_LAYOUT_VERSION_EXPLICIT": bool(shared.SHARED_LAYOUT_VERSION_EXPLICIT),
        "EXISTING_PROJECTS_MIGRATED": existing_migrations,
        "PREMIUM_CALCULATOR_MOVED": premium_moved,
        "NODE_MODULES_SHARED": node_rule.shared_allowed,
        "PROJECT_VENV_SHARED": venv_rule.shared_allowed,
        "CARGO_REGISTRY_CACHE_SHARED_ALLOWED": cargo_registry_rule.shared_allowed,
        "CARGO_GIT_CACHE_SHARED_ALLOWED": cargo_git_rule.shared_allowed,
        "INEXACT_SHARED_RESOURCE_REUSE": inexact_reuse,
        "CROSS_PROJECT_MUTABLE_STATE_COLLISIONS": local_collision,
        "WRITE_ESCAPE_ACCEPTED": write_escape,
        "REPARSE_ESCAPE_ACCEPTED": reparse_accepted,
        "SYMLINK_ESCAPE_EFFECTS": int(symlink_effect),
        "ACL_DENIAL_SECURITY_BYPASSES": int(acl_result.accepted or security_store.paths_for(acl_key).directory.exists()),
        "AUTOMATIC_ELEVATION_ATTEMPTS": security_store.automatic_elevation_attempts,
        "FOREIGN_SHARED_LOCK_RELEASE_ACCEPTED": foreign_release,
        "CONCURRENT_SHARED_OPERATIONS": len(concurrent_results),
        "TORN_SHARED_ENTRIES": int(store.lookup(partial_key).hit),
        "CROSS_PROJECT_COLLISIONS": int(different_paths),
        "POISONED_SHARED_ENTRY_ACCEPTED": poisoned_lookup.hit,
        "PARTIAL_SHARED_ENTRY_ACCEPTED": partial_lookup.hit,
        "QUOTA_BYPASS_ACCEPTED": quota_bypass,
        "ACTIVE_SHARED_RESOURCE_COLLECTED": active_collected,
        "REFERENCED_SHARED_RESOURCE_COLLECTED": referenced_collected,
        "EXACT_KEY_REUSE_DIVERGENCES": exact_reuse_divergences,
        "DIFFERENT_KEY_ALIASING": different_aliasing,
        "SHARED_RESOURCE_BECOMES_READINESS_AUTHORITY": shared_readiness_authority,
        "SECURITY_FIXTURES": security_fixture_count,
        "SECURITY_VERIFIER_DIVERGENCES": security_divergences,
        "HARDCODED_GATE_RESULT_FIELDS": hardcoded_fields,
        "NO_HARDCODED_GATE_RESULTS": no_hardcoded,
        "SOURCE_HEAD": head,
        "SOURCE_TREE": tree,
        "WORKTREE_CLEAN": clean,
        "SOURCE_BOUND_MACHINE_GATE": source_bound,
        "NX037_STATUS": status_value,
    }


def test_nx037_machine_gate_execution() -> None:
    gate = run_nx037_machine_gate()
    print(json.dumps(gate, indent=2, sort_keys=True))
    assert gate["SHARED_RESOURCE_POLICY_VERSION_EXPLICIT"] is True
    assert gate["SHARED_LAYOUT_VERSION_EXPLICIT"] is True
    assert gate["EXISTING_PROJECTS_MIGRATED"] == 0
    assert gate["PREMIUM_CALCULATOR_MOVED"] is False
    assert gate["NODE_MODULES_SHARED"] is False
    assert gate["PROJECT_VENV_SHARED"] is False
    assert gate["CARGO_REGISTRY_CACHE_SHARED_ALLOWED"] is True
    assert gate["CARGO_GIT_CACHE_SHARED_ALLOWED"] is True
    assert gate["INEXACT_SHARED_RESOURCE_REUSE"] == 0
    assert gate["CROSS_PROJECT_MUTABLE_STATE_COLLISIONS"] == 0
    assert gate["WRITE_ESCAPE_ACCEPTED"] is False
    assert gate["REPARSE_ESCAPE_ACCEPTED"] is False
    assert gate["SYMLINK_ESCAPE_EFFECTS"] == 0
    assert gate["ACL_DENIAL_SECURITY_BYPASSES"] == 0
    assert gate["AUTOMATIC_ELEVATION_ATTEMPTS"] == 0
    assert gate["FOREIGN_SHARED_LOCK_RELEASE_ACCEPTED"] is False
    assert gate["CONCURRENT_SHARED_OPERATIONS"] >= 6
    assert gate["TORN_SHARED_ENTRIES"] == 0
    assert gate["CROSS_PROJECT_COLLISIONS"] == 0
    assert gate["POISONED_SHARED_ENTRY_ACCEPTED"] is False
    assert gate["PARTIAL_SHARED_ENTRY_ACCEPTED"] is False
    assert gate["QUOTA_BYPASS_ACCEPTED"] is False
    assert gate["ACTIVE_SHARED_RESOURCE_COLLECTED"] is False
    assert gate["REFERENCED_SHARED_RESOURCE_COLLECTED"] is False
    assert gate["EXACT_KEY_REUSE_DIVERGENCES"] == 0
    assert gate["DIFFERENT_KEY_ALIASING"] == 0
    assert gate["SHARED_RESOURCE_BECOMES_READINESS_AUTHORITY"] is False
    assert gate["SECURITY_FIXTURES"] >= 10
    assert gate["SECURITY_VERIFIER_DIVERGENCES"] == 0
    assert gate["HARDCODED_GATE_RESULT_FIELDS"] == []
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    assert gate["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
    assert gate["NX037_STATUS"] == "PASS"

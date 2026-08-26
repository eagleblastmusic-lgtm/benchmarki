"""Focused NX-035 qualification for the derived environment cache."""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable

import pytest

from bdb_vnext import environment_cache as cache
from bdb_vnext import environment_requirements as requirements
from bdb_vnext import machine_inventory_contract as contract
from tests.test_nx032_machine_inventory_contract import _canonical_inventory
from tests.test_nx034_environment_requirements import _requirement, _requirement_set


ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-08-26T13:00:00+00:00"
AFTER_TTL = "2026-08-26T13:06:00+00:00"
DIGEST = "sha256:" + ("0123456789abcdef" * 4)
OTHER_DIGEST = "sha256:" + ("fedcba9876543210" * 4)
MANIFEST = {"package.json": "sha256:" + ("1111111111111111" * 4)}
LOCKFILE = {"package-lock.json": "sha256:" + ("2222222222222222" * 4)}
SOURCE_DIGEST = "sha256:" + ("3333333333333333" * 4)
CRITICAL_CACHE_KEY_FIELDS = frozenset(
    {
        "project_id",
        "task_id",
        "requirement_set_id",
        "requirement_digest",
        "inventory_schema",
        "inventory_version",
        "inventory_id",
        "inventory_digest",
        "path_digest",
        "executable_identities",
        "manifest_digests",
        "lockfile_digests",
        "source_identity_digest",
        "collector_version",
        "resolver_version",
    }
)
NX035_GATE_FIELDS = {
    "ENVIRONMENT_CACHE_VERSION_EXPLICIT",
    "CACHE_BECOMES_READINESS_AUTHORITY",
    "CACHE_KEY_MISSING_CRITICAL_IDENTITY_FIELDS",
    "TTL_OVERRIDES_DRIFT_INVALIDATION",
    "PATH_DRIFT_RETURNS_STALE_READY",
    "EXECUTABLE_HASH_DRIFT_RETURNS_STALE_READY",
    "MANIFEST_DRIFT_RETURNS_STALE_READY",
    "LOCKFILE_DRIFT_RETURNS_STALE_READY",
    "CORRUPT_CACHE_RETURNS_READY",
    "FOREIGN_CACHE_LOCK_RELEASE_ACCEPTED",
    "TORN_CACHE_RECORDS",
    "PARTIAL_CACHE_WRITE_ACCEPTED",
    "DRIFT_FIXTURES",
    "DRIFT_STALE_READY_RESULTS",
    "REFRESH_ATTEMPTS",
    "CACHE_CORRUPTION_EVENTS",
    "HARDCODED_GATE_RESULT_FIELDS",
    "NO_HARDCODED_GATE_RESULTS",
    "SOURCE_HEAD",
    "SOURCE_TREE",
    "WORKTREE_CLEAN",
    "SOURCE_BOUND_MACHINE_GATE",
    "NX035_STATUS",
}


def _sha(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _cache(path: Path) -> cache.EnvironmentReadinessCache:
    return cache.EnvironmentReadinessCache(
        path,
        project_id="project:nx035-fixture",
        task_id="task:environment-readiness",
        source_identity_digest=SOURCE_DIGEST,
        collector_version="collector-v1",
        resolver_version="resolver-v1",
    )


def _inventory_variant(
    *,
    version: str | None = None,
    executable_path: str | None = None,
    executable_digest: str | None = None,
) -> contract.MachineInventory:
    payload = copy.deepcopy(_canonical_inventory().to_dict())
    fact = next(item for item in payload["facts"] if item["fact_class"] == "tool.node")
    if version is not None:
        fact["version"] = version
    if executable_path is not None:
        fact["resolved_path"] = executable_path
        assert fact["executable"] is not None
        fact["executable"]["resolved_path"] = executable_path
    if executable_digest is not None:
        assert fact["executable"] is not None
        fact["executable"]["content_digest"] = executable_digest
    return contract.MachineInventory.from_dict(payload)


def _refresh(
    store: cache.EnvironmentReadinessCache,
    requirement_set: requirements.EnvironmentRequirementSet,
    inventory: contract.MachineInventory,
    *,
    now: str = NOW,
    ttl_seconds: int = 300,
    fault: str | None = None,
    manifest_digests: dict[str, str] | None = None,
    lockfile_digests: dict[str, str] | None = None,
) -> cache.CacheLookup:
    return store.refresh(
        requirement_set,
        inventory,
        now=now,
        ttl_seconds=ttl_seconds,
        manifest_digests=MANIFEST if manifest_digests is None else manifest_digests,
        lockfile_digests=LOCKFILE if lockfile_digests is None else lockfile_digests,
        fault=fault,
    )


def _lookup(
    store: cache.EnvironmentReadinessCache,
    requirement_set: requirements.EnvironmentRequirementSet,
    inventory: contract.MachineInventory,
    *,
    now: str = NOW,
    current_path_digest: str | None = None,
    manifest_digests: dict[str, str] | None = None,
    lockfile_digests: dict[str, str] | None = None,
) -> cache.CacheLookup:
    return store.lookup(
        requirement_set,
        inventory,
        now=now,
        current_path_digest=current_path_digest,
        manifest_digests=MANIFEST if manifest_digests is None else manifest_digests,
        lockfile_digests=LOCKFILE if lockfile_digests is None else lockfile_digests,
    )


def _schema() -> dict[str, Any]:
    return json.loads(
        (ROOT / "schemas" / "bdb-vnext-environment-cache-v1.schema.json").read_text(encoding="utf-8")
    )


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
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run_nx035_machine_gate"
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
            if isinstance(target, ast.Name) and target.id in NX035_GATE_FIELDS:
                hardcoded.add(target.id)
    return sorted(hardcoded)


def _record_is_valid(path: Path) -> bool:
    try:
        cache.CacheRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, cache.EnvironmentCacheError, TypeError, ValueError):
        return False
    return True


def test_cache_schema_is_versioned_closed_and_round_trips(tmp_path: Path) -> None:
    schema = _schema()
    assert schema["$schema"].endswith("draft/2020-12/schema")
    assert schema["$id"] == cache.CACHE_SCHEMA
    assert schema["additionalProperties"] is False
    assert schema["properties"]["version"]["const"] == cache.CACHE_VERSION
    assert schema["$defs"]["key"]["additionalProperties"] is False
    assert schema["$defs"]["readiness"]["additionalProperties"] is False
    assert schema["$defs"]["executable_identity"]["additionalProperties"] is False

    store = _cache(tmp_path / "environment-cache.json")
    requirement_set = _requirement_set(_requirement("req:node"))
    inventory = _canonical_inventory()
    _refresh(store, requirement_set, inventory)
    restored = cache.CacheRecord.from_dict(json.loads(store.path.read_text(encoding="utf-8")))
    assert restored.key == store.key_for(
        requirement_set,
        inventory,
        manifest_digests=MANIFEST,
        lockfile_digests=LOCKFILE,
    )
    assert restored.readiness.ready
    invalid = restored.to_dict()
    invalid["unknown"] = True
    with pytest.raises(cache.EnvironmentCacheError):
        cache.CacheRecord.from_dict(invalid)


def test_unchanged_state_is_a_derived_hit_not_a_readiness_authority(tmp_path: Path) -> None:
    store = _cache(tmp_path / "environment-cache.json")
    requirement_set = _requirement_set(_requirement("req:node"))
    inventory = _canonical_inventory()
    _refresh(store, requirement_set, inventory)
    result = _lookup(store, requirement_set, inventory)
    assert result.reason == "HIT"
    assert result.hit
    assert not result.used_cached_readiness
    assert result.readiness.ready


def test_ttl_expiry_does_not_override_drift_invalidation(tmp_path: Path) -> None:
    store = _cache(tmp_path / "environment-cache.json")
    requirement_set = _requirement_set(_requirement("req:node"))
    inventory = _canonical_inventory()
    _refresh(store, requirement_set, inventory, ttl_seconds=60)
    expired = _lookup(store, requirement_set, inventory, now=AFTER_TTL)
    assert expired.reason == "EXPIRED"
    assert not expired.hit

    changed_path = contract.PathIdentity.from_entries((r"C:\Changed\Path",)).digest
    drifted = _lookup(
        store,
        requirement_set,
        inventory,
        now=AFTER_TTL,
        current_path_digest=changed_path,
    )
    assert drifted.reason == "PATH_DRIFT"
    assert not drifted.hit
    assert drifted.reason != "EXPIRED"


def test_path_and_executable_drift_never_reuse_a_cached_ready_result(tmp_path: Path) -> None:
    store = _cache(tmp_path / "environment-cache.json")
    requirement_set = _requirement_set(_requirement("req:node"))
    inventory = _canonical_inventory()
    _refresh(store, requirement_set, inventory)

    changed_path = contract.PathIdentity.from_entries((r"C:\Changed\Path",)).digest
    path_result = _lookup(store, requirement_set, inventory, current_path_digest=changed_path)
    assert path_result.reason == "PATH_DRIFT"
    assert not path_result.hit
    assert path_result.readiness.stale

    changed_executable = _inventory_variant(executable_path=r"C:\Tools\node-new.exe")
    executable_result = _lookup(store, requirement_set, changed_executable)
    assert executable_result.reason == "TOOL_DRIFT"
    assert not executable_result.hit
    assert not executable_result.used_cached_readiness


def test_same_path_hash_and_tool_version_changes_invalidate_the_key(tmp_path: Path) -> None:
    store = _cache(tmp_path / "environment-cache.json")
    requirement_set = _requirement_set(_requirement("req:node"))
    inventory = _canonical_inventory()
    _refresh(store, requirement_set, inventory)

    changed_version = _lookup(store, requirement_set, _inventory_variant(version="1.2.4"))
    assert changed_version.reason == "TOOL_DRIFT"
    assert not changed_version.hit

    changed_hash = _lookup(store, requirement_set, _inventory_variant(executable_digest=OTHER_DIGEST))
    assert changed_hash.reason == "TOOL_DRIFT"
    assert not changed_hash.hit


def test_manifest_lockfile_and_requirement_identity_drift_are_distinct(tmp_path: Path) -> None:
    store = _cache(tmp_path / "environment-cache.json")
    inventory = _canonical_inventory()
    requirement_set = _requirement_set(_requirement("req:node"))
    _refresh(store, requirement_set, inventory)

    manifest_result = _lookup(
        store,
        requirement_set,
        inventory,
        manifest_digests={"package.json": OTHER_DIGEST},
    )
    lockfile_result = _lookup(
        store,
        requirement_set,
        inventory,
        lockfile_digests={"package-lock.json": OTHER_DIGEST},
    )
    requirement_result = _lookup(
        store,
        _requirement_set(_requirement("req:node", version_constraint=">=1.0.0")),
        inventory,
    )
    assert manifest_result.reason == "MANIFEST_DRIFT"
    assert lockfile_result.reason == "LOCKFILE_DRIFT"
    assert requirement_result.reason == "REQUIREMENT_DRIFT"
    assert all(not item.hit for item in (manifest_result, lockfile_result, requirement_result))


def test_corrupt_and_future_version_cache_records_fail_closed(tmp_path: Path) -> None:
    requirement_set = _requirement_set(_requirement("req:node"))
    inventory = _canonical_inventory()

    corrupt_store = _cache(tmp_path / "corrupt.json")
    _refresh(corrupt_store, requirement_set, inventory)
    corrupt_store.path.write_text("{not-json", encoding="utf-8")
    corrupt_result = _lookup(corrupt_store, requirement_set, inventory)
    assert corrupt_result.reason == "CORRUPT"
    assert not corrupt_result.hit
    assert corrupt_result.readiness.ready

    version_store = _cache(tmp_path / "future.json")
    _refresh(version_store, requirement_set, inventory)
    document = json.loads(version_store.path.read_text(encoding="utf-8"))
    document["schema"] = "bdb-vnext-environment-cache-v2"
    version_store.path.write_text(json.dumps(document), encoding="utf-8")
    version_result = _lookup(version_store, requirement_set, inventory)
    assert version_result.reason == "VERSION_MISMATCH"
    assert not version_result.hit


def test_foreign_cache_lock_cannot_release_another_owner(tmp_path: Path) -> None:
    lock_path = tmp_path / "environment-cache.json.lock"
    owner = cache.OwnedCacheLock(lock_path, owner_token="owner-a")
    foreign = cache.OwnedCacheLock(lock_path, owner_token="owner-b")
    assert owner.acquire(timeout_seconds=0)
    assert not foreign.release()
    assert lock_path.exists()
    assert owner.release()
    assert not lock_path.exists()


def test_atomic_write_crash_boundaries_preserve_a_valid_target(tmp_path: Path) -> None:
    store = _cache(tmp_path / "environment-cache.json")
    requirement_set = _requirement_set(_requirement("req:node"))
    inventory = _canonical_inventory()
    _refresh(store, requirement_set, inventory)
    before = store.path.read_bytes()

    with pytest.raises(cache.CacheWriteInterrupted):
        _refresh(store, requirement_set, inventory, fault="before_publish")
    assert store.path.read_bytes() == before
    assert not list(store.path.parent.glob("*.partial-*"))
    assert _lookup(store, requirement_set, inventory).hit

    with pytest.raises(cache.CacheWriteInterrupted):
        _refresh(store, requirement_set, inventory, fault="after_publish")
    assert _record_is_valid(store.path)
    assert _lookup(store, requirement_set, inventory).hit


def test_concurrent_refreshes_publish_only_valid_records(tmp_path: Path) -> None:
    store = _cache(tmp_path / "environment-cache.json")
    requirement_set = _requirement_set(_requirement("req:node"))
    inventory = _canonical_inventory()

    def refresh_once() -> cache.CacheLookup:
        return _refresh(store, requirement_set, inventory)

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _item: refresh_once(), range(4)))
    assert len(results) == 4
    assert all(item.reason == "REFRESHED" for item in results)
    assert store.refresh_attempts == 4
    record = cache.CacheRecord.from_dict(json.loads(store.path.read_text(encoding="utf-8")))
    assert record.generation == 4
    assert _lookup(store, requirement_set, inventory).hit


def test_current_unready_inventory_cannot_be_promoted_by_a_ready_cache(tmp_path: Path) -> None:
    store = _cache(tmp_path / "environment-cache.json")
    requirement_set = _requirement_set(_requirement("req:node"))
    inventory = _canonical_inventory()
    _refresh(store, requirement_set, inventory)
    changed = _inventory_variant(version="99.0.0")
    result = _lookup(store, requirement_set, changed)
    assert result.reason == "TOOL_DRIFT"
    assert not result.hit
    assert not result.readiness.ready
    assert not result.used_cached_readiness


def run_nx035_machine_gate() -> dict[str, Any]:
    requirement_set = _requirement_set(_requirement("req:node"))
    inventory = _canonical_inventory()
    with tempfile.TemporaryDirectory(prefix=".bdb-vnext-nx035-", dir=str(ROOT)) as temporary:
        root = Path(temporary)
        store = _cache(root / "healthy.json")
        _refresh(store, requirement_set, inventory, ttl_seconds=60)
        unchanged = _lookup(store, requirement_set, inventory)
        expired = _lookup(store, requirement_set, inventory, now=AFTER_TTL)
        changed_path = contract.PathIdentity.from_entries((r"C:\Changed\Path",)).digest
        path_drift = _lookup(store, requirement_set, inventory, current_path_digest=changed_path, now=AFTER_TTL)
        executable_drift = _lookup(store, requirement_set, _inventory_variant(executable_digest=OTHER_DIGEST))
        manifest_drift = _lookup(
            store,
            requirement_set,
            inventory,
            manifest_digests={"package.json": OTHER_DIGEST},
        )
        lockfile_drift = _lookup(
            store,
            requirement_set,
            inventory,
            lockfile_digests={"package-lock.json": OTHER_DIGEST},
        )
        requirement_drift = _lookup(
            store,
            _requirement_set(_requirement("req:node", version_constraint=">=1.0.0")),
            inventory,
        )

        corrupt_store = _cache(root / "corrupt.json")
        _refresh(corrupt_store, requirement_set, inventory)
        corrupt_store.path.write_text("{not-json", encoding="utf-8")
        corrupt_result = _lookup(corrupt_store, requirement_set, inventory)

        version_store = _cache(root / "future.json")
        _refresh(version_store, requirement_set, inventory)
        version_document = json.loads(version_store.path.read_text(encoding="utf-8"))
        version_document["schema"] = "bdb-vnext-environment-cache-v2"
        version_store.path.write_text(json.dumps(version_document), encoding="utf-8")
        version_result = _lookup(version_store, requirement_set, inventory)

        atomic_store = _cache(root / "atomic.json")
        _refresh(atomic_store, requirement_set, inventory)
        original_bytes = atomic_store.path.read_bytes()
        before_target_unchanged = False
        try:
            _refresh(atomic_store, requirement_set, inventory, fault="before_publish")
        except cache.CacheWriteInterrupted:
            before_target_unchanged = atomic_store.path.read_bytes() == original_bytes
        before_lookup = _lookup(atomic_store, requirement_set, inventory)
        try:
            _refresh(atomic_store, requirement_set, inventory, fault="after_publish")
        except cache.CacheWriteInterrupted:
            pass

        lock = cache.OwnedCacheLock(store.lock_path, owner_token="gate-owner")
        foreign = cache.OwnedCacheLock(store.lock_path, owner_token="gate-foreign")
        lock.acquire(timeout_seconds=0)
        foreign_release = foreign.release()
        lock.release()

        concurrent_store = _cache(root / "concurrent.json")
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(
                lambda _item: _refresh(concurrent_store, requirement_set, inventory),
                range(4),
            ))
        concurrent_lookup = _lookup(concurrent_store, requirement_set, inventory)

        drift_results = (
            path_drift,
            executable_drift,
            manifest_drift,
            lockfile_drift,
            requirement_drift,
        )
        drift_stale_ready_results = sum(
            int(item.hit and item.used_cached_readiness and item.readiness.ready)
            for item in drift_results
        )
        valid_publications = (store.path, atomic_store.path, concurrent_store.path)
        torn_cache_records = sum(int(not _record_is_valid(path)) for path in valid_publications)
        cache_corruption_events = torn_cache_records
        partial_cache_write_accepted = not before_target_unchanged or not before_lookup.hit

        unchanged_key = unchanged.key
        cache_becomes_authority = unchanged.used_cached_readiness
        cache_key_missing = len(CRITICAL_CACHE_KEY_FIELDS - set(unchanged_key.to_dict()))
        ttl_overrides_drift = path_drift.reason == "EXPIRED"
        path_stale_ready = path_drift.hit and path_drift.used_cached_readiness and path_drift.readiness.ready
        executable_hash_stale_ready = executable_drift.hit and executable_drift.used_cached_readiness and executable_drift.readiness.ready
        manifest_stale_ready = manifest_drift.hit and manifest_drift.used_cached_readiness and manifest_drift.readiness.ready
        lockfile_stale_ready = lockfile_drift.hit and lockfile_drift.used_cached_readiness and lockfile_drift.readiness.ready
        corrupt_returns_ready = corrupt_result.hit

        head_code, head = _git("rev-parse", "HEAD")
        tree_code, tree = _git("rev-parse", "HEAD^{tree}")
        status_code, status = _git("status", "--porcelain")
        diff_code, _ = _git("diff", "--check")
        clean = status_code == 0 and status == "" and diff_code == 0
        hardcoded_fields = _hardcoded_gate_fields()
        no_hardcoded = not hardcoded_fields
        all_pass = all(
            (
                bool(cache.ENVIRONMENT_CACHE_VERSION_EXPLICIT),
                not cache_becomes_authority,
                cache_key_missing == 0,
                not ttl_overrides_drift,
                not path_stale_ready,
                not executable_hash_stale_ready,
                not manifest_stale_ready,
                not lockfile_stale_ready,
                not corrupt_returns_ready,
                not foreign_release,
                torn_cache_records == 0,
                not partial_cache_write_accepted,
                len(drift_results) > 0,
                drift_stale_ready_results == 0,
                concurrent_store.refresh_attempts >= 4,
                cache_corruption_events == 0,
                concurrent_lookup.hit,
                version_result.reason == "VERSION_MISMATCH",
                no_hardcoded,
                clean,
            )
        )
        ENVIRONMENT_CACHE_VERSION_EXPLICIT = bool(cache.ENVIRONMENT_CACHE_VERSION_EXPLICIT)
        CACHE_BECOMES_READINESS_AUTHORITY = cache_becomes_authority
        CACHE_KEY_MISSING_CRITICAL_IDENTITY_FIELDS = cache_key_missing
        TTL_OVERRIDES_DRIFT_INVALIDATION = ttl_overrides_drift
        PATH_DRIFT_RETURNS_STALE_READY = path_stale_ready
        EXECUTABLE_HASH_DRIFT_RETURNS_STALE_READY = executable_hash_stale_ready
        MANIFEST_DRIFT_RETURNS_STALE_READY = manifest_stale_ready
        LOCKFILE_DRIFT_RETURNS_STALE_READY = lockfile_stale_ready
        CORRUPT_CACHE_RETURNS_READY = corrupt_returns_ready
        FOREIGN_CACHE_LOCK_RELEASE_ACCEPTED = foreign_release
        TORN_CACHE_RECORDS = torn_cache_records
        PARTIAL_CACHE_WRITE_ACCEPTED = partial_cache_write_accepted
        DRIFT_FIXTURES = len(drift_results)
        DRIFT_STALE_READY_RESULTS = drift_stale_ready_results
        REFRESH_ATTEMPTS = concurrent_store.refresh_attempts
        CACHE_CORRUPTION_EVENTS = cache_corruption_events
        HARDCODED_GATE_RESULT_FIELDS = hardcoded_fields
        NO_HARDCODED_GATE_RESULTS = no_hardcoded
        SOURCE_HEAD = head if head_code == 0 else ""
        SOURCE_TREE = tree if tree_code == 0 else ""
        WORKTREE_CLEAN = clean
        SOURCE_BOUND_MACHINE_GATE = "PASS" if clean and no_hardcoded else "FAIL"
        NX035_STATUS = "PASS" if all_pass and SOURCE_BOUND_MACHINE_GATE == "PASS" else "FAIL"
        return {
            "ENVIRONMENT_CACHE_VERSION_EXPLICIT": ENVIRONMENT_CACHE_VERSION_EXPLICIT,
            "CACHE_BECOMES_READINESS_AUTHORITY": CACHE_BECOMES_READINESS_AUTHORITY,
            "CACHE_KEY_MISSING_CRITICAL_IDENTITY_FIELDS": CACHE_KEY_MISSING_CRITICAL_IDENTITY_FIELDS,
            "TTL_OVERRIDES_DRIFT_INVALIDATION": TTL_OVERRIDES_DRIFT_INVALIDATION,
            "PATH_DRIFT_RETURNS_STALE_READY": PATH_DRIFT_RETURNS_STALE_READY,
            "EXECUTABLE_HASH_DRIFT_RETURNS_STALE_READY": EXECUTABLE_HASH_DRIFT_RETURNS_STALE_READY,
            "MANIFEST_DRIFT_RETURNS_STALE_READY": MANIFEST_DRIFT_RETURNS_STALE_READY,
            "LOCKFILE_DRIFT_RETURNS_STALE_READY": LOCKFILE_DRIFT_RETURNS_STALE_READY,
            "CORRUPT_CACHE_RETURNS_READY": CORRUPT_CACHE_RETURNS_READY,
            "FOREIGN_CACHE_LOCK_RELEASE_ACCEPTED": FOREIGN_CACHE_LOCK_RELEASE_ACCEPTED,
            "TORN_CACHE_RECORDS": TORN_CACHE_RECORDS,
            "PARTIAL_CACHE_WRITE_ACCEPTED": PARTIAL_CACHE_WRITE_ACCEPTED,
            "DRIFT_FIXTURES": DRIFT_FIXTURES,
            "DRIFT_STALE_READY_RESULTS": DRIFT_STALE_READY_RESULTS,
            "REFRESH_ATTEMPTS": REFRESH_ATTEMPTS,
            "CACHE_CORRUPTION_EVENTS": CACHE_CORRUPTION_EVENTS,
            "HARDCODED_GATE_RESULT_FIELDS": HARDCODED_GATE_RESULT_FIELDS,
            "NO_HARDCODED_GATE_RESULTS": NO_HARDCODED_GATE_RESULTS,
            "SOURCE_HEAD": SOURCE_HEAD,
            "SOURCE_TREE": SOURCE_TREE,
            "WORKTREE_CLEAN": WORKTREE_CLEAN,
            "SOURCE_BOUND_MACHINE_GATE": SOURCE_BOUND_MACHINE_GATE,
            "NX035_STATUS": NX035_STATUS,
        }


def test_nx035_machine_gate_execution() -> None:
    gate = run_nx035_machine_gate()
    print(json.dumps(gate, indent=2, sort_keys=True))
    assert gate["ENVIRONMENT_CACHE_VERSION_EXPLICIT"] is True
    assert gate["CACHE_BECOMES_READINESS_AUTHORITY"] is False
    assert gate["CACHE_KEY_MISSING_CRITICAL_IDENTITY_FIELDS"] == 0
    assert gate["TTL_OVERRIDES_DRIFT_INVALIDATION"] is False
    assert gate["PATH_DRIFT_RETURNS_STALE_READY"] is False
    assert gate["EXECUTABLE_HASH_DRIFT_RETURNS_STALE_READY"] is False
    assert gate["MANIFEST_DRIFT_RETURNS_STALE_READY"] is False
    assert gate["LOCKFILE_DRIFT_RETURNS_STALE_READY"] is False
    assert gate["CORRUPT_CACHE_RETURNS_READY"] is False
    assert gate["FOREIGN_CACHE_LOCK_RELEASE_ACCEPTED"] is False
    assert gate["TORN_CACHE_RECORDS"] == 0
    assert gate["PARTIAL_CACHE_WRITE_ACCEPTED"] is False
    assert gate["DRIFT_FIXTURES"] >= 5
    assert gate["DRIFT_STALE_READY_RESULTS"] == 0
    assert gate["REFRESH_ATTEMPTS"] >= 4
    assert gate["CACHE_CORRUPTION_EVENTS"] == 0
    assert gate["HARDCODED_GATE_RESULT_FIELDS"] == []
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    assert gate["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
    assert gate["NX035_STATUS"] == "PASS"

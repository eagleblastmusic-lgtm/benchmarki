"""Tests for NX-001 Baseline Manifest, Invariant Map, and Machine Gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bdb_vnext.baseline_manifest import (
    ACCEPTED_BRANCH,
    ACCEPTED_HEAD,
    ACCEPTED_TREE,
    CANONICAL_SOURCE_ROOT,
    BaselineManifest,
    InvariantItem,
    SourceGitIdentity,
    build_accepted_baseline_manifest,
    build_canonical_invariant_map,
    get_stale_historical_claims,
    run_nx001_machine_gate,
    verify_baseline_source,
    verify_invariant_map,
    verify_single_root_smoke,
)


def test_manifest_structure_and_digest() -> None:
    manifest = build_accepted_baseline_manifest()
    assert manifest.schema == "bdb-vnext-nx001-baseline-manifest-v1"
    assert manifest.task_id == "NX-001"
    assert manifest.project_id == "bdb-vnext-next-iteration"
    assert manifest.source_git.head == ACCEPTED_HEAD
    assert manifest.source_git.tree == ACCEPTED_TREE
    assert manifest.source_git.branch == ACCEPTED_BRANCH
    assert manifest.manifest_digest.startswith("sha256:")
    assert len(manifest.manifest_digest) == 71  # "sha256:" (7) + 64 hex chars


def test_nx001_rejects_stale_head_and_tree() -> None:
    """Verifier must FAIL if source differs from manifest."""
    manifest = build_accepted_baseline_manifest()

    # Case 1: Stale HEAD
    stale_head_manifest = BaselineManifest(
        schema=manifest.schema,
        task_id=manifest.task_id,
        project_id=manifest.project_id,
        created_at=manifest.created_at,
        source_git=SourceGitIdentity(
            branch=ACCEPTED_BRANCH,
            head="0000000000000000000000000000000000000000",  # Stale/fake HEAD
            tree=ACCEPTED_TREE,
            upstream=manifest.source_git.upstream,
        ),
        single_root=manifest.single_root,
        runtime_identity=manifest.runtime_identity,
        control_state=manifest.control_state,
        relevant_schemas=manifest.relevant_schemas,
        manifest_digest=manifest.manifest_digest,
    )

    result_stale_head = verify_baseline_source(CANONICAL_SOURCE_ROOT, stale_head_manifest, git_ref=ACCEPTED_HEAD)
    assert not result_stale_head.passed
    assert any("stale_head" in m for m in result_stale_head.mismatches)

    # Case 2: Stale tree
    stale_tree_manifest = BaselineManifest(
        schema=manifest.schema,
        task_id=manifest.task_id,
        project_id=manifest.project_id,
        created_at=manifest.created_at,
        source_git=SourceGitIdentity(
            branch=ACCEPTED_BRANCH,
            head=ACCEPTED_HEAD,
            tree="1111111111111111111111111111111111111111",  # Stale/fake tree
            upstream=manifest.source_git.upstream,
        ),
        single_root=manifest.single_root,
        runtime_identity=manifest.runtime_identity,
        control_state=manifest.control_state,
        relevant_schemas=manifest.relevant_schemas,
        manifest_digest=manifest.manifest_digest,
    )

    result_stale_tree = verify_baseline_source(CANONICAL_SOURCE_ROOT, stale_tree_manifest, git_ref=ACCEPTED_HEAD)
    assert not result_stale_tree.passed
    assert any("stale_tree" in m for m in result_stale_tree.mismatches)


def test_nx001_invariant_map_completeness() -> None:
    """Invariant map must cover all required architectural invariant classes without omissions."""
    invariants = build_canonical_invariant_map()
    passed, errors = verify_invariant_map(invariants)
    assert passed, f"Invariant map validation failed: {errors}"
    assert len(errors) == 0

    required_classes = {
        "single_root",
        "source_bound_runtime",
        "bootstrap_m9b_m3c_separation",
        "project_execution_authority",
        "browser_native_protocol",
        "fail_closed_identity",
    }
    present_classes = {item.invariant_class for item in invariants}
    assert required_classes.issubset(present_classes)

    # Test omission detection
    incomplete_invariants = [item for item in invariants if item.invariant_class != "single_root"]
    passed_incomplete, errors_incomplete = verify_invariant_map(incomplete_invariants)
    assert not passed_incomplete
    assert any("Missing required invariant classes" in e for e in errors_incomplete)


def test_nx001_single_root_smoke_no_mutation() -> None:
    """Smoke test confirms single-root layout without modifying any runtime files."""
    passed, errors = verify_single_root_smoke(CANONICAL_SOURCE_ROOT)
    assert passed, f"Single-root smoke test failed: {errors}"
    assert len(errors) == 0

    # Ensure runtime directories exist
    runtime_dir = Path(CANONICAL_SOURCE_ROOT) / "runtime"
    assert (runtime_dir / "clients").is_dir()
    assert (runtime_dir / "config").is_dir()
    assert (runtime_dir / "control").is_dir()


def test_nx001_stale_claims_explicitly_registered() -> None:
    """Historical assertions must be explicitly registered as stale and not accepted as fresh PASS."""
    stale_claims = get_stale_historical_claims()
    assert len(stale_claims) >= 5

    claim_ids = {c.claim_id for c in stale_claims}
    assert "STALE-001" in claim_ids  # Historical M2d
    assert "STALE-002" in claim_ids  # Legacy 145 tests
    assert "STALE-003" in claim_ids  # AppData runtime
    assert "STALE-004" in claim_ids  # Prior commit browser witness
    assert "STALE-005" in claim_ids  # Prior bootstrap candidate

    for claim in stale_claims:
        assert claim.why_stale != ""
        assert claim.qualification_requirement != ""


def test_nx001_machine_gate_execution() -> None:
    """Deterministic machine gate must return PASS on current accepted baseline."""
    passed, report = run_nx001_machine_gate(CANONICAL_SOURCE_ROOT, target_git_ref=ACCEPTED_HEAD)
    assert passed is True
    assert report["status"] == "PASS"
    assert report["task_id"] == "NX-001"
    assert report["accepted_head"] == ACCEPTED_HEAD
    assert report["accepted_tree"] == ACCEPTED_TREE
    assert report["source_head"] == ACCEPTED_HEAD
    assert report["source_tree"] == ACCEPTED_TREE
    assert report["branch"] == ACCEPTED_BRANCH
    assert report["upstream"] == "origin/bdb-vnext"
    assert report["invariant_map_verification"]["passed"] is True
    assert report["single_root_smoke"]["passed"] is True

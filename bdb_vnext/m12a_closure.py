"""Full M12a closure proof above the base compatibility-zero capture.

The base M12a gate proves runtime/source compatibility usage zero.  This module
adds the remaining canonical closure evidence before M12b may begin:

* fresh repository-wide compatibility inventory with explicit disposition;
* stale Browser/client rejection while ACTIVE authority bytes remain unchanged;
* interrupted/missing/tampered archive rehearsal on a scratch copy only;
* bounded post-ACTIVE soak telemetry and a versioned benchmark basis.

This module is evidence-only.  It owns no activation, maintenance, deletion,
contract, install, route, start, stop, or writer mutation surface.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, NoReturn

from bdb_shared.evidence import canonical_json_bytes, semantic_digest
from bdb_vnext.composition import BROWSER_EXTENSION_ID, PROTOCOL_GENERATION
from bdb_vnext.m11c_active_reader import M11cActiveReadError, observe_bootstrap_activation
from bdb_vnext.m11c_windows_clients import M11cClientError, query_client_plan, require_client_verification
from bdb_vnext.m12a_compatibility_zero import (
    M12aCompatibilityError,
    capture_compatibility_zero,
    verify_compatibility_zero,
)
from bdb_vnext.m9a_handoff import M9aHandoffError, revalidate_side_by_side_digest, verify_side_by_side_archive
from bdb_vnext.m9b_activation import M9bActivationError, read_activation
from bdb_vnext.m9b_native_host import (
    M9B_NATIVE_REQUEST_SCHEMA,
    M9bNativeError,
    VNextNativeConfig,
    handle_message,
)


M12A_CLOSURE_SCHEMA = "bdb-vnext-m12a-full-closure-report-v1"
M12A_CLOSURE_RESULT_SCHEMA = "bdb-vnext-m12a-full-closure-result-v1"
M12A_INVENTORY_SCHEMA = "bdb-vnext-m12a-compatibility-inventory-v1"
M12A_BENCHMARK_BASIS_SCHEMA = "bdb-vnext-m12a-benchmark-basis-v1"
M12A_STALE_CLIENT_SCHEMA = "bdb-vnext-m12a-stale-client-rehearsal-v1"
M12A_INTERRUPTED_ARCHIVE_SCHEMA = "bdb-vnext-m12a-interrupted-archive-rehearsal-v1"
M12A_SOAK_SCHEMA = "bdb-vnext-m12a-read-only-soak-v1"

_MAX_OBJECT_BYTES = 4 * 1024 * 1024
_SHA40_LENGTH = 40

_COMPATIBILITY_TOKENS = (
    "bdb_bridge",
    "com.bartosz.dev_bridge",
    "BartoszDevBridge",
    "legacy",
    "m9a_",
    "m11a_",
    "m11c_",
)

_BENCHMARK_BASIS_FILES = (
    "docs/LOCAL_BROWSER_BENCHMARK.md",
    "scripts/run_local_browser_benchmark.py",
    "tests/test_local_browser_benchmark.py",
)


class M12aClosureError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def _fail(code: str, message: str, *, details: Mapping[str, Any] | None = None) -> NoReturn:
    raise M12aClosureError(code, message, details=details)


def _absolute(value: str | Path, *, field: str) -> Path:
    path = Path(value).expanduser().absolute()
    if not path.is_absolute() or path.is_symlink() or not path.exists():
        _fail("path_unavailable", f"{field} must be an available absolute non-symlink path")
    return path


def _git(repo: Path, *args: str, binary: bool = False) -> bytes | str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise M12aClosureError("git_observation_failed", "Git inventory observation failed") from exc
    if completed.returncode != 0:
        _fail("git_subject_unavailable", f"Git subject is unavailable: {' '.join(args)}")
    return completed.stdout if binary else completed.stdout.decode("utf-8", errors="strict")


def _source_commit(repo: Path, value: str) -> str:
    if not isinstance(value, str) or len(value) != _SHA40_LENGTH or any(ch not in "0123456789abcdef" for ch in value):
        _fail("invalid_source_identity", "source_commit must be an exact lowercase Git SHA")
    _git(repo, "cat-file", "-e", f"{value}^{{commit}}")
    return value


def _file_bytes(repo: Path, commit: str, path: str) -> bytes | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), "show", f"{commit}:{path}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise M12aClosureError("git_observation_failed", "Git file observation failed") from exc
    if completed.returncode != 0:
        return None
    if len(completed.stdout) > _MAX_OBJECT_BYTES:
        _fail("inventory_file_too_large", f"inventory source is too large: {path}")
    return completed.stdout


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _classify_path(path: str) -> tuple[str, str] | None:
    if path.startswith("bdb_bridge/"):
        return "LEGACY_RUNTIME_PACKAGE", "EXCLUDE_FROM_TARGET_ONLY_RELEASE_IN_M12B"
    if path.startswith("bdb_operator/"):
        return "LEGACY_OPERATOR_PACKAGE", "EXCLUDE_FROM_TARGET_ONLY_RELEASE_IN_M12B"
    if path.startswith("bdb_gui/"):
        return "LEGACY_UI_PACKAGE", "EXCLUDE_FROM_TARGET_ONLY_RELEASE_IN_M12B"
    if path.startswith("bdb_poc/"):
        return "LEGACY_POC_PACKAGE", "ARCHIVE_OR_EXCLUDE_FROM_TARGET_ONLY_RELEASE_IN_M12B"
    if path.startswith("browser_extension/"):
        return "LEGACY_BROWSER_PACKAGE", "EXCLUDE_FROM_TARGET_ONLY_RELEASE_IN_M12B"
    if path.startswith("browser_extension_vnext/"):
        return "TARGET_BROWSER_PACKAGE", "RETAIN_IN_TARGET_ONLY_RELEASE_IN_M12B"
    if path == "packaging/windows/native_host_entry.py":
        return "LEGACY_NATIVE_PACKAGING_ENTRY", "EXCLUDE_FROM_TARGET_ONLY_RELEASE_IN_M12B"
    if path == "packaging/windows/vnext_native_host_entry.py":
        return "TARGET_NATIVE_PACKAGING_ENTRY", "RETAIN_IN_TARGET_ONLY_RELEASE_IN_M12B"
    if path.startswith("bdb_vnext/"):
        return "VNEXT_MIGRATION_OR_COMPATIBILITY_SOURCE", "RETAIN_ONLY_IF_TARGET_CLOSURE_REQUIRES_ELSE_REMOVE_IN_M12B"
    if path.startswith("bdb_shared/"):
        return "SHARED_COMPATIBILITY_REFERENCE", "RETAIN_ONLY_NON_LEGACY_FOUNDATION_IN_M12B"
    if path.startswith(("bdb_integrations/", "bdb_release/", "bdb_bartosz_os/")):
        return "AUXILIARY_COMPATIBILITY_REFERENCE", "REVIEW_AND_EXCLUDE_NON_TARGET_SURFACES_IN_M12B"
    if path.startswith("benchmarks/"):
        return "BENCHMARK_COMPATIBILITY_EVIDENCE", "RETAIN_AS_NON_PRODUCTION_BENCHMARK_ARCHIVE_IN_M12B"
    if path.startswith("scripts/"):
        return "INSTALLER_WRAPPER_SCRIPT", "ARCHIVE_OR_DELETE_SOURCE_ONLY_SURFACES_IN_M12B"
    if path.startswith("tests/"):
        return "TEST_ONLY_COMPATIBILITY_REFERENCE", "RETAIN_AS_NON_PRODUCTION_TEST_OR_ARCHIVE_IN_M12B"
    if path.startswith("docs/"):
        return "DOCUMENTATION_HISTORY_REFERENCE", "RETAIN_AS_NON_AUTHORITY_HISTORY_IN_M12B"
    if path.startswith("schemas/"):
        return "CONTRACT_SCHEMA_REFERENCE", "RETAIN_ONLY_REQUIRED_TARGET_OR_ARCHIVE_SCHEMA_IN_M12B"
    if path.startswith(".github/"):
        return "CI_COMPATIBILITY_REFERENCE", "RETAIN_ONLY_TARGET_RELEASE_CI_IN_M12B"
    if path == "pyproject.toml":
        return "PACKAGE_COMPOSITION_SURFACE", "CONTRACT_TO_TARGET_ONLY_PACKAGE_IN_M12B"
    if "/" not in path:
        return "ROOT_COMPATIBILITY_REFERENCE", "REVIEW_AND_RETAIN_ONLY_TARGET_RELEASE_CONTENT_IN_M12B"
    return None


def inventory_compatibility_surfaces(*, repo_root: str | Path, source_commit: str) -> dict[str, Any]:
    """Scan the complete tracked Git subject and classify every compatibility hit."""

    repo = _absolute(repo_root, field="repo_root")
    commit = _source_commit(repo, source_commit)
    paths = [line for line in str(_git(repo, "ls-tree", "-r", "--name-only", commit)).splitlines() if line]
    path_set = set(paths)

    matched: set[str] = set()
    token_hits: dict[str, set[str]] = {}
    for token in _COMPATIBILITY_TOKENS:
        try:
            output = str(_git(repo, "grep", "-Il", "-e", token, commit, "--"))
        except M12aClosureError as exc:
            # git grep returns 1 when there are no matches; use a direct subprocess
            # so an empty token class is not mistaken for an unavailable subject.
            if exc.code != "git_subject_unavailable":
                raise
            completed = subprocess.run(
                ["git", "-C", str(repo), "grep", "-Il", "-e", token, commit, "--"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
            if completed.returncode not in (0, 1):
                _fail("git_inventory_failed", f"git grep failed for token: {token}")
            output = completed.stdout.decode("utf-8", errors="strict")
        prefix = f"{commit}:"
        for raw_path in output.splitlines():
            path = raw_path[len(prefix):] if raw_path.startswith(prefix) else raw_path
            if path in path_set:
                matched.add(path)
                token_hits.setdefault(path, set()).add(token)

    for path in paths:
        if path.startswith(("bdb_bridge/", "bdb_operator/", "bdb_gui/", "bdb_poc/", "browser_extension/")):
            matched.add(path)

    entries: list[dict[str, Any]] = []
    unclassified: list[str] = []
    for path in sorted(matched):
        classification = _classify_path(path)
        if classification is None:
            unclassified.append(path)
            continue
        surface_class, disposition = classification
        payload = _file_bytes(repo, commit, path)
        entries.append(
            {
                "path": path,
                "surface_class": surface_class,
                "disposition": disposition,
                "token_hits": sorted(token_hits.get(path, set())),
                "sha256": _sha256(payload) if payload is not None else None,
            }
        )

    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry["surface_class"]] = counts.get(entry["surface_class"], 0) + 1
    complete = not unclassified and len(paths) > 0
    return {
        "schema": M12A_INVENTORY_SCHEMA,
        "source_commit": commit,
        "tracked_path_count": len(paths),
        "compatibility_surface_count": len(entries),
        "surface_class_counts": {key: counts[key] for key in sorted(counts)},
        "entries": entries,
        "unclassified_compatibility_paths": unclassified,
        "inventory_complete": complete,
        "production_mutation_performed": False,
    }


def build_benchmark_basis(*, repo_root: str | Path, source_commit: str) -> dict[str, Any]:
    """Build the complete named benchmark basis without re-arming Legacy."""

    repo = _absolute(repo_root, field="repo_root")
    commit = _source_commit(repo, source_commit)
    basis_files: list[dict[str, Any]] = []
    missing: list[str] = []
    for path in _BENCHMARK_BASIS_FILES:
        payload = _file_bytes(repo, commit, path)
        if payload is None:
            missing.append(path)
        else:
            basis_files.append({"path": path, "sha256": _sha256(payload), "size_bytes": len(payload)})

    scenarios = [
        {"scenario_id": "historical-local-browser-functional-performance", "evidence": "preserved-benchmark-harness", "execution": "HISTORICAL_BASIS_ONLY_DO_NOT_REARM_LEGACY"},
        {"scenario_id": "post-active-browser-native-exact-verification", "evidence": "m11c-client-verification", "execution": "LIVE_READ_ONLY"},
        {"scenario_id": "post-active-legacy-zero-write-soak", "evidence": "m9a-revalidation-soak", "execution": "LIVE_READ_ONLY"},
        {"scenario_id": "stale-browser-client-rejection", "evidence": "m12a-stale-client-rehearsal", "execution": "LIVE_FAIL_CLOSED_READ_ONLY"},
        {"scenario_id": "interrupted-archive-recovery", "evidence": "m12a-scratch-archive-rehearsal", "execution": "SCRATCH_ONLY"},
        {"scenario_id": "single-writer-admission", "evidence": "m3c-supported-path-scan", "execution": "SOURCE_AND_RUNTIME_OBSERVATION"},
        {"scenario_id": "active-native-import-closure", "evidence": "exact-active-git-source-scan", "execution": "SOURCE_READ_ONLY"},
        {"scenario_id": "active-browser-no-fallback", "evidence": "exact-active-browser-bundle-scan", "execution": "SOURCE_READ_ONLY"},
    ]
    return {
        "schema": M12A_BENCHMARK_BASIS_SCHEMA,
        "source_commit": commit,
        "historical_basis_files": basis_files,
        "missing_basis_files": missing,
        "scenarios": scenarios,
        "scenario_count": len(scenarios),
        "basis_complete": not missing and len(scenarios) == 8,
        "legacy_benchmark_rearmed": False,
        "final_target_only_benchmark_deferred_to_m12b": True,
        "production_mutation_performed": False,
    }


def _runtime_snapshot(*, authority_root: Path, runtime_root: Path) -> dict[str, str]:
    bootstrap = observe_bootstrap_activation(authority_root=authority_root)
    if bootstrap.get("status") != "ACTIVE" or bootstrap.get("production_activation_performed") is not True:
        _fail("production_active_required", "M12a closure requires External Bootstrap ACTIVE")
    state = bootstrap.get("state")
    if not isinstance(state, Mapping):
        _fail("active_state_invalid", "External ACTIVE state is unavailable")
    state_sha = state.get("state_sha256")
    if not isinstance(state_sha, str):
        _fail("active_state_invalid", "External ACTIVE state digest is unavailable")

    activation = read_activation(runtime_root)
    if activation is None or activation.state != "ACTIVE":
        _fail("client_gate_not_active", "M12a closure requires M9b ACTIVE")
    activation_map = activation.as_dict()
    plan = query_client_plan(runtime_root=runtime_root)["plan"]
    verification = require_client_verification(
        runtime_root=runtime_root,
        expected_client_plan_sha256=plan["client_plan_sha256"],
    )
    return {
        "bootstrap_state_sha256": state_sha,
        "m9b_record_digest": str(activation_map["record_digest"]),
        "client_plan_sha256": str(plan["client_plan_sha256"]),
        "client_verification_sha256": str(verification["verification_sha256"]),
    }


def rehearse_stale_client_rejection(*, authority_root: str | Path, runtime_root: str | Path) -> dict[str, Any]:
    """Prove old/stale Browser clients fail explicitly without touching ACTIVE state."""

    authority = _absolute(authority_root, field="authority_root")
    runtime = _absolute(runtime_root, field="runtime_root")
    before = _runtime_snapshot(authority_root=authority, runtime_root=runtime)
    config = VNextNativeConfig.from_json(runtime / "config" / "native-host.json")

    cases = [
        (
            "old-protocol",
            {
                "schema": M9B_NATIVE_REQUEST_SCHEMA,
                "request_id": "m12a-old-protocol",
                "action": "handshake",
                "protocol_generation": "bdb-vnext-protocol-v0",
                "browser_extension_id": BROWSER_EXTENSION_ID,
            },
            "unsupported_protocol",
        ),
        (
            "wrong-extension",
            {
                "schema": M9B_NATIVE_REQUEST_SCHEMA,
                "request_id": "m12a-wrong-extension",
                "action": "handshake",
                "protocol_generation": PROTOCOL_GENERATION,
                "browser_extension_id": "a" * 32,
            },
            "client_identity_mismatch",
        ),
    ]
    observed: list[dict[str, str]] = []
    for case_id, message, expected in cases:
        try:
            handle_message(config, message)
        except M9bNativeError as exc:
            if exc.code != expected:
                _fail("stale_client_rejection_mismatch", f"{case_id} returned {exc.code}, expected {expected}")
            observed.append({"case_id": case_id, "error_code": exc.code})
        else:
            _fail("stale_client_accepted", f"stale client case was accepted: {case_id}")

    after = _runtime_snapshot(authority_root=authority, runtime_root=runtime)
    unchanged = before == after
    if not unchanged:
        _fail("stale_client_mutated_authority", "stale-client rehearsal changed ACTIVE authority state")
    return {
        "schema": M12A_STALE_CLIENT_SCHEMA,
        "cases": observed,
        "old_protocol_explicit_upgrade_error": any(item["error_code"] == "unsupported_protocol" for item in observed),
        "active_state_unchanged": unchanged,
        "before": before,
        "after": after,
        "production_mutation_performed": False,
    }


def rehearse_interrupted_archive(
    *,
    authority_root: str | Path,
    runtime_root: str | Path,
    freeze_digest: str,
) -> dict[str, Any]:
    """Rehearse missing/tampered archive failures on a disposable copy only."""

    authority = _absolute(authority_root, field="authority_root")
    runtime = _absolute(runtime_root, field="runtime_root")
    before = _runtime_snapshot(authority_root=authority, runtime_root=runtime)
    verified = verify_side_by_side_archive(runtime_root=runtime, freeze_digest=freeze_digest)
    refs = verified.get("evidence_refs")
    if not isinstance(refs, list) or len(refs) < 2 or any(not isinstance(ref, str) for ref in refs):
        _fail("archive_rehearsal_invalid", "M9a archive references are incomplete")

    source_root = runtime / "evidence" / "m9a-side-by-side" / "objects"
    missing_code: str | None = None
    tamper_code: str | None = None
    with tempfile.TemporaryDirectory(prefix="bdb-m12a-archive-") as temporary:
        scratch = Path(temporary)
        target_root = scratch / "evidence" / "m9a-side-by-side" / "objects"
        target_root.mkdir(parents=True)
        for ref in refs:
            source = source_root / f"{ref[7:]}.json"
            destination = target_root / source.name
            shutil.copyfile(source, destination)
        verify_side_by_side_archive(runtime_root=scratch, freeze_digest=freeze_digest)

        child_ref = str(refs[0])
        if child_ref == freeze_digest:
            child_ref = str(refs[1])
        child_path = target_root / f"{child_ref[7:]}.json"
        original = child_path.read_bytes()
        child_path.unlink()
        try:
            verify_side_by_side_archive(runtime_root=scratch, freeze_digest=freeze_digest)
        except M9aHandoffError as exc:
            missing_code = exc.code
        else:
            _fail("archive_missing_object_accepted", "scratch archive accepted a missing child object")

        child_path.write_bytes(original)
        child_path.write_text('{"tampered":true}', encoding="utf-8")
        try:
            verify_side_by_side_archive(runtime_root=scratch, freeze_digest=freeze_digest)
        except M9aHandoffError as exc:
            tamper_code = exc.code
        else:
            _fail("archive_tamper_accepted", "scratch archive accepted tampered child evidence")

    if missing_code != "evidence_missing" or tamper_code not in {"evidence_digest_mismatch", "evidence_invalid"}:
        _fail(
            "archive_rehearsal_error_class_mismatch",
            "scratch interrupted archive rehearsal returned unexpected failure classes",
            details={"missing": missing_code, "tamper": tamper_code},
        )
    after = _runtime_snapshot(authority_root=authority, runtime_root=runtime)
    if before != after:
        _fail("archive_rehearsal_mutated_authority", "archive rehearsal changed ACTIVE authority state")
    return {
        "schema": M12A_INTERRUPTED_ARCHIVE_SCHEMA,
        "baseline_archive_readable": True,
        "missing_object_error_code": missing_code,
        "tampered_object_error_code": tamper_code,
        "scratch_only": True,
        "active_state_unchanged": True,
        "production_mutation_performed": False,
    }


def _nearest_rank(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def capture_read_only_soak(
    *,
    authority_root: str | Path,
    runtime_root: str | Path,
    legacy_runtime_root: str | Path,
    freeze_digest: str,
    iterations: int = 5,
    interval_seconds: float = 0.05,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Collect bounded representative post-ACTIVE telemetry without writes."""

    if isinstance(iterations, bool) or not isinstance(iterations, int) or iterations < 3 or iterations > 50:
        _fail("invalid_soak_iterations", "M12a soak iterations must be between 3 and 50")
    if interval_seconds < 0 or interval_seconds > 5:
        _fail("invalid_soak_interval", "M12a soak interval must be between 0 and 5 seconds")
    authority = _absolute(authority_root, field="authority_root")
    runtime = _absolute(runtime_root, field="runtime_root")
    legacy = _absolute(legacy_runtime_root, field="legacy_runtime_root")

    samples: list[float] = []
    identities: list[dict[str, str]] = []
    for index in range(iterations):
        started = time.perf_counter()
        identity = _runtime_snapshot(authority_root=authority, runtime_root=runtime)
        observed_freeze = revalidate_side_by_side_digest(
            runtime_root=runtime,
            legacy_runtime_root=legacy,
            freeze_digest=freeze_digest,
        )
        if observed_freeze != freeze_digest:
            _fail("soak_freeze_mismatch", "M12a soak revalidation returned a different freeze digest")
        samples.append((time.perf_counter() - started) * 1000.0)
        identities.append(identity)
        if index + 1 < iterations and interval_seconds:
            sleep_fn(interval_seconds)

    stable = all(item == identities[0] for item in identities[1:])
    if not stable:
        _fail("soak_identity_drift", "post-ACTIVE authority identity changed during M12a soak")
    return {
        "schema": M12A_SOAK_SCHEMA,
        "iterations": iterations,
        "all_iterations_passed": True,
        "identity_stable": True,
        "identity": identities[0],
        "latency_ms": {
            "minimum": min(samples),
            "mean": sum(samples) / len(samples),
            "p50": _nearest_rank(samples, 0.50),
            "p95": _nearest_rank(samples, 0.95),
            "maximum": max(samples),
        },
        "performance_threshold_applied": False,
        "final_target_only_benchmark_deferred_to_m12b": True,
        "production_mutation_performed": False,
    }


def _objects_root(runtime: Path) -> Path:
    return runtime / "evidence" / "m12a-closure" / "objects"


def _write_object(runtime: Path, value: Mapping[str, Any]) -> tuple[str, Path]:
    payload = canonical_json_bytes(dict(value))
    if len(payload) > _MAX_OBJECT_BYTES:
        _fail("closure_evidence_too_large", "M12a closure evidence exceeds bounded size")
    digest = semantic_digest(value)
    path = _objects_root(runtime) / f"{digest[7:]}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            _fail("closure_evidence_conflict", "existing M12a closure object differs")
        return digest, path
    temporary = path.parent / f".{path.name}.partial-{uuid.uuid4().hex}"
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise M12aClosureError("closure_evidence_write_failed", "M12a closure evidence could not be published") from exc
    return digest, path


def _read_object(runtime: Path, digest: str) -> dict[str, Any]:
    if not isinstance(digest, str) or len(digest) != 71 or not digest.startswith("sha256:"):
        _fail("closure_digest_invalid", "M12a closure digest must be exact sha256")
    path = _objects_root(runtime) / f"{digest[7:]}.json"
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise M12aClosureError("closure_evidence_missing", "M12a closure evidence is unavailable") from exc
    if len(payload) > _MAX_OBJECT_BYTES:
        _fail("closure_evidence_too_large", "M12a closure evidence exceeds bounded size")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise M12aClosureError("closure_evidence_invalid", "M12a closure evidence is invalid JSON") from exc
    if not isinstance(value, Mapping) or semantic_digest(value) != digest:
        _fail("closure_evidence_digest_mismatch", "M12a closure evidence digest differs")
    return {str(key): item for key, item in value.items()}


def capture_full_closure(
    *,
    authority_root: str | Path,
    runtime_root: str | Path,
    legacy_runtime_root: str | Path,
    repo_root: str | Path,
    observation_seconds: float = 2.0,
    soak_iterations: int = 5,
) -> dict[str, Any]:
    """Capture all M12a canonical DONE proofs; no M12b effect is possible here."""

    runtime = _absolute(runtime_root, field="runtime_root")
    base = capture_compatibility_zero(
        authority_root=authority_root,
        runtime_root=runtime,
        legacy_runtime_root=legacy_runtime_root,
        repo_root=repo_root,
        observation_seconds=observation_seconds,
    )
    if base.get("status") != "PASS_CLOSED":
        return {
            "schema": M12A_CLOSURE_RESULT_SCHEMA,
            "status": "BLOCKED",
            "error_code": "base_m12a_not_closed",
            "base_report_sha256": base.get("report_sha256"),
            "production_mutation_performed": False,
            "final_deletion_performed": False,
        }

    report = base["report"]
    source_commit = str(report["active_source_commit"])
    freeze_digest = str(report["m9a_freeze_digest"])
    inventory = inventory_compatibility_surfaces(repo_root=repo_root, source_commit=source_commit)
    benchmark = build_benchmark_basis(repo_root=repo_root, source_commit=source_commit)
    stale = rehearse_stale_client_rejection(authority_root=authority_root, runtime_root=runtime)
    interrupted = rehearse_interrupted_archive(
        authority_root=authority_root,
        runtime_root=runtime,
        freeze_digest=freeze_digest,
    )
    soak = capture_read_only_soak(
        authority_root=authority_root,
        runtime_root=runtime,
        legacy_runtime_root=legacy_runtime_root,
        freeze_digest=freeze_digest,
        iterations=soak_iterations,
    )

    pass_closed = all(
        (
            inventory["inventory_complete"] is True,
            inventory["unclassified_compatibility_paths"] == [],
            benchmark["basis_complete"] is True,
            stale["old_protocol_explicit_upgrade_error"] is True,
            stale["active_state_unchanged"] is True,
            interrupted["baseline_archive_readable"] is True,
            interrupted["active_state_unchanged"] is True,
            soak["all_iterations_passed"] is True,
            soak["identity_stable"] is True,
        )
    )
    closure = {
        "schema": M12A_CLOSURE_SCHEMA,
        "status": "PASS_CLOSED" if pass_closed else "BLOCKED",
        "base_report_sha256": base["report_sha256"],
        "deletion_plan_sha256": base["deletion_plan_sha256"],
        "active_source_commit": source_commit,
        "m9a_freeze_digest": freeze_digest,
        "compatibility_inventory": inventory,
        "stale_client_rehearsal": stale,
        "interrupted_archive_rehearsal": interrupted,
        "benchmark_basis": benchmark,
        "read_only_soak": soak,
        "unnamed_exceptions": [],
        "m12b_unlocked": pass_closed,
        "production_mutation_performed": False,
        "final_deletion_performed": False,
    }
    digest, path = _write_object(runtime, closure)
    return {
        "schema": M12A_CLOSURE_RESULT_SCHEMA,
        "status": closure["status"],
        "closure_report_sha256": digest,
        "closure_report_path": str(path),
        "report": closure,
        "production_mutation_performed": False,
        "final_deletion_performed": False,
    }


def verify_full_closure(*, runtime_root: str | Path, closure_report_sha256: str) -> dict[str, Any]:
    runtime = _absolute(runtime_root, field="runtime_root")
    report = _read_object(runtime, closure_report_sha256)
    if report.get("schema") != M12A_CLOSURE_SCHEMA:
        _fail("closure_report_invalid", "M12a closure report schema differs")
    base_sha = report.get("base_report_sha256")
    if not isinstance(base_sha, str):
        _fail("closure_report_invalid", "M12a closure report has no base report binding")
    base = verify_compatibility_zero(runtime_root=runtime, report_sha256=base_sha)
    if report.get("status") == "PASS_CLOSED":
        if (
            base.get("status") != "PASS_CLOSED"
            or report.get("m12b_unlocked") is not True
            or report.get("unnamed_exceptions") != []
            or report.get("production_mutation_performed") is not False
            or report.get("final_deletion_performed") is not False
            or report.get("compatibility_inventory", {}).get("inventory_complete") is not True
            or report.get("stale_client_rehearsal", {}).get("active_state_unchanged") is not True
            or report.get("interrupted_archive_rehearsal", {}).get("active_state_unchanged") is not True
            or report.get("benchmark_basis", {}).get("basis_complete") is not True
            or report.get("read_only_soak", {}).get("identity_stable") is not True
        ):
            _fail("closure_report_invalid", "M12a PASS_CLOSED closure invariants differ")
    return {
        "schema": M12A_CLOSURE_RESULT_SCHEMA,
        "status": report["status"],
        "closure_report_sha256": closure_report_sha256,
        "report": report,
        "base_report": base["report"],
        "production_mutation_performed": False,
        "final_deletion_performed": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BDB Next M12a full canonical closure proof")
    sub = parser.add_subparsers(dest="command", required=True)
    capture = sub.add_parser("capture")
    capture.add_argument("--authority-root", required=True)
    capture.add_argument("--runtime-root", required=True)
    capture.add_argument("--legacy-runtime-root", required=True)
    capture.add_argument("--repo-root", required=True)
    capture.add_argument("--observation-seconds", type=float, default=2.0)
    capture.add_argument("--soak-iterations", type=int, default=5)
    verify = sub.add_parser("verify")
    verify.add_argument("--runtime-root", required=True)
    verify.add_argument("--closure-report-sha256", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "capture":
            result = capture_full_closure(
                authority_root=args.authority_root,
                runtime_root=args.runtime_root,
                legacy_runtime_root=args.legacy_runtime_root,
                repo_root=args.repo_root,
                observation_seconds=args.observation_seconds,
                soak_iterations=args.soak_iterations,
            )
        else:
            result = verify_full_closure(
                runtime_root=args.runtime_root,
                closure_report_sha256=args.closure_report_sha256,
            )
        print(canonical_json_bytes(result).decode("utf-8"))
        return 0 if result["status"] == "PASS_CLOSED" else 2
    except (
        M12aClosureError,
        M12aCompatibilityError,
        M11cActiveReadError,
        M11cClientError,
        M9aHandoffError,
        M9bActivationError,
        M9bNativeError,
    ) as exc:
        print(
            canonical_json_bytes(
                {
                    "schema": M12A_CLOSURE_RESULT_SCHEMA,
                    "status": "BLOCKED",
                    "error_code": getattr(exc, "code", "m12a_closure_failed"),
                    "error": str(exc),
                    "details": getattr(exc, "details", {}),
                    "production_mutation_performed": False,
                    "final_deletion_performed": False,
                }
            ).decode("utf-8")
        )
        return 2


__all__ = [
    "M12A_BENCHMARK_BASIS_SCHEMA",
    "M12A_CLOSURE_RESULT_SCHEMA",
    "M12A_CLOSURE_SCHEMA",
    "M12A_INTERRUPTED_ARCHIVE_SCHEMA",
    "M12A_INVENTORY_SCHEMA",
    "M12A_SOAK_SCHEMA",
    "M12A_STALE_CLIENT_SCHEMA",
    "M12aClosureError",
    "build_benchmark_basis",
    "capture_full_closure",
    "capture_read_only_soak",
    "inventory_compatibility_surfaces",
    "rehearse_interrupted_archive",
    "rehearse_stale_client_rejection",
    "verify_full_closure",
]


if __name__ == "__main__":
    raise SystemExit(main())

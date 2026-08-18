"""M12a post-activation compatibility-zero gate.

M12a is observation and evidence only.  It does not delete, contract, disable,
activate, switch, install, start, or stop anything.  Its job is to prove that
all migration/Legacy compatibility paths have zero production usage and an
explicit M12b disposition before destructive cleanup is allowed.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, NoReturn

from bdb_shared.evidence import canonical_json_bytes, semantic_digest
from bdb_vnext.m11c_cutover import M11cCutoverError, observe_bootstrap_activation, query_cutover_plan
from bdb_vnext.m11c_windows_clients import (
    M11cClientError,
    observe_windows_native_routes,
    query_client_plan,
    require_client_verification,
)
from bdb_vnext.m9a_handoff import M9aHandoffError, revalidate_side_by_side_digest, verify_side_by_side_archive
from bdb_vnext.m3c_admission import scan_supported_vnext_admission_paths
from bdb_vnext.m9b_activation import M9bActivationError, read_activation
from bdb_vnext.m9b_reconciliation import M9bReconciliationError, verify_post_active_reconciliation


M12A_REPORT_SCHEMA = "bdb-vnext-m12a-compatibility-zero-report-v1"
M12A_DELETION_PLAN_SCHEMA = "bdb-vnext-m12a-deletion-plan-v1"
M12A_RESULT_SCHEMA = "bdb-vnext-m12a-compatibility-zero-result-v1"
M12A_SCOPE = "post-activation-compatibility-zero-v1"

_MAX_EVIDENCE_BYTES = 2 * 1024 * 1024
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_LEGACY_HOST_LITERAL = re.compile(r"[\"']com\.bartosz\.dev_bridge[\"']")

_FORBIDDEN_ACTIVE_ROOTS = (
    "bdb_bridge",
    "bdb_operator",
    "bdb_gui",
    "bdb_poc",
)
_FORBIDDEN_MIGRATION_MODULES = (
    "bdb_vnext.m9a_blocker_probe_compat",
    "bdb_vnext.m9a_handoff",
    "bdb_vnext.m9a_handoff_cli",
    "bdb_vnext.m11a_bootstrap_admin",
    "bdb_vnext.m11a_prepared_activation",
    "bdb_vnext.m11a_windows_tcb",
    "bdb_vnext.m11c_cutover",
    "bdb_vnext.m11c_cutover_cli",
    "bdb_vnext.m11c_final_prepare",
    "bdb_vnext.m11c_legacy_recovery",
    "bdb_vnext.m11c_native_artifact",
    "bdb_vnext.m11c_native_artifact_cli",
)


class M12aCompatibilityError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def _fail(code: str, message: str, *, details: Mapping[str, Any] | None = None) -> NoReturn:
    raise M12aCompatibilityError(code, message, details=details)


def _absolute(value: str | Path, *, field: str, must_exist: bool = True) -> Path:
    path = Path(value).expanduser().absolute()
    if not path.is_absolute():
        _fail("invalid_path", f"{field} must be absolute")
    if must_exist and (path.is_symlink() or not path.exists()):
        _fail("path_unavailable", f"{field} is unavailable")
    return path


def _digest_field(value: object, field: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        _fail("invalid_digest", f"{field} must be an exact sha256 digest")
    return value


def _sha40(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA40.fullmatch(value) is None:
        _fail("invalid_source_identity", f"{field} must be an exact Git SHA")
    return value


def _run_git(repo: Path, *args: str, binary: bool = False) -> bytes | str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise M12aCompatibilityError("git_observation_failed", "Git source observation failed") from exc
    if completed.returncode != 0:
        _fail("git_subject_unavailable", f"Git subject is unavailable: {' '.join(args)}")
    return completed.stdout if binary else completed.stdout.decode("utf-8", errors="strict")


def _git_file(repo: Path, commit: str, path: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), "show", f"{commit}:{path}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise M12aCompatibilityError("git_observation_failed", "Git source observation failed") from exc
    if completed.returncode != 0:
        return None
    if len(completed.stdout) > _MAX_EVIDENCE_BYTES:
        _fail("source_file_too_large", f"source file is too large for M12a scan: {path}")
    try:
        return completed.stdout.decode("utf-8")
    except UnicodeError as exc:
        raise M12aCompatibilityError("source_decode_failed", f"source file is not UTF-8: {path}") from exc


def _module_source(repo: Path, commit: str, module: str) -> tuple[str, str] | None:
    stem = module.replace(".", "/")
    for path in (f"{stem}.py", f"{stem}/__init__.py"):
        text = _git_file(repo, commit, path)
        if text is not None:
            return path, text
    return None


def _resolved_imports(module: str, tree: ast.AST) -> set[str]:
    result: set[str] = set()
    package = module.rpartition(".")[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                result.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base_parts = package.split(".") if package else []
                trim = node.level - 1
                if trim > len(base_parts):
                    continue
                prefix = base_parts[: len(base_parts) - trim]
                if node.module:
                    prefix.extend(node.module.split("."))
                base = ".".join(part for part in prefix if part)
            else:
                base = node.module or ""
            if base:
                result.add(base)
                for alias in node.names:
                    if alias.name != "*":
                        result.add(f"{base}.{alias.name}")
    return result


def scan_active_python_closure(
    *,
    repo_root: str | Path,
    source_commit: str,
    root_module: str = "bdb_vnext.m9b_native_host",
) -> dict[str, Any]:
    """Scan the exact Git subject used to build the production Native Host."""

    repo = _absolute(repo_root, field="repo_root")
    source_commit = _sha40(source_commit, "source_commit")
    _run_git(repo, "cat-file", "-e", f"{source_commit}^{{commit}}")

    queue: deque[str] = deque([root_module])
    visited: dict[str, str] = {}
    external: set[str] = set()
    missing_local: set[str] = set()
    parse_errors: list[str] = []

    while queue:
        module = queue.popleft()
        if module in visited:
            continue
        source = _module_source(repo, source_commit, module)
        if source is None:
            missing_local.add(module)
            continue
        path, text = source
        visited[module] = path
        try:
            tree = ast.parse(text, filename=path)
        except SyntaxError:
            parse_errors.append(module)
            continue
        for imported in sorted(_resolved_imports(module, tree)):
            if imported.startswith("bdb_vnext"):
                # ImportFrom adds both the module and possible imported symbols.
                # Enqueue only subjects that actually resolve to source modules.
                if _module_source(repo, source_commit, imported) is not None:
                    queue.append(imported)
            elif imported.startswith(_FORBIDDEN_ACTIVE_ROOTS):
                external.add(imported)

    migration_hits = sorted(
        module
        for module in visited
        if any(module == item or module.startswith(item + ".") for item in _FORBIDDEN_MIGRATION_MODULES)
    )
    legacy_hits = sorted(external)
    clean = not parse_errors and not migration_hits and not legacy_hits
    return {
        "root_module": root_module,
        "source_commit": source_commit,
        "module_count": len(visited),
        "modules": sorted(visited),
        "paths": {key: visited[key] for key in sorted(visited)},
        "migration_only_modules": migration_hits,
        "legacy_package_imports": legacy_hits,
        "parse_errors": sorted(parse_errors),
        "unresolved_local_symbols": sorted(missing_local),
        "compatibility_usage_zero": clean,
    }


def scan_active_browser_bundle(*, repo_root: str | Path, source_commit: str) -> dict[str, Any]:
    repo = _absolute(repo_root, field="repo_root")
    source_commit = _sha40(source_commit, "source_commit")
    manifest_raw = _git_file(repo, source_commit, "browser_extension_vnext/client_files.json")
    if manifest_raw is None:
        _fail("browser_source_missing", "active Browser client file manifest is missing")
    try:
        manifest = json.loads(manifest_raw)
    except json.JSONDecodeError as exc:
        raise M12aCompatibilityError("browser_source_invalid", "Browser client file manifest is invalid") from exc
    if not isinstance(manifest, Mapping) or manifest.get("schema") != "bdb-vnext-browser-client-files-v1":
        _fail("browser_source_invalid", "Browser client file manifest schema differs")
    client_files = manifest.get("files")
    if (
        not isinstance(client_files, list)
        or not client_files
        or any(not isinstance(item, str) or not item or Path(item).name != item for item in client_files)
        or len(client_files) != len(set(client_files))
        or "client_files.json" not in client_files
        or "manifest.json" not in client_files
        or "transport_worker.js" not in client_files
    ):
        _fail("browser_source_invalid", "Browser client file manifest has invalid entries")

    legacy_host_hits: list[str] = []
    fallback_hits: list[str] = []
    missing: list[str] = []
    for name in client_files:
        path = f"browser_extension_vnext/{name}"
        text = _git_file(repo, source_commit, path)
        if text is None:
            missing.append(path)
            continue
        if _LEGACY_HOST_LITERAL.search(text):
            legacy_host_hits.append(path)
        if re.search(r"legacy[_-]?fallback\s*[:=]\s*true", text, flags=re.IGNORECASE):
            fallback_hits.append(path)
    clean = not missing and not legacy_host_hits and not fallback_hits
    return {
        "source_commit": source_commit,
        "client_files": list(client_files),
        "missing_files": sorted(missing),
        "legacy_native_host_references": sorted(legacy_host_hits),
        "legacy_fallback_enabled": sorted(fallback_hits),
        "compatibility_usage_zero": clean,
    }


def _source_disposition(repo: Path, commit: str) -> dict[str, Any]:
    pyproject = _git_file(repo, commit, "pyproject.toml") or ""
    legacy_entrypoints = [
        name
        for name in ("bdb", "bdb-native-host", "bdb-operator", "bdb-control-center", "bdb-inventory")
        if re.search(rf"(?m)^{re.escape(name)}\s*=", pyproject)
    ]
    legacy_package_patterns = [
        token
        for token in ("bdb_bridge*", "bdb_operator*", "bdb_gui*", "bdb_poc*")
        if token in pyproject
    ]
    return {
        "legacy_entrypoints_present": legacy_entrypoints,
        "legacy_package_patterns_present": legacy_package_patterns,
        "disposition": "REMOVE_FROM_TARGET_ONLY_PACKAGE_IN_M12B",
        "presence_allowed_in_m12a": True,
    }


def _write_object(runtime: Path, payload: Mapping[str, Any]) -> tuple[str, Path]:
    document = canonical_json_bytes(dict(payload))
    if len(document) > _MAX_EVIDENCE_BYTES:
        _fail("evidence_too_large", "M12a evidence exceeds its bounded size")
    digest = semantic_digest(payload)
    root = runtime / "evidence" / "m12a-compatibility-zero" / "objects"
    path = root / f"{digest[7:]}.json"
    root.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            current = path.read_bytes()
        except OSError as exc:
            raise M12aCompatibilityError("evidence_read_failed", "existing M12a evidence cannot be read") from exc
        if current != document:
            _fail("evidence_digest_conflict", "content-addressed M12a evidence differs")
        return digest, path
    staging = root / f".{path.name}.partial-{uuid.uuid4().hex}"
    try:
        with staging.open("xb") as handle:
            handle.write(document)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, path)
    except OSError as exc:
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            pass
        raise M12aCompatibilityError("evidence_write_failed", "M12a evidence could not be published") from exc
    return digest, path


def _read_object(runtime: Path, digest: str) -> dict[str, Any]:
    _digest_field(digest, "evidence_sha256")
    path = runtime / "evidence" / "m12a-compatibility-zero" / "objects" / f"{digest[7:]}.json"
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise M12aCompatibilityError("evidence_missing", "M12a evidence object is unavailable") from exc
    if len(raw) > _MAX_EVIDENCE_BYTES:
        _fail("evidence_too_large", "M12a evidence object exceeds its bounded size")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise M12aCompatibilityError("evidence_invalid", "M12a evidence object is invalid JSON") from exc
    if not isinstance(value, Mapping) or semantic_digest(value) != digest:
        _fail("evidence_digest_mismatch", "M12a evidence object digest differs")
    return {str(key): item for key, item in value.items()}


def _deletion_plan(*, source_scan: Mapping[str, Any], browser_scan: Mapping[str, Any]) -> dict[str, Any]:
    entries = [
        {
            "bridge_id": "legacy-native-messaging-route",
            "usage_zero_required": True,
            "disposition": "DELETE_STALE_ROUTE_INSTALLER_SURFACES_IN_M12B",
        },
        {
            "bridge_id": "legacy-writer-profile-lifecycle",
            "usage_zero_required": True,
            "disposition": "ARCHIVE_READ_ONLY_HISTORY_AND_REMOVE_ACTIVE_WRITER_SURFACES_IN_M12B",
        },
        {
            "bridge_id": "legacy-spool-receipt-promoter-recovery",
            "usage_zero_required": True,
            "disposition": "ARCHIVE_OR_DROP_PER_FINAL_M12B_MANIFEST",
        },
        {
            "bridge_id": "migration-only-vnext-modules",
            "usage_zero_required": True,
            "disposition": "REMOVE_FROM_ACTIVE_TARGET_PACKAGE_IN_M12B",
        },
        {
            "bridge_id": "legacy-python-packages-entrypoints",
            "usage_zero_required": True,
            "disposition": "EXCLUDE_FROM_TARGET_ONLY_RELEASE_IN_M12B",
        },
        {
            "bridge_id": "legacy-installer-hotfix-scripts",
            "usage_zero_required": True,
            "disposition": "ARCHIVE_OR_DELETE_SOURCE_ONLY_SURFACES_IN_M12B",
        },
        {
            "bridge_id": "vnext-previous-recovery-slot",
            "usage_zero_required": False,
            "disposition": "RETAIN_AS_VNEXT_RECOVERY_NOT_LEGACY",
        },
    ]
    return {
        "schema": M12A_DELETION_PLAN_SCHEMA,
        "status": "PLANNED_NOT_APPLIED",
        "entries": entries,
        "active_python_compatibility_usage_zero": source_scan["compatibility_usage_zero"],
        "active_browser_compatibility_usage_zero": browser_scan["compatibility_usage_zero"],
        "final_deletion_performed": False,
        "production_mutation_performed": False,
    }


def capture_compatibility_zero(
    *,
    authority_root: str | Path,
    runtime_root: str | Path,
    client_runtime_root: str | Path | None = None,
    legacy_runtime_root: str | Path,
    repo_root: str | Path,
    observation_seconds: float = 2.0,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Capture the post-activation M12a gate without changing production state."""

    if observation_seconds < 0 or observation_seconds > 60:
        _fail("invalid_observation_window", "M12a observation window must be between 0 and 60 seconds")
    authority = _absolute(authority_root, field="authority_root")
    runtime = _absolute(runtime_root, field="runtime_root")
    client_runtime = _absolute(client_runtime_root or runtime_root, field="client_runtime_root")
    legacy = _absolute(legacy_runtime_root, field="legacy_runtime_root")
    repo = _absolute(repo_root, field="repo_root")

    try:
        bootstrap = observe_bootstrap_activation(authority_root=authority)
    except M11cCutoverError as exc:
        raise M12aCompatibilityError(exc.code, str(exc)) from exc
    if bootstrap.get("status") != "ACTIVE" or bootstrap.get("production_activation_performed") is not True:
        _fail("production_active_required", "M12a requires the exact post-M11c ACTIVE state")
    state = bootstrap["state"]
    active = bootstrap["slots"]["ACTIVE"]
    previous = bootstrap["slots"]["PREVIOUS"]
    if bootstrap["slots"].get("CANDIDATE") is not None:
        _fail("candidate_still_present", "M12a requires CANDIDATE to be empty")
    if not isinstance(previous, Mapping) or previous.get("known_good") is not True:
        _fail("previous_recovery_unavailable", "M12a requires an independently known-good PREVIOUS slot")

    client_gate = read_activation(runtime)
    if (
        client_gate is None
        or client_gate.state != "ACTIVE"
        or client_gate.writer_enabled is not True
        or client_gate.intake_enabled is not True
    ):
        _fail("client_gate_not_active", "M12a requires the subordinate M9b gate ACTIVE")

    try:
        client_plan = query_client_plan(runtime_root=client_runtime)["plan"]
        verification = require_client_verification(
            runtime_root=client_runtime,
            expected_client_plan_sha256=client_plan["client_plan_sha256"],
        )
        routes = observe_windows_native_routes(runtime_root=client_runtime)
    except M11cClientError as exc:
        raise M12aCompatibilityError(exc.code, str(exc)) from exc
    if routes.get("target_registered") is not True or routes.get("target_conflict") or routes.get("legacy_route_present"):
        _fail("native_route_not_exclusive", "M12a requires exclusive exact vNext Native route ownership")

    activation_id = state.get("activation_id")
    if not isinstance(activation_id, str) or not activation_id.startswith("m11c-"):
        _fail("activation_identity_invalid", "post-cutover activation identity differs")
    reconciliation: dict[str, Any] | None = None
    if activation_id.startswith("m11c-maint-"):
        maintenance_id = activation_id[len("m11c-maint-") :]
        try:
            reconciliation = verify_post_active_reconciliation(
                authority_root=authority,
                deployed_runtime_root=runtime,
                maintenance_id=maintenance_id,
                expected_plan_sha256=state["cutover_plan_sha256"],
            )
        except M9bReconciliationError as exc:
            raise M12aCompatibilityError(exc.code, str(exc)) from exc
        freeze_digest = _digest_field(reconciliation["plan"].get("m9a_freeze_digest"), "m9a_freeze_digest")
    else:
        cutover_id = activation_id[len("m11c-") :]
        cutover = query_cutover_plan(authority_root=authority, cutover_id=cutover_id)["plan"]
        if cutover.get("cutover_plan_sha256") != state.get("cutover_plan_sha256"):
            _fail("cutover_plan_binding_mismatch", "ACTIVE state binds a different immutable cutover plan")
        freeze_digest = _digest_field(cutover.get("m9a_freeze_digest"), "m9a_freeze_digest")

    try:
        first = revalidate_side_by_side_digest(
            runtime_root=runtime,
            legacy_runtime_root=legacy,
            freeze_digest=freeze_digest,
        )
        sleep_fn(observation_seconds)
        second = revalidate_side_by_side_digest(
            runtime_root=runtime,
            legacy_runtime_root=legacy,
            freeze_digest=freeze_digest,
        )
    except M9aHandoffError as exc:
        raise M12aCompatibilityError(exc.code, str(exc)) from exc
    runtime_zero = first == second == freeze_digest
    archive_verification = verify_side_by_side_archive(
        runtime_root=runtime,
        freeze_digest=freeze_digest,
    )
    archive_readable = archive_verification.get("archive_readable") is True
    admission_scan = scan_supported_vnext_admission_paths()
    admission_exclusive = (
        admission_scan.get("pass") is True
        and admission_scan.get("legacy_paths_supported") is False
        and admission_scan.get("alternate_accepting_writers") == []
    )

    active_source = _sha40(active.get("source_commit"), "active_source_commit")
    source_scan = scan_active_python_closure(repo_root=repo, source_commit=active_source)
    browser_scan = scan_active_browser_bundle(repo_root=repo, source_commit=active_source)
    source_disposition = _source_disposition(repo, active_source)
    deletion_plan = _deletion_plan(source_scan=source_scan, browser_scan=browser_scan)
    deletion_ref, deletion_path = _write_object(runtime, deletion_plan)

    bridge_matrix = [
        {
            "bridge_id": "legacy-native-messaging-route",
            "usage_zero": routes.get("legacy_route_present") is False and routes.get("target_registered") is True,
            "disposition": "DELETE_STALE_ROUTE_INSTALLER_SURFACES_IN_M12B",
        },
        {
            "bridge_id": "legacy-writer-profile-lifecycle",
            "usage_zero": runtime_zero,
            "disposition": "ARCHIVE_READ_ONLY_HISTORY_AND_REMOVE_ACTIVE_WRITER_SURFACES_IN_M12B",
        },
        {
            "bridge_id": "legacy-spool-receipt-promoter-recovery",
            "usage_zero": runtime_zero,
            "disposition": "ARCHIVE_OR_DROP_PER_FINAL_M12B_MANIFEST",
        },
        {
            "bridge_id": "migration-only-vnext-modules",
            "usage_zero": source_scan["compatibility_usage_zero"],
            "disposition": "REMOVE_FROM_ACTIVE_TARGET_PACKAGE_IN_M12B",
        },
        {
            "bridge_id": "legacy-python-packages-entrypoints",
            "usage_zero": not source_scan["legacy_package_imports"],
            "disposition": "EXCLUDE_FROM_TARGET_ONLY_RELEASE_IN_M12B",
        },
        {
            "bridge_id": "legacy-browser-fallback",
            "usage_zero": browser_scan["compatibility_usage_zero"],
            "disposition": "REMOVE_ANY_STALE_FALLBACK_SURFACES_IN_M12B",
        },
        {
            "bridge_id": "alternate-admission-writers",
            "usage_zero": admission_exclusive,
            "disposition": "RETAIN_ONLY_CANONICAL_M3C_WRITER_IN_M12B",
        },
    ]
    compatibility_zero = all(item["usage_zero"] is True for item in bridge_matrix)
    pass_closed = (
        compatibility_zero
        and archive_readable
        and verification.get("native_launch_verified") is True
        and client_plan.get("source_head") == active_source
        and client_gate.source_head == active_source
    )

    report = {
        "schema": M12A_REPORT_SCHEMA,
        "status": "PASS_CLOSED" if pass_closed else "BLOCKED",
        "scope": M12A_SCOPE,
        "activation_id": activation_id,
        "cutover_plan_sha256": state["cutover_plan_sha256"],
        "active_source_commit": active_source,
        "active_bundle_sha256": active["bundle_sha256"],
        "previous_source_commit": previous["source_commit"],
        "previous_bundle_sha256": previous["bundle_sha256"],
        "client_plan_sha256": client_plan["client_plan_sha256"],
        "client_verification_sha256": verification["verification_sha256"],
        "m9a_freeze_digest": freeze_digest,
        "runtime_zero_observed": runtime_zero,
        "archive_readable": archive_readable,
        "m9a_archive_verification": archive_verification,
        "native_route_exclusive": routes.get("legacy_route_present") is False and routes.get("target_registered") is True,
        "client_runtime_root": str(client_runtime),
        "m9b_reconciliation": reconciliation,
        "admission_path_scan": admission_scan,
        "active_python_closure": source_scan,
        "active_browser_closure": browser_scan,
        "source_only_legacy_surfaces": source_disposition,
        "bridge_matrix": bridge_matrix,
        "deletion_plan_sha256": deletion_ref,
        "deletion_plan_path": str(deletion_path),
        "compatibility_usage_zero": compatibility_zero,
        "unnamed_exceptions": [],
        "final_deletion_performed": False,
        "legacy_product_globally_disabled": False,
        "production_activation_performed": True,
        "production_mutation_performed": False,
    }
    report_ref, report_path = _write_object(runtime, report)
    return {
        "schema": M12A_RESULT_SCHEMA,
        "status": report["status"],
        "report_sha256": report_ref,
        "report_path": str(report_path),
        "deletion_plan_sha256": deletion_ref,
        "deletion_plan_path": str(deletion_path),
        "report": report,
        "production_activation_performed": True,
        "production_mutation_performed": False,
        "final_deletion_performed": False,
    }


def verify_compatibility_zero(*, runtime_root: str | Path, report_sha256: str) -> dict[str, Any]:
    runtime = _absolute(runtime_root, field="runtime_root")
    report = _read_object(runtime, _digest_field(report_sha256, "report_sha256"))
    if report.get("schema") != M12A_REPORT_SCHEMA or report.get("scope") != M12A_SCOPE:
        _fail("m12a_report_invalid", "M12a report identity differs")
    deletion_ref = _digest_field(report.get("deletion_plan_sha256"), "deletion_plan_sha256")
    deletion = _read_object(runtime, deletion_ref)
    if deletion.get("schema") != M12A_DELETION_PLAN_SCHEMA or deletion.get("status") != "PLANNED_NOT_APPLIED":
        _fail("m12a_deletion_plan_invalid", "M12a deletion plan identity differs")
    if report.get("status") == "PASS_CLOSED":
        if (
            report.get("compatibility_usage_zero") is not True
            or report.get("archive_readable") is not True
            or report.get("unnamed_exceptions") != []
            or report.get("final_deletion_performed") is not False
        ):
            _fail("m12a_report_invalid", "M12a PASS_CLOSED invariants differ")
    return {
        "schema": M12A_RESULT_SCHEMA,
        "status": report["status"],
        "report_sha256": report_sha256,
        "report": report,
        "deletion_plan": deletion,
        "production_mutation_performed": False,
        "final_deletion_performed": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BDB Next M12a compatibility-zero evidence gate")
    sub = parser.add_subparsers(dest="command", required=True)
    capture = sub.add_parser("capture")
    capture.add_argument("--authority-root", required=True)
    capture.add_argument("--runtime-root", required=True)
    capture.add_argument("--legacy-runtime-root", required=True)
    capture.add_argument("--repo-root", required=True)
    capture.add_argument("--observation-seconds", type=float, default=2.0)
    verify = sub.add_parser("verify")
    verify.add_argument("--runtime-root", required=True)
    verify.add_argument("--report-sha256", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "capture":
            result = capture_compatibility_zero(
                authority_root=args.authority_root,
                runtime_root=args.runtime_root,
                legacy_runtime_root=args.legacy_runtime_root,
                repo_root=args.repo_root,
                observation_seconds=args.observation_seconds,
            )
        else:
            result = verify_compatibility_zero(
                runtime_root=args.runtime_root,
                report_sha256=args.report_sha256,
            )
        print(canonical_json_bytes(result).decode("utf-8"))
        return 0 if result["status"] == "PASS_CLOSED" else 2
    except (M12aCompatibilityError, M11cCutoverError, M11cClientError, M9aHandoffError, M9bActivationError) as exc:
        print(
            canonical_json_bytes(
                {
                    "schema": M12A_RESULT_SCHEMA,
                    "status": "BLOCKED",
                    "error_code": getattr(exc, "code", "m12a_failed"),
                    "error": str(exc),
                    "details": getattr(exc, "details", {}),
                    "production_mutation_performed": False,
                    "final_deletion_performed": False,
                }
            ).decode("utf-8")
        )
        return 2


__all__ = [
    "M12A_DELETION_PLAN_SCHEMA",
    "M12A_REPORT_SCHEMA",
    "M12A_RESULT_SCHEMA",
    "M12A_SCOPE",
    "M12aCompatibilityError",
    "capture_compatibility_zero",
    "scan_active_browser_bundle",
    "scan_active_python_closure",
    "verify_compatibility_zero",
]


if __name__ == "__main__":
    raise SystemExit(main())

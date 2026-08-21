"""M12b exact deletion subject and fail-closed readiness boundary.

M12a proves compatibility usage zero but deliberately performs no deletion.
This module turns that proof into a second, immutable, exact-plan-bound subject.
It observes the external Bootstrap/M9b/M3c/client authorities, records every
physical reference that must be retained, and exposes a guarded apply boundary
for a later, separately approved operation.  ``prepare`` and ``verify`` are
non-destructive; no caller may infer permission to delete from the M12a report
alone.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import uuid
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, NoReturn

from bdb_shared.evidence import canonical_json_bytes, semantic_digest


M12B_SUBJECT_SCHEMA = "bdb-vnext-m12b-deletion-subject-v1"
M12B_PLAN_SCHEMA = "bdb-vnext-m12b-deletion-plan-v1"
M12B_RESULT_SCHEMA = "bdb-vnext-m12b-readiness-result-v1"
M12B_SCOPE = "target-only-final-deletion-v1"
M12B_PLAN_STATUS = "PREFLIGHT_READY"
M12B_BLOCKED_STATUS = "PREFLIGHT_BLOCKED"
M12B_APPROVAL_TOKEN = "M12B-DELETE-APPROVED"
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/).+$")
_MAX_OBJECT_BYTES = 4 * 1024 * 1024

_CATEGORIES = frozenset(
    {
        "ACTIVE_PRODUCTION_REQUIRED",
        "PREVIOUS_RECOVERY_REQUIRED",
        "IMMUTABLE_EVIDENCE",
        "SOURCE_ONLY",
        "LEGACY_COMPATIBLE_USAGE_ZERO",
        "DISPOSABLE",
        "UNKNOWN_MUST_NOT_DELETE",
    }
)


class M12bDeletionError(RuntimeError):
    """Typed, fail-closed M12b error."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def _fail(code: str, message: str, *, details: Mapping[str, Any] | None = None) -> NoReturn:
    raise M12bDeletionError(code, message, details=details)


def _digest(value: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(dict(value))).hexdigest()


def _check_digest(value: object, field: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        _fail("m12b_invalid_digest", f"{field} is not an exact sha256 digest")
    return value


def _check_sha40(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA40.fullmatch(value) is None:
        _fail("m12b_invalid_source_identity", f"{field} is not an exact Git SHA")
    return value


def _absolute(value: str | Path, *, field: str, must_exist: bool = True) -> Path:
    path = Path(value).expanduser().absolute()
    if not _ABSOLUTE.fullmatch(str(path)):
        _fail("m12b_invalid_path", f"{field} must be absolute")
    if must_exist and (path.is_symlink() or not path.exists()):
        _fail("m12b_path_unavailable", f"{field} is unavailable")
    return path


def _json(path: Path, *, field: str) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        if len(raw) > _MAX_OBJECT_BYTES:
            _fail("m12b_record_too_large", f"{field} exceeds the bounded record size")
        value = json.loads(raw.decode("utf-8"))
    except M12bDeletionError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _fail("m12b_record_unavailable", f"{field} is unavailable or invalid", details={"path": str(path)})
    if not isinstance(value, dict):
        _fail("m12b_record_invalid", f"{field} must be a JSON object")
    return {str(key): item for key, item in value.items()}


def _write_immutable(path: Path, document: Mapping[str, Any], *, field: str) -> None:
    payload = canonical_json_bytes(document)
    if len(payload) > _MAX_OBJECT_BYTES:
        _fail("m12b_record_too_large", f"{field} exceeds the bounded record size")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = _json(path, field=field)
        if existing != dict(document):
            _fail("m12b_immutable_conflict", f"{field} already exists with different bytes")
        return
    if path.is_symlink():
        _fail("m12b_symlink_path", f"{field} path is a symlink")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(payload)
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        _fail("m12b_record_write_failed", f"{field} could not be published")
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass


def _source_classification(surface_class: object) -> tuple[str, str, bool]:
    """Return category, proposed target-only action, and delete eligibility."""

    legacy = {
        "LEGACY_RUNTIME_PACKAGE",
        "LEGACY_OPERATOR_PACKAGE",
        "LEGACY_UI_PACKAGE",
        "LEGACY_POC_PACKAGE",
        "LEGACY_BROWSER_PACKAGE",
        "LEGACY_NATIVE_PACKAGING_ENTRY",
    }
    source_only = {
        "INSTALLER_WRAPPER_SCRIPT",
        "BENCHMARK_COMPATIBILITY_EVIDENCE",
        "TEST_ONLY_COMPATIBILITY_REFERENCE",
        "DOCUMENTATION_HISTORY_REFERENCE",
        "CI_COMPATIBILITY_REFERENCE",
        "CONTRACT_SCHEMA_REFERENCE",
    }
    if surface_class in legacy:
        return "LEGACY_COMPATIBLE_USAGE_ZERO", "EXCLUDE_FROM_TARGET_ONLY_RELEASE", False
    if surface_class in source_only:
        return "SOURCE_ONLY", "ARCHIVE_OR_RETAIN_SOURCE_ONLY", False
    if surface_class in {"TARGET_BROWSER_PACKAGE", "TARGET_NATIVE_PACKAGING_ENTRY"}:
        return "ACTIVE_PRODUCTION_REQUIRED", "RETAIN_IN_TARGET_RELEASE", False
    return "UNKNOWN_MUST_NOT_DELETE", "RETAIN_UNTIL_EXACT_TARGET_CLOSURE", False


def _classify_inventory(entries: Sequence[Mapping[str, Any]], *, active_source_paths: Iterable[str] = ()) -> tuple[list[dict[str, Any]], list[str]]:
    classified: list[dict[str, Any]] = []
    unknown: list[str] = []
    active_paths = {str(path) for path in active_source_paths}
    for raw in entries:
        path = raw.get("path")
        if not isinstance(path, str) or not path:
            _fail("m12b_inventory_invalid", "every M12a inventory entry needs a path")
        if path in active_paths:
            category, action, eligible = "ACTIVE_PRODUCTION_REQUIRED", "RETAIN_IN_ACTIVE_CLOSURE", False
        else:
            category, action, eligible = _source_classification(raw.get("surface_class"))
        item = {
            "path": path,
            "surface_class": raw.get("surface_class"),
            "m12a_disposition": raw.get("disposition"),
            "sha256": raw.get("sha256"),
            "category": category,
            "proposed_action": action,
            "delete_eligible": eligible,
        }
        classified.append(item)
        if category == "UNKNOWN_MUST_NOT_DELETE":
            unknown.append(path)
    return classified, unknown


def _check_physical_references(references: Iterable[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    result: list[dict[str, Any]] = []
    unknown: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in references:
        path = raw.get("path")
        category = raw.get("category")
        authority = raw.get("authority")
        if not isinstance(path, str) or not _ABSOLUTE.match(path):
            _fail("m12b_reference_invalid", "physical references must use absolute paths")
        if category not in _CATEGORIES or not isinstance(authority, str) or not authority:
            _fail("m12b_reference_invalid", "physical references need a category and authority")
        key = (path.casefold(), str(category), authority)
        if key in seen:
            continue
        seen.add(key)
        item = {
            "path": path,
            "category": category,
            "authority": authority,
            "exists": Path(path).exists(),
            "sha256": raw.get("sha256"),
            "required": raw.get("required", True),
        }
        result.append(item)
        if category == "UNKNOWN_MUST_NOT_DELETE":
            unknown.append(path)
    return result, unknown


def build_m12b_subject(
    *,
    subject_id: str,
    closure_report: Mapping[str, Any],
    deletion_plan: Mapping[str, Any],
    source_commit: str,
    source_tree: str,
    bootstrap: Mapping[str, Any],
    m9b: Mapping[str, Any],
    m3c: Mapping[str, Any],
    client: Mapping[str, Any],
    route_rebind: Mapping[str, Any],
    physical_references: Iterable[Mapping[str, Any]],
    active_source_paths: Iterable[str] = (),
    native_routes: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an exact subject/plan from already-canonical M12a/M11c records."""

    if not isinstance(subject_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", subject_id):
        _fail("m12b_subject_id_invalid", "subject_id is not a stable bounded identifier")
    _check_sha40(source_commit, "source_commit")
    _check_sha40(source_tree, "source_tree")
    if closure_report.get("schema") != "bdb-vnext-m12a-full-closure-report-v1":
        _fail("m12b_closure_invalid", "M12a closure schema differs")
    if closure_report.get("status") != "PASS_CLOSED" or closure_report.get("m12b_unlocked") is not True:
        _fail("m12b_closure_not_unlocked", "M12a closure does not unlock M12b")
    if closure_report.get("production_mutation_performed") is not False or closure_report.get("final_deletion_performed") is not False:
        _fail("m12b_closure_has_effect", "M12a closure reports a forbidden production effect")
    closure_digest = _check_digest(closure_report.get("closure_report_sha256"), "closure_report_sha256") if closure_report.get("closure_report_sha256") else None
    deletion_digest = _check_digest(closure_report.get("deletion_plan_sha256"), "deletion_plan_sha256")
    if deletion_plan.get("schema") != "bdb-vnext-m12a-deletion-plan-v1" or deletion_plan.get("status") != "PLANNED_NOT_APPLIED":
        _fail("m12b_deletion_plan_invalid", "M12a deletion plan is not planned-only")
    if semantic_digest(deletion_plan) != deletion_digest:
        _fail("m12b_deletion_plan_digest_mismatch", "M12a deletion plan digest differs")
    if closure_report.get("active_source_commit") != source_commit:
        _fail("m12b_source_identity_mismatch", "M12a and candidate source commits differ")

    state = bootstrap.get("state")
    slots = bootstrap.get("slots")
    if bootstrap.get("status") != "ACTIVE" or not isinstance(state, Mapping) or not isinstance(slots, Mapping):
        _fail("m12b_bootstrap_not_active", "Bootstrap ACTIVE observation is required")
    active = slots.get("ACTIVE")
    previous = slots.get("PREVIOUS")
    if not isinstance(active, Mapping) or not isinstance(previous, Mapping):
        _fail("m12b_slot_observation_invalid", "ACTIVE and PREVIOUS manifests are required")
    for item, name in ((active, "ACTIVE"), (previous, "PREVIOUS")):
        if item.get("source_commit") is None:
            _fail("m12b_slot_observation_invalid", f"{name} manifest has no source identity")
    state_sha = _check_digest(state.get("state_sha256"), "bootstrap_state_sha256")
    active_sha = _check_digest(state.get("active_manifest_sha256"), "active_manifest_sha256")
    previous_sha = _check_digest(state.get("previous_manifest_sha256"), "previous_manifest_sha256")
    if active.get("source_commit") != source_commit:
        _fail("m12b_active_source_mismatch", "ACTIVE source differs from M12a source")

    required = {"plan_sha256", "state_sha256", "target_m9b_record_sha256"}
    if not required.issubset(m9b):
        _fail("m12b_m9b_observation_incomplete", "M9b reconciliation observation is incomplete")
    for field in ("plan_sha256", "state_sha256", "target_m9b_record_sha256"):
        _check_digest(m9b[field], f"m9b_{field}")
    for field in ("control_digest", "kill_switch_digest"):
        _check_digest(m3c.get(field), f"m3c_{field}")
    for field in ("client_plan_sha256", "verification_sha256"):
        _check_digest(client.get(field), f"client_{field}")
    for field in ("plan_sha256", "state_sha256"):
        _check_digest(route_rebind.get(field), f"route_rebind_{field}")

    inventory = closure_report.get("compatibility_inventory")
    entries = inventory.get("entries") if isinstance(inventory, Mapping) else None
    if not isinstance(entries, list) or inventory.get("inventory_complete") is not True:
        _fail("m12b_inventory_incomplete", "M12a compatibility inventory is incomplete")
    classified, inventory_unknown = _classify_inventory(entries, active_source_paths=active_source_paths)
    refs, reference_unknown = _check_physical_references(physical_references)
    unknown = sorted(set(inventory_unknown + reference_unknown))
    if native_routes is not None and (
        native_routes.get("target_registered") is not True
        or native_routes.get("legacy_route_present") is True
    ):
        unknown.append("HKCU Native Messaging target/legacy route readback")
        unknown = sorted(set(unknown))
    categories = Counter(item["category"] for item in classified + refs)

    subject_payload = {
        "schema": M12B_SUBJECT_SCHEMA,
        "scope": M12B_SCOPE,
        "subject_id": subject_id,
        "source_commit": source_commit,
        "source_tree": source_tree,
        "closure_report_sha256": closure_digest,
        "m12a_deletion_plan_sha256": deletion_digest,
        "bootstrap": {
            "state_sha256": state_sha,
            "active_manifest_sha256": active_sha,
            "previous_manifest_sha256": previous_sha,
            "runtime_id": state.get("runtime_id"),
            "generation_id": state.get("generation_id"),
            "legacy_runtime_root": state.get("legacy_runtime_root"),
        },
        "m9b": dict(m9b),
        "m3c": dict(m3c),
        "client": dict(client),
        "route_rebind": dict(route_rebind),
        "native_routes": dict(native_routes or {}),
        "inventory": classified,
        "physical_references": refs,
        "category_counts": {key: categories.get(key, 0) for key in sorted(_CATEGORIES)},
        "unknown_paths": unknown,
        "production_deletion_performed": False,
    }
    subject_sha = _digest(subject_payload)
    plan_payload = {
        "schema": M12B_PLAN_SCHEMA,
        "scope": M12B_SCOPE,
        "subject_id": subject_id,
        "subject_sha256": subject_sha,
        "m12a_closure_sha256": closure_digest,
        "m12a_deletion_plan_sha256": deletion_digest,
        "status": M12B_BLOCKED_STATUS if unknown else M12B_PLAN_STATUS,
        "operator_approval_required": True,
        "apply_mode": "EXACT_PATH_BOUND_FAIL_CLOSED",
        "candidate_targets": [],
        "unknown_paths": unknown,
        "protected_categories": [
            "ACTIVE_PRODUCTION_REQUIRED",
            "PREVIOUS_RECOVERY_REQUIRED",
            "IMMUTABLE_EVIDENCE",
            "LEGACY_COMPATIBLE_USAGE_ZERO",
        ],
        "production_deletion_performed": False,
        "subject": subject_payload,
    }
    plan_sha = _digest(plan_payload)
    return {
        "schema": M12B_RESULT_SCHEMA,
        "status": plan_payload["status"],
        "subject_sha256": subject_sha,
        "plan_sha256": plan_sha,
        "subject": {**subject_payload, "subject_sha256": subject_sha},
        "plan": {**plan_payload, "plan_sha256": plan_sha},
        "production_deletion_performed": False,
    }


def _git_tree(repo: Path, commit: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", f"{commit}^{{tree}}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        _fail("m12b_source_unavailable", "source tree identity could not be observed")
    return completed.stdout.decode("ascii").strip()


def _recursive_paths(value: object, *, category: str, authority: str, out: list[dict[str, Any]]) -> None:
    if isinstance(value, Mapping):
        for item in value.values():
            _recursive_paths(item, category=category, authority=authority, out=out)
    elif isinstance(value, list):
        for item in value:
            _recursive_paths(item, category=category, authority=authority, out=out)
    elif isinstance(value, str) and _ABSOLUTE.match(value):
        out.append({"path": value, "category": category, "authority": authority, "required": True})


def _m3c(runtime: Path) -> dict[str, Any]:
    control = _json(runtime / "control" / "m3c-control.json", field="m3c_control")
    kill = _json(runtime / "control" / "m3c-kill-switch.json", field="m3c_kill_switch")
    return {
        "control_digest": semantic_digest(control),
        "kill_switch_digest": semantic_digest(kill),
        "authority_id": control.get("authority_id"),
        "writer_id": control.get("writer_id"),
        "mode": control.get("mode"),
        "admission_enabled": kill.get("admission_enabled"),
    }


def prepare_m12b_readiness(
    *,
    authority_root: str | Path,
    runtime_root: str | Path,
    client_runtime_root: str | Path,
    legacy_runtime_root: str | Path,
    repo_root: str | Path,
    subject_id: str,
    closure_report_sha256: str,
    deletion_plan_sha256: str,
) -> dict[str, Any]:
    """Observe canonical M12a/M11c authorities and publish no deletion."""

    authority = _absolute(authority_root, field="authority_root")
    runtime = _absolute(runtime_root, field="runtime_root")
    client_runtime = _absolute(client_runtime_root, field="client_runtime_root")
    legacy = _absolute(legacy_runtime_root, field="legacy_runtime_root")
    repo = _absolute(repo_root, field="repo_root")
    _check_digest(closure_report_sha256, "closure_report_sha256")
    _check_digest(deletion_plan_sha256, "deletion_plan_sha256")

    from bdb_vnext.m11c_active_reader import observe_bootstrap_activation
    from bdb_vnext.m12a_closure import verify_full_closure
    from bdb_vnext.m11c_windows_clients import query_client_plan
    from bdb_vnext.m11c_windows_clients import observe_windows_native_routes
    from bdb_vnext.m11c_client_route_rebind import query_client_route_rebind
    from bdb_vnext.m9b_reconciliation import query_post_active_reconciliation

    closure_result = verify_full_closure(runtime_root=runtime, closure_report_sha256=closure_report_sha256)
    closure = dict(closure_result["report"])
    deletion = dict(closure_result["base_report"])  # populated below from the verified base result
    from bdb_vnext.m12a_compatibility_zero import verify_compatibility_zero

    base = verify_compatibility_zero(runtime_root=runtime, report_sha256=closure["base_report_sha256"])
    deletion = dict(base["deletion_plan"])
    active_source_paths = base["report"].get("active_python_closure", {}).get("paths", {}).values()
    if semantic_digest(deletion) != deletion_plan_sha256:
        _fail("m12b_deletion_plan_digest_mismatch", "supplied deletion plan does not match M12a closure")
    bootstrap = observe_bootstrap_activation(authority_root=authority)
    state = bootstrap.get("state") or {}
    active_sha = str(state.get("active_manifest_sha256", ""))
    previous_sha = str(state.get("previous_manifest_sha256", ""))
    maintenance_id = str(state.get("activation_id", "")).removeprefix("m11c-maint-")
    if not maintenance_id:
        _fail("m12b_maintenance_identity_missing", "ACTIVE Bootstrap has no maintenance identity")
    m9b_result = query_post_active_reconciliation(authority_root=authority, maintenance_id=maintenance_id)
    m9b = {
        "plan_sha256": m9b_result.get("plan", {}).get("plan_sha256"),
        "state_sha256": m9b_result.get("state", {}).get("state_sha256"),
        "target_m9b_record_sha256": m9b_result.get("subject", {}).get("target_m9b_record_sha256")
        or m9b_result.get("plan", {}).get("target_m9b_record_sha256"),
        "maintenance_id": maintenance_id,
    }
    m9b_plan = m9b_result.get("plan", {})
    m9b["target_m9b_record_sha256"] = m9b.get("target_m9b_record_sha256") or m9b_plan.get("target_m9b_record_sha256")
    client_result = query_client_plan(runtime_root=client_runtime)
    client_plan = client_result["plan"]
    native_routes = observe_windows_native_routes(runtime_root=client_runtime)
    verification_path = client_runtime / "clients" / "browser-client-verification.json"
    verification = _json(verification_path, field="client_verification")
    client = {
        "client_plan_sha256": client_plan.get("client_plan_sha256"),
        "verification_sha256": verification.get("verification_sha256"),
        "source_head": client_plan.get("source_head"),
        "source_tree": client_plan.get("source_tree"),
        "browser_bundle_digest": client_plan.get("browser_bundle_digest"),
        "native_manifest_sha256": client_plan.get("native_manifest_sha256"),
        "native_manifest_path": client_plan.get("native_manifest_path"),
        "native_routes": native_routes,
    }
    rebind_id = str(m9b_plan.get("route_rebind_id", ""))
    if not rebind_id:
        _fail("m12b_route_rebind_identity_missing", "M9b plan has no route-rebind identity")
    rebind_result = query_client_route_rebind(authority_root=authority, rebind_id=rebind_id)
    rebind_plan = rebind_result.get("plan", {})
    rebind = {
        "rebind_id": rebind_id,
        "plan_sha256": rebind_plan.get("route_rebind_plan_sha256") or rebind_plan.get("plan_sha256"),
        "state_sha256": rebind_result.get("state", {}).get("state_sha256"),
    }
    m3c = _m3c(runtime)
    references: list[dict[str, Any]] = []
    _recursive_paths(bootstrap.get("state", {}), category="ACTIVE_PRODUCTION_REQUIRED", authority="bootstrap-state", out=references)
    _recursive_paths(bootstrap.get("slots", {}).get("ACTIVE", {}), category="ACTIVE_PRODUCTION_REQUIRED", authority="bootstrap-active", out=references)
    _recursive_paths(bootstrap.get("slots", {}).get("PREVIOUS", {}), category="PREVIOUS_RECOVERY_REQUIRED", authority="bootstrap-previous", out=references)
    _recursive_paths(m9b_result, category="ACTIVE_PRODUCTION_REQUIRED", authority="m9b-reconciliation", out=references)
    _recursive_paths(client_plan, category="ACTIVE_PRODUCTION_REQUIRED", authority="client-plan", out=references)
    _recursive_paths(verification, category="ACTIVE_PRODUCTION_REQUIRED", authority="client-verification", out=references)
    _recursive_paths(rebind_result, category="ACTIVE_PRODUCTION_REQUIRED", authority="route-rebind", out=references)
    _recursive_paths(m3c, category="ACTIVE_PRODUCTION_REQUIRED", authority="m3c-control", out=references)
    _recursive_paths(native_routes, category="ACTIVE_PRODUCTION_REQUIRED", authority="native-route-readback", out=references)
    references.extend(
        [
            {"path": str(authority / "bootstrap.lock"), "category": "ACTIVE_PRODUCTION_REQUIRED", "authority": "bootstrap-lock"},
            {"path": str(authority / "slot-state.json"), "category": "ACTIVE_PRODUCTION_REQUIRED", "authority": "bootstrap-slot-state"},
            {"path": str(authority / "slot-manifests" / f"{active_sha[7:]}.json"), "category": "ACTIVE_PRODUCTION_REQUIRED", "authority": "bootstrap-active-manifest"},
            {"path": str(authority / "slot-manifests" / f"{previous_sha[7:]}.json"), "category": "PREVIOUS_RECOVERY_REQUIRED", "authority": "bootstrap-previous-manifest"},
            {"path": str(runtime / "control" / "control.db"), "category": "ACTIVE_PRODUCTION_REQUIRED", "authority": "control-store"},
            {"path": str(runtime / "control" / "control.db.seal.json"), "category": "ACTIVE_PRODUCTION_REQUIRED", "authority": "control-store-seal"},
            {"path": str(runtime / "control" / "m3c-control.json"), "category": "ACTIVE_PRODUCTION_REQUIRED", "authority": "m3c-control"},
            {"path": str(runtime / "control" / "m3c-kill-switch.json"), "category": "ACTIVE_PRODUCTION_REQUIRED", "authority": "m3c-kill-switch"},
            {"path": str(runtime / "evidence" / "m12a-closure" / "objects" / f"{closure_report_sha256[7:]}.json"), "category": "IMMUTABLE_EVIDENCE", "authority": "m12a-closure"},
            {"path": str(runtime / "evidence" / "m12a-compatibility-zero" / "objects" / f"{closure['base_report_sha256'][7:]}.json"), "category": "IMMUTABLE_EVIDENCE", "authority": "m12a-base-report"},
            {"path": str(runtime / "evidence" / "m12a-compatibility-zero" / "objects" / f"{deletion_plan_sha256[7:]}.json"), "category": "IMMUTABLE_EVIDENCE", "authority": "m12a-deletion-plan"},
            {"path": str(authority / "maintenance" / "m9b-reconciliation" / "plans" / f"{maintenance_id}.json"), "category": "IMMUTABLE_EVIDENCE", "authority": "m9b-plan"},
            {"path": str(authority / "maintenance" / "m9b-reconciliation" / "states" / f"{maintenance_id}.json"), "category": "IMMUTABLE_EVIDENCE", "authority": "m9b-state"},
            {"path": str(authority / "maintenance" / "client-route-rebind" / "plans" / f"{rebind_id}.json"), "category": "IMMUTABLE_EVIDENCE", "authority": "route-rebind-plan"},
            {"path": str(authority / "maintenance" / "client-route-rebind" / "states" / f"{rebind_id}.json"), "category": "IMMUTABLE_EVIDENCE", "authority": "route-rebind-state"},
            {"path": str(runtime / "evidence"), "category": "IMMUTABLE_EVIDENCE", "authority": "m12a-m12b-evidence"},
            {"path": str(client_runtime / "evidence"), "category": "IMMUTABLE_EVIDENCE", "authority": "client-evidence"},
            {"path": str(legacy), "category": "LEGACY_COMPATIBLE_USAGE_ZERO", "authority": "bootstrap-legacy-root"},
        ]
    )
    source_commit = str(closure.get("active_source_commit"))
    source_tree = _git_tree(repo, source_commit)
    result = build_m12b_subject(
        subject_id=subject_id,
        closure_report={**closure, "closure_report_sha256": closure_report_sha256},
        deletion_plan=deletion,
        source_commit=source_commit,
        source_tree=source_tree,
        bootstrap=bootstrap,
        m9b=m9b,
        m3c=m3c,
        client=client,
        route_rebind=rebind,
        physical_references=references,
        active_source_paths=active_source_paths,
        native_routes=native_routes,
    )
    evidence_root = runtime / "evidence" / "m12b-readiness"
    subject_path = evidence_root / "objects" / f"{result['subject_sha256'][7:]}.json"
    plan_path = evidence_root / "plans" / f"{subject_id}.json"
    _write_immutable(subject_path, result["subject"], field="m12b_subject")
    _write_immutable(plan_path, result["plan"], field="m12b_plan")
    return {
        **result,
        "subject_path": str(subject_path),
        "plan_path": str(plan_path),
        "production_deletion_performed": False,
    }


def verify_m12b_plan(*, plan_path: str | Path, expected_plan_sha256: str | None = None) -> dict[str, Any]:
    path = _absolute(plan_path, field="plan_path")
    plan = _json(path, field="m12b_plan")
    supplied = _check_digest(plan.get("plan_sha256"), "plan_sha256")
    payload = dict(plan)
    payload.pop("plan_sha256", None)
    if _digest(payload) != supplied:
        _fail("m12b_plan_digest_mismatch", "M12b plan digest differs")
    if expected_plan_sha256 is not None and supplied != _check_digest(expected_plan_sha256, "expected_plan_sha256"):
        _fail("m12b_plan_identity_mismatch", "M12b plan is not the expected immutable subject")
    if plan.get("production_deletion_performed") is not False:
        _fail("m12b_plan_has_effect", "M12b plan reports a production effect")
    return {"schema": M12B_RESULT_SCHEMA, "status": plan.get("status"), "plan_sha256": supplied, "plan": plan, "production_deletion_performed": False}


def apply_m12b_deletion(*, plan_path: str | Path, approval_token: str, dry_run: bool = True) -> dict[str, Any]:
    """Guarded future effect boundary; this run must use ``dry_run=True``."""

    verified = verify_m12b_plan(plan_path=plan_path)
    plan = verified["plan"]
    if approval_token != M12B_APPROVAL_TOKEN:
        _fail("m12b_operator_approval_required", "exact M12b operator approval is required")
    if plan.get("status") != M12B_PLAN_STATUS or plan.get("unknown_paths"):
        _fail("m12b_plan_not_ready", "M12b plan contains unresolved protected/unknown paths")
    if not dry_run:
        _fail("m12b_destructive_effect_disabled", "production deletion is disabled in this readiness slice")
    return {
        "schema": M12B_RESULT_SCHEMA,
        "status": "DRY_RUN_ONLY",
        "plan_sha256": verified["plan_sha256"],
        "deleted_paths": [],
        "production_deletion_performed": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BDB Next M12b exact deletion readiness")
    sub = parser.add_subparsers(dest="command", required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--plan", required=True)
    verify.add_argument("--expected-plan-sha256")
    dry = sub.add_parser("apply")
    dry.add_argument("--plan", required=True)
    dry.add_argument("--approval-token", required=True)
    dry.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "verify":
            result = verify_m12b_plan(plan_path=args.plan, expected_plan_sha256=args.expected_plan_sha256)
        else:
            result = apply_m12b_deletion(plan_path=args.plan, approval_token=args.approval_token, dry_run=args.dry_run)
    except M12bDeletionError as exc:
        print(json.dumps({"schema": M12B_RESULT_SCHEMA, "status": "FAIL", "error_code": exc.code, "message": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


__all__ = [
    "M12B_APPROVAL_TOKEN",
    "M12B_BLOCKED_STATUS",
    "M12B_PLAN_SCHEMA",
    "M12B_PLAN_STATUS",
    "M12B_RESULT_SCHEMA",
    "M12B_SCOPE",
    "M12B_SUBJECT_SCHEMA",
    "M12bDeletionError",
    "apply_m12b_deletion",
    "build_m12b_subject",
    "main",
    "prepare_m12b_readiness",
    "verify_m12b_plan",
]

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
import stat
import subprocess
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, NoReturn

from bdb_shared.evidence import canonical_json_bytes, semantic_digest


M12B_SUBJECT_SCHEMA = "bdb-vnext-m12b-deletion-subject-v1"
M12B_PLAN_SCHEMA = "bdb-vnext-m12b-deletion-plan-v1"
M12B_RESULT_SCHEMA = "bdb-vnext-m12b-readiness-result-v1"
M12B_JOURNAL_SCHEMA = "bdb-vnext-m12b-deletion-journal-v1"
M12B_SCOPE = "target-only-final-deletion-v1"
M12B_PLAN_STATUS = "PREFLIGHT_READY"
M12B_BLOCKED_STATUS = "PREFLIGHT_BLOCKED"
M12B_APPROVAL_TOKEN = "M12B-DELETE-APPROVED"
M12B_JOURNAL_STATES = frozenset({"PREPARED", "APPLYING", "PARTIAL", "COMPLETED", "BLOCKED"})
M12B_PRODUCTION_OBSERVATION_SCHEMA = "bdb-vnext-m12b-production-acceptance-observation-v1"
_PROTECTED_CATEGORIES = frozenset(
    {
        "ACTIVE_PRODUCTION_REQUIRED",
        "PREVIOUS_RECOVERY_REQUIRED",
        "IMMUTABLE_EVIDENCE",
        "LEGACY_COMPATIBLE_USAGE_ZERO",
    }
)
_ALLOWED_PHYSICAL_ACTIONS = frozenset({"DELETE_FILE", "REMOVE_EMPTY_DIRECTORY"})
_ALLOWED_PHYSICAL_DISPOSITIONS = frozenset(
    {
        "DELETE_STALE_ROUTE_INSTALLER_SURFACES_IN_M12B",
        "ARCHIVE_OR_DROP_PER_FINAL_M12B_MANIFEST",
        "ARCHIVE_OR_DELETE_SOURCE_ONLY_SURFACES_IN_M12B",
        "REMOVE_ANY_STALE_FALLBACK_SURFACES_IN_M12B",
    }
)
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


def _production_acceptance_observation(
    *,
    execution_scope: str,
    production_observation: Mapping[str, Any] | None,
    production_acceptance: bool,
) -> tuple[bool, dict[str, Any]]:
    """Resolve acceptance from an exact observation, never from a production flag."""

    if execution_scope == "fixture":
        if production_observation is not None:
            return production_observation.get("production_acceptance") is True, dict(production_observation)
        return production_acceptance is True, {
            "schema": M12B_PRODUCTION_OBSERVATION_SCHEMA,
            "source": "fixture",
            "production_acceptance": production_acceptance is True,
        }
    if production_acceptance is True and production_observation is None:
        _fail(
            "m12b_production_observation_required",
            "production acceptance must come from the canonical read-only observation",
        )
    if not isinstance(production_observation, Mapping):
        return False, {
            "schema": M12B_PRODUCTION_OBSERVATION_SCHEMA,
            "source": "missing",
            "production_acceptance": False,
            "reason": "canonical production observation unavailable",
        }
    observation = dict(production_observation)
    if observation.get("schema") != M12B_PRODUCTION_OBSERVATION_SCHEMA:
        return False, {
            **observation,
            "schema": M12B_PRODUCTION_OBSERVATION_SCHEMA,
            "production_acceptance": False,
            "reason": "canonical production observation schema mismatch",
        }
    return observation.get("production_acceptance") is True, observation


def _absolute(value: str | Path, *, field: str, must_exist: bool = True) -> Path:
    path = Path(value).expanduser().absolute()
    if not _ABSOLUTE.fullmatch(str(path)):
        _fail("m12b_invalid_path", f"{field} must be absolute")
    if must_exist and (path.is_symlink() or not path.exists()):
        _fail("m12b_path_unavailable", f"{field} is unavailable")
    return path


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        _fail("m12b_target_unavailable", "target bytes could not be observed", details={"path": str(path)})
    return "sha256:" + digest.hexdigest()


def _is_reparse_point(path: Path) -> bool:
    """Reject symlinks/junctions/reparse points before any effect."""

    if path.is_symlink():
        return True
    try:
        attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _path_key(path: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(Path(path).absolute())))


def _is_same_or_child(path: str | Path, root: str | Path) -> bool:
    try:
        return os.path.commonpath([_path_key(path), _path_key(root)]) == _path_key(root)
    except ValueError:
        return False


def _observe_target(path: Path) -> dict[str, Any]:
    if _is_reparse_point(path):
        _fail("m12b_target_reparse_point", "target is a symlink/junction/reparse point", details={"path": str(path)})
    if not path.exists():
        return {"exists": False, "kind": "missing"}
    if path.is_file():
        return {
            "exists": True,
            "kind": "file",
            "size_bytes": path.stat().st_size,
            "sha256": _sha256_path(path),
        }
    if path.is_dir():
        entries = sorted(item.name for item in path.iterdir())
        if len(entries) > 4096:
            _fail("m12b_directory_too_large", "directory target exceeds bounded entry count", details={"path": str(path)})
        return {"exists": True, "kind": "directory", "entries": entries}
    _fail("m12b_target_type_unsupported", "target is neither a regular file nor directory", details={"path": str(path)})


def _expected_pre_state(path: Path, *, action_type: str) -> dict[str, Any]:
    observed = _observe_target(path)
    if not observed.get("exists"):
        _fail("m12b_target_missing", "deletion target is missing at plan time", details={"path": str(path)})
    if action_type == "DELETE_FILE" and observed.get("kind") != "file":
        _fail("m12b_target_type_mismatch", "DELETE_FILE requires a regular file", details={"path": str(path)})
    if action_type == "REMOVE_EMPTY_DIRECTORY":
        if observed.get("kind") != "directory" or observed.get("entries"):
            _fail("m12b_directory_not_empty", "REMOVE_EMPTY_DIRECTORY requires an empty directory", details={"path": str(path)})
    return observed


def _target_id(payload: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(dict(payload))).hexdigest()


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
    if surface_class == "VNEXT_MIGRATION_OR_COMPATIBILITY_SOURCE":
        return "SOURCE_ONLY", "EXCLUDE_FROM_TARGET_ONLY_PACKAGE", False
    if surface_class in {
        "SHARED_COMPATIBILITY_REFERENCE",
        "AUXILIARY_COMPATIBILITY_REFERENCE",
        "PACKAGE_COMPOSITION_SURFACE",
        "ROOT_COMPATIBILITY_REFERENCE",
    }:
        return "SOURCE_ONLY", "RETAIN_SOURCE_NOT_TARGET_RUNTIME", False
    return "UNKNOWN_MUST_NOT_DELETE", "RETAIN_UNTIL_EXACT_TARGET_CLOSURE", False


def _classify_inventory(entries: Sequence[Mapping[str, Any]], *, active_source_paths: Iterable[str] = ()) -> tuple[list[dict[str, Any]], list[str]]:
    classified: list[dict[str, Any]] = []
    unknown: list[str] = []
    active_paths = {str(path).replace("\\", "/") for path in active_source_paths}

    def is_active(path: str) -> bool:
        normalized = path.replace("\\", "/")
        if normalized == "bdb_vnext/__init__.py":
            return True
        module_name = normalized.removesuffix(".py").replace("/", ".")
        return normalized in active_paths or module_name in active_paths

    for raw in entries:
        path = raw.get("path")
        if not isinstance(path, str) or not path:
            _fail("m12b_inventory_invalid", "every M12a inventory entry needs a path")
        if is_active(path):
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
            "surface_class": raw.get("surface_class"),
            "disposition": raw.get("disposition"),
            "action_type": raw.get("action_type"),
            "delete_eligible": raw.get("delete_eligible", False) is True,
        }
        result.append(item)
        if category == "UNKNOWN_MUST_NOT_DELETE":
            unknown.append(path)
    return result, unknown


def _package_closure(classified: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    retained = sorted(
        str(item["path"])
        for item in classified
        if item.get("category") == "ACTIVE_PRODUCTION_REQUIRED"
    )
    excluded = sorted(
        str(item["path"])
        for item in classified
        if item.get("category") in {"SOURCE_ONLY", "LEGACY_COMPATIBLE_USAGE_ZERO"}
    )
    unresolved = sorted(
        str(item["path"])
        for item in classified
        if item.get("category") == "UNKNOWN_MUST_NOT_DELETE"
    )
    payload = {
        "schema": "bdb-vnext-m12b-target-package-closure-v1",
        "retained_source_paths": retained,
        "excluded_source_paths": excluded,
        "unresolved_source_paths": unresolved,
        "source_closure_is_not_a_physical_delete": True,
    }
    return {**payload, "closure_sha256": _target_id(payload)}


def _candidate_targets(
    references: Sequence[Mapping[str, Any]],
    *,
    protected_categories: Iterable[str] = _PROTECTED_CATEGORIES,
) -> list[dict[str, Any]]:
    protected_roots = [
        str(item["path"])
        for item in references
        if item.get("category") in set(protected_categories)
    ]
    targets: list[dict[str, Any]] = []
    for item in references:
        if item.get("delete_eligible") is not True:
            continue
        path_value = item.get("path")
        action_type = item.get("action_type")
        disposition = item.get("disposition")
        if not isinstance(path_value, str) or not _ABSOLUTE.fullmatch(path_value):
            _fail("m12b_target_path_invalid", "candidate target path must be absolute")
        if item.get("category") != "DISPOSABLE":
            _fail("m12b_target_category_invalid", "only DISPOSABLE physical references may be deleted")
        if action_type not in _ALLOWED_PHYSICAL_ACTIONS:
            _fail("m12b_target_action_invalid", "candidate target action is not allowlisted")
        if disposition not in _ALLOWED_PHYSICAL_DISPOSITIONS:
            _fail("m12b_target_disposition_invalid", "candidate target disposition is not allowlisted")
        path = Path(path_value).absolute()
        if any(_is_same_or_child(path, root) for root in protected_roots):
            _fail("m12b_target_protected", "candidate target overlaps a protected authority", details={"path": str(path)})
        if _is_reparse_point(path):
            _fail("m12b_target_reparse_point", "candidate target is a symlink/junction/reparse point", details={"path": str(path)})
        pre_state = item.get("expected_pre_state")
        if pre_state is None:
            pre_state = _expected_pre_state(path, action_type=str(action_type))
        if not isinstance(pre_state, Mapping):
            _fail("m12b_target_precondition_invalid", "candidate target pre-state is invalid")
        payload = {
            "path": str(path),
            "category": item["category"],
            "authority": item.get("authority"),
            "surface_class": item.get("surface_class"),
            "disposition": disposition,
            "action_type": action_type,
            "expected_pre_state": dict(pre_state),
            "expected_post_state": {"exists": False, "kind": "missing"},
            "protection_checks": {
                "protected_overlap": False,
                "reparse_point_rejected": True,
                "wildcard_rejected": True,
                "recursive_delete_rejected": True,
            },
        }
        target = {**payload, "target_id": _target_id(payload)}
        targets.append(target)
    ids = [str(item["target_id"]) for item in targets]
    if len(ids) != len(set(ids)):
        _fail("m12b_target_identity_conflict", "candidate target identities are duplicated")
    return sorted(targets, key=lambda item: str(item["target_id"]))


def _route_unknowns(native_routes: Mapping[str, Any] | None, *, expected_manifest: str | None) -> list[str]:
    if native_routes is None:
        return ["HKCU Native Messaging target/legacy route readback"]
    target = native_routes.get("target")
    if not isinstance(target, list):
        return ["HKCU Native Messaging target/legacy route readback"]
    expected = _path_key(expected_manifest) if expected_manifest else None
    hkcu = [item for item in target if isinstance(item, Mapping) and item.get("root") == "HKCU"]
    views = {str(item.get("view")) for item in hkcu}
    values = {_path_key(item.get("value")) for item in hkcu if isinstance(item.get("value"), str)}
    unknown: list[str] = []
    if (
        native_routes.get("target_registered") is not True
        or native_routes.get("target_conflict") is not False
        or native_routes.get("legacy_route_present") is not False
        or views != {"32", "64"}
        or len(values) != 1
        or (expected is not None and values != {expected})
        or any(item.get("root") != "HKCU" for item in target)
    ):
        unknown.append("HKCU Native Messaging target/legacy route readback")
    return unknown


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
    production_acceptance: bool = False,
    production_observation: Mapping[str, Any] | None = None,
    execution_scope: str = "production",
    journal_path: str | Path | None = None,
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
    if client.get("source_head") != source_commit or client.get("source_tree") != source_tree:
        _fail("m12b_client_source_mismatch", "client plan source identity differs from the exact M12a source")
    native_manifest_path = client.get("native_manifest_path")
    if not isinstance(native_manifest_path, str) or _ABSOLUTE.fullmatch(native_manifest_path) is None:
        _fail("m12b_client_manifest_path_invalid", "client plan must bind an exact absolute Native manifest path")
    for field in ("plan_sha256", "state_sha256"):
        _check_digest(route_rebind.get(field), f"route_rebind_{field}")

    inventory = closure_report.get("compatibility_inventory")
    entries = inventory.get("entries") if isinstance(inventory, Mapping) else None
    if not isinstance(entries, list) or inventory.get("inventory_complete") is not True:
        _fail("m12b_inventory_incomplete", "M12a compatibility inventory is incomplete")
    classified, inventory_unknown = _classify_inventory(entries, active_source_paths=active_source_paths)
    refs, reference_unknown = _check_physical_references(physical_references)
    package_closure = _package_closure(classified)
    candidate_targets = _candidate_targets(refs)
    route_unknown = _route_unknowns(native_routes, expected_manifest=client.get("native_manifest_path"))
    unknown = sorted(set(inventory_unknown + reference_unknown + route_unknown))
    if execution_scope not in {"production", "fixture"}:
        _fail("m12b_execution_scope_invalid", "execution_scope is invalid")
    accepted, acceptance_observation = _production_acceptance_observation(
        execution_scope=execution_scope,
        production_observation=production_observation,
        production_acceptance=production_acceptance,
    )
    if execution_scope == "production" and accepted is True:
        expected_observation = {
            "source": "m9b_reconciliation_subject",
            "bootstrap_state_sha256": state_sha,
            "active_source_commit": source_commit,
            "active_source_tree": source_tree,
            "m9b_state": "ACTIVE",
            "writer_enabled": True,
            "intake_enabled": True,
            "m3c_admission_enabled": True,
            "native_routes": dict(native_routes or {}),
        }
        if any(acceptance_observation.get(key) != value for key, value in expected_observation.items()):
            accepted = False
            acceptance_observation = {
                **acceptance_observation,
                "production_acceptance": False,
                "reason": "canonical production observation does not bind the exact current subject",
            }
    if execution_scope == "production" and accepted is not True:
        unknown = sorted(set((*unknown, "production acceptance observation not proven")))
    if journal_path is not None:
        journal_path = str(_absolute(journal_path, field="journal_path", must_exist=False))
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
        "production_observation": acceptance_observation,
        "inventory": classified,
        "physical_references": refs,
        "package_closure": package_closure,
        "candidate_targets": candidate_targets,
        "zero_target_reason": (
            None if candidate_targets else "NO_EXACT_PHYSICAL_DISPOSABLE_SURFACE_IN_M12A_EVIDENCE"
        ),
        "category_counts": {key: categories.get(key, 0) for key in sorted(_CATEGORIES)},
        "unknown_paths": unknown,
        "production_acceptance": accepted,
        "execution_scope": execution_scope,
        "journal_path": journal_path,
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
        "status": M12B_BLOCKED_STATUS if unknown or accepted is not True else M12B_PLAN_STATUS,
        "operator_approval_required": True,
        "apply_mode": "EXACT_PATH_BOUND_GUARDED",
        "candidate_targets": candidate_targets,
        "package_closure": package_closure,
        "zero_target_reason": subject_payload["zero_target_reason"],
        "unknown_paths": unknown,
        "protected_categories": [
            "ACTIVE_PRODUCTION_REQUIRED",
            "PREVIOUS_RECOVERY_REQUIRED",
            "IMMUTABLE_EVIDENCE",
            "LEGACY_COMPATIBLE_USAGE_ZERO",
        ],
        "production_acceptance": accepted,
        "production_observation": acceptance_observation,
        "execution_scope": execution_scope,
        "journal_path": journal_path,
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
    from bdb_vnext.m9b_activation import read_activation
    from bdb_vnext.m9b_reconciliation import _subject as observe_production_subject
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
    m9b_result = query_post_active_reconciliation(
        authority_root=authority,
        maintenance_id=maintenance_id,
        deployed_runtime_root=runtime,
    )
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
    try:
        observed_subject = observe_production_subject(
            authority=authority,
            deployed_runtime=runtime,
            client_runtime=client_runtime,
            maintenance_id=maintenance_id,
            maintenance_plan_sha256=str(state.get("cutover_plan_sha256", "")),
            route_rebind_id=rebind_id,
            route_rebind_plan_sha256=str(rebind["plan_sha256"]),
        )
        observed_m9b = observed_subject["m9b"]
        production_observation = {
            "schema": M12B_PRODUCTION_OBSERVATION_SCHEMA,
            "source": "m9b_reconciliation_subject",
            "production_acceptance": (
                observed_subject["routes"].get("target_registered") is True
                and observed_subject["routes"].get("target_conflict") is False
                and observed_subject["routes"].get("legacy_route_present") is False
                and observed_m9b.state == "ACTIVE"
                and observed_m9b.writer_enabled is True
                and observed_m9b.intake_enabled is True
            ),
            "bootstrap_state_sha256": observed_subject["bootstrap"].get("state_sha256"),
            "active_source_commit": observed_subject["active"].get("source_commit"),
            "active_source_tree": observed_subject["maintenance"].get("candidate_source_tree"),
            "m9b_state": observed_m9b.state,
            "m9b_source_head": observed_m9b.source_head,
            "writer_enabled": observed_m9b.writer_enabled,
            "intake_enabled": observed_m9b.intake_enabled,
            "native_routes": observed_subject["routes"],
            "m3c_admission_enabled": observed_subject["m3c"].get("kill_switch", {}).get("admission_enabled"),
        }
    except Exception as exc:
        observed_m9b = read_activation(runtime)
        production_observation = {
            "schema": M12B_PRODUCTION_OBSERVATION_SCHEMA,
            "source": "m9b_reconciliation_subject",
            "production_acceptance": False,
            "reason": "canonical production subject did not prove acceptance",
            "error_code": getattr(exc, "code", "production_subject_unavailable"),
            "error": str(exc),
            "m9b_state": observed_m9b.state if observed_m9b is not None else None,
            "m9b_source_head": observed_m9b.source_head if observed_m9b is not None else None,
            "writer_enabled": observed_m9b.writer_enabled if observed_m9b is not None else None,
            "intake_enabled": observed_m9b.intake_enabled if observed_m9b is not None else None,
            "native_routes": native_routes,
            "m3c_admission_enabled": m3c.get("admission_enabled"),
        }
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
    evidence_root = runtime / "evidence" / "m12b-readiness"
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
        production_observation=production_observation,
        execution_scope="production",
        journal_path=evidence_root / "journals" / f"{subject_id}.json",
    )
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
    targets = plan.get("candidate_targets")
    if not isinstance(targets, list):
        _fail("m12b_target_list_invalid", "M12b candidate_targets must be a list")
    if not targets and not isinstance(plan.get("zero_target_reason"), str):
        _fail("m12b_zero_target_reason_missing", "a zero-target plan needs a formal reason")
    for target in targets:
        if not isinstance(target, Mapping):
            _fail("m12b_target_invalid", "M12b candidate target must be an object")
        path = target.get("path")
        if not isinstance(path, str) or not _ABSOLUTE.fullmatch(path) or any(char in path for char in "*?[]"):
            _fail("m12b_target_path_invalid", "M12b candidate target path is not exact")
        if target.get("action_type") not in _ALLOWED_PHYSICAL_ACTIONS:
            _fail("m12b_target_action_invalid", "M12b candidate target action is not allowlisted")
        if target.get("category") != "DISPOSABLE":
            _fail("m12b_target_category_invalid", "M12b candidate target is not disposable")
        if not isinstance(target.get("target_id"), str):
            _fail("m12b_target_identity_missing", "M12b candidate target has no identity")
    return {"schema": M12B_RESULT_SCHEMA, "status": plan.get("status"), "plan_sha256": supplied, "plan": plan, "production_deletion_performed": False}


def _write_journal(path: Path, document: Mapping[str, Any]) -> None:
    payload = canonical_json_bytes(document)
    if len(payload) > _MAX_OBJECT_BYTES:
        _fail("m12b_journal_too_large", "M12b journal exceeds bounded size")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(payload)
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        _fail("m12b_journal_write_failed", "M12b journal could not be persisted")
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass


def _read_journal(path: Path, *, plan_sha256: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    journal = _json(path, field="m12b_journal")
    if journal.get("schema") != M12B_JOURNAL_SCHEMA or journal.get("plan_sha256") != plan_sha256:
        _fail("m12b_journal_identity_mismatch", "M12b journal does not belong to the exact plan")
    if journal.get("state") not in M12B_JOURNAL_STATES:
        _fail("m12b_journal_state_invalid", "M12b journal state is invalid")
    return journal


def _authority_fingerprint(value: Mapping[str, Any]) -> str:
    subject = value.get("subject") if isinstance(value.get("subject"), Mapping) else value
    if not isinstance(subject, Mapping):
        _fail("m12b_authority_readback_invalid", "authority readback is not an object")
    projection = {
        key: subject.get(key)
        for key in ("bootstrap", "m9b", "m3c", "client", "route_rebind", "native_routes")
    }
    return _digest(projection)


def _revalidate_authority(
    plan: Mapping[str, Any],
    *,
    authority_reader: Callable[[], Mapping[str, Any]] | None,
) -> None:
    if authority_reader is None:
        _fail("m12b_authority_readback_required", "canonical authority readback is required before apply")
    try:
        current = authority_reader()
    except M12bDeletionError:
        raise
    except Exception as exc:
        _fail("m12b_authority_readback_failed", "canonical authority readback failed")
    expected = plan.get("subject")
    if not isinstance(expected, Mapping) or _authority_fingerprint(current) != _authority_fingerprint(expected):
        _fail("m12b_authority_changed", "Bootstrap/M9b/M3c/client/route authority changed")


def _validate_target_contract(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    targets = plan.get("candidate_targets")
    if not isinstance(targets, list):
        _fail("m12b_target_list_invalid", "candidate_targets must be a list")
    protected = plan.get("protected_categories")
    if not isinstance(protected, list) or set(protected) != set(_PROTECTED_CATEGORIES):
        _fail("m12b_protection_contract_invalid", "protected category contract differs")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    refs = plan.get("subject", {}).get("physical_references", [])
    protected_roots = [
        item.get("path")
        for item in refs
        if isinstance(item, Mapping) and item.get("category") in _PROTECTED_CATEGORIES
    ]
    for raw in targets:
        if not isinstance(raw, Mapping):
            _fail("m12b_target_invalid", "candidate target must be an object")
        target = dict(raw)
        target_id = target.get("target_id")
        path_value = target.get("path")
        if not isinstance(target_id, str) or target_id in seen:
            _fail("m12b_target_identity_conflict", "candidate target identity is missing or duplicated")
        if not isinstance(path_value, str) or not _ABSOLUTE.fullmatch(path_value) or any(char in path_value for char in "*?[]"):
            _fail("m12b_target_path_invalid", "candidate target path is not exact")
        if any(_is_same_or_child(path_value, root) for root in protected_roots if isinstance(root, str)):
            _fail("m12b_target_protected", "candidate target overlaps protected authority", details={"path": path_value})
        if target.get("action_type") not in _ALLOWED_PHYSICAL_ACTIONS or target.get("category") != "DISPOSABLE":
            _fail("m12b_target_contract_invalid", "candidate target is outside the allowlist")
        if target.get("disposition") not in _ALLOWED_PHYSICAL_DISPOSITIONS:
            _fail("m12b_target_contract_invalid", "candidate target disposition is outside the allowlist")
        expected_post = target.get("expected_post_state")
        if expected_post != {"exists": False, "kind": "missing"}:
            _fail("m12b_target_contract_invalid", "candidate target post-state is not exact")
        expected_pre = target.get("expected_pre_state")
        if not isinstance(expected_pre, Mapping) or expected_pre.get("exists") is not True:
            _fail("m12b_target_contract_invalid", "candidate target pre-state is not exact")
        payload = {key: value for key, value in target.items() if key != "target_id"}
        if target_id != _target_id(payload):
            _fail("m12b_target_identity_mismatch", "candidate target identity does not match its exact contract")
        seen.add(target_id)
        result.append(target)
    return result


def _apply_target(target: Mapping[str, Any], *, allow_already_missing: bool = False) -> dict[str, Any]:
    path = Path(str(target["path"])).absolute()
    if _is_reparse_point(path):
        _fail("m12b_target_reparse_point", "target became a reparse point", details={"path": str(path)})
    observed = _observe_target(path)
    expected = target.get("expected_pre_state")
    if not isinstance(expected, Mapping):
        _fail("m12b_target_precondition_invalid", "target has no exact pre-state")
    if observed != dict(expected):
        if allow_already_missing and observed == {"exists": False, "kind": "missing"}:
            return observed
        _fail("m12b_target_changed", "target pre-state differs", details={"path": str(path)})
    action = target.get("action_type")
    try:
        if action == "DELETE_FILE":
            if not path.is_file() or path.is_symlink():
                _fail("m12b_target_type_mismatch", "DELETE_FILE target is not a regular file")
            path.unlink()
        elif action == "REMOVE_EMPTY_DIRECTORY":
            if not path.is_dir() or any(path.iterdir()):
                _fail("m12b_directory_not_empty", "directory is no longer empty")
            path.rmdir()
        else:
            _fail("m12b_target_action_invalid", "target action is not allowlisted")
    except M12bDeletionError:
        raise
    except OSError as exc:
        _fail("m12b_target_delete_failed", "exact target deletion failed", details={"path": str(path)})
    after = _observe_target(path)
    if after != {"exists": False, "kind": "missing"}:
        _fail("m12b_target_readback_failed", "target deletion readback is not exact", details={"path": str(path)})
    return after


def apply_m12b_deletion(
    *,
    plan_path: str | Path,
    approval_token: str,
    dry_run: bool = True,
    authority_reader: Callable[[], Mapping[str, Any]] | None = None,
    production_apply_approved: bool = False,
    fault_at: str | None = None,
) -> dict[str, Any]:
    """Apply an exact target-only plan with durable, replayable journal state.

    A production caller must supply both the exact operator token and an
    explicit ``production_apply_approved`` flag after its final lock/readback.
    Tests use ``execution_scope=fixture`` and never touch production paths.
    """

    verified = verify_m12b_plan(plan_path=plan_path)
    plan = verified["plan"]
    plan_sha = verified["plan_sha256"]
    if approval_token != M12B_APPROVAL_TOKEN:
        _fail("m12b_operator_approval_required", "exact M12b operator approval is required")
    if plan.get("status") != M12B_PLAN_STATUS or plan.get("unknown_paths"):
        _fail("m12b_plan_not_ready", "M12b plan contains unresolved protected/unknown paths")
    targets = _validate_target_contract(plan)
    if dry_run:
        return {
            "schema": M12B_RESULT_SCHEMA,
            "status": "DRY_RUN_ONLY",
            "plan_sha256": plan_sha,
            "candidate_targets": targets,
            "deleted_paths": [],
            "production_deletion_performed": False,
        }
    if plan.get("production_acceptance") is not True:
        _fail("m12b_production_acceptance_required", "production acceptance is not published in the exact plan")
    if plan.get("execution_scope") == "production" and production_apply_approved is not True:
        _fail("m12b_production_approval_required", "production apply requires a separate explicit approval boundary")
    _revalidate_authority(plan, authority_reader=authority_reader)
    journal_value = plan.get("journal_path")
    if not isinstance(journal_value, str) or not _ABSOLUTE.fullmatch(journal_value):
        _fail("m12b_journal_path_invalid", "exact journal path is required")
    journal_path = Path(journal_value)
    journal = _read_journal(journal_path, plan_sha256=plan_sha)
    if journal is None:
        journal = {
            "schema": M12B_JOURNAL_SCHEMA,
            "plan_sha256": plan_sha,
            "subject_sha256": plan.get("subject_sha256"),
            "state": "PREPARED",
            "completed_actions": [],
            "current_target_id": None,
            "production_deletion_performed": False,
        }
        _write_journal(journal_path, journal)
    completed = set(journal.get("completed_actions", []))
    if journal.get("state") == "COMPLETED":
        target_ids = {str(target["target_id"]) for target in targets}
        if completed != target_ids:
            _fail("m12b_journal_incomplete", "completed journal does not contain every exact target")
        for target in targets:
            if _observe_target(Path(str(target["path"])).absolute()) != {"exists": False, "kind": "missing"}:
                _fail("m12b_target_readback_failed", "completed target is no longer absent", details={"path": target["path"]})
        return {
            "schema": M12B_RESULT_SCHEMA,
            "status": "COMPLETED",
            "plan_sha256": plan_sha,
            "deleted_paths": [target["path"] for target in targets],
            "production_deletion_performed": bool(targets) and plan.get("execution_scope") == "production",
        }
    deleted: list[str] = []
    try:
        for index, target in enumerate(targets):
            target_id = str(target["target_id"])
            if target_id in completed:
                continue
            recovering_current = (
                journal.get("state") == "APPLYING"
                and journal.get("current_target_id") == target_id
            )
            if not recovering_current:
                current_pre = _observe_target(Path(str(target["path"])).absolute())
                if current_pre != dict(target["expected_pre_state"]):
                    _fail(
                        "m12b_target_changed",
                        "target pre-state differs before journaled action",
                        details={"path": target["path"]},
                    )
            if fault_at in {"before_first_deletion", f"before_target:{target_id}"} or (
                fault_at == "before_first_target" and index == 0
            ):
                _fail("m12b_fault_injected", "fault injected before target deletion")
            journal = {
                **journal,
                "state": "APPLYING",
                "current_target_id": target_id,
                "completed_actions": sorted(completed),
            }
            _write_journal(journal_path, journal)
            _revalidate_authority(plan, authority_reader=authority_reader)
            _apply_target(
                target,
                allow_already_missing=recovering_current,
            )
            deleted.append(str(target["path"]))
            if fault_at in {"after_target", f"after_target:{target_id}"}:
                _fail("m12b_fault_injected", "fault injected after target deletion")
            completed.add(target_id)
            if fault_at == "before_journal_update":
                _fail("m12b_fault_injected", "fault injected before journal update")
            journal = {
                **journal,
                "state": "PARTIAL" if len(completed) < len(targets) else "APPLYING",
                "current_target_id": None,
                "completed_actions": sorted(completed),
            }
            _write_journal(journal_path, journal)
        journal = {
            **journal,
            "state": "COMPLETED",
            "current_target_id": None,
            "completed_actions": sorted(completed),
            "production_deletion_performed": bool(targets) and plan.get("execution_scope") == "production",
        }
        _write_journal(journal_path, journal)
    except M12bDeletionError as exc:
        # Fault injection models a process crash: leave the durable APPLYING
        # checkpoint for replay.  Other typed failures are durable BLOCKED
        # outcomes and must not be mistaken for a completed action.
        if exc.code != "m12b_fault_injected":
            try:
                _write_journal(
                    journal_path,
                    {
                        **journal,
                        "state": "BLOCKED",
                        "completed_actions": sorted(completed),
                    },
                )
            except M12bDeletionError:
                pass
        raise
    except Exception as exc:
        blocked = {
            **journal,
            "state": "BLOCKED",
            "completed_actions": sorted(completed),
        }
        _write_journal(journal_path, blocked)
        _fail("m12b_apply_blocked", "M12b apply stopped fail-closed")
    return {
        "schema": M12B_RESULT_SCHEMA,
        "status": "COMPLETED",
        "plan_sha256": plan_sha,
        "deleted_paths": deleted,
        "production_deletion_performed": bool(deleted) and plan.get("execution_scope") == "production",
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
    dry.add_argument("--execute", action="store_true")
    dry.add_argument("--production-approved", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "verify":
            result = verify_m12b_plan(plan_path=args.plan, expected_plan_sha256=args.expected_plan_sha256)
        else:
            result = apply_m12b_deletion(
                plan_path=args.plan,
                approval_token=args.approval_token,
                dry_run=not args.execute,
                production_apply_approved=args.production_approved,
            )
    except M12bDeletionError as exc:
        print(json.dumps({"schema": M12B_RESULT_SCHEMA, "status": "FAIL", "error_code": exc.code, "message": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


__all__ = [
    "M12B_APPROVAL_TOKEN",
    "M12B_BLOCKED_STATUS",
    "M12B_JOURNAL_SCHEMA",
    "M12B_JOURNAL_STATES",
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

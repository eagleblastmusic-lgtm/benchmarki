"""Side-by-side M9a handoff evidence for BDB Next.

This is the revised-roadmap M9a path.  It does not execute the historical
roll-forward Legacy freeze in :mod:`bdb_vnext.m9a_freeze`.  Instead it proves
that the Browser/Native takeover subject is quiescent and non-colliding while
Legacy stays installed and independently recoverable/selectable.

The only writes are content-addressed evidence and bounded scratch copies below
the vNext runtime.  No Legacy config, spool, registry route, writer, or vNext
activation pointer is mutated here.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, NoReturn

from bdb_shared.evidence import canonical_json_bytes, semantic_digest
from bdb_vnext.m11c_windows_clients import (
    M11cClientError,
    observe_windows_native_routes,
    query_client_plan,
    require_client_verification,
)
from bdb_vnext.m9a_blocker_probe_compat import probe_profile
from bdb_vnext.m9a_freeze import ProfileSpec as LegacyProfileSpec
from bdb_vnext.m9a_freeze import _validate_probe as _validate_legacy_probe
from bdb_vnext.m9b_activation import M9bActivationError, validate_m9a_freeze_report


HANDOFF_SCOPE = "browser-native-same-subject-v1"
HANDOFF_ARCHIVE_SCHEMA = "bdb-vnext-m9a-side-by-side-archive-v1"
HANDOFF_RESULT_SCHEMA = "bdb-vnext-m9a-side-by-side-result-v1"
FREEZE_REPORT_SCHEMA = "bdb-vnext-m9a-freeze-report-v1"
LEGACY_NATIVE_CONFIG_SCHEMA = "bdb-native-host-config-v1"
_MAX_EVIDENCE_BYTES = 16 * 1024 * 1024
_MAX_PROFILES = 32


class M9aHandoffError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> NoReturn:
    raise M9aHandoffError(code, message)


def _absolute(value: str | Path, *, field: str) -> Path:
    path = Path(value).expanduser().absolute()
    if not path.is_absolute():
        _fail("invalid_path", f"{field} must be absolute")
    return path


def _objects_root(runtime: Path) -> Path:
    return runtime / "evidence" / "m9a-side-by-side" / "objects"


def _write_object(runtime: Path, payload: Mapping[str, Any]) -> tuple[str, Path]:
    document = canonical_json_bytes(dict(payload))
    if len(document) > _MAX_EVIDENCE_BYTES:
        _fail("evidence_too_large", "M9a evidence object exceeds its bounded size")
    digest = semantic_digest(payload)
    path = _objects_root(runtime) / f"{digest[7:]}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            current = path.read_bytes()
        except OSError as exc:
            raise M9aHandoffError("evidence_read_failed", "existing M9a evidence cannot be read") from exc
        if current != document:
            _fail("evidence_digest_conflict", "content-addressed M9a evidence differs")
        return digest, path
    staging = path.parent / f".{path.name}.partial-{uuid.uuid4().hex}"
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
        raise M9aHandoffError("evidence_write_failed", "M9a evidence could not be published") from exc
    return digest, path


def _read_object(runtime: Path, digest: object) -> dict[str, Any]:
    if not isinstance(digest, str) or len(digest) != 71 or not digest.startswith("sha256:"):
        _fail("evidence_ref_invalid", "M9a evidence reference must be an exact sha256 digest")
    path = _objects_root(runtime) / f"{digest[7:]}.json"
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise M9aHandoffError("evidence_missing", "referenced M9a evidence is unavailable") from exc
    if len(payload) > _MAX_EVIDENCE_BYTES:
        _fail("evidence_too_large", "referenced M9a evidence exceeds its bounded size")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise M9aHandoffError("evidence_invalid", "referenced M9a evidence is not valid JSON") from exc
    if not isinstance(value, Mapping) or semantic_digest(value) != digest:
        _fail("evidence_digest_mismatch", "referenced M9a evidence digest differs")
    return {str(key): item for key, item in value.items()}


def _legacy_profiles(legacy_root: Path) -> tuple[tuple[str, Path], ...]:
    native_config = legacy_root / "native-host.json"
    if native_config.is_symlink() or not native_config.is_file():
        _fail("legacy_native_config_missing", "Legacy native-host.json must remain installed and readable")
    try:
        document = json.loads(native_config.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise M9aHandoffError("legacy_native_config_invalid", "Legacy native-host.json is not valid JSON") from exc
    if not isinstance(document, Mapping) or document.get("schema") != LEGACY_NATIVE_CONFIG_SCHEMA:
        _fail("legacy_native_config_invalid", "Legacy Native config schema differs")
    repositories = document.get("repositories")
    if repositories is None:
        legacy_path = document.get("bridge_config_path")
        if not isinstance(legacy_path, str) or not legacy_path:
            _fail("legacy_native_config_invalid", "Legacy Native config has no repository binding")
        repositories = {"default": {"bridge_config_path": legacy_path}}
    if not isinstance(repositories, Mapping) or not repositories or len(repositories) > _MAX_PROFILES:
        _fail("legacy_native_config_invalid", "Legacy repository bindings are invalid")
    result: list[tuple[str, Path]] = []
    for profile_id, item in sorted(repositories.items(), key=lambda pair: str(pair[0])):
        if not isinstance(profile_id, str) or not profile_id or len(profile_id) > 128:
            _fail("legacy_profile_invalid", "Legacy profile identity is invalid")
        if isinstance(item, str):
            raw_path = item
        elif isinstance(item, Mapping):
            raw_path = item.get("bridge_config_path")
        else:
            raw_path = None
        if not isinstance(raw_path, str) or not raw_path:
            _fail("legacy_profile_invalid", f"Legacy profile {profile_id} has no bridge config")
        path = Path(raw_path).expanduser().absolute()
        if path.is_symlink() or not path.is_file():
            _fail("legacy_profile_unavailable", f"Legacy profile config is unavailable: {profile_id}")
        result.append((profile_id, path))
    return tuple(result)


def _probe_once(
    *,
    profiles: Sequence[tuple[str, Path]],
    native_config: Path,
    scratch: Path,
) -> dict[str, dict[str, Any]]:
    observations: dict[str, dict[str, Any]] = {}
    for profile_id, bridge_config in profiles:
        probe = probe_profile(
            profile_id=profile_id,
            bridge_config_path=bridge_config,
            native_config_path=native_config,
            scratch_dir=scratch,
            max_records=5000,
        ).as_dict()
        expected = probe.get("probe_digest")
        if not isinstance(expected, str):
            _fail("legacy_probe_invalid", f"Legacy probe digest missing: {profile_id}")
        try:
            _validate_legacy_probe(LegacyProfileSpec(profile_id, bridge_config, expected), probe)
        except Exception as exc:
            raise M9aHandoffError("legacy_not_quiescent", f"Legacy profile is not safe for scoped handoff: {profile_id}: {exc}") from exc
        observations[profile_id] = probe
    return observations


def _normalize_routes(raw: Mapping[str, Any]) -> dict[str, Any]:
    target = raw.get("target")
    legacy = raw.get("legacy")
    if not isinstance(target, list) or not isinstance(legacy, list):
        _fail("route_inventory_invalid", "Native route observation is incomplete")

    def rows(items: list[object]) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for item in items:
            if not isinstance(item, Mapping):
                _fail("route_inventory_invalid", "Native route record is invalid")
            root, view, value = item.get("root"), item.get("view"), item.get("value")
            if not all(isinstance(part, str) and part for part in (root, view, value)):
                _fail("route_inventory_invalid", "Native route record fields differ")
            result.append({"root": root, "view": view, "value": value})
        return sorted(result, key=lambda row: (row["root"], row["view"], row["value"]))

    return {
        "schema": "bdb-vnext-m9a-route-evidence-v1",
        "scope": HANDOFF_SCOPE,
        "target": rows(target),
        "legacy": rows(legacy),
        "target_registered": raw.get("target_registered") is True,
        "target_conflict": raw.get("target_conflict") is True,
        "legacy_route_present": raw.get("legacy_route_present") is True,
        "legacy_product_globally_disabled": False,
        "production_activation_performed": False,
    }


def _facts(routes: Mapping[str, Any], first: Mapping[str, Any], second: Mapping[str, Any]) -> dict[str, Any]:
    profile_ids = sorted(set(first) | set(second))
    drift = sum(
        1
        for profile_id in profile_ids
        if first.get(profile_id, {}).get("probe_digest") != second.get(profile_id, {}).get("probe_digest")
    )
    duplicate_routes = len(routes["legacy"])
    collision_count = int(routes["target_conflict"]) + int(not routes["target_registered"])
    ownership_collisions = duplicate_routes + collision_count
    ingress_frozen = duplicate_routes == 0 and routes["target_registered"] is True and routes["target_conflict"] is False
    zero_write = drift == 0
    writer_frozen = ingress_frozen and zero_write
    return {
        "duplicate_routes": duplicate_routes,
        "collision_count": collision_count,
        "legacy_store_drift": drift,
        "ownership_collisions": ownership_collisions,
        "legacy_ingress_frozen": ingress_frozen,
        "legacy_writer_frozen": writer_frozen,
        "zero_new_write_observed": zero_write,
    }


def capture_side_by_side_handoff(
    *,
    runtime_root: str | Path,
    legacy_runtime_root: str | Path,
    observation_seconds: float = 2.0,
    route_observer: Callable[..., Mapping[str, Any]] = observe_windows_native_routes,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Capture PASS_CLOSED M9a evidence without disabling the Legacy product."""

    if observation_seconds < 0 or observation_seconds > 60:
        _fail("observation_window_invalid", "M9a observation window must be between 0 and 60 seconds")
    runtime = _absolute(runtime_root, field="runtime_root")
    legacy = _absolute(legacy_runtime_root, field="legacy_runtime_root")
    if legacy.is_symlink() or not legacy.is_dir():
        _fail("legacy_runtime_unavailable", "Legacy runtime must remain installed")
    try:
        client = query_client_plan(runtime_root=runtime)
        plan = client["plan"]
        verification = require_client_verification(
            runtime_root=runtime,
            expected_client_plan_sha256=plan["client_plan_sha256"],
        )
        routes = _normalize_routes(route_observer(runtime_root=runtime))
    except (M11cClientError, KeyError, TypeError) as exc:
        code = getattr(exc, "code", "client_precondition_failed")
        raise M9aHandoffError(str(code), "M9a requires exact VERIFIED vNext client state") from exc

    profiles = _legacy_profiles(legacy)
    scratch_root = runtime / "evidence" / "m9a-side-by-side" / "scratch" / uuid.uuid4().hex
    native_config = legacy / "native-host.json"
    try:
        first = _probe_once(profiles=profiles, native_config=native_config, scratch=scratch_root / "first")
        if observation_seconds:
            sleep_fn(observation_seconds)
        second = _probe_once(profiles=profiles, native_config=native_config, scratch=scratch_root / "second")
    finally:
        shutil.rmtree(scratch_root, ignore_errors=True)

    facts = _facts(routes, first, second)
    route_ref, _ = _write_object(runtime, routes)
    first_payload = {
        "schema": "bdb-vnext-m9a-probe-set-v1",
        "phase": "FIRST",
        "scope": HANDOFF_SCOPE,
        "profiles": first,
        "legacy_mutation_performed": False,
        "production_activation_performed": False,
    }
    second_payload = {**first_payload, "phase": "SECOND", "profiles": second}
    first_ref, _ = _write_object(runtime, first_payload)
    second_ref, _ = _write_object(runtime, second_payload)
    client_payload = {
        "schema": "bdb-vnext-m9a-client-binding-v1",
        "scope": HANDOFF_SCOPE,
        "client_plan_sha256": plan["client_plan_sha256"],
        "client_verification_sha256": verification["verification_sha256"],
        "source_head": plan["source_head"],
        "source_tree": plan["source_tree"],
        "browser_bundle_digest": plan["browser_bundle_digest"],
        "native_manifest_sha256": plan["native_manifest_sha256"],
        "production_activation_performed": False,
    }
    client_ref, _ = _write_object(runtime, client_payload)

    pass_closed = all(
        (
            facts["duplicate_routes"] == 0,
            facts["collision_count"] == 0,
            facts["legacy_store_drift"] == 0,
            facts["ownership_collisions"] == 0,
            facts["legacy_ingress_frozen"] is True,
            facts["legacy_writer_frozen"] is True,
            facts["zero_new_write_observed"] is True,
        )
    )
    archive_payload = {
        "schema": HANDOFF_ARCHIVE_SCHEMA,
        "scope": HANDOFF_SCOPE,
        "legacy_runtime_root": str(legacy),
        "legacy_native_config_path": str(native_config),
        "profiles": [
            {"profile_id": profile_id, "bridge_config_path": str(path)} for profile_id, path in profiles
        ],
        "source_head": plan["source_head"],
        "source_tree": plan["source_tree"],
        "client_plan_sha256": plan["client_plan_sha256"],
        "route_evidence_ref": route_ref,
        "first_probe_ref": first_ref,
        "second_probe_ref": second_ref,
        "client_evidence_ref": client_ref,
        **facts,
        "legacy_product_globally_disabled": False,
        "legacy_mutation_performed": False,
        "production_activation_performed": False,
    }
    archive_ref, archive_path = _write_object(runtime, archive_payload)
    archive_readable = _read_object(runtime, archive_ref) == archive_payload
    pass_closed = pass_closed and archive_readable
    report = {
        "schema": FREEZE_REPORT_SCHEMA,
        "status": "PASS_CLOSED" if pass_closed else "BLOCKED",
        "scope": HANDOFF_SCOPE,
        "source_head": plan["source_head"],
        "source_tree": plan["source_tree"],
        "client_plan_sha256": plan["client_plan_sha256"],
        **facts,
        "archive_created": archive_readable,
        "archive_readable": archive_readable,
        "vnext_activation_allowed": False,
        "m9b_allowed": False,
        "partial_freeze_requires_roll_forward": False,
        "legacy_product_globally_disabled": False,
        "evidence_refs": [route_ref, first_ref, second_ref, client_ref, archive_ref],
        "freeze_digest": archive_ref,
    }
    if pass_closed:
        try:
            validate_m9a_freeze_report(report)
        except M9bActivationError as exc:
            raise M9aHandoffError(exc.code, str(exc)) from exc
    report_ref, report_path = _write_object(runtime, report)
    return {
        "schema": HANDOFF_RESULT_SCHEMA,
        "status": report["status"],
        "report": report,
        "report_sha256": report_ref,
        "report_path": str(report_path),
        "archive_path": str(archive_path),
        "legacy_mutation_performed": False,
        "legacy_product_globally_disabled": False,
        "production_activation_performed": False,
    }


def _archive_for_digest(runtime: Path, freeze_digest: object) -> dict[str, Any]:
    archive = _read_object(runtime, freeze_digest)
    if archive.get("schema") != HANDOFF_ARCHIVE_SCHEMA or archive.get("scope") != HANDOFF_SCOPE:
        _fail("m9a_archive_invalid", "M9a freeze digest does not identify side-by-side handoff evidence")
    return archive


def verify_side_by_side_report(*, runtime_root: str | Path, report: Mapping[str, Any]) -> str:
    """Cryptographically bind a PASS_CLOSED report to the local evidence archive."""

    runtime = _absolute(runtime_root, field="runtime_root")
    try:
        freeze_digest = validate_m9a_freeze_report(report)
    except M9bActivationError as exc:
        raise M9aHandoffError(exc.code, str(exc)) from exc
    if report.get("scope") != HANDOFF_SCOPE or report.get("legacy_product_globally_disabled") is not False:
        _fail("m9a_scope_invalid", "M9a report does not prove the side-by-side scope")
    archive = _archive_for_digest(runtime, freeze_digest)
    refs = report.get("evidence_refs")
    expected_refs = [
        archive["route_evidence_ref"],
        archive["first_probe_ref"],
        archive["second_probe_ref"],
        archive["client_evidence_ref"],
        freeze_digest,
    ]
    if refs != expected_refs:
        _fail("m9a_evidence_invalid", "M9a report evidence references differ from the archive")
    for ref in expected_refs:
        _read_object(runtime, ref)
    plan = query_client_plan(runtime_root=runtime)["plan"]
    for field in ("source_head", "source_tree", "client_plan_sha256"):
        if report.get(field) != plan.get(field) or archive.get(field) != plan.get(field):
            _fail("m9a_client_binding_mismatch", "M9a handoff binds a different staged source/client subject")
    expected = {
        "duplicate_routes": 0,
        "collision_count": 0,
        "legacy_store_drift": 0,
        "ownership_collisions": 0,
        "legacy_ingress_frozen": True,
        "legacy_writer_frozen": True,
        "zero_new_write_observed": True,
    }
    if any(report.get(key) != value or archive.get(key) != value for key, value in expected.items()):
        _fail("m9a_archive_invalid", "M9a PASS_CLOSED facts differ from archived observations")
    if report.get("archive_created") is not True or report.get("archive_readable") is not True:
        _fail("m9a_archive_invalid", "M9a report does not prove a readable evidence archive")
    return freeze_digest


def verify_side_by_side_archive(*, runtime_root: str | Path, freeze_digest: str) -> dict[str, Any]:
    """Read every content-addressed M9a evidence object bound by the archive."""

    runtime = _absolute(runtime_root, field="runtime_root")
    archive = _archive_for_digest(runtime, freeze_digest)
    refs = [
        archive.get("route_evidence_ref"),
        archive.get("first_probe_ref"),
        archive.get("second_probe_ref"),
        archive.get("client_evidence_ref"),
        freeze_digest,
    ]
    if any(not isinstance(ref, str) for ref in refs):
        _fail("m9a_archive_invalid", "M9a archive evidence references are incomplete")
    objects = [_read_object(runtime, str(ref)) for ref in refs]
    first, second, client = objects[1], objects[2], objects[3]
    if (
        first.get("schema") != "bdb-vnext-m9a-probe-set-v1"
        or first.get("phase") != "FIRST"
        or second.get("schema") != "bdb-vnext-m9a-probe-set-v1"
        or second.get("phase") != "SECOND"
        or client.get("schema") != "bdb-vnext-m9a-client-binding-v1"
    ):
        _fail("m9a_archive_invalid", "M9a archive referenced evidence identities differ")
    return {
        "schema": "bdb-vnext-m9a-archive-verification-v1",
        "freeze_digest": freeze_digest,
        "archive_readable": True,
        "evidence_refs": [str(ref) for ref in refs],
        "evidence_object_count": len(refs),
        "legacy_mutation_performed": False,
        "production_activation_performed": False,
    }


def revalidate_side_by_side_digest(
    *,
    runtime_root: str | Path,
    legacy_runtime_root: str | Path,
    freeze_digest: str,
    route_observer: Callable[..., Mapping[str, Any]] = observe_windows_native_routes,
) -> str:
    """Re-observe the takeover fence immediately before preparation/apply."""

    runtime = _absolute(runtime_root, field="runtime_root")
    legacy = _absolute(legacy_runtime_root, field="legacy_runtime_root")
    archive = _archive_for_digest(runtime, freeze_digest)
    if archive.get("legacy_runtime_root") != str(legacy):
        _fail("m9a_legacy_binding_mismatch", "M9a archive binds a different Legacy runtime")
    plan = query_client_plan(runtime_root=runtime)["plan"]
    if any(archive.get(field) != plan.get(field) for field in ("source_head", "source_tree", "client_plan_sha256")):
        _fail("m9a_client_binding_mismatch", "M9a archive is stale for the current staged clients")
    require_client_verification(runtime_root=runtime, expected_client_plan_sha256=plan["client_plan_sha256"])
    routes = _normalize_routes(route_observer(runtime_root=runtime))
    if routes["legacy_route_present"] or routes["target_conflict"] or not routes["target_registered"]:
        _fail("m9a_route_fence_stale", "Browser/Native handoff route fence changed")

    profiles_raw = archive.get("profiles")
    if not isinstance(profiles_raw, list) or not profiles_raw:
        _fail("m9a_archive_invalid", "M9a archive profile binding is missing")
    profiles: list[tuple[str, Path]] = []
    for item in profiles_raw:
        if not isinstance(item, Mapping):
            _fail("m9a_archive_invalid", "M9a archive profile binding is invalid")
        profile_id, raw_path = item.get("profile_id"), item.get("bridge_config_path")
        if not isinstance(profile_id, str) or not isinstance(raw_path, str):
            _fail("m9a_archive_invalid", "M9a archive profile fields differ")
        profiles.append((profile_id, Path(raw_path).expanduser().absolute()))

    scratch = runtime / "evidence" / "m9a-side-by-side" / "scratch" / uuid.uuid4().hex
    try:
        current = _probe_once(profiles=profiles, native_config=legacy / "native-host.json", scratch=scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    second = _read_object(runtime, archive["second_probe_ref"])
    archived_profiles = second.get("profiles")
    if not isinstance(archived_profiles, Mapping):
        _fail("m9a_archive_invalid", "M9a archived probe set is invalid")
    if {
        profile_id: probe.get("probe_digest") for profile_id, probe in current.items()
    } != {
        str(profile_id): probe.get("probe_digest")
        for profile_id, probe in archived_profiles.items()
        if isinstance(probe, Mapping)
    }:
        _fail("m9a_legacy_drift", "Legacy takeover-sensitive state changed after M9a capture")
    return freeze_digest


__all__ = [
    "FREEZE_REPORT_SCHEMA",
    "HANDOFF_ARCHIVE_SCHEMA",
    "HANDOFF_RESULT_SCHEMA",
    "HANDOFF_SCOPE",
    "M9aHandoffError",
    "capture_side_by_side_handoff",
    "revalidate_side_by_side_digest",
    "verify_side_by_side_archive",
    "verify_side_by_side_report",
]

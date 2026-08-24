"""M11c Windows Browser/Native client staging and verification.

This module never activates BDB Next. It stages exact Browser/Native subjects,
records a non-authoritative Browser launch witness, and manages the dedicated
vNext Native Messaging registration. The external ProgramData Bootstrap remains
the only production activation authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NoReturn

from bdb_shared.evidence import canonical_json_bytes
from bdb_vnext.bootstrap import _absolute_path, _load_json
from bdb_vnext.composition import (
    BROWSER_EXTENSION_ID,
    GENERATION_ID,
    NATIVE_HOST_NAME,
    PROTOCOL_GENERATION,
    RUNTIME_ID,
    load_browser_identity,
)
from bdb_vnext.m11c_native_artifact import NATIVE_ARTIFACT_MANIFEST, verify_native_artifact


CLIENT_PLAN_SCHEMA = "bdb-vnext-m11c-client-plan-v1"
CLIENT_VERIFICATION_SCHEMA = "bdb-vnext-m11c-client-verification-v1"
BROWSER_BUNDLE_SCHEMA = "bdb-vnext-m11c-browser-bundle-v1"
NATIVE_CONFIG_SCHEMA = "bdb-vnext-native-host-config-v2"
BROWSER_INSTALL_MODE = "OPERATOR_LOAD_UNPACKED"
LEGACY_NATIVE_HOST_NAME = "com.bartosz.dev_bridge"
TARGET_REGISTRY_SUBKEY = rf"Software\Google\Chrome\NativeMessagingHosts\{NATIVE_HOST_NAME}"
LEGACY_REGISTRY_SUBKEY = rf"Software\Google\Chrome\NativeMessagingHosts\{LEGACY_NATIVE_HOST_NAME}"
TARGET_REGISTRY_KEY = rf"HKCU\{TARGET_REGISTRY_SUBKEY}"
LEGACY_REGISTRY_KEY = rf"HKCU\{LEGACY_REGISTRY_SUBKEY}"

_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_BROWSER_FILES = 64
_MAX_BROWSER_BYTES = 8 * 1024 * 1024


class M11cClientError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> NoReturn:
    raise M11cClientError(code, message)


def _digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _digest(value: Mapping[str, Any]) -> str:
    return _digest_bytes(canonical_json_bytes(value))


def _check_digest(value: object, field: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        _fail("invalid_digest", f"{field} must be an exact sha256 digest")
    return value


def _check_sha40(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA40.fullmatch(value) is None:
        _fail("invalid_source_identity", f"{field} must be an exact 40-character Git SHA")
    return value


def _plan_path(runtime: Path) -> Path:
    return runtime / "clients" / "client-plan.json"


def client_verification_path(runtime_root: str | Path) -> Path:
    return _absolute_path(runtime_root, field="runtime_root") / "clients" / "browser-client-verification.json"


def _atomic_json(path: Path, document: Mapping[str, Any], *, immutable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if immutable and path.exists():
        if _load_json(path, field="m11c_client_evidence") != document:
            _fail("client_evidence_conflict", "existing client evidence differs")
        return
    staging = path.parent / f".{path.name}.partial-{uuid.uuid4().hex}"
    try:
        with staging.open("xb") as handle:
            handle.write(canonical_json_bytes(document))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, path)
    except OSError as exc:
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            pass
        raise M11cClientError("client_evidence_write_failed", "client evidence publication failed") from exc


def inspect_browser_bundle(root: str | Path) -> dict[str, Any]:
    source = _absolute_path(root, field="browser_bundle_root")
    if source.is_symlink() or not source.is_dir():
        _fail("browser_bundle_invalid", "Browser bundle root must be a regular directory")
    files: list[dict[str, Any]] = []
    total = 0
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            _fail("browser_bundle_invalid", "Browser bundle may not contain symlinks")
        if not path.is_file():
            continue
        relative = path.relative_to(source).as_posix()
        payload = path.read_bytes()
        total += len(payload)
        files.append({"path": relative, "size": len(payload), "sha256": _digest_bytes(payload)})
        if len(files) > _MAX_BROWSER_FILES or total > _MAX_BROWSER_BYTES:
            _fail("browser_bundle_too_large", "Browser bundle exceeds bounded staging limits")
    if not files or not any(item["path"] == "manifest.json" for item in files):
        _fail("browser_bundle_invalid", "Browser bundle is missing manifest.json")
    try:
        manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise M11cClientError("browser_manifest_invalid", "Browser manifest is not valid JSON") from exc
    identity = load_browser_identity()
    if (
        not isinstance(manifest, Mapping)
        or manifest.get("manifest_version") != 3
        or manifest.get("key") != identity["public_key_spki_der_base64"]
        or identity["extension_id"] != BROWSER_EXTENSION_ID
    ):
        _fail("browser_manifest_invalid", "Browser manifest identity differs")
    payload = {
        "schema": BROWSER_BUNDLE_SCHEMA,
        "extension_id": BROWSER_EXTENSION_ID,
        "file_count": len(files),
        "total_bytes": total,
        "files": files,
    }
    return {**payload, "bundle_digest": _digest(payload)}


def _copy_browser_bundle(source: Path, target: Path, expected_digest: str) -> None:
    if target.exists():
        observed = inspect_browser_bundle(target)
        if observed["bundle_digest"] != expected_digest:
            _fail("browser_stage_conflict", "existing staged Browser bundle differs")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}.partial-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        for item in inspect_browser_bundle(source)["files"]:
            relative = Path(item["path"])
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / relative, destination)
        if inspect_browser_bundle(staging)["bundle_digest"] != expected_digest:
            _fail("browser_stage_mismatch", "staged Browser bytes differ from source")
        os.replace(staging, target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _copy_native_executable(source: Path, target: Path, expected_digest: str) -> None:
    """Place the exact Native bytes inside the staged runtime.

    Chrome launches the executable from the Native Messaging manifest without
    knowing the staged config path.  Keeping the exact bytes under
    ``runtime/clients/native-host`` lets the frozen entrypoint resolve the
    matching runtime config structurally, while preserving the artifact
    digest and refusing to overwrite a conflicting staged executable.
    """

    if target.exists():
        if target.is_symlink() or not target.is_file() or _digest_bytes(target.read_bytes()) != expected_digest:
            _fail("native_stage_conflict", "existing staged Native Host bytes differ")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}.partial-{uuid.uuid4().hex}"
    try:
        shutil.copyfile(source, staging)
        if _digest_bytes(staging.read_bytes()) != expected_digest:
            _fail("native_stage_mismatch", "staged Native Host bytes differ from source")
        os.replace(staging, target)
    except Exception:
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _copy_native_payload(source_executable: Path, target_directory: Path, *, source_head: str, source_tree: str) -> dict[str, Any] | None:
    """Copy a verified onedir payload; retain standalone onefile compatibility."""

    artifact_manifest = source_executable.parent / NATIVE_ARTIFACT_MANIFEST
    if not artifact_manifest.is_file():
        return None
    try:
        artifact = verify_native_artifact(
            artifact_manifest,
            expected_source_head=source_head,
            expected_source_tree=source_tree,
        )
    except Exception as exc:
        raise M11cClientError("native_artifact_invalid", "Native Host payload failed exact artifact verification") from exc
    for relative, _size, expected_digest in artifact.payload_files:
        source = artifact.payload_root / Path(relative)
        target = target_directory / Path(relative)
        _copy_native_executable(source, target, expected_digest)
    copied_manifest = target_directory / artifact.manifest_path.name
    _copy_native_executable(artifact.manifest_path, copied_manifest, _digest_bytes(artifact.manifest_path.read_bytes()))
    return {
        "native_artifact_kind": artifact.artifact_kind,
        "native_payload_sha256": artifact.payload_sha256,
        "native_payload_size_bytes": artifact.payload_size_bytes,
        "native_artifact_manifest_path": str(copied_manifest),
        "native_artifact_manifest_sha256": artifact.manifest_sha256,
    }


def _native_config(*, runtime: Path, legacy: Path, bootstrap: Path) -> dict[str, Any]:
    return {
        "schema": NATIVE_CONFIG_SCHEMA,
        "generation_id": GENERATION_ID,
        "protocol_generation": PROTOCOL_GENERATION,
        "native_host_name": NATIVE_HOST_NAME,
        "browser_extension_id": BROWSER_EXTENSION_ID,
        "runtime_root": str(runtime),
        "legacy_runtime_root": str(legacy),
        "bootstrap_authority_root": str(bootstrap),
    }


def _native_manifest(executable: Path) -> dict[str, Any]:
    return {
        "name": NATIVE_HOST_NAME,
        "description": "Bartosz Dev Bridge vNext Native Messaging host",
        "path": str(executable),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{BROWSER_EXTENSION_ID}/"],
    }


def _client_plan_document(
    *,
    runtime: Path,
    source_head: str,
    source_tree: str,
    browser_bundle_root: Path,
    browser_bundle_digest: str,
    executable: Path,
    executable_digest: str,
    config_path: Path,
    config: Mapping[str, Any],
    manifest_path: Path,
    native_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the canonical path-bound client plan for one runtime root."""

    payload = {
        "schema": CLIENT_PLAN_SCHEMA,
        "runtime_id": RUNTIME_ID,
        "generation_id": GENERATION_ID,
        "protocol_generation": PROTOCOL_GENERATION,
        "source_head": source_head,
        "source_tree": source_tree,
        "browser_extension_id": BROWSER_EXTENSION_ID,
        "browser_install_mode": BROWSER_INSTALL_MODE,
        "browser_operator_action_required": True,
        "browser_bundle_root": str(browser_bundle_root),
        "browser_bundle_digest": browser_bundle_digest,
        "native_host_name": NATIVE_HOST_NAME,
        "native_host_executable": str(executable),
        "native_host_executable_sha256": executable_digest,
        "native_config_path": str(config_path),
        "native_config_sha256": _digest_bytes(canonical_json_bytes(config)),
        "native_manifest_path": str(manifest_path),
        "native_manifest_sha256": _digest_bytes(canonical_json_bytes(_native_manifest(executable))),
        "target_registry_key": TARGET_REGISTRY_KEY,
        "legacy_native_host_name": LEGACY_NATIVE_HOST_NAME,
        "legacy_registry_key": LEGACY_REGISTRY_KEY,
        "production_activation_performed": False,
    }
    if native_payload is not None:
        payload.update(native_payload)
    return {**payload, "client_plan_sha256": _digest(payload)}


def stage_client_plan(
    *,
    runtime_root: str | Path,
    legacy_runtime_root: str | Path,
    bootstrap_authority_root: str | Path,
    browser_source_root: str | Path,
    native_host_executable: str | Path,
    source_head: str,
    source_tree: str,
) -> dict[str, Any]:
    """Stage exact client bytes/configuration without activating either route."""

    runtime = _absolute_path(runtime_root, field="runtime_root")
    legacy = _absolute_path(legacy_runtime_root, field="legacy_runtime_root")
    bootstrap = _absolute_path(bootstrap_authority_root, field="bootstrap_authority_root")
    source_head = _check_sha40(source_head, "source_head")
    source_tree = _check_sha40(source_tree, "source_tree")
    browser_source = _absolute_path(browser_source_root, field="browser_source_root")
    executable = _absolute_path(native_host_executable, field="native_host_executable")
    if executable.is_symlink() or not executable.is_file():
        _fail("native_host_executable_invalid", "vNext Native Host executable must be a regular file")

    browser = inspect_browser_bundle(browser_source)
    staged_browser = runtime / "clients" / "browser-extension"
    _copy_browser_bundle(browser_source, staged_browser, browser["bundle_digest"])

    config_path = runtime / "config" / "native-host.json"
    staged_executable = runtime / "clients" / "native-host" / executable.name
    executable_digest = _digest_bytes(executable.read_bytes())
    native_payload = _copy_native_payload(
        executable,
        staged_executable.parent,
        source_head=source_head,
        source_tree=source_tree,
    )
    if native_payload is None:
        _copy_native_executable(executable, staged_executable, executable_digest)
    manifest_path = runtime / "clients" / "native-host" / f"{NATIVE_HOST_NAME}.json"
    config = _native_config(runtime=runtime, legacy=legacy, bootstrap=bootstrap)
    native_manifest = _native_manifest(staged_executable)
    _atomic_json(config_path, config)
    _atomic_json(manifest_path, native_manifest)

    document = _client_plan_document(
        runtime=runtime,
        source_head=source_head,
        source_tree=source_tree,
        browser_bundle_root=staged_browser,
        browser_bundle_digest=browser["bundle_digest"],
        executable=staged_executable,
        executable_digest=executable_digest,
        config_path=config_path,
        config=config,
        manifest_path=manifest_path,
        native_payload=native_payload,
    )
    _atomic_json(_plan_path(runtime), document, immutable=True)
    return query_client_plan(runtime_root=runtime)


def _load_plan(runtime: Path) -> dict[str, Any]:
    document = _load_json(_plan_path(runtime), field="m11c_client_plan")
    if (
        document.get("schema") != CLIENT_PLAN_SCHEMA
        or document.get("runtime_id") != RUNTIME_ID
        or document.get("generation_id") != GENERATION_ID
        or document.get("protocol_generation") != PROTOCOL_GENERATION
        or document.get("browser_extension_id") != BROWSER_EXTENSION_ID
        or document.get("native_host_name") != NATIVE_HOST_NAME
        or document.get("browser_install_mode") != BROWSER_INSTALL_MODE
        or document.get("browser_operator_action_required") is not True
        or document.get("target_registry_key") != TARGET_REGISTRY_KEY
        or document.get("legacy_registry_key") != LEGACY_REGISTRY_KEY
        or document.get("production_activation_performed") is not False
    ):
        _fail("client_plan_identity_mismatch", "M11c client plan identity differs")
    supplied = _check_digest(document.get("client_plan_sha256"), "client_plan_sha256")
    payload = dict(document)
    payload.pop("client_plan_sha256", None)
    if _digest(payload) != supplied:
        _fail("client_plan_digest_mismatch", "M11c client plan digest differs")
    _check_sha40(document.get("source_head"), "source_head")
    _check_sha40(document.get("source_tree"), "source_tree")
    return document


def query_client_plan(*, runtime_root: str | Path) -> dict[str, Any]:
    runtime = _absolute_path(runtime_root, field="runtime_root")
    plan = _load_plan(runtime)
    browser = inspect_browser_bundle(plan["browser_bundle_root"])
    if browser["bundle_digest"] != plan["browser_bundle_digest"]:
        _fail("browser_bundle_stale", "staged Browser bundle differs from client plan")
    executable = Path(plan["native_host_executable"])
    if not executable.is_file() or _digest_bytes(executable.read_bytes()) != plan["native_host_executable_sha256"]:
        _fail("native_host_executable_stale", "Native Host executable differs from client plan")
    if "native_artifact_manifest_path" in plan:
        try:
            artifact = verify_native_artifact(
                plan["native_artifact_manifest_path"],
                expected_source_head=plan["source_head"],
                expected_source_tree=plan["source_tree"],
            )
        except Exception as exc:
            raise M11cClientError("native_payload_stale", "Native Host onedir payload differs from client plan") from exc
        if (
            artifact.artifact_kind != plan.get("native_artifact_kind")
            or artifact.payload_sha256 != plan.get("native_payload_sha256")
            or artifact.payload_size_bytes != plan.get("native_payload_size_bytes")
            or artifact.manifest_sha256 != plan.get("native_artifact_manifest_sha256")
        ):
            _fail("native_payload_stale", "Native Host onedir payload receipt differs from client plan")
    config_path = Path(plan["native_config_path"])
    manifest_path = Path(plan["native_manifest_path"])
    if not config_path.is_file() or _digest_bytes(config_path.read_bytes()) != plan["native_config_sha256"]:
        _fail("native_config_stale", "Native Host config differs from client plan")
    if not manifest_path.is_file() or _digest_bytes(manifest_path.read_bytes()) != plan["native_manifest_sha256"]:
        _fail("native_manifest_stale", "Native Messaging manifest differs from client plan")
    manifest = _load_json(manifest_path, field="native_manifest")
    if manifest != _native_manifest(executable):
        _fail("native_manifest_identity_mismatch", "Native Messaging manifest identity differs")
    return {
        "schema": CLIENT_PLAN_SCHEMA,
        "plan": plan,
        "browser": browser,
        "production_activation_performed": False,
    }


def _browser_observation_payload(observation: Mapping[str, Any]) -> dict[str, Any]:
    expected_keys = {"schema", "extension_id", "file_count", "total_bytes", "files"}
    if set(observation) != expected_keys:
        _fail("browser_runtime_bundle_mismatch", "Browser runtime observation fields differ")
    if observation.get("schema") != BROWSER_BUNDLE_SCHEMA or observation.get("extension_id") != BROWSER_EXTENSION_ID:
        _fail("browser_runtime_bundle_mismatch", "Browser runtime observation identity differs")
    file_count = observation.get("file_count")
    total_bytes = observation.get("total_bytes")
    files = observation.get("files")
    if isinstance(file_count, bool) or not isinstance(file_count, int) or file_count < 1 or file_count > _MAX_BROWSER_FILES:
        _fail("browser_runtime_bundle_mismatch", "Browser runtime file count is invalid")
    if isinstance(total_bytes, bool) or not isinstance(total_bytes, int) or total_bytes < 1 or total_bytes > _MAX_BROWSER_BYTES:
        _fail("browser_runtime_bundle_mismatch", "Browser runtime byte count is invalid")
    if not isinstance(files, list) or len(files) != file_count:
        _fail("browser_runtime_bundle_mismatch", "Browser runtime file inventory is incomplete")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    observed_total = 0
    for item in files:
        if not isinstance(item, Mapping) or set(item) != {"path", "size", "sha256"}:
            _fail("browser_runtime_bundle_mismatch", "Browser runtime file record differs")
        path = item.get("path")
        size = item.get("size")
        digest = item.get("sha256")
        if not isinstance(path, str) or not path or path.startswith(('/', '\\')) or ".." in Path(path).parts or path in seen:
            _fail("browser_runtime_bundle_mismatch", "Browser runtime file path is invalid")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0 or size > _MAX_BROWSER_BYTES:
            _fail("browser_runtime_bundle_mismatch", "Browser runtime file size is invalid")
        _check_digest(digest, "browser_runtime_file.sha256")
        seen.add(path)
        observed_total += size
        normalized.append({"path": path, "size": size, "sha256": digest})
    if observed_total != total_bytes:
        _fail("browser_runtime_bundle_mismatch", "Browser runtime byte total differs")
    return {
        "schema": BROWSER_BUNDLE_SCHEMA,
        "extension_id": BROWSER_EXTENSION_ID,
        "file_count": file_count,
        "total_bytes": total_bytes,
        "files": normalized,
    }


def record_browser_launch_verification(
    *,
    runtime_root: str | Path,
    caller_origin: str,
    browser_observation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Record Chrome->Native proof only for the exact staged Browser package."""

    runtime = _absolute_path(runtime_root, field="runtime_root")
    expected_origin = f"chrome-extension://{BROWSER_EXTENSION_ID}/"
    if caller_origin != expected_origin:
        _fail("browser_origin_mismatch", "Chrome caller origin is not the pinned vNext extension")
    observed = query_client_plan(runtime_root=runtime)
    plan = observed["plan"]
    expected_browser = dict(observed["browser"])
    expected_browser.pop("bundle_digest", None)
    supplied_browser = expected_browser if browser_observation is None else _browser_observation_payload(browser_observation)
    if supplied_browser != expected_browser:
        _fail("browser_runtime_bundle_mismatch", "running Browser package differs from staged client plan")
    payload = {
        "schema": CLIENT_VERIFICATION_SCHEMA,
        "runtime_id": RUNTIME_ID,
        "generation_id": GENERATION_ID,
        "protocol_generation": PROTOCOL_GENERATION,
        "client_plan_sha256": plan["client_plan_sha256"],
        "browser_bundle_digest": plan["browser_bundle_digest"],
        "browser_extension_id": BROWSER_EXTENSION_ID,
        "caller_origin": caller_origin,
        "native_host_name": NATIVE_HOST_NAME,
        "native_launch_verified": True,
        "production_activation_performed": False,
    }
    document = {**payload, "verification_sha256": _digest(payload)}
    _atomic_json(client_verification_path(runtime), document, immutable=True)
    return document


def require_client_verification(*, runtime_root: str | Path, expected_client_plan_sha256: str) -> dict[str, Any]:
    runtime = _absolute_path(runtime_root, field="runtime_root")
    expected_plan = _check_digest(expected_client_plan_sha256, "expected_client_plan_sha256")
    current = query_client_plan(runtime_root=runtime)
    plan = current["plan"]
    document = _load_json(client_verification_path(runtime), field="m11c_client_verification")
    if (
        document.get("schema") != CLIENT_VERIFICATION_SCHEMA
        or document.get("runtime_id") != RUNTIME_ID
        or document.get("generation_id") != GENERATION_ID
        or document.get("protocol_generation") != PROTOCOL_GENERATION
        or document.get("client_plan_sha256") != expected_plan
        or document.get("browser_bundle_digest") != plan["browser_bundle_digest"]
        or document.get("browser_extension_id") != BROWSER_EXTENSION_ID
        or document.get("caller_origin") != f"chrome-extension://{BROWSER_EXTENSION_ID}/"
        or document.get("native_host_name") != NATIVE_HOST_NAME
        or document.get("native_launch_verified") is not True
        or document.get("production_activation_performed") is not False
    ):
        _fail("client_verification_invalid", "Browser/Native client verification differs")
    supplied = _check_digest(document.get("verification_sha256"), "verification_sha256")
    payload = dict(document)
    payload.pop("verification_sha256", None)
    if _digest(payload) != supplied:
        _fail("client_verification_digest_mismatch", "Browser/Native verification digest differs")
    return document


def _winreg_module():
    if os.name != "nt":
        _fail("windows_required", "Native Messaging registry operations require Windows")
    import winreg  # type: ignore[import-not-found]

    return winreg


def _registry_default(root: object, subkey: str, access: int) -> str | None:
    winreg = _winreg_module()
    try:
        with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ | access) as key:
            value, kind = winreg.QueryValueEx(key, None)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise M11cClientError("registry_read_failed", "Native Messaging registry cannot be inspected") from exc
    if kind != winreg.REG_SZ or not isinstance(value, str) or not value:
        _fail("registry_value_invalid", "Native Messaging registry default value is invalid")
    return value


def observe_windows_native_routes(*, runtime_root: str | Path) -> dict[str, Any]:
    plan = query_client_plan(runtime_root=runtime_root)["plan"]
    winreg = _winreg_module()
    target_values: list[dict[str, Any]] = []
    legacy_values: list[dict[str, Any]] = []
    for root_name, root in (("HKCU", winreg.HKEY_CURRENT_USER), ("HKLM", winreg.HKEY_LOCAL_MACHINE)):
        for view_name, view in (("32", winreg.KEY_WOW64_32KEY), ("64", winreg.KEY_WOW64_64KEY)):
            target = _registry_default(root, TARGET_REGISTRY_SUBKEY, view)
            legacy = _registry_default(root, LEGACY_REGISTRY_SUBKEY, view)
            if target is not None:
                target_values.append({"root": root_name, "view": view_name, "value": target})
            if legacy is not None:
                legacy_values.append({"root": root_name, "view": view_name, "value": legacy})
    expected = os.path.normcase(str(Path(plan["native_manifest_path"])))
    conflicting = [
        item
        for item in target_values
        if item["root"] != "HKCU" or os.path.normcase(item["value"]) != expected
    ]
    exact_hkcu_views = {
        item["view"]
        for item in target_values
        if item["root"] == "HKCU" and os.path.normcase(item["value"]) == expected
    }
    return {
        "target": target_values,
        "legacy": legacy_values,
        "target_conflict": bool(conflicting),
        "target_registered": exact_hkcu_views == {"32", "64"} and not conflicting,
        "target_registered_views": sorted(exact_hkcu_views),
        "legacy_route_present": bool(legacy_values),
        "production_activation_performed": False,
    }


def _backup_prior_target_route(runtime: Path, before: Mapping[str, Any], expected_manifest: str) -> None:
    payload = {
        "schema": "bdb-vnext-m11c-prior-target-native-route-backup-v1",
        "target": list(before["target"]),
        "replacement_manifest": expected_manifest,
        "production_activation_performed": False,
    }
    document = {**payload, "backup_sha256": _digest(payload)}
    _atomic_json(runtime / "clients" / "prior-target-native-route-backup.json", document, immutable=True)



def set_windows_target_native_route_view(
    *,
    runtime_root: str | Path,
    view: str,
    manifest_path: str,
) -> None:
    """Write exactly one HKCU target view for the canonical route transition.

    This helper is intentionally not a public activation command. It is used
    only while the post-ACTIVE maintenance lock holds the exact route plan.
    """
    if view not in {"32", "64"}:
        _fail("registry_view_invalid", "Native target registry view is invalid")
    runtime = _absolute_path(runtime_root, field="runtime_root")
    _ = query_client_plan(runtime_root=runtime)
    target = Path(manifest_path).expanduser()
    if not target.is_absolute() or target.is_symlink() or not target.is_file():
        _fail("native_manifest_invalid", "Native target manifest must be an existing regular file")
    winreg = _winreg_module()
    wow_view = winreg.KEY_WOW64_32KEY if view == "32" else winreg.KEY_WOW64_64KEY
    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            TARGET_REGISTRY_SUBKEY,
            0,
            winreg.KEY_SET_VALUE | wow_view,
        ) as key:
            winreg.SetValueEx(key, None, 0, winreg.REG_SZ, str(target))
    except OSError as exc:
        raise M11cClientError("registry_write_failed", "vNext Native Messaging route transition failed") from exc


def register_windows_target_native_host(
    *,
    runtime_root: str | Path,
    replace_existing_target: bool = False,
) -> dict[str, Any]:
    """Register exact vNext host in both HKCU views; optionally migrate a stale HKCU rehearsal route."""

    runtime = _absolute_path(runtime_root, field="runtime_root")
    plan = query_client_plan(runtime_root=runtime)["plan"]
    before = observe_windows_native_routes(runtime_root=runtime)
    if before["target_conflict"]:
        if any(item["root"] == "HKLM" for item in before["target"]):
            _fail("target_native_route_requires_admin", "conflicting vNext Native route exists in HKLM")
        if replace_existing_target is not True:
            _fail("target_native_route_conflict", "another vNext Native Messaging registration differs")
        _backup_prior_target_route(runtime, before, str(Path(plan["native_manifest_path"])))

    winreg = _winreg_module()
    expected = str(Path(plan["native_manifest_path"]))
    try:
        for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
            with winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER,
                TARGET_REGISTRY_SUBKEY,
                0,
                winreg.KEY_SET_VALUE | view,
            ) as key:
                winreg.SetValueEx(key, None, 0, winreg.REG_SZ, expected)
    except OSError as exc:
        raise M11cClientError("registry_write_failed", "vNext Native Messaging registration failed") from exc
    after = observe_windows_native_routes(runtime_root=runtime)
    if after["target_conflict"] or after["target_registered"] is not True:
        _fail("target_native_route_unverified", "vNext Native Messaging registration could not be re-observed")
    return after


def disable_windows_legacy_native_route(*, runtime_root: str | Path) -> dict[str, Any]:
    """Disable Legacy Native Messaging only at the explicit final cutover boundary."""

    runtime = _absolute_path(runtime_root, field="runtime_root")
    before = observe_windows_native_routes(runtime_root=runtime)
    if before["target_conflict"] or not before["target_registered"]:
        _fail("target_native_route_unverified", "target Native route must be exact before Legacy is disabled")
    if not before["legacy_route_present"]:
        return before
    if any(item["root"] == "HKLM" for item in before["legacy"]):
        _fail("legacy_native_route_requires_admin", "Legacy Native route exists in HKLM and cannot be silently disabled")
    backup_payload = {
        "schema": "bdb-vnext-m11c-legacy-native-route-backup-v1",
        "legacy": before["legacy"],
        "production_activation_performed": False,
    }
    backup = {**backup_payload, "backup_sha256": _digest(backup_payload)}
    _atomic_json(runtime / "clients" / "legacy-native-route-backup.json", backup, immutable=True)

    winreg = _winreg_module()
    for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
        try:
            winreg.DeleteKeyEx(winreg.HKEY_CURRENT_USER, LEGACY_REGISTRY_SUBKEY, view, 0)
        except FileNotFoundError:
            pass
        except AttributeError:
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, LEGACY_REGISTRY_SUBKEY)
            except FileNotFoundError:
                pass
        except OSError as exc:
            raise M11cClientError("legacy_route_disable_failed", "Legacy Native Messaging route could not be disabled") from exc
    after = observe_windows_native_routes(runtime_root=runtime)
    if after["legacy_route_present"]:
        _fail("legacy_route_still_present", "Legacy Native Messaging route remains visible after disable")
    return after


__all__ = [
    "BROWSER_INSTALL_MODE",
    "CLIENT_PLAN_SCHEMA",
    "CLIENT_VERIFICATION_SCHEMA",
    "M11cClientError",
    "client_verification_path",
    "disable_windows_legacy_native_route",
    "inspect_browser_bundle",
    "observe_windows_native_routes",
    "record_browser_launch_verification",
    "register_windows_target_native_host",
    "set_windows_target_native_route_view",
    "require_client_verification",
    "stage_client_plan",
    "query_client_plan",
]

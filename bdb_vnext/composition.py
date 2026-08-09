"""Read-only M1a identity/composition contract; it never activates or creates vNext state."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bdb_shared.evidence import canonical_json_bytes, sanitize_report, semantic_digest


COMPOSITION_SCHEMA = "bdb-vnext-composition-v1"
STATUS_SCHEMA = "bdb-vnext-composition-status-v1"
ARCHITECTURE_FREEZE = "BDB Architecture Freeze v1"
EXECUTION_STRATEGY = "parallel-vnext-build-v1"
SOURCE_BRANCH = "bdb-vnext"
GENERATION_ID = "bdb-vnext-g1"
RUNTIME_ID = "devmaster.bdb.vnext.runtime"
PROTOCOL_GENERATION = "bdb-vnext-protocol-v1"
CONFIG_GENERATION = "bdb-vnext-config-v1"
CONTROL_STORE_SCHEMA = "bdb-vnext-control-store-v1"
NATIVE_HOST_NAME = "com.bartosz.dev_bridge.vnext"
LEGACY_NATIVE_HOST_NAME = "com.bartosz.dev_bridge"
BROWSER_COMPONENT_ID = "devmaster.bdb.vnext.browser-extension"
BROWSER_IDENTITY_SCHEMA = "bdb-vnext-browser-identity-v1"
BROWSER_EXTENSION_ID = "mopnolkjddkmgojfjkenjobehhmmklll"
NATIVE_COMPONENT_ID = "devmaster.bdb.vnext.native-host"
CONTROL_CENTER_COMPONENT_ID = "devmaster.bdb.vnext.control-center"
COMPOSITION_PROVIDER_ID = "devmaster.bdb.vnext.composition-manifest"
CONTROL_PROVIDER_ID = "devmaster.bdb.vnext.control-store"
NATIVE_PROVIDER_ID = "devmaster.bdb.vnext.native-transport"
BROWSER_PROVIDER_ID = "devmaster.bdb.vnext.browser-transport"
CONTROL_CENTER_PROVIDER_ID = "devmaster.bdb.vnext.control-center-query"
REPO_VIEW_COMPONENT_ID = "devmaster.bdb.vnext.repo-view"
REPO_VIEW_PROVIDER_ID = "devmaster.bdb.vnext.repo-view"

_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_EXTENSION_ID = re.compile(r"^[a-p]{32}$")
_MAX_MANIFEST_BYTES = 256 * 1024
_MAX_IDENTITY_BYTES = 32 * 1024
_MAX_BUNDLE_FILES = 4_096
_MAX_BUNDLE_FILE_BYTES = 64 * 1024 * 1024
_MAX_BUNDLE_TOTAL_BYTES = 512 * 1024 * 1024
_HEX_TO_EXTENSION_ID = str.maketrans("0123456789abcdef", "abcdefghijklmnop")
_BUNDLE_COMPONENT_IDS = (
    RUNTIME_ID,
    BROWSER_COMPONENT_ID,
    NATIVE_COMPONENT_ID,
    CONTROL_CENTER_COMPONENT_ID,
)


class VNextCompositionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def default_local_app_data_root() -> Path:
    value = os.environ.get("LOCALAPPDATA")
    if value:
        return Path(value).expanduser().resolve(strict=False)
    return (Path.home() / "AppData" / "Local").resolve(strict=False)


def default_vnext_runtime_root() -> Path:
    return default_local_app_data_root() / "BartoszDevBridge-vNext"


def default_legacy_runtime_root() -> Path:
    return default_local_app_data_root() / "BartoszDevBridge"


def default_browser_identity_path() -> Path:
    return Path(__file__).with_name("browser_identity.json")


def _file_token(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _read_stable_bytes(path: str | Path, *, max_bytes: int, field: str) -> tuple[Path, bytes]:
    source = Path(path).expanduser().absolute()
    _assert_no_reparse_components(source, code="invalid_artifact_path", field=field)
    before = source.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise VNextCompositionError("invalid_artifact_path", f"{field} must be a regular file")
    if before.st_size > max_bytes:
        raise VNextCompositionError("artifact_too_large", f"{field} exceeds its bounded read limit")
    with source.open("rb") as handle:
        payload = handle.read(max_bytes + 1)
    after = source.stat(follow_symlinks=False)
    if _file_token(before) != _file_token(after):
        raise VNextCompositionError("artifact_unstable", f"{field} changed during observation")
    if len(payload) > max_bytes:
        raise VNextCompositionError("artifact_too_large", f"{field} exceeds its bounded read limit")
    return source.resolve(strict=True), payload


def _derive_extension_id(public_key_der: bytes) -> str:
    return hashlib.sha256(public_key_der).hexdigest()[:32].translate(_HEX_TO_EXTENSION_ID)


def load_browser_identity(path: str | Path | None = None) -> dict[str, Any]:
    source, payload = _read_stable_bytes(
        path if path is not None else default_browser_identity_path(),
        max_bytes=_MAX_IDENTITY_BYTES,
        field="browser_identity",
    )
    try:
        value = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise VNextCompositionError("invalid_browser_identity", "Browser identity is not valid JSON") from exc
    identity = _mapping(value, field="browser_identity")
    _exact_keys(
        identity,
        {
            "schema",
            "component_id",
            "extension_id",
            "algorithm",
            "public_key_spki_der_base64",
            "public_key_sha256",
            "private_key_in_repository",
            "semantic_digest",
        },
        field="browser_identity",
    )
    if (
        identity.get("schema") != BROWSER_IDENTITY_SCHEMA
        or identity.get("component_id") != BROWSER_COMPONENT_ID
        or identity.get("algorithm") != "RSA-2048-SPKI-SHA256"
        or identity.get("private_key_in_repository") is not False
    ):
        raise VNextCompositionError("browser_identity_mismatch", "Browser packaging identity differs")
    encoded_key = identity.get("public_key_spki_der_base64")
    if not isinstance(encoded_key, str):
        raise VNextCompositionError("invalid_browser_identity", "Browser public key is missing")
    try:
        public_key = base64.b64decode(encoded_key, validate=True)
    except ValueError as exc:
        raise VNextCompositionError("invalid_browser_identity", "Browser public key is invalid") from exc
    key_digest = "sha256:" + hashlib.sha256(public_key).hexdigest()
    extension_id = _derive_extension_id(public_key)
    if len(public_key) < 256 or identity.get("public_key_sha256") != key_digest:
        raise VNextCompositionError("browser_key_mismatch", "Browser public key digest differs")
    if identity.get("extension_id") != extension_id or extension_id != BROWSER_EXTENSION_ID:
        raise VNextCompositionError("browser_extension_id_mismatch", "Browser extension ID differs")
    if identity.get("semantic_digest") != semantic_digest(identity):
        raise VNextCompositionError("browser_identity_digest_mismatch", "Browser identity digest differs")
    if source.name != "browser_identity.json":
        raise VNextCompositionError("browser_identity_mismatch", "Browser identity resource name differs")
    return dict(identity)


def _is_reparse(info: os.stat_result) -> bool:
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(info, "st_file_attributes", 0) & marker)


def _assert_no_reparse_components(path: Path, *, code: str, field: str) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        info = current.stat(follow_symlinks=False)
        if current.is_symlink() or _is_reparse(info):
            raise VNextCompositionError(
                code,
                f"{field} must not contain symlinks, junctions or reparse points",
            )


def _sha256_file(path: Path) -> tuple[int, str]:
    before = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or _is_reparse(before):
        raise VNextCompositionError("invalid_bundle_entry", "bundle entries must be regular files")
    if before.st_size > _MAX_BUNDLE_FILE_BYTES:
        raise VNextCompositionError("bundle_file_too_large", "bundle file exceeds the bounded read limit")
    digest = hashlib.sha256()
    observed = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            observed += len(chunk)
            if observed > _MAX_BUNDLE_FILE_BYTES:
                raise VNextCompositionError(
                    "bundle_file_too_large",
                    "bundle file exceeds the bounded read limit",
                )
            digest.update(chunk)
    after = path.stat(follow_symlinks=False)
    if _file_token(before) != _file_token(after):
        raise VNextCompositionError("bundle_unstable", "bundle file changed during observation")
    return observed, "sha256:" + digest.hexdigest()


def _directory_membership(path: Path) -> tuple[tuple[str, str, tuple[int, int, int, int]], ...]:
    entries: list[tuple[str, str, tuple[int, int, int, int]]] = []
    with os.scandir(path) as iterator:
        for entry in iterator:
            info = entry.stat(follow_symlinks=False)
            if entry.is_symlink() or _is_reparse(info):
                raise VNextCompositionError(
                    "bundle_reparse_point",
                    "bundle must not contain symlinks, junctions or reparse points",
                )
            if stat.S_ISDIR(info.st_mode):
                kind = "directory"
            elif stat.S_ISREG(info.st_mode):
                kind = "file"
            else:
                raise VNextCompositionError("invalid_bundle_entry", "bundle entry type is unsupported")
            entries.append((entry.name, kind, _file_token(info)))
    return tuple(sorted(entries, key=lambda item: item[0]))


def _observe_bundle_directory(root: Path) -> tuple[int, int, str]:
    pending = [root]
    memberships: dict[Path, tuple[tuple[str, str, tuple[int, int, int, int]], ...]] = {}
    records: list[dict[str, Any]] = []
    total_bytes = 0
    while pending:
        directory = pending.pop()
        membership = _directory_membership(directory)
        memberships[directory] = membership
        for name, kind, _token in reversed(membership):
            child = directory / name
            if kind == "directory":
                pending.append(child)
                continue
            if len(records) >= _MAX_BUNDLE_FILES:
                raise VNextCompositionError("bundle_file_cap", "bundle exceeds the bounded file-count limit")
            size_bytes, digest = _sha256_file(child)
            total_bytes += size_bytes
            if total_bytes > _MAX_BUNDLE_TOTAL_BYTES:
                raise VNextCompositionError("bundle_total_too_large", "bundle exceeds the total read limit")
            records.append(
                {
                    "path": child.relative_to(root).as_posix(),
                    "size_bytes": size_bytes,
                    "sha256": digest,
                }
            )
    if not records:
        raise VNextCompositionError("bundle_empty", "bundle directory must contain at least one file")
    for directory, before in memberships.items():
        if _directory_membership(directory) != before:
            raise VNextCompositionError("bundle_unstable", "bundle membership changed during observation")
    records.sort(key=lambda item: item["path"])
    payload = {"schema": "bdb-vnext-bundle-digest-v1", "files": records}
    digest = "sha256:" + hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    return len(records), total_bytes, digest


def _load_browser_bundle_manifest(root: Path, identity: Mapping[str, Any]) -> None:
    _source, payload = _read_stable_bytes(
        root / "manifest.json",
        max_bytes=_MAX_IDENTITY_BYTES,
        field="browser_bundle_manifest",
    )
    try:
        manifest = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise VNextCompositionError("invalid_browser_bundle", "Browser manifest is not valid JSON") from exc
    if not isinstance(manifest, Mapping):
        raise VNextCompositionError("invalid_browser_bundle", "Browser manifest must be an object")
    if manifest.get("manifest_version") != 3 or manifest.get("key") != identity["public_key_spki_der_base64"]:
        raise VNextCompositionError(
            "browser_bundle_identity_mismatch",
            "Browser bundle is not pinned to the vNext packaging identity",
        )
    if not isinstance(manifest.get("name"), str) or not manifest["name"].strip():
        raise VNextCompositionError("invalid_browser_bundle", "Browser bundle name is missing")


def _not_built_bundle(component_id: str) -> dict[str, Any]:
    return {
        "component_id": component_id,
        "state": "not_built",
        "kind": None,
        "path": None,
        "file_count": 0,
        "size_bytes": 0,
        "sha256": None,
    }


def observe_bundle(
    component_id: str,
    path: str | Path,
    *,
    legacy_runtime_root: str | Path,
) -> dict[str, Any]:
    if component_id not in _BUNDLE_COMPONENT_IDS:
        raise VNextCompositionError("unknown_bundle_component", "bundle component ID is unsupported")
    source = Path(path).expanduser()
    if not source.is_absolute():
        raise VNextCompositionError("relative_path", f"bundle[{component_id}] must be absolute")
    source = source.absolute()
    _assert_no_reparse_components(
        source,
        code="bundle_reparse_point",
        field=f"bundle[{component_id}]",
    )
    source = source.resolve(strict=True)
    legacy = _absolute_path(legacy_runtime_root, field="legacy_runtime_root")
    if _overlaps(source, legacy):
        raise VNextCompositionError("legacy_bundle_overlap", "vNext bundle observation overlaps legacy runtime")
    info = source.stat(follow_symlinks=False)
    if _is_reparse(info):
        raise VNextCompositionError("bundle_reparse_point", "bundle root must not be a reparse point")
    if stat.S_ISDIR(info.st_mode):
        if component_id == BROWSER_COMPONENT_ID:
            _load_browser_bundle_manifest(source, load_browser_identity())
        file_count, size_bytes, digest = _observe_bundle_directory(source)
        kind = "directory"
    elif stat.S_ISREG(info.st_mode):
        if component_id == BROWSER_COMPONENT_ID:
            raise VNextCompositionError(
                "invalid_browser_bundle",
                "M1a Browser packaging smoke requires an unpacked bundle directory",
            )
        size_bytes, file_digest = _sha256_file(source)
        file_count = 1
        digest_payload = {
            "schema": "bdb-vnext-bundle-digest-v1",
            "files": [{"path": source.name, "size_bytes": size_bytes, "sha256": file_digest}],
        }
        digest = "sha256:" + hashlib.sha256(canonical_json_bytes(digest_payload)).hexdigest()
        kind = "file"
    else:
        raise VNextCompositionError("invalid_bundle_entry", "bundle root type is unsupported")
    return {
        "component_id": component_id,
        "state": "observed",
        "kind": kind,
        "path": str(source),
        "file_count": file_count,
        "size_bytes": size_bytes,
        "sha256": digest,
    }


def _bundle_records(
    bundle_paths: Mapping[str, str | Path] | None,
    *,
    legacy_runtime_root: str | Path,
) -> list[dict[str, Any]]:
    supplied = dict(bundle_paths or {})
    unknown = set(supplied) - set(_BUNDLE_COMPONENT_IDS)
    if unknown:
        raise VNextCompositionError(
            "unknown_bundle_component",
            f"unsupported bundle component IDs: {sorted(unknown)}",
        )
    return [
        observe_bundle(component_id, supplied[component_id], legacy_runtime_root=legacy_runtime_root)
        if component_id in supplied
        else _not_built_bundle(component_id)
        for component_id in _BUNDLE_COMPONENT_IDS
    ]


def _absolute_path(value: str | Path, *, field: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise VNextCompositionError("relative_path", f"{field} must be absolute")
    return path.resolve(strict=False)


def _comparable(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.fspath(path)))


def _contained(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((_comparable(path), _comparable(root))) == _comparable(root)
    except ValueError:
        return False


def _overlaps(left: Path, right: Path) -> bool:
    return _contained(left, right) or _contained(right, left)


@dataclass(frozen=True)
class VNextLayout:
    runtime_root: Path
    control_store: Path
    spool_inbox: Path
    spool_results: Path
    receipts_store: Path
    instance_lock: Path
    pid_file: Path
    config: Path
    browser_bundle_root: Path
    native_host_manifest: Path

    @classmethod
    def create(cls, runtime_root: str | Path) -> "VNextLayout":
        root = _absolute_path(runtime_root, field="runtime_root")
        return cls(
            runtime_root=root,
            control_store=root / "control" / "control.db",
            spool_inbox=root / "transport" / "spool" / "inbox",
            spool_results=root / "transport" / "spool" / "results",
            receipts_store=root / "transport" / "receipts" / "native-requests.json",
            instance_lock=root / "coordination" / "bdb-vnext.lock",
            pid_file=root / "coordination" / "bdb-vnext.pid",
            config=root / "config" / "bdb-vnext.json",
            browser_bundle_root=root / "components" / "browser-extension",
            native_host_manifest=root / "config" / f"{NATIVE_HOST_NAME}.json",
        )

    def assert_isolated(
        self,
        *,
        legacy_runtime_root: str | Path,
        forbidden_roots: Iterable[str | Path] = (),
    ) -> None:
        legacy = _absolute_path(legacy_runtime_root, field="legacy_runtime_root")
        if _overlaps(self.runtime_root, legacy):
            raise VNextCompositionError(
                "legacy_runtime_overlap",
                "vNext and legacy runtime roots must not overlap",
            )
        for index, value in enumerate(forbidden_roots):
            root = _absolute_path(value, field=f"forbidden_roots[{index}]")
            if _overlaps(self.runtime_root, root):
                raise VNextCompositionError(
                    "foreign_state_overlap",
                    "vNext runtime root overlaps a forbidden source or foreign-state root",
                )

        domains = (
            self.runtime_root / "control",
            self.runtime_root / "transport",
            self.runtime_root / "coordination",
            self.runtime_root / "config",
            self.runtime_root / "components",
        )
        if not all(_contained(path, self.runtime_root) and path != self.runtime_root for path in domains):
            raise VNextCompositionError("path_escape", "a vNext mutable domain escapes runtime_root")
        for index, left in enumerate(domains):
            for right in domains[index + 1 :]:
                if _overlaps(left, right):
                    raise VNextCompositionError("mutable_domain_overlap", "vNext mutable domains overlap")

    def to_dict(self) -> dict[str, str]:
        return {
            "runtime_root": str(self.runtime_root),
            "control_store": str(self.control_store),
            "spool_inbox": str(self.spool_inbox),
            "spool_results": str(self.spool_results),
            "receipts_store": str(self.receipts_store),
            "instance_lock": str(self.instance_lock),
            "pid_file": str(self.pid_file),
            "config": str(self.config),
            "browser_bundle_root": str(self.browser_bundle_root),
            "native_host_manifest": str(self.native_host_manifest),
        }


def _native_registry_keys(host_name: str) -> list[str]:
    suffix = f"NativeMessagingHosts\\{host_name}"
    return [
        f"HKCU\\Software\\Google\\Chrome\\{suffix}",
        f"HKCU\\Software\\Microsoft\\Edge\\{suffix}",
    ]


def _provider(
    provider_id: str,
    component_id: str,
    kind: str,
    state: str,
) -> dict[str, Any]:
    return {
        "provider_id": provider_id,
        "component_id": component_id,
        "kind": kind,
        "state": state,
        "writer_enabled": False,
    }


def _provider_registry() -> list[dict[str, Any]]:
    return [
        _provider(
            COMPOSITION_PROVIDER_ID,
            RUNTIME_ID,
            "diagnostic_composition_manifest",
            "active_read_only",
        ),
        _provider(CONTROL_PROVIDER_ID, RUNTIME_ID, "control_store", "reserved_disabled"),
        _provider(NATIVE_PROVIDER_ID, NATIVE_COMPONENT_ID, "native_transport", "active_read_only"),
        _provider(BROWSER_PROVIDER_ID, BROWSER_COMPONENT_ID, "browser_transport", "active_read_only"),
        _provider(
            CONTROL_CENTER_PROVIDER_ID,
            CONTROL_CENTER_COMPONENT_ID,
            "control_center_query",
            "reserved_disabled",
        ),
        _provider(REPO_VIEW_PROVIDER_ID, REPO_VIEW_COMPONENT_ID, "repo_view", "active_read_only"),
    ]


def _composition_edges() -> list[dict[str, str]]:
    return [
        {"from": BROWSER_PROVIDER_ID, "to": NATIVE_PROVIDER_ID},
        {"from": NATIVE_PROVIDER_ID, "to": CONTROL_PROVIDER_ID},
        {"from": CONTROL_CENTER_PROVIDER_ID, "to": CONTROL_PROVIDER_ID},
    ]


def build_vnext_composition_manifest(
    *,
    source_commit: str,
    runtime_root: str | Path | None = None,
    legacy_runtime_root: str | Path | None = None,
    forbidden_roots: Iterable[str | Path] = (),
    legacy_extension_ids: Iterable[str] = (),
    bundle_paths: Mapping[str, str | Path] | None = None,
) -> dict[str, Any]:
    if not isinstance(source_commit, str) or _SHA40.fullmatch(source_commit) is None:
        raise VNextCompositionError("invalid_source_commit", "source_commit must be lowercase SHA-40")
    browser_identity = load_browser_identity()
    browser_extension_id = browser_identity["extension_id"]
    if browser_extension_id in set(legacy_extension_ids):
        raise VNextCompositionError(
            "browser_identity_overlap",
            "vNext Browser extension ID must differ from every legacy extension ID",
        )

    runtime = runtime_root if runtime_root is not None else default_vnext_runtime_root()
    legacy = legacy_runtime_root if legacy_runtime_root is not None else default_legacy_runtime_root()
    layout = VNextLayout.create(runtime)
    layout.assert_isolated(legacy_runtime_root=legacy, forbidden_roots=forbidden_roots)
    legacy_root = _absolute_path(legacy, field="legacy_runtime_root")
    browser_origin = f"chrome-extension://{browser_extension_id}/"
    document: dict[str, Any] = {
        "schema": COMPOSITION_SCHEMA,
        "manifest_version": 1,
        "architecture_freeze": ARCHITECTURE_FREEZE,
        "execution_strategy": EXECUTION_STRATEGY,
        "basis": {"source_commit": source_commit, "source_branch": SOURCE_BRANCH},
        "generation": {
            "generation_id": GENERATION_ID,
            "runtime_id": RUNTIME_ID,
            "protocol_generation": PROTOCOL_GENERATION,
            "config_generation": CONFIG_GENERATION,
            "mode": "build_only",
            "writer_enabled": False,
        },
        "paths": layout.to_dict(),
        "identities": {
            "control_store": {
                "store_id": "devmaster.bdb.vnext.control-store",
                "schema": CONTROL_STORE_SCHEMA,
            },
            "browser_extension": {
                "component_id": BROWSER_COMPONENT_ID,
                "extension_id": browser_extension_id,
                "origin": browser_origin,
                "identity_state": "bound",
                "packaging_key_policy": "dedicated_vnext_public_key_pinned",
                "identity_resource": "browser_identity.json",
                "identity_digest": browser_identity["semantic_digest"],
                "public_key_sha256": browser_identity["public_key_sha256"],
                "private_key_in_repository": False,
                "registration_state": "uninstalled",
            },
            "native_host": {
                "component_id": NATIVE_COMPONENT_ID,
                "host_name": NATIVE_HOST_NAME,
                "manifest_schema": "bdb-vnext-native-host-manifest-v1",
                "registration_state": "unregistered",
                "windows_registry_keys": _native_registry_keys(NATIVE_HOST_NAME),
            },
            "protocol": {
                "component_id": "devmaster.bdb.vnext.protocol",
                "generation": PROTOCOL_GENERATION,
                "minimum_version": 1,
                "maximum_version": 1,
                "legacy_compatible": False,
            },
        },
        "composition": {
            "root_provider_id": COMPOSITION_PROVIDER_ID,
            "providers": _provider_registry(),
            "edges": _composition_edges(),
        },
        "bundles": _bundle_records(bundle_paths, legacy_runtime_root=legacy_root),
        "legacy_boundary": {
            "runtime_root": str(legacy_root),
            "native_host_name": LEGACY_NATIVE_HOST_NAME,
            "current_role": "frozen_active_tool",
            "vnext_access": "none",
            "cutover_role": "read_only_archive",
            "semantic_migration_required": False,
        },
        "activation": {
            "state": "disabled",
            "writer_enabled": False,
            "manifest_is_activation_authority": False,
            "blockers": [
                "explicit_activation_execution_unit_required",
            ],
        },
    }
    document["semantic_digest"] = semantic_digest(document)
    validate_vnext_composition_manifest(document)
    return document


def _mapping(value: object, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise VNextCompositionError("invalid_manifest", f"{field} must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], *, field: str) -> None:
    actual = set(value)
    if actual != expected:
        raise VNextCompositionError(
            "invalid_manifest_fields",
            f"{field} fields differ: missing={sorted(expected - actual)}, unknown={sorted(actual - expected)}",
        )


def validate_vnext_composition_manifest(document: Mapping[str, Any]) -> None:
    _exact_keys(
        document,
        {
            "schema",
            "manifest_version",
            "architecture_freeze",
            "execution_strategy",
            "basis",
            "generation",
            "paths",
            "identities",
            "composition",
            "bundles",
            "legacy_boundary",
            "activation",
            "semantic_digest",
        },
        field="manifest",
    )
    if document.get("schema") != COMPOSITION_SCHEMA or document.get("manifest_version") != 1:
        raise VNextCompositionError("unsupported_manifest", "vNext composition schema is unsupported")
    if document.get("architecture_freeze") != ARCHITECTURE_FREEZE:
        raise VNextCompositionError("architecture_mismatch", "Architecture Freeze identity differs")
    if document.get("execution_strategy") != EXECUTION_STRATEGY:
        raise VNextCompositionError("strategy_mismatch", "execution strategy differs")

    basis = _mapping(document.get("basis"), field="basis")
    _exact_keys(basis, {"source_commit", "source_branch"}, field="basis")
    commit = basis.get("source_commit")
    if not isinstance(commit, str) or _SHA40.fullmatch(commit) is None:
        raise VNextCompositionError("invalid_source_commit", "basis.source_commit is invalid")
    if basis.get("source_branch") != SOURCE_BRANCH:
        raise VNextCompositionError("branch_mismatch", "basis.source_branch must be bdb-vnext")

    generation = _mapping(document.get("generation"), field="generation")
    expected_generation = {
        "generation_id": GENERATION_ID,
        "runtime_id": RUNTIME_ID,
        "protocol_generation": PROTOCOL_GENERATION,
        "config_generation": CONFIG_GENERATION,
        "mode": "build_only",
        "writer_enabled": False,
    }
    if dict(generation) != expected_generation:
        raise VNextCompositionError("generation_mismatch", "vNext generation declaration differs")

    paths = _mapping(document.get("paths"), field="paths")
    root_value = paths.get("runtime_root")
    if not isinstance(root_value, str):
        raise VNextCompositionError("invalid_manifest", "paths.runtime_root must be a string")
    root = _absolute_path(root_value, field="paths.runtime_root")
    expected_paths = VNextLayout.create(root).to_dict()
    if dict(paths) != expected_paths:
        raise VNextCompositionError("layout_mismatch", "vNext runtime layout differs")

    legacy = _mapping(document.get("legacy_boundary"), field="legacy_boundary")
    legacy_root_value = legacy.get("runtime_root")
    if not isinstance(legacy_root_value, str):
        raise VNextCompositionError("invalid_manifest", "legacy_boundary.runtime_root must be a string")
    legacy_root = _absolute_path(legacy_root_value, field="legacy_boundary.runtime_root")
    VNextLayout.create(root).assert_isolated(legacy_runtime_root=legacy_root)
    expected_legacy = {
        "runtime_root": str(legacy_root),
        "native_host_name": LEGACY_NATIVE_HOST_NAME,
        "current_role": "frozen_active_tool",
        "vnext_access": "none",
        "cutover_role": "read_only_archive",
        "semantic_migration_required": False,
    }
    if dict(legacy) != expected_legacy:
        raise VNextCompositionError("legacy_boundary_mismatch", "legacy boundary differs")

    identities = _mapping(document.get("identities"), field="identities")
    _exact_keys(
        identities,
        {"control_store", "browser_extension", "native_host", "protocol"},
        field="identities",
    )
    control = _mapping(identities.get("control_store"), field="identities.control_store")
    expected_control = {
        "store_id": "devmaster.bdb.vnext.control-store",
        "schema": CONTROL_STORE_SCHEMA,
    }
    if dict(control) != expected_control:
        raise VNextCompositionError("control_store_identity_mismatch", "Control Store identity differs")
    browser = _mapping(identities.get("browser_extension"), field="identities.browser_extension")
    browser_identity = load_browser_identity()
    _exact_keys(
        browser,
        {
            "component_id",
            "extension_id",
            "origin",
            "identity_state",
            "packaging_key_policy",
            "identity_resource",
            "identity_digest",
            "public_key_sha256",
            "private_key_in_repository",
            "registration_state",
        },
        field="identities.browser_extension",
    )
    extension_id = browser.get("extension_id")
    if extension_id is not None and (
        not isinstance(extension_id, str) or _EXTENSION_ID.fullmatch(extension_id) is None
    ):
        raise VNextCompositionError("invalid_browser_extension_id", "Browser extension ID is invalid")
    expected_browser = {
        "component_id": BROWSER_COMPONENT_ID,
        "extension_id": browser_identity["extension_id"],
        "origin": f"chrome-extension://{browser_identity['extension_id']}/",
        "identity_state": "bound",
        "packaging_key_policy": "dedicated_vnext_public_key_pinned",
        "identity_resource": "browser_identity.json",
        "identity_digest": browser_identity["semantic_digest"],
        "public_key_sha256": browser_identity["public_key_sha256"],
        "private_key_in_repository": False,
        "registration_state": "uninstalled",
    }
    if dict(browser) != expected_browser:
        raise VNextCompositionError("browser_identity_mismatch", "Browser identity state differs")

    native = _mapping(identities.get("native_host"), field="identities.native_host")
    expected_native = {
        "component_id": NATIVE_COMPONENT_ID,
        "host_name": NATIVE_HOST_NAME,
        "manifest_schema": "bdb-vnext-native-host-manifest-v1",
        "registration_state": "unregistered",
        "windows_registry_keys": _native_registry_keys(NATIVE_HOST_NAME),
    }
    if dict(native) != expected_native:
        raise VNextCompositionError("native_identity_mismatch", "Native Host identity differs")
    protocol = _mapping(identities.get("protocol"), field="identities.protocol")
    expected_protocol = {
        "component_id": "devmaster.bdb.vnext.protocol",
        "generation": PROTOCOL_GENERATION,
        "minimum_version": 1,
        "maximum_version": 1,
        "legacy_compatible": False,
    }
    if dict(protocol) != expected_protocol:
        raise VNextCompositionError("protocol_generation_mismatch", "protocol generation differs")

    composition = _mapping(document.get("composition"), field="composition")
    _exact_keys(composition, {"root_provider_id", "providers", "edges"}, field="composition")
    providers = composition.get("providers")
    if not isinstance(providers, list) or providers != _provider_registry():
        raise VNextCompositionError("provider_registry_missing", "provider registry is missing")
    provider_ids = [item.get("provider_id") for item in providers if isinstance(item, Mapping)]
    if len(provider_ids) != len(providers) or len(provider_ids) != len(set(provider_ids)):
        raise VNextCompositionError("duplicate_provider_id", "provider IDs must be unique")
    if (
        composition.get("root_provider_id") != COMPOSITION_PROVIDER_ID
        or COMPOSITION_PROVIDER_ID not in provider_ids
    ):
        raise VNextCompositionError("composition_root_missing", "composition root provider is missing")
    if any(item.get("writer_enabled") is not False for item in providers):
        raise VNextCompositionError("writer_enabled", "M1a-vNext providers must remain read-only/disabled")
    edges = composition.get("edges")
    if not isinstance(edges, list) or edges != _composition_edges():
        raise VNextCompositionError("composition_edge_invalid", "composition edge is invalid")

    bundles = document.get("bundles")
    if not isinstance(bundles, list) or any(not isinstance(bundle, Mapping) for bundle in bundles):
        raise VNextCompositionError("bundle_identity_invalid", "bundle identity is invalid")
    bundle_ids = [bundle.get("component_id") for bundle in bundles]
    if tuple(bundle_ids) != _BUNDLE_COMPONENT_IDS:
        raise VNextCompositionError("bundle_identity_invalid", "bundle component IDs differ")
    observed_paths: list[Path] = []
    for bundle in bundles:
        _exact_keys(
            bundle,
            {
                "component_id",
                "state",
                "kind",
                "path",
                "file_count",
                "size_bytes",
                "sha256",
            },
            field="bundles[]",
        )
        state = bundle.get("state")
        digest_value = bundle.get("sha256")
        if state == "not_built":
            if dict(bundle) != _not_built_bundle(str(bundle["component_id"])):
                raise VNextCompositionError("bundle_identity_invalid", "not-built bundle differs")
            continue
        if state != "observed":
            raise VNextCompositionError("bundle_identity_invalid", "bundle state is unsupported")
        raw_path = bundle.get("path")
        file_count = bundle.get("file_count")
        size_bytes = bundle.get("size_bytes")
        if (
            bundle.get("kind") not in {"file", "directory"}
            or not isinstance(raw_path, str)
            or not isinstance(file_count, int)
            or isinstance(file_count, bool)
            or not 1 <= file_count <= _MAX_BUNDLE_FILES
            or not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or not 0 <= size_bytes <= _MAX_BUNDLE_TOTAL_BYTES
            or not isinstance(digest_value, str)
            or _SHA256.fullmatch(digest_value) is None
        ):
            raise VNextCompositionError("bundle_identity_invalid", "observed bundle digest is invalid")
        path_value = _absolute_path(raw_path, field="bundles[].path")
        if _overlaps(path_value, legacy_root):
            raise VNextCompositionError("legacy_bundle_overlap", "bundle overlaps legacy runtime")
        if bundle.get("component_id") == BROWSER_COMPONENT_ID and bundle.get("kind") != "directory":
            raise VNextCompositionError("invalid_browser_bundle", "Browser bundle must be a directory")
        observed_paths.append(path_value)
    for index, left in enumerate(observed_paths):
        for right in observed_paths[index + 1 :]:
            if _overlaps(left, right):
                raise VNextCompositionError("bundle_path_overlap", "observed vNext bundle paths overlap")

    activation = _mapping(document.get("activation"), field="activation")
    expected_blockers = ["explicit_activation_execution_unit_required"]
    expected_activation = {
        "state": "disabled",
        "writer_enabled": False,
        "manifest_is_activation_authority": False,
        "blockers": expected_blockers,
    }
    if dict(activation) != expected_activation:
        raise VNextCompositionError("activation_enabled", "M1a manifest cannot activate vNext")

    digest = document.get("semantic_digest")
    if not isinstance(digest, str) or digest != semantic_digest(document):
        raise VNextCompositionError("digest_mismatch", "composition semantic digest differs")


def load_vnext_composition_manifest(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser().absolute()
    if source.is_symlink():
        raise VNextCompositionError("invalid_manifest_path", "manifest must be a regular file")
    before = source.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise VNextCompositionError("invalid_manifest_path", "manifest must be a regular file")
    if before.st_size > _MAX_MANIFEST_BYTES:
        raise VNextCompositionError("manifest_too_large", "manifest exceeds the bounded read limit")
    with source.open("rb") as handle:
        payload = handle.read(_MAX_MANIFEST_BYTES + 1)
    after = source.stat(follow_symlinks=False)
    before_token = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_token = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if before_token != after_token:
        raise VNextCompositionError("manifest_unstable", "manifest changed during observation")
    if len(payload) > _MAX_MANIFEST_BYTES:
        raise VNextCompositionError("manifest_too_large", "manifest exceeds the bounded read limit")
    value = json.loads(payload.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise VNextCompositionError("invalid_manifest", "manifest root must be an object")
    validate_vnext_composition_manifest(value)
    return value


def composition_status(
    expected: Mapping[str, Any],
    observed: Mapping[str, Any] | None,
) -> dict[str, Any]:
    validate_vnext_composition_manifest(expected)
    blockers: list[dict[str, str]] = []
    observed_digest: str | None = None
    if observed is None:
        blockers.append({"code": "manifest_missing", "field": "observed"})
    else:
        observed_digest_value = observed.get("semantic_digest")
        observed_digest = observed_digest_value if isinstance(observed_digest_value, str) else None
        try:
            validate_vnext_composition_manifest(observed)
        except VNextCompositionError as exc:
            blockers.append({"code": exc.code, "field": "observed"})
        else:
            if observed_digest != expected.get("semantic_digest"):
                blockers.append({"code": "composition_digest_mismatch", "field": "semantic_digest"})

    return {
        "schema": STATUS_SCHEMA,
        "result": "MATCH" if not blockers else "MISMATCH",
        "compatible": not blockers,
        "activation_ready": False,
        "expected_digest": expected.get("semantic_digest"),
        "observed_digest": observed_digest,
        "blockers": blockers,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bdb-vnext-manifest",
        description="Build or compare the read-only M1a vNext composition manifest",
    )
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--legacy-runtime-root", type=Path)
    parser.add_argument("--forbid-root", action="append", type=Path, default=[])
    parser.add_argument("--legacy-extension-id", action="append", default=[])
    parser.add_argument(
        "--bundle",
        action="append",
        default=[],
        metavar="COMPONENT_ID=PATH",
        help="Observe one explicit vNext bundle without writing to it",
    )
    parser.add_argument("--observed-manifest", type=Path)
    parser.add_argument("--sanitized", action="store_true")
    return parser


def _parse_bundle_bindings(values: Sequence[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        component_id, separator, raw_path = value.partition("=")
        if not separator or not raw_path or component_id not in _BUNDLE_COMPONENT_IDS:
            raise VNextCompositionError(
                "invalid_bundle_binding",
                "--bundle must use a supported COMPONENT_ID=PATH binding",
            )
        if component_id in result:
            raise VNextCompositionError("duplicate_bundle_binding", "bundle component was supplied twice")
        result[component_id] = Path(raw_path)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest = build_vnext_composition_manifest(
            source_commit=args.source_commit,
            runtime_root=args.runtime_root,
            legacy_runtime_root=args.legacy_runtime_root,
            forbidden_roots=args.forbid_root,
            legacy_extension_ids=args.legacy_extension_id,
            bundle_paths=_parse_bundle_bindings(args.bundle),
        )
        if args.observed_manifest is None:
            result: Mapping[str, Any] = manifest
            exit_code = 0
        else:
            observed = load_vnext_composition_manifest(args.observed_manifest)
            result = composition_status(manifest, observed)
            exit_code = 0 if result["compatible"] is True else 2
        if args.sanitized:
            result = sanitize_report(result)
        sys.stdout.buffer.write(canonical_json_bytes(result))
        return exit_code
    except (OSError, UnicodeError, json.JSONDecodeError, VNextCompositionError) as exc:
        code = exc.code if isinstance(exc, VNextCompositionError) else "manifest_unavailable"
        sys.stderr.write(f"bdb-vnext-manifest failed: {code}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

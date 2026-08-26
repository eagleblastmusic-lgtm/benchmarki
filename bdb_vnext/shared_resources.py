"""Bounded, exact-keyed shared-resource policy for NX-037.

The store in this module is deliberately narrower than a package manager.  It
publishes verified immutable artifacts and append-only cache entries below one
configured root.  Project outputs (node_modules, Python virtual environments,
and Cargo target directories) are denied by policy and therefore cannot be
turned into shared state merely by choosing a matching path.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from bdb_shared.evidence import canonical_json_bytes


SHARED_RESOURCE_POLICY_SCHEMA = "bdb-vnext-shared-resource-policy-v1"
SHARED_RESOURCE_POLICY_VERSION = "v1"
SHARED_LAYOUT_SCHEMA = "bdb-vnext-shared-layout-v1"
SHARED_LAYOUT_VERSION = "v1"
SHARED_RESOURCE_RESULT_SCHEMA = "bdb-vnext-shared-resource-result-v1"
SHARED_RESOURCE_RESULT_VERSION = "v1"
SHARED_RESOURCE_POLICY_VERSION_EXPLICIT = True
SHARED_LAYOUT_VERSION_EXPLICIT = True
SHARED_RESOURCE_RESULT_VERSION_EXPLICIT = True
CANONICAL_SHARED_ROOT = r"C:\Projekty\_Shared"
MAX_SHARED_ENTRY_BYTES = 16 * 1024 * 1024

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTITY = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]*$")
_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_ABSOLUTE_WINDOWS = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


class _StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class MutabilityClass(_StringEnum):
    SEALED_IMMUTABLE = "SEALED_IMMUTABLE"
    APPEND_ONLY_CACHE = "APPEND_ONLY_CACHE"
    MUTABLE_DISALLOWED = "MUTABLE_DISALLOWED"


class SharedResourceStatus(_StringEnum):
    PUBLISHED = "PUBLISHED"
    REBUILT = "REBUILT"
    EXACT_HIT = "EXACT_HIT"
    MISSING = "MISSING"
    POISONED = "POISONED"
    BLOCKED = "BLOCKED"
    ACL_DENIED = "ACL_DENIED"
    QUOTA_BLOCKED = "QUOTA_BLOCKED"
    LOCK_BUSY = "LOCK_BUSY"
    INVALID = "INVALID"


class SharedResourceError(ValueError):
    """Stable fail-closed error for shared-root paths and contracts."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SharedPublicationInterrupted(SharedResourceError):
    """Deterministic publication crash boundary used by qualification."""


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SharedResourceError("text_required", f"{field_name} must be non-empty text")
    return value


def _identity(value: Any, field_name: str) -> str:
    text = _text(value, field_name)
    if _IDENTITY.fullmatch(text) is None:
        raise SharedResourceError("identity_invalid", f"{field_name} has an invalid identity")
    return text


def _digest(value: Any, field_name: str) -> str:
    text = _text(value, field_name)
    if _DIGEST.fullmatch(text) is None:
        raise SharedResourceError("digest_invalid", f"{field_name} must be a sha256 digest")
    return text


def _bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise SharedResourceError("boolean_required", f"{field_name} must be boolean")
    return value


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _strict_mapping(value: Any, fields: set[str], field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SharedResourceError("mapping_required", f"{field_name} must be an object")
    unknown = sorted(str(key) for key in value if str(key) not in fields)
    if unknown:
        raise SharedResourceError("unknown_field", f"{field_name} has unknown fields: {', '.join(unknown)}")
    return value


def _relative(value: Any, field_name: str) -> str:
    text = _text(value, field_name).replace("\\", "/")
    if text.startswith("/") or text.startswith("//") or re.match(r"^[A-Za-z]:", text):
        raise SharedResourceError("path_escape", f"{field_name} must be relative")
    parts = text.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise SharedResourceError("path_escape", f"{field_name} contains traversal or empty components")
    return "/".join(parts)


def _is_reparse(info: os.stat_result) -> bool:
    return bool(int(getattr(info, "st_file_attributes", 0)) & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)))


def _assert_no_reparse_components(path: Path, *, field_name: str) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor) if absolute.anchor else Path()
    parts = absolute.parts[1:] if absolute.anchor else absolute.parts
    for part in parts:
        current /= part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
            raise SharedResourceError("reparse_point", f"{field_name} contains a symlink or reparse point")


def _ensure_directory(path: Path, *, field_name: str) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor) if absolute.anchor else Path()
    parts = absolute.parts[1:] if absolute.anchor else absolute.parts
    for part in parts:
        current /= part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            try:
                current.mkdir()
            except FileExistsError:
                pass
            info = os.lstat(current)
        if stat.S_ISLNK(info.st_mode) or _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
            raise SharedResourceError("invalid_shared_path", f"{field_name} contains a non-directory component")


def _root(value: str | Path) -> Path:
    root = Path(value).expanduser().absolute()
    _assert_no_reparse_components(root, field_name="shared root")
    _ensure_directory(root, field_name="shared root")
    _assert_no_reparse_components(root, field_name="shared root")
    return root


def _safe_child(root: Path, relative: str, *, field_name: str) -> Path:
    normalised = _relative(relative, field_name)
    target = root.joinpath(*normalised.split("/"))
    _assert_no_reparse_components(target, field_name=field_name)
    try:
        child_path = os.path.normcase(os.path.abspath(os.fspath(target)))
        root_path = os.path.normcase(os.path.abspath(os.fspath(root)))
        if os.path.commonpath((child_path, root_path)) != root_path:
            raise ValueError("different common path")
    except (OSError, ValueError) as exc:
        raise SharedResourceError("path_escape", f"{field_name} escapes the shared root") from exc
    return target


def _read_regular(path: Path, *, field_name: str) -> bytes:
    _assert_no_reparse_components(path, field_name=field_name)
    try:
        info = os.lstat(path)
    except FileNotFoundError as exc:
        raise SharedResourceError("missing_entry", f"{field_name} is missing") from exc
    if stat.S_ISLNK(info.st_mode) or _is_reparse(info) or not stat.S_ISREG(info.st_mode):
        raise SharedResourceError("invalid_entry", f"{field_name} is not a regular file")
    if info.st_size > MAX_SHARED_ENTRY_BYTES:
        raise SharedResourceError("entry_too_large", f"{field_name} exceeds the bounded size")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise SharedResourceError("entry_read_failed", f"{field_name} could not be read") from exc
    if len(payload) > MAX_SHARED_ENTRY_BYTES:
        raise SharedResourceError("entry_too_large", f"{field_name} exceeds the bounded size")
    return payload


def _write_fsync(path: Path, payload: bytes) -> None:
    if len(payload) > MAX_SHARED_ENTRY_BYTES:
        raise SharedResourceError("entry_too_large", "shared entry exceeds the bounded size")
    _assert_no_reparse_components(path.parent, field_name="shared temporary parent")
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise SharedResourceError("shared_write_failed", "shared entry temporary write failed") from exc


@dataclass(frozen=True)
class SharedLayoutContract:
    root: str = CANONICAL_SHARED_ROOT
    areas: tuple[tuple[str, str], ...] = (
        ("Cache", "append-only package/download caches"),
        ("Environment", "exact-keyed immutable environment resources"),
        ("PackageStores", "exact-keyed package stores"),
        ("Toolchains", "sealed exact-version toolchains"),
    )
    schema: str = SHARED_LAYOUT_SCHEMA
    version: str = SHARED_LAYOUT_VERSION
    layout_digest: str = field(default="", compare=True)

    def __post_init__(self) -> None:
        if self.schema != SHARED_LAYOUT_SCHEMA or self.version != SHARED_LAYOUT_VERSION:
            raise SharedResourceError("layout_version_invalid", "unsupported shared layout schema/version")
        _text(self.root, "layout.root")
        values = tuple(sorted(((_identity(name, "layout.area.name"), _text(description, "layout.area.description")) for name, description in self.areas)))
        if len({name for name, _ in values}) != len(values):
            raise SharedResourceError("layout_duplicate_area", "shared layout areas must be unique")
        object.__setattr__(self, "areas", values)
        computed = _sha256(canonical_json_bytes(self._payload()))
        if self.layout_digest and self.layout_digest != computed:
            raise SharedResourceError("layout_digest_mismatch", "shared layout digest does not match content")
        object.__setattr__(self, "layout_digest", computed)

    def _payload(self) -> dict[str, Any]:
        return {"schema": self.schema, "version": self.version, "root": self.root, "areas": {name: description for name, description in self.areas}}

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["layout_digest"] = self.layout_digest
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SharedLayoutContract":
        data = _strict_mapping(value, {"schema", "version", "root", "areas", "layout_digest"}, "shared_layout")
        areas = data.get("areas")
        if not isinstance(areas, Mapping):
            raise SharedResourceError("layout_shape_invalid", "shared layout areas must be an object")
        return cls(
            root=_text(data.get("root"), "layout.root"),
            areas=tuple((_text(name, "layout.area.name"), _text(description, "layout.area.description")) for name, description in areas.items()),
            schema=_text(data.get("schema"), "layout.schema"),
            version=_text(data.get("version"), "layout.version"),
            layout_digest=_digest(data.get("layout_digest"), "layout.layout_digest"),
        )


@dataclass(frozen=True)
class SharingRule:
    ecosystem: str
    artifact_class: str
    area: str
    mutability: MutabilityClass
    shared_allowed: bool
    project_local_required: bool

    def __post_init__(self) -> None:
        _identity(self.ecosystem, "rule.ecosystem")
        _identity(self.artifact_class, "rule.artifact_class")
        _identity(self.area, "rule.area")
        object.__setattr__(self, "mutability", MutabilityClass(self.mutability))
        _bool(self.shared_allowed, "rule.shared_allowed")
        _bool(self.project_local_required, "rule.project_local_required")
        if self.project_local_required and self.shared_allowed:
            raise SharedResourceError("sharing_rule_conflict", "project-local output cannot be shared")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ecosystem": self.ecosystem,
            "artifact_class": self.artifact_class,
            "area": self.area,
            "mutability": self.mutability.value,
            "shared_allowed": self.shared_allowed,
            "project_local_required": self.project_local_required,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SharingRule":
        data = _strict_mapping(value, {"ecosystem", "artifact_class", "area", "mutability", "shared_allowed", "project_local_required"}, "sharing_rule")
        return cls(
            ecosystem=_identity(data.get("ecosystem"), "rule.ecosystem"),
            artifact_class=_identity(data.get("artifact_class"), "rule.artifact_class"),
            area=_identity(data.get("area"), "rule.area"),
            mutability=MutabilityClass(data.get("mutability")),
            shared_allowed=_bool(data.get("shared_allowed"), "rule.shared_allowed"),
            project_local_required=_bool(data.get("project_local_required"), "rule.project_local_required"),
        )


@dataclass(frozen=True)
class SharedResourcePolicy:
    rules: tuple[SharingRule, ...]
    schema: str = SHARED_RESOURCE_POLICY_SCHEMA
    version: str = SHARED_RESOURCE_POLICY_VERSION
    policy_digest: str = field(default="", compare=True)

    def __post_init__(self) -> None:
        if self.schema != SHARED_RESOURCE_POLICY_SCHEMA or self.version != SHARED_RESOURCE_POLICY_VERSION:
            raise SharedResourceError("policy_version_invalid", "unsupported shared policy schema/version")
        if any(not isinstance(item, SharingRule) for item in self.rules):
            raise SharedResourceError("policy_rule_invalid", "shared policy rules must be typed")
        rules = tuple(sorted(self.rules, key=lambda item: (item.ecosystem, item.artifact_class)))
        if len({(item.ecosystem, item.artifact_class) for item in rules}) != len(rules):
            raise SharedResourceError("policy_rule_duplicate", "shared policy rules must be unique")
        object.__setattr__(self, "rules", rules)
        computed = _sha256(canonical_json_bytes(self._payload()))
        if self.policy_digest and self.policy_digest != computed:
            raise SharedResourceError("policy_digest_mismatch", "shared policy digest does not match content")
        object.__setattr__(self, "policy_digest", computed)

    def _payload(self) -> dict[str, Any]:
        return {"schema": self.schema, "version": self.version, "rules": [item.to_dict() for item in self.rules]}

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["policy_digest"] = self.policy_digest
        return payload

    def rule_for(self, ecosystem: str, artifact_class: str) -> SharingRule:
        key = (_identity(ecosystem, "resource.ecosystem"), _identity(artifact_class, "resource.artifact_class"))
        for rule in self.rules:
            if (rule.ecosystem, rule.artifact_class) == key:
                return rule
        raise SharedResourceError("policy_denied", "resource type is not present in the explicit sharing matrix")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SharedResourcePolicy":
        data = _strict_mapping(value, {"schema", "version", "rules", "policy_digest"}, "shared_policy")
        rules = data.get("rules")
        if not isinstance(rules, list) or any(not isinstance(item, Mapping) for item in rules):
            raise SharedResourceError("policy_shape_invalid", "shared policy rules must be objects")
        return cls(
            rules=tuple(SharingRule.from_dict(item) for item in rules),
            schema=_text(data.get("schema"), "policy.schema"),
            version=_text(data.get("version"), "policy.version"),
            policy_digest=_digest(data.get("policy_digest"), "policy.policy_digest"),
        )


DEFAULT_SHARED_RESOURCE_POLICY = SharedResourcePolicy(
    rules=(
        SharingRule("npm", "download_cache", "Cache", MutabilityClass.APPEND_ONLY_CACHE, True, False),
        SharingRule("pnpm", "download_cache", "Cache", MutabilityClass.APPEND_ONLY_CACHE, True, False),
        SharingRule("python", "wheel_cache", "Cache", MutabilityClass.APPEND_ONLY_CACHE, True, False),
        SharingRule("python", "venv", "Environment", MutabilityClass.MUTABLE_DISALLOWED, False, True),
        SharingRule("node", "node_modules", "Environment", MutabilityClass.MUTABLE_DISALLOWED, False, True),
        SharingRule("cargo", "registry_cache", "Cache", MutabilityClass.APPEND_ONLY_CACHE, True, False),
        SharingRule("cargo", "git_cache", "Cache", MutabilityClass.APPEND_ONLY_CACHE, True, False),
        SharingRule("cargo", "target", "Environment", MutabilityClass.MUTABLE_DISALLOWED, False, True),
        SharingRule("toolchain", "sealed_toolchain", "Toolchains", MutabilityClass.SEALED_IMMUTABLE, True, False),
    )
)


@dataclass(frozen=True)
class SharedResourceKey:
    ecosystem: str
    artifact_class: str
    platform: str
    tool_version: str
    content_digest: str
    policy_version: str = SHARED_RESOURCE_POLICY_VERSION
    key_digest: str = field(default="", compare=True)

    def __post_init__(self) -> None:
        _identity(self.ecosystem, "key.ecosystem")
        _identity(self.artifact_class, "key.artifact_class")
        _identity(self.platform, "key.platform")
        _text(self.tool_version, "key.tool_version")
        _digest(self.content_digest, "key.content_digest")
        if self.policy_version != SHARED_RESOURCE_POLICY_VERSION:
            raise SharedResourceError("key_policy_version_invalid", "shared key policy version is unsupported")
        computed = _sha256(canonical_json_bytes(self._payload()))
        if self.key_digest and self.key_digest != computed:
            raise SharedResourceError("key_digest_mismatch", "shared resource key digest does not match content")
        object.__setattr__(self, "key_digest", computed)

    def _payload(self) -> dict[str, str]:
        return {
            "ecosystem": self.ecosystem,
            "artifact_class": self.artifact_class,
            "platform": self.platform,
            "tool_version": self.tool_version,
            "content_digest": self.content_digest,
            "policy_version": self.policy_version,
        }

    def to_dict(self) -> dict[str, str]:
        payload = self._payload()
        payload["key_digest"] = self.key_digest
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SharedResourceKey":
        data = _strict_mapping(value, {"ecosystem", "artifact_class", "platform", "tool_version", "content_digest", "policy_version", "key_digest"}, "resource_key")
        return cls(
            ecosystem=_identity(data.get("ecosystem"), "key.ecosystem"),
            artifact_class=_identity(data.get("artifact_class"), "key.artifact_class"),
            platform=_identity(data.get("platform"), "key.platform"),
            tool_version=_text(data.get("tool_version"), "key.tool_version"),
            content_digest=_digest(data.get("content_digest"), "key.content_digest"),
            policy_version=_text(data.get("policy_version"), "key.policy_version"),
            key_digest=_digest(data.get("key_digest"), "key.key_digest"),
        )


@dataclass(frozen=True)
class SharedResourcePaths:
    relative_directory: str
    directory: Path
    payload: Path
    manifest: Path
    lock: Path


@dataclass(frozen=True)
class SharedResourceManifest:
    key: SharedResourceKey
    area: str
    mutability: MutabilityClass
    payload_digest: str
    size_bytes: int
    schema: str = SHARED_RESOURCE_POLICY_SCHEMA
    version: str = SHARED_RESOURCE_POLICY_VERSION
    manifest_digest: str = field(default="", compare=True)

    def __post_init__(self) -> None:
        if self.schema != SHARED_RESOURCE_POLICY_SCHEMA or self.version != SHARED_RESOURCE_POLICY_VERSION:
            raise SharedResourceError("manifest_version_invalid", "unsupported shared manifest schema/version")
        if not isinstance(self.key, SharedResourceKey):
            raise SharedResourceError("manifest_key_invalid", "shared manifest key must be typed")
        _identity(self.area, "manifest.area")
        object.__setattr__(self, "mutability", MutabilityClass(self.mutability))
        _digest(self.payload_digest, "manifest.payload_digest")
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int) or self.size_bytes < 0:
            raise SharedResourceError("manifest_size_invalid", "shared manifest size must be non-negative")
        computed = _sha256(canonical_json_bytes(self._payload()))
        if self.manifest_digest and self.manifest_digest != computed:
            raise SharedResourceError("manifest_digest_mismatch", "shared manifest digest does not match content")
        object.__setattr__(self, "manifest_digest", computed)

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "key": self.key.to_dict(),
            "area": self.area,
            "mutability": self.mutability.value,
            "payload_digest": self.payload_digest,
            "size_bytes": self.size_bytes,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["manifest_digest"] = self.manifest_digest
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SharedResourceManifest":
        data = _strict_mapping(value, {"schema", "version", "key", "area", "mutability", "payload_digest", "size_bytes", "manifest_digest"}, "shared_manifest")
        if not isinstance(data.get("key"), Mapping):
            raise SharedResourceError("manifest_shape_invalid", "shared manifest key must be an object")
        return cls(
            key=SharedResourceKey.from_dict(data["key"]),
            area=_identity(data.get("area"), "manifest.area"),
            mutability=MutabilityClass(data.get("mutability")),
            payload_digest=_digest(data.get("payload_digest"), "manifest.payload_digest"),
            size_bytes=data.get("size_bytes"),
            schema=_text(data.get("schema"), "manifest.schema"),
            version=_text(data.get("version"), "manifest.version"),
            manifest_digest=_digest(data.get("manifest_digest"), "manifest.manifest_digest"),
        )


@dataclass(frozen=True)
class SharedResourceResult:
    status: SharedResourceStatus
    key_digest: str
    accepted: bool
    exact_hit: bool
    bytes_written: int
    manifest_digest: str | None
    resource_relative_path: str | None
    reason: str
    schema: str = SHARED_RESOURCE_RESULT_SCHEMA
    version: str = SHARED_RESOURCE_RESULT_VERSION
    result_digest: str = field(default="", compare=True)

    def __post_init__(self) -> None:
        if self.schema != SHARED_RESOURCE_RESULT_SCHEMA or self.version != SHARED_RESOURCE_RESULT_VERSION:
            raise SharedResourceError("result_version_invalid", "unsupported shared result schema/version")
        object.__setattr__(self, "status", SharedResourceStatus(self.status))
        _digest(self.key_digest, "result.key_digest")
        _bool(self.accepted, "result.accepted")
        _bool(self.exact_hit, "result.exact_hit")
        if isinstance(self.bytes_written, bool) or not isinstance(self.bytes_written, int) or self.bytes_written < 0:
            raise SharedResourceError("result_size_invalid", "result bytes_written must be non-negative")
        if self.manifest_digest is not None:
            _digest(self.manifest_digest, "result.manifest_digest")
        if self.resource_relative_path is not None:
            object.__setattr__(self, "resource_relative_path", _relative(self.resource_relative_path, "result.resource_relative_path"))
        _text(self.reason, "result.reason")
        computed = _sha256(canonical_json_bytes(self._payload()))
        if self.result_digest and self.result_digest != computed:
            raise SharedResourceError("result_digest_mismatch", "shared result digest does not match content")
        object.__setattr__(self, "result_digest", computed)

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "status": self.status.value,
            "key_digest": self.key_digest,
            "accepted": self.accepted,
            "exact_hit": self.exact_hit,
            "bytes_written": self.bytes_written,
            "manifest_digest": self.manifest_digest,
            "resource_relative_path": self.resource_relative_path,
            "reason": self.reason,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["result_digest"] = self.result_digest
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SharedResourceResult":
        data = _strict_mapping(
            value,
            {
                "schema",
                "version",
                "status",
                "key_digest",
                "accepted",
                "exact_hit",
                "bytes_written",
                "manifest_digest",
                "resource_relative_path",
                "reason",
                "result_digest",
            },
            "shared_result",
        )
        return cls(
            schema=_text(data.get("schema"), "result.schema"),
            version=_text(data.get("version"), "result.version"),
            status=SharedResourceStatus(data.get("status")),
            key_digest=_digest(data.get("key_digest"), "result.key_digest"),
            accepted=_bool(data.get("accepted"), "result.accepted"),
            exact_hit=_bool(data.get("exact_hit"), "result.exact_hit"),
            bytes_written=data.get("bytes_written"),
            manifest_digest=None if data.get("manifest_digest") is None else _digest(data.get("manifest_digest"), "result.manifest_digest"),
            resource_relative_path=None if data.get("resource_relative_path") is None else _text(data.get("resource_relative_path"), "result.resource_relative_path"),
            reason=_text(data.get("reason"), "result.reason"),
            result_digest=_digest(data.get("result_digest"), "result.result_digest"),
        )


@dataclass(frozen=True)
class SharedLookup:
    status: SharedResourceStatus
    hit: bool
    valid: bool
    key: SharedResourceKey
    manifest: SharedResourceManifest | None
    payload: bytes | None
    reason: str
    readiness_authority: bool = False


@dataclass(frozen=True)
class QuotaPolicy:
    limits: tuple[tuple[str, int], ...] = (
        ("Cache", 1024 * 1024),
        ("Environment", 1024 * 1024),
        ("PackageStores", 1024 * 1024),
        ("Toolchains", 1024 * 1024),
    )

    def __post_init__(self) -> None:
        values = tuple(sorted((_identity(area, "quota.area"), limit) for area, limit in self.limits))
        if any(isinstance(limit, bool) or not isinstance(limit, int) or limit < 0 for _, limit in values):
            raise SharedResourceError("quota_invalid", "quota limits must be non-negative integers")
        if len({area for area, _ in values}) != len(values):
            raise SharedResourceError("quota_duplicate", "quota areas must be unique")
        object.__setattr__(self, "limits", values)

    def limit_for(self, area: str) -> int:
        for name, limit in self.limits:
            if name == area:
                return limit
        return 0


class OwnedSharedLock:
    """Ownership-safe lock file; release only compares the owner token."""

    def __init__(self, path: Path, *, owner_token: str) -> None:
        self.path = path
        token = _text(owner_token, "lock.owner_token")
        if _TOKEN.fullmatch(token) is None:
            raise SharedResourceError("lock_owner_invalid", "lock owner token has an invalid form")
        self.owner_token = token
        self._held = False

    def acquire(self, *, timeout_seconds: float = 2.0) -> bool:
        if timeout_seconds < 0:
            raise SharedResourceError("lock_timeout_invalid", "lock timeout must be non-negative")
        _ensure_directory(self.path.parent, field_name="shared lock parent")
        deadline = __import__("time").monotonic() + timeout_seconds
        payload = canonical_json_bytes({"schema": "bdb-vnext-owned-shared-lock-v1", "owner_token": self.owner_token})
        while True:
            try:
                with self.path.open("xb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._held = True
                return True
            except FileExistsError:
                if __import__("time").monotonic() >= deadline:
                    return False
                __import__("time").sleep(0.005)
            except OSError as exc:
                raise SharedResourceError("lock_acquire_failed", "shared lock acquisition failed") from exc

    def release(self) -> bool:
        if not self.path.exists():
            self._held = False
            return False
        try:
            observed = json.loads(_read_regular(self.path, field_name="shared lock").decode("utf-8"))
        except (SharedResourceError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            return False
        if not isinstance(observed, Mapping) or observed.get("owner_token") != self.owner_token:
            return False
        try:
            self.path.unlink()
        except OSError as exc:
            raise SharedResourceError("lock_release_failed", "owned shared lock could not be released") from exc
        self._held = False
        return True


class SharedResourceStore:
    """Exact-keyed shared artifact store with fail-closed security boundaries."""

    def __init__(
        self,
        root: str | Path,
        *,
        policy: SharedResourcePolicy = DEFAULT_SHARED_RESOURCE_POLICY,
        quota: QuotaPolicy = QuotaPolicy(),
    ) -> None:
        self.root = _root(root)
        self.policy = policy
        self.quota = quota
        self.layout = SharedLayoutContract(root=str(self.root))
        self.migration_events: list[dict[str, str]] = []
        self.automatic_elevation_attempts = 0
        self._active: dict[str, str] = {}
        for area, _ in self.layout.areas:
            _ensure_directory(self.root / area, field_name=f"shared layout {area}")

    def safe_path(self, relative_path: str) -> Path:
        return _safe_child(self.root, relative_path, field_name="shared resource path")

    def rule_for(self, key: SharedResourceKey) -> SharingRule:
        if not isinstance(key, SharedResourceKey):
            raise SharedResourceError("key_invalid", "shared store requires SharedResourceKey")
        return self.policy.rule_for(key.ecosystem, key.artifact_class)

    def paths_for(self, key: SharedResourceKey) -> SharedResourcePaths:
        rule = self.rule_for(key)
        relative = f"{rule.area}/{key.ecosystem}/{key.artifact_class}/{key.key_digest[7:]}"
        directory = _safe_child(self.root, relative, field_name="shared resource directory")
        return SharedResourcePaths(relative, directory, directory / "payload.bin", directory / "manifest.json", directory / ".lock")

    def _result(
        self,
        key: SharedResourceKey,
        status: SharedResourceStatus,
        *,
        accepted: bool = False,
        exact_hit: bool = False,
        bytes_written: int = 0,
        manifest_digest: str | None = None,
        paths: SharedResourcePaths | None = None,
        reason: str,
    ) -> SharedResourceResult:
        return SharedResourceResult(
            status=status,
            key_digest=key.key_digest,
            accepted=accepted,
            exact_hit=exact_hit,
            bytes_written=bytes_written,
            manifest_digest=manifest_digest,
            resource_relative_path=None if paths is None else f"{paths.relative_directory}/payload.bin",
            reason=reason,
        )

    def lookup(self, key: SharedResourceKey) -> SharedLookup:
        try:
            paths = self.paths_for(key)
        except SharedResourceError as exc:
            return SharedLookup(SharedResourceStatus.BLOCKED, False, False, key, None, None, exc.code)
        if not paths.manifest.exists() or not paths.payload.exists():
            return SharedLookup(SharedResourceStatus.MISSING, False, False, key, None, None, "MANIFEST_OR_PAYLOAD_MISSING")
        try:
            document = json.loads(_read_regular(paths.manifest, field_name="shared manifest").decode("utf-8"))
            if not isinstance(document, Mapping):
                raise SharedResourceError("manifest_invalid", "shared manifest is not an object")
            manifest = SharedResourceManifest.from_dict(document)
            payload = _read_regular(paths.payload, field_name="shared payload")
            if manifest.key != key or manifest.area != self.rule_for(key).area or manifest.payload_digest != _sha256(payload) or manifest.size_bytes != len(payload):
                raise SharedResourceError("poisoned_entry", "shared entry identity or content is invalid")
            return SharedLookup(SharedResourceStatus.EXACT_HIT, True, True, key, manifest, payload, "EXACT_KEY_MATCH")
        except (SharedResourceError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            return SharedLookup(SharedResourceStatus.POISONED, False, False, key, None, None, getattr(exc, "code", "POISONED_ENTRY"))

    def _area_bytes(self, area: str, *, exclude: Path | None = None) -> int:
        area_root = _safe_child(self.root, area, field_name="quota area")
        total = 0
        if not area_root.exists():
            return 0
        for manifest_path in area_root.rglob("manifest.json"):
            _assert_no_reparse_components(manifest_path, field_name="quota manifest")
            directory = manifest_path.parent
            if exclude is not None and directory == exclude:
                continue
            payload_path = directory / "payload.bin"
            try:
                payload = _read_regular(payload_path, field_name="quota payload")
            except SharedResourceError:
                continue
            total += len(payload)
        return total

    def _publish_file(self, target: Path, payload: bytes) -> None:
        _assert_no_reparse_components(target.parent, field_name="shared publication parent")
        staging = target.parent / f".{target.name}.partial-{uuid.uuid4().hex}"
        try:
            _write_fsync(staging, payload)
            _assert_no_reparse_components(target.parent, field_name="shared publication parent")
            os.replace(staging, target)
        except OSError as exc:
            raise SharedResourceError("shared_publish_failed", "shared publication failed") from exc
        finally:
            try:
                staging.unlink(missing_ok=True)
            except OSError:
                pass

    def publish(
        self,
        key: SharedResourceKey,
        payload: bytes,
        *,
        project_id: str,
        allow_write: bool = True,
        fault: str | None = None,
    ) -> SharedResourceResult:
        if not isinstance(key, SharedResourceKey):
            raise SharedResourceError("key_invalid", "publish requires SharedResourceKey")
        _identity(project_id, "project_id")
        if not isinstance(payload, bytes):
            raise SharedResourceError("payload_invalid", "shared payload must be bytes")
        if fault not in {None, "before_publication", "after_payload", "before_manifest", "after_manifest"}:
            raise SharedResourceError("fault_invalid", "unsupported shared publication fault")
        rule = self.rule_for(key)
        paths = self.paths_for(key)
        if not rule.shared_allowed or rule.project_local_required or rule.mutability is MutabilityClass.MUTABLE_DISALLOWED:
            return self._result(key, SharedResourceStatus.BLOCKED, paths=paths, reason="PROJECT_STATE_MUST_REMAIN_LOCAL")
        if not allow_write:
            return self._result(key, SharedResourceStatus.ACL_DENIED, paths=paths, reason="ACL_DENIED_NO_ELEVATION")
        observed_digest = _sha256(payload)
        if observed_digest != key.content_digest:
            return self._result(key, SharedResourceStatus.INVALID, paths=paths, reason="PAYLOAD_DIGEST_MISMATCH")
        _ensure_directory(paths.directory, field_name="shared resource directory")
        lock = OwnedSharedLock(paths.lock, owner_token=f"writer-{uuid.uuid4().hex}")
        if not lock.acquire(timeout_seconds=2.0):
            return self._result(key, SharedResourceStatus.LOCK_BUSY, paths=paths, reason="FOREIGN_OR_BUSY_LOCK")
        try:
            existing = self.lookup(key)
            if existing.hit and existing.manifest is not None:
                return self._result(
                    key,
                    SharedResourceStatus.EXACT_HIT,
                    accepted=True,
                    exact_hit=True,
                    manifest_digest=existing.manifest.manifest_digest,
                    paths=paths,
                    reason="EXACT_KEY_REUSED",
                )
            if fault == "before_publication":
                raise SharedPublicationInterrupted("crash_before_publication", "publication interrupted before payload publication")
            if self._area_bytes(rule.area, exclude=paths.directory) + len(payload) > self.quota.limit_for(rule.area):
                return self._result(key, SharedResourceStatus.QUOTA_BLOCKED, paths=paths, reason="SHARED_AREA_QUOTA_EXHAUSTED")
            self._publish_file(paths.payload, payload)
            if fault == "after_payload":
                raise SharedPublicationInterrupted("crash_after_payload", "publication interrupted after payload publication")
            manifest = SharedResourceManifest(
                key=key,
                area=rule.area,
                mutability=rule.mutability,
                payload_digest=observed_digest,
                size_bytes=len(payload),
            )
            if fault == "before_manifest":
                raise SharedPublicationInterrupted("crash_before_manifest", "publication interrupted before manifest publication")
            self._publish_file(paths.manifest, canonical_json_bytes(manifest.to_dict()))
            if fault == "after_manifest":
                raise SharedPublicationInterrupted("crash_after_manifest", "publication interrupted after manifest publication")
            verified = self.lookup(key)
            if not verified.hit or verified.manifest is None:
                return self._result(key, SharedResourceStatus.POISONED, paths=paths, reason="POST_PUBLICATION_VERIFICATION_FAILED")
            status = SharedResourceStatus.REBUILT if existing.status is SharedResourceStatus.POISONED else SharedResourceStatus.PUBLISHED
            return self._result(
                key,
                status,
                accepted=True,
                bytes_written=len(payload),
                manifest_digest=verified.manifest.manifest_digest,
                paths=paths,
                reason="SEALED_SHARED_ENTRY_READY",
            )
        finally:
            lock.release()

    def probe_write(self, relative_path: str, payload: bytes = b"probe") -> bool:
        target = _safe_child(self.root, relative_path, field_name="security probe path")
        _ensure_directory(target.parent, field_name="security probe parent")
        self._publish_file(target, payload)
        return True

    def mark_active(self, key: SharedResourceKey, *, owner_token: str) -> bool:
        token = _text(owner_token, "active.owner_token")
        if _TOKEN.fullmatch(token) is None:
            raise SharedResourceError("active_owner_invalid", "active owner token is invalid")
        self.rule_for(key)
        if key.key_digest in self._active:
            return False
        self._active[key.key_digest] = token
        return True

    def release_active(self, key: SharedResourceKey, *, owner_token: str) -> bool:
        if self._active.get(key.key_digest) != owner_token:
            return False
        del self._active[key.key_digest]
        return True

    def garbage_collect(self, *, referenced_key_digests: Sequence[str] = ()) -> tuple[str, ...]:
        referenced = {_digest(item, "referenced_key_digest") for item in referenced_key_digests}
        collected: list[str] = []
        for area, _ in self.layout.areas:
            area_root = _safe_child(self.root, area, field_name="garbage collection area")
            for manifest_path in area_root.rglob("manifest.json"):
                _assert_no_reparse_components(manifest_path, field_name="garbage collection manifest")
                directory = manifest_path.parent
                try:
                    document = json.loads(_read_regular(manifest_path, field_name="garbage collection manifest").decode("utf-8"))
                    manifest = SharedResourceManifest.from_dict(document)
                except (SharedResourceError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                    continue
                if manifest.key.key_digest in referenced or manifest.key.key_digest in self._active:
                    continue
                lock = directory / ".lock"
                if lock.exists():
                    continue
                _assert_no_reparse_components(directory, field_name="garbage collection resource")
                try:
                    (directory / "payload.bin").unlink(missing_ok=True)
                    manifest_path.unlink(missing_ok=True)
                    directory.rmdir()
                except OSError:
                    continue
                collected.append(manifest.key.key_digest)
        return tuple(sorted(collected))


__all__ = [
    "CANONICAL_SHARED_ROOT",
    "DEFAULT_SHARED_RESOURCE_POLICY",
    "MutabilityClass",
    "OwnedSharedLock",
    "QuotaPolicy",
    "SHARED_LAYOUT_SCHEMA",
    "SHARED_LAYOUT_VERSION",
    "SHARED_LAYOUT_VERSION_EXPLICIT",
    "SHARED_RESOURCE_POLICY_SCHEMA",
    "SHARED_RESOURCE_POLICY_VERSION",
    "SHARED_RESOURCE_POLICY_VERSION_EXPLICIT",
    "SHARED_RESOURCE_RESULT_SCHEMA",
    "SHARED_RESOURCE_RESULT_VERSION",
    "SHARED_RESOURCE_RESULT_VERSION_EXPLICIT",
    "SharedLayoutContract",
    "SharedLookup",
    "SharedPublicationInterrupted",
    "SharedResourceError",
    "SharedResourceKey",
    "SharedResourceManifest",
    "SharedResourcePaths",
    "SharedResourcePolicy",
    "SharedResourceResult",
    "SharedResourceStatus",
    "SharedResourceStore",
    "SharingRule",
]

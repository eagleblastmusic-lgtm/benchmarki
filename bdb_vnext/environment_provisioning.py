"""Bounded project-local environment planning and provisioning for NX-036.

This module deliberately uses a deterministic, fixture-safe adapter.  It
records the exact tool/package inputs and creates only project-local marker
artifacts.  It does not invoke pip, npm, rustup, mutate PATH/registry, use a
shared root, or silently fall back to global state.  A later execution task
may attach real adapters behind the same contracts and policy boundary.
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


ENVIRONMENT_PROVISIONING_SCHEMA = "bdb-vnext-environment-provisioning-v1"
ENVIRONMENT_PROVISIONING_VERSION = "v1"
ENVIRONMENT_PLAN_SCHEMA = "bdb-vnext-environment-plan-v1"
ENVIRONMENT_PLAN_VERSION = "v1"
ENVIRONMENT_RESULT_SCHEMA = "bdb-vnext-environment-result-v1"
ENVIRONMENT_RESULT_VERSION = "v1"
ENVIRONMENT_MANIFEST_SCHEMA = "bdb-vnext-environment-manifest-v1"
ENVIRONMENT_MANIFEST_VERSION = "v1"
ENVIRONMENT_PROVISIONING_VERSION_EXPLICIT = True
ENVIRONMENT_PLAN_VERSION_EXPLICIT = True
ENVIRONMENT_RESULT_VERSION_EXPLICIT = True
ENVIRONMENT_MANIFEST_VERSION_EXPLICIT = True

PROJECT_ENVIRONMENT_ROOT = ".bdb-vnext/environment"
MARKER_SCHEMA = "bdb-vnext-project-environment-marker-v1"
MAX_MARKER_BYTES = 512 * 1024

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_IDENTITY = re.compile(r"^[a-z][a-z0-9_.:-]*$")
_ABSOLUTE_WINDOWS = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


class _StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class EffectClass(_StringEnum):
    NO_MUTATION = "NO_MUTATION"
    SAFE_PROJECT_LOCAL_MUTATION = "SAFE_PROJECT_LOCAL_MUTATION"
    SHARED_RESOURCE_MUTATION = "SHARED_RESOURCE_MUTATION"
    PRIVILEGE_REQUIRED = "PRIVILEGE_REQUIRED"
    POLICY_DENIED = "POLICY_DENIED"


class ApprovalClass(_StringEnum):
    NO_MUTATION = "NO_MUTATION"
    SAFE_PROJECT_LOCAL_MUTATION = "SAFE_PROJECT_LOCAL_MUTATION"
    SHARED_RESOURCE_MUTATION = "SHARED_RESOURCE_MUTATION"
    PRIVILEGE_REQUIRED = "PRIVILEGE_REQUIRED"
    POLICY_DENIED = "POLICY_DENIED"


class EnvironmentStatus(_StringEnum):
    PROVISIONED = "PROVISIONED"
    ALREADY_READY = "ALREADY_READY"
    REBUILT = "REBUILT"
    BLOCKED = "BLOCKED"
    OFFLINE_CACHE_MISS = "OFFLINE_CACHE_MISS"
    STALE_PLAN = "STALE_PLAN"
    POLICY_DENIED = "POLICY_DENIED"


class ProvisioningError(ValueError):
    """Stable fail-closed error for the environment contract or filesystem."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ProvisioningInterrupted(ProvisioningError):
    """A deterministic crash boundary used by the focused qualification."""


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProvisioningError("text_required", f"{field_name} must be non-empty text")
    return value


def _identity(value: Any, field_name: str) -> str:
    text = _text(value, field_name)
    if _IDENTITY.fullmatch(text) is None:
        raise ProvisioningError("identity_invalid", f"{field_name} has an invalid identity")
    return text


def _digest(value: Any, field_name: str) -> str:
    text = _text(value, field_name)
    if _DIGEST.fullmatch(text) is None:
        raise ProvisioningError("digest_invalid", f"{field_name} must be a sha256 digest")
    return text


def _source_sha(value: Any, field_name: str) -> str:
    text = _text(value, field_name).lower()
    if _SHA.fullmatch(text) is None:
        raise ProvisioningError("source_identity_invalid", f"{field_name} must be a Git SHA")
    return text


def _strict_mapping(value: Any, fields: set[str], field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProvisioningError("mapping_required", f"{field_name} must be an object")
    unknown = sorted(str(key) for key in value if str(key) not in fields)
    if unknown:
        raise ProvisioningError("unknown_field", f"{field_name} has unknown fields: {', '.join(unknown)}")
    return value


def _bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ProvisioningError("boolean_required", f"{field_name} must be boolean")
    return value


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _normalise_digest_map(value: Mapping[str, str] | None, field_name: str) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise ProvisioningError("digest_map_invalid", f"{field_name} must be an object")
    entries = tuple(sorted((_text(name, f"{field_name}.name"), _digest(digest, f"{field_name}.{name}")) for name, digest in value.items()))
    if len({name for name, _ in entries}) != len(entries):
        raise ProvisioningError("digest_map_duplicate", f"{field_name} contains duplicate names")
    return entries


def _digest_map(entries: tuple[tuple[str, str], ...]) -> dict[str, str]:
    return {name: digest for name, digest in entries}


def _normalise_relative(value: Any, field_name: str) -> str:
    text = _text(value, field_name).replace("\\", "/")
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text) or text.startswith("//"):
        raise ProvisioningError("path_escape", f"{field_name} must be relative")
    parts = text.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise ProvisioningError("path_escape", f"{field_name} contains traversal or empty path components")
    return "/".join(parts)


def _is_reparse(info: os.stat_result) -> bool:
    marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(int(getattr(info, "st_file_attributes", 0)) & marker)


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
            raise ProvisioningError("reparse_point", f"{field_name} contains a symlink or reparse point")


def _project_root(value: str | Path) -> Path:
    root = Path(value).expanduser().absolute()
    if not root.is_dir():
        raise ProvisioningError("project_root_missing", "project root must be an existing directory")
    _assert_no_reparse_components(root, field_name="project_root")
    return root


def _safe_project_child(root: Path, relative: str, *, field_name: str) -> Path:
    normalised = _normalise_relative(relative, field_name)
    target = root.joinpath(*normalised.split("/"))
    _assert_no_reparse_components(target, field_name=field_name)
    resolved_root = root.resolve(strict=False)
    resolved_target = target.resolve(strict=False)
    try:
        resolved_target.relative_to(resolved_root)
    except ValueError as exc:
        raise ProvisioningError("path_escape", f"{field_name} escapes the project root") from exc
    if resolved_target == resolved_root:
        raise ProvisioningError("path_escape", f"{field_name} cannot be the project root")
    return target


def _ensure_directory(path: Path, *, field_name: str) -> bool:
    """Create a directory one component at a time and reject reparse paths."""

    absolute = path.absolute()
    current = Path(absolute.anchor) if absolute.anchor else Path()
    parts = absolute.parts[1:] if absolute.anchor else absolute.parts
    created = False
    for part in parts:
        current /= part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            try:
                current.mkdir()
            except FileExistsError:
                pass
            created = True
            info = os.lstat(current)
        if stat.S_ISLNK(info.st_mode) or _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
            raise ProvisioningError("invalid_environment_path", f"{field_name} contains a non-directory component")
    return created


def _atomic_write(path: Path, payload: bytes) -> None:
    if len(payload) > MAX_MARKER_BYTES:
        raise ProvisioningError("marker_too_large", "environment marker exceeds the bounded size")
    _assert_no_reparse_components(path.parent, field_name="environment marker parent")
    staging = path.parent / f".{path.name}.partial-{uuid.uuid4().hex}"
    try:
        with staging.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _assert_no_reparse_components(path.parent, field_name="environment marker parent")
        os.replace(staging, path)
    except OSError as exc:
        raise ProvisioningError("environment_write_failed", "project-local environment write failed") from exc
    finally:
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            pass


def _read_regular(path: Path, *, field_name: str) -> bytes:
    _assert_no_reparse_components(path, field_name=field_name)
    try:
        info = os.lstat(path)
    except FileNotFoundError as exc:
        raise ProvisioningError("artifact_missing", f"{field_name} is missing") from exc
    if stat.S_ISLNK(info.st_mode) or _is_reparse(info) or not stat.S_ISREG(info.st_mode):
        raise ProvisioningError("invalid_artifact", f"{field_name} is not a regular file")
    if info.st_size > MAX_MARKER_BYTES:
        raise ProvisioningError("artifact_too_large", f"{field_name} exceeds the bounded size")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ProvisioningError("artifact_read_failed", f"{field_name} could not be read") from exc
    if len(payload) > MAX_MARKER_BYTES:
        raise ProvisioningError("artifact_too_large", f"{field_name} exceeds the bounded size")
    return payload


@dataclass(frozen=True)
class ToolIdentity:
    name: str
    path: str
    digest: str
    version: str

    def __post_init__(self) -> None:
        _identity(self.name, "tool.name")
        path = _text(self.path, "tool.path")
        if _ABSOLUTE_WINDOWS.match(path) is None and not Path(path).is_absolute():
            raise ProvisioningError("tool_path_invalid", "tool.path must be absolute")
        _digest(self.digest, "tool.digest")
        _text(self.version, "tool.version")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ToolIdentity":
        data = _strict_mapping(value, {"name", "path", "digest", "version"}, "tool")
        return cls(
            name=_identity(data.get("name"), "tool.name"),
            path=_text(data.get("path"), "tool.path"),
            digest=_digest(data.get("digest"), "tool.digest"),
            version=_text(data.get("version"), "tool.version"),
        )

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "path": self.path, "digest": self.digest, "version": self.version}


@dataclass(frozen=True)
class PackageIdentity:
    name: str
    version: str
    digest: str

    def __post_init__(self) -> None:
        _identity(self.name.replace("-", "."), "package.name")
        _text(self.version, "package.version")
        _digest(self.digest, "package.digest")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PackageIdentity":
        data = _strict_mapping(value, {"name", "version", "digest"}, "package")
        return cls(
            name=_text(data.get("name"), "package.name"),
            version=_text(data.get("version"), "package.version"),
            digest=_digest(data.get("digest"), "package.digest"),
        )

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "version": self.version, "digest": self.digest}


def _ordered_packages(value: Sequence[PackageIdentity], field_name: str) -> tuple[PackageIdentity, ...]:
    if any(not isinstance(item, PackageIdentity) for item in value):
        raise ProvisioningError("package_invalid", f"{field_name} contains an untyped package")
    ordered = tuple(sorted(value, key=lambda item: item.name))
    if len({item.name for item in ordered}) != len(ordered):
        raise ProvisioningError("package_duplicate", f"{field_name} contains duplicate packages")
    return ordered


@dataclass(frozen=True)
class PlatformIdentity:
    os_name: str
    architecture: str
    machine_id: str
    path_digest: str
    python_implementation: str

    def __post_init__(self) -> None:
        for field_name, value in (
            ("os_name", self.os_name),
            ("architecture", self.architecture),
            ("machine_id", self.machine_id),
            ("python_implementation", self.python_implementation),
        ):
            _text(value, f"platform.{field_name}")
        _digest(self.path_digest, "platform.path_digest")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PlatformIdentity":
        data = _strict_mapping(value, {"os_name", "architecture", "machine_id", "path_digest", "python_implementation"}, "platform")
        return cls(
            os_name=_text(data.get("os_name"), "platform.os_name"),
            architecture=_text(data.get("architecture"), "platform.architecture"),
            machine_id=_text(data.get("machine_id"), "platform.machine_id"),
            path_digest=_digest(data.get("path_digest"), "platform.path_digest"),
            python_implementation=_text(data.get("python_implementation"), "platform.python_implementation"),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "os_name": self.os_name,
            "architecture": self.architecture,
            "machine_id": self.machine_id,
            "path_digest": self.path_digest,
            "python_implementation": self.python_implementation,
        }


@dataclass(frozen=True)
class PythonEnvironmentRequest:
    interpreter: ToolIdentity
    requirements_digest: str
    packages: tuple[PackageIdentity, ...] = ()
    venv_relative_path: str = "python"
    offline: bool = False
    cache_available: bool = True

    def __post_init__(self) -> None:
        if self.interpreter.name not in {"python", "python3"}:
            raise ProvisioningError("python_identity_invalid", "Python request needs a Python interpreter identity")
        _digest(self.requirements_digest, "python.requirements_digest")
        object.__setattr__(self, "packages", _ordered_packages(self.packages, "python.packages"))
        object.__setattr__(self, "venv_relative_path", _normalise_relative(self.venv_relative_path, "python.venv_relative_path"))
        _bool(self.offline, "python.offline")
        _bool(self.cache_available, "python.cache_available")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PythonEnvironmentRequest":
        fields = {"interpreter", "requirements_digest", "packages", "venv_relative_path", "offline", "cache_available"}
        data = _strict_mapping(value, fields, "python")
        raw_packages = data.get("packages", [])
        if (
            not isinstance(data.get("interpreter"), Mapping)
            or not isinstance(raw_packages, list)
            or any(not isinstance(item, Mapping) for item in raw_packages)
        ):
            raise ProvisioningError("python_shape_invalid", "python request has invalid nested fields")
        return cls(
            interpreter=ToolIdentity.from_dict(data["interpreter"]),
            requirements_digest=_digest(data.get("requirements_digest"), "python.requirements_digest"),
            packages=tuple(PackageIdentity.from_dict(item) for item in raw_packages),
            venv_relative_path=_text(data.get("venv_relative_path", "python"), "python.venv_relative_path"),
            offline=data.get("offline", False),
            cache_available=data.get("cache_available", True),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "interpreter": self.interpreter.to_dict(),
            "requirements_digest": self.requirements_digest,
            "packages": [item.to_dict() for item in self.packages],
            "venv_relative_path": self.venv_relative_path,
            "offline": self.offline,
            "cache_available": self.cache_available,
        }


@dataclass(frozen=True)
class NodeEnvironmentRequest:
    node: ToolIdentity
    npm: ToolIdentity
    manifest_digest: str
    lockfile_digest: str | None = None
    packages: tuple[PackageIdentity, ...] = ()
    node_modules_relative_path: str = "node_modules"
    package_manager: str = "npm"
    offline: bool = False
    cache_available: bool = True

    def __post_init__(self) -> None:
        if self.node.name != "node" or self.npm.name != "npm":
            raise ProvisioningError("node_identity_invalid", "Node request needs exact node and npm identities")
        _digest(self.manifest_digest, "node.manifest_digest")
        if self.lockfile_digest is not None:
            _digest(self.lockfile_digest, "node.lockfile_digest")
        object.__setattr__(self, "packages", _ordered_packages(self.packages, "node.packages"))
        object.__setattr__(self, "node_modules_relative_path", _normalise_relative(self.node_modules_relative_path, "node.node_modules_relative_path"))
        _identity(self.package_manager.replace("-", "."), "node.package_manager")
        _bool(self.offline, "node.offline")
        _bool(self.cache_available, "node.cache_available")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "NodeEnvironmentRequest":
        fields = {"node", "npm", "manifest_digest", "lockfile_digest", "packages", "node_modules_relative_path", "package_manager", "offline", "cache_available"}
        data = _strict_mapping(value, fields, "node")
        raw_packages = data.get("packages", [])
        if (
            not isinstance(data.get("node"), Mapping)
            or not isinstance(data.get("npm"), Mapping)
            or not isinstance(raw_packages, list)
            or any(not isinstance(item, Mapping) for item in raw_packages)
        ):
            raise ProvisioningError("node_shape_invalid", "node request has invalid nested fields")
        return cls(
            node=ToolIdentity.from_dict(data["node"]),
            npm=ToolIdentity.from_dict(data["npm"]),
            manifest_digest=_digest(data.get("manifest_digest"), "node.manifest_digest"),
            lockfile_digest=data.get("lockfile_digest"),
            packages=tuple(PackageIdentity.from_dict(item) for item in raw_packages),
            node_modules_relative_path=_text(data.get("node_modules_relative_path", "node_modules"), "node.node_modules_relative_path"),
            package_manager=_text(data.get("package_manager", "npm"), "node.package_manager"),
            offline=data.get("offline", False),
            cache_available=data.get("cache_available", True),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "node": self.node.to_dict(),
            "npm": self.npm.to_dict(),
            "manifest_digest": self.manifest_digest,
            "lockfile_digest": self.lockfile_digest,
            "packages": [item.to_dict() for item in self.packages],
            "node_modules_relative_path": self.node_modules_relative_path,
            "package_manager": self.package_manager,
            "offline": self.offline,
            "cache_available": self.cache_available,
        }


@dataclass(frozen=True)
class RustEnvironmentRequest:
    rustup: ToolIdentity
    rustc: ToolIdentity
    cargo: ToolIdentity
    toolchain: str
    manifest_digest: str
    lockfile_digest: str | None = None
    target_relative_path: str = "target"
    offline: bool = False
    cache_available: bool = True

    def __post_init__(self) -> None:
        if self.rustup.name != "rustup" or self.rustc.name != "rustc" or self.cargo.name != "cargo":
            raise ProvisioningError("rust_identity_invalid", "Rust request needs rustup, rustc and cargo identities")
        _identity(self.toolchain.replace("-", "."), "rust.toolchain")
        _digest(self.manifest_digest, "rust.manifest_digest")
        if self.lockfile_digest is not None:
            _digest(self.lockfile_digest, "rust.lockfile_digest")
        object.__setattr__(self, "target_relative_path", _normalise_relative(self.target_relative_path, "rust.target_relative_path"))
        _bool(self.offline, "rust.offline")
        _bool(self.cache_available, "rust.cache_available")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RustEnvironmentRequest":
        fields = {"rustup", "rustc", "cargo", "toolchain", "manifest_digest", "lockfile_digest", "target_relative_path", "offline", "cache_available"}
        data = _strict_mapping(value, fields, "rust")
        if not all(isinstance(data.get(name), Mapping) for name in ("rustup", "rustc", "cargo")):
            raise ProvisioningError("rust_shape_invalid", "rust request has invalid tool identities")
        return cls(
            rustup=ToolIdentity.from_dict(data["rustup"]),
            rustc=ToolIdentity.from_dict(data["rustc"]),
            cargo=ToolIdentity.from_dict(data["cargo"]),
            toolchain=_text(data.get("toolchain"), "rust.toolchain"),
            manifest_digest=_digest(data.get("manifest_digest"), "rust.manifest_digest"),
            lockfile_digest=data.get("lockfile_digest"),
            target_relative_path=_text(data.get("target_relative_path", "target"), "rust.target_relative_path"),
            offline=data.get("offline", False),
            cache_available=data.get("cache_available", True),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "rustup": self.rustup.to_dict(),
            "rustc": self.rustc.to_dict(),
            "cargo": self.cargo.to_dict(),
            "toolchain": self.toolchain,
            "manifest_digest": self.manifest_digest,
            "lockfile_digest": self.lockfile_digest,
            "target_relative_path": self.target_relative_path,
            "offline": self.offline,
            "cache_available": self.cache_available,
        }


@dataclass(frozen=True)
class RequestedEffect:
    component: str
    effect_class: EffectClass
    approval_class: ApprovalClass
    target_relative_path: str
    reason: str

    def __post_init__(self) -> None:
        _identity(self.component, "effect.component")
        object.__setattr__(self, "effect_class", EffectClass(self.effect_class))
        object.__setattr__(self, "approval_class", ApprovalClass(self.approval_class))
        object.__setattr__(self, "target_relative_path", _normalise_relative(self.target_relative_path, "effect.target_relative_path"))
        _text(self.reason, "effect.reason")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RequestedEffect":
        data = _strict_mapping(value, {"component", "effect_class", "approval_class", "target_relative_path", "reason"}, "effect")
        return cls(
            component=_identity(data.get("component"), "effect.component"),
            effect_class=EffectClass(data.get("effect_class")),
            approval_class=ApprovalClass(data.get("approval_class")),
            target_relative_path=_text(data.get("target_relative_path"), "effect.target_relative_path"),
            reason=_text(data.get("reason"), "effect.reason"),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "component": self.component,
            "effect_class": self.effect_class.value,
            "approval_class": self.approval_class.value,
            "target_relative_path": self.target_relative_path,
            "reason": self.reason,
        }


def _component_effects(
    root: str,
    python: PythonEnvironmentRequest | None,
    node: NodeEnvironmentRequest | None,
    rust: RustEnvironmentRequest | None,
) -> tuple[RequestedEffect, ...]:
    effects: list[RequestedEffect] = []
    if python is not None:
        effects.append(RequestedEffect("python", EffectClass.SAFE_PROJECT_LOCAL_MUTATION, ApprovalClass.SAFE_PROJECT_LOCAL_MUTATION, f"{root}/{python.venv_relative_path}/pyvenv.cfg", "project-local Python venv marker"))
    if node is not None:
        effects.append(RequestedEffect("node", EffectClass.SAFE_PROJECT_LOCAL_MUTATION, ApprovalClass.SAFE_PROJECT_LOCAL_MUTATION, f"{root}/{node.node_modules_relative_path}/.bdb-node-install.json", "project-local Node dependency marker"))
    if rust is not None:
        effects.append(RequestedEffect("rust", EffectClass.SAFE_PROJECT_LOCAL_MUTATION, ApprovalClass.SAFE_PROJECT_LOCAL_MUTATION, f"{root}/{rust.target_relative_path}/.bdb-rust-toolchain.json", "project-local Rust target marker"))
    return tuple(effects)


@dataclass(frozen=True)
class EnvironmentPlan:
    plan_id: str
    project_id: str
    task_id: str
    requirement_set_id: str
    requirement_digest: str
    inventory_digest: str
    source_head: str
    source_tree: str
    platform_identity: PlatformIdentity
    provisioning_adapter_version: str
    approval_class: ApprovalClass
    project_environment_relative_root: str
    requested_effects: tuple[RequestedEffect, ...]
    python: PythonEnvironmentRequest | None = None
    node: NodeEnvironmentRequest | None = None
    rust: RustEnvironmentRequest | None = None
    schema: str = ENVIRONMENT_PLAN_SCHEMA
    version: str = ENVIRONMENT_PLAN_VERSION
    plan_digest: str = field(default="", compare=True)

    def __post_init__(self) -> None:
        if self.schema != ENVIRONMENT_PLAN_SCHEMA or self.version != ENVIRONMENT_PLAN_VERSION:
            raise ProvisioningError("plan_version_invalid", "unsupported environment plan schema/version")
        for field_name, value in (("plan_id", self.plan_id), ("project_id", self.project_id), ("task_id", self.task_id), ("requirement_set_id", self.requirement_set_id), ("provisioning_adapter_version", self.provisioning_adapter_version)):
            _identity(value, f"plan.{field_name}")
        _digest(self.requirement_digest, "plan.requirement_digest")
        _digest(self.inventory_digest, "plan.inventory_digest")
        _source_sha(self.source_head, "plan.source_head")
        _source_sha(self.source_tree, "plan.source_tree")
        if not isinstance(self.platform_identity, PlatformIdentity):
            raise ProvisioningError("platform_invalid", "plan.platform_identity must be typed")
        object.__setattr__(self, "approval_class", ApprovalClass(self.approval_class))
        object.__setattr__(self, "project_environment_relative_root", _normalise_relative(self.project_environment_relative_root, "plan.project_environment_relative_root"))
        if any(not isinstance(item, RequestedEffect) for item in self.requested_effects):
            raise ProvisioningError("effect_invalid", "plan.requested_effects must be typed")
        ordered_effects = tuple(sorted(self.requested_effects, key=lambda item: (item.component, item.target_relative_path)))
        if len({(item.component, item.target_relative_path) for item in ordered_effects}) != len(ordered_effects):
            raise ProvisioningError("effect_duplicate", "plan.requested_effects contains duplicate targets")
        object.__setattr__(self, "requested_effects", ordered_effects)
        components = {name for name, value in (("python", self.python), ("node", self.node), ("rust", self.rust)) if value is not None}
        if any(item.component not in components for item in ordered_effects):
            raise ProvisioningError("effect_component_missing", "plan effect names an absent component")
        computed = _sha256_bytes(canonical_json_bytes(self._payload()))
        if self.plan_digest and self.plan_digest != computed:
            raise ProvisioningError("plan_digest_mismatch", "environment plan digest does not match content")
        object.__setattr__(self, "plan_digest", computed)

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "plan_id": self.plan_id,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "requirement_set_id": self.requirement_set_id,
            "requirement_digest": self.requirement_digest,
            "inventory_digest": self.inventory_digest,
            "source_head": self.source_head,
            "source_tree": self.source_tree,
            "platform_identity": self.platform_identity.to_dict(),
            "provisioning_adapter_version": self.provisioning_adapter_version,
            "approval_class": self.approval_class.value,
            "project_environment_relative_root": self.project_environment_relative_root,
            "requested_effects": [item.to_dict() for item in self.requested_effects],
            "python": None if self.python is None else self.python.to_dict(),
            "node": None if self.node is None else self.node.to_dict(),
            "rust": None if self.rust is None else self.rust.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["plan_digest"] = self.plan_digest
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EnvironmentPlan":
        fields = {
            "schema", "version", "plan_id", "project_id", "task_id", "requirement_set_id", "requirement_digest", "inventory_digest", "source_head", "source_tree", "platform_identity", "provisioning_adapter_version", "approval_class", "project_environment_relative_root", "requested_effects", "python", "node", "rust", "plan_digest",
        }
        data = _strict_mapping(value, fields, "environment_plan")
        raw_effects = data.get("requested_effects")
        if (
            not isinstance(data.get("platform_identity"), Mapping)
            or not isinstance(raw_effects, list)
            or any(not isinstance(item, Mapping) for item in raw_effects)
        ):
            raise ProvisioningError("plan_shape_invalid", "environment plan has invalid nested fields")
        def optional_request(name: str, factory: Any) -> Any:
            raw = data.get(name)
            if raw is None:
                return None
            if not isinstance(raw, Mapping):
                raise ProvisioningError("plan_shape_invalid", f"plan.{name} must be an object or null")
            return factory(raw)
        return cls(
            schema=_text(data.get("schema"), "plan.schema"),
            version=_text(data.get("version"), "plan.version"),
            plan_id=_identity(data.get("plan_id"), "plan.plan_id"),
            project_id=_identity(data.get("project_id"), "plan.project_id"),
            task_id=_identity(data.get("task_id"), "plan.task_id"),
            requirement_set_id=_identity(data.get("requirement_set_id"), "plan.requirement_set_id"),
            requirement_digest=_digest(data.get("requirement_digest"), "plan.requirement_digest"),
            inventory_digest=_digest(data.get("inventory_digest"), "plan.inventory_digest"),
            source_head=_source_sha(data.get("source_head"), "plan.source_head"),
            source_tree=_source_sha(data.get("source_tree"), "plan.source_tree"),
            platform_identity=PlatformIdentity.from_dict(data["platform_identity"]),
            provisioning_adapter_version=_identity(data.get("provisioning_adapter_version"), "plan.provisioning_adapter_version"),
            approval_class=ApprovalClass(data.get("approval_class")),
            project_environment_relative_root=_text(data.get("project_environment_relative_root"), "plan.project_environment_relative_root"),
            requested_effects=tuple(RequestedEffect.from_dict(item) for item in raw_effects),
            python=optional_request("python", PythonEnvironmentRequest.from_dict),
            node=optional_request("node", NodeEnvironmentRequest.from_dict),
            rust=optional_request("rust", RustEnvironmentRequest.from_dict),
            plan_digest=_digest(data.get("plan_digest"), "plan.plan_digest"),
        )


@dataclass(frozen=True)
class ManifestEntry:
    relative_path: str
    digest: str
    size_bytes: int
    kind: str = "FILE"

    def __post_init__(self) -> None:
        object.__setattr__(self, "relative_path", _normalise_relative(self.relative_path, "manifest_entry.relative_path"))
        _digest(self.digest, "manifest_entry.digest")
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int) or self.size_bytes < 0:
            raise ProvisioningError("manifest_size_invalid", "manifest entry size must be a non-negative integer")
        if self.kind != "FILE":
            raise ProvisioningError("manifest_kind_invalid", "only regular files are supported by NX-036")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ManifestEntry":
        data = _strict_mapping(value, {"relative_path", "digest", "size_bytes", "kind"}, "manifest_entry")
        return cls(
            relative_path=_text(data.get("relative_path"), "manifest_entry.relative_path"),
            digest=_digest(data.get("digest"), "manifest_entry.digest"),
            size_bytes=data.get("size_bytes"),
            kind=_text(data.get("kind", "FILE"), "manifest_entry.kind"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"relative_path": self.relative_path, "digest": self.digest, "size_bytes": self.size_bytes, "kind": self.kind}


@dataclass(frozen=True)
class ManifestComponent:
    component: str
    tool_identities: tuple[ToolIdentity, ...]
    package_identities: tuple[PackageIdentity, ...]
    output_paths: tuple[str, ...]
    manifest_digests: tuple[tuple[str, str], ...]
    lockfile_digests: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        _identity(self.component, "manifest_component.component")
        if any(not isinstance(item, ToolIdentity) for item in self.tool_identities):
            raise ProvisioningError("manifest_component_invalid", "manifest component tool identities must be typed")
        if any(not isinstance(item, PackageIdentity) for item in self.package_identities):
            raise ProvisioningError("manifest_component_invalid", "manifest component packages must be typed")
        object.__setattr__(self, "package_identities", _ordered_packages(self.package_identities, "manifest_component.package_identities"))
        output_paths = tuple(sorted(_normalise_relative(item, "manifest_component.output_path") for item in self.output_paths))
        if len(set(output_paths)) != len(output_paths):
            raise ProvisioningError("manifest_component_duplicate", "manifest component output paths must be unique")
        object.__setattr__(self, "output_paths", output_paths)
        for field_name, entries in (("manifest_digests", self.manifest_digests), ("lockfile_digests", self.lockfile_digests)):
            if tuple(sorted(entries)) != entries:
                raise ProvisioningError("manifest_digest_order_invalid", f"{field_name} is not canonical")
            for name, digest in entries:
                _text(name, f"{field_name}.name")
                _digest(digest, f"{field_name}.{name}")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ManifestComponent":
        fields = {"component", "tool_identities", "package_identities", "output_paths", "manifest_digests", "lockfile_digests"}
        data = _strict_mapping(value, fields, "manifest_component")
        tools = data.get("tool_identities")
        packages = data.get("package_identities")
        outputs = data.get("output_paths")
        if (
            not isinstance(tools, list)
            or not isinstance(packages, list)
            or not isinstance(outputs, list)
            or any(not isinstance(item, Mapping) for item in tools)
            or any(not isinstance(item, Mapping) for item in packages)
        ):
            raise ProvisioningError("manifest_component_shape_invalid", "manifest component lists are invalid")
        return cls(
            component=_identity(data.get("component"), "manifest_component.component"),
            tool_identities=tuple(ToolIdentity.from_dict(item) for item in tools),
            package_identities=tuple(PackageIdentity.from_dict(item) for item in packages),
            output_paths=tuple(_text(item, "manifest_component.output_path") for item in outputs),
            manifest_digests=_normalise_digest_map(data.get("manifest_digests"), "manifest_component.manifest_digests"),
            lockfile_digests=_normalise_digest_map(data.get("lockfile_digests"), "manifest_component.lockfile_digests"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "tool_identities": [item.to_dict() for item in self.tool_identities],
            "package_identities": [item.to_dict() for item in self.package_identities],
            "output_paths": list(self.output_paths),
            "manifest_digests": _digest_map(self.manifest_digests),
            "lockfile_digests": _digest_map(self.lockfile_digests),
        }


def _manifest_components(plan: EnvironmentPlan) -> tuple[ManifestComponent, ...]:
    components: list[ManifestComponent] = []
    if plan.python is not None:
        components.append(ManifestComponent("python", (plan.python.interpreter,), plan.python.packages, (f"{plan.python.venv_relative_path}/pyvenv.cfg", f"{plan.python.venv_relative_path}/requirements.lock"), (("requirements", plan.python.requirements_digest),), ()))
    if plan.node is not None:
        lock = () if plan.node.lockfile_digest is None else (("lockfile", plan.node.lockfile_digest),)
        components.append(ManifestComponent("node", (plan.node.node, plan.node.npm), plan.node.packages, (f"{plan.node.node_modules_relative_path}/.bdb-node-install.json",), (("package_manifest", plan.node.manifest_digest),), lock))
    if plan.rust is not None:
        lock = () if plan.rust.lockfile_digest is None else (("lockfile", plan.rust.lockfile_digest),)
        components.append(ManifestComponent("rust", (plan.rust.rustup, plan.rust.rustc, plan.rust.cargo), (), (f"{plan.rust.target_relative_path}/.bdb-rust-toolchain.json",), (("cargo_manifest", plan.rust.manifest_digest),), lock))
    return tuple(sorted(components, key=lambda item: item.component))


@dataclass(frozen=True)
class EnvironmentManifest:
    plan_id: str
    plan_digest: str
    project_id: str
    task_id: str
    requirement_digest: str
    inventory_digest: str
    source_head: str
    source_tree: str
    platform_identity: PlatformIdentity
    provisioning_adapter_version: str
    components: tuple[ManifestComponent, ...]
    entries: tuple[ManifestEntry, ...]
    schema: str = ENVIRONMENT_MANIFEST_SCHEMA
    version: str = ENVIRONMENT_MANIFEST_VERSION
    manifest_digest: str = field(default="", compare=True)

    def __post_init__(self) -> None:
        if self.schema != ENVIRONMENT_MANIFEST_SCHEMA or self.version != ENVIRONMENT_MANIFEST_VERSION:
            raise ProvisioningError("manifest_version_invalid", "unsupported environment manifest schema/version")
        for field_name, value in (("plan_id", self.plan_id), ("project_id", self.project_id), ("task_id", self.task_id), ("provisioning_adapter_version", self.provisioning_adapter_version)):
            _identity(value, f"manifest.{field_name}")
        _digest(self.plan_digest, "manifest.plan_digest")
        _digest(self.requirement_digest, "manifest.requirement_digest")
        _digest(self.inventory_digest, "manifest.inventory_digest")
        _source_sha(self.source_head, "manifest.source_head")
        _source_sha(self.source_tree, "manifest.source_tree")
        if not isinstance(self.platform_identity, PlatformIdentity):
            raise ProvisioningError("platform_invalid", "manifest.platform_identity must be typed")
        if any(not isinstance(item, ManifestComponent) for item in self.components):
            raise ProvisioningError("manifest_component_invalid", "manifest components must be typed")
        if any(not isinstance(item, ManifestEntry) for item in self.entries):
            raise ProvisioningError("manifest_entry_invalid", "manifest entries must be typed")
        components = tuple(sorted(self.components, key=lambda item: item.component))
        entries = tuple(sorted(self.entries, key=lambda item: item.relative_path))
        if len({item.component for item in components}) != len(components):
            raise ProvisioningError("manifest_component_duplicate", "manifest components must be unique")
        if len({item.relative_path for item in entries}) != len(entries):
            raise ProvisioningError("manifest_entry_duplicate", "manifest entries must be unique")
        object.__setattr__(self, "components", components)
        object.__setattr__(self, "entries", entries)
        computed = _sha256_bytes(canonical_json_bytes(self._payload()))
        if self.manifest_digest and self.manifest_digest != computed:
            raise ProvisioningError("manifest_digest_mismatch", "environment manifest digest does not match content")
        object.__setattr__(self, "manifest_digest", computed)

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "plan_id": self.plan_id,
            "plan_digest": self.plan_digest,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "requirement_digest": self.requirement_digest,
            "inventory_digest": self.inventory_digest,
            "source_head": self.source_head,
            "source_tree": self.source_tree,
            "platform_identity": self.platform_identity.to_dict(),
            "provisioning_adapter_version": self.provisioning_adapter_version,
            "components": [item.to_dict() for item in self.components],
            "entries": [item.to_dict() for item in self.entries],
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["manifest_digest"] = self.manifest_digest
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EnvironmentManifest":
        fields = {"schema", "version", "plan_id", "plan_digest", "project_id", "task_id", "requirement_digest", "inventory_digest", "source_head", "source_tree", "platform_identity", "provisioning_adapter_version", "components", "entries", "manifest_digest"}
        data = _strict_mapping(value, fields, "environment_manifest")
        components = data.get("components")
        entries = data.get("entries")
        if (
            not isinstance(data.get("platform_identity"), Mapping)
            or not isinstance(components, list)
            or not isinstance(entries, list)
            or any(not isinstance(item, Mapping) for item in components)
            or any(not isinstance(item, Mapping) for item in entries)
        ):
            raise ProvisioningError("manifest_shape_invalid", "environment manifest has invalid nested fields")
        return cls(
            schema=_text(data.get("schema"), "manifest.schema"),
            version=_text(data.get("version"), "manifest.version"),
            plan_id=_identity(data.get("plan_id"), "manifest.plan_id"),
            plan_digest=_digest(data.get("plan_digest"), "manifest.plan_digest"),
            project_id=_identity(data.get("project_id"), "manifest.project_id"),
            task_id=_identity(data.get("task_id"), "manifest.task_id"),
            requirement_digest=_digest(data.get("requirement_digest"), "manifest.requirement_digest"),
            inventory_digest=_digest(data.get("inventory_digest"), "manifest.inventory_digest"),
            source_head=_source_sha(data.get("source_head"), "manifest.source_head"),
            source_tree=_source_sha(data.get("source_tree"), "manifest.source_tree"),
            platform_identity=PlatformIdentity.from_dict(data["platform_identity"]),
            provisioning_adapter_version=_identity(data.get("provisioning_adapter_version"), "manifest.provisioning_adapter_version"),
            components=tuple(ManifestComponent.from_dict(item) for item in components),
            entries=tuple(ManifestEntry.from_dict(item) for item in entries),
            manifest_digest=_digest(data.get("manifest_digest"), "manifest.manifest_digest"),
        )


@dataclass(frozen=True)
class ActualEffect:
    component: str
    effect_class: EffectClass
    approval_class: ApprovalClass
    target_relative_path: str
    operation: str
    artifact_digest: str | None
    source_head: str
    source_tree: str

    def __post_init__(self) -> None:
        _identity(self.component, "actual_effect.component")
        object.__setattr__(self, "effect_class", EffectClass(self.effect_class))
        object.__setattr__(self, "approval_class", ApprovalClass(self.approval_class))
        object.__setattr__(self, "target_relative_path", _normalise_relative(self.target_relative_path, "actual_effect.target_relative_path"))
        _identity(self.operation.lower().replace("_", "."), "actual_effect.operation")
        if self.artifact_digest is not None:
            _digest(self.artifact_digest, "actual_effect.artifact_digest")
        _source_sha(self.source_head, "actual_effect.source_head")
        _source_sha(self.source_tree, "actual_effect.source_tree")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ActualEffect":
        fields = {"component", "effect_class", "approval_class", "target_relative_path", "operation", "artifact_digest", "source_head", "source_tree"}
        data = _strict_mapping(value, fields, "actual_effect")
        return cls(
            component=_identity(data.get("component"), "actual_effect.component"),
            effect_class=EffectClass(data.get("effect_class")),
            approval_class=ApprovalClass(data.get("approval_class")),
            target_relative_path=_text(data.get("target_relative_path"), "actual_effect.target_relative_path"),
            operation=_text(data.get("operation"), "actual_effect.operation"),
            artifact_digest=data.get("artifact_digest"),
            source_head=_source_sha(data.get("source_head"), "actual_effect.source_head"),
            source_tree=_source_sha(data.get("source_tree"), "actual_effect.source_tree"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "effect_class": self.effect_class.value,
            "approval_class": self.approval_class.value,
            "target_relative_path": self.target_relative_path,
            "operation": self.operation,
            "artifact_digest": self.artifact_digest,
            "source_head": self.source_head,
            "source_tree": self.source_tree,
        }


@dataclass(frozen=True)
class EnvironmentResult:
    plan_id: str
    plan_digest: str
    project_id: str
    task_id: str
    source_head: str
    source_tree: str
    status: EnvironmentStatus
    approval_class: ApprovalClass
    actual_effects: tuple[ActualEffect, ...]
    created_or_reused_paths: tuple[str, ...]
    tool_identities: tuple[ToolIdentity, ...]
    manifest_digests: tuple[tuple[str, str], ...]
    lockfile_digests: tuple[tuple[str, str], ...]
    final_readiness: bool
    manifest_digest: str | None
    diagnostics: tuple[str, ...]
    schema: str = ENVIRONMENT_RESULT_SCHEMA
    version: str = ENVIRONMENT_RESULT_VERSION
    result_digest: str = field(default="", compare=True)

    def __post_init__(self) -> None:
        if self.schema != ENVIRONMENT_RESULT_SCHEMA or self.version != ENVIRONMENT_RESULT_VERSION:
            raise ProvisioningError("result_version_invalid", "unsupported environment result schema/version")
        for field_name, value in (("plan_id", self.plan_id), ("project_id", self.project_id), ("task_id", self.task_id)):
            _identity(value, f"result.{field_name}")
        _digest(self.plan_digest, "result.plan_digest")
        _source_sha(self.source_head, "result.source_head")
        _source_sha(self.source_tree, "result.source_tree")
        object.__setattr__(self, "status", EnvironmentStatus(self.status))
        object.__setattr__(self, "approval_class", ApprovalClass(self.approval_class))
        _bool(self.final_readiness, "result.final_readiness")
        if self.manifest_digest is not None:
            _digest(self.manifest_digest, "result.manifest_digest")
        if any(not isinstance(item, ActualEffect) for item in self.actual_effects):
            raise ProvisioningError("result_effect_invalid", "result.actual_effects must be typed")
        object.__setattr__(self, "created_or_reused_paths", tuple(sorted(_normalise_relative(item, "result.created_or_reused_path") for item in self.created_or_reused_paths)))
        if any(not isinstance(item, ToolIdentity) for item in self.tool_identities):
            raise ProvisioningError("result_tool_invalid", "result.tool_identities must be typed")
        object.__setattr__(self, "diagnostics", tuple(_text(item, "result.diagnostic") for item in self.diagnostics))
        object.__setattr__(self, "manifest_digests", _normalise_digest_map(dict(self.manifest_digests), "result.manifest_digests"))
        object.__setattr__(self, "lockfile_digests", _normalise_digest_map(dict(self.lockfile_digests), "result.lockfile_digests"))
        computed = _sha256_bytes(canonical_json_bytes(self._payload()))
        if self.result_digest and self.result_digest != computed:
            raise ProvisioningError("result_digest_mismatch", "environment result digest does not match content")
        object.__setattr__(self, "result_digest", computed)

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "plan_id": self.plan_id,
            "plan_digest": self.plan_digest,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "source_head": self.source_head,
            "source_tree": self.source_tree,
            "status": self.status.value,
            "approval_class": self.approval_class.value,
            "actual_effects": [item.to_dict() for item in self.actual_effects],
            "created_or_reused_paths": list(self.created_or_reused_paths),
            "tool_identities": [item.to_dict() for item in self.tool_identities],
            "manifest_digests": _digest_map(self.manifest_digests),
            "lockfile_digests": _digest_map(self.lockfile_digests),
            "final_readiness": self.final_readiness,
            "manifest_digest": self.manifest_digest,
            "diagnostics": list(self.diagnostics),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EnvironmentResult":
        fields = {
            "schema",
            "version",
            "plan_id",
            "plan_digest",
            "project_id",
            "task_id",
            "source_head",
            "source_tree",
            "status",
            "approval_class",
            "actual_effects",
            "created_or_reused_paths",
            "tool_identities",
            "manifest_digests",
            "lockfile_digests",
            "final_readiness",
            "manifest_digest",
            "diagnostics",
            "result_digest",
        }
        data = _strict_mapping(value, fields, "environment_result")
        actual_effects = data.get("actual_effects")
        created_or_reused_paths = data.get("created_or_reused_paths")
        tool_identities = data.get("tool_identities")
        diagnostics = data.get("diagnostics")
        if not isinstance(actual_effects, list) or any(not isinstance(item, Mapping) for item in actual_effects):
            raise ProvisioningError("result_effect_invalid", "environment_result.actual_effects must be objects")
        if not isinstance(created_or_reused_paths, list):
            raise ProvisioningError("result_paths_invalid", "environment_result.created_or_reused_paths must be a list")
        if not isinstance(tool_identities, list) or any(not isinstance(item, Mapping) for item in tool_identities):
            raise ProvisioningError("result_tool_invalid", "environment_result.tool_identities must be objects")
        if not isinstance(diagnostics, list):
            raise ProvisioningError("result_diagnostics_invalid", "environment_result.diagnostics must be a list")
        manifest_digests = data.get("manifest_digests")
        lockfile_digests = data.get("lockfile_digests")
        if not isinstance(manifest_digests, Mapping) or not isinstance(lockfile_digests, Mapping):
            raise ProvisioningError("result_digest_map_invalid", "environment result digest maps must be objects")
        return cls(
            plan_id=_identity(data.get("plan_id"), "result.plan_id"),
            plan_digest=_digest(data.get("plan_digest"), "result.plan_digest"),
            project_id=_identity(data.get("project_id"), "result.project_id"),
            task_id=_identity(data.get("task_id"), "result.task_id"),
            source_head=_source_sha(data.get("source_head"), "result.source_head"),
            source_tree=_source_sha(data.get("source_tree"), "result.source_tree"),
            status=EnvironmentStatus(data.get("status")),
            approval_class=ApprovalClass(data.get("approval_class")),
            actual_effects=tuple(ActualEffect.from_dict(item) for item in actual_effects),
            created_or_reused_paths=tuple(_text(item, "result.created_or_reused_path") for item in created_or_reused_paths),
            tool_identities=tuple(ToolIdentity.from_dict(item) for item in tool_identities),
            manifest_digests=tuple((str(key), _digest(item, f"result.manifest_digests.{key}")) for key, item in manifest_digests.items()),
            lockfile_digests=tuple((str(key), _digest(item, f"result.lockfile_digests.{key}")) for key, item in lockfile_digests.items()),
            final_readiness=_bool(data.get("final_readiness"), "result.final_readiness"),
            manifest_digest=None if data.get("manifest_digest") is None else _digest(data.get("manifest_digest"), "result.manifest_digest"),
            diagnostics=tuple(_text(item, "result.diagnostic") for item in diagnostics),
            schema=_text(data.get("schema"), "result.schema"),
            version=_text(data.get("version"), "result.version"),
            result_digest=_text(data.get("result_digest"), "result.result_digest"),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["result_digest"] = self.result_digest
        return payload


@dataclass(frozen=True)
class EnvironmentInspection:
    ready: bool
    manifest: EnvironmentManifest | None
    reason: str


def _tool_identities(plan: EnvironmentPlan) -> tuple[ToolIdentity, ...]:
    result: list[ToolIdentity] = []
    if plan.python is not None:
        result.append(plan.python.interpreter)
    if plan.node is not None:
        result.extend((plan.node.node, plan.node.npm))
    if plan.rust is not None:
        result.extend((plan.rust.rustup, plan.rust.rustc, plan.rust.cargo))
    return tuple(result)


def _plan_digest_maps(plan: EnvironmentPlan) -> tuple[tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]]:
    manifest: list[tuple[str, str]] = []
    lockfile: list[tuple[str, str]] = []
    if plan.python is not None:
        manifest.append(("python.requirements", plan.python.requirements_digest))
    if plan.node is not None:
        manifest.append(("node.package_manifest", plan.node.manifest_digest))
        if plan.node.lockfile_digest is not None:
            lockfile.append(("node.lockfile", plan.node.lockfile_digest))
    if plan.rust is not None:
        manifest.append(("rust.cargo_manifest", plan.rust.manifest_digest))
        if plan.rust.lockfile_digest is not None:
            lockfile.append(("rust.lockfile", plan.rust.lockfile_digest))
    return tuple(sorted(manifest)), tuple(sorted(lockfile))


def _marker_payloads(plan: EnvironmentPlan) -> tuple[tuple[str, str, bytes], ...]:
    result: list[tuple[str, str, bytes]] = []
    if plan.python is not None:
        marker = canonical_json_bytes(
            {
                "schema": MARKER_SCHEMA,
                "component": "python",
                "plan_digest": plan.plan_digest,
                "interpreter": plan.python.interpreter.to_dict(),
                "requirements_digest": plan.python.requirements_digest,
                "packages": [item.to_dict() for item in plan.python.packages],
            }
        )
        requirements = canonical_json_bytes({"requirements_digest": plan.python.requirements_digest, "packages": [item.to_dict() for item in plan.python.packages]})
        result.append(("python", f"{plan.python.venv_relative_path}/pyvenv.cfg", marker))
        result.append(("python", f"{plan.python.venv_relative_path}/requirements.lock", requirements))
    if plan.node is not None:
        marker = canonical_json_bytes(
            {
                "schema": MARKER_SCHEMA,
                "component": "node",
                "plan_digest": plan.plan_digest,
                "node": plan.node.node.to_dict(),
                "npm": plan.node.npm.to_dict(),
                "package_manager": plan.node.package_manager,
                "manifest_digest": plan.node.manifest_digest,
                "lockfile_digest": plan.node.lockfile_digest,
                "packages": [item.to_dict() for item in plan.node.packages],
            }
        )
        result.append(("node", f"{plan.node.node_modules_relative_path}/.bdb-node-install.json", marker))
    if plan.rust is not None:
        marker = canonical_json_bytes(
            {
                "schema": MARKER_SCHEMA,
                "component": "rust",
                "plan_digest": plan.plan_digest,
                "toolchain": plan.rust.toolchain,
                "rustup": plan.rust.rustup.to_dict(),
                "rustc": plan.rust.rustc.to_dict(),
                "cargo": plan.rust.cargo.to_dict(),
                "manifest_digest": plan.rust.manifest_digest,
                "lockfile_digest": plan.rust.lockfile_digest,
                "selection_scope": "PROJECT_LOCAL",
            }
        )
        result.append(("rust", f"{plan.rust.target_relative_path}/.bdb-rust-toolchain.json", marker))
    return tuple(result)


def _manifest_for(plan: EnvironmentPlan, entries: tuple[ManifestEntry, ...]) -> EnvironmentManifest:
    return EnvironmentManifest(
        plan_id=plan.plan_id,
        plan_digest=plan.plan_digest,
        project_id=plan.project_id,
        task_id=plan.task_id,
        requirement_digest=plan.requirement_digest,
        inventory_digest=plan.inventory_digest,
        source_head=plan.source_head,
        source_tree=plan.source_tree,
        platform_identity=plan.platform_identity,
        provisioning_adapter_version=plan.provisioning_adapter_version,
        components=_manifest_components(plan),
        entries=entries,
    )


class ProjectLocalEnvironmentProvisioner:
    """Deterministic project-local adapter with no global/shared side effects."""

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = _project_root(project_root)

    def environment_root(self, plan: EnvironmentPlan) -> Path:
        return _safe_project_child(self.project_root, plan.project_environment_relative_root, field_name="project_environment_relative_root")

    def _manifest_path(self, plan: EnvironmentPlan) -> Path:
        root = self.environment_root(plan)
        return root / "environment-manifest.json"

    def _load_manifest(self, plan: EnvironmentPlan) -> EnvironmentManifest | None:
        path = self._manifest_path(plan)
        if not path.exists():
            return None
        try:
            raw = _read_regular(path, field_name="environment manifest")
            document = json.loads(raw.decode("utf-8"))
            if not isinstance(document, Mapping):
                return None
            return EnvironmentManifest.from_dict(document)
        except (ProvisioningError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            return None

    def inspect(self, plan: EnvironmentPlan, *, current_source_head: str, current_source_tree: str) -> EnvironmentInspection:
        try:
            _source_sha(current_source_head, "current_source_head")
            _source_sha(current_source_tree, "current_source_tree")
            manifest = self._load_manifest(plan)
            if manifest is None:
                return EnvironmentInspection(False, None, "MANIFEST_MISSING_OR_INVALID")
            if manifest.plan_id != plan.plan_id or manifest.plan_digest != plan.plan_digest:
                return EnvironmentInspection(False, manifest, "PLAN_IDENTITY_MISMATCH")
            if manifest.source_head != current_source_head or manifest.source_tree != current_source_tree:
                return EnvironmentInspection(False, manifest, "SOURCE_IDENTITY_MISMATCH")
            if not self._manifest_matches_plan(manifest, plan):
                return EnvironmentInspection(False, manifest, "PLAN_CONTENT_MISMATCH")
            root = self.environment_root(plan)
            for entry in manifest.entries:
                payload = _read_regular(_safe_project_child(root, entry.relative_path, field_name="manifest entry"), field_name="manifest entry")
                if _sha256_bytes(payload) != entry.digest or len(payload) != entry.size_bytes:
                    return EnvironmentInspection(False, manifest, "MANIFEST_ENTRY_DIGEST_MISMATCH")
            expected_paths = {relative for _, relative, _ in _marker_payloads(plan)}
            if {entry.relative_path for entry in manifest.entries} != expected_paths:
                return EnvironmentInspection(False, manifest, "MANIFEST_ENTRY_SET_MISMATCH")
            return EnvironmentInspection(True, manifest, "ENVIRONMENT_READY")
        except ProvisioningError as exc:
            return EnvironmentInspection(False, None, exc.code)

    @staticmethod
    def _manifest_matches_plan(manifest: EnvironmentManifest, plan: EnvironmentPlan) -> bool:
        return (
            manifest.project_id == plan.project_id
            and manifest.task_id == plan.task_id
            and manifest.requirement_digest == plan.requirement_digest
            and manifest.inventory_digest == plan.inventory_digest
            and manifest.source_head == plan.source_head
            and manifest.source_tree == plan.source_tree
            and manifest.platform_identity == plan.platform_identity
            and manifest.provisioning_adapter_version == plan.provisioning_adapter_version
            and manifest.components == _manifest_components(plan)
        )

    @staticmethod
    def _offline_cache_miss(plan: EnvironmentPlan) -> bool:
        return any(
            request is not None and request.offline and not request.cache_available
            for request in (plan.python, plan.node, plan.rust)
        )

    def _result(
        self,
        plan: EnvironmentPlan,
        *,
        current_source_head: str,
        current_source_tree: str,
        status: EnvironmentStatus,
        approval_class: ApprovalClass,
        actual_effects: tuple[ActualEffect, ...] = (),
        created_or_reused_paths: tuple[str, ...] = (),
        final_readiness: bool = False,
        manifest_digest: str | None = None,
        diagnostics: tuple[str, ...] = (),
    ) -> EnvironmentResult:
        manifest_digests, lockfile_digests = _plan_digest_maps(plan)
        return EnvironmentResult(
            plan_id=plan.plan_id,
            plan_digest=plan.plan_digest,
            project_id=plan.project_id,
            task_id=plan.task_id,
            source_head=current_source_head,
            source_tree=current_source_tree,
            status=status,
            approval_class=approval_class,
            actual_effects=actual_effects,
            created_or_reused_paths=created_or_reused_paths,
            tool_identities=_tool_identities(plan),
            manifest_digests=manifest_digests,
            lockfile_digests=lockfile_digests,
            final_readiness=final_readiness,
            manifest_digest=manifest_digest,
            diagnostics=diagnostics,
        )

    def provision(
        self,
        plan: EnvironmentPlan,
        *,
        current_source_head: str,
        current_source_tree: str,
        fault: str | None = None,
    ) -> EnvironmentResult:
        if fault not in {None, "after_plan_prepared", "after_environment_created", "after_dependency_install", "before_manifest", "after_manifest"}:
            raise ProvisioningError("fault_invalid", "unsupported environment provisioning fault")
        _source_sha(current_source_head, "current_source_head")
        _source_sha(current_source_tree, "current_source_tree")
        if plan.source_head != current_source_head or plan.source_tree != current_source_tree:
            return self._result(
                plan,
                current_source_head=current_source_head,
                current_source_tree=current_source_tree,
                status=EnvironmentStatus.STALE_PLAN,
                approval_class=plan.approval_class,
                diagnostics=("STALE_ENVIRONMENT_PLAN",),
            )
        try:
            root = self.environment_root(plan)
            requested_safe = all(
                item.effect_class is EffectClass.SAFE_PROJECT_LOCAL_MUTATION
                and item.approval_class is ApprovalClass.SAFE_PROJECT_LOCAL_MUTATION
                for item in plan.requested_effects
            )
            plan_safe = plan.approval_class in {ApprovalClass.NO_MUTATION, ApprovalClass.SAFE_PROJECT_LOCAL_MUTATION}
        except ProvisioningError as exc:
            return self._result(
                plan,
                current_source_head=current_source_head,
                current_source_tree=current_source_tree,
                status=EnvironmentStatus.POLICY_DENIED,
                approval_class=ApprovalClass.POLICY_DENIED,
                diagnostics=(exc.code,),
            )
        inspection = self.inspect(plan, current_source_head=current_source_head, current_source_tree=current_source_tree)
        if inspection.ready and inspection.manifest is not None:
            paths = tuple(entry.relative_path for entry in inspection.manifest.entries)
            return self._result(
                plan,
                current_source_head=current_source_head,
                current_source_tree=current_source_tree,
                status=EnvironmentStatus.ALREADY_READY,
                approval_class=ApprovalClass.NO_MUTATION,
                created_or_reused_paths=paths,
                final_readiness=True,
                manifest_digest=inspection.manifest.manifest_digest,
                diagnostics=("EXACT_ENVIRONMENT_REUSED",),
            )
        if not plan_safe or not requested_safe:
            return self._result(
                plan,
                current_source_head=current_source_head,
                current_source_tree=current_source_tree,
                status=EnvironmentStatus.POLICY_DENIED,
                approval_class=ApprovalClass.POLICY_DENIED,
                diagnostics=("EFFECT_APPROVAL_REQUIRED",),
            )
        if self._offline_cache_miss(plan):
            return self._result(
                plan,
                current_source_head=current_source_head,
                current_source_tree=current_source_tree,
                status=EnvironmentStatus.OFFLINE_CACHE_MISS,
                approval_class=plan.approval_class,
                diagnostics=("OFFLINE_REQUIRED_ARTIFACT_NOT_AVAILABLE",),
            )
        if fault == "after_plan_prepared":
            raise ProvisioningInterrupted("crash_after_plan_prepared", "provisioning interrupted before project mutation")

        root_created = _ensure_directory(root, field_name="project environment root")
        effects: list[ActualEffect] = []
        if root_created:
            effects.append(
                ActualEffect(
                    component="environment",
                    effect_class=EffectClass.SAFE_PROJECT_LOCAL_MUTATION,
                    approval_class=ApprovalClass.SAFE_PROJECT_LOCAL_MUTATION,
                    target_relative_path=plan.project_environment_relative_root,
                    operation="CREATE_DIRECTORY",
                    artifact_digest=None,
                    source_head=current_source_head,
                    source_tree=current_source_tree,
                )
            )
        if fault == "after_environment_created":
            raise ProvisioningInterrupted("crash_after_environment_created", "provisioning interrupted after environment directory creation")

        entries: list[ManifestEntry] = []
        managed_paths: list[str] = []
        for component, relative, payload in _marker_payloads(plan):
            target = _safe_project_child(root, relative, field_name=f"{component} environment output")
            _ensure_directory(target.parent, field_name=f"{component} environment output parent")
            artifact_digest = _sha256_bytes(payload)
            operation = "CREATE"
            try:
                existing = _read_regular(target, field_name=f"{component} environment output")
            except ProvisioningError:
                existing = None
            if existing == payload:
                operation = "REUSE"
            else:
                _atomic_write(target, payload)
                operation = "CREATE" if existing is None else "UPDATE"
                effects.append(
                    ActualEffect(
                        component=component,
                        effect_class=EffectClass.SAFE_PROJECT_LOCAL_MUTATION,
                        approval_class=ApprovalClass.SAFE_PROJECT_LOCAL_MUTATION,
                        target_relative_path=f"{plan.project_environment_relative_root}/{relative}",
                        operation=operation,
                        artifact_digest=artifact_digest,
                        source_head=current_source_head,
                        source_tree=current_source_tree,
                    )
                )
            entries.append(ManifestEntry(relative, artifact_digest, len(payload)))
            managed_paths.append(f"{plan.project_environment_relative_root}/{relative}")

        if fault == "after_dependency_install":
            raise ProvisioningInterrupted("crash_after_dependency_install", "provisioning interrupted after dependency artifacts")
        if fault == "before_manifest":
            raise ProvisioningInterrupted("crash_before_manifest", "provisioning interrupted before manifest publication")

        manifest = _manifest_for(plan, tuple(entries))
        manifest_path = root / "environment-manifest.json"
        _atomic_write(manifest_path, canonical_json_bytes(manifest.to_dict()))
        effects.append(
            ActualEffect(
                component="environment",
                effect_class=EffectClass.SAFE_PROJECT_LOCAL_MUTATION,
                approval_class=ApprovalClass.SAFE_PROJECT_LOCAL_MUTATION,
                target_relative_path=f"{plan.project_environment_relative_root}/environment-manifest.json",
                operation="PUBLISH_MANIFEST",
                artifact_digest=manifest.manifest_digest,
                source_head=current_source_head,
                source_tree=current_source_tree,
            )
        )
        if fault == "after_manifest":
            raise ProvisioningInterrupted("crash_after_manifest", "provisioning interrupted after manifest publication")
        final = self.inspect(plan, current_source_head=current_source_head, current_source_tree=current_source_tree)
        if not final.ready or final.manifest is None:
            return self._result(
                plan,
                current_source_head=current_source_head,
                current_source_tree=current_source_tree,
                status=EnvironmentStatus.BLOCKED,
                approval_class=plan.approval_class,
                actual_effects=tuple(effects),
                created_or_reused_paths=tuple(managed_paths),
                diagnostics=("POST_PROVISION_MANIFEST_VERIFICATION_FAILED",),
            )
        status = EnvironmentStatus.REBUILT if inspection.manifest is not None else EnvironmentStatus.PROVISIONED
        return self._result(
            plan,
            current_source_head=current_source_head,
            current_source_tree=current_source_tree,
            status=status,
            approval_class=plan.approval_class,
            actual_effects=tuple(effects),
            created_or_reused_paths=tuple(managed_paths),
            final_readiness=True,
            manifest_digest=final.manifest.manifest_digest,
            diagnostics=("PROJECT_LOCAL_ENVIRONMENT_READY",),
        )


__all__ = [
    "ActualEffect",
    "ApprovalClass",
    "ENVIRONMENT_MANIFEST_SCHEMA",
    "ENVIRONMENT_MANIFEST_VERSION",
    "ENVIRONMENT_MANIFEST_VERSION_EXPLICIT",
    "ENVIRONMENT_PLAN_SCHEMA",
    "ENVIRONMENT_PLAN_VERSION",
    "ENVIRONMENT_PLAN_VERSION_EXPLICIT",
    "ENVIRONMENT_PROVISIONING_SCHEMA",
    "ENVIRONMENT_PROVISIONING_VERSION",
    "ENVIRONMENT_PROVISIONING_VERSION_EXPLICIT",
    "ENVIRONMENT_RESULT_SCHEMA",
    "ENVIRONMENT_RESULT_VERSION",
    "ENVIRONMENT_RESULT_VERSION_EXPLICIT",
    "EffectClass",
    "EnvironmentInspection",
    "EnvironmentManifest",
    "EnvironmentPlan",
    "EnvironmentResult",
    "EnvironmentStatus",
    "ManifestComponent",
    "ManifestEntry",
    "NodeEnvironmentRequest",
    "PackageIdentity",
    "PlatformIdentity",
    "ProjectLocalEnvironmentProvisioner",
    "ProvisioningError",
    "ProvisioningInterrupted",
    "PythonEnvironmentRequest",
    "RequestedEffect",
    "RustEnvironmentRequest",
    "ToolIdentity",
]

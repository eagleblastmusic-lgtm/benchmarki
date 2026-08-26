"""Fail-closed, drift-aware cache for NX-034 environment readiness.

The cache is a derived optimization.  A cache record can make an unchanged
resolution observable as a hit, but it can never make an environment ready:
each lookup resolves the current typed inventory through NX-034 first.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from bdb_shared.evidence import canonical_json_bytes

from .environment_requirements import (
    EnvironmentRequirementSet,
    READINESS_SCHEMA,
    READINESS_VERSION,
    ReadinessResult,
    ReadinessStatus,
    RequirementContractError,
    RequirementDisposition,
    RequirementResolution,
    resolve_requirements,
    task_start_allowed,
)
from .machine_inventory_contract import (
    FactStatus,
    MachineInventory,
    MachineInventoryContractError,
    require_valid_machine_inventory,
    VerificationDisposition,
)


CACHE_SCHEMA = "bdb-vnext-environment-cache-v1"
CACHE_VERSION = "v1"
ENVIRONMENT_CACHE_VERSION_EXPLICIT = True
DEFAULT_CACHE_TTL_SECONDS = 300
MAX_CACHE_BYTES = 2_000_000
CACHE_DIAGNOSTIC_REASONS = (
    "HIT",
    "MISS",
    "EXPIRED",
    "PATH_DRIFT",
    "TOOL_DRIFT",
    "MANIFEST_DRIFT",
    "LOCKFILE_DRIFT",
    "REQUIREMENT_DRIFT",
    "VERSION_MISMATCH",
    "CORRUPT",
    "NOT_READY",
    "RECORD_DIVERGENCE",
    "REFRESHED",
)

_DIGEST = r"^sha256:[0-9a-f]{64}$"
_IDENTITY = r"^[a-z][a-z0-9_.:-]*$"


class EnvironmentCacheError(ValueError):
    """Stable fail-closed error for cache contract or publication failures."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CacheWriteInterrupted(EnvironmentCacheError):
    """A testable crash boundary before or after atomic cache publication."""


class CacheLockError(EnvironmentCacheError):
    """The cache lock could not be acquired or safely released."""


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EnvironmentCacheError("text_required", f"{field_name} must be non-empty text")
    return value


def _digest(value: Any, field_name: str) -> str:
    text = _text(value, field_name)
    if re.fullmatch(_DIGEST, text) is None:
        raise EnvironmentCacheError("digest_invalid", f"{field_name} must be a sha256 digest")
    return text


def _strict_mapping(value: Any, fields: set[str], field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EnvironmentCacheError("mapping_required", f"{field_name} must be an object")
    unknown = sorted(str(key) for key in value if str(key) not in fields)
    if unknown:
        raise EnvironmentCacheError("unknown_field", f"{field_name} has unknown fields: {', '.join(unknown)}")
    return value


def _timestamp(value: Any, field_name: str) -> str:
    text = _text(value, field_name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EnvironmentCacheError("timestamp_invalid", f"{field_name} is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise EnvironmentCacheError("timestamp_timezone_missing", f"{field_name} needs a timezone")
    return text


def _utc_datetime(value: str | datetime, field_name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = _timestamp(value, field_name)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise EnvironmentCacheError("timestamp_timezone_missing", f"{field_name} needs a timezone")
    return parsed.astimezone(timezone.utc)


def _utc_text(value: str | datetime, field_name: str) -> str:
    return _utc_datetime(value, field_name).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _inventory_digest(inventory: MachineInventory) -> str:
    return _sha256(inventory.canonical_bytes(normalize_time=True))


def _normalise_digest_entries(value: Mapping[str, str] | None, field_name: str) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise EnvironmentCacheError("digest_map_invalid", f"{field_name} must be an object")
    entries: list[tuple[str, str]] = []
    for raw_name, raw_digest in value.items():
        name = _text(raw_name, f"{field_name}.name")
        entries.append((name, _digest(raw_digest, f"{field_name}.{name}")))
    return tuple(sorted(entries))


def _digest_entries_to_dict(entries: tuple[tuple[str, str], ...]) -> dict[str, str]:
    return {name: digest for name, digest in entries}


@dataclass(frozen=True)
class ExecutableIdentityKey:
    """The minimum version/path/hash identity for an observed tool fact."""

    fact_class: str
    subject: str
    status: str
    verification: str
    fact_version: str | None
    fact_path: str | None
    fact_digest: str | None
    executable_path: str | None
    executable_digest: str | None
    executable_version: str | None
    probe_id: str

    def __post_init__(self) -> None:
        _text(self.fact_class, "executable_identity.fact_class")
        _text(self.subject, "executable_identity.subject")
        _text(self.status, "executable_identity.status")
        _text(self.verification, "executable_identity.verification")
        _text(self.probe_id, "executable_identity.probe_id")
        for field_name, value in (
            ("fact_version", self.fact_version),
            ("fact_path", self.fact_path),
            ("fact_digest", self.fact_digest),
            ("executable_path", self.executable_path),
            ("executable_digest", self.executable_digest),
            ("executable_version", self.executable_version),
        ):
            if value is not None and not isinstance(value, str):
                raise EnvironmentCacheError("executable_identity_invalid", f"{field_name} must be text or null")
        if self.fact_digest is not None:
            _digest(self.fact_digest, "executable_identity.fact_digest")
        if self.executable_digest is not None:
            _digest(self.executable_digest, "executable_identity.executable_digest")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExecutableIdentityKey":
        fields = {
            "fact_class",
            "subject",
            "status",
            "verification",
            "fact_version",
            "fact_path",
            "fact_digest",
            "executable_path",
            "executable_digest",
            "executable_version",
            "probe_id",
        }
        data = _strict_mapping(value, fields, "executable_identity")
        return cls(
            fact_class=_text(data.get("fact_class"), "executable_identity.fact_class"),
            subject=_text(data.get("subject"), "executable_identity.subject"),
            status=_text(data.get("status"), "executable_identity.status"),
            verification=_text(data.get("verification"), "executable_identity.verification"),
            fact_version=data.get("fact_version"),
            fact_path=data.get("fact_path"),
            fact_digest=data.get("fact_digest"),
            executable_path=data.get("executable_path"),
            executable_digest=data.get("executable_digest"),
            executable_version=data.get("executable_version"),
            probe_id=_text(data.get("probe_id"), "executable_identity.probe_id"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "fact_class": self.fact_class,
            "subject": self.subject,
            "status": self.status,
            "verification": self.verification,
            "fact_version": self.fact_version,
            "fact_path": self.fact_path,
            "fact_digest": self.fact_digest,
            "executable_path": self.executable_path,
            "executable_digest": self.executable_digest,
            "executable_version": self.executable_version,
            "probe_id": self.probe_id,
        }


def _inventory_executable_identities(inventory: MachineInventory) -> tuple[ExecutableIdentityKey, ...]:
    identities: list[ExecutableIdentityKey] = []
    for fact in inventory.facts:
        if not fact.fact_class.startswith("tool.") and fact.executable is None:
            continue
        executable = fact.executable
        identities.append(
            ExecutableIdentityKey(
                fact_class=fact.fact_class,
                subject=fact.subject,
                status=fact.status.value,
                verification=fact.verification.value,
                fact_version=fact.version,
                fact_path=fact.resolved_path,
                fact_digest=fact.digest,
                executable_path=executable.resolved_path if executable is not None else None,
                executable_digest=executable.content_digest if executable is not None else None,
                executable_version=executable.reported_version if executable is not None else None,
                probe_id=executable.probe_id if executable is not None else fact.probe_id,
            )
        )
    return tuple(sorted(identities, key=lambda item: (item.fact_class, item.subject)))


@dataclass(frozen=True)
class CacheKey:
    """All identity inputs that can affect a readiness-cache reuse decision."""

    project_id: str
    task_id: str
    requirement_set_id: str
    requirement_digest: str
    inventory_schema: str
    inventory_version: str
    inventory_id: str
    inventory_digest: str
    path_digest: str
    executable_identities: tuple[ExecutableIdentityKey, ...]
    manifest_digests: tuple[tuple[str, str], ...]
    lockfile_digests: tuple[tuple[str, str], ...]
    source_identity_digest: str
    collector_version: str
    resolver_version: str
    schema: str = CACHE_SCHEMA
    version: str = CACHE_VERSION
    key_digest: str = field(default="", compare=True)

    def __post_init__(self) -> None:
        if self.schema != CACHE_SCHEMA or self.version != CACHE_VERSION:
            raise EnvironmentCacheError("cache_version_invalid", "unsupported cache key schema/version")
        for field_name, value in (
            ("project_id", self.project_id),
            ("task_id", self.task_id),
            ("requirement_set_id", self.requirement_set_id),
            ("inventory_schema", self.inventory_schema),
            ("inventory_version", self.inventory_version),
            ("inventory_id", self.inventory_id),
            ("collector_version", self.collector_version),
            ("resolver_version", self.resolver_version),
        ):
            _text(value, f"cache_key.{field_name}")
        for field_name, value in (
            ("requirement_digest", self.requirement_digest),
            ("inventory_digest", self.inventory_digest),
            ("path_digest", self.path_digest),
            ("source_identity_digest", self.source_identity_digest),
        ):
            _digest(value, f"cache_key.{field_name}")
        if any(not isinstance(item, ExecutableIdentityKey) for item in self.executable_identities):
            raise EnvironmentCacheError("executable_identity_invalid", "cache key executable identities must be typed")
        if tuple(sorted(self.executable_identities, key=lambda item: (item.fact_class, item.subject))) != self.executable_identities:
            raise EnvironmentCacheError("executable_identity_order_invalid", "cache key executable identities are not sorted")
        for field_name, entries in (("manifest_digests", self.manifest_digests), ("lockfile_digests", self.lockfile_digests)):
            if tuple(sorted(entries)) != entries or len({name for name, _ in entries}) != len(entries):
                raise EnvironmentCacheError("digest_map_order_invalid", f"cache_key.{field_name} is not canonical")
            for name, digest in entries:
                _text(name, f"cache_key.{field_name}.name")
                _digest(digest, f"cache_key.{field_name}.{name}")
        computed = _sha256(canonical_json_bytes(self._payload()))
        if self.key_digest and self.key_digest != computed:
            raise EnvironmentCacheError("key_digest_mismatch", "cache key digest does not match identity fields")
        object.__setattr__(self, "key_digest", computed)

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "requirement_set_id": self.requirement_set_id,
            "requirement_digest": self.requirement_digest,
            "inventory_schema": self.inventory_schema,
            "inventory_version": self.inventory_version,
            "inventory_id": self.inventory_id,
            "inventory_digest": self.inventory_digest,
            "path_digest": self.path_digest,
            "executable_identities": [item.to_dict() for item in self.executable_identities],
            "manifest_digests": _digest_entries_to_dict(self.manifest_digests),
            "lockfile_digests": _digest_entries_to_dict(self.lockfile_digests),
            "source_identity_digest": self.source_identity_digest,
            "collector_version": self.collector_version,
            "resolver_version": self.resolver_version,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["key_digest"] = self.key_digest
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CacheKey":
        fields = {
            "schema",
            "version",
            "project_id",
            "task_id",
            "requirement_set_id",
            "requirement_digest",
            "inventory_schema",
            "inventory_version",
            "inventory_id",
            "inventory_digest",
            "path_digest",
            "executable_identities",
            "manifest_digests",
            "lockfile_digests",
            "source_identity_digest",
            "collector_version",
            "resolver_version",
            "key_digest",
        }
        data = _strict_mapping(value, fields, "cache_key")
        raw_executables = data.get("executable_identities")
        if not isinstance(raw_executables, list) or any(not isinstance(item, Mapping) for item in raw_executables):
            raise EnvironmentCacheError("executable_identity_invalid", "cache_key.executable_identities must be an object list")
        def read_map(field_name: str) -> tuple[tuple[str, str], ...]:
            raw = data.get(field_name)
            if not isinstance(raw, Mapping):
                raise EnvironmentCacheError("digest_map_invalid", f"cache_key.{field_name} must be an object")
            return _normalise_digest_entries(raw, f"cache_key.{field_name}")
        return cls(
            schema=_text(data.get("schema"), "cache_key.schema"),
            version=_text(data.get("version"), "cache_key.version"),
            project_id=_text(data.get("project_id"), "cache_key.project_id"),
            task_id=_text(data.get("task_id"), "cache_key.task_id"),
            requirement_set_id=_text(data.get("requirement_set_id"), "cache_key.requirement_set_id"),
            requirement_digest=_digest(data.get("requirement_digest"), "cache_key.requirement_digest"),
            inventory_schema=_text(data.get("inventory_schema"), "cache_key.inventory_schema"),
            inventory_version=_text(data.get("inventory_version"), "cache_key.inventory_version"),
            inventory_id=_text(data.get("inventory_id"), "cache_key.inventory_id"),
            inventory_digest=_digest(data.get("inventory_digest"), "cache_key.inventory_digest"),
            path_digest=_digest(data.get("path_digest"), "cache_key.path_digest"),
            executable_identities=tuple(ExecutableIdentityKey.from_dict(item) for item in raw_executables),
            manifest_digests=read_map("manifest_digests"),
            lockfile_digests=read_map("lockfile_digests"),
            source_identity_digest=_digest(data.get("source_identity_digest"), "cache_key.source_identity_digest"),
            collector_version=_text(data.get("collector_version"), "cache_key.collector_version"),
            resolver_version=_text(data.get("resolver_version"), "cache_key.resolver_version"),
            key_digest=_digest(data.get("key_digest"), "cache_key.key_digest"),
        )


def build_cache_key(
    requirement_set: EnvironmentRequirementSet,
    inventory: MachineInventory,
    *,
    project_id: str = "project:bdb-vnext",
    task_id: str = "task:environment-readiness",
    current_path_digest: str | None = None,
    manifest_digests: Mapping[str, str] | None = None,
    lockfile_digests: Mapping[str, str] | None = None,
    source_identity_digest: str,
    collector_version: str,
    resolver_version: str,
) -> CacheKey:
    """Build a semantic key from typed current state and all drift identities."""

    if not isinstance(requirement_set, EnvironmentRequirementSet):
        raise EnvironmentCacheError("requirement_set_invalid", "cache key requires EnvironmentRequirementSet")
    try:
        typed_inventory = require_valid_machine_inventory(inventory)
    except (MachineInventoryContractError, ValueError, TypeError) as exc:
        raise EnvironmentCacheError("inventory_invalid", "cache key requires valid MachineInventory") from exc
    path_digest = current_path_digest or typed_inventory.path_identity.digest
    return CacheKey(
        project_id=_text(project_id, "project_id"),
        task_id=_text(task_id, "task_id"),
        requirement_set_id=requirement_set.set_id,
        requirement_digest=requirement_set.requirement_digest,
        inventory_schema=typed_inventory.schema,
        inventory_version=typed_inventory.version,
        inventory_id=typed_inventory.inventory_id,
        inventory_digest=_inventory_digest(typed_inventory),
        path_digest=_digest(path_digest, "current_path_digest"),
        executable_identities=_inventory_executable_identities(typed_inventory),
        manifest_digests=_normalise_digest_entries(manifest_digests, "manifest_digests"),
        lockfile_digests=_normalise_digest_entries(lockfile_digests, "lockfile_digests"),
        source_identity_digest=_digest(source_identity_digest, "source_identity_digest"),
        collector_version=_text(collector_version, "collector_version"),
        resolver_version=_text(resolver_version, "resolver_version"),
    )


def _readiness_from_dict(value: Mapping[str, Any]) -> ReadinessResult:
    fields = {
        "schema",
        "version",
        "status",
        "ready",
        "requirement_digest",
        "inventory_id",
        "inventory_digest",
        "inventory_freshness",
        "evaluated_at",
        "stale",
        "blocking_requirement_ids",
        "requirements",
        "explanation",
    }
    data = _strict_mapping(value, fields, "readiness")
    if data.get("schema") != READINESS_SCHEMA or data.get("version") != READINESS_VERSION:
        raise EnvironmentCacheError("readiness_version_invalid", "unsupported readiness schema/version")
    raw_blocking = data.get("blocking_requirement_ids")
    raw_requirements = data.get("requirements")
    if not isinstance(raw_blocking, list) or any(not isinstance(item, str) for item in raw_blocking):
        raise EnvironmentCacheError("readiness_shape_invalid", "readiness.blocking_requirement_ids must be a string list")
    if not isinstance(raw_requirements, list) or any(not isinstance(item, Mapping) for item in raw_requirements):
        raise EnvironmentCacheError("readiness_shape_invalid", "readiness.requirements must be an object list")
    resolutions: list[RequirementResolution] = []
    resolution_fields = {
        "requirement_id",
        "capability",
        "required",
        "disposition",
        "selected_capability",
        "observed_fact_class",
        "observed_version",
        "observed_path",
        "observed_digest",
        "blocking",
        "reason",
        "explanation",
        "requirement_digest",
        "inventory_id",
        "inventory_freshness",
    }
    for raw in raw_requirements:
        resolution = _strict_mapping(raw, resolution_fields, "readiness.requirement")
        resolutions.append(
            RequirementResolution(
                requirement_id=_text(resolution.get("requirement_id"), "resolution.requirement_id"),
                capability=_text(resolution.get("capability"), "resolution.capability"),
                required=resolution.get("required"),
                disposition=RequirementDisposition(resolution.get("disposition")),
                selected_capability=resolution.get("selected_capability"),
                observed_fact_class=resolution.get("observed_fact_class"),
                observed_version=resolution.get("observed_version"),
                observed_path=resolution.get("observed_path"),
                observed_digest=resolution.get("observed_digest"),
                blocking=resolution.get("blocking"),
                reason=_text(resolution.get("reason"), "resolution.reason"),
                explanation=_text(resolution.get("explanation"), "resolution.explanation"),
                requirement_digest=_digest(resolution.get("requirement_digest"), "resolution.requirement_digest"),
                inventory_id=_text(resolution.get("inventory_id"), "resolution.inventory_id"),
                inventory_freshness=_text(resolution.get("inventory_freshness"), "resolution.inventory_freshness"),
            )
        )
    try:
        status = ReadinessStatus(data.get("status"))
    except ValueError as exc:
        raise EnvironmentCacheError("readiness_status_invalid", "readiness.status is unsupported") from exc
    if not isinstance(data.get("ready"), bool) or not isinstance(data.get("stale"), bool):
        raise EnvironmentCacheError("readiness_shape_invalid", "readiness.ready and readiness.stale must be boolean")
    result = ReadinessResult(
        schema=_text(data.get("schema"), "readiness.schema"),
        version=_text(data.get("version"), "readiness.version"),
        status=status,
        requirement_digest=_digest(data.get("requirement_digest"), "readiness.requirement_digest"),
        inventory_id=_text(data.get("inventory_id"), "readiness.inventory_id"),
        inventory_digest=_digest(data.get("inventory_digest"), "readiness.inventory_digest"),
        inventory_freshness=_text(data.get("inventory_freshness"), "readiness.inventory_freshness"),
        evaluated_at=_timestamp(data.get("evaluated_at"), "readiness.evaluated_at"),
        stale=data.get("stale"),
        blocking_requirement_ids=tuple(raw_blocking),
        requirements=tuple(resolutions),
        explanation=_text(data.get("explanation"), "readiness.explanation"),
    )
    if data.get("ready") != result.ready:
        raise EnvironmentCacheError("readiness_ready_mismatch", "readiness.ready does not match status")
    if result.ready and result.blocking_requirement_ids:
        raise EnvironmentCacheError("readiness_blocking_mismatch", "ready readiness cannot have blocking requirements")
    return result


@dataclass(frozen=True)
class CacheRecord:
    key: CacheKey
    readiness: ReadinessResult
    created_at: str
    expires_at: str
    generation: int
    schema: str = CACHE_SCHEMA
    version: str = CACHE_VERSION
    record_digest: str = field(default="", compare=True)

    def __post_init__(self) -> None:
        if self.schema != CACHE_SCHEMA or self.version != CACHE_VERSION:
            raise EnvironmentCacheError("cache_version_invalid", "unsupported cache record schema/version")
        if not isinstance(self.key, CacheKey) or not isinstance(self.readiness, ReadinessResult):
            raise EnvironmentCacheError("cache_record_shape_invalid", "cache record key/readiness must be typed")
        _timestamp(self.created_at, "cache_record.created_at")
        _timestamp(self.expires_at, "cache_record.expires_at")
        if _utc_datetime(self.expires_at, "cache_record.expires_at") <= _utc_datetime(self.created_at, "cache_record.created_at"):
            raise EnvironmentCacheError("cache_ttl_invalid", "cache record expiry must follow creation")
        if isinstance(self.generation, bool) or not isinstance(self.generation, int) or self.generation <= 0:
            raise EnvironmentCacheError("cache_generation_invalid", "cache record generation must be positive")
        if self.readiness.requirement_digest != self.key.requirement_digest:
            raise EnvironmentCacheError("cache_requirement_mismatch", "cache record requirement identity diverges")
        if self.readiness.inventory_id != self.key.inventory_id or self.readiness.inventory_digest != self.key.inventory_digest:
            raise EnvironmentCacheError("cache_inventory_mismatch", "cache record inventory identity diverges")
        computed = _sha256(canonical_json_bytes(self._payload()))
        if self.record_digest and self.record_digest != computed:
            raise EnvironmentCacheError("record_digest_mismatch", "cache record digest does not match content")
        object.__setattr__(self, "record_digest", computed)

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "key": self.key.to_dict(),
            "readiness": self.readiness.to_dict(),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "generation": self.generation,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["record_digest"] = self.record_digest
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CacheRecord":
        fields = {"schema", "version", "key", "readiness", "created_at", "expires_at", "generation", "record_digest"}
        data = _strict_mapping(value, fields, "cache_record")
        if not isinstance(data.get("key"), Mapping) or not isinstance(data.get("readiness"), Mapping):
            raise EnvironmentCacheError("cache_record_shape_invalid", "cache record key/readiness must be objects")
        return cls(
            schema=_text(data.get("schema"), "cache_record.schema"),
            version=_text(data.get("version"), "cache_record.version"),
            key=CacheKey.from_dict(data["key"]),
            readiness=_readiness_from_dict(data["readiness"]),
            created_at=_timestamp(data.get("created_at"), "cache_record.created_at"),
            expires_at=_timestamp(data.get("expires_at"), "cache_record.expires_at"),
            generation=data.get("generation"),
            record_digest=_digest(data.get("record_digest"), "cache_record.record_digest"),
        )


@dataclass(frozen=True)
class CacheLookup:
    reason: str
    hit: bool
    readiness: ReadinessResult
    key: CacheKey
    used_cached_readiness: bool = False
    record_generation: int | None = None

    def __post_init__(self) -> None:
        if self.reason not in CACHE_DIAGNOSTIC_REASONS:
            raise EnvironmentCacheError("cache_reason_invalid", "unsupported cache lookup diagnostic")
        if not isinstance(self.hit, bool) or not isinstance(self.used_cached_readiness, bool):
            raise EnvironmentCacheError("cache_lookup_shape_invalid", "cache lookup flags must be boolean")
        if self.hit and self.reason != "HIT":
            raise EnvironmentCacheError("cache_lookup_shape_invalid", "only HIT can report a cache hit")
        if self.used_cached_readiness:
            raise EnvironmentCacheError("cache_authority_violation", "cached readiness cannot become readiness authority")


class OwnedCacheLock:
    """Exclusive lock record whose owner is required for release."""

    def __init__(self, path: str | Path, *, owner_token: str | None = None) -> None:
        self.path = Path(path)
        self.owner_token = owner_token or uuid.uuid4().hex
        _text(self.owner_token, "lock.owner_token")
        self._acquired = False

    def acquire(self, *, timeout_seconds: float = 2.0, poll_seconds: float = 0.01) -> bool:
        if timeout_seconds < 0 or poll_seconds <= 0:
            raise CacheLockError("lock_timeout_invalid", "lock timeout and poll interval must be positive")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = canonical_json_bytes(
            {
                "schema": "bdb-vnext-environment-cache-lock-v1",
                "owner_token": self.owner_token,
            }
        )
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                with self.path.open("xb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._acquired = True
                return True
            except FileExistsError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(poll_seconds)
            except OSError as exc:
                raise CacheLockError("lock_acquire_failed", "cache lock could not be created") from exc

    def release(self) -> bool:
        if not self._acquired:
            return False
        try:
            with self.path.open("rb") as handle:
                raw = handle.read(16_384)
            document = json.loads(raw.decode("utf-8"))
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            return False
        if not isinstance(document, Mapping) or document.get("owner_token") != self.owner_token:
            return False
        try:
            self.path.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise CacheLockError("lock_release_failed", "owned cache lock could not be removed") from exc
        self._acquired = False
        return True

    def __enter__(self) -> "OwnedCacheLock":
        if not self.acquire():
            raise CacheLockError("lock_busy", "cache lock is held by another owner")
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.release()


def _drift_reason(previous: CacheKey, current: CacheKey) -> str | None:
    if previous.path_digest != current.path_digest:
        return "PATH_DRIFT"
    if previous.executable_identities != current.executable_identities:
        return "TOOL_DRIFT"
    if previous.manifest_digests != current.manifest_digests:
        return "MANIFEST_DRIFT"
    if previous.lockfile_digests != current.lockfile_digests:
        return "LOCKFILE_DRIFT"
    if previous.requirement_digest != current.requirement_digest or previous.requirement_set_id != current.requirement_set_id:
        return "REQUIREMENT_DRIFT"
    if (
        previous.project_id != current.project_id
        or previous.task_id != current.task_id
        or previous.inventory_schema != current.inventory_schema
        or previous.inventory_version != current.inventory_version
        or previous.inventory_id != current.inventory_id
        or previous.inventory_digest != current.inventory_digest
        or previous.source_identity_digest != current.source_identity_digest
        or previous.collector_version != current.collector_version
        or previous.resolver_version != current.resolver_version
    ):
        return "VERSION_MISMATCH"
    return None


def _atomic_publish(path: Path, document: Mapping[str, Any], *, fault: str | None = None) -> None:
    if fault not in {None, "before_publish", "after_publish"}:
        raise EnvironmentCacheError("fault_invalid", "unsupported cache publication fault")
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.parent / f".{path.name}.partial-{uuid.uuid4().hex}"
    try:
        with staging.open("xb") as handle:
            handle.write(canonical_json_bytes(document))
            handle.flush()
            os.fsync(handle.fileno())
        if fault == "before_publish":
            raise CacheWriteInterrupted("crash_before_publish", "cache write interrupted before atomic publication")
        os.replace(staging, path)
        if fault == "after_publish":
            raise CacheWriteInterrupted("crash_after_publish", "cache write interrupted after atomic publication")
    except CacheWriteInterrupted:
        raise
    except OSError as exc:
        raise EnvironmentCacheError("cache_publish_failed", "cache record publication failed") from exc
    finally:
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            pass


class EnvironmentReadinessCache:
    """File-backed cache with atomic writes and fail-closed current resolution."""

    def __init__(
        self,
        path: str | Path,
        *,
        project_id: str = "project:bdb-vnext",
        task_id: str = "task:environment-readiness",
        source_identity_digest: str,
        collector_version: str = "bdb-vnext-machine-inventory-v1",
        resolver_version: str = "bdb-vnext-environment-resolver-v1",
        lock_timeout_seconds: float = 2.0,
    ) -> None:
        self.path = Path(path)
        self.lock_path = Path(str(self.path) + ".lock")
        self.project_id = _text(project_id, "project_id")
        self.task_id = _text(task_id, "task_id")
        self.source_identity_digest = _digest(source_identity_digest, "source_identity_digest")
        self.collector_version = _text(collector_version, "collector_version")
        self.resolver_version = _text(resolver_version, "resolver_version")
        if lock_timeout_seconds < 0:
            raise CacheLockError("lock_timeout_invalid", "lock timeout cannot be negative")
        self.lock_timeout_seconds = lock_timeout_seconds
        self.refresh_attempts = 0
        self.corrupt_reads = 0

    def key_for(
        self,
        requirement_set: EnvironmentRequirementSet,
        inventory: MachineInventory,
        *,
        current_path_digest: str | None = None,
        manifest_digests: Mapping[str, str] | None = None,
        lockfile_digests: Mapping[str, str] | None = None,
        source_identity_digest: str | None = None,
        collector_version: str | None = None,
        resolver_version: str | None = None,
    ) -> CacheKey:
        return build_cache_key(
            requirement_set,
            inventory,
            project_id=self.project_id,
            task_id=self.task_id,
            current_path_digest=current_path_digest,
            manifest_digests=manifest_digests,
            lockfile_digests=lockfile_digests,
            source_identity_digest=source_identity_digest or self.source_identity_digest,
            collector_version=collector_version or self.collector_version,
            resolver_version=resolver_version or self.resolver_version,
        )

    @staticmethod
    def _current_readiness(
        requirement_set: EnvironmentRequirementSet,
        inventory: MachineInventory,
        *,
        evaluated_at: str,
        current_path_digest: str | None,
    ) -> ReadinessResult:
        try:
            typed_inventory = require_valid_machine_inventory(inventory)
        except (MachineInventoryContractError, ValueError, TypeError) as exc:
            raise EnvironmentCacheError("inventory_invalid", "cache lookup requires valid MachineInventory") from exc
        return resolve_requirements(
            requirement_set,
            typed_inventory,
            evaluated_at=evaluated_at,
            current_path_digest=current_path_digest or typed_inventory.path_identity.digest,
        )

    def _read_record(self) -> CacheRecord:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError as exc:
            raise EnvironmentCacheError("cache_missing", "cache record is absent") from exc
        except OSError as exc:
            raise EnvironmentCacheError("cache_read_failed", "cache record could not be read") from exc
        if len(raw) > MAX_CACHE_BYTES:
            raise EnvironmentCacheError("cache_too_large", "cache record exceeds bounded read size")
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EnvironmentCacheError("cache_corrupt", "cache record is not valid JSON") from exc
        if not isinstance(document, Mapping):
            raise EnvironmentCacheError("cache_corrupt", "cache record must be a JSON object")
        return CacheRecord.from_dict(document)

    def lookup(
        self,
        requirement_set: EnvironmentRequirementSet,
        inventory: MachineInventory,
        *,
        now: str | datetime,
        current_path_digest: str | None = None,
        manifest_digests: Mapping[str, str] | None = None,
        lockfile_digests: Mapping[str, str] | None = None,
        source_identity_digest: str | None = None,
        collector_version: str | None = None,
        resolver_version: str | None = None,
    ) -> CacheLookup:
        now_text = _utc_text(now, "lookup.now")
        key = self.key_for(
            requirement_set,
            inventory,
            current_path_digest=current_path_digest,
            manifest_digests=manifest_digests,
            lockfile_digests=lockfile_digests,
            source_identity_digest=source_identity_digest,
            collector_version=collector_version,
            resolver_version=resolver_version,
        )
        readiness = self._current_readiness(
            requirement_set,
            inventory,
            evaluated_at=now_text,
            current_path_digest=current_path_digest,
        )
        try:
            record = self._read_record()
        except EnvironmentCacheError as exc:
            if exc.code == "cache_missing":
                return CacheLookup("MISS", False, readiness, key)
            if exc.code == "cache_version_invalid" or exc.code == "readiness_version_invalid":
                return CacheLookup("VERSION_MISMATCH", False, readiness, key)
            self.corrupt_reads += 1
            return CacheLookup("CORRUPT", False, readiness, key)
        reason = _drift_reason(record.key, key)
        if reason is not None:
            return CacheLookup(reason, False, readiness, key, record_generation=record.generation)
        if _utc_datetime(now_text, "lookup.now") >= _utc_datetime(record.expires_at, "cache_record.expires_at"):
            return CacheLookup("EXPIRED", False, readiness, key, record_generation=record.generation)
        if record.readiness.canonical_bytes(normalize_time=True) != readiness.canonical_bytes(normalize_time=True):
            return CacheLookup("RECORD_DIVERGENCE", False, readiness, key, record_generation=record.generation)
        if not task_start_allowed(readiness):
            return CacheLookup("NOT_READY", False, readiness, key, record_generation=record.generation)
        return CacheLookup("HIT", True, readiness, key, record_generation=record.generation)

    def refresh(
        self,
        requirement_set: EnvironmentRequirementSet,
        inventory: MachineInventory,
        *,
        now: str | datetime,
        ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
        current_path_digest: str | None = None,
        manifest_digests: Mapping[str, str] | None = None,
        lockfile_digests: Mapping[str, str] | None = None,
        source_identity_digest: str | None = None,
        collector_version: str | None = None,
        resolver_version: str | None = None,
        fault: str | None = None,
    ) -> CacheLookup:
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
            raise EnvironmentCacheError("cache_ttl_invalid", "cache TTL must be a positive integer")
        self.refresh_attempts += 1
        now_text = _utc_text(now, "refresh.now")
        key = self.key_for(
            requirement_set,
            inventory,
            current_path_digest=current_path_digest,
            manifest_digests=manifest_digests,
            lockfile_digests=lockfile_digests,
            source_identity_digest=source_identity_digest,
            collector_version=collector_version,
            resolver_version=resolver_version,
        )
        readiness = self._current_readiness(
            requirement_set,
            inventory,
            evaluated_at=now_text,
            current_path_digest=current_path_digest,
        )
        with OwnedCacheLock(self.lock_path) as _lock:
            generation = 1
            try:
                previous = self._read_record()
                generation = previous.generation + 1
            except EnvironmentCacheError:
                pass
            expires_at = _utc_text(_utc_datetime(now_text, "refresh.now") + timedelta(seconds=ttl_seconds), "cache_record.expires_at")
            record = CacheRecord(
                key=key,
                readiness=readiness,
                created_at=now_text,
                expires_at=expires_at,
                generation=generation,
            )
            _atomic_publish(self.path, record.to_dict(), fault=fault)
        return CacheLookup("REFRESHED", False, readiness, key, record_generation=record.generation)


__all__ = [
    "CACHE_DIAGNOSTIC_REASONS",
    "CACHE_SCHEMA",
    "CACHE_VERSION",
    "CacheKey",
    "CacheLockError",
    "CacheLookup",
    "CacheRecord",
    "CacheWriteInterrupted",
    "DEFAULT_CACHE_TTL_SECONDS",
    "ENVIRONMENT_CACHE_VERSION_EXPLICIT",
    "EnvironmentCacheError",
    "EnvironmentReadinessCache",
    "ExecutableIdentityKey",
    "OwnedCacheLock",
    "build_cache_key",
]

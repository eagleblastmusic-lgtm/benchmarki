"""NX-032 typed Machine Inventory v1 contract.

This module defines the data contract consumed by the future NX-033
collectors.  It deliberately performs no machine probing, process execution,
PATH mutation, registry mutation, installation, or workflow-state mutation.
"""

from __future__ import annotations

import hashlib
import json
import ntpath
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Sequence

from bdb_shared.evidence import canonical_json_bytes


MACHINE_INVENTORY_SCHEMA = "bdb-vnext-machine-inventory-v1"
MACHINE_INVENTORY_VERSION = "v1"
MACHINE_INVENTORY_VERSION_EXPLICIT = True
PATH_CANONICALIZATION_VERSION = "v1"
PROBE_REGISTRY_SCHEMA = "bdb-vnext-machine-probe-registry-v1"
PROBE_REGISTRY_VERSION = "v1"
PROBE_REGISTRY_VERSION_EXPLICIT = True
REDACTION_POLICY_VERSION = "bdb-vnext-machine-redaction-v1"


class _ValueEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class FactStatus(_ValueEnum):
    AVAILABLE = "AVAILABLE"
    MISSING = "MISSING"
    UNVERIFIABLE = "UNVERIFIABLE"
    MALFORMED = "MALFORMED"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"


class InventorySource(_ValueEnum):
    OS_API = "OS_API"
    ENVIRONMENT = "ENVIRONMENT"
    REGISTRY = "REGISTRY"
    FILESYSTEM = "FILESYSTEM"
    COMMAND = "COMMAND"
    FIXTURE = "FIXTURE"


class VerificationDisposition(_ValueEnum):
    VERIFIED = "VERIFIED"
    DECLARED = "DECLARED"
    UNVERIFIED = "UNVERIFIED"
    REJECTED = "REJECTED"


class Confidence(_ValueEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    NONE = "NONE"


class ProbeSourceKind(_ValueEnum):
    OS_API = "OS_API"
    ENVIRONMENT = "ENVIRONMENT"
    REGISTRY = "REGISTRY"
    FILESYSTEM = "FILESYSTEM"
    COMMAND = "COMMAND"


class ProbeTimeoutClass(_ValueEnum):
    NONE = "NONE"
    SHORT = "SHORT"
    MEDIUM = "MEDIUM"
    LONG = "LONG"


class ProbeSensitivity(_ValueEnum):
    PUBLIC = "PUBLIC"
    SENSITIVE = "SENSITIVE"
    SECRET = "SECRET"


REQUIRED_FACT_CLASSES: tuple[str, ...] = (
    "os_identity",
    "windows_build",
    "architecture",
    "path_identity",
    "tool.node",
    "tool.npm",
    "tool.git",
    "tool.python",
    "tool.rust",
    "tool.rustup",
    "tool.cargo",
    "tool.pwsh",
    "tool.windows_powershell",
    "tool.msvc",
    "tool.windows_sdk",
    "tool.webview2",
)

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_FACT_CLASS = re.compile(r"^[a-z][a-z0-9_.-]*$")
_WINDOWS_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")
_SECRET_KEY = re.compile(
    r"(?i)(?:token|access[_-]?token|api[_-]?key|password|passwd|secret|authorization|bearer|cookie|session(?:[_-]?id)?|private[_-]?key)"
)
_SECRET_VALUE = re.compile(
    r"(?is)(?:\bbearer\s+[^\s,;]+|-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----|(?:^|[;\s])(?:session(?:id)?|sid|phpsessid)=[^;\s]+)"
)
_SHELL_META = re.compile(r"[&|<>;$()\r\n`]")


class MachineInventoryContractError(ValueError):
    """A typed contract violation with a stable, non-secret error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class MachineInventoryValidationError(MachineInventoryContractError):
    """Raised by ``require_valid_machine_inventory`` for invalid input."""

    def __init__(self, errors: Sequence[str]) -> None:
        normalized = tuple(str(item) for item in errors)
        super().__init__("inventory_invalid", "; ".join(normalized))
        self.errors = normalized


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    errors: tuple[str, ...] = ()


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MachineInventoryContractError("text_required", f"{field} must be non-empty text")
    return value


def _timestamp(value: Any, field: str) -> str:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MachineInventoryContractError("timestamp_invalid", f"{field} is not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise MachineInventoryContractError("timestamp_timezone_missing", f"{field} must include a timezone")
    return text


def _strict_mapping(value: Any, fields: set[str], field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MachineInventoryContractError("mapping_required", f"{field} must be an object")
    actual = {str(key) for key in value}
    unknown = sorted(actual - fields)
    if unknown:
        raise MachineInventoryContractError("unknown_field", f"{field} has unknown fields: {', '.join(unknown)}")
    return value


def _enum(value: Any, enum_type: type[_ValueEnum], field: str) -> _ValueEnum:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise MachineInventoryContractError("enum_invalid", f"{field} has an invalid disposition") from exc


def _digest(value: Any, field: str) -> str:
    text = _text(value, field)
    if not _DIGEST.fullmatch(text):
        raise MachineInventoryContractError("digest_invalid", f"{field} must be a sha256 digest")
    return text


def _canonical_windows_path_entry(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MachineInventoryContractError("path_entry_invalid", "PATH contains an empty or non-text entry")
    normalized = ntpath.normpath(value.strip().replace("/", "\\"))
    if re.fullmatch(r"[A-Za-z]:", normalized):
        normalized += "\\"
    return normalized.casefold()


def _path_digest(entries: Sequence[str]) -> str:
    payload = {
        "case_semantics": "WINDOWS_CASE_INSENSITIVE",
        "entries": list(entries),
        "separator": "\\",
        "version": PATH_CANONICALIZATION_VERSION,
    }
    return "sha256:" + hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


@dataclass(frozen=True)
class PathIdentity:
    """Canonical Windows PATH identity without retaining the raw environment."""

    entries: tuple[str, ...]
    duplicate_entries: tuple[str, ...]
    digest: str
    canonicalization_version: str = PATH_CANONICALIZATION_VERSION
    case_semantics: str = "WINDOWS_CASE_INSENSITIVE"
    separator: str = "\\"
    duplicate_policy: str = "FIRST_WINS"

    @classmethod
    def from_entries(cls, entries: Sequence[str]) -> "PathIdentity":
        canonical: list[str] = []
        duplicates: list[str] = []
        seen: set[str] = set()
        for raw in entries:
            value = _canonical_windows_path_entry(raw)
            if value in seen:
                if value not in duplicates:
                    duplicates.append(value)
                continue
            seen.add(value)
            canonical.append(value)
        return cls(
            entries=tuple(canonical),
            duplicate_entries=tuple(sorted(duplicates)),
            digest=_path_digest(canonical),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PathIdentity":
        fields = {
            "entries",
            "duplicate_entries",
            "digest",
            "canonicalization_version",
            "case_semantics",
            "separator",
            "duplicate_policy",
        }
        data = _strict_mapping(value, fields, "path_identity")
        entries = data.get("entries")
        duplicates = data.get("duplicate_entries")
        if not isinstance(entries, list) or any(not isinstance(item, str) for item in entries):
            raise MachineInventoryContractError("path_entries_invalid", "path_identity.entries must be a string list")
        if not isinstance(duplicates, list) or any(not isinstance(item, str) for item in duplicates):
            raise MachineInventoryContractError("path_duplicates_invalid", "path_identity.duplicate_entries must be a string list")
        canonical_entries = tuple(_canonical_windows_path_entry(item) for item in entries)
        canonical_duplicates = tuple(sorted({_canonical_windows_path_entry(item) for item in duplicates}))
        if canonical_entries != tuple(entries):
            raise MachineInventoryContractError("path_entries_not_canonical", "path_identity.entries are not canonical")
        if canonical_duplicates != tuple(duplicates):
            raise MachineInventoryContractError("path_duplicates_not_canonical", "path_identity.duplicate_entries are not canonical")
        result = cls(
            entries=canonical_entries,
            duplicate_entries=canonical_duplicates,
            digest=_digest(data.get("digest"), "path_identity.digest"),
            canonicalization_version=_text(data.get("canonicalization_version"), "path_identity.canonicalization_version"),
            case_semantics=_text(data.get("case_semantics"), "path_identity.case_semantics"),
            separator=_text(data.get("separator"), "path_identity.separator"),
            duplicate_policy=_text(data.get("duplicate_policy"), "path_identity.duplicate_policy"),
        )
        if result.canonicalization_version != PATH_CANONICALIZATION_VERSION:
            raise MachineInventoryContractError("path_version_invalid", "path identity version is unsupported")
        if result.case_semantics != "WINDOWS_CASE_INSENSITIVE" or result.separator != "\\":
            raise MachineInventoryContractError("path_semantics_invalid", "PATH identity is not Windows-canonical")
        if result.duplicate_policy != "FIRST_WINS":
            raise MachineInventoryContractError("path_duplicate_policy_invalid", "PATH duplicate policy is unsupported")
        if tuple(result.entries) != tuple(dict.fromkeys(result.entries)):
            raise MachineInventoryContractError("path_duplicates_unresolved", "canonical PATH entries still contain duplicates")
        if result.digest != _path_digest(result.entries):
            raise MachineInventoryContractError("path_digest_mismatch", "PATH digest does not match canonical entries")
        if tuple(sorted(set(result.duplicate_entries))) != result.duplicate_entries:
            raise MachineInventoryContractError("path_duplicate_order_invalid", "PATH duplicate evidence is not deterministic")
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "entries": list(self.entries),
            "duplicate_entries": list(self.duplicate_entries),
            "digest": self.digest,
            "canonicalization_version": self.canonicalization_version,
            "case_semantics": self.case_semantics,
            "separator": self.separator,
            "duplicate_policy": self.duplicate_policy,
        }


@dataclass(frozen=True)
class ExecutableIdentity:
    """Exact executable identity reserved for NX-033 observations."""

    resolved_path: str | None
    content_digest: str | None
    reported_version: str | None
    source: InventorySource
    probe_id: str
    status: FactStatus
    verification: VerificationDisposition

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", InventorySource(self.source))
        object.__setattr__(self, "status", FactStatus(self.status))
        object.__setattr__(self, "verification", VerificationDisposition(self.verification))
        _text(self.probe_id, "executable.probe_id")
        if self.resolved_path is not None and not _WINDOWS_ABSOLUTE.match(self.resolved_path):
            raise MachineInventoryContractError("executable_path_not_exact", "executable.resolved_path must be absolute")
        if self.content_digest is not None:
            _digest(self.content_digest, "executable.content_digest")
        if self.status is FactStatus.AVAILABLE and self.resolved_path is None:
            raise MachineInventoryContractError("executable_path_required", "AVAILABLE executable identity needs an exact path")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExecutableIdentity":
        fields = {
            "resolved_path",
            "content_digest",
            "reported_version",
            "source",
            "probe_id",
            "status",
            "verification",
        }
        data = _strict_mapping(value, fields, "executable")
        return cls(
            resolved_path=data.get("resolved_path"),
            content_digest=data.get("content_digest"),
            reported_version=data.get("reported_version"),
            source=_enum(data.get("source"), InventorySource, "executable.source"),  # type: ignore[arg-type]
            probe_id=_text(data.get("probe_id"), "executable.probe_id"),
            status=_enum(data.get("status"), FactStatus, "executable.status"),  # type: ignore[arg-type]
            verification=_enum(data.get("verification"), VerificationDisposition, "executable.verification"),  # type: ignore[arg-type]
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "resolved_path": self.resolved_path,
            "content_digest": self.content_digest,
            "reported_version": self.reported_version,
            "source": self.source.value,
            "probe_id": self.probe_id,
            "status": self.status.value,
            "verification": self.verification.value,
        }


@dataclass(frozen=True)
class InventoryFact:
    fact_class: str
    subject: str
    status: FactStatus
    source: InventorySource
    collected_at: str
    confidence: Confidence
    verification: VerificationDisposition
    probe_id: str
    value: Mapping[str, Any] | None = None
    resolved_path: str | None = None
    version: str | None = None
    digest: str | None = None
    executable: ExecutableIdentity | None = None
    details: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.fact_class, str) or not _FACT_CLASS.fullmatch(self.fact_class):
            raise MachineInventoryContractError("fact_class_invalid", "fact_class has invalid identity")
        _text(self.subject, "fact.subject")
        object.__setattr__(self, "status", FactStatus(self.status))
        object.__setattr__(self, "source", InventorySource(self.source))
        object.__setattr__(self, "confidence", Confidence(self.confidence))
        object.__setattr__(self, "verification", VerificationDisposition(self.verification))
        _timestamp(self.collected_at, "fact.collected_at")
        _text(self.probe_id, "fact.probe_id")
        if self.resolved_path is not None and not _WINDOWS_ABSOLUTE.match(self.resolved_path):
            raise MachineInventoryContractError("fact_path_not_exact", "fact.resolved_path must be absolute")
        if self.digest is not None:
            _digest(self.digest, "fact.digest")
        if self.status is FactStatus.AVAILABLE and self.verification in {
            VerificationDisposition.UNVERIFIED,
            VerificationDisposition.REJECTED,
        }:
            raise MachineInventoryContractError("available_unverified", "AVAILABLE fact cannot be unverified or rejected")
        if self.status is FactStatus.UNVERIFIABLE and self.verification is not VerificationDisposition.UNVERIFIED:
            raise MachineInventoryContractError("unverifiable_disposition_invalid", "UNVERIFIABLE fact needs UNVERIFIED disposition")
        if self.status is FactStatus.MALFORMED and self.verification not in {
            VerificationDisposition.REJECTED,
            VerificationDisposition.UNVERIFIED,
        }:
            raise MachineInventoryContractError("malformed_disposition_invalid", "MALFORMED fact needs rejected/unverified disposition")
        if self.status is FactStatus.MISSING and (
            self.resolved_path is not None or self.executable is not None
        ):
            raise MachineInventoryContractError("missing_identity_fabricated", "MISSING fact cannot claim an executable identity")
        for field_name, field_value in (("fact.value", self.value), ("fact.details", self.details)):
            if field_value is not None and not isinstance(field_value, Mapping):
                raise MachineInventoryContractError("fact_object_required", f"{field_name} must be an object")
            if field_value is not None:
                try:
                    json.dumps(field_value, ensure_ascii=False, sort_keys=True)
                except (TypeError, ValueError) as exc:
                    raise MachineInventoryContractError("fact_value_not_json", f"{field_name} must be JSON-compatible") from exc

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "InventoryFact":
        fields = {
            "fact_class",
            "subject",
            "status",
            "source",
            "collected_at",
            "confidence",
            "verification",
            "probe_id",
            "value",
            "resolved_path",
            "version",
            "digest",
            "executable",
            "details",
        }
        data = _strict_mapping(value, fields, "fact")
        executable = data.get("executable")
        if executable is not None and not isinstance(executable, Mapping):
            raise MachineInventoryContractError("executable_object_required", "fact.executable must be an object")
        return cls(
            fact_class=_text(data.get("fact_class"), "fact.fact_class"),
            subject=_text(data.get("subject"), "fact.subject"),
            status=_enum(data.get("status"), FactStatus, "fact.status"),  # type: ignore[arg-type]
            source=_enum(data.get("source"), InventorySource, "fact.source"),  # type: ignore[arg-type]
            collected_at=_timestamp(data.get("collected_at"), "fact.collected_at"),
            confidence=_enum(data.get("confidence"), Confidence, "fact.confidence"),  # type: ignore[arg-type]
            verification=_enum(data.get("verification"), VerificationDisposition, "fact.verification"),  # type: ignore[arg-type]
            probe_id=_text(data.get("probe_id"), "fact.probe_id"),
            value=data.get("value"),
            resolved_path=data.get("resolved_path"),
            version=data.get("version"),
            digest=data.get("digest"),
            executable=ExecutableIdentity.from_dict(executable) if isinstance(executable, Mapping) else None,
            details=data.get("details"),
        )

    def to_dict(self, *, normalize_time: bool = False) -> dict[str, Any]:
        return {
            "fact_class": self.fact_class,
            "subject": self.subject,
            "status": self.status.value,
            "source": self.source.value,
            "collected_at": "<normalized>" if normalize_time else self.collected_at,
            "confidence": self.confidence.value,
            "verification": self.verification.value,
            "probe_id": self.probe_id,
            "value": dict(self.value) if self.value is not None else None,
            "resolved_path": self.resolved_path,
            "version": self.version,
            "digest": self.digest,
            "executable": self.executable.to_dict() if self.executable is not None else None,
            "details": dict(self.details) if self.details is not None else None,
        }


@dataclass(frozen=True)
class RedactionMetadata:
    policy_version: str = REDACTION_POLICY_VERSION
    raw_environment_included: bool = False
    sensitive_values_redacted: bool = True

    def __post_init__(self) -> None:
        if self.policy_version != REDACTION_POLICY_VERSION:
            raise MachineInventoryContractError("redaction_version_invalid", "unsupported redaction policy version")
        if not isinstance(self.raw_environment_included, bool) or not isinstance(self.sensitive_values_redacted, bool):
            raise MachineInventoryContractError("redaction_flags_invalid", "redaction flags must be boolean")
        if self.raw_environment_included or not self.sensitive_values_redacted:
            raise MachineInventoryContractError("redaction_policy_violation", "raw environment and unredacted sensitive values are forbidden")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RedactionMetadata":
        fields = {"policy_version", "raw_environment_included", "sensitive_values_redacted"}
        data = _strict_mapping(value, fields, "redaction")
        return cls(
            policy_version=_text(data.get("policy_version"), "redaction.policy_version"),
            raw_environment_included=data.get("raw_environment_included"),
            sensitive_values_redacted=data.get("sensitive_values_redacted"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "raw_environment_included": self.raw_environment_included,
            "sensitive_values_redacted": self.sensitive_values_redacted,
        }


@dataclass(frozen=True)
class MachineInventory:
    schema: str
    version: str
    inventory_id: str
    collected_at: str
    path_identity: PathIdentity
    facts: tuple[InventoryFact, ...]
    redaction: RedactionMetadata

    def __post_init__(self) -> None:
        if self.schema != MACHINE_INVENTORY_SCHEMA or self.version != MACHINE_INVENTORY_VERSION:
            raise MachineInventoryContractError("inventory_version_invalid", "unsupported machine inventory schema/version")
        _text(self.inventory_id, "inventory_id")
        _timestamp(self.collected_at, "collected_at")
        if not isinstance(self.path_identity, PathIdentity):
            raise MachineInventoryContractError("path_identity_required", "path_identity is required")
        if not isinstance(self.redaction, RedactionMetadata):
            raise MachineInventoryContractError("redaction_required", "redaction metadata is required")
        if any(not isinstance(item, InventoryFact) for item in self.facts):
            raise MachineInventoryContractError("fact_object_required", "inventory.facts must contain typed facts")
        ordered = tuple(sorted(self.facts, key=lambda item: (item.fact_class, item.subject)))
        if len({(item.fact_class, item.subject) for item in ordered}) != len(ordered):
            raise MachineInventoryContractError("duplicate_fact_identity", "inventory contains duplicate fact identities")
        object.__setattr__(self, "facts", ordered)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MachineInventory":
        fields = {"schema", "version", "inventory_id", "collected_at", "path_identity", "facts", "redaction"}
        data = _strict_mapping(value, fields, "inventory")
        raw_facts = data.get("facts")
        if not isinstance(raw_facts, list):
            raise MachineInventoryContractError("facts_required", "inventory.facts must be a list")
        path = data.get("path_identity")
        redaction = data.get("redaction")
        if not isinstance(path, Mapping) or not isinstance(redaction, Mapping):
            raise MachineInventoryContractError("inventory_nested_object_missing", "path_identity and redaction are required objects")
        return cls(
            schema=_text(data.get("schema"), "inventory.schema"),
            version=_text(data.get("version"), "inventory.version"),
            inventory_id=_text(data.get("inventory_id"), "inventory.inventory_id"),
            collected_at=_timestamp(data.get("collected_at"), "inventory.collected_at"),
            path_identity=PathIdentity.from_dict(path),
            facts=tuple(InventoryFact.from_dict(item) for item in raw_facts),
            redaction=RedactionMetadata.from_dict(redaction),
        )

    def to_dict(self, *, normalize_time: bool = False) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "inventory_id": self.inventory_id,
            "collected_at": "<normalized>" if normalize_time else self.collected_at,
            "path_identity": self.path_identity.to_dict(),
            "facts": [fact.to_dict(normalize_time=normalize_time) for fact in self.facts],
            "redaction": self.redaction.to_dict(),
        }

    def canonical_bytes(self, *, normalize_time: bool = False) -> bytes:
        return canonical_json_bytes(self.to_dict(normalize_time=normalize_time))


def _sensitive_field_paths(value: Any, *, path: str = "") -> tuple[str, ...]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            if _SECRET_KEY.search(str(key)) and item != "[REDACTED]":
                found.append(child)
            found.extend(_sensitive_field_paths(item, path=child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_sensitive_field_paths(item, path=f"{path}[{index}]"))
    elif isinstance(value, str) and _SECRET_VALUE.search(value) and value != "[REDACTED]":
        found.append(path or "value")
    return tuple(sorted(set(found)))


def redact_evidence(value: Any) -> Any:
    """Recursively redact secret-bearing fields without dumping the environment."""

    def redact(item: Any, *, key: str | None = None) -> Any:
        if key is not None and _SECRET_KEY.search(key):
            return "[REDACTED]"
        if isinstance(item, Mapping):
            return {str(child_key): redact(child_value, key=str(child_key)) for child_key, child_value in item.items()}
        if isinstance(item, list):
            return [redact(child, key=key) for child in item]
        if isinstance(item, tuple):
            return [redact(child, key=key) for child in item]
        if isinstance(item, str) and _SECRET_VALUE.search(item):
            return "[REDACTED]"
        return item

    return redact(value)


def redact_environment(environment: Mapping[str, Any]) -> dict[str, Any]:
    """Return a redacted copy; callers must not serialize the raw mapping."""

    if not isinstance(environment, Mapping):
        raise MachineInventoryContractError("environment_mapping_required", "environment evidence must be an object")
    return dict(redact_evidence(environment))


def path_evidence(path_identity: PathIdentity) -> dict[str, Any]:
    """Return the only PATH evidence shape allowed by this contract."""

    if not isinstance(path_identity, PathIdentity):
        raise MachineInventoryContractError("path_identity_required", "PATH evidence needs PathIdentity")
    return {"path_identity": path_identity.to_dict()}


def validate_machine_inventory(value: MachineInventory | Mapping[str, Any]) -> ValidationResult:
    errors: list[str] = []
    try:
        inventory = value if isinstance(value, MachineInventory) else MachineInventory.from_dict(value)
        payload = inventory.to_dict()
        classes = [fact.fact_class for fact in inventory.facts]
        missing = sorted(set(REQUIRED_FACT_CLASSES) - set(classes))
        if missing:
            errors.append("missing_required_fact_classes:" + ",".join(missing))
        path_fact = next((fact for fact in inventory.facts if fact.fact_class == "path_identity"), None)
        if path_fact is None or path_fact.digest != inventory.path_identity.digest:
            errors.append("path_fact_digest_mismatch")
        leaks = _sensitive_field_paths(payload)
        if leaks:
            errors.extend(f"unredacted_sensitive_field:{path}" for path in leaks)
    except MachineInventoryContractError as exc:
        errors.append(exc.code)
    except (TypeError, ValueError):
        errors.append("inventory_shape_invalid")
    return ValidationResult(valid=not errors, errors=tuple(sorted(set(errors))))


def require_valid_machine_inventory(value: MachineInventory | Mapping[str, Any]) -> MachineInventory:
    result = validate_machine_inventory(value)
    if not result.valid:
        raise MachineInventoryValidationError(result.errors)
    return value if isinstance(value, MachineInventory) else MachineInventory.from_dict(value)


@dataclass(frozen=True)
class ProbeDefinition:
    """Typed probe metadata; it contains no executable shell command."""

    probe_id: str
    fact_class: str
    source_kind: ProbeSourceKind
    timeout_class: ProbeTimeoutClass
    parser_strategy: str
    parser_version: str
    candidate_strategy: str
    sensitivity: ProbeSensitivity
    redaction_behavior: str
    argv: tuple[str, ...] = ()
    shell_enabled: bool = False

    def __post_init__(self) -> None:
        _text(self.probe_id, "probe.probe_id")
        if not _FACT_CLASS.fullmatch(self.fact_class):
            raise MachineInventoryContractError("probe_fact_class_invalid", "probe fact class has invalid identity")
        object.__setattr__(self, "source_kind", ProbeSourceKind(self.source_kind))
        object.__setattr__(self, "timeout_class", ProbeTimeoutClass(self.timeout_class))
        object.__setattr__(self, "sensitivity", ProbeSensitivity(self.sensitivity))
        for field_name, value in (
            ("parser_strategy", self.parser_strategy),
            ("parser_version", self.parser_version),
            ("candidate_strategy", self.candidate_strategy),
            ("redaction_behavior", self.redaction_behavior),
        ):
            _text(value, f"probe.{field_name}")
        if not isinstance(self.shell_enabled, bool) or self.shell_enabled:
            raise MachineInventoryContractError("shell_probe_forbidden", "probe definitions cannot enable a shell")
        if self.source_kind is ProbeSourceKind.COMMAND:
            if not self.argv or any(not isinstance(item, str) or not item.strip() for item in self.argv):
                raise MachineInventoryContractError("explicit_argv_required", "command probes require explicit argv")
            if any(_SHELL_META.search(item) for item in self.argv):
                raise MachineInventoryContractError("shell_string_probe_forbidden", "command probe argv contains shell syntax")
        elif self.argv:
            raise MachineInventoryContractError("argv_source_mismatch", "argv is only valid for command probes")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProbeDefinition":
        fields = {
            "probe_id",
            "fact_class",
            "source_kind",
            "timeout_class",
            "parser_strategy",
            "parser_version",
            "candidate_strategy",
            "sensitivity",
            "redaction_behavior",
            "argv",
            "shell_enabled",
        }
        data = _strict_mapping(value, fields, "probe")
        argv = data.get("argv", [])
        if not isinstance(argv, list):
            raise MachineInventoryContractError("argv_invalid", "probe.argv must be a list")
        return cls(
            probe_id=_text(data.get("probe_id"), "probe.probe_id"),
            fact_class=_text(data.get("fact_class"), "probe.fact_class"),
            source_kind=_enum(data.get("source_kind"), ProbeSourceKind, "probe.source_kind"),  # type: ignore[arg-type]
            timeout_class=_enum(data.get("timeout_class"), ProbeTimeoutClass, "probe.timeout_class"),  # type: ignore[arg-type]
            parser_strategy=_text(data.get("parser_strategy"), "probe.parser_strategy"),
            parser_version=_text(data.get("parser_version"), "probe.parser_version"),
            candidate_strategy=_text(data.get("candidate_strategy"), "probe.candidate_strategy"),
            sensitivity=_enum(data.get("sensitivity"), ProbeSensitivity, "probe.sensitivity"),  # type: ignore[arg-type]
            redaction_behavior=_text(data.get("redaction_behavior"), "probe.redaction_behavior"),
            argv=tuple(argv),
            shell_enabled=data.get("shell_enabled", False),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "probe_id": self.probe_id,
            "fact_class": self.fact_class,
            "source_kind": self.source_kind.value,
            "timeout_class": self.timeout_class.value,
            "parser_strategy": self.parser_strategy,
            "parser_version": self.parser_version,
            "candidate_strategy": self.candidate_strategy,
            "sensitivity": self.sensitivity.value,
            "redaction_behavior": self.redaction_behavior,
            "argv": list(self.argv),
            "shell_enabled": self.shell_enabled,
        }


@dataclass(frozen=True)
class ProbeRegistry:
    schema: str
    version: str
    definitions: tuple[ProbeDefinition, ...]

    def __post_init__(self) -> None:
        if self.schema != PROBE_REGISTRY_SCHEMA or self.version != PROBE_REGISTRY_VERSION:
            raise MachineInventoryContractError("probe_registry_version_invalid", "unsupported probe registry version")
        ordered = tuple(sorted(self.definitions, key=lambda item: item.probe_id))
        if len({item.probe_id for item in ordered}) != len(ordered):
            raise MachineInventoryContractError("duplicate_probe_id", "probe registry contains duplicate IDs")
        object.__setattr__(self, "definitions", ordered)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProbeRegistry":
        fields = {"schema", "version", "definitions"}
        data = _strict_mapping(value, fields, "probe_registry")
        definitions = data.get("definitions")
        if not isinstance(definitions, list):
            raise MachineInventoryContractError("probe_definitions_required", "probe_registry.definitions must be a list")
        if any(not isinstance(item, Mapping) for item in definitions):
            raise MachineInventoryContractError("probe_definition_object_required", "probe_registry.definitions must contain objects")
        return cls(
            schema=_text(data.get("schema"), "probe_registry.schema"),
            version=_text(data.get("version"), "probe_registry.version"),
            definitions=tuple(ProbeDefinition.from_dict(item) for item in definitions),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "definitions": [item.to_dict() for item in self.definitions],
        }

    @property
    def missing_required_fact_classes(self) -> tuple[str, ...]:
        present = {item.fact_class for item in self.definitions}
        return tuple(sorted(set(REQUIRED_FACT_CLASSES) - present))


def _probe(
    probe_id: str,
    fact_class: str,
    source_kind: ProbeSourceKind,
    *,
    argv: tuple[str, ...] = (),
    timeout_class: ProbeTimeoutClass = ProbeTimeoutClass.SHORT,
    sensitivity: ProbeSensitivity = ProbeSensitivity.PUBLIC,
) -> ProbeDefinition:
    return ProbeDefinition(
        probe_id=probe_id,
        fact_class=fact_class,
        source_kind=source_kind,
        timeout_class=timeout_class,
        parser_strategy="typed-field-v1",
        parser_version="v1",
        candidate_strategy="explicit-candidate-list-v1",
        sensitivity=sensitivity,
        redaction_behavior="redact-sensitive-fields-v1",
        argv=argv,
    )


CANONICAL_PROBE_REGISTRY = ProbeRegistry(
    schema=PROBE_REGISTRY_SCHEMA,
    version=PROBE_REGISTRY_VERSION,
    definitions=(
        _probe("probe:architecture", "architecture", ProbeSourceKind.OS_API),
        _probe("probe:node", "tool.node", ProbeSourceKind.COMMAND, argv=("node", "--version")),
        _probe("probe:npm", "tool.npm", ProbeSourceKind.COMMAND, argv=("npm", "--version")),
        _probe("probe:git", "tool.git", ProbeSourceKind.COMMAND, argv=("git", "--version")),
        _probe("probe:python", "tool.python", ProbeSourceKind.COMMAND, argv=("python", "--version")),
        _probe("probe:rust", "tool.rust", ProbeSourceKind.COMMAND, argv=("rustc", "--version")),
        _probe("probe:rustup", "tool.rustup", ProbeSourceKind.COMMAND, argv=("rustup", "--version")),
        _probe("probe:cargo", "tool.cargo", ProbeSourceKind.COMMAND, argv=("cargo", "--version")),
        _probe("probe:pwsh", "tool.pwsh", ProbeSourceKind.COMMAND, argv=("pwsh", "--version")),
        _probe("probe:windows-powershell", "tool.windows_powershell", ProbeSourceKind.COMMAND, argv=("powershell", "-Version")),
        _probe("probe:msvc", "tool.msvc", ProbeSourceKind.REGISTRY, timeout_class=ProbeTimeoutClass.MEDIUM),
        _probe("probe:os-identity", "os_identity", ProbeSourceKind.OS_API),
        _probe("probe:path", "path_identity", ProbeSourceKind.ENVIRONMENT, sensitivity=ProbeSensitivity.SENSITIVE),
        _probe("probe:webview2", "tool.webview2", ProbeSourceKind.REGISTRY, timeout_class=ProbeTimeoutClass.MEDIUM),
        _probe("probe:windows-build", "windows_build", ProbeSourceKind.OS_API),
        _probe("probe:windows-sdk", "tool.windows_sdk", ProbeSourceKind.REGISTRY, timeout_class=ProbeTimeoutClass.MEDIUM),
    ),
)


__all__ = [
    "CANONICAL_PROBE_REGISTRY",
    "Confidence",
    "ExecutableIdentity",
    "FactStatus",
    "InventoryFact",
    "InventorySource",
    "MachineInventory",
    "MachineInventoryContractError",
    "MachineInventoryValidationError",
    "MACHINE_INVENTORY_SCHEMA",
    "MACHINE_INVENTORY_VERSION",
    "MACHINE_INVENTORY_VERSION_EXPLICIT",
    "PATH_CANONICALIZATION_VERSION",
    "PathIdentity",
    "ProbeDefinition",
    "ProbeRegistry",
    "ProbeSensitivity",
    "ProbeSourceKind",
    "ProbeTimeoutClass",
    "PROBE_REGISTRY_SCHEMA",
    "PROBE_REGISTRY_VERSION",
    "PROBE_REGISTRY_VERSION_EXPLICIT",
    "REDACTION_POLICY_VERSION",
    "RedactionMetadata",
    "REQUIRED_FACT_CLASSES",
    "ValidationResult",
    "VerificationDisposition",
    "path_evidence",
    "redact_evidence",
    "redact_environment",
    "require_valid_machine_inventory",
    "validate_machine_inventory",
]

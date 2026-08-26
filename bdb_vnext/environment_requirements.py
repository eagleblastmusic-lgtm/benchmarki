"""Immutable environment requirements and fail-closed readiness resolution.

NX-034 compares declared project/task requirements with the typed Machine
Inventory contract from NX-032/NX-033.  It is intentionally diagnostic and
side-effect free: it never installs, provisions, mutates workflow state, or
executes a local task.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from functools import total_ordering
from typing import Any, Mapping, Sequence

from bdb_shared.evidence import canonical_json_bytes

from .machine_inventory_contract import (
    MACHINE_INVENTORY_SCHEMA,
    MACHINE_INVENTORY_VERSION,
    FactStatus,
    MachineInventory,
    MachineInventoryContractError,
    InventoryFact,
    validate_machine_inventory,
)


REQUIREMENT_SCHEMA = "bdb-vnext-environment-requirement-v1"
REQUIREMENT_VERSION = "v1"
ENVIRONMENT_REQUIREMENT_VERSION_EXPLICIT = True
READINESS_SCHEMA = "bdb-vnext-environment-readiness-v1"
READINESS_VERSION = "v1"

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTITY = re.compile(r"^[a-z][a-z0-9_.:-]*$")
_WINDOWS_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")
_VERSION_TEXT = re.compile(
    r"^[vV]?([0-9]+(?:\.[0-9]+){0,3})(?:[-+]([0-9A-Za-z.-]+))?$"
)
_WILDCARD = re.compile(r"^[vV]?([0-9]+(?:\.(?:[0-9]+|[xX*])){0,3})$")


class _StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class RequirementDisposition(_StringEnum):
    ALREADY_AVAILABLE = "ALREADY_AVAILABLE"
    MISSING = "MISSING"
    VERSION_MISMATCH = "VERSION_MISMATCH"
    UNVERIFIABLE = "UNVERIFIABLE"


class ReadinessStatus(_StringEnum):
    ENVIRONMENT_READY = "ENVIRONMENT_READY"
    ENVIRONMENT_NOT_READY = "ENVIRONMENT_NOT_READY"


class RequirementContractError(ValueError):
    """A stable, fail-closed requirement or resolver contract error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RequirementContractError("text_required", f"{field_name} must be non-empty text")
    return value


def _strict_mapping(value: Any, fields: set[str], field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RequirementContractError("mapping_required", f"{field_name} must be an object")
    unknown = sorted(str(key) for key in value if str(key) not in fields)
    if unknown:
        raise RequirementContractError("unknown_field", f"{field_name} has unknown fields: {', '.join(unknown)}")
    return value


def _digest(value: Any, field_name: str) -> str:
    text = _text(value, field_name)
    if not _DIGEST.fullmatch(text):
        raise RequirementContractError("digest_invalid", f"{field_name} must be a sha256 digest")
    return text


def _timestamp(value: Any, field_name: str) -> str:
    text = _text(value, field_name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RequirementContractError("timestamp_invalid", f"{field_name} is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise RequirementContractError("timestamp_timezone_missing", f"{field_name} needs a timezone")
    return text


@total_ordering
@dataclass(frozen=True)
class Version:
    numbers: tuple[int, ...]
    prerelease: tuple[str, ...] = ()

    @classmethod
    def parse(cls, value: str) -> "Version":
        text = _text(value, "version")
        match = _VERSION_TEXT.fullmatch(text)
        if not match:
            raise RequirementContractError("version_invalid", "version is not a supported numeric version")
        numbers = tuple(int(item) for item in match.group(1).split("."))
        prerelease = tuple(match.group(2).split(".")) if match.group(2) else ()
        return cls(numbers=numbers, prerelease=prerelease)

    def _numbers(self, width: int) -> tuple[int, ...]:
        return self.numbers + (0,) * (width - len(self.numbers))

    def _compare(self, other: "Version") -> int:
        if not isinstance(other, Version):
            return NotImplemented
        width = max(len(self.numbers), len(other.numbers))
        left = self._numbers(width)
        right = other._numbers(width)
        if left != right:
            return 1 if left > right else -1
        if self.prerelease == other.prerelease:
            return 0
        if not self.prerelease:
            return 1
        if not other.prerelease:
            return -1
        for left_part, right_part in zip(self.prerelease, other.prerelease):
            if left_part == right_part:
                continue
            left_numeric = left_part.isdigit()
            right_numeric = right_part.isdigit()
            if left_numeric and right_numeric:
                return 1 if int(left_part) > int(right_part) else -1
            if left_numeric != right_numeric:
                return -1 if left_numeric else 1
            return 1 if left_part > right_part else -1
        return 1 if len(self.prerelease) > len(other.prerelease) else -1

    def __lt__(self, other: object) -> bool:
        result = self._compare(other)  # type: ignore[arg-type]
        return result < 0

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Version) and self._compare(other) == 0

    def __str__(self) -> str:
        text = ".".join(str(item) for item in self.numbers)
        return text + ("-" + ".".join(self.prerelease) if self.prerelease else "")


@dataclass(frozen=True)
class VersionTerm:
    operator: str
    version: Version

    def matches(self, observed: Version) -> bool:
        if self.operator == "==":
            return observed == self.version
        if self.operator == "!=":
            return observed != self.version
        if self.operator == ">=":
            return observed >= self.version
        if self.operator == "<=":
            return observed <= self.version
        if self.operator == ">":
            return observed > self.version
        if self.operator == "<":
            return observed < self.version
        raise RequirementContractError("constraint_operator_invalid", "unsupported version constraint operator")

    def __str__(self) -> str:
        return f"{self.operator}{self.version}"


@dataclass(frozen=True)
class VersionConstraint:
    terms: tuple[VersionTerm, ...]
    normalized: str

    @classmethod
    def parse(cls, expression: str) -> "VersionConstraint":
        text = _text(expression, "version_constraint")
        raw_terms = [item.strip() for item in text.split(",") if item.strip()]
        if not raw_terms:
            raise RequirementContractError("version_constraint_invalid", "version constraint has no terms")
        terms: list[VersionTerm] = []
        for raw_term in raw_terms:
            match = re.fullmatch(r"(==|!=|>=|<=|>|<|\^|~)?\s*(.+)", raw_term)
            if not match:
                raise RequirementContractError("version_constraint_invalid", "version constraint term is malformed")
            operator = match.group(1) or "=="
            version_text = match.group(2).strip()
            if _WILDCARD.fullmatch(version_text) and any(
                part.casefold() in {"x", "*"}
                for part in version_text.removeprefix("v").removeprefix("V").split(".")
            ):
                terms.extend(cls._wildcard_terms(version_text))
                continue
            version = Version.parse(version_text)
            if operator == "^":
                terms.extend(cls._compatible_terms(version))
            elif operator == "~":
                terms.extend(cls._tilde_terms(version))
            else:
                terms.append(VersionTerm(operator, version))
        if not terms:
            raise RequirementContractError("version_constraint_invalid", "version constraint has no usable terms")
        return cls(terms=tuple(terms), normalized=",".join(str(term) for term in terms))

    @staticmethod
    def _wildcard_terms(value: str) -> tuple[VersionTerm, ...]:
        text = value[1:] if value[:1].casefold() == "v" else value
        parts = text.split(".")
        wildcard_index = next((index for index, part in enumerate(parts) if part.casefold() in {"x", "*"}), None)
        if wildcard_index is None:
            return (VersionTerm("==", Version.parse(value)),)
        if wildcard_index == 0:
            raise RequirementContractError("version_constraint_invalid", "wildcard cannot replace the major version")
        if any(part.casefold() not in {"x", "*"} and not part.isdigit() for part in parts):
            raise RequirementContractError("version_constraint_invalid", "wildcard version is malformed")
        lower_numbers = tuple(int(part) for part in parts[:wildcard_index])
        lower = Version(lower_numbers)
        upper_numbers = list(lower_numbers)
        upper_numbers[-1] += 1
        upper = Version(tuple(upper_numbers))
        return VersionTerm(">=", lower), VersionTerm("<", upper)

    @staticmethod
    def _compatible_terms(version: Version) -> tuple[VersionTerm, ...]:
        numbers = version.numbers
        if numbers[0] > 0:
            upper = Version((numbers[0] + 1,))
        elif len(numbers) > 1 and numbers[1] > 0:
            upper = Version((0, numbers[1] + 1))
        else:
            upper = Version((0, 0, (numbers[2] + 1) if len(numbers) > 2 else 1))
        return VersionTerm(">=", version), VersionTerm("<", upper)

    @staticmethod
    def _tilde_terms(version: Version) -> tuple[VersionTerm, ...]:
        numbers = version.numbers
        if len(numbers) == 1:
            upper = Version((numbers[0] + 1,))
        else:
            upper = Version((numbers[0], numbers[1] + 1))
        return VersionTerm(">=", version), VersionTerm("<", upper)

    def matches(self, observed: str | Version) -> bool:
        version = observed if isinstance(observed, Version) else Version.parse(observed)
        return all(term.matches(version) for term in self.terms)


@dataclass(frozen=True)
class RequirementSource:
    kind: str
    reference: str
    digest: str

    def __post_init__(self) -> None:
        if self.kind not in {"PROJECT_MANIFEST", "TASK_DECLARATION", "FIXTURE"}:
            raise RequirementContractError("source_kind_invalid", "requirement source kind is unsupported")
        _text(self.reference, "source.reference")
        _digest(self.digest, "source.digest")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RequirementSource":
        data = _strict_mapping(value, {"kind", "reference", "digest"}, "source")
        return cls(
            kind=_text(data.get("kind"), "source.kind"),
            reference=_text(data.get("reference"), "source.reference"),
            digest=_digest(data.get("digest"), "source.digest"),
        )

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "reference": self.reference, "digest": self.digest}


@dataclass(frozen=True)
class RequirementProvenance:
    declared_at: str
    declaration_id: str
    authority: str

    def __post_init__(self) -> None:
        _timestamp(self.declared_at, "provenance.declared_at")
        _text(self.declaration_id, "provenance.declaration_id")
        _text(self.authority, "provenance.authority")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RequirementProvenance":
        data = _strict_mapping(value, {"declared_at", "declaration_id", "authority"}, "provenance")
        return cls(
            declared_at=_timestamp(data.get("declared_at"), "provenance.declared_at"),
            declaration_id=_text(data.get("declaration_id"), "provenance.declaration_id"),
            authority=_text(data.get("authority"), "provenance.authority"),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "declared_at": self.declared_at,
            "declaration_id": self.declaration_id,
            "authority": self.authority,
        }


@dataclass(frozen=True)
class AlternativeCapability:
    capability: str
    version_constraint: str | None = None

    def __post_init__(self) -> None:
        if not _IDENTITY.fullmatch(self.capability):
            raise RequirementContractError("capability_invalid", "alternative capability has invalid identity")
        if self.version_constraint is not None:
            object.__setattr__(self, "version_constraint", VersionConstraint.parse(self.version_constraint).normalized)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AlternativeCapability":
        data = _strict_mapping(value, {"capability", "version_constraint"}, "alternative")
        return cls(
            capability=_text(data.get("capability"), "alternative.capability"),
            version_constraint=data.get("version_constraint"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"capability": self.capability, "version_constraint": self.version_constraint}


@dataclass(frozen=True)
class EnvironmentRequirement:
    requirement_id: str
    capability: str
    required: bool
    version_constraint: str | None
    source: RequirementSource
    provenance: RequirementProvenance
    alternatives: tuple[AlternativeCapability, ...] = ()
    exact_executable_path: str | None = None
    exact_executable_digest: str | None = None

    def __post_init__(self) -> None:
        if not _IDENTITY.fullmatch(self.requirement_id):
            raise RequirementContractError("requirement_id_invalid", "requirement_id has invalid identity")
        if not _IDENTITY.fullmatch(self.capability):
            raise RequirementContractError("capability_invalid", "capability has invalid identity")
        if not isinstance(self.required, bool):
            raise RequirementContractError("required_invalid", "required must be boolean")
        if self.version_constraint is not None:
            object.__setattr__(self, "version_constraint", VersionConstraint.parse(self.version_constraint).normalized)
        if not isinstance(self.source, RequirementSource) or not isinstance(self.provenance, RequirementProvenance):
            raise RequirementContractError("provenance_invalid", "requirement source and provenance are required")
        if any(not isinstance(item, AlternativeCapability) for item in self.alternatives):
            raise RequirementContractError("alternative_invalid", "alternatives must be typed")
        normalized = tuple(sorted(self.alternatives, key=lambda item: (item.capability, item.version_constraint or "")))
        identities = {(item.capability, item.version_constraint) for item in normalized}
        if len(identities) != len(normalized):
            raise RequirementContractError("alternative_duplicate", "alternatives must be unique")
        if self.capability in {item.capability for item in normalized}:
            raise RequirementContractError("alternative_primary_duplicate", "primary capability cannot be repeated")
        object.__setattr__(self, "alternatives", normalized)
        if self.exact_executable_path is not None and not _WINDOWS_ABSOLUTE.match(self.exact_executable_path):
            raise RequirementContractError("executable_path_invalid", "exact executable path must be absolute")
        if self.exact_executable_digest is not None:
            _digest(self.exact_executable_digest, "exact_executable_digest")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EnvironmentRequirement":
        fields = {
            "requirement_id",
            "capability",
            "required",
            "version_constraint",
            "source",
            "provenance",
            "alternatives",
            "exact_executable_path",
            "exact_executable_digest",
        }
        data = _strict_mapping(value, fields, "requirement")
        alternatives = data.get("alternatives", [])
        if not isinstance(alternatives, list) or any(not isinstance(item, Mapping) for item in alternatives):
            raise RequirementContractError("alternative_invalid", "requirement.alternatives must be an object list")
        source = data.get("source")
        provenance = data.get("provenance")
        if not isinstance(source, Mapping) or not isinstance(provenance, Mapping):
            raise RequirementContractError("provenance_invalid", "requirement source and provenance are required objects")
        return cls(
            requirement_id=_text(data.get("requirement_id"), "requirement.requirement_id"),
            capability=_text(data.get("capability"), "requirement.capability"),
            required=data.get("required"),
            version_constraint=data.get("version_constraint"),
            source=RequirementSource.from_dict(source),
            provenance=RequirementProvenance.from_dict(provenance),
            alternatives=tuple(AlternativeCapability.from_dict(item) for item in alternatives),
            exact_executable_path=data.get("exact_executable_path"),
            exact_executable_digest=data.get("exact_executable_digest"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "capability": self.capability,
            "required": self.required,
            "version_constraint": self.version_constraint,
            "source": self.source.to_dict(),
            "provenance": self.provenance.to_dict(),
            "alternatives": [item.to_dict() for item in self.alternatives],
            "exact_executable_path": self.exact_executable_path,
            "exact_executable_digest": self.exact_executable_digest,
        }


@dataclass(frozen=True)
class EnvironmentRequirementSet:
    set_id: str
    requirements: tuple[EnvironmentRequirement, ...]
    inventory_contract_version: str = MACHINE_INVENTORY_VERSION
    max_inventory_age_seconds: int = 86_400
    expected_path_digest: str | None = None
    schema: str = REQUIREMENT_SCHEMA
    version: str = REQUIREMENT_VERSION

    def __post_init__(self) -> None:
        if self.schema != REQUIREMENT_SCHEMA or self.version != REQUIREMENT_VERSION:
            raise RequirementContractError("requirement_version_invalid", "unsupported requirement schema/version")
        if not _IDENTITY.fullmatch(self.set_id):
            raise RequirementContractError("set_id_invalid", "requirement set identity is invalid")
        if self.inventory_contract_version != MACHINE_INVENTORY_VERSION:
            raise RequirementContractError("inventory_contract_invalid", "unsupported inventory contract version")
        if isinstance(self.max_inventory_age_seconds, bool) or not isinstance(self.max_inventory_age_seconds, int) or self.max_inventory_age_seconds <= 0:
            raise RequirementContractError("freshness_policy_invalid", "inventory age must be a positive integer")
        if self.expected_path_digest is not None:
            _digest(self.expected_path_digest, "expected_path_digest")
        if any(not isinstance(item, EnvironmentRequirement) for item in self.requirements):
            raise RequirementContractError("requirement_invalid", "requirement set contains an untyped requirement")
        ordered = tuple(sorted(self.requirements, key=lambda item: item.requirement_id))
        if len({item.requirement_id for item in ordered}) != len(ordered):
            raise RequirementContractError("requirement_duplicate", "requirement IDs must be unique")
        object.__setattr__(self, "requirements", ordered)

    @property
    def requirement_digest(self) -> str:
        payload = {
            "schema": self.schema,
            "version": self.version,
            "set_id": self.set_id,
            "inventory_contract_version": self.inventory_contract_version,
            "max_inventory_age_seconds": self.max_inventory_age_seconds,
            "expected_path_digest": self.expected_path_digest,
            "requirements": [item.to_dict() for item in self.requirements],
        }
        return "sha256:" + hashlib.sha256(canonical_json_bytes(payload)).hexdigest()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EnvironmentRequirementSet":
        fields = {
            "schema",
            "version",
            "set_id",
            "inventory_contract_version",
            "max_inventory_age_seconds",
            "expected_path_digest",
            "requirements",
            "requirement_digest",
        }
        data = _strict_mapping(value, fields, "requirement_set")
        raw_requirements = data.get("requirements")
        if not isinstance(raw_requirements, list) or any(not isinstance(item, Mapping) for item in raw_requirements):
            raise RequirementContractError("requirements_invalid", "requirement_set.requirements must be an object list")
        result = cls(
            schema=_text(data.get("schema"), "requirement_set.schema"),
            version=_text(data.get("version"), "requirement_set.version"),
            set_id=_text(data.get("set_id"), "requirement_set.set_id"),
            inventory_contract_version=_text(data.get("inventory_contract_version"), "inventory_contract_version"),
            max_inventory_age_seconds=data.get("max_inventory_age_seconds"),
            expected_path_digest=data.get("expected_path_digest"),
            requirements=tuple(EnvironmentRequirement.from_dict(item) for item in raw_requirements),
        )
        if _digest(data.get("requirement_digest"), "requirement_digest") != result.requirement_digest:
            raise RequirementContractError("requirement_digest_mismatch", "requirement set digest does not match content")
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "set_id": self.set_id,
            "inventory_contract_version": self.inventory_contract_version,
            "max_inventory_age_seconds": self.max_inventory_age_seconds,
            "expected_path_digest": self.expected_path_digest,
            "requirements": [item.to_dict() for item in self.requirements],
            "requirement_digest": self.requirement_digest,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())


@dataclass(frozen=True)
class RequirementResolution:
    requirement_id: str
    capability: str
    required: bool
    disposition: RequirementDisposition
    selected_capability: str | None
    observed_fact_class: str | None
    observed_version: str | None
    observed_path: str | None
    observed_digest: str | None
    blocking: bool
    reason: str
    explanation: str
    requirement_digest: str
    inventory_id: str
    inventory_freshness: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "capability": self.capability,
            "required": self.required,
            "disposition": self.disposition.value,
            "selected_capability": self.selected_capability,
            "observed_fact_class": self.observed_fact_class,
            "observed_version": self.observed_version,
            "observed_path": self.observed_path,
            "observed_digest": self.observed_digest,
            "blocking": self.blocking,
            "reason": self.reason,
            "explanation": self.explanation,
            "requirement_digest": self.requirement_digest,
            "inventory_id": self.inventory_id,
            "inventory_freshness": self.inventory_freshness,
        }


@dataclass(frozen=True)
class ReadinessResult:
    status: ReadinessStatus
    requirement_digest: str
    inventory_id: str
    inventory_digest: str
    inventory_freshness: str
    evaluated_at: str
    stale: bool
    blocking_requirement_ids: tuple[str, ...]
    requirements: tuple[RequirementResolution, ...]
    explanation: str
    schema: str = READINESS_SCHEMA
    version: str = READINESS_VERSION

    @property
    def ready(self) -> bool:
        return self.status is ReadinessStatus.ENVIRONMENT_READY

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "status": self.status.value,
            "ready": self.ready,
            "requirement_digest": self.requirement_digest,
            "inventory_id": self.inventory_id,
            "inventory_digest": self.inventory_digest,
            "inventory_freshness": self.inventory_freshness,
            "evaluated_at": self.evaluated_at,
            "stale": self.stale,
            "blocking_requirement_ids": list(self.blocking_requirement_ids),
            "requirements": [item.to_dict() for item in self.requirements],
            "explanation": self.explanation,
        }

    def canonical_bytes(self, *, normalize_time: bool = False) -> bytes:
        payload = self.to_dict()
        if normalize_time:
            payload["evaluated_at"] = "<normalized>"
        return canonical_json_bytes(payload)


def _inventory_digest(inventory: MachineInventory) -> str:
    return "sha256:" + hashlib.sha256(inventory.canonical_bytes(normalize_time=True)).hexdigest()


def _parse_datetime(text: str) -> datetime:
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise RequirementContractError("timestamp_timezone_missing", "evaluation timestamp needs a timezone")
    return parsed.astimezone(timezone.utc)


def _fact_for_capability(facts: Mapping[str, InventoryFact], capability: str) -> InventoryFact | None:
    return facts.get(capability)


def _candidate_resolution(
    requirement: EnvironmentRequirement,
    capability: str,
    constraint_text: str | None,
    facts: Mapping[str, InventoryFact],
) -> tuple[RequirementDisposition, InventoryFact | None, str]:
    fact = _fact_for_capability(facts, capability)
    if fact is None or fact.status is FactStatus.MISSING:
        return RequirementDisposition.MISSING, fact, "CAPABILITY_MISSING"
    if fact.status is not FactStatus.AVAILABLE:
        return RequirementDisposition.UNVERIFIABLE, fact, f"OBSERVED_STATUS_{fact.status.value}"
    if constraint_text is not None:
        try:
            constraint = VersionConstraint.parse(constraint_text)
            if not fact.version:
                return RequirementDisposition.UNVERIFIABLE, fact, "OBSERVED_VERSION_MISSING"
            if not constraint.matches(fact.version):
                return RequirementDisposition.VERSION_MISMATCH, fact, "VERSION_CONSTRAINT_UNSATISFIED"
        except RequirementContractError:
            return RequirementDisposition.UNVERIFIABLE, fact, "OBSERVED_VERSION_MALFORMED"
    if requirement.exact_executable_path is not None or requirement.exact_executable_digest is not None:
        if fact.executable is None or fact.resolved_path is None:
            return RequirementDisposition.UNVERIFIABLE, fact, "EXECUTABLE_IDENTITY_UNAVAILABLE"
        if requirement.exact_executable_path is not None and fact.resolved_path.casefold() != requirement.exact_executable_path.casefold():
            return RequirementDisposition.VERSION_MISMATCH, fact, "EXECUTABLE_PATH_MISMATCH"
        if requirement.exact_executable_digest is not None and fact.executable.content_digest != requirement.exact_executable_digest:
            return RequirementDisposition.VERSION_MISMATCH, fact, "EXECUTABLE_DIGEST_MISMATCH"
    return RequirementDisposition.ALREADY_AVAILABLE, fact, "REQUIREMENT_SATISFIED"


def _resolve_one(
    requirement: EnvironmentRequirement,
    facts: Mapping[str, InventoryFact],
    *,
    inventory_id: str,
    inventory_freshness: str,
    requirement_digest: str,
) -> RequirementResolution:
    candidates = ((requirement.capability, requirement.version_constraint),) + tuple(
        (item.capability, item.version_constraint or requirement.version_constraint)
        for item in requirement.alternatives
    )
    outcomes: list[tuple[str, RequirementDisposition, InventoryFact | None, str]] = []
    for capability, constraint in candidates:
        disposition, fact, reason = _candidate_resolution(requirement, capability, constraint, facts)
        outcomes.append((capability, disposition, fact, reason))
        if disposition is RequirementDisposition.ALREADY_AVAILABLE:
            break
    selected_capability, disposition, fact, reason = outcomes[0]
    available = next((item for item in outcomes if item[1] is RequirementDisposition.ALREADY_AVAILABLE), None)
    if available is not None:
        selected_capability, disposition, fact, reason = available
    elif any(item[1] is RequirementDisposition.UNVERIFIABLE for item in outcomes):
        selected_capability, disposition, fact, reason = next(
            item for item in outcomes if item[1] is RequirementDisposition.UNVERIFIABLE
        )
    elif any(item[1] is RequirementDisposition.VERSION_MISMATCH for item in outcomes):
        selected_capability, disposition, fact, reason = next(
            item for item in outcomes if item[1] is RequirementDisposition.VERSION_MISMATCH
        )
    blocking = disposition is not RequirementDisposition.ALREADY_AVAILABLE and requirement.required
    observed_version = fact.version if fact is not None else None
    observed_path = fact.resolved_path if fact is not None else None
    observed_digest = fact.executable.content_digest if fact is not None and fact.executable is not None else None
    explanation = (
        f"requirement={requirement.requirement_id}; capability={requirement.capability}; "
        f"observed={selected_capability if fact is not None else 'none'}; disposition={disposition.value}; "
        f"reason={reason}; required={requirement.required}; blocking={blocking}; "
        f"version={observed_version or 'none'}; provenance={requirement.provenance.declaration_id}; "
        f"freshness={inventory_freshness}; requirement_digest={requirement_digest}; inventory={inventory_id}"
    )
    return RequirementResolution(
        requirement_id=requirement.requirement_id,
        capability=requirement.capability,
        required=requirement.required,
        disposition=disposition,
        selected_capability=selected_capability if fact is not None else None,
        observed_fact_class=fact.fact_class if fact is not None else None,
        observed_version=observed_version,
        observed_path=observed_path,
        observed_digest=observed_digest,
        blocking=blocking,
        reason=reason,
        explanation=explanation,
        requirement_digest=requirement_digest,
        inventory_id=inventory_id,
        inventory_freshness=inventory_freshness,
    )


def _unverifiable_resolution(
    requirement: EnvironmentRequirement,
    *,
    inventory_id: str,
    inventory_freshness: str,
    requirement_digest: str,
    reason: str,
) -> RequirementResolution:
    blocking = requirement.required
    explanation = (
        f"requirement={requirement.requirement_id}; capability={requirement.capability}; observed=unavailable; "
        f"disposition=UNVERIFIABLE; reason={reason}; required={requirement.required}; blocking={blocking}; "
        f"version=none; provenance={requirement.provenance.declaration_id}; freshness={inventory_freshness}; "
        f"requirement_digest={requirement_digest}; inventory={inventory_id}"
    )
    return RequirementResolution(
        requirement_id=requirement.requirement_id,
        capability=requirement.capability,
        required=requirement.required,
        disposition=RequirementDisposition.UNVERIFIABLE,
        selected_capability=None,
        observed_fact_class=None,
        observed_version=None,
        observed_path=None,
        observed_digest=None,
        blocking=blocking,
        reason=reason,
        explanation=explanation,
        requirement_digest=requirement_digest,
        inventory_id=inventory_id,
        inventory_freshness=inventory_freshness,
    )


def resolve_requirements(
    requirement_set: EnvironmentRequirementSet,
    inventory: MachineInventory | Mapping[str, Any],
    *,
    evaluated_at: str,
    current_path_digest: str | None = None,
) -> ReadinessResult:
    """Resolve immutable requirements against current typed inventory."""

    evaluated_at = _timestamp(evaluated_at, "evaluated_at")
    requirement_digest = requirement_set.requirement_digest
    try:
        if isinstance(inventory, MachineInventory):
            typed_inventory = inventory
        else:
            from .machine_inventory_contract import require_valid_machine_inventory

            typed_inventory = require_valid_machine_inventory(inventory)
    except (MachineInventoryContractError, ValueError, TypeError):
        inventory_id = "inventory:invalid"
        inventory_digest = "sha256:" + hashlib.sha256(b"invalid-inventory").hexdigest()
        resolutions = tuple(
            _unverifiable_resolution(
                requirement,
                inventory_id=inventory_id,
                inventory_freshness="INVALID",
                requirement_digest=requirement_digest,
                reason="INVENTORY_INVALID",
            )
            for requirement in requirement_set.requirements
        )
        blocking_ids = tuple(item.requirement_id for item in resolutions if item.blocking)
        return ReadinessResult(
            status=ReadinessStatus.ENVIRONMENT_NOT_READY,
            requirement_digest=requirement_digest,
            inventory_id=inventory_id,
            inventory_digest=inventory_digest,
            inventory_freshness="INVALID",
            evaluated_at=evaluated_at,
            stale=True,
            blocking_requirement_ids=blocking_ids,
            requirements=resolutions,
            explanation="environment inventory is invalid; readiness is fail-closed",
        )

    inventory_id = typed_inventory.inventory_id
    inventory_digest = _inventory_digest(typed_inventory)
    freshness = "CURRENT"
    stale_reason: str | None = None
    try:
        age = (_parse_datetime(evaluated_at) - _parse_datetime(typed_inventory.collected_at)).total_seconds()
        if age < 0 or age > requirement_set.max_inventory_age_seconds:
            stale_reason = "INVENTORY_FRESHNESS_EXPIRED"
        if requirement_set.expected_path_digest is not None and typed_inventory.path_identity.digest != requirement_set.expected_path_digest:
            stale_reason = "PATH_IDENTITY_CHANGED"
        if current_path_digest is not None:
            _digest(current_path_digest, "current_path_digest")
            if typed_inventory.path_identity.digest != current_path_digest:
                stale_reason = "CURRENT_PATH_IDENTITY_CHANGED"
    except (RequirementContractError, ValueError, TypeError):
        stale_reason = "INVENTORY_TIMESTAMP_INVALID"
    if stale_reason is not None:
        freshness = "STALE"
        resolutions = tuple(
            _unverifiable_resolution(
                requirement,
                inventory_id=inventory_id,
                inventory_freshness=freshness,
                requirement_digest=requirement_digest,
                reason=stale_reason,
            )
            for requirement in requirement_set.requirements
        )
        blocking_ids = tuple(item.requirement_id for item in resolutions if item.blocking)
        return ReadinessResult(
            status=ReadinessStatus.ENVIRONMENT_NOT_READY,
            requirement_digest=requirement_digest,
            inventory_id=inventory_id,
            inventory_digest=inventory_digest,
            inventory_freshness=freshness,
            evaluated_at=evaluated_at,
            stale=True,
            blocking_requirement_ids=blocking_ids,
            requirements=resolutions,
            explanation=f"environment inventory is stale; readiness is fail-closed ({stale_reason})",
        )

    facts = {fact.fact_class: fact for fact in typed_inventory.facts}
    resolutions = tuple(
        _resolve_one(
            requirement,
            facts,
            inventory_id=inventory_id,
            inventory_freshness=freshness,
            requirement_digest=requirement_digest,
        )
        for requirement in requirement_set.requirements
    )
    blocking_ids = tuple(item.requirement_id for item in resolutions if item.blocking)
    ready = not blocking_ids
    return ReadinessResult(
        status=ReadinessStatus.ENVIRONMENT_READY if ready else ReadinessStatus.ENVIRONMENT_NOT_READY,
        requirement_digest=requirement_digest,
        inventory_id=inventory_id,
        inventory_digest=inventory_digest,
        inventory_freshness=freshness,
        evaluated_at=evaluated_at,
        stale=False,
        blocking_requirement_ids=blocking_ids,
        requirements=resolutions,
        explanation="environment requirements satisfied" if ready else "one or more required environment requirements block readiness",
    )


def task_start_allowed(readiness: ReadinessResult) -> bool:
    """The only task-start classification exposed by NX-034; it has no effects."""

    if not isinstance(readiness, ReadinessResult):
        return False
    return readiness.ready and not readiness.stale and not readiness.blocking_requirement_ids


__all__ = [
    "AlternativeCapability",
    "ENVIRONMENT_REQUIREMENT_VERSION_EXPLICIT",
    "EnvironmentRequirement",
    "EnvironmentRequirementSet",
    "READINESS_SCHEMA",
    "READINESS_VERSION",
    "ReadinessResult",
    "ReadinessStatus",
    "RequirementContractError",
    "RequirementDisposition",
    "RequirementProvenance",
    "RequirementResolution",
    "RequirementSource",
    "REQUIREMENT_SCHEMA",
    "REQUIREMENT_VERSION",
    "Version",
    "VersionConstraint",
    "resolve_requirements",
    "task_start_allowed",
]

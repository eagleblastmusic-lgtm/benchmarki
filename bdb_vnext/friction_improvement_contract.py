"""NX-059: Operational Friction and Improvement Contracts and Lifecycle.

Authoritative contracts and transition validators for:
- FrictionEventV1 (raw observed friction with provenance and content-addressed evidence)
- ImprovementItemV1 (selective learning/backlog items with decision trace)
- FrictionTransitionEventV1 (append-only lifecycle audit events)

Invariants:
- Append-only friction lifecycle (no historical state mutation)
- Strict provenance (MACHINE vs OPERATOR / MANUAL_NOTE, missing rejected)
- Content-addressed / canonical immutable evidence refs only
- Cross-project identity collision defense
- No automated project plan or project source code mutations
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence


# ==============================================================================
# Contract Schemas and Versions
# ==============================================================================

FRICTION_EVENT_SCHEMA = "bdb-vnext-friction-event-v1"
FRICTION_EVENT_VERSION = "1.0.0"
FRICTION_EVENT_VERSION_EXPLICIT = True

IMPROVEMENT_ITEM_SCHEMA = "bdb-vnext-improvement-item-v1"
IMPROVEMENT_ITEM_VERSION = "1.0.0"
IMPROVEMENT_ITEM_VERSION_EXPLICIT = True

FRICTION_TRANSITION_SCHEMA = "bdb-vnext-friction-transition-v1"
FRICTION_TRANSITION_VERSION = "1.0.0"
FRICTION_TRANSITION_VERSION_EXPLICIT = True

FRICTION_LIFECYCLE_STATES = 5
IMPROVEMENT_LIFECYCLE_STATES = 4

# Invariant boundary flags
MISSING_PROVENANCE_ACCEPTED = False
NONCANONICAL_EVIDENCE_REFS_ACCEPTED = False
HISTORICAL_FRICTION_MUTATIONS = 0
AUTO_PROJECT_PLAN_MUTATIONS = 0
AUTO_PROJECT_SOURCE_MUTATIONS = 0
CROSS_PROJECT_IDENTITY_COLLISIONS = 0


# ==============================================================================
# Exceptions
# ==============================================================================

class FrictionContractError(ValueError):
    """Raised when a friction or improvement contract invariant is violated."""
    pass


# ==============================================================================
# Enums
# ==============================================================================

class FrictionStatus(str, Enum):
    OBSERVED = "OBSERVED"
    TRIAGED = "TRIAGED"
    PROMOTED = "PROMOTED"
    RESOLVED = "RESOLVED"
    SUPERSEDED = "SUPERSEDED"


class ImprovementStatus(str, Enum):
    OPEN = "OPEN"
    PLANNED = "PLANNED"
    DONE = "DONE"
    REJECTED = "REJECTED"


class RecordProvenance(str, Enum):
    MACHINE = "MACHINE"
    OPERATOR = "OPERATOR"
    MANUAL_NOTE = "MANUAL_NOTE"


class FrictionCategory(str, Enum):
    ENVIRONMENT = "ENVIRONMENT"
    TOOLING = "TOOLING"
    INFRASTRUCTURE = "INFRASTRUCTURE"
    CODE_LOGIC = "CODE_LOGIC"
    WITNESS = "WITNESS"
    RECOVERY = "RECOVERY"
    OPERATOR = "OPERATOR"
    TIMEOUT = "TIMEOUT"
    CONFIGURATION = "CONFIGURATION"
    PROCESS_EXECUTION = "PROCESS_EXECUTION"


class FrictionSeverity(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ImprovementPriority(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


# ==============================================================================
# Evidence Reference Patterns
# ==============================================================================

_CANONICAL_EVIDENCE_PATTERNS = [
    re.compile(r"^sha256:[0-9a-f]{64}$"),
    re.compile(r"^blob:[0-9a-f]{40,64}$"),
    re.compile(r"^bdb-evidence:[a-zA-Z0-9_\-\.:]+$"),
    re.compile(r"^bdb-content:[0-9a-f]{40,64}$"),
    re.compile(r"^urn:bdb:evidence:[a-zA-Z0-9_\-\.:]+$"),
    re.compile(r"^urn:bdb:witness:[a-zA-Z0-9_\-\.:]+$"),
]

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_HEX40 = re.compile(r"^[0-9a-f]{40}$")


def validate_evidence_ref(ref: str) -> bool:
    """Validate that an evidence reference is content-addressed or canonical BDB reference."""
    if not isinstance(ref, str) or not ref.strip():
        return False
    # Reject known non-canonical / vague expressions
    vague_patterns = [
        "latest.log",
        "current screenshot",
        "current.log",
        "output.log",
        "temp.log",
    ]
    if ref.strip().lower() in vague_patterns:
        return False
    # Check against canonical patterns
    return any(pat.match(ref) for pat in _CANONICAL_EVIDENCE_PATTERNS)


def validate_provenance(prov: Any) -> RecordProvenance:
    """Validate that provenance is explicit and allowed."""
    if prov is None:
        raise FrictionContractError("Provenance cannot be null or missing.")
    if isinstance(prov, RecordProvenance):
        return prov
    if isinstance(prov, str):
        try:
            return RecordProvenance(prov)
        except ValueError:
            pass
    raise FrictionContractError(f"Invalid or unknown provenance: {prov!r}")


# ==============================================================================
# Allowed Transitions
# ==============================================================================

ALLOWED_FRICTION_TRANSITIONS: dict[FrictionStatus, set[FrictionStatus]] = {
    FrictionStatus.OBSERVED: {FrictionStatus.TRIAGED, FrictionStatus.SUPERSEDED},
    FrictionStatus.TRIAGED: {FrictionStatus.PROMOTED, FrictionStatus.RESOLVED, FrictionStatus.SUPERSEDED},
    FrictionStatus.PROMOTED: {FrictionStatus.RESOLVED, FrictionStatus.SUPERSEDED},
    FrictionStatus.RESOLVED: set(),
    FrictionStatus.SUPERSEDED: set(),
}

ALLOWED_IMPROVEMENT_TRANSITIONS: dict[ImprovementStatus, set[ImprovementStatus]] = {
    ImprovementStatus.OPEN: {ImprovementStatus.PLANNED, ImprovementStatus.DONE, ImprovementStatus.REJECTED},
    ImprovementStatus.PLANNED: {ImprovementStatus.DONE, ImprovementStatus.REJECTED},
    ImprovementStatus.DONE: set(),
    ImprovementStatus.REJECTED: set(),
}


def is_allowed_friction_transition(prev: FrictionStatus | str, next_st: FrictionStatus | str) -> bool:
    try:
        prev_enum = FrictionStatus(prev)
        next_enum = FrictionStatus(next_st)
    except ValueError:
        return False
    return next_enum in ALLOWED_FRICTION_TRANSITIONS.get(prev_enum, set())


def is_allowed_improvement_transition(prev: ImprovementStatus | str, next_st: ImprovementStatus | str) -> bool:
    try:
        prev_enum = ImprovementStatus(prev)
        next_enum = ImprovementStatus(next_st)
    except ValueError:
        return False
    return next_enum in ALLOWED_IMPROVEMENT_TRANSITIONS.get(prev_enum, set())


# ==============================================================================
# Canonical Serialization & Fingerprints
# ==============================================================================

def canonical_json_dumps(data: Any) -> str:
    """Deterministic canonical JSON serialization (RFC 8785 style, sorted keys, no whitespace)."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_digest(data: Any) -> str:
    """Deterministic SHA-256 digest of canonical JSON."""
    raw = canonical_json_dumps(data).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def compute_friction_fingerprint(
    project_id: str,
    category: FrictionCategory | str,
    failure_class: str,
    symptom_signature: str,
    subsystem: str | None = None,
) -> str:
    """Compute stable deterministic semantic fingerprint for friction deduplication.
    
    Excludes volatile runtime noise: timestamps, PIDs, random temp dirs, unparsed stdout.
    """
    cat_val = category.value if isinstance(category, FrictionCategory) else str(category)
    norm_sig = symptom_signature.strip().lower()
    payload = {
        "category": cat_val,
        "failure_class": failure_class.strip(),
        "project_id": project_id.strip(),
        "subsystem": (subsystem or "").strip().lower(),
        "symptom_signature": norm_sig,
    }
    return canonical_digest(payload)


def compute_improvement_fingerprint(
    project_id: str,
    opportunity_signature: str,
    scope: str = "PROJECT_LOCAL",
) -> str:
    """Compute stable deterministic fingerprint for improvement item deduplication."""
    payload = {
        "opportunity_signature": opportunity_signature.strip().lower(),
        "project_id": project_id.strip(),
        "scope": scope.strip(),
    }
    return canonical_digest(payload)


# ==============================================================================
# Data Structures
# ==============================================================================

@dataclass(frozen=True)
class FrictionEventV1:
    schema: str
    schema_version: str
    event_id: str
    fingerprint: str
    project_id: str
    category: FrictionCategory
    failure_class: str
    symptom: str
    severity: FrictionSeverity
    provenance: RecordProvenance
    first_observed_at: str
    last_observed_at: str
    occurrence_count: int
    evidence_refs: tuple[str, ...]
    status: FrictionStatus
    run_id: str | None = None
    milestone_id: str | None = None
    task_id: str | None = None
    binding_id: str | None = None
    attempt_id: str | None = None
    root_cause: str | None = None
    resolution: str | None = None
    promoted_to_improvement_id: str | None = None
    superseded_by_event_id: str | None = None
    source_head: str | None = None
    source_tree: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "fingerprint": self.fingerprint,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "milestone_id": self.milestone_id,
            "task_id": self.task_id,
            "binding_id": self.binding_id,
            "attempt_id": self.attempt_id,
            "category": self.category.value if isinstance(self.category, FrictionCategory) else str(self.category),
            "failure_class": self.failure_class,
            "symptom": self.symptom,
            "severity": self.severity.value if isinstance(self.severity, FrictionSeverity) else str(self.severity),
            "provenance": self.provenance.value if isinstance(self.provenance, RecordProvenance) else str(self.provenance),
            "first_observed_at": self.first_observed_at,
            "last_observed_at": self.last_observed_at,
            "occurrence_count": self.occurrence_count,
            "evidence_refs": list(self.evidence_refs),
            "status": self.status.value if isinstance(self.status, FrictionStatus) else str(self.status),
            "root_cause": self.root_cause,
            "resolution": self.resolution,
            "promoted_to_improvement_id": self.promoted_to_improvement_id,
            "superseded_by_event_id": self.superseded_by_event_id,
            "source_head": self.source_head,
            "source_tree": self.source_tree,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> FrictionEventV1:
        errors = validate_friction_event_dict(data)
        if errors:
            raise FrictionContractError(f"Invalid FrictionEventV1 data: {'; '.join(errors)}")
        return cls(
            schema=data["schema"],
            schema_version=data["schema_version"],
            event_id=data["event_id"],
            fingerprint=data["fingerprint"],
            project_id=data["project_id"],
            run_id=data.get("run_id"),
            milestone_id=data.get("milestone_id"),
            task_id=data.get("task_id"),
            binding_id=data.get("binding_id"),
            attempt_id=data.get("attempt_id"),
            category=FrictionCategory(data["category"]),
            failure_class=data["failure_class"],
            symptom=data["symptom"],
            severity=FrictionSeverity(data["severity"]),
            provenance=RecordProvenance(data["provenance"]),
            first_observed_at=data["first_observed_at"],
            last_observed_at=data["last_observed_at"],
            occurrence_count=int(data["occurrence_count"]),
            evidence_refs=tuple(data["evidence_refs"]),
            status=FrictionStatus(data["status"]),
            root_cause=data.get("root_cause"),
            resolution=data.get("resolution"),
            promoted_to_improvement_id=data.get("promoted_to_improvement_id"),
            superseded_by_event_id=data.get("superseded_by_event_id"),
            source_head=data.get("source_head"),
            source_tree=data.get("source_tree"),
        )


@dataclass(frozen=True)
class FrictionTransitionEventV1:
    schema: str
    schema_version: str
    transition_id: str
    event_id: str
    previous_status: FrictionStatus
    new_status: FrictionStatus
    reason: str
    timestamp: str
    provenance: RecordProvenance
    evidence_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "transition_id": self.transition_id,
            "event_id": self.event_id,
            "previous_status": self.previous_status.value if isinstance(self.previous_status, FrictionStatus) else str(self.previous_status),
            "new_status": self.new_status.value if isinstance(self.new_status, FrictionStatus) else str(self.new_status),
            "reason": self.reason,
            "timestamp": self.timestamp,
            "provenance": self.provenance.value if isinstance(self.provenance, RecordProvenance) else str(self.provenance),
            "evidence_refs": list(self.evidence_refs),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> FrictionTransitionEventV1:
        errors = validate_friction_transition_dict(data)
        if errors:
            raise FrictionContractError(f"Invalid FrictionTransitionEventV1: {'; '.join(errors)}")
        return cls(
            schema=data["schema"],
            schema_version=data["schema_version"],
            transition_id=data["transition_id"],
            event_id=data["event_id"],
            previous_status=FrictionStatus(data["previous_status"]),
            new_status=FrictionStatus(data["new_status"]),
            reason=data["reason"],
            timestamp=data["timestamp"],
            provenance=RecordProvenance(data["provenance"]),
            evidence_refs=tuple(data.get("evidence_refs", [])),
        )


@dataclass(frozen=True)
class ImprovementItemV1:
    schema: str
    schema_version: str
    improvement_id: str
    fingerprint: str
    title: str
    opportunity: str
    priority: ImprovementPriority
    source_friction_refs: tuple[str, ...]
    project_id: str
    provenance: RecordProvenance
    decision_reason: str
    status: ImprovementStatus
    created_at: str
    updated_at: str
    revision: int
    evidence_refs: tuple[str, ...]
    superseded_by_improvement_id: str | None = None
    merged_into_improvement_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "improvement_id": self.improvement_id,
            "fingerprint": self.fingerprint,
            "title": self.title,
            "opportunity": self.opportunity,
            "priority": self.priority.value if isinstance(self.priority, ImprovementPriority) else str(self.priority),
            "source_friction_refs": list(self.source_friction_refs),
            "project_id": self.project_id,
            "provenance": self.provenance.value if isinstance(self.provenance, RecordProvenance) else str(self.provenance),
            "decision_reason": self.decision_reason,
            "status": self.status.value if isinstance(self.status, ImprovementStatus) else str(self.status),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "revision": self.revision,
            "evidence_refs": list(self.evidence_refs),
            "superseded_by_improvement_id": self.superseded_by_improvement_id,
            "merged_into_improvement_id": self.merged_into_improvement_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ImprovementItemV1:
        errors = validate_improvement_item_dict(data)
        if errors:
            raise FrictionContractError(f"Invalid ImprovementItemV1 data: {'; '.join(errors)}")
        return cls(
            schema=data["schema"],
            schema_version=data["schema_version"],
            improvement_id=data["improvement_id"],
            fingerprint=data["fingerprint"],
            title=data["title"],
            opportunity=data["opportunity"],
            priority=ImprovementPriority(data["priority"]),
            source_friction_refs=tuple(data["source_friction_refs"]),
            project_id=data["project_id"],
            provenance=RecordProvenance(data["provenance"]),
            decision_reason=data["decision_reason"],
            status=ImprovementStatus(data["status"]),
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            revision=int(data["revision"]),
            evidence_refs=tuple(data["evidence_refs"]),
            superseded_by_improvement_id=data.get("superseded_by_improvement_id"),
            merged_into_improvement_id=data.get("merged_into_improvement_id"),
        )


# ==============================================================================
# Validators
# ==============================================================================

def validate_friction_event_dict(data: Mapping[str, Any]) -> list[str]:
    """Validate dictionary against FrictionEventV1 semantic contract."""
    errors: list[str] = []
    if not isinstance(data, Mapping):
        return ["Data must be a mapping"]

    if data.get("schema") != FRICTION_EVENT_SCHEMA:
        errors.append(f"Invalid schema: expected '{FRICTION_EVENT_SCHEMA}', got '{data.get('schema')}'")
    if data.get("schema_version") != FRICTION_EVENT_VERSION:
        errors.append(f"Invalid schema_version: expected '{FRICTION_EVENT_VERSION}', got '{data.get('schema_version')}'")

    for req in ["event_id", "fingerprint", "project_id", "category", "failure_class",
                "symptom", "severity", "provenance", "first_observed_at", "last_observed_at",
                "occurrence_count", "evidence_refs", "status"]:
        if req not in data or data[req] is None:
            errors.append(f"Missing required field: '{req}'")

    if "event_id" in data and (not isinstance(data["event_id"], str) or not data["event_id"].strip()):
        errors.append("event_id must be a non-empty string")

    if "fingerprint" in data:
        fp = data["fingerprint"]
        if not isinstance(fp, str) or not _SHA256_HEX.match(fp):
            errors.append(f"fingerprint must be a 64-char hex sha256, got {fp!r}")

    if "project_id" in data and (not isinstance(data["project_id"], str) or not data["project_id"].strip()):
        errors.append("project_id must be a non-empty string")

    if "category" in data:
        try:
            FrictionCategory(data["category"])
        except ValueError:
            errors.append(f"Unknown category: {data['category']!r}")

    if "severity" in data:
        try:
            FrictionSeverity(data["severity"])
        except ValueError:
            errors.append(f"Unknown severity: {data['severity']!r}")

    if "provenance" in data:
        try:
            validate_provenance(data["provenance"])
        except FrictionContractError as ex:
            errors.append(str(ex))

    if "status" in data:
        try:
            FrictionStatus(data["status"])
        except ValueError:
            errors.append(f"Unknown status: {data['status']!r}")

    if "occurrence_count" in data:
        cnt = data["occurrence_count"]
        if not isinstance(cnt, int) or cnt < 1:
            errors.append(f"occurrence_count must be integer >= 1, got {cnt!r}")

    if "evidence_refs" in data:
        refs = data["evidence_refs"]
        if not isinstance(refs, (list, tuple)) or len(refs) == 0:
            errors.append("evidence_refs must be a non-empty sequence")
        else:
            for r in refs:
                if not validate_evidence_ref(r):
                    errors.append(f"Non-canonical evidence reference rejected: {r!r}")

    if "source_head" in data and data["source_head"] is not None:
        if not isinstance(data["source_head"], str) or not _HEX40.match(data["source_head"]):
            errors.append(f"source_head must be 40-char hex sha, got {data['source_head']!r}")

    if "source_tree" in data and data["source_tree"] is not None:
        if not isinstance(data["source_tree"], str) or not _HEX40.match(data["source_tree"]):
            errors.append(f"source_tree must be 40-char hex sha, got {data['source_tree']!r}")

    return errors


def validate_friction_transition_dict(data: Mapping[str, Any]) -> list[str]:
    """Validate dictionary against FrictionTransitionEventV1 semantic contract."""
    errors: list[str] = []
    if not isinstance(data, Mapping):
        return ["Data must be a mapping"]

    if data.get("schema") != FRICTION_TRANSITION_SCHEMA:
        errors.append(f"Invalid schema: expected '{FRICTION_TRANSITION_SCHEMA}', got '{data.get('schema')}'")
    if data.get("schema_version") != FRICTION_TRANSITION_VERSION:
        errors.append(f"Invalid schema_version: expected '{FRICTION_TRANSITION_VERSION}', got '{data.get('schema_version')}'")

    for req in ["transition_id", "event_id", "previous_status", "new_status", "reason", "timestamp", "provenance"]:
        if req not in data or data[req] is None:
            errors.append(f"Missing required field: '{req}'")

    if "previous_status" in data and "new_status" in data:
        prev = data["previous_status"]
        nxt = data["new_status"]
        if not is_allowed_friction_transition(prev, nxt):
            errors.append(f"Illegal friction transition: {prev} -> {nxt}")

    if "provenance" in data:
        try:
            validate_provenance(data["provenance"])
        except FrictionContractError as ex:
            errors.append(str(ex))

    if "reason" in data and (not isinstance(data["reason"], str) or not data["reason"].strip()):
        errors.append("reason must be a non-empty string")

    if "evidence_refs" in data and data["evidence_refs"]:
        for r in data["evidence_refs"]:
            if not validate_evidence_ref(r):
                errors.append(f"Non-canonical evidence reference rejected: {r!r}")

    return errors


def validate_improvement_item_dict(data: Mapping[str, Any]) -> list[str]:
    """Validate dictionary against ImprovementItemV1 semantic contract."""
    errors: list[str] = []
    if not isinstance(data, Mapping):
        return ["Data must be a mapping"]

    if data.get("schema") != IMPROVEMENT_ITEM_SCHEMA:
        errors.append(f"Invalid schema: expected '{IMPROVEMENT_ITEM_SCHEMA}', got '{data.get('schema')}'")
    if data.get("schema_version") != IMPROVEMENT_ITEM_VERSION:
        errors.append(f"Invalid schema_version: expected '{IMPROVEMENT_ITEM_VERSION}', got '{data.get('schema_version')}'")

    for req in ["improvement_id", "fingerprint", "title", "opportunity", "priority",
                "source_friction_refs", "project_id", "provenance", "decision_reason",
                "status", "created_at", "updated_at", "revision", "evidence_refs"]:
        if req not in data or data[req] is None:
            errors.append(f"Missing required field: '{req}'")

    if "fingerprint" in data:
        fp = data["fingerprint"]
        if not isinstance(fp, str) or not _SHA256_HEX.match(fp):
            errors.append(f"fingerprint must be a 64-char hex sha256, got {fp!r}")

    if "priority" in data:
        try:
            ImprovementPriority(data["priority"])
        except ValueError:
            errors.append(f"Unknown priority: {data['priority']!r}")

    if "status" in data:
        try:
            ImprovementStatus(data["status"])
        except ValueError:
            errors.append(f"Unknown status: {data['status']!r}")

    if "provenance" in data:
        try:
            validate_provenance(data["provenance"])
        except FrictionContractError as ex:
            errors.append(str(ex))

    if "source_friction_refs" in data:
        refs = data["source_friction_refs"]
        if not isinstance(refs, (list, tuple)) or len(refs) == 0:
            errors.append("source_friction_refs must be a non-empty sequence")

    if "revision" in data:
        rev = data["revision"]
        if not isinstance(rev, int) or rev < 1:
            errors.append(f"revision must be integer >= 1, got {rev!r}")

    if "evidence_refs" in data and data["evidence_refs"]:
        for r in data["evidence_refs"]:
            if not validate_evidence_ref(r):
                errors.append(f"Non-canonical evidence reference rejected: {r!r}")

    return errors


# ==============================================================================
# Append-Only Lifecycle State Operations
# ==============================================================================

def create_friction_transition(
    event: FrictionEventV1,
    new_status: FrictionStatus,
    reason: str,
    provenance: RecordProvenance,
    transition_id: str | None = None,
    timestamp: str | None = None,
    evidence_refs: Sequence[str] | None = None,
) -> tuple[FrictionEventV1, FrictionTransitionEventV1]:
    """Execute an allowed lifecycle transition producing an updated event snapshot and an immutable transition record.
    
    Fails closed if the transition is illegal.
    """
    if not is_allowed_friction_transition(event.status, new_status):
        raise FrictionContractError(f"Illegal transition: {event.status.value} -> {new_status.value}")
    
    prov = validate_provenance(provenance)
    ts = timestamp or datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    t_id = transition_id or f"tr_{hashlib.sha256(f'{event.event_id}:{event.status.value}:{new_status.value}:{ts}'.encode()).hexdigest()[:16]}"
    ev_refs = tuple(evidence_refs or ())
    for r in ev_refs:
        if not validate_evidence_ref(r):
            raise FrictionContractError(f"Non-canonical evidence reference: {r!r}")

    trans_record = FrictionTransitionEventV1(
        schema=FRICTION_TRANSITION_SCHEMA,
        schema_version=FRICTION_TRANSITION_VERSION,
        transition_id=t_id,
        event_id=event.event_id,
        previous_status=event.status,
        new_status=new_status,
        reason=reason,
        timestamp=ts,
        provenance=prov,
        evidence_refs=ev_refs,
    )

    # Return updated event snapshot (preserving original first_observed_at and identity)
    updated_event = FrictionEventV1(
        schema=event.schema,
        schema_version=event.schema_version,
        event_id=event.event_id,
        fingerprint=event.fingerprint,
        project_id=event.project_id,
        run_id=event.run_id,
        milestone_id=event.milestone_id,
        task_id=event.task_id,
        binding_id=event.binding_id,
        attempt_id=event.attempt_id,
        category=event.category,
        failure_class=event.failure_class,
        symptom=event.symptom,
        severity=event.severity,
        provenance=event.provenance,
        first_observed_at=event.first_observed_at,
        last_observed_at=ts,
        occurrence_count=event.occurrence_count,
        evidence_refs=event.evidence_refs + ev_refs,
        status=new_status,
        root_cause=event.root_cause,
        resolution=event.resolution,
        promoted_to_improvement_id=event.promoted_to_improvement_id,
        superseded_by_event_id=event.superseded_by_event_id,
        source_head=event.source_head,
        source_tree=event.source_tree,
    )

    return updated_event, trans_record


def create_improvement_transition(
    item: ImprovementItemV1,
    new_status: ImprovementStatus,
    reason: str,
    provenance: RecordProvenance,
    timestamp: str | None = None,
    new_evidence_refs: Sequence[str] | None = None,
) -> ImprovementItemV1:
    """Transition an improvement item to a new allowed status, bumping revision."""
    if not is_allowed_improvement_transition(item.status, new_status):
        raise FrictionContractError(f"Illegal improvement transition: {item.status.value} -> {new_status.value}")

    prov = validate_provenance(provenance)
    ts = timestamp or datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    ev_refs = tuple(new_evidence_refs or ())
    for r in ev_refs:
        if not validate_evidence_ref(r):
            raise FrictionContractError(f"Non-canonical evidence reference: {r!r}")

    return ImprovementItemV1(
        schema=item.schema,
        schema_version=item.schema_version,
        improvement_id=item.improvement_id,
        fingerprint=item.fingerprint,
        title=item.title,
        opportunity=item.opportunity,
        priority=item.priority,
        source_friction_refs=item.source_friction_refs,
        project_id=item.project_id,
        provenance=prov,
        decision_reason=reason,
        status=new_status,
        created_at=item.created_at,
        updated_at=ts,
        revision=item.revision + 1,
        evidence_refs=item.evidence_refs + ev_refs,
        superseded_by_improvement_id=item.superseded_by_improvement_id,
        merged_into_improvement_id=item.merged_into_improvement_id,
    )

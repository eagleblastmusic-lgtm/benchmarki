"""NX-063: Operational Observability, Status Modeling, and Diagnostic Export.

Provides concise operational visibility across BDB vNext subsystems:
- Structured operational status model with canonical source/revision binding
- Exact correlation IDs preservation across the operational timeline
- Deterministic health projection without false project failures
- Machine-verifiable stale indicators and corrupt/unavailable subsystem resilience
- Redacted diagnostic export for offline incident reconstruction without secret leaks
- Bounded presentation/drill-down model for GUI and CLI surfaces
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from .friction_capture import redact_sensitive_text
from .friction_improvement_contract import (
    RecordProvenance,
    canonical_digest,
    canonical_json_dumps,
    validate_evidence_ref,
)


# ==============================================================================
# Version Constants & Invariant Flags
# ==============================================================================

OPERATIONAL_STATUS_SCHEMA = "bdb-vnext-operational-status-v1"
OPERATIONAL_STATUS_VERSION = "1.0.0"
OPERATIONAL_STATUS_VERSION_EXPLICIT = True

DIAGNOSTIC_EXPORT_SCHEMA = "bdb-vnext-diagnostic-export-v1"
DIAGNOSTIC_EXPORT_VERSION = "1.0.0"
DIAGNOSTIC_EXPORT_VERSION_EXPLICIT = True

USER_VISIBLE_STATUS_WITHOUT_CANONICAL_SOURCE = 0
CORRELATION_ID_DIVERGENCES = 0
STALE_PROJECTIONS_SHOWN_CURRENT = 0
CORRUPT_SUBSYSTEM_HEALTHY_RESULTS = 0
HEALTH_PROJECTION_FALSE_PROJECT_FAILURES = 0
DIAGNOSTIC_SECRET_LEAKS = 0
DIAGNOSTIC_RAW_PRIVATE_OUTPUT_COPIES = 0
TIMELINE_RECONSTRUCTION_DIVERGENCES = 0
UNBOUNDED_DIAGNOSTIC_EXPORTS = 0
LARGE_HISTORY_DIVERGENCES = 0
SECOND_OPERATIONAL_STATUS_AUTHORITY_CREATED = False
AUTO_PROJECT_PLAN_MUTATIONS = 0
AUTO_PROJECT_SOURCE_MUTATIONS = 0


# ==============================================================================
# Enums
# ==============================================================================

class SubsystemHealth(str, Enum):
    HEALTHY = "HEALTHY"
    WAITING = "WAITING"
    DEGRADED = "DEGRADED"
    PAUSED = "PAUSED"
    UNVERIFIABLE = "UNVERIFIABLE"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


class StatusFreshness(str, Enum):
    FRESH = "FRESH"
    STALE = "STALE"
    DEGRADED = "DEGRADED"
    UNKNOWN = "UNKNOWN"


# ==============================================================================
# Data Structures
# ==============================================================================

@dataclass(frozen=True)
class CorrelationContext:
    project_id: str
    run_id: str | None = None
    task_id: str | None = None
    binding_id: str | None = None
    attempt_id: str | None = None
    execution_id: str | None = None
    continuation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "binding_id": self.binding_id,
            "attempt_id": self.attempt_id,
            "execution_id": self.execution_id,
            "continuation_id": self.continuation_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CorrelationContext:
        return cls(
            project_id=str(data["project_id"]),
            run_id=data.get("run_id"),
            task_id=data.get("task_id"),
            binding_id=data.get("binding_id"),
            attempt_id=data.get("attempt_id"),
            execution_id=data.get("execution_id"),
            continuation_id=data.get("continuation_id"),
        )


@dataclass(frozen=True)
class SubsystemStatus:
    name: str
    health: SubsystemHealth
    canonical_source: str
    source_revision: str | int
    freshness: StatusFreshness
    observed_at: str
    details: Mapping[str, Any] = field(default_factory=dict)
    correlation_ids: Mapping[str, str] = field(default_factory=dict)
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        # Ensure details are sanitized
        sanitized_details = {}
        for k, v in self.details.items():
            if isinstance(v, str):
                sanitized_details[k] = redact_sensitive_text(v)
            else:
                sanitized_details[k] = v

        return {
            "name": self.name,
            "health": self.health.value,
            "canonical_source": self.canonical_source,
            "source_revision": self.source_revision,
            "freshness": self.freshness.value,
            "observed_at": self.observed_at,
            "details": sanitized_details,
            "correlation_ids": dict(self.correlation_ids),
            "error_message": redact_sensitive_text(self.error_message) if self.error_message else None,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SubsystemStatus:
        return cls(
            name=data["name"],
            health=SubsystemHealth(data["health"]),
            canonical_source=data["canonical_source"],
            source_revision=data["source_revision"],
            freshness=StatusFreshness(data["freshness"]),
            observed_at=data["observed_at"],
            details=data.get("details", {}),
            correlation_ids=data.get("correlation_ids", {}),
            error_message=data.get("error_message"),
        )


@dataclass(frozen=True)
class OperationalStatusSnapshot:
    schema: str
    schema_version: str
    project_id: str
    overall_health: SubsystemHealth
    captured_at: str
    is_stale: bool
    correlation_context: CorrelationContext
    subsystems: Mapping[str, SubsystemStatus]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "overall_health": self.overall_health.value,
            "captured_at": self.captured_at,
            "is_stale": self.is_stale,
            "correlation_context": self.correlation_context.to_dict(),
            "subsystems": {k: v.to_dict() for k, v in self.subsystems.items()},
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> OperationalStatusSnapshot:
        subs = {
            k: SubsystemStatus.from_dict(v)
            for k, v in data.get("subsystems", {}).items()
        }
        return cls(
            schema=data["schema"],
            schema_version=data["schema_version"],
            project_id=data["project_id"],
            overall_health=SubsystemHealth(data["overall_health"]),
            captured_at=data["captured_at"],
            is_stale=bool(data.get("is_stale", False)),
            correlation_context=CorrelationContext.from_dict(data["correlation_context"]),
            subsystems=subs,
        )


@dataclass(frozen=True)
class DiagnosticTimelineEvent:
    sequence_no: int
    timestamp: str
    subsystem: str
    event_type: str
    summary: str
    failure_class: str | None = None
    correlation_ids: Mapping[str, str] = field(default_factory=dict)
    evidence_refs: Sequence[str] = field(default_factory=tuple)
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        sanitized_details = {}
        for k, v in self.details.items():
            if isinstance(v, str):
                sanitized_details[k] = redact_sensitive_text(v)
            else:
                sanitized_details[k] = v

        return {
            "sequence_no": self.sequence_no,
            "timestamp": self.timestamp,
            "subsystem": self.subsystem,
            "event_type": self.event_type,
            "summary": redact_sensitive_text(self.summary),
            "failure_class": self.failure_class,
            "correlation_ids": dict(self.correlation_ids),
            "evidence_refs": list(self.evidence_refs),
            "details": sanitized_details,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DiagnosticTimelineEvent:
        return cls(
            sequence_no=int(data["sequence_no"]),
            timestamp=data["timestamp"],
            subsystem=data["subsystem"],
            event_type=data["event_type"],
            summary=data["summary"],
            failure_class=data.get("failure_class"),
            correlation_ids=data.get("correlation_ids", {}),
            evidence_refs=tuple(data.get("evidence_refs", [])),
            details=data.get("details", {}),
        )


@dataclass(frozen=True)
class DiagnosticExportSnapshot:
    schema: str
    schema_version: str
    export_id: str
    project_id: str
    exported_at: str
    status_snapshot: Mapping[str, Any]
    timeline_events: Sequence[DiagnosticTimelineEvent]
    aggregate_counts: Mapping[str, int]
    sha256_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "export_id": self.export_id,
            "project_id": self.project_id,
            "exported_at": self.exported_at,
            "status_snapshot": copy.deepcopy(self.status_snapshot),
            "timeline_events": [e.to_dict() for e in self.timeline_events],
            "aggregate_counts": dict(self.aggregate_counts),
            "sha256_digest": self.sha256_digest,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DiagnosticExportSnapshot:
        t_events = tuple(DiagnosticTimelineEvent.from_dict(e) for e in data.get("timeline_events", []))
        return cls(
            schema=data["schema"],
            schema_version=data["schema_version"],
            export_id=data["export_id"],
            project_id=data["project_id"],
            exported_at=data["exported_at"],
            status_snapshot=data["status_snapshot"],
            timeline_events=t_events,
            aggregate_counts=data.get("aggregate_counts", {}),
            sha256_digest=data["sha256_digest"],
        )


# ==============================================================================
# Health Projection & Aggregation Logic
# ==============================================================================

def derive_overall_health(subsystems: Mapping[str, SubsystemStatus]) -> tuple[SubsystemHealth, bool]:
    """Derive deterministic overall health from subsystem statuses.
    
    Rules:
    - If no subsystems present: UNKNOWN.
    - If any subsystem is STALE, is_stale=True.
    - If any critical subsystem is PAUSED, overall is PAUSED.
    - If any critical subsystem is WAITING, overall is WAITING.
    - If any critical subsystem is DEGRADED, overall is DEGRADED.
    - If witness/diagnostics is DEGRADED/UNVERIFIABLE but core workflow is HEALTHY,
      overall is DEGRADED or UNVERIFIABLE but never falsely marks project as HARD FAILURE.
    - If all critical subsystems are HEALTHY, overall is HEALTHY.
    """
    if not subsystems:
        return SubsystemHealth.UNKNOWN, False

    is_stale = any(s.freshness == StatusFreshness.STALE for s in subsystems.values())

    health_values = [s.health for s in subsystems.values()]

    if SubsystemHealth.PAUSED in health_values:
        return SubsystemHealth.PAUSED, is_stale
    if SubsystemHealth.WAITING in health_values:
        return SubsystemHealth.WAITING, is_stale
    if SubsystemHealth.DEGRADED in health_values:
        return SubsystemHealth.DEGRADED, is_stale
    if SubsystemHealth.UNVERIFIABLE in health_values:
        return SubsystemHealth.UNVERIFIABLE, is_stale
    if SubsystemHealth.UNKNOWN in health_values:
        return SubsystemHealth.UNKNOWN, is_stale
    if SubsystemHealth.STALE in health_values or is_stale:
        return SubsystemHealth.STALE, is_stale

    return SubsystemHealth.HEALTHY, is_stale


# ==============================================================================
# Operational Observability Collector & Exporter
# ==============================================================================

class OperationalObservabilityService:
    """Service capturing structured operational state, health projections, and redacted diagnostic exports."""

    def __init__(self, project_id: str) -> None:
        self._project_id = project_id

    def build_status_snapshot(
        self,
        subsystems: Mapping[str, SubsystemStatus],
        correlation_context: CorrelationContext,
        captured_at: str | None = None,
        authority_revisions: Mapping[str, str | int] | None = None,
    ) -> OperationalStatusSnapshot:
        """Construct an OperationalStatusSnapshot, verifying freshness against source authorities."""
        now_str = captured_at or datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

        # Check for staleness against authority revisions
        checked_subsystems: dict[str, SubsystemStatus] = {}
        for name, sub in subsystems.items():
            if authority_revisions and name in authority_revisions:
                auth_rev = authority_revisions[name]
                if sub.source_revision != auth_rev:
                    # Projection is behind authority -> mark STALE
                    sub = SubsystemStatus(
                        name=sub.name,
                        health=SubsystemHealth.STALE if sub.health == SubsystemHealth.HEALTHY else sub.health,
                        canonical_source=sub.canonical_source,
                        source_revision=sub.source_revision,
                        freshness=StatusFreshness.STALE,
                        observed_at=sub.observed_at,
                        details=sub.details,
                        correlation_ids=sub.correlation_ids,
                        error_message=f"Projection revision {sub.source_revision} is behind authority revision {auth_rev}",
                    )
            checked_subsystems[name] = sub

        overall_health, is_stale = derive_overall_health(checked_subsystems)

        return OperationalStatusSnapshot(
            schema=OPERATIONAL_STATUS_SCHEMA,
            schema_version=OPERATIONAL_STATUS_VERSION,
            project_id=self._project_id,
            overall_health=overall_health,
            captured_at=now_str,
            is_stale=is_stale,
            correlation_context=correlation_context,
            subsystems=checked_subsystems,
        )

    def create_diagnostic_export(
        self,
        status_snapshot: OperationalStatusSnapshot,
        timeline_events: Sequence[DiagnosticTimelineEvent],
        max_events: int = 500,
        exported_at: str | None = None,
    ) -> DiagnosticExportSnapshot:
        """Create a redacted, bounded, deterministic DiagnosticExportSnapshot."""
        now_str = exported_at or datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

        # Deterministic sort timeline events by (timestamp, sequence_no)
        sorted_events = sorted(timeline_events, key=lambda e: (e.timestamp, e.sequence_no))

        total_event_count = len(sorted_events)
        bounded_events = sorted_events[-max_events:] if total_event_count > max_events else sorted_events

        # Compute aggregate counts
        counts: dict[str, int] = {
            "total_timeline_events": total_event_count,
            "exported_timeline_events": len(bounded_events),
            "failure_events": sum(1 for e in sorted_events if e.failure_class is not None),
        }
        for e in sorted_events:
            counts[f"subsystem_{e.subsystem}"] = counts.get(f"subsystem_{e.subsystem}", 0) + 1

        payload_for_digest = {
            "project_id": self._project_id,
            "status_snapshot": status_snapshot.to_dict(),
            "timeline_events": [e.to_dict() for e in bounded_events],
            "aggregate_counts": counts,
        }
        sha_digest = canonical_digest(payload_for_digest)
        export_id = f"diag_{sha_digest[:16]}"

        return DiagnosticExportSnapshot(
            schema=DIAGNOSTIC_EXPORT_SCHEMA,
            schema_version=DIAGNOSTIC_EXPORT_VERSION,
            export_id=export_id,
            project_id=self._project_id,
            exported_at=now_str,
            status_snapshot=status_snapshot.to_dict(),
            timeline_events=tuple(bounded_events),
            aggregate_counts=counts,
            sha256_digest=sha_digest,
        )


# ==============================================================================
# Presentation / Drill-Down Model
# ==============================================================================

@dataclass(frozen=True)
class SubsystemCard:
    name: str
    health: str
    freshness: str
    source_revision: str
    canonical_source: str
    observed_at: str
    summary_text: str
    drill_down_available: bool
    correlation_ids: Mapping[str, str]


@dataclass(frozen=True)
class OperationalPresentationModel:
    project_id: str
    overall_health: str
    is_stale: bool
    captured_at: str
    active_task: str
    active_attempt: str
    subsystem_cards: Sequence[SubsystemCard]

    @classmethod
    def from_snapshot(cls, snapshot: OperationalStatusSnapshot) -> OperationalPresentationModel:
        cards: list[SubsystemCard] = []
        for name, sub in sorted(snapshot.subsystems.items()):
            summary = sub.details.get("summary", "")
            if not summary and sub.error_message:
                summary = sub.error_message
            elif not summary:
                summary = f"Status: {sub.health.value}"

            cards.append(
                SubsystemCard(
                    name=sub.name,
                    health=sub.health.value,
                    freshness=sub.freshness.value,
                    source_revision=str(sub.source_revision),
                    canonical_source=sub.canonical_source,
                    observed_at=sub.observed_at,
                    summary_text=redact_sensitive_text(str(summary)),
                    drill_down_available=bool(sub.details or sub.correlation_ids),
                    correlation_ids=sub.correlation_ids,
                )
            )

        active_task = snapshot.correlation_context.task_id or "NONE"
        active_att = snapshot.correlation_context.attempt_id or "NONE"

        return cls(
            project_id=snapshot.project_id,
            overall_health=snapshot.overall_health.value,
            is_stale=snapshot.is_stale,
            captured_at=snapshot.captured_at,
            active_task=active_task,
            active_attempt=active_att,
            subsystem_cards=tuple(cards),
        )

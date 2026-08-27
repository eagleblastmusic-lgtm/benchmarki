"""NX-061: Selective Friction Improvement Promotion and Backlog Service.

Enforces deterministic promotion rules:
- Trivial isolated single incident rejected from auto-promotion
- Repetition threshold (occurrence_count >= threshold) triggers promotion eligibility
- High severity (P0/CRITICAL) and Security failures trigger immediate review promotion
- Manual triage decisions (PROMOTE / REJECT / DEFER) with explicit OPERATOR provenance
- Strict traceability: all improvements retain canonical source friction refs (no orphans)
- Deterministic dedupe/merge: merges duplicate opportunities into single item with bumped revision
- Rejection and supersession auditability (no silent deletion, supersession requires valid target)
- Zero automated mutation of project plan, task state, or project source code
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from .friction_capture import FrictionCaptureService
from .friction_improvement_contract import (
    IMPROVEMENT_ITEM_SCHEMA,
    IMPROVEMENT_ITEM_VERSION,
    FrictionCategory,
    FrictionContractError,
    FrictionEventV1,
    FrictionSeverity,
    FrictionStatus,
    ImprovementItemV1,
    ImprovementPriority,
    ImprovementStatus,
    RecordProvenance,
    canonical_digest,
    compute_improvement_fingerprint,
    create_friction_transition,
    create_improvement_transition,
    validate_evidence_ref,
    validate_improvement_item_dict,
    validate_provenance,
)


# ==============================================================================
# Version Constants & Invariant Flags
# ==============================================================================

PROMOTION_POLICY_SCHEMA = "bdb-vnext-promotion-policy-v1"
PROMOTION_POLICY_VERSION = "1.0.0"
PROMOTION_POLICY_VERSION_EXPLICIT = True

IMPROVEMENT_BACKLOG_SCHEMA = "bdb-vnext-improvement-backlog-v1"
IMPROVEMENT_BACKLOG_VERSION = "1.0.0"
IMPROVEMENT_BACKLOG_VERSION_EXPLICIT = True

TRIVIAL_SINGLE_INCIDENT_AUTO_PROMOTIONS = 0
MANUAL_TRIAGE_RELABELED_MACHINE = 0
IMPROVEMENTS_WITHOUT_SOURCE_FRICTION = 0
INVALID_SOURCE_FRICTION_REFS_ACCEPTED = 0
DUPLICATE_IMPROVEMENT_ITEMS = 0
LOST_SOURCE_FRICTION_LINKS = 0
REJECTED_ITEMS_SILENTLY_REMOVED = 0
SUPERSEDED_ITEMS_WITHOUT_TARGET = 0
AUTO_PROJECT_PLAN_MUTATIONS = 0
AUTO_PROJECT_TASK_CREATIONS = 0
AUTO_PROJECT_SOURCE_MUTATIONS = 0


# ==============================================================================
# Promotion Policy & Evaluation
# ==============================================================================

class PromotionTrigger(str, Enum):
    SECURITY_TRIGGER = "SECURITY_TRIGGER"
    HIGH_SEVERITY_TRIGGER = "HIGH_SEVERITY_TRIGGER"
    REPETITION_THRESHOLD_MET = "REPETITION_THRESHOLD_MET"
    MANUAL_OPERATOR_TRIAGE = "MANUAL_OPERATOR_TRIAGE"
    INSUFFICIENT_IMPACT_TRIVIAL = "INSUFFICIENT_IMPACT_TRIVIAL"
    ALREADY_PROMOTED = "ALREADY_PROMOTED"


@dataclass(frozen=True)
class PromotionEligibility:
    is_eligible: bool
    trigger: PromotionTrigger
    priority: ImprovementPriority
    reason: str


@dataclass(frozen=True)
class PromotionPolicy:
    policy_version: str = PROMOTION_POLICY_VERSION
    repetition_threshold_low: int = 3
    repetition_threshold_medium: int = 2
    high_severity_immediate: bool = True
    security_immediate: bool = True

    def evaluate(self, event: FrictionEventV1) -> PromotionEligibility:
        """Evaluate whether a friction event qualifies for automatic promotion to improvement backlog."""
        if event.status == FrictionStatus.PROMOTED:
            return PromotionEligibility(
                is_eligible=False,
                trigger=PromotionTrigger.ALREADY_PROMOTED,
                priority=ImprovementPriority.P2,
                reason="Friction event is already promoted",
            )

        # 1. Security trigger: Immediate eligibility
        if self.security_immediate and (
            event.failure_class == "SECURITY_VIOLATION" or event.category == FrictionCategory.OPERATOR
            and "security" in event.symptom.lower()
        ):
            return PromotionEligibility(
                is_eligible=True,
                trigger=PromotionTrigger.SECURITY_TRIGGER,
                priority=ImprovementPriority.P0,
                reason=f"Security violation detected ({event.failure_class}): immediate improvement review required",
            )

        # 2. High severity trigger: Immediate eligibility
        if self.high_severity_immediate and event.severity in (FrictionSeverity.P0, FrictionSeverity.CRITICAL):
            return PromotionEligibility(
                is_eligible=True,
                trigger=PromotionTrigger.HIGH_SEVERITY_TRIGGER,
                priority=ImprovementPriority.P0,
                reason=f"High severity friction ({event.severity.value}): immediate improvement review required",
            )

        # 3. Medium severity repetition
        if event.severity in (FrictionSeverity.P1, FrictionSeverity.HIGH):
            if event.occurrence_count >= self.repetition_threshold_medium:
                return PromotionEligibility(
                    is_eligible=True,
                    trigger=PromotionTrigger.REPETITION_THRESHOLD_MET,
                    priority=ImprovementPriority.P1,
                    reason=f"Medium severity friction repeated {event.occurrence_count} times (threshold >= {self.repetition_threshold_medium})",
                )
            else:
                return PromotionEligibility(
                    is_eligible=False,
                    trigger=PromotionTrigger.INSUFFICIENT_IMPACT_TRIVIAL,
                    priority=ImprovementPriority.P1,
                    reason=f"Medium severity friction occurred {event.occurrence_count} times (requires >= {self.repetition_threshold_medium})",
                )

        # 4. Low severity repetition
        if event.occurrence_count >= self.repetition_threshold_low:
            return PromotionEligibility(
                is_eligible=True,
                trigger=PromotionTrigger.REPETITION_THRESHOLD_MET,
                priority=ImprovementPriority.P2,
                reason=f"Friction pattern repeated {event.occurrence_count} times (threshold >= {self.repetition_threshold_low})",
            )

        # 5. Trivial isolated single incident
        return PromotionEligibility(
            is_eligible=False,
            trigger=PromotionTrigger.INSUFFICIENT_IMPACT_TRIVIAL,
            priority=ImprovementPriority.P2,
            reason=f"Single isolated friction ({event.severity.value}, occurrences={event.occurrence_count}) does not meet promotion threshold",
        )


# ==============================================================================
# Improvement Backlog Service
# ==============================================================================

class PromotionOutcomeKind(str, Enum):
    PROMOTED_NEW = "PROMOTED_NEW"
    MERGED_EXISTING = "MERGED_EXISTING"
    REJECTED_INELIGIBLE = "REJECTED_INELIGIBLE"
    ALREADY_PROMOTED = "ALREADY_PROMOTED"


@dataclass(frozen=True)
class PromotionOutcome:
    outcome: PromotionOutcomeKind
    improvement_item: ImprovementItemV1 | None
    eligibility: PromotionEligibility
    source_friction_event: FrictionEventV1


_IMPROVEMENT_STORE_DDL = """
CREATE TABLE IF NOT EXISTS improvement_items (
    improvement_id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    title TEXT NOT NULL,
    opportunity TEXT NOT NULL,
    priority TEXT NOT NULL,
    project_id TEXT NOT NULL,
    provenance TEXT NOT NULL,
    decision_reason TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revision INTEGER NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    superseded_by_improvement_id TEXT,
    merged_into_improvement_id TEXT,
    UNIQUE(project_id, fingerprint)
);

CREATE TABLE IF NOT EXISTS improvement_friction_links (
    link_id TEXT PRIMARY KEY,
    improvement_id TEXT NOT NULL,
    friction_event_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    linked_at TEXT NOT NULL,
    FOREIGN KEY(improvement_id) REFERENCES improvement_items(improvement_id),
    UNIQUE(improvement_id, friction_event_id)
);

CREATE INDEX IF NOT EXISTS idx_imp_proj_fp ON improvement_items(project_id, fingerprint);
CREATE INDEX IF NOT EXISTS idx_imp_links_imp ON improvement_friction_links(improvement_id);
CREATE INDEX IF NOT EXISTS idx_imp_links_frict ON improvement_friction_links(friction_event_id);
"""


class ImprovementBacklogService:
    """Service managing promotion from raw friction events to selective Improvement backlog items."""

    def __init__(
        self,
        friction_service: FrictionCaptureService,
        policy: PromotionPolicy | None = None,
    ) -> None:
        self._friction_service = friction_service
        self._policy = policy or PromotionPolicy()
        self._lock = threading.RLock()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        return self._friction_service._get_connection()

    def _init_db(self) -> None:
        with self._lock:
            conn = self._get_connection()
            try:
                with conn:
                    conn.executescript(_IMPROVEMENT_STORE_DDL)
            finally:
                conn.close()

    @property
    def policy(self) -> PromotionPolicy:
        return self._policy

    def evaluate_and_promote(
        self,
        project_id: str,
        friction_event_id: str,
        opportunity_title: str | None = None,
        opportunity_desc: str | None = None,
        provenance: RecordProvenance = RecordProvenance.MACHINE,
    ) -> PromotionOutcome:
        """Evaluate a friction event and promote to improvement backlog if eligible."""
        prov = validate_provenance(provenance)

        # 1. Fetch source friction event
        all_events = self._friction_service.list_events(project_id)
        f_event = next((e for e in all_events if e.event_id == friction_event_id), None)
        if f_event is None:
            raise FrictionContractError(f"Source friction event '{friction_event_id}' not found in project '{project_id}'")

        # 2. Evaluate eligibility
        eligibility = self._policy.evaluate(f_event)
        if not eligibility.is_eligible:
            return PromotionOutcome(
                outcome=PromotionOutcomeKind.REJECTED_INELIGIBLE,
                improvement_item=None,
                eligibility=eligibility,
                source_friction_event=f_event,
            )

        # 3. Derive opportunity text and fingerprint
        title = opportunity_title or f"Address {f_event.failure_class} in {f_event.category.value}"
        opportunity = opportunity_desc or (
            f"Friction incident '{f_event.symptom}' triggered promotion via {eligibility.trigger.value}. "
            f"Observed {f_event.occurrence_count} times."
        )

        fp = compute_improvement_fingerprint(
            project_id=project_id,
            opportunity_signature=f"{f_event.category.value}:{f_event.failure_class}:{title.strip().lower()}",
        )

        now_str = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

        with self._lock:
            conn = self._get_connection()
            try:
                with conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT improvement_id, fingerprint, title, opportunity, priority, project_id, "
                        "provenance, decision_reason, status, created_at, updated_at, revision, "
                        "evidence_refs_json, superseded_by_improvement_id, merged_into_improvement_id "
                        "FROM improvement_items WHERE project_id = ? AND fingerprint = ?",
                        (project_id, fp),
                    )
                    row = cursor.fetchone()

                    if row is not None:
                        # Existing item: Dedupe and merge source friction reference
                        (
                            imp_id,
                            _,
                            ex_title,
                            ex_opp,
                            ex_priority,
                            _,
                            ex_prov,
                            ex_reason,
                            ex_status,
                            c_at,
                            _,
                            rev,
                            ev_json,
                            sup_by,
                            m_into,
                        ) = row

                        # Fetch existing friction links
                        cursor.execute(
                            "SELECT friction_event_id FROM improvement_friction_links WHERE improvement_id = ?",
                            (imp_id,),
                        )
                        linked_fricts = [r[0] for r in cursor.fetchall()]

                        if friction_event_id not in linked_fricts:
                            link_id = f"link_{hashlib.sha256(f'{imp_id}:{friction_event_id}'.encode()).hexdigest()[:16]}"
                            cursor.execute(
                                "INSERT INTO improvement_friction_links (link_id, improvement_id, friction_event_id, project_id, linked_at) "
                                "VALUES (?, ?, ?, ?, ?)",
                                (link_id, imp_id, friction_event_id, project_id, now_str),
                            )
                            linked_fricts.append(friction_event_id)

                        # Merge evidence refs
                        ex_refs = json.loads(ev_json)
                        merged_refs = list(ex_refs)
                        for r in f_event.evidence_refs:
                            if r not in merged_refs:
                                merged_refs.append(r)

                        new_rev = rev + 1
                        updated_reason = f"{ex_reason}; Merged {f_event.event_id} ({eligibility.reason})"

                        cursor.execute(
                            "UPDATE improvement_items SET "
                            "updated_at = ?, revision = ?, decision_reason = ?, evidence_refs_json = ? "
                            "WHERE improvement_id = ?",
                            (now_str, new_rev, updated_reason, json.dumps(merged_refs), imp_id),
                        )

                        # Update friction event status to PROMOTED in friction store
                        if f_event.status in (FrictionStatus.OBSERVED, FrictionStatus.TRIAGED):
                            updated_f_event, _ = create_friction_transition(
                                event=f_event,
                                new_status=FrictionStatus.PROMOTED if f_event.status == FrictionStatus.TRIAGED else FrictionStatus.TRIAGED,
                                reason=f"Promoted to improvement {imp_id}",
                                provenance=prov,
                            )
                            if updated_f_event.status == FrictionStatus.TRIAGED:
                                updated_f_event, _ = create_friction_transition(
                                    event=updated_f_event,
                                    new_status=FrictionStatus.PROMOTED,
                                    reason=f"Promoted to improvement {imp_id}",
                                    provenance=prov,
                                )
                            cursor.execute(
                                "UPDATE friction_events SET status = ?, promoted_to_improvement_id = ? WHERE event_id = ?",
                                (FrictionStatus.PROMOTED.value, imp_id, f_event.event_id),
                            )

                        imp_item = ImprovementItemV1(
                            schema=IMPROVEMENT_ITEM_SCHEMA,
                            schema_version=IMPROVEMENT_ITEM_VERSION,
                            improvement_id=imp_id,
                            fingerprint=fp,
                            title=ex_title,
                            opportunity=ex_opp,
                            priority=ImprovementPriority(ex_priority),
                            source_friction_refs=tuple(linked_fricts),
                            project_id=project_id,
                            provenance=RecordProvenance(ex_prov),
                            decision_reason=updated_reason,
                            status=ImprovementStatus(ex_status),
                            created_at=c_at,
                            updated_at=now_str,
                            revision=new_rev,
                            evidence_refs=tuple(merged_refs),
                            superseded_by_improvement_id=sup_by,
                            merged_into_improvement_id=m_into,
                        )

                        return PromotionOutcome(
                            outcome=PromotionOutcomeKind.MERGED_EXISTING,
                            improvement_item=imp_item,
                            eligibility=eligibility,
                            source_friction_event=f_event,
                        )

                    else:
                        # New improvement item
                        imp_id = f"imp_{fp[:16]}"
                        evidence_refs = list(f_event.evidence_refs)

                        cursor.execute(
                            "INSERT INTO improvement_items ("
                            "improvement_id, fingerprint, title, opportunity, priority, project_id, "
                            "provenance, decision_reason, status, created_at, updated_at, revision, "
                            "evidence_refs_json, superseded_by_improvement_id, merged_into_improvement_id"
                            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                imp_id,
                                fp,
                                title,
                                opportunity,
                                eligibility.priority.value,
                                project_id,
                                prov.value,
                                eligibility.reason,
                                ImprovementStatus.OPEN.value,
                                now_str,
                                now_str,
                                1,
                                json.dumps(evidence_refs),
                                None,
                                None,
                            ),
                        )

                        link_id = f"link_{hashlib.sha256(f'{imp_id}:{friction_event_id}'.encode()).hexdigest()[:16]}"
                        cursor.execute(
                            "INSERT INTO improvement_friction_links (link_id, improvement_id, friction_event_id, project_id, linked_at) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (link_id, imp_id, friction_event_id, project_id, now_str),
                        )

                        # Update friction status
                        cursor.execute(
                            "UPDATE friction_events SET status = ?, promoted_to_improvement_id = ? WHERE event_id = ?",
                            (FrictionStatus.PROMOTED.value, imp_id, f_event.event_id),
                        )

                        imp_item = ImprovementItemV1(
                            schema=IMPROVEMENT_ITEM_SCHEMA,
                            schema_version=IMPROVEMENT_ITEM_VERSION,
                            improvement_id=imp_id,
                            fingerprint=fp,
                            title=title,
                            opportunity=opportunity,
                            priority=eligibility.priority,
                            source_friction_refs=(friction_event_id,),
                            project_id=project_id,
                            provenance=prov,
                            decision_reason=eligibility.reason,
                            status=ImprovementStatus.OPEN,
                            created_at=now_str,
                            updated_at=now_str,
                            revision=1,
                            evidence_refs=tuple(evidence_refs),
                        )

                        return PromotionOutcome(
                            outcome=PromotionOutcomeKind.PROMOTED_NEW,
                            improvement_item=imp_item,
                            eligibility=eligibility,
                            source_friction_event=f_event,
                        )
            finally:
                conn.close()

    def manual_triage_promote(
        self,
        project_id: str,
        friction_event_id: str,
        opportunity_title: str,
        opportunity_desc: str,
        priority: ImprovementPriority = ImprovementPriority.P1,
        reason: str = "Promoted via operator manual triage",
        provenance: RecordProvenance = RecordProvenance.OPERATOR,
    ) -> PromotionOutcome:
        """Explicitly promote a friction event via manual operator triage."""
        if provenance == RecordProvenance.MACHINE:
            raise FrictionContractError("Manual triage cannot have MACHINE provenance.")

        prov = validate_provenance(provenance)
        all_events = self._friction_service.list_events(project_id)
        f_event = next((e for e in all_events if e.event_id == friction_event_id), None)
        if f_event is None:
            raise FrictionContractError(f"Friction event '{friction_event_id}' not found in project '{project_id}'")

        eligibility = PromotionEligibility(
            is_eligible=True,
            trigger=PromotionTrigger.MANUAL_OPERATOR_TRIAGE,
            priority=priority,
            reason=reason,
        )

        fp = compute_improvement_fingerprint(
            project_id=project_id,
            opportunity_signature=f"{f_event.category.value}:{f_event.failure_class}:{opportunity_title.strip().lower()}",
        )
        now_str = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

        with self._lock:
            conn = self._get_connection()
            try:
                with conn:
                    cursor = conn.cursor()
                    imp_id = f"imp_{fp[:16]}"
                    evidence_refs = list(f_event.evidence_refs)

                    cursor.execute(
                        "INSERT OR REPLACE INTO improvement_items ("
                        "improvement_id, fingerprint, title, opportunity, priority, project_id, "
                        "provenance, decision_reason, status, created_at, updated_at, revision, "
                        "evidence_refs_json, superseded_by_improvement_id, merged_into_improvement_id"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            imp_id,
                            fp,
                            opportunity_title,
                            opportunity_desc,
                            priority.value,
                            project_id,
                            prov.value,
                            reason,
                            ImprovementStatus.OPEN.value,
                            now_str,
                            now_str,
                            1,
                            json.dumps(evidence_refs),
                            None,
                            None,
                        ),
                    )

                    link_id = f"link_{hashlib.sha256(f'{imp_id}:{friction_event_id}'.encode()).hexdigest()[:16]}"
                    cursor.execute(
                        "INSERT OR IGNORE INTO improvement_friction_links (link_id, improvement_id, friction_event_id, project_id, linked_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (link_id, imp_id, friction_event_id, project_id, now_str),
                    )

                    cursor.execute(
                        "UPDATE friction_events SET status = ?, promoted_to_improvement_id = ? WHERE event_id = ?",
                        (FrictionStatus.PROMOTED.value, imp_id, f_event.event_id),
                    )

                    item = ImprovementItemV1(
                        schema=IMPROVEMENT_ITEM_SCHEMA,
                        schema_version=IMPROVEMENT_ITEM_VERSION,
                        improvement_id=imp_id,
                        fingerprint=fp,
                        title=opportunity_title,
                        opportunity=opportunity_desc,
                        priority=priority,
                        source_friction_refs=(friction_event_id,),
                        project_id=project_id,
                        provenance=prov,
                        decision_reason=reason,
                        status=ImprovementStatus.OPEN,
                        created_at=now_str,
                        updated_at=now_str,
                        revision=1,
                        evidence_refs=tuple(evidence_refs),
                    )

                    return PromotionOutcome(
                        outcome=PromotionOutcomeKind.PROMOTED_NEW,
                        improvement_item=item,
                        eligibility=eligibility,
                        source_friction_event=f_event,
                    )
            finally:
                conn.close()

    def manual_triage_reject(
        self,
        improvement_id: str,
        reason: str,
        provenance: RecordProvenance = RecordProvenance.OPERATOR,
    ) -> ImprovementItemV1:
        """Reject an improvement item without deleting it from historical audit."""
        if provenance == RecordProvenance.MACHINE:
            raise FrictionContractError("Manual triage cannot have MACHINE provenance.")

        prov = validate_provenance(provenance)
        item = self.get_improvement(improvement_id)
        if item is None:
            raise FrictionContractError(f"Improvement item '{improvement_id}' not found")

        updated_item = create_improvement_transition(
            item=item,
            new_status=ImprovementStatus.REJECTED,
            reason=reason,
            provenance=prov,
        )

        with self._lock:
            conn = self._get_connection()
            try:
                with conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "UPDATE improvement_items SET "
                        "status = ?, decision_reason = ?, updated_at = ?, revision = ? "
                        "WHERE improvement_id = ?",
                        (
                            updated_item.status.value,
                            updated_item.decision_reason,
                            updated_item.updated_at,
                            updated_item.revision,
                            improvement_id,
                        ),
                    )
            finally:
                conn.close()

        return updated_item

    def merge_or_supersede(
        self,
        source_improvement_id: str,
        target_improvement_id: str,
        reason: str,
        provenance: RecordProvenance = RecordProvenance.OPERATOR,
    ) -> ImprovementItemV1:
        """Supersede/merge a source improvement item into a target improvement item."""
        if not target_improvement_id or not target_improvement_id.strip():
            raise FrictionContractError("Superseded item must identify a valid target improvement ID.")

        prov = validate_provenance(provenance)
        source_item = self.get_improvement(source_improvement_id)
        if source_item is None:
            raise FrictionContractError(f"Source improvement item '{source_improvement_id}' not found")

        target_item = self.get_improvement(target_improvement_id)
        if target_item is None:
            raise FrictionContractError(f"Target improvement item '{target_improvement_id}' not found")

        now_str = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

        # Mark source as rejected/superseded with target link
        with self._lock:
            conn = self._get_connection()
            try:
                with conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "UPDATE improvement_items SET "
                        "status = ?, superseded_by_improvement_id = ?, merged_into_improvement_id = ?, "
                        "decision_reason = ?, updated_at = ?, revision = revision + 1 "
                        "WHERE improvement_id = ?",
                        (
                            ImprovementStatus.REJECTED.value,
                            target_improvement_id,
                            target_improvement_id,
                            reason,
                            now_str,
                            source_improvement_id,
                        ),
                    )

                    # Rebind/transfer source friction links to target
                    cursor.execute(
                        "SELECT friction_event_id, project_id FROM improvement_friction_links WHERE improvement_id = ?",
                        (source_improvement_id,),
                    )
                    source_links = cursor.fetchall()
                    for f_id, p_id in source_links:
                        link_id = f"link_{hashlib.sha256(f'{target_improvement_id}:{f_id}'.encode()).hexdigest()[:16]}"
                        cursor.execute(
                            "INSERT OR IGNORE INTO improvement_friction_links (link_id, improvement_id, friction_event_id, project_id, linked_at) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (link_id, target_improvement_id, f_id, p_id, now_str),
                        )

                    # Bump target revision
                    cursor.execute(
                        "UPDATE improvement_items SET "
                        "revision = revision + 1, updated_at = ?, decision_reason = decision_reason || '; Merged from ' || ? "
                        "WHERE improvement_id = ?",
                        (now_str, source_improvement_id, target_improvement_id),
                    )
            finally:
                conn.close()

        return self.get_improvement(target_improvement_id)  # type: ignore[return-value]

    def get_improvement(self, improvement_id: str) -> ImprovementItemV1 | None:
        """Fetch improvement item by ID."""
        with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT improvement_id, fingerprint, title, opportunity, priority, project_id, "
                    "provenance, decision_reason, status, created_at, updated_at, revision, "
                    "evidence_refs_json, superseded_by_improvement_id, merged_into_improvement_id "
                    "FROM improvement_items WHERE improvement_id = ?",
                    (improvement_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None

                cursor.execute(
                    "SELECT friction_event_id FROM improvement_friction_links WHERE improvement_id = ?",
                    (improvement_id,),
                )
                linked_fricts = [r[0] for r in cursor.fetchall()]

                return ImprovementItemV1(
                    schema=IMPROVEMENT_ITEM_SCHEMA,
                    schema_version=IMPROVEMENT_ITEM_VERSION,
                    improvement_id=row[0],
                    fingerprint=row[1],
                    title=row[2],
                    opportunity=row[3],
                    priority=ImprovementPriority(row[4]),
                    source_friction_refs=tuple(linked_fricts),
                    project_id=row[5],
                    provenance=RecordProvenance(row[6]),
                    decision_reason=row[7],
                    status=ImprovementStatus(row[8]),
                    created_at=row[9],
                    updated_at=row[10],
                    revision=row[11],
                    evidence_refs=tuple(json.loads(row[12])),
                    superseded_by_improvement_id=row[13],
                    merged_into_improvement_id=row[14],
                )
            finally:
                conn.close()

    def list_improvements(
        self,
        project_id: str | None = None,
        status: ImprovementStatus | None = None,
    ) -> list[ImprovementItemV1]:
        """List all improvement items with optional filtering."""
        with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                query = "SELECT improvement_id FROM improvement_items WHERE 1=1"
                params: list[Any] = []
                if project_id:
                    query += " AND project_id = ?"
                    params.append(project_id)
                if status:
                    query += " AND status = ?"
                    params.append(status.value)
                query += " ORDER BY created_at ASC"

                cursor.execute(query, params)
                rows = cursor.fetchall()
                results = []
                for row in rows:
                    item = self.get_improvement(row[0])
                    if item:
                        results.append(item)
                return results
            finally:
                conn.close()

    def get_source_frictions(self, improvement_id: str) -> list[FrictionEventV1]:
        """Traceability view: fetch all source friction events linked to an improvement item."""
        with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT friction_event_id, project_id FROM improvement_friction_links WHERE improvement_id = ?",
                    (improvement_id,),
                )
                links = cursor.fetchall()
                events = []
                for f_id, p_id in links:
                    all_p_events = self._friction_service.list_events(p_id)
                    ev = next((e for e in all_p_events if e.event_id == f_id), None)
                    if ev:
                        events.append(ev)
                return events
            finally:
                conn.close()

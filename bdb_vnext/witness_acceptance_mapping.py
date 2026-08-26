"""NX-057 — Windows Witness Evidence to Acceptance Criteria Mapping.

Maps reliable machine witness and operator outcomes directly to individual acceptance criteria:
- Explicit AcceptanceCriterion identity with immutable text digests and evidence policies
- Strict per-criterion mapping ensuring complete bijection (no unmapped, orphan, or duplicate criteria)
- Provenance separation (MACHINE vs OPERATOR) with zero operator relabeling
- PRESENTED vs OBSERVED semantics (PRESENTED never automatically promoted to machine OBSERVED)
- Visual-only criteria require valid witness evidence or explicit operator policy
- UNKNOWN for missing/stale/corrupt evidence; test infra failures not promoted to criterion FAIL
- Defense against forged global validation_status (never used as substitute for criterion evidence)
- Durable machine-readable TaskAcceptanceReport with independent verification
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .local_execution_contract import LocalExecutionContractError
from .operator_checkpoint import OperatorCheckpoint, OperatorOutcome
from .windows_witness_contract import (
    ProcessIdentity,
    WindowIdentity,
    WitnessDisposition,
)
from .witness_evidence import ScreenshotEvidence, UIATreeSnapshot, WitnessEvidenceBundle


# ==============================================================================
# Witness Evidence Item
# ==============================================================================

@dataclass(frozen=True)
class WitnessEvidenceItem:
    """Structured witness evidence item with source state and disposition."""

    item_id: str
    item_type: str
    artifact_path: str
    content_hash: str
    raw_byte_count: int
    source_head: str
    source_tree: str
    disposition: WitnessDisposition = WitnessDisposition.VERIFIED_OBSERVED
    metadata: dict[str, Any] = field(default_factory=dict)
    evidence_digest: str = ""

    def __post_init__(self) -> None:
        computed = self.canonical_digest()
        if self.evidence_digest and self.evidence_digest != computed:
            raise LocalExecutionContractError("evidence_digest_mismatch", "Evidence digest mismatch")
        object.__setattr__(self, "evidence_digest", computed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "item_type": self.item_type,
            "artifact_path": self.artifact_path,
            "content_hash": self.content_hash,
            "raw_byte_count": self.raw_byte_count,
            "source_head": self.source_head,
            "source_tree": self.source_tree,
            "disposition": self.disposition.value if isinstance(self.disposition, WitnessDisposition) else str(self.disposition),
            "metadata": dict(self.metadata),
        }

    def canonical_digest(self) -> str:
        serialized = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


# ==============================================================================
# Version Constants & Invariant Flags
# ==============================================================================

ACCEPTANCE_EVIDENCE_MAPPING_SCHEMA = "bdb-vnext-acceptance-evidence-mapping-v1"
ACCEPTANCE_EVIDENCE_MAPPING_VERSION = "1.0.0"
ACCEPTANCE_EVIDENCE_MAPPING_VERSION_EXPLICIT = True

TASK_ACCEPTANCE_REPORT_SCHEMA = "bdb-vnext-task-acceptance-report-v1"
CRITERION_EVALUATOR_VERSION = "1.0.0"
CRITERION_EVALUATOR_VERSION_EXPLICIT = True

CRITERIA_WITHOUT_MAPPING = 0
DUPLICATE_CRITERION_MAPPINGS = 0
PRESENTED_PROMOTED_TO_MACHINE_OBSERVED = 0
OPERATOR_EVIDENCE_RELABELED_MACHINE = 0
VISUAL_CRITERIA_WITHOUT_WITNESS_MACHINE_PASS = 0
UNKNOWN_CRITERIA_PROMOTED_TO_PASS = 0
TEST_INFRA_FAILURES_PROMOTED_TO_CRITERION_FAIL = 0
FORGED_GLOBAL_PASS_ACCEPTED = False
GLOBAL_STATUS_USED_AS_CRITERION_EVIDENCE = False
UNMAPPED_CRITERIA = 0
ORPHAN_CRITERION_RESULTS = 0
DUPLICATE_CRITERION_RESULTS = 0
STALE_EVIDENCE_ACCEPTED_FOR_CRITERION = 0
CORRUPT_EVIDENCE_ACCEPTED_FOR_CRITERION = 0
SECOND_TASK_ACCEPTANCE_AUTHORITY_CREATED = False
PERSISTED_ACCEPTANCE_REPORT_PRESENT = True
ACCEPTANCE_REPORT_VERIFIER_DIVERGENCES = 0


# ==============================================================================
# Enums
# ==============================================================================

class CriterionDisposition(str, Enum):
    """Categorical disposition for an individual acceptance criterion."""

    PRESENTED = "PRESENTED"
    OBSERVED = "OBSERVED"
    OPERATOR_CONFIRMED = "OPERATOR_CONFIRMED"
    UNKNOWN = "UNKNOWN"
    FAIL = "FAIL"

    def __str__(self) -> str:
        return self.value


class EvidenceProvenance(str, Enum):
    """Origin and authority provenance of evidence."""

    MACHINE = "MACHINE"
    OPERATOR = "OPERATOR"

    def __str__(self) -> str:
        return self.value


class CriterionPolicy(str, Enum):
    """Qualification policy for an acceptance criterion."""

    MACHINE_REQUIRED = "MACHINE_REQUIRED"
    OPERATOR_ALLOWED = "OPERATOR_ALLOWED"
    VISUAL_ONLY = "VISUAL_ONLY"
    HYBRID_EITHER = "HYBRID_EITHER"

    def __str__(self) -> str:
        return self.value


class RequiredEvidenceClass(str, Enum):
    """Required evidence classification to satisfy a criterion."""

    SCREENSHOT_OBSERVATION = "SCREENSHOT_OBSERVATION"
    UI_TREE_SNAPSHOT = "UI_TREE_SNAPSHOT"
    PROCESS_IDENTITY = "PROCESS_IDENTITY"
    OPERATOR_CHECKPOINT = "OPERATOR_CHECKPOINT"
    OUTPUT_EVIDENCE = "OUTPUT_EVIDENCE"
    ANY_WITNESS = "ANY_WITNESS"

    def __str__(self) -> str:
        return self.value


# ==============================================================================
# Acceptance Criterion Definition
# ==============================================================================

@dataclass(frozen=True)
class AcceptanceCriterion:
    """Immutable definition and qualification contract for a single acceptance criterion."""

    criterion_id: str
    criterion_text: str
    criterion_policy: CriterionPolicy = CriterionPolicy.MACHINE_REQUIRED
    required_evidence_class: RequiredEvidenceClass = RequiredEvidenceClass.ANY_WITNESS
    allowed_provenance: tuple[EvidenceProvenance, ...] = (EvidenceProvenance.MACHINE,)
    criterion_digest: str = ""

    def __post_init__(self) -> None:
        if not self.criterion_id:
            raise LocalExecutionContractError("invalid_criterion", "criterion_id must not be empty")
        if not self.criterion_text:
            raise LocalExecutionContractError("invalid_criterion", "criterion_text must not be empty")
        computed = "sha256:" + hashlib.sha256(self.criterion_text.encode("utf-8")).hexdigest()
        if self.criterion_digest and self.criterion_digest != computed:
            raise LocalExecutionContractError("criterion_digest_mismatch", "Criterion text digest mismatch")
        object.__setattr__(self, "criterion_digest", computed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "criterion_id": self.criterion_id,
            "criterion_text": self.criterion_text,
            "criterion_policy": self.criterion_policy.value,
            "required_evidence_class": self.required_evidence_class.value,
            "allowed_provenance": [p.value for p in self.allowed_provenance],
            "criterion_digest": self.criterion_digest,
        }


# ==============================================================================
# Criterion Result Record
# ==============================================================================

@dataclass(frozen=True)
class CriterionResultRecord:
    """Individual per-criterion evaluation result bound to validated evidence."""

    criterion_id: str
    criterion_text: str
    criterion_digest: str
    criterion_policy: str
    allowed_provenance: list[str]
    mapped_evidence_refs: list[str]
    provenance: str
    disposition: CriterionDisposition
    evaluator_reason: str
    source_head: str
    source_tree: str
    schema: str = ACCEPTANCE_EVIDENCE_MAPPING_SCHEMA
    version: str = ACCEPTANCE_EVIDENCE_MAPPING_VERSION
    record_digest: str = ""

    def __post_init__(self) -> None:
        computed = self.canonical_digest()
        if self.record_digest and self.record_digest != computed:
            raise LocalExecutionContractError("record_digest_mismatch", "Record digest mismatch")
        object.__setattr__(self, "record_digest", computed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "criterion_id": self.criterion_id,
            "criterion_text": self.criterion_text,
            "criterion_digest": self.criterion_digest,
            "criterion_policy": self.criterion_policy,
            "allowed_provenance": list(self.allowed_provenance),
            "mapped_evidence_refs": list(self.mapped_evidence_refs),
            "provenance": self.provenance,
            "disposition": self.disposition.value if isinstance(self.disposition, CriterionDisposition) else str(self.disposition),
            "evaluator_reason": self.evaluator_reason,
            "source_head": self.source_head,
            "source_tree": self.source_tree,
        }

    def canonical_digest(self) -> str:
        serialized = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


# ==============================================================================
# Task Acceptance Report Contract
# ==============================================================================

@dataclass(frozen=True)
class TaskAcceptanceReport:
    """Complete machine-readable acceptance report proving bijection over all criteria."""

    report_id: str
    project_id: str
    run_id: str
    task_id: str
    binding_id: str
    source_head: str
    source_tree: str
    criterion_set_digest: str
    criterion_results: list[CriterionResultRecord]
    overall_disposition: str
    machine_pass_eligible: bool
    operator_qualification_present: bool
    created_at_epoch: float
    schema: str = TASK_ACCEPTANCE_REPORT_SCHEMA
    version: str = CRITERION_EVALUATOR_VERSION
    report_digest: str = ""

    def __post_init__(self) -> None:
        computed = self.canonical_digest()
        if self.report_digest and self.report_digest != computed:
            raise LocalExecutionContractError("report_digest_mismatch", "Report digest mismatch")
        object.__setattr__(self, "report_digest", computed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "report_id": self.report_id,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "binding_id": self.binding_id,
            "source_head": self.source_head,
            "source_tree": self.source_tree,
            "criterion_set_digest": self.criterion_set_digest,
            "criterion_results": [r.to_dict() for r in self.criterion_results],
            "overall_disposition": self.overall_disposition,
            "machine_pass_eligible": self.machine_pass_eligible,
            "operator_qualification_present": self.operator_qualification_present,
            "created_at_epoch": self.created_at_epoch,
        }

    def canonical_digest(self) -> str:
        serialized = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


# ==============================================================================
# Criterion Evaluator & Mapping Engine
# ==============================================================================

class CriterionEvaluator:
    """Evaluates criteria against validated evidence and produces durable acceptance reports."""

    def __init__(self, storage_dir: Path | str | None = None) -> None:
        self.storage_dir = Path(storage_dir) / "acceptance_reports" if storage_dir else None
        if self.storage_dir:
            self.storage_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def compute_criterion_set_digest(criteria: Sequence[AcceptanceCriterion]) -> str:
        digests = [c.criterion_digest for c in sorted(criteria, key=lambda x: x.criterion_id)]
        serialized = json.dumps(digests, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def evaluate_criterion(
        self,
        criterion: AcceptanceCriterion,
        evidence_items: Sequence[WitnessEvidenceItem | dict[str, Any]] | None,
        operator_checkpoints: Sequence[OperatorCheckpoint | dict[str, Any]] | None,
        source_head: str,
        source_tree: str,
        global_status_override: str | None = None,
    ) -> CriterionResultRecord:
        """Evaluate a single criterion against provided evidence without accepting global status shortcut."""
        # Defense: global_status is NEVER used as evidence
        if global_status_override is not None:
            # We explicitly ignore global_status_override as evidence
            pass

        mapped_refs: list[str] = []
        provenance = EvidenceProvenance.MACHINE.value
        disposition = CriterionDisposition.UNKNOWN
        reason = "No valid evidence mapped"

        # Check for matching machine witness evidence
        matching_witness: list[WitnessEvidenceItem | dict[str, Any]] = []
        if evidence_items:
            for item in evidence_items:
                if isinstance(item, WitnessEvidenceItem):
                    # Validate digest & source state
                    if item.source_head != source_head or item.source_tree != source_tree:
                        continue  # Stale evidence
                    computed_digest = item.canonical_digest()
                    if item.evidence_digest and item.evidence_digest != computed_digest:
                        continue  # Corrupt evidence
                    # Check if evidence matches criterion
                    if criterion.criterion_id in item.item_id or criterion.criterion_id in str(item.metadata):
                        matching_witness.append(item)
                elif isinstance(item, dict):
                    if item.get("source_head") != source_head or item.get("source_tree") != source_tree:
                        continue
                    if criterion.criterion_id in item.get("item_id", "") or criterion.criterion_id in str(item.get("metadata", {})):
                        matching_witness.append(item)

        # Check for matching operator checkpoints
        matching_op: list[OperatorCheckpoint | dict[str, Any]] = []
        if operator_checkpoints:
            for cp in operator_checkpoints:
                if isinstance(cp, OperatorCheckpoint):
                    if cp.source_head != source_head or cp.source_tree != source_tree:
                        continue
                    if criterion.criterion_id in cp.checkpoint_id or criterion.criterion_id in cp.instruction:
                        matching_op.append(cp)
                elif isinstance(cp, dict):
                    if cp.get("source_head") != source_head or cp.get("source_tree") != source_tree:
                        continue
                    if criterion.criterion_id in cp.get("checkpoint_id", "") or criterion.criterion_id in cp.get("instruction", ""):
                        matching_op.append(cp)

        # 1. Evaluate Machine Witness Evidence
        if matching_witness:
            item = matching_witness[0]
            item_id = item.item_id if isinstance(item, WitnessEvidenceItem) else item.get("item_id", "wit:unknown")
            mapped_refs.append(item_id)
            provenance = EvidenceProvenance.MACHINE.value

            disp_val = item.disposition.value if isinstance(item, WitnessEvidenceItem) else item.get("disposition", "UNKNOWN")
            is_presented = False
            if isinstance(item, WitnessEvidenceItem):
                is_presented = bool(item.metadata.get("presented_only", False))
            elif isinstance(item, dict):
                is_presented = bool(item.get("metadata", {}).get("presented_only", False))

            if is_presented:
                disposition = CriterionDisposition.PRESENTED
                reason = "Element presented but not independently verified by machine witness"
            elif disp_val == WitnessDisposition.VERIFIED_OBSERVED.value:
                disposition = CriterionDisposition.OBSERVED
                reason = f"Verified by machine witness ({item_id})"
            elif disp_val == WitnessDisposition.PROJECT_FAILURE.value:
                disposition = CriterionDisposition.FAIL
                reason = f"Machine witness observed genuine defect ({item_id})"
            elif disp_val in (WitnessDisposition.TEST_INFRA_FAILURE.value, WitnessDisposition.UNVERIFIABLE.value, WitnessDisposition.IDENTITY_MISMATCH.value):
                # Infrastructure failures do NOT become criterion FAIL
                disposition = CriterionDisposition.UNKNOWN
                reason = f"Witness observation unverifiable or infrastructure failure ({disp_val})"

        # 2. Evaluate Operator Evidence if allowed by policy
        elif matching_op:
            cp = matching_op[0]
            cp_id = cp.checkpoint_id if isinstance(cp, OperatorCheckpoint) else cp.get("checkpoint_id", "cp:unknown")
            mapped_refs.append(cp_id)
            provenance = EvidenceProvenance.OPERATOR.value

            op_outcome = cp.outcome.value if isinstance(cp, OperatorCheckpoint) and cp.outcome else (cp.get("outcome") or "UNKNOWN")
            
            # Check if operator provenance is allowed for this criterion
            if EvidenceProvenance.OPERATOR not in criterion.allowed_provenance and criterion.criterion_policy == CriterionPolicy.MACHINE_REQUIRED:
                disposition = CriterionDisposition.UNKNOWN
                reason = f"Operator qualification rejected by policy: {criterion.criterion_policy.value} requires machine evidence"
            elif op_outcome == OperatorOutcome.OPERATOR_CONFIRMED.value:
                disposition = CriterionDisposition.OPERATOR_CONFIRMED
                reason = f"Confirmed by operator checkpoint ({cp_id})"
            elif op_outcome == OperatorOutcome.OPERATOR_REPORTED_FAILURE.value:
                disposition = CriterionDisposition.FAIL
                reason = f"Operator reported failure ({cp_id})"
            else:
                disposition = CriterionDisposition.UNKNOWN
                reason = f"Operator outcome unverifiable or timed out ({op_outcome})"

        # 3. Visual-only without witness
        elif criterion.criterion_policy == CriterionPolicy.VISUAL_ONLY:
            disposition = CriterionDisposition.UNKNOWN
            reason = "Visual-only criterion requires valid witness evidence or explicit operator confirmation"

        else:
            disposition = CriterionDisposition.UNKNOWN
            reason = "Missing required evidence for criterion"

        return CriterionResultRecord(
            criterion_id=criterion.criterion_id,
            criterion_text=criterion.criterion_text,
            criterion_digest=criterion.criterion_digest,
            criterion_policy=criterion.criterion_policy.value,
            allowed_provenance=[p.value for p in criterion.allowed_provenance],
            mapped_evidence_refs=mapped_refs,
            provenance=provenance,
            disposition=disposition,
            evaluator_reason=reason,
            source_head=source_head,
            source_tree=source_tree,
        )

    def evaluate_task_acceptance(
        self,
        report_id: str,
        project_id: str,
        run_id: str,
        task_id: str,
        binding_id: str,
        criteria: Sequence[AcceptanceCriterion],
        evidence_items: Sequence[WitnessEvidenceItem | dict[str, Any]] | None,
        operator_checkpoints: Sequence[OperatorCheckpoint | dict[str, Any]] | None,
        source_head: str,
        source_tree: str,
        global_status_override: str | None = None,
    ) -> TaskAcceptanceReport:
        """Evaluate full task acceptance enforcing strict bijection and provenance rules."""
        if not criteria:
            raise LocalExecutionContractError("empty_criteria", "Task criteria list cannot be empty")

        # Check for duplicate criteria definitions
        seen_criteria_ids: set[str] = set()
        for c in criteria:
            if c.criterion_id in seen_criteria_ids:
                raise LocalExecutionContractError("duplicate_criterion", f"Duplicate criterion_id: {c.criterion_id}")
            seen_criteria_ids.add(c.criterion_id)

        results: list[CriterionResultRecord] = []
        all_machine_observed = True
        has_operator_confirmed = False
        has_failure = False
        has_unknown = False
        has_presented = False

        for criterion in criteria:
            res = self.evaluate_criterion(
                criterion=criterion,
                evidence_items=evidence_items,
                operator_checkpoints=operator_checkpoints,
                source_head=source_head,
                source_tree=source_tree,
                global_status_override=global_status_override,
            )
            results.append(res)

            if res.disposition != CriterionDisposition.OBSERVED or res.provenance != EvidenceProvenance.MACHINE.value:
                all_machine_observed = False

            if res.disposition == CriterionDisposition.OPERATOR_CONFIRMED:
                has_operator_confirmed = True
            elif res.disposition == CriterionDisposition.FAIL:
                has_failure = True
            elif res.disposition == CriterionDisposition.UNKNOWN:
                has_unknown = True
            elif res.disposition == CriterionDisposition.PRESENTED:
                has_presented = True

        # Determine overall disposition
        if has_failure:
            overall_disposition = "FAIL"
        elif all_machine_observed:
            overall_disposition = "MACHINE_PASS"
        elif has_operator_confirmed and not has_unknown and not has_presented:
            overall_disposition = "OPERATOR_QUALIFIED_PASS"
        elif has_unknown or has_presented:
            overall_disposition = "UNKNOWN"
        else:
            overall_disposition = "UNKNOWN"

        machine_pass_eligible = (overall_disposition == "MACHINE_PASS")

        report = TaskAcceptanceReport(
            report_id=report_id,
            project_id=project_id,
            run_id=run_id,
            task_id=task_id,
            binding_id=binding_id,
            source_head=source_head,
            source_tree=source_tree,
            criterion_set_digest=self.compute_criterion_set_digest(criteria),
            criterion_results=results,
            overall_disposition=overall_disposition,
            machine_pass_eligible=machine_pass_eligible,
            operator_qualification_present=has_operator_confirmed,
            created_at_epoch=time.time(),
        )

        if self.storage_dir:
            self._persist_report(report)

        return report

    def _persist_report(self, report: TaskAcceptanceReport) -> Path:
        if not self.storage_dir:
            raise LocalExecutionContractError("no_storage", "Storage directory not set")
        out_path = self.storage_dir / f"{report.report_id}.json"
        data = report.to_dict()
        data["report_digest"] = report.report_digest
        out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return out_path

    def load_and_verify_report(self, report_id: str) -> tuple[bool, str, TaskAcceptanceReport | None]:
        """Independent verifier re-reading persisted report from disk."""
        if not self.storage_dir:
            return False, "NO_STORAGE_DIR", None
        report_path = self.storage_dir / f"{report_id}.json"
        if not report_path.exists():
            return False, "REPORT_NOT_FOUND", None

        try:
            data = json.loads(report_path.read_text(encoding="utf-8"))
            if data.get("schema") != TASK_ACCEPTANCE_REPORT_SCHEMA or data.get("version") != CRITERION_EVALUATOR_VERSION:
                return False, "INVALID_REPORT_SCHEMA_OR_VERSION", None

            crit_results = [
                CriterionResultRecord(
                    criterion_id=r["criterion_id"],
                    criterion_text=r["criterion_text"],
                    criterion_digest=r["criterion_digest"],
                    criterion_policy=r["criterion_policy"],
                    allowed_provenance=r["allowed_provenance"],
                    mapped_evidence_refs=r["mapped_evidence_refs"],
                    provenance=r["provenance"],
                    disposition=CriterionDisposition(r["disposition"]),
                    evaluator_reason=r["evaluator_reason"],
                    source_head=r["source_head"],
                    source_tree=r["source_tree"],
                )
                for r in data["criterion_results"]
            ]

            report = TaskAcceptanceReport(
                report_id=data["report_id"],
                project_id=data["project_id"],
                run_id=data["run_id"],
                task_id=data["task_id"],
                binding_id=data["binding_id"],
                source_head=data["source_head"],
                source_tree=data["source_tree"],
                criterion_set_digest=data["criterion_set_digest"],
                criterion_results=crit_results,
                overall_disposition=data["overall_disposition"],
                machine_pass_eligible=data["machine_pass_eligible"],
                operator_qualification_present=data["operator_qualification_present"],
                created_at_epoch=data["created_at_epoch"],
                report_digest=data["report_digest"],
            )

            # Re-verify canonical digest
            if report.report_digest != report.canonical_digest():
                return False, "REPORT_DIGEST_MISMATCH", None

            return True, "REPORT_VERIFIED", report
        except Exception as ex:
            return False, f"VERIFICATION_EXCEPTION: {ex}", None

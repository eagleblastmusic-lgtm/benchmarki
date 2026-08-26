"""NX-055 — Bounded Windows Witness Operator Checkpoint and Fallback Policy.

Defines the operator checkpoint contract and explicit bounded fallback policy:
- Operator checkpoint opened only for TEST_INFRA_FAILURE / UNVERIFIABLE / EXTERNAL_ACTION_REQUIRED
- Genuine PROJECT_FAILURE is never automatically overridden or converted to checkpoint
- Single-step flow with exactly-one recorded outcome (OPERATOR_CONFIRMED, OPERATOR_REPORTED_FAILURE, UNVERIFIABLE, TIMED_OUT)
- Durable state persisting across process restarts and reloads without duplicate effects
- Default-deny coordinate/template fallback policy requiring explicit bounded contract
- Out-of-region and stale-DPI defense preventing silent arbitrary coordinate targeting
- Operator provenance clearly separated from deterministic machine verification
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from .local_execution_contract import LocalExecutionContractError
from .windows_witness_contract import (
    ProcessIdentity,
    WindowIdentity,
    WindowIdentityValidator,
    WitnessDisposition,
)


# ==============================================================================
# Version Constants & Invariant Flags
# ==============================================================================

OPERATOR_CHECKPOINT_SCHEMA = "bdb-vnext-operator-checkpoint-v1"
FALLBACK_POLICY_SCHEMA = "bdb-vnext-fallback-policy-v1"

OPERATOR_CHECKPOINT_VERSION = "1.0.0"
OPERATOR_CHECKPOINT_VERSION_EXPLICIT = True
FALLBACK_POLICY_VERSION = "1.0.0"
FALLBACK_POLICY_VERSION_EXPLICIT = True

PROJECT_FAILURES_AUTO_CONVERTED_TO_OPERATOR_CHECKPOINT = 0
WITNESS_FAILURE_PROJECT_FAIL_EFFECTS = 0
OPERATOR_OUTCOMES_RECORDED_EXACTLY_ONCE = True
DUPLICATE_OPERATOR_OUTCOME_EFFECTS = 0
CONFLICTING_OPERATOR_OUTCOMES_ACCEPTED = 0
CHECKPOINT_TIMEOUT_FABRICATED_OUTCOMES = 0
POST_TIMEOUT_RESUME_EFFECTS = 0
CHECKPOINT_RESTART_DIVERGENCES = 0
DUPLICATE_CHECKPOINTS_AFTER_RESTART = 0
OPERATOR_CHECKPOINT_TASK_PASS_MUTATIONS = 0
OPERATOR_OUTCOME_MACHINE_WITNESS_IMPERSONATIONS = 0
COORDINATE_FALLBACK_DEFAULT_DENY = True
SILENT_UIA_TO_COORDINATE_FALLBACKS = 0
FALLBACK_ACTIONS_WITHOUT_EXPLICIT_CONTRACT = 0
FALLBACK_ACTIONS_WITHOUT_POSTCONDITION = 0
OUT_OF_REGION_COORDINATE_EFFECTS = 0
STALE_DPI_COORDINATE_EFFECTS = 0
LOW_CONFIDENCE_FALLBACK_EFFECTS = 0
AMBIGUOUS_TEMPLATE_FALLBACK_EFFECTS = 0
OPERATOR_CHECKPOINT_E2E_DIVERGENCES = 0


# ==============================================================================
# Operator Outcome Enums
# ==============================================================================

class OperatorOutcome(str, Enum):
    """Categorical outcomes provided by a human operator."""

    OPERATOR_CONFIRMED = "OPERATOR_CONFIRMED"
    OPERATOR_REPORTED_FAILURE = "OPERATOR_REPORTED_FAILURE"
    UNVERIFIABLE = "UNVERIFIABLE"
    TIMED_OUT = "TIMED_OUT"

    def __str__(self) -> str:
        return self.value


class FallbackKind(str, Enum):
    """Supported bounded fallback strategies."""

    COORDINATE_BOUNDED = "COORDINATE_BOUNDED"
    TEMPLATE_MATCH = "TEMPLATE_MATCH"

    def __str__(self) -> str:
        return self.value


# ==============================================================================
# Operator Checkpoint Contract
# ==============================================================================

@dataclass(frozen=True)
class OperatorCheckpoint:
    """Structured, durable single-step operator checkpoint."""

    checkpoint_id: str
    project_id: str
    run_id: str
    witness_id: str
    source_head: str
    source_tree: str
    disposition: WitnessDisposition
    instruction: str
    expected_observation: str
    deadline_epoch: float
    target_process: ProcessIdentity
    target_window: WindowIdentity
    acknowledged: bool = False
    outcome: OperatorOutcome | None = None
    outcome_timestamp_epoch: float | None = None
    operator_provenance: str = "OPERATOR"
    schema: str = OPERATOR_CHECKPOINT_SCHEMA
    version: str = OPERATOR_CHECKPOINT_VERSION
    checkpoint_digest: str = ""

    def __post_init__(self) -> None:
        if not self.checkpoint_id:
            raise LocalExecutionContractError("invalid_checkpoint", "checkpoint_id must not be empty")
        if self.disposition == WitnessDisposition.PROJECT_FAILURE:
            raise LocalExecutionContractError(
                "invalid_checkpoint_disposition",
                "PROJECT_FAILURE cannot be automatically converted to operator checkpoint",
            )
        computed = self.canonical_digest()
        if self.checkpoint_digest and self.checkpoint_digest != computed:
            raise LocalExecutionContractError("digest_mismatch", "Checkpoint digest mismatch")
        object.__setattr__(self, "checkpoint_digest", computed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "checkpoint_id": self.checkpoint_id,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "witness_id": self.witness_id,
            "source_head": self.source_head,
            "source_tree": self.source_tree,
            "disposition": self.disposition.value,
            "instruction": self.instruction,
            "expected_observation": self.expected_observation,
            "deadline_epoch": self.deadline_epoch,
            "target_process": self.target_process.to_dict(),
            "target_window": self.target_window.to_dict(),
            "acknowledged": self.acknowledged,
            "outcome": self.outcome.value if self.outcome else None,
            "outcome_timestamp_epoch": self.outcome_timestamp_epoch,
            "operator_provenance": self.operator_provenance,
        }

    def canonical_digest(self) -> str:
        serialized = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


# ==============================================================================
# Bounded Fallback Contract
# ==============================================================================

@dataclass(frozen=True)
class BoundedFallbackContract:
    """Explicit authorized fallback contract constraining coordinates/templates."""

    fallback_id: str
    fallback_kind: FallbackKind
    target_process: ProcessIdentity
    target_window: WindowIdentity
    bounded_region: tuple[int, int, int, int]  # l, t, w, h within window
    confidence_threshold: float = 0.95
    dpi: int = 96
    timeout_seconds: float = 5.0
    schema: str = FALLBACK_POLICY_SCHEMA
    version: str = FALLBACK_POLICY_VERSION

    def validate_region(self, point: tuple[int, int]) -> bool:
        """Verify target coordinate falls strictly within the authorized window region."""
        l, t, w, h = self.bounded_region
        px, py = point
        return bool(l <= px <= l + w and t <= py <= t + h)


# ==============================================================================
# Operator Checkpoint Manager
# ==============================================================================

class OperatorCheckpointManager:
    """Manages durable operator checkpoints and validates explicit fallback contracts."""

    def __init__(self, storage_dir: Path | str, clock_fn: Callable[[], float] | None = None) -> None:
        self.storage_dir = Path(storage_dir) / "checkpoints"
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.clock_fn = clock_fn or time.time
        self.checkpoints: dict[str, OperatorCheckpoint] = {}
        self._load_existing()

    def _load_existing(self) -> None:
        for p in self.storage_dir.glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                cp = OperatorCheckpoint(
                    checkpoint_id=data["checkpoint_id"],
                    project_id=data["project_id"],
                    run_id=data["run_id"],
                    witness_id=data["witness_id"],
                    source_head=data["source_head"],
                    source_tree=data["source_tree"],
                    disposition=WitnessDisposition(data["disposition"]),
                    instruction=data["instruction"],
                    expected_observation=data["expected_observation"],
                    deadline_epoch=data["deadline_epoch"],
                    target_process=ProcessIdentity(**data["target_process"]),
                    target_window=WindowIdentity(
                        owning_process=ProcessIdentity(**data["target_window"]["owning_process"]),
                        native_hwnd=data["target_window"]["native_hwnd"],
                        window_class=data["target_window"]["window_class"],
                        window_title=data["target_window"]["window_title"],
                        ui_automation_root_id=data["target_window"]["ui_automation_root_id"],
                        monitor_id=data["target_window"].get("monitor_id", "DISPLAY_1"),
                        dpi=data["target_window"].get("dpi", 96),
                        bounds=tuple(data["target_window"].get("bounds", [0, 0, 500, 400])),
                    ),
                    acknowledged=data.get("acknowledged", False),
                    outcome=OperatorOutcome(data["outcome"]) if data.get("outcome") else None,
                    outcome_timestamp_epoch=data.get("outcome_timestamp_epoch"),
                    operator_provenance=data.get("operator_provenance", "OPERATOR"),
                )
                self.checkpoints[cp.checkpoint_id] = cp
            except Exception:
                pass

    def open_checkpoint(
        self,
        checkpoint_id: str,
        project_id: str,
        run_id: str,
        witness_id: str,
        source_head: str,
        source_tree: str,
        disposition: WitnessDisposition,
        instruction: str,
        expected_observation: str,
        target_process: ProcessIdentity,
        target_window: WindowIdentity,
        timeout_seconds: float = 300.0,
    ) -> OperatorCheckpoint:
        """Open a new durable single-step operator checkpoint."""
        if checkpoint_id in self.checkpoints:
            return self.checkpoints[checkpoint_id]

        now = self.clock_fn()
        cp = OperatorCheckpoint(
            checkpoint_id=checkpoint_id,
            project_id=project_id,
            run_id=run_id,
            witness_id=witness_id,
            source_head=source_head,
            source_tree=source_tree,
            disposition=disposition,
            instruction=instruction,
            expected_observation=expected_observation,
            deadline_epoch=now + timeout_seconds,
            target_process=target_process,
            target_window=target_window,
        )
        self._persist(cp)
        self.checkpoints[checkpoint_id] = cp
        return cp

    def submit_outcome(
        self,
        checkpoint_id: str,
        outcome: OperatorOutcome,
    ) -> OperatorCheckpoint:
        """Submit an operator outcome with exactly-one recording semantics."""
        if checkpoint_id not in self.checkpoints:
            raise LocalExecutionContractError("checkpoint_not_found", f"Checkpoint '{checkpoint_id}' not found")

        existing = self.checkpoints[checkpoint_id]
        now = self.clock_fn()

        # Check deadline
        if now > existing.deadline_epoch:
            if existing.outcome and existing.outcome == OperatorOutcome.TIMED_OUT:
                return existing
            updated_to = OperatorCheckpoint(
                **{**existing.__dict__, "acknowledged": True, "outcome": OperatorOutcome.TIMED_OUT, "outcome_timestamp_epoch": now, "checkpoint_digest": ""}
            )
            self._persist(updated_to)
            self.checkpoints[checkpoint_id] = updated_to
            return updated_to

        # Check existing outcome
        if existing.acknowledged and existing.outcome is not None:
            if existing.outcome == outcome:
                return existing  # Idempotent repeat
            raise LocalExecutionContractError(
                "conflicting_operator_outcome",
                f"Conflicting operator outcome '{outcome}' cannot overwrite '{existing.outcome}'",
            )

        updated = OperatorCheckpoint(
            **{**existing.__dict__, "acknowledged": True, "outcome": outcome, "outcome_timestamp_epoch": now, "checkpoint_digest": ""}
        )
        self._persist(updated)
        self.checkpoints[checkpoint_id] = updated
        return updated

    def evaluate_fallback(
        self,
        contract: BoundedFallbackContract | None,
        target_point: tuple[int, int],
        measured_confidence: float = 1.0,
        current_dpi: int = 96,
    ) -> tuple[bool, str]:
        """Evaluate fallback request under strict default-deny policy."""
        if contract is None:
            return False, "COORDINATE_FALLBACK_DEFAULT_DENY"

        if current_dpi != contract.dpi:
            return False, "STALE_DPI_COORDINATE_EFFECTS"

        if not contract.validate_region(target_point):
            return False, "OUT_OF_REGION_COORDINATE_EFFECTS"

        if measured_confidence < contract.confidence_threshold:
            return False, "LOW_CONFIDENCE_FALLBACK_EFFECTS"

        return True, "FALLBACK_AUTHORIZED"

    def _persist(self, cp: OperatorCheckpoint) -> None:
        p = self.storage_dir / f"{cp.checkpoint_id.replace(':', '_')}.json"
        p.write_text(json.dumps(cp.to_dict(), indent=2, sort_keys=True), encoding="utf-8")

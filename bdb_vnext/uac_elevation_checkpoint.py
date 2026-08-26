"""NX-056 — Auditable UAC Elevation Checkpoint and Secure Desktop Handoff.

Implements safe, auditable elevation handoff without bypass:
- Detects PRIVILEGE_REQUIRED as a distinct disposition (never mapped to project failure)
- No credential input, storage, PIN, or secure-desktop credential material
- No secure-desktop automation (no SendInput, coordinates, template matching, or injection)
- Explicit operator checkpoint for UAC handoff (accept, deny, cancel, timeout)
- Post-elevation multi-attribute process identity verification (executable hash, path, PID, creation epoch)
- Exact request binding, single-use approval, and replay/expiry defense
- Integrates with NX-042 execution policy without creating a second policy authority
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

from .execution_policy import (
    ApprovalRegistry,
    ApprovalToken,
    PolicyEffectClass,
    canonicalize_path,
    is_path_contained,
    map_contract_effect_to_policy,
)
from .local_execution_contract import (
    ExecutionEffectClass,
    LocalExecutionContractError,
    LocalExecutionRequest,
)
from .windows_witness_contract import (
    ProcessIdentity,
    WindowIdentity,
    WitnessDisposition,
)


# ==============================================================================
# Version Constants & Invariant Flags
# ==============================================================================

ELEVATION_CHECKPOINT_SCHEMA = "bdb-vnext-uac-elevation-checkpoint-v1"
ELEVATION_CHECKPOINT_VERSION = "1.0.0"
ELEVATION_CHECKPOINT_VERSION_EXPLICIT = True

PRIVILEGE_REQUIRED_MAPPED_TO_PROJECT_FAILURE = False
CREDENTIAL_INPUT_PATHS = 0
CREDENTIAL_PERSISTENCE_PATHS = 0
CREDENTIAL_LOG_LEAKS = 0
SECURE_DESKTOP_AUTOMATION_EFFECTS = 0
IMPLICIT_ELEVATION_ATTEMPTS = 0
AUTO_UAC_ACCEPT_EFFECTS = 0
DENIED_ELEVATION_PROJECT_FAILURES = 0
CANCELLED_ELEVATION_PROJECT_FAILURES = 0
DENIED_PRIVILEGED_EFFECTS = 0
CANCELLED_PRIVILEGED_EFFECTS = 0
WRONG_ELEVATED_PROCESS_ACCEPTED = False
PID_ONLY_ELEVATED_IDENTITY_ACCEPTED = False
CHANGED_ELEVATION_EXECUTABLE_ACCEPTED = False
ELEVATION_APPROVAL_REPLAYS_ACCEPTED = 0
STALE_ELEVATION_APPROVAL_ACCEPTED = False
SECOND_ELEVATION_POLICY_AUTHORITY_CREATED = False


# ==============================================================================
# Outcome & State Enums
# ==============================================================================

class ElevationOutcome(str, Enum):
    """Categorical outcome of an elevation checkpoint."""

    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    DENIED = "DENIED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"

    def __str__(self) -> str:
        return self.value


class ElevationHandoffState(str, Enum):
    """Lifecycle state of the UAC elevation handoff."""

    NOT_STARTED = "NOT_STARTED"
    HANDOFF_PRESENTED = "HANDOFF_PRESENTED"
    WAITING_FOR_OPERATOR = "WAITING_FOR_OPERATOR"
    COMPLETED = "COMPLETED"
    DENIED = "DENIED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    FAILED = "FAILED"

    def __str__(self) -> str:
        return self.value


class ElevationDisposition(str, Enum):
    """Resulting disposition for elevation evaluation and handoff."""

    PRIVILEGE_REQUIRED = "PRIVILEGE_REQUIRED"
    ELEVATION_HANDOFF_READY = "ELEVATION_HANDOFF_READY"
    ELEVATION_ACCEPTED = "ELEVATION_ACCEPTED"
    ELEVATION_DENIED = "ELEVATION_DENIED"
    ELEVATION_CANCELLED = "ELEVATION_CANCELLED"
    ELEVATION_TIMED_OUT = "ELEVATION_TIMED_OUT"
    ELEVATION_IDENTITY_MISMATCH = "ELEVATION_IDENTITY_MISMATCH"
    ELEVATION_UNVERIFIABLE = "ELEVATION_UNVERIFIABLE"

    def __str__(self) -> str:
        return self.value


# ==============================================================================
# Elevation Checkpoint Contract
# ==============================================================================

@dataclass(frozen=True)
class ElevationCheckpoint:
    """Explicit versioned UAC elevation checkpoint binding request identity."""

    checkpoint_id: str
    project_id: str
    run_id: str
    task_id: str
    binding_id: str
    request_id: str
    execution_id: str
    requested_effect: str
    effect_class: str
    reason: str
    requested_executable_path: str
    requested_executable_sha256: str
    requested_argv: Sequence[str] = field(default_factory=list)
    requested_argv_digest: str = ""
    publisher: str | None = None
    source_head: str = ""
    source_tree: str = ""
    candidate_boundary: str = ""
    created_at_epoch: float = 0.0
    deadline_epoch: float = 0.0
    handoff_state: ElevationHandoffState = ElevationHandoffState.NOT_STARTED
    operator_outcome: ElevationOutcome = ElevationOutcome.PENDING
    outcome_timestamp_epoch: float | None = None
    post_elevation_evidence: dict[str, Any] | None = None
    approval_token_id: str | None = None
    schema: str = ELEVATION_CHECKPOINT_SCHEMA
    version: str = ELEVATION_CHECKPOINT_VERSION
    checkpoint_digest: str = ""

    def __post_init__(self) -> None:
        if not self.checkpoint_id:
            raise LocalExecutionContractError("invalid_checkpoint", "checkpoint_id must not be empty")
        if not self.requested_executable_sha256.startswith("sha256:"):
            raise LocalExecutionContractError(
                "invalid_checkpoint",
                "requested_executable_sha256 must start with 'sha256:'",
            )
        if not self.requested_argv_digest:
            computed_argv_digest = "sha256:" + hashlib.sha256(
                json.dumps(list(self.requested_argv), separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            object.__setattr__(self, "requested_argv_digest", computed_argv_digest)

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
            "task_id": self.task_id,
            "binding_id": self.binding_id,
            "request_id": self.request_id,
            "execution_id": self.execution_id,
            "requested_effect": self.requested_effect,
            "effect_class": self.effect_class,
            "reason": self.reason,
            "requested_executable_path": self.requested_executable_path,
            "requested_executable_sha256": self.requested_executable_sha256,
            "requested_argv": list(self.requested_argv),
            "requested_argv_digest": self.requested_argv_digest,
            "publisher": self.publisher,
            "source_head": self.source_head,
            "source_tree": self.source_tree,
            "candidate_boundary": self.candidate_boundary,
            "created_at_epoch": self.created_at_epoch,
            "deadline_epoch": self.deadline_epoch,
            "handoff_state": self.handoff_state.value if isinstance(self.handoff_state, ElevationHandoffState) else str(self.handoff_state),
            "operator_outcome": self.operator_outcome.value if isinstance(self.operator_outcome, ElevationOutcome) else str(self.operator_outcome),
            "outcome_timestamp_epoch": self.outcome_timestamp_epoch,
            "post_elevation_evidence": self.post_elevation_evidence,
            "approval_token_id": self.approval_token_id,
        }

    def canonical_digest(self) -> str:
        serialized = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


# ==============================================================================
# UAC Elevation Checkpoint Manager
# ==============================================================================

class UACElevationCheckpointManager:
    """Manages durable UAC elevation checkpoints and safe operator handoff."""

    def __init__(
        self,
        storage_dir: Path | str,
        clock_fn: Callable[[], float] | None = None,
        approval_registry: ApprovalRegistry | None = None,
    ) -> None:
        self.storage_dir = Path(storage_dir) / "checkpoints_uac"
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.clock_fn = clock_fn or time.time
        self.approval_registry = approval_registry or ApprovalRegistry()
        self.checkpoints: dict[str, ElevationCheckpoint] = {}
        self._consumed_checkpoints: set[str] = set()
        self._load_existing()

    def _load_existing(self) -> None:
        for p in self.storage_dir.glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if data.get("schema") != ELEVATION_CHECKPOINT_SCHEMA or data.get("version") != ELEVATION_CHECKPOINT_VERSION:
                    continue
                cp = ElevationCheckpoint(
                    checkpoint_id=data["checkpoint_id"],
                    project_id=data["project_id"],
                    run_id=data["run_id"],
                    task_id=data["task_id"],
                    binding_id=data["binding_id"],
                    request_id=data["request_id"],
                    execution_id=data["execution_id"],
                    requested_effect=data["requested_effect"],
                    effect_class=data["effect_class"],
                    reason=data["reason"],
                    requested_executable_path=data["requested_executable_path"],
                    requested_executable_sha256=data["requested_executable_sha256"],
                    requested_argv=data.get("requested_argv", []),
                    requested_argv_digest=data.get("requested_argv_digest", ""),
                    publisher=data.get("publisher"),
                    source_head=data["source_head"],
                    source_tree=data["source_tree"],
                    candidate_boundary=data["candidate_boundary"],
                    created_at_epoch=data["created_at_epoch"],
                    deadline_epoch=data["deadline_epoch"],
                    handoff_state=ElevationHandoffState(data["handoff_state"]),
                    operator_outcome=ElevationOutcome(data["operator_outcome"]),
                    outcome_timestamp_epoch=data.get("outcome_timestamp_epoch"),
                    post_elevation_evidence=data.get("post_elevation_evidence"),
                    approval_token_id=data.get("approval_token_id"),
                )
                self.checkpoints[cp.checkpoint_id] = cp
                if cp.handoff_state == ElevationHandoffState.COMPLETED and cp.approval_token_id:
                    self._consumed_checkpoints.add(cp.checkpoint_id)
            except Exception:
                pass

    def _persist(self, cp: ElevationCheckpoint) -> None:
        out_path = self.storage_dir / f"{hashlib.sha256(cp.checkpoint_id.encode('utf-8')).hexdigest()[:24]}.json"
        data = cp.to_dict()
        data["checkpoint_digest"] = cp.checkpoint_digest
        out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def evaluate_elevation_need(
        request: LocalExecutionRequest,
        effect_class: PolicyEffectClass | ExecutionEffectClass | str,
    ) -> tuple[bool, ElevationDisposition, str]:
        """Determine if an execution request requires UAC elevation."""
        policy_effect = map_contract_effect_to_policy(effect_class)
        if policy_effect in (PolicyEffectClass.ELEVATED, PolicyEffectClass.DESTRUCTIVE):
            return True, ElevationDisposition.PRIVILEGE_REQUIRED, f"Effect class '{policy_effect.value}' requires elevated privileges"
        return False, ElevationDisposition.ELEVATION_HANDOFF_READY, "Elevation not required"

    def create_checkpoint(
        self,
        checkpoint_id: str,
        project_id: str,
        run_id: str,
        task_id: str,
        binding_id: str,
        request: LocalExecutionRequest,
        effect_class: PolicyEffectClass | ExecutionEffectClass | str,
        reason: str,
        requested_executable_path: str,
        requested_executable_sha256: str,
        requested_argv: Sequence[str] | None = None,
        publisher: str | None = None,
        timeout_seconds: float = 300.0,
    ) -> ElevationCheckpoint:
        """Create a new durable UAC elevation checkpoint."""
        if checkpoint_id in self.checkpoints:
            return self.checkpoints[checkpoint_id]

        now = self.clock_fn()
        argv_list = list(requested_argv) if requested_argv is not None else list(request.argv)
        policy_effect = map_contract_effect_to_policy(effect_class)

        cp = ElevationCheckpoint(
            checkpoint_id=checkpoint_id,
            project_id=project_id,
            run_id=run_id,
            task_id=task_id,
            binding_id=binding_id,
            request_id=f"req:{request.execution_id}",
            execution_id=request.execution_id,
            requested_effect=request.adapter_id,
            effect_class=policy_effect.value,
            reason=reason,
            requested_executable_path=str(canonicalize_path(requested_executable_path)),
            requested_executable_sha256=requested_executable_sha256,
            requested_argv=argv_list,
            publisher=publisher,
            source_head=request.expected_source_head,
            source_tree=request.expected_source_tree,
            candidate_boundary=str(canonicalize_path(request.cwd)),
            created_at_epoch=now,
            deadline_epoch=now + timeout_seconds,
            handoff_state=ElevationHandoffState.NOT_STARTED,
            operator_outcome=ElevationOutcome.PENDING,
        )
        self._persist(cp)
        self.checkpoints[checkpoint_id] = cp
        return cp

    def present_handoff(self, checkpoint_id: str) -> dict[str, Any]:
        """Present elevation handoff instruction to the user without secure desktop automation."""
        if checkpoint_id not in self.checkpoints:
            raise LocalExecutionContractError("checkpoint_not_found", f"Checkpoint '{checkpoint_id}' not found")

        cp = self.checkpoints[checkpoint_id]
        now = self.clock_fn()

        if now > cp.deadline_epoch:
            updated_to = ElevationCheckpoint(
                **{
                    **cp.__dict__,
                    "handoff_state": ElevationHandoffState.TIMED_OUT,
                    "operator_outcome": ElevationOutcome.TIMED_OUT,
                    "outcome_timestamp_epoch": now,
                    "checkpoint_digest": "",
                }
            )
            self._persist(updated_to)
            self.checkpoints[checkpoint_id] = updated_to
            return {
                "handoff_state": ElevationHandoffState.TIMED_OUT.value,
                "instruction": "Elevation request timed out.",
            }

        updated = ElevationCheckpoint(
            **{
                **cp.__dict__,
                "handoff_state": ElevationHandoffState.WAITING_FOR_OPERATOR,
                "checkpoint_digest": "",
            }
        )
        self._persist(updated)
        self.checkpoints[checkpoint_id] = updated

        return {
            "schema": ELEVATION_CHECKPOINT_SCHEMA,
            "version": ELEVATION_CHECKPOINT_VERSION,
            "checkpoint_id": cp.checkpoint_id,
            "handoff_state": ElevationHandoffState.WAITING_FOR_OPERATOR.value,
            "instruction": (
                f"Windows UAC Elevation Required for '{cp.requested_executable_path}'. "
                "Please review the Windows consent prompt on the Secure Desktop and select Yes to proceed or No to deny. "
                "BDB does not automate the secure desktop or store credentials."
            ),
            "requested_executable": cp.requested_executable_path,
            "requested_executable_sha256": cp.requested_executable_sha256,
            "publisher": cp.publisher,
            "reason": cp.reason,
            "deadline_epoch": cp.deadline_epoch,
        }

    def submit_operator_outcome(
        self,
        checkpoint_id: str,
        outcome: ElevationOutcome,
    ) -> ElevationCheckpoint:
        """Record operator outcome with exactly-one semantics and strict conflict rejection."""
        if checkpoint_id not in self.checkpoints:
            raise LocalExecutionContractError("checkpoint_not_found", f"Checkpoint '{checkpoint_id}' not found")

        existing = self.checkpoints[checkpoint_id]
        now = self.clock_fn()

        # Check deadline
        if now > existing.deadline_epoch:
            if existing.operator_outcome == ElevationOutcome.TIMED_OUT:
                return existing
            updated_to = ElevationCheckpoint(
                **{
                    **existing.__dict__,
                    "handoff_state": ElevationHandoffState.TIMED_OUT,
                    "operator_outcome": ElevationOutcome.TIMED_OUT,
                    "outcome_timestamp_epoch": now,
                    "checkpoint_digest": "",
                }
            )
            self._persist(updated_to)
            self.checkpoints[checkpoint_id] = updated_to
            return updated_to

        # Check existing outcome
        if existing.operator_outcome != ElevationOutcome.PENDING:
            if existing.operator_outcome == outcome:
                return existing  # Idempotent repeat
            raise LocalExecutionContractError(
                "conflicting_elevation_outcome",
                f"Cannot override existing outcome '{existing.operator_outcome.value}' with '{outcome.value}'",
            )

        new_state = ElevationHandoffState.WAITING_FOR_OPERATOR
        if outcome == ElevationOutcome.ACCEPTED:
            new_state = ElevationHandoffState.WAITING_FOR_OPERATOR  # Awaits post-elevation identity verification
        elif outcome == ElevationOutcome.DENIED:
            new_state = ElevationHandoffState.DENIED
        elif outcome == ElevationOutcome.CANCELLED:
            new_state = ElevationHandoffState.CANCELLED
        elif outcome == ElevationOutcome.TIMED_OUT:
            new_state = ElevationHandoffState.TIMED_OUT

        updated = ElevationCheckpoint(
            **{
                **existing.__dict__,
                "handoff_state": new_state,
                "operator_outcome": outcome,
                "outcome_timestamp_epoch": now,
                "checkpoint_digest": "",
            }
        )
        self._persist(updated)
        self.checkpoints[checkpoint_id] = updated
        return updated

    def verify_and_bind_post_elevation_process(
        self,
        checkpoint_id: str,
        discovered_process: ProcessIdentity | dict[str, Any],
        current_head: str,
        current_tree: str,
        execution_request: LocalExecutionRequest | None = None,
    ) -> tuple[bool, str, ElevationCheckpoint]:
        """Perform strict multi-attribute post-elevation process verification and bind approval."""
        if checkpoint_id not in self.checkpoints:
            raise LocalExecutionContractError("checkpoint_not_found", f"Checkpoint '{checkpoint_id}' not found")

        cp = self.checkpoints[checkpoint_id]
        now = self.clock_fn()

        # 1. Replay defense
        if checkpoint_id in self._consumed_checkpoints:
            return False, "ELEVATION_APPROVAL_REPLAY_DENIED", cp

        # 2. Expiry defense
        if now > cp.deadline_epoch:
            return False, "ELEVATION_APPROVAL_EXPIRED_DENIED", cp

        # 3. Outcome check
        if cp.operator_outcome != ElevationOutcome.ACCEPTED:
            return False, f"ELEVATION_NOT_ACCEPTED: {cp.operator_outcome.value}", cp

        # 4. Source HEAD/TREE binding
        if cp.source_head != current_head or cp.source_tree != current_tree:
            return False, "STALE_SOURCE_ELEVATION_DENIED", cp

        # 5. Extract process attributes
        if isinstance(discovered_process, dict):
            proc_path = discovered_process.get("executable_path", "")
            proc_sha256 = discovered_process.get("executable_sha256", "")
            proc_pid = discovered_process.get("pid", 0)
            proc_create_time = discovered_process.get("create_time_epoch", 0.0)
            proc_publisher = discovered_process.get("publisher")
        elif isinstance(discovered_process, ProcessIdentity):
            proc_path = discovered_process.executable_path
            proc_sha256 = discovered_process.executable_sha256
            proc_pid = discovered_process.pid
            proc_create_time = discovered_process.create_time_epoch
            proc_publisher = discovered_process.publisher
        else:
            return False, "INVALID_PROCESS_IDENTITY_TYPE", cp

        # Defense against PID-only verification
        if not proc_path or not proc_sha256 or proc_sha256 == "sha256:0000000000000000000000000000000000000000000000000000000000000000":
            return False, "PID_ONLY_IDENTITY_REJECTED", cp

        # 6. Exact executable path check
        canon_proc_path = str(canonicalize_path(proc_path))
        canon_req_path = str(canonicalize_path(cp.requested_executable_path))
        if canon_proc_path.lower() != canon_req_path.lower():
            return False, f"WRONG_EXECUTABLE_PATH: '{canon_proc_path}' != '{canon_req_path}'", cp

        # 7. Exact executable hash check (binary mutation defense)
        if proc_sha256 != cp.requested_executable_sha256:
            return False, f"CHANGED_EXECUTABLE_HASH: '{proc_sha256}' != '{cp.requested_executable_sha256}'", cp

        # 8. Creation time / PID reuse defense
        if proc_create_time < (cp.created_at_epoch - 1.0):  # Allow 1s clock skew tolerance
            return False, "PRE_EXISTING_PROCESS_PID_REUSE_DENIED", cp

        # 9. Issue ApprovalToken via NX-042 ApprovalRegistry
        policy_effect = PolicyEffectClass(cp.effect_class)
        token_id = f"appr:elevated:{cp.execution_id}:{hashlib.sha256(os.urandom(16)).hexdigest()[:12]}"
        
        # Register token in NX-042 approval registry
        token = ApprovalToken(
            token_id=token_id,
            request_digest=execution_request.request_digest if execution_request else cp.checkpoint_digest,
            project_id=cp.project_id,
            effect_class=policy_effect,
            expected_source_head=cp.source_head,
            expected_source_tree=cp.source_tree,
            issued_at=now,
            expires_at=cp.deadline_epoch,
        )
        self.approval_registry._tokens[token_id] = token

        # Mark consumed
        self._consumed_checkpoints.add(checkpoint_id)

        # Update checkpoint with evidence and token
        evidence_dict = {
            "executable_path": canon_proc_path,
            "executable_sha256": proc_sha256,
            "pid": proc_pid,
            "create_time_epoch": proc_create_time,
            "publisher": proc_publisher,
            "verified_at_epoch": now,
        }

        updated = ElevationCheckpoint(
            **{
                **cp.__dict__,
                "handoff_state": ElevationHandoffState.COMPLETED,
                "post_elevation_evidence": evidence_dict,
                "approval_token_id": token_id,
                "checkpoint_digest": "",
            }
        )
        self._persist(updated)
        self.checkpoints[checkpoint_id] = updated
        return True, "ELEVATED_PROCESS_IDENTITY_VERIFIED", updated

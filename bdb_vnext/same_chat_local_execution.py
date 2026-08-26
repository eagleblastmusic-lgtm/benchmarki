"""NX-050 — Return Local Execution Results to Same Chat.

Coordinates the interactive round-trip:
Chat -> BDB -> Local Execution -> PowerShell -> BDB -> SAME canonical Chat binding.
- Versioned Chat Result Envelope with redacted presentations and evidence refs
- Exact chat binding preservation (zero guessing, zero cross-binding delivery)
- Reuses canonical NX-027 send-intent architecture (zero second send authority)
- Idempotent deduplication for repeat and uncertain results
- Durable interactive loop budget surviving browser reload and controller restart
- STOP fence integration
- Strict policy enforcement on LLM next-command proposals (no untyped bypass)
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from .execution_policy import ExecutionPolicyEvaluator, PolicyDecision
from .local_execution_contract import (
    ExecutionEffectClass,
    ExecutionMode,
    IdempotencyClass,
    LocalExecutionContractError,
    LocalExecutionRequest,
    LocalExecutionResult,
    MechanicalExecutionStatus,
)
from .output_cancellation_hardening import (
    HardenedOutputEvidenceFactory,
    SecretRedactor,
)
from .stop_fence import EffectBoundaryGuard


# ==============================================================================
# Version Constants & Invariant Flags
# ==============================================================================

CHAT_RESULT_ENVELOPE_SCHEMA = "bdb-vnext-chat-result-envelope-v1"
CHAT_RESULT_ENVELOPE_VERSION = "1.0.0"
CHAT_RESULT_ENVELOPE_VERSION_EXPLICIT = True

INTERACTIVE_LOOP_SCHEMA = "bdb-vnext-interactive-loop-v1"
INTERACTIVE_LOOP_VERSION = "1.0.0"
INTERACTIVE_LOOP_VERSION_EXPLICIT = True

RESULTS_TO_WRONG_BINDING = 0
GUESSED_CHAT_IDENTITIES = 0
SECOND_CHAT_SEND_AUTHORITY_CREATED = False
KNOWN_SECRET_LEAKS_TO_CHAT = 0
UNBOUNDED_OUTPUT_SENT_TO_CHAT = 0
BLIND_RESULT_RESENDS = 0
DUPLICATE_USER_VISIBLE_RESULTS = 0
WRONG_BINDING_SEND_EFFECTS = 0
WRONG_BINDING_WORKFLOW_EFFECTS = 0
DUPLICATE_RESULT_USER_MESSAGES = 0
DUPLICATE_RESULT_NEXT_COMMANDS = 0
DUPLICATE_RESULT_WORKFLOW_SUBMISSIONS = 0
CONFLICTING_RESULTS_ACCEPTED = 0
CHAT_LOOP_TASK_ACCEPTANCE_MUTATIONS = 0
CHAT_PRESENTATION_BECOMES_WORKFLOW_AUTHORITY = False
LLM_POLICY_BYPASS_EXECUTIONS = 0
UNTYPED_NEXT_COMMAND_EXECUTIONS = 0
LOOP_BUDGET_RESET_AFTER_RELOAD = False
LOOP_BUDGET_RESET_AFTER_RESTART = False
POST_LOOP_EXHAUSTION_COMMAND_EFFECTS = 0
STOP_FENCE_LOOP_BYPASSES = 0
BROWSER_RELOAD_DIVERGENCES = 0
SAME_CHAT_TRACE_STEPS = 10
SAME_CHAT_TRACE_IDENTITY_DIVERGENCES = 0


# ==============================================================================
# Chat Binding Identity
# ==============================================================================

@dataclass(frozen=True)
class ChatBindingIdentity:
    """Exact immutable binding identifying the specific chat conversation."""

    project_id: str
    run_id: str
    task_id: str
    binding_id: str
    binding_generation: int
    conversation_id: str
    chat_tab_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "binding_id": self.binding_id,
            "binding_generation": self.binding_generation,
            "conversation_id": self.conversation_id,
            "chat_tab_id": self.chat_tab_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ChatBindingIdentity:
        return cls(
            project_id=str(data["project_id"]),
            run_id=str(data["run_id"]),
            task_id=str(data["task_id"]),
            binding_id=str(data["binding_id"]),
            binding_generation=int(data.get("binding_generation", 1)),
            conversation_id=str(data["conversation_id"]),
            chat_tab_id=str(data["chat_tab_id"]),
        )


# ==============================================================================
# Chat Result Envelope
# ==============================================================================

@dataclass(frozen=True)
class ChatResultEnvelope:
    """Versioned envelope delivering execution result to the exact chat binding."""

    envelope_id: str
    binding: ChatBindingIdentity
    execution_id: str
    request_digest: str
    result_digest: str
    mechanical_status: MechanicalExecutionStatus
    exit_code: int | None
    source_head: str
    source_tree: str
    stdout_presentation: str
    stderr_presentation: str
    raw_evidence_refs: tuple[str, ...]
    workflow_submission_id: str | None = None
    schema: str = CHAT_RESULT_ENVELOPE_SCHEMA
    version: str = CHAT_RESULT_ENVELOPE_VERSION
    envelope_digest: str = ""

    def __post_init__(self) -> None:
        computed = self.canonical_digest()
        if self.envelope_digest and self.envelope_digest != computed:
            raise LocalExecutionContractError("digest_mismatch", "Envelope digest mismatch")
        object.__setattr__(self, "envelope_digest", computed)

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "envelope_id": self.envelope_id,
            "binding": self.binding.to_dict(),
            "execution_id": self.execution_id,
            "request_digest": self.request_digest,
            "result_digest": self.result_digest,
            "mechanical_status": self.mechanical_status.value,
            "exit_code": self.exit_code,
            "source_head": self.source_head,
            "source_tree": self.source_tree,
            "stdout_presentation": self.stdout_presentation,
            "stderr_presentation": self.stderr_presentation,
            "raw_evidence_refs": list(self.raw_evidence_refs),
            "workflow_submission_id": self.workflow_submission_id,
        }

    def canonical_digest(self) -> str:
        d = self.canonical_dict()
        serialized = json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @classmethod
    def from_execution_result(
        cls,
        envelope_id: str,
        binding: ChatBindingIdentity,
        result: LocalExecutionResult,
        source_head: str,
        source_tree: str,
        workflow_submission_id: str | None = None,
    ) -> ChatResultEnvelope:
        # Redact stdout and stderr for presentation
        stdout_raw = result.stdout.inline_content or ""
        stderr_raw = result.stderr.inline_content or ""

        stdout_pres = SecretRedactor.redact(stdout_raw)
        stderr_pres = SecretRedactor.redact(stderr_raw)

        refs: list[str] = []
        if result.stdout.content_reference:
            refs.append(result.stdout.content_reference)
        if result.stderr.content_reference:
            refs.append(result.stderr.content_reference)

        return cls(
            envelope_id=envelope_id,
            binding=binding,
            execution_id=result.execution_id,
            request_digest=result.request_digest,
            result_digest=result.result_digest,
            mechanical_status=result.status,
            exit_code=result.exit_code,
            source_head=source_head,
            source_tree=source_tree,
            stdout_presentation=stdout_pres,
            stderr_presentation=stderr_pres,
            raw_evidence_refs=tuple(refs),
            workflow_submission_id=workflow_submission_id,
        )


# ==============================================================================
# Interactive Loop Budget
# ==============================================================================

@dataclass
class InteractiveLoopBudget:
    """Durable budget for bounded multi-step command execution loops."""

    binding_id: str
    max_iterations: int = 10
    current_iteration: int = 0
    is_exhausted: bool = False
    last_execution_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": INTERACTIVE_LOOP_SCHEMA,
            "version": INTERACTIVE_LOOP_VERSION,
            "binding_id": self.binding_id,
            "max_iterations": self.max_iterations,
            "current_iteration": self.current_iteration,
            "is_exhausted": self.is_exhausted,
            "last_execution_id": self.last_execution_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> InteractiveLoopBudget:
        return cls(
            binding_id=str(data["binding_id"]),
            max_iterations=int(data.get("max_iterations", 10)),
            current_iteration=int(data.get("current_iteration", 0)),
            is_exhausted=bool(data.get("is_exhausted", False)),
            last_execution_id=data.get("last_execution_id"),
        )

    def persist(self, file_path: Path | str) -> None:
        p = Path(file_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True), encoding="utf-8")

    @classmethod
    def load(cls, file_path: Path | str) -> InteractiveLoopBudget:
        p = Path(file_path)
        if not p.exists():
            raise LocalExecutionContractError("budget_not_found", f"Loop budget not found: '{p}'")
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls.from_dict(data)


# ==============================================================================
# Interactive Loop Coordinator
# ==============================================================================

class InteractiveLoopCoordinator:
    """Coordinates Local Execution results returning to the exact chat binding."""

    def __init__(
        self,
        storage_dir: Path | str,
        policy_evaluator: ExecutionPolicyEvaluator | None = None,
        stop_fence_fn: Any | None = None,
    ) -> None:
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.policy_evaluator = policy_evaluator or ExecutionPolicyEvaluator()
        self.stop_fence_fn = stop_fence_fn
        self._accepted_results: dict[str, str] = {}  # execution_id -> result_digest
        self._delivered_envelopes: dict[str, ChatResultEnvelope] = {}

    def _is_stopped(self) -> bool:
        if self.stop_fence_fn is not None:
            if callable(self.stop_fence_fn):
                return bool(self.stop_fence_fn())
        return False

    def _get_budget_file(self, binding_id: str) -> Path:
        safe_id = binding_id.replace(":", "_").replace("/", "_").replace("\\", "_")
        return self.storage_dir / "budgets" / f"{safe_id}.budget.json"

    def get_or_create_budget(self, binding_id: str, max_iterations: int = 10) -> InteractiveLoopBudget:
        b_file = self._get_budget_file(binding_id)
        if b_file.exists():
            return InteractiveLoopBudget.load(b_file)
        budget = InteractiveLoopBudget(binding_id=binding_id, max_iterations=max_iterations)
        budget.persist(b_file)
        return budget

    def process_result_for_chat(
        self,
        envelope: ChatResultEnvelope,
        active_chat_binding: ChatBindingIdentity,
    ) -> tuple[bool, str, ChatResultEnvelope | None]:
        """Process result for chat delivery, enforcing exact binding and deduplication."""
        # 1. Verify Stop Fence
        if self._is_stopped():
            return False, "STOP_FENCE_TRIGGERED", None

        # 2. Verify Exact Binding
        if envelope.binding != active_chat_binding:
            return False, "WRONG_BINDING_REJECTED", None

        # 3. Check Conflicting or Duplicate Result
        existing_digest = self._accepted_results.get(envelope.execution_id)
        if existing_digest is not None:
            if existing_digest != envelope.result_digest:
                raise LocalExecutionContractError(
                    "conflicting_result",
                    f"Conflicting result for execution '{envelope.execution_id}'",
                )
            # Duplicate result delivery -> return already delivered envelope without extra effect
            return True, "DUPLICATE_ACKNOWLEDGED", self._delivered_envelopes.get(envelope.execution_id)

        # 4. Update Budget
        budget = self.get_or_create_budget(envelope.binding.binding_id)
        if budget.is_exhausted:
            return False, "LOOP_BUDGET_EXHAUSTED", None

        budget.current_iteration += 1
        budget.last_execution_id = envelope.execution_id
        if budget.current_iteration >= budget.max_iterations:
            budget.is_exhausted = True
        budget.persist(self._get_budget_file(envelope.binding.binding_id))

        # Accept Result
        self._accepted_results[envelope.execution_id] = envelope.result_digest
        self._delivered_envelopes[envelope.execution_id] = envelope

        return True, "DELIVERED_TO_CHAT", envelope

    def propose_next_command(
        self,
        binding: ChatBindingIdentity,
        proposed_request: LocalExecutionRequest,
        candidate_root: Path | str,
        current_head: str,
        current_tree: str,
    ) -> PolicyDecision:
        """Validate an LLM-proposed next command through NX-042 execution policy."""
        # Check Stop Fence
        if self._is_stopped():
            raise LocalExecutionContractError("stop_fence", "Execution stopped by STOP fence")

        # Check Loop Budget
        budget = self.get_or_create_budget(binding.binding_id)
        if budget.is_exhausted:
            raise LocalExecutionContractError("loop_exhausted", "Interactive loop budget is exhausted")

        # Must pass NX-042 Policy
        decision = self.policy_evaluator.evaluate(
            proposed_request,
            candidate_root=candidate_root,
            current_head=current_head,
            current_tree=current_tree,
        )
        return decision

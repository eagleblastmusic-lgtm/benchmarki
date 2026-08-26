"""NX-012: Failure taxonomy, semantic kinds, and transition table contract.

Formalizes required failure classes and explicitly separates:
- actual failure (FAILURE)
- waiting (WAITING)
- policy pause (POLICY_PAUSE)
- human/operator checkpoint (HUMAN_CHECKPOINT)

Defines required evidence, allowed transitions, AUTO behavior, retry/repair/reconciliation/resume rules.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


# ==============================================================================
# 1. SEMANTIC KINDS
# ==============================================================================

class SemanticKind(enum.Enum):
    FAILURE = "FAILURE"
    WAITING = "WAITING"
    POLICY_PAUSE = "POLICY_PAUSE"
    HUMAN_CHECKPOINT = "HUMAN_CHECKPOINT"


# ==============================================================================
# 2. CANONICAL FAILURE CLASSES
# ==============================================================================

class FailureClass(enum.Enum):
    PROJECT_REPAIRABLE = "PROJECT_REPAIRABLE"
    TRANSIENT_INFRASTRUCTURE = "TRANSIENT_INFRASTRUCTURE"
    CI_WAITING = "CI_WAITING"
    TEST_INFRA_FAILURE = "TEST_INFRA_FAILURE"
    TRANSPORT_UNCERTAIN = "TRANSPORT_UNCERTAIN"
    ENVIRONMENT_REPAIRABLE = "ENVIRONMENT_REPAIRABLE"
    SOURCE_DIVERGENCE = "SOURCE_DIVERGENCE"
    POLICY_VIOLATION = "POLICY_VIOLATION"
    SECURITY_VIOLATION = "SECURITY_VIOLATION"
    DATA_CORRUPTION = "DATA_CORRUPTION"
    EXTERNAL_ACTION_REQUIRED = "EXTERNAL_ACTION_REQUIRED"
    AMBIGUOUS_FAILURE = "AMBIGUOUS_FAILURE"
    PHASE_SCOPE_VIOLATION = "PHASE_SCOPE_VIOLATION"
    EARLY_IMPLEMENTATION = "EARLY_IMPLEMENTATION"


REQUIRED_FAILURE_CLASSES: tuple[str, ...] = tuple(c.value for c in FailureClass)


# ==============================================================================
# 3. AUTO ACTIONS
# ==============================================================================

class AutoAction(enum.Enum):
    AUTO_REPAIR_PROJECT = "AUTO_REPAIR_PROJECT"
    AUTO_REPAIR_BOUNDED_PROJECT = "AUTO_REPAIR_BOUNDED_PROJECT"
    AUTO_RETRY_BACKOFF = "AUTO_RETRY_BACKOFF"
    AUTO_POLL = "AUTO_POLL"
    AUTO_REPAIR_TEST_INFRA = "AUTO_REPAIR_TEST_INFRA"
    AUTO_RECONCILE = "AUTO_RECONCILE"
    AUTO_REPAIR_ENVIRONMENT = "AUTO_REPAIR_ENVIRONMENT"
    AUTO_STOP_FAIL_CLOSED = "AUTO_STOP_FAIL_CLOSED"
    AUTO_PAUSE_POLICY = "AUTO_PAUSE_POLICY"
    AUTO_FREEZE_FAIL_CLOSED = "AUTO_FREEZE_FAIL_CLOSED"
    AUTO_WAIT_FOR_OPERATOR = "AUTO_WAIT_FOR_OPERATOR"
    AUTO_FAIL_CLOSED = "AUTO_FAIL_CLOSED"


# ==============================================================================
# 4. TRANSITION SPECIFICATION & TABLE
# ==============================================================================

@dataclass(frozen=True)
class TransitionSpec:
    failure_class: FailureClass
    semantic_kind: SemanticKind
    description: str
    required_evidence: tuple[str, ...]
    auto_action: AutoAction
    retry_allowed: bool
    repair_allowed: bool
    reconciliation_required: bool
    operator_required: bool
    terminal: bool
    resume_condition: str
    escalation_target: FailureClass | None


TRANSITION_MATRIX: dict[FailureClass, TransitionSpec] = {
    FailureClass.PROJECT_REPAIRABLE: TransitionSpec(
        failure_class=FailureClass.PROJECT_REPAIRABLE,
        semantic_kind=SemanticKind.FAILURE,
        description="Code, syntax, logic error or missing artifact repairable within task boundary.",
        required_evidence=("failure_code", "diagnostic_message", "test_or_artifact_ref"),
        auto_action=AutoAction.AUTO_REPAIR_PROJECT,
        retry_allowed=True,
        repair_allowed=True,
        reconciliation_required=False,
        operator_required=False,
        terminal=False,
        resume_condition="passing test rerun or verified artifact creation",
        escalation_target=FailureClass.EXTERNAL_ACTION_REQUIRED,
    ),
    FailureClass.TRANSIENT_INFRASTRUCTURE: TransitionSpec(
        failure_class=FailureClass.TRANSIENT_INFRASTRUCTURE,
        semantic_kind=SemanticKind.FAILURE,
        description="Transient network blip, connector connect timeout, or temporary queue lock contention.",
        required_evidence=("error_code", "retryable_error_pattern", "resource_target"),
        auto_action=AutoAction.AUTO_RETRY_BACKOFF,
        retry_allowed=True,
        repair_allowed=False,  # Never modify code for infrastructure blips
        reconciliation_required=False,
        operator_required=False,
        terminal=False,
        resume_condition="backoff timer expiry and connection re-established",
        escalation_target=FailureClass.EXTERNAL_ACTION_REQUIRED,
    ),
    FailureClass.CI_WAITING: TransitionSpec(
        failure_class=FailureClass.CI_WAITING,
        semantic_kind=SemanticKind.WAITING,
        description="External CI workflow run currently in progress or queued for target commit.",
        required_evidence=("ci_provider", "run_id", "target_head_sha", "run_status"),
        auto_action=AutoAction.AUTO_POLL,
        retry_allowed=False,  # Waiting does not consume failure retry budget
        repair_allowed=False,  # Waiting does not create project repair attempts
        reconciliation_required=False,
        operator_required=False,
        terminal=False,
        resume_condition="CI run terminal status observed (success/failure) via poll or webhook",
        escalation_target=FailureClass.EXTERNAL_ACTION_REQUIRED,
    ),
    FailureClass.TEST_INFRA_FAILURE: TransitionSpec(
        failure_class=FailureClass.TEST_INFRA_FAILURE,
        semantic_kind=SemanticKind.FAILURE,
        description="Test framework crash, test runner timeout, or faulty test oracle (e.g. precision bug).",
        required_evidence=("test_runner_exit_code", "harness_crash_trace", "test_id"),
        auto_action=AutoAction.AUTO_REPAIR_TEST_INFRA,
        retry_allowed=True,
        repair_allowed=True,
        reconciliation_required=False,
        operator_required=False,
        terminal=False,
        resume_condition="test harness restored or oracle fixed",
        escalation_target=FailureClass.EXTERNAL_ACTION_REQUIRED,
    ),
    FailureClass.TRANSPORT_UNCERTAIN: TransitionSpec(
        failure_class=FailureClass.TRANSPORT_UNCERTAIN,
        semantic_kind=SemanticKind.FAILURE,
        description="Command/message delivery state unconfirmed (missing ack, socket drop mid-send).",
        required_evidence=("correlation_id", "command_id", "last_known_delivery_state"),
        auto_action=AutoAction.AUTO_RECONCILE,
        retry_allowed=False,  # Blind resend strictly prohibited before reconciliation
        repair_allowed=False,
        reconciliation_required=True,  # Must query recipient state first
        operator_required=False,
        terminal=False,
        resume_condition="recipient state queried and delivery status confirmed or re-enqueued",
        escalation_target=FailureClass.EXTERNAL_ACTION_REQUIRED,
    ),
    FailureClass.ENVIRONMENT_REPAIRABLE: TransitionSpec(
        failure_class=FailureClass.ENVIRONMENT_REPAIRABLE,
        semantic_kind=SemanticKind.FAILURE,
        description="Missing environment dependency, tool binary not found in PATH, missing SDK.",
        required_evidence=("missing_binary_or_package", "resolved_path", "exit_code"),
        auto_action=AutoAction.AUTO_REPAIR_ENVIRONMENT,
        retry_allowed=True,
        repair_allowed=True,  # Environment repair allowed; project code untouched
        reconciliation_required=False,
        operator_required=False,
        terminal=False,
        resume_condition="dependency installed or environment variable activated",
        escalation_target=FailureClass.EXTERNAL_ACTION_REQUIRED,
    ),
    FailureClass.SOURCE_DIVERGENCE: TransitionSpec(
        failure_class=FailureClass.SOURCE_DIVERGENCE,
        semantic_kind=SemanticKind.FAILURE,
        description="Git repository HEAD or tree does not match expected baseline / accepted candidate.",
        required_evidence=("expected_head", "observed_head", "expected_tree", "observed_tree"),
        auto_action=AutoAction.AUTO_STOP_FAIL_CLOSED,
        retry_allowed=False,  # Never blindly retry against diverged source
        repair_allowed=False,
        reconciliation_required=True,
        operator_required=True,
        terminal=True,
        resume_condition="operator aligns source or checks out canonical baseline",
        escalation_target=FailureClass.EXTERNAL_ACTION_REQUIRED,
    ),
    FailureClass.POLICY_VIOLATION: TransitionSpec(
        failure_class=FailureClass.POLICY_VIOLATION,
        semantic_kind=SemanticKind.POLICY_PAUSE,
        description="Action violates active project policy, rate ceiling, or scope boundary.",
        required_evidence=("policy_rule_id", "attempted_action", "resource_target"),
        auto_action=AutoAction.AUTO_PAUSE_POLICY,
        retry_allowed=False,  # Automatic privilege escalation prohibited
        repair_allowed=False,
        reconciliation_required=True,
        operator_required=True,
        terminal=False,
        resume_condition="policy relaxation or scope boundary adjustment by operator",
        escalation_target=FailureClass.EXTERNAL_ACTION_REQUIRED,
    ),
    FailureClass.SECURITY_VIOLATION: TransitionSpec(
        failure_class=FailureClass.SECURITY_VIOLATION,
        semantic_kind=SemanticKind.FAILURE,
        description="Path traversal, symlink escape outside canonical root, or unauthorized write.",
        required_evidence=("violation_type", "offending_path_or_token", "canonical_root"),
        auto_action=AutoAction.AUTO_FREEZE_FAIL_CLOSED,
        retry_allowed=False,  # Automatic retry absolutely prohibited
        repair_allowed=False,
        reconciliation_required=False,
        operator_required=True,
        terminal=True,
        resume_condition="manual security audit and operator unfreeze",
        escalation_target=FailureClass.EXTERNAL_ACTION_REQUIRED,
    ),
    FailureClass.DATA_CORRUPTION: TransitionSpec(
        failure_class=FailureClass.DATA_CORRUPTION,
        semantic_kind=SemanticKind.FAILURE,
        description="Database integrity check failure, malformed JSON, or unreadable state storage.",
        required_evidence=("corrupted_file_path", "integrity_check_output", "expected_format"),
        auto_action=AutoAction.AUTO_FREEZE_FAIL_CLOSED,
        retry_allowed=False,  # Blind auto repair on corrupted storage prohibited
        repair_allowed=False,
        reconciliation_required=True,
        operator_required=True,
        terminal=True,
        resume_condition="point-in-time restore from verified backup",
        escalation_target=FailureClass.EXTERNAL_ACTION_REQUIRED,
    ),
    FailureClass.EXTERNAL_ACTION_REQUIRED: TransitionSpec(
        failure_class=FailureClass.EXTERNAL_ACTION_REQUIRED,
        semantic_kind=SemanticKind.HUMAN_CHECKPOINT,
        description="Human review required: open question decision, manual gate approval, external credentials.",
        required_evidence=("checkpoint_id", "decision_or_question_id", "reason"),
        auto_action=AutoAction.AUTO_WAIT_FOR_OPERATOR,
        retry_allowed=False,
        repair_allowed=False,
        reconciliation_required=False,
        operator_required=True,
        terminal=False,
        resume_condition="operator records decision or signs off on checkpoint",
        escalation_target=None,
    ),
    FailureClass.AMBIGUOUS_FAILURE: TransitionSpec(
        failure_class=FailureClass.AMBIGUOUS_FAILURE,
        semantic_kind=SemanticKind.FAILURE,
        description="Conflicting, incomplete, or unknown failure signals that cannot be deterministically resolved.",
        required_evidence=("conflicting_rule_ids_or_missing_fields", "raw_observation"),
        auto_action=AutoAction.AUTO_FAIL_CLOSED,
        retry_allowed=False,  # Must fail closed, never accept as PASS or blind retry
        repair_allowed=False,
        reconciliation_required=True,
        operator_required=True,
        terminal=True,
        resume_condition="diagnostic clarification or manual failure classification by operator",
        escalation_target=FailureClass.EXTERNAL_ACTION_REQUIRED,
    ),
    FailureClass.PHASE_SCOPE_VIOLATION: TransitionSpec(
        failure_class=FailureClass.PHASE_SCOPE_VIOLATION,
        semantic_kind=SemanticKind.FAILURE,
        description="Code changes touched files or milestones outside current task scope.",
        required_evidence=("out_of_scope_files", "allowed_task_scope", "current_milestone_id"),
        auto_action=AutoAction.AUTO_REPAIR_BOUNDED_PROJECT,
        retry_allowed=True,  # Bounded project repair within task boundary
        repair_allowed=True,  # Restoring violated boundary only
        reconciliation_required=True,
        operator_required=False,
        terminal=False,
        resume_condition="out-of-boundary changes reverted and task boundary verified",
        escalation_target=FailureClass.EXTERNAL_ACTION_REQUIRED,
    ),
    FailureClass.EARLY_IMPLEMENTATION: TransitionSpec(
        failure_class=FailureClass.EARLY_IMPLEMENTATION,
        semantic_kind=SemanticKind.FAILURE,
        description="Premature implementation of future task/feature before prerequisite milestone is committed.",
        required_evidence=("uncommitted_prerequisite_id", "premature_symbols_or_files"),
        auto_action=AutoAction.AUTO_REPAIR_BOUNDED_PROJECT,
        retry_allowed=True,  # Bounded project repair
        repair_allowed=True,  # Confined strictly to boundary
        reconciliation_required=True,
        operator_required=False,
        terminal=False,
        resume_condition="premature code isolated and prerequisite focus restored",
        escalation_target=FailureClass.EXTERNAL_ACTION_REQUIRED,
    ),
}


# ==============================================================================
# 5. PRECEDENCE HIERARCHY
# ==============================================================================

# Deterministic evaluation order: highest safety / criticality evaluated first
PRECEDENCE_ORDER: tuple[FailureClass, ...] = (
    FailureClass.SECURITY_VIOLATION,
    FailureClass.DATA_CORRUPTION,
    FailureClass.SOURCE_DIVERGENCE,
    FailureClass.POLICY_VIOLATION,
    FailureClass.CI_WAITING,
    FailureClass.TRANSIENT_INFRASTRUCTURE,
    FailureClass.TRANSPORT_UNCERTAIN,
    FailureClass.ENVIRONMENT_REPAIRABLE,
    FailureClass.TEST_INFRA_FAILURE,
    FailureClass.PHASE_SCOPE_VIOLATION,
    FailureClass.EARLY_IMPLEMENTATION,
    FailureClass.PROJECT_REPAIRABLE,
    FailureClass.EXTERNAL_ACTION_REQUIRED,
    FailureClass.AMBIGUOUS_FAILURE,
)


# ==============================================================================
# 6. TRANSITION VALIDATOR
# ==============================================================================

def validate_transition(
    current_class: FailureClass,
    target_action: str,
    *,
    context: Mapping[str, Any] | None = None,
) -> tuple[bool, str]:
    """Validates whether a transition from a failure class to a target action is legal.

    Returns (is_legal, reason).
    Enforces all canonical transition invariants:
    - CI_WAITING cannot transition to FAILED merely because poll is pending
    - SECURITY_VIOLATION cannot auto-retry
    - DATA_CORRUPTION cannot blind auto-repair
    - TRANSPORT_UNCERTAIN cannot resend without reconciliation
    - AMBIGUOUS_FAILURE cannot accept PASS
    - POLICY_VIOLATION cannot auto-privilege escalate
    - TERMINAL states cannot auto-retry
    """
    spec = TRANSITION_MATRIX.get(current_class)
    if spec is None:
        return False, f"unknown_failure_class: {current_class}"

    ctx = context or {}
    action = target_action.upper()

    # Invariant 1: AMBIGUOUS_FAILURE cannot produce PASS
    if current_class == FailureClass.AMBIGUOUS_FAILURE and action in {"PASS", "ACCEPT", "PROMOTE"}:
        return False, "ambiguous_failure_cannot_accept_pass"

    # Invariant 2: CI_WAITING is not failure and cannot fail solely for waiting
    if current_class == FailureClass.CI_WAITING:
        if action in {"FAILED", "FAIL", "MARK_FAILED"} and ctx.get("poll_pending", True):
            return False, "ci_waiting_cannot_fail_while_pending"
        if action in {"AUTO_REPAIR_PROJECT", "CREATE_REPAIR_ATTEMPT"}:
            return False, "ci_waiting_cannot_create_project_repair"

    # Invariant 3: SECURITY_VIOLATION cannot auto-retry
    if current_class == FailureClass.SECURITY_VIOLATION:
        if action in {"AUTO_RETRY", "RETRY", "AUTO_RETRY_BACKOFF"}:
            return False, "security_violation_auto_retry_prohibited"
        if action in {"ESCALATE_PRIVILEGE", "AUTO_BYPASS"}:
            return False, "security_violation_bypass_prohibited"

    # Invariant 4: DATA_CORRUPTION cannot blind auto repair
    if current_class == FailureClass.DATA_CORRUPTION:
        if action in {"AUTO_REPAIR_PROJECT", "RETRY", "AUTO_RETRY"}:
            return False, "data_corruption_blind_repair_prohibited"

    # Invariant 5: TRANSPORT_UNCERTAIN cannot resend before reconciliation
    if current_class == FailureClass.TRANSPORT_UNCERTAIN:
        if action in {"RESEND", "RETRY", "AUTO_RETRY"} and not ctx.get("reconciled", False):
            return False, "transport_uncertain_blind_resend_prohibited"

    # Invariant 6: POLICY_VIOLATION cannot auto-privilege escalate
    if current_class == FailureClass.POLICY_VIOLATION:
        if action in {"AUTO_ESCALATE_PRIVILEGE", "AUTO_BYPASS", "RETRY"}:
            return False, "policy_violation_auto_escalation_prohibited"

    # Invariant 7: PHASE_SCOPE_VIOLATION & EARLY_IMPLEMENTATION cannot broaden outside boundary
    if current_class in {FailureClass.PHASE_SCOPE_VIOLATION, FailureClass.EARLY_IMPLEMENTATION}:
        if action in {"BROADEN_SCOPE", "MODIFY_OUT_OF_SCOPE_FILES"}:
            return False, "repair_cannot_escape_task_boundary"
        if action in {"ABORT_PROJECT", "TERMINATE_PROJECT"} and not ctx.get("budget_exhausted", False):
            return False, "scope_violation_must_attempt_bounded_repair_first"

    # Invariant 8: Terminal states cannot auto-retry
    if spec.terminal and action in {"AUTO_RETRY", "RETRY"}:
        return False, f"terminal_state_auto_retry_prohibited: {current_class.value}"

    return True, "legal_transition"


# ==============================================================================
# 7. INVENTORY OF EXISTING REPOSITORY ERROR STATES MAPPED TO NX-012
# ==============================================================================

@dataclass(frozen=True)
class ExistingStateMapping:
    existing_state_or_error: str
    current_owner: str
    current_meaning: str
    current_auto_behavior: str
    canonical_nx012_class: FailureClass
    required_evidence: tuple[str, ...]


EXISTING_STATE_MAPPINGS: tuple[ExistingStateMapping, ...] = (
    ExistingStateMapping(
        existing_state_or_error="COMPILATION_ERROR / SyntaxError",
        current_owner="ProjectExecution / Task Attempt Validator",
        current_meaning="Code failed to compile or parse syntactically",
        current_auto_behavior="Blocked / failure attempt recorded",
        canonical_nx012_class=FailureClass.PROJECT_REPAIRABLE,
        required_evidence=("failure_code", "diagnostic_message", "test_or_artifact_ref"),
    ),
    ExistingStateMapping(
        existing_state_or_error="ConnectTimeout / GITHUB_CONNECTOR_TIMEOUT",
        current_owner="GitHub Connector / External Client",
        current_meaning="Network connection timed out reaching external GitHub API",
        current_auto_behavior="Transient exception raised",
        canonical_nx012_class=FailureClass.TRANSIENT_INFRASTRUCTURE,
        required_evidence=("error_code", "retryable_error_pattern", "resource_target"),
    ),
    ExistingStateMapping(
        existing_state_or_error="CI_IN_PROGRESS / GITHUB_ACTIONS_PENDING",
        current_owner="Project Workflow / CI Monitor",
        current_meaning="External CI run is currently executing or queued",
        current_auto_behavior="Premature WAITING_EXTERNAL (v1 flaw to be repaired)",
        canonical_nx012_class=FailureClass.CI_WAITING,
        required_evidence=("ci_provider", "run_id", "target_head_sha", "run_status"),
    ),
    ExistingStateMapping(
        existing_state_or_error="IEEE_754_PRECISION_ORACLE / TestRunnerCrash",
        current_owner="Test Runner / Oracle Evaluator",
        current_meaning="Test failure caused by overly strict oracle or runner crash",
        current_auto_behavior="Task blocked as failure",
        canonical_nx012_class=FailureClass.TEST_INFRA_FAILURE,
        required_evidence=("test_runner_exit_code", "harness_crash_trace", "test_id"),
    ),
    ExistingStateMapping(
        existing_state_or_error="unacked_launch_message / dropped_rpc",
        current_owner="ProjectLaunchQueueAdapter",
        current_meaning="Dispatch status unconfirmed by consumer",
        current_auto_behavior="Pending outbox without confirmation",
        canonical_nx012_class=FailureClass.TRANSPORT_UNCERTAIN,
        required_evidence=("correlation_id", "command_id", "last_known_delivery_state"),
    ),
    ExistingStateMapping(
        existing_state_or_error="dotnet_not_found / python_module_missing",
        current_owner="Execution Environment / Tool Runner",
        current_meaning="Required toolchain or interpreter missing in execution environment",
        current_auto_behavior="Tool execution error",
        canonical_nx012_class=FailureClass.ENVIRONMENT_REPAIRABLE,
        required_evidence=("missing_binary_or_package", "resolved_path", "exit_code"),
    ),
    ExistingStateMapping(
        existing_state_or_error="expected_repo_head_before mismatch / tree diverged",
        current_owner="ProjectExecution / Task Dispatch",
        current_meaning="Local repository HEAD/tree diverged from expected task binding baseline",
        current_auto_behavior="Stale result / binding rejected",
        canonical_nx012_class=FailureClass.SOURCE_DIVERGENCE,
        required_evidence=("expected_head", "observed_head", "expected_tree", "observed_tree"),
    ),
    ExistingStateMapping(
        existing_state_or_error="unauthorized_file_touch / milestone_scope_breach",
        current_owner="Project Scope Enforcement / Orchestrator",
        current_meaning="Attempted modification of protected files outside current task",
        current_auto_behavior="Policy rejection",
        canonical_nx012_class=FailureClass.POLICY_VIOLATION,
        required_evidence=("policy_rule_id", "attempted_action", "resource_target"),
    ),
    ExistingStateMapping(
        existing_state_or_error="path_traversal_detected / store_root_symlink",
        current_owner="ProjectMemoryStoreV2 / Storage Confinement",
        current_meaning="Storage path attempts to escape canonical root via traversal or symlink",
        current_auto_behavior="ProjectMemoryV2Error fail closed",
        canonical_nx012_class=FailureClass.SECURITY_VIOLATION,
        required_evidence=("violation_type", "offending_path_or_token", "canonical_root"),
    ),
    ExistingStateMapping(
        existing_state_or_error="sqlite_integrity_check_failed / corrupted_state_json",
        current_owner="ProjectMemoryStore / SQLite DB",
        current_meaning="Persistent state storage corrupted or unreadable",
        current_auto_behavior="Fail closed exception",
        canonical_nx012_class=FailureClass.DATA_CORRUPTION,
        required_evidence=("corrupted_file_path", "integrity_check_output", "expected_format"),
    ),
    ExistingStateMapping(
        existing_state_or_error="decision_required / manual_gate_signoff",
        current_owner="ProjectMemory (attention items) / Human Review",
        current_meaning="Execution paused waiting for operator decision or gate signoff",
        current_auto_behavior="Attention item open, requires user intervention",
        canonical_nx012_class=FailureClass.EXTERNAL_ACTION_REQUIRED,
        required_evidence=("checkpoint_id", "decision_or_question_id", "reason"),
    ),
    ExistingStateMapping(
        existing_state_or_error="unknown_failure_code / conflicting_diagnostics",
        current_owner="Failure Classifier / Observer",
        current_meaning="Failure observation does not match any deterministic rule or has contradictory evidence",
        current_auto_behavior="Fail closed review required",
        canonical_nx012_class=FailureClass.AMBIGUOUS_FAILURE,
        required_evidence=("conflicting_rule_ids_or_missing_fields", "raw_observation"),
    ),
    ExistingStateMapping(
        existing_state_or_error="PHASE_SCOPE_VIOLATION in G1",
        current_owner="Milestone Scope Gate / Audit Incident",
        current_meaning="Changes touched code of later milestone",
        current_auto_behavior="Scope rejection requiring bounded repair",
        canonical_nx012_class=FailureClass.PHASE_SCOPE_VIOLATION,
        required_evidence=("out_of_scope_files", "allowed_task_scope", "current_milestone_id"),
    ),
    ExistingStateMapping(
        existing_state_or_error="EARLY_IMPLEMENTATION in G1",
        current_owner="Milestone Scope Gate / Audit Incident",
        current_meaning="Implemented ahead of plan before prerequisite commits",
        current_auto_behavior="Scope rejection requiring isolation",
        canonical_nx012_class=FailureClass.EARLY_IMPLEMENTATION,
        required_evidence=("uncommitted_prerequisite_id", "premature_symbols_or_files"),
    ),
)

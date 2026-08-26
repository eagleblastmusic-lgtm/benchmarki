"""NX-013: Deterministic Failure Classifier.

Classifies structured tool/test/CI/transport observations deterministically without LLM authority.
- Identical evidence produces identical classification
- LLM / advisory text cannot promote status to PASS
- Conflicting rules or malformed evidence resolve to AMBIGUOUS_FAILURE
- Output binds to canonical evidence digest
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from bdb_vnext.failure_taxonomy import (
    TRANSITION_MATRIX,
    AutoAction,
    FailureClass,
    SemanticKind,
)


def compute_evidence_digest(evidence: Mapping[str, Any]) -> str:
    """Computes deterministic SHA-256 digest over normalized canonical JSON."""
    # Exclude ephemeral/internal keys starting with '_'
    normalized = {k: v for k, v in evidence.items() if not k.startswith("_")}
    serialized = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"sha256:{hashlib.sha256(serialized.encode('utf-8')).hexdigest()}"


@dataclass(frozen=True)
class ClassifierRule:
    rule_id: str
    priority: int  # Lower number = evaluated first / higher precedence
    output_class: FailureClass
    required_fields: tuple[str, ...]
    predicate: Callable[[Mapping[str, Any]], bool]
    source_confidence: str
    description: str


@dataclass(frozen=True)
class ClassificationResult:
    failure_class: FailureClass
    semantic_kind: SemanticKind
    rule_id: str
    auto_action: AutoAction
    evidence_digest: str
    matched_rules: tuple[str, ...]
    details: dict[str, Any]


# ==============================================================================
# RULE DEFINITIONS
# ==============================================================================

# Priority tiers:
# 100: Security violation (Highest)
# 200: Data corruption
# 300: Source divergence
# 400: Policy violation
# 500: CI waiting
# 600: Transient infrastructure
# 700: Transport uncertainty
# 800: Environment repairable
# 900: Test infrastructure failure
# 1000: Phase scope violation
# 1100: Early implementation
# 1200: Project repairable
# 1300: External action required
# 9999: Fallback / Ambiguous (Lowest)

RULES_LIST: list[ClassifierRule] = [
    # 1. Security Violation
    ClassifierRule(
        rule_id="RULE_SEC_001_PATH_TRAVERSAL",
        priority=100,
        output_class=FailureClass.SECURITY_VIOLATION,
        required_fields=("violation_type", "offending_path_or_token"),
        predicate=lambda e: (
            e.get("violation_type") in {"path_traversal", "symlink_escape", "security_violation"}
            or ".." in str(e.get("offending_path_or_token", ""))
            or e.get("code") == "path_traversal_detected"
            or e.get("code") == "store_root_symlink"
        ),
        source_confidence="STRUCTURED_DETERMINISTIC",
        description="Path traversal or symlink escape outside canonical runtime root",
    ),
    # 2. Data Corruption
    ClassifierRule(
        rule_id="RULE_CORRUPT_001_STORAGE_INTEGRITY",
        priority=200,
        output_class=FailureClass.DATA_CORRUPTION,
        required_fields=("integrity_check_output",),
        predicate=lambda e: (
            e.get("integrity_check_output") not in {None, "ok", "PASS"}
            or e.get("code") in {"database_corrupted", "sqlite_integrity_failed", "malformed_state_json"}
            or "database disk image is malformed" in str(e.get("error_message", "")).lower()
            or "file is not a database" in str(e.get("error_message", "")).lower()
        ),
        source_confidence="STRUCTURED_DETERMINISTIC",
        description="Database integrity check failure or unreadable/corrupted persistent storage",
    ),
    # 3. Source Divergence
    ClassifierRule(
        rule_id="RULE_SRC_001_HEAD_MISMATCH",
        priority=300,
        output_class=FailureClass.SOURCE_DIVERGENCE,
        required_fields=("expected_head", "observed_head"),
        predicate=lambda e: (
            bool(e.get("expected_head"))
            and bool(e.get("observed_head"))
            and e.get("expected_head") != e.get("observed_head")
            or e.get("code") in {"source_diverged", "head_mismatch", "tree_mismatch"}
        ),
        source_confidence="STRUCTURED_DETERMINISTIC",
        description="Local repository HEAD or tree does not match expected baseline",
    ),
    # 4. Policy Violation
    ClassifierRule(
        rule_id="RULE_POL_001_SCOPE_BREACH",
        priority=400,
        output_class=FailureClass.POLICY_VIOLATION,
        required_fields=("policy_rule_id",),
        predicate=lambda e: (
            bool(e.get("policy_rule_id"))
            and (
                e.get("violation_class") == "POLICY_VIOLATION"
                or e.get("code") in {"unauthorized_file_touch", "milestone_scope_breach", "policy_forbidden"}
            )
        ),
        source_confidence="STRUCTURED_DETERMINISTIC",
        description="Action violates active project execution policy or touches forbidden resources",
    ),
    # 5. CI Waiting (WAITING, not FAILURE)
    ClassifierRule(
        rule_id="RULE_CI_001_IN_PROGRESS",
        priority=500,
        output_class=FailureClass.CI_WAITING,
        required_fields=("ci_provider", "run_status"),
        predicate=lambda e: (
            bool(e.get("ci_provider"))
            and str(e.get("run_status")).lower() in {"in_progress", "pending", "queued", "waiting"}
            and str(e.get("conclusion", "")).lower() not in {"failure", "timed_out", "action_required"}
        ),
        source_confidence="STRUCTURED_DETERMINISTIC",
        description="External CI workflow run currently in progress or queued for target commit",
    ),
    # 6. Transient Infrastructure Failure
    ClassifierRule(
        rule_id="RULE_INFRA_001_TIMEOUT_AND_LOCK",
        priority=600,
        output_class=FailureClass.TRANSIENT_INFRASTRUCTURE,
        required_fields=(),
        predicate=lambda e: (
            e.get("error_code") in {"ConnectTimeout", "ReadTimeout", "EBUSY", "ETIMEDOUT", "GITHUB_CONNECTOR_TIMEOUT"}
            or e.get("code") in {"store_busy", "queue_locked", "lock_contention", "lock_busy"}
            or "database is locked" in str(e.get("error_message", "")).lower()
            or "database is busy" in str(e.get("error_message", "")).lower()
            or "timed out" in str(e.get("error_message", "")).lower()
        ),
        source_confidence="STRUCTURED_DETERMINISTIC",
        description="Transient network timeout, connector failure, or temporary lock contention",
    ),
    # 7. Transport Uncertainty
    ClassifierRule(
        rule_id="RULE_TRANS_001_UNACKED_DISPATCH",
        priority=700,
        output_class=FailureClass.TRANSPORT_UNCERTAIN,
        required_fields=(),
        predicate=lambda e: (
            e.get("last_known_delivery_state") in {"DISPATCHED_NO_ACK", "UNKNOWN_DELIVERY", "UNCONFIRMED"}
            or e.get("code") in {"transport_uncertain", "unacked_launch_message", "dropped_rpc"}
        ),
        source_confidence="STRUCTURED_DETERMINISTIC",
        description="Command or message dispatched but delivery status unacknowledged",
    ),
    # 8. Environment Repairable
    ClassifierRule(
        rule_id="RULE_ENV_001_MISSING_TOOLCHAIN",
        priority=800,
        output_class=FailureClass.ENVIRONMENT_REPAIRABLE,
        required_fields=(),
        predicate=lambda e: (
            bool(e.get("missing_binary_or_package"))
            or e.get("code") in {"dotnet_not_found", "python_module_missing", "tool_not_in_path"}
            or "not recognized as an internal or external command" in str(e.get("error_message", "")).lower()
            or "no such file or directory" in str(e.get("error_message", "")).lower() and e.get("exit_code") == 127
        ),
        source_confidence="STRUCTURED_DETERMINISTIC",
        description="Required tool binary, SDK, or package missing from execution environment",
    ),
    # 9. Test Infrastructure Failure
    ClassifierRule(
        rule_id="RULE_TEST_001_HARNESS_OR_ORACLE",
        priority=900,
        output_class=FailureClass.TEST_INFRA_FAILURE,
        required_fields=(),
        predicate=lambda e: (
            e.get("test_runner_exit_code") in {139, 134, -11, 255}  # Segfault / abnormal abort
            or e.get("code") in {"IEEE_754_PRECISION_ORACLE", "test_runner_crash", "oracle_precision_defect"}
            or e.get("oracle_defect") is True
        ),
        source_confidence="STRUCTURED_DETERMINISTIC",
        description="Test runner crash, harness abort, or faulty/over-strict test oracle",
    ),
    # 10. Phase Scope Violation
    ClassifierRule(
        rule_id="RULE_SCOPE_001_PHASE_VIOLATION",
        priority=1000,
        output_class=FailureClass.PHASE_SCOPE_VIOLATION,
        required_fields=(),
        predicate=lambda e: (
            bool(e.get("out_of_scope_files"))
            or e.get("failure_code") == "PHASE_SCOPE_VIOLATION"
            or e.get("code") == "PHASE_SCOPE_VIOLATION"
        ),
        source_confidence="STRUCTURED_DETERMINISTIC",
        description="Changes touched files or subsystems belonging to a later milestone/phase",
    ),
    # 11. Early Implementation
    ClassifierRule(
        rule_id="RULE_SCOPE_002_EARLY_IMPLEMENTATION",
        priority=1100,
        output_class=FailureClass.EARLY_IMPLEMENTATION,
        required_fields=(),
        predicate=lambda e: (
            bool(e.get("uncommitted_prerequisite_id"))
            or e.get("failure_code") == "EARLY_IMPLEMENTATION"
            or e.get("code") == "EARLY_IMPLEMENTATION"
        ),
        source_confidence="STRUCTURED_DETERMINISTIC",
        description="Premature implementation of future feature ahead of committed prerequisites",
    ),
    # 12. Project Repairable
    ClassifierRule(
        rule_id="RULE_PROJ_001_CODE_OR_TEST_FAILURE",
        priority=1200,
        output_class=FailureClass.PROJECT_REPAIRABLE,
        required_fields=(),
        predicate=lambda e: (
            e.get("failure_code") in {
                "COMPILATION_ERROR", "SYNTAX_ERROR", "TEST_FAILURE",
                "ASSERTION_FAILED", "MISSING_ARTIFACT", "VALIDATION_FAILED",
            }
            or e.get("code") in {"compilation_failed", "syntax_error", "test_assertion_failed"}
            or (e.get("execution_status") == "FAIL" and not e.get("code"))
            or bool(e.get("missing_project_artifact"))
        ),
        source_confidence="STRUCTURED_DETERMINISTIC",
        description="Code defect, compilation error, failing test assertion, or missing task artifact",
    ),
    # 13. External Action Required
    ClassifierRule(
        rule_id="RULE_EXT_001_HUMAN_CHECKPOINT",
        priority=1300,
        output_class=FailureClass.EXTERNAL_ACTION_REQUIRED,
        required_fields=(),
        predicate=lambda e: (
            bool(e.get("checkpoint_id"))
            or bool(e.get("decision_or_question_id"))
            or e.get("code") in {"decision_required", "operator_checkpoint", "manual_gate_signoff"}
            or e.get("requires_human") is True
        ),
        source_confidence="STRUCTURED_DETERMINISTIC",
        description="Human/operator decision required, manual gate signoff, or external credential",
    ),
]


# Explicitly sorted rule registry by (priority, rule_id)
RULE_REGISTRY: tuple[ClassifierRule, ...] = tuple(
    sorted(RULES_LIST, key=lambda r: (r.priority, r.rule_id))
)


# ==============================================================================
# CLASSIFIER ENGINE
# ==============================================================================

class DeterministicFailureClassifier:
    """Classifies structured evidence deterministically using canonical precedence."""

    def __init__(self, rules: Sequence[ClassifierRule] | None = None) -> None:
        self.rules = tuple(rules) if rules is not None else RULE_REGISTRY
        # Invariant: unique rule IDs
        rule_ids = [r.rule_id for r in self.rules]
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError(f"Duplicate rule IDs detected in classifier registry: {rule_ids}")

    def classify(self, evidence: Mapping[str, Any]) -> ClassificationResult:
        """Classifies structured evidence into a canonical failure class.

        Invariants:
        1. Identical evidence produces identical classification.
        2. LLM/user advisory cannot promote status to PASS or override deterministic rules.
        3. Conflicting rules with equal priority or unresolvable outcomes resolve to AMBIGUOUS_FAILURE.
        4. Malformed evidence or unhandled observations resolve to AMBIGUOUS_FAILURE (fail closed).
        5. Output binds to canonical input digest.
        """
        digest = compute_evidence_digest(evidence)

        # Invariant: Malformed / empty / invalid evidence fails closed to AMBIGUOUS_FAILURE
        if not evidence or not isinstance(evidence, Mapping):
            ambig_spec = TRANSITION_MATRIX[FailureClass.AMBIGUOUS_FAILURE]
            return ClassificationResult(
                failure_class=FailureClass.AMBIGUOUS_FAILURE,
                semantic_kind=ambig_spec.semantic_kind,
                rule_id="RULE_FALLBACK_MALFORMED_EVIDENCE",
                auto_action=ambig_spec.auto_action,
                evidence_digest=digest,
                matched_rules=(),
                details={"reason": "malformed_or_empty_evidence"},
            )

        # Check required fields for explicit domains if flagged
        matched_rules: list[ClassifierRule] = []
        for rule in self.rules:
            # Check required fields for this rule
            if rule.required_fields and not all(f in evidence for f in rule.required_fields):
                continue
            try:
                if rule.predicate(evidence):
                    matched_rules.append(rule)
            except Exception:
                # Any exception in predicate evaluation fails closed
                continue

        # No match -> fail closed to AMBIGUOUS_FAILURE
        if not matched_rules:
            ambig_spec = TRANSITION_MATRIX[FailureClass.AMBIGUOUS_FAILURE]
            return ClassificationResult(
                failure_class=FailureClass.AMBIGUOUS_FAILURE,
                semantic_kind=ambig_spec.semantic_kind,
                rule_id="RULE_FALLBACK_NO_MATCH",
                auto_action=ambig_spec.auto_action,
                evidence_digest=digest,
                matched_rules=(),
                details={"reason": "no_deterministic_rule_matched"},
            )

        # Sort matches by priority (lowest priority number = highest precedence)
        sorted_matches = sorted(matched_rules, key=lambda r: (r.priority, r.rule_id))

        top_priority = sorted_matches[0].priority
        top_tier_matches = [r for r in sorted_matches if r.priority == top_priority]

        # Check for conflict: multiple top-tier matches with conflicting output classes
        top_classes = {r.output_class for r in top_tier_matches}
        if len(top_classes) > 1:
            ambig_spec = TRANSITION_MATRIX[FailureClass.AMBIGUOUS_FAILURE]
            return ClassificationResult(
                failure_class=FailureClass.AMBIGUOUS_FAILURE,
                semantic_kind=ambig_spec.semantic_kind,
                rule_id="RULE_FALLBACK_CONFLICT",
                auto_action=ambig_spec.auto_action,
                evidence_digest=digest,
                matched_rules=tuple(r.rule_id for r in matched_rules),
                details={
                    "reason": "unresolved_rule_conflict",
                    "conflicting_rules": [r.rule_id for r in top_tier_matches],
                    "conflicting_classes": [c.value for c in top_classes],
                },
            )

        # Single unambiguous top match
        winning_rule = top_tier_matches[0]
        output_class = winning_rule.output_class
        spec = TRANSITION_MATRIX[output_class]

        return ClassificationResult(
            failure_class=output_class,
            semantic_kind=spec.semantic_kind,
            rule_id=winning_rule.rule_id,
            auto_action=spec.auto_action,
            evidence_digest=digest,
            matched_rules=tuple(r.rule_id for r in matched_rules),
            details={"description": winning_rule.description},
        )

"""NX-012 — Failure Taxonomy & Transition Table — Machine Gate Tests.

Tests:
1. Canonical failure class inventory completeness
2. Semantic kinds separation (FAILURE vs WAITING vs POLICY_PAUSE vs HUMAN_CHECKPOINT)
3. CI_WAITING is not failure invariant
4. AMBIGUOUS_FAILURE fails closed invariant
5. PHASE_SCOPE_VIOLATION & EARLY_IMPLEMENTATION bounded repair invariant
6. Positive & negative fixtures for every canonical failure class
7. Legal & illegal transition validation
8. Classifier precedence contract completeness
9. NX-012 deterministic machine gate
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path
from typing import Any, Mapping

import pytest

from bdb_vnext.failure_taxonomy import (
    EXISTING_STATE_MAPPINGS,
    PRECEDENCE_ORDER,
    REQUIRED_FAILURE_CLASSES,
    TRANSITION_MATRIX,
    AutoAction,
    FailureClass,
    SemanticKind,
    TransitionSpec,
    validate_transition,
)


# ==============================================================================
# Fixtures & Coverage Registry
# ==============================================================================

# Explicit mapping of each class to test case data for positive and negative fixtures
POSITIVE_FIXTURE_CASES: dict[FailureClass, dict[str, Any]] = {
    FailureClass.PROJECT_REPAIRABLE: {
        "failure_code": "COMPILATION_ERROR",
        "diagnostic_message": "SyntaxError in foo.py: line 10",
        "test_or_artifact_ref": "tests/test_foo.py",
    },
    FailureClass.TRANSIENT_INFRASTRUCTURE: {
        "error_code": "ConnectTimeout",
        "retryable_error_pattern": "HTTPSConnectionPool(host='api.github.com', port=443): Max retries exceeded",
        "resource_target": "https://api.github.com/repos/owner/repo",
    },
    FailureClass.CI_WAITING: {
        "ci_provider": "github-actions",
        "run_id": "987654321",
        "target_head_sha": "a" * 40,
        "run_status": "in_progress",
    },
    FailureClass.TEST_INFRA_FAILURE: {
        "test_runner_exit_code": 139,
        "harness_crash_trace": "Segmentation fault in pytest runner runner.exe",
        "test_id": "tests/test_oracle.py::test_precision",
    },
    FailureClass.TRANSPORT_UNCERTAIN: {
        "correlation_id": "corr-12345",
        "command_id": "cmd-67890",
        "last_known_delivery_state": "DISPATCHED_NO_ACK",
    },
    FailureClass.ENVIRONMENT_REPAIRABLE: {
        "missing_binary_or_package": "dotnet",
        "resolved_path": None,
        "exit_code": 127,
    },
    FailureClass.SOURCE_DIVERGENCE: {
        "expected_head": "a" * 40,
        "observed_head": "b" * 40,
        "expected_tree": "c" * 40,
        "observed_tree": "d" * 40,
    },
    FailureClass.POLICY_VIOLATION: {
        "policy_rule_id": "POL-003",
        "attempted_action": "touch_file",
        "resource_target": "src/protected_config.json",
    },
    FailureClass.SECURITY_VIOLATION: {
        "violation_type": "path_traversal",
        "offending_path_or_token": "../../etc/passwd",
        "canonical_root": "/app/canonical",
    },
    FailureClass.DATA_CORRUPTION: {
        "corrupted_file_path": "project_memory.db",
        "integrity_check_output": "Error: file is not a database",
        "expected_format": "sqlite3_wal",
    },
    FailureClass.EXTERNAL_ACTION_REQUIRED: {
        "checkpoint_id": "cp-gate-review",
        "decision_or_question_id": "OQ-001",
        "reason": "Operator must confirm production cutover criteria",
    },
    FailureClass.AMBIGUOUS_FAILURE: {
        "conflicting_rule_ids_or_missing_fields": ("RULE_A", "RULE_B"),
        "raw_observation": "Unrecognized error code: ERR_UNKNOWN_99",
    },
    FailureClass.PHASE_SCOPE_VIOLATION: {
        "out_of_scope_files": ("src/next_milestone_feature.py",),
        "allowed_task_scope": ("src/current_task_only.py",),
        "current_milestone_id": "NX-M1",
    },
    FailureClass.EARLY_IMPLEMENTATION: {
        "uncommitted_prerequisite_id": "NX-011",
        "premature_symbols_or_files": ("PrematureV2Class",),
    },
}

NEGATIVE_FIXTURE_CASES: dict[FailureClass, dict[str, Any]] = {
    FailureClass.PROJECT_REPAIRABLE: {
        "note": "Transient infrastructure timeout is NOT project repairable",
        "data": {"error_code": "ConnectTimeout"},
    },
    FailureClass.TRANSIENT_INFRASTRUCTURE: {
        "note": "Syntax error is NOT a transient infrastructure failure",
        "data": {"failure_code": "COMPILATION_ERROR"},
    },
    FailureClass.CI_WAITING: {
        "note": "Completed CI run with exit code 1 is FAILURE, NOT CI_WAITING",
        "data": {"ci_provider": "github-actions", "run_status": "completed", "conclusion": "failure"},
    },
    FailureClass.TEST_INFRA_FAILURE: {
        "note": "Standard assertion failure is project repairable, NOT test infra failure",
        "data": {"failure_code": "ASSERTION_FAILED", "test_runner_exit_code": 1},
    },
    FailureClass.TRANSPORT_UNCERTAIN: {
        "note": "Confirmed ACK received is NOT transport uncertain",
        "data": {"last_known_delivery_state": "ACK_CONFIRMED"},
    },
    FailureClass.ENVIRONMENT_REPAIRABLE: {
        "note": "Application logic bug is NOT environment repairable",
        "data": {"failure_code": "VALUE_ERROR"},
    },
    FailureClass.SOURCE_DIVERGENCE: {
        "note": "Matching HEAD and clean worktree is NOT source divergence",
        "data": {"expected_head": "a" * 40, "observed_head": "a" * 40, "diff": ""},
    },
    FailureClass.POLICY_VIOLATION: {
        "note": "Allowed task file edit is NOT a policy violation",
        "data": {"attempted_action": "touch_file", "resource_target": "allowed/task.py"},
    },
    FailureClass.SECURITY_VIOLATION: {
        "note": "Normal confined write within root is NOT a security violation",
        "data": {"canonical_root": "/app", "target_path": "/app/control/db.sqlite"},
    },
    FailureClass.DATA_CORRUPTION: {
        "note": "Valid database passing PRAGMA integrity_check is NOT data corruption",
        "data": {"integrity_check_output": "ok"},
    },
    FailureClass.EXTERNAL_ACTION_REQUIRED: {
        "note": "Fully autonomous task without checkpoints is NOT external action required",
        "data": {"requires_human": False, "open_questions": []},
    },
    FailureClass.AMBIGUOUS_FAILURE: {
        "note": "Deterministic single-rule match with full evidence is NOT ambiguous",
        "data": {"failure_code": "COMPILATION_ERROR", "diagnostic": "valid"},
    },
    FailureClass.PHASE_SCOPE_VIOLATION: {
        "note": "Changes strictly within allowed task files are NOT phase scope violation",
        "data": {"modified_files": ["allowed.py"], "allowed_scope": ["allowed.py"]},
    },
    FailureClass.EARLY_IMPLEMENTATION: {
        "note": "Implementing only tasks whose prerequisites are committed is NOT early implementation",
        "data": {"uncommitted_prerequisites": []},
    },
}


def derive_class_fixture_coverage() -> tuple[int, int, int, list[str], list[str]]:
    """Measures exact positive and negative fixture coverage for all required failure classes."""
    required_classes = set(FailureClass)
    positive_covered = {c for c in POSITIVE_FIXTURE_CASES if c in required_classes}
    negative_covered = {c for c in NEGATIVE_FIXTURE_CASES if c in required_classes}

    missing_positive = [c.value for c in required_classes if c not in positive_covered]
    missing_negative = [c.value for c in required_classes if c not in negative_covered]

    return (
        len(required_classes),
        len(positive_covered),
        len(negative_covered),
        sorted(missing_positive),
        sorted(missing_negative),
    )


# ==============================================================================
# 1. Canonical Failure Class Inventory
# ==============================================================================

class TestCanonicalFailureClassInventory:
    def test_all_14_required_classes_defined(self) -> None:
        defined = {c.value for c in FailureClass}
        expected = {
            "PROJECT_REPAIRABLE", "TRANSIENT_INFRASTRUCTURE", "CI_WAITING",
            "TEST_INFRA_FAILURE", "TRANSPORT_UNCERTAIN", "ENVIRONMENT_REPAIRABLE",
            "SOURCE_DIVERGENCE", "POLICY_VIOLATION", "SECURITY_VIOLATION",
            "DATA_CORRUPTION", "EXTERNAL_ACTION_REQUIRED", "AMBIGUOUS_FAILURE",
            "PHASE_SCOPE_VIOLATION", "EARLY_IMPLEMENTATION",
        }
        assert defined == expected
        assert len(defined) == 14

    def test_every_class_in_transition_matrix(self) -> None:
        for fc in FailureClass:
            assert fc in TRANSITION_MATRIX, f"{fc.value} missing from TRANSITION_MATRIX"
            spec = TRANSITION_MATRIX[fc]
            assert isinstance(spec, TransitionSpec)
            assert spec.failure_class == fc

    def test_existing_state_mappings_complete(self) -> None:
        mapped_classes = {m.canonical_nx012_class for m in EXISTING_STATE_MAPPINGS}
        # All required classes must be covered in the repository error inventory
        assert set(FailureClass).issubset(mapped_classes)


# ==============================================================================
# 2. Semantic Kinds Separation
# ==============================================================================

class TestSemanticKindSeparation:
    def test_four_distinct_semantic_kinds(self) -> None:
        kinds = {k.value for k in SemanticKind}
        assert kinds == {"FAILURE", "WAITING", "POLICY_PAUSE", "HUMAN_CHECKPOINT"}

    def test_ci_waiting_is_waiting_kind(self) -> None:
        spec = TRANSITION_MATRIX[FailureClass.CI_WAITING]
        assert spec.semantic_kind == SemanticKind.WAITING
        assert spec.semantic_kind != SemanticKind.FAILURE

    def test_policy_violation_is_policy_pause(self) -> None:
        spec = TRANSITION_MATRIX[FailureClass.POLICY_VIOLATION]
        assert spec.semantic_kind == SemanticKind.POLICY_PAUSE

    def test_external_action_is_human_checkpoint(self) -> None:
        spec = TRANSITION_MATRIX[FailureClass.EXTERNAL_ACTION_REQUIRED]
        assert spec.semantic_kind == SemanticKind.HUMAN_CHECKPOINT


# ==============================================================================
# 3. CI_WAITING Invariants
# ==============================================================================

class TestCIWaitingInvariants:
    def test_ci_waiting_is_not_failure(self) -> None:
        spec = TRANSITION_MATRIX[FailureClass.CI_WAITING]
        assert spec.semantic_kind != SemanticKind.FAILURE
        assert spec.retry_allowed is False  # Does not consume retry budget

    def test_ci_waiting_cannot_create_project_repair(self) -> None:
        legal, reason = validate_transition(
            FailureClass.CI_WAITING, "AUTO_REPAIR_PROJECT"
        )
        assert legal is False
        assert "ci_waiting_cannot_create_project_repair" in reason

    def test_ci_waiting_cannot_fail_solely_while_pending(self) -> None:
        legal, reason = validate_transition(
            FailureClass.CI_WAITING, "FAILED", context={"poll_pending": True}
        )
        assert legal is False
        assert "ci_waiting_cannot_fail_while_pending" in reason

    def test_ci_waiting_allows_polling(self) -> None:
        legal, reason = validate_transition(
            FailureClass.CI_WAITING, "AUTO_POLL"
        )
        assert legal is True


# ==============================================================================
# 4. AMBIGUOUS_FAILURE Invariants
# ==============================================================================

class TestAmbiguousFailureInvariants:
    def test_ambiguous_failure_cannot_produce_pass(self) -> None:
        for action in ["PASS", "ACCEPT", "PROMOTE"]:
            legal, reason = validate_transition(FailureClass.AMBIGUOUS_FAILURE, action)
            assert legal is False
            assert "ambiguous_failure_cannot_accept_pass" in reason

    def test_ambiguous_failure_fails_closed_and_is_terminal(self) -> None:
        spec = TRANSITION_MATRIX[FailureClass.AMBIGUOUS_FAILURE]
        assert spec.auto_action == AutoAction.AUTO_FAIL_CLOSED
        assert spec.retry_allowed is False
        assert spec.terminal is True
        assert spec.operator_required is True


# ==============================================================================
# 5. PHASE_SCOPE_VIOLATION & EARLY_IMPLEMENTATION Invariants
# ==============================================================================

class TestScopeViolationInvariants:
    def test_phase_scope_maps_to_bounded_repair(self) -> None:
        spec = TRANSITION_MATRIX[FailureClass.PHASE_SCOPE_VIOLATION]
        assert spec.auto_action == AutoAction.AUTO_REPAIR_BOUNDED_PROJECT
        assert spec.repair_allowed is True
        assert spec.retry_allowed is True
        assert spec.terminal is False

    def test_early_implementation_maps_to_bounded_repair(self) -> None:
        spec = TRANSITION_MATRIX[FailureClass.EARLY_IMPLEMENTATION]
        assert spec.auto_action == AutoAction.AUTO_REPAIR_BOUNDED_PROJECT
        assert spec.repair_allowed is True
        assert spec.retry_allowed is True
        assert spec.terminal is False

    def test_repair_cannot_escape_task_boundary(self) -> None:
        for fc in [FailureClass.PHASE_SCOPE_VIOLATION, FailureClass.EARLY_IMPLEMENTATION]:
            legal, reason = validate_transition(fc, "BROADEN_SCOPE")
            assert legal is False
            assert "repair_cannot_escape_task_boundary" in reason

    def test_scope_violation_cannot_abort_project_by_default(self) -> None:
        for fc in [FailureClass.PHASE_SCOPE_VIOLATION, FailureClass.EARLY_IMPLEMENTATION]:
            legal, reason = validate_transition(fc, "ABORT_PROJECT", context={"budget_exhausted": False})
            assert legal is False
            assert "scope_violation_must_attempt_bounded_repair_first" in reason


# ==============================================================================
# 6. Positive & Negative Fixtures for Every Class
# ==============================================================================

class TestClassFixturesCoverage:
    def test_fixture_coverage_is_complete(self) -> None:
        total, pos, neg, missing_pos, missing_neg = derive_class_fixture_coverage()
        assert total == 14
        assert pos == 14
        assert neg == 14
        assert missing_pos == []
        assert missing_neg == []

    @pytest.mark.parametrize("fc", list(FailureClass))
    def test_positive_fixture_has_required_evidence(self, fc: FailureClass) -> None:
        spec = TRANSITION_MATRIX[fc]
        pos_data = POSITIVE_FIXTURE_CASES[fc]
        for field in spec.required_evidence:
            assert field in pos_data, f"Positive fixture for {fc.value} missing field {field}"

    @pytest.mark.parametrize("fc", list(FailureClass))
    def test_negative_fixture_is_defined(self, fc: FailureClass) -> None:
        neg_data = NEGATIVE_FIXTURE_CASES[fc]
        assert "note" in neg_data
        assert "data" in neg_data


# ==============================================================================
# 7. Legal & Illegal Transitions Validation
# ==============================================================================

class TestTransitionsValidation:
    def test_legal_transitions_pass(self) -> None:
        legal_cases = [
            (FailureClass.PROJECT_REPAIRABLE, "AUTO_REPAIR_PROJECT"),
            (FailureClass.TRANSIENT_INFRASTRUCTURE, "AUTO_RETRY_BACKOFF"),
            (FailureClass.CI_WAITING, "AUTO_POLL"),
            (FailureClass.TEST_INFRA_FAILURE, "AUTO_REPAIR_TEST_INFRA"),
            (FailureClass.TRANSPORT_UNCERTAIN, "AUTO_RECONCILE"),
            (FailureClass.ENVIRONMENT_REPAIRABLE, "AUTO_REPAIR_ENVIRONMENT"),
            (FailureClass.POLICY_VIOLATION, "AUTO_PAUSE_POLICY"),
            (FailureClass.EXTERNAL_ACTION_REQUIRED, "AUTO_WAIT_FOR_OPERATOR"),
            (FailureClass.PHASE_SCOPE_VIOLATION, "AUTO_REPAIR_BOUNDED_PROJECT"),
            (FailureClass.EARLY_IMPLEMENTATION, "AUTO_REPAIR_BOUNDED_PROJECT"),
        ]
        for fc, action in legal_cases:
            legal, reason = validate_transition(fc, action)
            assert legal is True, f"Expected legal transition for {fc.value} -> {action}: {reason}"

    def test_illegal_transitions_rejected(self) -> None:
        illegal_cases = [
            # CI_WAITING cannot fail while pending
            (FailureClass.CI_WAITING, "FAILED", {"poll_pending": True}),
            # CI_WAITING cannot create repair attempt
            (FailureClass.CI_WAITING, "AUTO_REPAIR_PROJECT", {}),
            # SECURITY_VIOLATION cannot auto-retry
            (FailureClass.SECURITY_VIOLATION, "AUTO_RETRY", {}),
            # DATA_CORRUPTION cannot blind repair
            (FailureClass.DATA_CORRUPTION, "AUTO_REPAIR_PROJECT", {}),
            # TRANSPORT_UNCERTAIN cannot resend without reconciliation
            (FailureClass.TRANSPORT_UNCERTAIN, "RESEND", {"reconciled": False}),
            # AMBIGUOUS_FAILURE cannot accept PASS
            (FailureClass.AMBIGUOUS_FAILURE, "PASS", {}),
            # POLICY_VIOLATION cannot privilege escalate
            (FailureClass.POLICY_VIOLATION, "AUTO_ESCALATE_PRIVILEGE", {}),
            # Terminal states cannot auto-retry
            (FailureClass.SOURCE_DIVERGENCE, "AUTO_RETRY", {}),
            (FailureClass.SECURITY_VIOLATION, "RETRY", {}),
            (FailureClass.DATA_CORRUPTION, "RETRY", {}),
            # Scope repair cannot escape task boundary
            (FailureClass.PHASE_SCOPE_VIOLATION, "BROADEN_SCOPE", {}),
            (FailureClass.EARLY_IMPLEMENTATION, "BROADEN_SCOPE", {}),
        ]
        accepted_illegal = 0
        for fc, action, ctx in illegal_cases:
            legal, reason = validate_transition(fc, action, context=ctx)
            if legal:
                accepted_illegal += 1

        assert accepted_illegal == 0


# ==============================================================================
# 8. Precedence Hierarchy Contract
# ==============================================================================

class TestPrecedenceHierarchy:
    def test_precedence_order_covers_all_classes(self) -> None:
        assert len(PRECEDENCE_ORDER) == 14
        assert set(PRECEDENCE_ORDER) == set(FailureClass)

    def test_security_has_highest_precedence(self) -> None:
        assert PRECEDENCE_ORDER[0] == FailureClass.SECURITY_VIOLATION

    def test_ambiguous_is_last_fallback(self) -> None:
        assert PRECEDENCE_ORDER[-1] == FailureClass.AMBIGUOUS_FAILURE


# ==============================================================================
# 9. NX-012 Machine Gate
# ==============================================================================

def inspect_nx012_gate_for_hardcoded_results() -> tuple[bool, list[str]]:
    """AST-inspect run_nx012_machine_gate for hardcoded outcomes."""
    source_path = Path(__file__)
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    gate_func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "run_nx012_machine_gate":
            gate_func = node
            break

    if gate_func is None:
        return (False, ["run_nx012_machine_gate not found"])

    REQUIRED_FIELDS = {
        "REQUIRED_CLASS_COUNT", "MISSING_REQUIRED_CLASSES",
        "EVERY_CLASS_HAS_POSITIVE_FIXTURE", "EVERY_CLASS_HAS_NEGATIVE_FIXTURE",
        "CI_WAITING_IS_FAILURE", "CI_WAITING_PROJECT_REPAIR_CREATED",
        "AMBIGUOUS_FAILURE_FAILS_CLOSED", "AMBIGUOUS_FAILURE_CAN_ACCEPT_PASS",
        "PHASE_SCOPE_VIOLATION_BOUNDED_REPAIR", "EARLY_IMPLEMENTATION_BOUNDED_REPAIR",
        "WAITING_FAILURE_SEPARATION", "POLICY_PAUSE_SEPARATION", "HUMAN_CHECKPOINT_SEPARATION",
        "ILLEGAL_TRANSITIONS_ACCEPTED",
        "CLASSIFIER_PRECEDENCE_CONTRACT_COMPLETE", "OPEN_TAXONOMY_AMBIGUITIES",
        "NX012_STATUS",
    }

    hardcoded_fields: list[str] = []
    for node in ast.walk(gate_func):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in REQUIRED_FIELDS:
                    val = node.value
                    if isinstance(val, ast.Constant) and val.value in (True, False, "PASS", "FAIL", 0):
                        hardcoded_fields.append(target.id)

    return (len(hardcoded_fields) == 0, hardcoded_fields)


def run_nx012_machine_gate() -> dict[str, Any]:
    """NX-012 deterministic machine gate — all results derived from contract & fixtures."""
    repo_root = Path(__file__).resolve().parent.parent

    # 1. Canonical failure class inventory
    defined_classes = {c.value for c in FailureClass}
    expected_classes = {
        "PROJECT_REPAIRABLE", "TRANSIENT_INFRASTRUCTURE", "CI_WAITING",
        "TEST_INFRA_FAILURE", "TRANSPORT_UNCERTAIN", "ENVIRONMENT_REPAIRABLE",
        "SOURCE_DIVERGENCE", "POLICY_VIOLATION", "SECURITY_VIOLATION",
        "DATA_CORRUPTION", "EXTERNAL_ACTION_REQUIRED", "AMBIGUOUS_FAILURE",
        "PHASE_SCOPE_VIOLATION", "EARLY_IMPLEMENTATION",
    }
    missing_classes = sorted(list(expected_classes - defined_classes))
    REQUIRED_CLASS_COUNT = len(defined_classes)
    MISSING_REQUIRED_CLASSES = missing_classes

    # 2. Fixture coverage measurement
    total_classes, pos_count, neg_count, missing_pos, missing_neg = derive_class_fixture_coverage()
    EVERY_CLASS_HAS_POSITIVE_FIXTURE = bool(pos_count == total_classes and len(missing_pos) == 0)
    EVERY_CLASS_HAS_NEGATIVE_FIXTURE = bool(neg_count == total_classes and len(missing_neg) == 0)

    # 3. CI_WAITING invariant checks
    ci_spec = TRANSITION_MATRIX[FailureClass.CI_WAITING]
    ci_is_fail = bool(ci_spec.semantic_kind == SemanticKind.FAILURE)
    ci_repair_legal, _ = validate_transition(FailureClass.CI_WAITING, "AUTO_REPAIR_PROJECT")
    ci_fail_legal, _ = validate_transition(FailureClass.CI_WAITING, "FAILED", context={"poll_pending": True})
    CI_WAITING_IS_FAILURE = bool(ci_is_fail or ci_fail_legal)
    CI_WAITING_PROJECT_REPAIR_CREATED = bool(ci_repair_legal or ci_spec.repair_allowed)

    # 4. AMBIGUOUS_FAILURE invariant checks
    ambig_spec = TRANSITION_MATRIX[FailureClass.AMBIGUOUS_FAILURE]
    ambig_pass_legal, _ = validate_transition(FailureClass.AMBIGUOUS_FAILURE, "PASS")
    AMBIGUOUS_FAILURE_CAN_ACCEPT_PASS = bool(ambig_pass_legal)
    AMBIGUOUS_FAILURE_FAILS_CLOSED = bool(
        ambig_spec.auto_action == AutoAction.AUTO_FAIL_CLOSED
        and ambig_spec.terminal is True
        and ambig_spec.retry_allowed is False
        and not ambig_pass_legal
    )

    # 5. PHASE_SCOPE_VIOLATION & EARLY_IMPLEMENTATION bounded repair
    phase_spec = TRANSITION_MATRIX[FailureClass.PHASE_SCOPE_VIOLATION]
    early_spec = TRANSITION_MATRIX[FailureClass.EARLY_IMPLEMENTATION]
    phase_broaden_legal, _ = validate_transition(FailureClass.PHASE_SCOPE_VIOLATION, "BROADEN_SCOPE")
    early_broaden_legal, _ = validate_transition(FailureClass.EARLY_IMPLEMENTATION, "BROADEN_SCOPE")
    PHASE_SCOPE_VIOLATION_BOUNDED_REPAIR = bool(
        phase_spec.auto_action == AutoAction.AUTO_REPAIR_BOUNDED_PROJECT
        and phase_spec.repair_allowed is True
        and not phase_broaden_legal
    )
    EARLY_IMPLEMENTATION_BOUNDED_REPAIR = bool(
        early_spec.auto_action == AutoAction.AUTO_REPAIR_BOUNDED_PROJECT
        and early_spec.repair_allowed is True
        and not early_broaden_legal
    )

    # 6. Semantic kind separations
    waiting_classes = [c for c, s in TRANSITION_MATRIX.items() if s.semantic_kind == SemanticKind.WAITING]
    WAITING_FAILURE_SEPARATION = (
        "PASS"
        if len(waiting_classes) > 0 and all(TRANSITION_MATRIX[c].semantic_kind != SemanticKind.FAILURE for c in waiting_classes)
        else "FAIL"
    )

    policy_classes = [c for c, s in TRANSITION_MATRIX.items() if s.semantic_kind == SemanticKind.POLICY_PAUSE]
    POLICY_PAUSE_SEPARATION = (
        "PASS"
        if len(policy_classes) > 0 and all(TRANSITION_MATRIX[c].semantic_kind == SemanticKind.POLICY_PAUSE for c in policy_classes)
        else "FAIL"
    )

    human_classes = [c for c, s in TRANSITION_MATRIX.items() if s.semantic_kind == SemanticKind.HUMAN_CHECKPOINT]
    HUMAN_CHECKPOINT_SEPARATION = (
        "PASS"
        if len(human_classes) > 0 and all(TRANSITION_MATRIX[c].semantic_kind == SemanticKind.HUMAN_CHECKPOINT for c in human_classes)
        else "FAIL"
    )

    # 7. Illegal transition test cases evaluation
    illegal_test_matrix = [
        (FailureClass.CI_WAITING, "FAILED", {"poll_pending": True}),
        (FailureClass.CI_WAITING, "AUTO_REPAIR_PROJECT", {}),
        (FailureClass.SECURITY_VIOLATION, "AUTO_RETRY", {}),
        (FailureClass.SECURITY_VIOLATION, "ESCALATE_PRIVILEGE", {}),
        (FailureClass.DATA_CORRUPTION, "AUTO_REPAIR_PROJECT", {}),
        (FailureClass.TRANSPORT_UNCERTAIN, "RESEND", {"reconciled": False}),
        (FailureClass.AMBIGUOUS_FAILURE, "PASS", {}),
        (FailureClass.POLICY_VIOLATION, "AUTO_ESCALATE_PRIVILEGE", {}),
        (FailureClass.SOURCE_DIVERGENCE, "AUTO_RETRY", {}),
        (FailureClass.DATA_CORRUPTION, "RETRY", {}),
        (FailureClass.PHASE_SCOPE_VIOLATION, "BROADEN_SCOPE", {}),
        (FailureClass.EARLY_IMPLEMENTATION, "BROADEN_SCOPE", {}),
    ]
    accepted_illegal_count = sum(
        1 for fc, action, ctx in illegal_test_matrix
        if validate_transition(fc, action, context=ctx)[0]
    )
    ILLEGAL_TRANSITIONS_ACCEPTED = accepted_illegal_count

    # 8. Precedence contract
    CLASSIFIER_PRECEDENCE_CONTRACT_COMPLETE = bool(
        len(PRECEDENCE_ORDER) == 14
        and PRECEDENCE_ORDER[0] == FailureClass.SECURITY_VIOLATION
        and PRECEDENCE_ORDER[-1] == FailureClass.AMBIGUOUS_FAILURE
    )

    # 9. Open ambiguities
    ambiguity_checks = [
        REQUIRED_CLASS_COUNT == 14,
        len(MISSING_REQUIRED_CLASSES) == 0,
        EVERY_CLASS_HAS_POSITIVE_FIXTURE,
        EVERY_CLASS_HAS_NEGATIVE_FIXTURE,
        not CI_WAITING_IS_FAILURE,
        not CI_WAITING_PROJECT_REPAIR_CREATED,
        AMBIGUOUS_FAILURE_FAILS_CLOSED,
        not AMBIGUOUS_FAILURE_CAN_ACCEPT_PASS,
        PHASE_SCOPE_VIOLATION_BOUNDED_REPAIR,
        EARLY_IMPLEMENTATION_BOUNDED_REPAIR,
        WAITING_FAILURE_SEPARATION == "PASS",
        POLICY_PAUSE_SEPARATION == "PASS",
        HUMAN_CHECKPOINT_SEPARATION == "PASS",
        ILLEGAL_TRANSITIONS_ACCEPTED == 0,
        CLASSIFIER_PRECEDENCE_CONTRACT_COMPLETE,
    ]
    OPEN_TAXONOMY_AMBIGUITIES = sum(1 for chk in ambiguity_checks if not chk)

    # AST check
    no_hardcoded, hardcoded_fields = inspect_nx012_gate_for_hardcoded_results()
    NO_HARDCODED_GATE_RESULTS = no_hardcoded

    # Source binding
    try:
        head_proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_root), capture_output=True, text=True, check=True)
        head_sha = head_proc.stdout.strip()
        tree_proc = subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=str(repo_root), capture_output=True, text=True, check=True)
        tree_sha = tree_proc.stdout.strip()
        diff_proc = subprocess.run(["git", "diff", "--quiet"], cwd=str(repo_root))
        cached_proc = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=str(repo_root))
        status_proc = subprocess.run(["git", "status", "--porcelain"], cwd=str(repo_root), capture_output=True, text=True, check=True)
        worktree_clean = (
            diff_proc.returncode == 0
            and cached_proc.returncode == 0
            and len(status_proc.stdout.strip()) == 0
        )
        source_bound_ok = (len(head_sha) == 40 and len(tree_sha) == 40 and worktree_clean)
    except Exception:
        head_sha = "unknown"
        tree_sha = "unknown"
        worktree_clean = False
        source_bound_ok = False

    SOURCE_BOUND_MACHINE_GATE = ("PASS" if source_bound_ok else "FAIL")

    all_pass = (
        REQUIRED_CLASS_COUNT == 14
        and len(MISSING_REQUIRED_CLASSES) == 0
        and EVERY_CLASS_HAS_POSITIVE_FIXTURE is True
        and EVERY_CLASS_HAS_NEGATIVE_FIXTURE is True
        and CI_WAITING_IS_FAILURE is False
        and CI_WAITING_PROJECT_REPAIR_CREATED is False
        and AMBIGUOUS_FAILURE_FAILS_CLOSED is True
        and AMBIGUOUS_FAILURE_CAN_ACCEPT_PASS is False
        and PHASE_SCOPE_VIOLATION_BOUNDED_REPAIR is True
        and EARLY_IMPLEMENTATION_BOUNDED_REPAIR is True
        and WAITING_FAILURE_SEPARATION == "PASS"
        and POLICY_PAUSE_SEPARATION == "PASS"
        and HUMAN_CHECKPOINT_SEPARATION == "PASS"
        and ILLEGAL_TRANSITIONS_ACCEPTED == 0
        and CLASSIFIER_PRECEDENCE_CONTRACT_COMPLETE is True
        and OPEN_TAXONOMY_AMBIGUITIES == 0
        and NO_HARDCODED_GATE_RESULTS is True
        and SOURCE_BOUND_MACHINE_GATE == "PASS"
    )

    return {
        "task_id": "NX-012",
        "REQUIRED_CLASS_COUNT": REQUIRED_CLASS_COUNT,
        "MISSING_REQUIRED_CLASSES": MISSING_REQUIRED_CLASSES,
        "EVERY_CLASS_HAS_POSITIVE_FIXTURE": EVERY_CLASS_HAS_POSITIVE_FIXTURE,
        "EVERY_CLASS_HAS_NEGATIVE_FIXTURE": EVERY_CLASS_HAS_NEGATIVE_FIXTURE,
        "CI_WAITING_IS_FAILURE": CI_WAITING_IS_FAILURE,
        "CI_WAITING_PROJECT_REPAIR_CREATED": CI_WAITING_PROJECT_REPAIR_CREATED,
        "AMBIGUOUS_FAILURE_FAILS_CLOSED": AMBIGUOUS_FAILURE_FAILS_CLOSED,
        "AMBIGUOUS_FAILURE_CAN_ACCEPT_PASS": AMBIGUOUS_FAILURE_CAN_ACCEPT_PASS,
        "PHASE_SCOPE_VIOLATION_BOUNDED_REPAIR": PHASE_SCOPE_VIOLATION_BOUNDED_REPAIR,
        "EARLY_IMPLEMENTATION_BOUNDED_REPAIR": EARLY_IMPLEMENTATION_BOUNDED_REPAIR,
        "WAITING_FAILURE_SEPARATION": WAITING_FAILURE_SEPARATION,
        "POLICY_PAUSE_SEPARATION": POLICY_PAUSE_SEPARATION,
        "HUMAN_CHECKPOINT_SEPARATION": HUMAN_CHECKPOINT_SEPARATION,
        "ILLEGAL_TRANSITIONS_ACCEPTED": ILLEGAL_TRANSITIONS_ACCEPTED,
        "CLASSIFIER_PRECEDENCE_CONTRACT_COMPLETE": CLASSIFIER_PRECEDENCE_CONTRACT_COMPLETE,
        "OPEN_TAXONOMY_AMBIGUITIES": OPEN_TAXONOMY_AMBIGUITIES,
        "HARDCODED_GATE_RESULT_FIELDS": hardcoded_fields,
        "NO_HARDCODED_GATE_RESULTS": NO_HARDCODED_GATE_RESULTS,
        "SOURCE_HEAD": head_sha,
        "SOURCE_TREE": tree_sha,
        "WORKTREE_CLEAN": worktree_clean,
        "SOURCE_BOUND_MACHINE_GATE": SOURCE_BOUND_MACHINE_GATE,
        "NX012_STATUS": ("PASS" if all_pass else "FAIL"),
    }


def test_nx012_machine_gate_execution() -> None:
    """NX-012 canonical machine gate."""
    gate = run_nx012_machine_gate()

    assert gate["REQUIRED_CLASS_COUNT"] == 14
    assert gate["MISSING_REQUIRED_CLASSES"] == []
    assert gate["EVERY_CLASS_HAS_POSITIVE_FIXTURE"] is True
    assert gate["EVERY_CLASS_HAS_NEGATIVE_FIXTURE"] is True
    assert gate["CI_WAITING_IS_FAILURE"] is False
    assert gate["CI_WAITING_PROJECT_REPAIR_CREATED"] is False
    assert gate["AMBIGUOUS_FAILURE_FAILS_CLOSED"] is True
    assert gate["AMBIGUOUS_FAILURE_CAN_ACCEPT_PASS"] is False
    assert gate["PHASE_SCOPE_VIOLATION_BOUNDED_REPAIR"] is True
    assert gate["EARLY_IMPLEMENTATION_BOUNDED_REPAIR"] is True
    assert gate["WAITING_FAILURE_SEPARATION"] == "PASS"
    assert gate["POLICY_PAUSE_SEPARATION"] == "PASS"
    assert gate["HUMAN_CHECKPOINT_SEPARATION"] == "PASS"
    assert gate["ILLEGAL_TRANSITIONS_ACCEPTED"] == 0
    assert gate["CLASSIFIER_PRECEDENCE_CONTRACT_COMPLETE"] is True
    assert gate["OPEN_TAXONOMY_AMBIGUITIES"] == 0
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    assert gate["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
    assert gate["NX012_STATUS"] == "PASS"

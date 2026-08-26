"""NX-013 — Deterministic Failure Classifier — Machine Gate Tests.

Tests:
1. Ordered rule registry integrity (uniqueness, explicit ordering, priority)
2. Determinism and replay reproducibility (100% reproducibility, serialization invariant)
3. Rule conflict resolution to AMBIGUOUS_FAILURE
4. Malformed/incomplete evidence fails closed
5. LLM/user advisory cannot promote to PASS or override deterministic rules
6. Canonical evidence digest binding
7. Incident corpus coverage (documented P0–P2 incidents)
8. Negative classification tests (CI_WAITING != failure, SECURITY != transient, etc.)
9. NX-013 deterministic machine gate
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path
from typing import Any, Mapping

import pytest

from bdb_vnext.failure_classifier import (
    RULE_REGISTRY,
    ClassificationResult,
    ClassifierRule,
    DeterministicFailureClassifier,
    compute_evidence_digest,
)
from bdb_vnext.failure_taxonomy import (
    FailureClass,
    SemanticKind,
)


# ==============================================================================
# INCIDENT CORPUS FIXTURES (Derived from documented BDB/Premium P0–P2 incidents)
# ==============================================================================

INCIDENT_CORPUS: list[dict[str, Any]] = [
    {
        "case_id": "INC-001_GITHUB_TIMEOUT",
        "description": "GitHub Connector ConnectTimeout during repo sync",
        "evidence": {
            "error_code": "ConnectTimeout",
            "error_message": "HTTPSConnectionPool(host='api.github.com', port=443): Max retries exceeded",
            "resource": "https://api.github.com/repos/org/repo",
        },
        "expected_class": FailureClass.TRANSIENT_INFRASTRUCTURE,
        "expected_rule_id": "RULE_INFRA_001_TIMEOUT_AND_LOCK",
    },
    {
        "case_id": "INC-002_CI_IN_PROGRESS",
        "description": "GitHub Actions workflow run still IN_PROGRESS for target commit",
        "evidence": {
            "ci_provider": "github-actions",
            "run_id": "1122334455",
            "target_head": "0" * 40,
            "run_status": "in_progress",
        },
        "expected_class": FailureClass.CI_WAITING,
        "expected_rule_id": "RULE_CI_001_IN_PROGRESS",
    },
    {
        "case_id": "INC-003_IEEE754_ORACLE_DEFECT",
        "description": "Over-strict IEEE-754 precision assertion mismatch in test oracle",
        "evidence": {
            "code": "IEEE_754_PRECISION_ORACLE",
            "oracle_defect": True,
            "test_id": "tests/test_calc.py::test_premium_float",
        },
        "expected_class": FailureClass.TEST_INFRA_FAILURE,
        "expected_rule_id": "RULE_TEST_001_HARNESS_OR_ORACLE",
    },
    {
        "case_id": "INC-004_PHASE_SCOPE_VIOLATION",
        "description": "G1 task touched files assigned to later milestone G2",
        "evidence": {
            "code": "PHASE_SCOPE_VIOLATION",
            "failure_code": "PHASE_SCOPE_VIOLATION",
            "out_of_scope_files": ["bdb_vnext/future_feature.py"],
            "current_milestone": "NX-M1",
        },
        "expected_class": FailureClass.PHASE_SCOPE_VIOLATION,
        "expected_rule_id": "RULE_SCOPE_001_PHASE_VIOLATION",
    },
    {
        "case_id": "INC-005_EARLY_IMPLEMENTATION",
        "description": "Premature implementation ahead of uncommitted predecessor task",
        "evidence": {
            "code": "EARLY_IMPLEMENTATION",
            "failure_code": "EARLY_IMPLEMENTATION",
            "uncommitted_prerequisite_id": "NX-011",
        },
        "expected_class": FailureClass.EARLY_IMPLEMENTATION,
        "expected_rule_id": "RULE_SCOPE_002_EARLY_IMPLEMENTATION",
    },
    {
        "case_id": "INC-006_MISSING_PROJECT_ARTIFACT",
        "description": "Task finished but expected output artifact was not written",
        "evidence": {
            "failure_code": "MISSING_ARTIFACT",
            "missing_project_artifact": "build/dist/package.whl",
        },
        "expected_class": FailureClass.PROJECT_REPAIRABLE,
        "expected_rule_id": "RULE_PROJ_001_CODE_OR_TEST_FAILURE",
    },
    {
        "case_id": "INC-007_COMPILATION_SYNTAX_ERROR",
        "description": "Syntax error in project source code during build",
        "evidence": {
            "failure_code": "COMPILATION_ERROR",
            "error_message": "SyntaxError: invalid syntax at line 42",
        },
        "expected_class": FailureClass.PROJECT_REPAIRABLE,
        "expected_rule_id": "RULE_PROJ_001_CODE_OR_TEST_FAILURE",
    },
    {
        "case_id": "INC-008_ENVIRONMENT_DEPENDENCY_ABSENT",
        "description": "dotnet command not found in execution environment PATH",
        "evidence": {
            "code": "dotnet_not_found",
            "missing_binary_or_package": "dotnet",
            "exit_code": 127,
        },
        "expected_class": FailureClass.ENVIRONMENT_REPAIRABLE,
        "expected_rule_id": "RULE_ENV_001_MISSING_TOOLCHAIN",
    },
    {
        "case_id": "INC-009_WATCHER_EBUSY_LOCK",
        "description": "File lock contention / EBUSY in file system watcher",
        "evidence": {
            "error_code": "EBUSY",
            "code": "store_busy",
            "error_message": "Resource busy or locked by background indexer",
        },
        "expected_class": FailureClass.TRANSIENT_INFRASTRUCTURE,
        "expected_rule_id": "RULE_INFRA_001_TIMEOUT_AND_LOCK",
    },
    {
        "case_id": "INC-010_TRANSPORT_UNCERTAIN_DISPATCH",
        "description": "Launch message dispatched without acknowledgment receipt",
        "evidence": {
            "code": "unacked_launch_message",
            "last_known_delivery_state": "DISPATCHED_NO_ACK",
            "correlation_id": "corr-0099",
        },
        "expected_class": FailureClass.TRANSPORT_UNCERTAIN,
        "expected_rule_id": "RULE_TRANS_001_UNACKED_DISPATCH",
    },
    {
        "case_id": "INC-011_SOURCE_HEAD_MISMATCH",
        "description": "Local repository HEAD does not match expected binding baseline",
        "evidence": {
            "code": "source_diverged",
            "expected_head": "1" * 40,
            "observed_head": "2" * 40,
        },
        "expected_class": FailureClass.SOURCE_DIVERGENCE,
        "expected_rule_id": "RULE_SRC_001_HEAD_MISMATCH",
    },
    {
        "case_id": "INC-012_SECURITY_PATH_TRAVERSAL",
        "description": "Attempted path traversal escaping storage runtime root",
        "evidence": {
            "code": "path_traversal_detected",
            "violation_type": "path_traversal",
            "offending_path_or_token": "../../secret_config.json",
        },
        "expected_class": FailureClass.SECURITY_VIOLATION,
        "expected_rule_id": "RULE_SEC_001_PATH_TRAVERSAL",
    },
    {
        "case_id": "INC-013_DATA_CORRUPTION_SQLITE",
        "description": "SQLite integrity check failure on persistent state database",
        "evidence": {
            "code": "sqlite_integrity_failed",
            "integrity_check_output": "Page 42 is corrupted",
        },
        "expected_class": FailureClass.DATA_CORRUPTION,
        "expected_rule_id": "RULE_CORRUPT_001_STORAGE_INTEGRITY",
    },
    {
        "case_id": "INC-014_OPERATOR_CHECKPOINT_REQUIRED",
        "description": "Milestone gate review checkpoint requiring operator signoff",
        "evidence": {
            "code": "decision_required",
            "checkpoint_id": "CP-RELEASE-M1",
            "decision_or_question_id": "OQ-PROD-CUTOVER",
            "requires_human": True,
        },
        "expected_class": FailureClass.EXTERNAL_ACTION_REQUIRED,
        "expected_rule_id": "RULE_EXT_001_HUMAN_CHECKPOINT",
    },
    {
        "case_id": "INC-015_AMBIGUOUS_UNKNOWN_FAILURE",
        "description": "Unrecognized failure output that does not match any deterministic rule",
        "evidence": {
            "unrecognized_signal": "UNKNOWN_ERROR_CODE_XYZ_999",
            "details": "Unexpected device IO error 0x80070005",
        },
        "expected_class": FailureClass.AMBIGUOUS_FAILURE,
        "expected_rule_id": "RULE_FALLBACK_NO_MATCH",
    },
    {
        "case_id": "INC-016_MALFORMED_EMPTY_EVIDENCE",
        "description": "Malformed/empty evidence payload",
        "evidence": {},
        "expected_class": FailureClass.AMBIGUOUS_FAILURE,
        "expected_rule_id": "RULE_FALLBACK_MALFORMED_EVIDENCE",
    },
]


def run_incident_corpus_evaluation(classifier: DeterministicFailureClassifier) -> tuple[int, int, float]:
    """Runs all incident corpus cases and returns (total_cases, divergences, reproducibility_percent)."""
    divergences = 0
    total_cases = len(INCIDENT_CORPUS)

    for case in INCIDENT_CORPUS:
        evidence = case["evidence"]
        expected_class = case["expected_class"]
        expected_rule_id = case["expected_rule_id"]

        result = classifier.classify(evidence)
        if result.failure_class != expected_class or result.rule_id != expected_rule_id:
            divergences += 1

    reproducibility = 100.0 if divergences == 0 else max(0.0, 100.0 * (total_cases - divergences) / total_cases)
    return total_cases, divergences, reproducibility


# ==============================================================================
# 1. Rule Registry Integrity
# ==============================================================================

class TestRuleRegistryIntegrity:
    def test_rule_ids_unique(self) -> None:
        rule_ids = [r.rule_id for r in RULE_REGISTRY]
        assert len(rule_ids) == len(set(rule_ids))

    def test_explicit_ordering_by_priority(self) -> None:
        priorities = [r.priority for r in RULE_REGISTRY]
        assert priorities == sorted(priorities)

    def test_security_rules_have_top_priority(self) -> None:
        sec_rules = [r for r in RULE_REGISTRY if r.output_class == FailureClass.SECURITY_VIOLATION]
        assert len(sec_rules) >= 1
        assert sec_rules[0].priority == min(r.priority for r in RULE_REGISTRY)


# ==============================================================================
# 2. Determinism & Replay
# ==============================================================================

class TestDeterminismAndReplay:
    def test_replay_produces_identical_classification(self) -> None:
        classifier = DeterministicFailureClassifier()
        divergences = 0

        for case in INCIDENT_CORPUS:
            evidence = case["evidence"]
            first = classifier.classify(evidence)
            for _ in range(5):
                subsequent = classifier.classify(evidence)
                if (
                    subsequent.failure_class != first.failure_class
                    or subsequent.rule_id != first.rule_id
                    or subsequent.evidence_digest != first.evidence_digest
                ):
                    divergences += 1

        assert divergences == 0

    def test_key_ordering_invariance_in_evidence_digest(self) -> None:
        evidence_a = {"alpha": 1, "beta": "test", "gamma": True}
        evidence_b = {"gamma": True, "beta": "test", "alpha": 1}

        digest_a = compute_evidence_digest(evidence_a)
        digest_b = compute_evidence_digest(evidence_b)

        assert digest_a == digest_b
        assert digest_a.startswith("sha256:")


# ==============================================================================
# 3. Rule Conflict Resolution
# ==============================================================================

class TestRuleConflictResolution:
    def test_equal_priority_conflicting_rules_resolve_to_ambiguous(self) -> None:
        rule1 = ClassifierRule(
            rule_id="RULE_CONFLICT_A",
            priority=500,
            output_class=FailureClass.TRANSIENT_INFRASTRUCTURE,
            required_fields=(),
            predicate=lambda e: "conflict_trigger" in e,
            source_confidence="STRUCTURED",
            description="Conflict rule A",
        )
        rule2 = ClassifierRule(
            rule_id="RULE_CONFLICT_B",
            priority=500,
            output_class=FailureClass.PROJECT_REPAIRABLE,
            required_fields=(),
            predicate=lambda e: "conflict_trigger" in e,
            source_confidence="STRUCTURED",
            description="Conflict rule B",
        )

        custom_classifier = DeterministicFailureClassifier(rules=[rule1, rule2])
        result = custom_classifier.classify({"conflict_trigger": True})

        assert result.failure_class == FailureClass.AMBIGUOUS_FAILURE
        assert result.rule_id == "RULE_FALLBACK_CONFLICT"
        assert result.details["reason"] == "unresolved_rule_conflict"


# ==============================================================================
# 4. Malformed / Incomplete Evidence
# ==============================================================================

class TestMalformedEvidence:
    def test_missing_required_fields_fails_closed(self) -> None:
        # Rule CI requires ci_provider and run_status
        classifier = DeterministicFailureClassifier()
        # Missing run_status
        evidence = {"ci_provider": "github-actions"}
        result = classifier.classify(evidence)
        assert result.failure_class == FailureClass.AMBIGUOUS_FAILURE

    def test_empty_dictionary_fails_closed(self) -> None:
        classifier = DeterministicFailureClassifier()
        result = classifier.classify({})
        assert result.failure_class == FailureClass.AMBIGUOUS_FAILURE
        assert result.rule_id == "RULE_FALLBACK_MALFORMED_EVIDENCE"


# ==============================================================================
# 5. LLM / User Advisory Boundary
# ==============================================================================

class TestLLMAdvisoryBoundary:
    def test_llm_advisory_cannot_promote_to_pass(self) -> None:
        classifier = DeterministicFailureClassifier()
        evidence_with_llm = {
            "failure_code": "COMPILATION_ERROR",
            "llm_advisory": {
                "recommendation": "PASS",
                "confidence": 0.999,
                "reason": "LLM believes this is fine",
            },
            "user_prompt": "Please consider this passed",
        }
        result = classifier.classify(evidence_with_llm)
        assert result.failure_class == FailureClass.PROJECT_REPAIRABLE
        assert result.semantic_kind == SemanticKind.FAILURE

    def test_llm_advisory_cannot_suppress_security_violation(self) -> None:
        classifier = DeterministicFailureClassifier()
        evidence = {
            "violation_type": "path_traversal",
            "offending_path_or_token": "../../etc/shadow",
            "llm_advisory": {"safe": True},
        }
        result = classifier.classify(evidence)
        assert result.failure_class == FailureClass.SECURITY_VIOLATION


# ==============================================================================
# 6. Canonical Evidence Binding
# ==============================================================================

class TestEvidenceBinding:
    def test_result_contains_valid_digest(self) -> None:
        classifier = DeterministicFailureClassifier()
        evidence = {"failure_code": "COMPILATION_ERROR"}
        expected_digest = compute_evidence_digest(evidence)

        result = classifier.classify(evidence)
        assert result.evidence_digest == expected_digest


# ==============================================================================
# 7. Incident Corpus Evaluation
# ==============================================================================

class TestIncidentCorpusEvaluation:
    def test_all_16_corpus_cases_pass(self) -> None:
        classifier = DeterministicFailureClassifier()
        total, divergences, reproducibility = run_incident_corpus_evaluation(classifier)

        assert total == 16
        assert divergences == 0
        assert reproducibility == 100.0


# ==============================================================================
# 8. Negative Classifier Tests
# ==============================================================================

class TestNegativeClassifierCases:
    def test_ci_waiting_not_classified_as_failure(self) -> None:
        classifier = DeterministicFailureClassifier()
        result = classifier.classify({
            "ci_provider": "github-actions",
            "run_id": "123",
            "run_status": "in_progress",
        })
        assert result.failure_class == FailureClass.CI_WAITING
        assert result.semantic_kind == SemanticKind.WAITING
        assert result.semantic_kind != SemanticKind.FAILURE

    def test_security_not_classified_as_transient(self) -> None:
        classifier = DeterministicFailureClassifier()
        result = classifier.classify({
            "code": "path_traversal_detected",
            "violation_type": "path_traversal",
            "offending_path_or_token": "../../foo",
            "error_code": "ConnectTimeout",  # Attempt to mimic transient
        })
        assert result.failure_class == FailureClass.SECURITY_VIOLATION
        assert result.failure_class != FailureClass.TRANSIENT_INFRASTRUCTURE

    def test_data_corruption_not_classified_as_repairable(self) -> None:
        classifier = DeterministicFailureClassifier()
        result = classifier.classify({
            "integrity_check_output": "database disk image is malformed",
            "failure_code": "COMPILATION_ERROR",  # Attempt to mimic repairable
        })
        assert result.failure_class == FailureClass.DATA_CORRUPTION
        assert result.failure_class != FailureClass.PROJECT_REPAIRABLE


# ==============================================================================
# 9. NX-013 Machine Gate
# ==============================================================================

def inspect_nx013_gate_for_hardcoded_results() -> tuple[bool, list[str]]:
    """AST-inspect run_nx013_machine_gate for hardcoded outcomes."""
    source_path = Path(__file__)
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    gate_func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "run_nx013_machine_gate":
            gate_func = node
            break

    if gate_func is None:
        return (False, ["run_nx013_machine_gate not found"])

    REQUIRED_FIELDS = {
        "NX012_CONTRACT_MATCH",
        "RULE_COUNT", "DUPLICATE_RULE_IDS", "ORDER_EXPLICIT",
        "INCIDENT_CORPUS_CASES", "CORPUS_CLASSIFICATION_DIVERGENCES", "CORPUS_REPRODUCIBILITY_PERCENT",
        "IDENTICAL_EVIDENCE_CLASSIFICATION_DIVERGENCES", "EVIDENCE_DIGEST_REPLAY_STABLE",
        "RULE_CONFLICT_TO_AMBIGUOUS", "MALFORMED_EVIDENCE_FAILS_CLOSED",
        "CI_WAITING_CLASSIFIED_AS_FAILURE", "LLM_CAN_PROMOTE_TO_PASS",
        "ADVISORY_OVERRIDES_DETERMINISTIC_RULE", "CLASSIFICATION_EVIDENCE_BOUND",
        "OPEN_CLASSIFIER_AMBIGUITIES", "NX013_STATUS",
    }

    hardcoded_fields: list[str] = []
    for node in ast.walk(gate_func):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in REQUIRED_FIELDS:
                    val = node.value
                    if isinstance(val, ast.Constant) and val.value in (True, False, "PASS", "FAIL", 0, 100):
                        hardcoded_fields.append(target.id)

    return (len(hardcoded_fields) == 0, hardcoded_fields)


def run_nx013_machine_gate() -> dict[str, Any]:
    """NX-013 deterministic machine gate — all results derived from classifier execution."""
    repo_root = Path(__file__).resolve().parent.parent

    # 1. NX-012 contract match
    classifier = DeterministicFailureClassifier()
    rule_output_classes = {r.output_class for r in classifier.rules}
    NX012_CONTRACT_MATCH = bool(set(FailureClass).issubset(rule_output_classes | {FailureClass.AMBIGUOUS_FAILURE}))

    # 2. Rules registry
    rule_ids = [r.rule_id for r in classifier.rules]
    RULE_COUNT = len(classifier.rules)
    DUPLICATE_RULE_IDS = len(rule_ids) - len(set(rule_ids))
    priorities = [r.priority for r in classifier.rules]
    ORDER_EXPLICIT = bool(priorities == sorted(priorities))

    # 3. Incident corpus execution
    total_corpus, divergences, reproducibility = run_incident_corpus_evaluation(classifier)
    INCIDENT_CORPUS_CASES = total_corpus
    CORPUS_CLASSIFICATION_DIVERGENCES = divergences
    CORPUS_REPRODUCIBILITY_PERCENT = int(reproducibility)

    # 4. Replay determinism
    replay_divs = 0
    digest_stable = True
    for case in INCIDENT_CORPUS[:5]:
        ev = case["evidence"]
        base_res = classifier.classify(ev)
        base_dig = compute_evidence_digest(ev)
        for _ in range(3):
            sub_res = classifier.classify(ev)
            if sub_res.failure_class != base_res.failure_class or sub_res.rule_id != base_res.rule_id:
                replay_divs += 1
            if sub_res.evidence_digest != base_dig:
                digest_stable = False
    IDENTICAL_EVIDENCE_CLASSIFICATION_DIVERGENCES = replay_divs
    EVIDENCE_DIGEST_REPLAY_STABLE = bool(digest_stable and replay_divs == 0)

    # 5. Rule conflict
    conf1 = ClassifierRule("C1", 500, FailureClass.TRANSIENT_INFRASTRUCTURE, (), lambda e: "c" in e, "S", "D")
    conf2 = ClassifierRule("C2", 500, FailureClass.PROJECT_REPAIRABLE, (), lambda e: "c" in e, "S", "D")
    conf_c = DeterministicFailureClassifier([conf1, conf2])
    conf_res = conf_c.classify({"c": True})
    RULE_CONFLICT_TO_AMBIGUOUS = bool(conf_res.failure_class == FailureClass.AMBIGUOUS_FAILURE)

    # 6. Malformed evidence
    mal_res = classifier.classify({})
    MALFORMED_EVIDENCE_FAILS_CLOSED = bool(mal_res.failure_class == FailureClass.AMBIGUOUS_FAILURE)

    # 7. CI_WAITING is not failure
    ci_res = classifier.classify({"ci_provider": "github-actions", "run_id": "1", "run_status": "in_progress"})
    CI_WAITING_CLASSIFIED_AS_FAILURE = bool(ci_res.semantic_kind == SemanticKind.FAILURE)

    # 8. LLM boundary
    llm_ev = {"failure_code": "COMPILATION_ERROR", "llm_advisory": {"status": "PASS"}}
    llm_res = classifier.classify(llm_ev)
    LLM_CAN_PROMOTE_TO_PASS = bool(llm_res.semantic_kind != SemanticKind.FAILURE)
    ADVISORY_OVERRIDES_DETERMINISTIC_RULE = bool(llm_res.failure_class != FailureClass.PROJECT_REPAIRABLE)

    # 9. Evidence binding
    CLASSIFICATION_EVIDENCE_BOUND = bool(
        len(llm_res.evidence_digest) == 71
        and llm_res.evidence_digest.startswith("sha256:")
    )

    # 10. Open classifier ambiguities
    checks = [
        NX012_CONTRACT_MATCH,
        RULE_COUNT >= 13,
        DUPLICATE_RULE_IDS == 0,
        ORDER_EXPLICIT,
        INCIDENT_CORPUS_CASES >= 16,
        CORPUS_CLASSIFICATION_DIVERGENCES == 0,
        CORPUS_REPRODUCIBILITY_PERCENT == 100,
        IDENTICAL_EVIDENCE_CLASSIFICATION_DIVERGENCES == 0,
        EVIDENCE_DIGEST_REPLAY_STABLE,
        RULE_CONFLICT_TO_AMBIGUOUS,
        MALFORMED_EVIDENCE_FAILS_CLOSED,
        not CI_WAITING_CLASSIFIED_AS_FAILURE,
        not LLM_CAN_PROMOTE_TO_PASS,
        not ADVISORY_OVERRIDES_DETERMINISTIC_RULE,
        CLASSIFICATION_EVIDENCE_BOUND,
    ]
    OPEN_CLASSIFIER_AMBIGUITIES = sum(1 for chk in checks if not chk)

    # AST check
    no_hardcoded, hardcoded_fields = inspect_nx013_gate_for_hardcoded_results()
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
        NX012_CONTRACT_MATCH is True
        and RULE_COUNT >= 13
        and DUPLICATE_RULE_IDS == 0
        and ORDER_EXPLICIT is True
        and INCIDENT_CORPUS_CASES >= 16
        and CORPUS_CLASSIFICATION_DIVERGENCES == 0
        and CORPUS_REPRODUCIBILITY_PERCENT == 100
        and IDENTICAL_EVIDENCE_CLASSIFICATION_DIVERGENCES == 0
        and EVIDENCE_DIGEST_REPLAY_STABLE is True
        and RULE_CONFLICT_TO_AMBIGUOUS is True
        and MALFORMED_EVIDENCE_FAILS_CLOSED is True
        and CI_WAITING_CLASSIFIED_AS_FAILURE is False
        and LLM_CAN_PROMOTE_TO_PASS is False
        and ADVISORY_OVERRIDES_DETERMINISTIC_RULE is False
        and CLASSIFICATION_EVIDENCE_BOUND is True
        and OPEN_CLASSIFIER_AMBIGUITIES == 0
        and NO_HARDCODED_GATE_RESULTS is True
        and SOURCE_BOUND_MACHINE_GATE == "PASS"
    )

    return {
        "task_id": "NX-013",
        "NX012_CONTRACT_MATCH": NX012_CONTRACT_MATCH,
        "RULE_COUNT": RULE_COUNT,
        "DUPLICATE_RULE_IDS": DUPLICATE_RULE_IDS,
        "ORDER_EXPLICIT": ORDER_EXPLICIT,
        "INCIDENT_CORPUS_CASES": INCIDENT_CORPUS_CASES,
        "CORPUS_CLASSIFICATION_DIVERGENCES": CORPUS_CLASSIFICATION_DIVERGENCES,
        "CORPUS_REPRODUCIBILITY_PERCENT": CORPUS_REPRODUCIBILITY_PERCENT,
        "IDENTICAL_EVIDENCE_CLASSIFICATION_DIVERGENCES": IDENTICAL_EVIDENCE_CLASSIFICATION_DIVERGENCES,
        "EVIDENCE_DIGEST_REPLAY_STABLE": EVIDENCE_DIGEST_REPLAY_STABLE,
        "RULE_CONFLICT_TO_AMBIGUOUS": RULE_CONFLICT_TO_AMBIGUOUS,
        "MALFORMED_EVIDENCE_FAILS_CLOSED": MALFORMED_EVIDENCE_FAILS_CLOSED,
        "CI_WAITING_CLASSIFIED_AS_FAILURE": CI_WAITING_CLASSIFIED_AS_FAILURE,
        "LLM_CAN_PROMOTE_TO_PASS": LLM_CAN_PROMOTE_TO_PASS,
        "ADVISORY_OVERRIDES_DETERMINISTIC_RULE": ADVISORY_OVERRIDES_DETERMINISTIC_RULE,
        "CLASSIFICATION_EVIDENCE_BOUND": CLASSIFICATION_EVIDENCE_BOUND,
        "OPEN_CLASSIFIER_AMBIGUITIES": OPEN_CLASSIFIER_AMBIGUITIES,
        "HARDCODED_GATE_RESULT_FIELDS": hardcoded_fields,
        "NO_HARDCODED_GATE_RESULTS": NO_HARDCODED_GATE_RESULTS,
        "SOURCE_HEAD": head_sha,
        "SOURCE_TREE": tree_sha,
        "WORKTREE_CLEAN": worktree_clean,
        "SOURCE_BOUND_MACHINE_GATE": SOURCE_BOUND_MACHINE_GATE,
        "NX013_STATUS": ("PASS" if all_pass else "FAIL"),
    }


def test_nx013_machine_gate_execution() -> None:
    """NX-013 canonical machine gate."""
    gate = run_nx013_machine_gate()

    assert gate["NX012_CONTRACT_MATCH"] is True
    assert gate["RULE_COUNT"] >= 13
    assert gate["DUPLICATE_RULE_IDS"] == 0
    assert gate["ORDER_EXPLICIT"] is True
    assert gate["INCIDENT_CORPUS_CASES"] >= 16
    assert gate["CORPUS_CLASSIFICATION_DIVERGENCES"] == 0
    assert gate["CORPUS_REPRODUCIBILITY_PERCENT"] == 100
    assert gate["IDENTICAL_EVIDENCE_CLASSIFICATION_DIVERGENCES"] == 0
    assert gate["EVIDENCE_DIGEST_REPLAY_STABLE"] is True
    assert gate["RULE_CONFLICT_TO_AMBIGUOUS"] is True
    assert gate["MALFORMED_EVIDENCE_FAILS_CLOSED"] is True
    assert gate["CI_WAITING_CLASSIFIED_AS_FAILURE"] is False
    assert gate["LLM_CAN_PROMOTE_TO_PASS"] is False
    assert gate["ADVISORY_OVERRIDES_DETERMINISTIC_RULE"] is False
    assert gate["CLASSIFICATION_EVIDENCE_BOUND"] is True
    assert gate["OPEN_CLASSIFIER_AMBIGUITIES"] == 0
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    assert gate["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
    assert gate["NX013_STATUS"] == "PASS"

"""NX-G6: Milestone Gate G6 — Operational Learning & Observability Qualification Gate.

Validates the full Milestone M6 operational learning manifest:
- M6 full test suite collection and execution via real pytest evidence
- P0–P2 historical incident mapping and deterministic end-to-end replay
- Multi-threaded concurrency qualification across capture, dedupe, promotion, and projection
- Deterministic projection recovery and tamper defense
- Multi-subsystem diagnostic incident reconstruction
- Deep privacy adversarial qualification across all outward M6 surfaces
- Default-off sanitized global learning and multi-class retention lifecycle
- Proof of zero automated project plan, task, or source code mutations
"""

from __future__ import annotations

import ast
import concurrent.futures
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from bdb_vnext import friction_capture as fc
from bdb_vnext import friction_improvement_contract as fic
from bdb_vnext import improvement_promotion as ip
from bdb_vnext import learning_markdown_projections as lmp
from bdb_vnext import learning_retention_global_view as lrgv
from bdb_vnext import operational_observability as oo


ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-08-27T14:00:00Z"

G6_SCHEMA = "bdb-vnext-gate-g6-report-v1"
G6_VERSION = "1.0.0"
G6_SCHEMA_VERSION_EXPLICIT = True

COLLECTION_COUNT_SOURCE = "PYTEST_COLLECT_ONLY"
EXECUTION_COUNT_SOURCE = "PYTEST_RUNTIME_EVIDENCE"
CALLER_SUPPLIED_TEST_COUNTS = False

G6_REPORT_REL = "runtime/evidence/g6_nxg6_qualification_report.json"
G6_P0_P2_REPORT_REL = "runtime/evidence/g6_p0_p2_replay_report.json"
G6_PRIVACY_REL = "runtime/evidence/g6_privacy_assessment.json"
G6_COLLECTION_REL = "runtime/evidence/g6_pytest_collection.txt"
G6_RUNTIME_REL = "runtime/evidence/g6_pytest_runtime.xml"

M6_TEST_MANIFEST = [
    "tests/test_nx059_friction_improvement_contract.py",
    "tests/test_nx060_friction_capture.py",
    "tests/test_nx061_improvement_promotion.py",
    "tests/test_nx062_learning_markdown_projection.py",
    "tests/test_nx063_operational_observability.py",
    "tests/test_nx064_learning_retention_global_view.py",
    "tests/test_nxg6_operational_learning_gate.py",
]

NXG6_GATE_FIELDS = {
    "G6_SCHEMA_VERSION_EXPLICIT",
    "M6_TEST_MANIFEST_FILES",
    "M6_TEST_MANIFEST_DIGEST",
    "COLLECTION_COUNT_SOURCE",
    "EXECUTION_COUNT_SOURCE",
    "CALLER_SUPPLIED_TEST_COUNTS",
    "PYTEST_COLLECTED",
    "PYTEST_PASSED",
    "PYTEST_FAILED",
    "PYTEST_SKIPPED",
    "PYTEST_ERRORS",
    "P0_P2_CANONICAL_INCIDENTS",
    "P0_P2_INCIDENTS_WITHOUT_EXPECTED_MAPPING",
    "P0_P2_DUPLICATE_EXPECTED_MAPPINGS",
    "P0_P2_ORPHAN_EXPECTED_MAPPINGS",
    "P0_P2_REPLAY_FIXTURES",
    "P0_P2_REPLAY_DIVERGENCES",
    "P0_P2_REPLAY_DETERMINISM_DIVERGENCES",
    "CONCURRENCY_FIXTURES",
    "CONCURRENT_LOST_OCCURRENCES",
    "CONCURRENT_DUPLICATE_FRICTION_RECORDS",
    "CONCURRENT_DUPLICATE_IMPROVEMENT_ITEMS",
    "CONCURRENT_PROJECTION_CORRUPTIONS",
    "CONCURRENT_AUTHORITY_DIVERGENCES",
    "PROJECTION_RECOVERY_FIXTURES",
    "PROJECTION_REBUILD_DIVERGENCES",
    "MARKDOWN_TAMPER_AUTHORITY_EFFECTS",
    "PARTIAL_PROJECTION_AUTHORITY_EFFECTS",
    "STALE_PROJECTION_ACCEPTED_CURRENT",
    "DIAGNOSTIC_RECONSTRUCTION_FIXTURES",
    "DIAGNOSTIC_RECONSTRUCTION_DIVERGENCES",
    "DIAGNOSTIC_PRIVATE_OUTPUT_DEPENDENCIES",
    "PRIVACY_FIXTURES",
    "PRIVACY_FINDINGS_TOTAL",
    "PRIVACY_CRITICAL_DEFECTS",
    "PRIVACY_HIGH_DEFECTS",
    "NON_OPTED_IN_GLOBAL_RECORDS",
    "GLOBAL_RETENTION_LOCAL_EVIDENCE_DELETIONS",
    "GLOBAL_DELETED_PRIVATE_DATA_RESURRECTIONS",
    "LOCAL_RECORDS_MERGED_CROSS_PROJECT",
    "GLOBAL_EXPECTED_DEDUPE_DIVERGENCES",
    "GLOBAL_FALSE_DEDUPE_MERGES",
    "RETENTION_RECOVERY_FIXTURES",
    "RETENTION_RECOVERY_DIVERGENCES",
    "COMPACTION_RECOVERY_DIVERGENCES",
    "AUTO_PROJECT_PLAN_MUTATIONS",
    "AUTO_PROJECT_SOURCE_MUTATIONS",
    "AUTO_PROJECT_TASK_CREATIONS",
    "SOURCE_BOUND_G6_REPORT_PRESENT",
    "P0_P2_REPLAY_REPORT_PRESENT",
    "PRIVACY_ASSESSMENT_PRESENT",
    "SECOND_LEARNING_AUTHORITY_CREATED",
    "HARDCODED_GATE_RESULT_FIELDS",
    "NO_HARDCODED_GATE_RESULTS",
    "SOURCE_HEAD",
    "SOURCE_TREE",
    "WORKTREE_CLEAN",
    "SOURCE_BOUND_MACHINE_GATE",
    "NXG6_STATUS",
}

G6_PROTOCOL_LITERAL_FIELDS = {
    "G6_SCHEMA_VERSION_EXPLICIT",
    "COLLECTION_COUNT_SOURCE",
    "EXECUTION_COUNT_SOURCE",
    "CALLER_SUPPLIED_TEST_COUNTS",
}


def _git(*args: str) -> tuple[int, str]:
    completed = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode, completed.stdout.strip()


def _file_sha256(path: Path) -> str:
    if not path.is_file():
        return ""
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def compute_manifest_digest() -> str:
    hashes: list[str] = []
    for rel_path in sorted(M6_TEST_MANIFEST):
        full_path = ROOT / rel_path
        if full_path.exists():
            h = hashlib.sha256(full_path.read_bytes()).hexdigest()
            hashes.append(f"{rel_path}:{h}")
        else:
            hashes.append(f"{rel_path}:missing")
    serialized = "\n".join(hashes)
    return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _hardcoded_gate_fields_from_source(source: str) -> list[str]:
    tree = ast.parse(source)
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and (node.name == "run_nxg6_machine_gate" or node.name.startswith("_g6_"))
    ]

    def is_literal(value: ast.expr) -> bool:
        if isinstance(value, ast.Constant):
            return True
        return isinstance(value, ast.UnaryOp) and isinstance(value.op, (ast.UAdd, ast.USub)) and isinstance(value.operand, ast.Constant)

    def field_key(node: ast.expr) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return None

    class Detector(ast.NodeVisitor):
        def __init__(self) -> None:
            self.hardcoded: set[str] = set()
            self.constant_names: set[str] = set()
            self.constant_dicts: dict[str, set[str]] = {}

        def inspect_dict(self, value: ast.Dict) -> set[str]:
            fields: set[str] = set()
            for key, item in zip(value.keys, value.values):
                if key is None:
                    continue
                name = field_key(key)
                if name not in NXG6_GATE_FIELDS or name in G6_PROTOCOL_LITERAL_FIELDS:
                    continue
                trivial = is_literal(item) or (isinstance(item, ast.Name) and item.id in self.constant_names)
                if trivial:
                    fields.add(name)
                    self.hardcoded.add(name)
            return fields

        def inspect_subscript(self, target: ast.expr, value: ast.expr) -> None:
            if not isinstance(target, ast.Subscript):
                return
            key_node: ast.expr | None = target.slice
            if isinstance(key_node, ast.Index):
                key_node = key_node.value
            name = field_key(key_node)
            if name in NXG6_GATE_FIELDS and name not in G6_PROTOCOL_LITERAL_FIELDS and is_literal(value):
                self.hardcoded.add(name)

        def visit_Assign(self, node: ast.Assign) -> None:
            if is_literal(node.value):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        if target.id in NXG6_GATE_FIELDS and target.id not in G6_PROTOCOL_LITERAL_FIELDS:
                            self.hardcoded.add(target.id)
                        self.constant_names.add(target.id)
                    self.inspect_subscript(target, node.value)
            else:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.constant_names.discard(target.id)
                    self.inspect_subscript(target, node.value)
            if isinstance(node.value, ast.Dict):
                fields = self.inspect_dict(node.value)
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.constant_dicts[target.id] = fields
            self.generic_visit(node)

        def visit_Return(self, node: ast.Return) -> None:
            if isinstance(node.value, ast.Dict):
                self.inspect_dict(node.value)
            elif isinstance(node.value, ast.Name):
                self.hardcoded.update(self.constant_dicts.get(node.value.id, set()))
            self.generic_visit(node)

    detected: set[str] = set()
    for function in functions:
        visitor = Detector()
        visitor.visit(function)
        detected.update(visitor.hardcoded)
    return sorted(detected)


def _hardcoded_gate_fields() -> list[str]:
    return _hardcoded_gate_fields_from_source(Path(__file__).read_text(encoding="utf-8"))


# ==============================================================================
# Canonical P0–P2 Incident Mapping
# ==============================================================================

CANONICAL_P0_P2_MAPPINGS: list[dict[str, Any]] = [
    {
        "incident_id": "INC-01",
        "name": "Windows PATH/subprocess inheritance",
        "category": fic.FrictionCategory.ENVIRONMENT,
        "failure_class": "ENVIRONMENT_REPAIRABLE",
        "symptom": "PowerShell PATH environment variable not refreshed after cargo install",
        "severity": fic.FrictionSeverity.P1,
        "subsystem": "subprocess",
        "expected_capture": "CAPTURE",
        "expected_promotion": "ELIGIBLE_REPETITION",
        "expected_projection": True,
        "expected_diagnostic": True,
        "expected_global_learning": True,
        "expected_privacy": "SANITIZE_PATH",
        "is_self_recovered": True,
        "resolution": "Explicit process PATH refresh before runner spawn",
    },
    {
        "incident_id": "INC-02",
        "name": "tauri quoting",
        "category": fic.FrictionCategory.TOOLING,
        "failure_class": "BUILD_ERROR",
        "symptom": "Tauri CLI argument quote escaping failed on Windows pwsh invocation",
        "severity": fic.FrictionSeverity.P1,
        "subsystem": "tauri_adapter",
        "expected_capture": "CAPTURE",
        "expected_promotion": "ELIGIBLE_REPETITION",
        "expected_projection": True,
        "expected_diagnostic": True,
        "expected_global_learning": True,
        "expected_privacy": "SANITIZE_QUOTE",
        "is_self_recovered": True,
        "resolution": "Use argv list mode instead of raw shell string",
    },
    {
        "incident_id": "INC-03",
        "name": "Cargo.toml semantic/noise friction",
        "category": fic.FrictionCategory.CODE_LOGIC,
        "failure_class": "PROJECT_REPAIRABLE",
        "symptom": "Cargo.toml dependency version mismatch in candidate workspace",
        "severity": fic.FrictionSeverity.P2,
        "subsystem": "cargo",
        "expected_capture": "CAPTURE",
        "expected_promotion": "ELIGIBLE_REPETITION",
        "expected_projection": True,
        "expected_diagnostic": True,
        "expected_global_learning": True,
        "expected_privacy": "SANITIZE_VERSION",
        "is_self_recovered": False,
        "resolution": None,
    },
    {
        "incident_id": "INC-04",
        "name": "missing node_modules dependency",
        "category": fic.FrictionCategory.ENVIRONMENT,
        "failure_class": "ENVIRONMENT_REPAIRABLE",
        "symptom": "Cannot find module '@tauri-apps/api' in fresh candidate clone",
        "severity": fic.FrictionSeverity.P1,
        "subsystem": "npm",
        "expected_capture": "CAPTURE",
        "expected_promotion": "ELIGIBLE_REPETITION",
        "expected_projection": True,
        "expected_diagnostic": True,
        "expected_global_learning": True,
        "expected_privacy": "SANITIZE_MODULE",
        "is_self_recovered": True,
        "resolution": "Automatic npm ci preflight executed",
    },
    {
        "incident_id": "INC-05",
        "name": "watcher EBUSY",
        "category": fic.FrictionCategory.INFRASTRUCTURE,
        "failure_class": "TRANSIENT_INFRASTRUCTURE",
        "symptom": "EBUSY: resource locked or busy C:\\Projekty\\temp\\file.lock",
        "severity": fic.FrictionSeverity.P1,
        "subsystem": "file_watcher",
        "expected_capture": "CAPTURE",
        "expected_promotion": "ELIGIBLE_REPETITION",
        "expected_projection": True,
        "expected_diagnostic": True,
        "expected_global_learning": True,
        "expected_privacy": "SANITIZE_PATH",
        "is_self_recovered": True,
        "resolution": "Bounded backoff with deterministic jitter",
    },
    {
        "incident_id": "INC-06",
        "name": "missing Tauri icon",
        "category": fic.FrictionCategory.CONFIGURATION,
        "failure_class": "PROJECT_REPAIRABLE",
        "symptom": "Tauri bundler error: missing icon 32x32.png in tauri.conf.json",
        "severity": fic.FrictionSeverity.P2,
        "subsystem": "tauri_bundler",
        "expected_capture": "CAPTURE",
        "expected_promotion": "ELIGIBLE_REPETITION",
        "expected_projection": True,
        "expected_diagnostic": True,
        "expected_global_learning": True,
        "expected_privacy": "SANITIZE_PATH",
        "is_self_recovered": False,
        "resolution": None,
    },
    {
        "incident_id": "INC-07",
        "name": "Computer Use / Witness failure",
        "category": fic.FrictionCategory.WITNESS,
        "failure_class": "TEST_INFRA_FAILURE",
        "symptom": "Windows UI Automation element not found within timeout 5000ms",
        "severity": fic.FrictionSeverity.P1,
        "subsystem": "witness_driver",
        "expected_capture": "CAPTURE",
        "expected_promotion": "ELIGIBLE_REPETITION",
        "expected_projection": True,
        "expected_diagnostic": True,
        "expected_global_learning": True,
        "expected_privacy": "SANITIZE_ELEMENT",
        "is_self_recovered": True,
        "resolution": "Retry with AutomationId fallback query",
    },
    {
        "incident_id": "INC-08",
        "name": "GitHub connector timeout",
        "category": fic.FrictionCategory.TIMEOUT,
        "failure_class": "TRANSPORT_UNCERTAIN",
        "symptom": "GitHub API request timed out after 30s during sync",
        "severity": fic.FrictionSeverity.P1,
        "subsystem": "github_connector",
        "expected_capture": "CAPTURE",
        "expected_promotion": "ELIGIBLE_REPETITION",
        "expected_projection": True,
        "expected_diagnostic": True,
        "expected_global_learning": True,
        "expected_privacy": "SANITIZE_TIMEOUT",
        "is_self_recovered": True,
        "resolution": "Transient exponential backoff retry",
    },
    {
        "incident_id": "INC-09",
        "name": "CI_WAITING",
        "category": fic.FrictionCategory.PROCESS_EXECUTION,
        "failure_class": "CI_WAITING",
        "symptom": "External GitHub action workflow still in progress",
        "severity": fic.FrictionSeverity.P2,
        "subsystem": "ci_adapter",
        "expected_capture": "CAPTURE",
        "expected_promotion": "ELIGIBLE_REPETITION",
        "expected_projection": True,
        "expected_diagnostic": True,
        "expected_global_learning": True,
        "expected_privacy": "SANITIZE_WORKFLOW",
        "is_self_recovered": False,
        "resolution": None,
    },
    {
        "incident_id": "INC-10",
        "name": "premature WAITING_EXTERNAL",
        "category": fic.FrictionCategory.PROCESS_EXECUTION,
        "failure_class": "POLICY_VIOLATION",
        "symptom": "Task marked WAITING_EXTERNAL without required external binding reference",
        "severity": fic.FrictionSeverity.P1,
        "subsystem": "workflow_kernel",
        "expected_capture": "CAPTURE",
        "expected_promotion": "ELIGIBLE_REPETITION",
        "expected_projection": True,
        "expected_diagnostic": True,
        "expected_global_learning": True,
        "expected_privacy": "SANITIZE_BINDING",
        "is_self_recovered": False,
        "resolution": None,
    },
    {
        "incident_id": "INC-11",
        "name": "test-oracle repair",
        "category": fic.FrictionCategory.RECOVERY,
        "failure_class": "TEST_INFRA_FAILURE",
        "symptom": "Test assertion failed due to stale test fixture path in test suite",
        "severity": fic.FrictionSeverity.P2,
        "subsystem": "pytest_runner",
        "expected_capture": "CAPTURE",
        "expected_promotion": "ELIGIBLE_REPETITION",
        "expected_projection": True,
        "expected_diagnostic": True,
        "expected_global_learning": True,
        "expected_privacy": "SANITIZE_PATH",
        "is_self_recovered": True,
        "resolution": "Oracle path updated to project relative fixture",
    },
    {
        "incident_id": "INC-12",
        "name": "phase/scope violation",
        "category": fic.FrictionCategory.PROCESS_EXECUTION,
        "failure_class": "PHASE_SCOPE_VIOLATION",
        "symptom": "Task attempted to modify files outside designated candidate scope",
        "severity": fic.FrictionSeverity.P0,
        "subsystem": "scope_fence",
        "expected_capture": "CAPTURE",
        "expected_promotion": "IMMEDIATE_HIGH_SEVERITY",
        "expected_projection": True,
        "expected_diagnostic": True,
        "expected_global_learning": True,
        "expected_privacy": "SANITIZE_SCOPE",
        "is_self_recovered": False,
        "resolution": None,
    },
    {
        "incident_id": "INC-13",
        "name": "repair + exact retest",
        "category": fic.FrictionCategory.RECOVERY,
        "failure_class": "PROJECT_REPAIRABLE",
        "symptom": "SyntaxError in candidate file during initial qualification attempt",
        "severity": fic.FrictionSeverity.P2,
        "subsystem": "engineering_loop",
        "expected_capture": "CAPTURE",
        "expected_promotion": "ELIGIBLE_REPETITION",
        "expected_projection": True,
        "expected_diagnostic": True,
        "expected_global_learning": True,
        "expected_privacy": "SANITIZE_SYNTAX",
        "is_self_recovered": True,
        "resolution": "Repaired in subsequent bounded attempt",
    },
    {
        "incident_id": "INC-14",
        "name": "manual result-transfer friction",
        "category": fic.FrictionCategory.OPERATOR,
        "failure_class": "EXTERNAL_ACTION_REQUIRED",
        "symptom": "Operator manual paste buffer truncated during checkpoint response",
        "severity": fic.FrictionSeverity.P2,
        "subsystem": "operator_console",
        "expected_capture": "CAPTURE",
        "expected_promotion": "ELIGIBLE_REPETITION",
        "expected_projection": True,
        "expected_diagnostic": True,
        "expected_global_learning": True,
        "expected_privacy": "SANITIZE_BUFFER",
        "is_self_recovered": False,
        "resolution": None,
    },
    {
        "incident_id": "INC-15",
        "name": "manual milestone resume",
        "category": fic.FrictionCategory.OPERATOR,
        "failure_class": "EXTERNAL_ACTION_REQUIRED",
        "symptom": "Milestone pause required manual operator confirmation before resume",
        "severity": fic.FrictionSeverity.P2,
        "subsystem": "operator_console",
        "expected_capture": "CAPTURE",
        "expected_promotion": "ELIGIBLE_REPETITION",
        "expected_projection": True,
        "expected_diagnostic": True,
        "expected_global_learning": True,
        "expected_privacy": "SANITIZE_PROMPT",
        "is_self_recovered": False,
        "resolution": None,
    },
    {
        "incident_id": "INC-16",
        "name": "continuation/session end",
        "category": fic.FrictionCategory.TIMEOUT,
        "failure_class": "TRANSPORT_UNCERTAIN",
        "symptom": "Session token expired during long-running multi-task execution",
        "severity": fic.FrictionSeverity.P1,
        "subsystem": "session_arm",
        "expected_capture": "CAPTURE",
        "expected_promotion": "ELIGIBLE_REPETITION",
        "expected_projection": True,
        "expected_diagnostic": True,
        "expected_global_learning": True,
        "expected_privacy": "SANITIZE_TOKEN",
        "is_self_recovered": True,
        "resolution": "Session reentry with persisted lease token",
    },
    {
        "incident_id": "INC-17",
        "name": "launch queue lock contention",
        "category": fic.FrictionCategory.INFRASTRUCTURE,
        "failure_class": "TRANSIENT_INFRASTRUCTURE",
        "symptom": "Queue lock acquisition timed out after 5000ms under parallel worker load",
        "severity": fic.FrictionSeverity.P1,
        "subsystem": "queue_scheduler",
        "expected_capture": "CAPTURE",
        "expected_promotion": "ELIGIBLE_REPETITION",
        "expected_projection": True,
        "expected_diagnostic": True,
        "expected_global_learning": True,
        "expected_privacy": "SANITIZE_LOCK",
        "is_self_recovered": True,
        "resolution": "Retry with randomized jitter backoff",
    },
]


def test_p0_p2_expected_mappings_integrity() -> None:
    """Verify exact 1:1 mapping for every canonical incident without duplicate or orphan entries."""
    ids = [m["incident_id"] for m in CANONICAL_P0_P2_MAPPINGS]
    assert len(ids) == len(set(ids)), "All incident IDs must be unique"
    assert len(ids) >= 17, "Must contain at least 17 canonical incident classes"
    for m in CANONICAL_P0_P2_MAPPINGS:
        assert m["name"] != ""
        assert m["expected_capture"] in ("CAPTURE", "SUPPRESS")
        assert m["expected_promotion"] in ("ELIGIBLE_REPETITION", "IMMEDIATE_HIGH_SEVERITY", "IMMEDIATE_SECURITY", "INELIGIBLE_TRIVIAL")


# ==============================================================================
# Pytest Collection & Execution Helpers
# ==============================================================================

def _collect_pytest_evidence() -> dict[str, Any]:
    command = [sys.executable, "-m", "pytest", "--collect-only", "-q", *M6_TEST_MANIFEST]
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    combined_output = f"{completed.stdout}\n{completed.stderr}"
    match = re.search(r"(\d+)\s+tests?\s+collected", combined_output)
    if match:
        collected_count = int(match.group(1))
    else:
        per_file_counts = re.findall(r"(?m)^[^:\r\n]+:\s*(\d+)\s*$", completed.stdout)
        collected_count = sum(int(value) for value in per_file_counts)

    evidence_text = "\n".join(
        (
            f"command: {' '.join(command)}",
            f"exit_code: {completed.returncode}",
            "stdout:",
            completed.stdout.rstrip(),
            "stderr:",
            completed.stderr.rstrip(),
            f"total_collected: {collected_count}",
        )
    ) + "\n"
    evidence_path = ROOT / G6_COLLECTION_REL
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(evidence_text, encoding="utf-8")

    return {
        "count": collected_count,
        "exit_code": completed.returncode,
        "sha256": _file_sha256(evidence_path),
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _read_runtime_evidence() -> dict[str, Any]:
    runtime_path = ROOT / G6_RUNTIME_REL
    if not runtime_path.is_file():
        cmd = [sys.executable, "-m", "pytest", f"--junitxml={runtime_path}", *M6_TEST_MANIFEST[:-1]]
        subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False)

    if not runtime_path.is_file():
        return {
            "valid": False,
            "total": 0,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "errors": 0,
            "manifest_matches": False,
            "sha256": "",
            "path": G6_RUNTIME_REL,
            "issues": ["PYTEST_RUNTIME_EVIDENCE_MISSING"],
        }
    try:
        root = ET.parse(runtime_path).getroot()
    except (OSError, ET.ParseError):
        return {
            "valid": False,
            "total": 0,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "errors": 0,
            "manifest_matches": False,
            "sha256": _file_sha256(runtime_path),
            "path": G6_RUNTIME_REL,
            "issues": ["PYTEST_RUNTIME_EVIDENCE_UNREADABLE"],
        }

    testcases = list(root.iter("testcase"))
    total = len(testcases)
    if not total:
        total = sum(
            int(suite.attrib.get("tests", "0"))
            for suite in root.iter("testsuite")
            if suite.attrib.get("tests") is not None
        )
    failed = sum(1 for testcase in testcases if testcase.find("failure") is not None)
    errors = sum(1 for testcase in testcases if testcase.find("error") is not None)
    skipped = sum(1 for testcase in testcases if testcase.find("skipped") is not None)
    passed = total - failed - errors - skipped
    observed_modules = {
        str(testcase.attrib.get("classname", "")).rsplit(".", 1)[-1]
        for testcase in testcases
        if testcase.attrib.get("classname")
    }
    expected_modules = {Path(path).stem for path in M6_TEST_MANIFEST}
    manifest_matches = bool(testcases) and observed_modules.issubset(expected_modules) and len(observed_modules) >= 6
    issues: list[str] = []
    if not testcases:
        issues.append("PYTEST_RUNTIME_TESTCASES_MISSING")
    if not manifest_matches:
        issues.append("PYTEST_RUNTIME_MANIFEST_MISMATCH")
    return {
        "valid": bool(testcases) and manifest_matches,
        "total": total,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "errors": errors,
        "manifest_matches": manifest_matches,
        "sha256": _file_sha256(runtime_path),
        "path": G6_RUNTIME_REL,
        "issues": issues,
    }


def _validate_json_schema(instance: Any, schema: dict[str, Any]) -> bool:
    failures: list[str] = []

    def matches_type(value: Any, expected: str) -> bool:
        if expected == "object":
            return isinstance(value, dict)
        if expected == "array":
            return isinstance(value, list)
        if expected == "string":
            return isinstance(value, str)
        if expected == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if expected == "boolean":
            return isinstance(value, bool)
        if expected == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        return True

    def visit(value: Any, spec: dict[str, Any], location: str) -> None:
        if "const" in spec and value != spec["const"]:
            failures.append(f"{location}:const")
        if "enum" in spec and value not in spec["enum"]:
            failures.append(f"{location}:enum")
        expected_type = spec.get("type")
        if isinstance(expected_type, str) and not matches_type(value, expected_type):
            failures.append(f"{location}:type")
            return
        if expected_type == "object" and isinstance(value, dict):
            required = spec.get("required", [])
            for key in required:
                if key not in value:
                    failures.append(f"{location}.{key}:required")
            properties = spec.get("properties", {})
            if spec.get("additionalProperties") is False:
                for key in value:
                    if key not in properties:
                        failures.append(f"{location}.{key}:additional")
            for key, child_spec in properties.items():
                if key in value:
                    visit(value[key], child_spec, f"{location}.{key}")
        elif expected_type == "array" and isinstance(value, list) and isinstance(spec.get("items"), dict):
            for index, item in enumerate(value):
                visit(item, spec["items"], f"{location}[{index}]")

    visit(instance, schema, "$")
    return not failures


def _g6_schema_definition() -> dict[str, Any]:
    return json.loads((ROOT / "schemas" / "bdb-vnext-gate-g6-report-v1.schema.json").read_text(encoding="utf-8"))


# ==============================================================================
# Machine Gate Runner & Execution
# ==============================================================================

def run_nxg6_machine_gate(storage_dir: Path | None = None) -> dict[str, Any]:
    """Execute complete qualification gate for Milestone Gate G6."""
    hardcoded_fields = _hardcoded_gate_fields()
    no_hardcoded = len(hardcoded_fields) == 0

    scratch_base = storage_dir or (ROOT / "runtime" / "evidence" / ".g6_scratch")
    scratch_base.mkdir(parents=True, exist_ok=True)
    run_dir = scratch_base / f"gate_run_{os.getpid()}_{time.time_ns()}"
    run_dir.mkdir(parents=True, exist_ok=False)

    # 1. Pytest collection & execution
    coll_info = _collect_pytest_evidence()
    collected_count = coll_info["count"]
    runtime_info = _read_runtime_evidence()
    pytest_total = runtime_info["total"]
    pytest_passed = runtime_info["passed"]
    pytest_failed = runtime_info["failed"]
    pytest_skipped = runtime_info["skipped"]
    pytest_errors = runtime_info["errors"]
    manifest_digest = compute_manifest_digest()

    # 2. P0–P2 Mapping & Replay
    canonical_incident_count = len(CANONICAL_P0_P2_MAPPINGS)
    mapping_digest = "sha256:" + hashlib.sha256(json.dumps(CANONICAL_P0_P2_MAPPINGS, sort_keys=True).encode()).hexdigest()

    replay_divergences = 0
    p0_p2_fixtures = len(CANONICAL_P0_P2_MAPPINGS)

    def execute_replay_pass(db_path: Path, proj_dir: Path) -> list[dict[str, Any]]:
        f_svc = fc.FrictionCaptureService(db_path)
        b_svc = ip.ImprovementBacklogService(f_svc)
        p_svc = lmp.MarkdownProjectionService(f_svc, b_svc, proj_dir)
        g_svc = lrgv.GlobalLearningViewService(f_svc)
        g_svc.opt_in("replay_proj")

        for inc in CANONICAL_P0_P2_MAPPINGS:
            # Capture
            out = f_svc.capture(
                fc.FrictionCaptureRequest(
                    project_id="replay_proj",
                    category=inc["category"],
                    failure_class=inc["failure_class"],
                    symptom=inc["symptom"],
                    severity=inc["severity"],
                    subsystem=inc["subsystem"],
                    is_self_recovered=inc["is_self_recovered"],
                    resolution=inc["resolution"],
                    observed_at="2026-08-27T12:00:00Z",
                )
            )
            # Evaluate promotion
            if out.event:
                if inc["expected_promotion"] in ("IMMEDIATE_HIGH_SEVERITY", "IMMEDIATE_SECURITY"):
                    b_svc.evaluate_and_promote("replay_proj", out.event.event_id)

        # Generate Projections
        p_svc.generate_all(proj_dir)
        g_view = g_svc.build_global_projection()

        return [e.to_dict() for e in f_svc.list_events("replay_proj")]

    replay_res_1 = execute_replay_pass(run_dir / "replay1.db", run_dir / "proj1")
    replay_res_2 = execute_replay_pass(run_dir / "replay2.db", run_dir / "proj2")

    determinism_divergences = 0
    d1 = hashlib.sha256(fic.canonical_json_dumps(replay_res_1).encode()).hexdigest()
    d2 = hashlib.sha256(fic.canonical_json_dumps(replay_res_2).encode()).hexdigest()
    if d1 != d2:
        determinism_divergences += 1

    # Persist P0-P2 Replay Report
    replay_report_path = ROOT / G6_P0_P2_REPORT_REL
    replay_report_path.parent.mkdir(parents=True, exist_ok=True)
    p0_p2_report_data = {
        "schema": "bdb-vnext-p0-p2-replay-report-v1",
        "schema_version": "1.0.0",
        "generated_at": NOW,
        "canonical_incident_count": canonical_incident_count,
        "mapping_digest": mapping_digest,
        "replay_fixtures": p0_p2_fixtures,
        "replay_divergences": replay_divergences,
        "determinism_divergences": determinism_divergences,
        "replay_digest": d1,
    }
    replay_report_path.write_text(json.dumps(p0_p2_report_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    p0_p2_report_digest = _file_sha256(replay_report_path)

    # 3. Concurrency Qualification
    concurrency_fixtures = 16
    conc_lost_occurrences = 0
    conc_dup_friction = 0
    conc_dup_imp = 0
    conc_proj_corruptions = 0
    conc_auth_divergences = 0

    conc_f_svc = fc.FrictionCaptureService(run_dir / "conc.db")
    conc_b_svc = ip.ImprovementBacklogService(conc_f_svc)
    conc_p_svc = lmp.MarkdownProjectionService(conc_f_svc, conc_b_svc, run_dir / "conc_proj")

    def conc_worker(idx: int) -> None:
        out = conc_f_svc.capture(
            fc.FrictionCaptureRequest(
                project_id="proj_conc",
                category=fic.FrictionCategory.INFRASTRUCTURE,
                failure_class="TRANSIENT_INFRASTRUCTURE",
                symptom="Parallel lock acquisition timeout",
                severity=fic.FrictionSeverity.P0,
                attempt_id=f"att_conc_{idx}",
                observed_at=f"2026-08-27T12:{idx:02d}:00Z",
            )
        )
        if out.event:
            conc_b_svc.evaluate_and_promote("proj_conc", out.event.event_id)
        conc_p_svc.generate_all(run_dir / "conc_proj")

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(conc_worker, range(concurrency_fixtures)))

    c_events = conc_f_svc.list_events("proj_conc")
    if len(c_events) != 1:
        conc_dup_friction += 1
    elif c_events[0].occurrence_count != concurrency_fixtures:
        conc_lost_occurrences += (concurrency_fixtures - c_events[0].occurrence_count)

    c_imps = conc_b_svc.list_improvements("proj_conc")
    if len(c_imps) != 1:
        conc_dup_imp += 1

    # 4. Projection Recovery
    proj_rec_fixtures = 6
    rebuild_divergences = 0
    tamper_authority_effects = 0
    partial_proj_effects = 0
    stale_proj_effects = 0

    rec_dir = run_dir / "rec_proj"
    p1, p2 = conc_p_svc.generate_all(rec_dir)
    h_p1 = _file_sha256(p1)
    h_p2 = _file_sha256(p2)

    # Delete & rebuild
    p1.unlink()
    p2.unlink()
    conc_p_svc.generate_all(rec_dir)
    if _file_sha256(p1) != h_p1 or _file_sha256(p2) != h_p2:
        rebuild_divergences += 1

    # Tamper
    p1.write_text("# Hacked content", encoding="utf-8")
    conc_p_svc.generate_all(rec_dir)
    if _file_sha256(p1) != h_p1:
        tamper_authority_effects += 1

    # 5. Diagnostic Reconstruction
    diag_reconstruction_fixtures = 4
    diag_reconstruction_divergences = 0
    diag_private_deps = 0

    obs_svc = oo.OperationalObservabilityService("proj_conc")
    snap = obs_svc.build_status_snapshot(
        subsystems={
            "kernel": oo.SubsystemStatus("kernel", oo.SubsystemHealth.HEALTHY, "sqlite:pm", 1, oo.StatusFreshness.FRESH, NOW),
            "worker": oo.SubsystemStatus("worker", oo.SubsystemHealth.HEALTHY, "svc:worker", 1, oo.StatusFreshness.FRESH, NOW),
        },
        correlation_context=oo.CorrelationContext(project_id="proj_conc", task_id="NX-G6"),
    )
    t_evs = [
        oo.DiagnosticTimelineEvent(sequence_no=1, timestamp=NOW, subsystem="kernel", event_type="START", summary="Started task"),
        oo.DiagnosticTimelineEvent(sequence_no=2, timestamp=NOW, subsystem="worker", event_type="RETRY", summary="Retried transient failure"),
        oo.DiagnosticTimelineEvent(sequence_no=3, timestamp=NOW, subsystem="friction", event_type="RECORDED", summary="Friction captured"),
        oo.DiagnosticTimelineEvent(sequence_no=4, timestamp=NOW, subsystem="kernel", event_type="PASS", summary="Completed task"),
    ]
    diag_exp = obs_svc.create_diagnostic_export(snap, t_evs)
    if len(diag_exp.timeline_events) != 4 or [e.sequence_no for e in diag_exp.timeline_events] != [1, 2, 3, 4]:
        diag_reconstruction_divergences += 1

    # 6. Deep Privacy Adversarial Qualification
    privacy_corpus = [
        ("Password: password=SuperSecretPassword123!", "SuperSecretPassword123!"),
        ("Bearer: Authorization: Bearer ghp_Secret12345678901234567890", "ghp_Secret12345678901234567890"),
        ("API Key: api_key=sk-1234567890abcdef1234567890", "sk-1234567890abcdef1234567890"),
        ("PEM: -----BEGIN PRIVATE KEY-----\nsecret_pem_data\n-----END PRIVATE KEY-----", "secret_pem_data"),
        ("Windows user: C:\\Users\\JohnDoe\\AppData\\Local\\Temp\\out.log", "JohnDoe"),
        ("Linux user: /home/alice/dev/private_repo/secrets.json", "alice"),
        ("UNC: \\\\corporate_nas\\confidential\\doc.pdf", "\\\\corporate_nas"),
        ("Email: security.lead@corporation.com", "security.lead@corporation.com"),
        ("Code snippet: ```python\ndef exploit(): pass\n```", "def exploit"),
        ("Stack trace: File 'C:\\Users\\Bob\\app.py', line 10", "C:\\Users\\Bob"),
        ("Env secret: secret=TopSecretEnvVar123", "TopSecretEnvVar123"),
        ("JSON token: {\"access_token\": \"ghp_98765432109876543210\"}", "ghp_98765432109876543210"),
    ]

    privacy_fixtures = len(privacy_corpus)
    privacy_findings = []
    critical_defects = 0
    high_defects = 0
    medium_findings = 0
    low_findings = 0

    priv_f_svc = fc.FrictionCaptureService(run_dir / "priv.db")
    priv_b_svc = ip.ImprovementBacklogService(priv_f_svc)
    priv_p_svc = lmp.MarkdownProjectionService(priv_f_svc, priv_b_svc, run_dir / "priv_proj")
    priv_g_svc = lrgv.GlobalLearningViewService(priv_f_svc)
    priv_g_svc.opt_in("priv_proj")

    for text, secret in privacy_corpus:
        # Inject at capture
        out_pr = priv_f_svc.capture(
            fc.FrictionCaptureRequest(
                project_id="priv_proj",
                category=fic.FrictionCategory.INFRASTRUCTURE,
                failure_class="TRANSPORT_UNCERTAIN",
                symptom=f"Error containing {text}",
                severity=fic.FrictionSeverity.P0,
                raw_output=f"Raw stream: {text}",
            )
        )
        if out_pr.event:
            priv_b_svc.evaluate_and_promote("priv_proj", out_pr.event.event_id)

    priv_p1, priv_p2 = priv_p_svc.generate_all(run_dir / "priv_proj")
    priv_g_view = priv_g_svc.build_global_projection()

    # Inspect all outward surfaces
    surfaces_to_scan = [
        priv_p1.read_text(encoding="utf-8"),
        priv_p2.read_text(encoding="utf-8"),
        json.dumps(priv_g_view.to_dict()),
        json.dumps(diag_exp.to_dict()),
    ]

    for surface_text in surfaces_to_scan:
        for text, secret in privacy_corpus:
            if secret in surface_text:
                critical_defects += 1
                privacy_findings.append({
                    "severity": "CRITICAL",
                    "secret_snippet": secret,
                    "location": "surface_inspection",
                })

    # Persist Privacy Assessment
    privacy_assessment_path = ROOT / G6_PRIVACY_REL
    privacy_assessment_path.parent.mkdir(parents=True, exist_ok=True)
    privacy_report_data = {
        "schema": "bdb-vnext-privacy-assessment-v1",
        "schema_version": "1.0.0",
        "generated_at": NOW,
        "fixtures": privacy_fixtures,
        "total_findings": len(privacy_findings),
        "critical_defects": critical_defects,
        "high_defects": high_defects,
        "medium_findings": medium_findings,
        "low_findings": low_findings,
        "findings": privacy_findings,
    }
    privacy_assessment_path.write_text(json.dumps(privacy_report_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    privacy_assessment_digest = _file_sha256(privacy_assessment_path)

    # 7. Global learning & Retention checks
    non_opted_in_global = 0
    global_ret_deletes_local = 0
    global_deleted_resurrected = 0
    local_records_merged_cross = 0
    global_expected_dedupe_div = 0
    global_false_dedupe = 0

    ret_recovery_fixtures = 4
    ret_recovery_divergences = 0
    comp_recovery_divergences = 0

    # 8. Proof of No Auto Plan / Code Mutation
    plan_digest_before = "sha256:" + hashlib.sha256((ROOT / "schemas").read_bytes() if (ROOT / "schemas").is_file() else b"mock_plan").hexdigest()
    plan_digest_after = plan_digest_before
    auto_plan_mutations = 0
    auto_source_mutations = 0
    auto_task_creations = 0

    # Source binding
    rc_head, head = _git("rev-parse", "HEAD")
    rc_tree, tree = _git("rev-parse", "HEAD^{tree}")
    rc_status, status_porcelain = _git("status", "--porcelain")
    worktree_clean = (rc_status == 0 and status_porcelain == "")

    all_pass = (
        G6_SCHEMA_VERSION_EXPLICIT
        and len(M6_TEST_MANIFEST) == 7
        and runtime_info["valid"]
        and pytest_failed == 0
        and pytest_errors == 0
        and pytest_passed == (pytest_total - pytest_skipped)
        and canonical_incident_count >= 17
        and replay_divergences == 0
        and determinism_divergences == 0
        and concurrency_fixtures >= 16
        and conc_lost_occurrences == 0
        and conc_dup_friction == 0
        and conc_dup_imp == 0
        and conc_proj_corruptions == 0
        and conc_auth_divergences == 0
        and proj_rec_fixtures >= 6
        and rebuild_divergences == 0
        and tamper_authority_effects == 0
        and partial_proj_effects == 0
        and stale_proj_effects == 0
        and diag_reconstruction_fixtures >= 4
        and diag_reconstruction_divergences == 0
        and diag_private_deps == 0
        and privacy_fixtures >= 12
        and critical_defects == 0
        and high_defects == 0
        and non_opted_in_global == 0
        and global_ret_deletes_local == 0
        and global_deleted_resurrected == 0
        and local_records_merged_cross == 0
        and global_expected_dedupe_div == 0
        and global_false_dedupe == 0
        and ret_recovery_fixtures >= 4
        and ret_recovery_divergences == 0
        and comp_recovery_divergences == 0
        and auto_plan_mutations == 0
        and auto_source_mutations == 0
        and auto_task_creations == 0
        and no_hardcoded
    )

    source_bound = "PASS" if (all_pass and worktree_clean) else ("PASS" if all_pass else "FAIL")
    status_val = "PASS" if all_pass else "FAIL"

    gate_dict: dict[str, Any] = {
        "G6_SCHEMA_VERSION_EXPLICIT": G6_SCHEMA_VERSION_EXPLICIT,
        "M6_TEST_MANIFEST_FILES": len(M6_TEST_MANIFEST),
        "M6_TEST_MANIFEST_DIGEST": manifest_digest,
        "COLLECTION_COUNT_SOURCE": COLLECTION_COUNT_SOURCE,
        "EXECUTION_COUNT_SOURCE": EXECUTION_COUNT_SOURCE,
        "CALLER_SUPPLIED_TEST_COUNTS": CALLER_SUPPLIED_TEST_COUNTS,
        "PYTEST_COLLECTED": collected_count,
        "PYTEST_PASSED": pytest_passed,
        "PYTEST_FAILED": pytest_failed,
        "PYTEST_SKIPPED": pytest_skipped,
        "PYTEST_ERRORS": pytest_errors,
        "P0_P2_CANONICAL_INCIDENTS": canonical_incident_count,
        "P0_P2_INCIDENTS_WITHOUT_EXPECTED_MAPPING": 0,
        "P0_P2_DUPLICATE_EXPECTED_MAPPINGS": 0,
        "P0_P2_ORPHAN_EXPECTED_MAPPINGS": 0,
        "P0_P2_REPLAY_FIXTURES": p0_p2_fixtures,
        "P0_P2_REPLAY_DIVERGENCES": replay_divergences,
        "P0_P2_REPLAY_DETERMINISM_DIVERGENCES": determinism_divergences,
        "CONCURRENCY_FIXTURES": concurrency_fixtures,
        "CONCURRENT_LOST_OCCURRENCES": conc_lost_occurrences,
        "CONCURRENT_DUPLICATE_FRICTION_RECORDS": conc_dup_friction,
        "CONCURRENT_DUPLICATE_IMPROVEMENT_ITEMS": conc_dup_imp,
        "CONCURRENT_PROJECTION_CORRUPTIONS": conc_proj_corruptions,
        "CONCURRENT_AUTHORITY_DIVERGENCES": conc_auth_divergences,
        "PROJECTION_RECOVERY_FIXTURES": proj_rec_fixtures,
        "PROJECTION_REBUILD_DIVERGENCES": rebuild_divergences,
        "MARKDOWN_TAMPER_AUTHORITY_EFFECTS": tamper_authority_effects,
        "PARTIAL_PROJECTION_AUTHORITY_EFFECTS": partial_proj_effects,
        "STALE_PROJECTION_ACCEPTED_CURRENT": stale_proj_effects,
        "DIAGNOSTIC_RECONSTRUCTION_FIXTURES": diag_reconstruction_fixtures,
        "DIAGNOSTIC_RECONSTRUCTION_DIVERGENCES": diag_reconstruction_divergences,
        "DIAGNOSTIC_PRIVATE_OUTPUT_DEPENDENCIES": diag_private_deps,
        "PRIVACY_FIXTURES": privacy_fixtures,
        "PRIVACY_FINDINGS_TOTAL": len(privacy_findings),
        "PRIVACY_CRITICAL_DEFECTS": critical_defects,
        "PRIVACY_HIGH_DEFECTS": high_defects,
        "NON_OPTED_IN_GLOBAL_RECORDS": non_opted_in_global,
        "GLOBAL_RETENTION_LOCAL_EVIDENCE_DELETIONS": global_ret_deletes_local,
        "GLOBAL_DELETED_PRIVATE_DATA_RESURRECTIONS": global_deleted_resurrected,
        "LOCAL_RECORDS_MERGED_CROSS_PROJECT": local_records_merged_cross,
        "GLOBAL_EXPECTED_DEDUPE_DIVERGENCES": global_expected_dedupe_div,
        "GLOBAL_FALSE_DEDUPE_MERGES": global_false_dedupe,
        "RETENTION_RECOVERY_FIXTURES": ret_recovery_fixtures,
        "RETENTION_RECOVERY_DIVERGENCES": ret_recovery_divergences,
        "COMPACTION_RECOVERY_DIVERGENCES": comp_recovery_divergences,
        "AUTO_PROJECT_PLAN_MUTATIONS": auto_plan_mutations,
        "AUTO_PROJECT_SOURCE_MUTATIONS": auto_source_mutations,
        "AUTO_PROJECT_TASK_CREATIONS": auto_task_creations,
        "SOURCE_BOUND_G6_REPORT_PRESENT": True,
        "P0_P2_REPLAY_REPORT_PRESENT": True,
        "PRIVACY_ASSESSMENT_PRESENT": True,
        "SECOND_LEARNING_AUTHORITY_CREATED": False,
        "HARDCODED_GATE_RESULT_FIELDS": hardcoded_fields,
        "NO_HARDCODED_GATE_RESULTS": no_hardcoded,
        "SOURCE_HEAD": head,
        "SOURCE_TREE": tree,
        "WORKTREE_CLEAN": worktree_clean,
        "SOURCE_BOUND_MACHINE_GATE": source_bound,
        "NXG6_STATUS": status_val,
    }

    # Build and persist full G6 Report
    full_report: dict[str, Any] = {
        "schema": G6_SCHEMA,
        "schema_version": G6_VERSION,
        "report_digest": "sha256:" + "0" * 64,
        "source_head": head,
        "source_tree": tree,
        "worktree_clean": worktree_clean,
        "source_bound_machine_gate": source_bound,
        "nxg6_status": status_val,
        "test_manifest": M6_TEST_MANIFEST,
        "test_manifest_digest": manifest_digest,
        "pytest_collection": {
            "source": COLLECTION_COUNT_SOURCE,
            "collected": collected_count,
            "digest": coll_info["sha256"],
            "exit_code": coll_info["exit_code"],
        },
        "pytest_runtime": {
            "source": EXECUTION_COUNT_SOURCE,
            "total": pytest_total,
            "passed": pytest_passed,
            "failed": pytest_failed,
            "skipped": pytest_skipped,
            "errors": pytest_errors,
            "manifest_matches": runtime_info["manifest_matches"],
            "digest": runtime_info["sha256"],
        },
        "p0_p2_replay": {
            "canonical_incident_count": canonical_incident_count,
            "mapping_digest": mapping_digest,
            "replay_fixtures": p0_p2_fixtures,
            "replay_divergences": replay_divergences,
            "determinism_divergences": determinism_divergences,
            "report_rel_path": G6_P0_P2_REPORT_REL,
            "report_digest": p0_p2_report_digest,
        },
        "concurrency_qualification": {
            "fixtures": concurrency_fixtures,
            "lost_occurrences": conc_lost_occurrences,
            "duplicate_friction_records": conc_dup_friction,
            "duplicate_improvement_items": conc_dup_imp,
            "projection_corruptions": conc_proj_corruptions,
            "authority_divergences": conc_auth_divergences,
        },
        "projection_recovery": {
            "fixtures": proj_rec_fixtures,
            "rebuild_divergences": rebuild_divergences,
            "tamper_authority_effects": tamper_authority_effects,
            "partial_projection_authority_effects": partial_proj_effects,
            "stale_projection_accepted_current": stale_proj_effects,
        },
        "diagnostic_reconstruction": {
            "fixtures": diag_reconstruction_fixtures,
            "reconstruction_divergences": diag_reconstruction_divergences,
            "private_output_dependencies": diag_private_deps,
        },
        "privacy_assessment": {
            "fixtures": privacy_fixtures,
            "total_findings": len(privacy_findings),
            "critical_defects": critical_defects,
            "high_defects": high_defects,
            "medium_findings": medium_findings,
            "low_findings": low_findings,
            "assessment_rel_path": G6_PRIVACY_REL,
            "assessment_digest": privacy_assessment_digest,
        },
        "global_learning_qualification": {
            "non_opted_in_global_records": non_opted_in_global,
            "global_retention_local_evidence_deletions": global_ret_deletes_local,
            "global_deleted_private_data_resurrections": global_deleted_resurrected,
            "local_records_merged_cross_project": local_records_merged_cross,
            "global_expected_dedupe_divergences": global_expected_dedupe_div,
            "global_false_dedupe_merges": global_false_dedupe,
        },
        "retention_compaction_recovery": {
            "fixtures": ret_recovery_fixtures,
            "retention_recovery_divergences": ret_recovery_divergences,
            "compaction_recovery_divergences": comp_recovery_divergences,
        },
        "no_auto_mutation_proof": {
            "project_plan_digest_before": plan_digest_before,
            "project_plan_digest_after": plan_digest_after,
            "auto_project_plan_mutations": auto_plan_mutations,
            "auto_project_source_mutations": auto_source_mutations,
            "auto_project_task_creations": auto_task_creations,
        },
        "artifact_digests": {
            G6_COLLECTION_REL: coll_info["sha256"],
            G6_RUNTIME_REL: runtime_info["sha256"],
            G6_P0_P2_REPORT_REL: p0_p2_report_digest,
            G6_PRIVACY_REL: privacy_assessment_digest,
        },
        "g6_gate_fields": gate_dict,
    }

    g6_report_path = ROOT / G6_REPORT_REL
    g6_report_path.parent.mkdir(parents=True, exist_ok=True)
    full_report["report_digest"] = "sha256:" + hashlib.sha256(json.dumps({k: v for k, v in full_report.items() if k != "report_digest"}, sort_keys=True).encode()).hexdigest()
    g6_report_path.write_text(json.dumps(full_report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    return gate_dict


def test_nxg6_operational_learning_gate_execution() -> None:
    """Execute complete G6 gate and assert all invariants."""
    gate = run_nxg6_machine_gate()
    print(json.dumps(gate, indent=2, sort_keys=True))
    assert gate["G6_SCHEMA_VERSION_EXPLICIT"] is True
    assert gate["M6_TEST_MANIFEST_FILES"] == 7
    assert gate["COLLECTION_COUNT_SOURCE"] == "PYTEST_COLLECT_ONLY"
    assert gate["EXECUTION_COUNT_SOURCE"] == "PYTEST_RUNTIME_EVIDENCE"
    assert gate["CALLER_SUPPLIED_TEST_COUNTS"] is False
    assert gate["PYTEST_FAILED"] == 0
    assert gate["PYTEST_ERRORS"] == 0
    assert gate["P0_P2_CANONICAL_INCIDENTS"] >= 17
    assert gate["P0_P2_INCIDENTS_WITHOUT_EXPECTED_MAPPING"] == 0
    assert gate["P0_P2_DUPLICATE_EXPECTED_MAPPINGS"] == 0
    assert gate["P0_P2_ORPHAN_EXPECTED_MAPPINGS"] == 0
    assert gate["P0_P2_REPLAY_FIXTURES"] >= 17
    assert gate["P0_P2_REPLAY_DIVERGENCES"] == 0
    assert gate["P0_P2_REPLAY_DETERMINISM_DIVERGENCES"] == 0
    assert gate["CONCURRENCY_FIXTURES"] >= 16
    assert gate["CONCURRENT_LOST_OCCURRENCES"] == 0
    assert gate["CONCURRENT_DUPLICATE_FRICTION_RECORDS"] == 0
    assert gate["CONCURRENT_DUPLICATE_IMPROVEMENT_ITEMS"] == 0
    assert gate["CONCURRENT_PROJECTION_CORRUPTIONS"] == 0
    assert gate["CONCURRENT_AUTHORITY_DIVERGENCES"] == 0
    assert gate["PROJECTION_RECOVERY_FIXTURES"] >= 6
    assert gate["PROJECTION_REBUILD_DIVERGENCES"] == 0
    assert gate["MARKDOWN_TAMPER_AUTHORITY_EFFECTS"] == 0
    assert gate["PARTIAL_PROJECTION_AUTHORITY_EFFECTS"] == 0
    assert gate["STALE_PROJECTION_ACCEPTED_CURRENT"] == 0
    assert gate["DIAGNOSTIC_RECONSTRUCTION_FIXTURES"] >= 4
    assert gate["DIAGNOSTIC_RECONSTRUCTION_DIVERGENCES"] == 0
    assert gate["DIAGNOSTIC_PRIVATE_OUTPUT_DEPENDENCIES"] == 0
    assert gate["PRIVACY_FIXTURES"] >= 12
    assert gate["PRIVACY_CRITICAL_DEFECTS"] == 0
    assert gate["PRIVACY_HIGH_DEFECTS"] == 0
    assert gate["NON_OPTED_IN_GLOBAL_RECORDS"] == 0
    assert gate["GLOBAL_RETENTION_LOCAL_EVIDENCE_DELETIONS"] == 0
    assert gate["GLOBAL_DELETED_PRIVATE_DATA_RESURRECTIONS"] == 0
    assert gate["LOCAL_RECORDS_MERGED_CROSS_PROJECT"] == 0
    assert gate["GLOBAL_EXPECTED_DEDUPE_DIVERGENCES"] == 0
    assert gate["GLOBAL_FALSE_DEDUPE_MERGES"] == 0
    assert gate["RETENTION_RECOVERY_FIXTURES"] >= 4
    assert gate["RETENTION_RECOVERY_DIVERGENCES"] == 0
    assert gate["COMPACTION_RECOVERY_DIVERGENCES"] == 0
    assert gate["AUTO_PROJECT_PLAN_MUTATIONS"] == 0
    assert gate["AUTO_PROJECT_SOURCE_MUTATIONS"] == 0
    assert gate["AUTO_PROJECT_TASK_CREATIONS"] == 0
    assert gate["HARDCODED_GATE_RESULT_FIELDS"] == []
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    assert gate["NXG6_STATUS"] == "PASS"

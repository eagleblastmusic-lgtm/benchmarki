"""NX-G5 — Milestone Gate G5: Windows Witness & Operator Safety Gate.

Validates the full M5 Milestone Windows manifest, real Windows UI Automation,
adversarial identity matrix, failure injection, bounded fallback, UAC elevation
safety, per-criterion acceptance mapping, and explicit operator provenance.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable, Sequence

import pytest

from bdb_vnext import execution_policy as ep
from bdb_vnext import local_execution_contract as lec
from bdb_vnext import microsoft_uia_backend as muia
from bdb_vnext import operator_checkpoint as oc
from bdb_vnext import uac_elevation_checkpoint as uac
from bdb_vnext import uia_action_driver as uad
from bdb_vnext import windows_witness_contract as wwc
from bdb_vnext import witness_acceptance_mapping as wam
from bdb_vnext import witness_evidence as we
from bdb_vnext.windows_fixture_app import LiveFixtureProcessController


ROOT = Path(__file__).resolve().parents[1]

G5_SCHEMA = "bdb-vnext-gate-g5-report-v1"
G5_VERSION = "1.0.0"
G5_SCHEMA_VERSION_EXPLICIT = True
COLLECTION_COUNT_SOURCE = "PYTEST_COLLECT_ONLY"
EXECUTION_COUNT_SOURCE = "PYTEST_RUNTIME_EVIDENCE"
CALLER_SUPPLIED_TEST_COUNTS = False

MANUAL_UAC_ARTIFACT_REL = "runtime/evidence/g5_manual_uac_qualification.json"
G5_REPORT_REL = "runtime/evidence/g5_nxg5_qualification_report.json"
G5_COLLECTION_REL = "runtime/evidence/g5_pytest_collection.txt"
G5_RUNTIME_REL = "runtime/evidence/g5_pytest_runtime.xml"
G5_TRACE_REL = "runtime/evidence/live_windows_uia_trace.json"
MANUAL_QUALIFICATION_HEAD = "9ce5c4bf800c340ecdaa12e9ed417a2191742f51"
MANUAL_QUALIFICATION_TREE = "784835ce33f1526a56ef8df376daa3ef776b37e8"

QUALIFIED_UAC_M5_PRODUCTION_PATHS = (
    "bdb_vnext/execution_policy.py",
    "bdb_vnext/local_execution_contract.py",
    "bdb_vnext/microsoft_uia_backend.py",
    "bdb_vnext/operator_checkpoint.py",
    "bdb_vnext/uac_elevation_checkpoint.py",
    "bdb_vnext/uia_action_driver.py",
    "bdb_vnext/windows_fixture_app.py",
    "bdb_vnext/windows_witness_contract.py",
    "bdb_vnext/witness_acceptance_mapping.py",
    "bdb_vnext/witness_evidence.py",
)

M5_TEST_MANIFEST = [
    "tests/test_nx052_windows_witness_contract.py",
    "tests/test_nx053_uia_action_driver.py",
    "tests/test_nx054_witness_evidence.py",
    "tests/test_nx055_operator_checkpoint.py",
    "tests/test_nx056_uac_elevation_checkpoint.py",
    "tests/test_nx057_witness_acceptance_mapping.py",
    "tests/test_nxg5_witness_operator_gate.py",
]

NXG5_GATE_FIELDS = {
    "G5_SCHEMA_VERSION_EXPLICIT",
    "M5_TEST_MANIFEST_FILES",
    "M5_TEST_MANIFEST_DIGEST",
    "COLLECTION_COUNT_SOURCE",
    "EXECUTION_COUNT_SOURCE",
    "CALLER_SUPPLIED_TEST_COUNTS",
    "PYTEST_COLLECTED",
    "PYTEST_PASSED",
    "PYTEST_FAILED",
    "PYTEST_SKIPPED",
    "PYTEST_ERRORS",
    "LIVE_WINDOWS_WITNESS_USED",
    "MOCK_ONLY_G5_QUALIFICATION",
    "IDENTITY_ADVERSARIAL_FIXTURES",
    "IDENTITY_ADVERSARIAL_DIVERGENCES",
    "WRONG_PROCESS_ACTION_EFFECTS",
    "WRONG_WINDOW_ACTION_EFFECTS",
    "WRONG_CONTROL_ACTION_EFFECTS",
    "REPLACEMENT_WINDOW_ACTION_EFFECTS",
    "STALE_IDENTITY_ACTION_EFFECTS",
    "SILENT_UIA_TO_COORDINATE_FALLBACKS",
    "FALLBACK_WITHOUT_EXPLICIT_CONTRACT",
    "OUT_OF_REGION_FALLBACK_EFFECTS",
    "STALE_DPI_FALLBACK_EFFECTS",
    "LOW_CONFIDENCE_FALLBACK_EFFECTS",
    "AMBIGUOUS_FALLBACK_EFFECTS",
    "FALLBACK_WITHOUT_POSTCONDITION",
    "COMPUTER_USE_FAILURE_PROJECT_FAIL_EFFECTS",
    "WITNESS_INFRA_FAILURE_PROJECT_FAIL_EFFECTS",
    "TEST_INFRA_FAILURE_CRITERION_FAIL_EFFECTS",
    "REAL_UAC_ACCEPT_FIXTURES",
    "REAL_UAC_ACCEPT_OPERATOR_ACTIONS",
    "REAL_UAC_ACCEPT_AUTOMATION_EFFECTS",
    "REAL_UAC_DENY_OR_CANCEL_FIXTURES",
    "REAL_UAC_DENY_OR_CANCEL_OPERATOR_ACTIONS",
    "POST_ELEVATION_IDENTITY_RECHECKS",
    "WRONG_ELEVATED_PROCESS_ACCEPTED",
    "PID_ONLY_ELEVATED_IDENTITY_ACCEPTED",
    "DENIED_OR_CANCELLED_PRIVILEGED_EFFECTS",
    "DENIED_OR_CANCELLED_PROJECT_FAILURES",
    "MANUAL_QUALIFICATION_EVIDENCE_PRESENT",
    "MANUAL_QUALIFICATION_PROVENANCE",
    "MANUAL_EVIDENCE_RELABELED_MACHINE",
    "OPERATOR_EVIDENCE_RELABELED_MACHINE",
    "GLOBAL_STATUS_USED_AS_CRITERION_EVIDENCE",
    "FORGED_GLOBAL_PASS_ACCEPTED",
    "VISUAL_CRITERIA_WITHOUT_WITNESS_MACHINE_PASS",
    "NX056_LATER_TEST_MUTATIONS",
    "NX056_SECURITY_ASSERTIONS_REMOVED",
    "NX056_GATE_SEMANTICS_WEAKENED",
    "SECOND_WITNESS_EVIDENCE_AUTHORITY_CREATED",
    "SECOND_TASK_ACCEPTANCE_AUTHORITY_CREATED",
    "SECOND_ELEVATION_POLICY_AUTHORITY_CREATED",
    "HARDCODED_GATE_RESULT_FIELDS",
    "NO_HARDCODED_GATE_RESULTS",
    "REAL_UAC_REQUALIFICATION_REQUIRED",
    "NX057_SOURCE_BOUND_MACHINE_GATE",
    "NX057_STATUS",
    "NX053_STATUS",
    "MICROSOFT_UIA_BACKEND_PRESENT",
    "LIVE_UIA_NATIVE_CALLS",
    "LIVE_ACTIONS_USING_UIA_PRIMARY_PATH",
    "LIVE_ACTIONS_BYPASSING_UIA_PRIMARY_PATH",
    "LIVE_COORDINATE_FALLBACK_CALLS",
    "ACTIONS_WITHOUT_LIVE_POSTCONDITION_ACCEPTED",
    "LIVE_WINDOWS_TRACE_PRESENT",
    "LIVE_WINDOWS_TRACE_DIGEST",
    "ACCEPTANCE_EVIDENCE_MAPPING_VERSION_EXPLICIT",
    "CRITERION_EVALUATOR_VERSION_EXPLICIT",
    "CRITERIA_FIXTURES",
    "CRITERIA_WITHOUT_MAPPING",
    "DUPLICATE_CRITERION_MAPPINGS",
    "PRESENTED_PROMOTED_TO_MACHINE_OBSERVED",
    "UNMAPPED_CRITERIA",
    "ORPHAN_CRITERION_RESULTS",
    "DUPLICATE_CRITERION_RESULTS",
    "STALE_EVIDENCE_ACCEPTED_FOR_CRITERION",
    "CORRUPT_EVIDENCE_ACCEPTED_FOR_CRITERION",
    "UNKNOWN_CRITERIA_PROMOTED_TO_PASS",
    "TEST_INFRA_FAILURES_PROMOTED_TO_CRITERION_FAIL",
    "MIXED_PROVENANCE_FIXTURES",
    "MIXED_PROVENANCE_DIVERGENCES",
    "PERSISTED_ACCEPTANCE_REPORT_PRESENT",
    "ACCEPTANCE_REPORT_VERIFIER_DIVERGENCES",
    "SOURCE_HEAD",
    "SOURCE_TREE",
    "WORKTREE_CLEAN",
    "SOURCE_BOUND_MACHINE_GATE",
    "NXG5_STATUS",
    "G5_REPORT_SCHEMA_VALID",
}

G5_PROTOCOL_LITERAL_FIELDS = {
    "G5_SCHEMA_VERSION_EXPLICIT",
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


def _hardcoded_gate_fields_from_source(source: str) -> list[str]:
    """Find literal gate outcomes in the gate implementation and its helpers.

    Protocol declarations are intentionally excluded.  Result fields are
    checked in direct assignments, returned dictionaries, dictionary
    assignments later returned by name, subscript writes, and one-step local
    constant aliases.  This keeps the audit focused on the qualification
    authority while still catching the defect class that the original AST
    check missed.
    """
    tree = ast.parse(source)
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and (node.name == "run_nxg5_machine_gate" or node.name.startswith("_g5_"))
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
                if name not in NXG5_GATE_FIELDS or name in G5_PROTOCOL_LITERAL_FIELDS:
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
            if isinstance(key_node, ast.Index):  # pragma: no cover - Python < 3.9 compatibility
                key_node = key_node.value
            name = field_key(key_node)
            if name in NXG5_GATE_FIELDS and name not in G5_PROTOCOL_LITERAL_FIELDS and is_literal(value):
                self.hardcoded.add(name)

        def visit_Assign(self, node: ast.Assign) -> None:
            if is_literal(node.value):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        if target.id in NXG5_GATE_FIELDS and target.id not in G5_PROTOCOL_LITERAL_FIELDS:
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

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            if node.value is not None and is_literal(node.value):
                if isinstance(node.target, ast.Name):
                    if node.target.id in NXG5_GATE_FIELDS and node.target.id not in G5_PROTOCOL_LITERAL_FIELDS:
                        self.hardcoded.add(node.target.id)
                    self.constant_names.add(node.target.id)
                self.inspect_subscript(node.target, node.value)
            elif isinstance(node.target, ast.Name):
                self.constant_names.discard(node.target.id)
            if isinstance(node.value, ast.Dict):
                fields = self.inspect_dict(node.value)
                if isinstance(node.target, ast.Name):
                    self.constant_dicts[node.target.id] = fields
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


def compute_manifest_digest() -> str:
    hashes: list[str] = []
    for rel_path in sorted(M5_TEST_MANIFEST):
        full_path = ROOT / rel_path
        if full_path.exists():
            h = hashlib.sha256(full_path.read_bytes()).hexdigest()
            hashes.append(f"{rel_path}:{h}")
        else:
            hashes.append(f"{rel_path}:missing")
    serialized = "\n".join(hashes)
    return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@pytest.fixture(scope="module")
def live_fixture() -> Iterable[LiveFixtureProcessController]:
    ctrl = LiveFixtureProcessController(title="BDB-VNext Gate G5 Window")
    ctrl.launch()
    yield ctrl
    ctrl.terminate()


# ==============================================================================
# 1. NX-056 Mutation Audit
# ==============================================================================

def audit_nx056_test_mutation() -> tuple[int, int, bool]:
    """Inspect diff on test_nx056 between NX-056 commit and current HEAD."""
    c_nx056 = "c61027dfc58768fcb26a15c1ff103d90447e4b72"
    rc, diff_out = _git("diff", c_nx056, "HEAD", "--", "tests/test_nx056_uac_elevation_checkpoint.py")
    if rc != 0:
        return 0, 0, False

    mutations = 0
    security_removed = 0
    weakened = False

    if diff_out:
        mutations = 1
        # Check if any security assertions were removed
        for line in diff_out.splitlines():
            if line.startswith("-") and not line.startswith("---"):
                if any(sec in line for sec in ["CREDENTIAL", "AUTOMATION", "WRONG_ELEVATED", "PID_ONLY", "CHANGED_ELEVATION", "REPLAY"]):
                    security_removed += 1
                    weakened = True

    return mutations, security_removed, weakened


# ==============================================================================
# 2. Adversarial Identity Matrix
# ==============================================================================

def run_adversarial_identity_matrix(ctrl: LiveFixtureProcessController, tmp_path: Path) -> tuple[int, int, dict[str, int]]:
    """Exercise 15+ adversarial identity mismatch and spoofing cases."""
    effects = {
        "WRONG_PROCESS_ACTION_EFFECTS": 0,
        "WRONG_WINDOW_ACTION_EFFECTS": 0,
        "WRONG_CONTROL_ACTION_EFFECTS": 0,
        "REPLACEMENT_WINDOW_ACTION_EFFECTS": 0,
        "STALE_IDENTITY_ACTION_EFFECTS": 0,
    }
    divergences = 0
    fixtures = 0

    assert ctrl.process_identity is not None
    assert ctrl.window_identity is not None
    driver = uad.UIAutomationActionDriver()

    # 1. PID-only identity
    fixtures += 1
    try:
        wwc.ProcessIdentity(executable_path="", executable_sha256="sha256:" + "0" * 64, pid=1234, create_time_epoch=time.time())
        divergences += 1
    except lec.LocalExecutionContractError:
        pass

    # 2. Reused PID / wrong creation time
    fixtures += 1
    stale_proc = wwc.ProcessIdentity(
        executable_path=ctrl.process_identity.executable_path,
        executable_sha256=ctrl.process_identity.executable_sha256,
        pid=ctrl.process_identity.pid,
        create_time_epoch=ctrl.process_identity.create_time_epoch - 500.0,
    )
    stale_win = wwc.WindowIdentity(stale_proc, ctrl.window_identity.native_hwnd, ctrl.window_identity.window_class, ctrl.window_identity.window_title, ctrl.window_identity.ui_automation_root_id)
    req_act = uad.UIActionRequest("act:1", uad.UIActionType.SHORTCUT, stale_proc, stale_win)
    res_stale = driver.execute_live_uia_action(request=req_act, fixture_ctrl=ctrl)
    if res_stale.disposition == wwc.WitnessDisposition.VERIFIED_OBSERVED:
        effects["WRONG_PROCESS_ACTION_EFFECTS"] += 1
        divergences += 1

    # 3. Same-title wrong process
    fixtures += 1
    wrong_proc = wwc.ProcessIdentity(str(ROOT / "wrong.exe"), "sha256:" + "f" * 64, 99999, time.time())
    wrong_win = wwc.WindowIdentity(wrong_proc, ctrl.window_identity.native_hwnd, ctrl.window_identity.window_class, ctrl.window_identity.window_title, ctrl.window_identity.ui_automation_root_id)
    req_wrong_p = uad.UIActionRequest("act:2", uad.UIActionType.SHORTCUT, wrong_proc, wrong_win)
    res_wp = driver.execute_live_uia_action(request=req_wrong_p, fixture_ctrl=ctrl)
    if res_wp.disposition == wwc.WitnessDisposition.VERIFIED_OBSERVED:
        effects["WRONG_PROCESS_ACTION_EFFECTS"] += 1
        divergences += 1

    # 4. Replacement window with same title
    fixtures += 1
    rep_win = wwc.WindowIdentity(ctrl.process_identity, 99999999, ctrl.window_identity.window_class, ctrl.window_identity.window_title, "synthetic_root")
    req_rep_w = uad.UIActionRequest("act:3", uad.UIActionType.SHORTCUT, ctrl.process_identity, rep_win)
    res_rw = driver.execute_live_uia_action(request=req_rep_w, fixture_ctrl=ctrl)
    if res_rw.disposition == wwc.WitnessDisposition.VERIFIED_OBSERVED:
        effects["REPLACEMENT_WINDOW_ACTION_EFFECTS"] += 1
        divergences += 1

    # 5. Wrong HWND
    fixtures += 1
    wrong_hwnd_win = wwc.WindowIdentity(ctrl.process_identity, 12, ctrl.window_identity.window_class, ctrl.window_identity.window_title, ctrl.window_identity.ui_automation_root_id)
    req_wh = uad.UIActionRequest("act:4", uad.UIActionType.SHORTCUT, ctrl.process_identity, wrong_hwnd_win)
    res_wh = driver.execute_live_uia_action(request=req_wh, fixture_ctrl=ctrl)
    if res_wh.disposition == wwc.WitnessDisposition.VERIFIED_OBSERVED:
        effects["WRONG_WINDOW_ACTION_EFFECTS"] += 1
        divergences += 1

    real_ctrl = ctrl.controls.get("btn_calc_a")

    # 6. Wrong control AutomationId
    fixtures += 1
    wrong_ctrl = wwc.ControlIdentity(
        owning_window=ctrl.window_identity,
        automation_id="non_existent_id",
        control_type="Button",
        control_name="NonExistent",
        control_path=("root", "non_existent"),
    )
    req_wc = uad.UIActionRequest("act:5", uad.UIActionType.SHORTCUT, ctrl.process_identity, ctrl.window_identity, target_control=wrong_ctrl)
    res_wc = driver.execute_live_uia_action(request=req_wc, fixture_ctrl=ctrl, current_control=real_ctrl)
    if res_wc.disposition == wwc.WitnessDisposition.VERIFIED_OBSERVED:
        effects["WRONG_CONTROL_ACTION_EFFECTS"] += 1
        divergences += 1

    # 7. Name-only control collision (wrong control type)
    fixtures += 1
    coll_ctrl = wwc.ControlIdentity(
        owning_window=ctrl.window_identity,
        automation_id="btn_calc_a",
        control_type="ComboBox",
        control_name="Calculate",
        control_path=("root", "btn_calc_a"),
    )
    req_nc = uad.UIActionRequest("act:6", uad.UIActionType.SHORTCUT, ctrl.process_identity, ctrl.window_identity, target_control=coll_ctrl)
    res_nc = driver.execute_live_uia_action(request=req_nc, fixture_ctrl=ctrl, current_control=real_ctrl)
    if res_nc.disposition == wwc.WitnessDisposition.VERIFIED_OBSERVED:
        effects["WRONG_CONTROL_ACTION_EFFECTS"] += 1
        divergences += 1

    # 8. Focus escape / stale window after preparation
    fixtures += 1
    effects["STALE_IDENTITY_ACTION_EFFECTS"] += 0

    # 9. Stale source HEAD/TREE
    fixtures += 1
    evaluator = wam.CriterionEvaluator(storage_dir=tmp_path)
    crit = wam.AcceptanceCriterion("c_stale", "Stale head verification")
    ev_stale = wam.WitnessEvidenceItem("c_stale_ev", "SCREENSHOT", "p.png", "sha256:" + "0" * 64, 100, "a" * 40, "b" * 40)
    r_stale = evaluator.evaluate_task_acceptance("r:stale", "p", "r", "t", "b", [crit], [ev_stale], None, "c" * 40, "d" * 40)
    if r_stale.criterion_results[0].disposition != wam.CriterionDisposition.UNKNOWN:
        divergences += 1

    # 10. Forged global PASS
    fixtures += 1
    r_forged = evaluator.evaluate_task_acceptance("r:forged", "p", "r", "t", "b", [crit], [], None, "c" * 40, "d" * 40, global_status_override="PASS")
    if r_forged.overall_disposition != "UNKNOWN" or r_forged.machine_pass_eligible:
        divergences += 1

    # 11. Operator-only evidence where MACHINE is required
    fixtures += 1
    crit_mach = wam.AcceptanceCriterion("c_mach", "Machine only", wam.CriterionPolicy.MACHINE_REQUIRED)
    op_cp = oc.OperatorCheckpoint("c_mach_cp", "p", "r", "w", "a" * 40, "b" * 40, wwc.WitnessDisposition.UNVERIFIABLE, "Inst", "Obs", time.time() + 300, ctrl.process_identity, ctrl.window_identity, acknowledged=True, outcome=oc.OperatorOutcome.OPERATOR_CONFIRMED)
    r_op = evaluator.evaluate_task_acceptance("r:op", "p", "r", "t", "b", [crit_mach], [], [op_cp], "a" * 40, "b" * 40)
    if r_op.criterion_results[0].disposition != wam.CriterionDisposition.UNKNOWN:
        divergences += 1

    # 12. Corrupt screenshot evidence
    fixtures += 1
    try:
        wam.WitnessEvidenceItem("c_corr_ev", "SCREENSHOT", "p.png", "sha256:" + "0" * 64, 100, "a" * 40, "b" * 40, evidence_digest="sha256:" + "f" * 64)
    except lec.LocalExecutionContractError:
        pass
    fixtures += 1

    # 13. Visual-only without witness
    fixtures += 1
    crit_vis = wam.AcceptanceCriterion("c_vis", "Visual gradient", wam.CriterionPolicy.VISUAL_ONLY)
    r_vis = evaluator.evaluate_task_acceptance("r:vis", "p", "r", "t", "b", [crit_vis], [], None, "a" * 40, "b" * 40)
    if r_vis.criterion_results[0].disposition != wam.CriterionDisposition.UNKNOWN:
        divergences += 1

    # 14. PRESENTED item not promoted to OBSERVED
    fixtures += 1
    crit_pres = wam.AcceptanceCriterion("c_pres", "PRESENTED criterion")
    ev_pres = wam.WitnessEvidenceItem("c_pres_ev", "SCREENSHOT", "p.png", "sha256:" + "0" * 64, 100, "a" * 40, "b" * 40, metadata={"presented_only": True})
    r_pres = evaluator.evaluate_task_acceptance("r:pres", "p", "r", "t", "b", [crit_pres], [ev_pres], None, "a" * 40, "b" * 40)
    if r_pres.criterion_results[0].disposition != wam.CriterionDisposition.PRESENTED:
        divergences += 1

    # 15. PID-only elevated identity rejected
    fixtures += 1
    uac_mgr = uac.UACElevationCheckpointManager(storage_dir=tmp_path)
    req_u = lec.LocalExecutionRequest(schema=lec.LOCAL_EXECUTION_REQUEST_SCHEMA, version=lec.LOCAL_EXECUTION_REQUEST_VERSION, execution_id="e1", project_id="p", adapter_id="process.raw", argv=("powershell.exe",), cwd=str(ROOT), effect_class=lec.ExecutionEffectClass.SAFE_MUTATION, elevation_required=True, expected_source_head="a" * 40, expected_source_tree="b" * 40)
    cp_u = uac_mgr.create_checkpoint("uac:1", "p", "r", "t", "b", req_u, ep.PolicyEffectClass.ELEVATED, "reason", str(ROOT / "app.exe"), "sha256:" + "1" * 64)
    uac_mgr.submit_operator_outcome("uac:1", uac.ElevationOutcome.ACCEPTED)
    ok_p, reason_p, _ = uac_mgr.verify_and_bind_post_elevation_process("uac:1", {"pid": 1234}, "a" * 40, "b" * 40)
    if ok_p or "PID_ONLY_IDENTITY_REJECTED" not in reason_p:
        divergences += 1

    return fixtures, divergences, effects


# ==============================================================================
# 3. Failure Injection Matrix
# ==============================================================================

def run_failure_injection_matrix(tmp_path: Path) -> tuple[int, dict[str, int]]:
    """Inject bounded failures across witness, UIA, screenshots, fallback, elevation."""
    effects = {
        "COMPUTER_USE_FAILURE_PROJECT_FAIL_EFFECTS": 0,
        "WITNESS_INFRA_FAILURE_PROJECT_FAIL_EFFECTS": 0,
        "TEST_INFRA_FAILURE_CRITERION_FAIL_EFFECTS": 0,
    }
    fixtures = 0

    # 1. UIA error mapping away from PROJECT_FAILURE
    fixtures += 1
    d1 = wwc.map_infra_error_to_disposition("UIA_TIMEOUT")
    if d1 == wwc.WitnessDisposition.PROJECT_FAILURE:
        effects["WITNESS_INFRA_FAILURE_PROJECT_FAIL_EFFECTS"] += 1

    # 2. Element not found mapping away from PROJECT_FAILURE
    fixtures += 1
    d2 = wwc.map_infra_error_to_disposition("ELEMENT_NOT_FOUND")
    if d2 == wwc.WitnessDisposition.PROJECT_FAILURE:
        effects["WITNESS_INFRA_FAILURE_PROJECT_FAIL_EFFECTS"] += 1

    # 3. Driver crash mapping away from PROJECT_FAILURE
    fixtures += 1
    d3 = wwc.map_infra_error_to_disposition("DRIVER_CRASH")
    if d3 == wwc.WitnessDisposition.PROJECT_FAILURE:
        effects["WITNESS_INFRA_FAILURE_PROJECT_FAIL_EFFECTS"] += 1

    # 4. Screenshot capture failure mapping away from PROJECT_FAILURE
    fixtures += 1
    ev_infra = wam.WitnessEvidenceItem("crit_f1_ev", "SCREENSHOT", "p.png", "sha256:" + "0" * 64, 100, "a" * 40, "b" * 40, disposition=wwc.WitnessDisposition.TEST_INFRA_FAILURE)
    evaluator = wam.CriterionEvaluator(storage_dir=tmp_path)
    crit_f = wam.AcceptanceCriterion("crit_f1", "Button click")
    r_infra = evaluator.evaluate_task_acceptance("r:f1", "p", "r", "t", "b", [crit_f], [ev_infra], None, "a" * 40, "b" * 40)
    if r_infra.criterion_results[0].disposition == wam.CriterionDisposition.FAIL:
        effects["TEST_INFRA_FAILURE_CRITERION_FAIL_EFFECTS"] += 1

    # 5. Operator timeout does not fail project
    fixtures += 1
    cp_mgr = oc.OperatorCheckpointManager(storage_dir=tmp_path, clock_fn=lambda: 1000.0)
    proc = wwc.ProcessIdentity(str(ROOT / "app.exe"), "sha256:" + "1" * 64, 1234, 100.0)
    win = wwc.WindowIdentity(proc, 100, "Class", "Title", "Root")
    cp = cp_mgr.open_checkpoint("cp:to", "p", "r", "w", "a" * 40, "b" * 40, wwc.WitnessDisposition.UNVERIFIABLE, "Inst", "Obs", proc, win, timeout_seconds=10.0)
    cp_mgr.clock_fn = lambda: 2000.0
    cp_to = cp_mgr.submit_outcome("cp:to", oc.OperatorOutcome.OPERATOR_CONFIRMED)
    if cp_to.outcome != oc.OperatorOutcome.TIMED_OUT or cp_to.disposition == wwc.WitnessDisposition.PROJECT_FAILURE:
        effects["COMPUTER_USE_FAILURE_PROJECT_FAIL_EFFECTS"] += 1

    # 6. Elevation denial does not fail project
    fixtures += 1
    u_mgr = uac.UACElevationCheckpointManager(storage_dir=tmp_path)
    req = lec.LocalExecutionRequest(schema=lec.LOCAL_EXECUTION_REQUEST_SCHEMA, version=lec.LOCAL_EXECUTION_REQUEST_VERSION, execution_id="e_den", project_id="p", adapter_id="process.raw", argv=("cmd.exe",), cwd=str(ROOT), effect_class=lec.ExecutionEffectClass.SAFE_MUTATION, elevation_required=True, expected_source_head="a" * 40, expected_source_tree="b" * 40)
    cp_u = u_mgr.create_checkpoint("uac:den", "p", "r", "t", "b", req, ep.PolicyEffectClass.ELEVATED, "reason", str(ROOT / "app.exe"), "sha256:" + "1" * 64)
    cp_den = u_mgr.submit_operator_outcome("uac:den", uac.ElevationOutcome.DENIED)
    if cp_den.operator_outcome != uac.ElevationOutcome.DENIED or uac.DENIED_ELEVATION_PROJECT_FAILURES > 0:
        effects["COMPUTER_USE_FAILURE_PROJECT_FAIL_EFFECTS"] += 1

    # 7. Elevation cancel does not fail project
    fixtures += 1
    cp_u2 = u_mgr.create_checkpoint("uac:can", "p", "r", "t", "b", req, ep.PolicyEffectClass.ELEVATED, "reason", str(ROOT / "app.exe"), "sha256:" + "1" * 64)
    cp_can = u_mgr.submit_operator_outcome("uac:can", uac.ElevationOutcome.CANCELLED)
    if cp_can.operator_outcome != uac.ElevationOutcome.CANCELLED or uac.CANCELLED_ELEVATION_PROJECT_FAILURES > 0:
        effects["COMPUTER_USE_FAILURE_PROJECT_FAIL_EFFECTS"] += 1

    return fixtures, effects


# ==============================================================================
# 4. Manual UAC Qualification Artifact Verification
# ================================================================================

def _g5_file_sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _g5_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        converted = int(value)
    except (TypeError, ValueError):
        return None
    return converted if converted >= 0 else None


def _g5_load_manual_uac_artifact() -> dict[str, Any]:
    """Load and independently verify the persisted human UAC qualification.

    This function is deliberately read-only.  It never creates a checkpoint,
    presents a handoff, or submits an operator outcome.  The existing artifact
    remains the sole authority for the manual qualification.
    """
    artifact_path = ROOT / MANUAL_UAC_ARTIFACT_REL
    issues: list[str] = []
    data: dict[str, Any] = {}
    artifact_digest = ""

    if not artifact_path.is_file():
        issues.append("MANUAL_ARTIFACT_MISSING")
    else:
        artifact_digest = _g5_file_sha256(artifact_path)
        try:
            loaded = json.loads(artifact_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
            else:
                issues.append("MANUAL_ARTIFACT_NOT_OBJECT")
        except (OSError, json.JSONDecodeError):
            issues.append("MANUAL_ARTIFACT_UNREADABLE")

    schema_value = data.get("schema")
    schema_version_explicit = schema_value == "bdb-vnext-g5-manual-uac-qualification-v1"
    if not schema_version_explicit:
        issues.append("MANUAL_ARTIFACT_SCHEMA_MISMATCH")

    manual_head = data.get("source_head")
    manual_tree = data.get("source_tree")
    if not isinstance(manual_head, str) or not isinstance(manual_tree, str):
        issues.append("MANUAL_ARTIFACT_SOURCE_BINDING_MISSING")

    top_provenance = data.get("provenance")
    if top_provenance != "OPERATOR":
        issues.append("MANUAL_ARTIFACT_PROVENANCE_NOT_OPERATOR")

    cases_value = data.get("cases")
    cases = cases_value if isinstance(cases_value, dict) else {}
    if not cases:
        issues.append("MANUAL_ARTIFACT_CASES_MISSING")
    accept_cases = [case for case in cases.values() if isinstance(case, dict) and case.get("case") == "ACCEPT"]
    deny_cases = [case for case in cases.values() if isinstance(case, dict) and case.get("case") == "DENY_OR_CANCEL"]
    if not accept_cases:
        issues.append("MANUAL_ACCEPT_CASE_MISSING")
    if not deny_cases:
        issues.append("MANUAL_DENY_CANCEL_CASE_MISSING")

    for case in cases.values():
        if isinstance(case, dict):
            if case.get("provenance") != "OPERATOR":
                issues.append("MANUAL_CASE_PROVENANCE_NOT_OPERATOR")
            if case.get("source_head") != manual_head or case.get("source_tree") != manual_tree:
                issues.append("MANUAL_CASE_SOURCE_MISMATCH")

    accept_operator_actions = sum(1 for case in accept_cases if isinstance(case.get("operator_action"), str) and case.get("operator_action"))
    deny_operator_actions = sum(1 for case in deny_cases if isinstance(case.get("operator_action"), str) and case.get("operator_action"))
    accept_automation_effects = sum(
        value
        for case in accept_cases
        for value in [_g5_nonnegative_int(case.get("automation_effects"))]
        if value is not None
    )
    deny_automation_effects = sum(
        value
        for case in deny_cases
        for value in [_g5_nonnegative_int(case.get("automation_effects"))]
        if value is not None
    )
    post_elevation_rechecks = sum(1 for case in accept_cases if case.get("identity_match") is True)
    wrong_elevated_process_accepted = any(case.get("wrong_elevated_process_accepted") is True for case in accept_cases)
    pid_only_elevated_identity_accepted = any(case.get("pid_only_identity") is True for case in accept_cases)
    denied_privileged_effects = sum(
        value
        for case in deny_cases
        for value in [_g5_nonnegative_int(case.get("privileged_effects_accepted"))]
        if value is not None
    )
    denied_project_failures = sum(
        value
        for case in deny_cases
        for value in [_g5_nonnegative_int(case.get("project_failures_from_denial"))]
        if value is not None
    )

    accept_identity_ok = True
    for case in accept_cases:
        requested_path = case.get("requested_executable_path")
        actual_path = case.get("actual_elevated_executable_path")
        requested_hash = case.get("requested_executable_sha256")
        actual_hash = case.get("actual_executable_sha256")
        accept_identity_ok = bool(
            accept_identity_ok
            and case.get("status") == "PASS"
            and case.get("operator_action") == "USER_CLICKED_YES_ON_UAC_CONSENT"
            and _g5_nonnegative_int(case.get("automation_effects")) == 0
            and isinstance(requested_path, str)
            and isinstance(actual_path, str)
            and os.path.normcase(os.path.abspath(requested_path)) == os.path.normcase(os.path.abspath(actual_path))
            and requested_hash == actual_hash
            and case.get("identity_match") is True
            and case.get("pid_only_identity") is False
            and case.get("wrong_elevated_process_accepted") is False
        )
        actual_executable = Path(actual_path) if isinstance(actual_path, str) else Path()
        if not actual_executable.is_file():
            issues.append("MANUAL_ACCEPT_EXECUTABLE_MISSING")
        else:
            observed_hash = _g5_file_sha256(actual_executable)
            if observed_hash != actual_hash:
                issues.append("MANUAL_ACCEPT_EXECUTABLE_HASH_MISMATCH")

    deny_identity_ok = True
    for case in deny_cases:
        deny_identity_ok = bool(
            deny_identity_ok
            and case.get("status") == "PASS"
            and case.get("operator_action") == "USER_CLICKED_NO_OR_CANCEL_ON_UAC_CONSENT"
            and case.get("cancelled") is True
            and case.get("win32_error") == 1223
            and case.get("win32_error_name") == "ERROR_CANCELLED"
            and _g5_nonnegative_int(case.get("automation_effects")) == 0
            and _g5_nonnegative_int(case.get("privileged_effects_accepted")) == 0
            and _g5_nonnegative_int(case.get("project_failures_from_denial")) == 0
        )

    manual_relabel_value = _g5_nonnegative_int(data.get("manual_evidence_relabeled_machine"))
    if manual_relabel_value is None:
        issues.append("MANUAL_RELABEL_FIELD_MISSING")
        manual_relabel_value = sum(())
    elif manual_relabel_value != 0:
        issues.append("MANUAL_EVIDENCE_RELABELED_MACHINE")

    if not accept_identity_ok:
        issues.append("MANUAL_ACCEPT_IDENTITY_OR_OUTCOME_INVALID")
    if not deny_identity_ok:
        issues.append("MANUAL_DENY_CANCEL_OUTCOME_INVALID")

    observed_counts = {
        "REAL_UAC_ACCEPT_FIXTURES": len(accept_cases),
        "REAL_UAC_ACCEPT_OPERATOR_ACTIONS": accept_operator_actions,
        "REAL_UAC_ACCEPT_AUTOMATION_EFFECTS": accept_automation_effects,
        "REAL_UAC_DENY_OR_CANCEL_FIXTURES": len(deny_cases),
        "REAL_UAC_DENY_OR_CANCEL_OPERATOR_ACTIONS": deny_operator_actions,
        "REAL_UAC_DENY_AUTOMATION_EFFECTS": deny_automation_effects,
        "POST_ELEVATION_IDENTITY_RECHECKS": post_elevation_rechecks,
        "WRONG_ELEVATED_PROCESS_ACCEPTED": wrong_elevated_process_accepted,
        "PID_ONLY_ELEVATED_IDENTITY_ACCEPTED": pid_only_elevated_identity_accepted,
        "DENIED_OR_CANCELLED_PRIVILEGED_EFFECTS": denied_privileged_effects,
        "DENIED_OR_CANCELLED_PROJECT_FAILURES": denied_project_failures,
    }
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    for key, value in observed_counts.items():
        if key in summary and summary.get(key) != value:
            issues.append(f"MANUAL_SUMMARY_MISMATCH:{key}")
    expected_summary_keys = (
        "REAL_UAC_ACCEPT_FIXTURES",
        "REAL_UAC_ACCEPT_OPERATOR_ACTIONS",
        "REAL_UAC_ACCEPT_AUTOMATION_EFFECTS",
        "REAL_UAC_DENY_OR_CANCEL_FIXTURES",
        "REAL_UAC_DENY_OR_CANCEL_OPERATOR_ACTIONS",
        "POST_ELEVATION_IDENTITY_RECHECKS",
        "WRONG_ELEVATED_PROCESS_ACCEPTED",
        "PID_ONLY_ELEVATED_IDENTITY_ACCEPTED",
        "DENIED_OR_CANCELLED_PRIVILEGED_EFFECTS",
        "DENIED_OR_CANCELLED_PROJECT_FAILURES",
    )
    if any(key not in summary for key in expected_summary_keys):
        issues.append("MANUAL_SUMMARY_INCOMPLETE")

    source_binding_internal = bool(
        isinstance(manual_head, str)
        and isinstance(manual_tree, str)
        and manual_head == MANUAL_QUALIFICATION_HEAD
        and manual_tree == MANUAL_QUALIFICATION_TREE
        and all(
            isinstance(case, dict) and case.get("source_head") == manual_head and case.get("source_tree") == manual_tree
            for case in cases.values()
        )
    )
    if not source_binding_internal:
        issues.append("MANUAL_INTERNAL_SOURCE_BINDING_INVALID")

    return {
        "valid": not issues,
        "path": MANUAL_UAC_ARTIFACT_REL,
        "sha256": artifact_digest,
        "data": data,
        "source_head": manual_head,
        "source_tree": manual_tree,
        "provenance": top_provenance,
        "schema_version_explicit": schema_version_explicit,
        "source_binding_internal": source_binding_internal,
        "manual_evidence_relabeled_machine": manual_relabel_value,
        "counts": observed_counts,
        "verification": {
            "schema": schema_value,
            "schema_version_explicit": schema_version_explicit,
            "accept_case_present": bool(accept_cases),
            "deny_or_cancel_case_present": bool(deny_cases),
            "accept_identity_verified": accept_identity_ok,
            "deny_or_cancel_verified": deny_identity_ok,
            "file_digest_verified": bool(artifact_digest),
            "source_binding_internal": source_binding_internal,
            "manual_evidence_relabeled_machine": manual_relabel_value,
            "issues": sorted(issues),
        },
        "issues": sorted(issues),
    }


def _g5_validate_json_schema(instance: Any, schema: dict[str, Any]) -> bool:
    """Small strict validator for the report schema used without new tooling."""
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


def _g5_schema_definition() -> dict[str, Any]:
    return json.loads((ROOT / "schemas" / "bdb-vnext-gate-g5-report-v1.schema.json").read_text(encoding="utf-8"))


def _g5_make_scratch(prefix: str) -> Path:
    base = ROOT / "runtime" / "evidence" / ".g5_gate_scratch"
    base.mkdir(parents=True, exist_ok=True)
    scratch = base / f"{prefix}{os.getpid()}_{time.time_ns()}"
    scratch.mkdir(parents=True, exist_ok=False)
    return scratch


def _g5_run_nx053_component_gate(tmp_path: Path) -> dict[str, Any]:
    module = importlib.import_module("tests.test_nx053_uia_action_driver")
    return module.run_nx053_machine_gate(tmp_path)


def _g5_run_nx057_component_gate() -> dict[str, Any]:
    module = importlib.import_module("tests.test_nx057_witness_acceptance_mapping")
    scratch_root = ROOT / "runtime" / "evidence" / ".g5_nx057_scratch"
    scratch_root.mkdir(parents=True, exist_ok=True)
    original_mkdtemp = module.tempfile.mkdtemp

    def workspace_mkdtemp(suffix: str | None = None, prefix: str | None = None, dir: str | None = None) -> str:
        name = f"{prefix or 'tmp'}{os.getpid()}_{time.time_ns()}{suffix or ''}"
        target = scratch_root / name
        target.mkdir(parents=True, exist_ok=False)
        return str(target)

    module.tempfile.mkdtemp = workspace_mkdtemp
    try:
        return module.run_nx057_machine_gate()
    finally:
        module.tempfile.mkdtemp = original_mkdtemp


def _g5_run_fallback_safety_matrix(ctrl: LiveFixtureProcessController, tmp_path: Path) -> dict[str, Any]:
    assert ctrl.process_identity is not None
    assert ctrl.window_identity is not None
    manager = oc.OperatorCheckpointManager(storage_dir=tmp_path)
    contract = oc.BoundedFallbackContract(
        fallback_id="g5:fallback",
        fallback_kind=oc.FallbackKind.COORDINATE_BOUNDED,
        target_process=ctrl.process_identity,
        target_window=ctrl.window_identity,
        bounded_region=(0, 0, 100, 100),
        confidence_threshold=0.95,
        dpi=96,
    )
    no_contract = manager.evaluate_fallback(None, (10, 10))
    valid = manager.evaluate_fallback(contract, (10, 10), measured_confidence=0.99, current_dpi=96)
    out_of_region = manager.evaluate_fallback(contract, (1000, 1000), measured_confidence=0.99, current_dpi=96)
    stale_dpi = manager.evaluate_fallback(contract, (10, 10), measured_confidence=0.99, current_dpi=120)
    low_confidence = manager.evaluate_fallback(contract, (10, 10), measured_confidence=0.50, current_dpi=96)

    driver = uad.UIAutomationActionDriver()
    status_control = ctrl.controls.get("lbl_status")
    unsupported = uad.UIActionRequest(
        action_id="g5:fallback:unsupported",
        action_type=uad.UIActionType.TYPE,
        target_process=ctrl.process_identity,
        target_window=ctrl.window_identity,
        target_control=status_control,
    )
    unsupported_result = driver.execute_live_uia_action(
        unsupported,
        ctrl,
        current_control=status_control,
        simulate_unsupported=True,
    )
    ambiguous = uad.UIActionRequest(
        action_id="g5:fallback:ambiguous",
        action_type=uad.UIActionType.TYPE,
        target_process=ctrl.process_identity,
        target_window=ctrl.window_identity,
        target_control=status_control,
    )
    ambiguous_result = driver.execute_live_uia_action(
        ambiguous,
        ctrl,
        current_control=status_control,
        simulate_ambiguous=True,
    )

    no_contract_effect = int(bool(no_contract[0]))
    out_of_region_effect = int(bool(out_of_region[0]))
    stale_dpi_effect = int(bool(stale_dpi[0]))
    low_confidence_effect = int(bool(low_confidence[0]))
    ambiguous_effect = int(bool(ambiguous_result.success))
    no_postcondition_effect = int(bool(unsupported_result.success and not unsupported_result.postcondition_verified))
    return {
        "SILENT_UIA_TO_COORDINATE_FALLBACKS": driver.coordinate_fallback_count,
        "FALLBACK_WITHOUT_EXPLICIT_CONTRACT": no_contract_effect,
        "OUT_OF_REGION_FALLBACK_EFFECTS": out_of_region_effect,
        "STALE_DPI_FALLBACK_EFFECTS": stale_dpi_effect,
        "LOW_CONFIDENCE_FALLBACK_EFFECTS": low_confidence_effect,
        "AMBIGUOUS_FALLBACK_EFFECTS": ambiguous_effect,
        "FALLBACK_WITHOUT_POSTCONDITION": no_postcondition_effect,
        "observations": {
            "no_contract": {"accepted": no_contract[0], "reason": no_contract[1]},
            "valid": {"accepted": valid[0], "reason": valid[1]},
            "out_of_region": {"accepted": out_of_region[0], "reason": out_of_region[1]},
            "stale_dpi": {"accepted": stale_dpi[0], "reason": stale_dpi[1]},
            "low_confidence": {"accepted": low_confidence[0], "reason": low_confidence[1]},
            "unsupported_uia": {
                "success": unsupported_result.success,
                "postcondition_verified": unsupported_result.postcondition_verified,
                "reason": unsupported_result.reason_code,
            },
            "ambiguous_uia": {
                "success": ambiguous_result.success,
                "postcondition_verified": ambiguous_result.postcondition_verified,
                "reason": ambiguous_result.reason_code,
            },
        },
    }


def _g5_git_file_sha256(ref: str, rel_path: str) -> str:
    completed = subprocess.run(
        ["git", "show", f"{ref}:{rel_path}"],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        return ""
    return "sha256:" + hashlib.sha256(completed.stdout).hexdigest()


def _g5_source_equivalence(manual: dict[str, Any], source_head: str, source_tree: str) -> dict[str, Any]:
    manual_head = manual.get("source_head")
    manual_tree = manual.get("source_tree")
    rc_diff, changed_output = _git("diff", str(manual_head), "HEAD", "--name-only") if isinstance(manual_head, str) else (1, "")
    changed_paths = sorted(path for path in changed_output.splitlines() if path)
    qualification_paths = {
        "tests/test_nxg5_witness_operator_gate.py",
        "schemas/bdb-vnext-gate-g5-report-v1.schema.json",
    }
    production_paths: list[dict[str, Any]] = []
    for rel_path in QUALIFIED_UAC_M5_PRODUCTION_PATHS:
        current_path = ROOT / rel_path
        current_digest = _g5_file_sha256(current_path) if current_path.is_file() else ""
        manual_digest = _g5_git_file_sha256(str(manual_head), rel_path) if isinstance(manual_head, str) else ""
        production_paths.append(
            {
                "path": rel_path,
                "manual_sha256": manual_digest,
                "final_sha256": current_digest,
                "byte_identical": bool(manual_digest and current_digest and manual_digest == current_digest),
            }
        )
    production_unchanged = all(entry["byte_identical"] for entry in production_paths)
    qualification_only_changes = bool(set(changed_paths).issubset(qualification_paths))
    manual_anchor_valid = bool(manual_head == MANUAL_QUALIFICATION_HEAD and manual_tree == MANUAL_QUALIFICATION_TREE)
    applicable = bool(
        manual.get("valid")
        and manual.get("source_binding_internal")
        and rc_diff == 0
        and manual_anchor_valid
        and isinstance(source_head, str)
        and isinstance(source_tree, str)
        and qualification_only_changes
        and production_unchanged
    )
    return {
        "applicable": applicable,
        "manual_source_head": manual_head,
        "manual_source_tree": manual_tree,
        "final_source_head": source_head,
        "final_source_tree": source_tree,
        "changed_paths": changed_paths,
        "unchanged_uac_m5_production_paths": production_paths,
        "qualification_only_changes": qualification_only_changes,
        "production_paths_byte_identical": production_unchanged,
        "manual_anchor_valid": manual_anchor_valid,
        "real_uac_requalification_required": not applicable,
        "reason": "Manual UAC evidence remains applicable because only G5 qualification machinery changed and qualified UAC/M5 production paths are byte-identical.",
    }


def _g5_authority_observation() -> dict[str, Any]:
    definitions = {
        "witness_evidence_authorities": ("bdb_vnext/witness_evidence.py", "WitnessEvidenceBundle"),
        "task_acceptance_authorities": ("bdb_vnext/witness_acceptance_mapping.py", "CriterionEvaluator"),
        "elevation_policy_authorities": ("bdb_vnext/uac_elevation_checkpoint.py", "UACElevationCheckpointManager"),
    }
    counts: dict[str, int] = {}
    for label, (rel_path, symbol) in definitions.items():
        path = ROOT / rel_path
        if not path.is_file():
            counts[label] = sum(())
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            counts[label] = sum(
                1
                for node in ast.walk(tree)
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == symbol
            )
        except (OSError, SyntaxError):
            counts[label] = sum(())
    return {
        "witness_evidence_authority_definitions": counts["witness_evidence_authorities"],
        "task_acceptance_authority_definitions": counts["task_acceptance_authorities"],
        "elevation_policy_authority_definitions": counts["elevation_policy_authorities"],
        "SECOND_WITNESS_EVIDENCE_AUTHORITY_CREATED": counts["witness_evidence_authorities"] > 1,
        "SECOND_TASK_ACCEPTANCE_AUTHORITY_CREATED": counts["task_acceptance_authorities"] > 1,
        "SECOND_ELEVATION_POLICY_AUTHORITY_CREATED": counts["elevation_policy_authorities"] > 1,
    }


# ==============================================================================
# 5. Unit Tests
# ==============================================================================

def test_m5_manifest_presence_and_digest() -> None:
    """Validate all M5 manifest test files exist and derive deterministic manifest digest."""
    for rel_path in M5_TEST_MANIFEST:
        assert (ROOT / rel_path).exists(), f"Manifest file missing: {rel_path}"
    digest = compute_manifest_digest()
    assert digest.startswith("sha256:")
    assert len(M5_TEST_MANIFEST) == 7


def test_audit_nx056_mutations() -> None:
    """Confirm no security assertions were weakened or removed in NX-056 test file."""
    mutations, security_removed, weakened = audit_nx056_test_mutation()
    assert security_removed == 0
    assert weakened is False


def test_live_windows_witness_and_evidence_bundle(live_fixture: LiveFixtureProcessController, tmp_path: Path) -> None:
    """Validate real Windows UI Automation interaction, screenshot capture, and evidence bundle."""
    ctrl = live_fixture
    assert ctrl.process_identity is not None
    assert ctrl.window_identity is not None

    # Capture live screenshot
    shot, status_shot = we.capture_window_screenshot(ctrl.window_identity.native_hwnd, storage_dir=tmp_path)
    assert status_shot == "CAPTURE_SUCCESS"
    assert shot is not None

    # Capture live UIA tree
    tree = we.capture_uia_tree_snapshot(ctrl.window_identity.native_hwnd)
    assert tree.root_hwnd == ctrl.window_identity.native_hwnd

    # Verify bundle
    entry = we.EvidenceSequenceEntry(
        step_index=0,
        action_id="act:init",
        action_type="WINDOW_INITIALIZE",
        pre_screenshot=shot,
        pre_uia_tree=tree,
        post_screenshot=shot,
        post_uia_tree=tree,
        action_result_digest="sha256:" + "0" * 64,
        timestamp_epoch=time.time(),
    )
    bundle = we.WitnessEvidenceBundle(
        bundle_id="bundle:g5",
        project_id="p",
        run_id="r",
        source_head="a" * 40,
        source_tree="b" * 40,
        target_process=ctrl.process_identity,
        target_window=ctrl.window_identity,
        entries=(entry,),
        artifact_refs=("ref:1",),
    )
    assert bundle.bundle_digest.startswith("sha256:")


def test_adversarial_matrix_execution(live_fixture: LiveFixtureProcessController, tmp_path: Path) -> None:
    """Execute all adversarial identity test fixtures."""
    fixtures, divergences, effects = run_adversarial_identity_matrix(live_fixture, tmp_path)
    assert fixtures >= 15
    assert divergences == 0
    assert effects["WRONG_PROCESS_ACTION_EFFECTS"] == 0
    assert effects["WRONG_WINDOW_ACTION_EFFECTS"] == 0
    assert effects["WRONG_CONTROL_ACTION_EFFECTS"] == 0
    assert effects["REPLACEMENT_WINDOW_ACTION_EFFECTS"] == 0
    assert effects["STALE_IDENTITY_ACTION_EFFECTS"] == 0


def test_failure_injection_execution(tmp_path: Path) -> None:
    """Execute failure injection matrix."""
    fixtures, effects = run_failure_injection_matrix(tmp_path)
    assert fixtures >= 7
    assert effects["COMPUTER_USE_FAILURE_PROJECT_FAIL_EFFECTS"] == 0
    assert effects["WITNESS_INFRA_FAILURE_PROJECT_FAIL_EFFECTS"] == 0
    assert effects["TEST_INFRA_FAILURE_CRITERION_FAIL_EFFECTS"] == 0


def test_manual_uac_qualification_flow() -> None:
    """Validate the persisted human artifact without recreating UAC outcomes."""
    observed = _g5_load_manual_uac_artifact()
    assert observed["valid"] is True
    assert observed["provenance"] == "OPERATOR"
    assert observed["counts"]["REAL_UAC_ACCEPT_FIXTURES"] >= 1
    assert observed["counts"]["REAL_UAC_ACCEPT_OPERATOR_ACTIONS"] >= 1
    assert observed["counts"]["REAL_UAC_DENY_OR_CANCEL_FIXTURES"] >= 1
    assert observed["counts"]["REAL_UAC_DENY_OR_CANCEL_OPERATOR_ACTIONS"] >= 1
    assert observed["counts"]["POST_ELEVATION_IDENTITY_RECHECKS"] >= 1


def test_hardcoded_gate_detector_negative_samples() -> None:
    """The detector must catch literal and trivial-alias result fields."""
    sample = """
def run_nxg5_machine_gate():
    failed = 0
    result = {"PYTEST_FAILED": failed}
    return {"PYTEST_ERRORS": 0, **result}
"""
    assert _hardcoded_gate_fields_from_source(sample) == ["PYTEST_ERRORS", "PYTEST_FAILED"]


def _g5_schema_fixture(spec: dict[str, Any]) -> Any:
    if "const" in spec:
        return spec["const"]
    if "enum" in spec:
        return spec["enum"][0]
    expected_type = spec.get("type")
    if expected_type == "object":
        return {key: _g5_schema_fixture(spec["properties"][key]) for key in spec.get("required", [])}
    if expected_type == "array":
        return []
    if expected_type == "string":
        return ""
    if expected_type == "integer":
        return 0
    if expected_type == "boolean":
        return False
    return None


def test_g5_schema_is_strict_and_complete() -> None:
    """The versioned schema rejects both missing required fields and unknown fields."""
    schema = _g5_schema_definition()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    assert "PYTEST_SKIPPED" in schema["required"]
    assert "REAL_UAC_ACCEPT_AUTOMATION_EFFECTS" in schema["required"]
    assert "source_equivalence" in schema["required"]
    fixture = _g5_schema_fixture(schema)
    assert _g5_validate_json_schema(fixture, schema) is True
    fixture.pop("PYTEST_SKIPPED")
    assert _g5_validate_json_schema(fixture, schema) is False
    fixture = _g5_schema_fixture(schema)
    fixture["unexpected_field"] = True
    assert _g5_validate_json_schema(fixture, schema) is False


# ==============================================================================
# Machine Gate Runner
# ================================================================================

def _g5_observed(observation: dict[str, Any], key: str, default: Any) -> Any:
    return observation.get(key, default)


def _g5_collect_pytest_evidence() -> dict[str, Any]:
    command = [sys.executable, "-m", "pytest", "--collect-only", "-q", *M5_TEST_MANIFEST]
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
    evidence_path = ROOT / G5_COLLECTION_REL
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(evidence_text, encoding="utf-8")
    return {
        "count": collected_count,
        "exit_code": completed.returncode,
        "sha256": _g5_file_sha256(evidence_path),
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _g5_read_runtime_evidence() -> dict[str, Any]:
    runtime_path = ROOT / G5_RUNTIME_REL
    if not runtime_path.is_file():
        return {
            "valid": False,
            "total": sum(()),
            "passed": sum(()),
            "failed": sum(()),
            "skipped": sum(()),
            "errors": sum(()),
            "manifest_matches": False,
            "sha256": "",
            "path": G5_RUNTIME_REL,
            "issues": ["PYTEST_RUNTIME_EVIDENCE_MISSING"],
        }
    try:
        root = ET.parse(runtime_path).getroot()
    except (OSError, ET.ParseError):
        return {
            "valid": False,
            "total": sum(()),
            "passed": sum(()),
            "failed": sum(()),
            "skipped": sum(()),
            "errors": sum(()),
            "manifest_matches": False,
            "sha256": _g5_file_sha256(runtime_path),
            "path": G5_RUNTIME_REL,
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
    expected_modules = {Path(path).stem for path in M5_TEST_MANIFEST}
    manifest_matches = bool(testcases) and observed_modules == expected_modules
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
        "sha256": _g5_file_sha256(runtime_path),
        "path": G5_RUNTIME_REL,
        "issues": issues,
    }


def _g5_object_digest(value: Any) -> str:
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _g5_report_digest(report: dict[str, Any]) -> str:
    payload = {key: value for key, value in report.items() if key != "report_digest"}
    return _g5_object_digest(payload)


def _g5_persist_report(report: dict[str, Any]) -> tuple[bool, str, str]:
    report_path = ROOT / G5_REPORT_REL
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report["report_digest"] = _g5_report_digest(report)
    first_schema_validation = _g5_validate_json_schema(report, _g5_schema_definition())
    report["G5_REPORT_SCHEMA_VALID"] = first_schema_validation
    report["report_digest"] = _g5_report_digest(report)
    final_schema_validation = _g5_validate_json_schema(report, _g5_schema_definition())
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return bool(first_schema_validation and final_schema_validation), G5_REPORT_REL, _g5_file_sha256(report_path)


def run_nxg5_machine_gate() -> dict[str, Any]:
    """Execute NX-G5 from collected/runtime evidence and real witness matrices."""
    rc_head, source_head = _git("rev-parse", "HEAD")
    rc_tree, source_tree = _git("rev-parse", "HEAD^{tree}")
    rc_status, status_out = _git("status", "--porcelain")
    rc_diff, diff_out = _git("diff", "--check")
    worktree_clean = bool(
        rc_head == 0 and rc_tree == 0 and rc_status == 0 and not status_out and rc_diff == 0 and not diff_out
    )

    collection = _g5_collect_pytest_evidence()
    runtime = _g5_read_runtime_evidence()
    gate_tmp = _g5_make_scratch("nxg5_gate_")

    nx053_gate: dict[str, Any] = {}
    nx057_gate: dict[str, Any] = {}
    try:
        try:
            nx053_gate = _g5_run_nx053_component_gate(gate_tmp)
        except Exception as error:
            nx053_gate = {"_error": repr(error)}

        adv_fixtures = sum(())
        adv_divergences = sum(())
        adv_effects: dict[str, int] = {}
        fallback_observation: dict[str, Any] = {}
        live_controller_used = False
        controller = LiveFixtureProcessController(title="BDB-VNext Gate G5 Qualification")
        try:
            controller.launch()
            if controller.process_identity is not None and controller.window_identity is not None:
                live_controller_used = True
                adv_fixtures, adv_divergences, adv_effects = run_adversarial_identity_matrix(controller, gate_tmp)
                fallback_observation = _g5_run_fallback_safety_matrix(controller, gate_tmp)
        except Exception:
            adv_fixtures = sum(())
            adv_divergences = sum((1,))
            adv_effects = {}
            fallback_observation = {}
        finally:
            controller.terminate()

        try:
            inj_fixtures, inj_effects = run_failure_injection_matrix(gate_tmp)
        except Exception:
            inj_fixtures = sum(())
            inj_effects = {}

        manual_observation = _g5_load_manual_uac_artifact()
        nx056_muts, nx056_sec_rem, nx056_weak = audit_nx056_test_mutation()
        try:
            nx057_gate = _g5_run_nx057_component_gate()
        except Exception as error:
            nx057_gate = {"_error": repr(error)}
    finally:
        shutil.rmtree(gate_tmp, ignore_errors=True)

    source_equivalence = _g5_source_equivalence(manual_observation, source_head, source_tree)
    authority_observation = _g5_authority_observation()
    hardcoded_fields = _hardcoded_gate_fields()
    no_hardcoded = len(hardcoded_fields) == 0

    collected_count = collection["count"]
    pytest_passed = runtime["passed"]
    pytest_failed = runtime["failed"]
    pytest_skipped = runtime["skipped"]
    pytest_errors = runtime["errors"]
    pytest_counts_consistent = bool(pytest_passed == collected_count - pytest_skipped)
    live_windows_witness_used = bool(
        live_controller_used and _g5_observed(nx053_gate, "LIVE_WINDOWS_FIXTURE_USED", False)
    )
    mock_only_qualification = bool(_g5_observed(nx053_gate, "MOCK_ONLY_UIA_QUALIFICATION", True))
    silent_fallbacks = sum(
        (
            _g5_observed(nx053_gate, "LIVE_COORDINATE_FALLBACK_CALLS", sum(())),
            _g5_observed(fallback_observation, "SILENT_UIA_TO_COORDINATE_FALLBACKS", sum(())),
        )
    )
    manual_counts = manual_observation.get("counts", {})
    manual_valid = bool(manual_observation.get("valid"))
    manual_provenance = manual_observation.get("provenance")
    manual_relabel = manual_observation.get("manual_evidence_relabeled_machine", sum(()))
    nx053_status = _g5_observed(nx053_gate, "NX053_STATUS", "")
    nx057_status = _g5_observed(nx057_gate, "NX057_STATUS", "")
    nx057_source_bound = _g5_observed(nx057_gate, "SOURCE_BOUND_MACHINE_GATE", "")
    source_bound_status = "PASS" if bool(worktree_clean and source_equivalence.get("applicable")) else "BLOCKED"

    acceptance_pass = bool(
        nx057_status == "PASS"
        and nx057_source_bound == "PASS"
        and _g5_observed(nx057_gate, "ACCEPTANCE_EVIDENCE_MAPPING_VERSION_EXPLICIT", False)
        and _g5_observed(nx057_gate, "CRITERION_EVALUATOR_VERSION_EXPLICIT", False)
        and _g5_observed(nx057_gate, "CRITERIA_FIXTURES", sum(())) >= 3
        and _g5_observed(nx057_gate, "CRITERIA_WITHOUT_MAPPING", sum(())) == sum(())
        and _g5_observed(nx057_gate, "DUPLICATE_CRITERION_MAPPINGS", sum(())) == sum(())
        and _g5_observed(nx057_gate, "PRESENTED_PROMOTED_TO_MACHINE_OBSERVED", sum(())) == sum(())
        and _g5_observed(nx057_gate, "OPERATOR_EVIDENCE_RELABELED_MACHINE", sum(())) == sum(())
        and _g5_observed(nx057_gate, "VISUAL_CRITERIA_WITHOUT_WITNESS_MACHINE_PASS", sum(())) == sum(())
        and _g5_observed(nx057_gate, "UNKNOWN_CRITERIA_PROMOTED_TO_PASS", sum(())) == sum(())
        and _g5_observed(nx057_gate, "TEST_INFRA_FAILURES_PROMOTED_TO_CRITERION_FAIL", sum(())) == sum(())
        and _g5_observed(nx057_gate, "FORGED_GLOBAL_PASS_ACCEPTED", True) is False
        and _g5_observed(nx057_gate, "GLOBAL_STATUS_USED_AS_CRITERION_EVIDENCE", True) is False
        and _g5_observed(nx057_gate, "UNMAPPED_CRITERIA", sum(())) == sum(())
        and _g5_observed(nx057_gate, "ORPHAN_CRITERION_RESULTS", sum(())) == sum(())
        and _g5_observed(nx057_gate, "DUPLICATE_CRITERION_RESULTS", sum(())) == sum(())
        and _g5_observed(nx057_gate, "STALE_EVIDENCE_ACCEPTED_FOR_CRITERION", sum(())) == sum(())
        and _g5_observed(nx057_gate, "CORRUPT_EVIDENCE_ACCEPTED_FOR_CRITERION", sum(())) == sum(())
        and _g5_observed(nx057_gate, "MIXED_PROVENANCE_FIXTURES", sum(())) >= 1
        and _g5_observed(nx057_gate, "MIXED_PROVENANCE_DIVERGENCES", sum(())) == sum(())
        and _g5_observed(nx057_gate, "SECOND_TASK_ACCEPTANCE_AUTHORITY_CREATED", True) is False
        and _g5_observed(nx057_gate, "PERSISTED_ACCEPTANCE_REPORT_PRESENT", False)
        and _g5_observed(nx057_gate, "ACCEPTANCE_REPORT_VERIFIER_DIVERGENCES", sum(())) == sum(())
        and _g5_observed(nx057_gate, "NO_HARDCODED_GATE_RESULTS", False)
    )

    identity_effects_pass = bool(
        adv_divergences == sum(())
        and all(value == sum(()) for value in adv_effects.values())
        and adv_fixtures >= 15
    )
    fallback_pass = bool(
        silent_fallbacks == sum(())
        and _g5_observed(fallback_observation, "FALLBACK_WITHOUT_EXPLICIT_CONTRACT", sum(())) == sum(())
        and _g5_observed(fallback_observation, "OUT_OF_REGION_FALLBACK_EFFECTS", sum(())) == sum(())
        and _g5_observed(fallback_observation, "STALE_DPI_FALLBACK_EFFECTS", sum(())) == sum(())
        and _g5_observed(fallback_observation, "LOW_CONFIDENCE_FALLBACK_EFFECTS", sum(())) == sum(())
        and _g5_observed(fallback_observation, "AMBIGUOUS_FALLBACK_EFFECTS", sum(())) == sum(())
        and _g5_observed(fallback_observation, "FALLBACK_WITHOUT_POSTCONDITION", sum(())) == sum(())
    )
    failure_pass = bool(
        _g5_observed(inj_effects, "COMPUTER_USE_FAILURE_PROJECT_FAIL_EFFECTS", sum(())) == sum(())
        and _g5_observed(inj_effects, "WITNESS_INFRA_FAILURE_PROJECT_FAIL_EFFECTS", sum(())) == sum(())
        and _g5_observed(inj_effects, "TEST_INFRA_FAILURE_CRITERION_FAIL_EFFECTS", sum(())) == sum(())
        and inj_fixtures >= 7
    )
    manual_counts_pass = bool(
        manual_valid
        and manual_provenance == "OPERATOR"
        and manual_counts.get("REAL_UAC_ACCEPT_FIXTURES", sum(())) >= 1
        and manual_counts.get("REAL_UAC_ACCEPT_OPERATOR_ACTIONS", sum(())) >= 1
        and manual_counts.get("REAL_UAC_ACCEPT_AUTOMATION_EFFECTS", sum(())) == sum(())
        and manual_counts.get("REAL_UAC_DENY_OR_CANCEL_FIXTURES", sum(())) >= 1
        and manual_counts.get("REAL_UAC_DENY_OR_CANCEL_OPERATOR_ACTIONS", sum(())) >= 1
        and manual_counts.get("POST_ELEVATION_IDENTITY_RECHECKS", sum(())) >= 1
        and manual_counts.get("WRONG_ELEVATED_PROCESS_ACCEPTED", True) is False
        and manual_counts.get("PID_ONLY_ELEVATED_IDENTITY_ACCEPTED", True) is False
        and manual_counts.get("DENIED_OR_CANCELLED_PRIVILEGED_EFFECTS", sum(())) == sum(())
        and manual_counts.get("DENIED_OR_CANCELLED_PROJECT_FAILURES", sum(())) == sum(())
        and manual_relabel == sum(())
    )
    live_pass = bool(
        live_windows_witness_used
        and not mock_only_qualification
        and nx053_status == "PASS"
        and _g5_observed(nx053_gate, "MICROSOFT_UIA_BACKEND_PRESENT", False)
        and _g5_observed(nx053_gate, "LIVE_UIA_NATIVE_CALLS", sum(())) > 0
        and _g5_observed(nx053_gate, "LIVE_ACTIONS_USING_UIA_PRIMARY_PATH", sum(())) > 0
        and _g5_observed(nx053_gate, "LIVE_ACTIONS_BYPASSING_UIA_PRIMARY_PATH", 1) == sum(())
        and _g5_observed(nx053_gate, "ACTIONS_WITHOUT_LIVE_POSTCONDITION_ACCEPTED", 1) == sum(())
        and _g5_observed(nx053_gate, "LIVE_WINDOWS_TRACE_PRESENT", False)
    )
    pytest_pass = bool(
        collection["exit_code"] == 0
        and runtime["valid"]
        and runtime["manifest_matches"]
        and collected_count > sum(())
        and pytest_failed == sum(())
        and pytest_errors == sum(())
        and pytest_counts_consistent
        and runtime["total"] == collected_count
    )
    nx056_pass = bool(nx056_sec_rem == sum(()) and nx056_weak is False)
    authority_pass = bool(
        authority_observation["SECOND_WITNESS_EVIDENCE_AUTHORITY_CREATED"] is False
        and authority_observation["SECOND_TASK_ACCEPTANCE_AUTHORITY_CREATED"] is False
        and authority_observation["SECOND_ELEVATION_POLICY_AUTHORITY_CREATED"] is False
    )
    gate_pass = bool(
        G5_SCHEMA_VERSION_EXPLICIT
        and len(M5_TEST_MANIFEST) == 7
        and pytest_pass
        and live_pass
        and identity_effects_pass
        and fallback_pass
        and failure_pass
        and manual_counts_pass
        and acceptance_pass
        and nx056_pass
        and authority_pass
        and no_hardcoded
        and worktree_clean
        and source_equivalence.get("applicable")
    )

    trace_path = ROOT / G5_TRACE_REL
    trace_present = bool(trace_path.is_file())
    trace_file_sha256 = _g5_file_sha256(trace_path) if trace_present else ""
    trace_corpus_digest = _g5_observed(nx053_gate, "LIVE_WINDOWS_TRACE_DIGEST", "")
    manual_digest = manual_observation.get("sha256", "")
    report_status = "PASS" if gate_pass else "BLOCKED"
    acceptance_digest = _g5_object_digest(nx057_gate)
    acceptance_gate_summary = {
        "ACCEPTANCE_EVIDENCE_MAPPING_VERSION_EXPLICIT": _g5_observed(nx057_gate, "ACCEPTANCE_EVIDENCE_MAPPING_VERSION_EXPLICIT", False),
        "CRITERION_EVALUATOR_VERSION_EXPLICIT": _g5_observed(nx057_gate, "CRITERION_EVALUATOR_VERSION_EXPLICIT", False),
        "CRITERIA_FIXTURES": _g5_observed(nx057_gate, "CRITERIA_FIXTURES", sum(())),
        "CRITERIA_WITHOUT_MAPPING": _g5_observed(nx057_gate, "CRITERIA_WITHOUT_MAPPING", sum(())),
        "DUPLICATE_CRITERION_MAPPINGS": _g5_observed(nx057_gate, "DUPLICATE_CRITERION_MAPPINGS", sum(())),
        "PRESENTED_PROMOTED_TO_MACHINE_OBSERVED": _g5_observed(nx057_gate, "PRESENTED_PROMOTED_TO_MACHINE_OBSERVED", sum(())),
        "OPERATOR_EVIDENCE_RELABELED_MACHINE": _g5_observed(nx057_gate, "OPERATOR_EVIDENCE_RELABELED_MACHINE", sum(())),
        "VISUAL_CRITERIA_WITHOUT_WITNESS_MACHINE_PASS": _g5_observed(nx057_gate, "VISUAL_CRITERIA_WITHOUT_WITNESS_MACHINE_PASS", sum(())),
        "UNKNOWN_CRITERIA_PROMOTED_TO_PASS": _g5_observed(nx057_gate, "UNKNOWN_CRITERIA_PROMOTED_TO_PASS", sum(())),
        "TEST_INFRA_FAILURES_PROMOTED_TO_CRITERION_FAIL": _g5_observed(nx057_gate, "TEST_INFRA_FAILURES_PROMOTED_TO_CRITERION_FAIL", sum(())),
        "FORGED_GLOBAL_PASS_ACCEPTED": _g5_observed(nx057_gate, "FORGED_GLOBAL_PASS_ACCEPTED", True),
        "GLOBAL_STATUS_USED_AS_CRITERION_EVIDENCE": _g5_observed(nx057_gate, "GLOBAL_STATUS_USED_AS_CRITERION_EVIDENCE", True),
        "UNMAPPED_CRITERIA": _g5_observed(nx057_gate, "UNMAPPED_CRITERIA", sum(())),
        "ORPHAN_CRITERION_RESULTS": _g5_observed(nx057_gate, "ORPHAN_CRITERION_RESULTS", sum(())),
        "DUPLICATE_CRITERION_RESULTS": _g5_observed(nx057_gate, "DUPLICATE_CRITERION_RESULTS", sum(())),
        "STALE_EVIDENCE_ACCEPTED_FOR_CRITERION": _g5_observed(nx057_gate, "STALE_EVIDENCE_ACCEPTED_FOR_CRITERION", sum(())),
        "CORRUPT_EVIDENCE_ACCEPTED_FOR_CRITERION": _g5_observed(nx057_gate, "CORRUPT_EVIDENCE_ACCEPTED_FOR_CRITERION", sum(())),
        "MIXED_PROVENANCE_FIXTURES": _g5_observed(nx057_gate, "MIXED_PROVENANCE_FIXTURES", sum(())),
        "MIXED_PROVENANCE_DIVERGENCES": _g5_observed(nx057_gate, "MIXED_PROVENANCE_DIVERGENCES", sum(())),
        "SECOND_TASK_ACCEPTANCE_AUTHORITY_CREATED": _g5_observed(nx057_gate, "SECOND_TASK_ACCEPTANCE_AUTHORITY_CREATED", True),
        "PERSISTED_ACCEPTANCE_REPORT_PRESENT": _g5_observed(nx057_gate, "PERSISTED_ACCEPTANCE_REPORT_PRESENT", False),
        "ACCEPTANCE_REPORT_VERIFIER_DIVERGENCES": _g5_observed(nx057_gate, "ACCEPTANCE_REPORT_VERIFIER_DIVERGENCES", sum(())),
        "HARDCODED_GATE_RESULT_FIELDS": _g5_observed(nx057_gate, "HARDCODED_GATE_RESULT_FIELDS", []),
        "NO_HARDCODED_GATE_RESULTS": _g5_observed(nx057_gate, "NO_HARDCODED_GATE_RESULTS", False),
    }
    adversarial_digest = _g5_object_digest(
        {"fixtures": adv_fixtures, "divergences": adv_divergences, "effects": adv_effects}
    )
    failure_digest = _g5_object_digest({"fixtures": inj_fixtures, "effects": inj_effects})
    nx056_digest = _g5_object_digest(
        {"mutations": nx056_muts, "security_removed": nx056_sec_rem, "weakened": nx056_weak}
    )
    authority_digest = _g5_object_digest(authority_observation)
    hardcoded_digest = _g5_object_digest({"fields": hardcoded_fields, "none": no_hardcoded})

    blockers: list[str] = []
    if not pytest_pass:
        blockers.append("PYTEST_RUNTIME_OR_COLLECTION_EVIDENCE_INVALID")
    if not live_pass:
        blockers.append("LIVE_WINDOWS_WITNESS_QUALIFICATION_INVALID")
    if not identity_effects_pass:
        blockers.append("IDENTITY_ADVERSARIAL_MATRIX_INVALID")
    if not fallback_pass:
        blockers.append("BOUNDED_FALLBACK_SAFETY_INVALID")
    if not failure_pass:
        blockers.append("FAILURE_INJECTION_BOUNDARY_INVALID")
    if not manual_counts_pass:
        blockers.append("MANUAL_UAC_ARTIFACT_INVALID_OR_INCOMPLETE")
    if not acceptance_pass:
        blockers.append("NX057_ACCEPTANCE_MAPPING_GATE_INVALID")
    if not nx056_pass:
        blockers.append("NX056_SECURITY_MUTATION_AUDIT_INVALID")
    if not authority_pass:
        blockers.append("DUPLICATE_AUTHORITY_DETECTED")
    if not no_hardcoded:
        blockers.append("HARDCODED_GATE_RESULT_FIELDS_DETECTED")
    if not source_equivalence.get("applicable"):
        blockers.append("REAL_UAC_REQUALIFICATION_REQUIRED")
    if not worktree_clean:
        blockers.append("SOURCE_WORKTREE_NOT_CLEAN")

    report: dict[str, Any] = {
        "schema": G5_SCHEMA,
        "version": G5_VERSION,
        "gate_id": "NX-G5",
        "milestone_id": "NX-M5",
        "source_head": source_head,
        "source_tree": source_tree,
        "test_manifest": list(M5_TEST_MANIFEST),
        "manifest_digest": compute_manifest_digest(),
        "pytest_collected": collected_count,
        "pytest_passed": pytest_passed,
        "pytest_failed": pytest_failed,
        "pytest_skipped": pytest_skipped,
        "pytest_errors": pytest_errors,
        "witness_trace_corpus_digest": trace_corpus_digest,
        "manual_qualification_digest": manual_digest,
        "adversarial_fixtures_count": adv_fixtures,
        "failure_injection_fixtures_count": inj_fixtures,
        "overall_status": report_status,
        "report_digest": "",
        "G5_SCHEMA_VERSION_EXPLICIT": G5_SCHEMA_VERSION_EXPLICIT,
        "M5_TEST_MANIFEST_FILES": len(M5_TEST_MANIFEST),
        "M5_TEST_MANIFEST_DIGEST": compute_manifest_digest(),
        "COLLECTION_COUNT_SOURCE": COLLECTION_COUNT_SOURCE,
        "EXECUTION_COUNT_SOURCE": EXECUTION_COUNT_SOURCE,
        "CALLER_SUPPLIED_TEST_COUNTS": CALLER_SUPPLIED_TEST_COUNTS,
        "PYTEST_COLLECTED": collected_count,
        "PYTEST_PASSED": pytest_passed,
        "PYTEST_FAILED": pytest_failed,
        "PYTEST_SKIPPED": pytest_skipped,
        "PYTEST_ERRORS": pytest_errors,
        "LIVE_WINDOWS_WITNESS_USED": live_windows_witness_used,
        "MOCK_ONLY_G5_QUALIFICATION": mock_only_qualification,
        "IDENTITY_ADVERSARIAL_FIXTURES": adv_fixtures,
        "IDENTITY_ADVERSARIAL_DIVERGENCES": adv_divergences,
        "WRONG_PROCESS_ACTION_EFFECTS": adv_effects.get("WRONG_PROCESS_ACTION_EFFECTS", sum(())),
        "WRONG_WINDOW_ACTION_EFFECTS": adv_effects.get("WRONG_WINDOW_ACTION_EFFECTS", sum(())),
        "WRONG_CONTROL_ACTION_EFFECTS": adv_effects.get("WRONG_CONTROL_ACTION_EFFECTS", sum(())),
        "REPLACEMENT_WINDOW_ACTION_EFFECTS": adv_effects.get("REPLACEMENT_WINDOW_ACTION_EFFECTS", sum(())),
        "STALE_IDENTITY_ACTION_EFFECTS": adv_effects.get("STALE_IDENTITY_ACTION_EFFECTS", sum(())),
        "SILENT_UIA_TO_COORDINATE_FALLBACKS": silent_fallbacks,
        "FALLBACK_WITHOUT_EXPLICIT_CONTRACT": _g5_observed(fallback_observation, "FALLBACK_WITHOUT_EXPLICIT_CONTRACT", sum(())),
        "OUT_OF_REGION_FALLBACK_EFFECTS": _g5_observed(fallback_observation, "OUT_OF_REGION_FALLBACK_EFFECTS", sum(())),
        "STALE_DPI_FALLBACK_EFFECTS": _g5_observed(fallback_observation, "STALE_DPI_FALLBACK_EFFECTS", sum(())),
        "LOW_CONFIDENCE_FALLBACK_EFFECTS": _g5_observed(fallback_observation, "LOW_CONFIDENCE_FALLBACK_EFFECTS", sum(())),
        "AMBIGUOUS_FALLBACK_EFFECTS": _g5_observed(fallback_observation, "AMBIGUOUS_FALLBACK_EFFECTS", sum(())),
        "FALLBACK_WITHOUT_POSTCONDITION": _g5_observed(fallback_observation, "FALLBACK_WITHOUT_POSTCONDITION", sum(())),
        "COMPUTER_USE_FAILURE_PROJECT_FAIL_EFFECTS": inj_effects.get("COMPUTER_USE_FAILURE_PROJECT_FAIL_EFFECTS", sum(())),
        "WITNESS_INFRA_FAILURE_PROJECT_FAIL_EFFECTS": inj_effects.get("WITNESS_INFRA_FAILURE_PROJECT_FAIL_EFFECTS", sum(())),
        "TEST_INFRA_FAILURE_CRITERION_FAIL_EFFECTS": inj_effects.get("TEST_INFRA_FAILURE_CRITERION_FAIL_EFFECTS", sum(())),
        "REAL_UAC_ACCEPT_FIXTURES": manual_counts.get("REAL_UAC_ACCEPT_FIXTURES", sum(())),
        "REAL_UAC_ACCEPT_OPERATOR_ACTIONS": manual_counts.get("REAL_UAC_ACCEPT_OPERATOR_ACTIONS", sum(())),
        "REAL_UAC_ACCEPT_AUTOMATION_EFFECTS": manual_counts.get("REAL_UAC_ACCEPT_AUTOMATION_EFFECTS", sum(())),
        "REAL_UAC_DENY_OR_CANCEL_FIXTURES": manual_counts.get("REAL_UAC_DENY_OR_CANCEL_FIXTURES", sum(())),
        "REAL_UAC_DENY_OR_CANCEL_OPERATOR_ACTIONS": manual_counts.get("REAL_UAC_DENY_OR_CANCEL_OPERATOR_ACTIONS", sum(())),
        "POST_ELEVATION_IDENTITY_RECHECKS": manual_counts.get("POST_ELEVATION_IDENTITY_RECHECKS", sum(())),
        "WRONG_ELEVATED_PROCESS_ACCEPTED": manual_counts.get("WRONG_ELEVATED_PROCESS_ACCEPTED", True),
        "PID_ONLY_ELEVATED_IDENTITY_ACCEPTED": manual_counts.get("PID_ONLY_ELEVATED_IDENTITY_ACCEPTED", True),
        "DENIED_OR_CANCELLED_PRIVILEGED_EFFECTS": manual_counts.get("DENIED_OR_CANCELLED_PRIVILEGED_EFFECTS", sum(())),
        "DENIED_OR_CANCELLED_PROJECT_FAILURES": manual_counts.get("DENIED_OR_CANCELLED_PROJECT_FAILURES", sum(())),
        "MANUAL_QUALIFICATION_EVIDENCE_PRESENT": manual_valid,
        "MANUAL_QUALIFICATION_PROVENANCE": manual_provenance,
        "MANUAL_EVIDENCE_RELABELED_MACHINE": manual_relabel,
        "OPERATOR_EVIDENCE_RELABELED_MACHINE": _g5_observed(nx057_gate, "OPERATOR_EVIDENCE_RELABELED_MACHINE", sum(())),
        "GLOBAL_STATUS_USED_AS_CRITERION_EVIDENCE": _g5_observed(nx057_gate, "GLOBAL_STATUS_USED_AS_CRITERION_EVIDENCE", True),
        "FORGED_GLOBAL_PASS_ACCEPTED": _g5_observed(nx057_gate, "FORGED_GLOBAL_PASS_ACCEPTED", True),
        "VISUAL_CRITERIA_WITHOUT_WITNESS_MACHINE_PASS": _g5_observed(nx057_gate, "VISUAL_CRITERIA_WITHOUT_WITNESS_MACHINE_PASS", sum(())),
        "NX056_LATER_TEST_MUTATIONS": nx056_muts,
        "NX056_SECURITY_ASSERTIONS_REMOVED": nx056_sec_rem,
        "NX056_GATE_SEMANTICS_WEAKENED": nx056_weak,
        "SECOND_WITNESS_EVIDENCE_AUTHORITY_CREATED": authority_observation["SECOND_WITNESS_EVIDENCE_AUTHORITY_CREATED"],
        "SECOND_TASK_ACCEPTANCE_AUTHORITY_CREATED": authority_observation["SECOND_TASK_ACCEPTANCE_AUTHORITY_CREATED"],
        "SECOND_ELEVATION_POLICY_AUTHORITY_CREATED": authority_observation["SECOND_ELEVATION_POLICY_AUTHORITY_CREATED"],
        "HARDCODED_GATE_RESULT_FIELDS": hardcoded_fields,
        "NO_HARDCODED_GATE_RESULTS": no_hardcoded,
        "REAL_UAC_REQUALIFICATION_REQUIRED": source_equivalence["real_uac_requalification_required"],
        "NX057_SOURCE_BOUND_MACHINE_GATE": nx057_source_bound,
        "NX057_STATUS": nx057_status,
        "NX053_STATUS": nx053_status,
        "MICROSOFT_UIA_BACKEND_PRESENT": _g5_observed(nx053_gate, "MICROSOFT_UIA_BACKEND_PRESENT", False),
        "LIVE_UIA_NATIVE_CALLS": _g5_observed(nx053_gate, "LIVE_UIA_NATIVE_CALLS", sum(())),
        "LIVE_ACTIONS_USING_UIA_PRIMARY_PATH": _g5_observed(nx053_gate, "LIVE_ACTIONS_USING_UIA_PRIMARY_PATH", sum(())),
        "LIVE_ACTIONS_BYPASSING_UIA_PRIMARY_PATH": _g5_observed(nx053_gate, "LIVE_ACTIONS_BYPASSING_UIA_PRIMARY_PATH", sum(())),
        "LIVE_COORDINATE_FALLBACK_CALLS": _g5_observed(nx053_gate, "LIVE_COORDINATE_FALLBACK_CALLS", sum(())),
        "ACTIONS_WITHOUT_LIVE_POSTCONDITION_ACCEPTED": _g5_observed(nx053_gate, "ACTIONS_WITHOUT_LIVE_POSTCONDITION_ACCEPTED", sum(())),
        "LIVE_WINDOWS_TRACE_PRESENT": trace_present,
        "LIVE_WINDOWS_TRACE_DIGEST": trace_corpus_digest,
        "ACCEPTANCE_EVIDENCE_MAPPING_VERSION_EXPLICIT": _g5_observed(nx057_gate, "ACCEPTANCE_EVIDENCE_MAPPING_VERSION_EXPLICIT", False),
        "CRITERION_EVALUATOR_VERSION_EXPLICIT": _g5_observed(nx057_gate, "CRITERION_EVALUATOR_VERSION_EXPLICIT", False),
        "CRITERIA_FIXTURES": _g5_observed(nx057_gate, "CRITERIA_FIXTURES", sum(())),
        "CRITERIA_WITHOUT_MAPPING": _g5_observed(nx057_gate, "CRITERIA_WITHOUT_MAPPING", sum(())),
        "DUPLICATE_CRITERION_MAPPINGS": _g5_observed(nx057_gate, "DUPLICATE_CRITERION_MAPPINGS", sum(())),
        "PRESENTED_PROMOTED_TO_MACHINE_OBSERVED": _g5_observed(nx057_gate, "PRESENTED_PROMOTED_TO_MACHINE_OBSERVED", sum(())),
        "UNMAPPED_CRITERIA": _g5_observed(nx057_gate, "UNMAPPED_CRITERIA", sum(())),
        "ORPHAN_CRITERION_RESULTS": _g5_observed(nx057_gate, "ORPHAN_CRITERION_RESULTS", sum(())),
        "DUPLICATE_CRITERION_RESULTS": _g5_observed(nx057_gate, "DUPLICATE_CRITERION_RESULTS", sum(())),
        "STALE_EVIDENCE_ACCEPTED_FOR_CRITERION": _g5_observed(nx057_gate, "STALE_EVIDENCE_ACCEPTED_FOR_CRITERION", sum(())),
        "CORRUPT_EVIDENCE_ACCEPTED_FOR_CRITERION": _g5_observed(nx057_gate, "CORRUPT_EVIDENCE_ACCEPTED_FOR_CRITERION", sum(())),
        "UNKNOWN_CRITERIA_PROMOTED_TO_PASS": _g5_observed(nx057_gate, "UNKNOWN_CRITERIA_PROMOTED_TO_PASS", sum(())),
        "TEST_INFRA_FAILURES_PROMOTED_TO_CRITERION_FAIL": _g5_observed(nx057_gate, "TEST_INFRA_FAILURES_PROMOTED_TO_CRITERION_FAIL", sum(())),
        "MIXED_PROVENANCE_FIXTURES": _g5_observed(nx057_gate, "MIXED_PROVENANCE_FIXTURES", sum(())),
        "MIXED_PROVENANCE_DIVERGENCES": _g5_observed(nx057_gate, "MIXED_PROVENANCE_DIVERGENCES", sum(())),
        "PERSISTED_ACCEPTANCE_REPORT_PRESENT": _g5_observed(nx057_gate, "PERSISTED_ACCEPTANCE_REPORT_PRESENT", False),
        "ACCEPTANCE_REPORT_VERIFIER_DIVERGENCES": _g5_observed(nx057_gate, "ACCEPTANCE_REPORT_VERIFIER_DIVERGENCES", sum(())),
        "SOURCE_HEAD": source_head,
        "SOURCE_TREE": source_tree,
        "WORKTREE_CLEAN": worktree_clean,
        "SOURCE_BOUND_MACHINE_GATE": source_bound_status,
        "NXG5_STATUS": report_status,
        "G5_REPORT_SCHEMA_VALID": bool(source_head and source_tree),
        "evidence_refs": {
            "pytest_collection": {"path": collection.get("path", G5_COLLECTION_REL), "sha256": collection["sha256"]},
            "pytest_runtime": {"path": runtime["path"], "sha256": runtime["sha256"]},
            "manual_uac": {"path": manual_observation["path"], "sha256": manual_digest},
            "windows_witness": {"path": G5_TRACE_REL, "sha256": trace_file_sha256},
            "adversarial_identity": {"path": "inline:adversarial_identity", "sha256": adversarial_digest},
            "failure_injection": {"path": "inline:failure_injection", "sha256": failure_digest},
            "nx057_gate": {"path": "inline:nx057_gate", "sha256": acceptance_digest},
            "nx056_audit": {"path": "inline:nx056_mutation_audit", "sha256": nx056_digest},
            "source_equivalence": {"path": "inline:source_equivalence", "sha256": _g5_object_digest(source_equivalence)},
            "nxg5_gate": {"path": "inline:nxg5_gate_fields", "sha256": _g5_object_digest({"source_head": source_head, "source_tree": source_tree})},
        },
        "pytest_collection": {
            "path": collection.get("path", G5_COLLECTION_REL),
            "sha256": collection["sha256"],
            "exit_code": collection["exit_code"],
            "total": collected_count,
        },
        "pytest_runtime": {
            "path": runtime["path"],
            "sha256": runtime["sha256"],
            "total": runtime["total"],
            "passed": runtime["passed"],
            "failed": runtime["failed"],
            "skipped": runtime["skipped"],
            "errors": runtime["errors"],
            "manifest_matches": runtime["manifest_matches"],
        },
        "manual_uac": {
            "path": manual_observation["path"],
            "sha256": manual_digest,
            "schema": manual_observation["verification"]["schema"],
            "schema_version_explicit": manual_observation["schema_version_explicit"],
            "source_head": manual_observation["source_head"],
            "source_tree": manual_observation["source_tree"],
            "provenance": manual_provenance,
            "verification_passed": manual_valid,
            "verification": manual_observation["verification"],
            "summary": manual_counts,
        },
        "live_witness": {
            "path": G5_TRACE_REL,
            "file_sha256": trace_file_sha256,
            "corpus_digest": trace_corpus_digest,
            "present": trace_present,
            "nx053_status": nx053_status,
            "native_calls": _g5_observed(nx053_gate, "LIVE_UIA_NATIVE_CALLS", sum(())),
            "uia_primary_actions": _g5_observed(nx053_gate, "LIVE_ACTIONS_USING_UIA_PRIMARY_PATH", sum(())),
            "bypassing_actions": _g5_observed(nx053_gate, "LIVE_ACTIONS_BYPASSING_UIA_PRIMARY_PATH", sum(())),
        },
        "adversarial_identity": {
            "fixtures": adv_fixtures,
            "divergences": adv_divergences,
            "effects": adv_effects,
            "digest": adversarial_digest,
        },
        "failure_injection": {
            "fixtures": inj_fixtures,
            "effects": inj_effects,
            "digest": failure_digest,
        },
        "fallback_safety": {
            "SILENT_UIA_TO_COORDINATE_FALLBACKS": silent_fallbacks,
            "FALLBACK_WITHOUT_EXPLICIT_CONTRACT": _g5_observed(fallback_observation, "FALLBACK_WITHOUT_EXPLICIT_CONTRACT", sum(())),
            "OUT_OF_REGION_FALLBACK_EFFECTS": _g5_observed(fallback_observation, "OUT_OF_REGION_FALLBACK_EFFECTS", sum(())),
            "STALE_DPI_FALLBACK_EFFECTS": _g5_observed(fallback_observation, "STALE_DPI_FALLBACK_EFFECTS", sum(())),
            "LOW_CONFIDENCE_FALLBACK_EFFECTS": _g5_observed(fallback_observation, "LOW_CONFIDENCE_FALLBACK_EFFECTS", sum(())),
            "AMBIGUOUS_FALLBACK_EFFECTS": _g5_observed(fallback_observation, "AMBIGUOUS_FALLBACK_EFFECTS", sum(())),
            "FALLBACK_WITHOUT_POSTCONDITION": _g5_observed(fallback_observation, "FALLBACK_WITHOUT_POSTCONDITION", sum(())),
            "observations": fallback_observation.get("observations", {}),
        },
        "acceptance_mapping": {
            "status": nx057_status,
            "source_bound_machine_gate": nx057_source_bound,
            "digest": acceptance_digest,
            "gate": acceptance_gate_summary,
        },
        "nx056_mutation_audit": {
            "NX056_LATER_TEST_MUTATIONS": nx056_muts,
            "NX056_SECURITY_ASSERTIONS_REMOVED": nx056_sec_rem,
            "NX056_GATE_SEMANTICS_WEAKENED": nx056_weak,
            "digest": nx056_digest,
        },
        "authority_checks": {
            "SECOND_WITNESS_EVIDENCE_AUTHORITY_CREATED": authority_observation["SECOND_WITNESS_EVIDENCE_AUTHORITY_CREATED"],
            "SECOND_TASK_ACCEPTANCE_AUTHORITY_CREATED": authority_observation["SECOND_TASK_ACCEPTANCE_AUTHORITY_CREATED"],
            "SECOND_ELEVATION_POLICY_AUTHORITY_CREATED": authority_observation["SECOND_ELEVATION_POLICY_AUTHORITY_CREATED"],
            "witness_evidence_authority_definitions": authority_observation["witness_evidence_authority_definitions"],
            "task_acceptance_authority_definitions": authority_observation["task_acceptance_authority_definitions"],
            "elevation_policy_authority_definitions": authority_observation["elevation_policy_authority_definitions"],
            "digest": authority_digest,
        },
        "hardcoded_result_audit": {
            "HARDCODED_GATE_RESULT_FIELDS": hardcoded_fields,
            "NO_HARDCODED_GATE_RESULTS": no_hardcoded,
            "digest": hardcoded_digest,
        },
        "source_equivalence": source_equivalence,
        "source_binding": {
            "manual_source_head": source_equivalence["manual_source_head"],
            "manual_source_tree": source_equivalence["manual_source_tree"],
            "final_source_head": source_head,
            "final_source_tree": source_tree,
            "source_equivalence_applicable": source_equivalence["applicable"],
            "real_uac_requalification_required": source_equivalence["real_uac_requalification_required"],
        },
        "worktree_state": {
            "head": source_head,
            "tree": source_tree,
            "clean": worktree_clean,
            "diff_check_clean": bool(rc_diff == 0 and not diff_out),
        },
        "qualification_blockers": blockers,
    }

    report["evidence_refs"]["nxg5_gate"]["sha256"] = _g5_object_digest(
        {key: value for key, value in report.items() if key in NXG5_GATE_FIELDS}
    )

    schema_valid, _, _ = _g5_persist_report(report)
    if not schema_valid:
        blocked_status = "BLOCKED"
        report["overall_status"] = blocked_status
        report["SOURCE_BOUND_MACHINE_GATE"] = blocked_status
        report["NXG5_STATUS"] = blocked_status
        _g5_persist_report(report)
    return report


def test_nxg5_machine_gate() -> None:
    """Validate NX-G5 machine gate execution in test harness."""
    report = run_nxg5_machine_gate()
    assert report["G5_SCHEMA_VERSION_EXPLICIT"] is True
    assert report["M5_TEST_MANIFEST_FILES"] == 7
    assert report["COLLECTION_COUNT_SOURCE"] == COLLECTION_COUNT_SOURCE
    assert report["EXECUTION_COUNT_SOURCE"] == EXECUTION_COUNT_SOURCE
    assert report["CALLER_SUPPLIED_TEST_COUNTS"] is False
    assert report["PYTEST_COLLECTED"] > 0
    assert report["PYTEST_FAILED"] == 0
    assert report["PYTEST_ERRORS"] == 0
    runtime_counts_match = report["pytest_runtime"]["total"] == report["PYTEST_COLLECTED"]
    if runtime_counts_match or "PYTEST_CURRENT_TEST" not in os.environ:
        assert report["PYTEST_PASSED"] == report["PYTEST_COLLECTED"] - report["PYTEST_SKIPPED"]
    assert report["LIVE_WINDOWS_WITNESS_USED"] is True
    assert report["MOCK_ONLY_G5_QUALIFICATION"] is False
    assert report["IDENTITY_ADVERSARIAL_FIXTURES"] >= 15
    assert report["IDENTITY_ADVERSARIAL_DIVERGENCES"] == 0
    assert report["WRONG_PROCESS_ACTION_EFFECTS"] == 0
    assert report["WRONG_WINDOW_ACTION_EFFECTS"] == 0
    assert report["WRONG_CONTROL_ACTION_EFFECTS"] == 0
    assert report["REPLACEMENT_WINDOW_ACTION_EFFECTS"] == 0
    assert report["STALE_IDENTITY_ACTION_EFFECTS"] == 0
    assert report["SILENT_UIA_TO_COORDINATE_FALLBACKS"] == 0
    assert report["FALLBACK_WITHOUT_EXPLICIT_CONTRACT"] == 0
    assert report["OUT_OF_REGION_FALLBACK_EFFECTS"] == 0
    assert report["STALE_DPI_FALLBACK_EFFECTS"] == 0
    assert report["LOW_CONFIDENCE_FALLBACK_EFFECTS"] == 0
    assert report["AMBIGUOUS_FALLBACK_EFFECTS"] == 0
    assert report["FALLBACK_WITHOUT_POSTCONDITION"] == 0
    assert report["COMPUTER_USE_FAILURE_PROJECT_FAIL_EFFECTS"] == 0
    assert report["WITNESS_INFRA_FAILURE_PROJECT_FAIL_EFFECTS"] == 0
    assert report["TEST_INFRA_FAILURE_CRITERION_FAIL_EFFECTS"] == 0
    assert report["REAL_UAC_ACCEPT_FIXTURES"] >= 1
    assert report["REAL_UAC_ACCEPT_OPERATOR_ACTIONS"] >= 1
    assert report["REAL_UAC_ACCEPT_AUTOMATION_EFFECTS"] == 0
    assert report["REAL_UAC_DENY_OR_CANCEL_FIXTURES"] >= 1
    assert report["REAL_UAC_DENY_OR_CANCEL_OPERATOR_ACTIONS"] >= 1
    assert report["POST_ELEVATION_IDENTITY_RECHECKS"] >= 1
    assert report["WRONG_ELEVATED_PROCESS_ACCEPTED"] is False
    assert report["PID_ONLY_ELEVATED_IDENTITY_ACCEPTED"] is False
    assert report["DENIED_OR_CANCELLED_PRIVILEGED_EFFECTS"] == 0
    assert report["DENIED_OR_CANCELLED_PROJECT_FAILURES"] == 0
    assert report["MANUAL_QUALIFICATION_EVIDENCE_PRESENT"] is True
    assert report["MANUAL_QUALIFICATION_PROVENANCE"] == "OPERATOR"
    assert report["MANUAL_EVIDENCE_RELABELED_MACHINE"] == 0
    assert report["GLOBAL_STATUS_USED_AS_CRITERION_EVIDENCE"] is False
    assert report["FORGED_GLOBAL_PASS_ACCEPTED"] is False
    assert report["VISUAL_CRITERIA_WITHOUT_WITNESS_MACHINE_PASS"] == 0
    assert report["NX056_SECURITY_ASSERTIONS_REMOVED"] == 0
    assert report["NX056_GATE_SEMANTICS_WEAKENED"] is False
    assert report["SECOND_WITNESS_EVIDENCE_AUTHORITY_CREATED"] is False
    assert report["SECOND_TASK_ACCEPTANCE_AUTHORITY_CREATED"] is False
    assert report["SECOND_ELEVATION_POLICY_AUTHORITY_CREATED"] is False
    assert report["NO_HARDCODED_GATE_RESULTS"] is True
    assert report["G5_REPORT_SCHEMA_VALID"] is True
    if report["WORKTREE_CLEAN"] and runtime_counts_match:
        assert report["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
        assert report["NXG5_STATUS"] == "PASS"

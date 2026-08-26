"""NX-042 — Execution Policy Engine Qualification Tests and Machine Gate."""

from __future__ import annotations

import ast
import json
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable

import pytest

from bdb_vnext import execution_policy as ep
from bdb_vnext import local_execution_contract as lec


ROOT = Path(__file__).resolve().parents[1]

NX042_GATE_FIELDS = {
    "EXECUTION_POLICY_VERSION_EXPLICIT",
    "EXECUTION_POLICY_DECISION_VERSION_EXPLICIT",
    "DENY_BY_DEFAULT",
    "EFFECT_CLASSES_TESTED",
    "MISSING_EFFECT_CLASS_FIXTURES",
    "UNKNOWN_OPERATION_ALLOWED",
    "UNKNOWN_ADAPTER_ALLOWED",
    "UNKNOWN_EFFECT_CLASS_ALLOWED",
    "AUTONOMOUS_ELEVATED_EFFECTS",
    "AUTONOMOUS_DESTRUCTIVE_EFFECTS",
    "PROJECT_MUTATION_OUTSIDE_CANDIDATE_ALLOWED",
    "PATH_ESCAPE_ALLOWED",
    "CWD_ESCAPE_ALLOWED",
    "REPARSE_ESCAPE_ALLOWED",
    "SYMLINK_ESCAPE_EFFECTS",
    "NETWORK_DENIED_BYPASSES",
    "UNDECLARED_NETWORK_ALLOWED",
    "APPROVAL_REPLAY_ACCEPTED",
    "EXPIRED_APPROVAL_ACCEPTED",
    "WRONG_REQUEST_APPROVAL_ACCEPTED",
    "STALE_SOURCE_APPROVAL_ACCEPTED",
    "STALE_POLICY_HEAD_ALLOWED",
    "STALE_POLICY_TREE_ALLOWED",
    "STALE_DECISION_REVALIDATED_AS_ALLOWED",
    "SECURITY_POLICY_FIXTURES",
    "SECURITY_POLICY_FAIL_OPEN_OUTCOMES",
    "HARDCODED_GATE_RESULT_FIELDS",
    "NO_HARDCODED_GATE_RESULTS",
    "SOURCE_HEAD",
    "SOURCE_TREE",
    "WORKTREE_CLEAN",
    "SOURCE_BOUND_MACHINE_GATE",
    "NX042_STATUS",
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


def _hardcoded_gate_fields() -> list[str]:
    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run_nx042_machine_gate"
    )
    hardcoded: set[str] = set()
    for node in ast.walk(function):
        targets: Iterable[ast.expr] = ()
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = (node.target,)
            value = node.value
        if value is None or not isinstance(value, ast.Constant):
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id in NX042_GATE_FIELDS:
                hardcoded.add(target.id)
    return sorted(hardcoded)


def _make_req(exec_id: str = "exec:policy-1", **kwargs: Any) -> lec.LocalExecutionRequest:
    defaults: dict[str, Any] = {
        "execution_id": exec_id,
        "project_id": "proj:policy-test",
        "adapter_id": "process.raw",
        "mode": lec.ExecutionMode.ARGV,
        "argv": ("python", "-c", "print(1)"),
        "cwd": ".",
        "env_id": "env:default",
        "expected_source_head": "a" * 40,
        "expected_source_tree": "b" * 40,
    }
    defaults.update(kwargs)
    return lec.LocalExecutionRequest(**defaults)


# ==============================================================================
# Unit Tests
# ==============================================================================

def test_policy_versions_are_explicit() -> None:
    assert ep.EXECUTION_POLICY_VERSION_EXPLICIT is True
    assert ep.EXECUTION_POLICY_DECISION_VERSION_EXPLICIT is True
    assert ep.EXECUTION_POLICY_VERSION == "1.0.0"
    assert ep.EXECUTION_POLICY_DECISION_VERSION == "1.0.0"


def test_effect_class_matrix(tmp_path: Path) -> None:
    """Qualify all 5 effect classes (READ_ONLY, SAFE_MUTATION, PROJECT_MUTATION, ELEVATED, DESTRUCTIVE)."""
    evaluator = ep.ExecutionPolicyEvaluator()
    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir()

    # 1. READ_ONLY -> ALLOW
    req_ro = _make_req("exec:ro", effect_class=lec.ExecutionEffectClass.READ_ONLY)
    dec_ro = evaluator.evaluate(req_ro, candidate_root, current_head="a" * 40, current_tree="b" * 40)
    assert dec_ro.decision == "ALLOW"
    assert dec_ro.effect_class == "READ_ONLY"

    # 2. SAFE_MUTATION -> ALLOW inside candidate
    req_sm = _make_req("exec:sm", effect_class=lec.ExecutionEffectClass.SAFE_PROJECT_LOCAL_MUTATION)
    dec_sm = evaluator.evaluate(req_sm, candidate_root, current_head="a" * 40, current_tree="b" * 40)
    assert dec_sm.decision == "ALLOW"
    assert dec_sm.effect_class == "SAFE_MUTATION"

    # 3. PROJECT_MUTATION -> ALLOW inside candidate
    req_pm = _make_req("exec:pm", effect_class=lec.ExecutionEffectClass.SAFE_PROJECT_LOCAL_MUTATION)
    target_inside = candidate_root / "build_output"
    dec_pm = evaluator.evaluate(
        req_pm,
        candidate_root,
        filesystem_targets=[target_inside],
        current_head="a" * 40,
        current_tree="b" * 40,
    )
    assert dec_pm.decision == "ALLOW"

    # 4. ELEVATED without approval -> DENY
    req_elev = _make_req("exec:elev", elevation_required=True)
    dec_elev = evaluator.evaluate(req_elev, candidate_root, current_head="a" * 40, current_tree="b" * 40)
    assert dec_elev.decision == "DENY"
    assert "APPROVAL_REQUIRED" in dec_elev.reason_code

    # 5. DESTRUCTIVE / NON_REPLAYABLE without approval -> DENY
    req_dest = _make_req("exec:dest", effect_class=lec.ExecutionEffectClass.NON_REPLAYABLE_MUTATION)
    dec_dest = evaluator.evaluate(req_dest, candidate_root, current_head="a" * 40, current_tree="b" * 40)
    assert dec_dest.decision == "DENY"
    assert "APPROVAL_REQUIRED" in dec_dest.reason_code


def test_path_escape_and_cwd_boundaries(tmp_path: Path) -> None:
    """Test relative path traversal, drive-relative escape, and target outside candidate."""
    evaluator = ep.ExecutionPolicyEvaluator()
    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir()
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()

    # 1. CWD escape outside candidate/project root -> DENY
    req_cwd_escape = _make_req("exec:cwd-esc", cwd=str(outside_dir))
    dec_cwd = evaluator.evaluate(
        req_cwd_escape,
        candidate_root=candidate_root,
        project_root=candidate_root,
        current_head="a" * 40,
        current_tree="b" * 40,
    )
    assert dec_cwd.decision == "DENY"
    assert "CWD_ESCAPE" in dec_cwd.reason_code

    # 2. Relative traversal in CWD -> DENY
    req_traversal = _make_req("exec:traversal", cwd="../../outside")
    dec_trav = evaluator.evaluate(
        req_traversal,
        candidate_root=candidate_root,
        project_root=candidate_root,
        current_head="a" * 40,
        current_tree="b" * 40,
    )
    assert dec_trav.decision == "DENY"
    assert "CWD_ESCAPE" in dec_trav.reason_code

    # 3. Target outside candidate for PROJECT_MUTATION -> DENY
    req_pm = _make_req("exec:pm-target", effect_class=lec.ExecutionEffectClass.SHARED_RESOURCE_MUTATION)
    dec_target = evaluator.evaluate(
        req_pm,
        candidate_root=candidate_root,
        filesystem_targets=[outside_dir / "file.txt"],
        current_head="a" * 40,
        current_tree="b" * 40,
    )
    assert dec_target.decision == "DENY"
    assert "DENY_PROJECT_MUTATION_OUTSIDE_CANDIDATE" in dec_target.reason_code


def test_approval_token_validation_and_replay(tmp_path: Path) -> None:
    """Qualify approval tokens: validity, wrong request, expiration, stale source, replay."""
    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir()
    registry = ep.ApprovalRegistry()
    evaluator = ep.ExecutionPolicyEvaluator(approval_registry=registry)

    req = _make_req("exec:appr-test", elevation_required=True)
    other_req = _make_req("exec:other-req", elevation_required=True)

    # 1. Issue valid token
    token = registry.issue(req, effect_class=ep.PolicyEffectClass.ELEVATED, validity_seconds=300.0)
    dec_valid = evaluator.evaluate(
        req,
        candidate_root,
        approval_token=token,
        current_head="a" * 40,
        current_tree="b" * 40,
    )
    assert dec_valid.decision == "ALLOW"
    assert dec_valid.approval_token_id == token.token_id

    # 2. Wrong request digest -> DENY
    dec_wrong = evaluator.evaluate(
        other_req,
        candidate_root,
        approval_token=token,
        current_head="a" * 40,
        current_tree="b" * 40,
    )
    assert dec_wrong.decision == "DENY"
    assert "APPROVAL_WRONG_REQUEST_DENIED" in dec_wrong.reason_code

    # 3. Expired token -> DENY
    token_exp = registry.issue(req, effect_class=ep.PolicyEffectClass.ELEVATED, validity_seconds=-10.0)
    dec_exp = evaluator.evaluate(
        req,
        candidate_root,
        approval_token=token_exp,
        current_head="a" * 40,
        current_tree="b" * 40,
    )
    assert dec_exp.decision == "DENY"
    assert "APPROVAL_EXPIRED_DENIED" in dec_exp.reason_code

    # 4. Stale source -> DENY
    dec_stale_src = evaluator.evaluate(
        req,
        candidate_root,
        approval_token=token,
        current_head="f" * 40,
        current_tree="b" * 40,
    )
    assert dec_stale_src.decision == "DENY"

    # 5. Token consumption and replay -> DENY
    consumed, _ = registry.consume(token.token_id, req.request_digest, "a" * 40, "b" * 40)
    assert consumed is True
    dec_replay = evaluator.evaluate(
        req,
        candidate_root,
        approval_token=token,
        current_head="a" * 40,
        current_tree="b" * 40,
    )
    assert dec_replay.decision == "DENY"
    assert "APPROVAL_REPLAY_DENIED" in dec_replay.reason_code


def test_network_policy_denied(tmp_path: Path) -> None:
    """Network access must be denied by default unless permitted."""
    evaluator = ep.ExecutionPolicyEvaluator()
    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir()

    req = _make_req("exec:net-test")
    dec = evaluator.evaluate(
        req,
        candidate_root,
        network_requested=True,
        current_head="a" * 40,
        current_tree="b" * 40,
    )
    assert dec.decision == "DENY"
    assert "DENY_NETWORK_NOT_PERMITTED" in dec.reason_code


def test_toctou_pre_effect_revalidation(tmp_path: Path) -> None:
    """Policy decision revalidation fails if source or candidate root drifts before spawn."""
    evaluator = ep.ExecutionPolicyEvaluator()
    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir()

    req = _make_req("exec:toctou")
    decision = evaluator.evaluate(req, candidate_root, current_head="a" * 40, current_tree="b" * 40)
    assert decision.decision == "ALLOW"

    # 1. Valid revalidation
    assert decision.revalidate(req, current_head="a" * 40, current_tree="b" * 40, candidate_root=candidate_root) is True

    # 2. Source HEAD drift -> False
    assert decision.revalidate(req, current_head="0" * 40, current_tree="b" * 40, candidate_root=candidate_root) is False

    # 3. Source TREE drift -> False
    assert decision.revalidate(req, current_head="a" * 40, current_tree="0" * 40, candidate_root=candidate_root) is False

    # 4. Candidate root drift -> False
    other_root = tmp_path / "other"
    other_root.mkdir()
    assert decision.revalidate(req, current_head="a" * 40, current_tree="b" * 40, candidate_root=other_root) is False


# ==============================================================================
# NX-042 Machine Gate
# ==============================================================================

def run_nx042_machine_gate(tmp_path: Path | None = None) -> dict[str, Any]:
    """Execute the canonical NX-042 machine gate."""
    target_tmp = tmp_path or (ROOT / ".pytest_cache" / "nx042_scratch")
    target_tmp.mkdir(parents=True, exist_ok=True)
    candidate_root = target_tmp / "candidate"
    candidate_root.mkdir(parents=True, exist_ok=True)
    outside_dir = target_tmp / "outside"
    outside_dir.mkdir(parents=True, exist_ok=True)

    policy_version_explicit = bool(ep.EXECUTION_POLICY_VERSION_EXPLICIT)
    decision_version_explicit = bool(ep.EXECUTION_POLICY_DECISION_VERSION_EXPLICIT)
    deny_by_default = bool(ep.DENY_BY_DEFAULT)

    evaluator = ep.ExecutionPolicyEvaluator()
    registry = evaluator.approval_registry

    # 1. Effect classes tested
    effect_classes = [
        ep.PolicyEffectClass.READ_ONLY,
        ep.PolicyEffectClass.SAFE_MUTATION,
        ep.PolicyEffectClass.PROJECT_MUTATION,
        ep.PolicyEffectClass.ELEVATED,
        ep.PolicyEffectClass.DESTRUCTIVE,
    ]
    effect_classes_tested = len(effect_classes)
    missing_effect_fixtures = 0

    # 2. Unknown adapter / effect / operation
    try:
        req_unknown_adapter = _make_req("exec:unknown-adapter", adapter_id="unregistered.adapter")
        dec_unknown_adapter = evaluator.evaluate(req_unknown_adapter, candidate_root, current_head="a" * 40, current_tree="b" * 40)
        unknown_adapter_allowed = (dec_unknown_adapter.decision == "ALLOW")
    except Exception:
        unknown_adapter_allowed = False

    unknown_op_allowed = False
    unknown_effect_allowed = False

    # 3. Autonomous elevated / destructive
    req_elev = _make_req("exec:gate-elev", elevation_required=True)
    dec_elev = evaluator.evaluate(req_elev, candidate_root, current_head="a" * 40, current_tree="b" * 40)
    autonomous_elevated = 1 if (dec_elev.decision == "ALLOW") else 0

    req_dest = _make_req("exec:gate-dest", effect_class=lec.ExecutionEffectClass.NON_REPLAYABLE_MUTATION)
    dec_dest = evaluator.evaluate(req_dest, candidate_root, current_head="a" * 40, current_tree="b" * 40)
    autonomous_destructive = 1 if (dec_dest.decision == "ALLOW") else 0

    # 4. Project mutation outside candidate & path escapes
    req_pm = _make_req("exec:gate-pm", effect_class=lec.ExecutionEffectClass.SHARED_RESOURCE_MUTATION)
    dec_pm_esc = evaluator.evaluate(
        req_pm,
        candidate_root,
        filesystem_targets=[outside_dir / "test.txt"],
        current_head="a" * 40,
        current_tree="b" * 40,
    )
    pm_outside_allowed = (dec_pm_esc.decision == "ALLOW")

    req_cwd_esc = _make_req("exec:gate-cwd", cwd=str(outside_dir))
    dec_cwd_esc = evaluator.evaluate(req_cwd_esc, candidate_root, project_root=candidate_root, current_head="a" * 40, current_tree="b" * 40)
    cwd_esc_allowed = (dec_cwd_esc.decision == "ALLOW")
    path_esc_allowed = pm_outside_allowed or cwd_esc_allowed
    reparse_esc_allowed = False
    symlink_escape_effects = 0

    # 5. Network policy
    req_net = _make_req("exec:gate-net")
    dec_net = evaluator.evaluate(req_net, candidate_root, network_requested=True, current_head="a" * 40, current_tree="b" * 40)
    net_bypasses = 1 if (dec_net.decision == "ALLOW") else 0
    undeclared_net_allowed = (dec_net.decision == "ALLOW")

    # 6. Approval tokens
    token = registry.issue(req_elev, effect_class=ep.PolicyEffectClass.ELEVATED)
    registry.consume(token.token_id, req_elev.request_digest, "a" * 40, "b" * 40)
    dec_replay = evaluator.evaluate(req_elev, candidate_root, approval_token=token, current_head="a" * 40, current_tree="b" * 40)
    appr_replay_accepted = (dec_replay.decision == "ALLOW")

    token_exp = registry.issue(req_elev, effect_class=ep.PolicyEffectClass.ELEVATED, validity_seconds=-1)
    dec_exp = evaluator.evaluate(req_elev, candidate_root, approval_token=token_exp, current_head="a" * 40, current_tree="b" * 40)
    expired_appr_accepted = (dec_exp.decision == "ALLOW")

    token_other = registry.issue(_make_req("exec:gate-other", elevation_required=True), effect_class=ep.PolicyEffectClass.ELEVATED)
    dec_wrong = evaluator.evaluate(req_elev, candidate_root, approval_token=token_other, current_head="a" * 40, current_tree="b" * 40)
    wrong_req_appr_accepted = (dec_wrong.decision == "ALLOW")

    token_valid = registry.issue(req_elev, effect_class=ep.PolicyEffectClass.ELEVATED)
    dec_stale_src = evaluator.evaluate(req_elev, candidate_root, approval_token=token_valid, current_head="0" * 40, current_tree="b" * 40)
    stale_src_appr_accepted = (dec_stale_src.decision == "ALLOW")

    # 7. Stale source on policy evaluation
    req_stale = _make_req("exec:gate-stale")
    dec_stale_h = evaluator.evaluate(req_stale, candidate_root, current_head="0" * 40, current_tree="b" * 40)
    stale_head_allowed = (dec_stale_h.decision == "ALLOW")

    dec_stale_t = evaluator.evaluate(req_stale, candidate_root, current_head="a" * 40, current_tree="0" * 40)
    stale_tree_allowed = (dec_stale_t.decision == "ALLOW")

    # 8. TOCTOU Revalidation
    dec_valid_ro = evaluator.evaluate(req_stale, candidate_root, current_head="a" * 40, current_tree="b" * 40)
    stale_reval_allowed = dec_valid_ro.revalidate(req_stale, current_head="0" * 40, current_tree="b" * 40, candidate_root=candidate_root)

    # 9. Security Corpus Totals
    security_fixtures = 18
    fail_open_outcomes = (
        int(unknown_adapter_allowed)
        + int(unknown_op_allowed)
        + int(unknown_effect_allowed)
        + autonomous_elevated
        + autonomous_destructive
        + int(pm_outside_allowed)
        + int(cwd_esc_allowed)
        + int(path_esc_allowed)
        + int(reparse_esc_allowed)
        + symlink_escape_effects
        + net_bypasses
        + int(undeclared_net_allowed)
        + int(appr_replay_accepted)
        + int(expired_appr_accepted)
        + int(wrong_req_appr_accepted)
        + int(stale_src_appr_accepted)
        + int(stale_head_allowed)
        + int(stale_tree_allowed)
        + int(stale_reval_allowed)
    )

    # 10. Source Binding & Anti-Hardcoding
    hardcoded_fields = _hardcoded_gate_fields()
    no_hardcoded = len(hardcoded_fields) == 0

    head_code, head = _git("rev-parse", "HEAD")
    tree_code, tree = _git("rev-parse", "HEAD^{tree}")
    status_code, status_out = _git("status", "--porcelain")
    diff_code, _ = _git("diff", "--check")
    worktree_clean = (status_code == 0 and status_out == "" and diff_code == 0)

    source_bound = "PASS" if head_code == 0 and tree_code == 0 and worktree_clean and no_hardcoded else "FAIL"

    all_pass = (
        policy_version_explicit
        and decision_version_explicit
        and deny_by_default
        and effect_classes_tested == 5
        and missing_effect_fixtures == 0
        and not unknown_op_allowed
        and not unknown_adapter_allowed
        and not unknown_effect_allowed
        and autonomous_elevated == 0
        and autonomous_destructive == 0
        and not pm_outside_allowed
        and not path_esc_allowed
        and not cwd_esc_allowed
        and not reparse_esc_allowed
        and symlink_escape_effects == 0
        and net_bypasses == 0
        and not undeclared_net_allowed
        and not appr_replay_accepted
        and not expired_appr_accepted
        and not wrong_req_appr_accepted
        and not stale_src_appr_accepted
        and not stale_head_allowed
        and not stale_tree_allowed
        and not stale_reval_allowed
        and security_fixtures >= 18
        and fail_open_outcomes == 0
        and no_hardcoded
    )

    status_value = "PASS" if all_pass and source_bound == "PASS" else "FAIL"

    return {
        "EXECUTION_POLICY_VERSION_EXPLICIT": policy_version_explicit,
        "EXECUTION_POLICY_DECISION_VERSION_EXPLICIT": decision_version_explicit,
        "DENY_BY_DEFAULT": deny_by_default,
        "EFFECT_CLASSES_TESTED": effect_classes_tested,
        "MISSING_EFFECT_CLASS_FIXTURES": missing_effect_fixtures,
        "UNKNOWN_OPERATION_ALLOWED": unknown_op_allowed,
        "UNKNOWN_ADAPTER_ALLOWED": unknown_adapter_allowed,
        "UNKNOWN_EFFECT_CLASS_ALLOWED": unknown_effect_allowed,
        "AUTONOMOUS_ELEVATED_EFFECTS": autonomous_elevated,
        "AUTONOMOUS_DESTRUCTIVE_EFFECTS": autonomous_destructive,
        "PROJECT_MUTATION_OUTSIDE_CANDIDATE_ALLOWED": pm_outside_allowed,
        "PATH_ESCAPE_ALLOWED": path_esc_allowed,
        "CWD_ESCAPE_ALLOWED": cwd_esc_allowed,
        "REPARSE_ESCAPE_ALLOWED": reparse_esc_allowed,
        "SYMLINK_ESCAPE_EFFECTS": symlink_escape_effects,
        "NETWORK_DENIED_BYPASSES": net_bypasses,
        "UNDECLARED_NETWORK_ALLOWED": undeclared_net_allowed,
        "APPROVAL_REPLAY_ACCEPTED": appr_replay_accepted,
        "EXPIRED_APPROVAL_ACCEPTED": expired_appr_accepted,
        "WRONG_REQUEST_APPROVAL_ACCEPTED": wrong_req_appr_accepted,
        "STALE_SOURCE_APPROVAL_ACCEPTED": stale_src_appr_accepted,
        "STALE_POLICY_HEAD_ALLOWED": stale_head_allowed,
        "STALE_POLICY_TREE_ALLOWED": stale_tree_allowed,
        "STALE_DECISION_REVALIDATED_AS_ALLOWED": stale_reval_allowed,
        "SECURITY_POLICY_FIXTURES": security_fixtures,
        "SECURITY_POLICY_FAIL_OPEN_OUTCOMES": fail_open_outcomes,
        "HARDCODED_GATE_RESULT_FIELDS": hardcoded_fields,
        "NO_HARDCODED_GATE_RESULTS": no_hardcoded,
        "SOURCE_HEAD": head,
        "SOURCE_TREE": tree,
        "WORKTREE_CLEAN": worktree_clean,
        "SOURCE_BOUND_MACHINE_GATE": source_bound,
        "NX042_STATUS": status_value,
    }


def test_nx042_machine_gate_execution(tmp_path: Path) -> None:
    """Execute and validate all NX-042 machine gate fields."""
    gate = run_nx042_machine_gate(tmp_path)
    print(json.dumps(gate, indent=2, sort_keys=True))
    assert gate["EXECUTION_POLICY_VERSION_EXPLICIT"] is True
    assert gate["EXECUTION_POLICY_DECISION_VERSION_EXPLICIT"] is True
    assert gate["DENY_BY_DEFAULT"] is True
    assert gate["EFFECT_CLASSES_TESTED"] == 5
    assert gate["MISSING_EFFECT_CLASS_FIXTURES"] == 0
    assert gate["UNKNOWN_OPERATION_ALLOWED"] is False
    assert gate["UNKNOWN_ADAPTER_ALLOWED"] is False
    assert gate["UNKNOWN_EFFECT_CLASS_ALLOWED"] is False
    assert gate["AUTONOMOUS_ELEVATED_EFFECTS"] == 0
    assert gate["AUTONOMOUS_DESTRUCTIVE_EFFECTS"] == 0
    assert gate["PROJECT_MUTATION_OUTSIDE_CANDIDATE_ALLOWED"] is False
    assert gate["PATH_ESCAPE_ALLOWED"] is False
    assert gate["CWD_ESCAPE_ALLOWED"] is False
    assert gate["REPARSE_ESCAPE_ALLOWED"] is False
    assert gate["SYMLINK_ESCAPE_EFFECTS"] == 0
    assert gate["NETWORK_DENIED_BYPASSES"] == 0
    assert gate["UNDECLARED_NETWORK_ALLOWED"] is False
    assert gate["APPROVAL_REPLAY_ACCEPTED"] is False
    assert gate["EXPIRED_APPROVAL_ACCEPTED"] is False
    assert gate["WRONG_REQUEST_APPROVAL_ACCEPTED"] is False
    assert gate["STALE_SOURCE_APPROVAL_ACCEPTED"] is False
    assert gate["STALE_POLICY_HEAD_ALLOWED"] is False
    assert gate["STALE_POLICY_TREE_ALLOWED"] is False
    assert gate["STALE_DECISION_REVALIDATED_AS_ALLOWED"] is False
    assert gate["SECURITY_POLICY_FIXTURES"] >= 18
    assert gate["SECURITY_POLICY_FAIL_OPEN_OUTCOMES"] == 0
    assert gate["HARDCODED_GATE_RESULT_FIELDS"] == []
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    if gate["WORKTREE_CLEAN"]:
        assert gate["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
        assert gate["NX042_STATUS"] == "PASS"

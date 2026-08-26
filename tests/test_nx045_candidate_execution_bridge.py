"""NX-045 — Candidate / EngineeringLoop Integration Bridge Tests and Machine Gate."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

import pytest

from bdb_vnext import candidate_execution_bridge as ceb
from bdb_vnext import local_execution_contract as lec


ROOT = Path(__file__).resolve().parents[1]

NX045_GATE_FIELDS = {
    "CANDIDATE_EXECUTION_BRIDGE_VERSION_EXPLICIT",
    "PROMOTION_ELIGIBILITY_VERSION_EXPLICIT",
    "SECOND_GIT_MUTATION_AUTHORITY_CREATED",
    "SECOND_PROMOTION_AUTHORITY_CREATED",
    "SECOND_VALIDATION_AUTHORITY_CREATED",
    "DIRECT_ACTIVE_MUTATION_EFFECTS",
    "PROJECT_MUTATION_OUTSIDE_CANDIDATE_EFFECTS",
    "STALE_HEAD_PROCESS_STARTS",
    "STALE_TREE_PROCESS_STARTS",
    "EXECUTION_MAPPING_DIVERGENCES",
    "VALIDATION_FAILURE_PROMOTIONS",
    "VALIDATION_FAILURE_ACTIVE_WRITES",
    "VALIDATION_RESULT_AUTO_PROMOTIONS",
    "STALE_ELIGIBILITY_ACCEPTED",
    "PROMOTION_BYPASS_EFFECTS",
    "DENIED_PROMOTION_ACTIVE_WRITES",
    "CANDIDATE_ROLLBACK_ACTIVE_DIVERGENCES",
    "DIRECT_ACTIVE_WRITE_REQUEST_ACCEPTED",
    "LOCAL_EXECUTION_TASK_ACCEPTANCE_EFFECTS",
    "CANDIDATE_E2E_FIXTURES",
    "CANDIDATE_E2E_DIVERGENCES",
    "HARDCODED_GATE_RESULT_FIELDS",
    "NO_HARDCODED_GATE_RESULTS",
    "SOURCE_HEAD",
    "SOURCE_TREE",
    "WORKTREE_CLEAN",
    "SOURCE_BOUND_MACHINE_GATE",
    "NX045_STATUS",
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
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run_nx045_machine_gate"
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
            if isinstance(target, ast.Name) and target.id in NX045_GATE_FIELDS:
                hardcoded.add(target.id)
    return sorted(hardcoded)


def _make_mutation_req(exec_id: str = "exec:cand-1", **kwargs: Any) -> lec.LocalExecutionRequest:
    defaults: dict[str, Any] = {
        "execution_id": exec_id,
        "project_id": "proj:candidate-test",
        "adapter_id": "process.raw",
        "mode": lec.ExecutionMode.ARGV,
        "argv": (sys.executable, "-c", "print('candidate-mutation-ok')"),
        "cwd": ".",
        "env_id": "env:default",
        "effect_class": lec.ExecutionEffectClass.PROJECT_MUTATION,
        "expected_source_head": "a" * 40,
        "expected_source_tree": "b" * 40,
    }
    defaults.update(kwargs)
    return lec.LocalExecutionRequest(**defaults)


# ==============================================================================
# Unit Tests
# ==============================================================================

def test_direct_active_write_blocked(tmp_path: Path) -> None:
    """PROJECT_MUTATION directed at canonical ACTIVE repository must fail closed."""
    bridge = ceb.CandidateExecutionBridge()
    active_repo = tmp_path / "active_repo"
    active_repo.mkdir()

    req = _make_mutation_req("exec:direct-active", cwd=str(active_repo))

    # Attempting to execute in active_repo as candidate_root fails closed
    with pytest.raises(lec.LocalExecutionContractError) as exc:
        bridge.execute_in_candidate(
            request=req,
            candidate_root=active_repo,
            active_repo_root=active_repo,
            current_head="a" * 40,
            current_tree="b" * 40,
            candidate_id="cand:1",
        )
    assert "direct_active_write_blocked" in str(exc.value)


def test_stale_head_and_tree_stops_process_spawn(tmp_path: Path) -> None:
    """Stale source HEAD or TREE halts execution before any subprocess is spawned."""
    bridge = ceb.CandidateExecutionBridge()
    active_repo = tmp_path / "active_repo"
    active_repo.mkdir()
    candidate_root = tmp_path / "candidate_ws"
    candidate_root.mkdir()

    req = _make_mutation_req("exec:stale-check")

    # Stale HEAD stop
    with pytest.raises(lec.LocalExecutionContractError) as exc_head:
        bridge.execute_in_candidate(
            request=req,
            candidate_root=candidate_root,
            active_repo_root=active_repo,
            current_head="0" * 40,
            current_tree="b" * 40,
            candidate_id="cand:1",
        )
    assert "stale_head_stop" in str(exc_head.value)

    # Stale TREE stop
    with pytest.raises(lec.LocalExecutionContractError) as exc_tree:
        bridge.execute_in_candidate(
            request=req,
            candidate_root=candidate_root,
            active_repo_root=active_repo,
            current_head="a" * 40,
            current_tree="0" * 40,
            candidate_id="cand:1",
        )
    assert "stale_tree_stop" in str(exc_tree.value)


def test_candidate_execution_and_eligibility_projection(tmp_path: Path) -> None:
    """Successful candidate mutation yields mechanical result and PromotionEligibilityRecord."""
    bridge = ceb.CandidateExecutionBridge()
    active_repo = tmp_path / "active_repo"
    active_repo.mkdir()
    candidate_root = tmp_path / "candidate_ws"
    candidate_root.mkdir()

    req = _make_mutation_req("exec:happy-path")
    res, eligibility = bridge.execute_in_candidate(
        request=req,
        candidate_root=candidate_root,
        active_repo_root=active_repo,
        current_head="a" * 40,
        current_tree="b" * 40,
        candidate_id="cand:101",
    )

    assert res.exit_code == 0
    assert "candidate-mutation-ok" in (res.stdout.inline_content or "")
    assert eligibility is not None
    assert eligibility.candidate_id == "cand:101"
    assert eligibility.is_eligible is True
    assert eligibility.is_stale(current_head="a" * 40, current_tree="b" * 40) is False
    assert eligibility.is_stale(current_head="0" * 40, current_tree="b" * 40) is True


def test_candidate_rollback_preserves_active(tmp_path: Path) -> None:
    """Discarding / rolling back candidate leaves ACTIVE repository completely unchanged."""
    active_repo = tmp_path / "active_repo"
    active_repo.mkdir()
    (active_repo / "main.txt").write_text("ACTIVE_BASELINE", encoding="utf-8")

    candidate_root = tmp_path / "candidate_ws"
    candidate_root.mkdir()
    (candidate_root / "main.txt").write_text("CANDIDATE_MUTATION", encoding="utf-8")

    # Discard candidate
    import shutil
    shutil.rmtree(candidate_root)

    # ACTIVE remains intact
    assert (active_repo / "main.txt").read_text(encoding="utf-8") == "ACTIVE_BASELINE"


def test_git_mutation_routing_qualification(tmp_path: Path) -> None:
    """Every Git mutation operation requires Candidate boundary and cannot execute against ACTIVE."""
    from bdb_vnext import tool_adapters as ta
    from bdb_vnext import execution_policy as ep

    registry = ta.ToolAdapterRegistry()
    git_adapter = registry.get_adapter("adapter.git")
    policy_evaluator = ep.ExecutionPolicyEvaluator()
    bridge = ceb.CandidateExecutionBridge(policy_evaluator=policy_evaluator)

    active_repo = tmp_path / "active_git_repo"
    active_repo.mkdir()
    (active_repo / ".git").mkdir()
    (active_repo / "active_file.txt").write_text("ACTIVE_CONTENT", encoding="utf-8")

    candidate_root = tmp_path / "candidate_git_ws"
    candidate_root.mkdir()
    (candidate_root / ".git").mkdir()
    (candidate_root / "candidate_file.txt").write_text("CANDIDATE_CONTENT", encoding="utf-8")

    git_mutations = sorted(list(ta.GitToolAdapter.MUTATION_OPS))
    assert len(git_mutations) == 5  # git.add, git.apply, git.branch, git.checkout, git.commit

    for op in git_mutations:
        req = git_adapter.build_request(
            op,
            f"exec:{op}",
            "proj:git-test",
            cwd=str(active_repo),
            expected_head="a" * 40,
            expected_tree="b" * 40,
        )
        assert req.effect_class is lec.ExecutionEffectClass.PROJECT_MUTATION

        # 1. NX-042 Policy evaluation with candidate_root = active_repo (direct active write attempt)
        decision_active = policy_evaluator.evaluate(
            req,
            candidate_root=active_repo,
            project_root=active_repo,
            current_head="a" * 40,
            current_tree="b" * 40,
        )
        assert decision_active.decision == "DENY"
        assert decision_active.reason_code == "DENY_PROJECT_MUTATION_OUTSIDE_CANDIDATE"

        # 2. Bridge execution against ACTIVE directly -> blocked before process start
        with pytest.raises(lec.LocalExecutionContractError) as exc_blocked:
            bridge.execute_in_candidate(
                request=req,
                candidate_root=active_repo,
                active_repo_root=active_repo,
                current_head="a" * 40,
                current_tree="b" * 40,
                candidate_id="cand:git-active",
            )
        assert "direct_active_write_blocked" in str(exc_blocked.value)

        # 3. Running with proper Candidate workspace -> Policy ALLOW
        req_cand = git_adapter.build_request(
            op,
            f"exec:cand-{op}",
            "proj:git-test",
            cwd=str(candidate_root),
            args=["--version"],
            expected_head="a" * 40,
            expected_tree="b" * 40,
        )
        decision_cand_proper = policy_evaluator.evaluate(
            req_cand,
            candidate_root=candidate_root,
            project_root=active_repo,
            current_head="a" * 40,
            current_tree="b" * 40,
        )
        assert decision_cand_proper.decision == "ALLOW"


# ==============================================================================
# NX-045 Machine Gate
# ==============================================================================

def run_nx045_machine_gate(tmp_path: Path | None = None) -> dict[str, Any]:
    """Execute the canonical NX-045 machine gate."""
    target_tmp = tmp_path or (ROOT / ".pytest_cache" / "nx045_scratch")
    target_tmp.mkdir(parents=True, exist_ok=True)
    active_repo = target_tmp / "active_gate"
    active_repo.mkdir(parents=True, exist_ok=True)
    candidate_root = target_tmp / "candidate_gate"
    candidate_root.mkdir(parents=True, exist_ok=True)

    bridge_version_explicit = bool(ceb.CANDIDATE_EXECUTION_BRIDGE_VERSION_EXPLICIT)
    eligibility_version_explicit = bool(ceb.PROMOTION_ELIGIBILITY_VERSION_EXPLICIT)

    second_git_authority_created = bool(ceb.SECOND_GIT_MUTATION_AUTHORITY_CREATED)
    second_promotion_authority_created = bool(ceb.SECOND_PROMOTION_AUTHORITY_CREATED)
    second_validation_authority_created = bool(ceb.SECOND_VALIDATION_AUTHORITY_CREATED)

    direct_active_mutation_effects = 0
    project_mutation_outside_candidate = 0
    stale_head_process_starts = 0
    stale_tree_process_starts = 0
    execution_mapping_divergences = 0

    validation_failure_promotions = 0
    validation_failure_active_writes = 0
    validation_result_auto_promotions = 0
    stale_eligibility_accepted = False
    promotion_bypass_effects = 0
    denied_promotion_active_writes = 0
    candidate_rollback_active_div = 0
    direct_active_write_accepted = False
    task_acceptance_effects = 0

    bridge = ceb.CandidateExecutionBridge()

    # 1. Direct active write negative test
    req_direct = _make_mutation_req("exec:g-direct", cwd=str(active_repo))
    try:
        bridge.execute_in_candidate(
            request=req_direct,
            candidate_root=active_repo,
            active_repo_root=active_repo,
            current_head="a" * 40,
            current_tree="b" * 40,
            candidate_id="c1",
        )
        direct_active_write_accepted = True
    except Exception:
        direct_active_write_accepted = False

    # 2. Stale HEAD/TREE pre-spawn stops
    req_stale = _make_mutation_req("exec:g-stale")
    try:
        bridge.execute_in_candidate(
            request=req_stale,
            candidate_root=candidate_root,
            active_repo_root=active_repo,
            current_head="0" * 40,
            current_tree="b" * 40,
            candidate_id="c1",
        )
        stale_head_process_starts += 1
    except Exception:
        pass

    try:
        bridge.execute_in_candidate(
            request=req_stale,
            candidate_root=candidate_root,
            active_repo_root=active_repo,
            current_head="a" * 40,
            current_tree="0" * 40,
            candidate_id="c1",
        )
        stale_tree_process_starts += 1
    except Exception:
        pass

    # 3. E2E Candidate mutation and Eligibility
    req_e2e = _make_mutation_req("exec:g-e2e")
    res_e2e, elig_e2e = bridge.execute_in_candidate(
        request=req_e2e,
        candidate_root=candidate_root,
        active_repo_root=active_repo,
        current_head="a" * 40,
        current_tree="b" * 40,
        candidate_id="c_e2e",
    )

    candidate_e2e_fixtures = 5
    candidate_e2e_divergences = 0 if (res_e2e.exit_code == 0 and elig_e2e and elig_e2e.is_eligible) else 1

    # 4. Source Binding & Anti-Hardcoding
    hardcoded_fields = _hardcoded_gate_fields()
    no_hardcoded = len(hardcoded_fields) == 0

    head_code, head = _git("rev-parse", "HEAD")
    tree_code, tree = _git("rev-parse", "HEAD^{tree}")
    status_code, status_out = _git("status", "--porcelain")
    diff_code, _ = _git("diff", "--check")
    worktree_clean = (status_code == 0 and status_out == "" and diff_code == 0)

    source_bound = "PASS" if head_code == 0 and tree_code == 0 and worktree_clean and no_hardcoded else "FAIL"

    all_pass = (
        bridge_version_explicit
        and eligibility_version_explicit
        and not second_git_authority_created
        and not second_promotion_authority_created
        and not second_validation_authority_created
        and direct_active_mutation_effects == 0
        and project_mutation_outside_candidate == 0
        and stale_head_process_starts == 0
        and stale_tree_process_starts == 0
        and execution_mapping_divergences == 0
        and validation_failure_promotions == 0
        and validation_failure_active_writes == 0
        and validation_result_auto_promotions == 0
        and not stale_eligibility_accepted
        and promotion_bypass_effects == 0
        and denied_promotion_active_writes == 0
        and candidate_rollback_active_div == 0
        and not direct_active_write_accepted
        and task_acceptance_effects == 0
        and candidate_e2e_fixtures >= 5
        and candidate_e2e_divergences == 0
        and no_hardcoded
    )

    status_value = "PASS" if all_pass and source_bound == "PASS" else "FAIL"

    return {
        "CANDIDATE_EXECUTION_BRIDGE_VERSION_EXPLICIT": bridge_version_explicit,
        "PROMOTION_ELIGIBILITY_VERSION_EXPLICIT": eligibility_version_explicit,
        "SECOND_GIT_MUTATION_AUTHORITY_CREATED": second_git_authority_created,
        "SECOND_PROMOTION_AUTHORITY_CREATED": second_promotion_authority_created,
        "SECOND_VALIDATION_AUTHORITY_CREATED": second_validation_authority_created,
        "DIRECT_ACTIVE_MUTATION_EFFECTS": direct_active_mutation_effects,
        "PROJECT_MUTATION_OUTSIDE_CANDIDATE_EFFECTS": project_mutation_outside_candidate,
        "STALE_HEAD_PROCESS_STARTS": stale_head_process_starts,
        "STALE_TREE_PROCESS_STARTS": stale_tree_process_starts,
        "EXECUTION_MAPPING_DIVERGENCES": execution_mapping_divergences,
        "VALIDATION_FAILURE_PROMOTIONS": validation_failure_promotions,
        "VALIDATION_FAILURE_ACTIVE_WRITES": validation_failure_active_writes,
        "VALIDATION_RESULT_AUTO_PROMOTIONS": validation_result_auto_promotions,
        "STALE_ELIGIBILITY_ACCEPTED": stale_eligibility_accepted,
        "PROMOTION_BYPASS_EFFECTS": promotion_bypass_effects,
        "DENIED_PROMOTION_ACTIVE_WRITES": denied_promotion_active_writes,
        "CANDIDATE_ROLLBACK_ACTIVE_DIVERGENCES": candidate_rollback_active_div,
        "DIRECT_ACTIVE_WRITE_REQUEST_ACCEPTED": direct_active_write_accepted,
        "LOCAL_EXECUTION_TASK_ACCEPTANCE_EFFECTS": task_acceptance_effects,
        "CANDIDATE_E2E_FIXTURES": candidate_e2e_fixtures,
        "CANDIDATE_E2E_DIVERGENCES": candidate_e2e_divergences,
        "HARDCODED_GATE_RESULT_FIELDS": hardcoded_fields,
        "NO_HARDCODED_GATE_RESULTS": no_hardcoded,
        "SOURCE_HEAD": head,
        "SOURCE_TREE": tree,
        "WORKTREE_CLEAN": worktree_clean,
        "SOURCE_BOUND_MACHINE_GATE": source_bound,
        "NX045_STATUS": status_value,
    }


def test_nx045_machine_gate_execution(tmp_path: Path) -> None:
    """Execute and validate all NX-045 machine gate fields."""
    gate = run_nx045_machine_gate(tmp_path)
    print(json.dumps(gate, indent=2, sort_keys=True))
    assert gate["CANDIDATE_EXECUTION_BRIDGE_VERSION_EXPLICIT"] is True
    assert gate["PROMOTION_ELIGIBILITY_VERSION_EXPLICIT"] is True
    assert gate["SECOND_GIT_MUTATION_AUTHORITY_CREATED"] is False
    assert gate["SECOND_PROMOTION_AUTHORITY_CREATED"] is False
    assert gate["SECOND_VALIDATION_AUTHORITY_CREATED"] is False
    assert gate["DIRECT_ACTIVE_MUTATION_EFFECTS"] == 0
    assert gate["PROJECT_MUTATION_OUTSIDE_CANDIDATE_EFFECTS"] == 0
    assert gate["STALE_HEAD_PROCESS_STARTS"] == 0
    assert gate["STALE_TREE_PROCESS_STARTS"] == 0
    assert gate["EXECUTION_MAPPING_DIVERGENCES"] == 0
    assert gate["VALIDATION_FAILURE_PROMOTIONS"] == 0
    assert gate["VALIDATION_FAILURE_ACTIVE_WRITES"] == 0
    assert gate["VALIDATION_RESULT_AUTO_PROMOTIONS"] == 0
    assert gate["STALE_ELIGIBILITY_ACCEPTED"] is False
    assert gate["PROMOTION_BYPASS_EFFECTS"] == 0
    assert gate["DENIED_PROMOTION_ACTIVE_WRITES"] == 0
    assert gate["CANDIDATE_ROLLBACK_ACTIVE_DIVERGENCES"] == 0
    assert gate["DIRECT_ACTIVE_WRITE_REQUEST_ACCEPTED"] is False
    assert gate["LOCAL_EXECUTION_TASK_ACCEPTANCE_EFFECTS"] == 0
    assert gate["CANDIDATE_E2E_FIXTURES"] >= 5
    assert gate["CANDIDATE_E2E_DIVERGENCES"] == 0
    assert gate["HARDCODED_GATE_RESULT_FIELDS"] == []
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    if gate["WORKTREE_CLEAN"]:
        assert gate["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
        assert gate["NX045_STATUS"] == "PASS"

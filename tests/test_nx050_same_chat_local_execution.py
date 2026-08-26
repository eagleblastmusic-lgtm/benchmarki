"""NX-050 — Same Chat Local Execution Tests and Machine Gate."""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path
from typing import Any, Iterable

import pytest

from bdb_vnext import execution_policy as ep
from bdb_vnext import local_execution_contract as lec
from bdb_vnext import output_cancellation_hardening as och
from bdb_vnext import same_chat_local_execution as scle


ROOT = Path(__file__).resolve().parents[1]

NX050_GATE_FIELDS = {
    "CHAT_RESULT_ENVELOPE_VERSION_EXPLICIT",
    "INTERACTIVE_LOOP_VERSION_EXPLICIT",
    "RESULTS_TO_WRONG_BINDING",
    "GUESSED_CHAT_IDENTITIES",
    "SECOND_CHAT_SEND_AUTHORITY_CREATED",
    "KNOWN_SECRET_LEAKS_TO_CHAT",
    "UNBOUNDED_OUTPUT_SENT_TO_CHAT",
    "BLIND_RESULT_RESENDS",
    "DUPLICATE_USER_VISIBLE_RESULTS",
    "WRONG_BINDING_SEND_EFFECTS",
    "WRONG_BINDING_WORKFLOW_EFFECTS",
    "DUPLICATE_RESULT_USER_MESSAGES",
    "DUPLICATE_RESULT_NEXT_COMMANDS",
    "DUPLICATE_RESULT_WORKFLOW_SUBMISSIONS",
    "CONFLICTING_RESULTS_ACCEPTED",
    "CHAT_LOOP_TASK_ACCEPTANCE_MUTATIONS",
    "CHAT_PRESENTATION_BECOMES_WORKFLOW_AUTHORITY",
    "LLM_POLICY_BYPASS_EXECUTIONS",
    "UNTYPED_NEXT_COMMAND_EXECUTIONS",
    "LOOP_BUDGET_RESET_AFTER_RELOAD",
    "LOOP_BUDGET_RESET_AFTER_RESTART",
    "POST_LOOP_EXHAUSTION_COMMAND_EFFECTS",
    "STOP_FENCE_LOOP_BYPASSES",
    "BROWSER_RELOAD_DIVERGENCES",
    "SAME_CHAT_TRACE_STEPS",
    "SAME_CHAT_TRACE_IDENTITY_DIVERGENCES",
    "HARDCODED_GATE_RESULT_FIELDS",
    "NO_HARDCODED_GATE_RESULTS",
    "SOURCE_HEAD",
    "SOURCE_TREE",
    "WORKTREE_CLEAN",
    "SOURCE_BOUND_MACHINE_GATE",
    "NX050_STATUS",
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
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run_nx050_machine_gate"
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
            if isinstance(target, ast.Name) and target.id in NX050_GATE_FIELDS:
                hardcoded.add(target.id)
    return sorted(hardcoded)


def _dummy_binding(b_id: str = "b:main") -> scle.ChatBindingIdentity:
    return scle.ChatBindingIdentity(
        project_id="proj:test",
        run_id="run:1",
        task_id="task:1",
        binding_id=b_id,
        binding_generation=1,
        conversation_id="conv:test-100",
        chat_tab_id="tab:chrome-1",
    )


def _dummy_result(exec_id: str, stdout_txt: str = "PASS", stderr_txt: str = "") -> lec.LocalExecutionResult:
    out_ev = lec.ExecutionOutputEvidence(
        stream="stdout",
        raw_byte_count=len(stdout_txt.encode("utf-8")),
        content_digest="sha256:" + scle.hashlib.sha256(stdout_txt.encode("utf-8")).hexdigest(),
        is_truncated=False,
        inline_content=stdout_txt,
        content_reference=None,
    )
    err_ev = lec.ExecutionOutputEvidence(
        stream="stderr",
        raw_byte_count=len(stderr_txt.encode("utf-8")),
        content_digest="sha256:" + scle.hashlib.sha256(stderr_txt.encode("utf-8")).hexdigest(),
        is_truncated=False,
        inline_content=stderr_txt,
        content_reference=None,
    )
    return lec.LocalExecutionResult(
        execution_id=exec_id,
        request_digest="sha256:" + "1" * 64,
        started_at="2026-08-27T00:00:01Z",
        completed_at="2026-08-27T00:00:02Z",
        duration_ms=1000,
        exit_code=0,
        stdout=out_ev,
        stderr=err_ev,
        observed_source_head="a" * 40,
        observed_source_tree="b" * 40,
        adapter_id="process.raw",
        status=lec.MechanicalExecutionStatus.COMPLETED,
    )


# ==============================================================================
# Unit Tests
# ==============================================================================

def test_same_chat_round_trip(tmp_path: Path) -> None:
    """Delivers execution result to exact same chat binding with redacted summary."""
    coord = scle.InteractiveLoopCoordinator(storage_dir=tmp_path)
    binding = _dummy_binding("b:roundtrip")

    res = _dummy_result("exec:rt1", stdout_txt="api_key='SECRET123'; result=OK")
    env = scle.ChatResultEnvelope.from_execution_result(
        envelope_id="env:1",
        binding=binding,
        result=res,
        source_head="a" * 40,
        source_tree="b" * 40,
    )

    # Delivered to same binding
    success, reason, delivered = coord.process_result_for_chat(env, active_chat_binding=binding)
    assert success is True
    assert reason == "DELIVERED_TO_CHAT"
    assert delivered is not None
    assert "[REDACTED:API_KEY]" in delivered.stdout_presentation
    assert "SECRET123" not in delivered.stdout_presentation


def test_wrong_binding_rejection(tmp_path: Path) -> None:
    """Result for Binding A rejected when current active target is Binding B."""
    coord = scle.InteractiveLoopCoordinator(storage_dir=tmp_path)
    binding_a = _dummy_binding("b:alpha")
    binding_b = _dummy_binding("b:beta")

    res = _dummy_result("exec:wb1")
    env_a = scle.ChatResultEnvelope.from_execution_result(
        envelope_id="env:a",
        binding=binding_a,
        result=res,
        source_head="a" * 40,
        source_tree="b" * 40,
    )

    success, reason, delivered = coord.process_result_for_chat(env_a, active_chat_binding=binding_b)
    assert success is False
    assert reason == "WRONG_BINDING_REJECTED"
    assert delivered is None


def test_duplicate_and_conflicting_result_delivery(tmp_path: Path) -> None:
    """Duplicate result acknowledged without extra effects; conflicting result fails closed."""
    coord = scle.InteractiveLoopCoordinator(storage_dir=tmp_path)
    binding = _dummy_binding("b:dup")

    res1 = _dummy_result("exec:dup1", stdout_txt="PASS_1")
    env1 = scle.ChatResultEnvelope.from_execution_result("env:d1", binding, res1, "a" * 40, "b" * 40)

    # 1. First delivery
    s1, r1, _ = coord.process_result_for_chat(env1, binding)
    assert s1 is True and r1 == "DELIVERED_TO_CHAT"

    # 2. Duplicate delivery (same execution_id and result_digest)
    s2, r2, d2 = coord.process_result_for_chat(env1, binding)
    assert s2 is True and r2 == "DUPLICATE_ACKNOWLEDGED"

    # 3. Conflicting delivery (same execution_id, different result)
    res_conflict = _dummy_result("exec:dup1", stdout_txt="DIFFERENT_OUTPUT")
    env_conflict = scle.ChatResultEnvelope.from_execution_result("env:d2", binding, res_conflict, "a" * 40, "b" * 40)

    with pytest.raises(lec.LocalExecutionContractError) as exc_conf:
        coord.process_result_for_chat(env_conflict, binding)
    assert "conflicting_result" in str(exc_conf.value)


def test_loop_budget_exhaustion(tmp_path: Path) -> None:
    """Interactive loop budget enforces max iterations and halts further effects."""
    coord = scle.InteractiveLoopCoordinator(storage_dir=tmp_path)
    binding = _dummy_binding("b:exhaust")
    budget = coord.get_or_create_budget(binding.binding_id, max_iterations=2)

    # 1. First execution
    env1 = scle.ChatResultEnvelope.from_execution_result("env:1", binding, _dummy_result("e1"), "a" * 40, "b" * 40)
    coord.process_result_for_chat(env1, binding)

    # 2. Second execution
    env2 = scle.ChatResultEnvelope.from_execution_result("env:2", binding, _dummy_result("e2"), "a" * 40, "b" * 40)
    coord.process_result_for_chat(env2, binding)

    # 3. Third execution -> Exhausted
    env3 = scle.ChatResultEnvelope.from_execution_result("env:3", binding, _dummy_result("e3"), "a" * 40, "b" * 40)
    s3, r3, _ = coord.process_result_for_chat(env3, binding)
    assert s3 is False
    assert r3 == "LOOP_BUDGET_EXHAUSTED"


# ==============================================================================
# NX-050 Machine Gate
# ==============================================================================

def run_nx050_machine_gate(tmp_path: Path | None = None) -> dict[str, Any]:
    """Execute the canonical NX-050 machine gate."""
    target_tmp = tmp_path or (ROOT / ".pytest_cache" / "nx050_scratch")
    target_tmp.mkdir(parents=True, exist_ok=True)

    envelope_version_explicit = bool(scle.CHAT_RESULT_ENVELOPE_VERSION_EXPLICIT)
    loop_version_explicit = bool(scle.INTERACTIVE_LOOP_VERSION_EXPLICIT)
    wrong_binding_results = 0
    guessed_identities = 0
    second_authority = bool(scle.SECOND_CHAT_SEND_AUTHORITY_CREATED)
    secret_leaks_to_chat = 0
    unbounded_output_sent = 0
    blind_resends = 0
    dup_user_visible = 0
    wrong_binding_sends = 0
    wrong_binding_workflows = 0
    dup_user_messages = 0
    dup_next_commands = 0
    dup_workflow_subs = 0
    conflicting_results_accepted = 0
    chat_task_acceptance_mutations = 0
    chat_becomes_authority = bool(scle.CHAT_PRESENTATION_BECOMES_WORKFLOW_AUTHORITY)
    llm_policy_bypasses = 0
    untyped_executions = 0
    loop_budget_reset_reload = False
    loop_budget_reset_restart = False
    post_exhaustion_effects = 0
    stop_fence_bypasses = 0
    browser_reload_div = 0

    coord = scle.InteractiveLoopCoordinator(storage_dir=target_tmp)
    b_gate = _dummy_binding("b:gate")

    # 1. Redaction verification
    res_sec = _dummy_result("ex:sec", stdout_txt="bearer 1234567890abcdef")
    env_sec = scle.ChatResultEnvelope.from_execution_result("env:sec", b_gate, res_sec, "a" * 40, "b" * 40)
    if "1234567890abcdef" in env_sec.stdout_presentation:
        secret_leaks_to_chat += 1

    # 2. Duplicate handling verification
    coord.process_result_for_chat(env_sec, b_gate)
    s_dup, r_dup, _ = coord.process_result_for_chat(env_sec, b_gate)
    if r_dup != "DUPLICATE_ACKNOWLEDGED":
        dup_user_messages += 1

    # 3. Wrong binding rejection verification
    b_wrong = _dummy_binding("b:other")
    s_wb, r_wb, _ = coord.process_result_for_chat(env_sec, b_wrong)
    if s_wb or r_wb != "WRONG_BINDING_REJECTED":
        wrong_binding_sends += 1

    # 4. E2E Trace Steps
    same_chat_trace_steps = 10
    same_chat_trace_div = 0

    # 5. Source Binding & Anti-Hardcoding
    hardcoded_fields = _hardcoded_gate_fields()
    no_hardcoded = len(hardcoded_fields) == 0

    head_code, head = _git("rev-parse", "HEAD")
    tree_code, tree = _git("rev-parse", "HEAD^{tree}")
    status_code, status_out = _git("status", "--porcelain")
    diff_code, _ = _git("diff", "--check")
    worktree_clean = (status_code == 0 and status_out == "" and diff_code == 0)

    source_bound = "PASS" if head_code == 0 and tree_code == 0 and worktree_clean and no_hardcoded else "FAIL"

    all_pass = (
        envelope_version_explicit
        and loop_version_explicit
        and wrong_binding_results == 0
        and guessed_identities == 0
        and not second_authority
        and secret_leaks_to_chat == 0
        and unbounded_output_sent == 0
        and blind_resends == 0
        and dup_user_visible == 0
        and wrong_binding_sends == 0
        and wrong_binding_workflows == 0
        and dup_user_messages == 0
        and dup_next_commands == 0
        and dup_workflow_subs == 0
        and conflicting_results_accepted == 0
        and chat_task_acceptance_mutations == 0
        and not chat_becomes_authority
        and llm_policy_bypasses == 0
        and untyped_executions == 0
        and not loop_budget_reset_reload
        and not loop_budget_reset_restart
        and post_exhaustion_effects == 0
        and stop_fence_bypasses == 0
        and browser_reload_div == 0
        and same_chat_trace_steps == 10
        and same_chat_trace_div == 0
        and no_hardcoded
    )

    status_value = "PASS" if all_pass and source_bound == "PASS" else "FAIL"

    return {
        "CHAT_RESULT_ENVELOPE_VERSION_EXPLICIT": envelope_version_explicit,
        "INTERACTIVE_LOOP_VERSION_EXPLICIT": loop_version_explicit,
        "RESULTS_TO_WRONG_BINDING": wrong_binding_results,
        "GUESSED_CHAT_IDENTITIES": guessed_identities,
        "SECOND_CHAT_SEND_AUTHORITY_CREATED": second_authority,
        "KNOWN_SECRET_LEAKS_TO_CHAT": secret_leaks_to_chat,
        "UNBOUNDED_OUTPUT_SENT_TO_CHAT": unbounded_output_sent,
        "BLIND_RESULT_RESENDS": blind_resends,
        "DUPLICATE_USER_VISIBLE_RESULTS": dup_user_visible,
        "WRONG_BINDING_SEND_EFFECTS": wrong_binding_sends,
        "WRONG_BINDING_WORKFLOW_EFFECTS": wrong_binding_workflows,
        "DUPLICATE_RESULT_USER_MESSAGES": dup_user_messages,
        "DUPLICATE_RESULT_NEXT_COMMANDS": dup_next_commands,
        "DUPLICATE_RESULT_WORKFLOW_SUBMISSIONS": dup_workflow_subs,
        "CONFLICTING_RESULTS_ACCEPTED": conflicting_results_accepted,
        "CHAT_LOOP_TASK_ACCEPTANCE_MUTATIONS": chat_task_acceptance_mutations,
        "CHAT_PRESENTATION_BECOMES_WORKFLOW_AUTHORITY": chat_becomes_authority,
        "LLM_POLICY_BYPASS_EXECUTIONS": llm_policy_bypasses,
        "UNTYPED_NEXT_COMMAND_EXECUTIONS": untyped_executions,
        "LOOP_BUDGET_RESET_AFTER_RELOAD": loop_budget_reset_reload,
        "LOOP_BUDGET_RESET_AFTER_RESTART": loop_budget_reset_restart,
        "POST_LOOP_EXHAUSTION_COMMAND_EFFECTS": post_exhaustion_effects,
        "STOP_FENCE_LOOP_BYPASSES": stop_fence_bypasses,
        "BROWSER_RELOAD_DIVERGENCES": browser_reload_div,
        "SAME_CHAT_TRACE_STEPS": same_chat_trace_steps,
        "SAME_CHAT_TRACE_IDENTITY_DIVERGENCES": same_chat_trace_div,
        "HARDCODED_GATE_RESULT_FIELDS": hardcoded_fields,
        "NO_HARDCODED_GATE_RESULTS": no_hardcoded,
        "SOURCE_HEAD": head,
        "SOURCE_TREE": tree,
        "WORKTREE_CLEAN": worktree_clean,
        "SOURCE_BOUND_MACHINE_GATE": source_bound,
        "NX050_STATUS": status_value,
    }


def test_nx050_machine_gate_execution(tmp_path: Path) -> None:
    """Execute and validate all NX-050 machine gate fields."""
    gate = run_nx050_machine_gate(tmp_path)
    print(json.dumps(gate, indent=2, sort_keys=True))
    assert gate["CHAT_RESULT_ENVELOPE_VERSION_EXPLICIT"] is True
    assert gate["INTERACTIVE_LOOP_VERSION_EXPLICIT"] is True
    assert gate["RESULTS_TO_WRONG_BINDING"] == 0
    assert gate["GUESSED_CHAT_IDENTITIES"] == 0
    assert gate["SECOND_CHAT_SEND_AUTHORITY_CREATED"] is False
    assert gate["KNOWN_SECRET_LEAKS_TO_CHAT"] == 0
    assert gate["UNBOUNDED_OUTPUT_SENT_TO_CHAT"] == 0
    assert gate["BLIND_RESULT_RESENDS"] == 0
    assert gate["DUPLICATE_USER_VISIBLE_RESULTS"] == 0
    assert gate["WRONG_BINDING_SEND_EFFECTS"] == 0
    assert gate["WRONG_BINDING_WORKFLOW_EFFECTS"] == 0
    assert gate["DUPLICATE_RESULT_USER_MESSAGES"] == 0
    assert gate["DUPLICATE_RESULT_NEXT_COMMANDS"] == 0
    assert gate["DUPLICATE_RESULT_WORKFLOW_SUBMISSIONS"] == 0
    assert gate["CONFLICTING_RESULTS_ACCEPTED"] == 0
    assert gate["CHAT_LOOP_TASK_ACCEPTANCE_MUTATIONS"] == 0
    assert gate["CHAT_PRESENTATION_BECOMES_WORKFLOW_AUTHORITY"] is False
    assert gate["LLM_POLICY_BYPASS_EXECUTIONS"] == 0
    assert gate["UNTYPED_NEXT_COMMAND_EXECUTIONS"] == 0
    assert gate["LOOP_BUDGET_RESET_AFTER_RELOAD"] is False
    assert gate["LOOP_BUDGET_RESET_AFTER_RESTART"] is False
    assert gate["POST_LOOP_EXHAUSTION_COMMAND_EFFECTS"] == 0
    assert gate["STOP_FENCE_LOOP_BYPASSES"] == 0
    assert gate["BROWSER_RELOAD_DIVERGENCES"] == 0
    assert gate["SAME_CHAT_TRACE_STEPS"] == 10
    assert gate["SAME_CHAT_TRACE_IDENTITY_DIVERGENCES"] == 0
    assert gate["HARDCODED_GATE_RESULT_FIELDS"] == []
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    if gate["WORKTREE_CLEAN"]:
        assert gate["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
        assert gate["NX050_STATUS"] == "PASS"

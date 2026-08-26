"""NX-G2 bounded final qualification for the NX-M2 authority boundary.

This file is a qualification artifact only.  It does not add a runtime
authority, scheduler, Browser action, Native action, or recovery behavior.
It composes the already accepted NX-020..NX-030 contracts into one explicit
four-scope trace and fault/restart readback.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import pytest

from bdb_vnext.auto_scope_contract import (
    AUTO_SCOPE_SCHEMA_VERSION,
    AutoScope,
    CanonicalWorkState,
    DEFAULT_AUTO_SCOPE,
    ScopeAction,
    ScopeInputSnapshot,
    evaluate_scope_transition,
)
from bdb_vnext.continuation_lease import (
    CONTINUATION_LEASE_UNDER_PROJECT_MEMORY_V2,
    CONTINUATION_LEASE_VERSION,
    CONTINUATION_LEASE_VERSION_EXPLICIT,
)
from bdb_vnext.project_center_auto import (
    AUTO_SCOPE_OPTIONS,
    CanonicalAutoState,
    CanonicalProjectCenterAutoCommands,
    ProjectCenterAutoViewModel,
)
from bdb_vnext.project_memory_v2_contract import AUTHORITY_INVENTORY
from bdb_vnext.send_intent import (
    BROWSER_LOCAL_STATE_IS_SEND_AUTHORITY,
    SEND_INTENT_UNDER_CANONICAL_AUTHORITY,
    SEND_INTENT_VERSION,
    SEND_INTENT_VERSION_EXPLICIT,
)


ROOT = Path(__file__).resolve().parents[1]

# This is the complete bounded NX-M2 corpus.  Browser/Native integration
# files are included only where they exercise the vNext AUTO/launch bridge or
# the native project-launch adapter used by continuation effects.
G2_TEST_MANIFEST: tuple[str, ...] = (
    "tests/test_nx020_auto_scope_contract.py",
    "tests/test_nx021_scope_orchestrator.py",
    "tests/test_nx022_stop_fence.py",
    "tests/test_nx023_project_scope.py",
    "tests/test_nx024_until_stopped.py",
    "tests/test_nx025_continuation_packet.py",
    "tests/test_nx026_continuation_lease.py",
    "tests/test_nx027_send_intent.py",
    "tests/test_nx028_session_reentry.py",
    "tests/test_nx029_restart_recovery.py",
    "tests/test_nx030_project_center.py",
    "tests/test_vnext_project_auto_browser.py",
    "tests/test_vnext_project_launch_browser_bridge.py",
    "tests/test_native_host_project_launcher.py",
    "tests/test_nxg2_m2_final_qualification.py",
)


@dataclass(frozen=True)
class ScopeTraceStep:
    name: str
    snapshot: ScopeInputSnapshot
    expected_action: ScopeAction
    expected_state: CanonicalWorkState
    expected_task_id: str | None = None
    expected_milestone_id: str | None = None
    expected_crosses_task: bool = False
    expected_crosses_milestone: bool = False
    expected_terminal: bool = False


def _task_start(scope: AutoScope) -> ScopeTraceStep:
    return ScopeTraceStep(
        name="launch-first-task",
        snapshot=ScopeInputSnapshot(
            current_scope=scope,
            current_milestone_id="M1",
            next_task_in_milestone_id="T1",
            next_task_dependencies_satisfied=True,
        ),
        expected_action=ScopeAction.LAUNCH_TASK,
        expected_state=CanonicalWorkState.RUNNABLE,
        expected_task_id="T1",
        expected_milestone_id="M1",
        expected_crosses_task=scope != AutoScope.TASK,
    )


def _scope_trace_corpus() -> dict[AutoScope, tuple[ScopeTraceStep, ...]]:
    """Return deterministic traces for all four legal AUTO scopes."""

    task = (
        _task_start(AutoScope.TASK),
        ScopeTraceStep(
            name="stop-after-one-accepted-task",
            snapshot=ScopeInputSnapshot(
                current_scope=AutoScope.TASK,
                current_task_id="T1",
                current_task_status="ACCEPTED",
                accepted_tasks_in_current_scope=1,
                current_milestone_id="M1",
                next_task_in_milestone_id="T2",
            ),
            expected_action=ScopeAction.STOP_SCOPE_COMPLETE,
            expected_state=CanonicalWorkState.COMPLETED,
            expected_terminal=True,
        ),
    )

    milestone = (
        _task_start(AutoScope.MILESTONE),
        ScopeTraceStep(
            name="continue-within-same-milestone",
            snapshot=ScopeInputSnapshot(
                current_scope=AutoScope.MILESTONE,
                current_task_id="T1",
                current_task_status="ACCEPTED",
                accepted_tasks_in_current_scope=1,
                current_milestone_id="M1",
                next_task_in_milestone_id="T2",
                next_task_dependencies_satisfied=True,
            ),
            expected_action=ScopeAction.LAUNCH_TASK,
            expected_state=CanonicalWorkState.RUNNABLE,
            expected_task_id="T2",
            expected_milestone_id="M1",
            expected_crosses_task=True,
        ),
        ScopeTraceStep(
            name="wait-for-current-milestone-gate",
            snapshot=ScopeInputSnapshot(
                current_scope=AutoScope.MILESTONE,
                current_task_id="T2",
                current_task_status="ACCEPTED",
                accepted_tasks_in_current_scope=2,
                current_milestone_id="M1",
                all_milestone_tasks_accepted=True,
                current_milestone_gate_status="NOT_REACHED",
            ),
            expected_action=ScopeAction.WAIT_MILESTONE_GATE_PENDING,
            expected_state=CanonicalWorkState.WAITING,
            expected_milestone_id="M1",
        ),
        ScopeTraceStep(
            name="stop-at-accepted-milestone-gate",
            snapshot=ScopeInputSnapshot(
                current_scope=AutoScope.MILESTONE,
                current_task_id="T2",
                current_task_status="ACCEPTED",
                accepted_tasks_in_current_scope=2,
                current_milestone_id="M1",
                all_milestone_tasks_accepted=True,
                current_milestone_gate_status="ACCEPTED",
                next_milestone_id="M2",
                first_task_in_next_milestone_id="T3",
            ),
            expected_action=ScopeAction.STOP_SCOPE_COMPLETE,
            expected_state=CanonicalWorkState.COMPLETED,
            expected_milestone_id="M1",
            expected_terminal=True,
        ),
    )

    project = (
        _task_start(AutoScope.PROJECT),
        ScopeTraceStep(
            name="continue-within-project-milestone",
            snapshot=ScopeInputSnapshot(
                current_scope=AutoScope.PROJECT,
                current_task_id="T1",
                current_task_status="ACCEPTED",
                accepted_tasks_in_current_scope=1,
                current_milestone_id="M1",
                next_task_in_milestone_id="T2",
                next_task_dependencies_satisfied=True,
            ),
            expected_action=ScopeAction.LAUNCH_TASK,
            expected_state=CanonicalWorkState.RUNNABLE,
            expected_task_id="T2",
            expected_milestone_id="M1",
            expected_crosses_task=True,
        ),
        ScopeTraceStep(
            name="cross-only-after-accepted-gate",
            snapshot=ScopeInputSnapshot(
                current_scope=AutoScope.PROJECT,
                current_task_id="T2",
                current_task_status="ACCEPTED",
                accepted_tasks_in_current_scope=2,
                current_milestone_id="M1",
                all_milestone_tasks_accepted=True,
                current_milestone_gate_status="ACCEPTED",
                next_milestone_id="M2",
                first_task_in_next_milestone_id="T3",
                next_milestone_dependencies_satisfied=True,
            ),
            expected_action=ScopeAction.LAUNCH_TASK,
            expected_state=CanonicalWorkState.RUNNABLE,
            expected_task_id="T3",
            expected_milestone_id="M2",
            expected_crosses_task=True,
            expected_crosses_milestone=True,
        ),
        ScopeTraceStep(
            name="stop-after-project-completion",
            snapshot=ScopeInputSnapshot(
                current_scope=AutoScope.PROJECT,
                current_task_id="T3",
                current_task_status="ACCEPTED",
                accepted_tasks_in_current_scope=3,
                current_milestone_id="M2",
                all_milestone_tasks_accepted=True,
                current_milestone_gate_status="ACCEPTED",
                all_project_milestones_completed=True,
            ),
            expected_action=ScopeAction.STOP_PROJECT_COMPLETE,
            expected_state=CanonicalWorkState.COMPLETED,
            expected_terminal=True,
        ),
    )

    until_stopped = (
        _task_start(AutoScope.UNTIL_STOPPED),
        ScopeTraceStep(
            name="continue-approved-work",
            snapshot=ScopeInputSnapshot(
                current_scope=AutoScope.UNTIL_STOPPED,
                current_task_id="T1",
                current_task_status="ACCEPTED",
                accepted_tasks_in_current_scope=1,
                current_milestone_id="M1",
                next_task_in_milestone_id="T2",
                next_task_dependencies_satisfied=True,
            ),
            expected_action=ScopeAction.LAUNCH_TASK,
            expected_state=CanonicalWorkState.RUNNABLE,
            expected_task_id="T2",
            expected_milestone_id="M1",
            expected_crosses_task=True,
        ),
        ScopeTraceStep(
            name="cross-approved-milestone-boundary",
            snapshot=ScopeInputSnapshot(
                current_scope=AutoScope.UNTIL_STOPPED,
                current_task_id="T2",
                current_task_status="ACCEPTED",
                accepted_tasks_in_current_scope=2,
                current_milestone_id="M1",
                all_milestone_tasks_accepted=True,
                current_milestone_gate_status="ACCEPTED",
                next_milestone_id="M2",
                first_task_in_next_milestone_id="T3",
                next_milestone_dependencies_satisfied=True,
            ),
            expected_action=ScopeAction.LAUNCH_TASK,
            expected_state=CanonicalWorkState.RUNNABLE,
            expected_task_id="T3",
            expected_milestone_id="M2",
            expected_crosses_task=True,
            expected_crosses_milestone=True,
        ),
        ScopeTraceStep(
            name="wait-for-plan-extension-not-invent-successor",
            snapshot=ScopeInputSnapshot(
                current_scope=AutoScope.UNTIL_STOPPED,
                current_task_id="T3",
                current_task_status="ACCEPTED",
                accepted_tasks_in_current_scope=3,
                current_milestone_id="M2",
                all_milestone_tasks_accepted=True,
                current_milestone_gate_status="ACCEPTED",
                approved_plan_exhausted=True,
            ),
            expected_action=ScopeAction.HALT_WAITING_FOR_PLAN,
            expected_state=CanonicalWorkState.WAITING_FOR_PLAN,
            expected_terminal=True,
        ),
        ScopeTraceStep(
            name="explicit-stop-fence-wins",
            snapshot=ScopeInputSnapshot(
                current_scope=AutoScope.UNTIL_STOPPED,
                current_task_id="T3",
                current_task_status="ACCEPTED",
                accepted_tasks_in_current_scope=3,
                current_milestone_id="M2",
                stop_requested=True,
                approved_plan_exhausted=True,
            ),
            expected_action=ScopeAction.STOP_EXTERNAL_STOP_REQUESTED,
            expected_state=CanonicalWorkState.PAUSED,
            expected_terminal=True,
        ),
    )

    return {
        AutoScope.TASK: task,
        AutoScope.MILESTONE: milestone,
        AutoScope.PROJECT: project,
        AutoScope.UNTIL_STOPPED: until_stopped,
    }


def _run_scope_trace_corpus() -> dict[str, dict[str, Any]]:
    observations: dict[str, dict[str, Any]] = {}
    for scope, trace in _scope_trace_corpus().items():
        divergences = 0
        task_boundary_crossings = 0
        milestone_boundary_crossings = 0
        observed_actions: list[str] = []
        for step in trace:
            decision = evaluate_scope_transition(step.snapshot)
            observed_actions.append(decision.action.value)
            divergences += int(decision.action != step.expected_action)
            divergences += int(decision.canonical_work_state != step.expected_state)
            divergences += int(decision.selected_task_id != step.expected_task_id)
            divergences += int(decision.selected_milestone_id != step.expected_milestone_id)
            divergences += int(decision.crosses_task_boundary != step.expected_crosses_task)
            divergences += int(decision.crosses_milestone_boundary != step.expected_crosses_milestone)
            divergences += int(decision.is_terminal != step.expected_terminal)
            task_boundary_crossings += int(decision.crosses_task_boundary)
            milestone_boundary_crossings += int(decision.crosses_milestone_boundary)
        observations[scope.value] = {
            "expected_steps": len(trace),
            "observed_steps": len(observed_actions),
            "observed_actions": observed_actions,
            "divergences": divergences,
            "task_boundary_crossings": task_boundary_crossings,
            "milestone_boundary_crossings": milestone_boundary_crossings,
        }
    return observations


def _authority_readback() -> dict[str, str]:
    facts = {fact.fact_name: fact for fact in AUTHORITY_INVENTORY}
    lease_fact = facts["lease_record"]
    send_fact = facts["send_intent"]
    storage = CanonicalProjectCenterAutoCommands.STORAGE_DATABASE
    owner = CanonicalProjectCenterAutoCommands.SCHEMA_OWNER
    transaction = CanonicalProjectCenterAutoCommands.TRANSACTION_AUTHORITY
    return {
        "LEASE_STORAGE_DATABASE": f"{storage} ({lease_fact.v2_table})",
        "LEASE_SCHEMA_OWNER": lease_fact.v2_owner,
        "LEASE_TRANSACTION_AUTHORITY": transaction,
        "SEND_INTENT_STORAGE_DATABASE": f"{storage} ({send_fact.v2_table})",
        "SEND_INTENT_SCHEMA_OWNER": send_fact.v2_owner,
        "SEND_INTENT_TRANSACTION_AUTHORITY": transaction,
    }


def _count(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if value is None:
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return int(bool(value))


def _sum_fields(gate: Mapping[str, Any], *fields: str) -> int:
    return sum(_count(gate.get(field)) for field in fields)


def _source_readback(repo_root: Path) -> tuple[str, str, bool]:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, text=True, check=True
    ).stdout.strip()
    tree = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=repo_root, capture_output=True, text=True, check=True
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo_root, capture_output=True, text=True, check=True
    ).stdout.strip()
    diff_check = subprocess.run(
        ["git", "diff", "--check"], cwd=repo_root, capture_output=True, text=True, check=False
    )
    return head, tree, not status and diff_check.returncode == 0


_G2_GATE_RESULT_FIELDS = frozenset(
    {
        "ALL_FOUR_SCOPES_QUALIFIED",
        "DEFAULT_SCOPE_IS_MILESTONE",
        "MILESTONE_LEGACY_DIVERGENCES",
        "UNAUTHORIZED_CROSS_BOUNDARY_EFFECTS",
        "DUPLICATE_TASK_EXECUTIONS",
        "DUPLICATE_CONTINUATION_EFFECTS",
        "DUPLICATE_USER_VISIBLE_SENDS",
        "BLIND_RESENDS",
        "STOP_FENCE_DIVERGENCES",
        "RESTART_NEXT_ACTION_DIVERGENCES",
        "SCOPE_TRACE_DIVERGENCES",
        "CONTINUATION_FAULT_DIVERGENCES",
        "GUI_AUTHORITY_BYPASSES",
        "OPEN_REQUIRED_M2_DEFECTS",
        "G2_TEST_FILES",
        "G2_TESTS_COLLECTED",
        "G2_TESTS_PASSED",
        "G2_TESTS_FAILED",
        "G2_TESTS_SKIPPED",
        "TEST_COUNT_DIVERGENCES",
        "LEASE_STORAGE_DATABASE",
        "LEASE_SCHEMA_OWNER",
        "LEASE_TRANSACTION_AUTHORITY",
        "SEND_INTENT_STORAGE_DATABASE",
        "SEND_INTENT_SCHEMA_OWNER",
        "SEND_INTENT_TRANSACTION_AUTHORITY",
        "HARDCODED_GATE_RESULT_FIELDS",
        "NO_HARDCODED_GATE_RESULTS",
        "SOURCE_HEAD",
        "SOURCE_TREE",
        "WORKTREE_CLEAN",
        "SOURCE_BOUND_MACHINE_GATE",
        "NX_G2_STATUS",
    }
)


def _inspect_g2_gate_for_hardcoded_results() -> tuple[bool, list[str]]:
    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "run_nx_g2_machine_gate"
    )
    hardcoded: list[str] = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in _G2_GATE_RESULT_FIELDS:
                hardcoded.append(target.id)
    return not hardcoded, sorted(set(hardcoded))


def _summary_count(output: str, word: str) -> int:
    return sum(int(match) for match in re.findall(rf"(\d+)\s+{word}", output))


def _run_bounded_manifest() -> dict[str, Any]:
    collect = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *G2_TEST_MANIFEST],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    collect_output = f"{collect.stdout}\n{collect.stderr}"
    collected_match = re.search(r"(\d+)\s+tests?\s+collected", collect_output)
    collected = int(collected_match.group(1)) if collected_match else 0

    nested_env = dict(os.environ)
    nested_env["BDB_G2_NESTED"] = "1"
    nested = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-k",
            "not test_nx_g2_machine_gate_execution",
            *G2_TEST_MANIFEST,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=nested_env,
        timeout=300,
    )
    nested_output = f"{nested.stdout}\n{nested.stderr}"
    passed = _summary_count(nested_output, "passed")
    failed = _summary_count(nested_output, "failed") + _summary_count(nested_output, "error")
    skipped = _summary_count(nested_output, "skipped")
    if nested.returncode != 0 and failed == 0:
        failed = 1
    gate_test_passed = int(nested.returncode == 0)
    measured_passed = passed + gate_test_passed
    measured_failed = failed
    measured_skipped = skipped
    return {
        "files": len(G2_TEST_MANIFEST),
        "collected": collected,
        "passed": measured_passed,
        "failed": measured_failed,
        "skipped": measured_skipped,
        "count_divergences": int(
            collected != measured_passed + measured_failed + measured_skipped
        ),
        "collect_returncode": collect.returncode,
        "nested_returncode": nested.returncode,
    }


def _machine_gate_subgates() -> dict[str, dict[str, Any]]:
    from tests.test_nx020_auto_scope_contract import run_nx020_machine_gate
    from tests.test_nx021_scope_orchestrator import run_nx021_machine_gate
    from tests.test_nx022_stop_fence import run_nx022_machine_gate
    from tests.test_nx023_project_scope import run_nx023_machine_gate
    from tests.test_nx024_until_stopped import run_nx024_machine_gate
    from tests.test_nx025_continuation_packet import run_nx025_machine_gate
    from tests.test_nx026_continuation_lease import run_nx026_machine_gate
    from tests.test_nx027_send_intent import run_nx027_machine_gate
    from tests.test_nx028_session_reentry import run_nx028_machine_gate
    from tests.test_nx029_restart_recovery import run_nx029_machine_gate
    from tests.test_nx030_project_center import run_nx030_machine_gate

    return {
        "NX020": run_nx020_machine_gate(),
        "NX021": run_nx021_machine_gate(),
        "NX022": run_nx022_machine_gate(),
        "NX023": run_nx023_machine_gate(),
        "NX024": run_nx024_machine_gate(),
        "NX025": run_nx025_machine_gate(),
        "NX026": run_nx026_machine_gate(),
        "NX027": run_nx027_machine_gate(),
        "NX028": run_nx028_machine_gate(),
        "NX029": run_nx029_machine_gate(),
        "NX030": run_nx030_machine_gate(),
    }


def run_nx_g2_machine_gate() -> dict[str, Any]:
    """Run the source-bound, bounded final NX-M2 qualification gate."""

    trace_observations = _run_scope_trace_corpus()
    subgates = _machine_gate_subgates()
    manifest = _run_bounded_manifest()
    authority = _authority_readback()
    no_hardcoded, hardcoded = _inspect_g2_gate_for_hardcoded_results()

    all_four_scopes = bool(
        set(trace_observations) == {scope.value for scope in AutoScope}
        and all(item["divergences"] == 0 for item in trace_observations.values())
    )
    default_scope = bool(
        DEFAULT_AUTO_SCOPE == AutoScope.MILESTONE
        and subgates["NX030"]["DEFAULT_GUI_SCOPE_IS_MILESTONE"]
    )
    scope_trace_divergences = sum(
        _count(item["divergences"]) for item in trace_observations.values()
    )
    milestone = trace_observations[AutoScope.MILESTONE.value]
    milestone_legacy_divergences = _count(milestone["divergences"]) + _count(
        milestone["milestone_boundary_crossings"]
    )

    nx022 = subgates["NX022"]
    nx026 = subgates["NX026"]
    nx027 = subgates["NX027"]
    nx028 = subgates["NX028"]
    nx029 = subgates["NX029"]
    nx030 = subgates["NX030"]

    unauthorized_cross_boundary_effects = (
        scope_trace_divergences
        + _sum_fields(
            nx022,
            "UNFENCED_EFFECT_PATHS",
            "EFFECTS_AFTER_STOP_BEFORE_SEND",
            "CLAIMED_OLD_EPOCH_EFFECTS_AFTER_STOP",
            "POST_FENCE_NEW_EFFECTS",
            "STALE_EPOCH_REPLAY_EFFECTS",
        )
        + _sum_fields(
            nx026,
            "LOSING_CLAIMANT_EFFECTS",
            "OLD_OWNER_AFTER_RECLAIM_EFFECTS",
            "STALE_PACKET_CLAIM_EFFECTS",
            "LEASE_BYPASSES_STOP_FENCE",
            "UNSAFE_RECLAIMS",
        )
        + _sum_fields(
            nx027,
            "PHYSICAL_SEND_WITHOUT_DURABLE_INTENT",
            "STALE_AUTHORITY_PHYSICAL_SENDS",
            "SEND_BYPASSES_STOP_FENCE",
            "STALE_LEASE_SEND_EFFECTS",
        )
        + _sum_fields(
            nx028,
            "STALE_REENTRY_EFFECTS",
            "REENTRY_BYPASSES_STOP_FENCE",
            "STALE_LEASE_REENTRY_EFFECTS",
            "BLIND_REENTRY_SENDS",
        )
        + _sum_fields(
            nx029,
            "CORRUPT_LOCAL_CACHE_CANONICAL_MUTATIONS",
            "AMBIGUOUS_RECOVERY_AUTO_EFFECTS",
            "RESTART_BLIND_RESENDS",
        )
    )
    duplicate_task_executions = _sum_fields(nx028, "ACCEPTED_TASK_REPEATED_AFTER_REENTRY")
    duplicate_continuation_effects = (
        _sum_fields(nx026, "DUPLICATE_COMPLETIONS")
        + _sum_fields(nx028, "DUPLICATE_REENTRY_EFFECTS")
        + _sum_fields(nx029, "RESTART_DUPLICATE_REENTRY_EFFECTS")
    )
    duplicate_user_visible_sends = (
        _sum_fields(nx027, "DUPLICATE_PHYSICAL_SEND_EFFECTS")
        + _sum_fields(nx028, "DUPLICATE_REENTRY_EFFECTS")
        + _sum_fields(nx029, "RESTART_DUPLICATE_REENTRY_EFFECTS")
    )
    blind_resends = (
        _sum_fields(nx027, "BLIND_RESENDS_AFTER_UNCERTAIN_DELIVERY")
        + _sum_fields(nx028, "BLIND_REENTRY_SENDS")
        + _sum_fields(nx029, "RESTART_BLIND_RESENDS")
    )
    stop_fence_divergences = (
        _sum_fields(
            nx022,
            "UNFENCED_EFFECT_PATHS",
            "EFFECTS_AFTER_STOP_BEFORE_SEND",
            "CLAIMED_OLD_EPOCH_EFFECTS_AFTER_STOP",
            "POST_FENCE_NEW_EFFECTS",
            "STALE_EPOCH_REPLAY_EFFECTS",
        )
        + _sum_fields(nx026, "LEASE_BYPASSES_STOP_FENCE")
        + _sum_fields(nx027, "SEND_BYPASSES_STOP_FENCE")
        + _sum_fields(nx028, "REENTRY_BYPASSES_STOP_FENCE")
        + _sum_fields(nx029, "RESTART_CLEARS_STOP")
    )
    restart_next_action_divergences = _sum_fields(
        nx029, "RECOVERY_ACTION_DIVERGENCES", "TRACE_DIVERGENCES"
    )
    continuation_fault_divergences = (
        _sum_fields(
            nx026,
            "FOREIGN_RENEW_ACCEPTED",
            "FOREIGN_RELEASE_ACCEPTED",
            "FOREIGN_COMPLETE_ACCEPTED",
            "FOREIGN_ABANDON_ACCEPTED",
            "PRE_EXPIRY_RECLAIM_ACCEPTED",
            "LATE_RENEW_AFTER_RECLAIM_ACCEPTED",
            "OLD_OWNER_AFTER_RECLAIM_EFFECTS",
            "DUPLICATE_COMPLETIONS",
            "COMPLETED_CONTINUATION_RECLAIMED",
            "ACCEPTED_TASK_REEXECUTED_BY_RECLAIM",
            "STALE_PACKET_CLAIM_EFFECTS",
            "LEASE_BYPASSES_STOP_FENCE",
            "RESTART_LEASE_STATE_DIVERGENCES",
            "UNSAFE_RECLAIMS",
        )
        + _sum_fields(
            nx027,
            "PHYSICAL_SEND_WITHOUT_DURABLE_INTENT",
            "DUPLICATE_PREPARE_RECORDS",
            "STALE_AUTHORITY_PHYSICAL_SENDS",
            "MISSING_COMPOSER_SEND_ATTEMPTS",
            "FOCUS_ONLY_MARKED_AS_SENT",
            "BLIND_RESENDS_AFTER_UNCERTAIN_DELIVERY",
            "DUPLICATE_PHYSICAL_SEND_EFFECTS",
            "WRONG_ACK_ACCEPTED",
            "DUPLICATE_ACK_EFFECTS",
            "MANUAL_USER_PROMPTS_REQUIRED_FOR_SAME_CHAT_TASK_TRANSITION",
            "GUESSED_CONVERSATION_IDENTITY_ACCEPTED",
            "SEND_BYPASSES_STOP_FENCE",
            "STALE_LEASE_SEND_EFFECTS",
            "TRACE_DIVERGENCES",
        )
        + _sum_fields(
            nx028,
            "STALE_REENTRY_EFFECTS",
            "ACCEPTED_TASK_REPEATED_AFTER_REENTRY",
            "IN_PROGRESS_BINDING_GENERATION_DIVERGENCES",
            "UNVERIFIED_AUTOMATED_NEW_CHAT_USED",
            "GUESSED_EXISTING_CONVERSATION_ACCEPTED",
            "OPERATOR_FALLBACK_REQUIRES_MANUAL_PROMPT_BUILD",
            "OPERATOR_FALLBACK_ADDITIONAL_DECISION_REQUIRED",
            "BLIND_REENTRY_SENDS",
            "DUPLICATE_REENTRY_EFFECTS",
            "REENTRY_BYPASSES_STOP_FENCE",
            "STALE_LEASE_REENTRY_EFFECTS",
            "UNCERTAIN_DELIVERY_CAUSES_NEW_REENTRY_SEND",
            "TRACE_DIVERGENCES",
        )
        + _sum_fields(
            nx029,
            "RECOVERY_PRECEDENCE_AMBIGUITIES",
            "GOLDEN_RECOVERY_TRACE_DIVERGENCES",
            "NATIVE_RESTART_DUPLICATE_EFFECTS",
            "PROCESS_DEATH_CAUSES_PREEXPIRY_RECLAIM",
            "RESTART_BLIND_RESENDS",
            "RESTART_DUPLICATE_REENTRY_EFFECTS",
            "RESTART_SCOPE_CURSOR_DIVERGENCES",
            "RESTART_CLEARS_STOP",
            "CORRUPT_LOCAL_CACHE_CANONICAL_MUTATIONS",
            "AMBIGUOUS_RECOVERY_AUTO_EFFECTS",
            "RECOVERY_DIGEST_DIVERGENCES",
            "TRACE_DIVERGENCES",
            "RECOVERY_ACTION_DIVERGENCES",
        )
    )
    gui_authority_bypasses = _sum_fields(
        nx030,
        "UI_BECOMES_WORKFLOW_AUTHORITY",
        "UI_SCOPE_SELECTION_MUTATES_AUTHORITY_DIRECTLY",
        "GUI_STOP_BYPASSES_CANONICAL_FENCE",
        "CONTINUE_REQUIRES_MANUAL_NEXT_TASK_SELECTION",
        "CONTINUE_REQUIRES_MANUAL_NEXT_MILESTONE_SELECTION",
        "RESUME_REQUIRES_MANUAL_PROMPT",
        "RESUME_FROM_BROWSER_LOCAL_STATE",
        "GUI_CANONICAL_STATE_DIVERGENCES",
    )

    required_gate_names = tuple(f"NX{number:03d}" for number in range(20, 31))
    subgate_defects = sum(
        int(str(subgates[name].get(f"{name}_STATUS", "FAIL")) != "PASS")
        for name in required_gate_names
    )
    open_required_m2_defects = (
        subgate_defects
        + int(not all_four_scopes)
        + int(not default_scope)
        + milestone_legacy_divergences
        + unauthorized_cross_boundary_effects
        + duplicate_task_executions
        + duplicate_continuation_effects
        + duplicate_user_visible_sends
        + blind_resends
        + stop_fence_divergences
        + restart_next_action_divergences
        + scope_trace_divergences
        + continuation_fault_divergences
        + gui_authority_bypasses
        + _count(manifest["failed"])
        + _count(manifest["count_divergences"])
    )

    head, tree, clean = _source_readback(ROOT)
    source_bound = bool(len(head) == 40 and len(tree) == 40 and clean)
    hardcoded_fields = hardcoded
    no_hardcoded_results = no_hardcoded

    ALL_FOUR_SCOPES_QUALIFIED = all_four_scopes
    DEFAULT_SCOPE_IS_MILESTONE = default_scope
    MILESTONE_LEGACY_DIVERGENCES = milestone_legacy_divergences
    UNAUTHORIZED_CROSS_BOUNDARY_EFFECTS = unauthorized_cross_boundary_effects
    DUPLICATE_TASK_EXECUTIONS = duplicate_task_executions
    DUPLICATE_CONTINUATION_EFFECTS = duplicate_continuation_effects
    DUPLICATE_USER_VISIBLE_SENDS = duplicate_user_visible_sends
    BLIND_RESENDS = blind_resends
    STOP_FENCE_DIVERGENCES = stop_fence_divergences
    RESTART_NEXT_ACTION_DIVERGENCES = restart_next_action_divergences
    SCOPE_TRACE_DIVERGENCES = scope_trace_divergences
    CONTINUATION_FAULT_DIVERGENCES = continuation_fault_divergences
    GUI_AUTHORITY_BYPASSES = gui_authority_bypasses
    OPEN_REQUIRED_M2_DEFECTS = open_required_m2_defects
    G2_TEST_FILES = manifest["files"]
    G2_TESTS_COLLECTED = manifest["collected"]
    G2_TESTS_PASSED = manifest["passed"]
    G2_TESTS_FAILED = manifest["failed"]
    G2_TESTS_SKIPPED = manifest["skipped"]
    TEST_COUNT_DIVERGENCES = manifest["count_divergences"]
    LEASE_STORAGE_DATABASE = authority["LEASE_STORAGE_DATABASE"]
    LEASE_SCHEMA_OWNER = authority["LEASE_SCHEMA_OWNER"]
    LEASE_TRANSACTION_AUTHORITY = authority["LEASE_TRANSACTION_AUTHORITY"]
    SEND_INTENT_STORAGE_DATABASE = authority["SEND_INTENT_STORAGE_DATABASE"]
    SEND_INTENT_SCHEMA_OWNER = authority["SEND_INTENT_SCHEMA_OWNER"]
    SEND_INTENT_TRANSACTION_AUTHORITY = authority["SEND_INTENT_TRANSACTION_AUTHORITY"]
    HARDCODED_GATE_RESULT_FIELDS = hardcoded_fields
    NO_HARDCODED_GATE_RESULTS = no_hardcoded_results
    SOURCE_HEAD = head
    SOURCE_TREE = tree
    WORKTREE_CLEAN = clean
    SOURCE_BOUND_MACHINE_GATE = "PASS" if source_bound else "FAIL"
    NX_G2_STATUS = "PASS" if (
        ALL_FOUR_SCOPES_QUALIFIED
        and DEFAULT_SCOPE_IS_MILESTONE
        and MILESTONE_LEGACY_DIVERGENCES == 0
        and UNAUTHORIZED_CROSS_BOUNDARY_EFFECTS == 0
        and DUPLICATE_TASK_EXECUTIONS == 0
        and DUPLICATE_CONTINUATION_EFFECTS == 0
        and DUPLICATE_USER_VISIBLE_SENDS == 0
        and BLIND_RESENDS == 0
        and STOP_FENCE_DIVERGENCES == 0
        and RESTART_NEXT_ACTION_DIVERGENCES == 0
        and SCOPE_TRACE_DIVERGENCES == 0
        and CONTINUATION_FAULT_DIVERGENCES == 0
        and GUI_AUTHORITY_BYPASSES == 0
        and OPEN_REQUIRED_M2_DEFECTS == 0
        and G2_TESTS_FAILED == 0
        and TEST_COUNT_DIVERGENCES == 0
        and NO_HARDCODED_GATE_RESULTS
        and SOURCE_BOUND_MACHINE_GATE == "PASS"
    ) else "FAIL"

    return {
        "ALL_FOUR_SCOPES_QUALIFIED": ALL_FOUR_SCOPES_QUALIFIED,
        "DEFAULT_SCOPE_IS_MILESTONE": DEFAULT_SCOPE_IS_MILESTONE,
        "MILESTONE_LEGACY_DIVERGENCES": MILESTONE_LEGACY_DIVERGENCES,
        "UNAUTHORIZED_CROSS_BOUNDARY_EFFECTS": UNAUTHORIZED_CROSS_BOUNDARY_EFFECTS,
        "DUPLICATE_TASK_EXECUTIONS": DUPLICATE_TASK_EXECUTIONS,
        "DUPLICATE_CONTINUATION_EFFECTS": DUPLICATE_CONTINUATION_EFFECTS,
        "DUPLICATE_USER_VISIBLE_SENDS": DUPLICATE_USER_VISIBLE_SENDS,
        "BLIND_RESENDS": BLIND_RESENDS,
        "STOP_FENCE_DIVERGENCES": STOP_FENCE_DIVERGENCES,
        "RESTART_NEXT_ACTION_DIVERGENCES": RESTART_NEXT_ACTION_DIVERGENCES,
        "SCOPE_TRACE_DIVERGENCES": SCOPE_TRACE_DIVERGENCES,
        "CONTINUATION_FAULT_DIVERGENCES": CONTINUATION_FAULT_DIVERGENCES,
        "GUI_AUTHORITY_BYPASSES": GUI_AUTHORITY_BYPASSES,
        "OPEN_REQUIRED_M2_DEFECTS": OPEN_REQUIRED_M2_DEFECTS,
        "G2_TEST_FILES": G2_TEST_FILES,
        "G2_TESTS_COLLECTED": G2_TESTS_COLLECTED,
        "G2_TESTS_PASSED": G2_TESTS_PASSED,
        "G2_TESTS_FAILED": G2_TESTS_FAILED,
        "G2_TESTS_SKIPPED": G2_TESTS_SKIPPED,
        "TEST_COUNT_DIVERGENCES": TEST_COUNT_DIVERGENCES,
        **authority,
        "HARDCODED_GATE_RESULT_FIELDS": HARDCODED_GATE_RESULT_FIELDS,
        "NO_HARDCODED_GATE_RESULTS": NO_HARDCODED_GATE_RESULTS,
        "SOURCE_HEAD": SOURCE_HEAD,
        "SOURCE_TREE": SOURCE_TREE,
        "WORKTREE_CLEAN": WORKTREE_CLEAN,
        "SOURCE_BOUND_MACHINE_GATE": SOURCE_BOUND_MACHINE_GATE,
        "NX_G2_STATUS": NX_G2_STATUS,
    }


def test_g2_scope_trace_corpus_qualifies_all_four_scopes() -> None:
    observations = _run_scope_trace_corpus()
    assert set(observations) == {scope.value for scope in AUTO_SCOPE_OPTIONS}
    assert all(item["divergences"] == 0 for item in observations.values())
    assert observations[AutoScope.MILESTONE.value]["milestone_boundary_crossings"] == 0


def test_g2_project_center_projection_remains_non_authoritative() -> None:
    canonical = CanonicalAutoState(
        project_id="nx-g2-project",
        scope=AutoScope.MILESTONE,
        scope_status="ACTIVE",
        reason_code="ACTIVE",
        reason="canonical active state",
        plan_available=True,
        p2_completed=True,
        p3_started=False,
    )
    for scope in AUTO_SCOPE_OPTIONS:
        selected = ProjectCenterAutoViewModel.from_canonical(
            canonical,
            selected_scope=scope,
            browser_local_state={"scope": "PROJECT", "task_id": "stale-browser-task"},
        )
        assert selected.canonical == canonical
        assert selected.selected_scope == scope
        assert "task_id" not in selected.continue_intent()
        assert "milestone_id" not in selected.continue_intent()
        assert "prompt" not in selected.resume_intent()
        assert "browser" not in selected.resume_intent()


def test_g2_authority_readback_uses_one_project_memory_v2_root() -> None:
    facts = {fact.fact_name: fact for fact in AUTHORITY_INVENTORY}
    assert facts["lease_record"].v2_owner == "ProjectMemoryStoreV2"
    assert facts["lease_record"].v2_table == "leases"
    assert facts["send_intent"].v2_owner == "ProjectMemoryStoreV2"
    assert facts["send_intent"].v2_table == "send_intents"
    assert CONTINUATION_LEASE_VERSION_EXPLICIT is True
    assert CONTINUATION_LEASE_VERSION == "v1"
    assert CONTINUATION_LEASE_UNDER_PROJECT_MEMORY_V2 is True
    assert SEND_INTENT_VERSION_EXPLICIT is True
    assert SEND_INTENT_VERSION == "v1"
    assert SEND_INTENT_UNDER_CANONICAL_AUTHORITY is True
    assert BROWSER_LOCAL_STATE_IS_SEND_AUTHORITY is False
    assert CanonicalProjectCenterAutoCommands.SECOND_AUTHORITY_CREATED is False
    assert AUTO_SCOPE_SCHEMA_VERSION == "1.0.0"


def test_nx_g2_machine_gate_execution() -> None:
    gate = run_nx_g2_machine_gate()
    print(json.dumps(gate, ensure_ascii=False, sort_keys=True))
    assert gate["ALL_FOUR_SCOPES_QUALIFIED"] is True
    assert gate["DEFAULT_SCOPE_IS_MILESTONE"] is True
    assert gate["MILESTONE_LEGACY_DIVERGENCES"] == 0
    assert gate["UNAUTHORIZED_CROSS_BOUNDARY_EFFECTS"] == 0
    assert gate["DUPLICATE_TASK_EXECUTIONS"] == 0
    assert gate["DUPLICATE_CONTINUATION_EFFECTS"] == 0
    assert gate["DUPLICATE_USER_VISIBLE_SENDS"] == 0
    assert gate["BLIND_RESENDS"] == 0
    assert gate["STOP_FENCE_DIVERGENCES"] == 0
    assert gate["RESTART_NEXT_ACTION_DIVERGENCES"] == 0
    assert gate["SCOPE_TRACE_DIVERGENCES"] == 0
    assert gate["CONTINUATION_FAULT_DIVERGENCES"] == 0
    assert gate["GUI_AUTHORITY_BYPASSES"] == 0
    assert gate["OPEN_REQUIRED_M2_DEFECTS"] == 0
    assert gate["G2_TEST_FILES"] == len(G2_TEST_MANIFEST)
    assert gate["G2_TESTS_COLLECTED"] >= gate["G2_TESTS_PASSED"]
    assert gate["G2_TESTS_FAILED"] == 0
    assert gate["TEST_COUNT_DIVERGENCES"] == 0
    assert gate["HARDCODED_GATE_RESULT_FIELDS"] == []
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    assert gate["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
    assert gate["NX_G2_STATUS"] == "PASS"

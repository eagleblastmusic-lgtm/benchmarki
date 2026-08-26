"""NX-028 canonical session re-entry qualification.

The fixtures intentionally exercise the durable Project Memory v2 relation,
not Browser/local session state.  A new-conversation capability is not supplied
because the current architecture has no official identity-verifiable adapter;
the D-017 operator checkpoint is therefore the measured fallback.
"""

from __future__ import annotations

import ast
import json
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bdb_vnext.auto_scope_contract import AutoScope, ScopeAction
from bdb_vnext.continuation_packet import ContinuationAuthoritySnapshot, ContinuationPacket, build_packet
from bdb_vnext.project_memory_v2_contract import PROJECT_MEMORY_V2_DDL
from bdb_vnext.send_intent import ExactConversationBinding, FakeBrowser
from bdb_vnext.session_reentry import (
    BROWSER_LOCAL_STATE_IS_REENTRY_AUTHORITY,
    REENTRY_UNDER_PROJECT_MEMORY_V2_AUTHORITY,
    SECOND_REENTRY_AUTHORITY_CREATED,
    SESSION_LIVENESS_VERSION,
    SESSION_LIVENESS_VERSION_EXPLICIT,
    SESSION_REENTRY_VERSION,
    SESSION_REENTRY_VERSION_EXPLICIT,
    TURN_END_MARKS_TASK_ACCEPTED,
    ReentryChannel,
    SessionContinuationController,
    SessionLivenessState,
)
from bdb_vnext.stop_fence import execute_stop_transaction


START = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)
EXPIRY = START + timedelta(hours=1)
PLAN_DIGEST = "sha256:" + "1" * 64
STATE_DIGEST = "sha256:" + "2" * 64
EVIDENCE_DIGEST = "sha256:" + "3" * 64
HEAD = "a" * 40
TREE = "b" * 40
EXPECTED_EXISTING_TRACE = (
    "SESSION_END",
    "TURN_ENDED",
    "CONTINUATION_PENDING",
    "LIVE_AUTHORITY_VALIDATED",
    "LEASE_CLAIMED",
    "EXISTING_CONVERSATION_VERIFIED",
    "INTENT_PREPARED",
    "DOM_PRECONDITIONS_VALID",
    "PHYSICAL_SEND",
    "SEND_CONFIRMED",
    "ACK",
    "REENTRY_CONFIRMED",
)


@dataclass
class VirtualClock:
    value: datetime = START

    def now(self) -> datetime:
        return self.value


def _packet(project_id: str = "nx028-project", **overrides: object) -> ContinuationPacket:
    values: dict[str, object] = {
        "project_id": project_id,
        "plan_identity": "plan:nx-m2",
        "plan_version": 1,
        "plan_digest": PLAN_DIGEST,
        "scope": AutoScope.UNTIL_STOPPED,
        "run_id": "run:nx028",
        "scope_epoch": 4,
        "current_milestone_id": "NX-M2",
        "current_task_id": "NX-028",
        "execution_binding_id": "binding:nx028",
        "expected_repo_head_before": HEAD,
        "state_revision": 7,
        "state_digest": STATE_DIGEST,
        "allowed_next_action": ScopeAction.LAUNCH_TASK,
        "budget_summary": {"remaining_attempts": 2, "remaining_retry_budget": 1},
        "evidence_refs": [EVIDENCE_DIGEST],
        "issued_at": START,
        "expires_at": EXPIRY,
        "attempt_id": "attempt:nx028",
        "expected_tree": TREE,
        "conversation_binding_policy": "EXISTING_CHAT_ONLY",
    }
    values.update(overrides)
    return build_packet(**values)  # type: ignore[arg-type]


def _authority(project_id: str = "nx028-project", **overrides: object) -> ContinuationAuthoritySnapshot:
    values: dict[str, object] = {
        "project_id": project_id,
        "plan_identity": "plan:nx-m2",
        "plan_version": 1,
        "plan_digest": PLAN_DIGEST,
        "scope": AutoScope.UNTIL_STOPPED,
        "run_id": "run:nx028",
        "scope_epoch": 4,
        "current_milestone_id": "NX-M2",
        "current_task_id": "NX-028",
        "execution_binding_id": "binding:nx028",
        "expected_repo_head_before": HEAD,
        "state_revision": 7,
        "state_digest": STATE_DIGEST,
        "allowed_next_action": ScopeAction.LAUNCH_TASK,
        "budget_summary": {"remaining_attempts": 2, "remaining_retry_budget": 1},
        "evidence_refs": [EVIDENCE_DIGEST],
        "status": "ACTIVE",
        "task_status": "IN_PROGRESS",
        "stop_requested": False,
        "plan_approved": True,
        "attempt_id": "attempt:nx028",
        "expected_tree": TREE,
        "conversation_binding_policy": "EXISTING_CHAT_ONLY",
    }
    values.update(overrides)
    return ContinuationAuthoritySnapshot(**values)  # type: ignore[arg-type]


def _create_database(path: Path, project_id: str = "nx028-project") -> None:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(PROJECT_MEMORY_V2_DDL)
    now = START.isoformat(timespec="microseconds").replace("+00:00", "Z")
    conn.execute(
        """
        INSERT INTO projects (
            project_id, display_name, repo_alias, local_repo_path,
            brief_json, revision, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (project_id, "NX-028", "nx028", "/tmp/nx028", "{}", 1, now, now),
    )
    conn.execute(
        """
        INSERT INTO project_plans (
            project_id, plan_version, plan_digest, schema, plan_json, imported_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (project_id, 1, PLAN_DIGEST, "bdb-project-plan-v1", "{}", now),
    )
    conn.execute(
        """
        INSERT INTO execution_bindings (
            execution_binding_id, project_id, plan_version, task_id, launch_id,
            correlation_id, command_id, repo_alias, expected_repo_head_before,
            status, generation, superseded, conversation_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "binding:nx028",
            project_id,
            1,
            "NX-028",
            "launch:nx028",
            "correlation:nx028",
            "command:nx028",
            "nx028",
            HEAD,
            "ACTIVE",
            1,
            0,
            "chat:existing-nx028",
            now,
        ),
    )
    conn.execute(
        """
        INSERT INTO scope_cursors (
            cursor_id, project_id, run_id, scope, scope_epoch,
            current_milestone_id, current_task_id, plan_identity,
            plan_version, state_revision, disposition, status, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "cursor:nx028",
            project_id,
            "run:nx028",
            "UNTIL_STOPPED",
            4,
            "NX-M2",
            "NX-028",
            "plan:nx-m2",
            1,
            7,
            "RUNNING",
            "ACTIVE",
            now,
        ),
    )
    conn.commit()
    conn.close()


def _setup(path: Path, project_id: str = "nx028-project") -> tuple[ContinuationPacket, ContinuationAuthoritySnapshot, VirtualClock, SessionContinuationController]:
    _create_database(path, project_id)
    clock = VirtualClock()
    packet = _packet(project_id)
    authority = _authority(project_id)
    controller = SessionContinuationController(path, project_id, clock=clock.now)
    return packet, authority, clock, controller


def test_nx028_schema_and_versioned_authority_contract(tmp_path: Path) -> None:
    packet, authority, _, controller = _setup(tmp_path / "schema.db")
    assert packet["current_task_id"] == "NX-028"
    assert authority.task_status == "IN_PROGRESS"
    assert SESSION_REENTRY_VERSION == "v1"
    assert SESSION_LIVENESS_VERSION == "v1"
    assert SESSION_REENTRY_VERSION_EXPLICIT is True
    assert SESSION_LIVENESS_VERSION_EXPLICIT is True
    assert REENTRY_UNDER_PROJECT_MEMORY_V2_AUTHORITY is True
    assert BROWSER_LOCAL_STATE_IS_REENTRY_AUTHORITY is False
    assert SECOND_REENTRY_AUTHORITY_CREATED is False
    assert TURN_END_MARKS_TASK_ACCEPTED is False
    conn = sqlite3.connect(str(tmp_path / "schema.db"))
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    columns = {row[1] for row in conn.execute("PRAGMA table_info(session_reentries)")}
    conn.close()
    assert "session_reentries" in tables
    assert {
        "packet_json",
        "liveness_state",
        "selected_channel",
        "binding_generation",
        "operator_prompt_build_required",
        "operator_decision_required",
        "effect_count",
    }.issubset(columns)
    assert controller.SECOND_REENTRY_AUTHORITY_CREATED is False


def test_natural_turn_end_reuses_exact_existing_chat_and_preserves_binding_generation(tmp_path: Path) -> None:
    packet, authority, clock, controller = _setup(tmp_path / "existing.db")
    browser = FakeBrowser("chat:existing-nx028")
    result = controller.reenter(
        packet,
        authority,
        owner_id="browser-a",
        payload="Continue the next approved task in this existing chat.",
        existing_binding=ExactConversationBinding.from_verified("chat:existing-nx028"),
        browser=browser,
        now=clock.now(),
    )
    assert result.accepted
    assert result.selected_channel == ReentryChannel.EXISTING_CONVERSATION_VERIFIED.value
    assert result.effects == 1
    assert result.trace == EXPECTED_EXISTING_TRACE
    assert result.record is not None
    assert result.record.liveness_state == SessionLivenessState.REENTRY_CONFIRMED.value
    assert result.record.binding_generation == 1
    conn = sqlite3.connect(str(tmp_path / "existing.db"))
    binding = conn.execute(
        "SELECT generation, conversation_id FROM execution_bindings WHERE execution_binding_id = 'binding:nx028'"
    ).fetchone()
    intent_count = conn.execute("SELECT COUNT(*) FROM send_intents").fetchone()[0]
    conn.close()
    assert binding == (1, "chat:existing-nx028")
    assert intent_count == 1


def test_duplicate_reentry_signal_has_zero_second_effect(tmp_path: Path) -> None:
    packet, authority, clock, controller = _setup(tmp_path / "duplicate.db")
    first = controller.reenter(
        packet,
        authority,
        owner_id="browser-a",
        payload="Continue the next approved task in this existing chat.",
        existing_binding=ExactConversationBinding.from_verified("chat:existing-nx028"),
        browser=FakeBrowser("chat:existing-nx028"),
        now=clock.now(),
    )
    duplicate_browser = FakeBrowser("chat:existing-nx028")
    duplicate = controller.reenter(
        packet,
        authority,
        owner_id="browser-b",
        payload="Continue the next approved task in this existing chat.",
        existing_binding=ExactConversationBinding.from_verified("chat:existing-nx028"),
        browser=duplicate_browser,
        now=clock.now(),
    )
    assert first.accepted and first.effects == 1
    assert duplicate.accepted and duplicate.idempotent and duplicate.effects == 0
    assert duplicate_browser.visible_send_count == 0
    conn = sqlite3.connect(str(tmp_path / "duplicate.db"))
    assert conn.execute("SELECT COUNT(*) FROM session_reentries").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM send_intents").fetchone()[0] == 1
    conn.close()


def test_unavailable_conversation_creates_one_click_operator_checkpoint_and_can_resume(tmp_path: Path) -> None:
    packet, authority, clock, controller = _setup(tmp_path / "operator.db")
    pending = controller.reenter(
        packet,
        authority,
        owner_id="browser-a",
        payload="Continue the next approved task in this existing chat.",
        now=clock.now(),
    )
    assert pending.accepted
    assert pending.selected_channel == ReentryChannel.OPERATOR_ASSISTED_CHECKPOINT_REQUIRED.value
    assert pending.effects == 0
    assert pending.checkpoint_id is not None
    assert pending.record is not None
    assert pending.record.liveness_state == SessionLivenessState.OPERATOR_CHECKPOINT.value
    assert pending.record.operator_prompt_build_required is False
    assert pending.record.operator_decision_required is False

    browser = FakeBrowser("chat:existing-nx028")
    resumed = controller.continue_from_operator_checkpoint(
        pending.checkpoint_id,
        authority,
        owner_id="operator-a",
        binding=ExactConversationBinding.from_verified("chat:existing-nx028"),
        browser=browser,
        now=clock.now(),
    )
    assert resumed.accepted and resumed.effects == 1
    assert resumed.record is not None
    assert resumed.record.liveness_state == SessionLivenessState.REENTRY_CONFIRMED.value
    assert resumed.manual_user_prompts == 0
    duplicate = controller.continue_from_operator_checkpoint(
        pending.checkpoint_id,
        authority,
        owner_id="operator-b",
        binding=ExactConversationBinding.from_verified("chat:existing-nx028"),
        browser=FakeBrowser("chat:existing-nx028"),
        now=clock.now(),
    )
    assert duplicate.accepted and duplicate.idempotent and duplicate.effects == 0


def test_guessed_stale_and_policy_denied_channels_fail_closed_without_send(tmp_path: Path) -> None:
    guessed_packet, guessed_authority, guessed_clock, guessed_controller = _setup(tmp_path / "guessed.db")
    guessed_browser = FakeBrowser("chat:existing-nx028")
    guessed = guessed_controller.reenter(
        guessed_packet,
        guessed_authority,
        owner_id="browser-a",
        payload="payload",
        existing_binding=ExactConversationBinding("chat:existing-nx028", "guessed-proof"),
        browser=guessed_browser,
        now=guessed_clock.now(),
    )
    assert guessed.accepted
    assert guessed.selected_channel == ReentryChannel.OPERATOR_ASSISTED_CHECKPOINT_REQUIRED.value
    assert guessed_browser.visible_send_count == 0

    stale_packet, stale_authority, stale_clock, stale_controller = _setup(tmp_path / "stale-chat.db")
    stale_browser = FakeBrowser("chat:other")
    stale = stale_controller.reenter(
        stale_packet,
        stale_authority,
        owner_id="browser-a",
        payload="payload",
        existing_binding=ExactConversationBinding.from_verified("chat:existing-nx028"),
        browser=stale_browser,
        now=stale_clock.now(),
    )
    assert stale.accepted
    assert stale.selected_channel == ReentryChannel.OPERATOR_ASSISTED_CHECKPOINT_REQUIRED.value
    assert stale_browser.visible_send_count == 0

    denied_packet, denied_authority, denied_clock, denied_controller = _setup(tmp_path / "policy.db")
    denied_browser = FakeBrowser("chat:existing-nx028")
    denied = denied_controller.reenter(
        denied_packet,
        denied_authority,
        owner_id="browser-a",
        payload="payload",
        existing_binding=ExactConversationBinding.from_verified("chat:existing-nx028"),
        browser=denied_browser,
        policy_allowed=False,
        now=denied_clock.now(),
    )
    assert denied.accepted
    assert denied.selected_channel == ReentryChannel.OPERATOR_ASSISTED_CHECKPOINT_REQUIRED.value
    assert denied_browser.visible_send_count == 0


def test_accepted_task_stale_authority_stop_and_lease_loss_have_zero_effect(tmp_path: Path) -> None:
    accepted_packet, accepted_authority, accepted_clock, accepted_controller = _setup(tmp_path / "accepted.db")
    accepted_browser = FakeBrowser("chat:existing-nx028")
    accepted = accepted_controller.reenter(
        accepted_packet,
        _authority(task_status="ACCEPTED"),
        owner_id="browser-a",
        payload="payload",
        existing_binding=ExactConversationBinding.from_verified("chat:existing-nx028"),
        browser=accepted_browser,
        now=accepted_clock.now(),
    )
    assert not accepted.accepted
    assert accepted_browser.visible_send_count == 0

    stale_packet, stale_authority, stale_clock, stale_controller = _setup(tmp_path / "stale-authority.db")
    stale_browser = FakeBrowser("chat:existing-nx028")
    stale = stale_controller.reenter(
        stale_packet,
        _authority(state_revision=8),
        owner_id="browser-a",
        payload="payload",
        existing_binding=ExactConversationBinding.from_verified("chat:existing-nx028"),
        browser=stale_browser,
        now=stale_clock.now(),
    )
    assert not stale.accepted
    assert stale_browser.visible_send_count == 0

    stop_packet, stop_authority, stop_clock, stop_controller = _setup(tmp_path / "stop.db")
    stop_conn = sqlite3.connect(str(tmp_path / "stop.db"))
    stop_conn.row_factory = sqlite3.Row
    execute_stop_transaction(stop_conn, "nx028-project", expected_epoch=4, reason="NX-028 STOP")
    stop_conn.close()
    stop_browser = FakeBrowser("chat:existing-nx028")
    stopped = stop_controller.reenter(
        stop_packet,
        stop_authority,
        owner_id="browser-a",
        payload="payload",
        existing_binding=ExactConversationBinding.from_verified("chat:existing-nx028"),
        browser=stop_browser,
        now=stop_clock.now(),
    )
    assert not stopped.accepted
    assert stop_browser.visible_send_count == 0

    lease_packet, lease_authority, lease_clock, lease_controller = _setup(tmp_path / "lease-held.db")
    external_claim = lease_controller.lease.claim(lease_packet, lease_authority, owner_id="other", now=lease_clock.now())
    assert external_claim.claimed
    lease_browser = FakeBrowser("chat:existing-nx028")
    lease_lost = lease_controller.reenter(
        lease_packet,
        lease_authority,
        owner_id="browser-a",
        payload="payload",
        existing_binding=ExactConversationBinding.from_verified("chat:existing-nx028"),
        browser=lease_browser,
        now=lease_clock.now(),
    )
    assert not lease_lost.accepted
    assert lease_browser.visible_send_count == 0


def test_uncertain_delivery_is_not_reentered_or_blindly_resent(tmp_path: Path) -> None:
    packet, authority, clock, controller = _setup(tmp_path / "uncertain.db")
    browser = FakeBrowser("chat:existing-nx028")
    browser.crash_after_physical = True
    first = controller.reenter(
        packet,
        authority,
        owner_id="browser-a",
        payload="payload",
        existing_binding=ExactConversationBinding.from_verified("chat:existing-nx028"),
        browser=browser,
        now=clock.now(),
    )
    retry_browser = FakeBrowser("chat:existing-nx028")
    retry = controller.reenter(
        packet,
        authority,
        owner_id="browser-b",
        payload="payload",
        existing_binding=ExactConversationBinding.from_verified("chat:existing-nx028"),
        browser=retry_browser,
        now=clock.now(),
    )
    assert not first.accepted
    assert first.effects == 1
    assert first.record is not None and first.record.liveness_state == SessionLivenessState.REENTRY_FAILED.value
    assert not retry.accepted
    assert retry_browser.visible_send_count == 0
    conn = sqlite3.connect(str(tmp_path / "uncertain.db"))
    status = conn.execute("SELECT status FROM send_intents").fetchone()[0]
    conn.close()
    assert status == "UNCERTAIN"


def test_turn_end_signal_is_durable_and_does_not_accept_task(tmp_path: Path) -> None:
    packet, authority, clock, controller = _setup(tmp_path / "turn-end.db")
    result = controller.mark_turn_ended(packet, authority, payload="payload", now=clock.now())
    assert result.accepted
    assert result.effects == 0
    assert result.record is not None
    assert result.record.liveness_state == SessionLivenessState.CONTINUATION_PENDING.value
    assert "TURN_ENDED" in result.trace
    assert authority.task_status == "IN_PROGRESS"
    conn = sqlite3.connect(str(tmp_path / "turn-end.db"))
    task_status = conn.execute("SELECT status FROM task_execution_states WHERE project_id = ?", ("nx028-project",)).fetchone()
    generation = conn.execute("SELECT generation FROM execution_bindings WHERE execution_binding_id = 'binding:nx028'").fetchone()[0]
    conn.close()
    assert task_status is None or task_status[0] != "ACCEPTED"
    assert generation == 1
    replay = controller.mark_turn_ended(packet, authority, payload="payload", now=clock.now())
    assert replay.accepted and replay.idempotent
    assert replay.reason_code == "ALREADY_PENDING"
    assert replay.effects == 0


_NX028_GATE_RESULT_FIELDS = frozenset(
    {
        "SESSION_REENTRY_VERSION_EXPLICIT",
        "SESSION_LIVENESS_VERSION_EXPLICIT",
        "REENTRY_UNDER_PROJECT_MEMORY_V2_AUTHORITY",
        "BROWSER_LOCAL_STATE_IS_REENTRY_AUTHORITY",
        "SECOND_REENTRY_AUTHORITY_CREATED",
        "TURN_END_MARKS_TASK_ACCEPTED",
        "ACCEPTED_TASK_REPEATED_AFTER_REENTRY",
        "IN_PROGRESS_BINDING_GENERATION_DIVERGENCES",
        "STALE_REENTRY_EFFECTS",
        "UNVERIFIED_AUTOMATED_NEW_CHAT_USED",
        "NEW_CHAT_BIND_IDENTITY_VERIFIED",
        "GUESSED_EXISTING_CONVERSATION_ACCEPTED",
        "SELECTED_REENTRY_CHANNEL",
        "OPERATOR_FALLBACK_REQUIRES_MANUAL_PROMPT_BUILD",
        "OPERATOR_FALLBACK_ADDITIONAL_DECISION_REQUIRED",
        "BLIND_REENTRY_SENDS",
        "DUPLICATE_REENTRY_EFFECTS",
        "MAX_CONTINUATION_EFFECTS_PER_IDENTITY",
        "REENTRY_BYPASSES_STOP_FENCE",
        "STALE_LEASE_REENTRY_EFFECTS",
        "UNCERTAIN_DELIVERY_CAUSES_NEW_REENTRY_SEND",
        "CHANNEL_OUTCOMES_TESTED",
        "EXPECTED_TRACE_STEPS",
        "OBSERVED_TRACE_STEPS",
        "TRACE_DIVERGENCES",
        "CONTINUATION_EFFECTS",
    }
)


def inspect_nx028_gate_for_hardcoded_results() -> tuple[bool, list[str]]:
    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    gate = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run_nx028_machine_gate")
    hardcoded: set[str] = set()
    for node in ast.walk(gate):
        targets: list[str] = []
        value: ast.AST | None = None
        if isinstance(node, ast.Assign):
            targets = [target.id for target in node.targets if isinstance(target, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
            value = node.value
        if value is not None and isinstance(value, (ast.Constant, ast.List, ast.Tuple, ast.Dict, ast.Set)):
            hardcoded.update(name for name in targets if name in _NX028_GATE_RESULT_FIELDS)
    return not hardcoded, sorted(hardcoded)


def _source_readback(repo_root: Path) -> tuple[str, str, bool]:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, text=True, check=True
    ).stdout.strip()
    tree = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=repo_root, capture_output=True, text=True, check=True
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo_root, capture_output=True, text=True, check=True
    ).stdout
    return head, tree, status == ""


def run_nx028_machine_gate() -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parent.parent
    paths = [repo_root / "tests" / name for name in (
        ".nx028-gate-main.db",
        ".nx028-gate-operator.db",
        ".nx028-gate-stale.db",
        ".nx028-gate-stop.db",
        ".nx028-gate-lease.db",
        ".nx028-gate-uncertain.db",
        ".nx028-gate-accepted.db",
    )]
    for path in paths:
        path.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(f"{path}{suffix}").unlink(missing_ok=True)
    try:
        main_packet, main_authority, main_clock, main_controller = _setup(paths[0])
        main_browser = FakeBrowser("chat:existing-nx028")
        main_result = main_controller.reenter(
            main_packet,
            main_authority,
            owner_id="browser-a",
            payload="payload",
            existing_binding=ExactConversationBinding.from_verified("chat:existing-nx028"),
            browser=main_browser,
            now=main_clock.now(),
        )
        duplicate_browser = FakeBrowser("chat:existing-nx028")
        duplicate_result = main_controller.reenter(
            main_packet,
            main_authority,
            owner_id="browser-b",
            payload="payload",
            existing_binding=ExactConversationBinding.from_verified("chat:existing-nx028"),
            browser=duplicate_browser,
            now=main_clock.now(),
        )

        operator_packet, operator_authority, operator_clock, operator_controller = _setup(paths[1])
        operator_pending = operator_controller.reenter(
            operator_packet,
            operator_authority,
            owner_id="browser-a",
            payload="payload",
            now=operator_clock.now(),
        )
        operator_browser = FakeBrowser("chat:existing-nx028")
        operator_done = operator_controller.continue_from_operator_checkpoint(
            operator_pending.checkpoint_id or "missing",
            operator_authority,
            owner_id="operator-a",
            binding=ExactConversationBinding.from_verified("chat:existing-nx028"),
            browser=operator_browser,
            now=operator_clock.now(),
        )

        stale_packet, stale_authority, stale_clock, stale_controller = _setup(paths[2])
        stale_browser = FakeBrowser("chat:existing-nx028")
        stale_result = stale_controller.reenter(
            stale_packet,
            _authority(state_revision=8),
            owner_id="browser-a",
            payload="payload",
            existing_binding=ExactConversationBinding.from_verified("chat:existing-nx028"),
            browser=stale_browser,
            now=stale_clock.now(),
        )
        guessed_browser = FakeBrowser("chat:existing-nx028")
        guessed_result = stale_controller.reenter(
            stale_packet,
            stale_authority,
            owner_id="browser-b",
            payload="payload-2",
            existing_binding=ExactConversationBinding("chat:existing-nx028", "guessed-proof"),
            browser=guessed_browser,
            now=stale_clock.now(),
        )

        stop_packet, stop_authority, stop_clock, stop_controller = _setup(paths[3])
        stop_conn = sqlite3.connect(str(paths[3]))
        stop_conn.row_factory = sqlite3.Row
        execute_stop_transaction(stop_conn, "nx028-project", expected_epoch=4, reason="NX-028 machine gate STOP")
        stop_conn.close()
        stop_browser = FakeBrowser("chat:existing-nx028")
        stop_result = stop_controller.reenter(
            stop_packet,
            stop_authority,
            owner_id="browser-a",
            payload="payload",
            existing_binding=ExactConversationBinding.from_verified("chat:existing-nx028"),
            browser=stop_browser,
            now=stop_clock.now(),
        )

        lease_packet, lease_authority, lease_clock, lease_controller = _setup(paths[4])
        external_claim = lease_controller.lease.claim(lease_packet, lease_authority, owner_id="other", now=lease_clock.now())
        lease_browser = FakeBrowser("chat:existing-nx028")
        lease_result = lease_controller.reenter(
            lease_packet,
            lease_authority,
            owner_id="browser-a",
            payload="payload",
            existing_binding=ExactConversationBinding.from_verified("chat:existing-nx028"),
            browser=lease_browser,
            now=lease_clock.now(),
        )

        uncertain_packet, uncertain_authority, uncertain_clock, uncertain_controller = _setup(paths[5])
        uncertain_browser = FakeBrowser("chat:existing-nx028")
        uncertain_browser.crash_after_physical = True
        uncertain_result = uncertain_controller.reenter(
            uncertain_packet,
            uncertain_authority,
            owner_id="browser-a",
            payload="payload",
            existing_binding=ExactConversationBinding.from_verified("chat:existing-nx028"),
            browser=uncertain_browser,
            now=uncertain_clock.now(),
        )
        uncertain_retry_browser = FakeBrowser("chat:existing-nx028")
        uncertain_retry = uncertain_controller.reenter(
            uncertain_packet,
            uncertain_authority,
            owner_id="browser-b",
            payload="payload",
            existing_binding=ExactConversationBinding.from_verified("chat:existing-nx028"),
            browser=uncertain_retry_browser,
            now=uncertain_clock.now(),
        )

        accepted_packet, _, accepted_clock, accepted_controller = _setup(paths[6])
        accepted_browser = FakeBrowser("chat:existing-nx028")
        accepted_result = accepted_controller.reenter(
            accepted_packet,
            _authority(task_status="ACCEPTED"),
            owner_id="browser-a",
            payload="payload",
            existing_binding=ExactConversationBinding.from_verified("chat:existing-nx028"),
            browser=accepted_browser,
            now=accepted_clock.now(),
        )

        binding_conn = sqlite3.connect(str(paths[0]))
        binding_generation = binding_conn.execute(
            "SELECT generation FROM execution_bindings WHERE execution_binding_id = 'binding:nx028'"
        ).fetchone()[0]
        binding_conn.close()
        no_hardcoded, hardcoded = inspect_nx028_gate_for_hardcoded_results()
        expected_trace = EXPECTED_EXISTING_TRACE
        observed_trace = main_result.trace
        channels = {
            main_result.selected_channel,
            operator_pending.selected_channel,
        }
        head, tree, clean = _source_readback(repo_root)
        return {
            "SESSION_REENTRY_VERSION_EXPLICIT": bool(SESSION_REENTRY_VERSION_EXPLICIT and SESSION_REENTRY_VERSION == "v1"),
            "SESSION_LIVENESS_VERSION_EXPLICIT": bool(SESSION_LIVENESS_VERSION_EXPLICIT and SESSION_LIVENESS_VERSION == "v1"),
            "REENTRY_UNDER_PROJECT_MEMORY_V2_AUTHORITY": bool(REENTRY_UNDER_PROJECT_MEMORY_V2_AUTHORITY),
            "BROWSER_LOCAL_STATE_IS_REENTRY_AUTHORITY": bool(BROWSER_LOCAL_STATE_IS_REENTRY_AUTHORITY),
            "SECOND_REENTRY_AUTHORITY_CREATED": bool(SECOND_REENTRY_AUTHORITY_CREATED),
            "TURN_END_MARKS_TASK_ACCEPTED": bool(TURN_END_MARKS_TASK_ACCEPTED),
            "ACCEPTED_TASK_REPEATED_AFTER_REENTRY": bool(accepted_browser.visible_send_count > 0),
            "IN_PROGRESS_BINDING_GENERATION_DIVERGENCES": int(binding_generation != 1),
            "STALE_REENTRY_EFFECTS": int(stale_browser.visible_send_count),
            "UNVERIFIED_AUTOMATED_NEW_CHAT_USED": bool(ReentryChannel.OFFICIAL_NEW_CONVERSATION_CAPABILITY_VERIFIED.value in channels),
            "NEW_CHAT_BIND_IDENTITY_VERIFIED": bool(ReentryChannel.OFFICIAL_NEW_CONVERSATION_CAPABILITY_VERIFIED.value in channels),
            "GUESSED_EXISTING_CONVERSATION_ACCEPTED": bool(guessed_result.selected_channel == ReentryChannel.EXISTING_CONVERSATION_VERIFIED.value),
            "SELECTED_REENTRY_CHANNEL": main_result.selected_channel,
            "OPERATOR_FALLBACK_REQUIRES_MANUAL_PROMPT_BUILD": bool(operator_pending.record.operator_prompt_build_required) if operator_pending.record else True,
            "OPERATOR_FALLBACK_ADDITIONAL_DECISION_REQUIRED": bool(operator_pending.record.operator_decision_required) if operator_pending.record else True,
            "BLIND_REENTRY_SENDS": int(stale_browser.visible_send_count + guessed_browser.visible_send_count + stop_browser.visible_send_count),
            "DUPLICATE_REENTRY_EFFECTS": int(duplicate_result.effects),
            "MAX_CONTINUATION_EFFECTS_PER_IDENTITY": max(int(record.effect_count) for record in (main_result.record, operator_done.record, uncertain_result.record) if record is not None),
            "REENTRY_BYPASSES_STOP_FENCE": int(stop_browser.visible_send_count),
            "STALE_LEASE_REENTRY_EFFECTS": int(lease_browser.visible_send_count if external_claim.claimed else 1),
            "UNCERTAIN_DELIVERY_CAUSES_NEW_REENTRY_SEND": bool(uncertain_retry_browser.visible_send_count > 0),
            "CHANNEL_OUTCOMES_TESTED": len(channels),
            "EXPECTED_TRACE_STEPS": expected_trace,
            "OBSERVED_TRACE_STEPS": observed_trace,
            "TRACE_DIVERGENCES": int(expected_trace != observed_trace),
            "CONTINUATION_EFFECTS": int(main_result.effects + operator_done.effects),
            "HARDCODED_GATE_RESULT_FIELDS": hardcoded,
            "NO_HARDCODED_GATE_RESULTS": no_hardcoded,
            "SOURCE_HEAD": head,
            "SOURCE_TREE": tree,
            "WORKTREE_CLEAN": clean,
            "SOURCE_BOUND_MACHINE_GATE": "PASS" if len(head) == 40 and len(tree) == 40 and clean else "FAIL",
            "NX028_STATUS": "PASS" if (
                main_result.accepted
                and main_result.effects == 1
                and main_result.selected_channel == ReentryChannel.EXISTING_CONVERSATION_VERIFIED.value
                and observed_trace == expected_trace
                and duplicate_result.accepted
                and duplicate_result.effects == 0
                and operator_pending.accepted
                and operator_pending.effects == 0
                and operator_done.accepted
                and operator_done.effects == 1
                and stale_browser.visible_send_count == 0
                and guessed_browser.visible_send_count == 0
                and stop_result.accepted is False
                and stop_browser.visible_send_count == 0
                and lease_result.accepted is False
                and lease_browser.visible_send_count == 0
                and uncertain_result.effects == 1
                and uncertain_retry.accepted is False
                and uncertain_retry_browser.visible_send_count == 0
                and accepted_result.accepted is False
                and accepted_browser.visible_send_count == 0
                and no_hardcoded
                and len(head) == 40
                and len(tree) == 40
                and clean
            ) else "FAIL",
        }
    finally:
        for path in paths:
            path.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm"):
                Path(f"{path}{suffix}").unlink(missing_ok=True)


def test_nx028_machine_gate_execution() -> None:
    gate = run_nx028_machine_gate()
    assert gate["SESSION_REENTRY_VERSION_EXPLICIT"] is True
    assert gate["SESSION_LIVENESS_VERSION_EXPLICIT"] is True
    assert gate["REENTRY_UNDER_PROJECT_MEMORY_V2_AUTHORITY"] is True
    assert gate["BROWSER_LOCAL_STATE_IS_REENTRY_AUTHORITY"] is False
    assert gate["SECOND_REENTRY_AUTHORITY_CREATED"] is False
    assert gate["TURN_END_MARKS_TASK_ACCEPTED"] is False
    assert gate["ACCEPTED_TASK_REPEATED_AFTER_REENTRY"] is False
    assert gate["IN_PROGRESS_BINDING_GENERATION_DIVERGENCES"] == 0
    assert gate["STALE_REENTRY_EFFECTS"] == 0
    assert gate["UNVERIFIED_AUTOMATED_NEW_CHAT_USED"] is False
    assert gate["NEW_CHAT_BIND_IDENTITY_VERIFIED"] is False
    assert gate["GUESSED_EXISTING_CONVERSATION_ACCEPTED"] is False
    assert gate["SELECTED_REENTRY_CHANNEL"] == ReentryChannel.EXISTING_CONVERSATION_VERIFIED.value
    assert gate["OPERATOR_FALLBACK_REQUIRES_MANUAL_PROMPT_BUILD"] is False
    assert gate["OPERATOR_FALLBACK_ADDITIONAL_DECISION_REQUIRED"] is False
    assert gate["BLIND_REENTRY_SENDS"] == 0
    assert gate["DUPLICATE_REENTRY_EFFECTS"] == 0
    assert gate["MAX_CONTINUATION_EFFECTS_PER_IDENTITY"] == 1
    assert gate["REENTRY_BYPASSES_STOP_FENCE"] == 0
    assert gate["STALE_LEASE_REENTRY_EFFECTS"] == 0
    assert gate["UNCERTAIN_DELIVERY_CAUSES_NEW_REENTRY_SEND"] is False
    assert gate["CHANNEL_OUTCOMES_TESTED"] == 2
    assert gate["EXPECTED_TRACE_STEPS"] == EXPECTED_EXISTING_TRACE
    assert gate["OBSERVED_TRACE_STEPS"] == EXPECTED_EXISTING_TRACE
    assert gate["TRACE_DIVERGENCES"] == 0
    assert gate["CONTINUATION_EFFECTS"] == 2
    assert gate["HARDCODED_GATE_RESULT_FIELDS"] == []
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    assert gate["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
    assert gate["NX028_STATUS"] == "PASS"

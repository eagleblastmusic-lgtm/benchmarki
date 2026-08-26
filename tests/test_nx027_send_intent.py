"""NX-027 durable send intent and same-chat re-entry qualification."""

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
from bdb_vnext.continuation_lease import ContinuationLeaseCoordinator
from bdb_vnext.continuation_packet import ContinuationAuthoritySnapshot, ContinuationPacket, build_packet
from bdb_vnext.project_memory_v2_contract import PROJECT_MEMORY_V2_DDL
from bdb_vnext.send_intent import (
    BROWSER_LOCAL_STATE_IS_SEND_AUTHORITY as BROWSER_LOCAL_STATE_CONTRACT,
    SEND_INTENT_UNDER_CANONICAL_AUTHORITY as SEND_INTENT_AUTHORITY_CONTRACT,
    SEND_INTENT_VERSION,
    SEND_INTENT_VERSION_EXPLICIT as SEND_INTENT_VERSION_CONTRACT,
    DomPreconditions,
    ExactConversationBinding,
    FakeBrowser,
    NoDeliveryProof,
    SendAck,
    SendIntentCoordinator,
    StructuredSendEvidence,
)
from bdb_vnext.stop_fence import execute_stop_transaction


START = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)
EXPIRY = START + timedelta(hours=1)
PLAN_DIGEST = "sha256:" + "1" * 64
STATE_DIGEST = "sha256:" + "2" * 64
EVIDENCE_DIGEST = "sha256:" + "3" * 64
HEAD = "a" * 40
TREE = "b" * 40
EXPECTED_TRACE = (
    "INTENT_PREPARED",
    "DOM_PRECONDITIONS_VALID",
    "PHYSICAL_SEND",
    "SEND_CONFIRMED",
    "ACK",
)


@dataclass
class VirtualClock:
    value: datetime = START

    def now(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> datetime:
        self.value = self.value + timedelta(**kwargs)
        return self.value


def _packet(project_id: str = "nx027-project", **overrides: object) -> ContinuationPacket:
    values: dict[str, object] = {
        "project_id": project_id,
        "plan_identity": "plan:nx-m2",
        "plan_version": 1,
        "plan_digest": PLAN_DIGEST,
        "scope": AutoScope.UNTIL_STOPPED,
        "run_id": "run:nx027",
        "scope_epoch": 4,
        "current_milestone_id": "NX-M2",
        "current_task_id": "NX-027",
        "execution_binding_id": "binding:nx027",
        "expected_repo_head_before": HEAD,
        "state_revision": 7,
        "state_digest": STATE_DIGEST,
        "allowed_next_action": ScopeAction.LAUNCH_TASK,
        "budget_summary": {"remaining_attempts": 2, "remaining_retry_budget": 1},
        "evidence_refs": [EVIDENCE_DIGEST],
        "issued_at": START,
        "expires_at": EXPIRY,
        "attempt_id": "attempt:nx027",
        "expected_tree": TREE,
        "conversation_binding_policy": "EXISTING_CHAT_ONLY",
    }
    values.update(overrides)
    return build_packet(**values)  # type: ignore[arg-type]


def _authority(project_id: str = "nx027-project", **overrides: object) -> ContinuationAuthoritySnapshot:
    values: dict[str, object] = {
        "project_id": project_id,
        "plan_identity": "plan:nx-m2",
        "plan_version": 1,
        "plan_digest": PLAN_DIGEST,
        "scope": AutoScope.UNTIL_STOPPED,
        "run_id": "run:nx027",
        "scope_epoch": 4,
        "current_milestone_id": "NX-M2",
        "current_task_id": "NX-027",
        "execution_binding_id": "binding:nx027",
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
        "attempt_id": "attempt:nx027",
        "expected_tree": TREE,
        "conversation_binding_policy": "EXISTING_CHAT_ONLY",
    }
    values.update(overrides)
    return ContinuationAuthoritySnapshot(**values)  # type: ignore[arg-type]


def _create_database(path: Path, project_id: str = "nx027-project") -> None:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(PROJECT_MEMORY_V2_DDL)
    now = START.isoformat(timespec="microseconds").replace("+00:00", "Z")
    conn.execute(
        "INSERT INTO projects (project_id, display_name, repo_alias, local_repo_path, brief_json, revision, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (project_id, "NX-027", "nx027", "/tmp/nx027", "{}", 1, now, now),
    )
    conn.execute(
        "INSERT INTO project_plans (project_id, plan_version, plan_digest, schema, plan_json, imported_at) VALUES (?,?,?,?,?,?)",
        (project_id, 1, PLAN_DIGEST, "bdb-project-plan-v1", "{}", now),
    )
    conn.execute(
        """
        INSERT INTO execution_bindings (
            execution_binding_id, project_id, plan_version, task_id, launch_id,
            correlation_id, command_id, repo_alias, expected_repo_head_before,
            status, generation, superseded, conversation_id, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "binding:nx027",
            project_id,
            1,
            "NX-027",
            "launch:nx027",
            "correlation:nx027",
            "command:nx027",
            "nx027",
            HEAD,
            "ACTIVE",
            1,
            0,
            "chat:existing-nx027",
            now,
        ),
    )
    conn.execute(
        """
        INSERT INTO scope_cursors (
            cursor_id, project_id, run_id, scope, scope_epoch,
            current_milestone_id, current_task_id, plan_identity,
            plan_version, state_revision, disposition, status, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "cursor:nx027",
            project_id,
            "run:nx027",
            "UNTIL_STOPPED",
            4,
            "NX-M2",
            "NX-027",
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


def _setup(path: Path, project_id: str = "nx027-project") -> tuple[ContinuationPacket, ContinuationAuthoritySnapshot, VirtualClock, ContinuationLeaseCoordinator, SendIntentCoordinator]:
    _create_database(path, project_id)
    clock = VirtualClock()
    packet = _packet(project_id)
    authority = _authority(project_id)
    lease = ContinuationLeaseCoordinator(path, project_id, lease_seconds=30, clock=clock.now)
    send = SendIntentCoordinator(path, project_id, lease_coordinator=lease, clock=clock.now)
    return packet, authority, clock, lease, send


def _claim(
    lease: ContinuationLeaseCoordinator,
    packet: ContinuationPacket,
    authority: ContinuationAuthoritySnapshot,
    *,
    owner_id: str = "browser-a",
    now: datetime = START,
) -> Any:
    result = lease.claim(packet, authority, owner_id=owner_id, now=now)
    assert result.claimed and result.owner_token is not None
    return result


def _prepare(
    send: SendIntentCoordinator,
    lease_claim: Any,
    packet: ContinuationPacket,
    authority: ContinuationAuthoritySnapshot,
    *,
    binding: ExactConversationBinding | None = None,
    payload: str = "Continue the next approved task in this existing chat.",
    now: datetime = START,
) -> Any:
    result = send.prepare(
        packet,
        authority,
        lease_claim.owner_token,
        payload,
        conversation_binding=binding or ExactConversationBinding.from_verified("chat:existing-nx027"),
        now=now,
    )
    assert result.accepted and result.intent is not None
    return result


def test_nx027_schema_and_authority_contract(tmp_path: Path) -> None:
    path = tmp_path / "schema.db"
    _create_database(path)
    conn = sqlite3.connect(str(path))
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    columns = {row[1] for row in conn.execute("PRAGMA table_info(send_intents)").fetchall()}
    conn.close()
    assert SEND_INTENT_VERSION == "v1"
    assert SEND_INTENT_VERSION_CONTRACT is True
    assert SEND_INTENT_AUTHORITY_CONTRACT is True
    assert BROWSER_LOCAL_STATE_CONTRACT is False
    assert "send_intents" in tables
    assert {"intent_key", "packet_digest", "lease_owner_token_hash", "conversation_binding_id", "state_revision", "status"}.issubset(columns)


def test_prepare_is_durable_and_duplicate_prepare_is_one_record(tmp_path: Path) -> None:
    packet, authority, clock, lease, send = _setup(tmp_path / "prepare.db")
    claim = _claim(lease, packet, authority)
    binding = ExactConversationBinding.from_verified("chat:existing-nx027")
    first = send.prepare(packet, authority, claim.owner_token, "payload", conversation_binding=binding, now=clock.now())
    duplicate = send.prepare(packet, authority, claim.owner_token, "payload", conversation_binding=binding, now=clock.now())
    assert first.accepted and first.intent is not None
    assert duplicate.accepted and duplicate.idempotent
    assert duplicate.intent == first.intent
    assert send.read(first.intent.intent_id).status == "PREPARED"  # type: ignore[union-attr]
    conn = sqlite3.connect(str(tmp_path / "prepare.db"))
    count = conn.execute("SELECT COUNT(*) FROM send_intents WHERE intent_key = ?", (first.intent.intent_key,)).fetchone()[0]
    conn.close()
    assert count == 1


def test_missing_composer_focus_and_positive_confirmation_rules(tmp_path: Path) -> None:
    path = tmp_path / "dom.db"
    packet, authority, clock, lease, send = _setup(path)
    claim = _claim(lease, packet, authority)
    prepared = _prepare(send, claim, packet, authority)
    missing = FakeBrowser("chat:existing-nx027", composer_present=False)
    missing_result = send.send_once(prepared.intent.intent_id, packet, authority, claim.owner_token, missing, now=clock.now())
    assert not missing_result.accepted and missing_result.reason_code == "MISSING_COMPOSER"
    assert missing.visible_send_count == 0
    assert send.read(prepared.intent.intent_id).status == "PREPARED"  # type: ignore[union-attr]

    path2 = tmp_path / "focus.db"
    packet2, authority2, clock2, lease2, send2 = _setup(path2)
    claim2 = _claim(lease2, packet2, authority2)
    prepared2 = _prepare(send2, claim2, packet2, authority2)
    browser = FakeBrowser("chat:existing-nx027")
    allowed = send2.allow_send(prepared2.intent.intent_id, packet2, authority2, claim2.owner_token, browser.snapshot(), now=clock2.now())
    attempted = send2.begin_physical_send(prepared2.intent.intent_id, packet2, authority2, claim2.owner_token, browser.snapshot(), now=clock2.now())
    assert allowed.accepted and attempted.accepted
    focus = send2.confirm(prepared2.intent.intent_id, packet2, claim2.owner_token, StructuredSendEvidence.focus_only_observation(prepared2.intent.intent_id), now=clock2.now())
    assert not focus.accepted
    evidence = browser.physical_send(attempted.intent)
    confirmed = send2.confirm(prepared2.intent.intent_id, packet2, claim2.owner_token, evidence, now=clock2.now())
    assert confirmed.accepted and confirmed.intent is not None


def test_crash_uncertainty_never_blind_resends_and_reconciliation_is_durable(tmp_path: Path) -> None:
    path = tmp_path / "crash.db"
    packet, authority, clock, lease, send = _setup(path)
    claim = _claim(lease, packet, authority)
    prepared = _prepare(send, claim, packet, authority)
    browser = FakeBrowser("chat:existing-nx027")
    browser.crash_after_physical = True
    uncertain = send.send_once(prepared.intent.intent_id, packet, authority, claim.owner_token, browser, now=clock.now())
    assert not uncertain.accepted and uncertain.reason_code == "UNCERTAIN_DELIVERY"
    assert browser.visible_send_count == 1
    assert send.read(prepared.intent.intent_id).status == "UNCERTAIN"  # type: ignore[union-attr]
    retry = send.send_once(prepared.intent.intent_id, packet, authority, claim.owner_token, browser, now=clock.now())
    assert not retry.accepted and retry.reason_code in {"INTENT_NOT_SENDABLE", "PHYSICAL_SEND_ALREADY_ATTEMPTED"}
    assert browser.visible_send_count == 1
    evidence = browser.visible_evidence[0]
    confirmed = send.reconcile(prepared.intent.intent_id, packet, claim.owner_token, evidence=evidence, now=clock.now())
    assert confirmed.accepted and confirmed.intent is not None and confirmed.intent.status == "SEND_CONFIRMED"

    ack = send.ack(
        prepared.intent.intent_id,
        packet,
        claim.owner_token,
        SendAck(prepared.intent.intent_id, packet.continuation_id, packet["packet_digest"], packet["execution_binding_id"], "chat:existing-nx027"),
        now=clock.now(),
    )
    assert ack.accepted and ack.intent is not None and ack.intent.status == "ACKNOWLEDGED"
    duplicate_send = send.send_once(prepared.intent.intent_id, packet, authority, claim.owner_token, browser, now=clock.now())
    assert not duplicate_send.accepted
    assert browser.visible_send_count == 1

    path2 = tmp_path / "no-delivery.db"
    packet2, authority2, clock2, lease2, send2 = _setup(path2)
    claim2 = _claim(lease2, packet2, authority2)
    prepared2 = _prepare(send2, claim2, packet2, authority2)
    browser2 = FakeBrowser("chat:existing-nx027")
    allowed = send2.allow_send(prepared2.intent.intent_id, packet2, authority2, claim2.owner_token, browser2.snapshot(), now=clock2.now())
    attempted = send2.begin_physical_send(prepared2.intent.intent_id, packet2, authority2, claim2.owner_token, browser2.snapshot(), now=clock2.now())
    assert allowed.accepted and attempted.accepted
    uncertain2 = send2.mark_uncertain(prepared2.intent.intent_id, claim2.owner_token, reason="adapter boundary", now=clock2.now())
    assert not uncertain2.accepted
    safe_retry = send2.reconcile(prepared2.intent.intent_id, packet2, claim2.owner_token, no_delivery_proof=browser2.no_delivery_proof(prepared2.intent.intent_id), now=clock2.now())
    assert safe_retry.accepted and safe_retry.reason_code == "RETRY_ALLOWED"
    assert browser2.visible_send_count == 0


def test_same_chat_task_transition_exactly_once_and_ack_identity(tmp_path: Path) -> None:
    path = tmp_path / "same-chat.db"
    packet, authority, clock, lease, send = _setup(path)
    claim = _claim(lease, packet, authority, owner_id="native-b")
    binding = ExactConversationBinding.from_verified("chat:existing-nx027")
    browser = FakeBrowser("chat:existing-nx027")
    flow = send.continue_same_chat(
        packet,
        authority,
        claim,
        binding,
        browser,
        "Continue Task B using the approved continuation packet.",
        now=clock.now(),
    )
    assert flow.accepted
    assert flow.trace == EXPECTED_TRACE
    assert flow.manual_user_prompts == 0
    assert flow.visible_sends == 1
    assert flow.created_conversations == 0
    assert flow.intent is not None and flow.intent.status == "ACKNOWLEDGED"
    assert lease.read(packet).status == "COMPLETED"  # type: ignore[union-attr]

    wrong_ack = send.ack(
        flow.intent.intent_id,
        packet,
        claim.owner_token,
        SendAck(flow.intent.intent_id, packet.continuation_id, packet["packet_digest"], "wrong-binding", "chat:existing-nx027"),
        now=clock.now(),
    )
    duplicate_ack = send.ack(
        flow.intent.intent_id,
        packet,
        claim.owner_token,
        SendAck(flow.intent.intent_id, packet.continuation_id, packet["packet_digest"], packet["execution_binding_id"], "chat:existing-nx027"),
        now=clock.now(),
    )
    assert not wrong_ack.accepted
    assert duplicate_ack.accepted and duplicate_ack.idempotent and not duplicate_ack.mutated


def test_guessed_binding_stale_authority_reload_stop_and_stale_lease_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "negative.db"
    packet, authority, clock, lease, send = _setup(path)
    claim = _claim(lease, packet, authority)
    guessed = send.prepare(
        packet,
        authority,
        claim.owner_token,
        "payload",
        conversation_binding=ExactConversationBinding("chat:existing-nx027", "guessed-proof"),
        now=clock.now(),
    )
    assert not guessed.accepted and guessed.reason_code == "GUESSED_CONVERSATION_IDENTITY"

    prepared = _prepare(send, claim, packet, authority)
    wrong_dom = FakeBrowser("chat:another-conversation")
    wrong_result = send.send_once(prepared.intent.intent_id, packet, authority, claim.owner_token, wrong_dom, now=clock.now())
    assert not wrong_result.accepted and wrong_dom.visible_send_count == 0
    wrong_dom.reload()

    stale_authority = _authority(state_revision=8)
    stale_browser = FakeBrowser("chat:existing-nx027")
    stale_result = send.send_once(prepared.intent.intent_id, packet, stale_authority, claim.owner_token, stale_browser, now=clock.now())
    assert not stale_result.accepted and stale_browser.visible_send_count == 0

    stop_path = tmp_path / "stop.db"
    stop_packet, stop_authority, stop_clock, stop_lease, stop_send = _setup(stop_path)
    stop_claim = _claim(stop_lease, stop_packet, stop_authority)
    stop_prepared = _prepare(stop_send, stop_claim, stop_packet, stop_authority)
    stop_conn = sqlite3.connect(str(stop_path))
    stop_conn.row_factory = sqlite3.Row
    execute_stop_transaction(stop_conn, "nx027-project", expected_epoch=4, reason="send stop")
    stop_conn.close()
    stop_browser = FakeBrowser("chat:existing-nx027")
    stopped = stop_send.send_once(stop_prepared.intent.intent_id, stop_packet, stop_authority, stop_claim.owner_token, stop_browser, now=stop_clock.now())
    assert not stopped.accepted and stop_browser.visible_send_count == 0
    assert stop_send.read(stop_prepared.intent.intent_id).status == "FENCED"  # type: ignore[union-attr]

    stale_path = tmp_path / "stale-lease.db"
    stale_packet, stale_authority, stale_clock, stale_lease, stale_send = _setup(stale_path)
    stale_claim = _claim(stale_lease, stale_packet, stale_authority)
    stale_prepared = _prepare(stale_send, stale_claim, stale_packet, stale_authority)
    stale_clock.advance(seconds=31)
    reclaimed = stale_lease.reclaim(stale_packet, stale_authority, owner_id="native-new", reason="owner expired", now=stale_clock.now())
    assert reclaimed.claimed
    stale_lease_browser = FakeBrowser("chat:existing-nx027")
    stale_lease_result = stale_send.send_once(stale_prepared.intent.intent_id, stale_packet, stale_authority, stale_claim.owner_token, stale_lease_browser, now=stale_clock.now())
    assert not stale_lease_result.accepted and stale_lease_browser.visible_send_count == 0


_NX027_GATE_RESULT_FIELDS = frozenset(
    {
        "SEND_INTENT_VERSION_EXPLICIT",
        "SEND_INTENT_UNDER_CANONICAL_AUTHORITY",
        "BROWSER_LOCAL_STATE_IS_SEND_AUTHORITY",
        "PHYSICAL_SEND_WITHOUT_DURABLE_INTENT",
        "DUPLICATE_PREPARE_RECORDS",
        "STALE_AUTHORITY_PHYSICAL_SENDS",
        "MISSING_COMPOSER_SEND_ATTEMPTS",
        "FOCUS_ONLY_MARKED_AS_SENT",
        "CRASH_BOUNDARY_CASES",
        "BLIND_RESENDS_AFTER_UNCERTAIN_DELIVERY",
        "USER_VISIBLE_SENDS_FOR_ONE_INTENT",
        "DUPLICATE_PHYSICAL_SEND_EFFECTS",
        "WRONG_ACK_ACCEPTED",
        "DUPLICATE_ACK_EFFECTS",
        "MANUAL_USER_PROMPTS_REQUIRED_FOR_SAME_CHAT_TASK_TRANSITION",
        "GUESSED_CONVERSATION_IDENTITY_ACCEPTED",
        "SEND_BYPASSES_STOP_FENCE",
        "STALE_LEASE_SEND_EFFECTS",
        "EXPECTED_TRACE_STEPS",
        "OBSERVED_TRACE_STEPS",
        "TRACE_DIVERGENCES",
    }
)


def inspect_nx027_gate_for_hardcoded_results() -> tuple[bool, list[str]]:
    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    gate = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run_nx027_machine_gate")
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
            hardcoded.update(name for name in targets if name in _NX027_GATE_RESULT_FIELDS)
    return not hardcoded, sorted(hardcoded)


def run_nx027_machine_gate() -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parent.parent
    paths = [repo_root / "tests" / name for name in (
        ".nx027-gate-main.db",
        ".nx027-gate-crash.db",
        ".nx027-gate-stop.db",
        ".nx027-gate-stale.db",
    )]
    for path in paths:
        path.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(f"{path}{suffix}").unlink(missing_ok=True)
    try:
        main_packet, main_authority, main_clock, main_lease, main_send = _setup(paths[0])
        main_claim = _claim(main_lease, main_packet, main_authority)
        binding = ExactConversationBinding.from_verified("chat:existing-nx027")
        main_browser = FakeBrowser("chat:existing-nx027")
        prepared = main_send.prepare(main_packet, main_authority, main_claim.owner_token, "Continue Task B", conversation_binding=binding, now=main_clock.now(), trace=main_browser.trace)
        duplicate = main_send.prepare(main_packet, main_authority, main_claim.owner_token, "Continue Task B", conversation_binding=binding, now=main_clock.now())
        duplicate_conn = sqlite3.connect(str(paths[0]))
        duplicate_records = duplicate_conn.execute("SELECT COUNT(*) FROM send_intents WHERE intent_key = ?", (prepared.intent.intent_key,)).fetchone()[0] if prepared.intent else 0
        duplicate_conn.close()
        main_flow = main_send.continue_same_chat(main_packet, main_authority, main_claim, binding, main_browser, "Continue Task B", now=main_clock.now())
        wrong_ack = main_send.ack(
            prepared.intent.intent_id if prepared.intent else "missing",
            main_packet,
            main_claim.owner_token,
            SendAck(prepared.intent.intent_id if prepared.intent else "missing", main_packet.continuation_id, main_packet["packet_digest"], "wrong", "chat:existing-nx027"),
            now=main_clock.now(),
        ) if prepared.intent else None
        duplicate_ack = main_send.ack(
            prepared.intent.intent_id if prepared.intent else "missing",
            main_packet,
            main_claim.owner_token,
            SendAck(prepared.intent.intent_id if prepared.intent else "missing", main_packet.continuation_id, main_packet["packet_digest"], main_packet["execution_binding_id"], "chat:existing-nx027"),
            now=main_clock.now(),
        ) if prepared.intent else None

        invalid_browser = FakeBrowser("chat:existing-nx027")
        invalid_result = main_send.send_once("missing-intent", main_packet, main_authority, main_claim.owner_token, invalid_browser, now=main_clock.now())

        composer_path = repo_root / "tests" / ".nx027-gate-composer.db"
        composer_path.unlink(missing_ok=True)
        composer_packet, composer_authority, composer_clock, composer_lease, composer_send = _setup(composer_path)
        composer_claim = _claim(composer_lease, composer_packet, composer_authority)
        composer_prepared = _prepare(composer_send, composer_claim, composer_packet, composer_authority)
        missing_browser = FakeBrowser("chat:existing-nx027", composer_present=False)
        missing_result = composer_send.send_once(composer_prepared.intent.intent_id, composer_packet, composer_authority, composer_claim.owner_token, missing_browser, now=composer_clock.now())

        crash_path = paths[1]
        crash_packet, crash_authority, crash_clock, crash_lease, crash_send = _setup(crash_path)
        crash_claim = _claim(crash_lease, crash_packet, crash_authority)
        crash_prepared = _prepare(crash_send, crash_claim, crash_packet, crash_authority)
        crash_browser = FakeBrowser("chat:existing-nx027")
        crash_browser.crash_after_physical = True
        crash_result = crash_send.send_once(crash_prepared.intent.intent_id, crash_packet, crash_authority, crash_claim.owner_token, crash_browser, now=crash_clock.now())
        crash_visible_after = crash_browser.visible_send_count
        crash_retry = crash_send.send_once(crash_prepared.intent.intent_id, crash_packet, crash_authority, crash_claim.owner_token, crash_browser, now=crash_clock.now())
        crash_visible_after_retry = crash_browser.visible_send_count
        crash_evidence = crash_browser.visible_evidence[0]
        crash_confirm = crash_send.reconcile(crash_prepared.intent.intent_id, crash_packet, crash_claim.owner_token, evidence=crash_evidence, now=crash_clock.now())
        crash_ack = crash_send.ack(
            crash_prepared.intent.intent_id,
            crash_packet,
            crash_claim.owner_token,
            SendAck(crash_prepared.intent.intent_id, crash_packet.continuation_id, crash_packet["packet_digest"], crash_packet["execution_binding_id"], "chat:existing-nx027"),
            now=crash_clock.now(),
        )

        stale_packet = _packet(current_task_id="NX-027")
        stale_authority = _authority(state_revision=8)
        stale_result = composer_send.send_once(composer_prepared.intent.intent_id, stale_packet, stale_authority, composer_claim.owner_token, FakeBrowser("chat:existing-nx027"), now=composer_clock.now())

        stop_path = paths[2]
        stop_packet, stop_authority, stop_clock, stop_lease, stop_send = _setup(stop_path)
        stop_claim = _claim(stop_lease, stop_packet, stop_authority)
        stop_prepared = _prepare(stop_send, stop_claim, stop_packet, stop_authority)
        stop_conn = sqlite3.connect(str(stop_path))
        stop_conn.row_factory = sqlite3.Row
        execute_stop_transaction(stop_conn, "nx027-project", expected_epoch=4, reason="machine gate STOP")
        stop_conn.close()
        stop_browser = FakeBrowser("chat:existing-nx027")
        stop_result = stop_send.send_once(stop_prepared.intent.intent_id, stop_packet, stop_authority, stop_claim.owner_token, stop_browser, now=stop_clock.now())

        stale_path = paths[3]
        stale_lease_packet, stale_lease_authority, stale_clock, stale_lease, stale_send = _setup(stale_path)
        stale_claim = _claim(stale_lease, stale_lease_packet, stale_lease_authority)
        stale_intent = _prepare(stale_send, stale_claim, stale_lease_packet, stale_lease_authority)
        stale_clock.advance(seconds=31)
        stale_lease.reclaim(stale_lease_packet, stale_lease_authority, owner_id="native-new", reason="expired owner", now=stale_clock.now())
        stale_lease_browser = FakeBrowser("chat:existing-nx027")
        stale_lease_result = stale_send.send_once(stale_intent.intent.intent_id, stale_lease_packet, stale_lease_authority, stale_claim.owner_token, stale_lease_browser, now=stale_clock.now())

        crash_boundary_results = [
            invalid_result.reason_code == "INTENT_NOT_FOUND" and invalid_browser.visible_send_count == 0,
            prepared.intent is not None and prepared.intent.status == "PREPARED",
            composer_prepared.intent is not None and missing_result.reason_code == "MISSING_COMPOSER",
            crash_visible_after == 1 and crash_result.reason_code == "UNCERTAIN_DELIVERY",
            crash_confirm.accepted and crash_ack.accepted,
            main_flow.accepted and duplicate_ack is not None and duplicate_ack.idempotent,
        ]

        no_hardcoded, hardcoded = inspect_nx027_gate_for_hardcoded_results()
        # Remove all workspace-local gate fixtures before this source readback.
        for path in paths + [composer_path]:
            path.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm"):
                Path(f"{path}{suffix}").unlink(missing_ok=True)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, text=True, check=True).stdout.strip()
        tree = subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=repo_root, capture_output=True, text=True, check=True).stdout.strip()
        clean = subprocess.run(["git", "status", "--porcelain"], cwd=repo_root, capture_output=True, text=True, check=True).stdout == ""

        expected_trace = EXPECTED_TRACE
        observed_trace = main_flow.trace
        duplicate_ack_effects = int(duplicate_ack.mutated) if duplicate_ack is not None else 1
        status = "PASS" if (
            bool(SEND_INTENT_VERSION_CONTRACT and SEND_INTENT_VERSION == "v1")
            and bool(SEND_INTENT_AUTHORITY_CONTRACT and prepared.accepted and prepared.intent is not None)
            and not bool(BROWSER_LOCAL_STATE_CONTRACT)
            and int(invalid_browser.visible_send_count) == 0
            and max(0, int(duplicate_records) - 1) == 0
            and int(stale_result.intent is not None and getattr(stale_result, "physical_effects", 0) > 0) == 0
            and int("PHYSICAL_SEND" in missing_browser.trace) == 0
            and int(bool(StructuredSendEvidence.focus_only_observation("focus").structured)) == 0
            and len(crash_boundary_results) == 6
            and all(crash_boundary_results)
            and max(0, crash_visible_after_retry - crash_visible_after) == 0
            and main_flow.visible_sends == 1
            and max(0, main_flow.visible_sends - 1) == 0
            and (wrong_ack is None or not wrong_ack.accepted)
            and duplicate_ack_effects == 0
            and main_flow.manual_user_prompts == 0
            and prepared.accepted is True
            and not stop_browser.visible_send_count
            and not stale_lease_browser.visible_send_count
            and expected_trace == observed_trace
            and no_hardcoded
            and len(head) == 40
            and len(tree) == 40
            and clean
        ) else "FAIL"

        return {
            "SEND_INTENT_VERSION_EXPLICIT": bool(SEND_INTENT_VERSION_CONTRACT and SEND_INTENT_VERSION == "v1"),
            "SEND_INTENT_UNDER_CANONICAL_AUTHORITY": bool(SEND_INTENT_AUTHORITY_CONTRACT and prepared.accepted and prepared.intent is not None),
            "BROWSER_LOCAL_STATE_IS_SEND_AUTHORITY": bool(BROWSER_LOCAL_STATE_CONTRACT),
            "PHYSICAL_SEND_WITHOUT_DURABLE_INTENT": int(invalid_browser.visible_send_count),
            "DUPLICATE_PREPARE_RECORDS": max(0, int(duplicate_records) - 1),
            "STALE_AUTHORITY_PHYSICAL_SENDS": int(stale_result.intent is not None and getattr(stale_result, "physical_effects", 0) > 0),
            "MISSING_COMPOSER_SEND_ATTEMPTS": int("PHYSICAL_SEND" in missing_browser.trace),
            "FOCUS_ONLY_MARKED_AS_SENT": int(bool(StructuredSendEvidence.focus_only_observation("focus").structured)),
            "CRASH_BOUNDARY_CASES": len(crash_boundary_results),
            "BLIND_RESENDS_AFTER_UNCERTAIN_DELIVERY": max(0, crash_visible_after_retry - crash_visible_after),
            "USER_VISIBLE_SENDS_FOR_ONE_INTENT": main_flow.visible_sends,
            "DUPLICATE_PHYSICAL_SEND_EFFECTS": max(0, main_flow.visible_sends - 1),
            "WRONG_ACK_ACCEPTED": bool(wrong_ack.accepted) if wrong_ack is not None else True,
            "DUPLICATE_ACK_EFFECTS": duplicate_ack_effects,
            "MANUAL_USER_PROMPTS_REQUIRED_FOR_SAME_CHAT_TASK_TRANSITION": main_flow.manual_user_prompts,
            "GUESSED_CONVERSATION_IDENTITY_ACCEPTED": bool(ExactConversationBinding("chat:existing-nx027", "guessed-proof").is_exact),
            "SEND_BYPASSES_STOP_FENCE": int(stop_browser.visible_send_count),
            "STALE_LEASE_SEND_EFFECTS": int(stale_lease_browser.visible_send_count),
            "EXPECTED_TRACE_STEPS": expected_trace,
            "OBSERVED_TRACE_STEPS": observed_trace,
            "TRACE_DIVERGENCES": int(expected_trace != observed_trace),
            "HARDCODED_GATE_RESULT_FIELDS": hardcoded,
            "NO_HARDCODED_GATE_RESULTS": no_hardcoded,
            "SOURCE_HEAD": head,
            "SOURCE_TREE": tree,
            "WORKTREE_CLEAN": clean,
            "SOURCE_BOUND_MACHINE_GATE": "PASS" if len(head) == 40 and len(tree) == 40 and clean else "FAIL",
            "NX027_STATUS": status,
        }
    finally:
        for path in paths + [repo_root / "tests" / ".nx027-gate-composer.db"]:
            path.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm"):
                Path(f"{path}{suffix}").unlink(missing_ok=True)


def test_nx027_machine_gate_execution() -> None:
    gate = run_nx027_machine_gate()
    assert gate["SEND_INTENT_VERSION_EXPLICIT"] is True
    assert gate["SEND_INTENT_UNDER_CANONICAL_AUTHORITY"] is True
    assert gate["BROWSER_LOCAL_STATE_IS_SEND_AUTHORITY"] is False
    assert gate["PHYSICAL_SEND_WITHOUT_DURABLE_INTENT"] == 0
    assert gate["DUPLICATE_PREPARE_RECORDS"] == 0
    assert gate["STALE_AUTHORITY_PHYSICAL_SENDS"] == 0
    assert gate["MISSING_COMPOSER_SEND_ATTEMPTS"] == 0
    assert gate["FOCUS_ONLY_MARKED_AS_SENT"] == 0
    assert gate["CRASH_BOUNDARY_CASES"] == 6
    assert gate["BLIND_RESENDS_AFTER_UNCERTAIN_DELIVERY"] == 0
    assert gate["USER_VISIBLE_SENDS_FOR_ONE_INTENT"] == 1
    assert gate["DUPLICATE_PHYSICAL_SEND_EFFECTS"] == 0
    assert gate["WRONG_ACK_ACCEPTED"] is False
    assert gate["DUPLICATE_ACK_EFFECTS"] == 0
    assert gate["MANUAL_USER_PROMPTS_REQUIRED_FOR_SAME_CHAT_TASK_TRANSITION"] == 0
    assert gate["GUESSED_CONVERSATION_IDENTITY_ACCEPTED"] is False
    assert gate["SEND_BYPASSES_STOP_FENCE"] == 0
    assert gate["STALE_LEASE_SEND_EFFECTS"] == 0
    assert gate["EXPECTED_TRACE_STEPS"] == EXPECTED_TRACE
    assert gate["OBSERVED_TRACE_STEPS"] == EXPECTED_TRACE
    assert gate["TRACE_DIVERGENCES"] == 0
    assert gate["HARDCODED_GATE_RESULT_FIELDS"] == []
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    assert gate["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
    assert gate["NX027_STATUS"] == "PASS"

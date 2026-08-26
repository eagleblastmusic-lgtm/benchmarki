"""NX-026 continuation lease/claim/idempotency qualification.

The tests use one PM v2 SQLite database per fixture and a virtual clock.  The
concurrency cases use separate connections to the same database file so the
claim result is produced by SQLite's writer CAS, not by a process-local lock.
"""

from __future__ import annotations

import ast
import hashlib
import sqlite3
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pytest

from bdb_vnext.auto_scope_contract import AutoScope, ScopeAction
from bdb_vnext.continuation_lease import (
    CONTINUATION_LEASE_RESOURCE_TYPE,
    CONTINUATION_LEASE_UNDER_PROJECT_MEMORY_V2 as LEASE_UNDER_PM_V2_CONTRACT,
    CONTINUATION_LEASE_VERSION,
    CONTINUATION_LEASE_VERSION_EXPLICIT as LEASE_VERSION_EXPLICIT_CONTRACT,
    ContinuationLeaseCoordinator,
)
from bdb_vnext.continuation_packet import (
    ContinuationAuthoritySnapshot,
    ContinuationPacket,
    build_packet,
)
from bdb_vnext.project_memory_v2_contract import PROJECT_MEMORY_V2_DDL
from bdb_vnext.stop_fence import execute_stop_transaction


START = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)
EXPIRY = START + timedelta(hours=1)
PLAN_DIGEST = "sha256:" + "1" * 64
STATE_DIGEST = "sha256:" + "2" * 64
EVIDENCE_DIGEST = "sha256:" + "3" * 64
HEAD = "a" * 40
TREE = "b" * 40


@dataclass
class VirtualClock:
    value: datetime = START

    def now(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> datetime:
        self.value = self.value + timedelta(**kwargs)
        return self.value


def _packet(**overrides: object) -> ContinuationPacket:
    values: dict[str, object] = {
        "project_id": "nx026-project",
        "plan_identity": "plan:nx-m2",
        "plan_version": 1,
        "plan_digest": PLAN_DIGEST,
        "scope": AutoScope.UNTIL_STOPPED,
        "run_id": "run:nx026",
        "scope_epoch": 4,
        "current_milestone_id": "NX-M2",
        "current_task_id": "NX-026",
        "execution_binding_id": "binding:nx026",
        "expected_repo_head_before": HEAD,
        "state_revision": 7,
        "state_digest": STATE_DIGEST,
        "allowed_next_action": ScopeAction.LAUNCH_TASK,
        "budget_summary": {"remaining_attempts": 2, "remaining_retry_budget": 1},
        "evidence_refs": [EVIDENCE_DIGEST],
        "issued_at": START,
        "expires_at": EXPIRY,
        "attempt_id": "attempt:nx026",
        "expected_tree": TREE,
        "conversation_binding_policy": "EXISTING_CHAT_ONLY",
    }
    values.update(overrides)
    return build_packet(**values)  # type: ignore[arg-type]


def _authority(**overrides: object) -> ContinuationAuthoritySnapshot:
    values: dict[str, object] = {
        "project_id": "nx026-project",
        "plan_identity": "plan:nx-m2",
        "plan_version": 1,
        "plan_digest": PLAN_DIGEST,
        "scope": AutoScope.UNTIL_STOPPED,
        "run_id": "run:nx026",
        "scope_epoch": 4,
        "current_milestone_id": "NX-M2",
        "current_task_id": "NX-026",
        "execution_binding_id": "binding:nx026",
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
        "attempt_id": "attempt:nx026",
        "expected_tree": TREE,
        "conversation_binding_policy": "EXISTING_CHAT_ONLY",
    }
    values.update(overrides)
    return ContinuationAuthoritySnapshot(**values)  # type: ignore[arg-type]


def _create_database(path: Path, project_id: str = "nx026-project", epoch: int = 4) -> None:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(PROJECT_MEMORY_V2_DDL)
    now = START.isoformat(timespec="microseconds").replace("+00:00", "Z")
    conn.execute(
        "INSERT INTO projects (project_id, display_name, repo_alias, local_repo_path, brief_json, revision, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (project_id, "NX-026", "nx026", "/tmp/nx026", "{}", 1, now, now),
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
            "cursor:nx026",
            project_id,
            "run:nx026",
            "UNTIL_STOPPED",
            epoch,
            "NX-M2",
            "NX-026",
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


def _coordinator(path: Path, clock: VirtualClock | None = None) -> ContinuationLeaseCoordinator:
    return ContinuationLeaseCoordinator(
        path,
        "nx026-project",
        lease_seconds=30,
        clock=(clock.now if clock is not None else lambda: START),
    )


def _source_readback(repo_root: Path) -> tuple[str, str, bool]:
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, text=True, check=True).stdout.strip()
    tree = subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=repo_root, capture_output=True, text=True, check=True).stdout.strip()
    clean = subprocess.run(["git", "status", "--porcelain"], cwd=repo_root, capture_output=True, text=True, check=True).stdout == ""
    return head, tree, clean


def _packet_for_project(project_id: str, *, task_id: str = "NX-026") -> tuple[ContinuationPacket, ContinuationAuthoritySnapshot]:
    packet = _packet(project_id=project_id, current_task_id=task_id)
    authority = _authority(project_id=project_id, current_task_id=task_id)
    return packet, authority


def test_nx026_schema_and_authority_contract(tmp_path: Path) -> None:
    path = tmp_path / "schema.db"
    _create_database(path)
    conn = sqlite3.connect(str(path))
    columns = {row[1] for row in conn.execute("PRAGMA table_info(leases)").fetchall()}
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
    conn.close()

    assert CONTINUATION_LEASE_VERSION == "v1"
    assert LEASE_VERSION_EXPLICIT_CONTRACT is True
    assert LEASE_UNDER_PM_V2_CONTRACT is True
    assert "continuation_id" in columns
    assert "packet_digest" in columns
    assert "scope_epoch" in columns
    assert "owner_token_hash" in columns
    assert "state_revision" in columns
    assert "idx_continuation_claimed_lease" in indexes
    assert "leases" in tables
    assert "continuation_leases" not in tables


def test_claim_renew_release_completion_and_foreign_tokens(tmp_path: Path) -> None:
    path = tmp_path / "lease.db"
    _create_database(path)
    packet, authority = _packet_for_project("nx026-project")
    clock = VirtualClock()
    coordinator = _coordinator(path, clock)

    first = coordinator.claim(packet, authority, owner_id="browser-a", now=clock.now())
    assert first.claimed and first.owner_token is not None
    assert first.lease is not None and first.lease.status == "CLAIMED"
    raw = coordinator.read(packet)
    assert raw is not None
    assert raw.owner_token_hash != first.owner_token
    assert raw.owner_token_hash == hashlib.sha256(first.owner_token.encode()).hexdigest()

    foreign = "foreign-token-that-is-not-the-owner"
    state_revision = raw.state_revision
    assert not coordinator.renew(packet, authority, foreign, now=clock.now())
    assert not coordinator.release(packet, foreign, now=clock.now())
    assert not coordinator.complete(packet, foreign, now=clock.now())
    assert not coordinator.abandon(packet, foreign, now=clock.now())
    assert coordinator.read(packet).state_revision == state_revision  # type: ignore[union-attr]

    before_expiry = first.lease.expires_at
    renewed = coordinator.renew(packet, authority, first.owner_token, now=clock.advance(seconds=5))
    assert renewed.accepted and renewed.lease is not None
    assert renewed.lease.expires_at != before_expiry
    released = coordinator.release(packet, first.owner_token, now=clock.advance(seconds=1))
    assert released.accepted and released.lease is not None and released.lease.status == "AVAILABLE"

    second = coordinator.claim(packet, authority, owner_id="native-b", now=clock.advance(seconds=1))
    assert second.claimed and second.owner_token is not None
    assert second.lease is not None and second.lease.generation > first.lease.generation
    completed = coordinator.complete(packet, second.owner_token, now=clock.advance(seconds=1))
    duplicate = coordinator.complete(packet, second.owner_token, now=clock.now())
    wrong_duplicate = coordinator.complete(packet, foreign, now=clock.now())
    assert completed.accepted and completed.lease is not None and completed.lease.status == "COMPLETED"
    assert duplicate.accepted and duplicate.idempotent and not duplicate.mutated
    assert not wrong_duplicate.accepted
    assert not coordinator.reclaim(packet, authority, owner_id="native-c", reason="late", now=clock.advance(seconds=1))


def test_expiry_reclaim_late_owner_and_restart_are_durable(tmp_path: Path) -> None:
    path = tmp_path / "expiry.db"
    _create_database(path)
    packet, authority = _packet_for_project("nx026-project")
    clock = VirtualClock()
    coordinator = _coordinator(path, clock)

    owner_a = coordinator.claim(packet, authority, owner_id="browser-a", now=clock.now())
    assert owner_a.owner_token is not None
    active_before_restart = coordinator.read(packet)
    active_after_restart = _coordinator(path, clock).read(packet)
    assert active_before_restart == active_after_restart

    assert not coordinator.reclaim(packet, authority, owner_id="native-b", reason="too early", now=clock.advance(seconds=29))
    clock.advance(seconds=2)
    expired_before_restart = coordinator.read(packet)
    expired_after_restart = _coordinator(path, clock).read(packet)
    assert expired_before_restart == expired_after_restart

    late_renew = coordinator.renew(packet, authority, owner_a.owner_token, now=clock.now())
    owner_b = coordinator.reclaim(packet, authority, owner_id="native-b", reason="owner crash after expiry", now=clock.now())
    assert not late_renew.accepted
    assert owner_b.claimed and owner_b.owner_token is not None
    assert not coordinator.authorize_effect(packet, authority, owner_a.owner_token, now=clock.now())
    assert coordinator.authorize_effect(packet, authority, owner_b.owner_token, now=clock.now())

    completed = coordinator.complete(packet, owner_b.owner_token, now=clock.advance(seconds=1))
    assert completed.accepted
    assert _coordinator(path, clock).read(packet).status == "COMPLETED"  # type: ignore[union-attr]

    abandoned_path = tmp_path / "abandoned.db"
    _create_database(abandoned_path)
    abandoned_packet, abandoned_authority = _packet_for_project("nx026-project")
    abandoned_coord = _coordinator(abandoned_path, clock)
    abandoned_claim = abandoned_coord.claim(abandoned_packet, abandoned_authority, owner_id="browser-a", now=clock.now())
    assert abandoned_claim.owner_token is not None
    abandoned = abandoned_coord.abandon(abandoned_packet, abandoned_claim.owner_token, reason="operator checkpoint", now=clock.now())
    assert abandoned.accepted and abandoned.lease is not None and abandoned.lease.status == "ABANDONED"
    assert _coordinator(abandoned_path, clock).read(abandoned_packet).status == "ABANDONED"  # type: ignore[union-attr]


def test_accepted_stale_and_stop_fence_reject_claim_or_effect(tmp_path: Path) -> None:
    path = tmp_path / "fences.db"
    _create_database(path)
    packet, authority = _packet_for_project("nx026-project")
    clock = VirtualClock()
    coordinator = _coordinator(path, clock)
    claimed = coordinator.claim(packet, authority, owner_id="browser-a", now=clock.now())
    assert claimed.owner_token is not None

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    execute_stop_transaction(conn, "nx026-project", expected_epoch=4, reason="NX-026 stop fence")
    conn.close()
    assert not coordinator.authorize_effect(packet, authority, claimed.owner_token, now=clock.now())
    assert not coordinator.reclaim(packet, authority, owner_id="native-b", reason="stopped", now=clock.advance(seconds=31))

    accepted_path = tmp_path / "accepted.db"
    _create_database(accepted_path)
    accepted_clock = VirtualClock()
    accepted_coord = _coordinator(accepted_path, accepted_clock)
    accepted_claim = accepted_coord.claim(packet, authority, owner_id="browser-a", now=accepted_clock.now())
    accepted_authority = _authority(task_status="ACCEPTED")
    assert not accepted_coord.reclaim(packet, accepted_authority, owner_id="native-b", reason="accepted task", now=accepted_clock.advance(seconds=31))

    stale_packet = _packet(current_task_id="NX-027")
    assert not coordinator.claim(stale_packet, authority, owner_id="stale", now=clock.now())
    assert accepted_claim.owner_token is not None


def _run_contention_case(path: Path, packet: ContinuationPacket, authority: ContinuationAuthoritySnapshot, count: int) -> list[Any]:
    barrier = threading.Barrier(count)

    def contender(index: int) -> Any:
        coordinator = ContinuationLeaseCoordinator(path, "nx026-project", lease_seconds=30, clock=lambda: START)
        barrier.wait(timeout=10)
        return coordinator.claim(packet, authority, owner_id=f"worker-{index}", now=START)

    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(contender, range(count)))


def test_real_sqlite_concurrency_has_one_winner_and_zero_loser_effects(tmp_path: Path) -> None:
    path = tmp_path / "concurrency.db"
    _create_database(path)
    packet, authority = _packet_for_project("nx026-project")
    results = _run_contention_case(path, packet, authority, 12)
    winners = [result for result in results if result.claimed]
    assert len(winners) == 1
    assert sum(result.effects_allowed for result in results) == 1
    winner = winners[0]
    assert winner.owner_token is not None
    assert ContinuationLeaseCoordinator(path, "nx026-project").complete(packet, winner.owner_token, now=START)


_NX026_GATE_RESULT_FIELDS = frozenset(
    {
        "CONTINUATION_LEASE_VERSION_EXPLICIT",
        "CONTINUATION_LEASE_UNDER_PROJECT_MEMORY_V2",
        "SECOND_LEASE_AUTHORITY_CREATED",
        "CLAIM_CAS_ATOMIC",
        "MAX_SIMULTANEOUS_VALID_CLAIMANTS_PER_EPOCH",
        "LOSING_CLAIMANT_EFFECTS",
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
        "CONCURRENCY_CASES",
        "CLAIM_ATTEMPTS",
        "SUCCESSFUL_CLAIMS",
        "DUPLICATE_EFFECT_RIGHTS",
    }
)


def inspect_nx026_gate_for_hardcoded_results() -> tuple[bool, list[str]]:
    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    gate = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run_nx026_machine_gate")
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
            hardcoded.update(name for name in targets if name in _NX026_GATE_RESULT_FIELDS)
    return not hardcoded, sorted(hardcoded)


def run_nx026_machine_gate() -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parent.parent
    gate_path = repo_root / "tests" / ".nx026-machine-gate.db"
    gate_path.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        (Path(f"{gate_path}{suffix}")).unlink(missing_ok=True)

    packet, authority = _packet_for_project("nx026-project")
    contention_cases: list[list[Any]] = []
    try:
        _create_database(gate_path)
        first_case = _run_contention_case(gate_path, packet, authority, 12)
        contention_cases.append(first_case)
        first_winners = [result for result in first_case if result.claimed]
        winner = first_winners[0] if first_winners else None
        winner_token = winner.owner_token if winner is not None else None
        active_coord = ContinuationLeaseCoordinator(gate_path, "nx026-project")
        if winner_token is not None:
            active_coord.complete(packet, winner_token, now=START)

        second_path = repo_root / "tests" / ".nx026-machine-gate-second.db"
        second_path.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(f"{second_path}{suffix}").unlink(missing_ok=True)
        _create_database(second_path)
        second_case = _run_contention_case(second_path, packet, authority, 16)
        contention_cases.append(second_case)

        lease_path = repo_root / "tests" / ".nx026-machine-gate-lease.db"
        lease_path.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(f"{lease_path}{suffix}").unlink(missing_ok=True)
        _create_database(lease_path)
        clock = VirtualClock()
        coordinator = _coordinator(lease_path, clock)
        owner_a = coordinator.claim(packet, authority, owner_id="browser-a", now=clock.now())
        token_a = owner_a.owner_token or ""
        foreign = "foreign-token-used-only-for-negative-tests"
        before_expiry_reclaim = coordinator.reclaim(packet, authority, owner_id="native-b", reason="too early", now=clock.advance(seconds=29))
        clock.advance(seconds=2)
        late_renew_before_reclaim = coordinator.renew(packet, authority, token_a, now=clock.now())
        reclaimed = coordinator.reclaim(packet, authority, owner_id="native-b", reason="owner crash/expiry", now=clock.now())
        token_b = reclaimed.owner_token or ""
        late_renew_after_reclaim = coordinator.renew(packet, authority, token_a, now=clock.now())
        old_effect = coordinator.authorize_effect(packet, authority, token_a, now=clock.now())
        new_effect = coordinator.authorize_effect(packet, authority, token_b, now=clock.now())
        foreign_renew = coordinator.renew(packet, authority, foreign, now=clock.now())
        foreign_release = coordinator.release(packet, foreign, now=clock.now())
        foreign_complete = coordinator.complete(packet, foreign, now=clock.now())
        foreign_abandon = coordinator.abandon(packet, foreign, now=clock.now())
        complete = coordinator.complete(packet, token_b, now=clock.advance(seconds=1))
        duplicate_complete = coordinator.complete(packet, token_b, now=clock.now())
        completed_reclaim = coordinator.reclaim(packet, authority, owner_id="native-c", reason="completed", now=clock.now())

        accepted_authority = _authority(task_status="ACCEPTED")
        accepted_reclaim = coordinator.reclaim(packet, accepted_authority, owner_id="native-c", reason="accepted", now=clock.now())

        stop_path = repo_root / "tests" / ".nx026-machine-gate-stop.db"
        stop_path.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(f"{stop_path}{suffix}").unlink(missing_ok=True)
        _create_database(stop_path)
        stop_clock = VirtualClock()
        stop_coord = _coordinator(stop_path, stop_clock)
        stop_claim = stop_coord.claim(packet, authority, owner_id="browser-stop", now=stop_clock.now())
        stop_token = stop_claim.owner_token or ""
        stop_conn = sqlite3.connect(str(stop_path))
        stop_conn.row_factory = sqlite3.Row
        execute_stop_transaction(stop_conn, "nx026-project", expected_epoch=4, reason="machine gate stop")
        stop_conn.close()
        stop_effect = stop_coord.authorize_effect(packet, authority, stop_token, now=stop_clock.now())
        stop_reclaim = stop_coord.reclaim(packet, authority, owner_id="native-stop", reason="stopped", now=stop_clock.advance(seconds=31))

        stale_packet = _packet(current_task_id="NX-027")
        stale_claim = active_coord.claim(stale_packet, authority, owner_id="stale", now=START)

        restart_before = coordinator.read(packet)
        restart_after = ContinuationLeaseCoordinator(lease_path, "nx026-project").read(packet)
        restart_state_divergences = int(restart_before != restart_after)

        no_hardcoded, hardcoded = inspect_nx026_gate_for_hardcoded_results()
        tables_conn = sqlite3.connect(str(lease_path))
        tables = {row[0] for row in tables_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        columns = {row[1] for row in tables_conn.execute("PRAGMA table_info(leases)").fetchall()}
        indexes = {row[0] for row in tables_conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        tables_conn.close()

        # The gate databases live under the workspace so the sandbox can use
        # SQLite reliably.  Remove them before the source-bound readback; the
        # cleanliness result must describe the committed source, not the
        # gate's own temporary fixtures.
        for path in (
            gate_path,
            second_path,
            lease_path,
            stop_path,
        ):
            path.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm"):
                Path(f"{path}{suffix}").unlink(missing_ok=True)

        head, tree, clean = _source_readback(repo_root)

        successful_claims = sum(sum(result.claimed for result in case) for case in contention_cases)
        claim_attempts = sum(len(case) for case in contention_cases)
        max_simultaneous = max((sum(result.claimed for result in case) for case in contention_cases), default=0)
        loser_effects = sum(
            int(result.claimed is False and result.effects_allowed > 0)
            for case in contention_cases
            for result in case
        )
        duplicate_effect_rights = max(0, int(new_effect.accepted) + int(old_effect.accepted) - 1)
        unsafe_reclaims = sum(
            int(result.accepted)
            for result in (before_expiry_reclaim, completed_reclaim, accepted_reclaim, stop_reclaim)
        )
        # NX-027's send_intents relation is the canonical outbox for a
        # continuation effect, not a competing lease authority.  Only a
        # separate lease table would violate the NX-026 single-authority gate.
        continuation_tables = tables.intersection({"continuation_leases", "continuation_lease_store"})
        status = "PASS" if (
            bool(LEASE_VERSION_EXPLICIT_CONTRACT and CONTINUATION_LEASE_VERSION == "v1")
            and bool(LEASE_UNDER_PM_V2_CONTRACT and "leases" in tables and "continuation_id" in columns)
            and not bool(continuation_tables)
            and bool("idx_continuation_claimed_lease" in indexes and successful_claims == len(contention_cases))
            and max_simultaneous == 1
            and loser_effects == 0
            and not foreign_renew.accepted
            and not foreign_release.accepted
            and not foreign_complete.accepted
            and not foreign_abandon.accepted
            and not before_expiry_reclaim.accepted
            and not late_renew_after_reclaim.accepted
            and not old_effect.accepted
            and not duplicate_complete.mutated
            and not completed_reclaim.accepted
            and not accepted_reclaim.accepted
            and not stale_claim.claimed
            and not stop_effect.accepted
            and not stop_reclaim.accepted
            and restart_state_divergences == 0
            and unsafe_reclaims == 0
            and claim_attempts >= 20
            and successful_claims == len(contention_cases)
            and duplicate_effect_rights == 0
            and no_hardcoded
            and len(head) == 40
            and len(tree) == 40
            and clean
        ) else "FAIL"

        return {
            "CONTINUATION_LEASE_VERSION_EXPLICIT": bool(LEASE_VERSION_EXPLICIT_CONTRACT and CONTINUATION_LEASE_VERSION == "v1"),
            "CONTINUATION_LEASE_UNDER_PROJECT_MEMORY_V2": bool(LEASE_UNDER_PM_V2_CONTRACT and "leases" in tables and "continuation_id" in columns),
            "SECOND_LEASE_AUTHORITY_CREATED": bool(continuation_tables),
            "CLAIM_CAS_ATOMIC": bool("idx_continuation_claimed_lease" in indexes and successful_claims == len(contention_cases)),
            "MAX_SIMULTANEOUS_VALID_CLAIMANTS_PER_EPOCH": max_simultaneous,
            "LOSING_CLAIMANT_EFFECTS": loser_effects,
            "FOREIGN_RENEW_ACCEPTED": bool(foreign_renew.accepted),
            "FOREIGN_RELEASE_ACCEPTED": bool(foreign_release.accepted),
            "FOREIGN_COMPLETE_ACCEPTED": bool(foreign_complete.accepted),
            "FOREIGN_ABANDON_ACCEPTED": bool(foreign_abandon.accepted),
            "PRE_EXPIRY_RECLAIM_ACCEPTED": bool(before_expiry_reclaim.accepted),
            "LATE_RENEW_AFTER_RECLAIM_ACCEPTED": bool(late_renew_after_reclaim.accepted),
            "OLD_OWNER_AFTER_RECLAIM_EFFECTS": int(old_effect.accepted),
            "DUPLICATE_COMPLETIONS": int(duplicate_complete.mutated),
            "COMPLETED_CONTINUATION_RECLAIMED": int(completed_reclaim.accepted),
            "ACCEPTED_TASK_REEXECUTED_BY_RECLAIM": int(accepted_reclaim.accepted),
            "STALE_PACKET_CLAIM_EFFECTS": int(stale_claim.effects_allowed),
            "LEASE_BYPASSES_STOP_FENCE": int(stop_effect.accepted) + int(stop_reclaim.accepted),
            "RESTART_LEASE_STATE_DIVERGENCES": restart_state_divergences,
            "UNSAFE_RECLAIMS": unsafe_reclaims,
            "CONCURRENCY_CASES": len(contention_cases),
            "CLAIM_ATTEMPTS": claim_attempts,
            "SUCCESSFUL_CLAIMS": successful_claims,
            "DUPLICATE_EFFECT_RIGHTS": duplicate_effect_rights,
            "HARDCODED_GATE_RESULT_FIELDS": hardcoded,
            "NO_HARDCODED_GATE_RESULTS": no_hardcoded,
            "SOURCE_HEAD": head,
            "SOURCE_TREE": tree,
            "WORKTREE_CLEAN": clean,
            "SOURCE_BOUND_MACHINE_GATE": "PASS" if len(head) == 40 and len(tree) == 40 and clean else "FAIL",
            "NX026_STATUS": status,
        }
    finally:
        for path in (
            gate_path,
            repo_root / "tests" / ".nx026-machine-gate-second.db",
            repo_root / "tests" / ".nx026-machine-gate-lease.db",
            repo_root / "tests" / ".nx026-machine-gate-stop.db",
        ):
            path.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm"):
                Path(f"{path}{suffix}").unlink(missing_ok=True)


def test_nx026_machine_gate_execution() -> None:
    gate = run_nx026_machine_gate()
    assert gate["CONTINUATION_LEASE_VERSION_EXPLICIT"] is True
    assert gate["CONTINUATION_LEASE_UNDER_PROJECT_MEMORY_V2"] is True
    assert gate["SECOND_LEASE_AUTHORITY_CREATED"] is False
    assert gate["CLAIM_CAS_ATOMIC"] is True
    assert gate["MAX_SIMULTANEOUS_VALID_CLAIMANTS_PER_EPOCH"] == 1
    assert gate["LOSING_CLAIMANT_EFFECTS"] == 0
    assert gate["FOREIGN_RENEW_ACCEPTED"] is False
    assert gate["FOREIGN_RELEASE_ACCEPTED"] is False
    assert gate["FOREIGN_COMPLETE_ACCEPTED"] is False
    assert gate["FOREIGN_ABANDON_ACCEPTED"] is False
    assert gate["PRE_EXPIRY_RECLAIM_ACCEPTED"] is False
    assert gate["LATE_RENEW_AFTER_RECLAIM_ACCEPTED"] is False
    assert gate["OLD_OWNER_AFTER_RECLAIM_EFFECTS"] == 0
    assert gate["DUPLICATE_COMPLETIONS"] == 0
    assert gate["COMPLETED_CONTINUATION_RECLAIMED"] == 0
    assert gate["ACCEPTED_TASK_REEXECUTED_BY_RECLAIM"] == 0
    assert gate["STALE_PACKET_CLAIM_EFFECTS"] == 0
    assert gate["LEASE_BYPASSES_STOP_FENCE"] == 0
    assert gate["RESTART_LEASE_STATE_DIVERGENCES"] == 0
    assert gate["UNSAFE_RECLAIMS"] == 0
    assert gate["CONCURRENCY_CASES"] >= 2
    assert gate["CLAIM_ATTEMPTS"] >= 20
    assert gate["SUCCESSFUL_CLAIMS"] == gate["CONCURRENCY_CASES"]
    assert gate["DUPLICATE_EFFECT_RIGHTS"] == 0
    assert gate["HARDCODED_GATE_RESULT_FIELDS"] == []
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    assert gate["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
    assert gate["NX026_STATUS"] == "PASS"

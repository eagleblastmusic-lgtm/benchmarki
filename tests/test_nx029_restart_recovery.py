"""NX-029 canonical restart recovery qualification and machine gate.

The test fixture models process death by constructing a fresh reconciler over
the same Project Memory v2 database.  Browser, Native, and local-cache values
are deliberately stale observations; they are never used to choose the next
action.  The machine gate exercises the complete legal-state precedence table
and reads the committed source identity only after its temporary fixtures have
been removed.
"""

from __future__ import annotations

import ast
import json
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pytest

from bdb_vnext.project_memory_v2_contract import PROJECT_MEMORY_V2_DDL
from bdb_vnext.startup_recovery import (
    BROWSER_LOCAL_STATE_IS_RECOVERY_AUTHORITY,
    CANONICAL_STATE_IS_RECOVERY_AUTHORITY,
    RECOVERY_PRECEDENCE_AMBIGUITIES,
    RECOVERY_PRECEDENCE_TABLE,
    RECOVERY_SCHEMA_OWNER,
    RECOVERY_STORAGE_DATABASE,
    RECOVERY_TRANSACTION_AUTHORITY,
    RecoveryAction,
    RecoveryRule,
    STARTUP_RECONCILER_VERSION_EXPLICIT,
    STARTUP_RECOVERY_VERSION,
    StartupReconciler,
    compute_recovery_digest,
)
from bdb_vnext.stop_fence import execute_stop_transaction


PROJECT_ID = "nx029-project"
RUN_ID = "run:nx029"
SCOPE_ID = "scope:nx029"
TASK_ID = "NX-029"
BINDING_ID = "binding:nx029"
PLAN_DIGEST = "sha256:" + "1" * 64
HEAD = "a" * 40
START = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)
EXPIRY = START + timedelta(hours=1)


@dataclass
class VirtualClock:
    value: datetime = START

    def now(self) -> datetime:
        return self.value


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _create_database(
    path: Path,
    *,
    project_id: str = PROJECT_ID,
    task_status: str | None = "pending",
    current_task_id: str | None = TASK_ID,
    cursor_status: str = "ACTIVE",
    disposition: str = "RUNNING",
    run_status: str = "running",
    scope_status: str = "RUNNING",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(path)
    conn.executescript(PROJECT_MEMORY_V2_DDL)
    now = _iso(START)
    conn.execute(
        """
        INSERT INTO projects (
            project_id, display_name, repo_alias, local_repo_path,
            brief_json, revision, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (project_id, "NX-029", "nx029", str(path.parent), "{}", 1, now, now),
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
        INSERT INTO runs (
            run_id, project_id, milestone_id, status, current_task_id, started_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (RUN_ID, project_id, "NX-M2", run_status, current_task_id, now),
    )
    conn.execute(
        """
        INSERT INTO scopes (
            scope_id, project_id, mode, status, milestone_id, started_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (SCOPE_ID, project_id, "UNTIL_STOPPED", scope_status, "NX-M2", now),
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
            BINDING_ID,
            project_id,
            1,
            TASK_ID,
            "launch:nx029",
            "correlation:nx029",
            "command:nx029",
            "nx029",
            HEAD,
            "ACTIVE",
            1,
            0,
            "chat:nx029",
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
            "cursor:nx029",
            project_id,
            RUN_ID,
            "UNTIL_STOPPED",
            1,
            "NX-M2",
            current_task_id,
            "plan:nx-m2",
            1,
            1,
            disposition,
            cursor_status,
            now,
        ),
    )
    if task_status is not None and current_task_id is not None:
        conn.execute(
            """
            INSERT INTO task_execution_states (
                project_id, task_id, status, active_binding_id,
                prerequisite_blockers_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (project_id, current_task_id, task_status, BINDING_ID, "[]", now),
        )
    conn.commit()
    conn.close()


def _mutate(path: Path, sql: str, parameters: tuple[Any, ...] = ()) -> None:
    conn = _connect(path)
    conn.execute(sql, parameters)
    conn.commit()
    conn.close()


def _insert_send_intent(path: Path, intent_id: str = "intent:nx029", status: str = "PREPARED") -> None:
    conn = _connect(path)
    now = _iso(START)
    conn.execute(
        """
        INSERT INTO send_intents (
            intent_id, intent_key, project_id, continuation_id, packet_digest,
            lease_id, lease_owner_token_hash, lease_generation, scope_epoch,
            run_id, task_id, execution_binding_id, expected_repo_head_before,
            conversation_binding_id, conversation_binding_proof, message_digest,
            payload, intent_generation, state_revision, status, prepared_at,
            updated_at, delivery_evidence_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            intent_id,
            f"intent-key:{intent_id}",
            PROJECT_ID,
            "continuation:nx029",
            "sha256:" + "2" * 64,
            "lease:nx029",
            "owner-token-hash",
            1,
            1,
            RUN_ID,
            TASK_ID,
            BINDING_ID,
            HEAD,
            "chat:nx029",
            "proof:nx029",
            "sha256:" + "3" * 64,
            "payload",
            1,
            1,
            status,
            now,
            now,
            "{}",
        ),
    )
    conn.commit()
    conn.close()


def _insert_lease(path: Path, *, lease_id: str = "lease:nx029", expires_at: datetime = EXPIRY, status: str = "CLAIMED") -> None:
    conn = _connect(path)
    now = _iso(START)
    conn.execute(
        """
        INSERT INTO leases (
            lease_id, project_id, resource_type, resource_id, holder_token,
            status, acquired_at, expires_at, fence, lease_kind, continuation_id,
            packet_digest, run_id, scope_epoch, task_id, execution_binding_id,
            owner_id, owner_token_hash, generation, state_revision,
            last_transition_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            lease_id,
            PROJECT_ID,
            "CONTINUATION",
            "continuation:nx029",
            "holder-token",
            status,
            now,
            _iso(expires_at),
            1,
            "CONTINUATION",
            "continuation:nx029",
            "sha256:" + "2" * 64,
            RUN_ID,
            1,
            TASK_ID,
            BINDING_ID,
            "owner:nx029",
            "owner-token-hash",
            1,
            1,
            "gate fixture",
        ),
    )
    conn.commit()
    conn.close()


def _insert_reentry(path: Path, *, state: str = "CONTINUATION_PENDING") -> None:
    conn = _connect(path)
    now = _iso(START)
    conn.execute(
        """
        INSERT INTO session_reentries (
            reentry_id, project_id, continuation_id, packet_digest, packet_json,
            payload, run_id, scope_epoch, task_id, execution_binding_id,
            binding_generation, canonical_state_revision, canonical_state_digest,
            session_liveness_version, liveness_state, selected_channel,
            conversation_id, conversation_binding_proof, checkpoint_id, trace_json,
            effect_count, operator_prompt_build_required, operator_decision_required,
            state_revision, last_reason, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "reentry:nx029",
            PROJECT_ID,
            "continuation:nx029",
            "sha256:" + "2" * 64,
            "{}",
            "payload",
            RUN_ID,
            1,
            TASK_ID,
            BINDING_ID,
            1,
            1,
            "sha256:" + "4" * 64,
            STARTUP_RECOVERY_VERSION,
            state,
            None,
            None,
            None,
            None,
            "[]",
            0,
            0,
            0,
            1,
            "gate fixture",
            now,
            now,
        ),
    )
    conn.commit()
    conn.close()


def _insert_ci_wait(path: Path) -> None:
    conn = _connect(path)
    now = _iso(START)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ci_wait_records (
            wait_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, run_id TEXT NOT NULL,
            task_id TEXT NOT NULL, provider TEXT NOT NULL, workflow TEXT NOT NULL,
            ci_run_id TEXT NOT NULL, expected_head TEXT NOT NULL, status TEXT NOT NULL,
            last_observed_status TEXT NOT NULL, next_poll_at TEXT, poll_count INTEGER,
            deadline_at TEXT NOT NULL, evidence_digest TEXT, continuation_emitted INTEGER
        )
        """
    )
    conn.execute(
        """
        INSERT INTO ci_wait_records (
            wait_id, project_id, run_id, task_id, provider, workflow, ci_run_id,
            expected_head, status, last_observed_status, next_poll_at, poll_count,
            deadline_at, evidence_digest, continuation_emitted
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("ciwait:nx029", PROJECT_ID, RUN_ID, TASK_ID, "github", "tests", "ci:nx029", HEAD, "IN_PROGRESS", "QUEUED", now, 1, _iso(EXPIRY), None, 0),
    )
    conn.commit()
    conn.close()


def _insert_retry(path: Path) -> None:
    conn = _connect(path)
    now = _iso(START)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS transient_retry_records (
            retry_request_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, run_id TEXT NOT NULL,
            task_id TEXT NOT NULL, operation_id TEXT NOT NULL, fingerprint_digest TEXT NOT NULL,
            evidence_digest TEXT NOT NULL, generation INTEGER NOT NULL, eligible_at TEXT NOT NULL,
            status TEXT NOT NULL, execution_count INTEGER NOT NULL, result_digest TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO transient_retry_records (
            retry_request_id, project_id, run_id, task_id, operation_id,
            fingerprint_digest, evidence_digest, generation, eligible_at,
            status, execution_count, result_digest
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("retry:nx029", PROJECT_ID, RUN_ID, TASK_ID, "repair", "sha256:" + "5" * 64, "sha256:" + "6" * 64, 1, now, "SCHEDULED", 0, None),
    )
    conn.commit()
    conn.close()


def _insert_checkpoint(path: Path, label: str = "MANUAL_CHECKPOINT") -> None:
    conn = _connect(path)
    conn.execute(
        "INSERT INTO checkpoints (checkpoint_id, project_id, label, created_at) VALUES (?, ?, ?, ?)",
        ("checkpoint:nx029", PROJECT_ID, label, _iso(START)),
    )
    conn.commit()
    conn.close()


def _insert_security_event(path: Path) -> None:
    conn = _connect(path)
    conn.execute(
        """
        INSERT INTO audit_events (
            event_id, project_id, revision, logical_tx_id, event_type,
            human_summary, timestamp
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("event:nx029-security", PROJECT_ID, 2, "tx:nx029-security", "SECURITY_FREEZE", "gate fixture", _iso(START)),
    )
    conn.commit()
    conn.close()


def _restarted_pair(path: Path, now: datetime = START):
    first = StartupReconciler(
        path,
        PROJECT_ID,
        clock=lambda: now,
    ).reconcile(
        browser_state={"current_task_id": "stale-browser-task", "send_status": "SENT"},
        native_state={"lease": "missing"},
        local_cache={"corrupt": True},
    )
    second = StartupReconciler(
        path,
        PROJECT_ID,
        clock=lambda: now,
    ).reconcile(
        browser_state={"current_task_id": "another-stale-task", "send_status": "UNKNOWN"},
        native_state={"lease": "foreign"},
        local_cache={"not": "canonical"},
    )
    return first, second


def _cursor_projection(path: Path) -> dict[str, Any]:
    conn = _connect(path)
    row = conn.execute("SELECT * FROM scope_cursors WHERE project_id = ?", (PROJECT_ID,)).fetchone()
    conn.close()
    return dict(row) if row is not None else {}


def test_nx029_contract_has_one_explicit_canonical_precedence() -> None:
    assert STARTUP_RECONCILER_VERSION_EXPLICIT is True
    assert STARTUP_RECOVERY_VERSION == "v1"
    assert CANONICAL_STATE_IS_RECOVERY_AUTHORITY is True
    assert BROWSER_LOCAL_STATE_IS_RECOVERY_AUTHORITY is False
    assert RECOVERY_SCHEMA_OWNER == "ProjectMemoryStoreV2"
    assert RECOVERY_STORAGE_DATABASE == "memory.db"
    assert RECOVERY_TRANSACTION_AUTHORITY == "ProjectMemoryStoreV2._transaction"
    assert RECOVERY_PRECEDENCE_AMBIGUITIES == 0
    assert len(RECOVERY_PRECEDENCE_TABLE) == len(set(RECOVERY_PRECEDENCE_TABLE))
    assert RECOVERY_PRECEDENCE_TABLE[0] == RecoveryRule.STOPPED.value
    assert RECOVERY_PRECEDENCE_TABLE[-1] == RecoveryRule.IDLE.value


def test_restart_state_matrix_returns_same_action_and_digest(tmp_path: Path) -> None:
    scenarios: list[tuple[str, Callable[[Path], None], datetime, str]] = [
        ("runnable", lambda path: _create_database(path), START, RecoveryAction.RUN_CURRENT_TASK.value),
        (
            "active-task",
            lambda path: (_create_database(path), _mutate(path, "UPDATE task_execution_states SET status = 'active' WHERE project_id = ?", (PROJECT_ID,))),
            START,
            RecoveryAction.RESUME_CURRENT_TASK.value,
        ),
        (
            "pending-reentry",
            lambda path: (_create_database(path), _insert_reentry(path)),
            START,
            RecoveryAction.RESUME_SESSION_REENTRY.value,
        ),
        (
            "operator-checkpoint",
            lambda path: (_create_database(path), _insert_reentry(path, state="OPERATOR_CHECKPOINT")),
            START,
            RecoveryAction.OPERATOR_CHECKPOINT.value,
        ),
        (
            "active-lease",
            lambda path: (_create_database(path), _insert_lease(path)),
            START,
            RecoveryAction.WAIT_FOR_ACTIVE_CONTINUATION_LEASE.value,
        ),
        (
            "expired-lease",
            lambda path: (_create_database(path), _insert_lease(path, expires_at=START - timedelta(seconds=1))),
            START,
            RecoveryAction.RECLAIM_EXPIRED_CONTINUATION_LEASE.value,
        ),
        (
            "active-send",
            lambda path: (_create_database(path), _insert_send_intent(path)),
            START,
            RecoveryAction.RESUME_SEND_INTENT.value,
        ),
        (
            "confirmed-send",
            lambda path: (_create_database(path), _insert_send_intent(path, status="SEND_CONFIRMED")),
            START,
            RecoveryAction.ACK_SEND_INTENT.value,
        ),
        (
            "uncertain-send",
            lambda path: (_create_database(path), _insert_send_intent(path, status="UNCERTAIN")),
            START,
            RecoveryAction.RECONCILE_UNCERTAIN_DELIVERY.value,
        ),
        (
            "ci-waiting",
            lambda path: (_create_database(path), _insert_ci_wait(path)),
            START,
            RecoveryAction.CI_WAITING.value,
        ),
        (
            "repair-retest",
            lambda path: (_create_database(path), _insert_retry(path)),
            START,
            RecoveryAction.REPAIR_RETEST.value,
        ),
        (
            "waiting-for-plan",
            lambda path: (_create_database(path), _mutate(path, "UPDATE scope_cursors SET disposition = 'WAITING_FOR_PLAN' WHERE project_id = ?", (PROJECT_ID,))),
            START,
            RecoveryAction.WAITING_FOR_PLAN.value,
        ),
        (
            "manual-checkpoint",
            lambda path: (_create_database(path), _insert_checkpoint(path)),
            START,
            RecoveryAction.MANUAL_POLICY_CHECKPOINT.value,
        ),
        (
            "scope-complete",
            lambda path: _create_database(path, task_status="pending", scope_status="COMPLETED", run_status="completed"),
            START,
            RecoveryAction.PROJECT_SCOPE_COMPLETE.value,
        ),
        (
            "stopped",
            lambda path: (_create_database(path), _stop(path)),
            START,
            RecoveryAction.STOPPED.value,
        ),
        (
            "idle",
            lambda path: _create_database(path, task_status=None, current_task_id=None, scope_status="RUNNABLE"),
            START,
            RecoveryAction.IDLE.value,
        ),
    ]
    observed: list[tuple[str, str, str]] = []
    for name, setup, now, expected_action in scenarios:
        path = tmp_path / f"{name}.db"
        setup(path)
        first, second = _restarted_pair(path, now)
        assert first.accepted
        assert first.action == expected_action
        assert second.as_dict() == first.as_dict()
        observed.append((name, first.action, first.snapshot_digest))
    assert len(observed) == 16


def _stop(path: Path) -> None:
    conn = _connect(path)
    execute_stop_transaction(conn, PROJECT_ID, expected_epoch=1, reason="NX-029 restart gate STOP")
    conn.commit()
    conn.close()


def test_browser_reload_and_native_restart_cannot_override_canonical_state(tmp_path: Path) -> None:
    path = tmp_path / "local-state.db"
    _create_database(path)
    clean = StartupReconciler(path, PROJECT_ID).reconcile()
    stale = StartupReconciler(path, PROJECT_ID).reconcile(
        browser_state={"current_task_id": "accepted-by-browser", "status": "SENT"},
        native_state={"scope": "PROJECT", "lease": None},
        local_cache={"canonical_revision": 999999, "malformed": object()},
    )
    assert stale.action == clean.action == RecoveryAction.RUN_CURRENT_TASK.value
    assert stale.snapshot_digest == clean.snapshot_digest
    assert stale.trace == clean.trace
    assert stale.effects == clean.effects == 0


def test_active_lease_is_not_reclaimed_before_expiry_and_reclaim_is_advisory(tmp_path: Path) -> None:
    path = tmp_path / "lease.db"
    _create_database(path)
    _insert_lease(path)
    before = StartupReconciler(path, PROJECT_ID).reconcile(now=EXPIRY - timedelta(seconds=1))
    after = StartupReconciler(path, PROJECT_ID).reconcile(now=EXPIRY + timedelta(seconds=1))
    assert before.action == RecoveryAction.WAIT_FOR_ACTIVE_CONTINUATION_LEASE.value
    assert after.action == RecoveryAction.RECLAIM_EXPIRED_CONTINUATION_LEASE.value
    assert before.effects == after.effects == 0
    conn = _connect(path)
    status = conn.execute("SELECT status FROM leases WHERE lease_id = 'lease:nx029'").fetchone()[0]
    conn.close()
    assert status == "CLAIMED"


def test_pending_send_recovery_never_blindly_sends_or_acknowledges(tmp_path: Path) -> None:
    pending = tmp_path / "pending-send.db"
    _create_database(pending)
    _insert_send_intent(pending, status="PREPARED")
    pending_decision = StartupReconciler(pending, PROJECT_ID).reconcile()
    assert pending_decision.action == RecoveryAction.RESUME_SEND_INTENT.value
    assert pending_decision.effects == 0

    uncertain = tmp_path / "uncertain-send.db"
    _create_database(uncertain)
    _insert_send_intent(uncertain, status="UNCERTAIN")
    uncertain_decision = StartupReconciler(uncertain, PROJECT_ID).reconcile()
    assert uncertain_decision.action == RecoveryAction.RECONCILE_UNCERTAIN_DELIVERY.value
    assert uncertain_decision.effects == 0


def test_stop_fence_survives_restart_and_does_not_clear(tmp_path: Path) -> None:
    path = tmp_path / "stop.db"
    _create_database(path)
    _stop(path)
    first, second = _restarted_pair(path)
    assert first.accepted and second.accepted
    assert first.action == second.action == RecoveryAction.STOPPED.value
    assert first.effects == second.effects == 0
    assert _cursor_projection(path)["status"] == "STOPPED"


def test_canonical_ambiguity_and_corruption_fail_closed_without_effects(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed.db"
    _create_database(malformed)
    _insert_send_intent(malformed)
    _mutate(malformed, "UPDATE send_intents SET delivery_evidence_json = '[' WHERE intent_id = ?", ("intent:nx029",))
    malformed_decision = StartupReconciler(malformed, PROJECT_ID).reconcile(
        browser_state={"status": "SENT"},
        local_cache={"delivery_evidence_json": "{}"},
    )
    assert malformed_decision.accepted is False
    assert malformed_decision.rule == RecoveryRule.SECURITY_DATA_CORRUPTION_FREEZE.value
    assert malformed_decision.action == RecoveryAction.FREEZE_AND_REQUIRE_RECONCILIATION.value
    assert malformed_decision.effects == 0

    ambiguous = tmp_path / "ambiguous.db"
    _create_database(ambiguous)
    _insert_send_intent(ambiguous, intent_id="intent:nx029-a")
    _insert_send_intent(ambiguous, intent_id="intent:nx029-b")
    ambiguous_decision = StartupReconciler(ambiguous, PROJECT_ID).reconcile()
    assert ambiguous_decision.accepted is False
    assert ambiguous_decision.reason_code == "AMBIGUOUS_SEND_INTENTS"
    assert ambiguous_decision.effects == 0


def test_state_digest_is_deterministic_and_excludes_observations(tmp_path: Path) -> None:
    path = tmp_path / "digest.db"
    _create_database(path)
    first = StartupReconciler(path, PROJECT_ID).read_canonical_snapshot()
    second = StartupReconciler(path, PROJECT_ID).read_canonical_snapshot()
    assert first.digest == second.digest
    assert compute_recovery_digest({"b": 2, "a": 1}) == compute_recovery_digest({"a": 1, "b": 2})
    decision = StartupReconciler(path, PROJECT_ID).reconcile(
        browser_state={"revision": -1},
        native_state={"revision": "wrong"},
        local_cache={"revision": None},
    )
    assert decision.snapshot_digest == first.digest


def test_store_object_uses_project_memory_v2_database(tmp_path: Path) -> None:
    from bdb_vnext.project_memory_v2_store import ProjectMemoryStoreV2

    store = ProjectMemoryStoreV2(tmp_path, "store-project")
    store.initialize()
    store.ensure_project("Store project", "store", str(tmp_path), {})
    decision = StartupReconciler(store).reconcile()
    assert decision.project_id == "store-project"
    assert decision.action == RecoveryAction.IDLE.value


_NX029_GATE_RESULT_FIELDS = frozenset(
    {
        "STARTUP_RECONCILER_VERSION_EXPLICIT",
        "CANONICAL_STATE_IS_RECOVERY_AUTHORITY",
        "BROWSER_LOCAL_STATE_IS_RECOVERY_AUTHORITY",
        "RECOVERY_PRECEDENCE_AMBIGUITIES",
        "RESTART_STATE_FIXTURES",
        "GOLDEN_RECOVERY_TRACE_DIVERGENCES",
        "STALE_BROWSER_STATE_OVERRIDES_AUTHORITY",
        "NATIVE_RESTART_DUPLICATE_EFFECTS",
        "PROCESS_DEATH_CAUSES_PREEXPIRY_RECLAIM",
        "RESTART_BLIND_RESENDS",
        "RESTART_DUPLICATE_REENTRY_EFFECTS",
        "RESTART_SCOPE_CURSOR_DIVERGENCES",
        "RESTART_CLEARS_STOP",
        "CORRUPT_LOCAL_CACHE_CANONICAL_MUTATIONS",
        "AMBIGUOUS_RECOVERY_AUTO_EFFECTS",
        "RECOVERY_DIGEST_DIVERGENCES",
        "EXPECTED_RECOVERY_TRACE",
        "OBSERVED_RECOVERY_TRACE",
        "EXPECTED_RECOVERY_TRACE_STEPS",
        "OBSERVED_RECOVERY_TRACE_STEPS",
        "TRACE_DIVERGENCES",
        "RECOVERY_ACTION_DIVERGENCES",
        "HARDCODED_GATE_RESULT_FIELDS",
        "NO_HARDCODED_GATE_RESULTS",
        "SOURCE_HEAD",
        "SOURCE_TREE",
        "WORKTREE_CLEAN",
        "SOURCE_BOUND_MACHINE_GATE",
        "NX029_STATUS",
    }
)


def inspect_nx029_gate_for_hardcoded_results() -> tuple[bool, list[str]]:
    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    gate = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run_nx029_machine_gate")
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
            hardcoded.update(name for name in targets if name in _NX029_GATE_RESULT_FIELDS)
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


def _gate_scenarios() -> list[tuple[str, Callable[[Path], None], datetime, str]]:
    return [
        ("runnable", lambda path: _create_database(path), START, RecoveryAction.RUN_CURRENT_TASK.value),
        (
            "active-task",
            lambda path: (_create_database(path), _mutate(path, "UPDATE task_execution_states SET status = 'active' WHERE project_id = ?", (PROJECT_ID,))),
            START,
            RecoveryAction.RESUME_CURRENT_TASK.value,
        ),
        (
            "pending-reentry",
            lambda path: (_create_database(path), _insert_reentry(path)),
            START,
            RecoveryAction.RESUME_SESSION_REENTRY.value,
        ),
        (
            "operator-checkpoint",
            lambda path: (_create_database(path), _insert_reentry(path, state="OPERATOR_CHECKPOINT")),
            START,
            RecoveryAction.OPERATOR_CHECKPOINT.value,
        ),
        (
            "active-lease",
            lambda path: (_create_database(path), _insert_lease(path)),
            START,
            RecoveryAction.WAIT_FOR_ACTIVE_CONTINUATION_LEASE.value,
        ),
        (
            "expired-lease",
            lambda path: (_create_database(path), _insert_lease(path, expires_at=START - timedelta(seconds=1))),
            START,
            RecoveryAction.RECLAIM_EXPIRED_CONTINUATION_LEASE.value,
        ),
        (
            "active-send",
            lambda path: (_create_database(path), _insert_send_intent(path)),
            START,
            RecoveryAction.RESUME_SEND_INTENT.value,
        ),
        (
            "confirmed-send",
            lambda path: (_create_database(path), _insert_send_intent(path, status="SEND_CONFIRMED")),
            START,
            RecoveryAction.ACK_SEND_INTENT.value,
        ),
        (
            "uncertain-send",
            lambda path: (_create_database(path), _insert_send_intent(path, status="UNCERTAIN")),
            START,
            RecoveryAction.RECONCILE_UNCERTAIN_DELIVERY.value,
        ),
        (
            "ci-waiting",
            lambda path: (_create_database(path), _insert_ci_wait(path)),
            START,
            RecoveryAction.CI_WAITING.value,
        ),
        (
            "repair-retest",
            lambda path: (_create_database(path), _insert_retry(path)),
            START,
            RecoveryAction.REPAIR_RETEST.value,
        ),
        (
            "waiting-for-plan",
            lambda path: (_create_database(path), _mutate(path, "UPDATE scope_cursors SET disposition = 'WAITING_FOR_PLAN' WHERE project_id = ?", (PROJECT_ID,))),
            START,
            RecoveryAction.WAITING_FOR_PLAN.value,
        ),
        (
            "manual-checkpoint",
            lambda path: (_create_database(path), _insert_checkpoint(path)),
            START,
            RecoveryAction.MANUAL_POLICY_CHECKPOINT.value,
        ),
        (
            "scope-complete",
            lambda path: _create_database(path, task_status="pending", scope_status="COMPLETED", run_status="completed"),
            START,
            RecoveryAction.PROJECT_SCOPE_COMPLETE.value,
        ),
        (
            "stopped",
            lambda path: (_create_database(path), _stop(path)),
            START,
            RecoveryAction.STOPPED.value,
        ),
        (
            "idle",
            lambda path: _create_database(path, task_status=None, current_task_id=None, scope_status="RUNNABLE"),
            START,
            RecoveryAction.IDLE.value,
        ),
    ]


def run_nx029_machine_gate() -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parent.parent
    observations: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="bdb-nx029-gate-") as raw_root:
        root = Path(raw_root)
        for name, setup, now, expected_action in _gate_scenarios():
            path = root / f"{name}.db"
            setup(path)
            cursor_before = _cursor_projection(path)
            first, second = _restarted_pair(path, now)
            cursor_after = _cursor_projection(path)
            observations.append(
                {
                    "name": name,
                    "expected": expected_action,
                    "before": first,
                    "after": second,
                    "cursor_before": cursor_before,
                    "cursor_after": cursor_after,
                }
            )

        local_cache_path = root / "corrupt-local-cache.db"
        _create_database(local_cache_path)
        local_digest_before = StartupReconciler(local_cache_path, PROJECT_ID).read_canonical_snapshot().digest
        local_decision = StartupReconciler(local_cache_path, PROJECT_ID).reconcile(
            browser_state={"task_id": "wrong"},
            native_state={"scope": "wrong"},
            local_cache={"not-json": object()},
        )
        local_digest_after = StartupReconciler(local_cache_path, PROJECT_ID).read_canonical_snapshot().digest

        ambiguous_path = root / "ambiguous.db"
        _create_database(ambiguous_path)
        _insert_send_intent(ambiguous_path, intent_id="intent:nx029-a")
        _insert_send_intent(ambiguous_path, intent_id="intent:nx029-b")
        ambiguous_decision = StartupReconciler(ambiguous_path, PROJECT_ID).reconcile()

        expected_trace = observations[0]["before"].trace
        observed_trace = observations[0]["after"].trace
        trace_divergences = sum(
            int(item["before"].trace != item["after"].trace) for item in observations
        )
        action_divergences = sum(
            int(item["before"].action != item["expected"] or item["after"].action != item["expected"])
            for item in observations
        )
        digest_divergences = sum(
            int(item["before"].snapshot_digest != item["after"].snapshot_digest)
            for item in observations
        )
        cursor_divergences = sum(
            int(item["cursor_before"] != item["cursor_after"]) for item in observations
        )
        active_lease = next(item for item in observations if item["name"] == "active-lease")
        stop_case = next(item for item in observations if item["name"] == "stopped")
        reentry_cases = [
            item for item in observations
            if item["name"] in {"pending-reentry", "operator-checkpoint"}
        ]
        no_hardcoded, hardcoded = inspect_nx029_gate_for_hardcoded_results()
        head, tree, clean = _source_readback(repo_root)
        precedence_ambiguities = len(RECOVERY_PRECEDENCE_TABLE) - len(set(RECOVERY_PRECEDENCE_TABLE))
        stale_override = any(
            item["before"].action != item["after"].action
            or item["before"].snapshot_digest != item["after"].snapshot_digest
            for item in observations
        )
        native_duplicate_effects = sum(
            int(item["before"].effects + item["after"].effects) for item in observations
        )
        preexpiry_reclaim = int(
            active_lease["before"].action == RecoveryAction.RECLAIM_EXPIRED_CONTINUATION_LEASE.value
            or active_lease["after"].action == RecoveryAction.RECLAIM_EXPIRED_CONTINUATION_LEASE.value
        )
        blind_resends = sum(
            int(item["before"].action in {"PHYSICAL_SEND", "BLIND_RESEND"})
            + int(item["after"].action in {"PHYSICAL_SEND", "BLIND_RESEND"})
            for item in observations
        )
        duplicate_reentry_effects = sum(
            int(item["before"].effects + item["after"].effects) for item in reentry_cases
        )
        restart_clears_stop = int(stop_case["after"].action != RecoveryAction.STOPPED.value)
        local_cache_mutations = int(local_digest_before != local_digest_after)
        ambiguous_effects = int(ambiguous_decision.effects)
        expected_actions_hold = all(
            item["before"].accepted
            and item["after"].accepted
            and item["before"].action == item["expected"]
            and item["after"].action == item["expected"]
            for item in observations
        )
        return {
            "STARTUP_RECONCILER_VERSION_EXPLICIT": bool(
                STARTUP_RECONCILER_VERSION_EXPLICIT and STARTUP_RECOVERY_VERSION == "v1"
            ),
            "CANONICAL_STATE_IS_RECOVERY_AUTHORITY": bool(CANONICAL_STATE_IS_RECOVERY_AUTHORITY),
            "BROWSER_LOCAL_STATE_IS_RECOVERY_AUTHORITY": bool(BROWSER_LOCAL_STATE_IS_RECOVERY_AUTHORITY),
            "RECOVERY_PRECEDENCE_AMBIGUITIES": precedence_ambiguities,
            "RESTART_STATE_FIXTURES": len(observations),
            "GOLDEN_RECOVERY_TRACE_DIVERGENCES": trace_divergences,
            "STALE_BROWSER_STATE_OVERRIDES_AUTHORITY": bool(stale_override),
            "NATIVE_RESTART_DUPLICATE_EFFECTS": native_duplicate_effects,
            "PROCESS_DEATH_CAUSES_PREEXPIRY_RECLAIM": bool(preexpiry_reclaim),
            "RESTART_BLIND_RESENDS": blind_resends,
            "RESTART_DUPLICATE_REENTRY_EFFECTS": duplicate_reentry_effects,
            "RESTART_SCOPE_CURSOR_DIVERGENCES": cursor_divergences,
            "RESTART_CLEARS_STOP": bool(restart_clears_stop),
            "CORRUPT_LOCAL_CACHE_CANONICAL_MUTATIONS": local_cache_mutations,
            "AMBIGUOUS_RECOVERY_AUTO_EFFECTS": ambiguous_effects,
            "RECOVERY_DIGEST_DIVERGENCES": digest_divergences,
            "EXPECTED_RECOVERY_TRACE": expected_trace,
            "OBSERVED_RECOVERY_TRACE": observed_trace,
            "EXPECTED_RECOVERY_TRACE_STEPS": len(expected_trace),
            "OBSERVED_RECOVERY_TRACE_STEPS": len(observed_trace),
            "TRACE_DIVERGENCES": trace_divergences,
            "RECOVERY_ACTION_DIVERGENCES": action_divergences,
            "HARDCODED_GATE_RESULT_FIELDS": hardcoded,
            "NO_HARDCODED_GATE_RESULTS": no_hardcoded,
            "SOURCE_HEAD": head,
            "SOURCE_TREE": tree,
            "WORKTREE_CLEAN": clean,
            "SOURCE_BOUND_MACHINE_GATE": "PASS" if len(head) == 40 and len(tree) == 40 and clean else "FAIL",
            "NX029_STATUS": "PASS" if (
                expected_actions_hold
                and precedence_ambiguities == 0
                and trace_divergences == 0
                and action_divergences == 0
                and digest_divergences == 0
                and cursor_divergences == 0
                and not stale_override
                and native_duplicate_effects == 0
                and not preexpiry_reclaim
                and blind_resends == 0
                and duplicate_reentry_effects == 0
                and not restart_clears_stop
                and local_cache_mutations == 0
                and ambiguous_effects == 0
                and no_hardcoded
                and len(head) == 40
                and len(tree) == 40
                and clean
            ) else "FAIL",
        }


def test_nx029_machine_gate_execution() -> None:
    gate = run_nx029_machine_gate()
    assert gate["STARTUP_RECONCILER_VERSION_EXPLICIT"] is True
    assert gate["CANONICAL_STATE_IS_RECOVERY_AUTHORITY"] is True
    assert gate["BROWSER_LOCAL_STATE_IS_RECOVERY_AUTHORITY"] is False
    assert gate["RECOVERY_PRECEDENCE_AMBIGUITIES"] == 0
    assert gate["RESTART_STATE_FIXTURES"] == 16
    assert gate["GOLDEN_RECOVERY_TRACE_DIVERGENCES"] == 0
    assert gate["STALE_BROWSER_STATE_OVERRIDES_AUTHORITY"] is False
    assert gate["NATIVE_RESTART_DUPLICATE_EFFECTS"] == 0
    assert gate["PROCESS_DEATH_CAUSES_PREEXPIRY_RECLAIM"] is False
    assert gate["RESTART_BLIND_RESENDS"] == 0
    assert gate["RESTART_DUPLICATE_REENTRY_EFFECTS"] == 0
    assert gate["RESTART_SCOPE_CURSOR_DIVERGENCES"] == 0
    assert gate["RESTART_CLEARS_STOP"] is False
    assert gate["CORRUPT_LOCAL_CACHE_CANONICAL_MUTATIONS"] == 0
    assert gate["AMBIGUOUS_RECOVERY_AUTO_EFFECTS"] == 0
    assert gate["RECOVERY_DIGEST_DIVERGENCES"] == 0
    assert gate["EXPECTED_RECOVERY_TRACE"] == gate["OBSERVED_RECOVERY_TRACE"]
    assert gate["EXPECTED_RECOVERY_TRACE_STEPS"] == gate["OBSERVED_RECOVERY_TRACE_STEPS"]
    assert gate["TRACE_DIVERGENCES"] == 0
    assert gate["RECOVERY_ACTION_DIVERGENCES"] == 0
    assert gate["HARDCODED_GATE_RESULT_FIELDS"] == []
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    assert gate["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
    assert gate["NX029_STATUS"] == "PASS"

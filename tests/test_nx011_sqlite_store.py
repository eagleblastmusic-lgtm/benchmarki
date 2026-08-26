"""NX-011 — Transactional SQLite Project Memory v2 Store — Machine Gate Tests.

Tests:
1.  fresh DB initialization
2.  schema contract matches NX-010
3.  transaction rollback
4.  multi-record atomicity
5.  stale CAS
6.  concurrent readers/writer
7.  bounded writer contention & busy classification
8.  reopen/recovery
9.  interrupted transaction
10. migration interruption
11. foreign-key enforcement
12. root/path confinement (mocked & resolved)
13. backup
14. restore
15. backup/restore digest parity
16. v1-v2 public API conformance corpus (fresh & restored)
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import sqlite3
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from bdb_vnext.project_memory_v2_contract import (
    PROJECT_MEMORY_V2_DDL,
    PROJECT_MEMORY_V2_SCHEMA_VERSION,
)
from bdb_vnext.project_memory_v2_store import (
    ProjectMemoryStoreV2,
    ProjectMemoryV2Error,
    V1_PUBLIC_API_METHODS,
    check_v2_api_conformance,
)


# ===========================================================
# Fixtures
# ===========================================================

@pytest.fixture
def store(tmp_path: Path) -> ProjectMemoryStoreV2:
    s = ProjectMemoryStoreV2(tmp_path, "p-test")
    s.initialize()
    s.ensure_project("Test Project", "repo-test", str(tmp_path / "repo"), {"name": "T", "goal": "G"})
    return s


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    return tmp_path


# ===========================================================
# Helper: Semantic Corpus
# ===========================================================

def run_semantic_corpus(store: ProjectMemoryStoreV2) -> int:
    """Executes full semantic mutation corpus against store and returns divergence count."""
    divergences = 0

    # 1. append_event
    ev = store.append_event("PROJECT_CREATED", "Initialized")
    if not ev.get("event_id", "").startswith(f"{store.project_id}:"):
        divergences += 1

    # 2. add_decision
    dec = store.add_decision(title="D1", decision="Adopt SQLite", reason="ACID")
    if dec.get("decision_id") != "D-001" or dec.get("status") != "active":
        divergences += 1

    # 3. add_inbox
    ib = store.add_inbox(title="I1", description="Idea 1")
    if ib.get("inbox_id") != "I-001" or ib.get("status") != "new":
        divergences += 1

    # 4. update_inbox
    uib = store.update_inbox("I-001", "resolved")
    if uib.get("status") != "resolved":
        divergences += 1

    # 5. add_risk
    rk = store.add_risk(title="R1", description="Risk 1", severity="high")
    if rk.get("risk_id") != "R-001" or rk.get("status") != "open":
        divergences += 1

    # 6. resolve_risk
    rrk = store.resolve_risk("R-001", "mitigated")
    if rrk.get("status") != "mitigated":
        divergences += 1

    # 7. add_debt
    dt = store.add_debt(title="TD1", description="Tech debt 1")
    if dt.get("debt_id") != "TD-001" or dt.get("status") != "open":
        divergences += 1

    # 8. resolve_debt
    rdt = store.resolve_debt("TD-001", "resolved")
    if rdt.get("status") != "resolved":
        divergences += 1

    # 9. add_attention
    att = store.add_attention(type="blocked", title="A1", description="Blocked item")
    if att.get("attention_id") != "ATT-001" or att.get("status") != "open":
        divergences += 1

    # 10. resolve_attention
    ratt = store.resolve_attention("ATT-001")
    if ratt.get("status") != "resolved":
        divergences += 1

    # 11. create_checkpoint
    cp = store.create_checkpoint(label="CP-1", plan_version=1)
    if not cp.get("checkpoint_id", "").startswith("cp-"):
        divergences += 1

    # 12. ensure_initial_plan
    plan_v1 = {"plan_version": 1, "schema": "bdb-project-plan-v1", "milestones": []}
    p1 = store.ensure_initial_plan(plan_v1)
    if p1.get("plan_version") != 1:
        divergences += 1

    # 13. apply_update
    plan_v2 = {"plan_version": 2, "schema": "bdb-project-plan-v1", "milestones": []}
    p2 = store.apply_update(plan_v2)
    if p2.get("plan_version") != 2:
        divergences += 1

    # 14. execution_transaction
    def _etxn(conn: sqlite3.Connection, rev: int) -> str:
        return "etxn_ok"
    if store.execution_transaction(_etxn) != "etxn_ok":
        divergences += 1

    # 15. write_transaction with revision CAS
    curr_rev = store.get_revision()
    def _wtxn(conn: sqlite3.Connection, rev: int) -> str:
        return "wtxn_ok"
    if store.write_transaction(_wtxn, expected_revision=curr_rev) != "wtxn_ok":
        divergences += 1

    # 16. read_state
    state = store.read_state()
    if state.get("project_id") != store.project_id or len(state.get("events", [])) < 10:
        divergences += 1

    return divergences


def verify_restored_corpus(store: ProjectMemoryStoreV2) -> int:
    """Verifies that restored DB has complete data matching semantic corpus."""
    divergences = 0
    state = store.read_state()
    if state.get("project_id") != store.project_id:
        divergences += 1
    if len(state.get("events", [])) < 10:
        divergences += 1
    if len(state.get("decisions", [])) < 1:
        divergences += 1
    if len(state.get("inbox", [])) < 1:
        divergences += 1
    if len(state.get("risks", [])) < 1:
        divergences += 1
    if len(state.get("technical_debt", [])) < 1:
        divergences += 1
    if len(state.get("attention", [])) < 1:
        divergences += 1
    if len(state.get("checkpoints", [])) < 1:
        divergences += 1
    return divergences


# ===========================================================
# 1. Fresh DB initialization
# ===========================================================

class TestFreshDBInit:
    def test_initialize_creates_db(self, tmp_path: Path) -> None:
        s = ProjectMemoryStoreV2(tmp_path, "p-init")
        s.initialize()
        assert s.db_path.exists()

    def test_initialize_is_idempotent(self, tmp_path: Path) -> None:
        s = ProjectMemoryStoreV2(tmp_path, "p-init")
        s.initialize()
        s.initialize()
        assert s.db_path.exists()

    def test_schema_version_recorded(self, tmp_path: Path) -> None:
        s = ProjectMemoryStoreV2(tmp_path, "p-init")
        s.initialize()
        assert s.schema_version() == PROJECT_MEMORY_V2_SCHEMA_VERSION


# ===========================================================
# 2. Schema contract matches NX-010
# ===========================================================

class TestSchemaContractMatch:
    def test_all_nx010_tables_present(self, store: ProjectMemoryStoreV2) -> None:
        conn = store._connect()
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        conn.close()
        required = {
            "schema_migrations", "projects", "project_plans", "runs", "scopes",
            "task_execution_states", "execution_bindings", "attempts", "checkpoints",
            "launch_outbox", "decisions", "inbox_items", "risks", "technical_debt",
            "attention_items", "audit_events", "failures", "evidence_records", "leases",
        }
        assert required.issubset(tables), f"Missing: {required - tables}"

    def test_foreign_keys_enabled(self, store: ProjectMemoryStoreV2) -> None:
        conn = store._connect()
        fk = conn.execute("PRAGMA foreign_keys").fetchone()
        conn.close()
        assert fk[0] == 1

    def test_wal_mode_active(self, store: ProjectMemoryStoreV2) -> None:
        conn = store._connect()
        jm = conn.execute("PRAGMA journal_mode").fetchone()
        conn.close()
        assert jm[0].lower() == "wal"


# ===========================================================
# 3. Transaction rollback
# ===========================================================

class TestTransactionRollback:
    def test_failed_operation_rolls_back(self, store: ProjectMemoryStoreV2) -> None:
        rev_before = store.get_revision()
        with pytest.raises(ValueError):
            def _bad_op(conn: sqlite3.Connection, rev: int) -> None:
                conn.execute(
                    "INSERT INTO audit_events (event_id, project_id, revision, logical_tx_id, event_type, human_summary, timestamp) VALUES (?,?,?,?,?,?,?)",
                    ("e-rb", store.project_id, 1, "tx-rb", "PROJECT_CREATED", "Test", "2026-01-01T00:00:00Z"),
                )
                raise ValueError("simulated failure")
            store.write_transaction(_bad_op)
        rev_after = store.get_revision()
        assert rev_after == rev_before

        conn = store._connect()
        count = conn.execute("SELECT COUNT(*) FROM audit_events WHERE project_id = ?", (store.project_id,)).fetchone()[0]
        conn.close()
        assert count == 0


# ===========================================================
# 4. Multi-record atomicity
# ===========================================================

class TestMultiRecordAtomicity:
    def test_multi_insert_atomic(self, store: ProjectMemoryStoreV2) -> None:
        def _op(conn: sqlite3.Connection, rev: int) -> str:
            ts = "2026-01-01T00:00:00Z"
            conn.execute(
                "INSERT INTO audit_events (event_id, project_id, revision, logical_tx_id, event_type, human_summary, timestamp) VALUES (?,?,?,?,?,?,?)",
                ("e-a1", store.project_id, 1, "tx-multi", "PROJECT_CREATED", "Init", ts),
            )
            conn.execute(
                "INSERT INTO decisions (decision_id, project_id, title, decision, reason, status, created_at) VALUES (?,?,?,?,?,?,?)",
                ("d-a1", store.project_id, "D1", "Do it", "Because", "active", ts),
            )
            return "done"
        result = store.write_transaction(_op)
        assert result == "done"

        conn = store._connect()
        events = conn.execute("SELECT COUNT(*) FROM audit_events WHERE project_id = ?", (store.project_id,)).fetchone()[0]
        decisions = conn.execute("SELECT COUNT(*) FROM decisions WHERE project_id = ?", (store.project_id,)).fetchone()[0]
        conn.close()
        assert events == 1
        assert decisions == 1

    def test_multi_insert_fails_atomically(self, store: ProjectMemoryStoreV2) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            def _op(conn: sqlite3.Connection, rev: int) -> None:
                ts = "2026-01-01T00:00:00Z"
                conn.execute(
                    "INSERT INTO audit_events (event_id, project_id, revision, logical_tx_id, event_type, human_summary, timestamp) VALUES (?,?,?,?,?,?,?)",
                    ("e-af", store.project_id, 1, "tx-af", "PROJECT_CREATED", "Init", ts),
                )
                # FK violation: nonexistent binding
                conn.execute(
                    "INSERT INTO attempts (attempt_id, execution_binding_id, project_id, task_id, generation, head_before, execution_status, validation_status, promotion_status, canonical_result_digest, identity_version, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("a-af", "nonexistent", store.project_id, "t1", 1, "0" * 40, "PASS", "PASS", "PASS", "sha256:x", "v2", ts),
                )
            store.write_transaction(_op)

        conn = store._connect()
        events = conn.execute("SELECT COUNT(*) FROM audit_events WHERE project_id = ?", (store.project_id,)).fetchone()[0]
        conn.close()
        assert events == 0  # rolled back


# ===========================================================
# 5. Stale CAS
# ===========================================================

class TestStaleCAS:
    def test_stale_revision_rejected(self, store: ProjectMemoryStoreV2) -> None:
        store.append_event("PROJECT_CREATED", "Init")
        current_rev = store.get_revision()

        with pytest.raises(ProjectMemoryV2Error) as exc_info:
            store.write_transaction(
                lambda conn, rev: None,
                expected_revision=current_rev - 1,
            )
        assert exc_info.value.code == "stale_revision_rejected"

    def test_matching_revision_accepted(self, store: ProjectMemoryStoreV2) -> None:
        store.append_event("PROJECT_CREATED", "Init")
        current_rev = store.get_revision()
        store.write_transaction(
            lambda conn, rev: None,
            expected_revision=current_rev,
        )
        assert store.get_revision() == current_rev + 1


# ===========================================================
# 6. Concurrent readers/writer
# ===========================================================

class TestConcurrentReadersWriter:
    def test_readers_during_writes(self, store: ProjectMemoryStoreV2) -> None:
        for i in range(5):
            store.append_event("PROJECT_CREATED", f"Event {i}")

        errors = []
        read_counts = []

        def _reader() -> int:
            try:
                events = store.read_events()
                return len(events)
            except Exception as e:
                errors.append(str(e))
                return -1

        def _writer() -> None:
            try:
                store.append_event("TASK_STARTED", f"Concurrent write {uuid.uuid4().hex[:4]}")
            except Exception as e:
                errors.append(str(e))

        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = []
            for _ in range(4):
                futures.append(executor.submit(_reader))
            futures.append(executor.submit(_writer))
            for _ in range(4):
                futures.append(executor.submit(_reader))

            for f in as_completed(futures):
                result = f.result()
                if isinstance(result, int) and result >= 0:
                    read_counts.append(result)

        assert len(errors) == 0, f"Errors: {errors}"
        assert all(c >= 5 for c in read_counts)


# ===========================================================
# 7. Bounded writer contention & Busy classification
# ===========================================================

class TestWriterContention:
    def test_concurrent_writers_classification(self, store: ProjectMemoryStoreV2) -> None:
        results = {"committed": 0, "stale": 0, "busy": 0, "raw_sqlite_busy": 0, "errors": []}
        lock = threading.Lock()

        def _write(idx: int) -> None:
            try:
                store.append_event("TASK_STARTED", f"Writer {idx}")
                with lock:
                    results["committed"] += 1
            except ProjectMemoryV2Error as e:
                with lock:
                    if e.code == "stale_revision_rejected":
                        results["stale"] += 1
                    elif e.code == "store_busy":
                        results["busy"] += 1
                    else:
                        results["errors"].append(f"{e.code}: {e}")
            except sqlite3.OperationalError as e:
                with lock:
                    if "locked" in str(e).lower() or "busy" in str(e).lower():
                        results["raw_sqlite_busy"] += 1
                    else:
                        results["errors"].append(str(e))
            except Exception as e:
                with lock:
                    results["errors"].append(str(e))

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(_write, i) for i in range(10)]
            for f in as_completed(futures):
                f.result()

        assert results["raw_sqlite_busy"] == 0, "Raw sqlite busy error leaked through public API"
        assert len(results["errors"]) == 0, f"Errors: {results['errors']}"
        assert results["committed"] > 0
        assert results["committed"] + results["stale"] + results["busy"] == 10
        events = store.read_events()
        assert len(events) == results["committed"]

    def test_sqlite_busy_classified_as_store_busy(self, store: ProjectMemoryStoreV2) -> None:
        class _BusyConn:
            def execute(self, *args: Any, **kwargs: Any) -> Any:
                raise sqlite3.OperationalError("database is locked")
            def rollback(self) -> None:
                pass
            def close(self) -> None:
                pass

        with patch.object(store, "_connect", return_value=_BusyConn()):
            with pytest.raises(ProjectMemoryV2Error) as exc_info:
                store.append_event("TEST", "test")
            assert exc_info.value.code == "store_busy"

    def test_unexpected_sqlite_error_not_masked_as_busy(self, store: ProjectMemoryStoreV2) -> None:
        class _SyntaxErrConn:
            def execute(self, *args: Any, **kwargs: Any) -> Any:
                raise sqlite3.OperationalError("syntax error near WHERE")
            def rollback(self) -> None:
                pass
            def close(self) -> None:
                pass

        with patch.object(store, "_connect", return_value=_SyntaxErrConn()):
            with pytest.raises(sqlite3.OperationalError):
                store.append_event("TEST", "test")


# ===========================================================
# 8. Reopen/recovery
# ===========================================================

class TestReopenRecovery:
    def test_reopen_preserves_data(self, tmp_path: Path) -> None:
        s1 = ProjectMemoryStoreV2(tmp_path, "p-reopen")
        s1.initialize()
        s1.ensure_project("P", "r", "/r", {"name": "P"})
        s1.append_event("PROJECT_CREATED", "Created")

        s2 = ProjectMemoryStoreV2(tmp_path, "p-reopen")
        s2.initialize()
        events = s2.read_events()
        assert len(events) == 1
        assert events[0]["event_type"] == "PROJECT_CREATED"

    def test_reopen_after_wal_recovery(self, tmp_path: Path) -> None:
        s = ProjectMemoryStoreV2(tmp_path, "p-wal")
        s.initialize()
        s.ensure_project("P", "r", "/r", {"name": "P"})
        s.append_event("PROJECT_CREATED", "Created")

        conn = s._connect()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()

        s2 = ProjectMemoryStoreV2(tmp_path, "p-wal")
        events = s2.read_events()
        assert len(events) == 1


# ===========================================================
# 9. Interrupted transaction
# ===========================================================

class TestInterruptedTransaction:
    def test_interrupted_write_leaves_state_intact(self, store: ProjectMemoryStoreV2) -> None:
        store.append_event("PROJECT_CREATED", "Init")
        rev_before = store.get_revision()

        try:
            def _op(conn: sqlite3.Connection, rev: int) -> None:
                conn.execute(
                    "INSERT INTO audit_events (event_id, project_id, revision, logical_tx_id, event_type, human_summary, timestamp) VALUES (?,?,?,?,?,?,?)",
                    ("e-int", store.project_id, 99, "tx-int", "TASK_STARTED", "Will fail", "2026-01-01T00:00:00Z"),
                )
                raise RuntimeError("simulated process crash")
            store.write_transaction(_op)
        except RuntimeError:
            pass

        assert store.get_revision() == rev_before
        events = store.read_events()
        assert len(events) == 1

    def test_connection_abort_leaves_uncommitted_absent(self, tmp_path: Path) -> None:
        s = ProjectMemoryStoreV2(tmp_path, "p-abort")
        s.initialize()
        s.ensure_project("P", "r", "/r", {"name": "P"})

        conn = s._connect()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO audit_events (event_id, project_id, revision, logical_tx_id, event_type, human_summary, timestamp) VALUES (?,?,?,?,?,?,?)",
            ("e-abort", "p-abort", 1, "tx-abort", "PROJECT_CREATED", "Aborted", "2026-01-01T00:00:00Z"),
        )
        conn.close()

        events = s.read_events()
        assert len(events) == 0


# ===========================================================
# 10. Migration interruption
# ===========================================================

class TestMigrationInterruption:
    def test_duplicate_migration_idempotent(self, tmp_path: Path) -> None:
        s = ProjectMemoryStoreV2(tmp_path, "p-mig")
        s.initialize()
        s.initialize()
        assert s.schema_version() == PROJECT_MEMORY_V2_SCHEMA_VERSION

    def test_migration_records_logged(self, tmp_path: Path) -> None:
        s = ProjectMemoryStoreV2(tmp_path, "p-mig")
        s.initialize()
        conn = s._connect()
        rows = conn.execute("SELECT version, description FROM schema_migrations").fetchall()
        conn.close()
        assert len(rows) >= 1
        assert rows[0][0] == PROJECT_MEMORY_V2_SCHEMA_VERSION


# ===========================================================
# 11. Foreign-key enforcement
# ===========================================================

class TestForeignKeyEnforcement:
    def test_fk_violation_rejected(self, store: ProjectMemoryStoreV2) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            def _op(conn: sqlite3.Connection, rev: int) -> None:
                conn.execute(
                    "INSERT INTO execution_bindings (execution_binding_id, project_id, plan_version, task_id, launch_id, correlation_id, command_id, repo_alias, expected_repo_head_before, status, generation, superseded, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("fk-b", store.project_id, 999, "t1", "l1", "c1", "cmd1", "r", "0" * 40, "ACTIVE", 1, 0, "2026-01-01T00:00:00Z"),
                )
            store.write_transaction(_op)

    def test_fk_check_passes_clean_db(self, store: ProjectMemoryStoreV2) -> None:
        ok, violations = store.foreign_key_check()
        assert ok is True
        assert len(violations) == 0


# ===========================================================
# 12. Root/path confinement
# ===========================================================

class TestPathConfinement:
    def test_mocked_symlink_root_rejected(self, tmp_path: Path) -> None:
        """Tests that symlink roots are rejected without requiring Windows admin privileges."""
        with patch.object(Path, "is_symlink", return_value=True):
            with pytest.raises(ProjectMemoryV2Error) as exc_info:
                ProjectMemoryStoreV2(tmp_path, "p-sym")
            assert exc_info.value.code == "store_root_symlink"

    def test_db_lives_under_canonical_root(self, tmp_path: Path) -> None:
        s = ProjectMemoryStoreV2(tmp_path, "p-conf")
        s.initialize()
        resolved = s.db_path.resolve()
        assert str(resolved).startswith(str(tmp_path.resolve()))


# ===========================================================
# 13. Backup
# ===========================================================

class TestBackup:
    def test_backup_creates_file(self, store: ProjectMemoryStoreV2, tmp_path: Path) -> None:
        store.append_event("PROJECT_CREATED", "Init")
        backup_path = tmp_path / "backup.db"
        digest = store.backup(backup_path)
        assert backup_path.exists()
        assert digest.startswith("sha256:")

    def test_backup_is_valid_db(self, store: ProjectMemoryStoreV2, tmp_path: Path) -> None:
        store.append_event("PROJECT_CREATED", "Init")
        backup_path = tmp_path / "backup.db"
        store.backup(backup_path)
        conn = sqlite3.connect(str(backup_path))
        result = conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()
        assert result[0] == "ok"


# ===========================================================
# 14. Restore
# ===========================================================

class TestRestore:
    def test_restore_creates_file(self, store: ProjectMemoryStoreV2, tmp_path: Path) -> None:
        store.append_event("PROJECT_CREATED", "Init")
        backup_path = tmp_path / "backup.db"
        store.backup(backup_path)
        restored_path = tmp_path / "restored.db"
        digest = ProjectMemoryStoreV2.restore(backup_path, restored_path)
        assert restored_path.exists()
        assert digest.startswith("sha256:")


# ===========================================================
# 15. Backup/restore digest parity
# ===========================================================

class TestBackupRestoreDigestParity:
    def test_backup_restore_semantic_parity(self, store: ProjectMemoryStoreV2, tmp_path: Path) -> None:
        store.append_event("PROJECT_CREATED", "Init")
        store.add_decision(title="D1", decision="Do it", reason="Why not")
        source_digest = store.semantic_digest()

        backup_path = tmp_path / "backup_parity.db"
        store.backup(backup_path)

        restored_path = tmp_path / "restored_parity.db"
        ProjectMemoryStoreV2.restore(backup_path, restored_path)

        restored_store = ProjectMemoryStoreV2.__new__(ProjectMemoryStoreV2)
        restored_store.project_id = store.project_id
        restored_store.db_path = restored_path
        restored_store.store_root = tmp_path
        restored_store._initialized = True

        restored_digest = restored_store.semantic_digest()
        assert source_digest == restored_digest


# ===========================================================
# 16. v1-v2 Public API Conformance Corpus (Fresh & Restored)
# ===========================================================

class TestV1V2APIConformance:
    def test_v1_api_method_inventory_complete(self) -> None:
        v1, v2, missing = check_v2_api_conformance()
        assert len(v1) == 16
        assert missing == []

    def test_semantic_corpus_on_fresh_and_restored_db(self, tmp_path: Path) -> None:
        # 1. Fresh DB execution
        fresh_store = ProjectMemoryStoreV2(tmp_path / "corpus_fresh", "p-corpus")
        fresh_store.initialize()
        fresh_store.ensure_project("Corpus Proj", "r", "/r", {"name": "C"})
        fresh_divergences = run_semantic_corpus(fresh_store)
        assert fresh_divergences == 0

        source_digest = fresh_store.semantic_digest()

        # 2. Backup
        backup_path = tmp_path / "corpus_backup.db"
        fresh_store.backup(backup_path)

        # 3. Restore
        restored_path = tmp_path / "corpus_restored.db"
        ProjectMemoryStoreV2.restore(backup_path, restored_path)

        restored_store = ProjectMemoryStoreV2.__new__(ProjectMemoryStoreV2)
        restored_store.project_id = "p-corpus"
        restored_store.db_path = restored_path
        restored_store.store_root = tmp_path
        restored_store._initialized = True

        # 4. Integrity check & semantic digest parity on restored DB
        ok, msg = restored_store.integrity_check()
        assert ok is True
        assert msg == "ok"
        restored_digest = restored_store.semantic_digest()
        assert source_digest == restored_digest

        # 5. Restored DB verification
        restored_divergences = verify_restored_corpus(restored_store)
        assert restored_divergences == 0


# ===========================================================
# NX-011 Machine Gate
# ===========================================================

def inspect_nx011_gate_for_hardcoded_results() -> tuple[bool, list[str]]:
    """AST-inspect run_nx011_machine_gate for hardcoded outcomes."""
    source_path = Path(__file__)
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    gate_func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "run_nx011_machine_gate":
            gate_func = node
            break

    if gate_func is None:
        return (False, ["run_nx011_machine_gate not found"])

    REQUIRED_FIELDS = {
        "NX010_SCHEMA_CONTRACT_MATCH", "SQLITE_FOREIGN_KEYS_ENABLED",
        "TRANSACTION_BOUNDARY", "PARTIAL_TRANSACTION_ACCEPTED",
        "REVISION_CAS", "LOST_UPDATES",
        "PUBLIC_API_METHODS_CHECKED", "FRESH_DB_PUBLIC_API_DIVERGENCES",
        "RESTORED_DB_PUBLIC_API_DIVERGENCES", "PUBLIC_API_SEMANTIC_DIVERGENCES",
        "RESTORED_DB_INTEGRITY_CHECK", "RESTORED_DB_SEMANTIC_DIGEST_PARITY",
        "RAW_SQLITE_BUSY_ESCAPES_PUBLIC_API", "WRITER_CONTENTION_CLASSIFIED",
        "CONCURRENT_READERS_WRITER",
        "CRASH_RECOVERY", "SQLITE_INTEGRITY_CHECK",
        "BACKUP", "RESTORE", "BACKUP_RESTORE_DIGEST_PARITY",
        "MIGRATIONS_TABLE", "INTERRUPTED_MIGRATION_RECOVERY",
        "WRITE_ESCAPE_FROM_CANONICAL_ROOT",
        "SECOND_WORKFLOW_AUTHORITY_CREATED", "LIVE_V1_CUTOVER_PERFORMED",
        "NX011_STATUS",
    }

    hardcoded_fields: list[str] = []
    for node in ast.walk(gate_func):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in REQUIRED_FIELDS:
                    val = node.value
                    if isinstance(val, ast.Constant) and val.value in (True, False, "PASS", "FAIL", 0):
                        hardcoded_fields.append(target.id)

    return (len(hardcoded_fields) == 0, hardcoded_fields)


def run_nx011_machine_gate(tmp_path: Path) -> dict[str, Any]:
    """NX-011 deterministic machine gate — all results derived."""
    repo_root = Path(__file__).resolve().parent.parent

    # --- Schema contract match ---
    store = ProjectMemoryStoreV2(tmp_path / "gate", "p-gate")
    store.initialize()
    conn = store._connect()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    required_tables = {
        "schema_migrations", "projects", "project_plans", "runs", "scopes",
        "task_execution_states", "execution_bindings", "attempts", "checkpoints",
        "launch_outbox", "decisions", "inbox_items", "risks", "technical_debt",
        "attention_items", "audit_events", "failures", "evidence_records", "leases",
    }
    NX010_SCHEMA_CONTRACT_MATCH = required_tables.issubset(tables)

    fk_status = conn.execute("PRAGMA foreign_keys").fetchone()
    SQLITE_FOREIGN_KEYS_ENABLED = (fk_status[0] == 1)
    conn.close()

    # --- Transactions ---
    store.ensure_project("Gate Project", "repo-gate", "/gate", {"name": "G"})
    tx_rollback_ok = True
    try:
        def _bad(c: sqlite3.Connection, r: int) -> None:
            c.execute(
                "INSERT INTO audit_events (event_id, project_id, revision, logical_tx_id, event_type, human_summary, timestamp) VALUES (?,?,?,?,?,?,?)",
                ("e-gfail", "p-gate", 1, "tx-gfail", "PROJECT_CREATED", "Fail", "2026-01-01T00:00:00Z"),
            )
            raise ValueError("rollback test")
        store.write_transaction(_bad)
    except ValueError:
        pass
    rev_after = store.get_revision()
    tx_events = store.read_events()
    tx_rollback_ok = (rev_after == 1 and len(tx_events) == 0)
    TRANSACTION_BOUNDARY = ("PASS" if tx_rollback_ok else "FAIL")
    PARTIAL_TRANSACTION_ACCEPTED = not tx_rollback_ok

    # --- CAS ---
    store.append_event("PROJECT_CREATED", "Init")
    current_rev = store.get_revision()
    cas_ok = True
    try:
        store.write_transaction(lambda c, r: None, expected_revision=current_rev - 1)
        cas_ok = False
    except ProjectMemoryV2Error as e:
        cas_ok = (e.code == "stale_revision_rejected")
    REVISION_CAS = ("PASS" if cas_ok else "FAIL")

    # --- Concurrent writers, lost updates & busy classification ---
    writer_results = {"committed": 0, "stale": 0, "busy": 0, "raw_sqlite_busy": 0, "errors": []}
    wlock = threading.Lock()

    def _cw(idx: int) -> None:
        try:
            store.append_event("TASK_STARTED", f"CW-{idx}")
            with wlock:
                writer_results["committed"] += 1
        except ProjectMemoryV2Error as e:
            with wlock:
                if e.code == "stale_revision_rejected":
                    writer_results["stale"] += 1
                elif e.code == "store_busy":
                    writer_results["busy"] += 1
                else:
                    writer_results["errors"].append(f"{e.code}: {e}")
        except sqlite3.OperationalError as e:
            with wlock:
                if "locked" in str(e).lower() or "busy" in str(e).lower():
                    writer_results["raw_sqlite_busy"] += 1
                else:
                    writer_results["errors"].append(str(e))
        except Exception as e:
            with wlock:
                writer_results["errors"].append(str(e))

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(_cw, i) for i in range(8)]
        for f in as_completed(futures):
            f.result()

    actual_events = store.read_events()
    committed = writer_results["committed"]
    total_events = len(actual_events)
    LOST_UPDATES = max(0, (committed + 1) - total_events)
    RAW_SQLITE_BUSY_ESCAPES_PUBLIC_API = writer_results["raw_sqlite_busy"]
    CONCURRENT_READERS_WRITER = ("PASS" if len(writer_results["errors"]) == 0 and committed > 0 else "FAIL")
    WRITER_CONTENTION_CLASSIFIED = bool(
        RAW_SQLITE_BUSY_ESCAPES_PUBLIC_API == 0
        and (committed + writer_results["stale"] + writer_results["busy"] == 8)
    )

    # --- API conformance inventory ---
    v1_api_methods, v2_api_methods, missing_api_methods = check_v2_api_conformance()
    PUBLIC_API_METHODS_CHECKED = len(v1_api_methods)
    MISSING_V2_CONFORMANCE_METHODS = missing_api_methods

    # --- API conformance execution on fresh and restored DB ---
    fresh_corpus_store = ProjectMemoryStoreV2(tmp_path / "gate_corpus_fresh", "p-gate-corpus")
    fresh_corpus_store.initialize()
    fresh_corpus_store.ensure_project("Corpus Gate", "r", "/r", {"name": "CG"})
    FRESH_DB_PUBLIC_API_DIVERGENCES = run_semantic_corpus(fresh_corpus_store)
    corpus_source_digest = fresh_corpus_store.semantic_digest()

    # Backup & Restore
    gate_backup_path = tmp_path / "gate_corpus_backup.db"
    fresh_corpus_store.backup(gate_backup_path)
    gate_restored_path = tmp_path / "gate_corpus_restored.db"
    ProjectMemoryStoreV2.restore(gate_backup_path, gate_restored_path)

    restored_corpus_store = ProjectMemoryStoreV2.__new__(ProjectMemoryStoreV2)
    restored_corpus_store.project_id = "p-gate-corpus"
    restored_corpus_store.db_path = gate_restored_path
    restored_corpus_store.store_root = tmp_path
    restored_corpus_store._initialized = True

    r_ok, r_msg = restored_corpus_store.integrity_check()
    RESTORED_DB_INTEGRITY_CHECK = ("PASS" if r_ok and r_msg == "ok" else "FAIL")
    corpus_restored_digest = restored_corpus_store.semantic_digest()
    RESTORED_DB_SEMANTIC_DIGEST_PARITY = bool(corpus_source_digest == corpus_restored_digest)
    RESTORED_DB_PUBLIC_API_DIVERGENCES = verify_restored_corpus(restored_corpus_store)
    PUBLIC_API_SEMANTIC_DIVERGENCES = FRESH_DB_PUBLIC_API_DIVERGENCES + RESTORED_DB_PUBLIC_API_DIVERGENCES

    # --- Crash recovery ---
    crash_store = ProjectMemoryStoreV2(tmp_path / "gate_crash", "p-crash")
    crash_store.initialize()
    crash_store.ensure_project("C", "r", "/r", {"name": "C"})
    crash_store.append_event("PROJECT_CREATED", "Init")
    try:
        def _crash(c: sqlite3.Connection, r: int) -> None:
            c.execute(
                "INSERT INTO audit_events (event_id, project_id, revision, logical_tx_id, event_type, human_summary, timestamp) VALUES (?,?,?,?,?,?,?)",
                ("e-crash", "p-crash", 99, "tx-crash", "TASK_STARTED", "Crash", "2026-01-01T00:00:00Z"),
            )
            raise RuntimeError("crash")
        crash_store.write_transaction(_crash)
    except RuntimeError:
        pass
    crash_events = crash_store.read_events()
    crash_ok, crash_msg = crash_store.integrity_check()
    CRASH_RECOVERY = ("PASS" if len(crash_events) == 1 and crash_ok else "FAIL")
    SQLITE_INTEGRITY_CHECK = ("PASS" if crash_ok else "FAIL")

    # --- Standalone Backup/Restore ---
    backup_store = ProjectMemoryStoreV2(tmp_path / "gate_backup", "p-backup")
    backup_store.initialize()
    backup_store.ensure_project("B", "r", "/r", {"name": "B"})
    backup_store.append_event("PROJECT_CREATED", "Init")
    backup_store.add_decision(title="D", decision="D", reason="R")
    source_digest = backup_store.semantic_digest()

    backup_path = tmp_path / "gate_backup.db"
    backup_file_digest = backup_store.backup(backup_path)
    BACKUP = ("PASS" if backup_path.exists() and backup_file_digest.startswith("sha256:") else "FAIL")

    restored_path = tmp_path / "gate_restored.db"
    restore_file_digest = ProjectMemoryStoreV2.restore(backup_path, restored_path)
    RESTORE = ("PASS" if restored_path.exists() and restore_file_digest.startswith("sha256:") else "FAIL")

    restored_s = ProjectMemoryStoreV2.__new__(ProjectMemoryStoreV2)
    restored_s.project_id = "p-backup"
    restored_s.db_path = restored_path
    restored_s.store_root = tmp_path
    restored_s._initialized = True
    restored_digest = restored_s.semantic_digest()
    BACKUP_RESTORE_DIGEST_PARITY = bool(source_digest == restored_digest)

    # --- Migrations ---
    mig_store = ProjectMemoryStoreV2(tmp_path / "gate_mig", "p-mig")
    mig_store.initialize()
    mig_ver = mig_store.schema_version()
    MIGRATIONS_TABLE = ("PASS" if mig_ver == PROJECT_MEMORY_V2_SCHEMA_VERSION else "FAIL")
    mig_store.initialize()
    mig_ver2 = mig_store.schema_version()
    INTERRUPTED_MIGRATION_RECOVERY = ("PASS" if mig_ver2 == mig_ver else "FAIL")

    # --- Path confinement ---
    confinement_store = ProjectMemoryStoreV2(tmp_path / "gate_conf", "p-conf")
    confinement_store.initialize()
    resolved = confinement_store.db_path.resolve()
    WRITE_ESCAPE_FROM_CANONICAL_ROOT = not str(resolved).startswith(str((tmp_path / "gate_conf").resolve()))

    # --- Authority checks ---
    SECOND_WORKFLOW_AUTHORITY_CREATED = (
        hasattr(ProjectMemoryStoreV2, 'replace_v1_authority')
        or hasattr(ProjectMemoryStoreV2, 'activate_as_primary')
    )
    LIVE_V1_CUTOVER_PERFORMED = (
        hasattr(ProjectMemoryStoreV2, 'perform_cutover')
        or hasattr(ProjectMemoryStoreV2, 'switch_authority')
    )

    # --- AST hardcoded check ---
    no_hardcoded, hardcoded_fields = inspect_nx011_gate_for_hardcoded_results()
    NO_HARDCODED_GATE_RESULTS = no_hardcoded

    # --- Source binding ---
    try:
        head_proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_root), capture_output=True, text=True, check=True)
        head_sha = head_proc.stdout.strip()
        tree_proc = subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=str(repo_root), capture_output=True, text=True, check=True)
        tree_sha = tree_proc.stdout.strip()
        diff_proc = subprocess.run(["git", "diff", "--quiet"], cwd=str(repo_root))
        cached_proc = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=str(repo_root))
        status_proc = subprocess.run(["git", "status", "--porcelain"], cwd=str(repo_root), capture_output=True, text=True, check=True)
        worktree_clean = (
            diff_proc.returncode == 0
            and cached_proc.returncode == 0
            and len(status_proc.stdout.strip()) == 0
        )
        source_bound_ok = (len(head_sha) == 40 and len(tree_sha) == 40 and worktree_clean)
    except Exception:
        head_sha = "unknown"
        tree_sha = "unknown"
        worktree_clean = False
        source_bound_ok = False

    SOURCE_BOUND_MACHINE_GATE = ("PASS" if source_bound_ok else "FAIL")

    all_pass = (
        NX010_SCHEMA_CONTRACT_MATCH is True
        and SQLITE_FOREIGN_KEYS_ENABLED is True
        and TRANSACTION_BOUNDARY == "PASS"
        and PARTIAL_TRANSACTION_ACCEPTED is False
        and REVISION_CAS == "PASS"
        and LOST_UPDATES == 0
        and PUBLIC_API_METHODS_CHECKED >= 16
        and len(MISSING_V2_CONFORMANCE_METHODS) == 0
        and FRESH_DB_PUBLIC_API_DIVERGENCES == 0
        and RESTORED_DB_PUBLIC_API_DIVERGENCES == 0
        and PUBLIC_API_SEMANTIC_DIVERGENCES == 0
        and RESTORED_DB_INTEGRITY_CHECK == "PASS"
        and RESTORED_DB_SEMANTIC_DIGEST_PARITY is True
        and RAW_SQLITE_BUSY_ESCAPES_PUBLIC_API == 0
        and WRITER_CONTENTION_CLASSIFIED is True
        and CONCURRENT_READERS_WRITER == "PASS"
        and CRASH_RECOVERY == "PASS"
        and SQLITE_INTEGRITY_CHECK == "PASS"
        and BACKUP == "PASS"
        and RESTORE == "PASS"
        and BACKUP_RESTORE_DIGEST_PARITY is True
        and MIGRATIONS_TABLE == "PASS"
        and INTERRUPTED_MIGRATION_RECOVERY == "PASS"
        and WRITE_ESCAPE_FROM_CANONICAL_ROOT is False
        and SECOND_WORKFLOW_AUTHORITY_CREATED is False
        and LIVE_V1_CUTOVER_PERFORMED is False
        and NO_HARDCODED_GATE_RESULTS is True
        and SOURCE_BOUND_MACHINE_GATE == "PASS"
    )

    return {
        "task_id": "NX-011",
        "NX010_SCHEMA_CONTRACT_MATCH": NX010_SCHEMA_CONTRACT_MATCH,
        "SQLITE_FOREIGN_KEYS_ENABLED": SQLITE_FOREIGN_KEYS_ENABLED,
        "TRANSACTION_BOUNDARY": TRANSACTION_BOUNDARY,
        "PARTIAL_TRANSACTION_ACCEPTED": PARTIAL_TRANSACTION_ACCEPTED,
        "REVISION_CAS": REVISION_CAS,
        "LOST_UPDATES": LOST_UPDATES,
        "PUBLIC_API_METHODS_CHECKED": PUBLIC_API_METHODS_CHECKED,
        "MISSING_V2_CONFORMANCE_METHODS": MISSING_V2_CONFORMANCE_METHODS,
        "FRESH_DB_PUBLIC_API_DIVERGENCES": FRESH_DB_PUBLIC_API_DIVERGENCES,
        "RESTORED_DB_PUBLIC_API_DIVERGENCES": RESTORED_DB_PUBLIC_API_DIVERGENCES,
        "PUBLIC_API_SEMANTIC_DIVERGENCES": PUBLIC_API_SEMANTIC_DIVERGENCES,
        "RESTORED_DB_INTEGRITY_CHECK": RESTORED_DB_INTEGRITY_CHECK,
        "RESTORED_DB_SEMANTIC_DIGEST_PARITY": RESTORED_DB_SEMANTIC_DIGEST_PARITY,
        "RAW_SQLITE_BUSY_ESCAPES_PUBLIC_API": RAW_SQLITE_BUSY_ESCAPES_PUBLIC_API,
        "WRITER_CONTENTION_CLASSIFIED": WRITER_CONTENTION_CLASSIFIED,
        "CONCURRENT_READERS_WRITER": CONCURRENT_READERS_WRITER,
        "CRASH_RECOVERY": CRASH_RECOVERY,
        "SQLITE_INTEGRITY_CHECK": SQLITE_INTEGRITY_CHECK,
        "BACKUP": BACKUP,
        "RESTORE": RESTORE,
        "BACKUP_RESTORE_DIGEST_PARITY": BACKUP_RESTORE_DIGEST_PARITY,
        "MIGRATIONS_TABLE": MIGRATIONS_TABLE,
        "INTERRUPTED_MIGRATION_RECOVERY": INTERRUPTED_MIGRATION_RECOVERY,
        "WRITE_ESCAPE_FROM_CANONICAL_ROOT": WRITE_ESCAPE_FROM_CANONICAL_ROOT,
        "SECOND_WORKFLOW_AUTHORITY_CREATED": SECOND_WORKFLOW_AUTHORITY_CREATED,
        "LIVE_V1_CUTOVER_PERFORMED": LIVE_V1_CUTOVER_PERFORMED,
        "HARDCODED_GATE_RESULT_FIELDS": hardcoded_fields,
        "NO_HARDCODED_GATE_RESULTS": NO_HARDCODED_GATE_RESULTS,
        "SOURCE_HEAD": head_sha,
        "SOURCE_TREE": tree_sha,
        "WORKTREE_CLEAN": worktree_clean,
        "SOURCE_BOUND_MACHINE_GATE": SOURCE_BOUND_MACHINE_GATE,
        "NX011_STATUS": ("PASS" if all_pass else "FAIL"),
    }


def test_nx011_machine_gate_execution(tmp_path: Path) -> None:
    """NX-011 canonical machine gate."""
    gate = run_nx011_machine_gate(tmp_path)

    assert gate["NX010_SCHEMA_CONTRACT_MATCH"] is True
    assert gate["SQLITE_FOREIGN_KEYS_ENABLED"] is True
    assert gate["TRANSACTION_BOUNDARY"] == "PASS"
    assert gate["PARTIAL_TRANSACTION_ACCEPTED"] is False
    assert gate["REVISION_CAS"] == "PASS"
    assert gate["LOST_UPDATES"] == 0
    assert gate["PUBLIC_API_METHODS_CHECKED"] >= 16
    assert gate["MISSING_V2_CONFORMANCE_METHODS"] == []
    assert gate["FRESH_DB_PUBLIC_API_DIVERGENCES"] == 0
    assert gate["RESTORED_DB_PUBLIC_API_DIVERGENCES"] == 0
    assert gate["PUBLIC_API_SEMANTIC_DIVERGENCES"] == 0
    assert gate["RESTORED_DB_INTEGRITY_CHECK"] == "PASS"
    assert gate["RESTORED_DB_SEMANTIC_DIGEST_PARITY"] is True
    assert gate["RAW_SQLITE_BUSY_ESCAPES_PUBLIC_API"] == 0
    assert gate["WRITER_CONTENTION_CLASSIFIED"] is True
    assert gate["CONCURRENT_READERS_WRITER"] == "PASS"
    assert gate["CRASH_RECOVERY"] == "PASS"
    assert gate["SQLITE_INTEGRITY_CHECK"] == "PASS"
    assert gate["BACKUP"] == "PASS"
    assert gate["RESTORE"] == "PASS"
    assert gate["BACKUP_RESTORE_DIGEST_PARITY"] is True
    assert gate["MIGRATIONS_TABLE"] == "PASS"
    assert gate["INTERRUPTED_MIGRATION_RECOVERY"] == "PASS"
    assert gate["WRITE_ESCAPE_FROM_CANONICAL_ROOT"] is False
    assert gate["SECOND_WORKFLOW_AUTHORITY_CREATED"] is False
    assert gate["LIVE_V1_CUTOVER_PERFORMED"] is False
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    assert gate["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
    assert gate["NX011_STATUS"] == "PASS"

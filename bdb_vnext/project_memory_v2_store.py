"""ProjectMemoryStoreV2 — Transactional SQLite backend under NX-010 contract.

NX-011: Implements a bounded, ACID-compliant store using the NX-010 DDL schema.
This does NOT become a second workflow authority — it lives beneath the
existing logical API and can be qualified independently.

Policies:
- journal_mode = WAL (best read concurrency on Windows/Linux)
- synchronous = NORMAL (durable after WAL checkpoint, not individual txn)
- busy_timeout = 5000 ms
- foreign_keys = ON (always)
- connection lifetime = per-operation (no long-lived connections)
- schema version tracked in schema_migrations table
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence, TypeVar

from .project_memory_v2_contract import (
    PROJECT_MEMORY_V2_DDL,
    PROJECT_MEMORY_V2_SCHEMA_VERSION,
    PROJECT_MEMORY_V2_SCHEMA_IDENTIFIER,
)


class ProjectMemoryV2Error(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> None:
    raise ProjectMemoryV2Error(code, message)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


_T = TypeVar("_T")

_SQLITE_SCHEMA_VERSION = PROJECT_MEMORY_V2_SCHEMA_VERSION
_BUSY_TIMEOUT_MS = 5000
_JOURNAL_MODE = "WAL"
_SYNCHRONOUS = "NORMAL"

_MIGRATION_REGISTRY: list[tuple[str, str, str]] = [
    (_SQLITE_SCHEMA_VERSION, "Initial NX-010 schema", PROJECT_MEMORY_V2_DDL),
]

# Canonical list of public mutation & access methods required for v1 conformance
V1_PUBLIC_API_METHODS: tuple[str, ...] = (
    "append_event",
    "add_decision",
    "add_inbox",
    "update_inbox",
    "add_risk",
    "resolve_risk",
    "add_debt",
    "resolve_debt",
    "add_attention",
    "resolve_attention",
    "create_checkpoint",
    "ensure_initial_plan",
    "apply_update",
    "execution_transaction",
    "write_transaction",
    "read_state",
)


def check_v2_api_conformance() -> tuple[list[str], list[str], list[str]]:
    """Checks ProjectMemoryStoreV2 methods against the canonical V1_PUBLIC_API_METHODS list."""
    v1_methods = list(V1_PUBLIC_API_METHODS)
    v2_methods = [
        m for m in v1_methods
        if hasattr(ProjectMemoryStoreV2, m) and callable(getattr(ProjectMemoryStoreV2, m))
    ]
    missing = [m for m in v1_methods if m not in v2_methods]
    return v1_methods, v2_methods, missing


class ProjectMemoryStoreV2:
    """Transactional SQLite-backed Project Memory v2 store.

    Lives inside the canonical runtime root.
    Does NOT replace v1 as live production authority.
    """

    def __init__(self, runtime_root: str | Path, project_id: str) -> None:
        self.project_id = project_id
        root = Path(runtime_root).expanduser().resolve()

        # Path confinement: reject symlinks
        if root.exists() and root.is_symlink():
            _fail("store_root_symlink", "runtime root must not be a symlink")

        self.store_root = root / "control" / "project-memory-v2"
        self.store_root.mkdir(parents=True, exist_ok=True)

        # Validate confinement: resolved path must still be under runtime_root
        resolved_root = self.store_root.resolve()
        if not str(resolved_root).startswith(str(root.resolve())):
            _fail("path_traversal_detected", "store path escapes canonical root")

        self.db_path = self.store_root / f"{project_id}.db"
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        """Open a connection with canonical pragmas."""
        try:
            conn = sqlite3.connect(str(self.db_path), timeout=_BUSY_TIMEOUT_MS / 1000)
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(f"PRAGMA journal_mode = {_JOURNAL_MODE}")
            conn.execute(f"PRAGMA synchronous = {_SYNCHRONOUS}")
            conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            return conn
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() or "busy" in str(e).lower():
                _fail("store_busy", f"project memory database is busy: {e}")
            raise

    def initialize(self) -> None:
        """Apply schema migrations (idempotent)."""
        conn = self._connect()
        try:
            # Ensure migrations table exists first
            conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL,
                    description TEXT NOT NULL
                )
            """)
            conn.commit()

            for version, description, ddl in _MIGRATION_REGISTRY:
                existing = conn.execute(
                    "SELECT version FROM schema_migrations WHERE version = ?", (version,)
                ).fetchone()
                if existing is not None:
                    continue
                try:
                    conn.executescript(ddl)
                    conn.execute(
                        "INSERT INTO schema_migrations (version, applied_at, description) VALUES (?, ?, ?)",
                        (version, _now(), description),
                    )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            self._initialized = True
        finally:
            conn.close()

    def _ensure_initialized(self) -> None:
        if not self._initialized:
            self.initialize()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """Context manager yielding a connection within a BEGIN IMMEDIATE transaction.

        Classifies transient SQLite busy/locked contention into ProjectMemoryV2Error('store_busy').
        """
        self._ensure_initialized()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except sqlite3.OperationalError as e:
            try:
                conn.rollback()
            except Exception:
                pass
            if "locked" in str(e).lower() or "busy" in str(e).lower():
                _fail("store_busy", f"project memory database is busy: {e}")
            raise
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def get_revision(self) -> int:
        """Read current project revision."""
        self._ensure_initialized()
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT revision FROM projects WHERE project_id = ?", (self.project_id,)
            ).fetchone()
            return row[0] if row else 0
        finally:
            conn.close()

    def ensure_project(self, display_name: str, repo_alias: str, local_repo_path: str, brief: Mapping[str, Any]) -> None:
        """Create the project row if it doesn't exist."""
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT project_id FROM projects WHERE project_id = ?", (self.project_id,)
            ).fetchone()
            if existing:
                return
            conn.execute(
                "INSERT INTO projects (project_id, display_name, repo_alias, local_repo_path, github_repo, brief_json, revision, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (self.project_id, display_name, repo_alias, local_repo_path, None, json.dumps(brief, sort_keys=True), 1, _now(), _now()),
            )

    def write_transaction(
        self,
        operation: Callable[[sqlite3.Connection, int], _T],
        *,
        expected_revision: int | None = None,
    ) -> _T:
        """Execute a transactional operation with CAS revision check.

        The operation receives (conn, current_revision) and must return the result.
        Revision is auto-incremented on success.
        """
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT revision FROM projects WHERE project_id = ?", (self.project_id,)
            ).fetchone()
            if row is None:
                _fail("project_not_found", f"project {self.project_id} does not exist")
            current_revision = row[0]

            if expected_revision is not None and current_revision != expected_revision:
                _fail(
                    "stale_revision_rejected",
                    f"expected revision {expected_revision}, but current is {current_revision}",
                )

            result = operation(conn, current_revision)

            next_revision = current_revision + 1
            conn.execute(
                "UPDATE projects SET revision = ?, updated_at = ? WHERE project_id = ?",
                (next_revision, _now(), self.project_id),
            )
            return result

    def execution_transaction(
        self,
        operation: Callable[[sqlite3.Connection, int], _T],
        *,
        expected_revision: int | None = None,
    ) -> _T:
        """Commit execution transition atomically with CAS revision check."""
        return self.write_transaction(operation, expected_revision=expected_revision)

    def append_event(self, event_type: str, human_summary: str, **kwargs: str | None) -> dict[str, Any]:
        """Append a single audit event within a transaction."""
        def _op(conn: sqlite3.Connection, rev: int) -> dict[str, Any]:
            max_rev_row = conn.execute(
                "SELECT COALESCE(MAX(revision), 0) FROM audit_events WHERE project_id = ?",
                (self.project_id,),
            ).fetchone()
            next_event_rev = max_rev_row[0] + 1

            event_id = f"{self.project_id}:e{next_event_rev:06d}"
            tx_id = f"tx-{uuid.uuid4().hex[:12]}"
            ts = _now()

            conn.execute(
                "INSERT INTO audit_events (event_id, project_id, revision, logical_tx_id, event_type, human_summary, task_id, milestone_id, plan_version, git_head, correlation_id, timestamp) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, self.project_id, next_event_rev, tx_id, event_type, human_summary,
                 kwargs.get("task_id"), kwargs.get("milestone_id"),
                 kwargs.get("plan_version"), kwargs.get("git_head"),
                 kwargs.get("correlation_id"), ts),
            )
            return {"event_id": event_id, "revision": next_event_rev, "timestamp": ts}

        return self.write_transaction(_op)

    def add_decision(
        self,
        *,
        title: str,
        decision: str,
        reason: str,
        plan_version: str | None = None,
        related_task_ids: Sequence[str] = (),
        supersedes_decision_id: str | None = None,
    ) -> dict[str, Any]:
        """Add a decision record."""
        def _op(conn: sqlite3.Connection, rev: int) -> dict[str, Any]:
            count = conn.execute("SELECT COUNT(*) FROM decisions WHERE project_id = ?", (self.project_id,)).fetchone()[0]
            decision_id = f"D-{count + 1:03d}"
            ts = _now()
            conn.execute(
                "INSERT INTO decisions (decision_id, project_id, title, decision, reason, status, created_at, related_task_ids_json, related_plan_version, supersedes_decision_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (decision_id, self.project_id, title, decision, reason, "active", ts, json.dumps(list(related_task_ids)), int(plan_version) if plan_version else None, supersedes_decision_id),
            )
            tx_id = f"tx-{uuid.uuid4().hex[:12]}"
            max_rev = conn.execute("SELECT COALESCE(MAX(revision), 0) FROM audit_events WHERE project_id = ?", (self.project_id,)).fetchone()[0]
            conn.execute(
                "INSERT INTO audit_events (event_id, project_id, revision, logical_tx_id, event_type, human_summary, timestamp) VALUES (?,?,?,?,?,?,?)",
                (f"{self.project_id}:e{max_rev + 1:06d}", self.project_id, max_rev + 1, tx_id, "DECISION_ADDED", f"Decision added: {title}", ts),
            )
            return {"decision_id": decision_id, "status": "active"}
        return self.write_transaction(_op)

    def add_inbox(self, *, title: str, description: str) -> dict[str, Any]:
        """Add an inbox item."""
        def _op(conn: sqlite3.Connection, rev: int) -> dict[str, Any]:
            count = conn.execute("SELECT COUNT(*) FROM inbox_items WHERE project_id = ?", (self.project_id,)).fetchone()[0]
            inbox_id = f"I-{count + 1:03d}"
            ts = _now()
            conn.execute(
                "INSERT INTO inbox_items (inbox_id, project_id, title, description, status, created_at) VALUES (?,?,?,?,?,?)",
                (inbox_id, self.project_id, title, description, "new", ts),
            )
            tx_id = f"tx-{uuid.uuid4().hex[:12]}"
            max_rev = conn.execute("SELECT COALESCE(MAX(revision), 0) FROM audit_events WHERE project_id = ?", (self.project_id,)).fetchone()[0]
            conn.execute(
                "INSERT INTO audit_events (event_id, project_id, revision, logical_tx_id, event_type, human_summary, timestamp) VALUES (?,?,?,?,?,?,?)",
                (f"{self.project_id}:e{max_rev + 1:06d}", self.project_id, max_rev + 1, tx_id, "INBOX_ITEM_ADDED", f"Inbox: {title}", ts),
            )
            return {"inbox_id": inbox_id, "status": "new"}
        return self.write_transaction(_op)

    def update_inbox(self, inbox_id: str, status: str) -> dict[str, Any]:
        """Update status of an inbox item."""
        def _op(conn: sqlite3.Connection, rev: int) -> dict[str, Any]:
            row = conn.execute(
                "SELECT title FROM inbox_items WHERE inbox_id = ? AND project_id = ?",
                (inbox_id, self.project_id),
            ).fetchone()
            if row is None:
                _fail("inbox_not_found", f"inbox item {inbox_id} does not exist")
            conn.execute(
                "UPDATE inbox_items SET status = ? WHERE inbox_id = ? AND project_id = ?",
                (status, inbox_id, self.project_id),
            )
            ts = _now()
            tx_id = f"tx-{uuid.uuid4().hex[:12]}"
            max_rev = conn.execute("SELECT COALESCE(MAX(revision), 0) FROM audit_events WHERE project_id = ?", (self.project_id,)).fetchone()[0]
            conn.execute(
                "INSERT INTO audit_events (event_id, project_id, revision, logical_tx_id, event_type, human_summary, timestamp) VALUES (?,?,?,?,?,?,?)",
                (f"{self.project_id}:e{max_rev + 1:06d}", self.project_id, max_rev + 1, tx_id, "INBOX_ITEM_RESOLVED" if status == "resolved" else "INBOX_ITEM_UPDATED", f"Inbox {inbox_id}: {status}", ts),
            )
            return {"inbox_id": inbox_id, "status": status}
        return self.write_transaction(_op)

    def add_risk(self, *, title: str, description: str, severity: str = "medium") -> dict[str, Any]:
        """Add a risk record."""
        def _op(conn: sqlite3.Connection, rev: int) -> dict[str, Any]:
            count = conn.execute("SELECT COUNT(*) FROM risks WHERE project_id = ?", (self.project_id,)).fetchone()[0]
            risk_id = f"R-{count + 1:03d}"
            ts = _now()
            conn.execute(
                "INSERT INTO risks (risk_id, project_id, title, description, severity, status, created_at) VALUES (?,?,?,?,?,?,?)",
                (risk_id, self.project_id, title, description, severity, "open", ts),
            )
            tx_id = f"tx-{uuid.uuid4().hex[:12]}"
            max_rev = conn.execute("SELECT COALESCE(MAX(revision), 0) FROM audit_events WHERE project_id = ?", (self.project_id,)).fetchone()[0]
            conn.execute(
                "INSERT INTO audit_events (event_id, project_id, revision, logical_tx_id, event_type, human_summary, timestamp) VALUES (?,?,?,?,?,?,?)",
                (f"{self.project_id}:e{max_rev + 1:06d}", self.project_id, max_rev + 1, tx_id, "RISK_ADDED", f"Risk: {title}", ts),
            )
            return {"risk_id": risk_id, "status": "open"}
        return self.write_transaction(_op)

    def resolve_risk(self, risk_id: str, status: str = "resolved") -> dict[str, Any]:
        """Resolve or update risk status."""
        def _op(conn: sqlite3.Connection, rev: int) -> dict[str, Any]:
            row = conn.execute(
                "SELECT title FROM risks WHERE risk_id = ? AND project_id = ?",
                (risk_id, self.project_id),
            ).fetchone()
            if row is None:
                _fail("risk_not_found", f"risk {risk_id} does not exist")
            conn.execute(
                "UPDATE risks SET status = ? WHERE risk_id = ? AND project_id = ?",
                (status, risk_id, self.project_id),
            )
            ts = _now()
            tx_id = f"tx-{uuid.uuid4().hex[:12]}"
            max_rev = conn.execute("SELECT COALESCE(MAX(revision), 0) FROM audit_events WHERE project_id = ?", (self.project_id,)).fetchone()[0]
            conn.execute(
                "INSERT INTO audit_events (event_id, project_id, revision, logical_tx_id, event_type, human_summary, timestamp) VALUES (?,?,?,?,?,?,?)",
                (f"{self.project_id}:e{max_rev + 1:06d}", self.project_id, max_rev + 1, tx_id, "RISK_RESOLVED", f"Risk {risk_id}: {status}", ts),
            )
            return {"risk_id": risk_id, "status": status}
        return self.write_transaction(_op)

    def add_debt(self, *, title: str, description: str, related_task_ids: Sequence[str] = (), suggested_review_milestone: str | None = None) -> dict[str, Any]:
        """Add a technical debt record."""
        def _op(conn: sqlite3.Connection, rev: int) -> dict[str, Any]:
            count = conn.execute("SELECT COUNT(*) FROM technical_debt WHERE project_id = ?", (self.project_id,)).fetchone()[0]
            debt_id = f"TD-{count + 1:03d}"
            ts = _now()
            conn.execute(
                "INSERT INTO technical_debt (debt_id, project_id, title, description, status, created_at, related_task_ids_json, suggested_review_milestone) VALUES (?,?,?,?,?,?,?,?)",
                (debt_id, self.project_id, title, description, "open", ts, json.dumps(list(related_task_ids)), suggested_review_milestone),
            )
            tx_id = f"tx-{uuid.uuid4().hex[:12]}"
            max_rev = conn.execute("SELECT COALESCE(MAX(revision), 0) FROM audit_events WHERE project_id = ?", (self.project_id,)).fetchone()[0]
            conn.execute(
                "INSERT INTO audit_events (event_id, project_id, revision, logical_tx_id, event_type, human_summary, timestamp) VALUES (?,?,?,?,?,?,?)",
                (f"{self.project_id}:e{max_rev + 1:06d}", self.project_id, max_rev + 1, tx_id, "TECH_DEBT_ADDED", f"Debt: {title}", ts),
            )
            return {"debt_id": debt_id, "status": "open"}
        return self.write_transaction(_op)

    def resolve_debt(self, debt_id: str, status: str = "resolved") -> dict[str, Any]:
        """Resolve technical debt."""
        def _op(conn: sqlite3.Connection, rev: int) -> dict[str, Any]:
            row = conn.execute(
                "SELECT title FROM technical_debt WHERE debt_id = ? AND project_id = ?",
                (debt_id, self.project_id),
            ).fetchone()
            if row is None:
                _fail("debt_not_found", f"technical debt {debt_id} does not exist")
            conn.execute(
                "UPDATE technical_debt SET status = ? WHERE debt_id = ? AND project_id = ?",
                (status, debt_id, self.project_id),
            )
            ts = _now()
            tx_id = f"tx-{uuid.uuid4().hex[:12]}"
            max_rev = conn.execute("SELECT COALESCE(MAX(revision), 0) FROM audit_events WHERE project_id = ?", (self.project_id,)).fetchone()[0]
            conn.execute(
                "INSERT INTO audit_events (event_id, project_id, revision, logical_tx_id, event_type, human_summary, timestamp) VALUES (?,?,?,?,?,?,?)",
                (f"{self.project_id}:e{max_rev + 1:06d}", self.project_id, max_rev + 1, tx_id, "TECH_DEBT_RESOLVED", f"Debt {debt_id}: {status}", ts),
            )
            return {"debt_id": debt_id, "status": status}
        return self.write_transaction(_op)

    def add_attention(self, *, type: str | None = None, type_: str | None = None, title: str, description: str) -> dict[str, Any]:
        """Add an attention item."""
        att_type = type or type_ or "blocked"
        def _op(conn: sqlite3.Connection, rev: int) -> dict[str, Any]:
            count = conn.execute("SELECT COUNT(*) FROM attention_items WHERE project_id = ?", (self.project_id,)).fetchone()[0]
            att_id = f"ATT-{count + 1:03d}"
            ts = _now()
            conn.execute(
                "INSERT INTO attention_items (attention_id, project_id, type, title, description, status, created_at) VALUES (?,?,?,?,?,?,?)",
                (att_id, self.project_id, att_type, title, description, "open", ts),
            )
            tx_id = f"tx-{uuid.uuid4().hex[:12]}"
            max_rev = conn.execute("SELECT COALESCE(MAX(revision), 0) FROM audit_events WHERE project_id = ?", (self.project_id,)).fetchone()[0]
            conn.execute(
                "INSERT INTO audit_events (event_id, project_id, revision, logical_tx_id, event_type, human_summary, timestamp) VALUES (?,?,?,?,?,?,?)",
                (f"{self.project_id}:e{max_rev + 1:06d}", self.project_id, max_rev + 1, tx_id, "ATTENTION_ADDED", f"Attention: {title}", ts),
            )
            return {"attention_id": att_id, "status": "open"}
        return self.write_transaction(_op)

    def resolve_attention(self, attention_id: str) -> dict[str, Any]:
        """Resolve attention item."""
        def _op(conn: sqlite3.Connection, rev: int) -> dict[str, Any]:
            row = conn.execute(
                "SELECT title FROM attention_items WHERE attention_id = ? AND project_id = ?",
                (attention_id, self.project_id),
            ).fetchone()
            if row is None:
                _fail("attention_not_found", f"attention item {attention_id} does not exist")
            conn.execute(
                "UPDATE attention_items SET status = 'resolved' WHERE attention_id = ? AND project_id = ?",
                (attention_id, self.project_id),
            )
            ts = _now()
            tx_id = f"tx-{uuid.uuid4().hex[:12]}"
            max_rev = conn.execute("SELECT COALESCE(MAX(revision), 0) FROM audit_events WHERE project_id = ?", (self.project_id,)).fetchone()[0]
            conn.execute(
                "INSERT INTO audit_events (event_id, project_id, revision, logical_tx_id, event_type, human_summary, timestamp) VALUES (?,?,?,?,?,?,?)",
                (f"{self.project_id}:e{max_rev + 1:06d}", self.project_id, max_rev + 1, tx_id, "ATTENTION_RESOLVED", f"Attention {attention_id} resolved", ts),
            )
            return {"attention_id": attention_id, "status": "resolved"}
        return self.write_transaction(_op)

    def create_checkpoint(
        self,
        *,
        label: str,
        plan_version: int | str | None = None,
        git_head: str | None = None,
        completed_task_ids: Sequence[str] = (),
        current_task_id: str | None = None,
        active_decision_ids: Sequence[str] = (),
        open_blocker_ids: Sequence[str] = (),
        human_summary: str | None = None,
    ) -> dict[str, Any]:
        """Create an immutable checkpoint."""
        def _op(conn: sqlite3.Connection, rev: int) -> dict[str, Any]:
            checkpoint_id = f"cp-{uuid.uuid4().hex[:8]}"
            ts = _now()
            pv = int(plan_version) if plan_version is not None else None
            conn.execute(
                "INSERT INTO checkpoints (checkpoint_id, project_id, label, plan_version, git_head, completed_task_ids_json, current_task_id, active_decision_ids_json, open_blocker_ids_json, human_summary, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (checkpoint_id, self.project_id, label, pv, git_head, json.dumps(list(completed_task_ids)), current_task_id, json.dumps(list(active_decision_ids)), json.dumps(list(open_blocker_ids)), human_summary, ts),
            )
            tx_id = f"tx-{uuid.uuid4().hex[:12]}"
            max_rev = conn.execute("SELECT COALESCE(MAX(revision), 0) FROM audit_events WHERE project_id = ?", (self.project_id,)).fetchone()[0]
            conn.execute(
                "INSERT INTO audit_events (event_id, project_id, revision, logical_tx_id, event_type, human_summary, timestamp) VALUES (?,?,?,?,?,?,?)",
                (f"{self.project_id}:e{max_rev + 1:06d}", self.project_id, max_rev + 1, tx_id, "CHECKPOINT_CREATED", f"Checkpoint: {label}", ts),
            )
            return {"checkpoint_id": checkpoint_id, "label": label}
        return self.write_transaction(_op)

    def ensure_initial_plan(self, plan: Any) -> dict[str, Any]:
        """Import initial project plan (v1)."""
        def _op(conn: sqlite3.Connection, rev: int) -> dict[str, Any]:
            plan_dict = plan.to_dict() if hasattr(plan, "to_dict") else dict(plan)
            version = int(plan_dict.get("plan_version", 1))
            if version != 1:
                _fail("plan_initial_version_invalid", "initial plan must be v1")
            existing = conn.execute(
                "SELECT plan_version FROM project_plans WHERE project_id = ? AND plan_version = 1",
                (self.project_id,),
            ).fetchone()
            if existing:
                _fail("plan_already_exists", "project already has a plan v1")
            plan_json = json.dumps(plan_dict, sort_keys=True)
            plan_digest = f"sha256:{hashlib.sha256(plan_json.encode('utf-8')).hexdigest()}"
            schema = plan_dict.get("schema", "bdb-project-plan-v1")
            ts = _now()
            conn.execute(
                "INSERT INTO project_plans (project_id, plan_version, plan_digest, schema, plan_json, imported_at) VALUES (?,?,?,?,?,?)",
                (self.project_id, 1, plan_digest, schema, plan_json, ts),
            )
            tx_id = f"tx-{uuid.uuid4().hex[:12]}"
            max_rev = conn.execute("SELECT COALESCE(MAX(revision), 0) FROM audit_events WHERE project_id = ?", (self.project_id,)).fetchone()[0]
            conn.execute(
                "INSERT INTO audit_events (event_id, project_id, revision, logical_tx_id, event_type, human_summary, plan_version, timestamp) VALUES (?,?,?,?,?,?,?,?)",
                (f"{self.project_id}:e{max_rev + 1:06d}", self.project_id, max_rev + 1, tx_id, "PLAN_IMPORTED", "Zaimportowano plan v1", 1, ts),
            )
            return {"project_id": self.project_id, "plan_version": 1, "plan_digest": plan_digest}
        return self.write_transaction(_op)

    def apply_update(self, candidate_plan: Any, preview: Any = None) -> dict[str, Any]:
        """Apply plan update (v2, v3, etc.)."""
        def _op(conn: sqlite3.Connection, rev: int) -> dict[str, Any]:
            plan_dict = candidate_plan.to_dict() if hasattr(candidate_plan, "to_dict") else dict(candidate_plan)
            version = int(plan_dict.get("plan_version", 2))
            plan_json = json.dumps(plan_dict, sort_keys=True)
            plan_digest = f"sha256:{hashlib.sha256(plan_json.encode('utf-8')).hexdigest()}"
            schema = plan_dict.get("schema", "bdb-project-plan-v1")
            ts = _now()
            conn.execute(
                "INSERT INTO project_plans (project_id, plan_version, plan_digest, schema, plan_json, imported_at) VALUES (?,?,?,?,?,?)",
                (self.project_id, version, plan_digest, schema, plan_json, ts),
            )
            tx_id = f"tx-{uuid.uuid4().hex[:12]}"
            max_rev = conn.execute("SELECT COALESCE(MAX(revision), 0) FROM audit_events WHERE project_id = ?", (self.project_id,)).fetchone()[0]
            conn.execute(
                "INSERT INTO audit_events (event_id, project_id, revision, logical_tx_id, event_type, human_summary, plan_version, timestamp) VALUES (?,?,?,?,?,?,?,?)",
                (f"{self.project_id}:e{max_rev + 1:06d}", self.project_id, max_rev + 1, tx_id, "PLAN_UPDATED", f"Zaktualizowano plan do v{version}", version, ts),
            )
            return {"project_id": self.project_id, "plan_version": version, "plan_digest": plan_digest}
        return self.write_transaction(_op)

    def read_events(self) -> list[dict[str, Any]]:
        """Read all audit events ordered by revision."""
        self._ensure_initialized()
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT event_id, project_id, revision, event_type, human_summary, task_id, milestone_id, plan_version, git_head, correlation_id, timestamp FROM audit_events WHERE project_id = ? ORDER BY revision",
                (self.project_id,),
            ).fetchall()
            return [
                {
                    "event_id": r[0], "project_id": r[1], "revision": r[2],
                    "event_type": r[3], "human_summary": r[4], "task_id": r[5],
                    "milestone_id": r[6], "plan_version": r[7], "git_head": r[8],
                    "correlation_id": r[9], "timestamp": r[10],
                }
                for r in rows
            ]
        finally:
            conn.close()

    def read_decisions(self) -> list[dict[str, Any]]:
        """Read all decisions."""
        self._ensure_initialized()
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT decision_id, title, decision, reason, status, created_at FROM decisions WHERE project_id = ? ORDER BY decision_id",
                (self.project_id,),
            ).fetchall()
            return [{"decision_id": r[0], "title": r[1], "decision": r[2], "reason": r[3], "status": r[4], "created_at": r[5]} for r in rows]
        finally:
            conn.close()

    def read_state(self) -> dict[str, Any]:
        """Read full project memory state as dictionary."""
        self._ensure_initialized()
        conn = self._connect()
        try:
            proj = conn.execute(
                "SELECT project_id, revision FROM projects WHERE project_id = ?", (self.project_id,)
            ).fetchone()
            rev = proj[1] if proj else 1
            events = self.read_events()
            decisions = self.read_decisions()
            inbox = [
                {"inbox_id": r[0], "title": r[1], "description": r[2], "status": r[3], "created_at": r[4]}
                for r in conn.execute("SELECT inbox_id, title, description, status, created_at FROM inbox_items WHERE project_id = ?", (self.project_id,)).fetchall()
            ]
            risks = [
                {"risk_id": r[0], "title": r[1], "description": r[2], "severity": r[3], "status": r[4], "created_at": r[5]}
                for r in conn.execute("SELECT risk_id, title, description, severity, status, created_at FROM risks WHERE project_id = ?", (self.project_id,)).fetchall()
            ]
            debt = [
                {"debt_id": r[0], "title": r[1], "description": r[2], "status": r[3], "created_at": r[4]}
                for r in conn.execute("SELECT debt_id, title, description, status, created_at FROM technical_debt WHERE project_id = ?", (self.project_id,)).fetchall()
            ]
            attention = [
                {"attention_id": r[0], "type": r[1], "title": r[2], "description": r[3], "status": r[4], "created_at": r[5]}
                for r in conn.execute("SELECT attention_id, type, title, description, status, created_at FROM attention_items WHERE project_id = ?", (self.project_id,)).fetchall()
            ]
            checkpoints = [
                {"checkpoint_id": r[0], "label": r[1], "plan_version": r[2], "created_at": r[3]}
                for r in conn.execute("SELECT checkpoint_id, label, plan_version, created_at FROM checkpoints WHERE project_id = ?", (self.project_id,)).fetchall()
            ]
            return {
                "project_id": self.project_id,
                "revision": rev,
                "events": events,
                "decisions": decisions,
                "inbox": inbox,
                "risks": risks,
                "technical_debt": debt,
                "attention": attention,
                "checkpoints": checkpoints,
            }
        finally:
            conn.close()

    def semantic_digest(self) -> str:
        """Compute a deterministic digest of the store's semantic state."""
        self._ensure_initialized()
        conn = self._connect()
        try:
            h = hashlib.sha256()
            # Project
            proj = conn.execute("SELECT project_id, revision FROM projects WHERE project_id = ?", (self.project_id,)).fetchone()
            if proj:
                h.update(f"project:{proj[0]}:rev:{proj[1]}".encode())
            # Events
            events = conn.execute("SELECT event_id, event_type, human_summary FROM audit_events WHERE project_id = ? ORDER BY revision", (self.project_id,)).fetchall()
            for e in events:
                h.update(f"event:{e[0]}:{e[1]}:{e[2]}".encode())
            # Decisions
            decisions = conn.execute("SELECT decision_id, title, status FROM decisions WHERE project_id = ? ORDER BY decision_id", (self.project_id,)).fetchall()
            for d in decisions:
                h.update(f"decision:{d[0]}:{d[1]}:{d[2]}".encode())
            # Inbox
            inbox = conn.execute("SELECT inbox_id, title, status FROM inbox_items WHERE project_id = ? ORDER BY inbox_id", (self.project_id,)).fetchall()
            for i in inbox:
                h.update(f"inbox:{i[0]}:{i[1]}:{i[2]}".encode())
            # Risks
            risks = conn.execute("SELECT risk_id, title, status FROM risks WHERE project_id = ? ORDER BY risk_id", (self.project_id,)).fetchall()
            for r in risks:
                h.update(f"risk:{r[0]}:{r[1]}:{r[2]}".encode())
            # Debt
            debt = conn.execute("SELECT debt_id, title, status FROM technical_debt WHERE project_id = ? ORDER BY debt_id", (self.project_id,)).fetchall()
            for d in debt:
                h.update(f"debt:{d[0]}:{d[1]}:{d[2]}".encode())
            # Attention
            att = conn.execute("SELECT attention_id, title, status FROM attention_items WHERE project_id = ? ORDER BY attention_id", (self.project_id,)).fetchall()
            for a in att:
                h.update(f"attention:{a[0]}:{a[1]}:{a[2]}".encode())
            # Checkpoints
            cps = conn.execute("SELECT checkpoint_id, label FROM checkpoints WHERE project_id = ? ORDER BY checkpoint_id", (self.project_id,)).fetchall()
            for c in cps:
                h.update(f"checkpoint:{c[0]}:{c[1]}".encode())
            # Plans
            plans = conn.execute("SELECT plan_version, plan_digest FROM project_plans WHERE project_id = ? ORDER BY plan_version", (self.project_id,)).fetchall()
            for p in plans:
                h.update(f"plan:{p[0]}:{p[1]}".encode())
            return f"sha256:{h.hexdigest()}"
        finally:
            conn.close()

    def integrity_check(self) -> tuple[bool, str]:
        """Run SQLite integrity_check."""
        self._ensure_initialized()
        conn = self._connect()
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()
            ok = result[0] == "ok" if result else False
            return (ok, result[0] if result else "unknown")
        finally:
            conn.close()

    def foreign_key_check(self) -> tuple[bool, list[Any]]:
        """Run SQLite foreign_key_check."""
        self._ensure_initialized()
        conn = self._connect()
        try:
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            return (len(violations) == 0, violations)
        finally:
            conn.close()

    def backup(self, dest_path: Path) -> str:
        """Create an online backup. Returns digest of backup file."""
        self._ensure_initialized()
        source = self._connect()
        dest = sqlite3.connect(str(dest_path))
        try:
            source.backup(dest)
            dest.close()
            source.close()
            digest = hashlib.sha256(dest_path.read_bytes()).hexdigest()
            return f"sha256:{digest}"
        except Exception:
            dest.close()
            source.close()
            raise

    def schema_version(self) -> str | None:
        """Read current schema version from migrations table."""
        self._ensure_initialized()
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT version FROM schema_migrations ORDER BY applied_at DESC LIMIT 1"
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    @staticmethod
    def restore(backup_path: Path, dest_path: Path) -> str:
        """Restore a backup to destination. Returns digest of restored DB."""
        shutil.copy2(str(backup_path), str(dest_path))
        digest = hashlib.sha256(dest_path.read_bytes()).hexdigest()
        return f"sha256:{digest}"

    def get_scope_orchestrator(self) -> Any:
        """Returns a ScopeOrchestrator bound directly to this canonical ProjectMemoryStoreV2 authority."""
        from .scope_orchestrator import ScopeOrchestrator
        self._ensure_initialized()
        return ScopeOrchestrator(self._connect(), self.project_id)

    def request_stop(
        self,
        *,
        expected_epoch: int | None = None,
        reason: str = "External STOP requested",
        actor_class: str = "operator",
    ) -> tuple[Any, bool, int, int]:
        """Requests canonical STOP under Project Memory v2 authority."""
        from .stop_fence import execute_stop_transaction
        self._ensure_initialized()
        with self._transaction() as conn:
            return execute_stop_transaction(
                conn,
                self.project_id,
                expected_epoch=expected_epoch,
                reason=reason,
                actor_class=actor_class,
            )

    def resume_scope(
        self,
        *,
        expected_prior_epoch: int | None = None,
        new_run_id: str | None = None,
        actor_class: str = "operator",
    ) -> Any:
        """Resumes scope under Project Memory v2 authority into a new epoch."""
        from .stop_fence import execute_resume_transaction
        self._ensure_initialized()
        with self._transaction() as conn:
            return execute_resume_transaction(
                conn,
                self.project_id,
                expected_prior_epoch=expected_prior_epoch,
                new_run_id=new_run_id,
                actor_class=actor_class,
            )

    def get_stop_fence(self, epoch: int) -> Any | None:
        """Reads a stop fence record from Project Memory v2 if exists."""
        self._ensure_initialized()
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM stop_fences WHERE project_id = ? AND scope_epoch = ?",
                (self.project_id, epoch),
            ).fetchone()
            if not row:
                return None
            from .stop_fence import StopFenceRecord
            return StopFenceRecord(
                fence_id=row["fence_id"],
                project_id=row["project_id"],
                run_id=row["run_id"],
                scope=row["scope"],
                scope_epoch=row["scope_epoch"],
                cursor_id=row["cursor_id"],
                stop_requested_at=row["stop_requested_at"],
                stop_reason=row["stop_reason"],
                actor_class=row["actor_class"],
                prior_disposition=row["prior_disposition"],
                source_state_revision=row["source_state_revision"],
                committed_revision=row["committed_revision"],
                cancelled_work_ids=tuple(json.loads(row["cancelled_work_ids_json"])),
                created_at=row["created_at"],
            )
        finally:
            conn.close()

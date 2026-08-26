"""NX-041 — Authenticated Local Worker and IPC.

Provides repo-local authenticated IPC, durable request claim/lease/cancel,
single-flight execution ownership, and crash/restart recovery.

The worker is MECHANICS ONLY:
- Does not select tasks or decide workflow PASS/FAIL
- Does not advance milestones or promote candidates
- Does not grant Native Host workflow authority
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .local_execution_contract import (
    ExecutionEffectClass,
    IdempotencyClass,
    LocalExecutionContractError,
    LocalExecutionRequest,
    LocalExecutionResult,
    MechanicalExecutionStatus,
)


# ==============================================================================
# Protocol Version & Constants
# ==============================================================================

LOCAL_WORKER_PROTOCOL_SCHEMA = "bdb-vnext-local-worker-protocol-v1"
LOCAL_WORKER_PROTOCOL_VERSION = "1.0.0"
LOCAL_WORKER_PROTOCOL_VERSION_EXPLICIT = True

WORKER_CAN_MARK_TASK_PASS = False
NATIVE_HOST_BECOMES_WORKFLOW_AUTHORITY = False
MAX_SIMULTANEOUS_EXECUTION_OWNERS_PER_EXECUTION_ID = 1
PROJECT_SIMULTANEOUS_LOCAL_EFFECTS_MAX = 1


# ==============================================================================
# Enums
# ==============================================================================

class _StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class WorkerMessageType(_StringEnum):
    SUBMIT = "SUBMIT"
    CLAIM = "CLAIM"
    HEARTBEAT = "HEARTBEAT"
    CANCEL = "CANCEL"
    STATUS = "STATUS"
    RESULT = "RESULT"
    ACK = "ACK"


class ExecutionQueueState(_StringEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    COMPLETED = "COMPLETED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


# ==============================================================================
# Authentication & IPC Message Contracts
# ==============================================================================

class WorkerAuthError(PermissionError):
    """Authentication or authorization failure in local worker IPC."""


@dataclass(frozen=True)
class RuntimeAuthContext:
    """Canonical runtime authentication secret/context."""

    runtime_id: str
    auth_token: str
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def validate(self, provided_token: str) -> bool:
        return bool(self.auth_token and provided_token == self.auth_token)


@dataclass(frozen=True)
class WorkerMessage:
    """Versioned IPC message for local worker coordination."""

    msg_id: str
    msg_type: WorkerMessageType
    execution_id: str
    request_digest: str
    runtime_id: str
    auth_token: str
    protocol_version: str = LOCAL_WORKER_PROTOCOL_VERSION
    owner_token: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def validate_protocol(self) -> None:
        if self.protocol_version != LOCAL_WORKER_PROTOCOL_VERSION:
            raise LocalExecutionContractError(
                "protocol_version_mismatch",
                f"Expected protocol version {LOCAL_WORKER_PROTOCOL_VERSION}, got {self.protocol_version}",
            )


# ==============================================================================
# Durable Outbox & Queue Store
# ==============================================================================

_QUEUE_DDL = """
CREATE TABLE IF NOT EXISTS execution_outbox (
    execution_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    request_json TEXT NOT NULL,
    state TEXT NOT NULL,
    owner_token TEXT,
    lease_expires_at REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    result_json TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    effect_class TEXT NOT NULL,
    idempotency TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outbox_project_state ON execution_outbox (project_id, state);
"""


@dataclass
class QueueRecord:
    execution_id: str
    project_id: str
    request_digest: str
    request: LocalExecutionRequest
    state: ExecutionQueueState
    owner_token: str | None
    lease_expires_at: float
    created_at: float
    updated_at: float
    result: LocalExecutionResult | None = None
    cancel_requested: bool = False
    effect_class: ExecutionEffectClass = ExecutionEffectClass.READ_ONLY
    idempotency: IdempotencyClass = IdempotencyClass.IDEMPOTENT_REPLAYABLE


class DurableExecutionOutbox:
    """Transactional, SQLite-backed durable outbox for local execution lifecycle."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = str(db_path)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.executescript(_QUEUE_DDL)

    def submit_request(self, request: LocalExecutionRequest) -> tuple[bool, QueueRecord]:
        """Submit a request. Exact duplicate returns existing logical execution; conflicting digest fails closed."""
        now = time.time()
        req_json = json.dumps(request.to_dict(), sort_keys=True)

        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM execution_outbox WHERE execution_id = ?", (request.execution_id,))
            row = cur.fetchone()
            if row is not None:
                existing_digest = row["request_digest"]
                if existing_digest != request.request_digest:
                    raise LocalExecutionContractError(
                        "conflicting_duplicate_request",
                        f"execution_id '{request.execution_id}' already exists with digest '{existing_digest}' (requested '{request.request_digest}')",
                    )
                return False, self._row_to_record(row)

            # Single flight check per project for active mutable effects
            if request.effect_class is not ExecutionEffectClass.READ_ONLY:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM execution_outbox 
                    WHERE project_id = ? 
                      AND state IN ('CLAIMED', 'RUNNING')
                      AND effect_class != 'READ_ONLY'
                      AND lease_expires_at > ?
                    """,
                    (request.project_id, now),
                )
                active_count = cur.fetchone()[0]
                if active_count >= PROJECT_SIMULTANEOUS_LOCAL_EFFECTS_MAX:
                    raise LocalExecutionContractError(
                        "single_flight_violation",
                        f"Project '{request.project_id}' already has {active_count} active execution(s)",
                    )

            cur.execute(
                """
                INSERT INTO execution_outbox (
                    execution_id, project_id, request_digest, request_json, state,
                    owner_token, lease_expires_at, created_at, updated_at,
                    result_json, cancel_requested, effect_class, idempotency
                ) VALUES (?, ?, ?, ?, ?, NULL, 0, ?, ?, NULL, 0, ?, ?)
                """,
                (
                    request.execution_id,
                    request.project_id,
                    request.request_digest,
                    req_json,
                    ExecutionQueueState.PENDING.value,
                    now,
                    now,
                    request.effect_class.value,
                    request.idempotency.value,
                ),
            )
            cur.execute("SELECT * FROM execution_outbox WHERE execution_id = ?", (request.execution_id,))
            row = cur.fetchone()
            return True, self._row_to_record(row)

    def claim_lease(
        self,
        execution_id: str,
        owner_token: str,
        lease_duration_seconds: float = 30.0,
    ) -> bool:
        """Atomic CAS claim: only one worker can acquire execution rights per execution_id."""
        now = time.time()
        expiry = now + lease_duration_seconds

        with self._get_connection() as conn:
            cur = conn.cursor()
            # Can claim if PENDING or if previous lease has fully expired
            cur.execute(
                """
                UPDATE execution_outbox
                SET state = ?, owner_token = ?, lease_expires_at = ?, updated_at = ?
                WHERE execution_id = ?
                  AND (state = ? OR (state = ? AND lease_expires_at < ?))
                """,
                (
                    ExecutionQueueState.CLAIMED.value,
                    owner_token,
                    expiry,
                    now,
                    execution_id,
                    ExecutionQueueState.PENDING.value,
                    ExecutionQueueState.CLAIMED.value,
                    now,
                ),
            )
            return cur.rowcount == 1

    def renew_lease(
        self,
        execution_id: str,
        owner_token: str,
        lease_duration_seconds: float = 30.0,
    ) -> bool:
        """Renew active lease; foreign owner tokens are rejected."""
        now = time.time()
        expiry = now + lease_duration_seconds

        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE execution_outbox
                SET lease_expires_at = ?, updated_at = ?
                WHERE execution_id = ? AND owner_token = ? AND state IN (?, ?)
                """,
                (
                    expiry,
                    now,
                    execution_id,
                    owner_token,
                    ExecutionQueueState.CLAIMED.value,
                    ExecutionQueueState.RUNNING.value,
                ),
            )
            return cur.rowcount == 1

    def mark_running(self, execution_id: str, owner_token: str) -> bool:
        now = time.time()
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE execution_outbox
                SET state = ?, updated_at = ?
                WHERE execution_id = ? AND owner_token = ? AND state = ?
                """,
                (
                    ExecutionQueueState.RUNNING.value,
                    now,
                    execution_id,
                    owner_token,
                    ExecutionQueueState.CLAIMED.value,
                ),
            )
            return cur.rowcount == 1

    def request_cancel(self, execution_id: str) -> bool:
        now = time.time()
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE execution_outbox
                SET cancel_requested = 1, updated_at = ?
                WHERE execution_id = ? AND state NOT IN (?)
                """,
                (
                    now,
                    execution_id,
                    ExecutionQueueState.COMPLETED.value,
                ),
            )
            return cur.rowcount == 1

    def record_result(
        self,
        execution_id: str,
        owner_token: str,
        result: LocalExecutionResult,
    ) -> bool:
        """Persist execution result atomically and mark COMPLETED."""
        now = time.time()
        res_json = json.dumps(result.to_dict(), sort_keys=True)

        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE execution_outbox
                SET state = ?, result_json = ?, updated_at = ?
                WHERE execution_id = ? AND owner_token = ? AND state IN (?, ?, ?)
                """,
                (
                    ExecutionQueueState.COMPLETED.value,
                    res_json,
                    now,
                    execution_id,
                    owner_token,
                    ExecutionQueueState.CLAIMED.value,
                    ExecutionQueueState.RUNNING.value,
                    ExecutionQueueState.CANCEL_REQUESTED.value,
                ),
            )
            return cur.rowcount == 1

    def mark_reconciliation_required(self, execution_id: str) -> bool:
        now = time.time()
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE execution_outbox
                SET state = ?, updated_at = ?
                WHERE execution_id = ? AND state != ?
                """,
                (
                    ExecutionQueueState.RECONCILIATION_REQUIRED.value,
                    now,
                    execution_id,
                    ExecutionQueueState.COMPLETED.value,
                ),
            )
            return cur.rowcount == 1

    def get_record(self, execution_id: str) -> QueueRecord | None:
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM execution_outbox WHERE execution_id = ?", (execution_id,))
            row = cur.fetchone()
            return self._row_to_record(row) if row else None

    def _row_to_record(self, row: sqlite3.Row) -> QueueRecord:
        req = LocalExecutionRequest.from_dict(json.loads(row["request_json"]))
        res = LocalExecutionResult.from_dict(json.loads(row["result_json"])) if row["result_json"] else None
        return QueueRecord(
            execution_id=row["execution_id"],
            project_id=row["project_id"],
            request_digest=row["request_digest"],
            request=req,
            state=ExecutionQueueState(row["state"]),
            owner_token=row["owner_token"],
            lease_expires_at=row["lease_expires_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            result=res,
            cancel_requested=bool(row["cancel_requested"]),
            effect_class=ExecutionEffectClass(row["effect_class"]),
            idempotency=IdempotencyClass(row["idempotency"]),
        )


# ==============================================================================
# Execution Backend Interface (Injected / Fake support)
# ==============================================================================

class AbstractExecutionBackend:
    """Pluggable backend for mechanical execution (allows deterministic test fakes)."""

    def execute(
        self,
        request: LocalExecutionRequest,
        *,
        is_cancelled: Callable[[], bool],
    ) -> LocalExecutionResult:
        raise NotImplementedError


class SimulatedExecutionBackend(AbstractExecutionBackend):
    """Simulated execution backend with fault injection and effect tracking."""

    def __init__(
        self,
        exit_code: int = 0,
        stdout_bytes: bytes = b"",
        stderr_bytes: bytes = b"",
        delay_seconds: float = 0.0,
        crash_before_effect: bool = False,
        crash_after_effect: bool = False,
    ) -> None:
        self.exit_code = exit_code
        self.stdout_bytes = stdout_bytes
        self.stderr_bytes = stderr_bytes
        self.delay_seconds = delay_seconds
        self.crash_before_effect = crash_before_effect
        self.crash_after_effect = crash_after_effect
        self.execution_attempts = 0
        self.effects_executed = 0

    def execute(
        self,
        request: LocalExecutionRequest,
        *,
        is_cancelled: Callable[[], bool],
    ) -> LocalExecutionResult:
        self.execution_attempts += 1

        if self.crash_before_effect:
            raise RuntimeError("Crash simulated before execution effect")

        if is_cancelled():
            from .local_execution_contract import ExecutionOutputEvidence
            return LocalExecutionResult(
                execution_id=request.execution_id,
                request_digest=request.request_digest,
                started_at=datetime.now(timezone.utc).isoformat(),
                completed_at=datetime.now(timezone.utc).isoformat(),
                duration_ms=10,
                exit_code=130,
                stdout=ExecutionOutputEvidence.from_bytes("stdout", b""),
                stderr=ExecutionOutputEvidence.from_bytes("stderr", b"Execution cancelled"),
                observed_source_head=request.expected_source_head,
                observed_source_tree=request.expected_source_tree,
                adapter_id=request.adapter_id,
                status=MechanicalExecutionStatus.CANCELLED,
                cancelled=True,
                cancel_reason="USER_CANCELLED",
            )

        if self.delay_seconds > 0:
            time.sleep(self.delay_seconds)

        self.effects_executed += 1

        if self.crash_after_effect:
            raise RuntimeError("Crash simulated after execution effect")

        from .local_execution_contract import ExecutionOutputEvidence
        return LocalExecutionResult(
            execution_id=request.execution_id,
            request_digest=request.request_digest,
            started_at=datetime.now(timezone.utc).isoformat(),
            completed_at=datetime.now(timezone.utc).isoformat(),
            duration_ms=int(self.delay_seconds * 1000) + 10,
            exit_code=self.exit_code,
            stdout=ExecutionOutputEvidence.from_bytes("stdout", self.stdout_bytes),
            stderr=ExecutionOutputEvidence.from_bytes("stderr", self.stderr_bytes),
            observed_source_head=request.expected_source_head,
            observed_source_tree=request.expected_source_tree,
            adapter_id=request.adapter_id,
            status=MechanicalExecutionStatus.COMPLETED,
        )


# ==============================================================================
# Authenticated Local Worker
# ==============================================================================

class LocalExecutionWorker:
    """Authenticated local execution coordinator handling lifecycle, lease, and recovery."""

    def __init__(
        self,
        worker_id: str,
        auth_context: RuntimeAuthContext,
        outbox: DurableExecutionOutbox,
        backend: AbstractExecutionBackend | None = None,
    ) -> None:
        self.worker_id = worker_id
        self.auth_context = auth_context
        self.outbox = outbox
        self.backend = backend or SimulatedExecutionBackend()
        self.owner_token = f"owner:{worker_id}:{uuid.uuid4().hex[:12]}"
        self.observed_trace: list[str] = []

    def _record_trace(self, action: str) -> None:
        self.observed_trace.append(action)

    def verify_auth(self, message: WorkerMessage) -> None:
        message.validate_protocol()
        if not self.auth_context.validate(message.auth_token):
            raise WorkerAuthError(f"Unauthenticated request from runtime '{message.runtime_id}'")

    def submit(self, message: WorkerMessage, request: LocalExecutionRequest) -> QueueRecord:
        """Submit a request into the durable outbox."""
        self.verify_auth(message)
        self._record_trace("SUBMIT")
        created, record = self.outbox.submit_request(request)
        if created:
            self._record_trace("PERSIST")
        return record

    def claim(self, message: WorkerMessage, lease_seconds: float = 30.0) -> bool:
        """Acquire lease on an execution."""
        self.verify_auth(message)
        claimed = self.outbox.claim_lease(
            message.execution_id,
            self.owner_token,
            lease_duration_seconds=lease_seconds,
        )
        if claimed:
            self._record_trace("CLAIM")
        return claimed

    def cancel(self, message: WorkerMessage, reason: str = "CANCEL_REQUESTED") -> bool:
        """Request cancellation of an execution."""
        self.verify_auth(message)
        self._record_trace("CANCEL")
        return self.outbox.request_cancel(message.execution_id)

    def execute_claimed(self, execution_id: str) -> LocalExecutionResult:
        """Execute a claimed request through the backend, handling cancellation and persistence."""
        record = self.outbox.get_record(execution_id)
        if record is None or record.owner_token != self.owner_token:
            raise LocalExecutionContractError("invalid_owner_token", "Cannot execute request without valid claim owner token")

        self.outbox.mark_running(execution_id, self.owner_token)
        self._record_trace("AUTH_VALIDATION")
        self._record_trace("DISPATCH")

        def check_cancelled() -> bool:
            rec = self.outbox.get_record(execution_id)
            return rec is not None and rec.cancel_requested

        try:
            result = self.backend.execute(record.request, is_cancelled=check_cancelled)
        except Exception as e:
            # Handle crash during execution
            if record.idempotency is not IdempotencyClass.IDEMPOTENT_REPLAYABLE:
                self.outbox.mark_reconciliation_required(execution_id)
            raise e

        # Persist mechanical result
        self.outbox.record_result(execution_id, self.owner_token, result)
        self._record_trace("RESULT_PERSIST")
        self._record_trace("COMPLETE_ACK")
        return result

    def handle_crash_recovery(self, execution_id: str) -> str:
        """Recover an execution after worker crash according to its idempotency classification.

        - IDEMPOTENT_REPLAYABLE: Lease expires -> can be safely re-claimed and re-run.
        - RECONCILE_ONLY / NON_REPLAYABLE: Outcome is uncertain -> marks RECONCILIATION_REQUIRED.
        """
        record = self.outbox.get_record(execution_id)
        if record is None:
            return "NOT_FOUND"

        if record.state is ExecutionQueueState.COMPLETED:
            return "ALREADY_COMPLETED"

        if record.state is ExecutionQueueState.RECONCILIATION_REQUIRED:
            return "RECONCILIATION_REQUIRED"

        if record.state is ExecutionQueueState.RUNNING:
            if record.idempotency is not IdempotencyClass.IDEMPOTENT_REPLAYABLE:
                self.outbox.mark_reconciliation_required(execution_id)
                return "RECONCILIATION_REQUIRED"

        return "RECLAIMABLE"

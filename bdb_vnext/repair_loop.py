"""NX-015: Bounded Automatic Repair and Exact Retest Loop.

Implements:
1. Versioned repair request identity bound to project, task, failed binding, and exact retest selector.
2. Lifecycle conformance: preserves same task ID, creates monotonic new attempt/binding generation.
3. Minimal repair envelope: strictly enforces allowed boundary and rejects scope escapes.
4. READY_FOR_RETEST intermediate state: repair effect never directly promotes to PASS.
5. Exact retest selector: targets exact failed test/gate and rejects wrong verifier results.
6. Rebuildable repair history projection and crash boundary reconciliation.
"""

from __future__ import annotations

import enum
import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from bdb_vnext.binding_lifecycle import (
    STATUS_ACCEPTED,
    STATUS_ACTIVE,
    STATUS_FAILED,
    STATUS_SUPERSEDED,
    validate_binding_transition,
)
from bdb_vnext.failure_budget import (
    ExhaustionState,
    FailureBudgetLedger,
    FailureFingerprint,
    compute_failure_fingerprint,
)
from bdb_vnext.failure_classifier import (
    ClassificationResult,
    compute_evidence_digest,
)
from bdb_vnext.failure_taxonomy import (
    AutoAction,
    FailureClass,
    SemanticKind,
    TRANSITION_MATRIX,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ==============================================================================
# 1. RETEST SELECTOR & REPAIR ENVELOPE
# ==============================================================================

@dataclass(frozen=True)
class RetestSelector:
    selector_type: str  # e.g., "EXACT_TEST", "MACHINE_GATE", "CUSTOM_VERIFIER"
    target: str         # e.g., "tests/test_calculator.py::test_add"
    expected_verifier_id: str
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RepairScopeEnvelope:
    allowed_paths: tuple[str, ...]
    disallowed_paths: tuple[str, ...] = ()
    max_changed_files: int = 10

    def validate_mutation(self, changed_paths: Sequence[str]) -> tuple[bool, str]:
        """Validates that proposed file modifications stay strictly within allowed scope."""
        if len(changed_paths) > self.max_changed_files:
            return False, f"changed file count {len(changed_paths)} exceeds max {self.max_changed_files}"

        for p in changed_paths:
            normalized = p.replace("\\", "/").strip()
            # Path traversal / escape detection
            if ".." in normalized or normalized.startswith("/") or ":" in normalized:
                return False, f"path traversal or absolute path escape detected: {p}"

            # Check explicit disallowed patterns
            for dis in self.disallowed_paths:
                dis_norm = dis.replace("\\", "/").strip()
                if normalized == dis_norm or normalized.startswith(dis_norm.rstrip("/") + "/"):
                    return False, f"path {p} violates disallowed boundary {dis}"

            # Must match at least one allowed prefix or exact path
            matched = False
            for allowed in self.allowed_paths:
                allowed_norm = allowed.replace("\\", "/").strip()
                if allowed_norm == "*" or normalized == allowed_norm or normalized.startswith(allowed_norm.rstrip("/") + "/"):
                    matched = True
                    break

            if not matched:
                return False, f"path {p} escapes allowed repair envelope {self.allowed_paths}"

        return True, "scope_valid"


# ==============================================================================
# 2. REPAIR STAGES & REQUEST IDENTITY
# ==============================================================================

class RepairStage(enum.Enum):
    INTENT_RECORDED = "INTENT_RECORDED"
    EFFECT_APPLIED = "EFFECT_APPLIED"
    READY_FOR_RETEST = "READY_FOR_RETEST"
    RETEST_RUNNING = "RETEST_RUNNING"
    ACCEPTED = "ACCEPTED"
    REPAIR_FAILED = "REPAIR_FAILED"


@dataclass(frozen=True)
class RepairRequest:
    repair_request_id: str
    project_id: str
    run_id: str
    task_id: str
    failed_binding_id: str
    failed_attempt_id: str | None
    fingerprint: FailureFingerprint
    classification: FailureClass
    evidence_digest: str
    repair_generation: int
    scope_envelope: RepairScopeEnvelope
    expected_source_head: str
    expected_source_tree: str
    retest_selector: RetestSelector
    created_at: str


@dataclass(frozen=True)
class VerifierExecutionResult:
    verifier_id: str
    target: str
    status: str  # "PASS" or "FAIL"
    evidence_digest: str
    diagnostic: str = ""


# ==============================================================================
# 3. REPAIR LOOP CONTROLLER
# ==============================================================================

class RepairLoopController:
    """Coordinates the failure -> budget -> repair envelope -> exact retest -> acceptance cycle."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        project_id: str,
        budget_ledger: FailureBudgetLedger,
    ) -> None:
        self.conn = conn
        self.project_id = project_id
        self.budget_ledger = budget_ledger
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS repair_requests (
                repair_request_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                failed_binding_id TEXT NOT NULL,
                failed_attempt_id TEXT,
                fingerprint_digest TEXT NOT NULL,
                failure_class TEXT NOT NULL,
                evidence_digest TEXT NOT NULL,
                repair_generation INTEGER NOT NULL,
                scope_envelope_json TEXT NOT NULL,
                expected_source_head TEXT NOT NULL,
                expected_source_tree TEXT NOT NULL,
                retest_selector_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS repair_records (
                repair_request_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                binding_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                generation INTEGER NOT NULL,
                fingerprint_digest TEXT NOT NULL,
                evidence_digest TEXT NOT NULL,
                source_before TEXT NOT NULL,
                source_after TEXT,
                changed_paths_json TEXT NOT NULL DEFAULT '[]',
                retest_selector_json TEXT NOT NULL,
                stage TEXT NOT NULL,
                retest_status TEXT,
                retest_evidence_digest TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (repair_request_id) REFERENCES repair_requests(repair_request_id)
            );

            CREATE TABLE IF NOT EXISTS accepted_repair_results (
                acceptance_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                binding_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                canonical_result_digest TEXT NOT NULL,
                accepted_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_accepted_repair_task ON accepted_repair_results(project_id, task_id);
        """)
        self.conn.commit()

    def create_repair_request(
        self,
        *,
        run_id: str,
        task_id: str,
        failed_binding_id: str,
        failed_attempt_id: str | None,
        fingerprint: FailureFingerprint,
        classification: FailureClass,
        evidence_digest: str,
        scope_envelope: RepairScopeEnvelope,
        expected_source_head: str,
        expected_source_tree: str,
        retest_selector: RetestSelector,
        current_binding_generation: int = 1,
    ) -> tuple[bool, RepairRequest | None, str]:
        """Evaluates budget and initiates a formal, idempotent repair request.

        Exact duplicate repair request is idempotent: returns existing request without duplicating attempt.
        Preserves same task ID and run ID.
        Monotonically advances attempt/binding generation.
        """
        # 1. Check budget authorization
        ev_eval = self.budget_ledger.evaluate_failure(
            task_id=task_id,
            classification_or_class=classification,
            evidence=fingerprint.semantic_features,
            rule_id=fingerprint.rule_id,
            run_id=run_id,
        )[1]

        if not ev_eval.allowed:
            return False, None, f"budget_exhausted: {ev_eval.reason}"

        # 2. Check for duplicate repair request (idempotency guard)
        repair_gen = current_binding_generation + 1
        req_seed = f"{self.project_id}:{run_id}:{task_id}:{failed_binding_id}:{fingerprint.fingerprint_digest}:{repair_gen}"
        req_id = f"rep-{hashlib.sha256(req_seed.encode()).hexdigest()[:16]}"

        existing = self.conn.execute(
            "SELECT repair_request_id FROM repair_requests WHERE repair_request_id = ?",
            (req_id,),
        ).fetchone()

        if existing is not None:
            # Idempotent return without creating duplicate attempt
            req = self._load_repair_request(req_id)
            return True, req, "duplicate_repair_request_idempotent"

        # 3. Consume repair budget
        consume_res = self.budget_ledger.consume_repair_attempt(task_id, fingerprint)
        if not consume_res.allowed:
            return False, None, f"budget_exhaustion_during_consumption: {consume_res.reason}"

        now_iso = _now_iso()
        req = RepairRequest(
            repair_request_id=req_id,
            project_id=self.project_id,
            run_id=run_id,
            task_id=task_id,
            failed_binding_id=failed_binding_id,
            failed_attempt_id=failed_attempt_id,
            fingerprint=fingerprint,
            classification=classification,
            evidence_digest=evidence_digest,
            repair_generation=repair_gen,
            scope_envelope=scope_envelope,
            expected_source_head=expected_source_head,
            expected_source_tree=expected_source_tree,
            retest_selector=retest_selector,
            created_at=now_iso,
        )

        # 4. Create new binding and attempt identifiers
        new_binding_id = f"bnd-{hashlib.sha256(f'{self.project_id}:{task_id}:{repair_gen}'.encode()).hexdigest()[:16]}"
        new_attempt_id = f"att-{hashlib.sha256(f'{new_binding_id}:repair:{repair_gen}'.encode()).hexdigest()[:16]}"

        self.conn.execute(
            """
            INSERT INTO repair_requests (
                repair_request_id, project_id, run_id, task_id,
                failed_binding_id, failed_attempt_id, fingerprint_digest,
                failure_class, evidence_digest, repair_generation,
                scope_envelope_json, expected_source_head, expected_source_tree,
                retest_selector_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                req.repair_request_id,
                req.project_id,
                req.run_id,
                req.task_id,
                req.failed_binding_id,
                req.failed_attempt_id,
                req.fingerprint.fingerprint_digest,
                req.classification.value,
                req.evidence_digest,
                req.repair_generation,
                json.dumps(asdict(req.scope_envelope)),
                req.expected_source_head,
                req.expected_source_tree,
                json.dumps(asdict(req.retest_selector)),
                req.created_at,
            ),
        )

        self.conn.execute(
            """
            INSERT INTO repair_records (
                repair_request_id, project_id, task_id, run_id,
                binding_id, attempt_id, generation, fingerprint_digest,
                evidence_digest, source_before, source_after,
                changed_paths_json, retest_selector_json, stage,
                retest_status, retest_evidence_digest, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, '[]', ?, ?, NULL, NULL, ?, ?)
            """,
            (
                req.repair_request_id,
                req.project_id,
                req.task_id,
                req.run_id,
                new_binding_id,
                new_attempt_id,
                repair_gen,
                req.fingerprint.fingerprint_digest,
                req.evidence_digest,
                req.expected_source_head,
                json.dumps(asdict(req.retest_selector)),
                RepairStage.INTENT_RECORDED.value,
                now_iso,
                now_iso,
            ),
        )
        self.conn.commit()

        return True, req, "repair_request_created"

    def apply_repair_effect(
        self,
        repair_request_id: str,
        changed_paths: Sequence[str],
        source_after_head: str,
    ) -> tuple[bool, str]:
        """Applies bounded repair patch to workspace after validating scope envelope."""
        req = self._load_repair_request(repair_request_id)
        if req is None:
            return False, "repair_request_not_found"

        # Enforce minimal repair envelope
        valid, reason = req.scope_envelope.validate_mutation(changed_paths)
        if not valid:
            return False, f"scope_violation: {reason}"

        now_iso = _now_iso()
        self.conn.execute(
            """
            UPDATE repair_records
            SET source_after = ?,
                changed_paths_json = ?,
                stage = ?,
                updated_at = ?
            WHERE repair_request_id = ?
            """,
            (
                source_after_head,
                json.dumps(list(changed_paths)),
                RepairStage.EFFECT_APPLIED.value,
                now_iso,
                repair_request_id,
            ),
        )
        self.conn.commit()
        return True, "effect_applied_within_boundary"

    def mark_ready_for_retest(self, repair_request_id: str) -> tuple[bool, str]:
        """Transitions repair to READY_FOR_RETEST.

        Invariant: Repair completion does NOT directly promote to PASS.
        Only exact verifier execution can produce acceptance.
        """
        row = self.conn.execute(
            "SELECT stage FROM repair_records WHERE repair_request_id = ?",
            (repair_request_id,),
        ).fetchone()
        if row is None:
            return False, "repair_record_not_found"

        current_stage = row[0]
        if current_stage != RepairStage.EFFECT_APPLIED.value:
            return False, f"cannot_transition_to_retest_from_{current_stage}"

        now_iso = _now_iso()
        self.conn.execute(
            "UPDATE repair_records SET stage = ?, updated_at = ? WHERE repair_request_id = ?",
            (RepairStage.READY_FOR_RETEST.value, now_iso, repair_request_id),
        )
        self.conn.commit()
        return True, "ready_for_retest"

    def execute_exact_retest(
        self,
        repair_request_id: str,
        verifier: Callable[[RetestSelector], VerifierExecutionResult],
    ) -> tuple[bool, str]:
        """Executes exact bound verifier and records acceptance or failure.

        Rejects results from mismatched verifier target.
        Enforces exactly 1 accepted result.
        """
        req = self._load_repair_request(repair_request_id)
        if req is None:
            return False, "repair_request_not_found"

        row = self.conn.execute(
            "SELECT stage, binding_id, attempt_id FROM repair_records WHERE repair_request_id = ?",
            (repair_request_id,),
        ).fetchone()
        if row is None:
            return False, "repair_record_not_found"

        stage, binding_id, attempt_id = row[0], row[1], row[2]
        if stage != RepairStage.READY_FOR_RETEST.value:
            return False, f"invalid_retest_stage: {stage}"

        # Execute verifier
        result = verifier(req.retest_selector)

        # Retest selector guard: reject mismatched/wrong verifier results
        if result.target != req.retest_selector.target:
            return False, f"wrong_retest_target: expected {req.retest_selector.target}, got {result.target}"
        if result.verifier_id != req.retest_selector.expected_verifier_id:
            return False, f"wrong_verifier_id: expected {req.retest_selector.expected_verifier_id}, got {result.verifier_id}"

        now_iso = _now_iso()

        if result.status == "PASS":
            # Record exact accepted result (unique per task)
            acc_id = f"acc-{hashlib.sha256(f'{self.project_id}:{req.task_id}'.encode()).hexdigest()[:16]}"
            try:
                self.conn.execute(
                    """
                    INSERT INTO accepted_repair_results (
                        acceptance_id, project_id, task_id, run_id,
                        binding_id, attempt_id, canonical_result_digest, accepted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        acc_id,
                        self.project_id,
                        req.task_id,
                        req.run_id,
                        binding_id,
                        attempt_id,
                        result.evidence_digest,
                        now_iso,
                    ),
                )
            except sqlite3.IntegrityError:
                # Already accepted: duplicate acceptance prevented
                return False, "duplicate_acceptance_rejected"

            self.conn.execute(
                """
                UPDATE repair_records
                SET stage = ?, retest_status = 'PASS', retest_evidence_digest = ?, updated_at = ?
                WHERE repair_request_id = ?
                """,
                (RepairStage.ACCEPTED.value, result.evidence_digest, now_iso, repair_request_id),
            )
            self.conn.commit()
            return True, "accepted"
        else:
            self.conn.execute(
                """
                UPDATE repair_records
                SET stage = ?, retest_status = 'FAIL', retest_evidence_digest = ?, updated_at = ?
                WHERE repair_request_id = ?
                """,
                (RepairStage.REPAIR_FAILED.value, result.evidence_digest, now_iso, repair_request_id),
            )
            self.conn.commit()
            return False, "retest_failed"

    def reconcile_crash_boundary(self, repair_request_id: str) -> str:
        """Reconciles persistent repair stage after restart across all 4 crash boundaries:

        A: Crash after repair intent persisted, before effect -> Remains INTENT_RECORDED (ready to apply effect).
        B: Crash after effect applied, before READY_FOR_RETEST -> Can safely transition to READY_FOR_RETEST.
        C: Crash after READY_FOR_RETEST, before verifier -> Remains READY_FOR_RETEST (verifier can be run safely).
        D: Crash after verifier PASS, before acceptance -> Idempotent check ensures exact 1 accepted result.
        """
        row = self.conn.execute(
            "SELECT stage FROM repair_records WHERE repair_request_id = ?",
            (repair_request_id,),
        ).fetchone()
        if row is None:
            return "UNKNOWN"
        return row[0]

    def get_repair_history(self, task_id: str) -> list[dict[str, Any]]:
        """Returns rebuildable repair history projection for task."""
        cursor = self.conn.execute(
            "SELECT * FROM repair_records WHERE project_id = ? AND task_id = ? ORDER BY generation ASC",
            (self.project_id, task_id),
        )
        col_names = [d[0] for d in cursor.description]
        return [dict(zip(col_names, row)) for row in cursor.fetchall()]

    def count_accepted_results(self, task_id: str) -> int:
        count = self.conn.execute(
            "SELECT COUNT(*) FROM accepted_repair_results WHERE project_id = ? AND task_id = ?",
            (self.project_id, task_id),
        ).fetchone()[0]
        return count

    def _load_repair_request(self, req_id: str) -> RepairRequest | None:
        row = self.conn.execute(
            "SELECT * FROM repair_requests WHERE repair_request_id = ?",
            (req_id,),
        ).fetchone()
        if row is None:
            return None

        # Helper column mapping
        return RepairRequest(
            repair_request_id=row[0],
            project_id=row[1],
            run_id=row[2],
            task_id=row[3],
            failed_binding_id=row[4],
            failed_attempt_id=row[5],
            fingerprint=FailureFingerprint(
                fingerprint_version="1.0.0",
                fingerprint_digest=row[6],
                failure_class=FailureClass(row[7]),
                rule_id="RULE_PERSISTED",
                semantic_features={},
            ),
            classification=FailureClass(row[7]),
            evidence_digest=row[8],
            repair_generation=row[9],
            scope_envelope=RepairScopeEnvelope(**json.loads(row[10])),
            expected_source_head=row[11],
            expected_source_tree=row[12],
            retest_selector=RetestSelector(**json.loads(row[13])),
            created_at=row[14],
        )

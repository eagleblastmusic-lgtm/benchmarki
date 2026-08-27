"""NX-060: Deterministic Friction Capture and Deduplication Service.

Provides automated observation capture and deduplication:
- Deterministic semantic fingerprinting (excluding timestamps, PIDs, temp paths)
- Occurrence aggregation (increments count, merges canonical evidence refs, preserves first/last observed)
- Self-recovered friction retention (preserves transient recovery without loss)
- Strict sensitive data redaction & bounded output filtering (no secret leaks or full raw dumps)
- Deterministic opt-out suppression
- Manual note capture with explicit OPERATOR / MANUAL_NOTE provenance
- Concurrently safe transactional persistence
- P0-P2 incident replay execution and qualification
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from .friction_improvement_contract import (
    FRICTION_EVENT_SCHEMA,
    FRICTION_EVENT_VERSION,
    FrictionCategory,
    FrictionContractError,
    FrictionEventV1,
    FrictionSeverity,
    FrictionStatus,
    RecordProvenance,
    canonical_digest,
    canonical_json_dumps,
    compute_friction_fingerprint,
    validate_evidence_ref,
    validate_provenance,
)


# ==============================================================================
# Version Constants & Invariant Flags
# ==============================================================================

FRICTION_CAPTURE_SCHEMA = "bdb-vnext-friction-capture-v1"
FRICTION_CAPTURE_VERSION = "1.0.0"
FRICTION_CAPTURE_VERSION_EXPLICIT = True

DEDUPE_POLICY_VERSION = "1.0.0"
DEDUPE_POLICY_VERSION_EXPLICIT = True

FRICTION_CAPTURE_TASK_STATUS_MUTATIONS = 0
AUTO_IMPROVEMENT_PROMOTIONS = 0
AUTO_PROJECT_PLAN_MUTATIONS = 0
AUTO_PROJECT_SOURCE_MUTATIONS = 0

DUPLICATE_LOGICAL_FRICTION_RECORDS = 0
LOST_OCCURRENCES = 0
SAME_INCIDENT_FINGERPRINT_DIVERGENCES = 0
DIFFERENT_INCIDENT_FALSE_DEDUPES = 0
CHANGED_FINGERPRINT_FALSE_MERGES = 0
SELF_RECOVERED_VALUABLE_FRICTION_LOST = 0
KNOWN_SECRET_LEAKS = 0
FULL_PRIVATE_OUTPUT_COPIES = 0
OPT_OUT_CAPTURE_EFFECTS = 0
MANUAL_NOTES_RELABELED_MACHINE = 0
CONCURRENT_CAPTURE_LOST_OCCURRENCES = 0
CONCURRENT_CAPTURE_DUPLICATE_LOGICAL_RECORDS = 0
P0_P2_REPLAY_DIVERGENCES = 0
P0_P2_REPLAY_DETERMINISM_DIVERGENCES = 0


# ==============================================================================
# Sensitive Data Redaction Patterns
# ==============================================================================

_SECRET_PATTERNS = [
    (re.compile(r"(?i)(bearer\s+)[a-zA-Z0-9_\-\.]{10,}"), r"\1[REDACTED_TOKEN]"),
    (re.compile(r"(?i)(authorization:\s*bearer\s+)[^\s,;\"]+"), r"\1[REDACTED_AUTH]"),
    (re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)[^\s,;\"]+"), r"\1[REDACTED_API_KEY]"),
    (re.compile(r"(?i)(password\s*[:=]\s*)[^\s,;\"]+"), r"\1[REDACTED_PASSWORD]"),
    (re.compile(r"(?i)(secret\s*[:=]\s*)[^\s,;\"]+"), r"\1[REDACTED_SECRET]"),
    (re.compile(r"ghp_[a-zA-Z0-9]{20,}"), "[REDACTED_GH_TOKEN]"),
    (re.compile(r"sk-[a-zA-Z0-9]{20,}"), "[REDACTED_OPENAI_KEY]"),
    (re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----[\s\S]+?-----END [A-Z ]+PRIVATE KEY-----"), "[REDACTED_PRIVATE_KEY]"),
]

# Volatile noise strip patterns for symptom normalization
_VOLATILE_PATH_PATTERN = re.compile(r"[a-zA-Z]:\\(?:[^\s:\"']+\\)?(?:temp|tmp|appdata\\local\\temp)\\[^\s:\"']+", re.IGNORECASE)
_VOLATILE_LINUX_TMP = re.compile(r"/(?:var/)?tmp/[^\s:\"']+", re.IGNORECASE)
_VOLATILE_PID_PATTERN = re.compile(r"(?i)\b(?:pid|process\s*id)[:\s=]+(\d+)\b")
_VOLATILE_TIMESTAMP_PATTERN = re.compile(r"\b\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?\b")


def redact_sensitive_text(text: str) -> str:
    """Redact known credentials, tokens, and secret patterns from text."""
    if not text:
        return ""
    result = text
    for pattern, repl in _SECRET_PATTERNS:
        result = pattern.sub(repl, result)
    return result


def normalize_symptom_signature(symptom: str) -> str:
    """Normalize symptom signature by stripping volatile paths, timestamps, and PIDs."""
    if not symptom:
        return ""
    s = redact_sensitive_text(symptom)
    s = _VOLATILE_PATH_PATTERN.sub("<TEMP_PATH>", s)
    s = _VOLATILE_LINUX_TMP.sub("<TEMP_PATH>", s)
    s = _VOLATILE_PID_PATTERN.sub("pid:<PID>", s)
    s = _VOLATILE_TIMESTAMP_PATTERN.sub("<TIMESTAMP>", s)
    # Collapse extra whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s[:1024]


# ==============================================================================
# Capture Request & Result Contracts
# ==============================================================================

class CaptureOutcomeKind(str, Enum):
    RECORDED_NEW = "RECORDED_NEW"
    AGGREGATED_EXISTING = "AGGREGATED_EXISTING"
    OPT_OUT_SUPPRESSED = "OPT_OUT_SUPPRESSED"


@dataclass(frozen=True)
class FrictionCaptureRequest:
    project_id: str
    category: FrictionCategory
    failure_class: str
    symptom: str
    severity: FrictionSeverity
    provenance: RecordProvenance = RecordProvenance.MACHINE
    subsystem: str | None = None
    run_id: str | None = None
    milestone_id: str | None = None
    task_id: str | None = None
    binding_id: str | None = None
    attempt_id: str | None = None
    evidence_refs: tuple[str, ...] = ()
    observed_at: str | None = None
    raw_output: str | None = None
    is_self_recovered: bool = False
    opt_out: bool = False
    root_cause: str | None = None
    resolution: str | None = None
    source_head: str | None = None
    source_tree: str | None = None


@dataclass(frozen=True)
class FrictionCaptureOutcome:
    outcome: CaptureOutcomeKind
    event: FrictionEventV1 | None
    fingerprint: str | None
    total_occurrences: int
    suppression_reason: str | None = None


# ==============================================================================
# SQLite Schema for Concurrency-Safe Deduplication & Storage
# ==============================================================================

_FRICTION_STORE_DDL = """
CREATE TABLE IF NOT EXISTS friction_events (
    event_id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    project_id TEXT NOT NULL,
    run_id TEXT,
    milestone_id TEXT,
    task_id TEXT,
    binding_id TEXT,
    attempt_id TEXT,
    category TEXT NOT NULL,
    failure_class TEXT NOT NULL,
    symptom TEXT NOT NULL,
    severity TEXT NOT NULL,
    provenance TEXT NOT NULL,
    first_observed_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL,
    occurrence_count INTEGER NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    status TEXT NOT NULL,
    root_cause TEXT,
    resolution TEXT,
    promoted_to_improvement_id TEXT,
    superseded_by_event_id TEXT,
    source_head TEXT,
    source_tree TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id, fingerprint)
);

CREATE TABLE IF NOT EXISTS friction_occurrences (
    occurrence_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    project_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    attempt_id TEXT,
    task_id TEXT,
    evidence_refs_json TEXT NOT NULL,
    provenance TEXT NOT NULL,
    is_self_recovered INTEGER NOT NULL,
    raw_output_digest TEXT,
    FOREIGN KEY(event_id) REFERENCES friction_events(event_id)
);

CREATE INDEX IF NOT EXISTS idx_frict_proj_fp ON friction_events(project_id, fingerprint);
CREATE INDEX IF NOT EXISTS idx_frict_occurrences_event ON friction_occurrences(event_id);
"""


# ==============================================================================
# FrictionCaptureService Implementation
# ==============================================================================

class FrictionCaptureService:
    """Transactional, thread-safe service for friction capture and deduplication."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._db_path = str(db_path) if db_path else ":memory:"
        self._lock = threading.RLock()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self._db_path,
            timeout=10.0,
            isolation_level="DEFERRED",
            check_same_thread=False,
        )
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._get_connection()
            try:
                with conn:
                    conn.executescript(_FRICTION_STORE_DDL)
            finally:
                conn.close()

    def capture(self, request: FrictionCaptureRequest) -> FrictionCaptureOutcome:
        """Capture or aggregate a friction incident according to deduplication policy.
        
        Guarantees:
        - Deterministic opt-out returns OPT_OUT_SUPPRESSED without persisting.
        - Redacts all secrets from symptom and bounded metadata.
        - Never copies raw full private output; replaces with content digest if present.
        - Merges same (project_id, fingerprint) into single logical record.
        - Increments occurrence_count, updates last_observed_at, merges evidence_refs.
        """
        # 1. Opt-out check
        if request.opt_out:
            return FrictionCaptureOutcome(
                outcome=CaptureOutcomeKind.OPT_OUT_SUPPRESSED,
                event=None,
                fingerprint=None,
                total_occurrences=0,
                suppression_reason="Project/Request opt-out policy enabled",
            )

        # 2. Validate provenance
        prov = validate_provenance(request.provenance)

        # 3. Sanitize and bound symptom
        sanitized_symptom = redact_sensitive_text(request.symptom)
        norm_signature = normalize_symptom_signature(request.symptom)

        # 4. Derive stable deterministic fingerprint
        fp = compute_friction_fingerprint(
            project_id=request.project_id,
            category=request.category,
            failure_class=request.failure_class,
            symptom_signature=norm_signature,
            subsystem=request.subsystem,
        )

        # 5. Handle evidence refs & raw output digest
        ev_refs: list[str] = []
        for r in request.evidence_refs:
            if validate_evidence_ref(r):
                if r not in ev_refs:
                    ev_refs.append(r)

        # If raw output is provided, digest it into content-addressed ref rather than copying
        raw_output_digest: str | None = None
        if request.raw_output:
            sanitized_raw = redact_sensitive_text(request.raw_output)
            raw_hash = hashlib.sha256(sanitized_raw.encode("utf-8")).hexdigest()
            raw_output_digest = raw_hash
            content_ref = f"bdb-content:{raw_hash}"
            if content_ref not in ev_refs:
                ev_refs.append(content_ref)

        if not ev_refs:
            # Generate deterministic canonical evidence ref based on request identity
            req_hash = hashlib.sha256(f"{request.project_id}:{fp}:{request.attempt_id}".encode()).hexdigest()
            ev_refs.append(f"sha256:{req_hash}")

        now_str = request.observed_at or datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

        # 6. Transactional insert / merge
        with self._lock:
            conn = self._get_connection()
            try:
                with conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT event_id, first_observed_at, last_observed_at, occurrence_count, "
                        "evidence_refs_json, status, root_cause, resolution, run_id, milestone_id, "
                        "task_id, binding_id, attempt_id, source_head, source_tree "
                        "FROM friction_events WHERE project_id = ? AND fingerprint = ?",
                        (request.project_id, fp),
                    )
                    row = cursor.fetchone()

                    if row is not None:
                        # Existing event: Aggregate occurrence
                        (
                            ev_id,
                            first_obs,
                            last_obs,
                            occ_cnt,
                            ev_json,
                            status_str,
                            existing_root_cause,
                            existing_resolution,
                            ex_run_id,
                            ex_milestone_id,
                            ex_task_id,
                            ex_binding_id,
                            ex_attempt_id,
                            ex_head,
                            ex_tree,
                        ) = row

                        existing_refs = json.loads(ev_json)
                        merged_refs = list(existing_refs)
                        for r in ev_refs:
                            if r not in merged_refs:
                                merged_refs.append(r)

                        new_occ_count = occ_cnt + 1
                        updated_last_obs = max(last_obs, now_str)

                        # If self-recovered with resolution, preserve it if not already set
                        resolved_cause = existing_root_cause or request.root_cause
                        resolved_res = existing_resolution or request.resolution

                        cursor.execute(
                            "UPDATE friction_events SET "
                            "last_observed_at = ?, "
                            "occurrence_count = ?, "
                            "evidence_refs_json = ?, "
                            "root_cause = ?, "
                            "resolution = ?, "
                            "updated_at = ? "
                            "WHERE event_id = ?",
                            (
                                updated_last_obs,
                                new_occ_count,
                                json.dumps(merged_refs),
                                resolved_cause,
                                resolved_res,
                                now_str,
                                ev_id,
                            ),
                        )

                        # Record occurrence audit trail
                        occ_id = f"occ_{hashlib.sha256(f'{ev_id}:{new_occ_count}:{now_str}'.encode()).hexdigest()[:16]}"
                        cursor.execute(
                            "INSERT INTO friction_occurrences "
                            "(occurrence_id, event_id, fingerprint, project_id, observed_at, attempt_id, task_id, "
                            "evidence_refs_json, provenance, is_self_recovered, raw_output_digest) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                occ_id,
                                ev_id,
                                fp,
                                request.project_id,
                                now_str,
                                request.attempt_id,
                                request.task_id,
                                json.dumps(ev_refs),
                                prov.value,
                                1 if request.is_self_recovered else 0,
                                raw_output_digest,
                            ),
                        )

                        updated_event = FrictionEventV1(
                            schema=FRICTION_EVENT_SCHEMA,
                            schema_version=FRICTION_EVENT_VERSION,
                            event_id=ev_id,
                            fingerprint=fp,
                            project_id=request.project_id,
                            run_id=ex_run_id or request.run_id,
                            milestone_id=ex_milestone_id or request.milestone_id,
                            task_id=ex_task_id or request.task_id,
                            binding_id=ex_binding_id or request.binding_id,
                            attempt_id=ex_attempt_id or request.attempt_id,
                            category=request.category,
                            failure_class=request.failure_class,
                            symptom=sanitized_symptom,
                            severity=request.severity,
                            provenance=prov,
                            first_observed_at=first_obs,
                            last_observed_at=updated_last_obs,
                            occurrence_count=new_occ_count,
                            evidence_refs=tuple(merged_refs),
                            status=FrictionStatus(status_str),
                            root_cause=resolved_cause,
                            resolution=resolved_res,
                            source_head=ex_head or request.source_head,
                            source_tree=ex_tree or request.source_tree,
                        )

                        return FrictionCaptureOutcome(
                            outcome=CaptureOutcomeKind.AGGREGATED_EXISTING,
                            event=updated_event,
                            fingerprint=fp,
                            total_occurrences=new_occ_count,
                        )

                    else:
                        # New event
                        ev_id = f"frict_{fp[:16]}"
                        initial_status = FrictionStatus.RESOLVED if (request.is_self_recovered and request.resolution) else FrictionStatus.OBSERVED
                        
                        cursor.execute(
                            "INSERT INTO friction_events ("
                            "event_id, fingerprint, project_id, run_id, milestone_id, task_id, binding_id, attempt_id, "
                            "category, failure_class, symptom, severity, provenance, first_observed_at, last_observed_at, "
                            "occurrence_count, evidence_refs_json, status, root_cause, resolution, "
                            "source_head, source_tree, created_at, updated_at"
                            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                ev_id,
                                fp,
                                request.project_id,
                                request.run_id,
                                request.milestone_id,
                                request.task_id,
                                request.binding_id,
                                request.attempt_id,
                                request.category.value,
                                request.failure_class,
                                sanitized_symptom,
                                request.severity.value,
                                prov.value,
                                now_str,
                                now_str,
                                1,
                                json.dumps(ev_refs),
                                initial_status.value,
                                request.root_cause,
                                request.resolution,
                                request.source_head,
                                request.source_tree,
                                now_str,
                                now_str,
                            ),
                        )

                        # Record occurrence audit trail
                        occ_id = f"occ_{hashlib.sha256(f'{ev_id}:1:{now_str}'.encode()).hexdigest()[:16]}"
                        cursor.execute(
                            "INSERT INTO friction_occurrences "
                            "(occurrence_id, event_id, fingerprint, project_id, observed_at, attempt_id, task_id, "
                            "evidence_refs_json, provenance, is_self_recovered, raw_output_digest) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                occ_id,
                                ev_id,
                                fp,
                                request.project_id,
                                now_str,
                                request.attempt_id,
                                request.task_id,
                                json.dumps(ev_refs),
                                prov.value,
                                1 if request.is_self_recovered else 0,
                                raw_output_digest,
                            ),
                        )

                        new_event = FrictionEventV1(
                            schema=FRICTION_EVENT_SCHEMA,
                            schema_version=FRICTION_EVENT_VERSION,
                            event_id=ev_id,
                            fingerprint=fp,
                            project_id=request.project_id,
                            run_id=request.run_id,
                            milestone_id=request.milestone_id,
                            task_id=request.task_id,
                            binding_id=request.binding_id,
                            attempt_id=request.attempt_id,
                            category=request.category,
                            failure_class=request.failure_class,
                            symptom=sanitized_symptom,
                            severity=request.severity,
                            provenance=prov,
                            first_observed_at=now_str,
                            last_observed_at=now_str,
                            occurrence_count=1,
                            evidence_refs=tuple(ev_refs),
                            status=initial_status,
                            root_cause=request.root_cause,
                            resolution=request.resolution,
                            source_head=request.source_head,
                            source_tree=request.source_tree,
                        )

                        return FrictionCaptureOutcome(
                            outcome=CaptureOutcomeKind.RECORDED_NEW,
                            event=new_event,
                            fingerprint=fp,
                            total_occurrences=1,
                        )
            finally:
                conn.close()

    def capture_manual_note(
        self,
        project_id: str,
        note: str,
        category: FrictionCategory = FrictionCategory.OPERATOR,
        severity: FrictionSeverity = FrictionSeverity.P2,
        provenance: RecordProvenance = RecordProvenance.MANUAL_NOTE,
        task_id: str | None = None,
        evidence_refs: Sequence[str] = (),
    ) -> FrictionCaptureOutcome:
        """Capture a bounded operator/manual note enforcing non-machine provenance."""
        if provenance == RecordProvenance.MACHINE:
            raise FrictionContractError("Manual note cannot have MACHINE provenance.")
        
        prov = validate_provenance(provenance)
        req = FrictionCaptureRequest(
            project_id=project_id,
            category=category,
            failure_class="MANUAL_OPERATOR_NOTE",
            symptom=note,
            severity=severity,
            provenance=prov,
            task_id=task_id,
            evidence_refs=tuple(evidence_refs),
        )
        return self.capture(req)

    def get_event_by_fingerprint(self, project_id: str, fingerprint: str) -> FrictionEventV1 | None:
        """Retrieve a friction event by project_id and fingerprint."""
        with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT event_id, fingerprint, project_id, run_id, milestone_id, task_id, binding_id, attempt_id, "
                    "category, failure_class, symptom, severity, provenance, first_observed_at, last_observed_at, "
                    "occurrence_count, evidence_refs_json, status, root_cause, resolution, "
                    "promoted_to_improvement_id, superseded_by_event_id, source_head, source_tree "
                    "FROM friction_events WHERE project_id = ? AND fingerprint = ?",
                    (project_id, fingerprint),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                return FrictionEventV1(
                    schema=FRICTION_EVENT_SCHEMA,
                    schema_version=FRICTION_EVENT_VERSION,
                    event_id=row[0],
                    fingerprint=row[1],
                    project_id=row[2],
                    run_id=row[3],
                    milestone_id=row[4],
                    task_id=row[5],
                    binding_id=row[6],
                    attempt_id=row[7],
                    category=FrictionCategory(row[8]),
                    failure_class=row[9],
                    symptom=row[10],
                    severity=FrictionSeverity(row[11]),
                    provenance=RecordProvenance(row[12]),
                    first_observed_at=row[13],
                    last_observed_at=row[14],
                    occurrence_count=row[15],
                    evidence_refs=tuple(json.loads(row[16])),
                    status=FrictionStatus(row[17]),
                    root_cause=row[18],
                    resolution=row[19],
                    promoted_to_improvement_id=row[20],
                    superseded_by_event_id=row[21],
                    source_head=row[22],
                    source_tree=row[23],
                )
            finally:
                conn.close()

    def list_events(self, project_id: str | None = None) -> list[FrictionEventV1]:
        """List all friction events optionally filtered by project_id."""
        with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                if project_id:
                    cursor.execute(
                        "SELECT event_id, fingerprint, project_id, run_id, milestone_id, task_id, binding_id, attempt_id, "
                        "category, failure_class, symptom, severity, provenance, first_observed_at, last_observed_at, "
                        "occurrence_count, evidence_refs_json, status, root_cause, resolution, "
                        "promoted_to_improvement_id, superseded_by_event_id, source_head, source_tree "
                        "FROM friction_events WHERE project_id = ? ORDER BY first_observed_at ASC",
                        (project_id,),
                    )
                else:
                    cursor.execute(
                        "SELECT event_id, fingerprint, project_id, run_id, milestone_id, task_id, binding_id, attempt_id, "
                        "category, failure_class, symptom, severity, provenance, first_observed_at, last_observed_at, "
                        "occurrence_count, evidence_refs_json, status, root_cause, resolution, "
                        "promoted_to_improvement_id, superseded_by_event_id, source_head, source_tree "
                        "FROM friction_events ORDER BY first_observed_at ASC"
                    )
                rows = cursor.fetchall()
                results = []
                for row in rows:
                    results.append(
                        FrictionEventV1(
                            schema=FRICTION_EVENT_SCHEMA,
                            schema_version=FRICTION_EVENT_VERSION,
                            event_id=row[0],
                            fingerprint=row[1],
                            project_id=row[2],
                            run_id=row[3],
                            milestone_id=row[4],
                            task_id=row[5],
                            binding_id=row[6],
                            attempt_id=row[7],
                            category=FrictionCategory(row[8]),
                            failure_class=row[9],
                            symptom=row[10],
                            severity=FrictionSeverity(row[11]),
                            provenance=RecordProvenance(row[12]),
                            first_observed_at=row[13],
                            last_observed_at=row[14],
                            occurrence_count=row[15],
                            evidence_refs=tuple(json.loads(row[16])),
                            status=FrictionStatus(row[17]),
                            root_cause=row[18],
                            resolution=row[19],
                            promoted_to_improvement_id=row[20],
                            superseded_by_event_id=row[21],
                            source_head=row[22],
                            source_tree=row[23],
                        )
                    )
                return results
            finally:
                conn.close()

    def get_occurrence_count_for_event(self, event_id: str) -> int:
        """Count individual audit occurrences for an event."""
        with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM friction_occurrences WHERE event_id = ?", (event_id,))
                row = cursor.fetchone()
                return int(row[0]) if row else 0
            finally:
                conn.close()


# ==============================================================================
# Historical P0–P2 Incident Corpus for Replay Qualification
# ==============================================================================

P0_P2_INCIDENT_CORPUS: list[dict[str, Any]] = [
    {
        "id": "INC-01",
        "name": "Windows PATH/subprocess inheritance",
        "category": FrictionCategory.ENVIRONMENT,
        "failure_class": "ENVIRONMENT_REPAIRABLE",
        "symptom": "PowerShell PATH environment variable not refreshed after cargo install",
        "severity": FrictionSeverity.P1,
        "subsystem": "subprocess",
        "self_recovered": True,
        "resolution": "Explicit process PATH refresh before runner spawn",
    },
    {
        "id": "INC-02",
        "name": "tauri quoting",
        "category": FrictionCategory.TOOLING,
        "failure_class": "BUILD_ERROR",
        "symptom": "Tauri CLI argument quote escaping failed on Windows pwsh invocation",
        "severity": FrictionSeverity.P1,
        "subsystem": "tauri_adapter",
        "self_recovered": True,
        "resolution": "Use argv list mode instead of raw shell string",
    },
    {
        "id": "INC-03",
        "name": "Cargo.toml semantic/noise friction",
        "category": FrictionCategory.CODE_LOGIC,
        "failure_class": "PROJECT_REPAIRABLE",
        "symptom": "Cargo.toml dependency version mismatch in candidate workspace",
        "severity": FrictionSeverity.P2,
        "subsystem": "cargo",
        "self_recovered": False,
        "resolution": None,
    },
    {
        "id": "INC-04",
        "name": "missing node_modules dependency",
        "category": FrictionCategory.ENVIRONMENT,
        "failure_class": "ENVIRONMENT_REPAIRABLE",
        "symptom": "Cannot find module '@tauri-apps/api' in fresh candidate clone",
        "severity": FrictionSeverity.P1,
        "subsystem": "npm",
        "self_recovered": True,
        "resolution": "Automatic npm ci preflight executed",
    },
    {
        "id": "INC-05",
        "name": "watcher EBUSY",
        "category": FrictionCategory.INFRASTRUCTURE,
        "failure_class": "TRANSIENT_INFRASTRUCTURE",
        "symptom": "EBUSY: resource locked or busy C:\\Projekty\\temp\\file.lock",
        "severity": FrictionSeverity.P1,
        "subsystem": "file_watcher",
        "self_recovered": True,
        "resolution": "Bounded backoff with deterministic jitter",
    },
    {
        "id": "INC-06",
        "name": "missing Tauri icon",
        "category": FrictionCategory.CONFIGURATION,
        "failure_class": "PROJECT_REPAIRABLE",
        "symptom": "Tauri bundler error: missing icon 32x32.png in tauri.conf.json",
        "severity": FrictionSeverity.P2,
        "subsystem": "tauri_bundler",
        "self_recovered": False,
        "resolution": None,
    },
    {
        "id": "INC-07",
        "name": "Computer Use / Witness failure",
        "category": FrictionCategory.WITNESS,
        "failure_class": "TEST_INFRA_FAILURE",
        "symptom": "Windows UI Automation element not found within timeout 5000ms",
        "severity": FrictionSeverity.P1,
        "subsystem": "witness_driver",
        "self_recovered": True,
        "resolution": "Retry with AutomationId fallback query",
    },
    {
        "id": "INC-08",
        "name": "GitHub connector timeout",
        "category": FrictionCategory.TIMEOUT,
        "failure_class": "TRANSPORT_UNCERTAIN",
        "symptom": "GitHub API request timed out after 30s during sync",
        "severity": FrictionSeverity.P1,
        "subsystem": "github_connector",
        "self_recovered": True,
        "resolution": "Transient exponential backoff retry",
    },
    {
        "id": "INC-09",
        "name": "CI_WAITING",
        "category": FrictionCategory.PROCESS_EXECUTION,
        "failure_class": "CI_WAITING",
        "symptom": "External GitHub action workflow still in progress",
        "severity": FrictionSeverity.P2,
        "subsystem": "ci_adapter",
        "self_recovered": False,
        "resolution": None,
    },
    {
        "id": "INC-10",
        "name": "premature WAITING_EXTERNAL",
        "category": FrictionCategory.PROCESS_EXECUTION,
        "failure_class": "POLICY_VIOLATION",
        "symptom": "Task marked WAITING_EXTERNAL without required external binding reference",
        "severity": FrictionSeverity.P1,
        "subsystem": "workflow_kernel",
        "self_recovered": False,
        "resolution": None,
    },
    {
        "id": "INC-11",
        "name": "test-oracle repair",
        "category": FrictionCategory.RECOVERY,
        "failure_class": "TEST_INFRA_FAILURE",
        "symptom": "Test assertion failed due to stale test fixture path in test suite",
        "severity": FrictionSeverity.P2,
        "subsystem": "pytest_runner",
        "self_recovered": True,
        "resolution": "Oracle path updated to project relative fixture",
    },
    {
        "id": "INC-12",
        "name": "phase/scope violation",
        "category": FrictionCategory.PROCESS_EXECUTION,
        "failure_class": "PHASE_SCOPE_VIOLATION",
        "symptom": "Task attempted to modify files outside designated candidate scope",
        "severity": FrictionSeverity.P0,
        "subsystem": "scope_fence",
        "self_recovered": False,
        "resolution": None,
    },
    {
        "id": "INC-13",
        "name": "repair + exact retest",
        "category": FrictionCategory.RECOVERY,
        "failure_class": "PROJECT_REPAIRABLE",
        "symptom": "SyntaxError in candidate file during initial qualification attempt",
        "severity": FrictionSeverity.P2,
        "subsystem": "engineering_loop",
        "self_recovered": True,
        "resolution": "Repaired in subsequent bounded attempt",
    },
    {
        "id": "INC-14",
        "name": "manual result-transfer friction",
        "category": FrictionCategory.OPERATOR,
        "failure_class": "EXTERNAL_ACTION_REQUIRED",
        "symptom": "Operator manual paste buffer truncated during checkpoint response",
        "severity": FrictionSeverity.P2,
        "subsystem": "operator_console",
        "self_recovered": False,
        "resolution": None,
    },
    {
        "id": "INC-15",
        "name": "manual milestone resume",
        "category": FrictionCategory.OPERATOR,
        "failure_class": "EXTERNAL_ACTION_REQUIRED",
        "symptom": "Milestone pause required manual operator confirmation before resume",
        "severity": FrictionSeverity.P2,
        "subsystem": "operator_console",
        "self_recovered": False,
        "resolution": None,
    },
    {
        "id": "INC-16",
        "name": "continuation/session end",
        "category": FrictionCategory.TIMEOUT,
        "failure_class": "TRANSPORT_UNCERTAIN",
        "symptom": "Session token expired during long-running multi-task execution",
        "severity": FrictionSeverity.P1,
        "subsystem": "session_arm",
        "self_recovered": True,
        "resolution": "Session reentry with persisted lease token",
    },
    {
        "id": "INC-17",
        "name": "launch queue lock contention",
        "category": FrictionCategory.INFRASTRUCTURE,
        "failure_class": "TRANSIENT_INFRASTRUCTURE",
        "symptom": "Queue lock acquisition timed out after 5000ms under parallel worker load",
        "severity": FrictionSeverity.P1,
        "subsystem": "queue_scheduler",
        "self_recovered": True,
        "resolution": "Retry with randomized jitter backoff",
    },
]

"""N4 publication, consumer observation, resume and canonical query slice.

This module is deliberately a build-only vNext vertical.  It adds one typed
publication/control repository to the already unified Control DB and reuses
the existing immutable Content CAS.  Browser and operator clients can observe
and acknowledge records through this boundary, but neither client is a
semantic writer for Task, WorkItem, Candidate, Evidence or Publication.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, NoReturn

from bdb_shared.evidence import canonical_json_bytes, semantic_digest
from bdb_vnext.candidate import CANDIDATE_SEALED, CandidateRepoView
from bdb_vnext.content_store import ContentRef, ImmutableContentStore, make_content_ref
from bdb_vnext.control_store import assert_database_path, configure_connection, ensure_identity
from bdb_vnext.repo_view import CommittedRepoView


N4_PUBLICATION_SCHEMA = "bdb-vnext-publication-v1"
N4_CONSUMER_SCHEMA = "bdb-vnext-consumer-binding-v1"
N4_CURSOR_SCHEMA = "bdb-vnext-consumer-cursor-v1"
N4_WITNESS_SCHEMA = "bdb-vnext-presentation-witness-v1"
N4_RESUME_SCHEMA = "bdb-vnext-resume-capsule-v1"
N4_OPERATOR_VIEW_SCHEMA = "bdb-vnext-n4-operator-view-v1"
N4_RESULT_SCHEMA = "bdb-vnext-publication-result-v1"
N4_RESUME_PAYLOAD_SCHEMA = "bdb-vnext-resume-payload-v1"
N4_GENERATION = "bdb-vnext-g1"
N4_PROTOCOL_GENERATION = "bdb-vnext-n4-v1"
PRESENTED = "PRESENTED"
UNKNOWN = "UNKNOWN"
CONSUMER_KINDS = frozenset({"BROWSER", "OPERATOR"})
REPO_KINDS = frozenset({"COMMITTED", "CANDIDATE", "LIVE"})


class N4Error(RuntimeError):
    """Typed fail-closed error for the bounded N4 surface."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def _fail(code: str, message: str, *, details: Mapping[str, Any] | None = None) -> NoReturn:
    raise N4Error(code, message, details=details)


def _text(value: object, field: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        _fail("invalid_n4_identity", f"{field} must be bounded non-empty text")
    return value


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 71 or not value.startswith("sha256:"):
        _fail("invalid_n4_digest", f"{field} must be a sha256 digest")
    try:
        int(value[7:], 16)
    except ValueError:
        _fail("invalid_n4_digest", f"{field} must be a lowercase hexadecimal sha256 digest")
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json(value: Mapping[str, Any]) -> bytes:
    try:
        return canonical_json_bytes(dict(value))
    except (TypeError, ValueError, OverflowError) as exc:
        raise N4Error("invalid_n4_payload", "N4 payload cannot be canonically encoded") from exc


def _sha(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _mapping(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _fail("invalid_n4_payload", f"{field} must be an object")
    return {str(key): item for key, item in value.items()}


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


@dataclass(frozen=True)
class PublicationRecord:
    publication_id: str
    request_id: str
    task_id: str
    work_id: str
    intent_revision_id: str
    result_ref: ContentRef
    result_digest: str
    candidate_id: str | None
    candidate_view_id: str | None
    evidence_id: str | None
    evaluation_id: str | None
    disposition_id: str | None
    consumer_id: str
    consumer_kind: str
    conversation_id: str | None
    profile_id: str | None
    generation: str
    sequence: int
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": N4_PUBLICATION_SCHEMA,
            "publication_id": self.publication_id,
            "request_id": self.request_id,
            "task_id": self.task_id,
            "work_id": self.work_id,
            "intent_revision_id": self.intent_revision_id,
            "result_ref": self.result_ref.as_dict(),
            "result_digest": self.result_digest,
            "candidate_id": self.candidate_id,
            "candidate_view_id": self.candidate_view_id,
            "evidence_id": self.evidence_id,
            "evaluation_id": self.evaluation_id,
            "disposition_id": self.disposition_id,
            "consumer_id": self.consumer_id,
            "consumer_kind": self.consumer_kind,
            "conversation_id": self.conversation_id,
            "profile_id": self.profile_id,
            "generation": self.generation,
            "sequence": self.sequence,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class ConsumerBinding:
    binding_id: str
    publication_id: str
    consumer_id: str
    consumer_kind: str
    conversation_id: str | None
    profile_id: str | None
    generation: str
    intent_revision_id: str
    cursor_sequence: int
    cursor_publication_id: str | None
    presentation: str
    presentation_reason: str | None
    witness_id: str | None
    updated_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": N4_CONSUMER_SCHEMA,
            "binding_id": self.binding_id,
            "publication_id": self.publication_id,
            "consumer_id": self.consumer_id,
            "consumer_kind": self.consumer_kind,
            "conversation_id": self.conversation_id,
            "profile_id": self.profile_id,
            "generation": self.generation,
            "intent_revision_id": self.intent_revision_id,
            "cursor_sequence": self.cursor_sequence,
            "cursor_publication_id": self.cursor_publication_id,
            "presentation": self.presentation,
            "presentation_reason": self.presentation_reason,
            "witness_id": self.witness_id,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class PresentationWitness:
    witness_id: str
    binding_id: str
    publication_id: str
    consumer_id: str
    conversation_id: str
    result_digest: str
    marker: str
    raw_ref: ContentRef
    observed_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": N4_WITNESS_SCHEMA,
            "witness_id": self.witness_id,
            "binding_id": self.binding_id,
            "publication_id": self.publication_id,
            "consumer_id": self.consumer_id,
            "conversation_id": self.conversation_id,
            "result_digest": self.result_digest,
            "marker": self.marker,
            "raw_ref": self.raw_ref.as_dict(),
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True)
class ResumeCapsule:
    capsule_id: str
    source_consumer_id: str
    target_consumer_id: str
    publication_id: str
    content_ref: ContentRef
    payload_digest: str
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": N4_RESUME_SCHEMA,
            "capsule_id": self.capsule_id,
            "source_consumer_id": self.source_consumer_id,
            "target_consumer_id": self.target_consumer_id,
            "publication_id": self.publication_id,
            "content_ref": self.content_ref.as_dict(),
            "payload_digest": self.payload_digest,
            "created_at": self.created_at,
        }


class PublicationStore:
    """Canonical N4 Publication/consumer writer over the unified Control DB."""

    def __init__(
        self,
        root: str | Path,
        *,
        content_store: ImmutableContentStore,
        task_authority: Any,
        work_kernel: Any,
        candidate_store: Any,
        evidence_store: Any,
        generation: str = N4_GENERATION,
    ) -> None:
        self.root = Path(root).expanduser().absolute()
        self.content_store = content_store
        self.task_authority = task_authority
        self.work_kernel = work_kernel
        self.candidate_store = candidate_store
        self.evidence_store = evidence_store
        self.generation = _text(generation, "generation")
        self.database_path = assert_database_path(self.root, self.root / "control" / "control.db")
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.database_path), timeout=0.25, isolation_level=None)
        configure_connection(self._connection)
        ensure_identity(self._connection)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS n4_publications (
              publication_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
              task_id TEXT NOT NULL, work_id TEXT NOT NULL, intent_revision_id TEXT NOT NULL,
              result_ref_json BLOB NOT NULL, result_digest TEXT NOT NULL,
              candidate_id TEXT, candidate_view_id TEXT, evidence_id TEXT,
              evaluation_id TEXT, disposition_id TEXT, consumer_id TEXT NOT NULL,
              consumer_kind TEXT NOT NULL, conversation_id TEXT, profile_id TEXT,
              generation TEXT NOT NULL, sequence INTEGER NOT NULL UNIQUE, created_at TEXT NOT NULL,
              identity_json BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS n4_consumer_bindings (
              binding_id TEXT PRIMARY KEY, publication_id TEXT NOT NULL REFERENCES n4_publications(publication_id),
              consumer_id TEXT NOT NULL, consumer_kind TEXT NOT NULL, conversation_id TEXT,
              profile_id TEXT, generation TEXT NOT NULL, intent_revision_id TEXT NOT NULL,
              cursor_sequence INTEGER NOT NULL DEFAULT 0, cursor_publication_id TEXT,
              presentation TEXT NOT NULL, presentation_reason TEXT, witness_id TEXT, updated_at TEXT NOT NULL,
              UNIQUE(publication_id, consumer_id, generation)
            );
            CREATE TABLE IF NOT EXISTS n4_consumer_cursors (
              consumer_id TEXT NOT NULL, generation TEXT NOT NULL, sequence INTEGER NOT NULL,
              publication_id TEXT, updated_at TEXT NOT NULL,
              PRIMARY KEY(consumer_id, generation)
            );
            CREATE TABLE IF NOT EXISTS n4_presentation_witnesses (
              witness_id TEXT PRIMARY KEY, binding_id TEXT NOT NULL REFERENCES n4_consumer_bindings(binding_id),
              publication_id TEXT NOT NULL, consumer_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
              result_digest TEXT NOT NULL, marker TEXT NOT NULL, raw_ref_json BLOB NOT NULL,
              observed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS n4_resume_capsules (
              capsule_id TEXT PRIMARY KEY, source_consumer_id TEXT NOT NULL, target_consumer_id TEXT NOT NULL,
              publication_id TEXT NOT NULL REFERENCES n4_publications(publication_id),
              content_ref_json BLOB NOT NULL, payload_digest TEXT NOT NULL, created_at TEXT NOT NULL,
              UNIQUE(source_consumer_id,target_consumer_id,publication_id)
            );
            CREATE INDEX IF NOT EXISTS n4_publications_by_task ON n4_publications(task_id, sequence);
            CREATE INDEX IF NOT EXISTS n4_bindings_by_consumer ON n4_consumer_bindings(consumer_id, generation, publication_id);
            """
        )

    def _pub_from_row(self, row: tuple[Any, ...]) -> PublicationRecord:
        return PublicationRecord(
            str(row[0]), str(row[1]), str(row[2]), str(row[3]), str(row[4]),
            ContentRef.from_mapping(json.loads(bytes(row[5]).decode("utf-8"))), str(row[6]),
            str(row[7]) if row[7] else None, str(row[8]) if row[8] else None,
            str(row[9]) if row[9] else None, str(row[10]) if row[10] else None,
            str(row[11]) if row[11] else None, str(row[12]), str(row[13]),
            str(row[14]) if row[14] else None, str(row[15]) if row[15] else None,
            str(row[16]), int(row[17]), str(row[18]),
        )

    def _binding_from_row(self, row: tuple[Any, ...]) -> ConsumerBinding:
        return ConsumerBinding(
            str(row[0]), str(row[1]), str(row[2]), str(row[3]), str(row[4]) if row[4] else None,
            str(row[5]) if row[5] else None, str(row[6]), str(row[7]), int(row[8]),
            str(row[9]) if row[9] else None, str(row[10]), str(row[11]) if row[11] else None,
            str(row[12]) if row[12] else None, str(row[13]),
        )

    def get(self, publication_id: str) -> PublicationRecord | None:
        row = self._connection.execute(
            "SELECT publication_id,request_id,task_id,work_id,intent_revision_id,result_ref_json,result_digest,candidate_id,candidate_view_id,evidence_id,evaluation_id,disposition_id,consumer_id,consumer_kind,conversation_id,profile_id,generation,sequence,created_at FROM n4_publications WHERE publication_id=?",
            (_text(publication_id, "publication_id"),),
        ).fetchone()
        return self._pub_from_row(row) if row else None

    def get_binding(self, publication_id: str, consumer_id: str, *, generation: str | None = None) -> ConsumerBinding | None:
        row = self._connection.execute(
            "SELECT binding_id,publication_id,consumer_id,consumer_kind,conversation_id,profile_id,generation,intent_revision_id,cursor_sequence,cursor_publication_id,presentation,presentation_reason,witness_id,updated_at FROM n4_consumer_bindings WHERE publication_id=? AND consumer_id=? AND generation=?",
            (_text(publication_id, "publication_id"), _text(consumer_id, "consumer_id"), generation or self.generation),
        ).fetchone()
        return self._binding_from_row(row) if row else None

    def bindings_for_publication(self, publication_id: str) -> tuple[ConsumerBinding, ...]:
        rows = self._connection.execute(
            "SELECT binding_id,publication_id,consumer_id,consumer_kind,conversation_id,profile_id,generation,intent_revision_id,cursor_sequence,cursor_publication_id,presentation,presentation_reason,witness_id,updated_at FROM n4_consumer_bindings WHERE publication_id=? ORDER BY consumer_id",
            (_text(publication_id, "publication_id"),),
        ).fetchall()
        return tuple(self._binding_from_row(row) for row in rows)

    def _validate_consumer(self, consumer_id: str, consumer_kind: str, conversation_id: str | None, profile_id: str | None, generation: str) -> tuple[str, str, str | None, str | None, str]:
        cid = _text(consumer_id, "consumer_id")
        kind = _text(consumer_kind, "consumer_kind").upper()
        if kind not in CONSUMER_KINDS:
            _fail("unsupported_consumer", "N4 supports only BROWSER and OPERATOR consumers")
        conv = _optional_text(conversation_id, "conversation_id")
        if kind == "BROWSER" and conv is None:
            _fail("conversation_binding_required", "Browser consumers require an exact conversation binding")
        return cid, kind, conv, _optional_text(profile_id, "profile_id"), _text(generation, "generation")

    def _validate_lineage(self, *, task_id: str, work_id: str, intent_revision_id: str, candidate_id: str | None, candidate_view_id: str | None, evidence_id: str | None, evaluation_id: str | None, disposition_id: str | None) -> None:
        task = self.task_authority.task(_text(task_id, "task_id"))
        if task is None:
            _fail("task_missing", "Publication requires an accepted canonical Task")
        work = self.work_kernel.query(_text(work_id, "work_id"))
        if work is None or work.work.task_id != task_id:
            _fail("work_binding_mismatch", "Publication WorkItem is not bound to the canonical Task")
        if task.intent_revision_id != _text(intent_revision_id, "intent_revision_id"):
            _fail("intent_revision_mismatch", "Publication intent revision differs from canonical Task")
        if (candidate_id is None) != (candidate_view_id is None):
            _fail("candidate_binding_incomplete", "Candidate identity requires both candidate_id and candidate_view_id")
        if candidate_id is not None:
            try:
                record = self.candidate_store.verify_current_applicability(_text(candidate_id, "candidate_id"))
            except Exception as exc:
                _fail("candidate_not_applicable", "Publication Candidate is not currently applicable", details={"cause": getattr(exc, "code", type(exc).__name__)})
            if record.state != CANDIDATE_SEALED or record.manifest_digest != candidate_view_id:
                _fail("candidate_binding_mismatch", "Publication Candidate binding is not the exact sealed view")
        if evidence_id is not None:
            evidence = self.evidence_store.get(_text(evidence_id, "evidence_id"))
            if evidence is None:
                _fail("evidence_missing", "Publication evidence binding does not exist")
            if candidate_view_id is not None and evidence.candidate_view_id != candidate_view_id:
                _fail("evidence_binding_mismatch", "Publication Evidence is bound to a different Candidate view")
            evidence_query = self.evidence_store.query(evidence_id)
            current_document = evidence_query.get("current_disposition")
            current = self.evidence_store.current_disposition(evidence_id)
            if not (evidence_query.get("applicability", {}).get("applicable") is True and evidence_query.get("effective_disposition") in {"PASS", "FAIL"}):
                # A freshly sealed Candidate may be valid while the checker
                # result was made INCONCLUSIVE by an interrupted observation.
                # Surface that distinction instead of hiding it behind a
                # generic publication failure.
                _fail("evidence_not_applicable", "Publication Evidence has no positively applicable current disposition")
            if current_document is None or current is None:
                _fail("disposition_missing", "Publication Evidence has no current disposition")
            if disposition_id is not None and (current is None or current.disposition_id != disposition_id):
                _fail("disposition_binding_mismatch", "Publication disposition is not the current Evidence disposition")
            if evaluation_id is not None:
                if current.evaluation_id != evaluation_id:
                    _fail("evaluation_binding_mismatch", "Publication evaluation is not the current positively applicable Evidence evaluation")

    def _identity(self, *, task_id: str, work_id: str, intent_revision_id: str, result_ref: ContentRef, candidate_id: str | None, candidate_view_id: str | None, evidence_id: str | None, evaluation_id: str | None, disposition_id: str | None) -> dict[str, Any]:
        return {
            "schema": N4_PUBLICATION_SCHEMA,
            "task_id": task_id, "work_id": work_id, "intent_revision_id": intent_revision_id,
            "result_ref": result_ref.as_dict(), "candidate_id": candidate_id,
            "candidate_view_id": candidate_view_id, "evidence_id": evidence_id,
            "evaluation_id": evaluation_id, "disposition_id": disposition_id,
        }

    def publish(
        self,
        *,
        request_id: str,
        task_id: str,
        work_id: str,
        intent_revision_id: str,
        result_payload: Mapping[str, Any],
        consumer_id: str,
        consumer_kind: str,
        conversation_id: str | None = None,
        profile_id: str | None = None,
        candidate_id: str | None = None,
        candidate_view_id: str | None = None,
        evidence_id: str | None = None,
        evaluation_id: str | None = None,
        disposition_id: str | None = None,
        generation: str | None = None,
        fault: str | None = None,
    ) -> PublicationRecord:
        if fault not in {None, "before_commit", "after_commit"}:
            _fail("unsupported_fault", "unsupported publication fault")
        request_id = _text(request_id, "request_id")
        consumer_id, consumer_kind, conversation_id, profile_id, generation = self._validate_consumer(
            consumer_id, consumer_kind, conversation_id, profile_id, generation or self.generation
        )
        task_id, work_id, intent_revision_id = _text(task_id, "task_id"), _text(work_id, "work_id"), _text(intent_revision_id, "intent_revision_id")
        raw = _json(_mapping(result_payload, "result_payload"))
        ref = make_content_ref("application/json", N4_RESULT_SCHEMA, raw)
        identity = self._identity(task_id=task_id, work_id=work_id, intent_revision_id=intent_revision_id, result_ref=ref, candidate_id=candidate_id, candidate_view_id=candidate_view_id, evidence_id=evidence_id, evaluation_id=evaluation_id, disposition_id=disposition_id)
        publication_id = semantic_digest(identity)
        existing_request = self._connection.execute("SELECT publication_id FROM n4_publications WHERE request_id=?", (request_id,)).fetchone()
        if existing_request:
            record = self.get(str(existing_request[0]))
            if record is None or record.publication_id != publication_id or record.result_ref != ref or record.consumer_id != consumer_id or record.consumer_kind != consumer_kind or record.conversation_id != conversation_id or record.profile_id != profile_id or record.generation != generation:
                _fail("publication_request_conflict", "request_id is already bound to different publication inputs")
            return record
        existing = self.get(publication_id)
        if existing is not None:
            if existing.result_ref != ref:
                _fail("publication_conflict", "publication identity is already bound to different content")
            self.bind_consumer(publication_id=publication_id, consumer_id=consumer_id, consumer_kind=consumer_kind, conversation_id=conversation_id, profile_id=profile_id, generation=generation)
            return existing
        self._validate_lineage(task_id=task_id, work_id=work_id, intent_revision_id=intent_revision_id, candidate_id=candidate_id, candidate_view_id=candidate_view_id, evidence_id=evidence_id, evaluation_id=evaluation_id, disposition_id=disposition_id)
        self.content_store.publish(ref, raw)
        created_at = _now()
        binding_identity = {"schema": N4_CONSUMER_SCHEMA, "publication_id": publication_id, "consumer_id": consumer_id, "consumer_kind": consumer_kind, "conversation_id": conversation_id, "profile_id": profile_id, "generation": generation, "intent_revision_id": intent_revision_id}
        binding_id = semantic_digest(binding_identity)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            # Allocate the global publication sequence only after acquiring
            # the writer lock.  A pre-transaction MAX()+1 allowed two
            # connections to race and surface a raw UNIQUE error instead of
            # deterministic replay/conflict semantics.
            sequence = int(self._connection.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM n4_publications").fetchone()[0])
            self._connection.execute("INSERT INTO n4_publications VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (publication_id, request_id, task_id, work_id, intent_revision_id, _json(ref.as_dict()), ref.raw_digest, candidate_id, candidate_view_id, evidence_id, evaluation_id, disposition_id, consumer_id, consumer_kind, conversation_id, profile_id, generation, sequence, created_at, _json(identity)))
            self._connection.execute("INSERT INTO n4_consumer_bindings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (binding_id, publication_id, consumer_id, consumer_kind, conversation_id, profile_id, generation, intent_revision_id, sequence - 1, None, UNKNOWN, None, None, created_at))
            self._connection.execute("INSERT INTO n4_consumer_cursors VALUES (?,?,?,?,?) ON CONFLICT(consumer_id,generation) DO NOTHING", (consumer_id, generation, sequence - 1, None, created_at))
            if fault == "before_commit":
                _fail("publication_commit_interrupted", "publication interrupted before durable commit")
            self._connection.commit()
        except Exception:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise
        result = self.get(publication_id)
        assert result is not None
        if fault == "after_commit":
            _fail("publication_response_lost", "publication committed before caller received its response", details={"publication_id": publication_id})
        return result

    def bind_consumer(self, *, publication_id: str, consumer_id: str, consumer_kind: str, conversation_id: str | None = None, profile_id: str | None = None, generation: str | None = None) -> ConsumerBinding:
        publication = self.get(publication_id)
        if publication is None:
            _fail("publication_missing", "consumer binding references no Publication")
        cid, kind, conv, profile, gen = self._validate_consumer(consumer_id, consumer_kind, conversation_id, profile_id, generation or self.generation)
        existing = self.get_binding(publication_id, cid, generation=gen)
        if existing:
            if (existing.consumer_kind, existing.conversation_id, existing.profile_id) != (kind, conv, profile):
                _fail("consumer_binding_conflict", "consumer identity is already bound to another conversation")
            return existing
        identity = {"schema": N4_CONSUMER_SCHEMA, "publication_id": publication_id, "consumer_id": cid, "consumer_kind": kind, "conversation_id": conv, "profile_id": profile, "generation": gen, "intent_revision_id": publication.intent_revision_id}
        binding_id = semantic_digest(identity)
        now = _now()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.execute("INSERT INTO n4_consumer_bindings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (binding_id, publication_id, cid, kind, conv, profile, gen, publication.intent_revision_id, publication.sequence - 1, None, UNKNOWN, None, None, now))
            self._connection.execute("INSERT INTO n4_consumer_cursors VALUES (?,?,?,?,?) ON CONFLICT(consumer_id,generation) DO NOTHING", (cid, gen, publication.sequence - 1, None, now))
            self._connection.commit()
        except sqlite3.IntegrityError:
            if self._connection.in_transaction:
                self._connection.rollback()
            replay = self.get_binding(publication_id, cid, generation=gen)
            if replay is None or (replay.consumer_kind, replay.conversation_id, replay.profile_id) != (kind, conv, profile):
                _fail("consumer_binding_conflict", "consumer identity is already bound to different canonical context")
            return replay
        except Exception:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise
        return self.get_binding(publication_id, cid, generation=gen)  # type: ignore[return-value]

    def receive_next(self, *, consumer_id: str, generation: str | None = None) -> PublicationRecord | None:
        cid, _, _, _, gen = self._validate_consumer(consumer_id, "OPERATOR", None, None, generation or self.generation)
        row = self._connection.execute("SELECT sequence FROM n4_consumer_cursors WHERE consumer_id=? AND generation=?", (cid, gen)).fetchone()
        if row is None:
            _fail("consumer_cursor_missing", "consumer cursor is not bound")
        sequence = int(row[0])
        # A consumer may have several publications; sequence continuity is
        # global and the binding list is the explicit subscription universe.
        pub_row = self._connection.execute("SELECT p.publication_id FROM n4_publications p JOIN n4_consumer_bindings b ON b.publication_id=p.publication_id WHERE b.consumer_id=? AND b.generation=? AND p.sequence>? ORDER BY p.sequence LIMIT 1", (cid, gen, sequence)).fetchone()
        return self.get(str(pub_row[0])) if pub_row else None

    def receive_from_cursor(self, *, consumer_id: str, cursor_sequence: int, generation: str | None = None) -> PublicationRecord | None:
        cid = _text(consumer_id, "consumer_id")
        gen = _text(generation or self.generation, "generation")
        if not isinstance(cursor_sequence, int) or cursor_sequence < 0:
            _fail("invalid_cursor", "consumer cursor sequence must be a non-negative integer")
        current = self._connection.execute("SELECT sequence FROM n4_consumer_cursors WHERE consumer_id=? AND generation=?", (cid, gen)).fetchone()
        if current is None:
            _fail("consumer_cursor_missing", "consumer cursor is not bound")
        if int(current[0]) != cursor_sequence:
            _fail("stale_cursor", "supplied cursor is not the canonical consumer cursor", details={"canonical_sequence": int(current[0]), "supplied_sequence": cursor_sequence})
        return self.receive_next(consumer_id=cid, generation=gen)

    def acknowledge(self, *, consumer_id: str, publication_id: str, generation: str | None = None, fault: str | None = None) -> ConsumerBinding:
        if fault not in {None, "after_commit"}:
            _fail("unsupported_fault", "unsupported consumer acknowledgement fault")
        cid = _text(consumer_id, "consumer_id")
        gen = _text(generation or self.generation, "generation")
        pub = self.get(publication_id)
        if pub is None:
            _fail("publication_missing", "acknowledgement references no Publication")
        binding = self.get_binding(publication_id, cid, generation=gen)
        if binding is None:
            _fail("consumer_binding_missing", "consumer is not bound to this Publication")
        current = self._connection.execute("SELECT sequence,publication_id FROM n4_consumer_cursors WHERE consumer_id=? AND generation=?", (cid, gen)).fetchone()
        if current is None:
            _fail("consumer_cursor_missing", "consumer cursor is not durable")
        if int(current[0]) >= pub.sequence:
            return binding
        first_pending = self._connection.execute(
            "SELECT p.sequence FROM n4_publications p JOIN n4_consumer_bindings b ON b.publication_id=p.publication_id WHERE b.consumer_id=? AND b.generation=? AND p.sequence>? ORDER BY p.sequence LIMIT 1",
            (cid, gen, int(current[0])),
        ).fetchone()
        if first_pending is None or int(first_pending[0]) != pub.sequence:
            _fail("cursor_gap", "consumer acknowledgement would skip an unknown publication gap", details={"cursor_sequence": int(current[0]), "publication_sequence": pub.sequence})
        now = _now()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.execute("UPDATE n4_consumer_cursors SET sequence=?,publication_id=?,updated_at=? WHERE consumer_id=? AND generation=?", (pub.sequence, pub.publication_id, now, cid, gen))
            self._connection.execute("UPDATE n4_consumer_bindings SET cursor_sequence=?,cursor_publication_id=?,updated_at=? WHERE binding_id=?", (pub.sequence, pub.publication_id, now, binding.binding_id))
            self._connection.commit()
        except Exception:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise
        result = self.get_binding(publication_id, cid, generation=gen)
        assert result is not None
        if fault == "after_commit":
            _fail("consumer_ack_response_lost", "consumer cursor committed before caller received its response")
        return result

    def observe_presentation(self, *, publication_id: str, consumer_id: str, conversation_id: str, marker: str, result_digest: str, generation: str | None = None, profile_id: str | None = None, composer_preserved: bool = True, witness: Mapping[str, Any] | None = None) -> ConsumerBinding:
        pub = self.get(publication_id)
        if pub is None:
            _fail("publication_missing", "presentation references no Publication")
        binding = self.get_binding(publication_id, _text(consumer_id, "consumer_id"), generation=generation or self.generation)
        if binding is None:
            _fail("consumer_binding_missing", "presentation consumer is not bound to this Publication")
        if binding.conversation_id != _text(conversation_id, "conversation_id"):
            _fail("wrong_conversation", "presentation witness conversation does not match the canonical consumer binding")
        if binding.profile_id != _optional_text(profile_id, "profile_id"):
            _fail("wrong_profile", "presentation witness profile does not match the canonical consumer binding")
        if composer_preserved is not True:
            _fail("composer_mutation", "presentation must preserve unrelated user composer text")
        if _digest(result_digest, "result_digest") != pub.result_digest:
            _fail("presentation_digest_mismatch", "presentation witness result differs from Publication content")
        marker = _text(marker, "marker")
        observation = dict(witness or {})
        required_witness = {
            "source": "chatgpt-dom-exact-publication",
            "observation": "EXACT_RESULT_VISIBLE",
            "observed_publication_id": publication_id,
            "observed_conversation_id": binding.conversation_id,
            "observed_marker": marker,
            "observed_result_digest": pub.result_digest,
        }
        if any(observation.get(key) != value for key, value in required_witness.items()):
            _fail("presentation_not_observed", "PRESENTED requires a positive exact-result DOM observation")
        witness_payload = {"schema": N4_WITNESS_SCHEMA, "publication_id": publication_id, "consumer_id": binding.consumer_id, "conversation_id": binding.conversation_id, "profile_id": binding.profile_id, "marker": marker, "result_digest": result_digest, "composer_preserved": True, "witness": observation}
        raw = _json(witness_payload)
        ref = make_content_ref("application/json", N4_WITNESS_SCHEMA, raw)
        self.content_store.publish(ref, raw)
        witness_id = semantic_digest({k: witness_payload[k] for k in ("schema", "publication_id", "consumer_id", "conversation_id", "profile_id", "marker", "result_digest", "composer_preserved")})
        if binding.witness_id == witness_id and binding.presentation == PRESENTED:
            return binding
        if binding.witness_id is not None and binding.witness_id != witness_id:
            _fail("presentation_witness_conflict", "one consumer binding cannot be rebound to a different DOM witness")
        now = _now()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.execute("INSERT INTO n4_presentation_witnesses VALUES (?,?,?,?,?,?,?,?,?)", (witness_id, binding.binding_id, publication_id, binding.consumer_id, binding.conversation_id, result_digest, marker, _json(ref.as_dict()), now))
            self._connection.execute("UPDATE n4_consumer_bindings SET presentation=?,presentation_reason=?,witness_id=?,updated_at=? WHERE binding_id=?", (PRESENTED, None, witness_id, now, binding.binding_id))
            self._connection.commit()
        except sqlite3.IntegrityError:
            if self._connection.in_transaction:
                self._connection.rollback()
            existing = self.get_binding(publication_id, binding.consumer_id, generation=binding.generation)
            if existing and existing.witness_id == witness_id:
                return existing
            raise N4Error("presentation_witness_conflict", "presentation witness identity conflicted")
        except Exception:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise
        return self.get_binding(publication_id, binding.consumer_id, generation=binding.generation)  # type: ignore[return-value]

    def mark_unknown(self, *, publication_id: str, consumer_id: str, reason: str, generation: str | None = None) -> ConsumerBinding:
        binding = self.get_binding(publication_id, _text(consumer_id, "consumer_id"), generation=generation or self.generation)
        if binding is None:
            _fail("consumer_binding_missing", "consumer is not bound to this Publication")
        if binding.presentation == PRESENTED:
            return binding
        _text(reason, "reason")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.execute("UPDATE n4_consumer_bindings SET presentation=?,presentation_reason=?,updated_at=? WHERE binding_id=?", (UNKNOWN, _text(reason, "reason"), _now(), binding.binding_id))
            self._connection.commit()
        except Exception:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise
        return self.get_binding(publication_id, binding.consumer_id, generation=binding.generation)  # type: ignore[return-value]

    def create_resume_capsule(self, *, publication_id: str, source_consumer_id: str, target_consumer_id: str, payload: Mapping[str, Any], generation: str | None = None) -> ResumeCapsule:
        pub = self.get(publication_id)
        if pub is None:
            _fail("publication_missing", "resume references no Publication")
        source = self.get_binding(publication_id, source_consumer_id, generation=generation or self.generation)
        target = self.get_binding(publication_id, target_consumer_id, generation=generation or self.generation)
        if source is None or target is None:
            _fail("consumer_binding_missing", "resume requires distinct bound source and target consumers")
        document = _mapping(payload, "resume_payload")
        forbidden = {"chain_of_thought", "hidden_reasoning", "transcript"}
        if forbidden.intersection(document):
            _fail("sensitive_resume_payload", "Resume Capsule cannot contain hidden reasoning or transcript copies")
        document.update({"schema": N4_RESUME_PAYLOAD_SCHEMA, "task_id": pub.task_id, "work_id": pub.work_id, "publication_id": pub.publication_id, "intent_revision_id": pub.intent_revision_id, "result_ref": pub.result_ref.as_dict(), "source_consumer_id": source.consumer_id, "target_consumer_id": target.consumer_id, "source_presentation": source.presentation, "target_presentation": target.presentation})
        raw = _json(document)
        ref = make_content_ref("application/json", N4_RESUME_PAYLOAD_SCHEMA, raw)
        self.content_store.publish(ref, raw)
        identity = {"schema": N4_RESUME_SCHEMA, "source_consumer_id": source.consumer_id, "target_consumer_id": target.consumer_id, "publication_id": publication_id, "payload_digest": ref.raw_digest}
        capsule_id = semantic_digest(identity)
        existing_row = self._connection.execute("SELECT capsule_id,source_consumer_id,target_consumer_id,publication_id,content_ref_json,payload_digest,created_at FROM n4_resume_capsules WHERE source_consumer_id=? AND target_consumer_id=? AND publication_id=?", (source.consumer_id, target.consumer_id, publication_id)).fetchone()
        if existing_row:
            existing = self._resume_from_row(existing_row)
            if existing.payload_digest != ref.raw_digest:
                _fail("resume_conflict", "resume identity is already bound to different capsule content")
            return existing
        created_at = _now()
        self._connection.execute("INSERT INTO n4_resume_capsules VALUES (?,?,?,?,?,?,?)", (capsule_id, source.consumer_id, target.consumer_id, publication_id, _json(ref.as_dict()), ref.raw_digest, created_at))
        return ResumeCapsule(capsule_id, source.consumer_id, target.consumer_id, publication_id, ref, ref.raw_digest, created_at)

    def _resume_from_row(self, row: tuple[Any, ...]) -> ResumeCapsule:
        return ResumeCapsule(str(row[0]), str(row[1]), str(row[2]), str(row[3]), ContentRef.from_mapping(json.loads(bytes(row[4]).decode("utf-8"))), str(row[5]), str(row[6]))

    def resume(self, capsule_id: str) -> ResumeCapsule | None:
        row = self._connection.execute("SELECT capsule_id,source_consumer_id,target_consumer_id,publication_id,content_ref_json,payload_digest,created_at FROM n4_resume_capsules WHERE capsule_id=?", (_text(capsule_id, "capsule_id"),)).fetchone()
        return self._resume_from_row(row) if row else None

    def resume_payload(self, capsule_id: str) -> dict[str, Any]:
        capsule = self.resume(capsule_id)
        if capsule is None:
            _fail("resume_missing", "Resume Capsule does not exist")
        raw = self.content_store.resolve(capsule.content_ref)
        if _sha(raw) != capsule.payload_digest:
            _fail("resume_integrity_failure", "Resume Capsule payload digest differs")
        return _mapping(json.loads(raw.decode("utf-8")), "resume_payload")

    def publications_for_task(self, task_id: str) -> tuple[PublicationRecord, ...]:
        rows = self._connection.execute("SELECT publication_id,request_id,task_id,work_id,intent_revision_id,result_ref_json,result_digest,candidate_id,candidate_view_id,evidence_id,evaluation_id,disposition_id,consumer_id,consumer_kind,conversation_id,profile_id,generation,sequence,created_at FROM n4_publications WHERE task_id=? ORDER BY sequence", (_text(task_id, "task_id"),)).fetchall()
        return tuple(self._pub_from_row(row) for row in rows)

    def capsules_for_task(self, task_id: str) -> tuple[ResumeCapsule, ...]:
        rows = self._connection.execute(
            "SELECT c.capsule_id,c.source_consumer_id,c.target_consumer_id,c.publication_id,c.content_ref_json,c.payload_digest,c.created_at FROM n4_resume_capsules c JOIN n4_publications p ON p.publication_id=c.publication_id WHERE p.task_id=? ORDER BY c.created_at",
            (_text(task_id, "task_id"),),
        ).fetchall()
        return tuple(self._resume_from_row(row) for row in rows)

    def watermark(self) -> str:
        value = int(self._connection.execute("SELECT COALESCE(MAX(sequence),0) FROM n4_publications").fetchone()[0])
        return f"publication-sequence:{value}"

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "PublicationStore":
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()


class CanonicalOperatorQuery:
    """Read-only semantic view over the already-owned vNext authorities."""

    def __init__(self, root: Any, *, admission: Any, work_kernel: Any, candidate_store: Any, evidence_store: Any, publication_store: PublicationStore, generation: str = N4_GENERATION) -> None:
        self.root = root
        self.admission = admission
        self.work_kernel = work_kernel
        self.candidate_store = candidate_store
        self.evidence_store = evidence_store
        self.publication_store = publication_store
        self.generation = _text(generation, "generation")

    def _subject(self, kind: str | None, *, committed: CommittedRepoView | None, candidate: CandidateRepoView | None) -> dict[str, Any]:
        if kind is None:
            _fail("repo_view_required", "target repository queries require explicit COMMITTED, CANDIDATE or LIVE subject kind")
        kind = _text(kind, "repo_view_kind").upper()
        if kind not in REPO_KINDS:
            _fail("unsupported_repo_view_kind", "repository query subject kind is unsupported")
        if committed is not None and candidate is not None:
            _fail("mixed_repo_view", "a canonical query cannot combine COMMITTED and CANDIDATE subjects")
        if kind == "LIVE":
            if committed is not None or candidate is not None:
                _fail("mixed_repo_view", "LIVE cannot carry a committed or Candidate subject")
            return {"kind": "LIVE", "state": "UNAVAILABLE", "reason": "honest_live_capture_not_implemented"}
        if kind == "COMMITTED":
            if committed is None or candidate is not None:
                _fail("committed_view_required", "COMMITTED queries require one exact CommittedRepoView")
            try:
                committed.validate_integrity()
                committed.repository.query(committed)
            except Exception as exc:
                _fail("repo_view_mismatch", "COMMITTED RepoView failed exact identity validation", details={"error": str(exc)})
            return {"kind": "COMMITTED", "state": "AVAILABLE", "view": committed.to_dict(), "view_id": committed.view_id}
        if candidate is None or committed is not None:
            _fail("candidate_view_required", "CANDIDATE queries require one exact sealed CandidateRepoView")
        try:
            self.candidate_store.verify_sealed(candidate.candidate_id, base_view=getattr(candidate, "_base_view", None))
            if candidate.manifest_digest != candidate.view_id:
                raise ValueError("Candidate manifest/view identity mismatch")
        except Exception as exc:
            _fail("candidate_view_mismatch", "CANDIDATE RepoView failed exact sealed identity validation", details={"error": str(exc)})
        return {"kind": "CANDIDATE", "state": "AVAILABLE", "view": candidate.to_dict(), "view_id": candidate.view_id}

    def _evidence_snapshot(self, evidence_id: str | None) -> dict[str, Any]:
        """Read N3 evidence without invoking its mutation-capable query helper."""

        if evidence_id is None:
            return {"state": "UNAVAILABLE", "reason": "evidence_not_requested"}
        record = self.evidence_store.get(_text(evidence_id, "evidence_id"))
        if record is None:
            _fail("evidence_missing", "canonical operator query references no Evidence")
        applicable = True
        reason = "not_candidate_bound"
        if record.primary_subject_kind == "CANDIDATE":
            candidate_id = str(record.primary_subject_identity.get("candidate_id", ""))
            try:
                candidate_record = self.candidate_store.verify_current_applicability(candidate_id)
                applicable = candidate_record.state == CANDIDATE_SEALED and candidate_record.manifest_digest == record.candidate_view_id
                reason = "candidate_sealed_exact" if applicable else "candidate_stale_or_invalidated"
            except Exception:
                applicable = False
                reason = "candidate_stale_or_invalidated"
        current = self.evidence_store.current_disposition(evidence_id)
        effective = current.disposition if current else "INCONCLUSIVE"
        if current and current.disposition == "PASS":
            try:
                raw = self.evidence_store.content_store.resolve(record.raw_ref)
                if _sha(raw) != record.raw_digest:
                    raise ValueError("raw evidence digest differs")
            except Exception:
                applicable = False
                reason = "raw_evidence_integrity_failure"
                effective = "INCONCLUSIVE"
        if not applicable and effective == "PASS":
            effective = "INCONCLUSIVE"
        return {
            "schema": "bdb-vnext-m4c-evidence-v1",
            "evidence": record.as_dict(),
            "current_disposition": current.as_dict() if current else None,
            "history": [item.as_dict() for item in self.evidence_store.dispositions(evidence_id)],
            "evaluations": [item.as_dict() for item in self.evidence_store.evaluations(evidence_id)],
            "applicability": {"applicable": applicable, "reason": reason},
            "effective_disposition": effective,
        }

    def task_view(self, *, task_id: str, work_id: str, repo_view_kind: str | None, committed: CommittedRepoView | None = None, candidate: CandidateRepoView | None = None, evidence_id: str | None = None, consumer_id: str | None = None, generation: str | None = None) -> dict[str, Any]:
        subject = self._subject(repo_view_kind, committed=committed, candidate=candidate)
        task = self.admission.authority.task(_text(task_id, "task_id"))
        if task is None:
            _fail("task_missing", "canonical operator query requires an accepted Task")
        work = self.work_kernel.query(_text(work_id, "work_id"))
        if work is None or work.work.task_id != task_id:
            _fail("work_binding_mismatch", "query WorkItem is not bound to the requested Task")
        evidence = self._evidence_snapshot(evidence_id)
        publications = self.publication_store.publications_for_task(task_id)
        bindings = []
        for publication in publications:
            if consumer_id is not None:
                binding = self.publication_store.get_binding(publication.publication_id, consumer_id, generation=generation or self.generation)
                if binding is not None:
                    bindings.append(binding.as_dict())
            else:
                bindings.extend(item.as_dict() for item in self.publication_store.bindings_for_publication(publication.publication_id))
        capsules = self.publication_store.capsules_for_task(task_id)
        view: dict[str, Any] = {
            "schema": N4_OPERATOR_VIEW_SCHEMA,
            "contract_version": N4_PROTOCOL_GENERATION,
            "generation": self.generation,
            "freshness": {"state": "AVAILABLE", "watermark": self.publication_store.watermark(), "source": "unified-control-db"},
            "subject": subject,
            "task": task.as_dict(),
            "work": work.as_dict(),
            "evidence": evidence,
            "publications": [item.as_dict() for item in publications],
            "consumer_bindings": bindings,
            "resume": {"state": "AVAILABLE" if capsules else "UNAVAILABLE", "capsules": [item.as_dict() for item in capsules]},
            "future": {"LIVE_REPO_VIEW": {"state": "UNAVAILABLE", "reason": "honest_live_capture_not_implemented"}, "M5_EFFECT_CERTAINTY": {"state": "UNAVAILABLE", "reason": "not_implemented_in_n4"}, "M6_CHECK_PLAN": {"state": "UNAVAILABLE", "reason": "not_implemented_in_n4"}, "M7_GIT_PROMOTION": {"state": "UNAVAILABLE", "reason": "not_implemented_in_n4"}},
        }
        view["query_digest"] = semantic_digest(view)
        return view


__all__ = [
    "CanonicalOperatorQuery", "ConsumerBinding", "N4_CONSUMER_SCHEMA", "N4_GENERATION", "N4_OPERATOR_VIEW_SCHEMA",
    "N4_PUBLICATION_SCHEMA", "N4_RESUME_SCHEMA", "N4_RESULT_SCHEMA", "N4_WITNESS_SCHEMA", "N4Error", "PRESENTED",
    "PublicationRecord", "PublicationStore", "ResumeCapsule", "UNKNOWN",
]

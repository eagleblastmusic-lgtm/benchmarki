"""N3 exact evidence integrity store and bounded Candidate checker."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from bdb_shared.evidence import canonical_json_bytes, semantic_digest
from bdb_vnext.candidate import CandidateRepoView, CandidateError, CANDIDATE_SEALED
from bdb_vnext.content_store import ContentRef, ImmutableContentStore, make_content_ref
from bdb_vnext.control_store import assert_database_path, configure_connection, ensure_identity
from bdb_vnext.m4c_environment import CheckerEnvironment

EVIDENCE_SCHEMA = "bdb-vnext-m4c-evidence-v1"
EVALUATION_SCHEMA = "bdb-vnext-m4c-evaluation-v1"
DISPOSITION_SCHEMA = "bdb-vnext-m4c-disposition-v1"
GAP_SCHEMA = "bdb-vnext-m4c-evidence-gap-v1"
RAW_SCHEMA = "m4c-raw-observation-v1"
CHECKER_ID = "bdb-vnext-m4c-minimum-candidate-checker"
CHECKER_VERSION = "1"
EVALUATION_RESULTS = {"PASS", "FAIL", "INCONCLUSIVE", "INVALID"}
APPLICABILITY = {"APPLICABLE", "INCONCLUSIVE", "INVALID"}

class EvidenceError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message); self.code = code; self.details = dict(details or {})

def _fail(code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
    raise EvidenceError(code, message, details=details)

def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()

def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def _text(value: object, field: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        _fail("invalid_evidence_identity", f"{field} must be bounded text")
    return value

def _json(value: Mapping[str, Any]) -> bytes:
    return canonical_json_bytes(value)

def _checker_code_digest(project_root: Path) -> str:
    try:
        return _digest((project_root / "bdb_vnext" / "m4c_evidence.py").read_bytes())
    except OSError as exc:
        raise EvidenceError("checker_identity_unavailable", "checker source cannot be hashed") from exc

@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str; request_id: str; primary_subject_kind: str; primary_subject_identity: Mapping[str, Any]; candidate_view_id: str | None; raw_ref: ContentRef; raw_digest: str; checker_id: str; checker_version: str; checker_code_digest: str; environment: Mapping[str, Any]; observation_started_at: str; observation_finished_at: str; completeness: str; applicability: str; status: str; created_at: str
    def as_dict(self) -> dict[str, Any]:
        return {"schema": EVIDENCE_SCHEMA, "evidence_id": self.evidence_id, "request_id": self.request_id, "primary_subject_kind": self.primary_subject_kind, "primary_subject_identity": dict(self.primary_subject_identity), "candidate_view_id": self.candidate_view_id, "raw_ref": self.raw_ref.as_dict(), "raw_digest": self.raw_digest, "checker_id": self.checker_id, "checker_version": self.checker_version, "checker_code_digest": self.checker_code_digest, "environment": dict(self.environment), "observation_started_at": self.observation_started_at, "observation_finished_at": self.observation_finished_at, "completeness": self.completeness, "applicability": self.applicability, "status": self.status, "created_at": self.created_at}

@dataclass(frozen=True)
class EvaluationRecord:
    evaluation_id: str; evidence_id: str; evaluator_id: str; evaluator_version: str; evaluator_code_digest: str; config_digest: str; result: str; applicability: str; detail: Mapping[str, Any]; created_at: str
    def as_dict(self) -> dict[str, Any]:
        return {"schema": EVALUATION_SCHEMA, "evaluation_id": self.evaluation_id, "evidence_id": self.evidence_id, "evaluator_id": self.evaluator_id, "evaluator_version": self.evaluator_version, "evaluator_code_digest": self.evaluator_code_digest, "config_digest": self.config_digest, "result": self.result, "applicability": self.applicability, "detail": dict(self.detail), "created_at": self.created_at}

@dataclass(frozen=True)
class DispositionRecord:
    disposition_id: str; evidence_id: str; evaluation_id: str; disposition: str; supersedes: str | None; created_at: str
    def as_dict(self) -> dict[str, Any]:
        return {"schema": DISPOSITION_SCHEMA, "disposition_id": self.disposition_id, "evidence_id": self.evidence_id, "evaluation_id": self.evaluation_id, "disposition": self.disposition, "supersedes": self.supersedes, "created_at": self.created_at}

@dataclass(frozen=True)
class EvidenceGap:
    gap_id: str; primary_subject_kind: str; primary_subject_identity: Mapping[str, Any]; reason: str; details: Mapping[str, Any]; created_at: str
    def as_dict(self) -> dict[str, Any]:
        return {"schema": GAP_SCHEMA, "gap_id": self.gap_id, "primary_subject_kind": self.primary_subject_kind, "primary_subject_identity": dict(self.primary_subject_identity), "reason": self.reason, "details": dict(self.details), "created_at": self.created_at}


class EvidenceStore:
    """Canonical N3 evidence repository over the unified Control DB."""

    def __init__(self, root: str | Path, *, content_store: ImmutableContentStore | None = None, candidate_store: Any | None = None) -> None:
        self.root = Path(root).expanduser().absolute()
        self.content_store = content_store or ImmutableContentStore(self.root)
        self.database_path = assert_database_path(self.root, self.root / "control" / "control.db")
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.database_path), timeout=0.25, isolation_level=None)
        configure_connection(self._connection); ensure_identity(self._connection)
        self.candidate_store = candidate_store; self._ensure_schema()

    def _ensure_schema(self) -> None:
        self._connection.executescript("""
        CREATE TABLE IF NOT EXISTS m4c_evidence_records (
          evidence_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, primary_subject_kind TEXT NOT NULL,
          primary_subject_identity_json BLOB NOT NULL, candidate_view_id TEXT, raw_ref_json BLOB NOT NULL,
          raw_digest TEXT NOT NULL, checker_id TEXT NOT NULL, checker_version TEXT NOT NULL,
          checker_code_digest TEXT NOT NULL, environment_json BLOB NOT NULL, observation_started_at TEXT NOT NULL,
          observation_finished_at TEXT NOT NULL, completeness TEXT NOT NULL, applicability TEXT NOT NULL,
          status TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS m4c_evaluations (
          evaluation_id TEXT PRIMARY KEY, evidence_id TEXT NOT NULL REFERENCES m4c_evidence_records(evidence_id),
          evaluator_id TEXT NOT NULL, evaluator_version TEXT NOT NULL, evaluator_code_digest TEXT NOT NULL,
          config_digest TEXT NOT NULL, result TEXT NOT NULL, applicability TEXT NOT NULL,
          detail_json BLOB NOT NULL, created_at TEXT NOT NULL,
          UNIQUE(evidence_id,evaluator_id,evaluator_version,evaluator_code_digest,config_digest));
        CREATE TABLE IF NOT EXISTS m4c_dispositions (
          disposition_id TEXT PRIMARY KEY, evidence_id TEXT NOT NULL REFERENCES m4c_evidence_records(evidence_id),
          evaluation_id TEXT NOT NULL REFERENCES m4c_evaluations(evaluation_id), disposition TEXT NOT NULL,
          supersedes TEXT, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS m4c_disposition_heads (
          evidence_id TEXT PRIMARY KEY REFERENCES m4c_evidence_records(evidence_id),
          disposition_id TEXT NOT NULL REFERENCES m4c_dispositions(disposition_id));
        CREATE TABLE IF NOT EXISTS m4c_evidence_gaps (
          gap_id TEXT PRIMARY KEY, primary_subject_kind TEXT NOT NULL, primary_subject_identity_json BLOB NOT NULL,
          reason TEXT NOT NULL, details_json BLOB NOT NULL, created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS m4c_evidence_by_subject ON m4c_evidence_records(primary_subject_kind,candidate_view_id);
        """)

    def _record_from_row(self, row: tuple[Any, ...]) -> EvidenceRecord:
        return EvidenceRecord(str(row[0]),str(row[1]),str(row[2]),json.loads(bytes(row[3]).decode()),str(row[4]) if row[4] else None,ContentRef.from_mapping(json.loads(bytes(row[5]).decode())),str(row[6]),str(row[7]),str(row[8]),str(row[9]),json.loads(bytes(row[10]).decode()),str(row[11]),str(row[12]),str(row[13]),str(row[14]),str(row[15]),str(row[16]))

    def get(self, evidence_id: str, *, connection: sqlite3.Connection | None = None) -> EvidenceRecord | None:
        source = connection or self._connection
        row = source.execute("SELECT evidence_id,request_id,primary_subject_kind,primary_subject_identity_json,candidate_view_id,raw_ref_json,raw_digest,checker_id,checker_version,checker_code_digest,environment_json,observation_started_at,observation_finished_at,completeness,applicability,status,created_at FROM m4c_evidence_records WHERE evidence_id=?",(evidence_id,)).fetchone()
        return self._record_from_row(row) if row else None

    def _existing_request(self, request_id: str) -> EvidenceRecord | None:
        row = self._connection.execute("SELECT evidence_id,request_id,primary_subject_kind,primary_subject_identity_json,candidate_view_id,raw_ref_json,raw_digest,checker_id,checker_version,checker_code_digest,environment_json,observation_started_at,observation_finished_at,completeness,applicability,status,created_at FROM m4c_evidence_records WHERE request_id=?",(request_id,)).fetchone()
        return self._record_from_row(row) if row else None

    def record_observation(self, *, request_id: str, primary_subject_kind: str, primary_subject_identity: Mapping[str, Any], candidate_view_id: str | None, raw_observation: Mapping[str, Any], checker_id: str, checker_version: str, checker_code_digest: str, environment: Mapping[str, Any], observation_started_at: str, observation_finished_at: str, completeness: str, applicability: str, status: str) -> EvidenceRecord:
        request_id = _text(request_id,"request_id"); existing = self._existing_request(request_id)
        if existing:
            same_request = (
                existing.primary_subject_kind == primary_subject_kind
                and dict(existing.primary_subject_identity) == dict(primary_subject_identity)
                and existing.candidate_view_id == candidate_view_id
                and existing.checker_id == checker_id
                and existing.checker_version == checker_version
                and existing.checker_code_digest == checker_code_digest
                and dict(existing.environment) == dict(environment)
                and existing.completeness == completeness
                and existing.applicability == applicability
                and existing.status == status
            )
            if not same_request:
                _fail("evidence_request_conflict", "request_id is already bound to different evidence inputs")
            return existing
        if primary_subject_kind == "CANDIDATE":
            subject_view_id = primary_subject_identity.get("view_id") or primary_subject_identity.get("manifest_digest")
            if not isinstance(subject_view_id, str) or candidate_view_id != subject_view_id:
                _fail("subject_binding_mismatch", "Candidate evidence must bind the exact Candidate view identity")
        raw = _json(dict(raw_observation)); ref = make_content_ref("application/json",RAW_SCHEMA,raw); self.content_store.publish(ref,raw)
        identity = {"schema":EVIDENCE_SCHEMA,"request_id":request_id,"primary_subject_kind":_text(primary_subject_kind,"primary_subject_kind"),"primary_subject_identity":dict(primary_subject_identity),"candidate_view_id":candidate_view_id,"raw_digest":ref.raw_digest,"checker_id":_text(checker_id,"checker_id"),"checker_version":_text(checker_version,"checker_version"),"checker_code_digest":_text(checker_code_digest,"checker_code_digest"),"environment":dict(environment),"observation_started_at":_text(observation_started_at,"observation_started_at"),"observation_finished_at":_text(observation_finished_at,"observation_finished_at"),"completeness":_text(completeness,"completeness"),"applicability":_text(applicability,"applicability"),"status":_text(status,"status")}
        evidence_id = semantic_digest(identity); created_at = _now()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.execute("INSERT INTO m4c_evidence_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(evidence_id,request_id,primary_subject_kind,_json(dict(primary_subject_identity)),candidate_view_id,_json(ref.as_dict()),ref.raw_digest,checker_id,checker_version,checker_code_digest,_json(dict(environment)),observation_started_at,observation_finished_at,completeness,applicability,status,created_at)); self._connection.commit()
        except sqlite3.IntegrityError:
            self._connection.rollback(); existing=self._existing_request(request_id)
            if existing: return existing
            raise EvidenceError("evidence_identity_conflict","evidence request identity conflicted")
        except Exception:
            self._connection.rollback(); raise
        return self.get(evidence_id)  # type: ignore[return-value]

    def raw_observation(self, evidence_id: str) -> bytes:
        record=self.get(evidence_id)
        if record is None: _fail("evidence_missing","evidence does not exist")
        try: raw=self.content_store.resolve(record.raw_ref)
        except EvidenceError:
            raise
        except Exception as exc:
            code = getattr(exc, "code", None)
            mapped = "raw_evidence_integrity_failure" if code in {"raw_integrity_failure", "semantic_integrity_failure"} else "raw_evidence_unavailable"
            raise EvidenceError(mapped,"immutable raw observation is unavailable or failed integrity verification") from exc
        if _digest(raw)!=record.raw_digest: _fail("raw_evidence_integrity_failure","raw observation digest differs")
        return raw

    def _evaluation_from_row(self,row:tuple[Any,...])->EvaluationRecord:
        return EvaluationRecord(str(row[0]),str(row[1]),str(row[2]),str(row[3]),str(row[4]),str(row[5]),str(row[6]),str(row[7]),json.loads(bytes(row[8]).decode()),str(row[9]))

    def evaluations(self,evidence_id:str)->tuple[EvaluationRecord,...]:
        rows=self._connection.execute("SELECT evaluation_id,evidence_id,evaluator_id,evaluator_version,evaluator_code_digest,config_digest,result,applicability,detail_json,created_at FROM m4c_evaluations WHERE evidence_id=? ORDER BY rowid",(evidence_id,)).fetchall(); return tuple(self._evaluation_from_row(row) for row in rows)

    def _disposition_from_row(self,row:tuple[Any,...])->DispositionRecord:
        return DispositionRecord(str(row[0]),str(row[1]),str(row[2]),str(row[3]),str(row[4]) if row[4] else None,str(row[5]))

    def dispositions(self,evidence_id:str)->tuple[DispositionRecord,...]:
        rows=self._connection.execute("SELECT disposition_id,evidence_id,evaluation_id,disposition,supersedes,created_at FROM m4c_dispositions WHERE evidence_id=? ORDER BY rowid",(evidence_id,)).fetchall(); return tuple(self._disposition_from_row(row) for row in rows)

    def current_disposition(self,evidence_id:str,*,connection:sqlite3.Connection|None=None)->DispositionRecord|None:
        source=connection or self._connection
        row=source.execute("SELECT d.disposition_id,d.evidence_id,d.evaluation_id,d.disposition,d.supersedes,d.created_at FROM m4c_dispositions d JOIN m4c_disposition_heads h ON h.disposition_id=d.disposition_id WHERE h.evidence_id=?",(evidence_id,)).fetchone(); return self._disposition_from_row(row) if row else None

    def _candidate_applicable(self,record:EvidenceRecord,*,connection:sqlite3.Connection|None=None)->tuple[bool,str]:
        if record.primary_subject_kind!="CANDIDATE": return True,"not_candidate_bound"
        if self.candidate_store is None: return False,"candidate_authority_unavailable"
        candidate_id=str(record.primary_subject_identity.get("candidate_id",""))
        try:
            current=self.candidate_store.verify_current_applicability(candidate_id,connection=connection)
        except Exception:
            return False,"candidate_stale_or_invalidated"
        if current is None or current.state!=CANDIDATE_SEALED or current.manifest_digest!=record.candidate_view_id: return False,"candidate_stale_or_invalidated"
        return True,"candidate_sealed_exact"

    def authorize_current(
        self,
        evidence_id:str,
        *,
        candidate_view_id:str|None,
        evaluation_id:str|None,
        disposition_id:str|None,
        connection:sqlite3.Connection|None=None,
    )->dict[str,Any]:
        """Return one exact positive authorization snapshot for Publication.

        When ``connection`` is the Publication writer connection, every Control
        DB component is read from that transaction snapshot. Candidate payload
        authority is immutable CAS/Git-bundle content bound by its manifest.
        """

        source=connection or self._connection
        record=self.get(evidence_id,connection=source)
        if record is None: _fail("evidence_missing","Publication evidence binding does not exist")
        if candidate_view_id is not None and record.candidate_view_id!=candidate_view_id:
            _fail("evidence_binding_mismatch","Publication Evidence is bound to a different Candidate view")
        applicable,reason=self._candidate_applicable(record,connection=source)
        if not applicable:
            _fail("evidence_not_applicable","Publication Evidence Candidate is not currently applicable",details={"reason":reason})
        self.raw_observation(evidence_id)
        current=self.current_disposition(evidence_id,connection=source)
        if current is None or current.disposition not in {"PASS","FAIL"}:
            _fail("evidence_not_applicable","Publication Evidence has no positively applicable current disposition")
        row=source.execute("SELECT evaluation_id,evidence_id,evaluator_id,evaluator_version,evaluator_code_digest,config_digest,result,applicability,detail_json,created_at FROM m4c_evaluations WHERE evaluation_id=?",(current.evaluation_id,)).fetchone()
        if row is None: _fail("evaluation_missing","current Evidence evaluation does not exist")
        evaluation=self._evaluation_from_row(row)
        if evaluation.applicability!="APPLICABLE" or evaluation.result not in {"PASS","FAIL"} or evaluation.result!=current.disposition:
            _fail("evidence_not_applicable","current Evidence evaluation lacks positive applicability")
        if evaluation_id is not None and evaluation.evaluation_id!=evaluation_id:
            _fail("evaluation_binding_mismatch","Publication evaluation is not current")
        if disposition_id is not None and current.disposition_id!=disposition_id:
            _fail("disposition_binding_mismatch","Publication disposition is not current")
        snapshot={"schema":"bdb-vnext-m4c-applicability-authorization-v1","evidence_id":record.evidence_id,"raw_digest":record.raw_digest,"candidate_view_id":record.candidate_view_id,"evaluation_id":evaluation.evaluation_id,"disposition_id":current.disposition_id,"disposition":current.disposition}
        snapshot["authorization_digest"]=semantic_digest(snapshot)
        return snapshot

    def query(self,evidence_id:str)->dict[str,Any]:
        record=self.get(evidence_id)
        if record is None: _fail("evidence_missing","evidence does not exist")
        current=self.current_disposition(evidence_id); applicable,reason=self._candidate_applicable(record); effective=current.disposition if current else "INCONCLUSIVE"
        if current and current.disposition=="PASS":
            try:
                self.raw_observation(evidence_id)
            except EvidenceError as exc:
                applicable=False
                reason=exc.code
                effective="INCONCLUSIVE"
        if not applicable and effective=="PASS": effective="INCONCLUSIVE"
        return {"schema":EVIDENCE_SCHEMA,"evidence":record.as_dict(),"current_disposition":current.as_dict() if current else None,"history":[item.as_dict() for item in self.dispositions(evidence_id)],"evaluations":[item.as_dict() for item in self.evaluations(evidence_id)],"applicability":{"applicable":applicable,"reason":reason},"effective_disposition":effective}

    def _normalize_evaluation(self,record:EvidenceRecord,*,requested_result:str,requested_applicability:str,requested_detail:Mapping[str,Any],connection:sqlite3.Connection|None=None)->tuple[str,str,dict[str,Any],bool]:
        result,applicability,detail=requested_result,requested_applicability,dict(requested_detail)
        candidate_ok,reason=self._candidate_applicable(record,connection=connection)
        if result=="PASS" and (applicability!="APPLICABLE" or not candidate_ok or record.completeness!="COMPLETE"):
            result,applicability="INCONCLUSIVE","INCONCLUSIVE"
            detail={**detail,"fail_closed_reason":reason if not candidate_ok else "positive_applicability_not_established"}
        if result=="PASS":
            try:
                self.raw_observation(record.evidence_id)
            except EvidenceError as exc:
                result,applicability="INCONCLUSIVE","INCONCLUSIVE"
                detail={**detail,"fail_closed_reason":exc.code}
        return result,applicability,detail,candidate_ok

    def evaluate(self,*,evidence_id:str,evaluator_id:str,evaluator_version:str,evaluator_code_digest:str,config_digest:str,result:str,applicability:str,detail:Mapping[str,Any],supersedes_evaluation_id:str|None=None,fault:str|None=None)->EvaluationRecord:
        record=self.get(evidence_id)
        if record is None: _fail("evidence_missing","evidence does not exist")
        if result not in EVALUATION_RESULTS or applicability not in APPLICABILITY: _fail("evaluation_contract_invalid","evaluation result/applicability is unsupported")
        requested_result,result_applicability,requested_detail=result,applicability,dict(detail)
        # Preflight is diagnostic/early-fail evidence only. It is deliberately
        # not used as authority for the durable evaluation below.
        self._normalize_evaluation(record,requested_result=requested_result,requested_applicability=result_applicability,requested_detail=requested_detail)
        if fault=="before_evaluation_commit": _fail("evaluation_commit_interrupted","evaluation interrupted before durable commit")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            locked_record=self.get(evidence_id,connection=self._connection)
            if locked_record is None: _fail("evidence_missing","evidence does not exist")
            result,applicability,detail,candidate_ok=self._normalize_evaluation(locked_record,requested_result=requested_result,requested_applicability=result_applicability,requested_detail=requested_detail,connection=self._connection)
            identity={"schema":EVALUATION_SCHEMA,"evidence_id":evidence_id,"evaluator_id":evaluator_id,"evaluator_version":evaluator_version,"evaluator_code_digest":evaluator_code_digest,"config_digest":config_digest,"result":result,"applicability":applicability,"detail":dict(detail)}
            evaluation_id=semantic_digest(identity); created_at=_now()
            existing=self._connection.execute("SELECT evaluation_id,evidence_id,evaluator_id,evaluator_version,evaluator_code_digest,config_digest,result,applicability,detail_json,created_at FROM m4c_evaluations WHERE evidence_id=? AND evaluator_id=? AND evaluator_version=? AND evaluator_code_digest=? AND config_digest=?",(evidence_id,evaluator_id,evaluator_version,evaluator_code_digest,config_digest)).fetchone()
            if existing:
                existing_record=self._evaluation_from_row(existing)
                if existing_record.result != result or existing_record.applicability != applicability or dict(existing_record.detail) != dict(detail):
                    _fail("evaluation_identity_conflict", "evaluator identity is already bound to a different result")
                self._connection.rollback()
                return existing_record
            previous=self.current_disposition(evidence_id,connection=self._connection)
            supersedes=supersedes_evaluation_id or (previous.evaluation_id if previous else None)
            if supersedes_evaluation_id is not None and (previous is None or supersedes_evaluation_id != previous.evaluation_id):
                _fail("supersession_target_invalid", "new evaluation must supersede the current disposition head")
            disposition=result if applicability=="APPLICABLE" and result in {"PASS","FAIL"} and candidate_ok else "INCONCLUSIVE" if result=="PASS" else result
            disposition_id=semantic_digest({"schema":DISPOSITION_SCHEMA,"evidence_id":evidence_id,"evaluation_id":evaluation_id,"disposition":disposition,"supersedes":supersedes})
            self._connection.execute("INSERT INTO m4c_evaluations VALUES (?,?,?,?,?,?,?,?,?,?)",(evaluation_id,evidence_id,evaluator_id,evaluator_version,evaluator_code_digest,config_digest,result,applicability,_json(dict(detail)),created_at))
            self._connection.execute("INSERT INTO m4c_dispositions VALUES (?,?,?,?,?,?)",(disposition_id,evidence_id,evaluation_id,disposition,supersedes,created_at))
            self._connection.execute("INSERT INTO m4c_disposition_heads VALUES (?,?) ON CONFLICT(evidence_id) DO UPDATE SET disposition_id=excluded.disposition_id",(evidence_id,disposition_id))
            self._connection.commit()
        except Exception:
            if self._connection.in_transaction: self._connection.rollback()
            raise
        if fault=="after_evaluation_commit": raise EvidenceError("evaluation_response_lost","evaluation committed before response",details={"evaluation_id":evaluation_id})
        return self._evaluation_from_row(self._connection.execute("SELECT evaluation_id,evidence_id,evaluator_id,evaluator_version,evaluator_code_digest,config_digest,result,applicability,detail_json,created_at FROM m4c_evaluations WHERE evaluation_id=?",(evaluation_id,)).fetchone())

    def record_gap(self,*,primary_subject_kind:str,primary_subject_identity:Mapping[str,Any],reason:str,details:Mapping[str,Any])->EvidenceGap:
        identity={"schema":GAP_SCHEMA,"primary_subject_kind":_text(primary_subject_kind,"primary_subject_kind"),"primary_subject_identity":dict(primary_subject_identity),"reason":_text(reason,"reason"),"details":dict(details)}; gap_id=semantic_digest(identity); existing=self._connection.execute("SELECT gap_id,primary_subject_kind,primary_subject_identity_json,reason,details_json,created_at FROM m4c_evidence_gaps WHERE gap_id=?",(gap_id,)).fetchone()
        if existing: return EvidenceGap(str(existing[0]),str(existing[1]),json.loads(bytes(existing[2]).decode()),str(existing[3]),json.loads(bytes(existing[4]).decode()),str(existing[5]))
        created_at=_now(); self._connection.execute("INSERT INTO m4c_evidence_gaps VALUES (?,?,?,?,?,?)",(gap_id,primary_subject_kind,_json(dict(primary_subject_identity)),reason,_json(dict(details)),created_at)); return EvidenceGap(gap_id,primary_subject_kind,dict(primary_subject_identity),reason,dict(details),created_at)

    def close(self)->None: self._connection.close()
    def __enter__(self)->"EvidenceStore": return self
    def __exit__(self,*_args:object)->None: self.close()


class MinimumCandidateChecker:
    """One real, narrow checker for a sealed Candidate RepoView."""
    def __init__(self,project_root:str|Path,evidence:EvidenceStore)->None:
        self.project_root=Path(project_root).expanduser().absolute(); self.evidence=evidence; self.environment=CheckerEnvironment.expected(self.project_root); self.code_digest=_checker_code_digest(self.project_root)

    def _probe(self,*,fault:str|None=None)->dict[str,Any]:
        if fault=="before_spawn": _fail("checker_not_started","checker child was not spawned")
        if fault=="interpreter_missing": _fail("interpreter_unavailable","expected checker interpreter is unavailable")
        if fault in {"dependency_failure","process_crash","timeout","malformed_output"}: return {"status":"INCONCLUSIVE","reason":fault}
        try:
            completed=subprocess.run([self.environment.interpreter_path,"-m","bdb_vnext.m4c_evidence","--probe",str(self.project_root)],shell=False,capture_output=True,text=True,env=self.environment.child_environment(),timeout=20,check=False)
        except (OSError,subprocess.SubprocessError) as exc: return {"status":"INCONCLUSIVE","reason":type(exc).__name__}
        if completed.returncode!=0: return {"status":"INCONCLUSIVE","reason":"checker_process_failed","returncode":completed.returncode}
        try: observed=json.loads(completed.stdout)
        except json.JSONDecodeError: return {"status":"INCONCLUSIVE","reason":"malformed_output"}
        if observed!=self.environment.as_dict(): return {"status":"INCONCLUSIVE","reason":"environment_mismatch","observed":observed,"expected":self.environment.as_dict()}
        return {"status":"READY","environment":observed}

    def check(self,candidate:CandidateRepoView,*,request_id:str,evaluator_id:str="m4c-exact-candidate-evaluator",evaluator_version:str="1",config_digest:str="sha256:"+"0"*64,fault:str|None=None)->EvaluationRecord:
        if not isinstance(candidate,CandidateRepoView): _fail("unsupported_subject","minimum checker accepts only Candidate RepoView")
        started=_now(); subject={"candidate_id":candidate.candidate_id,"view_id":candidate.view_id,"manifest_digest":candidate.manifest_digest,"candidate_tree_digest":candidate.candidate_tree_digest,"base_view_id":candidate.base_view_id,"repository_id":candidate.repository_id}; probe=self._probe(fault=fault); raw={"schema":RAW_SCHEMA,"checker_id":CHECKER_ID,"checker_version":CHECKER_VERSION,"subject":subject,"environment_probe":probe}; applicability="APPLICABLE" if probe.get("status")=="READY" else "INCONCLUSIVE"; completeness="COMPLETE" if applicability=="APPLICABLE" else "INCOMPLETE"; status="CHECKED" if applicability=="APPLICABLE" else "UNAVAILABLE"
        if applicability=="APPLICABLE":
            try:
                # Verify the mutable Candidate workspace once before and once
                # after reading the full tree.  Calling CandidateRepoView
                # ``read_bytes`` for every unchanged base path would rescan a
                # large Windows worktree for each file (quadratic and prone to
                # timeout), while the seal/manifest plus the two bounded scans
                # provide the same exactness boundary.
                candidate._store.verify_sealed(candidate.candidate_id,base_view=candidate._base_view); entries=[]
                for entry in candidate.list_entries():
                    planned = next((item for item in candidate.path_bindings if item.path == entry.path), None)
                    if planned is not None:
                        payload = candidate._store.content_store.resolve(planned.after_ref)
                        entries.append({"path":entry.path,"object_oid":entry.object_oid,"size_bytes":len(payload),"raw_digest":_digest(payload),"content_source":"candidate_cas"})
                    else:
                        # Unchanged bytes are already bound by the exact
                        # committed tree identity.  Do not invoke one Git
                        # subprocess per path for a large repository; retain
                        # the object identity and let the surrounding seal
                        # verification prove the complete tree.
                        entries.append({"path":entry.path,"object_oid":entry.object_oid,"size_bytes":entry.size_bytes,"raw_digest":None,"content_source":"committed_tree"})
                candidate._store.verify_sealed(candidate.candidate_id,base_view=candidate._base_view)
                raw["observation"]={"status":"EXACT_CANDIDATE_READABLE","entries":entries,"candidate_tree_digest":candidate.candidate_tree_digest}
            except (CandidateError,EvidenceError) as exc: applicability,completeness,status="INCONCLUSIVE","INCOMPLETE","UNAVAILABLE"; raw["observation"]={"status":"INCONCLUSIVE","reason":getattr(exc,"code",type(exc).__name__)}
        observation=self.evidence.record_observation(request_id=request_id,primary_subject_kind="CANDIDATE",primary_subject_identity=subject,candidate_view_id=candidate.view_id,raw_observation=raw,checker_id=CHECKER_ID,checker_version=CHECKER_VERSION,checker_code_digest=self.code_digest,environment=self.environment.as_dict(),observation_started_at=started,observation_finished_at=_now(),completeness=completeness,applicability=applicability,status=status)
        if fault=="raw_only": _fail("evaluation_not_committed","raw observation committed without evaluation",details={"evidence_id":observation.evidence_id})
        result="PASS" if applicability=="APPLICABLE" and status=="CHECKED" else "INCONCLUSIVE"; detail={"checker_status":status,"raw_digest":observation.raw_digest,"subject_match":True}; return self.evidence.evaluate(evidence_id=observation.evidence_id,evaluator_id=evaluator_id,evaluator_version=evaluator_version,evaluator_code_digest=self.code_digest,config_digest=config_digest,result=result,applicability=applicability,detail=detail,fault="after_evaluation_commit" if fault=="lost_response" else None)

def _probe_main(project_root:str)->int:
    print(json.dumps(CheckerEnvironment.observed(project_root).as_dict(),sort_keys=True)); return 0

if __name__=="__main__" and len(sys.argv)>=2 and sys.argv[1]=="--probe": raise SystemExit(_probe_main(sys.argv[2]))

__all__=["DISPOSITION_SCHEMA","EVIDENCE_SCHEMA","EVALUATION_SCHEMA","EvidenceError","EvidenceGap","EvidenceRecord","EvidenceStore","EvaluationRecord","DispositionRecord","MinimumCandidateChecker"]

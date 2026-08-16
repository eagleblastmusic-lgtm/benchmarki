"""M6a promotion-grade evidence core and semantic policy gate over M4c EvidenceStore."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Mapping, Sequence

from bdb_shared.evidence import canonical_json_bytes, semantic_digest
from bdb_vnext.control_store import (
    ControlStoreError,
    begin_control_write,
    commit_control_write,
    rollback_control_write,
)
from bdb_vnext.m4c_evidence import (
    EvidenceStore,
    EvidenceError,
)

OBLIGATION_SCHEMA = "bdb-vnext-m6a-obligation-v1"
ASSESSMENT_SCHEMA = "bdb-vnext-m6a-assessment-v1"
WAIVER_DECISION_SCHEMA = "bdb-vnext-m6a-waiver-decision-v1"
APPROVAL_SCHEMA = "bdb-vnext-m6a-approval-v1"
GATE_SCHEMA = "bdb-vnext-m6a-gate-v1"
OBLIGATION_QUERY_SCHEMA = "bdb-vnext-m6a-obligation-query-v1"
ENVIRONMENT_FINGERPRINT_SCHEMA = "bdb-vnext-m6a-environment-fingerprint-v1"
SUBJECT_SCHEMA = "bdb-vnext-m6a-subject-v1"

Waivability = Literal["NEVER", "AUTHORIZED_USER", "ADMIN_ONLY"]
AssessmentStatus = Literal["SATISFIED", "UNSATISFIED", "UNKNOWN", "STALE"]
Applicability = Literal["APPLICABLE", "NOT_APPLICABLE", "UNKNOWN"]
Verdict = Literal["PASS", "FAIL", "NOT_APPLICABLE", "UNKNOWN"]
WaiverAuthority = Literal["USER", "ADMIN"]

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class M6aError(RuntimeError):
    """Bounded, machine-readable M6a evidence policy failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def _fail(
    code: str,
    message: str,
    *,
    details: Mapping[str, Any] | None = None,
) -> None:
    raise M6aError(code, message, details=details)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_digest(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.match(value):
        _fail(
            "invalid_digest_format",
            f"{field_name} must be exact lowercase sha256:<64 hex>",
            details={"field": field_name, "value": value},
        )
    return value


def compute_subject_digest(subject_kind: str, subject_identity: Mapping[str, Any]) -> str:
    payload = {
        "schema": SUBJECT_SCHEMA,
        "subject_kind": str(subject_kind),
        "subject_identity": dict(subject_identity),
    }
    return semantic_digest(payload)


@dataclass(frozen=True)
class ObligationRecord:
    obligation_id: str
    subject_kind: str
    subject_identity: Mapping[str, Any]
    subject_digest: str
    requirement: str
    evidence_contract: Mapping[str, Any]
    waivability: Waivability
    risk: str
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": OBLIGATION_SCHEMA,
            "obligation_id": self.obligation_id,
            "subject_kind": self.subject_kind,
            "subject_identity": dict(self.subject_identity),
            "subject_digest": self.subject_digest,
            "requirement": self.requirement,
            "evidence_contract": dict(self.evidence_contract),
            "waivability": self.waivability,
            "risk": self.risk,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class AssessmentRecord:
    assessment_id: str
    obligation_id: str
    evidence_id: str | None
    evaluation_id: str | None
    disposition_id: str | None
    subject_digest: str
    status: AssessmentStatus
    applicability: Applicability
    verdict: Verdict
    details: Mapping[str, Any]
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": ASSESSMENT_SCHEMA,
            "assessment_id": self.assessment_id,
            "obligation_id": self.obligation_id,
            "evidence_id": self.evidence_id,
            "evaluation_id": self.evaluation_id,
            "disposition_id": self.disposition_id,
            "subject_digest": self.subject_digest,
            "status": self.status,
            "applicability": self.applicability,
            "verdict": self.verdict,
            "details": dict(self.details),
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class WaiverDecision:
    waiver_id: str
    obligation_id: str
    subject_digest: str
    risk: str
    actor: str
    authority: WaiverAuthority
    rationale: str
    scope: str
    expires_at: str
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": WAIVER_DECISION_SCHEMA,
            "waiver_id": self.waiver_id,
            "obligation_id": self.obligation_id,
            "subject_digest": self.subject_digest,
            "risk": self.risk,
            "actor": self.actor,
            "authority": self.authority,
            "rationale": self.rationale,
            "scope": self.scope,
            "expires_at": self.expires_at,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class ApprovalRecord:
    approval_id: str
    subject_digest: str
    intent_revision_id: str
    effect_digest: str
    policy_digest: str
    actor: str
    authority: str
    scope: str
    expires_at: str
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": APPROVAL_SCHEMA,
            "approval_id": self.approval_id,
            "subject_digest": self.subject_digest,
            "intent_revision_id": self.intent_revision_id,
            "effect_digest": self.effect_digest,
            "policy_digest": self.policy_digest,
            "actor": self.actor,
            "authority": self.authority,
            "scope": self.scope,
            "expires_at": self.expires_at,
            "created_at": self.created_at,
        }


def _derive_verdict(status: AssessmentStatus, applicability: Applicability) -> Verdict:
    if applicability == "NOT_APPLICABLE":
        return "NOT_APPLICABLE"
    if status == "SATISFIED" and applicability == "APPLICABLE":
        return "PASS"
    if status == "UNSATISFIED" and applicability == "APPLICABLE":
        return "FAIL"
    return "UNKNOWN"


class EvidencePolicyGate:
    """Deterministic promotion evidence gate over the canonical M4c EvidenceStore."""

    def __init__(self, evidence_store: EvidenceStore) -> None:
        if evidence_store is None:
            _fail("evidence_store_required", "EvidencePolicyGate requires canonical EvidenceStore")
        self.evidence_store = evidence_store
        self._connection = evidence_store._connection
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS m6a_obligations (
              obligation_id TEXT PRIMARY KEY,
              subject_kind TEXT NOT NULL,
              subject_identity_json BLOB NOT NULL,
              subject_digest TEXT NOT NULL,
              requirement TEXT NOT NULL,
              evidence_contract_json BLOB NOT NULL,
              waivability TEXT NOT NULL,
              risk TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS m6a_assessments (
              assessment_id TEXT PRIMARY KEY,
              obligation_id TEXT NOT NULL REFERENCES m6a_obligations(obligation_id),
              evidence_id TEXT,
              evaluation_id TEXT,
              disposition_id TEXT,
              subject_digest TEXT NOT NULL,
              status TEXT NOT NULL,
              applicability TEXT NOT NULL,
              verdict TEXT NOT NULL,
              details_json BLOB NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS m6a_waiver_decisions (
              waiver_id TEXT PRIMARY KEY,
              obligation_id TEXT NOT NULL REFERENCES m6a_obligations(obligation_id),
              subject_digest TEXT NOT NULL,
              risk TEXT NOT NULL,
              actor TEXT NOT NULL,
              authority TEXT NOT NULL,
              rationale TEXT NOT NULL,
              scope TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS m6a_approvals (
              approval_id TEXT PRIMARY KEY,
              subject_digest TEXT NOT NULL,
              intent_revision_id TEXT NOT NULL,
              effect_digest TEXT NOT NULL,
              policy_digest TEXT NOT NULL,
              actor TEXT NOT NULL,
              authority TEXT NOT NULL,
              scope TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS m6a_assessments_by_obligation ON m6a_assessments(obligation_id, created_at);
            CREATE INDEX IF NOT EXISTS m6a_waivers_by_obligation ON m6a_waiver_decisions(obligation_id, created_at);
            CREATE INDEX IF NOT EXISTS m6a_approvals_by_subject ON m6a_approvals(subject_digest, created_at);
            """
        )

    def _begin_write(self) -> None:
        try:
            begin_control_write(self._connection)
        except ControlStoreError as exc:
            _fail(exc.code, str(exc))

    def _commit_write(self) -> None:
        try:
            commit_control_write(self._connection)
        except ControlStoreError as exc:
            _fail(exc.code, str(exc))

    def _rollback_write(self) -> None:
        try:
            rollback_control_write(self._connection)
        except ControlStoreError as exc:
            _fail(exc.code, str(exc))

    def create_obligation(
        self,
        *,
        subject_kind: str,
        subject_identity: Mapping[str, Any],
        requirement: str,
        evidence_contract: Mapping[str, Any],
        waivability: str,
        risk: str,
    ) -> ObligationRecord:
        if waivability not in {"NEVER", "AUTHORIZED_USER", "ADMIN_ONLY"}:
            _fail("invalid_waivability", f"waivability must be NEVER, AUTHORIZED_USER, or ADMIN_ONLY: {waivability}")

        contract = dict(evidence_contract)
        for required_key in ("evidence_type", "coverage", "freshness"):
            if required_key not in contract or not contract[required_key]:
                _fail("invalid_evidence_contract", f"evidence_contract missing required field: {required_key}")
        if contract["freshness"] not in {"CURRENT", "IMMUTABLE_EXACT"}:
            _fail("invalid_freshness", f"freshness must be CURRENT or IMMUTABLE_EXACT: {contract['freshness']}")

        # Validate digest fields if present
        for digest_key in ("checker_code_digest", "environment_fingerprint", "evaluation_config_digest"):
            if digest_key in contract and contract[digest_key] is not None:
                _validate_digest(contract[digest_key], digest_key)

        subject_digest = compute_subject_digest(subject_kind, subject_identity)
        identity = {
            "schema": OBLIGATION_SCHEMA,
            "subject_kind": subject_kind,
            "subject_identity": dict(subject_identity),
            "subject_digest": subject_digest,
            "requirement": requirement,
            "evidence_contract": contract,
            "waivability": waivability,
            "risk": risk,
        }
        obligation_id = semantic_digest(identity)
        created_at = _now()

        existing = self.get_obligation(obligation_id)
        if existing is not None:
            return existing

        self._begin_write()
        try:
            self._connection.execute(
                "INSERT INTO m6a_obligations VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    obligation_id,
                    subject_kind,
                    canonical_json_bytes(dict(subject_identity)),
                    subject_digest,
                    requirement,
                    canonical_json_bytes(contract),
                    waivability,
                    risk,
                    created_at,
                ),
            )
            self._commit_write()
        except sqlite3.IntegrityError:
            self._rollback_write()
            existing = self.get_obligation(obligation_id)
            if existing is not None:
                return existing
            _fail("obligation_storage_conflict", "obligation identity conflict during commit")
        except Exception:
            self._rollback_write()
            raise

        return self.get_obligation(obligation_id)  # type: ignore[return-value]

    def get_obligation(self, obligation_id: str) -> ObligationRecord | None:
        row = self._connection.execute(
            "SELECT obligation_id, subject_kind, subject_identity_json, subject_digest, requirement, evidence_contract_json, waivability, risk, created_at FROM m6a_obligations WHERE obligation_id=?",
            (obligation_id,),
        ).fetchone()
        if not row:
            return None
        return ObligationRecord(
            obligation_id=str(row[0]),
            subject_kind=str(row[1]),
            subject_identity=json.loads(bytes(row[2]).decode("utf-8")),
            subject_digest=str(row[3]),
            requirement=str(row[4]),
            evidence_contract=json.loads(bytes(row[5]).decode("utf-8")),
            waivability=row[6],  # type: ignore[arg-type]
            risk=str(row[7]),
            created_at=str(row[8]),
        )

    def create_waiver(
        self,
        *,
        obligation_id: str,
        subject_digest: str,
        risk: str,
        actor: str,
        authority: str,
        rationale: str,
        scope: str,
        expires_at: str,
    ) -> WaiverDecision:
        if authority not in {"USER", "ADMIN"}:
            _fail("invalid_authority", f"authority must be USER or ADMIN: {authority}")
        _validate_digest(subject_digest, "subject_digest")

        obligation = self.get_obligation(obligation_id)
        if obligation is None:
            _fail("obligation_missing", "waiver requires an existing obligation")

        identity = {
            "schema": WAIVER_DECISION_SCHEMA,
            "obligation_id": obligation_id,
            "subject_digest": subject_digest,
            "risk": risk,
            "actor": actor,
            "authority": authority,
            "rationale": rationale,
            "scope": scope,
            "expires_at": expires_at,
        }
        waiver_id = semantic_digest(identity)
        created_at = _now()

        existing = self.get_waiver(waiver_id)
        if existing is not None:
            return existing

        self._begin_write()
        try:
            self._connection.execute(
                "INSERT INTO m6a_waiver_decisions VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    waiver_id,
                    obligation_id,
                    subject_digest,
                    risk,
                    actor,
                    authority,
                    rationale,
                    scope,
                    expires_at,
                    created_at,
                ),
            )
            self._commit_write()
        except sqlite3.IntegrityError:
            self._rollback_write()
            existing = self.get_waiver(waiver_id)
            if existing is not None:
                return existing
            _fail("waiver_storage_conflict", "waiver identity conflict during commit")
        except Exception:
            self._rollback_write()
            raise

        return self.get_waiver(waiver_id)  # type: ignore[return-value]

    def get_waiver(self, waiver_id: str) -> WaiverDecision | None:
        row = self._connection.execute(
            "SELECT waiver_id, obligation_id, subject_digest, risk, actor, authority, rationale, scope, expires_at, created_at FROM m6a_waiver_decisions WHERE waiver_id=?",
            (waiver_id,),
        ).fetchone()
        if not row:
            return None
        return WaiverDecision(
            waiver_id=str(row[0]),
            obligation_id=str(row[1]),
            subject_digest=str(row[2]),
            risk=str(row[3]),
            actor=str(row[4]),
            authority=row[5],  # type: ignore[arg-type]
            rationale=str(row[6]),
            scope=str(row[7]),
            expires_at=str(row[8]),
            created_at=str(row[9]),
        )

    def waivers_for_obligation(self, obligation_id: str) -> tuple[WaiverDecision, ...]:
        rows = self._connection.execute(
            "SELECT waiver_id, obligation_id, subject_digest, risk, actor, authority, rationale, scope, expires_at, created_at FROM m6a_waiver_decisions WHERE obligation_id=? ORDER BY rowid",
            (obligation_id,),
        ).fetchall()
        return tuple(
            WaiverDecision(
                waiver_id=str(row[0]),
                obligation_id=str(row[1]),
                subject_digest=str(row[2]),
                risk=str(row[3]),
                actor=str(row[4]),
                authority=row[5],  # type: ignore[arg-type]
                rationale=str(row[6]),
                scope=str(row[7]),
                expires_at=str(row[8]),
                created_at=str(row[9]),
            )
            for row in rows
        )

    def create_approval(
        self,
        *,
        subject_digest: str,
        intent_revision_id: str,
        effect_digest: str,
        policy_digest: str,
        actor: str,
        authority: str,
        scope: str,
        expires_at: str,
    ) -> ApprovalRecord:
        _validate_digest(subject_digest, "subject_digest")
        _validate_digest(effect_digest, "effect_digest")
        _validate_digest(policy_digest, "policy_digest")

        identity = {
            "schema": APPROVAL_SCHEMA,
            "subject_digest": subject_digest,
            "intent_revision_id": intent_revision_id,
            "effect_digest": effect_digest,
            "policy_digest": policy_digest,
            "actor": actor,
            "authority": authority,
            "scope": scope,
            "expires_at": expires_at,
        }
        approval_id = semantic_digest(identity)
        created_at = _now()

        existing = self.get_approval(approval_id)
        if existing is not None:
            return existing

        self._begin_write()
        try:
            self._connection.execute(
                "INSERT INTO m6a_approvals VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    approval_id,
                    subject_digest,
                    intent_revision_id,
                    effect_digest,
                    policy_digest,
                    actor,
                    authority,
                    scope,
                    expires_at,
                    created_at,
                ),
            )
            self._commit_write()
        except sqlite3.IntegrityError:
            self._rollback_write()
            existing = self.get_approval(approval_id)
            if existing is not None:
                return existing
            _fail("approval_storage_conflict", "approval identity conflict during commit")
        except Exception:
            self._rollback_write()
            raise

        return self.get_approval(approval_id)  # type: ignore[return-value]

    def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        row = self._connection.execute(
            "SELECT approval_id, subject_digest, intent_revision_id, effect_digest, policy_digest, actor, authority, scope, expires_at, created_at FROM m6a_approvals WHERE approval_id=?",
            (approval_id,),
        ).fetchone()
        if not row:
            return None
        return ApprovalRecord(
            approval_id=str(row[0]),
            subject_digest=str(row[1]),
            intent_revision_id=str(row[2]),
            effect_digest=str(row[3]),
            policy_digest=str(row[4]),
            actor=str(row[5]),
            authority=str(row[6]),
            scope=str(row[7]),
            expires_at=str(row[8]),
            created_at=str(row[9]),
        )

    def assess_obligation(
        self,
        obligation_id: str,
        evidence_id: str | None = None,
        *,
        not_applicable: bool = False,
        not_applicable_reason: str | None = None,
    ) -> AssessmentRecord:
        obligation = self.get_obligation(obligation_id)
        if obligation is None:
            _fail("obligation_missing", "cannot assess missing obligation", details={"obligation_id": obligation_id})

        created_at = _now()
        evaluation_id: str | None = None
        disposition_id: str | None = None

        if not_applicable:
            status: AssessmentStatus = "SATISFIED"
            applicability: Applicability = "NOT_APPLICABLE"
            verdict: Verdict = "NOT_APPLICABLE"
            details = {"reason": not_applicable_reason or "explicit_not_applicable"}
        elif evidence_id is None:
            status = "UNKNOWN"
            applicability = "UNKNOWN"
            verdict = "UNKNOWN"
            details = {"reason": "evidence_missing"}
        else:
            evidence_record = self.evidence_store.get(evidence_id)
            if evidence_record is None:
                status = "UNKNOWN"
                applicability = "UNKNOWN"
                verdict = "UNKNOWN"
                details = {"reason": "evidence_missing"}
            else:
                contract = obligation.evidence_contract
                # 1. Exact subject verification
                ev_subject_digest = compute_subject_digest(
                    evidence_record.primary_subject_kind,
                    evidence_record.primary_subject_identity,
                )
                if (
                    evidence_record.primary_subject_kind != obligation.subject_kind
                    or dict(evidence_record.primary_subject_identity) != dict(obligation.subject_identity)
                    or ev_subject_digest != obligation.subject_digest
                ):
                    status = "STALE"
                    applicability = "UNKNOWN"
                    verdict = "UNKNOWN"
                    details = {
                        "reason": "subject_mismatch",
                        "expected_subject_digest": obligation.subject_digest,
                        "evidence_subject_digest": ev_subject_digest,
                    }
                # 2. Checker contract verification
                elif contract.get("checker_id") and evidence_record.checker_id != contract["checker_id"]:
                    status = "STALE"
                    applicability = "UNKNOWN"
                    verdict = "UNKNOWN"
                    details = {"reason": "checker_id_mismatch"}
                elif contract.get("checker_version") and evidence_record.checker_version != contract["checker_version"]:
                    status = "STALE"
                    applicability = "UNKNOWN"
                    verdict = "UNKNOWN"
                    details = {"reason": "checker_version_mismatch"}
                elif contract.get("checker_code_digest") and evidence_record.checker_code_digest != contract["checker_code_digest"]:
                    status = "STALE"
                    applicability = "UNKNOWN"
                    verdict = "UNKNOWN"
                    details = {"reason": "checker_code_digest_mismatch"}
                # 3. Environment fingerprint verification
                elif contract.get("environment_fingerprint") and (
                    evidence_record.environment.get("fingerprint") != contract["environment_fingerprint"]
                    and semantic_digest(evidence_record.environment) != contract["environment_fingerprint"]
                ):
                    status = "STALE"
                    applicability = "UNKNOWN"
                    verdict = "UNKNOWN"
                    details = {"reason": "environment_fingerprint_mismatch"}
                else:
                    # 4. M4c query verification (Candidate applicability & raw integrity)
                    try:
                        q = self.evidence_store.query(evidence_id)
                        candidate_applicable = q["applicability"]["applicable"]
                        cand_reason = q["applicability"]["reason"]
                        effective = q["effective_disposition"]
                        current_disp = q["current_disposition"]
                        disposition_id = current_disp["disposition_id"] if current_disp else None

                        # Find evaluation
                        evals = self.evidence_store.evaluations(evidence_id)
                        current_eval = evals[-1] if evals else None
                        evaluation_id = current_eval.evaluation_id if current_eval else None

                        # 5. Config digest verification
                        if contract.get("evaluation_config_digest") and (
                            current_eval is None or current_eval.config_digest != contract["evaluation_config_digest"]
                        ):
                            status = "STALE"
                            applicability = "UNKNOWN"
                            verdict = "UNKNOWN"
                            details = {"reason": "evaluation_config_digest_mismatch"}
                        elif not candidate_applicable:
                            status = "STALE"
                            applicability = "UNKNOWN"
                            verdict = "UNKNOWN"
                            details = {"reason": cand_reason or "candidate_stale_or_invalidated"}
                        elif effective == "PASS":
                            status = "SATISFIED"
                            applicability = "APPLICABLE"
                            verdict = "PASS"
                            details = {"disposition": "PASS"}
                        elif effective == "FAIL":
                            status = "UNSATISFIED"
                            applicability = "APPLICABLE"
                            verdict = "FAIL"
                            details = {"disposition": "FAIL"}
                        else:
                            status = "UNKNOWN"
                            applicability = "UNKNOWN"
                            verdict = "UNKNOWN"
                            details = {"disposition": effective}
                    except EvidenceError as exc:
                        status = "STALE"
                        applicability = "UNKNOWN"
                        verdict = "UNKNOWN"
                        details = {"reason": exc.code, "error": str(exc)}

        identity = {
            "schema": ASSESSMENT_SCHEMA,
            "obligation_id": obligation_id,
            "evidence_id": evidence_id,
            "evaluation_id": evaluation_id,
            "disposition_id": disposition_id,
            "subject_digest": obligation.subject_digest,
            "status": status,
            "applicability": applicability,
            "verdict": verdict,
            "details": details,
            "created_at": created_at,
        }
        assessment_id = semantic_digest(identity)

        self._begin_write()
        try:
            self._connection.execute(
                "INSERT INTO m6a_assessments VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    assessment_id,
                    obligation_id,
                    evidence_id,
                    evaluation_id,
                    disposition_id,
                    obligation.subject_digest,
                    status,
                    applicability,
                    verdict,
                    canonical_json_bytes(details),
                    created_at,
                ),
            )
            self._commit_write()
        except Exception:
            self._rollback_write()
            raise

        return AssessmentRecord(
            assessment_id=assessment_id,
            obligation_id=obligation_id,
            evidence_id=evidence_id,
            evaluation_id=evaluation_id,
            disposition_id=disposition_id,
            subject_digest=obligation.subject_digest,
            status=status,
            applicability=applicability,
            verdict=verdict,
            details=details,
            created_at=created_at,
        )

    def get_latest_assessment(self, obligation_id: str) -> AssessmentRecord | None:
        row = self._connection.execute(
            "SELECT assessment_id, obligation_id, evidence_id, evaluation_id, disposition_id, subject_digest, status, applicability, verdict, details_json, created_at FROM m6a_assessments WHERE obligation_id=? ORDER BY rowid DESC LIMIT 1",
            (obligation_id,),
        ).fetchone()
        if not row:
            return None
        return AssessmentRecord(
            assessment_id=str(row[0]),
            obligation_id=str(row[1]),
            evidence_id=str(row[2]) if row[2] else None,
            evaluation_id=str(row[3]) if row[3] else None,
            disposition_id=str(row[4]) if row[4] else None,
            subject_digest=str(row[5]),
            status=row[6],  # type: ignore[arg-type]
            applicability=row[7],  # type: ignore[arg-type]
            verdict=row[8],  # type: ignore[arg-type]
            details=json.loads(bytes(row[9]).decode("utf-8")),
            created_at=str(row[10]),
        )

    def query(self, obligation_id: str) -> dict[str, Any]:
        obligation = self.get_obligation(obligation_id)
        if obligation is None:
            _fail("obligation_missing", "obligation does not exist", details={"obligation_id": obligation_id})
        current = self.get_latest_assessment(obligation_id)
        waivers = self.waivers_for_obligation(obligation_id)

        payload: dict[str, Any] = {
            "schema": OBLIGATION_QUERY_SCHEMA,
            "obligation": obligation.as_dict(),
            "current_assessment": current.as_dict() if current else None,
            "waiver_history": [w.as_dict() for w in waivers],
        }
        payload["query_digest"] = semantic_digest(payload)
        return payload

    def promotion_gate(
        self,
        *,
        obligation_ids: Sequence[str],
        approval_id: str | None,
        subject: Mapping[str, Any],
        intent_revision_id: str,
        effect_digest: str,
        policy_digest: str,
        scope: str,
        now: str | None = None,
    ) -> dict[str, Any]:
        now_ts = now or _now()

        # Determine subject_digest
        if "subject_kind" in subject and "subject_identity" in subject:
            expected_subject_digest = compute_subject_digest(subject["subject_kind"], subject["subject_identity"])
        elif "subject_digest" in subject:
            expected_subject_digest = str(subject["subject_digest"])
        else:
            _fail("invalid_subject", "subject must contain subject_kind and subject_identity or subject_digest")

        obligation_results: list[dict[str, Any]] = []
        reasons: list[str] = []
        all_obligations_passed = True

        for ob_id in obligation_ids:
            obligation = self.get_obligation(ob_id)
            if obligation is None:
                all_obligations_passed = False
                reasons.append(f"obligation_{ob_id}_missing")
                obligation_results.append({
                    "obligation_id": ob_id,
                    "status": "UNKNOWN",
                    "applicability": "UNKNOWN",
                    "verdict": "UNKNOWN",
                    "waivability": "NEVER",
                    "allowed_by_waiver": False,
                    "waiver_id": None,
                    "reasons": ["obligation_missing"],
                })
                continue

            if obligation.subject_digest != expected_subject_digest:
                all_obligations_passed = False
                reasons.append(f"obligation_{ob_id}_subject_mismatch")
                obligation_results.append({
                    "obligation_id": ob_id,
                    "status": "STALE",
                    "applicability": "UNKNOWN",
                    "verdict": "UNKNOWN",
                    "waivability": obligation.waivability,
                    "allowed_by_waiver": False,
                    "waiver_id": None,
                    "reasons": ["subject_digest_mismatch"],
                })
                continue

            assessment = self.get_latest_assessment(ob_id)
            if assessment is None:
                assessment_status = "UNKNOWN"
                assessment_app = "UNKNOWN"
                assessment_verdict = "UNKNOWN"
            else:
                assessment_status = assessment.status
                assessment_app = assessment.applicability
                assessment_verdict = assessment.verdict

            allowed_by_waiver = False
            matching_waiver_id: str | None = None
            ob_passed = False
            ob_reasons: list[str] = []

            if assessment_verdict in {"PASS", "NOT_APPLICABLE"}:
                ob_passed = True
            else:
                # Check waivers
                if obligation.waivability == "NEVER":
                    ob_reasons.append("obligation_not_waivable")
                else:
                    waivers = self.waivers_for_obligation(ob_id)
                    for w in waivers:
                        # Check subject, risk, scope
                        if w.subject_digest != expected_subject_digest:
                            continue
                        if w.risk != obligation.risk:
                            continue
                        if w.scope != scope:
                            continue
                        # Check authority
                        if obligation.waivability == "ADMIN_ONLY" and w.authority != "ADMIN":
                            continue
                        if obligation.waivability == "AUTHORIZED_USER" and w.authority not in {"USER", "ADMIN"}:
                            continue
                        # Check expiration
                        if now_ts > w.expires_at:
                            continue
                        # Found valid waiver!
                        allowed_by_waiver = True
                        matching_waiver_id = w.waiver_id
                        ob_passed = True
                        break
                    if not ob_passed:
                        ob_reasons.append("no_valid_waiver")

            if not ob_passed:
                all_obligations_passed = False
                reasons.append(f"obligation_{ob_id}_unsatisfied")

            obligation_results.append({
                "obligation_id": ob_id,
                "status": assessment_status,
                "applicability": assessment_app,
                "verdict": assessment_verdict,
                "waivability": obligation.waivability,
                "allowed_by_waiver": allowed_by_waiver,
                "waiver_id": matching_waiver_id,
                "reasons": ob_reasons,
            })

        # Check approval
        approval_valid = False
        if approval_id is None:
            reasons.append("approval_missing")
        else:
            approval = self.get_approval(approval_id)
            if approval is None:
                reasons.append("approval_not_found")
            elif approval.subject_digest != expected_subject_digest:
                reasons.append("approval_subject_mismatch")
            elif approval.intent_revision_id != intent_revision_id:
                reasons.append("approval_intent_mismatch")
            elif approval.effect_digest != effect_digest:
                reasons.append("approval_effect_mismatch")
            elif approval.policy_digest != policy_digest:
                reasons.append("approval_policy_mismatch")
            elif approval.scope != scope:
                reasons.append("approval_scope_mismatch")
            elif now_ts > approval.expires_at:
                reasons.append("approval_expired")
            else:
                approval_valid = True

        allowed = all_obligations_passed and approval_valid
        decision: Literal["ALLOW", "BLOCK"] = "ALLOW" if allowed else "BLOCK"

        result_payload: dict[str, Any] = {
            "schema": GATE_SCHEMA,
            "decision": decision,
            "allowed": allowed,
            "subject_digest": expected_subject_digest,
            "approval_id": approval_id if approval_valid else None,
            "obligation_results": obligation_results,
            "reasons": reasons,
        }
        result_payload["decision_digest"] = semantic_digest(result_payload)
        return result_payload


__all__ = [
    "APPROVAL_SCHEMA",
    "ASSESSMENT_SCHEMA",
    "Applicability",
    "ApprovalRecord",
    "AssessmentRecord",
    "AssessmentStatus",
    "ENVIRONMENT_FINGERPRINT_SCHEMA",
    "EvidencePolicyGate",
    "GATE_SCHEMA",
    "M6aError",
    "OBLIGATION_QUERY_SCHEMA",
    "OBLIGATION_SCHEMA",
    "ObligationRecord",
    "SUBJECT_SCHEMA",
    "Verdict",
    "WAIVER_DECISION_SCHEMA",
    "WaiverAuthority",
    "WaiverDecision",
    "Waivability",
    "compute_subject_digest",
]

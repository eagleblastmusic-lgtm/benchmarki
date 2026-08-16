"""M6c canonical validation/policy authority cutover for inactive BDB vNext.

M6b remains the deterministic checker selector and M4c/M6a remain the
canonical evidence/policy stores.  This module makes one allowlisted vNext flow
bind those pieces into an ACTIVE_CANONICAL gate.  It never accepts a legacy or
model-facing profile identifier and has no fallback to a legacy selector.

This is build-only internal closure: it does not activate production refs,
delete legacy runtime code, or mutate Git.  M7c is the first consumer that may
use this authority to gate an isolated vNext promotion effect.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, NoReturn, Sequence

from bdb_shared.evidence import canonical_json_bytes, semantic_digest
from bdb_vnext.control_store import (
    ControlStoreError,
    begin_control_write,
    commit_control_write,
    rollback_control_write,
)
from bdb_vnext.engineering_loop import ValidationCommand
from bdb_vnext.m4c_evidence import EvidenceError
from bdb_vnext.m6a_evidence_policy import EvidencePolicyGate, compute_subject_digest
from bdb_vnext.m6b_check_plan import DeterministicCheckPlanSelector, M6bError


M6C_FLOW_SCHEMA = "bdb-vnext-m6c-validation-flow-v1"
M6C_GATE_SCHEMA = "bdb-vnext-m6c-validation-gate-v1"
M6C_QUERY_SCHEMA = "bdb-vnext-m6c-validation-query-v1"
M6C_AUTHORITY = "devmaster.bdb.vnext.validation.canonical-authority"
M6C_MODE = "ACTIVE_CANONICAL"

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class M6cError(RuntimeError):
    """Typed fail-closed M6c authority/cutover failure."""

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
) -> NoReturn:
    raise M6cError(code, message, details=details)


def _text(value: object, field: str, *, maximum: int = 1024) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or "\x00" in value
        or any(ord(char) < 32 for char in value)
    ):
        _fail("invalid_validation_authority_input", f"{field} must be bounded non-empty text")
    return value


def _digest(value: object, field: str) -> str:
    text = _text(value, field, maximum=71)
    if _DIGEST_RE.fullmatch(text) is None:
        _fail("invalid_validation_authority_input", f"{field} must be exact lowercase sha256:<64 hex>")
    return text


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _subject_digest(subject: Mapping[str, Any]) -> str:
    if "subject_kind" in subject and "subject_identity" in subject:
        kind = _text(subject["subject_kind"], "subject_kind")
        identity = subject["subject_identity"]
        if not isinstance(identity, Mapping):
            _fail("invalid_validation_subject", "subject_identity must be a mapping")
        return compute_subject_digest(kind, identity)
    if "subject_digest" in subject:
        return _digest(subject["subject_digest"], "subject_digest")
    _fail(
        "invalid_validation_subject",
        "subject must contain subject_kind + subject_identity or subject_digest",
    )


def _evidence_environment_fingerprint(environment: Mapping[str, Any]) -> str | None:
    direct = environment.get("fingerprint")
    if isinstance(direct, str) and _DIGEST_RE.fullmatch(direct):
        return direct
    if environment:
        return semantic_digest(dict(environment))
    return None


@dataclass(frozen=True)
class ActiveValidationFlow:
    flow_id: str
    revision_id: str
    policy_revision: str
    generation: str
    scope: str
    required_capabilities: tuple[str, ...]
    plan: Mapping[str, Any]
    plan_digest: str
    registry_digest: str
    policy_digest: str
    activated_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": M6C_FLOW_SCHEMA,
            "authority": M6C_AUTHORITY,
            "mode": M6C_MODE,
            "flow_id": self.flow_id,
            "revision_id": self.revision_id,
            "policy_revision": self.policy_revision,
            "generation": self.generation,
            "scope": self.scope,
            "required_capabilities": list(self.required_capabilities),
            "selected_check_plan": dict(self.plan),
            "plan_digest": self.plan_digest,
            "registry_digest": self.registry_digest,
            "policy_digest": self.policy_digest,
            "activated_at": self.activated_at,
        }


class CanonicalValidationAuthority:
    """One deterministic validation/policy authority for allowlisted vNext flows."""

    def __init__(
        self,
        *,
        evidence_policy_gate: EvidencePolicyGate,
        selector: DeterministicCheckPlanSelector | None = None,
    ) -> None:
        if evidence_policy_gate is None:
            _fail("m6c_dependencies_required", "M6c requires the canonical M6a EvidencePolicyGate")
        self.evidence_policy_gate = evidence_policy_gate
        self.evidence_store = evidence_policy_gate.evidence_store
        self.selector = selector or DeterministicCheckPlanSelector()
        self._connection = evidence_policy_gate._connection
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS m6c_validation_flow_revisions (
              revision_id TEXT PRIMARY KEY,
              flow_id TEXT NOT NULL,
              policy_revision TEXT NOT NULL,
              generation TEXT NOT NULL,
              scope TEXT NOT NULL,
              required_capabilities_json BLOB NOT NULL,
              plan_json BLOB NOT NULL,
              plan_digest TEXT NOT NULL,
              registry_digest TEXT NOT NULL,
              policy_digest TEXT NOT NULL UNIQUE,
              activated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS m6c_validation_flow_heads (
              flow_id TEXT PRIMARY KEY,
              revision_id TEXT NOT NULL REFERENCES m6c_validation_flow_revisions(revision_id)
            );
            CREATE INDEX IF NOT EXISTS m6c_validation_revisions_by_flow
              ON m6c_validation_flow_revisions(flow_id, activated_at);
            """
        )

    def _begin(self) -> None:
        try:
            begin_control_write(self._connection)
        except ControlStoreError as exc:
            _fail(exc.code, str(exc))

    def _commit(self) -> None:
        try:
            commit_control_write(self._connection)
        except ControlStoreError as exc:
            _fail(exc.code, str(exc))

    def _rollback(self) -> None:
        try:
            rollback_control_write(self._connection)
        except ControlStoreError as exc:
            _fail(exc.code, str(exc))

    @staticmethod
    def _flow_from_row(row: tuple[Any, ...]) -> ActiveValidationFlow:
        required = tuple(json.loads(bytes(row[5]).decode("utf-8")))
        plan = json.loads(bytes(row[6]).decode("utf-8"))
        return ActiveValidationFlow(
            revision_id=str(row[0]),
            flow_id=str(row[1]),
            policy_revision=str(row[2]),
            generation=str(row[3]),
            scope=str(row[4]),
            required_capabilities=required,
            plan=plan,
            plan_digest=str(row[7]),
            registry_digest=str(row[8]),
            policy_digest=str(row[9]),
            activated_at=str(row[10]),
        )

    def get_active_flow(self, flow_id: str) -> ActiveValidationFlow | None:
        flow_id = _text(flow_id, "flow_id", maximum=256)
        row = self._connection.execute(
            "SELECT r.revision_id,r.flow_id,r.policy_revision,r.generation,r.scope,"
            "r.required_capabilities_json,r.plan_json,r.plan_digest,r.registry_digest,"
            "r.policy_digest,r.activated_at "
            "FROM m6c_validation_flow_heads h "
            "JOIN m6c_validation_flow_revisions r ON r.revision_id=h.revision_id "
            "WHERE h.flow_id=?",
            (flow_id,),
        ).fetchone()
        return self._flow_from_row(row) if row else None

    def activate_flow(
        self,
        *,
        flow_id: str,
        policy_revision: str,
        generation: str,
        scope: str,
        required_capabilities: Iterable[str],
        executable_bindings: Mapping[str, str],
    ) -> ActiveValidationFlow:
        """Activate one exact runtime-selected CheckPlan for an internal vNext flow.

        Activation is explicit and versioned.  A changed policy/checker plan
        creates a new immutable revision and atomically moves only that flow's
        head.  Existing approvals therefore stop matching the new policy
        digest instead of silently carrying across the cutover.
        """

        flow_id = _text(flow_id, "flow_id", maximum=256)
        policy_revision = _text(policy_revision, "policy_revision", maximum=256)
        generation = _text(generation, "generation", maximum=256)
        scope = _text(scope, "scope", maximum=512)
        required = tuple(sorted({_text(item, "capability_id", maximum=256) for item in required_capabilities}))
        if not required:
            _fail("required_capability_missing", "M6c flow requires at least one validation capability")

        try:
            plan = self.selector.plan(
                required_capabilities=required,
                executable_bindings=executable_bindings,
            )
        except M6bError as exc:
            raise M6cError(
                "validation_capability_unavailable",
                "canonical vNext validation plan cannot be selected exactly",
                details={"cause": exc.code, **dict(exc.details)},
            ) from exc

        policy_material = {
            "schema": M6C_FLOW_SCHEMA,
            "authority": M6C_AUTHORITY,
            "mode": M6C_MODE,
            "flow_id": flow_id,
            "policy_revision": policy_revision,
            "generation": generation,
            "scope": scope,
            "required_capabilities": list(required),
            "plan_digest": plan.plan_digest,
            "registry_digest": plan.registry_digest,
        }
        policy_digest = semantic_digest(policy_material)
        revision_id = semantic_digest({**policy_material, "policy_digest": policy_digest})
        existing = self._connection.execute(
            "SELECT revision_id,flow_id,policy_revision,generation,scope,required_capabilities_json,"
            "plan_json,plan_digest,registry_digest,policy_digest,activated_at "
            "FROM m6c_validation_flow_revisions WHERE revision_id=?",
            (revision_id,),
        ).fetchone()
        if existing is None:
            activated_at = _now()
            self._begin()
            try:
                self._connection.execute(
                    "INSERT INTO m6c_validation_flow_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        revision_id,
                        flow_id,
                        policy_revision,
                        generation,
                        scope,
                        canonical_json_bytes(list(required)),
                        canonical_json_bytes(plan.as_dict()),
                        plan.plan_digest,
                        plan.registry_digest,
                        policy_digest,
                        activated_at,
                    ),
                )
                self._connection.execute(
                    "INSERT INTO m6c_validation_flow_heads(flow_id,revision_id) VALUES (?,?) "
                    "ON CONFLICT(flow_id) DO UPDATE SET revision_id=excluded.revision_id",
                    (flow_id, revision_id),
                )
                self._commit()
            except sqlite3.IntegrityError as exc:
                if self._connection.in_transaction:
                    self._rollback()
                raise M6cError(
                    "validation_flow_storage_conflict",
                    "canonical validation flow revision conflicted",
                ) from exc
            except Exception:
                if self._connection.in_transaction:
                    self._rollback()
                raise
        else:
            current = self.get_active_flow(flow_id)
            if current is None or current.revision_id != revision_id:
                self._begin()
                try:
                    self._connection.execute(
                        "INSERT INTO m6c_validation_flow_heads(flow_id,revision_id) VALUES (?,?) "
                        "ON CONFLICT(flow_id) DO UPDATE SET revision_id=excluded.revision_id",
                        (flow_id, revision_id),
                    )
                    self._commit()
                except Exception:
                    if self._connection.in_transaction:
                        self._rollback()
                    raise

        result = self.get_active_flow(flow_id)
        assert result is not None
        return result

    def validation_commands(self, flow_id: str) -> tuple[ValidationCommand, ...]:
        """Return only commands selected by the current canonical M6b plan."""

        flow = self.get_active_flow(flow_id)
        if flow is None:
            _fail("validation_flow_inactive", "canonical validation flow is not active")
        commands: list[ValidationCommand] = []
        checks = flow.plan.get("checks")
        if not isinstance(checks, list) or not checks:
            _fail("validation_plan_corrupt", "active validation plan has no checks")
        for check in checks:
            if not isinstance(check, Mapping):
                _fail("validation_plan_corrupt", "active validation plan contains an invalid check")
            argv = check.get("argv")
            if not isinstance(argv, list) or not argv or any(not isinstance(item, str) or not item for item in argv):
                _fail("validation_plan_corrupt", "active validation command argv is invalid")
            commands.append(
                ValidationCommand(
                    checker_id=_text(check.get("checker_id"), "checker_id", maximum=256),
                    checker_version=_text(check.get("checker_version"), "checker_version", maximum=256),
                    argv=tuple(argv),
                    cwd=_text(check.get("cwd"), "cwd", maximum=4096),
                    timeout_seconds=float(check.get("timeout_seconds")),
                    max_stdout_bytes=int(check.get("max_stdout_bytes")),
                    max_stderr_bytes=int(check.get("max_stderr_bytes")),
                )
            )
        return tuple(commands)

    def authorize(
        self,
        *,
        flow_id: str,
        obligation_by_capability: Mapping[str, str],
        evidence_by_capability: Mapping[str, str],
        approval_id: str | None,
        subject: Mapping[str, Any],
        intent_revision_id: str,
        effect_digest: str,
        scope: str,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Evaluate the sole canonical M6 gate for one enabled vNext effect.

        Every required M6b capability must be represented by one exact M6a
        obligation whose current assessment points at Evidence produced by the
        same checker/version/code identity and exact environment contract.
        Legacy profile results cannot satisfy this mapping because they are not
        consulted and there is no fallback branch.
        """

        flow = self.get_active_flow(flow_id)
        if flow is None:
            _fail("validation_flow_inactive", "canonical validation flow is not active")
        if scope != flow.scope:
            _fail(
                "validation_scope_mismatch",
                "requested effect scope differs from the active validation flow",
            )
        effect_digest = _digest(effect_digest, "effect_digest")
        expected_subject_digest = _subject_digest(subject)

        required = set(flow.required_capabilities)
        obligation_keys = set(obligation_by_capability)
        evidence_keys = set(evidence_by_capability)
        coverage_reasons: list[str] = []
        coverage: list[dict[str, Any]] = []
        coverage_ok = True
        if obligation_keys != required:
            coverage_ok = False
            coverage_reasons.append("obligation_capability_set_mismatch")
        if evidence_keys != required:
            coverage_ok = False
            coverage_reasons.append("evidence_capability_set_mismatch")

        checks = flow.plan.get("checks")
        if not isinstance(checks, list):
            _fail("validation_plan_corrupt", "active validation plan checks are invalid")
        by_capability = {
            str(item.get("capability_id")): item
            for item in checks
            if isinstance(item, Mapping) and item.get("capability_id")
        }
        if set(by_capability) != required:
            _fail("validation_plan_corrupt", "active plan capability set differs from flow contract")

        obligation_ids: list[str] = []
        for capability_id in sorted(required):
            item_reasons: list[str] = []
            obligation_id = obligation_by_capability.get(capability_id)
            evidence_id = evidence_by_capability.get(capability_id)
            check = by_capability[capability_id]
            obligation = self.evidence_policy_gate.get_obligation(obligation_id) if obligation_id else None
            evidence = self.evidence_store.get(evidence_id) if evidence_id else None
            assessment = self.evidence_policy_gate.get_latest_assessment(obligation_id) if obligation_id else None

            if obligation is None:
                item_reasons.append("obligation_missing")
            else:
                obligation_ids.append(obligation.obligation_id)
                if obligation.subject_digest != expected_subject_digest:
                    item_reasons.append("obligation_subject_mismatch")
                contract = obligation.evidence_contract
                if contract.get("checker_id") != check.get("checker_id"):
                    item_reasons.append("checker_id_contract_mismatch")
                if contract.get("checker_version") != check.get("checker_version"):
                    item_reasons.append("checker_version_contract_mismatch")
                if contract.get("checker_code_digest") != check.get("checker_code_digest"):
                    item_reasons.append("checker_code_contract_mismatch")
                expected_environment = contract.get("environment_fingerprint")
                if not isinstance(expected_environment, str) or _DIGEST_RE.fullmatch(expected_environment) is None:
                    item_reasons.append("environment_contract_missing")

            if evidence is None:
                item_reasons.append("evidence_missing")
            else:
                if evidence.checker_id != check.get("checker_id"):
                    item_reasons.append("evidence_checker_id_mismatch")
                if evidence.checker_version != check.get("checker_version"):
                    item_reasons.append("evidence_checker_version_mismatch")
                if evidence.checker_code_digest != check.get("checker_code_digest"):
                    item_reasons.append("evidence_checker_code_mismatch")
                actual_environment = _evidence_environment_fingerprint(evidence.environment)
                expected_environment = obligation.evidence_contract.get("environment_fingerprint") if obligation else None
                if actual_environment is None or actual_environment != expected_environment:
                    item_reasons.append("evidence_environment_mismatch")

                # Authorization must re-observe current Evidence/Candidate truth.
                # A previously PASS/FAIL Assessment is immutable history and
                # cannot by itself authorize after the Candidate or raw Evidence
                # has become stale.  This read-only query adds no new Assessment
                # and never re-runs the checker.
                try:
                    current_evidence = self.evidence_store.query(evidence.evidence_id)
                except EvidenceError as exc:
                    item_reasons.append(f"evidence_current_query_failed:{exc.code}")
                else:
                    current_applicability = current_evidence.get("applicability")
                    if (
                        not isinstance(current_applicability, Mapping)
                        or current_applicability.get("applicable") is not True
                    ):
                        current_reason = current_applicability.get("reason")
                        item_reasons.append(
                            "evidence_not_current:"
                            + (str(current_reason) if current_reason else "applicability_unknown")
                        )
                    if assessment is not None:
                        expected_disposition = (
                            "PASS" if assessment.verdict == "PASS"
                            else "FAIL" if assessment.verdict == "FAIL"
                            else None
                        )
                        if (
                            expected_disposition is not None
                            and current_evidence.get("effective_disposition") != expected_disposition
                        ):
                            item_reasons.append("evidence_disposition_changed")

            if assessment is None:
                item_reasons.append("assessment_missing")
            elif evidence_id is None or assessment.evidence_id != evidence_id:
                item_reasons.append("assessment_evidence_mismatch")
            elif assessment.subject_digest != expected_subject_digest:
                item_reasons.append("assessment_subject_mismatch")

            item_ok = not item_reasons
            if not item_ok:
                coverage_ok = False
            coverage.append(
                {
                    "capability_id": capability_id,
                    "obligation_id": obligation_id,
                    "evidence_id": evidence_id,
                    "assessment_id": assessment.assessment_id if assessment else None,
                    "covered": item_ok,
                    "reasons": item_reasons,
                }
            )

        # M6a remains the semantic assessment/waiver/approval authority.  M6c
        # only adds the mandatory M6b-plan coverage and uses the *active* M6c
        # policy digest, making stale policy approvals fail closed.
        semantic_gate = self.evidence_policy_gate.promotion_gate(
            obligation_ids=tuple(sorted(set(obligation_ids))),
            approval_id=approval_id,
            subject={"subject_digest": expected_subject_digest},
            intent_revision_id=_text(intent_revision_id, "intent_revision_id", maximum=256),
            effect_digest=effect_digest,
            policy_digest=flow.policy_digest,
            scope=scope,
            now=now,
        )
        allowed = coverage_ok and semantic_gate.get("allowed") is True
        reasons = list(coverage_reasons)
        if semantic_gate.get("allowed") is not True:
            reasons.extend(str(item) for item in semantic_gate.get("reasons", []))

        result: dict[str, Any] = {
            "schema": M6C_GATE_SCHEMA,
            "authority": M6C_AUTHORITY,
            "mode": M6C_MODE,
            "decision": "ALLOW" if allowed else "BLOCK",
            "allowed": allowed,
            "flow_id": flow.flow_id,
            "flow_revision_id": flow.revision_id,
            "policy_digest": flow.policy_digest,
            "plan_digest": flow.plan_digest,
            "registry_digest": flow.registry_digest,
            "subject_digest": expected_subject_digest,
            "effect_digest": effect_digest,
            "approval_id": approval_id if semantic_gate.get("approval_id") else None,
            "coverage": coverage,
            "semantic_gate_decision_digest": semantic_gate.get("decision_digest"),
            "reasons": reasons,
        }
        result["decision_digest"] = semantic_digest(result)
        return result

    def query(self, flow_id: str) -> dict[str, Any]:
        flow = self.get_active_flow(flow_id)
        if flow is None:
            _fail("validation_flow_inactive", "canonical validation flow is not active")
        payload: dict[str, Any] = {
            "schema": M6C_QUERY_SCHEMA,
            "authority": M6C_AUTHORITY,
            "mode": M6C_MODE,
            "flow": flow.as_dict(),
            "legacy_selector_authority": False,
            "model_selected_validation": False,
            "production_activation": False,
        }
        payload["query_digest"] = semantic_digest(payload)
        return payload


__all__ = [
    "ActiveValidationFlow",
    "CanonicalValidationAuthority",
    "M6C_AUTHORITY",
    "M6C_FLOW_SCHEMA",
    "M6C_GATE_SCHEMA",
    "M6C_MODE",
    "M6C_QUERY_SCHEMA",
    "M6cError",
]

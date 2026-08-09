"""Build-only M2c engineering-intelligence semantic contracts.

M2c is deliberately a set of immutable, rebuildable records.  It does not
own Task identity, lifecycle state, a writer, a daemon, or repository bytes.
Exact committed RepoView and accepted M2b typed fragments remain the source
and transport authorities respectively.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, NoReturn

from bdb_shared.evidence import canonical_json_bytes, semantic_digest
from bdb_vnext.content_store import (
    DurableBindingStore,
    ImmutableContentStore,
    RepoViewBinding,
    TypedContextFragment,
    make_content_ref,
)
from bdb_vnext.context_transport import BrowserTransportProvider, NativeTransportProvider
from bdb_vnext.repo_view import CommittedRepoView


UNDERSTANDING_SCHEMA = "bdb-vnext-repository-understanding-v1"
CONTEXT_PACKAGE_SCHEMA = "bdb-vnext-context-package-v1"
CONTEXT_REQUEST_SCHEMA = "bdb-vnext-context-request-v1"
CONTEXT_RESOLUTION_SCHEMA = "bdb-vnext-context-resolution-v1"
ENGINEERING_DECISION_SCHEMA = "bdb-vnext-engineering-decision-v1"
CLAIM_SCHEMA = "bdb-vnext-understanding-claim-v1"
CONTRADICTION_SCHEMA = "bdb-vnext-understanding-contradiction-v1"
UNKNOWN_SCHEMA = "bdb-vnext-understanding-unknown-v1"
OMISSION_SCHEMA = "bdb-vnext-context-omission-v1"
AFFORDANCE_SCHEMA = "bdb-vnext-context-affordance-v1"
DECISION_OPTION_SCHEMA = "bdb-vnext-decision-option-v1"
M2C_PRODUCER_ID = "bdb-vnext-engineering-intelligence"
M2C_PRODUCER_VERSION = "m2c-v1"
M2C_POLICY_VERSION = "m2c-context-policy-v1"
SEMANTIC_RECORD_CONTENT_TYPE = "application/vnd.bdb-vnext.semantic+json"
HORIZONS = frozenset({"LOCAL", "COMPONENT", "REPOSITORY"})
CLAIM_KINDS = frozenset({"FACT", "INFERENCE", "ASSUMPTION", "HYPOTHESIS"})
CLAIM_AUTHORITIES = frozenset({"EXACT_SOURCE", "DERIVED"})
RESOLUTION_OUTCOMES = frozenset({"RESOLVED", "DENIED", "UNAVAILABLE"})
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+:-]{0,127}$")
_MAX_TEXT = 4096


class EngineeringIntelligenceError(ValueError):
    """Typed fail-closed error for M2c semantic contracts."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def _fail(code: str, message: str, *, details: Mapping[str, Any] | None = None) -> NoReturn:
    raise EngineeringIntelligenceError(code, message, details=details)


def _text(value: object, *, field: str, max_length: int = _MAX_TEXT) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        _fail("malformed_m2c_record", f"{field} must be a bounded non-empty string")
    return value


def _identifier(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        _fail("malformed_m2c_record", f"{field} must be a bounded identifier")
    return value


def _digest(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        _fail("malformed_m2c_record", f"{field} must be a lowercase sha256 digest")
    return value


def _sequence(value: object, *, field: str, allow_empty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        _fail("malformed_m2c_record", f"{field} must be an array")
    result = tuple(_text(item, field=f"{field}[]") for item in value)
    if not allow_empty and not result:
        _fail("malformed_m2c_record", f"{field} must not be empty")
    if len(set(result)) != len(result):
        _fail("duplicate_m2c_value", f"{field} must contain unique values")
    return result


def _identifier_sequence(value: object, *, field: str, allow_empty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        _fail("malformed_m2c_record", f"{field} must be an array")
    result = tuple(_identifier(item, field=f"{field}[]") for item in value)
    if not allow_empty and not result:
        _fail("malformed_m2c_record", f"{field} must not be empty")
    if len(set(result)) != len(result):
        _fail("duplicate_m2c_value", f"{field} must contain unique values")
    return result


def _mapping(value: object, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail("malformed_m2c_record", f"{field} must be an object")
    return value


def _exact_fields(value: Mapping[str, Any], required: set[str], *, field: str) -> None:
    if set(value) != required:
        _fail("malformed_m2c_record", f"{field} has an unexpected field set")


def _record_digest(identity: Mapping[str, Any]) -> str:
    return semantic_digest(identity)


def _parse_repo_binding(value: object, *, field: str = "repo_view") -> RepoViewBinding:
    if isinstance(value, RepoViewBinding):
        return value
    try:
        return RepoViewBinding.from_mapping(_mapping(value, field=field))
    except ValueError as exc:
        if isinstance(exc, EngineeringIntelligenceError):
            raise
        _fail("malformed_repo_view_basis", f"{field} is not an exact RepoView binding")


def _parse_intent(value: object) -> "IntentBasis":
    if isinstance(value, IntentBasis):
        return value
    return IntentBasis.from_mapping(_mapping(value, field="intent_basis"))


def _same_repo(left: RepoViewBinding, right: RepoViewBinding) -> bool:
    return left == right


def _coverage_state(
    requested_dimensions: Sequence[str],
    covered_dimensions: Sequence[str],
    must_see_categories: Sequence[str],
    covered_must_see: Sequence[str],
    unknowns: Sequence[object],
    omissions: Sequence[object],
    contradictions: Sequence[object],
) -> Literal["COMPLETE", "PARTIAL", "BLOCKED"]:
    missing_requested = set(requested_dimensions) - set(covered_dimensions)
    missing_must_see = set(must_see_categories) - set(covered_must_see)
    if missing_requested or missing_must_see:
        return "BLOCKED"
    if any(getattr(item, "policy_denied", False) for item in omissions):
        return "BLOCKED"
    if unknowns or omissions or contradictions:
        return "PARTIAL"
    return "COMPLETE"


@dataclass(frozen=True)
class IntentBasis:
    """Opaque caller-supplied intent identity; M2c never allocates Task IDs."""

    task_id: str
    intent_revision: str
    intent_digest: str

    def __post_init__(self) -> None:
        _text(self.task_id, field="task_id")
        _identifier(self.intent_revision, field="intent_revision")
        _digest(self.intent_digest, field="intent_digest")

    def as_dict(self) -> dict[str, str]:
        return {
            "task_id": self.task_id,
            "intent_revision": self.intent_revision,
            "intent_digest": self.intent_digest,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "IntentBasis":
        _exact_fields(value, {"task_id", "intent_revision", "intent_digest"}, field="intent_basis")
        return cls(value["task_id"], value["intent_revision"], value["intent_digest"])


@dataclass(frozen=True)
class UnderstandingClaim:
    """One epistemic claim explicitly separated from exact source authority."""

    claim_id: str
    repo_view: RepoViewBinding
    subject: str
    dimension: str
    kind: str
    authority: str
    statement: str
    evidence_refs: tuple[str, ...]
    basis_refs: tuple[str, ...]
    producer_id: str = M2C_PRODUCER_ID
    producer_version: str = M2C_PRODUCER_VERSION

    def __post_init__(self) -> None:
        _digest(self.claim_id, field="claim_id")
        if not isinstance(self.repo_view, RepoViewBinding):
            _fail("malformed_claim", "claim repo_view must be RepoViewBinding")
        _text(self.subject, field="claim.subject")
        _identifier(self.dimension, field="claim.dimension")
        if self.kind not in CLAIM_KINDS:
            _fail("claim_kind_invalid", f"unsupported claim kind: {self.kind}")
        if self.authority not in CLAIM_AUTHORITIES:
            _fail("claim_authority_invalid", f"unsupported claim authority: {self.authority}")
        if self.kind == "FACT" and self.authority != "EXACT_SOURCE":
            _fail("claim_authority_mismatch", "FACT claims must be exact-source claims")
        if self.kind != "FACT" and self.authority != "DERIVED":
            _fail("claim_authority_mismatch", "non-FACT claims must remain visibly derived")
        _text(self.statement, field="claim.statement")
        if not self.evidence_refs and self.kind in {"FACT", "INFERENCE"}:
            _fail("claim_evidence_required", "FACT and INFERENCE claims require evidence references")
        if self.kind == "FACT" and any(_DIGEST.fullmatch(reference) is None for reference in self.evidence_refs):
            _fail("claim_evidence_not_exact", "FACT evidence references must be exact sha256-bound IDs")
        for reference in (*self.evidence_refs, *self.basis_refs):
            _text(reference, field="claim.reference", max_length=512)
        _identifier(self.producer_id, field="claim.producer_id")
        _identifier(self.producer_version, field="claim.producer_version")
        if self.claim_id != _record_digest(self._identity_payload()):
            _fail("claim_integrity_failure", "claim_id does not match its semantic identity")

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema": CLAIM_SCHEMA,
            "repo_view": self.repo_view.as_dict(),
            "subject": self.subject,
            "dimension": self.dimension,
            "kind": self.kind,
            "authority": self.authority,
            "statement": self.statement,
            "evidence_refs": list(self.evidence_refs),
            "basis_refs": list(self.basis_refs),
            "producer_id": self.producer_id,
            "producer_version": self.producer_version,
        }

    def as_dict(self) -> dict[str, Any]:
        return {"schema": CLAIM_SCHEMA, "claim_id": self.claim_id, **self._identity_payload()}

    @classmethod
    def create(
        cls,
        repo_view: CommittedRepoView | RepoViewBinding,
        *,
        subject: str,
        dimension: str,
        kind: str,
        statement: str,
        evidence_refs: Sequence[str] = (),
        basis_refs: Sequence[str] = (),
        producer_id: str = M2C_PRODUCER_ID,
        producer_version: str = M2C_PRODUCER_VERSION,
    ) -> "UnderstandingClaim":
        binding = RepoViewBinding.from_view(repo_view) if isinstance(repo_view, CommittedRepoView) else repo_view
        if not isinstance(binding, RepoViewBinding):
            _fail("malformed_repo_view_basis", "claim requires an exact RepoView binding")
        identity = {
            "schema": CLAIM_SCHEMA,
            "repo_view": binding.as_dict(),
            "subject": subject,
            "dimension": dimension,
            "kind": kind,
            "authority": "EXACT_SOURCE" if kind == "FACT" else "DERIVED",
            "statement": statement,
            "evidence_refs": list(evidence_refs),
            "basis_refs": list(basis_refs),
            "producer_id": producer_id,
            "producer_version": producer_version,
        }
        return cls(
            _record_digest(identity),
            binding,
            subject,
            dimension,
            kind,
            identity["authority"],
            statement,
            tuple(evidence_refs),
            tuple(basis_refs),
            producer_id,
            producer_version,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "UnderstandingClaim":
        _exact_fields(
            value,
            {
                "schema",
                "claim_id",
                "repo_view",
                "subject",
                "dimension",
                "kind",
                "authority",
                "statement",
                "evidence_refs",
                "basis_refs",
                "producer_id",
                "producer_version",
            },
            field="claim",
        )
        if value["schema"] != CLAIM_SCHEMA:
            _fail("schema_mismatch", "unsupported claim schema")
        return cls(
            value["claim_id"],
            _parse_repo_binding(value["repo_view"]),
            value["subject"],
            value["dimension"],
            value["kind"],
            value["authority"],
            value["statement"],
            _sequence(value["evidence_refs"], field="claim.evidence_refs"),
            _sequence(value["basis_refs"], field="claim.basis_refs"),
            value["producer_id"],
            value["producer_version"],
        )


@dataclass(frozen=True)
class Unknown:
    unknown_id: str
    repo_view: RepoViewBinding
    subject: str
    dimension: str
    reason: str
    material: bool = True
    producer_id: str = M2C_PRODUCER_ID
    producer_version: str = M2C_PRODUCER_VERSION

    def __post_init__(self) -> None:
        _digest(self.unknown_id, field="unknown_id")
        if not isinstance(self.repo_view, RepoViewBinding):
            _fail("malformed_unknown", "unknown repo_view must be RepoViewBinding")
        _text(self.subject, field="unknown.subject")
        _identifier(self.dimension, field="unknown.dimension")
        _text(self.reason, field="unknown.reason")
        if not isinstance(self.material, bool):
            _fail("malformed_unknown", "unknown.material must be boolean")
        _identifier(self.producer_id, field="unknown.producer_id")
        _identifier(self.producer_version, field="unknown.producer_version")
        if self.unknown_id != _record_digest(self._identity_payload()):
            _fail("unknown_integrity_failure", "unknown_id does not match its semantic identity")

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema": UNKNOWN_SCHEMA,
            "repo_view": self.repo_view.as_dict(),
            "subject": self.subject,
            "dimension": self.dimension,
            "reason": self.reason,
            "material": self.material,
            "producer_id": self.producer_id,
            "producer_version": self.producer_version,
        }

    def as_dict(self) -> dict[str, Any]:
        return {"schema": UNKNOWN_SCHEMA, "unknown_id": self.unknown_id, **self._identity_payload()}

    @classmethod
    def create(
        cls,
        repo_view: CommittedRepoView | RepoViewBinding,
        *,
        subject: str,
        dimension: str,
        reason: str,
        material: bool = True,
    ) -> "Unknown":
        binding = RepoViewBinding.from_view(repo_view) if isinstance(repo_view, CommittedRepoView) else repo_view
        if not isinstance(binding, RepoViewBinding):
            _fail("malformed_repo_view_basis", "unknown requires an exact RepoView binding")
        identity = {
            "schema": UNKNOWN_SCHEMA,
            "repo_view": binding.as_dict(),
            "subject": subject,
            "dimension": dimension,
            "reason": reason,
            "material": material,
            "producer_id": M2C_PRODUCER_ID,
            "producer_version": M2C_PRODUCER_VERSION,
        }
        return cls(_record_digest(identity), binding, subject, dimension, reason, material)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Unknown":
        _exact_fields(
            value,
            {"schema", "unknown_id", "repo_view", "subject", "dimension", "reason", "material", "producer_id", "producer_version"},
            field="unknown",
        )
        if value["schema"] != UNKNOWN_SCHEMA:
            _fail("schema_mismatch", "unsupported unknown schema")
        return cls(
            value["unknown_id"],
            _parse_repo_binding(value["repo_view"]),
            value["subject"],
            value["dimension"],
            value["reason"],
            value["material"],
            value["producer_id"],
            value["producer_version"],
        )


@dataclass(frozen=True)
class Omission:
    omission_id: str
    repo_view: RepoViewBinding
    dimension: str
    reason: str
    policy_denied: bool = False
    producer_id: str = M2C_PRODUCER_ID
    producer_version: str = M2C_PRODUCER_VERSION

    def __post_init__(self) -> None:
        _digest(self.omission_id, field="omission_id")
        if not isinstance(self.repo_view, RepoViewBinding):
            _fail("malformed_omission", "omission repo_view must be RepoViewBinding")
        _identifier(self.dimension, field="omission.dimension")
        _text(self.reason, field="omission.reason")
        if not isinstance(self.policy_denied, bool):
            _fail("malformed_omission", "omission.policy_denied must be boolean")
        _identifier(self.producer_id, field="omission.producer_id")
        _identifier(self.producer_version, field="omission.producer_version")
        if self.omission_id != _record_digest(self._identity_payload()):
            _fail("omission_integrity_failure", "omission_id does not match its semantic identity")

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema": OMISSION_SCHEMA,
            "repo_view": self.repo_view.as_dict(),
            "dimension": self.dimension,
            "reason": self.reason,
            "policy_denied": self.policy_denied,
            "producer_id": self.producer_id,
            "producer_version": self.producer_version,
        }

    def as_dict(self) -> dict[str, Any]:
        return {"schema": OMISSION_SCHEMA, "omission_id": self.omission_id, **self._identity_payload()}

    @classmethod
    def create(
        cls,
        repo_view: CommittedRepoView | RepoViewBinding,
        *,
        dimension: str,
        reason: str,
        policy_denied: bool = False,
    ) -> "Omission":
        binding = RepoViewBinding.from_view(repo_view) if isinstance(repo_view, CommittedRepoView) else repo_view
        if not isinstance(binding, RepoViewBinding):
            _fail("malformed_repo_view_basis", "omission requires an exact RepoView binding")
        identity = {
            "schema": OMISSION_SCHEMA,
            "repo_view": binding.as_dict(),
            "dimension": dimension,
            "reason": reason,
            "policy_denied": policy_denied,
            "producer_id": M2C_PRODUCER_ID,
            "producer_version": M2C_PRODUCER_VERSION,
        }
        return cls(_record_digest(identity), binding, dimension, reason, policy_denied)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Omission":
        _exact_fields(
            value,
            {"schema", "omission_id", "repo_view", "dimension", "reason", "policy_denied", "producer_id", "producer_version"},
            field="omission",
        )
        if value["schema"] != OMISSION_SCHEMA:
            _fail("schema_mismatch", "unsupported omission schema")
        return cls(
            value["omission_id"],
            _parse_repo_binding(value["repo_view"]),
            value["dimension"],
            value["reason"],
            value["policy_denied"],
            value["producer_id"],
            value["producer_version"],
        )


@dataclass(frozen=True)
class ClaimContradiction:
    contradiction_id: str
    repo_view: RepoViewBinding
    subject: str
    dimension: str
    claim_ids: tuple[str, ...]
    source_claim_ids: tuple[str, ...]
    derived_claim_ids: tuple[str, ...]
    reason: str
    source_authority: str
    producer_id: str = M2C_PRODUCER_ID
    producer_version: str = M2C_PRODUCER_VERSION

    def __post_init__(self) -> None:
        _digest(self.contradiction_id, field="contradiction_id")
        if not isinstance(self.repo_view, RepoViewBinding):
            _fail("malformed_contradiction", "contradiction repo_view must be RepoViewBinding")
        _text(self.subject, field="contradiction.subject")
        _identifier(self.dimension, field="contradiction.dimension")
        for field, values in (
            ("contradiction.claim_ids", self.claim_ids),
            ("contradiction.source_claim_ids", self.source_claim_ids),
            ("contradiction.derived_claim_ids", self.derived_claim_ids),
        ):
            if not values or len(set(values)) != len(values):
                _fail("malformed_contradiction", f"{field} must contain unique claim IDs")
            for value in values:
                _digest(value, field=f"{field}[]")
        if not set(self.source_claim_ids).issubset(self.claim_ids) or not set(self.derived_claim_ids).issubset(self.claim_ids):
            _fail("malformed_contradiction", "contradiction source/derived claims must be members of claim_ids")
        if set(self.source_claim_ids) & set(self.derived_claim_ids):
            _fail("malformed_contradiction", "a claim cannot be both source and derived")
        expected_authority = "EXACT_SOURCE_WINS" if self.source_claim_ids else "NO_EXACT_SOURCE_CLAIM"
        if self.source_authority != expected_authority:
            _fail("source_authority_mismatch", "contradiction source authority is not explicit")
        _text(self.reason, field="contradiction.reason")
        _identifier(self.producer_id, field="contradiction.producer_id")
        _identifier(self.producer_version, field="contradiction.producer_version")
        if self.contradiction_id != _record_digest(self._identity_payload()):
            _fail("contradiction_integrity_failure", "contradiction_id does not match its semantic identity")

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema": CONTRADICTION_SCHEMA,
            "repo_view": self.repo_view.as_dict(),
            "subject": self.subject,
            "dimension": self.dimension,
            "claim_ids": list(self.claim_ids),
            "source_claim_ids": list(self.source_claim_ids),
            "derived_claim_ids": list(self.derived_claim_ids),
            "reason": self.reason,
            "source_authority": self.source_authority,
            "producer_id": self.producer_id,
            "producer_version": self.producer_version,
        }

    def as_dict(self) -> dict[str, Any]:
        return {"schema": CONTRADICTION_SCHEMA, "contradiction_id": self.contradiction_id, **self._identity_payload()}

    @classmethod
    def create(
        cls,
        repo_view: CommittedRepoView | RepoViewBinding,
        *,
        subject: str,
        dimension: str,
        claim_ids: Sequence[str],
        source_claim_ids: Sequence[str] = (),
        derived_claim_ids: Sequence[str] = (),
        reason: str,
    ) -> "ClaimContradiction":
        binding = RepoViewBinding.from_view(repo_view) if isinstance(repo_view, CommittedRepoView) else repo_view
        if not isinstance(binding, RepoViewBinding):
            _fail("malformed_repo_view_basis", "contradiction requires an exact RepoView binding")
        source = tuple(source_claim_ids)
        identity = {
            "schema": CONTRADICTION_SCHEMA,
            "repo_view": binding.as_dict(),
            "subject": subject,
            "dimension": dimension,
            "claim_ids": list(claim_ids),
            "source_claim_ids": list(source),
            "derived_claim_ids": list(derived_claim_ids),
            "reason": reason,
            "source_authority": "EXACT_SOURCE_WINS" if source else "NO_EXACT_SOURCE_CLAIM",
            "producer_id": M2C_PRODUCER_ID,
            "producer_version": M2C_PRODUCER_VERSION,
        }
        return cls(
            _record_digest(identity),
            binding,
            subject,
            dimension,
            tuple(claim_ids),
            source,
            tuple(derived_claim_ids),
            reason,
            identity["source_authority"],
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ClaimContradiction":
        _exact_fields(
            value,
            {"schema", "contradiction_id", "repo_view", "subject", "dimension", "claim_ids", "source_claim_ids", "derived_claim_ids", "reason", "source_authority", "producer_id", "producer_version"},
            field="contradiction",
        )
        if value["schema"] != CONTRADICTION_SCHEMA:
            _fail("schema_mismatch", "unsupported contradiction schema")
        return cls(
            value["contradiction_id"],
            _parse_repo_binding(value["repo_view"]),
            value["subject"],
            value["dimension"],
            _sequence(value["claim_ids"], field="contradiction.claim_ids", allow_empty=False),
            _sequence(value["source_claim_ids"], field="contradiction.source_claim_ids"),
            _sequence(value["derived_claim_ids"], field="contradiction.derived_claim_ids", allow_empty=False),
            value["reason"],
            value["source_authority"],
            value["producer_id"],
            value["producer_version"],
        )


@dataclass(frozen=True)
class ContextAffordance:
    """An on-demand semantic affordance, not a transport retry record."""

    affordance_id: str
    dimension: str
    horizon: str
    evidence_type: str
    reason: str
    producer_id: str = M2C_PRODUCER_ID
    producer_version: str = M2C_PRODUCER_VERSION

    def __post_init__(self) -> None:
        _digest(self.affordance_id, field="affordance_id")
        _identifier(self.dimension, field="affordance.dimension")
        if self.horizon not in HORIZONS:
            _fail("horizon_invalid", f"unsupported affordance horizon: {self.horizon}")
        _identifier(self.evidence_type, field="affordance.evidence_type")
        _text(self.reason, field="affordance.reason")
        _identifier(self.producer_id, field="affordance.producer_id")
        _identifier(self.producer_version, field="affordance.producer_version")
        if self.affordance_id != _record_digest(self._identity_payload()):
            _fail("affordance_integrity_failure", "affordance_id does not match its semantic identity")

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema": AFFORDANCE_SCHEMA,
            "dimension": self.dimension,
            "horizon": self.horizon,
            "evidence_type": self.evidence_type,
            "reason": self.reason,
            "producer_id": self.producer_id,
            "producer_version": self.producer_version,
        }

    def as_dict(self) -> dict[str, Any]:
        return {"schema": AFFORDANCE_SCHEMA, "affordance_id": self.affordance_id, **self._identity_payload()}

    @classmethod
    def create(cls, *, dimension: str, horizon: str, evidence_type: str, reason: str) -> "ContextAffordance":
        identity = {
            "schema": AFFORDANCE_SCHEMA,
            "dimension": dimension,
            "horizon": horizon,
            "evidence_type": evidence_type,
            "reason": reason,
            "producer_id": M2C_PRODUCER_ID,
            "producer_version": M2C_PRODUCER_VERSION,
        }
        return cls(_record_digest(identity), dimension, horizon, evidence_type, reason)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ContextAffordance":
        _exact_fields(
            value,
            {"schema", "affordance_id", "dimension", "horizon", "evidence_type", "reason", "producer_id", "producer_version"},
            field="affordance",
        )
        if value["schema"] != AFFORDANCE_SCHEMA:
            _fail("schema_mismatch", "unsupported affordance schema")
        return cls(
            value["affordance_id"],
            value["dimension"],
            value["horizon"],
            value["evidence_type"],
            value["reason"],
            value["producer_id"],
            value["producer_version"],
        )


@dataclass(frozen=True)
class RepositoryUnderstandingView:
    """Rebuildable exact-RepoView claim projection, never source-byte authority."""

    understanding_id: str
    intent_basis: IntentBasis
    repo_view: RepoViewBinding
    claims: tuple[UnderstandingClaim, ...]
    requested_dimensions: tuple[str, ...]
    covered_dimensions: tuple[str, ...]
    must_see_categories: tuple[str, ...]
    covered_must_see: tuple[str, ...]
    unknowns: tuple[Unknown, ...]
    omissions: tuple[Omission, ...]
    contradictions: tuple[ClaimContradiction, ...]
    invalidation_predicates: tuple[str, ...]
    producer_id: str = M2C_PRODUCER_ID
    producer_version: str = M2C_PRODUCER_VERSION

    def __post_init__(self) -> None:
        _digest(self.understanding_id, field="understanding_id")
        if not isinstance(self.intent_basis, IntentBasis) or not isinstance(self.repo_view, RepoViewBinding):
            _fail("malformed_understanding", "understanding requires typed intent and RepoView basis")
        _validate_unique_records(self.claims, "claim_id", field="understanding.claims")
        _validate_unique_records(self.unknowns, "unknown_id", field="understanding.unknowns")
        _validate_unique_records(self.omissions, "omission_id", field="understanding.omissions")
        _validate_unique_records(self.contradictions, "contradiction_id", field="understanding.contradictions")
        for claim in self.claims:
            if not isinstance(claim, UnderstandingClaim) or not _same_repo(claim.repo_view, self.repo_view):
                _fail("understanding_basis_mismatch", "all claims must bind the exact Understanding RepoView")
        for unknown in self.unknowns:
            if not isinstance(unknown, Unknown) or not _same_repo(unknown.repo_view, self.repo_view):
                _fail("understanding_basis_mismatch", "all unknowns must bind the exact Understanding RepoView")
        for omission in self.omissions:
            if not isinstance(omission, Omission) or not _same_repo(omission.repo_view, self.repo_view):
                _fail("understanding_basis_mismatch", "all omissions must bind the exact Understanding RepoView")
        claim_by_id = {claim.claim_id: claim for claim in self.claims}
        for contradiction in self.contradictions:
            if not isinstance(contradiction, ClaimContradiction) or not _same_repo(contradiction.repo_view, self.repo_view):
                _fail("understanding_basis_mismatch", "contradictions must bind the exact Understanding RepoView")
            if not set(contradiction.claim_ids).issubset(claim_by_id):
                _fail("contradiction_claim_missing", "contradiction references a claim not present in Understanding")
            for claim_id in contradiction.source_claim_ids:
                if claim_by_id[claim_id].kind != "FACT" or claim_by_id[claim_id].authority != "EXACT_SOURCE":
                    _fail("source_authority_mismatch", "source contradiction claims must be exact FACT claims")
            for claim_id in contradiction.derived_claim_ids:
                if claim_by_id[claim_id].kind == "FACT" or claim_by_id[claim_id].authority != "DERIVED":
                    _fail("source_authority_mismatch", "derived contradiction claims cannot be FACT claims")
        _validate_coverage_fields(
            self.requested_dimensions,
            self.covered_dimensions,
            self.must_see_categories,
            self.covered_must_see,
        )
        if not self.invalidation_predicates:
            _fail("invalidation_predicates_required", "Understanding must expose invalidation predicates")
        _identifier(self.producer_id, field="understanding.producer_id")
        _identifier(self.producer_version, field="understanding.producer_version")
        self._validate_conflicts()
        if self.understanding_id != _record_digest(self._identity_payload()):
            _fail("understanding_integrity_failure", "understanding_id does not match its semantic identity")

    def _validate_conflicts(self) -> None:
        groups: dict[tuple[str, str], list[UnderstandingClaim]] = {}
        for claim in self.claims:
            groups.setdefault((claim.dimension, claim.subject), []).append(claim)
        represented = [set(item.claim_ids) for item in self.contradictions]
        for group in groups.values():
            if len({claim.statement for claim in group}) <= 1:
                continue
            expected = {claim.claim_id for claim in group}
            if expected not in represented:
                _fail(
                    "contradiction_required",
                    "conflicting claims must remain explicitly represented as a contradiction",
                    details={"claim_ids": sorted(expected)},
                )

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema": UNDERSTANDING_SCHEMA,
            "intent_basis": self.intent_basis.as_dict(),
            "repo_view": self.repo_view.as_dict(),
            "claims": [claim.as_dict() for claim in self.claims],
            "requested_dimensions": list(self.requested_dimensions),
            "covered_dimensions": list(self.covered_dimensions),
            "must_see_categories": list(self.must_see_categories),
            "covered_must_see": list(self.covered_must_see),
            "unknowns": [item.as_dict() for item in self.unknowns],
            "omissions": [item.as_dict() for item in self.omissions],
            "contradictions": [item.as_dict() for item in self.contradictions],
            "invalidation_predicates": list(self.invalidation_predicates),
            "producer_id": self.producer_id,
            "producer_version": self.producer_version,
        }

    @property
    def coverage_status(self) -> Literal["COMPLETE", "PARTIAL", "BLOCKED"]:
        return _coverage_state(
            self.requested_dimensions,
            self.covered_dimensions,
            self.must_see_categories,
            self.covered_must_see,
            self.unknowns,
            self.omissions,
            self.contradictions,
        )

    @property
    def gap_ids(self) -> tuple[str, ...]:
        return tuple(item.unknown_id for item in self.unknowns) + tuple(item.omission_id for item in self.omissions)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": UNDERSTANDING_SCHEMA,
            "understanding_id": self.understanding_id,
            "intent_basis": self.intent_basis.as_dict(),
            "repo_view": self.repo_view.as_dict(),
            "claims": [claim.as_dict() for claim in self.claims],
            "requested_dimensions": list(self.requested_dimensions),
            "covered_dimensions": list(self.covered_dimensions),
            "must_see_categories": list(self.must_see_categories),
            "covered_must_see": list(self.covered_must_see),
            "unknowns": [item.as_dict() for item in self.unknowns],
            "omissions": [item.as_dict() for item in self.omissions],
            "contradictions": [item.as_dict() for item in self.contradictions],
            "invalidation_predicates": list(self.invalidation_predicates),
            "coverage_status": self.coverage_status,
            "producer_id": self.producer_id,
            "producer_version": self.producer_version,
        }

    def to_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def create(
        cls,
        intent_basis: IntentBasis,
        repo_view: CommittedRepoView | RepoViewBinding,
        *,
        claims: Sequence[UnderstandingClaim] = (),
        requested_dimensions: Sequence[str],
        covered_dimensions: Sequence[str] = (),
        must_see_categories: Sequence[str] = (),
        covered_must_see: Sequence[str] = (),
        unknowns: Sequence[Unknown] = (),
        omissions: Sequence[Omission] = (),
        contradictions: Sequence[ClaimContradiction] = (),
        invalidation_predicates: Sequence[str] = (
            "repo_view.view_id_changed",
            "intent_basis.changed",
            "producer_or_schema.changed",
        ),
        producer_id: str = M2C_PRODUCER_ID,
        producer_version: str = M2C_PRODUCER_VERSION,
    ) -> "RepositoryUnderstandingView":
        if not isinstance(intent_basis, IntentBasis):
            _fail("malformed_intent_basis", "Understanding requires IntentBasis")
        binding = RepoViewBinding.from_view(repo_view) if isinstance(repo_view, CommittedRepoView) else repo_view
        if not isinstance(binding, RepoViewBinding):
            _fail("malformed_repo_view_basis", "Understanding requires an exact RepoView binding")
        identity = {
            "schema": UNDERSTANDING_SCHEMA,
            "intent_basis": intent_basis.as_dict(),
            "repo_view": binding.as_dict(),
            "claims": [item.as_dict() for item in claims],
            "requested_dimensions": list(requested_dimensions),
            "covered_dimensions": list(covered_dimensions),
            "must_see_categories": list(must_see_categories),
            "covered_must_see": list(covered_must_see),
            "unknowns": [item.as_dict() for item in unknowns],
            "omissions": [item.as_dict() for item in omissions],
            "contradictions": [item.as_dict() for item in contradictions],
            "invalidation_predicates": list(invalidation_predicates),
            "producer_id": producer_id,
            "producer_version": producer_version,
        }
        return cls(
            _record_digest(identity),
            intent_basis,
            binding,
            tuple(claims),
            tuple(requested_dimensions),
            tuple(covered_dimensions),
            tuple(must_see_categories),
            tuple(covered_must_see),
            tuple(unknowns),
            tuple(omissions),
            tuple(contradictions),
            tuple(invalidation_predicates),
            producer_id,
            producer_version,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RepositoryUnderstandingView":
        _exact_fields(
            value,
            {
                "schema",
                "understanding_id",
                "intent_basis",
                "repo_view",
                "claims",
                "requested_dimensions",
                "covered_dimensions",
                "must_see_categories",
                "covered_must_see",
                "unknowns",
                "omissions",
                "contradictions",
                "invalidation_predicates",
                "coverage_status",
                "producer_id",
                "producer_version",
            },
            field="understanding",
        )
        if value["schema"] != UNDERSTANDING_SCHEMA:
            _fail("schema_mismatch", "unsupported RepositoryUnderstanding schema")
        result = cls.create(
            _parse_intent(value["intent_basis"]),
            _parse_repo_binding(value["repo_view"]),
            claims=tuple(UnderstandingClaim.from_mapping(item) for item in value["claims"]),
            requested_dimensions=_identifier_sequence(value["requested_dimensions"], field="understanding.requested_dimensions"),
            covered_dimensions=_identifier_sequence(value["covered_dimensions"], field="understanding.covered_dimensions"),
            must_see_categories=_identifier_sequence(value["must_see_categories"], field="understanding.must_see_categories"),
            covered_must_see=_identifier_sequence(value["covered_must_see"], field="understanding.covered_must_see"),
            unknowns=tuple(Unknown.from_mapping(item) for item in value["unknowns"]),
            omissions=tuple(Omission.from_mapping(item) for item in value["omissions"]),
            contradictions=tuple(ClaimContradiction.from_mapping(item) for item in value["contradictions"]),
            invalidation_predicates=_identifier_sequence(value["invalidation_predicates"], field="understanding.invalidation_predicates", allow_empty=False),
            producer_id=value["producer_id"],
            producer_version=value["producer_version"],
        )
        if value["coverage_status"] != result.coverage_status:
            _fail("coverage_status_mismatch", "coverage_status must be mechanically derived")
        if value["understanding_id"] != result.understanding_id:
            _fail("understanding_integrity_failure", "understanding_id differs from exact record identity")
        return result


def _validate_unique_records(records: Sequence[object], attribute: str, *, field: str) -> None:
    values = [getattr(record, attribute, None) for record in records]
    if len(values) != len(set(values)):
        _fail("duplicate_m2c_value", f"{field} contains duplicate identities")


def _validate_coverage_fields(
    requested_dimensions: Sequence[str],
    covered_dimensions: Sequence[str],
    must_see_categories: Sequence[str],
    covered_must_see: Sequence[str],
) -> None:
    for field, values in (
        ("requested_dimensions", requested_dimensions),
        ("covered_dimensions", covered_dimensions),
        ("must_see_categories", must_see_categories),
        ("covered_must_see", covered_must_see),
    ):
        _identifier_sequence(values, field=field)
    if not set(covered_dimensions).issubset(requested_dimensions):
        _fail("coverage_overclaim", "covered dimensions must be requested dimensions")
    if not set(covered_must_see).issubset(must_see_categories):
        _fail("coverage_overclaim", "covered must-see categories must be required categories")


@dataclass(frozen=True)
class ContextPackage:
    """Immutable quality capsule referring to typed fragments, never duplicating bytes."""

    package_id: str
    intent_basis: IntentBasis
    repo_view: RepoViewBinding
    understanding_id: str
    horizon: str
    requested_dimensions: tuple[str, ...]
    covered_dimensions: tuple[str, ...]
    must_see_categories: tuple[str, ...]
    covered_must_see: tuple[str, ...]
    unknowns: tuple[Unknown, ...]
    omissions: tuple[Omission, ...]
    contradictions: tuple[ClaimContradiction, ...]
    included_fragment_ids: tuple[str, ...]
    affordances: tuple[ContextAffordance, ...]
    architecture_constraints_included: tuple[str, ...]
    invalidation_predicates: tuple[str, ...]
    policy_version: str = M2C_POLICY_VERSION
    producer_id: str = M2C_PRODUCER_ID
    producer_version: str = M2C_PRODUCER_VERSION
    fact_claim_ids: tuple[str, ...] = ()
    inference_claim_ids: tuple[str, ...] = ()
    assumption_claim_ids: tuple[str, ...] = ()
    hypothesis_claim_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _digest(self.package_id, field="package_id")
        _digest(self.understanding_id, field="understanding_id")
        if not isinstance(self.intent_basis, IntentBasis) or not isinstance(self.repo_view, RepoViewBinding):
            _fail("malformed_context_package", "ContextPackage requires typed intent and RepoView basis")
        if self.horizon not in HORIZONS:
            _fail("horizon_invalid", f"unsupported context horizon: {self.horizon}")
        _validate_coverage_fields(
            self.requested_dimensions,
            self.covered_dimensions,
            self.must_see_categories,
            self.covered_must_see,
        )
        _validate_unique_records(self.unknowns, "unknown_id", field="package.unknowns")
        _validate_unique_records(self.omissions, "omission_id", field="package.omissions")
        _validate_unique_records(self.contradictions, "contradiction_id", field="package.contradictions")
        _validate_unique_records(self.affordances, "affordance_id", field="package.affordances")
        for unknown in self.unknowns:
            if not isinstance(unknown, Unknown) or not _same_repo(unknown.repo_view, self.repo_view):
                _fail("context_package_basis_mismatch", "package unknowns must bind the package RepoView")
        for omission in self.omissions:
            if not isinstance(omission, Omission) or not _same_repo(omission.repo_view, self.repo_view):
                _fail("context_package_basis_mismatch", "package omissions must bind the package RepoView")
        for contradiction in self.contradictions:
            if not isinstance(contradiction, ClaimContradiction) or not _same_repo(contradiction.repo_view, self.repo_view):
                _fail("context_package_basis_mismatch", "package contradictions must bind the package RepoView")
        for fragment_id in self.included_fragment_ids:
            _digest(fragment_id, field="package.included_fragment_ids[]")
        _identifier_sequence(self.architecture_constraints_included, field="package.architecture_constraints_included")
        _identifier_sequence(self.invalidation_predicates, field="package.invalidation_predicates", allow_empty=False)
        _identifier(self.policy_version, field="package.policy_version")
        _identifier(self.producer_id, field="package.producer_id")
        _identifier(self.producer_version, field="package.producer_version")
        for field, values in (
            ("package.fact_claim_ids", self.fact_claim_ids),
            ("package.inference_claim_ids", self.inference_claim_ids),
            ("package.assumption_claim_ids", self.assumption_claim_ids),
            ("package.hypothesis_claim_ids", self.hypothesis_claim_ids),
        ):
            _digest_sequence(values, field=field)
        if len({claim_id for values in (self.fact_claim_ids, self.inference_claim_ids, self.assumption_claim_ids, self.hypothesis_claim_ids) for claim_id in values}) != sum(
            len(values) for values in (self.fact_claim_ids, self.inference_claim_ids, self.assumption_claim_ids, self.hypothesis_claim_ids)
        ):
            _fail("package_claim_overlap", "ContextPackage claim classifications must be disjoint")
        if self.package_id != _record_digest(self._identity_payload()):
            _fail("context_package_integrity_failure", "package_id does not match its semantic identity")

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema": CONTEXT_PACKAGE_SCHEMA,
            "intent_basis": self.intent_basis.as_dict(),
            "repo_view": self.repo_view.as_dict(),
            "understanding_id": self.understanding_id,
            "horizon": self.horizon,
            "requested_dimensions": list(self.requested_dimensions),
            "covered_dimensions": list(self.covered_dimensions),
            "must_see_categories": list(self.must_see_categories),
            "covered_must_see": list(self.covered_must_see),
            "unknowns": [item.as_dict() for item in self.unknowns],
            "omissions": [item.as_dict() for item in self.omissions],
            "contradictions": [item.as_dict() for item in self.contradictions],
            "included_fragment_ids": list(self.included_fragment_ids),
            "affordances": [item.as_dict() for item in self.affordances],
            "architecture_constraints_included": list(self.architecture_constraints_included),
            "invalidation_predicates": list(self.invalidation_predicates),
            "policy_version": self.policy_version,
            "producer_id": self.producer_id,
            "producer_version": self.producer_version,
            "fact_claim_ids": list(self.fact_claim_ids),
            "inference_claim_ids": list(self.inference_claim_ids),
            "assumption_claim_ids": list(self.assumption_claim_ids),
            "hypothesis_claim_ids": list(self.hypothesis_claim_ids),
        }

    @property
    def coverage_status(self) -> Literal["COMPLETE", "PARTIAL", "BLOCKED"]:
        return _coverage_state(
            self.requested_dimensions,
            self.covered_dimensions,
            self.must_see_categories,
            self.covered_must_see,
            self.unknowns,
            self.omissions,
            self.contradictions,
        )

    @property
    def gap_ids(self) -> tuple[str, ...]:
        return tuple(item.unknown_id for item in self.unknowns) + tuple(item.omission_id for item in self.omissions)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": CONTEXT_PACKAGE_SCHEMA,
            "package_id": self.package_id,
            "intent_basis": self.intent_basis.as_dict(),
            "repo_view": self.repo_view.as_dict(),
            "understanding_id": self.understanding_id,
            "horizon": self.horizon,
            "requested_dimensions": list(self.requested_dimensions),
            "covered_dimensions": list(self.covered_dimensions),
            "must_see_categories": list(self.must_see_categories),
            "covered_must_see": list(self.covered_must_see),
            "unknowns": [item.as_dict() for item in self.unknowns],
            "omissions": [item.as_dict() for item in self.omissions],
            "contradictions": [item.as_dict() for item in self.contradictions],
            "included_fragment_ids": list(self.included_fragment_ids),
            "affordances": [item.as_dict() for item in self.affordances],
            "architecture_constraints_included": list(self.architecture_constraints_included),
            "invalidation_predicates": list(self.invalidation_predicates),
            "coverage_status": self.coverage_status,
            "policy_version": self.policy_version,
            "producer_id": self.producer_id,
            "producer_version": self.producer_version,
            "fact_claim_ids": list(self.fact_claim_ids),
            "inference_claim_ids": list(self.inference_claim_ids),
            "assumption_claim_ids": list(self.assumption_claim_ids),
            "hypothesis_claim_ids": list(self.hypothesis_claim_ids),
        }

    def to_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def create(
        cls,
        intent_basis: IntentBasis,
        repo_view: CommittedRepoView | RepoViewBinding,
        *,
        understanding_id: str,
        horizon: str,
        requested_dimensions: Sequence[str],
        covered_dimensions: Sequence[str] = (),
        must_see_categories: Sequence[str] = (),
        covered_must_see: Sequence[str] = (),
        unknowns: Sequence[Unknown] = (),
        omissions: Sequence[Omission] = (),
        contradictions: Sequence[ClaimContradiction] = (),
        included_fragment_ids: Sequence[str] = (),
        affordances: Sequence[ContextAffordance] = (),
        architecture_constraints_included: Sequence[str] = (),
        invalidation_predicates: Sequence[str] = (
            "repo_view.view_id_changed",
            "intent_basis.changed",
            "producer_or_schema.changed",
        ),
        policy_version: str = M2C_POLICY_VERSION,
        producer_id: str = M2C_PRODUCER_ID,
        producer_version: str = M2C_PRODUCER_VERSION,
        fact_claim_ids: Sequence[str] = (),
        inference_claim_ids: Sequence[str] = (),
        assumption_claim_ids: Sequence[str] = (),
        hypothesis_claim_ids: Sequence[str] = (),
    ) -> "ContextPackage":
        if not isinstance(intent_basis, IntentBasis):
            _fail("malformed_intent_basis", "ContextPackage requires IntentBasis")
        binding = RepoViewBinding.from_view(repo_view) if isinstance(repo_view, CommittedRepoView) else repo_view
        if not isinstance(binding, RepoViewBinding):
            _fail("malformed_repo_view_basis", "ContextPackage requires an exact RepoView binding")
        identity = {
            "schema": CONTEXT_PACKAGE_SCHEMA,
            "intent_basis": intent_basis.as_dict(),
            "repo_view": binding.as_dict(),
            "understanding_id": understanding_id,
            "horizon": horizon,
            "requested_dimensions": list(requested_dimensions),
            "covered_dimensions": list(covered_dimensions),
            "must_see_categories": list(must_see_categories),
            "covered_must_see": list(covered_must_see),
            "unknowns": [item.as_dict() for item in unknowns],
            "omissions": [item.as_dict() for item in omissions],
            "contradictions": [item.as_dict() for item in contradictions],
            "included_fragment_ids": list(included_fragment_ids),
            "affordances": [item.as_dict() for item in affordances],
            "architecture_constraints_included": list(architecture_constraints_included),
            "invalidation_predicates": list(invalidation_predicates),
            "policy_version": policy_version,
            "producer_id": producer_id,
            "producer_version": producer_version,
            "fact_claim_ids": list(fact_claim_ids),
            "inference_claim_ids": list(inference_claim_ids),
            "assumption_claim_ids": list(assumption_claim_ids),
            "hypothesis_claim_ids": list(hypothesis_claim_ids),
        }
        return cls(
            _record_digest(identity),
            intent_basis,
            binding,
            understanding_id,
            horizon,
            tuple(requested_dimensions),
            tuple(covered_dimensions),
            tuple(must_see_categories),
            tuple(covered_must_see),
            tuple(unknowns),
            tuple(omissions),
            tuple(contradictions),
            tuple(included_fragment_ids),
            tuple(affordances),
            tuple(architecture_constraints_included),
            tuple(invalidation_predicates),
            policy_version,
            producer_id,
            producer_version,
            tuple(fact_claim_ids),
            tuple(inference_claim_ids),
            tuple(assumption_claim_ids),
            tuple(hypothesis_claim_ids),
        )

    @classmethod
    def from_understanding(
        cls,
        understanding: RepositoryUnderstandingView,
        *,
        horizon: str,
        included_fragment_ids: Sequence[str] = (),
        affordances: Sequence[ContextAffordance] = (),
        architecture_constraints_included: Sequence[str] = (),
    ) -> "ContextPackage":
        return cls.create(
            understanding.intent_basis,
            understanding.repo_view,
            understanding_id=understanding.understanding_id,
            horizon=horizon,
            requested_dimensions=understanding.requested_dimensions,
            covered_dimensions=understanding.covered_dimensions,
            must_see_categories=understanding.must_see_categories,
            covered_must_see=understanding.covered_must_see,
            unknowns=understanding.unknowns,
            omissions=understanding.omissions,
            contradictions=understanding.contradictions,
            included_fragment_ids=included_fragment_ids,
            affordances=affordances,
            architecture_constraints_included=architecture_constraints_included,
            invalidation_predicates=understanding.invalidation_predicates,
            producer_id=understanding.producer_id,
            producer_version=understanding.producer_version,
            fact_claim_ids=tuple(claim.claim_id for claim in understanding.claims if claim.kind == "FACT"),
            inference_claim_ids=tuple(claim.claim_id for claim in understanding.claims if claim.kind == "INFERENCE"),
            assumption_claim_ids=tuple(claim.claim_id for claim in understanding.claims if claim.kind == "ASSUMPTION"),
            hypothesis_claim_ids=tuple(claim.claim_id for claim in understanding.claims if claim.kind == "HYPOTHESIS"),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ContextPackage":
        _exact_fields(
            value,
            {
                "schema",
                "package_id",
                "intent_basis",
                "repo_view",
                "understanding_id",
                "horizon",
                "requested_dimensions",
                "covered_dimensions",
                "must_see_categories",
                "covered_must_see",
                "unknowns",
                "omissions",
                "contradictions",
                "included_fragment_ids",
                "affordances",
                "architecture_constraints_included",
                "invalidation_predicates",
                "coverage_status",
                "policy_version",
                "producer_id",
                "producer_version",
                "fact_claim_ids",
                "inference_claim_ids",
                "assumption_claim_ids",
                "hypothesis_claim_ids",
            },
            field="context_package",
        )
        if value["schema"] != CONTEXT_PACKAGE_SCHEMA:
            _fail("schema_mismatch", "unsupported ContextPackage schema")
        result = cls.create(
            _parse_intent(value["intent_basis"]),
            _parse_repo_binding(value["repo_view"]),
            understanding_id=value["understanding_id"],
            horizon=value["horizon"],
            requested_dimensions=_identifier_sequence(value["requested_dimensions"], field="package.requested_dimensions"),
            covered_dimensions=_identifier_sequence(value["covered_dimensions"], field="package.covered_dimensions"),
            must_see_categories=_identifier_sequence(value["must_see_categories"], field="package.must_see_categories"),
            covered_must_see=_identifier_sequence(value["covered_must_see"], field="package.covered_must_see"),
            unknowns=tuple(Unknown.from_mapping(item) for item in value["unknowns"]),
            omissions=tuple(Omission.from_mapping(item) for item in value["omissions"]),
            contradictions=tuple(ClaimContradiction.from_mapping(item) for item in value["contradictions"]),
            included_fragment_ids=_sequence(value["included_fragment_ids"], field="package.included_fragment_ids"),
            affordances=tuple(ContextAffordance.from_mapping(item) for item in value["affordances"]),
            architecture_constraints_included=_identifier_sequence(value["architecture_constraints_included"], field="package.architecture_constraints_included"),
            invalidation_predicates=_identifier_sequence(value["invalidation_predicates"], field="package.invalidation_predicates", allow_empty=False),
            policy_version=value["policy_version"],
            producer_id=value["producer_id"],
            producer_version=value["producer_version"],
            fact_claim_ids=_digest_sequence(value["fact_claim_ids"], field="package.fact_claim_ids"),
            inference_claim_ids=_digest_sequence(value["inference_claim_ids"], field="package.inference_claim_ids"),
            assumption_claim_ids=_digest_sequence(value["assumption_claim_ids"], field="package.assumption_claim_ids"),
            hypothesis_claim_ids=_digest_sequence(value["hypothesis_claim_ids"], field="package.hypothesis_claim_ids"),
        )
        if value["coverage_status"] != result.coverage_status:
            _fail("coverage_status_mismatch", "package coverage_status must be mechanically derived")
        if value["package_id"] != result.package_id:
            _fail("context_package_integrity_failure", "package_id differs from exact record identity")
        return result


@dataclass(frozen=True)
class ContextRequest:
    """Immutable semantic request for more context; never a scheduler item."""

    request_id: str
    intent_basis: IntentBasis
    repo_view: RepoViewBinding
    source_package_id: str
    gap_ids: tuple[str, ...]
    horizon: str
    requested_dimensions: tuple[str, ...]
    requested_evidence: tuple[str, ...]
    question: str
    counterexample: str | None
    reason: str
    producer_id: str = M2C_PRODUCER_ID
    producer_version: str = M2C_PRODUCER_VERSION

    def __post_init__(self) -> None:
        _digest(self.request_id, field="request_id")
        if not isinstance(self.intent_basis, IntentBasis) or not isinstance(self.repo_view, RepoViewBinding):
            _fail("malformed_context_request", "ContextRequest requires typed intent and RepoView basis")
        _digest(self.source_package_id, field="source_package_id")
        _digest_sequence(self.gap_ids, field="request.gap_ids", allow_empty=False)
        if self.horizon not in HORIZONS:
            _fail("horizon_invalid", f"unsupported request horizon: {self.horizon}")
        _identifier_sequence(self.requested_dimensions, field="request.requested_dimensions", allow_empty=False)
        _sequence(self.requested_evidence, field="request.requested_evidence", allow_empty=False)
        _text(self.question, field="request.question")
        if self.counterexample is not None:
            _text(self.counterexample, field="request.counterexample")
        _text(self.reason, field="request.reason")
        _identifier(self.producer_id, field="request.producer_id")
        _identifier(self.producer_version, field="request.producer_version")
        if self.request_id != _record_digest(self._identity_payload()):
            _fail("context_request_integrity_failure", "request_id does not match its semantic identity")

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema": CONTEXT_REQUEST_SCHEMA,
            "intent_basis": self.intent_basis.as_dict(),
            "repo_view": self.repo_view.as_dict(),
            "source_package_id": self.source_package_id,
            "gap_ids": list(self.gap_ids),
            "horizon": self.horizon,
            "requested_dimensions": list(self.requested_dimensions),
            "requested_evidence": list(self.requested_evidence),
            "question": self.question,
            "counterexample": self.counterexample,
            "reason": self.reason,
            "producer_id": self.producer_id,
            "producer_version": self.producer_version,
        }

    def as_dict(self) -> dict[str, Any]:
        return {"schema": CONTEXT_REQUEST_SCHEMA, "request_id": self.request_id, **self._identity_payload()}

    def to_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    def validate_source_package(self, package: ContextPackage) -> None:
        if not isinstance(package, ContextPackage):
            _fail("stale_request_basis", "ContextRequest source package is unavailable")
        if (
            package.package_id != self.source_package_id
            or package.intent_basis != self.intent_basis
            or package.repo_view != self.repo_view
            or not set(self.gap_ids).issubset(package.gap_ids)
        ):
            _fail("stale_request_basis", "ContextRequest is not bound to the exact source ContextPackage")

    @classmethod
    def create(
        cls,
        package: ContextPackage,
        *,
        gap_ids: Sequence[str],
        horizon: str,
        requested_dimensions: Sequence[str],
        requested_evidence: Sequence[str],
        question: str,
        reason: str,
        counterexample: str | None = None,
    ) -> "ContextRequest":
        if not isinstance(package, ContextPackage):
            _fail("malformed_context_request", "ContextRequest requires a source ContextPackage")
        gaps = tuple(gap_ids)
        if not set(gaps).issubset(package.gap_ids) or not gaps:
            _fail("gap_not_visible", "ContextRequest must target visible source-package gaps")
        identity = {
            "schema": CONTEXT_REQUEST_SCHEMA,
            "intent_basis": package.intent_basis.as_dict(),
            "repo_view": package.repo_view.as_dict(),
            "source_package_id": package.package_id,
            "gap_ids": list(gaps),
            "horizon": horizon,
            "requested_dimensions": list(requested_dimensions),
            "requested_evidence": list(requested_evidence),
            "question": question,
            "counterexample": counterexample,
            "reason": reason,
            "producer_id": M2C_PRODUCER_ID,
            "producer_version": M2C_PRODUCER_VERSION,
        }
        return cls(
            _record_digest(identity),
            package.intent_basis,
            package.repo_view,
            package.package_id,
            gaps,
            horizon,
            tuple(requested_dimensions),
            tuple(requested_evidence),
            question,
            counterexample,
            reason,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ContextRequest":
        _exact_fields(
            value,
            {
                "schema",
                "request_id",
                "intent_basis",
                "repo_view",
                "source_package_id",
                "gap_ids",
                "horizon",
                "requested_dimensions",
                "requested_evidence",
                "question",
                "counterexample",
                "reason",
                "producer_id",
                "producer_version",
            },
            field="context_request",
        )
        if value["schema"] != CONTEXT_REQUEST_SCHEMA:
            _fail("schema_mismatch", "unsupported ContextRequest schema")
        return cls(
            value["request_id"],
            _parse_intent(value["intent_basis"]),
            _parse_repo_binding(value["repo_view"]),
            value["source_package_id"],
            _digest_sequence(value["gap_ids"], field="request.gap_ids", allow_empty=False),
            value["horizon"],
            _identifier_sequence(value["requested_dimensions"], field="request.requested_dimensions", allow_empty=False),
            _sequence(value["requested_evidence"], field="request.requested_evidence", allow_empty=False),
            value["question"],
            value["counterexample"],
            value["reason"],
            value["producer_id"],
            value["producer_version"],
        )


@dataclass(frozen=True)
class ContextResolution:
    """Immutable request-to-result linkage; no request lifecycle state machine."""

    resolution_id: str
    request_id: str
    prior_package_id: str
    resulting_package_id: str | None
    outcome: str
    added_fragment_ids: tuple[str, ...]
    resolved_gap_ids: tuple[str, ...]
    unresolved_gap_ids: tuple[str, ...]
    denial_reason: str | None = None
    producer_id: str = M2C_PRODUCER_ID
    producer_version: str = M2C_PRODUCER_VERSION

    def __post_init__(self) -> None:
        _digest(self.resolution_id, field="resolution_id")
        _digest(self.request_id, field="request_id")
        _digest(self.prior_package_id, field="prior_package_id")
        if self.resulting_package_id is not None:
            _digest(self.resulting_package_id, field="resulting_package_id")
        if self.outcome not in RESOLUTION_OUTCOMES:
            _fail("resolution_outcome_invalid", f"unsupported ContextResolution outcome: {self.outcome}")
        _digest_sequence(self.added_fragment_ids, field="resolution.added_fragment_ids")
        _digest_sequence(self.resolved_gap_ids, field="resolution.resolved_gap_ids")
        _digest_sequence(self.unresolved_gap_ids, field="resolution.unresolved_gap_ids")
        if self.outcome == "RESOLVED":
            if self.resulting_package_id is None or not self.added_fragment_ids or not self.resolved_gap_ids:
                _fail("resolution_evidence_required", "RESOLVED linkage requires resulting package, fragments and gaps")
            if self.denial_reason is not None:
                _fail("malformed_context_resolution", "RESOLVED linkage cannot carry denial_reason")
        else:
            if not self.unresolved_gap_ids:
                _fail("resolution_gap_mismatch", "denied/unavailable linkage must keep at least one gap visible")
            if self.resulting_package_id is not None or self.added_fragment_ids or self.resolved_gap_ids:
                _fail("resolution_denial_mismatch", "denied/unavailable linkage cannot claim a result or repair")
            if self.denial_reason is None:
                _fail("resolution_denial_reason_required", "denied/unavailable linkage requires a visible reason")
        _identifier(self.producer_id, field="resolution.producer_id")
        _identifier(self.producer_version, field="resolution.producer_version")
        if self.resolution_id != _record_digest(self._identity_payload()):
            _fail("context_resolution_integrity_failure", "resolution_id does not match its semantic identity")

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema": CONTEXT_RESOLUTION_SCHEMA,
            "request_id": self.request_id,
            "prior_package_id": self.prior_package_id,
            "resulting_package_id": self.resulting_package_id,
            "outcome": self.outcome,
            "added_fragment_ids": list(self.added_fragment_ids),
            "resolved_gap_ids": list(self.resolved_gap_ids),
            "unresolved_gap_ids": list(self.unresolved_gap_ids),
            "denial_reason": self.denial_reason,
            "producer_id": self.producer_id,
            "producer_version": self.producer_version,
        }

    def as_dict(self) -> dict[str, Any]:
        return {"schema": CONTEXT_RESOLUTION_SCHEMA, "resolution_id": self.resolution_id, **self._identity_payload()}

    def to_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def create(
        cls,
        request: ContextRequest,
        prior_package: ContextPackage,
        *,
        resulting_package: ContextPackage | None = None,
        added_fragments: Sequence[TypedContextFragment] = (),
        binding_store: DurableBindingStore | None = None,
        resolved_gap_ids: Sequence[str] = (),
        unresolved_gap_ids: Sequence[str] | None = None,
        outcome: str = "RESOLVED",
        denial_reason: str | None = None,
    ) -> "ContextResolution":
        if not isinstance(request, ContextRequest) or not isinstance(prior_package, ContextPackage):
            _fail("malformed_context_resolution", "resolution requires typed request and prior package")
        request.validate_source_package(prior_package)
        if outcome == "RESOLVED":
            if not isinstance(resulting_package, ContextPackage):
                _fail("resolution_evidence_required", "resolved request requires resulting ContextPackage")
            if resulting_package.intent_basis != prior_package.intent_basis or resulting_package.repo_view != prior_package.repo_view:
                _fail("resolution_basis_mismatch", "resulting package must preserve exact intent and RepoView basis")
            fragments = tuple(added_fragments)
            if binding_store is None or not fragments:
                _fail("resolution_evidence_required", "resolved request requires accepted M2b fragments")
            fragment_ids: list[str] = []
            for fragment in fragments:
                if not isinstance(fragment, TypedContextFragment) or fragment.repo_view != prior_package.repo_view:
                    _fail("resolution_basis_mismatch", "added fragment is not bound to the exact package RepoView")
                accepted = binding_store.resolve_accepted(fragment.fragment_id)
                if accepted.fragment != fragment:
                    _fail("resolution_evidence_required", "added fragment is not the accepted durable binding")
                fragment_ids.append(fragment.fragment_id)
            resolved = tuple(resolved_gap_ids)
            if not resolved or not set(resolved).issubset(request.gap_ids):
                _fail("resolution_gap_mismatch", "resolved gaps must be requested visible gaps")
            if set(resolved) & set(resulting_package.gap_ids):
                _fail("resolution_gap_mismatch", "a resolved gap remains visible in resulting package")
            unresolved = tuple(unresolved_gap_ids or resulting_package.gap_ids)
            if set(unresolved) != set(resulting_package.gap_ids):
                _fail("resolution_gap_mismatch", "unresolved gaps must match resulting package gaps")
            added = tuple(fragment_ids)
            result_id = resulting_package.package_id
        elif outcome in {"DENIED", "UNAVAILABLE"}:
            if resulting_package is not None or added_fragments or resolved_gap_ids:
                _fail("resolution_denial_mismatch", "denied/unavailable request cannot carry repair evidence")
            unresolved = tuple(unresolved_gap_ids or prior_package.gap_ids)
            if not set(request.gap_ids).issubset(unresolved):
                _fail("resolution_gap_mismatch", "denied/unavailable request must keep requested gaps visible")
            if set(unresolved) != set(prior_package.gap_ids):
                _fail("resolution_gap_mismatch", "denied/unavailable linkage must preserve all prior gaps")
            added = ()
            resolved = ()
            result_id = None
        else:
            _fail("resolution_outcome_invalid", f"unsupported ContextResolution outcome: {outcome}")
        identity = {
            "schema": CONTEXT_RESOLUTION_SCHEMA,
            "request_id": request.request_id,
            "prior_package_id": prior_package.package_id,
            "resulting_package_id": result_id,
            "outcome": outcome,
            "added_fragment_ids": list(added),
            "resolved_gap_ids": list(resolved),
            "unresolved_gap_ids": list(unresolved),
            "denial_reason": denial_reason,
            "producer_id": M2C_PRODUCER_ID,
            "producer_version": M2C_PRODUCER_VERSION,
        }
        return cls(
            _record_digest(identity),
            request.request_id,
            prior_package.package_id,
            result_id,
            outcome,
            added,
            resolved,
            unresolved,
            denial_reason,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ContextResolution":
        _exact_fields(
            value,
            {"schema", "resolution_id", "request_id", "prior_package_id", "resulting_package_id", "outcome", "added_fragment_ids", "resolved_gap_ids", "unresolved_gap_ids", "denial_reason", "producer_id", "producer_version"},
            field="context_resolution",
        )
        if value["schema"] != CONTEXT_RESOLUTION_SCHEMA:
            _fail("schema_mismatch", "unsupported ContextResolution schema")
        result = cls(
            value["resolution_id"],
            value["request_id"],
            value["prior_package_id"],
            value["resulting_package_id"],
            value["outcome"],
            _digest_sequence(value["added_fragment_ids"], field="resolution.added_fragment_ids"),
            _digest_sequence(value["resolved_gap_ids"], field="resolution.resolved_gap_ids"),
            _digest_sequence(value["unresolved_gap_ids"], field="resolution.unresolved_gap_ids"),
            value["denial_reason"],
            value["producer_id"],
            value["producer_version"],
        )
        if value["resolution_id"] != result.resolution_id:
            _fail("context_resolution_integrity_failure", "resolution_id differs from exact record identity")
        return result


def _digest_sequence(value: object, *, field: str, allow_empty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        _fail("malformed_m2c_record", f"{field} must be an array")
    result = tuple(_digest(item, field=f"{field}[]") for item in value)
    if not allow_empty and not result:
        _fail("malformed_m2c_record", f"{field} must not be empty")
    if len(set(result)) != len(result):
        _fail("duplicate_m2c_value", f"{field} must contain unique digests")
    return result


@dataclass(frozen=True)
class DecisionOption:
    option_id: str
    summary: str
    tradeoffs: tuple[str, ...]
    risks: tuple[str, ...]

    def __post_init__(self) -> None:
        _identifier(self.option_id, field="option.option_id")
        _text(self.summary, field="option.summary")
        _sequence(self.tradeoffs, field="option.tradeoffs")
        _sequence(self.risks, field="option.risks")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": DECISION_OPTION_SCHEMA,
            "option_id": self.option_id,
            "summary": self.summary,
            "tradeoffs": list(self.tradeoffs),
            "risks": list(self.risks),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "DecisionOption":
        _exact_fields(value, {"schema", "option_id", "summary", "tradeoffs", "risks"}, field="decision_option")
        if value["schema"] != DECISION_OPTION_SCHEMA:
            _fail("schema_mismatch", "unsupported DecisionOption schema")
        return cls(
            value["option_id"],
            value["summary"],
            _sequence(value["tradeoffs"], field="option.tradeoffs"),
            _sequence(value["risks"], field="option.risks"),
        )


@dataclass(frozen=True)
class EngineeringDecision:
    """Concise immutable review/resume evidence without private model reasoning."""

    decision_id: str
    intent_basis: IntentBasis
    repo_view_bases: tuple[RepoViewBinding, ...]
    context_package_ids: tuple[str, ...]
    established_fact_claim_ids: tuple[str, ...]
    inference_claim_ids: tuple[str, ...]
    assumption_claim_ids: tuple[str, ...]
    hypothesis_claim_ids: tuple[str, ...]
    alternatives: tuple[DecisionOption, ...]
    chosen_option_id: str
    must_preserve: tuple[str, ...]
    must_not: tuple[str, ...]
    expected_effect_scope: tuple[str, ...]
    acceptance_obligations: tuple[str, ...]
    evidence_obligations: tuple[str, ...]
    uncertainty: tuple[str, ...]
    requested_context_ids: tuple[str, ...]
    architecture_consequences: tuple[str, ...]
    revisit_triggers: tuple[str, ...]
    producer_id: str = M2C_PRODUCER_ID
    producer_version: str = M2C_PRODUCER_VERSION

    def __post_init__(self) -> None:
        _digest(self.decision_id, field="decision_id")
        if not isinstance(self.intent_basis, IntentBasis) or not self.repo_view_bases:
            _fail("malformed_engineering_decision", "decision requires intent and at least one RepoView basis")
        for binding in self.repo_view_bases:
            if not isinstance(binding, RepoViewBinding):
                _fail("malformed_engineering_decision", "decision RepoView bases must be typed")
        for field, values in (
            ("decision.context_package_ids", self.context_package_ids),
            ("decision.established_fact_claim_ids", self.established_fact_claim_ids),
            ("decision.inference_claim_ids", self.inference_claim_ids),
            ("decision.assumption_claim_ids", self.assumption_claim_ids),
            ("decision.hypothesis_claim_ids", self.hypothesis_claim_ids),
            ("decision.requested_context_ids", self.requested_context_ids),
        ):
            _digest_sequence(values, field=field)
        _validate_unique_claim_lists(self)
        if not self.alternatives:
            _fail("decision_alternatives_required", "EngineeringDecision requires explicit alternatives")
        _validate_unique_records(self.alternatives, "option_id", field="decision.alternatives")
        if self.chosen_option_id not in {option.option_id for option in self.alternatives}:
            _fail("chosen_option_missing", "chosen option must be one of the explicit alternatives")
        _identifier(self.chosen_option_id, field="decision.chosen_option_id")
        for field, values in (
            ("decision.must_preserve", self.must_preserve),
            ("decision.must_not", self.must_not),
            ("decision.expected_effect_scope", self.expected_effect_scope),
            ("decision.acceptance_obligations", self.acceptance_obligations),
            ("decision.evidence_obligations", self.evidence_obligations),
            ("decision.uncertainty", self.uncertainty),
            ("decision.architecture_consequences", self.architecture_consequences),
            ("decision.revisit_triggers", self.revisit_triggers),
        ):
            _sequence(values, field=field)
        _identifier(self.producer_id, field="decision.producer_id")
        _identifier(self.producer_version, field="decision.producer_version")
        if self.decision_id != _record_digest(self._identity_payload()):
            _fail("decision_integrity_failure", "decision_id does not match its semantic identity")

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema": ENGINEERING_DECISION_SCHEMA,
            "intent_basis": self.intent_basis.as_dict(),
            "repo_view_bases": [item.as_dict() for item in self.repo_view_bases],
            "context_package_ids": list(self.context_package_ids),
            "established_fact_claim_ids": list(self.established_fact_claim_ids),
            "inference_claim_ids": list(self.inference_claim_ids),
            "assumption_claim_ids": list(self.assumption_claim_ids),
            "hypothesis_claim_ids": list(self.hypothesis_claim_ids),
            "alternatives": [item.as_dict() for item in self.alternatives],
            "chosen_option_id": self.chosen_option_id,
            "must_preserve": list(self.must_preserve),
            "must_not": list(self.must_not),
            "expected_effect_scope": list(self.expected_effect_scope),
            "acceptance_obligations": list(self.acceptance_obligations),
            "evidence_obligations": list(self.evidence_obligations),
            "uncertainty": list(self.uncertainty),
            "requested_context_ids": list(self.requested_context_ids),
            "architecture_consequences": list(self.architecture_consequences),
            "revisit_triggers": list(self.revisit_triggers),
            "producer_id": self.producer_id,
            "producer_version": self.producer_version,
        }

    def as_dict(self) -> dict[str, Any]:
        return {"schema": ENGINEERING_DECISION_SCHEMA, "decision_id": self.decision_id, **self._identity_payload()}

    def to_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def create(
        cls,
        intent_basis: IntentBasis,
        repo_views: Sequence[CommittedRepoView | RepoViewBinding],
        context_packages: Sequence[ContextPackage],
        *,
        established_fact_claim_ids: Sequence[str] = (),
        inference_claim_ids: Sequence[str] = (),
        assumption_claim_ids: Sequence[str] = (),
        hypothesis_claim_ids: Sequence[str] = (),
        alternatives: Sequence[DecisionOption],
        chosen_option_id: str,
        must_preserve: Sequence[str] = (),
        must_not: Sequence[str] = (),
        expected_effect_scope: Sequence[str] = (),
        acceptance_obligations: Sequence[str] = (),
        evidence_obligations: Sequence[str] = (),
        uncertainty: Sequence[str] = (),
        requested_context_ids: Sequence[str] = (),
        architecture_consequences: Sequence[str] = (),
        revisit_triggers: Sequence[str] = (),
        producer_id: str = M2C_PRODUCER_ID,
        producer_version: str = M2C_PRODUCER_VERSION,
    ) -> "EngineeringDecision":
        if not isinstance(intent_basis, IntentBasis):
            _fail("malformed_intent_basis", "EngineeringDecision requires IntentBasis")
        bindings = tuple(
            RepoViewBinding.from_view(view) if isinstance(view, CommittedRepoView) else view for view in repo_views
        )
        if not bindings or any(not isinstance(item, RepoViewBinding) for item in bindings):
            _fail("malformed_repo_view_basis", "EngineeringDecision requires exact RepoView bindings")
        packages = tuple(context_packages)
        if not packages:
            _fail("decision_context_required", "EngineeringDecision requires at least one ContextPackage basis")
        if any(
            not isinstance(package, ContextPackage)
            or package.intent_basis != intent_basis
            or package.repo_view not in bindings
            for package in packages
        ):
            _fail("decision_basis_mismatch", "decision ContextPackages must share exact intent and RepoView basis")
        identity = {
            "schema": ENGINEERING_DECISION_SCHEMA,
            "intent_basis": intent_basis.as_dict(),
            "repo_view_bases": [item.as_dict() for item in bindings],
            "context_package_ids": [item.package_id for item in packages],
            "established_fact_claim_ids": list(established_fact_claim_ids),
            "inference_claim_ids": list(inference_claim_ids),
            "assumption_claim_ids": list(assumption_claim_ids),
            "hypothesis_claim_ids": list(hypothesis_claim_ids),
            "alternatives": [item.as_dict() for item in alternatives],
            "chosen_option_id": chosen_option_id,
            "must_preserve": list(must_preserve),
            "must_not": list(must_not),
            "expected_effect_scope": list(expected_effect_scope),
            "acceptance_obligations": list(acceptance_obligations),
            "evidence_obligations": list(evidence_obligations),
            "uncertainty": list(uncertainty),
            "requested_context_ids": list(requested_context_ids),
            "architecture_consequences": list(architecture_consequences),
            "revisit_triggers": list(revisit_triggers),
            "producer_id": producer_id,
            "producer_version": producer_version,
        }
        return cls(
            _record_digest(identity),
            intent_basis,
            bindings,
            tuple(item.package_id for item in packages),
            tuple(established_fact_claim_ids),
            tuple(inference_claim_ids),
            tuple(assumption_claim_ids),
            tuple(hypothesis_claim_ids),
            tuple(alternatives),
            chosen_option_id,
            tuple(must_preserve),
            tuple(must_not),
            tuple(expected_effect_scope),
            tuple(acceptance_obligations),
            tuple(evidence_obligations),
            tuple(uncertainty),
            tuple(requested_context_ids),
            tuple(architecture_consequences),
            tuple(revisit_triggers),
            producer_id,
            producer_version,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "EngineeringDecision":
        _exact_fields(
            value,
            {
                "schema",
                "decision_id",
                "intent_basis",
                "repo_view_bases",
                "context_package_ids",
                "established_fact_claim_ids",
                "inference_claim_ids",
                "assumption_claim_ids",
                "hypothesis_claim_ids",
                "alternatives",
                "chosen_option_id",
                "must_preserve",
                "must_not",
                "expected_effect_scope",
                "acceptance_obligations",
                "evidence_obligations",
                "uncertainty",
                "requested_context_ids",
                "architecture_consequences",
                "revisit_triggers",
                "producer_id",
                "producer_version",
            },
            field="engineering_decision",
        )
        if value["schema"] != ENGINEERING_DECISION_SCHEMA:
            _fail("schema_mismatch", "unsupported EngineeringDecision schema")
        result = cls(
            value["decision_id"],
            _parse_intent(value["intent_basis"]),
            tuple(_parse_repo_binding(item, field="decision.repo_view_bases[]") for item in value["repo_view_bases"]),
            _digest_sequence(value["context_package_ids"], field="decision.context_package_ids"),
            _digest_sequence(value["established_fact_claim_ids"], field="decision.established_fact_claim_ids"),
            _digest_sequence(value["inference_claim_ids"], field="decision.inference_claim_ids"),
            _digest_sequence(value["assumption_claim_ids"], field="decision.assumption_claim_ids"),
            _digest_sequence(value["hypothesis_claim_ids"], field="decision.hypothesis_claim_ids"),
            tuple(DecisionOption.from_mapping(item) for item in value["alternatives"]),
            value["chosen_option_id"],
            _sequence(value["must_preserve"], field="decision.must_preserve"),
            _sequence(value["must_not"], field="decision.must_not"),
            _sequence(value["expected_effect_scope"], field="decision.expected_effect_scope"),
            _sequence(value["acceptance_obligations"], field="decision.acceptance_obligations"),
            _sequence(value["evidence_obligations"], field="decision.evidence_obligations"),
            _sequence(value["uncertainty"], field="decision.uncertainty"),
            _digest_sequence(value["requested_context_ids"], field="decision.requested_context_ids"),
            _sequence(value["architecture_consequences"], field="decision.architecture_consequences"),
            _sequence(value["revisit_triggers"], field="decision.revisit_triggers"),
            value["producer_id"],
            value["producer_version"],
        )
        return result


def _validate_unique_claim_lists(decision: EngineeringDecision) -> None:
    groups = (
        decision.established_fact_claim_ids,
        decision.inference_claim_ids,
        decision.assumption_claim_ids,
        decision.hypothesis_claim_ids,
    )
    flattened = [claim_id for group in groups for claim_id in group]
    if len(flattened) != len(set(flattened)):
        _fail("decision_claim_overlap", "a decision claim cannot be classified in multiple epistemic lists")


def validate_decision_applicability(
    decision: EngineeringDecision,
    *,
    intent_basis: IntentBasis,
    repo_views: Sequence[CommittedRepoView | RepoViewBinding],
    context_packages: Sequence[ContextPackage],
) -> None:
    """Reject reuse when any exact intent, RepoView or package basis changed."""

    if decision.intent_basis != intent_basis:
        if decision.intent_basis.intent_revision != intent_basis.intent_revision or decision.intent_basis.intent_digest != intent_basis.intent_digest:
            _fail("stale_intent_basis", "EngineeringDecision intent revision/digest is stale")
        _fail("stale_decision_basis", "EngineeringDecision task identity differs")
    current_views = tuple(
        RepoViewBinding.from_view(view) if isinstance(view, CommittedRepoView) else view for view in repo_views
    )
    for binding in decision.repo_view_bases:
        if binding not in current_views:
            _fail("stale_decision_basis", "EngineeringDecision RepoView commit/tree basis is stale")
    current_packages = {package.package_id: package for package in context_packages}
    for package_id in decision.context_package_ids:
        package = current_packages.get(package_id)
        if package is None:
            _fail("stale_decision_basis", "EngineeringDecision ContextPackage basis is unavailable")
        if package.intent_basis != intent_basis or package.repo_view not in current_views:
            _fail("stale_decision_basis", "EngineeringDecision ContextPackage basis is stale")


def semantic_record_bytes(record: object) -> bytes:
    """Return canonical bytes for one explicit M2c record, with no protocol fields."""

    as_dict = getattr(record, "as_dict", None)
    if not callable(as_dict):
        _fail("invalid_semantic_record", "M2c transport requires a semantic record with as_dict()")
    document = as_dict()
    if not isinstance(document, Mapping) or not isinstance(document.get("schema"), str):
        _fail("invalid_semantic_record", "semantic record must expose a schema")
    return canonical_json_bytes(document)


def semantic_record_content_ref(record: object):
    """Create the M2b ContentRef for canonical M2c record bytes."""

    document = getattr(record, "as_dict", lambda: None)()
    if not isinstance(document, Mapping) or not isinstance(document.get("schema"), str):
        _fail("invalid_semantic_record", "semantic record must expose a schema")
    return make_content_ref(SEMANTIC_RECORD_CONTENT_TYPE, document["schema"], semantic_record_bytes(record))


def publish_semantic_record(
    record: object,
    view: CommittedRepoView,
    content_store: ImmutableContentStore,
    binding_store: DurableBindingStore,
) -> TypedContextFragment:
    """Publish and durably accept one record through the existing M2b substrate."""

    if not isinstance(view, CommittedRepoView):
        _fail("malformed_repo_view_basis", "semantic record publication requires CommittedRepoView")
    raw = semantic_record_bytes(record)
    document = record.as_dict()  # type: ignore[union-attr]
    content_ref = make_content_ref(SEMANTIC_RECORD_CONTENT_TYPE, document["schema"], raw)
    content_store.publish(content_ref, raw)
    fragment = TypedContextFragment.create(
        view,
        content_ref,
        fragment_type=SEMANTIC_RECORD_CONTENT_TYPE,
        fragment_schema=document["schema"],
        payload_size_bytes=len(raw),
    )
    binding_store.accept(fragment, view=view)
    return fragment


def reconstruct_semantic_record(raw: bytes) -> object:
    """Reconstruct only known M2c records from exact canonical bytes."""

    if not isinstance(raw, bytes):
        _fail("invalid_semantic_record", "semantic record bytes must be bytes")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EngineeringIntelligenceError("malformed_semantic_record", "semantic record bytes are not canonical JSON") from exc
    if not isinstance(document, Mapping) or canonical_json_bytes(document) != raw:
        _fail("malformed_semantic_record", "semantic record bytes are not canonical")
    schema = document.get("schema")
    parsers = {
        UNDERSTANDING_SCHEMA: RepositoryUnderstandingView.from_mapping,
        CONTEXT_PACKAGE_SCHEMA: ContextPackage.from_mapping,
        CONTEXT_REQUEST_SCHEMA: ContextRequest.from_mapping,
        CONTEXT_RESOLUTION_SCHEMA: ContextResolution.from_mapping,
        ENGINEERING_DECISION_SCHEMA: EngineeringDecision.from_mapping,
    }
    parser = parsers.get(schema)
    if parser is None:
        _fail("schema_mismatch", "semantic record schema is not an M2c record")
    return parser(document)


def transport_semantic_record(
    record: object,
    view: CommittedRepoView,
    content_store: ImmutableContentStore,
    binding_store: DurableBindingStore,
) -> tuple[TypedContextFragment, object]:
    """Exercise the exact M2b Browser→Native path for one semantic record."""

    fragment = publish_semantic_record(record, view, content_store, binding_store)
    envelope = BrowserTransportProvider().encode(binding_store, fragment, expected_view=view)
    decoded = NativeTransportProvider().decode(envelope, bindings=binding_store, expected_view=view)
    if decoded.fragment != fragment:
        _fail("transport_integrity_failure", "M2b transport returned a different accepted fragment")
    return fragment, reconstruct_semantic_record(decoded.raw)


__all__ = [
    "AFFORDANCE_SCHEMA",
    "CLAIM_KINDS",
    "CLAIM_SCHEMA",
    "UNDERSTANDING_SCHEMA",
    "CONTEXT_PACKAGE_SCHEMA",
    "CONTEXT_REQUEST_SCHEMA",
    "CONTEXT_RESOLUTION_SCHEMA",
    "CONTRADICTION_SCHEMA",
    "ContextAffordance",
    "ContextPackage",
    "ContextRequest",
    "ContextResolution",
    "DecisionOption",
    "ENGINEERING_DECISION_SCHEMA",
    "EngineeringDecision",
    "EngineeringIntelligenceError",
    "HORIZONS",
    "IntentBasis",
    "M2C_POLICY_VERSION",
    "M2C_PRODUCER_ID",
    "M2C_PRODUCER_VERSION",
    "OMISSION_SCHEMA",
    "RepositoryUnderstandingView",
    "SEMANTIC_RECORD_CONTENT_TYPE",
    "UNKNOWN_SCHEMA",
    "UnderstandingClaim",
    "ClaimContradiction",
    "Unknown",
    "Omission",
    "publish_semantic_record",
    "reconstruct_semantic_record",
    "semantic_record_bytes",
    "semantic_record_content_ref",
    "transport_semantic_record",
    "validate_decision_applicability",
]

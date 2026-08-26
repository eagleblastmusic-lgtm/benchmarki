"""NX-025: minimal content-addressed continuation identity and packet.

The packet is a bounded transport artifact.  It carries enough immutable
identity to let a future reader ask Project Memory v2 whether re-entry is
still legal, but it never becomes workflow authority and it has no lease,
claim, sender, or re-entry side effect.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, NoReturn

from bdb_shared.evidence import canonical_json_bytes

from .auto_scope_contract import AutoScope, ScopeAction


CONTINUATION_PACKET_SCHEMA = "bdb-continuation-packet-v1"
CONTINUATION_PACKET_VERSION = "v1"
PACKET_SCHEMA = CONTINUATION_PACKET_SCHEMA
PACKET_VERSION = CONTINUATION_PACKET_VERSION
CONTINUATION_PACKET_VERSION_EXPLICIT = True
PACKET_CONTENT_ADDRESSED = True
PACKET_BECOMES_SECOND_AUTHORITY = False
PACKET_SIZE_LIMIT_EXPLICIT = True
DEFAULT_PACKET_MAX_BYTES = 16 * 1024
MAX_PACKET_BYTES = DEFAULT_PACKET_MAX_BYTES
MAX_EVIDENCE_REFS = 16
MAX_EVIDENCE_REF_BYTES = 512
MAX_BUDGET_SUMMARY_BYTES = 2 * 1024
MAX_METADATA_DEPTH = 3
MAX_METADATA_NODES = 96
EXPIRY_POLICY_REJECT_AT_OR_AFTER = "REJECT_AT_OR_AFTER_EXPIRY"

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SECRET_KEY = re.compile(
    r"(?i)(access[_-]?token|api[_-]?key|authorization|bearer|cookie|credential|password|passwd|private[_-]?key|secret|session(?:[_-]?token)?|token)"
)
_SECRET_VALUE = re.compile(
    r"(?is)(?:bearer\s+\S+|-----begin[^\n]*private key-----|(?:^|[\s;&])(?:access[_-]?token|api[_-]?key|authorization|cookie|password|passwd|session(?:[_-]?token)?)\s*[:=]\s*\S+)"
)
_TRANSCRIPT_FIELDS = frozenset(
    {
        "arbitrary_chat_history",
        "chat_history",
        "conversation_history",
        "conversation_transcript",
        "full_plan",
        "plan",
        "raw_ci_logs",
        "raw_test_logs",
        "test_logs",
        "transcript",
    }
)
_TERMINAL_TASK_STATUSES = frozenset({"ACCEPTED", "COMPLETED", "SKIPPED"})
_NON_CONTINUABLE_STATUSES = frozenset({"BLOCKED", "COMPLETED", "PAUSED", "STOPPED", "WAITING_FOR_PLAN"})
_DIGEST_FIELDS = frozenset({"continuation_id", "packet_digest"})

_REQUIRED_FIELDS = frozenset(
    {
        "schema",
        "packet_version",
        "project_id",
        "plan_identity",
        "plan_version",
        "plan_digest",
        "scope",
        "run_id",
        "scope_epoch",
        "current_milestone_id",
        "current_task_id",
        "execution_binding_id",
        "expected_repo_head_before",
        "state_revision",
        "state_digest",
        "allowed_next_action",
        "budget_summary",
        "evidence_refs",
        "issued_at",
        "expires_at",
        "expiry_policy",
        "continuation_generation",
        "continuation_id",
        "packet_digest",
    }
)
_OPTIONAL_FIELDS = frozenset({"attempt_id", "expected_tree", "conversation_binding_policy"})
CONTINUATION_PACKET_REQUIRED_FIELDS = _REQUIRED_FIELDS
CONTINUATION_PACKET_OPTIONAL_FIELDS = _OPTIONAL_FIELDS
CONTINUATION_PACKET_FIELDS = _REQUIRED_FIELDS | _OPTIONAL_FIELDS
CONTINUATION_IDENTITY_FIELDS = frozenset(
    {
        "project_id",
        "plan_identity",
        "plan_version",
        "plan_digest",
        "scope",
        "run_id",
        "scope_epoch",
        "current_milestone_id",
        "current_task_id",
        "execution_binding_id",
        "expected_repo_head_before",
        "state_revision",
        "state_digest",
        "allowed_next_action",
    }
)


class ContinuationPacketError(ValueError):
    """Fail-closed packet error with diagnostics that never contain values."""

    def __init__(self, code: str, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.field = field


def _fail(code: str, message: str, *, field: str | None = None) -> NoReturn:
    raise ContinuationPacketError(code, message, field=field)


def _field_name(field: object) -> str:
    value = str(field)
    return value[:96]


def _secret_like(value: str, *, key: str | None = None) -> bool:
    return bool((key and _SECRET_KEY.search(key)) or _SECRET_VALUE.search(value))


def redact_secrets(value: Any, *, key: str | None = None) -> Any:
    """Return a bounded diagnostic copy with obvious secret material redacted."""

    if key is not None and _SECRET_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(item_key): redact_secrets(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_secrets(item, key=key) for item in value]
    if isinstance(value, str) and _SECRET_VALUE.search(value):
        return "[REDACTED]"
    return value


def _bounded_text(
    value: Any,
    *,
    field: str,
    maximum: int = 256,
    allow_none: bool = False,
    reject_secrets: bool = True,
) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not value or len(value) > maximum:
        _fail("MALFORMED_FIELD", "packet field has an invalid bounded text value", field=field)
    if reject_secrets and _secret_like(value, key=field):
        _fail("SECRET_MATERIAL_REJECTED", "secret-like material is rejected before serialization", field=field)
    return value


def _bounded_int(value: Any, *, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        _fail("MALFORMED_FIELD", "packet field has an invalid integer value", field=field)
    return value


def _digest(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        _fail("MALFORMED_DIGEST", "packet digest field is not an exact sha256 reference", field=field)
    return value


def _scope(value: Any, *, field: str = "scope") -> str:
    try:
        return AutoScope(value).value
    except (TypeError, ValueError):
        _fail("MALFORMED_SCOPE", "packet scope is not a canonical AUTO scope", field=field)


def _action(value: Any, *, field: str = "allowed_next_action") -> str:
    try:
        return ScopeAction(value).value
    except (TypeError, ValueError):
        _fail("MALFORMED_ACTION", "packet allowed action is not a canonical scope action", field=field)


def _utc_datetime(value: Any, *, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ContinuationPacketError("MALFORMED_TIMESTAMP", "packet timestamp is not valid UTC ISO-8601", field=field) from exc
    else:
        _fail("MALFORMED_TIMESTAMP", "packet timestamp is not valid UTC ISO-8601", field=field)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail("MALFORMED_TIMESTAMP", "packet timestamp must carry an explicit timezone", field=field)
    return parsed.astimezone(timezone.utc)


def _canonical_timestamp(value: Any, *, field: str) -> str:
    return _utc_datetime(value, field=field).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _metadata_value(value: Any, *, field: str, depth: int = 0, nodes: list[int] | None = None) -> Any:
    if nodes is None:
        nodes = [0]
    nodes[0] += 1
    if nodes[0] > MAX_METADATA_NODES or depth > MAX_METADATA_DEPTH:
        _fail("METADATA_TOO_LARGE", "packet metadata exceeds its bounded shape", field=field)
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = _bounded_text(raw_key, field=f"{field}.key", maximum=64, reject_secrets=False)
            assert key is not None
            if _SECRET_KEY.search(key):
                _fail("SECRET_MATERIAL_REJECTED", "secret-like material is rejected before serialization", field=f"{field}.{key}")
            if key in normalized:
                _fail("MALFORMED_METADATA", "metadata contains duplicate canonical keys", field=field)
            normalized[key] = _metadata_value(raw_value, field=f"{field}.{key}", depth=depth + 1, nodes=nodes)
        return normalized
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_EVIDENCE_REFS:
            _fail("METADATA_TOO_LARGE", "packet metadata list exceeds its bounded length", field=field)
        return [
            _metadata_value(item, field=f"{field}[{index}]", depth=depth + 1, nodes=nodes)
            for index, item in enumerate(value)
        ]
    if isinstance(value, str):
        if len(value) > 256:
            _fail("METADATA_TOO_LARGE", "packet metadata string exceeds its bounded length", field=field)
        if _secret_like(value):
            _fail("SECRET_MATERIAL_REJECTED", "secret-like material is rejected before serialization", field=field)
        return value
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    _fail("MALFORMED_METADATA", "packet metadata contains an unsupported value", field=field)


def _normalize_evidence_ref(value: Any, *, field: str) -> str | dict[str, str]:
    if isinstance(value, str):
        return _digest(value, field=field)
    if not isinstance(value, Mapping):
        _fail("UNBOUNDED_RAW_EVIDENCE", "evidence reference must be a digest or bounded content reference", field=field)
    if len(value) > 6:
        _fail("UNBOUNDED_RAW_EVIDENCE", "evidence reference contains too many fields", field=field)
    allowed = {"evidence_id", "digest", "semantic_digest", "raw_digest", "schema", "type"}
    if any(not isinstance(key, str) or key not in allowed for key in value):
        _fail("UNBOUNDED_RAW_EVIDENCE", "evidence reference contains an unsupported raw field", field=field)
    normalized: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key)
        maximum = 128 if key in {"evidence_id", "schema", "type"} else 71
        text = _bounded_text(raw_value, field=f"{field}.{key}", maximum=maximum)
        assert text is not None
        if key in {"digest", "semantic_digest", "raw_digest"}:
            _digest(text, field=f"{field}.{key}")
        normalized[key] = text
    has_digest = isinstance(normalized.get("digest"), str)
    has_content_ref_pair = isinstance(normalized.get("semantic_digest"), str) and isinstance(normalized.get("raw_digest"), str)
    if not has_digest and not has_content_ref_pair:
        _fail("UNBOUNDED_RAW_EVIDENCE", "evidence reference has no integrity digest", field=field)
    return normalized


def _normalize_evidence_refs(value: Any, *, field: str = "evidence_refs") -> list[str | dict[str, str]]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        _fail("UNBOUNDED_RAW_EVIDENCE", "evidence references must be a bounded sequence", field=field)
    if len(value) > MAX_EVIDENCE_REFS:
        _fail("UNBOUNDED_RAW_EVIDENCE", "evidence references exceed the bounded count", field=field)
    normalized = [_normalize_evidence_ref(item, field=f"{field}[{index}]") for index, item in enumerate(value)]
    if len(canonical_json_bytes(normalized)) > MAX_BUDGET_SUMMARY_BYTES:
        _fail("UNBOUNDED_RAW_EVIDENCE", "evidence references exceed the bounded byte budget", field=field)
    return normalized


def _normalize_document(value: Mapping[str, Any], *, verify_digest: bool) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _fail("MALFORMED_PACKET", "continuation packet must be a JSON object")
    raw = dict(value)
    raw_keys = set(raw)
    missing = sorted(_REQUIRED_FIELDS - raw_keys)
    if missing:
        _fail("MISSING_REQUIRED_FIELD", "continuation packet is missing a required field", field=missing[0])
    unknown = sorted(raw_keys - CONTINUATION_PACKET_FIELDS, key=lambda item: str(item))
    if unknown:
        field = unknown[0]
        field_text = str(field)
        if field_text in _TRANSCRIPT_FIELDS or "transcript" in field_text.casefold() or "history" in field_text.casefold() or "log" in field_text.casefold():
            _fail("FULL_TRANSCRIPT_NOT_ALLOWED", "full conversation, plan, or raw log fields are not allowed", field=field)
        _fail("UNKNOWN_PACKET_FIELD", "continuation packet contains an unsupported field", field=field_text)

    normalized: dict[str, Any] = {
        "schema": _bounded_text(raw["schema"], field="schema", maximum=96),
        "packet_version": _bounded_text(raw["packet_version"], field="packet_version", maximum=16),
        "project_id": _bounded_text(raw["project_id"], field="project_id"),
        "plan_identity": _bounded_text(raw["plan_identity"], field="plan_identity"),
        "plan_version": _bounded_int(raw["plan_version"], field="plan_version", minimum=1),
        "plan_digest": _digest(raw["plan_digest"], field="plan_digest"),
        "scope": _scope(raw["scope"]),
        "run_id": _bounded_text(raw["run_id"], field="run_id"),
        "scope_epoch": _bounded_int(raw["scope_epoch"], field="scope_epoch", minimum=1),
        "current_milestone_id": _bounded_text(raw["current_milestone_id"], field="current_milestone_id"),
        "current_task_id": _bounded_text(raw["current_task_id"], field="current_task_id"),
        "execution_binding_id": _bounded_text(raw["execution_binding_id"], field="execution_binding_id"),
        "expected_repo_head_before": _bounded_text(raw["expected_repo_head_before"], field="expected_repo_head_before", maximum=128),
        "state_revision": _bounded_int(raw["state_revision"], field="state_revision", minimum=1),
        "state_digest": _digest(raw["state_digest"], field="state_digest"),
        "allowed_next_action": _action(raw["allowed_next_action"]),
        "budget_summary": _metadata_value(raw["budget_summary"], field="budget_summary"),
        "evidence_refs": _normalize_evidence_refs(raw["evidence_refs"]),
        "issued_at": _canonical_timestamp(raw["issued_at"], field="issued_at"),
        "expires_at": _canonical_timestamp(raw["expires_at"], field="expires_at"),
        "expiry_policy": _bounded_text(raw["expiry_policy"], field="expiry_policy", maximum=64),
        "continuation_generation": _bounded_int(raw["continuation_generation"], field="continuation_generation", minimum=1),
        "continuation_id": _digest(raw["continuation_id"], field="continuation_id"),
        "packet_digest": _digest(raw["packet_digest"], field="packet_digest"),
    }
    if "attempt_id" in raw:
        normalized["attempt_id"] = _bounded_text(raw["attempt_id"], field="attempt_id", maximum=128, allow_none=True)
    if "expected_tree" in raw:
        normalized["expected_tree"] = _bounded_text(raw["expected_tree"], field="expected_tree", maximum=128, allow_none=True)
    if "conversation_binding_policy" in raw:
        normalized["conversation_binding_policy"] = _bounded_text(
            raw["conversation_binding_policy"], field="conversation_binding_policy", maximum=96
        )

    if normalized["schema"] != CONTINUATION_PACKET_SCHEMA:
        _fail("UNKNOWN_PACKET_SCHEMA", "continuation packet schema is unsupported", field="schema")
    if normalized["packet_version"] != CONTINUATION_PACKET_VERSION:
        _fail("UNKNOWN_PACKET_VERSION", "continuation packet version is unsupported", field="packet_version")
    if normalized["expiry_policy"] != EXPIRY_POLICY_REJECT_AT_OR_AFTER:
        _fail("UNKNOWN_EXPIRY_POLICY", "continuation packet expiry policy is unsupported", field="expiry_policy")
    if _utc_datetime(normalized["expires_at"], field="expires_at") <= _utc_datetime(normalized["issued_at"], field="issued_at"):
        _fail("MALFORMED_TIMESTAMP", "continuation packet expiry must be after issue time", field="expires_at")
    if not isinstance(normalized["budget_summary"], Mapping):
        _fail("MALFORMED_METADATA", "budget summary must be a bounded mapping", field="budget_summary")
    if len(canonical_json_bytes(normalized["budget_summary"])) > MAX_BUDGET_SUMMARY_BYTES:
        _fail("METADATA_TOO_LARGE", "budget summary exceeds its bounded byte budget", field="budget_summary")

    expected_digest = _compute_digest_from_normalized(normalized)
    if verify_digest and (
        normalized["continuation_id"] != expected_digest or normalized["packet_digest"] != expected_digest
    ):
        _fail("PACKET_DIGEST_MISMATCH", "continuation packet digest does not match canonical semantic bytes")
    return normalized


def _compute_digest_from_normalized(document: Mapping[str, Any]) -> str:
    semantic = {key: document[key] for key in sorted(document) if key not in _DIGEST_FIELDS}
    return f"sha256:{hashlib.sha256(canonical_json_bytes(semantic)).hexdigest()}"


def compute_packet_digest(value: Mapping[str, Any] | "ContinuationPacket") -> str:
    """Compute the content address without trusting supplied digest fields."""

    document = value.document if isinstance(value, ContinuationPacket) else value
    normalized = _normalize_document(document, verify_digest=False)
    return _compute_digest_from_normalized(normalized)


def packet_digest(value: Mapping[str, Any] | "ContinuationPacket") -> str:
    return compute_packet_digest(value)


@dataclass(frozen=True)
class ContinuationPacket(Mapping[str, Any]):
    """Immutable mapping view over one validated continuation packet."""

    document: Mapping[str, Any]

    def __post_init__(self) -> None:
        normalized = _normalize_document(self.document, verify_digest=True)
        object.__setattr__(self, "document", normalized)

    def __getitem__(self, key: str) -> Any:
        return self.document[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.document)

    def __len__(self) -> int:
        return len(self.document)

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(dict(self.document))

    to_dict = as_dict

    @property
    def digest(self) -> str:
        return self.document["packet_digest"]

    @property
    def continuation_id(self) -> str:
        return self.document["continuation_id"]

    @property
    def expected_head(self) -> str:
        return self.document["expected_repo_head_before"]

    @property
    def canonical_state_revision(self) -> int:
        return self.document["state_revision"]

    @property
    def canonical_state_digest(self) -> str:
        return self.document["state_digest"]

    @property
    def continuation_packet_version(self) -> str:
        return self.document["packet_version"]

    @property
    def serialized(self) -> bytes:
        return canonical_json_bytes(self.document)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ContinuationPacket":
        return cls(value)

    from_dict = from_mapping


def build_packet(
    *,
    project_id: str,
    plan_identity: str,
    plan_version: int,
    plan_digest: str,
    scope: AutoScope | str,
    run_id: str,
    scope_epoch: int,
    current_milestone_id: str,
    current_task_id: str,
    execution_binding_id: str,
    expected_repo_head_before: str | None = None,
    state_revision: int | None = None,
    state_digest: str | None = None,
    allowed_next_action: ScopeAction | str | None = None,
    budget_summary: Mapping[str, Any] | None = None,
    evidence_refs: Sequence[Any] = (),
    issued_at: str | datetime | None = None,
    expires_at: str | datetime | None = None,
    expiry_policy: str = EXPIRY_POLICY_REJECT_AT_OR_AFTER,
    continuation_generation: int = 1,
    attempt_id: str | None = None,
    expected_tree: str | None = None,
    conversation_binding_policy: str = "EXISTING_CHAT_ONLY",
    max_bytes: int = DEFAULT_PACKET_MAX_BYTES,
    expected_head: str | None = None,
    canonical_state_revision: int | None = None,
    canonical_state_digest: str | None = None,
    allowed_action: ScopeAction | str | None = None,
) -> ContinuationPacket:
    """Create a deterministic v1 packet from one canonical state snapshot."""

    expected_repo_head_before = _coalesce_alias(
        expected_repo_head_before, expected_head, field="expected_repo_head_before"
    )
    state_revision = _coalesce_alias(state_revision, canonical_state_revision, field="state_revision")
    state_digest = _coalesce_alias(state_digest, canonical_state_digest, field="state_digest")
    allowed_next_action = _coalesce_alias(allowed_next_action, allowed_action, field="allowed_next_action")
    if issued_at is None:
        issued_at = datetime.now(timezone.utc)
    if expires_at is None:
        _fail("EXPIRY_REQUIRED", "continuation packet expiry must be explicit", field="expires_at")
    if state_revision is None or state_digest is None or expected_repo_head_before is None or allowed_next_action is None:
        _fail("MISSING_REQUIRED_FIELD", "continuation packet identity is incomplete")

    base: dict[str, Any] = {
        "schema": CONTINUATION_PACKET_SCHEMA,
        "packet_version": CONTINUATION_PACKET_VERSION,
        "project_id": project_id,
        "plan_identity": plan_identity,
        "plan_version": plan_version,
        "plan_digest": plan_digest,
        "scope": scope.value if isinstance(scope, AutoScope) else scope,
        "run_id": run_id,
        "scope_epoch": scope_epoch,
        "current_milestone_id": current_milestone_id,
        "current_task_id": current_task_id,
        "execution_binding_id": execution_binding_id,
        "expected_repo_head_before": expected_repo_head_before,
        "state_revision": state_revision,
        "state_digest": state_digest,
        "allowed_next_action": allowed_next_action.value if isinstance(allowed_next_action, ScopeAction) else allowed_next_action,
        "budget_summary": _copy_mapping(budget_summary, field="budget_summary"),
        "evidence_refs": _copy_sequence(evidence_refs, field="evidence_refs"),
        "issued_at": issued_at,
        "expires_at": expires_at,
        "expiry_policy": expiry_policy,
        "continuation_generation": continuation_generation,
        "continuation_id": "sha256:" + "0" * 64,
        "packet_digest": "sha256:" + "0" * 64,
    }
    if attempt_id is not None:
        base["attempt_id"] = attempt_id
    if expected_tree is not None:
        base["expected_tree"] = expected_tree
    if conversation_binding_policy is not None:
        base["conversation_binding_policy"] = conversation_binding_policy

    normalized = _normalize_document(base, verify_digest=False)
    digest = _compute_digest_from_normalized(normalized)
    normalized["continuation_id"] = digest
    normalized["packet_digest"] = digest
    packet = ContinuationPacket(normalized)
    _enforce_size(packet, max_bytes=max_bytes)
    return packet


def _coalesce_alias(primary: Any, alias: Any, *, field: str) -> Any:
    if primary is not None and alias is not None and primary != alias:
        _fail("CONFLICTING_ALIASES", "packet input aliases disagree", field=field)
    return primary if primary is not None else alias


def _copy_mapping(value: Mapping[str, Any] | None, *, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        _fail("MALFORMED_METADATA", "packet metadata must be a mapping", field=field)
    return dict(value)


def _copy_sequence(value: Sequence[Any] | None, *, field: str) -> list[Any]:
    if value is None or isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        _fail("UNBOUNDED_RAW_EVIDENCE", "evidence references must be a bounded sequence", field=field)
    return list(value)


create_continuation_packet = build_packet
make_continuation_packet = build_packet


def _max_bytes(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail("INVALID_SIZE_LIMIT", "packet byte-size limit must be a non-negative integer")
    return value


def _enforce_size(packet: ContinuationPacket, *, max_bytes: int) -> bytes:
    limit = _max_bytes(max_bytes)
    serialized = canonical_json_bytes(packet.document)
    if len(serialized) > limit:
        _fail("PACKET_TOO_LARGE", "serialized continuation packet exceeds its byte-size limit")
    return serialized


def serialize_packet(
    packet: ContinuationPacket | Mapping[str, Any],
    *,
    max_bytes: int = DEFAULT_PACKET_MAX_BYTES,
) -> bytes:
    """Serialize one packet using canonical UTF-8 JSON and enforce its limit."""

    typed = packet if isinstance(packet, ContinuationPacket) else ContinuationPacket.from_mapping(packet)
    return _enforce_size(typed, max_bytes=max_bytes)


def deserialize_packet(
    payload: bytes,
    *,
    max_bytes: int = DEFAULT_PACKET_MAX_BYTES,
) -> ContinuationPacket:
    """Read only canonical bytes; noncanonical or stale digest bytes fail closed."""

    limit = _max_bytes(max_bytes)
    if not isinstance(payload, bytes):
        _fail("MALFORMED_PACKET", "serialized continuation packet must be bytes")
    if len(payload) > limit:
        _fail("PACKET_TOO_LARGE", "serialized continuation packet exceeds its byte-size limit")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContinuationPacketError("MALFORMED_PACKET", "serialized continuation packet is not canonical UTF-8 JSON") from exc
    packet = ContinuationPacket.from_mapping(value)
    if canonical_json_bytes(packet.document) != payload:
        _fail("NON_CANONICAL_SERIALIZATION", "serialized continuation packet is not canonical JSON")
    return packet


def packet_size_bytes(packet: ContinuationPacket | Mapping[str, Any]) -> int:
    typed = packet if isinstance(packet, ContinuationPacket) else ContinuationPacket.from_mapping(packet)
    return len(canonical_json_bytes(typed.document))


@dataclass(frozen=True)
class ContinuationAuthoritySnapshot:
    """Read-only projection of the current canonical Project Memory facts."""

    project_id: str
    plan_identity: str
    plan_version: int
    plan_digest: str
    scope: AutoScope | str
    run_id: str
    scope_epoch: int
    current_milestone_id: str | None
    current_task_id: str | None
    execution_binding_id: str | None
    expected_repo_head_before: str
    state_revision: int
    state_digest: str
    allowed_next_action: ScopeAction | str
    budget_summary: Mapping[str, Any] = field(default_factory=dict)
    evidence_refs: Sequence[Any] = ()
    status: str = "ACTIVE"
    task_status: str = "IN_PROGRESS"
    stop_requested: bool = False
    plan_approved: bool = True
    attempt_id: str | None = None
    expected_tree: str | None = None
    conversation_binding_policy: str = "EXISTING_CHAT_ONLY"

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", _bounded_text(self.project_id, field="authority.project_id"))
        object.__setattr__(self, "plan_identity", _bounded_text(self.plan_identity, field="authority.plan_identity"))
        object.__setattr__(self, "plan_version", _bounded_int(self.plan_version, field="authority.plan_version", minimum=1))
        object.__setattr__(self, "plan_digest", _digest(self.plan_digest, field="authority.plan_digest"))
        object.__setattr__(self, "scope", _scope(self.scope, field="authority.scope"))
        object.__setattr__(self, "run_id", _bounded_text(self.run_id, field="authority.run_id"))
        object.__setattr__(self, "scope_epoch", _bounded_int(self.scope_epoch, field="authority.scope_epoch", minimum=1))
        object.__setattr__(self, "current_milestone_id", _bounded_text(self.current_milestone_id, field="authority.current_milestone_id", allow_none=True))
        object.__setattr__(self, "current_task_id", _bounded_text(self.current_task_id, field="authority.current_task_id", allow_none=True))
        object.__setattr__(self, "execution_binding_id", _bounded_text(self.execution_binding_id, field="authority.execution_binding_id", allow_none=True))
        object.__setattr__(self, "expected_repo_head_before", _bounded_text(self.expected_repo_head_before, field="authority.expected_repo_head_before", maximum=128))
        object.__setattr__(self, "state_revision", _bounded_int(self.state_revision, field="authority.state_revision", minimum=1))
        object.__setattr__(self, "state_digest", _digest(self.state_digest, field="authority.state_digest"))
        object.__setattr__(self, "allowed_next_action", _action(self.allowed_next_action, field="authority.allowed_next_action"))
        if not isinstance(self.budget_summary, Mapping):
            _fail("MALFORMED_AUTHORITY_SNAPSHOT", "authority budget summary must be a mapping")
        object.__setattr__(self, "budget_summary", _metadata_value(self.budget_summary, field="authority.budget_summary"))
        object.__setattr__(self, "evidence_refs", _normalize_evidence_refs(self.evidence_refs, field="authority.evidence_refs"))
        object.__setattr__(self, "status", _bounded_text(self.status, field="authority.status", maximum=64))
        object.__setattr__(self, "task_status", _bounded_text(self.task_status, field="authority.task_status", maximum=64))
        if not isinstance(self.stop_requested, bool) or not isinstance(self.plan_approved, bool):
            _fail("MALFORMED_AUTHORITY_SNAPSHOT", "authority boolean state is malformed")
        object.__setattr__(self, "attempt_id", _bounded_text(self.attempt_id, field="authority.attempt_id", maximum=128, allow_none=True))
        object.__setattr__(self, "expected_tree", _bounded_text(self.expected_tree, field="authority.expected_tree", maximum=128, allow_none=True))
        object.__setattr__(
            self,
            "conversation_binding_policy",
            _bounded_text(self.conversation_binding_policy, field="authority.conversation_binding_policy", maximum=96),
        )

    @property
    def expected_head(self) -> str:
        return self.expected_repo_head_before

    @property
    def canonical_state_revision(self) -> int:
        return self.state_revision

    @property
    def canonical_state_digest(self) -> str:
        return self.state_digest

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ContinuationAuthoritySnapshot":
        if not isinstance(value, Mapping):
            _fail("MALFORMED_AUTHORITY_SNAPSHOT", "live authority snapshot must be a mapping")
        expected_head = value.get("expected_repo_head_before", value.get("expected_head"))
        state_revision = value.get("state_revision", value.get("canonical_state_revision"))
        state_digest = value.get("state_digest", value.get("canonical_state_digest"))
        allowed_action = value.get("allowed_next_action", value.get("allowed_action"))
        required = {
            "project_id": value.get("project_id"),
            "plan_identity": value.get("plan_identity"),
            "plan_version": value.get("plan_version"),
            "plan_digest": value.get("plan_digest"),
            "scope": value.get("scope"),
            "run_id": value.get("run_id"),
            "scope_epoch": value.get("scope_epoch"),
            "current_milestone_id": value.get("current_milestone_id"),
            "current_task_id": value.get("current_task_id"),
            "execution_binding_id": value.get("execution_binding_id"),
            "expected_repo_head_before": expected_head,
            "state_revision": state_revision,
            "state_digest": state_digest,
            "allowed_next_action": allowed_action,
        }
        if any(item is None for item in required.values()):
            _fail("MALFORMED_AUTHORITY_SNAPSHOT", "live authority snapshot is incomplete")
        return cls(
            **required,
            budget_summary=value.get("budget_summary", {}),
            evidence_refs=value.get("evidence_refs", ()),
            status=value.get("status", "ACTIVE"),
            task_status=value.get("task_status", "IN_PROGRESS"),
            stop_requested=value.get("stop_requested", False),
            plan_approved=value.get("plan_approved", True),
            attempt_id=value.get("attempt_id"),
            expected_tree=value.get("expected_tree"),
            conversation_binding_policy=value.get("conversation_binding_policy", "EXISTING_CHAT_ONLY"),
        )


def authority_snapshot_from_cursor(
    cursor: Any,
    *,
    plan_digest: str,
    state_digest: str,
    expected_repo_head_before: str | None = None,
    expected_head: str | None = None,
    allowed_next_action: ScopeAction | str = ScopeAction.LAUNCH_TASK,
    execution_binding_id: str | None = None,
    budget_summary: Mapping[str, Any] | None = None,
    evidence_refs: Sequence[Any] = (),
    status: str = "ACTIVE",
    task_status: str = "IN_PROGRESS",
    stop_requested: bool = False,
    plan_approved: bool = True,
    attempt_id: str | None = None,
    expected_tree: str | None = None,
    conversation_binding_policy: str = "EXISTING_CHAT_ONLY",
) -> ContinuationAuthoritySnapshot:
    """Project an existing durable cursor into a validator-only snapshot."""

    if expected_repo_head_before is None:
        expected_repo_head_before = expected_head
    if expected_repo_head_before is None:
        _fail("MALFORMED_AUTHORITY_SNAPSHOT", "authority snapshot requires expected repository HEAD")
    return ContinuationAuthoritySnapshot(
        project_id=cursor.project_id,
        plan_identity=cursor.plan_identity,
        plan_version=cursor.plan_version,
        plan_digest=plan_digest,
        scope=cursor.scope,
        run_id=cursor.run_id,
        scope_epoch=cursor.scope_epoch,
        current_milestone_id=cursor.current_milestone_id,
        current_task_id=cursor.current_task_id,
        execution_binding_id=execution_binding_id,
        expected_repo_head_before=expected_repo_head_before,
        state_revision=cursor.state_revision,
        state_digest=state_digest,
        allowed_next_action=allowed_next_action,
        budget_summary=budget_summary or {},
        evidence_refs=evidence_refs,
        status=status,
        task_status=task_status,
        stop_requested=stop_requested,
        plan_approved=plan_approved,
        attempt_id=attempt_id,
        expected_tree=expected_tree,
        conversation_binding_policy=conversation_binding_policy,
    )


make_authority_snapshot = authority_snapshot_from_cursor
live_authority_snapshot = authority_snapshot_from_cursor


@dataclass(frozen=True)
class ContinuationValidationResult:
    valid: bool
    code: str
    message: str
    packet_digest: str | None = None

    @property
    def accepted(self) -> bool:
        return self.valid

    @property
    def reason_code(self) -> str:
        return self.code

    def __bool__(self) -> bool:
        return self.valid


def _invalid(code: str, message: str, *, packet_digest_value: str | None = None) -> ContinuationValidationResult:
    return ContinuationValidationResult(False, code, message, packet_digest_value)


def validate_packet(
    packet: ContinuationPacket | Mapping[str, Any] | bytes,
    authority: ContinuationAuthoritySnapshot | Mapping[str, Any],
    *,
    now: datetime | str | None = None,
    max_bytes: int = DEFAULT_PACKET_MAX_BYTES,
) -> ContinuationValidationResult:
    """Compare a packet with current authority without mutating any state."""

    try:
        if isinstance(packet, bytes):
            typed = deserialize_packet(packet, max_bytes=max_bytes)
        elif isinstance(packet, ContinuationPacket):
            typed = packet
            _enforce_size(typed, max_bytes=max_bytes)
        else:
            typed = ContinuationPacket.from_mapping(packet)
            _enforce_size(typed, max_bytes=max_bytes)
    except ContinuationPacketError as exc:
        return _invalid(exc.code, str(exc))

    try:
        live = authority if isinstance(authority, ContinuationAuthoritySnapshot) else ContinuationAuthoritySnapshot.from_mapping(authority)
    except ContinuationPacketError as exc:
        return _invalid(exc.code, str(exc), packet_digest_value=typed.digest)

    try:
        current_time = _utc_datetime(now if now is not None else datetime.now(timezone.utc), field="now")
    except ContinuationPacketError as exc:
        return _invalid(exc.code, str(exc), packet_digest_value=typed.digest)
    if current_time >= _utc_datetime(typed["expires_at"], field="expires_at"):
        return _invalid("EXPIRED_PACKET", "continuation packet is expired at the supplied authority clock", packet_digest_value=typed.digest)

    if not live.plan_approved:
        return _invalid("PLAN_NOT_APPROVED", "live authority has no approved plan", packet_digest_value=typed.digest)
    if live.stop_requested or str(live.status).upper() in {"STOPPED", "COMPLETED"}:
        return _invalid("STOP_CANONICAL", "canonical STOP or completion rejects continuation", packet_digest_value=typed.digest)
    if str(live.status).upper() in _NON_CONTINUABLE_STATUSES:
        return _invalid("AUTHORITY_NOT_CONTINUABLE", "live authority state is not legally continuable", packet_digest_value=typed.digest)
    if str(live.task_status).upper() in _TERMINAL_TASK_STATUSES:
        return _invalid("TASK_ALREADY_ACCEPTED", "current task is already terminal in live authority", packet_digest_value=typed.digest)

    comparisons = (
        ("project_id", "PROJECT_MISMATCH"),
        ("plan_identity", "STALE_PLAN_IDENTITY"),
        ("plan_version", "STALE_PLAN_VERSION"),
        ("plan_digest", "STALE_PLAN_DIGEST"),
        ("scope", "STALE_SCOPE"),
        ("run_id", "STALE_RUN"),
        ("scope_epoch", "STALE_SCOPE_EPOCH"),
        ("current_milestone_id", "STALE_MILESTONE"),
        ("current_task_id", "STALE_TASK"),
        ("execution_binding_id", "STALE_BINDING"),
        ("expected_repo_head_before", "STALE_HEAD"),
        ("state_revision", "STALE_STATE_REVISION"),
        ("state_digest", "STALE_STATE_DIGEST"),
        ("allowed_next_action", "STALE_ALLOWED_ACTION"),
    )
    for field_name, code in comparisons:
        if typed[field_name] != getattr(live, field_name):
            return _invalid(code, "packet identity differs from current canonical authority", packet_digest_value=typed.digest)

    if typed["budget_summary"] != live.budget_summary:
        return _invalid("STALE_BUDGET", "packet budget summary differs from current authority", packet_digest_value=typed.digest)
    if typed["evidence_refs"] != list(live.evidence_refs):
        return _invalid("STALE_EVIDENCE_REFS", "packet evidence references differ from current authority", packet_digest_value=typed.digest)
    if typed.get("conversation_binding_policy", "EXISTING_CHAT_ONLY") != live.conversation_binding_policy:
        return _invalid("STALE_BINDING_POLICY", "packet conversation binding policy differs from current authority", packet_digest_value=typed.digest)
    if typed.get("attempt_id") != live.attempt_id:
        return _invalid("STALE_ATTEMPT", "packet attempt identity differs from current authority", packet_digest_value=typed.digest)
    if typed.get("expected_tree") != live.expected_tree:
        return _invalid("STALE_TREE", "packet expected tree differs from current authority", packet_digest_value=typed.digest)

    return ContinuationValidationResult(True, "VALID", "packet matches current live authority", typed.digest)


validate_continuation_packet = validate_packet
validate = validate_packet


class ContinuationPacketValidator:
    """Stateless facade for the live-authority packet validator."""

    @staticmethod
    def validate(
        packet: ContinuationPacket | Mapping[str, Any] | bytes,
        authority: ContinuationAuthoritySnapshot | Mapping[str, Any],
        *,
        now: datetime | str | None = None,
        max_bytes: int = DEFAULT_PACKET_MAX_BYTES,
    ) -> ContinuationValidationResult:
        return validate_packet(packet, authority, now=now, max_bytes=max_bytes)

    validate_packet = validate


def validate_packet_or_raise(
    packet: ContinuationPacket | Mapping[str, Any] | bytes,
    authority: ContinuationAuthoritySnapshot | Mapping[str, Any],
    *,
    now: datetime | str | None = None,
    max_bytes: int = DEFAULT_PACKET_MAX_BYTES,
) -> ContinuationPacket:
    result = validate_packet(packet, authority, now=now, max_bytes=max_bytes)
    if not result.valid:
        _fail(result.code, result.message)
    if isinstance(packet, bytes):
        return deserialize_packet(packet, max_bytes=max_bytes)
    if isinstance(packet, ContinuationPacket):
        return packet
    return ContinuationPacket.from_mapping(packet)


__all__ = [
    "CONTINUATION_IDENTITY_FIELDS",
    "CONTINUATION_PACKET_FIELDS",
    "CONTINUATION_PACKET_OPTIONAL_FIELDS",
    "CONTINUATION_PACKET_REQUIRED_FIELDS",
    "CONTINUATION_PACKET_SCHEMA",
    "CONTINUATION_PACKET_VERSION",
    "CONTINUATION_PACKET_VERSION_EXPLICIT",
    "ContinuationAuthoritySnapshot",
    "ContinuationPacket",
    "ContinuationPacketError",
    "ContinuationPacketValidator",
    "ContinuationValidationResult",
    "DEFAULT_PACKET_MAX_BYTES",
    "EXPIRY_POLICY_REJECT_AT_OR_AFTER",
    "MAX_PACKET_BYTES",
    "PACKET_SCHEMA",
    "PACKET_VERSION",
    "PACKET_BECOMES_SECOND_AUTHORITY",
    "PACKET_CONTENT_ADDRESSED",
    "PACKET_SIZE_LIMIT_EXPLICIT",
    "authority_snapshot_from_cursor",
    "build_packet",
    "compute_packet_digest",
    "create_packet",
    "create_continuation_packet",
    "decode_packet",
    "deserialize_packet",
    "encode_packet",
    "live_authority_snapshot",
    "make_authority_snapshot",
    "make_continuation_packet",
    "packet_digest",
    "packet_size_bytes",
    "redact_secrets",
    "serialize_packet",
    "validate_continuation_packet",
    "validate",
    "validate_packet",
    "validate_packet_or_raise",
]


create_packet = build_packet
encode_packet = serialize_packet
decode_packet = deserialize_packet

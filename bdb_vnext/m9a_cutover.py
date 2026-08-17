"""M9a read-only legacy cutover classification.

This module does not stop legacy ingress, mutate legacy stores, create an archive,
or activate vNext.  It turns fresh R0a evidence plus explicit operator
classifications into a deterministic M9a preflight.  Effectful cutover remains
a separate local authority step and M9b is the only activation unit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from bdb_shared.evidence import semantic_digest


M9A_PREFLIGHT_SCHEMA = "bdb-vnext-m9a-preflight-v1"
M9A_DISPOSITION_SCHEMA = "bdb-vnext-m9a-collision-disposition-v1"
R0A_SCHEMA = "runtime-inventory-v1"

_TERMINAL_OR_FENCED = frozenset(
    {
        "TERMINAL",
        "DRAINED",
        "FENCED",
        "NO_LIVE_COLLISION_CAPABILITY",
    }
)
_BLOCKING_DISPOSITIONS = frozenset({"BLOCK_RESOURCE_CUTOVER"})
_ALLOWED_DISPOSITIONS = _TERMINAL_OR_FENCED | _BLOCKING_DISPOSITIONS

_UNRESOLVED_GROUPS = (
    "sessions",
    "commands",
    "outbox",
    "effects",
    "manual_reconciliation",
)


class M9aPreflightError(RuntimeError):
    """Typed failure for malformed or unsupported M9a evidence."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> None:
    raise M9aPreflightError(code, message)


def _mapping(value: object, *, code: str, message: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(code, message)
    return value


def _non_negative_int(value: object, *, code: str, message: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail(code, message)
    return value


def _source_map(report: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    sources = report.get("sources")
    if not isinstance(sources, list):
        _fail("inventory_shape_invalid", "R0a sources must be a list")
    result: dict[str, Mapping[str, Any]] = {}
    for item in sources:
        source = _mapping(
            item,
            code="inventory_shape_invalid",
            message="R0a source entry must be an object",
        )
        name = source.get("name")
        if not isinstance(name, str) or not name:
            _fail("inventory_shape_invalid", "R0a source entry has no bounded name")
        if name in result:
            _fail("inventory_shape_invalid", "R0a source names must be unique")
        result[name] = source
    return result


def _bounded_ids(group: Mapping[str, Any], *, group_name: str) -> tuple[str, ...]:
    ids = group.get("ids", [])
    if not isinstance(ids, list) or not all(isinstance(item, str) and item for item in ids):
        _fail("inventory_shape_invalid", f"R0a unresolved {group_name} ids are invalid")
    return tuple(ids)


@dataclass(frozen=True)
class CollisionDisposition:
    subject_kind: str
    subject_id: str
    disposition: str
    evidence_digest: str
    resource_key: str | None = None

    def __post_init__(self) -> None:
        if self.subject_kind not in _UNRESOLVED_GROUPS:
            raise ValueError("unsupported M9a subject_kind")
        if not self.subject_id or len(self.subject_id) > 512:
            raise ValueError("subject_id must be bounded")
        if self.disposition not in _ALLOWED_DISPOSITIONS:
            raise ValueError("unsupported M9a collision disposition")
        if not self.evidence_digest.startswith("sha256:") or len(self.evidence_digest) != 71:
            raise ValueError("evidence_digest must be sha256 identity")
        if self.resource_key is not None and (not self.resource_key or len(self.resource_key) > 512):
            raise ValueError("resource_key must be bounded when supplied")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": M9A_DISPOSITION_SCHEMA,
            "subject_kind": self.subject_kind,
            "subject_id": self.subject_id,
            "disposition": self.disposition,
            "evidence_digest": self.evidence_digest,
            "resource_key": self.resource_key,
        }


@dataclass(frozen=True)
class M9aPreflight:
    inventory_semantic_digest: str
    inventory_representation: str
    status: str
    reasons: tuple[str, ...]
    unresolved: Mapping[str, tuple[str, ...]]
    active_writer_count: int
    spool_entry_count: int
    receipt_reservation_count: int
    classifications: tuple[CollisionDisposition, ...]
    archive_subjects: tuple[str, ...]
    archive_missing: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": M9A_PREFLIGHT_SCHEMA,
            "inventory_semantic_digest": self.inventory_semantic_digest,
            "inventory_representation": self.inventory_representation,
            "status": self.status,
            "reasons": list(self.reasons),
            "unresolved": {
                name: list(items)
                for name, items in sorted(self.unresolved.items())
            },
            "active_writer_count": self.active_writer_count,
            "spool_entry_count": self.spool_entry_count,
            "receipt_reservation_count": self.receipt_reservation_count,
            "classifications": [item.as_dict() for item in self.classifications],
            "archive_subjects": list(self.archive_subjects),
            "archive_missing": list(self.archive_missing),
            "legacy_ingress_frozen": False,
            "legacy_writer_frozen": False,
            "archive_created": False,
            "vnext_activation_allowed": False,
            "m9b_allowed": False,
            "authorized_scope": [
                "READ_ONLY_OBSERVATION",
                "COLLISION_CLASSIFICATION",
                "BOUNDED_DRAIN_PLANNING",
                "ARCHIVE_CANDIDATE_PLANNING",
            ],
        }
        payload["preflight_digest"] = semantic_digest(payload)
        return payload


def _classification_map(
    dispositions: Sequence[CollisionDisposition],
) -> dict[tuple[str, str], CollisionDisposition]:
    result: dict[tuple[str, str], CollisionDisposition] = {}
    for item in dispositions:
        key = (item.subject_kind, item.subject_id)
        if key in result:
            _fail("duplicate_classification", "M9a subject was classified more than once")
        result[key] = item
    return result


def _archive_status(
    sources: Mapping[str, Mapping[str, Any]],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    required = (
        "bridge_config",
        "journal",
        "receipts",
        "spool",
        "promoter",
        "repository_browser_bundle",
    )
    present: list[str] = []
    missing: list[str] = []
    for name in required:
        source = sources.get(name)
        if (
            source is not None
            and source.get("status") == "OBSERVED"
            and source.get("complete") is True
        ):
            present.append(name)
        else:
            missing.append(name)
    for optional in ("native_config", "native_host_bundle", "deployed_browser_bundle"):
        source = sources.get(optional)
        if (
            source is not None
            and source.get("status") == "OBSERVED"
            and source.get("complete") is True
        ):
            present.append(optional)
    return tuple(present), tuple(missing)


def classify_legacy_cutover(
    report: Mapping[str, Any],
    *,
    dispositions: Sequence[CollisionDisposition] = (),
) -> M9aPreflight:
    """Classify fresh R0a evidence without mutating either generation.

    A READY result means only that the *read-only M9a preflight* sees no
    unresolved blocker.  It never means legacy ingress has been frozen and it
    never authorizes M9b.  The effectful local M9a step must still stop ingress,
    re-observe, drain/fence, and create/verify the archive candidate.
    """

    if report.get("schema") != R0A_SCHEMA:
        _fail("unsupported_inventory_schema", "M9a requires runtime-inventory-v1")

    digest = report.get("semantic_digest")
    if not isinstance(digest, str) or not digest.startswith("sha256:") or len(digest) != 71:
        _fail("inventory_identity_missing", "R0a semantic_digest is required")

    representation = report.get("representation")
    if representation not in {"PRIVATE_EXACT", "SANITIZED"}:
        _fail("inventory_representation_invalid", "R0a representation is unsupported")

    overall = _mapping(
        report.get("overall"),
        code="inventory_shape_invalid",
        message="R0a overall result is missing",
    )
    overall_result = overall.get("result")
    if overall_result not in {
        "READY_FOR_LOCAL_GATE",
        "INCOMPLETE",
        "INVALID",
        "UNSUPPORTED",
    }:
        _fail("inventory_result_invalid", "R0a overall result is unsupported")

    sources = _source_map(report)
    journal = sources.get("journal")
    unresolved: dict[str, tuple[str, ...]] = {name: () for name in _UNRESOLVED_GROUPS}
    active_writer_count = 0

    if journal is not None and journal.get("status") == "OBSERVED":
        facts = _mapping(
            journal.get("facts"),
            code="inventory_shape_invalid",
            message="R0a journal facts are missing",
        )
        writer_group = _mapping(
            facts.get("active_writer_candidates", {}),
            code="inventory_shape_invalid",
            message="R0a active writer group is invalid",
        )
        active_writer_count = _non_negative_int(
            writer_group.get("count", 0),
            code="inventory_shape_invalid",
            message="R0a active writer count is invalid",
        )
        unresolved_raw = _mapping(
            facts.get("unresolved", {}),
            code="inventory_shape_invalid",
            message="R0a unresolved journal facts are invalid",
        )
        for name in _UNRESOLVED_GROUPS:
            group = _mapping(
                unresolved_raw.get(name, {}),
                code="inventory_shape_invalid",
                message=f"R0a unresolved {name} group is invalid",
            )
            count = _non_negative_int(
                group.get("count", 0),
                code="inventory_shape_invalid",
                message=f"R0a unresolved {name} count is invalid",
            )
            ids = _bounded_ids(group, group_name=name)
            if group.get("truncated") is True or count > len(ids):
                unresolved[name] = tuple(ids) + ("<UNENUMERATED>",)
            else:
                unresolved[name] = ids

    spool = sources.get("spool")
    spool_entry_count = 0
    if spool is not None and spool.get("status") == "OBSERVED":
        facts = _mapping(
            spool.get("facts"),
            code="inventory_shape_invalid",
            message="R0a spool facts are missing",
        )
        spool_entry_count = _non_negative_int(
            facts.get("entry_count", 0),
            code="inventory_shape_invalid",
            message="R0a spool entry count is invalid",
        )

    receipts = sources.get("receipts")
    receipt_reservation_count = 0
    if receipts is not None and receipts.get("status") == "OBSERVED":
        facts = _mapping(
            receipts.get("facts"),
            code="inventory_shape_invalid",
            message="R0a receipt facts are missing",
        )
        receipt_reservation_count = _non_negative_int(
            facts.get("reservation_count", 0),
            code="inventory_shape_invalid",
            message="R0a receipt reservation count is invalid",
        )

    classification = _classification_map(dispositions)
    reasons: set[str] = set()

    if overall_result == "UNSUPPORTED":
        reasons.add("inventory_unsupported")
    elif overall_result == "INVALID":
        reasons.add("inventory_invalid")
    elif overall_result == "INCOMPLETE":
        reasons.add("inventory_incomplete")

    correlations = _mapping(
        report.get("correlations", {}),
        code="inventory_shape_invalid",
        message="R0a correlations are invalid",
    )
    blockers = correlations.get("blockers", [])
    if not isinstance(blockers, list):
        _fail("inventory_shape_invalid", "R0a correlation blockers are invalid")
    if blockers:
        reasons.add("cross_source_blocker")

    if active_writer_count:
        reasons.add("active_legacy_writer")
    if spool_entry_count:
        reasons.add("legacy_spool_not_empty")
    if receipt_reservation_count:
        reasons.add("native_reservations_present")

    undisposed = 0
    explicit_block = 0
    for group_name, ids in unresolved.items():
        for subject_id in ids:
            if subject_id == "<UNENUMERATED>":
                undisposed += 1
                reasons.add("unresolved_inventory_truncated")
                continue
            item = classification.get((group_name, subject_id))
            if item is None:
                undisposed += 1
                continue
            if item.disposition in _BLOCKING_DISPOSITIONS:
                explicit_block += 1
    if undisposed:
        reasons.add("collision_classification_required")
    if explicit_block:
        reasons.add("resource_cutover_blocked")

    archive_subjects, archive_missing = _archive_status(sources)
    if archive_missing:
        reasons.add("archive_candidate_inputs_incomplete")

    if "inventory_unsupported" in reasons:
        status = "BLOCKED_UNSUPPORTED"
    elif "inventory_invalid" in reasons:
        status = "BLOCKED_INVALID"
    elif (
        "active_legacy_writer" in reasons
        or "legacy_spool_not_empty" in reasons
        or "native_reservations_present" in reasons
    ):
        status = "DRAIN_REQUIRED"
    elif (
        "inventory_incomplete" in reasons
        or "cross_source_blocker" in reasons
        or "collision_classification_required" in reasons
        or "unresolved_inventory_truncated" in reasons
        or "resource_cutover_blocked" in reasons
        or "archive_candidate_inputs_incomplete" in reasons
    ):
        status = "RECONCILIATION_REQUIRED"
    else:
        status = "READY_FOR_LOCAL_M9A_FREEZE"

    return M9aPreflight(
        inventory_semantic_digest=digest,
        inventory_representation=representation,
        status=status,
        reasons=tuple(sorted(reasons)),
        unresolved={name: tuple(ids) for name, ids in unresolved.items()},
        active_writer_count=active_writer_count,
        spool_entry_count=spool_entry_count,
        receipt_reservation_count=receipt_reservation_count,
        classifications=tuple(dispositions),
        archive_subjects=archive_subjects,
        archive_missing=archive_missing,
    )


__all__ = [
    "CollisionDisposition",
    "M9A_DISPOSITION_SCHEMA",
    "M9A_PREFLIGHT_SCHEMA",
    "M9aPreflight",
    "M9aPreflightError",
    "classify_legacy_cutover",
]

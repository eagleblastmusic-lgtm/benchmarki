"""Aggregate read-only M9a preflights across explicit legacy profiles.

The local installation can legitimately contain more than one legacy profile
bound to the same repository. Migration safety is conjunctive: one profile
cannot make another profile safe. This module composes the existing
single-profile M9a classifier without introducing a writer, a new ledger, or
effectful cutover behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from bdb_shared.evidence import semantic_digest
from bdb_vnext.m9a_cutover import (
    CollisionDisposition,
    M9aPreflight,
    M9aPreflightError,
    classify_legacy_cutover,
)


M9A_MULTI_PROFILE_SCHEMA = "bdb-vnext-m9a-multi-profile-preflight-v1"

_STATUS_PRECEDENCE = {
    "READY_FOR_LOCAL_M9A_FREEZE": 0,
    "RECONCILIATION_REQUIRED": 1,
    "DRAIN_REQUIRED": 2,
    "BLOCKED_INVALID": 3,
    "BLOCKED_UNSUPPORTED": 4,
}


@dataclass(frozen=True)
class LegacyProfileEvidence:
    profile_id: str
    report: Mapping[str, Any]
    dispositions: tuple[CollisionDisposition, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.profile_id, str)
            or not self.profile_id
            or len(self.profile_id) > 128
            or "\x00" in self.profile_id
        ):
            raise ValueError("profile_id must be a bounded identifier")


@dataclass(frozen=True)
class InspectionObligation:
    profile_id: str
    kind: str
    subject: str
    reason_code: str

    def as_dict(self) -> dict[str, str]:
        return {
            "profile_id": self.profile_id,
            "kind": self.kind,
            "subject": self.subject,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True)
class MultiProfileM9aPreflight:
    profiles: tuple[tuple[str, M9aPreflight], ...]
    status: str
    obligations: tuple[InspectionObligation, ...]

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": M9A_MULTI_PROFILE_SCHEMA,
            "status": self.status,
            "profiles": [
                {
                    "profile_id": profile_id,
                    "preflight": preflight.as_dict(),
                }
                for profile_id, preflight in self.profiles
            ],
            "inspection_obligations": [
                obligation.as_dict() for obligation in self.obligations
            ],
            "all_profiles_ready_for_local_freeze": (
                bool(self.profiles)
                and all(
                    preflight.status == "READY_FOR_LOCAL_M9A_FREEZE"
                    for _, preflight in self.profiles
                )
            ),
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


def _source_map(report: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    sources = report.get("sources")
    if not isinstance(sources, list):
        return {}
    result: dict[str, Mapping[str, Any]] = {}
    for item in sources:
        if isinstance(item, Mapping):
            name = item.get("name")
            if isinstance(name, str) and name:
                result[name] = item
    return result


def _unclassified_count(preflight: M9aPreflight) -> int:
    classified = {
        (item.subject_kind, item.subject_id)
        for item in preflight.classifications
    }
    count = 0
    for group, ids in preflight.unresolved.items():
        for subject_id in ids:
            if subject_id == "<UNENUMERATED>" or (group, subject_id) not in classified:
                count += 1
    return count


def _obligations(
    profile: LegacyProfileEvidence,
    preflight: M9aPreflight,
) -> tuple[InspectionObligation, ...]:
    result: list[InspectionObligation] = []
    sources = _source_map(profile.report)

    if preflight.active_writer_count:
        result.append(
            InspectionObligation(
                profile.profile_id,
                "VERIFY_WRITER_CANDIDATE",
                "journal.active_writer_candidates",
                "active_legacy_writer",
            )
        )

    if preflight.spool_entry_count:
        result.append(
            InspectionObligation(
                profile.profile_id,
                "CLASSIFY_SPOOL_COLLISION_CAPABILITY",
                "spool",
                "legacy_spool_not_empty",
            )
        )

    if preflight.receipt_reservation_count:
        result.append(
            InspectionObligation(
                profile.profile_id,
                "CLASSIFY_NATIVE_RESERVATIONS",
                "receipts.submission_reservations",
                "native_reservations_present",
            )
        )

    for name in preflight.archive_missing:
        source = sources.get(name, {})
        status = source.get("status")
        errors = source.get("errors")
        error_codes = sorted(
            str(item.get("code"))
            for item in errors
            if isinstance(item, Mapping) and item.get("code")
        ) if isinstance(errors, list) else []
        suffix = ",".join(error_codes) if error_codes else str(status or "MISSING")
        result.append(
            InspectionObligation(
                profile.profile_id,
                "INSPECT_ARCHIVE_SOURCE",
                name,
                f"archive_source_{suffix}",
            )
        )

    if _unclassified_count(preflight):
        result.append(
            InspectionObligation(
                profile.profile_id,
                "CLASSIFY_UNRESOLVED_COLLISION_CAPABILITY",
                "journal.unresolved",
                "collision_classification_required",
            )
        )

    correlations = profile.report.get("correlations")
    if isinstance(correlations, Mapping):
        blockers = correlations.get("blockers")
        if isinstance(blockers, list):
            for blocker in blockers:
                if isinstance(blocker, Mapping):
                    code = blocker.get("code")
                    if isinstance(code, str) and code:
                        result.append(
                            InspectionObligation(
                                profile.profile_id,
                                "RESOLVE_CROSS_SOURCE_BLOCKER",
                                "correlations",
                                code,
                            )
                        )

    deduped: dict[tuple[str, str, str, str], InspectionObligation] = {}
    for item in result:
        key = (item.profile_id, item.kind, item.subject, item.reason_code)
        deduped[key] = item
    return tuple(deduped[key] for key in sorted(deduped))


def classify_legacy_profiles(
    profiles: Sequence[LegacyProfileEvidence],
) -> MultiProfileM9aPreflight:
    if not profiles:
        raise M9aPreflightError(
            "legacy_profiles_missing",
            "M9a requires at least one explicit legacy profile",
        )

    seen: set[str] = set()
    classified: list[tuple[str, M9aPreflight]] = []
    obligations: list[InspectionObligation] = []

    for profile in profiles:
        if profile.profile_id in seen:
            raise M9aPreflightError(
                "duplicate_legacy_profile",
                "legacy profile identifiers must be unique",
            )
        seen.add(profile.profile_id)
        preflight = classify_legacy_cutover(
            profile.report,
            dispositions=profile.dispositions,
        )
        classified.append((profile.profile_id, preflight))
        obligations.extend(_obligations(profile, preflight))

    status = max(
        (preflight.status for _, preflight in classified),
        key=lambda value: _STATUS_PRECEDENCE[value],
    )
    obligations_sorted = tuple(
        sorted(
            obligations,
            key=lambda item: (
                item.profile_id,
                item.kind,
                item.subject,
                item.reason_code,
            ),
        )
    )
    return MultiProfileM9aPreflight(
        profiles=tuple(classified),
        status=status,
        obligations=obligations_sorted,
    )


__all__ = [
    "InspectionObligation",
    "LegacyProfileEvidence",
    "M9A_MULTI_PROFILE_SCHEMA",
    "MultiProfileM9aPreflight",
    "classify_legacy_profiles",
]

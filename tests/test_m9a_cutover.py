from __future__ import annotations

from pathlib import Path

import pytest

from bdb_vnext.m9a_cutover import (
    CollisionDisposition,
    M9aPreflightError,
    classify_legacy_cutover,
)


DIGEST = "sha256:" + "a" * 64


def _source(name: str, *, facts: dict | None = None) -> dict:
    return {
        "name": name,
        "required": True,
        "status": "OBSERVED",
        "complete": True,
        "truncated": False,
        "identity": {},
        "facts": facts or {},
        "errors": [],
        "observation": {},
    }


def _group(ids: list[str] | None = None) -> dict:
    values = ids or []
    return {"count": len(values), "ids": values, "truncated": False}


def _report() -> dict:
    return {
        "schema": "runtime-inventory-v1",
        "representation": "PRIVATE_EXACT",
        "semantic_digest": DIGEST,
        "overall": {
            "result": "READY_FOR_LOCAL_GATE",
            "complete": True,
            "blockers": [],
            "safe_to_mutate": False,
        },
        "correlations": {"complete": True, "blockers": [], "findings": []},
        "sources": [
            _source("bridge_config"),
            _source(
                "journal",
                facts={
                    "active_writer_candidates": {
                        "count": 0,
                        "items": [],
                        "truncated": False,
                    },
                    "unresolved": {
                        "sessions": _group(),
                        "commands": _group(),
                        "outbox": _group(),
                        "effects": _group(),
                        "manual_reconciliation": _group(),
                    },
                },
            ),
            _source(
                "receipts",
                facts={
                    "request_count": 0,
                    "request_ids": [],
                    "reservation_count": 0,
                    "reservations": [],
                },
            ),
            _source("spool", facts={"entry_count": 0, "entries": []}),
            _source("promoter"),
            _source("repository_browser_bundle"),
            _source("native_config"),
        ],
    }


def test_clean_fresh_inventory_only_reaches_local_m9a_freeze_preflight() -> None:
    preflight = classify_legacy_cutover(_report())

    assert preflight.status == "READY_FOR_LOCAL_M9A_FREEZE"
    document = preflight.as_dict()
    assert document["legacy_ingress_frozen"] is False
    assert document["legacy_writer_frozen"] is False
    assert document["archive_created"] is False
    assert document["vnext_activation_allowed"] is False
    assert document["m9b_allowed"] is False
    assert "preflight_digest" in document


def test_active_writer_requires_drain() -> None:
    report = _report()
    journal = next(item for item in report["sources"] if item["name"] == "journal")
    journal["facts"]["active_writer_candidates"]["count"] = 1
    journal["facts"]["active_writer_candidates"]["items"] = [{"instance_id": "legacy-1"}]

    preflight = classify_legacy_cutover(report)

    assert preflight.status == "DRAIN_REQUIRED"
    assert "active_legacy_writer" in preflight.reasons


def test_spool_or_native_reservation_requires_drain() -> None:
    report = _report()
    spool = next(item for item in report["sources"] if item["name"] == "spool")
    receipts = next(item for item in report["sources"] if item["name"] == "receipts")
    spool["facts"]["entry_count"] = 2
    receipts["facts"]["reservation_count"] = 1

    preflight = classify_legacy_cutover(report)

    assert preflight.status == "DRAIN_REQUIRED"
    assert "legacy_spool_not_empty" in preflight.reasons
    assert "native_reservations_present" in preflight.reasons


def test_unresolved_effect_requires_explicit_collision_classification() -> None:
    report = _report()
    journal = next(item for item in report["sources"] if item["name"] == "journal")
    journal["facts"]["unresolved"]["effects"] = _group(["cmd-1"])

    preflight = classify_legacy_cutover(report)

    assert preflight.status == "RECONCILIATION_REQUIRED"
    assert "collision_classification_required" in preflight.reasons

    resolved = classify_legacy_cutover(
        report,
        dispositions=(
            CollisionDisposition(
                subject_kind="effects",
                subject_id="cmd-1",
                disposition="NO_LIVE_COLLISION_CAPABILITY",
                evidence_digest=DIGEST,
            ),
        ),
    )
    assert resolved.status == "READY_FOR_LOCAL_M9A_FREEZE"


def test_explicit_resource_block_can_never_be_discharged_by_preflight() -> None:
    report = _report()
    journal = next(item for item in report["sources"] if item["name"] == "journal")
    journal["facts"]["unresolved"]["commands"] = _group(["cmd-2"])

    preflight = classify_legacy_cutover(
        report,
        dispositions=(
            CollisionDisposition(
                subject_kind="commands",
                subject_id="cmd-2",
                disposition="BLOCK_RESOURCE_CUTOVER",
                evidence_digest=DIGEST,
                resource_key="repo:example",
            ),
        ),
    )

    assert preflight.status == "RECONCILIATION_REQUIRED"
    assert "resource_cutover_blocked" in preflight.reasons
    assert preflight.as_dict()["m9b_allowed"] is False


def test_truncated_unresolved_inventory_cannot_be_item_classified_safe() -> None:
    report = _report()
    journal = next(item for item in report["sources"] if item["name"] == "journal")
    journal["facts"]["unresolved"]["commands"] = {
        "count": 3,
        "ids": ["cmd-1"],
        "truncated": True,
    }

    preflight = classify_legacy_cutover(
        report,
        dispositions=(
            CollisionDisposition(
                subject_kind="commands",
                subject_id="cmd-1",
                disposition="TERMINAL",
                evidence_digest=DIGEST,
            ),
        ),
    )

    assert preflight.status == "RECONCILIATION_REQUIRED"
    assert "unresolved_inventory_truncated" in preflight.reasons


@pytest.mark.parametrize(
    ("overall_result", "expected"),
    [
        ("INCOMPLETE", "RECONCILIATION_REQUIRED"),
        ("INVALID", "BLOCKED_INVALID"),
        ("UNSUPPORTED", "BLOCKED_UNSUPPORTED"),
    ],
)
def test_r0a_non_ready_results_fail_closed(overall_result: str, expected: str) -> None:
    report = _report()
    report["overall"]["result"] = overall_result

    preflight = classify_legacy_cutover(report)

    assert preflight.status == expected
    assert preflight.as_dict()["m9b_allowed"] is False


def test_missing_archive_inputs_prevents_ready_freeze() -> None:
    report = _report()
    report["sources"] = [
        item for item in report["sources"] if item["name"] != "receipts"
    ]

    preflight = classify_legacy_cutover(report)

    assert preflight.status == "RECONCILIATION_REQUIRED"
    assert "receipts" in preflight.archive_missing
    assert "archive_candidate_inputs_incomplete" in preflight.reasons


def test_sanitized_inventory_is_diagnostic_only_but_never_grants_activation() -> None:
    report = _report()
    report["representation"] = "SANITIZED"

    preflight = classify_legacy_cutover(report)

    assert preflight.status == "READY_FOR_LOCAL_M9A_FREEZE"
    assert preflight.inventory_representation == "SANITIZED"
    assert preflight.as_dict()["vnext_activation_allowed"] is False


def test_wrong_inventory_schema_fails_closed() -> None:
    report = _report()
    report["schema"] = "other"

    with pytest.raises(M9aPreflightError) as captured:
        classify_legacy_cutover(report)

    assert captured.value.code == "unsupported_inventory_schema"


def test_module_has_no_legacy_runtime_or_mutation_dependency() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "bdb_vnext"
        / "m9a_cutover.py"
    ).read_text(encoding="utf-8")

    for forbidden in (
        "bdb_bridge",
        "sqlite3",
        "subprocess",
        "os.kill",
        "registry",
        "winreg",
        "INSERT INTO",
        "UPDATE ",
        "DELETE FROM",
        "CREATE TABLE",
    ):
        assert forbidden not in source

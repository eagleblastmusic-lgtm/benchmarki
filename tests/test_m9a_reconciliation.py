from __future__ import annotations

import pytest

from bdb_vnext.m9a_cutover import M9aPreflightError
from bdb_vnext.m9a_reconciliation import (
    LegacyProfileEvidence,
    classify_legacy_profiles,
)


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def _source(
    name: str,
    *,
    status: str = "OBSERVED",
    complete: bool = True,
    facts: dict | None = None,
    errors: list[dict] | None = None,
) -> dict:
    return {
        "name": name,
        "required": True,
        "status": status,
        "complete": complete,
        "truncated": False,
        "identity": {},
        "facts": facts or {},
        "errors": errors or [],
        "observation": {},
    }


def _group(ids: list[str] | None = None) -> dict:
    values = ids or []
    return {"count": len(values), "ids": values, "truncated": False}


def _report(digest: str) -> dict:
    return {
        "schema": "runtime-inventory-v1",
        "representation": "SANITIZED",
        "semantic_digest": digest,
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
                facts={"reservation_count": 0, "reservations": []},
            ),
            _source("spool", facts={"entry_count": 0, "entries": []}),
            _source("promoter"),
            _source("repository_browser_bundle"),
            _source("native_config"),
            _source("native_host_bundle"),
        ],
    }


def test_two_clean_profiles_are_conjunctively_ready_only_for_local_freeze() -> None:
    result = classify_legacy_profiles(
        (
            LegacyProfileEvidence("primary", _report(DIGEST_A)),
            LegacyProfileEvidence("self", _report(DIGEST_B)),
        )
    )

    document = result.as_dict()

    assert result.status == "READY_FOR_LOCAL_M9A_FREEZE"
    assert document["all_profiles_ready_for_local_freeze"] is True
    assert document["legacy_ingress_frozen"] is False
    assert document["m9b_allowed"] is False
    assert document["vnext_activation_allowed"] is False


def test_one_invalid_profile_blocks_the_whole_repository_cutover() -> None:
    clean = _report(DIGEST_A)
    invalid = _report(DIGEST_B)
    invalid["overall"]["result"] = "INVALID"
    receipts = next(
        item for item in invalid["sources"] if item["name"] == "receipts"
    )
    receipts.update(
        status="INVALID",
        complete=False,
        errors=[{"code": "invalid_shape", "message": "redacted"}],
    )

    result = classify_legacy_profiles(
        (
            LegacyProfileEvidence("primary", clean),
            LegacyProfileEvidence("self", invalid),
        )
    )

    assert result.status == "BLOCKED_INVALID"
    obligations = result.as_dict()["inspection_obligations"]
    assert {
        "profile_id": "self",
        "kind": "INSPECT_ARCHIVE_SOURCE",
        "subject": "receipts",
        "reason_code": "archive_source_invalid_shape",
    } in obligations


def test_obligations_expose_writer_spool_and_collision_work_without_authority() -> None:
    report = _report(DIGEST_A)
    journal = next(item for item in report["sources"] if item["name"] == "journal")
    journal["facts"]["active_writer_candidates"]["count"] = 1
    journal["facts"]["active_writer_candidates"]["items"] = [{"pid": 99}]
    journal["facts"]["unresolved"]["effects"] = _group(["effect-1"])
    spool = next(item for item in report["sources"] if item["name"] == "spool")
    spool["facts"]["entry_count"] = 3

    result = classify_legacy_profiles(
        (LegacyProfileEvidence("primary", report),)
    )

    obligations = {
        (item.kind, item.subject)
        for item in result.obligations
    }
    assert result.status == "DRAIN_REQUIRED"
    assert ("VERIFY_WRITER_CANDIDATE", "journal.active_writer_candidates") in obligations
    assert ("CLASSIFY_SPOOL_COLLISION_CAPABILITY", "spool") in obligations
    assert (
        "CLASSIFY_UNRESOLVED_COLLISION_CAPABILITY",
        "journal.unresolved",
    ) in obligations
    assert result.as_dict()["m9b_allowed"] is False


def test_duplicate_profile_identity_fails_closed() -> None:
    profile = LegacyProfileEvidence("same", _report(DIGEST_A))

    with pytest.raises(M9aPreflightError) as captured:
        classify_legacy_profiles((profile, profile))

    assert captured.value.code == "duplicate_legacy_profile"


def test_no_profile_never_means_ready() -> None:
    with pytest.raises(M9aPreflightError) as captured:
        classify_legacy_profiles(())

    assert captured.value.code == "legacy_profiles_missing"


def test_aggregator_has_no_mutation_or_legacy_runtime_dependency() -> None:
    import inspect
    import bdb_vnext.m9a_reconciliation as module

    source = inspect.getsource(module)
    for forbidden in (
        "bdb_bridge",
        "sqlite3",
        "subprocess",
        "winreg",
        "os.kill",
        "INSERT INTO",
        "UPDATE ",
        "DELETE FROM",
        "CREATE TABLE",
    ):
        assert forbidden not in source

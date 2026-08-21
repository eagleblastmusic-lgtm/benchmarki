from __future__ import annotations

import json
from pathlib import Path

import pytest

from bdb_shared.evidence import semantic_digest
from bdb_vnext.m12b_deletion import (
    M12B_APPROVAL_TOKEN,
    M12B_BLOCKED_STATUS,
    M12B_PLAN_STATUS,
    M12bDeletionError,
    apply_m12b_deletion,
    build_m12b_subject,
    verify_m12b_plan,
)


SOURCE = "a" * 40
TREE = "b" * 40
SHA = lambda n: "sha256:" + str(n) * 64


def _subject_inputs(*, unknown: bool = False) -> dict:
    deletion = {
        "schema": "bdb-vnext-m12a-deletion-plan-v1",
        "status": "PLANNED_NOT_APPLIED",
        "entries": [],
        "active_python_compatibility_usage_zero": True,
        "active_browser_compatibility_usage_zero": True,
        "final_deletion_performed": False,
        "production_mutation_performed": False,
    }
    inventory = [
        {"path": "bdb_bridge/legacy.py", "surface_class": "LEGACY_RUNTIME_PACKAGE", "disposition": "EXCLUDE", "sha256": SHA(1)},
        {"path": "scripts/old.ps1", "surface_class": "INSTALLER_WRAPPER_SCRIPT", "disposition": "ARCHIVE", "sha256": SHA(2)},
    ]
    if unknown:
        inventory.append({"path": "bdb_vnext/needs-review.py", "surface_class": "VNEXT_MIGRATION_OR_COMPATIBILITY_SOURCE", "disposition": "REVIEW", "sha256": SHA(3)})
    closure = {
        "schema": "bdb-vnext-m12a-full-closure-report-v1",
        "status": "PASS_CLOSED",
        "m12b_unlocked": True,
        "production_mutation_performed": False,
        "final_deletion_performed": False,
        "active_source_commit": SOURCE,
        "closure_report_sha256": SHA(4),
        "deletion_plan_sha256": semantic_digest(deletion),
        "compatibility_inventory": {"inventory_complete": True, "entries": inventory},
    }
    bootstrap = {
        "status": "ACTIVE",
        "state": {
            "state_sha256": SHA(5),
            "active_manifest_sha256": SHA(6),
            "previous_manifest_sha256": SHA(7),
            "runtime_id": "runtime",
            "generation_id": "g1",
            "legacy_runtime_root": r"C:\legacy",
        },
        "slots": {"ACTIVE": {"source_commit": SOURCE}, "PREVIOUS": {"source_commit": "c" * 40}},
    }
    return {
        "subject_id": "m12b-test-subject",
        "closure_report": closure,
        "deletion_plan": deletion,
        "source_commit": SOURCE,
        "source_tree": TREE,
        "bootstrap": bootstrap,
        "m9b": {"plan_sha256": SHA(8), "state_sha256": SHA(9), "target_m9b_record_sha256": SHA(1)},
        "m3c": {"control_digest": SHA(2), "kill_switch_digest": SHA(3)},
        "client": {"client_plan_sha256": SHA(4), "verification_sha256": SHA(5)},
        "route_rebind": {"plan_sha256": SHA(6), "state_sha256": SHA(7)},
        "physical_references": [
            {"path": r"C:\active\bundle", "category": "ACTIVE_PRODUCTION_REQUIRED", "authority": "bootstrap"},
            {"path": r"C:\previous\bundle", "category": "PREVIOUS_RECOVERY_REQUIRED", "authority": "bootstrap"},
            {"path": r"C:\evidence\m12a.json", "category": "IMMUTABLE_EVIDENCE", "authority": "m12a"},
        ],
    }


def test_build_subject_binds_closure_slots_and_classifies_inventory() -> None:
    result = build_m12b_subject(**_subject_inputs())
    assert result["status"] == M12B_PLAN_STATUS
    assert result["subject"]["source_tree"] == TREE
    assert result["subject"]["category_counts"]["ACTIVE_PRODUCTION_REQUIRED"] == 1
    assert result["subject"]["category_counts"]["LEGACY_COMPATIBLE_USAGE_ZERO"] == 1
    assert result["plan"]["production_deletion_performed"] is False


def test_unknown_inventory_is_blocked_and_must_not_delete() -> None:
    result = build_m12b_subject(**_subject_inputs(unknown=True))
    assert result["status"] == M12B_BLOCKED_STATUS
    assert "bdb_vnext/needs-review.py" in result["plan"]["unknown_paths"]
    with pytest.raises(M12bDeletionError) as caught:
        apply_m12b_deletion(plan_path=Path("missing.json"), approval_token=M12B_APPROVAL_TOKEN)
    assert caught.value.code == "m12b_path_unavailable"


def test_plan_is_immutable_and_apply_boundary_stays_dry_run(tmp_path: Path) -> None:
    result = build_m12b_subject(**_subject_inputs())
    plan = result["plan"]
    plan_path = tmp_path / "m12b-plan.json"
    plan_path.write_text(json.dumps({**plan, "plan_sha256": result["plan_sha256"]}, sort_keys=True), encoding="utf-8")
    verified = verify_m12b_plan(plan_path=plan_path, expected_plan_sha256=result["plan_sha256"])
    assert verified["plan_sha256"] == result["plan_sha256"]
    dry = apply_m12b_deletion(plan_path=plan_path, approval_token=M12B_APPROVAL_TOKEN, dry_run=True)
    assert dry["status"] == "DRY_RUN_ONLY"
    with pytest.raises(M12bDeletionError) as caught:
        apply_m12b_deletion(plan_path=plan_path, approval_token=M12B_APPROVAL_TOKEN, dry_run=False)
    assert caught.value.code == "m12b_destructive_effect_disabled"


def test_subject_rejects_wrong_source_identity() -> None:
    inputs = _subject_inputs()
    inputs["source_commit"] = "d" * 40
    with pytest.raises(M12bDeletionError) as caught:
        build_m12b_subject(**inputs)
    assert caught.value.code == "m12b_source_identity_mismatch"

from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from bdb_shared.evidence import canonical_json_bytes, semantic_digest
from bdb_vnext.m12b_deletion import (
    M12B_APPROVAL_TOKEN,
    M12B_BLOCKED_STATUS,
    M12B_PLAN_STATUS,
    M12bDeletionError,
    apply_m12b_deletion,
    build_m12b_subject,
    _resolve_route_rebind_identity,
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
        inventory.append({"path": "mystery.bin", "surface_class": "UNCLASSIFIED_RUNTIME_SURFACE", "disposition": "REVIEW", "sha256": SHA(3)})
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
        "client": {
            "client_plan_sha256": SHA(4),
            "verification_sha256": SHA(5),
            "source_head": SOURCE,
            "source_tree": TREE,
            "native_manifest_path": r"C:\manifest.json",
        },
        "route_rebind": {"plan_sha256": SHA(6), "state_sha256": SHA(7)},
        "native_routes": {
            "target": [
                {"root": "HKCU", "view": "32", "value": r"C:\manifest.json"},
                {"root": "HKCU", "view": "64", "value": r"C:\manifest.json"},
            ],
            "legacy": [],
            "target_conflict": False,
            "target_registered": True,
            "target_registered_views": ["32", "64"],
            "legacy_route_present": False,
        },
        "production_observation": {
            "schema": "bdb-vnext-m12b-production-acceptance-observation-v1",
            "source": "m9b_reconciliation_subject",
            "production_acceptance": True,
            "bootstrap_state_sha256": SHA(5),
            "active_source_commit": SOURCE,
            "active_source_tree": TREE,
            "m9b_state": "ACTIVE",
            "writer_enabled": True,
            "intake_enabled": True,
            "m3c_admission_enabled": True,
            "native_routes": {
                "target": [
                    {"root": "HKCU", "view": "32", "value": r"C:\manifest.json"},
                    {"root": "HKCU", "view": "64", "value": r"C:\manifest.json"},
                ],
                "legacy": [],
                "target_conflict": False,
                "target_registered": True,
                "target_registered_views": ["32", "64"],
                "legacy_route_present": False,
            },
        },
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


def test_migration_sources_resolve_to_target_package_exclusions() -> None:
    inputs = _subject_inputs()
    inputs["closure_report"]["compatibility_inventory"]["entries"].extend(
        [
            {
                "path": "bdb_vnext/m11c_cutover.py",
                "surface_class": "VNEXT_MIGRATION_OR_COMPATIBILITY_SOURCE",
                "disposition": "REMOVE_FROM_ACTIVE_TARGET_PACKAGE_IN_M12B",
                "sha256": SHA(10),
            },
            {
                "path": "bdb_vnext/__init__.py",
                "surface_class": "VNEXT_MIGRATION_OR_COMPATIBILITY_SOURCE",
                "disposition": "RETAIN_PACKAGE_ROOT",
                "sha256": SHA(11),
            },
            {
                "path": "pyproject.toml",
                "surface_class": "PACKAGE_COMPOSITION_SURFACE",
                "disposition": "TARGET_PACKAGE_MANIFEST_ONLY",
                "sha256": SHA(12),
            },
        ]
    )
    result = build_m12b_subject(**inputs)
    by_path = {item["path"]: item for item in result["subject"]["inventory"]}
    assert by_path["bdb_vnext/m11c_cutover.py"]["category"] == "SOURCE_ONLY"
    assert by_path["bdb_vnext/__init__.py"]["category"] == "ACTIVE_PRODUCTION_REQUIRED"
    assert by_path["pyproject.toml"]["category"] == "SOURCE_ONLY"
    closure = result["plan"]["package_closure"]
    assert "bdb_vnext/m11c_cutover.py" in closure["excluded_source_paths"]
    assert "bdb_vnext/__init__.py" in closure["retained_source_paths"]
    assert result["plan"]["unknown_paths"] == []


def test_unknown_inventory_is_blocked_and_must_not_delete() -> None:
    result = build_m12b_subject(**_subject_inputs(unknown=True))
    assert result["status"] == M12B_BLOCKED_STATUS
    assert "mystery.bin" in result["plan"]["unknown_paths"]
    with pytest.raises(M12bDeletionError) as caught:
        apply_m12b_deletion(plan_path=Path("missing.json"), approval_token=M12B_APPROVAL_TOKEN)
    assert caught.value.code == "m12b_path_unavailable"


def test_plan_is_immutable_and_apply_boundary_stays_dry_run(tmp_path: Path) -> None:
    inputs = _subject_inputs()
    inputs["execution_scope"] = "fixture"
    inputs["production_acceptance"] = True
    inputs.pop("production_observation")
    result = build_m12b_subject(**inputs)
    plan = result["plan"]
    plan_path = tmp_path / "m12b-plan.json"
    plan_path.write_text(json.dumps({**plan, "plan_sha256": result["plan_sha256"]}, sort_keys=True), encoding="utf-8")
    verified = verify_m12b_plan(plan_path=plan_path, expected_plan_sha256=result["plan_sha256"])
    assert verified["plan_sha256"] == result["plan_sha256"]
    dry = apply_m12b_deletion(plan_path=plan_path, approval_token=M12B_APPROVAL_TOKEN, dry_run=True)
    assert dry["status"] == "DRY_RUN_ONLY"
    with pytest.raises(M12bDeletionError) as caught:
        apply_m12b_deletion(plan_path=plan_path, approval_token=M12B_APPROVAL_TOKEN, dry_run=False)
    assert caught.value.code == "m12b_authority_readback_required"


def test_production_acceptance_cannot_be_caller_supplied() -> None:
    inputs = _subject_inputs()
    inputs.pop("production_observation")
    inputs["production_acceptance"] = True
    with pytest.raises(M12bDeletionError) as caught:
        build_m12b_subject(**inputs)
    assert caught.value.code == "m12b_production_observation_required"


def test_false_production_observation_blocks_preflight() -> None:
    inputs = _subject_inputs()
    inputs["production_observation"] = {
        "schema": "bdb-vnext-m12b-production-acceptance-observation-v1",
        "source": "m9b_reconciliation_subject",
        "production_acceptance": False,
    }
    result = build_m12b_subject(**inputs)
    assert result["status"] == M12B_BLOCKED_STATUS
    assert result["plan"]["production_acceptance"] is False
    assert "production acceptance observation not proven" in result["plan"]["unknown_paths"]


def test_m12b_requires_explicit_successor_route_identity() -> None:
    historical = {"route_rebind_id": "historical-route"}
    assert _resolve_route_rebind_identity(
        m9b_plan=historical,
        current_route_rebind_id="successor-route",
        current_route_rebind_plan_sha256=SHA(10),
    ) == ("successor-route", SHA(10), "CURRENT_SUCCESSOR")
    assert _resolve_route_rebind_identity(
        m9b_plan=historical,
        current_route_rebind_id=None,
        current_route_rebind_plan_sha256=None,
    ) == ("historical-route", None, "HISTORICAL")
    with pytest.raises(M12bDeletionError) as caught:
        _resolve_route_rebind_identity(
            m9b_plan=historical,
            current_route_rebind_id="successor-route",
            current_route_rebind_plan_sha256=None,
        )
    assert caught.value.code == "m12b_route_rebind_identity_invalid"


def test_subject_rejects_wrong_source_identity() -> None:
    inputs = _subject_inputs()
    inputs["source_commit"] = "d" * 40
    with pytest.raises(M12bDeletionError) as caught:
        build_m12b_subject(**inputs)
    assert caught.value.code == "m12b_source_identity_mismatch"


def _fixture_plan(tmp_path: Path, *, name: str = "delete-me.txt") -> tuple[dict, Path, Path]:
    target = tmp_path / name
    target.write_bytes(b"disposable fixture\r\n")
    inputs = _subject_inputs()
    inputs["physical_references"] = [
        {
            "path": str(target),
            "category": "DISPOSABLE",
            "authority": "fixture-disposable",
            "surface_class": "TEST_FIXTURE",
            "disposition": "ARCHIVE_OR_DELETE_SOURCE_ONLY_SURFACES_IN_M12B",
            "action_type": "DELETE_FILE",
            "delete_eligible": True,
        },
        *inputs["physical_references"],
    ]
    inputs["production_acceptance"] = True
    inputs["execution_scope"] = "fixture"
    inputs["journal_path"] = tmp_path / "m12b.journal.json"
    result = build_m12b_subject(**inputs)
    plan_path = tmp_path / "m12b-plan.json"
    plan_path.write_bytes(json.dumps({**result["plan"], "plan_sha256": result["plan_sha256"]}, sort_keys=True).encode())
    return result, plan_path, target


def test_fixture_apply_is_exact_replayable_and_journaled(tmp_path: Path) -> None:
    result, plan_path, target = _fixture_plan(tmp_path)
    authority = lambda: result["subject"]
    applied = apply_m12b_deletion(
        plan_path=plan_path,
        approval_token=M12B_APPROVAL_TOKEN,
        dry_run=False,
        authority_reader=authority,
    )
    assert applied["status"] == "COMPLETED"
    assert not target.exists()
    journal = json.loads(Path(result["plan"]["journal_path"]).read_text(encoding="utf-8"))
    assert journal["state"] == "COMPLETED"
    replay = apply_m12b_deletion(
        plan_path=plan_path,
        approval_token=M12B_APPROVAL_TOKEN,
        dry_run=False,
        authority_reader=authority,
    )
    assert replay["status"] == "COMPLETED"
    assert not target.exists()


def test_fixture_fault_after_delete_recovers_without_repeating_effect(tmp_path: Path) -> None:
    result, plan_path, target = _fixture_plan(tmp_path, name="fault-after.txt")
    authority = lambda: result["subject"]
    with pytest.raises(M12bDeletionError) as caught:
        apply_m12b_deletion(
            plan_path=plan_path,
            approval_token=M12B_APPROVAL_TOKEN,
            dry_run=False,
            authority_reader=authority,
            fault_at="after_target",
        )
    assert caught.value.code == "m12b_fault_injected"
    assert not target.exists()
    journal = json.loads(Path(result["plan"]["journal_path"]).read_text(encoding="utf-8"))
    assert journal["state"] == "APPLYING"
    recovered = apply_m12b_deletion(
        plan_path=plan_path,
        approval_token=M12B_APPROVAL_TOKEN,
        dry_run=False,
        authority_reader=authority,
    )
    assert recovered["status"] == "COMPLETED"
    assert not target.exists()


def test_protected_reference_cannot_become_disposable_target(tmp_path: Path) -> None:
    protected = tmp_path / "active"
    protected.mkdir()
    target = protected / "must-keep.txt"
    target.write_text("protected", encoding="utf-8")
    inputs = _subject_inputs()
    inputs["physical_references"] = [
        {
            "path": str(protected),
            "category": "ACTIVE_PRODUCTION_REQUIRED",
            "authority": "fixture-active",
        },
        {
            "path": str(target),
            "category": "DISPOSABLE",
            "authority": "fixture-disposable",
            "disposition": "ARCHIVE_OR_DELETE_SOURCE_ONLY_SURFACES_IN_M12B",
            "action_type": "DELETE_FILE",
            "delete_eligible": True,
        },
        *inputs["physical_references"],
    ]
    with pytest.raises(M12bDeletionError) as caught:
        build_m12b_subject(**inputs)
    assert caught.value.code == "m12b_target_protected"


def test_target_change_and_authority_change_fail_closed(tmp_path: Path) -> None:
    result, plan_path, target = _fixture_plan(tmp_path, name="changed.txt")
    target.write_bytes(b"foreign change")
    with pytest.raises(M12bDeletionError) as caught:
        apply_m12b_deletion(
            plan_path=plan_path,
            approval_token=M12B_APPROVAL_TOKEN,
            dry_run=False,
            authority_reader=lambda: result["subject"],
        )
    assert caught.value.code == "m12b_target_changed"
    assert target.exists()

    result2, plan_path2, target2 = _fixture_plan(tmp_path, name="authority.txt")
    changed = {**result2["subject"], "m9b": {**result2["subject"]["m9b"], "state_sha256": SHA(99)}}
    with pytest.raises(M12bDeletionError) as caught:
        apply_m12b_deletion(
            plan_path=plan_path2,
            approval_token=M12B_APPROVAL_TOKEN,
            dry_run=False,
            authority_reader=lambda: changed,
        )
    assert caught.value.code == "m12b_authority_changed"
    assert target2.exists()


def test_missing_unjournaled_target_is_not_treated_as_completed(tmp_path: Path) -> None:
    result, plan_path, target = _fixture_plan(tmp_path, name="missing-before-apply.txt")
    target.unlink()
    with pytest.raises(M12bDeletionError) as caught:
        apply_m12b_deletion(
            plan_path=plan_path,
            approval_token=M12B_APPROVAL_TOKEN,
            dry_run=False,
            authority_reader=lambda: result["subject"],
        )
    assert caught.value.code == "m12b_target_changed"
    journal = json.loads(Path(result["plan"]["journal_path"]).read_text(encoding="utf-8"))
    assert journal["state"] == "BLOCKED"


def test_target_identity_and_post_state_are_bound_to_plan(tmp_path: Path) -> None:
    result, plan_path, _target = _fixture_plan(tmp_path, name="identity.txt")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["candidate_targets"][0]["target_id"] = "sha256:" + "0" * 64
    payload = dict(plan)
    payload.pop("plan_sha256", None)
    plan["plan_sha256"] = "sha256:" + hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    plan_path.write_bytes(json.dumps(plan, sort_keys=True).encode())
    with pytest.raises(M12bDeletionError) as caught:
        apply_m12b_deletion(
            plan_path=plan_path,
            approval_token=M12B_APPROVAL_TOKEN,
            dry_run=True,
            authority_reader=lambda: result["subject"],
        )
    assert caught.value.code == "m12b_target_identity_mismatch"

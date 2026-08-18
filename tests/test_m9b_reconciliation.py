from __future__ import annotations

from pathlib import Path

import pytest

import bdb_vnext.m9b_reconciliation as reconciliation
from bdb_vnext.m9b_activation import ActivationRecord, M9bActivationError, write_activation


HEAD = "a" * 40
TREE = "b" * 40
OLD_HEAD = "c" * 40
OLD_TREE = "d" * 40
BOOT = "sha256:" + "1" * 64
ACTIVE = "sha256:" + "2" * 64
PREVIOUS = "sha256:" + "3" * 64
MAINTENANCE = "sha256:" + "4" * 64
ROUTE = "sha256:" + "5" * 64
CLIENT = "sha256:" + "6" * 64
BROWSER = "sha256:" + "7" * 64
NATIVE = "sha256:" + "8" * 64
BUNDLE = "sha256:" + "9" * 64
CANDIDATE = "sha256:" + "a" * 64


def _old() -> ActivationRecord:
    return ActivationRecord(
        activation_id="m9b-old-reconciliation",
        state="ACTIVE",
        source_head=OLD_HEAD,
        source_tree=OLD_TREE,
        m9a_freeze_digest=BOOT,
        browser_bundle_digest=BROWSER,
        native_manifest_digest=NATIVE,
        writer_enabled=True,
        intake_enabled=True,
    )


def _subject() -> dict[str, object]:
    return {
        "maintenance": {
            "plan_sha256": MAINTENANCE,
            "maintenance_id": "m11c-test-reconciliation",
            "route_transition_plan_sha256": ROUTE,
            "candidate_source_head": HEAD,
            "candidate_source_tree": TREE,
            "candidate_manifest_sha256": CANDIDATE,
            "candidate_bundle_sha256": BUNDLE,
            "client_plan_sha256": CLIENT,
            "candidate_native_manifest_path": "C:/candidate/native-host.json",
            "browser_bundle_digest": BROWSER,
            "native_manifest_digest": NATIVE,
        },
        "route_plan": {"route_transition_plan_sha256": ROUTE},
        "route_state": {"phase": "COMPLETED", "bootstrap_phase": "NEW", "bootstrap_state_sha256": BOOT},
        "bootstrap": {"state_sha256": BOOT, "active_manifest_sha256": ACTIVE, "previous_manifest_sha256": PREVIOUS},
        "active": {"source_commit": HEAD, "source_tree": TREE},
        "client_plan": {"client_plan_sha256": CLIENT, "browser_bundle_digest": BROWSER, "native_manifest_sha256": NATIVE},
        "client_verification": {"verification_sha256": CLIENT},
        "routes": {"target_registered": True, "target_conflict": False, "legacy_route_present": False},
        "m9b": _old(),
        "m3c": {"control_digest": CLIENT, "kill_switch_digest": CLIENT},
    }


def _prepare(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path, dict[str, object], dict[str, object]]:
    authority = tmp_path / "authority"
    deployed = tmp_path / "deployed"
    client = tmp_path / "client"
    for path in (authority, deployed, client):
        path.mkdir()
    write_activation(deployed, _old())
    subject = _subject()
    monkeypatch.setattr(reconciliation, "_subject", lambda **_: subject)
    prepared = reconciliation.prepare_post_active_reconciliation(
        authority_root=authority,
        deployed_runtime_root=deployed,
        candidate_client_runtime_root=client,
        maintenance_id="m11c-test-reconciliation",
        maintenance_plan_sha256=MAINTENANCE,
    )
    return authority, deployed, subject, prepared


def test_reconcile_updates_deployed_m9b_and_replays_idempotently(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    authority, deployed, _subject_value, prepared = _prepare(monkeypatch, tmp_path)
    first = reconciliation.reconcile_post_active_m9b(
        authority_root=authority,
        deployed_runtime_root=deployed,
        maintenance_id="m11c-test-reconciliation",
        expected_plan_sha256=prepared["plan_sha256"],
    )
    second = reconciliation.reconcile_post_active_m9b(
        authority_root=authority,
        deployed_runtime_root=deployed,
        maintenance_id="m11c-test-reconciliation",
        expected_plan_sha256=prepared["plan_sha256"],
    )
    assert first["status"] == "COMPLETED"
    assert second["status"] == "COMPLETED"
    assert second["target_matches"] is True
    assert second["state"]["phase"] == "COMPLETED"
    assert second["state"]["m9b_phase"] == "COMPLETED"


def test_reconcile_rejects_foreign_m9b_record(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    authority, deployed, _subject_value, prepared = _prepare(monkeypatch, tmp_path)
    foreign = ActivationRecord(
        activation_id="m9b-foreign",
        state="ACTIVE",
        source_head="e" * 40,
        source_tree="f" * 40,
        m9a_freeze_digest=BOOT,
        browser_bundle_digest=BROWSER,
        native_manifest_digest=NATIVE,
        writer_enabled=True,
        intake_enabled=True,
    )
    write_activation(deployed, foreign)
    with pytest.raises(reconciliation.M9bReconciliationError) as caught:
        reconciliation.reconcile_post_active_m9b(
            authority_root=authority,
            deployed_runtime_root=deployed,
            maintenance_id="m11c-test-reconciliation",
            expected_plan_sha256=prepared["plan_sha256"],
        )
    assert caught.value.code == "reconciliation_foreign_m9b"


def test_record_commit_before_journal_is_recovered_forward_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    authority, deployed, _subject_value, prepared = _prepare(monkeypatch, tmp_path)

    def fault(stage: str) -> None:
        if stage == "after_replace":
            raise RuntimeError("simulated crash after M9b replace")

    with pytest.raises(M9bActivationError):
        reconciliation.reconcile_post_active_m9b(
            authority_root=authority,
            deployed_runtime_root=deployed,
            maintenance_id="m11c-test-reconciliation",
            expected_plan_sha256=prepared["plan_sha256"],
            fault_hook=fault,
        )
    replay = reconciliation.reconcile_post_active_m9b(
        authority_root=authority,
        deployed_runtime_root=deployed,
        maintenance_id="m11c-test-reconciliation",
        expected_plan_sha256=prepared["plan_sha256"],
    )
    assert replay["status"] == "COMPLETED"
    assert replay["replayed_after_record_commit"] is True
    assert replay["target_matches"] is True
def test_reconcile_revalidates_subject_before_m9b_write(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    authority, deployed, subject, prepared = _prepare(monkeypatch, tmp_path)
    drifted = dict(subject)
    drifted_maintenance = dict(subject["maintenance"])  # type: ignore[index]
    drifted_maintenance["candidate_source_tree"] = "e" * 40
    drifted["maintenance"] = drifted_maintenance
    monkeypatch.setattr(reconciliation, "_subject", lambda **_: drifted)

    with pytest.raises(reconciliation.M9bReconciliationError) as caught:
        reconciliation.reconcile_post_active_m9b(
            authority_root=authority,
            deployed_runtime_root=deployed,
            maintenance_id="m11c-test-reconciliation",
            expected_plan_sha256=prepared["plan_sha256"],
        )
    assert caught.value.code == "reconciliation_subject_changed"

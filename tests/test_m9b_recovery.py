from __future__ import annotations

from pathlib import Path

import pytest

import bdb_vnext.m9b_recovery as recovery
from bdb_vnext.m9b_activation import ActivationRecord, read_activation


HEAD = "a" * 40
TREE = "b" * 40
OLD_HEAD = "c" * 40
OLD_TREE = "d" * 40
BOOT = "sha256:" + "1" * 64
ACTIVE = "sha256:" + "2" * 64
PREVIOUS = "sha256:" + "3" * 64
HIST = "sha256:" + "4" * 64
CLIENT = "sha256:" + "5" * 64
BROWSER = "sha256:" + "6" * 64
NATIVE = "sha256:" + "7" * 64
VERIFY = "sha256:" + "8" * 64
CONTROL = "sha256:" + "9" * 64
KILL = "sha256:" + "a" * 64
EXECUTABLE = "sha256:" + "b" * 64
CONFIG = "sha256:" + "c" * 64


def _historical_target() -> ActivationRecord:
    return ActivationRecord(
        activation_id="m9b-final-cutover-history",
        state="ACTIVE",
        source_head=OLD_HEAD,
        source_tree=OLD_TREE,
        m9a_freeze_digest=BOOT,
        browser_bundle_digest=BROWSER,
        native_manifest_digest=NATIVE,
        writer_enabled=True,
        intake_enabled=True,
    )


def _fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    authority = tmp_path / "authority"
    deployed = tmp_path / "deployed"
    authority.mkdir()
    deployed.mkdir()
    historical = _historical_target()
    historical_plan = {
        "target_m9b_record": historical.as_dict(),
        "target_m9b_record_sha256": historical.as_dict()["record_digest"],
        "m9a_freeze_digest": BOOT,
    }
    monkeypatch.setattr(
        recovery,
        "query_post_active_reconciliation",
        lambda **_: {"status": "COMPLETED", "state": {"phase": "COMPLETED"}, "plan": historical_plan},
    )
    monkeypatch.setattr(
        recovery,
        "observe_bootstrap_activation",
        lambda **_: {
            "status": "ACTIVE",
            "production_activation_performed": True,
            "state": {
                "state_sha256": BOOT,
                "active_manifest_sha256": ACTIVE,
                "previous_manifest_sha256": PREVIOUS,
            },
            "slots": {
                "ACTIVE": {"known_good": True, "source_commit": HEAD, "source_tree": TREE},
                "PREVIOUS": {"known_good": True},
            },
        },
    )
    client_plan = {
        "client_plan_sha256": CLIENT,
        "source_head": HEAD,
        "source_tree": TREE,
        "browser_bundle_digest": BROWSER,
        "native_manifest_sha256": NATIVE,
        "native_host_executable_sha256": EXECUTABLE,
        "native_config_sha256": CONFIG,
        "native_manifest_path": "C:/stable/native-host.json",
        "protocol_generation": "bdb-vnext-protocol-v1",
        "generation_id": "bdb-vnext-g1",
        "browser_extension_id": "mopnolkjddkmgojfjkenjobehhmmklll",
        "native_host_name": "com.bartosz.dev_bridge.vnext",
    }
    monkeypatch.setattr(recovery, "query_client_plan", lambda **_: {"plan": client_plan})
    monkeypatch.setattr(recovery, "require_client_verification", lambda **_: {"verification_sha256": VERIFY})
    monkeypatch.setattr(
        recovery,
        "observe_windows_native_routes",
        lambda **_: {"target_registered": True, "target_conflict": False, "legacy_route_present": False},
    )
    monkeypatch.setattr(recovery, "_m3c_state", lambda _runtime: {"control_digest": CONTROL, "kill_switch_digest": KILL})
    return authority, deployed


def _prepare(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path, dict[str, object]]:
    authority, deployed = _fixture(monkeypatch, tmp_path)
    prepared = recovery.prepare_missing_m9b_recovery(
        authority_root=authority,
        deployed_runtime_root=deployed,
        recovery_id="m9b-recover-current",
        historical_reconciliation_id="m11c-history",
        historical_reconciliation_plan_sha256=HIST,
    )
    return authority, deployed, prepared


def test_missing_m9b_prepare_and_recovery_binds_current_subject(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    authority, deployed, prepared = _prepare(monkeypatch, tmp_path)
    assert prepared["status"] == "PREPARED"
    result = recovery.recover_missing_m9b(
        authority_root=authority,
        recovery_id="m9b-recover-current",
        expected_plan_sha256=prepared["plan_sha256"],
        deployed_runtime_root=deployed,
        operator_approved=True,
    )
    assert result["status"] == "COMPLETED"
    assert result["target_matches"] is True
    record = read_activation(deployed)
    assert record is not None
    assert record.source_head == HEAD
    assert record.source_tree == TREE
    assert record.state == "ACTIVE"
    assert record.writer_enabled is True and record.intake_enabled is True


def test_recovery_reads_active_tree_from_bundle_provenance_when_slot_omits_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    authority, deployed = _fixture(monkeypatch, tmp_path)
    provenance_root = tmp_path / "active-bundle"
    provenance_root.mkdir()
    payload = {
        "schema": "bdb-vnext-runtime-bundle-provenance-v1",
        "runtime_id": "devmaster.bdb.vnext.runtime",
        "generation_id": "bdb-vnext-g1",
        "source_head": HEAD,
        "source_tree": TREE,
        "native_artifact_manifest_sha256": NATIVE,
        "native_executable_sha256": EXECUTABLE,
        "production_activation_performed": False,
    }
    from bdb_shared.evidence import canonical_json_bytes, semantic_digest

    provenance_root.joinpath("source-provenance.json").write_bytes(
        canonical_json_bytes({**payload, "provenance_sha256": semantic_digest(payload)})
    )
    monkeypatch.setattr(
        recovery,
        "observe_bootstrap_activation",
        lambda **_: {
            "status": "ACTIVE",
            "production_activation_performed": True,
            "state": {"state_sha256": BOOT, "active_manifest_sha256": ACTIVE, "previous_manifest_sha256": PREVIOUS},
            "slots": {
                "ACTIVE": {"known_good": True, "source_commit": HEAD, "bundle_root": str(provenance_root)},
                "PREVIOUS": {"known_good": True},
            },
        },
    )
    prepared = recovery.prepare_missing_m9b_recovery(
        authority_root=authority,
        deployed_runtime_root=deployed,
        recovery_id="m9b-recover-provenance",
        historical_reconciliation_id="m11c-history",
        historical_reconciliation_plan_sha256=HIST,
    )
    assert prepared["status"] == "PREPARED"


@pytest.mark.parametrize(
    "point",
    [
        "after_clients_verified_commit_before_journal",
        "after_activating_commit_before_journal",
        "after_active_commit_before_journal",
    ],
)
def test_recovery_replays_exactly_after_each_transition_fault(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, point: str) -> None:
    authority, deployed, prepared = _prepare(monkeypatch, tmp_path)

    def fault(point: str) -> None:
        if point == fault.target:
            raise RuntimeError(point)

    fault.target = point  # type: ignore[attr-defined]
    with pytest.raises(recovery.M9bRecoveryError) as caught:
        recovery.recover_missing_m9b(
            authority_root=authority,
            recovery_id="m9b-recover-current",
            expected_plan_sha256=prepared["plan_sha256"],
            deployed_runtime_root=deployed,
            operator_approved=True,
            fault_hook=fault,
        )
    assert caught.value.code == "recovery_fault_injected"
    recovery.recover_missing_m9b(
        authority_root=authority,
        recovery_id="m9b-recover-current",
        expected_plan_sha256=prepared["plan_sha256"],
        deployed_runtime_root=deployed,
        operator_approved=True,
    )
    final = recovery.query_missing_m9b_recovery(
        authority_root=authority,
        recovery_id="m9b-recover-current",
        expected_plan_sha256=prepared["plan_sha256"],
        deployed_runtime_root=deployed,
    )
    assert final["status"] == "COMPLETED"


def test_recovery_rejects_foreign_m9b_and_stale_subject(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    authority, deployed, prepared = _prepare(monkeypatch, tmp_path)
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
    from bdb_vnext.m9b_activation import write_activation

    write_activation(deployed, foreign)
    with pytest.raises(recovery.M9bRecoveryError) as caught:
        recovery.recover_missing_m9b(
            authority_root=authority,
            recovery_id="m9b-recover-current",
            expected_plan_sha256=prepared["plan_sha256"],
            deployed_runtime_root=deployed,
            operator_approved=True,
        )
    assert caught.value.code == "recovery_foreign_m9b"


def test_recovery_requires_exact_operator_plan(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    authority, deployed, prepared = _prepare(monkeypatch, tmp_path)
    with pytest.raises(recovery.M9bRecoveryError) as caught:
        recovery.recover_missing_m9b(
            authority_root=authority,
            recovery_id="m9b-recover-current",
            expected_plan_sha256="sha256:" + "f" * 64,
            deployed_runtime_root=deployed,
            operator_approved=True,
        )
    assert caught.value.code == "recovery_plan_stale"
    with pytest.raises(recovery.M9bRecoveryError) as not_approved:
        recovery.recover_missing_m9b(
            authority_root=authority,
            recovery_id="m9b-recover-current",
            expected_plan_sha256=prepared["plan_sha256"],
            deployed_runtime_root=deployed,
            operator_approved=False,
        )
    assert not_approved.value.code == "operator_approval_required"


def test_prepare_rejects_bootstrap_client_source_mismatch_without_m9b_write(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    authority, deployed = _fixture(monkeypatch, tmp_path)
    original = recovery.observe_bootstrap_activation
    observed = original()
    observed["slots"]["ACTIVE"]["source_commit"] = OLD_HEAD
    monkeypatch.setattr(recovery, "observe_bootstrap_activation", lambda **_: observed)
    with pytest.raises(recovery.M9bRecoveryError) as caught:
        recovery.prepare_missing_m9b_recovery(
            authority_root=authority,
            deployed_runtime_root=deployed,
            recovery_id="m9b-source-mismatch",
            historical_reconciliation_id="m11c-history",
            historical_reconciliation_plan_sha256=HIST,
        )
    assert caught.value.code == "bootstrap_client_source_mismatch"
    assert read_activation(deployed) is None


def test_prepare_rejects_stale_browser_verification_without_m9b_write(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    authority, deployed = _fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(recovery, "require_client_verification", lambda **_: (_ for _ in ()).throw(recovery.M9bRecoveryError("browser_verification_stale", "stale")))
    with pytest.raises(recovery.M9bRecoveryError) as caught:
        recovery.prepare_missing_m9b_recovery(
            authority_root=authority,
            deployed_runtime_root=deployed,
            recovery_id="m9b-stale-browser",
            historical_reconciliation_id="m11c-history",
            historical_reconciliation_plan_sha256=HIST,
        )
    assert caught.value.code == "browser_verification_stale"
    assert read_activation(deployed) is None


def test_prepare_rejects_noncanonical_m3c_and_legacy_route(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    authority, deployed = _fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(recovery, "_m3c_state", lambda _runtime: (_ for _ in ()).throw(recovery.M9bRecoveryError("m3c_not_canonical", "disabled")))
    with pytest.raises(recovery.M9bRecoveryError) as caught:
        recovery.prepare_missing_m9b_recovery(
            authority_root=authority,
            deployed_runtime_root=deployed,
            recovery_id="m9b-m3c-disabled",
            historical_reconciliation_id="m11c-history",
            historical_reconciliation_plan_sha256=HIST,
        )
    assert caught.value.code == "m3c_not_canonical"
    assert read_activation(deployed) is None

    legacy_root = tmp_path / "legacy"
    legacy_root.mkdir()
    authority, deployed = _fixture(monkeypatch, legacy_root)
    monkeypatch.setattr(recovery, "observe_windows_native_routes", lambda **_: {"target_registered": True, "target_conflict": False, "legacy_route_present": True})
    with pytest.raises(recovery.M9bRecoveryError) as caught:
        recovery.prepare_missing_m9b_recovery(
            authority_root=authority,
            deployed_runtime_root=deployed,
            recovery_id="m9b-legacy-present",
            historical_reconciliation_id="m11c-history",
            historical_reconciliation_plan_sha256=HIST,
        )
    assert caught.value.code == "native_route_not_exclusive"
    assert read_activation(deployed) is None


def test_prepare_rejects_missing_or_corrupt_historical_lineage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    authority, deployed = _fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(recovery, "query_post_active_reconciliation", lambda **_: {"status": "BLOCKED", "state": {"phase": "PREPARED"}})
    with pytest.raises(recovery.M9bRecoveryError) as caught:
        recovery.prepare_missing_m9b_recovery(
            authority_root=authority,
            deployed_runtime_root=deployed,
            recovery_id="m9b-no-history",
            historical_reconciliation_id="m11c-history",
            historical_reconciliation_plan_sha256=HIST,
        )
    assert caught.value.code == "historical_recovery_evidence_invalid"
    assert read_activation(deployed) is None


def test_recovery_revalidates_subject_before_semantic_write(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    authority, deployed, prepared = _prepare(monkeypatch, tmp_path)
    original = recovery._revalidate_subject
    calls = 0

    def fail_on_second(*, authority: Path, deployed: Path, plan: dict[str, object], allow_records: tuple[ActivationRecord, ...]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise recovery.M9bRecoveryError("recovery_subject_changed", "subject changed")
        return original(authority=authority, deployed=deployed, plan=plan, allow_records=allow_records)

    monkeypatch.setattr(recovery, "_revalidate_subject", fail_on_second)
    with pytest.raises(recovery.M9bRecoveryError) as caught:
        recovery.recover_missing_m9b(
            authority_root=authority,
            recovery_id="m9b-recover-current",
            expected_plan_sha256=prepared["plan_sha256"],
            deployed_runtime_root=deployed,
            operator_approved=True,
        )
    assert caught.value.code == "recovery_subject_changed"
    assert read_activation(deployed) is None

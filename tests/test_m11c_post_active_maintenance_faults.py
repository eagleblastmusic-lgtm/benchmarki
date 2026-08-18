from __future__ import annotations

from pathlib import Path

import pytest

import bdb_vnext.m11c_post_active_maintenance as maintenance
from test_m11c_post_active_maintenance import HEAD, SHA, TREE, _active, _patch_common, _prepare


PREPARE_FAULTS = {
    "before_candidate_publication",
    "after_candidate_publication",
    "before_plan_publication",
    "after_plan_publication",
}
APPLY_FAULTS = {
    "after_revalidation_before_switch",
    "after_manifest_inspection",
    "after_manifest_publication",
    "before_state_publication",
    "after_state_publication",
}


@pytest.mark.parametrize("case", sorted(PREPARE_FAULTS | APPLY_FAULTS))
def test_post_active_fault_matrix_is_never_silent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str) -> None:
    if case in PREPARE_FAULTS:
        _patch_common(monkeypatch, tmp_path)

        def prepare_fault(stage: str) -> None:
            if stage == case:
                raise maintenance.MaintenanceFault(stage)

        with pytest.raises(maintenance.MaintenanceFault):
            maintenance.prepare_post_active_maintenance(
                authority_root=tmp_path / "authority", candidate_bundle_root=tmp_path / "bundle", candidate_bundle_sha256=SHA,
                candidate_client_runtime_root=tmp_path / "client", source_head=HEAD, source_tree=TREE,
                native_artifact_manifest_sha256=SHA, maintenance_id=f"fault-{case}", fault_hook=prepare_fault,
            )
        assert not (tmp_path / "authority" / "slot-state.json").exists()
        return

    current = _active(tmp_path)
    _patch_common(monkeypatch, tmp_path)
    prepared = _prepare(monkeypatch, tmp_path, f"apply-fault-{case}")
    candidate_doc = {
        "schema": "bdb-vnext-bootstrap-slot-manifest-v1", "slot": "ACTIVE", "bundle_root": str(tmp_path / "bundle"),
        "bundle_sha256": SHA, "bundle_role": "candidate", "source_commit": HEAD, "known_good": True,
        "bundle_id": "candidate", "manifest_sha256": SHA,
        "compatibility": {"supported_control_schema": {"min": 1, "max": 1}, "capabilities": ["canonical-admission-v1"]},
    }
    monkeypatch.setattr(maintenance, "_inspect", lambda *_args, **_kwargs: candidate_doc)
    monkeypatch.setattr(maintenance, "_publish", lambda *_args, **_kwargs: SHA)
    monkeypatch.setattr(maintenance, "_replace_state", lambda *_args, **_kwargs: None)
    after = _active(tmp_path)
    after["active"] = {**current["active"], "source_commit": HEAD, "bundle_sha256": SHA}
    observations = iter((current, after))
    monkeypatch.setattr(maintenance, "_active_observation", lambda _authority: next(observations))

    def apply_fault(stage: str) -> None:
        if stage == case:
            raise maintenance.MaintenanceFault(stage)

    with pytest.raises(maintenance.MaintenanceFault):
        maintenance.apply_post_active_maintenance(
            authority_root=tmp_path / "authority", maintenance_id=f"apply-fault-{case}",
            expected_plan_sha256=prepared["plan"]["plan_sha256"], operator_approved=True, fault_hook=apply_fault,
        )
    assert current["active"]["source_commit"] == "3" * 40


def test_stale_revalidation_is_blocked_without_pointer_write(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    current = _active(tmp_path)
    _patch_common(monkeypatch, tmp_path)
    prepared = _prepare(monkeypatch, tmp_path, "stale-revalidation")
    changed = _active(tmp_path)
    changed["state"] = {**current["state"], "state_sha256": "sha256:" + "f" * 64}
    monkeypatch.setattr(maintenance, "_active_observation", lambda _authority: changed)
    with pytest.raises(maintenance.M11cMaintenanceError) as caught:
        maintenance.apply_post_active_maintenance(
            authority_root=tmp_path / "authority", maintenance_id="stale-revalidation",
            expected_plan_sha256=prepared["plan"]["plan_sha256"], operator_approved=True,
        )
    assert caught.value.code == "maintenance_plan_stale"

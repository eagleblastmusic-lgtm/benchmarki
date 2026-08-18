from __future__ import annotations

from pathlib import Path

import pytest

import bdb_vnext.m11c_post_active_maintenance as maintenance
from test_m11c_post_active_maintenance import SHA, TREE, HEAD, _patch_common, _prepare


def test_apply_rejects_runtime_client_bytes_different_from_approved_plan(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_common(monkeypatch, tmp_path)
    prepared = _prepare(monkeypatch, tmp_path, "client-mismatch")
    monkeypatch.setattr(
        maintenance,
        "_client_identity",
        lambda *_args, **_kwargs: {"client_plan_sha256": "sha256:" + "b" * 64, "browser_bundle_digest": SHA, "native_manifest_digest": SHA},
    )
    with pytest.raises(maintenance.M11cMaintenanceError) as caught:
        maintenance.apply_post_active_maintenance(
            authority_root=tmp_path / "authority", maintenance_id="client-mismatch",
            expected_plan_sha256=prepared["plan"]["plan_sha256"], operator_approved=True,
        )
    assert caught.value.code == "maintenance_client_binding_mismatch"

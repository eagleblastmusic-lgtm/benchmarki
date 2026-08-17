from __future__ import annotations

import json
from pathlib import Path

import pytest

from bdb_shared.evidence import semantic_digest
import bdb_vnext.m9b_activation as m9b
from bdb_vnext.m9b_activation import (
    M9bActivationError,
    activation_path,
    read_activation,
    record_clients_verified,
    require_active,
    validate_m9a_freeze_report,
)


HEAD = "1" * 40
TREE = "2" * 40
BROWSER_DIGEST = "sha256:" + "3" * 64
NATIVE_DIGEST = "sha256:" + "4" * 64
FREEZE_DIGEST = "sha256:" + "5" * 64


def _m9a_report(**overrides: object) -> dict[str, object]:
    report: dict[str, object] = {
        "schema": "bdb-vnext-m9a-freeze-report-v1",
        "status": "PASS_CLOSED",
        "legacy_ingress_frozen": True,
        "legacy_writer_frozen": True,
        "archive_created": True,
        "zero_new_write_observed": True,
        "vnext_activation_allowed": False,
        "m9b_allowed": False,
        "partial_freeze_requires_roll_forward": False,
        "freeze_digest": FREEZE_DIGEST,
    }
    report.update(overrides)
    return report


def _verified(root: Path, *, activation_id: str = "m9b-test-activation"):
    return record_clients_verified(
        root,
        m9a_report=_m9a_report(),
        source_head=HEAD,
        source_tree=TREE,
        browser_bundle_digest=BROWSER_DIGEST,
        native_manifest_digest=NATIVE_DIGEST,
        activation_id=activation_id,
    )


def test_m9b_no_longer_exports_product_activation_helpers() -> None:
    assert not hasattr(m9b, "activate")
    assert not hasattr(m9b, "finalize_interrupted_activation")
    assert "_begin_bootstrap_client_gate" not in m9b.__all__
    assert "_finalize_bootstrap_client_gate" not in m9b.__all__


def test_missing_activation_is_explicitly_not_active(tmp_path: Path) -> None:
    assert read_activation(tmp_path) is None
    with pytest.raises(M9bActivationError) as exc:
        require_active(tmp_path)
    assert exc.value.code == "vnext_not_active"
    assert not activation_path(tmp_path).exists()


def test_m9a_pass_closed_is_precondition_evidence_not_activation_authority() -> None:
    assert validate_m9a_freeze_report(_m9a_report()) == FREEZE_DIGEST
    for field, value in (
        ("legacy_ingress_frozen", False),
        ("legacy_writer_frozen", False),
        ("archive_created", False),
        ("zero_new_write_observed", False),
    ):
        with pytest.raises(M9bActivationError) as exc:
            validate_m9a_freeze_report(_m9a_report(**{field: value}))
        assert exc.value.code == "m9a_not_closed"
    with pytest.raises(M9bActivationError) as exc:
        validate_m9a_freeze_report(_m9a_report(m9b_allowed=True))
    assert exc.value.code == "m9a_evidence_invalid"


def test_clients_verified_keeps_writer_and_intake_off(tmp_path: Path) -> None:
    record = _verified(tmp_path)
    assert record.state == "CLIENTS_VERIFIED"
    assert record.writer_enabled is False
    assert record.intake_enabled is False
    assert read_activation(tmp_path) == record
    with pytest.raises(M9bActivationError) as exc:
        require_active(tmp_path)
    assert exc.value.code == "vnext_not_active"


def test_private_bootstrap_begin_keeps_route_fail_closed(tmp_path: Path) -> None:
    record = _verified(tmp_path)
    activating = m9b._begin_bootstrap_client_gate(
        tmp_path,
        expected_activation_id=record.activation_id,
    )
    assert activating.state == "ACTIVATING"
    assert activating.writer_enabled is False
    assert activating.intake_enabled is False
    with pytest.raises(M9bActivationError) as exc:
        require_active(tmp_path)
    assert exc.value.code == "vnext_not_active"


def test_private_bootstrap_finalize_requires_observed_m3c_intake(tmp_path: Path) -> None:
    record = _verified(tmp_path)
    m9b._begin_bootstrap_client_gate(tmp_path, expected_activation_id=record.activation_id)

    with pytest.raises(M9bActivationError) as exc:
        m9b._finalize_bootstrap_client_gate(
            tmp_path,
            expected_activation_id=record.activation_id,
            canonical_intake_is_enabled=lambda: False,
        )
    assert exc.value.code == "canonical_intake_not_enabled"

    active = m9b._finalize_bootstrap_client_gate(
        tmp_path,
        expected_activation_id=record.activation_id,
        canonical_intake_is_enabled=lambda: True,
    )
    assert active.state == "ACTIVE"
    assert active.writer_enabled is True
    assert active.intake_enabled is True
    assert require_active(tmp_path) == active


def test_activation_record_rejects_digest_tamper(tmp_path: Path) -> None:
    _verified(tmp_path)
    path = activation_path(tmp_path)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["browser_bundle_digest"] = "sha256:" + "a" * 64
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(M9bActivationError) as exc:
        read_activation(tmp_path)
    assert exc.value.code == "activation_digest_mismatch"


def test_activation_record_rejects_semantically_rehashed_wrong_client_identity(tmp_path: Path) -> None:
    _verified(tmp_path)
    path = activation_path(tmp_path)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["native_host_name"] = "com.bartosz.dev_bridge"
    unsigned = {key: value for key, value in document.items() if key != "record_digest"}
    document["record_digest"] = semantic_digest(unsigned)
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(M9bActivationError) as exc:
        read_activation(tmp_path)
    assert exc.value.code == "client_identity_mismatch"

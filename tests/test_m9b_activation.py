from __future__ import annotations

import json
from pathlib import Path

import pytest

from bdb_shared.evidence import semantic_digest
from bdb_vnext.m9b_activation import (
    M9bActivationError,
    activate,
    activation_path,
    finalize_interrupted_activation,
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
    observed = read_activation(tmp_path)
    assert observed == record
    with pytest.raises(M9bActivationError) as exc:
        require_active(tmp_path)
    assert exc.value.code == "vnext_not_active"


def test_activation_callback_runs_behind_activating_external_fence(tmp_path: Path) -> None:
    record = _verified(tmp_path)
    observed_during_callback: list[str] = []

    def enable() -> None:
        current = read_activation(tmp_path)
        assert current is not None
        observed_during_callback.append(current.state)
        with pytest.raises(M9bActivationError) as exc:
            require_active(tmp_path)
        assert exc.value.code == "vnext_not_active"

    active = activate(
        tmp_path,
        expected_activation_id=record.activation_id,
        enable_canonical_intake=enable,
    )
    assert observed_during_callback == ["ACTIVATING"]
    assert active.state == "ACTIVE"
    assert active.writer_enabled is True
    assert active.intake_enabled is True
    assert require_active(tmp_path) == active


def test_enable_failure_leaves_external_route_fail_closed_in_activating(tmp_path: Path) -> None:
    record = _verified(tmp_path)

    def fail() -> None:
        raise RuntimeError("simulated intake failure")

    with pytest.raises(M9bActivationError) as exc:
        activate(
            tmp_path,
            expected_activation_id=record.activation_id,
            enable_canonical_intake=fail,
        )
    assert exc.value.code == "canonical_intake_enable_failed"
    current = read_activation(tmp_path)
    assert current is not None
    assert current.state == "ACTIVATING"
    assert current.writer_enabled is False
    assert current.intake_enabled is False
    with pytest.raises(M9bActivationError):
        require_active(tmp_path)


def test_interrupted_activation_can_finalize_only_after_intake_is_observed_enabled(tmp_path: Path) -> None:
    record = _verified(tmp_path)

    with pytest.raises(M9bActivationError):
        activate(
            tmp_path,
            expected_activation_id=record.activation_id,
            enable_canonical_intake=lambda: (_ for _ in ()).throw(RuntimeError("crash")),
        )

    with pytest.raises(M9bActivationError) as exc:
        finalize_interrupted_activation(
            tmp_path,
            expected_activation_id=record.activation_id,
            canonical_intake_is_enabled=lambda: False,
        )
    assert exc.value.code == "canonical_intake_not_enabled"

    active = finalize_interrupted_activation(
        tmp_path,
        expected_activation_id=record.activation_id,
        canonical_intake_is_enabled=lambda: True,
    )
    assert active.state == "ACTIVE"
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

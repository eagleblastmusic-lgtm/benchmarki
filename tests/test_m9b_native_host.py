from __future__ import annotations

import io
import json
import struct
from pathlib import Path

import pytest

from bdb_vnext.composition import BROWSER_EXTENSION_ID, GENERATION_ID, NATIVE_HOST_NAME, PROTOCOL_GENERATION
import bdb_vnext.m9b_activation as m9b
from bdb_vnext.m9b_activation import record_clients_verified
from bdb_vnext.m9b_native_host import (
    M9B_NATIVE_CONFIG_SCHEMA,
    M9B_NATIVE_REQUEST_SCHEMA,
    M9bNativeError,
    VNextNativeConfig,
    handle_message,
    read_native_message,
    write_native_message,
)


HEAD = "1" * 40
TREE = "2" * 40
BROWSER_DIGEST = "sha256:" + "3" * 64
NATIVE_DIGEST = "sha256:" + "4" * 64
FREEZE_DIGEST = "sha256:" + "5" * 64


def _config(tmp_path: Path) -> VNextNativeConfig:
    return VNextNativeConfig(
        runtime_root=tmp_path / "vnext",
        legacy_runtime_root=tmp_path / "legacy",
        bootstrap_authority_root=tmp_path / "ProgramData" / "BartoszDevBridge-Next" / "bootstrap",
    )


def _message(action: str, **extra: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema": M9B_NATIVE_REQUEST_SCHEMA,
        "request_id": "request-1",
        "action": action,
        "protocol_generation": PROTOCOL_GENERATION,
        "browser_extension_id": BROWSER_EXTENSION_ID,
    }
    value.update(extra)
    return value


def _m9a_report() -> dict[str, object]:
    return {
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


def _submit_message() -> dict[str, object]:
    return _message(
        "admission.submit",
        request={
            "submission_key": "submission-1",
            "intent_revision": "revision-1",
            "intent": {"goal": "test"},
            "conversation_binding": {"conversation_id": "chat-1"},
            "consumer_binding": {"consumer_id": "browser-1"},
        },
    )


def test_config_is_exact_vnext_identity_and_binds_external_bootstrap(tmp_path: Path) -> None:
    path = tmp_path / "native-host.json"
    document = {
        "schema": M9B_NATIVE_CONFIG_SCHEMA,
        "generation_id": GENERATION_ID,
        "protocol_generation": PROTOCOL_GENERATION,
        "native_host_name": NATIVE_HOST_NAME,
        "browser_extension_id": BROWSER_EXTENSION_ID,
        "runtime_root": str(tmp_path / "vnext"),
        "legacy_runtime_root": str(tmp_path / "legacy"),
        "bootstrap_authority_root": str(tmp_path / "ProgramData" / "BartoszDevBridge-Next" / "bootstrap"),
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    loaded = VNextNativeConfig.from_json(path)
    assert loaded.native_host_name == "com.bartosz.dev_bridge.vnext"
    assert loaded.browser_extension_id == BROWSER_EXTENSION_ID
    assert loaded.bootstrap_authority_root == Path(document["bootstrap_authority_root"]).absolute()

    document["native_host_name"] = "com.bartosz.dev_bridge"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(M9bNativeError) as exc:
        VNextNativeConfig.from_json(path)
    assert exc.value.code == "client_identity_mismatch"


def test_config_rejects_bootstrap_inside_mutable_runtime(tmp_path: Path) -> None:
    with pytest.raises(M9bNativeError) as exc:
        VNextNativeConfig(
            runtime_root=tmp_path / "vnext",
            legacy_runtime_root=tmp_path / "legacy",
            bootstrap_authority_root=tmp_path / "vnext" / "bootstrap",
        )
    assert exc.value.code == "bootstrap_overlap"


def test_status_is_available_while_external_activation_is_off(tmp_path: Path) -> None:
    response = handle_message(
        _config(tmp_path),
        {
            "schema": M9B_NATIVE_REQUEST_SCHEMA,
            "request_id": "status-1",
            "action": "status",
        },
    )
    assert response["status"] == "success"
    assert response["native_host_name"] == NATIVE_HOST_NAME
    assert response["browser_extension_id"] == BROWSER_EXTENSION_ID
    assert response["activation"]["state"] == "OFF"
    assert response["activation"]["bootstrap_state"] == "OFF"
    assert response["activation"]["production_acceptance"] is False
    assert response["legacy_fallback"] is False


def test_handshake_rejects_wrong_protocol_without_legacy_fallback(tmp_path: Path) -> None:
    message = _message("handshake")
    message["protocol_generation"] = "legacy-generation"
    with pytest.raises(M9bNativeError) as exc:
        handle_message(_config(tmp_path), message)
    assert exc.value.code == "unsupported_protocol"


def test_submit_before_client_gate_active_does_not_open_canonical_authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened = False

    def forbidden_open(*args: object, **kwargs: object):
        nonlocal opened
        opened = True
        raise AssertionError("canonical authority must not open before ACTIVE")

    monkeypatch.setattr("bdb_vnext.m9b_native_host.CanonicalVNextAdmissionAuthority.open", forbidden_open)
    with pytest.raises(M9bNativeError) as exc:
        handle_message(_config(tmp_path), _submit_message())
    assert exc.value.code == "vnext_not_active"
    assert opened is False


def test_runtime_local_m9b_active_alone_cannot_open_production_route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    verified = record_clients_verified(
        config.runtime_root,
        m9a_report=_m9a_report(),
        source_head=HEAD,
        source_tree=TREE,
        browser_bundle_digest=BROWSER_DIGEST,
        native_manifest_digest=NATIVE_DIGEST,
        activation_id="m9b-native-negative",
    )
    m9b._begin_bootstrap_client_gate(
        config.runtime_root,
        expected_activation_id=verified.activation_id,
    )
    m9b._finalize_bootstrap_client_gate(
        config.runtime_root,
        expected_activation_id=verified.activation_id,
        canonical_intake_is_enabled=lambda: True,
    )
    opened = False

    def forbidden_open(*args: object, **kwargs: object):
        nonlocal opened
        opened = True
        raise AssertionError("canonical authority must not open without external Bootstrap ACTIVE")

    monkeypatch.setattr("bdb_vnext.m9b_native_host.CanonicalVNextAdmissionAuthority.open", forbidden_open)
    with pytest.raises(M9bNativeError) as exc:
        handle_message(config, _submit_message())
    assert exc.value.code == "bootstrap_not_active"
    assert opened is False

    status = handle_message(
        config,
        {"schema": M9B_NATIVE_REQUEST_SCHEMA, "request_id": "status-2", "action": "status"},
    )
    assert status["activation"]["state"] == "ACTIVE"
    assert status["activation"]["bootstrap_state"] == "OFF"
    assert status["activation"]["production_acceptance"] is False


def test_native_framing_is_bounded_little_endian_json() -> None:
    output = io.BytesIO()
    write_native_message(output, {"schema": "test", "ok": True})
    raw = output.getvalue()
    length = struct.unpack("<I", raw[:4])[0]
    assert length == len(raw[4:])
    assert json.loads(raw[4:].decode("utf-8")) == {"ok": True, "schema": "test"}

    source = io.BytesIO(raw)
    assert read_native_message(source) == {"ok": True, "schema": "test"}
    assert read_native_message(source) is None


def test_native_framing_rejects_oversized_claim() -> None:
    source = io.BytesIO(struct.pack("<I", 1024 * 1024 + 1))
    with pytest.raises(M9bNativeError) as exc:
        read_native_message(source)
    assert exc.value.code == "native_frame_invalid"

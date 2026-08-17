from __future__ import annotations

import io
import json
import struct
from pathlib import Path

import pytest

from bdb_vnext.composition import BROWSER_EXTENSION_ID, GENERATION_ID, NATIVE_HOST_NAME, PROTOCOL_GENERATION
from bdb_vnext.m9b_native_host import (
    M9B_NATIVE_CONFIG_SCHEMA,
    M9B_NATIVE_REQUEST_SCHEMA,
    M9bNativeError,
    VNextNativeConfig,
    handle_message,
    read_native_message,
    write_native_message,
)


def _config(tmp_path: Path) -> VNextNativeConfig:
    return VNextNativeConfig(
        runtime_root=tmp_path / "vnext",
        legacy_runtime_root=tmp_path / "legacy",
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


def test_config_is_exact_vnext_identity_and_rejects_legacy_host(tmp_path: Path) -> None:
    path = tmp_path / "native-host.json"
    document = {
        "schema": M9B_NATIVE_CONFIG_SCHEMA,
        "generation_id": GENERATION_ID,
        "protocol_generation": PROTOCOL_GENERATION,
        "native_host_name": NATIVE_HOST_NAME,
        "browser_extension_id": BROWSER_EXTENSION_ID,
        "runtime_root": str(tmp_path / "vnext"),
        "legacy_runtime_root": str(tmp_path / "legacy"),
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    loaded = VNextNativeConfig.from_json(path)
    assert loaded.native_host_name == "com.bartosz.dev_bridge.vnext"
    assert loaded.browser_extension_id == BROWSER_EXTENSION_ID

    document["native_host_name"] = "com.bartosz.dev_bridge"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(M9bNativeError) as exc:
        VNextNativeConfig.from_json(path)
    assert exc.value.code == "client_identity_mismatch"


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
    assert response["activation"]["production_acceptance"] is False
    assert response["legacy_fallback"] is False


def test_handshake_rejects_wrong_protocol_without_legacy_fallback(tmp_path: Path) -> None:
    message = _message("handshake")
    message["protocol_generation"] = "legacy-generation"
    with pytest.raises(M9bNativeError) as exc:
        handle_message(_config(tmp_path), message)
    assert exc.value.code == "unsupported_protocol"


def test_submit_before_active_does_not_open_canonical_authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened = False

    def forbidden_open(*args: object, **kwargs: object):
        nonlocal opened
        opened = True
        raise AssertionError("canonical authority must not open before ACTIVE")

    monkeypatch.setattr("bdb_vnext.m9b_native_host.CanonicalVNextAdmissionAuthority.open", forbidden_open)
    with pytest.raises(M9bNativeError) as exc:
        handle_message(
            _config(tmp_path),
            _message(
                "admission.submit",
                request={
                    "submission_key": "submission-1",
                    "intent_revision": "revision-1",
                    "intent": {"goal": "test"},
                    "conversation_binding": {"conversation_id": "chat-1"},
                    "consumer_binding": {"consumer_id": "browser-1"},
                },
            ),
        )
    assert exc.value.code == "vnext_not_active"
    assert opened is False


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

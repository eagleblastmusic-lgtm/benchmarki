"""Dedicated M9b Native Messaging transport for the active vNext generation.

This host is intentionally separate from ``bdb_bridge.native_host``.  It has no
legacy repository aliases, receipt store, spool, wake event, Session/Command
model or legacy fallback.  It transports bounded canonical admission messages
to the M3c authority and refuses production submission unless the independent
M9b external activation fence is ``ACTIVE``.
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Mapping, NoReturn

from bdb_vnext.composition import (
    BROWSER_EXTENSION_ID,
    GENERATION_ID,
    NATIVE_HOST_NAME,
    PROTOCOL_GENERATION,
    default_legacy_runtime_root,
    default_vnext_runtime_root,
)
from bdb_vnext.m3a_submission import M3aError, ShadowSubmissionRequest
from bdb_vnext.m3c_admission import CanonicalVNextAdmissionAuthority, M3cError
from bdb_vnext.m9b_activation import M9bActivationError, read_activation, require_active


M9B_NATIVE_CONFIG_SCHEMA = "bdb-vnext-native-host-config-v1"
M9B_NATIVE_REQUEST_SCHEMA = "bdb-vnext-native-request-v1"
M9B_NATIVE_RESPONSE_SCHEMA = "bdb-vnext-native-response-v1"
M9B_NATIVE_MAX_MESSAGE_BYTES = 1024 * 1024
M9B_NATIVE_ACTIONS = frozenset({"status", "handshake", "admission.submit", "admission.lookup"})


class M9bNativeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> NoReturn:
    raise M9bNativeError(code, message)


def _bounded_text(value: object, *, field: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        _fail("invalid_payload", f"{field} must be bounded non-empty text")
    return value


def default_vnext_native_config_path() -> Path:
    return default_vnext_runtime_root() / "config" / "native-host.json"


@dataclass(frozen=True)
class VNextNativeConfig:
    runtime_root: Path
    legacy_runtime_root: Path
    generation_id: str = GENERATION_ID
    protocol_generation: str = PROTOCOL_GENERATION
    native_host_name: str = NATIVE_HOST_NAME
    browser_extension_id: str = BROWSER_EXTENSION_ID
    schema: str = M9B_NATIVE_CONFIG_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != M9B_NATIVE_CONFIG_SCHEMA:
            _fail("config_schema_mismatch", "vNext Native config schema differs")
        if self.generation_id != GENERATION_ID or self.protocol_generation != PROTOCOL_GENERATION:
            _fail("generation_mismatch", "vNext Native config generation differs")
        if self.native_host_name != NATIVE_HOST_NAME or self.browser_extension_id != BROWSER_EXTENSION_ID:
            _fail("client_identity_mismatch", "vNext Native config client identity differs")
        runtime = self.runtime_root.expanduser().absolute()
        legacy = self.legacy_runtime_root.expanduser().absolute()
        try:
            if os.path.commonpath([os.fspath(runtime), os.fspath(legacy)]) in {
                os.fspath(runtime),
                os.fspath(legacy),
            }:
                _fail("runtime_overlap", "vNext and legacy runtime roots must be isolated")
        except ValueError:
            pass
        object.__setattr__(self, "runtime_root", runtime)
        object.__setattr__(self, "legacy_runtime_root", legacy)

    @classmethod
    def from_json(cls, path: str | Path) -> "VNextNativeConfig":
        source = Path(path).expanduser().absolute()
        if source.is_symlink() or not source.is_file():
            _fail("invalid_config", "vNext Native config must be a regular file")
        try:
            document = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise M9bNativeError("invalid_config", "vNext Native config is not valid JSON") from exc
        if not isinstance(document, Mapping):
            _fail("invalid_config", "vNext Native config must be an object")
        expected_keys = {
            "schema",
            "generation_id",
            "protocol_generation",
            "native_host_name",
            "browser_extension_id",
            "runtime_root",
            "legacy_runtime_root",
        }
        if set(document) != expected_keys:
            _fail("invalid_config", "vNext Native config fields differ")
        runtime = Path(_bounded_text(document["runtime_root"], field="runtime_root", maximum=4096))
        legacy = Path(_bounded_text(document["legacy_runtime_root"], field="legacy_runtime_root", maximum=4096))
        if not runtime.is_absolute() or not legacy.is_absolute():
            _fail("invalid_config", "vNext Native runtime roots must be absolute")
        return cls(
            runtime_root=runtime,
            legacy_runtime_root=legacy,
            generation_id=document["generation_id"],
            protocol_generation=document["protocol_generation"],
            native_host_name=document["native_host_name"],
            browser_extension_id=document["browser_extension_id"],
            schema=document["schema"],
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "generation_id": self.generation_id,
            "protocol_generation": self.protocol_generation,
            "native_host_name": self.native_host_name,
            "browser_extension_id": self.browser_extension_id,
            "runtime_root": str(self.runtime_root),
            "legacy_runtime_root": str(self.legacy_runtime_root),
        }


def _request(message: Mapping[str, Any]) -> ShadowSubmissionRequest:
    value = message.get("request")
    if not isinstance(value, Mapping):
        _fail("invalid_payload", "admission request must be an object")
    try:
        return ShadowSubmissionRequest(
            submission_key=value["submission_key"],
            intent_revision=value["intent_revision"],
            intent=value["intent"],
            conversation_binding=value["conversation_binding"],
            consumer_binding=value["consumer_binding"],
            canonicalization_version=value.get("canonicalization_version", "bdb-vnext-canonical-request-v1"),
            task_id=value.get("task_id"),
            expected_intent_revision_id=value.get("expected_intent_revision_id"),
            request_digest=value.get("request_digest"),
        )
    except KeyError as exc:
        raise M9bNativeError("invalid_payload", "admission request is missing a required field") from exc
    except M3aError as exc:
        raise M9bNativeError(exc.code, str(exc)) from exc


def _assert_protocol(message: Mapping[str, Any], config: VNextNativeConfig) -> None:
    if message.get("protocol_generation") != config.protocol_generation:
        _fail("unsupported_protocol", "Native request protocol generation differs")
    supplied_extension = message.get("browser_extension_id")
    if supplied_extension is not None and supplied_extension != config.browser_extension_id:
        _fail("client_identity_mismatch", "Browser extension identity differs")


def _activation_projection(config: VNextNativeConfig) -> dict[str, Any]:
    try:
        activation = read_activation(config.runtime_root)
    except M9bActivationError as exc:
        raise M9bNativeError(exc.code, str(exc)) from exc
    if activation is None:
        return {
            "state": "OFF",
            "activation_id": None,
            "production_acceptance": False,
            "writer_enabled": False,
            "intake_enabled": False,
        }
    return {
        "state": activation.state,
        "activation_id": activation.activation_id,
        "production_acceptance": activation.state == "ACTIVE",
        "writer_enabled": activation.writer_enabled,
        "intake_enabled": activation.intake_enabled,
    }


def _base_response(config: VNextNativeConfig, request_id: str, *, status: str = "success") -> dict[str, Any]:
    return {
        "schema": M9B_NATIVE_RESPONSE_SCHEMA,
        "status": status,
        "request_id": request_id,
        "generation_id": config.generation_id,
        "protocol_generation": config.protocol_generation,
        "native_host_name": config.native_host_name,
        "browser_extension_id": config.browser_extension_id,
    }


def handle_message(config: VNextNativeConfig, message: Mapping[str, Any]) -> dict[str, Any]:
    if message.get("schema") != M9B_NATIVE_REQUEST_SCHEMA:
        _fail("unsupported_schema", "Native request schema differs")
    request_id = _bounded_text(message.get("request_id"), field="request_id", maximum=128)
    action = _bounded_text(message.get("action"), field="action", maximum=64)
    if action not in M9B_NATIVE_ACTIONS:
        _fail("unsupported_action", "Native request action is unsupported")

    if action == "status":
        response = _base_response(config, request_id)
        response["activation"] = _activation_projection(config)
        response["legacy_fallback"] = False
        return response

    _assert_protocol(message, config)
    if action == "handshake":
        response = _base_response(config, request_id)
        response["activation"] = _activation_projection(config)
        response["capabilities"] = {
            "canonical_admission": True,
            "canonical_lookup": True,
            "legacy_fallback": False,
            "legacy_receipts": False,
            "legacy_spool": False,
        }
        return response

    try:
        require_active(config.runtime_root)
    except M9bActivationError as exc:
        raise M9bNativeError(exc.code, str(exc)) from exc

    authority = CanonicalVNextAdmissionAuthority.open(
        config.runtime_root,
        legacy_root=config.legacy_runtime_root,
    )
    try:
        if authority.admission_enabled is not True:
            _fail("canonical_intake_disabled", "M3c canonical intake kill switch is not enabled")
        if action == "admission.submit":
            receipt = authority.admit(_request(message))
        else:
            submission_key = _bounded_text(message.get("submission_key"), field="submission_key", maximum=192)
            request_digest = _bounded_text(message.get("request_digest"), field="request_digest", maximum=71)
            receipt = authority.lookup(submission_key, request_digest)
        response = _base_response(config, request_id)
        response["receipt"] = receipt.as_dict() if receipt is not None else None
        return response
    except M3cError as exc:
        raise M9bNativeError(exc.code, str(exc)) from exc
    finally:
        authority.close()


def read_native_message(stream: BinaryIO, *, max_bytes: int = M9B_NATIVE_MAX_MESSAGE_BYTES) -> dict[str, Any] | None:
    header = stream.read(4)
    if header == b"":
        return None
    if len(header) != 4:
        _fail("native_frame_invalid", "Native Messaging frame header is truncated")
    length = struct.unpack("<I", header)[0]
    if length < 2 or length > max_bytes:
        _fail("native_frame_invalid", "Native Messaging frame length is out of bounds")
    payload = stream.read(length)
    if len(payload) != length:
        _fail("native_frame_invalid", "Native Messaging frame body is truncated")
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise M9bNativeError("native_frame_invalid", "Native Messaging frame is not valid JSON") from exc
    if not isinstance(document, Mapping):
        _fail("native_frame_invalid", "Native Messaging request must be an object")
    return {str(key): value for key, value in document.items()}


def write_native_message(stream: BinaryIO, message: Mapping[str, Any]) -> None:
    payload = json.dumps(dict(message), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(payload) > M9B_NATIVE_MAX_MESSAGE_BYTES:
        _fail("native_response_too_large", "Native Messaging response exceeds the bounded frame size")
    stream.write(struct.pack("<I", len(payload)))
    stream.write(payload)
    stream.flush()


def _error_response(config: VNextNativeConfig, request_id: str, exc: BaseException) -> dict[str, Any]:
    code = getattr(exc, "code", "internal_error")
    response = _base_response(config, request_id, status="failed")
    response["error_code"] = str(code)
    response["error"] = str(exc) if isinstance(exc, M9bNativeError) else "vNext Native Host failed closed"
    response["legacy_fallback"] = False
    return response


def serve(config: VNextNativeConfig, stdin: BinaryIO, stdout: BinaryIO) -> int:
    while True:
        try:
            message = read_native_message(stdin)
            if message is None:
                return 0
            request_id = str(message.get("request_id") or "invalid-request")[:128]
            try:
                response = handle_message(config, message)
            except (M9bNativeError, M3aError, M3cError, M9bActivationError) as exc:
                response = _error_response(config, request_id, exc)
            write_native_message(stdout, response)
        except M9bNativeError:
            return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BDB vNext M9b dedicated Native Messaging host")
    parser.add_argument("--config", default=str(default_vnext_native_config_path()))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = VNextNativeConfig.from_json(args.config)
    except M9bNativeError:
        return 2
    return serve(config, sys.stdin.buffer, sys.stdout.buffer)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "M9B_NATIVE_ACTIONS",
    "M9B_NATIVE_CONFIG_SCHEMA",
    "M9B_NATIVE_MAX_MESSAGE_BYTES",
    "M9B_NATIVE_REQUEST_SCHEMA",
    "M9B_NATIVE_RESPONSE_SCHEMA",
    "M9bNativeError",
    "VNextNativeConfig",
    "default_vnext_native_config_path",
    "handle_message",
    "main",
    "read_native_message",
    "serve",
    "write_native_message",
]

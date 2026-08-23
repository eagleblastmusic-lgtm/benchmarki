"""Dedicated BDB Next Native Messaging transport.

The Native Host owns transport only. Production admission requires three
independent gates to agree:

1. the M11c external ProgramData Bootstrap is ACTIVE (activation authority),
2. the M9b Browser/Native client gate is ACTIVE (subordinate route gate), and
3. the canonical M3c intake switch is enabled (internal writer gate).

Chrome supplies the caller extension origin on the Native Messaging process
command line. A successful handshake from that pinned origin may publish a
bounded M11c client-verification observation only when the Browser also proves
its exact running packaged bytes match the staged client plan.
"""

from __future__ import annotations

import argparse
import json
import os
import re
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
    default_vnext_runtime_root,
)
from bdb_vnext.m11c_active_reader import M11cActiveReadError, observe_bootstrap_activation, require_bootstrap_active
from bdb_vnext.m11c_windows_clients import M11cClientError, record_browser_launch_verification
from bdb_vnext.m3a_submission import M3aError, ShadowSubmissionRequest
from bdb_vnext.m3c_admission import CanonicalVNextAdmissionAuthority, M3cError
from bdb_vnext.m9b_activation import M9bActivationError, read_activation, require_active
from bdb_vnext.project_catalog import ProjectCatalog
from bdb_vnext.project_execution import ProjectExecutionCoordinator, ProjectExecutionError, ProjectExecutionSubmission
from bdb_vnext.project_workflow import ProjectWorkflow, ProjectWorkflowError
from bdb_vnext.project_launch import (
    ProjectLaunchQueueAdapter,
    ProjectLaunchQueueError,
    default_project_launch_queue_path,
)


M9B_NATIVE_CONFIG_SCHEMA = "bdb-vnext-native-host-config-v2"
M9B_NATIVE_REQUEST_SCHEMA = "bdb-vnext-native-request-v1"
M9B_NATIVE_RESPONSE_SCHEMA = "bdb-vnext-native-response-v1"
M9B_NATIVE_MAX_MESSAGE_BYTES = 1024 * 1024
M9B_NATIVE_ACTIONS = frozenset({
    "status",
    "handshake",
    "admission.submit",
    "admission.lookup",
    "project_launch_peek",
    "project_launch_claim",
    "project_launch_ack",
    "project_execution_submit",
})


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


def _overlaps(left: Path, right: Path) -> bool:
    try:
        common = os.path.commonpath((os.fspath(left), os.fspath(right)))
    except ValueError:
        return False
    return common in {os.fspath(left), os.fspath(right)}


def _expected_origin() -> str:
    return f"chrome-extension://{BROWSER_EXTENSION_ID}/"


def _validate_caller_origin(value: str | None) -> str | None:
    if value is None:
        return None
    if value != _expected_origin():
        _fail("browser_origin_mismatch", "Native Messaging caller is not the pinned vNext extension")
    return value


@dataclass(frozen=True)
class VNextNativeConfig:
    runtime_root: Path
    legacy_runtime_root: Path
    bootstrap_authority_root: Path
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
        bootstrap = self.bootstrap_authority_root.expanduser().absolute()
        if _overlaps(runtime, legacy):
            _fail("runtime_overlap", "vNext and legacy runtime roots must be isolated")
        if _overlaps(bootstrap, runtime) or _overlaps(bootstrap, legacy):
            _fail("bootstrap_overlap", "external Bootstrap authority must be isolated from runtime roots")
        object.__setattr__(self, "runtime_root", runtime)
        object.__setattr__(self, "legacy_runtime_root", legacy)
        object.__setattr__(self, "bootstrap_authority_root", bootstrap)

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
            "bootstrap_authority_root",
        }
        if set(document) != expected_keys:
            _fail("invalid_config", "vNext Native config fields differ")
        runtime = Path(_bounded_text(document["runtime_root"], field="runtime_root", maximum=4096))
        legacy = Path(_bounded_text(document["legacy_runtime_root"], field="legacy_runtime_root", maximum=4096))
        bootstrap = Path(_bounded_text(document["bootstrap_authority_root"], field="bootstrap_authority_root", maximum=4096))
        if not runtime.is_absolute() or not legacy.is_absolute() or not bootstrap.is_absolute():
            _fail("invalid_config", "vNext Native roots must be absolute")
        return cls(
            runtime_root=runtime,
            legacy_runtime_root=legacy,
            bootstrap_authority_root=bootstrap,
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
            "bootstrap_authority_root": str(self.bootstrap_authority_root),
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


def _project_launch_queue() -> ProjectLaunchQueueAdapter:
    """Open the one vNext GUI queue; it is intentionally outside lifecycle DB state."""

    return ProjectLaunchQueueAdapter(default_project_launch_queue_path())


def _project_launch_response(config: VNextNativeConfig, request_id: str, *, status: str, launch: object = None, **extra: Any) -> dict[str, Any]:
    response = _base_response(config, request_id)
    response.update({"status": status, "launch": None if launch is None else launch.to_dict(), **extra})
    response["legacy_fallback"] = False
    return response


def _project_execution_response(config: VNextNativeConfig, request_id: str, receipt: Mapping[str, Any]) -> dict[str, Any]:
    response = _base_response(config, request_id)
    response.update({"status": "project_execution", "receipt": dict(receipt), "legacy_fallback": False})
    return response


def _conversation_id(value: object) -> str:
    if not isinstance(value, str) or len(value) < 8 or len(value) > 128 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", value):
        _fail("execution_conversation_invalid", "conversation_id is invalid")
    return value


def _activation_projection(config: VNextNativeConfig) -> dict[str, Any]:
    try:
        client = read_activation(config.runtime_root)
        bootstrap = observe_bootstrap_activation(authority_root=config.bootstrap_authority_root)
    except (M9bActivationError, M11cActiveReadError) as exc:
        raise M9bNativeError(exc.code, str(exc)) from exc
    if client is None:
        return {
            "state": "OFF",
            "activation_id": None,
            "bootstrap_state": bootstrap["status"],
            "production_acceptance": False,
            "writer_enabled": False,
            "intake_enabled": False,
        }
    bootstrap_matches = bootstrap["status"] == "ACTIVE" and bootstrap["slots"]["ACTIVE"]["source_commit"] == client.source_head
    effective = client.state == "ACTIVE" and client.writer_enabled and client.intake_enabled and bootstrap_matches
    return {
        "state": client.state,
        "activation_id": client.activation_id,
        "bootstrap_state": bootstrap["status"],
        "production_acceptance": bool(effective),
        "writer_enabled": client.writer_enabled,
        "intake_enabled": client.intake_enabled,
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


def handle_message(
    config: VNextNativeConfig,
    message: Mapping[str, Any],
    *,
    caller_origin: str | None = None,
) -> dict[str, Any]:
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
            "external_bootstrap_authority": True,
            "legacy_fallback": False,
            "legacy_receipts": False,
            "legacy_spool": False,
        }
        if caller_origin is not None:
            observation = message.get("browser_observation")
            if not isinstance(observation, Mapping):
                _fail("browser_runtime_observation_required", "Chrome handshake must prove exact running Browser package bytes")
            try:
                verification = record_browser_launch_verification(
                    runtime_root=config.runtime_root,
                    caller_origin=_validate_caller_origin(caller_origin) or "",
                    browser_observation=observation,
                )
            except M11cClientError as exc:
                raise M9bNativeError(exc.code, str(exc)) from exc
            response["client_verification_sha256"] = verification["verification_sha256"]
        return response

    if action == "project_execution_submit":
        conversation_id = _conversation_id(message.get("conversation_id"))
        launch_id = _bounded_text(message.get("launch_id"), field="launch_id", maximum=64)
        raw_result = message.get("result")
        try:
            submission = ProjectExecutionSubmission.from_mapping(raw_result)
            workflow = ProjectWorkflow(config.runtime_root, catalog=ProjectCatalog(config.runtime_root))
            receipt = workflow.submit_project_execution_result(submission.to_dict(), conversation_id=conversation_id, launch_id=launch_id)
            return _project_execution_response(config, request_id, receipt)
        except (ProjectExecutionError, ProjectWorkflowError) as exc:
            raise M9bNativeError(getattr(exc, "code", "project_execution_failed"), str(exc)) from exc

    # Project launch is a bounded transport handoff from the canonical GUI to
    # one eligible Browser conversation. It is deliberately independent of
    # production admission/activation: it never writes Task/Work state and it
    # cannot enable intake or change Bootstrap authority.
    if action in {"project_launch_peek", "project_launch_claim", "project_launch_ack"}:
        queue = _project_launch_queue()
        try:
            if action == "project_launch_peek":
                launch = queue.peek()
                return _project_launch_response(
                    config,
                    request_id,
                    status="project_launch" if launch is not None else "empty",
                    launch=launch,
                )
            launch_id = _bounded_text(message.get("launch_id"), field="launch_id", maximum=64)
            claim_id = _bounded_text(message.get("claim_id"), field="claim_id", maximum=64)
            if action == "project_launch_claim":
                conversation_id = message.get("conversation_id")
                if conversation_id is not None:
                    conversation_id = _conversation_id(conversation_id)
                    preview = queue.peek()
                    if preview is not None and preview.launch_id == launch_id and preview.project_id and preview.execution_binding_id:
                        try:
                            ProjectExecutionCoordinator(config.runtime_root, catalog=ProjectCatalog(config.runtime_root)).bind_conversation(preview.project_id, preview.execution_binding_id, conversation_id)
                        except ProjectExecutionError as exc:
                            raise M9bNativeError(exc.code, str(exc)) from exc
                launch = queue.claim(launch_id=launch_id, claim_id=claim_id, lease_seconds=30)
                return _project_launch_response(
                    config,
                    request_id,
                    status="claimed" if launch is not None else "busy_or_missing",
                    launch=launch,
                    launch_id=launch_id,
                    claim_id=claim_id,
                )
            acknowledged = queue.acknowledge(launch_id=launch_id, claim_id=claim_id)
            return _project_launch_response(
                config,
                request_id,
                status="acknowledged" if acknowledged else "not_found_or_not_owner",
                launch_id=launch_id,
                claim_id=claim_id,
            )
        except ProjectLaunchQueueError as exc:
            raise M9bNativeError(exc.code, str(exc)) from exc

    try:
        client_gate = require_active(config.runtime_root)
        require_bootstrap_active(config.bootstrap_authority_root, expected_source_head=client_gate.source_head)
    except (M9bActivationError, M11cActiveReadError) as exc:
        raise M9bNativeError(exc.code, str(exc)) from exc

    authority = CanonicalVNextAdmissionAuthority.open(config.runtime_root, legacy_root=config.legacy_runtime_root)
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


def serve(
    config: VNextNativeConfig,
    stdin: BinaryIO,
    stdout: BinaryIO,
    *,
    caller_origin: str | None = None,
) -> int:
    try:
        caller_origin = _validate_caller_origin(caller_origin)
    except M9bNativeError:
        return 2
    while True:
        try:
            message = read_native_message(stdin)
            if message is None:
                return 0
            request_id = str(message.get("request_id") or "invalid-request")[:128]
            try:
                response = handle_message(config, message, caller_origin=caller_origin)
            except (M9bNativeError, M3aError, M3cError, M9bActivationError, M11cActiveReadError, M11cClientError) as exc:
                response = _error_response(config, request_id, exc)
            write_native_message(stdout, response)
        except M9bNativeError:
            return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BDB Next dedicated Native Messaging host")
    parser.add_argument("caller_origin", nargs="?", help="Chrome Native Messaging caller origin")
    parser.add_argument("--parent-window", dest="parent_window", default=None)
    parser.add_argument("--config", default=str(default_vnext_native_config_path()))
    return parser


def _set_windows_binary_stdio() -> None:
    if os.name != "nt":
        return
    try:
        import msvcrt

        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    except (AttributeError, OSError):
        _fail("native_stdio_invalid", "Native Messaging stdio could not be set to binary mode")


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        config = VNextNativeConfig.from_json(args.config)
        _validate_caller_origin(args.caller_origin)
        _set_windows_binary_stdio()
    except (M9bNativeError, SystemExit):
        return 2
    return serve(config, sys.stdin.buffer, sys.stdout.buffer, caller_origin=args.caller_origin)


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

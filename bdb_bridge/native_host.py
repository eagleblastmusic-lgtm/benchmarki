from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable

from .local_result_sink import LocalResultSink
from .local_spool_transport import LOCAL_ENVELOPE_SCHEMA, LocalSpoolWriter
from .local_wake import signal_running_bridge
from .mirror_sync import MirrorSynchronizer
from .native_actions import ACTION_SCHEMA, NativeActionComposer, NativeSessionStore, RepositoryAlias
from .native_request_receipts import NativeRequestReceipt, NativeRequestReceiptStore
from .repository_search import SEARCH_TEXT_OPERATION, search_repository
from .repository_inspection import INSPECT_BUNDLE_OPERATION, inspect_repository
from .runtime_version import BDB_RUNTIME_VERSION, require_compatible_service_runtime
from .native_messaging import DEFAULT_MAX_MESSAGE_BYTES, read_native_message, write_native_message
from .protocol import (
    BridgeError,
    command_id_for,
    parse_strict_utc_timestamp,
    require_int,
    require_string,
    result_path_for,
    validate_session_id,
)


NATIVE_HOST_NAME = "com.bartosz.dev_bridge"
NATIVE_CONFIG_SCHEMA = "bdb-native-host-config-v1"
NATIVE_ARM_SCHEMA = "bdb-native-arm-v1"
NATIVE_REQUEST_SCHEMA = "bdb-native-request-v1"
NATIVE_RESPONSE_SCHEMA = "bdb-native-response-v1"
NATIVE_HOST_VERSION = BDB_RUNTIME_VERSION
_ORIGIN_RE = re.compile(r"^chrome-extension://[a-p]{32}/$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.json$")
_MAX_WAIT_SECONDS = 120.0
_SAFE_CLIENT_ERROR_CODES = frozenset(
    {
        "invalid_payload",
        "invalid_session_id",
        "unsupported_schema",
        "policy_denied",
        "journal_conflict",
        "dirty_source_checkout",
        "unsafe_path",
        "result_too_large",
        "mirror_sync_failed",
        "version_mismatch",
        "bridge_restart_required",
    }
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def default_native_config_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        root = Path(local_app_data)
    else:
        root = Path.home() / "AppData" / "Local"
    return (root / "BartoszDevBridge" / "native-host.json").resolve(strict=False)


def _atomic_json_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    temporary = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY)
            except OSError:
                directory_fd = None
            if directory_fd is not None:
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


@dataclass(frozen=True)
class NativeHostConfig:
    repositories: dict[str, RepositoryAlias]
    allowed_origins: tuple[str, ...]
    state_path: Path
    session_store_path: Path
    request_store_path: Path
    max_wait_seconds: float = 30.0
    max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES

    @classmethod
    def from_json(cls, path: str | Path) -> "NativeHostConfig":
        config_path = Path(path).expanduser().resolve(strict=True)
        if not config_path.is_file() or config_path.is_symlink():
            raise BridgeError("invalid_config", "Native host config must be a regular file")
        raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict) or raw.get("schema") != NATIVE_CONFIG_SCHEMA:
            raise BridgeError("unsupported_schema", "Native host config schema is unsupported")

        repositories_raw = raw.get("repositories")
        if repositories_raw is None:
            legacy_path = raw.get("bridge_config_path")
            if not isinstance(legacy_path, str) or not legacy_path:
                raise BridgeError("invalid_config", "Native host config requires repositories")
            repositories_raw = {"default": {"bridge_config_path": legacy_path}}
        if not isinstance(repositories_raw, dict) or not repositories_raw or len(repositories_raw) > 32:
            raise BridgeError("invalid_config", "repositories must be a non-empty object with at most 32 aliases")
        repositories: dict[str, RepositoryAlias] = {}
        for alias, item in repositories_raw.items():
            if not isinstance(alias, str):
                raise BridgeError("invalid_config", "Repository aliases must be strings")
            if isinstance(item, str):
                bridge_path = item
            elif isinstance(item, dict):
                bridge_path = require_string(item, "bridge_config_path")
            else:
                raise BridgeError("invalid_config", f"Repository alias {alias} has an invalid definition")
            repositories[alias] = RepositoryAlias.load(alias, bridge_path)

        origins = raw.get("allowed_origins")
        if (
            not isinstance(origins, list)
            or not origins
            or not all(isinstance(item, str) and _ORIGIN_RE.fullmatch(item) for item in origins)
        ):
            raise BridgeError("invalid_config", "allowed_origins must contain exact extension origins")
        if len(set(origins)) != len(origins):
            raise BridgeError("invalid_config", "allowed_origins must not contain duplicates")

        state_path = _local_config_path(raw.get("state_path"), config_path.parent / "native-host-arm.json", config_path.parent, "state_path")
        session_store_path = _local_config_path(
            raw.get("session_store_path"),
            config_path.parent / "native-host-sessions.json",
            config_path.parent,
            "session_store_path",
        )
        request_store_path = _local_config_path(
            raw.get("request_store_path"),
            config_path.parent / "native-host-requests.json",
            config_path.parent,
            "request_store_path",
        )
        if len({state_path, session_store_path, request_store_path}) != 3:
            raise BridgeError("invalid_config", "Native host state stores must use distinct paths")

        max_wait_seconds = float(raw.get("max_wait_seconds", 30.0))
        if not 0.0 <= max_wait_seconds <= _MAX_WAIT_SECONDS:
            raise BridgeError("invalid_config", "max_wait_seconds must be between 0 and 120")
        max_message_bytes = raw.get("max_message_bytes", DEFAULT_MAX_MESSAGE_BYTES)
        if (
            isinstance(max_message_bytes, bool)
            or not isinstance(max_message_bytes, int)
            or not 1024 <= max_message_bytes <= DEFAULT_MAX_MESSAGE_BYTES
        ):
            raise BridgeError("invalid_config", "max_message_bytes must be between 1024 and 1048576")

        return cls(
            repositories=repositories,
            allowed_origins=tuple(origins),
            state_path=state_path,
            session_store_path=session_store_path,
            request_store_path=request_store_path,
            max_wait_seconds=max_wait_seconds,
            max_message_bytes=max_message_bytes,
        )


def _local_config_path(raw: object, default: Path, parent: Path, field: str) -> Path:
    path = default if raw is None else Path(str(raw)).expanduser().resolve(strict=False)
    path = path.resolve(strict=False)
    if path.parent != parent:
        raise BridgeError("invalid_config", f"{field} must stay beside the native host config")
    return path


@dataclass(frozen=True)
class NativeArmStatus:
    armed: bool
    armed_until: str | None
    generation_id: str | None


class NativeArmStore:
    def __init__(self, path: str | Path, *, now_fn: Callable[[], datetime] = _utc_now) -> None:
        self.path = Path(path).expanduser().resolve(strict=False)
        self.now_fn = now_fn

    def arm(self, *, minutes: int) -> NativeArmStatus:
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 60:
            raise BridgeError("invalid_payload", "Arm duration must be between 1 and 60 minutes")
        until = self.now_fn() + timedelta(minutes=minutes)
        generation_id = secrets.token_hex(16)
        payload = {
            "schema": NATIVE_ARM_SCHEMA,
            "armed": True,
            "armed_until": _utc_text(until),
            "generation_id": generation_id,
        }
        _atomic_json_write(self.path, payload)
        return NativeArmStatus(True, payload["armed_until"], generation_id)

    def disarm(self) -> NativeArmStatus:
        payload = {
            "schema": NATIVE_ARM_SCHEMA,
            "armed": False,
            "armed_until": None,
            "generation_id": secrets.token_hex(16),
        }
        _atomic_json_write(self.path, payload)
        return NativeArmStatus(False, None, payload["generation_id"])

    def status(self) -> NativeArmStatus:
        if not self.path.exists():
            return NativeArmStatus(False, None, None)
        if self.path.is_symlink() or not self.path.is_file():
            raise BridgeError("invalid_config", "Native host arm state must be a regular file")
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise BridgeError("invalid_config", "Native host arm state is invalid JSON") from exc
        if not isinstance(raw, dict) or raw.get("schema") != NATIVE_ARM_SCHEMA:
            raise BridgeError("unsupported_schema", "Native host arm state schema is unsupported")
        generation_id = raw.get("generation_id")
        if generation_id is not None and not isinstance(generation_id, str):
            raise BridgeError("invalid_config", "Native host generation_id must be a string")
        if raw.get("armed") is not True:
            return NativeArmStatus(False, None, generation_id)
        armed_until = require_string(raw, "armed_until")
        until = parse_strict_utc_timestamp(armed_until, field="armed_until")
        if self.now_fn() >= until:
            return NativeArmStatus(False, armed_until, generation_id)
        return NativeArmStatus(True, armed_until, generation_id)


class NativeHostService:
    def __init__(
        self,
        native_config: NativeHostConfig,
        *,
        origin: str,
        now_fn: Callable[[], datetime] = _utc_now,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if origin not in native_config.allowed_origins:
            raise BridgeError("policy_denied", "Native messaging origin is not allowed")
        self.native_config = native_config
        self.origin = origin
        self.now_fn = now_fn
        self.sleeper = sleeper
        self.monotonic = monotonic
        self.arm_store = NativeArmStore(native_config.state_path, now_fn=now_fn)
        self.session_store = NativeSessionStore(native_config.session_store_path, writer=_atomic_json_write)
        self.request_store = NativeRequestReceiptStore(
            native_config.request_store_path,
            writer=_atomic_json_write,
        )
        self.action_composer = NativeActionComposer(
            native_config.repositories,
            self.session_store,
            now_fn=now_fn,
        )

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = require_string(request, "request_id")
        if _REQUEST_ID_RE.fullmatch(request_id) is None:
            raise BridgeError("invalid_payload", "request_id has an unsafe format")
        if request.get("schema") != NATIVE_REQUEST_SCHEMA:
            raise BridgeError("unsupported_schema", "Native request schema is unsupported")
        client_version = request.get("client_version")
        if client_version is not None and client_version != NATIVE_HOST_VERSION:
            raise BridgeError("version_mismatch", "Browser extension and Native Host versions differ")
        action = require_string(request, "action")

        if action == "status":
            return self._response(
                request_id,
                "status",
                arm=self._arm_payload(),
                repository_aliases=sorted(self.native_config.repositories),
            )
        if action == "context":
            alias = require_string(request, "repo_alias")
            repository = self._repository(alias)
            sync_requested = request.get("sync_mirror", False)
            if not isinstance(sync_requested, bool):
                raise BridgeError("invalid_payload", "context sync_mirror must be boolean")
            mirror_sync = None
            if sync_requested:
                arm = self.arm_store.status()
                if not arm.armed:
                    raise BridgeError("policy_denied", "Native host is DISARMED or its TTL expired")
                mirror_sync = self._try_sync_repository(repository, phase="pre_workspace_context")
            context = self.action_composer.context(alias)
            if mirror_sync is not None:
                context["mirror_sync"] = mirror_sync
            return self._response(
                request_id,
                "context",
                context=context,
                arm=self._arm_payload(),
            )
        if action == "lookup_submission_nonce":
            alias = require_string(request, "repo_alias")
            repository = self._repository(alias)
            client_submission_nonce = require_string(request, "client_submission_nonce")
            validate_session_id(client_submission_nonce)
            reservation = self.request_store.get_by_submission_nonce(client_submission_nonce)
            if reservation is None:
                return self._response(
                    request_id,
                    "submission_nonce_missing",
                    repo_alias=repository.alias,
                    client_submission_nonce=client_submission_nonce,
                )
            if require_string(reservation, "repo_alias") != repository.alias:
                raise BridgeError(
                    "journal_conflict",
                    "client_submission_nonce belongs to another repository alias",
                )
            return self._response(
                request_id,
                "submission_nonce_found",
                repo_alias=repository.alias,
                client_submission_nonce=client_submission_nonce,
                command_id=require_string(reservation, "command_id"),
                session_id=require_string(reservation, "session_id"),
                sequence=require_int(reservation, "sequence"),
                filename=require_string(reservation, "filename"),
            )
        if action not in {"submit", "submit_action", "search_text", "inspect_bundle", "result"}:
            raise BridgeError("policy_denied", "Native action is not allowed")

        arm = self.arm_store.status()
        if not arm.armed:
            raise BridgeError("policy_denied", "Native host is DISARMED or its TTL expired")
        wait_seconds = self._wait_seconds(request)

        if action == "search_text":
            bdb_action = request.get("bdb_action")
            if not isinstance(bdb_action, dict) or bdb_action.get("schema") != ACTION_SCHEMA:
                raise BridgeError("unsupported_schema", f"search_text requires {ACTION_SCHEMA}")
            alias = require_string(bdb_action, "repo_alias")
            if bdb_action.get("operation") != SEARCH_TEXT_OPERATION:
                raise BridgeError("invalid_payload", "search_text request operation mismatch")
            payload = bdb_action.get("payload")
            if not isinstance(payload, dict):
                raise BridgeError("invalid_payload", "search_text payload must be an object")
            repository = self._repository(alias)
            mirror_sync = self._try_sync_repository(repository, phase="pre_search_text")
            result = search_repository(repository.bridge_config, payload)
            if mirror_sync is not None:
                result["mirror_sync"] = mirror_sync
            return self._response(
                request_id,
                "completed",
                repo_alias=repository.alias,
                result=result,
                arm=self._arm_payload(),
            )

        if action == "inspect_bundle":
            bdb_action = request.get("bdb_action")
            if not isinstance(bdb_action, dict) or bdb_action.get("schema") != ACTION_SCHEMA:
                raise BridgeError("unsupported_schema", f"inspect_bundle requires {ACTION_SCHEMA}")
            alias = require_string(bdb_action, "repo_alias")
            if bdb_action.get("operation") != INSPECT_BUNDLE_OPERATION:
                raise BridgeError("invalid_payload", "inspect_bundle request operation mismatch")
            payload = bdb_action.get("payload")
            if not isinstance(payload, dict):
                raise BridgeError("invalid_payload", "inspect_bundle payload must be an object")
            repository = self._repository(alias)
            mirror_sync = self._try_sync_repository(repository, phase="pre_inspect_bundle")
            presentation = bdb_action.get("presentation")
            automation = bdb_action.get("automation")
            compact = bool(
                isinstance(presentation, dict) and presentation.get("mode") == "compact"
            ) or bool(isinstance(automation, dict) and automation.get("mode") == "auto")
            result = inspect_repository(repository.bridge_config, payload, compact=compact)
            if mirror_sync is not None:
                result["mirror_sync"] = mirror_sync
            return self._response(
                request_id,
                "completed",
                repo_alias=repository.alias,
                result=result,
                arm=self._arm_payload(),
            )

        if action == "submit_action":
            bdb_action = request.get("bdb_action")
            if not isinstance(bdb_action, dict):
                raise BridgeError("invalid_payload", "submit_action requires bdb_action")
            alias = require_string(bdb_action, "repo_alias")
            repository = self._repository(alias)
            action_sha256 = self._request_sha256(bdb_action)
            recovered = self._recover_request(
                request_id,
                action_sha256=action_sha256,
                repository=repository,
                wait_seconds=wait_seconds,
            )
            if recovered is not None:
                return recovered
            operation = bdb_action.get("operation")
            mutating = operation in {"replace_exact_and_test", "multi_file_patch"}
            if mutating:
                require_compatible_service_runtime(repository.bridge_config.journal_path)
            mirror_sync = (
                self._sync_repository(repository, phase="pre_action")
                if mutating
                else self._try_sync_repository(repository, phase="pre_action")
            )
            composed_repository, envelope = self.action_composer.compose(bdb_action)
            if composed_repository.alias != repository.alias:
                raise BridgeError("journal_conflict", "Composed repository alias changed unexpectedly")
            command = envelope["command"]
            assert isinstance(command, dict)
            session_id = require_string(command, "session_id")
            sequence = require_int(command, "sequence")
            default_filename = f"{session_id}-{sequence:06d}.json"
            filename = request.get("filename", default_filename)
            if not isinstance(filename, str) or _SAFE_FILENAME_RE.fullmatch(filename) is None:
                raise BridgeError("unsafe_path", "filename must be a safe .json basename")
            return self._submit_envelope(
                request_id,
                repository,
                envelope,
                filename=filename,
                wait_seconds=wait_seconds,
                mirror_sync=mirror_sync,
                request_receipt=NativeRequestReceipt(
                    request_id=request_id,
                    action_sha256=action_sha256,
                    repo_alias=repository.alias,
                    session_id=session_id,
                    sequence=sequence,
                    filename=filename,
                    created_at=_utc_text(self.now_fn()),
                    client_submission_nonce=require_string(command, "client_submission_nonce"),
                ),
            )

        if action == "submit":
            alias = require_string(request, "repo_alias")
            repository = self._repository(alias)
            envelope = request.get("envelope")
            if not isinstance(envelope, dict) or envelope.get("schema") != LOCAL_ENVELOPE_SCHEMA:
                raise BridgeError("invalid_payload", "submit requires bdb-local-envelope-v1")
            command = envelope.get("command")
            operation = command.get("operation") if isinstance(command, dict) else None
            mutating = operation in {"replace_exact_and_test", "multi_file_patch"}
            if mutating:
                require_compatible_service_runtime(repository.bridge_config.journal_path)
            mirror_sync = (
                self._sync_repository(repository, phase="pre_envelope")
                if mutating
                else self._try_sync_repository(repository, phase="pre_envelope")
            )
            filename = require_string(request, "filename")
            if _SAFE_FILENAME_RE.fullmatch(filename) is None:
                raise BridgeError("unsafe_path", "filename must be a safe .json basename")
            return self._submit_envelope(
                request_id,
                repository,
                envelope,
                filename=filename,
                wait_seconds=wait_seconds,
                mirror_sync=mirror_sync,
            )

        session_id = require_string(request, "session_id")
        validate_session_id(session_id)
        sequence = require_int(request, "sequence")
        if isinstance(sequence, bool) or sequence <= 0:
            raise BridgeError("invalid_payload", "sequence must be a positive integer")
        repository = self._repository_for_session(request, session_id)
        result = self._wait_for_result(repository, session_id, sequence, wait_seconds)
        if result is None:
            return self._response(
                request_id,
                "pending",
                command_id=command_id_for(session_id, sequence),
                repo_alias=repository.alias,
                arm=self._arm_payload(),
            )
        return self._response(
            request_id,
            "completed",
            command_id=command_id_for(session_id, sequence),
            repo_alias=repository.alias,
            result=result,
            arm=self._arm_payload(),
        )

    def _submit_envelope(
        self,
        request_id: str,
        repository: RepositoryAlias,
        envelope: dict[str, Any],
        *,
        filename: str,
        wait_seconds: float,
        mirror_sync: dict[str, Any] | None = None,
        request_receipt: NativeRequestReceipt | None = None,
    ) -> dict[str, Any]:
        command = envelope.get("command")
        manifest = envelope.get("manifest")
        if not isinstance(command, dict) or not isinstance(manifest, dict):
            raise BridgeError("invalid_payload", "Envelope manifest and command must be objects")
        session_id = require_string(command, "session_id")
        validate_session_id(session_id)
        sequence = require_int(command, "sequence")
        if isinstance(sequence, bool) or sequence <= 0:
            raise BridgeError("invalid_payload", "sequence must be a positive integer")
        expected_command_id = command_id_for(session_id, sequence)
        if require_string(command, "command_id") != expected_command_id:
            raise BridgeError("invalid_payload", "command_id does not match session_id and sequence")
        if require_string(manifest, "repository_id") != repository.bridge_config.repository_id:
            raise BridgeError("policy_denied", "Envelope repository_id does not match the trusted alias")

        if request_receipt is not None:
            self.request_store.reserve(request_receipt)
        destination = LocalSpoolWriter(repository.bridge_config.direct_spool_dir).submit(
            envelope,
            filename=filename,
        )
        wake_signaled = signal_running_bridge(repository.bridge_config.runtime_dir)
        result = self._wait_for_result(repository, session_id, sequence, wait_seconds)
        if result is None:
            return self._response(
                request_id,
                "accepted",
                command_id=expected_command_id,
                repo_alias=repository.alias,
                filename=destination.name,
                wake_signaled=wake_signaled,
                mirror_sync=mirror_sync,
                arm=self._arm_payload(),
            )
        client_result = dict(result)
        if mirror_sync is not None:
            client_result["mirror_sync"] = mirror_sync
        return self._response(
            request_id,
            "completed",
            command_id=expected_command_id,
            repo_alias=repository.alias,
            wake_signaled=wake_signaled,
            result=client_result,
            arm=self._arm_payload(),
        )

    @staticmethod
    def _request_sha256(value: dict[str, Any]) -> str:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="strict")
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def _recover_request(
        self,
        request_id: str,
        *,
        action_sha256: str,
        repository: RepositoryAlias,
        wait_seconds: float,
    ) -> dict[str, Any] | None:
        receipt = self.request_store.get(request_id)
        if receipt is None:
            return None
        if receipt.action_sha256 != action_sha256 or receipt.repo_alias != repository.alias:
            raise BridgeError("journal_conflict", "Native request_id replay does not match its receipt")
        result = self._wait_for_result(
            repository,
            receipt.session_id,
            receipt.sequence,
            wait_seconds,
        )
        if result is None:
            return self._response(
                request_id,
                "accepted",
                command_id=receipt.command_id,
                repo_alias=repository.alias,
                filename=receipt.filename,
                wake_signaled=False,
                request_recovered=True,
                arm=self._arm_payload(),
            )
        return self._response(
            request_id,
            "completed",
            command_id=receipt.command_id,
            repo_alias=repository.alias,
            request_recovered=True,
            result=result,
            arm=self._arm_payload(),
        )


    @staticmethod
    def _sync_repository(repository: RepositoryAlias, *, phase: str) -> dict[str, Any] | None:
        return MirrorSynchronizer(repository.bridge_config).sync(phase=phase)

    @staticmethod
    def _try_sync_repository(repository: RepositoryAlias, *, phase: str) -> dict[str, Any] | None:
        return MirrorSynchronizer(repository.bridge_config).try_sync(phase=phase)

    def _repository_for_session(self, request: dict[str, Any], session_id: str) -> RepositoryAlias:
        requested_alias = request.get("repo_alias")
        record = self.session_store.get(session_id)
        if record is not None:
            if requested_alias is not None and requested_alias != record.repo_alias:
                raise BridgeError("policy_denied", "Result request alias does not match the session")
            return self._repository(record.repo_alias)
        if not isinstance(requested_alias, str):
            raise BridgeError("invalid_payload", "repo_alias is required for an unregistered session")
        return self._repository(requested_alias)

    def _repository(self, alias: str) -> RepositoryAlias:
        repository = self.native_config.repositories.get(alias)
        if repository is None:
            raise BridgeError("policy_denied", "Repository alias is not configured")
        return repository

    def _wait_seconds(self, request: dict[str, Any]) -> float:
        wait_seconds = request.get("wait_seconds", self.native_config.max_wait_seconds)
        if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, (int, float)):
            raise BridgeError("invalid_payload", "wait_seconds must be a number")
        wait_seconds = float(wait_seconds)
        if not 0.0 <= wait_seconds <= self.native_config.max_wait_seconds:
            raise BridgeError("invalid_payload", "wait_seconds exceeds the configured maximum")
        return wait_seconds

    def _wait_for_result(
        self,
        repository: RepositoryAlias,
        session_id: str,
        sequence: int,
        wait_seconds: float,
    ) -> dict[str, Any] | None:
        remote_path = result_path_for(session_id, sequence)
        results = LocalResultSink(repository.bridge_config.direct_result_dir)
        deadline = self.monotonic() + wait_seconds
        while True:
            content = results.read(remote_path)
            if content is not None:
                try:
                    parsed = json.loads(content.decode("utf-8", errors="strict"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise BridgeError("journal_corrupt", "Local result is not strict UTF-8 JSON") from exc
                if not isinstance(parsed, dict):
                    raise BridgeError("journal_corrupt", "Local result root must be an object")
                return parsed
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                return None
            self.sleeper(min(0.05, remaining))

    def _arm_payload(self) -> dict[str, Any]:
        status = self.arm_store.status()
        return {
            "armed": status.armed,
            "armed_until": status.armed_until,
            "generation_id": status.generation_id,
        }

    @staticmethod
    def _response(request_id: str, status: str, **payload: Any) -> dict[str, Any]:
        return {
            "schema": NATIVE_RESPONSE_SCHEMA,
            "host_version": NATIVE_HOST_VERSION,
            "request_id": request_id,
            "status": status,
            **payload,
        }


def _error_response(request_id: str, exc: Exception) -> dict[str, Any]:
    code = str(getattr(exc, "code", "internal_error"))
    if code not in _SAFE_CLIENT_ERROR_CODES:
        code = "internal_error"
    public_messages = {
        "bridge_restart_required": (
            "Aktywna usługa BDB jest starsza lub niedostępna. Uruchom ponownie sesję BDB i ponów akcję."
        ),
        "version_mismatch": (
            "Wersje rozszerzenia i Native Host różnią się. Przeładuj rozszerzenie BDB."
        ),
    }
    message = public_messages.get(code, f"Native request failed: {code}")
    details = None
    if code == "invalid_payload" and isinstance(exc, BridgeError):
        message = str(exc)
        details = exc.details or {
            "rule_id": "invalid_payload",
            "phase": "native_validation",
            "effect_started": False,
        }
    error = {
        "code": code,
        "message": message,
    }
    if details is not None:
        error["details"] = details
    return {
        "schema": NATIVE_RESPONSE_SCHEMA,
        "host_version": NATIVE_HOST_VERSION,
        "request_id": request_id,
        "status": "failed",
        "error": error,
    }


def _write_error_diagnostic(
    native_config: NativeHostConfig,
    request_id: str,
    exc: Exception,
) -> None:
    try:
        stack = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        _atomic_json_write(
            native_config.request_store_path.parent / "native-host-last-error.json",
            {
                "schema": "bdb-native-host-error-v1",
                "host_version": NATIVE_HOST_VERSION,
                "request_id": request_id,
                "error_type": type(exc).__name__,
                "error_code": str(getattr(exc, "code", "internal_error")),
                "message": str(exc)[:1_000],
                "traceback": stack[-8_000:],
                "recorded_at": _utc_text(_utc_now()),
            },
        )
    except Exception:
        pass


def run_host(
    *,
    config_path: str | Path,
    origin: str,
    input_stream: BinaryIO,
    output_stream: BinaryIO,
) -> int:
    native_config = NativeHostConfig.from_json(config_path)
    service = NativeHostService(native_config, origin=origin)
    while True:
        request = read_native_message(
            input_stream,
            max_message_bytes=native_config.max_message_bytes,
        )
        if request is None:
            return 0
        request_id = request.get("request_id")
        safe_request_id = request_id if isinstance(request_id, str) and _REQUEST_ID_RE.fullmatch(request_id) else "invalid"
        try:
            response = service.handle(request)
        except Exception as exc:
            _write_error_diagnostic(native_config, safe_request_id, exc)
            response = _error_response(safe_request_id, exc)
        write_native_message(
            output_stream,
            response,
            max_message_bytes=native_config.max_message_bytes,
        )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="bdb-native-host")
    parser.add_argument("origin", nargs="?")
    parser.add_argument("--parent-window")
    parser.add_argument("--config")
    return parser.parse_args(argv)


def main() -> None:
    args = _parse_args(sys.argv[1:])
    origin = args.origin
    if not isinstance(origin, str) or _ORIGIN_RE.fullmatch(origin) is None:
        sys.exit(2)
    config_path = Path(args.config).expanduser().resolve(strict=False) if args.config else default_native_config_path()
    try:
        code = run_host(
            config_path=config_path,
            origin=origin,
            input_stream=sys.stdin.buffer,
            output_stream=sys.stdout.buffer,
        )
    except Exception:
        code = 1
    sys.exit(code)

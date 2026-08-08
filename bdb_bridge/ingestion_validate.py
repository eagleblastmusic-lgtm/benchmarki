from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .models import BridgeErrorCode
from .protocol import (
    BridgeError,
    SCHEMA_VERSION,
    command_id_for,
    command_path_for,
    manifest_path_for,
    parse_command_path,
    parse_manifest_path,
    parse_strict_utc_timestamp,
    require_int,
    require_string,
    validate_base_sha,
    validate_session_id,
    validate_strict_utc_timestamp,
)
from .repair_correlation import parse_repair_correlation
from .serializers import sha256_text


def raw_sha256(content: str) -> str:
    try:
        content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise BridgeError(
            BridgeErrorCode.INVALID_PAYLOAD,
            "Document content must be valid UTF-8",
        ) from exc
    return sha256_text(content)


def parse_manifest_envelope(content: str, *, source_path: str) -> dict[str, Any]:
    path_session_id = parse_manifest_path(source_path)
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise BridgeError(
            BridgeErrorCode.INVALID_JSON,
            f"Invalid manifest JSON at {source_path}: {exc}",
        ) from exc
    if not isinstance(parsed, dict):
        raise BridgeError(
            BridgeErrorCode.INVALID_MANIFEST,
            f"Manifest must be a JSON object at {source_path}",
        )
    if parsed.get("schema_version") != SCHEMA_VERSION:
        raise BridgeError(
            BridgeErrorCode.UNSUPPORTED_SCHEMA,
            f"Unsupported manifest schema at {source_path}",
        )
    session_id = require_string(parsed, "session_id")
    validate_session_id(session_id)
    if session_id != path_session_id:
        raise BridgeError(
            BridgeErrorCode.SESSION_MISMATCH,
            f"Manifest session_id does not match path {source_path}",
        )
    repository_id = require_string(parsed, "repository_id")
    base_sha = validate_base_sha(require_string(parsed, "base_sha"))
    created_at = validate_strict_utc_timestamp(require_string(parsed, "created_at"), field="created_at")
    expires_at = validate_strict_utc_timestamp(require_string(parsed, "expires_at"), field="expires_at")
    if parse_strict_utc_timestamp(expires_at, field="expires_at") <= parse_strict_utc_timestamp(
        created_at, field="created_at"
    ):
        raise BridgeError(
            BridgeErrorCode.INVALID_MANIFEST,
            "Manifest expires_at must be after created_at",
        )
    correlation = parse_repair_correlation(
        parsed.get("repair_correlation"),
        session_id=session_id,
        field="manifest.repair_correlation",
    )
    if correlation is not None:
        parsed["repair_correlation"] = correlation.as_dict()
    return parsed


def parse_command_envelope(content: str, *, source_path: str) -> dict[str, Any]:
    path_session_id, path_sequence = parse_command_path(source_path)
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise BridgeError(
            BridgeErrorCode.INVALID_JSON,
            f"Invalid command JSON at {source_path}: {exc}",
        ) from exc
    if not isinstance(parsed, dict):
        raise BridgeError(
            BridgeErrorCode.INVALID_PAYLOAD,
            f"Command must be a JSON object at {source_path}",
        )
    if parsed.get("schema_version") != SCHEMA_VERSION:
        raise BridgeError(
            BridgeErrorCode.UNSUPPORTED_SCHEMA,
            f"Unsupported command schema at {source_path}",
        )
    session_id = require_string(parsed, "session_id")
    validate_session_id(session_id)
    if session_id != path_session_id:
        raise BridgeError(
            BridgeErrorCode.SESSION_MISMATCH,
            f"Command session_id does not match path {source_path}",
        )
    if "task_id" in parsed:
        task_id = parsed["task_id"]
        if not isinstance(task_id, str):
            raise BridgeError(
                BridgeErrorCode.INVALID_PAYLOAD,
                "Command task_id must be a string",
            )
        try:
            validate_session_id(task_id)
        except BridgeError as exc:
            raise BridgeError(
                BridgeErrorCode.INVALID_PAYLOAD,
                "Command task_id must be UUID or ULID",
            ) from exc
    if "attempt_id" in parsed:
        attempt_id = parsed["attempt_id"]
        if not isinstance(attempt_id, str):
            raise BridgeError(
                BridgeErrorCode.INVALID_PAYLOAD,
                "Command attempt_id must be a string",
            )
        try:
            validate_session_id(attempt_id)
        except BridgeError as exc:
            raise BridgeError(
                BridgeErrorCode.INVALID_PAYLOAD,
                "Command attempt_id must be UUID or ULID",
            ) from exc
    if "client_submission_nonce" in parsed:
        submission_nonce = parsed["client_submission_nonce"]
        if not isinstance(submission_nonce, str):
            raise BridgeError(
                BridgeErrorCode.INVALID_PAYLOAD,
                "Command client_submission_nonce must be a string",
            )
        try:
            validate_session_id(submission_nonce)
        except BridgeError as exc:
            raise BridgeError(
                BridgeErrorCode.INVALID_PAYLOAD,
                "Command client_submission_nonce must be UUID or ULID",
            ) from exc
    sequence = require_int(parsed, "sequence")
    if isinstance(sequence, bool) or sequence <= 0:
        raise BridgeError(
            BridgeErrorCode.INVALID_PAYLOAD,
            "Command sequence must be a positive integer",
        )
    if sequence != path_sequence:
        raise BridgeError(
            BridgeErrorCode.SEQUENCE_MISMATCH,
            f"Command sequence does not match path {source_path}",
        )
    expected_command_id = command_id_for(session_id, sequence)
    command_id = require_string(parsed, "command_id")
    if command_id != expected_command_id:
        raise BridgeError(
            BridgeErrorCode.COMMAND_ID_MISMATCH,
            f"Command command_id does not match session and sequence for {source_path}",
        )
    operation = require_string(parsed, "operation")
    payload = parsed.get("payload")
    if not isinstance(payload, dict):
        raise BridgeError(
            BridgeErrorCode.INVALID_PAYLOAD,
            "Command payload must be a JSON object",
        )
    expected_revision = require_int(parsed, "expected_revision")
    if isinstance(expected_revision, bool) or expected_revision < 0:
        raise BridgeError(
            BridgeErrorCode.INVALID_REVISION,
            "Command expected_revision must be a non-negative integer",
        )
    expected_state_hash = parsed.get("expected_state_hash")
    if expected_state_hash is not None and (
        not isinstance(expected_state_hash, str) or not expected_state_hash
    ):
        raise BridgeError(
            BridgeErrorCode.INVALID_PAYLOAD,
            "Command expected_state_hash must be null or a non-empty string",
        )
    created_at = validate_strict_utc_timestamp(require_string(parsed, "created_at"), field="created_at")
    expires_at = validate_strict_utc_timestamp(require_string(parsed, "expires_at"), field="expires_at")
    if parse_strict_utc_timestamp(expires_at, field="expires_at") <= parse_strict_utc_timestamp(
        created_at, field="created_at"
    ):
        raise BridgeError(
            BridgeErrorCode.INVALID_PAYLOAD,
            "Command expires_at must be after created_at",
        )
    return parsed


def is_expired(expires_at: str, *, now: datetime) -> bool:
    return now >= parse_strict_utc_timestamp(expires_at, field="expires_at")


def manifest_path_matches_session(source_path: str, session_id: str) -> bool:
    try:
        return parse_manifest_path(source_path) == session_id
    except BridgeError:
        return False


def command_path_matches(source_path: str, session_id: str, sequence: int) -> bool:
    try:
        path_session_id, path_sequence = parse_command_path(source_path)
    except BridgeError:
        return False
    return path_session_id == session_id and path_sequence == sequence


def expected_manifest_path(session_id: str) -> str:
    return manifest_path_for(session_id)


def expected_command_path(session_id: str, sequence: int) -> str:
    return command_path_for(session_id, sequence)

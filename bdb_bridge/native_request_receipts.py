from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .protocol import BridgeError, require_int, require_string, validate_session_id


REQUEST_RECEIPT_SCHEMA = "bdb-native-request-receipts-v1"
_MAX_REQUEST_RECEIPTS = 1_024
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class NativeRequestReceipt:
    request_id: str
    action_sha256: str
    repo_alias: str
    session_id: str
    sequence: int
    filename: str
    created_at: str
    client_submission_nonce: str | None = None

    @property
    def command_id(self) -> str:
        return f"{self.session_id}:{self.sequence:06d}"


class NativeRequestReceiptStore:
    """Durably map one Native Messaging request to one submitted command."""

    def __init__(self, path: str | Path, *, writer: Callable[[Path, dict[str, Any]], None]) -> None:
        self.path = Path(path).expanduser().resolve(strict=False)
        self._writer = writer

    def get(self, request_id: str) -> NativeRequestReceipt | None:
        raw = self._read()
        item = raw["requests"].get(request_id)
        return None if item is None else self._record(request_id, item)

    def get_by_submission_nonce(self, client_submission_nonce: str) -> dict[str, Any] | None:
        validate_session_id(client_submission_nonce)
        raw = self._read()
        item = raw["submission_reservations"].get(client_submission_nonce)
        if item is None:
            return None
        if not isinstance(item, dict):
            raise BridgeError("invalid_config", "Native submission reservation is invalid")
        action_sha256 = require_string(item, "action_sha256")
        if _SHA256_RE.fullmatch(action_sha256) is None:
            raise BridgeError("invalid_config", "Native submission reservation hash is invalid")
        session_id = require_string(item, "session_id")
        validate_session_id(session_id)
        sequence = require_int(item, "sequence")
        if isinstance(sequence, bool) or sequence <= 0:
            raise BridgeError("invalid_config", "Native submission reservation sequence is invalid")
        command_id = require_string(item, "command_id")
        expected_command_id = f"{session_id}:{sequence:06d}"
        if command_id != expected_command_id:
            raise BridgeError("invalid_config", "Native submission reservation command_id is invalid")
        require_string(item, "repo_alias")
        require_string(item, "filename")
        require_string(item, "created_at")
        return dict(item)

    def bind(self, receipt: NativeRequestReceipt) -> NativeRequestReceipt:
        return self.reserve(receipt)

    def reserve(self, receipt: NativeRequestReceipt) -> NativeRequestReceipt:
        candidate = self._document(receipt)
        raw = self._read()
        requests = raw["requests"]
        reservations = raw.setdefault("submission_reservations", {})
        existing = requests.get(receipt.request_id)
        if existing is not None and existing != candidate:
            raise BridgeError("journal_conflict", "Native request_id is bound to another command")

        nonce = receipt.client_submission_nonce
        if nonce is not None:
            validate_session_id(nonce)
            reservation = {
                "command_id": receipt.command_id,
                "action_sha256": receipt.action_sha256,
                "repo_alias": receipt.repo_alias,
                "session_id": receipt.session_id,
                "sequence": receipt.sequence,
                "filename": receipt.filename,
                "created_at": receipt.created_at,
            }
            existing_reservation = reservations.get(nonce)
            if existing_reservation is not None:
                if not isinstance(existing_reservation, dict):
                    raise BridgeError("invalid_config", "Native submission reservation is invalid")
                comparable = {key: value for key, value in reservation.items() if key != "created_at"}
                existing_comparable = {
                    key: value for key, value in existing_reservation.items() if key != "created_at"
                }
                if existing_comparable != comparable:
                    raise BridgeError(
                        "journal_conflict",
                        "client_submission_nonce is reserved for another command",
                    )
            else:
                reservations[nonce] = reservation

        if existing is None:
            requests[receipt.request_id] = candidate
        if len(requests) > _MAX_REQUEST_RECEIPTS:
            oldest = sorted(
                requests,
                key=lambda key: str(requests[key].get("created_at", ""))
                if isinstance(requests[key], dict)
                else "",
            )[: len(requests) - _MAX_REQUEST_RECEIPTS]
            for key in oldest:
                del requests[key]
        if len(reservations) > _MAX_REQUEST_RECEIPTS:
            oldest_reservations = sorted(
                reservations,
                key=lambda key: str(reservations[key].get("created_at", ""))
                if isinstance(reservations[key], dict)
                else "",
            )[: len(reservations) - _MAX_REQUEST_RECEIPTS]
            for key in oldest_reservations:
                del reservations[key]
        self._writer(self.path, raw)
        return receipt

    @staticmethod
    def _document(receipt: NativeRequestReceipt) -> dict[str, Any]:
        validate_session_id(receipt.session_id)
        if _SHA256_RE.fullmatch(receipt.action_sha256) is None:
            raise BridgeError("invalid_payload", "Native request action hash is invalid")
        if receipt.sequence <= 0:
            raise BridgeError("invalid_payload", "Native request sequence must be positive")
        return {
            "action_sha256": receipt.action_sha256,
            "repo_alias": receipt.repo_alias,
            "session_id": receipt.session_id,
            "sequence": receipt.sequence,
            "filename": receipt.filename,
            "created_at": receipt.created_at,
        }

    def _record(self, request_id: str, item: Any) -> NativeRequestReceipt:
        if not isinstance(item, dict):
            raise BridgeError("invalid_config", "Native request receipt is invalid")
        action_sha256 = require_string(item, "action_sha256")
        if _SHA256_RE.fullmatch(action_sha256) is None:
            raise BridgeError("invalid_config", "Native request receipt hash is invalid")
        session_id = require_string(item, "session_id")
        validate_session_id(session_id)
        sequence = require_int(item, "sequence")
        if isinstance(sequence, bool) or sequence <= 0:
            raise BridgeError("invalid_config", "Native request receipt sequence is invalid")
        return NativeRequestReceipt(
            request_id=request_id,
            action_sha256=action_sha256,
            repo_alias=require_string(item, "repo_alias"),
            session_id=session_id,
            sequence=sequence,
            filename=require_string(item, "filename"),
            created_at=require_string(item, "created_at"),
        )

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "schema": REQUEST_RECEIPT_SCHEMA,
                "requests": {},
                "submission_reservations": {},
            }
        if self.path.is_symlink() or not self.path.is_file():
            raise BridgeError("invalid_config", "Native request receipt store must be a regular file")
        import json

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise BridgeError("invalid_config", "Native request receipt store is invalid JSON") from exc
        if not isinstance(raw, dict) or raw.get("schema") != REQUEST_RECEIPT_SCHEMA:
            raise BridgeError("unsupported_schema", "Native request receipt store schema is unsupported")
        requests = raw.get("requests")
        if not isinstance(requests, dict) or len(requests) > _MAX_REQUEST_RECEIPTS:
            raise BridgeError("invalid_config", "Native request receipt store is invalid")
        reservations = raw.setdefault("submission_reservations", {})
        if not isinstance(reservations, dict) or len(reservations) > _MAX_REQUEST_RECEIPTS:
            raise BridgeError("invalid_config", "Native submission reservation store is invalid")
        return raw

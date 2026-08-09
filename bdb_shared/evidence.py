"""Canonical evidence helpers shared without importing a runtime generation."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any


SANITIZATION_VERSION = "runtime-inventory-sanitized-v1"

_SECRET_KEY = re.compile(
    r"(?i)(token|password|passwd|secret|cookie|authorization|api[_-]?key|access[_-]?key|private[_-]?key)"
)
_SECRET_URL = re.compile(r"(https?://)[^\s/@]+(?::[^\s/@]*)?@", re.IGNORECASE)
_ABSOLUTE_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/)")
_VOLATILE_SEMANTIC_KEYS = frozenset(
    {
        "inventory_id",
        "observed_at",
        "started_at",
        "finished_at",
        "duration_ms",
        "mtime_ns",
        "ctime_ns",
        "message",
    }
)


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def semantic_payload(report: Mapping[str, Any]) -> dict[str, Any]:
    def strip(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                str(key): strip(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
                if str(key) not in _VOLATILE_SEMANTIC_KEYS
                and str(key) not in {"semantic_digest", "representation", "sanitization"}
            }
        if isinstance(value, (list, tuple)):
            return [strip(item) for item in value]
        return value

    return strip(report)


def semantic_digest(report: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(canonical_json_bytes(semantic_payload(report))).hexdigest()
    return f"sha256:{digest}"


def _pseudonym(value: str, *, salt: bytes, kind: str) -> str:
    digest = hashlib.sha256(
        salt + b"\0" + kind.encode("ascii") + b"\0" + value.encode("utf-8")
    ).hexdigest()
    return f"<{kind}:{digest[:16]}>"


def sanitize_report(report: Mapping[str, Any]) -> dict[str, Any]:
    private = copy.deepcopy(dict(report))
    salt = str(private.get("semantic_digest", "runtime-inventory")).encode("utf-8")
    identifier_keys = {
        "repository_id",
        "session_id",
        "command_id",
        "request_id",
        "client_submission_nonce",
        "instance_id",
        "repo_alias",
        "alias",
        "dirty_entries",
        "seen_keys",
        "request_ids",
        "ids",
        "filename",
        "allowed_origins",
        "head",
        "upstream_oid",
        "source_commit",
        "parent_commit",
        "repository_head",
        "promoter_commit",
    }

    def sanitize(value: Any, *, key: str | None = None) -> Any:
        if key is not None and _SECRET_KEY.search(key):
            return "[REDACTED]"
        if isinstance(value, Mapping):
            return {
                str(item_key): sanitize(item_value, key=str(item_key))
                for item_key, item_value in value.items()
            }
        if isinstance(value, list):
            return [sanitize(item, key=key) for item in value]
        if isinstance(value, tuple):
            return [sanitize(item, key=key) for item in value]
        if isinstance(value, str):
            if key == "message":
                return "[REDACTED]"
            if key and ("path" in key.casefold() or key.casefold() in {"root", "remote"}):
                return _pseudonym(value, salt=salt, kind="path")
            if key in identifier_keys or (key and (key.endswith("_id") or key.endswith("_nonce"))):
                return _pseudonym(value, salt=salt, kind="id")
            if _ABSOLUTE_PATH.match(value):
                return _pseudonym(value, salt=salt, kind="path")
            return _SECRET_URL.sub(r"\1<redacted>@", value)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return str(value)

    sanitized = sanitize(private)
    sanitized["representation"] = "SANITIZED"
    sanitized["sanitization"] = {
        "version": SANITIZATION_VERSION,
        "pseudonym_scope": "semantic_digest",
        "pseudonyms_are_linkable_not_anonymous": True,
        "private_report_unchanged": True,
    }
    return sanitized

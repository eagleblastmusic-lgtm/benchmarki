from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from bdb_bridge.protocol import (
    COMMAND_PATH_RE,
    SCHEMA_VERSION,
    SESSION_RE,
    BridgeError,
    path_matches,
    require_int,
    require_string,
    result_path_for,
    validate_path_pattern,
    validate_repo_relative_path,
    validate_session_id,
)
from bdb_bridge.serializers import (
    MAX_RESULT_BYTES,
    MAX_TAIL_CHARS,
    canonical_json,
    finalize_result,
    sha256_text,
    tail,
)

EXECUTOR_VERSION = "0.1.0-poc"
MAX_READ_LINES = 400


def sanitized_test_environment() -> dict[str, str]:
    import sys
    allowed = ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0"})
    env["PYTHONPATH"] = os.environ.get("PYTHONPATH", os.pathsep.join(sys.path))
    return env


def changed_paths(status: str) -> list[str]:
    paths: list[str] = []
    for line in status.splitlines():
        if len(line) < 4:
            continue
        value = line[3:]
        if " -> " in value:
            value = value.split(" -> ", 1)[1]
        paths.append(value.replace("\\", "/"))
    return sorted(paths)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def text_or_empty(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value


def summary_from_test(outcome: dict[str, Any]) -> str:
    combined = (outcome["stdout"] + "\n" + outcome["stderr"]).strip().splitlines()
    if combined:
        return tail(combined[-1], 500)
    return f"pytest exit code {outcome['exit_code']}"

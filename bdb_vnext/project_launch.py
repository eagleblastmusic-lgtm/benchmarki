"""Narrow vNext-to-Browser prompt queue adapter.

The queue is transport only.  Canonical project metadata remains in
``project_catalog``; the Browser/Native consumer claims and acknowledges the
bounded pending launch using the existing v1 JSON contract.
"""

from __future__ import annotations

import json
import os
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


PROJECT_LAUNCH_SCHEMA = "bdb-project-launch-v1"
PROJECT_LAUNCH_QUEUE_SCHEMA = "bdb-project-launch-queue-v1"
MAX_PROJECT_PROMPT_CHARS = 50_000


def default_project_launch_queue_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    root = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return (root / "BartoszDevBridge" / "project-launch-queue.json").absolute()


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class ProjectLaunch:
    launch_id: str
    repo_alias: str
    prompt: str
    auto_send: bool
    created_at: str
    expires_at: str
    schema: str = PROJECT_LAUNCH_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "launch_id": self.launch_id,
            "repo_alias": self.repo_alias,
            "prompt": self.prompt,
            "auto_send": self.auto_send,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


class ProjectLaunchQueueError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ProjectLaunchQueueAdapter:
    """Atomic single-pending launch writer used by the vNext GUI."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or default_project_launch_queue_path()).expanduser().absolute()

    def enqueue(self, *, repo_alias: str, prompt: str, ttl_minutes: int = 10) -> ProjectLaunch:
        if not isinstance(repo_alias, str) or not repo_alias or len(repo_alias) > 32 or not repo_alias[0].islower():
            raise ProjectLaunchQueueError("repo_alias_invalid", "repo_alias is not bounded")
        normalized = prompt.strip()
        if not normalized or len(normalized) > MAX_PROJECT_PROMPT_CHARS:
            raise ProjectLaunchQueueError("prompt_invalid", "prompt is empty or exceeds its bound")
        if isinstance(ttl_minutes, bool) or not isinstance(ttl_minutes, int) or not 1 <= ttl_minutes <= 60:
            raise ProjectLaunchQueueError("ttl_invalid", "ttl_minutes must be between 1 and 60")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_name(self.path.name + ".lock")
        descriptor: int | None = None
        deadline = time.monotonic() + 3.0
        while descriptor is None:
            try:
                descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise ProjectLaunchQueueError("queue_busy", "project launch queue is busy")
                time.sleep(0.01)
        try:
            raw = None
            if self.path.exists():
                if self.path.is_symlink() or not self.path.is_file():
                    raise ProjectLaunchQueueError("queue_path_invalid", "project launch queue must be a regular file")
                raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
                if not isinstance(raw, dict) or raw.get("schema") != PROJECT_LAUNCH_QUEUE_SCHEMA:
                    raise ProjectLaunchQueueError("queue_schema_invalid", "project launch queue schema is unsupported")
                pending = raw.get("pending")
                if isinstance(pending, dict) and pending.get("expires_at"):
                    try:
                        expires = datetime.fromisoformat(str(pending["expires_at"])[:-1] + "+00:00")
                    except ValueError:
                        raise ProjectLaunchQueueError("queue_corrupt", "pending launch expiry is invalid")
                    if datetime.now(timezone.utc) < expires:
                        raise ProjectLaunchQueueError("queue_pending", "project launch queue already contains a pending prompt")
            now = datetime.now(timezone.utc)
            launch = ProjectLaunch(str(uuid.uuid4()), repo_alias, normalized, False, _utc_text(now), _utc_text(now + timedelta(minutes=ttl_minutes)))
            payload = {"schema": PROJECT_LAUNCH_QUEUE_SCHEMA, "pending": launch.to_dict(), "claim": None}
            temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
            try:
                with temporary.open("xb") as handle:
                    handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8"))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
            return launch
        finally:
            if descriptor is not None:
                os.close(descriptor)
            lock.unlink(missing_ok=True)


__all__ = ["MAX_PROJECT_PROMPT_CHARS", "PROJECT_LAUNCH_SCHEMA", "PROJECT_LAUNCH_QUEUE_SCHEMA", "ProjectLaunch", "ProjectLaunchQueueAdapter", "ProjectLaunchQueueError", "default_project_launch_queue_path"]

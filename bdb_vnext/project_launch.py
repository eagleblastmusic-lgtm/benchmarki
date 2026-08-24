"""Narrow vNext-to-Browser prompt queue adapter.

The queue is transport only. Canonical project metadata remains in
``project_catalog``; the Browser/Native consumer claims and acknowledges the
bounded pending launch using the existing v1 JSON contract. The claim is a
short-lived lease, never a second semantic authority.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping


PROJECT_LAUNCH_SCHEMA = "bdb-project-launch-v1"
PROJECT_LAUNCH_QUEUE_SCHEMA = "bdb-project-launch-queue-v1"
PROJECT_LAUNCH_CLAIM_SCHEMA = "bdb-project-launch-claim-v1"
MAX_PROJECT_PROMPT_CHARS = 50_000
_ALIAS_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_LOCK_TIMEOUT_SECONDS = 3.0
_STALE_LOCK_SECONDS = 30.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def default_project_launch_queue_path() -> Path:
    """Return the one queue path shared by the canonical GUI and Native host."""

    local_app_data = os.environ.get("LOCALAPPDATA")
    root = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return (root / "BartoszDevBridge" / "project-launch-queue.json").absolute()


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_utc(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"{field} must use canonical UTC Z form")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include timezone")
    return parsed.astimezone(timezone.utc)


def _uuid_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a UUID string")
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{field} must be a UUID string") from exc
    return value


def _atomic_json_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise ProjectLaunchQueueError("queue_write_failed", "project launch queue publication failed") from exc


@dataclass(frozen=True)
class ProjectLaunch:
    launch_id: str
    repo_alias: str
    prompt: str
    auto_send: bool
    created_at: str
    expires_at: str
    schema: str = PROJECT_LAUNCH_SCHEMA
    project_id: str | None = None
    plan_version: str | None = None
    task_id: str | None = None
    execution_binding_id: str | None = None
    correlation_id: str | None = None
    command_id: str | None = None
    expected_repo_head_before: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "launch_id": self.launch_id,
            "repo_alias": self.repo_alias,
            "prompt": self.prompt,
            "auto_send": self.auto_send,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            **({"project_id": self.project_id} if self.project_id is not None else {}),
            **({"plan_version": self.plan_version} if self.plan_version is not None else {}),
            **({"task_id": self.task_id} if self.task_id is not None else {}),
            **({"execution_binding_id": self.execution_binding_id} if self.execution_binding_id is not None else {}),
            **({"correlation_id": self.correlation_id} if self.correlation_id is not None else {}),
            **({"command_id": self.command_id} if self.command_id is not None else {}),
            **({"expected_repo_head_before": self.expected_repo_head_before} if self.expected_repo_head_before is not None else {}),
        }

    @classmethod
    def from_dict(cls, value: object) -> "ProjectLaunch":
        if not isinstance(value, Mapping) or value.get("schema") != PROJECT_LAUNCH_SCHEMA:
            raise ValueError("project launch schema is unsupported")
        launch_id = _uuid_text(value.get("launch_id"), "launch_id")
        repo_alias = value.get("repo_alias")
        prompt = value.get("prompt")
        auto_send = value.get("auto_send")
        created_at = value.get("created_at")
        expires_at = value.get("expires_at")
        if not isinstance(repo_alias, str) or _ALIAS_RE.fullmatch(repo_alias) is None:
            raise ValueError("repo_alias has an unsafe format")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROJECT_PROMPT_CHARS:
            raise ValueError("prompt must be non-empty and bounded")
        if not isinstance(auto_send, bool):
            raise ValueError("project launch auto_send must be boolean")
        created = _parse_utc(created_at, "created_at")
        expires = _parse_utc(expires_at, "expires_at")
        if expires <= created:
            raise ValueError("expires_at must be later than created_at")
        optional = {
            field: value.get(field)
            for field in (
                "project_id", "plan_version", "task_id", "execution_binding_id",
                "correlation_id", "command_id", "expected_repo_head_before",
            )
            if value.get(field) is not None
        }
        if any(not isinstance(item, str) or not item or len(item) > 256 for item in optional.values()):
            raise ValueError("project launch metadata is invalid")
        return cls(
            launch_id=launch_id,
            repo_alias=repo_alias,
            prompt=prompt,
            auto_send=auto_send,
            created_at=created_at,
            expires_at=expires_at,
            schema=PROJECT_LAUNCH_SCHEMA,
            **optional,
        )


@dataclass(frozen=True)
class ProjectLaunchClaim:
    claim_id: str
    launch_id: str
    claimed_at: str
    expires_at: str
    schema: str = PROJECT_LAUNCH_CLAIM_SCHEMA

    def to_dict(self) -> dict[str, str]:
        return {
            "schema": self.schema,
            "claim_id": self.claim_id,
            "launch_id": self.launch_id,
            "claimed_at": self.claimed_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ProjectLaunchClaim":
        if not isinstance(value, Mapping) or value.get("schema") != PROJECT_LAUNCH_CLAIM_SCHEMA:
            raise ValueError("project launch claim schema is unsupported")
        claim_id = _uuid_text(value.get("claim_id"), "claim_id")
        launch_id = _uuid_text(value.get("launch_id"), "launch_id")
        claimed_at = value.get("claimed_at")
        expires_at = value.get("expires_at")
        claimed = _parse_utc(claimed_at, "claimed_at")
        expires = _parse_utc(expires_at, "expires_at")
        if expires <= claimed:
            raise ValueError("claim expires_at must be later than claimed_at")
        return cls(claim_id, launch_id, claimed_at, expires_at)


class ProjectLaunchQueueError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ProjectLaunchQueueAdapter:
    """Atomic single-pending launch writer/lease used by GUI and vNext Native."""

    def __init__(self, path: str | Path | None = None, *, now_fn: Callable[[], datetime] | None = None) -> None:
        self.path = Path(path or default_project_launch_queue_path()).expanduser().absolute()
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.now_fn = now_fn or _utc_now

    def _from_dict(self, value: Mapping[str, Any]) -> ProjectLaunch:
        try:
            return ProjectLaunch.from_dict(value)
        except (KeyError, TypeError, ValueError) as exc:
            raise ProjectLaunchQueueError("queue_corrupt", "project launch is invalid") from exc

    def _read_state_unlocked(self) -> tuple[ProjectLaunch | None, ProjectLaunchClaim | None]:
        if not self.path.exists():
            return None, None
        if self.path.is_symlink() or not self.path.is_file():
            raise ProjectLaunchQueueError("queue_path_invalid", "project launch queue must be a regular file")
        try:
            document = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProjectLaunchQueueError("queue_corrupt", "project launch queue is not valid JSON") from exc
        if not isinstance(document, Mapping) or document.get("schema") != PROJECT_LAUNCH_QUEUE_SCHEMA:
            raise ProjectLaunchQueueError("queue_schema_invalid", "project launch queue schema is unsupported")
        try:
            pending = None if document.get("pending") is None else self._from_dict(document["pending"])
            claim = None if document.get("claim") is None else ProjectLaunchClaim.from_dict(document["claim"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProjectLaunchQueueError("queue_corrupt", "project launch queue state is invalid") from exc
        if claim is not None and pending is not None and claim.launch_id != pending.launch_id:
            raise ProjectLaunchQueueError("queue_corrupt", "project launch claim does not match pending launch")
        return pending, claim

    def _normalize_expiry(self, pending: ProjectLaunch | None, claim: ProjectLaunchClaim | None) -> tuple[ProjectLaunch | None, ProjectLaunchClaim | None]:
        now = self.now_fn().astimezone(timezone.utc)
        if pending is not None and now >= _parse_utc(pending.expires_at, "expires_at"):
            return None, None
        if claim is not None and (pending is None or now >= _parse_utc(claim.expires_at, "claim.expires_at")):
            claim = None
        return pending, claim

    def _write_state_unlocked(self, pending: ProjectLaunch | None, claim: ProjectLaunchClaim | None) -> None:
        _atomic_json_write(
            self.path,
            {
                "schema": PROJECT_LAUNCH_QUEUE_SCHEMA,
                "pending": None if pending is None else pending.to_dict(),
                "claim": None if claim is None else claim.to_dict(),
            },
        )

    def peek(self) -> ProjectLaunch | None:
        with self._lock():
            pending, claim = self._read_state_unlocked()
            normalized = self._normalize_expiry(pending, claim)
            if normalized != (pending, claim):
                self._write_state_unlocked(*normalized)
            return normalized[0]

    def enqueue(
        self,
        *,
        repo_alias: str,
        prompt: str,
        auto_send: bool = False,
        ttl_minutes: int = 10,
        launch_id: str | None = None,
        project_id: str | None = None,
        plan_version: str | None = None,
        task_id: str | None = None,
        execution_binding_id: str | None = None,
        correlation_id: str | None = None,
        command_id: str | None = None,
        expected_repo_head_before: str | None = None,
    ) -> ProjectLaunch:
        if not isinstance(repo_alias, str) or _ALIAS_RE.fullmatch(repo_alias) is None:
            raise ProjectLaunchQueueError("repo_alias_invalid", "repo_alias is not bounded")
        normalized = prompt.strip()
        if not normalized or len(normalized) > MAX_PROJECT_PROMPT_CHARS:
            raise ProjectLaunchQueueError("prompt_invalid", "prompt is empty or exceeds its bound")
        if not isinstance(auto_send, bool):
            raise ProjectLaunchQueueError("auto_send_invalid", "auto_send must be boolean")
        if isinstance(ttl_minutes, bool) or not isinstance(ttl_minutes, int) or not 1 <= ttl_minutes <= 60:
            raise ProjectLaunchQueueError("ttl_invalid", "ttl_minutes must be between 1 and 60")
        supplied_launch_id = launch_id or str(uuid.uuid4())
        try:
            supplied_launch_id = _uuid_text(supplied_launch_id, "launch_id")
        except ValueError as exc:
            raise ProjectLaunchQueueError("launch_id_invalid", str(exc)) from exc
        metadata = {
            field: value
            for field, value in {
                "project_id": project_id,
                "plan_version": plan_version,
                "task_id": task_id,
                "execution_binding_id": execution_binding_id,
                "correlation_id": correlation_id,
                "command_id": command_id,
                "expected_repo_head_before": expected_repo_head_before,
            }.items()
            if value is not None
        }
        if any(not isinstance(value, str) or not value or len(value) > 256 for value in metadata.values()):
            raise ProjectLaunchQueueError("metadata_invalid", "project launch metadata is invalid")
        with self._lock():
            pending, claim = self._normalize_expiry(*self._read_state_unlocked())
            if pending is not None:
                raise ProjectLaunchQueueError("queue_pending", "project launch queue already contains a pending prompt")
            now = self.now_fn().astimezone(timezone.utc)
            launch = ProjectLaunch(
                launch_id=supplied_launch_id,
                repo_alias=repo_alias,
                prompt=normalized,
                auto_send=auto_send,
                created_at=_utc_text(now),
                expires_at=_utc_text(now + timedelta(minutes=ttl_minutes)),
                **metadata,
            )
            self._write_state_unlocked(launch, None)
            return launch

    def claim(self, *, launch_id: str, claim_id: str, lease_seconds: int = 30) -> ProjectLaunch | None:
        try:
            launch_id = _uuid_text(launch_id, "launch_id")
            claim_id = _uuid_text(claim_id, "claim_id")
        except ValueError as exc:
            raise ProjectLaunchQueueError("claim_id_invalid", str(exc)) from exc
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not 5 <= lease_seconds <= 120:
            raise ProjectLaunchQueueError("lease_invalid", "lease_seconds must be between 5 and 120")
        with self._lock():
            raw_pending, raw_claim = self._read_state_unlocked()
            pending, current = self._normalize_expiry(raw_pending, raw_claim)
            if pending is None or pending.launch_id != launch_id:
                if (pending, current) != (raw_pending, raw_claim):
                    self._write_state_unlocked(pending, current)
                return None
            if current is not None:
                if current.claim_id == claim_id:
                    return pending
                return None
            now = self.now_fn().astimezone(timezone.utc)
            new_claim = ProjectLaunchClaim(
                claim_id=claim_id,
                launch_id=launch_id,
                claimed_at=_utc_text(now),
                expires_at=_utc_text(now + timedelta(seconds=lease_seconds)),
            )
            self._write_state_unlocked(pending, new_claim)
            return pending

    def acknowledge(self, *, launch_id: str, claim_id: str) -> bool:
        try:
            launch_id = _uuid_text(launch_id, "launch_id")
            claim_id = _uuid_text(claim_id, "claim_id")
        except ValueError as exc:
            raise ProjectLaunchQueueError("claim_id_invalid", str(exc)) from exc
        with self._lock():
            pending, claim = self._normalize_expiry(*self._read_state_unlocked())
            if pending is None or claim is None or pending.launch_id != launch_id or claim.launch_id != launch_id or claim.claim_id != claim_id:
                self._write_state_unlocked(pending, claim)
                return False
            self._write_state_unlocked(None, None)
            return True

    def claim_matches(self, *, launch_id: str, claim_id: str) -> bool:
        """Return whether the caller currently owns the pending launch lease."""
        try:
            launch_id = _uuid_text(launch_id, "launch_id")
            claim_id = _uuid_text(claim_id, "claim_id")
        except ValueError as exc:
            raise ProjectLaunchQueueError("claim_id_invalid", str(exc)) from exc
        with self._lock():
            raw_pending, raw_claim = self._read_state_unlocked()
            pending, claim = self._normalize_expiry(raw_pending, raw_claim)
            if (pending, claim) != (raw_pending, raw_claim):
                self._write_state_unlocked(pending, claim)
            return bool(
                pending is not None
                and claim is not None
                and pending.launch_id == launch_id
                and claim.launch_id == launch_id
                and claim.claim_id == claim_id
            )

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor: int | None = None
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while descriptor is None:
            try:
                descriptor = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                try:
                    if time.time() - self.lock_path.stat().st_mtime >= _STALE_LOCK_SECONDS:
                        self.lock_path.unlink()
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() >= deadline:
                    raise ProjectLaunchQueueError("queue_busy", "project launch queue is busy")
                time.sleep(0.01)
        try:
            os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
            os.close(descriptor)
            descriptor = None
            yield
        finally:
            if descriptor is not None:
                os.close(descriptor)
            self.lock_path.unlink(missing_ok=True)


__all__ = [
    "MAX_PROJECT_PROMPT_CHARS",
    "PROJECT_LAUNCH_SCHEMA",
    "PROJECT_LAUNCH_QUEUE_SCHEMA",
    "PROJECT_LAUNCH_CLAIM_SCHEMA",
    "ProjectLaunch",
    "ProjectLaunchClaim",
    "ProjectLaunchQueueAdapter",
    "ProjectLaunchQueueError",
    "default_project_launch_queue_path",
]

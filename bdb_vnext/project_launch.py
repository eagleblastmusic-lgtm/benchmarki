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
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from bdb_vnext.composition import default_vnext_runtime_root


PROJECT_LAUNCH_SCHEMA = "bdb-project-launch-v1"
PROJECT_LAUNCH_QUEUE_SCHEMA = "bdb-project-launch-queue-v1"
PROJECT_LAUNCH_CLAIM_SCHEMA = "bdb-project-launch-claim-v1"
PROJECT_LAUNCH_LOCK_SCHEMA = "bdb-project-launch-lock-v1"
MAX_PROJECT_PROMPT_CHARS = 50_000
_ALIAS_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_LOCK_TIMEOUT_SECONDS = 5.0
_STALE_LOCK_SECONDS = 10.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def default_project_launch_queue_path() -> Path:
    """Return the one queue path shared by the canonical GUI and Native host."""
    return (default_vnext_runtime_root() / "control" / "project-launch-queue.json").absolute()


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


def _is_pid_alive(pid: int) -> bool:
    """Check if process with given PID is currently active and alive."""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return kernel32.GetLastError() == 5
            try:
                exit_code = wintypes.DWORD()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    STILL_ACTIVE = 259
                    return bool(exit_code.value == STILL_ACTIVE)
                return False
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return True
    else:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False


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
        repo_alias = str(value.get("repo_alias") or "")
        prompt = str(value.get("prompt") or "")
        auto_send = value.get("auto_send")
        created_at = value.get("created_at")
        expires_at = value.get("expires_at")
        if _ALIAS_RE.fullmatch(repo_alias) is None:
            raise ValueError("repo_alias must match repository alias format")
        if not prompt or len(prompt) > MAX_PROJECT_PROMPT_CHARS:
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


@dataclass(frozen=True)
class ProjectLaunchLockInfo:
    owner_token: str
    pid: int
    acquired_at: str
    stale_after_seconds: float
    schema: str = PROJECT_LAUNCH_LOCK_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner_token": self.owner_token,
            "pid": self.pid,
            "acquired_at": self.acquired_at,
            "stale_after_seconds": self.stale_after_seconds,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProjectLaunchLockInfo":
        if not isinstance(value, Mapping) or value.get("schema") != PROJECT_LAUNCH_LOCK_SCHEMA:
            raise ValueError("invalid lock schema")
        owner_token = value.get("owner_token")
        if not isinstance(owner_token, str) or not owner_token or len(owner_token) > 256:
            raise ValueError("invalid owner_token in lock metadata")
        pid_raw = value.get("pid")
        if not isinstance(pid_raw, int) or isinstance(pid_raw, bool) or pid_raw <= 0:
            raise ValueError("invalid pid in lock metadata")
        acquired_at = value.get("acquired_at")
        _parse_utc(acquired_at, "acquired_at")
        stale_after = value.get("stale_after_seconds", _STALE_LOCK_SECONDS)
        if not isinstance(stale_after, (int, float)) or isinstance(stale_after, bool) or stale_after <= 0:
            raise ValueError("invalid stale_after_seconds in lock metadata")
        return cls(
            owner_token=owner_token,
            pid=pid_raw,
            acquired_at=str(acquired_at),
            stale_after_seconds=float(stale_after),
            schema=PROJECT_LAUNCH_LOCK_SCHEMA,
        )


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
        self._thread_lock = threading.Lock()

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
        if ttl_minutes <= 0 or ttl_minutes > 60 * 24:
            raise ProjectLaunchQueueError("queue_ttl_invalid", "ttl_minutes must be positive and bounded")
        created_dt = self.now_fn().astimezone(timezone.utc)
        expires_dt = created_dt + timedelta(minutes=ttl_minutes)
        created_at = _utc_text(created_dt)
        expires_at = _utc_text(expires_dt)
        try:
            candidate = ProjectLaunch(
                launch_id=launch_id or str(uuid.uuid4()),
                repo_alias=repo_alias,
                prompt=prompt,
                auto_send=auto_send,
                created_at=created_at,
                expires_at=expires_at,
                project_id=project_id,
                plan_version=plan_version,
                task_id=task_id,
                execution_binding_id=execution_binding_id,
                correlation_id=correlation_id,
                command_id=command_id,
                expected_repo_head_before=expected_repo_head_before,
            )
        except ValueError as exc:
            raise ProjectLaunchQueueError("queue_payload_invalid", str(exc)) from exc

        with self._lock():
            raw_pending, raw_claim = self._read_state_unlocked()
            pending, _ = self._normalize_expiry(raw_pending, raw_claim)
            if pending is not None:
                raise ProjectLaunchQueueError("queue_pending", "queue already contains an unexpired launch")
            self._write_state_unlocked(candidate, None)
            return candidate

    def claim(self, *, launch_id: str, claim_id: str, lease_seconds: int = 30) -> ProjectLaunchClaim | None:
        if lease_seconds <= 0 or lease_seconds > 600:
            raise ProjectLaunchQueueError("queue_lease_invalid", "lease_seconds must be positive and bounded")
        try:
            validated_launch = _uuid_text(launch_id, "launch_id")
            validated_claim = _uuid_text(claim_id, "claim_id")
        except ValueError as exc:
            raise ProjectLaunchQueueError("queue_claim_invalid", str(exc)) from exc

        now_dt = self.now_fn().astimezone(timezone.utc)
        expires_dt = now_dt + timedelta(seconds=lease_seconds)
        claimed_at = _utc_text(now_dt)
        expires_at = _utc_text(expires_dt)

        with self._lock():
            raw_pending, raw_claim = self._read_state_unlocked()
            pending, claim = self._normalize_expiry(raw_pending, raw_claim)
            if pending is None or pending.launch_id != validated_launch:
                if (pending, claim) != (raw_pending, raw_claim):
                    self._write_state_unlocked(pending, claim)
                return None
            if claim is not None:
                if claim.claim_id == validated_claim and claim.launch_id == validated_launch:
                    return pending
                return None
            new_claim = ProjectLaunchClaim(
                claim_id=validated_claim,
                launch_id=validated_launch,
                claimed_at=claimed_at,
                expires_at=expires_at,
            )
            self._write_state_unlocked(pending, new_claim)
            return pending

    def acknowledge(self, *, launch_id: str, claim_id: str) -> bool:
        try:
            validated_launch = _uuid_text(launch_id, "launch_id")
            validated_claim = _uuid_text(claim_id, "claim_id")
        except ValueError as exc:
            raise ProjectLaunchQueueError("queue_ack_invalid", str(exc)) from exc

        with self._lock():
            raw_pending, raw_claim = self._read_state_unlocked()
            pending, claim = self._normalize_expiry(raw_pending, raw_claim)
            if (
                pending is not None
                and claim is not None
                and pending.launch_id == validated_launch
                and claim.launch_id == validated_launch
                and claim.claim_id == validated_claim
            ):
                self._write_state_unlocked(None, None)
                return True
            if (pending, claim) != (raw_pending, raw_claim):
                self._write_state_unlocked(pending, claim)
            return False

    def claim_matches(self, *, launch_id: str, claim_id: str) -> bool:
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

    def _read_lock_info_safe(self) -> tuple[ProjectLaunchLockInfo | None, float | None]:
        """Safely read lock file and return (parsed_info, mtime)."""
        try:
            mtime = self.lock_path.stat().st_mtime
            raw = self.lock_path.read_text(encoding="utf-8-sig")
            try:
                data = json.loads(raw)
                return ProjectLaunchLockInfo.from_dict(data), mtime
            except Exception:
                return None, mtime
        except (FileNotFoundError, PermissionError, OSError, UnicodeDecodeError, ValueError):
            return None, None

    def _is_lock_stale(self, info: ProjectLaunchLockInfo | None, mtime: float | None) -> bool:
        """Determine whether a lock can be safely reclaimed.

        Invariants:
        - AGE_ONLY_RECLAIM = FALSE
        - CORRUPT_LOCK_AGE_ONLY_RECLAIM = FALSE
        - UNREADABLE_LOCK_AGE_ONLY_RECLAIM = FALSE
        - UNKNOWN_OWNER_LOCK_DELETED = FALSE

        Fail closed: A lock is ONLY reclaimable if:
        1. Lock metadata is fully valid and parseable (info is not None).
        2. Owner process is confirmed dead (_is_pid_alive(info.pid) == False).
        3. Lease duration has expired (now - acquired_at >= info.stale_after_seconds).

        If metadata is corrupt, unreadable, missing, or ownership cannot be confirmed,
        the lock is NEVER considered stale and must NOT be deleted.
        """
        if info is None:
            return False

        if _is_pid_alive(info.pid):
            return False

        try:
            now_ts = time.time()
            acquired_dt = _parse_utc(info.acquired_at, "acquired_at")
            if now_ts - acquired_dt.timestamp() >= info.stale_after_seconds:
                return True
        except Exception:
            return False

        return False

    @contextmanager
    def _lock(self) -> Iterator[str]:
        """Ownership-aware, compare-before-release lock.

        Yields owner_token. Safely handles Windows PermissionError during concurrency.
        """
        with self._thread_lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            owner_token = uuid.uuid4().hex
            pid = os.getpid()
            acquired = False
            deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
            backoff = 0.002
            acquired_ts = 0.0

            while not acquired:
                descriptor: int | None = None
                try:
                    descriptor = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    now_str = _utc_text(self.now_fn())
                    lock_info = ProjectLaunchLockInfo(
                        owner_token=owner_token,
                        pid=pid,
                        acquired_at=now_str,
                        stale_after_seconds=_STALE_LOCK_SECONDS,
                    )
                    payload = json.dumps(lock_info.to_dict(), ensure_ascii=False, indent=2).encode("utf-8")
                    os.write(descriptor, payload)
                    os.close(descriptor)
                    descriptor = None
                    acquired = True
                    acquired_ts = time.time()
                    break
                except (FileExistsError, PermissionError):
                    info, mtime = self._read_lock_info_safe()
                    if self._is_lock_stale(info, mtime):
                        try:
                            current_info, _ = self._read_lock_info_safe()
                            if current_info is not None and info is not None:
                                if current_info.owner_token == info.owner_token:
                                    self.lock_path.unlink()
                        except (FileNotFoundError, PermissionError, OSError):
                            pass
                        continue

                    if time.monotonic() >= deadline:
                        raise ProjectLaunchQueueError("queue_busy", "project launch queue is busy") from None
                    time.sleep(backoff)
                    backoff = min(0.02, backoff * 1.5)
                finally:
                    if descriptor is not None:
                        try:
                            os.close(descriptor)
                        except OSError:
                            pass

            try:
                yield owner_token
            finally:
                try:
                    info, _ = self._read_lock_info_safe()
                    if info is not None and info.owner_token == owner_token:
                        self.lock_path.unlink(missing_ok=True)
                except (FileNotFoundError, PermissionError, OSError):
                    pass


__all__ = [
    "MAX_PROJECT_PROMPT_CHARS",
    "PROJECT_LAUNCH_SCHEMA",
    "PROJECT_LAUNCH_QUEUE_SCHEMA",
    "PROJECT_LAUNCH_CLAIM_SCHEMA",
    "PROJECT_LAUNCH_LOCK_SCHEMA",
    "ProjectLaunch",
    "ProjectLaunchClaim",
    "ProjectLaunchLockInfo",
    "ProjectLaunchQueueAdapter",
    "ProjectLaunchQueueError",
    "default_project_launch_queue_path",
]

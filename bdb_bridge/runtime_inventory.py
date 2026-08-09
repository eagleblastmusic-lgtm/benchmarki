from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Sequence
from uuid import uuid4

from bdb_shared.evidence import (
    SANITIZATION_VERSION,
    canonical_json_bytes,
    sanitize_report,
    semantic_digest,
    semantic_payload,
)

from .code_relationship_migration import MIGRATION_V8
from .direct_checkout_workspace_migration import MIGRATION_V11, MIGRATION_V12
from .migrations import JOURNAL_TABLES as BASE_JOURNAL_TABLES
from .migrations import MIGRATIONS as BASE_MIGRATIONS
from .multi_file_patch_migration import MIGRATION_V9
from .multi_file_patch_runtime_migration import MIGRATION_V10
from .repository_index_migration import MIGRATION_V7
from .runtime_version import BDB_RUNTIME_VERSION
from .workspace_lifecycle_migration import MIGRATION_V6


REPORT_SCHEMA = "runtime-inventory-v1"
PROVIDER_VERSION = "1.0"
POLICY_VERSION = "r0a-minimal-v1"

BRIDGE_CONFIG_SCHEMA = "1.1"
NATIVE_CONFIG_SCHEMA = "bdb-native-host-config-v1"
RECEIPT_SCHEMA = "bdb-native-request-receipts-v1"
SPOOL_SCHEMA = "bdb-local-envelope-v1"
PROMOTER_STATE_SCHEMA = "bdb-workspace-promoter-state-v1"
PROMOTION_RECEIPT_SCHEMA = "bdb-workspace-promotion-v1"
REPOSITORY_SEQUENCE_SCHEMA = "bdb-repository-event-seq-v1"

SUPPORTED_MIGRATIONS = (
    *BASE_MIGRATIONS[:5],
    MIGRATION_V6,
    MIGRATION_V7,
    MIGRATION_V8,
    MIGRATION_V9,
    MIGRATION_V10,
    MIGRATION_V11,
    MIGRATION_V12,
)
SUPPORTED_MIGRATION_MAP = {migration.version: migration for migration in SUPPORTED_MIGRATIONS}
SUPPORTED_JOURNAL_TABLES = frozenset(
    {
        *BASE_JOURNAL_TABLES,
        "workspace_lifecycle",
        "repository_snapshots",
        "repository_files",
        "repository_symbols",
        "repository_analyses",
        "repository_imports",
        "repository_symbol_references",
        "repository_dependency_edges",
        "multi_file_patch_checkpoints",
        "multi_file_patch_checkpoint_paths",
        "multi_file_patch_profile_runs",
        "validation_runs",
    }
)

_TERMINAL_COMMAND_STATES = frozenset(
    {
        "acknowledged",
        "rejected",
        "expired",
        "policy_denied",
        "stale_revision",
        "state_mismatch",
        "cancelled",
    }
)
_TERMINAL_SESSION_STATES = frozenset({"completed", "aborted"})
_SAFE_SPOOL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.json$")
_REPOSITORY_ALIAS = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_SHA40 = re.compile(r"^[0-9a-fA-F]{40}$")
_EXTENSION_ORIGIN = re.compile(r"^chrome-extension://[a-p]{32}/$")
_SECRET_URL = re.compile(r"(https?://)[^\s/@]+(?::[^\s/@]*)?@", re.IGNORECASE)


class SourceStatus(StrEnum):
    OBSERVED = "OBSERVED"
    UNAVAILABLE = "UNAVAILABLE"
    UNSTABLE = "UNSTABLE"
    INVALID = "INVALID"
    UNSUPPORTED = "UNSUPPORTED"


class OverallResult(StrEnum):
    READY_FOR_LOCAL_GATE = "READY_FOR_LOCAL_GATE"
    INCOMPLETE = "INCOMPLETE"
    INVALID = "INVALID"
    UNSUPPORTED = "UNSUPPORTED"


class InventoryFailure(Exception):
    def __init__(self, status: SourceStatus, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class OutputFailure(Exception):
    pass


FaultHook = Callable[[str, str], None]


@dataclass(frozen=True)
class InventoryRequest:
    repository_path: Path
    bridge_config_path: Path
    native_config_path: Path | None = None
    browser_bundle_path: Path | None = None
    native_manifest_path: Path | None = None
    scratch_dir: Path | None = None
    max_records: int = 100
    max_file_bytes: int = 2 * 1024 * 1024
    max_total_bytes: int = 64 * 1024 * 1024
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        for field_name in (
            "repository_path",
            "bridge_config_path",
            "native_config_path",
            "browser_bundle_path",
            "native_manifest_path",
            "scratch_dir",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, Path(value).expanduser().absolute())
        if isinstance(self.max_records, bool) or not 1 <= self.max_records <= 5_000:
            raise ValueError("max_records must be between 1 and 5000")
        if isinstance(self.max_file_bytes, bool) or not 1_024 <= self.max_file_bytes <= 64 * 1024 * 1024:
            raise ValueError("max_file_bytes must be between 1024 and 67108864")
        if isinstance(self.max_total_bytes, bool) or not self.max_file_bytes <= self.max_total_bytes <= 512 * 1024 * 1024:
            raise ValueError("max_total_bytes must be between max_file_bytes and 536870912")
        if not 0.1 <= float(self.timeout_seconds) <= 120.0:
            raise ValueError("timeout_seconds must be between 0.1 and 120")


@dataclass(frozen=True)
class BridgeDescriptor:
    config_path: Path
    repository_id: str
    control_repo_path: Path
    fixture_repo_path: Path
    worktree_root: Path
    runtime_dir: Path
    journal_path: Path
    direct_spool_dir: Path
    direct_result_dir: Path
    workspace_mode: str


@dataclass(frozen=True)
class NativeDescriptor:
    config_path: Path
    request_store_path: Path
    state_path: Path
    session_store_path: Path
    repository_config_paths: tuple[Path, ...]
    allowed_origins: tuple[str, ...]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _sha256_bytes(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _safe_message(value: object, *, limit: int = 500) -> str:
    text = str(value).replace("\x00", "")
    text = _SECRET_URL.sub(r"\1<redacted>@", text)
    return " ".join(text.split())[:limit]


def _error(code: str, message: object) -> dict[str, str]:
    return {"code": code, "message": _safe_message(message)}


def _source(
    name: str,
    kind: str,
    *,
    required: bool,
    status: SourceStatus,
    complete: bool,
    identity: Mapping[str, Any] | None = None,
    facts: Mapping[str, Any] | None = None,
    errors: Iterable[Mapping[str, str]] = (),
    truncated: bool = False,
    observation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "kind": kind,
        "required": required,
        "status": status.value,
        "complete": bool(complete and status is SourceStatus.OBSERVED and not truncated),
        "truncated": bool(truncated),
        "identity": dict(identity or {}),
        "facts": dict(facts or {}),
        "errors": [dict(item) for item in errors],
        "observation": dict(observation or {}),
    }


def _failed_source(
    name: str,
    kind: str,
    *,
    required: bool,
    failure: InventoryFailure,
    observation: Mapping[str, Any],
) -> dict[str, Any]:
    return _source(
        name,
        kind,
        required=required,
        status=failure.status,
        complete=False,
        errors=(_error(failure.code, failure),),
        observation=observation,
    )


def _file_token(path: Path) -> dict[str, Any]:
    info = path.stat(follow_symlinks=False)
    return {
        "size": int(info.st_size),
        "mtime_ns": int(info.st_mtime_ns),
        "ctime_ns": int(info.st_ctime_ns),
        "file_id": int(info.st_ino),
        "mode": int(info.st_mode),
    }


def _is_reparse(path: Path) -> bool:
    try:
        if path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)()):
            return True
        attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except OSError:
        return False


def _has_reparse_component(path: Path) -> bool:
    lexical = Path(os.path.abspath(os.fspath(path)))
    cursor = Path(lexical.anchor)
    if cursor.exists() and _is_reparse(cursor):
        return True
    for part in lexical.parts[1:]:
        cursor = cursor / part
        if cursor.exists() and _is_reparse(cursor):
            return True
    return False


def _comparable(path: Path) -> str:
    value = os.path.abspath(os.fspath(path))
    if sys.platform == "win32":
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
    return os.path.normcase(os.path.normpath(value))


def _contained(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((_comparable(path), _comparable(root))) == _comparable(root)
    except ValueError:
        return False


def _assert_contained(path: Path, root: Path, *, label: str, allow_missing: bool = False) -> None:
    lexical_path = Path(os.path.abspath(os.fspath(path)))
    lexical_root = Path(os.path.abspath(os.fspath(root)))
    if not _contained(lexical_path, lexical_root):
        raise InventoryFailure(SourceStatus.INVALID, "path_escape", f"{label} escapes its declared root")
    if _has_reparse_component(lexical_root):
        raise InventoryFailure(SourceStatus.INVALID, "reparse_point", f"{label} root path contains a symlink/junction/reparse point")
    try:
        resolved_root = lexical_root.resolve(strict=False)
        resolved_path = lexical_path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise InventoryFailure(SourceStatus.INVALID, "path_resolution_failed", f"{label}: {exc}") from exc
    if not _contained(resolved_path, resolved_root):
        raise InventoryFailure(SourceStatus.INVALID, "path_escape", f"{label} resolves outside its declared root")

    cursor = lexical_root
    if cursor.exists() and _is_reparse(cursor):
        raise InventoryFailure(SourceStatus.INVALID, "reparse_point", f"{label} root is a symlink/junction/reparse point")
    relative = lexical_path.relative_to(lexical_root)
    for part in relative.parts:
        cursor = cursor / part
        if cursor.exists() and _is_reparse(cursor):
            raise InventoryFailure(SourceStatus.INVALID, "reparse_point", f"{label} contains a symlink/junction/reparse point")
    if not allow_missing and not lexical_path.exists():
        raise InventoryFailure(SourceStatus.UNAVAILABLE, "missing", f"{label} is unavailable")


def _read_stable_bytes(
    path: Path,
    *,
    root: Path,
    label: str,
    max_bytes: int,
    fault_hook: FaultHook | None = None,
) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
    _assert_contained(path, root, label=label)
    if not path.is_file():
        raise InventoryFailure(SourceStatus.INVALID, "not_regular_file", f"{label} is not a regular file")
    before = _file_token(path)
    if before["size"] > max_bytes:
        raise InventoryFailure(SourceStatus.INVALID, "size_limit", f"{label} exceeds the bounded read limit")
    try:
        with path.open("rb") as handle:
            content = handle.read(max_bytes + 1)
    except PermissionError as exc:
        raise InventoryFailure(SourceStatus.UNAVAILABLE, "permission_denied", f"{label}: {exc}") from exc
    except OSError as exc:
        raise InventoryFailure(SourceStatus.UNAVAILABLE, "read_failed", f"{label}: {exc}") from exc
    if len(content) > max_bytes:
        raise InventoryFailure(SourceStatus.INVALID, "size_limit", f"{label} exceeds the bounded read limit")
    if fault_hook is not None:
        fault_hook(label, "after_read")
    try:
        after = _file_token(path)
    except OSError as exc:
        raise InventoryFailure(SourceStatus.UNSTABLE, "source_disappeared", f"{label}: {exc}") from exc
    if before != after or len(content) != after["size"]:
        raise InventoryFailure(SourceStatus.UNSTABLE, "identity_changed", f"{label} changed during observation")
    return content, before, after


def _read_stable_json(
    path: Path,
    *,
    root: Path,
    label: str,
    max_bytes: int,
    fault_hook: FaultHook | None = None,
) -> tuple[dict[str, Any], str, dict[str, Any], dict[str, Any]]:
    content, before, after = _read_stable_bytes(
        path,
        root=root,
        label=label,
        max_bytes=max_bytes,
        fault_hook=fault_hook,
    )
    try:
        value = json.loads(content.decode("utf-8-sig", errors="strict"))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise InventoryFailure(SourceStatus.INVALID, "invalid_json", f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise InventoryFailure(SourceStatus.INVALID, "invalid_shape", f"{label} must contain a JSON object")
    return value, _sha256_bytes(content), before, after


def _resolve_config_path(value: Any, *, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise InventoryFailure(SourceStatus.INVALID, "invalid_config", f"Bridge config field {field} is missing")
    try:
        return Path(value).expanduser().absolute().resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise InventoryFailure(SourceStatus.INVALID, "invalid_config", f"Bridge config field {field} is invalid") from exc


def _parse_bridge_descriptor(path: Path, document: Mapping[str, Any]) -> BridgeDescriptor:
    schema = document.get("schema_version")
    if schema != BRIDGE_CONFIG_SCHEMA:
        raise InventoryFailure(SourceStatus.UNSUPPORTED, "unsupported_schema", "Bridge config schema is unsupported")
    control = _resolve_config_path(document.get("control_repo_path"), field="control_repo_path")
    fixture = _resolve_config_path(document.get("fixture_repo_path"), field="fixture_repo_path")
    worktrees = _resolve_config_path(document.get("worktree_root"), field="worktree_root")
    runtime = (
        _resolve_config_path(document.get("runtime_dir"), field="runtime_dir")
        if document.get("runtime_dir") is not None
        else (worktrees.parent / "bdb_runtime").resolve(strict=False)
    )
    journal = (
        _resolve_config_path(document.get("journal_path"), field="journal_path")
        if document.get("journal_path") is not None
        else runtime / "journal.db"
    )
    spool = (
        _resolve_config_path(document.get("direct_spool_dir"), field="direct_spool_dir")
        if document.get("direct_spool_dir") is not None
        else runtime / "direct_spool" / "inbox"
    )
    results = (
        _resolve_config_path(document.get("direct_result_dir"), field="direct_result_dir")
        if document.get("direct_result_dir") is not None
        else runtime / "direct_spool" / "results"
    )
    repository_id = document.get("repository_id", "bdb-poc-fixture")
    if not isinstance(repository_id, str) or not repository_id.strip():
        raise InventoryFailure(SourceStatus.INVALID, "invalid_config", "Bridge repository_id is invalid")
    workspace_mode = document.get("workspace_mode", "isolated_worktree")
    if workspace_mode not in {"isolated_worktree", "direct_checkout"}:
        raise InventoryFailure(SourceStatus.INVALID, "invalid_config", "Bridge workspace_mode is invalid")
    for candidate, label in ((journal, "Journal"), (spool, "spool"), (results, "result store")):
        _assert_contained(candidate, runtime, label=label, allow_missing=True)
    if spool == results or _contained(spool, results) or _contained(results, spool):
        raise InventoryFailure(SourceStatus.INVALID, "path_overlap", "Spool and result directories overlap")
    for candidate in (control, fixture, worktrees):
        if _contained(runtime, candidate) or _contained(candidate, runtime):
            raise InventoryFailure(SourceStatus.INVALID, "path_overlap", "Runtime and repository/worktree roots overlap")
    return BridgeDescriptor(
        config_path=path,
        repository_id=repository_id,
        control_repo_path=control,
        fixture_repo_path=fixture,
        worktree_root=worktrees,
        runtime_dir=runtime,
        journal_path=journal,
        direct_spool_dir=spool,
        direct_result_dir=results,
        workspace_mode=workspace_mode,
    )


def _parse_native_descriptor(path: Path, document: Mapping[str, Any]) -> NativeDescriptor:
    if document.get("schema") != NATIVE_CONFIG_SCHEMA:
        raise InventoryFailure(SourceStatus.UNSUPPORTED, "unsupported_schema", "Native Host config schema is unsupported")
    repositories = document.get("repositories")
    if repositories is None:
        legacy = document.get("bridge_config_path")
        repositories = {"default": {"bridge_config_path": legacy}}
    if not isinstance(repositories, dict) or not repositories or len(repositories) > 32:
        raise InventoryFailure(SourceStatus.INVALID, "invalid_config", "Native Host repositories are invalid")
    origins = document.get("allowed_origins")
    if (
        not isinstance(origins, list)
        or not origins
        or not all(isinstance(item, str) and _EXTENSION_ORIGIN.fullmatch(item) for item in origins)
        or len(set(origins)) != len(origins)
    ):
        raise InventoryFailure(SourceStatus.INVALID, "invalid_config", "Native Host allowed_origins are invalid")
    config_paths: list[Path] = []
    for alias, item in sorted(repositories.items(), key=lambda pair: str(pair[0])):
        if not isinstance(alias, str) or _REPOSITORY_ALIAS.fullmatch(alias) is None:
            raise InventoryFailure(SourceStatus.INVALID, "invalid_config", "Native Host repository alias is invalid")
        bridge_value = item if isinstance(item, str) else item.get("bridge_config_path") if isinstance(item, dict) else None
        if not isinstance(bridge_value, str) or not bridge_value:
            raise InventoryFailure(SourceStatus.INVALID, "invalid_config", "Native Host repository config path is invalid")
        try:
            config_paths.append(Path(bridge_value).expanduser().absolute().resolve(strict=False))
        except (OSError, RuntimeError, ValueError) as exc:
            raise InventoryFailure(SourceStatus.INVALID, "invalid_config", "Native Host repository config path is invalid") from exc
    parent = path.parent.resolve(strict=False)

    def beside(field: str, default_name: str) -> Path:
        raw = document.get(field)
        try:
            candidate = (parent / default_name) if raw is None else Path(str(raw)).expanduser().absolute().resolve(strict=False)
        except (OSError, RuntimeError, ValueError) as exc:
            raise InventoryFailure(SourceStatus.INVALID, "invalid_config", f"{field} is invalid") from exc
        _assert_contained(candidate, parent, label=field, allow_missing=True)
        if candidate.parent.resolve(strict=False) != parent:
            raise InventoryFailure(SourceStatus.INVALID, "path_escape", f"{field} must stay beside Native Host config")
        return candidate

    state = beside("state_path", "native-host-arm.json")
    sessions = beside("session_store_path", "native-host-sessions.json")
    requests = beside("request_store_path", "native-host-requests.json")
    if len({state, sessions, requests}) != 3:
        raise InventoryFailure(SourceStatus.INVALID, "path_overlap", "Native Host state paths overlap")
    return NativeDescriptor(path, requests, state, sessions, tuple(config_paths), tuple(origins))


def _pid_identity(pid: int) -> dict[str, Any]:
    """Observe liveness plus a process-creation token without controlling the process."""
    if isinstance(pid, bool) or pid <= 0:
        return {"alive": False, "creation_token": None}
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
        except PermissionError:
            alive: bool | None = None
        except OSError:
            return {"alive": False, "creation_token": None}
        else:
            alive = True
        creation_token: str | None = None
        proc_stat = Path("/proc") / str(pid) / "stat"
        if proc_stat.exists():
            try:
                fields = proc_stat.read_text(encoding="ascii", errors="strict").rsplit(")", 1)[1].split()
                creation_token = fields[19]
            except FileNotFoundError:
                return {"alive": False, "creation_token": None}
            except (IndexError, OSError, UnicodeError):
                creation_token = None
        return {"alive": alive, "creation_token": creation_token}
    process_query_limited_information = 0x1000

    class _FileTime(ctypes.Structure):
        _fields_ = (("low", ctypes.c_ulong), ("high", ctypes.c_ulong))

    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.GetProcessTimes.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
        )
        kernel32.GetProcessTimes.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    except (AttributeError, OSError):
        return {"alive": None, "creation_token": None}
    if not handle:
        alive = None if kernel32.GetLastError() == 5 else False
        return {"alive": alive, "creation_token": None}
    try:
        exit_code = ctypes.c_ulong()
        alive = bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)) and exit_code.value == 259)
        creation = _FileTime()
        exit_time = _FileTime()
        kernel_time = _FileTime()
        user_time = _FileTime()
        creation_token = None
        if alive and kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            creation_token = str((int(creation.high) << 32) | int(creation.low))
        return {"alive": alive, "creation_token": creation_token}
    finally:
        kernel32.CloseHandle(handle)


def _pid_alive(pid: int) -> bool | None:
    return _pid_identity(pid)["alive"]


def _run_git(repository: Path, arguments: Sequence[str], *, timeout: float, max_output: int) -> str:
    environment = dict(os.environ)
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    command = [
        "git",
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "maintenance.auto=false",
        "-C",
        str(repository),
        *arguments,
    ]
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise InventoryFailure(SourceStatus.UNAVAILABLE, "timeout", "Git observation timed out") from exc
    output = completed.stdout + completed.stderr
    if len(output) > max_output:
        raise InventoryFailure(SourceStatus.INVALID, "output_limit", "Git observation exceeded the output limit")
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace")
        raise InventoryFailure(SourceStatus.INVALID, "git_error", f"Git observation failed: {detail}")
    try:
        return completed.stdout.decode("utf-8", errors="strict").strip()
    except UnicodeError as exc:
        raise InventoryFailure(SourceStatus.INVALID, "invalid_encoding", "Git output is not valid UTF-8") from exc


def _optional_git(repository: Path, arguments: Sequence[str], *, timeout: float, max_output: int) -> str | None:
    try:
        return _run_git(repository, arguments, timeout=timeout, max_output=max_output)
    except InventoryFailure as failure:
        if failure.code == "git_error":
            return None
        raise


def _safe_remote(value: str | None) -> str | None:
    if not value:
        return None
    return _SECRET_URL.sub(r"\1<redacted>@", value)


def _scan_directory(
    directory: Path,
    *,
    max_entries: int,
    label: str,
) -> tuple[list[Any], bool]:
    entries: list[Any] = []
    try:
        with os.scandir(directory) as iterator:
            for entry in iterator:
                if len(entries) >= max_entries:
                    return sorted(entries, key=lambda item: item.name.casefold()), True
                entries.append(entry)
    except PermissionError as exc:
        raise InventoryFailure(SourceStatus.UNAVAILABLE, "permission_denied", f"{label}: {exc}") from exc
    except OSError as exc:
        raise InventoryFailure(SourceStatus.UNAVAILABLE, "directory_read_failed", f"{label}: {exc}") from exc
    return sorted(entries, key=lambda item: item.name.casefold()), False


def _directory_names(entries: Sequence[Any]) -> tuple[str, ...]:
    return tuple(item.name for item in entries)


def _assert_directory_stable(directory: Path, names_before: tuple[str, ...], *, label: str) -> None:
    try:
        current, current_truncated = _scan_directory(
            directory,
            max_entries=len(names_before) + 1,
            label=label,
        )
    except InventoryFailure as exc:
        raise InventoryFailure(SourceStatus.UNSTABLE, "source_disappeared", f"{label} became unavailable") from exc
    if current_truncated or _directory_names(current) != names_before:
        raise InventoryFailure(SourceStatus.UNSTABLE, "identity_changed", f"{label} changed during observation")


def _collect_repository(request: InventoryRequest, fault_hook: FaultHook | None) -> dict[str, Any]:
    root = request.repository_path
    _assert_contained(root, root, label="repository")
    if not root.is_dir():
        raise InventoryFailure(SourceStatus.INVALID, "not_directory", "Repository is not a directory")
    limit = min(request.max_total_bytes, 16 * 1024 * 1024)
    head_before = _run_git(root, ("rev-parse", "HEAD"), timeout=request.timeout_seconds, max_output=limit)
    top_level = Path(
        _run_git(root, ("rev-parse", "--show-toplevel"), timeout=request.timeout_seconds, max_output=limit)
    ).absolute().resolve(strict=False)
    if _comparable(top_level) != _comparable(root.resolve(strict=False)):
        raise InventoryFailure(SourceStatus.INVALID, "repository_mismatch", "Git top-level differs from the declared repository")
    status_before = _run_git(
        root,
        ("status", "--porcelain=v2", "--branch", "--untracked-files=all"),
        timeout=request.timeout_seconds,
        max_output=limit,
    )
    branch = _optional_git(root, ("symbolic-ref", "--short", "-q", "HEAD"), timeout=request.timeout_seconds, max_output=limit)
    upstream = _optional_git(root, ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"), timeout=request.timeout_seconds, max_output=limit)
    upstream_oid = (
        _optional_git(root, ("rev-parse", "@{upstream}"), timeout=request.timeout_seconds, max_output=limit)
        if upstream
        else None
    )
    remote = _optional_git(root, ("remote", "get-url", "origin"), timeout=request.timeout_seconds, max_output=limit)
    worktrees_text = _run_git(root, ("worktree", "list", "--porcelain"), timeout=request.timeout_seconds, max_output=limit)
    worktree_count = sum(1 for line in worktrees_text.splitlines() if line.startswith("worktree "))
    dirty_entries = [
        line for line in status_before.splitlines() if line and not line.startswith("# ")
    ]
    dirty_entry_count = len(dirty_entries)
    truncated = dirty_entry_count > request.max_records
    dirty_entries = dirty_entries[: request.max_records]
    if fault_hook is not None:
        fault_hook("repository", "after_read")
    head_after = _run_git(root, ("rev-parse", "HEAD"), timeout=request.timeout_seconds, max_output=limit)
    status_after = _run_git(
        root,
        ("status", "--porcelain=v2", "--branch", "--untracked-files=all"),
        timeout=request.timeout_seconds,
        max_output=limit,
    )
    if head_before != head_after or status_before != status_after:
        raise InventoryFailure(SourceStatus.UNSTABLE, "identity_changed", "Repository changed during observation")
    identity = {
        "path": str(root.resolve(strict=False)),
        "head": head_before.lower(),
        "branch": branch,
        "detached": branch is None,
        "upstream": upstream,
        "upstream_oid": upstream_oid.lower() if upstream_oid else None,
        "remote": _safe_remote(remote),
        "remote_digest": _sha256_bytes((remote or "").encode("utf-8")) if remote else None,
    }
    return _source(
        "repository",
        "git_repository",
        required=True,
        status=SourceStatus.OBSERVED,
        complete=not truncated,
        identity=identity,
        facts={
            "dirty": bool(dirty_entries),
            "dirty_entry_count": dirty_entry_count,
            "dirty_entries": dirty_entries,
            "worktree_count": worktree_count,
            "git_optional_locks": False,
        },
        truncated=truncated,
    )


def _collect_repository_runtime(
    request: InventoryRequest,
    fault_hook: FaultHook | None,
) -> dict[str, Any]:
    repository = request.repository_path
    module_manifest_path = repository / "manifests" / "bartosz-dev-bridge.module.json"
    module_manifest, module_digest, module_before, module_after = _read_stable_json(
        module_manifest_path,
        root=repository,
        label="module_manifest",
        max_bytes=request.max_file_bytes,
        fault_hook=fault_hook,
    )
    if module_manifest.get("schema") != "bartosz-os-module-manifest-v1":
        raise InventoryFailure(SourceStatus.UNSUPPORTED, "unsupported_schema", "BDB module manifest schema is unsupported")
    package_version = module_manifest.get("version")
    if not isinstance(package_version, str) or not package_version:
        raise InventoryFailure(SourceStatus.INVALID, "version_missing", "BDB module version is invalid")
    runtime_path = repository / "bdb_bridge" / "runtime_version.py"
    runtime_content, runtime_before, runtime_after = _read_stable_bytes(
        runtime_path,
        root=repository,
        label="runtime_version_source",
        max_bytes=request.max_file_bytes,
        fault_hook=fault_hook,
    )
    try:
        if _file_token(module_manifest_path) != module_after:
            raise InventoryFailure(SourceStatus.UNSTABLE, "identity_changed", "BDB module manifest changed during observation")
    except OSError as exc:
        raise InventoryFailure(SourceStatus.UNSTABLE, "source_disappeared", f"BDB module manifest: {exc}") from exc
    return _source(
        "repository_runtime",
        "service_native_runtime_source",
        required=True,
        status=SourceStatus.OBSERVED,
        complete=True,
        identity={
            "repository_path": str(repository),
            "package_version": package_version,
            "service_runtime_version": BDB_RUNTIME_VERSION,
            "native_host_version": BDB_RUNTIME_VERSION,
            "module_manifest_digest": module_digest,
            "runtime_source_digest": _sha256_bytes(runtime_content),
        },
        facts={
            "module_manifest_pre_identity": module_before,
            "module_manifest_post_identity": module_after,
            "runtime_source_pre_identity": runtime_before,
            "runtime_source_post_identity": runtime_after,
        },
    )


def _collect_bridge_config(
    request: InventoryRequest,
    fault_hook: FaultHook | None,
) -> tuple[dict[str, Any], BridgeDescriptor]:
    path = request.bridge_config_path
    document, digest, before, after = _read_stable_json(
        path,
        root=path.parent,
        label="bridge_config",
        max_bytes=request.max_file_bytes,
        fault_hook=fault_hook,
    )
    descriptor = _parse_bridge_descriptor(path, document)
    record = _source(
        "bridge_config",
        "declared_configuration",
        required=True,
        status=SourceStatus.OBSERVED,
        complete=True,
        identity={"path": str(path.resolve(strict=False)), "sha256": digest, "schema": BRIDGE_CONFIG_SCHEMA},
        facts={
            "repository_id": descriptor.repository_id,
            "workspace_mode": descriptor.workspace_mode,
            "declared_paths": {
                "control_repository": str(descriptor.control_repo_path),
                "fixture_repository": str(descriptor.fixture_repo_path),
                "worktree_root": str(descriptor.worktree_root),
                "runtime_dir": str(descriptor.runtime_dir),
                "journal": str(descriptor.journal_path),
                "spool": str(descriptor.direct_spool_dir),
                "results": str(descriptor.direct_result_dir),
            },
            "pre_identity": before,
            "post_identity": after,
        },
    )
    return record, descriptor


def _collect_native_config(
    request: InventoryRequest,
    fault_hook: FaultHook | None,
) -> tuple[dict[str, Any], NativeDescriptor] | tuple[dict[str, Any], None]:
    path = request.native_config_path
    if path is None:
        failure = InventoryFailure(SourceStatus.UNAVAILABLE, "not_declared", "Native Host config was not declared")
        return _failed_source(
            "native_config",
            "declared_configuration",
            required=False,
            failure=failure,
            observation={},
        ), None
    document, digest, before, after = _read_stable_json(
        path,
        root=path.parent,
        label="native_config",
        max_bytes=request.max_file_bytes,
        fault_hook=fault_hook,
    )
    descriptor = _parse_native_descriptor(path, document)
    record = _source(
        "native_config",
        "declared_configuration",
        required=True,
        status=SourceStatus.OBSERVED,
        complete=True,
        identity={"path": str(path.resolve(strict=False)), "sha256": digest, "schema": NATIVE_CONFIG_SCHEMA},
        facts={
            "repository_count": len(descriptor.repository_config_paths),
            "repository_config_paths": [str(item) for item in descriptor.repository_config_paths],
            "allowed_origins": list(descriptor.allowed_origins),
            "request_store_path": str(descriptor.request_store_path),
            "state_path": str(descriptor.state_path),
            "session_store_path": str(descriptor.session_store_path),
            "pre_identity": before,
            "post_identity": after,
        },
    )
    return record, descriptor


def _bounded_tree_digest(
    root: Path,
    *,
    max_files: int,
    max_file_bytes: int,
    max_total_bytes: int,
    fault_hook: FaultHook | None,
    label: str,
) -> tuple[str, list[dict[str, Any]], bool]:
    _assert_contained(root, root, label=label)
    if not root.is_dir():
        raise InventoryFailure(SourceStatus.INVALID, "not_directory", f"{label} is not a directory")
    entries: list[dict[str, Any]] = []
    pending = [root]
    directory_snapshots: dict[Path, tuple[str, ...]] = {}
    scanned_entries = 0
    total = 0
    truncated = False
    while pending:
        directory = pending.pop()
        _assert_contained(directory, root, label=label)
        remaining = max_files - scanned_entries
        if remaining <= 0:
            truncated = True
            break
        children, directory_truncated = _scan_directory(
            directory,
            max_entries=remaining,
            label=label,
        )
        if directory_truncated:
            truncated = True
            pending.clear()
            break
        directory_snapshots[directory] = _directory_names(children)
        scanned_entries += len(children)
        for child in children:
            path = Path(child.path)
            if _is_reparse(path):
                raise InventoryFailure(SourceStatus.INVALID, "reparse_point", f"{label} contains a reparse point")
            if child.is_dir(follow_symlinks=False):
                pending.append(path)
                continue
            if not child.is_file(follow_symlinks=False):
                raise InventoryFailure(SourceStatus.INVALID, "not_regular_file", f"{label} contains a non-regular entry")
            if len(entries) >= max_files:
                truncated = True
                pending.clear()
                break
            content, before, after = _read_stable_bytes(
                path,
                root=root,
                label=label,
                max_bytes=max_file_bytes,
                fault_hook=fault_hook,
            )
            total += len(content)
            if total > max_total_bytes:
                truncated = True
                pending.clear()
                break
            entries.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "size": len(content),
                    "sha256": _sha256_bytes(content),
                    "pre_identity": before,
                    "post_identity": after,
                }
            )
    if fault_hook is not None:
        fault_hook(label, "after_tree_scan")
    for directory, names_before in directory_snapshots.items():
        _assert_directory_stable(directory, names_before, label=label)
    semantic_entries = [
        {"path": item["path"], "size": item["size"], "sha256": item["sha256"]}
        for item in entries
    ]
    return _sha256_bytes(canonical_json_bytes(semantic_entries)), entries, truncated


def _collect_bundle(
    name: str,
    root: Path | None,
    *,
    required: bool,
    request: InventoryRequest,
    fault_hook: FaultHook | None,
) -> dict[str, Any]:
    if root is None:
        failure = InventoryFailure(SourceStatus.UNAVAILABLE, "not_declared", f"{name} was not declared")
        return _failed_source(name, "browser_bundle", required=required, failure=failure, observation={})
    if not (root / "manifest.json").exists():
        raise InventoryFailure(SourceStatus.INVALID, "manifest_missing", f"{name} has no manifest.json")
    document, manifest_digest, _, _ = _read_stable_json(
        root / "manifest.json",
        root=root,
        label=name,
        max_bytes=request.max_file_bytes,
        fault_hook=fault_hook,
    )
    if document.get("manifest_version") != 3:
        raise InventoryFailure(SourceStatus.UNSUPPORTED, "unsupported_manifest", f"{name} manifest version is unsupported")
    version = document.get("version")
    if not isinstance(version, str) or not version:
        raise InventoryFailure(SourceStatus.INVALID, "version_missing", f"{name} version is invalid")
    bundle_digest, entries, truncated = _bounded_tree_digest(
        root,
        max_files=request.max_records,
        max_file_bytes=request.max_file_bytes,
        max_total_bytes=request.max_total_bytes,
        fault_hook=fault_hook,
        label=name,
    )
    observed_manifest = next((entry for entry in entries if entry["path"] == "manifest.json"), None)
    if observed_manifest is not None and observed_manifest["sha256"] != manifest_digest:
        raise InventoryFailure(SourceStatus.UNSTABLE, "identity_changed", f"{name} manifest changed during observation")
    return _source(
        name,
        "browser_bundle",
        required=required,
        status=SourceStatus.OBSERVED,
        complete=not truncated,
        identity={
            "path": str(root.resolve(strict=False)),
            "manifest_version": 3,
            "version": version,
            "bundle_digest": bundle_digest,
            "manifest_digest": manifest_digest,
        },
        facts={"file_count": len(entries), "entries": entries},
        truncated=truncated,
    )


def _collect_native_manifest(
    request: InventoryRequest,
    fault_hook: FaultHook | None,
) -> dict[str, Any]:
    path = request.native_manifest_path
    if path is None:
        failure = InventoryFailure(SourceStatus.UNAVAILABLE, "not_declared", "Native Host install manifest was not declared")
        return _failed_source(
            "native_host_bundle",
            "native_host_bundle",
            required=False,
            failure=failure,
            observation={},
        )
    document, digest, before, after = _read_stable_json(
        path,
        root=path.parent,
        label="native_host_manifest",
        max_bytes=request.max_file_bytes,
        fault_hook=fault_hook,
    )
    if document.get("name") != "com.bartosz.dev_bridge" or document.get("type") != "stdio":
        raise InventoryFailure(SourceStatus.INVALID, "invalid_manifest", "Native Host manifest identity/type is invalid")
    origins = document.get("allowed_origins")
    if (
        not isinstance(origins, list)
        or not origins
        or not all(isinstance(item, str) and _EXTENSION_ORIGIN.fullmatch(item) for item in origins)
        or len(set(origins)) != len(origins)
    ):
        raise InventoryFailure(SourceStatus.INVALID, "invalid_manifest", "Native Host manifest allowed_origins are invalid")
    host_path = document.get("path")
    if not isinstance(host_path, str) or not host_path:
        raise InventoryFailure(SourceStatus.INVALID, "host_path_missing", "Native Host manifest path is missing")
    try:
        lexical_executable = Path(host_path).expanduser().absolute()
        if _has_reparse_component(lexical_executable):
            raise InventoryFailure(
                SourceStatus.INVALID,
                "reparse_point",
                "Native Host executable path contains a symlink/junction/reparse point",
            )
        executable = lexical_executable.resolve(strict=False)
    except InventoryFailure:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise InventoryFailure(SourceStatus.INVALID, "invalid_manifest", "Native Host executable path is invalid") from exc
    content, executable_before, executable_after = _read_stable_bytes(
        executable,
        root=executable.parent,
        label="native_host_executable",
        max_bytes=request.max_total_bytes,
        fault_hook=fault_hook,
    )
    try:
        if _file_token(path) != after:
            raise InventoryFailure(SourceStatus.UNSTABLE, "identity_changed", "Native Host manifest changed during observation")
    except OSError as exc:
        raise InventoryFailure(SourceStatus.UNSTABLE, "source_disappeared", f"Native Host manifest: {exc}") from exc
    return _source(
        "native_host_bundle",
        "native_host_bundle",
        required=True,
        status=SourceStatus.OBSERVED,
        complete=True,
        identity={
            "manifest_path": str(path),
            "manifest_digest": digest,
            "executable_path": str(executable),
            "executable_digest": _sha256_bytes(content),
        },
        facts={
            "allowed_origins": list(origins),
            "manifest_pre_identity": before,
            "manifest_post_identity": after,
            "executable_pre_identity": executable_before,
            "executable_post_identity": executable_after,
        },
    )


def _copy_stable_sqlite(
    database: Path,
    target_dir: Path,
    *,
    runtime_root: Path,
    max_total_bytes: int,
    fault_hook: FaultHook | None,
) -> tuple[Path, dict[str, Any]]:
    paths = {
        "database": database,
        "wal": database.with_name(database.name + "-wal"),
        "shm": database.with_name(database.name + "-shm"),
    }
    _assert_contained(database, runtime_root, label="journal")
    if not database.is_file():
        raise InventoryFailure(SourceStatus.INVALID, "not_regular_file", "Journal is not a regular file")
    pre: dict[str, Any] = {}
    for name, path in paths.items():
        if path.exists():
            _assert_contained(path, runtime_root, label=f"journal_{name}")
            if not path.is_file():
                raise InventoryFailure(SourceStatus.INVALID, "not_regular_file", f"Journal {name} is not a regular file")
            pre[name] = _file_token(path)
    total = sum(int(item["size"]) for item in pre.values())
    if total > max_total_bytes:
        raise InventoryFailure(SourceStatus.INVALID, "size_limit", "Journal snapshot exceeds the bounded read limit")
    copy_path = target_dir / database.name
    digests: dict[str, str] = {}
    for name, source_path in paths.items():
        if name not in pre:
            continue
        destination = target_dir / source_path.name
        digest = hashlib.sha256()
        copied = 0
        try:
            with source_path.open("rb") as source:
                target_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(target_fd, "wb", closefd=True) as target:
                    while True:
                        block = source.read(min(1024 * 1024, max_total_bytes - copied + 1))
                        if not block:
                            break
                        copied += len(block)
                        if copied > max_total_bytes:
                            raise InventoryFailure(SourceStatus.INVALID, "size_limit", "Journal snapshot exceeds the bounded read limit")
                        digest.update(block)
                        target.write(block)
        except PermissionError as exc:
            raise InventoryFailure(SourceStatus.UNAVAILABLE, "permission_denied", f"Journal {name}: {exc}") from exc
        except OSError as exc:
            raise InventoryFailure(SourceStatus.UNAVAILABLE, "read_failed", f"Journal {name}: {exc}") from exc
        digests[name] = "sha256:" + digest.hexdigest()
    if fault_hook is not None:
        fault_hook("journal", "after_snapshot")
    post: dict[str, Any] = {}
    for name, path in paths.items():
        if path.exists():
            post[name] = _file_token(path)
    if pre != post:
        raise InventoryFailure(SourceStatus.UNSTABLE, "identity_changed", "Journal/WAL identity changed during observation")
    return copy_path, {
        "source_files": {
            name: {"path": str(paths[name]), "pre_identity": pre[name], "post_identity": post[name], "sha256": digests[name]}
            for name in sorted(pre)
        },
        "wal_present": "wal" in pre,
        "shm_present": "shm" in pre,
        "snapshot_bytes": total,
    }


def _query_bounded_ids(
    connection: sqlite3.Connection,
    *,
    table: str,
    id_column: str,
    where: str,
    parameters: Sequence[Any],
    limit: int,
) -> dict[str, Any]:
    count = int(connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", parameters).fetchone()[0])
    rows = connection.execute(
        f"SELECT {id_column} FROM {table} WHERE {where} ORDER BY {id_column} LIMIT ?",
        (*parameters, limit + 1),
    ).fetchall()
    identifiers = [str(row[0]) for row in rows[:limit]]
    return {"count": count, "ids": identifiers, "truncated": count > limit}


def _inspect_sqlite_copy(
    copy_path: Path,
    *,
    max_records: int,
    fault_hook: FaultHook | None = None,
) -> dict[str, Any]:
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            copy_path.resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=1.0,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        integrity_rows = [str(row[0]) for row in connection.execute("PRAGMA integrity_check").fetchmany(101)]
        if integrity_rows != ["ok"]:
            raise InventoryFailure(SourceStatus.INVALID, "integrity_failed", "Journal integrity_check did not return ok")
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        if "schema_migrations" not in tables:
            raise InventoryFailure(SourceStatus.INVALID, "migration_table_missing", "Journal has no schema_migrations table")
        unknown_tables = sorted(tables - SUPPORTED_JOURNAL_TABLES)
        if unknown_tables:
            raise InventoryFailure(SourceStatus.UNSUPPORTED, "unknown_tables", "Journal contains unsupported tables")
        applied = [
            (int(row[0]), str(row[1]), str(row[2]))
            for row in connection.execute(
                "SELECT version,name,checksum FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        versions = [item[0] for item in applied]
        if versions != list(range(1, len(versions) + 1)):
            raise InventoryFailure(SourceStatus.INVALID, "migration_gap", "Journal migration versions contain gaps")
        if versions and versions[-1] > len(SUPPORTED_MIGRATIONS):
            raise InventoryFailure(SourceStatus.UNSUPPORTED, "future_schema", "Journal schema is newer than the supported reader")
        for version, name, checksum in applied:
            expected = SUPPORTED_MIGRATION_MAP.get(version)
            if expected is None:
                raise InventoryFailure(SourceStatus.UNSUPPORTED, "unsupported_migration", "Journal migration is unsupported")
            if expected.name != name or expected.checksum() != checksum:
                raise InventoryFailure(SourceStatus.INVALID, "migration_mismatch", "Journal migration name/checksum differs from the supported registry")
        required_tables = {"sessions", "commands", "outbox", "operation_effects", "service_instances"}
        if not required_tables.issubset(tables):
            raise InventoryFailure(SourceStatus.UNSUPPORTED, "schema_too_old", "Journal schema lacks required R0a tables")
        sessions = _query_bounded_ids(
            connection,
            table="sessions",
            id_column="session_id",
            where=f"state NOT IN ({','.join('?' for _ in _TERMINAL_SESSION_STATES)})",
            parameters=tuple(sorted(_TERMINAL_SESSION_STATES)),
            limit=max_records,
        )
        commands = _query_bounded_ids(
            connection,
            table="commands",
            id_column="command_id",
            where=f"state NOT IN ({','.join('?' for _ in _TERMINAL_COMMAND_STATES)})",
            parameters=tuple(sorted(_TERMINAL_COMMAND_STATES)),
            limit=max_records,
        )
        outbox = _query_bounded_ids(
            connection,
            table="outbox",
            id_column="command_id",
            where="state != ?",
            parameters=("published",),
            limit=max_records,
        )
        reconciliation = _query_bounded_ids(
            connection,
            table="commands",
            id_column="command_id",
            where="state = ?",
            parameters=("manual_reconciliation_required",),
            limit=max_records,
        )
        effect_rows = connection.execute(
            """
            SELECT e.command_id
            FROM operation_effects e
            JOIN commands c ON c.command_id=e.command_id
            WHERE c.state != 'acknowledged'
            ORDER BY e.command_id
            LIMIT ?
            """,
            (max_records + 1,),
        ).fetchall()
        effect_count = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM operation_effects e
                JOIN commands c ON c.command_id=e.command_id
                WHERE c.state != 'acknowledged'
                """
            ).fetchone()[0]
        )
        effects = {
            "count": effect_count,
            "ids": [str(row[0]) for row in effect_rows[:max_records]],
            "truncated": effect_count > max_records,
        }
        service_rows = connection.execute(
                "SELECT instance_id,pid,state FROM service_instances WHERE state IN ('running','stopping') ORDER BY instance_id LIMIT ?",
                (max_records + 1,),
            ).fetchall()[:max_records]
        process_pre = {int(row[1]): _pid_identity(int(row[1])) for row in service_rows}
        if fault_hook is not None:
            fault_hook("process_identity", "between_observations")
        process_post = {int(row[1]): _pid_identity(int(row[1])) for row in service_rows}
        if process_pre != process_post:
            raise InventoryFailure(
                SourceStatus.UNSTABLE,
                "pid_reused",
                "An active-writer PID disappeared or changed process identity during observation",
            )
        services = [
            {
                "instance_id": str(row[0]),
                "pid": int(row[1]),
                "state": str(row[2]),
                "pid_alive": process_post[int(row[1])]["alive"],
                "process_identity": process_post[int(row[1])],
            }
            for row in service_rows
        ]
        service_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM service_instances WHERE state IN ('running','stopping')"
            ).fetchone()[0]
        )
        journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        return {
            "integrity": "ok",
            "journal_mode": journal_mode,
            "schema_version": versions[-1] if versions else 0,
            "supported_schema_version": len(SUPPORTED_MIGRATIONS),
            "migrations": [
                {"version": version, "name": name, "checksum": checksum}
                for version, name, checksum in applied
            ],
            "table_count": len(tables),
            "unresolved": {
                "sessions": sessions,
                "commands": commands,
                "outbox": outbox,
                "effects": effects,
                "manual_reconciliation": reconciliation,
            },
            "active_writer_candidates": {
                "count": service_count,
                "items": services,
                "truncated": service_count > max_records,
            },
        }
    except InventoryFailure:
        raise
    except sqlite3.OperationalError as exc:
        message = str(exc).lower()
        if "locked" in message or "busy" in message:
            raise InventoryFailure(SourceStatus.UNAVAILABLE, "sqlite_busy", "Journal is busy/locked") from exc
        if "malformed" in message or "not a database" in message:
            raise InventoryFailure(SourceStatus.INVALID, "sqlite_corrupt", "Journal is corrupt") from exc
        raise InventoryFailure(SourceStatus.INVALID, "sqlite_error", f"Journal read failed: {exc}") from exc
    except sqlite3.DatabaseError as exc:
        raise InventoryFailure(SourceStatus.INVALID, "sqlite_corrupt", f"Journal read failed: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()


def _collect_journal(
    request: InventoryRequest,
    descriptor: BridgeDescriptor,
    fault_hook: FaultHook | None,
) -> dict[str, Any]:
    database = descriptor.journal_path
    if not database.exists():
        raise InventoryFailure(SourceStatus.UNAVAILABLE, "missing", "Journal is unavailable")
    scratch = request.scratch_dir or Path(tempfile.gettempdir())
    scratch = scratch.expanduser().absolute().resolve(strict=False)
    _assert_contained(scratch, scratch, label="scratch")
    if not scratch.is_dir():
        raise InventoryFailure(SourceStatus.INVALID, "scratch_unavailable", "Scratch root is not a directory")
    if _contained(scratch, request.repository_path) or _contained(scratch, descriptor.runtime_dir):
        raise InventoryFailure(SourceStatus.INVALID, "scratch_overlap", "Scratch root overlaps an observed source root")
    temp_parent = scratch / f"bdb-r0a-journal-copy-{uuid4().hex}"
    try:
        temp_parent.mkdir(mode=0o755 if os.name == "nt" else 0o700)
    except PermissionError as exc:
        raise InventoryFailure(SourceStatus.UNAVAILABLE, "scratch_permission_denied", f"Scratch root: {exc}") from exc
    try:
        copy_path, snapshot = _copy_stable_sqlite(
            database,
            temp_parent,
            runtime_root=descriptor.runtime_dir,
            max_total_bytes=request.max_total_bytes,
            fault_hook=fault_hook,
        )
        facts = _inspect_sqlite_copy(
            copy_path,
            max_records=request.max_records,
            fault_hook=fault_hook,
        )
    finally:
        _remove_private_temp(temp_parent)
    truncated = any(
        bool(group.get("truncated"))
        for group in facts["unresolved"].values()
    ) or bool(facts["active_writer_candidates"]["truncated"])
    return _source(
        "journal",
        "sqlite_journal_v12",
        required=True,
        status=SourceStatus.OBSERVED,
        complete=not truncated,
        identity={
            "path": str(database),
            "database_digest": snapshot["source_files"]["database"]["sha256"],
            "schema_version": facts["schema_version"],
        },
        facts={**snapshot, **facts, "source_open_mode": "byte-copy-only", "copy_query_mode": "mode=ro+query_only"},
        truncated=truncated,
    )


def _remove_private_temp(root: Path) -> None:
    # The directory is created by this module with a random basename and contains
    # only private SQLite copies.  Do not use this helper for caller-owned paths.
    try:
        for child in root.iterdir():
            if child.is_file() and not child.is_symlink():
                child.unlink()
        root.rmdir()
    except FileNotFoundError:
        return


def _collect_receipts(
    request: InventoryRequest,
    descriptor: NativeDescriptor | None,
    fault_hook: FaultHook | None,
) -> dict[str, Any]:
    if descriptor is None:
        raise InventoryFailure(SourceStatus.UNAVAILABLE, "not_declared", "Receipt store was not declared")
    path = descriptor.request_store_path
    document, digest, before, after = _read_stable_json(
        path,
        root=descriptor.config_path.parent,
        label="receipt_store",
        max_bytes=request.max_file_bytes,
        fault_hook=fault_hook,
    )
    if document.get("schema") != RECEIPT_SCHEMA:
        raise InventoryFailure(SourceStatus.UNSUPPORTED, "unsupported_schema", "Receipt store schema is unsupported")
    requests = document.get("requests")
    reservations = document.get("submission_reservations")
    if not isinstance(requests, dict) or not isinstance(reservations, dict):
        raise InventoryFailure(SourceStatus.INVALID, "invalid_shape", "Receipt store collections are invalid")
    truncated = len(requests) > request.max_records or len(reservations) > request.max_records
    reservation_items: list[dict[str, Any]] = []
    reservation_commands: set[str] = set()
    for nonce, item in sorted(reservations.items(), key=lambda pair: str(pair[0]))[: request.max_records]:
        if not isinstance(nonce, str) or not isinstance(item, dict):
            raise InventoryFailure(SourceStatus.INVALID, "invalid_receipt", "Receipt reservation is invalid")
        command_id = item.get("command_id")
        filename = item.get("filename")
        if not isinstance(command_id, str) or not isinstance(filename, str) or not _SAFE_SPOOL_NAME.fullmatch(filename):
            raise InventoryFailure(SourceStatus.INVALID, "invalid_receipt", "Receipt reservation identity is invalid")
        if command_id in reservation_commands:
            raise InventoryFailure(SourceStatus.INVALID, "duplicate_identity", "Receipt reservations contain a duplicate command_id")
        reservation_commands.add(command_id)
        reservation_items.append(
            {
                "client_submission_nonce": nonce,
                "command_id": command_id,
                "filename": filename,
                "action_sha256": item.get("action_sha256"),
            }
        )
    return _source(
        "receipts",
        "native_request_receipts_v1",
        required=True,
        status=SourceStatus.OBSERVED,
        complete=not truncated,
        identity={"path": str(path), "sha256": digest, "schema": RECEIPT_SCHEMA},
        facts={
            "request_count": len(requests),
            "request_ids": sorted(str(item) for item in requests)[: request.max_records],
            "reservation_count": len(reservations),
            "reservations": reservation_items,
            "pre_identity": before,
            "post_identity": after,
        },
        truncated=truncated,
    )


def _collect_spool(
    request: InventoryRequest,
    descriptor: BridgeDescriptor,
    fault_hook: FaultHook | None,
) -> dict[str, Any]:
    root = descriptor.direct_spool_dir
    _assert_contained(root, descriptor.runtime_dir, label="spool")
    if not root.is_dir():
        raise InventoryFailure(SourceStatus.INVALID, "not_directory", "Spool is not a directory")
    children, directory_truncated = _scan_directory(
        root,
        max_entries=request.max_records,
        label="Spool",
    )
    if directory_truncated:
        children = []
    names_before = _directory_names(children)
    candidates = [item for item in children if item.name.endswith(".json")]
    truncated = directory_truncated
    entries: list[dict[str, Any]] = []
    command_ids: set[str] = set()
    nonces: set[str] = set()
    total = 0
    for item in candidates[: request.max_records]:
        if not _SAFE_SPOOL_NAME.fullmatch(item.name):
            raise InventoryFailure(SourceStatus.INVALID, "unsafe_name", "Spool contains an unsafe filename")
        path = Path(item.path)
        document, digest, before, after = _read_stable_json(
            path,
            root=root,
            label="spool_entry",
            max_bytes=request.max_file_bytes,
            fault_hook=fault_hook,
        )
        total += before["size"]
        if total > request.max_total_bytes:
            truncated = True
            break
        if document.get("schema") != SPOOL_SCHEMA:
            raise InventoryFailure(SourceStatus.UNSUPPORTED, "unsupported_schema", "Spool envelope schema is unsupported")
        command = document.get("command")
        if not isinstance(command, dict):
            raise InventoryFailure(SourceStatus.INVALID, "invalid_envelope", "Spool command is invalid")
        command_id = command.get("command_id")
        if not isinstance(command_id, str) or not command_id:
            raise InventoryFailure(SourceStatus.INVALID, "invalid_envelope", "Spool command_id is invalid")
        nonce = command.get("client_submission_nonce")
        if command_id in command_ids or (isinstance(nonce, str) and nonce in nonces):
            raise InventoryFailure(SourceStatus.INVALID, "duplicate_identity", "Spool contains duplicate command/submission identity")
        command_ids.add(command_id)
        if isinstance(nonce, str):
            nonces.add(nonce)
        entries.append(
            {
                "filename": item.name,
                "command_id": command_id,
                "client_submission_nonce": nonce,
                "sha256": digest,
                "pre_identity": before,
                "post_identity": after,
            }
        )
    if fault_hook is not None:
        fault_hook("spool", "after_scan")
    if not directory_truncated:
        _assert_directory_stable(root, names_before, label="Spool")
    return _source(
        "spool",
        "local_spool_v1",
        required=True,
        status=SourceStatus.OBSERVED,
        complete=not truncated,
        identity={"path": str(root)},
        facts={
            "entry_count": len(candidates),
            "directory_entry_count_lower_bound": request.max_records + 1 if directory_truncated else len(children),
            "entries": entries,
            "bytes_observed": total,
        },
        truncated=truncated,
    )


def _collect_promoter(
    request: InventoryRequest,
    descriptor: BridgeDescriptor,
    fault_hook: FaultHook | None,
) -> dict[str, Any]:
    runtime = descriptor.runtime_dir
    state_path = runtime / "workspace-promoter-state.json"
    receipts_root = runtime / "promotions"
    state, state_digest, state_before, state_after = _read_stable_json(
        state_path,
        root=runtime,
        label="promoter_state",
        max_bytes=request.max_file_bytes,
        fault_hook=fault_hook,
    )
    if state.get("schema") != PROMOTER_STATE_SCHEMA:
        raise InventoryFailure(SourceStatus.UNSUPPORTED, "unsupported_schema", "Promoter state schema is unsupported")
    if not isinstance(state.get("initialized"), bool) or not isinstance(state.get("seen"), dict):
        raise InventoryFailure(SourceStatus.INVALID, "invalid_promoter_state", "Promoter state is invalid")
    _assert_contained(receipts_root, runtime, label="promotion_receipts")
    if not receipts_root.is_dir():
        raise InventoryFailure(SourceStatus.INVALID, "not_directory", "Promotion receipts root is not a directory")
    children, directory_truncated = _scan_directory(
        receipts_root,
        max_entries=request.max_records,
        label="Promotion receipts",
    )
    if directory_truncated:
        children = []
    names_before = _directory_names(children)
    receipt_files = [item for item in children if item.name.endswith(".json") and not item.name.startswith(".")]
    truncated = len(state["seen"]) > request.max_records or directory_truncated
    receipts: list[dict[str, Any]] = []
    receipt_identities: set[tuple[str, int]] = set()
    repository_sequences: set[int] = set()
    for item in receipt_files[: request.max_records]:
        document, digest, before, after = _read_stable_json(
            Path(item.path),
            root=receipts_root,
            label="promotion_receipt",
            max_bytes=request.max_file_bytes,
            fault_hook=fault_hook,
        )
        if document.get("schema") != PROMOTION_RECEIPT_SCHEMA:
            raise InventoryFailure(SourceStatus.UNSUPPORTED, "unsupported_schema", "Promotion receipt schema is unsupported")
        source_commit = document.get("source_commit")
        parent_commit = document.get("parent_commit")
        if not isinstance(source_commit, str) or not _SHA40.fullmatch(source_commit):
            raise InventoryFailure(SourceStatus.INVALID, "invalid_receipt", "Promotion receipt source commit is invalid")
        if not isinstance(parent_commit, str) or not _SHA40.fullmatch(parent_commit):
            raise InventoryFailure(SourceStatus.INVALID, "invalid_receipt", "Promotion receipt parent commit is invalid")
        sequence = document.get("repository_event_seq")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
            raise InventoryFailure(SourceStatus.INVALID, "invalid_receipt", "Promotion receipt repository sequence is invalid")
        session_id = document.get("session_id")
        command_sequence = document.get("sequence")
        if (
            not isinstance(session_id, str)
            or not session_id
            or isinstance(command_sequence, bool)
            or not isinstance(command_sequence, int)
            or command_sequence <= 0
        ):
            raise InventoryFailure(SourceStatus.INVALID, "invalid_receipt", "Promotion receipt command identity is invalid")
        receipt_identity = (session_id, command_sequence)
        if receipt_identity in receipt_identities or sequence in repository_sequences:
            raise InventoryFailure(SourceStatus.INVALID, "duplicate_identity", "Promotion receipts contain a duplicate identity")
        receipt_identities.add(receipt_identity)
        repository_sequences.add(sequence)
        receipts.append(
            {
                "filename": item.name,
                "session_id": session_id,
                "sequence": command_sequence,
                "repository_event_seq": sequence,
                "source_commit": source_commit.lower(),
                "parent_commit": parent_commit.lower(),
                "result_sha256": document.get("result_sha256"),
                "sha256": digest,
                "pre_identity": before,
                "post_identity": after,
            }
        )
    sequence_path = receipts_root / ".repository-event-seq.json"
    sequence_value = 0
    sequence_identity: dict[str, Any] | None = None
    if sequence_path.exists():
        sequence_document, sequence_digest, seq_before, seq_after = _read_stable_json(
            sequence_path,
            root=receipts_root,
            label="repository_event_sequence",
            max_bytes=request.max_file_bytes,
            fault_hook=fault_hook,
        )
        if sequence_document.get("schema") != REPOSITORY_SEQUENCE_SCHEMA:
            raise InventoryFailure(SourceStatus.UNSUPPORTED, "unsupported_schema", "Repository sequence schema is unsupported")
        sequence_value = sequence_document.get("repository_event_seq")
        if isinstance(sequence_value, bool) or not isinstance(sequence_value, int) or sequence_value < 0:
            raise InventoryFailure(SourceStatus.INVALID, "invalid_sequence", "Repository event sequence is invalid")
        sequence_identity = {
            "sha256": sequence_digest,
            "pre_identity": seq_before,
            "post_identity": seq_after,
        }
    if not truncated and (
        (receipts and max(item["repository_event_seq"] for item in receipts) != sequence_value)
        or (not receipts and sequence_value != 0)
    ):
        raise InventoryFailure(SourceStatus.INVALID, "sequence_disagreement", "Promotion receipts and repository sequence disagree")
    if fault_hook is not None:
        fault_hook("promoter", "after_scan")
    if not directory_truncated:
        _assert_directory_stable(receipts_root, names_before, label="Promotion receipts")
    try:
        if _file_token(state_path) != state_after:
            raise InventoryFailure(SourceStatus.UNSTABLE, "identity_changed", "Promoter state changed during observation")
    except OSError as exc:
        raise InventoryFailure(SourceStatus.UNSTABLE, "source_disappeared", f"Promoter state: {exc}") from exc
    return _source(
        "promoter",
        "workspace_promoter_v1",
        required=True,
        status=SourceStatus.OBSERVED,
        complete=not truncated,
        identity={"state_path": str(state_path), "state_digest": state_digest, "receipts_path": str(receipts_root)},
        facts={
            "initialized": state["initialized"],
            "seen_count": len(state["seen"]),
            "seen_keys": sorted(str(key) for key in state["seen"])[: request.max_records],
            "receipt_count": len(receipt_files),
            "directory_entry_count_lower_bound": request.max_records + 1 if directory_truncated else len(children),
            "receipts": receipts,
            "repository_event_seq": sequence_value,
            "sequence_identity": sequence_identity,
            "state_pre_identity": state_before,
            "state_post_identity": state_after,
        },
        truncated=truncated,
    )


def _source_by_name(sources: Sequence[Mapping[str, Any]], name: str) -> Mapping[str, Any] | None:
    return next((source for source in sources if source.get("name") == name), None)


def _correlations(sources: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    blockers: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    correlation_inputs_complete = all(
        source.get("required") is not True
        or (
            source.get("status") == SourceStatus.OBSERVED.value
            and source.get("complete") is True
        )
        for source in sources
    )
    receipts = _source_by_name(sources, "receipts")
    spool = _source_by_name(sources, "spool")
    if (
        receipts
        and spool
        and receipts.get("status") == spool.get("status") == SourceStatus.OBSERVED.value
        and receipts.get("complete") is True
        and spool.get("complete") is True
    ):
        reservation_items = receipts.get("facts", {}).get("reservations", [])
        spool_entries = spool.get("facts", {}).get("entries", [])
        spool_names = {item.get("filename") for item in spool_entries}
        reservation_nonces = {item.get("client_submission_nonce") for item in reservation_items}
        receipt_without_spool = [item.get("command_id") for item in reservation_items if item.get("filename") not in spool_names]
        spool_without_receipt = [
            item.get("command_id")
            for item in spool_entries
            if item.get("client_submission_nonce") not in reservation_nonces
        ]
        if receipt_without_spool:
            blockers.append({"code": "receipt_without_spool", "ids": sorted(str(item) for item in receipt_without_spool)})
        if spool_without_receipt:
            blockers.append({"code": "spool_without_receipt", "ids": sorted(str(item) for item in spool_without_receipt)})

    repository = _source_by_name(sources, "repository")
    bridge = _source_by_name(sources, "bridge_config")
    if repository and bridge and repository.get("status") == bridge.get("status") == SourceStatus.OBSERVED.value:
        repo_path = repository.get("identity", {}).get("path")
        configured = bridge.get("facts", {}).get("declared_paths", {}).get("fixture_repository")
        if isinstance(repo_path, str) and isinstance(configured, str) and _comparable(Path(repo_path)) != _comparable(Path(configured)):
            blockers.append({"code": "repository_binding_mismatch"})

    native_config = _source_by_name(sources, "native_config")
    if (
        native_config
        and bridge
        and native_config.get("status") == bridge.get("status") == SourceStatus.OBSERVED.value
    ):
        bridge_path = bridge.get("identity", {}).get("path")
        native_paths = native_config.get("facts", {}).get("repository_config_paths", [])
        if isinstance(bridge_path, str) and not any(
            isinstance(item, str) and _comparable(Path(item)) == _comparable(Path(bridge_path))
            for item in native_paths
        ):
            blockers.append({"code": "native_bridge_binding_mismatch"})

    native_bundle = _source_by_name(sources, "native_host_bundle")
    if (
        native_config
        and native_bundle
        and native_config.get("status") == native_bundle.get("status") == SourceStatus.OBSERVED.value
        and native_config.get("complete") is True
        and native_bundle.get("complete") is True
        and native_config.get("facts", {}).get("allowed_origins")
        != native_bundle.get("facts", {}).get("allowed_origins")
    ):
        blockers.append({"code": "native_origin_mismatch"})

    promoter = _source_by_name(sources, "promoter")
    if (
        repository
        and promoter
        and repository.get("status") == promoter.get("status") == SourceStatus.OBSERVED.value
        and repository.get("complete") is True
        and promoter.get("complete") is True
    ):
        receipts_list = promoter.get("facts", {}).get("receipts", [])
        if receipts_list:
            latest = max(receipts_list, key=lambda item: int(item.get("repository_event_seq", 0)))
            if latest.get("source_commit") != repository.get("identity", {}).get("head"):
                blockers.append(
                    {
                        "code": "promoter_ref_disagreement",
                        "promoter_commit": latest.get("source_commit"),
                        "repository_head": repository.get("identity", {}).get("head"),
                    }
                )

    source_bundle = _source_by_name(sources, "repository_browser_bundle")
    deployed_bundle = _source_by_name(sources, "deployed_browser_bundle")
    runtime = _source_by_name(sources, "repository_runtime")
    if (
        runtime
        and source_bundle
        and runtime.get("status") == source_bundle.get("status") == SourceStatus.OBSERVED.value
        and runtime.get("complete") is True
        and source_bundle.get("complete") is True
    ):
        if runtime.get("identity", {}).get("service_runtime_version") != source_bundle.get("identity", {}).get("version"):
            blockers.append({"code": "runtime_browser_version_mismatch"})
    if (
        source_bundle
        and deployed_bundle
        and source_bundle.get("status") == deployed_bundle.get("status") == SourceStatus.OBSERVED.value
        and source_bundle.get("complete") is True
        and deployed_bundle.get("complete") is True
    ):
        if source_bundle.get("identity", {}).get("bundle_digest") != deployed_bundle.get("identity", {}).get("bundle_digest"):
            blockers.append({"code": "bundle_repository_mismatch"})

    journal = _source_by_name(sources, "journal")
    if journal and journal.get("status") == SourceStatus.OBSERVED.value:
        candidates = journal.get("facts", {}).get("active_writer_candidates", {})
        if int(candidates.get("count", 0)):
            findings.append({"code": "active_writer_candidates_observed", "count": candidates.get("count")})
    return {
        "complete": correlation_inputs_complete and not blockers,
        "blockers": sorted(blockers, key=lambda item: str(item.get("code"))),
        "findings": sorted(findings, key=lambda item: str(item.get("code"))),
    }


def _overall(sources: Sequence[Mapping[str, Any]], correlations: Mapping[str, Any]) -> dict[str, Any]:
    required = [source for source in sources if source.get("required") is True]
    unsupported = [source["name"] for source in required if source.get("status") == SourceStatus.UNSUPPORTED.value]
    invalid = [source["name"] for source in required if source.get("status") == SourceStatus.INVALID.value]
    incomplete = [
        source["name"]
        for source in required
        if source.get("status") in {SourceStatus.UNAVAILABLE.value, SourceStatus.UNSTABLE.value}
        or source.get("complete") is not True
    ]
    blockers = [dict(item) for item in correlations.get("blockers", [])]
    if unsupported:
        result = OverallResult.UNSUPPORTED
    elif invalid:
        result = OverallResult.INVALID
    elif incomplete or blockers:
        result = OverallResult.INCOMPLETE
    else:
        result = OverallResult.READY_FOR_LOCAL_GATE
    return {
        "result": result.value,
        "complete": result is OverallResult.READY_FOR_LOCAL_GATE,
        "unsupported_sources": sorted(unsupported),
        "invalid_sources": sorted(invalid),
        "incomplete_sources": sorted(incomplete),
        "blockers": blockers,
        "safe_to_mutate": False,
        "note": "R0a readiness permits only the separate R0b local gate; it is never a production SAFE decision.",
    }


class InventoryProvider:
    """Bounded R0a evidence provider.  It never opens observed stores for writing."""

    def __init__(
        self,
        *,
        now_fn: Callable[[], str] = _utc_now,
        monotonic_fn: Callable[[], float] = time.monotonic,
        fault_hook: FaultHook | None = None,
    ) -> None:
        self._now_fn = now_fn
        self._monotonic_fn = monotonic_fn
        self._fault_hook = fault_hook

    def collect(self, request: InventoryRequest) -> dict[str, Any]:
        started_at = self._now_fn()
        started = self._monotonic_fn()
        sources: list[dict[str, Any]] = []
        bridge_descriptor: BridgeDescriptor | None = None
        native_descriptor: NativeDescriptor | None = None

        sources.append(self._attempt("repository", "git_repository", True, lambda: _collect_repository(request, self._fault_hook)))
        sources.append(
            self._attempt(
                "repository_runtime",
                "service_native_runtime_source",
                True,
                lambda: _collect_repository_runtime(request, self._fault_hook),
            )
        )
        bridge_record, bridge_descriptor = self._attempt_with_descriptor(
            "bridge_config",
            "declared_configuration",
            True,
            lambda: _collect_bridge_config(request, self._fault_hook),
        )
        sources.append(bridge_record)
        native_record, native_descriptor = self._attempt_with_descriptor(
            "native_config",
            "declared_configuration",
            True,
            lambda: _collect_native_config(request, self._fault_hook),
        )
        sources.append(native_record)

        source_bundle_path = request.repository_path / "browser_extension"
        sources.append(
            self._attempt(
                "repository_browser_bundle",
                "browser_bundle",
                True,
                lambda: _collect_bundle(
                    "repository_browser_bundle",
                    source_bundle_path,
                    required=True,
                    request=request,
                    fault_hook=self._fault_hook,
                ),
            )
        )
        sources.append(
            self._attempt(
                "deployed_browser_bundle",
                "browser_bundle",
                request.browser_bundle_path is not None,
                lambda: _collect_bundle(
                    "deployed_browser_bundle",
                    request.browser_bundle_path,
                    required=request.browser_bundle_path is not None,
                    request=request,
                    fault_hook=self._fault_hook,
                ),
            )
        )
        sources.append(
            self._attempt(
                "native_host_bundle",
                "native_host_bundle",
                request.native_manifest_path is not None,
                lambda: _collect_native_manifest(request, self._fault_hook),
            )
        )

        if bridge_descriptor is None:
            for name, kind in (("journal", "sqlite_journal_v12"), ("spool", "local_spool_v1"), ("promoter", "workspace_promoter_v1")):
                sources.append(
                    _failed_source(
                        name,
                        kind,
                        required=True,
                        failure=InventoryFailure(SourceStatus.UNAVAILABLE, "prerequisite_unavailable", "Bridge config could not declare this source"),
                        observation={},
                    )
                )
        else:
            sources.append(self._attempt("journal", "sqlite_journal_v12", True, lambda: _collect_journal(request, bridge_descriptor, self._fault_hook)))
            sources.append(self._attempt("spool", "local_spool_v1", True, lambda: _collect_spool(request, bridge_descriptor, self._fault_hook)))
            sources.append(self._attempt("promoter", "workspace_promoter_v1", True, lambda: _collect_promoter(request, bridge_descriptor, self._fault_hook)))

        sources.append(
            self._attempt(
                "receipts",
                "native_request_receipts_v1",
                True,
                lambda: _collect_receipts(request, native_descriptor, self._fault_hook),
            )
        )
        sources = sorted(sources, key=lambda item: item["name"])
        correlations = _correlations(sources)
        report: dict[str, Any] = {
            "schema": REPORT_SCHEMA,
            "provider": {"name": "bdb-runtime-inventory", "version": PROVIDER_VERSION},
            "policy": {
                "version": POLICY_VERSION,
                "max_records": request.max_records,
                "max_file_bytes": request.max_file_bytes,
                "max_total_bytes": request.max_total_bytes,
                "timeout_seconds": request.timeout_seconds,
            },
            "inventory_id": "inv-" + uuid4().hex,
            "representation": "PRIVATE_EXACT",
            "observation": {
                "started_at": started_at,
                "finished_at": self._now_fn(),
                "duration_ms": max(0, int((self._monotonic_fn() - started) * 1000)),
                "platform": sys.platform,
                "python": sys.version.split()[0],
            },
            "sources": sources,
            "correlations": correlations,
        }
        report["overall"] = _overall(sources, correlations)
        report["semantic_digest"] = semantic_digest(report)
        return report

    def _attempt(
        self,
        name: str,
        kind: str,
        required: bool,
        callback: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        started_at = self._now_fn()
        started = self._monotonic_fn()
        try:
            record = callback()
        except InventoryFailure as failure:
            record = _failed_source(name, kind, required=required, failure=failure, observation={})
        except PermissionError as exc:
            record = _failed_source(
                name,
                kind,
                required=required,
                failure=InventoryFailure(SourceStatus.UNAVAILABLE, "permission_denied", str(exc)),
                observation={},
            )
        except OSError as exc:
            record = _failed_source(
                name,
                kind,
                required=required,
                failure=InventoryFailure(SourceStatus.UNAVAILABLE, "os_error", str(exc)),
                observation={},
            )
        except (ValueError, TypeError, RecursionError) as exc:
            record = _failed_source(
                name,
                kind,
                required=required,
                failure=InventoryFailure(SourceStatus.INVALID, "invalid_data", str(exc)),
                observation={},
            )
        record["required"] = required
        record["observation"] = {
            "started_at": started_at,
            "finished_at": self._now_fn(),
            "duration_ms": max(0, int((self._monotonic_fn() - started) * 1000)),
        }
        return record

    def _attempt_with_descriptor(
        self,
        name: str,
        kind: str,
        required: bool,
        callback: Callable[[], tuple[dict[str, Any], Any]],
    ) -> tuple[dict[str, Any], Any | None]:
        descriptor: Any | None = None

        def collect() -> dict[str, Any]:
            nonlocal descriptor
            record, descriptor = callback()
            return record

        return self._attempt(name, kind, required, collect), descriptor


def _forbidden_output(target: Path, forbidden_roots: Iterable[Path]) -> bool:
    for root in forbidden_roots:
        if target == root or _contained(target, root):
            return True
    return False


def atomic_write_report(
    report: Mapping[str, Any],
    output_path: str | Path,
    *,
    forbidden_roots: Iterable[Path] = (),
    overwrite: bool = False,
    fault_hook: FaultHook | None = None,
) -> Path:
    target = Path(output_path).expanduser().absolute()
    parent = target.parent
    if not parent.is_dir() or _has_reparse_component(parent):
        raise OutputFailure("Report parent must be an existing regular directory")
    if _forbidden_output(target, forbidden_roots):
        raise OutputFailure("Report output must stay outside every observed source root")
    if target.exists() and not overwrite:
        raise OutputFailure("Report output already exists")
    if target.exists() and (target.is_dir() or _is_reparse(target)):
        raise OutputFailure("Report output is not a regular file")
    payload = canonical_json_bytes(report)
    temporary = parent / f".{target.name}.{uuid4().hex}.tmp"
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if fault_hook is not None:
            fault_hook("report_output", "before_replace")
        if overwrite:
            os.replace(temporary, target)
        else:
            try:
                os.link(temporary, target)
            except FileExistsError as exc:
                raise OutputFailure("Report output already exists") from exc
            try:
                temporary.unlink()
            except OSError:
                target.unlink()
                raise
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
    except Exception as exc:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        if isinstance(exc, OutputFailure):
            raise
        raise OutputFailure(f"Atomic report write failed: {_safe_message(exc)}") from exc
    return target


def report_source_roots(request: InventoryRequest, report: Mapping[str, Any] | None = None) -> tuple[Path, ...]:
    roots = [request.repository_path, request.bridge_config_path]
    for path in (request.native_config_path, request.browser_bundle_path, request.native_manifest_path):
        if path is not None:
            roots.append(path if path.is_dir() else path.parent)
    if report is not None:
        bridge = _source_by_name(report.get("sources", []), "bridge_config")
        if bridge is not None:
            declared = bridge.get("facts", {}).get("declared_paths", {})
            for name in ("runtime_dir", "journal", "spool", "results"):
                value = declared.get(name)
                if isinstance(value, str):
                    candidate = Path(value)
                    roots.append(candidate if name in {"runtime_dir", "spool", "results"} else candidate.parent)
        native_bundle = _source_by_name(report.get("sources", []), "native_host_bundle")
        if native_bundle is not None:
            executable = native_bundle.get("identity", {}).get("executable_path")
            if isinstance(executable, str):
                roots.append(Path(executable).parent)
    return tuple(roots)


def human_summary(report: Mapping[str, Any]) -> str:
    overall = report.get("overall", {})
    lines = [
        f"BDB runtime inventory {report.get('inventory_id')}",
        f"Result: {overall.get('result')}",
        f"Semantic digest: {report.get('semantic_digest')}",
    ]
    for source in report.get("sources", []):
        suffix = " complete" if source.get("complete") else " incomplete"
        lines.append(f"- {source.get('name')}: {source.get('status')}{suffix}")
    blockers = overall.get("blockers", [])
    if blockers:
        lines.append("Blockers: " + ", ".join(str(item.get("code")) for item in blockers))
    lines.append("This R0a artifact is evidence only; it is never a production SAFE decision.")
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bdb-inventory", description="Generate a bounded read-only R0a inventory")
    parser.add_argument("--repository", required=True, type=Path)
    parser.add_argument("--bridge-config", required=True, type=Path)
    parser.add_argument("--native-config", type=Path)
    parser.add_argument("--browser-bundle", type=Path)
    parser.add_argument("--native-manifest", type=Path)
    parser.add_argument("--scratch-dir", type=Path)
    parser.add_argument("--private-report", type=Path)
    parser.add_argument("--sanitized-report", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--json", action="store_true", help="Print the private machine-readable report")
    parser.add_argument("--max-records", type=int, default=100)
    parser.add_argument("--max-file-bytes", type=int, default=2 * 1024 * 1024)
    parser.add_argument("--max-total-bytes", type=int, default=64 * 1024 * 1024)
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if (
            args.private_report is not None
            and args.sanitized_report is not None
            and _comparable(args.private_report.absolute()) == _comparable(args.sanitized_report.absolute())
        ):
            raise ValueError("private and sanitized reports must use separate paths")
        request = InventoryRequest(
            repository_path=args.repository,
            bridge_config_path=args.bridge_config,
            native_config_path=args.native_config,
            browser_bundle_path=args.browser_bundle,
            native_manifest_path=args.native_manifest,
            scratch_dir=args.scratch_dir,
            max_records=args.max_records,
            max_file_bytes=args.max_file_bytes,
            max_total_bytes=args.max_total_bytes,
            timeout_seconds=args.timeout_seconds,
        )
        report = InventoryProvider().collect(request)
        forbidden = report_source_roots(request, report)
        if args.private_report is not None:
            atomic_write_report(report, args.private_report, forbidden_roots=forbidden, overwrite=args.overwrite)
        if args.sanitized_report is not None:
            atomic_write_report(
                sanitize_report(report),
                args.sanitized_report,
                forbidden_roots=forbidden,
                overwrite=args.overwrite,
            )
    except (ValueError, OutputFailure) as exc:
        sys.stderr.write(f"bdb-inventory failed: {_safe_message(exc)}\n")
        return 1
    if args.json:
        sys.stdout.buffer.write(canonical_json_bytes(report))
    else:
        print(human_summary(report))
    result = report["overall"]["result"]
    return {
        OverallResult.READY_FOR_LOCAL_GATE.value: 0,
        OverallResult.INCOMPLETE.value: 2,
        OverallResult.INVALID.value: 3,
        OverallResult.UNSUPPORTED.value: 4,
    }[result]


if __name__ == "__main__":
    raise SystemExit(main())

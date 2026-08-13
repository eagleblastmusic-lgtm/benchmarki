"""External, build-only bootstrap and recovery floor for BDB vNext.

This module deliberately has no activation-pointer writer.  It verifies exact
runtime bundles, takes a coordinated snapshot of declared vNext resources,
runs a bounded health check, and proves recovery into an isolated target.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from bdb_shared.evidence import canonical_json_bytes
from bdb_vnext.composition import (
    RUNTIME_ID,
    VNextCompositionError,
    observe_bundle,
)
from bdb_vnext.control_store import (
    ControlStoreError,
    backup_identity,
    validate_identity_document,
)


BUNDLE_SCHEMA = "bdb-vnext-runtime-bundle-v1"
BACKUP_SCHEMA = "bdb-vnext-backup-manifest-v1"
CONTROL_BACKUP_SCHEMA = "bdb-vnext-backup-manifest-v2"
RESULT_SCHEMA = "bdb-vnext-bootstrap-result-v1"
HEALTH_SCHEMA = "bdb-vnext-health-v1"
RESTORE_RECEIPT_SCHEMA = "bdb-vnext-restore-receipt-v1"

_BUNDLE_MANIFEST_NAME = "bundle.json"
_BACKUP_MANIFEST_NAME = "backup-manifest.json"
_MAX_JSON_BYTES = 1024 * 1024
_MAX_BACKUP_FILES = 4_096
_MAX_BACKUP_FILE_BYTES = 64 * 1024 * 1024
_MAX_BACKUP_TOTAL_BYTES = 512 * 1024 * 1024
_MAX_HEALTH_OUTPUT_BYTES = 64 * 1024
_MAX_HEALTH_TIMEOUT_SECONDS = 120.0
_COPY_CHUNK_BYTES = 1024 * 1024

_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_ATTEMPT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")

BootstrapStatus = Literal["READY", "RECOVERED", "BLOCKED"]
CopyFile = Callable[[Path, Path], None]
PathHook = Callable[[Path], None]


class BootstrapError(RuntimeError):
    """A bounded, operator-visible bootstrap failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class BackupSubject:
    name: str
    kind: Literal["sqlite_db", "sqlite_wal", "directory", "file"]
    relative_path: str
    required: bool = False


DECLARED_VNEXT_BACKUP_SUBJECTS = (
    BackupSubject("control_db", "sqlite_db", "control/control.db"),
    BackupSubject("control_wal", "sqlite_wal", "control/control.db-wal"),
    BackupSubject("content", "directory", "content"),
    BackupSubject("config", "file", "config/bdb-vnext.json"),
)
CONTROL_BACKUP_SUBJECTS = (
    *DECLARED_VNEXT_BACKUP_SUBJECTS[:2],
    # The external seal is an independent fresh-vs-existing authority.  It is
    # present in control-identity (v2) backups; v1 remains backward compatible
    # with the historical four-subject contract.
    BackupSubject("control_seal", "file", "control/control.db.seal.json", required=True),
    *DECLARED_VNEXT_BACKUP_SUBJECTS[2:],
)


def _backup_subjects(*, include_control_identity: bool) -> tuple[BackupSubject, ...]:
    return CONTROL_BACKUP_SUBJECTS if include_control_identity else DECLARED_VNEXT_BACKUP_SUBJECTS


@dataclass(frozen=True)
class RuntimeBundle:
    root: Path
    bundle_id: str
    role: Literal["candidate", "recovery"]
    source_commit: str
    schema_min: int
    schema_max: int
    known_good: bool
    health_entrypoint: str
    sha256: str
    file_count: int
    size_bytes: int

    def supports(self, schema_version: int) -> bool:
        return self.schema_min <= schema_version <= self.schema_max

    def identity_document(self) -> dict[str, Any]:
        return {
            "bundle_id": self.bundle_id,
            "role": self.role,
            "source_commit": self.source_commit,
            "supported_control_schema": {
                "min": self.schema_min,
                "max": self.schema_max,
            },
            "known_good": self.known_good,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class BackupArtifact:
    path: Path
    manifest_sha256: str
    document: dict[str, Any]


@dataclass(frozen=True)
class BootstrapRequest:
    authority_root: Path
    runtime_root: Path
    legacy_runtime_root: Path
    candidate_bundle: Path
    candidate_expected_sha256: str
    recovery_bundle: Path
    recovery_expected_sha256: str
    recovery_target: Path
    required_control_schema: int
    source_is_quiesced: bool
    health_timeout_seconds: float = 10.0
    attempt_id: str | None = None


@dataclass(frozen=True)
class BootstrapResult:
    status: BootstrapStatus
    code: str
    document: dict[str, Any]
    witness_path: Path | None


def _fail(code: str, message: str) -> None:
    raise BootstrapError(code, message)


def _exact_keys(value: Mapping[str, Any], expected: set[str], *, field: str) -> None:
    if set(value) != expected:
        _fail("invalid_contract", f"{field} has unexpected or missing fields")


def _mapping(value: object, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail("invalid_contract", f"{field} must be an object")
    return value


def _valid_schema_version(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2_147_483_647:
        _fail("invalid_contract", f"{field} must be a bounded non-negative integer")
    return value


def _absolute_path(value: str | Path, *, field: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        _fail("relative_path", f"{field} must be absolute")
    return Path(os.path.abspath(path))


def _is_reparse(info: os.stat_result) -> bool:
    attributes = int(getattr(info, "st_file_attributes", 0))
    return bool(attributes & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)))


def _assert_no_reparse_components(path: Path, *, field: str) -> None:
    current = Path(path.anchor)
    parts = path.parts[1:] if path.anchor else path.parts
    for part in parts:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            break
        if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
            _fail("reparse_point", f"{field} contains a symlink or reparse point")


def _contains(child: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath((str(child), str(parent))) == os.path.commonpath(
            (str(parent), str(parent))
        )
    except ValueError:
        return False


def _overlaps(left: Path, right: Path) -> bool:
    return _contains(left, right) or _contains(right, left)


def _safe_relative(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        _fail("invalid_contract", f"{field} must be a non-empty POSIX relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        _fail("path_escape", f"{field} escapes its declared root")
    normalized = path.as_posix()
    if normalized != value:
        _fail("invalid_contract", f"{field} must be normalized")
    return normalized


def _file_token(info: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(info.st_size),
        int(info.st_mtime_ns),
        int(info.st_ctime_ns),
        int(getattr(info, "st_ino", 0)),
    )


def _stable_bytes(path: Path, *, field: str, max_bytes: int = _MAX_JSON_BYTES) -> bytes:
    _assert_no_reparse_components(path, field=field)
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        _fail("missing_file", f"{field} is missing")
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode) or _is_reparse(before):
        _fail("invalid_file", f"{field} must be a regular non-reparse file")
    if before.st_size > max_bytes:
        _fail("file_too_large", f"{field} exceeds its bounded size")
    with path.open("rb") as handle:
        data = handle.read(max_bytes + 1)
    after = os.lstat(path)
    if len(data) > max_bytes:
        _fail("file_too_large", f"{field} exceeds its bounded size")
    if _file_token(before) != _file_token(after):
        _fail("moving_file", f"{field} changed while it was read")
    return data


def _load_json(path: Path, *, field: str) -> dict[str, Any]:
    try:
        value = json.loads(_stable_bytes(path, field=field).decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise BootstrapError("invalid_json", f"{field} is not UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise BootstrapError("invalid_json", f"{field} is not valid JSON") from exc
    if not isinstance(value, dict):
        _fail("invalid_contract", f"{field} must contain an object")
    return value


def _sha256_document(value: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _hash_file(path: Path, *, field: str) -> tuple[int, str, tuple[int, int, int, int]]:
    _assert_no_reparse_components(path, field=field)
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        _fail("missing_file", f"{field} is missing")
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode) or _is_reparse(before):
        _fail("invalid_file", f"{field} must be a regular non-reparse file")
    if before.st_size > _MAX_BACKUP_FILE_BYTES:
        _fail("backup_file_too_large", f"{field} exceeds the per-file backup limit")
    digest = hashlib.sha256()
    observed = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_COPY_CHUNK_BYTES)
            if not chunk:
                break
            observed += len(chunk)
            if observed > _MAX_BACKUP_FILE_BYTES:
                _fail("backup_file_too_large", f"{field} exceeds the per-file backup limit")
            digest.update(chunk)
    after = os.lstat(path)
    if observed != before.st_size or _file_token(before) != _file_token(after):
        _fail("moving_file", f"{field} changed while it was hashed")
    return observed, "sha256:" + digest.hexdigest(), _file_token(after)


def _validate_digest(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        _fail("invalid_digest", f"{field} must be an exact sha256 digest")
    return value


def inspect_runtime_bundle(
    root: str | Path,
    *,
    expected_role: Literal["candidate", "recovery"],
    expected_sha256: str,
    legacy_runtime_root: str | Path,
) -> RuntimeBundle:
    """Bind a bundle descriptor to a stable, exact directory digest."""

    expected = _validate_digest(expected_sha256, field=f"{expected_role}_expected_sha256")
    source = _absolute_path(root, field=f"{expected_role}_bundle")
    if not source.exists():
        _fail(f"{expected_role}_bundle_missing", f"{expected_role} bundle is missing")
    try:
        first = observe_bundle(RUNTIME_ID, source, legacy_runtime_root=legacy_runtime_root)
    except VNextCompositionError as exc:
        code = "legacy_overlap" if exc.code == "legacy_bundle_overlap" else "bundle_observation_failed"
        raise BootstrapError(code, str(exc)) from exc
    except OSError as exc:
        raise BootstrapError("bundle_observation_failed", f"{expected_role} bundle cannot be read") from exc
    if first["kind"] != "directory":
        _fail("invalid_bundle", f"{expected_role} bundle must be a directory")
    if first["sha256"] != expected:
        _fail("bundle_digest_mismatch", f"{expected_role} bundle digest does not match authority input")

    manifest = _load_json(source / _BUNDLE_MANIFEST_NAME, field=f"{expected_role}.bundle.json")
    _exact_keys(
        manifest,
        {
            "schema",
            "runtime_id",
            "bundle_id",
            "role",
            "source_commit",
            "supported_control_schema",
            "known_good",
            "health_entrypoint",
            "activation_policy",
        },
        field=f"{expected_role}.bundle.json",
    )
    if manifest["schema"] != BUNDLE_SCHEMA or manifest["runtime_id"] != RUNTIME_ID:
        _fail("bundle_identity_mismatch", f"{expected_role} bundle has the wrong schema or runtime identity")
    bundle_id = manifest["bundle_id"]
    if not isinstance(bundle_id, str) or _ID.fullmatch(bundle_id) is None:
        _fail("invalid_contract", f"{expected_role} bundle_id is invalid")
    if manifest["role"] != expected_role:
        _fail("bundle_role_mismatch", f"{expected_role} bundle declares a different role")
    source_commit = manifest["source_commit"]
    if not isinstance(source_commit, str) or _COMMIT.fullmatch(source_commit) is None:
        _fail("invalid_contract", f"{expected_role} source_commit is not an exact Git object ID")
    compatibility = _mapping(
        manifest["supported_control_schema"], field=f"{expected_role}.supported_control_schema"
    )
    _exact_keys(compatibility, {"min", "max"}, field=f"{expected_role}.supported_control_schema")
    schema_min = _valid_schema_version(compatibility["min"], field="supported_control_schema.min")
    schema_max = _valid_schema_version(compatibility["max"], field="supported_control_schema.max")
    if schema_min > schema_max:
        _fail("invalid_schema_range", f"{expected_role} schema compatibility range is inverted")
    known_good = manifest["known_good"]
    if not isinstance(known_good, bool):
        _fail("invalid_contract", f"{expected_role}.known_good must be boolean")
    if expected_role == "recovery" and not known_good:
        _fail("recovery_not_known_good", "recovery bundle is not explicitly known-good")
    health_entrypoint = _safe_relative(
        manifest["health_entrypoint"], field=f"{expected_role}.health_entrypoint"
    )
    policy = _mapping(manifest["activation_policy"], field=f"{expected_role}.activation_policy")
    _exact_keys(policy, {"candidate_may_write_final_pointer"}, field=f"{expected_role}.activation_policy")
    if policy["candidate_may_write_final_pointer"] is not False:
        _fail(
            "candidate_self_activation_requested",
            f"{expected_role} bundle requests final activation-pointer authority",
        )
    entrypoint = source.joinpath(*PurePosixPath(health_entrypoint).parts)
    if not entrypoint.is_file():
        _fail("health_entrypoint_missing", f"{expected_role} health entrypoint is missing")

    try:
        second = observe_bundle(RUNTIME_ID, source, legacy_runtime_root=legacy_runtime_root)
    except (OSError, VNextCompositionError) as exc:
        raise BootstrapError("moving_bundle", f"{expected_role} bundle changed during inspection") from exc
    stable_fields = ("kind", "file_count", "size_bytes", "sha256")
    if any(first[field] != second[field] for field in stable_fields):
        _fail("moving_bundle", f"{expected_role} bundle changed during inspection")
    return RuntimeBundle(
        root=source,
        bundle_id=bundle_id,
        role=expected_role,
        source_commit=source_commit,
        schema_min=schema_min,
        schema_max=schema_max,
        known_good=known_good,
        health_entrypoint=health_entrypoint,
        sha256=expected,
        file_count=int(first["file_count"]),
        size_bytes=int(first["size_bytes"]),
    )


class BootstrapLock:
    """Non-blocking OS lock for one external bootstrap authority root."""

    def __init__(self, path: str | Path) -> None:
        self.path = _absolute_path(path, field="bootstrap_lock")
        self._handle: Any | None = None

    def __enter__(self) -> "BootstrapLock":
        _assert_no_reparse_components(self.path, field="bootstrap_lock")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+b")
        except OSError as exc:
            raise BootstrapError("authority_lock_failed", "bootstrap authority lock cannot be opened") from exc
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            handle.close()
            raise BootstrapError("concurrent_attempt", "another bootstrap/recovery attempt owns authority") from exc
        self._handle = handle
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _read_header(path: Path, size: int, *, field: str) -> tuple[bytes, int]:
    _assert_no_reparse_components(path, field=field)
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode) or _is_reparse(before):
        _fail("invalid_file", f"{field} must be a regular non-reparse file")
    with path.open("rb") as handle:
        header = handle.read(size)
    after = os.lstat(path)
    if _file_token(before) != _file_token(after):
        _fail("moving_file", f"{field} changed during SQLite inspection")
    return header, int(after.st_size)


def _sqlite_page_size(path: Path) -> int:
    header, size = _read_header(path, 100, field="control_db")
    if len(header) < 100 or header[:16] != b"SQLite format 3\0":
        _fail("invalid_sqlite_db", "control DB fixture has an invalid or incomplete SQLite header")
    raw = int.from_bytes(header[16:18], "big")
    page_size = 65_536 if raw == 1 else raw
    if page_size < 512 or page_size > 65_536 or page_size & (page_size - 1):
        _fail("invalid_sqlite_db", "control DB fixture declares an invalid page size")
    if size < page_size or size % page_size:
        _fail("incomplete_sqlite_db", "control DB fixture is not an integral SQLite page image")
    return page_size


def _validate_sqlite_wal(path: Path, *, page_size: int) -> None:
    header, size = _read_header(path, 32, field="control_wal")
    if len(header) < 32:
        _fail("incomplete_sqlite_wal", "control WAL fixture has no complete header")
    magic = int.from_bytes(header[:4], "big")
    wal_page_size = int.from_bytes(header[8:12], "big")
    if magic not in {0x377F0682, 0x377F0683}:
        _fail("invalid_sqlite_wal", "control WAL fixture has an invalid magic value")
    if wal_page_size != page_size:
        _fail("db_wal_mismatch", "control DB and WAL page sizes do not match")
    frame_size = 24 + page_size
    if size < 32 or (size - 32) % frame_size:
        _fail("incomplete_sqlite_wal", "control WAL fixture ends inside a frame")


def _subject_path(root: Path, subject: BackupSubject) -> Path:
    relative = _safe_relative(subject.relative_path, field=f"subject[{subject.name}].relative_path")
    target = root.joinpath(*PurePosixPath(relative).parts)
    if not _contains(target, root) or target == root:
        _fail("path_escape", f"backup subject {subject.name} escapes runtime_root")
    return target


def _snapshot_subject(
    root: Path, subject: BackupSubject
) -> tuple[dict[str, Any], tuple[tuple[str, int, str, tuple[int, int, int, int]], ...]]:
    target = _subject_path(root, subject)
    try:
        root_info = os.lstat(target)
    except FileNotFoundError:
        if subject.required:
            _fail("required_subject_missing", f"required backup subject {subject.name} is missing")
        return (
            {
                "name": subject.name,
                "kind": subject.kind,
                "relative_path": subject.relative_path,
                "required": subject.required,
                "state": "declared_absent",
                "files": [],
            },
            (),
        )

    _assert_no_reparse_components(target, field=f"subject[{subject.name}]")
    if stat.S_ISLNK(root_info.st_mode) or _is_reparse(root_info):
        _fail("reparse_point", f"backup subject {subject.name} is a reparse point")
    if subject.kind == "directory":
        if not stat.S_ISDIR(root_info.st_mode):
            _fail("subject_type_mismatch", f"backup subject {subject.name} must be a directory")
        pending = [target]
        members: list[Path] = []
        while pending:
            directory = pending.pop()
            entries = sorted(os.scandir(directory), key=lambda item: item.name.casefold())
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                path = Path(entry.path)
                if entry.is_symlink() or _is_reparse(info):
                    _fail("reparse_point", f"backup subject {subject.name} contains a reparse point")
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode):
                    members.append(path)
                else:
                    _fail("invalid_file", f"backup subject {subject.name} contains an unsupported entry")
    else:
        if not stat.S_ISREG(root_info.st_mode):
            _fail("subject_type_mismatch", f"backup subject {subject.name} must be a file")
        members = [target]

    records: list[dict[str, Any]] = []
    signature: list[tuple[str, int, str, tuple[int, int, int, int]]] = []
    for path in sorted(members, key=lambda item: item.as_posix().casefold()):
        relative = path.relative_to(root).as_posix()
        size, digest, token = _hash_file(path, field=f"subject[{subject.name}]/{relative}")
        records.append({"path": relative, "size_bytes": size, "sha256": digest})
        signature.append((relative, size, digest, token))
    return (
        {
            "name": subject.name,
            "kind": subject.kind,
            "relative_path": subject.relative_path,
            "required": subject.required,
            "state": "present",
            "files": records,
        },
        tuple(signature),
    )


def _snapshot_declared(
    root: Path,
    *,
    include_control_identity: bool = False,
) -> tuple[list[dict[str, Any]], tuple[tuple[str, tuple[Any, ...]], ...]]:
    documents: list[dict[str, Any]] = []
    signatures: list[tuple[str, tuple[Any, ...]]] = []
    file_count = 0
    total_bytes = 0
    for subject in _backup_subjects(include_control_identity=include_control_identity):
        document, signature = _snapshot_subject(root, subject)
        documents.append(document)
        signatures.append((subject.name, signature))
        file_count += len(signature)
        total_bytes += sum(item[1] for item in signature)
        if file_count > _MAX_BACKUP_FILES:
            _fail("backup_file_limit", "declared vNext resources exceed the bounded file count")
        if total_bytes > _MAX_BACKUP_TOTAL_BYTES:
            _fail("backup_size_limit", "declared vNext resources exceed the bounded total size")
    return documents, tuple(signatures)


def _sqlite_pair_document(root: Path, subjects: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_name = {str(subject["name"]): subject for subject in subjects}
    database = by_name["control_db"]
    wal = by_name["control_wal"]
    database_state = database["state"]
    wal_state = wal["state"]
    if wal_state == "present" and database_state != "present":
        _fail("db_wal_mismatch", "control WAL exists without its control DB")
    page_size: int | None = None
    if database_state == "present":
        page_size = _sqlite_page_size(_subject_path(root, DECLARED_VNEXT_BACKUP_SUBJECTS[0]))
    if wal_state == "present":
        assert page_size is not None
        _validate_sqlite_wal(
            _subject_path(root, DECLARED_VNEXT_BACKUP_SUBJECTS[1]), page_size=page_size
        )
    return {
        "database_state": database_state,
        "wal_state": wal_state,
        "page_size": page_size,
    }


def _default_copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as reader, destination.open("xb") as writer:
        while True:
            chunk = reader.read(_COPY_CHUNK_BYTES)
            if not chunk:
                break
            writer.write(chunk)
        writer.flush()
        os.fsync(writer.fileno())


def _copy_verified(
    source: Path,
    destination: Path,
    *,
    expected_size: int,
    expected_sha256: str,
    copy_file: CopyFile | None,
    failure_code: str,
) -> None:
    try:
        (copy_file or _default_copy_file)(source, destination)
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        code = failure_code
        if isinstance(exc, OSError):
            code = "backup_write_failed" if failure_code == "backup_copy_failed" else "restore_write_failed"
        raise BootstrapError(code, f"copy failed before verified publication: {type(exc).__name__}") from exc
    size, digest, _ = _hash_file(destination, field="copied_file")
    if size != expected_size or digest != expected_sha256:
        _fail("copy_integrity_failure", "copied bytes do not match the exact source identity")


def _write_new_json(path: Path, document: Mapping[str, Any]) -> None:
    data = canonical_json_bytes(document)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise BootstrapError("manifest_write_failed", "durable manifest write failed") from exc


def _actual_backup_files(root: Path) -> set[str]:
    files: set[str] = set()
    pending = [root]
    while pending:
        directory = pending.pop()
        for entry in os.scandir(directory):
            info = entry.stat(follow_symlinks=False)
            path = Path(entry.path)
            if entry.is_symlink() or _is_reparse(info):
                _fail("reparse_point", "backup contains a reparse point")
            if stat.S_ISDIR(info.st_mode):
                pending.append(path)
            elif stat.S_ISREG(info.st_mode):
                files.add(path.relative_to(root).as_posix())
            else:
                _fail("invalid_file", "backup contains an unsupported entry")
            if len(files) > _MAX_BACKUP_FILES + 1:
                _fail("backup_file_limit", "backup exceeds the bounded file count")
    return files


def _validate_backup_document(document: Mapping[str, Any]) -> None:
    schema = document.get("schema")
    expected_fields = {
        "schema",
        "runtime_id",
        "backup_id",
        "source_root",
        "required_control_schema",
        "source_quiesced",
        "subjects",
        "sqlite_pair",
        "manifest_sha256",
    }
    if schema == CONTROL_BACKUP_SCHEMA:
        expected_fields.add("control_identity")
    _exact_keys(
        document,
        expected_fields,
        field="backup_manifest",
    )
    if schema not in {BACKUP_SCHEMA, CONTROL_BACKUP_SCHEMA} or document["runtime_id"] != RUNTIME_ID:
        _fail("invalid_backup_manifest", "backup schema or runtime identity is wrong")
    if document["schema"] == CONTROL_BACKUP_SCHEMA:
        if "control_identity" not in document:
            _fail("invalid_backup_manifest", "unified Control DB backup is missing its identity")
        identity = document["control_identity"]
        if not isinstance(identity, Mapping):
            _fail("invalid_backup_manifest", "control_identity must be an object")
    elif "control_identity" in document:
        _fail("invalid_backup_manifest", "v1 backup cannot carry a v2 Control DB identity")
    backup_id = document["backup_id"]
    if not isinstance(backup_id, str) or _ATTEMPT_ID.fullmatch(backup_id) is None:
        _fail("invalid_backup_manifest", "backup_id is invalid")
    _absolute_path(document["source_root"], field="backup.source_root")
    _valid_schema_version(document["required_control_schema"], field="required_control_schema")
    if document["source_quiesced"] is not True:
        _fail("invalid_backup_manifest", "backup does not attest a quiesced source")
    subjects = document["subjects"]
    declarations = _backup_subjects(include_control_identity=document["schema"] == CONTROL_BACKUP_SCHEMA)
    if not isinstance(subjects, list) or len(subjects) != len(declarations):
        _fail("invalid_backup_manifest", "backup subject set is incomplete")
    seen_files: set[str] = set()
    for actual, expected in zip(subjects, declarations, strict=True):
        subject = _mapping(actual, field=f"subject[{expected.name}]")
        _exact_keys(
            subject,
            {"name", "kind", "relative_path", "required", "state", "files"},
            field=f"subject[{expected.name}]",
        )
        identity = (
            subject["name"],
            subject["kind"],
            subject["relative_path"],
            subject["required"],
        )
        if identity != (expected.name, expected.kind, expected.relative_path, expected.required):
            _fail("invalid_backup_manifest", "backup subject identity differs from the frozen declaration")
        if subject["state"] not in {"present", "declared_absent"}:
            _fail("invalid_backup_manifest", "backup subject state is invalid")
        files = subject["files"]
        if not isinstance(files, list) or (subject["state"] == "declared_absent" and files):
            _fail("invalid_backup_manifest", "backup subject file list contradicts its state")
        for index, item in enumerate(files):
            record = _mapping(item, field=f"subject[{expected.name}].files[{index}]")
            _exact_keys(record, {"path", "size_bytes", "sha256"}, field="backup_file")
            relative = _safe_relative(record["path"], field="backup_file.path")
            size = record["size_bytes"]
            if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= _MAX_BACKUP_FILE_BYTES:
                _fail("invalid_backup_manifest", "backup file size is invalid")
            _validate_digest(record["sha256"], field="backup_file.sha256")
            expected_root = PurePosixPath(expected.relative_path)
            member = PurePosixPath(relative)
            if expected.kind == "directory":
                if member == expected_root or expected_root not in member.parents:
                    _fail("invalid_backup_manifest", "directory member escapes its backup subject")
            elif member != expected_root:
                _fail("invalid_backup_manifest", "file member differs from its backup subject")
            if relative in seen_files:
                _fail("invalid_backup_manifest", "backup contains duplicate file identities")
            seen_files.add(relative)
    pair = _mapping(document["sqlite_pair"], field="sqlite_pair")
    _exact_keys(pair, {"database_state", "wal_state", "page_size"}, field="sqlite_pair")
    if pair["database_state"] not in {"present", "declared_absent"} or pair["wal_state"] not in {
        "present",
        "declared_absent",
    }:
        _fail("invalid_backup_manifest", "SQLite pair state is invalid")
    if pair["page_size"] is not None:
        _valid_schema_version(pair["page_size"], field="sqlite_pair.page_size")
    supplied_digest = _validate_digest(document["manifest_sha256"], field="manifest_sha256")
    payload = dict(document)
    payload.pop("manifest_sha256")
    if _sha256_document(payload) != supplied_digest:
        _fail("backup_manifest_digest_mismatch", "backup manifest digest does not match its payload")


def _verify_backup_contents(root: Path, *, require_directory_name: bool) -> BackupArtifact:
    _assert_no_reparse_components(root, field="backup_root")
    if not root.is_dir():
        _fail("backup_missing", "backup directory is missing")
    document = _load_json(root / _BACKUP_MANIFEST_NAME, field="backup_manifest")
    _validate_backup_document(document)
    if require_directory_name and root.name != document["backup_id"]:
        _fail("backup_identity_mismatch", "backup directory name differs from backup_id")
    observed_subjects, _ = _snapshot_declared(
        root, include_control_identity=document["schema"] == CONTROL_BACKUP_SCHEMA
    )
    if observed_subjects != document["subjects"]:
        _fail("backup_integrity_failure", "backup bytes differ from the coordinated manifest")
    observed_pair = _sqlite_pair_document(root, observed_subjects)
    if observed_pair != document["sqlite_pair"]:
        _fail("backup_integrity_failure", "backup DB/WAL identity differs from its manifest")
    expected_files = {
        item["path"]
        for subject in document["subjects"]
        for item in subject["files"]
    }
    expected_files.add(_BACKUP_MANIFEST_NAME)
    if _actual_backup_files(root) != expected_files:
        _fail("backup_integrity_failure", "backup contains missing or foreign files")
    if document["schema"] == CONTROL_BACKUP_SCHEMA:
        try:
            config_subject = next(subject for subject in observed_subjects if subject["name"] == "config")
            config_files = config_subject["files"]
            if config_subject["state"] != "present" or len(config_files) != 1:
                _fail("backup_control_identity_invalid", "unified backup config identity is missing")
            validate_identity_document(
                document["control_identity"],
                config_sha256=str(config_files[0]["sha256"]),
            )
        except ControlStoreError as exc:
            _fail(exc.code, str(exc))
    return BackupArtifact(root, str(document["manifest_sha256"]), dict(document))


def verify_backup(path: str | Path) -> BackupArtifact:
    """Verify a published backup without trusting source runtime state."""

    root = _absolute_path(path, field="backup")
    return _verify_backup_contents(root, require_directory_name=True)


def create_coordinated_backup(
    runtime_root: str | Path,
    backup_root: str | Path,
    *,
    backup_id: str,
    required_control_schema: int,
    source_is_quiesced: bool,
    copy_file: CopyFile | None = None,
    before_publish: PathHook | None = None,
    include_control_identity: bool = False,
) -> BackupArtifact:
    """Publish one all-or-nothing backup directory on the same filesystem."""

    source = _absolute_path(runtime_root, field="runtime_root")
    destination_root = _absolute_path(backup_root, field="backup_root")
    if not source.is_dir():
        _fail("runtime_root_missing", "vNext runtime_root must exist before backup")
    _assert_no_reparse_components(source, field="runtime_root")
    _assert_no_reparse_components(destination_root, field="backup_root")
    if _overlaps(source, destination_root):
        _fail("authority_overlap", "backup authority and candidate runtime roots overlap")
    if not source_is_quiesced:
        _fail("source_not_quiesced", "coordinated backup requires an explicitly quiesced vNext source")
    if not isinstance(backup_id, str) or _ATTEMPT_ID.fullmatch(backup_id) is None:
        _fail("invalid_backup_id", "backup_id is invalid")
    schema_version = _valid_schema_version(required_control_schema, field="required_control_schema")

    if include_control_identity and not (source / "control" / "control.db").is_file():
        _fail("control_identity_unavailable", "unified Control DB is required for a v2 backup")
    if include_control_identity and not (source / "control" / "control.db.seal.json").is_file():
        _fail("control_seal_missing", "control-identity backup requires the external Control DB seal")
    declarations = _backup_subjects(include_control_identity=include_control_identity)
    subjects, first_signature = _snapshot_declared(source, include_control_identity=include_control_identity)
    sqlite_pair = _sqlite_pair_document(source, subjects)
    control_identity: dict[str, Any] | None = None
    if include_control_identity:
        control_path = _subject_path(source, DECLARED_VNEXT_BACKUP_SUBJECTS[0])
        if not control_path.is_file():
            _fail("control_identity_unavailable", "unified Control DB is required for a v2 backup")
        try:
            connection = sqlite3.connect(str(control_path))
            try:
                control_identity = backup_identity(
                    connection,
                    config_path=_subject_path(source, next(item for item in declarations if item.name == "config")),
                )
            finally:
                connection.close()
        except ControlStoreError as exc:
            _fail(exc.code, str(exc))
        except sqlite3.DatabaseError as exc:
            _fail("control_identity_unavailable", "unified Control DB identity could not be read")
    destination_root.mkdir(parents=True, exist_ok=True)
    final = destination_root / backup_id
    if final.exists():
        _fail("backup_exists", "backup_id already exists and will not be overwritten")
    staging = destination_root / f".{backup_id}.partial-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        for subject in subjects:
            if subject["state"] == "present" and subject["kind"] == "directory":
                _subject_path(staging, next(item for item in declarations if item.name == subject["name"])).mkdir(parents=True)
            for record in subject["files"]:
                relative = PurePosixPath(record["path"])
                _copy_verified(
                    source.joinpath(*relative.parts),
                    staging.joinpath(*relative.parts),
                    expected_size=int(record["size_bytes"]),
                    expected_sha256=str(record["sha256"]),
                    copy_file=copy_file,
                    failure_code="backup_copy_failed",
                )
        second_subjects, second_signature = _snapshot_declared(
            source, include_control_identity=include_control_identity
        )
        if first_signature != second_signature or subjects != second_subjects:
            _fail("moving_backup_source", "declared vNext resources changed during coordinated backup")
        payload: dict[str, Any] = {
            "schema": CONTROL_BACKUP_SCHEMA if include_control_identity else BACKUP_SCHEMA,
            "runtime_id": RUNTIME_ID,
            "backup_id": backup_id,
            "source_root": str(source),
            "required_control_schema": schema_version,
            "source_quiesced": True,
            "subjects": subjects,
            "sqlite_pair": sqlite_pair,
        }
        if control_identity is not None:
            payload["control_identity"] = control_identity
        document = {**payload, "manifest_sha256": _sha256_document(payload)}
        _write_new_json(staging / _BACKUP_MANIFEST_NAME, document)
        _verify_backup_contents(staging, require_directory_name=False)
        if before_publish is not None:
            try:
                before_publish(staging)
            except BaseException as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                raise BootstrapError(
                    "backup_publish_interrupted", "backup publication was interrupted before rename"
                ) from exc
            _verify_backup_contents(staging, require_directory_name=False)
        try:
            os.replace(staging, final)
        except OSError as exc:
            raise BootstrapError("backup_publish_failed", "atomic backup publication failed") from exc
    except BaseException:
        # A partial directory is intentionally never promoted or treated as a backup.
        raise
    return _verify_backup_contents(final, require_directory_name=True)


def _verify_restored(root: Path, manifest: Mapping[str, Any]) -> str:
    observed_subjects, _ = _snapshot_declared(
        root, include_control_identity=manifest["schema"] == CONTROL_BACKUP_SCHEMA
    )
    if observed_subjects != manifest["subjects"]:
        _fail("restore_integrity_failure", "restored bytes differ from the exact backup manifest")
    if _sqlite_pair_document(root, observed_subjects) != manifest["sqlite_pair"]:
        _fail("restore_integrity_failure", "restored DB/WAL identity differs from the backup")
    expected_files = {
        item["path"]
        for subject in manifest["subjects"]
        for item in subject["files"]
    }
    if _actual_backup_files(root) != expected_files:
        _fail("restore_integrity_failure", "restored target contains missing or foreign files")
    if manifest["schema"] == CONTROL_BACKUP_SCHEMA:
        try:
            config_subject = next(subject for subject in observed_subjects if subject["name"] == "config")
            config_files = config_subject["files"]
            if config_subject["state"] != "present" or len(config_files) != 1:
                _fail("restore_control_identity_invalid", "restored config identity is missing")
            validate_identity_document(
                manifest["control_identity"],
                config_sha256=str(config_files[0]["sha256"]),
            )
        except ControlStoreError as exc:
            _fail(exc.code, str(exc))
    identity = {
        "backup_manifest_sha256": manifest["manifest_sha256"],
        "subjects": observed_subjects,
        "sqlite_pair": manifest["sqlite_pair"],
    }
    return _sha256_document(identity)


def restore_backup(
    backup_path: str | Path,
    target_root: str | Path,
    *,
    authority_root: str | Path,
    legacy_runtime_root: str | Path,
    forbidden_roots: Sequence[str | Path] = (),
    copy_file: CopyFile | None = None,
    before_publish: PathHook | None = None,
    after_publish: PathHook | None = None,
) -> dict[str, Any]:
    """Restore exact backup bytes into a new isolated target; never overwrite."""

    backup = verify_backup(backup_path)
    target = _absolute_path(target_root, field="restore_target")
    authority = _absolute_path(authority_root, field="authority_root")
    legacy = _absolute_path(legacy_runtime_root, field="legacy_runtime_root")
    source = _absolute_path(backup.document["source_root"], field="backup.source_root")
    roots = [backup.path, authority, source]
    roots.extend(_absolute_path(root, field="forbidden_root") for root in forbidden_roots)
    if _overlaps(target, legacy):
        _fail("legacy_overlap", "restore target overlaps frozen legacy runtime")
    if any(_overlaps(target, root) for root in roots):
        _fail("foreign_state_overlap", "restore target overlaps source, authority, bundle, or foreign state")
    _assert_no_reparse_components(target, field="restore_target")
    if target.exists():
        _fail("restore_target_exists", "restore target already exists and will not be overwritten")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}.restore-partial-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        declarations = _backup_subjects(include_control_identity=backup.document["schema"] == CONTROL_BACKUP_SCHEMA)
        for subject_doc, subject in zip(backup.document["subjects"], declarations, strict=True):
            if subject_doc["state"] == "present" and subject.kind == "directory":
                _subject_path(staging, subject).mkdir(parents=True)
            for record in subject_doc["files"]:
                relative = PurePosixPath(record["path"])
                _copy_verified(
                    backup.path.joinpath(*relative.parts),
                    staging.joinpath(*relative.parts),
                    expected_size=int(record["size_bytes"]),
                    expected_sha256=str(record["sha256"]),
                    copy_file=copy_file,
                    failure_code="restore_copy_failed",
                )
        restore_sha256 = _verify_restored(staging, backup.document)
        if before_publish is not None:
            try:
                before_publish(staging)
            except BaseException as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                raise BootstrapError(
                    "restore_publish_interrupted", "restore publication was interrupted before rename"
                ) from exc
            _verify_restored(staging, backup.document)
        try:
            os.replace(staging, target)
        except OSError as exc:
            raise BootstrapError("restore_publish_failed", "atomic restore publication failed") from exc
        if after_publish is not None:
            try:
                after_publish(target)
            except BaseException as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                raise BootstrapError(
                    "restore_post_publish_failed", "post-publish restore verification hook failed"
                ) from exc
        observed_sha256 = _verify_restored(target, backup.document)
        if observed_sha256 != restore_sha256:
            _fail("restore_integrity_failure", "restored identity changed after atomic publication")
    except BaseException:
        # Never delete an uncertain target; incomplete staging is not a valid restore.
        raise
    return {
        "schema": RESTORE_RECEIPT_SCHEMA,
        "runtime_id": RUNTIME_ID,
        "backup_manifest_sha256": backup.manifest_sha256,
        "target_root": str(target),
        "restore_sha256": restore_sha256,
        "verified": True,
    }


def _observe_bundle_unchanged(bundle: RuntimeBundle, *, legacy_runtime_root: Path) -> None:
    try:
        observed = observe_bundle(
            RUNTIME_ID, bundle.root, legacy_runtime_root=legacy_runtime_root
        )
    except (OSError, VNextCompositionError) as exc:
        raise BootstrapError("moving_bundle", f"{bundle.role} bundle cannot be re-observed") from exc
    if (
        observed["kind"] != "directory"
        or observed["sha256"] != bundle.sha256
        or observed["file_count"] != bundle.file_count
        or observed["size_bytes"] != bundle.size_bytes
    ):
        _fail("moving_bundle", f"{bundle.role} bundle identity changed")


def _bounded_process(
    command: Sequence[str], *, cwd: Path, timeout_seconds: float
) -> tuple[int, bytes, bytes]:
    if not 0 < timeout_seconds <= _MAX_HEALTH_TIMEOUT_SECONDS:
        _fail("invalid_health_timeout", "health timeout must be positive and bounded")
    environment = {
        key: value
        for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
        if (value := os.environ.get(key)) is not None
    }
    creationflags = 0
    if os.name == "nt":
        creationflags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) | int(
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    try:
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            creationflags=creationflags,
        )
    except OSError as exc:
        raise BootstrapError("health_start_failed", "health process could not start") from exc
    assert process.stdout is not None and process.stderr is not None
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    guard = threading.Lock()
    overflow = threading.Event()

    def read_pipe(name: str, pipe: Any) -> None:
        while True:
            chunk = pipe.read(4_096)
            if not chunk:
                return
            with guard:
                remaining = _MAX_HEALTH_OUTPUT_BYTES + 1 - len(buffers[name])
                if remaining > 0:
                    buffers[name].extend(chunk[:remaining])
                if len(buffers[name]) > _MAX_HEALTH_OUTPUT_BYTES:
                    overflow.set()

    threads = [
        threading.Thread(target=read_pipe, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=read_pipe, args=("stderr", process.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout_seconds
    timed_out = False
    while process.poll() is None:
        if overflow.is_set():
            process.kill()
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            process.kill()
            break
        overflow.wait(min(0.025, remaining))
    try:
        return_code = process.wait(timeout=2.0)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        raise BootstrapError("health_process_unbounded", "health process did not terminate after kill") from exc
    for thread in threads:
        thread.join(timeout=2.0)
    if any(thread.is_alive() for thread in threads):
        _fail("health_process_unbounded", "health output readers did not terminate")
    if timed_out:
        _fail("health_timeout", "bounded health check timed out")
    if overflow.is_set():
        _fail("health_output_too_large", "health output exceeded its bounded contract")
    return return_code, bytes(buffers["stdout"]), bytes(buffers["stderr"])


def run_health_check(
    bundle: RuntimeBundle,
    *,
    required_control_schema: int,
    legacy_runtime_root: str | Path,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Run one exact bundle's fixed health entrypoint without shell expansion."""

    schema_version = _valid_schema_version(required_control_schema, field="required_control_schema")
    legacy = _absolute_path(legacy_runtime_root, field="legacy_runtime_root")
    _observe_bundle_unchanged(bundle, legacy_runtime_root=legacy)
    entrypoint = bundle.root.joinpath(*PurePosixPath(bundle.health_entrypoint).parts)
    return_code, stdout, _stderr = _bounded_process(
        (
            sys.executable,
            "-I",
            str(entrypoint),
            "--bdb-vnext-health-check",
            f"--control-schema={schema_version}",
        ),
        cwd=bundle.root,
        timeout_seconds=timeout_seconds,
    )
    _observe_bundle_unchanged(bundle, legacy_runtime_root=legacy)
    if return_code != 0:
        _fail("health_failed", f"{bundle.role} health process returned {return_code}")
    try:
        value = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BootstrapError("invalid_health_output", "health output is not one UTF-8 JSON value") from exc
    response = _mapping(value, field="health_output")
    _exact_keys(
        response,
        {"schema", "status", "runtime_id", "bundle_id", "observed_control_schema"},
        field="health_output",
    )
    expected = {
        "schema": HEALTH_SCHEMA,
        "status": "READY",
        "runtime_id": RUNTIME_ID,
        "bundle_id": bundle.bundle_id,
        "observed_control_schema": schema_version,
    }
    if dict(response) != expected:
        _fail("health_identity_mismatch", "health output is not bound to the exact bundle/schema subject")
    return {**expected, "witness_sha256": _sha256_document(expected)}


def _validate_request_topology(request: BootstrapRequest) -> dict[str, Path]:
    paths = {
        "authority": _absolute_path(request.authority_root, field="authority_root"),
        "runtime": _absolute_path(request.runtime_root, field="runtime_root"),
        "legacy": _absolute_path(request.legacy_runtime_root, field="legacy_runtime_root"),
        "candidate": _absolute_path(request.candidate_bundle, field="candidate_bundle"),
        "recovery": _absolute_path(request.recovery_bundle, field="recovery_bundle"),
        "restore": _absolute_path(request.recovery_target, field="recovery_target"),
    }
    if _overlaps(paths["runtime"], paths["legacy"]):
        _fail("legacy_overlap", "vNext runtime overlaps frozen legacy runtime")
    if _overlaps(paths["authority"], paths["legacy"]):
        _fail("legacy_overlap", "bootstrap authority overlaps frozen legacy runtime")
    isolated = ("runtime", "candidate", "recovery", "restore")
    for name in isolated:
        if _overlaps(paths["authority"], paths[name]):
            _fail("authority_overlap", f"bootstrap authority overlaps {name} state")
    if _overlaps(paths["candidate"], paths["recovery"]):
        _fail("foreign_state_overlap", "candidate and recovery bundles overlap")
    if _overlaps(paths["runtime"], paths["candidate"]) or _overlaps(
        paths["runtime"], paths["recovery"]
    ):
        _fail("foreign_state_overlap", "candidate runtime and immutable bundles overlap")
    if any(
        _overlaps(paths["restore"], paths[name])
        for name in ("legacy", "runtime", "candidate", "recovery")
    ):
        _fail("legacy_overlap" if _overlaps(paths["restore"], paths["legacy"]) else "foreign_state_overlap", "restore target is not isolated")
    for name in ("authority", "runtime", "candidate", "recovery", "restore"):
        _assert_no_reparse_components(paths[name], field=name)
    return paths


def _result_document(
    *,
    attempt_id: str,
    status: BootstrapStatus,
    code: str,
    message: str | None,
    required_control_schema: int,
    candidate: RuntimeBundle | None,
    recovery: RuntimeBundle | None,
    backup: BackupArtifact | None,
    restore: Mapping[str, Any] | None,
    candidate_health: Mapping[str, Any] | None,
    recovery_health: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "schema": RESULT_SCHEMA,
        "runtime_id": RUNTIME_ID,
        "attempt_id": attempt_id,
        "status": status,
        "error": None if message is None else {"code": code, "message": message},
        "required_control_schema": required_control_schema,
        "candidate": None if candidate is None else candidate.identity_document(),
        "recovery": None if recovery is None else recovery.identity_document(),
        "backup": None
        if backup is None
        else {
            "path": str(backup.path),
            "manifest_sha256": backup.manifest_sha256,
        },
        "restore": None if restore is None else dict(restore),
        "health": {
            "candidate": None if candidate_health is None else dict(candidate_health),
            "recovery": None if recovery_health is None else dict(recovery_health),
        },
        "authority": {
            "boundary": "external_bootstrap_root",
            "candidate_may_write_final_pointer": False,
            "final_activation_pointer": None,
            "production_activation_performed": False,
        },
    }


def _write_result_witness(
    authority_root: Path, *, attempt_id: str, document: Mapping[str, Any]
) -> Path:
    attempts = authority_root / "attempts"
    final = attempts / f"{attempt_id}.json"
    if final.exists():
        _fail("attempt_exists", "attempt witness already exists and will not be overwritten")
    staging = attempts / f".{attempt_id}.partial-{uuid.uuid4().hex}.json"
    _write_new_json(staging, document)
    try:
        os.replace(staging, final)
    except OSError as exc:
        raise BootstrapError("authority_manifest_write_failed", "atomic witness publication failed") from exc
    if _load_json(final, field="bootstrap_result") != document:
        _fail("authority_manifest_write_failed", "durable bootstrap witness failed post-write verification")
    return final


def _blocked_without_witness(
    *, attempt_id: str, schema_version: int, error: BootstrapError
) -> BootstrapResult:
    document = _result_document(
        attempt_id=attempt_id,
        status="BLOCKED",
        code=error.code,
        message=str(error),
        required_control_schema=schema_version,
        candidate=None,
        recovery=None,
        backup=None,
        restore=None,
        candidate_health=None,
        recovery_health=None,
    )
    return BootstrapResult("BLOCKED", error.code, document, None)


def execute_bootstrap(
    request: BootstrapRequest,
    *,
    backup_copy_file: CopyFile | None = None,
    backup_before_publish: PathHook | None = None,
    restore_copy_file: CopyFile | None = None,
    restore_before_publish: PathHook | None = None,
    restore_after_publish: PathHook | None = None,
) -> BootstrapResult:
    """Execute the external M1b floor without activating any runtime pointer."""

    attempt_id = request.attempt_id or uuid.uuid4().hex
    if _ATTEMPT_ID.fullmatch(attempt_id) is None:
        error = BootstrapError("invalid_attempt_id", "attempt_id is invalid")
        return _blocked_without_witness(
            attempt_id="invalid-attempt", schema_version=0, error=error
        )
    try:
        schema_version = _valid_schema_version(
            request.required_control_schema, field="required_control_schema"
        )
        paths = _validate_request_topology(request)
    except BootstrapError as error:
        return _blocked_without_witness(
            attempt_id=attempt_id,
            schema_version=request.required_control_schema
            if isinstance(request.required_control_schema, int)
            else 0,
            error=error,
        )

    candidate: RuntimeBundle | None = None
    recovery: RuntimeBundle | None = None
    backup: BackupArtifact | None = None
    restore: Mapping[str, Any] | None = None
    candidate_health: Mapping[str, Any] | None = None
    recovery_health: Mapping[str, Any] | None = None
    status: BootstrapStatus = "BLOCKED"
    code = "bootstrap_blocked"
    message: str | None = None
    try:
        with BootstrapLock(paths["authority"] / "bootstrap.lock"):
            try:
                candidate = inspect_runtime_bundle(
                    paths["candidate"],
                    expected_role="candidate",
                    expected_sha256=request.candidate_expected_sha256,
                    legacy_runtime_root=paths["legacy"],
                )
                recovery = inspect_runtime_bundle(
                    paths["recovery"],
                    expected_role="recovery",
                    expected_sha256=request.recovery_expected_sha256,
                    legacy_runtime_root=paths["legacy"],
                )
                if not candidate.supports(schema_version):
                    _fail("candidate_schema_unsupported", "candidate bundle does not support required schema")
                if not recovery.supports(schema_version):
                    _fail("recovery_schema_unsupported", "known-good recovery bundle does not support required schema")
                recovery_health = run_health_check(
                    recovery,
                    required_control_schema=schema_version,
                    legacy_runtime_root=paths["legacy"],
                    timeout_seconds=request.health_timeout_seconds,
                )
                backup = create_coordinated_backup(
                    paths["runtime"],
                    paths["authority"] / "backups",
                    backup_id=attempt_id,
                    required_control_schema=schema_version,
                    source_is_quiesced=request.source_is_quiesced,
                    copy_file=backup_copy_file,
                    before_publish=backup_before_publish,
                )
                try:
                    candidate_health = run_health_check(
                        candidate,
                        required_control_schema=schema_version,
                        legacy_runtime_root=paths["legacy"],
                        timeout_seconds=request.health_timeout_seconds,
                    )
                except BootstrapError as candidate_error:
                    restore = restore_backup(
                        backup.path,
                        paths["restore"],
                        authority_root=paths["authority"],
                        legacy_runtime_root=paths["legacy"],
                        forbidden_roots=(paths["candidate"], paths["recovery"]),
                        copy_file=restore_copy_file,
                        before_publish=restore_before_publish,
                        after_publish=restore_after_publish,
                    )
                    recovery_health = run_health_check(
                        recovery,
                        required_control_schema=schema_version,
                        legacy_runtime_root=paths["legacy"],
                        timeout_seconds=request.health_timeout_seconds,
                    )
                    status = "RECOVERED"
                    code = candidate_error.code
                    message = str(candidate_error)
                else:
                    status = "READY"
                    code = "ready"
                    message = None
            except BootstrapError as error:
                status = "BLOCKED"
                code = error.code
                message = str(error)
            except OSError as error:
                status = "BLOCKED"
                code = "filesystem_failure"
                message = f"bounded filesystem operation failed: {error.__class__.__name__}"
            except Exception as error:
                status = "BLOCKED"
                code = "bootstrap_internal_error"
                message = f"bounded bootstrap operation failed: {error.__class__.__name__}"

            document = _result_document(
                attempt_id=attempt_id,
                status=status,
                code=code,
                message=message,
                required_control_schema=schema_version,
                candidate=candidate,
                recovery=recovery,
                backup=backup,
                restore=restore,
                candidate_health=candidate_health,
                recovery_health=recovery_health,
            )
            try:
                witness = _write_result_witness(
                    paths["authority"], attempt_id=attempt_id, document=document
                )
            except BootstrapError as error:
                blocked = _result_document(
                    attempt_id=attempt_id,
                    status="BLOCKED",
                    code="authority_manifest_write_failed",
                    message=str(error),
                    required_control_schema=schema_version,
                    candidate=candidate,
                    recovery=recovery,
                    backup=backup,
                    restore=restore,
                    candidate_health=candidate_health,
                    recovery_health=recovery_health,
                )
                return BootstrapResult("BLOCKED", "authority_manifest_write_failed", blocked, None)
            return BootstrapResult(status, code, document, witness)
    except BootstrapError as error:
        return _blocked_without_witness(
            attempt_id=attempt_id, schema_version=schema_version, error=error
        )
    except Exception as error:
        return _blocked_without_witness(
            attempt_id=attempt_id,
            schema_version=schema_version,
            error=BootstrapError(
                "bootstrap_internal_error",
                f"bounded bootstrap operation failed: {error.__class__.__name__}",
            ),
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bdb-vnext-bootstrap",
        description="External build-only BDB vNext bootstrap/recovery floor (never activates a pointer).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="preflight, back up, health-check, and recover if needed")
    for name in (
        "authority-root",
        "runtime-root",
        "legacy-runtime-root",
        "candidate-bundle",
        "candidate-digest",
        "recovery-bundle",
        "recovery-digest",
        "recovery-target",
    ):
        run.add_argument(f"--{name}", required=True)
    run.add_argument("--control-schema", required=True, type=int)
    run.add_argument("--health-timeout", type=float, default=10.0)
    run.add_argument("--attempt-id")
    run.add_argument("--source-quiesced", action="store_true")

    verify = subparsers.add_parser("verify-backup", help="verify one published backup")
    verify.add_argument("--backup", required=True)

    restore = subparsers.add_parser("restore", help="restore one backup into a new isolated target")
    for name in (
        "backup",
        "target",
        "authority-root",
        "legacy-runtime-root",
        "candidate-bundle",
        "recovery-bundle",
    ):
        restore.add_argument(f"--{name}", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "verify-backup":
            artifact = verify_backup(args.backup)
            output: Mapping[str, Any] = artifact.document
            exit_code = 0
        elif args.command == "restore":
            authority = _absolute_path(args.authority_root, field="authority_root")
            with BootstrapLock(authority / "bootstrap.lock"):
                output = restore_backup(
                    args.backup,
                    args.target,
                    authority_root=authority,
                    legacy_runtime_root=args.legacy_runtime_root,
                    forbidden_roots=(args.candidate_bundle, args.recovery_bundle),
                )
            exit_code = 0
        else:
            result = execute_bootstrap(
                BootstrapRequest(
                    authority_root=Path(args.authority_root),
                    runtime_root=Path(args.runtime_root),
                    legacy_runtime_root=Path(args.legacy_runtime_root),
                    candidate_bundle=Path(args.candidate_bundle),
                    candidate_expected_sha256=args.candidate_digest,
                    recovery_bundle=Path(args.recovery_bundle),
                    recovery_expected_sha256=args.recovery_digest,
                    recovery_target=Path(args.recovery_target),
                    required_control_schema=args.control_schema,
                    source_is_quiesced=args.source_quiesced,
                    health_timeout_seconds=args.health_timeout,
                    attempt_id=args.attempt_id,
                )
            )
            output = result.document
            exit_code = {"READY": 0, "RECOVERED": 10, "BLOCKED": 20}[result.status]
    except BootstrapError as error:
        output = {
            "schema": RESULT_SCHEMA,
            "runtime_id": RUNTIME_ID,
            "status": "BLOCKED",
            "error": {"code": error.code, "message": str(error)},
        }
        exit_code = 20
    sys.stdout.buffer.write(canonical_json_bytes(output))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

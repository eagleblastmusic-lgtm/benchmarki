# BDB vNext M2d exact follow-up source context

BENCHMARK_ONLY
This is the same neutral exact source bundle for either arm.

Committed commit: 4b724eda100345969eb236f877dd46f0bb91c0cb
Committed tree: 90ddd52fd997cb67a13767145fd387f7e0ad7141

## SOURCE bdb_vnext/content_store.py
object: 6fada01c12bfec5aaae1100455f8c0c12d04c147
size_bytes: 31409
raw_sha256: sha256:7ccbfc24106d53b03d1af927d14d0d6b81921d6065cf156cfc7cafbb967eebbd
```text
"""Small isolated vNext typed-content and durable-binding primitives.

This is the production-shaped M2b substrate.  It deliberately owns no task,
submission, lifecycle, scheduler, runtime activation or legacy state.  The
content object/ref pair is immutable and the binding store accepts a fragment
only after the exact object has been published and verified.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, NoReturn

from bdb_shared.evidence import canonical_json_bytes
from bdb_vnext.repo_view import CommittedRepoView


CONTENT_STORE_SCHEMA = "bdb-vnext-content-store-v1"
M2B_BINDING_STORE_SCHEMA = "bdb-vnext-m2b-binding-store-v1"
M2B_BINDING_STORE_VERSION = 1
M2B_RUNTIME_CONFIG_SCHEMA = "bdb-vnext-m2b-runtime-config-v1"
CONTEXT_FRAGMENT_SCHEMA = "bdb-vnext-context-fragment-v1"
M2B_SEMANTIC_DOMAIN = "bdb-vnext-m2b-semantic-v1"
X2_SEMANTIC_DOMAIN = "bdb-vnext-x2-semantic-v1"
MAX_CONTENT_BYTES = 8 * 1024 * 1024
MAX_CONTEXT_FRAGMENT_BYTES = 1 * 1024 * 1024
MAX_METADATA_BYTES = 128 * 1024
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+:-]{0,127}$")


class ContentStoreError(ValueError):
    """Typed fail-closed error for M2b content and binding operations."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def _fail(code: str, message: str, *, details: Mapping[str, Any] | None = None) -> NoReturn:
    raise ContentStoreError(code, message, details=details)


def _digest_bytes(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _validate_digest(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        _fail("malformed_content_ref", f"{field} is not a lowercase sha256 digest")
    return value


def _validate_identifier(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        _fail("malformed_content_ref", f"{field} is not a bounded identifier")
    return value


def _validate_oid(value: object, *, field: str, object_format: str) -> str:
    if not isinstance(value, str):
        _fail("malformed_repo_view_binding", f"{field} is not an object ID")
    length = 40 if object_format == "sha1" else 64
    if len(value) != length or any(character not in "0123456789abcdef" for character in value):
        _fail("malformed_repo_view_binding", f"{field} is not a {object_format} object ID")
    return value


def _semantic_representation(content_type: str, schema: str, raw: bytes) -> tuple[str, dict[str, Any]]:
    if (content_type, schema) == ("text/plain", "x2-text-v1"):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ContentStoreError("semantic_decode_failure", "text content is not valid UTF-8") from exc
        return X2_SEMANTIC_DOMAIN, {"encoding": "utf-8", "text": text}
    if (content_type, schema) == ("application/octet-stream", "x2-bytes-v1"):
        return X2_SEMANTIC_DOMAIN, {"base64": base64.b64encode(raw).decode("ascii")}
    if content_type == "text/plain":
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ContentStoreError("semantic_decode_failure", "text content is not valid UTF-8") from exc
        return M2B_SEMANTIC_DOMAIN, {"encoding": "utf-8", "text": text}
    return M2B_SEMANTIC_DOMAIN, {"base64": base64.b64encode(raw).decode("ascii")}


def _semantic_digest(content_type: str, schema: str, raw: bytes) -> str:
    domain, value = _semantic_representation(content_type, schema, raw)
    semantic = {
        "domain": domain,
        "type": content_type,
        "schema": schema,
        "value": value,
    }
    # X2's accepted semantic domains predate the shared evidence helper and
    # intentionally hash canonical JSON without a trailing newline.  Keep
    # that exact byte contract for the two published X2 domains while the
    # versioned M2b domains continue using the repository evidence helper.
    serialized = (
        json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if domain == X2_SEMANTIC_DOMAIN
        else canonical_json_bytes(semantic)
    )
    return _digest_bytes(serialized)


@dataclass(frozen=True)
class ContentRef:
    """The accepted X2 four-field contract, reused without a fixture import."""

    type: str
    schema: str
    semantic_digest: str
    raw_digest: str

    def __post_init__(self) -> None:
        _validate_identifier(self.type, field="type")
        _validate_identifier(self.schema, field="schema")
        _validate_digest(self.semantic_digest, field="semantic_digest")
        _validate_digest(self.raw_digest, field="raw_digest")

    def as_dict(self) -> dict[str, str]:
        return {
            "type": self.type,
            "schema": self.schema,
            "semantic_digest": self.semantic_digest,
            "raw_digest": self.raw_digest,
        }

    to_dict = as_dict

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ContentRef":
        if not isinstance(value, Mapping) or set(value) != {"type", "schema", "semantic_digest", "raw_digest"}:
            _fail("malformed_content_ref", "ContentRef has an unexpected field set")
        return cls(
            _validate_identifier(value["type"], field="type"),
            _validate_identifier(value["schema"], field="schema"),
            _validate_digest(value["semantic_digest"], field="semantic_digest"),
            _validate_digest(value["raw_digest"], field="raw_digest"),
        )


def make_content_ref(content_type: str, schema: str, raw: bytes) -> ContentRef:
    if not isinstance(raw, bytes):
        _fail("invalid_payload", "content payload must be bytes")
    if len(raw) > MAX_CONTENT_BYTES:
        _fail("payload_too_large", "content payload exceeds the bounded content limit")
    content_type = _validate_identifier(content_type, field="type")
    schema = _validate_identifier(schema, field="schema")
    return ContentRef(
        content_type,
        schema,
        _semantic_digest(content_type, schema, raw),
        _digest_bytes(raw),
    )


def verify_content_ref(ref: ContentRef, raw: bytes) -> None:
    if not isinstance(ref, ContentRef) or not isinstance(raw, bytes):
        _fail("content_ref_integrity_failure", "ContentRef verification requires a typed ref and bytes")
    if len(raw) > MAX_CONTENT_BYTES:
        _fail("payload_too_large", "content payload exceeds the bounded content limit")
    if _digest_bytes(raw) != ref.raw_digest:
        _fail("raw_integrity_failure", "content bytes do not match ContentRef.raw_digest")
    if _semantic_digest(ref.type, ref.schema, raw) != ref.semantic_digest:
        _fail("semantic_integrity_failure", "content bytes do not match ContentRef.semantic_digest")


def _contains(child: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath((str(child), str(parent))) == os.path.commonpath((str(parent), str(parent)))
    except ValueError:
        return False


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
            continue
        if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
            _fail("reparse_point", f"{field} contains a symlink or reparse point")


def _absolute_root(value: str | Path) -> Path:
    root = Path(value).expanduser()
    if not root.is_absolute():
        _fail("relative_path", "M2b roots must be absolute")
    _assert_no_reparse_components(root, field="m2b root")
    root.mkdir(parents=True, exist_ok=True)
    resolved = root.resolve(strict=True)
    _assert_no_reparse_components(resolved, field="m2b root")
    return resolved


def _safe_child(root: Path, relative: str, *, field: str) -> Path:
    path = PurePosixPath(relative)
    if path.is_absolute() or not relative or "\\" in relative or any(part in {"", ".", ".."} for part in path.parts):
        _fail("path_escape", f"{field} is not an allowed relative path")
    target = root.joinpath(*path.parts)
    if not _contains(target, root) or target == root:
        _fail("path_escape", f"{field} escapes its isolated root")
    _assert_no_reparse_components(target, field=field)
    return target


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(getattr(info, "st_dev", 0)),
        int(getattr(info, "st_ino", 0)),
        int(getattr(info, "st_size", 0)),
        int(getattr(info, "st_mtime_ns", 0)),
        int(getattr(info, "st_file_attributes", 0)),
    )


def _read_verified(path: Path, *, field: str, max_bytes: int, missing_code: str) -> bytes:
    try:
        before = os.lstat(path)
    except FileNotFoundError as exc:
        _fail(missing_code, f"{field} is missing")
    if stat.S_ISLNK(before.st_mode) or _is_reparse(before):
        _fail("reparse_point", f"{field} is a symlink or reparse point")
    if not stat.S_ISREG(before.st_mode):
        _fail("unexpected_file_type", f"{field} is not a regular file")
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0)) | int(getattr(os, "O_NOFOLLOW", 0))
    try:
        descriptor = os.open(str(path), flags)
    except FileNotFoundError as exc:
        _fail(missing_code, f"{field} disappeared before open")
    except OSError as exc:
        raise ContentStoreError("content_open_failed", f"unable to open {field}") from exc
    try:
        opened = os.fstat(descriptor)
        if _file_identity(opened) != _file_identity(before):
            _fail("path_identity_changed", f"{field} changed before handle acquisition")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            payload = handle.read(max_bytes + 1)
        after_handle = os.fstat(descriptor)
        try:
            after_path = os.lstat(path)
        except FileNotFoundError:
            _fail("path_identity_changed", f"{field} disappeared during read")
        if (
            _file_identity(after_handle) != _file_identity(opened)
            or _file_identity(after_path) != _file_identity(opened)
        ):
            _fail("path_identity_changed", f"{field} changed during handle-bound read")
        if len(payload) > max_bytes:
            _fail("payload_too_large", f"{field} exceeds its bounded read limit")
        return payload
    finally:
        os.close(descriptor)


def _write_fsync(path: Path, payload: bytes) -> None:
    _assert_no_reparse_components(path.parent, field="temporary parent")
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | int(getattr(os, "O_BINARY", 0))
    try:
        descriptor = os.open(str(path), flags, 0o600)
    except FileExistsError as exc:
        raise ContentStoreError("temporary_collision", "temporary publication path already exists") from exc
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class ContentPublication:
    ref: ContentRef
    object_relative_path: str
    ref_relative_path: str
    object_publication: Literal["published", "converged"]
    ref_publication: Literal["published", "converged"]


class ImmutableContentStore:
    """Immutable object/ref store with handle-bound reads and no overwrite."""

    def __init__(self, root: str | Path) -> None:
        self.root = _absolute_root(root)
        self.content_root = self.root / "content"
        self.objects_root = self.content_root / "objects"
        self.refs_root = self.content_root / "refs"
        self.temp_root = self.content_root / "tmp"
        for directory in (self.content_root, self.objects_root, self.refs_root, self.temp_root):
            _assert_no_reparse_components(directory, field="content layout")
            directory.mkdir(parents=True, exist_ok=True)
            _assert_no_reparse_components(directory, field="content layout")

    def object_relative_path(self, ref: ContentRef) -> str:
        _validate_digest(ref.raw_digest, field="raw_digest")
        return f"objects/{ref.raw_digest[7:]}.bin"

    def ref_relative_path(self, ref: ContentRef) -> str:
        _validate_digest(ref.semantic_digest, field="semantic_digest")
        return f"refs/{ref.semantic_digest[7:]}.json"

    def object_path(self, ref: ContentRef) -> Path:
        return _safe_child(self.content_root, self.object_relative_path(ref), field="object path")

    def ref_path(self, ref: ContentRef) -> Path:
        return _safe_child(self.content_root, self.ref_relative_path(ref), field="ref path")

    def _temporary_path(self, prefix: str) -> Path:
        return _safe_child(
            self.content_root,
            f"tmp/{prefix}-{hashlib.sha256(os.urandom(32)).hexdigest()}.partial",
            field="temporary path",
        )

    def _publish_immutable(self, temporary: Path, target: Path, expected: bytes, *, field: str) -> Literal["published", "converged"]:
        _assert_no_reparse_components(target.parent, field=f"{field} parent")
        if os.path.lexists(target):
            observed = _read_verified(target, field=field, max_bytes=MAX_CONTENT_BYTES, missing_code="raw_object_missing")
            if observed != expected:
                _fail("immutable_conflict", f"{field} already contains different bytes")
            temporary.unlink(missing_ok=True)
            return "converged"
        try:
            os.link(temporary, target, follow_symlinks=False)
        except FileExistsError:
            observed = _read_verified(target, field=field, max_bytes=MAX_CONTENT_BYTES, missing_code="raw_object_missing")
            if observed != expected:
                _fail("immutable_conflict", f"{field} won a race with different bytes")
            temporary.unlink(missing_ok=True)
            return "converged"
        except OSError as exc:
            raise ContentStoreError("atomic_publish_failed", f"atomic immutable publication failed for {field}") from exc
        temporary.unlink(missing_ok=True)
        return "published"

    def publish(self, ref: ContentRef, raw: bytes) -> ContentPublication:
        if not isinstance(ref, ContentRef):
            _fail("invalid_content_ref", "publish requires ContentRef")
        verify_content_ref(ref, raw)
        object_path = self.object_path(ref)
        ref_path = self.ref_path(ref)
        object_temp = self._temporary_path("object")
        _write_fsync(object_temp, raw)
        object_publication = self._publish_immutable(object_temp, object_path, raw, field="committed object")
        metadata = canonical_json_bytes(
            {
                "schema": CONTENT_STORE_SCHEMA,
                "content_ref": ref.as_dict(),
                "object_relative_path": self.object_relative_path(ref),
            }
        )
        ref_temp = self._temporary_path("ref")
        _write_fsync(ref_temp, metadata)
        ref_publication = self._publish_immutable(ref_temp, ref_path, metadata, field="committed ref")
        self.resolve(ref)
        return ContentPublication(
            ref,
            self.object_relative_path(ref),
            self.ref_relative_path(ref),
            object_publication,
            ref_publication,
        )

    def resolve(self, ref: ContentRef) -> bytes:
        if not isinstance(ref, ContentRef):
            _fail("invalid_content_ref", "resolve requires ContentRef")
        ref_path = self.ref_path(ref)
        try:
            metadata_bytes = _read_verified(
                ref_path,
                field="committed ref",
                max_bytes=MAX_METADATA_BYTES,
                missing_code="content_ref_missing",
            )
        except ContentStoreError as exc:
            if exc.code == "content_ref_missing" and os.path.lexists(self.object_path(ref)):
                _fail("content_ref_not_committed", "object exists without an authoritative committed ref")
            raise
        try:
            document = json.loads(metadata_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContentStoreError("malformed_content_ref", "committed ref is not valid JSON") from exc
        if not isinstance(document, dict) or set(document) != {"schema", "content_ref", "object_relative_path"}:
            _fail("malformed_content_ref", "committed ref has the wrong field set")
        if document["schema"] != CONTENT_STORE_SCHEMA:
            _fail("content_store_schema_mismatch", "committed ref schema is unsupported")
        stored = ContentRef.from_mapping(document["content_ref"])
        if stored != ref:
            _fail("content_ref_identity_mismatch", "requested ContentRef differs from committed ref")
        if document["object_relative_path"] != self.object_relative_path(ref):
            _fail("content_metadata_path_mismatch", "committed ref object path is not raw-digest derived")
        raw = _read_verified(
            self.object_path(ref),
            field="committed object",
            max_bytes=MAX_CONTENT_BYTES,
            missing_code="raw_object_missing",
        )
        verify_content_ref(ref, raw)
        return raw


@dataclass(frozen=True)
class RepoViewBinding:
    """Exact repository/commit/tree authority carried by a context fragment."""

    repository_id: str
    repository_identity_digest: str
    object_format: str
    commit_oid: str
    tree_oid: str
    view_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.repository_id, str) or not self.repository_id:
            _fail("malformed_repo_view_binding", "repository_id is required")
        _validate_digest(self.repository_identity_digest, field="repository_identity_digest")
        if self.object_format not in {"sha1", "sha256"}:
            _fail("malformed_repo_view_binding", "object_format is unsupported")
        _validate_oid(self.commit_oid, field="commit_oid", object_format=self.object_format)
        _validate_oid(self.tree_oid, field="tree_oid", object_format=self.object_format)
        _validate_digest(self.view_id, field="view_id")

    @classmethod
    def from_view(cls, view: CommittedRepoView) -> "RepoViewBinding":
        if not isinstance(view, CommittedRepoView):
            _fail("invalid_repo_view", "M2b requires an exact CommittedRepoView")
        view._authoritative_reader()
        return cls(
            view.repository_id,
            view.repository_identity_digest,
            view.object_format,
            view.commit_oid,
            view.tree_oid,
            view.view_id,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RepoViewBinding":
        required = {
            "repository_id",
            "repository_identity_digest",
            "object_format",
            "commit_oid",
            "tree_oid",
            "view_id",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            _fail("malformed_repo_view_binding", "RepoView binding has the wrong field set")
        return cls(*(value[key] for key in (
            "repository_id",
            "repository_identity_digest",
            "object_format",
            "commit_oid",
            "tree_oid",
            "view_id",
        )))

    def as_dict(self) -> dict[str, str]:
        return {
            "repository_id": self.repository_id,
            "repository_identity_digest": self.repository_identity_digest,
            "object_format": self.object_format,
            "commit_oid": self.commit_oid,
            "tree_oid": self.tree_oid,
            "view_id": self.view_id,
        }

    def matches_view(self, view: CommittedRepoView) -> bool:
        return self == RepoViewBinding.from_view(view)


def _fragment_identity_digest(identity: Mapping[str, Any]) -> str:
    return _digest_bytes(canonical_json_bytes(identity))


@dataclass(frozen=True)
class TypedContextFragment:
    """Deterministic typed fragment metadata; bytes remain in ContentStore."""

    fragment_id: str
    fragment_type: str
    fragment_schema: str
    content_ref: ContentRef
    repo_view: RepoViewBinding
    payload_size_bytes: int

    def __post_init__(self) -> None:
        _validate_digest(self.fragment_id, field="fragment_id")
        _validate_identifier(self.fragment_type, field="fragment_type")
        _validate_identifier(self.fragment_schema, field="fragment_schema")
        if not isinstance(self.content_ref, ContentRef) or not isinstance(self.repo_view, RepoViewBinding):
            _fail("malformed_fragment", "fragment ContentRef and RepoView binding are typed objects")
        if not isinstance(self.payload_size_bytes, int) or isinstance(self.payload_size_bytes, bool):
            _fail("malformed_fragment", "payload_size_bytes must be an integer")
        if not 0 <= self.payload_size_bytes <= MAX_CONTEXT_FRAGMENT_BYTES:
            _fail("payload_too_large", "fragment exceeds the bounded payload limit")
        if self.fragment_id != _fragment_identity_digest(self._identity_payload()):
            _fail("fragment_integrity_failure", "fragment_id does not match exact fragment identity")

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "fragment_type": self.fragment_type,
            "fragment_schema": self.fragment_schema,
            "content_ref": self.content_ref.as_dict(),
            "repo_view": self.repo_view.as_dict(),
            "payload_size_bytes": self.payload_size_bytes,
        }

    @classmethod
    def create(
        cls,
        view: CommittedRepoView,
        content_ref: ContentRef,
        *,
        fragment_type: str,
        fragment_schema: str,
        payload_size_bytes: int,
    ) -> "TypedContextFragment":
        if not isinstance(content_ref, ContentRef):
            _fail("invalid_content_ref", "typed fragment creation requires ContentRef")
        binding = RepoViewBinding.from_view(view)
        identity = {
            "fragment_type": _validate_identifier(fragment_type, field="fragment_type"),
            "fragment_schema": _validate_identifier(fragment_schema, field="fragment_schema"),
            "content_ref": content_ref.as_dict(),
            "repo_view": binding.as_dict(),
            "payload_size_bytes": payload_size_bytes,
        }
        return cls(
            _fragment_identity_digest(identity),
            identity["fragment_type"],
            identity["fragment_schema"],
            content_ref,
            binding,
            payload_size_bytes,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "TypedContextFragment":
        required = {
            "schema",
            "fragment_id",
            "fragment_type",
            "fragment_schema",
            "content_ref",
            "repo_view",
            "payload_size_bytes",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            _fail("malformed_fragment", "typed fragment has the wrong field set")
        if value["schema"] != CONTEXT_FRAGMENT_SCHEMA:
            _fail("fragment_schema_mismatch", "typed fragment schema is unsupported")
        return cls(
            value["fragment_id"],
            value["fragment_type"],
            value["fragment_schema"],
            ContentRef.from_mapping(value["content_ref"]),
            RepoViewBinding.from_mapping(value["repo_view"]),
            value["payload_size_bytes"],
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": CONTEXT_FRAGMENT_SCHEMA,
            "fragment_id": self.fragment_id,
            "fragment_type": self.fragment_type,
            "fragment_schema": self.fragment_schema,
            "content_ref": self.content_ref.as_dict(),
            "repo_view": self.repo_view.as_dict(),
            "payload_size_bytes": self.payload_size_bytes,
        }

    to_dict = as_dict

    def to_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    def verify_payload(self, raw: bytes) -> None:
        if len(raw) != self.payload_size_bytes:
            _fail("fragment_length_mismatch", "payload length differs from typed fragment metadata")
        verify_content_ref(self.content_ref, raw)


@dataclass(frozen=True)
class AcceptedBinding:
    fragment: TypedContextFragment
    raw: bytes
    publication: Literal["published", "converged"]


class DurableBindingStore:
    """Minimal isolated SQLite binding substrate, not the future Control Store."""

    def __init__(self, root: str | Path, *, content_store: ImmutableContentStore | None = None) -> None:
        self.root = _absolute_root(root)
        self.content_store = content_store or ImmutableContentStore(self.root)
        if self.content_store.root != self.root:
            _fail("authority_overlap", "binding store and content store must share one isolated root")
        self.database_path = self.root / "control" / "control.db"
        self.config_path = self.root / "config" / "bdb-vnext.json"
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        _assert_no_reparse_components(self.database_path.parent, field="binding database parent")
        _assert_no_reparse_components(self.database_path, field="binding database")
        _assert_no_reparse_components(self.config_path.parent, field="binding config parent")
        self._ensure_config()
        self._connection = sqlite3.connect(str(self.database_path))
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS m2b_accepted_bindings ("
            "fragment_id TEXT PRIMARY KEY, binding_json BLOB NOT NULL"
            ")"
        )
        self._connection.commit()

    def _ensure_config(self) -> None:
        document = {
            "schema": M2B_RUNTIME_CONFIG_SCHEMA,
            "binding_store_schema": M2B_BINDING_STORE_SCHEMA,
            "binding_store_version": M2B_BINDING_STORE_VERSION,
        }
        expected = canonical_json_bytes(document)
        if self.config_path.exists():
            actual = _read_verified(
                self.config_path,
                field="binding config",
                max_bytes=MAX_METADATA_BYTES,
                missing_code="binding_config_missing",
            )
            if actual != expected:
                _fail("binding_config_mismatch", "M2b binding config identity differs")
            return
        _write_fsync(self.config_path, expected)

    def accept(self, fragment: TypedContextFragment, *, view: CommittedRepoView) -> AcceptedBinding:
        if not isinstance(fragment, TypedContextFragment):
            _fail("invalid_fragment", "binding acceptance requires TypedContextFragment")
        if not isinstance(view, CommittedRepoView):
            _fail("invalid_repo_view", "binding acceptance requires CommittedRepoView")
        expected_binding = RepoViewBinding.from_view(view)
        if fragment.repo_view != expected_binding:
            _fail("repo_view_binding_mismatch", "fragment is bound to a different exact RepoView")
        raw = self.content_store.resolve(fragment.content_ref)
        fragment.verify_payload(raw)
        document = fragment.to_json_bytes()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._connection.execute(
                "SELECT binding_json FROM m2b_accepted_bindings WHERE fragment_id = ?",
                (fragment.fragment_id,),
            ).fetchone()
            if row is not None:
                if bytes(row[0]) != document:
                    _fail("conflicting_binding", "fragment identity already has different accepted metadata")
                self._connection.commit()
                return AcceptedBinding(fragment, raw, "converged")
            self._connection.execute(
                "INSERT INTO m2b_accepted_bindings(fragment_id, binding_json) VALUES (?, ?)",
                (fragment.fragment_id, document),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return AcceptedBinding(fragment, raw, "published")

    def resolve_accepted(
        self,
        fragment_id: str,
        *,
        expected_view: CommittedRepoView | None = None,
    ) -> AcceptedBinding:
        _validate_digest(fragment_id, field="fragment_id")
        row = self._connection.execute(
            "SELECT binding_json FROM m2b_accepted_bindings WHERE fragment_id = ?",
            (fragment_id,),
        ).fetchone()
        if row is None:
            _fail("binding_missing", "fragment has no accepted durable binding")
        stored_bytes = (
            bytes(row[0])
            if isinstance(row[0], (bytes, bytearray, memoryview))
            else str(row[0]).encode("utf-8")
        )
        try:
            fragment = TypedContextFragment.from_mapping(json.loads(stored_bytes.decode("utf-8")))
        except ContentStoreError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContentStoreError("binding_corrupt", "durable binding is not valid JSON") from exc
        if fragment.fragment_id != fragment_id:
            _fail("binding_integrity_failure", "durable binding key differs from fragment identity")
        if expected_view is not None:
            if not fragment.repo_view.matches_view(expected_view):
                _fail("repo_view_binding_mismatch", "durable binding is bound to a different RepoView")
        raw = self.content_store.resolve(fragment.content_ref)
        fragment.verify_payload(raw)
        return AcceptedBinding(fragment, raw, "converged")

    assert_accepted = resolve_accepted

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "DurableBindingStore":
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()


__all__ = [
    "AcceptedBinding",
    "CONTEXT_FRAGMENT_SCHEMA",
    "CONTENT_STORE_SCHEMA",
    "ContentPublication",
    "ContentRef",
    "ContentStoreError",
    "DurableBindingStore",
    "ImmutableContentStore",
    "MAX_CONTENT_BYTES",
    "MAX_CONTEXT_FRAGMENT_BYTES",
    "M2B_BINDING_STORE_SCHEMA",
    "M2B_BINDING_STORE_VERSION",
    "RepoViewBinding",
    "TypedContextFragment",
    "make_content_ref",
    "verify_content_ref",
]

```

## SOURCE schemas/bdb-vnext-context-package-v1.schema.json
object: a00bf1d5b8194838961f403f2ab05162f493018c
size_bytes: 7747
raw_sha256: sha256:ca21c12300d4021a2652688ecf318546c2048a14810a74fc3bc3e3971f4415e9
```text
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "urn:bdb:schema:vnext-context-package:v1",
  "title": "BDB vNext ContextPackage v1",
  "type": "object",
  "additionalProperties": false,
  "required": ["schema", "package_id", "intent_basis", "repo_view", "understanding_id", "horizon", "requested_dimensions", "covered_dimensions", "must_see_categories", "covered_must_see", "coverage_bindings", "unknowns", "omissions", "contradictions", "included_fragment_ids", "affordances", "architecture_constraints_included", "invalidation_predicates", "coverage_status", "policy_version", "producer_id", "producer_version", "fact_claim_ids", "inference_claim_ids", "assumption_claim_ids", "hypothesis_claim_ids"],
  "properties": {
    "schema": { "const": "bdb-vnext-context-package-v1" }, "package_id": { "$ref": "#/$defs/sha256" },
    "intent_basis": { "$ref": "#/$defs/intent" }, "repo_view": { "$ref": "#/$defs/repoView" },
    "understanding_id": { "$ref": "#/$defs/sha256" }, "horizon": { "enum": ["LOCAL", "COMPONENT", "REPOSITORY"] },
    "requested_dimensions": { "$ref": "#/$defs/identifiers" }, "covered_dimensions": { "$ref": "#/$defs/identifiers" },
    "must_see_categories": { "$ref": "#/$defs/identifiers" }, "covered_must_see": { "$ref": "#/$defs/identifiers" },
    "coverage_bindings": { "type": "array", "items": { "$ref": "#/$defs/coverageBinding" }, "uniqueItems": true },
    "unknowns": { "type": "array", "items": { "$ref": "#/$defs/unknown" } },
    "omissions": { "type": "array", "items": { "$ref": "#/$defs/omission" } },
    "contradictions": { "type": "array", "items": { "$ref": "#/$defs/contradiction" } },
    "included_fragment_ids": { "type": "array", "items": { "$ref": "#/$defs/sha256" }, "uniqueItems": true },
    "affordances": { "type": "array", "items": { "$ref": "#/$defs/affordance" } },
    "architecture_constraints_included": { "$ref": "#/$defs/identifiers" },
    "invalidation_predicates": { "$ref": "#/$defs/nonEmptyIdentifiers" },
    "coverage_status": { "enum": ["COMPLETE", "PARTIAL", "BLOCKED"] },
    "policy_version": { "$ref": "#/$defs/identifier" }, "producer_id": { "$ref": "#/$defs/identifier" }, "producer_version": { "$ref": "#/$defs/identifier" },
    "fact_claim_ids": { "$ref": "#/$defs/digests" }, "inference_claim_ids": { "$ref": "#/$defs/digests" }, "assumption_claim_ids": { "$ref": "#/$defs/digests" }, "hypothesis_claim_ids": { "$ref": "#/$defs/digests" }
  },
  "$defs": {
    "sha256": { "type": "string", "pattern": "^sha256:[0-9a-f]{64}$" },
    "sha40": { "type": "string", "pattern": "^[0-9a-f]{40}$" }, "sha256Oid": { "type": "string", "pattern": "^[0-9a-f]{64}$" },
    "identifier": { "type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._/+:-]{0,127}$" },
    "identifiers": { "type": "array", "items": { "$ref": "#/$defs/identifier" }, "uniqueItems": true }, "digests": { "type": "array", "items": { "$ref": "#/$defs/sha256" }, "uniqueItems": true },
    "nonEmptyIdentifiers": { "type": "array", "minItems": 1, "items": { "$ref": "#/$defs/identifier" }, "uniqueItems": true },
    "text": { "type": "string", "minLength": 1, "maxLength": 4096 },
    "intent": {
      "type": "object", "additionalProperties": false, "required": ["task_id", "intent_revision", "intent_digest"],
      "properties": { "task_id": { "$ref": "#/$defs/text" }, "intent_revision": { "$ref": "#/$defs/identifier" }, "intent_digest": { "$ref": "#/$defs/sha256" } }
    },
    "repoView": {
      "type": "object", "additionalProperties": false, "required": ["repository_id", "repository_identity_digest", "object_format", "commit_oid", "tree_oid", "view_id"],
      "properties": {
        "repository_id": { "$ref": "#/$defs/text" }, "repository_identity_digest": { "$ref": "#/$defs/sha256" }, "object_format": { "enum": ["sha1", "sha256"] },
        "commit_oid": { "oneOf": [{ "$ref": "#/$defs/sha40" }, { "$ref": "#/$defs/sha256Oid" }] }, "tree_oid": { "oneOf": [{ "$ref": "#/$defs/sha40" }, { "$ref": "#/$defs/sha256Oid" }] }, "view_id": { "$ref": "#/$defs/sha256" }
      }
    },
    "coverageBinding": {
      "type": "object", "additionalProperties": false,
      "required": ["schema", "coverage_binding_id", "repo_view", "target_kind", "target", "supporting_claim_ids", "supporting_fragment_ids"],
      "properties": {
        "schema": { "const": "bdb-vnext-coverage-binding-v1" }, "coverage_binding_id": { "$ref": "#/$defs/sha256" },
        "repo_view": { "$ref": "#/$defs/repoView" }, "target_kind": { "enum": ["DIMENSION", "MUST_SEE"] }, "target": { "$ref": "#/$defs/identifier" },
        "supporting_claim_ids": { "$ref": "#/$defs/digests" }, "supporting_fragment_ids": { "$ref": "#/$defs/digests" }
      }
    },
    "unknown": {
      "type": "object", "additionalProperties": false, "required": ["schema", "unknown_id", "repo_view", "subject", "dimension", "reason", "material", "producer_id", "producer_version"],
      "properties": {
        "schema": { "const": "bdb-vnext-understanding-unknown-v1" }, "unknown_id": { "$ref": "#/$defs/sha256" }, "repo_view": { "$ref": "#/$defs/repoView" }, "subject": { "$ref": "#/$defs/text" }, "dimension": { "$ref": "#/$defs/identifier" }, "reason": { "$ref": "#/$defs/text" }, "material": { "type": "boolean" }, "producer_id": { "$ref": "#/$defs/identifier" }, "producer_version": { "$ref": "#/$defs/identifier" }
      }
    },
    "omission": {
      "type": "object", "additionalProperties": false, "required": ["schema", "omission_id", "repo_view", "dimension", "reason", "policy_denied", "producer_id", "producer_version"],
      "properties": {
        "schema": { "const": "bdb-vnext-context-omission-v1" }, "omission_id": { "$ref": "#/$defs/sha256" }, "repo_view": { "$ref": "#/$defs/repoView" }, "dimension": { "$ref": "#/$defs/identifier" }, "reason": { "$ref": "#/$defs/text" }, "policy_denied": { "type": "boolean" }, "producer_id": { "$ref": "#/$defs/identifier" }, "producer_version": { "$ref": "#/$defs/identifier" }
      }
    },
    "contradiction": {
      "type": "object", "additionalProperties": false, "required": ["schema", "contradiction_id", "repo_view", "subject", "dimension", "claim_ids", "source_claim_ids", "derived_claim_ids", "reason", "source_authority", "producer_id", "producer_version"],
      "properties": {
        "schema": { "const": "bdb-vnext-understanding-contradiction-v1" }, "contradiction_id": { "$ref": "#/$defs/sha256" }, "repo_view": { "$ref": "#/$defs/repoView" }, "subject": { "$ref": "#/$defs/text" }, "dimension": { "$ref": "#/$defs/identifier" }, "claim_ids": { "type": "array", "minItems": 2, "items": { "$ref": "#/$defs/sha256" }, "uniqueItems": true }, "source_claim_ids": { "type": "array", "items": { "$ref": "#/$defs/sha256" }, "uniqueItems": true }, "derived_claim_ids": { "type": "array", "items": { "$ref": "#/$defs/sha256" }, "uniqueItems": true }, "reason": { "$ref": "#/$defs/text" }, "source_authority": { "enum": ["EXACT_SOURCE_WINS", "NO_EXACT_SOURCE_CLAIM"] }, "producer_id": { "$ref": "#/$defs/identifier" }, "producer_version": { "$ref": "#/$defs/identifier" }
      }
    },
    "affordance": {
      "type": "object", "additionalProperties": false, "required": ["schema", "affordance_id", "dimension", "horizon", "evidence_type", "reason", "producer_id", "producer_version"],
      "properties": {
        "schema": { "const": "bdb-vnext-context-affordance-v1" }, "affordance_id": { "$ref": "#/$defs/sha256" }, "dimension": { "$ref": "#/$defs/identifier" }, "horizon": { "enum": ["LOCAL", "COMPONENT", "REPOSITORY"] }, "evidence_type": { "$ref": "#/$defs/identifier" }, "reason": { "$ref": "#/$defs/text" }, "producer_id": { "$ref": "#/$defs/identifier" }, "producer_version": { "$ref": "#/$defs/identifier" }
      }
    }
  }
}

```

## SOURCE tests/test_vnext_engineering_intelligence.py
object: 2b0cf718cc2d38edf76d8eafe0d18739fc2cb6ea
size_bytes: 25942
raw_sha256: sha256:8acf23fef9d892b714bfd66b3745cece6e593f2e84a7f1d8a587a7bb4e92ca43
```text
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from bdb_shared.evidence import semantic_digest
from bdb_vnext.content_store import DurableBindingStore, ImmutableContentStore, TypedContextFragment, make_content_ref
from bdb_vnext.engineering_intelligence import (
    ClaimContradiction,
    ContextPackage,
    ContextRequest,
    ContextResolution,
    CoverageBinding,
    DecisionOption,
    EngineeringDecision,
    EngineeringIntelligenceError,
    GapResolutionEvidence,
    IntentBasis,
    Omission,
    RepoSourceEvidence,
    RepositoryUnderstandingView,
    SourceEvidenceRef,
    UnderstandingClaim,
    Unknown,
    publish_repo_source_evidence,
    reconstruct_semantic_record,
    transport_semantic_record,
    validate_decision_applicability,
)
from bdb_vnext.repo_view import RepositoryResource


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)
    return completed.stdout.strip()


def _fixture(tmp_path: Path) -> tuple[Path, object, object, IntentBasis]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.name", "M2c Test")
    _git(repo, "config", "user.email", "m2c@example.invalid")
    (repo / "README.md").write_text("M2c fixture\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "service.py").write_text("OWNER = 'A'\n", encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "recovery.txt").write_text("RECOVERY = 'A'\n", encoding="utf-8")
    _git(repo, "add", "--all")
    _git(repo, "commit", "-qm", "M2c fixture")
    resource = RepositoryResource.from_path(repo, repository_id="m2c-fixture")
    view = resource.resolve_committed("refs/heads/main", observed_at="2026-08-10T00:00:00Z")
    intent = IntentBasis(
        "external-task:m2c-1",
        "r1",
        semantic_digest({"intent": "understand ownership and recovery", "constraints": ["read-only"]}),
    )
    return repo, resource, view, intent


def _accepted_fragment(tmp_path: Path, view: object, label: str) -> tuple[ImmutableContentStore, DurableBindingStore, TypedContextFragment, SourceEvidenceRef]:
    runtime = tmp_path / f"runtime-{label}"
    content_store = ImmutableContentStore(runtime)
    binding_store = DurableBindingStore(runtime, content_store=content_store)
    raw = f"source evidence: {label}\n".encode("utf-8")
    content_ref = make_content_ref("text/plain", "m2c-source-v1", raw)
    content_store.publish(content_ref, raw)
    fragment = TypedContextFragment.create(
        view,
        content_ref,
        fragment_type="text/plain",
        fragment_schema="m2c-source-v1",
        payload_size_bytes=len(raw),
    )
    binding_store.accept(fragment, view=view)
    evidence = SourceEvidenceRef.create_verified(view, fragment, binding_store)
    return content_store, binding_store, fragment, evidence


def _repo_source(
    tmp_path: Path,
    view: object,
    path: str,
    *,
    content_store: ImmutableContentStore | None = None,
    binding_store: DurableBindingStore | None = None,
) -> tuple[ImmutableContentStore, DurableBindingStore, TypedContextFragment, RepoSourceEvidence]:
    if content_store is None or binding_store is None:
        runtime = tmp_path / "runtime-repo-source"
        content_store = ImmutableContentStore(runtime)
        binding_store = DurableBindingStore(runtime, content_store=content_store)
    evidence = publish_repo_source_evidence(
        view,
        path,
        content_store,
        binding_store,
        fragment_type="text/plain",
        fragment_schema="m2c-repo-source-v1",
    )
    fragment = binding_store.resolve_accepted(evidence.fragment_id, expected_view=view).fragment
    return content_store, binding_store, fragment, evidence


def _seeded(tmp_path: Path):
    repo, resource, view, intent = _fixture(tmp_path)
    content_store, binding_store, ownership_fragment, ownership_evidence = _repo_source(tmp_path, view, "src/service.py")
    ownership = UnderstandingClaim.create(
        view,
        subject="src/service.py",
        dimension="ownership",
        kind="FACT",
        statement="component ownership is explicit in the committed source",
        source_evidence=[ownership_evidence],
        binding_store=binding_store,
    )
    ownership_coverage = CoverageBinding.create(
        view,
        target_kind="DIMENSION",
        target="ownership",
        supporting_claim_ids=[ownership.claim_id],
        supporting_fragment_ids=[ownership_fragment.fragment_id],
    )
    recovery_gap = Unknown.create(
        view,
        subject="repository recovery boundary",
        dimension="recovery",
        reason="no exact recovery evidence was requested in this initial package",
    )
    unrelated_gap = Unknown.create(
        view,
        subject="dependency boundary",
        dimension="dependencies",
        reason="dependency evidence was intentionally not requested",
    )
    understanding = RepositoryUnderstandingView.create(
        intent,
        view,
        claims=[ownership],
        requested_dimensions=["ownership", "recovery", "dependencies"],
        covered_dimensions=["ownership"],
        must_see_categories=["recovery"],
        covered_must_see=[],
        coverage_bindings=[ownership_coverage],
        unknowns=[recovery_gap, unrelated_gap],
        binding_store=binding_store,
    )
    package = ContextPackage.from_understanding(
        understanding,
        horizon="COMPONENT",
        included_fragment_ids=[ownership_fragment.fragment_id],
    )
    return (
        repo,
        resource,
        view,
        intent,
        content_store,
        binding_store,
        ownership_fragment,
        ownership,
        understanding,
        package,
        recovery_gap,
        unrelated_gap,
    )


def _digest(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


def test_fact_requires_verified_source_evidence_and_exact_repo_content_binding(tmp_path: Path) -> None:
    _repo, _resource, view, _intent = _fixture(tmp_path)
    with pytest.raises(EngineeringIntelligenceError) as arbitrary:
        UnderstandingClaim.create(
            view,
            subject="src/service.py",
            dimension="ownership",
            kind="FACT",
            statement="untrusted digest is not source authority",
            evidence_refs=[_digest("arbitrary")],
        )
    assert arbitrary.value.code == "repository_source_evidence_required"

    generic_content, generic_bindings, generic_fragment, generic_evidence = _accepted_fragment(tmp_path, view, "accepted")
    with pytest.raises(EngineeringIntelligenceError) as generic:
        UnderstandingClaim.create(
            view,
            subject="src/service.py",
            dimension="ownership",
            kind="FACT",
            statement="a generic accepted fragment is not repository source authority",
            source_evidence=[generic_evidence],
            binding_store=generic_bindings,
        )
    assert generic.value.code == "repository_source_evidence_required"

    content, bindings, fragment, evidence = _repo_source(tmp_path, view, "src/service.py")
    claim = UnderstandingClaim.create(
        view,
        subject="src/service.py",
        dimension="ownership",
        kind="FACT",
        statement="accepted source is exact",
        source_evidence=[evidence],
        binding_store=bindings,
    )
    assert claim.source_evidence[0].fragment_id == fragment.fragment_id
    foreign_root = tmp_path / "foreign"
    foreign_root.mkdir()
    _foreign_repo, _foreign_resource, foreign_view, _foreign_intent = _fixture(foreign_root)
    foreign_content, foreign_bindings, foreign_fragment, foreign_evidence = _repo_source(foreign_root, foreign_view, "src/service.py")
    content.publish(foreign_evidence.content_ref, foreign_content.resolve(foreign_evidence.content_ref))
    bindings.accept(foreign_fragment, view=foreign_view)
    with pytest.raises(EngineeringIntelligenceError) as foreign_repo:
        UnderstandingClaim.create(
            view,
            subject="src/service.py",
            dimension="ownership",
            kind="FACT",
            statement="foreign RepoView cannot ground this claim",
            source_evidence=[foreign_evidence],
            binding_store=bindings,
        )
    assert foreign_repo.value.code == "repository_source_repo_mismatch"
    with pytest.raises(EngineeringIntelligenceError) as unaccepted:
        UnderstandingClaim.create(
            view,
            subject="src/service.py",
            dimension="ownership",
            kind="FACT",
            statement="descriptor must be accepted",
            source_evidence=[evidence],
            binding_store=DurableBindingStore(tmp_path / "empty-runtime"),
        )
    assert unaccepted.value.code == "repository_source_binding_failure"
    bindings.close()
    generic_bindings.close()
    foreign_bindings.close()


def test_repo_source_evidence_proves_committed_bytes_and_rejects_fabricated_accepted_bytes(tmp_path: Path) -> None:
    repo, _resource, view, _intent = _fixture(tmp_path)
    content, bindings, fragment, evidence = _repo_source(tmp_path, view, "src/service.py")
    assert view.query().read_bytes("src/service.py") == b"OWNER = 'A'\n"
    assert bindings.resolve_accepted(evidence.fragment_id, expected_view=view).raw == b"OWNER = 'A'\n"
    assert evidence.source_object_id == view.query().get_entry("src/service.py").object_oid

    # The checkout may become dirty after the exact committed RepoView is built;
    # source evidence still resolves the immutable committed blob.
    (repo / "src" / "service.py").write_text("OWNER = 'WORKTREE'\n", encoding="utf-8")
    assert view.read_text("src/service.py") == "OWNER = 'A'\n"
    assert bindings.resolve_accepted(evidence.fragment_id, expected_view=view).raw == b"OWNER = 'A'\n"

    claim = UnderstandingClaim.create(
        view,
        subject="src/service.py",
        dimension="ownership",
        kind="FACT",
        statement="ownership is grounded in the exact committed bytes",
        source_evidence=[evidence],
        binding_store=bindings,
    )
    assert claim.authority == "EXACT_SOURCE"

    fabricated_raw = b"OWNER = 'EVIL'\n"
    fabricated_ref = make_content_ref(evidence.content_ref.type, evidence.content_ref.schema, fabricated_raw)
    content.publish(fabricated_ref, fabricated_raw)
    fabricated_fragment = TypedContextFragment.create(
        view,
        fabricated_ref,
        fragment_type=evidence.fragment_type,
        fragment_schema=evidence.fragment_schema,
        payload_size_bytes=len(fabricated_raw),
    )
    bindings.accept(fabricated_fragment, view=view)
    fabricated_evidence = RepoSourceEvidence.from_fragment(
        view=view,
        source_path=evidence.source_path,
        source_object_id=evidence.source_object_id,
        fragment=fabricated_fragment,
    )
    with pytest.raises(EngineeringIntelligenceError) as mismatch:
        fabricated_evidence.validate(view, bindings)
    assert mismatch.value.code == "repository_source_mismatch"
    bindings.close()


def test_repo_source_evidence_fails_closed_for_foreign_stale_and_missing_sources(tmp_path: Path) -> None:
    repo, resource, view, _intent = _fixture(tmp_path)
    content, bindings, _fragment, evidence = _repo_source(tmp_path, view, "src/service.py")

    foreign_root = tmp_path / "foreign-source"
    foreign_root.mkdir()
    _foreign_repo, _foreign_resource, foreign_view, _foreign_intent = _fixture(foreign_root)
    _foreign_content, foreign_bindings, _foreign_fragment, foreign_evidence = _repo_source(
        foreign_root,
        foreign_view,
        "src/service.py",
    )
    with pytest.raises(EngineeringIntelligenceError) as foreign:
        foreign_evidence.validate(view, bindings)
    assert foreign.value.code == "repository_source_repo_mismatch"

    (repo / "src" / "service.py").write_text("OWNER = 'B'\n", encoding="utf-8")
    _git(repo, "add", "src/service.py")
    _git(repo, "commit", "-qm", "stale source fixture")
    stale_view = resource.resolve_committed("refs/heads/main", observed_at="2026-08-10T00:02:00Z")
    with pytest.raises(EngineeringIntelligenceError) as stale:
        evidence.validate(stale_view, bindings)
    assert stale.value.code == "repository_source_repo_mismatch"

    with pytest.raises(EngineeringIntelligenceError) as missing:
        publish_repo_source_evidence(
            view,
            "missing/source.txt",
            content,
            bindings,
            fragment_type="text/plain",
            fragment_schema="m2c-repo-source-v1",
        )
    assert missing.value.code == "repository_source_not_found"
    with pytest.raises(EngineeringIntelligenceError) as unsafe:
        publish_repo_source_evidence(
            view,
            "../src/service.py",
            content,
            bindings,
            fragment_type="text/plain",
            fragment_schema="m2c-repo-source-v1",
        )
    assert unsafe.value.code == "unsafe_source_path"
    bindings.close()
    foreign_bindings.close()


def test_understanding_claims_separate_fact_inference_and_require_exact_basis(tmp_path: Path) -> None:
    (_repo, _resource, _view, _intent, _content, bindings, _fragment, _ownership, understanding, _package, _gap, _other) = _seeded(tmp_path)
    with pytest.raises(EngineeringIntelligenceError) as failure:
        UnderstandingClaim(
            _digest("bad-claim"),
            understanding.repo_view,
            "src/service.py",
            "ownership",
            "FACT",
            "DERIVED",
            "source-looking claim",
            (_digest("bad"),),
            (),
        )
    assert failure.value.code == "claim_authority_mismatch"
    bindings.close()


def test_coverage_requires_explicit_grounded_bindings_and_rejects_random_support(tmp_path: Path) -> None:
    (_repo, _resource, view, intent, _content, bindings, _fragment, ownership, _understanding, _package, gap, _other) = _seeded(tmp_path)
    with pytest.raises(EngineeringIntelligenceError) as missing:
        RepositoryUnderstandingView.create(
            intent,
            view,
            claims=[ownership],
            requested_dimensions=["ownership"],
            covered_dimensions=["ownership"],
            binding_store=bindings,
        )
    assert missing.value.code == "coverage_grounding_required"
    random_binding = CoverageBinding.create(
        view,
        target_kind="DIMENSION",
        target="ownership",
        supporting_claim_ids=[_digest("foreign-claim")],
    )
    with pytest.raises(EngineeringIntelligenceError) as foreign:
        RepositoryUnderstandingView.create(
            intent,
            view,
            claims=[ownership],
            requested_dimensions=["ownership"],
            covered_dimensions=["ownership"],
            coverage_bindings=[random_binding],
            binding_store=bindings,
        )
    assert foreign.value.code == "coverage_claim_missing"
    assert gap.unknown_id not in ownership.claim_id
    bindings.close()


def test_conflicting_source_and_inference_remain_visible_and_source_wins(tmp_path: Path) -> None:
    (_repo, _resource, view, _intent, _content, bindings, _fragment, source, seeded, _package, _gap, _other) = _seeded(tmp_path)
    inference = UnderstandingClaim.create(
        seeded.repo_view,
        subject=source.subject,
        dimension=source.dimension,
        kind="INFERENCE",
        statement="component ownership is inferred as B",
        evidence_refs=[source.claim_id],
        basis_refs=[source.claim_id],
    )
    contradiction = ClaimContradiction.create(
        seeded.repo_view,
        subject=source.subject,
        dimension=source.dimension,
        claim_ids=[source.claim_id, inference.claim_id],
        source_claim_ids=[source.claim_id],
        derived_claim_ids=[inference.claim_id],
        reason="derived ownership conflicts with exact source claim",
    )
    combined = RepositoryUnderstandingView.create(
        seeded.intent_basis,
        view,
        claims=[source, inference],
        requested_dimensions=["ownership"],
        covered_dimensions=["ownership"],
        coverage_bindings=[CoverageBinding.create(view, target_kind="DIMENSION", target="ownership", supporting_claim_ids=[source.claim_id])],
        contradictions=[contradiction],
        binding_store=bindings,
    )
    assert combined.contradictions[0].source_authority == "EXACT_SOURCE_WINS"
    assert combined.coverage_status == "PARTIAL"
    with pytest.raises(EngineeringIntelligenceError) as missing_contradiction:
        RepositoryUnderstandingView.create(
            seeded.intent_basis,
            view,
            claims=[source, inference],
            requested_dimensions=["ownership"],
            covered_dimensions=["ownership"],
            coverage_bindings=[CoverageBinding.create(view, target_kind="DIMENSION", target="ownership", supporting_claim_ids=[source.claim_id])],
            binding_store=bindings,
        )
    assert missing_contradiction.value.code == "contradiction_required"
    bindings.close()


def test_context_resolution_preserves_unrelated_gaps_and_denial_keeps_all_visible(tmp_path: Path) -> None:
    (
        _repo,
        _resource,
        view,
        intent,
        content_store,
        bindings,
        ownership_fragment,
        ownership,
        seeded,
        prior_package,
        recovery_gap,
        unrelated_gap,
    ) = _seeded(tmp_path)
    request = ContextRequest.create(
        prior_package,
        gap_ids=[recovery_gap.unknown_id],
        horizon="COMPONENT",
        requested_dimensions=["recovery"],
        requested_evidence=["committed recovery boundary"],
        question="Which exact committed component owns recovery?",
        reason="repair only the visible recovery gap",
    )
    _recovery_content, _recovery_bindings, recovery_fragment, recovery_evidence = _repo_source(
        tmp_path,
        view,
        "docs/recovery.txt",
        content_store=content_store,
        binding_store=bindings,
    )
    recovery = UnderstandingClaim.create(
        view,
        subject="repository recovery boundary",
        dimension="recovery",
        kind="FACT",
        statement="recovery evidence is supplied by the exact committed source",
        source_evidence=[recovery_evidence],
        binding_store=bindings,
    )
    recovery_dimension = CoverageBinding.create(
        view,
        target_kind="DIMENSION",
        target="recovery",
        supporting_claim_ids=[recovery.claim_id],
        supporting_fragment_ids=[recovery_fragment.fragment_id],
    )
    recovery_must_see = CoverageBinding.create(
        view,
        target_kind="MUST_SEE",
        target="recovery",
        supporting_claim_ids=[recovery.claim_id],
        supporting_fragment_ids=[recovery_fragment.fragment_id],
    )
    expanded = RepositoryUnderstandingView.create(
        intent,
        view,
        claims=[ownership, recovery],
        requested_dimensions=["ownership", "recovery", "dependencies"],
        covered_dimensions=["ownership", "recovery"],
        must_see_categories=["recovery"],
        covered_must_see=["recovery"],
        coverage_bindings=[seeded.coverage_bindings[0], recovery_dimension, recovery_must_see],
        unknowns=[unrelated_gap],
        binding_store=bindings,
    )
    repaired_package = ContextPackage.from_understanding(
        expanded,
        horizon="COMPONENT",
        included_fragment_ids=[ownership_fragment.fragment_id, recovery_fragment.fragment_id],
    )
    evidence = GapResolutionEvidence.create(
        gap_id=recovery_gap.unknown_id,
        added_fragment_ids=[recovery_fragment.fragment_id],
        supporting_claim_ids=[recovery.claim_id],
        coverage_binding_ids=[recovery_dimension.coverage_binding_id],
    )
    resolution = ContextResolution.create(
        request,
        prior_package,
        resulting_package=repaired_package,
        added_fragments=[recovery_fragment],
        binding_store=bindings,
        resolved_gap_ids=[recovery_gap.unknown_id],
        gap_resolution_evidence=[evidence],
    )
    assert resolution.outcome == "RESOLVED"
    assert set(repaired_package.gap_ids) == {unrelated_gap.unknown_id}
    assert resolution.unresolved_gap_ids == (unrelated_gap.unknown_id,)
    with pytest.raises(EngineeringIntelligenceError) as silent_drop:
        ContextResolution.create(
            request,
            prior_package,
            resulting_package=ContextPackage.from_understanding(expanded, horizon="COMPONENT", included_fragment_ids=[ownership_fragment.fragment_id, recovery_fragment.fragment_id]),
            added_fragments=[recovery_fragment],
            binding_store=bindings,
            resolved_gap_ids=[recovery_gap.unknown_id],
            unresolved_gap_ids=[],
            gap_resolution_evidence=[evidence],
        )
    assert silent_drop.value.code == "resolution_gap_mismatch"
    denied = ContextResolution.create(request, prior_package, outcome="DENIED", denial_reason="policy denied")
    assert denied.unresolved_gap_ids == prior_package.gap_ids
    assert ContextResolution.from_mapping(resolution.as_dict()) == resolution
    bindings.close()


def test_decision_claim_membership_and_exact_basis_sets(tmp_path: Path) -> None:
    (repo, resource, view, intent, _content, bindings, ownership_fragment, ownership, understanding, package, _gap, _other) = _seeded(tmp_path)
    option_zero = DecisionOption("OPTION_ZERO", "retain the read-only path", ("no writer",), ("gap remains",))
    option_one = DecisionOption("OPTION_ONE", "expand exact context", ("more evidence",), ("larger package",))
    kwargs = dict(
        established_fact_claim_ids=[ownership.claim_id],
        alternatives=[option_zero, option_one],
        chosen_option_id="OPTION_ONE",
        must_preserve=["exact RepoView authority", "runtime OFF"],
        must_not=["Task lifecycle", "private chain-of-thought"],
        expected_effect_scope=["M2c semantic records only"],
        acceptance_obligations=["seeded gap repair"],
        evidence_obligations=["M2b exact roundtrip"],
        uncertainty=["recovery evidence is initially unknown"],
        architecture_consequences=["rebuildable projection"],
        revisit_triggers=["RepoView or intent basis changes"],
        understanding_bases=[understanding],
        binding_store=bindings,
    )
    decision = EngineeringDecision.create(intent, [view], [package], **kwargs)
    same = EngineeringDecision.create(intent, [view], [package], **kwargs)
    assert same.decision_id == decision.decision_id
    package.validate_source_grounding(understanding, view, bindings)
    parsed_understanding = RepositoryUnderstandingView.from_mapping(understanding.as_dict())
    parsed_package = ContextPackage.from_mapping(package.as_dict())
    parsed_package.validate_source_grounding(parsed_understanding, view, bindings)
    _semantic_fragment, transported_understanding = transport_semantic_record(
        understanding,
        view,
        _content,
        bindings,
    )
    assert transported_understanding == understanding
    validate_decision_applicability(decision, intent_basis=intent, repo_views=[view], context_packages=[package])
    with pytest.raises(EngineeringIntelligenceError) as random_claim:
        EngineeringDecision.create(intent, [view], [package], established_fact_claim_ids=[_digest("random")], **{k: v for k, v in kwargs.items() if k != "established_fact_claim_ids"})
    assert random_claim.value.code == "decision_claim_basis_mismatch"
    changed_package = ContextPackage.from_understanding(understanding, horizon="COMPONENT", included_fragment_ids=[ownership_fragment.fragment_id, _digest("new")])
    with pytest.raises(EngineeringIntelligenceError) as old_new:
        validate_decision_applicability(decision, intent_basis=intent, repo_views=[view], context_packages=[package, changed_package])
    assert old_new.value.code == "stale_decision_basis"
    with pytest.raises(EngineeringIntelligenceError) as new_only:
        validate_decision_applicability(decision, intent_basis=intent, repo_views=[view], context_packages=[changed_package])
    assert new_only.value.code == "stale_decision_basis"
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "changed view")
    changed_view = resource.resolve_committed("refs/heads/main", observed_at="2026-08-10T00:01:00Z")
    with pytest.raises(EngineeringIntelligenceError) as stale_view:
        validate_decision_applicability(decision, intent_basis=intent, repo_views=[changed_view], context_packages=[package])
    assert stale_view.value.code == "stale_decision_basis"
    bindings.close()


def test_semantic_record_roundtrip_and_import_side_effect_contract(tmp_path: Path) -> None:
    _repo, _resource, view, intent = _fixture(tmp_path)
    unknown = Unknown.create(view, subject="network boundary", dimension="network", reason="not requested")
    understanding = RepositoryUnderstandingView.create(intent, view, requested_dimensions=["network"], unknowns=[unknown])
    package = ContextPackage.from_understanding(understanding, horizon="LOCAL")
    request = ContextRequest.create(
        package,
        gap_ids=[unknown.unknown_id],
        horizon="LOCAL",
        requested_dimensions=["network"],
        requested_evidence=["network boundary"],
        question="What is the network boundary?",
        reason="unknown boundary",
    )
    raw = request.to_json_bytes()
    assert reconstruct_semantic_record(raw) == request
    assert raw == request.to_json_bytes()

```

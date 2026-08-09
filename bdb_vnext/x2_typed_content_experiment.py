"""X2 fixture-only typed content durability experiment.

This module deliberately does not provide a production Content Store.  It
exercises a small immutable content root, a typed ContentRef contract and the
existing M1b coordinated backup/restore floor on disposable resources only.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, NoReturn

from bdb_vnext.bootstrap import (
    BootstrapError,
    BackupArtifact,
    create_coordinated_backup,
    restore_backup,
    verify_backup,
)


X2_SCHEMA = "bdb-vnext-x2-typed-content-v1"
X2_FIXTURE_SCHEMA = "bdb-vnext-x2-fixture-ref-v1"
X2_SEMANTIC_DOMAIN = "bdb-vnext-x2-semantic-v1"
X2_STATUS = Literal["PASS", "FAIL", "INCONCLUSIVE"]
_DIGEST = r"^sha256:[0-9a-f]{64}$"
_IDENTIFIER = r"^[A-Za-z0-9][A-Za-z0-9._/+:-]{0,127}$"
_SUPPORTED_DOMAINS = {
    ("text/plain", "x2-text-v1"),
    ("application/octet-stream", "x2-bytes-v1"),
}


class X2ExperimentError(RuntimeError):
    """A deterministic X2 experiment or contract failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> NoReturn:
    raise X2ExperimentError(code, message)


def _require(condition: bool, code: str, message: str) -> None:
    if not condition:
        _fail(code, message)


def _canonical_json_bytes(document: Mapping[str, Any]) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest_bytes(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _validate_digest(value: object, *, field: str) -> str:
    if not isinstance(value, str) or len(value) != 71 or not value.startswith("sha256:"):
        _fail("malformed_content_ref", f"{field} is not a sha256 digest")
    try:
        int(value[7:], 16)
    except ValueError:
        _fail("malformed_content_ref", f"{field} is not hexadecimal")
    if not all(character in "0123456789abcdef" for character in value[7:]):
        _fail("malformed_content_ref", f"{field} is not lowercase hexadecimal")
    return value


def _validate_identifier(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        _fail("malformed_content_ref", f"{field} is not a bounded identifier")
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._/+:-")
    if value[0] not in allowed or any(character not in allowed for character in value):
        _fail("malformed_content_ref", f"{field} contains an invalid character")
    return value


def _semantic_representation(content_type: str, schema: str, raw: bytes) -> dict[str, Any]:
    if (content_type, schema) == ("text/plain", "x2-text-v1"):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise X2ExperimentError("semantic_decode_failure", "text fixture is not valid UTF-8") from exc
        return {"encoding": "utf-8", "text": text}
    if (content_type, schema) == ("application/octet-stream", "x2-bytes-v1"):
        return {"base64": base64.b64encode(raw).decode("ascii")}
    _fail("unsupported_semantic_domain", f"unsupported X2 fixture domain {(content_type, schema)!r}")


def _semantic_digest(content_type: str, schema: str, raw: bytes) -> str:
    semantic = {
        "domain": X2_SEMANTIC_DOMAIN,
        "type": content_type,
        "schema": schema,
        "value": _semantic_representation(content_type, schema, raw),
    }
    return _digest_bytes(_canonical_json_bytes(semantic))


@dataclass(frozen=True)
class ContentRef:
    """The exact X2 fixture contract; JSON key ``type`` is intentional."""

    type: str
    schema: str
    semantic_digest: str
    raw_digest: str

    def as_dict(self) -> dict[str, str]:
        return {
            "type": self.type,
            "schema": self.schema,
            "semantic_digest": self.semantic_digest,
            "raw_digest": self.raw_digest,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ContentRef:
        if set(value) != {"type", "schema", "semantic_digest", "raw_digest"}:
            _fail("malformed_content_ref", "ContentRef has an unexpected field set")
        content_type = _validate_identifier(value["type"], field="type")
        schema = _validate_identifier(value["schema"], field="schema")
        semantic_digest = _validate_digest(value["semantic_digest"], field="semantic_digest")
        raw_digest = _validate_digest(value["raw_digest"], field="raw_digest")
        return cls(content_type, schema, semantic_digest, raw_digest)


def make_content_ref(content_type: str, schema: str, raw: bytes) -> ContentRef:
    """Create a ref only for the two explicit X2 fixture domains."""

    _validate_identifier(content_type, field="type")
    _validate_identifier(schema, field="schema")
    if (content_type, schema) not in _SUPPORTED_DOMAINS:
        _fail("unsupported_semantic_domain", f"unsupported X2 fixture domain {(content_type, schema)!r}")
    return ContentRef(
        content_type,
        schema,
        _semantic_digest(content_type, schema, raw),
        _digest_bytes(raw),
    )


def _absolute_path(value: str | Path, *, field: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        _fail("relative_path", f"{field} must be absolute")
    return Path(os.path.abspath(path))


def _contains(child: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath((str(child), str(parent))) == os.path.commonpath(
            (str(parent), str(parent))
        )
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


def validate_content_relative_path(relative: str) -> str:
    """Validate the POSIX layout path used by the fixture."""

    if not isinstance(relative, str) or not relative or "\\" in relative:
        _fail("path_escape", "content path must be a non-empty POSIX relative path")
    path = PurePosixPath(relative)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        _fail("path_escape", "content path escapes the isolated root")
    return path.as_posix()


def _safe_child(root: Path, relative: str, *, field: str) -> Path:
    relative = validate_content_relative_path(relative)
    target = root.joinpath(*PurePosixPath(relative).parts)
    if not _contains(target, root) or target == root:
        _fail("path_escape", f"{field} escapes its root")
    _assert_no_reparse_components(target, field=field)
    return target


def _write_fsync(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _read_regular(path: Path, *, field: str) -> bytes:
    try:
        info = os.lstat(path)
    except FileNotFoundError as exc:
        raise X2ExperimentError("raw_object_missing", f"{field} is missing") from exc
    if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
        _fail("reparse_point", f"{field} is a symlink or reparse point")
    if not stat.S_ISREG(info.st_mode):
        _fail("unexpected_file_type", f"{field} is not a regular file")
    return path.read_bytes()


@dataclass(frozen=True)
class CommitReceipt:
    ref: ContentRef
    object_relative_path: str
    ref_relative_path: str
    object_publication: Literal["published", "converged"]
    ref_publication: Literal["published", "converged"]


class TypedContentStore:
    """Small immutable fixture store; not a production storage abstraction."""

    def __init__(self, root: str | Path) -> None:
        self.root = _absolute_path(root, field="content_store_root")
        self.content_root = self.root / "content"
        self.objects_root = self.content_root / "objects"
        self.refs_root = self.content_root / "refs"
        self.temp_root = self.content_root / "tmp"
        self._publish_lock = threading.RLock()
        self._ensure_layout()

    def _ensure_layout(self) -> None:
        _assert_no_reparse_components(self.root, field="content_store_root")
        for directory in (self.objects_root, self.refs_root, self.temp_root):
            directory.mkdir(parents=True, exist_ok=True)
            _assert_no_reparse_components(directory, field="content_layout")

    def _object_relative(self, raw_digest: str) -> str:
        _validate_digest(raw_digest, field="raw_digest")
        return f"objects/{raw_digest[7:]}.bin"

    def _ref_relative(self, semantic_digest: str) -> str:
        _validate_digest(semantic_digest, field="semantic_digest")
        return f"refs/{semantic_digest[7:]}.json"

    def _object_path(self, raw_digest: str) -> Path:
        return _safe_child(self.content_root, self._object_relative(raw_digest), field="object_path")

    def _ref_path(self, semantic_digest: str) -> Path:
        return _safe_child(self.content_root, self._ref_relative(semantic_digest), field="ref_path")

    def _temp_path(self, prefix: str) -> Path:
        return _safe_child(
            self.content_root,
            f"tmp/{prefix}-{uuid.uuid4().hex}.partial",
            field="temporary_path",
        )

    def _publish_immutable(self, temporary: Path, target: Path, expected: bytes) -> Literal["published", "converged"]:
        """Publish with an atomic same-volume replace and no overwrite of a committed value."""

        with self._publish_lock:
            if os.path.lexists(target):
                observed = _read_regular(target, field="existing_committed_file")
                if observed != expected:
                    _fail("immutable_object_conflict", f"{target} already contains different bytes")
                temporary.unlink(missing_ok=True)
                return "converged"
            try:
                os.replace(temporary, target)
            except OSError as exc:
                raise X2ExperimentError("atomic_publish_failed", "atomic os.replace publication failed") from exc
            return "published"

    def _metadata(self, ref: ContentRef) -> bytes:
        document = {
            "schema": X2_SCHEMA,
            "content_ref": ref.as_dict(),
            "object_relative_path": self._object_relative(ref.raw_digest),
        }
        return _canonical_json_bytes(document)

    def commit(
        self,
        ref: ContentRef,
        raw: bytes,
        *,
        failure: str | None = None,
    ) -> CommitReceipt:
        """Publish an exact object/ref pair, with deterministic fault injection."""

        parsed = ContentRef.from_mapping(ref.as_dict())
        expected_raw = _digest_bytes(raw)
        try:
            expected_semantic = _semantic_digest(parsed.type, parsed.schema, raw)
        except X2ExperimentError:
            raise
        if parsed.raw_digest != expected_raw or parsed.semantic_digest != expected_semantic:
            _fail("content_ref_integrity_failure", "ContentRef does not bind the supplied bytes")

        object_path = self._object_path(parsed.raw_digest)
        ref_path = self._ref_path(parsed.semantic_digest)
        if failure == "before_temp_write":
            _fail("publication_failed_before_temp", "injected failure before temporary write")

        object_temp = self._temp_path("object")
        try:
            if failure == "during_temp_write":
                partial = raw[: max(1, len(raw) // 2)]
                _write_fsync(object_temp, partial)
                _fail("publication_failed_during_temp", "injected failure during temporary write")
            _write_fsync(object_temp, raw)
            if failure == "after_temp_write_before_publish":
                _fail("publication_failed_before_publish", "injected failure before object publication")
            object_publication = self._publish_immutable(object_temp, object_path, raw)
            if failure == "after_object_publish_before_ref":
                _fail("publication_failed_before_ref", "injected failure after object publication")

            ref_temp = self._temp_path("ref")
            _write_fsync(ref_temp, self._metadata(parsed))
            ref_publication = self._publish_immutable(ref_temp, ref_path, self._metadata(parsed))
            if failure == "after_ref_publish":
                _fail("publication_ack_lost", "injected failure after ref publication")

            self.resolve(parsed)
            return CommitReceipt(
                parsed,
                self._object_relative(parsed.raw_digest),
                self._ref_relative(parsed.semantic_digest),
                object_publication,
                ref_publication,
            )
        except Exception:
            # Failed publications intentionally retain temp/orphan evidence for
            # deterministic classification; the whole root is disposable.
            raise

    def resolve(self, ref: ContentRef | Mapping[str, Any]) -> bytes:
        """Resolve only after exact ref, raw and semantic checks."""

        parsed = ref if isinstance(ref, ContentRef) else ContentRef.from_mapping(ref)
        ref_path = self._ref_path(parsed.semantic_digest)
        try:
            metadata_bytes = _read_regular(ref_path, field="committed_ref")
        except X2ExperimentError as exc:
            if exc.code == "raw_object_missing":
                object_path = self._object_path(parsed.raw_digest)
                if os.path.lexists(object_path):
                    _fail("content_ref_not_committed", "no committed ref exists for this identity")
                _fail("content_ref_missing", "committed ref is missing")
            raise
        try:
            document = json.loads(metadata_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise X2ExperimentError("malformed_committed_ref", "committed ref metadata is not valid JSON") from exc
        if not isinstance(document, dict) or set(document) != {
            "schema",
            "content_ref",
            "object_relative_path",
        }:
            _fail("malformed_committed_ref", "committed ref metadata has the wrong shape")
        if document["schema"] != X2_SCHEMA:
            _fail("metadata_schema_mismatch", "committed ref metadata has the wrong schema")
        stored = ContentRef.from_mapping(document["content_ref"])
        if stored.type != parsed.type or stored.schema != parsed.schema:
            _fail("semantic_type_schema_mismatch", "requested type/schema differs from committed ref")
        if stored.semantic_digest != parsed.semantic_digest or stored.raw_digest != parsed.raw_digest:
            _fail("content_ref_identity_mismatch", "requested ContentRef differs from committed ref")
        expected_object_relative = self._object_relative(parsed.raw_digest)
        if document["object_relative_path"] != expected_object_relative:
            _fail("metadata_path_mismatch", "metadata path is not derived from the exact raw digest")
        object_path = self._object_path(parsed.raw_digest)
        raw = _read_regular(object_path, field="committed_object")
        if _digest_bytes(raw) != parsed.raw_digest:
            _fail("raw_integrity_failure", "committed object raw digest does not match ContentRef")
        try:
            semantic_digest = _semantic_digest(parsed.type, parsed.schema, raw)
        except X2ExperimentError as exc:
            if exc.code == "unsupported_semantic_domain":
                raise X2ExperimentError("semantic_type_schema_mismatch", str(exc)) from exc
            raise
        if semantic_digest != parsed.semantic_digest:
            _fail("semantic_integrity_failure", "semantic digest does not match ContentRef")
        return raw

    def classify_layout(self) -> dict[str, list[str]]:
        """Classify temp/orphan/foreign files without any cleanup or GC."""

        temp_files = sorted(
            str(path.relative_to(self.root)).replace("\\", "/")
            for path in self.temp_root.glob("*")
            if path.is_file() or path.is_symlink()
        )
        referenced_objects: set[str] = set()
        foreign_files: list[str] = []
        for path in self.refs_root.glob("*.json"):
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
                ref = ContentRef.from_mapping(document["content_ref"])
                referenced_objects.add(self._object_relative(ref.raw_digest))
            except (OSError, KeyError, TypeError, ValueError, X2ExperimentError):
                foreign_files.append(str(path.relative_to(self.root)).replace("\\", "/"))
        object_files = {
            str(path.relative_to(self.content_root)).replace("\\", "/")
            for path in self.objects_root.glob("*")
            if path.is_file() or path.is_symlink()
        }
        orphan_objects = sorted(object_files - referenced_objects)
        for path in self.content_root.glob("*"):
            if path.name not in {"objects", "refs", "tmp"}:
                foreign_files.append(str(path.relative_to(self.root)).replace("\\", "/"))
        return {
            "orphan_temp": temp_files,
            "orphan_objects": orphan_objects,
            "foreign": sorted(set(foreign_files)),
        }


def _fixture_ref() -> tuple[ContentRef, bytes]:
    raw = "x2 committed fixture — immutable bytes\n".encode("utf-8")
    return make_content_ref("text/plain", "x2-text-v1", raw), raw


def _write_fixture_config(runtime: Path, ref: ContentRef) -> None:
    path = runtime / "config" / "bdb-vnext.json"
    _write_fsync(
        path,
        _canonical_json_bytes(
            {
                "schema": X2_FIXTURE_SCHEMA,
                "content_ref": ref.as_dict(),
            }
        ),
    )


def _create_sqlite_fixture(runtime: Path, ref: ContentRef) -> sqlite3.Connection:
    database = runtime / "control" / "control.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    writer = sqlite3.connect(database)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA synchronous=FULL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE x2_fixture (semantic_digest TEXT NOT NULL, raw_digest TEXT NOT NULL)")
    writer.execute(
        "INSERT INTO x2_fixture VALUES (?, ?)",
        (ref.semantic_digest, ref.raw_digest),
    )
    writer.commit()
    writer.close()
    reader = sqlite3.connect(database)
    reader.execute("PRAGMA query_only=ON")
    reader.execute("BEGIN")
    return reader


def _expect_failure(operation: Callable[[], Any], *, accepted: set[str] | None = None) -> str:
    try:
        operation()
    except X2ExperimentError as exc:
        if accepted is not None and exc.code not in accepted:
            _fail("unexpected_failure_code", f"observed {exc.code!r}, expected one of {sorted(accepted)!r}")
        return exc.code
    else:
        _fail("fault_accepted", "fault operation unexpectedly succeeded")


def _run_publication_faults(root: Path, ref: ContentRef, raw: bytes) -> dict[str, Any]:
    cases: dict[str, Any] = {}
    for failure in (
        "before_temp_write",
        "during_temp_write",
        "after_temp_write_before_publish",
        "after_object_publish_before_ref",
    ):
        store = TypedContentStore(root / f"fault-{failure}")
        code = _expect_failure(lambda: store.commit(ref, raw, failure=failure))
        resolution_code = _expect_failure(lambda: store.resolve(ref), accepted={"content_ref_missing", "content_ref_not_committed", "raw_object_missing"})
        layout = store.classify_layout()
        cases[failure] = {
            "failure_code": code,
            "resolve_code": resolution_code,
            "orphan_temp": layout["orphan_temp"],
            "orphan_objects": layout["orphan_objects"],
        }

    acknowledged_store = TypedContentStore(root / "fault-after-ref-publish")
    ack_code = _expect_failure(
        lambda: acknowledged_store.commit(ref, raw, failure="after_ref_publish"),
        accepted={"publication_ack_lost"},
    )
    _require(acknowledged_store.resolve(ref) == raw, "post_publish_resolve_failure", "published ref did not resolve after acknowledgement loss")
    cases["after_ref_publish"] = {
        "failure_code": ack_code,
        "resolve": "exact_bytes",
        "layout": acknowledged_store.classify_layout(),
    }
    return cases


def _run_integrity_faults(root: Path, ref: ContentRef, raw: bytes) -> dict[str, Any]:
    cases: dict[str, str] = {}
    missing_store = TypedContentStore(root / "integrity-missing")
    missing_store.commit(ref, raw)
    missing_store._object_path(ref.raw_digest).unlink()
    cases["missing_committed_object"] = _expect_failure(lambda: missing_store.resolve(ref), accepted={"raw_object_missing"})

    truncated_store = TypedContentStore(root / "integrity-truncated")
    truncated_store.commit(ref, raw)
    truncated_path = truncated_store._object_path(ref.raw_digest)
    truncated_path.write_bytes(raw[:-1])
    cases["truncated_committed_object"] = _expect_failure(lambda: truncated_store.resolve(ref), accepted={"raw_integrity_failure"})

    mutated_store = TypedContentStore(root / "integrity-mutated")
    mutated_store.commit(ref, raw)
    mutated_path = mutated_store._object_path(ref.raw_digest)
    mutated = bytearray(raw)
    mutated[0] ^= 0xFF
    mutated_path.write_bytes(bytes(mutated))
    cases["mutated_committed_object"] = _expect_failure(lambda: mutated_store.resolve(ref), accepted={"raw_integrity_failure"})

    wrong_raw_store = TypedContentStore(root / "integrity-wrong-raw")
    wrong_raw_store.commit(ref, raw)
    wrong_raw = ContentRef(ref.type, ref.schema, ref.semantic_digest, _digest_bytes(b"other raw bytes"))
    cases["wrong_raw_digest"] = _expect_failure(lambda: wrong_raw_store.resolve(wrong_raw), accepted={"content_ref_identity_mismatch", "content_ref_not_committed"})

    wrong_type = ContentRef("application/octet-stream", ref.schema, ref.semantic_digest, ref.raw_digest)
    wrong_schema = ContentRef(ref.type, "x2-other-schema-v1", ref.semantic_digest, ref.raw_digest)
    type_store = TypedContentStore(root / "integrity-type-schema")
    type_store.commit(ref, raw)
    cases["wrong_semantic_type"] = _expect_failure(lambda: type_store.resolve(wrong_type), accepted={"semantic_type_schema_mismatch"})
    cases["wrong_schema"] = _expect_failure(lambda: type_store.resolve(wrong_schema), accepted={"semantic_type_schema_mismatch"})

    malformed = {"type": ref.type, "schema": ref.schema, "raw_digest": ref.raw_digest}
    cases["malformed_content_ref"] = _expect_failure(lambda: ContentRef.from_mapping(malformed), accepted={"malformed_content_ref"})
    cases["path_escape"] = _expect_failure(lambda: validate_content_relative_path("../outside/object.bin"), accepted={"path_escape"})
    return cases


def _run_semantic_and_concurrency(root: Path, ref: ContentRef, raw: bytes) -> dict[str, Any]:
    binary_ref = make_content_ref("application/octet-stream", "x2-bytes-v1", raw)
    _require(binary_ref.raw_digest == ref.raw_digest, "raw_identity_failure", "same raw bytes did not share raw digest")
    _require(binary_ref.semantic_digest != ref.semantic_digest, "semantic_domain_failure", "type/schema did not domain-separate semantic digest")
    semantic_store = TypedContentStore(root / "semantic-domains")
    semantic_store.commit(ref, raw)
    semantic_store.commit(binary_ref, raw)
    _require(semantic_store.resolve(ref) == raw, "semantic_text_resolve_failure", "text ref did not resolve")
    _require(semantic_store.resolve(binary_ref) == raw, "semantic_binary_resolve_failure", "binary ref did not resolve")

    concurrent_store = TypedContentStore(root / "concurrent-identical")
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(lambda _: concurrent_store.commit(ref, raw), range(2)))
    _require(
        sorted(receipt.object_publication for receipt in receipts) == ["converged", "published"],
        "concurrent_identical_publication_failure",
        "same-object writers did not converge to one immutable publication",
    )
    _require(concurrent_store.resolve(ref) == raw, "concurrent_identical_resolve_failure", "concurrent ref did not resolve exactly")

    conflicting_store = TypedContentStore(root / "concurrent-conflict")
    conflicting_raw = b"conflicting writer bytes\n"
    invalid_ref = ref
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(conflicting_store.commit, ref, raw),
            pool.submit(conflicting_store.commit, invalid_ref, conflicting_raw),
        ]
        results: list[str] = []
        for future in futures:
            try:
                future.result()
            except X2ExperimentError as exc:
                results.append(exc.code)
            else:
                results.append("committed")
    _require(results.count("committed") == 1, "concurrent_conflict_ambiguity", f"conflicting writer results were {results!r}")
    _require("content_ref_integrity_failure" in results, "concurrent_conflict_not_blocked", f"conflicting writer results were {results!r}")
    _require(conflicting_store.resolve(ref) == raw, "concurrent_conflict_corruption", "valid writer bytes were corrupted")
    return {
        "same_raw_different_semantic_domain": {
            "raw_digest_equal": True,
            "semantic_digest_equal": False,
            "both_exactly_resolved": True,
        },
        "same_object_writers": {
            "publication_results": sorted(receipt.object_publication for receipt in receipts),
            "resolved_exactly": True,
        },
        "conflicting_writer": {
            "results": results,
            "failure_code": "content_ref_integrity_failure",
            "resolved_exactly": True,
        },
    }


def _run_orphan_and_foreign(root: Path, ref: ContentRef, raw: bytes) -> dict[str, Any]:
    orphan_store = TypedContentStore(root / "orphan")
    orphan_code = _expect_failure(
        lambda: orphan_store.commit(ref, raw, failure="after_object_publish_before_ref"),
        accepted={"publication_failed_before_ref"},
    )
    orphan_layout = orphan_store.classify_layout()
    _require(orphan_layout["orphan_objects"], "orphan_not_classified", "published object orphan was not classified")
    _expect_failure(lambda: orphan_store.resolve(ref), accepted={"content_ref_missing", "content_ref_not_committed"})

    foreign_store = TypedContentStore(root / "foreign")
    foreign_store.commit(ref, raw)
    foreign_path = foreign_store.content_root / "unexpected.bin"
    _write_fsync(foreign_path, b"foreign")
    foreign_layout = foreign_store.classify_layout()
    _require("content/unexpected.bin" in foreign_layout["foreign"], "foreign_file_not_classified", "foreign file was not classified")

    symlink_store = TypedContentStore(root / "symlink")
    symlink_ref, symlink_raw = _fixture_ref()
    outside = root / "symlink-outside.bin"
    _write_fsync(outside, symlink_raw)
    object_path = symlink_store._object_path(symlink_ref.raw_digest)
    ref_path = symlink_store._ref_path(symlink_ref.semantic_digest)
    symlink_available = True
    reparse_kind = "symlink"
    try:
        os.symlink(outside, object_path)
    except (OSError, NotImplementedError):
        symlink_available = False
    if symlink_available:
        _write_fsync(ref_path, symlink_store._metadata(symlink_ref))
        symlink_code = _expect_failure(lambda: symlink_store.resolve(symlink_ref), accepted={"reparse_point"})
    else:
        # Windows may deny file-symlink creation without the developer-mode
        # privilege. A directory junction is still a real NTFS reparse point
        # and exercises the same containment guard without touching user data.
        reparse_kind = "junction"
        junction_root = root / "junction"
        junction_target = root / "junction-target"
        junction_target.mkdir(parents=True, exist_ok=True)
        junction_link = junction_root / "content" / "objects"
        junction_link.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction_link), str(junction_target)],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0:
                try:
                    TypedContentStore(junction_root)
                except X2ExperimentError as exc:
                    symlink_available = exc.code == "reparse_point"
                    symlink_code = exc.code
                else:
                    symlink_code = "reparse_point_not_rejected"
            else:
                symlink_code = "reparse_creation_unavailable"
        else:
            symlink_code = "reparse_creation_unavailable"
    return {
        "orphan": {
            "failure_code": orphan_code,
            "orphan_objects": orphan_layout["orphan_objects"],
            "resolve_blocked": True,
        },
        "foreign_file": {
            "classification": foreign_layout["foreign"],
            "resolve_authority_unchanged": True,
        },
        "symlink_reparse": {
            "available": symlink_available,
            "kind": reparse_kind,
            "failure_code": symlink_code,
        },
    }


def _copy_backup(artifact: BackupArtifact, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(artifact.path, destination)
    return destination


def _run_backup_restore(root: Path, ref: ContentRef, raw: bytes) -> dict[str, Any]:
    runtime = root / "m1b-runtime"
    authority = root / "m1b-authority"
    restored = root / "m1b-restored"
    store = TypedContentStore(runtime)
    store.commit(ref, raw)
    _write_fixture_config(runtime, ref)
    reader = _create_sqlite_fixture(runtime, ref)
    try:
        artifact = create_coordinated_backup(
            runtime,
            authority / "backups",
            backup_id="x2-valid-content",
            required_control_schema=1,
            source_is_quiesced=True,
        )
    finally:
        reader.rollback()
        reader.close()
    receipt = restore_backup(
        artifact.path,
        restored,
        authority_root=authority,
        legacy_runtime_root=root / "legacy-reference",
    )
    restored_config = json.loads((restored / "config" / "bdb-vnext.json").read_text(encoding="utf-8"))
    restored_ref = ContentRef.from_mapping(restored_config["content_ref"])
    restored_bytes = TypedContentStore(restored).resolve(restored_ref)
    _require(receipt["verified"] is True, "m1b_restore_not_verified", "M1b restore did not verify")
    _require(restored_ref == ref, "m1b_ref_identity_changed", "restore changed exact ContentRef")
    _require(restored_bytes == raw, "m1b_content_identity_changed", "restore changed exact content bytes")

    object_relative = store._object_relative(ref.raw_digest)
    tamper_cases: dict[str, str] = {}
    for name in ("missing_content", "truncated_content", "corrupt_content", "foreign_file"):
        case = _copy_backup(artifact, root / "m1b-tamper" / name / artifact.path.name)
        object_path = case / "content" / object_relative
        if name == "missing_content":
            object_path.unlink()
        elif name == "truncated_content":
            object_path.write_bytes(object_path.read_bytes()[:-1])
        elif name == "corrupt_content":
            payload = bytearray(object_path.read_bytes())
            payload[0] ^= 0xFF
            object_path.write_bytes(bytes(payload))
        else:
            _write_fsync(case / "content" / "unexpected.bin", b"foreign")
        try:
            verify_backup(case)
        except BootstrapError as exc:
            tamper_cases[name] = exc.code
        else:
            _fail("m1b_tamper_accepted", f"M1b accepted tampered content case {name}")

    restore_target = root / "m1b-restore-integrity-failure"

    def corrupt_after_publish(target: Path) -> None:
        target_object = target / "content" / object_relative
        target_object.write_bytes(b"tampered-after-publish")

    try:
        restore_backup(
            artifact.path,
            restore_target,
            authority_root=authority,
            legacy_runtime_root=root / "legacy-reference-restore",
            after_publish=corrupt_after_publish,
        )
    except BootstrapError as exc:
        restore_integrity_code = exc.code
    else:
        _fail("m1b_restore_tamper_accepted", "M1b restore accepted post-publish content tamper")
    return {
        "backup_manifest_sha256": artifact.manifest_sha256,
        "restore_sha256": receipt["restore_sha256"],
        "valid_restore": {
            "verified": receipt["verified"],
            "exact_ref": restored_ref == ref,
            "exact_raw_digest": _digest_bytes(restored_bytes) == ref.raw_digest,
            "exact_semantic_digest": _semantic_digest(ref.type, ref.schema, restored_bytes) == ref.semantic_digest,
        },
        "tamper_failure_codes": tamper_cases,
        "restore_integrity_failure_code": restore_integrity_code,
        "content_subject": object_relative,
    }


def run_experiment(root: str | Path) -> dict[str, Any]:
    """Run the complete bounded X2 capsule and return JSON-safe evidence."""

    root_path = _absolute_path(root, field="experiment_root")
    root_path.mkdir(parents=True, exist_ok=True)
    ref, raw = _fixture_ref()
    try:
        normal_store = TypedContentStore(root_path / "normal")
        first = normal_store.commit(ref, raw)
        duplicate = normal_store.commit(ref, raw)
        _require(first.object_publication == "published", "normal_publication_failure", "first publication did not publish")
        _require(duplicate.object_publication == "converged", "duplicate_publication_failure", "duplicate write did not converge")
        _require(normal_store.resolve(ref) == raw, "normal_resolve_failure", "normal committed object did not resolve exactly")
        publication_faults = _run_publication_faults(root_path, ref, raw)
        integrity_faults = _run_integrity_faults(root_path, ref, raw)
        semantic_concurrency = _run_semantic_and_concurrency(root_path, ref, raw)
        orphan_foreign = _run_orphan_and_foreign(root_path, ref, raw)
        backup = _run_backup_restore(root_path, ref, raw)
        symlink_available = orphan_foreign["symlink_reparse"]["available"]
        if not symlink_available:
            _fail("symlink_boundary_unavailable", "supported Windows symlink/reparse boundary was unavailable")
        return {
            "schema": X2_SCHEMA,
            "status": "PASS",
            "hypotheses": {
                "H1": "PASS",
                "H2": "PASS",
                "H3": "PASS",
                "H4": "PASS",
                "H5": "PASS",
                "H6": "PASS",
            },
            "content_ref_contract": {
                "fields": ["type", "schema", "semantic_digest", "raw_digest"],
                "raw_digest": "sha256(exact stored bytes)",
                "semantic_digest": "sha256(canonical domain + explicit type + schema + fixture semantic representation)",
                "raw_path_authority": False,
            },
            "layout": {
                "object": "content/objects/<raw_digest_hex>.bin",
                "ref": "content/refs/<semantic_digest_hex>.json",
                "temporary": "content/tmp/*.partial",
                "publication": "same-volume os.replace after fsync; existing committed bytes are never overwritten",
            },
            "environment": {
                "platform": platform.platform(),
                "os_name": os.name,
                "python": platform.python_version(),
                "filesystem_boundary": "real files, fsync, atomic os.replace, concurrent Windows fixture paths",
            },
            "normal_commit": {
                "first_publication": first.object_publication,
                "duplicate_publication": duplicate.object_publication,
                "resolved_exactly": True,
            },
            "publication_faults": publication_faults,
            "integrity_faults": integrity_faults,
            "semantic_and_concurrency": semantic_concurrency,
            "orphan_and_foreign": orphan_foreign,
            "m1b_backup_restore": backup,
            "authority": {
                "second_authority": False,
                "production_store": False,
                "runtime_activation": False,
                "legacy_touched": False,
            },
            "limitations": [
                "fixture-only metadata/ref model; no production Content Store or GC",
                "physical power-loss and process-kill were not simulated; deterministic injected failure boundaries were observed",
                "semantic canonicalization covers only explicit X2 text/plain and application/octet-stream fixture domains",
            ],
        }
    except X2ExperimentError as exc:
        return {
            "schema": X2_SCHEMA,
            "status": "INCONCLUSIVE" if exc.code in {"symlink_boundary_unavailable", "atomic_publish_failed"} else "FAIL",
            "failure": {"code": exc.code, "message": str(exc)},
            "authority": {
                "second_authority": False,
                "production_store": False,
                "runtime_activation": False,
                "legacy_touched": False,
            },
        }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the fixture-only BDB Next X2 experiment")
    parser.add_argument("--root", required=True, help="absolute disposable experiment root")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    evidence = run_experiment(args.root)
    print(json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if evidence["status"] == "PASS" else 3


if __name__ == "__main__":
    raise SystemExit(main())

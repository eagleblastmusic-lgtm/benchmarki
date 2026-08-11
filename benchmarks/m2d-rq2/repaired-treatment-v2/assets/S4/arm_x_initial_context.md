# BDB vNext M2d benchmark context

BENCHMARK_ONLY
NOT_RUNTIME_AUTHORITY
NOT_LEGACY_FALLBACK
ARM X — conventional flat context

Task: Assess three designs for supplying repository-source context to a model: A, a generic accepted fragment only; B, an exact RepoSourceEvidence chain; and C, direct filesystem reads. Recommend one under the frozen architecture, explain alternatives and trade-offs, identify constraints and unknowns, and state the migration boundary. Do not edit the repository and do not propose a cutover.
Phase: INITIAL
Repository: bdb-vnext-benchmark-subject
Committed commit: 4b724eda100345969eb236f877dd46f0bb91c0cb
Committed tree: 90ddd52fd997cb67a13767145fd387f7e0ad7141
RepoView: sha256:625e76129333136da65e642c91b52693a9e2f4bc8242ff89c3143e2b9e86518d

The following exact committed source subjects are available:

## SOURCE bdb_vnext/repo_view.py
object: ace7141837283be7257067dfa8127a27a6e359ed
size_bytes: 31726
raw_sha256: sha256:e889fbee0a4d7ae195ddca1bc3c7e4f050a531b787554dcfa8e96308ea23f6a0
```text
"""Read-only, exact Git-backed RepoView primitives for the inactive vNext line.

The module deliberately owns no store, writer, runtime activation or legacy
provider import.  A ``RepositoryResource`` binds one Git object database to an
explicit logical repository identity.  ``CommittedRepoView`` then binds one
observed ref to immutable commit/tree objects; all reads are performed against
that commit and never against a moving ref or the checkout filesystem.
"""

from __future__ import annotations

import datetime as _datetime
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from bdb_shared.evidence import canonical_json_bytes, semantic_digest


REPOSITORY_RESOURCE_SCHEMA = "bdb-vnext-repository-resource-v1"
REPO_VIEW_SCHEMA = "bdb-vnext-repo-view-v1"
COMMITTED_KIND = "COMMITTED"
GIT_OBJECT_DATABASE_AUTHORITY = "git-object-database"
DEFAULT_GIT_TIMEOUT_SECONDS = 30
DEFAULT_MAX_BLOB_BYTES = 8 * 1024 * 1024
_HEX = frozenset("0123456789abcdef")
_GIT_NO_REPLACE_OBJECTS = "GIT_NO_REPLACE_OBJECTS"


class RepoViewError(ValueError):
    """Fail-closed error raised by the typed RepoView boundary."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def _git_environment() -> dict[str, str]:
    """Return the bounded environment accepted by RepoView Git reads."""

    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    environment[_GIT_NO_REPLACE_OBJECTS] = "1"
    return environment


def _required_text(value: object, *, field_name: str, max_length: int = 512) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > max_length
        or "\x00" in value
        or any(ord(char) < 32 for char in value)
    ):
        raise RepoViewError("invalid_payload", f"{field_name} must be a bounded non-empty string")
    return value


def _validate_ref(value: object) -> str:
    ref = _required_text(value, field_name="ref", max_length=1024)
    if ref.startswith("-") or any(ord(char) < 32 for char in ref):
        raise RepoViewError("invalid_ref", "Git ref is not safe for an exact read")
    return ref


def _validate_path(value: object) -> str:
    path = _required_text(value, field_name="path", max_length=4096).replace("\\", "/")
    if path.startswith("/") or path.endswith("/") or "\x00" in path:
        raise RepoViewError("unsafe_path", "Repository paths must be relative POSIX paths")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise RepoViewError("unsafe_path", "Repository paths may not contain empty or traversal segments")
    return path


def _validate_object_format(value: object) -> str:
    object_format = _required_text(value, field_name="object_format", max_length=32).lower()
    if object_format not in {"sha1", "sha256"}:
        raise RepoViewError("unsupported_object_format", "Only Git sha1 and sha256 object formats are supported")
    return object_format


def _validate_oid(value: object, *, object_format: str, field_name: str) -> str:
    oid = _required_text(value, field_name=field_name, max_length=128).lower()
    expected_length = 40 if object_format == "sha1" else 64
    if len(oid) != expected_length or any(char not in _HEX for char in oid):
        raise RepoViewError("invalid_object_id", f"{field_name} is not a {object_format} object ID")
    return oid


def _validate_max_bytes(value: object) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
        or value > DEFAULT_MAX_BLOB_BYTES
    ):
        raise RepoViewError(
            "invalid_read_limit",
            f"max_bytes must be between 0 and {DEFAULT_MAX_BLOB_BYTES}",
        )
    return value


def _absolute_existing(value: str | Path, *, field_name: str, directory: bool) -> Path:
    candidate = Path(value).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise RepoViewError("repository_unavailable", f"Unable to resolve {field_name}") from exc
    if directory and not resolved.is_dir():
        raise RepoViewError("repository_unavailable", f"{field_name} is not a directory")
    if not directory and not resolved.exists():
        raise RepoViewError("repository_unavailable", f"{field_name} does not exist")
    return resolved


def _utc_now() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical_remote(value: str) -> str:
    # Do not retain credentials if a repository has an authenticated URL.
    if "://" not in value or "@" not in value.split("://", 1)[1].split("/", 1)[0]:
        return value
    scheme, remainder = value.split("://", 1)
    _credentials, host = remainder.split("@", 1)
    return f"{scheme}://{host}"


def _canonical_path_string(value: Path) -> str:
    return str(value).replace("\\", "/")


def _parse_git_error(completed: subprocess.CompletedProcess[bytes], *, operation: str) -> RepoViewError:
    # Git diagnostics are intentionally not surfaced verbatim: they may contain
    # local absolute paths or remote URLs.  The typed error still preserves the
    # operation and return code needed by callers and tests.
    return RepoViewError(
        "git_read_failed",
        f"Git read failed during {operation}",
        details={"operation": operation, "returncode": completed.returncode},
    )


class _GitReader:
    """Small read-only Git object adapter with no legacy package dependency."""

    def __init__(self, root: Path, *, timeout_seconds: int = DEFAULT_GIT_TIMEOUT_SECONDS) -> None:
        self.root = root
        self.timeout_seconds = timeout_seconds

    def _execute(self, args: Iterable[str], *, operation: str) -> subprocess.CompletedProcess[bytes]:
        command = ["git", "--no-replace-objects", "-C", str(self.root), *args]
        try:
            return subprocess.run(
                command,
                shell=False,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
                env=_git_environment(),
            )
        except FileNotFoundError as exc:
            raise RepoViewError("git_unavailable", "git executable is not available") from exc
        except subprocess.TimeoutExpired as exc:
            raise RepoViewError("git_read_timeout", f"Git read timed out during {operation}") from exc

    def run(self, args: Iterable[str], *, operation: str, check: bool = True) -> bytes:
        completed = self._execute(args, operation=operation)
        if check and completed.returncode != 0:
            raise _parse_git_error(completed, operation=operation)
        return completed.stdout

    def optional(self, args: Iterable[str], *, operation: str) -> bytes | None:
        completed = self._execute(args, operation=operation)
        return completed.stdout if completed.returncode == 0 else None

    def top_level(self) -> Path:
        raw = self.run(["rev-parse", "--show-toplevel"], operation="repository resolution")
        return _absolute_existing(raw.decode("utf-8", errors="strict").strip(), field_name="repository root", directory=True)

    def common_dir(self, *, top_level: Path) -> Path:
        raw = self.run(["rev-parse", "--git-common-dir"], operation="Git common directory resolution")
        value = Path(raw.decode("utf-8", errors="strict").strip())
        if not value.is_absolute():
            value = top_level / value
        return _absolute_existing(value, field_name="Git common directory", directory=True)

    def object_format(self) -> str:
        raw = self.run(["rev-parse", "--show-object-format"], operation="Git object format resolution")
        return _validate_object_format(raw.decode("ascii", errors="strict").strip())

    def remotes(self) -> tuple[str, ...]:
        raw = self.optional(["config", "--get-regexp", r"^remote\..*\.url$"], operation="remote provenance read")
        if raw is None:
            return ()
        values: list[str] = []
        for line in raw.decode("utf-8", errors="strict").splitlines():
            _key, separator, value = line.partition(" ")
            if not separator or not value:
                continue
            canonical = _canonical_remote(value.strip())
            if canonical not in values:
                values.append(canonical)
        return tuple(values)

    def resolve_commit(self, ref: str, *, object_format: str) -> str:
        ref = _validate_ref(ref)
        raw = self.run(
            ["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
            operation="commit resolution",
        )
        return _validate_oid(raw.decode("ascii", errors="strict").strip(), object_format=object_format, field_name="commit_oid")

    def resolve_tree(self, commit_oid: str, *, object_format: str) -> str:
        commit_oid = _validate_oid(commit_oid, object_format=object_format, field_name="commit_oid")
        raw = self.run(
            ["rev-parse", "--verify", "--end-of-options", f"{commit_oid}^{{tree}}"],
            operation="tree resolution",
        )
        return _validate_oid(raw.decode("ascii", errors="strict").strip(), object_format=object_format, field_name="tree_oid")

    def commit_provenance(self, commit_oid: str, *, object_format: str) -> dict[str, Any]:
        commit_oid = _validate_oid(commit_oid, object_format=object_format, field_name="commit_oid")
        raw = self.run(
            ["show", "-s", "--format=%H%x00%T%x00%P%x00%aI%x00%cI", commit_oid],
            operation="commit provenance read",
        )
        fields = raw.decode("utf-8", errors="strict").rstrip("\n").split("\x00")
        if len(fields) != 5:
            raise RepoViewError("git_read_failed", "Commit provenance has an unexpected shape")
        resolved_commit, resolved_tree, parents, authored_at, committed_at = fields
        return {
            "source_authority": GIT_OBJECT_DATABASE_AUTHORITY,
            "commit_oid": _validate_oid(resolved_commit, object_format=object_format, field_name="commit_oid"),
            "tree_oid": _validate_oid(resolved_tree, object_format=object_format, field_name="tree_oid"),
            "parent_oids": tuple(
                _validate_oid(item, object_format=object_format, field_name="parent_oid")
                for item in parents.split()
            ),
            "author_timestamp": authored_at,
            "committer_timestamp": committed_at,
        }

    def list_tree(
        self,
        tree_oid: str,
        *,
        object_format: str,
        prefix: str | None = None,
    ) -> tuple["RepoTreeEntry", ...]:
        tree_oid = _validate_oid(tree_oid, object_format=object_format, field_name="tree_oid")
        args = ["ls-tree", "-r", "-z", "--long", tree_oid]
        normalized_prefix: str | None = None
        if prefix is not None:
            normalized_prefix = _validate_path(prefix)
            args.extend(["--", f":(literal){normalized_prefix}"])
        raw = self.run(args, operation="committed tree read")
        entries: list[RepoTreeEntry] = []
        for chunk in raw.split(b"\0"):
            if not chunk:
                continue
            try:
                metadata, path_bytes = chunk.split(b"\t", 1)
                mode_bytes, type_bytes, oid_bytes, size_bytes = metadata.split(b" ", 3)
                path = path_bytes.decode("utf-8", errors="strict")
                mode = mode_bytes.decode("ascii", errors="strict")
                object_type = type_bytes.decode("ascii", errors="strict")
                size_token = size_bytes.decode("ascii", errors="strict").strip()
            except (UnicodeError, ValueError) as exc:
                raise RepoViewError("malformed_tree", "Git tree entry has an unexpected shape") from exc
            path = _validate_path(path)
            object_oid = _validate_oid(oid_bytes.decode("ascii", errors="strict"), object_format=object_format, field_name="object_oid")
            if size_token == "-":
                size = 0
            else:
                try:
                    size = int(size_token)
                except ValueError as exc:
                    raise RepoViewError("malformed_tree", "Git tree entry size is invalid") from exc
                if size < 0:
                    raise RepoViewError("malformed_tree", "Git tree entry size is negative")
            file_kind = "symlink" if mode == "120000" else "submodule" if mode == "160000" or object_type == "commit" else "regular"
            if object_type not in {"blob", "commit"}:
                raise RepoViewError("unsupported_tree_entry", "Git tree entry object type is unsupported")
            entries.append(
                RepoTreeEntry(
                    path=path,
                    mode=mode,
                    object_type=object_type,
                    object_oid=object_oid,
                    size_bytes=size,
                    file_kind=file_kind,
                )
            )
        entries.sort(key=lambda item: item.path)
        if normalized_prefix is not None:
            entries = [
                entry
                for entry in entries
                if entry.path == normalized_prefix or entry.path.startswith(normalized_prefix + "/")
            ]
        return tuple(entries)

    def tree_entry(self, tree_oid: str, path: str, *, object_format: str) -> "RepoTreeEntry":
        normalized_path = _validate_path(path)
        for entry in self.list_tree(tree_oid, object_format=object_format, prefix=normalized_path):
            if entry.path == normalized_path:
                return entry
        raise RepoViewError("missing_path", f"Committed RepoView does not contain path: {normalized_path}")

    def read_blob(
        self,
        object_oid: str,
        *,
        object_format: str,
        expected_size: int,
        max_bytes: int,
    ) -> bytes:
        object_oid = _validate_oid(object_oid, object_format=object_format, field_name="object_oid")
        max_bytes = _validate_max_bytes(max_bytes)
        raw_size = self.run(["cat-file", "-s", object_oid], operation="committed blob size read")
        try:
            observed_size = int(raw_size.decode("ascii", errors="strict").strip())
        except (UnicodeError, ValueError) as exc:
            raise RepoViewError("git_read_failed", "Committed blob size has an unexpected shape") from exc
        if observed_size != expected_size:
            raise RepoViewError(
                "object_size_mismatch",
                "Committed tree entry size does not match the bound Git object",
                details={"expected_size": expected_size, "observed_size": observed_size},
            )
        if observed_size > max_bytes:
            raise RepoViewError(
                "blob_too_large",
                "Committed blob exceeds the bounded read limit",
                details={"blob_size": observed_size, "max_bytes": max_bytes},
            )
        payload = self.run(["cat-file", "blob", object_oid], operation="committed blob read")
        if len(payload) != observed_size:
            raise RepoViewError(
                "object_size_mismatch",
                "Committed blob bytes do not match the observed Git object size",
                details={"expected_size": observed_size, "observed_size": len(payload)},
            )
        return payload


def _identity_material(
    *,
    repository_id: str,
    common_dir: Path,
    object_format: str,
    remotes: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "schema": REPOSITORY_RESOURCE_SCHEMA,
        "repository_id": repository_id,
        "git_common_dir": _canonical_path_string(common_dir),
        "object_format": object_format,
        "remote_urls": list(remotes),
    }


@dataclass(frozen=True)
class RepoTreeEntry:
    """Typed immutable entry from one committed Git tree."""

    path: str
    mode: str
    object_type: str
    object_oid: str
    size_bytes: int
    file_kind: str

    @property
    def is_regular_file(self) -> bool:
        return self.file_kind == "regular" and self.object_type == "blob"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "mode": self.mode,
            "object_type": self.object_type,
            "object_oid": self.object_oid,
            "size_bytes": self.size_bytes,
            "file_kind": self.file_kind,
        }


@dataclass(frozen=True)
class RepositoryResource:
    """Exact, read-only binding to one Git repository object database."""

    schema: str
    repository_id: str
    root: str
    git_common_dir: str
    object_format: str
    remote_urls: tuple[str, ...]
    identity_digest: str

    @classmethod
    def from_path(
        cls,
        repo_path: str | Path,
        *,
        repository_id: str | None = None,
        timeout_seconds: int = DEFAULT_GIT_TIMEOUT_SECONDS,
    ) -> "RepositoryResource":
        input_path = _absolute_existing(repo_path, field_name="repo_path", directory=True)
        reader = _GitReader(input_path, timeout_seconds=timeout_seconds)
        try:
            root = reader.top_level()
            common_dir = reader.common_dir(top_level=root)
            object_format = reader.object_format()
            remotes = reader.remotes()
        except RepoViewError:
            raise
        except (OSError, UnicodeError) as exc:
            raise RepoViewError("repository_unavailable", "Repository identity could not be observed") from exc
        resolved_id = _required_text(repository_id, field_name="repository_id") if repository_id is not None else None
        material_without_id = {
            "schema": REPOSITORY_RESOURCE_SCHEMA,
            "git_common_dir": _canonical_path_string(common_dir),
            "object_format": object_format,
            "remote_urls": list(remotes),
        }
        if resolved_id is None:
            resolved_id = "repo-" + semantic_digest(material_without_id).split(":", 1)[1][:32]
        identity = semantic_digest(
            _identity_material(
                repository_id=resolved_id,
                common_dir=common_dir,
                object_format=object_format,
                remotes=remotes,
            )
        )
        return cls(
            schema=REPOSITORY_RESOURCE_SCHEMA,
            repository_id=resolved_id,
            root=_canonical_path_string(root),
            git_common_dir=_canonical_path_string(common_dir),
            object_format=object_format,
            remote_urls=remotes,
            identity_digest=identity,
        )

    inspect = from_path
    open = from_path

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "repository_id": self.repository_id,
            "root": self.root,
            "git_common_dir": self.git_common_dir,
            "object_format": self.object_format,
            "remote_urls": list(self.remote_urls),
            "identity_digest": self.identity_digest,
        }

    def _reader(self, *, timeout_seconds: int = DEFAULT_GIT_TIMEOUT_SECONDS) -> _GitReader:
        return _GitReader(Path(self.root), timeout_seconds=timeout_seconds)

    def _assert_still_bound(self, *, timeout_seconds: int = DEFAULT_GIT_TIMEOUT_SECONDS) -> _GitReader:
        reader = self._reader(timeout_seconds=timeout_seconds)
        current = RepositoryResource.from_path(self.root, repository_id=self.repository_id, timeout_seconds=timeout_seconds)
        if current.identity_digest != self.identity_digest or current.git_common_dir != self.git_common_dir:
            raise RepoViewError(
                "repository_changed",
                "Repository resource no longer matches its exact identity binding",
                details={"expected_identity_digest": self.identity_digest, "observed_identity_digest": current.identity_digest},
            )
        return reader

    def resolve_committed(
        self,
        ref: str,
        *,
        observed_at: str | None = None,
        timeout_seconds: int = DEFAULT_GIT_TIMEOUT_SECONDS,
    ) -> "CommittedRepoView":
        ref = _validate_ref(ref)
        reader = self._assert_still_bound(timeout_seconds=timeout_seconds)
        commit_oid = reader.resolve_commit(ref, object_format=self.object_format)
        tree_oid = reader.resolve_tree(commit_oid, object_format=self.object_format)
        provenance = reader.commit_provenance(commit_oid, object_format=self.object_format)
        provenance.update(
            {
                "observed_ref": ref,
                "observed_at": observed_at or _utc_now(),
                "repository_identity_digest": self.identity_digest,
            }
        )
        identity = {
            "schema": REPO_VIEW_SCHEMA,
            "kind": COMMITTED_KIND,
            "repository_id": self.repository_id,
            "repository_identity_digest": self.identity_digest,
            "object_format": self.object_format,
            "commit_oid": commit_oid,
            "tree_oid": tree_oid,
        }
        view_id = semantic_digest(identity)
        return CommittedRepoView(
            schema=REPO_VIEW_SCHEMA,
            kind=COMMITTED_KIND,
            repository_id=self.repository_id,
            repository_identity_digest=self.identity_digest,
            object_format=self.object_format,
            commit_oid=commit_oid,
            tree_oid=tree_oid,
            observed_ref=ref,
            view_id=view_id,
            provenance=dict(provenance),
            _resource=self,
        )

    resolve = resolve_committed
    committed = resolve_committed

    def query(self, view: "CommittedRepoView") -> "RepoViewQuery":
        if not isinstance(view, CommittedRepoView):
            raise RepoViewError("invalid_view", "query requires a CommittedRepoView")
        if (
            view.repository_id != self.repository_id
            or view.repository_identity_digest != self.identity_digest
            or view.object_format != self.object_format
        ):
            raise RepoViewError(
                "wrong_repository",
                "RepoView repository binding does not match this Repository resource",
                details={"expected_repository_id": self.repository_id, "observed_repository_id": view.repository_id},
            )
        view._authoritative_reader()
        return RepoViewQuery(view)

    def current_commit(self, ref: str, *, timeout_seconds: int = DEFAULT_GIT_TIMEOUT_SECONDS) -> str:
        reader = self._assert_still_bound(timeout_seconds=timeout_seconds)
        return reader.resolve_commit(_validate_ref(ref), object_format=self.object_format)


@dataclass(frozen=True)
class CommittedRepoView:
    """Immutable exact commit/tree descriptor with typed read access."""

    schema: str
    kind: str
    repository_id: str
    repository_identity_digest: str
    object_format: str
    commit_oid: str
    tree_oid: str
    observed_ref: str
    view_id: str
    provenance: Mapping[str, Any]
    # Compatibility-only projection cache. It is never consulted for path,
    # object or byte authority; exact Git tree lookup always wins.
    entries: tuple[RepoTreeEntry, ...] = field(default=(), repr=False, compare=False)
    _resource: RepositoryResource = field(repr=False, compare=False, default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.schema != REPO_VIEW_SCHEMA or self.kind != COMMITTED_KIND:
            raise RepoViewError("unsupported_view", "Only bdb-vnext COMMITTED RepoViews are supported")
        _validate_object_format(self.object_format)
        _validate_oid(self.commit_oid, object_format=self.object_format, field_name="commit_oid")
        _validate_oid(self.tree_oid, object_format=self.object_format, field_name="tree_oid")
        _required_text(self.repository_id, field_name="repository_id")
        _required_text(self.repository_identity_digest, field_name="repository_identity_digest")
        _required_text(self.observed_ref, field_name="observed_ref")
        _required_text(self.view_id, field_name="view_id")
        if self._resource is None:
            raise RepoViewError("invalid_view", "A CommittedRepoView requires its bound RepositoryResource")
        object.__setattr__(self, "provenance", MappingProxyType(dict(self.provenance)))
        object.__setattr__(self, "entries", tuple(self.entries))
        self.validate_integrity()

    @property
    def repository(self) -> RepositoryResource:
        return self._resource

    @property
    def commit_sha(self) -> str:
        return self.commit_oid

    @property
    def tree_sha(self) -> str:
        return self.tree_oid

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "kind": self.kind,
            "view_id": self.view_id,
            "repository": {
                "repository_id": self.repository_id,
                "identity_digest": self.repository_identity_digest,
                "object_format": self.object_format,
            },
            "commit_oid": self.commit_oid,
            "tree_oid": self.tree_oid,
            "observed_ref": self.observed_ref,
            "provenance": dict(self.provenance),
        }

    def to_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema": REPO_VIEW_SCHEMA,
            "kind": COMMITTED_KIND,
            "repository_id": self.repository_id,
            "repository_identity_digest": self.repository_identity_digest,
            "object_format": self.object_format,
            "commit_oid": self.commit_oid,
            "tree_oid": self.tree_oid,
        }

    def validate_integrity(self) -> None:
        if semantic_digest(self._identity_payload()) != self.view_id:
            raise RepoViewError("view_integrity_mismatch", "Committed RepoView identity digest does not match its descriptor")
        if (
            self._resource.repository_id != self.repository_id
            or self._resource.identity_digest != self.repository_identity_digest
            or self._resource.object_format != self.object_format
        ):
            raise RepoViewError("wrong_repository", "Committed RepoView is bound to a different repository resource")
        provenance = self.provenance
        if (
            provenance.get("source_authority") != GIT_OBJECT_DATABASE_AUTHORITY
            or provenance.get("commit_oid") != self.commit_oid
            or provenance.get("tree_oid") != self.tree_oid
            or provenance.get("observed_ref") != self.observed_ref
            or provenance.get("repository_identity_digest") != self.repository_identity_digest
        ):
            raise RepoViewError("view_integrity_mismatch", "Committed RepoView provenance does not match its descriptor")

    def _authoritative_reader(self) -> _GitReader:
        self.validate_integrity()
        reader = self._resource._assert_still_bound()
        observed = reader.commit_provenance(self.commit_oid, object_format=self.object_format)
        provenance = self.provenance
        if (
            observed["tree_oid"] != self.tree_oid
            or tuple(observed["parent_oids"]) != tuple(provenance.get("parent_oids", ()))
            or observed["author_timestamp"] != provenance.get("author_timestamp")
            or observed["committer_timestamp"] != provenance.get("committer_timestamp")
        ):
            raise RepoViewError(
                "commit_tree_binding_mismatch",
                "Committed RepoView metadata does not match the bound Git commit object",
            )
        return reader

    def entry(self, path: str) -> RepoTreeEntry:
        reader = self._authoritative_reader()
        return reader.tree_entry(self.tree_oid, path, object_format=self.object_format)

    def list_entries(self, prefix: str | None = None) -> tuple[RepoTreeEntry, ...]:
        reader = self._authoritative_reader()
        normalized_prefix = None if prefix is None or prefix == "" else _validate_path(prefix.rstrip("/"))
        return reader.list_tree(self.tree_oid, object_format=self.object_format, prefix=normalized_prefix)

    def read_bytes(self, path: str, *, max_bytes: int = DEFAULT_MAX_BLOB_BYTES) -> bytes:
        reader = self._authoritative_reader()
        entry = reader.tree_entry(self.tree_oid, path, object_format=self.object_format)
        if not entry.is_regular_file:
            raise RepoViewError("unsupported_path", f"Only regular committed files are readable: {entry.path}")
        return reader.read_blob(
            entry.object_oid,
            object_format=self.object_format,
            expected_size=entry.size_bytes,
            max_bytes=max_bytes,
        )

    def read_text(
        self,
        path: str,
        *,
        encoding: str = "utf-8",
        max_bytes: int = DEFAULT_MAX_BLOB_BYTES,
    ) -> str:
        try:
            return self.read_bytes(path, max_bytes=max_bytes).decode(encoding, errors="strict")
        except UnicodeDecodeError as exc:
            raise RepoViewError("not_text", f"Committed path is not valid {encoding}: {path}") from exc

    def query(self) -> "RepoViewQuery":
        return RepoViewQuery(self)

    def freshness(self, ref: str) -> "RepoViewFreshness":
        current = self._resource.current_commit(ref)
        return RepoViewFreshness(
            observed_ref=_validate_ref(ref),
            recorded_commit_oid=self.commit_oid,
            current_commit_oid=current,
            status="CURRENT" if current == self.commit_oid else "STALE",
        )

    def is_current(self, ref: str) -> bool:
        return self.freshness(ref).is_current


@dataclass(frozen=True)
class RepoViewFreshness:
    observed_ref: str
    recorded_commit_oid: str
    current_commit_oid: str
    status: str

    @property
    def is_current(self) -> bool:
        return self.status == "CURRENT"

    @property
    def is_stale(self) -> bool:
        return self.status == "STALE"


@dataclass(frozen=True)
class RepoViewQuery:
    """Typed query boundary; it cannot resolve an implicit moving ref."""

    view: CommittedRepoView

    @property
    def repository_id(self) -> str:
        return self.view.repository_id

    @property
    def commit_oid(self) -> str:
        return self.view.commit_oid

    @property
    def tree_oid(self) -> str:
        return self.view.tree_oid

    def list_entries(self, prefix: str | None = None) -> tuple[RepoTreeEntry, ...]:
        return self.view.list_entries(prefix)

    def get_entry(self, path: str) -> RepoTreeEntry:
        return self.view.entry(path)

    def read_bytes(self, path: str, *, max_bytes: int = DEFAULT_MAX_BLOB_BYTES) -> bytes:
        return self.view.read_bytes(path, max_bytes=max_bytes)

    def read_text(
        self,
        path: str,
        *,
        encoding: str = "utf-8",
        max_bytes: int = DEFAULT_MAX_BLOB_BYTES,
    ) -> str:
        return self.view.read_text(path, encoding=encoding, max_bytes=max_bytes)


__all__ = [
    "COMMITTED_KIND",
    "DEFAULT_MAX_BLOB_BYTES",
    "GIT_OBJECT_DATABASE_AUTHORITY",
    "REPO_VIEW_SCHEMA",
    "REPOSITORY_RESOURCE_SCHEMA",
    "CommittedRepoView",
    "RepoTreeEntry",
    "RepoViewError",
    "RepoViewFreshness",
    "RepoViewQuery",
    "RepositoryResource",
]

```

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

## SOURCE bdb_vnext/context_transport.py
object: e0730a288ee02784fcc4c629beafc35936b41a25
size_bytes: 10353
raw_sha256: sha256:45a00aca01654089725278083d5f174e236c5cdc6aff606f58bfbf601304f481
```text
"""Build-only exact typed-context transport for the BDB Next boundary."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from bdb_shared.evidence import canonical_json_bytes, semantic_digest
from bdb_vnext.composition import GENERATION_ID, PROTOCOL_GENERATION
from bdb_vnext.content_store import (
    ContentStoreError,
    DurableBindingStore,
    MAX_CONTEXT_FRAGMENT_BYTES,
    TypedContextFragment,
)
from bdb_vnext.repo_view import CommittedRepoView


TRANSPORT_SCHEMA = "bdb-vnext-transport-envelope-v1"
PROTOCOL_VERSION = 1
MESSAGE_KIND = "typed_context_fragment"
MAX_TRANSPORT_PAYLOAD_BYTES = MAX_CONTEXT_FRAGMENT_BYTES
MAX_TRANSPORT_ENVELOPE_BYTES = 2 * 1024 * 1024
BROWSER_PROVIDER_CONTRACT = "bdb-vnext-browser-transport-contract-v1"
NATIVE_PROVIDER_CONTRACT = "bdb-vnext-native-transport-contract-v1"
IMPLEMENTATION_IDENTITY_SCHEMA = "bdb-vnext-transport-implementation-identity-v1"
BROWSER_IMPLEMENTATION_REVISION = "bdb-vnext-browser-transport-implementation-r1"
NATIVE_IMPLEMENTATION_REVISION = "bdb-vnext-native-transport-implementation-r1"
_IMPLEMENTATION_MODULE = "bdb_vnext.context_transport"
_BROWSER_PROVIDER_ID = "devmaster.bdb.vnext.browser-transport"
_NATIVE_PROVIDER_ID = "devmaster.bdb.vnext.native-transport"


class TransportError(ValueError):
    """Typed fail-closed transport error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> None:
    raise TransportError(code, message)


def _message_id(core: dict[str, Any]) -> str:
    return f"sha256:{hashlib.sha256(canonical_json_bytes(core)).hexdigest()}"


def _provider_identity(
    provider_id: str,
    contract: str,
    implementation_module: str,
    implementation_qualname: str,
    implementation_revision: str,
) -> str:
    return semantic_digest(
        {
            "identity_schema": IMPLEMENTATION_IDENTITY_SCHEMA,
            "provider_id": provider_id,
            "provider_contract": contract,
            "provider_contract_version": PROTOCOL_VERSION,
            "protocol_generation": PROTOCOL_GENERATION,
            "implementation_module": implementation_module,
            "implementation_qualname": implementation_qualname,
            "implementation_revision": implementation_revision,
        }
    )


@dataclass(frozen=True)
class DecodedTransport:
    fragment: TypedContextFragment
    raw: bytes
    message_id: str


def encode_envelope(fragment: TypedContextFragment, raw: bytes) -> bytes:
    if not isinstance(fragment, TypedContextFragment):
        _fail("invalid_fragment", "transport encoding requires TypedContextFragment")
    if not isinstance(raw, bytes):
        _fail("invalid_payload", "transport payload must be bytes")
    if len(raw) > MAX_TRANSPORT_PAYLOAD_BYTES:
        _fail("payload_too_large", "transport payload exceeds the bounded limit")
    try:
        fragment.verify_payload(raw)
    except ContentStoreError as exc:
        raise TransportError(exc.code, str(exc)) from exc
    core: dict[str, Any] = {
        "schema": TRANSPORT_SCHEMA,
        "protocol_generation": PROTOCOL_GENERATION,
        "protocol_version": PROTOCOL_VERSION,
        "message_kind": MESSAGE_KIND,
        "fragment": fragment.as_dict(),
        "payload_length_bytes": len(raw),
        "payload_base64": base64.b64encode(raw).decode("ascii"),
    }
    document = {**core, "message_id": _message_id(core)}
    serialized = canonical_json_bytes(document)
    if len(serialized) > MAX_TRANSPORT_ENVELOPE_BYTES:
        _fail("envelope_too_large", "transport envelope exceeds the bounded limit")
    return serialized


def decode_envelope(payload: bytes) -> DecodedTransport:
    if not isinstance(payload, bytes):
        _fail("invalid_envelope", "transport envelope must be bytes")
    if len(payload) > MAX_TRANSPORT_ENVELOPE_BYTES:
        _fail("envelope_too_large", "transport envelope exceeds the bounded limit")
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TransportError("malformed_envelope", "transport envelope is not valid UTF-8 JSON") from exc
    if not isinstance(document, dict):
        _fail("malformed_envelope", "transport envelope must be a JSON object")
    required = {
        "schema",
        "protocol_generation",
        "protocol_version",
        "message_kind",
        "message_id",
        "fragment",
        "payload_length_bytes",
        "payload_base64",
    }
    if set(document) != required:
        _fail("malformed_envelope", "transport envelope has an unexpected field set")
    if canonical_json_bytes(document) != payload:
        _fail("malformed_envelope", "transport envelope is not canonical or contains trailing bytes")
    if document["schema"] != TRANSPORT_SCHEMA:
        _fail("unsupported_envelope_schema", "transport envelope schema is unsupported")
    if document["protocol_generation"] != PROTOCOL_GENERATION:
        _fail("unsupported_protocol_generation", "transport protocol generation is unsupported")
    if document["protocol_version"] != PROTOCOL_VERSION:
        _fail("unsupported_protocol_version", "transport protocol version is unsupported")
    if document["message_kind"] != MESSAGE_KIND:
        _fail("unknown_message_kind", "transport message kind is unsupported")
    message_id = document["message_id"]
    if (
        not isinstance(message_id, str)
        or len(message_id) != 71
        or not message_id.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in message_id[7:])
    ):
        _fail("message_integrity_failure", "transport message identity is malformed")
    length = document["payload_length_bytes"]
    if not isinstance(length, int) or isinstance(length, bool) or not 0 <= length <= MAX_TRANSPORT_PAYLOAD_BYTES:
        _fail("payload_too_large", "transport payload length is outside the bounded range")
    encoded = document["payload_base64"]
    if not isinstance(encoded, str):
        _fail("malformed_envelope", "transport payload encoding is malformed")
    try:
        raw = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise TransportError("malformed_payload_encoding", "transport payload is not strict base64") from exc
    if base64.b64encode(raw).decode("ascii") != encoded:
        _fail("malformed_payload_encoding", "transport payload base64 is not canonical")
    if len(raw) != length:
        _fail("payload_length_mismatch", "transport payload length does not match envelope metadata")
    core = {key: document[key] for key in required if key != "message_id"}
    if message_id != _message_id(core):
        _fail("message_integrity_failure", "transport message identity does not match exact envelope")
    try:
        fragment = TypedContextFragment.from_mapping(document["fragment"])
        fragment.verify_payload(raw)
    except ContentStoreError as exc:
        raise TransportError(exc.code, str(exc)) from exc
    return DecodedTransport(fragment, raw, message_id)


@dataclass(frozen=True)
class BrowserTransportProvider:
    """Read-only adapter that emits only an already accepted binding."""

    generation: str = GENERATION_ID
    provider_contract: str = BROWSER_PROVIDER_CONTRACT
    provider_contract_version: int = PROTOCOL_VERSION
    implementation_module: str = _IMPLEMENTATION_MODULE
    implementation_qualname: str = "BrowserTransportProvider"
    implementation_revision: str = BROWSER_IMPLEMENTATION_REVISION
    implementation_identity: str = _provider_identity(
        _BROWSER_PROVIDER_ID,
        BROWSER_PROVIDER_CONTRACT,
        _IMPLEMENTATION_MODULE,
        "BrowserTransportProvider",
        BROWSER_IMPLEMENTATION_REVISION,
    )

    def encode(
        self,
        bindings: DurableBindingStore,
        fragment: TypedContextFragment,
        *,
        expected_view: CommittedRepoView | None = None,
    ) -> bytes:
        accepted = bindings.resolve_accepted(fragment.fragment_id, expected_view=expected_view)
        if accepted.fragment != fragment:
            _fail("binding_integrity_failure", "transport fragment differs from accepted durable binding")
        return encode_envelope(fragment, accepted.raw)


@dataclass(frozen=True)
class NativeTransportProvider:
    """Read-only adapter that decodes exact envelopes and rejects unbound data."""

    generation: str = GENERATION_ID
    provider_contract: str = NATIVE_PROVIDER_CONTRACT
    provider_contract_version: int = PROTOCOL_VERSION
    implementation_module: str = _IMPLEMENTATION_MODULE
    implementation_qualname: str = "NativeTransportProvider"
    implementation_revision: str = NATIVE_IMPLEMENTATION_REVISION
    implementation_identity: str = _provider_identity(
        _NATIVE_PROVIDER_ID,
        NATIVE_PROVIDER_CONTRACT,
        _IMPLEMENTATION_MODULE,
        "NativeTransportProvider",
        NATIVE_IMPLEMENTATION_REVISION,
    )

    def decode(
        self,
        payload: bytes,
        *,
        bindings: DurableBindingStore | None = None,
        expected_view: CommittedRepoView | None = None,
    ) -> DecodedTransport:
        if bindings is None:
            _fail(
                "binding_store_required",
                "bound Native transport decoding requires a durable binding store",
            )
        decoded = decode_envelope(payload)
        accepted = bindings.resolve_accepted(
            decoded.fragment.fragment_id,
            expected_view=expected_view,
        )
        if accepted.fragment != decoded.fragment or accepted.raw != decoded.raw:
            _fail("binding_integrity_failure", "decoded envelope differs from accepted durable binding")
        return decoded


__all__ = [
    "BROWSER_PROVIDER_CONTRACT",
    "BROWSER_IMPLEMENTATION_REVISION",
    "BrowserTransportProvider",
    "DecodedTransport",
    "MESSAGE_KIND",
    "MAX_TRANSPORT_ENVELOPE_BYTES",
    "MAX_TRANSPORT_PAYLOAD_BYTES",
    "NATIVE_PROVIDER_CONTRACT",
    "NATIVE_IMPLEMENTATION_REVISION",
    "NativeTransportProvider",
    "PROTOCOL_VERSION",
    "TRANSPORT_SCHEMA",
    "TransportError",
    "decode_envelope",
    "encode_envelope",
]

```

## SOURCE bdb_vnext/engineering_intelligence.py
object: b1ab8465d8e944cfac7719a5ade321b041c299c7
size_bytes: 149421
raw_sha256: sha256:5bbe00eb63832185f15ccd0f295f3ed1f1ac71e2ce369221237503a4d10cbc91
```text
"""Build-only M2c engineering-intelligence semantic contracts.

M2c is deliberately a set of immutable, rebuildable records.  It does not
own Task identity, lifecycle state, a writer, a daemon, or repository bytes.
Exact committed RepoView and accepted M2b typed fragments remain the source
and transport authorities respectively.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, NoReturn

from bdb_shared.evidence import canonical_json_bytes, semantic_digest
from bdb_vnext.content_store import (
    AcceptedBinding,
    ContentRef,
    ContentStoreError,
    DurableBindingStore,
    ImmutableContentStore,
    RepoViewBinding,
    TypedContextFragment,
    make_content_ref,
)
from bdb_vnext.context_transport import BrowserTransportProvider, NativeTransportProvider
from bdb_vnext.repo_view import CommittedRepoView, RepoViewError


UNDERSTANDING_SCHEMA = "bdb-vnext-repository-understanding-v1"
CONTEXT_PACKAGE_SCHEMA = "bdb-vnext-context-package-v1"
CONTEXT_REQUEST_SCHEMA = "bdb-vnext-context-request-v1"
CONTEXT_RESOLUTION_SCHEMA = "bdb-vnext-context-resolution-v1"
ENGINEERING_DECISION_SCHEMA = "bdb-vnext-engineering-decision-v1"
CLAIM_SCHEMA = "bdb-vnext-understanding-claim-v1"
CONTRADICTION_SCHEMA = "bdb-vnext-understanding-contradiction-v1"
UNKNOWN_SCHEMA = "bdb-vnext-understanding-unknown-v1"
OMISSION_SCHEMA = "bdb-vnext-context-omission-v1"
AFFORDANCE_SCHEMA = "bdb-vnext-context-affordance-v1"
DECISION_OPTION_SCHEMA = "bdb-vnext-decision-option-v1"
SOURCE_EVIDENCE_SCHEMA = "bdb-vnext-source-evidence-ref-v1"
REPO_SOURCE_EVIDENCE_SCHEMA = "bdb-vnext-repo-source-evidence-v1"
COVERAGE_BINDING_SCHEMA = "bdb-vnext-coverage-binding-v1"
GAP_RESOLUTION_EVIDENCE_SCHEMA = "bdb-vnext-gap-resolution-evidence-v1"
M2C_PRODUCER_ID = "bdb-vnext-engineering-intelligence"
M2C_PRODUCER_VERSION = "m2c-v1"
M2C_POLICY_VERSION = "m2c-context-policy-v1"
SEMANTIC_RECORD_CONTENT_TYPE = "application/vnd.bdb-vnext.semantic+json"
HORIZONS = frozenset({"LOCAL", "COMPONENT", "REPOSITORY"})
CLAIM_KINDS = frozenset({"FACT", "INFERENCE", "ASSUMPTION", "HYPOTHESIS"})
CLAIM_AUTHORITIES = frozenset({"EXACT_SOURCE", "DERIVED"})
RESOLUTION_OUTCOMES = frozenset({"RESOLVED", "DENIED", "UNAVAILABLE"})
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+:-]{0,127}$")
_MAX_TEXT = 4096


class EngineeringIntelligenceError(ValueError):
    """Typed fail-closed error for M2c semantic contracts."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def _fail(code: str, message: str, *, details: Mapping[str, Any] | None = None) -> NoReturn:
    raise EngineeringIntelligenceError(code, message, details=details)


def _text(value: object, *, field: str, max_length: int = _MAX_TEXT) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        _fail("malformed_m2c_record", f"{field} must be a bounded non-empty string")
    return value


def _identifier(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        _fail("malformed_m2c_record", f"{field} must be a bounded identifier")
    return value


def _digest(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        _fail("malformed_m2c_record", f"{field} must be a lowercase sha256 digest")
    return value


def _sequence(value: object, *, field: str, allow_empty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        _fail("malformed_m2c_record", f"{field} must be an array")
    result = tuple(_text(item, field=f"{field}[]") for item in value)
    if not allow_empty and not result:
        _fail("malformed_m2c_record", f"{field} must not be empty")
    if len(set(result)) != len(result):
        _fail("duplicate_m2c_value", f"{field} must contain unique values")
    return result


def _identifier_sequence(value: object, *, field: str, allow_empty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        _fail("malformed_m2c_record", f"{field} must be an array")
    result = tuple(_identifier(item, field=f"{field}[]") for item in value)
    if not allow_empty and not result:
        _fail("malformed_m2c_record", f"{field} must not be empty")
    if len(set(result)) != len(result):
        _fail("duplicate_m2c_value", f"{field} must contain unique values")
    return result


def _mapping(value: object, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail("malformed_m2c_record", f"{field} must be an object")
    return value


def _exact_fields(value: Mapping[str, Any], required: set[str], *, field: str) -> None:
    if set(value) != required:
        _fail("malformed_m2c_record", f"{field} has an unexpected field set")


def _record_digest(identity: Mapping[str, Any]) -> str:
    return semantic_digest(identity)


def _parse_repo_binding(value: object, *, field: str = "repo_view") -> RepoViewBinding:
    if isinstance(value, RepoViewBinding):
        return value
    try:
        return RepoViewBinding.from_mapping(_mapping(value, field=field))
    except ValueError as exc:
        if isinstance(exc, EngineeringIntelligenceError):
            raise
        _fail("malformed_repo_view_basis", f"{field} is not an exact RepoView binding")


def _parse_intent(value: object) -> "IntentBasis":
    if isinstance(value, IntentBasis):
        return value
    return IntentBasis.from_mapping(_mapping(value, field="intent_basis"))


def _same_repo(left: RepoViewBinding, right: RepoViewBinding) -> bool:
    return left == right


def _binding_for_repo(repo_view: CommittedRepoView | RepoViewBinding) -> RepoViewBinding:
    binding = RepoViewBinding.from_view(repo_view) if isinstance(repo_view, CommittedRepoView) else repo_view
    if not isinstance(binding, RepoViewBinding):
        _fail("malformed_repo_view_basis", "an exact RepoView binding is required")
    return binding


def _repository_source_path(value: object) -> str:
    path = _text(value, field="source_evidence.source_path", max_length=4096)
    if (
        path.startswith("/")
        or path.endswith("/")
        or "\\" in path
        or "\x00" in path
        or any(ord(character) < 32 for character in path)
        or re.match(r"^[A-Za-z]:", path) is not None
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        _fail("unsafe_source_path", "repository source paths must be relative POSIX paths without traversal")
    return path


def _repository_source_object_id(value: object, repo_view: RepoViewBinding) -> str:
    if not isinstance(value, str):
        _fail("malformed_source_evidence", "source_object_id must be a Git object ID")
    expected_length = 40 if repo_view.object_format == "sha1" else 64
    if len(value) != expected_length or any(character not in "0123456789abcdef" for character in value):
        _fail("malformed_source_evidence", "source_object_id does not match the exact RepoView object format")
    return value


def _accepted_fragment(
    evidence: "SourceEvidenceRef",
    binding_store: DurableBindingStore,
    repo_view: CommittedRepoView | RepoViewBinding,
) -> TypedContextFragment:
    if not isinstance(binding_store, DurableBindingStore):
        _fail("source_evidence_store_required", "source evidence requires the M2b DurableBindingStore")
    expected_view = repo_view if isinstance(repo_view, CommittedRepoView) else None
    try:
        accepted = binding_store.resolve_accepted(evidence.fragment_id, expected_view=expected_view)
    except ContentStoreError as exc:
        _fail("source_evidence_unaccepted", "source evidence is not an accepted durable M2b fragment", details={"cause": exc.code})
    fragment = accepted.fragment
    if fragment.repo_view != evidence.repo_view or fragment.content_ref != evidence.content_ref:
        _fail("source_evidence_binding_mismatch", "source evidence does not match the accepted fragment ContentRef/RepoView")
    if fragment.fragment_type != evidence.fragment_type or fragment.fragment_schema != evidence.fragment_schema:
        _fail("source_evidence_binding_mismatch", "source evidence does not match the accepted fragment type/schema")
    if evidence.evidence_id != SourceEvidenceRef.from_fragment(fragment).evidence_id:
        _fail("source_evidence_integrity_failure", "source evidence identity differs from the accepted fragment")
    if fragment.repo_view != _binding_for_repo(repo_view):
        _fail("source_evidence_repo_mismatch", "source evidence is bound to a different exact RepoView")
    return fragment


def _coverage_state(
    requested_dimensions: Sequence[str],
    covered_dimensions: Sequence[str],
    must_see_categories: Sequence[str],
    covered_must_see: Sequence[str],
    unknowns: Sequence[object],
    omissions: Sequence[object],
    contradictions: Sequence[object],
    coverage_bindings: Sequence[CoverageBinding] = (),
) -> Literal["COMPLETE", "PARTIAL", "BLOCKED"]:
    missing_requested = set(requested_dimensions) - set(covered_dimensions)
    missing_must_see = set(must_see_categories) - set(covered_must_see)
    if missing_requested or missing_must_see:
        return "BLOCKED"
    if (set(covered_dimensions) or set(covered_must_see)) and not coverage_bindings:
        return "BLOCKED"
    if any(getattr(item, "policy_denied", False) for item in omissions):
        return "BLOCKED"
    if unknowns or omissions or contradictions:
        return "PARTIAL"
    return "COMPLETE"


@dataclass(frozen=True)
class IntentBasis:
    """Opaque caller-supplied intent identity; M2c never allocates Task IDs."""

    task_id: str
    intent_revision: str
    intent_digest: str

    def __post_init__(self) -> None:
        _text(self.task_id, field="task_id")
        _identifier(self.intent_revision, field="intent_revision")
        _digest(self.intent_digest, field="intent_digest")

    def as_dict(self) -> dict[str, str]:
        return {
            "task_id": self.task_id,
            "intent_revision": self.intent_revision,
            "intent_digest": self.intent_digest,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "IntentBasis":
        _exact_fields(value, {"task_id", "intent_revision", "intent_digest"}, field="intent_basis")
        return cls(value["task_id"], value["intent_revision"], value["intent_digest"])


@dataclass(frozen=True)
class SourceEvidenceRef:
    """A non-authoritative descriptor for one accepted M2b typed fragment.

    Parsing this descriptor is intentionally pure.  Only ``create_verified``
    (or ``validate`` against a live DurableBindingStore) establishes that the
    descriptor still names the exact accepted fragment for the exact RepoView.
    """

    evidence_id: str
    repo_view: RepoViewBinding
    fragment_id: str
    content_ref: ContentRef
    fragment_type: str
    fragment_schema: str

    def __post_init__(self) -> None:
        _digest(self.evidence_id, field="source_evidence.evidence_id")
        if not isinstance(self.repo_view, RepoViewBinding):
            _fail("malformed_source_evidence", "source evidence requires a typed RepoView binding")
        _digest(self.fragment_id, field="source_evidence.fragment_id")
        if not isinstance(self.content_ref, ContentRef):
            _fail("malformed_source_evidence", "source evidence requires a typed ContentRef")
        _identifier(self.fragment_type, field="source_evidence.fragment_type")
        _identifier(self.fragment_schema, field="source_evidence.fragment_schema")
        if self.evidence_id != _record_digest(self._identity_payload()):
            _fail("source_evidence_integrity_failure", "evidence_id does not match the exact fragment descriptor")

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema": SOURCE_EVIDENCE_SCHEMA,
            "repo_view": self.repo_view.as_dict(),
            "fragment_id": self.fragment_id,
            "content_ref": self.content_ref.as_dict(),
            "fragment_type": self.fragment_type,
            "fragment_schema": self.fragment_schema,
        }

    def as_dict(self) -> dict[str, Any]:
        return {"schema": SOURCE_EVIDENCE_SCHEMA, "evidence_id": self.evidence_id, **self._identity_payload()}

    @classmethod
    def from_fragment(cls, fragment: TypedContextFragment) -> "SourceEvidenceRef":
        if not isinstance(fragment, TypedContextFragment):
            _fail("malformed_source_evidence", "source evidence descriptor requires TypedContextFragment")
        identity = {
            "schema": SOURCE_EVIDENCE_SCHEMA,
            "repo_view": fragment.repo_view.as_dict(),
            "fragment_id": fragment.fragment_id,
            "content_ref": fragment.content_ref.as_dict(),
            "fragment_type": fragment.fragment_type,
            "fragment_schema": fragment.fragment_schema,
        }
        result = cls(
            _record_digest(identity),
            fragment.repo_view,
            fragment.fragment_id,
            fragment.content_ref,
            fragment.fragment_type,
            fragment.fragment_schema,
        )
        return result

    @classmethod
    def create_verified(
        cls,
        view: CommittedRepoView | RepoViewBinding,
        fragment: TypedContextFragment,
        binding_store: DurableBindingStore,
    ) -> "SourceEvidenceRef":
        descriptor = cls.from_fragment(fragment)
        _accepted_fragment(descriptor, binding_store, view)
        return descriptor

    @classmethod
    def from_accepted(
        cls,
        accepted: AcceptedBinding,
        *,
        view: CommittedRepoView | RepoViewBinding,
        binding_store: DurableBindingStore,
    ) -> "SourceEvidenceRef":
        if not isinstance(accepted, AcceptedBinding):
            _fail("malformed_source_evidence", "from_accepted requires an M2b AcceptedBinding")
        return cls.create_verified(view, accepted.fragment, binding_store)

    def validate(
        self,
        binding_store: DurableBindingStore,
        view: CommittedRepoView | RepoViewBinding,
    ) -> TypedContextFragment:
        return _accepted_fragment(self, binding_store, view)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SourceEvidenceRef":
        _exact_fields(
            value,
            {"schema", "evidence_id", "repo_view", "fragment_id", "content_ref", "fragment_type", "fragment_schema"},
            field="source_evidence",
        )
        if value["schema"] != SOURCE_EVIDENCE_SCHEMA:
            _fail("schema_mismatch", "unsupported source evidence schema")
        return cls(
            value["evidence_id"],
            _parse_repo_binding(value["repo_view"], field="source_evidence.repo_view"),
            value["fragment_id"],
            ContentRef.from_mapping(_mapping(value["content_ref"], field="source_evidence.content_ref")),
            value["fragment_type"],
            value["fragment_schema"],
        )


@dataclass(frozen=True)
class RepoSourceEvidence:
    """Exact committed-repository source evidence, not a generic M2b claim."""

    evidence_id: str
    repo_view: RepoViewBinding
    source_path: str
    source_object_id: str
    fragment_id: str
    content_ref: ContentRef
    fragment_type: str
    fragment_schema: str

    def __post_init__(self) -> None:
        _digest(self.evidence_id, field="repo_source_evidence.evidence_id")
        if not isinstance(self.repo_view, RepoViewBinding):
            _fail("malformed_source_evidence", "repository source evidence requires a typed RepoView binding")
        _repository_source_path(self.source_path)
        _repository_source_object_id(self.source_object_id, self.repo_view)
        _digest(self.fragment_id, field="repo_source_evidence.fragment_id")
        if not isinstance(self.content_ref, ContentRef):
            _fail("malformed_source_evidence", "repository source evidence requires a typed ContentRef")
        _identifier(self.fragment_type, field="repo_source_evidence.fragment_type")
        _identifier(self.fragment_schema, field="repo_source_evidence.fragment_schema")
        if self.evidence_id != _record_digest(self._identity_payload()):
            _fail("repo_source_evidence_integrity_failure", "evidence_id does not match exact repository-source identity")

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema": REPO_SOURCE_EVIDENCE_SCHEMA,
            "repo_view": self.repo_view.as_dict(),
            "source_path": self.source_path,
            "source_object_id": self.source_object_id,
            "fragment_id": self.fragment_id,
            "content_ref": self.content_ref.as_dict(),
            "fragment_type": self.fragment_type,
            "fragment_schema": self.fragment_schema,
        }

    def as_dict(self) -> dict[str, Any]:
        return {"schema": REPO_SOURCE_EVIDENCE_SCHEMA, "evidence_id": self.evidence_id, **self._identity_payload()}

    @classmethod
    def from_fragment(
        cls,
        *,
        view: CommittedRepoView,
        source_path: str,
        source_object_id: str,
        fragment: TypedContextFragment,
    ) -> "RepoSourceEvidence":
        if not isinstance(view, CommittedRepoView) or not isinstance(fragment, TypedContextFragment):
            _fail("malformed_source_evidence", "repository source evidence requires a CommittedRepoView and TypedContextFragment")
        binding = RepoViewBinding.from_view(view)
        identity = {
            "schema": REPO_SOURCE_EVIDENCE_SCHEMA,
            "repo_view": binding.as_dict(),
            "source_path": _repository_source_path(source_path),
            "source_object_id": _repository_source_object_id(source_object_id, binding),
            "fragment_id": fragment.fragment_id,
            "content_ref": fragment.content_ref.as_dict(),
            "fragment_type": fragment.fragment_type,
            "fragment_schema": fragment.fragment_schema,
        }
        if fragment.repo_view != binding:
            _fail("repository_source_repo_mismatch", "source fragment is bound to a different exact RepoView")
        return cls(
            _record_digest(identity),
            binding,
            identity["source_path"],
            identity["source_object_id"],
            fragment.fragment_id,
            fragment.content_ref,
            fragment.fragment_type,
            fragment.fragment_schema,
        )

    def validate(
        self,
        view: CommittedRepoView,
        binding_store: DurableBindingStore,
    ) -> TypedContextFragment:
        if not isinstance(view, CommittedRepoView):
            _fail("repository_source_authority_required", "repository source validation requires a CommittedRepoView")
        if not isinstance(binding_store, DurableBindingStore):
            _fail("repository_source_binding_failure", "repository source validation requires the live M2b binding store")
        expected_binding = RepoViewBinding.from_view(view)
        if self.repo_view != expected_binding:
            _fail("repository_source_repo_mismatch", "source evidence RepoView differs from the exact committed reader")
        try:
            entry = view.query().get_entry(self.source_path)
            if not entry.is_regular_file:
                _fail("repository_source_not_found", "repository source path is not a committed regular file")
            if entry.object_oid != self.source_object_id:
                _fail("repository_source_mismatch", "source object identity differs from the committed RepoView tree")
            source_bytes = view.query().read_bytes(self.source_path)
        except RepoViewError as exc:
            if exc.code in {"missing_path", "unsupported_path"}:
                _fail("repository_source_not_found", "source path is absent from the exact committed RepoView", details={"cause": exc.code})
            _fail("repository_source_mismatch", "exact committed RepoView source read failed", details={"cause": exc.code})
        try:
            accepted = binding_store.resolve_accepted(self.fragment_id, expected_view=view)
        except Exception as exc:
            code = getattr(exc, "code", "binding_failure")
            _fail("repository_source_binding_failure", "source fragment is not an accepted exact M2b binding", details={"cause": code})
        fragment = accepted.fragment
        if (
            fragment.repo_view != self.repo_view
            or fragment.content_ref != self.content_ref
            or fragment.fragment_type != self.fragment_type
            or fragment.fragment_schema != self.fragment_schema
        ):
            _fail("repository_source_binding_failure", "accepted M2b fragment differs from repository-source evidence")
        try:
            resolved_bytes = accepted.raw
            expected_ref = make_content_ref(self.content_ref.type, self.content_ref.schema, source_bytes)
        except Exception as exc:
            _fail("repository_source_mismatch", "source bytes could not be bound to ContentRef", details={"cause": type(exc).__name__})
        if expected_ref != self.content_ref or resolved_bytes != source_bytes:
            _fail("repository_source_mismatch", "committed source bytes differ from the accepted immutable M2b object")
        if self.evidence_id != _record_digest(self._identity_payload()):
            _fail("repo_source_evidence_integrity_failure", "repository-source evidence identity is not deterministic")
        return fragment

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RepoSourceEvidence":
        _exact_fields(
            value,
            {"schema", "evidence_id", "repo_view", "source_path", "source_object_id", "fragment_id", "content_ref", "fragment_type", "fragment_schema"},
            field="repo_source_evidence",
        )
        if value["schema"] != REPO_SOURCE_EVIDENCE_SCHEMA:
            _fail("schema_mismatch", "unsupported repository-source evidence schema")
        binding = _parse_repo_binding(value["repo_view"], field="repo_source_evidence.repo_view")
        return cls(
            value["evidence_id"],
            binding,
            value["source_path"],
            value["source_object_id"],
            value["fragment_id"],
            ContentRef.from_mapping(_mapping(value["content_ref"], field="repo_source_evidence.content_ref")),
            value["fragment_type"],
            value["fragment_schema"],
        )


def publish_repo_source_evidence(
    view: CommittedRepoView,
    source_path: str,
    content_store: ImmutableContentStore,
    binding_store: DurableBindingStore,
    *,
    fragment_type: str,
    fragment_schema: str,
) -> RepoSourceEvidence:
    """Read committed bytes through M2a, then publish/accept one M2c source edge."""

    if not isinstance(view, CommittedRepoView):
        _fail("repository_source_authority_required", "source publication requires a CommittedRepoView")
    if not isinstance(content_store, ImmutableContentStore) or not isinstance(binding_store, DurableBindingStore):
        _fail("repository_source_store_required", "source publication requires the M2b content and binding stores")
    normalized_path = _repository_source_path(source_path)
    query = view.query()
    try:
        entry = query.get_entry(normalized_path)
    except RepoViewError as exc:
        if exc.code in {"missing_path", "unsupported_path"}:
            _fail("repository_source_not_found", "source path is absent from the exact committed RepoView", details={"cause": exc.code})
        _fail("repository_source_mismatch", "exact committed RepoView source lookup failed", details={"cause": exc.code})
    if not entry.is_regular_file:
        _fail("repository_source_not_found", "only committed regular files may become source evidence")
    try:
        source_bytes = query.read_bytes(normalized_path)
    except RepoViewError as exc:
        if exc.code in {"missing_path", "unsupported_path"}:
            _fail("repository_source_not_found", "source path is absent from the exact committed RepoView", details={"cause": exc.code})
        _fail("repository_source_mismatch", "exact committed RepoView source read failed", details={"cause": exc.code})
    content_ref = make_content_ref(fragment_type, fragment_schema, source_bytes)
    content_store.publish(content_ref, source_bytes)
    fragment = TypedContextFragment.create(
        view,
        content_ref,
        fragment_type=fragment_type,
        fragment_schema=fragment_schema,
        payload_size_bytes=len(source_bytes),
    )
    binding_store.accept(fragment, view=view)
    evidence = RepoSourceEvidence.from_fragment(
        view=view,
        source_path=normalized_path,
        source_object_id=entry.object_oid,
        fragment=fragment,
    )
    evidence.validate(view, binding_store)
    return evidence


@dataclass(frozen=True)
class CoverageBinding:
    """Deterministic proof that one covered target has explicit support."""

    coverage_binding_id: str
    repo_view: RepoViewBinding
    target_kind: str
    target: str
    supporting_claim_ids: tuple[str, ...]
    supporting_fragment_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _digest(self.coverage_binding_id, field="coverage_binding.coverage_binding_id")
        if not isinstance(self.repo_view, RepoViewBinding):
            _fail("malformed_coverage_binding", "coverage binding requires a typed RepoView")
        if self.target_kind not in {"DIMENSION", "MUST_SEE"}:
            _fail("coverage_target_invalid", "coverage binding target_kind must be DIMENSION or MUST_SEE")
        _identifier(self.target, field="coverage_binding.target")
        _digest_sequence(self.supporting_claim_ids, field="coverage_binding.supporting_claim_ids")
        _digest_sequence(self.supporting_fragment_ids, field="coverage_binding.supporting_fragment_ids")
        if not self.supporting_claim_ids and not self.supporting_fragment_ids:
            _fail("coverage_grounding_required", "every covered target requires explicit claim or fragment support")
        if self.coverage_binding_id != _record_digest(self._identity_payload()):
            _fail("coverage_binding_integrity_failure", "coverage_binding_id does not match exact binding identity")

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema": COVERAGE_BINDING_SCHEMA,
            "repo_view": self.repo_view.as_dict(),
            "target_kind": self.target_kind,
            "target": self.target,
            "supporting_claim_ids": list(self.supporting_claim_ids),
            "supporting_fragment_ids": list(self.supporting_fragment_ids),
        }

    def as_dict(self) -> dict[str, Any]:
        return {"schema": COVERAGE_BINDING_SCHEMA, "coverage_binding_id": self.coverage_binding_id, **self._identity_payload()}

    @classmethod
    def create(
        cls,
        repo_view: CommittedRepoView | RepoViewBinding,
        *,
        target_kind: str,
        target: str,
        supporting_claim_ids: Sequence[str] = (),
        supporting_fragment_ids: Sequence[str] = (),
    ) -> "CoverageBinding":
        binding = _binding_for_repo(repo_view)
        identity = {
            "schema": COVERAGE_BINDING_SCHEMA,
            "repo_view": binding.as_dict(),
            "target_kind": target_kind,
            "target": target,
            "supporting_claim_ids": list(supporting_claim_ids),
            "supporting_fragment_ids": list(supporting_fragment_ids),
        }
        return cls(
            _record_digest(identity),
            binding,
            target_kind,
            target,
            tuple(supporting_claim_ids),
            tuple(supporting_fragment_ids),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CoverageBinding":
        _exact_fields(
            value,
            {"schema", "coverage_binding_id", "repo_view", "target_kind", "target", "supporting_claim_ids", "supporting_fragment_ids"},
            field="coverage_binding",
        )
        if value["schema"] != COVERAGE_BINDING_SCHEMA:
            _fail("schema_mismatch", "unsupported coverage binding schema")
        return cls(
            value["coverage_binding_id"],
            _parse_repo_binding(value["repo_view"], field="coverage_binding.repo_view"),
            value["target_kind"],
            value["target"],
            _digest_sequence(value["supporting_claim_ids"], field="coverage_binding.supporting_claim_ids"),
            _digest_sequence(value["supporting_fragment_ids"], field="coverage_binding.supporting_fragment_ids"),
        )


@dataclass(frozen=True)
class UnderstandingClaim:
    """One epistemic claim explicitly separated from exact source authority."""

    claim_id: str
    repo_view: RepoViewBinding
    subject: str
    dimension: str
    kind: str
    authority: str
    statement: str
    evidence_refs: tuple[str, ...]
    basis_refs: tuple[str, ...]
    producer_id: str = M2C_PRODUCER_ID
    producer_version: str = M2C_PRODUCER_VERSION
    source_evidence: tuple[SourceEvidenceRef | RepoSourceEvidence, ...] = ()

    def __post_init__(self) -> None:
        _digest(self.claim_id, field="claim_id")
        if not isinstance(self.repo_view, RepoViewBinding):
            _fail("malformed_claim", "claim repo_view must be RepoViewBinding")
        _text(self.subject, field="claim.subject")
        _identifier(self.dimension, field="claim.dimension")
        if self.kind not in CLAIM_KINDS:
            _fail("claim_kind_invalid", f"unsupported claim kind: {self.kind}")
        if self.authority not in CLAIM_AUTHORITIES:
            _fail("claim_authority_invalid", f"unsupported claim authority: {self.authority}")
        if self.kind == "FACT" and self.authority != "EXACT_SOURCE":
            _fail("claim_authority_mismatch", "FACT claims must be exact-source claims")
        if self.kind != "FACT" and self.authority != "DERIVED":
            _fail("claim_authority_mismatch", "non-FACT claims must remain visibly derived")
        _text(self.statement, field="claim.statement")
        if not self.evidence_refs and self.kind in {"FACT", "INFERENCE"}:
            _fail("claim_evidence_required", "FACT and INFERENCE claims require evidence references")
        if any(not isinstance(item, (SourceEvidenceRef, RepoSourceEvidence)) for item in self.source_evidence):
            _fail("malformed_claim", "claim source_evidence must contain typed evidence records")
        if self.kind == "FACT":
            if not self.source_evidence:
                _fail("repository_source_evidence_required", "FACT claims require repository-source evidence")
            expected_refs = tuple(item.evidence_id for item in self.source_evidence)
            if tuple(self.evidence_refs) != expected_refs:
                _fail("source_evidence_binding_mismatch", "FACT evidence_refs must exactly name source_evidence IDs")
        elif self.source_evidence:
            _fail("claim_authority_mismatch", "only FACT claims may carry source evidence")
        for reference in (*self.evidence_refs, *self.basis_refs):
            _text(reference, field="claim.reference", max_length=512)
        _identifier(self.producer_id, field="claim.producer_id")
        _identifier(self.producer_version, field="claim.producer_version")
        if self.claim_id != _record_digest(self._identity_payload()):
            _fail("claim_integrity_failure", "claim_id does not match its semantic identity")

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema": CLAIM_SCHEMA,
            "repo_view": self.repo_view.as_dict(),
            "subject": self.subject,
            "dimension": self.dimension,
            "kind": self.kind,
            "authority": self.authority,
            "statement": self.statement,
            "evidence_refs": list(self.evidence_refs),
            "basis_refs": list(self.basis_refs),
            "producer_id": self.producer_id,
            "producer_version": self.producer_version,
            "source_evidence": [item.as_dict() for item in self.source_evidence],
        }

    def as_dict(self) -> dict[str, Any]:
        return {"schema": CLAIM_SCHEMA, "claim_id": self.claim_id, **self._identity_payload()}

    @classmethod
    def create(
        cls,
        repo_view: CommittedRepoView | RepoViewBinding,
        *,
        subject: str,
        dimension: str,
        kind: str,
        statement: str,
        evidence_refs: Sequence[str] = (),
        basis_refs: Sequence[str] = (),
        source_evidence: Sequence[SourceEvidenceRef | RepoSourceEvidence] = (),
        source_evidence_refs: Sequence[SourceEvidenceRef | RepoSourceEvidence] | None = None,
        binding_store: DurableBindingStore | None = None,
        producer_id: str = M2C_PRODUCER_ID,
        producer_version: str = M2C_PRODUCER_VERSION,
    ) -> "UnderstandingClaim":
        binding = RepoViewBinding.from_view(repo_view) if isinstance(repo_view, CommittedRepoView) else repo_view
        if not isinstance(binding, RepoViewBinding):
            _fail("malformed_repo_view_basis", "claim requires an exact RepoView binding")
        evidence_items = tuple(source_evidence_refs if source_evidence_refs is not None else source_evidence)
        if kind == "FACT":
            if not evidence_items:
                _fail("repository_source_evidence_required", "FACT claims require repository-source evidence")
            if binding_store is None:
                _fail("repository_source_store_required", "FACT claims require M2a and M2b source validation")
            for item in evidence_items:
                if not isinstance(item, RepoSourceEvidence):
                    _fail("repository_source_evidence_required", "generic M2b SourceEvidenceRef cannot become FACT authority")
                if not isinstance(repo_view, CommittedRepoView):
                    _fail("repository_source_authority_required", "FACT creation requires the exact CommittedRepoView reader")
                item.validate(repo_view, binding_store)
            normalized_refs = tuple(item.evidence_id for item in evidence_items)
            if evidence_refs and tuple(evidence_refs) not in {normalized_refs, tuple(item.fragment_id for item in evidence_items)}:
                _fail("source_evidence_binding_mismatch", "FACT evidence_refs must match verified source evidence IDs")
            evidence_refs = normalized_refs
        elif evidence_items:
            _fail("claim_authority_mismatch", "only FACT claims may carry source evidence")
        identity = {
            "schema": CLAIM_SCHEMA,
            "repo_view": binding.as_dict(),
            "subject": subject,
            "dimension": dimension,
            "kind": kind,
            "authority": "EXACT_SOURCE" if kind == "FACT" else "DERIVED",
            "statement": statement,
            "evidence_refs": list(evidence_refs),
            "basis_refs": list(basis_refs),
            "producer_id": producer_id,
            "producer_version": producer_version,
            "source_evidence": [item.as_dict() for item in evidence_items],
        }
        return cls(
            _record_digest(identity),
            binding,
            subject,
            dimension,
            kind,
            identity["authority"],
            statement,
            tuple(evidence_refs),
            tuple(basis_refs),
            producer_id,
            producer_version,
            evidence_items,
        )

    def validate_source_grounding(
        self,
        binding_store: DurableBindingStore,
        repo_view: CommittedRepoView | RepoViewBinding,
    ) -> None:
        if self.kind != "FACT":
            return
        if not self.source_evidence:
            _fail("repository_source_evidence_required", "FACT claims require repository-source evidence")
        for item in self.source_evidence:
            if not isinstance(item, RepoSourceEvidence) or not isinstance(repo_view, CommittedRepoView):
                _fail("repository_source_authority_required", "FACT grounding requires RepoSourceEvidence and CommittedRepoView")
            item.validate(repo_view, binding_store)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "UnderstandingClaim":
        _exact_fields(
            value,
            {
                "schema",
                "claim_id",
                "repo_view",
                "subject",
                "dimension",
                "kind",
                "authority",
                "statement",
                "evidence_refs",
                "basis_refs",
                "producer_id",
                "producer_version",
                "source_evidence",
            },
            field="claim",
        )
        if value["schema"] != CLAIM_SCHEMA:
            _fail("schema_mismatch", "unsupported claim schema")
        return cls(
            value["claim_id"],
            _parse_repo_binding(value["repo_view"]),
            value["subject"],
            value["dimension"],
            value["kind"],
            value["authority"],
            value["statement"],
            _sequence(value["evidence_refs"], field="claim.evidence_refs"),
            _sequence(value["basis_refs"], field="claim.basis_refs"),
            value["producer_id"],
            value["producer_version"],
            tuple(
                RepoSourceEvidence.from_mapping(item)
                if isinstance(item, Mapping) and item.get("schema") == REPO_SOURCE_EVIDENCE_SCHEMA
                else SourceEvidenceRef.from_mapping(item)
                for item in value["source_evidence"]
            ),
        )


@dataclass(frozen=True)
class Unknown:
    unknown_id: str
    repo_view: RepoViewBinding
    subject: str
    dimension: str
    reason: str
    material: bool = True
    producer_id: str = M2C_PRODUCER_ID
    producer_version: str = M2C_PRODUCER_VERSION

    def __post_init__(self) -> None:
        _digest(self.unknown_id, field="unknown_id")
        if not isinstance(self.repo_view, RepoViewBinding):
            _fail("malformed_unknown", "unknown repo_view must be RepoViewBinding")
        _text(self.subject, field="unknown.subject")
        _identifier(self.dimension, field="unknown.dimension")
        _text(self.reason, field="unknown.reason")
        if not isinstance(self.material, bool):
            _fail("malformed_unknown", "unknown.material must be boolean")
        _identifier(self.producer_id, field="unknown.producer_id")
        _identifier(self.producer_version, field="unknown.producer_version")
        if self.unknown_id != _record_digest(self._identity_payload()):
            _fail("unknown_integrity_failure", "unknown_id does not match its semantic identity")

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema": UNKNOWN_SCHEMA,
            "repo_view": self.repo_view.as_dict(),
            "subject": self.subject,
            "dimension": self.dimension,
            "reason": self.reason,
            "material": self.material,
            "producer_id": self.producer_id,
            "producer_version": self.producer_version,
        }

    def as_dict(self) -> dict[str, Any]:
        return {"schema": UNKNOWN_SCHEMA, "unknown_id": self.unknown_id, **self._identity_payload()}

    @classmethod
    def create(
        cls,
        repo_view: CommittedRepoView | RepoViewBinding,
        *,
        subject: str,
        dimension: str,
        reason: str,
        material: bool = True,
    ) -> "Unknown":
        binding = RepoViewBinding.from_view(repo_view) if isinstance(repo_view, CommittedRepoView) else repo_view
        if not isinstance(binding, RepoViewBinding):
            _fail("malformed_repo_view_basis", "unknown requires an exact RepoView binding")
        identity = {
            "schema": UNKNOWN_SCHEMA,
            "repo_view": binding.as_dict(),
            "subject": subject,
            "dimension": dimension,
            "reason": reason,
            "material": material,
            "producer_id": M2C_PRODUCER_ID,
            "producer_version": M2C_PRODUCER_VERSION,
        }
        return cls(_record_digest(identity), binding, subject, dimension, reason, material)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Unknown":
        _exact_fields(
            value,
            {"schema", "unknown_id", "repo_view", "subject", "dimension", "reason", "material", "producer_id", "producer_version"},
            field="unknown",
        )
        if value["schema"] != UNKNOWN_SCHEMA:
            _fail("schema_mismatch", "unsupported unknown schema")
        return cls(
            value["unknown_id"],
            _parse_repo_binding(value["repo_view"]),
            value["subject"],
            value["dimension"],
            value["reason"],
            value["material"],
            value["producer_id"],
            value["producer_version"],
        )


@dataclass(frozen=True)
class Omission:
    omission_id: str
    repo_view: RepoViewBinding
    dimension: str
    reason: str
    policy_denied: bool = False
    producer_id: str = M2C_PRODUCER_ID
    producer_version: str = M2C_PRODUCER_VERSION

    def __post_init__(self) -> None:
        _digest(self.omission_id, field="omission_id")
        if not isinstance(self.repo_view, RepoViewBinding):
            _fail("malformed_omission", "omission repo_view must be RepoViewBinding")
        _identifier(self.dimension, field="omission.dimension")
        _text(self.reason, field="omission.reason")
        if not isinstance(self.policy_denied, bool):
            _fail("malformed_omission", "omission.policy_denied must be boolean")
        _identifier(self.producer_id, field="omission.producer_id")
        _identifier(self.producer_version, field="omission.producer_version")
        if self.omission_id != _record_digest(self._identity_payload()):
            _fail("omission_integrity_failure", "omission_id does not match its semantic identity")

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema": OMISSION_SCHEMA,
            "repo_view": self.repo_view.as_dict(),
            "dimension": self.dimension,
            "reason": self.reason,
            "policy_denied": self.policy_denied,
            "producer_id": self.producer_id,
            "producer_version": self.producer_version,
        }

    def as_dict(self) -> dict[str, Any]:
        return {"schema": OMISSION_SCHEMA, "omission_id": self.omission_id, **self._identity_payload()}

    @classmethod
    def create(
        cls,
        repo_view: CommittedRepoView | RepoViewBinding,
        *,
        dimension: str,
        reason: str,
        policy_denied: bool = False,
    ) -> "Omission":
        binding = RepoViewBinding.from_view(repo_view) if isinstance(repo_view, CommittedRepoView) else repo_view
        if not isinstance(binding, RepoViewBinding):
            _fail("malformed_repo_view_basis", "omission requires an exact RepoView binding")
        identity = {
            "schema": OMISSION_SCHEMA,
            "repo_view": binding.as_dict(),
            "dimension": dimension,
            "reason": reason,
            "policy_denied": policy_denied,
            "producer_id": M2C_PRODUCER_ID,
            "producer_version": M2C_PRODUCER_VERSION,
        }
        return cls(_record_digest(identity), binding, dimension, reason, policy_denied)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Omission":
        _exact_fields(
            value,
            {"schema", "omission_id", "repo_view", "dimension", "reason", "policy_denied", "producer_id", "producer_version"},
            field="omission",
        )
        if value["schema"] != OMISSION_SCHEMA:
            _fail("schema_mismatch", "unsupported omission schema")
        return cls(
            value["omission_id"],
            _parse_repo_binding(value["repo_view"]),
            value["dimension"],
            value["reason"],
            value["policy_denied"],
            value["producer_id"],
            value["producer_version"],
        )


@dataclass(frozen=True)
class ClaimContradiction:
    contradiction_id: str
    repo_view: RepoViewBinding
    subject: str
    dimension: str
    claim_ids: tuple[str, ...]
    source_claim_ids: tuple[str, ...]
    derived_claim_ids: tuple[str, ...]
    reason: str
    source_authority: str
    producer_id: str = M2C_PRODUCER_ID
    producer_version: str = M2C_PRODUCER_VERSION

    def __post_init__(self) -> None:
        _digest(self.contradiction_id, field="contradiction_id")
        if not isinstance(self.repo_view, RepoViewBinding):
            _fail("malformed_contradiction", "contradiction repo_view must be RepoViewBinding")
        _text(self.subject, field="contradiction.subject")
        _identifier(self.dimension, field="contradiction.dimension")
        for field, values in (
            ("contradiction.claim_ids", self.claim_ids),
            ("contradiction.source_claim_ids", self.source_claim_ids),
            ("contradiction.derived_claim_ids", self.derived_claim_ids),
        ):
            if not values or len(set(values)) != len(values):
                _fail("malformed_contradiction", f"{field} must contain unique claim IDs")
            for value in values:
                _digest(value, field=f"{field}[]")
        if not set(self.source_claim_ids).issubset(self.claim_ids) or not set(self.derived_claim_ids).issubset(self.claim_ids):
            _fail("malformed_contradiction", "contradiction source/derived claims must be members of claim_ids")
        if set(self.source_claim_ids) & set(self.derived_claim_ids):
            _fail("malformed_contradiction", "a claim cannot be both source and derived")
        expected_authority = "EXACT_SOURCE_WINS" if self.source_claim_ids else "NO_EXACT_SOURCE_CLAIM"
        if self.source_authority != expected_authority:
            _fail("source_authority_mismatch", "contradiction source authority is not explicit")
        _text(self.reason, field="contradiction.reason")
        _identifier(self.producer_id, field="contradiction.producer_id")
        _identifier(self.producer_version, field="contradiction.producer_version")
        if self.contradiction_id != _record_digest(self._identity_payload()):
            _fail("contradiction_integrity_failure", "contradiction_id does not match its semantic identity")

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema": CONTRADICTION_SCHEMA,
            "repo_view": self.repo_view.as_dict(),
            "subject": self.subject,
            "dimension": self.dimension,
            "claim_ids": list(self.claim_ids),
            "source_claim_ids": list(self.source_claim_ids),
            "derived_claim_ids": list(self.derived_claim_ids),
            "reason": self.reason,
            "source_authority": self.source_authority,
            "producer_id": self.producer_id,
            "producer_version": self.producer_version,
        }

    def as_dict(self) -> dict[str, Any]:
        return {"schema": CONTRADICTION_SCHEMA, "contradiction_id": self.contradiction_id, **self._identity_payload()}

    @classmethod
    def create(
        cls,
        repo_view: CommittedRepoView | RepoViewBinding,
        *,
        subject: str,
        dimension: str,
        claim_ids: Sequence[str],
        source_claim_ids: Sequence[str] = (),
        derived_claim_ids: Sequence[str] = (),
        reason: str,
    ) -> "ClaimContradiction":
        binding = RepoViewBinding.from_view(repo_view) if isinstance(repo_view, CommittedRepoView) else repo_view
        if not isinstance(binding, RepoViewBinding):
            _fail("malformed_repo_view_basis", "contradiction requires an exact RepoView binding")
        source = tuple(source_claim_ids)
        identity = {
            "schema": CONTRADICTION_SCHEMA,
            "repo_view": binding.as_dict(),
            "subject": subject,
            "dimension": dimension,
            "claim_ids": list(claim_ids),
            "source_claim_ids": list(source),
            "derived_claim_ids": list(derived_claim_ids),
            "reason": reason,
            "source_authority": "EXACT_SOURCE_WINS" if source else "NO_EXACT_SOURCE_CLAIM",
            "producer_id": M2C_PRODUCER_ID,
            "producer_version": M2C_PRODUCER_VERSION,
        }
        return cls(
            _record_digest(identity),
            binding,
            subject,
            dimension,
            tuple(claim_ids),
            source,
            tuple(derived_claim_ids),
            reason,
            identity["source_authority"],
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ClaimContradiction":
        _exact_fields(
            value,
            {"schema", "contradiction_id", "repo_view", "subject", "dimension", "claim_ids", "source_claim_ids", "derived_claim_ids", "reason", "source_authority", "producer_id", "producer_version"},
            field="contradiction",
        )
        if value["schema"] != CONTRADICTION_SCHEMA:
            _fail("schema_mismatch", "unsupported contradiction schema")
        return cls(
            value["contradiction_id"],
            _parse_repo_binding(value["repo_view"]),
            value["subject"],
            value["dimension"],
            _sequence(value["claim_ids"], field="contradiction.claim_ids", allow_empty=False),
            _sequence(value["source_claim_ids"], field="contradiction.source_claim_ids"),
            _sequence(value["derived_claim_ids"], field="contradiction.derived_claim_ids", allow_empty=False),
            value["reason"],
            value["source_authority"],
            value["producer_id"],
            value["producer_version"],
        )


@dataclass(frozen=True)
class ContextAffordance:
    """An on-demand semantic affordance, not a transport retry record."""

    affordance_id: str
    dimension: str
    horizon: str
    evidence_type: str
    reason: str
    producer_id: str = M2C_PRODUCER_ID
    producer_version: str = M2C_PRODUCER_VERSION

    def __post_init__(self) -> None:
        _digest(self.affordance_id, field="affordance_id")
        _identifier(self.dimension, field="affordance.dimension")
        if self.horizon not in HORIZONS:
            _fail("horizon_invalid", f"unsupported affordance horizon: {self.horizon}")
        _identifier(self.evidence_type, field="affordance.evidence_type")
        _text(self.reason, field="affordance.reason")
        _identifier(self.producer_id, field="affordance.producer_id")
        _identifier(self.producer_version, field="affordance.producer_version")
        if self.affordance_id != _record_digest(self._identity_payload()):
            _fail("affordance_integrity_failure", "affordance_id does not match its semantic identity")

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema": AFFORDANCE_SCHEMA,
            "dimension": self.dimension,
            "horizon": self.horizon,
            "evidence_type": self.evidence_type,
            "reason": self.reason,
            "producer_id": self.producer_id,
            "producer_version": self.producer_version,
        }

    def as_dict(self) -> dict[str, Any]:
        return {"schema": AFFORDANCE_SCHEMA, "affordance_id": self.affordance_id, **self._identity_payload()}

    @classmethod
    def create(cls, *, dimension: str, horizon: str, evidence_type: str, reason: str) -> "ContextAffordance":
        identity = {
            "schema": AFFORDANCE_SCHEMA,
            "dimension": dimension,
            "horizon": horizon,
            "evidence_type": evidence_type,
            "reason": reason,
            "producer_id": M2C_PRODUCER_ID,
            "producer_version": M2C_PRODUCER_VERSION,
        }
        return cls(_record_digest(identity), dimension, horizon, evidence_type, reason)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ContextAffordance":
        _exact_fields(
            value,
            {"schema", "affordance_id", "dimension", "horizon", "evidence_type", "reason", "producer_id", "producer_version"},
            field="affordance",
        )
        if value["schema"] != AFFORDANCE_SCHEMA:
            _fail("schema_mismatch", "unsupported affordance schema")
        return cls(
            value["affordance_id"],
            value["dimension"],
            value["horizon"],
            value["evidence_type"],
            value["reason"],
            value["producer_id"],
            value["producer_version"],
        )


def _validate_coverage_bindings(
    bindings: Sequence[CoverageBinding],
    *,
    repo_view: RepoViewBinding,
    claims: Sequence[UnderstandingClaim],
    allowed_claim_ids: Sequence[str] | None = None,
    requested_dimensions: Sequence[str],
    covered_dimensions: Sequence[str],
    must_see_categories: Sequence[str],
    covered_must_see: Sequence[str],
    binding_store: DurableBindingStore | None = None,
    committed_view: CommittedRepoView | None = None,
    included_fragment_ids: Sequence[str] = (),
    allowed_fragment_ids: Sequence[str] | None = None,
) -> None:
    _validate_unique_records(bindings, "coverage_binding_id", field="coverage_bindings")
    claim_by_id = {claim.claim_id: claim for claim in claims}
    allowed_claim_set = set(allowed_claim_ids if allowed_claim_ids is not None else claim_by_id)
    expected_targets = {("DIMENSION", item) for item in covered_dimensions} | {
        ("MUST_SEE", item) for item in covered_must_see
    }
    observed_targets: set[tuple[str, str]] = set()
    for binding in bindings:
        if not isinstance(binding, CoverageBinding) or binding.repo_view != repo_view:
            _fail("coverage_basis_mismatch", "coverage bindings must bind the exact RepoView")
        target = (binding.target_kind, binding.target)
        if target not in expected_targets:
            _fail("coverage_overclaim", "coverage binding targets an uncovered or unrequested target")
        if target in observed_targets:
            _fail("duplicate_m2c_value", "coverage targets must have one deterministic binding")
        observed_targets.add(target)
        if not set(binding.supporting_claim_ids).issubset(allowed_claim_set):
            _fail("coverage_claim_missing", "coverage binding names a claim outside the Understanding")
        for claim_id in binding.supporting_claim_ids:
            claim = claim_by_id.get(claim_id)
            if claim is not None and claim.repo_view != repo_view:
                _fail("coverage_basis_mismatch", "coverage supporting claim has a foreign RepoView")
        for fragment_id in binding.supporting_fragment_ids:
            if included_fragment_ids and fragment_id not in set(included_fragment_ids):
                _fail("coverage_fragment_missing", "coverage binding fragment is not included in the ContextPackage")
            if allowed_fragment_ids is not None and fragment_id not in set(allowed_fragment_ids):
                _fail("coverage_fragment_missing", "coverage binding fragment is not part of the real Understanding evidence")
            if binding_store is None:
                # Pure record parsing remains possible, but it does not confer
                # authority.  Authority-sensitive construction validates the
                # same edge again with a live binding store below.
                continue
            try:
                accepted = binding_store.resolve_accepted(
                    fragment_id,
                    expected_view=committed_view,
                )
            except ContentStoreError as exc:
                _fail("coverage_fragment_unaccepted", "coverage fragment is not an accepted durable M2b binding", details={"cause": exc.code})
            if accepted.fragment.repo_view != repo_view:
                _fail("coverage_basis_mismatch", "coverage fragment has a foreign RepoView")
    if observed_targets != expected_targets:
        _fail("coverage_grounding_required", "every covered dimension and must-see target needs a CoverageBinding")


@dataclass(frozen=True)
class RepositoryUnderstandingView:
    """Rebuildable exact-RepoView claim projection, never source-byte authority."""

    understanding_id: str
    intent_basis: IntentBasis
    repo_view: RepoViewBinding
    claims: tuple[UnderstandingClaim, ...]
    requested_dimensions: tuple[str, ...]
    covered_dimensions: tuple[str, ...]
    must_see_categories: tuple[str, ...]
    covered_must_see: tuple[str, ...]
    coverage_bindings: tuple[CoverageBinding, ...]
    unknowns: tuple[Unknown, ...]
    omissions: tuple[Omission, ...]
    contradictions: tuple[ClaimContradiction, ...]
    invalidation_predicates: tuple[str, ...]
    producer_id: str = M2C_PRODUCER_ID
    producer_version: str = M2C_PRODUCER_VERSION

    def __post_init__(self) -> None:
        _digest(self.understanding_id, field="understanding_id")
        if not isinstance(self.intent_basis, IntentBasis) or not isinstance(self.repo_view, RepoViewBinding):
            _fail("malformed_understanding", "understanding requires typed intent and RepoView basis")
        _validate_unique_records(self.claims, "claim_id", field="understanding.claims")
        _validate_unique_records(self.unknowns, "unknown_id", field="understanding.unknowns")
        _validate_unique_records(self.omissions, "omission_id", field="understanding.omissions")
        _validate_unique_records(self.contradictions, "contradiction_id", field="understanding.contradictions")
        if any(not isinstance(item, CoverageBinding) for item in self.coverage_bindings):
            _fail("malformed_coverage_binding", "understanding coverage_bindings must be typed")
        for claim in self.claims:
            if not isinstance(claim, UnderstandingClaim) or not _same_repo(claim.repo_view, self.repo_view):
                _fail("understanding_basis_mismatch", "all claims must bind the exact Understanding RepoView")
        for unknown in self.unknowns:
            if not isinstance(unknown, Unknown) or not _same_repo(unknown.repo_view, self.repo_view):
                _fail("understanding_basis_mismatch", "all unknowns must bind the exact Understanding RepoView")
        for omission in self.omissions:
            if not isinstance(omission, Omission) or not _same_repo(omission.repo_view, self.repo_view):
                _fail("understanding_basis_mismatch", "all omissions must bind the exact Understanding RepoView")
        claim_by_id = {claim.claim_id: claim for claim in self.claims}
        for contradiction in self.contradictions:
            if not isinstance(contradiction, ClaimContradiction) or not _same_repo(contradiction.repo_view, self.repo_view):
                _fail("understanding_basis_mismatch", "contradictions must bind the exact Understanding RepoView")
            if not set(contradiction.claim_ids).issubset(claim_by_id):
                _fail("contradiction_claim_missing", "contradiction references a claim not present in Understanding")
            for claim_id in contradiction.source_claim_ids:
                if claim_by_id[claim_id].kind != "FACT" or claim_by_id[claim_id].authority != "EXACT_SOURCE":
                    _fail("source_authority_mismatch", "source contradiction claims must be exact FACT claims")
            for claim_id in contradiction.derived_claim_ids:
                if claim_by_id[claim_id].kind == "FACT" or claim_by_id[claim_id].authority != "DERIVED":
                    _fail("source_authority_mismatch", "derived contradiction claims cannot be FACT claims")
        _validate_coverage_fields(
            self.requested_dimensions,
            self.covered_dimensions,
            self.must_see_categories,
            self.covered_must_see,
        )
        _validate_coverage_bindings(
            self.coverage_bindings,
            repo_view=self.repo_view,
            claims=self.claims,
            requested_dimensions=self.requested_dimensions,
            covered_dimensions=self.covered_dimensions,
            must_see_categories=self.must_see_categories,
            covered_must_see=self.covered_must_see,
        )
        if not self.invalidation_predicates:
            _fail("invalidation_predicates_required", "Understanding must expose invalidation predicates")
        _identifier(self.producer_id, field="understanding.producer_id")
        _identifier(self.producer_version, field="understanding.producer_version")
        self._validate_conflicts()
        if self.understanding_id != _record_digest(self._identity_payload()):
            _fail("understanding_integrity_failure", "understanding_id does not match its semantic identity")

    def _validate_conflicts(self) -> None:
        groups: dict[tuple[str, str], list[UnderstandingClaim]] = {}
        for claim in self.claims:
            groups.setdefault((claim.dimension, claim.subject), []).append(claim)
        represented = [set(item.claim_ids) for item in self.contradictions]
        for group in groups.values():
            if len({claim.statement for claim in group}) <= 1:
                continue
            expected = {claim.claim_id for claim in group}
            if expected not in represented:
                _fail(
                    "contradiction_required",
                    "conflicting claims must remain explicitly represented as a contradiction",
                    details={"claim_ids": sorted(expected)},
                )

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema": UNDERSTANDING_SCHEMA,
            "intent_basis": self.intent_basis.as_dict(),
            "repo_view": self.repo_view.as_dict(),
            "claims": [claim.as_dict() for claim in self.claims],
            "requested_dimensions": list(self.requested_dimensions),
            "covered_dimensions": list(self.covered_dimensions),
            "must_see_categories": list(self.must_see_categories),
            "covered_must_see": list(self.covered_must_see),
            "coverage_bindings": [item.as_dict() for item in self.coverage_bindings],
            "unknowns": [item.as_dict() for item in self.unknowns],
            "omissions": [item.as_dict() for item in self.omissions],
            "contradictions": [item.as_dict() for item in self.contradictions],
            "invalidation_predicates": list(self.invalidation_predicates),
            "producer_id": self.producer_id,
            "producer_version": self.producer_version,
        }

    @property
    def coverage_status(self) -> Literal["COMPLETE", "PARTIAL", "BLOCKED"]:
        return _coverage_state(
            self.requested_dimensions,
            self.covered_dimensions,
            self.must_see_categories,
            self.covered_must_see,
            self.unknowns,
            self.omissions,
            self.contradictions,
            self.coverage_bindings,
        )

    @property
    def gap_ids(self) -> tuple[str, ...]:
        return tuple(item.unknown_id for item in self.unknowns) + tuple(item.omission_id for item in self.omissions)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": UNDERSTANDING_SCHEMA,
            "understanding_id": self.understanding_id,
            "intent_basis": self.intent_basis.as_dict(),
            "repo_view": self.repo_view.as_dict(),
            "claims": [claim.as_dict() for claim in self.claims],
            "requested_dimensions": list(self.requested_dimensions),
            "covered_dimensions": list(self.covered_dimensions),
            "must_see_categories": list(self.must_see_categories),
            "covered_must_see": list(self.covered_must_see),
            "coverage_bindings": [item.as_dict() for item in self.coverage_bindings],
            "unknowns": [item.as_dict() for item in self.unknowns],
            "omissions": [item.as_dict() for item in self.omissions],
            "contradictions": [item.as_dict() for item in self.contradictions],
            "invalidation_predicates": list(self.invalidation_predicates),
            "coverage_status": self.coverage_status,
            "producer_id": self.producer_id,
            "producer_version": self.producer_version,
        }

    def to_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def create(
        cls,
        intent_basis: IntentBasis,
        repo_view: CommittedRepoView | RepoViewBinding,
        *,
        claims: Sequence[UnderstandingClaim] = (),
        requested_dimensions: Sequence[str],
        covered_dimensions: Sequence[str] = (),
        must_see_categories: Sequence[str] = (),
        covered_must_see: Sequence[str] = (),
        coverage_bindings: Sequence[CoverageBinding] = (),
        unknowns: Sequence[Unknown] = (),
        omissions: Sequence[Omission] = (),
        contradictions: Sequence[ClaimContradiction] = (),
        invalidation_predicates: Sequence[str] = (
            "repo_view.view_id_changed",
            "intent_basis.changed",
            "producer_or_schema.changed",
        ),
        producer_id: str = M2C_PRODUCER_ID,
        producer_version: str = M2C_PRODUCER_VERSION,
        binding_store: DurableBindingStore | None = None,
    ) -> "RepositoryUnderstandingView":
        if not isinstance(intent_basis, IntentBasis):
            _fail("malformed_intent_basis", "Understanding requires IntentBasis")
        binding = RepoViewBinding.from_view(repo_view) if isinstance(repo_view, CommittedRepoView) else repo_view
        if not isinstance(binding, RepoViewBinding):
            _fail("malformed_repo_view_basis", "Understanding requires an exact RepoView binding")
        identity = {
            "schema": UNDERSTANDING_SCHEMA,
            "intent_basis": intent_basis.as_dict(),
            "repo_view": binding.as_dict(),
            "claims": [item.as_dict() for item in claims],
            "requested_dimensions": list(requested_dimensions),
            "covered_dimensions": list(covered_dimensions),
            "must_see_categories": list(must_see_categories),
            "covered_must_see": list(covered_must_see),
            "coverage_bindings": [item.as_dict() for item in coverage_bindings],
            "unknowns": [item.as_dict() for item in unknowns],
            "omissions": [item.as_dict() for item in omissions],
            "contradictions": [item.as_dict() for item in contradictions],
            "invalidation_predicates": list(invalidation_predicates),
            "producer_id": producer_id,
            "producer_version": producer_version,
        }
        result = cls(
            _record_digest(identity),
            intent_basis,
            binding,
            tuple(claims),
            tuple(requested_dimensions),
            tuple(covered_dimensions),
            tuple(must_see_categories),
            tuple(covered_must_see),
            tuple(coverage_bindings),
            tuple(unknowns),
            tuple(omissions),
            tuple(contradictions),
            tuple(invalidation_predicates),
            producer_id,
            producer_version,
        )
        if any(claim.kind == "FACT" for claim in result.claims) or any(
            item.supporting_fragment_ids for item in result.coverage_bindings
        ):
            if binding_store is None:
                _fail("source_evidence_store_required", "authoritative Understanding construction requires M2b grounding")
            result.validate_source_grounding(binding_store, repo_view)
        return result

    def validate_source_grounding(
        self,
        binding_store: DurableBindingStore,
        repo_view: CommittedRepoView | RepoViewBinding | None = None,
    ) -> None:
        expected_repo = self.repo_view if repo_view is None else _binding_for_repo(repo_view)
        if expected_repo != self.repo_view:
            _fail("understanding_basis_mismatch", "grounding RepoView differs from Understanding RepoView")
        for claim in self.claims:
            claim.validate_source_grounding(binding_store, repo_view or self.repo_view)
        _validate_coverage_bindings(
            self.coverage_bindings,
            repo_view=self.repo_view,
            claims=self.claims,
            requested_dimensions=self.requested_dimensions,
            covered_dimensions=self.covered_dimensions,
            must_see_categories=self.must_see_categories,
            covered_must_see=self.covered_must_see,
            binding_store=binding_store,
            committed_view=repo_view if isinstance(repo_view, CommittedRepoView) else None,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RepositoryUnderstandingView":
        _exact_fields(
            value,
            {
                "schema",
                "understanding_id",
                "intent_basis",
                "repo_view",
                "claims",
                "requested_dimensions",
                "covered_dimensions",
                "must_see_categories",
                "covered_must_see",
                "coverage_bindings",
                "unknowns",
                "omissions",
                "contradictions",
                "invalidation_predicates",
                "coverage_status",
                "producer_id",
                "producer_version",
            },
            field="understanding",
        )
        if value["schema"] != UNDERSTANDING_SCHEMA:
            _fail("schema_mismatch", "unsupported RepositoryUnderstanding schema")
        intent = _parse_intent(value["intent_basis"])
        binding = _parse_repo_binding(value["repo_view"])
        claims = tuple(UnderstandingClaim.from_mapping(item) for item in value["claims"])
        result = cls(
            value["understanding_id"],
            intent,
            binding,
            claims,
            requested_dimensions=_identifier_sequence(value["requested_dimensions"], field="understanding.requested_dimensions"),
            covered_dimensions=_identifier_sequence(value["covered_dimensions"], field="understanding.covered_dimensions"),
            must_see_categories=_identifier_sequence(value["must_see_categories"], field="understanding.must_see_categories"),
            covered_must_see=_identifier_sequence(value["covered_must_see"], field="understanding.covered_must_see"),
            coverage_bindings=tuple(CoverageBinding.from_mapping(item) for item in value["coverage_bindings"]),
            unknowns=tuple(Unknown.from_mapping(item) for item in value["unknowns"]),
            omissions=tuple(Omission.from_mapping(item) for item in value["omissions"]),
            contradictions=tuple(ClaimContradiction.from_mapping(item) for item in value["contradictions"]),
            invalidation_predicates=_identifier_sequence(value["invalidation_predicates"], field="understanding.invalidation_predicates", allow_empty=False),
            producer_id=value["producer_id"],
            producer_version=value["producer_version"],
        )
        if value["coverage_status"] != result.coverage_status:
            _fail("coverage_status_mismatch", "coverage_status must be mechanically derived")
        if value["understanding_id"] != result.understanding_id:
            _fail("understanding_integrity_failure", "understanding_id differs from exact record identity")
        return result


def _validate_unique_records(records: Sequence[object], attribute: str, *, field: str) -> None:
    values = [getattr(record, attribute, None) for record in records]
    if len(values) != len(set(values)):
        _fail("duplicate_m2c_value", f"{field} contains duplicate identities")


def _validate_coverage_fields(
    requested_dimensions: Sequence[str],
    covered_dimensions: Sequence[str],
    must_see_categories: Sequence[str],
    covered_must_see: Sequence[str],
) -> None:
    for field, values in (
        ("requested_dimensions", requested_dimensions),
        ("covered_dimensions", covered_dimensions),
        ("must_see_categories", must_see_categories),
        ("covered_must_see", covered_must_see),
    ):
        _identifier_sequence(values, field=field)
    if not set(covered_dimensions).issubset(requested_dimensions):
        _fail("coverage_overclaim", "covered dimensions must be requested dimensions")
    if not set(covered_must_see).issubset(must_see_categories):
        _fail("coverage_overclaim", "covered must-see categories must be required categories")


@dataclass(frozen=True)
class ContextPackage:
    """Immutable quality capsule referring to typed fragments, never duplicating bytes."""

    package_id: str
    intent_basis: IntentBasis
    repo_view: RepoViewBinding
    understanding_id: str
    horizon: str
    requested_dimensions: tuple[str, ...]
    covered_dimensions: tuple[str, ...]
    must_see_categories: tuple[str, ...]
    covered_must_see: tuple[str, ...]
    coverage_bindings: tuple[CoverageBinding, ...]
    unknowns: tuple[Unknown, ...]
    omissions: tuple[Omission, ...]
    contradictions: tuple[ClaimContradiction, ...]
    included_fragment_ids: tuple[str, ...]
    affordances: tuple[ContextAffordance, ...]
    architecture_constraints_included: tuple[str, ...]
    invalidation_predicates: tuple[str, ...]
    policy_version: str = M2C_POLICY_VERSION
    producer_id: str = M2C_PRODUCER_ID
    producer_version: str = M2C_PRODUCER_VERSION
    fact_claim_ids: tuple[str, ...] = ()
    inference_claim_ids: tuple[str, ...] = ()
    assumption_claim_ids: tuple[str, ...] = ()
    hypothesis_claim_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _digest(self.package_id, field="package_id")
        _digest(self.understanding_id, field="understanding_id")
        if not isinstance(self.intent_basis, IntentBasis) or not isinstance(self.repo_view, RepoViewBinding):
            _fail("malformed_context_package", "ContextPackage requires typed intent and RepoView basis")
        if self.horizon not in HORIZONS:
            _fail("horizon_invalid", f"unsupported context horizon: {self.horizon}")
        _validate_coverage_fields(
            self.requested_dimensions,
            self.covered_dimensions,
            self.must_see_categories,
            self.covered_must_see,
        )
        if any(not isinstance(item, CoverageBinding) for item in self.coverage_bindings):
            _fail("malformed_coverage_binding", "package coverage_bindings must be typed")
        _validate_unique_records(self.unknowns, "unknown_id", field="package.unknowns")
        _validate_unique_records(self.omissions, "omission_id", field="package.omissions")
        _validate_unique_records(self.contradictions, "contradiction_id", field="package.contradictions")
        _validate_unique_records(self.affordances, "affordance_id", field="package.affordances")
        for unknown in self.unknowns:
            if not isinstance(unknown, Unknown) or not _same_repo(unknown.repo_view, self.repo_view):
                _fail("context_package_basis_mismatch", "package unknowns must bind the package RepoView")
        for omission in self.omissions:
            if not isinstance(omission, Omission) or not _same_repo(omission.repo_view, self.repo_view):
                _fail("context_package_basis_mismatch", "package omissions must bind the package RepoView")
        for contradiction in self.contradictions:
            if not isinstance(contradiction, ClaimContradiction) or not _same_repo(contradiction.repo_view, self.repo_view):
                _fail("context_package_basis_mismatch", "package contradictions must bind the package RepoView")
        claim_ids = {
            claim_id
            for values in (
                self.fact_claim_ids,
                self.inference_claim_ids,
                self.assumption_claim_ids,
                self.hypothesis_claim_ids,
            )
            for claim_id in values
        }
        _validate_coverage_bindings(
            self.coverage_bindings,
            repo_view=self.repo_view,
            claims=(),
            allowed_claim_ids=claim_ids,
            requested_dimensions=self.requested_dimensions,
            covered_dimensions=self.covered_dimensions,
            must_see_categories=self.must_see_categories,
            covered_must_see=self.covered_must_see,
            included_fragment_ids=self.included_fragment_ids,
        )
        for fragment_id in self.included_fragment_ids:
            _digest(fragment_id, field="package.included_fragment_ids[]")
        _identifier_sequence(self.architecture_constraints_included, field="package.architecture_constraints_included")
        _identifier_sequence(self.invalidation_predicates, field="package.invalidation_predicates", allow_empty=False)
        _identifier(self.policy_version, field="package.policy_version")
        _identifier(self.producer_id, field="package.producer_id")
        _identifier(self.producer_version, field="package.producer_version")
        for field, values in (
            ("package.fact_claim_ids", self.fact_claim_ids),
            ("package.inference_claim_ids", self.inference_claim_ids),
            ("package.assumption_claim_ids", self.assumption_claim_ids),
            ("package.hypothesis_claim_ids", self.hypothesis_claim_ids),
        ):
            _digest_sequence(values, field=field)
        if len({claim_id for values in (self.fact_claim_ids, self.inference_claim_ids, self.assumption_claim_ids, self.hypothesis_claim_ids) for claim_id in values}) != sum(
            len(values) for values in (self.fact_claim_ids, self.inference_claim_ids, self.assumption_claim_ids, self.hypothesis_claim_ids)
        ):
            _fail("package_claim_overlap", "ContextPackage claim classifications must be disjoint")
        if self.package_id != _record_digest(self._identity_payload()):
            _fail("context_package_integrity_failure", "package_id does not match its semantic identity")

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema": CONTEXT_PACKAGE_SCHEMA,
            "intent_basis": self.intent_basis.as_dict(),
            "repo_view": self.repo_view.as_dict(),
            "understanding_id": self.understanding_id,
            "horizon": self.horizon,
            "requested_dimensions": list(self.requested_dimensions),
            "covered_dimensions": list(self.covered_dimensions),
            "must_see_categories": list(self.must_see_categories),
            "covered_must_see": list(self.covered_must_see),
            "coverage_bindings": [item.as_dict() for item in self.coverage_bindings],
            "unknowns": [item.as_dict() for item in self.unknowns],
            "omissions": [item.as_dict() for item in self.omissions],
            "contradictions": [item.as_dict() for item in self.contradictions],
            "included_fragment_ids": list(self.included_fragment_ids),
            "affordances": [item.as_dict() for item in self.affordances],
            "architecture_constraints_included": list(self.architecture_constraints_included),
            "invalidation_predicates": list(self.invalidation_predicates),
            "policy_version": self.policy_version,
            "producer_id": self.producer_id,
            "producer_version": self.producer_version,
            "fact_claim_ids": list(self.fact_claim_ids),
            "inference_claim_ids": list(self.inference_claim_ids),
            "assumption_claim_ids": list(self.assumption_claim_ids),
            "hypothesis_claim_ids": list(self.hypothesis_claim_ids),
        }

    @property
    def coverage_status(self) -> Literal["COMPLETE", "PARTIAL", "BLOCKED"]:
        return _coverage_state(
            self.requested_dimensions,
            self.covered_dimensions,
            self.must_see_categories,
            self.covered_must_see,
            self.unknowns,
            self.omissions,
            self.contradictions,
            self.coverage_bindings,
        )

    @property
    def gap_ids(self) -> tuple[str, ...]:
        return tuple(item.unknown_id for item in self.unknowns) + tuple(item.omission_id for item in self.omissions)

    def validate_source_grounding(
        self,
        understanding: RepositoryUnderstandingView,
        view: CommittedRepoView,
        binding_store: DurableBindingStore,
    ) -> None:
        """Recheck package authority against live M2a and M2b authorities."""

        if not isinstance(understanding, RepositoryUnderstandingView) or not isinstance(view, CommittedRepoView):
            _fail("repository_source_authority_required", "ContextPackage grounding requires typed Understanding and CommittedRepoView")
        expected_binding = RepoViewBinding.from_view(view)
        if (
            self.understanding_id != understanding.understanding_id
            or self.intent_basis != understanding.intent_basis
            or self.repo_view != expected_binding
            or tuple(self.requested_dimensions) != understanding.requested_dimensions
            or tuple(self.covered_dimensions) != understanding.covered_dimensions
            or tuple(self.must_see_categories) != understanding.must_see_categories
            or tuple(self.covered_must_see) != understanding.covered_must_see
            or tuple(self.coverage_bindings) != understanding.coverage_bindings
            or tuple(self.unknowns) != understanding.unknowns
            or tuple(self.omissions) != understanding.omissions
            or tuple(self.contradictions) != understanding.contradictions
            or self.fact_claim_ids != tuple(claim.claim_id for claim in understanding.claims if claim.kind == "FACT")
            or self.inference_claim_ids != tuple(claim.claim_id for claim in understanding.claims if claim.kind == "INFERENCE")
            or self.assumption_claim_ids != tuple(claim.claim_id for claim in understanding.claims if claim.kind == "ASSUMPTION")
            or self.hypothesis_claim_ids != tuple(claim.claim_id for claim in understanding.claims if claim.kind == "HYPOTHESIS")
        ):
            _fail("context_package_basis_mismatch", "ContextPackage does not match the exact Understanding basis")
        understanding.validate_source_grounding(binding_store, view)
        for fragment_id in self.included_fragment_ids:
            try:
                accepted = binding_store.resolve_accepted(fragment_id, expected_view=view)
            except Exception as exc:
                _fail("repository_source_binding_failure", "ContextPackage includes a non-accepted M2b fragment", details={"cause": getattr(exc, "code", "binding_failure")})
            if accepted.fragment.repo_view != expected_binding:
                _fail("repository_source_repo_mismatch", "ContextPackage fragment has a foreign RepoView")
        _validate_coverage_bindings(
            self.coverage_bindings,
            repo_view=expected_binding,
            claims=understanding.claims,
            requested_dimensions=self.requested_dimensions,
            covered_dimensions=self.covered_dimensions,
            must_see_categories=self.must_see_categories,
            covered_must_see=self.covered_must_see,
            binding_store=binding_store,
            committed_view=view,
            included_fragment_ids=self.included_fragment_ids,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": CONTEXT_PACKAGE_SCHEMA,
            "package_id": self.package_id,
            "intent_basis": self.intent_basis.as_dict(),
            "repo_view": self.repo_view.as_dict(),
            "understanding_id": self.understanding_id,
            "horizon": self.horizon,
            "requested_dimensions": list(self.requested_dimensions),
            "covered_dimensions": list(self.covered_dimensions),
            "must_see_categories": list(self.must_see_categories),
            "covered_must_see": list(self.covered_must_see),
            "coverage_bindings": [item.as_dict() for item in self.coverage_bindings],
            "unknowns": [item.as_dict() for item in self.unknowns],
            "omissions": [item.as_dict() for item in self.omissions],
            "contradictions": [item.as_dict() for item in self.contradictions],
            "included_fragment_ids": list(self.included_fragment_ids),
            "affordances": [item.as_dict() for item in self.affordances],
            "architecture_constraints_included": list(self.architecture_constraints_included),
            "invalidation_predicates": list(self.invalidation_predicates),
            "coverage_status": self.coverage_status,
            "policy_version": self.policy_version,
            "producer_id": self.producer_id,
            "producer_version": self.producer_version,
            "fact_claim_ids": list(self.fact_claim_ids),
            "inference_claim_ids": list(self.inference_claim_ids),
            "assumption_claim_ids": list(self.assumption_claim_ids),
            "hypothesis_claim_ids": list(self.hypothesis_claim_ids),
        }

    def to_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def create(
        cls,
        intent_basis: IntentBasis,
        repo_view: CommittedRepoView | RepoViewBinding,
        *,
        understanding_id: str,
        horizon: str,
        requested_dimensions: Sequence[str],
        covered_dimensions: Sequence[str] = (),
        must_see_categories: Sequence[str] = (),
        covered_must_see: Sequence[str] = (),
        coverage_bindings: Sequence[CoverageBinding] = (),
        unknowns: Sequence[Unknown] = (),
        omissions: Sequence[Omission] = (),
        contradictions: Sequence[ClaimContradiction] = (),
        included_fragment_ids: Sequence[str] = (),
        affordances: Sequence[ContextAffordance] = (),
        architecture_constraints_included: Sequence[str] = (),
        invalidation_predicates: Sequence[str] = (
            "repo_view.view_id_changed",
            "intent_basis.changed",
            "producer_or_schema.changed",
        ),
        policy_version: str = M2C_POLICY_VERSION,
        producer_id: str = M2C_PRODUCER_ID,
        producer_version: str = M2C_PRODUCER_VERSION,
        fact_claim_ids: Sequence[str] = (),
        inference_claim_ids: Sequence[str] = (),
        assumption_claim_ids: Sequence[str] = (),
        hypothesis_claim_ids: Sequence[str] = (),
        understanding: RepositoryUnderstandingView | None = None,
        binding_store: DurableBindingStore | None = None,
    ) -> "ContextPackage":
        if not isinstance(intent_basis, IntentBasis):
            _fail("malformed_intent_basis", "ContextPackage requires IntentBasis")
        binding = RepoViewBinding.from_view(repo_view) if isinstance(repo_view, CommittedRepoView) else repo_view
        if not isinstance(binding, RepoViewBinding):
            _fail("malformed_repo_view_basis", "ContextPackage requires an exact RepoView binding")
        _validate_coverage_fields(requested_dimensions, covered_dimensions, must_see_categories, covered_must_see)
        if understanding is not None:
            if (
                not isinstance(understanding, RepositoryUnderstandingView)
                or understanding.understanding_id != understanding_id
                or understanding.intent_basis != intent_basis
                or understanding.repo_view != binding
            ):
                _fail("context_package_basis_mismatch", "ContextPackage must bind the exact RepositoryUnderstanding")
            expected_claims = {
                "FACT": tuple(claim.claim_id for claim in understanding.claims if claim.kind == "FACT"),
                "INFERENCE": tuple(claim.claim_id for claim in understanding.claims if claim.kind == "INFERENCE"),
                "ASSUMPTION": tuple(claim.claim_id for claim in understanding.claims if claim.kind == "ASSUMPTION"),
                "HYPOTHESIS": tuple(claim.claim_id for claim in understanding.claims if claim.kind == "HYPOTHESIS"),
            }
            if (
                tuple(requested_dimensions) != understanding.requested_dimensions
                or tuple(covered_dimensions) != understanding.covered_dimensions
                or tuple(must_see_categories) != understanding.must_see_categories
                or tuple(covered_must_see) != understanding.covered_must_see
                or tuple(coverage_bindings) != understanding.coverage_bindings
                or tuple(unknowns) != understanding.unknowns
                or tuple(omissions) != understanding.omissions
                or tuple(contradictions) != understanding.contradictions
                or tuple(fact_claim_ids) != expected_claims["FACT"]
                or tuple(inference_claim_ids) != expected_claims["INFERENCE"]
                or tuple(assumption_claim_ids) != expected_claims["ASSUMPTION"]
                or tuple(hypothesis_claim_ids) != expected_claims["HYPOTHESIS"]
            ):
                _fail("context_package_basis_mismatch", "ContextPackage fields must be derived from the exact Understanding")
            if binding_store is not None:
                understanding.validate_source_grounding(binding_store, repo_view)
        if (covered_dimensions or covered_must_see or coverage_bindings or fact_claim_ids or inference_claim_ids or assumption_claim_ids or hypothesis_claim_ids) and understanding is None:
            _fail("understanding_basis_required", "covered ContextPackage claims require a real RepositoryUnderstanding basis")
        allowed_fragments = None
        if understanding is not None and binding_store is None:
            allowed_fragments = tuple(
                fragment_id
                for claim in understanding.claims
                for evidence in claim.source_evidence
                for fragment_id in (evidence.fragment_id,)
            )
        _validate_coverage_bindings(
            tuple(coverage_bindings),
            repo_view=binding,
            claims=(),
            allowed_claim_ids=tuple(
                claim_id
                for values in (fact_claim_ids, inference_claim_ids, assumption_claim_ids, hypothesis_claim_ids)
                for claim_id in values
            ),
            requested_dimensions=requested_dimensions,
            covered_dimensions=covered_dimensions,
            must_see_categories=must_see_categories,
            covered_must_see=covered_must_see,
            binding_store=binding_store,
            committed_view=repo_view if isinstance(repo_view, CommittedRepoView) else None,
            included_fragment_ids=included_fragment_ids,
            allowed_fragment_ids=allowed_fragments,
        )
        identity = {
            "schema": CONTEXT_PACKAGE_SCHEMA,
            "intent_basis": intent_basis.as_dict(),
            "repo_view": binding.as_dict(),
            "understanding_id": understanding_id,
            "horizon": horizon,
            "requested_dimensions": list(requested_dimensions),
            "covered_dimensions": list(covered_dimensions),
            "must_see_categories": list(must_see_categories),
            "covered_must_see": list(covered_must_see),
            "coverage_bindings": [item.as_dict() for item in coverage_bindings],
            "unknowns": [item.as_dict() for item in unknowns],
            "omissions": [item.as_dict() for item in omissions],
            "contradictions": [item.as_dict() for item in contradictions],
            "included_fragment_ids": list(included_fragment_ids),
            "affordances": [item.as_dict() for item in affordances],
            "architecture_constraints_included": list(architecture_constraints_included),
            "invalidation_predicates": list(invalidation_predicates),
            "policy_version": policy_version,
            "producer_id": producer_id,
            "producer_version": producer_version,
            "fact_claim_ids": list(fact_claim_ids),
            "inference_claim_ids": list(inference_claim_ids),
            "assumption_claim_ids": list(assumption_claim_ids),
            "hypothesis_claim_ids": list(hypothesis_claim_ids),
        }
        return cls(
            _record_digest(identity),
            intent_basis,
            binding,
            understanding_id,
            horizon,
            tuple(requested_dimensions),
            tuple(covered_dimensions),
            tuple(must_see_categories),
            tuple(covered_must_see),
            tuple(coverage_bindings),
            tuple(unknowns),
            tuple(omissions),
            tuple(contradictions),
            tuple(included_fragment_ids),
            tuple(affordances),
            tuple(architecture_constraints_included),
            tuple(invalidation_predicates),
            policy_version,
            producer_id,
            producer_version,
            tuple(fact_claim_ids),
            tuple(inference_claim_ids),
            tuple(assumption_claim_ids),
            tuple(hypothesis_claim_ids),
        )

    @classmethod
    def from_understanding(
        cls,
        understanding: RepositoryUnderstandingView,
        *,
        horizon: str,
        included_fragment_ids: Sequence[str] = (),
        affordances: Sequence[ContextAffordance] = (),
        architecture_constraints_included: Sequence[str] = (),
        binding_store: DurableBindingStore | None = None,
    ) -> "ContextPackage":
        return cls.create(
            understanding.intent_basis,
            understanding.repo_view,
            understanding_id=understanding.understanding_id,
            horizon=horizon,
            requested_dimensions=understanding.requested_dimensions,
            covered_dimensions=understanding.covered_dimensions,
            must_see_categories=understanding.must_see_categories,
            covered_must_see=understanding.covered_must_see,
            coverage_bindings=understanding.coverage_bindings,
            unknowns=understanding.unknowns,
            omissions=understanding.omissions,
            contradictions=understanding.contradictions,
            included_fragment_ids=included_fragment_ids,
            affordances=affordances,
            architecture_constraints_included=architecture_constraints_included,
            invalidation_predicates=understanding.invalidation_predicates,
            producer_id=understanding.producer_id,
            producer_version=understanding.producer_version,
            fact_claim_ids=tuple(claim.claim_id for claim in understanding.claims if claim.kind == "FACT"),
            inference_claim_ids=tuple(claim.claim_id for claim in understanding.claims if claim.kind == "INFERENCE"),
            assumption_claim_ids=tuple(claim.claim_id for claim in understanding.claims if claim.kind == "ASSUMPTION"),
            hypothesis_claim_ids=tuple(claim.claim_id for claim in understanding.claims if claim.kind == "HYPOTHESIS"),
            understanding=understanding,
            binding_store=binding_store,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ContextPackage":
        _exact_fields(
            value,
            {
                "schema",
                "package_id",
                "intent_basis",
                "repo_view",
                "understanding_id",
                "horizon",
                "requested_dimensions",
                "covered_dimensions",
                "must_see_categories",
                "covered_must_see",
                "coverage_bindings",
                "unknowns",
                "omissions",
                "contradictions",
                "included_fragment_ids",
                "affordances",
                "architecture_constraints_included",
                "invalidation_predicates",
                "coverage_status",
                "policy_version",
                "producer_id",
                "producer_version",
                "fact_claim_ids",
                "inference_claim_ids",
                "assumption_claim_ids",
                "hypothesis_claim_ids",
            },
            field="context_package",
        )
        if value["schema"] != CONTEXT_PACKAGE_SCHEMA:
            _fail("schema_mismatch", "unsupported ContextPackage schema")
        result = cls(
            package_id=value["package_id"],
            intent_basis=_parse_intent(value["intent_basis"]),
            repo_view=_parse_repo_binding(value["repo_view"]),
            understanding_id=value["understanding_id"],
            horizon=value["horizon"],
            requested_dimensions=_identifier_sequence(value["requested_dimensions"], field="package.requested_dimensions"),
            covered_dimensions=_identifier_sequence(value["covered_dimensions"], field="package.covered_dimensions"),
            must_see_categories=_identifier_sequence(value["must_see_categories"], field="package.must_see_categories"),
            covered_must_see=_identifier_sequence(value["covered_must_see"], field="package.covered_must_see"),
            coverage_bindings=tuple(CoverageBinding.from_mapping(item) for item in value["coverage_bindings"]),
            unknowns=tuple(Unknown.from_mapping(item) for item in value["unknowns"]),
            omissions=tuple(Omission.from_mapping(item) for item in value["omissions"]),
            contradictions=tuple(ClaimContradiction.from_mapping(item) for item in value["contradictions"]),
            included_fragment_ids=_sequence(value["included_fragment_ids"], field="package.included_fragment_ids"),
            affordances=tuple(ContextAffordance.from_mapping(item) for item in value["affordances"]),
            architecture_constraints_included=_identifier_sequence(value["architecture_constraints_included"], field="package.architecture_constraints_included"),
            invalidation_predicates=_identifier_sequence(value["invalidation_predicates"], field="package.invalidation_predicates", allow_empty=False),
            policy_version=value["policy_version"],
            producer_id=value["producer_id"],
            producer_version=value["producer_version"],
            fact_claim_ids=_digest_sequence(value["fact_claim_ids"], field="package.fact_claim_ids"),
            inference_claim_ids=_digest_sequence(value["inference_claim_ids"], field="package.inference_claim_ids"),
            assumption_claim_ids=_digest_sequence(value["assumption_claim_ids"], field="package.assumption_claim_ids"),
            hypothesis_claim_ids=_digest_sequence(value["hypothesis_claim_ids"], field="package.hypothesis_claim_ids"),
        )
        if value["coverage_status"] != result.coverage_status:
            _fail("coverage_status_mismatch", "package coverage_status must be mechanically derived")
        if value["package_id"] != result.package_id:
            _fail("context_package_integrity_failure", "package_id differs from exact record identity")
        return result


@dataclass(frozen=True)
class ContextRequest:
    """Immutable semantic request for more context; never a scheduler item."""

    request_id: str
    intent_basis: IntentBasis
    repo_view: RepoViewBinding
    source_package_id: str
    gap_ids: tuple[str, ...]
    horizon: str
    requested_dimensions: tuple[str, ...]
    requested_evidence: tuple[str, ...]
    question: str
    counterexample: str | None
    reason: str
    producer_id: str = M2C_PRODUCER_ID
    producer_version: str = M2C_PRODUCER_VERSION

    def __post_init__(self) -> None:
        _digest(self.request_id, field="request_id")
        if not isinstance(self.intent_basis, IntentBasis) or not isinstance(self.repo_view, RepoViewBinding):
            _fail("malformed_context_request", "ContextRequest requires typed intent and RepoView basis")
        _digest(self.source_package_id, field="source_package_id")
        _digest_sequence(self.gap_ids, field="request.gap_ids", allow_empty=False)
        if self.horizon not in HORIZONS:
            _fail("horizon_invalid", f"unsupported request horizon: {self.horizon}")
        _identifier_sequence(self.requested_dimensions, field="request.requested_dimensions", allow_empty=False)
        _sequence(self.requested_evidence, field="request.requested_evidence", allow_empty=False)
        _text(self.question, field="request.question")
        if self.counterexample is not None:
            _text(self.counterexample, field="request.counterexample")
        _text(self.reason, field="request.reason")
        _identifier(self.producer_id, field="request.producer_id")
        _identifier(self.producer_version, field="request.producer_version")
        if self.request_id != _record_digest(self._identity_payload()):
            _fail("context_request_integrity_failure", "request_id does not match its semantic identity")

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema": CONTEXT_REQUEST_SCHEMA,
            "intent_basis": self.intent_basis.as_dict(),
            "repo_view": self.repo_view.as_dict(),
            "source_package_id": self.source_package_id,
            "gap_ids": list(self.gap_ids),
            "horizon": self.horizon,
            "requested_dimensions": list(self.requested_dimensions),
            "requested_evidence": list(self.requested_evidence),
            "question": self.question,
            "counterexample": self.counterexample,
            "reason": self.reason,
            "producer_id": self.producer_id,
            "producer_version": self.producer_version,
        }

    def as_dict(self) -> dict[str, Any]:
        return {"schema": CONTEXT_REQUEST_SCHEMA, "request_id": self.request_id, **self._identity_payload()}

    def to_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    def validate_source_package(self, package: ContextPackage) -> None:
        if not isinstance(package, ContextPackage):
            _fail("stale_request_basis", "ContextRequest source package is unavailable")
        if (
            package.package_id != self.source_package_id
            or package.intent_basis != self.intent_basis
            or package.repo_view != self.repo_view
            or not set(self.gap_ids).issubset(package.gap_ids)
        ):
            _fail("stale_request_basis", "ContextRequest is not bound to the exact source ContextPackage")

    @classmethod
    def create(
        cls,
        package: ContextPackage,
        *,
        gap_ids: Sequence[str],
        horizon: str,
        requested_dimensions: Sequence[str],
        requested_evidence: Sequence[str],
        question: str,
        reason: str,
        counterexample: str | None = None,
    ) -> "ContextRequest":
        if not isinstance(package, ContextPackage):
            _fail("malformed_context_request", "ContextRequest requires a source ContextPackage")
        gaps = tuple(gap_ids)
        if not set(gaps).issubset(package.gap_ids) or not gaps:
            _fail("gap_not_visible", "ContextRequest must target visible source-package gaps")
        identity = {
            "schema": CONTEXT_REQUEST_SCHEMA,
            "intent_basis": package.intent_basis.as_dict(),
            "repo_view": package.repo_view.as_dict(),
            "source_package_id": package.package_id,
            "gap_ids": list(gaps),
            "horizon": horizon,
            "requested_dimensions": list(requested_dimensions),
            "requested_evidence": list(requested_evidence),
            "question": question,
            "counterexample": counterexample,
            "reason": reason,
            "producer_id": M2C_PRODUCER_ID,
            "producer_version": M2C_PRODUCER_VERSION,
        }
        return cls(
            _record_digest(identity),
            package.intent_basis,
            package.repo_view,
            package.package_id,
            gaps,
            horizon,
            tuple(requested_dimensions),
            tuple(requested_evidence),
            question,
            counterexample,
            reason,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ContextRequest":
        _exact_fields(
            value,
            {
                "schema",
                "request_id",
                "intent_basis",
                "repo_view",
                "source_package_id",
                "gap_ids",
                "horizon",
                "requested_dimensions",
                "requested_evidence",
                "question",
                "counterexample",
                "reason",
                "producer_id",
                "producer_version",
            },
            field="context_request",
        )
        if value["schema"] != CONTEXT_REQUEST_SCHEMA:
            _fail("schema_mismatch", "unsupported ContextRequest schema")
        return cls(
            value["request_id"],
            _parse_intent(value["intent_basis"]),
            _parse_repo_binding(value["repo_view"]),
            value["source_package_id"],
            _digest_sequence(value["gap_ids"], field="request.gap_ids", allow_empty=False),
            value["horizon"],
            _identifier_sequence(value["requested_dimensions"], field="request.requested_dimensions", allow_empty=False),
            _sequence(value["requested_evidence"], field="request.requested_evidence", allow_empty=False),
            value["question"],
            value["counterexample"],
            value["reason"],
            value["producer_id"],
            value["producer_version"],
        )


@dataclass(frozen=True)
class GapResolutionEvidence:
    """Explicit evidence edge for one resolved ContextPackage gap."""

    gap_resolution_id: str
    gap_id: str
    added_fragment_ids: tuple[str, ...]
    supporting_claim_ids: tuple[str, ...]
    coverage_binding_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _digest(self.gap_resolution_id, field="gap_resolution.gap_resolution_id")
        _digest(self.gap_id, field="gap_resolution.gap_id")
        _digest_sequence(self.added_fragment_ids, field="gap_resolution.added_fragment_ids")
        _digest_sequence(self.supporting_claim_ids, field="gap_resolution.supporting_claim_ids")
        _digest_sequence(self.coverage_binding_ids, field="gap_resolution.coverage_binding_ids")
        if not (self.added_fragment_ids or self.supporting_claim_ids or self.coverage_binding_ids):
            _fail("resolution_evidence_required", "each resolved gap requires explicit fragment/claim/coverage evidence")
        if self.gap_resolution_id != _record_digest(self._identity_payload()):
            _fail("gap_resolution_integrity_failure", "gap_resolution_id does not match exact evidence identity")

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema": GAP_RESOLUTION_EVIDENCE_SCHEMA,
            "gap_id": self.gap_id,
            "added_fragment_ids": list(self.added_fragment_ids),
            "supporting_claim_ids": list(self.supporting_claim_ids),
            "coverage_binding_ids": list(self.coverage_binding_ids),
        }

    def as_dict(self) -> dict[str, Any]:
        return {"schema": GAP_RESOLUTION_EVIDENCE_SCHEMA, "gap_resolution_id": self.gap_resolution_id, **self._identity_payload()}

    @classmethod
    def create(
        cls,
        *,
        gap_id: str,
        added_fragment_ids: Sequence[str] = (),
        supporting_claim_ids: Sequence[str] = (),
        coverage_binding_ids: Sequence[str] = (),
    ) -> "GapResolutionEvidence":
        identity = {
            "schema": GAP_RESOLUTION_EVIDENCE_SCHEMA,
            "gap_id": gap_id,
            "added_fragment_ids": list(added_fragment_ids),
            "supporting_claim_ids": list(supporting_claim_ids),
            "coverage_binding_ids": list(coverage_binding_ids),
        }
        return cls(
            _record_digest(identity),
            gap_id,
            tuple(added_fragment_ids),
            tuple(supporting_claim_ids),
            tuple(coverage_binding_ids),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "GapResolutionEvidence":
        _exact_fields(
            value,
            {"schema", "gap_resolution_id", "gap_id", "added_fragment_ids", "supporting_claim_ids", "coverage_binding_ids"},
            field="gap_resolution_evidence",
        )
        if value["schema"] != GAP_RESOLUTION_EVIDENCE_SCHEMA:
            _fail("schema_mismatch", "unsupported gap-resolution evidence schema")
        return cls(
            value["gap_resolution_id"],
            value["gap_id"],
            _digest_sequence(value["added_fragment_ids"], field="gap_resolution.added_fragment_ids"),
            _digest_sequence(value["supporting_claim_ids"], field="gap_resolution.supporting_claim_ids"),
            _digest_sequence(value["coverage_binding_ids"], field="gap_resolution.coverage_binding_ids"),
        )


@dataclass(frozen=True)
class ContextResolution:
    """Immutable request-to-result linkage; no request lifecycle state machine."""

    resolution_id: str
    request_id: str
    prior_package_id: str
    resulting_package_id: str | None
    outcome: str
    added_fragment_ids: tuple[str, ...]
    resolved_gap_ids: tuple[str, ...]
    unresolved_gap_ids: tuple[str, ...]
    denial_reason: str | None = None
    introduced_gap_ids: tuple[str, ...] = ()
    gap_resolution_evidence: tuple[GapResolutionEvidence, ...] = ()
    producer_id: str = M2C_PRODUCER_ID
    producer_version: str = M2C_PRODUCER_VERSION

    def __post_init__(self) -> None:
        _digest(self.resolution_id, field="resolution_id")
        _digest(self.request_id, field="request_id")
        _digest(self.prior_package_id, field="prior_package_id")
        if self.resulting_package_id is not None:
            _digest(self.resulting_package_id, field="resulting_package_id")
        if self.outcome not in RESOLUTION_OUTCOMES:
            _fail("resolution_outcome_invalid", f"unsupported ContextResolution outcome: {self.outcome}")
        _digest_sequence(self.added_fragment_ids, field="resolution.added_fragment_ids")
        _digest_sequence(self.resolved_gap_ids, field="resolution.resolved_gap_ids")
        _digest_sequence(self.unresolved_gap_ids, field="resolution.unresolved_gap_ids")
        _digest_sequence(self.introduced_gap_ids, field="resolution.introduced_gap_ids")
        _validate_unique_records(self.gap_resolution_evidence, "gap_resolution_id", field="resolution.gap_resolution_evidence")
        if any(not isinstance(item, GapResolutionEvidence) for item in self.gap_resolution_evidence):
            _fail("malformed_context_resolution", "gap_resolution_evidence must be typed")
        if self.outcome == "RESOLVED":
            if self.resulting_package_id is None or not self.added_fragment_ids or not self.resolved_gap_ids:
                _fail("resolution_evidence_required", "RESOLVED linkage requires resulting package, fragments and gaps")
            if self.denial_reason is not None:
                _fail("malformed_context_resolution", "RESOLVED linkage cannot carry denial_reason")
        else:
            if not self.unresolved_gap_ids:
                _fail("resolution_gap_mismatch", "denied/unavailable linkage must keep at least one gap visible")
            if self.resulting_package_id is not None or self.added_fragment_ids or self.resolved_gap_ids or self.introduced_gap_ids or self.gap_resolution_evidence:
                _fail("resolution_denial_mismatch", "denied/unavailable linkage cannot claim a result or repair")
            if self.denial_reason is None:
                _fail("resolution_denial_reason_required", "denied/unavailable linkage requires a visible reason")
        _identifier(self.producer_id, field="resolution.producer_id")
        _identifier(self.producer_version, field="resolution.producer_version")
        if self.resolution_id != _record_digest(self._identity_payload()):
            _fail("context_resolution_integrity_failure", "resolution_id does not match its semantic identity")

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema": CONTEXT_RESOLUTION_SCHEMA,
            "request_id": self.request_id,
            "prior_package_id": self.prior_package_id,
            "resulting_package_id": self.resulting_package_id,
            "outcome": self.outcome,
            "added_fragment_ids": list(self.added_fragment_ids),
            "resolved_gap_ids": list(self.resolved_gap_ids),
            "unresolved_gap_ids": list(self.unresolved_gap_ids),
            "denial_reason": self.denial_reason,
            "introduced_gap_ids": list(self.introduced_gap_ids),
            "gap_resolution_evidence": [item.as_dict() for item in self.gap_resolution_evidence],
            "producer_id": self.producer_id,
            "producer_version": self.producer_version,
        }

    def as_dict(self) -> dict[str, Any]:
        return {"schema": CONTEXT_RESOLUTION_SCHEMA, "resolution_id": self.resolution_id, **self._identity_payload()}

    def to_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def create(
        cls,
        request: ContextRequest,
        prior_package: ContextPackage,
        *,
        resulting_package: ContextPackage | None = None,
        added_fragments: Sequence[TypedContextFragment] = (),
        binding_store: DurableBindingStore | None = None,
        resolved_gap_ids: Sequence[str] = (),
        unresolved_gap_ids: Sequence[str] | None = None,
        outcome: str = "RESOLVED",
        denial_reason: str | None = None,
        gap_resolution_evidence: Sequence[GapResolutionEvidence] = (),
        introduced_gap_ids: Sequence[str] = (),
    ) -> "ContextResolution":
        if not isinstance(request, ContextRequest) or not isinstance(prior_package, ContextPackage):
            _fail("malformed_context_resolution", "resolution requires typed request and prior package")
        request.validate_source_package(prior_package)
        if outcome == "RESOLVED":
            if not isinstance(resulting_package, ContextPackage):
                _fail("resolution_evidence_required", "resolved request requires resulting ContextPackage")
            if resulting_package.intent_basis != prior_package.intent_basis or resulting_package.repo_view != prior_package.repo_view:
                _fail("resolution_basis_mismatch", "resulting package must preserve exact intent and RepoView basis")
            fragments = tuple(added_fragments)
            if binding_store is None or not fragments:
                _fail("resolution_evidence_required", "resolved request requires accepted M2b fragments")
            fragment_ids: list[str] = []
            for fragment in fragments:
                if not isinstance(fragment, TypedContextFragment) or fragment.repo_view != prior_package.repo_view:
                    _fail("resolution_basis_mismatch", "added fragment is not bound to the exact package RepoView")
                accepted = binding_store.resolve_accepted(fragment.fragment_id)
                if accepted.fragment != fragment:
                    _fail("resolution_evidence_required", "added fragment is not the accepted durable binding")
                fragment_ids.append(fragment.fragment_id)
            resolved = tuple(resolved_gap_ids)
            if not resolved or not set(resolved).issubset(request.gap_ids):
                _fail("resolution_gap_mismatch", "resolved gaps must be requested visible gaps")
            if set(resolved) & set(resulting_package.gap_ids):
                _fail("resolution_gap_mismatch", "a resolved gap remains visible in resulting package")
            introduced = tuple(introduced_gap_ids)
            if set(introduced) & set(prior_package.gap_ids):
                _fail("resolution_gap_mismatch", "introduced gaps must be new to the prior package")
            expected_resulting = (set(prior_package.gap_ids) - set(resolved)) | set(introduced)
            if set(resulting_package.gap_ids) != expected_resulting:
                _fail("resolution_gap_mismatch", "resulting gaps must equal prior minus resolved plus introduced gaps")
            unresolved = tuple(resulting_package.gap_ids) if unresolved_gap_ids is None else tuple(unresolved_gap_ids)
            if set(unresolved) != set(resulting_package.gap_ids):
                _fail("resolution_gap_mismatch", "unresolved gaps must match resulting package gaps")
            added = tuple(fragment_ids)
            evidence = tuple(gap_resolution_evidence)
            if {item.gap_id for item in evidence} != set(resolved) or len(evidence) != len(resolved):
                _fail("resolution_evidence_required", "each and only each resolved gap requires one evidence edge")
            package_claim_ids = {
                claim_id
                for values in (
                    resulting_package.fact_claim_ids,
                    resulting_package.inference_claim_ids,
                    resulting_package.assumption_claim_ids,
                    resulting_package.hypothesis_claim_ids,
                )
                for claim_id in values
            }
            coverage_by_id = {item.coverage_binding_id: item for item in resulting_package.coverage_bindings}
            gaps_by_id = {item.unknown_id: item for item in prior_package.unknowns} | {
                item.omission_id: item for item in prior_package.omissions
            }
            for item in evidence:
                if not set(item.added_fragment_ids).issubset(set(added)):
                    _fail("resolution_evidence_mismatch", "gap evidence must point only to newly accepted fragments")
                if not set(item.supporting_claim_ids).issubset(package_claim_ids):
                    _fail("resolution_evidence_mismatch", "gap evidence names a claim outside the resulting package")
                if not set(item.coverage_binding_ids).issubset(coverage_by_id):
                    _fail("resolution_evidence_mismatch", "gap evidence names a coverage binding outside the resulting package")
                gap = gaps_by_id.get(item.gap_id)
                if gap is None:
                    _fail("resolution_gap_mismatch", "gap evidence names a gap outside the prior package")
                if not any(
                    coverage_by_id[binding_id].target == gap.dimension
                    for binding_id in item.coverage_binding_ids
                ):
                    _fail("resolution_evidence_mismatch", "gap evidence does not ground the resolved gap dimension")
                for binding_id in item.coverage_binding_ids:
                    coverage = coverage_by_id[binding_id]
                    if not (
                        set(coverage.supporting_fragment_ids) & set(item.added_fragment_ids)
                        or set(coverage.supporting_claim_ids) & set(item.supporting_claim_ids)
                    ):
                        _fail("resolution_evidence_mismatch", "gap evidence is not linked to the binding support it names")
            result_id = resulting_package.package_id
        elif outcome in {"DENIED", "UNAVAILABLE"}:
            if resulting_package is not None or added_fragments or resolved_gap_ids or gap_resolution_evidence or introduced_gap_ids:
                _fail("resolution_denial_mismatch", "denied/unavailable request cannot carry repair evidence")
            unresolved = tuple(prior_package.gap_ids) if unresolved_gap_ids is None else tuple(unresolved_gap_ids)
            if not set(request.gap_ids).issubset(unresolved):
                _fail("resolution_gap_mismatch", "denied/unavailable request must keep requested gaps visible")
            if set(unresolved) != set(prior_package.gap_ids):
                _fail("resolution_gap_mismatch", "denied/unavailable linkage must preserve all prior gaps")
            added = ()
            resolved = ()
            evidence = ()
            introduced = ()
            result_id = None
        else:
            _fail("resolution_outcome_invalid", f"unsupported ContextResolution outcome: {outcome}")
        identity = {
            "schema": CONTEXT_RESOLUTION_SCHEMA,
            "request_id": request.request_id,
            "prior_package_id": prior_package.package_id,
            "resulting_package_id": result_id,
            "outcome": outcome,
            "added_fragment_ids": list(added),
            "resolved_gap_ids": list(resolved),
            "unresolved_gap_ids": list(unresolved),
            "denial_reason": denial_reason,
            "introduced_gap_ids": list(introduced),
            "gap_resolution_evidence": [item.as_dict() for item in evidence],
            "producer_id": M2C_PRODUCER_ID,
            "producer_version": M2C_PRODUCER_VERSION,
        }
        return cls(
            _record_digest(identity),
            request.request_id,
            prior_package.package_id,
            result_id,
            outcome,
            added,
            resolved,
            unresolved,
            denial_reason,
            introduced,
            evidence,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ContextResolution":
        _exact_fields(
            value,
            {"schema", "resolution_id", "request_id", "prior_package_id", "resulting_package_id", "outcome", "added_fragment_ids", "resolved_gap_ids", "unresolved_gap_ids", "denial_reason", "introduced_gap_ids", "gap_resolution_evidence", "producer_id", "producer_version"},
            field="context_resolution",
        )
        if value["schema"] != CONTEXT_RESOLUTION_SCHEMA:
            _fail("schema_mismatch", "unsupported ContextResolution schema")
        result = cls(
            value["resolution_id"],
            value["request_id"],
            value["prior_package_id"],
            value["resulting_package_id"],
            value["outcome"],
            _digest_sequence(value["added_fragment_ids"], field="resolution.added_fragment_ids"),
            _digest_sequence(value["resolved_gap_ids"], field="resolution.resolved_gap_ids"),
            _digest_sequence(value["unresolved_gap_ids"], field="resolution.unresolved_gap_ids"),
            value["denial_reason"],
            _digest_sequence(value["introduced_gap_ids"], field="resolution.introduced_gap_ids"),
            tuple(GapResolutionEvidence.from_mapping(item) for item in value["gap_resolution_evidence"]),
            value["producer_id"],
            value["producer_version"],
        )
        if value["resolution_id"] != result.resolution_id:
            _fail("context_resolution_integrity_failure", "resolution_id differs from exact record identity")
        return result


def _digest_sequence(value: object, *, field: str, allow_empty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        _fail("malformed_m2c_record", f"{field} must be an array")
    result = tuple(_digest(item, field=f"{field}[]") for item in value)
    if not allow_empty and not result:
        _fail("malformed_m2c_record", f"{field} must not be empty")
    if len(set(result)) != len(result):
        _fail("duplicate_m2c_value", f"{field} must contain unique digests")
    return result


@dataclass(frozen=True)
class DecisionOption:
    option_id: str
    summary: str
    tradeoffs: tuple[str, ...]
    risks: tuple[str, ...]

    def __post_init__(self) -> None:
        _identifier(self.option_id, field="option.option_id")
        _text(self.summary, field="option.summary")
        _sequence(self.tradeoffs, field="option.tradeoffs")
        _sequence(self.risks, field="option.risks")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": DECISION_OPTION_SCHEMA,
            "option_id": self.option_id,
            "summary": self.summary,
            "tradeoffs": list(self.tradeoffs),
            "risks": list(self.risks),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "DecisionOption":
        _exact_fields(value, {"schema", "option_id", "summary", "tradeoffs", "risks"}, field="decision_option")
        if value["schema"] != DECISION_OPTION_SCHEMA:
            _fail("schema_mismatch", "unsupported DecisionOption schema")
        return cls(
            value["option_id"],
            value["summary"],
            _sequence(value["tradeoffs"], field="option.tradeoffs"),
            _sequence(value["risks"], field="option.risks"),
        )


@dataclass(frozen=True)
class EngineeringDecision:
    """Concise immutable review/resume evidence without private model reasoning."""

    decision_id: str
    intent_basis: IntentBasis
    repo_view_bases: tuple[RepoViewBinding, ...]
    context_package_ids: tuple[str, ...]
    established_fact_claim_ids: tuple[str, ...]
    inference_claim_ids: tuple[str, ...]
    assumption_claim_ids: tuple[str, ...]
    hypothesis_claim_ids: tuple[str, ...]
    alternatives: tuple[DecisionOption, ...]
    chosen_option_id: str
    must_preserve: tuple[str, ...]
    must_not: tuple[str, ...]
    expected_effect_scope: tuple[str, ...]
    acceptance_obligations: tuple[str, ...]
    evidence_obligations: tuple[str, ...]
    uncertainty: tuple[str, ...]
    requested_context_ids: tuple[str, ...]
    architecture_consequences: tuple[str, ...]
    revisit_triggers: tuple[str, ...]
    producer_id: str = M2C_PRODUCER_ID
    producer_version: str = M2C_PRODUCER_VERSION

    def __post_init__(self) -> None:
        _digest(self.decision_id, field="decision_id")
        if not isinstance(self.intent_basis, IntentBasis) or not self.repo_view_bases:
            _fail("malformed_engineering_decision", "decision requires intent and at least one RepoView basis")
        for binding in self.repo_view_bases:
            if not isinstance(binding, RepoViewBinding):
                _fail("malformed_engineering_decision", "decision RepoView bases must be typed")
        for field, values in (
            ("decision.context_package_ids", self.context_package_ids),
            ("decision.established_fact_claim_ids", self.established_fact_claim_ids),
            ("decision.inference_claim_ids", self.inference_claim_ids),
            ("decision.assumption_claim_ids", self.assumption_claim_ids),
            ("decision.hypothesis_claim_ids", self.hypothesis_claim_ids),
            ("decision.requested_context_ids", self.requested_context_ids),
        ):
            _digest_sequence(values, field=field)
        _validate_unique_claim_lists(self)
        if not self.alternatives:
            _fail("decision_alternatives_required", "EngineeringDecision requires explicit alternatives")
        _validate_unique_records(self.alternatives, "option_id", field="decision.alternatives")
        if self.chosen_option_id not in {option.option_id for option in self.alternatives}:
            _fail("chosen_option_missing", "chosen option must be one of the explicit alternatives")
        _identifier(self.chosen_option_id, field="decision.chosen_option_id")
        for field, values in (
            ("decision.must_preserve", self.must_preserve),
            ("decision.must_not", self.must_not),
            ("decision.expected_effect_scope", self.expected_effect_scope),
            ("decision.acceptance_obligations", self.acceptance_obligations),
            ("decision.evidence_obligations", self.evidence_obligations),
            ("decision.uncertainty", self.uncertainty),
            ("decision.architecture_consequences", self.architecture_consequences),
            ("decision.revisit_triggers", self.revisit_triggers),
        ):
            _sequence(values, field=field)
        _identifier(self.producer_id, field="decision.producer_id")
        _identifier(self.producer_version, field="decision.producer_version")
        if self.decision_id != _record_digest(self._identity_payload()):
            _fail("decision_integrity_failure", "decision_id does not match its semantic identity")

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema": ENGINEERING_DECISION_SCHEMA,
            "intent_basis": self.intent_basis.as_dict(),
            "repo_view_bases": [item.as_dict() for item in self.repo_view_bases],
            "context_package_ids": list(self.context_package_ids),
            "established_fact_claim_ids": list(self.established_fact_claim_ids),
            "inference_claim_ids": list(self.inference_claim_ids),
            "assumption_claim_ids": list(self.assumption_claim_ids),
            "hypothesis_claim_ids": list(self.hypothesis_claim_ids),
            "alternatives": [item.as_dict() for item in self.alternatives],
            "chosen_option_id": self.chosen_option_id,
            "must_preserve": list(self.must_preserve),
            "must_not": list(self.must_not),
            "expected_effect_scope": list(self.expected_effect_scope),
            "acceptance_obligations": list(self.acceptance_obligations),
            "evidence_obligations": list(self.evidence_obligations),
            "uncertainty": list(self.uncertainty),
            "requested_context_ids": list(self.requested_context_ids),
            "architecture_consequences": list(self.architecture_consequences),
            "revisit_triggers": list(self.revisit_triggers),
            "producer_id": self.producer_id,
            "producer_version": self.producer_version,
        }

    def as_dict(self) -> dict[str, Any]:
        return {"schema": ENGINEERING_DECISION_SCHEMA, "decision_id": self.decision_id, **self._identity_payload()}

    def to_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def create(
        cls,
        intent_basis: IntentBasis,
        repo_views: Sequence[CommittedRepoView | RepoViewBinding],
        context_packages: Sequence[ContextPackage],
        *,
        established_fact_claim_ids: Sequence[str] = (),
        inference_claim_ids: Sequence[str] = (),
        assumption_claim_ids: Sequence[str] = (),
        hypothesis_claim_ids: Sequence[str] = (),
        alternatives: Sequence[DecisionOption],
        chosen_option_id: str,
        must_preserve: Sequence[str] = (),
        must_not: Sequence[str] = (),
        expected_effect_scope: Sequence[str] = (),
        acceptance_obligations: Sequence[str] = (),
        evidence_obligations: Sequence[str] = (),
        uncertainty: Sequence[str] = (),
        requested_context_ids: Sequence[str] = (),
        architecture_consequences: Sequence[str] = (),
        revisit_triggers: Sequence[str] = (),
        producer_id: str = M2C_PRODUCER_ID,
        producer_version: str = M2C_PRODUCER_VERSION,
        understanding_bases: Sequence[RepositoryUnderstandingView] = (),
        understandings: Sequence[RepositoryUnderstandingView] | None = None,
        binding_store: DurableBindingStore | None = None,
    ) -> "EngineeringDecision":
        if not isinstance(intent_basis, IntentBasis):
            _fail("malformed_intent_basis", "EngineeringDecision requires IntentBasis")
        bindings = tuple(
            RepoViewBinding.from_view(view) if isinstance(view, CommittedRepoView) else view for view in repo_views
        )
        if not bindings or any(not isinstance(item, RepoViewBinding) for item in bindings):
            _fail("malformed_repo_view_basis", "EngineeringDecision requires exact RepoView bindings")
        committed_views = {
            RepoViewBinding.from_view(view): view for view in repo_views if isinstance(view, CommittedRepoView)
        }
        packages = tuple(context_packages)
        if not packages:
            _fail("decision_context_required", "EngineeringDecision requires at least one ContextPackage basis")
        if any(
            not isinstance(package, ContextPackage)
            or package.intent_basis != intent_basis
            or package.repo_view not in bindings
            for package in packages
        ):
            _fail("decision_basis_mismatch", "decision ContextPackages must share exact intent and RepoView basis")
        if {item for item in bindings} != {package.repo_view for package in packages}:
            _fail("decision_basis_mismatch", "decision RepoView bases must exactly equal package RepoView bases")
        supplied_bases = tuple(understandings) if understandings is not None else tuple(understanding_bases)
        claim_groups = (
            ("FACT", tuple(established_fact_claim_ids)),
            ("INFERENCE", tuple(inference_claim_ids)),
            ("ASSUMPTION", tuple(assumption_claim_ids)),
            ("HYPOTHESIS", tuple(hypothesis_claim_ids)),
        )
        if any(ids for _kind, ids in claim_groups) and not supplied_bases:
            _fail("decision_claim_basis_required", "decision claims require the actual Understanding basis")
        actual_claims: dict[str, UnderstandingClaim] = {}
        for understanding in supplied_bases:
            if not isinstance(understanding, RepositoryUnderstandingView):
                _fail("decision_claim_basis_mismatch", "decision Understanding bases must be typed")
            if understanding.intent_basis != intent_basis or understanding.repo_view not in bindings:
                _fail("decision_basis_mismatch", "decision Understanding basis has a foreign intent or RepoView")
            if binding_store is not None:
                committed_view = committed_views.get(understanding.repo_view)
                needs_exact_view = any(claim.kind == "FACT" for claim in understanding.claims) or any(
                    binding.supporting_fragment_ids for binding in understanding.coverage_bindings
                )
                package = next(
                    (candidate for candidate in packages if candidate.understanding_id == understanding.understanding_id),
                    None,
                )
                needs_exact_view = needs_exact_view or bool(package and package.included_fragment_ids)
                if needs_exact_view and committed_view is None:
                    _fail(
                        "decision_source_authority_required",
                        "source-grounded decision bases require the exact CommittedRepoView reader",
                    )
                understanding.validate_source_grounding(
                    binding_store,
                    committed_view if committed_view is not None else understanding.repo_view,
                )
                if package is not None and (needs_exact_view or package.included_fragment_ids):
                    if committed_view is None:
                        _fail(
                            "decision_source_authority_required",
                            "source-grounded ContextPackage bases require the exact CommittedRepoView reader",
                        )
                    package.validate_source_grounding(understanding, committed_view, binding_store)
            for claim in understanding.claims:
                if claim.claim_id in actual_claims and actual_claims[claim.claim_id] != claim:
                    _fail("decision_claim_basis_mismatch", "decision claim ID has conflicting Understanding definitions")
                actual_claims[claim.claim_id] = claim
        package_by_understanding = {package.understanding_id: package for package in packages}
        for understanding in supplied_bases:
            package = package_by_understanding.get(understanding.understanding_id)
            if package is None:
                _fail("decision_basis_mismatch", "decision Understanding has no exact ContextPackage")
        package_claim_classes: dict[str, str] = {}
        for package in packages:
            for kind, ids in (
                ("FACT", package.fact_claim_ids),
                ("INFERENCE", package.inference_claim_ids),
                ("ASSUMPTION", package.assumption_claim_ids),
                ("HYPOTHESIS", package.hypothesis_claim_ids),
            ):
                for claim_id in ids:
                    package_claim_classes[claim_id] = kind
        for kind, ids in claim_groups:
            for claim_id in ids:
                claim = actual_claims.get(claim_id)
                if claim is None or claim.kind != kind or package_claim_classes.get(claim_id) != kind:
                    _fail("decision_claim_basis_mismatch", "decision claim ID is not an exact member of its declared class")
                if kind == "FACT":
                    if binding_store is None:
                        _fail("decision_claim_basis_required", "FACT decision claims require source grounding")
                    committed_view = committed_views.get(claim.repo_view)
                    if committed_view is None:
                        _fail(
                            "decision_source_authority_required",
                            "FACT decision claims require the exact CommittedRepoView reader",
                        )
                    claim.validate_source_grounding(binding_store, committed_view)
        identity = {
            "schema": ENGINEERING_DECISION_SCHEMA,
            "intent_basis": intent_basis.as_dict(),
            "repo_view_bases": [item.as_dict() for item in bindings],
            "context_package_ids": [item.package_id for item in packages],
            "established_fact_claim_ids": list(established_fact_claim_ids),
            "inference_claim_ids": list(inference_claim_ids),
            "assumption_claim_ids": list(assumption_claim_ids),
            "hypothesis_claim_ids": list(hypothesis_claim_ids),
            "alternatives": [item.as_dict() for item in alternatives],
            "chosen_option_id": chosen_option_id,
            "must_preserve": list(must_preserve),
            "must_not": list(must_not),
            "expected_effect_scope": list(expected_effect_scope),
            "acceptance_obligations": list(acceptance_obligations),
            "evidence_obligations": list(evidence_obligations),
            "uncertainty": list(uncertainty),
            "requested_context_ids": list(requested_context_ids),
            "architecture_consequences": list(architecture_consequences),
            "revisit_triggers": list(revisit_triggers),
            "producer_id": producer_id,
            "producer_version": producer_version,
        }
        return cls(
            _record_digest(identity),
            intent_basis,
            bindings,
            tuple(item.package_id for item in packages),
            tuple(established_fact_claim_ids),
            tuple(inference_claim_ids),
            tuple(assumption_claim_ids),
            tuple(hypothesis_claim_ids),
            tuple(alternatives),
            chosen_option_id,
            tuple(must_preserve),
            tuple(must_not),
            tuple(expected_effect_scope),
            tuple(acceptance_obligations),
            tuple(evidence_obligations),
            tuple(uncertainty),
            tuple(requested_context_ids),
            tuple(architecture_consequences),
            tuple(revisit_triggers),
            producer_id,
            producer_version,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "EngineeringDecision":
        _exact_fields(
            value,
            {
                "schema",
                "decision_id",
                "intent_basis",
                "repo_view_bases",
                "context_package_ids",
                "established_fact_claim_ids",
                "inference_claim_ids",
                "assumption_claim_ids",
                "hypothesis_claim_ids",
                "alternatives",
                "chosen_option_id",
                "must_preserve",
                "must_not",
                "expected_effect_scope",
                "acceptance_obligations",
                "evidence_obligations",
                "uncertainty",
                "requested_context_ids",
                "architecture_consequences",
                "revisit_triggers",
                "producer_id",
                "producer_version",
            },
            field="engineering_decision",
        )
        if value["schema"] != ENGINEERING_DECISION_SCHEMA:
            _fail("schema_mismatch", "unsupported EngineeringDecision schema")
        result = cls(
            value["decision_id"],
            _parse_intent(value["intent_basis"]),
            tuple(_parse_repo_binding(item, field="decision.repo_view_bases[]") for item in value["repo_view_bases"]),
            _digest_sequence(value["context_package_ids"], field="decision.context_package_ids"),
            _digest_sequence(value["established_fact_claim_ids"], field="decision.established_fact_claim_ids"),
            _digest_sequence(value["inference_claim_ids"], field="decision.inference_claim_ids"),
            _digest_sequence(value["assumption_claim_ids"], field="decision.assumption_claim_ids"),
            _digest_sequence(value["hypothesis_claim_ids"], field="decision.hypothesis_claim_ids"),
            tuple(DecisionOption.from_mapping(item) for item in value["alternatives"]),
            value["chosen_option_id"],
            _sequence(value["must_preserve"], field="decision.must_preserve"),
            _sequence(value["must_not"], field="decision.must_not"),
            _sequence(value["expected_effect_scope"], field="decision.expected_effect_scope"),
            _sequence(value["acceptance_obligations"], field="decision.acceptance_obligations"),
            _sequence(value["evidence_obligations"], field="decision.evidence_obligations"),
            _sequence(value["uncertainty"], field="decision.uncertainty"),
            _digest_sequence(value["requested_context_ids"], field="decision.requested_context_ids"),
            _sequence(value["architecture_consequences"], field="decision.architecture_consequences"),
            _sequence(value["revisit_triggers"], field="decision.revisit_triggers"),
            value["producer_id"],
            value["producer_version"],
        )
        return result


def _validate_unique_claim_lists(decision: EngineeringDecision) -> None:
    groups = (
        decision.established_fact_claim_ids,
        decision.inference_claim_ids,
        decision.assumption_claim_ids,
        decision.hypothesis_claim_ids,
    )
    flattened = [claim_id for group in groups for claim_id in group]
    if len(flattened) != len(set(flattened)):
        _fail("decision_claim_overlap", "a decision claim cannot be classified in multiple epistemic lists")


def validate_decision_applicability(
    decision: EngineeringDecision,
    *,
    intent_basis: IntentBasis,
    repo_views: Sequence[CommittedRepoView | RepoViewBinding],
    context_packages: Sequence[ContextPackage],
) -> None:
    """Reject reuse when any exact intent, RepoView or package basis changed."""

    if decision.intent_basis != intent_basis:
        if decision.intent_basis.intent_revision != intent_basis.intent_revision or decision.intent_basis.intent_digest != intent_basis.intent_digest:
            _fail("stale_intent_basis", "EngineeringDecision intent revision/digest is stale")
        _fail("stale_decision_basis", "EngineeringDecision task identity differs")
    current_views = tuple(
        RepoViewBinding.from_view(view) if isinstance(view, CommittedRepoView) else view for view in repo_views
    )
    if set(decision.repo_view_bases) != set(current_views):
        _fail("stale_decision_basis", "EngineeringDecision RepoView basis set is not exactly current")
    current_packages = {package.package_id: package for package in context_packages}
    if set(decision.context_package_ids) != set(current_packages):
        _fail("stale_decision_basis", "EngineeringDecision ContextPackage basis set is not exactly current")
    for package in current_packages.values():
        if package.intent_basis != intent_basis or package.repo_view not in current_views:
            _fail("stale_decision_basis", "EngineeringDecision ContextPackage basis is stale")


def semantic_record_bytes(record: object) -> bytes:
    """Return canonical bytes for one explicit M2c record, with no protocol fields."""

    as_dict = getattr(record, "as_dict", None)
    if not callable(as_dict):
        _fail("invalid_semantic_record", "M2c transport requires a semantic record with as_dict()")
    document = as_dict()
    if not isinstance(document, Mapping) or not isinstance(document.get("schema"), str):
        _fail("invalid_semantic_record", "semantic record must expose a schema")
    return canonical_json_bytes(document)


def semantic_record_content_ref(record: object):
    """Create the M2b ContentRef for canonical M2c record bytes."""

    document = getattr(record, "as_dict", lambda: None)()
    if not isinstance(document, Mapping) or not isinstance(document.get("schema"), str):
        _fail("invalid_semantic_record", "semantic record must expose a schema")
    return make_content_ref(SEMANTIC_RECORD_CONTENT_TYPE, document["schema"], semantic_record_bytes(record))


def publish_semantic_record(
    record: object,
    view: CommittedRepoView,
    content_store: ImmutableContentStore,
    binding_store: DurableBindingStore,
) -> TypedContextFragment:
    """Publish and durably accept one record through the existing M2b substrate."""

    if not isinstance(view, CommittedRepoView):
        _fail("malformed_repo_view_basis", "semantic record publication requires CommittedRepoView")
    raw = semantic_record_bytes(record)
    document = record.as_dict()  # type: ignore[union-attr]
    content_ref = make_content_ref(SEMANTIC_RECORD_CONTENT_TYPE, document["schema"], raw)
    content_store.publish(content_ref, raw)
    fragment = TypedContextFragment.create(
        view,
        content_ref,
        fragment_type=SEMANTIC_RECORD_CONTENT_TYPE,
        fragment_schema=document["schema"],
        payload_size_bytes=len(raw),
    )
    binding_store.accept(fragment, view=view)
    return fragment


def reconstruct_semantic_record(raw: bytes) -> object:
    """Reconstruct only known M2c records from exact canonical bytes."""

    if not isinstance(raw, bytes):
        _fail("invalid_semantic_record", "semantic record bytes must be bytes")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EngineeringIntelligenceError("malformed_semantic_record", "semantic record bytes are not canonical JSON") from exc
    if not isinstance(document, Mapping) or canonical_json_bytes(document) != raw:
        _fail("malformed_semantic_record", "semantic record bytes are not canonical")
    schema = document.get("schema")
    parsers = {
        UNDERSTANDING_SCHEMA: RepositoryUnderstandingView.from_mapping,
        CONTEXT_PACKAGE_SCHEMA: ContextPackage.from_mapping,
        CONTEXT_REQUEST_SCHEMA: ContextRequest.from_mapping,
        CONTEXT_RESOLUTION_SCHEMA: ContextResolution.from_mapping,
        ENGINEERING_DECISION_SCHEMA: EngineeringDecision.from_mapping,
    }
    parser = parsers.get(schema)
    if parser is None:
        _fail("schema_mismatch", "semantic record schema is not an M2c record")
    return parser(document)


def transport_semantic_record(
    record: object,
    view: CommittedRepoView,
    content_store: ImmutableContentStore,
    binding_store: DurableBindingStore,
) -> tuple[TypedContextFragment, object]:
    """Exercise the exact M2b Browser→Native path for one semantic record."""

    fragment = publish_semantic_record(record, view, content_store, binding_store)
    envelope = BrowserTransportProvider().encode(binding_store, fragment, expected_view=view)
    decoded = NativeTransportProvider().decode(envelope, bindings=binding_store, expected_view=view)
    if decoded.fragment != fragment:
        _fail("transport_integrity_failure", "M2b transport returned a different accepted fragment")
    return fragment, reconstruct_semantic_record(decoded.raw)


__all__ = [
    "AFFORDANCE_SCHEMA",
    "CLAIM_KINDS",
    "CLAIM_SCHEMA",
    "UNDERSTANDING_SCHEMA",
    "CONTEXT_PACKAGE_SCHEMA",
    "CONTEXT_REQUEST_SCHEMA",
    "CONTEXT_RESOLUTION_SCHEMA",
    "CONTRADICTION_SCHEMA",
    "ContextAffordance",
    "ContextPackage",
    "ContextRequest",
    "ContextResolution",
    "DecisionOption",
    "ENGINEERING_DECISION_SCHEMA",
    "EngineeringDecision",
    "EngineeringIntelligenceError",
    "HORIZONS",
    "IntentBasis",
    "M2C_POLICY_VERSION",
    "M2C_PRODUCER_ID",
    "M2C_PRODUCER_VERSION",
    "OMISSION_SCHEMA",
    "RepositoryUnderstandingView",
    "RepoSourceEvidence",
    "REPO_SOURCE_EVIDENCE_SCHEMA",
    "SEMANTIC_RECORD_CONTENT_TYPE",
    "UNKNOWN_SCHEMA",
    "UnderstandingClaim",
    "ClaimContradiction",
    "Unknown",
    "Omission",
    "publish_semantic_record",
    "publish_repo_source_evidence",
    "reconstruct_semantic_record",
    "semantic_record_bytes",
    "semantic_record_content_ref",
    "transport_semantic_record",
    "validate_decision_applicability",
]

```

## SOURCE bdb_vnext/composition.py
object: e537abd96419846bd102fd6a4afb2799605294ae
size_bytes: 42779
raw_sha256: sha256:1d729dc8baefbf073b6e935f9f3f276af33fdf0d9c3135b98c524301405db9d4
```text
"""Read-only M1a identity/composition contract; it never activates or creates vNext state."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bdb_shared.evidence import canonical_json_bytes, sanitize_report, semantic_digest


COMPOSITION_SCHEMA = "bdb-vnext-composition-v1"
STATUS_SCHEMA = "bdb-vnext-composition-status-v1"
ARCHITECTURE_FREEZE = "BDB Architecture Freeze v1"
EXECUTION_STRATEGY = "parallel-vnext-build-v1"
SOURCE_BRANCH = "bdb-vnext"
GENERATION_ID = "bdb-vnext-g1"
RUNTIME_ID = "devmaster.bdb.vnext.runtime"
PROTOCOL_GENERATION = "bdb-vnext-protocol-v1"
CONFIG_GENERATION = "bdb-vnext-config-v1"
CONTROL_STORE_SCHEMA = "bdb-vnext-control-store-v1"
NATIVE_HOST_NAME = "com.bartosz.dev_bridge.vnext"
LEGACY_NATIVE_HOST_NAME = "com.bartosz.dev_bridge"
BROWSER_COMPONENT_ID = "devmaster.bdb.vnext.browser-extension"
BROWSER_IDENTITY_SCHEMA = "bdb-vnext-browser-identity-v1"
BROWSER_EXTENSION_ID = "mopnolkjddkmgojfjkenjobehhmmklll"
NATIVE_COMPONENT_ID = "devmaster.bdb.vnext.native-host"
CONTROL_CENTER_COMPONENT_ID = "devmaster.bdb.vnext.control-center"
COMPOSITION_PROVIDER_ID = "devmaster.bdb.vnext.composition-manifest"
CONTROL_PROVIDER_ID = "devmaster.bdb.vnext.control-store"
NATIVE_PROVIDER_ID = "devmaster.bdb.vnext.native-transport"
BROWSER_PROVIDER_ID = "devmaster.bdb.vnext.browser-transport"
CONTROL_CENTER_PROVIDER_ID = "devmaster.bdb.vnext.control-center-query"
REPO_VIEW_COMPONENT_ID = "devmaster.bdb.vnext.repo-view"
REPO_VIEW_PROVIDER_ID = "devmaster.bdb.vnext.repo-view"

_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_EXTENSION_ID = re.compile(r"^[a-p]{32}$")
_MAX_MANIFEST_BYTES = 256 * 1024
_MAX_IDENTITY_BYTES = 32 * 1024
_MAX_BUNDLE_FILES = 4_096
_MAX_BUNDLE_FILE_BYTES = 64 * 1024 * 1024
_MAX_BUNDLE_TOTAL_BYTES = 512 * 1024 * 1024
_HEX_TO_EXTENSION_ID = str.maketrans("0123456789abcdef", "abcdefghijklmnop")
_BUNDLE_COMPONENT_IDS = (
    RUNTIME_ID,
    BROWSER_COMPONENT_ID,
    NATIVE_COMPONENT_ID,
    CONTROL_CENTER_COMPONENT_ID,
)


class VNextCompositionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def default_local_app_data_root() -> Path:
    value = os.environ.get("LOCALAPPDATA")
    if value:
        return Path(value).expanduser().resolve(strict=False)
    return (Path.home() / "AppData" / "Local").resolve(strict=False)


def default_vnext_runtime_root() -> Path:
    return default_local_app_data_root() / "BartoszDevBridge-vNext"


def default_legacy_runtime_root() -> Path:
    return default_local_app_data_root() / "BartoszDevBridge"


def default_browser_identity_path() -> Path:
    return Path(__file__).with_name("browser_identity.json")


def _file_token(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _read_stable_bytes(path: str | Path, *, max_bytes: int, field: str) -> tuple[Path, bytes]:
    source = Path(path).expanduser().absolute()
    _assert_no_reparse_components(source, code="invalid_artifact_path", field=field)
    before = source.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise VNextCompositionError("invalid_artifact_path", f"{field} must be a regular file")
    if before.st_size > max_bytes:
        raise VNextCompositionError("artifact_too_large", f"{field} exceeds its bounded read limit")
    with source.open("rb") as handle:
        payload = handle.read(max_bytes + 1)
    after = source.stat(follow_symlinks=False)
    if _file_token(before) != _file_token(after):
        raise VNextCompositionError("artifact_unstable", f"{field} changed during observation")
    if len(payload) > max_bytes:
        raise VNextCompositionError("artifact_too_large", f"{field} exceeds its bounded read limit")
    return source.resolve(strict=True), payload


def _derive_extension_id(public_key_der: bytes) -> str:
    return hashlib.sha256(public_key_der).hexdigest()[:32].translate(_HEX_TO_EXTENSION_ID)


def load_browser_identity(path: str | Path | None = None) -> dict[str, Any]:
    source, payload = _read_stable_bytes(
        path if path is not None else default_browser_identity_path(),
        max_bytes=_MAX_IDENTITY_BYTES,
        field="browser_identity",
    )
    try:
        value = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise VNextCompositionError("invalid_browser_identity", "Browser identity is not valid JSON") from exc
    identity = _mapping(value, field="browser_identity")
    _exact_keys(
        identity,
        {
            "schema",
            "component_id",
            "extension_id",
            "algorithm",
            "public_key_spki_der_base64",
            "public_key_sha256",
            "private_key_in_repository",
            "semantic_digest",
        },
        field="browser_identity",
    )
    if (
        identity.get("schema") != BROWSER_IDENTITY_SCHEMA
        or identity.get("component_id") != BROWSER_COMPONENT_ID
        or identity.get("algorithm") != "RSA-2048-SPKI-SHA256"
        or identity.get("private_key_in_repository") is not False
    ):
        raise VNextCompositionError("browser_identity_mismatch", "Browser packaging identity differs")
    encoded_key = identity.get("public_key_spki_der_base64")
    if not isinstance(encoded_key, str):
        raise VNextCompositionError("invalid_browser_identity", "Browser public key is missing")
    try:
        public_key = base64.b64decode(encoded_key, validate=True)
    except ValueError as exc:
        raise VNextCompositionError("invalid_browser_identity", "Browser public key is invalid") from exc
    key_digest = "sha256:" + hashlib.sha256(public_key).hexdigest()
    extension_id = _derive_extension_id(public_key)
    if len(public_key) < 256 or identity.get("public_key_sha256") != key_digest:
        raise VNextCompositionError("browser_key_mismatch", "Browser public key digest differs")
    if identity.get("extension_id") != extension_id or extension_id != BROWSER_EXTENSION_ID:
        raise VNextCompositionError("browser_extension_id_mismatch", "Browser extension ID differs")
    if identity.get("semantic_digest") != semantic_digest(identity):
        raise VNextCompositionError("browser_identity_digest_mismatch", "Browser identity digest differs")
    if source.name != "browser_identity.json":
        raise VNextCompositionError("browser_identity_mismatch", "Browser identity resource name differs")
    return dict(identity)


def _is_reparse(info: os.stat_result) -> bool:
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(info, "st_file_attributes", 0) & marker)


def _assert_no_reparse_components(path: Path, *, code: str, field: str) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        info = current.stat(follow_symlinks=False)
        if current.is_symlink() or _is_reparse(info):
            raise VNextCompositionError(
                code,
                f"{field} must not contain symlinks, junctions or reparse points",
            )


def _sha256_file(path: Path) -> tuple[int, str]:
    before = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or _is_reparse(before):
        raise VNextCompositionError("invalid_bundle_entry", "bundle entries must be regular files")
    if before.st_size > _MAX_BUNDLE_FILE_BYTES:
        raise VNextCompositionError("bundle_file_too_large", "bundle file exceeds the bounded read limit")
    digest = hashlib.sha256()
    observed = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            observed += len(chunk)
            if observed > _MAX_BUNDLE_FILE_BYTES:
                raise VNextCompositionError(
                    "bundle_file_too_large",
                    "bundle file exceeds the bounded read limit",
                )
            digest.update(chunk)
    after = path.stat(follow_symlinks=False)
    if _file_token(before) != _file_token(after):
        raise VNextCompositionError("bundle_unstable", "bundle file changed during observation")
    return observed, "sha256:" + digest.hexdigest()


def _directory_membership(path: Path) -> tuple[tuple[str, str, tuple[int, int, int, int]], ...]:
    entries: list[tuple[str, str, tuple[int, int, int, int]]] = []
    with os.scandir(path) as iterator:
        for entry in iterator:
            info = entry.stat(follow_symlinks=False)
            if entry.is_symlink() or _is_reparse(info):
                raise VNextCompositionError(
                    "bundle_reparse_point",
                    "bundle must not contain symlinks, junctions or reparse points",
                )
            if stat.S_ISDIR(info.st_mode):
                kind = "directory"
            elif stat.S_ISREG(info.st_mode):
                kind = "file"
            else:
                raise VNextCompositionError("invalid_bundle_entry", "bundle entry type is unsupported")
            entries.append((entry.name, kind, _file_token(info)))
    return tuple(sorted(entries, key=lambda item: item[0]))


def _observe_bundle_directory(root: Path) -> tuple[int, int, str]:
    pending = [root]
    memberships: dict[Path, tuple[tuple[str, str, tuple[int, int, int, int]], ...]] = {}
    records: list[dict[str, Any]] = []
    total_bytes = 0
    while pending:
        directory = pending.pop()
        membership = _directory_membership(directory)
        memberships[directory] = membership
        for name, kind, _token in reversed(membership):
            child = directory / name
            if kind == "directory":
                pending.append(child)
                continue
            if len(records) >= _MAX_BUNDLE_FILES:
                raise VNextCompositionError("bundle_file_cap", "bundle exceeds the bounded file-count limit")
            size_bytes, digest = _sha256_file(child)
            total_bytes += size_bytes
            if total_bytes > _MAX_BUNDLE_TOTAL_BYTES:
                raise VNextCompositionError("bundle_total_too_large", "bundle exceeds the total read limit")
            records.append(
                {
                    "path": child.relative_to(root).as_posix(),
                    "size_bytes": size_bytes,
                    "sha256": digest,
                }
            )
    if not records:
        raise VNextCompositionError("bundle_empty", "bundle directory must contain at least one file")
    for directory, before in memberships.items():
        if _directory_membership(directory) != before:
            raise VNextCompositionError("bundle_unstable", "bundle membership changed during observation")
    records.sort(key=lambda item: item["path"])
    payload = {"schema": "bdb-vnext-bundle-digest-v1", "files": records}
    digest = "sha256:" + hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    return len(records), total_bytes, digest


def _load_browser_bundle_manifest(root: Path, identity: Mapping[str, Any]) -> None:
    _source, payload = _read_stable_bytes(
        root / "manifest.json",
        max_bytes=_MAX_IDENTITY_BYTES,
        field="browser_bundle_manifest",
    )
    try:
        manifest = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise VNextCompositionError("invalid_browser_bundle", "Browser manifest is not valid JSON") from exc
    if not isinstance(manifest, Mapping):
        raise VNextCompositionError("invalid_browser_bundle", "Browser manifest must be an object")
    if manifest.get("manifest_version") != 3 or manifest.get("key") != identity["public_key_spki_der_base64"]:
        raise VNextCompositionError(
            "browser_bundle_identity_mismatch",
            "Browser bundle is not pinned to the vNext packaging identity",
        )
    if not isinstance(manifest.get("name"), str) or not manifest["name"].strip():
        raise VNextCompositionError("invalid_browser_bundle", "Browser bundle name is missing")


def _not_built_bundle(component_id: str) -> dict[str, Any]:
    return {
        "component_id": component_id,
        "state": "not_built",
        "kind": None,
        "path": None,
        "file_count": 0,
        "size_bytes": 0,
        "sha256": None,
    }


def observe_bundle(
    component_id: str,
    path: str | Path,
    *,
    legacy_runtime_root: str | Path,
) -> dict[str, Any]:
    if component_id not in _BUNDLE_COMPONENT_IDS:
        raise VNextCompositionError("unknown_bundle_component", "bundle component ID is unsupported")
    source = Path(path).expanduser()
    if not source.is_absolute():
        raise VNextCompositionError("relative_path", f"bundle[{component_id}] must be absolute")
    source = source.absolute()
    _assert_no_reparse_components(
        source,
        code="bundle_reparse_point",
        field=f"bundle[{component_id}]",
    )
    source = source.resolve(strict=True)
    legacy = _absolute_path(legacy_runtime_root, field="legacy_runtime_root")
    if _overlaps(source, legacy):
        raise VNextCompositionError("legacy_bundle_overlap", "vNext bundle observation overlaps legacy runtime")
    info = source.stat(follow_symlinks=False)
    if _is_reparse(info):
        raise VNextCompositionError("bundle_reparse_point", "bundle root must not be a reparse point")
    if stat.S_ISDIR(info.st_mode):
        if component_id == BROWSER_COMPONENT_ID:
            _load_browser_bundle_manifest(source, load_browser_identity())
        file_count, size_bytes, digest = _observe_bundle_directory(source)
        kind = "directory"
    elif stat.S_ISREG(info.st_mode):
        if component_id == BROWSER_COMPONENT_ID:
            raise VNextCompositionError(
                "invalid_browser_bundle",
                "M1a Browser packaging smoke requires an unpacked bundle directory",
            )
        size_bytes, file_digest = _sha256_file(source)
        file_count = 1
        digest_payload = {
            "schema": "bdb-vnext-bundle-digest-v1",
            "files": [{"path": source.name, "size_bytes": size_bytes, "sha256": file_digest}],
        }
        digest = "sha256:" + hashlib.sha256(canonical_json_bytes(digest_payload)).hexdigest()
        kind = "file"
    else:
        raise VNextCompositionError("invalid_bundle_entry", "bundle root type is unsupported")
    return {
        "component_id": component_id,
        "state": "observed",
        "kind": kind,
        "path": str(source),
        "file_count": file_count,
        "size_bytes": size_bytes,
        "sha256": digest,
    }


def _bundle_records(
    bundle_paths: Mapping[str, str | Path] | None,
    *,
    legacy_runtime_root: str | Path,
) -> list[dict[str, Any]]:
    supplied = dict(bundle_paths or {})
    unknown = set(supplied) - set(_BUNDLE_COMPONENT_IDS)
    if unknown:
        raise VNextCompositionError(
            "unknown_bundle_component",
            f"unsupported bundle component IDs: {sorted(unknown)}",
        )
    return [
        observe_bundle(component_id, supplied[component_id], legacy_runtime_root=legacy_runtime_root)
        if component_id in supplied
        else _not_built_bundle(component_id)
        for component_id in _BUNDLE_COMPONENT_IDS
    ]


def _absolute_path(value: str | Path, *, field: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise VNextCompositionError("relative_path", f"{field} must be absolute")
    return path.resolve(strict=False)


def _comparable(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.fspath(path)))


def _contained(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((_comparable(path), _comparable(root))) == _comparable(root)
    except ValueError:
        return False


def _overlaps(left: Path, right: Path) -> bool:
    return _contained(left, right) or _contained(right, left)


@dataclass(frozen=True)
class VNextLayout:
    runtime_root: Path
    control_store: Path
    spool_inbox: Path
    spool_results: Path
    receipts_store: Path
    instance_lock: Path
    pid_file: Path
    config: Path
    browser_bundle_root: Path
    native_host_manifest: Path

    @classmethod
    def create(cls, runtime_root: str | Path) -> "VNextLayout":
        root = _absolute_path(runtime_root, field="runtime_root")
        return cls(
            runtime_root=root,
            control_store=root / "control" / "control.db",
            spool_inbox=root / "transport" / "spool" / "inbox",
            spool_results=root / "transport" / "spool" / "results",
            receipts_store=root / "transport" / "receipts" / "native-requests.json",
            instance_lock=root / "coordination" / "bdb-vnext.lock",
            pid_file=root / "coordination" / "bdb-vnext.pid",
            config=root / "config" / "bdb-vnext.json",
            browser_bundle_root=root / "components" / "browser-extension",
            native_host_manifest=root / "config" / f"{NATIVE_HOST_NAME}.json",
        )

    def assert_isolated(
        self,
        *,
        legacy_runtime_root: str | Path,
        forbidden_roots: Iterable[str | Path] = (),
    ) -> None:
        legacy = _absolute_path(legacy_runtime_root, field="legacy_runtime_root")
        if _overlaps(self.runtime_root, legacy):
            raise VNextCompositionError(
                "legacy_runtime_overlap",
                "vNext and legacy runtime roots must not overlap",
            )
        for index, value in enumerate(forbidden_roots):
            root = _absolute_path(value, field=f"forbidden_roots[{index}]")
            if _overlaps(self.runtime_root, root):
                raise VNextCompositionError(
                    "foreign_state_overlap",
                    "vNext runtime root overlaps a forbidden source or foreign-state root",
                )

        domains = (
            self.runtime_root / "control",
            self.runtime_root / "transport",
            self.runtime_root / "coordination",
            self.runtime_root / "config",
            self.runtime_root / "components",
        )
        if not all(_contained(path, self.runtime_root) and path != self.runtime_root for path in domains):
            raise VNextCompositionError("path_escape", "a vNext mutable domain escapes runtime_root")
        for index, left in enumerate(domains):
            for right in domains[index + 1 :]:
                if _overlaps(left, right):
                    raise VNextCompositionError("mutable_domain_overlap", "vNext mutable domains overlap")

    def to_dict(self) -> dict[str, str]:
        return {
            "runtime_root": str(self.runtime_root),
            "control_store": str(self.control_store),
            "spool_inbox": str(self.spool_inbox),
            "spool_results": str(self.spool_results),
            "receipts_store": str(self.receipts_store),
            "instance_lock": str(self.instance_lock),
            "pid_file": str(self.pid_file),
            "config": str(self.config),
            "browser_bundle_root": str(self.browser_bundle_root),
            "native_host_manifest": str(self.native_host_manifest),
        }


def _native_registry_keys(host_name: str) -> list[str]:
    suffix = f"NativeMessagingHosts\\{host_name}"
    return [
        f"HKCU\\Software\\Google\\Chrome\\{suffix}",
        f"HKCU\\Software\\Microsoft\\Edge\\{suffix}",
    ]


def _provider(
    provider_id: str,
    component_id: str,
    kind: str,
    state: str,
) -> dict[str, Any]:
    return {
        "provider_id": provider_id,
        "component_id": component_id,
        "kind": kind,
        "state": state,
        "writer_enabled": False,
    }


def _provider_registry() -> list[dict[str, Any]]:
    return [
        _provider(
            COMPOSITION_PROVIDER_ID,
            RUNTIME_ID,
            "diagnostic_composition_manifest",
            "active_read_only",
        ),
        _provider(CONTROL_PROVIDER_ID, RUNTIME_ID, "control_store", "reserved_disabled"),
        _provider(NATIVE_PROVIDER_ID, NATIVE_COMPONENT_ID, "native_transport", "active_read_only"),
        _provider(BROWSER_PROVIDER_ID, BROWSER_COMPONENT_ID, "browser_transport", "active_read_only"),
        _provider(
            CONTROL_CENTER_PROVIDER_ID,
            CONTROL_CENTER_COMPONENT_ID,
            "control_center_query",
            "reserved_disabled",
        ),
        _provider(REPO_VIEW_PROVIDER_ID, REPO_VIEW_COMPONENT_ID, "repo_view", "active_read_only"),
    ]


def _composition_edges() -> list[dict[str, str]]:
    return [
        {"from": BROWSER_PROVIDER_ID, "to": NATIVE_PROVIDER_ID},
        {"from": NATIVE_PROVIDER_ID, "to": CONTROL_PROVIDER_ID},
        {"from": CONTROL_CENTER_PROVIDER_ID, "to": CONTROL_PROVIDER_ID},
    ]


def build_vnext_composition_manifest(
    *,
    source_commit: str,
    runtime_root: str | Path | None = None,
    legacy_runtime_root: str | Path | None = None,
    forbidden_roots: Iterable[str | Path] = (),
    legacy_extension_ids: Iterable[str] = (),
    bundle_paths: Mapping[str, str | Path] | None = None,
) -> dict[str, Any]:
    if not isinstance(source_commit, str) or _SHA40.fullmatch(source_commit) is None:
        raise VNextCompositionError("invalid_source_commit", "source_commit must be lowercase SHA-40")
    browser_identity = load_browser_identity()
    browser_extension_id = browser_identity["extension_id"]
    if browser_extension_id in set(legacy_extension_ids):
        raise VNextCompositionError(
            "browser_identity_overlap",
            "vNext Browser extension ID must differ from every legacy extension ID",
        )

    runtime = runtime_root if runtime_root is not None else default_vnext_runtime_root()
    legacy = legacy_runtime_root if legacy_runtime_root is not None else default_legacy_runtime_root()
    layout = VNextLayout.create(runtime)
    layout.assert_isolated(legacy_runtime_root=legacy, forbidden_roots=forbidden_roots)
    legacy_root = _absolute_path(legacy, field="legacy_runtime_root")
    browser_origin = f"chrome-extension://{browser_extension_id}/"
    document: dict[str, Any] = {
        "schema": COMPOSITION_SCHEMA,
        "manifest_version": 1,
        "architecture_freeze": ARCHITECTURE_FREEZE,
        "execution_strategy": EXECUTION_STRATEGY,
        "basis": {"source_commit": source_commit, "source_branch": SOURCE_BRANCH},
        "generation": {
            "generation_id": GENERATION_ID,
            "runtime_id": RUNTIME_ID,
            "protocol_generation": PROTOCOL_GENERATION,
            "config_generation": CONFIG_GENERATION,
            "mode": "build_only",
            "writer_enabled": False,
        },
        "paths": layout.to_dict(),
        "identities": {
            "control_store": {
                "store_id": "devmaster.bdb.vnext.control-store",
                "schema": CONTROL_STORE_SCHEMA,
            },
            "browser_extension": {
                "component_id": BROWSER_COMPONENT_ID,
                "extension_id": browser_extension_id,
                "origin": browser_origin,
                "identity_state": "bound",
                "packaging_key_policy": "dedicated_vnext_public_key_pinned",
                "identity_resource": "browser_identity.json",
                "identity_digest": browser_identity["semantic_digest"],
                "public_key_sha256": browser_identity["public_key_sha256"],
                "private_key_in_repository": False,
                "registration_state": "uninstalled",
            },
            "native_host": {
                "component_id": NATIVE_COMPONENT_ID,
                "host_name": NATIVE_HOST_NAME,
                "manifest_schema": "bdb-vnext-native-host-manifest-v1",
                "registration_state": "unregistered",
                "windows_registry_keys": _native_registry_keys(NATIVE_HOST_NAME),
            },
            "protocol": {
                "component_id": "devmaster.bdb.vnext.protocol",
                "generation": PROTOCOL_GENERATION,
                "minimum_version": 1,
                "maximum_version": 1,
                "legacy_compatible": False,
            },
        },
        "composition": {
            "root_provider_id": COMPOSITION_PROVIDER_ID,
            "providers": _provider_registry(),
            "edges": _composition_edges(),
        },
        "bundles": _bundle_records(bundle_paths, legacy_runtime_root=legacy_root),
        "legacy_boundary": {
            "runtime_root": str(legacy_root),
            "native_host_name": LEGACY_NATIVE_HOST_NAME,
            "current_role": "frozen_active_tool",
            "vnext_access": "none",
            "cutover_role": "read_only_archive",
            "semantic_migration_required": False,
        },
        "activation": {
            "state": "disabled",
            "writer_enabled": False,
            "manifest_is_activation_authority": False,
            "blockers": [
                "explicit_activation_execution_unit_required",
            ],
        },
    }
    document["semantic_digest"] = semantic_digest(document)
    validate_vnext_composition_manifest(document)
    return document


def _mapping(value: object, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise VNextCompositionError("invalid_manifest", f"{field} must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], *, field: str) -> None:
    actual = set(value)
    if actual != expected:
        raise VNextCompositionError(
            "invalid_manifest_fields",
            f"{field} fields differ: missing={sorted(expected - actual)}, unknown={sorted(actual - expected)}",
        )


def validate_vnext_composition_manifest(document: Mapping[str, Any]) -> None:
    _exact_keys(
        document,
        {
            "schema",
            "manifest_version",
            "architecture_freeze",
            "execution_strategy",
            "basis",
            "generation",
            "paths",
            "identities",
            "composition",
            "bundles",
            "legacy_boundary",
            "activation",
            "semantic_digest",
        },
        field="manifest",
    )
    if document.get("schema") != COMPOSITION_SCHEMA or document.get("manifest_version") != 1:
        raise VNextCompositionError("unsupported_manifest", "vNext composition schema is unsupported")
    if document.get("architecture_freeze") != ARCHITECTURE_FREEZE:
        raise VNextCompositionError("architecture_mismatch", "Architecture Freeze identity differs")
    if document.get("execution_strategy") != EXECUTION_STRATEGY:
        raise VNextCompositionError("strategy_mismatch", "execution strategy differs")

    basis = _mapping(document.get("basis"), field="basis")
    _exact_keys(basis, {"source_commit", "source_branch"}, field="basis")
    commit = basis.get("source_commit")
    if not isinstance(commit, str) or _SHA40.fullmatch(commit) is None:
        raise VNextCompositionError("invalid_source_commit", "basis.source_commit is invalid")
    if basis.get("source_branch") != SOURCE_BRANCH:
        raise VNextCompositionError("branch_mismatch", "basis.source_branch must be bdb-vnext")

    generation = _mapping(document.get("generation"), field="generation")
    expected_generation = {
        "generation_id": GENERATION_ID,
        "runtime_id": RUNTIME_ID,
        "protocol_generation": PROTOCOL_GENERATION,
        "config_generation": CONFIG_GENERATION,
        "mode": "build_only",
        "writer_enabled": False,
    }
    if dict(generation) != expected_generation:
        raise VNextCompositionError("generation_mismatch", "vNext generation declaration differs")

    paths = _mapping(document.get("paths"), field="paths")
    root_value = paths.get("runtime_root")
    if not isinstance(root_value, str):
        raise VNextCompositionError("invalid_manifest", "paths.runtime_root must be a string")
    root = _absolute_path(root_value, field="paths.runtime_root")
    expected_paths = VNextLayout.create(root).to_dict()
    if dict(paths) != expected_paths:
        raise VNextCompositionError("layout_mismatch", "vNext runtime layout differs")

    legacy = _mapping(document.get("legacy_boundary"), field="legacy_boundary")
    legacy_root_value = legacy.get("runtime_root")
    if not isinstance(legacy_root_value, str):
        raise VNextCompositionError("invalid_manifest", "legacy_boundary.runtime_root must be a string")
    legacy_root = _absolute_path(legacy_root_value, field="legacy_boundary.runtime_root")
    VNextLayout.create(root).assert_isolated(legacy_runtime_root=legacy_root)
    expected_legacy = {
        "runtime_root": str(legacy_root),
        "native_host_name": LEGACY_NATIVE_HOST_NAME,
        "current_role": "frozen_active_tool",
        "vnext_access": "none",
        "cutover_role": "read_only_archive",
        "semantic_migration_required": False,
    }
    if dict(legacy) != expected_legacy:
        raise VNextCompositionError("legacy_boundary_mismatch", "legacy boundary differs")

    identities = _mapping(document.get("identities"), field="identities")
    _exact_keys(
        identities,
        {"control_store", "browser_extension", "native_host", "protocol"},
        field="identities",
    )
    control = _mapping(identities.get("control_store"), field="identities.control_store")
    expected_control = {
        "store_id": "devmaster.bdb.vnext.control-store",
        "schema": CONTROL_STORE_SCHEMA,
    }
    if dict(control) != expected_control:
        raise VNextCompositionError("control_store_identity_mismatch", "Control Store identity differs")
    browser = _mapping(identities.get("browser_extension"), field="identities.browser_extension")
    browser_identity = load_browser_identity()
    _exact_keys(
        browser,
        {
            "component_id",
            "extension_id",
            "origin",
            "identity_state",
            "packaging_key_policy",
            "identity_resource",
            "identity_digest",
            "public_key_sha256",
            "private_key_in_repository",
            "registration_state",
        },
        field="identities.browser_extension",
    )
    extension_id = browser.get("extension_id")
    if extension_id is not None and (
        not isinstance(extension_id, str) or _EXTENSION_ID.fullmatch(extension_id) is None
    ):
        raise VNextCompositionError("invalid_browser_extension_id", "Browser extension ID is invalid")
    expected_browser = {
        "component_id": BROWSER_COMPONENT_ID,
        "extension_id": browser_identity["extension_id"],
        "origin": f"chrome-extension://{browser_identity['extension_id']}/",
        "identity_state": "bound",
        "packaging_key_policy": "dedicated_vnext_public_key_pinned",
        "identity_resource": "browser_identity.json",
        "identity_digest": browser_identity["semantic_digest"],
        "public_key_sha256": browser_identity["public_key_sha256"],
        "private_key_in_repository": False,
        "registration_state": "uninstalled",
    }
    if dict(browser) != expected_browser:
        raise VNextCompositionError("browser_identity_mismatch", "Browser identity state differs")

    native = _mapping(identities.get("native_host"), field="identities.native_host")
    expected_native = {
        "component_id": NATIVE_COMPONENT_ID,
        "host_name": NATIVE_HOST_NAME,
        "manifest_schema": "bdb-vnext-native-host-manifest-v1",
        "registration_state": "unregistered",
        "windows_registry_keys": _native_registry_keys(NATIVE_HOST_NAME),
    }
    if dict(native) != expected_native:
        raise VNextCompositionError("native_identity_mismatch", "Native Host identity differs")
    protocol = _mapping(identities.get("protocol"), field="identities.protocol")
    expected_protocol = {
        "component_id": "devmaster.bdb.vnext.protocol",
        "generation": PROTOCOL_GENERATION,
        "minimum_version": 1,
        "maximum_version": 1,
        "legacy_compatible": False,
    }
    if dict(protocol) != expected_protocol:
        raise VNextCompositionError("protocol_generation_mismatch", "protocol generation differs")

    composition = _mapping(document.get("composition"), field="composition")
    _exact_keys(composition, {"root_provider_id", "providers", "edges"}, field="composition")
    providers = composition.get("providers")
    if not isinstance(providers, list) or providers != _provider_registry():
        raise VNextCompositionError("provider_registry_missing", "provider registry is missing")
    provider_ids = [item.get("provider_id") for item in providers if isinstance(item, Mapping)]
    if len(provider_ids) != len(providers) or len(provider_ids) != len(set(provider_ids)):
        raise VNextCompositionError("duplicate_provider_id", "provider IDs must be unique")
    if (
        composition.get("root_provider_id") != COMPOSITION_PROVIDER_ID
        or COMPOSITION_PROVIDER_ID not in provider_ids
    ):
        raise VNextCompositionError("composition_root_missing", "composition root provider is missing")
    if any(item.get("writer_enabled") is not False for item in providers):
        raise VNextCompositionError("writer_enabled", "M1a-vNext providers must remain read-only/disabled")
    edges = composition.get("edges")
    if not isinstance(edges, list) or edges != _composition_edges():
        raise VNextCompositionError("composition_edge_invalid", "composition edge is invalid")

    bundles = document.get("bundles")
    if not isinstance(bundles, list) or any(not isinstance(bundle, Mapping) for bundle in bundles):
        raise VNextCompositionError("bundle_identity_invalid", "bundle identity is invalid")
    bundle_ids = [bundle.get("component_id") for bundle in bundles]
    if tuple(bundle_ids) != _BUNDLE_COMPONENT_IDS:
        raise VNextCompositionError("bundle_identity_invalid", "bundle component IDs differ")
    observed_paths: list[Path] = []
    for bundle in bundles:
        _exact_keys(
            bundle,
            {
                "component_id",
                "state",
                "kind",
                "path",
                "file_count",
                "size_bytes",
                "sha256",
            },
            field="bundles[]",
        )
        state = bundle.get("state")
        digest_value = bundle.get("sha256")
        if state == "not_built":
            if dict(bundle) != _not_built_bundle(str(bundle["component_id"])):
                raise VNextCompositionError("bundle_identity_invalid", "not-built bundle differs")
            continue
        if state != "observed":
            raise VNextCompositionError("bundle_identity_invalid", "bundle state is unsupported")
        raw_path = bundle.get("path")
        file_count = bundle.get("file_count")
        size_bytes = bundle.get("size_bytes")
        if (
            bundle.get("kind") not in {"file", "directory"}
            or not isinstance(raw_path, str)
            or not isinstance(file_count, int)
            or isinstance(file_count, bool)
            or not 1 <= file_count <= _MAX_BUNDLE_FILES
            or not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or not 0 <= size_bytes <= _MAX_BUNDLE_TOTAL_BYTES
            or not isinstance(digest_value, str)
            or _SHA256.fullmatch(digest_value) is None
        ):
            raise VNextCompositionError("bundle_identity_invalid", "observed bundle digest is invalid")
        path_value = _absolute_path(raw_path, field="bundles[].path")
        if _overlaps(path_value, legacy_root):
            raise VNextCompositionError("legacy_bundle_overlap", "bundle overlaps legacy runtime")
        if bundle.get("component_id") == BROWSER_COMPONENT_ID and bundle.get("kind") != "directory":
            raise VNextCompositionError("invalid_browser_bundle", "Browser bundle must be a directory")
        observed_paths.append(path_value)
    for index, left in enumerate(observed_paths):
        for right in observed_paths[index + 1 :]:
            if _overlaps(left, right):
                raise VNextCompositionError("bundle_path_overlap", "observed vNext bundle paths overlap")

    activation = _mapping(document.get("activation"), field="activation")
    expected_blockers = ["explicit_activation_execution_unit_required"]
    expected_activation = {
        "state": "disabled",
        "writer_enabled": False,
        "manifest_is_activation_authority": False,
        "blockers": expected_blockers,
    }
    if dict(activation) != expected_activation:
        raise VNextCompositionError("activation_enabled", "M1a manifest cannot activate vNext")

    digest = document.get("semantic_digest")
    if not isinstance(digest, str) or digest != semantic_digest(document):
        raise VNextCompositionError("digest_mismatch", "composition semantic digest differs")


def load_vnext_composition_manifest(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser().absolute()
    if source.is_symlink():
        raise VNextCompositionError("invalid_manifest_path", "manifest must be a regular file")
    before = source.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise VNextCompositionError("invalid_manifest_path", "manifest must be a regular file")
    if before.st_size > _MAX_MANIFEST_BYTES:
        raise VNextCompositionError("manifest_too_large", "manifest exceeds the bounded read limit")
    with source.open("rb") as handle:
        payload = handle.read(_MAX_MANIFEST_BYTES + 1)
    after = source.stat(follow_symlinks=False)
    before_token = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_token = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if before_token != after_token:
        raise VNextCompositionError("manifest_unstable", "manifest changed during observation")
    if len(payload) > _MAX_MANIFEST_BYTES:
        raise VNextCompositionError("manifest_too_large", "manifest exceeds the bounded read limit")
    value = json.loads(payload.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise VNextCompositionError("invalid_manifest", "manifest root must be an object")
    validate_vnext_composition_manifest(value)
    return value


def composition_status(
    expected: Mapping[str, Any],
    observed: Mapping[str, Any] | None,
) -> dict[str, Any]:
    validate_vnext_composition_manifest(expected)
    blockers: list[dict[str, str]] = []
    observed_digest: str | None = None
    if observed is None:
        blockers.append({"code": "manifest_missing", "field": "observed"})
    else:
        observed_digest_value = observed.get("semantic_digest")
        observed_digest = observed_digest_value if isinstance(observed_digest_value, str) else None
        try:
            validate_vnext_composition_manifest(observed)
        except VNextCompositionError as exc:
            blockers.append({"code": exc.code, "field": "observed"})
        else:
            if observed_digest != expected.get("semantic_digest"):
                blockers.append({"code": "composition_digest_mismatch", "field": "semantic_digest"})

    return {
        "schema": STATUS_SCHEMA,
        "result": "MATCH" if not blockers else "MISMATCH",
        "compatible": not blockers,
        "activation_ready": False,
        "expected_digest": expected.get("semantic_digest"),
        "observed_digest": observed_digest,
        "blockers": blockers,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bdb-vnext-manifest",
        description="Build or compare the read-only M1a vNext composition manifest",
    )
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--legacy-runtime-root", type=Path)
    parser.add_argument("--forbid-root", action="append", type=Path, default=[])
    parser.add_argument("--legacy-extension-id", action="append", default=[])
    parser.add_argument(
        "--bundle",
        action="append",
        default=[],
        metavar="COMPONENT_ID=PATH",
        help="Observe one explicit vNext bundle without writing to it",
    )
    parser.add_argument("--observed-manifest", type=Path)
    parser.add_argument("--sanitized", action="store_true")
    return parser


def _parse_bundle_bindings(values: Sequence[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        component_id, separator, raw_path = value.partition("=")
        if not separator or not raw_path or component_id not in _BUNDLE_COMPONENT_IDS:
            raise VNextCompositionError(
                "invalid_bundle_binding",
                "--bundle must use a supported COMPONENT_ID=PATH binding",
            )
        if component_id in result:
            raise VNextCompositionError("duplicate_bundle_binding", "bundle component was supplied twice")
        result[component_id] = Path(raw_path)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest = build_vnext_composition_manifest(
            source_commit=args.source_commit,
            runtime_root=args.runtime_root,
            legacy_runtime_root=args.legacy_runtime_root,
            forbidden_roots=args.forbid_root,
            legacy_extension_ids=args.legacy_extension_id,
            bundle_paths=_parse_bundle_bindings(args.bundle),
        )
        if args.observed_manifest is None:
            result: Mapping[str, Any] = manifest
            exit_code = 0
        else:
            observed = load_vnext_composition_manifest(args.observed_manifest)
            result = composition_status(manifest, observed)
            exit_code = 0 if result["compatible"] is True else 2
        if args.sanitized:
            result = sanitize_report(result)
        sys.stdout.buffer.write(canonical_json_bytes(result))
        return exit_code
    except (OSError, UnicodeError, json.JSONDecodeError, VNextCompositionError) as exc:
        code = exc.code if isinstance(exc, VNextCompositionError) else "manifest_unavailable"
        sys.stderr.write(f"bdb-vnext-manifest failed: {code}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

```

## SOURCE docs/m2b-vnext-typed-context-transport.md
object: f4b2045dd91102c2df0cc8ee1001934478a29422
size_bytes: 5287
raw_sha256: sha256:8f6e22ebb3bc6f6d08155363dabffe602023d48ed5e56dea102357e01a0010d1
```text
# M2b-vNext — Typed Context Transport

M2b establishes a build-only exact-or-fail chain from an accepted hardened
M2a `COMMITTED` RepoView to a typed Browser/Native transport envelope. It does
not activate the runtime, install the extension, register the Native Host, or
introduce task/submission/work lifecycle state.

## Exact contracts

`ContentRef` preserves the accepted X2 four-field contract exactly:

```json
{
  "type": "...",
  "schema": "...",
  "semantic_digest": "sha256:...",
  "raw_digest": "sha256:..."
}
```

`TypedContextFragment` is defined by
[`bdb-vnext-context-fragment-v1.schema.json`](../schemas/bdb-vnext-context-fragment-v1.schema.json).
It binds:

- `repository_id`, `repository_identity_digest`, `object_format`,
  `commit_oid`, `tree_oid`, and `view_id` from the exact M2a RepoView;
- `fragment_type` and `fragment_schema`;
- the complete four-field `ContentRef`;
- bounded `payload_size_bytes` (maximum `1,048,576`);
- deterministic `fragment_id` over all of the above.

The payload bytes are never reconstructed from a filename or a moving ref.
They are read from the immutable M2b content object addressed by `raw_digest`.

The transport envelope is defined by
[`bdb-vnext-transport-envelope-v1.schema.json`](../schemas/bdb-vnext-transport-envelope-v1.schema.json):

- protocol generation: `bdb-vnext-protocol-v1`;
- protocol version: `1`;
- message kind: `typed_context_fragment`;
- deterministic `message_id`;
- exact fragment metadata;
- exact `payload_length_bytes` and strict base64 payload;
- maximum envelope size: `2,097,152` bytes.

Browser encoding is provided by
`devmaster.bdb.vnext.browser-transport`; Native decoding is provided by
`devmaster.bdb.vnext.native-transport`. Both are read-only adapters in one
testable Python harness. No Browser process, extension installation, Native
Host registration, or user activation occurs.

## Durable acceptance and recovery

`bdb_vnext/content_store.py` contains the isolated M2b substrate. Its
`control/control.db` is a versioned `m2b_accepted_bindings` SQLite store, not
the future canonical Control Store: it contains no Task, Submission, WorkItem,
effect, scheduler, or lifecycle tables.

Acceptance ordering is fixed:

1. publish the complete content object to the same-volume immutable object
   path (`os.link(temp, target, follow_symlinks=False)`; no committed target is
   overwritten);
2. publish and verify the exact ContentRef metadata;
3. verify the bytes and exact RepoView binding, then atomically accept the
   fragment row in SQLite (`BEGIN IMMEDIATE`, `synchronous=FULL`);
4. only an accepted binding may be emitted by the Browser adapter.

Duplicate exact publication converges. An incompatible existing object/ref or
conflicting accepted binding fails closed. An object without an accepted
binding is non-authoritative.

M2b-R2 uses handle-bound reads: the path is checked for symlink/reparse and
regular-file identity, opened with `O_NOFOLLOW` when available, read through
the descriptor, and revalidated against the open handle and pathname after the
read. Publication uses a no-overwrite hard-link create-if-absent primitive.
This is a bounded same-root pathname/reparse defence, not a hostile-admin
sandbox.

The recovery test uses the M1b coordinated backup/restore API with the fixed
M1b subjects (`control/control.db`, optional WAL, `content`, and
`config/bdb-vnext.json`). After restore it reopens M2b storage and verifies
exact RepoView identity, ContentRef, both digests, and bytes. Tamper cases for
wrong ContentRef metadata, missing object, wrong object bytes, and RepoView
binding mismatch all fail closed.

## Provider identity and isolation

M1c remains the only explicit composition root. The current build-only root
binds the tested Browser and Native adapters, plus the existing composition
diagnostic and RepoView providers. Control Store and Control Center remain
`RESERVED`.

Each transport binding contributes deterministic:

- provider contract;
- contract version;
- exact implementation module and qualname;
- explicit implementation revision;
- implementation identity digest derived from that stable, versioned descriptor.

No identity depends on object representation, memory address, import order, or
mutable global state. Runtime, writer, and activation remain `OFF / OFF / OFF`.

## Exact-or-fail matrix

Focused tests cover:

- exact Browser→Native roundtrip;
- deterministic envelope and strict canonical framing;
- wrong generation, unsupported version, legacy generation, unknown kind;
- malformed/truncated/invalid UTF-8/extra-field/trailing payload;
- payload and envelope limits, including boundary and boundary+1;
- wrong fragment type/schema, semantic/raw digest, and RepoView identity;
- missing/unbound/corrupt object and immutable conflicting publication;
- path/reparse or foreign pathname substitution;
- duplicate/conflicting durable binding;
- M1b backup/restore and post-restore logical-binding tamper cases;
- unknown/unavailable provider behavior and deterministic provider identity;
- AST/import-negative and side-effect-free import checks.

M2c Understanding, M2d benchmark, lifecycle/task/submission state, outbox/ACK
ledger, retry/effects, promotion, Control Center, installer, and production
activation remain unstarted.

```

## SOURCE docs/m2c-vnext-engineering-intelligence.md
object: 2454a4ec9ff9c7469a656a7c44056f650db76b59
size_bytes: 4620
raw_sha256: sha256:2c59a1000794f47b91974973a5f671296e3503ff2f3d841d3466d3dde535ac2b
```text
# M2c-vNext — Engineering Intelligence Slice

M2c is a build-only, additive semantic layer. It introduces immutable,
rebuildable records bound to an exact M2a `COMMITTED` RepoView and uses M2b
typed content/transport without creating a new mutable authority.

## Record families

- `RepositoryUnderstandingView` — claim projection with explicit `FACT`,
  `INFERENCE`, `ASSUMPTION`, and `HYPOTHESIS` kinds; coverage, must-see
  categories, unknowns, omissions, contradictions, provenance and
  invalidation predicates.
- `ContextPackage` — categorical quality capsule with requested versus covered
  dimensions, explicit epistemic claim classifications, fragment IDs,
  architecture constraints and on-demand affordances. `COMPLETE`, `PARTIAL`
  and `BLOCKED` are mechanically derived; no token count or quality score is
  authoritative.
- `ContextRequest` — immutable request for a visible gap, bound to the exact
  intent, RepoView and source package. It is not a Task, WorkItem, effect,
  retry, capability grant or lifecycle record.
- `ContextResolution` — immutable linkage from request and prior package to a
  resulting package, added accepted fragments, resolved gaps and remaining
  gaps. Denial/unavailability keeps the gap visible.
- `EngineeringDecision` — concise review/resume evidence containing exact
  intent/RepoView/package bases, claim classifications, alternatives,
  trade-offs, selected option, obligations, uncertainty and revisit triggers.

`IntentBasis.task_id` is opaque caller input. M2c consumes
`task_id`/`intent_revision`/`intent_digest` and never allocates Task IDs,
creates Task tables or owns lifecycle/admission.

## Authority and epistemics

Git object data exposed by exact RepoView remains source authority. Understanding
is a projection/claim view, not source bytes authority. Conflicting claims are
preserved as explicit contradictions; when an exact-source `FACT` conflicts
with a derived claim, the record states `EXACT_SOURCE_WINS` without silently
rewriting either claim. Private chain-of-thought, hidden reasoning traces and
token-by-token reasoning are never stored.

Every record identity is derived from stable canonical fields, including exact
intent/RepoView basis and producer/schema versions. Rebuilding the same inputs
converges; changing a material basis or producer/schema identity changes the
record identity and stale decision applicability is rejected.

FACT claims require `RepoSourceEvidence`, a distinct descriptor that binds a
repo-relative source path and committed tree-entry object ID to an accepted M2b
fragment. `publish_repo_source_evidence()` reads the bytes itself through the
exact M2a `CommittedRepoView.query().get_entry()/read_bytes()` boundary, then
publishes the immutable `ContentRef` and accepted fragment. Live validation
proves the complete chain: exact RepoView and source object ID, M2a reread
bytes, `ContentRef`, and accepted fragment raw bytes are all equal. A generic
accepted `SourceEvidenceRef` remains useful for parser/negative cases but can
never promote a claim to `FACT`; parsing a serialized descriptor is not
authority verification. `ContextPackage` and `EngineeringDecision` source
grounding likewise require the exact CommittedRepoView plus live M2b binding
store. `CoverageBinding` records explicitly bind every covered dimension and
must-see target to actual claim IDs and/or accepted fragment IDs, so
`COMPLETE` cannot be self-attested.

`ContextResolution` carries one explicit gap-to-evidence edge per resolved
gap. It enforces `resulting_gap_ids = prior_gap_ids - resolved_gap_ids +
introduced_gap_ids`, keeps unrelated gaps visible, and requires the edge to
name resulting coverage for the resolved gap and the newly accepted evidence.
Denial and unavailability preserve the complete prior gap set. An
`EngineeringDecision` may classify only claim IDs present in the supplied
Understanding basis and requires exact equality of the current RepoView set,
ContextPackage set and opaque IntentBasis when applicability is checked.

## M2b boundary

`publish_semantic_record()` creates canonical JSON bytes, an M2b `ContentRef`,
an exact RepoView-bound `TypedContextFragment`, and a durable accepted binding.
`transport_semantic_record()` exercises Browser encode → Native decode with the
binding store and reconstructs the exact record. Semantic records contain no
protocol/chunk bookkeeping.

No daemon, database, writer, scheduler, API requirement, Browser UI,
production activation or Control Center authority is introduced. Runtime,
writer and activation remain `OFF / OFF / OFF`. M2d remains unstarted.

```

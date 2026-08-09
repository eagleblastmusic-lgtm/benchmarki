"""Read-only, exact Git-backed RepoView primitives for the inactive vNext line.

The module deliberately owns no store, writer, runtime activation or legacy
provider import.  A ``RepositoryResource`` binds one Git object database to an
explicit logical repository identity.  ``CommittedRepoView`` then binds one
observed ref to immutable commit/tree objects; all reads are performed against
that commit and never against a moving ref or the checkout filesystem.
"""

from __future__ import annotations

import datetime as _datetime
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


class RepoViewError(ValueError):
    """Fail-closed error raised by the typed RepoView boundary."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


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

    def run(self, args: Iterable[str], *, operation: str, check: bool = True) -> bytes:
        command = ["git", "-C", str(self.root), *args]
        try:
            completed = subprocess.run(
                command,
                shell=False,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RepoViewError("git_unavailable", "git executable is not available") from exc
        except subprocess.TimeoutExpired as exc:
            raise RepoViewError("git_read_timeout", f"Git read timed out during {operation}") from exc
        if check and completed.returncode != 0:
            raise _parse_git_error(completed, operation=operation)
        return completed.stdout

    def optional(self, args: Iterable[str], *, operation: str) -> bytes | None:
        command = ["git", "-C", str(self.root), *args]
        try:
            completed = subprocess.run(
                command,
                shell=False,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RepoViewError("git_unavailable", "git executable is not available") from exc
        except subprocess.TimeoutExpired as exc:
            raise RepoViewError("git_read_timeout", f"Git read timed out during {operation}") from exc
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

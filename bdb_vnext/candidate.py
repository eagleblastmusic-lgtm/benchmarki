"""Bounded M4b exact local effect and sealed Candidate RepoView.

The source checkout and Git refs
are read-only; all mutable coordination state lives in the N1 Control DB and
immutable replacement bytes use the existing Content CAS.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sqlite3
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, NoReturn

from bdb_shared.evidence import canonical_json_bytes, semantic_digest
from bdb_vnext.content_store import (
    ContentRef,
    DurableBindingStore,
    ImmutableContentStore,
    make_content_ref,
)
from bdb_vnext.control_store import (
    assert_database_path,
    begin_control_write,
    commit_control_write,
    ControlStoreError,
    configure_connection,
    ensure_identity,
    rollback_control_write,
)
from bdb_vnext.repo_view import CommittedRepoView, RepoTreeEntry, RepositoryResource


CANDIDATE_SCHEMA = "bdb-vnext-candidate-v1"
CANDIDATE_PATH_SCHEMA = "bdb-vnext-candidate-path-v1"
CANDIDATE_EFFECT_SCHEMA = "bdb-vnext-candidate-effect-v1"
CANDIDATE_VIEW_SCHEMA_V1 = "bdb-vnext-candidate-repo-view-v1"
CANDIDATE_VIEW_SCHEMA = "bdb-vnext-candidate-repo-view-v2"
CANDIDATE_BASE_AUTHORITY_SCHEMA = "bdb-vnext-candidate-base-git-bundle-v1"
CANDIDATE_ABSENCE_SCHEMA = "bdb-vnext-candidate-absence-v1"
CANDIDATE_KIND = "CANDIDATE"
CANDIDATE_EFFECT_CLASS = "EXACT_REPLACEMENT_V1"
CANDIDATE_PREPARED = "PREPARED"
CANDIDATE_POSSIBLE = "POSSIBLE"
CANDIDATE_APPLIED = "APPLIED"
CANDIDATE_OBSERVED = "OBSERVED"
CANDIDATE_SEALED = "SEALED"
CANDIDATE_DIVERGED = "DIVERGED"
CANDIDATE_UNKNOWN = "UNKNOWN"
CANDIDATE_INVALIDATED = "INVALIDATED"
CandidateState = Literal[
    "PREPARED", "POSSIBLE", "APPLIED", "OBSERVED", "SEALED", "DIVERGED", "UNKNOWN", "INVALIDATED"
]
EffectObservation = Literal["BEFORE", "AFTER", "PARTIAL", "DIVERGED", "UNKNOWN"]
MAX_PATHS = 32
MAX_FILE_BYTES = 8 * 1024 * 1024


class CandidateError(RuntimeError):
    """Typed fail-closed M4b error."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def _fail(code: str, message: str, *, details: Mapping[str, Any] | None = None) -> NoReturn:
    raise CandidateError(code, message, details=details)


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def apply_exact_replacements(
    before: bytes,
    replacements: Sequence[Mapping[str, Any]],
    preimage_digest: object,
) -> bytes:
    """Build an exact postimage from non-overlapping byte replacements.

    The complete preimage is bound before any replacement is considered.  Each
    old byte string must occur exactly once (including overlapping matches),
    and all spans must be disjoint.  This keeps the operation deterministic and
    prevents a later replacement from matching bytes introduced by an earlier
    one.
    """

    if not isinstance(preimage_digest, str) or preimage_digest != _digest(before):
        _fail("replacement_preimage_mismatch", "exact replacement preimage does not match the current file")
    if not isinstance(replacements, Sequence) or isinstance(replacements, (str, bytes)) or not replacements:
        _fail("replacement_invalid", "exact replacement set must be non-empty")
    if len(replacements) > MAX_PATHS:
        _fail("replacement_budget_exceeded", "exact replacement set is too large")

    spans: list[tuple[int, int, bytes]] = []
    total_bytes = 0
    for index, raw in enumerate(replacements):
        if not isinstance(raw, Mapping):
            _fail("replacement_invalid", "exact replacement must be a mapping", details={"index": index})
        old = raw.get("old")
        new = raw.get("new")
        if not isinstance(old, bytes) or not old or not isinstance(new, bytes):
            _fail("replacement_invalid", "exact replacement requires non-empty old and byte new values", details={"index": index})
        if len(old) > MAX_FILE_BYTES or len(new) > MAX_FILE_BYTES:
            _fail("replacement_budget_exceeded", "exact replacement bytes exceed the file budget", details={"index": index})
        positions: list[int] = []
        cursor = 0
        while True:
            match = before.find(old, cursor)
            if match < 0:
                break
            positions.append(match)
            cursor = match + 1
        if len(positions) != 1:
            _fail(
                "replacement_match_count",
                "each exact replacement must match exactly once",
                details={"index": index, "matches": len(positions)},
            )
        start = positions[0]
        spans.append((start, start + len(old), new))
        total_bytes += len(old) + len(new)
        if total_bytes > MAX_FILE_BYTES:
            _fail("replacement_budget_exceeded", "exact replacement bytes exceed the file budget")

    spans.sort(key=lambda item: item[0])
    for previous, current in zip(spans, spans[1:]):
        if current[0] < previous[1]:
            _fail("replacement_overlap", "exact replacement spans overlap")

    result = bytearray()
    cursor = 0
    for start, end, new in spans:
        result.extend(before[cursor:start])
        result.extend(new)
        cursor = end
    result.extend(before[cursor:])
    if len(result) > MAX_FILE_BYTES:
        _fail("replacement_result_too_large", "exact replacement postimage exceeds the file budget")
    return bytes(result)


def _id(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 192 or "\x00" in value:
        _fail("invalid_identifier", f"{field} must be a bounded non-empty identifier")
    return value


def _path(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
        _fail("unsafe_candidate_path", "Candidate path is invalid")
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if normalized.startswith("/") or any(part in {"", ".", ".."} for part in parts) or any(":" in part for part in parts):
        _fail("unsafe_candidate_path", "Candidate paths must be relative and traversal-free")
    if any(part.upper() in reserved or part.endswith((".", " ")) for part in parts):
        _fail("unsafe_candidate_path", "reserved Windows path names are not allowed")
    return normalized


def _contains(root: Path, child: Path) -> bool:
    try:
        return os.path.commonpath((os.path.normcase(os.path.abspath(root)), os.path.normcase(os.path.abspath(child)))) == os.path.normcase(os.path.abspath(root))
    except ValueError:
        return False


def _reparse(path: Path) -> bool:
    info = path.stat(follow_symlinks=False)
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & marker)


def _safe_root(path: Path) -> Path:
    root = path.expanduser().absolute()
    if not root.is_dir() or _reparse(root):
        _fail("workspace_unavailable", "Candidate workspace root must be a regular directory")
    current = Path(root.anchor)
    for part in root.parts[1:]:
        current /= part
        if _reparse(current):
            _fail("workspace_reparse_point", "Candidate workspace path contains a reparse point")
    return root


def _safe_child(root: Path, relative: str) -> Path:
    target = (root / Path(*relative.split("/"))).absolute()
    if target == root or not _contains(root, target):
        _fail("path_escape", "Candidate path escapes its workspace")
    current = root
    for component in relative.split("/")[:-1]:
        current = current / component
        if current.exists() and _reparse(current):
            _fail("workspace_reparse_point", "Candidate path traverses a reparse point")
    return target


def _read_exact(path: Path) -> bytes:
    try:
        before = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or _reparse(path) or before.st_size > MAX_FILE_BYTES:
            _fail("candidate_path_invalid", f"planned path is not a bounded regular file: {path}")
        payload = path.read_bytes()
        after = path.stat(follow_symlinks=False)
    except FileNotFoundError as exc:
        raise CandidateError("candidate_path_missing", f"planned path is missing: {path}") from exc
    except OSError as exc:
        raise CandidateError("candidate_read_failed", f"Candidate path could not be read: {path}") from exc
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        _fail("candidate_path_changed", f"planned path changed during read: {path}")
    return payload


def _file_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)


def _path_exists(path: Path) -> bool:
    """Return existence without following a dangling symlink."""

    return os.path.lexists(str(path))


def _effect_value(value: object) -> object:
    """Convert bounded operation metadata into canonical JSON-safe values."""

    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, Mapping):
        return {str(key): _effect_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_effect_value(item) for item in value]
    return value


def _absence_ref() -> ContentRef:
    return make_content_ref("application/x-bdb-absence", CANDIDATE_ABSENCE_SCHEMA, b"")


def _is_absence_ref(ref: ContentRef) -> bool:
    return ref.type == "application/x-bdb-absence" and ref.schema == CANDIDATE_ABSENCE_SCHEMA and ref.raw_digest == _digest(b"")


def _git_mode(path: Path) -> str:
    return "100755" if (_file_mode(path) & 0o111) else "100644"


def _git_index_entries(workspace: Path) -> dict[str, tuple[str, str]]:
    """Read canonical Git blob identities/modes instead of filesystem bytes.

    On Windows a checked-out ``.cmd`` file can expose an executable bit through
    ``stat`` even though the committed tree records mode ``100644``.  Candidate
    tree equality is a Git-object contract, so tracked identities and modes
    must come from the index when a non-planned tracked file is larger than the
    bounded effect byte budget.
    """

    completed = subprocess.run(
        ["git", "-C", str(workspace), "ls-files", "-s", "-z"],
        shell=False,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if completed.returncode != 0:
        return {}
    try:
        text = completed.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return {}
    entries: dict[str, tuple[str, str]] = {}
    for item in text.split("\x00"):
        if not item:
            continue
        prefix, separator, path = item.partition("\t")
        if not separator:
            continue
        fields = prefix.split(" ")
        mode = fields[0] if fields else ""
        oid = fields[1] if len(fields) > 1 else ""
        if mode in {"100644", "100755", "120000", "160000"} and oid:
            entries[path.replace("\\", "/")] = (oid, mode)
    return entries


def _git_path_is_unchanged(workspace: Path, path: str) -> bool:
    """Verify a large tracked path against the exact worktree index."""

    completed = subprocess.run(
        ["git", "-C", str(workspace), "diff", "--quiet", "--no-ext-diff", "--ignore-submodules", "--", path],
        shell=False,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    _fail("candidate_git_state_failed", "Git could not verify a bounded tracked path")


def _same_absolute_path(first: str | Path, second: str | Path) -> bool:
    """Compare Windows paths without resolving or following reparse points."""

    return os.path.normcase(os.path.abspath(os.fspath(first))) == os.path.normcase(os.path.abspath(os.fspath(second)))


def _git_worktree_state(source: Path, workspace: Path) -> tuple[bool, bool]:
    """Return whether *workspace* is registered, and whether it is locked."""

    completed = subprocess.run(
        ["git", "-C", str(source), "worktree", "list", "--porcelain"],
        shell=False,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if completed.returncode != 0:
        return False, False
    matching = False
    locked = False
    for line in completed.stdout.splitlines():
        if line.startswith("worktree "):
            matching = _same_absolute_path(line[len("worktree "):].strip(), workspace)
            locked = False
        elif matching and line.startswith("locked"):
            locked = True
    return matching, locked


def _blob_oid(raw: bytes, object_format: str) -> str:
    algorithm = "sha256" if object_format == "sha256" else "sha1"
    header = f"blob {len(raw)}\0".encode("ascii")
    return hashlib.new(algorithm, header + raw).hexdigest()


@dataclass(frozen=True)
class CandidatePathPlan:
    path: str
    before_digest: str
    after_digest: str
    before_ref: ContentRef
    after_ref: ContentRef
    before_mode: int
    after_mode: int
    before_size: int
    after_size: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": CANDIDATE_PATH_SCHEMA,
            "path": self.path,
            "before_digest": self.before_digest,
            "after_digest": self.after_digest,
            "before_ref": self.before_ref.as_dict(),
            "after_ref": self.after_ref.as_dict(),
            "before_mode": self.before_mode,
            "after_mode": self.after_mode,
            "before_size": self.before_size,
            "after_size": self.after_size,
        }


@dataclass(frozen=True)
class CandidateRecord:
    candidate_id: str
    effect_id: str
    work_id: str
    task_id: str
    state: CandidateState
    effect_certainty: str
    base_view: Mapping[str, Any]
    workspace_root: str
    workspace_generation: str
    config_digest: str
    lease_id: str
    fence: int
    base_tree_digest: str
    planned_tree_digest: str
    observed_tree_digest: str | None
    planned_paths: tuple[CandidatePathPlan, ...]
    observed_paths: Mapping[str, str]
    candidate_view_id: Mapping[str, Any] | None
    manifest_digest: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": CANDIDATE_SCHEMA,
            "candidate_id": self.candidate_id,
            "effect_id": self.effect_id,
            "work_id": self.work_id,
            "task_id": self.task_id,
            "state": self.state,
            "effect_certainty": self.effect_certainty,
            "base_view": dict(self.base_view),
            "workspace_root": self.workspace_root,
            "workspace_generation": self.workspace_generation,
            "config_digest": self.config_digest,
            "lease_id": self.lease_id,
            "fence": self.fence,
            "base_tree_digest": self.base_tree_digest,
            "planned_tree_digest": self.planned_tree_digest,
            "observed_tree_digest": self.observed_tree_digest,
            "planned_paths": [item.as_dict() for item in self.planned_paths],
            "observed_paths": dict(self.observed_paths),
            "candidate_view_id": self.candidate_view_id,
            "manifest_digest": self.manifest_digest,
        }


@dataclass(frozen=True)
class CandidateRepoView:
    """Immutable, sealed view of the exact planned Candidate tree."""

    schema: str
    kind: str
    candidate_id: str
    effect_id: str
    work_id: str
    task_id: str
    repository_id: str
    base_view_id: str
    base_commit_oid: str
    base_tree_oid: str
    candidate_tree_digest: str
    changed_paths: tuple[str, ...]
    path_bindings: tuple[CandidatePathPlan, ...]
    base_authority: Mapping[str, Any]
    manifest_digest: str
    workspace_generation: str
    config_digest: str
    _store: "CandidateStore" = field(repr=False, compare=False, default=None)  # type: ignore[assignment]
    _base_view: CommittedRepoView | None = field(repr=False, compare=False, default=None)

    def __post_init__(self) -> None:
        if self.schema not in {CANDIDATE_VIEW_SCHEMA_V1, CANDIDATE_VIEW_SCHEMA} or self.kind != CANDIDATE_KIND or self._store is None:
            _fail("candidate_view_invalid", "unsupported Candidate RepoView")
        if self.schema == CANDIDATE_VIEW_SCHEMA and not self.base_authority:
            _fail("candidate_base_authority_missing", "v2 Candidate RepoView requires archived base authority")
        if semantic_digest(self._identity_payload()) != self.manifest_digest:
            _fail("candidate_view_integrity_failure", "Candidate view identity digest differs")

    def _identity_payload(self) -> dict[str, Any]:
        identity = {
            "schema": self.schema,
            "kind": self.kind,
            "candidate_id": self.candidate_id,
            "effect_id": self.effect_id,
            "work_id": self.work_id,
            "task_id": self.task_id,
            "repository_id": self.repository_id,
            "base_view_id": self.base_view_id,
            "base_commit_oid": self.base_commit_oid,
            "base_tree_oid": self.base_tree_oid,
            "candidate_tree_digest": self.candidate_tree_digest,
            "changed_paths": list(self.changed_paths),
            "path_bindings": [item.as_dict() for item in self.path_bindings],
            "workspace_generation": self.workspace_generation,
            "config_digest": self.config_digest,
        }
        if self.schema == CANDIDATE_VIEW_SCHEMA:
            identity["base_authority"] = dict(self.base_authority)
        return identity

    @property
    def view_id(self) -> str:
        return self.manifest_digest

    def to_dict(self) -> dict[str, Any]:
        return {**self._identity_payload(), "view_id": self.view_id, "manifest_digest": self.manifest_digest}

    def read_bytes(self, path: str) -> bytes:
        path = _path(path)
        try:
            self._store.invalidate_if_changed(self.candidate_id, base_view=self._base_view)
        except CandidateError as exc:
            if exc.code not in {"stale_fence", "work_kernel_unavailable"}:
                raise
        self._store.verify_sealed(self.candidate_id, base_view=self._base_view)
        record = self._store.get(self.candidate_id)
        if record is None or record.state != CANDIDATE_SEALED or record.manifest_digest != self.manifest_digest:
            _fail("candidate_not_sealed", "Candidate view is no longer sealed")
        plan = next((item for item in record.planned_paths if item.path == path), None)
        if plan is not None:
            if _is_absence_ref(plan.after_ref):
                _fail("candidate_missing_path", "Candidate RepoView does not contain the planned deleted path")
            raw = self._store.content_store.resolve(plan.after_ref)
            if _digest(raw) != plan.after_digest:
                _fail("candidate_content_integrity_failure", "sealed Candidate content digest differs")
            return raw
        if self._base_view is None:
            _fail("candidate_base_unavailable", "unchanged Candidate bytes require the bound base view")
        return self._base_view.read_bytes(path)

    def list_entries(self) -> tuple[RepoTreeEntry, ...]:
        self._store.verify_sealed(self.candidate_id, base_view=self._base_view)
        if self._base_view is None:
            _fail("candidate_base_unavailable", "Candidate entries require the bound base view")
        entries = {entry.path: entry for entry in self._base_view.list_entries()}
        for plan in self.path_bindings:
            if _is_absence_ref(plan.after_ref):
                entries.pop(plan.path, None)
                continue
            raw = self._store.content_store.resolve(plan.after_ref)
            entry = entries.get(plan.path)
            mode = plan.after_mode
            entries[plan.path] = RepoTreeEntry(
                path=plan.path,
                mode="100755" if mode & 0o111 else "100644",
                object_type="blob",
                object_oid=_blob_oid(raw, self._base_view.object_format),
                size_bytes=len(raw),
                file_kind="regular",
            )
        return tuple(entries[path] for path in sorted(entries))

    def entry(self, path: str) -> RepoTreeEntry:
        normalized = _path(path)
        for entry in self.list_entries():
            if entry.path == normalized:
                return entry
        _fail("candidate_missing_path", f"Candidate RepoView does not contain path: {normalized}")


class CandidateStore:
    """One typed M4b repository over the unified N1 Control DB."""

    def __init__(
        self,
        root: str | Path,
        *,
        content_store: ImmutableContentStore | None = None,
        work_kernel: Any | None = None,
        generation: str = "bdb-vnext-g1",
    ) -> None:
        self.root = _safe_root(Path(root))
        self.control_root = self.root / "control"
        self.control_root.mkdir(parents=True, exist_ok=True)
        self.database_path = assert_database_path(self.root, self.control_root / "control.db")
        self.content_store = content_store or ImmutableContentStore(self.root)
        self.bindings = DurableBindingStore(self.root, content_store=self.content_store)
        self.workspace_root = self.root / "candidates"
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        _safe_root(self.workspace_root)
        self.generation = _id(generation, field="workspace_generation")
        self.work_kernel = work_kernel
        self._verified_base_archives: set[str] = set()
        self._connection = self.bindings._connection
        configure_connection(self._connection)
        ensure_identity(self._connection)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS m4b_candidate_effects (
                candidate_id TEXT PRIMARY KEY,
                effect_id TEXT NOT NULL UNIQUE,
                work_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                state TEXT NOT NULL,
                effect_certainty TEXT NOT NULL,
                base_view_json BLOB NOT NULL,
                workspace_root TEXT NOT NULL,
            workspace_generation TEXT NOT NULL,
                config_digest TEXT NOT NULL DEFAULT '',
                lease_id TEXT NOT NULL DEFAULT '',
                fence INTEGER NOT NULL DEFAULT 0,
                base_tree_digest TEXT NOT NULL DEFAULT '',
                planned_tree_digest TEXT NOT NULL DEFAULT '',
                observed_tree_digest TEXT,
                observed_json BLOB NOT NULL,
                candidate_view_json BLOB,
                manifest_digest TEXT,
                UNIQUE(work_id, effect_id)
            );
            CREATE TABLE IF NOT EXISTS m4b_candidate_paths (
                candidate_id TEXT NOT NULL REFERENCES m4b_candidate_effects(candidate_id),
                path TEXT NOT NULL,
                before_digest TEXT NOT NULL,
                after_digest TEXT NOT NULL,
                before_ref_json BLOB NOT NULL,
                after_ref_json BLOB NOT NULL,
                before_mode INTEGER NOT NULL,
                after_mode INTEGER NOT NULL,
                before_size INTEGER NOT NULL,
                after_size INTEGER NOT NULL,
                observed TEXT,
                PRIMARY KEY(candidate_id, path)
            );
            CREATE INDEX IF NOT EXISTS m4b_candidate_paths_by_candidate ON m4b_candidate_paths(candidate_id);
            """
        )
        commit_control_write(self._connection)
        columns = {str(row[1]) for row in self._connection.execute("PRAGMA table_info(m4b_candidate_effects)").fetchall()}
        if "config_digest" not in columns:
            self._connection.execute("ALTER TABLE m4b_candidate_effects ADD COLUMN config_digest TEXT NOT NULL DEFAULT ''")
            commit_control_write(self._connection)

    def _write(self, operation: Any) -> Any:
        """Run one Candidate state mutation through the shared DB floor."""

        try:
            begin_control_write(self._connection)
        except ControlStoreError as exc:
            _fail(exc.code, str(exc))
        try:
            result = operation()
            commit_control_write(self._connection)
            return result
        except ControlStoreError as exc:
            rollback_control_write(self._connection)
            _fail(exc.code, str(exc))
        except CandidateError:
            rollback_control_write(self._connection)
            raise
        except sqlite3.IntegrityError as exc:
            rollback_control_write(self._connection)
            _fail("candidate_storage_conflict", "Candidate state conflicted with canonical Control DB state")
        except sqlite3.DatabaseError as exc:
            rollback_control_write(self._connection)
            _fail("candidate_storage_failed", "Candidate state could not be committed")

    @property
    def config_digest(self) -> str:
        return _digest((self.root / "config" / "bdb-vnext.json").read_bytes())

    def _workspace_for(self, candidate_id: str) -> Path:
        candidate_id = _id(candidate_id, field="candidate_id")
        directory = "candidate-" + hashlib.sha256(candidate_id.encode("utf-8")).hexdigest()[:40]
        return _safe_child(self.workspace_root, directory)

    @staticmethod
    def _serialize_view(view: CommittedRepoView) -> dict[str, Any]:
        view.validate_integrity()
        return view.to_dict()

    def _bound_base_view(self, record: CandidateRecord) -> CommittedRepoView:
        """Reconstruct the exact committed authority persisted by preparation.

        Readers must not need a caller-supplied moving ref in order to verify a
        retained sealed workspace.  The durable Candidate record already binds
        the repository identity and exact commit/tree; reopen that identity and
        require the resolved committed object to equal the persisted view.
        """

        base = record.base_view
        repository = base.get("repository")
        if not isinstance(repository, Mapping):
            _fail("candidate_base_corrupt", "Candidate base repository binding is missing")
        repository_id = repository.get("repository_id")
        identity_digest = repository.get("identity_digest")
        if not isinstance(repository_id, str) or not isinstance(identity_digest, str):
            _fail("candidate_base_corrupt", "Candidate base repository identity is incomplete")
        # The serialized RepoView deliberately excludes a mutable filesystem
        # locator.  A retained Git-native Candidate worktree is nevertheless
        # bound to the same common object database, so it is the safe locator
        # from which to reopen the persisted exact commit.
        workspace = Path(record.workspace_root)
        if not workspace.exists():
            _fail("candidate_base_unavailable", "retained Candidate workspace is unavailable for current applicability proof")
        try:
            resource = RepositoryResource.from_path(workspace, repository_id=repository_id)
            if resource.identity_digest != identity_digest:
                _fail("candidate_base_mismatch", "Candidate workspace is bound to a different repository authority")
            view = resource.resolve_committed(str(base.get("commit_oid", "")))
        except CandidateError:
            raise
        except Exception as exc:
            raise CandidateError("candidate_base_unavailable", "Candidate committed base cannot be reopened") from exc
        if (
            view.view_id != base.get("view_id")
            or view.commit_oid != base.get("commit_oid")
            or view.tree_oid != base.get("tree_oid")
            or view.repository_id != repository_id
            or view.repository_identity_digest != identity_digest
        ):
            _fail("candidate_base_mismatch", "reopened Candidate base differs from the prepared exact RepoView")
        return view

    def _archive_base_authority(self, workspace: Path, view: CommittedRepoView) -> dict[str, Any]:
        """Publish a self-contained exact Git object authority into Content CAS."""

        with tempfile.TemporaryDirectory(prefix="bdb-m4b-base-") as directory:
            bundle = Path(directory) / "base.bundle"
            completed = subprocess.run(
                ["git", "-C", str(workspace), "bundle", "create", str(bundle), "HEAD"],
                shell=False,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if completed.returncode != 0 or not bundle.is_file():
                raise CandidateError(
                    "candidate_base_archive_failed",
                    "exact Candidate base Git objects could not be archived",
                    details={"returncode": completed.returncode},
                )
            raw = bundle.read_bytes()
        try:
            ref = make_content_ref("application/x-git-bundle", CANDIDATE_BASE_AUTHORITY_SCHEMA, raw)
            self.content_store.publish(ref, raw)
        except Exception as exc:
            raise CandidateError("candidate_base_archive_failed", "exact Candidate base archive could not enter Content CAS") from exc
        return {
            "schema": CANDIDATE_BASE_AUTHORITY_SCHEMA,
            "kind": "GIT_BUNDLE_CAS_V1",
            "content_ref": ref.as_dict(),
            "repository_id": view.repository_id,
            "repository_identity_digest": view.repository_identity_digest,
            "object_format": view.object_format,
            "commit_oid": view.commit_oid,
            "tree_oid": view.tree_oid,
        }

    def _verify_base_authority(self, record: CandidateRecord) -> Mapping[str, Any]:
        document = record.candidate_view_id
        if not isinstance(document, Mapping) or document.get("schema") != CANDIDATE_VIEW_SCHEMA:
            _fail("candidate_base_authority_missing", "sealed Candidate lacks restorable exact Git authority")
        authority = document.get("base_authority")
        if not isinstance(authority, Mapping) or authority.get("schema") != CANDIDATE_BASE_AUTHORITY_SCHEMA or authority.get("kind") != "GIT_BUNDLE_CAS_V1":
            _fail("candidate_base_authority_missing", "sealed Candidate base authority is incomplete")
        base = record.base_view
        repository = base.get("repository")
        expected = {
            "repository_id": repository.get("repository_id") if isinstance(repository, Mapping) else None,
            "repository_identity_digest": repository.get("identity_digest") if isinstance(repository, Mapping) else None,
            "object_format": repository.get("object_format") if isinstance(repository, Mapping) else None,
            "commit_oid": base.get("commit_oid"),
            "tree_oid": base.get("tree_oid"),
        }
        if any(authority.get(key) != value for key, value in expected.items()):
            _fail("candidate_base_mismatch", "Candidate base archive binding differs from the prepared RepoView")
        try:
            ref = ContentRef.from_mapping(authority.get("content_ref"))
            raw = self.content_store.resolve(ref)
        except Exception as exc:
            raise CandidateError("candidate_base_unavailable", "Candidate base Git authority is unavailable from Content CAS") from exc
        if ref.raw_digest in self._verified_base_archives:
            return authority
        with tempfile.TemporaryDirectory(prefix="bdb-m4b-verify-") as directory:
            root = Path(directory)
            bundle = root / "base.bundle"
            repository_root = root / "repository.git"
            bundle.write_bytes(raw)
            cloned = subprocess.run(
                ["git", "clone", "--bare", str(bundle), str(repository_root)],
                shell=False,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if cloned.returncode != 0:
                _fail("candidate_base_unavailable", "Candidate base Git bundle failed exact object verification")
            commit = subprocess.run(
                ["git", "-C", str(repository_root), "rev-parse", f"{expected['commit_oid']}^{{commit}}"],
                shell=False, capture_output=True, text=True, timeout=10, check=False,
            )
            tree = subprocess.run(
                ["git", "-C", str(repository_root), "rev-parse", f"{expected['commit_oid']}^{{tree}}"],
                shell=False, capture_output=True, text=True, timeout=10, check=False,
            )
            object_format = subprocess.run(
                ["git", "-C", str(repository_root), "rev-parse", "--show-object-format"],
                shell=False, capture_output=True, text=True, timeout=10, check=False,
            )
            if (
                commit.returncode != 0
                or commit.stdout.strip() != expected["commit_oid"]
                or tree.returncode != 0
                or tree.stdout.strip() != expected["tree_oid"]
                or object_format.returncode != 0
                or object_format.stdout.strip() != expected["object_format"]
            ):
                _fail("candidate_base_mismatch", "Candidate base Git bundle contains different exact objects")
        self._verified_base_archives.add(ref.raw_digest)
        return authority

    def _verify_view_document(self, record: CandidateRecord) -> Mapping[str, Any]:
        document = record.candidate_view_id
        if not isinstance(document, Mapping) or record.manifest_digest is None:
            _fail("candidate_view_integrity_failure", "sealed Candidate manifest is missing")
        identity = dict(document)
        view_id = identity.pop("view_id", None)
        manifest_digest = identity.pop("manifest_digest", None)
        if view_id != record.manifest_digest or manifest_digest != record.manifest_digest or semantic_digest(identity) != record.manifest_digest:
            _fail("candidate_view_integrity_failure", "sealed Candidate manifest digest differs")
        return document

    def verify_current_applicability(self, candidate_id: str, *, connection: sqlite3.Connection | None = None) -> CandidateRecord:
        """Positively prove the current sealed Candidate before consumption.

        This is the canonical read-side applicability boundary used by
        Evidence, Publication and recovery. Missing or later-edited retained
        workspaces do not redefine a v2 sealed Candidate: its manifest, exact
        Git bundle and content CAS are the post-seal authority. Explicit
        workspace observation may invalidate that subject, but consumers never
        derive applicability from mutable filesystem bytes on their own.
        """

        record = self.get(candidate_id, connection=connection)
        if record is None:
            _fail("candidate_missing", "Candidate does not exist")
        if record.state != CANDIDATE_SEALED:
            _fail("candidate_not_sealed", "Candidate has no sealed immutable view")
        document = self._verify_view_document(record)
        if document.get("schema") == CANDIDATE_VIEW_SCHEMA:
            self._verify_base_authority(record)
            for plan in record.planned_paths:
                try:
                    before = self.content_store.resolve(plan.before_ref)
                    after = self.content_store.resolve(plan.after_ref)
                except Exception as exc:
                    raise CandidateError("candidate_content_unavailable", "sealed Candidate CAS content is unavailable") from exc
                if _digest(before) != plan.before_digest or _digest(after) != plan.after_digest:
                    _fail("candidate_content_integrity_failure", "sealed Candidate content digest differs")
            return record
        if document.get("schema") == CANDIDATE_VIEW_SCHEMA_V1:
            base_view = self._bound_base_view(record)
            current = self.invalidate_if_changed(record.candidate_id, base_view=base_view)
            if current.state != CANDIDATE_SEALED:
                _fail("candidate_invalidated", "Candidate no longer matches its immutable sealed view")
            return self.verify_sealed(record.candidate_id, base_view=base_view)
        _fail("candidate_view_integrity_failure", "sealed Candidate view schema is unsupported")

    def retention_inventory(self) -> tuple[dict[str, Any], ...]:
        """Classify retained Candidate workspaces without deleting evidence."""

        rows = self._connection.execute(
            "SELECT candidate_id,state,workspace_root,manifest_digest FROM m4b_candidate_effects ORDER BY candidate_id"
        ).fetchall()
        inventory: list[dict[str, Any]] = []
        for candidate_id, state, workspace_root, manifest_digest in rows:
            references: list[str] = []
            for table, column in (("m4c_evidence_records", "candidate_view_id"), ("n4_publications", "candidate_id")):
                try:
                    count = int(self._connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} IN (?,?)", (candidate_id, manifest_digest)).fetchone()[0])
                except sqlite3.DatabaseError:
                    count = 0
                if count:
                    references.append(table)
            current = self.get(str(candidate_id)) if manifest_digest else None
            archive_backed = bool(
                current
                and isinstance(current.candidate_view_id, Mapping)
                and current.candidate_view_id.get("schema") == CANDIDATE_VIEW_SCHEMA
                and isinstance(current.candidate_view_id.get("base_authority"), Mapping)
            )
            if references and archive_backed:
                classification = "canonically_referenced_archive_backed"
            elif references:
                classification = "canonically_referenced"
            elif state in {CANDIDATE_PREPARED, CANDIDATE_POSSIBLE, CANDIDATE_APPLIED, CANDIDATE_UNKNOWN, CANDIDATE_DIVERGED}:
                classification = "recovery_required"
            elif state == CANDIDATE_INVALIDATED:
                classification = "disposable_candidate"
            else:
                classification = "historical_evidence"
            inventory.append({"candidate_id": str(candidate_id), "state": str(state), "workspace_root": str(workspace_root), "manifest_digest": manifest_digest, "classification": classification, "workspace_recovery_required": not archive_backed, "referenced_by": references})
        return tuple(inventory)

    def _base_entries(self, view: CommittedRepoView) -> dict[str, tuple[str, str]]:
        entries = view.list_entries()
        result: dict[str, tuple[str, str]] = {}
        folded: dict[str, str] = {}
        for entry in entries:
            if not entry.is_regular_file:
                _fail("unsupported_base_tree", f"Candidate exact replacement requires regular base files: {entry.path}")
            key = entry.path.casefold()
            if key in folded and folded[key] != entry.path:
                _fail("case_collision", "Committed base tree contains Windows case-colliding paths")
            folded[key] = entry.path
            result[entry.path] = (entry.object_oid, entry.mode)
        return result

    def _workspace_entries(self, workspace: Path, *, object_format: str) -> dict[str, tuple[str, str]]:
        result: dict[str, tuple[str, str]] = {}
        index_entries = _git_index_entries(workspace)
        index_modes = {path: mode for path, (_oid, mode) in index_entries.items()}
        for directory, dir_names, file_names in os.walk(workspace, topdown=True, followlinks=False):
            current = Path(directory)
            if current == workspace:
                dir_names[:] = [name for name in dir_names if name != ".git"]
            elif ".git" in dir_names:
                _fail("foreign_candidate_state", "nested .git metadata is not part of the Candidate tree")
            for name in dir_names:
                child = current / name
                if _reparse(child):
                    _fail("workspace_reparse_point", "Candidate workspace contains a reparse point")
            for name in file_names:
                if name == ".git":
                    if current != workspace:
                        _fail("foreign_candidate_state", "nested .git metadata is not part of the Candidate tree")
                    continue
                child = current / name
                if _reparse(child):
                    _fail("workspace_reparse_point", "Candidate workspace contains a reparse point")
                relative = child.relative_to(workspace).as_posix()
                normalized = _path(relative)
                indexed = index_entries.get(normalized)
                if indexed is not None and child.stat(follow_symlinks=False).st_size > MAX_FILE_BYTES:
                    if not _git_path_is_unchanged(workspace, normalized):
                        _fail("candidate_path_invalid", "large tracked path changed outside the bounded effect plan")
                    result[normalized] = indexed
                    continue
                raw = _read_exact(child)
                result[normalized] = (_blob_oid(raw, object_format), index_modes.get(normalized, _git_mode(child)))
        return dict(sorted(result.items()))

    @staticmethod
    def _tree_digest(entries: Mapping[str, tuple[str, str]]) -> str:
        return semantic_digest({"schema": CANDIDATE_VIEW_SCHEMA, "entries": [[path, oid, mode] for path, (oid, mode) in sorted(entries.items())]})

    def _assert_owner(self, record: CandidateRecord) -> None:
        if self.work_kernel is None:
            _fail("work_kernel_unavailable", "Candidate effects require the canonical Work Kernel")
        try:
            self.work_kernel.assert_current_lease(record.work_id, record.lease_id, record.fence)
        except Exception as exc:
            code = getattr(exc, "code", "stale_fence")
            raise CandidateError("stale_fence", "Candidate owner no longer holds the current lease/fence", details={"cause": code}) from exc

    @staticmethod
    def _is_noop_plan(plan: CandidatePathPlan) -> bool:
        """Identify an already-applied path retained in an accumulated plan."""

        return (
            plan.before_digest == plan.after_digest
            and plan.before_mode == plan.after_mode
            and plan.before_size == plan.after_size
        )

    def _plan_from_row(self, row: tuple[Any, ...]) -> CandidatePathPlan:
        return CandidatePathPlan(
            str(row[1]), str(row[2]), str(row[3]),
            ContentRef.from_mapping(json.loads(bytes(row[4]).decode("utf-8"))),
            ContentRef.from_mapping(json.loads(bytes(row[5]).decode("utf-8"))),
            int(row[6]), int(row[7]), int(row[8]), int(row[9]),
        )

    def _record_row(self, row: tuple[Any, ...], *, connection: sqlite3.Connection | None = None) -> CandidateRecord:
        source = connection or self._connection
        paths = tuple(self._plan_from_row(item) for item in source.execute(
            "SELECT candidate_id,path,before_digest,after_digest,before_ref_json,after_ref_json,before_mode,after_mode,before_size,after_size FROM m4b_candidate_paths WHERE candidate_id=? ORDER BY path",
            (row[0],),
        ).fetchall())
        view = json.loads(bytes(row[16]).decode("utf-8")) if row[16] else None
        return CandidateRecord(
            str(row[0]), str(row[1]), str(row[2]), str(row[3]), str(row[4]), str(row[5]),
            json.loads(bytes(row[6]).decode("utf-8")), str(row[7]), str(row[8]), str(row[9]), str(row[10]), int(row[11]),
            str(row[12]), str(row[13]), str(row[14]) if row[14] else None,
            paths, json.loads(bytes(row[15]).decode("utf-8")), view, str(row[17]) if row[17] else None,
        )

    def get(self, candidate_id: str, *, connection: sqlite3.Connection | None = None) -> CandidateRecord | None:
        source = connection or self._connection
        row = source.execute(
            "SELECT candidate_id,effect_id,work_id,task_id,state,effect_certainty,base_view_json,workspace_root,workspace_generation,config_digest,lease_id,fence,base_tree_digest,planned_tree_digest,observed_tree_digest,observed_json,candidate_view_json,manifest_digest FROM m4b_candidate_effects WHERE candidate_id=?",
            (_id(candidate_id, field="candidate_id"),),
        ).fetchone()
        return self._record_row(row, connection=source) if row else None

    def _workspace_matches_base(self, workspace: Path, base_view: CommittedRepoView, source: Path) -> bool:
        """Allow reuse only for an unrecorded, clean exact-base worktree."""

        if not _path_exists(workspace) or not workspace.is_dir() or _reparse(workspace):
            return False
        registered, locked = _git_worktree_state(source, workspace)
        if not registered or locked:
            return False
        observed = subprocess.run(["git", "-C", str(workspace), "rev-parse", "HEAD"], shell=False, capture_output=True, text=True, timeout=10, check=False)
        tree = subprocess.run(["git", "-C", str(workspace), "rev-parse", "HEAD^{tree}"], shell=False, capture_output=True, text=True, timeout=10, check=False)
        if observed.returncode != 0 or observed.stdout.strip() != base_view.commit_oid or tree.returncode != 0 or tree.stdout.strip() != base_view.tree_oid:
            return False
        status = subprocess.run(["git", "-C", str(workspace), "status", "--porcelain=v1", "--untracked-files=all"], shell=False, capture_output=True, text=True, timeout=30, check=False)
        if status.returncode != 0 or status.stdout:
            return False
        try:
            return self._workspace_entries(workspace, object_format=base_view.object_format) == self._base_entries(base_view)
        except CandidateError:
            return False

    def _next_orphan_path(self, workspace: Path) -> Path:
        for index in range(1, 1001):
            name = f"{workspace.name}.orphan-{index}"
            candidate = _safe_child(self.workspace_root, name)
            if not _path_exists(candidate):
                return candidate
        _fail("workspace_quarantine_exhausted", "no bounded orphan quarantine path is available")

    def _quarantine_workspace(self, source: Path, workspace: Path) -> Path:
        """Move an unowned workspace aside without deleting its bytes."""

        if _reparse(workspace):
            _fail("workspace_quarantine_unsafe", "orphan Candidate workspace is a reparse point")
        registered, locked = _git_worktree_state(source, workspace)
        if locked:
            _fail("workspace_locked", "orphan Candidate workspace is Git-locked")
        quarantine = self._next_orphan_path(workspace)
        if registered:
            completed = subprocess.run(["git", "-C", str(source), "worktree", "move", str(workspace), str(quarantine)], shell=False, capture_output=True, text=True, timeout=30, check=False)
        else:
            try:
                os.replace(str(workspace), str(quarantine))
                completed = None
            except OSError as exc:
                _fail("workspace_quarantine_failed", "unregistered orphan Candidate workspace could not be preserved", details={"error": str(exc)})
        if completed is not None and completed.returncode != 0:
            _fail("workspace_quarantine_failed", "orphan Candidate worktree could not be quarantined", details={"stderr": completed.stderr[-1000:]})
        if _path_exists(workspace) or not _path_exists(quarantine):
            _fail("workspace_quarantine_failed", "orphan Candidate workspace quarantine was incomplete")
        return quarantine

    def create_workspace(self, *, candidate_id: str, base_view: CommittedRepoView) -> Path:
        candidate_id = _id(candidate_id, field="candidate_id")
        base_view.validate_integrity()
        source = Path(base_view.repository.root).absolute()
        if _contains(self.root, source) or _contains(source, self.root):
            _fail("workspace_source_overlap", "Candidate workspace must not overlap the source repository")
        if self.get(candidate_id) is not None:
            _fail("workspace_exists", "canonical Candidate workspace already exists", details={"candidate_id": candidate_id})
        workspace = self._workspace_for(candidate_id)
        if _path_exists(workspace):
            if self._workspace_matches_base(workspace, base_view, source):
                return workspace
            self._quarantine_workspace(source, workspace)
        workspace.parent.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            [
                "git",
                "-c", "core.autocrlf=false",
                "-c", "core.longpaths=true",
                "-C", str(source), "worktree", "add", "--detach", str(workspace), base_view.commit_oid,
            ],
            shell=False, capture_output=True, text=True, timeout=30, check=False,
        )
        if completed.returncode != 0:
            raise CandidateError("workspace_create_failed", "isolated Git Candidate worktree could not be created", details={"stderr": completed.stderr[-1000:]})
        try:
            observed = subprocess.run(["git", "-C", str(workspace), "rev-parse", "HEAD"], shell=False, capture_output=True, text=True, timeout=10, check=False)
            tree = subprocess.run(["git", "-C", str(workspace), "rev-parse", "HEAD^{tree}"], shell=False, capture_output=True, text=True, timeout=10, check=False)
            if observed.returncode != 0 or observed.stdout.strip() != base_view.commit_oid or tree.returncode != 0 or tree.stdout.strip() != base_view.tree_oid:
                _fail("workspace_base_mismatch", "Candidate worktree HEAD differs from the exact Committed RepoView")
            _safe_root(workspace)
            return workspace
        except Exception:
            subprocess.run(["git", "-C", str(source), "worktree", "remove", "--force", str(workspace)], shell=False, capture_output=True, text=True, timeout=30, check=False)
            raise

    def prepare(
        self,
        *,
        candidate_id: str,
        effect_id: str | None = None,
        work_id: str,
        task_id: str,
        lease_id: str,
        fence: int,
        base_view: CommittedRepoView,
        workspace_root: str | Path,
        replacements: Mapping[str, bytes],
    ) -> CandidateRecord:
        candidate_id, work_id, task_id, lease_id = (_id(candidate_id, field="candidate_id"), _id(work_id, field="work_id"), _id(task_id, field="task_id"), _id(lease_id, field="lease_id"))
        if not isinstance(fence, int) or isinstance(fence, bool) or fence < 1:
            _fail("invalid_fence", "fence must be a positive integer")
        if not isinstance(replacements, Mapping) or not replacements or len(replacements) > MAX_PATHS:
            _fail("invalid_write_set", "exact replacement requires a bounded non-empty write set")
        base = self._serialize_view(base_view)
        base_entries = self._base_entries(base_view)
        workspace = _safe_root(Path(workspace_root))
        if workspace != self._workspace_for(candidate_id):
            _fail("foreign_workspace", "Candidate workspace must be the generated workspace for this candidate")
        if self.work_kernel is None:
            _fail("work_kernel_unavailable", "Candidate effects require the canonical Work Kernel")
        query = self.work_kernel.query(work_id)
        if query is None or query.work.task_id != task_id:
            _fail("task_binding_mismatch", "Candidate work item is not bound to the supplied canonical Task")
        self.work_kernel.assert_current_lease(work_id, lease_id, fence)
        object_format = str(base_view.object_format)
        actual_base = self._workspace_entries(workspace, object_format=object_format)
        base_tree_digest = self._tree_digest(base_entries)
        if actual_base != base_entries:
            _fail("candidate_base_mismatch", "Candidate workspace does not exactly equal the committed base tree")
        plans: list[CandidatePathPlan] = []
        seen_case_paths: set[str] = set()
        base_case_paths = {item.casefold(): item for item in base_entries}
        planned_entries = dict(base_entries)
        for raw_path, after in sorted(replacements.items()):
            path = _path(raw_path)
            folded_path = path.casefold()
            if folded_path in seen_case_paths:
                _fail("case_collision", "replacement paths collide on a case-insensitive filesystem")
            seen_case_paths.add(folded_path)
            if folded_path in base_case_paths and base_case_paths[folded_path] != path:
                _fail("case_collision", "replacement path differs from its committed path only by case")
            if path not in base_entries:
                _fail("path_not_in_base", "exact replacement may only replace an existing committed regular file")
            if not isinstance(after, bytes) or len(after) > MAX_FILE_BYTES:
                _fail("invalid_after_bytes", "planned replacement bytes are invalid or too large")
            target = _safe_child(workspace, path)
            before = _read_exact(target)
            before_mode = _file_mode(target)
            after_ref = make_content_ref("application/octet-stream", "m4b-candidate-after-v1", after)
            before_ref = make_content_ref("application/octet-stream", "m4b-candidate-before-v1", before)
            self.content_store.publish(before_ref, before)
            self.content_store.publish(after_ref, after)
            plan = CandidatePathPlan(path, _digest(before), _digest(after), before_ref, after_ref, before_mode, before_mode, len(before), len(after))
            plans.append(plan)
            planned_entries[path] = (_blob_oid(after, object_format), base_entries[path][1])
        planned_tree_digest = self._tree_digest(planned_entries)
        effect_material = {
            "schema": CANDIDATE_EFFECT_SCHEMA,
            "class": CANDIDATE_EFFECT_CLASS,
            "candidate_id": candidate_id,
            "work_id": work_id,
            "task_id": task_id,
            "lease_id": lease_id,
            "fence": fence,
            "base_view": base,
            "base_tree_digest": base_tree_digest,
            "planned_tree_digest": planned_tree_digest,
            "workspace_root": str(workspace),
            "workspace_generation": self.generation,
            "config_digest": self.config_digest,
            "paths": [item.as_dict() for item in plans],
        }
        expected_effect_id = semantic_digest(effect_material)
        if effect_id is not None and effect_id != expected_effect_id:
            _fail("effect_identity_mismatch", "effect_id does not equal the exact prepared effect digest", details={"expected": expected_effect_id})
        effect_id = expected_effect_id
        if self.get(candidate_id) is not None:
            _fail("candidate_exists", "candidate_id is already bound")
        begin_control_write(self._connection)
        try:
            self._connection.execute(
                "INSERT INTO m4b_candidate_effects(candidate_id,effect_id,work_id,task_id,state,effect_certainty,base_view_json,workspace_root,workspace_generation,config_digest,lease_id,fence,base_tree_digest,planned_tree_digest,observed_tree_digest,observed_json,candidate_view_json,manifest_digest) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (candidate_id, effect_id, work_id, task_id, CANDIDATE_PREPARED, "NOT_ASSESSED", canonical_json_bytes(base), str(workspace), self.generation, self.config_digest, lease_id, fence, base_tree_digest, planned_tree_digest, None, b"{}", None, None),
            )
            for item in plans:
                self._connection.execute(
                    "INSERT INTO m4b_candidate_paths(candidate_id,path,before_digest,after_digest,before_ref_json,after_ref_json,before_mode,after_mode,before_size,after_size,observed) VALUES (?,?,?,?,?,?,?,?,?,?,NULL)",
                    (candidate_id, item.path, item.before_digest, item.after_digest, canonical_json_bytes(item.before_ref.as_dict()), canonical_json_bytes(item.after_ref.as_dict()), item.before_mode, item.after_mode, item.before_size, item.after_size),
                )
            commit_control_write(self._connection)
        except Exception:
            self._connection.rollback()
            raise
        return self.get(candidate_id)  # type: ignore[return-value]

    def prepare_operations(
        self,
        *,
        candidate_id: str,
        effect_id: str | None = None,
        work_id: str,
        task_id: str,
        lease_id: str,
        fence: int,
        base_view: CommittedRepoView,
        workspace_root: str | Path,
        operations: Sequence[Mapping[str, Any]],
    ) -> CandidateRecord:
        """Prepare the bounded ``BDB_EDIT_V1`` operation set.

        This keeps the existing Candidate writer and exact tree proof.  A
        rename is represented as one source deletion plus one destination
        creation; the edit artifact retains the higher-level operation in its
        own immutable fact record.
        """

        candidate_id, work_id, task_id, lease_id = (
            _id(candidate_id, field="candidate_id"),
            _id(work_id, field="work_id"),
            _id(task_id, field="task_id"),
            _id(lease_id, field="lease_id"),
        )
        if not isinstance(fence, int) or isinstance(fence, bool) or fence < 1:
            _fail("invalid_fence", "fence must be a positive integer")
        if not isinstance(operations, Sequence) or isinstance(operations, (str, bytes)) or not operations or len(operations) > MAX_PATHS:
            _fail("invalid_write_set", "edit operation set must be bounded and non-empty")
        base = self._serialize_view(base_view)
        base_entries = self._base_entries(base_view)
        workspace = _safe_root(Path(workspace_root))
        if workspace != self._workspace_for(candidate_id):
            _fail("foreign_workspace", "Candidate workspace must be the generated workspace for this candidate")
        if self.work_kernel is None:
            _fail("work_kernel_unavailable", "Candidate effects require the canonical Work Kernel")
        query = self.work_kernel.query(work_id)
        if query is None or query.work.task_id != task_id:
            _fail("task_binding_mismatch", "Candidate work item is not bound to the supplied canonical Task")
        self.work_kernel.assert_current_lease(work_id, lease_id, fence)
        object_format = str(base_view.object_format)
        actual_base = self._workspace_entries(workspace, object_format=object_format)
        base_tree_digest = self._tree_digest(base_entries)
        if actual_base != base_entries:
            _fail("candidate_base_mismatch", "Candidate workspace does not exactly equal the committed base tree")

        normalized: list[dict[str, Any]] = []
        expanded: list[dict[str, Any]] = []
        seen_operations: set[str] = set()
        for raw in operations:
            if not isinstance(raw, Mapping):
                _fail("invalid_edit_operation", "each edit operation must be a mapping")
            operation = str(raw.get("operation", "")).upper()
            path = _path(raw.get("path"))
            if operation not in {"CREATE", "MODIFY", "DELETE", "RENAME"}:
                _fail("unsupported_edit_operation", "only CREATE, MODIFY, DELETE and RENAME are supported")
            if path in seen_operations:
                _fail("duplicate_edit_path", "an edit path may occur only once")
            seen_operations.add(path)
            mode = raw.get("mode", 0o644)
            if not isinstance(mode, int) or isinstance(mode, bool) or mode not in {0o644, 0o755, 0o100644, 0o100755}:
                _fail("invalid_edit_mode", "edit mode must be 644 or 755")
            mode = mode & 0o777
            content = raw.get("content")
            if content is not None and not isinstance(content, bytes):
                _fail("invalid_edit_bytes", "edit content must be bytes")
            if isinstance(content, bytes) and len(content) > MAX_FILE_BYTES:
                _fail("invalid_edit_bytes", "edit content is too large")
            replacements = raw.get("replacements")
            preimage_digest = raw.get("preimage_digest")
            source_path = raw.get("source_path")
            if source_path is not None:
                source_path = _path(source_path)
            if operation == "RENAME":
                if source_path is None or source_path == path or content is not None or replacements is not None or preimage_digest is not None:
                    _fail("invalid_rename", "RENAME requires a distinct source_path and no content")
                if source_path.casefold() == path.casefold():
                    _fail("case_collision", "RENAME cannot change only the case of a Windows path")
                if source_path in seen_operations:
                    _fail("duplicate_edit_path", "a rename source may occur only once")
                seen_operations.add(source_path)
                normalized.append({"operation": operation, "path": path, "source_path": source_path, "mode": mode})
                expanded.extend((
                    {"operation": "DELETE", "path": source_path, "mode": mode, "rename_destination": path},
                    {"operation": "CREATE", "path": path, "mode": mode, "rename_source": source_path},
                ))
            else:
                if source_path is not None:
                    _fail("unexpected_source_path", "source_path is valid only for RENAME")
                if operation == "CREATE" and (not isinstance(content, bytes) or replacements is not None or preimage_digest is not None):
                    _fail("invalid_edit_bytes", "CREATE requires content bytes")
                if operation == "MODIFY":
                    has_content = isinstance(content, bytes)
                    has_replacements = replacements is not None
                    if has_content == has_replacements:
                        _fail("invalid_edit_bytes", "MODIFY requires either content bytes or exact replacements")
                    if has_replacements and not isinstance(preimage_digest, str):
                        _fail("replacement_preimage_required", "exact replacements require a preimage digest")
                if operation == "DELETE" and (content is not None or replacements is not None or preimage_digest is not None):
                    _fail("invalid_edit_bytes", "DELETE cannot include content")
                normalized.append({"operation": operation, "path": path, "mode": mode, "content": content, "replacements": replacements, "preimage_digest": preimage_digest})
                expanded.append({"operation": operation, "path": path, "mode": mode, "content": content, "replacements": replacements, "preimage_digest": preimage_digest})

        final_entries = dict(base_entries)
        folded: dict[str, str] = {path.casefold(): path for path in final_entries}
        plans: list[CandidatePathPlan] = []
        absence = _absence_ref()
        self.content_store.publish(absence, b"")
        for item in expanded:
            operation = str(item["operation"])
            path = str(item["path"])
            key = path.casefold()
            existing_path = folded.get(key)
            if existing_path is not None and existing_path != path:
                _fail("case_collision", "edit paths collide on a case-insensitive filesystem")
            if operation == "CREATE":
                if path in final_entries:
                    _fail("path_already_exists", "CREATE requires an absent path")
                before, before_ref, before_mode, before_size = b"", absence, 0o644, 0
                after_mode = int(item["mode"])
                if "rename_source" in item:
                    source = str(item["rename_source"])
                    if source not in base_entries:
                        _fail("path_not_in_base", "RENAME source must exist in the committed base")
                    before_source = _read_exact(_safe_child(workspace, source))
                    after = before_source
                    after_mode = _file_mode(_safe_child(workspace, source))
                else:
                    after = item.get("content")
                if not isinstance(after, bytes):
                    _fail("invalid_edit_bytes", "CREATE requires content bytes")
                after_ref = make_content_ref("application/octet-stream", "m4b-candidate-after-v1", after)
                self.content_store.publish(before_ref, before)
                self.content_store.publish(after_ref, after)
                plan = CandidatePathPlan(path, _digest(before), _digest(after), before_ref, after_ref, before_mode, after_mode, before_size, len(after))
                plans.append(plan)
                final_entries[path] = (_blob_oid(after, object_format), "100755" if after_mode & 0o111 else "100644")
                folded[key] = path
            elif operation == "DELETE":
                if path not in final_entries:
                    _fail("path_not_in_base", "DELETE requires an existing committed path")
                target = _safe_child(workspace, path)
                before = _read_exact(target)
                before_mode = _file_mode(target)
                before_ref = make_content_ref("application/octet-stream", "m4b-candidate-before-v1", before)
                self.content_store.publish(before_ref, before)
                self.content_store.publish(absence, b"")
                plans.append(CandidatePathPlan(path, _digest(before), _digest(b""), before_ref, absence, before_mode, 0o644, len(before), 0))
                final_entries.pop(path, None)
                folded.pop(key, None)
            else:
                if path not in final_entries:
                    _fail("path_not_in_base", "MODIFY requires an existing committed path")
                target = _safe_child(workspace, path)
                before = _read_exact(target)
                before_mode = _file_mode(target)
                replacements = item.get("replacements")
                preimage_digest = item.get("preimage_digest")
                if replacements is not None:
                    after = apply_exact_replacements(before, replacements, preimage_digest)
                else:
                    after = item.get("content")
                    if not isinstance(after, bytes):
                        _fail("invalid_edit_bytes", "MODIFY requires content bytes")
                    if preimage_digest is not None and preimage_digest != _digest(before):
                        _fail("replacement_preimage_mismatch", "exact replacement preimage does not match the current file")
                before_ref = make_content_ref("application/octet-stream", "m4b-candidate-before-v1", before)
                after_ref = make_content_ref("application/octet-stream", "m4b-candidate-after-v1", after)
                self.content_store.publish(before_ref, before)
                self.content_store.publish(after_ref, after)
                requested_mode = int(item["mode"])
                plans.append(CandidatePathPlan(path, _digest(before), _digest(after), before_ref, after_ref, before_mode, before_mode if requested_mode == 0o644 else requested_mode, len(before), len(after)))
                final_entries[path] = (_blob_oid(after, object_format), "100755" if int(item["mode"]) & 0o111 else "100644")
        if len(plans) > MAX_PATHS:
            _fail("invalid_write_set", "expanded edit write set is too large")
        planned_tree_digest = self._tree_digest(final_entries)
        effect_material = {
            "schema": CANDIDATE_EFFECT_SCHEMA,
            "class": "EXACT_EDIT_V1",
            "candidate_id": candidate_id,
            "work_id": work_id,
            "task_id": task_id,
            "lease_id": lease_id,
            "fence": fence,
            "base_view": base,
            "base_tree_digest": base_tree_digest,
            "planned_tree_digest": planned_tree_digest,
            "workspace_root": str(workspace),
            "workspace_generation": self.generation,
            "config_digest": self.config_digest,
            "paths": [item.as_dict() for item in plans],
            "operations": [{key: _effect_value(value) for key, value in item.items()} for item in normalized],
        }
        expected_effect_id = semantic_digest(effect_material)
        if effect_id is not None and effect_id != expected_effect_id:
            _fail("effect_identity_mismatch", "effect_id does not equal the exact prepared edit digest", details={"expected": expected_effect_id})
        effect_id = expected_effect_id
        if self.get(candidate_id) is not None:
            _fail("candidate_exists", "candidate_id is already bound")
        begin_control_write(self._connection)
        try:
            self._connection.execute(
                "INSERT INTO m4b_candidate_effects(candidate_id,effect_id,work_id,task_id,state,effect_certainty,base_view_json,workspace_root,workspace_generation,config_digest,lease_id,fence,base_tree_digest,planned_tree_digest,observed_tree_digest,observed_json,candidate_view_json,manifest_digest) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (candidate_id, effect_id, work_id, task_id, CANDIDATE_PREPARED, "NOT_ASSESSED", canonical_json_bytes(base), str(workspace), self.generation, self.config_digest, lease_id, fence, base_tree_digest, planned_tree_digest, None, b"{}", None, None),
            )
            for item in plans:
                self._connection.execute(
                    "INSERT INTO m4b_candidate_paths(candidate_id,path,before_digest,after_digest,before_ref_json,after_ref_json,before_mode,after_mode,before_size,after_size,observed) VALUES (?,?,?,?,?,?,?,?,?,?,NULL)",
                    (candidate_id, item.path, item.before_digest, item.after_digest, canonical_json_bytes(item.before_ref.as_dict()), canonical_json_bytes(item.after_ref.as_dict()), item.before_mode, item.after_mode, item.before_size, item.after_size),
                )
            commit_control_write(self._connection)
        except Exception:
            self._connection.rollback()
            raise
        return self.get(candidate_id)  # type: ignore[return-value]

    def reprepare_desired(
        self,
        *,
        candidate_id: str,
        base_view: CommittedRepoView,
        workspace_root: str | Path,
        desired_files: Mapping[str, bytes | None],
        desired_modes: Mapping[str, int] | None = None,
        allow_noop_only: bool = False,
    ) -> CandidateRecord:
        """Replan one unfinished Candidate after a bounded validation loop.

        Edit batches remain durable facts in the engineering-loop table; this
        method keeps one Candidate identity and replaces only its unsealed
        plan.  The current workspace is the exact BEFORE image for the next
        apply, while the original Committed RepoView remains the tree basis.
        """

        record = self.get(candidate_id)
        if record is None:
            _fail("candidate_missing", "Candidate does not exist")
        if record.state in {CANDIDATE_SEALED, CANDIDATE_INVALIDATED}:
            _fail("candidate_state_conflict", "sealed or invalidated Candidate cannot be replanned")
        self._assert_owner(record)
        base_view.validate_integrity()
        workspace = _safe_root(Path(workspace_root))
        if workspace != self._workspace_for(candidate_id):
            _fail("foreign_workspace", "Candidate workspace must be the generated workspace for this candidate")
        base_entries = self._base_entries(base_view)
        actual_entries = self._workspace_entries(workspace, object_format=base_view.object_format)
        desired: dict[str, bytes | None] = {}
        for raw_path, content in desired_files.items():
            path = _path(raw_path)
            if content is not None and (not isinstance(content, bytes) or len(content) > MAX_FILE_BYTES):
                _fail("invalid_edit_bytes", "desired file content is invalid or too large")
            desired[path] = content
        expected_paths = set(base_entries) | set(desired)
        if set(actual_entries) - expected_paths:
            _fail("foreign_candidate_state", "Candidate workspace contains a path outside the accumulated edit plan")
        plans: list[CandidatePathPlan] = []
        planned_entries = dict(base_entries)
        modes = dict(desired_modes or {})
        for path in sorted(expected_paths):
            base_present = path in base_entries
            target_present = path in desired and desired[path] is not None
            if path in desired:
                after = desired[path]
            elif base_present:
                after = base_view.read_bytes(path)
            else:
                after = None
            current_present = path in actual_entries
            if current_present:
                current = _read_exact(_safe_child(workspace, path))
                current_mode = _file_mode(_safe_child(workspace, path))
            else:
                current = None
                current_mode = 0o644
            base_bytes = base_view.read_bytes(path) if base_present else None
            if current == base_bytes and after == base_bytes:
                continue
            before_ref = _absence_ref() if current is None else make_content_ref("application/octet-stream", "m4b-candidate-before-v1", current)
            after_ref = _absence_ref() if after is None else make_content_ref("application/octet-stream", "m4b-candidate-after-v1", after)
            if current is not None:
                self.content_store.publish(before_ref, current)
            else:
                self.content_store.publish(before_ref, b"")
            if after is not None:
                self.content_store.publish(after_ref, after)
            else:
                self.content_store.publish(after_ref, b"")
            after_mode = int(modes.get(path, current_mode if current is not None else (0o755 if path in base_entries and base_entries[path][1] == "100755" else 0o644))) & 0o777
            if allow_noop_only and current == after:
                # A legacy recovery rebind observes bytes already present in
                # the workspace.  Preserve the exact observed filesystem
                # mode so Windows ACL presentation cannot turn a no-op into
                # a synthetic write.
                after_mode = current_mode
            # A replan may carry an already-applied path from an earlier
            # model turn.  When the workspace is already exactly at the new
            # desired bytes there is no effect to apply for this path.  Keep
            # it in the planned tree, but do not create a BEFORE/AFTER plan:
            # such a no-op cannot be observed as a fresh filesystem effect
            # (and would otherwise leave the replan PREPARED forever).
            if current == after:
                if after is None:
                    planned_entries.pop(path, None)
                else:
                    planned_entries[path] = (_blob_oid(after, base_view.object_format), "100755" if after_mode & 0o111 else "100644")
                plans.append(CandidatePathPlan(
                    path,
                    _digest(current or b""),
                    _digest(after or b""),
                    before_ref,
                    after_ref,
                    current_mode,
                    after_mode,
                    len(current or b""),
                    len(after or b""),
                ))
                continue
            plan = CandidatePathPlan(path, _digest(current or b""), _digest(after or b""), before_ref, after_ref, current_mode, after_mode, len(current or b""), len(after or b""))
            plans.append(plan)
            if after is None:
                planned_entries.pop(path, None)
            else:
                planned_entries[path] = (_blob_oid(after, base_view.object_format), "100755" if after_mode & 0o111 else "100644")
        if not plans or (not allow_noop_only and all(self._is_noop_plan(item) for item in plans)):
            _fail("empty_edit_plan", "replanned Candidate has no net change")
        planned_tree_digest = self._tree_digest(planned_entries)
        effect_material = {
            "schema": CANDIDATE_EFFECT_SCHEMA,
            "class": "EXACT_EDIT_V1",
            "candidate_id": record.candidate_id,
            "work_id": record.work_id,
            "task_id": record.task_id,
            "lease_id": record.lease_id,
            "fence": record.fence,
            "base_view": base_view.to_dict(),
            "base_tree_digest": record.base_tree_digest,
            "planned_tree_digest": planned_tree_digest,
            "workspace_root": str(workspace),
            "workspace_generation": record.workspace_generation,
            "config_digest": record.config_digest,
            "paths": [item.as_dict() for item in plans],
        }
        effect_id = semantic_digest(effect_material)
        begin_control_write(self._connection)
        try:
            self._connection.execute("DELETE FROM m4b_candidate_paths WHERE candidate_id=?", (candidate_id,))
            self._connection.execute(
                "UPDATE m4b_candidate_effects SET effect_id=?,state=?,effect_certainty=?,base_view_json=?,planned_tree_digest=?,observed_tree_digest=NULL,observed_json=?,candidate_view_json=NULL,manifest_digest=NULL WHERE candidate_id=?",
                (effect_id, CANDIDATE_PREPARED, "NOT_ASSESSED", canonical_json_bytes(base_view.to_dict()), planned_tree_digest, b"{}", candidate_id),
            )
            for item in plans:
                self._connection.execute(
                    "INSERT INTO m4b_candidate_paths(candidate_id,path,before_digest,after_digest,before_ref_json,after_ref_json,before_mode,after_mode,before_size,after_size,observed) VALUES (?,?,?,?,?,?,?,?,?,?,NULL)",
                    (candidate_id, item.path, item.before_digest, item.after_digest, canonical_json_bytes(item.before_ref.as_dict()), canonical_json_bytes(item.after_ref.as_dict()), item.before_mode, item.after_mode, item.before_size, item.after_size),
                )
            commit_control_write(self._connection)
        except Exception:
            self._connection.rollback()
            raise
        return self.get(candidate_id)  # type: ignore[return-value]

    def mark_possible(self, candidate_id: str) -> CandidateRecord:
        record = self.get(candidate_id)
        if record is None:
            _fail("candidate_missing", "Candidate does not exist")
        self._assert_owner(record)
        if record.state == CANDIDATE_POSSIBLE:
            return record
        if record.state != CANDIDATE_PREPARED:
            _fail("candidate_state_conflict", "Candidate is not PREPARED")
        self._write(lambda: self._connection.execute("UPDATE m4b_candidate_effects SET state=?,effect_certainty=? WHERE candidate_id=?", (CANDIDATE_POSSIBLE, "POSSIBLE", record.candidate_id)))
        return self.get(candidate_id)  # type: ignore[return-value]

    def adopt_lease(self, candidate_id: str, *, lease_id: str, fence: int) -> CandidateRecord:
        """Transfer unfinished effect observation to the current fenced owner."""
        record = self.get(candidate_id)
        if record is None:
            _fail("candidate_missing", "Candidate does not exist")
        if record.state in {CANDIDATE_SEALED, CANDIDATE_INVALIDATED}:
            _fail("candidate_state_conflict", "sealed or invalidated Candidate cannot change owner")
        lease_id = _id(lease_id, field="lease_id")
        if not isinstance(fence, int) or isinstance(fence, bool) or fence < 1:
            _fail("invalid_fence", "fence must be a positive integer")
        if self.work_kernel is None:
            _fail("work_kernel_unavailable", "Candidate effects require the canonical Work Kernel")
        self.work_kernel.assert_current_lease(record.work_id, lease_id, fence)
        self._write(lambda: self._connection.execute("UPDATE m4b_candidate_effects SET lease_id=?,fence=? WHERE candidate_id=?", (lease_id, fence, record.candidate_id)))
        return self.get(candidate_id)  # type: ignore[return-value]

    def _observe_one(self, workspace: Path, plan: CandidatePathPlan) -> str:
        target = _safe_child(workspace, plan.path)
        exists = _path_exists(target)
        before_absent = _is_absence_ref(plan.before_ref)
        after_absent = _is_absence_ref(plan.after_ref)
        if not exists:
            if before_absent and after_absent:
                return "AFTER"
            if before_absent:
                return "BEFORE"
            if after_absent:
                return "AFTER"
            return "DIVERGED"
        if before_absent and after_absent:
            return "DIVERGED"
        try:
            actual = _read_exact(target)
        except CandidateError as exc:
            if exc.code == "candidate_path_missing":
                return "DIVERGED"
            raise
        digest = _digest(actual)
        try:
            mode = _file_mode(target)
        except CandidateError:
            return "DIVERGED"
        before_mode_matches = mode == plan.before_mode
        after_mode_matches = mode == plan.after_mode
        if _is_absence_ref(plan.before_ref):
            # Windows ACLs commonly expose 0666 for a newly-created regular
            # file even when the Candidate tree contract is 100644.  For a
            # path whose BEFORE state was absence, compare the Git mode while
            # retaining exact filesystem mode checks for existing files.
            after_mode_matches = _git_mode(target) == ("100755" if plan.after_mode & 0o111 else "100644")
        if self._is_noop_plan(plan) and digest == plan.after_digest and after_mode_matches:
            return "AFTER"
        if digest == plan.before_digest and before_mode_matches:
            return "BEFORE"
        if digest == plan.after_digest and after_mode_matches:
            return "AFTER"
        return "DIVERGED"

    def observe(self, candidate_id: str, *, fault: str | None = None) -> CandidateRecord:
        if fault not in {None, "during_observation"}:
            _fail("unsupported_fault", "unsupported observation fault injection")
        record = self.get(candidate_id)
        if record is None:
            _fail("candidate_missing", "Candidate does not exist")
        if record.state in {CANDIDATE_SEALED, CANDIDATE_INVALIDATED}:
            return record
        self._assert_owner(record)
        try:
            workspace = _safe_root(Path(record.workspace_root))
        except CandidateError as exc:
            if exc.code in {"workspace_unavailable", "workspace_reparse_point"}:
                self._set_uncertain(record.candidate_id, certainty="UNKNOWN", state=CANDIDATE_UNKNOWN)
                return self.get(candidate_id)  # type: ignore[return-value]
            raise
        observations = {item.path: self._observe_one(workspace, item) for item in record.planned_paths}
        if fault == "during_observation":
            raise CandidateError("candidate_observation_interrupted", "observation interrupted before durable observation commit")
        if all(value == "BEFORE" for value in observations.values()):
            state, certainty = CANDIDATE_PREPARED, "NOT_ASSESSED"
        elif all(value == "AFTER" for value in observations.values()):
            state, certainty = CANDIDATE_OBSERVED, "CERTAIN"
        elif any(value == "DIVERGED" for value in observations.values()):
            state, certainty = CANDIDATE_DIVERGED, "UNKNOWN"
        else:
            state, certainty = CANDIDATE_DIVERGED, "UNKNOWN"
        def persist_observation() -> None:
            self._connection.execute("UPDATE m4b_candidate_effects SET state=?,effect_certainty=?,observed_json=? WHERE candidate_id=?", (state, certainty, canonical_json_bytes(observations), record.candidate_id))
            for path, value in observations.items():
                self._connection.execute("UPDATE m4b_candidate_paths SET observed=? WHERE candidate_id=? AND path=?", (value, record.candidate_id, path))

        self._write(persist_observation)
        return self.get(candidate_id)  # type: ignore[return-value]

    def _set_uncertain(self, candidate_id: str, *, certainty: str = "UNKNOWN", state: str = CANDIDATE_UNKNOWN) -> None:
        self._write(lambda: self._connection.execute("UPDATE m4b_candidate_effects SET state=?,effect_certainty=? WHERE candidate_id=?", (state, certainty, candidate_id)))

    def apply(self, candidate_id: str, *, fail_after_paths: int | None = None, fault: str | None = None) -> CandidateRecord:
        allowed_faults = {None, "before_write", "during_temp_create", "after_temp_write", "during_replace", "locked_file", "permission_denied", "disk_full", "after_write_before_observe"}
        if fault not in allowed_faults:
            _fail("unsupported_fault", "unsupported deterministic filesystem fault injection")
        record = self.mark_possible(candidate_id)
        workspace = _safe_root(Path(record.workspace_root))
        observations = {item.path: self._observe_one(workspace, item) for item in record.planned_paths}
        if any(value not in {"BEFORE", "AFTER"} for value in observations.values()):
            _fail("apply_requires_before", "apply requires exact BEFORE observations; inspect before retry")
        if any(value == "AFTER" and not self._is_noop_plan(item) and not (_is_absence_ref(item.before_ref) and _is_absence_ref(item.after_ref)) for item, value in ((item, observations[item.path]) for item in record.planned_paths)):
            _fail("apply_requires_before", "apply requires exact BEFORE observations; inspect before retry")
        if fault == "before_write":
            raise CandidateError("candidate_apply_interrupted", "apply stopped before first filesystem write", details={"effect_certainty": "POSSIBLE"})
        for index, plan in enumerate(record.planned_paths, start=1):
            self._assert_owner(self.get(candidate_id))  # type: ignore[arg-type]
            if self._is_noop_plan(plan):
                continue
            target = _safe_child(workspace, plan.path)
            _safe_child(workspace, plan.path)
            before_absent = _is_absence_ref(plan.before_ref)
            after_absent = _is_absence_ref(plan.after_ref)
            if before_absent and after_absent and not _path_exists(target):
                if fail_after_paths is not None and index >= fail_after_paths:
                    self._set_uncertain(candidate_id, certainty="POSSIBLE", state=CANDIDATE_POSSIBLE)
                    return self.get(candidate_id)  # type: ignore[return-value]
                continue
            if before_absent:
                if _path_exists(target):
                    self._set_uncertain(candidate_id, certainty="UNKNOWN", state=CANDIDATE_DIVERGED)
                    _fail("candidate_concurrent_modification", "planned creation path already exists")
            else:
                try:
                    current = _read_exact(target)
                    current_digest = _digest(current)
                    current_mode = _file_mode(target)
                except CandidateError:
                    self._set_uncertain(candidate_id, certainty="UNKNOWN", state=CANDIDATE_DIVERGED)
                    _fail("candidate_concurrent_modification", "planned path changed before the filesystem replacement")
                if current_digest != plan.before_digest or current_mode != plan.before_mode:
                    self._set_uncertain(candidate_id, certainty="UNKNOWN", state=CANDIDATE_DIVERGED)
                    _fail("candidate_concurrent_modification", "planned path changed before the filesystem replacement")
            if fault == "during_temp_create":
                self._set_uncertain(candidate_id, certainty="POSSIBLE", state=CANDIDATE_POSSIBLE)
                raise CandidateError("candidate_apply_failed", "temporary file creation failed; observation is required", details={"fault": fault})
            try:
                if after_absent:
                    if fault in {"locked_file", "permission_denied", "disk_full", "during_replace"}:
                        raise OSError(f"injected filesystem fault: {fault}")
                    target.unlink()
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    fd, temporary_name = tempfile.mkstemp(prefix=".m4b-", dir=str(target.parent))
                    temporary = Path(temporary_name)
                    try:
                        with os.fdopen(fd, "wb") as handle:
                            handle.write(self.content_store.resolve(plan.after_ref))
                            if fault == "after_temp_write":
                                raise OSError("injected filesystem fault: after_temp_write")
                            handle.flush()
                            os.fsync(handle.fileno())
                        if fault in {"locked_file", "permission_denied", "disk_full", "during_replace"}:
                            raise OSError(f"injected filesystem fault: {fault}")
                        _safe_child(workspace, plan.path)
                        os.replace(temporary, target)
                        os.chmod(target, plan.after_mode)
                    finally:
                        if temporary.exists():
                            temporary.unlink(missing_ok=True)
            except OSError as exc:
                self._set_uncertain(candidate_id, certainty="POSSIBLE" if fault else "UNKNOWN", state=CANDIDATE_POSSIBLE if fault else CANDIDATE_UNKNOWN)
                raise CandidateError("candidate_apply_failed", "filesystem apply failed; exact observation is required", details={"fault": fault or "os_error"}) from exc
            if fail_after_paths is not None and index >= fail_after_paths:
                self._set_uncertain(candidate_id, certainty="POSSIBLE", state=CANDIDATE_POSSIBLE)
                return self.get(candidate_id)  # type: ignore[return-value]
        self._write(lambda: self._connection.execute("UPDATE m4b_candidate_effects SET state=?,effect_certainty=? WHERE candidate_id=?", (CANDIDATE_APPLIED, "POSSIBLE", candidate_id)))
        if fault == "after_write_before_observe":
            raise CandidateError("candidate_apply_interrupted", "all writes completed before observation", details={"effect_certainty": "POSSIBLE"})
        return self.observe(candidate_id)

    def _candidate_view(self, record: CandidateRecord, view: CommittedRepoView) -> CandidateRepoView:
        document = record.manifest_digest
        if record.state != CANDIDATE_SEALED or not document or not isinstance(record.candidate_view_id, Mapping):
            _fail("candidate_not_sealed", "Candidate has no immutable sealed view")
        schema = record.candidate_view_id.get("schema")
        if schema not in {CANDIDATE_VIEW_SCHEMA_V1, CANDIDATE_VIEW_SCHEMA}:
            _fail("candidate_view_integrity_failure", "persisted Candidate view schema is unsupported")
        base = record.base_view
        expected_document = {
            "schema": schema,
            "kind": CANDIDATE_KIND,
            "candidate_id": record.candidate_id,
            "effect_id": record.effect_id,
            "work_id": record.work_id,
            "task_id": record.task_id,
            "repository_id": str(base["repository"]["repository_id"]),
            "base_view_id": str(base["view_id"]),
            "base_commit_oid": str(base["commit_oid"]),
            "base_tree_oid": str(base["tree_oid"]),
            "candidate_tree_digest": record.observed_tree_digest or record.planned_tree_digest,
            "changed_paths": [item.path for item in record.planned_paths],
            "path_bindings": [item.as_dict() for item in record.planned_paths],
            "workspace_generation": record.workspace_generation,
            "config_digest": record.config_digest,
            "view_id": document,
            "manifest_digest": document,
        }
        if schema == CANDIDATE_VIEW_SCHEMA:
            expected_document["base_authority"] = record.candidate_view_id.get("base_authority")
        if dict(record.candidate_view_id) != expected_document:
            _fail("candidate_view_integrity_failure", "persisted Candidate view differs from its sealed record")
        return CandidateRepoView(
            str(schema), CANDIDATE_KIND, record.candidate_id, record.effect_id, record.work_id, record.task_id,
            str(base["repository"]["repository_id"]), str(base["view_id"]), str(base["commit_oid"]), str(base["tree_oid"]),
            record.observed_tree_digest or record.planned_tree_digest, tuple(item.path for item in record.planned_paths), record.planned_paths,
            dict(record.candidate_view_id["base_authority"]) if schema == CANDIDATE_VIEW_SCHEMA else {}, document, record.workspace_generation, record.config_digest, self, view,
        )

    def seal(self, candidate_id: str, *, base_view: CommittedRepoView | None = None, fault: str | None = None) -> tuple[CandidateRecord, CandidateRepoView]:
        if fault not in {None, "before_seal_commit", "after_seal_commit"}:
            _fail("unsupported_fault", "unsupported seal fault injection")
        record = self.observe(candidate_id)
        self._assert_owner(record)
        if record.state != CANDIDATE_OBSERVED or record.effect_certainty != "CERTAIN":
            _fail("candidate_not_exact", "Candidate cannot seal without exact AFTER observation")
        if base_view is None:
            _fail("base_view_required", "sealing requires the exact CommittedRepoView for tree proof")
        base_view.validate_integrity()
        if base_view.view_id != str(record.base_view["view_id"]):
            _fail("base_view_mismatch", "seal base view differs from prepared exact basis")
        workspace = _safe_root(Path(record.workspace_root))
        actual_entries = self._workspace_entries(workspace, object_format=base_view.object_format)
        base_entries = self._base_entries(base_view)
        planned_entries = dict(base_entries)
        for item in record.planned_paths:
            if _is_absence_ref(item.after_ref):
                planned_entries.pop(item.path, None)
            else:
                planned_entries[item.path] = (_blob_oid(self.content_store.resolve(item.after_ref), base_view.object_format), "100755" if item.after_mode & 0o111 else "100644")
        observed_digest = self._tree_digest(actual_entries)
        planned_digest = self._tree_digest(planned_entries)
        if actual_entries != planned_entries or observed_digest != planned_digest:
            self._write(lambda: self._connection.execute("UPDATE m4b_candidate_effects SET state=?,effect_certainty=?,observed_tree_digest=? WHERE candidate_id=?", (CANDIDATE_DIVERGED, "UNKNOWN", observed_digest, record.candidate_id)))
            _fail("candidate_tree_mismatch", "observed Candidate tree is not exactly the planned tree", details={"planned": planned_digest, "observed": observed_digest})
        base_authority = self._archive_base_authority(workspace, base_view)
        identity = {
            "schema": CANDIDATE_VIEW_SCHEMA,
            "kind": CANDIDATE_KIND,
            "candidate_id": record.candidate_id,
            "effect_id": record.effect_id,
            "work_id": record.work_id,
            "task_id": record.task_id,
            "repository_id": str(record.base_view["repository"]["repository_id"]),
            "base_view_id": str(record.base_view["view_id"]),
            "base_commit_oid": str(record.base_view["commit_oid"]),
            "base_tree_oid": str(record.base_view["tree_oid"]),
            "candidate_tree_digest": observed_digest,
            "changed_paths": [item.path for item in record.planned_paths],
            "path_bindings": [item.as_dict() for item in record.planned_paths],
            "base_authority": base_authority,
            "workspace_generation": record.workspace_generation,
            "config_digest": record.config_digest,
        }
        manifest_digest = semantic_digest(identity)
        view_doc = {**identity, "view_id": manifest_digest, "manifest_digest": manifest_digest}
        if fault == "before_seal_commit":
            raise CandidateError("candidate_seal_interrupted", "seal interrupted before durable manifest commit")
        self._write(lambda: self._connection.execute("UPDATE m4b_candidate_effects SET state=?,effect_certainty=?,observed_tree_digest=?,candidate_view_json=?,manifest_digest=? WHERE candidate_id=?", (CANDIDATE_SEALED, "CERTAIN", observed_digest, canonical_json_bytes(view_doc), manifest_digest, record.candidate_id)))
        if fault == "after_seal_commit":
            raise CandidateError("candidate_seal_response_lost", "seal committed before caller received its response", details={"manifest_digest": manifest_digest})
        updated = self.get(candidate_id)
        assert updated is not None
        return updated, self._candidate_view(updated, base_view)

    def get_view(self, candidate_id: str, base_view: CommittedRepoView) -> CandidateRepoView:
        record = self.get(candidate_id)
        if record is None:
            _fail("candidate_missing", "Candidate does not exist")
        if record.base_view.get("view_id") != base_view.view_id:
            _fail("base_view_mismatch", "requested view differs from sealed base")
        return self._candidate_view(record, base_view)

    def invalidate_if_changed(self, candidate_id: str, *, base_view: CommittedRepoView | None = None) -> CandidateRecord:
        record = self.get(candidate_id)
        if record is None:
            _fail("candidate_missing", "Candidate does not exist")
        if record.state != CANDIDATE_SEALED:
            return record
        workspace_path = Path(record.workspace_root)
        if not workspace_path.exists():
            return record
        workspace = _safe_root(workspace_path)
        changed = any(self._observe_one(workspace, item) != "AFTER" for item in record.planned_paths)
        if base_view is not None:
            base_view.validate_integrity()
            actual = self._workspace_entries(workspace, object_format=base_view.object_format)
            planned = self._base_entries(base_view)
            for item in record.planned_paths:
                if _is_absence_ref(item.after_ref):
                    planned.pop(item.path, None)
                else:
                    planned[item.path] = (_blob_oid(self.content_store.resolve(item.after_ref), base_view.object_format), "100755" if item.after_mode & 0o111 else "100644")
            changed = changed or actual != planned
        if changed:
            # Revalidation is readable after the producing lease is released,
            # but changing canonical Candidate state still requires the
            # current Work lease/fence.  A stale reader therefore fails closed
            # rather than silently rewriting the sealed record.
            self._assert_owner(record)
            self._write(lambda: self._connection.execute("UPDATE m4b_candidate_effects SET state=?,effect_certainty=? WHERE candidate_id=?", (CANDIDATE_INVALIDATED, "UNKNOWN", record.candidate_id)))
            return self.get(candidate_id)  # type: ignore[return-value]
        return record

    def verify_sealed(self, candidate_id: str, *, base_view: CommittedRepoView | None = None) -> CandidateRecord:
        """Read-only seal check used by Candidate readers and restart recovery."""
        record = self.get(candidate_id)
        if record is None:
            _fail("candidate_missing", "Candidate does not exist")
        if record.state != CANDIDATE_SEALED:
            _fail("candidate_not_sealed", "Candidate has no sealed immutable view")
        if base_view is None:
            return record
        base_view.validate_integrity()
        workspace_path = Path(record.workspace_root)
        if not workspace_path.exists():
            # After seal the CAS + manifest + exact committed base are the
            # Candidate authority; mutable workspace retention is optional.
            return record
        actual = self._workspace_entries(_safe_root(workspace_path), object_format=base_view.object_format)
        planned = self._base_entries(base_view)
        for item in record.planned_paths:
            if _is_absence_ref(item.after_ref):
                planned.pop(item.path, None)
            else:
                planned[item.path] = (_blob_oid(self.content_store.resolve(item.after_ref), base_view.object_format), "100755" if item.after_mode & 0o111 else "100644")
        if actual != planned:
            _fail("candidate_invalidated", "sealed Candidate workspace no longer matches its immutable tree")
        return record

    def dispose_workspace_from_source(self, candidate_id: str, source_root: str | Path) -> None:
        record = self.get(candidate_id)
        if record is not None and record.state == CANDIDATE_SEALED:
            _fail("sealed_workspace_retained", "sealed Candidate workspaces are retained for downstream evidence")
        workspace = self._workspace_for(_id(candidate_id, field="candidate_id"))
        if not workspace.exists():
            return
        source = Path(source_root).expanduser().absolute()
        completed = subprocess.run(["git", "-C", str(source), "worktree", "remove", "--force", str(workspace)], shell=False, capture_output=True, text=True, timeout=30, check=False)
        if completed.returncode != 0:
            raise CandidateError("workspace_dispose_failed", "Candidate workspace could not be disposed", details={"stderr": completed.stderr[-1000:]})

    def close(self) -> None:
        self.bindings.close()

    def __enter__(self) -> "CandidateStore":
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()


__all__ = [
    "CANDIDATE_EFFECT_CLASS", "CANDIDATE_EFFECT_SCHEMA", "CANDIDATE_KIND", "CANDIDATE_PREPARED", "CANDIDATE_POSSIBLE",
    "CANDIDATE_OBSERVED", "CANDIDATE_SEALED", "CANDIDATE_DIVERGED", "CANDIDATE_UNKNOWN", "CANDIDATE_INVALIDATED",
    "CANDIDATE_SCHEMA", "CANDIDATE_VIEW_SCHEMA", "CANDIDATE_VIEW_SCHEMA_V1", "CANDIDATE_BASE_AUTHORITY_SCHEMA", "CandidateError", "CandidatePathPlan", "CandidateRecord", "CandidateRepoView", "CandidateStore",
]

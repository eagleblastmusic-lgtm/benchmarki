"""Bounded M4b exact local effect and sealed Candidate RepoView.

Only ``EXACT_REPLACEMENT_V1`` is supported.  The source checkout and Git refs
are read-only; all mutable coordination state lives in the N1 Control DB and
immutable replacement bytes use the existing Content CAS.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tempfile
from collections.abc import Mapping
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
from bdb_vnext.control_store import assert_database_path, configure_connection, ensure_identity
from bdb_vnext.repo_view import CommittedRepoView, RepoTreeEntry


CANDIDATE_SCHEMA = "bdb-vnext-candidate-v1"
CANDIDATE_PATH_SCHEMA = "bdb-vnext-candidate-path-v1"
CANDIDATE_EFFECT_SCHEMA = "bdb-vnext-candidate-effect-v1"
CANDIDATE_VIEW_SCHEMA = "bdb-vnext-candidate-repo-view-v1"
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


def _git_mode(path: Path) -> str:
    return "100755" if (_file_mode(path) & 0o111) else "100644"


def _git_index_modes(workspace: Path) -> dict[str, str]:
    """Read canonical Git modes instead of inferring them from Windows ACLs.

    On Windows a checked-out ``.cmd`` file can expose an executable bit through
    ``stat`` even though the committed tree records mode ``100644``.  Candidate
    tree equality is a Git-object contract, so tracked modes must come from the
    index; the filesystem mode remains only a fallback for foreign/untracked
    files (which are rejected by the exact-tree comparison).
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
    modes: dict[str, str] = {}
    for item in text.split("\x00"):
        if not item:
            continue
        prefix, separator, path = item.partition("\t")
        if not separator:
            continue
        mode = prefix.split(" ", 1)[0]
        if mode in {"100644", "100755", "120000", "160000"}:
            modes[path.replace("\\", "/")] = mode
    return modes


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
    manifest_digest: str
    workspace_generation: str
    config_digest: str
    _store: "CandidateStore" = field(repr=False, compare=False, default=None)  # type: ignore[assignment]
    _base_view: CommittedRepoView | None = field(repr=False, compare=False, default=None)

    def __post_init__(self) -> None:
        if self.schema != CANDIDATE_VIEW_SCHEMA or self.kind != CANDIDATE_KIND or self._store is None:
            _fail("candidate_view_invalid", "unsupported Candidate RepoView")
        if semantic_digest(self._identity_payload()) != self.manifest_digest:
            _fail("candidate_view_integrity_failure", "Candidate view identity digest differs")

    def _identity_payload(self) -> dict[str, Any]:
        return {
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
            raw = self._store.content_store.resolve(plan.after_ref)
            entry = entries.get(plan.path)
            if entry is None:
                _fail("candidate_path_not_planned", "Candidate path is absent from the exact base tree")
            entries[plan.path] = RepoTreeEntry(
                path=plan.path,
                mode=entry.mode,
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
        self._connection.commit()
        columns = {str(row[1]) for row in self._connection.execute("PRAGMA table_info(m4b_candidate_effects)").fetchall()}
        if "config_digest" not in columns:
            self._connection.execute("ALTER TABLE m4b_candidate_effects ADD COLUMN config_digest TEXT NOT NULL DEFAULT ''")
            self._connection.commit()

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
        index_modes = _git_index_modes(workspace)
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
                raw = _read_exact(child)
                relative = child.relative_to(workspace).as_posix()
                normalized = _path(relative)
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

    def _plan_from_row(self, row: tuple[Any, ...]) -> CandidatePathPlan:
        return CandidatePathPlan(
            str(row[1]), str(row[2]), str(row[3]),
            ContentRef.from_mapping(json.loads(bytes(row[4]).decode("utf-8"))),
            ContentRef.from_mapping(json.loads(bytes(row[5]).decode("utf-8"))),
            int(row[6]), int(row[7]), int(row[8]), int(row[9]),
        )

    def _record_row(self, row: tuple[Any, ...]) -> CandidateRecord:
        paths = tuple(self._plan_from_row(item) for item in self._connection.execute(
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

    def get(self, candidate_id: str) -> CandidateRecord | None:
        row = self._connection.execute(
            "SELECT candidate_id,effect_id,work_id,task_id,state,effect_certainty,base_view_json,workspace_root,workspace_generation,config_digest,lease_id,fence,base_tree_digest,planned_tree_digest,observed_tree_digest,observed_json,candidate_view_json,manifest_digest FROM m4b_candidate_effects WHERE candidate_id=?",
            (_id(candidate_id, field="candidate_id"),),
        ).fetchone()
        return self._record_row(row) if row else None

    def create_workspace(self, *, candidate_id: str, base_view: CommittedRepoView) -> Path:
        candidate_id = _id(candidate_id, field="candidate_id")
        base_view.validate_integrity()
        workspace = self._workspace_for(candidate_id)
        if workspace.exists():
            _fail("workspace_exists", "Candidate workspace is not disposable/empty")
        source = Path(base_view.repository.root).absolute()
        if _contains(self.root, source) or _contains(source, self.root):
            _fail("workspace_source_overlap", "Candidate workspace must not overlap the source repository")
        workspace.parent.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            ["git", "-c", "core.autocrlf=false", "-C", str(source), "worktree", "add", "--detach", str(workspace), base_view.commit_oid],
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
        self._connection.execute("BEGIN IMMEDIATE")
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
            self._connection.commit()
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
        self._connection.execute("UPDATE m4b_candidate_effects SET state=?,effect_certainty=? WHERE candidate_id=?", (CANDIDATE_POSSIBLE, "POSSIBLE", record.candidate_id))
        self._connection.commit()
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
        self._connection.execute("UPDATE m4b_candidate_effects SET lease_id=?,fence=? WHERE candidate_id=?", (lease_id, fence, record.candidate_id))
        self._connection.commit()
        return self.get(candidate_id)  # type: ignore[return-value]

    def _observe_one(self, workspace: Path, plan: CandidatePathPlan) -> str:
        try:
            actual = _read_exact(_safe_child(workspace, plan.path))
        except CandidateError as exc:
            if exc.code == "candidate_path_missing":
                return "DIVERGED"
            raise
        digest = _digest(actual)
        try:
            mode = _file_mode(_safe_child(workspace, plan.path))
        except CandidateError:
            return "DIVERGED"
        if digest == plan.before_digest and mode == plan.before_mode:
            return "BEFORE"
        if digest == plan.after_digest and mode == plan.after_mode:
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
        self._connection.execute("UPDATE m4b_candidate_effects SET state=?,effect_certainty=?,observed_json=? WHERE candidate_id=?", (state, certainty, canonical_json_bytes(observations), record.candidate_id))
        for path, value in observations.items():
            self._connection.execute("UPDATE m4b_candidate_paths SET observed=? WHERE candidate_id=? AND path=?", (value, record.candidate_id, path))
        self._connection.commit()
        return self.get(candidate_id)  # type: ignore[return-value]

    def _set_uncertain(self, candidate_id: str, *, certainty: str = "UNKNOWN", state: str = CANDIDATE_UNKNOWN) -> None:
        self._connection.execute("UPDATE m4b_candidate_effects SET state=?,effect_certainty=? WHERE candidate_id=?", (state, certainty, candidate_id))
        self._connection.commit()

    def apply(self, candidate_id: str, *, fail_after_paths: int | None = None, fault: str | None = None) -> CandidateRecord:
        allowed_faults = {None, "before_write", "during_temp_create", "after_temp_write", "during_replace", "locked_file", "permission_denied", "disk_full", "after_write_before_observe"}
        if fault not in allowed_faults:
            _fail("unsupported_fault", "unsupported deterministic filesystem fault injection")
        record = self.mark_possible(candidate_id)
        workspace = _safe_root(Path(record.workspace_root))
        observations = {item.path: self._observe_one(workspace, item) for item in record.planned_paths}
        if any(value != "BEFORE" for value in observations.values()):
            _fail("apply_requires_before", "apply requires exact BEFORE observations; inspect before retry")
        if fault == "before_write":
            raise CandidateError("candidate_apply_interrupted", "apply stopped before first filesystem write", details={"effect_certainty": "POSSIBLE"})
        for index, plan in enumerate(record.planned_paths, start=1):
            self._assert_owner(self.get(candidate_id))  # type: ignore[arg-type]
            target = _safe_child(workspace, plan.path)
            target.parent.mkdir(parents=True, exist_ok=True)
            _safe_child(workspace, plan.path)
            current = _read_exact(target)
            if _digest(current) != plan.before_digest or _file_mode(target) != plan.before_mode:
                self._set_uncertain(candidate_id, certainty="UNKNOWN", state=CANDIDATE_DIVERGED)
                _fail("candidate_concurrent_modification", "planned path changed before the filesystem replacement")
            if fault == "during_temp_create":
                self._set_uncertain(candidate_id, certainty="POSSIBLE", state=CANDIDATE_POSSIBLE)
                raise CandidateError("candidate_apply_failed", "temporary file creation failed; observation is required", details={"fault": fault})
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
            except OSError as exc:
                temporary.unlink(missing_ok=True)
                self._set_uncertain(candidate_id, certainty="POSSIBLE" if fault else "UNKNOWN", state=CANDIDATE_POSSIBLE if fault else CANDIDATE_UNKNOWN)
                raise CandidateError("candidate_apply_failed", "filesystem apply failed; exact observation is required", details={"fault": fault or "os_error"}) from exc
            if fail_after_paths is not None and index >= fail_after_paths:
                self._set_uncertain(candidate_id, certainty="POSSIBLE", state=CANDIDATE_POSSIBLE)
                return self.get(candidate_id)  # type: ignore[return-value]
        self._connection.execute("UPDATE m4b_candidate_effects SET state=?,effect_certainty=? WHERE candidate_id=?", (CANDIDATE_APPLIED, "POSSIBLE", candidate_id))
        self._connection.commit()
        if fault == "after_write_before_observe":
            raise CandidateError("candidate_apply_interrupted", "all writes completed before observation", details={"effect_certainty": "POSSIBLE"})
        return self.observe(candidate_id)

    def _candidate_view(self, record: CandidateRecord, view: CommittedRepoView) -> CandidateRepoView:
        document = record.manifest_digest
        if record.state != CANDIDATE_SEALED or not document or not isinstance(record.candidate_view_id, Mapping):
            _fail("candidate_not_sealed", "Candidate has no immutable sealed view")
        base = record.base_view
        expected_document = {
            "schema": CANDIDATE_VIEW_SCHEMA,
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
        if dict(record.candidate_view_id) != expected_document:
            _fail("candidate_view_integrity_failure", "persisted Candidate view differs from its sealed record")
        return CandidateRepoView(
            CANDIDATE_VIEW_SCHEMA, CANDIDATE_KIND, record.candidate_id, record.effect_id, record.work_id, record.task_id,
            str(base["repository"]["repository_id"]), str(base["view_id"]), str(base["commit_oid"]), str(base["tree_oid"]),
            record.observed_tree_digest or record.planned_tree_digest, tuple(item.path for item in record.planned_paths), record.planned_paths,
            document, record.workspace_generation, record.config_digest, self, view,
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
            planned_entries[item.path] = (_blob_oid(self.content_store.resolve(item.after_ref), base_view.object_format), "100755" if item.after_mode & 0o111 else "100644")
        observed_digest = self._tree_digest(actual_entries)
        planned_digest = self._tree_digest(planned_entries)
        if actual_entries != planned_entries or observed_digest != planned_digest:
            self._connection.execute("UPDATE m4b_candidate_effects SET state=?,effect_certainty=?,observed_tree_digest=? WHERE candidate_id=?", (CANDIDATE_DIVERGED, "UNKNOWN", observed_digest, record.candidate_id))
            self._connection.commit()
            _fail("candidate_tree_mismatch", "observed Candidate tree is not exactly the planned tree", details={"planned": planned_digest, "observed": observed_digest})
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
            "workspace_generation": record.workspace_generation,
            "config_digest": record.config_digest,
        }
        manifest_digest = semantic_digest(identity)
        view_doc = {**identity, "view_id": manifest_digest, "manifest_digest": manifest_digest}
        if fault == "before_seal_commit":
            raise CandidateError("candidate_seal_interrupted", "seal interrupted before durable manifest commit")
        self._connection.execute("UPDATE m4b_candidate_effects SET state=?,effect_certainty=?,observed_tree_digest=?,candidate_view_json=?,manifest_digest=? WHERE candidate_id=?", (CANDIDATE_SEALED, "CERTAIN", observed_digest, canonical_json_bytes(view_doc), manifest_digest, record.candidate_id))
        self._connection.commit()
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
        self._assert_owner(record)
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
                planned[item.path] = (_blob_oid(self.content_store.resolve(item.after_ref), base_view.object_format), "100755" if item.after_mode & 0o111 else "100644")
            changed = changed or actual != planned
        if changed:
            self._connection.execute("UPDATE m4b_candidate_effects SET state=?,effect_certainty=? WHERE candidate_id=?", (CANDIDATE_INVALIDATED, "UNKNOWN", record.candidate_id))
            self._connection.commit()
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
    "CANDIDATE_SCHEMA", "CANDIDATE_VIEW_SCHEMA", "CandidateError", "CandidatePathPlan", "CandidateRecord", "CandidateRepoView", "CandidateStore",
]

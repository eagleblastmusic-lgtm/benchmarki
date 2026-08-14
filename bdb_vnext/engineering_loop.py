"""Bounded model-authored edit loop for the Phase 1 worktree vertical.

The module deliberately owns no lifecycle.  It parses a small ``BDB_EDIT_V1``
artifact, records edit/validation facts in the unified Control DB, delegates
filesystem authority to :class:`CandidateStore`, and uses the existing N3/N4
stores for evidence and publication.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from bdb_shared.evidence import canonical_json_bytes, semantic_digest
from bdb_vnext.candidate import CandidateError, CandidateRecord, CandidateRepoView, CandidateStore
from bdb_vnext.control_store import begin_control_write, commit_control_write
from bdb_vnext.content_store import make_content_ref
from bdb_vnext.m4c_evidence import EvidenceRecord, EvidenceStore, EvaluationRecord
from bdb_vnext.n4_publication import PublicationRecord, PublicationStore
from bdb_vnext.repo_view import CommittedRepoView


EDIT_SCHEMA = "bdb-vnext-edit-v1"
EDITOR_PORT_SCHEMA = "bdb-vnext-editor-port-v1"
EDIT_BATCH_SCHEMA = "bdb-vnext-edit-batch-v1"
VALIDATION_SCHEMA = "bdb-vnext-validation-run-v1"
EDIT_OPERATIONS: tuple[str, ...] = ("CREATE", "MODIFY", "DELETE", "RENAME")
MAX_EDIT_OPERATIONS = 32
MAX_EDIT_BYTES = 8 * 1024 * 1024
MAX_ARTIFACT_BYTES = 256 * 1024
MAX_VALIDATION_TIMEOUT = 300.0
MAX_VALIDATION_OUTPUT = 1 * 1024 * 1024


class EngineeringLoopError(RuntimeError):
    """Typed fail-closed error for model artifacts and checker execution."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def _fail(code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
    raise EngineeringLoopError(code, message, details=details)


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256 or "\x00" in value:
        _fail("invalid_identifier", f"{field_name} must be a bounded non-empty string")
    return value


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _path(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
        _fail("unsafe_edit_path", "edit path is invalid")
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if normalized.startswith("/") or any(part in {"", ".", ".."} for part in parts) or any(":" in part for part in parts):
        _fail("unsafe_edit_path", "edit paths must be relative and traversal-free")
    if any(part.upper() in reserved or part.endswith((".", " ")) for part in parts):
        _fail("unsafe_edit_path", "reserved Windows path names are not allowed")
    return normalized


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _decode_b64(value: object) -> bytes:
    if not isinstance(value, str) or len(value) > MAX_EDIT_BYTES * 2:
        _fail("invalid_edit_bytes", "content_b64 is invalid or too large")
    try:
        raw = base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise EngineeringLoopError("invalid_edit_bytes", "content_b64 is not canonical base64") from exc
    if len(raw) > MAX_EDIT_BYTES:
        _fail("invalid_edit_bytes", "edit content is too large")
    if _b64(raw) != value:
        _fail("invalid_edit_bytes", "content_b64 must use canonical base64")
    return raw


@dataclass(frozen=True)
class EditOperation:
    operation: Literal["CREATE", "MODIFY", "DELETE", "RENAME"]
    path: str
    content: bytes | None = None
    source_path: str | None = None
    mode: int = 0o644

    def __post_init__(self) -> None:
        if self.operation not in EDIT_OPERATIONS:
            _fail("unsupported_edit_operation", "unsupported edit operation")
        normalized = _path(self.path)
        object.__setattr__(self, "path", normalized)
        if self.source_path is not None:
            object.__setattr__(self, "source_path", _path(self.source_path))
        if self.mode not in {0o644, 0o755, 0o100644, 0o100755}:
            _fail("invalid_edit_mode", "mode must be 644 or 755")
        object.__setattr__(self, "mode", self.mode & 0o777)
        if self.operation == "RENAME":
            if self.source_path is None or self.source_path == self.path or self.content is not None:
                _fail("invalid_rename", "RENAME requires source_path and no content")
        elif self.source_path is not None:
            _fail("unexpected_source_path", "source_path is valid only for RENAME")
        elif self.operation in {"CREATE", "MODIFY"} and not isinstance(self.content, bytes):
            _fail("invalid_edit_bytes", f"{self.operation} requires content bytes")
        elif self.operation == "DELETE" and self.content is not None:
            _fail("invalid_edit_bytes", "DELETE cannot include content")
        if isinstance(self.content, bytes) and len(self.content) > MAX_EDIT_BYTES:
            _fail("invalid_edit_bytes", "edit content is too large")

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"operation": self.operation, "path": self.path, "mode": self.mode}
        if self.source_path is not None:
            result["source_path"] = self.source_path
        if self.content is not None:
            result["content_b64"] = _b64(self.content)
        return result

    def candidate_mapping(self) -> dict[str, Any]:
        return {"operation": self.operation, "path": self.path, "source_path": self.source_path, "content": self.content, "mode": self.mode}


@dataclass(frozen=True)
class EditBatch:
    schema: str
    base_view_id: str
    expected_tree_digest: str
    task_id: str
    work_id: str
    run_id: str
    lease_id: str
    fence: int
    candidate_id: str
    workspace_generation: str
    operations: tuple[EditOperation, ...]
    budget: Mapping[str, Any]
    artifact_digest: str

    def payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "base_view_id": self.base_view_id,
            "expected_tree_digest": self.expected_tree_digest,
            "task_id": self.task_id,
            "work_id": self.work_id,
            "run_id": self.run_id,
            "lease_id": self.lease_id,
            "fence": self.fence,
            "candidate_id": self.candidate_id,
            "workspace_generation": self.workspace_generation,
            "operations": [item.as_dict() for item in self.operations],
            "budget": dict(self.budget),
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self.payload(), "artifact_digest": self.artifact_digest}

    @classmethod
    def from_mapping(cls, document: Mapping[str, Any]) -> "EditBatch":
        if not isinstance(document, Mapping) or document.get("schema") != EDIT_SCHEMA:
            _fail("edit_schema_mismatch", "only BDB_EDIT_V1 artifacts are accepted")
        allowed = {"schema", "base_view_id", "expected_tree_digest", "task_id", "work_id", "run_id", "lease_id", "fence", "candidate_id", "workspace_generation", "operations", "budget", "artifact_digest"}
        if set(document) - allowed:
            _fail("edit_schema_mismatch", "edit artifact contains unknown fields")
        operations_raw = document.get("operations")
        if not isinstance(operations_raw, Sequence) or isinstance(operations_raw, (str, bytes)) or not operations_raw or len(operations_raw) > MAX_EDIT_OPERATIONS:
            _fail("invalid_edit_operation", "edit artifact operations are missing or too large")
        operations: list[EditOperation] = []
        folded: set[str] = set()
        total_bytes = 0
        for raw in operations_raw:
            if not isinstance(raw, Mapping):
                _fail("invalid_edit_operation", "edit operation must be a mapping")
            operation = str(raw.get("operation", "")).upper()
            content = _decode_b64(raw["content_b64"]) if "content_b64" in raw else None
            try:
                mode = int(raw.get("mode", 0o644))
            except (TypeError, ValueError) as exc:
                raise EngineeringLoopError("invalid_edit_mode", "mode must be an integer") from exc
            item = EditOperation(operation=operation, path=raw.get("path"), source_path=raw.get("source_path"), content=content, mode=mode)
            for path in (item.path, item.source_path):
                if path is None:
                    continue
                key = path.casefold()
                if key in folded:
                    _fail("case_collision", "edit paths collide on a case-insensitive filesystem")
                folded.add(key)
            total_bytes += len(content or b"")
            operations.append(item)
        if total_bytes > MAX_EDIT_BYTES:
            _fail("edit_budget_exceeded", "total edit bytes exceed the bounded budget")
        budget = document.get("budget", {})
        if not isinstance(budget, Mapping):
            _fail("invalid_edit_budget", "budget must be a mapping")
        try:
            max_operations = int(budget.get("max_operations", MAX_EDIT_OPERATIONS))
            max_bytes = int(budget.get("max_bytes", MAX_EDIT_BYTES))
        except (TypeError, ValueError) as exc:
            raise EngineeringLoopError("invalid_edit_budget", "edit budget values must be integers") from exc
        if max_operations < len(operations) or max_bytes < total_bytes:
            _fail("edit_budget_exceeded", "edit artifact exceeds its declared budget")
        payload = {
            "schema": EDIT_SCHEMA,
            "base_view_id": _text(document.get("base_view_id"), "base_view_id"),
            "expected_tree_digest": _text(document.get("expected_tree_digest"), "expected_tree_digest"),
            "task_id": _text(document.get("task_id"), "task_id"),
            "work_id": _text(document.get("work_id"), "work_id"),
            "run_id": _text(document.get("run_id"), "run_id"),
            "lease_id": _text(document.get("lease_id"), "lease_id"),
            "fence": document.get("fence"),
            "candidate_id": _text(document.get("candidate_id"), "candidate_id"),
            "workspace_generation": _text(document.get("workspace_generation"), "workspace_generation"),
            "operations": [item.as_dict() for item in operations],
            "budget": dict(budget),
        }
        if not isinstance(payload["fence"], int) or isinstance(payload["fence"], bool) or payload["fence"] < 1:
            _fail("invalid_fence", "fence must be a positive integer")
        computed = semantic_digest(payload)
        declared = document.get("artifact_digest")
        if declared is not None and declared != computed:
            _fail("edit_artifact_digest_mismatch", "artifact_digest does not match canonical edit bytes")
        return cls(EDIT_SCHEMA, payload["base_view_id"], payload["expected_tree_digest"], payload["task_id"], payload["work_id"], payload["run_id"], payload["lease_id"], payload["fence"], payload["candidate_id"], payload["workspace_generation"], tuple(operations), dict(budget), computed)


@dataclass(frozen=True)
class ValidationCommand:
    checker_id: str
    checker_version: str
    argv: tuple[str, ...]
    cwd: str = "."
    timeout_seconds: float = 30.0
    max_stdout_bytes: int = 64 * 1024
    max_stderr_bytes: int = 64 * 1024
    environment: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.argv or any(not isinstance(item, str) or not item or "\x00" in item for item in self.argv):
            _fail("invalid_validation_command", "validation argv must be a non-empty sequence")
        if self.timeout_seconds <= 0 or self.timeout_seconds > MAX_VALIDATION_TIMEOUT:
            _fail("invalid_validation_budget", "validation timeout is outside the bounded range")
        if self.max_stdout_bytes <= 0 or self.max_stdout_bytes > MAX_VALIDATION_OUTPUT or self.max_stderr_bytes <= 0 or self.max_stderr_bytes > MAX_VALIDATION_OUTPUT:
            _fail("invalid_validation_budget", "validation output budget is outside the bounded range")
        _path(self.cwd) if self.cwd != "." else None

    @property
    def checker_code_digest(self) -> str:
        return semantic_digest({"schema": VALIDATION_SCHEMA, "checker_id": self.checker_id, "checker_version": self.checker_version, "argv": list(self.argv)})


@dataclass(frozen=True)
class ValidationPolicy:
    allowed_argv: tuple[tuple[str, ...], ...]
    allowed_cwd: str = "."


@dataclass(frozen=True)
class ValidationResult:
    schema: str
    checker_id: str
    checker_version: str
    argv: tuple[str, ...]
    cwd: str
    status: Literal["PASS", "FAIL", "INCONCLUSIVE"]
    returncode: int | None
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool
    environment_fingerprint: str
    started_at: float
    finished_at: float
    error_code: str | None = None

    @property
    def duration_seconds(self) -> float:
        return max(0.0, self.finished_at - self.started_at)

    @property
    def feedback(self) -> str:
        """Bounded checker feedback for the next model iteration."""

        return (self.stderr or self.stdout).decode("utf-8", errors="replace")[:MAX_VALIDATION_OUTPUT]


class ValidationRunner:
    """Run only exact allowlisted checker argv inside the Candidate workspace."""

    def __init__(self, policy: ValidationPolicy) -> None:
        self.policy = policy

    def run(self, command: ValidationCommand, workspace: str | Path) -> ValidationResult:
        argv = tuple(command.argv)
        if argv not in self.policy.allowed_argv:
            _fail("validation_command_not_allowed", "checker argv is not in the exact allowlist")
        if command.cwd != self.policy.allowed_cwd:
            _fail("validation_cwd_not_allowed", "checker cwd is not in the exact allowlist")
        root = Path(workspace).absolute()
        cwd = (root / command.cwd).absolute()
        try:
            if os.path.commonpath((os.path.normcase(str(root)), os.path.normcase(str(cwd)))) != os.path.normcase(str(root)):
                _fail("validation_cwd_escape", "checker cwd escapes the Candidate workspace")
        except ValueError:
            _fail("validation_cwd_escape", "checker cwd is on a different volume")
        if not cwd.is_dir():
            _fail("validation_cwd_missing", "checker cwd does not exist")
        env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR", "PATH", "PATHEXT") if key in os.environ}
        env.update({"PYTHONNOUSERSITE": "1", "PYTHONHASHSEED": "0", "BDB_VALIDATION_ONLY": "1"})
        env.update({str(key): str(value) for key, value in command.environment.items()})
        environment_fingerprint = semantic_digest({"schema": "bdb-validation-environment-v1", "python": sys.version, "executable": sys.executable, "environment": {key: env.get(key, "") for key in sorted(env)}})
        started = time.time()
        returncode: int | None = None
        timed_out = False
        error_code: str | None = None
        with tempfile.TemporaryFile() as out_file, tempfile.TemporaryFile() as err_file:
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
            try:
                process = subprocess.Popen(list(argv), cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=out_file, stderr=err_file, shell=False, creationflags=creationflags, start_new_session=os.name != "nt")
                try:
                    returncode = process.wait(timeout=command.timeout_seconds)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    error_code = "validation_timeout"
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, shell=False)
                    else:
                        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    process.wait(timeout=5)
            except OSError:
                error_code = "validation_exec_failed"
            out_file.seek(0)
            err_file.seek(0)
            stdout = out_file.read(command.max_stdout_bytes + 1)
            stderr = err_file.read(command.max_stderr_bytes + 1)
        stdout_truncated = len(stdout) > command.max_stdout_bytes
        stderr_truncated = len(stderr) > command.max_stderr_bytes
        stdout = stdout[: command.max_stdout_bytes]
        stderr = stderr[: command.max_stderr_bytes]
        finished = time.time()
        if timed_out or error_code:
            status: Literal["PASS", "FAIL", "INCONCLUSIVE"] = "INCONCLUSIVE"
        elif stdout_truncated or stderr_truncated:
            status = "INCONCLUSIVE"
            error_code = "validation_output_limit"
        else:
            status = "PASS" if returncode == 0 else "FAIL"
        return ValidationResult(VALIDATION_SCHEMA, command.checker_id, command.checker_version, argv, command.cwd, status, returncode, stdout, stderr, stdout_truncated, stderr_truncated, timed_out, environment_fingerprint, started, finished, error_code)


@dataclass(frozen=True)
class ValidationRunRecord:
    validation_id: str
    batch_id: str
    candidate_id: str
    status: str
    evidence_id: str | None
    result: ValidationResult


@dataclass(frozen=True)
class EngineeringIteration:
    batch: EditBatch
    candidate: CandidateRecord
    validation: ValidationRunRecord


@dataclass(frozen=True)
class EngineeringFinalization:
    candidate: CandidateRecord
    candidate_view: CandidateRepoView
    validation: ValidationRunRecord
    evaluation: EvaluationRecord | None
    publication: PublicationRecord | None


class EditorPort:
    """Typed boundary between model-authored edit artifacts and CandidateStore."""

    def __init__(self, candidate_store: CandidateStore, *, evidence_store: EvidenceStore | None = None) -> None:
        self.candidate_store = candidate_store
        self.evidence_store = evidence_store
        self.connection = candidate_store._connection
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS p1_edit_batches (
              batch_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL,
              task_id TEXT NOT NULL, work_id TEXT NOT NULL, run_id TEXT NOT NULL,
              base_view_id TEXT NOT NULL, expected_tree_digest TEXT NOT NULL,
              artifact_digest TEXT NOT NULL, operations_json BLOB NOT NULL,
              workspace_generation TEXT NOT NULL, status TEXT NOT NULL,
              effect_certainty TEXT NOT NULL, created_at REAL NOT NULL,
              updated_at REAL NOT NULL,
              UNIQUE(candidate_id,batch_id)
            );
            CREATE TABLE IF NOT EXISTS p1_validation_runs (
              validation_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES p1_edit_batches(batch_id),
              candidate_id TEXT NOT NULL, checker_id TEXT NOT NULL,
              checker_version TEXT NOT NULL, command_json BLOB NOT NULL,
              environment_fingerprint TEXT NOT NULL, status TEXT NOT NULL,
              returncode INTEGER, stdout_ref_json BLOB, stderr_ref_json BLOB,
              stdout_digest TEXT, stderr_digest TEXT, evidence_id TEXT,
              started_at REAL NOT NULL, finished_at REAL NOT NULL,
              error_code TEXT
            );
            CREATE INDEX IF NOT EXISTS p1_edit_batches_by_candidate ON p1_edit_batches(candidate_id,created_at);
            CREATE INDEX IF NOT EXISTS p1_validation_runs_by_batch ON p1_validation_runs(batch_id,started_at);
            """
        )
        commit_control_write(self.connection)

    @staticmethod
    def _batch_id(batch: EditBatch) -> str:
        return semantic_digest({"schema": EDIT_BATCH_SCHEMA, "artifact_digest": batch.artifact_digest, "candidate_id": batch.candidate_id, "run_id": batch.run_id})

    def _current_tree_digest(self, workspace: Path, base_view: CommittedRepoView) -> str:
        return self.candidate_store._tree_digest(self.candidate_store._workspace_entries(workspace, object_format=base_view.object_format))

    def _validate_batch(self, batch: EditBatch, base_view: CommittedRepoView, workspace: Path) -> None:
        base_view.validate_integrity()
        if batch.base_view_id != base_view.view_id:
            _fail("repo_view_mismatch", "edit artifact is bound to a different exact Committed RepoView")
        if batch.workspace_generation != self.candidate_store.generation:
            _fail("workspace_generation_mismatch", "edit artifact workspace generation is stale")
        query = self.candidate_store.work_kernel.query(batch.work_id) if self.candidate_store.work_kernel is not None else None
        if query is None or query.active_run is None or query.active_run.run_id != batch.run_id:
            _fail("run_binding_mismatch", "edit artifact is not owned by the active canonical Run")
        current_tree = self._current_tree_digest(workspace, base_view)
        if current_tree != batch.expected_tree_digest:
            _fail("tree_precondition_mismatch", "edit artifact expected tree does not match the isolated workspace", details={"expected": batch.expected_tree_digest, "actual": current_tree})

    def _persist_batch(self, batch: EditBatch, *, status: str, effect_certainty: str) -> str:
        batch_id = self._batch_id(batch)
        now = time.time()
        operations_json = canonical_json_bytes([item.as_dict() for item in batch.operations])
        existing = self.connection.execute("SELECT artifact_digest,candidate_id FROM p1_edit_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if existing is not None:
            if str(existing[0]) != batch.artifact_digest or str(existing[1]) != batch.candidate_id:
                _fail("edit_batch_conflict", "batch identity is already bound to different artifact bytes")
            self.connection.execute("UPDATE p1_edit_batches SET status=?,effect_certainty=?,updated_at=? WHERE batch_id=?", (status, effect_certainty, now, batch_id))
            commit_control_write(self.connection)
            return batch_id
        self.connection.execute("INSERT INTO p1_edit_batches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (batch_id, batch.candidate_id, batch.task_id, batch.work_id, batch.run_id, batch.base_view_id, batch.expected_tree_digest, batch.artifact_digest, operations_json, batch.workspace_generation, status, effect_certainty, now, now))
        commit_control_write(self.connection)
        return batch_id

    def prepare_batch(self, batch: EditBatch, *, base_view: CommittedRepoView, workspace: str | Path, desired_files: Mapping[str, bytes | None] | None = None) -> CandidateRecord:
        workspace_path = Path(workspace).absolute()
        self._validate_batch(batch, base_view, workspace_path)
        existing = self.candidate_store.get(batch.candidate_id)
        if existing is not None:
            existing_batch = self.connection.execute("SELECT artifact_digest FROM p1_edit_batches WHERE batch_id=?", (self._batch_id(batch),)).fetchone()
            if existing_batch is not None and str(existing_batch[0]) == batch.artifact_digest:
                status_row = self.connection.execute("SELECT status FROM p1_edit_batches WHERE batch_id=?", (self._batch_id(batch),)).fetchone()
                status = str(status_row[0]) if status_row is not None else ""
                # A concurrent/retried Browser delivery can observe the
                # durable batch row before the Candidate transition is
                # visible on its connection.  Only a completed batch is a
                # replay; a PREPARED batch must still reconcile/apply its
                # exact plan, even if the Candidate row is momentarily
                # OBSERVED from the preceding iteration.
                if status in {"OBSERVED", "SEALED"}:
                    return existing
                if status != "PREPARED":
                    _fail("edit_batch_state_conflict", "existing edit batch has an unsupported lifecycle status")
                if existing.state == "PREPARED":
                    return existing
                if desired_files is None:
                    _fail("desired_state_required", "a prepared repeated edit batch requires the accumulated desired state")
                record = self.candidate_store.reprepare_desired(candidate_id=batch.candidate_id, base_view=base_view, workspace_root=workspace_path, desired_files=desired_files)
                self._persist_batch(batch, status="PREPARED", effect_certainty=record.effect_certainty)
                return record
        if existing is None:
            record = self.candidate_store.prepare_operations(candidate_id=batch.candidate_id, work_id=batch.work_id, task_id=batch.task_id, lease_id=batch.lease_id, fence=batch.fence, base_view=base_view, workspace_root=workspace_path, operations=[item.candidate_mapping() for item in batch.operations])
        else:
            if existing.state in {"SEALED", "INVALIDATED"}:
                _fail("candidate_state_conflict", "sealed Candidate cannot accept another edit batch")
            if desired_files is None:
                _fail("desired_state_required", "a subsequent edit batch must provide the accumulated desired state")
            record = self.candidate_store.reprepare_desired(candidate_id=batch.candidate_id, base_view=base_view, workspace_root=workspace_path, desired_files=desired_files)
        self._persist_batch(batch, status="PREPARED", effect_certainty=record.effect_certainty)
        return record

    def apply_batch(self, batch: EditBatch) -> CandidateRecord:
        existing = self.candidate_store.get(batch.candidate_id)
        if existing is not None and existing.state in {"OBSERVED", "SEALED"}:
            row = self.connection.execute("SELECT artifact_digest,candidate_id,status FROM p1_edit_batches WHERE batch_id=?", (self._batch_id(batch),)).fetchone()
            if row is not None and (str(row[0]) != batch.artifact_digest or str(row[1]) != batch.candidate_id):
                _fail("edit_batch_conflict", "batch identity is already bound to different artifact bytes")
            # Candidate state alone is insufficient during iterative delivery:
            # a new batch may already be durably PREPARED while the preceding
            # OBSERVED snapshot is still visible to this connection.  Only a
            # completed batch may take the replay/no-op path.
            if row is None or str(row[2]) in {"OBSERVED", "SEALED"}:
                self._persist_batch(batch, status=existing.state, effect_certainty=existing.effect_certainty)
                return existing
        try:
            record = self.candidate_store.apply(batch.candidate_id)
        except CandidateError:
            current = self.candidate_store.get(batch.candidate_id)
            if current is not None:
                self._persist_batch(batch, status=current.state, effect_certainty=current.effect_certainty)
            raise
        self._persist_batch(batch, status=record.state, effect_certainty=record.effect_certainty)
        return record

    def replay_batch(self, batch: EditBatch, *, base_view: CommittedRepoView) -> dict[str, Any] | None:
        """Return the canonical result for an already completed exact batch.

        Browser-local processed-artifact markers are only a cache.  If they are
        lost, the immutable batch/candidate/validation rows provide the replay
        identity and no second filesystem apply or evidence observation is
        created.  A merely PREPARED batch is intentionally left to the normal
        reconciliation path, because its effect boundary may not yet have
        been crossed.
        """
        batch_id = self._batch_id(batch)
        row = self.connection.execute(
            "SELECT artifact_digest,candidate_id,status FROM p1_edit_batches WHERE batch_id=?",
            (batch_id,),
        ).fetchone()
        if row is None:
            return None
        if str(row[0]) != batch.artifact_digest or str(row[1]) != batch.candidate_id:
            _fail("edit_batch_conflict", "replayed artifact identity differs from the canonical batch")
        candidate = self.candidate_store.get(batch.candidate_id)
        if candidate is None:
            _fail("candidate_missing", "canonical edit batch has no Candidate record")
        if candidate.state == "PREPARED":
            return None
        validation = self.connection.execute(
            "SELECT validation_id,status,evidence_id FROM p1_validation_runs WHERE batch_id=? ORDER BY finished_at DESC LIMIT 1",
            (batch_id,),
        ).fetchone()
        if validation is None:
            _fail("engineering_reconciliation_required", "completed edit batch has no canonical validation record")
        workspace = Path(candidate.workspace_root)
        if not workspace.is_dir():
            _fail("candidate_workspace_missing", "canonical edit batch workspace is unavailable for replay")
        current_tree = self._current_tree_digest(workspace, base_view)
        return {
            "status": "REPLAYED",
            "batch_id": batch_id,
            "validation_status": str(validation[1]),
            "validation_id": str(validation[0]),
            "validation_evidence_id": str(validation[2]) if validation[2] is not None else None,
            "candidate_id": candidate.candidate_id,
            "candidate_state": candidate.state,
            "candidate_workspace": candidate.workspace_root,
            "current_tree_digest": current_tree,
            "expected_tree_digest": batch.expected_tree_digest,
            "ready_to_seal": candidate.state == "OBSERVED" and str(validation[1]) == "PASS",
        }

    def _persist_validation(self, batch_id: str, candidate_id: str, command: ValidationCommand, result: ValidationResult, *, evidence_id: str | None, stdout_ref: Mapping[str, Any], stderr_ref: Mapping[str, Any]) -> str:
        identity = {"schema": VALIDATION_SCHEMA, "batch_id": batch_id, "candidate_id": candidate_id, "checker_id": command.checker_id, "checker_version": command.checker_version, "argv": list(command.argv), "environment_fingerprint": result.environment_fingerprint, "status": result.status, "returncode": result.returncode, "stdout_digest": _digest(result.stdout), "stderr_digest": _digest(result.stderr), "error_code": result.error_code}
        validation_id = semantic_digest(identity)
        existing = self.connection.execute("SELECT validation_id FROM p1_validation_runs WHERE validation_id=?", (validation_id,)).fetchone()
        if existing is None:
            self.connection.execute("INSERT INTO p1_validation_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (validation_id, batch_id, candidate_id, command.checker_id, command.checker_version, canonical_json_bytes({"argv": list(command.argv), "cwd": command.cwd, "timeout_seconds": command.timeout_seconds}), result.environment_fingerprint, result.status, result.returncode, canonical_json_bytes(stdout_ref), canonical_json_bytes(stderr_ref), _digest(result.stdout), _digest(result.stderr), evidence_id, result.started_at, result.finished_at, result.error_code))
            commit_control_write(self.connection)
        return validation_id

    def record_validation(self, *, batch: EditBatch, result: ValidationResult, candidate_view: CandidateRepoView | None = None) -> ValidationRunRecord:
        batch_id = self._batch_id(batch)
        content_store = self.candidate_store.content_store
        stdout_ref = make_content_ref("text/plain", "bdb-vnext-validation-stdout-v1", result.stdout)
        stderr_ref = make_content_ref("text/plain", "bdb-vnext-validation-stderr-v1", result.stderr)
        content_store.publish(stdout_ref, result.stdout)
        content_store.publish(stderr_ref, result.stderr)
        evidence_id: str | None = None
        if self.evidence_store is not None:
            subject_kind = "CANDIDATE" if candidate_view is not None else "CANDIDATE_WORKSPACE"
            subject = {"schema": EDITOR_PORT_SCHEMA, "candidate_id": batch.candidate_id, "batch_id": batch_id, "workspace_generation": batch.workspace_generation}
            if candidate_view is not None:
                subject.update({"view_id": candidate_view.view_id, "candidate_tree_digest": candidate_view.candidate_tree_digest})
            raw = {"schema": VALIDATION_SCHEMA, "command": {"checker_id": result.checker_id, "checker_version": result.checker_version, "argv": list(result.argv), "cwd": result.cwd}, "status": result.status, "returncode": result.returncode, "stdout_ref": stdout_ref.as_dict(), "stderr_ref": stderr_ref.as_dict(), "stdout_digest": _digest(result.stdout), "stderr_digest": _digest(result.stderr), "timing": {"started_at": result.started_at, "finished_at": result.finished_at, "duration_seconds": result.duration_seconds}, "environment_fingerprint": result.environment_fingerprint, "error_code": result.error_code}
            evidence = self.evidence_store.record_observation(request_id="p1-validation:" + semantic_digest({"batch_id": batch_id, "stdout": _digest(result.stdout), "stderr": _digest(result.stderr), "candidate_view_id": candidate_view.view_id if candidate_view else None}), primary_subject_kind=subject_kind, primary_subject_identity=subject, candidate_view_id=candidate_view.view_id if candidate_view else None, raw_observation=raw, checker_id=result.checker_id, checker_version=result.checker_version, checker_code_digest=ValidationCommand(result.checker_id, result.checker_version, result.argv).checker_code_digest, environment={"fingerprint": result.environment_fingerprint}, observation_started_at=str(result.started_at), observation_finished_at=str(result.finished_at), completeness="COMPLETE" if result.status != "INCONCLUSIVE" else "PARTIAL", applicability="APPLICABLE" if candidate_view is not None and result.status in {"PASS", "FAIL"} else "INCONCLUSIVE", status=result.status)
            evidence_id = evidence.evidence_id
        validation_id = self._persist_validation(batch_id, batch.candidate_id, ValidationCommand(result.checker_id, result.checker_version, result.argv, result.cwd), result, evidence_id=evidence_id, stdout_ref=stdout_ref.as_dict(), stderr_ref=stderr_ref.as_dict())
        return ValidationRunRecord(validation_id, batch_id, batch.candidate_id, result.status, evidence_id, result)

    def recover_candidate(self, candidate_id: str, *, base_view: CommittedRepoView) -> CandidateRecord:
        """Read/reconcile an interrupted loop without blind reapplication."""

        record = self.candidate_store.get(candidate_id)
        if record is None:
            _fail("candidate_missing", "engineering Candidate does not exist")
        if record.state == "SEALED":
            return self.candidate_store.verify_current_applicability(candidate_id)
        observed = self.candidate_store.observe(candidate_id)
        if observed.state in {"DIVERGED", "UNKNOWN"}:
            _fail("engineering_reconciliation_required", "interrupted edit requires exact observation before retry")
        return observed


class EngineeringLoop:
    """Small iterative controller: edit → validate → feedback → edit."""

    def __init__(self, editor: EditorPort, runner: ValidationRunner, *, evidence_store: EvidenceStore | None = None, publication_store: PublicationStore | None = None) -> None:
        self.editor = editor
        self.runner = runner
        self.evidence_store = evidence_store or editor.evidence_store
        self.publication_store = publication_store
        self._desired: dict[str, bytes | None] = {}
        self._base_view_id: str | None = None

    def _apply_to_desired(self, batch: EditBatch, base_view: CommittedRepoView) -> None:
        if self._base_view_id is None:
            self._base_view_id = base_view.view_id
        elif self._base_view_id != base_view.view_id:
            _fail("repo_view_mismatch", "engineering loop cannot change exact base RepoView")
        for operation in batch.operations:
            if operation.operation == "MODIFY":
                self._desired[operation.path] = operation.content
            elif operation.operation == "CREATE":
                self._desired[operation.path] = operation.content
            elif operation.operation == "DELETE":
                self._desired[operation.path] = None
            elif operation.operation == "RENAME":
                if operation.source_path is None:
                    _fail("invalid_rename", "RENAME source is required")
                source_content = self._desired.get(operation.source_path)
                if source_content is None and operation.source_path in self._desired:
                    _fail("rename_source_missing", "RENAME source was already deleted")
                if source_content is None:
                    source_content = base_view.read_bytes(operation.source_path)
                self._desired[operation.source_path] = None
                self._desired[operation.path] = source_content

    def iteration(self, batch: EditBatch, *, base_view: CommittedRepoView, workspace: str | Path, command: ValidationCommand) -> EngineeringIteration:
        self._apply_to_desired(batch, base_view)
        record = self.editor.prepare_batch(batch, base_view=base_view, workspace=workspace, desired_files=self._desired if self.editor.candidate_store.get(batch.candidate_id) is not None else None)
        applied = self.editor.apply_batch(batch)
        result = self.runner.run(command, workspace)
        validation = self.editor.record_validation(batch=batch, result=result)
        return EngineeringIteration(batch, applied, validation)

    @staticmethod
    def feedback(validation: ValidationRunRecord) -> str:
        return validation.result.feedback

    def finalize(self, batch: EditBatch, *, base_view: CommittedRepoView, workspace: str | Path, command: ValidationCommand, publication: Mapping[str, Any] | None = None) -> EngineeringFinalization:
        current = self.editor.candidate_store.get(batch.candidate_id)
        if current is None or current.state != "OBSERVED":
            _fail("candidate_not_observed", "Candidate must be exactly observed before seal")
        sealed, candidate_view = self.editor.candidate_store.seal(batch.candidate_id, base_view=base_view)
        result = self.runner.run(command, workspace)
        validation = self.editor.record_validation(batch=batch, result=result, candidate_view=candidate_view)
        evaluation: EvaluationRecord | None = None
        publication_record: PublicationRecord | None = None
        if self.evidence_store is not None and validation.evidence_id is not None:
            evaluation = self.evidence_store.evaluate(evidence_id=validation.evidence_id, evaluator_id="bdb-vnext-engineering-loop-checker", evaluator_version="v1", evaluator_code_digest=command.checker_code_digest, config_digest=result.environment_fingerprint, result=result.status, applicability="APPLICABLE" if result.status in {"PASS", "FAIL"} else "INCONCLUSIVE", detail={"schema": VALIDATION_SCHEMA, "returncode": result.returncode, "error_code": result.error_code})
        if publication is not None:
            if self.publication_store is None:
                _fail("publication_unavailable", "publication requested without the canonical PublicationStore")
            if evaluation is None or validation.evidence_id is None:
                _fail("publication_evidence_missing", "publication requires final Candidate-bound evidence")
            current_disposition = self.evidence_store.current_disposition(validation.evidence_id) if self.evidence_store else None
            if current_disposition is None:
                _fail("publication_disposition_missing", "publication requires a current evidence disposition")
            publication_record = self.publication_store.publish(task_id=batch.task_id, work_id=batch.work_id, intent_revision_id=str(publication["intent_revision_id"]), request_id=str(publication["request_id"]), result_payload={"schema": "bdb-vnext-engineering-result-v1", "candidate_view_id": candidate_view.view_id, "validation_id": validation.validation_id, "evidence_id": validation.evidence_id, "evaluation_id": evaluation.evaluation_id, "disposition_id": current_disposition.disposition_id, "status": result.status}, consumer_id=str(publication["consumer_id"]), consumer_kind=str(publication["consumer_kind"]), conversation_id=publication.get("conversation_id"), profile_id=publication.get("profile_id"), candidate_id=batch.candidate_id, candidate_view_id=candidate_view.view_id, evidence_id=validation.evidence_id, evaluation_id=evaluation.evaluation_id, disposition_id=current_disposition.disposition_id, generation=publication.get("generation"))
        return EngineeringFinalization(sealed, candidate_view, validation, evaluation, publication_record)


def parse_edit_artifact(document: Mapping[str, Any]) -> EditBatch:
    return EditBatch.from_mapping(document)


__all__ = [
    "EDIT_SCHEMA", "EDITOR_PORT_SCHEMA", "EDIT_BATCH_SCHEMA", "VALIDATION_SCHEMA", "EditOperation", "EditBatch", "EngineeringLoopError", "ValidationCommand", "ValidationPolicy", "ValidationResult", "ValidationRunner", "ValidationRunRecord", "EditorPort", "EngineeringIteration", "EngineeringFinalization", "EngineeringLoop", "parse_edit_artifact",
]

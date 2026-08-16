"""M7b checkout synchronization as an effect separate from M7a ref truth.

M7a may already have moved an isolated ``refs/bdb-vnext/...`` ref while the
attached checkout index/worktree still represent the old commit.  M7b owns
only that mechanical index/worktree synchronization.  It never changes the
source promotion ref, never pushes, never resets broadly, and never rewrites
M7a promotion certainty.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, NoReturn, Sequence

from bdb_shared.evidence import canonical_json_bytes, semantic_digest
from bdb_vnext.control_store import (
    ControlStoreError,
    begin_control_write,
    commit_control_write,
    rollback_control_write,
)
from bdb_vnext.repo_view import RepositoryResource, RepoViewError


M7B_EFFECT_SCHEMA = "bdb-vnext-m7b-checkout-effect-v1"
M7B_QUERY_SCHEMA = "bdb-vnext-m7b-checkout-effect-query-v1"
M7B_RESOURCE_SCHEMA = "bdb-vnext-m7b-checkout-resource-v1"
M7B_AUTHORITY = "devmaster.bdb.vnext.checkout-sync-adapter"

EffectState = Literal["PREPARED", "POSSIBLE", "AFTER", "DIVERGED", "UNKNOWN"]
EffectCertainty = Literal["BEFORE", "POSSIBLE", "AFTER", "DIVERGED", "AMBIGUOUS"]
SafeNextAction = Literal[
    "SAFE_TO_APPLY",
    "OBSERVE_REQUIRED",
    "COMPLETE_ALLOWED",
    "MANUAL_RECONCILIATION",
]

_HEX = frozenset("0123456789abcdef")


class M7bError(RuntimeError):
    """Typed fail-closed checkout synchronization failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def _fail(
    code: str,
    message: str,
    *,
    details: Mapping[str, Any] | None = None,
) -> NoReturn:
    raise M7bError(code, message, details=details)


def _text(value: object, field: str, *, maximum: int = 2048) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or "\x00" in value
        or any(ord(char) < 32 for char in value)
    ):
        _fail("invalid_checkout_effect_input", f"{field} must be bounded non-empty text")
    return value


def _oid(value: object, *, object_format: str, field: str) -> str:
    text = _text(value, field, maximum=128).lower()
    length = 40 if object_format == "sha1" else 64 if object_format == "sha256" else 0
    if length == 0:
        _fail("unsupported_object_format", "M7b supports only Git sha1/sha256 repositories")
    if len(text) != length or any(char not in _HEX for char in text) or set(text) == {"0"}:
        _fail("invalid_git_oid", f"{field} is not an exact {object_format} OID")
    return text


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _git_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    environment.update(
        {
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def _resource_key(repository_identity_digest: str, checkout_root: str) -> str:
    digest = semantic_digest(
        {
            "schema": M7B_RESOURCE_SCHEMA,
            "repository_identity_digest": repository_identity_digest,
            "checkout_root": checkout_root,
        }
    )
    return "git-checkout:" + digest.split(":", 1)[1]


@dataclass(frozen=True)
class CheckoutObservation:
    head_ref: str | None
    head_oid: str | None
    index_tree_oid: str | None
    tracked_worktree_matches_index: bool | None
    untracked_paths: tuple[str, ...]
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "head_ref": self.head_ref,
            "head_oid": self.head_oid,
            "index_tree_oid": self.index_tree_oid,
            "tracked_worktree_matches_index": self.tracked_worktree_matches_index,
            "untracked_paths": list(self.untracked_paths),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class CheckoutSyncEffect:
    work_id: str
    task_id: str
    run_id: str
    effect_id: str
    source_promotion_effect_id: str
    source_ref: str
    source_commit_oid: str
    old_commit_oid: str
    old_tree_oid: str
    new_tree_oid: str
    repository_id: str
    repository_identity_digest: str
    checkout_root: str
    object_format: str
    changed_paths: tuple[tuple[str, str], ...]
    resource_key: str
    state: EffectState
    effect_certainty: EffectCertainty
    observation: Mapping[str, Any]
    lease_id: str
    fence: int
    created_at: str
    updated_at: str

    @property
    def safe_next_action(self) -> SafeNextAction:
        if self.effect_certainty == "BEFORE":
            return "SAFE_TO_APPLY"
        if self.effect_certainty == "POSSIBLE":
            return "OBSERVE_REQUIRED"
        if self.effect_certainty == "AFTER":
            return "COMPLETE_ALLOWED"
        return "MANUAL_RECONCILIATION"

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": M7B_QUERY_SCHEMA,
            "authority": M7B_AUTHORITY,
            "mode": "BUILD_ONLY",
            "work_id": self.work_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "effect_id": self.effect_id,
            "source_promotion_effect_id": self.source_promotion_effect_id,
            "source_ref": self.source_ref,
            "source_commit_oid": self.source_commit_oid,
            "old_commit_oid": self.old_commit_oid,
            "old_tree_oid": self.old_tree_oid,
            "new_tree_oid": self.new_tree_oid,
            "repository_id": self.repository_id,
            "repository_identity_digest": self.repository_identity_digest,
            "checkout_root": self.checkout_root,
            "object_format": self.object_format,
            "changed_paths": [list(item) for item in self.changed_paths],
            "resource_key": self.resource_key,
            "state": self.state,
            "effect_certainty": self.effect_certainty,
            "safe_next_action": self.safe_next_action,
            "observation": dict(self.observation),
            "lease_id": self.lease_id,
            "fence": self.fence,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        payload["query_digest"] = semantic_digest(payload)
        return payload


class CheckoutSyncAdapter:
    """Separate Work/Run authority for synchronizing one exact local checkout."""

    def __init__(
        self,
        *,
        m7a_adapter: Any,
        work_kernel: Any,
        git_timeout_seconds: float = 30.0,
    ) -> None:
        if m7a_adapter is None or work_kernel is None:
            _fail("m7b_dependencies_required", "M7b requires the M7a reader and canonical Work Kernel")
        candidate_store = getattr(m7a_adapter, "candidate_store", None)
        if candidate_store is None or getattr(candidate_store, "_connection", None) is None:
            _fail("m7b_dependencies_required", "M7b requires the unified vNext Control DB")
        self.m7a_adapter = m7a_adapter
        self.work_kernel = work_kernel
        self._connection = candidate_store._connection
        self.git_timeout_seconds = float(git_timeout_seconds)
        if not 0 < self.git_timeout_seconds <= 300:
            _fail("invalid_git_timeout", "M7b Git timeout must be positive and bounded")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS m7b_checkout_effects (
              work_id TEXT PRIMARY KEY,
              task_id TEXT NOT NULL,
              run_id TEXT NOT NULL,
              effect_id TEXT NOT NULL UNIQUE,
              source_promotion_effect_id TEXT NOT NULL,
              source_ref TEXT NOT NULL,
              source_commit_oid TEXT NOT NULL,
              old_commit_oid TEXT NOT NULL,
              old_tree_oid TEXT NOT NULL,
              new_tree_oid TEXT NOT NULL,
              repository_id TEXT NOT NULL,
              repository_identity_digest TEXT NOT NULL,
              checkout_root TEXT NOT NULL,
              object_format TEXT NOT NULL,
              changed_paths_json BLOB NOT NULL,
              resource_key TEXT NOT NULL UNIQUE,
              state TEXT NOT NULL,
              effect_certainty TEXT NOT NULL,
              observation_json BLOB NOT NULL,
              lease_id TEXT NOT NULL,
              fence INTEGER NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS m7b_checkout_by_source
              ON m7b_checkout_effects(source_promotion_effect_id);
            """
        )

    def _begin(self) -> None:
        try:
            begin_control_write(self._connection)
        except ControlStoreError as exc:
            _fail(exc.code, str(exc))

    def _commit(self) -> None:
        try:
            commit_control_write(self._connection)
        except ControlStoreError as exc:
            _fail(exc.code, str(exc))

    def _rollback(self) -> None:
        try:
            rollback_control_write(self._connection)
        except ControlStoreError as exc:
            _fail(exc.code, str(exc))

    def _git(
        self,
        checkout_root: str | Path,
        args: Iterable[str],
        *,
        check: bool = True,
        operation: str,
    ) -> subprocess.CompletedProcess[bytes]:
        command = ["git", "--no-replace-objects", "-C", str(checkout_root), *list(args)]
        try:
            completed = subprocess.run(
                command,
                shell=False,
                capture_output=True,
                timeout=self.git_timeout_seconds,
                check=False,
                env=_git_environment(),
            )
        except FileNotFoundError as exc:
            raise M7bError("git_unavailable", "Git executable is unavailable") from exc
        except subprocess.TimeoutExpired as exc:
            raise M7bError(
                "checkout_effect_unknown",
                f"Git operation timed out during {operation}",
                details={"operation": operation},
            ) from exc
        except OSError as exc:
            raise M7bError(
                "checkout_effect_unknown",
                f"Git operation could not execute during {operation}",
                details={"operation": operation},
            ) from exc
        if check and completed.returncode != 0:
            _fail(
                "git_checkout_command_failed",
                f"Git command failed during {operation}",
                details={"operation": operation, "returncode": completed.returncode},
            )
        return completed

    def _source_promotion(self, effect_id: str) -> Any:
        effect_id = _text(effect_id, "source_promotion_effect_id", maximum=192)
        source = self.m7a_adapter.get(effect_id=effect_id)
        if source is None:
            _fail("source_promotion_missing", "M7b source M7a promotion effect does not exist")
        if str(source.state) != "AFTER" or str(source.effect_certainty) != "AFTER":
            _fail(
                "source_promotion_not_after",
                "checkout synchronization requires already-proven M7a promotion AFTER",
                details={
                    "source_state": str(source.state),
                    "source_effect_certainty": str(source.effect_certainty),
                },
            )
        return source

    def _repository(self, source: Any) -> RepositoryResource:
        try:
            repository = RepositoryResource.from_path(
                str(source.repository_root),
                repository_id=str(source.repository_id),
            )
        except RepoViewError as exc:
            raise M7bError(
                "checkout_repository_unavailable",
                "M7b checkout repository cannot be opened",
                details={"cause": exc.code},
            ) from exc
        if (
            repository.identity_digest != str(source.repository_identity_digest)
            or repository.object_format != str(source.object_format)
        ):
            _fail("repository_identity_mismatch", "M7b repository identity differs from M7a source")
        return repository

    def _tree_for_commit(self, repository: RepositoryResource, commit_oid: str) -> str:
        completed = self._git(
            repository.root,
            ["rev-parse", "--verify", f"{commit_oid}^{{tree}}"],
            operation="commit tree resolution",
        )
        return _oid(
            completed.stdout.decode("ascii", errors="strict").strip().lower(),
            object_format=repository.object_format,
            field="tree_oid",
        )

    def _changed_paths(
        self,
        repository: RepositoryResource,
        old_tree_oid: str,
        new_tree_oid: str,
    ) -> tuple[tuple[str, str], ...]:
        completed = self._git(
            repository.root,
            [
                "diff-tree",
                "--no-commit-id",
                "--name-status",
                "--no-renames",
                "-r",
                "-z",
                old_tree_oid,
                new_tree_oid,
            ],
            operation="checkout changed-path proof",
        )
        chunks = completed.stdout.split(b"\x00")
        if chunks and chunks[-1] == b"":
            chunks.pop()
        if len(chunks) % 2 != 0:
            _fail("checkout_diff_malformed", "Git changed-path proof is malformed")
        result: list[tuple[str, str]] = []
        for index in range(0, len(chunks), 2):
            status = chunks[index].decode("ascii", errors="strict")
            path = chunks[index + 1].decode("utf-8", errors="strict").replace("\\", "/")
            if status not in {"A", "D", "M", "T"}:
                _fail(
                    "checkout_diff_unsupported",
                    "M7b changed-path proof contains an unsupported status",
                    details={"status": status, "path": path},
                )
            if not path or path.startswith("/") or "\x00" in path:
                _fail("checkout_diff_malformed", "M7b changed-path proof contains an invalid path")
            result.append((status, path))
        return tuple(result)

    def _observe(self, record: CheckoutSyncEffect) -> CheckoutObservation:
        root = record.checkout_root
        symbolic = self._git(
            root,
            ["symbolic-ref", "-q", "HEAD"],
            check=False,
            operation="checkout HEAD symbolic-ref observation",
        )
        if symbolic.returncode == 0:
            head_ref = symbolic.stdout.decode("utf-8", errors="strict").strip()
        elif symbolic.returncode == 1:
            head_ref = None
        else:
            _fail(
                "checkout_observation_failed",
                "M7b could not observe checkout symbolic HEAD",
                details={"returncode": symbolic.returncode},
            )

        head = self._git(
            root,
            ["rev-parse", "--verify", "HEAD"],
            check=False,
            operation="checkout HEAD OID observation",
        )
        if head.returncode != 0:
            _fail("checkout_observation_failed", "M7b could not resolve checkout HEAD")
        head_oid = _oid(
            head.stdout.decode("ascii", errors="strict").strip().lower(),
            object_format=record.object_format,
            field="head_oid",
        )

        index = self._git(
            root,
            ["write-tree"],
            check=False,
            operation="checkout index tree observation",
        )
        if index.returncode != 0:
            return CheckoutObservation(
                head_ref=head_ref,
                head_oid=head_oid,
                index_tree_oid=None,
                tracked_worktree_matches_index=None,
                untracked_paths=(),
                reason="index_tree_unavailable",
            )
        index_tree_oid = _oid(
            index.stdout.decode("ascii", errors="strict").strip().lower(),
            object_format=record.object_format,
            field="index_tree_oid",
        )

        tracked = self._git(
            root,
            ["diff-files", "--quiet"],
            check=False,
            operation="tracked worktree observation",
        )
        if tracked.returncode == 0:
            tracked_matches = True
        elif tracked.returncode == 1:
            tracked_matches = False
        else:
            return CheckoutObservation(
                head_ref=head_ref,
                head_oid=head_oid,
                index_tree_oid=index_tree_oid,
                tracked_worktree_matches_index=None,
                untracked_paths=(),
                reason="tracked_worktree_unavailable",
            )

        untracked = self._git(
            root,
            ["ls-files", "--others", "--exclude-standard", "-z"],
            check=False,
            operation="untracked worktree observation",
        )
        if untracked.returncode != 0:
            return CheckoutObservation(
                head_ref=head_ref,
                head_oid=head_oid,
                index_tree_oid=index_tree_oid,
                tracked_worktree_matches_index=tracked_matches,
                untracked_paths=(),
                reason="untracked_worktree_unavailable",
            )
        untracked_paths = tuple(
            item.decode("utf-8", errors="strict").replace("\\", "/")
            for item in untracked.stdout.split(b"\x00")
            if item
        )
        return CheckoutObservation(
            head_ref=head_ref,
            head_oid=head_oid,
            index_tree_oid=index_tree_oid,
            tracked_worktree_matches_index=tracked_matches,
            untracked_paths=untracked_paths,
            reason="exact_checkout_observation",
        )

    def _classify(
        self,
        record: CheckoutSyncEffect,
        observation: CheckoutObservation,
    ) -> EffectCertainty:
        if observation.reason != "exact_checkout_observation":
            return "AMBIGUOUS"
        if (
            observation.head_ref != record.source_ref
            or observation.head_oid != record.source_commit_oid
            or observation.tracked_worktree_matches_index is not True
            or observation.untracked_paths
        ):
            return "DIVERGED"
        if observation.index_tree_oid == record.new_tree_oid:
            return "AFTER"
        if observation.index_tree_oid == record.old_tree_oid:
            return "BEFORE"
        return "DIVERGED"

    def _row_record(self, row: tuple[Any, ...]) -> CheckoutSyncEffect:
        changed = tuple(
            (str(item[0]), str(item[1]))
            for item in json.loads(bytes(row[14]).decode("utf-8"))
        )
        return CheckoutSyncEffect(
            work_id=str(row[0]),
            task_id=str(row[1]),
            run_id=str(row[2]),
            effect_id=str(row[3]),
            source_promotion_effect_id=str(row[4]),
            source_ref=str(row[5]),
            source_commit_oid=str(row[6]),
            old_commit_oid=str(row[7]),
            old_tree_oid=str(row[8]),
            new_tree_oid=str(row[9]),
            repository_id=str(row[10]),
            repository_identity_digest=str(row[11]),
            checkout_root=str(row[12]),
            object_format=str(row[13]),
            changed_paths=changed,
            resource_key=str(row[15]),
            state=str(row[16]),  # type: ignore[arg-type]
            effect_certainty=str(row[17]),  # type: ignore[arg-type]
            observation=json.loads(bytes(row[18]).decode("utf-8")),
            lease_id=str(row[19]),
            fence=int(row[20]),
            created_at=str(row[21]),
            updated_at=str(row[22]),
        )

    def get(
        self,
        *,
        effect_id: str | None = None,
        work_id: str | None = None,
    ) -> CheckoutSyncEffect | None:
        if (effect_id is None) == (work_id is None):
            _fail("checkout_effect_lookup_invalid", "provide exactly one of effect_id or work_id")
        field = "effect_id" if effect_id is not None else "work_id"
        value = effect_id if effect_id is not None else work_id
        row = self._connection.execute(
            "SELECT work_id,task_id,run_id,effect_id,source_promotion_effect_id,source_ref,source_commit_oid,"
            "old_commit_oid,old_tree_oid,new_tree_oid,repository_id,repository_identity_digest,checkout_root,"
            "object_format,changed_paths_json,resource_key,state,effect_certainty,observation_json,lease_id,fence,"
            "created_at,updated_at FROM m7b_checkout_effects WHERE " + field + "=?",
            (value,),
        ).fetchone()
        return self._row_record(row) if row else None

    def _assert_active_run(self, record: CheckoutSyncEffect) -> Any:
        query = self.work_kernel.query(record.work_id)
        if query is None or query.work.task_id != record.task_id:
            _fail("work_binding_mismatch", "M7b effect is not bound to the canonical WorkItem")
        run = query.active_run
        if run is None or run.run_id != record.run_id:
            _fail("active_run_required", "checkout synchronization requires the exact active Run")
        if run.lease_id != record.lease_id or int(run.fence) != int(record.fence):
            _fail("run_ownership_mismatch", "checkout synchronization Run lease/fence changed")
        try:
            self.work_kernel.assert_current_lease(
                record.work_id,
                record.lease_id,
                record.fence,
            )
        except Exception as exc:
            _fail(
                "stale_fence",
                "checkout synchronization lease/fence is no longer current",
                details={"cause": getattr(exc, "code", type(exc).__name__)},
            )
        if (
            query.resource_claim is None
            or query.resource_claim.resource_key != record.resource_key
            or query.resource_claim.lease_id != record.lease_id
            or int(query.resource_claim.fence) != int(record.fence)
            or query.resource_claim.state != "HELD"
        ):
            _fail(
                "resource_claim_required",
                "checkout synchronization requires its exact held Work Kernel resource claim",
            )
        return run

    def _persist_observation(
        self,
        record: CheckoutSyncEffect,
        *,
        certainty: EffectCertainty,
        observation: CheckoutObservation,
    ) -> CheckoutSyncEffect:
        if certainty == "AFTER":
            state: EffectState = "AFTER"
        elif certainty == "DIVERGED":
            state = "DIVERGED"
        elif certainty == "AMBIGUOUS":
            state = "UNKNOWN"
        else:
            state = "POSSIBLE" if record.state == "POSSIBLE" else "PREPARED"
        updated_at = _now()
        self._begin()
        try:
            self._connection.execute(
                "UPDATE m7b_checkout_effects SET state=?,effect_certainty=?,observation_json=?,updated_at=? "
                "WHERE work_id=? AND effect_id=?",
                (
                    state,
                    certainty,
                    canonical_json_bytes(observation.as_dict()),
                    updated_at,
                    record.work_id,
                    record.effect_id,
                ),
            )
            self._commit()
        except Exception:
            if self._connection.in_transaction:
                self._rollback()
            raise
        current = self.get(effect_id=record.effect_id)
        assert current is not None
        return current

    def prepare(
        self,
        *,
        source_promotion_effect_id: str,
        work_id: str,
        run_id: str,
        fault: str | None = None,
    ) -> CheckoutSyncEffect:
        source = self._source_promotion(source_promotion_effect_id)
        repository = self._repository(source)
        work_id = _text(work_id, "work_id", maximum=192)
        run_id = _text(run_id, "run_id", maximum=192)

        existing = self.get(work_id=work_id)
        if existing is not None:
            if (
                existing.source_promotion_effect_id != str(source.effect_id)
                or existing.run_id != run_id
            ):
                _fail(
                    "checkout_effect_identity_conflict",
                    "WorkItem is already bound to another checkout synchronization effect",
                )
            return existing

        query = self.work_kernel.query(work_id)
        if query is None or query.work.task_id != str(source.task_id):
            _fail("work_binding_mismatch", "M7b WorkItem must bind the source promotion Task")
        run = query.active_run
        if run is None or run.run_id != run_id:
            _fail("active_run_required", "M7b prepare requires the exact active Run")
        self.work_kernel.assert_current_lease(work_id, run.lease_id, run.fence)

        old_commit_oid = _oid(
            str(source.parent_commit_oid),
            object_format=repository.object_format,
            field="old_commit_oid",
        )
        source_commit_oid = _oid(
            str(source.prepared_commit_oid),
            object_format=repository.object_format,
            field="source_commit_oid",
        )
        old_tree_oid = self._tree_for_commit(repository, old_commit_oid)
        new_tree_oid = self._tree_for_commit(repository, source_commit_oid)
        if new_tree_oid != str(source.prepared_tree_oid):
            _fail("source_promotion_mismatch", "M7b source commit tree differs from M7a prepared tree")

        changed_paths = self._changed_paths(repository, old_tree_oid, new_tree_oid)
        if not changed_paths and old_tree_oid != new_tree_oid:
            _fail("checkout_diff_malformed", "non-identical source trees produced no changed-path proof")

        checkout_root = str(Path(repository.root).absolute())
        resource_key = _resource_key(repository.identity_digest, checkout_root)
        provisional = CheckoutSyncEffect(
            work_id=work_id,
            task_id=str(source.task_id),
            run_id=run_id,
            effect_id="pending",
            source_promotion_effect_id=str(source.effect_id),
            source_ref=str(source.target_ref),
            source_commit_oid=source_commit_oid,
            old_commit_oid=old_commit_oid,
            old_tree_oid=old_tree_oid,
            new_tree_oid=new_tree_oid,
            repository_id=repository.repository_id,
            repository_identity_digest=repository.identity_digest,
            checkout_root=checkout_root,
            object_format=repository.object_format,
            changed_paths=changed_paths,
            resource_key=resource_key,
            state="PREPARED",
            effect_certainty="BEFORE",
            observation={},
            lease_id=run.lease_id,
            fence=int(run.fence),
            created_at="pending",
            updated_at="pending",
        )
        try:
            observation = self._observe(provisional)
        except M7bError as exc:
            _fail(
                "checkout_precondition_unknown",
                "M7b could not establish exact local checkout precondition",
                details={"cause": exc.code},
            )
        certainty = self._classify(provisional, observation)
        if certainty not in {"BEFORE", "AFTER"}:
            _fail(
                "checkout_precondition_mismatch",
                "local checkout contains state outside the exact old/new synchronization boundary",
                details={"observation": observation.as_dict(), "effect_certainty": certainty},
            )

        self.work_kernel.claim_resource(work_id, resource_key, run.lease_id, run.fence)

        identity = {
            "schema": M7B_EFFECT_SCHEMA,
            "work_id": work_id,
            "task_id": str(source.task_id),
            "run_id": run_id,
            "source_promotion_effect_id": str(source.effect_id),
            "source_ref": str(source.target_ref),
            "source_commit_oid": source_commit_oid,
            "old_commit_oid": old_commit_oid,
            "old_tree_oid": old_tree_oid,
            "new_tree_oid": new_tree_oid,
            "repository_id": repository.repository_id,
            "repository_identity_digest": repository.identity_digest,
            "checkout_root": checkout_root,
            "object_format": repository.object_format,
            "changed_paths": [list(item) for item in changed_paths],
            "resource_key": resource_key,
            "lease_id": run.lease_id,
            "fence": int(run.fence),
        }
        effect_id = semantic_digest(identity)
        created_at = _now()
        state: EffectState = "AFTER" if certainty == "AFTER" else "PREPARED"
        self._begin()
        try:
            self._connection.execute(
                "INSERT INTO m7b_checkout_effects VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    work_id,
                    str(source.task_id),
                    run_id,
                    effect_id,
                    str(source.effect_id),
                    str(source.target_ref),
                    source_commit_oid,
                    old_commit_oid,
                    old_tree_oid,
                    new_tree_oid,
                    repository.repository_id,
                    repository.identity_digest,
                    checkout_root,
                    repository.object_format,
                    canonical_json_bytes([list(item) for item in changed_paths]),
                    resource_key,
                    state,
                    certainty,
                    canonical_json_bytes(observation.as_dict()),
                    run.lease_id,
                    int(run.fence),
                    created_at,
                    created_at,
                ),
            )
            self._commit()
        except sqlite3.IntegrityError:
            if self._connection.in_transaction:
                self._rollback()
            current = self.get(work_id=work_id)
            if current is not None and current.effect_id == effect_id:
                return current
            _fail("checkout_effect_storage_conflict", "M7b checkout effect identity conflicted")
        except Exception:
            if self._connection.in_transaction:
                self._rollback()
            raise
        if fault == "after_prepare_commit":
            _fail(
                "simulated_response_loss_after_prepare",
                "M7b prepare committed before response",
                details={"effect_id": effect_id},
            )
        result = self.get(effect_id=effect_id)
        assert result is not None
        return result

    def reconcile(self, *, effect_id: str) -> CheckoutSyncEffect:
        record = self.get(effect_id=effect_id)
        if record is None:
            _fail("checkout_effect_missing", "M7b checkout effect does not exist")
        # Source promotion is read as an independent prerequisite.  M7b never
        # writes or reclassifies the source M7a record.
        self._source_promotion(record.source_promotion_effect_id)
        try:
            observation = self._observe(record)
        except M7bError as exc:
            observation = CheckoutObservation(
                head_ref=None,
                head_oid=None,
                index_tree_oid=None,
                tracked_worktree_matches_index=None,
                untracked_paths=(),
                reason=exc.code,
            )
            certainty: EffectCertainty = "AMBIGUOUS"
        else:
            certainty = self._classify(record, observation)
        return self._persist_observation(
            record,
            certainty=certainty,
            observation=observation,
        )

    def _mark_possible(self, record: CheckoutSyncEffect) -> CheckoutSyncEffect:
        updated_at = _now()
        self._begin()
        try:
            cursor = self._connection.execute(
                "UPDATE m7b_checkout_effects SET state='POSSIBLE',effect_certainty='POSSIBLE',updated_at=? "
                "WHERE work_id=? AND effect_id=? AND effect_certainty='BEFORE'",
                (updated_at, record.work_id, record.effect_id),
            )
            if cursor.rowcount != 1:
                _fail("checkout_effect_state_conflict", "checkout effect certainty changed before boundary marker")
            self._commit()
        except Exception:
            if self._connection.in_transaction:
                self._rollback()
            raise
        current = self.get(effect_id=record.effect_id)
        assert current is not None
        return current

    def _apply_checkout(self, record: CheckoutSyncEffect) -> subprocess.CompletedProcess[bytes]:
        return self._git(
            record.checkout_root,
            ["read-tree", "-u", "-m", record.old_tree_oid, record.new_tree_oid],
            check=False,
            operation="exact checkout index/worktree synchronization",
        )

    def apply_if_safe(
        self,
        *,
        effect_id: str,
        fault: str | None = None,
    ) -> CheckoutSyncEffect:
        record = self.get(effect_id=effect_id)
        if record is None:
            _fail("checkout_effect_missing", "M7b checkout effect does not exist")

        observed = self.reconcile(effect_id=effect_id)
        if observed.effect_certainty == "AFTER":
            return observed
        if observed.effect_certainty == "DIVERGED":
            _fail(
                "checkout_diverged",
                "local checkout contains unrelated or unexpected state; automatic sync is prohibited",
                details={"observation": dict(observed.observation)},
            )
        if observed.effect_certainty == "AMBIGUOUS":
            _fail("checkout_reconciliation_required", "local checkout cannot be observed exactly")
        if observed.effect_certainty != "BEFORE":
            _fail("checkout_reconciliation_required", "checkout effect is not proven BEFORE")

        self._assert_active_run(observed)
        possible = self._mark_possible(observed)
        self._assert_active_run(possible)
        if fault == "after_possible":
            _fail(
                "simulated_crash_after_possible",
                "fault injected after durable POSSIBLE before checkout synchronization",
            )

        completed = self._apply_checkout(possible)
        if completed.returncode == 0:
            if fault == "after_checkout_update":
                _fail(
                    "simulated_crash_after_checkout_update",
                    "fault injected after checkout update before effect close",
                )
            final = self.reconcile(effect_id=effect_id)
            if final.effect_certainty != "AFTER":
                _fail(
                    "checkout_reconciliation_required",
                    "successful checkout command did not reconcile to AFTER",
                )
            return final

        final = self.reconcile(effect_id=effect_id)
        if final.effect_certainty == "AFTER":
            return final
        if final.effect_certainty == "BEFORE":
            _fail(
                "checkout_sync_failed",
                "checkout command failed and exact observation proves the old index/worktree remains",
                details={"safe_next_action": "SAFE_TO_APPLY", "returncode": completed.returncode},
            )
        if final.effect_certainty == "DIVERGED":
            _fail(
                "checkout_diverged",
                "checkout command left or encountered unexpected local state; automatic retry is prohibited",
                details={"observation": dict(final.observation)},
            )
        _fail(
            "checkout_reconciliation_required",
            "checkout command result is ambiguous after exact observation attempt",
        )

    def query(self, effect_id: str) -> dict[str, Any]:
        record = self.get(effect_id=effect_id)
        if record is None:
            _fail("checkout_effect_missing", "M7b checkout effect does not exist")
        source = self.m7a_adapter.get(effect_id=record.source_promotion_effect_id)
        source_view = None
        if source is not None:
            source_view = {
                "effect_id": str(source.effect_id),
                "state": str(source.state),
                "effect_certainty": str(source.effect_certainty),
                "target_ref": str(source.target_ref),
                "prepared_commit_oid": str(source.prepared_commit_oid),
            }
        payload = {
            "schema": M7B_QUERY_SCHEMA,
            "source_promotion": source_view,
            "checkout_sync": record.as_dict(),
        }
        payload["query_digest"] = semantic_digest(payload)
        return payload


__all__ = [
    "CheckoutObservation",
    "CheckoutSyncAdapter",
    "CheckoutSyncEffect",
    "EffectCertainty",
    "EffectState",
    "M7B_AUTHORITY",
    "M7B_EFFECT_SCHEMA",
    "M7B_QUERY_SCHEMA",
    "M7bError",
]

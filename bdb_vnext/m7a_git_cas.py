"""M7a prepared Git compare-and-swap adapter for the inactive BDB vNext line.

The adapter prepares immutable Git objects and one exact ref effect without
mutating a checkout, index, remote or legacy promotion receipt.  Git ref truth
is the physical witness.  The canonical Work Kernel owns the Work/Run/lease/
fence and the existing M6a EvidencePolicyGate is re-evaluated immediately
before the ref effect.

This is BUILD-ONLY.  Only refs below ``refs/bdb-vnext/`` are accepted and no
production authority cutover happens here.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, NoReturn, Sequence

from bdb_shared.evidence import canonical_json_bytes, semantic_digest
from bdb_vnext.candidate import (
    CANDIDATE_ABSENCE_SCHEMA,
    CANDIDATE_SEALED,
    CANDIDATE_VIEW_SCHEMA,
    CandidateRepoView,
)
from bdb_vnext.control_store import (
    ControlStoreError,
    begin_control_write,
    commit_control_write,
    rollback_control_write,
)
from bdb_vnext.m6a_evidence_policy import EvidencePolicyGate, compute_subject_digest
from bdb_vnext.repo_view import RepositoryResource, RepoViewError


M7A_EFFECT_SCHEMA = "bdb-vnext-m7a-git-effect-v1"
M7A_QUERY_SCHEMA = "bdb-vnext-m7a-git-effect-query-v1"
M7A_COMMIT_POLICY_SCHEMA = "bdb-vnext-m7a-commit-metadata-policy-v1"
M7A_RESOURCE_SCHEMA = "bdb-vnext-m7a-git-resource-v1"
M7A_AUTHORITY = "devmaster.bdb.vnext.git-cas-adapter"
M7A_ALLOWED_REF_PREFIX = "refs/bdb-vnext/"

EffectState = Literal["PREPARED", "POSSIBLE", "AFTER", "DIVERGED", "UNKNOWN"]
EffectCertainty = Literal["BEFORE", "POSSIBLE", "AFTER", "DIVERGED", "AMBIGUOUS"]
SafeNextAction = Literal[
    "SAFE_TO_APPLY",
    "OBSERVE_REQUIRED",
    "COMPLETE_ALLOWED",
    "MANUAL_RECONCILIATION",
]

_HEX = frozenset("0123456789abcdef")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class M7aError(RuntimeError):
    """Typed fail-closed error for prepared Git CAS."""

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
    raise M7aError(code, message, details=details)


def _text(value: object, field: str, *, maximum: int = 2048) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or "\x00" in value
        or any(ord(char) < 32 for char in value)
    ):
        _fail("invalid_git_effect_input", f"{field} must be bounded non-empty text")
    return value


def _digest(value: object, field: str) -> str:
    text = _text(value, field, maximum=71)
    if _DIGEST_RE.fullmatch(text) is None:
        _fail("invalid_git_effect_input", f"{field} must be exact lowercase sha256:<64 hex>")
    return text


def _oid(value: object, *, object_format: str, field: str, allow_zero: bool = False) -> str:
    text = _text(value, field, maximum=128).lower()
    length = 40 if object_format == "sha1" else 64 if object_format == "sha256" else 0
    if length == 0:
        _fail("unsupported_object_format", "M7a supports only Git sha1/sha256 repositories")
    if len(text) != length or any(char not in _HEX for char in text):
        _fail("invalid_git_oid", f"{field} is not an exact {object_format} OID")
    if not allow_zero and set(text) == {"0"}:
        _fail("invalid_git_oid", f"{field} cannot be the null OID")
    return text


def _zero_oid(object_format: str) -> str:
    return "0" * (40 if object_format == "sha1" else 64)


def _canonical_timestamp(value: object) -> str:
    text = _text(value, "timestamp", maximum=64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise M7aError("invalid_commit_metadata", "commit timestamp is not ISO-8601") from exc
    if parsed.tzinfo is None:
        _fail("invalid_commit_metadata", "commit timestamp must include a timezone")
    return (
        parsed.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _git_environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
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
    if extra:
        environment.update({str(key): str(value) for key, value in extra.items()})
    return environment


def _resource_key(repository_identity_digest: str, target_ref: str) -> str:
    digest = semantic_digest(
        {
            "schema": M7A_RESOURCE_SCHEMA,
            "repository_identity_digest": repository_identity_digest,
            "target_ref": target_ref,
        }
    )
    return "git-ref:" + digest.split(":", 1)[1]


def candidate_subject_identity(candidate: CandidateRepoView) -> dict[str, Any]:
    return {
        "candidate_id": candidate.candidate_id,
        "view_id": candidate.view_id,
        "manifest_digest": candidate.manifest_digest,
        "candidate_tree_digest": candidate.candidate_tree_digest,
        "base_view_id": candidate.base_view_id,
        "repository_id": candidate.repository_id,
    }


@dataclass(frozen=True)
class CommitMetadataPolicy:
    message: str
    author_name: str
    author_email: str
    committer_name: str
    committer_email: str
    timestamp: str

    def normalized(self) -> "CommitMetadataPolicy":
        message = _text(self.message, "commit_message", maximum=4096)
        author_name = _text(self.author_name, "author_name", maximum=256)
        author_email = _text(self.author_email, "author_email", maximum=320)
        committer_name = _text(self.committer_name, "committer_name", maximum=256)
        committer_email = _text(self.committer_email, "committer_email", maximum=320)
        if "\n" in author_name or "\n" in author_email or "\n" in committer_name or "\n" in committer_email:
            _fail("invalid_commit_metadata", "Git identity fields must be single-line")
        return CommitMetadataPolicy(
            message=message,
            author_name=author_name,
            author_email=author_email,
            committer_name=committer_name,
            committer_email=committer_email,
            timestamp=_canonical_timestamp(self.timestamp),
        )

    @property
    def policy_digest(self) -> str:
        return semantic_digest(self.as_dict())

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": M7A_COMMIT_POLICY_SCHEMA,
            "message": self.message,
            "author_name": self.author_name,
            "author_email": self.author_email,
            "committer_name": self.committer_name,
            "committer_email": self.committer_email,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class PreparedGitEffect:
    work_id: str
    task_id: str
    run_id: str
    effect_id: str
    candidate_id: str
    candidate_view_id: str
    candidate_tree_digest: str
    base_view_id: str
    repository_id: str
    repository_identity_digest: str
    repository_root: str
    object_format: str
    target_ref: str
    expected_old_oid: str
    prepared_tree_oid: str
    prepared_commit_oid: str
    parent_commit_oid: str
    resource_key: str
    subject_digest: str
    intent_revision_id: str
    validation_policy_digest: str
    check_plan_digest: str
    obligation_ids: tuple[str, ...]
    scope: str
    commit_metadata: Mapping[str, Any]
    commit_metadata_policy_digest: str
    state: EffectState
    effect_certainty: EffectCertainty
    observed_ref_oid: str | None
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
            "schema": M7A_QUERY_SCHEMA,
            "authority": M7A_AUTHORITY,
            "mode": "BUILD_ONLY",
            "work_id": self.work_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "effect_id": self.effect_id,
            "candidate_id": self.candidate_id,
            "candidate_view_id": self.candidate_view_id,
            "candidate_tree_digest": self.candidate_tree_digest,
            "base_view_id": self.base_view_id,
            "repository_id": self.repository_id,
            "repository_identity_digest": self.repository_identity_digest,
            "repository_root": self.repository_root,
            "object_format": self.object_format,
            "target_ref": self.target_ref,
            "expected_old_oid": self.expected_old_oid,
            "prepared_tree_oid": self.prepared_tree_oid,
            "prepared_commit_oid": self.prepared_commit_oid,
            "parent_commit_oid": self.parent_commit_oid,
            "resource_key": self.resource_key,
            "subject_digest": self.subject_digest,
            "intent_revision_id": self.intent_revision_id,
            "validation_policy_digest": self.validation_policy_digest,
            "check_plan_digest": self.check_plan_digest,
            "obligation_ids": list(self.obligation_ids),
            "scope": self.scope,
            "commit_metadata": dict(self.commit_metadata),
            "commit_metadata_policy_digest": self.commit_metadata_policy_digest,
            "state": self.state,
            "effect_certainty": self.effect_certainty,
            "safe_next_action": self.safe_next_action,
            "observed_ref_oid": self.observed_ref_oid,
            "observation": dict(self.observation),
            "lease_id": self.lease_id,
            "fence": self.fence,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        payload["query_digest"] = semantic_digest(payload)
        return payload


class PreparedGitCasAdapter:
    """One prepared, exact Git ref effect per canonical WorkItem."""

    def __init__(
        self,
        *,
        candidate_store: Any,
        work_kernel: Any,
        evidence_policy_gate: EvidencePolicyGate,
        allowed_ref_prefix: str = M7A_ALLOWED_REF_PREFIX,
        git_timeout_seconds: float = 30.0,
    ) -> None:
        if candidate_store is None or work_kernel is None or evidence_policy_gate is None:
            _fail("m7a_dependencies_required", "M7a requires Candidate, Work Kernel and M6a gate")
        self.candidate_store = candidate_store
        self.work_kernel = work_kernel
        self.evidence_policy_gate = evidence_policy_gate
        self.allowed_ref_prefix = _text(allowed_ref_prefix, "allowed_ref_prefix", maximum=512)
        if not self.allowed_ref_prefix.startswith("refs/"):
            _fail("invalid_ref_policy", "M7a allowed ref prefix must be a full refs/ namespace")
        self.git_timeout_seconds = float(git_timeout_seconds)
        if not 0 < self.git_timeout_seconds <= 300:
            _fail("invalid_git_timeout", "M7a Git timeout must be positive and bounded")
        self._connection = candidate_store._connection
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS m7a_git_effects (
              work_id TEXT PRIMARY KEY,
              task_id TEXT NOT NULL,
              run_id TEXT NOT NULL,
              effect_id TEXT NOT NULL UNIQUE,
              candidate_id TEXT NOT NULL,
              candidate_view_id TEXT NOT NULL,
              candidate_tree_digest TEXT NOT NULL,
              base_view_id TEXT NOT NULL,
              repository_id TEXT NOT NULL,
              repository_identity_digest TEXT NOT NULL,
              repository_root TEXT NOT NULL,
              object_format TEXT NOT NULL,
              target_ref TEXT NOT NULL,
              expected_old_oid TEXT NOT NULL,
              prepared_tree_oid TEXT NOT NULL,
              prepared_commit_oid TEXT NOT NULL,
              parent_commit_oid TEXT NOT NULL,
              resource_key TEXT NOT NULL UNIQUE,
              subject_digest TEXT NOT NULL,
              intent_revision_id TEXT NOT NULL,
              validation_policy_digest TEXT NOT NULL,
              check_plan_digest TEXT NOT NULL,
              obligation_ids_json BLOB NOT NULL,
              scope TEXT NOT NULL,
              commit_metadata_json BLOB NOT NULL,
              commit_metadata_policy_digest TEXT NOT NULL,
              state TEXT NOT NULL,
              effect_certainty TEXT NOT NULL,
              observed_ref_oid TEXT,
              observation_json BLOB NOT NULL,
              lease_id TEXT NOT NULL,
              fence INTEGER NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS m7a_git_effects_by_candidate
              ON m7a_git_effects(candidate_id, candidate_view_id);
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
        repository_root: str | Path,
        args: Iterable[str],
        *,
        input_bytes: bytes | None = None,
        extra_env: Mapping[str, str] | None = None,
        check: bool = True,
        operation: str,
    ) -> subprocess.CompletedProcess[bytes]:
        command = ["git", "--no-replace-objects", "-C", str(repository_root), *list(args)]
        try:
            completed = subprocess.run(
                command,
                input=input_bytes,
                shell=False,
                capture_output=True,
                timeout=self.git_timeout_seconds,
                check=False,
                env=_git_environment(extra_env),
            )
        except FileNotFoundError as exc:
            raise M7aError("git_unavailable", "Git executable is unavailable") from exc
        except subprocess.TimeoutExpired as exc:
            raise M7aError(
                "git_effect_unknown",
                f"Git operation timed out during {operation}",
                details={"operation": operation},
            ) from exc
        except OSError as exc:
            raise M7aError(
                "git_effect_unknown",
                f"Git operation could not execute during {operation}",
                details={"operation": operation},
            ) from exc
        if check and completed.returncode != 0:
            _fail(
                "git_command_failed",
                f"Git command failed during {operation}",
                details={"operation": operation, "returncode": completed.returncode},
            )
        return completed

    def _validate_ref(self, repository: RepositoryResource, target_ref: str) -> str:
        target_ref = _text(target_ref, "target_ref", maximum=1024)
        if not target_ref.startswith(self.allowed_ref_prefix):
            _fail(
                "production_ref_forbidden",
                "M7a build-only adapter accepts only isolated vNext refs",
                details={"allowed_prefix": self.allowed_ref_prefix},
            )
        completed = self._git(
            repository.root,
            ["check-ref-format", target_ref],
            check=False,
            operation="ref validation",
        )
        if completed.returncode != 0:
            _fail("invalid_target_ref", "target_ref is not a valid full Git ref")
        return target_ref

    def _bound_repository(self, record: PreparedGitEffect) -> RepositoryResource:
        try:
            repository = RepositoryResource.from_path(
                record.repository_root,
                repository_id=record.repository_id,
            )
        except RepoViewError as exc:
            raise M7aError(
                "repository_unavailable",
                "prepared Git repository cannot be reopened",
                details={"cause": exc.code},
            ) from exc
        if (
            repository.identity_digest != record.repository_identity_digest
            or repository.object_format != record.object_format
        ):
            _fail("repository_identity_mismatch", "prepared Git repository identity changed")
        return repository

    def _assert_candidate(self, record: PreparedGitEffect) -> None:
        try:
            current = self.candidate_store.verify_current_applicability(record.candidate_id)
        except Exception as exc:
            _fail(
                "candidate_not_current",
                "prepared Git effect requires the exact current sealed Candidate",
                details={"cause": getattr(exc, "code", type(exc).__name__)},
            )
        if (
            current.state != CANDIDATE_SEALED
            or current.manifest_digest != record.candidate_view_id
            or (current.observed_tree_digest or current.planned_tree_digest) != record.candidate_tree_digest
        ):
            _fail("candidate_not_current", "prepared Git effect Candidate binding is stale")

    def _assert_active_run(self, record: PreparedGitEffect) -> Any:
        query = self.work_kernel.query(record.work_id)
        if query is None or query.work.task_id != record.task_id:
            _fail("work_binding_mismatch", "M7a effect is not bound to the canonical WorkItem")
        run = query.active_run
        if run is None or run.run_id != record.run_id:
            _fail("active_run_required", "Git ref effect requires the exact active Run")
        if run.lease_id != record.lease_id or int(run.fence) != int(record.fence):
            _fail("run_ownership_mismatch", "Git ref effect Run lease/fence changed")
        try:
            self.work_kernel.assert_current_lease(
                record.work_id,
                record.lease_id,
                record.fence,
            )
        except Exception as exc:
            _fail(
                "stale_fence",
                "Git ref effect lease/fence is no longer current",
                details={"cause": getattr(exc, "code", type(exc).__name__)},
            )
        if (
            query.resource_claim is None
            or query.resource_claim.resource_key != record.resource_key
            or query.resource_claim.lease_id != record.lease_id
            or int(query.resource_claim.fence) != int(record.fence)
            or query.resource_claim.state != "HELD"
        ):
            _fail("resource_claim_required", "Git ref effect requires its exact held Work Kernel resource claim")
        return run

    def _row_record(self, row: tuple[Any, ...]) -> PreparedGitEffect:
        return PreparedGitEffect(
            work_id=str(row[0]),
            task_id=str(row[1]),
            run_id=str(row[2]),
            effect_id=str(row[3]),
            candidate_id=str(row[4]),
            candidate_view_id=str(row[5]),
            candidate_tree_digest=str(row[6]),
            base_view_id=str(row[7]),
            repository_id=str(row[8]),
            repository_identity_digest=str(row[9]),
            repository_root=str(row[10]),
            object_format=str(row[11]),
            target_ref=str(row[12]),
            expected_old_oid=str(row[13]),
            prepared_tree_oid=str(row[14]),
            prepared_commit_oid=str(row[15]),
            parent_commit_oid=str(row[16]),
            resource_key=str(row[17]),
            subject_digest=str(row[18]),
            intent_revision_id=str(row[19]),
            validation_policy_digest=str(row[20]),
            check_plan_digest=str(row[21]),
            obligation_ids=tuple(json.loads(bytes(row[22]).decode("utf-8"))),
            scope=str(row[23]),
            commit_metadata=json.loads(bytes(row[24]).decode("utf-8")),
            commit_metadata_policy_digest=str(row[25]),
            state=str(row[26]),  # type: ignore[arg-type]
            effect_certainty=str(row[27]),  # type: ignore[arg-type]
            observed_ref_oid=str(row[28]) if row[28] else None,
            observation=json.loads(bytes(row[29]).decode("utf-8")),
            lease_id=str(row[30]),
            fence=int(row[31]),
            created_at=str(row[32]),
            updated_at=str(row[33]),
        )

    def get(self, *, work_id: str | None = None, effect_id: str | None = None) -> PreparedGitEffect | None:
        if (work_id is None) == (effect_id is None):
            _fail("git_effect_lookup_invalid", "provide exactly one of work_id or effect_id")
        field = "work_id" if work_id is not None else "effect_id"
        value = work_id if work_id is not None else effect_id
        row = self._connection.execute(
            "SELECT work_id,task_id,run_id,effect_id,candidate_id,candidate_view_id,candidate_tree_digest,base_view_id,"
            "repository_id,repository_identity_digest,repository_root,object_format,target_ref,expected_old_oid,"
            "prepared_tree_oid,prepared_commit_oid,parent_commit_oid,resource_key,subject_digest,intent_revision_id,"
            "validation_policy_digest,check_plan_digest,obligation_ids_json,scope,commit_metadata_json,"
            "commit_metadata_policy_digest,state,effect_certainty,observed_ref_oid,observation_json,lease_id,fence,"
            "created_at,updated_at FROM m7a_git_effects WHERE " + field + "=?",
            (value,),
        ).fetchone()
        return self._row_record(row) if row else None

    def _observe_ref(self, repository: RepositoryResource, target_ref: str) -> str | None:
        # ``show-ref --verify --hash`` uses different non-zero statuses for an
        # absent ref across supported Git versions.  ``for-each-ref`` gives a
        # stable zero-result representation and also lets us require the exact
        # full ref name instead of inferring absence from an exit code.
        completed = self._git(
            repository.root,
            ["for-each-ref", "--format=%(refname)\t%(objectname)", target_ref],
            operation="ref observation",
        )
        observed: str | None = None
        for raw_line in completed.stdout.splitlines():
            ref_bytes, separator, oid_bytes = raw_line.partition(b"\t")
            if not separator:
                _fail("git_ref_observation_failed", "Git ref observation output is malformed")
            ref_name = ref_bytes.decode("utf-8", errors="strict")
            if ref_name != target_ref:
                continue
            if observed is not None:
                _fail("git_ref_observation_failed", "Git returned duplicate exact ref observations")
            observed = _oid(
                oid_bytes.decode("ascii", errors="strict").strip().lower(),
                object_format=repository.object_format,
                field="observed_ref_oid",
            )
        return observed

    def _expected_matches(self, record: PreparedGitEffect, observed: str | None) -> bool:
        null_oid = _zero_oid(record.object_format)
        if record.expected_old_oid == null_oid:
            return observed is None
        return observed == record.expected_old_oid

    def _classification(self, record: PreparedGitEffect, observed: str | None) -> EffectCertainty:
        if observed == record.prepared_commit_oid:
            return "AFTER"
        if self._expected_matches(record, observed):
            return "BEFORE"
        return "DIVERGED"

    def _persist_observation(
        self,
        record: PreparedGitEffect,
        *,
        certainty: EffectCertainty,
        observed: str | None,
        reason: str,
    ) -> PreparedGitEffect:
        state: EffectState
        if certainty == "AFTER":
            state = "AFTER"
        elif certainty == "DIVERGED":
            state = "DIVERGED"
        elif certainty == "AMBIGUOUS":
            state = "UNKNOWN"
        else:
            # Preserve the durable POSSIBLE boundary if it has ever been crossed.
            state = "POSSIBLE" if record.state == "POSSIBLE" else "PREPARED"
        observation = {
            "reason": reason,
            "observed_ref_oid": observed,
            "expected_old_oid": record.expected_old_oid,
            "prepared_commit_oid": record.prepared_commit_oid,
        }
        updated_at = _now()
        self._begin()
        try:
            self._connection.execute(
                "UPDATE m7a_git_effects SET state=?,effect_certainty=?,observed_ref_oid=?,observation_json=?,updated_at=? "
                "WHERE work_id=? AND effect_id=?",
                (
                    state,
                    certainty,
                    observed,
                    canonical_json_bytes(observation),
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

    def _candidate_entries_digest(self, candidate: CandidateRepoView) -> str:
        entries = [
            [entry.path, entry.object_oid, entry.mode]
            for entry in candidate.list_entries()
        ]
        return semantic_digest({"schema": CANDIDATE_VIEW_SCHEMA, "entries": entries})

    def _tree_entries_digest(self, repository: RepositoryResource, tree_oid: str) -> str:
        completed = self._git(
            repository.root,
            ["ls-tree", "-r", "-z", "--full-tree", tree_oid],
            operation="prepared tree verification",
        )
        entries: list[list[str]] = []
        for chunk in completed.stdout.split(b"\x00"):
            if not chunk:
                continue
            metadata, separator, path_bytes = chunk.partition(b"\t")
            if not separator:
                _fail("prepared_tree_invalid", "prepared Git tree has malformed output")
            fields = metadata.decode("ascii", errors="strict").split(" ")
            if len(fields) != 3 or fields[1] != "blob":
                _fail("prepared_tree_invalid", "prepared Git tree contains unsupported entries")
            mode, _kind, oid_value = fields
            path = path_bytes.decode("utf-8", errors="strict").replace("\\", "/")
            entries.append([path, oid_value.lower(), mode])
        entries.sort(key=lambda item: item[0])
        return semantic_digest({"schema": CANDIDATE_VIEW_SCHEMA, "entries": entries})

    def _materialize_tree_and_commit(
        self,
        *,
        candidate: CandidateRepoView,
        repository: RepositoryResource,
        metadata: CommitMetadataPolicy,
    ) -> tuple[str, str]:
        with tempfile.TemporaryDirectory(prefix="bdb-m7a-index-") as temporary:
            index_path = Path(temporary) / "index"
            index_env = {"GIT_INDEX_FILE": str(index_path)}
            self._git(
                repository.root,
                ["read-tree", candidate.base_tree_oid],
                extra_env=index_env,
                operation="temporary index base load",
            )
            for plan in candidate.path_bindings:
                if (
                    plan.after_ref.type == "application/x-bdb-absence"
                    and plan.after_ref.schema == CANDIDATE_ABSENCE_SCHEMA
                ):
                    self._git(
                        repository.root,
                        ["update-index", "--force-remove", "--", plan.path],
                        extra_env=index_env,
                        operation="temporary index delete",
                    )
                    continue
                try:
                    payload = self.candidate_store.content_store.resolve(plan.after_ref)
                except Exception as exc:
                    _fail(
                        "candidate_content_unavailable",
                        "Candidate after bytes are unavailable for Git object preparation",
                        details={"cause": getattr(exc, "code", type(exc).__name__)},
                    )
                hashed = self._git(
                    repository.root,
                    ["hash-object", "-w", "--stdin"],
                    input_bytes=payload,
                    operation="prepared blob write",
                )
                blob_oid = _oid(
                    hashed.stdout.decode("ascii", errors="strict").strip(),
                    object_format=repository.object_format,
                    field="prepared_blob_oid",
                )
                expected_entry = candidate.entry(plan.path)
                if blob_oid != expected_entry.object_oid:
                    _fail("prepared_blob_mismatch", "Git blob identity differs from sealed Candidate")
                mode = "100755" if plan.after_mode & 0o111 else "100644"
                self._git(
                    repository.root,
                    ["update-index", "--add", "--cacheinfo", mode, blob_oid, plan.path],
                    extra_env=index_env,
                    operation="temporary index update",
                )
            tree = self._git(
                repository.root,
                ["write-tree"],
                extra_env=index_env,
                operation="prepared tree write",
            )
            tree_oid = _oid(
                tree.stdout.decode("ascii", errors="strict").strip(),
                object_format=repository.object_format,
                field="prepared_tree_oid",
            )

        candidate_digest = self._candidate_entries_digest(candidate)
        if candidate_digest != candidate.candidate_tree_digest:
            _fail("candidate_tree_integrity_failure", "sealed Candidate tree digest is inconsistent")
        if self._tree_entries_digest(repository, tree_oid) != candidate.candidate_tree_digest:
            _fail("prepared_tree_mismatch", "prepared Git tree differs from sealed Candidate tree")

        commit_env = {
            "GIT_AUTHOR_NAME": metadata.author_name,
            "GIT_AUTHOR_EMAIL": metadata.author_email,
            "GIT_AUTHOR_DATE": metadata.timestamp,
            "GIT_COMMITTER_NAME": metadata.committer_name,
            "GIT_COMMITTER_EMAIL": metadata.committer_email,
            "GIT_COMMITTER_DATE": metadata.timestamp,
        }
        message = metadata.message.encode("utf-8")
        if not message.endswith(b"\n"):
            message += b"\n"
        commit = self._git(
            repository.root,
            ["-c", "commit.gpgsign=false", "commit-tree", tree_oid, "-p", candidate.base_commit_oid],
            input_bytes=message,
            extra_env=commit_env,
            operation="prepared commit write",
        )
        commit_oid = _oid(
            commit.stdout.decode("ascii", errors="strict").strip(),
            object_format=repository.object_format,
            field="prepared_commit_oid",
        )
        resolved_tree = self._git(
            repository.root,
            ["rev-parse", "--verify", f"{commit_oid}^{{tree}}"],
            operation="prepared commit tree verification",
        ).stdout.decode("ascii", errors="strict").strip().lower()
        parents = self._git(
            repository.root,
            ["show", "-s", "--format=%P", commit_oid],
            operation="prepared commit parent verification",
        ).stdout.decode("ascii", errors="strict").strip().lower().split()
        if resolved_tree != tree_oid or parents != [candidate.base_commit_oid]:
            _fail("prepared_commit_mismatch", "prepared commit is not the exact Candidate child commit")
        return tree_oid, commit_oid

    def prepare(
        self,
        *,
        candidate: CandidateRepoView,
        repository: RepositoryResource,
        work_id: str,
        run_id: str,
        target_ref: str,
        expected_old_oid: str,
        metadata: CommitMetadataPolicy,
        intent_revision_id: str,
        validation_policy_digest: str,
        check_plan_digest: str,
        obligation_ids: Sequence[str],
        scope: str,
        fault: str | None = None,
    ) -> PreparedGitEffect:
        if not isinstance(candidate, CandidateRepoView):
            _fail("candidate_required", "M7a prepare requires a sealed CandidateRepoView")
        work_id = _text(work_id, "work_id", maximum=192)
        run_id = _text(run_id, "run_id", maximum=192)
        intent_revision_id = _text(intent_revision_id, "intent_revision_id", maximum=192)
        validation_policy_digest = _digest(validation_policy_digest, "validation_policy_digest")
        check_plan_digest = _digest(check_plan_digest, "check_plan_digest")
        scope = _text(scope, "scope", maximum=512)
        obligations = tuple(sorted({_text(item, "obligation_id", maximum=192) for item in obligation_ids}))
        if not obligations:
            _fail("promotion_obligation_missing", "M7a requires at least one M6a obligation")
        metadata = metadata.normalized()

        target_ref = self._validate_ref(repository, target_ref)
        expected_old_oid = _oid(
            expected_old_oid,
            object_format=repository.object_format,
            field="expected_old_oid",
            allow_zero=True,
        )
        null_oid = _zero_oid(repository.object_format)
        if expected_old_oid not in {null_oid, candidate.base_commit_oid}:
            _fail(
                "expected_ref_not_candidate_base",
                "M7a expected old ref must be absent or the exact Candidate base commit",
            )

        existing = self.get(work_id=work_id)
        if existing is not None:
            requested = {
                "candidate_id": candidate.candidate_id,
                "candidate_view_id": candidate.view_id,
                "repository_identity_digest": repository.identity_digest,
                "target_ref": target_ref,
                "expected_old_oid": expected_old_oid,
                "intent_revision_id": intent_revision_id,
                "validation_policy_digest": validation_policy_digest,
                "check_plan_digest": check_plan_digest,
                "obligation_ids": obligations,
                "scope": scope,
                "commit_metadata": metadata.as_dict(),
            }
            actual = {
                "candidate_id": existing.candidate_id,
                "candidate_view_id": existing.candidate_view_id,
                "repository_identity_digest": existing.repository_identity_digest,
                "target_ref": existing.target_ref,
                "expected_old_oid": existing.expected_old_oid,
                "intent_revision_id": existing.intent_revision_id,
                "validation_policy_digest": existing.validation_policy_digest,
                "check_plan_digest": existing.check_plan_digest,
                "obligation_ids": existing.obligation_ids,
                "scope": existing.scope,
                "commit_metadata": dict(existing.commit_metadata),
            }
            if actual != requested:
                _fail("git_effect_identity_conflict", "WorkItem is already bound to another exact Git effect")
            return existing

        try:
            sealed = self.candidate_store.verify_current_applicability(candidate.candidate_id)
        except Exception as exc:
            _fail(
                "candidate_not_current",
                "M7a prepare requires a current sealed Candidate",
                details={"cause": getattr(exc, "code", type(exc).__name__)},
            )
        if sealed.manifest_digest != candidate.view_id:
            _fail("candidate_not_current", "CandidateRepoView differs from current sealed Candidate")

        if (
            repository.repository_id != candidate.repository_id
            or repository.object_format not in {"sha1", "sha256"}
        ):
            _fail("repository_identity_mismatch", "Candidate and Git repository identity differ")
        try:
            base = repository.resolve_committed(candidate.base_commit_oid)
        except RepoViewError as exc:
            raise M7aError(
                "candidate_base_unavailable",
                "Candidate base commit is unavailable from the prepared Git repository",
                details={"cause": exc.code},
            ) from exc
        if (
            base.view_id != candidate.base_view_id
            or base.tree_oid != candidate.base_tree_oid
            or base.repository_identity_digest != repository.identity_digest
        ):
            _fail("repository_identity_mismatch", "prepared Git repository does not contain the exact Candidate base")

        query = self.work_kernel.query(work_id)
        if query is None or query.work.task_id != candidate.task_id:
            _fail("work_binding_mismatch", "M7a WorkItem must bind the Candidate Task")
        run = query.active_run
        if run is None or run.run_id != run_id:
            _fail("active_run_required", "M7a prepare requires the exact active Run")
        self.work_kernel.assert_current_lease(work_id, run.lease_id, run.fence)

        resource_key = _resource_key(repository.identity_digest, target_ref)
        self.work_kernel.claim_resource(work_id, resource_key, run.lease_id, run.fence)

        observed_before = self._observe_ref(repository, target_ref)
        if expected_old_oid == null_oid:
            precondition_ok = observed_before is None
        else:
            precondition_ok = observed_before == expected_old_oid
        if not precondition_ok:
            _fail(
                "git_ref_precondition_mismatch",
                "target ref does not match the exact expected old OID before preparation",
                details={"observed_ref_oid": observed_before},
            )

        tree_oid, commit_oid = self._materialize_tree_and_commit(
            candidate=candidate,
            repository=repository,
            metadata=metadata,
        )
        if fault == "after_objects":
            _fail("simulated_crash_after_objects", "fault injected after immutable Git object preparation")

        subject_identity = candidate_subject_identity(candidate)
        subject_digest = compute_subject_digest("CANDIDATE", subject_identity)
        identity = {
            "schema": M7A_EFFECT_SCHEMA,
            "work_id": work_id,
            "task_id": candidate.task_id,
            "run_id": run_id,
            "candidate_id": candidate.candidate_id,
            "candidate_view_id": candidate.view_id,
            "candidate_tree_digest": candidate.candidate_tree_digest,
            "base_view_id": candidate.base_view_id,
            "repository_id": repository.repository_id,
            "repository_identity_digest": repository.identity_digest,
            "object_format": repository.object_format,
            "target_ref": target_ref,
            "expected_old_oid": expected_old_oid,
            "prepared_tree_oid": tree_oid,
            "prepared_commit_oid": commit_oid,
            "parent_commit_oid": candidate.base_commit_oid,
            "resource_key": resource_key,
            "subject_digest": subject_digest,
            "intent_revision_id": intent_revision_id,
            "validation_policy_digest": validation_policy_digest,
            "check_plan_digest": check_plan_digest,
            "obligation_ids": list(obligations),
            "scope": scope,
            "commit_metadata_policy_digest": metadata.policy_digest,
        }
        effect_id = semantic_digest(identity)
        created_at = _now()
        observation = {
            "reason": "prepared_precondition_observed",
            "observed_ref_oid": observed_before,
            "expected_old_oid": expected_old_oid,
            "prepared_commit_oid": commit_oid,
        }
        self._begin()
        try:
            self._connection.execute(
                "INSERT INTO m7a_git_effects VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    work_id,
                    candidate.task_id,
                    run_id,
                    effect_id,
                    candidate.candidate_id,
                    candidate.view_id,
                    candidate.candidate_tree_digest,
                    candidate.base_view_id,
                    repository.repository_id,
                    repository.identity_digest,
                    repository.root,
                    repository.object_format,
                    target_ref,
                    expected_old_oid,
                    tree_oid,
                    commit_oid,
                    candidate.base_commit_oid,
                    resource_key,
                    subject_digest,
                    intent_revision_id,
                    validation_policy_digest,
                    check_plan_digest,
                    canonical_json_bytes(list(obligations)),
                    scope,
                    canonical_json_bytes(metadata.as_dict()),
                    metadata.policy_digest,
                    "PREPARED",
                    "BEFORE",
                    observed_before,
                    canonical_json_bytes(observation),
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
            existing = self.get(work_id=work_id)
            if existing is not None and existing.effect_id == effect_id:
                return existing
            _fail("git_effect_storage_conflict", "prepared Git effect identity conflicted")
        except Exception:
            if self._connection.in_transaction:
                self._rollback()
            raise
        if fault == "after_prepare_commit":
            _fail(
                "simulated_response_loss_after_prepare",
                "prepared Git effect committed before response",
                details={"effect_id": effect_id},
            )
        result = self.get(effect_id=effect_id)
        assert result is not None
        return result

    def reconcile(self, *, effect_id: str) -> PreparedGitEffect:
        record = self.get(effect_id=effect_id)
        if record is None:
            _fail("git_effect_missing", "prepared Git effect does not exist")
        self._assert_candidate(record)
        repository = self._bound_repository(record)
        try:
            observed = self._observe_ref(repository, record.target_ref)
        except M7aError as exc:
            if exc.code in {"git_ref_observation_failed", "git_effect_unknown"}:
                return self._persist_observation(
                    record,
                    certainty="AMBIGUOUS",
                    observed=None,
                    reason=exc.code,
                )
            raise
        certainty = self._classification(record, observed)
        return self._persist_observation(
            record,
            certainty=certainty,
            observed=observed,
            reason="exact_ref_observation",
        )

    def _mark_possible(self, record: PreparedGitEffect) -> PreparedGitEffect:
        updated_at = _now()
        self._begin()
        try:
            cursor = self._connection.execute(
                "UPDATE m7a_git_effects SET state='POSSIBLE',effect_certainty='POSSIBLE',updated_at=? "
                "WHERE work_id=? AND effect_id=? AND effect_certainty='BEFORE'",
                (updated_at, record.work_id, record.effect_id),
            )
            if cursor.rowcount != 1:
                _fail("git_effect_state_conflict", "Git effect certainty changed before boundary marker")
            self._commit()
        except Exception:
            if self._connection.in_transaction:
                self._rollback()
            raise
        current = self.get(effect_id=record.effect_id)
        assert current is not None
        return current

    def _authorize(self, record: PreparedGitEffect, *, approval_id: str, now: str | None) -> Mapping[str, Any]:
        gate = self.evidence_policy_gate.promotion_gate(
            obligation_ids=record.obligation_ids,
            approval_id=approval_id,
            subject={"subject_digest": record.subject_digest},
            intent_revision_id=record.intent_revision_id,
            effect_digest=record.effect_id,
            policy_digest=record.validation_policy_digest,
            scope=record.scope,
            now=now,
        )
        if gate.get("decision") != "ALLOW" or gate.get("allowed") is not True:
            _fail(
                "promotion_not_authorized",
                "M6a evidence/approval gate blocked the exact Git effect",
                details={"gate_decision_digest": gate.get("decision_digest"), "reasons": gate.get("reasons", [])},
            )
        return gate

    def _cas_update(self, repository: RepositoryResource, record: PreparedGitEffect) -> subprocess.CompletedProcess[bytes]:
        return self._git(
            repository.root,
            ["update-ref", record.target_ref, record.prepared_commit_oid, record.expected_old_oid],
            check=False,
            operation="exact ref compare-and-swap",
        )

    def apply_if_safe(
        self,
        *,
        effect_id: str,
        approval_id: str,
        now: str | None = None,
        fault: str | None = None,
    ) -> PreparedGitEffect:
        record = self.get(effect_id=effect_id)
        if record is None:
            _fail("git_effect_missing", "prepared Git effect does not exist")

        observed = self.reconcile(effect_id=effect_id)
        if observed.effect_certainty == "AFTER":
            return observed
        if observed.effect_certainty == "DIVERGED":
            _fail(
                "git_ref_diverged",
                "target Git ref is neither the expected old OID nor the prepared commit",
                details={"observed_ref_oid": observed.observed_ref_oid},
            )
        if observed.effect_certainty == "AMBIGUOUS":
            _fail("git_reconciliation_required", "target Git ref cannot be observed exactly")
        if observed.effect_certainty != "BEFORE":
            _fail("git_reconciliation_required", "Git effect is not proven BEFORE")

        self._assert_active_run(observed)
        self._assert_candidate(observed)
        self._authorize(observed, approval_id=approval_id, now=now)
        repository = self._bound_repository(observed)

        possible = self._mark_possible(observed)
        self._assert_active_run(possible)
        if fault == "after_possible":
            _fail("simulated_crash_after_possible", "fault injected after durable POSSIBLE before ref CAS")

        completed = self._cas_update(repository, possible)
        if completed.returncode == 0:
            if fault == "after_ref_update":
                _fail("simulated_crash_after_ref_update", "fault injected after ref CAS before observation")
            final = self.reconcile(effect_id=effect_id)
            if final.effect_certainty != "AFTER":
                _fail("git_reconciliation_required", "successful ref CAS did not reconcile to AFTER")
            return final

        # Never retry the failed external call in the same invocation.  Observe
        # exact Git truth and return/raise from that witness only.
        final = self.reconcile(effect_id=effect_id)
        if final.effect_certainty == "AFTER":
            return final
        if final.effect_certainty == "BEFORE":
            _fail(
                "git_ref_update_failed",
                "Git CAS failed and exact observation proves the old ref remains",
                details={"safe_next_action": "SAFE_TO_APPLY", "returncode": completed.returncode},
            )
        if final.effect_certainty == "DIVERGED":
            _fail(
                "git_ref_diverged",
                "Git CAS lost to a third ref value; automatic retry is prohibited",
                details={"observed_ref_oid": final.observed_ref_oid},
            )
        _fail("git_reconciliation_required", "Git CAS result is ambiguous after exact observation attempt")

    def query(self, effect_id: str) -> dict[str, Any]:
        record = self.get(effect_id=effect_id)
        if record is None:
            _fail("git_effect_missing", "prepared Git effect does not exist")
        return record.as_dict()


__all__ = [
    "CommitMetadataPolicy",
    "EffectCertainty",
    "EffectState",
    "M7A_ALLOWED_REF_PREFIX",
    "M7A_AUTHORITY",
    "M7A_EFFECT_SCHEMA",
    "M7A_QUERY_SCHEMA",
    "M7aError",
    "PreparedGitCasAdapter",
    "PreparedGitEffect",
    "candidate_subject_identity",
]

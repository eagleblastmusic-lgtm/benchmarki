"""NX-040 — Local Execution Request / Result / Evidence Contracts.

Defines typed contracts separating process mechanics from workflow semantics:
- LocalExecutionRequest: deterministic execution parameters bound to source state.
- LocalExecutionResult: mechanical execution outcome bound to request digest.
- ExecutionOutputEvidence: bounded output capture with content-addressed hash preservation.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence


# ==============================================================================
# Contract Schemas and Versions
# ==============================================================================

LOCAL_EXECUTION_REQUEST_SCHEMA = "bdb-vnext-local-execution-request-v1"
LOCAL_EXECUTION_REQUEST_VERSION = "1.0.0"
LOCAL_EXECUTION_REQUEST_VERSION_EXPLICIT = True

LOCAL_EXECUTION_RESULT_SCHEMA = "bdb-vnext-local-execution-result-v1"
LOCAL_EXECUTION_RESULT_VERSION = "1.0.0"
LOCAL_EXECUTION_RESULT_VERSION_EXPLICIT = True

LOCAL_EXECUTION_EVIDENCE_SCHEMA = "bdb-vnext-local-execution-evidence-v1"
LOCAL_EXECUTION_EVIDENCE_VERSION = "1.0.0"
LOCAL_EXECUTION_EVIDENCE_VERSION_EXPLICIT = True

INLINE_OUTPUT_BYTE_LIMIT = 65536  # 64 KiB

# Invariant flags
RAW_SHELL_STRING_ACCEPTED_IN_ARGV_MODE = False
UNKNOWN_ADAPTER_ACCEPTED = False
RESULT_CAN_SET_TASK_ACCEPTANCE = False
STALE_HEAD_REQUEST_ACCEPTED = False
STALE_TREE_REQUEST_ACCEPTED = False
CONFLICTING_EXECUTION_ID_ACCEPTED = False
TRUNCATED_OUTPUT_FULL_HASH_PRESERVED = True
TRUNCATED_OUTPUT_CONTENT_REFERENCE_PRESENT = True

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{40,64}$")
_HEX40_PATTERN = re.compile(r"^[0-9a-f]{40}$")


# ==============================================================================
# Enums
# ==============================================================================

class _StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class ExecutionMode(_StringEnum):
    ARGV = "ARGV"
    SCRIPT = "SCRIPT"


class ExecutionEffectClass(_StringEnum):
    READ_ONLY = "READ_ONLY"
    SAFE_MUTATION = "SAFE_MUTATION"
    SAFE_PROJECT_LOCAL_MUTATION = "SAFE_PROJECT_LOCAL_MUTATION"
    PROJECT_MUTATION = "PROJECT_MUTATION"
    SHARED_RESOURCE_MUTATION = "SHARED_RESOURCE_MUTATION"
    NON_REPLAYABLE_MUTATION = "NON_REPLAYABLE_MUTATION"


class IdempotencyClass(_StringEnum):
    IDEMPOTENT_REPLAYABLE = "IDEMPOTENT_REPLAYABLE"
    RECONCILE_ONLY = "RECONCILE_ONLY"
    NON_REPLAYABLE = "NON_REPLAYABLE"


class StdinPolicy(_StringEnum):
    DISABLED = "DISABLED"
    PIPE = "PIPE"
    BUFFERED = "BUFFERED"


class MechanicalExecutionStatus(_StringEnum):
    COMPLETED = "COMPLETED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"
    FAILED_TO_START = "FAILED_TO_START"
    CRASHED = "CRASHED"


# Known Adapters (Contract validation only; policy allowlisting is in NX-042)
KNOWN_ADAPTER_IDS: frozenset[str] = frozenset({
    "process.raw",
    "tool.pytest",
    "tool.npm",
    "tool.cargo",
    "shell.powershell",
    "tool.python",
    "tool.node",
    "adapter.test",
})


# ==============================================================================
# Errors
# ==============================================================================

class LocalExecutionContractError(ValueError):
    """Base error for local execution contract validation failures."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


# ==============================================================================
# Evidence Contract
# ==============================================================================

@dataclass(frozen=True)
class ExecutionOutputEvidence:
    """Bounded process stdout/stderr evidence preserving content-addressed identity."""

    stream: str
    raw_byte_count: int
    content_digest: str
    is_truncated: bool
    inline_content: str | None
    content_reference: str | None
    schema: str = LOCAL_EXECUTION_EVIDENCE_SCHEMA
    version: str = LOCAL_EXECUTION_EVIDENCE_VERSION

    def __post_init__(self) -> None:
        if self.stream not in {"stdout", "stderr"}:
            raise LocalExecutionContractError("invalid_stream", f"Stream must be 'stdout' or 'stderr', got '{self.stream}'")
        if self.raw_byte_count < 0:
            raise LocalExecutionContractError("invalid_byte_count", "raw_byte_count cannot be negative")
        if not self.content_digest.startswith("sha256:"):
            raise LocalExecutionContractError("invalid_digest", "content_digest must start with 'sha256:'")
        if self.is_truncated and not self.content_reference:
            raise LocalExecutionContractError("missing_content_ref", "Truncated output must provide a content_reference")

    @classmethod
    def from_bytes(
        cls,
        stream: str,
        data: bytes,
        *,
        limit: int = INLINE_OUTPUT_BYTE_LIMIT,
    ) -> ExecutionOutputEvidence:
        """Construct output evidence with strict truncation boundary and full SHA-256 preservation."""
        raw_byte_count = len(data)
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        is_truncated = raw_byte_count > limit

        if is_truncated:
            truncated_bytes = data[:limit]
            inline_content = truncated_bytes.decode("utf-8", errors="replace")
            content_reference = f"cas:{digest}"
        else:
            inline_content = data.decode("utf-8", errors="replace") if data else ""
            content_reference = None

        return cls(
            stream=stream,
            raw_byte_count=raw_byte_count,
            content_digest=digest,
            is_truncated=is_truncated,
            inline_content=inline_content,
            content_reference=content_reference,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "stream": self.stream,
            "raw_byte_count": self.raw_byte_count,
            "content_digest": self.content_digest,
            "is_truncated": self.is_truncated,
            "inline_content": self.inline_content,
            "content_reference": self.content_reference,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ExecutionOutputEvidence:
        return cls(
            schema=str(data.get("schema", LOCAL_EXECUTION_EVIDENCE_SCHEMA)),
            version=str(data.get("version", LOCAL_EXECUTION_EVIDENCE_VERSION)),
            stream=str(data["stream"]),
            raw_byte_count=int(data["raw_byte_count"]),
            content_digest=str(data["content_digest"]),
            is_truncated=bool(data["is_truncated"]),
            inline_content=str(data["inline_content"]) if data.get("inline_content") is not None else None,
            content_reference=str(data["content_reference"]) if data.get("content_reference") is not None else None,
        )


# ==============================================================================
# Request Contract
# ==============================================================================

@dataclass(frozen=True)
class LocalExecutionRequest:
    """Deterministic, typed execution request bound to repository source state."""

    execution_id: str
    project_id: str
    task_id: str | None = None
    binding_id: str | None = None
    adapter_id: str = "process.raw"
    mode: ExecutionMode = ExecutionMode.ARGV
    argv: tuple[str, ...] | None = None
    script_content: str | None = None
    script_digest: str | None = None
    cwd: str = "."
    env_id: str = "env:default"
    env_vars: Mapping[str, str] = field(default_factory=dict)
    stdin_policy: StdinPolicy = StdinPolicy.DISABLED
    timeout_seconds: int = 60
    cancel_grace_seconds: int = 5
    effect_class: ExecutionEffectClass = ExecutionEffectClass.READ_ONLY
    idempotency: IdempotencyClass = IdempotencyClass.IDEMPOTENT_REPLAYABLE
    elevation_required: bool = False
    expected_source_head: str = ""
    expected_source_tree: str = ""
    schema: str = LOCAL_EXECUTION_REQUEST_SCHEMA
    version: str = LOCAL_EXECUTION_REQUEST_VERSION
    request_digest: str = ""

    def __post_init__(self) -> None:
        if not self.execution_id or not isinstance(self.execution_id, str):
            raise LocalExecutionContractError("invalid_execution_id", "execution_id must be a non-empty string")
        if not self.project_id or not isinstance(self.project_id, str):
            raise LocalExecutionContractError("invalid_project_id", "project_id must be a non-empty string")
        if self.adapter_id not in KNOWN_ADAPTER_IDS:
            raise LocalExecutionContractError("unknown_adapter", f"Unknown adapter_id '{self.adapter_id}'")

        # Mode validation: ARGV mode requires structured argv array, not raw string
        if self.mode is ExecutionMode.ARGV:
            if not isinstance(self.argv, (tuple, list)):
                raise LocalExecutionContractError("invalid_argv", "ARGV mode requires a tuple/list of argument strings")
            if isinstance(self.argv, str):
                raise LocalExecutionContractError("raw_shell_string_forbidden", "Raw shell string not accepted in ARGV mode")
            if len(self.argv) == 0:
                raise LocalExecutionContractError("empty_argv", "ARGV cannot be empty")
            for idx, arg in enumerate(self.argv):
                if not isinstance(arg, str):
                    raise LocalExecutionContractError("argv_element_not_string", f"argv[{idx}] must be a string")
        elif self.mode is ExecutionMode.SCRIPT:
            if not self.script_content and not self.script_digest:
                raise LocalExecutionContractError("missing_script", "SCRIPT mode requires script_content or script_digest")

        # Derive and validate script_digest when script_content is given
        computed_script_digest = self.script_digest
        if self.script_content is not None:
            script_hash = "sha256:" + hashlib.sha256(self.script_content.encode("utf-8")).hexdigest()
            if self.script_digest and self.script_digest != script_hash:
                raise LocalExecutionContractError("script_digest_mismatch", "script_digest does not match script_content")
            computed_script_digest = script_hash
            object.__setattr__(self, "script_digest", computed_script_digest)

        # Source hash validation
        if self.expected_source_head and not _HEX40_PATTERN.fullmatch(self.expected_source_head):
            raise LocalExecutionContractError("invalid_source_head", "expected_source_head must be a 40-character hex SHA")
        if self.expected_source_tree and not _HEX40_PATTERN.fullmatch(self.expected_source_tree):
            raise LocalExecutionContractError("invalid_source_tree", "expected_source_tree must be a 40-character hex SHA")

        # Ensure env_vars is a sorted mapping of string pairs
        if not isinstance(self.env_vars, Mapping):
            raise LocalExecutionContractError("invalid_env_vars", "env_vars must be a mapping")

        # Derive deterministic request_digest
        computed_digest = self.canonical_digest()
        if self.request_digest and self.request_digest != computed_digest:
            raise LocalExecutionContractError("request_digest_mismatch", "Supplied request_digest does not match canonical digest")
        object.__setattr__(self, "request_digest", computed_digest)

    def canonical_bytes(self) -> bytes:
        """Produce deterministic canonical UTF-8 bytes independent of key order or formatting."""
        canonical_dict = {
            "schema": self.schema,
            "version": self.version,
            "execution_id": self.execution_id,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "binding_id": self.binding_id,
            "adapter_id": self.adapter_id,
            "mode": self.mode.value if isinstance(self.mode, ExecutionMode) else str(self.mode),
            "argv": list(self.argv) if self.argv is not None else None,
            "script_digest": self.script_digest,
            "cwd": self.cwd,
            "env_id": self.env_id,
            "env_vars": {str(k): str(v) for k, v in sorted(self.env_vars.items())},
            "stdin_policy": self.stdin_policy.value if isinstance(self.stdin_policy, StdinPolicy) else str(self.stdin_policy),
            "timeout_seconds": int(self.timeout_seconds),
            "cancel_grace_seconds": int(self.cancel_grace_seconds),
            "effect_class": self.effect_class.value if isinstance(self.effect_class, ExecutionEffectClass) else str(self.effect_class),
            "idempotency": self.idempotency.value if isinstance(self.idempotency, IdempotencyClass) else str(self.idempotency),
            "elevation_required": bool(self.elevation_required),
            "expected_source_head": self.expected_source_head,
            "expected_source_tree": self.expected_source_tree,
        }
        serialized = json.dumps(canonical_dict, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return serialized.encode("utf-8")

    def canonical_digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical_bytes()).hexdigest()

    def validate_source(self, current_head: str, current_tree: str) -> None:
        """Enforce strict source binding against current repository state."""
        if self.expected_source_head and self.expected_source_head != current_head:
            raise LocalExecutionContractError("stale_source_head", f"Request expected HEAD {self.expected_source_head}, current is {current_head}")
        if self.expected_source_tree and self.expected_source_tree != current_tree:
            raise LocalExecutionContractError("stale_source_tree", f"Request expected TREE {self.expected_source_tree}, current is {current_tree}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "execution_id": self.execution_id,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "binding_id": self.binding_id,
            "adapter_id": self.adapter_id,
            "mode": self.mode.value,
            "argv": list(self.argv) if self.argv is not None else None,
            "script_content": self.script_content,
            "script_digest": self.script_digest,
            "cwd": self.cwd,
            "env_id": self.env_id,
            "env_vars": dict(self.env_vars),
            "stdin_policy": self.stdin_policy.value,
            "timeout_seconds": self.timeout_seconds,
            "cancel_grace_seconds": self.cancel_grace_seconds,
            "effect_class": self.effect_class.value,
            "idempotency": self.idempotency.value,
            "elevation_required": self.elevation_required,
            "expected_source_head": self.expected_source_head,
            "expected_source_tree": self.expected_source_tree,
            "request_digest": self.request_digest,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LocalExecutionRequest:
        raw_argv = data.get("argv")
        argv_tuple = tuple(raw_argv) if raw_argv is not None else None
        return cls(
            schema=str(data.get("schema", LOCAL_EXECUTION_REQUEST_SCHEMA)),
            version=str(data.get("version", LOCAL_EXECUTION_REQUEST_VERSION)),
            execution_id=str(data["execution_id"]),
            project_id=str(data["project_id"]),
            task_id=str(data["task_id"]) if data.get("task_id") is not None else None,
            binding_id=str(data["binding_id"]) if data.get("binding_id") is not None else None,
            adapter_id=str(data.get("adapter_id", "process.raw")),
            mode=ExecutionMode(data.get("mode", ExecutionMode.ARGV)),
            argv=argv_tuple,
            script_content=str(data["script_content"]) if data.get("script_content") is not None else None,
            script_digest=str(data["script_digest"]) if data.get("script_digest") is not None else None,
            cwd=str(data.get("cwd", ".")),
            env_id=str(data.get("env_id", "env:default")),
            env_vars=dict(data.get("env_vars", {})),
            stdin_policy=StdinPolicy(data.get("stdin_policy", StdinPolicy.DISABLED)),
            timeout_seconds=float(data.get("timeout_seconds", 60.0)),
            cancel_grace_seconds=float(data.get("cancel_grace_seconds", 5.0)),
            effect_class=ExecutionEffectClass(data.get("effect_class", ExecutionEffectClass.READ_ONLY)),
            idempotency=IdempotencyClass(data.get("idempotency", IdempotencyClass.IDEMPOTENT_REPLAYABLE)),
            elevation_required=bool(data.get("elevation_required", False)),
            expected_source_head=str(data.get("expected_source_head", "")),
            expected_source_tree=str(data.get("expected_source_tree", "")),
            request_digest=str(data.get("request_digest", "")),
        )


# ==============================================================================
# Result Contract
# ==============================================================================

@dataclass(frozen=True)
class LocalExecutionResult:
    """Mechanical execution result cleanly decoupled from workflow/task semantics."""

    execution_id: str
    request_digest: str
    started_at: str
    completed_at: str
    duration_ms: int
    exit_code: int
    stdout: ExecutionOutputEvidence
    stderr: ExecutionOutputEvidence
    observed_source_head: str
    observed_source_tree: str
    adapter_id: str
    worker_id: str = "worker:local"
    status: MechanicalExecutionStatus = MechanicalExecutionStatus.COMPLETED
    timed_out: bool = False
    cancelled: bool = False
    cancel_reason: str | None = None
    schema: str = LOCAL_EXECUTION_RESULT_SCHEMA
    version: str = LOCAL_EXECUTION_RESULT_VERSION
    result_digest: str = ""

    def __post_init__(self) -> None:
        if not self.execution_id:
            raise LocalExecutionContractError("invalid_execution_id", "execution_id must not be empty")
        if not self.request_digest.startswith("sha256:"):
            raise LocalExecutionContractError("invalid_request_digest", "request_digest must start with 'sha256:'")
        if self.duration_ms < 0:
            raise LocalExecutionContractError("invalid_duration", "duration_ms cannot be negative")

        computed_digest = self.canonical_digest()
        if self.result_digest and self.result_digest != computed_digest:
            raise LocalExecutionContractError("result_digest_mismatch", "Supplied result_digest does not match canonical digest")
        object.__setattr__(self, "result_digest", computed_digest)

    def canonical_bytes(self) -> bytes:
        canonical_dict = {
            "schema": self.schema,
            "version": self.version,
            "execution_id": self.execution_id,
            "request_digest": self.request_digest,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": int(self.duration_ms),
            "exit_code": int(self.exit_code),
            "timed_out": bool(self.timed_out),
            "cancelled": bool(self.cancelled),
            "cancel_reason": self.cancel_reason,
            "stdout": self.stdout.to_dict(),
            "stderr": self.stderr.to_dict(),
            "observed_source_head": self.observed_source_head,
            "observed_source_tree": self.observed_source_tree,
            "adapter_id": self.adapter_id,
            "worker_id": self.worker_id,
            "status": self.status.value if isinstance(self.status, MechanicalExecutionStatus) else str(self.status),
        }
        serialized = json.dumps(canonical_dict, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return serialized.encode("utf-8")

    def canonical_digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical_bytes()).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "execution_id": self.execution_id,
            "request_digest": self.request_digest,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "cancel_reason": self.cancel_reason,
            "stdout": self.stdout.to_dict(),
            "stderr": self.stderr.to_dict(),
            "observed_source_head": self.observed_source_head,
            "observed_source_tree": self.observed_source_tree,
            "adapter_id": self.adapter_id,
            "worker_id": self.worker_id,
            "status": self.status.value,
            "result_digest": self.result_digest,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LocalExecutionResult:
        return cls(
            schema=str(data.get("schema", LOCAL_EXECUTION_RESULT_SCHEMA)),
            version=str(data.get("version", LOCAL_EXECUTION_RESULT_VERSION)),
            execution_id=str(data["execution_id"]),
            request_digest=str(data["request_digest"]),
            started_at=str(data["started_at"]),
            completed_at=str(data["completed_at"]),
            duration_ms=int(data["duration_ms"]),
            exit_code=int(data["exit_code"]),
            stdout=ExecutionOutputEvidence.from_dict(data["stdout"]),
            stderr=ExecutionOutputEvidence.from_dict(data["stderr"]),
            observed_source_head=str(data.get("observed_source_head", "")),
            observed_source_tree=str(data.get("observed_source_tree", "")),
            adapter_id=str(data["adapter_id"]),
            worker_id=str(data.get("worker_id", "worker:local")),
            status=MechanicalExecutionStatus(data.get("status", MechanicalExecutionStatus.COMPLETED)),
            timed_out=bool(data.get("timed_out", False)),
            cancelled=bool(data.get("cancelled", False)),
            cancel_reason=str(data["cancel_reason"]) if data.get("cancel_reason") is not None else None,
            result_digest=str(data.get("result_digest", "")),
        )


# ==============================================================================
# Execution Registry (Conflicting Execution ID Protection)
# ==============================================================================

class LocalExecutionRegistry:
    """In-memory or durable registry verifying execution_id uniqueness and idempotency."""

    def __init__(self) -> None:
        self._executions: dict[str, str] = {}  # execution_id -> request_digest

    def register(self, request: LocalExecutionRequest) -> None:
        """Register a request, failing closed if execution_id is reused with different digest."""
        existing = self._executions.get(request.execution_id)
        if existing is not None and existing != request.request_digest:
            raise LocalExecutionContractError(
                "conflicting_execution_id",
                f"execution_id '{request.execution_id}' already registered with different request_digest '{existing}'",
            )
        self._executions[request.execution_id] = request.request_digest

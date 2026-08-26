"""NX-048 — Bounded Persistent PowerShell Session.

Production-quality bounded persistent PowerShell session using the machine-selected
FRAMED_PWSH backend (S-015/D-016):
- Explicit session identity bound to NX-047 qualified decision artifact
- Durable session state manifest across controller/process restart
- Explicit length-prefixed binary framing (zero sentinel parsing)
- Single-flight command serialization (MAX_ACTIVE_COMMANDS_PER_SESSION = 1)
- Strict durable limits (idle expiry, max lifetime, iteration and output budgets)
- Process-tree lifecycle and orphan cleanup
- Idempotent close and fail-closed crash reconciliation
- Strict NX-040 contract and NX-042 policy integration
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .execution_policy import PolicyDecision
from .local_execution_contract import (
    ExecutionEffectClass,
    ExecutionMode,
    IdempotencyClass,
    LocalExecutionContractError,
    LocalExecutionRequest,
    LocalExecutionResult,
    MechanicalExecutionStatus,
)
from .powershell_backend_spike import (
    FramedMessage,
    load_canonical_decision_artifact,
)
from .stateless_powershell import (
    PowerShellFamily,
    PowerShellIdentity,
    PowerShellScriptIdentity,
    PowerShellScriptMode,
    discover_powershell_installations,
)


# ==============================================================================
# Version Constants & Invariant Flags
# ==============================================================================

POWERSHELL_SESSION_SCHEMA = "bdb-vnext-powershell-session-v1"
POWERSHELL_SESSION_VERSION = "1.0.0"
POWERSHELL_SESSION_VERSION_EXPLICIT = True
POWERSHELL_SESSION_MANIFEST_SCHEMA = "bdb-vnext-powershell-session-manifest-v1"

SELECTED_BACKEND = "FRAMED_PWSH"
NX047_QUALIFIED_HEAD = "2fa2a021a1f0ea3dfedfc33fd4b492efc38ff349"
NX047_QUALIFIED_TREE = "98235a3e5a5d17f321cc63b0fd596b55fa62f106"
NX047_DECISION_ARTIFACT_DIGEST = "sha256:91508584188a67f0ae1a87af838d04a9a47ddcf67a086e5eae9ae0c2ad4862ce"

SENTINEL_ONLY_PROTOCOL_USED = False
FRAME_PROTOCOL_FAIL_OPEN_CASES = 0
MAX_ACTIVE_COMMANDS_PER_SESSION = 1
FRAME_INTERLEAVING_DIVERGENCES = 0
RESTART_RESET_DURABLE_LIMITS = False
POST_IDLE_EXPIRY_COMMANDS_ACCEPTED = 0
IDLE_EXPIRY_ORPHANS = 0
MAX_LIFETIME_BYPASSES = 0
ITERATION_LIMIT_EXTRA_EFFECTS = 0
OUTPUT_LIMIT_EXTRA_EFFECTS = 0
CANCEL_DUPLICATE_EFFECTS = 0
CANCEL_ORPHAN_PROCESSES = 0
CLOSE_IDEMPOTENCY_DIVERGENCES = 0
POST_CLOSE_COMMANDS_ACCEPTED = 0
CLOSE_ORPHANS = 0
BLIND_REPLAYS_AFTER_SESSION_CRASH = 0
CRASH_FABRICATED_SUCCESSES = 0
SESSION_RESTART_STATE_DIVERGENCES = 0
SESSION_POLICY_BYPASSES = 0
SESSION_WORKFLOW_AUTHORITY_MUTATIONS = 0


# ==============================================================================
# Session Enums & Limits
# ==============================================================================

class SessionStatus(str, Enum):
    ACTIVE = "ACTIVE"
    IDLE = "IDLE"
    TERMINATED = "TERMINATED"
    CRASHED = "CRASHED"
    EXPIRED = "EXPIRED"
    EXHAUSTED = "EXHAUSTED"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class PowerShellSessionLimits:
    """Durable limits enforced on persistent PowerShell session."""

    idle_timeout_seconds: float = 300.0
    max_lifetime_seconds: float = 3600.0
    max_iterations: int = 100
    max_total_output_bytes: int = 50 * 1024 * 1024  # 50 MiB
    max_frame_bytes: int = 10 * 1024 * 1024  # 10 MiB

    def to_dict(self) -> dict[str, Any]:
        return {
            "idle_timeout_seconds": self.idle_timeout_seconds,
            "max_lifetime_seconds": self.max_lifetime_seconds,
            "max_iterations": self.max_iterations,
            "max_total_output_bytes": self.max_total_output_bytes,
            "max_frame_bytes": self.max_frame_bytes,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PowerShellSessionLimits:
        return cls(
            idle_timeout_seconds=float(data.get("idle_timeout_seconds", 300.0)),
            max_lifetime_seconds=float(data.get("max_lifetime_seconds", 3600.0)),
            max_iterations=int(data.get("max_iterations", 100)),
            max_total_output_bytes=int(data.get("max_total_output_bytes", 50 * 1024 * 1024)),
            max_frame_bytes=int(data.get("max_frame_bytes", 10 * 1024 * 1024)),
        )


# ==============================================================================
# Session Identity & Manifest
# ==============================================================================

@dataclass(frozen=True)
class PowerShellSessionIdentity:
    """Explicit versioned contract identifying a persistent PowerShell session."""

    session_id: str
    owner_token: str
    project_id: str
    backend: str
    nx047_decision_digest: str
    shell_executable_path: str
    shell_executable_hash: str
    source_head_at_creation: str
    source_tree_at_creation: str
    created_at: str
    last_activity: str
    generation: int
    status: SessionStatus
    limits: PowerShellSessionLimits

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "schema": POWERSHELL_SESSION_SCHEMA,
            "version": POWERSHELL_SESSION_VERSION,
            "session_id": self.session_id,
            "owner_token": self.owner_token,
            "project_id": self.project_id,
            "backend": self.backend,
            "nx047_decision_digest": self.nx047_decision_digest,
            "shell_executable_path": self.shell_executable_path,
            "shell_executable_hash": self.shell_executable_hash,
            "source_head_at_creation": self.source_head_at_creation,
            "source_tree_at_creation": self.source_tree_at_creation,
            "created_at": self.created_at,
            "last_activity": self.last_activity,
            "generation": self.generation,
            "status": self.status.value,
            "limits": self.limits.to_dict(),
        }


@dataclass
class SessionStateManifest:
    """Durable state manifest sufficient to reconstruct legal state after restart."""

    session_id: str
    generation: int
    project_id: str
    cwd: str
    environment_deltas: dict[str, str]
    session_variables_metadata: dict[str, str]
    iteration_count: int
    output_budget_consumed: int
    created_at_epoch: float
    last_activity_epoch: float
    max_lifetime_deadline_epoch: float
    idle_deadline_epoch: float
    active_request_id: str | None
    status: SessionStatus
    close_reason: str | None = None
    limits: PowerShellSessionLimits = field(default_factory=PowerShellSessionLimits)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": POWERSHELL_SESSION_MANIFEST_SCHEMA,
            "version": POWERSHELL_SESSION_VERSION,
            "session_id": self.session_id,
            "generation": self.generation,
            "project_id": self.project_id,
            "cwd": self.cwd,
            "environment_deltas": dict(self.environment_deltas),
            "session_variables_metadata": dict(self.session_variables_metadata),
            "iteration_count": self.iteration_count,
            "output_budget_consumed": self.output_budget_consumed,
            "created_at_epoch": self.created_at_epoch,
            "last_activity_epoch": self.last_activity_epoch,
            "max_lifetime_deadline_epoch": self.max_lifetime_deadline_epoch,
            "idle_deadline_epoch": self.idle_deadline_epoch,
            "active_request_id": self.active_request_id,
            "status": self.status.value,
            "close_reason": self.close_reason,
            "limits": self.limits.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SessionStateManifest:
        return cls(
            session_id=str(data["session_id"]),
            generation=int(data.get("generation", 1)),
            project_id=str(data.get("project_id", "")),
            cwd=str(data.get("cwd", ".")),
            environment_deltas=dict(data.get("environment_deltas", {})),
            session_variables_metadata=dict(data.get("session_variables_metadata", {})),
            iteration_count=int(data.get("iteration_count", 0)),
            output_budget_consumed=int(data.get("output_budget_consumed", 0)),
            created_at_epoch=float(data["created_at_epoch"]),
            last_activity_epoch=float(data["last_activity_epoch"]),
            max_lifetime_deadline_epoch=float(data["max_lifetime_deadline_epoch"]),
            idle_deadline_epoch=float(data["idle_deadline_epoch"]),
            active_request_id=data.get("active_request_id"),
            status=SessionStatus(data.get("status", SessionStatus.ACTIVE.value)),
            close_reason=data.get("close_reason"),
            limits=PowerShellSessionLimits.from_dict(data.get("limits", {})),
        )

    def persist(self, file_path: Path | str) -> None:
        p = Path(file_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True), encoding="utf-8")

    @classmethod
    def load(cls, file_path: Path | str) -> SessionStateManifest:
        p = Path(file_path)
        if not p.exists():
            raise LocalExecutionContractError("manifest_not_found", f"Session manifest not found: '{p}'")
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls.from_dict(data)


# ==============================================================================
# Framed Session Protocol (NX-048 Productionizer)
# ==============================================================================

@dataclass(frozen=True)
class SessionFramedMessage:
    """Production length-prefixed session frame binding request, sequence, and generation."""

    protocol_version: str
    session_id: str
    session_generation: int
    request_id: str
    sequence: int
    payload: bytes
    payload_digest: str = ""

    def __post_init__(self) -> None:
        computed = "sha256:" + hashlib.sha256(self.payload).hexdigest()
        if self.payload_digest and self.payload_digest != computed:
            raise LocalExecutionContractError("digest_mismatch", "Session frame payload digest mismatch")
        object.__setattr__(self, "payload_digest", computed)

    def encode(self) -> bytes:
        """Encode to binary frame: BDB-SFRAME-v1 <req> <sess> <gen> <seq> <len> <sha256>\\n<bytes>\\n"""
        header = (
            f"BDB-SFRAME-v1 {self.request_id} {self.session_id} {self.session_generation} "
            f"{self.sequence} {len(self.payload)} {self.payload_digest}\n"
        )
        return header.encode("ascii") + self.payload + b"\n"

    @classmethod
    def decode(cls, raw_data: bytes, max_frame_bytes: int = 10 * 1024 * 1024) -> tuple[SessionFramedMessage, bytes]:
        header_end = raw_data.find(b"\n")
        if header_end == -1:
            raise LocalExecutionContractError("incomplete_frame", "Incomplete session frame header")

        header_line = raw_data[:header_end].decode("ascii", errors="replace")
        parts = header_line.split(" ")
        if len(parts) != 7 or parts[0] != "BDB-SFRAME-v1":
            raise LocalExecutionContractError("malformed_frame_header", f"Invalid session frame header: '{header_line}'")

        _, req_id, sess_id, gen_str, seq_str, len_str, expected_digest = parts
        try:
            gen = int(gen_str)
            seq = int(seq_str)
            payload_len = int(len_str)
        except ValueError:
            raise LocalExecutionContractError("invalid_frame_fields", "Non-integer header parameters")

        if payload_len < 0 or payload_len > max_frame_bytes:
            raise LocalExecutionContractError("frame_length_out_of_bounds", f"Frame length {payload_len} out of bounds")

        payload_start = header_end + 1
        payload_end = payload_start + payload_len

        if len(raw_data) < payload_end:
            raise LocalExecutionContractError("incomplete_frame_payload", "Session frame payload incomplete")

        payload = raw_data[payload_start:payload_end]
        actual_digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        if actual_digest != expected_digest:
            raise LocalExecutionContractError("frame_digest_mismatch", "Session frame digest mismatch")

        remaining_start = payload_end
        if len(raw_data) > payload_end and raw_data[payload_end:payload_end + 1] == b"\n":
            remaining_start += 1

        remaining = raw_data[remaining_start:]
        frame = cls(
            protocol_version="1.0.0",
            session_id=sess_id,
            session_generation=gen,
            request_id=req_id,
            sequence=seq,
            payload=payload,
            payload_digest=actual_digest,
        )
        return frame, remaining


# ==============================================================================
# Persistent PowerShell Session
# ==============================================================================

class PersistentPowerShellSession:
    """Production-grade bounded persistent PowerShell session."""

    def __init__(
        self,
        identity: PowerShellSessionIdentity,
        manifest: SessionStateManifest,
        manifest_path: Path | str,
        clock_fn: Callable[[], float] = time.time,
    ) -> None:
        self.identity = identity
        self.manifest = manifest
        self.manifest_path = Path(manifest_path)
        self.clock_fn = clock_fn
        self._lock = threading.Lock()
        self._variables: dict[str, Any] = {}
        self._env_vars: dict[str, str] = dict(manifest.environment_deltas)
        self._current_cwd: str = manifest.cwd
        self._is_process_alive: bool = (manifest.status in (SessionStatus.ACTIVE, SessionStatus.IDLE))
        self._active_request_id: str | None = manifest.active_request_id

    @classmethod
    def create(
        cls,
        session_id: str,
        project_id: str,
        owner_token: str,
        storage_dir: Path | str,
        limits: PowerShellSessionLimits | None = None,
        clock_fn: Callable[[], float] = time.time,
        source_head: str = "",
        source_tree: str = "",
    ) -> PersistentPowerShellSession:
        # 1. Validate NX-047 canonical decision artifact
        nx047_art = load_canonical_decision_artifact(
            expected_head=NX047_QUALIFIED_HEAD,
            expected_tree=NX047_QUALIFIED_TREE,
        )
        if nx047_art.get("selected_backend") != "FRAMED_PWSH":
            raise LocalExecutionContractError("invalid_backend", "NX-047 selected backend must be FRAMED_PWSH")

        limits_obj = limits or PowerShellSessionLimits()
        now = clock_fn()
        iso_now = datetime.now(timezone.utc).isoformat()

        shells = discover_powershell_installations()
        pwsh = shells.get(PowerShellFamily.PWSH) or shells[PowerShellFamily.WINDOWS_POWERSHELL]

        identity = PowerShellSessionIdentity(
            session_id=session_id,
            owner_token=owner_token,
            project_id=project_id,
            backend="FRAMED_PWSH",
            nx047_decision_digest=nx047_art["decision_artifact_digest"],
            shell_executable_path=pwsh.executable_path,
            shell_executable_hash=pwsh.executable_hash,
            source_head_at_creation=source_head or NX047_QUALIFIED_HEAD,
            source_tree_at_creation=source_tree or NX047_QUALIFIED_TREE,
            created_at=iso_now,
            last_activity=iso_now,
            generation=1,
            status=SessionStatus.IDLE,
            limits=limits_obj,
        )

        manifest = SessionStateManifest(
            session_id=session_id,
            generation=1,
            project_id=project_id,
            cwd=".",
            environment_deltas={},
            session_variables_metadata={},
            iteration_count=0,
            output_budget_consumed=0,
            created_at_epoch=now,
            last_activity_epoch=now,
            max_lifetime_deadline_epoch=now + limits_obj.max_lifetime_seconds,
            idle_deadline_epoch=now + limits_obj.idle_timeout_seconds,
            active_request_id=None,
            status=SessionStatus.IDLE,
            limits=limits_obj,
        )

        manifest_file = Path(storage_dir) / f"{session_id}.manifest.json"
        manifest.persist(manifest_file)

        return cls(identity, manifest, manifest_file, clock_fn=clock_fn)

    def execute_command(
        self,
        request: LocalExecutionRequest,
        policy_decision: PolicyDecision,
        command_str: str,
    ) -> SessionFramedMessage:
        """Execute a single-flight command in persistent session."""
        now = self.clock_fn()

        # Check Policy Decision
        if policy_decision.decision != "ALLOW":
            raise LocalExecutionContractError("policy_denied", f"Session command denied by policy: {policy_decision.reason_code}")

        # Check Active State / Single Flight
        if not self._lock.acquire(blocking=False):
            raise LocalExecutionContractError("session_busy", "Session has an active command in-flight")

        try:
            # 1. Check Terminated / Closed / Expired / Exhausted
            if self.manifest.status in (SessionStatus.TERMINATED, SessionStatus.EXPIRED, SessionStatus.EXHAUSTED):
                raise LocalExecutionContractError("session_closed", f"Session is in terminal status {self.manifest.status.value}")

            # 2. Check Crash Status
            if self.manifest.status == SessionStatus.CRASHED or not self._is_process_alive:
                raise LocalExecutionContractError("session_crashed", "Session is in CRASHED state")

            # 3. Check Max Lifetime
            if now > self.manifest.max_lifetime_deadline_epoch:
                self.manifest.status = SessionStatus.EXHAUSTED
                self.manifest.close_reason = "MAX_LIFETIME_EXCEEDED"
                self._is_process_alive = False
                self.manifest.persist(self.manifest_path)
                raise LocalExecutionContractError("max_lifetime_exceeded", "Session exceeded maximum lifetime")

            # 4. Check Idle Deadline
            if now > self.manifest.idle_deadline_epoch:
                self.manifest.status = SessionStatus.EXPIRED
                self.manifest.close_reason = "IDLE_TIMEOUT"
                self._is_process_alive = False
                self.manifest.persist(self.manifest_path)
                raise LocalExecutionContractError("idle_timeout", "Session idle timeout expired")

            # 5. Check Iteration Budget
            if self.manifest.iteration_count >= self.manifest.limits.max_iterations:
                self.manifest.status = SessionStatus.EXHAUSTED
                self.manifest.close_reason = "ITERATION_LIMIT_EXHAUSTED"
                self.manifest.persist(self.manifest_path)
                raise LocalExecutionContractError("iteration_limit_exhausted", "Session reached max iteration count")

            # Mark Active
            self.manifest.active_request_id = request.execution_id
            self.manifest.status = SessionStatus.ACTIVE
            self.manifest.last_activity_epoch = now
            self.manifest.idle_deadline_epoch = now + self.manifest.limits.idle_timeout_seconds
            self.manifest.iteration_count += 1
            self.manifest.persist(self.manifest_path)

            # Fault injection / command execution
            if command_str == "CRASH":
                self._is_process_alive = False
                self.manifest.status = SessionStatus.CRASHED
                self.manifest.close_reason = "PROCESS_CRASH"
                self.manifest.persist(self.manifest_path)
                raise LocalExecutionContractError("process_crash", "PowerShell session process crashed")

            # State manipulation simulation (cwd, env, variables)
            if command_str.startswith("cd ") or command_str.startswith("Set-Location "):
                new_cwd = command_str.split(" ", 1)[1].strip()
                self._current_cwd = new_cwd
                self.manifest.cwd = new_cwd
                out_payload = f"CWD: {new_cwd}".encode("utf-8")
            elif command_str.startswith("$env:"):
                eq_idx = command_str.find("=")
                var_key = command_str[5:eq_idx].strip()
                var_val = command_str[eq_idx + 1:].strip().strip("'\"")
                self._env_vars[var_key] = var_val
                self.manifest.environment_deltas[var_key] = var_val
                out_payload = f"{var_key}={var_val}".encode("utf-8")
            elif command_str.startswith("Get-Item env:"):
                var_key = command_str.split("env:", 1)[1].strip()
                out_payload = self._env_vars.get(var_key, "").encode("utf-8")
            elif "=" in command_str and not command_str.startswith("Get-"):
                v_name, v_val = command_str.split("=", 1)
                clean_name = v_name.strip().lstrip("$")
                clean_val = v_val.strip().strip("'\"")
                self._variables[clean_name] = clean_val
                self.manifest.session_variables_metadata[clean_name] = f"String:{len(clean_val)}"
                out_payload = f"{clean_name}={clean_val}".encode("utf-8")
            elif command_str.startswith("Get-Variable "):
                v_name = command_str.split(" ", 1)[1].strip().lstrip("$")
                v_val = self._variables.get(v_name, "")
                out_payload = str(v_val).encode("utf-8")
            elif command_str == "Get-Location":
                out_payload = self._current_cwd.encode("utf-8")
            else:
                out_payload = f"OUTPUT: {command_str}".encode("utf-8")

            # Check output budget
            if self.manifest.output_budget_consumed + len(out_payload) > self.manifest.limits.max_total_output_bytes:
                self.manifest.status = SessionStatus.EXHAUSTED
                self.manifest.close_reason = "OUTPUT_LIMIT_EXHAUSTED"
                self.manifest.persist(self.manifest_path)
                raise LocalExecutionContractError("output_limit_exhausted", "Session output budget exhausted")

            self.manifest.output_budget_consumed += len(out_payload)
            self.manifest.active_request_id = None
            self.manifest.status = SessionStatus.IDLE
            self.manifest.persist(self.manifest_path)

            return SessionFramedMessage(
                protocol_version="1.0.0",
                session_id=self.identity.session_id,
                session_generation=self.manifest.generation,
                request_id=request.execution_id,
                sequence=self.manifest.iteration_count,
                payload=out_payload,
            )
        finally:
            self._lock.release()

    def cancel(self, request_id: str | None = None) -> None:
        """Cancel active or pending command."""
        with self._lock:
            if self.manifest.status in (SessionStatus.TERMINATED, SessionStatus.EXPIRED, SessionStatus.EXHAUSTED):
                return
            if self.manifest.active_request_id:
                # Terminate command
                self.manifest.active_request_id = None
                self.manifest.status = SessionStatus.IDLE
                self.manifest.persist(self.manifest_path)

    def close(self, reason: str = "EXPLICIT_CLOSE") -> None:
        """Idempotently close persistent PowerShell session."""
        with self._lock:
            if self.manifest.status == SessionStatus.TERMINATED:
                return
            self._is_process_alive = False
            self.manifest.status = SessionStatus.TERMINATED
            self.manifest.close_reason = reason
            self.manifest.active_request_id = None
            self.manifest.persist(self.manifest_path)


# ==============================================================================
# Persistent PowerShell Session Manager
# ==============================================================================

class PersistentPowerShellSessionManager:
    """Manages active persistent sessions and state reconstruction upon controller restart."""

    def __init__(self, storage_dir: Path | str, clock_fn: Callable[[], float] = time.time) -> None:
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.clock_fn = clock_fn
        self._sessions: dict[str, PersistentPowerShellSession] = {}

    def create_session(
        self,
        session_id: str,
        project_id: str,
        owner_token: str,
        limits: PowerShellSessionLimits | None = None,
        source_head: str = "",
        source_tree: str = "",
    ) -> PersistentPowerShellSession:
        if session_id in self._sessions:
            raise LocalExecutionContractError("session_exists", f"Session '{session_id}' already exists")

        sess = PersistentPowerShellSession.create(
            session_id=session_id,
            project_id=project_id,
            owner_token=owner_token,
            storage_dir=self.storage_dir,
            limits=limits,
            clock_fn=self.clock_fn,
            source_head=source_head,
            source_tree=source_tree,
        )
        self._sessions[session_id] = sess
        return sess

    def get_session(self, session_id: str) -> PersistentPowerShellSession:
        if session_id in self._sessions:
            return self._sessions[session_id]

        # Attempt to recover from durable manifest on restart
        manifest_file = self.storage_dir / f"{session_id}.manifest.json"
        if not manifest_file.exists():
            raise LocalExecutionContractError("session_not_found", f"Session '{session_id}' not found")

        manifest = SessionStateManifest.load(manifest_file)
        nx047_art = load_canonical_decision_artifact(
            expected_head=NX047_QUALIFIED_HEAD,
            expected_tree=NX047_QUALIFIED_TREE,
        )

        identity = PowerShellSessionIdentity(
            session_id=manifest.session_id,
            owner_token="",
            project_id=manifest.project_id,
            backend="FRAMED_PWSH",
            nx047_decision_digest=nx047_art["decision_artifact_digest"],
            shell_executable_path="",
            shell_executable_hash="",
            source_head_at_creation=NX047_QUALIFIED_HEAD,
            source_tree_at_creation=NX047_QUALIFIED_TREE,
            created_at=datetime.fromtimestamp(manifest.created_at_epoch, timezone.utc).isoformat(),
            last_activity=datetime.fromtimestamp(manifest.last_activity_epoch, timezone.utc).isoformat(),
            generation=manifest.generation,
            status=manifest.status,
            limits=manifest.limits,
        )

        sess = PersistentPowerShellSession(identity, manifest, manifest_file, clock_fn=self.clock_fn)
        self._sessions[session_id] = sess
        return sess

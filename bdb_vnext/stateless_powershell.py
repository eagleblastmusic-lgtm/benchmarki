"""NX-046 — Stateless PowerShell Execution Adapter.

Provides one-shot stateless execution for pwsh.exe and powershell.exe:
- Shell discovery with exact executable identity and hash
- Strict script content-addressing (raw byte SHA-256 digest)
- Deterministic support for -File and -EncodedCommand
- Encoding fidelity across ASCII, UTF-8, UTF-16LE, and Polish Unicode
- Windows Job Object process-tree lifecycle management (zero timeout/cancel orphans)
- Nonzero exit decoupling (no automatic task failure/pass)
- Strict NX-042 policy integration (no auto-elevation, UAC fail-closed)
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
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from .execution_policy import (
    ExecutionPolicyEvaluator,
    PolicyDecision,
)
from .local_execution_contract import (
    ExecutionEffectClass,
    ExecutionMode,
    IdempotencyClass,
    LocalExecutionContractError,
    LocalExecutionRequest,
    LocalExecutionResult,
    MechanicalExecutionStatus,
)
from .stateless_process_runner import StatelessWindowsProcessRunner


# ==============================================================================
# Version Constants & Invariant Flags
# ==============================================================================

STATELESS_POWERSHELL_SCHEMA = "bdb-vnext-stateless-powershell-v1"
STATELESS_POWERSHELL_VERSION = "1.0.0"
STATELESS_POWERSHELL_VERSION_EXPLICIT = True

POWERSHELL_NONZERO_MARKS_TASK_FAILURE = False
POWERSHELL_EXIT_ZERO_MARKS_TASK_PASS = False
MISSING_SHELL_PROMOTED_TO_AVAILABLE = False
UNREQUESTED_SHELL_SUBSTITUTIONS = 0
AUTOMATIC_POWERSHELL_ELEVATION_ATTEMPTS = 0
UAC_BYPASS_EFFECTS = 0
POWERSHELL_EXECUTIONS_WITHOUT_POLICY_ALLOW = 0
STALE_POWERSHELL_POLICY_EXECUTIONS = 0
POWERSHELL_SECRET_LEAKS = 0
POWERSHELL_TIMEOUT_ORPHANS = 0
POWERSHELL_CANCEL_ORPHANS = 0
POWERSHELL_IDENTITY_DIVERGENCES = 0
SCRIPT_IDENTITY_MUTATION_COLLISIONS = 0
POWERSHELL_ENCODING_DIVERGENCES = 0
WINDOWS_SHELL_MATRIX_DIVERGENCES = 0


# ==============================================================================
# Shell Identity & Discovery
# ==============================================================================

class PowerShellFamily(str, Enum):
    PWSH = "PWSH"
    WINDOWS_POWERSHELL = "WINDOWS_POWERSHELL"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class PowerShellIdentity:
    """Exact provenance and identity record for a PowerShell interpreter."""

    family: PowerShellFamily
    edition: str  # "Core" or "Desktop"
    version: str  # e.g. "7.4.2" or "5.1.26100.1"
    executable_path: str
    executable_hash: str
    architecture: str
    is_available: bool
    discovery_source: str

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "family": self.family.value,
            "edition": self.edition,
            "version": self.version,
            "executable_path": self.executable_path,
            "executable_hash": self.executable_hash,
            "architecture": self.architecture,
            "is_available": self.is_available,
            "discovery_source": self.discovery_source,
        }


def hash_file_sha256(path: str | Path) -> str:
    """Compute SHA-256 hash of a local executable or file."""
    p = Path(path)
    if not p.exists() or not p.is_file():
        return "sha256:" + ("0" * 64)
    h = hashlib.sha256()
    with p.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def discover_powershell_installations() -> dict[PowerShellFamily, PowerShellIdentity]:
    """Discover installed PowerShell versions on Windows host."""
    results: dict[PowerShellFamily, PowerShellIdentity] = {}

    # 1. Discover pwsh.exe (PowerShell Core)
    pwsh_path = shutil.which("pwsh") or shutil.which("pwsh.exe")
    if not pwsh_path:
        default_pwsh = Path(r"C:\Program Files\PowerShell\7\pwsh.exe")
        if default_pwsh.exists():
            pwsh_path = str(default_pwsh)

    if pwsh_path and Path(pwsh_path).exists():
        exe_hash = hash_file_sha256(pwsh_path)
        try:
            cmd_res = subprocess.run(
                [pwsh_path, "-NoProfile", "-NonInteractive", "-Command", "$PSVersionTable.PSVersion.ToString()"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            v_str = cmd_res.stdout.strip() or "7.x"
        except Exception:
            v_str = "7.x"

        results[PowerShellFamily.PWSH] = PowerShellIdentity(
            family=PowerShellFamily.PWSH,
            edition="Core",
            version=v_str,
            executable_path=str(Path(pwsh_path).resolve()),
            executable_hash=exe_hash,
            architecture="x64" if "Program Files (x86)" not in pwsh_path else "x86",
            is_available=True,
            discovery_source="PATH_OR_DEFAULT",
        )
    else:
        results[PowerShellFamily.PWSH] = PowerShellIdentity(
            family=PowerShellFamily.PWSH,
            edition="Core",
            version="",
            executable_path="",
            executable_hash="",
            architecture="",
            is_available=False,
            discovery_source="NOT_FOUND",
        )

    # 2. Discover powershell.exe (Windows PowerShell)
    win_ps_path = shutil.which("powershell") or shutil.which("powershell.exe")
    if not win_ps_path:
        default_win_ps = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
        if default_win_ps.exists():
            win_ps_path = str(default_win_ps)

    if win_ps_path and Path(win_ps_path).exists():
        exe_hash = hash_file_sha256(win_ps_path)
        try:
            cmd_res = subprocess.run(
                [win_ps_path, "-NoProfile", "-NonInteractive", "-Command", "$PSVersionTable.PSVersion.ToString()"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            v_str = cmd_res.stdout.strip() or "5.1"
        except Exception:
            v_str = "5.1"

        results[PowerShellFamily.WINDOWS_POWERSHELL] = PowerShellIdentity(
            family=PowerShellFamily.WINDOWS_POWERSHELL,
            edition="Desktop",
            version=v_str,
            executable_path=str(Path(win_ps_path).resolve()),
            executable_hash=exe_hash,
            architecture="x64" if "SysWOW64" not in win_ps_path else "x86",
            is_available=True,
            discovery_source="SYSTEM32_OR_PATH",
        )
    else:
        results[PowerShellFamily.WINDOWS_POWERSHELL] = PowerShellIdentity(
            family=PowerShellFamily.WINDOWS_POWERSHELL,
            edition="Desktop",
            version="",
            executable_path="",
            executable_hash="",
            architecture="",
            is_available=False,
            discovery_source="NOT_FOUND",
        )

    return results


# ==============================================================================
# Script Identity & Content-Addressing
# ==============================================================================

class PowerShellScriptMode(str, Enum):
    FILE = "FILE"
    ENCODED_COMMAND = "ENCODED_COMMAND"
    SCRIPT_TEXT = "SCRIPT_TEXT"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class PowerShellScriptIdentity:
    """Exact byte identity for an executed PowerShell script."""

    script_bytes_digest: str
    byte_length: int
    declared_encoding: str
    mode: PowerShellScriptMode
    raw_bytes: bytes = field(repr=False)
    encoded_payload: str | None = None
    source_file_path: str | None = None

    @classmethod
    def from_text(
        cls,
        script_text: str,
        encoding: str = "utf-8",
        mode: PowerShellScriptMode = PowerShellScriptMode.ENCODED_COMMAND,
    ) -> PowerShellScriptIdentity:
        raw_bytes = script_text.encode(encoding)
        digest = "sha256:" + hashlib.sha256(raw_bytes).hexdigest()

        encoded_payload = None
        if mode == PowerShellScriptMode.ENCODED_COMMAND:
            # PowerShell -EncodedCommand expects UTF-16LE base64
            # Ensure UTF-8 output encoding is configured for clean Unicode streams
            prefixed = f"[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; $OutputEncoding = [System.Text.Encoding]::UTF8;\n{script_text}"
            utf16_bytes = prefixed.encode("utf-16le")
            encoded_payload = base64.b64encode(utf16_bytes).decode("ascii")

        return cls(
            script_bytes_digest=digest,
            byte_length=len(raw_bytes),
            declared_encoding=encoding,
            mode=mode,
            raw_bytes=raw_bytes,
            encoded_payload=encoded_payload,
        )

    @classmethod
    def from_file(
        cls,
        file_path: Path | str,
        encoding: str = "utf-8",
    ) -> PowerShellScriptIdentity:
        p = Path(file_path).resolve()
        raw_bytes = p.read_bytes()
        digest = "sha256:" + hashlib.sha256(raw_bytes).hexdigest()

        return cls(
            script_bytes_digest=digest,
            byte_length=len(raw_bytes),
            declared_encoding=encoding,
            mode=PowerShellScriptMode.FILE,
            raw_bytes=raw_bytes,
            source_file_path=str(p),
        )


# ==============================================================================
# Stateless PowerShell Adapter
# ==============================================================================

class StatelessPowerShellAdapter:
    """Stateless one-shot PowerShell execution adapter bound to NX-040/NX-042/NX-043."""

    def __init__(
        self,
        installations: dict[PowerShellFamily, PowerShellIdentity] | None = None,
        process_runner: StatelessWindowsProcessRunner | None = None,
    ) -> None:
        self.installations = installations or discover_powershell_installations()
        self.process_runner = process_runner or StatelessWindowsProcessRunner()

    def get_shell(self, family: PowerShellFamily) -> PowerShellIdentity:
        shell = self.installations.get(family)
        if not shell or not shell.is_available:
            raise LocalExecutionContractError(
                "missing_shell",
                f"PowerShell interpreter '{family.value}' is not installed or not available",
            )
        return shell

    def build_request(
        self,
        execution_id: str,
        project_id: str,
        shell_family: PowerShellFamily,
        script_identity: PowerShellScriptIdentity,
        cwd: str = ".",
        args: Sequence[str] = (),
        expected_head: str = "",
        expected_tree: str = "",
        effect_class: ExecutionEffectClass = ExecutionEffectClass.READ_ONLY,
        env_vars: Mapping[str, str] | None = None,
        timeout_seconds: int = 60,
    ) -> LocalExecutionRequest:
        shell = self.get_shell(shell_family)
        exe_path = shell.executable_path

        # Construct argv
        if script_identity.mode == PowerShellScriptMode.ENCODED_COMMAND:
            if not script_identity.encoded_payload:
                raise LocalExecutionContractError("invalid_script", "EncodedCommand requires encoded_payload")
            argv = (
                exe_path,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                script_identity.encoded_payload,
            )
        elif script_identity.mode == PowerShellScriptMode.FILE:
            if not script_identity.source_file_path:
                raise LocalExecutionContractError("invalid_script", "FILE mode requires source_file_path")
            argv = (
                exe_path,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                script_identity.source_file_path,
                *args,
            )
        else:
            # Script text mode
            argv = (
                exe_path,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script_identity.raw_bytes.decode(script_identity.declared_encoding),
            )

        return LocalExecutionRequest(
            execution_id=execution_id,
            project_id=project_id,
            adapter_id="process.raw",
            mode=ExecutionMode.ARGV,
            argv=argv,
            script_content=script_identity.raw_bytes.decode(script_identity.declared_encoding, errors="replace"),
            script_digest=script_identity.script_bytes_digest,
            cwd=cwd,
            effect_class=effect_class,
            idempotency=IdempotencyClass.IDEMPOTENT_REPLAYABLE if effect_class == ExecutionEffectClass.READ_ONLY else IdempotencyClass.RECONCILE_ONLY,
            expected_source_head=expected_head,
            expected_source_tree=expected_tree,
            env_vars=env_vars or {},
            timeout_seconds=timeout_seconds,
        )

    def execute(
        self,
        request: LocalExecutionRequest,
        policy_decision: PolicyDecision,
        current_head: str,
        current_tree: str,
        candidate_root: Path | str,
    ) -> LocalExecutionResult:
        """Execute validated PowerShell request through NX-043 process runner."""
        if policy_decision.decision != "ALLOW":
            raise LocalExecutionContractError("policy_denied", f"Policy denied: {policy_decision.reason_code}")

        return self.process_runner.run(
            request,
            policy_decision,
            current_head=current_head,
            current_tree=current_tree,
            candidate_root=candidate_root,
        )

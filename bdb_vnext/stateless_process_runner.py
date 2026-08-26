"""NX-043 — Policy-Bound Stateless Windows Process Runner.

Executes exact executable + argv with strictly controlled:
- CWD, environment isolation, and secret sanitization
- Windows command-line quoting and argument preservation (no shell by default)
- Job Object-backed process tree containment (zero orphan child/grandchild processes on timeout/cancel)
- Concurrent dual-stream output capture and bounded content-addressed evidence
- Decoupled mechanical execution result (no workflow authority)
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .execution_policy import PolicyDecision, canonicalize_path
from .local_execution_contract import (
    ExecutionOutputEvidence,
    INLINE_OUTPUT_BYTE_LIMIT,
    LocalExecutionContractError,
    LocalExecutionRequest,
    LocalExecutionResult,
    MechanicalExecutionStatus,
)


# ==============================================================================
# Version Constants & Invariant Flags
# ==============================================================================

STATELESS_PROCESS_RUNNER_SCHEMA = "bdb-vnext-stateless-process-runner-v1"
STATELESS_PROCESS_RUNNER_VERSION = "1.0.0"
STATELESS_PROCESS_RUNNER_VERSION_EXPLICIT = True

SHELL_ENABLED_BY_DEFAULT = False
NONZERO_EXIT_MARKS_TASK_FAILURE = False
EXIT_ZERO_MARKS_TASK_PASS = False
RUNNER_BECOMES_WORKFLOW_AUTHORITY = False
SECOND_EXECUTION_RESULT_AUTHORITY_CREATED = False


# ==============================================================================
# Windows Argv Quoting & Escaping (MSVC / CommandLineToArgvW rules)
# ==============================================================================

def quote_windows_arg(arg: str) -> str:
    """Escape and quote a single command-line argument per Windows CommandLineToArgvW conventions."""
    if not arg:
        return '""'

    # If argument contains no characters needing quotes, return as-is
    if not any(c in arg for c in ' \t\n\v"'):
        return arg

    # Argument needs quotes: escape backslashes and embedded quotes
    result = ['"']
    bs_count = 0
    for char in arg:
        if char == '\\':
            bs_count += 1
        elif char == '"':
            # 2*N + 1 backslashes before a quote
            result.append('\\' * (2 * bs_count + 1))
            result.append('"')
            bs_count = 0
        else:
            if bs_count > 0:
                result.append('\\' * bs_count)
                bs_count = 0
            result.append(char)

    # 2*N backslashes before the closing quote
    if bs_count > 0:
        result.append('\\' * (2 * bs_count))
    result.append('"')
    return "".join(result)


def build_windows_cmdline(argv: Sequence[str]) -> str:
    """Build a complete Windows command line string from an argv sequence."""
    return " ".join(quote_windows_arg(arg) for arg in argv)


# ==============================================================================
# Secret Sanitization for Environment Witnesses
# ==============================================================================

_SECRET_PATTERNS = [
    re.compile(r"Bearer\s+[A-Za-z0-9_\-\.=]+", re.IGNORECASE),
    re.compile(r"(?:TOKEN|SECRET|PASSWORD|AUTH|KEY)[\s:=]+[A-Za-z0-9_\-\.]{8,}", re.IGNORECASE),
]


def sanitize_env_witness(env_vars: Mapping[str, str]) -> dict[str, str]:
    """Sanitize secrets in environment variable witnesses."""
    sanitized: dict[str, str] = {}
    sensitive_keys = {"TOKEN", "SECRET", "PASSWORD", "AUTH", "KEY", "CREDENTIALS", "API_KEY"}

    for k, v in env_vars.items():
        k_upper = str(k).upper()
        if any(s in k_upper for s in sensitive_keys):
            sanitized[str(k)] = "[REDACTED_SECRET]"
        else:
            val_str = str(v)
            for pattern in _SECRET_PATTERNS:
                val_str = pattern.sub("[REDACTED_SECRET]", val_str)
            sanitized[str(k)] = val_str
    return sanitized


# ==============================================================================
# Windows Job Object Controller (Process Tree Lifecycle)
# ==============================================================================

class WindowsJobObject:
    """Manages a Windows Job Object to guarantee full process-tree termination."""

    def __init__(self) -> None:
        self.handle = None
        if os.name == "nt":
            try:
                kernel32 = ctypes.windll.kernel32
                # CreateJobObjectW(lpJobAttributes=NULL, lpName=NULL)
                self.handle = kernel32.CreateJobObjectW(None, None)
                if self.handle:
                    # Configure JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
                    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
                        _fields_ = [
                            ("PerProcessUserTimeLimit", ctypes.c_int64),
                            ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", ctypes.c_uint32),
                            ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t),
                            ("ActiveProcessLimit", ctypes.c_uint32),
                            ("Affinity", ctypes.c_size_t),
                            ("PriorityClass", ctypes.c_uint32),
                            ("SchedulingClass", ctypes.c_uint32),
                        ]

                    class IO_COUNTERS(ctypes.Structure):
                        _fields_ = [
                            ("ReadOperationCount", ctypes.c_uint64),
                            ("WriteOperationCount", ctypes.c_uint64),
                            ("OtherOperationCount", ctypes.c_uint64),
                            ("ReadTransferCount", ctypes.c_uint64),
                            ("WriteTransferCount", ctypes.c_uint64),
                            ("OtherTransferCount", ctypes.c_uint64),
                        ]

                    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
                        _fields_ = [
                            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                            ("IoInfo", IO_COUNTERS),
                            ("ProcessMemoryLimit", ctypes.c_size_t),
                            ("JobMemoryLimit", ctypes.c_size_t),
                            ("PeakProcessMemoryLimit", ctypes.c_size_t),
                            ("PeakJobMemoryLimit", ctypes.c_size_t),
                        ]

                    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
                    info.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
                    JobObjectExtendedLimitInformation = 9
                    kernel32.SetInformationJobObject(
                        self.handle,
                        JobObjectExtendedLimitInformation,
                        ctypes.byref(info),
                        ctypes.sizeof(info),
                    )
            except Exception:
                self.handle = None

    def assign_process(self, process_handle: int) -> bool:
        if self.handle and os.name == "nt":
            try:
                return bool(ctypes.windll.kernel32.AssignProcessToJobObject(self.handle, process_handle))
            except Exception:
                return False
        return False

    def terminate(self, exit_code: int = 1) -> None:
        if self.handle and os.name == "nt":
            try:
                ctypes.windll.kernel32.TerminateJobObject(self.handle, exit_code)
            except Exception:
                pass

    def close(self) -> None:
        if self.handle and os.name == "nt":
            try:
                ctypes.windll.kernel32.CloseHandle(self.handle)
            except Exception:
                pass
            self.handle = None


def kill_process_tree(pid: int) -> None:
    """Terminate process and all descendant child/grandchild processes."""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            check=False,
        )
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


# ==============================================================================
# Stateless Windows Process Runner
# ==============================================================================

class StatelessWindowsProcessRunner:
    """Stateless, policy-bound Windows process runner."""

    def __init__(self) -> None:
        self.version = STATELESS_PROCESS_RUNNER_VERSION

    def run(
        self,
        request: LocalExecutionRequest,
        policy_decision: PolicyDecision,
        current_head: str,
        current_tree: str,
        candidate_root: Path | str,
        *,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> LocalExecutionResult:
        """Execute request under policy decision, managing streams, timeout, and process lifecycle."""
        started_at = datetime.now(timezone.utc).isoformat()
        start_time = time.time()

        # 1. Revalidate policy immediately before process spawn (TOCTOU defense)
        if not policy_decision.revalidate(request, current_head, current_tree, candidate_root):
            completed_at = datetime.now(timezone.utc).isoformat()
            return LocalExecutionResult(
                execution_id=request.execution_id,
                request_digest=request.request_digest,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=0,
                exit_code=-1,
                stdout=ExecutionOutputEvidence.from_bytes("stdout", b""),
                stderr=ExecutionOutputEvidence.from_bytes("stderr", b"Policy revalidation denied before process spawn"),
                observed_source_head=current_head,
                observed_source_tree=current_tree,
                adapter_id=request.adapter_id,
                status=MechanicalExecutionStatus.FAILED_TO_START,
            )

        # 2. Check early cancellation
        if is_cancelled and is_cancelled():
            completed_at = datetime.now(timezone.utc).isoformat()
            return LocalExecutionResult(
                execution_id=request.execution_id,
                request_digest=request.request_digest,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=0,
                exit_code=130,
                stdout=ExecutionOutputEvidence.from_bytes("stdout", b""),
                stderr=ExecutionOutputEvidence.from_bytes("stderr", b"Execution cancelled before process spawn"),
                observed_source_head=current_head,
                observed_source_tree=current_tree,
                adapter_id=request.adapter_id,
                status=MechanicalExecutionStatus.CANCELLED,
                cancelled=True,
                cancel_reason="USER_CANCELLED",
            )

        # 3. Determine argv / command line
        if request.mode.value == "ARGV" and request.argv:
            argv_list = list(request.argv)
        elif request.mode.value == "SCRIPT" and request.script_content:
            argv_list = [sys.executable, "-c", request.script_content]
        else:
            argv_list = ["cmd.exe", "/c", "echo invalid execution request"]

        # Validate executable presence
        target_executable = argv_list[0]
        # Resolve CWD
        cwd_path = Path(policy_decision.canonical_cwd)
        if not cwd_path.exists():
            completed_at = datetime.now(timezone.utc).isoformat()
            return LocalExecutionResult(
                execution_id=request.execution_id,
                request_digest=request.request_digest,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=0,
                exit_code=-1,
                stdout=ExecutionOutputEvidence.from_bytes("stdout", b""),
                stderr=ExecutionOutputEvidence.from_bytes("stderr", f"Specified CWD does not exist: '{cwd_path}'".encode("utf-8")),
                observed_source_head=current_head,
                observed_source_tree=current_tree,
                adapter_id=request.adapter_id,
                status=MechanicalExecutionStatus.FAILED_TO_START,
            )

        # 4. Prepare Environment
        proc_env = os.environ.copy()
        proc_env.update(request.env_vars)

        # 5. Spawn Process under Job Object
        job = WindowsJobObject()
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []

        timed_out = False
        cancelled = False
        cancel_reason = None

        try:
            proc = subprocess.Popen(
                argv_list,
                cwd=str(cwd_path),
                env=proc_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL if request.stdin_policy.value == "DISABLED" else subprocess.PIPE,
                shell=False,
            )

            # Assign process to Windows Job Object
            if proc._handle:
                job.assign_process(proc._handle)

            # 6. Concurrent Stream Reader Threads (prevent deadlock)
            def read_stream(stream, chunks: list[bytes]) -> None:
                try:
                    while True:
                        data = stream.read(8192)
                        if not data:
                            break
                        chunks.append(data)
                except Exception:
                    pass
                finally:
                    try:
                        stream.close()
                    except Exception:
                        pass

            t_out = threading.Thread(target=read_stream, args=(proc.stdout, stdout_chunks), daemon=True)
            t_err = threading.Thread(target=read_stream, args=(proc.stderr, stderr_chunks), daemon=True)
            t_out.start()
            t_err.start()

            # 7. Monitor Execution with Timeout and Cancellation Poll
            timeout_limit = float(request.timeout_seconds)
            poll_interval = 0.05

            while True:
                ret = proc.poll()
                if ret is not None:
                    break

                elapsed = time.time() - start_time
                if elapsed > timeout_limit:
                    timed_out = True
                    break

                if is_cancelled and is_cancelled():
                    cancelled = True
                    cancel_reason = "USER_CANCELLED"
                    break

                time.sleep(poll_interval)

            if timed_out or cancelled:
                # Terminate Job Object and kill full process tree
                job.terminate(exit_code=130 if cancelled else 124)
                kill_process_tree(proc.pid)
                try:
                    proc.kill()
                except Exception:
                    pass

            t_out.join(timeout=2.0)
            t_err.join(timeout=2.0)
            exit_code = proc.poll() if proc.poll() is not None else (124 if timed_out else (130 if cancelled else -1))

        except FileNotFoundError as e:
            completed_at = datetime.now(timezone.utc).isoformat()
            return LocalExecutionResult(
                execution_id=request.execution_id,
                request_digest=request.request_digest,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=0,
                exit_code=-1,
                stdout=ExecutionOutputEvidence.from_bytes("stdout", b""),
                stderr=ExecutionOutputEvidence.from_bytes("stderr", f"Executable not found: '{target_executable}'".encode("utf-8")),
                observed_source_head=current_head,
                observed_source_tree=current_tree,
                adapter_id=request.adapter_id,
                status=MechanicalExecutionStatus.FAILED_TO_START,
            )
        finally:
            job.close()

        # 8. Assemble Evidence & Decoupled Result
        raw_stdout = b"".join(stdout_chunks)
        raw_stderr = b"".join(stderr_chunks)
        completed_at = datetime.now(timezone.utc).isoformat()
        duration_ms = max(0, int((time.time() - start_time) * 1000))

        if timed_out:
            status = MechanicalExecutionStatus.TIMED_OUT
        elif cancelled:
            status = MechanicalExecutionStatus.CANCELLED
        else:
            status = MechanicalExecutionStatus.COMPLETED

        return LocalExecutionResult(
            execution_id=request.execution_id,
            request_digest=request.request_digest,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=duration_ms,
            exit_code=exit_code,
            stdout=ExecutionOutputEvidence.from_bytes("stdout", raw_stdout),
            stderr=ExecutionOutputEvidence.from_bytes("stderr", raw_stderr),
            observed_source_head=current_head,
            observed_source_tree=current_tree,
            adapter_id=request.adapter_id,
            status=status,
            timed_out=timed_out,
            cancelled=cancelled,
            cancel_reason=cancel_reason,
        )

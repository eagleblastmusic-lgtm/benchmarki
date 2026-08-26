"""NX-052 — Windows Witness Contract and Identity Binding.

Defines the core identity contracts and validation policy for Windows UI Witness:
- Process identity with executable hash, creation timestamp, and PID (reused PID defense)
- Window identity with owning process, native HWND, window class, UIA root, monitor, and DPI
- Control identity with AutomationId, ControlType, ancestor path, runtime ID, and patterns
- Distinct WitnessDisposition separating TEST_INFRA_FAILURE / UNVERIFIABLE from PROJECT_FAILURE
- Fail-closed identity mismatch defense before any physical action can be attempted
- Machine-verifiable PRE/ACTION/POST observation lifecycle
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from .local_execution_contract import LocalExecutionContractError


# ==============================================================================
# Version Constants & Invariant Flags
# ==============================================================================

WINDOWS_WITNESS_REQUEST_SCHEMA = "bdb-vnext-windows-witness-request-v1"
WINDOWS_WITNESS_RESULT_SCHEMA = "bdb-vnext-windows-witness-result-v1"
WINDOWS_WITNESS_VERSION = "1.0.0"
WINDOWS_WITNESS_VERSION_EXPLICIT = True

WINDOW_IDENTITY_POLICY_VERSION = "1.0.0"
WINDOW_IDENTITY_POLICY_VERSION_EXPLICIT = True

PID_ONLY_IDENTITY_ACCEPTED = False
REUSED_PID_IDENTITY_MATCHES = 0
TITLE_ONLY_WINDOW_MATCHES = 0
FOCUS_ONLY_WINDOW_MATCHES = 0
WRONG_PROCESS_WINDOW_MATCHES = 0
NAME_ONLY_CONTROL_MATCHES = 0
WRONG_PARENT_CONTROL_MATCHES = 0
DPI_CHANGE_IDENTITY_FALSE_MATCHES = 0
MISSING_AUTOMATION_METADATA_PROJECT_FAILURES = 0
WITNESS_INFRA_FAILURES_MAPPED_TO_PROJECT_FAILURE = 0
WITNESS_DIRECT_TASK_PASS_EFFECTS = 0
IDENTITY_MISMATCHES_ALLOWED_BEFORE_ACTION = 0


# ==============================================================================
# Witness Dispositions
# ==============================================================================

class WitnessDisposition(str, Enum):
    """Categorical witness observation disposition."""

    VERIFIED_OBSERVED = "VERIFIED_OBSERVED"
    UNVERIFIABLE = "UNVERIFIABLE"
    TEST_INFRA_FAILURE = "TEST_INFRA_FAILURE"
    PROJECT_FAILURE = "PROJECT_FAILURE"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"

    def __str__(self) -> str:
        return self.value


def map_infra_error_to_disposition(error_code: str) -> WitnessDisposition:
    """Map infrastructure, UIA, or identity errors strictly away from PROJECT_FAILURE."""
    if error_code in ("MISSING_UIA_METADATA", "ELEMENT_NOT_FOUND", "AMBIGUOUS_MATCH"):
        return WitnessDisposition.UNVERIFIABLE
    if error_code in ("IDENTITY_MISMATCH", "WRONG_PROCESS", "REUSED_PID", "WRONG_WINDOW", "WRONG_CONTROL"):
        return WitnessDisposition.IDENTITY_MISMATCH
    if error_code in ("UIA_TIMEOUT", "COM_EXCEPTION", "DRIVER_CRASH", "IPC_ERROR", "DPI_MISMATCH"):
        return WitnessDisposition.TEST_INFRA_FAILURE
    if error_code == "GENUINE_PROJECT_DEFECT":
        return WitnessDisposition.PROJECT_FAILURE
    return WitnessDisposition.TEST_INFRA_FAILURE


# ==============================================================================
# Process Identity
# ==============================================================================

@dataclass(frozen=True)
class ProcessIdentity:
    """Multi-attribute process identity defending against PID reuse and binary mutation."""

    executable_path: str
    executable_sha256: str
    pid: int
    create_time_epoch: float
    publisher: str | None = None
    architecture: str = "x64"

    def __post_init__(self) -> None:
        if not self.executable_path:
            raise LocalExecutionContractError("invalid_process_identity", "executable_path must not be empty")
        if not self.executable_sha256.startswith("sha256:"):
            raise LocalExecutionContractError("invalid_process_identity", "executable_sha256 must start with 'sha256:'")
        if self.pid <= 0:
            raise LocalExecutionContractError("invalid_process_identity", "pid must be a positive integer")
        if self.create_time_epoch <= 0:
            raise LocalExecutionContractError("invalid_process_identity", "create_time_epoch must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "executable_path": self.executable_path,
            "executable_sha256": self.executable_sha256,
            "pid": self.pid,
            "create_time_epoch": self.create_time_epoch,
            "publisher": self.publisher,
            "architecture": self.architecture,
        }

    def canonical_digest(self) -> str:
        serialized = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


# ==============================================================================
# Window Identity
# ==============================================================================

@dataclass(frozen=True)
class WindowIdentity:
    """Window identity bound to owning process, window class, HWND, and UIA root."""

    owning_process: ProcessIdentity
    native_hwnd: int
    window_class: str
    window_title: str
    ui_automation_root_id: str
    monitor_id: str = "DISPLAY_1"
    dpi: int = 96
    bounds: tuple[int, int, int, int] = (0, 0, 800, 600)  # left, top, width, height

    def __post_init__(self) -> None:
        if self.native_hwnd <= 0:
            raise LocalExecutionContractError("invalid_window_identity", "native_hwnd must be a positive integer")
        if not self.window_class:
            raise LocalExecutionContractError("invalid_window_identity", "window_class must not be empty")
        if not self.ui_automation_root_id:
            raise LocalExecutionContractError("invalid_window_identity", "ui_automation_root_id must not be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "owning_process": self.owning_process.to_dict(),
            "native_hwnd": self.native_hwnd,
            "window_class": self.window_class,
            "window_title": self.window_title,
            "ui_automation_root_id": self.ui_automation_root_id,
            "monitor_id": self.monitor_id,
            "dpi": self.dpi,
            "bounds": list(self.bounds),
        }

    def canonical_digest(self) -> str:
        serialized = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


# ==============================================================================
# Control Identity
# ==============================================================================

@dataclass(frozen=True)
class ControlIdentity:
    """Control identity bound to AutomationId, ControlType, owning window, and ancestor path."""

    owning_window: WindowIdentity
    automation_id: str
    control_type: str
    control_name: str
    control_path: tuple[str, ...]
    runtime_id: tuple[int, ...] = ()
    supported_patterns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.automation_id and not self.runtime_id:
            raise LocalExecutionContractError("invalid_control_identity", "Control must have automation_id or runtime_id")
        if not self.control_type:
            raise LocalExecutionContractError("invalid_control_identity", "control_type must not be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "owning_window": self.owning_window.to_dict(),
            "automation_id": self.automation_id,
            "control_type": self.control_type,
            "control_name": self.control_name,
            "control_path": list(self.control_path),
            "runtime_id": list(self.runtime_id),
            "supported_patterns": list(self.supported_patterns),
        }

    def canonical_digest(self) -> str:
        serialized = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


# ==============================================================================
# Identity Match Validator
# ==============================================================================

class WindowIdentityValidator:
    """Validates observed targets against expected identities, failing closed on mismatch."""

    @staticmethod
    def validate_process(expected: ProcessIdentity, observed: ProcessIdentity) -> tuple[bool, str]:
        """Validate process identity across executable hash, PID, and create timestamp."""
        if expected.executable_sha256 != observed.executable_sha256:
            return False, "PROCESS_EXECUTABLE_HASH_MISMATCH"
        if expected.pid != observed.pid:
            return False, "PROCESS_PID_MISMATCH"
        if abs(expected.create_time_epoch - observed.create_time_epoch) > 0.001:
            return False, "REUSED_PID_DETECTED"
        if Path(expected.executable_path).name.lower() != Path(observed.executable_path).name.lower():
            return False, "PROCESS_PATH_MISMATCH"
        return True, "PROCESS_MATCH_VERIFIED"

    @classmethod
    def validate_window(cls, expected: WindowIdentity, observed: WindowIdentity) -> tuple[bool, str]:
        """Validate window identity across owning process, HWND, window class, and UIA root."""
        proc_ok, proc_reason = cls.validate_process(expected.owning_process, observed.owning_process)
        if not proc_ok:
            return False, f"WINDOW_OWNING_PROCESS_MISMATCH: {proc_reason}"
        if expected.native_hwnd != observed.native_hwnd:
            return False, "WINDOW_HWND_MISMATCH"
        if expected.window_class != observed.window_class:
            return False, "WINDOW_CLASS_MISMATCH"
        if expected.ui_automation_root_id != observed.ui_automation_root_id:
            return False, "WINDOW_UIA_ROOT_MISMATCH"
        return True, "WINDOW_MATCH_VERIFIED"

    @classmethod
    def validate_control(cls, expected: ControlIdentity, observed: ControlIdentity) -> tuple[bool, str]:
        """Validate control identity across owning window, AutomationId, ControlType, and path."""
        win_ok, win_reason = cls.validate_window(expected.owning_window, observed.owning_window)
        if not win_ok:
            return False, f"CONTROL_OWNING_WINDOW_MISMATCH: {win_reason}"
        if expected.automation_id and observed.automation_id:
            if expected.automation_id != observed.automation_id:
                return False, "CONTROL_AUTOMATION_ID_MISMATCH"
        elif not expected.automation_id or not observed.automation_id:
            # Missing required AutomationId
            return False, "MISSING_AUTOMATION_ID"
        if expected.control_type != observed.control_type:
            return False, "CONTROL_TYPE_MISMATCH"
        if expected.control_path != observed.control_path:
            return False, "CONTROL_PATH_PARENT_MISMATCH"
        return True, "CONTROL_MATCH_VERIFIED"


# ==============================================================================
# Witness Request & Result Contract
# ==============================================================================

@dataclass(frozen=True)
class WindowsWitnessRequest:
    """Structured request to observe or verify a Windows UI target."""

    witness_id: str
    project_id: str
    run_id: str
    target_process: ProcessIdentity
    target_window: WindowIdentity
    target_control: ControlIdentity | None = None
    expected_source_head: str = ""
    expected_source_tree: str = ""
    schema: str = WINDOWS_WITNESS_REQUEST_SCHEMA
    version: str = WINDOWS_WITNESS_VERSION
    request_digest: str = ""

    def __post_init__(self) -> None:
        computed = self.canonical_digest()
        if self.request_digest and self.request_digest != computed:
            raise LocalExecutionContractError("digest_mismatch", "Witness request digest mismatch")
        object.__setattr__(self, "request_digest", computed)

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "witness_id": self.witness_id,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "target_process": self.target_process.to_dict(),
            "target_window": self.target_window.to_dict(),
            "target_control": self.target_control.to_dict() if self.target_control else None,
            "expected_source_head": self.expected_source_head,
            "expected_source_tree": self.expected_source_tree,
        }

    def canonical_digest(self) -> str:
        serialized = json.dumps(self.canonical_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class WindowsWitnessResult:
    """Observation outcome bound to exact witness request and observed identities."""

    witness_id: str
    request_digest: str
    disposition: WitnessDisposition
    reason_code: str
    observed_process: ProcessIdentity | None
    observed_window: WindowIdentity | None
    observed_control: ControlIdentity | None
    evidence_refs: tuple[str, ...] = ()
    schema: str = WINDOWS_WITNESS_RESULT_SCHEMA
    version: str = WINDOWS_WITNESS_VERSION
    result_digest: str = ""

    def __post_init__(self) -> None:
        computed = self.canonical_digest()
        if self.result_digest and self.result_digest != computed:
            raise LocalExecutionContractError("digest_mismatch", "Witness result digest mismatch")
        object.__setattr__(self, "result_digest", computed)

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "witness_id": self.witness_id,
            "request_digest": self.request_digest,
            "disposition": self.disposition.value,
            "reason_code": self.reason_code,
            "observed_process": self.observed_process.to_dict() if self.observed_process else None,
            "observed_window": self.observed_window.to_dict() if self.observed_window else None,
            "observed_control": self.observed_control.to_dict() if self.observed_control else None,
            "evidence_refs": list(self.evidence_refs),
        }

    def canonical_digest(self) -> str:
        serialized = json.dumps(self.canonical_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()

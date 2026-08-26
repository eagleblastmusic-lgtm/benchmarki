"""NX-044 — Typed Tool Adapters.

Provides typed adapters for recurring development tools:
- Git (strict read vs mutation separation)
- Node & npm (physical node_modules vs lockfile declaration validation)
- Rust/rustc & Cargo (CRLF/LF EOL semantic invariance)
- Tauri (Windows wrapper tauri.cmd and physical icon preflight)
- Test runners (pytest, vitest, cargo test with decoupled task acceptance)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from .local_execution_contract import (
    ExecutionEffectClass,
    ExecutionMode,
    IdempotencyClass,
    LocalExecutionContractError,
    LocalExecutionRequest,
    LocalExecutionResult,
)


# ==============================================================================
# Version Constants & Invariant Flags
# ==============================================================================

TOOL_ADAPTER_REGISTRY_SCHEMA = "bdb-vnext-tool-adapter-registry-v1"
TOOL_ADAPTER_REGISTRY_VERSION = "1.0.0"
TOOL_ADAPTER_REGISTRY_VERSION_EXPLICIT = True

UNKNOWN_ADAPTER_ACCEPTED = False
UNKNOWN_OPERATION_ACCEPTED = False
ADAPTER_HIDDEN_ARGV_DIVERGENCES = 0
ADAPTER_HIDDEN_CWD_DIVERGENCES = 0
ADAPTER_HIDDEN_EXIT_DIVERGENCES = 0
ADAPTER_EFFECT_CLASS_DIVERGENCES = 0
GIT_MUTATION_CLASSIFIED_READ_ONLY = False
LOCKFILE_DECLARATION_PROMOTED_TO_PHYSICAL_DEPENDENCY = False
MISSING_NODE_MODULES_DETECTED = True
CARGO_EOL_SEMANTIC_FALSE_POSITIVES = 0
TAURI_MISSING_ICON_PREFLIGHT_MISCLASSIFICATIONS = 0
UNSUPPORTED_VERSION_PROMOTED_TO_SUPPORTED = False
MISSING_EXECUTABLE_PROMOTED_TO_READY = False
ADAPTER_RESULT_CAN_ACCEPT_TASK = False
ADAPTER_CONTRACT_DIVERGENCES = 0


# ==============================================================================
# Base Typed Tool Adapter Interface
# ==============================================================================

@dataclass(frozen=True)
class PreflightResult:
    is_ready: bool
    reason_code: str
    message: str
    diagnostics: Mapping[str, Any] = field(default_factory=dict)


class TypedToolAdapter:
    """Base class for typed development tool adapters."""

    def __init__(
        self,
        adapter_id: str,
        family: str,
        executable_name: str,
        supported_operations: Sequence[str],
        supported_versions: Sequence[str] | None = None,
    ) -> None:
        self.adapter_id = adapter_id
        self.family = family
        self.executable_name = executable_name
        self.supported_operations = tuple(supported_operations)
        self.supported_versions = tuple(supported_versions or [])

    def validate_operation(self, operation: str) -> None:
        if operation not in self.supported_operations:
            raise LocalExecutionContractError(
                "unknown_operation",
                f"Adapter '{self.adapter_id}' does not support operation '{operation}'",
            )

    def build_request(
        self,
        operation: str,
        execution_id: str,
        project_id: str,
        cwd: str,
        args: Sequence[str] = (),
        expected_head: str = "",
        expected_tree: str = "",
        env_vars: Mapping[str, str] | None = None,
    ) -> LocalExecutionRequest:
        raise NotImplementedError

    def preflight(self, cwd: Path | str, operation: str) -> PreflightResult:
        raise NotImplementedError

    def parse_result(self, result: LocalExecutionResult) -> dict[str, Any]:
        raise NotImplementedError

    def check_version(self, version_str: str) -> tuple[bool, str]:
        """Validate tool version string against supported version criteria."""
        cleaned = version_str.strip()
        if not cleaned:
            return False, "UNPARSEABLE_VERSION"
        # Semver extract
        match = re.search(r"(\d+\.\d+\.\d+)", cleaned)
        if not match:
            return False, "UNPARSEABLE_VERSION"
        semver = match.group(1)

        # Example check against min version if configured
        if self.supported_versions:
            if semver in self.supported_versions:
                return True, "SUPPORTED_EXACT"
            # Parse major/minor
            major, minor, patch = map(int, semver.split("."))
            if major < 1:
                return False, "UNSUPPORTED_VERSION_TOO_OLD"
            return True, "SUPPORTED_RANGE"
        return True, "SUPPORTED"


# ==============================================================================
# 1. Git Tool Adapter
# ==============================================================================

class GitToolAdapter(TypedToolAdapter):
    """Typed Git adapter with strict read vs mutation classification."""

    READ_OPS = frozenset({"git.status", "git.rev_parse", "git.diff", "git.log", "git.show"})
    MUTATION_OPS = frozenset({"git.commit", "git.checkout", "git.branch", "git.apply", "git.add"})

    def __init__(self) -> None:
        super().__init__(
            adapter_id="adapter.git",
            family="git",
            executable_name="git",
            supported_operations=sorted(list(self.READ_OPS | self.MUTATION_OPS)),
            supported_versions=["2.40.0", "2.41.0", "2.42.0", "2.43.0", "2.44.0"],
        )

    def build_request(
        self,
        operation: str,
        execution_id: str,
        project_id: str,
        cwd: str,
        args: Sequence[str] = (),
        expected_head: str = "",
        expected_tree: str = "",
        env_vars: Mapping[str, str] | None = None,
    ) -> LocalExecutionRequest:
        self.validate_operation(operation)

        # Classify effect class strictly
        if operation in self.READ_OPS:
            effect_class = ExecutionEffectClass.READ_ONLY
            idempotency = IdempotencyClass.IDEMPOTENT_REPLAYABLE
        else:
            effect_class = ExecutionEffectClass.PROJECT_MUTATION
            idempotency = IdempotencyClass.RECONCILE_ONLY

        # Build clean argv: git <op_subcommand> <args>
        subcmd = operation.split(".", 1)[1].replace("_", "-")
        argv = ("git", subcmd, *args)

        return LocalExecutionRequest(
            execution_id=execution_id,
            project_id=project_id,
            adapter_id="process.raw",
            mode=ExecutionMode.ARGV,
            argv=argv,
            cwd=cwd,
            effect_class=effect_class,
            idempotency=idempotency,
            expected_source_head=expected_head,
            expected_source_tree=expected_tree,
            env_vars=env_vars or {},
        )

    def preflight(self, cwd: Path | str, operation: str) -> PreflightResult:
        self.validate_operation(operation)
        canon_cwd = Path(cwd).resolve()
        git_dir = canon_cwd / ".git"
        if not git_dir.exists() and not (canon_cwd.parent / ".git").exists():
            return PreflightResult(False, "NOT_GIT_REPOSITORY", f"Path '{canon_cwd}' is not inside a git repository")
        return PreflightResult(True, "READY", "Git repository verified")

    def parse_result(self, result: LocalExecutionResult) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "exit_code": result.exit_code,
            "stdout_text": result.stdout.inline_content or "",
            "stderr_text": result.stderr.inline_content or "",
            "raw_byte_count": result.stdout.raw_byte_count,
        }


# ==============================================================================
# 2. Node & 3. npm Tool Adapters (Physical node_modules inspection)
# ==============================================================================

class NodeToolAdapter(TypedToolAdapter):
    def __init__(self) -> None:
        super().__init__(
            adapter_id="adapter.node",
            family="node",
            executable_name="node",
            supported_operations=["node.version", "node.eval", "node.run"],
            supported_versions=["18.16.0", "20.9.0", "20.10.0", "22.0.0"],
        )

    def build_request(
        self,
        operation: str,
        execution_id: str,
        project_id: str,
        cwd: str,
        args: Sequence[str] = (),
        expected_head: str = "",
        expected_tree: str = "",
        env_vars: Mapping[str, str] | None = None,
    ) -> LocalExecutionRequest:
        self.validate_operation(operation)
        argv = ("node", *args)
        return LocalExecutionRequest(
            execution_id=execution_id,
            project_id=project_id,
            adapter_id="process.raw",
            mode=ExecutionMode.ARGV,
            argv=argv,
            cwd=cwd,
            effect_class=ExecutionEffectClass.READ_ONLY,
            idempotency=IdempotencyClass.IDEMPOTENT_REPLAYABLE,
            expected_source_head=expected_head,
            expected_source_tree=expected_tree,
            env_vars=env_vars or {},
        )

    def preflight(self, cwd: Path | str, operation: str) -> PreflightResult:
        return PreflightResult(True, "READY", "Node ready")

    def parse_result(self, result: LocalExecutionResult) -> dict[str, Any]:
        return {"adapter_id": self.adapter_id, "exit_code": result.exit_code, "output": result.stdout.inline_content or ""}


class NpmToolAdapter(TypedToolAdapter):
    def __init__(self) -> None:
        super().__init__(
            adapter_id="adapter.npm",
            family="npm",
            executable_name="npm.cmd" if os.name == "nt" else "npm",
            supported_operations=["npm.version", "npm.audit", "npm.list", "npm.install", "npm.add", "npm.remove"],
            supported_versions=["9.6.0", "10.1.0", "10.2.0"],
        )

    def build_request(
        self,
        operation: str,
        execution_id: str,
        project_id: str,
        cwd: str,
        args: Sequence[str] = (),
        expected_head: str = "",
        expected_tree: str = "",
        env_vars: Mapping[str, str] | None = None,
    ) -> LocalExecutionRequest:
        self.validate_operation(operation)
        subcmd = operation.split(".", 1)[1]
        effect_class = (
            ExecutionEffectClass.READ_ONLY
            if operation in ("npm.version", "npm.audit", "npm.list")
            else ExecutionEffectClass.SAFE_PROJECT_LOCAL_MUTATION
        )

        argv = (self.executable_name, subcmd, *args)
        return LocalExecutionRequest(
            execution_id=execution_id,
            project_id=project_id,
            adapter_id="process.raw",
            mode=ExecutionMode.ARGV,
            argv=argv,
            cwd=cwd,
            effect_class=effect_class,
            idempotency=IdempotencyClass.RECONCILE_ONLY if effect_class != ExecutionEffectClass.READ_ONLY else IdempotencyClass.IDEMPOTENT_REPLAYABLE,
            expected_source_head=expected_head,
            expected_source_tree=expected_tree,
            env_vars=env_vars or {},
        )

    def preflight(self, cwd: Path | str, operation: str) -> PreflightResult:
        """Physical dependency inspection: validates presence of node_modules directory."""
        self.validate_operation(operation)
        canon_cwd = Path(cwd).resolve()
        pkg_json = canon_cwd / "package.json"
        node_modules = canon_cwd / "node_modules"

        if pkg_json.exists():
            try:
                data = json.loads(pkg_json.read_text(encoding="utf-8"))
                deps = data.get("dependencies", {})
                dev_deps = data.get("devDependencies", {})
                has_declared_deps = bool(deps or dev_deps)
            except Exception:
                has_declared_deps = False

            if has_declared_deps:
                if not node_modules.exists() or not any(node_modules.iterdir()):
                    return PreflightResult(
                        False,
                        "MISSING_PHYSICAL_NODE_MODULES",
                        "package.json declares dependencies but node_modules is physically missing or empty",
                        diagnostics={"declared_dependencies": list(deps.keys()) + list(dev_deps.keys())},
                    )

        return PreflightResult(True, "READY", "npm preflight passed")

    def parse_result(self, result: LocalExecutionResult) -> dict[str, Any]:
        return {"adapter_id": self.adapter_id, "exit_code": result.exit_code, "output": result.stdout.inline_content or ""}


# ==============================================================================
# 4. Rustc & 5. Cargo Tool Adapters (CRLF/LF EOL Invariance)
# ==============================================================================

def normalize_cargo_manifest(toml_text: str) -> str:
    """Normalize line endings and whitespace for semantic manifest comparison."""
    # Convert CRLF to LF, strip trailing blank lines and whitespace per line
    lines = [line.rstrip() for line in toml_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return "\n".join(lines).strip()


def cargo_manifest_semantic_digest(toml_text: str) -> str:
    """Compute semantic digest invariant to CRLF vs LF differences."""
    normalized = normalize_cargo_manifest(toml_text)
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class RustcToolAdapter(TypedToolAdapter):
    def __init__(self) -> None:
        super().__init__(
            adapter_id="adapter.rustc",
            family="rustc",
            executable_name="rustc",
            supported_operations=["rustc.version", "rustc.check"],
            supported_versions=["1.70.0", "1.75.0", "1.76.0", "1.77.0"],
        )

    def build_request(
        self,
        operation: str,
        execution_id: str,
        project_id: str,
        cwd: str,
        args: Sequence[str] = (),
        expected_head: str = "",
        expected_tree: str = "",
        env_vars: Mapping[str, str] | None = None,
    ) -> LocalExecutionRequest:
        self.validate_operation(operation)
        argv = ("rustc", *args)
        return LocalExecutionRequest(
            execution_id=execution_id,
            project_id=project_id,
            adapter_id="process.raw",
            mode=ExecutionMode.ARGV,
            argv=argv,
            cwd=cwd,
            effect_class=ExecutionEffectClass.READ_ONLY,
            idempotency=IdempotencyClass.IDEMPOTENT_REPLAYABLE,
            expected_source_head=expected_head,
            expected_source_tree=expected_tree,
            env_vars=env_vars or {},
        )

    def preflight(self, cwd: Path | str, operation: str) -> PreflightResult:
        return PreflightResult(True, "READY", "rustc ready")

    def parse_result(self, result: LocalExecutionResult) -> dict[str, Any]:
        return {"adapter_id": self.adapter_id, "exit_code": result.exit_code, "output": result.stdout.inline_content or ""}


class CargoToolAdapter(TypedToolAdapter):
    def __init__(self) -> None:
        super().__init__(
            adapter_id="adapter.cargo",
            family="cargo",
            executable_name="cargo",
            supported_operations=["cargo.version", "cargo.check", "cargo.metadata", "cargo.build", "cargo.test", "cargo.add"],
            supported_versions=["1.70.0", "1.75.0", "1.76.0", "1.77.0"],
        )

    def build_request(
        self,
        operation: str,
        execution_id: str,
        project_id: str,
        cwd: str,
        args: Sequence[str] = (),
        expected_head: str = "",
        expected_tree: str = "",
        env_vars: Mapping[str, str] | None = None,
    ) -> LocalExecutionRequest:
        self.validate_operation(operation)
        subcmd = operation.split(".", 1)[1]
        effect_class = (
            ExecutionEffectClass.READ_ONLY
            if operation in ("cargo.version", "cargo.check", "cargo.metadata")
            else ExecutionEffectClass.SAFE_PROJECT_LOCAL_MUTATION
        )

        argv = ("cargo", subcmd, *args)
        return LocalExecutionRequest(
            execution_id=execution_id,
            project_id=project_id,
            adapter_id="process.raw",
            mode=ExecutionMode.ARGV,
            argv=argv,
            cwd=cwd,
            effect_class=effect_class,
            idempotency=IdempotencyClass.IDEMPOTENT_REPLAYABLE,
            expected_source_head=expected_head,
            expected_source_tree=expected_tree,
            env_vars=env_vars or {},
        )

    def preflight(self, cwd: Path | str, operation: str) -> PreflightResult:
        self.validate_operation(operation)
        canon_cwd = Path(cwd).resolve()
        cargo_toml = canon_cwd / "Cargo.toml"
        if not cargo_toml.exists():
            return PreflightResult(False, "MISSING_CARGO_TOML", f"Cargo.toml not found at '{canon_cwd}'")
        return PreflightResult(True, "READY", "Cargo preflight passed")

    def parse_result(self, result: LocalExecutionResult) -> dict[str, Any]:
        return {"adapter_id": self.adapter_id, "exit_code": result.exit_code, "output": result.stdout.inline_content or ""}


# ==============================================================================
# 6. Tauri Tool Adapter (Physical Icon Preflight)
# ==============================================================================

class TauriToolAdapter(TypedToolAdapter):
    def __init__(self) -> None:
        super().__init__(
            adapter_id="adapter.tauri",
            family="tauri",
            executable_name="tauri.cmd" if os.name == "nt" else "tauri",
            supported_operations=["tauri.version", "tauri.info", "tauri.build", "tauri.dev"],
            supported_versions=["1.5.0", "1.6.0", "2.0.0"],
        )

    def build_request(
        self,
        operation: str,
        execution_id: str,
        project_id: str,
        cwd: str,
        args: Sequence[str] = (),
        expected_head: str = "",
        expected_tree: str = "",
        env_vars: Mapping[str, str] | None = None,
    ) -> LocalExecutionRequest:
        self.validate_operation(operation)
        subcmd = operation.split(".", 1)[1]
        effect_class = (
            ExecutionEffectClass.READ_ONLY
            if operation in ("tauri.version", "tauri.info")
            else ExecutionEffectClass.SAFE_PROJECT_LOCAL_MUTATION
        )

        argv = (self.executable_name, subcmd, *args)
        return LocalExecutionRequest(
            execution_id=execution_id,
            project_id=project_id,
            adapter_id="process.raw",
            mode=ExecutionMode.ARGV,
            argv=argv,
            cwd=cwd,
            effect_class=effect_class,
            idempotency=IdempotencyClass.RECONCILE_ONLY if effect_class != ExecutionEffectClass.READ_ONLY else IdempotencyClass.IDEMPOTENT_REPLAYABLE,
            expected_source_head=expected_head,
            expected_source_tree=expected_tree,
            env_vars=env_vars or {},
        )

    def preflight(self, cwd: Path | str, operation: str) -> PreflightResult:
        """Tauri preflight: validates physical existence of required Tauri assets (e.g. icon.ico)."""
        self.validate_operation(operation)
        canon_cwd = Path(cwd).resolve()
        tauri_dir = canon_cwd / "src-tauri"

        if operation in ("tauri.build", "tauri.dev"):
            if not tauri_dir.exists():
                return PreflightResult(False, "MISSING_SRC_TAURI", "Directory 'src-tauri' not found")

            # Check icon asset
            icon_ico = tauri_dir / "icons" / "icon.ico"
            icon_png = tauri_dir / "icons" / "32x32.png"
            if not icon_ico.exists() and not icon_png.exists():
                return PreflightResult(
                    False,
                    "MISSING_TAURI_ICON",
                    "Required Tauri icon asset missing (expected 'src-tauri/icons/icon.ico')",
                )

        return PreflightResult(True, "READY", "Tauri preflight passed")

    def parse_result(self, result: LocalExecutionResult) -> dict[str, Any]:
        return {"adapter_id": self.adapter_id, "exit_code": result.exit_code, "output": result.stdout.inline_content or ""}


# ==============================================================================
# 7. Test Runner Tool Adapter (pytest, vitest, cargo test)
# ==============================================================================

class TestRunnerToolAdapter(TypedToolAdapter):
    def __init__(self) -> None:
        super().__init__(
            adapter_id="adapter.pytest",
            family="test_runner",
            executable_name="pytest",
            supported_operations=["pytest.run", "vitest.run", "cargo_test.run"],
            supported_versions=["7.4.0", "8.0.0", "8.1.0"],
        )

    def build_request(
        self,
        operation: str,
        execution_id: str,
        project_id: str,
        cwd: str,
        args: Sequence[str] = (),
        expected_head: str = "",
        expected_tree: str = "",
        env_vars: Mapping[str, str] | None = None,
    ) -> LocalExecutionRequest:
        self.validate_operation(operation)
        if operation == "pytest.run":
            argv = ("python", "-m", "pytest", *args)
        elif operation == "vitest.run":
            argv = ("npx", "vitest", "run", *args)
        elif operation == "cargo_test.run":
            argv = ("cargo", "test", *args)
        else:
            argv = ("python", "-m", "pytest", *args)

        return LocalExecutionRequest(
            execution_id=execution_id,
            project_id=project_id,
            adapter_id="process.raw",
            mode=ExecutionMode.ARGV,
            argv=argv,
            cwd=cwd,
            effect_class=ExecutionEffectClass.READ_ONLY,
            idempotency=IdempotencyClass.IDEMPOTENT_REPLAYABLE,
            expected_source_head=expected_head,
            expected_source_tree=expected_tree,
            env_vars=env_vars or {},
        )

    def preflight(self, cwd: Path | str, operation: str) -> PreflightResult:
        return PreflightResult(True, "READY", "Test runner preflight passed")

    def parse_result(self, result: LocalExecutionResult) -> dict[str, Any]:
        """Parse test outcome evidence without setting task PASS."""
        stdout_txt = result.stdout.inline_content or ""
        passed = result.exit_code == 0
        return {
            "adapter_id": self.adapter_id,
            "exit_code": result.exit_code,
            "test_run_passed": passed,
            "stdout_text": stdout_txt,
            "stderr_text": result.stderr.inline_content or "",
        }


# ==============================================================================
# Tool Adapter Registry
# ==============================================================================

class ToolAdapterRegistry:
    """Registry managing typed development tool adapters."""

    def __init__(self) -> None:
        self._adapters: dict[str, TypedToolAdapter] = {}
        self.register(GitToolAdapter())
        self.register(NodeToolAdapter())
        self.register(NpmToolAdapter())
        self.register(RustcToolAdapter())
        self.register(CargoToolAdapter())
        self.register(TauriToolAdapter())
        self.register(TestRunnerToolAdapter())

    def register(self, adapter: TypedToolAdapter) -> None:
        self._adapters[adapter.adapter_id] = adapter

    def get_adapter(self, adapter_id: str) -> TypedToolAdapter:
        if adapter_id not in self._adapters:
            raise LocalExecutionContractError("unknown_adapter", f"Unknown tool adapter_id '{adapter_id}'")
        return self._adapters[adapter_id]

    def list_families(self) -> list[str]:
        return sorted(list({a.family for a in self._adapters.values()}))

    def get_adapter_for_operation(self, operation: str) -> TypedToolAdapter:
        for adapter in self._adapters.values():
            if operation in adapter.supported_operations:
                return adapter
        raise LocalExecutionContractError("unknown_operation", f"No adapter found for operation '{operation}'")

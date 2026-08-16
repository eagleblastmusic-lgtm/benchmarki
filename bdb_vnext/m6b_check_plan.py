"""M6b deterministic CheckPlan shadow for BDB vNext.

This module is deliberately read-only with respect to execution authority.  It
turns exact, deterministic capability facts into an immutable validation plan
and compares that plan with the frozen legacy fixed-profile surface.  It does
not run commands, select a profile from model text, mutate Git, or enable
promotion.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Literal, Mapping, NoReturn

from bdb_shared.evidence import semantic_digest
from bdb_vnext.engineering_loop import ValidationCommand


CHECK_PLAN_SCHEMA = "bdb-vnext-m6b-check-plan-v1"
SHADOW_SCHEMA = "bdb-vnext-m6b-check-plan-shadow-v1"
REGISTRY_SCHEMA = "bdb-vnext-m6b-checker-registry-v1"
PROCESS_POLICY_SCHEMA = "bdb-vnext-m6b-process-policy-v1"
M6B_AUTHORITY = "devmaster.bdb.vnext.validation.check-plan-shadow"

CapabilityId = Literal[
    "python.pytest",
    "python.unittest",
    "dotnet.test",
    "shopify.theme_check",
]
ShadowStatus = Literal["MATCH", "DIFFERENT"]


class M6bError(RuntimeError):
    """Typed fail-closed M6b selection/comparison failure."""

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
    raise M6bError(code, message, details=details)


def _bounded_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024 or "\x00" in value:
        _fail("invalid_check_plan_input", f"{field} must be bounded non-empty text")
    return value


@dataclass(frozen=True)
class ProcessPolicy:
    """Exact mechanical process contract; execution is owned elsewhere."""

    shell: bool = False
    stdin: str = "DEVNULL"
    capture_stdout: bool = True
    capture_stderr: bool = True
    timeout_kill_scope: str = "PROCESS_TREE"

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": PROCESS_POLICY_SCHEMA,
            "shell": self.shell,
            "stdin": self.stdin,
            "capture_stdout": self.capture_stdout,
            "capture_stderr": self.capture_stderr,
            "timeout_kill_scope": self.timeout_kill_scope,
        }


@dataclass(frozen=True)
class CheckerSpec:
    capability_id: str
    checker_id: str
    checker_version: str
    executable_binding: str
    arguments: tuple[str, ...]
    cwd: str = "."
    timeout_seconds: float = 30.0
    max_stdout_bytes: int = 64 * 1024
    max_stderr_bytes: int = 64 * 1024
    process_policy: ProcessPolicy = ProcessPolicy()

    def as_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "checker_id": self.checker_id,
            "checker_version": self.checker_version,
            "executable_binding": self.executable_binding,
            "arguments": list(self.arguments),
            "cwd": self.cwd,
            "timeout_seconds": self.timeout_seconds,
            "max_stdout_bytes": self.max_stdout_bytes,
            "max_stderr_bytes": self.max_stderr_bytes,
            "process_policy": self.process_policy.as_dict(),
        }


CHECKER_REGISTRY: tuple[CheckerSpec, ...] = (
    CheckerSpec(
        capability_id="dotnet.test",
        checker_id="dotnet-test",
        checker_version="1",
        executable_binding="dotnet",
        arguments=(
            "test",
            "--configuration",
            "Release",
            "--nologo",
            "--verbosity",
            "minimal",
        ),
    ),
    CheckerSpec(
        capability_id="python.pytest",
        checker_id="python-pytest",
        checker_version="1",
        executable_binding="python",
        arguments=("-m", "pytest", "-q"),
    ),
    CheckerSpec(
        capability_id="python.unittest",
        checker_id="python-unittest",
        checker_version="1",
        executable_binding="python",
        arguments=(
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-p",
            "test_*.py",
            "-v",
        ),
    ),
    CheckerSpec(
        capability_id="shopify.theme_check",
        checker_id="shopify-theme-check",
        checker_version="1",
        executable_binding="shopify",
        arguments=(
            "theme",
            "check",
            "--config",
            "theme-check:recommended",
            "--fail-level",
            "error",
            "--output",
            "json",
            "--no-color",
        ),
    ),
)


def _registry_by_capability(
    registry: Iterable[CheckerSpec],
) -> dict[str, CheckerSpec]:
    result: dict[str, CheckerSpec] = {}
    for spec in registry:
        if spec.capability_id in result:
            _fail(
                "duplicate_checker_capability",
                "checker registry contains duplicate capability identity",
                details={"capability_id": spec.capability_id},
            )
        result[spec.capability_id] = spec
    return result


def registry_digest(registry: Iterable[CheckerSpec] = CHECKER_REGISTRY) -> str:
    specs = sorted((item.as_dict() for item in registry), key=lambda item: item["capability_id"])
    return semantic_digest({"schema": REGISTRY_SCHEMA, "checkers": specs})


@dataclass(frozen=True)
class PlannedCheck:
    capability_id: str
    checker_id: str
    checker_version: str
    argv: tuple[str, ...]
    cwd: str
    timeout_seconds: float
    max_stdout_bytes: int
    max_stderr_bytes: int
    process_policy: ProcessPolicy

    @property
    def checker_code_digest(self) -> str:
        return self.validation_command().checker_code_digest

    def validation_command(self) -> ValidationCommand:
        """Project onto the existing bounded ValidationRunner command type."""

        return ValidationCommand(
            checker_id=self.checker_id,
            checker_version=self.checker_version,
            argv=self.argv,
            cwd=self.cwd,
            timeout_seconds=self.timeout_seconds,
            max_stdout_bytes=self.max_stdout_bytes,
            max_stderr_bytes=self.max_stderr_bytes,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "checker_id": self.checker_id,
            "checker_version": self.checker_version,
            "checker_code_digest": self.checker_code_digest,
            "argv": list(self.argv),
            "cwd": self.cwd,
            "timeout_seconds": self.timeout_seconds,
            "max_stdout_bytes": self.max_stdout_bytes,
            "max_stderr_bytes": self.max_stderr_bytes,
            "process_policy": self.process_policy.as_dict(),
        }

    def checker_set_identity(self) -> dict[str, str]:
        return {
            "capability_id": self.capability_id,
            "checker_id": self.checker_id,
            "checker_version": self.checker_version,
        }

    def execution_identity(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "argv": list(self.argv),
            "cwd": self.cwd,
            "timeout_seconds": self.timeout_seconds,
            "max_stdout_bytes": self.max_stdout_bytes,
            "max_stderr_bytes": self.max_stderr_bytes,
            "process_policy": self.process_policy.as_dict(),
        }


@dataclass(frozen=True)
class CheckPlan:
    required_capabilities: tuple[str, ...]
    checks: tuple[PlannedCheck, ...]
    registry_digest: str
    plan_digest: str

    @property
    def checker_set_digest(self) -> str:
        return semantic_digest(
            {
                "schema": "bdb-vnext-m6b-checker-set-v1",
                "checkers": [item.checker_set_identity() for item in self.checks],
            }
        )

    @property
    def execution_contract_digest(self) -> str:
        return semantic_digest(
            {
                "schema": "bdb-vnext-m6b-execution-contract-v1",
                "checks": [item.execution_identity() for item in self.checks],
            }
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": CHECK_PLAN_SCHEMA,
            "authority": M6B_AUTHORITY,
            "mode": "SHADOW_ONLY",
            "required_capabilities": list(self.required_capabilities),
            "checks": [item.as_dict() for item in self.checks],
            "registry_digest": self.registry_digest,
            "checker_set_digest": self.checker_set_digest,
            "execution_contract_digest": self.execution_contract_digest,
            "plan_digest": self.plan_digest,
        }


class DeterministicCheckPlanSelector:
    """Pure deterministic selector from authoritative capability facts only."""

    def __init__(self, registry: Iterable[CheckerSpec] = CHECKER_REGISTRY) -> None:
        self._registry = tuple(registry)
        self._by_capability = _registry_by_capability(self._registry)
        self._registry_digest = registry_digest(self._registry)

    def plan(
        self,
        *,
        required_capabilities: Iterable[str],
        executable_bindings: Mapping[str, str],
    ) -> CheckPlan:
        required = tuple(sorted({_bounded_text(item, "capability_id") for item in required_capabilities}))
        if not required:
            _fail("required_capability_missing", "CheckPlan requires at least one capability")

        checks: list[PlannedCheck] = []
        for capability_id in required:
            spec = self._by_capability.get(capability_id)
            if spec is None:
                _fail(
                    "required_capability_unknown",
                    "required validation capability is not present in the exact checker registry",
                    details={"capability_id": capability_id},
                )
            executable = executable_bindings.get(spec.executable_binding)
            if executable is None:
                _fail(
                    "required_capability_unknown",
                    "required validation executable binding is unknown",
                    details={
                        "capability_id": capability_id,
                        "executable_binding": spec.executable_binding,
                    },
                )
            executable = _bounded_text(executable, spec.executable_binding)
            checks.append(
                PlannedCheck(
                    capability_id=spec.capability_id,
                    checker_id=spec.checker_id,
                    checker_version=spec.checker_version,
                    argv=(executable, *spec.arguments),
                    cwd=spec.cwd,
                    timeout_seconds=spec.timeout_seconds,
                    max_stdout_bytes=spec.max_stdout_bytes,
                    max_stderr_bytes=spec.max_stderr_bytes,
                    process_policy=spec.process_policy,
                )
            )

        normalized_checks = tuple(checks)
        identity = {
            "schema": CHECK_PLAN_SCHEMA,
            "authority": M6B_AUTHORITY,
            "mode": "SHADOW_ONLY",
            "required_capabilities": list(required),
            "checks": [item.as_dict() for item in normalized_checks],
            "registry_digest": self._registry_digest,
        }
        return CheckPlan(
            required_capabilities=required,
            checks=normalized_checks,
            registry_digest=self._registry_digest,
            plan_digest=semantic_digest(identity),
        )


@dataclass(frozen=True)
class LegacyProfileSnapshot:
    profile_id: str
    checks: tuple[PlannedCheck, ...]
    checker_set_digest: str
    execution_contract_digest: str


_LEGACY_PROFILE_CAPABILITY = {
    "poc_pytest": "python.pytest",
    "poc_unittest": "python.unittest",
    "poc_dotnet": "dotnet.test",
    "shopify_theme_check": "shopify.theme_check",
}


class LegacyFixedProfileAdapter:
    """Read-only shadow adapter; it is never consulted by the vNext selector."""

    def __init__(self, registry: Iterable[CheckerSpec] = CHECKER_REGISTRY) -> None:
        self._by_capability = _registry_by_capability(tuple(registry))

    def snapshot(
        self,
        *,
        profile_id: str,
        executable_bindings: Mapping[str, str],
        timeout_seconds: float,
    ) -> LegacyProfileSnapshot:
        from bdb_bridge.fixed_test_profiles import fixed_profile_arguments

        profile_id = _bounded_text(profile_id, "profile_id")
        capability_id = _LEGACY_PROFILE_CAPABILITY.get(profile_id)
        if capability_id is None:
            _fail(
                "legacy_profile_not_shadowable",
                "legacy profile has no exact M6b shadow mapping",
                details={"profile_id": profile_id},
            )
        spec = self._by_capability[capability_id]
        executable = executable_bindings.get(spec.executable_binding)
        if executable is None:
            _fail(
                "required_capability_unknown",
                "legacy profile executable binding is unknown",
                details={"executable_binding": spec.executable_binding},
            )
        executable = _bounded_text(executable, spec.executable_binding)
        arguments = tuple(fixed_profile_arguments(profile_id))
        legacy_policy = ProcessPolicy(timeout_kill_scope="PROCESS_ONLY")
        check = PlannedCheck(
            capability_id=capability_id,
            checker_id=spec.checker_id,
            checker_version=spec.checker_version,
            argv=(executable, *arguments),
            cwd=".",
            timeout_seconds=float(timeout_seconds),
            max_stdout_bytes=spec.max_stdout_bytes,
            max_stderr_bytes=spec.max_stderr_bytes,
            process_policy=legacy_policy,
        )
        checks = (check,)
        return LegacyProfileSnapshot(
            profile_id=profile_id,
            checks=checks,
            checker_set_digest=semantic_digest(
                {
                    "schema": "bdb-vnext-m6b-checker-set-v1",
                    "checkers": [item.checker_set_identity() for item in checks],
                }
            ),
            execution_contract_digest=semantic_digest(
                {
                    "schema": "bdb-vnext-m6b-execution-contract-v1",
                    "checks": [item.execution_identity() for item in checks],
                }
            ),
        )


@dataclass(frozen=True)
class ShadowComparison:
    status: ShadowStatus
    checker_set_match: bool
    execution_contract_match: bool
    reasons: tuple[str, ...]
    vnext_plan_digest: str
    vnext_checker_set_digest: str
    legacy_checker_set_digest: str
    vnext_execution_contract_digest: str
    legacy_execution_contract_digest: str
    comparison_digest: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": SHADOW_SCHEMA,
            "mode": "SHADOW_ONLY",
            "status": self.status,
            "checker_set_match": self.checker_set_match,
            "execution_contract_match": self.execution_contract_match,
            "reasons": list(self.reasons),
            "vnext_plan_digest": self.vnext_plan_digest,
            "vnext_checker_set_digest": self.vnext_checker_set_digest,
            "legacy_checker_set_digest": self.legacy_checker_set_digest,
            "vnext_execution_contract_digest": self.vnext_execution_contract_digest,
            "legacy_execution_contract_digest": self.legacy_execution_contract_digest,
            "comparison_digest": self.comparison_digest,
        }


def compare_legacy_profile(plan: CheckPlan, legacy: LegacyProfileSnapshot) -> ShadowComparison:
    reasons: list[str] = []
    checker_set_match = plan.checker_set_digest == legacy.checker_set_digest
    if not checker_set_match:
        reasons.append("checker_set_mismatch")

    vnext_checks = [item.execution_identity() for item in plan.checks]
    legacy_checks = [item.execution_identity() for item in legacy.checks]
    if len(vnext_checks) != len(legacy_checks):
        reasons.append("checker_count_mismatch")
    else:
        for index, (vnext, old) in enumerate(zip(vnext_checks, legacy_checks, strict=True)):
            if vnext["capability_id"] != old["capability_id"]:
                reasons.append(f"capability_mismatch:{index}")
            if vnext["argv"] != old["argv"]:
                reasons.append(f"argv_mismatch:{index}")
            if vnext["cwd"] != old["cwd"]:
                reasons.append(f"cwd_mismatch:{index}")
            if vnext["timeout_seconds"] != old["timeout_seconds"]:
                reasons.append(f"timeout_mismatch:{index}")
            if vnext["max_stdout_bytes"] != old["max_stdout_bytes"] or vnext["max_stderr_bytes"] != old["max_stderr_bytes"]:
                reasons.append(f"output_budget_mismatch:{index}")
            if vnext["process_policy"] != old["process_policy"]:
                reasons.append(f"process_policy_mismatch:{index}")

    execution_contract_match = plan.execution_contract_digest == legacy.execution_contract_digest
    identity = {
        "schema": SHADOW_SCHEMA,
        "vnext_plan_digest": plan.plan_digest,
        "vnext_checker_set_digest": plan.checker_set_digest,
        "legacy_checker_set_digest": legacy.checker_set_digest,
        "vnext_execution_contract_digest": plan.execution_contract_digest,
        "legacy_execution_contract_digest": legacy.execution_contract_digest,
        "checker_set_match": checker_set_match,
        "execution_contract_match": execution_contract_match,
        "reasons": reasons,
    }
    return ShadowComparison(
        status="MATCH" if checker_set_match and execution_contract_match else "DIFFERENT",
        checker_set_match=checker_set_match,
        execution_contract_match=execution_contract_match,
        reasons=tuple(reasons),
        vnext_plan_digest=plan.plan_digest,
        vnext_checker_set_digest=plan.checker_set_digest,
        legacy_checker_set_digest=legacy.checker_set_digest,
        vnext_execution_contract_digest=plan.execution_contract_digest,
        legacy_execution_contract_digest=legacy.execution_contract_digest,
        comparison_digest=semantic_digest(identity),
    )


__all__ = [
    "CHECKER_REGISTRY",
    "CHECK_PLAN_SCHEMA",
    "CheckerSpec",
    "CheckPlan",
    "DeterministicCheckPlanSelector",
    "LegacyFixedProfileAdapter",
    "LegacyProfileSnapshot",
    "M6B_AUTHORITY",
    "M6bError",
    "PlannedCheck",
    "ProcessPolicy",
    "SHADOW_SCHEMA",
    "ShadowComparison",
    "compare_legacy_profile",
    "registry_digest",
]

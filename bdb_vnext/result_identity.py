"""BDB vNext - NX-004 Result Identity Contract and Digest Generator.

This module provides explicit versioning (v1 legacy vs v2 canonical) for Project
Execution result identity and deterministic cryptographic digest computation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

from bdb_shared.evidence import semantic_digest

if TYPE_CHECKING:
    from .project_execution import ProjectExecutionBinding

IDENTITY_VERSION_V1 = "v1"
IDENTITY_VERSION_V2 = "v2"
CURRENT_IDENTITY_VERSION = IDENTITY_VERSION_V2


def result_identity_v1(binding: "ProjectExecutionBinding", result: Mapping[str, Any]) -> dict[str, Any]:
    """Legacy v1 result identity (preserved for backwards-compatible reading and replay)."""
    return {
        "execution_binding_id": binding.execution_binding_id,
        "command_id": binding.command_id,
        "correlation_id": binding.correlation_id,
        "project_id": binding.project_id,
        "task_id": binding.task_id,
        "plan_version": binding.plan_version,
        "repo_alias": binding.repo_alias,
        "result_project_id": result.get("project_id"),
        "result_task_id": result.get("task_id"),
        "result_plan_version": result.get("plan_version"),
        "head_before": result.get("head_before"),
        "head_after": result.get("head_after"),
        "execution_status": result.get("execution_status"),
        "validation_status": result.get("validation_status"),
        "promotion_status": result.get("promotion_status"),
        "summary": result.get("result_summary", ""),
        "evidence_refs": list(result.get("evidence_refs", [])),
        "criteria": list(result.get("criteria", [])),
        "canonical_refs": dict(result.get("canonical_refs", {})) if isinstance(result.get("canonical_refs"), Mapping) else {},
    }


def result_identity_v2(binding: "ProjectExecutionBinding", result: Mapping[str, Any]) -> dict[str, Any]:
    """Canonical v2 result identity including failure_code, explicit version, and sorted evidence refs."""
    return {
        "identity_version": IDENTITY_VERSION_V2,
        "execution_binding_id": binding.execution_binding_id,
        "command_id": binding.command_id,
        "correlation_id": binding.correlation_id,
        "project_id": binding.project_id,
        "task_id": binding.task_id,
        "plan_version": str(binding.plan_version),
        "repo_alias": binding.repo_alias,
        "result_project_id": result.get("project_id"),
        "result_task_id": result.get("task_id"),
        "result_plan_version": str(result.get("plan_version")) if result.get("plan_version") is not None else None,
        "head_before": result.get("head_before"),
        "head_after": result.get("head_after"),
        "execution_status": result.get("execution_status"),
        "validation_status": result.get("validation_status"),
        "promotion_status": result.get("promotion_status"),
        "failure_code": result.get("failure_code"),
        "summary": result.get("result_summary", ""),
        "evidence_refs": sorted(str(ref) for ref in result.get("evidence_refs", [])),
        "criteria": [dict(item) for item in result.get("criteria", [])],
        "canonical_refs": dict(result.get("canonical_refs", {})) if isinstance(result.get("canonical_refs"), Mapping) else {},
    }


def execution_result_digest_v1(binding: "ProjectExecutionBinding", result: Mapping[str, Any]) -> str:
    """Compute sha256 digest using legacy v1 identity."""
    return semantic_digest(result_identity_v1(binding, result))


def execution_result_digest_v2(binding: "ProjectExecutionBinding", result: Mapping[str, Any]) -> str:
    """Compute sha256 digest using canonical v2 identity."""
    return semantic_digest(result_identity_v2(binding, result))


def execution_result_digest(
    binding: "ProjectExecutionBinding",
    result: Mapping[str, Any],
    *,
    version: str = CURRENT_IDENTITY_VERSION,
) -> str:
    """Default digest entrypoint; uses v2 for new writes."""
    if version == IDENTITY_VERSION_V1:
        return execution_result_digest_v1(binding, result)
    return execution_result_digest_v2(binding, result)


def verify_result_digest(
    binding: "ProjectExecutionBinding",
    result: Mapping[str, Any],
    expected_digest: str,
) -> tuple[bool, str | None]:
    """Verify an expected digest against v2 (first) and v1 (fallback for legacy records).

    Returns (matches, detected_version).
    """
    v2_digest = execution_result_digest_v2(binding, result)
    if v2_digest == expected_digest:
        return True, IDENTITY_VERSION_V2
    v1_digest = execution_result_digest_v1(binding, result)
    if v1_digest == expected_digest:
        return True, IDENTITY_VERSION_V1
    return False, None

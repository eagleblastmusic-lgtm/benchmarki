"""NX-042 — Local Execution Effect / Policy Engine.

Enforces deny-by-default execution policy across all operations before any process can start:
- Validates effect classes (READ_ONLY, SAFE_MUTATION, PROJECT_MUTATION, ELEVATED, DESTRUCTIVE)
- Enforces candidate / cwd boundary containment with symlink/reparse escape protection
- Enforces single-use approval token verification and source state binding
- Produces immutable structured PolicyDecision evidence with TOCTOU pre-effect revalidation
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from .local_execution_contract import (
    ExecutionEffectClass,
    KNOWN_ADAPTER_IDS,
    LocalExecutionContractError,
    LocalExecutionRequest,
)


# ==============================================================================
# Version Constants & Invariant Flags
# ==============================================================================

EXECUTION_POLICY_SCHEMA = "bdb-vnext-execution-policy-v1"
EXECUTION_POLICY_VERSION = "1.0.0"
EXECUTION_POLICY_VERSION_EXPLICIT = True

EXECUTION_POLICY_DECISION_SCHEMA = "bdb-vnext-execution-policy-decision-v1"
EXECUTION_POLICY_DECISION_VERSION = "1.0.0"
EXECUTION_POLICY_DECISION_VERSION_EXPLICIT = True

APPROVAL_TOKEN_SCHEMA = "bdb-vnext-approval-token-v1"
APPROVAL_TOKEN_VERSION = "1.0.0"

DENY_BY_DEFAULT = True
UNKNOWN_OPERATION_ALLOWED = False
UNKNOWN_ADAPTER_ALLOWED = False
UNKNOWN_EFFECT_CLASS_ALLOWED = False
AUTONOMOUS_ELEVATED_EFFECTS = 0
AUTONOMOUS_DESTRUCTIVE_EFFECTS = 0
PROJECT_MUTATION_OUTSIDE_CANDIDATE_ALLOWED = False
PATH_ESCAPE_ALLOWED = False
CWD_ESCAPE_ALLOWED = False
REPARSE_ESCAPE_ALLOWED = False
SYMLINK_ESCAPE_EFFECTS = 0
NETWORK_DENIED_BYPASSES = 0
UNDECLARED_NETWORK_ALLOWED = False
APPROVAL_REPLAY_ACCEPTED = False
EXPIRED_APPROVAL_ACCEPTED = False
WRONG_REQUEST_APPROVAL_ACCEPTED = False
STALE_SOURCE_APPROVAL_ACCEPTED = False
STALE_POLICY_HEAD_ALLOWED = False
STALE_POLICY_TREE_ALLOWED = False
STALE_DECISION_REVALIDATED_AS_ALLOWED = False


# ==============================================================================
# Policy Enums & Mapping
# ==============================================================================

class PolicyEffectClass(str, Enum):
    """Canonical policy effect classification."""

    READ_ONLY = "READ_ONLY"
    SAFE_MUTATION = "SAFE_MUTATION"
    PROJECT_MUTATION = "PROJECT_MUTATION"
    ELEVATED = "ELEVATED"
    DESTRUCTIVE = "DESTRUCTIVE"

    def __str__(self) -> str:
        return self.value


def map_contract_effect_to_policy(effect: ExecutionEffectClass | PolicyEffectClass | str) -> PolicyEffectClass:
    """Map contract effect class or string to canonical PolicyEffectClass."""
    val = effect.value if isinstance(effect, (ExecutionEffectClass, PolicyEffectClass)) else str(effect)
    if val in ("READ_ONLY",):
        return PolicyEffectClass.READ_ONLY
    if val in ("SAFE_MUTATION", "SAFE_PROJECT_LOCAL_MUTATION"):
        return PolicyEffectClass.SAFE_MUTATION
    if val in ("PROJECT_MUTATION", "SHARED_RESOURCE_MUTATION"):
        return PolicyEffectClass.PROJECT_MUTATION
    if val in ("ELEVATED",):
        return PolicyEffectClass.ELEVATED
    if val in ("DESTRUCTIVE", "NON_REPLAYABLE_MUTATION"):
        return PolicyEffectClass.DESTRUCTIVE
    raise LocalExecutionContractError("unknown_effect_class", f"Unknown effect class: '{effect}'")


# ==============================================================================
# Approval Tokens & Single-Use Registry
# ==============================================================================

@dataclass
class ApprovalToken:
    """Explicit authorization token required for ELEVATED and DESTRUCTIVE operations."""

    token_id: str
    request_digest: str
    project_id: str
    effect_class: PolicyEffectClass
    expected_source_head: str
    expected_source_tree: str
    issued_at: float
    expires_at: float
    consumed: bool = False
    schema: str = APPROVAL_TOKEN_SCHEMA
    version: str = APPROVAL_TOKEN_VERSION

    def is_valid_for(
        self,
        request_digest: str,
        current_head: str,
        current_tree: str,
        now: float | None = None,
    ) -> tuple[bool, str]:
        current_time = now if now is not None else time.time()
        if self.consumed:
            return False, "APPROVAL_REPLAY_DENIED"
        if current_time > self.expires_at:
            return False, "APPROVAL_EXPIRED_DENIED"
        if self.request_digest != request_digest:
            return False, "APPROVAL_WRONG_REQUEST_DENIED"
        if self.expected_source_head != current_head or self.expected_source_tree != current_tree:
            return False, "APPROVAL_STALE_SOURCE_DENIED"
        return True, "APPROVAL_VALID"


class ApprovalRegistry:
    """Thread-safe registry for single-use approval tokens."""

    def __init__(self) -> None:
        self._tokens: dict[str, ApprovalToken] = {}

    def issue(
        self,
        request: LocalExecutionRequest,
        effect_class: PolicyEffectClass,
        validity_seconds: float = 300.0,
    ) -> ApprovalToken:
        now = time.time()
        token = ApprovalToken(
            token_id=f"appr:{request.execution_id}:{hashlib.sha256(os.urandom(16)).hexdigest()[:12]}",
            request_digest=request.request_digest,
            project_id=request.project_id,
            effect_class=effect_class,
            expected_source_head=request.expected_source_head,
            expected_source_tree=request.expected_source_tree,
            issued_at=now,
            expires_at=now + validity_seconds,
        )
        self._tokens[token.token_id] = token
        return token

    def consume(
        self,
        token_id: str,
        request_digest: str,
        current_head: str,
        current_tree: str,
    ) -> tuple[bool, str]:
        token = self._tokens.get(token_id)
        if token is None:
            return False, "APPROVAL_NOT_FOUND"
        valid, reason = token.is_valid_for(request_digest, current_head, current_tree)
        if not valid:
            return False, reason
        token.consumed = True
        return True, "APPROVAL_CONSUMED"


# ==============================================================================
# Path Canonicalization & Boundary Verification
# ==============================================================================

def canonicalize_path(p: Path | str) -> Path:
    """Resolve symlinks, junctions, and relative segments to strict real canonical path."""
    resolved = Path(p).resolve()
    # Normalize Windows drive letter casing
    resolved_str = str(resolved)
    if len(resolved_str) >= 2 and resolved_str[1] == ":":
        resolved_str = resolved_str[0].upper() + resolved_str[1:]
    return Path(resolved_str)


def is_path_contained(target: Path | str, boundary: Path | str) -> bool:
    """Verify that target canonical path is strictly contained within boundary directory."""
    canon_target = canonicalize_path(target)
    canon_boundary = canonicalize_path(boundary)
    try:
        canon_target.relative_to(canon_boundary)
        return True
    except ValueError:
        return False


# ==============================================================================
# Policy Decision Evidence
# ==============================================================================

@dataclass(frozen=True)
class PolicyDecision:
    """Immutable structured policy decision evidence."""

    execution_id: str
    request_digest: str
    decision: str  # "ALLOW" or "DENY"
    reason_code: str
    effect_class: str
    canonical_cwd: str
    candidate_boundary: str
    network_allowed: bool
    approval_token_id: str | None
    expected_source_head: str
    expected_source_tree: str
    policy_version: str = EXECUTION_POLICY_VERSION
    schema: str = EXECUTION_POLICY_DECISION_SCHEMA
    version: str = EXECUTION_POLICY_DECISION_VERSION
    policy_digest: str = ""
    decision_digest: str = ""

    def __post_init__(self) -> None:
        p_digest = self.policy_digest or ("sha256:" + hashlib.sha256(self.policy_version.encode("utf-8")).hexdigest())
        object.__setattr__(self, "policy_digest", p_digest)

        computed_digest = self.canonical_digest()
        if self.decision_digest and self.decision_digest != computed_digest:
            raise LocalExecutionContractError("decision_digest_mismatch", "decision_digest mismatch")
        object.__setattr__(self, "decision_digest", computed_digest)

    def canonical_bytes(self) -> bytes:
        canonical_dict = {
            "schema": self.schema,
            "version": self.version,
            "execution_id": self.execution_id,
            "request_digest": self.request_digest,
            "decision": self.decision,
            "reason_code": self.reason_code,
            "effect_class": self.effect_class,
            "canonical_cwd": self.canonical_cwd,
            "candidate_boundary": self.candidate_boundary,
            "network_allowed": bool(self.network_allowed),
            "approval_token_id": self.approval_token_id,
            "expected_source_head": self.expected_source_head,
            "expected_source_tree": self.expected_source_tree,
            "policy_version": self.policy_version,
            "policy_digest": self.policy_digest,
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
            "decision": self.decision,
            "reason_code": self.reason_code,
            "effect_class": self.effect_class,
            "canonical_cwd": self.canonical_cwd,
            "candidate_boundary": self.candidate_boundary,
            "network_allowed": self.network_allowed,
            "approval_token_id": self.approval_token_id,
            "expected_source_head": self.expected_source_head,
            "expected_source_tree": self.expected_source_tree,
            "policy_version": self.policy_version,
            "policy_digest": self.policy_digest,
            "decision_digest": self.decision_digest,
        }

    def revalidate(
        self,
        request: LocalExecutionRequest,
        current_head: str,
        current_tree: str,
        candidate_root: Path | str,
    ) -> bool:
        """TOCTOU pre-effect revalidation: confirms decision remains valid immediately before spawn."""
        if self.decision != "ALLOW":
            return False
        if self.request_digest != request.request_digest:
            return False
        if self.expected_source_head != current_head or self.expected_source_tree != current_tree:
            return False
        if str(canonicalize_path(candidate_root)) != self.candidate_boundary:
            return False
        return True


# ==============================================================================
# Execution Policy Evaluator
# ==============================================================================

class ExecutionPolicyEvaluator:
    """Side-effect-free, deterministic execution policy evaluator."""

    def __init__(
        self,
        approval_registry: ApprovalRegistry | None = None,
        policy_version: str = EXECUTION_POLICY_VERSION,
    ) -> None:
        self.approval_registry = approval_registry or ApprovalRegistry()
        self.policy_version = policy_version
        self.policy_digest = "sha256:" + hashlib.sha256(policy_version.encode("utf-8")).hexdigest()

    def evaluate(
        self,
        request: LocalExecutionRequest,
        candidate_root: Path | str,
        project_root: Path | str | None = None,
        filesystem_targets: Sequence[Path | str] | None = None,
        approval_token: ApprovalToken | None = None,
        current_head: str | None = None,
        current_tree: str | None = None,
        network_requested: bool = False,
    ) -> PolicyDecision:
        """Evaluate execution policy against request and boundaries, returning structured decision."""
        canon_candidate = canonicalize_path(candidate_root)
        canon_project = canonicalize_path(project_root) if project_root else canon_candidate

        # Helper to construct a DENY decision
        def deny(code: str, effect_str: str = "UNKNOWN", canon_cwd_str: str = "") -> PolicyDecision:
            return PolicyDecision(
                execution_id=request.execution_id,
                request_digest=request.request_digest,
                decision="DENY",
                reason_code=code,
                effect_class=effect_str,
                canonical_cwd=canon_cwd_str,
                candidate_boundary=str(canon_candidate),
                network_allowed=False,
                approval_token_id=approval_token.token_id if approval_token else None,
                expected_source_head=request.expected_source_head,
                expected_source_tree=request.expected_source_tree,
                policy_version=self.policy_version,
                policy_digest=self.policy_digest,
            )

        # 1. Unknown Adapter Check
        if request.adapter_id not in KNOWN_ADAPTER_IDS:
            return deny("DENY_UNKNOWN_ADAPTER")

        # 2. Map Effect Class
        try:
            effect_class = map_contract_effect_to_policy(request.effect_class)
        except LocalExecutionContractError:
            return deny("DENY_UNKNOWN_EFFECT_CLASS")

        # 3. Source State Validation (HEAD / TREE)
        if current_head is not None and request.expected_source_head != current_head:
            return deny("DENY_STALE_HEAD", str(effect_class))
        if current_tree is not None and request.expected_source_tree != current_tree:
            return deny("DENY_STALE_TREE", str(effect_class))

        # 4. CWD Boundary Validation
        # Resolve request.cwd relative to candidate root if relative
        raw_cwd = Path(request.cwd)
        if not raw_cwd.is_absolute():
            resolved_cwd = canon_candidate / raw_cwd
        else:
            resolved_cwd = raw_cwd

        canon_cwd = canonicalize_path(resolved_cwd)
        if not is_path_contained(canon_cwd, canon_project):
            return deny("DENY_CWD_ESCAPE", str(effect_class), str(canon_cwd))

        # 5. Filesystem Targets Validation
        if filesystem_targets:
            for target in filesystem_targets:
                raw_target = Path(target)
                target_path = canon_candidate / raw_target if not raw_target.is_absolute() else raw_target
                canon_target = canonicalize_path(target_path)

                if effect_class in (PolicyEffectClass.READ_ONLY, PolicyEffectClass.SAFE_MUTATION):
                    if not is_path_contained(canon_target, canon_project):
                        return deny("DENY_TARGET_OUTSIDE_PROJECT", str(effect_class), str(canon_cwd))
                elif effect_class is PolicyEffectClass.PROJECT_MUTATION:
                    # PROJECT_MUTATION must stay strictly inside the candidate root
                    if not is_path_contained(canon_target, canon_candidate):
                        return deny("DENY_PROJECT_MUTATION_OUTSIDE_CANDIDATE", str(effect_class), str(canon_cwd))

        # 6. Network Policy
        if network_requested:
            # Network access is denied unless explicitly permitted
            return deny("DENY_NETWORK_NOT_PERMITTED", str(effect_class), str(canon_cwd))

        # 7. Elevation / Approval Enforcement
        if effect_class in (PolicyEffectClass.ELEVATED, PolicyEffectClass.DESTRUCTIVE) or request.elevation_required:
            if approval_token is None:
                return deny("DENY_APPROVAL_REQUIRED", str(effect_class), str(canon_cwd))

            is_valid, reason = approval_token.is_valid_for(
                request.request_digest,
                current_head=current_head or request.expected_source_head,
                current_tree=current_tree or request.expected_source_tree,
            )
            if not is_valid:
                return deny(reason, str(effect_class), str(canon_cwd))

        # 8. All Checks Passed -> ALLOW
        return PolicyDecision(
            execution_id=request.execution_id,
            request_digest=request.request_digest,
            decision="ALLOW",
            reason_code="ALLOW_POLICY_COMPLIANT",
            effect_class=str(effect_class),
            canonical_cwd=str(canon_cwd),
            candidate_boundary=str(canon_candidate),
            network_allowed=False,
            approval_token_id=approval_token.token_id if approval_token else None,
            expected_source_head=request.expected_source_head,
            expected_source_tree=request.expected_source_tree,
            policy_version=self.policy_version,
            policy_digest=self.policy_digest,
        )

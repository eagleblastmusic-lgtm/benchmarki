"""BDB vNext - NX-003 Binding and Attempt Lifecycle Contract.

This module defines the canonical lifecycle states, transitions, invariant checkers,
and reconciliation routines for Project Execution bindings and attempts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

# Canonical Binding Statuses
STATUS_ACTIVE = "ACTIVE"
STATUS_ACCEPTED = "ACCEPTED"
STATUS_FAILED = "FAILED"
STATUS_SUPERSEDED = "SUPERSEDED"

BINDING_STATUS_VALUES = frozenset({
    STATUS_ACTIVE,
    STATUS_ACCEPTED,
    STATUS_FAILED,
    STATUS_SUPERSEDED,
})

# Legal State Transitions
# ACTIVE -> ACCEPTED (upon PASS result acceptance)
# ACTIVE -> FAILED   (upon FAIL / BLOCKED result recording)
# ACTIVE -> SUPERSEDED (upon retry or creation of newer binding generation)
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_ACTIVE: frozenset({STATUS_ACCEPTED, STATUS_FAILED, STATUS_SUPERSEDED}),
    STATUS_ACCEPTED: frozenset(),   # Terminal
    STATUS_FAILED: frozenset(),     # Terminal for this generation
    STATUS_SUPERSEDED: frozenset(), # Terminal
}


class BindingLifecycleError(ValueError):
    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def validate_binding_transition(current_status: str, new_status: str) -> None:
    """Validate that transition from current_status to new_status is legal.

    Raises BindingLifecycleError if the transition is illegal or invalid.
    """
    if current_status not in BINDING_STATUS_VALUES:
        raise BindingLifecycleError(
            "binding_status_invalid",
            f"current binding status '{current_status}' is unknown",
            details={"current_status": current_status, "new_status": new_status},
        )
    if new_status not in BINDING_STATUS_VALUES:
        raise BindingLifecycleError(
            "binding_status_invalid",
            f"target binding status '{new_status}' is unknown",
            details={"current_status": current_status, "new_status": new_status},
        )
    if current_status == new_status:
        return  # Idempotent no-op

    allowed = LEGAL_TRANSITIONS.get(current_status, frozenset())
    if new_status not in allowed:
        raise BindingLifecycleError(
            "illegal_binding_transition",
            f"illegal transition from '{current_status}' to '{new_status}' (terminal state cannot be mutated)",
            details={"current_status": current_status, "new_status": new_status},
        )


def check_binding_lifecycle_invariants(execution: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """Verify canonical binding invariants on an execution state document:

    I1. At most one ACTIVE binding per (task_id, plan_version).
    I2. Generation numbers are monotonic positive integers per task.
    I3. Current binding is consistent with active bindings.
    I4. Terminal bindings (ACCEPTED, FAILED, SUPERSEDED) have legal flags.
    """
    errors: list[str] = []
    bindings = execution.get("bindings", [])
    if not isinstance(bindings, list):
        return False, ["execution.bindings is not a list"]

    # Group bindings by (task_id, plan_version)
    task_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for idx, b in enumerate(bindings):
        if not isinstance(b, dict):
            errors.append(f"bindings[{idx}] is not an object")
            continue
        task_id = str(b.get("task_id", ""))
        plan_version = str(b.get("plan_version", ""))
        task_groups.setdefault((task_id, plan_version), []).append(b)

    for (task_id, plan_version), group in task_groups.items():
        # I1: At most one ACTIVE binding
        active_bindings = [b for b in group if b.get("status") == STATUS_ACTIVE and not b.get("superseded")]
        if len(active_bindings) > 1:
            active_ids = [b.get("execution_binding_id") for b in active_bindings]
            errors.append(
                f"Task ({task_id}, v={plan_version}) has {len(active_bindings)} ACTIVE bindings: {active_ids}"
            )

        # I2: Monotonic generation numbers
        generations = [int(b.get("generation", 1)) for b in group if "generation" in b]
        if generations:
            # Check for duplicate generations or non-positive
            if any(g < 1 for g in generations):
                errors.append(f"Task ({task_id}, v={plan_version}) has generation < 1: {generations}")
            # Generations in append order must be strictly increasing
            group_gens = [int(b.get("generation", 1)) for b in group]
            for i in range(1, len(group_gens)):
                if group_gens[i] <= group_gens[i - 1]:
                    errors.append(
                        f"Task ({task_id}, v={plan_version}) generations not strictly increasing: {group_gens}"
                    )
                    break

        # I4: Check status values
        for b in group:
            status = b.get("status", STATUS_ACTIVE)
            if status not in BINDING_STATUS_VALUES:
                errors.append(f"Binding {b.get('execution_binding_id')} has invalid status '{status}'")
            if status == STATUS_SUPERSEDED and not b.get("superseded"):
                errors.append(f"Binding {b.get('execution_binding_id')} has status SUPERSEDED but superseded=False")

    return (len(errors) == 0, errors)


def reconcile_execution_bindings(execution: dict[str, Any]) -> dict[str, Any]:
    """Reconcile existing multiple-ACTIVE bindings deterministically and idempotently.

    For each (task_id, plan_version), if multiple bindings are marked ACTIVE:
    - Identifies the latest binding (matching current_binding_id or latest created_at / highest generation).
    - Terminalizes all prior active bindings to SUPERSEDED (superseded=True).
    - Normalizes monotonic generation numbers.
    """
    bindings = execution.get("bindings", [])
    if not bindings or not isinstance(bindings, list):
        return execution

    current_binding_id = execution.get("current_binding_id")
    now_iso = _utc_now()

    # Group bindings
    task_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for b in bindings:
        if isinstance(b, dict):
            task_id = str(b.get("task_id", ""))
            plan_version = str(b.get("plan_version", ""))
            task_groups.setdefault((task_id, plan_version), []).append(b)

    for (task_id, plan_version), group in task_groups.items():
        # Sort chronologically
        sorted_group = sorted(
            group,
            key=lambda item: (item.get("created_at", ""), item.get("execution_binding_id", ""))
        )

        # Assign strictly monotonic generations if missing or non-monotonic
        for idx, b in enumerate(sorted_group, start=1):
            if "generation" not in b or int(b.get("generation", 1)) < idx:
                b["generation"] = idx

        # Find multiple active
        active_bindings = [b for b in sorted_group if b.get("status") == STATUS_ACTIVE and not b.get("superseded")]
        if len(active_bindings) > 1:
            # Determine canonical active: preference to current_binding_id, else latest created
            canonical_active = None
            if current_binding_id:
                canonical_active = next((b for b in active_bindings if b.get("execution_binding_id") == current_binding_id), None)
            if canonical_active is None:
                canonical_active = active_bindings[-1]

            # Supersede all others
            for b in active_bindings:
                if b is not canonical_active:
                    b["status"] = STATUS_SUPERSEDED
                    b["superseded"] = True
                    if not b.get("finished_at"):
                        b["finished_at"] = now_iso

    return execution

"""Fail-closed Native Messaging route transition primitives for M11c maintenance.

Windows Registry and the external Bootstrap slot state are different
persistence domains. This module deliberately models the route half as a
small recoverable state machine: exact old routes may be restored before the
new Bootstrap pointer is published; after publication only candidate
roll-forward is legal.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, NoReturn

ROUTE_TRANSITION_PLAN_SCHEMA = "bdb-vnext-m11c-route-transition-plan-v1"
ROUTE_TRANSITION_STATE_SCHEMA = "bdb-vnext-m11c-route-transition-state-v1"
ROUTE_RECOVERY_MODE = "ROLLBACK_BEFORE_BOOTSTRAP_ROLL_FORWARD_AFTER"
ROUTE_VIEWS = ("32", "64")


class RouteTransitionError(RuntimeError):
    """Typed route-transition failure; never silently retries a foreign route."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RouteTransitionFault(RuntimeError):
    """Test-only crash marker for a route transition boundary."""

    def __init__(self, stage: str) -> None:
        super().__init__(stage)
        self.stage = stage


def _fail(code: str, message: str) -> NoReturn:
    raise RouteTransitionError(code, message)


def _path(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        _fail("route_invalid", f"{field} must be a non-empty path")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        _fail("route_invalid", f"{field} must be absolute")
    return str(candidate)


def canonical_routes(observation: Mapping[str, Any]) -> tuple[dict[str, str], ...]:
    """Validate the exact HKCU 32/64 target route and absence of Legacy/HKLM."""

    target = observation.get("target")
    legacy = observation.get("legacy")
    if not isinstance(target, list) or not isinstance(legacy, list):
        _fail("route_observation_invalid", "Native route observation is incomplete")
    if legacy:
        _fail("legacy_route_present", "Legacy Native Messaging route is present")
    values: dict[str, str] = {}
    for item in target:
        if not isinstance(item, Mapping):
            _fail("route_observation_invalid", "Native target route entry is malformed")
        root = item.get("root")
        view = item.get("view")
        if root != "HKCU" or view not in ROUTE_VIEWS:
            _fail("route_conflict", "Native target route contains a foreign root/view")
        if view in values:
            _fail("route_conflict", "Native target route contains a duplicate registry view")
        values[view] = _path(item.get("value"), field=f"target[{view}].value")
    if set(values) != set(ROUTE_VIEWS):
        _fail("route_missing_view", "Native target route must contain HKCU 32-bit and 64-bit views")
    return tuple({"root": "HKCU", "view": view, "value": values[view]} for view in ROUTE_VIEWS)


def route_values(routes: Sequence[Mapping[str, str]]) -> tuple[str, str]:
    canonical = canonical_routes({"target": [dict(item) for item in routes], "legacy": []})
    return tuple(item["value"] for item in canonical)  # type: ignore[return-value]


def classify_route(observation: Mapping[str, Any], *, old_routes: Sequence[Mapping[str, str]], candidate_manifest_path: str) -> str:
    """Return OLD, CANDIDATE, PARTIAL, or FOREIGN without guessing."""

    target = observation.get("target")
    legacy = observation.get("legacy")
    if not isinstance(target, list) or not isinstance(legacy, list) or legacy:
        return "FOREIGN"
    old = route_values(old_routes)
    candidate = _path(candidate_manifest_path, field="candidate_manifest_path")
    expected = {"32": old[0], "64": old[1]}
    seen: dict[str, str] = {}
    for item in target:
        if not isinstance(item, Mapping) or item.get("root") != "HKCU" or item.get("view") not in ROUTE_VIEWS:
            return "FOREIGN"
        view = str(item["view"])
        if view in seen:
            return "FOREIGN"
        try:
            seen[view] = _path(item.get("value"), field=f"target[{view}].value")
        except RouteTransitionError:
            return "FOREIGN"
    if set(seen) != set(ROUTE_VIEWS):
        return "PARTIAL"
    if all(os.path.normcase(seen[view]) == os.path.normcase(expected[view]) for view in ROUTE_VIEWS):
        return "OLD"
    if all(os.path.normcase(seen[view]) == os.path.normcase(candidate) for view in ROUTE_VIEWS):
        return "CANDIDATE"
    if all(os.path.normcase(seen[view]) in {os.path.normcase(expected[view]), os.path.normcase(candidate)} for view in ROUTE_VIEWS):
        return "PARTIAL"
    return "FOREIGN"


def transition_to_candidate(
    *,
    old_routes: Sequence[Mapping[str, str]],
    candidate_manifest_path: str,
    observe: Callable[[], Mapping[str, Any]],
    write_view: Callable[[str, str], None],
    fault_hook: Callable[[str], None] | None = None,
) -> Mapping[str, Any]:
    """Switch both HKCU views, proving exact readback after each bounded step."""

    candidate = _path(candidate_manifest_path, field="candidate_manifest_path")
    before = observe()
    if classify_route(before, old_routes=old_routes, candidate_manifest_path=candidate) != "OLD":
        _fail("route_plan_stale", "current Native route differs from the exact old route")
    if fault_hook:
        fault_hook("before_first_registry_change")
    write_view("32", candidate)
    if fault_hook:
        fault_hook("after_hkcu_32")
    write_view("64", candidate)
    if fault_hook:
        fault_hook("after_hkcu_64")
    after = observe()
    if classify_route(after, old_routes=old_routes, candidate_manifest_path=candidate) != "CANDIDATE":
        _fail("route_readback_mismatch", "candidate Native route did not read back exactly")
    if fault_hook:
        fault_hook("after_route_readback")
    return after



def roll_forward_to_candidate(
    *,
    old_routes: Sequence[Mapping[str, str]],
    candidate_manifest_path: str,
    observe: Callable[[], Mapping[str, Any]],
    write_view: Callable[[str, str], None],
    fault_hook: Callable[[str], None] | None = None,
) -> Mapping[str, Any]:
    """Repair OLD/PARTIAL route only after Bootstrap publication; never roll back."""

    candidate = _path(candidate_manifest_path, field="candidate_manifest_path")
    current = observe()
    phase = classify_route(current, old_routes=old_routes, candidate_manifest_path=candidate)
    if phase == "CANDIDATE":
        return current
    if phase not in {"OLD", "PARTIAL"}:
        _fail("route_roll_forward_foreign", "Native route cannot be safely rolled forward")
    if fault_hook:
        fault_hook("during_recovery")
    write_view("32", candidate)
    write_view("64", candidate)
    recovered = observe()
    if classify_route(recovered, old_routes=old_routes, candidate_manifest_path=candidate) != "CANDIDATE":
        _fail("route_roll_forward_mismatch", "exact candidate Native route was not restored")
    return recovered

def restore_old_route(
    *,
    old_routes: Sequence[Mapping[str, str]],
    candidate_manifest_path: str,
    observe: Callable[[], Mapping[str, Any]],
    write_view: Callable[[str, str], None],
    fault_hook: Callable[[str], None] | None = None,
) -> Mapping[str, Any]:
    """Restore exact old HKCU views; refuse foreign or Legacy state."""

    old = route_values(old_routes)
    current = observe()
    phase = classify_route(current, old_routes=old_routes, candidate_manifest_path=candidate_manifest_path)
    if phase == "OLD":
        return current
    if phase not in {"CANDIDATE", "PARTIAL"}:
        _fail("route_recovery_foreign", "Native route cannot be safely restored")
    if fault_hook:
        fault_hook("during_recovery")
    write_view("32", old[0])
    write_view("64", old[1])
    recovered = observe()
    if classify_route(recovered, old_routes=old_routes, candidate_manifest_path=candidate_manifest_path) != "OLD":
        _fail("route_recovery_mismatch", "exact old Native route was not restored")
    return recovered


__all__ = [
    "ROUTE_RECOVERY_MODE",
    "ROUTE_TRANSITION_PLAN_SCHEMA",
    "ROUTE_TRANSITION_STATE_SCHEMA",
    "ROUTE_VIEWS",
    "RouteTransitionError",
    "RouteTransitionFault",
    "canonical_routes",
    "classify_route",
    "restore_old_route",
    "roll_forward_to_candidate",
    "route_values",
    "transition_to_candidate",
]

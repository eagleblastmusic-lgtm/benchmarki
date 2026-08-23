"""Bounded diagnostic comparison for local and GitHub Actions verification.

This module is deliberately observational. It does not dispatch workflows,
choose a validation authority, or persist execution state. A comparison is
valid only when both observations name the same commit and compatible lock
identity; otherwise the result is explicitly unavailable or rejected.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Mapping


VERIFICATION_RACE_SCHEMA = "bdb-verification-race-v1"
_GIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_STAGES = frozenset({"startup_queue", "dependencies", "typecheck", "frontend_build", "cargo_check", "smoke", "total"})


class VerificationRaceError(ValueError):
    """Typed rejection for an unsafe or malformed diagnostic observation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise VerificationRaceError("verification_race_field_invalid", f"{field} is invalid")
    return value


@dataclass(frozen=True)
class VerificationMeasurement:
    source: str
    commit_sha: str
    state: str
    stage_seconds: Mapping[str, float]
    lock_digest: str | None = None
    total_seconds: float | None = None
    cache_mode: str = "UNKNOWN"
    external_run_id: str | None = None
    schema: str = VERIFICATION_RACE_SCHEMA

    @classmethod
    def from_mapping(cls, value: object) -> "VerificationMeasurement":
        if not isinstance(value, Mapping) or value.get("schema") != VERIFICATION_RACE_SCHEMA:
            raise VerificationRaceError("verification_race_schema_invalid", "verification measurement schema differs")
        allowed = {"schema", "source", "commit_sha", "lock_digest", "state", "stage_seconds", "total_seconds", "cache_mode", "external_run_id"}
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise VerificationRaceError("verification_race_field_unknown", f"unsupported measurement fields: {', '.join(unknown)}")
        source = _text(value.get("source"), "source", 32).upper()
        if source not in {"LOCAL", "GITHUB_ACTIONS"}:
            raise VerificationRaceError("verification_race_source_invalid", "source must be LOCAL or GITHUB_ACTIONS")
        commit_sha = _text(value.get("commit_sha"), "commit_sha", 64).lower()
        if _GIT_RE.fullmatch(commit_sha) is None:
            raise VerificationRaceError("verification_race_commit_invalid", "commit_sha must be a Git object identity")
        state = _text(value.get("state"), "state", 32).upper()
        if state not in {"PASS", "FAIL", "QUEUED", "RUNNING", "UNAVAILABLE"}:
            raise VerificationRaceError("verification_race_state_invalid", "measurement state is unsupported")
        raw_stages = value.get("stage_seconds", {})
        if not isinstance(raw_stages, Mapping) or len(raw_stages) > len(_STAGES):
            raise VerificationRaceError("verification_race_stages_invalid", "stage_seconds must be a bounded object")
        stages: dict[str, float] = {}
        for raw_name, raw_seconds in raw_stages.items():
            name = _text(raw_name, "stage_seconds key", 64)
            if name not in _STAGES:
                raise VerificationRaceError("verification_race_stage_unknown", f"unsupported verification stage: {name}")
            if isinstance(raw_seconds, bool) or not isinstance(raw_seconds, (int, float)) or not math.isfinite(float(raw_seconds)) or float(raw_seconds) < 0 or float(raw_seconds) > 86_400:
                raise VerificationRaceError("verification_race_duration_invalid", f"stage duration is invalid: {name}")
            stages[name] = float(raw_seconds)
        total = value.get("total_seconds")
        if total is not None:
            if isinstance(total, bool) or not isinstance(total, (int, float)) or not math.isfinite(float(total)) or float(total) < 0 or float(total) > 86_400:
                raise VerificationRaceError("verification_race_duration_invalid", "total_seconds is invalid")
            total = float(total)
        lock = value.get("lock_digest")
        if lock is not None:
            lock = _text(lock, "lock_digest", 128).lower()
            if re.fullmatch(r"[0-9a-f]{64}", lock) is None:
                raise VerificationRaceError("verification_race_lock_invalid", "lock_digest must be SHA-256")
        cache_mode = _text(value.get("cache_mode", "UNKNOWN"), "cache_mode", 16).upper()
        if cache_mode not in {"COLD", "WARM", "UNKNOWN"}:
            raise VerificationRaceError("verification_race_cache_invalid", "cache_mode is unsupported")
        run_id = value.get("external_run_id")
        if run_id is not None:
            run_id = _text(run_id, "external_run_id", 128)
        return cls(source, commit_sha, state, stages, lock, total, cache_mode, run_id)

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema": self.schema,
            "source": self.source,
            "commit_sha": self.commit_sha,
            "state": self.state,
            "stage_seconds": dict(self.stage_seconds),
            "cache_mode": self.cache_mode,
        }
        if self.lock_digest is not None:
            value["lock_digest"] = self.lock_digest
        if self.total_seconds is not None:
            value["total_seconds"] = self.total_seconds
        if self.external_run_id is not None:
            value["external_run_id"] = self.external_run_id
        return value


def compare_verification(local: Mapping[str, Any] | VerificationMeasurement, actions: Mapping[str, Any] | VerificationMeasurement | None = None) -> dict[str, Any]:
    """Compare observations without inventing unavailable timings."""
    local_m = local if isinstance(local, VerificationMeasurement) else VerificationMeasurement.from_mapping(local)
    if local_m.source != "LOCAL":
        raise VerificationRaceError("verification_race_source_invalid", "local observation must have source LOCAL")
    actions_m = None if actions is None else actions if isinstance(actions, VerificationMeasurement) else VerificationMeasurement.from_mapping(actions)
    if actions_m is not None and actions_m.source != "GITHUB_ACTIONS":
        raise VerificationRaceError("verification_race_source_invalid", "Actions observation must have source GITHUB_ACTIONS")
    if actions_m is None:
        return {"schema": VERIFICATION_RACE_SCHEMA, "status": "ACTIONS_UNAVAILABLE", "commit_sha": local_m.commit_sha, "local": local_m.to_dict(), "actions": None, "winner": None, "speedup": None}
    if local_m.commit_sha != actions_m.commit_sha:
        raise VerificationRaceError("verification_race_commit_mismatch", "local and Actions observations use different commits")
    if local_m.lock_digest and actions_m.lock_digest and local_m.lock_digest != actions_m.lock_digest:
        raise VerificationRaceError("verification_race_lock_mismatch", "local and Actions lock identities differ")
    result: dict[str, Any] = {"schema": VERIFICATION_RACE_SCHEMA, "status": "INCOMPLETE", "commit_sha": local_m.commit_sha, "lock_digest": local_m.lock_digest or actions_m.lock_digest, "local": local_m.to_dict(), "actions": actions_m.to_dict(), "winner": None, "speedup": None, "stage_comparison": {}}
    for stage in sorted(set(local_m.stage_seconds) & set(actions_m.stage_seconds)):
        result["stage_comparison"][stage] = {"local_seconds": local_m.stage_seconds[stage], "actions_seconds": actions_m.stage_seconds[stage]}
    if local_m.state == "PASS" and actions_m.state == "PASS" and local_m.total_seconds is not None and actions_m.total_seconds is not None:
        result["status"] = "COMPLETE"
        result["winner"] = "LOCAL" if local_m.total_seconds < actions_m.total_seconds else "GITHUB_ACTIONS" if actions_m.total_seconds < local_m.total_seconds else "TIE"
        faster = min(local_m.total_seconds, actions_m.total_seconds)
        result["speedup"] = None if faster <= 0 else max(local_m.total_seconds, actions_m.total_seconds) / faster
    return result


__all__ = ["VERIFICATION_RACE_SCHEMA", "VerificationMeasurement", "VerificationRaceError", "compare_verification"]

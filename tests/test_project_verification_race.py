from __future__ import annotations

import pytest

from bdb_vnext.verification_race import VerificationMeasurement, VerificationRaceError, compare_verification


HEAD = "a" * 40
LOCK = "b" * 64


def _measurement(source: str, *, total: float | None = 10.0, state: str = "PASS", commit: str = HEAD, lock: str | None = LOCK) -> dict[str, object]:
    return {
        "schema": "bdb-verification-race-v1",
        "source": source,
        "commit_sha": commit,
        "lock_digest": lock,
        "state": state,
        "stage_seconds": {"typecheck": 2.0, "total": total or 0.0},
        "total_seconds": total,
        "cache_mode": "WARM",
    }


def test_verification_race_requires_same_commit_and_compares_only_complete_observations() -> None:
    result = compare_verification(_measurement("LOCAL", total=4.0), _measurement("GITHUB_ACTIONS", total=12.0))
    assert result["status"] == "COMPLETE"
    assert result["winner"] == "LOCAL"
    assert result["stage_comparison"]["typecheck"] == {"local_seconds": 2.0, "actions_seconds": 2.0}
    assert result["speedup"] == 3.0
    with pytest.raises(VerificationRaceError) as error:
        compare_verification(_measurement("LOCAL"), _measurement("GITHUB_ACTIONS", commit="c" * 40))
    assert error.value.code == "verification_race_commit_mismatch"


def test_verification_race_missing_actions_is_explicit_and_no_fake_winner() -> None:
    result = compare_verification(_measurement("LOCAL", total=4.0), None)
    assert result["status"] == "ACTIONS_UNAVAILABLE"
    assert result["winner"] is None
    assert result["speedup"] is None
    partial = compare_verification(_measurement("LOCAL", total=4.0), _measurement("GITHUB_ACTIONS", state="RUNNING", total=None))
    assert partial["status"] == "INCOMPLETE"
    assert partial["winner"] is None


def test_verification_measurement_rejects_unknown_fields_and_bad_lock() -> None:
    with pytest.raises(VerificationRaceError):
        VerificationMeasurement.from_mapping({**_measurement("LOCAL"), "unexpected": True})
    with pytest.raises(VerificationRaceError):
        VerificationMeasurement.from_mapping({**_measurement("LOCAL"), "lock_digest": "not-a-digest"})

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from bdb_vnext.bootstrap import BUNDLE_SCHEMA, HEALTH_SCHEMA, BootstrapError, BootstrapLock
from bdb_vnext.composition import RUNTIME_ID, observe_bundle
from bdb_vnext.m11a_bootstrap_slots import SlotSource, initialize_slot_authority, stage_candidate_slot
from bdb_vnext.m11a_prepared_activation import prepare_candidate_activation
from bdb_vnext.m11b_fault_matrix import (
    InjectedCrash,
    M11bFaultError,
    advance_fault_experiment,
    cold_recover_fault_experiment,
    initialize_fault_experiment,
    matrix_pass,
    query_fault_experiment,
)


COMMIT = "a" * 40
OTHER_COMMIT = "b" * 40
THIRD_COMMIT = "c" * 40
CAPS = ("canonical-admission-v1", "content-store-v1")


def _health_source(bundle_id: str) -> str:
    payload = {
        "schema": HEALTH_SCHEMA,
        "status": "READY",
        "runtime_id": RUNTIME_ID,
        "bundle_id": bundle_id,
    }
    return (
        "import json, sys\n"
        "schema = int(next(v.split('=', 1)[1] for v in sys.argv if v.startswith('--control-schema=')))\n"
        f"payload = {payload!r}\n"
        "payload['observed_control_schema'] = schema\n"
        "print(json.dumps(payload, sort_keys=True, separators=(',', ':')))\n"
    )


def _write_bundle(root: Path, *, role: str, known_good: bool, source_commit: str) -> None:
    root.mkdir(parents=True)
    bundle_id = f"m11b-{root.name}"
    (root / "health.py").write_text(_health_source(bundle_id), encoding="utf-8", newline="\n")
    manifest = {
        "schema": BUNDLE_SCHEMA,
        "runtime_id": RUNTIME_ID,
        "bundle_id": bundle_id,
        "role": role,
        "source_commit": source_commit,
        "supported_control_schema": {"min": 1, "max": 1},
        "known_good": known_good,
        "health_entrypoint": "health.py",
        "activation_policy": {"candidate_may_write_final_pointer": False},
    }
    (root / "bundle.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _digest(root: Path, legacy: Path) -> str:
    value = observe_bundle(RUNTIME_ID, root, legacy_runtime_root=legacy)["sha256"]
    assert isinstance(value, str)
    return value


def _fixture(tmp_path: Path):
    legacy = tmp_path / "legacy"
    authority = tmp_path / "authority"
    runtime = tmp_path / "runtime"
    recovery = tmp_path / "recovery"
    experiment = tmp_path / "experiment"
    active_root = tmp_path / "active"
    previous_root = tmp_path / "previous"
    candidate_root = tmp_path / "candidate"
    runtime.mkdir()
    _write_bundle(active_root, role="candidate", known_good=True, source_commit=COMMIT)
    _write_bundle(previous_root, role="recovery", known_good=True, source_commit=OTHER_COMMIT)
    _write_bundle(candidate_root, role="candidate", known_good=False, source_commit=THIRD_COMMIT)
    active = SlotSource("ACTIVE", active_root, _digest(active_root, legacy), "candidate", CAPS)
    previous = SlotSource("PREVIOUS", previous_root, _digest(previous_root, legacy), "recovery", CAPS)
    candidate = SlotSource("CANDIDATE", candidate_root, _digest(candidate_root, legacy), "candidate", CAPS)
    initialize_slot_authority(
        authority_root=authority,
        legacy_runtime_root=legacy,
        active=active,
        previous=previous,
        required_control_schema=1,
        required_capabilities=CAPS,
    )
    stage_candidate_slot(authority_root=authority, candidate=candidate)
    prepare_candidate_activation(
        authority_root=authority,
        runtime_root=runtime,
        recovery_target=recovery,
        preparation_id="prep-1",
        source_is_quiesced=True,
    )
    initialize_fault_experiment(
        authority_root=authority,
        preparation_id="prep-1",
        experiment_root=experiment,
        experiment_id="exp-1",
    )
    return legacy, authority, runtime, recovery, experiment, active_root, previous_root, candidate_root


def _probe(**states: bool):
    def probe(_experiment, slot):
        return states.get(str(slot["manifest_sha256"]), states.get(str(slot["bundle_id"]), True))
    return probe


def _named_probe(*, active: bool = True, previous: bool = True, candidate: bool = True):
    def probe(experiment, slot):
        for name in ("ACTIVE", "PREVIOUS", "CANDIDATE"):
            if experiment["slots"][name]["manifest_sha256"] == slot["manifest_sha256"]:
                return {"ACTIVE": active, "PREVIOUS": previous, "CANDIDATE": candidate}[name]
        raise AssertionError("unknown slot")
    return probe


def test_initialization_is_disposable_and_points_to_original_active(tmp_path: Path) -> None:
    *_rest, experiment, _active, _previous, _candidate = _fixture(tmp_path)
    query = query_fault_experiment(experiment_root=experiment)
    assert query["experiment"]["experiment_only"] is True
    assert query["experiment"]["production_activation_performed"] is False
    assert query["pointer"]["slot"] == "ACTIVE"
    assert [event["boundary"] for event in query["events"]] == ["INITIALIZED"]


def test_full_experiment_rolls_forward_to_known_good_candidate(tmp_path: Path) -> None:
    *_rest, experiment, _active, _previous, _candidate = _fixture(tmp_path)
    result = advance_fault_experiment(experiment_root=experiment, health_probe=_named_probe())
    assert result["outcome"] == "KNOWN_GOOD_CANDIDATE"
    query = query_fault_experiment(experiment_root=experiment)
    assert query["pointer"]["slot"] == "CANDIDATE"
    assert [event["boundary"] for event in query["events"]] == [
        "INITIALIZED", "SWITCH_INTENT", "START_REQUESTED", "HEALTH_VERIFIED", "CONCLUDED"
    ]


@pytest.mark.parametrize(
    "boundary,expected",
    [
        ("INITIALIZED", "KNOWN_GOOD_ACTIVE"),
        ("SWITCH_INTENT", "KNOWN_GOOD_ACTIVE"),
        ("POINTER_PUBLISHED", "RECOVERED_PREVIOUS"),
        ("START_REQUESTED", "KNOWN_GOOD_CANDIDATE"),
        ("HEALTH_VERIFIED", "KNOWN_GOOD_CANDIDATE"),
        ("CONCLUDED", "KNOWN_GOOD_CANDIDATE"),
    ],
)
def test_crash_after_each_durable_boundary_has_deterministic_recovery(
    tmp_path: Path, boundary: str, expected: str
) -> None:
    *_rest, experiment, _active, _previous, _candidate = _fixture(tmp_path)
    with pytest.raises(InjectedCrash):
        advance_fault_experiment(
            experiment_root=experiment,
            crash_after=boundary,  # type: ignore[arg-type]
            health_probe=_named_probe(),
        )
    recovered = cold_recover_fault_experiment(
        experiment_root=experiment,
        health_probe=_named_probe(),
    )
    assert recovered["outcome"] == expected


def test_start_failure_recovers_previous(tmp_path: Path) -> None:
    *_rest, experiment, _active, _previous, _candidate = _fixture(tmp_path)
    result = advance_fault_experiment(
        experiment_root=experiment,
        start_success=False,
        health_probe=_named_probe(),
    )
    assert result["outcome"] == "RECOVERED_PREVIOUS"
    assert query_fault_experiment(experiment_root=experiment)["pointer"]["slot"] == "PREVIOUS"


def test_candidate_health_failure_recovers_previous(tmp_path: Path) -> None:
    *_rest, experiment, _active, _previous, _candidate = _fixture(tmp_path)
    result = advance_fault_experiment(
        experiment_root=experiment,
        health_probe=_named_probe(candidate=False),
    )
    assert result["outcome"] == "RECOVERED_PREVIOUS"


def test_candidate_and_previous_failure_is_quarantined(tmp_path: Path) -> None:
    *_rest, experiment, _active, _previous, _candidate = _fixture(tmp_path)
    result = advance_fault_experiment(
        experiment_root=experiment,
        health_probe=_named_probe(candidate=False, previous=False),
    )
    assert result["outcome"] == "BLOCKED_QUARANTINED"


def test_false_positive_old_health_is_not_trusted_on_cold_restart(tmp_path: Path) -> None:
    *_rest, experiment, _active, _previous, _candidate = _fixture(tmp_path)
    first = advance_fault_experiment(experiment_root=experiment, health_probe=_named_probe())
    assert first["outcome"] == "KNOWN_GOOD_CANDIDATE"
    recovered = cold_recover_fault_experiment(
        experiment_root=experiment,
        health_probe=_named_probe(candidate=False, previous=True),
        reason="fresh_health_disagrees",
    )
    assert recovered["outcome"] == "RECOVERED_PREVIOUS"


def test_invalid_final_pointer_is_deterministically_quarantined(tmp_path: Path) -> None:
    *_rest, experiment, _active, _previous, _candidate = _fixture(tmp_path)
    (experiment / "active-pointer.json").write_bytes(b'{"torn":')
    recovered = cold_recover_fault_experiment(experiment_root=experiment, health_probe=_named_probe())
    assert recovered["outcome"] == "BLOCKED_QUARANTINED"


def test_foreign_partial_pointer_staging_is_ignored(tmp_path: Path) -> None:
    *_rest, experiment, _active, _previous, _candidate = _fixture(tmp_path)
    (experiment / ".active-pointer.json.partial-foreign").write_bytes(b'{"torn":')
    recovered = cold_recover_fault_experiment(experiment_root=experiment, health_probe=_named_probe())
    assert recovered["outcome"] == "KNOWN_GOOD_ACTIVE"


def test_pointer_publication_failure_leaves_old_known_good_pointer(tmp_path: Path) -> None:
    *_rest, experiment, _active, _previous, _candidate = _fixture(tmp_path)

    def deny(path: Path, _document) -> None:
        if path.name == "active-pointer.json":
            raise OSError("simulated AV lock")

    with pytest.raises(OSError):
        advance_fault_experiment(
            experiment_root=experiment,
            health_probe=_named_probe(),
            pointer_write_hook=deny,
        )
    recovered = cold_recover_fault_experiment(experiment_root=experiment, health_probe=_named_probe())
    assert recovered["outcome"] == "KNOWN_GOOD_ACTIVE"


def test_unhealthy_original_active_recovers_previous(tmp_path: Path) -> None:
    *_rest, experiment, _active, _previous, _candidate = _fixture(tmp_path)
    recovered = cold_recover_fault_experiment(
        experiment_root=experiment,
        health_probe=_named_probe(active=False, previous=True),
    )
    assert recovered["outcome"] == "RECOVERED_PREVIOUS"


def test_no_healthy_active_or_previous_is_quarantined(tmp_path: Path) -> None:
    *_rest, experiment, _active, _previous, _candidate = _fixture(tmp_path)
    recovered = cold_recover_fault_experiment(
        experiment_root=experiment,
        health_probe=_named_probe(active=False, previous=False),
    )
    assert recovered["outcome"] == "BLOCKED_QUARANTINED"


def test_preparation_drift_blocks_experiment_before_switch(tmp_path: Path) -> None:
    _legacy, _authority, _runtime, _recovery, experiment, _active, _previous, candidate = _fixture(tmp_path)
    (candidate / "health.py").write_text("raise SystemExit(7)\n", encoding="utf-8", newline="\n")
    with pytest.raises(BootstrapError):
        advance_fault_experiment(experiment_root=experiment, health_probe=_named_probe())
    assert query_fault_experiment(experiment_root=experiment)["pointer"]["slot"] == "ACTIVE"


def test_concurrent_experiment_writer_is_blocked(tmp_path: Path) -> None:
    *_rest, experiment, _active, _previous, _candidate = _fixture(tmp_path)
    with BootstrapLock(experiment / "experiment.lock"):
        with pytest.raises(BootstrapError) as caught:
            advance_fault_experiment(experiment_root=experiment, health_probe=_named_probe())
    assert caught.value.code == "concurrent_attempt"


def test_real_process_hard_crash_after_pointer_is_recovered_by_new_process_boundary(tmp_path: Path) -> None:
    *_rest, experiment, _active, _previous, _candidate = _fixture(tmp_path)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "bdb_vnext.m11b_fault_matrix",
            "run",
            "--experiment-root",
            str(experiment),
            "--crash-after",
            "POINTER_PUBLISHED",
            "--hard-crash",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 91
    recovered = cold_recover_fault_experiment(experiment_root=experiment)
    assert recovered["outcome"] == "RECOVERED_PREVIOUS"


def test_matrix_pass_rejects_unknown_or_empty_outcomes() -> None:
    assert matrix_pass([{"outcome": "KNOWN_GOOD_ACTIVE"}, {"outcome": "BLOCKED_QUARANTINED"}])["status"] == "PASS"
    assert matrix_pass([])["status"] == "FAIL"
    assert matrix_pass([{"outcome": "AMBIGUOUS"}])["status"] == "FAIL"

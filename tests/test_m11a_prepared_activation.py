from __future__ import annotations

import json
from pathlib import Path

import pytest

import bdb_vnext.m11a_prepared_activation as prepared
from bdb_vnext.bootstrap import BUNDLE_SCHEMA, HEALTH_SCHEMA, BootstrapError
from bdb_vnext.composition import RUNTIME_ID, observe_bundle
from bdb_vnext.m11a_bootstrap_slots import (
    SlotSource,
    discard_candidate_slot,
    initialize_slot_authority,
    stage_candidate_slot,
)
from bdb_vnext.m11a_prepared_activation import (
    prepare_candidate_activation,
    query_prepared_activation,
)


COMMIT = "a" * 40
OTHER_COMMIT = "b" * 40
THIRD_COMMIT = "c" * 40
CAPS = ("canonical-admission-v1", "content-store-v1")


def _health_source(bundle_id: str, *, ready: bool = True) -> str:
    if not ready:
        return "raise SystemExit(9)\n"
    payload = {
        "schema": HEALTH_SCHEMA,
        "status": "READY",
        "runtime_id": RUNTIME_ID,
        "bundle_id": bundle_id,
    }
    return (
        "import json, sys\n"
        "schema = int(next(value.split('=', 1)[1] for value in sys.argv if value.startswith('--control-schema=')))\n"
        f"payload = {payload!r}\n"
        "payload['observed_control_schema'] = schema\n"
        "print(json.dumps(payload, sort_keys=True, separators=(',', ':')))\n"
    )


def _write_bundle(
    root: Path,
    *,
    role: str,
    known_good: bool,
    source_commit: str,
    health_ready: bool = True,
) -> None:
    root.mkdir(parents=True)
    bundle_id = f"m11a-{root.name}"
    (root / "health.py").write_text(
        _health_source(bundle_id, ready=health_ready), encoding="utf-8", newline="\n"
    )
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


def _prepared_fixture(tmp_path: Path, *, previous_health_ready: bool = True):
    legacy = tmp_path / "legacy"
    authority = tmp_path / "authority"
    runtime = tmp_path / "runtime"
    recovery_target = tmp_path / "recovery-target"
    active_root = tmp_path / "active"
    previous_root = tmp_path / "previous"
    candidate_root = tmp_path / "candidate"
    runtime.mkdir()
    _write_bundle(active_root, role="candidate", known_good=True, source_commit=COMMIT)
    _write_bundle(
        previous_root,
        role="recovery",
        known_good=True,
        source_commit=OTHER_COMMIT,
        health_ready=previous_health_ready,
    )
    _write_bundle(candidate_root, role="candidate", known_good=False, source_commit=THIRD_COMMIT)
    active = SlotSource("ACTIVE", active_root, _digest(active_root, legacy), "candidate", CAPS)
    previous = SlotSource("PREVIOUS", previous_root, _digest(previous_root, legacy), "recovery", CAPS)
    candidate = SlotSource("CANDIDATE", candidate_root, _digest(candidate_root, legacy), "candidate", CAPS)
    initialized = initialize_slot_authority(
        authority_root=authority,
        legacy_runtime_root=legacy,
        active=active,
        previous=previous,
        required_control_schema=1,
        required_capabilities=CAPS,
    )
    staged = stage_candidate_slot(authority_root=authority, candidate=candidate)
    return legacy, authority, runtime, recovery_target, active, previous, candidate, initialized, staged


def test_prepare_binds_exact_slots_backup_and_previous_health_without_activation(tmp_path: Path) -> None:
    _legacy, authority, runtime, recovery_target, _active, _previous, _candidate, _initialized, staged = _prepared_fixture(tmp_path)
    active_before = staged["state"]["active_manifest_sha256"]

    result = prepare_candidate_activation(
        authority_root=authority,
        runtime_root=runtime,
        recovery_target=recovery_target,
        preparation_id="prep-1",
        source_is_quiesced=True,
    )

    document = result["prepared"]
    assert document["slot_binding"] == {
        "slot_state_sha256": staged["state"]["state_sha256"],
        "active_manifest_sha256": staged["state"]["active_manifest_sha256"],
        "previous_manifest_sha256": staged["state"]["previous_manifest_sha256"],
        "candidate_manifest_sha256": staged["state"]["candidate_manifest_sha256"],
    }
    assert result["slots"]["state"]["active_manifest_sha256"] == active_before
    assert result["backup_verified"] is True
    assert Path(document["backup"]["path"]).is_dir()
    assert document["recovery"]["previous_health"]["status"] == "READY"
    assert document["recovery"]["target_root"] == str(recovery_target.absolute())
    assert document["candidate_may_write_active_pointer"] is False
    assert document["production_activation_performed"] is False
    assert result["actions"] == {"activate_candidate": False, "activation_deferred_to": "M11c"}
    assert not hasattr(prepared, "activate_candidate")
    assert not hasattr(prepared, "activate_candidate_slot")


def test_prepare_requires_staged_candidate_and_writes_no_record_on_failure(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    authority = tmp_path / "authority"
    runtime = tmp_path / "runtime"
    recovery_target = tmp_path / "recovery"
    active_root = tmp_path / "active"
    previous_root = tmp_path / "previous"
    runtime.mkdir()
    _write_bundle(active_root, role="candidate", known_good=True, source_commit=COMMIT)
    _write_bundle(previous_root, role="recovery", known_good=True, source_commit=OTHER_COMMIT)
    initialize_slot_authority(
        authority_root=authority,
        legacy_runtime_root=legacy,
        active=SlotSource("ACTIVE", active_root, _digest(active_root, legacy), "candidate", CAPS),
        previous=SlotSource("PREVIOUS", previous_root, _digest(previous_root, legacy), "recovery", CAPS),
        required_control_schema=1,
        required_capabilities=CAPS,
    )

    with pytest.raises(BootstrapError) as raised:
        prepare_candidate_activation(
            authority_root=authority,
            runtime_root=runtime,
            recovery_target=recovery_target,
            preparation_id="prep-no-candidate",
            source_is_quiesced=True,
        )

    assert raised.value.code == "candidate_required"
    assert not (authority / "prepared-activations" / "prep-no-candidate.json").exists()


def test_prepare_requires_previous_recovery_slot(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    authority = tmp_path / "authority"
    runtime = tmp_path / "runtime"
    recovery_target = tmp_path / "recovery"
    active_root = tmp_path / "active"
    candidate_root = tmp_path / "candidate"
    runtime.mkdir()
    _write_bundle(active_root, role="candidate", known_good=True, source_commit=COMMIT)
    _write_bundle(candidate_root, role="candidate", known_good=False, source_commit=THIRD_COMMIT)
    initialize_slot_authority(
        authority_root=authority,
        legacy_runtime_root=legacy,
        active=SlotSource("ACTIVE", active_root, _digest(active_root, legacy), "candidate", CAPS),
        required_control_schema=1,
        required_capabilities=CAPS,
    )
    stage_candidate_slot(
        authority_root=authority,
        candidate=SlotSource("CANDIDATE", candidate_root, _digest(candidate_root, legacy), "candidate", CAPS),
    )

    with pytest.raises(BootstrapError) as raised:
        prepare_candidate_activation(
            authority_root=authority,
            runtime_root=runtime,
            recovery_target=recovery_target,
            preparation_id="prep-no-previous",
            source_is_quiesced=True,
        )

    assert raised.value.code == "previous_required"


def test_previous_health_failure_blocks_before_backup_or_prepared_record(tmp_path: Path) -> None:
    _legacy, authority, runtime, recovery_target, _active, _previous, _candidate, _initialized, _staged = _prepared_fixture(
        tmp_path, previous_health_ready=False
    )

    with pytest.raises(BootstrapError) as raised:
        prepare_candidate_activation(
            authority_root=authority,
            runtime_root=runtime,
            recovery_target=recovery_target,
            preparation_id="prep-bad-health",
            source_is_quiesced=True,
        )

    assert raised.value.code == "health_failed"
    assert not (authority / "prepared-backups" / "prep-bad-health").exists()
    assert not (authority / "prepared-activations" / "prep-bad-health.json").exists()


def test_non_quiesced_runtime_blocks_and_leaves_active_unchanged(tmp_path: Path) -> None:
    _legacy, authority, runtime, recovery_target, _active, _previous, _candidate, _initialized, staged = _prepared_fixture(tmp_path)
    active_before = staged["state"]["active_manifest_sha256"]

    with pytest.raises(BootstrapError) as raised:
        prepare_candidate_activation(
            authority_root=authority,
            runtime_root=runtime,
            recovery_target=recovery_target,
            preparation_id="prep-moving-runtime",
            source_is_quiesced=False,
        )

    assert raised.value.code == "source_not_quiesced"
    from bdb_vnext.m11a_bootstrap_slots import query_slot_authority

    assert query_slot_authority(authority_root=authority)["state"]["active_manifest_sha256"] == active_before


def test_prepared_query_fails_closed_when_candidate_bytes_move(tmp_path: Path) -> None:
    _legacy, authority, runtime, recovery_target, _active, _previous, candidate, _initialized, _staged = _prepared_fixture(tmp_path)
    prepare_candidate_activation(
        authority_root=authority,
        runtime_root=runtime,
        recovery_target=recovery_target,
        preparation_id="prep-moving-candidate",
        source_is_quiesced=True,
    )
    (candidate.bundle_root / "health.py").write_text("raise SystemExit(7)\n", encoding="utf-8", newline="\n")

    with pytest.raises(BootstrapError) as raised:
        query_prepared_activation(authority_root=authority, preparation_id="prep-moving-candidate")

    assert raised.value.code == "bundle_digest_mismatch"


def test_prepared_query_fails_closed_when_slot_state_changes(tmp_path: Path) -> None:
    _legacy, authority, runtime, recovery_target, _active, _previous, _candidate, _initialized, _staged = _prepared_fixture(tmp_path)
    prepare_candidate_activation(
        authority_root=authority,
        runtime_root=runtime,
        recovery_target=recovery_target,
        preparation_id="prep-stale-state",
        source_is_quiesced=True,
    )
    discard_candidate_slot(authority_root=authority)

    with pytest.raises(BootstrapError) as raised:
        query_prepared_activation(authority_root=authority, preparation_id="prep-stale-state")

    assert raised.value.code == "candidate_required"


def test_prepared_query_detects_backup_tamper(tmp_path: Path) -> None:
    _legacy, authority, runtime, recovery_target, _active, _previous, _candidate, _initialized, _staged = _prepared_fixture(tmp_path)
    result = prepare_candidate_activation(
        authority_root=authority,
        runtime_root=runtime,
        recovery_target=recovery_target,
        preparation_id="prep-backup-tamper",
        source_is_quiesced=True,
    )
    backup_path = Path(result["prepared"]["backup"]["path"])
    (backup_path / "foreign.bin").write_bytes(b"tamper")

    with pytest.raises(BootstrapError) as raised:
        query_prepared_activation(authority_root=authority, preparation_id="prep-backup-tamper")

    assert raised.value.code == "backup_integrity_failure"

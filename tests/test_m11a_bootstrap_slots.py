from __future__ import annotations

import json
from pathlib import Path

import pytest

import bdb_vnext.m11a_bootstrap_slots as slots
from bdb_vnext.bootstrap import BUNDLE_SCHEMA, BootstrapError
from bdb_vnext.composition import RUNTIME_ID, observe_bundle
from bdb_vnext.m11a_bootstrap_slots import (
    SlotSource,
    discard_candidate_slot,
    initialize_slot_authority,
    query_slot_authority,
    stage_candidate_slot,
)


COMMIT = "a" * 40
OTHER_COMMIT = "b" * 40
THIRD_COMMIT = "c" * 40
CAPS = ("canonical-admission-v1", "content-store-v1")


def _write_bundle(
    root: Path,
    *,
    role: str,
    known_good: bool,
    source_commit: str,
    schema_min: int = 1,
    schema_max: int = 1,
    activation_allowed: bool = False,
) -> None:
    root.mkdir(parents=True)
    bundle_id = f"m11a-{root.name}"
    (root / "health.py").write_text("raise SystemExit(0)\n", encoding="utf-8", newline="\n")
    manifest = {
        "schema": BUNDLE_SCHEMA,
        "runtime_id": RUNTIME_ID,
        "bundle_id": bundle_id,
        "role": role,
        "source_commit": source_commit,
        "supported_control_schema": {"min": schema_min, "max": schema_max},
        "known_good": known_good,
        "health_entrypoint": "health.py",
        "activation_policy": {
            "candidate_may_write_final_pointer": activation_allowed,
        },
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


def _sources(tmp_path: Path) -> tuple[Path, Path, SlotSource, SlotSource, SlotSource]:
    legacy = tmp_path / "legacy"
    authority = tmp_path / "authority"
    active_root = tmp_path / "active"
    previous_root = tmp_path / "previous"
    candidate_root = tmp_path / "candidate"
    _write_bundle(active_root, role="candidate", known_good=True, source_commit=COMMIT)
    _write_bundle(previous_root, role="recovery", known_good=True, source_commit=OTHER_COMMIT)
    _write_bundle(candidate_root, role="candidate", known_good=False, source_commit=THIRD_COMMIT)
    active = SlotSource("ACTIVE", active_root, _digest(active_root, legacy), "candidate", CAPS)
    previous = SlotSource("PREVIOUS", previous_root, _digest(previous_root, legacy), "recovery", CAPS)
    candidate = SlotSource("CANDIDATE", candidate_root, _digest(candidate_root, legacy), "candidate", CAPS)
    return legacy, authority, active, previous, candidate


def _initialize(tmp_path: Path):
    legacy, authority, active, previous, candidate = _sources(tmp_path)
    query = initialize_slot_authority(
        authority_root=authority,
        legacy_runtime_root=legacy,
        active=active,
        previous=previous,
        required_control_schema=1,
        required_capabilities=CAPS,
    )
    return legacy, authority, active, previous, candidate, query


def test_initialize_external_slots_has_exact_single_active_and_no_activation_action(tmp_path: Path) -> None:
    _legacy, authority, _active, _previous, _candidate, query = _initialize(tmp_path)

    state = query["state"]
    assert state["active_manifest_sha256"].startswith("sha256:")
    assert state["previous_manifest_sha256"].startswith("sha256:")
    assert state["candidate_manifest_sha256"] is None
    assert query["slots"]["ACTIVE"]["slot"] == "ACTIVE"
    assert query["slots"]["PREVIOUS"]["slot"] == "PREVIOUS"
    assert query["slots"]["CANDIDATE"] is None
    assert query["authority"] == {
        "boundary": "external_bootstrap_root",
        "candidate_may_write_active_pointer": False,
        "production_activation_performed": False,
        "control_db_consulted": False,
    }
    assert query["actions"]["activate_candidate"] is False
    assert query["actions"]["activation_deferred_to"] == "M11c"
    assert not hasattr(slots, "activate_candidate_slot")
    assert (authority / "slot-state.json").is_file()
    assert len(list((authority / "slot-manifests").glob("*.json"))) == 2


def test_stage_candidate_preserves_active_pointer_and_binds_exact_compatibility(tmp_path: Path) -> None:
    _legacy, authority, _active, _previous, candidate, before = _initialize(tmp_path)
    active_before = before["state"]["active_manifest_sha256"]

    after = stage_candidate_slot(authority_root=authority, candidate=candidate)

    assert after["state"]["active_manifest_sha256"] == active_before
    assert after["state"]["candidate_manifest_sha256"].startswith("sha256:")
    assert after["slots"]["CANDIDATE"]["bundle_sha256"] == candidate.expected_sha256
    assert after["slots"]["CANDIDATE"]["compatibility"]["capabilities"] == sorted(CAPS)
    assert after["actions"]["stage_candidate"] is False
    assert after["actions"]["discard_candidate"] is True
    assert after["actions"]["activate_candidate"] is False


def test_incompatible_candidate_is_blocked_without_changing_external_state(tmp_path: Path) -> None:
    legacy, authority, _active, _previous, _candidate, before = _initialize(tmp_path)
    incompatible_root = tmp_path / "candidate-v2-only"
    _write_bundle(
        incompatible_root,
        role="candidate",
        known_good=False,
        source_commit=THIRD_COMMIT,
        schema_min=2,
        schema_max=3,
    )
    incompatible = SlotSource(
        "CANDIDATE",
        incompatible_root,
        _digest(incompatible_root, legacy),
        "candidate",
        CAPS,
    )

    with pytest.raises(BootstrapError) as raised:
        stage_candidate_slot(authority_root=authority, candidate=incompatible)

    assert raised.value.code == "candidate_incompatible"
    after = query_slot_authority(authority_root=authority)
    assert after["state"]["active_manifest_sha256"] == before["state"]["active_manifest_sha256"]
    assert after["state"]["candidate_manifest_sha256"] is None


def test_candidate_requesting_self_activation_is_rejected_by_m1b_bundle_contract(tmp_path: Path) -> None:
    legacy, authority, _active, _previous, _candidate, before = _initialize(tmp_path)
    hostile_root = tmp_path / "hostile-candidate"
    _write_bundle(
        hostile_root,
        role="candidate",
        known_good=False,
        source_commit=THIRD_COMMIT,
        activation_allowed=True,
    )
    hostile = SlotSource(
        "CANDIDATE",
        hostile_root,
        _digest(hostile_root, legacy),
        "candidate",
        CAPS,
    )

    with pytest.raises(BootstrapError) as raised:
        stage_candidate_slot(authority_root=authority, candidate=hostile)

    assert raised.value.code == "candidate_self_activation_requested"
    after = query_slot_authority(authority_root=authority)
    assert after["state"]["active_manifest_sha256"] == before["state"]["active_manifest_sha256"]
    assert after["slots"]["CANDIDATE"] is None


def test_query_fails_closed_when_staged_bundle_bytes_move(tmp_path: Path) -> None:
    _legacy, authority, _active, _previous, candidate, _before = _initialize(tmp_path)
    stage_candidate_slot(authority_root=authority, candidate=candidate)
    (candidate.bundle_root / "health.py").write_text(
        "raise SystemExit(9)\n", encoding="utf-8", newline="\n"
    )

    with pytest.raises(BootstrapError) as raised:
        query_slot_authority(authority_root=authority)

    assert raised.value.code == "bundle_digest_mismatch"


def test_discard_candidate_keeps_active_and_retains_immutable_manifest_evidence(tmp_path: Path) -> None:
    _legacy, authority, _active, _previous, candidate, before = _initialize(tmp_path)
    staged = stage_candidate_slot(authority_root=authority, candidate=candidate)
    candidate_digest = staged["state"]["candidate_manifest_sha256"]
    candidate_manifest = authority / "slot-manifests" / f"{candidate_digest[7:]}.json"
    assert candidate_manifest.is_file()

    after = discard_candidate_slot(authority_root=authority)

    assert after["state"]["active_manifest_sha256"] == before["state"]["active_manifest_sha256"]
    assert after["state"]["candidate_manifest_sha256"] is None
    assert candidate_manifest.is_file()
    assert after["actions"]["activate_candidate"] is False


def test_second_candidate_stage_is_rejected_without_replacing_first(tmp_path: Path) -> None:
    legacy, authority, _active, _previous, candidate, _before = _initialize(tmp_path)
    first = stage_candidate_slot(authority_root=authority, candidate=candidate)
    first_digest = first["state"]["candidate_manifest_sha256"]
    other_root = tmp_path / "candidate-other"
    _write_bundle(other_root, role="candidate", known_good=False, source_commit="d" * 40)
    other = SlotSource("CANDIDATE", other_root, _digest(other_root, legacy), "candidate", CAPS)

    with pytest.raises(BootstrapError) as raised:
        stage_candidate_slot(authority_root=authority, candidate=other)

    assert raised.value.code == "candidate_already_staged"
    assert query_slot_authority(authority_root=authority)["state"]["candidate_manifest_sha256"] == first_digest

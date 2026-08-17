from __future__ import annotations

import json
from pathlib import Path

import pytest

import bdb_vnext.m11c_cutover as m11c
import bdb_vnext.m9b_activation as m9b
from bdb_vnext.bootstrap import BUNDLE_SCHEMA, HEALTH_SCHEMA
from bdb_vnext.composition import BROWSER_EXTENSION_ID, RUNTIME_ID, observe_bundle
from bdb_vnext.m11a_bootstrap_slots import (
    SlotSource,
    initialize_slot_authority,
    query_slot_authority,
    stage_candidate_slot,
)
from bdb_vnext.m11a_prepared_activation import prepare_candidate_activation
from bdb_vnext.m11a_windows_tcb import (
    ADMINISTRATORS_SID,
    SYSTEM_SID,
    USERS_SID,
    WINDOWS_ACL_WITNESS_SCHEMA,
    build_windows_tcb_witness,
    default_windows_authority_root,
)
from bdb_vnext.m11c_windows_clients import record_browser_launch_verification, stage_client_plan
from bdb_vnext.m3c_admission import CanonicalVNextAdmissionAuthority
from bdb_vnext.m9b_activation import read_activation, record_clients_verified


ROOT = Path(__file__).resolve().parents[1]
BROWSER_SOURCE = ROOT / "browser_extension_vnext"
ORIGIN = f"chrome-extension://{BROWSER_EXTENSION_ID}/"
HEAD = "a" * 40
TREE = "b" * 40
OLD_HEAD = "c" * 40
PREVIOUS_HEAD = "d" * 40
CAPS = ("canonical-admission-v1", "content-store-v1")
FREEZE_DIGEST = "sha256:" + "3" * 64


def _health_source(bundle_id: str, *, ready: bool = True) -> str:
    if not ready:
        return "raise SystemExit(9)\n"
    payload = {"schema": HEALTH_SCHEMA, "status": "READY", "runtime_id": RUNTIME_ID, "bundle_id": bundle_id}
    return (
        "import json, sys\n"
        "schema = int(next(value.split('=', 1)[1] for value in sys.argv if value.startswith('--control-schema=')))\n"
        f"payload = {payload!r}\n"
        "payload['observed_control_schema'] = schema\n"
        "print(json.dumps(payload, sort_keys=True, separators=(',', ':')))\n"
    )


def _write_bundle(root: Path, *, role: str, known_good: bool, source_commit: str, health_ready: bool = True) -> None:
    root.mkdir(parents=True)
    bundle_id = f"m11c-{root.name}"
    (root / "health.py").write_text(_health_source(bundle_id, ready=health_ready), encoding="utf-8", newline="\n")
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
    (root / "bundle.json").write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8", newline="\n")


def _bundle_digest(root: Path, legacy: Path) -> str:
    value = observe_bundle(RUNTIME_ID, root, legacy_runtime_root=legacy)["sha256"]
    assert isinstance(value, str)
    return value


def _safe_acl() -> dict[str, object]:
    return {
        "schema": WINDOWS_ACL_WITNESS_SCHEMA,
        "owner_sid": ADMINISTRATORS_SID,
        "inheritance_protected": True,
        "entries": [
            {"sid": SYSTEM_SID, "type": "Allow", "rights": ["FullControl"], "inherited": False},
            {"sid": ADMINISTRATORS_SID, "type": "Allow", "rights": ["FullControl"], "inherited": False},
            {"sid": USERS_SID, "type": "Allow", "rights": ["ReadAndExecute", "Synchronize"], "inherited": False},
        ],
    }


def _m9a_report() -> dict[str, object]:
    return {
        "schema": "bdb-vnext-m9a-freeze-report-v1",
        "status": "PASS_CLOSED",
        "legacy_ingress_frozen": True,
        "legacy_writer_frozen": True,
        "archive_created": True,
        "zero_new_write_observed": True,
        "vnext_activation_allowed": False,
        "m9b_allowed": False,
        "partial_freeze_requires_roll_forward": False,
        "freeze_digest": FREEZE_DIGEST,
    }


def _fixture(tmp_path: Path, *, candidate_known_good: bool = True):
    program_data = tmp_path / "ProgramData"
    authority = default_windows_authority_root(program_data)
    runtime = tmp_path / "runtime"
    legacy = tmp_path / "legacy"
    recovery_target = tmp_path / "recovery-target"
    active_root = tmp_path / "active"
    previous_root = tmp_path / "previous"
    candidate_root = tmp_path / "candidate"
    native_executable = tmp_path / "Scripts" / "bdb-vnext-native-host.exe"
    runtime.mkdir()
    native_executable.parent.mkdir(parents=True)
    native_executable.write_bytes(b"m11c-fixture-native-host")

    client_plan = stage_client_plan(
        runtime_root=runtime,
        legacy_runtime_root=legacy,
        bootstrap_authority_root=authority,
        browser_source_root=BROWSER_SOURCE,
        native_host_executable=native_executable,
        source_head=HEAD,
        source_tree=TREE,
    )["plan"]
    client_verification = record_browser_launch_verification(runtime_root=runtime, caller_origin=ORIGIN)

    _write_bundle(active_root, role="candidate", known_good=True, source_commit=OLD_HEAD)
    _write_bundle(previous_root, role="recovery", known_good=True, source_commit=PREVIOUS_HEAD)
    _write_bundle(candidate_root, role="candidate", known_good=candidate_known_good, source_commit=HEAD)
    active = SlotSource("ACTIVE", active_root, _bundle_digest(active_root, legacy), "candidate", CAPS)
    previous = SlotSource("PREVIOUS", previous_root, _bundle_digest(previous_root, legacy), "recovery", CAPS)
    candidate = SlotSource("CANDIDATE", candidate_root, _bundle_digest(candidate_root, legacy), "candidate", CAPS)
    initialize_slot_authority(
        authority_root=authority,
        legacy_runtime_root=legacy,
        active=active,
        previous=previous,
        required_control_schema=1,
        required_capabilities=CAPS,
    )
    staged = stage_candidate_slot(authority_root=authority, candidate=candidate)
    prepared = prepare_candidate_activation(
        authority_root=authority,
        runtime_root=runtime,
        recovery_target=recovery_target,
        preparation_id="prep-final",
        source_is_quiesced=True,
    )
    witness = build_windows_tcb_witness(
        authority_root=authority,
        program_data=program_data,
        runtime_root=runtime,
        legacy_runtime_root=legacy,
        mutable_roots=(candidate_root,),
        acl_witness=_safe_acl(),
    )
    return {
        "program_data": program_data,
        "authority": authority,
        "runtime": runtime,
        "legacy": legacy,
        "candidate_root": candidate_root,
        "active_root": active_root,
        "staged": staged,
        "prepared": prepared,
        "witness": witness,
        "client_plan": client_plan,
        "client_verification": client_verification,
    }


def _plan(fixture: dict[str, object]) -> dict[str, object]:
    client_plan = fixture["client_plan"]
    result = m11c.prepare_cutover_plan(
        authority_root=fixture["authority"],
        runtime_root=fixture["runtime"],
        legacy_runtime_root=fixture["legacy"],
        preparation_id="prep-final",
        cutover_id="final-1",
        source_head=HEAD,
        source_tree=TREE,
        m9a_report=_m9a_report(),
        browser_bundle_digest=client_plan["browser_bundle_digest"],  # type: ignore[index]
        native_manifest_digest=client_plan["native_manifest_sha256"],  # type: ignore[index]
        tcb_witness=fixture["witness"],
    )
    return result["plan"]


def test_cutover_plan_is_exact_and_does_not_activate(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    active_before = fixture["staged"]["state"]["active_manifest_sha256"]  # type: ignore[index]
    plan = _plan(fixture)
    observed = m11c.observe_bootstrap_activation(authority_root=fixture["authority"])
    assert observed["status"] == "PREPARED"
    assert observed["production_activation_performed"] is False
    assert observed["state"]["active_manifest_sha256"] == active_before
    assert read_activation(fixture["runtime"]) is None
    assert plan["candidate_source_commit"] == HEAD
    assert plan["client_plan_sha256"] == fixture["client_plan"]["client_plan_sha256"]  # type: ignore[index]
    assert plan["operator_approval_required"] is True
    assert plan["candidate_may_write_active_pointer"] is False
    assert plan["production_activation_performed"] is False


def test_plan_rejects_candidate_not_explicitly_certified(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, candidate_known_good=False)
    with pytest.raises(m11c.M11cCutoverError) as caught:
        _plan(fixture)
    assert caught.value.code == "candidate_not_certified"
    assert m11c.observe_bootstrap_activation(authority_root=fixture["authority"])["status"] == "PREPARED"


def test_apply_requires_exact_plan_and_explicit_operator_approval(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    plan = _plan(fixture)
    with pytest.raises(m11c.M11cCutoverError) as not_approved:
        m11c._apply_cutover(
            authority_root=fixture["authority"], cutover_id="final-1", expected_plan_sha256=plan["cutover_plan_sha256"], operator_approved=False, tcb_witness=fixture["witness"]
        )
    assert not_approved.value.code == "operator_approval_required"
    with pytest.raises(m11c.M11cCutoverError) as stale:
        m11c._apply_cutover(
            authority_root=fixture["authority"], cutover_id="final-1", expected_plan_sha256="sha256:" + "f" * 64, operator_approved=True, tcb_witness=fixture["witness"]
        )
    assert stale.value.code == "cutover_plan_stale"
    assert read_activation(fixture["runtime"]) is None


def test_apply_promotes_same_external_pointer_and_closes_all_three_gates(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    plan = _plan(fixture)
    old_active_bundle = fixture["staged"]["slots"]["ACTIVE"]["bundle_sha256"]  # type: ignore[index]
    candidate_bundle = fixture["staged"]["slots"]["CANDIDATE"]["bundle_sha256"]  # type: ignore[index]
    result = m11c._apply_cutover(
        authority_root=fixture["authority"], cutover_id="final-1", expected_plan_sha256=plan["cutover_plan_sha256"], operator_approved=True, tcb_witness=fixture["witness"]
    )
    assert result["status"] == "ACTIVE"
    assert result["production_activation_performed"] is True
    bootstrap = m11c.require_bootstrap_active(fixture["authority"], expected_source_head=HEAD)
    assert bootstrap["state"]["schema"] == m11c.SLOT_STATE_V2_SCHEMA
    assert bootstrap["state"]["activation_authority"] == m11c.M11C_ACTIVATION_AUTHORITY
    assert bootstrap["state"]["candidate_manifest_sha256"] is None
    assert bootstrap["slots"]["ACTIVE"]["bundle_sha256"] == candidate_bundle
    assert bootstrap["slots"]["PREVIOUS"]["bundle_sha256"] == old_active_bundle
    assert bootstrap["slots"]["ACTIVE"]["source_commit"] == HEAD
    client = read_activation(fixture["runtime"])
    assert client is not None and client.state == "ACTIVE"
    authority = CanonicalVNextAdmissionAuthority.open(fixture["runtime"], legacy_root=fixture["legacy"])
    try:
        assert authority.admission_enabled is True
    finally:
        authority.close()


def test_apply_rejects_missing_browser_native_verification_before_activating(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    plan = _plan(fixture)
    (Path(fixture["runtime"]) / "clients" / "browser-client-verification.json").unlink()
    with pytest.raises((m11c.M11cCutoverError, FileNotFoundError)):
        m11c._apply_cutover(
            authority_root=fixture["authority"], cutover_id="final-1", expected_plan_sha256=plan["cutover_plan_sha256"], operator_approved=True, tcb_witness=fixture["witness"]
        )
    assert read_activation(fixture["runtime"]) is None
    assert m11c.observe_bootstrap_activation(authority_root=fixture["authority"])["status"] == "PREPARED"


def test_apply_resumes_same_cutover_after_crash_window_left_client_gate_activating(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    plan = _plan(fixture)
    verified = record_clients_verified(
        fixture["runtime"], m9a_report=_m9a_report(), source_head=HEAD, source_tree=TREE,
        browser_bundle_digest=plan["browser_bundle_digest"], native_manifest_digest=plan["native_manifest_digest"], activation_id="m9b-final-1",
    )
    authority = CanonicalVNextAdmissionAuthority.open(fixture["runtime"], legacy_root=fixture["legacy"])
    try:
        m9b._begin_bootstrap_client_gate(fixture["runtime"], expected_activation_id=verified.activation_id)
        authority.enable_intake()
        assert authority.admission_enabled is True
    finally:
        authority.close()
    interrupted = read_activation(fixture["runtime"])
    assert interrupted is not None and interrupted.state == "ACTIVATING"
    assert m11c.observe_bootstrap_activation(authority_root=fixture["authority"])["status"] == "PREPARED"
    resumed = m11c._apply_cutover(
        authority_root=fixture["authority"], cutover_id="final-1", expected_plan_sha256=plan["cutover_plan_sha256"], operator_approved=True, tcb_witness=fixture["witness"]
    )
    assert resumed["status"] == "ACTIVE"
    assert read_activation(fixture["runtime"]).state == "ACTIVE"  # type: ignore[union-attr]
    assert m11c.require_bootstrap_active(fixture["authority"])["status"] == "ACTIVE"


def test_apply_resumes_after_external_switch_before_client_gate_finalize(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    plan = _plan(fixture)
    verified = record_clients_verified(
        fixture["runtime"], m9a_report=_m9a_report(), source_head=HEAD, source_tree=TREE,
        browser_bundle_digest=plan["browser_bundle_digest"], native_manifest_digest=plan["native_manifest_digest"], activation_id="m9b-final-1",
    )
    authority = CanonicalVNextAdmissionAuthority.open(fixture["runtime"], legacy_root=fixture["legacy"])
    try:
        m9b._begin_bootstrap_client_gate(fixture["runtime"], expected_activation_id=verified.activation_id)
        authority.enable_intake()
        prepared_query = m11c.query_prepared_activation(authority_root=fixture["authority"], preparation_id="prep-final")
        m11c._publish_external_activation(authority=Path(fixture["authority"]), plan=plan, prepared_query=prepared_query)
    finally:
        authority.close()
    assert m11c.require_bootstrap_active(fixture["authority"])["status"] == "ACTIVE"
    assert read_activation(fixture["runtime"]).state == "ACTIVATING"  # type: ignore[union-attr]
    resumed = m11c._apply_cutover(
        authority_root=fixture["authority"], cutover_id="final-1", expected_plan_sha256=plan["cutover_plan_sha256"], operator_approved=True, tcb_witness=fixture["witness"]
    )
    assert resumed["status"] == "ACTIVE"
    assert read_activation(fixture["runtime"]).state == "ACTIVE"  # type: ignore[union-attr]


def test_apply_is_idempotent_after_exact_completed_cutover(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    plan = _plan(fixture)
    first = m11c._apply_cutover(
        authority_root=fixture["authority"], cutover_id="final-1", expected_plan_sha256=plan["cutover_plan_sha256"], operator_approved=True, tcb_witness=fixture["witness"]
    )
    second = m11c._apply_cutover(
        authority_root=fixture["authority"], cutover_id="final-1", expected_plan_sha256=plan["cutover_plan_sha256"], operator_approved=True, tcb_witness=fixture["witness"]
    )
    assert first["bootstrap"]["state"]["state_sha256"] == second["bootstrap"]["state"]["state_sha256"]
    assert second["client_gate"]["state"] == "ACTIVE"


def test_m11a_reader_cannot_mutate_after_m11c_replaces_slot_state_v1(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    plan = _plan(fixture)
    m11c._apply_cutover(
        authority_root=fixture["authority"], cutover_id="final-1", expected_plan_sha256=plan["cutover_plan_sha256"], operator_approved=True, tcb_witness=fixture["witness"]
    )
    with pytest.raises(Exception):
        query_slot_authority(authority_root=fixture["authority"])
    assert m11c.require_bootstrap_active(fixture["authority"])["status"] == "ACTIVE"

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bdb_vnext.composition import ADMISSION_PROVIDER_ID, build_vnext_composition_manifest
from bdb_vnext.m3a_submission import M3aError, ShadowSubmissionRequest, ShadowSubmissionStore
from bdb_vnext.m3b_browser_admission import AdmissionEnvelope, M3B_PROTOCOL_GENERATION, M3bError
from bdb_vnext.m3c_admission import (
    M3C_CANONICAL_ROLE,
    M3C_PROTOCOL_GENERATION,
    M3C_SCHEMA,
    M3C_WRITER_ID,
    CanonicalNativeAdmissionBridge,
    M3cError,
    open_vnext_admission_composition,
    scan_supported_vnext_admission_paths,
)


ROOT = Path(__file__).resolve().parents[1]


def make_request(key: str = "browser:m3c-1", *, intent: dict[str, object] | None = None) -> ShadowSubmissionRequest:
    return ShadowSubmissionRequest(
        submission_key=key,
        intent_revision="r1",
        intent=intent or {"operation": "inspect", "path": "bdb_vnext/repo_view.py"},
        conversation_binding={"conversation_id": "conversation-m3c"},
        consumer_binding={"consumer_id": "browser-m3c", "kind": "browser"},
    )


def open_stack(tmp_path: Path, *, existing_outbox: bool = False):
    return open_vnext_admission_composition(
        tmp_path / "vnext",
        legacy_root=tmp_path / "legacy",
        existing_outbox=existing_outbox,
    )


def test_supported_composition_has_one_canonical_writer_and_no_alternate() -> None:
    result = scan_supported_vnext_admission_paths()
    assert result["pass"] is True
    assert result["alternate_accepting_writers"] == []
    assert result["canonical_provider_ids"] == [ADMISSION_PROVIDER_ID]
    writers = result["supported_accepting_writers"]
    assert len(writers) == 1
    assert writers[0]["authority"] == M3C_CANONICAL_ROLE
    assert writers[0]["owner"] == M3C_WRITER_ID
    assert result["legacy_paths_supported"] is False


def test_canonical_query_is_the_only_acceptance_truth_and_replays_same_task(tmp_path: Path) -> None:
    request = make_request("browser:m3c-query")
    with open_stack(tmp_path) as stack:
        first = stack.client.submit(request)
        replay = stack.client.submit(request)
        assert first.outcome == "published"
        # A previously ACKED outbox entry reuses its durable receipt.  The
        # canonical transport query still exposes the replay disposition.
        assert replay.task_id == first.task_id
        canonical_replay = stack.bridge.send(AdmissionEnvelope(request))
        assert canonical_replay.outcome == "replay"
        query = stack.query(request.submission_key, request.computed_digest())
        assert query is not None
        assert query.as_dict()["schema"] == M3C_SCHEMA
        assert query.as_dict()["authority"] == M3C_CANONICAL_ROLE
        assert query.task_id == first.task_id
        assert stack.authority.counts()["tasks"] == 1


def test_same_key_different_digest_is_a_canonical_conflict(tmp_path: Path) -> None:
    with open_stack(tmp_path) as stack:
        first = make_request("browser:m3c-conflict", intent={"operation": "one"})
        stack.client.submit(first)
        with pytest.raises(M3bError) as caught:
            stack.client.submit(make_request("browser:m3c-conflict", intent={"operation": "two"}))
        assert caught.value.code == "submission_conflict"
        with pytest.raises(M3cError) as canonical:
            stack.authority.admit(make_request("browser:m3c-conflict", intent={"operation": "two"}))
        assert canonical.value.code == "submission_conflict"
        assert stack.authority.counts()["tasks"] == 1


def test_lost_ack_and_browser_restart_recover_the_same_canonical_task(tmp_path: Path) -> None:
    request = make_request("browser:m3c-lost-ack")
    with open_stack(tmp_path) as stack:
        with pytest.raises(M3cError) as caught:
            stack.client.submit(request, crash_point="after_send_before_ack")
        assert caught.value.code == "ack_lost"
        first_query = stack.query(request.submission_key, request.computed_digest())
        assert first_query is not None
        first_task = first_query.task_id
        assert stack.outbox.get(request.submission_key).state == "SENT"  # type: ignore[union-attr]
        stack.close()

    with open_stack(tmp_path, existing_outbox=True) as restarted:
        recovered = restarted.client.recover(request.submission_key)
        assert recovered is not None
        assert recovered.task_id == first_task
        assert restarted.query(request.submission_key, request.computed_digest()).task_id == first_task  # type: ignore[union-attr]


def test_native_restart_uses_canonical_lookup_and_protocol_skew_fails_closed(tmp_path: Path) -> None:
    request = make_request("browser:m3c-native-restart")
    with open_stack(tmp_path) as stack:
        first = stack.client.submit(request)
        restarted_bridge = CanonicalNativeAdmissionBridge(stack.authority)
        recovered = restarted_bridge.lookup(
            request.submission_key,
            request.computed_digest(),
            protocol_generation=M3C_PROTOCOL_GENERATION,
        )
        assert recovered is not None
        assert recovered.task_id == first.task_id

        skewed = CanonicalNativeAdmissionBridge(stack.authority, protocol_generation="bdb-vnext-protocol-v0")
        with pytest.raises(M3cError) as caught:
            skewed.handshake(M3C_PROTOCOL_GENERATION)
        assert caught.value.code == "unsupported_protocol"

        with pytest.raises(M3bError) as caught_envelope:
            AdmissionEnvelope(request, protocol_generation="legacy-protocol-v1")
        assert caught_envelope.value.code == "unsupported_protocol"


def test_kill_switch_stops_new_admission_but_keeps_query_and_state(tmp_path: Path) -> None:
    first = make_request("browser:m3c-kill-existing")
    blocked = make_request("browser:m3c-kill-blocked")
    with open_stack(tmp_path) as stack:
        accepted = stack.client.submit(first)
        stack.authority.disable_intake()
        assert stack.authority.admission_enabled is False
        with pytest.raises(M3cError) as caught:
            stack.client.submit(blocked)
        assert caught.value.code == "admission_disabled"
        assert stack.query(first.submission_key, first.computed_digest()).task_id == accepted.task_id  # type: ignore[union-attr]
        assert stack.query(blocked.submission_key, blocked.computed_digest()) is None

        stack.authority.enable_intake()
        assert stack.client.submit(blocked).status == "ACCEPTED"


def test_kill_after_canonical_commit_preserves_task_for_reconciliation(tmp_path: Path) -> None:
    request = make_request("browser:m3c-kill-after-commit")
    with open_stack(tmp_path) as stack:
        with pytest.raises(M3cError) as caught:
            stack.authority.admit(request, failpoint="after_commit")
        assert caught.value.code == "simulated_response_loss_after_commit"
        stack.authority.disable_intake()
        query = stack.query(request.submission_key, request.computed_digest())
        assert query is not None
        assert query.task_id is not None


def test_database_busy_is_explicit_and_does_not_fake_acceptance(tmp_path: Path) -> None:
    with open_stack(tmp_path) as stack:
        holder = ShadowSubmissionStore(tmp_path / "vnext", shadow=True, legacy_root=tmp_path / "legacy", busy_timeout_ms=25)
        try:
            request = make_request("browser:m3c-busy")
            with holder.hold_write_lock():
                with pytest.raises(M3cError) as caught:
                    stack.authority.admit(request)
                assert caught.value.code == "database_busy"
            assert stack.query(request.submission_key, request.computed_digest()) is None
        finally:
            holder.close()


def test_client_anchor_loss_fails_closed_without_new_task(tmp_path: Path) -> None:
    request = make_request("browser:m3c-anchor-loss")
    with open_stack(tmp_path) as stack:
        with pytest.raises(M3bError) as caught:
            stack.client.submit(request, crash_point="after_outbox_before_send")
        assert caught.value.code == "simulated_browser_crash_before_send"
        stack.close()

    anchor = tmp_path / "vnext" / "browser" / "outbox" / "anchor.json"
    anchor.unlink()
    with pytest.raises(M3bError) as caught:
        open_stack(tmp_path, existing_outbox=True)
    assert caught.value.code == "anchor_lost"

    with ShadowSubmissionStore(tmp_path / "vnext", shadow=True, legacy_root=tmp_path / "legacy") as store:
        assert store.lookup(request.submission_key) is None


def test_m3c_query_schema_and_control_markers_are_deterministic(tmp_path: Path) -> None:
    request = make_request("browser:m3c-schema")
    with open_stack(tmp_path) as stack:
        stack.client.submit(request)
        query = stack.query(request.submission_key, request.computed_digest())
        assert query is not None
        schema = json.loads((ROOT / "schemas" / "bdb-vnext-m3c-admission-v1.schema.json").read_text(encoding="utf-8"))
        assert schema["$id"] == M3C_SCHEMA
        assert query.as_dict()["protocol_generation"] == M3B_PROTOCOL_GENERATION
        control_root = tmp_path / "vnext" / "control"
        marker = json.loads((control_root / "m3c-control.json").read_text(encoding="utf-8"))
        kill = json.loads((control_root / "m3c-kill-switch.json").read_text(encoding="utf-8"))
        assert marker["production_intake"] is False
        assert marker["alternate_admission"] is False
        assert kill["admission_enabled"] is True


def test_composition_manifest_declares_canonical_admission_edge(tmp_path: Path) -> None:
    manifest = build_vnext_composition_manifest(
        source_commit="4" * 40,
        runtime_root=tmp_path / "vnext",
        legacy_runtime_root=tmp_path / "legacy",
        forbidden_roots=[ROOT],
    )
    providers = manifest["composition"]["providers"]
    canonical = [item for item in providers if item["provider_id"] == ADMISSION_PROVIDER_ID]
    assert len(canonical) == 1
    assert canonical[0]["kind"] == "canonical_admission_authority"
    assert canonical[0]["writer_enabled"] is False
    assert {tuple(edge.values()) for edge in manifest["composition"]["edges"]} >= {
        ("devmaster.bdb.vnext.native-transport", ADMISSION_PROVIDER_ID),
        (ADMISSION_PROVIDER_ID, "devmaster.bdb.vnext.control-store"),
    }

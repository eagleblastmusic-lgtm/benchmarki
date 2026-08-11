from __future__ import annotations

import json
from pathlib import Path

import pytest

from bdb_vnext.m3a_submission import M3aError, ShadowSubmissionRequest, ShadowSubmissionStore
from bdb_vnext.m3b_browser_admission import (
    AdmissionEnvelope,
    BrowserAdmissionClient,
    BrowserAdmissionOutbox,
    M3B_PROTOCOL_GENERATION,
    M3bError,
    ShadowNativeAdmissionBridge,
)


def make_request(key: str = "browser:m3b-1", *, intent: dict[str, object] | None = None) -> ShadowSubmissionRequest:
    return ShadowSubmissionRequest(
        submission_key=key,
        intent_revision="r1",
        intent=intent or {"operation": "inspect", "path": "bdb_vnext/repo_view.py"},
        conversation_binding={"conversation_id": "conversation-m3b"},
        consumer_binding={"consumer_id": "browser-m3b", "kind": "browser"},
    )


def make_stack(tmp_path: Path, *, max_entries: int = 128, host_generation: str = M3B_PROTOCOL_GENERATION):
    store = ShadowSubmissionStore(tmp_path / "control", shadow=True, legacy_root=tmp_path / "legacy")
    outbox = BrowserAdmissionOutbox(
        tmp_path / "client",
        shadow=True,
        legacy_root=tmp_path / "legacy",
        max_entries=max_entries,
    )
    bridge = ShadowNativeAdmissionBridge(store, protocol_generation=host_generation)
    client = BrowserAdmissionClient(outbox, bridge)
    return store, outbox, bridge, client


def test_capability_handshake_is_exact_and_non_production(tmp_path: Path) -> None:
    store = ShadowSubmissionStore(tmp_path / "control", shadow=True)
    bridge = ShadowNativeAdmissionBridge(store)
    capability = bridge.handshake(M3B_PROTOCOL_GENERATION)
    assert capability.protocol_generation == M3B_PROTOCOL_GENERATION
    assert capability.canonical_lookup is True
    assert capability.production_acceptance is False
    with pytest.raises(M3bError) as caught:
        bridge.handshake("legacy-protocol-v12")
    assert caught.value.code == "unsupported_protocol"
    store.close()


def test_outbox_is_durable_before_send_and_ack_binds_one_canonical_task(tmp_path: Path) -> None:
    store, outbox, bridge, client = make_stack(tmp_path)
    request = make_request()
    receipt = client.submit(request)
    assert receipt.status == "ACCEPTED"
    assert outbox.get(request.submission_key).state == "ACKED"  # type: ignore[union-attr]
    assert store.counts()["tasks"] == 1
    lookup = bridge.lookup(request.submission_key, request.computed_digest(), protocol_generation=M3B_PROTOCOL_GENERATION)
    assert lookup is not None
    assert lookup.task_id == receipt.task_id
    assert lookup.intent_revision_id == receipt.intent_revision_id
    assert lookup.outcome == "replay"
    outbox.close()
    store.close()


def test_crash_after_outbox_before_send_retries_same_key(tmp_path: Path) -> None:
    store, outbox, bridge, client = make_stack(tmp_path)
    request = make_request("browser:pre-send-crash")
    with pytest.raises(M3bError) as caught:
        client.submit(request, crash_point="after_outbox_before_send")
    assert caught.value.code == "simulated_browser_crash_before_send"
    assert store.lookup(request.submission_key) is None
    outbox.close()
    restarted = BrowserAdmissionOutbox.open_existing(tmp_path / "client", shadow=True, legacy_root=tmp_path / "legacy")
    restarted_client = BrowserAdmissionClient(restarted, ShadowNativeAdmissionBridge(store))
    receipt = restarted_client.retry(request.submission_key)
    assert receipt.status == "ACCEPTED"
    assert store.counts()["tasks"] == 1
    restarted.close()
    store.close()


def test_lost_ack_lookup_recovers_same_task_without_resend(tmp_path: Path) -> None:
    store, outbox, bridge, client = make_stack(tmp_path)
    request = make_request("browser:lost-ack")
    with pytest.raises(M3bError) as caught:
        client.submit(request, crash_point="after_send_before_ack")
    assert caught.value.code == "ack_lost"
    assert outbox.get(request.submission_key).state == "SENT"  # type: ignore[union-attr]
    first_counts = store.counts()
    outbox.close()
    restarted = BrowserAdmissionOutbox.open_existing(tmp_path / "client", shadow=True, legacy_root=tmp_path / "legacy")
    recovered = BrowserAdmissionClient(restarted, ShadowNativeAdmissionBridge(store)).recover(request.submission_key)
    assert recovered is not None
    assert recovered.outcome == "replay"
    assert store.counts() == first_counts
    restarted.close()
    store.close()


def test_duplicate_transport_and_same_key_retry_replay(tmp_path: Path) -> None:
    store, outbox, bridge, client = make_stack(tmp_path)
    request = make_request("browser:duplicate")
    first = client.submit(request)
    outbox_entry = outbox.get(request.submission_key)
    assert outbox_entry is not None
    duplicate = bridge.send(AdmissionEnvelope(request))
    assert duplicate.outcome == "replay"
    assert duplicate.task_id == first.task_id
    assert store.counts()["tasks"] == 1
    outbox.close()
    store.close()


def test_same_key_conflict_is_rejected_without_new_outbox_or_task(tmp_path: Path) -> None:
    store, outbox, bridge, client = make_stack(tmp_path)
    first = make_request("browser:conflict")
    client.submit(first)
    with pytest.raises(M3bError) as caught:
        client.submit(make_request("browser:conflict", intent={"operation": "different"}))
    assert caught.value.code == "submission_conflict"
    assert store.counts()["tasks"] == 1
    outbox.close()
    store.close()


def test_host_restart_and_extension_update_reopen_same_outbox(tmp_path: Path) -> None:
    store, outbox, bridge, client = make_stack(tmp_path)
    request = make_request("browser:restart")
    with pytest.raises(M3bError):
        client.submit(request, crash_point="after_send_before_ack")
    outbox.close()
    restarted_outbox = BrowserAdmissionOutbox.open_existing(tmp_path / "client", shadow=True, legacy_root=tmp_path / "legacy")
    restarted_client = BrowserAdmissionClient(restarted_outbox, ShadowNativeAdmissionBridge(store))
    receipt = restarted_client.recover(request.submission_key)
    assert receipt is not None
    assert restarted_outbox.get(request.submission_key).state == "ACKED"  # type: ignore[union-attr]
    restarted_outbox.close()
    store.close()


def test_old_host_new_extension_version_skew_fails_before_outbox_write(tmp_path: Path) -> None:
    store = ShadowSubmissionStore(tmp_path / "control", shadow=True)
    outbox = BrowserAdmissionOutbox(tmp_path / "client", shadow=True)
    old_host = ShadowNativeAdmissionBridge(store, protocol_generation="bdb-vnext-protocol-v0")
    with pytest.raises(M3bError) as caught:
        BrowserAdmissionClient(outbox, old_host)
    assert caught.value.code == "unsupported_protocol"
    assert outbox.get("browser:version-skew") is None
    outbox.close()
    store.close()


def test_quota_and_corrupt_outbox_are_explicit(tmp_path: Path) -> None:
    store = ShadowSubmissionStore(tmp_path / "control", shadow=True)
    outbox = BrowserAdmissionOutbox(tmp_path / "client", shadow=True, max_entries=1)
    client = BrowserAdmissionClient(outbox, ShadowNativeAdmissionBridge(store))
    client.submit(make_request("browser:quota-1"))
    with pytest.raises(M3bError) as quota:
        client.submit(make_request("browser:quota-2"))
    assert quota.value.code == "outbox_quota"
    outbox.close()
    store.close()

    corrupted = BrowserAdmissionOutbox.open_existing(tmp_path / "client", shadow=True)
    corrupted._connection.execute(
        "UPDATE m3b_outbox SET request_json=? WHERE submission_key=?",
        (b"not-json", "browser:quota-1"),
    )
    corrupted._connection.commit()
    corrupted.close()
    with pytest.raises(M3bError) as corrupt:
        BrowserAdmissionOutbox.open_existing(tmp_path / "client", shadow=True)
    assert corrupt.value.code == "outbox_corrupt"


def test_total_anchor_loss_fails_closed_and_never_auto_resubmits(tmp_path: Path) -> None:
    store, outbox, bridge, client = make_stack(tmp_path)
    request = make_request("browser:anchor-loss")
    with pytest.raises(M3bError):
        client.submit(request, crash_point="after_outbox_before_send")
    outbox.close()
    (tmp_path / "client" / "browser" / "outbox" / "anchor.json").unlink()
    with pytest.raises(M3bError) as caught:
        BrowserAdmissionOutbox.open_existing(tmp_path / "client", shadow=True)
    assert caught.value.code == "anchor_lost"
    assert store.lookup(request.submission_key) is None
    store.close()


def test_browser_outbox_is_not_a_task_authority(tmp_path: Path) -> None:
    store, outbox, bridge, client = make_stack(tmp_path)
    request = make_request("browser:non-authority")
    outbox.prepare(AdmissionEnvelope(request))
    assert store.lookup(request.submission_key) is None
    assert store.counts()["tasks"] == 0
    outbox.close()
    store.close()

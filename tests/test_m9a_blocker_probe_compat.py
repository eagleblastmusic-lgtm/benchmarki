from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from bdb_vnext.m9a_blocker_probe_compat import _promoter_observation, _receipt_shape


def write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def promotion_receipt(*, event_seq: int | None = None, session: str = "session-a") -> dict[str, object]:
    value: dict[str, object] = {
        "schema": "bdb-workspace-promotion-v1",
        "status": "promoted",
        "session_id": session,
        "sequence": 1,
        "source_commit": "1" * 40,
        "parent_commit": "2" * 40,
        "changed_files": ["app.py"],
    }
    if event_seq is not None:
        value["repository_event_seq"] = event_seq
    return value


def test_cli_wrapper_can_run_directly_from_scripts_path() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "run_m9a_blocker_probe_compat.py"

    completed = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "source-compatible read-only M9a legacy blocker probe" in completed.stdout


def test_native_receipt_store_accepts_missing_submission_reservations(tmp_path: Path) -> None:
    store = tmp_path / "native-host-requests.json"
    write_json(
        store,
        {
            "schema": "bdb-native-request-receipts-v1",
            "requests": {"request-1": {"opaque": True}},
        },
    )

    observed = _receipt_shape(store)

    assert observed["status"] == "VALID_LEGACY_COMPAT_SHAPE"
    assert observed["requests_count"] == 1
    assert observed["submission_reservations_present"] is False
    assert observed["submission_reservations_count"] == 0
    assert observed["compatibility_rule"] == "missing_submission_reservations_is_implicit_empty"
    assert observed["issues"] == []


def test_native_receipt_store_rejects_non_mapping_reservations(tmp_path: Path) -> None:
    store = tmp_path / "native-host-requests.json"
    write_json(
        store,
        {
            "schema": "bdb-native-request-receipts-v1",
            "requests": {},
            "submission_reservations": [],
        },
    )

    observed = _receipt_shape(store)

    assert observed["status"] == "INVALID_SHAPE"
    assert "invalid_submission_reservations_store" in observed["issues"]


def test_promoter_accepts_historical_receipts_without_event_sequence(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    write_json(
        runtime / "workspace-promoter-state.json",
        {
            "schema": "bdb-workspace-promoter-state-v1",
            "initialized": True,
            "seen": {"sessions/a/results/000001.json": {"status": "promoted"}},
        },
    )
    write_json(runtime / "promotions" / "legacy.json", promotion_receipt())
    write_json(
        runtime / "promotions" / ".repository-event-seq.json",
        {
            "schema": "bdb-repository-event-seq-v1",
            "repository_event_seq": 3,
        },
    )

    observed = _promoter_observation(runtime, max_records=100)

    assert observed["status"] == "VALID_LEGACY_COMPAT"
    assert observed["receipt_count"] == 1
    assert observed["legacy_unsequenced_receipts"] == 1
    assert observed["sequenced_receipts"] == 0
    assert observed["repository_event_seq"] == 3
    assert observed["issues"] == []


def test_promoter_accepts_mixed_historical_and_sequenced_receipts(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    write_json(
        runtime / "workspace-promoter-state.json",
        {
            "schema": "bdb-workspace-promoter-state-v1",
            "initialized": True,
            "seen": {},
        },
    )
    write_json(runtime / "promotions" / "legacy.json", promotion_receipt(session="legacy"))
    write_json(
        runtime / "promotions" / "current.json",
        promotion_receipt(event_seq=2, session="current"),
    )
    write_json(
        runtime / "promotions" / ".repository-event-seq.json",
        {
            "schema": "bdb-repository-event-seq-v1",
            "repository_event_seq": 3,
        },
    )

    observed = _promoter_observation(runtime, max_records=100)

    assert observed["status"] == "VALID_LEGACY_COMPAT"
    assert observed["legacy_unsequenced_receipts"] == 1
    assert observed["sequenced_receipts"] == 1
    assert observed["repository_event_seq"] == 3
    assert observed["issues"] == []


def test_promoter_rejects_counter_behind_persisted_receipt(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    write_json(
        runtime / "workspace-promoter-state.json",
        {
            "schema": "bdb-workspace-promoter-state-v1",
            "initialized": True,
            "seen": {},
        },
    )
    write_json(
        runtime / "promotions" / "current.json",
        promotion_receipt(event_seq=4),
    )
    write_json(
        runtime / "promotions" / ".repository-event-seq.json",
        {
            "schema": "bdb-repository-event-seq-v1",
            "repository_event_seq": 3,
        },
    )

    observed = _promoter_observation(runtime, max_records=100)

    assert observed["status"] == "INVALID"
    assert {item["code"] for item in observed["issues"]} == {
        "sequence_counter_behind_persisted_receipt"
    }


def test_promoter_rejects_invalid_present_event_sequence(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    write_json(
        runtime / "workspace-promoter-state.json",
        {
            "schema": "bdb-workspace-promoter-state-v1",
            "initialized": True,
            "seen": {},
        },
    )
    receipt = promotion_receipt()
    receipt["repository_event_seq"] = "3"
    write_json(runtime / "promotions" / "invalid.json", receipt)
    write_json(
        runtime / "promotions" / ".repository-event-seq.json",
        {
            "schema": "bdb-repository-event-seq-v1",
            "repository_event_seq": 3,
        },
    )

    observed = _promoter_observation(runtime, max_records=100)

    assert observed["status"] == "INVALID"
    assert {item["code"] for item in observed["issues"]} == {"invalid_repository_event_seq"}

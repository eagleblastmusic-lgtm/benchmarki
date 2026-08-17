from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from bdb_vnext.m9a_blocker_probe import PROBE_SCHEMA, probe_profile


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _journal(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE commands (
            command_id TEXT PRIMARY KEY,
            state TEXT NOT NULL
        );
        CREATE TABLE service_instances (
            instance_id TEXT PRIMARY KEY,
            pid INTEGER NOT NULL,
            state TEXT NOT NULL,
            heartbeat_at TEXT NOT NULL
        );
        CREATE TABLE operation_plans (
            command_id TEXT PRIMARY KEY,
            operation TEXT NOT NULL,
            target_path TEXT NOT NULL,
            profile_id TEXT NOT NULL
        );
        CREATE TABLE operation_effects (
            command_id TEXT PRIMARY KEY
        );
        CREATE TABLE outbox (
            command_id TEXT PRIMARY KEY
        );
        """
    )
    connection.executemany(
        "INSERT INTO commands(command_id, state) VALUES (?, ?)",
        [
            ("cmd-new", "claimed"),
            ("cmd-effect", "effect_recorded"),
            ("cmd-result", "result_staged"),
            ("cmd-terminal", "acknowledged"),
        ],
    )
    connection.executemany(
        "INSERT INTO operation_plans(command_id, operation, target_path, profile_id) VALUES (?, ?, ?, ?)",
        [
            ("cmd-new", "replace_exact_and_test", "theme/a.liquid", "shopify_theme_check"),
            ("cmd-effect", "replace_exact_and_test", "theme/b.liquid", "shopify_theme_check"),
            ("cmd-result", "replace_exact_and_test", "theme/c.liquid", "shopify_theme_check"),
        ],
    )
    connection.execute("INSERT INTO operation_effects(command_id) VALUES ('cmd-effect')")
    connection.execute("INSERT INTO outbox(command_id) VALUES ('cmd-result')")
    connection.execute(
        "INSERT INTO service_instances(instance_id, pid, state, heartbeat_at) VALUES ('inst-dead', 111, 'running', '2026-08-17T00:00:00Z')"
    )
    connection.commit()
    connection.close()


def _fixture(tmp_path: Path, *, invalid_receipts: bool = False, invalid_promoter: bool = False):
    runtime = tmp_path / "runtime"
    spool = runtime / "direct_spool" / "inbox"
    results = runtime / "direct_spool" / "results"
    spool.mkdir(parents=True)
    results.mkdir(parents=True)
    journal = runtime / "journal.db"
    _journal(journal)

    bridge = tmp_path / "bridge-config.json"
    _write_json(
        bridge,
        {
            "schema_version": "1.1",
            "runtime_dir": str(runtime),
            "journal_path": str(journal),
            "direct_spool_enabled": True,
            "direct_spool_dir": str(spool),
            "direct_result_dir": str(results),
        },
    )

    _write_json(
        spool / "one.json",
        {
            "schema": "bdb-local-envelope-v1",
            "command": {"command_id": "cmd-new"},
        },
    )
    _write_json(
        spool / "two.json",
        {
            "schema": "bdb-local-envelope-v1",
            "command": {"command_id": "cmd-not-in-journal"},
        },
    )

    native = tmp_path / "native-host.json"
    receipt_store = tmp_path / "native-host-requests.json"
    arm = tmp_path / "native-host-arm.json"
    _write_json(
        native,
        {
            "schema": "bdb-native-host-config-v1",
            "repositories": {"target": {"bridge_config_path": str(bridge)}},
            "allowed_origins": ["chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/"],
            "state_path": str(arm),
            "request_store_path": str(receipt_store),
        },
    )
    _write_json(
        arm,
        {
            "schema": "bdb-native-arm-v1",
            "armed": True,
            "armed_until": "2026-08-17T16:00:00Z",
            "generation_id": "g1",
        },
    )
    if invalid_receipts:
        _write_json(
            receipt_store,
            {
                "schema": "bdb-native-request-receipts-v1",
                "requests": [],
                "submission_reservations": {},
            },
        )
    else:
        _write_json(
            receipt_store,
            {
                "schema": "bdb-native-request-receipts-v1",
                "requests": {},
                "submission_reservations": {},
            },
        )

    promotions = runtime / "promotions"
    promotions.mkdir()
    _write_json(
        runtime / "workspace-promoter-state.json",
        {
            "schema": "bdb-workspace-promoter-state-v1",
            "initialized": True,
            "seen": {},
        },
    )
    if invalid_promoter:
        _write_json(
            promotions / "bad.json",
            {
                "schema": "bdb-workspace-promotion-v1",
                "source_commit": "not-a-sha",
                "parent_commit": "0" * 40,
                "repository_event_seq": 1,
                "session_id": "s1",
                "sequence": 1,
            },
        )
    else:
        _write_json(
            promotions / ".repository-event-seq.json",
            {
                "schema": "bdb-repository-event-seq-v1",
                "repository_event_seq": 0,
            },
        )
    return bridge, native, runtime


def test_probe_classifies_recovery_capability_without_mutating_legacy(tmp_path: Path) -> None:
    bridge, native, runtime = _fixture(tmp_path)
    journal_before = (runtime / "journal.db").read_bytes()
    spool_before = sorted(path.read_bytes() for path in (runtime / "direct_spool" / "inbox").iterdir())

    probe = probe_profile(
        profile_id="target",
        bridge_config_path=bridge,
        native_config_path=native,
        scratch_dir=tmp_path / "scratch",
        now_fn=lambda: datetime(2026, 8, 17, 15, 0, tzinfo=timezone.utc),
        pid_alive_fn=lambda pid: False,
        wake_event_fn=lambda runtime_path: False,
    ).as_dict()

    assert probe["schema"] == PROBE_SCHEMA
    assert probe["legacy_mutation_performed"] is False
    assert probe["vnext_activation_allowed"] is False
    assert probe["m9b_allowed"] is False
    assert probe["wake_event_present"] is False
    assert probe["journal"]["service_candidates"]["items"][0]["pid_alive"] is False
    assert probe["journal"]["capability_counts"] == {
        "ACKNOWLEDGEMENT_ONLY_ON_SERVICE_RESTART": 0,
        "IDEMPOTENT_REPLAY_OR_DIVERGENCE_ONLY": 1,
        "POTENTIAL_EXECUTION_WRITE_ON_SERVICE_RESTART": 1,
        "PUBLICATION_RECOVERY_ON_SERVICE_RESTART": 1,
    } or probe["journal"]["capability_counts"] == {
        "IDEMPOTENT_REPLAY_OR_DIVERGENCE_ONLY": 1,
        "POTENTIAL_EXECUTION_WRITE_ON_SERVICE_RESTART": 1,
        "PUBLICATION_RECOVERY_ON_SERVICE_RESTART": 1,
    }
    assert probe["spool"]["classification_counts"] == {
        "CORRELATED_WITH_UNRESOLVED_COMMAND": 1,
        "NEW_INGRESS_IF_SERVICE_RESTARTS": 1,
    }
    assert probe["native"]["profile_bound"] is True
    assert probe["native"]["arm"]["effective_armed"] is True
    assert (runtime / "journal.db").read_bytes() == journal_before
    assert sorted(path.read_bytes() for path in (runtime / "direct_spool" / "inbox").iterdir()) == spool_before


def test_probe_explains_receipt_shape_and_promoter_identity_failures(tmp_path: Path) -> None:
    bridge, native, _ = _fixture(tmp_path, invalid_receipts=True, invalid_promoter=True)

    probe = probe_profile(
        profile_id="target",
        bridge_config_path=bridge,
        native_config_path=native,
        scratch_dir=tmp_path / "scratch",
        now_fn=lambda: datetime(2026, 8, 17, 15, 0, tzinfo=timezone.utc),
        pid_alive_fn=lambda pid: False,
        wake_event_fn=lambda runtime_path: False,
    ).as_dict()

    assert probe["receipts"]["status"] == "INVALID_SHAPE"
    assert probe["receipts"]["requests_type"] == "list"
    assert probe["receipts"]["submission_reservations_type"] == "dict"
    assert probe["promoter"]["status"] == "INVALID"
    assert probe["promoter"]["issues"][0]["code"] == "invalid_source_commit"
    assert "command_json" not in json.dumps(probe)
    assert "not-a-sha" not in json.dumps(probe)


def test_missing_promotions_directory_is_reported_without_repair(tmp_path: Path) -> None:
    bridge, native, runtime = _fixture(tmp_path)
    for item in (runtime / "promotions").iterdir():
        item.unlink()
    (runtime / "promotions").rmdir()

    probe = probe_profile(
        profile_id="target",
        bridge_config_path=bridge,
        native_config_path=native,
        scratch_dir=tmp_path / "scratch",
        pid_alive_fn=lambda pid: False,
        wake_event_fn=lambda runtime_path: False,
    ).as_dict()

    assert probe["promoter"]["status"] == "MISSING"
    assert "promotions/" in probe["promoter"]["missing_components"]
    assert not (runtime / "promotions").exists()


def test_probe_source_has_no_legacy_import_or_observed_store_mutation_api() -> None:
    source = (Path(__file__).resolve().parents[1] / "bdb_vnext" / "m9a_blocker_probe.py").read_text(encoding="utf-8")
    assert "from bdb_bridge" not in source
    assert "import bdb_bridge" not in source
    for forbidden in (
        "INSERT INTO",
        "UPDATE commands",
        "DELETE FROM",
        "os.replace(",
        "subprocess",
        "winreg.SetValue",
        "winreg.Delete",
    ):
        assert forbidden not in source

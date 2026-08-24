from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import bdb_vnext.single_root_migration as migration
from bdb_shared.evidence import canonical_json_bytes
from bdb_vnext.composition import build_vnext_composition_manifest
from bdb_vnext.m3c_admission import (
    CanonicalVNextAdmissionAuthority,
    M3C_AUTHORITY_ID,
    M3C_CONTROL_SCHEMA,
    M3C_CONTROL_SCHEMA_V1,
    M3C_PROTOCOL_GENERATION,
    M3C_WRITER_ID,
)
from bdb_vnext.m9b_activation import ActivationRecord, write_activation
from bdb_vnext.project_catalog import ProjectBrief, ProjectCatalog, new_project_record
from bdb_vnext.provider_root import VNextCompositionRoot


HEAD = "1" * 40
TREE = "2" * 40
CLIENT = "sha256:" + "a" * 64
OLD_M9A = "sha256:" + "b" * 64
OLD_BROWSER = "sha256:" + "c" * 64
OLD_NATIVE = "sha256:" + "d" * 64
FINAL_HEAD = "5" * 40
FINAL_TREE = "6" * 40
FINAL_CLIENT = "sha256:" + "e" * 64


def _write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    initializing: bool = False,
) -> tuple[Path, Path, Path]:
    source = tmp_path / "retired-appdata"
    target = tmp_path / "repo" / "runtime"
    legacy = tmp_path / "legacy"
    source.mkdir(parents=True)
    target.mkdir(parents=True)
    legacy.mkdir()

    if initializing:
        authority = CanonicalVNextAdmissionAuthority.open(source, legacy_root=legacy)
        authority.close()
    else:
        composition = VNextCompositionRoot.from_manifest(
            build_vnext_composition_manifest(
                source_commit=HEAD,
                runtime_root=source,
                legacy_runtime_root=legacy,
            )
        )
        with composition.open_control_plane():
            pass
    marker = {
        "schema": M3C_CONTROL_SCHEMA_V1,
        "authority_id": M3C_AUTHORITY_ID,
        "writer_id": M3C_WRITER_ID,
        "protocol_generation": M3C_PROTOCOL_GENERATION,
        "mode": "INTERNAL_CANONICAL_ONLY",
        "legacy_import": False,
        "alternate_admission": False,
        "production_intake": False,
    }
    (source / "control" / "m3c-control.json").write_bytes(canonical_json_bytes(marker))
    write_activation(
        source,
        ActivationRecord(
            activation_id="m9b-old-active",
            state="ACTIVE",
            source_head="3" * 40,
            source_tree="4" * 40,
            m9a_freeze_digest=OLD_M9A,
            browser_bundle_digest=OLD_BROWSER,
            native_manifest_digest=OLD_NATIVE,
            writer_enabled=True,
            intake_enabled=True,
        ),
    )
    catalog = ProjectCatalog(source)
    catalog.upsert(
        new_project_record(
            project_id="project-1",
            display_name="Fixture",
            repo_alias="fixture",
            local_repo_path=tmp_path / "managed-repo",
            github_repo=None,
            brief=ProjectBrief("Fixture", "Test migration", "Bounded fixture", "test"),
        )
    )
    _write(source / "control" / "project-memory" / "project-1" / "memory.json", b'{"fixture":true}\n')
    _write(source / "recovery" / "old-proof" / "evidence.bin", b"immutable recovery")
    _write(source / "clients" / "browser-extension" / "manifest.json", b"old client")
    _write(source / "config" / "native-host.json", b"old native config")

    plan = {
        "client_plan_sha256": CLIENT,
        "source_head": HEAD,
        "source_tree": TREE,
        "production_activation_performed": False,
        "browser_bundle_root": str(target / "clients" / "browser-extension"),
        "native_manifest_path": str(target / "clients" / "native-host" / "com.bartosz.dev_bridge.vnext.json"),
        "native_host_executable": str(target / "clients" / "native-host" / "BDB-vNext-NativeHost.exe"),
        "native_config_path": str(target / "config" / "native-host.json"),
    }
    monkeypatch.setattr(migration, "query_client_plan", lambda **_: {"plan": plan})
    return source, target, legacy


def _prepare(source: Path, target: Path, legacy: Path) -> dict[str, object]:
    return migration.prepare_single_root_migration(
        source_runtime_root=source,
        target_runtime_root=target,
        legacy_runtime_root=legacy,
        migration_id="single-root-test",
        source_head=HEAD,
        source_tree=TREE,
        expected_client_plan_sha256=CLIENT,
    )


def test_exact_state_and_recovery_move_to_repo_root_and_m3c_migrates_v2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, target, legacy = _fixture(tmp_path, monkeypatch)
    before = migration._inventory(source)
    prepared = _prepare(source, target, legacy)

    applied = migration.apply_single_root_migration(
        target_runtime_root=target,
        migration_id="single-root-test",
        expected_plan_sha256=prepared["plan"]["plan_sha256"],
        operator_approved=True,
    )

    assert applied["status"] == "COMPLETED"
    assert migration._inventory(source) == before
    assert json.loads((target / "control" / "m3c-control.json").read_text(encoding="utf-8")) == {
        "schema": M3C_CONTROL_SCHEMA,
        "authority_id": M3C_AUTHORITY_ID,
        "writer_id": M3C_WRITER_ID,
        "protocol_generation": M3C_PROTOCOL_GENERATION,
        "mode": "INTERNAL_CANONICAL_ONLY",
        "legacy_import": False,
        "alternate_admission": False,
    }
    assert (target / "control" / "project-memory" / "project-1" / "memory.json").read_bytes() == b'{"fixture":true}\n'
    assert (target / "browser" / "outbox" / "anchor.json").read_bytes() == (
        source / "browser" / "outbox" / "anchor.json"
    ).read_bytes()
    assert (target / "browser" / "outbox" / "outbox.db").read_bytes() == (
        source / "browser" / "outbox" / "outbox.db"
    ).read_bytes()
    assert (target / "recovery" / "appdata-legacy" / "single-root-test" / "old-proof" / "evidence.bin").read_bytes() == b"immutable recovery"
    assert not (target / "clients" / "browser-extension" / "manifest.json").exists()
    assert not (target / "config" / "native-host.json").exists()
    replay = migration.apply_single_root_migration(
        target_runtime_root=target,
        migration_id="single-root-test",
        expected_plan_sha256=prepared["plan"]["plan_sha256"],
        operator_approved=True,
    )
    assert replay["replayed"] is True


def test_copy_fault_replays_without_duplicate_or_source_loss(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, target, legacy = _fixture(tmp_path, monkeypatch)
    prepared = _prepare(source, target, legacy)

    def fault(point: str) -> None:
        if point == "after_copy_0":
            raise RuntimeError("crash")

    with pytest.raises(RuntimeError, match="crash"):
        migration.apply_single_root_migration(
            target_runtime_root=target,
            migration_id="single-root-test",
            expected_plan_sha256=prepared["plan"]["plan_sha256"],
            operator_approved=True,
            fault_hook=fault,
        )
    completed = migration.apply_single_root_migration(
        target_runtime_root=target,
        migration_id="single-root-test",
        expected_plan_sha256=prepared["plan"]["plan_sha256"],
        operator_approved=True,
    )
    assert completed["status"] == "COMPLETED"
    assert source.exists()


def test_known_empty_initializing_control_is_preserved_then_completed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, target, legacy = _fixture(tmp_path, monkeypatch, initializing=True)
    source_database = (source / "control" / "control.db").read_bytes()
    source_seal = (source / "control" / "control.db.seal.json").read_bytes()
    assert json.loads(source_seal)["state"] == "INITIALIZING"
    prepared = _prepare(source, target, legacy)

    applied = migration.apply_single_root_migration(
        target_runtime_root=target,
        migration_id="single-root-test",
        expected_plan_sha256=prepared["plan"]["plan_sha256"],
        operator_approved=True,
    )

    control = applied["result"]["control"]
    assert control["upgraded_from_initializing"] is True
    assert control["integrity_check"] == "ok"
    assert control["foreign_key_violations"] == 0
    assert control["user_version"] == migration.CONTROL_DB_USER_VERSION
    archive = target / "recovery" / "single-root-migration" / "single-root-test.initializing-control"
    assert (archive / "control.db").read_bytes() == source_database
    assert (archive / "control.db.seal.json").read_bytes() == source_seal
    assert (source / "control" / "control.db").read_bytes() == source_database
    assert (source / "control" / "control.db.seal.json").read_bytes() == source_seal
    assert json.loads((target / "control" / "control.db.seal.json").read_text(encoding="utf-8"))["state"] == "SEALED"


def test_nonempty_initializing_control_is_not_silently_completed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, target, legacy = _fixture(tmp_path, monkeypatch, initializing=True)
    connection = sqlite3.connect(source / "control" / "control.db")
    try:
        connection.execute(
            "INSERT INTO m3a_submissions("
            "submission_key,request_digest,canonical_request,status,disposition,created_order"
            ") VALUES (?,?,?,?,?,?)",
            ("foreign", "sha256:" + "1" * 64, b"{}", "TOMBSTONED", "REJECTED", 1),
        )
        connection.commit()
    finally:
        connection.close()
    prepared = _prepare(source, target, legacy)

    with pytest.raises(migration.SingleRootMigrationError) as caught:
        migration.apply_single_root_migration(
            target_runtime_root=target,
            migration_id="single-root-test",
            expected_plan_sha256=prepared["plan"]["plan_sha256"],
            operator_approved=True,
        )

    assert caught.value.code == "migration_control_initialization_unknown"
    assert json.loads((target / "control" / "control.db.seal.json").read_text(encoding="utf-8"))["state"] == "INITIALIZING"


def test_changed_or_unknown_source_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, target, legacy = _fixture(tmp_path, monkeypatch)
    prepared = _prepare(source, target, legacy)
    (source / "control" / "project-catalog.json").write_text("{}", encoding="utf-8")
    with pytest.raises(migration.SingleRootMigrationError) as changed:
        migration.apply_single_root_migration(
            target_runtime_root=target,
            migration_id="single-root-test",
            expected_plan_sha256=prepared["plan"]["plan_sha256"],
            operator_approved=True,
        )
    assert changed.value.code == "migration_source_changed"

    source2, target2, legacy2 = _fixture(tmp_path / "second", monkeypatch)
    _write(source2 / "unknown-authority.json", b"{}")
    with pytest.raises(migration.SingleRootMigrationError) as unknown:
        _prepare(source2, target2, legacy2)
    assert unknown.value.code == "migration_unknown_source_state"


def test_retirement_fails_closed_before_live_gates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, target, legacy = _fixture(tmp_path, monkeypatch)
    prepared = _prepare(source, target, legacy)
    migration.apply_single_root_migration(
        target_runtime_root=target,
        migration_id="single-root-test",
        expected_plan_sha256=prepared["plan"]["plan_sha256"],
        operator_approved=True,
    )
    with pytest.raises(migration.SingleRootMigrationError):
        migration.retire_single_root_source(
            authority_root=tmp_path / "authority",
            target_runtime_root=target,
            migration_id="single-root-test",
            expected_plan_sha256=prepared["plan"]["plan_sha256"],
            operator_approved=True,
        )
    assert source.is_dir()


def test_retirement_binds_advanced_source_matched_live_subject(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target, legacy = _fixture(tmp_path, monkeypatch)
    prepared = _prepare(source, target, legacy)
    migration.apply_single_root_migration(
        target_runtime_root=target,
        migration_id="single-root-test",
        expected_plan_sha256=prepared["plan"]["plan_sha256"],
        operator_approved=True,
    )
    final_client = {
        "client_plan_sha256": FINAL_CLIENT,
        "source_head": FINAL_HEAD,
        "source_tree": FINAL_TREE,
        "production_activation_performed": False,
        "browser_bundle_root": str(target / "clients" / "browser-extension"),
        "native_manifest_path": str(target / "clients" / "native-host" / "com.bartosz.dev_bridge.vnext.json"),
        "native_host_executable": str(target / "clients" / "native-host" / "BDB-vNext-NativeHost.exe"),
        "native_config_path": str(target / "config" / "native-host.json"),
    }
    monkeypatch.setattr(migration, "query_client_plan", lambda **_: {"plan": final_client})
    monkeypatch.setattr(
        migration,
        "require_client_verification",
        lambda **_: {"client_plan_sha256": FINAL_CLIENT, "verification_sha256": "sha256:" + "f" * 64},
    )
    monkeypatch.setattr(
        migration,
        "observe_windows_native_routes",
        lambda **_: {"target_registered": True, "target_conflict": False, "legacy_route_present": False},
    )
    monkeypatch.setattr(
        migration,
        "observe_bootstrap_activation",
        lambda **_: {
            "status": "ACTIVE",
            "slots": {
                "ACTIVE": {"source_commit": FINAL_HEAD, "bundle_root": str(target / "bootstrap" / "active")},
                "PREVIOUS": {"source_commit": HEAD, "bundle_root": str(target / "bootstrap" / "previous")},
            },
            "state": {"state_sha256": "sha256:" + "0" * 64, "production_activation_performed": True},
        },
    )
    final_activation = ActivationRecord(
        activation_id="m9b-final-active",
        state="ACTIVE",
        source_head=FINAL_HEAD,
        source_tree=FINAL_TREE,
        m9a_freeze_digest=OLD_M9A,
        browser_bundle_digest=OLD_BROWSER,
        native_manifest_digest=OLD_NATIVE,
        writer_enabled=True,
        intake_enabled=True,
    )
    monkeypatch.setattr(migration, "read_activation", lambda *_: final_activation)
    monkeypatch.setattr(migration, "_verify_control", lambda *_args, **_kwargs: {"m3c_control_sha256": "sha256:" + "9" * 64})

    retired = migration.retire_single_root_source(
        authority_root=tmp_path / "authority",
        target_runtime_root=target,
        migration_id="single-root-test",
        expected_plan_sha256=prepared["plan"]["plan_sha256"],
        operator_approved=True,
    )

    assert retired["status"] == "RETIRED"
    assert retired["retirement"]["live_source_head"] == FINAL_HEAD
    assert retired["retirement"]["live_source_tree"] == FINAL_TREE
    assert retired["retirement"]["live_client_plan_sha256"] == FINAL_CLIENT
    assert not source.exists()

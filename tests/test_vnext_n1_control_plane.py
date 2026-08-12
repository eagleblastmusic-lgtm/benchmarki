import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from bdb_vnext.bootstrap import (
    CONTROL_BACKUP_SCHEMA,
    BootstrapError,
    create_coordinated_backup,
    restore_backup,
    verify_backup,
)
from bdb_vnext.composition import VNextLayout, build_vnext_composition_manifest
from bdb_vnext.content_store import DurableBindingStore, TypedContextFragment, make_content_ref
from bdb_vnext.m3a_submission import ShadowSubmissionRequest
from bdb_vnext.provider_root import VNextCompositionRoot
from bdb_vnext.control_store import ControlStoreError
from bdb_vnext.m4a_work_kernel import M4aError, WorkKernelStore


ROOT = Path(__file__).resolve().parents[1]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _manifest(tmp_path: Path) -> tuple[VNextCompositionRoot, Path, Path]:
    runtime = tmp_path / "vnext-runtime"
    legacy = tmp_path / "legacy-runtime"
    manifest = build_vnext_composition_manifest(
        source_commit="4" * 40,
        runtime_root=runtime,
        legacy_runtime_root=legacy,
        forbidden_roots=[ROOT],
    )
    return VNextCompositionRoot.from_manifest(manifest), runtime, legacy


def _request(key: str = "n1:request") -> ShadowSubmissionRequest:
    return ShadowSubmissionRequest(
        submission_key=key,
        intent_revision="r1",
        intent={"operation": "inspect", "path": "bdb_vnext/m4a_work_kernel.py"},
        conversation_binding={"conversation_id": "n1"},
        consumer_binding={"consumer_id": "n1", "kind": "browser"},
    )


def _repo(tmp_path: Path) -> object:
    repo = tmp_path / "subject"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.name", "N1 Test")
    _git(repo, "config", "user.email", "n1@example.invalid")
    (repo / "README.md").write_text("n1\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "N1 fixture")
    from bdb_vnext.repo_view import RepositoryResource

    return RepositoryResource.from_path(repo, repository_id="n1-subject").resolve_committed(
        "refs/heads/main", observed_at="2026-08-12T00:00:00Z"
    )


def test_gate_a_uses_one_physical_control_db_and_keeps_outbox_separate(tmp_path: Path) -> None:
    root, runtime, legacy = _manifest(tmp_path)
    with root.open_control_plane(clock=lambda: 100.0) as plane:
        assert plane.bindings.database_path == runtime / "control" / "control.db"
        assert plane.admission.authority.control_database_path == plane.bindings.database_path
        assert plane.work_kernel.database_path == plane.bindings.database_path
        assert plane.admission.outbox.database_path != plane.bindings.database_path
        receipt = plane.admission.authority.admit(_request())
        assert receipt.task_id
        item = plane.work_kernel.create_work_item("n1:work", receipt.task_id)
        assert plane.work_kernel.query(item.work_id).work == item  # type: ignore[union-attr]
        with sqlite3.connect(plane.bindings.database_path) as connection:
            tables = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        assert {"vnext_control_metadata", "m2b_accepted_bindings", "m3a_tasks", "m4a_work_items", "m3c_kill_switch"} <= tables
    assert VNextLayout.create(runtime).assert_isolated(legacy_runtime_root=legacy) is None


def test_gate_b_root_constructs_typed_control_plane_without_activation(tmp_path: Path) -> None:
    root, _runtime, _legacy = _manifest(tmp_path)
    assert root.status()["runtime_state"] == "OFF"
    assert root.status()["writer_state"] == "OFF"
    assert root.status()["activation_state"] == "OFF"
    with root.open_control_plane() as plane:
        assert plane.root is root
        assert plane.admission.authority.control_database_path == plane.work_kernel.database_path
        assert plane.work_kernel.writer_id == "m4a-vnext-work-kernel-writer"
        with sqlite3.connect(plane.work_kernel.database_path) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 2


def test_control_schema_version_and_layout_fail_closed(tmp_path: Path) -> None:
    root, runtime, _legacy = _manifest(tmp_path)
    with root.open_control_plane() as plane:
        database = plane.work_kernel.database_path
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version=99")
        connection.commit()
    with pytest.raises(ControlStoreError, match="user_version"):
        root.open_control_plane()


def test_gate_d_v2_backup_restores_semantic_state_and_reachable_content(tmp_path: Path) -> None:
    root, runtime, legacy = _manifest(tmp_path)
    subject = _repo(tmp_path)
    with root.open_control_plane(clock=lambda: 100.0) as plane:
        receipt = plane.admission.authority.admit(_request("n1:backup"))
        assert receipt.task_id
        item = plane.work_kernel.create_work_item("n1:backup-work", receipt.task_id)
        view = subject
        raw = b"n1-reachable\n"
        ref = make_content_ref("text/plain", "n1-test-v1", raw)
        plane.bindings.content_store.publish(ref, raw)
        fragment = TypedContextFragment.create(
            view,
            ref,
            fragment_type="text/plain",
            fragment_schema="n1-fragment-v1",
            payload_size_bytes=len(raw),
        )
        plane.bindings.accept(fragment, view=view)
        request_digest = receipt.request_digest
        before_admission = plane.admission.authority.query(_request("n1:backup").submission_key, request_digest)
        before_work = plane.work_kernel.query(item.work_id)
        assert before_admission is not None and before_work is not None
        authority = tmp_path / "backup-authority"
        artifact = create_coordinated_backup(
            runtime,
            authority / "backups",
            backup_id="n1-control",
            required_control_schema=1,
            source_is_quiesced=True,
            include_control_identity=True,
        )
        assert artifact.document["schema"] == CONTROL_BACKUP_SCHEMA
        backup_path = artifact.path
    verified = verify_backup(backup_path)
    restored = tmp_path / "cold-restore"
    receipt = restore_backup(
        backup_path,
        restored,
        authority_root=authority,
        legacy_runtime_root=legacy,
        forbidden_roots=(runtime,),
    )
    assert receipt["verified"] is True
    with sqlite3.connect(restored / "control" / "control.db") as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    restored_bindings = DurableBindingStore(restored)
    try:
        assert restored_bindings.resolve_accepted(fragment.fragment_id, expected_view=view).raw == raw
    finally:
        restored_bindings.close()
    restored_manifest = build_vnext_composition_manifest(
        source_commit="4" * 40,
        runtime_root=restored,
        legacy_runtime_root=tmp_path / "restored-legacy",
        forbidden_roots=[ROOT, runtime, authority],
    )
    restored_root = VNextCompositionRoot.from_manifest(restored_manifest)
    with restored_root.open_control_plane(clock=lambda: 100.0) as restored_plane:
        assert restored_plane.admission.authority.query(_request("n1:backup").submission_key, request_digest) == before_admission
        assert restored_plane.work_kernel.query("n1:backup-work").as_dict() == before_work.as_dict()
    assert verified.document["control_identity"]["migration_id"] == "n1-unified-control-v1"


@pytest.mark.parametrize(
    "tamper",
    ["missing_db", "missing_blob", "corrupt_blob", "truncated_db", "wrong_config", "wrong_identity", "foreign_target"],
)
def test_gate_d_negative_restore_cases_fail_closed(tmp_path: Path, tamper: str) -> None:
    root, runtime, legacy = _manifest(tmp_path)
    with root.open_control_plane() as plane:
        raw = b"n1-negative-content"
        ref = make_content_ref("text/plain", "n1-negative-v1", raw)
        plane.bindings.content_store.publish(ref, raw)
        authority = tmp_path / "authority"
        artifact = create_coordinated_backup(
            runtime,
            authority / "backups",
            backup_id="n1-negative",
            required_control_schema=1,
            source_is_quiesced=True,
            include_control_identity=True,
        )
        backup_path = artifact.path
    if tamper == "missing_db":
        case = tmp_path / "missing-db"
        shutil.copytree(backup_path, case)
        (case / "control" / "control.db").unlink()
        with pytest.raises(BootstrapError):
            verify_backup(case)
    elif tamper in {"missing_blob", "corrupt_blob"}:
        case = tmp_path / tamper
        shutil.copytree(backup_path, case)
        object_path = case / "content" / "objects" / (str(ref.raw_digest[7:]) + ".bin")
        if tamper == "missing_blob":
            object_path.unlink()
        else:
            object_path.write_bytes(b"wrong")
        with pytest.raises(BootstrapError):
            verify_backup(case)
    elif tamper == "truncated_db":
        case = tmp_path / tamper
        shutil.copytree(backup_path, case)
        database = case / "control" / "control.db"
        database.write_bytes(database.read_bytes()[:100])
        with pytest.raises(BootstrapError):
            verify_backup(case)
    elif tamper == "wrong_config":
        case = tmp_path / tamper
        shutil.copytree(backup_path, case)
        (case / "config" / "bdb-vnext.json").write_bytes(b"foreign-config\n")
        with pytest.raises(BootstrapError):
            verify_backup(case)
    elif tamper == "wrong_identity":
        case = tmp_path / "wrong-identity"
        shutil.copytree(backup_path, case)
        manifest_path = case / "backup-manifest.json"
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        document["control_identity"]["generation_id"] = "foreign-generation"
        manifest_path.write_bytes(json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        with pytest.raises(BootstrapError):
            verify_backup(case)
    else:
        with pytest.raises(BootstrapError) as failure:
            restore_backup(
                backup_path,
                runtime / "illegal-restore",
                authority_root=authority,
                legacy_runtime_root=legacy,
            )
        assert failure.value.code == "foreign_state_overlap"


def test_gate_d_v2_backup_requires_existing_control_db_without_creating_one(tmp_path: Path) -> None:
    root, runtime, _legacy = _manifest(tmp_path)
    runtime.mkdir(parents=True)
    with pytest.raises(BootstrapError) as failure:
        create_coordinated_backup(
            runtime,
            tmp_path / "authority" / "backups",
            backup_id="n1-no-control",
            required_control_schema=1,
            source_is_quiesced=True,
            include_control_identity=True,
        )
    assert failure.value.code == "control_identity_unavailable"
    assert not (runtime / "control" / "control.db").exists()


def test_gate_b_negative_scan_keeps_old_milestone_paths_retired() -> None:
    source = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "bdb_vnext").glob("*.py"))
    assert "m3a-shadow.db" not in source
    assert "m4a-work-kernel.db" not in source
    assert "sqlite3.connect" in source  # repositories remain explicit, typed writers


def test_gate_c_maps_known_legacy_terminal_rows_and_rejects_ambiguous_cancelled(tmp_path: Path) -> None:
    root, runtime, legacy = _manifest(tmp_path)
    with root.open_control_plane() as plane:
        accepted = plane.admission.authority.admit(_request("n1:migrate"))
        assert accepted.task_id
        connection = plane.bindings._connection
        plane.work_kernel.close()
        for table in ("m4a_transition_facts", "m4a_resource_claims", "m4a_leases", "m4a_waits", "m4a_runs", "m4a_work_items", "m4a_sequence"):
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        connection.executescript(
            """
            CREATE TABLE m4a_work_items (
                work_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, kind TEXT NOT NULL,
                disposition TEXT NOT NULL CHECK(disposition IN ('READY','RUNNING','WAITING','TERMINAL','CANCELLED')),
                state_version INTEGER NOT NULL, created_order INTEGER NOT NULL, updated_order INTEGER NOT NULL,
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE TABLE m4a_runs (
                run_id TEXT PRIMARY KEY, work_id TEXT NOT NULL, status TEXT NOT NULL,
                outcome TEXT, effect_certainty TEXT NOT NULL, lease_id TEXT NOT NULL,
                fence INTEGER NOT NULL, started_order INTEGER NOT NULL, ended_order INTEGER
            );
            """
        )
        connection.execute(
            "INSERT INTO m4a_work_items VALUES (?, ?, 'inspect', 'TERMINAL', 2, 1, 2, 1.0, 2.0)",
            ("n1:legacy-terminal", accepted.task_id),
        )
        connection.execute(
            "INSERT INTO m4a_runs VALUES ('n1:legacy-run', 'n1:legacy-terminal', 'FINISHED', 'FAILED', 'POSSIBLE', 'lease', 1, 1, 2)"
        )
        connection.commit()
        migrated = WorkKernelStore.open(runtime, task_authority=plane.admission.authority, legacy_root=legacy)
        try:
            query = migrated.query("n1:legacy-terminal")
            assert query is not None
            assert query.work.disposition == "FINISHED"
            assert query.last_run is not None and query.last_run.outcome == "FAILED"
        finally:
            migrated.close()

    root2, runtime2, legacy2 = _manifest(tmp_path / "ambiguous")
    with root2.open_control_plane() as plane2:
        accepted2 = plane2.admission.authority.admit(_request("n1:ambiguous"))
        assert accepted2.task_id
        connection = plane2.bindings._connection
        plane2.work_kernel.close()
        for table in ("m4a_transition_facts", "m4a_resource_claims", "m4a_leases", "m4a_waits", "m4a_runs", "m4a_work_items", "m4a_sequence"):
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        connection.executescript(
            """
            CREATE TABLE m4a_work_items (
                work_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, kind TEXT NOT NULL,
                disposition TEXT NOT NULL CHECK(disposition IN ('READY','RUNNING','WAITING','TERMINAL','CANCELLED')),
                state_version INTEGER NOT NULL, created_order INTEGER NOT NULL, updated_order INTEGER NOT NULL,
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO m4a_work_items VALUES (?, ?, 'inspect', 'CANCELLED', 1, 1, 1, 1.0, 1.0)",
            ("n1:legacy-cancelled", accepted2.task_id),
        )
        connection.commit()
        with pytest.raises(M4aError) as failure:
            WorkKernelStore.open(runtime2, task_authority=plane2.admission.authority, legacy_root=legacy2)
        assert failure.value.code == "lifecycle_migration_ambiguity"

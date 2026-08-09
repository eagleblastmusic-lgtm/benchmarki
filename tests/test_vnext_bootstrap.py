from __future__ import annotations

import errno
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from bdb_vnext.bootstrap import (
    BACKUP_SCHEMA,
    BUNDLE_SCHEMA,
    HEALTH_SCHEMA,
    RESULT_SCHEMA,
    BootstrapError,
    BootstrapLock,
    BootstrapRequest,
    create_coordinated_backup,
    execute_bootstrap,
    inspect_runtime_bundle,
    restore_backup,
    verify_backup,
)
from bdb_vnext.composition import RUNTIME_ID, observe_bundle


COMMIT = "a" * 40
OTHER_COMMIT = "b" * 40


@dataclass(frozen=True)
class BootstrapFixture:
    request: BootstrapRequest
    candidate: Path
    recovery: Path
    runtime: Path
    authority: Path
    legacy: Path
    restore: Path


def _health_source(bundle_id: str, mode: str) -> str:
    prelude = (
        "import json, sys, time\n"
        "schema = int(next(value.split('=', 1)[1] for value in sys.argv "
        "if value.startswith('--control-schema=')))\n"
    )
    if mode == "fail":
        return prelude + "raise SystemExit(7)\n"
    if mode == "timeout":
        return prelude + "time.sleep(5)\n"
    if mode == "flood":
        return prelude + "sys.stdout.write('x' * 70000)\n"
    observed_id = "wrong.bundle" if mode == "wrong_identity" else bundle_id
    payload = {
        "schema": HEALTH_SCHEMA,
        "status": "READY",
        "runtime_id": RUNTIME_ID,
        "bundle_id": observed_id,
    }
    return (
        prelude
        + f"payload = {payload!r}\n"
        + "payload['observed_control_schema'] = schema\n"
        + "print(json.dumps(payload, sort_keys=True, separators=(',', ':')))\n"
    )


def _write_bundle(
    root: Path,
    *,
    role: str,
    known_good: bool,
    schema_min: int = 0,
    schema_max: int = 2,
    health_mode: str = "ready",
    activation_allowed: bool = False,
    source_commit: str = COMMIT,
) -> None:
    root.mkdir(parents=True)
    bundle_id = f"bdb-vnext-{role}-fixture"
    (root / "health.py").write_text(
        _health_source(bundle_id, health_mode), encoding="utf-8", newline="\n"
    )
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


def _bundle_digest(root: Path, legacy: Path) -> str:
    value = observe_bundle(RUNTIME_ID, root, legacy_runtime_root=legacy)["sha256"]
    assert isinstance(value, str)
    return value


def _rewrite_bundle(root: Path, legacy: Path, update: callable) -> str:
    path = root / "bundle.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    update(manifest)
    path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return _bundle_digest(root, legacy)


def _write_sqlite_fixture(runtime: Path, *, include_wal: bool = True) -> None:
    control = runtime / "control"
    control.mkdir(parents=True, exist_ok=True)
    database = bytearray(512)
    database[:16] = b"SQLite format 3\0"
    database[16:18] = (512).to_bytes(2, "big")
    database[18] = 1
    database[19] = 1
    database[28:32] = (1).to_bytes(4, "big")
    (control / "control.db").write_bytes(database)
    if include_wal:
        wal = bytearray(32 + 24 + 512)
        wal[:4] = (0x377F0682).to_bytes(4, "big")
        wal[4:8] = (3_007_000).to_bytes(4, "big")
        wal[8:12] = (512).to_bytes(4, "big")
        (control / "control.db-wal").write_bytes(wal)


def _write_runtime(root: Path, *, resources: bool = True) -> None:
    root.mkdir(parents=True)
    if not resources:
        return
    _write_sqlite_fixture(root)
    (root / "content" / "nested").mkdir(parents=True)
    (root / "content" / "object.bin").write_bytes(b"exact-content-object")
    (root / "content" / "nested" / "other.bin").write_bytes(b"other")
    (root / "config").mkdir()
    (root / "config" / "bdb-vnext.json").write_text(
        '{"generation":"bdb-vnext-g1","writer_enabled":false}\n',
        encoding="utf-8",
        newline="\n",
    )


def _fixture(
    tmp_path: Path,
    *,
    candidate_health: str = "ready",
    recovery_health: str = "ready",
    candidate_range: tuple[int, int] = (0, 2),
    recovery_range: tuple[int, int] = (0, 2),
    resources: bool = True,
    attempt_id: str = "m1b-attempt",
) -> BootstrapFixture:
    legacy = tmp_path / "legacy-runtime"
    candidate = tmp_path / "candidate-bundle"
    recovery = tmp_path / "recovery-bundle"
    runtime = tmp_path / "vnext-runtime"
    authority = tmp_path / "bootstrap-authority"
    restored = tmp_path / "isolated-restore"
    _write_bundle(
        candidate,
        role="candidate",
        known_good=False,
        schema_min=candidate_range[0],
        schema_max=candidate_range[1],
        health_mode=candidate_health,
    )
    _write_bundle(
        recovery,
        role="recovery",
        known_good=True,
        schema_min=recovery_range[0],
        schema_max=recovery_range[1],
        health_mode=recovery_health,
        source_commit=OTHER_COMMIT,
    )
    _write_runtime(runtime, resources=resources)
    request = BootstrapRequest(
        authority_root=authority,
        runtime_root=runtime,
        legacy_runtime_root=legacy,
        candidate_bundle=candidate,
        candidate_expected_sha256=_bundle_digest(candidate, legacy),
        recovery_bundle=recovery,
        recovery_expected_sha256=_bundle_digest(recovery, legacy),
        recovery_target=restored,
        required_control_schema=1,
        source_is_quiesced=True,
        health_timeout_seconds=1.0,
        attempt_id=attempt_id,
    )
    return BootstrapFixture(request, candidate, recovery, runtime, authority, legacy, restored)


def test_ready_floor_binds_exact_bundles_backup_health_and_external_witness(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)

    result = execute_bootstrap(fixture.request)

    assert result.status == "READY"
    assert result.code == "ready"
    assert result.witness_path == fixture.authority / "attempts" / "m1b-attempt.json"
    assert result.witness_path.is_file()
    assert json.loads(result.witness_path.read_text(encoding="utf-8")) == result.document
    assert result.document["candidate"]["sha256"] == fixture.request.candidate_expected_sha256
    assert result.document["recovery"]["sha256"] == fixture.request.recovery_expected_sha256
    assert result.document["recovery"]["known_good"] is True
    assert result.document["health"]["candidate"]["status"] == "READY"
    assert result.document["health"]["recovery"]["status"] == "READY"
    assert result.document["authority"] == {
        "boundary": "external_bootstrap_root",
        "candidate_may_write_final_pointer": False,
        "final_activation_pointer": None,
        "production_activation_performed": False,
    }
    assert not (fixture.authority / "active-slot").exists()
    backup = verify_backup(Path(result.document["backup"]["path"]))
    assert backup.document["schema"] == BACKUP_SCHEMA
    assert backup.document["sqlite_pair"] == {
        "database_state": "present",
        "wal_state": "present",
        "page_size": 512,
    }


def test_real_isolated_restore_drill_preserves_exact_declared_resources(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    result = execute_bootstrap(fixture.request)
    drill_target = tmp_path / "drill-target"

    receipt = restore_backup(
        result.document["backup"]["path"],
        drill_target,
        authority_root=fixture.authority,
        legacy_runtime_root=fixture.legacy,
        forbidden_roots=(fixture.candidate, fixture.recovery),
    )

    assert receipt["verified"] is True
    assert receipt["backup_manifest_sha256"] == result.document["backup"]["manifest_sha256"]
    for relative in (
        "control/control.db",
        "control/control.db-wal",
        "content/object.bin",
        "content/nested/other.bin",
        "config/bdb-vnext.json",
    ):
        assert (drill_target / relative).read_bytes() == (fixture.runtime / relative).read_bytes()
    assert not fixture.restore.exists()


def test_declared_but_absent_pre_x1_resources_are_recorded_without_domain_schema(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, resources=False)

    result = execute_bootstrap(fixture.request)

    assert result.status == "READY"
    backup = verify_backup(result.document["backup"]["path"])
    assert [item["state"] for item in backup.document["subjects"]] == [
        "declared_absent",
        "declared_absent",
        "declared_absent",
        "declared_absent",
    ]
    assert backup.document["sqlite_pair"]["page_size"] is None


def test_candidate_health_failure_restores_only_known_good_compatible_bundle(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, candidate_health="fail")

    result = execute_bootstrap(fixture.request)

    assert result.status == "RECOVERED"
    assert result.code == "health_failed"
    assert result.document["restore"]["verified"] is True
    assert result.document["health"]["candidate"] is None
    assert result.document["health"]["recovery"]["bundle_id"] == "bdb-vnext-recovery-fixture"
    assert (fixture.restore / "content" / "object.bin").read_bytes() == b"exact-content-object"
    assert not (fixture.legacy / "control" / "control.db").exists()


@pytest.mark.parametrize("mode,expected_code", [("timeout", "health_timeout"), ("flood", "health_output_too_large"), ("wrong_identity", "health_identity_mismatch")])
def test_bounded_candidate_health_faults_recover_explicitly(
    tmp_path: Path, mode: str, expected_code: str
) -> None:
    fixture = _fixture(tmp_path, candidate_health=mode)
    request = replace(
        fixture.request, health_timeout_seconds=0.15 if mode == "timeout" else 1.0
    )

    result = execute_bootstrap(request)

    assert result.status == "RECOVERED"
    assert result.code == expected_code
    assert fixture.restore.is_dir()


@pytest.mark.parametrize("role", ["candidate", "recovery"])
def test_missing_bundle_is_blocked_without_fallback(tmp_path: Path, role: str) -> None:
    fixture = _fixture(tmp_path)
    missing = tmp_path / f"missing-{role}"
    digest = "sha256:" + "0" * 64
    request = (
        replace(fixture.request, candidate_bundle=missing, candidate_expected_sha256=digest)
        if role == "candidate"
        else replace(fixture.request, recovery_bundle=missing, recovery_expected_sha256=digest)
    )

    result = execute_bootstrap(request)

    assert result.status == "BLOCKED"
    assert result.code == f"{role}_bundle_missing"
    assert not fixture.restore.exists()


@pytest.mark.parametrize("role", ["candidate", "recovery"])
def test_corrupt_bundle_manifest_is_blocked(tmp_path: Path, role: str) -> None:
    fixture = _fixture(tmp_path)
    root = fixture.candidate if role == "candidate" else fixture.recovery
    (root / "bundle.json").write_text("{not-json\n", encoding="utf-8")
    digest = _bundle_digest(root, fixture.legacy)
    request = (
        replace(fixture.request, candidate_expected_sha256=digest)
        if role == "candidate"
        else replace(fixture.request, recovery_expected_sha256=digest)
    )

    result = execute_bootstrap(request)

    assert result.status == "BLOCKED"
    assert result.code == "invalid_json"


@pytest.mark.parametrize("role", ["candidate", "recovery"])
def test_bundle_digest_mismatch_is_blocked(tmp_path: Path, role: str) -> None:
    fixture = _fixture(tmp_path)
    bad = "sha256:" + "f" * 64
    request = (
        replace(fixture.request, candidate_expected_sha256=bad)
        if role == "candidate"
        else replace(fixture.request, recovery_expected_sha256=bad)
    )

    result = execute_bootstrap(request)

    assert result.status == "BLOCKED"
    assert result.code == "bundle_digest_mismatch"


@pytest.mark.parametrize(
    "candidate_range,recovery_range,expected_code",
    [((0, 0), (0, 2), "candidate_schema_unsupported"), ((0, 2), (0, 0), "recovery_schema_unsupported")],
)
def test_stale_schema_range_is_blocked(
    tmp_path: Path,
    candidate_range: tuple[int, int],
    recovery_range: tuple[int, int],
    expected_code: str,
) -> None:
    fixture = _fixture(
        tmp_path, candidate_range=candidate_range, recovery_range=recovery_range
    )

    result = execute_bootstrap(fixture.request)

    assert result.status == "BLOCKED"
    assert result.code == expected_code


def test_schema_incompatible_recovery_is_never_treated_as_binary_rollback(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, candidate_health="fail", recovery_range=(0, 0))

    result = execute_bootstrap(fixture.request)

    assert result.status == "BLOCKED"
    assert result.code == "recovery_schema_unsupported"
    assert not fixture.restore.exists()


def test_recovery_must_be_explicitly_known_good(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    digest = _rewrite_bundle(
        fixture.recovery, fixture.legacy, lambda manifest: manifest.update(known_good=False)
    )

    result = execute_bootstrap(
        replace(fixture.request, recovery_expected_sha256=digest)
    )

    assert result.status == "BLOCKED"
    assert result.code == "recovery_not_known_good"


def test_candidate_self_activation_request_is_blocked_before_health(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    marker = fixture.candidate / "health-ran"
    (fixture.candidate / "health.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
        encoding="utf-8",
    )

    def enable_activation(manifest: dict[str, object]) -> None:
        manifest["activation_policy"] = {"candidate_may_write_final_pointer": True}

    digest = _rewrite_bundle(fixture.candidate, fixture.legacy, enable_activation)

    result = execute_bootstrap(
        replace(fixture.request, candidate_expected_sha256=digest)
    )

    assert result.status == "BLOCKED"
    assert result.code == "candidate_self_activation_requested"
    assert not marker.exists()
    assert not (fixture.authority / "active_slot").exists()


def test_copy_crash_leaves_only_unpublished_partial_backup(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)

    def crash(source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes()[:7])
        raise RuntimeError("simulated crash")

    result = execute_bootstrap(fixture.request, backup_copy_file=crash)

    assert result.status == "BLOCKED"
    assert result.code == "backup_copy_failed"
    assert not (fixture.authority / "backups" / "m1b-attempt").exists()
    assert list((fixture.authority / "backups").glob(".m1b-attempt.partial-*"))


def test_disk_full_write_failure_is_explicit_and_never_publishes_backup(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)

    def disk_full(_source: Path, _destination: Path) -> None:
        raise OSError(errno.ENOSPC, "simulated disk full")

    result = execute_bootstrap(fixture.request, backup_copy_file=disk_full)

    assert result.status == "BLOCKED"
    assert result.code == "backup_write_failed"
    assert not (fixture.authority / "backups" / "m1b-attempt").exists()


def test_interruption_before_atomic_backup_publish_returns_blocked(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)

    def interrupt(_staging: Path) -> None:
        raise RuntimeError("simulated process interruption")

    result = execute_bootstrap(fixture.request, backup_before_publish=interrupt)

    assert result.status == "BLOCKED"
    assert result.code == "backup_publish_interrupted"
    assert not (fixture.authority / "backups" / "m1b-attempt").exists()


@pytest.mark.parametrize(
    "mutation,expected_code",
    [
        ("wal_without_db", "db_wal_mismatch"),
        ("incomplete_db", "incomplete_sqlite_db"),
        ("incomplete_wal", "incomplete_sqlite_wal"),
        ("page_mismatch", "db_wal_mismatch"),
    ],
)
def test_incomplete_or_mismatched_db_wal_fixture_is_rejected(
    tmp_path: Path, mutation: str, expected_code: str
) -> None:
    runtime = tmp_path / "runtime"
    _write_runtime(runtime)
    database = runtime / "control" / "control.db"
    wal = runtime / "control" / "control.db-wal"
    if mutation == "wal_without_db":
        database.unlink()
    elif mutation == "incomplete_db":
        database.write_bytes(database.read_bytes()[:-1])
    elif mutation == "incomplete_wal":
        wal.write_bytes(wal.read_bytes()[:-1])
    else:
        payload = bytearray(wal.read_bytes())
        payload[8:12] = (1_024).to_bytes(4, "big")
        wal.write_bytes(payload)

    with pytest.raises(BootstrapError) as raised:
        create_coordinated_backup(
            runtime,
            tmp_path / "authority" / "backups",
            backup_id="bad-sqlite",
            required_control_schema=1,
            source_is_quiesced=True,
        )

    assert raised.value.code == expected_code


def test_non_quiesced_source_is_blocked_before_copy(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)

    result = execute_bootstrap(replace(fixture.request, source_is_quiesced=False))

    assert result.status == "BLOCKED"
    assert result.code == "source_not_quiesced"


def test_restore_integrity_failure_is_blocked_and_uncertain_target_is_not_deleted(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    ready = execute_bootstrap(fixture.request)
    target = tmp_path / "corrupt-restore"

    def corrupt(restored: Path) -> None:
        (restored / "content" / "object.bin").write_bytes(b"tampered")

    with pytest.raises(BootstrapError) as raised:
        restore_backup(
            ready.document["backup"]["path"],
            target,
            authority_root=fixture.authority,
            legacy_runtime_root=fixture.legacy,
            forbidden_roots=(fixture.candidate, fixture.recovery),
            after_publish=corrupt,
        )

    assert raised.value.code == "restore_integrity_failure"
    assert target.exists()
    assert (target / "content" / "object.bin").read_bytes() == b"tampered"


def test_recovery_restore_integrity_failure_returns_durable_blocked_result(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, candidate_health="fail")

    def corrupt(restored: Path) -> None:
        (restored / "content" / "object.bin").write_bytes(b"tampered")

    result = execute_bootstrap(fixture.request, restore_after_publish=corrupt)

    assert result.status == "BLOCKED"
    assert result.code == "restore_integrity_failure"
    assert result.witness_path is not None and result.witness_path.is_file()
    assert fixture.restore.exists()


def test_backup_verification_rejects_tampered_bytes_and_manifest_digest(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    ready = execute_bootstrap(fixture.request)
    original = Path(ready.document["backup"]["path"])
    bytes_copy = tmp_path / "bytes-tamper" / original.name
    manifest_copy = tmp_path / "manifest-tamper" / original.name
    shutil.copytree(original, bytes_copy)
    shutil.copytree(original, manifest_copy)
    (bytes_copy / "content" / "object.bin").write_bytes(b"tampered")
    manifest_path = manifest_copy / "backup-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["manifest_sha256"] = "sha256:" + "0" * 64
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")

    with pytest.raises(BootstrapError) as bytes_error:
        verify_backup(bytes_copy)
    with pytest.raises(BootstrapError) as manifest_error:
        verify_backup(manifest_copy)

    assert bytes_error.value.code == "backup_integrity_failure"
    assert manifest_error.value.code == "backup_manifest_digest_mismatch"


def test_concurrent_bootstrap_attempt_is_bounded_and_does_not_write_a_witness(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    lock = fixture.authority / "bootstrap.lock"

    with BootstrapLock(lock):
        result = execute_bootstrap(fixture.request)

    assert result.status == "BLOCKED"
    assert result.code == "concurrent_attempt"
    assert result.witness_path is None
    assert not (fixture.authority / "attempts" / "m1b-attempt.json").exists()


def test_legacy_runtime_overlap_is_blocked_without_reading_or_writing_legacy(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)

    result = execute_bootstrap(
        replace(fixture.request, runtime_root=fixture.legacy / "vnext")
    )

    assert result.status == "BLOCKED"
    assert result.code == "legacy_overlap"
    assert not fixture.legacy.exists()


def test_bundle_inside_legacy_boundary_is_blocked(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    legacy_candidate = fixture.legacy / "candidate"
    _write_bundle(legacy_candidate, role="candidate", known_good=False)
    digest = _bundle_digest(legacy_candidate, tmp_path / "different-legacy")

    result = execute_bootstrap(
        replace(
            fixture.request,
            candidate_bundle=legacy_candidate,
            candidate_expected_sha256=digest,
        )
    )

    assert result.status == "BLOCKED"
    assert result.code == "legacy_overlap"


def test_restore_refuses_existing_or_overlapping_target(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    ready = execute_bootstrap(fixture.request)
    backup = ready.document["backup"]["path"]

    with pytest.raises(BootstrapError) as overlap:
        restore_backup(
            backup,
            fixture.legacy / "restore",
            authority_root=fixture.authority,
            legacy_runtime_root=fixture.legacy,
            forbidden_roots=(fixture.candidate, fixture.recovery),
        )
    assert overlap.value.code == "legacy_overlap"

    existing = tmp_path / "existing-target"
    existing.mkdir()
    with pytest.raises(BootstrapError) as exists:
        restore_backup(
            backup,
            existing,
            authority_root=fixture.authority,
            legacy_runtime_root=fixture.legacy,
            forbidden_roots=(fixture.candidate, fixture.recovery),
        )
    assert exists.value.code == "restore_target_exists"


def test_bundle_inspection_rejects_wrong_role_and_moving_digest(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)

    with pytest.raises(BootstrapError) as role:
        inspect_runtime_bundle(
            fixture.candidate,
            expected_role="recovery",
            expected_sha256=fixture.request.candidate_expected_sha256,
            legacy_runtime_root=fixture.legacy,
        )
    assert role.value.code == "bundle_role_mismatch"

    (fixture.candidate / "foreign.bin").write_bytes(b"changed")
    with pytest.raises(BootstrapError) as digest:
        inspect_runtime_bundle(
            fixture.candidate,
            expected_role="candidate",
            expected_sha256=fixture.request.candidate_expected_sha256,
            legacy_runtime_root=fixture.legacy,
        )
    assert digest.value.code == "bundle_digest_mismatch"


def test_schema_files_parse_and_cli_entrypoint_is_external(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    names = (
        "bdb-vnext-runtime-bundle-v1.schema.json",
        "bdb-vnext-backup-manifest-v1.schema.json",
        "bdb-vnext-bootstrap-result-v1.schema.json",
    )
    for name in names:
        schema = json.loads((root / "schemas" / name).read_text(encoding="utf-8"))
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["additionalProperties"] is False
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert 'bdb-vnext-bootstrap = "bdb_vnext.bootstrap:main"' in pyproject


def test_vnext_bootstrap_import_has_no_legacy_runtime_or_openai_dependency() -> None:
    root = Path(__file__).resolve().parents[1]
    command = (
        "import sys; "
        f"sys.path.insert(0, {str(root)!r}); "
        "import bdb_vnext.bootstrap; "
        "bad=[name for name in sys.modules if name.startswith(('bdb_bridge','bdb_release','openai'))]; "
        "assert not bad, bad"
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-c", command],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_module_has_no_activation_pointer_writer_contract() -> None:
    source = (Path(__file__).resolve().parents[1] / "bdb_vnext" / "bootstrap.py").read_text(
        encoding="utf-8"
    )
    assert "active_slot" not in source
    assert "previous_slot" not in source
    assert "production_activation_performed\": False" in source
    assert "candidate_may_write_final_pointer\": False" in source
    assert "bdb_bridge" not in source


@pytest.mark.skipif(os.name != "nt", reason="Windows lock semantics")
def test_windows_external_lock_is_exclusive_across_processes(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    lock = tmp_path / "authority" / "bootstrap.lock"
    child = (
        "import sys; "
        f"sys.path.insert(0, {str(root)!r}); "
        "from bdb_vnext.bootstrap import BootstrapError,BootstrapLock; "
        f"path={str(lock)!r}; "
        "\ntry:\n"
        "  with BootstrapLock(path): pass\n"
        "except BootstrapError as exc:\n"
        "  print(exc.code)\n"
        "  raise SystemExit(23)\n"
    )
    with BootstrapLock(lock):
        completed = subprocess.run(
            [sys.executable, "-I", "-c", child],
            cwd=root,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    assert completed.returncode == 23
    assert completed.stdout.strip() == "concurrent_attempt"


@pytest.mark.skipif(os.name != "nt", reason="Windows atomic filesystem drill")
def test_windows_backup_and_restore_publish_only_verified_final_directories(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, attempt_id="windows-drill")

    result = execute_bootstrap(fixture.request)
    drill = tmp_path / "windows-restored"
    receipt = restore_backup(
        result.document["backup"]["path"],
        drill,
        authority_root=fixture.authority,
        legacy_runtime_root=fixture.legacy,
        forbidden_roots=(fixture.candidate, fixture.recovery),
    )

    assert result.status == "READY"
    assert receipt["verified"] is True
    assert not list((fixture.authority / "backups").glob(".windows-drill.partial-*"))
    assert not list(tmp_path.glob(".windows-restored.restore-partial-*"))

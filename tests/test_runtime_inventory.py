from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from bdb_bridge import Journal
from bdb_bridge import runtime_inventory as inventory
from bdb_bridge.runtime_inventory import (
    InventoryProvider,
    InventoryRequest,
    OverallResult,
    OutputFailure,
    SourceStatus,
    atomic_write_report,
    canonical_json_bytes,
    main,
    sanitize_report,
    semantic_digest,
    semantic_payload,
)


ROOT = Path(__file__).resolve().parents[1]


class Fixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.repository = root / "repository"
        self.profile = root / "profile"
        self.runtime = root / "runtime"
        self.control = root / "control"
        self.worktrees = root / "worktrees"
        self.scratch = root / "scratch"
        self.reports = root / "reports"
        self.bridge_config = self.profile / "bridge-config.json"
        self.native_config = self.profile / "native-host.json"
        self.receipts = self.profile / "native-host-requests.json"
        self.spool = self.runtime / "direct_spool" / "inbox"
        self.results = self.runtime / "direct_spool" / "results"
        self.promotions = self.runtime / "promotions"
        self.promoter_state = self.runtime / "workspace-promoter-state.json"
        self.journal = self.runtime / "journal.db"
        self.deployed_bundle = root / "deployed-browser"

        for path in (
            self.repository,
            self.profile,
            self.runtime,
            self.control,
            self.worktrees,
            self.scratch,
            self.reports,
            self.spool,
            self.results,
            self.promotions,
        ):
            path.mkdir(parents=True, exist_ok=True)

        self._write_repository()
        self._write_configuration()
        self._write_stores()

    def _write_repository(self) -> None:
        extension = self.repository / "browser_extension"
        extension.mkdir()
        _json(
            extension / "manifest.json",
            {
                "manifest_version": 3,
                "name": "Bartosz Dev Bridge",
                "version": "0.4.7",
                "background": {"service_worker": "background.js"},
            },
        )
        (extension / "background.js").write_text("const BDB = true;\n", encoding="utf-8")
        manifests = self.repository / "manifests"
        manifests.mkdir()
        _json(
            manifests / "bartosz-dev-bridge.module.json",
            {
                "schema": "bartosz-os-module-manifest-v1",
                "module_id": "devmaster.bartosz-dev-bridge",
                "version": "0.3.1",
            },
        )
        bridge = self.repository / "bdb_bridge"
        bridge.mkdir()
        shutil.copy2(ROOT / "bdb_bridge" / "runtime_version.py", bridge / "runtime_version.py")
        _git(self.repository, "init")
        _git(self.repository, "config", "user.name", "R0a Test")
        _git(self.repository, "config", "user.email", "r0a@example.invalid")
        _git(self.repository, "add", ".")
        _git(self.repository, "commit", "-m", "fixture")
        shutil.copytree(extension, self.deployed_bundle)

    def _write_configuration(self) -> None:
        _json(
            self.bridge_config,
            {
                "schema_version": "1.1",
                "control_repo_path": str(self.control),
                "fixture_repo_path": str(self.repository),
                "worktree_root": str(self.worktrees),
                "runtime_dir": str(self.runtime),
                "journal_path": str(self.journal),
                "direct_spool_dir": str(self.spool),
                "direct_result_dir": str(self.results),
                "repository_id": "r0a-fixture",
                "allowed_paths": ["src/example.py"],
                "workspace_mode": "isolated_worktree",
            },
        )
        _json(
            self.native_config,
            {
                "schema": "bdb-native-host-config-v1",
                "repositories": {"r0a": {"bridge_config_path": str(self.bridge_config)}},
                "allowed_origins": ["chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/"],
                "request_store_path": str(self.receipts),
            },
        )

    def _write_stores(self) -> None:
        journal = Journal.open(self.journal)
        journal.close()
        _json(
            self.receipts,
            {
                "schema": "bdb-native-request-receipts-v1",
                "requests": {},
                "submission_reservations": {},
            },
        )
        _json(
            self.promoter_state,
            {
                "schema": "bdb-workspace-promoter-state-v1",
                "initialized": True,
                "seen": {},
            },
        )

    def request(self, **updates: Any) -> InventoryRequest:
        values: dict[str, Any] = {
            "repository_path": self.repository,
            "bridge_config_path": self.bridge_config,
            "native_config_path": self.native_config,
            "browser_bundle_path": self.deployed_bundle,
            "scratch_dir": self.scratch,
            "max_records": 100,
            "max_file_bytes": 2 * 1024 * 1024,
            "max_total_bytes": 64 * 1024 * 1024,
            "timeout_seconds": 5.0,
        }
        values.update(updates)
        return InventoryRequest(**values)

    def collect(self, **updates: Any) -> dict[str, Any]:
        return InventoryProvider().collect(self.request(**updates))


@pytest.fixture
def r0a(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path)


def _json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _source(report: dict[str, Any], name: str) -> dict[str, Any]:
    return next(item for item in report["sources"] if item["name"] == name)


def _tree_fingerprint(root: Path) -> dict[str, tuple[int, int, str]]:
    result: dict[str, tuple[int, int, str]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            content = path.read_bytes()
            info = path.stat()
            result[path.relative_to(root).as_posix()] = (
                len(content),
                info.st_mtime_ns,
                hashlib.sha256(content).hexdigest(),
            )
    return result


def _reservation(command_id: str = "session-a:000001", filename: str = "session-a-000001.json") -> dict[str, Any]:
    return {
        "command_id": command_id,
        "action_sha256": "sha256:" + "a" * 64,
        "repo_alias": "r0a",
        "session_id": command_id.split(":", 1)[0],
        "sequence": 1,
        "filename": filename,
        "created_at": "2026-08-09T00:00:00Z",
    }


def _spool_envelope(command_id: str = "session-a:000001", nonce: str = "nonce-a") -> dict[str, Any]:
    session, sequence = command_id.split(":")
    return {
        "schema": "bdb-local-envelope-v1",
        "submitted_at": "2026-08-09T00:00:00Z",
        "manifest": {"session_id": session, "repository_id": "r0a-fixture"},
        "command": {
            "session_id": session,
            "sequence": int(sequence),
            "command_id": command_id,
            "client_submission_nonce": nonce,
        },
    }


def test_clean_inventory_is_versioned_complete_deterministic_and_not_safe(r0a: Fixture) -> None:
    first = r0a.collect()
    second = r0a.collect()

    assert first["schema"] == "runtime-inventory-v1"
    assert first["provider"] == {"name": "bdb-runtime-inventory", "version": "1.0"}
    assert first["overall"]["result"] == OverallResult.READY_FOR_LOCAL_GATE
    assert first["overall"]["safe_to_mutate"] is False
    assert all(source["status"] in {status.value for status in SourceStatus} for source in first["sources"])
    assert first["semantic_digest"] == second["semantic_digest"]
    assert canonical_json_bytes(semantic_payload(first)) == canonical_json_bytes(semantic_payload(second))

    changed = deepcopy(first)
    changed["inventory_id"] = "inv-" + "f" * 32
    changed["observation"]["started_at"] = "2099-01-01T00:00:00Z"
    changed["observation"]["duration_ms"] = 999999
    for source in changed["sources"]:
        source["observation"]["finished_at"] = "2099-01-01T00:00:01Z"
        source["observation"]["duration_ms"] = 1234
    journal_files = _source(changed, "journal")["facts"]["source_files"]
    journal_files["database"]["pre_identity"]["mtime_ns"] = 1
    journal_files["database"]["post_identity"]["ctime_ns"] = 2
    assert semantic_digest(changed) == first["semantic_digest"]
    changed["observation"]["platform"] = "different-platform"
    assert semantic_digest(changed) != first["semantic_digest"]


def test_bom_and_crlf_configs_and_manifests_are_supported(r0a: Fixture) -> None:
    paths = (
        r0a.bridge_config,
        r0a.native_config,
        r0a.repository / "browser_extension" / "manifest.json",
        r0a.deployed_bundle / "manifest.json",
    )
    for path in paths:
        document = json.loads(path.read_text(encoding="utf-8"))
        rendered = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2).replace("\n", "\r\n") + "\r\n"
        path.write_text("\ufeff" + rendered, encoding="utf-8", newline="")
    _git(r0a.repository, "add", ".")
    _git(r0a.repository, "commit", "-m", "bom crlf manifest")

    report = r0a.collect()

    assert _source(report, "bridge_config")["status"] == SourceStatus.OBSERVED
    assert _source(report, "native_config")["status"] == SourceStatus.OBSERVED
    assert _source(report, "repository_browser_bundle")["status"] == SourceStatus.OBSERVED
    assert report["overall"]["result"] == OverallResult.READY_FOR_LOCAL_GATE


@pytest.mark.parametrize(
    ("mutation", "expected_status", "expected_result", "error_code"),
    [
        ("missing", SourceStatus.UNAVAILABLE, OverallResult.INCOMPLETE, "missing"),
        ("parse", SourceStatus.INVALID, OverallResult.INVALID, "invalid_json"),
        ("unsupported", SourceStatus.UNSUPPORTED, OverallResult.UNSUPPORTED, "unsupported_schema"),
    ],
)
def test_missing_parse_and_unsupported_receipts_never_mean_ready(
    r0a: Fixture,
    mutation: str,
    expected_status: SourceStatus,
    expected_result: OverallResult,
    error_code: str,
) -> None:
    if mutation == "missing":
        r0a.receipts.unlink()
    elif mutation == "parse":
        r0a.receipts.write_text("{broken", encoding="utf-8")
    else:
        _json(r0a.receipts, {"schema": "bdb-native-request-receipts-v2", "requests": {}, "submission_reservations": {}})

    report = r0a.collect()
    source = _source(report, "receipts")
    assert source["status"] == expected_status
    assert source["complete"] is False
    assert source["errors"][0]["code"] == error_code
    assert report["overall"]["result"] == expected_result


def test_permission_and_sqlite_busy_are_typed_and_fail_closed(r0a: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(label: str, phase: str) -> None:
        if label == "receipt_store" and phase == "after_read":
            raise PermissionError(errno.EACCES, "denied")

    denied = InventoryProvider(fault_hook=deny).collect(r0a.request())
    assert _source(denied, "receipts")["status"] == SourceStatus.UNAVAILABLE
    assert _source(denied, "receipts")["errors"][0]["code"] == "permission_denied"
    assert denied["overall"]["result"] == OverallResult.INCOMPLETE

    original_connect = inventory.sqlite3.connect

    def busy(database: Any, *args: Any, **kwargs: Any):
        if "bdb-r0a-journal-copy" in str(database):
            raise sqlite3.OperationalError("database is locked")
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(inventory.sqlite3, "connect", busy)
    locked = r0a.collect()
    assert _source(locked, "journal")["status"] == SourceStatus.UNAVAILABLE
    assert _source(locked, "journal")["errors"][0]["code"] == "sqlite_busy"
    assert locked["overall"]["result"] == OverallResult.INCOMPLETE


def test_source_identity_change_and_wal_appearance_are_unstable(r0a: Fixture) -> None:
    def change_receipt(label: str, phase: str) -> None:
        if label == "receipt_store" and phase == "after_read":
            r0a.receipts.write_text(r0a.receipts.read_text(encoding="utf-8") + " ", encoding="utf-8")

    receipt_report = InventoryProvider(fault_hook=change_receipt).collect(r0a.request())
    assert _source(receipt_report, "receipts")["status"] == SourceStatus.UNSTABLE
    assert receipt_report["overall"]["result"] == OverallResult.INCOMPLETE

    r0a._write_stores()

    def add_wal(label: str, phase: str) -> None:
        if label == "journal" and phase == "after_snapshot":
            r0a.journal.with_name(r0a.journal.name + "-wal").write_bytes(b"late-wal")

    wal_report = InventoryProvider(fault_hook=add_wal).collect(r0a.request())
    assert _source(wal_report, "journal")["status"] == SourceStatus.UNSTABLE
    assert _source(wal_report, "journal")["errors"][0]["code"] == "identity_changed"


def test_source_disappearance_is_unstable(r0a: Fixture) -> None:
    def remove_receipt(label: str, phase: str) -> None:
        if label == "receipt_store" and phase == "after_read":
            r0a.receipts.rename(r0a.receipts.with_suffix(".gone"))

    report = InventoryProvider(fault_hook=remove_receipt).collect(r0a.request())

    assert _source(report, "receipts")["status"] == SourceStatus.UNSTABLE
    assert _source(report, "receipts")["errors"][0]["code"] == "source_disappeared"
    assert report["overall"]["result"] == OverallResult.INCOMPLETE


def test_directory_membership_changes_are_unstable(r0a: Fixture) -> None:
    def change_spool(label: str, phase: str) -> None:
        if label == "spool" and phase == "after_scan":
            _json(r0a.spool / "late.json", _spool_envelope())

    spool_report = InventoryProvider(fault_hook=change_spool).collect(r0a.request())
    assert _source(spool_report, "spool")["status"] == SourceStatus.UNSTABLE
    assert _source(spool_report, "spool")["errors"][0]["code"] == "identity_changed"

    (r0a.spool / "late.json").unlink()

    def change_bundle(label: str, phase: str) -> None:
        if label == "repository_browser_bundle" and phase == "after_tree_scan":
            (r0a.repository / "browser_extension" / "late.js").write_text("late\n", encoding="utf-8")

    bundle_report = InventoryProvider(fault_hook=change_bundle).collect(r0a.request())
    assert _source(bundle_report, "repository_browser_bundle")["status"] == SourceStatus.UNSTABLE
    assert _source(bundle_report, "repository_browser_bundle")["errors"][0]["code"] == "identity_changed"


def test_pid_reuse_or_disappearance_is_unstable(r0a: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    connection = sqlite3.connect(r0a.journal)
    connection.execute(
        "INSERT INTO service_instances VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            "service-r0a",
            4242,
            "running",
            "2026-08-09T00:00:00Z",
            "2026-08-09T00:00:00Z",
            None,
            None,
            None,
            None,
            "2026-08-09T00:00:00Z",
            "2026-08-09T00:00:00Z",
        ),
    )
    connection.commit()
    connection.close()
    identities = iter(
        (
            {"alive": True, "creation_token": "100"},
            {"alive": True, "creation_token": "200"},
        )
    )
    monkeypatch.setattr(inventory, "_pid_identity", lambda pid: next(identities))

    report = r0a.collect()

    assert _source(report, "journal")["status"] == SourceStatus.UNSTABLE
    assert _source(report, "journal")["errors"][0]["code"] == "pid_reused"
    assert report["overall"]["result"] == OverallResult.INCOMPLETE


def test_stable_active_writer_is_a_finding_not_process_control(r0a: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    connection = sqlite3.connect(r0a.journal)
    connection.execute(
        "INSERT INTO service_instances VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            "service-r0a",
            os.getpid(),
            "running",
            "2026-08-09T00:00:00Z",
            "2026-08-09T00:00:00Z",
            None,
            None,
            None,
            None,
            "2026-08-09T00:00:00Z",
            "2026-08-09T00:00:00Z",
        ),
    )
    connection.commit()
    connection.close()
    identity = {"alive": True, "creation_token": "stable"}
    monkeypatch.setattr(inventory, "_pid_identity", lambda pid: identity)

    report = r0a.collect()

    assert report["overall"]["result"] == OverallResult.READY_FOR_LOCAL_GATE
    assert report["correlations"]["findings"] == [
        {"code": "active_writer_candidates_observed", "count": 1}
    ]


def test_current_process_identity_is_observable_on_windows() -> None:
    identity = inventory._pid_identity(os.getpid())

    assert identity["alive"] is True
    if sys.platform == "win32":
        assert identity["creation_token"] is not None


def test_corrupt_and_future_journals_are_invalid_or_unsupported(r0a: Fixture) -> None:
    r0a.journal.write_bytes(b"not sqlite")
    corrupt = r0a.collect()
    assert _source(corrupt, "journal")["status"] == SourceStatus.INVALID
    assert corrupt["overall"]["result"] == OverallResult.INVALID

    r0a.journal.unlink()
    journal = Journal.open(r0a.journal)
    journal.close()
    connection = sqlite3.connect(r0a.journal)
    connection.execute(
        "INSERT INTO schema_migrations(version,name,checksum,applied_at) VALUES(13,'future','x','2026-08-09T00:00:00Z')"
    )
    connection.commit()
    connection.close()
    future = r0a.collect()
    assert _source(future, "journal")["status"] == SourceStatus.UNSUPPORTED
    assert future["overall"]["result"] == OverallResult.UNSUPPORTED


def test_wal_fixture_is_observed_from_copy_without_touching_source(r0a: Fixture) -> None:
    connection = sqlite3.connect(r0a.journal)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        "INSERT INTO events(session_id,command_id,event_type,payload_json,created_at) VALUES(NULL,NULL,'r0a.test',NULL,'2026-08-09T00:00:00Z')"
    )
    connection.commit()
    before = _tree_fingerprint(r0a.runtime)
    try:
        report = r0a.collect()
        assert _tree_fingerprint(r0a.runtime) == before
    finally:
        connection.close()
    journal = _source(report, "journal")
    assert journal["status"] == SourceStatus.OBSERVED
    assert journal["facts"]["wal_present"] is True
    assert journal["facts"]["source_open_mode"] == "byte-copy-only"
    assert journal["facts"]["copy_query_mode"] == "mode=ro+query_only"
    assert not list(r0a.scratch.iterdir())


def test_caps_and_duplicate_spool_identity_are_explicit(r0a: Fixture) -> None:
    for index in range(1, 4):
        command_id = f"session-a:{index:06d}"
        _json(r0a.spool / f"session-a-{index:06d}.json", _spool_envelope(command_id, f"nonce-{index}"))
    capped = r0a.collect(max_records=2)
    assert _source(capped, "spool")["truncated"] is True
    assert _source(capped, "spool")["complete"] is False
    assert capped["overall"]["result"] == OverallResult.INCOMPLETE

    shutil.rmtree(r0a.spool)
    r0a.spool.mkdir()
    _json(r0a.spool / "first.json", _spool_envelope("session-a:000001", "nonce-a"))
    _json(r0a.spool / "second.json", _spool_envelope("session-a:000001", "nonce-b"))
    duplicate = r0a.collect()
    assert _source(duplicate, "spool")["status"] == SourceStatus.INVALID
    assert _source(duplicate, "spool")["errors"][0]["code"] == "duplicate_identity"


def test_receipt_spool_ghosts_are_bounded_blockers(r0a: Fixture) -> None:
    document = json.loads(r0a.receipts.read_text(encoding="utf-8"))
    document["submission_reservations"]["nonce-a"] = _reservation()
    _json(r0a.receipts, document)
    receipt_ghost = r0a.collect()
    assert {item["code"] for item in receipt_ghost["overall"]["blockers"]} == {"receipt_without_spool"}
    assert receipt_ghost["overall"]["result"] == OverallResult.INCOMPLETE

    _json(r0a.receipts, {"schema": "bdb-native-request-receipts-v1", "requests": {}, "submission_reservations": {}})
    _json(r0a.spool / "session-a-000001.json", _spool_envelope())
    spool_ghost = r0a.collect()
    assert {item["code"] for item in spool_ghost["overall"]["blockers"]} == {"spool_without_receipt"}
    assert spool_ghost["overall"]["result"] == OverallResult.INCOMPLETE


def test_promoter_ref_and_bundle_repository_disagreement_block(r0a: Fixture) -> None:
    _json(
        r0a.promotions / "session-a-000001.json",
        {
            "schema": "bdb-workspace-promotion-v1",
            "session_id": "session-a",
            "sequence": 1,
            "status": "promoted",
            "source_commit": "b" * 40,
            "parent_commit": "a" * 40,
            "changed_files": ["src/example.py"],
            "result_sha256": "sha256:" + "c" * 64,
            "repository_event_seq": 1,
        },
    )
    _json(
        r0a.promotions / ".repository-event-seq.json",
        {"schema": "bdb-repository-event-seq-v1", "repository_event_seq": 1},
    )
    (r0a.deployed_bundle / "background.js").write_text("const BDB = false;\n", encoding="utf-8")
    report = r0a.collect()
    blockers = {item["code"] for item in report["overall"]["blockers"]}
    assert blockers == {"promoter_ref_disagreement", "bundle_repository_mismatch"}
    assert report["overall"]["result"] == OverallResult.INCOMPLETE


def test_repository_bundle_runtime_version_mismatch_blocks(r0a: Fixture) -> None:
    manifest_path = r0a.repository / "browser_extension" / "manifest.json"
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    document["version"] = "9.9.9"
    _json(manifest_path, document)
    _git(r0a.repository, "add", ".")
    _git(r0a.repository, "commit", "-m", "version mismatch")
    report = r0a.collect()
    assert "runtime_browser_version_mismatch" in {item["code"] for item in report["overall"]["blockers"]}


def test_native_bridge_binding_mismatch_blocks(r0a: Fixture) -> None:
    document = json.loads(r0a.native_config.read_text(encoding="utf-8"))
    document["repositories"] = {"other": {"bridge_config_path": str(r0a.profile / "other-bridge.json")}}
    _json(r0a.native_config, document)

    report = r0a.collect()

    assert "native_bridge_binding_mismatch" in {item["code"] for item in report["overall"]["blockers"]}
    assert report["overall"]["result"] == OverallResult.INCOMPLETE


def test_sanitized_export_redacts_paths_ids_secrets_without_mutating_private(r0a: Fixture) -> None:
    document = json.loads(r0a.receipts.read_text(encoding="utf-8"))
    document["requests"]["request-sensitive"] = _reservation()
    _json(r0a.receipts, document)
    private = r0a.collect()
    private_before = deepcopy(private)
    private["sources"][0]["facts"]["api_token"] = "very-secret"
    sanitized = sanitize_report(private)

    assert private["sources"][0]["facts"]["api_token"] == "very-secret"
    assert sanitized["representation"] == "SANITIZED"
    serialized = canonical_json_bytes(sanitized).decode("utf-8")
    repository_head = _source(private, "repository")["identity"]["head"]
    assert str(r0a.root) not in serialized
    assert "request-sensitive" not in serialized
    assert "session-a-000001.json" not in serialized
    assert repository_head not in serialized
    assert "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/" not in serialized
    assert "very-secret" not in serialized
    assert sanitized["semantic_digest"] == private["semantic_digest"]
    assert private_before["representation"] == "PRIVATE_EXACT"


def test_atomic_outputs_are_separate_and_output_failure_leaves_no_partial(r0a: Fixture) -> None:
    report = r0a.collect()
    private_path = r0a.reports / "private.json"
    sanitized_path = r0a.reports / "sanitized.json"
    atomic_write_report(report, private_path, forbidden_roots=(r0a.repository, r0a.runtime))
    atomic_write_report(sanitize_report(report), sanitized_path, forbidden_roots=(r0a.repository, r0a.runtime))
    assert json.loads(private_path.read_text(encoding="utf-8"))["representation"] == "PRIVATE_EXACT"
    assert json.loads(sanitized_path.read_text(encoding="utf-8"))["representation"] == "SANITIZED"

    with pytest.raises(OutputFailure, match="outside"):
        atomic_write_report(report, r0a.repository / "forbidden.json", forbidden_roots=(r0a.repository,))

    failed_path = r0a.reports / "disk-full.json"

    def disk_full(label: str, phase: str) -> None:
        if label == "report_output" and phase == "before_replace":
            raise OSError(errno.ENOSPC, "disk full")

    with pytest.raises(OutputFailure, match="Atomic report write failed"):
        atomic_write_report(report, failed_path, fault_hook=disk_full)
    assert not failed_path.exists()
    assert not list(r0a.reports.glob(".*.tmp"))

    raced_path = r0a.reports / "raced.json"

    def create_race_winner(label: str, phase: str) -> None:
        if label == "report_output" and phase == "before_replace":
            raced_path.write_text("winner\n", encoding="utf-8")

    with pytest.raises(OutputFailure, match="already exists"):
        atomic_write_report(report, raced_path, fault_hook=create_race_winner)
    assert raced_path.read_text(encoding="utf-8") == "winner\n"
    assert not list(r0a.reports.glob(".*.tmp"))


def test_provider_does_not_call_domain_writers_claimers_ack_or_process_control(
    r0a: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bdb_bridge.instance_lock as lock_module
    import bdb_bridge.local_spool_transport as spool_module
    import bdb_bridge.native_request_receipts as receipt_module
    import bdb_bridge.workspace_promoter as promoter_module

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("forbidden source mutation path was called")

    monkeypatch.setattr(Journal, "open", forbidden)
    monkeypatch.setattr(lock_module.InstanceLock, "acquire", forbidden)
    monkeypatch.setattr(receipt_module.NativeRequestReceiptStore, "reserve", forbidden)
    monkeypatch.setattr(spool_module.LocalSpoolWriter, "submit", forbidden)
    monkeypatch.setattr(spool_module.LocalSpoolTransport, "fetch_snapshot", forbidden)
    monkeypatch.setattr(promoter_module.WorkspacePromoter, "promote_file", forbidden)
    monkeypatch.setattr(promoter_module.WorkspacePromotionWatcher, "scan_once", forbidden)
    monkeypatch.setattr(inventory.os, "replace", forbidden)

    report = r0a.collect()
    assert report["overall"]["result"] == OverallResult.READY_FOR_LOCAL_GATE


def test_all_observed_source_files_are_byte_and_timestamp_unchanged(r0a: Fixture) -> None:
    repository_before = _tree_fingerprint(r0a.repository)
    profile_before = _tree_fingerprint(r0a.profile)
    runtime_before = _tree_fingerprint(r0a.runtime)
    report = r0a.collect()
    assert report["overall"]["result"] == OverallResult.READY_FOR_LOCAL_GATE
    assert _tree_fingerprint(r0a.repository) == repository_before
    assert _tree_fingerprint(r0a.profile) == profile_before
    assert _tree_fingerprint(r0a.runtime) == runtime_before
    assert not list(r0a.scratch.iterdir())


def test_reparse_component_and_scratch_overlap_fail_closed(r0a: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    escape = r0a.runtime / "escape"
    escape.mkdir()
    document = json.loads(r0a.bridge_config.read_text(encoding="utf-8"))
    document["journal_path"] = str(escape / "journal.db")
    _json(r0a.bridge_config, document)
    original = inventory._is_reparse
    monkeypatch.setattr(inventory, "_is_reparse", lambda path: path == escape or original(path))
    escaped = r0a.collect()
    assert _source(escaped, "bridge_config")["status"] == SourceStatus.INVALID
    assert _source(escaped, "bridge_config")["errors"][0]["code"] == "reparse_point"

    monkeypatch.setattr(inventory, "_is_reparse", original)
    document["journal_path"] = str(r0a.journal)
    _json(r0a.bridge_config, document)
    overlap = r0a.collect(scratch_dir=r0a.repository)
    assert _source(overlap, "journal")["status"] == SourceStatus.INVALID
    assert _source(overlap, "journal")["errors"][0]["code"] == "scratch_overlap"


def test_actual_symlink_or_windows_junction_escape_is_rejected(r0a: Fixture) -> None:
    outside = r0a.root / "outside"
    outside.mkdir()
    link = r0a.runtime / "linked"
    if sys.platform == "win32":
        completed = subprocess.run(
            ["cmd", "/d", "/c", "mklink", "/J", str(link), str(outside)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        assert completed.returncode == 0, completed.stderr
    else:
        os.symlink(outside, link, target_is_directory=True)
    try:
        document = json.loads(r0a.bridge_config.read_text(encoding="utf-8"))
        document["journal_path"] = str(link / "journal.db")
        _json(r0a.bridge_config, document)
        report = r0a.collect()
        source = _source(report, "bridge_config")
        assert source["status"] == SourceStatus.INVALID
        assert source["errors"][0]["code"] in {"path_escape", "reparse_point"}
    finally:
        if sys.platform == "win32":
            os.rmdir(link)
        else:
            link.unlink()


def test_output_reparse_ancestor_is_rejected(r0a: Fixture) -> None:
    outside = r0a.root / "outside-reports"
    outside.mkdir()
    link = r0a.reports / "linked"
    if sys.platform == "win32":
        completed = subprocess.run(
            ["cmd", "/d", "/c", "mklink", "/J", str(link), str(outside)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        assert completed.returncode == 0, completed.stderr
    else:
        os.symlink(outside, link, target_is_directory=True)
    try:
        with pytest.raises(OutputFailure, match="regular directory"):
            atomic_write_report(r0a.collect(), link / "private.json")
        assert not (outside / "private.json").exists()
    finally:
        if sys.platform == "win32":
            os.rmdir(link)
        else:
            link.unlink()


def test_unicode_long_path_and_case_normalization_on_windows(r0a: Fixture) -> None:
    long_name = "Zażółć-gęślą-jaźń-" + "x" * 170
    long_parent = r0a.root / long_name
    long_bundle = long_parent / "browser"
    long_parent.mkdir()
    shutil.copytree(r0a.repository / "browser_extension", long_bundle)
    report = r0a.collect(browser_bundle_path=long_bundle)
    assert _source(report, "deployed_browser_bundle")["status"] == SourceStatus.OBSERVED
    if sys.platform == "win32":
        assert inventory._contained(Path(str(r0a.runtime).upper()), Path(str(r0a.root).upper()))


def test_cli_and_provider_have_same_semantic_report_and_schema_contract(r0a: Fixture) -> None:
    provider_report = r0a.collect()
    output = r0a.reports / "cli-private.json"
    sanitized = r0a.reports / "cli-sanitized.json"
    code = main(
        [
            "--repository",
            str(r0a.repository),
            "--bridge-config",
            str(r0a.bridge_config),
            "--native-config",
            str(r0a.native_config),
            "--browser-bundle",
            str(r0a.deployed_bundle),
            "--scratch-dir",
            str(r0a.scratch),
            "--timeout-seconds",
            "5",
            "--private-report",
            str(output),
            "--sanitized-report",
            str(sanitized),
        ]
    )
    cli_report = json.loads(output.read_text(encoding="utf-8"))
    assert code == 0
    assert cli_report["semantic_digest"] == provider_report["semantic_digest"]
    assert json.loads(sanitized.read_text(encoding="utf-8"))["representation"] == "SANITIZED"

    schema = json.loads((ROOT / "schemas" / "runtime-inventory-v1.schema.json").read_text(encoding="utf-8"))
    assert schema["$id"] == "runtime-inventory-v1"
    assert set(schema["required"]).issubset(cli_report)
    assert cli_report["overall"]["safe_to_mutate"] is False

    same_path = r0a.reports / "same.json"
    assert main(
        [
            "--repository",
            str(r0a.repository),
            "--bridge-config",
            str(r0a.bridge_config),
            "--native-config",
            str(r0a.native_config),
            "--private-report",
            str(same_path),
            "--sanitized-report",
            str(same_path),
        ]
    ) == 1
    assert not same_path.exists()


def test_architecture_has_no_source_write_claim_ack_checkpoint_or_broad_scan() -> None:
    source = (ROOT / "bdb_bridge" / "runtime_inventory.py").read_text(encoding="utf-8")
    forbidden = (
        "Journal.open(",
        "InstanceLock(",
        ".reserve(",
        ".submit(",
        ".fetch_snapshot(",
        ".scan_once(",
        "wal_checkpoint",
        "taskkill",
        "Stop-Process",
        "chrome.storage",
    )
    for token in forbidden:
        assert token not in source
    assert '"GIT_OPTIONAL_LOCKS"] = "0"' in source
    assert '"source_open_mode": "byte-copy-only"' in source
    assert '"safe_to_mutate": False' in source

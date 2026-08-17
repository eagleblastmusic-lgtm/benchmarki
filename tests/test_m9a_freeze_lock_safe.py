from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from bdb_vnext import m9a_freeze as base
from bdb_vnext.m9a_freeze_lock_safe import (
    _capture_lock_snapshot,
    _verified_cached_entry,
    execute_freeze_lock_safe,
)


DIGEST = "sha256:" + "1" * 64


def test_cached_lock_entry_does_not_reopen_source(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "bridge.instance.lock"
    source.write_bytes(b"\x00")
    snapshot = _capture_lock_snapshot(source)
    destination = tmp_path / "archive" / "bridge.instance.lock"

    original_read_bytes = Path.read_bytes

    def deny_second_source_read(path: Path) -> bytes:
        if path.resolve(strict=False) == source.resolve(strict=False):
            raise PermissionError(13, "Permission denied")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", deny_second_source_read)

    total = [0]
    entry = _verified_cached_entry(
        source=source,
        destination=destination,
        snapshot=snapshot,
        total_state=total,
    )

    assert destination.read_bytes() == b"\x00"
    assert entry["capture_mode"] == "PRE_LOCK_EXACT_COORDINATION_SNAPSHOT"
    assert entry["coordination_only"] is True
    assert entry["size"] == 1
    assert total == [1]


def test_adapter_routes_locked_file_through_prelock_snapshot(tmp_path: Path, monkeypatch) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    lock_path = runtime / "bridge.instance.lock"
    lock_path.write_bytes(b"\x00")
    ordinary = runtime / "ordinary.txt"
    ordinary.write_text("stable", encoding="utf-8")

    bridge_config = tmp_path / "bridge-config.json"
    bridge_config.write_text(
        json.dumps({"runtime_dir": str(runtime)}),
        encoding="utf-8",
    )
    profile = base.ProfileSpec("profile-a", bridge_config, DIGEST)

    original_archive_tree = base._archive_tree
    original_read_bytes = Path.read_bytes
    observed: dict[str, object] = {}

    def fake_execute_freeze(**kwargs):
        assert base._archive_tree is not original_archive_tree

        def deny_locked_source_read(path: Path) -> bytes:
            if path.resolve(strict=False) == lock_path.resolve(strict=False):
                raise PermissionError(13, "Permission denied")
            return original_read_bytes(path)

        monkeypatch.setattr(Path, "read_bytes", deny_locked_source_read)
        total = [0]
        entries = base._archive_tree(
            runtime,
            tmp_path / "archive-root",
            "profile-a-runtime",
            total_state=total,
        )
        observed["entries"] = entries
        observed["total"] = total[0]
        return {"status": "TEST_PASS"}

    monkeypatch.setattr(base, "execute_freeze", fake_execute_freeze)

    result = execute_freeze_lock_safe(
        profiles=(profile,),
        native_config_path=tmp_path / "native-host.json",
        archive_parent=tmp_path / "archive-parent",
        expected_vnext_root=tmp_path / "vnext",
        observation_seconds=2.0,
        apply=True,
    )

    assert result == {"status": "TEST_PASS"}
    entries = observed["entries"]
    assert isinstance(entries, list)
    lock_entries = [item for item in entries if item["source_path"] == str(lock_path)]
    assert len(lock_entries) == 1
    assert lock_entries[0]["capture_mode"] == "PRE_LOCK_EXACT_COORDINATION_SNAPSHOT"
    assert ordinary.read_text(encoding="utf-8") == "stable"
    assert base._archive_tree is original_archive_tree


def test_lock_safe_wrapper_can_be_invoked_directly() -> None:
    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [sys.executable, str(root / "scripts" / "run_m9a_freeze_lock_safe.py"), "--help"],
        cwd=root.parent,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--apply" in completed.stdout
    assert "--profile" in completed.stdout

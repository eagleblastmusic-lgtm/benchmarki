from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from bdb_bridge.workspace_context import WorkspaceContextBuilder


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        shell=False,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init")
    git(root, "config", "user.name", "Workspace Context Test")
    git(root, "config", "user.email", "workspace-context@example.invalid")
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / "private").mkdir()
    (root / "src" / "app.py").write_text(
        "class Calculator:\n"
        "    def add(self, left: int, right: int) -> int:\n"
        "        return left + right\n",
        encoding="utf-8",
        newline="\n",
    )
    (root / "tests" / "test_app.py").write_text(
        "def test_placeholder() -> None:\n"
        "    assert True\n",
        encoding="utf-8",
        newline="\n",
    )
    (root / "private" / "secret.txt").write_text("must not be disclosed\n", encoding="utf-8")
    (root / "src" / "binary.bin").write_bytes(b"\x00\xff\x01")
    git(root, "add", "--", ".")
    git(root, "commit", "-m", "fixture")
    return root


def context_config(tmp_path: Path, root: Path, patterns: tuple[str, ...]) -> SimpleNamespace:
    return SimpleNamespace(
        fixture_repo_path=root,
        runtime_dir=tmp_path / "runtime",
        allowed_paths=patterns,
    )


def test_snapshot_returns_only_allowed_text_files_and_symbols(tmp_path: Path) -> None:
    root = repository(tmp_path)
    config = context_config(tmp_path, root, ("src/*.py", "tests/*.py"))

    snapshot = WorkspaceContextBuilder(config).build()

    assert snapshot["source_clean"] is True
    assert snapshot["controlled_clean"] is True
    assert snapshot["tracked_paths"] == ["src/app.py", "tests/test_app.py"]
    assert [item["path"] for item in snapshot["snapshot_files"]] == [
        "src/app.py",
        "tests/test_app.py",
    ]
    assert any(item["text"].startswith("class Calculator") for item in snapshot["symbols"])
    assert any("def add" in item["text"] for item in snapshot["symbols"])
    serialized = json.dumps(snapshot, ensure_ascii=False)
    assert "must not be disclosed" not in serialized
    assert str(root) not in serialized
    assert snapshot["capabilities"]["workspace_context"] is True
    assert snapshot["capabilities"]["promotion_receipts"] is True
    assert snapshot["latest_promotion"] is None


def test_snapshot_reports_only_allowed_dirty_path_names(tmp_path: Path) -> None:
    root = repository(tmp_path)
    (root / "src" / "app.py").write_text("def changed() -> bool:\n    return True\n", encoding="utf-8")
    (root / "private" / "secret.txt").write_text("new private value\n", encoding="utf-8")
    config = context_config(tmp_path, root, ("src/*.py",))

    snapshot = WorkspaceContextBuilder(config).build()

    assert snapshot["source_clean"] is False
    assert snapshot["controlled_clean"] is False
    assert snapshot["source_changes"] == ["src/app.py"]
    assert snapshot["source_changes_outside_scope"] == 1
    assert snapshot["tracked_paths"] == ["src/app.py"]
    serialized = json.dumps(snapshot, ensure_ascii=False)
    assert "new private value" not in serialized
    assert "private/secret.txt" not in serialized


def test_dirty_crlf_checkout_context_uses_committed_lf_blob_bytes(tmp_path: Path) -> None:
    root = repository(tmp_path)
    path = root / "src" / "app.py"
    committed = path.read_bytes()
    physical = committed.replace(b"\n", b"\r\n")
    assert physical != committed
    path.write_bytes(physical)
    config = context_config(tmp_path, root, ("src/app.py",))

    snapshot = WorkspaceContextBuilder(config).build()
    app = next(item for item in snapshot["snapshot_files"] if item["path"] == "src/app.py")

    assert snapshot["source_clean"] is False
    assert snapshot["controlled_clean"] is False
    assert snapshot["source_changes"] == ["src/app.py"]
    assert snapshot["snapshot_source"] == "git_blobs"
    assert app["content"] == committed.decode("utf-8")
    assert app["bytes"] == len(committed)
    assert app["sha256"] == "sha256:" + hashlib.sha256(committed).hexdigest()


def test_snapshot_distinguishes_unrelated_dirty_paths_from_controlled_scope(
    tmp_path: Path,
) -> None:
    root = repository(tmp_path)
    (root / "private" / "secret.txt").write_text(
        "unrelated local value\n", encoding="utf-8"
    )
    config = context_config(tmp_path, root, ("src/*.py",))

    snapshot = WorkspaceContextBuilder(config).build_summary()

    assert snapshot["source_clean"] is False
    assert snapshot["controlled_clean"] is True
    assert snapshot["source_changes"] == []
    assert snapshot["source_changes_outside_scope"] == 1


def test_snapshot_exposes_only_valid_allowed_promotion_receipt(tmp_path: Path) -> None:
    root = repository(tmp_path)
    config = context_config(tmp_path, root, ("src/*.py",))
    promotions = Path(config.runtime_dir) / "promotions"
    promotions.mkdir(parents=True)
    receipt = {
        "schema": "bdb-workspace-promotion-v1",
        "status": "promoted",
        "command_id": "795545ec-2d28-46af-a4c4-c40877e9cf2a:000001",
        "source_commit": "a" * 40,
        "changed_files": ["src/app.py"],
        "file_sha256": {"src/app.py": "sha256:" + "b" * 64},
        "promoted_at": "2026-07-17T22:00:00.000000Z",
    }
    (promotions / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")

    snapshot = WorkspaceContextBuilder(config).build()

    assert snapshot["latest_promotion"] == {
        "status": "promoted",
        "command_id": receipt["command_id"],
        "source_commit": "a" * 40,
        "changed_files": ["src/app.py"],
        "file_sha256": receipt["file_sha256"],
        "promoted_at": receipt["promoted_at"],
    }

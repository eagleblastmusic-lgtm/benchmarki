from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bdb_bridge import BridgeError
from bdb_bridge.native_actions import (
    ACTION_SCHEMA,
    NativeActionComposer,
    NativeSessionStore,
    RepositoryAlias,
)


NOW = datetime(2026, 7, 21, 18, 0, 0, tzinfo=timezone.utc)
SESSION = "018f3f66-6cb3-4f66-9f2e-3d7647d1b701"


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


def digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def setup(tmp_path: Path) -> tuple[NativeActionComposer, NativeSessionStore, str]:
    fixture = tmp_path / "fixture"
    control = tmp_path / "control"
    worktrees = tmp_path / "worktrees"
    runtime = tmp_path / "runtime"
    for path in (fixture, control, worktrees, runtime):
        path.mkdir()
    git(fixture, "init")
    git(fixture, "config", "user.name", "Native Preflight Test")
    git(fixture, "config", "user.email", "native-preflight@example.invalid")
    (fixture / "src").mkdir()
    old = b"value = 1\n"
    (fixture / "src" / "app.py").write_bytes(old)
    git(fixture, "add", "--", "src/app.py")
    git(fixture, "commit", "-m", "fixture")

    config_path = tmp_path / "bridge.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": "1.1",
                "control_repo_path": str(control),
                "fixture_repo_path": str(fixture),
                "worktree_root": str(worktrees),
                "runtime_dir": str(runtime),
                "repository_id": "native-preflight",
                "allowed_paths": ["src/**"],
                "max_sequence": 3,
            }
        ),
        encoding="utf-8",
    )
    repository = RepositoryAlias.load("synthetic", config_path)

    def writer(path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")

    store = NativeSessionStore(tmp_path / "sessions.json", writer=writer)
    composer = NativeActionComposer(
        {"synthetic": repository},
        store,
        now_fn=lambda: NOW,
    )
    return composer, store, digest(old)


def replacement_action(path: str, expected: str, content: bytes, declared: str) -> dict:
    return {
        "schema": ACTION_SCHEMA,
        "repo_alias": "synthetic",
        "session_id": SESSION,
        "operation": "multi_file_patch",
        "sequence": 1,
        "expected_revision": 0,
        "payload": {
            "profile_id": "poc_pytest",
            "patch": {
                "schema": "bdb-multi-file-patch-v1",
                "operations": [
                    {
                        "schema": "bdb-file-replacement-v1",
                        "kind": "replace_file",
                        "path": path,
                        "expected_sha256": expected,
                        "content_base64": base64.b64encode(content).decode("ascii"),
                        "content_sha256": declared,
                    }
                ],
            },
        },
    }


def test_native_preflight_rejects_hash_before_session_binding(tmp_path: Path) -> None:
    composer, store, old_digest = setup(tmp_path)
    content = b"value = 2\n"

    with pytest.raises(BridgeError) as exc:
        composer.compose(
            replacement_action(
                "src/app.py",
                old_digest,
                content,
                "sha256:" + "f" * 64,
            )
        )

    assert exc.value.code == "invalid_payload"
    assert "content_sha256" in str(exc.value)
    assert store.get(SESSION) is None


def test_native_preflight_rejects_exact_path_before_session_binding(tmp_path: Path) -> None:
    composer, store, old_digest = setup(tmp_path)
    content = b"echo ok\n"

    with pytest.raises(BridgeError) as exc:
        composer.compose(
            replacement_action(
                "START-APP.cmd",
                old_digest,
                content,
                digest(content),
            )
        )

    assert exc.value.code == "policy_denied"
    assert str(exc.value) == "Path is not allowed by local policy: START-APP.cmd"
    assert store.get(SESSION) is None


def test_native_preflight_allows_valid_patch_and_binds_session(tmp_path: Path) -> None:
    composer, store, old_digest = setup(tmp_path)
    content = b"value = 3\n"

    action = replacement_action(
        "src/app.py",
        old_digest,
        content,
        digest(content),
    )
    action["client_submission_nonce"] = SESSION
    _, envelope = composer.compose(action)

    assert envelope["command"]["session_id"] == SESSION
    assert envelope["command"]["task_id"] == SESSION
    assert envelope["command"]["attempt_id"] == SESSION
    assert envelope["command"]["client_submission_nonce"] == SESSION
    assert envelope["manifest"]["allowed_paths"] == ["src/**"]
    assert store.get(SESSION) is not None

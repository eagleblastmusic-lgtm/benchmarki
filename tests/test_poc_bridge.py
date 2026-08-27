from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from bdb_poc import (
    BridgeConfig,
    BridgeError,
    ControlRepository,
    MAX_RESULT_BYTES,
    PocBridge,
    Workspace,
    canonical_json,
    finalize_result,
    result_path_for,
    validate_repo_relative_path,
    validate_session_id,
)

SESSION_ID = "018f3f66-6cb3-4f66-9f2e-3d7647d1b701"


def run_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if check and completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    return completed


def init_fixture(tmp_path: Path) -> tuple[Path, str]:
    source = Path(__file__).parents[1] / "bdb-poc-fixture"
    fixture = tmp_path / "fixture"
    shutil.copytree(source, fixture, ignore=shutil.ignore_patterns(".pytest_cache", "__pycache__", "*.pyc"))
    run_git(fixture, "init", "-b", "main")
    run_git(fixture, "config", "core.autocrlf", "false")
    run_git(fixture, "config", "user.name", "POC Test")
    run_git(fixture, "config", "user.email", "poc@example.invalid")
    run_git(fixture, "add", "--", ".gitattributes", ".gitignore", "pyproject.toml", "src", "tests")
    run_git(fixture, "commit", "-m", "fixture baseline")
    return fixture, run_git(fixture, "rev-parse", "HEAD").stdout.strip()


def make_config(tmp_path: Path, fixture: Path, control: Path | None = None) -> BridgeConfig:
    return BridgeConfig(
        control_repo_path=control or tmp_path / "control",
        fixture_repo_path=fixture,
        worktree_root=tmp_path / "worktrees",
        allowed_paths=("src/clamp.py", "tests/test_clamp.py"),
        poll_interval_seconds=0.01,
        max_poll_seconds=30,
        test_timeout_seconds=30,
        python_executable=sys.executable,
    )


def test_session_id_and_path_validation() -> None:
    validate_session_id(SESSION_ID)
    validate_session_id("01J2QX4M8M7WY7R4K5V6J8T9AB")
    with pytest.raises(BridgeError, match="UUID or ULID"):
        validate_session_id("../../escape")
    assert validate_repo_relative_path("src/clamp.py") == "src/clamp.py"
    for unsafe in ("../x", "/absolute", "src\\x.py", "./x"):
        with pytest.raises(BridgeError):
            validate_repo_relative_path(unsafe)


def test_command_contract_rejects_schema_sequence_and_revision(tmp_path: Path) -> None:
    fixture, _ = init_fixture(tmp_path)
    bridge = PocBridge(make_config(tmp_path, fixture))
    valid = {
        "schema_version": "1.1",
        "session_id": SESSION_ID,
        "command_id": f"{SESSION_ID}:000001",
        "sequence": 1,
        "operation": "open_read",
        "expected_revision": 0,
        "payload": {"path": "src/clamp.py", "start_line": 1, "end_line": 10},
    }
    bridge._validate_command(valid, SESSION_ID, 1)

    invalid_schema = dict(valid, schema_version="2.0")
    with pytest.raises(BridgeError) as schema_error:
        bridge._validate_command(invalid_schema, SESSION_ID, 1)
    assert schema_error.value.code == "unsupported_schema"

    invalid_sequence = dict(valid, sequence=2)
    with pytest.raises(BridgeError) as sequence_error:
        bridge._validate_command(invalid_sequence, SESSION_ID, 1)
    assert sequence_error.value.code == "sequence_mismatch"

    invalid_revision = dict(valid, expected_revision=-1)
    with pytest.raises(BridgeError) as revision_error:
        bridge._validate_command(invalid_revision, SESSION_ID, 1)
    assert revision_error.value.code == "invalid_revision"


def test_workspace_fail_then_pass(tmp_path: Path) -> None:
    fixture, base_sha = init_fixture(tmp_path)
    workspace = Workspace(
        make_config(tmp_path, fixture),
        SESSION_ID,
        base_sha,
        ["src/clamp.py", "tests/test_clamp.py"],
    )
    workspace.create()

    read = workspace.read_range("src/clamp.py", 1, 20)
    assert "return value" in read["content"]
    initial_hash = workspace.state_hash()

    first = workspace.replace_exact_and_test(
        {
            "path": "src/clamp.py",
            "old": "return value",
            "new": "return min(value, 100)",
            "profile_id": "poc_pytest",
        },
        sys.executable,
        30,
    )
    assert first["status"] == "failed"
    assert first["exit_code"] != 0
    assert first["revision_after"] == 1
    assert "src/clamp.py" in first["changed_files"]
    assert workspace.state_hash() != initial_hash

    second = workspace.replace_exact_and_test(
        {
            "path": "src/clamp.py",
            "old": "return min(value, 100)",
            "new": "return max(0, min(value, 100))",
            "profile_id": "poc_pytest",
        },
        sys.executable,
        30,
    )
    assert second["status"] == "success"
    assert second["exit_code"] == 0
    assert second["revision_after"] == 2
    assert "3 passed" in second["stdout"]


def test_workspace_rejects_scope_and_profile(tmp_path: Path) -> None:
    fixture, base_sha = init_fixture(tmp_path)
    workspace = Workspace(make_config(tmp_path, fixture), SESSION_ID, base_sha, ["src/clamp.py"])
    workspace.create()
    with pytest.raises(BridgeError) as scope:
        workspace.read_range("tests/test_clamp.py", 1, 5)
    assert scope.value.code == "scope_violation"
    with pytest.raises(BridgeError) as profile:
        workspace.replace_exact_and_test(
            {
                "path": "src/clamp.py",
                "old": "return value",
                "new": "return 1",
                "profile_id": "remote_shell",
            },
            sys.executable,
            30,
        )
    assert profile.value.code == "policy_denied"


def test_finalize_result_is_small_and_has_valid_end_marker() -> None:
    result = {
        "schema_version": "1.1",
        "session_id": SESSION_ID,
        "sequence": 1,
        "status": "success",
        "stdout_tail": "x" * 50_000,
        "stderr_tail": "",
        "diff": "y" * 50_000,
        "truncated": False,
    }
    serialized = finalize_result(result)
    assert len(serialized.encode("utf-8")) <= MAX_RESULT_BYTES
    parsed = json.loads(serialized)
    marker = parsed.pop("end_marker")
    expected = hashlib.sha256(canonical_json(parsed).encode("utf-8")).hexdigest()
    assert marker == f"BDB-END:sha256:{expected}"
    assert parsed["truncated"] is True


def init_control_remote(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "control.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    writer = tmp_path / "writer"
    subprocess.run(["git", "clone", str(remote), str(writer)], check=True, capture_output=True)
    run_git(writer, "config", "user.name", "POC Test")
    run_git(writer, "config", "user.email", "poc@example.invalid")
    (writer / "README.md").write_text("# control\n", encoding="utf-8")
    run_git(writer, "add", "README.md")
    run_git(writer, "commit", "-m", "initial")
    run_git(writer, "branch", "-M", "main")
    run_git(writer, "push", "-u", "origin", "main")
    for branch in ("commands", "results"):
        run_git(writer, "checkout", "-B", branch, "main")
        run_git(writer, "push", "-u", "origin", branch)
    run_git(writer, "checkout", "commands")

    bridge_control = tmp_path / "bridge-control"
    subprocess.run(
        ["git", "clone", "--branch", "main", str(remote), str(bridge_control)],
        check=True,
        capture_output=True,
    )
    return writer, bridge_control


def write_protocol(writer: Path, base_sha: str) -> None:
    manifest = {
        "schema_version": "1.1",
        "session_id": SESSION_ID,
        "title": "POC test",
        "repository_id": "bdb-poc-fixture",
        "base_ref": "main",
        "base_sha": base_sha,
        "allowed_paths": ["src/clamp.py", "tests/test_clamp.py"],
    }
    commands = [
        {
            "schema_version": "1.1",
            "session_id": SESSION_ID,
            "command_id": f"{SESSION_ID}:000001",
            "sequence": 1,
            "operation": "open_read",
            "expected_revision": 0,
            "payload": {"path": "src/clamp.py", "start_line": 1, "end_line": 20},
        },
        {
            "schema_version": "1.1",
            "session_id": SESSION_ID,
            "command_id": f"{SESSION_ID}:000002",
            "sequence": 2,
            "operation": "replace_exact_and_test",
            "expected_revision": 0,
            "payload": {
                "path": "src/clamp.py",
                "old": "return value",
                "new": "return min(value, 100)",
                "profile_id": "poc_pytest",
            },
        },
        {
            "schema_version": "1.1",
            "session_id": SESSION_ID,
            "command_id": f"{SESSION_ID}:000003",
            "sequence": 3,
            "operation": "replace_exact_and_test",
            "expected_revision": 1,
            "payload": {
                "path": "src/clamp.py",
                "old": "return min(value, 100)",
                "new": "return max(0, min(value, 100))",
                "profile_id": "poc_pytest",
            },
        },
    ]
    root = writer / "sessions" / SESSION_ID
    (root / "commands").mkdir(parents=True)
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    for index, command in enumerate(commands, 1):
        (root / "commands" / f"{index:06d}.json").write_text(json.dumps(command), encoding="utf-8")
    run_git(writer, "add", "--", f"sessions/{SESSION_ID}")
    run_git(writer, "commit", "-m", "add POC commands")
    run_git(writer, "push", "origin", "commands")


def test_end_to_end_local_transport(tmp_path: Path) -> None:
    fixture, base_sha = init_fixture(tmp_path)
    writer, bridge_control = init_control_remote(tmp_path)
    write_protocol(writer, base_sha)

    bridge = PocBridge(make_config(tmp_path, fixture, bridge_control))
    assert bridge.run() == 0

    run_git(writer, "fetch", "origin", "results")
    statuses = []
    for sequence in (1, 2, 3):
        path = result_path_for(SESSION_ID, sequence)
        raw = run_git(writer, "show", f"origin/results:{path}").stdout
        result = json.loads(raw)
        statuses.append(result["status"])
        assert result["end_marker"].startswith("BDB-END:sha256:")
        assert len(raw.encode("utf-8")) <= MAX_RESULT_BYTES + 1
    assert statuses == ["success", "failed", "success"]


def test_windows_scripts_use_new_configurable_default_root() -> None:
    repo_root = Path(__file__).parents[1]
    doc_path = repo_root / 'POC_0_WINDOWS_START.md'
    if not doc_path.exists():
        pytest.skip("Historical POC_0_WINDOWS_START.md prerequisite is not present in vNext repository")
    expected = r'C:\Projekt\DevMaster\POC0'
    legacy = r'C:\BartoszDev\POC0'

    for relative in (
        'scripts/bootstrap_windows.ps1',
        'scripts/run_poc_bridge.ps1',
    ):
        script = (repo_root / relative).read_text(encoding='utf-8')
        assert f'[string]$Root = "{expected}"' in script
        assert legacy not in script
        assert 'param(' in script
        assert '$Root' in script

    documentation = doc_path.read_text(encoding='utf-8')
    assert expected in documentation
    assert legacy not in documentation
    assert r'-Root "D:\BartoszDev\POC0"' in documentation


def test_windows_ci_parses_powershell_without_running_bootstrap() -> None:
    repo_root = Path(__file__).parents[1]
    workflow = (repo_root / '.github/workflows/bridge-ci.yml').read_text(encoding='utf-8')
    assert "if: runner.os == 'Windows'" in workflow
    assert '[System.Management.Automation.Language.Parser]::ParseFile' in workflow
    assert 'scripts/bootstrap_windows.ps1' in workflow
    assert 'scripts/run_poc_bridge.ps1' in workflow
    assert 'bootstrap_windows.ps1 -Root' not in workflow


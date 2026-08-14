from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from bdb_vnext.n6_rehearsal import (
    N6RehearsalConfig,
    P1_ENGINEERING_IDENTITY_SCHEMA,
    P1_ENGINEERING_TARGET_SCHEMA_V2,
    prepare_package,
)


def _git_target(tmp_path: Path) -> dict[str, str]:
    root = tmp_path / "target"
    root.mkdir()
    (root / "target.txt").write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "phase2/test"], cwd=root, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "BDB test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "bdb-test@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "add", "target.txt"], cwd=root, check=True)
    env = os.environ.copy()
    env.update({"GIT_AUTHOR_NAME": "BDB test", "GIT_AUTHOR_EMAIL": "bdb-test@example.invalid", "GIT_COMMITTER_NAME": "BDB test", "GIT_COMMITTER_EMAIL": "bdb-test@example.invalid"})
    subprocess.run(["git", "commit", "-m", "phase2 target"], cwd=root, check=True, capture_output=True, text=True, env=env)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True).stdout.strip()
    tree = subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=root, check=True, capture_output=True, text=True).stdout.strip()
    return {"repo_root": str(root), "repository_id": "gicleeart-phase2-test", "branch": "phase2/test", "commit": commit, "tree": tree}


def _target(base: dict[str, str], identity: dict[str, str]) -> dict[str, object]:
    return {
        **base,
        "allowed_paths": ["target.txt"],
        "engineering_identity": identity,
        "checker": {"checker_id": "bdb-phase2-target-checker", "checker_version": "1", "argv": [sys.executable, "-c", "pass"], "cwd": ".", "timeout_seconds": 60.0},
    }


def _identity(prefix: str, intent_revision: str, evaluator_id: str) -> dict[str, str]:
    return {"schema": P1_ENGINEERING_IDENTITY_SCHEMA, "prompt_prefix": prefix, "intent_revision": intent_revision, "evaluator_id": evaluator_id, "evaluator_version": "1"}


def _prepare(tmp_path: Path, target: dict[str, object], monkeypatch) -> tuple[dict[str, object], Path]:
    repo = Path(__file__).parents[1].absolute()
    package = tmp_path / "package"
    monkeypatch.setattr("bdb_vnext.n6_rehearsal._build_shim", lambda *args, **kwargs: None)
    execution = prepare_package(repo_root=repo, output=package, runtime_root=tmp_path / "runtime", legacy_runtime_root=tmp_path / "legacy", source_commit="HEAD", python_executable=sys.executable, engineering_target=target)
    return execution, package


def test_calculator_identity_remains_explicit_and_compatible(tmp_path: Path, monkeypatch) -> None:
    base = _git_target(tmp_path)
    execution, package = _prepare(tmp_path, _target(base, _identity("BDB-P1-CALC-BROWSER-E2E", "p1-calc-v1", "bdb-vnext-p1-calculator-evaluator")), monkeypatch)
    target = execution["package"]["engineering_target"]
    assert target["schema"] == P1_ENGINEERING_TARGET_SCHEMA_V2
    assert target["engineering_identity"]["prompt_prefix"] == "BDB-P1-CALC-BROWSER-E2E"
    assert target["engineering_identity"]["intent_revision"] == "p1-calc-v1"
    assert target["engineering_identity"]["evaluator_id"] == "bdb-vnext-p1-calculator-evaluator"
    assert "BDB-P1-CALC-BROWSER-E2E" in (package / "browser-extension" / "content.js").read_text(encoding="utf-8")
    assert N6RehearsalConfig.from_json(package / "native-config.json").engineering_target == target


def test_giclee_identity_is_bound_without_calculator_semantic_leak(tmp_path: Path, monkeypatch) -> None:
    base = _git_target(tmp_path)
    giclee = _identity("BDB-P2-GICLEE-ACCESSIBILITY", "p2-giclee-accessibility-v1", "bdb-vnext-p2-giclee-accessibility-evaluator")
    execution, package = _prepare(tmp_path, _target(base, giclee), monkeypatch)
    target = execution["package"]["engineering_target"]
    assert target["schema"] == P1_ENGINEERING_TARGET_SCHEMA_V2
    assert target["engineering_identity"] == giclee
    content = (package / "browser-extension" / "content.js").read_text(encoding="utf-8")
    background = (package / "browser-extension" / "background.js").read_text(encoding="utf-8")
    assert "BDB-P2-GICLEE-ACCESSIBILITY" in content
    assert "BDB-P2-GICLEE-ACCESSIBILITY" in background
    package_bytes = b"".join(path.read_bytes() for path in package.rglob("*") if path.is_file())
    assert b"BDB-P1-CALC-BROWSER-E2E" not in package_bytes
    assert b"p1-calc-v1" not in package_bytes
    assert b"bdb-vnext-p1-calculator-evaluator" not in package_bytes
    assert N6RehearsalConfig.from_json(package / "native-config.json").engineering_target == target

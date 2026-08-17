from __future__ import annotations

import json
import os
import struct
import subprocess
from pathlib import Path

import pytest

from bdb_vnext.composition import BROWSER_EXTENSION_ID, PROTOCOL_GENERATION
from bdb_vnext.m11c_native_artifact import build_windows_native_artifact, exact_git_subject
from bdb_vnext.m11c_windows_clients import stage_client_plan
from bdb_vnext.m9b_native_host import M9B_NATIVE_REQUEST_SCHEMA


pytestmark = pytest.mark.skipif(os.name != "nt", reason="frozen vNext Native artifact build is Windows-only")
ROOT = Path(__file__).resolve().parents[1]
ORIGIN = f"chrome-extension://{BROWSER_EXTENSION_ID}/"


def _frame(document: dict[str, object]) -> bytes:
    payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return struct.pack("<I", len(payload)) + payload


def _decode(value: bytes) -> dict[str, object]:
    assert len(value) >= 4
    length = struct.unpack("<I", value[:4])[0]
    assert len(value) == 4 + length
    result = json.loads(value[4:].decode("utf-8"))
    assert isinstance(result, dict)
    return result


def test_frozen_onefile_native_host_runs_exact_browser_handshake(tmp_path: Path) -> None:
    subject = exact_git_subject(ROOT)
    artifact = build_windows_native_artifact(repo_root=ROOT, output_root=tmp_path / "artifact")
    assert artifact.source_head == subject["source_head"]
    assert artifact.source_tree == subject["source_tree"]

    runtime = tmp_path / "runtime"
    legacy = tmp_path / "legacy"
    bootstrap = tmp_path / "ProgramData" / "BartoszDevBridge-Next" / "bootstrap"
    staged = stage_client_plan(
        runtime_root=runtime,
        legacy_runtime_root=legacy,
        bootstrap_authority_root=bootstrap,
        browser_source_root=ROOT / "browser_extension_vnext",
        native_host_executable=artifact.executable_path,
        source_head=artifact.source_head,
        source_tree=artifact.source_tree,
    )
    observation = dict(staged["browser"])
    observation.pop("bundle_digest")
    request = {
        "schema": M9B_NATIVE_REQUEST_SCHEMA,
        "request_id": "frozen-artifact-handshake",
        "action": "handshake",
        "protocol_generation": PROTOCOL_GENERATION,
        "browser_extension_id": BROWSER_EXTENSION_ID,
        "browser_observation": observation,
    }
    completed = subprocess.run(
        [str(artifact.executable_path), ORIGIN, "--parent-window=0", "--config", str(staged["plan"]["native_config_path"])],
        input=_frame(request),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    response = _decode(completed.stdout)
    assert response["status"] == "success"
    assert str(response["client_verification_sha256"]).startswith("sha256:")
    assert staged["plan"]["native_host_executable_sha256"] == artifact.executable_sha256

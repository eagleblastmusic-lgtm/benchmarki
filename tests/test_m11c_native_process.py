from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from bdb_vnext.composition import BROWSER_EXTENSION_ID, PROTOCOL_GENERATION
from bdb_vnext.m11c_windows_clients import require_client_verification, stage_client_plan
from bdb_vnext.m9b_native_host import M9B_NATIVE_REQUEST_SCHEMA


pytestmark = pytest.mark.skipif(os.name != "nt", reason="installed Native Messaging process proof is Windows-only")

ROOT = Path(__file__).resolve().parents[1]
BROWSER_SOURCE = ROOT / "browser_extension_vnext"
ORIGIN = f"chrome-extension://{BROWSER_EXTENSION_ID}/"
HEAD = "a" * 40
TREE = "b" * 40


def _native_executable() -> Path:
    value = shutil.which("bdb-vnext-native-host") or shutil.which("bdb-vnext-native-host.exe")
    if not value:
        pytest.skip("installed bdb-vnext-native-host entrypoint is missing from PATH")
    return Path(value).absolute()


def _frame(document: dict[str, object]) -> bytes:
    payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return struct.pack("<I", len(payload)) + payload


def _decode_one_frame(value: bytes) -> dict[str, object]:
    assert len(value) >= 4
    length = struct.unpack("<I", value[:4])[0]
    assert len(value) == 4 + length
    document = json.loads(value[4:].decode("utf-8"))
    assert isinstance(document, dict)
    return document


def _stage(tmp_path: Path):
    runtime = tmp_path / "runtime"
    legacy = tmp_path / "legacy"
    bootstrap = tmp_path / "ProgramData" / "BartoszDevBridge-Next" / "bootstrap"
    result = stage_client_plan(
        runtime_root=runtime,
        legacy_runtime_root=legacy,
        bootstrap_authority_root=bootstrap,
        browser_source_root=BROWSER_SOURCE,
        native_host_executable=_native_executable(),
        source_head=HEAD,
        source_tree=TREE,
    )
    observation = dict(result["browser"])
    observation.pop("bundle_digest")
    return runtime, result["plan"], observation


def test_installed_native_host_accepts_real_chrome_argv_binary_frame_and_records_witness(tmp_path: Path) -> None:
    runtime, plan, observation = _stage(tmp_path)
    request = {
        "schema": M9B_NATIVE_REQUEST_SCHEMA,
        "request_id": "process-handshake-1",
        "action": "handshake",
        "protocol_generation": PROTOCOL_GENERATION,
        "browser_extension_id": BROWSER_EXTENSION_ID,
        "browser_observation": observation,
    }
    completed = subprocess.run(
        [
            str(_native_executable()),
            ORIGIN,
            "--parent-window=0",
            "--config",
            str(plan["native_config_path"]),
        ],
        input=_frame(request),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    response = _decode_one_frame(completed.stdout)
    assert response["status"] == "success"
    assert response["browser_extension_id"] == BROWSER_EXTENSION_ID
    assert response["activation"]["production_acceptance"] is False  # type: ignore[index]
    assert str(response["client_verification_sha256"]).startswith("sha256:")

    witness = require_client_verification(
        runtime_root=runtime,
        expected_client_plan_sha256=plan["client_plan_sha256"],
    )
    assert witness["verification_sha256"] == response["client_verification_sha256"]
    assert witness["browser_bundle_digest"] == plan["browser_bundle_digest"]
    assert witness["production_activation_performed"] is False


def test_installed_native_host_rejects_foreign_extension_origin_before_reading_messages(tmp_path: Path) -> None:
    _runtime, plan, _observation = _stage(tmp_path)
    completed = subprocess.run(
        [
            str(_native_executable()),
            "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/",
            "--config",
            str(plan["native_config_path"]),
        ],
        input=b"",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 2
    assert completed.stdout == b""

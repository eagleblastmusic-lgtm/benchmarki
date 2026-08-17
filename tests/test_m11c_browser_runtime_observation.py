from __future__ import annotations

from pathlib import Path

import pytest

from bdb_vnext.composition import BROWSER_EXTENSION_ID
from bdb_vnext.m11c_windows_clients import (
    M11cClientError,
    record_browser_launch_verification,
    stage_client_plan,
)


ROOT = Path(__file__).resolve().parents[1]
BROWSER_SOURCE = ROOT / "browser_extension_vnext"
ORIGIN = f"chrome-extension://{BROWSER_EXTENSION_ID}/"


def _stage(tmp_path: Path):
    runtime = tmp_path / "runtime"
    executable = tmp_path / "Scripts" / "bdb-vnext-native-host.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"native")
    staged = stage_client_plan(
        runtime_root=runtime,
        legacy_runtime_root=tmp_path / "legacy",
        bootstrap_authority_root=tmp_path / "ProgramData" / "BartoszDevBridge-Next" / "bootstrap",
        browser_source_root=BROWSER_SOURCE,
        native_host_executable=executable,
        source_head="a" * 40,
        source_tree="b" * 40,
    )
    observation = dict(staged["browser"])
    observation.pop("bundle_digest")
    return runtime, staged, observation


def test_same_id_but_one_changed_runtime_file_hash_cannot_create_verification(tmp_path: Path) -> None:
    runtime, _staged, observation = _stage(tmp_path)
    tampered = dict(observation)
    tampered["files"] = [dict(item) for item in observation["files"]]
    tampered["files"][0]["sha256"] = "sha256:" + "f" * 64

    with pytest.raises(M11cClientError) as exc:
        record_browser_launch_verification(
            runtime_root=runtime,
            caller_origin=ORIGIN,
            browser_observation=tampered,
        )
    assert exc.value.code == "browser_runtime_bundle_mismatch"
    assert not (runtime / "clients" / "browser-client-verification.json").exists()


def test_missing_runtime_package_file_cannot_create_verification(tmp_path: Path) -> None:
    runtime, _staged, observation = _stage(tmp_path)
    missing = dict(observation)
    missing["files"] = list(observation["files"][:-1])
    missing["file_count"] = len(missing["files"])
    missing["total_bytes"] = sum(item["size"] for item in missing["files"])

    with pytest.raises(M11cClientError) as exc:
        record_browser_launch_verification(
            runtime_root=runtime,
            caller_origin=ORIGIN,
            browser_observation=missing,
        )
    assert exc.value.code == "browser_runtime_bundle_mismatch"


def test_exact_runtime_package_observation_creates_digest_bound_witness(tmp_path: Path) -> None:
    runtime, staged, observation = _stage(tmp_path)
    witness = record_browser_launch_verification(
        runtime_root=runtime,
        caller_origin=ORIGIN,
        browser_observation=observation,
    )
    assert witness["browser_bundle_digest"] == staged["plan"]["browser_bundle_digest"]
    assert witness["native_launch_verified"] is True
    assert witness["production_activation_performed"] is False

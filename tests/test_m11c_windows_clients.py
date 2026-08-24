from __future__ import annotations

from pathlib import Path

import pytest

from bdb_vnext.composition import BROWSER_EXTENSION_ID, NATIVE_HOST_NAME, PROTOCOL_GENERATION
import bdb_vnext.m11c_native_artifact as native_artifact
from bdb_shared.evidence import canonical_json_bytes
from bdb_vnext.m11c_windows_clients import (
    BROWSER_INSTALL_MODE,
    M11cClientError,
    inspect_browser_bundle,
    query_client_plan,
    record_browser_launch_verification,
    require_client_verification,
    stage_client_plan,
)
from bdb_vnext.m9b_native_host import (
    M9B_NATIVE_REQUEST_SCHEMA,
    VNextNativeConfig,
    _parser,
    handle_message,
)


ROOT = Path(__file__).resolve().parents[1]
BROWSER_SOURCE = ROOT / "browser_extension_vnext"
HEAD = "a" * 40
TREE = "b" * 40
ORIGIN = f"chrome-extension://{BROWSER_EXTENSION_ID}/"


def _stage(tmp_path: Path):
    runtime = tmp_path / "runtime"
    legacy = tmp_path / "legacy"
    bootstrap = tmp_path / "ProgramData" / "BartoszDevBridge-Next" / "bootstrap"
    executable = tmp_path / "Scripts" / "bdb-vnext-native-host.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"m11c-test-native-executable")
    result = stage_client_plan(
        runtime_root=runtime,
        legacy_runtime_root=legacy,
        bootstrap_authority_root=bootstrap,
        browser_source_root=BROWSER_SOURCE,
        native_host_executable=executable,
        source_head=HEAD,
        source_tree=TREE,
    )
    return runtime, legacy, bootstrap, executable, result


def _browser_observation(result: dict[str, object]) -> dict[str, object]:
    observation = dict(result["browser"])  # type: ignore[arg-type]
    observation.pop("bundle_digest")
    return observation


def test_browser_bundle_identity_is_content_addressed_and_pinned() -> None:
    observed = inspect_browser_bundle(BROWSER_SOURCE)
    assert observed["extension_id"] == BROWSER_EXTENSION_ID
    assert observed["bundle_digest"].startswith("sha256:")
    assert observed["file_count"] >= 6


def test_stage_client_plan_copies_exact_browser_and_builds_native_manifest(tmp_path: Path) -> None:
    runtime, _legacy, _bootstrap, executable, result = _stage(tmp_path)
    plan = result["plan"]
    assert plan["source_head"] == HEAD
    assert plan["source_tree"] == TREE
    assert plan["browser_extension_id"] == BROWSER_EXTENSION_ID
    assert plan["browser_install_mode"] == BROWSER_INSTALL_MODE
    assert plan["browser_operator_action_required"] is True
    assert Path(plan["browser_bundle_root"]) == runtime / "clients" / "browser-extension"
    staged_executable = Path(plan["native_host_executable"])
    assert staged_executable == runtime / "clients" / "native-host" / executable.name
    assert staged_executable.read_bytes() == executable.read_bytes()
    assert plan["native_host_name"] == NATIVE_HOST_NAME
    manifest = Path(plan["native_manifest_path"]).read_text(encoding="utf-8")
    assert NATIVE_HOST_NAME in manifest
    assert ORIGIN in manifest
    assert query_client_plan(runtime_root=runtime)["plan"]["client_plan_sha256"] == plan["client_plan_sha256"]


def test_stage_client_plan_copies_and_verifies_complete_onedir_payload(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifact"
    dependency = artifact_root / "_internal" / "VCRUNTIME140.dll"
    dependency.parent.mkdir(parents=True)
    dependency.write_bytes(b"runtime-dependency")
    executable = artifact_root / native_artifact.NATIVE_EXECUTABLE_NAME
    executable.write_bytes(b"native-executable")
    files, total, payload_digest = native_artifact._payload_inventory(artifact_root)
    payload = {
        "schema": native_artifact.NATIVE_ARTIFACT_SCHEMA,
        "runtime_id": "devmaster.bdb.vnext.runtime",
        "generation_id": "bdb-vnext-g1",
        "protocol_generation": PROTOCOL_GENERATION,
        "native_host_name": NATIVE_HOST_NAME,
        "browser_extension_id": BROWSER_EXTENSION_ID,
        "artifact_kind": "pyinstaller-onedir",
        "source_head": HEAD,
        "source_tree": TREE,
        "entrypoint": native_artifact.NATIVE_ENTRYPOINT,
        "python_version": "3.14.0",
        "pyinstaller_version": "6.14.0",
        "platform": "windows-x86_64",
        "executable": {"name": executable.name, "size_bytes": executable.stat().st_size, "sha256": native_artifact._sha256_path(executable)},
        "payload": {
            "files": [{"path": path, "size_bytes": size, "sha256": digest} for path, size, digest in files],
            "total_size_bytes": total,
            "sha256": payload_digest,
        },
        "production_activation_performed": False,
    }
    document = {**payload, "manifest_sha256": native_artifact._document_digest(payload)}
    (artifact_root / native_artifact.NATIVE_ARTIFACT_MANIFEST).write_bytes(canonical_json_bytes(document))
    runtime = tmp_path / "runtime"
    result = stage_client_plan(
        runtime_root=runtime,
        legacy_runtime_root=tmp_path / "legacy",
        bootstrap_authority_root=tmp_path / "bootstrap",
        browser_source_root=BROWSER_SOURCE,
        native_host_executable=executable,
        source_head=HEAD,
        source_tree=TREE,
    )
    plan = result["plan"]
    assert plan["native_artifact_kind"] == "pyinstaller-onedir"
    assert plan["native_payload_sha256"] == payload_digest
    assert (runtime / "clients" / "native-host" / "_internal" / "VCRUNTIME140.dll").read_bytes() == b"runtime-dependency"
    assert query_client_plan(runtime_root=runtime)["plan"] == plan


def test_conflicting_staged_native_bytes_fail_closed(tmp_path: Path) -> None:
    runtime, _legacy, _bootstrap, _executable, result = _stage(tmp_path)
    staged = Path(result["plan"]["native_host_executable"])
    staged.write_bytes(b"foreign-native-bytes")
    with pytest.raises(M11cClientError) as exc:
        stage_client_plan(
            runtime_root=runtime,
            legacy_runtime_root=tmp_path / "legacy",
            bootstrap_authority_root=tmp_path / "ProgramData" / "BartoszDevBridge-Next" / "bootstrap",
            browser_source_root=BROWSER_SOURCE,
            native_host_executable=tmp_path / "Scripts" / "bdb-vnext-native-host.exe",
            source_head=HEAD,
            source_tree=TREE,
        )
    assert exc.value.code == "native_stage_conflict"


def test_staged_browser_tamper_is_rejected(tmp_path: Path) -> None:
    runtime, *_rest, result = _stage(tmp_path)
    staged = Path(result["plan"]["browser_bundle_root"]) / "popup.js"
    staged.write_text(staged.read_text(encoding="utf-8") + "\n// tamper\n", encoding="utf-8")
    with pytest.raises(M11cClientError) as exc:
        query_client_plan(runtime_root=runtime)
    assert exc.value.code == "browser_bundle_stale"


def test_only_real_pinned_chrome_origin_can_publish_client_verification(tmp_path: Path) -> None:
    runtime, *_rest, result = _stage(tmp_path)
    plan = result["plan"]
    with pytest.raises(M11cClientError) as exc:
        record_browser_launch_verification(runtime_root=runtime, caller_origin="chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/")
    assert exc.value.code == "browser_origin_mismatch"

    witness = record_browser_launch_verification(
        runtime_root=runtime,
        caller_origin=ORIGIN,
        browser_observation=_browser_observation(result),
    )
    assert witness["native_launch_verified"] is True
    assert witness["client_plan_sha256"] == plan["client_plan_sha256"]
    assert require_client_verification(
        runtime_root=runtime,
        expected_client_plan_sha256=plan["client_plan_sha256"],
    )["verification_sha256"] == witness["verification_sha256"]


def test_native_handshake_from_chrome_origin_records_same_non_authoritative_witness(tmp_path: Path) -> None:
    runtime, legacy, bootstrap, _executable, result = _stage(tmp_path)
    config = VNextNativeConfig.from_json(result["plan"]["native_config_path"])
    response = handle_message(
        config,
        {
            "schema": M9B_NATIVE_REQUEST_SCHEMA,
            "request_id": "handshake-real-browser",
            "action": "handshake",
            "protocol_generation": PROTOCOL_GENERATION,
            "browser_extension_id": BROWSER_EXTENSION_ID,
            "browser_observation": _browser_observation(result),
        },
        caller_origin=ORIGIN,
    )
    assert response["status"] == "success"
    assert response["activation"]["production_acceptance"] is False
    assert response["client_verification_sha256"].startswith("sha256:")
    witness = require_client_verification(
        runtime_root=runtime,
        expected_client_plan_sha256=result["plan"]["client_plan_sha256"],
    )
    assert witness["browser_bundle_digest"] == result["plan"]["browser_bundle_digest"]
    assert witness["production_activation_performed"] is False
    assert config.runtime_root == runtime.absolute()
    assert config.legacy_runtime_root == legacy.absolute()
    assert config.bootstrap_authority_root == bootstrap.absolute()


def test_native_parser_accepts_real_chrome_process_arguments() -> None:
    parsed = _parser().parse_args([ORIGIN, "--parent-window=123"])
    assert parsed.caller_origin == ORIGIN
    assert parsed.parent_window == "123"

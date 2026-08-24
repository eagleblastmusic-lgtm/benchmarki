from __future__ import annotations

from pathlib import Path

import pytest

import bdb_vnext.m11c_native_artifact as artifact
from bdb_shared.evidence import canonical_json_bytes
from bdb_vnext.bootstrap import inspect_runtime_bundle, run_health_check
from bdb_vnext.composition import BROWSER_EXTENSION_ID, GENERATION_ID, NATIVE_HOST_NAME, PROTOCOL_GENERATION, RUNTIME_ID


HEAD = "a" * 40
TREE = "b" * 40


def test_onedir_native_artifact_is_built_windowless() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "bdb_vnext" / "m11c_native_artifact.py"
    ).read_text(encoding="utf-8")

    assert '"--onedir"' in source
    assert '"--windowed"' in source
    assert '"--console"' not in source


def _fake_artifact(tmp_path: Path) -> Path:
    root = tmp_path / "artifact"
    root.mkdir()
    exe = root / artifact.NATIVE_EXECUTABLE_NAME
    exe.write_bytes(b"frozen-vnext-native-host")
    dependency = root / "_internal" / "VCRUNTIME140.dll"
    dependency.parent.mkdir()
    dependency.write_bytes(b"repo-local-runtime-dependency")
    files, total, payload_digest = artifact._payload_inventory(root)
    payload = {
        "schema": artifact.NATIVE_ARTIFACT_SCHEMA,
        "runtime_id": RUNTIME_ID,
        "generation_id": GENERATION_ID,
        "protocol_generation": PROTOCOL_GENERATION,
        "native_host_name": NATIVE_HOST_NAME,
        "browser_extension_id": BROWSER_EXTENSION_ID,
        "artifact_kind": "pyinstaller-onedir",
        "source_head": HEAD,
        "source_tree": TREE,
        "entrypoint": artifact.NATIVE_ENTRYPOINT,
        "python_version": "3.12.0",
        "pyinstaller_version": "6.14.0",
        "platform": "windows-x86_64",
        "executable": {
            "name": artifact.NATIVE_EXECUTABLE_NAME,
            "size_bytes": exe.stat().st_size,
            "sha256": artifact._sha256_path(exe),
        },
        "payload": {
            "files": [{"path": path, "size_bytes": size, "sha256": digest} for path, size, digest in files],
            "total_size_bytes": total,
            "sha256": payload_digest,
        },
        "production_activation_performed": False,
    }
    document = {**payload, "manifest_sha256": artifact._document_digest(payload)}
    manifest = root / artifact.NATIVE_ARTIFACT_MANIFEST
    manifest.write_bytes(canonical_json_bytes(document))
    return manifest


def test_verified_artifact_binds_exact_source_and_executable(tmp_path: Path) -> None:
    manifest = _fake_artifact(tmp_path)
    verified = artifact.verify_native_artifact(manifest, expected_source_head=HEAD, expected_source_tree=TREE)
    assert verified.executable_sha256.startswith("sha256:")
    assert verified.manifest_sha256.startswith("sha256:")
    assert verified.artifact_kind == "pyinstaller-onedir"
    assert any(path == "_internal/VCRUNTIME140.dll" for path, _size, _digest in verified.payload_files)
    with pytest.raises(artifact.M11cArtifactError) as caught:
        artifact.verify_native_artifact(manifest, expected_source_head="c" * 40)
    assert caught.value.code == "artifact_source_mismatch"


def test_legacy_onefile_artifact_remains_verifiable(tmp_path: Path) -> None:
    root = tmp_path / "legacy-artifact"
    root.mkdir()
    executable = root / artifact.NATIVE_EXECUTABLE_NAME
    executable.write_bytes(b"historical-onefile-native-host")
    payload = {
        "schema": artifact.NATIVE_ARTIFACT_SCHEMA_V1,
        "runtime_id": RUNTIME_ID,
        "generation_id": GENERATION_ID,
        "protocol_generation": PROTOCOL_GENERATION,
        "native_host_name": NATIVE_HOST_NAME,
        "browser_extension_id": BROWSER_EXTENSION_ID,
        "artifact_kind": "pyinstaller-onefile",
        "source_head": HEAD,
        "source_tree": TREE,
        "entrypoint": artifact.NATIVE_ENTRYPOINT,
        "python_version": "3.12.0",
        "pyinstaller_version": "6.14.0",
        "platform": "windows-x86_64",
        "executable": {
            "name": artifact.NATIVE_EXECUTABLE_NAME,
            "size_bytes": executable.stat().st_size,
            "sha256": artifact._sha256_path(executable),
        },
        "production_activation_performed": False,
    }
    document = {**payload, "manifest_sha256": artifact._document_digest(payload)}
    manifest = root / "bdb-vnext-native-host-artifact-v1.json"
    manifest.write_bytes(canonical_json_bytes(document))

    verified = artifact.verify_native_artifact(
        manifest,
        expected_source_head=HEAD,
        expected_source_tree=TREE,
    )

    assert verified.artifact_kind == "pyinstaller-onefile"
    assert verified.payload_files == (
        (artifact.NATIVE_EXECUTABLE_NAME, executable.stat().st_size, artifact._sha256_path(executable)),
    )


def test_artifact_tamper_is_rejected(tmp_path: Path) -> None:
    manifest = _fake_artifact(tmp_path)
    (manifest.parent / artifact.NATIVE_EXECUTABLE_NAME).write_bytes(b"tampered")
    with pytest.raises(artifact.M11cArtifactError) as caught:
        artifact.verify_native_artifact(manifest)
    assert caught.value.code == "native_artifact_tampered"


def test_onedir_dependency_tamper_is_rejected(tmp_path: Path) -> None:
    manifest = _fake_artifact(tmp_path)
    (manifest.parent / "_internal" / "VCRUNTIME140.dll").write_bytes(b"tampered")
    with pytest.raises(artifact.M11cArtifactError) as caught:
        artifact.verify_native_artifact(manifest)
    assert caught.value.code == "native_artifact_tampered"


def test_materialized_runtime_bundle_is_exact_and_health_ready(tmp_path: Path) -> None:
    manifest = _fake_artifact(tmp_path)
    legacy = tmp_path / "legacy"
    candidate_root = tmp_path / "candidate"
    result = artifact.materialize_runtime_bundle(
        artifact_manifest=manifest,
        output_root=candidate_root,
        legacy_runtime_root=legacy,
        role="candidate",
        bundle_id="bdb-vnext-frozen-candidate",
        known_good=True,
    )
    bundle = inspect_runtime_bundle(
        candidate_root,
        expected_role="candidate",
        expected_sha256=result["bundle_sha256"],
        legacy_runtime_root=legacy,
    )
    health = run_health_check(bundle, required_control_schema=1, legacy_runtime_root=legacy, timeout_seconds=10.0)
    assert health["status"] == "READY"
    assert bundle.source_commit == HEAD
    assert (candidate_root / artifact.NATIVE_EXECUTABLE_NAME).is_file()
    assert (candidate_root / "_internal" / "VCRUNTIME140.dll").is_file()
    assert (candidate_root / artifact.NATIVE_ARTIFACT_MANIFEST).is_file()
    assert (candidate_root / artifact.RUNTIME_PROVENANCE_NAME).is_file()


def test_recovery_bundle_requires_known_good(tmp_path: Path) -> None:
    manifest = _fake_artifact(tmp_path)
    with pytest.raises(artifact.M11cArtifactError) as caught:
        artifact.materialize_runtime_bundle(
            artifact_manifest=manifest,
            output_root=tmp_path / "recovery",
            legacy_runtime_root=tmp_path / "legacy",
            role="recovery",
            bundle_id="bdb-vnext-recovery",
            known_good=False,
        )
    assert caught.value.code == "recovery_not_known_good"

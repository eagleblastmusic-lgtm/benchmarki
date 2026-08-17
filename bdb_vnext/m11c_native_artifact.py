"""Build-only vNext Native Host artifact and Bootstrap bundle materialization.

The production Browser/Native route must never be bound merely to a Python
console-script launcher that imports mutable repo/site-packages state.  This
module builds and verifies a Windows PyInstaller onefile executable, publishes
an immutable provenance manifest for its exact Git subject, and can wrap those
same executable bytes in the M1b/M11a runtime-bundle contract.

Nothing here installs, registers, starts, activates, or switches production.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NoReturn

from bdb_shared.evidence import canonical_json_bytes
from bdb_vnext.bootstrap import BUNDLE_SCHEMA, HEALTH_SCHEMA, BootstrapError, _absolute_path
from bdb_vnext.composition import (
    BROWSER_EXTENSION_ID,
    GENERATION_ID,
    NATIVE_HOST_NAME,
    PROTOCOL_GENERATION,
    RUNTIME_ID,
    observe_bundle,
)


NATIVE_ARTIFACT_SCHEMA = "bdb-vnext-native-host-artifact-v1"
NATIVE_ARTIFACT_MANIFEST = "bdb-vnext-native-host-artifact-v1.json"
NATIVE_EXECUTABLE_NAME = "BDB-vNext-NativeHost.exe"
NATIVE_ENTRYPOINT = "packaging/windows/vnext_native_host_entry.py"
RUNTIME_PROVENANCE_SCHEMA = "bdb-vnext-runtime-bundle-provenance-v1"
RUNTIME_PROVENANCE_NAME = "source-provenance.json"

_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_MAX_ARTIFACT_BYTES = 512 * 1024 * 1024


class M11cArtifactError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> NoReturn:
    raise M11cArtifactError(code, message)


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _document_digest(value: Mapping[str, Any]) -> str:
    return _sha256_bytes(canonical_json_bytes(value))


def _sha40(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA40.fullmatch(value) is None:
        _fail("invalid_source_identity", f"{field} must be an exact 40-character Git SHA")
    return value


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        _fail("invalid_digest", f"{field} must be an exact sha256 digest")
    return value


def _bounded_id(value: object, field: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        _fail("invalid_identifier", f"{field} is invalid")
    return value


def _git(repo: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise M11cArtifactError("git_identity_unavailable", "Git source identity could not be observed") from exc
    if completed.returncode != 0:
        _fail("git_identity_unavailable", "Git source identity could not be observed")
    return completed.stdout.decode("utf-8", errors="strict").strip()


def exact_git_subject(repo_root: str | Path) -> dict[str, str]:
    repo = _absolute_path(repo_root, field="repo_root")
    if not (repo / ".git").exists():
        # Worktrees normally expose .git as a file; both file and directory are valid.
        git_marker = repo / ".git"
        if not git_marker.is_file():
            _fail("git_repository_required", "artifact build requires an exact Git checkout/worktree")
    status = _git(repo, "status", "--porcelain=v1", "--untracked-files=normal")
    if status:
        _fail("dirty_source_subject", "artifact build requires a clean Git subject")
    head = _sha40(_git(repo, "rev-parse", "HEAD"), "source_head")
    tree = _sha40(_git(repo, "rev-parse", "HEAD^{tree}"), "source_tree")
    return {"source_head": head, "source_tree": tree}


@dataclass(frozen=True)
class VerifiedNativeArtifact:
    manifest_path: Path
    executable_path: Path
    source_head: str
    source_tree: str
    executable_sha256: str
    executable_size_bytes: int
    manifest_sha256: str


def _load_manifest(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 256 * 1024:
        _fail("artifact_manifest_invalid", "Native Host artifact manifest must be a bounded regular file")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise M11cArtifactError("artifact_manifest_invalid", "Native Host artifact manifest is not valid JSON") from exc
    if not isinstance(document, dict):
        _fail("artifact_manifest_invalid", "Native Host artifact manifest must be an object")
    return document


def verify_native_artifact(
    manifest_path: str | Path,
    *,
    expected_source_head: str | None = None,
    expected_source_tree: str | None = None,
) -> VerifiedNativeArtifact:
    manifest = _absolute_path(manifest_path, field="artifact_manifest")
    document = _load_manifest(manifest)
    expected_fields = {
        "schema",
        "runtime_id",
        "generation_id",
        "protocol_generation",
        "native_host_name",
        "browser_extension_id",
        "artifact_kind",
        "source_head",
        "source_tree",
        "entrypoint",
        "python_version",
        "pyinstaller_version",
        "platform",
        "executable",
        "production_activation_performed",
        "manifest_sha256",
    }
    if set(document) != expected_fields or (
        document.get("schema") != NATIVE_ARTIFACT_SCHEMA
        or document.get("runtime_id") != RUNTIME_ID
        or document.get("generation_id") != GENERATION_ID
        or document.get("protocol_generation") != PROTOCOL_GENERATION
        or document.get("native_host_name") != NATIVE_HOST_NAME
        or document.get("browser_extension_id") != BROWSER_EXTENSION_ID
        or document.get("artifact_kind") != "pyinstaller-onefile"
        or document.get("entrypoint") != NATIVE_ENTRYPOINT
        or document.get("platform") != "windows-x86_64"
        or document.get("production_activation_performed") is not False
    ):
        _fail("artifact_manifest_identity_mismatch", "Native Host artifact identity differs")
    source_head = _sha40(document.get("source_head"), "source_head")
    source_tree = _sha40(document.get("source_tree"), "source_tree")
    if expected_source_head is not None and source_head != _sha40(expected_source_head, "expected_source_head"):
        _fail("artifact_source_mismatch", "Native Host artifact source HEAD differs")
    if expected_source_tree is not None and source_tree != _sha40(expected_source_tree, "expected_source_tree"):
        _fail("artifact_source_mismatch", "Native Host artifact source tree differs")
    executable = document.get("executable")
    if not isinstance(executable, dict) or set(executable) != {"name", "size_bytes", "sha256"}:
        _fail("artifact_manifest_invalid", "Native Host executable receipt is invalid")
    if executable.get("name") != NATIVE_EXECUTABLE_NAME:
        _fail("artifact_manifest_identity_mismatch", "Native Host executable name differs")
    size = executable.get("size_bytes")
    if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= _MAX_ARTIFACT_BYTES:
        _fail("artifact_manifest_invalid", "Native Host executable size is invalid")
    expected_digest = _digest(executable.get("sha256"), "executable.sha256")
    supplied_manifest_digest = _digest(document.get("manifest_sha256"), "manifest_sha256")
    payload = dict(document)
    payload.pop("manifest_sha256", None)
    if _document_digest(payload) != supplied_manifest_digest:
        _fail("artifact_manifest_digest_mismatch", "Native Host artifact manifest digest differs")
    executable_path = manifest.parent / NATIVE_EXECUTABLE_NAME
    if executable_path.is_symlink() or not executable_path.is_file():
        _fail("native_artifact_missing", "Native Host onefile executable is missing")
    observed_size = executable_path.stat().st_size
    observed_digest = _sha256_path(executable_path)
    if observed_size != size or observed_digest != expected_digest:
        _fail("native_artifact_tampered", "Native Host onefile bytes differ from artifact manifest")
    return VerifiedNativeArtifact(
        manifest_path=manifest,
        executable_path=executable_path,
        source_head=source_head,
        source_tree=source_tree,
        executable_sha256=observed_digest,
        executable_size_bytes=observed_size,
        manifest_sha256=supplied_manifest_digest,
    )


def build_windows_native_artifact(
    *,
    repo_root: str | Path,
    output_root: str | Path,
) -> VerifiedNativeArtifact:
    """Build one frozen vNext Native Host artifact from one clean Git subject."""

    if os.name != "nt":
        _fail("windows_required", "vNext Native Host artifact build requires Windows")
    repo = _absolute_path(repo_root, field="repo_root")
    output = _absolute_path(output_root, field="output_root")
    subject = exact_git_subject(repo)
    entrypoint = repo / NATIVE_ENTRYPOINT
    if not entrypoint.is_file():
        _fail("entrypoint_missing", "dedicated vNext Native Host packaging entrypoint is missing")
    try:
        import PyInstaller  # type: ignore[import-not-found]
    except ImportError as exc:
        raise M11cArtifactError("pyinstaller_unavailable", "PyInstaller release dependency is required") from exc
    if output.exists() and any(output.iterdir()):
        _fail("artifact_output_not_empty", "Native Host artifact output root must be empty")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bdb-vnext-native-build-") as temporary:
        temp = Path(temporary)
        dist = temp / "dist"
        work = temp / "work"
        spec = temp / "spec"
        spec.mkdir()
        command = [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onefile",
            "--console",
            "--name",
            NATIVE_EXECUTABLE_NAME.removesuffix(".exe"),
            "--paths",
            str(repo),
            "--collect-submodules",
            "bdb_vnext",
            "--collect-data",
            "bdb_vnext",
            "--distpath",
            str(dist),
            "--workpath",
            str(work),
            "--specpath",
            str(spec),
            str(entrypoint),
        ]
        completed = subprocess.run(
            command,
            cwd=repo,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=300,
            check=False,
        )
        if completed.returncode != 0:
            tail = completed.stdout.decode("utf-8", errors="replace")[-4000:]
            raise M11cArtifactError("pyinstaller_build_failed", f"vNext Native Host onefile build failed: {tail}")
        built = dist / NATIVE_EXECUTABLE_NAME
        if not built.is_file():
            _fail("pyinstaller_output_missing", "PyInstaller did not produce the expected Native Host executable")
        target = output / NATIVE_EXECUTABLE_NAME
        shutil.copyfile(built, target)

    size = target.stat().st_size
    digest = _sha256_path(target)
    payload = {
        "schema": NATIVE_ARTIFACT_SCHEMA,
        "runtime_id": RUNTIME_ID,
        "generation_id": GENERATION_ID,
        "protocol_generation": PROTOCOL_GENERATION,
        "native_host_name": NATIVE_HOST_NAME,
        "browser_extension_id": BROWSER_EXTENSION_ID,
        "artifact_kind": "pyinstaller-onefile",
        "source_head": subject["source_head"],
        "source_tree": subject["source_tree"],
        "entrypoint": NATIVE_ENTRYPOINT,
        "python_version": platform.python_version(),
        "pyinstaller_version": str(PyInstaller.__version__),
        "platform": "windows-x86_64",
        "executable": {"name": NATIVE_EXECUTABLE_NAME, "size_bytes": size, "sha256": digest},
        "production_activation_performed": False,
    }
    document = {**payload, "manifest_sha256": _document_digest(payload)}
    manifest = output / NATIVE_ARTIFACT_MANIFEST
    manifest.write_bytes(canonical_json_bytes(document))
    return verify_native_artifact(
        manifest,
        expected_source_head=subject["source_head"],
        expected_source_tree=subject["source_tree"],
    )


def _health_source(
    *,
    bundle_id: str,
    schema_min: int,
    schema_max: int,
    artifact_manifest_sha256: str,
    executable_sha256: str,
    executable_size: int,
) -> str:
    return f'''from __future__ import annotations
import hashlib, json, pathlib, sys
BUNDLE_ID = {bundle_id!r}
SCHEMA_MIN = {schema_min!r}
SCHEMA_MAX = {schema_max!r}
MANIFEST_SHA = {artifact_manifest_sha256!r}
EXE_SHA = {executable_sha256!r}
EXE_SIZE = {executable_size!r}
root = pathlib.Path(__file__).resolve().parent
schema = int(next(value.split("=", 1)[1] for value in sys.argv if value.startswith("--control-schema=")))
if not SCHEMA_MIN <= schema <= SCHEMA_MAX:
    raise SystemExit(11)
manifest = root / {NATIVE_ARTIFACT_MANIFEST!r}
exe = root / {NATIVE_EXECUTABLE_NAME!r}
def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()
if not manifest.is_file() or sha(manifest) != MANIFEST_SHA:
    raise SystemExit(12)
if not exe.is_file() or exe.stat().st_size != EXE_SIZE or sha(exe) != EXE_SHA:
    raise SystemExit(13)
print(json.dumps({{"schema": {HEALTH_SCHEMA!r}, "status": "READY", "runtime_id": {RUNTIME_ID!r}, "bundle_id": BUNDLE_ID, "observed_control_schema": schema}}, sort_keys=True, separators=(",", ":")))
'''


def materialize_runtime_bundle(
    *,
    artifact_manifest: str | Path,
    output_root: str | Path,
    legacy_runtime_root: str | Path,
    role: Literal["candidate", "recovery"],
    bundle_id: str,
    known_good: bool,
    supported_control_schema: tuple[int, int] = (1, 1),
) -> dict[str, Any]:
    """Create one exact runtime bundle around the verified frozen Native Host."""

    if role not in {"candidate", "recovery"}:
        _fail("invalid_bundle_role", "runtime bundle role is invalid")
    bundle_id = _bounded_id(bundle_id, "bundle_id")
    if role == "recovery" and known_good is not True:
        _fail("recovery_not_known_good", "recovery runtime bundle must be explicitly known-good")
    low, high = supported_control_schema
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (low, high)) or low > high:
        _fail("invalid_schema_range", "supported Control schema range is invalid")
    artifact = verify_native_artifact(artifact_manifest)
    output = _absolute_path(output_root, field="runtime_bundle_output")
    legacy = _absolute_path(legacy_runtime_root, field="legacy_runtime_root")
    if output.exists():
        _fail("runtime_bundle_exists", "runtime bundle output already exists")
    output.mkdir(parents=True)
    shutil.copyfile(artifact.executable_path, output / NATIVE_EXECUTABLE_NAME)
    shutil.copyfile(artifact.manifest_path, output / NATIVE_ARTIFACT_MANIFEST)
    provenance_payload = {
        "schema": RUNTIME_PROVENANCE_SCHEMA,
        "runtime_id": RUNTIME_ID,
        "generation_id": GENERATION_ID,
        "source_head": artifact.source_head,
        "source_tree": artifact.source_tree,
        "native_artifact_manifest_sha256": artifact.manifest_sha256,
        "native_executable_sha256": artifact.executable_sha256,
        "production_activation_performed": False,
    }
    provenance = {**provenance_payload, "provenance_sha256": _document_digest(provenance_payload)}
    (output / RUNTIME_PROVENANCE_NAME).write_bytes(canonical_json_bytes(provenance))
    # Health pins the exact copied manifest file bytes as well as the executable.
    copied_manifest_sha = _sha256_path(output / NATIVE_ARTIFACT_MANIFEST)
    (output / "health.py").write_text(
        _health_source(
            bundle_id=bundle_id,
            schema_min=low,
            schema_max=high,
            artifact_manifest_sha256=copied_manifest_sha,
            executable_sha256=artifact.executable_sha256,
            executable_size=artifact.executable_size_bytes,
        ),
        encoding="utf-8",
        newline="\n",
    )
    bundle_manifest = {
        "schema": BUNDLE_SCHEMA,
        "runtime_id": RUNTIME_ID,
        "bundle_id": bundle_id,
        "role": role,
        "source_commit": artifact.source_head,
        "supported_control_schema": {"min": low, "max": high},
        "known_good": bool(known_good),
        "health_entrypoint": "health.py",
        "activation_policy": {"candidate_may_write_final_pointer": False},
    }
    (output / "bundle.json").write_bytes(canonical_json_bytes(bundle_manifest))
    try:
        observed = observe_bundle(RUNTIME_ID, output, legacy_runtime_root=legacy)
    except Exception as exc:
        shutil.rmtree(output, ignore_errors=True)
        if isinstance(exc, BootstrapError):
            raise
        raise M11cArtifactError("bundle_observation_failed", "materialized runtime bundle could not be observed") from exc
    return {
        "bundle_root": str(output),
        "bundle_sha256": observed["sha256"],
        "source_head": artifact.source_head,
        "source_tree": artifact.source_tree,
        "native_artifact_manifest_sha256": artifact.manifest_sha256,
        "native_executable_sha256": artifact.executable_sha256,
        "role": role,
        "bundle_id": bundle_id,
        "known_good": bool(known_good),
        "production_activation_performed": False,
    }


__all__ = [
    "M11cArtifactError",
    "NATIVE_ARTIFACT_MANIFEST",
    "NATIVE_ARTIFACT_SCHEMA",
    "NATIVE_EXECUTABLE_NAME",
    "VerifiedNativeArtifact",
    "build_windows_native_artifact",
    "exact_git_subject",
    "materialize_runtime_bundle",
    "verify_native_artifact",
]

"""Build-only N6 normal-Browser rehearsal substrate.

This module is intentionally a rehearsal boundary, not a product runtime.  It
owns no lifecycle truth: Browser messages are validated here, then delegated to
the existing M3/M4/N2/N3/N4 authorities through ``VNextCompositionRoot``.  The
generated MV3 package only observes an explicitly marked user conversation and
keeps raw Browser evidence in the existing N3 Content CAS/Evidence store.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Mapping

from bdb_shared.evidence import canonical_json_bytes, semantic_digest
from bdb_vnext.candidate import (
    CANDIDATE_DIVERGED,
    CANDIDATE_INVALIDATED,
    CANDIDATE_OBSERVED,
    CANDIDATE_PREPARED,
    CANDIDATE_SEALED,
    CANDIDATE_UNKNOWN,
    CandidateError,
)
from bdb_vnext.engineering_loop import (
    EditBatch,
    EDIT_SCHEMA,
    EditorPort,
    ValidationCommand,
    ValidationPolicy,
    ValidationRunner,
    parse_edit_artifact,
)
from bdb_vnext.composition import (
    BROWSER_COMPONENT_ID,
    NATIVE_HOST_NAME,
    PROTOCOL_GENERATION,
    build_vnext_composition_manifest,
    load_browser_identity,
)
from bdb_vnext.m3a_submission import ShadowSubmissionRequest
from bdb_vnext.n4_publication import N4Error
from bdb_vnext.provider_root import VNextCompositionRoot
from bdb_vnext.repo_view import RepositoryResource


N6_PACKAGE_SCHEMA = "bdb-vnext-n6-rehearsal-package-v3"
N6_CONFIG_SCHEMA = "bdb-vnext-n6-rehearsal-config-v3"
N6_EXECUTION_SCHEMA = "bdb-vnext-n6-execution-manifest-v3"
N6_INTERPRETER_SCHEMA = "bdb-vnext-n6-external-interpreter-identity-v1"
N6_BROWSER_NATIVE_BINDING_SCHEMA = "bdb-vnext-n6-browser-native-binding-v1"
N6_EVENT_SCHEMA = "bdb-vnext-n6-browser-event-v1"
N6_NATIVE_REQUEST_SCHEMA = "bdb-vnext-n6-native-request-v2"
N6_NATIVE_RESPONSE_SCHEMA = "bdb-vnext-n6-native-response-v2"
N6_PACKAGE_VERSION = "0.3.0"
N6_NATIVE_CODE_SCHEMA = "bdb-vnext-n6-native-code-v1"
N6_PROTOCOL_GENERATION = "bdb-vnext-n6-protocol-v2"
N6_NATIVE_HOST_NAME = NATIVE_HOST_NAME
N6_BROWSER_COMPONENT = BROWSER_COMPONENT_ID
N6_CAPTURE_CHECKER_ID = "bdb-vnext-n6-browser-capture"
N6_CAPTURE_CHECKER_VERSION = "1"
N6_MAX_MESSAGE_BYTES = 1024 * 1024
N6_MAX_PROMPT_BYTES = 64 * 1024
N6_MAX_ANSWER_BYTES = 512 * 1024
P1_ENGINEERING_TARGET_SCHEMA = "bdb-vnext-p1-engineering-target-v1"
P1_ENGINEERING_PREFIX = "BDB-P1-CALC-BROWSER-E2E"
P1_ENGINEERING_INTENT_REVISION = "p1-calc-v1"
P1_ENGINEERING_CHECKER_ID = "bdb-vnext-p1-calculator-checker"
P1_ENGINEERING_CHECKER_VERSION = "1"


class N6RehearsalError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def _fail(code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
    raise N6RehearsalError(code, message, details=details)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _sha(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _text(value: object, field: str, *, max_bytes: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > max_bytes:
        _fail("invalid_payload", f"{field} must be a bounded non-empty string")
    return value


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail("invalid_payload", f"{field} must be an object")
    return value


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return canonical_json_bytes(dict(value))


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _fail("config_invalid", f"cannot read JSON: {path}", details={"error": type(exc).__name__})
    if not isinstance(value, dict):
        _fail("config_invalid", f"JSON root must be an object: {path}")
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_json_bytes(value) + b"\n")


def _safe_abs(value: object, field: str) -> Path:
    raw = str(value) if isinstance(value, Path) else value
    path = Path(_text(raw, field)).expanduser().absolute()
    if not path.is_absolute():
        _fail("path_invalid", f"{field} must be absolute")
    return path


def _overlap(left: Path, right: Path) -> bool:
    a, b = os.path.normcase(os.path.normpath(str(left))), os.path.normcase(os.path.normpath(str(right)))
    try:
        return os.path.commonpath((a, b)) in {a, b}
    except ValueError:
        return False


def _request_id(prefix: str) -> str:
    return f"{prefix}:{secrets.token_hex(12)}"


def _task_conversation(found: Mapping[str, Any]) -> str:
    task = _mapping(found.get("task"), "task")
    binding = _mapping(task.get("conversation_binding"), "task.conversation_binding")
    return _text(binding.get("conversation_id"), "task.conversation_binding.conversation_id")


def _stable_id(prefix: str, value: str) -> str:
    return f"{prefix}:{hashlib.sha256(value.encode('utf-8')).hexdigest()[:40]}"


def _package_files(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.name in {"execution_manifest.json", "native-config.json", "MANUAL_BROWSER_REHEARSAL_PACKET.md"}:
            continue
        relative_parts = path.relative_to(root).parts
        if relative_parts and relative_parts[0] in {".dotnet", "native-shim-src"}:
            continue
        relative = path.relative_to(root).as_posix()
        raw = path.read_bytes()
        records.append({"path": relative, "size_bytes": len(raw), "sha256": _sha(raw)})
    return records


def _normalized_execution_documents(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    config = _read_json(root / "native-config.json")
    execution = _read_json(root / "execution_manifest.json")
    if config.get("schema") != N6_CONFIG_SCHEMA or execution.get("schema") != N6_EXECUTION_SCHEMA:
        _fail("package_identity_invalid", "N6 execution-controlling documents have unsupported schemas")
    config = json.loads(json.dumps(config))
    execution = json.loads(json.dumps(execution))
    package = execution.get("package")
    if not isinstance(package, dict):
        _fail("package_identity_invalid", "N6 execution manifest lacks package identity")
    subject = execution.get("subject")
    resources = execution.get("resources")
    native_host = package.get("native_host")
    browser_extension = package.get("browser_extension")
    if not all(isinstance(item, dict) for item in (subject, resources, native_host, browser_extension)):
        _fail("package_identity_invalid", "N6 execution manifest identity sections are incomplete")
    if config.get("package_digest") != package.get("digest"):
        _fail("package_identity_claim_mismatch", "N6 config and execution manifest claim different package identities")
    config_binding = _validated_browser_native_binding(config.get("browser_native_binding"))
    execution_binding = _validated_browser_native_binding(package.get("browser_native_binding"))
    if config_binding != execution_binding:
        _fail("package_execution_binding_mismatch", "N6 config and execution manifest carry different Browser/Native bindings")
    coherence = (
        (config.get("source_commit"), subject.get("commit")),
        (config.get("repo_root"), subject.get("repo_root")),
        (config.get("package_root"), package.get("root")),
        (config.get("runtime_root"), resources.get("runtime_root")),
        (config.get("legacy_runtime_root"), resources.get("legacy_runtime_root")),
        (config.get("production_activation"), resources.get("production_activation")),
        (config.get("native_code_root"), native_host.get("code_root")),
        (config.get("native_code_digest"), package.get("native_code_digest")),
        (config.get("interpreter_identity"), package.get("interpreter_identity")),
        (config.get("browser_extension_id"), browser_extension.get("extension_id")),
        (config.get("native_host_name"), native_host.get("name")),
        (config.get("protocol_generation"), package.get("protocol_generation")),
        (config_binding.get("source_commit"), subject.get("commit")),
        (config_binding.get("source_tree"), subject.get("tree")),
        (config_binding.get("repo_view_id"), subject.get("view_id")),
        (config_binding.get("runtime_root"), resources.get("runtime_root")),
        (config_binding.get("package_root"), package.get("root")),
        (config_binding.get("legacy_runtime_root"), resources.get("legacy_runtime_root")),
        (config_binding.get("native_code_digest"), package.get("native_code_digest")),
        (config_binding.get("interpreter_identity_digest"), package.get("interpreter_identity", {}).get("identity_digest")),
        (config_binding.get("browser_extension_id"), browser_extension.get("extension_id")),
        (config_binding.get("native_host_name"), native_host.get("name")),
        (config_binding.get("protocol_generation"), package.get("protocol_generation")),
        (config_binding.get("production_activation"), resources.get("production_activation")),
    )
    if ("engineering_target" in config) != ("engineering_target" in package):
        _fail("package_execution_binding_mismatch", "N6 config and execution manifest disagree about the engineering target")
    if "engineering_target" in config:
        if config.get("engineering_target") != package.get("engineering_target"):
            _fail("package_execution_binding_mismatch", "N6 config and execution manifest bind different engineering targets")
        _validated_engineering_target(_mapping(config["engineering_target"], "engineering_target"), forbidden_roots=(_safe_abs(config["repo_root"], "repo_root"), _safe_abs(config["runtime_root"], "runtime_root"), _safe_abs(config["legacy_runtime_root"], "legacy_runtime_root"), _safe_abs(config["package_root"], "package_root"), _safe_abs(config["native_code_root"], "native_code_root")))
    if any(left != right for left, right in coherence):
        _fail("package_execution_binding_mismatch", "N6 config and execution manifest bind different execution subjects")
    config["package_digest"] = "<COMPOUND-IDENTITY>"
    package["digest"] = "<COMPOUND-IDENTITY>"
    return config, execution


def package_digest(root: str | Path) -> str:
    path = Path(root).expanduser().absolute()
    records = _package_files(path)
    if not records:
        _fail("package_empty", "N6 rehearsal package contains no immutable files")
    config, execution = _normalized_execution_documents(path)
    return _sha(_json_bytes({
        "schema": N6_PACKAGE_SCHEMA,
        "version": N6_PACKAGE_VERSION,
        "files": records,
        "native_config": config,
        "execution_manifest": execution,
    }))


def interpreter_identity(executable: str | Path) -> dict[str, Any]:
    """Attest the selected external interpreter without claiming its stdlib."""

    path = Path(executable).expanduser().absolute()
    if not path.is_file():
        _fail("interpreter_unavailable", "selected N6 Python executable is unavailable")
    probe_source = (
        "import json,os,platform,sys,sysconfig;"
        "print(json.dumps({'executable':os.path.abspath(sys.executable),"
        "'implementation':sys.implementation.name,'version':platform.python_version(),"
        "'cache_tag':sys.implementation.cache_tag,'platform':platform.platform(),"
        "'prefix':os.path.abspath(sys.prefix),'base_prefix':os.path.abspath(sys.base_prefix),"
        "'stdlib_path':os.path.abspath(sysconfig.get_paths()['stdlib'])},sort_keys=True,separators=(',',':')))"
    )
    try:
        completed = subprocess.run(
            [str(path), "-I", "-S", "-c", probe_source],
            shell=False,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        observed = json.loads(completed.stdout) if completed.returncode == 0 else None
        executable_digest = _sha(path.read_bytes())
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise N6RehearsalError("interpreter_unavailable", "selected N6 Python identity could not be observed") from exc
    if not isinstance(observed, dict):
        _fail("interpreter_unavailable", "selected N6 Python identity probe failed")
    identity = {
        "schema": N6_INTERPRETER_SCHEMA,
        "ownership": "EXTERNAL_NOT_PACKAGE_OWNED",
        "attestation_scope": "EXECUTABLE_BYTES_AND_RUNTIME_METADATA",
        "executable_path": str(path),
        "executable_digest": executable_digest,
        "implementation": observed.get("implementation"),
        "version": observed.get("version"),
        "cache_tag": observed.get("cache_tag"),
        "platform": observed.get("platform"),
        "prefix": observed.get("prefix"),
        "base_prefix": observed.get("base_prefix"),
        "stdlib_path": observed.get("stdlib_path"),
        "stdlib_ownership": "EXTERNAL_NOT_PACKAGE_OWNED",
        "stdlib_bytes_digest": "NOT_ATTESTED",
    }
    identity["identity_digest"] = semantic_digest(identity)
    return identity


def _browser_native_binding(
    *,
    source_commit: str,
    source_tree: str,
    repo_view_id: str,
    runtime_root: Path,
    package_root: Path,
    legacy_runtime_root: Path,
    native_code_digest_value: str,
    interpreter_identity_digest: str,
    browser_extension_id: str,
) -> dict[str, Any]:
    """Create the non-circular exact execution binding used for Native handshake.

    The final compound package digest includes this binding and the generated
    Browser code which carries it. The binding itself deliberately excludes the
    compound digest, avoiding a self-referential hash while still naming every
    execution-controlling authority required before a semantic Browser event.
    """

    identity: dict[str, Any] = {
        "schema": N6_BROWSER_NATIVE_BINDING_SCHEMA,
        "source_commit": source_commit,
        "source_tree": source_tree,
        "repo_view_id": repo_view_id,
        "runtime_root": str(runtime_root),
        "package_root": str(package_root),
        "legacy_runtime_root": str(legacy_runtime_root),
        "native_code_digest": native_code_digest_value,
        "interpreter_identity_digest": interpreter_identity_digest,
        "browser_extension_id": browser_extension_id,
        "native_host_name": N6_NATIVE_HOST_NAME,
        "protocol_generation": N6_PROTOCOL_GENERATION,
        "production_activation": False,
    }
    identity["binding_digest"] = semantic_digest(identity)
    return identity


def _validated_engineering_target(
    value: Mapping[str, Any],
    *,
    forbidden_roots: tuple[Path, ...] = (),
) -> dict[str, Any]:
    """Validate the optional disposable pilot subject as an exact RepoView.

    This is deliberately a package/config binding, not a new repository
    authority.  The target is resolved once from its committed object and
    every Browser artifact must carry the resulting view identity.
    """

    allowed = {"schema", "repository_id", "repo_root", "branch", "commit", "tree", "view_id", "allowed_paths", "checker"}
    if not isinstance(value, Mapping) or set(value) != allowed or value.get("schema") != P1_ENGINEERING_TARGET_SCHEMA:
        _fail("engineering_target_invalid", "engineering target fields/schema differ")
    root = _safe_abs(value.get("repo_root"), "engineering_target.repo_root")
    if not root.is_dir():
        _fail("engineering_target_missing", "engineering target repository does not exist")
    for forbidden in forbidden_roots:
        if _overlap(root, forbidden):
            _fail("foreign_state_overlap", "engineering target overlaps a protected BDB/package root")
    repository_id = _text(value.get("repository_id"), "engineering_target.repository_id", max_bytes=256)
    branch = _text(value.get("branch"), "engineering_target.branch", max_bytes=256)
    commit = _text(value.get("commit"), "engineering_target.commit", max_bytes=40)
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        _fail("engineering_target_invalid", "engineering target commit is not an exact Git object")
    tree = _text(value.get("tree"), "engineering_target.tree", max_bytes=128)
    view_id = _text(value.get("view_id"), "engineering_target.view_id", max_bytes=256)
    allowed_paths = value.get("allowed_paths")
    if not isinstance(allowed_paths, list) or not allowed_paths:
        _fail("engineering_target_invalid", "engineering target allowed_paths must be a non-empty list")
    normalized_paths: list[str] = []
    folded: set[str] = set()
    for raw in allowed_paths:
        if not isinstance(raw, str) or not raw or raw.startswith(("/", "\\")) or "\\" in raw or ":" in raw or ".." in raw.split("/") or "" in raw.split("/"):
            _fail("engineering_target_invalid", "engineering target paths must be relative and traversal-free")
        key = raw.casefold()
        if key in folded:
            _fail("engineering_target_invalid", "engineering target paths collide on Windows")
        folded.add(key)
        normalized_paths.append(raw)
    checker = value.get("checker")
    if not isinstance(checker, Mapping) or set(checker) != {"checker_id", "checker_version", "argv", "cwd", "timeout_seconds"}:
        _fail("engineering_target_invalid", "engineering target checker fields differ")
    checker_id = _text(checker.get("checker_id"), "engineering_target.checker.checker_id", max_bytes=256)
    checker_version = _text(checker.get("checker_version"), "engineering_target.checker.checker_version", max_bytes=64)
    argv = checker.get("argv")
    if not isinstance(argv, list) or not argv or any(not isinstance(item, str) or not item or "\x00" in item for item in argv):
        _fail("engineering_target_invalid", "engineering target checker argv is invalid")
    cwd = _text(checker.get("cwd"), "engineering_target.checker.cwd", max_bytes=256)
    if cwd != ".":
        _fail("engineering_target_invalid", "engineering target checker cwd must be the target root")
    timeout = checker.get("timeout_seconds")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0 or timeout > 300:
        _fail("engineering_target_invalid", "engineering target checker timeout is outside the bound")
    try:
        observed_branch = subprocess.run(["git", "-C", str(root), "branch", "--show-current"], shell=False, capture_output=True, text=True, check=True, timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        _fail("engineering_target_invalid", "engineering target branch could not be observed", details={"error": type(exc).__name__})
    if observed_branch != branch:
        _fail("engineering_target_branch_mismatch", "engineering target branch differs from the frozen package binding")
    try:
        resolved = RepositoryResource.from_path(root, repository_id=repository_id).resolve_committed(commit)
    except Exception as exc:
        _fail("engineering_target_invalid", "engineering target committed RepoView could not be resolved", details={"error": type(exc).__name__})
    if resolved.tree_oid != tree or resolved.view_id != view_id:
        _fail("engineering_target_repo_view_mismatch", "engineering target tree/view identity differs from the resolved commit")
    return {
        "schema": P1_ENGINEERING_TARGET_SCHEMA,
        "repository_id": repository_id,
        "repo_root": str(root),
        "branch": branch,
        "commit": commit,
        "tree": tree,
        "view_id": view_id,
        "allowed_paths": sorted(normalized_paths),
        "checker": {
            "checker_id": checker_id,
            "checker_version": checker_version,
            "argv": list(argv),
            "cwd": ".",
            "timeout_seconds": float(timeout),
        },
    }


def _validated_browser_native_binding(value: object) -> dict[str, Any]:
    binding = dict(_mapping(value, "browser_native_binding"))
    required = {
        "schema", "source_commit", "source_tree", "repo_view_id",
        "runtime_root", "package_root", "legacy_runtime_root",
        "native_code_digest", "interpreter_identity_digest",
        "browser_extension_id", "native_host_name", "protocol_generation",
        "production_activation", "binding_digest",
    }
    if set(binding) != required or binding.get("schema") != N6_BROWSER_NATIVE_BINDING_SCHEMA:
        _fail("config_invalid", "N6 Browser/Native execution binding fields differ")
    digest = binding.pop("binding_digest")
    if digest != semantic_digest(binding):
        _fail("config_invalid", "N6 Browser/Native execution binding digest differs")
    binding["binding_digest"] = digest
    return binding


def _native_code_records(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        raw = path.read_bytes()
        records.append({"path": relative, "size_bytes": len(raw), "sha256": _sha(raw)})
    return records


def native_code_digest(root: str | Path) -> str:
    path = Path(root).expanduser().absolute()
    records = _native_code_records(path)
    if not records:
        _fail("native_code_empty", "N6 package contains no immutable Native code")
    return _sha(_json_bytes({"schema": N6_NATIVE_CODE_SCHEMA, "files": records}))


@dataclass(frozen=True)
class N6RehearsalConfig:
    repo_root: Path
    runtime_root: Path
    legacy_runtime_root: Path
    source_commit: str
    package_root: Path
    native_code_root: Path
    native_code_digest: str
    package_digest: str
    interpreter_identity: Mapping[str, Any]
    browser_native_binding: Mapping[str, Any]
    browser_extension_id: str
    native_host_name: str = N6_NATIVE_HOST_NAME
    protocol_generation: str = N6_PROTOCOL_GENERATION
    engineering_target: Mapping[str, Any] | None = None

    @classmethod
    def from_json(cls, path: str | Path) -> "N6RehearsalConfig":
        source = Path(path).expanduser().absolute()
        document = _read_json(source)
        required = {"schema", "repo_root", "runtime_root", "legacy_runtime_root", "source_commit", "package_root", "native_code_root", "native_code_digest", "package_digest", "interpreter_identity", "browser_native_binding", "browser_extension_id", "native_host_name", "protocol_generation", "production_activation"}
        if not required.issubset(document) or set(document) - (required | {"engineering_target"}) or document.get("schema") != N6_CONFIG_SCHEMA:
            _fail("config_invalid", "N6 config fields/schema differ")
        if document.get("production_activation") is not False:
            _fail("activation_forbidden", "N6 rehearsal config must remain build-only")
        repo = _safe_abs(document["repo_root"], "repo_root")
        runtime = _safe_abs(document["runtime_root"], "runtime_root")
        legacy = _safe_abs(document["legacy_runtime_root"], "legacy_runtime_root")
        package = _safe_abs(document["package_root"], "package_root")
        native_code = _safe_abs(document["native_code_root"], "native_code_root")
        package_norm = os.path.normcase(os.path.normpath(str(package)))
        native_norm = os.path.normcase(os.path.normpath(str(native_code)))
        try:
            native_inside_package = os.path.commonpath((package_norm, native_norm)) == package_norm and native_norm != package_norm
        except ValueError:
            native_inside_package = False
        if _overlap(runtime, legacy) or _overlap(runtime, repo) or _overlap(package, legacy) or _overlap(package, repo) or not native_inside_package or _overlap(native_code, repo):
            _fail("foreign_state_overlap", "N6 mutable roots overlap source or legacy")
        source_commit = _text(document["source_commit"], "source_commit", max_bytes=40)
        if len(source_commit) != 40 or any(char not in "0123456789abcdef" for char in source_commit):
            _fail("config_invalid", "source_commit is not an exact Git object")
        interpreter = _mapping(document["interpreter_identity"], "interpreter_identity")
        if interpreter.get("schema") != N6_INTERPRETER_SCHEMA or interpreter.get("ownership") != "EXTERNAL_NOT_PACKAGE_OWNED":
            _fail("config_invalid", "N6 external interpreter identity is invalid")
        identity = dict(interpreter)
        claimed_identity_digest = identity.pop("identity_digest", None)
        if claimed_identity_digest != semantic_digest(identity):
            _fail("config_invalid", "N6 external interpreter identity digest differs")
        binding = _validated_browser_native_binding(document["browser_native_binding"])
        binding_coherence = (
            (binding["source_commit"], source_commit),
            (binding["runtime_root"], str(runtime)),
            (binding["package_root"], str(package)),
            (binding["legacy_runtime_root"], str(legacy)),
            (binding["native_code_digest"], document["native_code_digest"]),
            (binding["interpreter_identity_digest"], interpreter["identity_digest"]),
            (binding["browser_extension_id"], document["browser_extension_id"]),
            (binding["native_host_name"], document["native_host_name"]),
            (binding["protocol_generation"], document["protocol_generation"]),
            (binding["production_activation"], document["production_activation"]),
        )
        if any(left != right for left, right in binding_coherence):
            _fail("config_invalid", "N6 Browser/Native execution binding differs from config")
        target = None
        if "engineering_target" in document:
            target = _validated_engineering_target(
                _mapping(document["engineering_target"], "engineering_target"),
                forbidden_roots=(repo, runtime, legacy, package, native_code),
            )
        return cls(repo, runtime, legacy, source_commit, package, native_code, _text(document["native_code_digest"], "native_code_digest"), _text(document["package_digest"], "package_digest"), dict(interpreter), binding, _text(document["browser_extension_id"], "browser_extension_id"), _text(document["native_host_name"], "native_host_name"), _text(document["protocol_generation"], "protocol_generation"), target)


def _event_base(request: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
    if request.get("schema") != N6_NATIVE_REQUEST_SCHEMA:
        _fail("unsupported_schema", "N6 Native request schema differs")
    request_id = _text(request.get("request_id"), "request_id")
    event = _text(request.get("event"), "event")
    package_id = _text(request.get("package_id"), "package_id")
    protocol = _text(request.get("protocol_generation"), "protocol_generation")
    binding_digest = _text(request.get("browser_native_binding_digest"), "browser_native_binding_digest")
    return request_id, event, package_id, protocol, binding_digest


class N6RehearsalService:
    """Native-side adapter over canonical vNext authorities."""

    def __init__(self, config: N6RehearsalConfig) -> None:
        self.config = config
        self.identity = load_browser_identity()
        if self.identity["extension_id"] != config.browser_extension_id:
            _fail("browser_identity_mismatch", "configured Browser extension identity differs")
        self.source = RepositoryResource.from_path(config.repo_root, repository_id="bdb-vnext-n6-subject")
        self.view = self.source.resolve_committed(config.source_commit)
        self.manifest = build_vnext_composition_manifest(
            source_commit=config.source_commit,
            runtime_root=config.runtime_root,
            legacy_runtime_root=config.legacy_runtime_root,
            forbidden_roots=[config.repo_root, config.package_root],
        )
        self.package_digest = package_digest(config.package_root)
        if self.package_digest != config.package_digest:
            _fail("package_identity_mismatch", "N6 package digest differs from config")
        self.native_code_digest = native_code_digest(config.native_code_root)
        if self.native_code_digest != config.native_code_digest:
            _fail("native_code_identity_mismatch", "N6 Native code digest differs from config")
        self.interpreter_identity = interpreter_identity(sys.executable)
        if self.interpreter_identity != dict(config.interpreter_identity):
            _fail("interpreter_identity_mismatch", "running external Python differs from the package execution identity")
        expected_binding = _browser_native_binding(
            source_commit=config.source_commit,
            source_tree=self.view.tree_oid,
            repo_view_id=self.view.view_id,
            runtime_root=config.runtime_root,
            package_root=config.package_root,
            legacy_runtime_root=config.legacy_runtime_root,
            native_code_digest_value=self.native_code_digest,
            interpreter_identity_digest=self.interpreter_identity["identity_digest"],
            browser_extension_id=self.identity["extension_id"],
        )
        if expected_binding != dict(config.browser_native_binding):
            _fail("browser_native_binding_mismatch", "configured Browser/Native execution binding differs from exact package authority")
        self.browser_native_binding = expected_binding
        self.engineering_target = dict(config.engineering_target) if config.engineering_target is not None else None
        self.engineering_source = None
        self.engineering_view = None
        if self.engineering_target is not None:
            self.engineering_target = _validated_engineering_target(
                self.engineering_target,
                forbidden_roots=(config.repo_root, config.runtime_root, config.legacy_runtime_root, config.package_root, config.native_code_root),
            )
            self.engineering_source = RepositoryResource.from_path(
                self.engineering_target["repo_root"],
                repository_id=self.engineering_target["repository_id"],
            )
            self.engineering_view = self.engineering_source.resolve_committed(self.engineering_target["commit"])

    def _open(self):
        existing = (self.config.runtime_root / "browser" / "outbox" / "anchor.json").is_file()
        return VNextCompositionRoot.from_manifest(self.manifest).open_control_plane(existing_outbox=existing)

    def _lookup(self, plane: Any, submission_key: str, request_digest: str | None = None) -> dict[str, Any] | None:
        if request_digest is None:
            # Internal read-only recovery lookup; it never creates or mutates a
            # Task and remains behind the canonical admission authority.
            store = getattr(plane.admission.authority, "_store", None)
            if store is None:
                _fail("request_digest_required", "canonical N6 lookup requires the exact request digest")
            receipt = store.lookup(submission_key)
        else:
            receipt = plane.admission.authority.lookup(submission_key, request_digest)
        if receipt is None or receipt.task_id is None:
            return None
        task = plane.admission.authority.task(receipt.task_id)
        publications = plane.publication.publications_for_task(receipt.task_id)
        publication = publications[0] if publications else None
        work = plane.work_kernel.query(_stable_id("n6-work", submission_key))
        candidate = plane.candidate.get(_stable_id("n6-candidate", submission_key))
        return {
            "submission_key": submission_key,
            "task_id": receipt.task_id,
            "intent_revision_id": receipt.intent_revision_id,
            "work_id": work.work.work_id if work else None,
            "candidate_id": candidate.candidate_id if candidate else None,
            "candidate_view_id": candidate.manifest_digest if candidate else None,
            "publication": publication.as_dict() if publication else None,
            "task": task.as_dict() if task else None,
            "work": work.as_dict() if work else None,
        }

    def _run_vertical(
        self,
        *,
        submission_key: str,
        prompt: str,
        conversation_id: str,
        profile_id: str | None,
        fault: str | None = None,
    ) -> dict[str, Any]:
        prompt = _text(prompt, "prompt", max_bytes=N6_MAX_PROMPT_BYTES)
        conversation_id = _text(conversation_id, "conversation_id")
        work_id = _stable_id("n6-work", submission_key)
        candidate_id = _stable_id("n6-candidate", submission_key)
        lease_id = _stable_id("n6-lease", submission_key)
        run_id = _stable_id("n6-run", submission_key)

        def checkpoint(name: str) -> None:
            if fault == name:
                _fail("n6_recovery_checkpoint", f"N6 fault injected after {name}", details={"checkpoint": name})

        with self._open() as plane:
            request = ShadowSubmissionRequest(
                submission_key=submission_key,
                intent_revision="n6-r1",
                intent={"operation": "n6-rehearsal", "prompt_digest": _sha(prompt.encode("utf-8")), "allowlisted_effect": "exact_candidate_replacement"},
                conversation_binding={"conversation_id": conversation_id, "profile_id": profile_id},
                consumer_binding={"consumer_id": _stable_id("n6-browser", conversation_id), "kind": "browser", "generation": "bdb-vnext-g1"},
            )
            request_digest = request.validated_digest()
            if fault == "admission_response_lost":
                # Canonical admission commits but the Browser outbox remains
                # SENT.  The next invocation must recover by digest lookup.
                receipt = plane.admission.client.submit(request, crash_point="after_send_before_ack")
            else:
                receipt = plane.admission.client.submit(request)
            checkpoint("admission")

            work_query = plane.work_kernel.query(work_id)
            if work_query is None:
                plane.work_kernel.create_work_item(work_id, receipt.task_id)
                work_query = plane.work_kernel.query(work_id)
            assert work_query is not None
            checkpoint("work")

            publications = plane.publication.publications_for_task(receipt.task_id)
            if len(publications) > 1:
                _fail("publication_conflict", "N6 Task has more than one canonical Publication")
            if work_query.work.disposition == "FINISHED":
                if not publications or work_query.last_run is None or work_query.last_run.outcome != "SUCCEEDED" or work_query.last_run.effect_certainty != "CERTAIN":
                    _fail("vertical_state_incomplete", "finished N6 Work lacks its exact successful Publication lineage")
                if work_query.lease is not None and work_query.lease.state == "ACTIVE":
                    plane.work_kernel.release_lease(
                        work_id,
                        work_query.lease.lease_id,
                        work_query.lease.fence,
                    )
                found = self._lookup(plane, submission_key, request_digest)
                assert found is not None
                return found

            lease = plane.work_kernel.acquire_lease(work_id, lease_id, "n6-native-worker", ttl_seconds=300.0)
            checkpoint("lease")
            work_query = plane.work_kernel.query(work_id)
            assert work_query is not None
            if work_query.work.disposition == "READY":
                plane.work_kernel.start_run(
                    work_id,
                    run_id,
                    lease.lease_id,
                    lease.fence,
                    work_query.work.state_version,
                )
            elif work_query.work.disposition == "RUNNING" and work_query.active_run is not None:
                if work_query.active_run.run_id != run_id:
                    _fail("active_run_conflict", "N6 Work is owned by a different active Run")
                if (work_query.active_run.lease_id, work_query.active_run.fence) != (lease.lease_id, lease.fence):
                    plane.work_kernel.adopt_active_run(
                        work_id,
                        run_id,
                        lease.lease_id,
                        lease.fence,
                        work_query.work.state_version,
                    )
            else:
                _fail("vertical_state_incomplete", "N6 Work cannot safely resume from its current lifecycle disposition")
            checkpoint("run")

            candidate = plane.candidate.get(candidate_id)
            if candidate is None:
                workspace = plane.candidate.create_workspace(candidate_id=candidate_id, base_view=self.view)
                before = self.view.read_bytes("bdb_vnext/__init__.py")
                after = before + (b"\n# N6 rehearsal candidate marker: " + submission_key.encode("ascii") + b"\n")
                candidate = plane.candidate.prepare(
                    candidate_id=candidate_id,
                    work_id=work_id,
                    task_id=receipt.task_id,
                    lease_id=lease.lease_id,
                    fence=lease.fence,
                    base_view=self.view,
                    workspace_root=workspace,
                    replacements={"bdb_vnext/__init__.py": after},
                )
                checkpoint("candidate_prepared")
            elif candidate.state not in {CANDIDATE_SEALED, CANDIDATE_INVALIDATED} and (candidate.lease_id, candidate.fence) != (lease.lease_id, lease.fence):
                candidate = plane.candidate.adopt_lease(candidate_id, lease_id=lease.lease_id, fence=lease.fence)

            if candidate.state == CANDIDATE_INVALIDATED:
                _fail("candidate_invalidated", "N6 Candidate was invalidated and cannot be retried")
            if candidate.state != CANDIDATE_SEALED:
                candidate = plane.candidate.observe(candidate_id)
                if candidate.state == CANDIDATE_PREPARED:
                    plane.candidate.mark_possible(candidate_id)
                    checkpoint("candidate_possible")
                    candidate = plane.candidate.apply(candidate_id)
                if candidate.state != CANDIDATE_OBSERVED:
                    candidate = plane.candidate.observe(candidate_id)
                if candidate.state in {CANDIDATE_DIVERGED, CANDIDATE_UNKNOWN}:
                    _fail("candidate_reconciliation_required", "N6 Candidate effect cannot be retried without exact observation")
                if candidate.state != CANDIDATE_OBSERVED:
                    _fail("candidate_reconciliation_required", "N6 Candidate has not reached an exact AFTER observation")
                checkpoint("candidate_observed")
                _sealed, candidate_view = plane.candidate.seal(candidate_id, base_view=self.view)
                checkpoint("candidate_sealed")
            else:
                plane.candidate.verify_current_applicability(candidate_id)
                candidate_view = plane.candidate.get_view(candidate_id, self.view)

            evaluation = None
            publications = plane.publication.publications_for_task(receipt.task_id)
            if not publications:
                from bdb_vnext.m4c_evidence import MinimumCandidateChecker

                checker = MinimumCandidateChecker(self.config.native_code_root, plane.evidence)
                checker_fault = "raw_only" if fault == "evidence" else "lost_response" if fault == "evaluation" else None
                evaluation = checker.check(
                    candidate_view,
                    request_id=_stable_id("n6-check", submission_key),
                    evaluator_id="bdb-vnext-n6-candidate-checker",
                    evaluator_version="1",
                    config_digest=_sha(_json_bytes(self.manifest)),
                    fault=checker_fault,
                )
                current = plane.evidence.current_disposition(evaluation.evidence_id)
                publication = plane.publication.publish(
                    request_id=_stable_id("n6-publication", submission_key),
                    task_id=receipt.task_id,
                    work_id=work_id,
                    intent_revision_id=receipt.intent_revision_id,
                    result_payload={"schema": N6_EVENT_SCHEMA, "candidate_view_id": candidate_view.view_id, "evidence_id": evaluation.evidence_id, "disposition": current.disposition if current else "INCONCLUSIVE", "prompt_digest": _sha(prompt.encode("utf-8"))},
                    consumer_id=_stable_id("n6-browser", conversation_id),
                    consumer_kind="BROWSER",
                    conversation_id=conversation_id,
                    profile_id=profile_id,
                    candidate_id=candidate_view.candidate_id,
                    candidate_view_id=candidate_view.view_id,
                    evidence_id=evaluation.evidence_id,
                    evaluation_id=evaluation.evaluation_id,
                    disposition_id=current.disposition_id if current else None,
                    fault="after_commit" if fault == "publication" else None,
                )
            else:
                publication = publications[0]
                evaluation = plane.evidence.evaluations(publication.evidence_id)[-1] if publication.evidence_id else None
            checkpoint("publication_committed")

            work_query = plane.work_kernel.query(work_id)
            assert work_query is not None
            if work_query.work.disposition != "FINISHED":
                if work_query.active_run is None or work_query.active_run.run_id != run_id:
                    _fail("active_run_missing", "N6 Work has no canonical active Run to finish")
                plane.work_kernel.finish_run(
                    work_id,
                    run_id,
                    lease.lease_id,
                    lease.fence,
                    work_query.work.state_version,
                    outcome="SUCCEEDED",
                    effect_certainty="CERTAIN",
                    failpoint="after_commit" if fault == "work_finished" else None,
                )
            final_work = plane.work_kernel.query(work_id)
            assert final_work is not None
            if final_work.lease is not None and final_work.lease.state == "ACTIVE":
                plane.work_kernel.release_lease(work_id, lease.lease_id, lease.fence)
            final_work = plane.work_kernel.query(work_id)
            assert final_work is not None
            return {
                "submission_key": submission_key,
                "task_id": receipt.task_id,
                "intent_revision_id": receipt.intent_revision_id,
                "work_id": work_id,
                "candidate_id": candidate_view.candidate_id,
                "candidate_view_id": candidate_view.view_id,
                "evidence_id": publication.evidence_id,
                "evaluation_id": publication.evaluation_id,
                "publication_id": publication.publication_id,
                "publication": publication.as_dict(),
                "presentation": self._presentation(plane, publication),
                "work": final_work.as_dict(),
                "repo_view": self.view.to_dict(),
                "package_digest": self.package_digest,
                "protocol_generation": N6_PROTOCOL_GENERATION,
            }

    def _engineering_ids(self, submission_key: str) -> dict[str, str]:
        return {
            "work_id": _stable_id("p1-work", submission_key),
            "candidate_id": _stable_id("p1-candidate", submission_key),
            "lease_id": _stable_id("p1-lease", submission_key),
            "run_id": _stable_id("p1-run", submission_key),
        }

    def _engineering_checker(self) -> tuple[ValidationCommand, ValidationRunner]:
        if self.engineering_target is None:
            _fail("engineering_target_unavailable", "this N6 package has no disposable engineering target")
        checker = self.engineering_target["checker"]
        command = ValidationCommand(
            str(checker["checker_id"]),
            str(checker["checker_version"]),
            tuple(str(item) for item in checker["argv"]),
            str(checker["cwd"]),
            float(checker["timeout_seconds"]),
        )
        return command, ValidationRunner(ValidationPolicy(allowed_argv=(command.argv,), allowed_cwd=command.cwd))

    def _engineering_prepare(
        self,
        *,
        submission_key: str,
        prompt: str,
        conversation_id: str,
        profile_id: str | None,
    ) -> dict[str, Any]:
        if self.engineering_view is None or self.engineering_target is None:
            _fail("engineering_target_unavailable", "engineering target is not present in this package")
        prompt = _text(prompt, "prompt", max_bytes=N6_MAX_PROMPT_BYTES)
        conversation_id = _text(conversation_id, "conversation_id")
        ids = self._engineering_ids(submission_key)
        prompt_digest = _sha(prompt.encode("utf-8"))
        with self._open() as plane:
            request = ShadowSubmissionRequest(
                submission_key=submission_key,
                intent_revision=P1_ENGINEERING_INTENT_REVISION,
                intent={
                    "operation": "p1-engineering-edit",
                    "prompt_digest": prompt_digest,
                    "target_repo_view_id": self.engineering_view.view_id,
                    "target_tree": self.engineering_view.tree_oid,
                    "allowed_paths": list(self.engineering_target["allowed_paths"]),
                },
                conversation_binding={"conversation_id": conversation_id, "profile_id": profile_id},
                consumer_binding={"consumer_id": _stable_id("p1-browser", conversation_id), "kind": "browser", "generation": "bdb-vnext-g1"},
            )
            request_digest = request.validated_digest()
            receipt = plane.admission.client.submit(request)
            work = plane.work_kernel.query(ids["work_id"])
            if work is None:
                plane.work_kernel.create_work_item(ids["work_id"], receipt.task_id, kind="p1-engineering")
                work = plane.work_kernel.query(ids["work_id"])
            if work is None:
                _fail("engineering_work_missing", "canonical WorkItem could not be created")
            if work.work.disposition == "FINISHED":
                candidate = plane.candidate.get(ids["candidate_id"])
                return self._engineering_context(receipt.task_id, request_digest, conversation_id, submission_key, work, candidate, plane.candidate.generation, plane.candidate._tree_digest(plane.candidate._base_entries(self.engineering_view)))
            # Recovery is canonical WorkKernel ownership transfer.  A prior
            # process may have left the intended Run active after its lease
            # expired; reacquiring the same lease then advances the fence.  In
            # that shape adopt the *same* Run through the WorkKernel rather
            # than rejecting it or rewriting rows here.
            lease = None
            if work.work.disposition == "RUNNING":
                active = work.active_run
                current_lease = work.lease
                if active is None or current_lease is None:
                    _fail("engineering_run_conflict", "engineering WorkItem has no unambiguous active Run/Lease")
                if active.run_id != ids["run_id"]:
                    _fail("engineering_run_conflict", "engineering WorkItem is owned by a different active Run")
                if active.lease_id != ids["lease_id"] or current_lease.lease_id != ids["lease_id"]:
                    _fail("engineering_run_conflict", "engineering WorkItem has a foreign active Lease")
                lease = plane.work_kernel.acquire_lease(
                    ids["work_id"], ids["lease_id"], "p1-browser-engineering", ttl_seconds=900.0
                )
                work = plane.work_kernel.query(ids["work_id"])
                if work is None or work.active_run is None or work.lease is None:
                    _fail("engineering_run_conflict", "engineering WorkItem recovery lost its active Run/Lease")
                active = work.active_run
                if active.run_id != ids["run_id"] or active.lease_id != lease.lease_id:
                    _fail("engineering_run_conflict", "engineering WorkItem is owned by a different active Run")
                if active.fence > lease.fence:
                    _fail("engineering_run_conflict", "engineering Run fence is newer than the current Lease fence")
                if active.fence < lease.fence:
                    plane.work_kernel.adopt_active_run(
                        ids["work_id"],
                        ids["run_id"],
                        lease.lease_id,
                        lease.fence,
                        work.work.state_version,
                    )
                    work = plane.work_kernel.query(ids["work_id"])
                if (
                    work is None
                    or work.work.disposition != "RUNNING"
                    or work.active_run is None
                    or work.active_run.run_id != ids["run_id"]
                    or work.active_run.lease_id != lease.lease_id
                    or work.active_run.fence != lease.fence
                ):
                    _fail("engineering_run_conflict", "engineering WorkItem recovery did not establish the expected Run owner")
            else:
                lease = plane.work_kernel.acquire_lease(ids["work_id"], ids["lease_id"], "p1-browser-engineering", ttl_seconds=900.0)
                work = plane.work_kernel.query(ids["work_id"])
                if work is None:
                    _fail("engineering_work_missing", "canonical WorkItem disappeared during preparation")
                if work.work.disposition == "READY":
                    plane.work_kernel.start_run(ids["work_id"], ids["run_id"], lease.lease_id, lease.fence, work.work.state_version)
                    work = plane.work_kernel.query(ids["work_id"])
                elif work.work.disposition == "RUNNING":
                    _fail("engineering_run_conflict", "engineering WorkItem changed to RUNNING during preparation")
            candidate = plane.candidate.get(ids["candidate_id"])
            return self._engineering_context(receipt.task_id, request_digest, conversation_id, submission_key, work, candidate, plane.candidate.generation, plane.candidate._tree_digest(plane.candidate._base_entries(self.engineering_view)))

    def _engineering_context(
        self,
        task_id: str,
        request_digest: str,
        conversation_id: str,
        submission_key: str,
        work: Any,
        candidate: Any,
        generation: str,
        current_tree_digest: str,
    ) -> dict[str, Any]:
        if self.engineering_view is None or self.engineering_target is None:
            _fail("engineering_target_unavailable", "engineering target is not present in this package")
        ids = self._engineering_ids(submission_key)
        lease = work.lease
        run = work.active_run or work.last_run
        return {
            "submission_key": submission_key,
            "task_id": task_id,
            "intent_revision": P1_ENGINEERING_INTENT_REVISION,
            "request_digest": request_digest,
            "conversation_id": conversation_id,
            "work_id": ids["work_id"],
            "run_id": run.run_id if run is not None else ids["run_id"],
            "lease_id": lease.lease_id if lease is not None else ids["lease_id"],
            "fence": lease.fence if lease is not None else (run.fence if run is not None else 0),
            "candidate_id": ids["candidate_id"],
            "workspace_generation": generation,
            "base_repo_view": self.engineering_view.to_dict(),
            "current_tree_digest": current_tree_digest,
            "target": {"repository_id": self.engineering_target["repository_id"], "commit": self.engineering_target["commit"], "tree": self.engineering_target["tree"], "view_id": self.engineering_target["view_id"], "allowed_paths": list(self.engineering_target["allowed_paths"]), "checker": dict(self.engineering_target["checker"])},
            "candidate_state": candidate.state if candidate is not None else "NOT_PREPARED",
            "candidate_view_id": candidate.manifest_digest if candidate is not None else None,
            "work": work.as_dict(),
        }

    def _engineering_desired_files(self, store: Any, candidate: Any, base_view: Any) -> dict[str, bytes | None]:
        workspace = Path(candidate.workspace_root)
        base_entries = store._base_entries(base_view)
        actual_entries = store._workspace_entries(workspace, object_format=base_view.object_format)
        desired: dict[str, bytes | None] = {}
        for path in sorted(set(base_entries) | set(actual_entries)):
            if path in actual_entries:
                raw = (workspace / Path(*path.split("/"))).read_bytes()
                base = base_view.read_bytes(path) if path in base_entries else None
                if base != raw:
                    desired[path] = raw
            elif path in base_entries:
                desired[path] = None
        return desired

    def _engineering_artifact(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if self.engineering_view is None or self.engineering_target is None:
            _fail("engineering_target_unavailable", "engineering target is not present in this package")
        required = {"submission_key", "conversation_id", "prompt_digest", "raw_answer", "raw_answer_digest", "artifact"}
        if set(payload) != required:
            _fail("engineering_payload_invalid", "engineering artifact payload fields differ")
        submission_key = _text(payload.get("submission_key"), "submission_key")
        conversation_id = _text(payload.get("conversation_id"), "conversation_id")
        prompt_digest = _text(payload.get("prompt_digest"), "prompt_digest")
        raw_answer = _text(payload.get("raw_answer"), "raw_answer", max_bytes=N6_MAX_ANSWER_BYTES)
        raw_answer_digest = _text(payload.get("raw_answer_digest"), "raw_answer_digest")
        if raw_answer_digest != _sha(raw_answer.encode("utf-8")):
            _fail("engineering_raw_digest_mismatch", "Browser raw answer digest differs")
        artifact = parse_edit_artifact(_mapping(payload.get("artifact"), "artifact"))
        ids = self._engineering_ids(submission_key)
        if artifact.base_view_id != self.engineering_view.view_id or artifact.task_id == "" or artifact.work_id != ids["work_id"] or artifact.run_id != ids["run_id"] or artifact.lease_id != ids["lease_id"] or artifact.fence < 1 or artifact.candidate_id != ids["candidate_id"]:
            _fail("engineering_artifact_binding_mismatch", "BDB_EDIT_V1 artifact is not bound to the exact engineering context")
        allowed = {str(path).casefold() for path in self.engineering_target["allowed_paths"]}
        for operation in artifact.operations:
            paths = [operation.path] + ([operation.source_path] if operation.source_path else [])
            if any(path.casefold() not in allowed for path in paths):
                _fail("engineering_path_not_allowlisted", "BDB_EDIT_V1 artifact touches a path outside the target allowlist")
        with self._open() as plane:
            found = self._lookup(plane, submission_key)
            if not found or found.get("task_id") is None:
                _fail("engineering_submission_not_found", "engineering submission was not canonically admitted")
            if _task_conversation(found) != conversation_id:
                _fail("conversation_owner_mismatch", "engineering artifact conversation differs from canonical Task ownership")
            if artifact.task_id != found["task_id"]:
                _fail("engineering_task_binding_mismatch", "BDB_EDIT_V1 artifact task_id differs from canonical Task")
            task = _mapping(found.get("task"), "task")
            expected_intent_digest = semantic_digest(
                {
                    "schema": "bdb-vnext-intent-v1",
                    "intent_revision": P1_ENGINEERING_INTENT_REVISION,
                    "intent": {
                        "operation": "p1-engineering-edit",
                        "prompt_digest": prompt_digest,
                        "target_repo_view_id": self.engineering_view.view_id,
                        "target_tree": self.engineering_view.tree_oid,
                        "allowed_paths": list(self.engineering_target["allowed_paths"]),
                    },
                }
            )
            if task.get("intent_digest") != expected_intent_digest:
                _fail("engineering_prompt_binding_mismatch", "Browser artifact prompt/target does not match canonical Task intent")
            work = plane.work_kernel.query(ids["work_id"])
            if work is None or work.active_run is None or work.active_run.run_id != ids["run_id"]:
                _fail("engineering_run_binding_mismatch", "engineering artifact is not owned by the active Run")
            if work.lease is None or work.lease.lease_id != artifact.lease_id or work.lease.fence != artifact.fence:
                _fail("engineering_fence_mismatch", "engineering artifact lease/fence is stale")
            candidate = plane.candidate.get(ids["candidate_id"])
            if candidate is not None and candidate.state in {CANDIDATE_SEALED, CANDIDATE_INVALIDATED}:
                _fail("candidate_state_conflict", "sealed or invalidated engineering Candidate cannot accept another artifact")
            if candidate is None:
                workspace_path = plane.candidate.create_workspace(candidate_id=ids["candidate_id"], base_view=self.engineering_view)
            else:
                workspace_path = Path(candidate.workspace_root)
            editor = EditorPort(plane.candidate, evidence_store=plane.evidence)
            desired = self._engineering_desired_files(plane.candidate, candidate, self.engineering_view) if candidate is not None else None
            # A raw assistant artifact is immutable evidence before any apply.
            artifact_evidence = plane.evidence.record_observation(
                request_id=_stable_id("p1-artifact", submission_key + ":" + artifact.artifact_digest),
                primary_subject_kind="ENGINEERING_ARTIFACT",
                primary_subject_identity={"submission_key": submission_key, "conversation_id": conversation_id, "prompt_digest": prompt_digest, "artifact_digest": artifact.artifact_digest, "task_id": found["task_id"], "work_id": ids["work_id"], "run_id": ids["run_id"]},
                candidate_view_id=None,
                raw_observation={"schema": N6_EVENT_SCHEMA, "event": "engineering_artifact", "raw_answer": raw_answer, "raw_answer_digest": raw_answer_digest, "artifact": artifact.as_dict()},
                checker_id="bdb-vnext-p1-browser-artifact",
                checker_version="1",
                checker_code_digest=semantic_digest({"schema": EDIT_SCHEMA, "adapter": "bdb_vnext.n6_rehearsal"}),
                environment={"surface": "normal-chatgpt-browser", "package_digest": self.package_digest, "protocol_generation": N6_PROTOCOL_GENERATION},
                observation_started_at=_now(), observation_finished_at=_now(), completeness="COMPLETE", applicability="APPLICABLE", status="CAPTURED",
            )
            record = editor.prepare_batch(artifact, base_view=self.engineering_view, workspace=workspace_path, desired_files=desired)
            applied = editor.apply_batch(artifact)
            observed = plane.candidate.observe(ids["candidate_id"])
            if observed.state in {CANDIDATE_DIVERGED, CANDIDATE_UNKNOWN}:
                _fail("engineering_reconciliation_required", "engineering Candidate observation is not exact")
            command, runner = self._engineering_checker()
            result = runner.run(command, observed.workspace_root)
            validation = editor.record_validation(batch=artifact, result=result)
            current_tree = editor._current_tree_digest(Path(observed.workspace_root), self.engineering_view)
            return {"status": "VALIDATED", "validation_status": validation.status, "feedback": result.feedback, "artifact_digest": artifact.artifact_digest, "artifact_evidence_id": artifact_evidence.evidence_id, "validation_id": validation.validation_id, "validation_evidence_id": validation.evidence_id, "candidate_id": ids["candidate_id"], "candidate_state": observed.state, "candidate_workspace": observed.workspace_root, "current_tree_digest": current_tree, "expected_tree_digest": artifact.expected_tree_digest, "task_id": found["task_id"], "work_id": ids["work_id"], "run_id": ids["run_id"], "lease_id": artifact.lease_id, "fence": artifact.fence, "ready_to_seal": validation.status == "PASS"}

    def _engineering_finalize(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if self.engineering_view is None or self.engineering_target is None:
            _fail("engineering_target_unavailable", "engineering target is not present in this package")
        if set(payload) != {"submission_key", "candidate_id"}:
            _fail("engineering_payload_invalid", "engineering finalize fields differ")
        submission_key = _text(payload.get("submission_key"), "submission_key")
        candidate_id = _text(payload.get("candidate_id"), "candidate_id")
        ids = self._engineering_ids(submission_key)
        if candidate_id != ids["candidate_id"]:
            _fail("engineering_candidate_binding_mismatch", "engineering finalize candidate differs")
        with self._open() as plane:
            found = self._lookup(plane, submission_key)
            if not found:
                _fail("engineering_submission_not_found", "engineering submission was not canonically admitted")
            existing_publications = plane.publication.publications_for_task(found["task_id"])
            if existing_publications:
                return {"status": "SEALED", "publication": existing_publications[0].as_dict(), "candidate_id": candidate_id, "candidate_view_id": found.get("candidate_view_id")}
            candidate = plane.candidate.get(candidate_id)
            if candidate is None or candidate.state != CANDIDATE_OBSERVED:
                _fail("candidate_not_observed", "engineering Candidate must be exactly observed before seal")
            validation_row = plane.candidate._connection.execute("SELECT validation_id,status,evidence_id,stdout_ref_json,stderr_ref_json,stdout_digest,stderr_digest,environment_fingerprint,started_at,finished_at,command_json FROM p1_validation_runs WHERE candidate_id=? ORDER BY finished_at DESC LIMIT 1", (candidate_id,)).fetchone()
            if validation_row is None or str(validation_row[1]) != "PASS":
                _fail("engineering_validation_not_green", "final engineering validation is not PASS")
            _sealed, candidate_view = plane.candidate.seal(candidate_id, base_view=self.engineering_view)
            command, _runner = self._engineering_checker()
            final_raw = {"schema": "bdb-vnext-p1-final-validation-v1", "validation_id": str(validation_row[0]), "status": str(validation_row[1]), "stdout_ref": json.loads(bytes(validation_row[3]).decode()), "stderr_ref": json.loads(bytes(validation_row[4]).decode()), "stdout_digest": str(validation_row[5]), "stderr_digest": str(validation_row[6]), "environment_fingerprint": str(validation_row[7]), "candidate_view": candidate_view.to_dict()}
            evidence = plane.evidence.record_observation(
                request_id=_stable_id("p1-final-evidence", submission_key),
                primary_subject_kind="CANDIDATE",
                primary_subject_identity={"schema": "bdb-vnext-p1-engineering-result-v1", "submission_key": submission_key, "candidate_id": candidate_id, "view_id": candidate_view.view_id, "candidate_tree_digest": candidate_view.candidate_tree_digest},
                candidate_view_id=candidate_view.view_id,
                raw_observation=final_raw,
                checker_id=command.checker_id,
                checker_version=command.checker_version,
                checker_code_digest=command.checker_code_digest,
                environment={"fingerprint": str(validation_row[7]), "target": self.engineering_target["repository_id"]},
                observation_started_at=str(validation_row[8]), observation_finished_at=str(validation_row[9]), completeness="COMPLETE", applicability="APPLICABLE", status="PASS",
            )
            evaluation = plane.evidence.evaluate(evidence_id=evidence.evidence_id, evaluator_id="bdb-vnext-p1-calculator-evaluator", evaluator_version="1", evaluator_code_digest=command.checker_code_digest, config_digest=semantic_digest(self.engineering_target), result="PASS", applicability="APPLICABLE", detail={"schema": "bdb-vnext-p1-engineering-result-v1", "candidate_view_id": candidate_view.view_id})
            disposition = plane.evidence.current_disposition(evidence.evidence_id)
            if disposition is None:
                _fail("engineering_disposition_missing", "final engineering evidence has no current disposition")
            publication = plane.publication.publish(
                request_id=_stable_id("p1-publication", submission_key),
                task_id=found["task_id"], work_id=ids["work_id"], intent_revision_id=P1_ENGINEERING_INTENT_REVISION,
                result_payload={"schema": "bdb-vnext-p1-engineering-publication-v1", "candidate_view_id": candidate_view.view_id, "evidence_id": evidence.evidence_id, "evaluation_id": evaluation.evaluation_id, "disposition_id": disposition.disposition_id, "status": "PASS"},
                consumer_id=_stable_id("p1-browser", _task_conversation(found)), consumer_kind="BROWSER", conversation_id=_task_conversation(found), profile_id=None, candidate_id=candidate_id, candidate_view_id=candidate_view.view_id, evidence_id=evidence.evidence_id, evaluation_id=evaluation.evaluation_id, disposition_id=disposition.disposition_id, generation="bdb-vnext-g1",
            )
            work = plane.work_kernel.query(ids["work_id"])
            if work is not None and work.active_run is not None and work.lease is not None:
                plane.work_kernel.finish_run(ids["work_id"], ids["run_id"], work.lease.lease_id, work.lease.fence, work.work.state_version, outcome="SUCCEEDED", effect_certainty="CERTAIN")
                finished = plane.work_kernel.query(ids["work_id"])
                if finished is not None and finished.lease is not None and finished.lease.state == "ACTIVE":
                    plane.work_kernel.release_lease(ids["work_id"], finished.lease.lease_id, finished.lease.fence)
            return {"status": "SEALED", "candidate_id": candidate_id, "candidate_view": candidate_view.to_dict(), "candidate_view_id": candidate_view.view_id, "candidate_tree_digest": candidate_view.candidate_tree_digest, "evidence_id": evidence.evidence_id, "evaluation_id": evaluation.evaluation_id, "disposition_id": disposition.disposition_id, "publication": publication.as_dict(), "work": (plane.work_kernel.query(ids["work_id"]).as_dict() if plane.work_kernel.query(ids["work_id"]) else None)}

    def _publication(self, plane: Any, submission_key: str) -> tuple[dict[str, Any], Any]:
        found = self._lookup(plane, submission_key)
        if not found or not found["publication"]:
            _fail("submission_not_found", "no canonical N6 publication exists for submission")
        publication = plane.publication.get(found["publication"]["publication_id"])
        if publication is None:
            _fail("publication_missing", "canonical publication disappeared")
        return found, publication

    @staticmethod
    def _presentation(plane: Any, publication: Any) -> dict[str, Any]:
        return {
            "schema": "bdb-vnext-n6-presentation-v1",
            "publication_id": publication.publication_id,
            "marker": "n6-publication:" + publication.publication_id,
            "result_digest": publication.result_digest,
            "rendered_label": "BDB vNext canonical Publication " + publication.publication_id + "\nResult digest " + publication.result_digest,
        }

    def handle(self, request: Mapping[str, Any]) -> dict[str, Any]:
        request_id, event, package_id, protocol, binding_digest = _event_base(request)
        if package_id != N6_PACKAGE_SCHEMA or protocol != N6_PROTOCOL_GENERATION:
            _fail("protocol_mismatch", "N6 package/protocol identity differs")
        if binding_digest != self.browser_native_binding["binding_digest"]:
            _fail("browser_native_binding_mismatch", "Browser request belongs to a different N6 execution package")
        if event == "status":
            return {"schema": N6_NATIVE_RESPONSE_SCHEMA, "request_id": request_id, "status": "READY", "package_digest": self.package_digest, "native_code_digest": self.native_code_digest, "interpreter_identity_digest": self.interpreter_identity["identity_digest"], "browser_native_binding_digest": self.browser_native_binding["binding_digest"], "browser_extension_id": self.identity["extension_id"], "native_host_name": N6_NATIVE_HOST_NAME, "protocol_generation": N6_PROTOCOL_GENERATION, "production_activation": False, "rehearsal_infrastructure": "ACTIVE_ONLY_WHEN_EXPLICIT_PACKAGE_LOADED", "production_runtime": "OFF", "production_writer": "OFF"}
        payload = _mapping(request.get("payload"), "payload")
        if event == "engineering_prepare":
            result = self._engineering_prepare(
                submission_key=_text(payload.get("submission_key"), "submission_key"),
                prompt=_text(payload.get("prompt"), "prompt", max_bytes=N6_MAX_PROMPT_BYTES),
                conversation_id=_text(payload.get("conversation_id"), "conversation_id"),
                profile_id=payload.get("profile_id") if isinstance(payload.get("profile_id"), str) else None,
            )
            return {"schema": N6_NATIVE_RESPONSE_SCHEMA, "request_id": request_id, "status": "ENGINEERING_READY", "result": result}
        if event == "engineering_artifact":
            result = self._engineering_artifact(payload)
            return {"schema": N6_NATIVE_RESPONSE_SCHEMA, "request_id": request_id, "status": "ENGINEERING_VALIDATED", "result": result}
        if event == "engineering_finalize":
            result = self._engineering_finalize(payload)
            return {"schema": N6_NATIVE_RESPONSE_SCHEMA, "request_id": request_id, "status": "ENGINEERING_SEALED", "result": result}
        if event == "submit_prompt":
            result = self._run_vertical(submission_key=_text(payload.get("submission_key"), "submission_key"), prompt=_text(payload.get("prompt"), "prompt", max_bytes=N6_MAX_PROMPT_BYTES), conversation_id=_text(payload.get("conversation_id"), "conversation_id"), profile_id=payload.get("profile_id") if isinstance(payload.get("profile_id"), str) else None)
            return {"schema": N6_NATIVE_RESPONSE_SCHEMA, "request_id": request_id, "status": "ACCEPTED", "result": result}
        submission_key = _text(payload.get("submission_key"), "submission_key")
        with self._open() as plane:
            found, publication = self._publication(plane, submission_key)
            if event == "lookup":
                return {"schema": N6_NATIVE_RESPONSE_SCHEMA, "request_id": request_id, "status": "FOUND", "result": {**found, "presentation": self._presentation(plane, publication)}}
            if event == "capture_answer":
                answer = _text(payload.get("raw_answer"), "raw_answer", max_bytes=N6_MAX_ANSWER_BYTES)
                model = payload.get("model") if isinstance(payload.get("model"), str) else None
                reasoning = payload.get("reasoning") if isinstance(payload.get("reasoning"), str) else None
                started = _text(payload.get("started_at"), "started_at")
                finished = _text(payload.get("finished_at"), "finished_at")
                conversation_id = _text(payload.get("conversation_id"), "conversation_id")
                if conversation_id != _task_conversation(found):
                    _fail("conversation_owner_mismatch", "Browser capture conversation differs from canonical Task ownership")
                completion = _text(payload.get("completion_observation"), "completion_observation")
                if completion != "DOM_TEXT_STABLE_AFTER_STREAM_END":
                    _fail("answer_completion_unverified", "Browser answer capture lacks a positive completed/stable DOM observation")
                raw = {"schema": N6_EVENT_SCHEMA, "event": "assistant_capture", "submission_key": submission_key, "task_id": found["task_id"], "work_id": found["work_id"], "candidate_id": found["candidate_id"], "candidate_view_id": found["candidate_view_id"], "publication_id": publication.publication_id, "conversation_id": conversation_id, "profile_id": payload.get("profile_id"), "profile_attestation": "NOT_OBSERVABLE", "model": model, "reasoning": reasoning, "model_attestation": "OPERATOR_VISIBLE_ATTESTATION", "reasoning_attestation": "OPERATOR_VISIBLE_ATTESTATION", "started_at": started, "finished_at": finished, "timing_attestation": "BROWSER_CLOCK_OBSERVATION", "completion_observation": completion, "raw_answer": answer, "raw_answer_digest": _sha(answer.encode("utf-8"))}
                request_id_value = _stable_id("n6-browser-answer", submission_key)
                evidence = plane.evidence.record_observation(request_id=request_id_value, primary_subject_kind="N6_BROWSER_RUN", primary_subject_identity={"submission_key": submission_key, "task_id": found["task_id"], "work_id": found["work_id"], "publication_id": publication.publication_id}, candidate_view_id=found["candidate_view_id"], raw_observation=raw, checker_id=N6_CAPTURE_CHECKER_ID, checker_version=N6_CAPTURE_CHECKER_VERSION, checker_code_digest=semantic_digest({"schema": N6_EVENT_SCHEMA, "module": "bdb_vnext.n6_rehearsal"}), environment={"model": model, "reasoning": reasoning, "model_attestation": "OPERATOR_VISIBLE_ATTESTATION", "reasoning_attestation": "OPERATOR_VISIBLE_ATTESTATION", "profile_attestation": "NOT_OBSERVABLE", "surface": "normal-chatgpt-browser", "package_digest": self.package_digest, "protocol_generation": N6_PROTOCOL_GENERATION}, observation_started_at=started, observation_finished_at=finished, completeness="COMPLETE" if answer and completion == "DOM_TEXT_STABLE_AFTER_STREAM_END" else "INCOMPLETE", applicability="APPLICABLE" if model == "GPT-5.6 Sol" and reasoning == "Wysoki" else "INCONCLUSIVE", status="CAPTURED")
                return {"schema": N6_NATIVE_RESPONSE_SCHEMA, "request_id": request_id, "status": "CAPTURED", "result": {"evidence_id": evidence.evidence_id, "raw_digest": evidence.raw_digest, "raw_answer_digest": raw["raw_answer_digest"], "applicability": evidence.applicability, "completeness": evidence.completeness}}
            if event == "witness":
                conversation_id = _text(payload.get("conversation_id"), "conversation_id")
                if conversation_id != _task_conversation(found):
                    _fail("conversation_owner_mismatch", "Browser witness conversation differs from canonical Task ownership")
                consumer_id = _stable_id("n6-browser", conversation_id)
                marker = _text(payload.get("marker"), "marker")
                capture_evidence_id = _text(payload.get("capture_evidence_id"), "capture_evidence_id")
                capture = plane.evidence.get(capture_evidence_id)
                if (
                    capture is None
                    or capture.request_id != _stable_id("n6-browser-answer", submission_key)
                    or capture.checker_id != N6_CAPTURE_CHECKER_ID
                    or capture.checker_version != N6_CAPTURE_CHECKER_VERSION
                    or capture.primary_subject_kind != "N6_BROWSER_RUN"
                    or capture.primary_subject_identity.get("submission_key") != submission_key
                    or capture.primary_subject_identity.get("publication_id") != publication.publication_id
                ):
                    _fail("presentation_not_observed", "Browser witness lacks the exact canonical assistant capture")
                binding = plane.publication.observe_presentation(
                    publication_id=publication.publication_id,
                    consumer_id=consumer_id,
                    conversation_id=conversation_id,
                    profile_id=payload.get("profile_id") if isinstance(payload.get("profile_id"), str) else None,
                    marker=marker,
                    result_digest=_text(payload.get("observed_result_digest"), "observed_result_digest"),
                    composer_preserved=payload.get("composer_preserved") is not False,
                    witness={
                        "source": "chatgpt-assistant-dom-capture",
                        "observation": "EXACT_CAPTURED_ASSISTANT_RESULT_VISIBLE",
                        "observed_publication_id": _text(payload.get("observed_publication_id"), "observed_publication_id"),
                        "observed_conversation_id": conversation_id,
                        "observed_result_digest": _text(payload.get("observed_result_digest"), "observed_result_digest"),
                        "capture_evidence_id": capture_evidence_id,
                        "observed_answer_digest": _text(payload.get("observed_answer_digest"), "observed_answer_digest"),
                        "dom_author_role": payload.get("dom_author_role"),
                        "completion_observation": payload.get("completion_observation"),
                        "extension_ui_ancestor": payload.get("extension_ui_ancestor"),
                    },
                )
                return {"schema": N6_NATIVE_RESPONSE_SCHEMA, "request_id": request_id, "status": "PRESENTED", "result": binding.as_dict()}
            if event == "unknown":
                conversation_id = _text(payload.get("conversation_id"), "conversation_id")
                if conversation_id != _task_conversation(found):
                    _fail("conversation_owner_mismatch", "Browser UNKNOWN conversation differs from canonical Task ownership")
                consumer_id = _stable_id("n6-browser", conversation_id)
                binding = plane.publication.mark_unknown(publication_id=publication.publication_id, consumer_id=consumer_id, reason=_text(payload.get("reason"), "reason"))
                return {"schema": N6_NATIVE_RESPONSE_SCHEMA, "request_id": request_id, "status": binding.presentation, "result": binding.as_dict()}
            if event == "resume":
                target_conversation = _text(payload.get("target_conversation_id"), "target_conversation_id")
                source_conversation = _text(payload.get("source_conversation_id"), "source_conversation_id")
                if source_conversation != _task_conversation(found):
                    _fail("conversation_owner_mismatch", "Resume source differs from canonical Task ownership")
                target_consumer = _stable_id("n6-browser", target_conversation)
                source_consumer = _stable_id("n6-browser", source_conversation)
                if target_consumer == source_consumer:
                    _fail("resume_consumer_conflict", "new-chat Resume requires a distinct consumer")
                if plane.publication.get_binding(publication.publication_id, target_consumer) is None:
                    plane.publication.bind_consumer(publication_id=publication.publication_id, consumer_id=target_consumer, consumer_kind="BROWSER", conversation_id=target_conversation, profile_id=payload.get("profile_id") if isinstance(payload.get("profile_id"), str) else None)
                capsule = plane.publication.create_resume_capsule(publication_id=publication.publication_id, source_consumer_id=source_consumer, target_consumer_id=target_consumer, payload={"repo_view": self.view.to_dict(), "candidate_view_id": found["candidate_view_id"], "evidence_id": publication.evidence_id, "continuation": "n6-rehearsal"})
                return {"schema": N6_NATIVE_RESPONSE_SCHEMA, "request_id": request_id, "status": "RESUMABLE", "result": {"capsule_id": capsule.capsule_id, "capsule": plane.publication.resume_payload(capsule.capsule_id), "target_consumer_id": target_consumer}}
        _fail("unsupported_event", f"unsupported N6 Browser event: {event}")


def _framing_read(stream: BinaryIO) -> dict[str, Any] | None:
    header = stream.read(4)
    if not header:
        return None
    if len(header) != 4:
        _fail("native_frame_invalid", "Native message length prefix is truncated")
    length = int.from_bytes(header, "little")
    if length <= 0 or length > N6_MAX_MESSAGE_BYTES:
        _fail("native_frame_invalid", "Native message length is outside the bounded limit")
    raw = stream.read(length)
    if len(raw) != length:
        _fail("native_frame_invalid", "Native message body is truncated")
    value = json.loads(raw.decode("utf-8"))
    return dict(_mapping(value, "native_message"))


def _framing_write(stream: BinaryIO, value: Mapping[str, Any]) -> None:
    raw = _json_bytes(value)
    if len(raw) > N6_MAX_MESSAGE_BYTES:
        _fail("native_frame_invalid", "Native response exceeds the bounded limit")
    stream.write(len(raw).to_bytes(4, "little") + raw)
    stream.flush()


def run_native_host(config_path: str | Path, *, input_stream: BinaryIO | None = None, output_stream: BinaryIO | None = None) -> int:
    config = N6RehearsalConfig.from_json(config_path)
    service = N6RehearsalService(config)
    source_in = input_stream or getattr(sys.stdin, "buffer", sys.stdin)
    source_out = output_stream or getattr(sys.stdout, "buffer", sys.stdout)
    while True:
        try:
            request = _framing_read(source_in)
            if request is None:
                return 0
            request_id = request.get("request_id") if isinstance(request.get("request_id"), str) else "invalid"
            response = service.handle(request)
        except Exception as exc:
            response = {"schema": N6_NATIVE_RESPONSE_SCHEMA, "request_id": request_id if "request_id" in locals() else "invalid", "status": "ERROR", "error": {"code": getattr(exc, "code", "internal_error"), "message": str(exc), "details": getattr(exc, "details", {})}}
        _framing_write(source_out, response)


def _js_background(binding_digest: str, extension_id: str) -> str:
    script = r'''"use strict";
const HOST = "com.bartosz.dev_bridge.vnext";
const PACKAGE = "bdb-vnext-n6-rehearsal-package-v3";
const PROTOCOL = "bdb-vnext-n6-protocol-v2";
const P1_ENGINEERING_PREFIX = "__P1_ENGINEERING_PREFIX__";
const P1_EDIT_SCHEMA = "bdb-vnext-edit-v1";
const REQUEST_SCHEMA = "bdb-vnext-n6-native-request-v2";
const RESPONSE_SCHEMA = "bdb-vnext-n6-native-response-v2";
const BROWSER_NATIVE_BINDING = __N6_BINDING__;
const EXTENSION_ID = __N6_EXTENSION__;
const CODE_READ_TOKEN_RE = /^bdb-n6-code-[0-9a-f-]{36}$/;
const CODE_READ_MAX_BYTES = 512 * 1024;
let port = null;
let validated = false;
let validation = null;
const pending = new Map();
function id(prefix) { return `${prefix}:${crypto.randomUUID()}`; }
function nativeError(response, fallback = "N6 Native Host identity handshake failed") {
  const error = response && response.error;
  const code = error && typeof error.code === "string" ? error.code : "native_status_invalid";
  const message = error && typeof error.message === "string" ? error.message : fallback;
  return new Error(`${code}: ${message}`);
}
function disconnect(activePort, error) {
  for (const [requestId, item] of pending.entries()) {
    if (item.port !== activePort) continue;
    clearTimeout(item.timer); pending.delete(requestId); item.reject(error);
  }
  if (port === activePort) { port = null; validated = false; }
}
function dropPort(error) {
  const activePort = port;
  if (!activePort) { validated = false; return; }
  disconnect(activePort, error);
  try { activePort.disconnect(); } catch (_) {}
}
function ensurePort() {
  if (port) return port;
  const connected = chrome.runtime.connectNative(HOST);
  port = connected;
  connected.onMessage.addListener((response) => {
    const item = response && pending.get(response.request_id);
    if (!item || item.port !== connected) return;
    pending.delete(response.request_id); clearTimeout(item.timer); item.resolve(response);
  });
  connected.onDisconnect.addListener(() => disconnect(connected, new Error((chrome.runtime.lastError && chrome.runtime.lastError.message) || "N6 Native Host disconnected")));
  return connected;
}
function post(event, payload, timeoutMs = 120000) {
  const request = {schema: REQUEST_SCHEMA, request_id: id(event), event, package_id: PACKAGE, protocol_generation: PROTOCOL, browser_native_binding_digest: BROWSER_NATIVE_BINDING, payload};
  return new Promise((resolve, reject) => {
    const activePort = ensurePort();
    const timer = setTimeout(() => { pending.delete(request.request_id); reject(new Error("N6 Native response timeout")); }, timeoutMs);
    pending.set(request.request_id, {resolve, reject, timer, port: activePort});
    try { activePort.postMessage(request); }
    catch (error) { clearTimeout(timer); pending.delete(request.request_id); dropPort(error); reject(error); }
  });
}
function exactStatus(response) {
  return Boolean(response && response.schema === RESPONSE_SCHEMA && response.status === "READY" && response.browser_native_binding_digest === BROWSER_NATIVE_BINDING && response.browser_extension_id === EXTENSION_ID && response.protocol_generation === PROTOCOL && response.production_activation === false);
}
async function ensureValidatedPort() {
  if (validated && port) return;
  if (validation) return validation;
  validation = (async () => {
    let failure = new Error("N6 Native Host identity handshake failed");
    for (let attempt = 0; attempt < 2; attempt += 1) {
      try {
        const response = await post("status", {}, 15000);
        if (!exactStatus(response)) throw nativeError(response);
        validated = true;
        return;
      } catch (error) {
        failure = error instanceof Error ? error : new Error(String(error));
        dropPort(failure);
      }
    }
    throw failure;
  })().finally(() => { validation = null; });
  return validation;
}
async function send(event, payload) {
  await ensureValidatedPort();
  return post(event, payload);
}
async function readMarkedCodeInMainWorld(sender, token) {
  if (!sender || !sender.tab || !Number.isInteger(sender.tab.id) || sender.tab.id < 0) throw new Error("N6 code read tab identity is invalid");
  if (typeof token !== "string" || !CODE_READ_TOKEN_RE.test(token)) throw new Error("N6 code read target token is invalid");
  const senderUrl = typeof sender.url === "string" ? sender.url : (typeof sender.tab.url === "string" ? sender.tab.url : "");
  let host = "";
  try { host = new URL(senderUrl).hostname.toLowerCase(); } catch (_) {}
  if (host !== "chatgpt.com" && host !== "chat.openai.com") throw new Error("N6 code read page origin is invalid");
  const executions = await chrome.scripting.executeScript({
    target: {tabId: sender.tab.id},
    world: "MAIN",
    func: (marker) => {
      const matches = [...document.querySelectorAll("pre[data-bdb-n6-code-read]")].filter((pre) => pre.getAttribute("data-bdb-n6-code-read") === marker);
      if (matches.length === 0) return {status: "MISSING", target_token: marker};
      if (matches.length !== 1) return {status: "AMBIGUOUS", target_token: marker};
      const pre = matches[0];
      const code = pre.querySelector("[data-language]");
      const text = code?.cmTile?.view?.state?.doc?.toString?.();
      if (typeof text !== "string") return {status: "INCOMPLETE", target_token: marker};
      return {status: "READY", target_token: marker, text, length: text.length, start: pre.getAttribute("data-start"), end: pre.getAttribute("data-end")};
    },
    args: [token],
  });
  const result = executions && executions.length === 1 ? executions[0].result : null;
  if (!result || result.target_token !== token) throw new Error("N6 code read result correlation failed");
  if (result.status !== "READY" || typeof result.text !== "string" || !Number.isInteger(result.length) || result.length !== result.text.length) throw new Error(`N6 complete CodeMirror artifact unavailable: ${result.status || "INVALID"}`);
  const byteLength = new TextEncoder().encode(result.text).length;
  if (byteLength <= 0 || byteLength > CODE_READ_MAX_BYTES) throw new Error("N6 complete CodeMirror artifact exceeds the observation bound");
  return {ok: true, result: {status: "READY", target_token: token, text: result.text, length: result.length, start: result.start, end: result.end}};
}
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message && message.type === "N6_READ_RENDERED_CODE") {
    readMarkedCodeInMainWorld(sender, message.token).then(sendResponse, (error) => sendResponse({ok: false, error: String(error)}));
    return true;
  }
  if (!message || message.type !== "N6_BROWSER_EVENT") return false;
  send(message.event, message.payload || {}).then((response) => sendResponse({ok: true, response}), (error) => sendResponse({ok: false, error: String(error)}));
  return true;
});
'''
    return script.replace("__N6_BINDING__", json.dumps(binding_digest)).replace("__N6_EXTENSION__", json.dumps(extension_id))


def _js_content() -> str:
    scenarios = json.dumps(
        {task["bdb"].replace("\r\n", "\n").strip(): task["id"] for task in N6_TASKS},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    script = r'''"use strict";
const OWNER_SCHEMA = "bdb-vnext-n6-conversation-owner-v1";
const PENDING_RESUME_SCHEMA = "bdb-vnext-n6-pending-resume-v1";
const RESUMED_BINDING_SCHEMA = "bdb-vnext-n6-resumed-binding-v1";
const PROTOCOL = "bdb-vnext-n6-protocol-v2";
const P1_ENGINEERING_PREFIX = __P1_ENGINEERING_PREFIX__;
const P1_EDIT_SCHEMA = "bdb-vnext-edit-v1";
const SCENARIO_BY_PROMPT = new Map(Object.entries(__N6_SCENARIOS__));
const PROMPT_BY_SCENARIO = new Map([...SCENARIO_BY_PROMPT].map(([prompt, scenario]) => [scenario, prompt]));
let active = null;
let resumedBinding = null;
let engineeringState = null;
let panel = null;
let currentConversation = null;
let currentRoute = null;
let synchronization = null;
const BLANK_CHAT_ROUTE = "chatgpt-blank-chat-scope";

function canonicalPrompt(text) { return String(text || "").replace(/\r\n?/g, "\n").trim(); }
function canonicalConversationId(href = location.href) {
  try {
    const url = new URL(href);
    const host = url.hostname.toLowerCase();
    if (host !== "chatgpt.com" && host !== "chat.openai.com") return null;
    const parts = url.pathname.split("/").filter(Boolean);
    for (let index = parts.length - 2; index >= 0; index -= 1) {
      if (parts[index] === "c" && /^[A-Za-z0-9_-]{8,128}$/.test(parts[index + 1])) {
        return "chatgpt-conversation:" + parts[index + 1];
      }
    }
  } catch (_) {}
  return null;
}
function renderedAssistantText(node) {
  const clone = node.cloneNode(true);
  for (const pre of clone.querySelectorAll("pre")) {
    const code = pre.querySelector("[data-language]");
    const language = code?.getAttribute("data-language") || "";
    const visible = code?.innerText || pre.innerText || pre.textContent || "";
    const documentText = code?.cmTile?.view?.state?.doc?.toString?.() || "";
    const content = canonicalPrompt(documentText.length > visible.length ? documentText : visible);
    pre.replaceWith(document.createTextNode("```" + language + "\n" + content + "\n```"));
  }
  return canonicalPrompt(clone.innerText || clone.textContent || "");
}
function readToken() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  return "bdb-n6-code-" + hex.slice(0, 8) + "-" + hex.slice(8, 12) + "-" + hex.slice(12, 16) + "-" + hex.slice(16, 20) + "-" + hex.slice(20);
}
function expectedCodeLength(pre) {
  if (!pre || typeof pre.getAttribute !== "function") return null;
  const start = Number(pre.getAttribute("data-start"));
  const end = Number(pre.getAttribute("data-end"));
  return Number.isFinite(start) && Number.isFinite(end) && end >= start ? end - start : null;
}
async function readMainWorldCodeText(pre, visible) {
  const token = readToken();
  pre.setAttribute("data-bdb-n6-code-read", token);
  try {
    const response = await chrome.runtime.sendMessage({type: "N6_READ_RENDERED_CODE", token});
    const result = response && response.ok === true ? response.result : null;
    if (!result || result.status !== "READY" || result.target_token !== token || typeof result.text !== "string" || result.length !== result.text.length) throw new Error((response && response.error) || "N6 complete CodeMirror artifact unavailable");
    if (result.length <= visible.length) throw new Error("N6 complete CodeMirror artifact is not longer than the visible fragment");
    return result.text;
  } finally {
    pre.removeAttribute("data-bdb-n6-code-read");
  }
}
async function renderedEngineeringAssistantText(node) {
  const clone = node.cloneNode(true);
  const sourcePres = [...node.querySelectorAll("pre")];
  const clonePres = [...clone.querySelectorAll("pre")];
  if (sourcePres.length !== clonePres.length) throw new Error("BDB engineering artifact target structure is ambiguous");
  const fullTexts = await Promise.all(sourcePres.map(async (pre) => {
    const code = pre.querySelector("[data-language]");
    if (!code) return "";
    const visible = canonicalPrompt(code.innerText || pre.innerText || pre.textContent || "");
    const expected = expectedCodeLength(pre);
    if (expected !== null && expected > visible.length + 32) return readMainWorldCodeText(pre, visible);
    return visible;
  }));
  for (let index = 0; index < clonePres.length; index += 1) {
    const pre = clonePres[index];
    const code = pre.querySelector("[data-language]");
    const language = code?.getAttribute("data-language") || "";
    const visible = canonicalPrompt(code?.innerText || pre.innerText || pre.textContent || "");
    const fullText = fullTexts[index] || "";
    const content = fullText || visible;
    pre.replaceWith(document.createTextNode("```" + language + "\n" + content + "\n```"));
  }
  return canonicalPrompt(clone.textContent || clone.innerText || "");
}
function assistantObservation(expectedPrompt) {
  const turns = [...document.querySelectorAll("[data-message-author-role]")];
  let promptIndex = -1;
  for (let index = 0; index < turns.length; index += 1) {
    const node = turns[index];
    if (node.dataset.messageAuthorRole === "user" && canonicalPrompt(node.innerText || node.textContent || "") === expectedPrompt) promptIndex = index;
  }
  if (promptIndex < 0) throw new Error("N6 exact frozen user prompt is unavailable in the canonical conversation DOM");
  const following = turns.slice(promptIndex + 1);
  if (following.length !== 1 || following[0].dataset.messageAuthorRole !== "assistant") throw new Error("N6 assistant answer is stale, ambiguous, or not the exact response to the frozen prompt");
  const node = following[0];
  if (node.closest("[data-bdb-n6-panel]")) throw new Error("N6 extension UI cannot witness its own presentation");
  const text = renderedAssistantText(node);
  return text ? {node, text} : null;
}
function streamInProgress() {
  return Boolean(document.querySelector("[data-testid='stop-button'],button[aria-label*='Stop'],button[aria-label*='Zatrzymaj']"));
}
async function stableAssistantObservation(expectedPrompt) {
  if (streamInProgress()) throw new Error("N6 assistant response is still streaming");
  const first = assistantObservation(expectedPrompt);
  if (!first) throw new Error("N6 visible assistant answer is unavailable");
  await new Promise((resolve) => setTimeout(resolve, 750));
  const second = assistantObservation(expectedPrompt);
  if (streamInProgress() || !second || first.text !== second.text) throw new Error("N6 assistant response completion is not stable");
  return {raw_answer: second.text, completion_observation: "DOM_TEXT_STABLE_AFTER_STREAM_END", dom_author_role: "assistant", extension_ui_ancestor: false};
}
function strictEngineeringArtifact(rawAnswer) {
  const matches = [...String(rawAnswer || "").matchAll(/```(?:json)?\s*([\s\S]*?)\s*```/gi)].map((match) => match[1].trim()).filter(Boolean);
  if (matches.length !== 1) throw new Error("BDB_EDIT_V1 requires exactly one fenced JSON artifact");
  let artifact;
  try { artifact = JSON.parse(matches[0]); } catch (_) { throw new Error("BDB_EDIT_V1 JSON is malformed"); }
  if (!artifact || typeof artifact !== "object" || artifact.schema !== P1_EDIT_SCHEMA || Array.isArray(artifact)) throw new Error("BDB_EDIT_V1 schema is missing");
  return artifact;
}
function latestEngineeringPrompt() {
  const turns = [...document.querySelectorAll("[data-message-author-role]")];
  for (let index = turns.length - 1; index >= 0; index -= 1) {
    const node = turns[index];
    if (node.dataset.messageAuthorRole === "user") {
      const text = canonicalPrompt(node.innerText || node.textContent || "");
      if (text.startsWith(P1_ENGINEERING_PREFIX + "\n")) return {node, text, index, turns};
    }
  }
  return null;
}
function engineeringObservation(prompt) {
  const next = prompt.turns[prompt.index + 1];
  if (!next || next.dataset.messageAuthorRole !== "assistant" || next.closest("[data-bdb-n6-panel]")) throw new Error("BDB engineering assistant answer is stale or ambiguous");
  return renderedEngineeringAssistantText(next).then((text) => text ? {raw_answer: text} : null);
}
async function stableEngineeringObservation(prompt) {
  if (streamInProgress()) throw new Error("BDB engineering assistant response is still streaming");
  const first = await engineeringObservation(prompt);
  if (!first) throw new Error("BDB engineering assistant answer is unavailable");
  await new Promise((resolve) => setTimeout(resolve, 750));
  const second = await engineeringObservation(prompt);
  if (streamInProgress() || !second || first.raw_answer !== second.raw_answer) throw new Error("BDB engineering assistant response is not stable");
  return second;
}
async function engineeringSubmissionKey(conversation, prompt) { return "p1-browser:" + await digest(PROTOCOL + "\\n" + conversation + "\\n" + prompt); }
async function engineeringProcessedKey(submissionKey, answerDigest) { return "p1_artifact:" + await digest(submissionKey + "\\n" + answerDigest); }
async function engineeringStateKey(conversation) { return "p1_state:" + await digest(conversation); }
async function restoreEngineeringState(conversation) {
  const key = await engineeringStateKey(conversation);
  const stored = await chrome.storage.local.get(key);
  const state = stored[key];
  return state && state.schema === "bdb-vnext-p1-engineering-state-v1" && state.conversation_id === conversation ? state : null;
}
async function persistEngineeringState(conversation, state) {
  const key = await engineeringStateKey(conversation);
  await chrome.storage.local.set({[key]: {...state, schema: "bdb-vnext-p1-engineering-state-v1", conversation_id: conversation}});
}
function userMessages() { return [...document.querySelectorAll("[data-message-author-role='user']")].map((node) => canonicalPrompt(node.innerText || node.textContent || "")); }
function send(event, payload) { return chrome.runtime.sendMessage({type: "N6_BROWSER_EVENT", event, payload}); }
function nativeErrorText(response, fallback = "N6 Native request failed") {
  if (response && response.response && response.response.error) {
    const error = response.response.error;
    const code = typeof error.code === "string" ? error.code : "native_error";
    const message = typeof error.message === "string" ? error.message : "Native Host rejected the request";
    return `${fallback}: ${code}: ${message}`;
  }
  if (response && typeof response.error === "string") return `${fallback}: ${response.error}`;
  return fallback;
}
function nativeResult(response, fallback) {
  if (!response || response.ok !== true || !response.response || response.response.status === "ERROR" || !response.response.result) return {result: null, error: nativeErrorText(response, fallback)};
  return {result: response.response.result, error: null};
}
async function digest(value) { const raw = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)); return [...new Uint8Array(raw)].map((byte) => byte.toString(16).padStart(2, "0")).join(""); }
async function ownerKey(conversation) { return "n6_owner:" + await digest(conversation); }
async function stableSubmissionKey(conversation, scenarioId, promptText) { return "n6-browser:" + await digest(PROTOCOL + "\n" + conversation + "\n" + scenarioId + "\n" + promptText); }
function validOwner(value, conversation = null) { return Boolean(value && value.schema === OWNER_SCHEMA && typeof value.submission_key === "string" && typeof value.conversation_id === "string" && (!conversation || value.conversation_id === conversation)); }
function clearPanel() { if (panel) panel.remove(); panel = null; }
function ensurePanel() { if (!panel) { panel = document.createElement("div"); panel.dataset.bdbN6Panel = "true"; panel.style.cssText = "position:fixed;right:16px;bottom:16px;z-index:2147483647;background:#111;color:#eee;padding:10px;border:1px solid #777;border-radius:8px;font:12px sans-serif;max-width:420px"; panel.innerHTML = "<strong>BDB vNext N6 rehearsal</strong><div class='n6-output'></div>"; document.documentElement.append(panel); } return panel; }
function write(text) { const output = ensurePanel().querySelector(".n6-output"); if (output) output.textContent = text; }
function addButton(label, callback) { const button = document.createElement("button"); button.textContent = label; button.style.margin = "2px"; button.addEventListener("click", () => { void Promise.resolve().then(callback).catch((error) => write(String(error))); }); panel.append(button); return button; }
function ownedConversation() {
  const live = canonicalConversationId();
  if (!live || live !== currentConversation || !validOwner(active, live)) throw new Error("N6 Browser conversation ownership changed");
  return live;
}

async function readOwner(conversation) {
  const key = await ownerKey(conversation);
  const stored = await chrome.storage.local.get(key);
  return validOwner(stored[key], conversation) ? stored[key] : null;
}
async function resumedBindingKey(conversation) { return "n6_resume:" + await digest(conversation); }
function validResumedBinding(value, conversation = null) { return Boolean(value && value.schema === RESUMED_BINDING_SCHEMA && typeof value.submission_key === "string" && typeof value.source_conversation_id === "string" && typeof value.target_conversation_id === "string" && (!conversation || value.target_conversation_id === conversation)); }
async function readResumedBinding(conversation) {
  const key = await resumedBindingKey(conversation);
  const stored = await chrome.storage.local.get(key);
  return validResumedBinding(stored[key], conversation) ? stored[key] : null;
}
async function persistResumedBinding(binding) {
  const key = await resumedBindingKey(binding.target_conversation_id);
  const stored = await chrome.storage.local.get(key);
  const existing = stored[key];
  if (existing && (!validResumedBinding(existing, binding.target_conversation_id) || existing.submission_key !== binding.submission_key || existing.capsule_id !== binding.capsule_id)) throw new Error("N6 Resume binding conflict");
  await chrome.storage.local.set({[key]: binding});
}
async function persistOwner(owner) {
  const key = await ownerKey(owner.conversation_id);
  const stored = await chrome.storage.local.get(key);
  const existing = stored[key];
  if (existing && (!validOwner(existing, owner.conversation_id) || existing.submission_key !== owner.submission_key || existing.prompt_digest !== owner.prompt_digest)) {
    throw new Error("N6 conversation ownership conflict");
  }
  await chrome.storage.local.set({[key]: owner});
}
async function setPendingResume() {
  ownedConversation();
  await chrome.storage.local.set({n6_pending_resume: {schema: PENDING_RESUME_SCHEMA, state: "PREPARED", owner: active}});
  write("New-chat Resume prepared for " + active.submission_key);
}
async function pendingResume() {
  const stored = await chrome.storage.local.get("n6_pending_resume");
  const pending = stored.n6_pending_resume;
  if (pending && pending.schema === PENDING_RESUME_SCHEMA && pending.state === "PREPARED" && validOwner(pending.owner)) return pending.owner;
  if (pending) await chrome.storage.local.remove("n6_pending_resume");
  return null;
}

function showPanel(result) {
  const live = canonicalConversationId();
  if (!live || live !== currentConversation) { clearPanel(); void synchronizeConversation(); return; }
  clearPanel(); ensurePanel();
  const publicationId = result.publication_id || (result.publication && result.publication.publication_id) || "unknown";
  const presentation = result.presentation || {};
  write("Task " + (result.task_id || "unknown") + "\nPublication " + publicationId + "\nOwner " + active.conversation_id);
  if (active.conversation_id === currentConversation) {
    addButton("Capture latest answer", async () => { const conversation = ownedConversation(); const expectedPrompt = PROMPT_BY_SCENARIO.get(active.scenario_id); if (!expectedPrompt) throw new Error("N6 frozen prompt identity is unavailable"); const observed = await stableAssistantObservation(expectedPrompt); const model = prompt("Visible ChatGPT model (exactly as shown):", "GPT-5.6 Sol"); if (model === null) throw new Error("N6 model attestation cancelled"); const reasoning = prompt("Visible reasoning setting (exactly as shown):", "Wysoki"); if (reasoning === null) throw new Error("N6 reasoning attestation cancelled"); const now = new Date().toISOString(); const response = await send("capture_answer", {submission_key: active.submission_key, conversation_id: conversation, profile_id: null, raw_answer: observed.raw_answer, completion_observation: observed.completion_observation, model, reasoning, started_at: active.started_at, finished_at: now}); const captured = response && response.response && response.response.result; if (!response.ok || !captured || typeof captured.evidence_id !== "string" || typeof captured.raw_answer_digest !== "string") throw new Error("N6 canonical assistant capture failed"); active = {...active, capture_evidence_id: captured.evidence_id, capture_answer_digest: captured.raw_answer_digest}; await persistOwner(active); write(JSON.stringify(response.response)); });
    addButton("Witness presentation", async () => { const conversation = ownedConversation(); if (typeof active.capture_evidence_id !== "string" || typeof active.capture_answer_digest !== "string") throw new Error("N6 exact assistant capture is required before presentation witness"); const expectedPrompt = PROMPT_BY_SCENARIO.get(active.scenario_id); if (!expectedPrompt) throw new Error("N6 frozen prompt identity is unavailable"); const observed = await stableAssistantObservation(expectedPrompt); const observedAnswerDigest = "sha256:" + await digest(observed.raw_answer); if (observedAnswerDigest !== active.capture_answer_digest) throw new Error("N6 visible assistant answer differs from the exact captured result"); const response = await send("witness", {submission_key: active.submission_key, conversation_id: conversation, marker: presentation.marker, observed_publication_id: publicationId, observed_result_digest: presentation.result_digest, capture_evidence_id: active.capture_evidence_id, observed_answer_digest: observedAnswerDigest, dom_author_role: observed.dom_author_role, completion_observation: observed.completion_observation, extension_ui_ancestor: observed.extension_ui_ancestor, composer_preserved: true}); write(JSON.stringify(response.response || response)); });
    addButton("Mark presentation UNKNOWN", async () => { const conversation = ownedConversation(); const response = await send("unknown", {submission_key: active.submission_key, conversation_id: conversation, reason: "manual_dom_witness_not_observed"}); write(JSON.stringify(response.response || response)); });
    addButton("Prepare new-chat Resume", setPendingResume);
  } else {
    addButton("Resume in this chat", async () => { const target = canonicalConversationId(); if (!target || target !== currentConversation || target === active.conversation_id) throw new Error("N6 Resume target ownership changed"); const response = await send("resume", {submission_key: active.submission_key, source_conversation_id: active.conversation_id, target_conversation_id: target, profile_id: null}); if (response.ok) await chrome.storage.local.remove("n6_pending_resume"); write(JSON.stringify(response.response || response)); });
  }
}
function showPendingResumePanel(result, owner, targetConversation) {
  const live = canonicalConversationId();
  if (!live || live !== currentConversation || live !== targetConversation) { clearPanel(); void synchronizeConversation(); return; }
  clearPanel(); ensurePanel();
  const publicationId = result.publication_id || (result.publication && result.publication.publication_id) || "unknown";
  write("Task " + (result.task_id || "unknown") + "\nPublication " + publicationId + "\nPending Resume from " + owner.conversation_id);
  addButton("Resume in this chat", async () => {
    const target = ownedConversationForResume(owner, targetConversation);
    const pending = await pendingResume();
    if (!pending || pending.submission_key !== owner.submission_key || pending.conversation_id !== owner.conversation_id) throw new Error("N6 Resume Capsule is stale or consumed");
    const response = await send("resume", {submission_key: owner.submission_key, source_conversation_id: owner.conversation_id, target_conversation_id: target, profile_id: null});
    if (!response.ok || !response.response || response.response.status === "ERROR") { write(JSON.stringify(response.response || response)); return; }
    const resumeResult = response.response.result || {};
    const capsule = resumeResult.capsule || {};
    const binding = {schema: RESUMED_BINDING_SCHEMA, submission_key: owner.submission_key, source_conversation_id: owner.conversation_id, target_conversation_id: target, capsule_id: resumeResult.capsule_id, publication_id: capsule.publication_id || publicationId, target_consumer_id: resumeResult.target_consumer_id};
    await persistResumedBinding(binding);
    await chrome.storage.local.remove("n6_pending_resume");
    resumedBinding = binding;
    showResumedBindingPanel(binding);
  });
}
function showBlankPendingResumePanel(result, owner) {
  if (canonicalConversationId() || currentRoute !== BLANK_CHAT_ROUTE) { clearPanel(); void synchronizeConversation(); return; }
  clearPanel(); ensurePanel();
  const publicationId = result.publication_id || (result.publication && result.publication.publication_id) || "unknown";
  write("Task " + (result.task_id || "unknown") + "\nPublication " + publicationId + "\nPrepared Resume — target chat is not yet canonical");
  addButton("Resume in this chat", async () => {
    if (!canonicalConversationId()) throw new Error("N6 Resume requires a stable ChatGPT conversation URL; send a message first");
    await synchronizeConversation();
  });
}
function ownedConversationForResume(owner, targetConversation) {
  const live = canonicalConversationId();
  if (!live || live !== currentConversation || live !== targetConversation || live === owner.conversation_id) throw new Error("N6 Resume target ownership changed");
  return live;
}
function showResumedBindingPanel(binding) {
  const live = canonicalConversationId();
  if (!live || live !== currentConversation || live !== binding.target_conversation_id) { clearPanel(); void synchronizeConversation(); return; }
  clearPanel(); ensurePanel();
  write("Resume bound in this chat\nPublication " + binding.publication_id + "\nSource " + binding.source_conversation_id + "\nCapsule " + binding.capsule_id);
}
async function lookupAndShow(owner, expectedConversation) {
  active = owner;
  const response = await send("lookup", {submission_key: owner.submission_key});
  if (expectedConversation !== currentConversation || canonicalConversationId() !== expectedConversation) return;
  const observed = nativeResult(response, "N6 canonical lookup failed");
  if (observed.result) showPanel(observed.result);
  else write(observed.error);
}
async function lookupAndShowPending(owner, expectedConversation) {
  const response = await send("lookup", {submission_key: owner.submission_key});
  if (expectedConversation !== currentConversation || canonicalConversationId() !== expectedConversation) return;
  if (response.ok && response.response && response.response.result) { showPendingResumePanel(response.response.result, owner, expectedConversation); return; }
  if (response.response && response.response.error && ["submission_not_found", "publication_missing"].includes(response.response.error.code)) await chrome.storage.local.remove("n6_pending_resume");
}
async function lookupAndShowPendingBlank(owner) {
  const response = await send("lookup", {submission_key: owner.submission_key});
  if (currentRoute !== BLANK_CHAT_ROUTE || canonicalConversationId()) return;
  if (response.ok && response.response && response.response.result) { showBlankPendingResumePanel(response.response.result, owner); return; }
  if (response.response && response.response.error && ["submission_not_found", "publication_missing"].includes(response.response.error.code)) await chrome.storage.local.remove("n6_pending_resume");
}
async function lookupAndShowResumed(binding, expectedConversation) {
  const response = await send("lookup", {submission_key: binding.submission_key});
  if (expectedConversation !== currentConversation || canonicalConversationId() !== expectedConversation) return;
  if (response.ok && response.response && response.response.result) { showResumedBindingPanel(binding); return; }
  await chrome.storage.local.remove(await resumedBindingKey(expectedConversation));
  resumedBinding = null;
}
function showEngineeringResult(result, submissionKey) {
  clearPanel(); ensurePanel();
  const status = result.validation_status || result.status || "READY";
  write("P1 Calculator engineering\nSubmission " + submissionKey + "\nValidation " + status + (result.feedback ? "\n" + result.feedback.slice(0, 1800) : ""));
  if (result.ready_to_seal === true) {
    addButton("Seal engineering Candidate", async () => {
      const response = await send("engineering_finalize", {submission_key: submissionKey, candidate_id: result.candidate_id});
      if (!response.ok || !response.response || response.response.status === "ERROR") throw new Error(nativeErrorText(response, "P1 Candidate seal failed"));
      write(JSON.stringify(response.response));
    });
  }
}
async function inspectEngineeringScenario(conversation) {
  if (!conversation) return;
  const prompt = latestEngineeringPrompt();
  if (!prompt) return;
  let prepared = engineeringState && engineeringState.conversation_id === conversation ? engineeringState : await restoreEngineeringState(conversation);
  if (!prepared) {
    const submissionKey = await engineeringSubmissionKey(conversation, prompt.text);
    const promptDigest = "sha256:" + await digest(prompt.text);
    const response = await send("engineering_prepare", {submission_key: submissionKey, prompt: prompt.text, conversation_id: conversation, profile_id: null});
    const observed = nativeResult(response, "P1 engineering preparation failed");
    if (!observed.result) { write(observed.error); return; }
    prepared = {...observed.result, submission_key: submissionKey, prompt_digest: promptDigest, initial_prompt_digest: promptDigest};
    engineeringState = prepared;
    await persistEngineeringState(conversation, prepared);
    showEngineeringResult({status: "ENGINEERING_READY", ...prepared}, submissionKey);
  }
  const submissionKey = prepared.submission_key;
  let observed;
  try { observed = await stableEngineeringObservation(prompt); } catch (_) { return; }
  const answerDigest = "sha256:" + await digest(observed.raw_answer);
  const processedKey = await engineeringProcessedKey(submissionKey, answerDigest);
  const processed = await chrome.storage.local.get(processedKey);
  if (processed[processedKey]) return;
  let artifact;
  try { artifact = strictEngineeringArtifact(observed.raw_answer); } catch (error) { showEngineeringResult({status: "ARTIFACT_REJECTED", feedback: String(error)}, submissionKey); return; }
  const response = await send("engineering_artifact", {submission_key: submissionKey, conversation_id: conversation, prompt_digest: prepared.prompt_digest, raw_answer: observed.raw_answer, raw_answer_digest: answerDigest, artifact});
  const result = nativeResult(response, "P1 engineering artifact rejected");
  if (!result.result) { showEngineeringResult({status: "ARTIFACT_REJECTED", feedback: result.error}, submissionKey); return; }
  await chrome.storage.local.set({[processedKey]: {schema: "bdb-vnext-p1-processed-artifact-v1", submission_key: submissionKey, answer_digest: answerDigest, artifact_digest: result.result.artifact_digest}});
  engineeringState = {...prepared, ...result.result, submission_key: submissionKey, conversation_id: conversation};
  await persistEngineeringState(conversation, engineeringState);
  showEngineeringResult(engineeringState, submissionKey);
}
async function restoreConversation(conversation) {
  active = null; resumedBinding = null; clearPanel();
  const owner = await readOwner(conversation);
  if (owner) { await lookupAndShow(owner, conversation); return; }
  const bound = await readResumedBinding(conversation);
  if (bound) { resumedBinding = bound; await lookupAndShowResumed(bound, conversation); return; }
  const pending = await pendingResume();
  if (pending && pending.conversation_id !== conversation) await lookupAndShowPending(pending, conversation);
}
async function restoreBlankConversation() {
  active = null; resumedBinding = null; clearPanel();
  const pending = await pendingResume();
  if (pending) await lookupAndShowPendingBlank(pending);
}
async function inspectExactScenario(conversation) {
  if (active) return;
  for (const text of userMessages()) {
    const scenarioId = SCENARIO_BY_PROMPT.get(text);
    if (!scenarioId) continue;
    const promptDigest = await digest(text);
    const submissionKey = await stableSubmissionKey(conversation, scenarioId, text);
    const owner = {schema: OWNER_SCHEMA, submission_key: submissionKey, conversation_id: conversation, scenario_id: scenarioId, prompt_digest: promptDigest, started_at: new Date().toISOString()};
    await persistOwner(owner);
    active = owner;
    const response = await send("submit_prompt", {submission_key: submissionKey, prompt: text, conversation_id: conversation, profile_id: null});
    if (conversation !== currentConversation || canonicalConversationId() !== conversation) return;
    const observed = nativeResult(response, "N6 canonical admission failed");
    if (!observed.result) { write(observed.error); return; }
    showPanel(observed.result);
    return;
  }
}
async function synchronizeConversation() {
  if (synchronization) return synchronization;
  synchronization = (async () => {
    const conversation = canonicalConversationId();
    const route = conversation || BLANK_CHAT_ROUTE;
    if (route !== currentRoute) {
      currentRoute = route;
      if (conversation) { currentConversation = conversation; await restoreConversation(conversation); }
      else { currentConversation = null; await restoreBlankConversation(); }
    }
    if (conversation) { await inspectExactScenario(conversation); await inspectEngineeringScenario(conversation); }
  })().finally(() => { synchronization = null; if ((canonicalConversationId() || BLANK_CHAT_ROUTE) !== currentRoute) void synchronizeConversation(); });
  return synchronization;
}
new MutationObserver(() => { void synchronizeConversation(); }).observe(document.documentElement, {subtree: true, childList: true, characterData: true});
window.addEventListener("popstate", () => { void synchronizeConversation(); });
setInterval(() => { void synchronizeConversation(); }, 1000);
void synchronizeConversation();
'''
    return script.replace("__N6_SCENARIOS__", scenarios).replace("__P1_ENGINEERING_PREFIX__", json.dumps(P1_ENGINEERING_PREFIX))


def _cs_shim_source(python_executable: Path, native_code_root: Path, config_path: Path) -> str:
    def esc(value: Path) -> str:
        return str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'''using System.Diagnostics;\nusing System.IO;\nvar psi = new ProcessStartInfo {{ FileName = "{esc(python_executable)}", Arguments = "-m bdb_vnext.n6_rehearsal native-host --config \\"{esc(config_path)}\\"", WorkingDirectory = "{esc(native_code_root)}", UseShellExecute = false, RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true, CreateNoWindow = true }};\npsi.Environment["PYTHONPATH"] = "{esc(native_code_root)}";\npsi.Environment["PYTHONNOUSERSITE"] = "1";\nusing var child = Process.Start(psi) ?? throw new InvalidOperationException("N6 Python host could not start");\nvar input = Console.OpenStandardInput(); var output = Console.OpenStandardOutput();\nasync Task PumpInput() {{ await input.CopyToAsync(child.StandardInput.BaseStream); child.StandardInput.Close(); }}\nvar toChild = PumpInput(); var fromChild = child.StandardOutput.BaseStream.CopyToAsync(output);\nawait Task.WhenAll(toChild, fromChild);\nawait child.WaitForExitAsync();\n'''


def _build_shim(output: Path, *, python_executable: Path, native_code_root: Path, config_path: Path) -> Path | None:
    dotnet = shutil.which("dotnet")
    if dotnet is None:
        return None
    source = output / "native-shim-src"
    publish = output / "native"
    source.mkdir(parents=True, exist_ok=True)
    (source / "Program.cs").write_text(_cs_shim_source(python_executable, native_code_root, config_path), encoding="utf-8")
    (source / "N6NativeHostShim.csproj").write_text("<Project Sdk=\"Microsoft.NET.Sdk\"><PropertyGroup><OutputType>Exe</OutputType><TargetFramework>net10.0</TargetFramework><ImplicitUsings>enable</ImplicitUsings><Nullable>enable</Nullable></PropertyGroup></Project>\n", encoding="utf-8")
    build_environment = os.environ.copy()
    build_environment["DOTNET_CLI_HOME"] = str(output / ".dotnet")
    build_environment["DOTNET_SKIP_FIRST_TIME_EXPERIENCE"] = "1"
    result = subprocess.run([dotnet, "build", str(source / "N6NativeHostShim.csproj"), "-c", "Release", "-p:RestoreIgnoreFailedSources=true", "--ignore-failed-sources", "-o", str(publish)], shell=False, capture_output=True, text=True, env=build_environment, timeout=180, check=False)
    if result.returncode != 0:
        return None
    executable = publish / "N6NativeHostShim.exe"
    return executable if executable.is_file() else None


N6_TASKS: tuple[dict[str, str], ...] = (
    {"id": "RUN-01", "class": "small", "bdb": "BDB-N6-REHEARSAL RUN-01\nInspect one exact function and state its owner, inputs, outputs, and one focused test. Do not modify files.", "plain": "Inspect one exact function and state its owner, inputs, outputs, and one focused test. Do not modify files."},
    {"id": "RUN-02", "class": "small", "bdb": "BDB-N6-REHEARSAL RUN-02\nExplain the smallest safe validation command for one named module and what it proves. Do not modify files.", "plain": "Explain the smallest safe validation command for one named module and what it proves. Do not modify files."},
    {"id": "RUN-03", "class": "medium", "bdb": "BDB-N6-REHEARSAL RUN-03\nTrace a cross-file request from its typed boundary to its durable reader. Identify the single writer and one failure boundary. Do not modify files.", "plain": "Trace a cross-file request from its typed boundary to its durable reader. Identify the single writer and one failure boundary. Do not modify files."},
    {"id": "RUN-04", "class": "medium", "bdb": "BDB-N6-REHEARSAL RUN-04\nCompare two related modules and identify which one is authoritative, which is projection, and which evidence would be required to prove the distinction. Do not modify files.", "plain": "Compare two related modules and identify which one is authoritative, which is projection, and which evidence would be required to prove the distinction. Do not modify files."},
    {"id": "RUN-05", "class": "complex", "bdb": "BDB-N6-REHEARSAL RUN-05\nAnalyze a stale-worker recovery path across admission, WorkItem lease/fence, Candidate observation, and publication. Preserve UNKNOWN where the filesystem observation is insufficient. Do not modify files.", "plain": "Analyze a stale-worker recovery path across admission, WorkItem lease/fence, Candidate observation, and publication. Preserve UNKNOWN where the filesystem observation is insufficient. Do not modify files."},
    {"id": "RUN-06", "class": "complex", "bdb": "BDB-N6-REHEARSAL RUN-06\nReview the repository's Browser-first boundary across ContextPackage, RepoView, Native transport, and operator query. Identify one authority that must not move into the Browser and one exact missing evidence request. Do not modify files.", "plain": "Review the repository's Browser-first boundary across ContextPackage, RepoView, Native transport, and operator query. Identify one authority that must not move into the Browser and one exact missing evidence request. Do not modify files."},
)


def prepare_package(*, repo_root: str | Path, output: str | Path, runtime_root: str | Path, legacy_runtime_root: str | Path, source_commit: str | None = None, python_executable: str | Path | None = None, engineering_target: Mapping[str, Any] | None = None) -> dict[str, Any]:
    repo = _safe_abs(repo_root, "repo_root")
    output_path = _safe_abs(output, "output")
    runtime = _safe_abs(runtime_root, "runtime_root")
    legacy = _safe_abs(legacy_runtime_root, "legacy_runtime_root")
    if _overlap(runtime, repo) or _overlap(runtime, legacy) or _overlap(runtime, output_path) or _overlap(output_path, legacy):
        _fail("foreign_state_overlap", "N6 package/runtime overlaps source or legacy")
    output_path.mkdir(parents=True, exist_ok=True)
    browser = output_path / "browser-extension"
    browser.mkdir(parents=True, exist_ok=True)
    identity = load_browser_identity()
    manifest = {"manifest_version": 3, "name": "BDB vNext N6 Rehearsal", "version": N6_PACKAGE_VERSION, "description": "Build-only user-operated BDB vNext rehearsal; not product activation.", "key": identity["public_key_spki_der_base64"], "permissions": ["nativeMessaging", "storage", "scripting"], "host_permissions": ["https://chatgpt.com/*", "https://chat.openai.com/*"], "background": {"service_worker": "background.js"}, "content_scripts": [{"matches": ["https://chatgpt.com/*", "https://chat.openai.com/*"], "js": ["content.js"], "run_at": "document_idle"}]}
    requested_commit = source_commit or "HEAD"
    commit = subprocess.run(["git", "-C", str(repo), "rev-parse", f"{requested_commit}^{{commit}}"], shell=False, capture_output=True, text=True, check=True).stdout.strip()
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        _fail("subject_invalid", "N6 source commit did not resolve to an exact Git object")
    tree = subprocess.run(["git", "-C", str(repo), "rev-parse", f"{commit}^{{tree}}"], shell=False, capture_output=True, text=True, check=True).stdout.strip()
    source_view = RepositoryResource.from_path(repo, repository_id="bdb-vnext-n6-subject").resolve_committed(commit)
    config_path = output_path / "native-config.json"
    py = Path(python_executable or sys.executable).expanduser().absolute()
    external_interpreter = interpreter_identity(py)
    target = None
    if engineering_target is not None:
        raw_target = dict(_mapping(engineering_target, "engineering_target"))
        target_root = _safe_abs(raw_target.get("repo_root"), "engineering_target.repo_root")
        target_commit = str(raw_target.get("commit") or raw_target.get("source_commit") or "HEAD")
        target_source = RepositoryResource.from_path(target_root, repository_id=str(raw_target.get("repository_id") or "bdb-p1-calculator"))
        target_view = target_source.resolve_committed(target_commit)
        branch_result = subprocess.run(["git", "-C", str(target_root), "branch", "--show-current"], shell=False, capture_output=True, text=True, check=True, timeout=20)
        target_checker = dict(raw_target.get("checker") or {"checker_id": P1_ENGINEERING_CHECKER_ID, "checker_version": P1_ENGINEERING_CHECKER_VERSION, "argv": [str(py), "tests/test_calculator.py"], "cwd": ".", "timeout_seconds": 60.0})
        target_document = {"schema": P1_ENGINEERING_TARGET_SCHEMA, "repository_id": target_source.repository_id, "repo_root": str(target_root), "branch": str(raw_target.get("branch") or branch_result.stdout.strip()), "commit": target_view.commit_oid, "tree": target_view.tree_oid, "view_id": target_view.view_id, "allowed_paths": list(raw_target.get("allowed_paths") or ["index.html", "styles.css", "app.js"]), "checker": target_checker}
        target = _validated_engineering_target(target_document, forbidden_roots=(repo, runtime, legacy, output_path))
    native_code = output_path / "native-code"
    native_code.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", commit, "bdb_vnext", "bdb_shared", "pyproject.toml"], shell=False, capture_output=True, check=True)
    import tarfile
    with tarfile.open(fileobj=__import__("io").BytesIO(archive.stdout), mode="r:") as tar:
        tar.extractall(native_code)
    native_digest = native_code_digest(native_code)
    binding = _browser_native_binding(
        source_commit=commit,
        source_tree=tree,
        repo_view_id=source_view.view_id,
        runtime_root=runtime,
        package_root=output_path,
        legacy_runtime_root=legacy,
        native_code_digest_value=native_digest,
        interpreter_identity_digest=external_interpreter["identity_digest"],
        browser_extension_id=identity["extension_id"],
    )
    config = {"schema": N6_CONFIG_SCHEMA, "repo_root": str(repo), "runtime_root": str(runtime), "legacy_runtime_root": str(legacy), "source_commit": commit, "package_root": str(output_path), "native_code_root": str(native_code), "native_code_digest": native_digest, "package_digest": "pending", "interpreter_identity": external_interpreter, "browser_native_binding": binding, "browser_extension_id": identity["extension_id"], "native_host_name": N6_NATIVE_HOST_NAME, "protocol_generation": N6_PROTOCOL_GENERATION, "production_activation": False}
    if target is not None:
        config["engineering_target"] = target
    _write_json(browser / "manifest.json", manifest)
    (browser / "background.js").write_text(_js_background(binding["binding_digest"], identity["extension_id"]), encoding="utf-8")
    (browser / "content.js").write_text(_js_content(), encoding="utf-8")
    _write_json(config_path, config)
    shim = _build_shim(output_path, python_executable=py, native_code_root=native_code, config_path=config_path)
    native_manifest_path = output_path / "native-host-manifest.json"
    native_path = shim or (output_path / "native-host.py")
    if shim is None:
        (output_path / "native-host.py").write_text("import sys\nsys.dont_write_bytecode = True\nfrom pathlib import Path\ncode = Path(__file__).resolve().parent / 'native-code'\nsys.path.insert(0, str(code))\nfrom bdb_vnext.n6_rehearsal import run_native_host\nrun_native_host(Path(__file__).resolve().parent / 'native-config.json')\n", encoding="utf-8")
    native_manifest = {"name": N6_NATIVE_HOST_NAME, "description": "BDB vNext N6 build-only rehearsal Native Host", "path": str(native_path), "type": "stdio", "allowed_origins": [f"chrome-extension://{identity['extension_id']}/"]}
    _write_json(native_manifest_path, native_manifest)
    register_script = output_path / "register-native-host.ps1"
    register_script.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        f"$key = 'HKCU:\\Software\\Google\\Chrome\\NativeMessagingHosts\\{N6_NATIVE_HOST_NAME}'\n"
        "if (Test-Path -LiteralPath $key) { throw 'Refusing to overwrite an existing Native Host registration.' }\n"
        "New-Item -Path $key -Force | Out-Null\n"
        f"New-ItemProperty -Path $key -Name '(default)' -Value '{str(native_manifest_path).replace(chr(39), chr(39)+chr(39))}' -PropertyType String -Force | Out-Null\n"
        "Write-Output ('Registered dedicated N6 Native Host: ' + $key)\n",
        encoding="utf-8",
    )
    execution_package = {"schema": N6_PACKAGE_SCHEMA, "version": N6_PACKAGE_VERSION, "digest": "pending", "root": str(output_path), "native_code_digest": config["native_code_digest"], "interpreter_identity": external_interpreter, "browser_native_binding": binding, "browser_extension": {"component_id": identity["component_id"], "extension_id": identity["extension_id"], "semantic_digest": identity["semantic_digest"], "manifest": str(browser / "manifest.json")}, "native_host": {"name": N6_NATIVE_HOST_NAME, "manifest": str(native_manifest_path), "path": str(native_path), "registration_script": str(register_script), "executable_ready": shim is not None, "code_root": str(native_code)}, "protocol_generation": N6_PROTOCOL_GENERATION}
    if target is not None:
        execution_package["engineering_target"] = target
    execution = {"schema": N6_EXECUTION_SCHEMA, "package": execution_package, "subject": {"repository": "bdb-vnext-n6-subject", "repo_root": str(repo), "branch": "bdb-vnext", "commit": commit, "tree": tree, "view_id": source_view.view_id}, "resources": {"runtime_root": str(runtime), "control_db": str(runtime / "control" / "control.db"), "legacy_runtime_root": str(legacy), "production_activation": False, "rehearsal_infrastructure": "ACTIVE_ONLY_WHEN_EXPLICIT_PACKAGE_LOADED", "legacy_mutation": False}, "prompts": list(N6_TASKS), "manual_gate": "USER_OPERATED_ONLY"}
    execution_path = output_path / "execution_manifest.json"
    _write_json(execution_path, execution)
    package = package_digest(output_path)
    config["package_digest"] = package
    execution["package"]["digest"] = package
    _write_json(config_path, config)
    _write_json(execution_path, execution)
    if package_digest(output_path) != package:
        _fail("package_identity_unstable", "N6 compound execution identity is not deterministic")
    return execution


def write_manual_packet(execution: Mapping[str, Any], path: str | Path) -> Path:
    package = _mapping(execution.get("package"), "package")
    subject = _mapping(execution.get("subject"), "subject")
    resources = _mapping(execution.get("resources"), "resources")
    lines = ["# MANUAL_BROWSER_REHEARSAL_PACKET — N6", "", "## Exact subject", "", f"repository: {subject['repository']}", f"branch: {subject['branch']}", f"HEAD: {subject['commit']}", f"tree: {subject['tree']}", f"RepoView: {subject['view_id']}", f"Browser extension ID: {package['browser_extension']['extension_id']}", f"Browser package digest: {package['digest']}", f"Browser/Native binding: {package['browser_native_binding']['binding_digest']}", f"Browser manifest: {package['browser_extension']['manifest']}", f"Native Host: {package['native_host']['name']}", f"Native Host manifest: {package['native_host']['manifest']}", f"Native registration script: {package['native_host'].get('registration_script')}", f"Native executable ready: {package['native_host']['executable_ready']}", f"Protocol: {package['protocol_generation']}", f"Control DB: {resources['control_db']}", f"Rehearsal runtime root: {resources['runtime_root']}", ""]
    if package.get("engineering_target"):
        target = _mapping(package["engineering_target"], "engineering_target")
        lines.extend(["## P1 engineering target", "", f"repository: {target['repository_id']}", f"root: {target['repo_root']}", f"branch: {target['branch']}", f"commit: {target['commit']}", f"tree: {target['tree']}", f"RepoView: {target['view_id']}", f"allowed paths: {', '.join(target['allowed_paths'])}", f"checker: {target['checker']['checker_id']} {target['checker']['checker_version']}", ""])
    lines.extend(["## Setup", "", "1. Do not touch the installed legacy extension or Native Host.", f"2. Before loading or reloading the extension, verify the dedicated `{package['native_host']['name']}` registry default points to `{package['native_host']['manifest']}` and that the manifest allowed origin is `chrome-extension://{package['browser_extension']['extension_id']}/`. The generated registration script may be used only when that dedicated key is absent; it refuses to overwrite an existing key.", f"3. Only after step 2, load or reload the unpacked folder `{Path(str(package['browser_extension']['manifest'])).parent}` in `chrome://extensions` and verify the pinned extension ID. This ordering prevents Chrome from retaining a Native port opened against an earlier manifest.", "4. Confirm `Native executable ready` is true, then open a normal ChatGPT conversation, choose visible model `GPT-5.6 Sol` and reasoning `Wysoki`, and keep those settings for all paired tasks.", "5. Start with a fresh conversation. Paste each BDB prompt below exactly. Wait for the normal answer before using the extension panel.", "6. For the primary vertical, click `Capture latest answer`, then `Witness presentation`. Use `Mark presentation UNKNOWN` when the DOM witness is intentionally absent.", "7. For Resume, open a new ChatGPT conversation and click `Resume in this chat`; the target conversation must remain distinct.", "8. For restart/lost-ACK, refresh ChatGPT after submitting a marked prompt, then wait for the panel to recover by lookup. Never submit the same prompt twice manually.", "", "## Expected observations", "", "PASS = normal ChatGPT answer is visible, the panel reports canonical IDs, and the requested witness/recovery result is explicit.", "FAIL = extension/Native identity mismatch, duplicate Task/Candidate/Publication, wrong conversation delivery, silent fallback, or mutation outside the rehearsal root.", "INCONCLUSIVE = model/settings/timestamps/raw answer or exact identity cannot be verified.", "", "## Primary vertical", "", "RUN-PRIMARY: use `RUN-05` below. Verify Task → WorkItem → Candidate → Evidence → Publication, capture raw answer, witness same conversation, mark UNKNOWN once, then new-chat Resume.", "", "## Paired prompts (run BDB arm with extension enabled; run NO-BDB arm with extension disabled)", ""])
    for task in execution.get("prompts", []):
        lines.extend([f"### {task['id']} — {task['class']}", "", "BDB arm:", "```text", str(task["bdb"]), "```", "", "NO-BDB arm:", "```text", str(task["plain"]), "```", ""])
    lines.extend(["## Fault actions", "", "- Refresh/reopen ChatGPT only; do not delete runtime files.", "- If the Native Host disconnects, stop and record the visible error; do not retry blindly.", "- To test UNKNOWN, do not click the witness button and click `Mark presentation UNKNOWN`.", "- For new-chat Resume, use a different conversation and confirm the old conversation is not reused.", "", "## Fallback evidence template", "", "```text", "RUN ID:", "START:", "END:", "CHATGPT MODEL/SETTING:", "BROWSER STEP RESULT:", "VISIBLE ERROR:", "REFRESH/RESTART PERFORMED:", "FINAL VISIBLE RESULT:", "NOTES:", "```", "", "N6 remains build-only; no product activation, legacy mutation, Git ref movement or push is authorized."])
    destination = Path(path).expanduser().absolute()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return destination


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m bdb_vnext.n6_rehearsal")
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--repo-root", required=True)
    prepare.add_argument("--output", required=True)
    prepare.add_argument("--runtime-root", required=True)
    prepare.add_argument("--legacy-runtime-root", required=True)
    prepare.add_argument("--source-commit")
    prepare.add_argument("--python")
    prepare.add_argument("--packet")
    host = sub.add_parser("native-host")
    host.add_argument("--config", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "prepare":
        execution = prepare_package(repo_root=args.repo_root, output=args.output, runtime_root=args.runtime_root, legacy_runtime_root=args.legacy_runtime_root, source_commit=args.source_commit, python_executable=args.python)
        packet = write_manual_packet(execution, args.packet or str(Path(args.output).expanduser().absolute() / "MANUAL_BROWSER_REHEARSAL_PACKET.md"))
        print(json.dumps({"status": "READY_FOR_MANUAL_BROWSER_GATE", "execution_manifest": str(Path(args.output).expanduser().absolute() / "execution_manifest.json"), "packet": str(packet), "package_digest": execution["package"]["digest"], "native_executable_ready": execution["package"]["native_host"]["executable_ready"]}, sort_keys=True))
        return 0
    return run_native_host(args.config)


__all__ = ["N6_CONFIG_SCHEMA", "N6_EVENT_SCHEMA", "N6_NATIVE_REQUEST_SCHEMA", "N6_NATIVE_RESPONSE_SCHEMA", "N6_PACKAGE_SCHEMA", "N6_PROTOCOL_GENERATION", "N6RehearsalConfig", "N6RehearsalError", "N6RehearsalService", "N6_TASKS", "native_code_digest", "package_digest", "prepare_package", "run_native_host", "write_manual_packet"]


if __name__ == "__main__":
    raise SystemExit(main())

"""Small project-owned execution identity for the inactive M4c checker.

The checker intentionally uses the current project interpreter with user-site
packages disabled.  The project has no runtime dependency lockfile, so the
empty runtime dependency declaration in ``pyproject.toml`` is the bounded
package-set identity.  This is an execution contract, not a general
environment manager.
"""

from __future__ import annotations

import hashlib
import os
import platform
import site
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from bdb_shared.evidence import canonical_json_bytes, semantic_digest


ENVIRONMENT_SCHEMA = "bdb-vnext-m4c-checker-environment-v1"
DEPENDENCY_IDENTITY_SCHEMA = "bdb-vnext-m4c-runtime-dependencies-v1"


class CheckerEnvironmentError(RuntimeError):
    """Environment identity cannot be established fail-closed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _digest_bytes(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _sha256_file(path: Path) -> str:
    try:
        return _digest_bytes(path.read_bytes())
    except OSError as exc:
        raise CheckerEnvironmentError("interpreter_or_manifest_unavailable", str(path)) from exc


def _root(value: str | Path) -> Path:
    path = Path(value).expanduser().absolute()
    if not path.is_dir():
        raise CheckerEnvironmentError("project_root_missing", "checker project root is not a directory")
    return path


def _dependency_identity(root: Path) -> tuple[str, str]:
    manifest = root / "pyproject.toml"
    try:
        document = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise CheckerEnvironmentError("project_manifest_invalid", "pyproject.toml cannot be verified") from exc
    project = document.get("project")
    if not isinstance(project, Mapping):
        raise CheckerEnvironmentError("project_manifest_invalid", "pyproject.toml has no project table")
    dependencies = project.get("dependencies", [])
    if not isinstance(dependencies, list) or any(not isinstance(item, str) for item in dependencies):
        raise CheckerEnvironmentError("dependency_identity_invalid", "runtime dependencies are not a string list")
    lock_candidates = ("uv.lock", "poetry.lock", "Pipfile.lock", "requirements.lock", "requirements.txt")
    lock = next((root / name for name in lock_candidates if (root / name).is_file()), None)
    if lock is not None:
        return f"lockfile:{lock.name}", _sha256_file(lock)
    identity = {
        "schema": DEPENDENCY_IDENTITY_SCHEMA,
        "runtime_dependencies": sorted(dependencies),
        "lockfile": None,
    }
    return "pyproject-runtime-dependencies", semantic_digest(identity)


def _base_identity(root: Path, *, no_user_site: bool) -> dict[str, Any]:
    interpreter = Path(sys.executable).expanduser().absolute()
    if not interpreter.is_file():
        raise CheckerEnvironmentError("interpreter_unavailable", "checker interpreter is not a regular file")
    project_manifest = root / "pyproject.toml"
    dependency_source, dependency_digest = _dependency_identity(root)
    return {
        "schema": ENVIRONMENT_SCHEMA,
        "project_root": str(root),
        "interpreter_path": str(interpreter),
        "interpreter_version": sys.version,
        "interpreter_digest": _sha256_file(interpreter),
        "project_manifest_digest": _sha256_file(project_manifest),
        "dependency_source": dependency_source,
        "dependency_digest": dependency_digest,
        "os_identity": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python_implementation": platform.python_implementation(),
        },
        "no_user_site": bool(no_user_site),
    }


def _fingerprint(identity: Mapping[str, Any]) -> str:
    return semantic_digest(identity)


@dataclass(frozen=True)
class CheckerEnvironment:
    schema: str
    environment_id: str
    project_root: str
    interpreter_path: str
    interpreter_version: str
    interpreter_digest: str
    project_manifest_digest: str
    dependency_source: str
    dependency_digest: str
    os_identity: Mapping[str, str]
    no_user_site: bool
    fingerprint: str

    @classmethod
    def expected(cls, project_root: str | Path) -> "CheckerEnvironment":
        root = _root(project_root)
        identity = _base_identity(root, no_user_site=True)
        fingerprint = _fingerprint(identity)
        return cls(
            schema=ENVIRONMENT_SCHEMA,
            environment_id=fingerprint,
            project_root=str(root),
            interpreter_path=str(identity["interpreter_path"]),
            interpreter_version=str(identity["interpreter_version"]),
            interpreter_digest=str(identity["interpreter_digest"]),
            project_manifest_digest=str(identity["project_manifest_digest"]),
            dependency_source=str(identity["dependency_source"]),
            dependency_digest=str(identity["dependency_digest"]),
            os_identity=dict(identity["os_identity"]),
            no_user_site=True,
            fingerprint=fingerprint,
        )

    @classmethod
    def observed(cls, project_root: str | Path) -> "CheckerEnvironment":
        root = _root(project_root)
        user_site = site.getusersitepackages()
        actual_no_user_site = not bool(site.ENABLE_USER_SITE) and user_site not in sys.path
        identity = _base_identity(root, no_user_site=actual_no_user_site)
        fingerprint = _fingerprint(identity)
        return cls(
            schema=ENVIRONMENT_SCHEMA,
            environment_id=fingerprint,
            project_root=str(root),
            interpreter_path=str(identity["interpreter_path"]),
            interpreter_version=str(identity["interpreter_version"]),
            interpreter_digest=str(identity["interpreter_digest"]),
            project_manifest_digest=str(identity["project_manifest_digest"]),
            dependency_source=str(identity["dependency_source"]),
            dependency_digest=str(identity["dependency_digest"]),
            os_identity=dict(identity["os_identity"]),
            no_user_site=actual_no_user_site,
            fingerprint=fingerprint,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "environment_id": self.environment_id,
            "project_root": self.project_root,
            "interpreter_path": self.interpreter_path,
            "interpreter_version": self.interpreter_version,
            "interpreter_digest": self.interpreter_digest,
            "project_manifest_digest": self.project_manifest_digest,
            "dependency_source": self.dependency_source,
            "dependency_digest": self.dependency_digest,
            "os_identity": dict(self.os_identity),
            "no_user_site": self.no_user_site,
            "fingerprint": self.fingerprint,
        }

    def child_environment(self) -> dict[str, str]:
        """Return a deliberately small child environment with no user site."""

        # Keep only process facts needed to reproduce the interpreter/platform
        # identity.  Windows ``platform.machine()`` consults the processor
        # environment; omitting it made the hermetic child report a different
        # machine fingerprint than its parent and fail closed spuriously.
        allowed = {
            "PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC",
            "PROCESSOR_ARCHITECTURE", "PROCESSOR_ARCHITEW6432", "PROCESSOR_IDENTIFIER",
            "PROCESSOR_LEVEL", "PROCESSOR_REVISION", "NUMBER_OF_PROCESSORS",
        }
        environment = {key: value for key, value in os.environ.items() if key in allowed}
        environment.update(
            {
                "PYTHONNOUSERSITE": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": self.project_root,
            }
        )
        return environment


__all__ = ["CheckerEnvironment", "CheckerEnvironmentError", "ENVIRONMENT_SCHEMA"]

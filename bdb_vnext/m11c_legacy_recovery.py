"""Config-only Legacy recovery repair used before revised side-by-side M9a.

The revised BDB Next cutover keeps the Legacy product independently recoverable.
If the canonical Legacy Native Host configuration is missing, this module may
restore *only that configuration file* from an already-existing local backup.

It deliberately has no registry, process, activation, install, start, stop, or
route mutation capability. It never synthesizes repository bindings. A backup
is eligible only when the existing Legacy NativeHostConfig parser can load it
completely, including all referenced BridgeConfig subjects.

When several byte-distinct valid backups exist, recovery remains fail-closed
unless exactly one byte identity is a monotonic semantic extension of every
other valid backup: global Native Host settings must match and every historical
repository alias must still point at the same resolved BridgeConfig path. The
selected backup may only add repository aliases.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from bdb_bridge.native_host import NativeHostConfig
from bdb_bridge.protocol import BridgeError
from bdb_shared.evidence import canonical_json_bytes


LEGACY_RECOVERY_SCHEMA = "bdb-vnext-m11c-legacy-config-recovery-v1"
_MAX_CONFIG_BYTES = 1024 * 1024
_BACKUP_PREFIX = "native-host.json"


class M11cLegacyRecoveryError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def _fail(code: str, message: str, *, details: dict[str, Any] | None = None) -> NoReturn:
    raise M11cLegacyRecoveryError(code, message, details=details)


def _absolute_root(value: str | Path) -> Path:
    root = Path(value).expanduser().absolute()
    if not root.is_absolute():
        _fail("invalid_legacy_root", "Legacy runtime root must be absolute")
    if root.is_symlink() or not root.is_dir():
        _fail("legacy_runtime_unavailable", "Legacy runtime root must remain an installed regular directory")
    return root


def _sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _read_bounded(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        _fail("legacy_config_invalid", f"Legacy config subject is not a regular file: {path.name}")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise M11cLegacyRecoveryError("legacy_config_unreadable", f"cannot stat {path.name}") from exc
    if size <= 0 or size > _MAX_CONFIG_BYTES:
        _fail("legacy_config_invalid", f"Legacy config subject has an invalid size: {path.name}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise M11cLegacyRecoveryError("legacy_config_unreadable", f"cannot read {path.name}") from exc


def _validate_native_config(path: Path) -> NativeHostConfig:
    try:
        return NativeHostConfig.from_json(path)
    except (BridgeError, OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        code = getattr(exc, "code", "legacy_native_config_invalid")
        raise M11cLegacyRecoveryError(str(code), f"Legacy Native config is not recoverable: {path.name}") from exc


def _candidate_paths(root: Path) -> tuple[Path, ...]:
    try:
        entries = tuple(root.iterdir())
    except OSError as exc:
        raise M11cLegacyRecoveryError("legacy_runtime_unreadable", "Legacy runtime root cannot be enumerated") from exc
    candidates = [
        path
        for path in entries
        if path.name != _BACKUP_PREFIX
        and path.name.startswith(_BACKUP_PREFIX)
        and path.name.endswith(".bak")
        and path.is_file()
        and not path.is_symlink()
    ]
    return tuple(sorted(candidates, key=lambda item: item.name.casefold()))


@dataclass(frozen=True)
class LegacyConfigSemantics:
    allowed_origins: tuple[str, ...]
    state_path: str
    session_store_path: str
    request_store_path: str
    max_wait_seconds: float
    max_message_bytes: int
    repository_bindings: tuple[tuple[str, str], ...]

    @classmethod
    def from_config(cls, config: NativeHostConfig) -> "LegacyConfigSemantics":
        return cls(
            allowed_origins=tuple(sorted(config.allowed_origins)),
            state_path=str(config.state_path),
            session_store_path=str(config.session_store_path),
            request_store_path=str(config.request_store_path),
            max_wait_seconds=float(config.max_wait_seconds),
            max_message_bytes=int(config.max_message_bytes),
            repository_bindings=tuple(
                sorted(
                    (alias, str(repository.bridge_config_path))
                    for alias, repository in config.repositories.items()
                )
            ),
        )

    def _global_identity(self) -> tuple[object, ...]:
        return (
            self.allowed_origins,
            self.state_path,
            self.session_store_path,
            self.request_store_path,
            self.max_wait_seconds,
            self.max_message_bytes,
        )

    def extends(self, other: "LegacyConfigSemantics") -> bool:
        """Return true only when self preserves other and may only add aliases."""

        if self._global_identity() != other._global_identity():
            return False
        candidate = dict(self.repository_bindings)
        historical = dict(other.repository_bindings)
        return all(candidate.get(alias) == path for alias, path in historical.items())

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed_origins": list(self.allowed_origins),
            "state_path": self.state_path,
            "session_store_path": self.session_store_path,
            "request_store_path": self.request_store_path,
            "max_wait_seconds": self.max_wait_seconds,
            "max_message_bytes": self.max_message_bytes,
            "repository_bindings": {
                alias: path for alias, path in self.repository_bindings
            },
        }


@dataclass(frozen=True)
class ValidBackup:
    path: Path
    sha256: str
    size_bytes: int
    repository_aliases: tuple[str, ...]
    semantics: LegacyConfigSemantics

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "repository_aliases": list(self.repository_aliases),
        }


def _valid_backup(path: Path) -> ValidBackup | None:
    try:
        payload = _read_bounded(path)
        parsed = _validate_native_config(path)
    except M11cLegacyRecoveryError:
        return None
    return ValidBackup(
        path=path,
        sha256=_sha256_bytes(payload),
        size_bytes=len(payload),
        repository_aliases=tuple(sorted(parsed.repositories)),
        semantics=LegacyConfigSemantics.from_config(parsed),
    )


def _identity_documents(valid: tuple[ValidBackup, ...]) -> list[dict[str, Any]]:
    identities: dict[str, list[ValidBackup]] = {}
    for item in valid:
        identities.setdefault(item.sha256, []).append(item)
    return [
        {
            "sha256": digest,
            "copies": [candidate.as_dict() for candidate in copies],
        }
        for digest, copies in sorted(identities.items())
    ]


def _dominant_identity(valid: tuple[ValidBackup, ...]) -> dict[str, Any] | None:
    """Find one byte identity that semantically extends every valid backup."""

    if not valid:
        return None
    dominant_digests = {
        candidate.sha256
        for candidate in valid
        if all(candidate.semantics.extends(other.semantics) for other in valid)
    }
    if len(dominant_digests) != 1:
        return None
    digest = next(iter(dominant_digests))
    copies = [candidate for candidate in valid if candidate.sha256 == digest]
    representative = copies[0]
    return {
        "sha256": digest,
        "copies": [candidate.as_dict() for candidate in copies],
        "repository_aliases": list(representative.repository_aliases),
        "selection_basis": "unique_monotonic_semantic_extension",
    }


def inspect_legacy_recovery(*, legacy_runtime_root: str | Path) -> dict[str, Any]:
    """Read-only inspection of the canonical config and eligible local backups."""

    root = _absolute_root(legacy_runtime_root)
    target = root / _BACKUP_PREFIX
    if target.exists():
        payload = _read_bounded(target)
        parsed = _validate_native_config(target)
        return {
            "schema": LEGACY_RECOVERY_SCHEMA,
            "status": "READY",
            "legacy_runtime_root": str(root),
            "target_path": str(target),
            "target_sha256": _sha256_bytes(payload),
            "repository_aliases": sorted(parsed.repositories),
            "valid_backup_identities": [],
            "dominant_backup_identity": None,
            "registry_mutation_performed": False,
            "process_mutation_performed": False,
            "production_activation_performed": False,
        }

    valid = tuple(item for path in _candidate_paths(root) if (item := _valid_backup(path)) is not None)
    return {
        "schema": LEGACY_RECOVERY_SCHEMA,
        "status": "RECOVERY_REQUIRED",
        "legacy_runtime_root": str(root),
        "target_path": str(target),
        "target_sha256": None,
        "repository_aliases": [],
        "valid_backup_identities": _identity_documents(valid),
        "dominant_backup_identity": _dominant_identity(valid),
        "registry_mutation_performed": False,
        "process_mutation_performed": False,
        "production_activation_performed": False,
    }


def _select_backup_identity(inspection: dict[str, Any]) -> tuple[dict[str, Any], str]:
    identities = inspection["valid_backup_identities"]
    if not identities:
        _fail(
            "legacy_native_config_backup_missing",
            "no valid Legacy native-host.json backup is available for config-only recovery",
        )
    if len(identities) == 1:
        return identities[0], "unique_valid_identity"

    dominant = inspection.get("dominant_backup_identity")
    if isinstance(dominant, dict):
        digest = dominant.get("sha256")
        matching = [identity for identity in identities if identity.get("sha256") == digest]
        if len(matching) == 1:
            return matching[0], "unique_monotonic_semantic_extension"

    _fail(
        "legacy_native_config_backup_ambiguous",
        "multiple valid Legacy configs exist without one unique monotonic semantic successor",
        details={
            "valid_backup_identities": identities,
            "dominant_backup_identity": dominant,
        },
    )


def restore_legacy_native_config(*, legacy_runtime_root: str | Path) -> dict[str, Any]:
    """Restore missing native-host.json from one proven validated backup identity."""

    root = _absolute_root(legacy_runtime_root)
    target = root / _BACKUP_PREFIX
    inspection = inspect_legacy_recovery(legacy_runtime_root=root)
    if inspection["status"] == "READY":
        return {**inspection, "status": "ALREADY_READY", "restored": False}

    identity, selection_basis = _select_backup_identity(inspection)
    copies = identity["copies"]
    source = Path(copies[0]["path"])
    payload = _read_bounded(source)
    if _sha256_bytes(payload) != identity["sha256"]:
        _fail("legacy_native_config_backup_changed", "selected Legacy config backup changed after inspection")
    _validate_native_config(source)

    # Re-observe the complete valid backup set before any write. A changing
    # history must never be hidden by the config-only recovery operation.
    confirmation = inspect_legacy_recovery(legacy_runtime_root=root)
    if confirmation["status"] == "READY":
        current = _read_bounded(target)
        if current == payload:
            parsed = _validate_native_config(target)
            return {
                "schema": LEGACY_RECOVERY_SCHEMA,
                "status": "ALREADY_READY",
                "legacy_runtime_root": str(root),
                "target_path": str(target),
                "target_sha256": _sha256_bytes(current),
                "repository_aliases": sorted(parsed.repositories),
                "source_backup_path": str(source),
                "source_backup_sha256": identity["sha256"],
                "selection_basis": selection_basis,
                "restored": False,
                "registry_mutation_performed": False,
                "process_mutation_performed": False,
                "production_activation_performed": False,
            }
        _fail("legacy_native_config_conflict", "Legacy native-host.json appeared with different bytes; refusing overwrite")
    if confirmation["valid_backup_identities"] != inspection["valid_backup_identities"]:
        _fail("legacy_native_config_backup_set_changed", "Legacy config backup set changed during recovery")
    confirmed_identity, confirmed_basis = _select_backup_identity(confirmation)
    if confirmed_identity["sha256"] != identity["sha256"] or confirmed_basis != selection_basis:
        _fail("legacy_native_config_backup_set_changed", "Legacy config recovery authority changed during re-observation")

    temporary = root / f".{_BACKUP_PREFIX}.recover-{os.getpid()}-{secrets.token_hex(4)}.tmp"
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if target.exists():
            _fail("legacy_native_config_conflict", "Legacy native-host.json appeared during recovery; refusing overwrite")
        os.replace(temporary, target)
    except M11cLegacyRecoveryError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise M11cLegacyRecoveryError("legacy_native_config_restore_failed", "Legacy native-host.json could not be restored atomically") from exc

    restored = _read_bounded(target)
    if restored != payload or _sha256_bytes(restored) != identity["sha256"]:
        _fail("legacy_native_config_restore_mismatch", "restored Legacy native-host.json differs from the validated backup")
    parsed = _validate_native_config(target)
    return {
        "schema": LEGACY_RECOVERY_SCHEMA,
        "status": "RECOVERED_CONFIG_ONLY",
        "legacy_runtime_root": str(root),
        "target_path": str(target),
        "target_sha256": identity["sha256"],
        "repository_aliases": sorted(parsed.repositories),
        "source_backup_path": str(source),
        "source_backup_sha256": identity["sha256"],
        "selection_basis": selection_basis,
        "restored": True,
        "registry_mutation_performed": False,
        "process_mutation_performed": False,
        "production_activation_performed": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bdb-vnext-legacy-recovery",
        description="Inspect or restore only the missing Legacy Native config; never modify registry/routes.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("status", "restore-config"):
        item = sub.add_parser(command)
        item.add_argument("--legacy-runtime-root", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "status":
            result = inspect_legacy_recovery(legacy_runtime_root=args.legacy_runtime_root)
        else:
            result = restore_legacy_native_config(legacy_runtime_root=args.legacy_runtime_root)
        exit_code = 0
    except M11cLegacyRecoveryError as exc:
        result = {
            "schema": LEGACY_RECOVERY_SCHEMA,
            "status": "BLOCKED",
            "error_code": exc.code,
            "error": str(exc),
            "details": exc.details,
            "registry_mutation_performed": False,
            "process_mutation_performed": False,
            "production_activation_performed": False,
        }
        exit_code = 2
    print(canonical_json_bytes(result).decode("utf-8"))
    return exit_code


__all__ = [
    "LEGACY_RECOVERY_SCHEMA",
    "M11cLegacyRecoveryError",
    "inspect_legacy_recovery",
    "restore_legacy_native_config",
]


if __name__ == "__main__":
    raise SystemExit(main())

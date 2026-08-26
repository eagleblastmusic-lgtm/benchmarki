"""Deterministic, bounded Machine Inventory collectors for NX-033.

The collector is deliberately an adapter around typed probe inputs.  It does
not install software, mutate PATH or the registry, invoke a shell, or change
workflow state.  Real probes are read-only; tests can inject fixture readers,
resolvers, hashers, and command runners without depending on a developer
machine's installed toolchain.
"""

from __future__ import annotations

import hashlib
import ntpath
import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from bdb_shared.evidence import canonical_json_bytes

from .machine_inventory_contract import (
    CANONICAL_PROBE_REGISTRY,
    Confidence,
    ExecutableIdentity,
    FactStatus,
    InventoryFact,
    InventorySource,
    MachineInventory,
    MachineInventoryContractError,
    PathIdentity,
    ProbeDefinition,
    ProbeRegistry,
    ProbeSourceKind,
    ProbeTimeoutClass,
    RedactionMetadata,
    VerificationDisposition,
    path_evidence,
    redact_evidence,
    require_valid_machine_inventory,
)


COLLECTOR_SCHEMA = "bdb-vnext-machine-inventory-collectors-v1"
COLLECTOR_VERSION = "v1"
COLLECTOR_VERSION_EXPLICIT = True
COLLECTOR_TIMEOUT_POLICY_VERSION = "bdb-vnext-machine-probe-timeouts-v1"
COLLECTOR_REDACTION_POLICY = "bdb-vnext-machine-redaction-v1"

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_VERSION = re.compile(r"(?<![0-9])v?([0-9]+(?:\.[0-9]+){1,3}(?:[-+][0-9A-Za-z.-]+)?)(?![0-9])")
_WINDOWS_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")
_TIMEOUT_SECONDS = {
    ProbeTimeoutClass.NONE: 0.0,
    ProbeTimeoutClass.SHORT: 2.0,
    ProbeTimeoutClass.MEDIUM: 5.0,
    ProbeTimeoutClass.LONG: 15.0,
}


class MachineInventoryCollectorError(RuntimeError):
    """A collector cannot produce a trustworthy bounded result."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CommandOutcome:
    """Small command result; stdout/stderr are never copied into evidence."""

    return_code: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    cancelled: bool = False

    def __post_init__(self) -> None:
        if self.return_code is not None and isinstance(self.return_code, bool):
            raise MachineInventoryCollectorError("return_code_invalid", "command return code must be an integer")
        if not isinstance(self.stdout, str) or not isinstance(self.stderr, str):
            raise MachineInventoryCollectorError("command_output_invalid", "command output must be text")
        if not isinstance(self.timed_out, bool) or not isinstance(self.cancelled, bool):
            raise MachineInventoryCollectorError("command_state_invalid", "command state flags must be boolean")


@dataclass(frozen=True)
class RegistryObservation:
    """Typed result supplied by a registry/optional-component reader."""

    status: FactStatus = FactStatus.AVAILABLE
    value: Mapping[str, Any] = field(default_factory=dict)
    resolved_path: str | None = None
    version: str | None = None
    digest: str | None = None
    verification: VerificationDisposition | None = None
    confidence: Confidence | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", FactStatus(self.status))
        if not isinstance(self.value, Mapping) or not isinstance(self.details, Mapping):
            raise MachineInventoryCollectorError("registry_observation_invalid", "registry observation objects are required")
        if self.verification is not None:
            object.__setattr__(self, "verification", VerificationDisposition(self.verification))
        if self.confidence is not None:
            object.__setattr__(self, "confidence", Confidence(self.confidence))


CommandRunner = Callable[[tuple[str, ...], Mapping[str, str], float], CommandOutcome]
ExecutableResolver = Callable[[str, Sequence[str], Mapping[str, str]], Sequence[str] | str | None]
FileHasher = Callable[[str], str]
RegistryReader = Callable[[str], RegistryObservation | Mapping[str, Any] | None]
OSReader = Callable[[], Mapping[str, Mapping[str, Any]]]
CancelCheck = Callable[[], bool]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _timeout_seconds(timeout_class: ProbeTimeoutClass) -> float:
    timeout = _TIMEOUT_SECONDS[ProbeTimeoutClass(timeout_class)]
    if timeout <= 0:
        raise MachineInventoryCollectorError("command_timeout_unbounded", "command probes require a positive timeout")
    return timeout


def probe_timeout_seconds(timeout_class: ProbeTimeoutClass) -> float:
    """Return the bounded timeout selected by a typed probe definition."""

    return _timeout_seconds(timeout_class)


def parse_reported_version(output: str) -> str | None:
    """Extract a version token without preserving arbitrary command output."""

    if not isinstance(output, str):
        return None
    match = _VERSION.search(output)
    return match.group(1) if match else None


def sha256_file(path: str | Path) -> str:
    """Hash one exact file; callers decide whether an unavailable hash is fatal."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _split_path(raw_path: str | None) -> tuple[str, ...]:
    if not isinstance(raw_path, str) or not raw_path:
        return ()
    separator = ";" if ";" in raw_path else os.pathsep
    return tuple(item for item in raw_path.split(separator) if item.strip())


def _controlled_environment(
    environment: Mapping[str, str] | None,
    path_entries: Sequence[str] | None,
) -> tuple[dict[str, str], tuple[str, ...]]:
    source = os.environ if environment is None else environment
    if not isinstance(source, Mapping):
        raise MachineInventoryCollectorError("environment_invalid", "collector environment must be a mapping")
    controlled: dict[str, str] = {}
    for key, value in source.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise MachineInventoryCollectorError("environment_invalid", "collector environment must contain text pairs")
        controlled[key] = value
    entries = tuple(path_entries) if path_entries is not None else _split_path(controlled.get("PATH"))
    if any(not isinstance(item, str) or not item.strip() for item in entries):
        raise MachineInventoryCollectorError("path_entries_invalid", "controlled PATH entries must be non-empty text")
    if path_entries is not None:
        controlled["PATH"] = os.pathsep.join(entries)
    return controlled, entries


def _path_identity(entries: Sequence[str]) -> tuple[PathIdentity, FactStatus, VerificationDisposition, Confidence, dict[str, Any]]:
    if not entries:
        identity = PathIdentity.from_entries(())
        return identity, FactStatus.MISSING, VerificationDisposition.UNVERIFIED, Confidence.LOW, {"reason": "path_missing"}
    try:
        identity = PathIdentity.from_entries(entries)
    except MachineInventoryContractError:
        identity = PathIdentity.from_entries(())
        return identity, FactStatus.UNVERIFIABLE, VerificationDisposition.UNVERIFIED, Confidence.LOW, {
            "reason": "path_canonicalization_failed"
        }
    return identity, FactStatus.AVAILABLE, VerificationDisposition.VERIFIED, Confidence.HIGH, {
        "duplicate_count": len(identity.duplicate_entries),
        "path_digest_recorded": True,
    }


def _default_os_reader() -> Mapping[str, Mapping[str, Any]]:
    try:
        system = platform.system()
        release = platform.release()
        build = platform.version()
        architecture = platform.machine()
    except Exception:
        return {}
    return {
        "os_identity": {"family": system, "release": release},
        "windows_build": {"build": build} if system.casefold() == "windows" and build else {},
        "architecture": {"architecture": architecture},
    }


def _default_resolver(command: str, path_entries: Sequence[str], environment: Mapping[str, str]) -> Sequence[str] | None:
    del path_entries
    return (shutil.which(command, path=environment.get("PATH")),)


def _default_command_runner(
    argv: tuple[str, ...], environment: Mapping[str, str], timeout_seconds: float
) -> CommandOutcome:
    try:
        completed = subprocess.run(
            list(argv),
            shell=False,
            capture_output=True,
            text=True,
            errors="replace",
            env=dict(environment),
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return CommandOutcome(return_code=None, timed_out=True)
    return CommandOutcome(
        return_code=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


def _default_registry_reader(fact_class: str) -> RegistryObservation | None:
    """Read-only optional-component hook; platform-specific registry work is deferred safely."""

    if platform.system().casefold() != "windows":
        return None
    del fact_class
    return None


def _normalize_candidates(candidates: Sequence[str] | str | None) -> tuple[str, ...]:
    if candidates is None:
        return ()
    raw = (candidates,) if isinstance(candidates, str) else tuple(candidates)
    normalized: dict[str, str] = {}
    for candidate in raw:
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        value = ntpath.normpath(candidate.strip())
        if not _WINDOWS_ABSOLUTE.match(value):
            continue
        normalized.setdefault(value.casefold(), value)
    return tuple(normalized[key] for key in sorted(normalized))


def _default_verification(status: FactStatus) -> VerificationDisposition:
    if status is FactStatus.AVAILABLE:
        return VerificationDisposition.VERIFIED
    if status is FactStatus.MALFORMED:
        return VerificationDisposition.REJECTED
    return VerificationDisposition.UNVERIFIED


def _default_confidence(status: FactStatus) -> Confidence:
    if status is FactStatus.AVAILABLE:
        return Confidence.HIGH
    if status is FactStatus.UNVERIFIABLE:
        return Confidence.NONE
    return Confidence.LOW


def _make_fact(
    *,
    fact_class: str,
    subject: str,
    status: FactStatus,
    source: InventorySource,
    collected_at: str,
    probe_id: str,
    value: Mapping[str, Any] | None = None,
    resolved_path: str | None = None,
    version: str | None = None,
    digest: str | None = None,
    details: Mapping[str, Any] | None = None,
    verification: VerificationDisposition | None = None,
    confidence: Confidence | None = None,
) -> InventoryFact:
    status = FactStatus(status)
    source = InventorySource(source)
    verification = verification or _default_verification(status)
    confidence = confidence or _default_confidence(status)
    clean_value = redact_evidence(dict(value)) if value is not None else None
    clean_details = redact_evidence(dict(details)) if details is not None else None
    executable: ExecutableIdentity | None = None
    if fact_class.startswith("tool.") and resolved_path is not None:
        try:
            executable = ExecutableIdentity(
                resolved_path=resolved_path,
                content_digest=digest,
                reported_version=version,
                source=source,
                probe_id=probe_id,
                status=status,
                verification=verification,
            )
        except MachineInventoryContractError:
            status = FactStatus.UNVERIFIABLE if status is FactStatus.AVAILABLE else status
            verification = _default_verification(status)
            confidence = _default_confidence(status)
            resolved_path = None
            digest = None
            executable = None
            clean_details = {
                **(clean_details or {}),
                "reason": "exact_executable_identity_unavailable",
            }
    return InventoryFact(
        fact_class=fact_class,
        subject=subject,
        status=status,
        source=source,
        collected_at=collected_at,
        confidence=confidence,
        verification=verification,
        probe_id=probe_id,
        value=clean_value,
        resolved_path=resolved_path,
        version=version,
        digest=digest,
        executable=executable,
        details=clean_details,
    )


def _core_fact(
    fact_class: str,
    collected_at: str,
    observation: Mapping[str, Any] | None,
) -> InventoryFact:
    value = dict(observation or {})
    available = bool(value) and all(isinstance(key, str) for key in value)
    status = FactStatus.AVAILABLE if available else FactStatus.UNVERIFIABLE
    details = {} if available else {"reason": "os_api_unavailable"}
    version = str(value.get("build")) if fact_class == "windows_build" and value.get("build") else None
    return _make_fact(
        fact_class=fact_class,
        subject=fact_class,
        status=status,
        source=InventorySource.OS_API,
        collected_at=collected_at,
        probe_id=f"collector:{fact_class}",
        value=value or None,
        version=version,
        details=details,
    )


def _path_fact(
    identity: PathIdentity,
    status: FactStatus,
    verification: VerificationDisposition,
    confidence: Confidence,
    collected_at: str,
    details: Mapping[str, Any],
) -> InventoryFact:
    return _make_fact(
        fact_class="path_identity",
        subject="PATH",
        status=status,
        source=InventorySource.ENVIRONMENT,
        collected_at=collected_at,
        probe_id="collector:path",
        value=path_evidence(identity),
        digest=identity.digest,
        details=details,
        verification=verification,
        confidence=confidence,
    )


def _command_fact(
    definition: ProbeDefinition,
    *,
    collected_at: str,
    environment: Mapping[str, str],
    path_entries: Sequence[str],
    resolver: ExecutableResolver,
    runner: CommandRunner,
    hasher: FileHasher,
) -> InventoryFact:
    timeout = _timeout_seconds(definition.timeout_class)
    candidates = _normalize_candidates(resolver(definition.argv[0], path_entries, environment))
    common_details: dict[str, Any] = {
        "argv": list(definition.argv),
        "candidate_count": len(candidates),
        "candidate_paths": list(candidates),
        "timeout_seconds": timeout,
    }
    if not candidates:
        return _make_fact(
            fact_class=definition.fact_class,
            subject=definition.fact_class.removeprefix("tool."),
            status=FactStatus.MISSING,
            source=InventorySource.COMMAND,
            collected_at=collected_at,
            probe_id=definition.probe_id,
            details={**common_details, "reason": "executable_not_found"},
        )
    resolved_path = candidates[0]
    argv = (resolved_path, *definition.argv[1:])
    try:
        outcome = runner(argv, environment, timeout)
    except TimeoutError:
        outcome = CommandOutcome(return_code=None, timed_out=True)
    except Exception:
        return _make_fact(
            fact_class=definition.fact_class,
            subject=definition.fact_class.removeprefix("tool."),
            status=FactStatus.ERROR,
            source=InventorySource.COMMAND,
            collected_at=collected_at,
            probe_id=definition.probe_id,
            resolved_path=resolved_path,
            details={**common_details, "reason": "command_runner_failed"},
        )
    if outcome.cancelled:
        status = FactStatus.ERROR
        reason = "probe_cancelled"
    elif outcome.timed_out:
        status = FactStatus.TIMEOUT
        reason = "probe_timeout"
    elif outcome.return_code != 0:
        status = FactStatus.ERROR
        reason = "command_nonzero"
    else:
        status = FactStatus.AVAILABLE
        reason = "command_verified"
    details = {
        **common_details,
        "reason": reason,
        "return_code": outcome.return_code,
        "stderr_present": bool(outcome.stderr),
    }
    if status is not FactStatus.AVAILABLE:
        return _make_fact(
            fact_class=definition.fact_class,
            subject=definition.fact_class.removeprefix("tool."),
            status=status,
            source=InventorySource.COMMAND,
            collected_at=collected_at,
            probe_id=definition.probe_id,
            resolved_path=resolved_path,
            details=details,
        )
    output = outcome.stdout or outcome.stderr
    version = parse_reported_version(output)
    if version is None:
        return _make_fact(
            fact_class=definition.fact_class,
            subject=definition.fact_class.removeprefix("tool."),
            status=FactStatus.MALFORMED,
            source=InventorySource.COMMAND,
            collected_at=collected_at,
            probe_id=definition.probe_id,
            resolved_path=resolved_path,
            details={**details, "reason": "version_parse_failed"},
        )
    try:
        digest = hasher(resolved_path)
    except Exception:
        digest = None
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        return _make_fact(
            fact_class=definition.fact_class,
            subject=definition.fact_class.removeprefix("tool."),
            status=FactStatus.UNVERIFIABLE,
            source=InventorySource.COMMAND,
            collected_at=collected_at,
            probe_id=definition.probe_id,
            resolved_path=resolved_path,
            version=version,
            details={**details, "reason": "content_digest_unavailable"},
        )
    return _make_fact(
        fact_class=definition.fact_class,
        subject=definition.fact_class.removeprefix("tool."),
        status=FactStatus.AVAILABLE,
        source=InventorySource.COMMAND,
        collected_at=collected_at,
        probe_id=definition.probe_id,
        resolved_path=resolved_path,
        version=version,
        digest=digest,
        details=details,
    )


def _registry_observation(value: RegistryObservation | Mapping[str, Any] | None) -> RegistryObservation | None:
    if value is None:
        return None
    if isinstance(value, RegistryObservation):
        return value
    if not isinstance(value, Mapping):
        raise MachineInventoryCollectorError("registry_observation_invalid", "registry reader returned a non-object")
    raw = dict(value)
    status = FactStatus(raw.pop("status", FactStatus.AVAILABLE))
    resolved_path = raw.pop("resolved_path", None)
    version = raw.pop("version", None)
    digest = raw.pop("digest", None)
    details = raw.pop("details", {})
    verification = raw.pop("verification", None)
    confidence = raw.pop("confidence", None)
    return RegistryObservation(
        status=status,
        value=raw,
        resolved_path=resolved_path,
        version=version,
        digest=digest,
        verification=verification,
        confidence=confidence,
        details=details if isinstance(details, Mapping) else {},
    )


def _registry_fact(
    definition: ProbeDefinition,
    *,
    collected_at: str,
    reader: RegistryReader,
    hasher: FileHasher,
) -> InventoryFact:
    try:
        observation = _registry_observation(reader(definition.fact_class))
    except PermissionError:
        observation = RegistryObservation(status=FactStatus.UNVERIFIABLE, details={"reason": "registry_access_denied"})
    except Exception:
        observation = RegistryObservation(status=FactStatus.UNVERIFIABLE, details={"reason": "registry_probe_failed"})
    subject = definition.fact_class.removeprefix("tool.")
    if observation is None:
        return _make_fact(
            fact_class=definition.fact_class,
            subject=subject,
            status=FactStatus.MISSING,
            source=InventorySource.REGISTRY,
            collected_at=collected_at,
            probe_id=definition.probe_id,
            details={"reason": "optional_component_not_found"},
        )
    digest = observation.digest
    if observation.status is FactStatus.AVAILABLE and observation.resolved_path and digest is None:
        try:
            digest = hasher(observation.resolved_path)
        except Exception:
            digest = None
    status = observation.status
    if status is FactStatus.AVAILABLE and observation.resolved_path and not isinstance(digest, str):
        status = FactStatus.UNVERIFIABLE
    return _make_fact(
        fact_class=definition.fact_class,
        subject=subject,
        status=status,
        source=InventorySource.REGISTRY,
        collected_at=collected_at,
        probe_id=definition.probe_id,
        value=observation.value or None,
        resolved_path=observation.resolved_path,
        version=observation.version,
        digest=digest,
        details=observation.details or None,
        verification=observation.verification,
        confidence=observation.confidence,
    )


def collect_filesystem_identity(
    *,
    fact_class: str,
    probe_id: str,
    path: str,
    collected_at: str,
    version: str | None = None,
    hasher: FileHasher = sha256_file,
) -> InventoryFact:
    """Collect one exact filesystem identity without invoking a process."""

    if not isinstance(path, str) or not _WINDOWS_ABSOLUTE.match(path):
        return _make_fact(
            fact_class=fact_class,
            subject=fact_class.removeprefix("tool."),
            status=FactStatus.MALFORMED,
            source=InventorySource.FILESYSTEM,
            collected_at=collected_at,
            probe_id=probe_id,
            details={"reason": "exact_path_required"},
        )
    try:
        digest = hasher(path)
    except Exception:
        return _make_fact(
            fact_class=fact_class,
            subject=fact_class.removeprefix("tool."),
            status=FactStatus.UNVERIFIABLE,
            source=InventorySource.FILESYSTEM,
            collected_at=collected_at,
            probe_id=probe_id,
            resolved_path=path,
            version=version,
            details={"reason": "content_digest_unavailable"},
        )
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        status = FactStatus.UNVERIFIABLE
    else:
        status = FactStatus.AVAILABLE
    return _make_fact(
        fact_class=fact_class,
        subject=fact_class.removeprefix("tool."),
        status=status,
        source=InventorySource.FILESYSTEM,
        collected_at=collected_at,
        probe_id=probe_id,
        resolved_path=path,
        version=version,
        digest=digest if status is FactStatus.AVAILABLE else None,
        details={"reason": "filesystem_identity_verified" if status is FactStatus.AVAILABLE else "content_digest_invalid"},
    )


def _unverified_probe_fact(definition: ProbeDefinition, collected_at: str, reason: str) -> InventoryFact:
    return _make_fact(
        fact_class=definition.fact_class,
        subject=definition.fact_class.removeprefix("tool."),
        status=FactStatus.ERROR,
        source=InventorySource(definition.source_kind.value),
        collected_at=collected_at,
        probe_id=definition.probe_id,
        details={"reason": reason},
    )


def collect_machine_inventory(
    *,
    inventory_id: str = "machine:local",
    collected_at: str | None = None,
    environment: Mapping[str, str] | None = None,
    path_entries: Sequence[str] | None = None,
    probe_registry: ProbeRegistry = CANONICAL_PROBE_REGISTRY,
    command_runner: CommandRunner = _default_command_runner,
    executable_resolver: ExecutableResolver = _default_resolver,
    file_hasher: FileHasher = sha256_file,
    registry_reader: RegistryReader = _default_registry_reader,
    os_reader: OSReader = _default_os_reader,
    cancel_check: CancelCheck | None = None,
) -> MachineInventory:
    """Collect the complete required inventory while preserving per-probe outcomes."""

    if not isinstance(probe_registry, ProbeRegistry):
        raise MachineInventoryCollectorError("probe_registry_invalid", "collector needs a typed probe registry")
    missing = probe_registry.missing_required_fact_classes
    if missing:
        raise MachineInventoryCollectorError("probe_registry_incomplete", "probe registry is missing required fact classes")
    controlled, raw_path_entries = _controlled_environment(environment, path_entries)
    timestamp = collected_at or _utc_now()
    path, path_status, path_verification, path_confidence, path_details = _path_identity(raw_path_entries)
    facts: list[InventoryFact] = [_path_fact(path, path_status, path_verification, path_confidence, timestamp, path_details)]
    try:
        observations = os_reader()
    except Exception:
        observations = {}
    if not isinstance(observations, Mapping):
        observations = {}
    for fact_class in ("os_identity", "windows_build", "architecture"):
        raw_observation = observations.get(fact_class)
        facts.append(
            _core_fact(
                fact_class,
                timestamp,
                raw_observation if isinstance(raw_observation, Mapping) else None,
            )
        )
    is_cancelled = cancel_check or (lambda: False)
    for definition in probe_registry.definitions:
        if definition.fact_class in {"os_identity", "windows_build", "architecture", "path_identity"}:
            continue
        if is_cancelled():
            facts.append(_unverified_probe_fact(definition, timestamp, "probe_cancelled"))
            continue
        try:
            if definition.source_kind is ProbeSourceKind.COMMAND:
                fact = _command_fact(
                    definition,
                    collected_at=timestamp,
                    environment=controlled,
                    path_entries=raw_path_entries,
                    resolver=executable_resolver,
                    runner=command_runner,
                    hasher=file_hasher,
                )
            elif definition.source_kind is ProbeSourceKind.REGISTRY:
                fact = _registry_fact(
                    definition,
                    collected_at=timestamp,
                    reader=registry_reader,
                    hasher=file_hasher,
                )
            elif definition.source_kind is ProbeSourceKind.FILESYSTEM:
                fact = _unverified_probe_fact(definition, timestamp, "filesystem_probe_requires_explicit_path")
            else:
                fact = _unverified_probe_fact(definition, timestamp, "unsupported_collector_source")
        except Exception:
            fact = _unverified_probe_fact(definition, timestamp, "probe_failure")
        facts.append(fact)
    inventory = MachineInventory(
        schema="bdb-vnext-machine-inventory-v1",
        version="v1",
        inventory_id=inventory_id,
        collected_at=timestamp,
        path_identity=path,
        facts=tuple(facts),
        redaction=RedactionMetadata(),
    )
    try:
        return require_valid_machine_inventory(inventory)
    except Exception as exc:
        raise MachineInventoryCollectorError("inventory_invalid", "collector produced invalid inventory") from exc


def collect_inventory(**kwargs: Any) -> MachineInventory:
    """Compatibility alias for callers that use the shorter collector name."""

    return collect_machine_inventory(**kwargs)


def collector_evidence(value: Any) -> Any:
    """Apply the NX-032 redaction policy to collector diagnostics."""

    return redact_evidence(value)


def collector_canonical_bytes(inventory: MachineInventory, *, normalize_time: bool = True) -> bytes:
    """Return canonical collector output for golden fixture comparisons."""

    if not isinstance(inventory, MachineInventory):
        raise MachineInventoryCollectorError("inventory_required", "canonical output needs a typed inventory")
    return canonical_json_bytes(inventory.to_dict(normalize_time=normalize_time))


__all__ = [
    "COLLECTOR_REDACTION_POLICY",
    "COLLECTOR_SCHEMA",
    "COLLECTOR_TIMEOUT_POLICY_VERSION",
    "COLLECTOR_VERSION",
    "COLLECTOR_VERSION_EXPLICIT",
    "CommandOutcome",
    "MachineInventoryCollectorError",
    "RegistryObservation",
    "collector_canonical_bytes",
    "collector_evidence",
    "collect_filesystem_identity",
    "collect_inventory",
    "collect_machine_inventory",
    "parse_reported_version",
    "probe_timeout_seconds",
    "sha256_file",
]

"""Fail-closed local M9a legacy ingress freeze and archive candidate creation.

This module is the effectful *local* half of M9a.  It never activates vNext.
The operation is intentionally roll-forward-only: after the first ingress-closing
mutation, failures are reported as a partial freeze and no automatic re-enable is
attempted.

The executor is Windows-only because it must acquire the exact byte-range locks
used by the legacy service and verify/remove the exact Native Messaging registry
bindings installed by the legacy installer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from bdb_shared.evidence import semantic_digest
from bdb_vnext.m9a_blocker_probe import _wake_event_exists
from bdb_vnext.m9a_blocker_probe_compat import probe_profile


FREEZE_SCHEMA = "bdb-vnext-m9a-freeze-report-v1"
ARCHIVE_SCHEMA = "bdb-vnext-m9a-archive-manifest-v1"
PROFILE_SPEC_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
HOST_NAME = "com.bartosz.dev_bridge"
_ALLOWED_CAPABILITIES = frozenset(
    {
        "ACKNOWLEDGEMENT_ONLY_ON_SERVICE_RESTART",
        "MANUAL_ONLY",
    }
)
_ALLOWED_SPOOL_CLASSES = frozenset(
    {
        "CORRELATED_WITH_UNRESOLVED_COMMAND",
        "NEW_INGRESS_IF_SERVICE_RESTARTS",
        "JOURNAL_TERMINAL_IDEMPOTENT_INPUT",
    }
)
_ALLOWED_RECEIPT_STATUSES = frozenset(
    {
        "VALID",
        "VALID_LEGACY_COMPAT_SHAPE",
        "MISSING",
    }
)
_ALLOWED_PROMOTER_STATUSES = frozenset(
    {
        "VALID",
        "VALID_LEGACY_COMPAT",
        "MISSING",
    }
)


class M9aFreezeError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProfileSpec:
    profile_id: str
    bridge_config_path: Path
    expected_probe_digest: str

    def __post_init__(self) -> None:
        if PROFILE_SPEC_RE.fullmatch(self.profile_id) is None:
            raise ValueError("profile_id is invalid")
        if SHA256_RE.fullmatch(self.expected_probe_digest) is None:
            raise ValueError("expected_probe_digest must be sha256")


@dataclass
class _HeldWindowsByteLock:
    path: Path
    handle: Any = None
    acquired: bool = False
    created_for_freeze: bool = False

    def acquire(self) -> None:
        if sys.platform != "win32":
            raise M9aFreezeError("M9a freeze requires Windows byte-range locking")
        import errno
        import msvcrt

        self.path = self.path.expanduser().resolve(strict=False)
        if not self.path.parent.is_dir():
            raise M9aFreezeError(f"runtime directory missing for lock: {self.path.parent}")
        existed = self.path.exists()
        if existed and (self.path.is_symlink() or not self.path.is_file()):
            raise M9aFreezeError(f"legacy lock path is not a regular file: {self.path}")
        self.handle = open(self.path, "a+b")
        self.created_for_freeze = not existed
        if self.handle.tell() == 0:
            self.handle.write(b"\x00")
            self.handle.flush()
        self.handle.seek(0)
        try:
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise M9aFreezeError(f"legacy instance lock is held: {self.path}") from exc
            raise M9aFreezeError(f"legacy instance lock failed: {self.path}: {exc}") from exc
        self.acquired = True

    def release(self) -> None:
        if not self.acquired or self.handle is None:
            return
        import msvcrt

        try:
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self.acquired = False
            self.handle.close()
            self.handle = None


def _utc_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _normalized(path: Path) -> str:
    return os.path.normcase(str(path.expanduser().resolve(strict=False)))


def _read_json_object(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise M9aFreezeError(f"expected regular JSON file: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise M9aFreezeError(f"unreadable JSON file: {path}") from exc
    if not isinstance(raw, dict):
        raise M9aFreezeError(f"JSON root is not an object: {path}")
    return raw


def _effective_paths(config_path: Path) -> dict[str, Path]:
    raw = _read_json_object(config_path)
    runtime_raw = raw.get("runtime_dir")
    if not isinstance(runtime_raw, str) or not runtime_raw:
        raise M9aFreezeError(f"bridge config has no runtime_dir: {config_path}")
    runtime = Path(runtime_raw).expanduser().resolve(strict=False)
    journal_raw = raw.get("journal_path")
    spool_raw = raw.get("direct_spool_dir")
    return {
        "runtime": runtime,
        "journal": (
            Path(journal_raw).expanduser().resolve(strict=False)
            if isinstance(journal_raw, str) and journal_raw
            else runtime / "journal.db"
        ),
        "spool": (
            Path(spool_raw).expanduser().resolve(strict=False)
            if isinstance(spool_raw, str) and spool_raw
            else runtime / "direct_spool" / "inbox"
        ),
    }


def _validate_probe(profile: ProfileSpec, probe: Mapping[str, Any]) -> None:
    actual_digest = probe.get("probe_digest")
    if actual_digest != profile.expected_probe_digest:
        raise M9aFreezeError(
            f"fresh probe digest changed for {profile.profile_id}: "
            f"expected {profile.expected_probe_digest}, observed {actual_digest}"
        )
    if probe.get("wake_event_present") is not False:
        raise M9aFreezeError(f"legacy wake event exists for {profile.profile_id}")
    native = probe.get("native")
    if not isinstance(native, Mapping):
        raise M9aFreezeError(f"native observation missing for {profile.profile_id}")
    if native.get("profile_bound") is not True:
        raise M9aFreezeError(f"native profile binding changed for {profile.profile_id}")
    arm = native.get("arm")
    if not isinstance(arm, Mapping) or arm.get("effective_armed") is not False:
        raise M9aFreezeError(f"native host is armed for {profile.profile_id}")

    journal = probe.get("journal")
    if not isinstance(journal, Mapping):
        raise M9aFreezeError(f"journal observation missing for {profile.profile_id}")
    candidates = journal.get("service_candidates")
    if not isinstance(candidates, Mapping):
        raise M9aFreezeError(f"service candidate observation missing for {profile.profile_id}")
    if candidates.get("truncated") is True:
        raise M9aFreezeError(f"service candidates truncated for {profile.profile_id}")
    items = candidates.get("items")
    if not isinstance(items, list):
        raise M9aFreezeError(f"service candidate list invalid for {profile.profile_id}")
    if any(isinstance(item, Mapping) and item.get("pid_alive") is True for item in items):
        raise M9aFreezeError(f"live legacy service PID exists for {profile.profile_id}")
    if journal.get("unresolved_truncated") is True:
        raise M9aFreezeError(f"unresolved legacy set truncated for {profile.profile_id}")
    capabilities = journal.get("capability_counts")
    if not isinstance(capabilities, Mapping):
        raise M9aFreezeError(f"capability counts missing for {profile.profile_id}")
    dangerous = sorted(str(key) for key, value in capabilities.items() if value and key not in _ALLOWED_CAPABILITIES)
    if dangerous:
        raise M9aFreezeError(
            f"write-capable or unknown legacy recovery exists for {profile.profile_id}: {dangerous}"
        )

    spool = probe.get("spool")
    if not isinstance(spool, Mapping) or spool.get("truncated") is True:
        raise M9aFreezeError(f"spool observation incomplete for {profile.profile_id}")
    classes = spool.get("classification_counts")
    if not isinstance(classes, Mapping):
        raise M9aFreezeError(f"spool classifications missing for {profile.profile_id}")
    unsafe_classes = sorted(str(key) for key, value in classes.items() if value and key not in _ALLOWED_SPOOL_CLASSES)
    if unsafe_classes:
        raise M9aFreezeError(f"unsafe spool classes for {profile.profile_id}: {unsafe_classes}")

    receipts = probe.get("receipts")
    if not isinstance(receipts, Mapping) or receipts.get("status") not in _ALLOWED_RECEIPT_STATUSES:
        raise M9aFreezeError(f"receipt store not archive-safe for {profile.profile_id}")
    if receipts.get("issues") not in (None, []):
        raise M9aFreezeError(f"receipt store has issues for {profile.profile_id}")

    promoter = probe.get("promoter")
    if not isinstance(promoter, Mapping) or promoter.get("status") not in _ALLOWED_PROMOTER_STATUSES:
        raise M9aFreezeError(f"promoter state not archive-safe for {profile.profile_id}")
    if promoter.get("issues") not in (None, []):
        raise M9aFreezeError(f"promoter has issues for {profile.profile_id}")
    if promoter.get("truncated") is True:
        raise M9aFreezeError(f"promoter observation truncated for {profile.profile_id}")

    if probe.get("vnext_activation_allowed") is not False or probe.get("m9b_allowed") is not False:
        raise M9aFreezeError("probe unexpectedly granted activation authority")


def _iter_regular_files(root: Path, *, max_files: int = 20_000) -> list[Path]:
    if not root.exists():
        return []
    if root.is_symlink() or not root.is_dir():
        raise M9aFreezeError(f"archive source root is not a regular directory: {root}")
    result: list[Path] = []
    for current, dirs, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in dirs:
            path = current_path / name
            if path.is_symlink():
                raise M9aFreezeError(f"symlinked directory in archive source: {path}")
        for name in files:
            path = current_path / name
            if path.is_symlink() or not path.is_file():
                raise M9aFreezeError(f"non-regular file in archive source: {path}")
            result.append(path)
            if len(result) > max_files:
                raise M9aFreezeError(f"archive source exceeds {max_files} files: {root}")
    return sorted(result, key=lambda item: item.as_posix().casefold())


def _copy_verified_file(
    source: Path,
    destination: Path,
    *,
    total_state: list[int],
    max_file_bytes: int = 256 * 1024 * 1024,
    max_total_bytes: int = 2 * 1024 * 1024 * 1024,
) -> dict[str, Any]:
    before = source.stat()
    if before.st_size > max_file_bytes:
        raise M9aFreezeError(f"archive file exceeds per-file bound: {source}")
    data = source.read_bytes()
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise M9aFreezeError(f"archive source changed during read: {source}")
    if len(data) != after.st_size:
        raise M9aFreezeError(f"archive source size mismatch: {source}")
    total_state[0] += len(data)
    if total_state[0] > max_total_bytes:
        raise M9aFreezeError("archive candidate exceeds total byte bound")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    source_digest = _sha256_bytes(data)
    copied = destination.read_bytes()
    copied_digest = _sha256_bytes(copied)
    if copied_digest != source_digest:
        raise M9aFreezeError(f"archive copy verification failed: {source}")
    return {
        "source_path": str(source),
        "archive_path": str(destination),
        "size": len(data),
        "sha256": source_digest,
    }


def _archive_tree(
    source_root: Path,
    archive_root: Path,
    label: str,
    *,
    total_state: list[int],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for source in _iter_regular_files(source_root):
        relative = source.relative_to(source_root)
        destination = archive_root / "sources" / label / relative
        entries.append(_copy_verified_file(source, destination, total_state=total_state))
    return entries


def _archive_single(
    source: Path,
    archive_root: Path,
    label: str,
    *,
    total_state: list[int],
    required: bool = True,
) -> list[dict[str, Any]]:
    if not source.exists():
        if required:
            raise M9aFreezeError(f"required archive source missing: {source}")
        return []
    if source.is_symlink() or not source.is_file():
        raise M9aFreezeError(f"archive source is not a regular file: {source}")
    destination = archive_root / "sources" / label / source.name
    return [_copy_verified_file(source, destination, total_state=total_state)]


def _native_registry_suffixes() -> tuple[tuple[str, str], ...]:
    return (
        ("chrome", r"Software\Google\Chrome\NativeMessagingHosts" + "\\" + HOST_NAME),
        ("edge", r"Software\Microsoft\Edge\NativeMessagingHosts" + "\\" + HOST_NAME),
    )


def _registry_state(expected_manifest: Path) -> dict[str, Any]:
    if sys.platform != "win32":
        raise M9aFreezeError("Native Messaging registry inspection requires Windows")
    import winreg

    result: dict[str, Any] = {"hkcu": {}, "hklm": {}}
    for root_name, root in (("hkcu", winreg.HKEY_CURRENT_USER), ("hklm", winreg.HKEY_LOCAL_MACHINE)):
        for browser, suffix in _native_registry_suffixes():
            try:
                with winreg.OpenKey(root, suffix, 0, winreg.KEY_READ) as key:
                    value, _ = winreg.QueryValueEx(key, None)
            except FileNotFoundError:
                result[root_name][browser] = {"present": False}
                continue
            except OSError as exc:
                raise M9aFreezeError(f"cannot read Native Messaging registry key {root_name}/{browser}: {exc}") from exc
            path = Path(str(value)).expanduser().resolve(strict=False)
            matches = _normalized(path) == _normalized(expected_manifest)
            result[root_name][browser] = {
                "present": True,
                "manifest_path": str(path),
                "matches_expected_legacy_manifest": matches,
            }
    return result


def _validate_registry_before_freeze(state: Mapping[str, Any]) -> None:
    hklm = state.get("hklm")
    if isinstance(hklm, Mapping):
        unexpected_hklm = [name for name, item in hklm.items() if isinstance(item, Mapping) and item.get("present") is True]
        if unexpected_hklm:
            raise M9aFreezeError(f"system-level Native Messaging binding exists: {sorted(unexpected_hklm)}")
    hkcu = state.get("hkcu")
    if not isinstance(hkcu, Mapping):
        raise M9aFreezeError("HKCU Native Messaging observation missing")
    mismatched = [
        name
        for name, item in hkcu.items()
        if isinstance(item, Mapping)
        and item.get("present") is True
        and item.get("matches_expected_legacy_manifest") is not True
    ]
    if mismatched:
        raise M9aFreezeError(f"Native Messaging binding points to foreign/unknown manifest: {sorted(mismatched)}")


def _remove_verified_hkcu_registry_bindings(expected_manifest: Path) -> list[str]:
    import winreg

    removed: list[str] = []
    for browser, suffix in _native_registry_suffixes():
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, suffix, 0, winreg.KEY_READ) as key:
                value, _ = winreg.QueryValueEx(key, None)
        except FileNotFoundError:
            continue
        path = Path(str(value)).expanduser().resolve(strict=False)
        if _normalized(path) != _normalized(expected_manifest):
            raise M9aFreezeError(f"refusing to remove changed Native Messaging binding: {browser}")
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, suffix)
        except OSError as exc:
            raise M9aFreezeError(f"failed to remove Native Messaging binding {browser}: {exc}") from exc
        removed.append(browser)
    return removed


def _rename_for_freeze(source: Path, stamp: str) -> Path:
    if not source.exists():
        raise M9aFreezeError(f"freeze source disappeared: {source}")
    target = source.with_name(f"{source.name}.m9a-frozen-{stamp}")
    if target.exists():
        raise M9aFreezeError(f"freeze target already exists: {target}")
    source.rename(target)
    return target


def _stable_hash_map(paths: Iterable[Path]) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(set(paths), key=lambda item: str(item).casefold()):
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file():
            raise M9aFreezeError(f"zero-write subject is not a regular file: {path}")
        before = path.stat()
        data = path.read_bytes()
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise M9aFreezeError(f"zero-write subject changed during read: {path}")
        result[str(path)] = _sha256_bytes(data)
    return result


def _write_report(path: Path, payload: Mapping[str, Any]) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(rendered, encoding="utf-8")
    os.replace(temporary, path)


def execute_freeze(
    *,
    profiles: Sequence[ProfileSpec],
    native_config_path: Path,
    archive_parent: Path,
    expected_vnext_root: Path,
    observation_seconds: float,
    apply: bool,
) -> dict[str, Any]:
    if sys.platform != "win32":
        raise M9aFreezeError("M9a freeze is Windows-only")
    if not apply:
        raise M9aFreezeError("effectful M9a freeze requires explicit --apply")
    if not profiles:
        raise M9aFreezeError("at least one legacy profile is required")
    if len({item.profile_id for item in profiles}) != len(profiles):
        raise M9aFreezeError("duplicate legacy profile_id")
    if observation_seconds < 2 or observation_seconds > 60:
        raise M9aFreezeError("observation_seconds must be between 2 and 60")
    if expected_vnext_root.exists():
        raise M9aFreezeError("vNext runtime root already exists; M9a requires vNext externally OFF")

    native_config_path = native_config_path.expanduser().resolve(strict=True)
    if native_config_path.is_symlink() or not native_config_path.is_file():
        raise M9aFreezeError("native config must be a regular file")
    install_root = native_config_path.parent
    expected_manifest = install_root / f"{HOST_NAME}.json"
    if expected_manifest.is_symlink() or not expected_manifest.is_file():
        raise M9aFreezeError("expected legacy Native Messaging manifest is missing")

    manifest_document = _read_json_object(expected_manifest)
    executable_raw = manifest_document.get("path")
    if not isinstance(executable_raw, str) or not executable_raw:
        raise M9aFreezeError("legacy Native Messaging manifest has no executable path")
    host_executable = Path(executable_raw).expanduser().resolve(strict=False)
    if host_executable.is_symlink() or not host_executable.is_file():
        raise M9aFreezeError("legacy Native Host executable is missing or non-regular")

    profile_paths = {item.profile_id: _effective_paths(item.bridge_config_path) for item in profiles}
    locks = [
        _HeldWindowsByteLock(profile_paths[item.profile_id]["runtime"] / "bridge.instance.lock")
        for item in profiles
    ]

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_parent = archive_parent.expanduser().resolve(strict=False)
    archive_root = archive_parent / f"m9a-{stamp}"
    if archive_root.exists():
        raise M9aFreezeError(f"archive candidate already exists: {archive_root}")
    archive_root.mkdir(parents=True, exist_ok=False)
    report_path = archive_root / "m9a-freeze-report.json"
    archive_manifest_path = archive_root / "archive-manifest.json"

    report: dict[str, Any] = {
        "schema": FREEZE_SCHEMA,
        "started_at": _utc_text(),
        "status": "STARTED",
        "archive_root": str(archive_root),
        "profiles": [item.profile_id for item in profiles],
        "preconditions": {},
        "effects": [],
        "post_freeze": {},
        "legacy_ingress_frozen": False,
        "legacy_writer_frozen": False,
        "archive_created": False,
        "zero_new_write_observed": False,
        "vnext_activation_allowed": False,
        "m9b_allowed": False,
        "partial_freeze_requires_roll_forward": False,
    }
    _write_report(report_path, report)

    first_effect = False
    try:
        for lock in locks:
            lock.acquire()
        report["preconditions"]["legacy_locks_acquired"] = True
        report["preconditions"]["lock_files_created_for_freeze"] = [
            str(lock.path) for lock in locks if lock.created_for_freeze
        ]

        fresh_probes: dict[str, dict[str, Any]] = {}
        scratch = archive_root / "probe-scratch"
        for item in profiles:
            probe = probe_profile(
                profile_id=item.profile_id,
                bridge_config_path=item.bridge_config_path,
                native_config_path=native_config_path,
                scratch_dir=scratch,
                max_records=5000,
            ).as_dict()
            _validate_probe(item, probe)
            fresh_probes[item.profile_id] = probe
        report["preconditions"]["fresh_probe_digests"] = {
            key: value["probe_digest"] for key, value in sorted(fresh_probes.items())
        }
        report["preconditions"]["fresh_probes_match_expected"] = True

        registry_before = _registry_state(expected_manifest)
        _validate_registry_before_freeze(registry_before)
        report["preconditions"]["registry_before"] = registry_before

        total_state = [0]
        archive_entries: list[dict[str, Any]] = []
        for item in profiles:
            paths = profile_paths[item.profile_id]
            archive_entries.extend(
                _archive_tree(
                    paths["runtime"],
                    archive_root,
                    f"profile-{item.profile_id}-runtime",
                    total_state=total_state,
                )
            )
            archive_entries.extend(
                _archive_single(
                    item.bridge_config_path,
                    archive_root,
                    f"profile-{item.profile_id}-config",
                    total_state=total_state,
                )
            )

        archive_entries.extend(
            _archive_single(native_config_path, archive_root, "native", total_state=total_state)
        )
        archive_entries.extend(
            _archive_single(expected_manifest, archive_root, "native", total_state=total_state)
        )
        for optional_name in (
            "native-host-arm.json",
            "native-host-sessions.json",
            "native-host-requests.json",
        ):
            archive_entries.extend(
                _archive_single(
                    install_root / optional_name,
                    archive_root,
                    "native",
                    total_state=total_state,
                    required=False,
                )
            )
        archive_entries.extend(
            _archive_single(host_executable, archive_root, "native-host-executable", total_state=total_state)
        )

        archive_manifest: dict[str, Any] = {
            "schema": ARCHIVE_SCHEMA,
            "created_at": _utc_text(),
            "entries": sorted(archive_entries, key=lambda entry: entry["source_path"].casefold()),
            "entry_count": len(archive_entries),
            "total_bytes": total_state[0],
            "profile_probe_digests": report["preconditions"]["fresh_probe_digests"],
            "registry_before": registry_before,
        }
        archive_manifest["archive_digest"] = semantic_digest(archive_manifest)
        _write_report(archive_manifest_path, archive_manifest)
        report["archive_created"] = True
        report["archive_manifest"] = {
            "path": str(archive_manifest_path),
            "digest": archive_manifest["archive_digest"],
            "entry_count": archive_manifest["entry_count"],
            "total_bytes": archive_manifest["total_bytes"],
        }
        report["status"] = "ARCHIVE_VERIFIED"
        _write_report(report_path, report)

        removed = _remove_verified_hkcu_registry_bindings(expected_manifest)
        first_effect = bool(removed)
        report["effects"].append({"effect": "NATIVE_MESSAGING_HKCU_UNREGISTER", "browsers": removed})
        report["partial_freeze_requires_roll_forward"] = first_effect
        _write_report(report_path, report)

        frozen_native_config = _rename_for_freeze(native_config_path, stamp)
        first_effect = True
        report["partial_freeze_requires_roll_forward"] = True
        report["effects"].append(
            {"effect": "NATIVE_CONFIG_FROZEN", "from": str(native_config_path), "to": str(frozen_native_config)}
        )
        _write_report(report_path, report)

        frozen_manifest = _rename_for_freeze(expected_manifest, stamp)
        report["effects"].append(
            {"effect": "NATIVE_MANIFEST_FROZEN", "from": str(expected_manifest), "to": str(frozen_manifest)}
        )
        _write_report(report_path, report)

        frozen_configs: dict[str, str] = {}
        frozen_spools: dict[str, str | None] = {}
        for item in profiles:
            frozen_config = _rename_for_freeze(item.bridge_config_path, stamp)
            frozen_configs[item.profile_id] = str(frozen_config)
            report["effects"].append(
                {
                    "effect": "BRIDGE_CONFIG_FROZEN",
                    "profile_id": item.profile_id,
                    "from": str(item.bridge_config_path),
                    "to": str(frozen_config),
                }
            )
            spool = profile_paths[item.profile_id]["spool"]
            if spool.exists():
                if spool.is_symlink() or not spool.is_dir():
                    raise M9aFreezeError(f"live spool root is not a regular directory: {spool}")
                frozen_spool = _rename_for_freeze(spool, stamp)
                frozen_spools[item.profile_id] = str(frozen_spool)
                report["effects"].append(
                    {
                        "effect": "SPOOL_QUARANTINED",
                        "profile_id": item.profile_id,
                        "from": str(spool),
                        "to": str(frozen_spool),
                        "entry_count": fresh_probes[item.profile_id]["spool"].get("entry_count"),
                    }
                )
            else:
                frozen_spools[item.profile_id] = None
            _write_report(report_path, report)

        registry_after = _registry_state(frozen_manifest)
        remaining_bindings = [
            f"{root}/{browser}"
            for root, values in registry_after.items()
            if isinstance(values, Mapping)
            for browser, item in values.items()
            if isinstance(item, Mapping) and item.get("present") is True
        ]
        if remaining_bindings:
            raise M9aFreezeError(f"Native Messaging binding remains after freeze: {remaining_bindings}")
        if native_config_path.exists() or expected_manifest.exists():
            raise M9aFreezeError("legacy Native Host live config/manifest path reappeared")
        for item in profiles:
            if item.bridge_config_path.exists():
                raise M9aFreezeError(f"legacy live bridge config reappeared: {item.profile_id}")
            if profile_paths[item.profile_id]["spool"].exists():
                raise M9aFreezeError(f"legacy live spool inbox reappeared: {item.profile_id}")
            if _wake_event_exists(profile_paths[item.profile_id]["runtime"]):
                raise M9aFreezeError(f"legacy wake event appeared after freeze: {item.profile_id}")

        zero_write_subjects: list[Path] = []
        for item in profiles:
            runtime = profile_paths[item.profile_id]["runtime"]
            zero_write_subjects.extend(
                path
                for path in _iter_regular_files(runtime)
                if path.name != "bridge.instance.lock" and ".m9a-frozen-" not in path.as_posix()
            )
        for optional_name in ("native-host-arm.json", "native-host-sessions.json", "native-host-requests.json"):
            zero_write_subjects.append(install_root / optional_name)

        before_hashes = _stable_hash_map(zero_write_subjects)
        interval_started = _utc_text()
        time.sleep(observation_seconds)
        after_hashes = _stable_hash_map(zero_write_subjects)
        interval_ended = _utc_text()
        if before_hashes != after_hashes:
            changed = sorted(set(before_hashes) | set(after_hashes))
            changed = [key for key in changed if before_hashes.get(key) != after_hashes.get(key)]
            raise M9aFreezeError(f"legacy writes observed after freeze: {changed[:20]}")

        registry_final = _registry_state(frozen_manifest)
        remaining_final = [
            f"{root}/{browser}"
            for root, values in registry_final.items()
            if isinstance(values, Mapping)
            for browser, item in values.items()
            if isinstance(item, Mapping) and item.get("present") is True
        ]
        if remaining_final:
            raise M9aFreezeError(f"Native Messaging binding resurrected during observation: {remaining_final}")
        if expected_vnext_root.exists():
            raise M9aFreezeError("vNext runtime root appeared during M9a freeze")

        report["post_freeze"] = {
            "registry": registry_final,
            "frozen_native_config": str(frozen_native_config),
            "frozen_manifest": str(frozen_manifest),
            "frozen_bridge_configs": frozen_configs,
            "quarantined_spools": frozen_spools,
            "zero_write_interval": {
                "started_at": interval_started,
                "ended_at": interval_ended,
                "seconds": observation_seconds,
                "subject_count": len(before_hashes),
                "unchanged": True,
            },
            "vnext_runtime_root_absent": True,
        }
        report["status"] = "PASS_CLOSED"
        report["legacy_ingress_frozen"] = True
        report["legacy_writer_frozen"] = True
        report["zero_new_write_observed"] = True
        report["vnext_activation_allowed"] = False
        report["m9b_allowed"] = False
        report["partial_freeze_requires_roll_forward"] = False
        report["completed_at"] = _utc_text()
        report["freeze_digest"] = semantic_digest(
            {key: value for key, value in report.items() if key != "freeze_digest"}
        )
        _write_report(report_path, report)
        return report
    except Exception as exc:
        report["status"] = "PARTIAL_FREEZE_STOP" if first_effect else "PRECONDITION_STOP"
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        report["partial_freeze_requires_roll_forward"] = bool(first_effect)
        report["vnext_activation_allowed"] = False
        report["m9b_allowed"] = False
        report["stopped_at"] = _utc_text()
        try:
            _write_report(report_path, report)
        except Exception:
            pass
        raise
    finally:
        for lock in reversed(locks):
            lock.release()


def _parse_profile_spec(raw: str) -> ProfileSpec:
    parts = raw.split("::", 2)
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("profile must be PROFILE_ID::BRIDGE_CONFIG::PROBE_DIGEST")
    profile_id, path, digest = parts
    try:
        return ProfileSpec(profile_id, Path(path), digest)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Execute one-way local M9a legacy ingress freeze")
    parser.add_argument("--profile", action="append", type=_parse_profile_spec, required=True)
    parser.add_argument("--native-config", required=True)
    parser.add_argument("--archive-parent", required=True)
    parser.add_argument("--vnext-runtime-root", required=True)
    parser.add_argument("--observation-seconds", type=float, default=10.0)
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = execute_freeze(
            profiles=tuple(args.profile),
            native_config_path=Path(args.native_config),
            archive_parent=Path(args.archive_parent),
            expected_vnext_root=Path(args.vnext_runtime_root),
            observation_seconds=args.observation_seconds,
            apply=args.apply,
        )
    except Exception as exc:
        sys.stderr.write(f"M9a freeze failed: {type(exc).__name__}: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ARCHIVE_SCHEMA",
    "FREEZE_SCHEMA",
    "M9aFreezeError",
    "ProfileSpec",
    "execute_freeze",
]

"""Bounded read-only diagnostics for fresh-R0b/M9a legacy blockers.

This probe is deliberately narrower than R0a.  It inspects only blocker classes
already identified by runtime-inventory-v1: stale service candidates, direct
spool entries, unresolved command/effect capability, Native ingress state,
receipt-store shape, and promoter integrity.

Observed legacy paths are never opened for writing.  The Journal is copied to a
caller-owned scratch directory after a stable-file check and the copy is opened
read-only/query-only.  The output contains aggregates, hashes and typed issues;
command JSON and file contents are never emitted.  Nothing in this module grants
mutation authority or M9b activation.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from bdb_shared.evidence import semantic_digest


PROBE_SCHEMA = "bdb-vnext-m9a-blocker-probe-v1"
_SHA40 = re.compile(r"^[0-9a-fA-F]{40}$")
_SAFE_SPOOL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.json$")
_TERMINAL_COMMAND_STATES = frozenset(
    {
        "acknowledged",
        "rejected",
        "expired",
        "policy_denied",
        "stale_revision",
        "state_mismatch",
        "cancelled",
    }
)


class BlockerProbeError(RuntimeError):
    pass


def _hash_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8", errors="strict")).hexdigest()


def _hash_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _read_bytes(path: Path, *, max_bytes: int = 8 * 1024 * 1024) -> tuple[bytes, tuple[int, int]]:
    if path.is_symlink() or not path.is_file():
        raise BlockerProbeError(f"not_regular_file:{path.name}")
    before = path.stat()
    if before.st_size > max_bytes:
        raise BlockerProbeError(f"size_limit:{path.name}")
    data = path.read_bytes()
    after = path.stat()
    token_before = (int(before.st_size), int(before.st_mtime_ns))
    token_after = (int(after.st_size), int(after.st_mtime_ns))
    if token_before != token_after or len(data) != after.st_size:
        raise BlockerProbeError(f"source_changed:{path.name}")
    return data, token_after


def _read_json(path: Path, *, max_bytes: int = 8 * 1024 * 1024) -> tuple[dict[str, Any], str]:
    data, _ = _read_bytes(path, max_bytes=max_bytes)
    try:
        value = json.loads(data.decode("utf-8-sig", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BlockerProbeError(f"invalid_json:{path.name}") from exc
    if not isinstance(value, dict):
        raise BlockerProbeError(f"invalid_shape:{path.name}")
    return value, _hash_bytes(data)


def _path(raw: object, default: Path) -> Path:
    if isinstance(raw, str) and raw:
        return Path(raw).expanduser().resolve(strict=False)
    return default.expanduser().resolve(strict=False)


def _effective_bridge_paths(config_path: Path) -> dict[str, Any]:
    raw, digest = _read_json(config_path)
    runtime = _path(raw.get("runtime_dir"), config_path.parent / "runtime")
    journal = _path(raw.get("journal_path"), runtime / "journal.db")
    spool = _path(raw.get("direct_spool_dir"), runtime / "direct_spool" / "inbox")
    results = _path(raw.get("direct_result_dir"), runtime / "direct_spool" / "results")
    return {
        "raw": raw,
        "digest": digest,
        "runtime": runtime,
        "journal": journal,
        "spool": spool,
        "results": results,
        "direct_spool_enabled": raw.get("direct_spool_enabled", True) is True,
    }


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except (OSError, PermissionError):
            return False
        return True
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    kernel32.GetExitCodeProcess.restype = ctypes.c_bool
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_uint32()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return int(code.value) == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _wake_event_name(runtime: Path) -> str:
    normalized = str(runtime.expanduser().resolve(strict=False)).casefold()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]
    return f"Local\\BDB-{digest}"


def _wake_event_exists(runtime: Path) -> bool:
    if os.name != "nt":
        return False
    SYNCHRONIZE = 0x00100000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenEventW.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.OpenEventW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenEventW(SYNCHRONIZE, False, _wake_event_name(runtime))
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True


def _native_manifests() -> tuple[dict[str, Any], ...]:
    if os.name != "nt":
        return ()
    import winreg

    suffixes = (
        r"Software\Google\Chrome\NativeMessagingHosts\com.bartosz.dev_bridge",
        r"Software\Microsoft\Edge\NativeMessagingHosts\com.bartosz.dev_bridge",
    )
    roots = (("HKCU", winreg.HKEY_CURRENT_USER), ("HKLM", winreg.HKEY_LOCAL_MACHINE))
    found: list[dict[str, Any]] = []
    for root_name, root in roots:
        for suffix in suffixes:
            try:
                with winreg.OpenKey(root, suffix, 0, winreg.KEY_READ) as key:
                    raw, _ = winreg.QueryValueEx(key, None)
            except OSError:
                continue
            path = Path(str(raw)).expanduser().resolve(strict=False)
            digest = None
            exists = path.is_file() and not path.is_symlink()
            if exists:
                try:
                    digest = _hash_bytes(_read_bytes(path, max_bytes=1024 * 1024)[0])
                except BlockerProbeError:
                    digest = None
            found.append(
                {
                    "registry_root": root_name,
                    "browser": "edge" if "Microsoft\\Edge" in suffix else "chrome",
                    "manifest_path_hash": _hash_text(str(path)),
                    "manifest_exists": exists,
                    "manifest_digest": digest,
                }
            )
    return tuple(found)


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _native_observation(native_config_path: Path, bridge_config_path: Path, *, now: datetime) -> dict[str, Any]:
    raw, digest = _read_json(native_config_path)
    parent = native_config_path.parent
    repositories = raw.get("repositories")
    bindings: list[dict[str, Any]] = []
    if isinstance(repositories, dict):
        for alias, item in sorted(repositories.items(), key=lambda pair: str(pair[0])):
            value = item if isinstance(item, str) else item.get("bridge_config_path") if isinstance(item, dict) else None
            if isinstance(value, str) and value:
                candidate = Path(value).expanduser().resolve(strict=False)
                bindings.append(
                    {
                        "alias": str(alias),
                        "bridge_path_hash": _hash_text(str(candidate)),
                        "matches_profile": os.path.normcase(str(candidate)) == os.path.normcase(str(bridge_config_path.resolve(strict=False))),
                    }
                )
    elif isinstance(raw.get("bridge_config_path"), str):
        candidate = Path(raw["bridge_config_path"]).expanduser().resolve(strict=False)
        bindings.append(
            {
                "alias": "default",
                "bridge_path_hash": _hash_text(str(candidate)),
                "matches_profile": os.path.normcase(str(candidate)) == os.path.normcase(str(bridge_config_path.resolve(strict=False))),
            }
        )

    arm_path = _path(raw.get("state_path"), parent / "native-host-arm.json")
    request_path = _path(raw.get("request_store_path"), parent / "native-host-requests.json")
    arm: dict[str, Any] = {"present": arm_path.is_file(), "effective_armed": False, "reason": "missing"}
    if arm_path.is_file() and not arm_path.is_symlink():
        try:
            arm_raw, arm_digest = _read_json(arm_path, max_bytes=1024 * 1024)
            armed_until = _parse_utc(arm_raw.get("armed_until"))
            declared = arm_raw.get("armed") is True
            effective = bool(declared and armed_until is not None and now < armed_until)
            arm = {
                "present": True,
                "digest": arm_digest,
                "schema": arm_raw.get("schema"),
                "declared_armed": declared,
                "effective_armed": effective,
                "armed_until": None if armed_until is None else armed_until.isoformat(),
                "reason": "armed" if effective else "disarmed_or_expired",
            }
        except BlockerProbeError as exc:
            arm = {"present": True, "effective_armed": False, "reason": str(exc)}

    return {
        "config_digest": digest,
        "schema": raw.get("schema"),
        "bindings": bindings,
        "profile_bound": any(item["matches_profile"] for item in bindings),
        "arm": arm,
        "request_store_path": request_path,
        "installed_manifests": list(_native_manifests()),
    }


def _receipt_shape(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"status": "MISSING"}
    try:
        raw, digest = _read_json(path, max_bytes=4 * 1024 * 1024)
    except BlockerProbeError as exc:
        return {"status": "INVALID", "issue": str(exc)}
    requests = raw.get("requests")
    reservations = raw.get("submission_reservations")
    return {
        "status": "VALID_SHAPE" if isinstance(requests, dict) and isinstance(reservations, dict) else "INVALID_SHAPE",
        "digest": digest,
        "schema": raw.get("schema"),
        "top_level_keys": sorted(str(key) for key in raw)[:64],
        "requests_type": type(requests).__name__,
        "requests_count": len(requests) if isinstance(requests, dict) else None,
        "submission_reservations_type": type(reservations).__name__,
        "submission_reservations_count": len(reservations) if isinstance(reservations, dict) else None,
    }


def _promoter_observation(runtime: Path, *, max_records: int) -> dict[str, Any]:
    state_path = runtime / "workspace-promoter-state.json"
    receipts_root = runtime / "promotions"
    missing: list[str] = []
    if not state_path.is_file():
        missing.append("workspace-promoter-state.json")
    if not receipts_root.is_dir():
        missing.append("promotions/")
    if missing:
        return {"status": "MISSING", "missing_components": missing}

    issues: list[dict[str, Any]] = []
    try:
        state, state_digest = _read_json(state_path, max_bytes=4 * 1024 * 1024)
    except BlockerProbeError as exc:
        return {"status": "INVALID", "issues": [{"code": str(exc)}]}
    if state.get("schema") != "bdb-workspace-promoter-state-v1":
        issues.append({"code": "unsupported_state_schema"})
    if not isinstance(state.get("initialized"), bool) or not isinstance(state.get("seen"), dict):
        issues.append({"code": "invalid_promoter_state"})

    receipt_files = sorted(
        (item for item in receipts_root.iterdir() if item.is_file() and not item.is_symlink() and item.suffix == ".json" and not item.name.startswith(".")),
        key=lambda item: item.name,
    )
    truncated = len(receipt_files) > max_records
    identities: set[tuple[str, int]] = set()
    sequences: set[int] = set()
    valid_sequences: list[int] = []
    for path in receipt_files[:max_records]:
        try:
            document, digest = _read_json(path, max_bytes=4 * 1024 * 1024)
        except BlockerProbeError as exc:
            issues.append({"code": str(exc), "file_id": _hash_text(path.name)})
            continue
        code = None
        if document.get("schema") != "bdb-workspace-promotion-v1":
            code = "unsupported_receipt_schema"
        source_commit = document.get("source_commit")
        parent_commit = document.get("parent_commit")
        sequence = document.get("repository_event_seq")
        session_id = document.get("session_id")
        command_sequence = document.get("sequence")
        if code is None and (not isinstance(source_commit, str) or not _SHA40.fullmatch(source_commit)):
            code = "invalid_source_commit"
        if code is None and (not isinstance(parent_commit, str) or not _SHA40.fullmatch(parent_commit)):
            code = "invalid_parent_commit"
        if code is None and (isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0):
            code = "invalid_repository_event_seq"
        if code is None and (not isinstance(session_id, str) or not session_id or isinstance(command_sequence, bool) or not isinstance(command_sequence, int) or command_sequence <= 0):
            code = "invalid_command_identity"
        identity = (str(session_id), int(command_sequence)) if isinstance(session_id, str) and isinstance(command_sequence, int) and not isinstance(command_sequence, bool) else None
        if code is None and (identity in identities or sequence in sequences):
            code = "duplicate_identity"
        if code is not None:
            issues.append({"code": code, "file_id": _hash_text(path.name), "digest": digest})
            continue
        assert identity is not None and isinstance(sequence, int)
        identities.add(identity)
        sequences.add(sequence)
        valid_sequences.append(sequence)

    sequence_path = receipts_root / ".repository-event-seq.json"
    sequence_value = 0
    if sequence_path.exists():
        try:
            seq_doc, seq_digest = _read_json(sequence_path, max_bytes=1024 * 1024)
            sequence_value = seq_doc.get("repository_event_seq")
            if seq_doc.get("schema") != "bdb-repository-event-seq-v1":
                issues.append({"code": "unsupported_sequence_schema", "digest": seq_digest})
            elif isinstance(sequence_value, bool) or not isinstance(sequence_value, int) or sequence_value < 0:
                issues.append({"code": "invalid_repository_sequence", "digest": seq_digest})
                sequence_value = 0
        except BlockerProbeError as exc:
            issues.append({"code": str(exc), "file_id": _hash_text(sequence_path.name)})
    if not truncated and not issues:
        if (valid_sequences and max(valid_sequences) != sequence_value) or (not valid_sequences and sequence_value != 0):
            issues.append({"code": "sequence_disagreement"})

    return {
        "status": "TRUNCATED" if truncated else "INVALID" if issues else "VALID",
        "state_digest": state_digest,
        "initialized": state.get("initialized"),
        "seen_count": len(state.get("seen", {})) if isinstance(state.get("seen"), dict) else None,
        "receipt_count": len(receipt_files),
        "repository_event_seq": sequence_value,
        "issues": issues[:max_records],
        "truncated": truncated,
    }


def _stable_copy_journal(source: Path, scratch: Path) -> tuple[Path, dict[str, Any]]:
    scratch.mkdir(parents=True, exist_ok=True)
    copied: dict[str, Any] = {}
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(source) + suffix)
        if not src.exists():
            continue
        if src.is_symlink() or not src.is_file():
            raise BlockerProbeError(f"journal_sidecar_not_regular:{suffix or 'db'}")
        before = src.stat()
        dst = scratch / ("journal.db" + suffix)
        shutil.copyfile(src, dst)
        after = src.stat()
        token_before = (int(before.st_size), int(before.st_mtime_ns))
        token_after = (int(after.st_size), int(after.st_mtime_ns))
        if token_before != token_after or dst.stat().st_size != after.st_size:
            raise BlockerProbeError(f"journal_changed_during_copy:{suffix or 'db'}")
        copied[suffix or "db"] = {"size": int(after.st_size), "digest": _hash_bytes(dst.read_bytes())}
    snapshot = scratch / "journal.db"
    if not snapshot.exists():
        raise BlockerProbeError("journal_missing")
    return snapshot, copied


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _command_capability(state: str, has_effect: bool) -> str:
    if state in {"discovered", "validated", "claimed"}:
        return "POTENTIAL_EXECUTION_WRITE_ON_SERVICE_RESTART"
    if state == "executing":
        return "RECOVERY_WRITE_OR_DIVERGENCE" if not has_effect else "INCONSISTENT_EXECUTING_WITH_EFFECT"
    if state == "effect_recorded":
        return "IDEMPOTENT_REPLAY_OR_DIVERGENCE_ONLY" if has_effect else "INCONSISTENT_EFFECT_STATE"
    if state == "result_staged":
        return "PUBLICATION_RECOVERY_ON_SERVICE_RESTART"
    if state == "result_published":
        return "ACKNOWLEDGEMENT_ONLY_ON_SERVICE_RESTART"
    if state == "manual_reconciliation_required":
        return "MANUAL_ONLY"
    return "UNKNOWN_NONTERMINAL_CAPABILITY"


def _journal_observation(snapshot: Path, *, max_records: int, pid_alive_fn: Callable[[int], bool]) -> tuple[dict[str, Any], dict[str, str]]:
    uri = f"file:{snapshot.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.execute("PRAGMA query_only=ON")
    command_states: dict[str, int] = {}
    resource_groups: Counter[tuple[str, str, str, str, bool, bool]] = Counter()
    command_state_by_id: dict[str, str] = {}
    try:
        tables = _table_names(connection)
        required = {"commands", "service_instances"}
        if not required.issubset(tables):
            raise BlockerProbeError("journal_required_tables_missing")
        for state, count in connection.execute("SELECT state, COUNT(*) FROM commands GROUP BY state ORDER BY state"):
            command_states[str(state)] = int(count)

        service_rows = list(
            connection.execute(
                "SELECT instance_id, pid, state, heartbeat_at FROM service_instances WHERE state IN ('running','stopping') ORDER BY instance_id LIMIT ?",
                (max_records + 1,),
            )
        )
        service_truncated = len(service_rows) > max_records
        service_items = [
            {
                "instance_id": _hash_text(str(instance_id)),
                "pid": int(pid),
                "pid_alive": pid_alive_fn(int(pid)),
                "state": str(state),
                "heartbeat_at": str(heartbeat_at),
            }
            for instance_id, pid, state, heartbeat_at in service_rows[:max_records]
        ]

        plan_table = "operation_plans" in tables
        effect_table = "operation_effects" in tables
        outbox_table = "outbox" in tables
        select = ["c.command_id", "c.state"]
        joins = []
        if plan_table:
            select.extend(["p.operation", "p.target_path", "p.profile_id"])
            joins.append("LEFT JOIN operation_plans p ON p.command_id = c.command_id")
        else:
            select.extend(["NULL", "NULL", "NULL"])
        if effect_table:
            select.append("CASE WHEN e.command_id IS NULL THEN 0 ELSE 1 END")
            joins.append("LEFT JOIN operation_effects e ON e.command_id = c.command_id")
        else:
            select.append("0")
        if outbox_table:
            select.append("CASE WHEN o.command_id IS NULL THEN 0 ELSE 1 END")
            joins.append("LEFT JOIN outbox o ON o.command_id = c.command_id")
        else:
            select.append("0")
        placeholders = ",".join("?" for _ in _TERMINAL_COMMAND_STATES)
        sql = (
            f"SELECT {', '.join(select)} FROM commands c {' '.join(joins)} "
            f"WHERE c.state NOT IN ({placeholders}) ORDER BY c.command_id LIMIT ?"
        )
        rows = list(connection.execute(sql, (*sorted(_TERMINAL_COMMAND_STATES), max_records + 1)))
        unresolved_truncated = len(rows) > max_records
        capability_counts: Counter[str] = Counter()
        for command_id, state, operation, target_path, profile_id, has_effect, has_outbox in rows[:max_records]:
            command_id = str(command_id)
            state = str(state)
            command_state_by_id[command_id] = state
            effect = bool(has_effect)
            capability = _command_capability(state, effect)
            capability_counts[capability] += 1
            resource_key = _hash_text(f"{profile_id or ''}|{target_path or ''}")
            resource_groups[(state, str(operation or ""), str(profile_id or ""), resource_key, effect, bool(has_outbox))] += 1

        groups = [
            {
                "state": key[0],
                "operation": key[1] or None,
                "profile_id": key[2] or None,
                "resource_key": key[3],
                "has_effect": key[4],
                "has_outbox": key[5],
                "count": count,
                "capability": _command_capability(key[0], key[4]),
            }
            for key, count in sorted(resource_groups.items(), key=lambda item: item[0])
        ]
        return (
            {
                "command_states": command_states,
                "service_candidates": {
                    "count": len(service_rows),
                    "items": service_items,
                    "truncated": service_truncated,
                },
                "unresolved_rows_observed": min(len(rows), max_records),
                "unresolved_truncated": unresolved_truncated,
                "capability_counts": dict(sorted(capability_counts.items())),
                "resource_groups": groups,
                "tables_observed": sorted(tables),
            },
            command_state_by_id,
        )
    finally:
        connection.close()


def _spool_observation(root: Path, command_states: Mapping[str, str], *, max_records: int) -> dict[str, Any]:
    if not root.is_dir():
        return {"status": "MISSING", "entry_count": 0, "entries": []}
    candidates = sorted((item for item in root.iterdir() if item.is_file() and not item.is_symlink() and item.suffix == ".json"), key=lambda item: item.name)
    truncated = len(candidates) > max_records
    entries: list[dict[str, Any]] = []
    classes: Counter[str] = Counter()
    for path in candidates[:max_records]:
        if not _SAFE_SPOOL.fullmatch(path.name):
            entries.append({"file_id": _hash_text(path.name), "classification": "UNSAFE_FILENAME"})
            classes["UNSAFE_FILENAME"] += 1
            continue
        try:
            document, digest = _read_json(path, max_bytes=1024 * 1024)
        except BlockerProbeError as exc:
            entries.append({"file_id": _hash_text(path.name), "classification": str(exc)})
            classes[str(exc)] += 1
            continue
        command = document.get("command")
        command_id = command.get("command_id") if isinstance(command, dict) else None
        state = command_states.get(str(command_id)) if isinstance(command_id, str) else None
        if not isinstance(command_id, str) or not command_id:
            classification = "INVALID_ENVELOPE_IDENTITY"
        elif state is None:
            classification = "NEW_INGRESS_IF_SERVICE_RESTARTS"
        elif state in _TERMINAL_COMMAND_STATES:
            classification = "JOURNAL_TERMINAL_IDEMPOTENT_INPUT"
        else:
            classification = "CORRELATED_WITH_UNRESOLVED_COMMAND"
        classes[classification] += 1
        entries.append(
            {
                "file_id": _hash_text(path.name),
                "digest": digest,
                "command_id": _hash_text(str(command_id)) if isinstance(command_id, str) else None,
                "journal_state": state,
                "classification": classification,
            }
        )
    return {
        "status": "TRUNCATED" if truncated else "OBSERVED",
        "entry_count": len(candidates),
        "classification_counts": dict(sorted(classes.items())),
        "entries": entries,
        "truncated": truncated,
    }


@dataclass(frozen=True)
class BlockerProbe:
    profile_id: str
    bridge_config_digest: str
    journal_snapshot: Mapping[str, Any]
    journal: Mapping[str, Any]
    spool: Mapping[str, Any]
    native: Mapping[str, Any]
    receipts: Mapping[str, Any]
    promoter: Mapping[str, Any]
    wake_event_present: bool

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": PROBE_SCHEMA,
            "profile_id": self.profile_id,
            "bridge_config_digest": self.bridge_config_digest,
            "journal_snapshot": dict(self.journal_snapshot),
            "journal": dict(self.journal),
            "spool": dict(self.spool),
            "native": dict(self.native),
            "receipts": dict(self.receipts),
            "promoter": dict(self.promoter),
            "wake_event_present": self.wake_event_present,
            "legacy_mutation_performed": False,
            "vnext_activation_allowed": False,
            "m9b_allowed": False,
            "authorized_scope": ["READ_ONLY_BLOCKER_OBSERVATION", "COLLISION_CLASSIFICATION_INPUT"],
        }
        payload["probe_digest"] = semantic_digest(payload)
        return payload


def probe_profile(
    *,
    profile_id: str,
    bridge_config_path: str | Path,
    native_config_path: str | Path,
    scratch_dir: str | Path,
    max_records: int = 500,
    now_fn: Callable[[], datetime] | None = None,
    pid_alive_fn: Callable[[int], bool] = _pid_alive,
    wake_event_fn: Callable[[Path], bool] = _wake_event_exists,
) -> BlockerProbe:
    if not profile_id or len(profile_id) > 128:
        raise ValueError("profile_id must be bounded")
    if not 1 <= max_records <= 5000:
        raise ValueError("max_records must be between 1 and 5000")
    bridge_path = Path(bridge_config_path).expanduser().resolve(strict=True)
    native_path = Path(native_config_path).expanduser().resolve(strict=True)
    scratch = Path(scratch_dir).expanduser().resolve(strict=False)
    bridge = _effective_bridge_paths(bridge_path)
    now = (now_fn or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc)
    native = _native_observation(native_path, bridge_path, now=now)
    snapshot, snapshot_identity = _stable_copy_journal(bridge["journal"], scratch / profile_id)
    journal, command_states = _journal_observation(snapshot, max_records=max_records, pid_alive_fn=pid_alive_fn)
    spool = _spool_observation(bridge["spool"], command_states, max_records=max_records)
    receipts = _receipt_shape(native["request_store_path"])
    promoter = _promoter_observation(bridge["runtime"], max_records=max_records)
    return BlockerProbe(
        profile_id=profile_id,
        bridge_config_digest=bridge["digest"],
        journal_snapshot=snapshot_identity,
        journal=journal,
        spool=spool,
        native={key: value for key, value in native.items() if key != "request_store_path"},
        receipts=receipts,
        promoter=promoter,
        wake_event_present=wake_event_fn(bridge["runtime"]) if bridge["direct_spool_enabled"] else False,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only M9a blocker-specific legacy probe")
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--bridge-config", required=True)
    parser.add_argument("--native-config", required=True)
    parser.add_argument("--scratch-dir", required=True)
    parser.add_argument("--json-out")
    parser.add_argument("--max-records", type=int, default=500)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = probe_profile(
            profile_id=args.profile_id,
            bridge_config_path=args.bridge_config,
            native_config_path=args.native_config,
            scratch_dir=args.scratch_dir,
            max_records=args.max_records,
        ).as_dict()
    except Exception as exc:
        sys.stderr.write(f"M9a blocker probe failed: {type(exc).__name__}: {exc}\n")
        return 2
    rendered = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.json_out:
        destination = Path(args.json_out).expanduser().resolve(strict=False)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["BlockerProbe", "BlockerProbeError", "PROBE_SCHEMA", "probe_profile"]

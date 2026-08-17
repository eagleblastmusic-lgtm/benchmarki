"""Windows lock-safe adapter for the M9a freeze executor.

The legacy service and the M9a executor both protect a runtime with a byte-range
lock on ``bridge.instance.lock``.  On Windows, once the executor owns that byte
range, opening the same file through a second handle can raise ``PermissionError``.

This adapter preserves the existing M9a executor semantics and changes only how
that coordination file is archived:

* before any legacy lock is acquired, capture the exact lock-file bytes if the
  file already exists;
* after the executor owns the lock, archive that cached coordination snapshot
  instead of opening a second handle;
* every non-lock runtime file continues through the original verified copy path;
* the adapter never grants activation authority and never changes ordering of
  the underlying freeze effects.

``bridge.instance.lock`` is coordination evidence, not repository/runtime
semantic authority.  The fresh probe digest and the subsequently acquired
exclusive byte-range lock remain the authority fence for the freeze operation.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from bdb_vnext import m9a_freeze as base


@dataclass(frozen=True)
class LockSnapshot:
    path: Path
    existed: bool
    data: bytes | None
    size: int | None
    mtime_ns: int | None


def _normalized(path: Path) -> str:
    return os.path.normcase(str(path.expanduser().resolve(strict=False)))


def _capture_lock_snapshot(path: Path) -> LockSnapshot:
    path = path.expanduser().resolve(strict=False)
    if not path.exists():
        return LockSnapshot(path=path, existed=False, data=None, size=None, mtime_ns=None)
    if path.is_symlink() or not path.is_file():
        raise base.M9aFreezeError(f"legacy lock path is not a regular file: {path}")
    before = path.stat()
    data = path.read_bytes()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise base.M9aFreezeError(f"legacy lock file changed during pre-lock snapshot: {path}")
    if len(data) != after.st_size:
        raise base.M9aFreezeError(f"legacy lock snapshot size mismatch: {path}")
    return LockSnapshot(
        path=path,
        existed=True,
        data=data,
        size=after.st_size,
        mtime_ns=after.st_mtime_ns,
    )


def _profile_lock_paths(profiles: Sequence[base.ProfileSpec]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for profile in profiles:
        paths = base._effective_paths(profile.bridge_config_path)
        lock_path = (paths["runtime"] / "bridge.instance.lock").resolve(strict=False)
        key = _normalized(lock_path)
        if key in result:
            raise base.M9aFreezeError(f"multiple profiles resolve to one legacy lock: {lock_path}")
        result[key] = lock_path
    return result


def _verified_cached_entry(
    *,
    source: Path,
    destination: Path,
    snapshot: LockSnapshot,
    total_state: list[int],
    max_total_bytes: int = 2 * 1024 * 1024 * 1024,
) -> dict[str, Any]:
    current = source.stat()
    if snapshot.existed:
        assert snapshot.data is not None
        assert snapshot.size is not None
        # The executor may move the file pointer and hold a byte-range lock, but
        # acquiring that lock does not legitimately resize the pre-existing file.
        if current.st_size != snapshot.size:
            raise base.M9aFreezeError(
                f"legacy lock size changed between pre-lock snapshot and archive: {source}"
            )
        data = snapshot.data
        capture_mode = "PRE_LOCK_EXACT_COORDINATION_SNAPSHOT"
    else:
        # The base executor creates a missing legacy lock as a one-byte file before
        # taking the byte-range lock.  Do not open a second handle on Windows.
        if current.st_size != 1:
            raise base.M9aFreezeError(
                f"newly-created freeze lock has unexpected size: {source}: {current.st_size}"
            )
        data = b"\x00"
        capture_mode = "FREEZE_CREATED_COORDINATION_SNAPSHOT"

    total_state[0] += len(data)
    if total_state[0] > max_total_bytes:
        raise base.M9aFreezeError("archive candidate exceeds total byte bound")

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    copied = destination.read_bytes()
    source_digest = base._sha256_bytes(data)
    if base._sha256_bytes(copied) != source_digest:
        raise base.M9aFreezeError(f"archive lock snapshot verification failed: {source}")

    return {
        "source_path": str(source),
        "archive_path": str(destination),
        "size": len(data),
        "sha256": source_digest,
        "capture_mode": capture_mode,
        "coordination_only": True,
    }


def execute_freeze_lock_safe(
    *,
    profiles: Sequence[base.ProfileSpec],
    native_config_path: Path,
    archive_parent: Path,
    expected_vnext_root: Path,
    observation_seconds: float,
    apply: bool,
) -> dict[str, Any]:
    """Run the canonical M9a executor with lock-file archiving made Windows-safe."""

    lock_paths = _profile_lock_paths(profiles)
    snapshots = {
        key: _capture_lock_snapshot(path)
        for key, path in sorted(lock_paths.items())
    }

    original_archive_tree = base._archive_tree

    def archive_tree_lock_safe(
        source_root: Path,
        archive_root: Path,
        label: str,
        *,
        total_state: list[int],
    ) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for source in base._iter_regular_files(source_root):
            relative = source.relative_to(source_root)
            destination = archive_root / "sources" / label / relative
            snapshot = snapshots.get(_normalized(source))
            if snapshot is None:
                entries.append(
                    base._copy_verified_file(
                        source,
                        destination,
                        total_state=total_state,
                    )
                )
            else:
                entries.append(
                    _verified_cached_entry(
                        source=source,
                        destination=destination,
                        snapshot=snapshot,
                        total_state=total_state,
                    )
                )
        return entries

    base._archive_tree = archive_tree_lock_safe
    try:
        return base.execute_freeze(
            profiles=profiles,
            native_config_path=native_config_path,
            archive_parent=archive_parent,
            expected_vnext_root=expected_vnext_root,
            observation_seconds=observation_seconds,
            apply=apply,
        )
    finally:
        base._archive_tree = original_archive_tree


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Execute one-way local M9a legacy ingress freeze with Windows lock-safe archive capture"
    )
    parser.add_argument("--profile", action="append", type=base._parse_profile_spec, required=True)
    parser.add_argument("--native-config", required=True)
    parser.add_argument("--archive-parent", required=True)
    parser.add_argument("--vnext-runtime-root", required=True)
    parser.add_argument("--observation-seconds", type=float, default=10.0)
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = execute_freeze_lock_safe(
            profiles=tuple(args.profile),
            native_config_path=Path(args.native_config),
            archive_parent=Path(args.archive_parent),
            expected_vnext_root=Path(args.vnext_runtime_root),
            observation_seconds=args.observation_seconds,
            apply=args.apply,
        )
    except Exception as exc:
        import sys

        sys.stderr.write(f"M9a lock-safe freeze failed: {type(exc).__name__}: {exc}\n")
        return 2

    import json
    import sys

    sys.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return 0


__all__ = [
    "LockSnapshot",
    "execute_freeze_lock_safe",
    "main",
]

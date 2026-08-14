from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import Any

from .git_object_reader import GitObjectReader
from .protocol import BridgeError, path_matches, validate_repo_relative_path
from .mirror_sync import MirrorSynchronizer
from .workspace_manager import Git, changed_paths


_MAX_TRACKED_PATHS = 2_000
_MAX_SNAPSHOT_FILES = 80
_MAX_SNAPSHOT_BYTES = 256 * 1024
_MAX_FILE_BYTES = 64 * 1024
_MAX_SYMBOLS = 500
_MAX_STATUS_PATHS = 200
_MAX_RECEIPTS_TO_INSPECT = 200
_TEXT_SUFFIXES = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".css",
        ".go",
        ".h",
        ".hpp",
        ".html",
        ".java",
        ".js",
        ".json",
        ".jsx",
        ".md",
        ".mjs",
        ".py",
        ".rb",
        ".rs",
        ".sh",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)
_TEXT_BASENAMES = frozenset(
    {
        ".editorconfig",
        ".gitignore",
        "Dockerfile",
        "Makefile",
        "Procfile",
    }
)
_SYMBOL_PATTERNS = (
    re.compile(r"^\s*(?:async\s+)?def\s+[A-Za-z_][A-Za-z0-9_]*\s*\("),
    re.compile(r"^\s*class\s+[A-Za-z_][A-Za-z0-9_]*\b"),
    re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+[A-Za-z_$][A-Za-z0-9_$]*\s*\("),
    re.compile(
        r"^\s*(?:export\s+)?(?:const|let|var)\s+[A-Za-z_$][A-Za-z0-9_$]*\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][A-Za-z0-9_$]*)\s*=>"
    ),
    re.compile(r"^\s*(?:public\s+|private\s+|protected\s+)?(?:static\s+)?(?:class|interface|enum)\s+[A-Za-z_][A-Za-z0-9_]*\b"),
)


class WorkspaceContextBuilder:
    """Build a bounded, read-only snapshot without disclosing local absolute paths."""

    _cache_lock = threading.RLock()
    _stable_cache: "OrderedDict[tuple[str, str, tuple[str, ...]], dict[str, Any]]" = OrderedDict()
    _cache_limit = 8

    def __init__(self, config: Any) -> None:
        self.config = config
        self.root = Path(config.fixture_repo_path).expanduser().resolve(strict=True)
        self.git = Git(self.root)

    def build(self) -> dict[str, Any]:
        if not self.root.joinpath(".git").exists():
            raise BridgeError("invalid_fixture_repo", "Configured workspace is not a Git checkout")

        status_text = self.git.run(["status", "--porcelain=v1"]).stdout
        all_changes = changed_paths(status_text)
        allowed_changes = [
            value
            for value in all_changes
            if path_matches(value, self.config.allowed_paths)
        ]
        outside_scope_count = len(all_changes) - len(allowed_changes)
        status_truncated = len(allowed_changes) > _MAX_STATUS_PATHS
        source_changes = allowed_changes[:_MAX_STATUS_PATHS]

        head = self.git.run(["rev-parse", "HEAD"]).stdout.strip().lower()
        cache_key = (str(self.root), head, tuple(self.config.allowed_paths))
        stable: dict[str, Any] | None = None
        if not allowed_changes:
            with self._cache_lock:
                cached = self._stable_cache.get(cache_key)
                if cached is not None:
                    self._stable_cache.move_to_end(cache_key)
                    stable = deepcopy(cached)
        if stable is None:
            # Model-facing source context is always reconstructed from the
            # committed Git tree. A mutable checkout may be CRLF-normalized
            # (or otherwise dirty) even when its canonical blob is LF; using
            # those physical bytes would produce a preimage digest that the
            # exact RepoView/Candidate path must correctly reject.
            stable = self._build_git_snapshot(head)
            if not allowed_changes:
                with self._cache_lock:
                    self._stable_cache[cache_key] = deepcopy(stable)
                    self._stable_cache.move_to_end(cache_key)
                    while len(self._stable_cache) > self._cache_limit:
                        self._stable_cache.popitem(last=False)

        return {
            "source_clean": not status_text.strip(),
            "controlled_clean": not allowed_changes,
            "source_changes": source_changes,
            "source_changes_truncated": status_truncated,
            "source_changes_outside_scope": outside_scope_count,
            **stable,
            "latest_promotion": self._latest_promotion(),
            "mirror_sync": MirrorSynchronizer(self.config).read_status(),
            "capabilities": {
                "workspace_context": True,
                "open_read": True,
                "search_text": True,
                "inspect_bundle": True,
                "multi_file_patch": True,
                "automatic_continuation": True,
                "promotion_receipts": True,
                "automatic_mirror_sync": bool(getattr(self.config, "mirror_sync_enabled", False)),
            },
            "limits": {
                "tracked_paths": _MAX_TRACKED_PATHS,
                "snapshot_files": _MAX_SNAPSHOT_FILES,
                "snapshot_bytes": _MAX_SNAPSHOT_BYTES,
                "file_bytes": _MAX_FILE_BYTES,
                "symbols": _MAX_SYMBOLS,
            },
        }

    def build_summary(self) -> dict[str, Any]:
        """Return action-safety metadata without indexing repository contents."""
        if not self.root.joinpath(".git").exists():
            raise BridgeError("invalid_fixture_repo", "Configured workspace is not a Git checkout")

        status_text = self.git.run(["status", "--porcelain=v1"]).stdout
        all_changes = changed_paths(status_text)
        allowed_changes = [
            value for value in all_changes if path_matches(value, self.config.allowed_paths)
        ]
        return {
            "source_clean": not status_text.strip(),
            "controlled_clean": not allowed_changes,
            "source_changes": allowed_changes[:_MAX_STATUS_PATHS],
            "source_changes_truncated": len(allowed_changes) > _MAX_STATUS_PATHS,
            "source_changes_outside_scope": len(all_changes) - len(allowed_changes),
            "symbols": [],
            "symbols_truncated": False,
            "latest_promotion": self._latest_promotion(),
            "capabilities": {
                "workspace_context": True,
                "open_read": True,
                "search_text": True,
                "inspect_bundle": True,
                "multi_file_patch": True,
                "automatic_continuation": True,
                "promotion_receipts": True,
                "automatic_mirror_sync": bool(
                    getattr(self.config, "mirror_sync_enabled", False)
                ),
            },
        }

    def _build_stable_snapshot(self) -> dict[str, Any]:
        tracked, tracked_truncated = self._tracked_paths()

        snapshots: list[dict[str, Any]] = []
        symbols: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        snapshot_bytes = 0
        snapshot_truncated = False

        for relative in tracked:
            if len(symbols) >= _MAX_SYMBOLS and len(snapshots) >= _MAX_SNAPSHOT_FILES:
                snapshot_truncated = True
                break
            path = self._safe_path(relative)
            if path.is_symlink() or not path.is_file():
                skipped.append({"path": relative, "reason": "not_regular_file"})
                continue
            if not self._looks_textual(path):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                skipped.append({"path": relative, "reason": "stat_failed"})
                continue
            if size > _MAX_FILE_BYTES:
                skipped.append({"path": relative, "reason": "file_too_large"})
                continue
            try:
                data = path.read_bytes()
                text = data.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                skipped.append({"path": relative, "reason": "not_utf8"})
                continue
            except OSError:
                skipped.append({"path": relative, "reason": "read_failed"})
                continue

            if len(symbols) < _MAX_SYMBOLS:
                symbols.extend(self._symbols(relative, text, _MAX_SYMBOLS - len(symbols)))

            if len(snapshots) >= _MAX_SNAPSHOT_FILES:
                snapshot_truncated = True
                continue
            if snapshot_bytes + len(data) > _MAX_SNAPSHOT_BYTES:
                snapshot_truncated = True
                continue
            snapshots.append(
                {
                    "path": relative,
                    "bytes": len(data),
                    "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
                    "content": text,
                }
            )
            snapshot_bytes += len(data)

        return {
            "tracked_paths": tracked,
            "tracked_paths_truncated": tracked_truncated,
            "snapshot_files": snapshots,
            "snapshot_bytes": snapshot_bytes,
            "snapshot_truncated": snapshot_truncated,
            "symbols": symbols[:_MAX_SYMBOLS],
            "symbols_truncated": len(symbols) >= _MAX_SYMBOLS,
            "skipped_files": skipped[:100],
        }

    def _build_git_snapshot(self, head: str) -> dict[str, Any]:
        reader = GitObjectReader(self.root)
        entries = [
            entry for entry in reader.list_tree(head)
            if path_matches(entry.path, self.config.allowed_paths)
        ]
        tracked_truncated = len(entries) > _MAX_TRACKED_PATHS
        entries = entries[:_MAX_TRACKED_PATHS]
        tracked = [entry.path for entry in entries]
        snapshots: list[dict[str, Any]] = []
        symbols: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        snapshot_bytes = 0
        snapshot_truncated = False

        candidates = []
        for entry in entries:
            if entry.mode == "120000" or entry.object_type != "blob":
                skipped.append({"path": entry.path, "reason": "not_regular_file"})
                continue
            if not self._looks_textual(Path(entry.path)):
                continue
            if entry.size_bytes > _MAX_FILE_BYTES:
                skipped.append({"path": entry.path, "reason": "file_too_large"})
                continue
            candidates.append(entry)

        for offset in range(0, len(candidates), 64):
            if len(symbols) >= _MAX_SYMBOLS and len(snapshots) >= _MAX_SNAPSHOT_FILES:
                snapshot_truncated = True
                break
            chunk = candidates[offset : offset + 64]
            blobs = reader.read_blobs(tuple(dict.fromkeys(item.object_sha for item in chunk)))
            for entry in chunk:
                data = blobs[entry.object_sha]
                try:
                    text = data.decode("utf-8", errors="strict")
                except UnicodeDecodeError:
                    skipped.append({"path": entry.path, "reason": "not_utf8"})
                    continue
                if len(symbols) < _MAX_SYMBOLS:
                    symbols.extend(self._symbols(entry.path, text, _MAX_SYMBOLS - len(symbols)))
                if len(snapshots) >= _MAX_SNAPSHOT_FILES:
                    snapshot_truncated = True
                    continue
                if snapshot_bytes + len(data) > _MAX_SNAPSHOT_BYTES:
                    snapshot_truncated = True
                    continue
                snapshots.append(
                    {
                        "path": entry.path,
                        "bytes": len(data),
                        "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
                        "content": text,
                    }
                )
                snapshot_bytes += len(data)

        return {
            "tracked_paths": tracked,
            "tracked_paths_truncated": tracked_truncated,
            "snapshot_files": snapshots,
            "snapshot_bytes": snapshot_bytes,
            "snapshot_truncated": snapshot_truncated,
            "snapshot_source": "git_blobs",
            "symbols": symbols[:_MAX_SYMBOLS],
            "symbols_truncated": len(symbols) >= _MAX_SYMBOLS,
            "skipped_files": skipped[:100],
        }

    def _tracked_paths(self) -> tuple[list[str], bool]:
        raw = self.git.run(["ls-files", "-z"]).stdout
        values: list[str] = []
        for item in raw.split("\0"):
            if not item:
                continue
            normalized = validate_repo_relative_path(item.replace("\\", "/"))
            if path_matches(normalized, self.config.allowed_paths):
                values.append(normalized)
        values = sorted(set(values))
        truncated = len(values) > _MAX_TRACKED_PATHS
        return values[:_MAX_TRACKED_PATHS], truncated

    def _safe_path(self, relative: str) -> Path:
        normalized = validate_repo_relative_path(relative)
        if not path_matches(normalized, self.config.allowed_paths):
            raise BridgeError("policy_denied", f"Path is not allowed by local policy: {normalized}")
        candidate = self.root.joinpath(*PurePosixPath(normalized).parts)
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise BridgeError("unsafe_path", f"Workspace path escaped configured root: {normalized}") from exc
        return resolved

    def _latest_promotion(self) -> dict[str, Any] | None:
        root = Path(self.config.runtime_dir).expanduser().resolve(strict=False) / "promotions"
        if not root.exists() or root.is_symlink() or not root.is_dir():
            return None
        candidates = sorted(
            (path for path in root.glob("*.json") if path.is_file() and not path.is_symlink()),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )[:_MAX_RECEIPTS_TO_INSPECT]
        for path in candidates:
            try:
                raw = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError, UnicodeError):
                continue
            if not isinstance(raw, dict) or raw.get("schema") != "bdb-workspace-promotion-v1":
                continue
            changed = raw.get("changed_files")
            hashes = raw.get("file_sha256")
            if (
                raw.get("status") != "promoted"
                or not isinstance(raw.get("command_id"), str)
                or not isinstance(raw.get("source_commit"), str)
                or not isinstance(raw.get("promoted_at"), str)
                or not isinstance(changed, list)
                or not all(isinstance(value, str) for value in changed)
                or not isinstance(hashes, dict)
            ):
                continue
            allowed_changed = [
                value
                for value in changed
                if path_matches(value, self.config.allowed_paths)
            ]
            if allowed_changed != changed:
                continue
            result = {
                "status": "promoted",
                "command_id": raw["command_id"],
                "source_commit": raw["source_commit"],
                "changed_files": changed,
                "file_sha256": hashes,
                "promoted_at": raw["promoted_at"],
            }
            mirror_sync = raw.get("mirror_sync")
            if isinstance(mirror_sync, dict):
                result["mirror_sync"] = mirror_sync
            return result
        return None

    @staticmethod
    def _looks_textual(path: Path) -> bool:
        return path.name in _TEXT_BASENAMES or path.suffix.lower() in _TEXT_SUFFIXES

    @staticmethod
    def symbols_from_text(
        relative: str,
        text: str,
        remaining: int,
        *,
        start_line: int = 1,
    ) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        if remaining <= 0:
            return found
        for number, line in enumerate(text.splitlines(), start=start_line):
            if any(pattern.search(line) for pattern in _SYMBOL_PATTERNS):
                found.append({"path": relative, "line": number, "text": line.strip()[:300]})
                if len(found) >= remaining:
                    break
        return found

    _symbols = symbols_from_text

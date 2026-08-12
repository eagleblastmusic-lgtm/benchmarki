"""Bounded, read-only code-fact providers for the N5 experiment.

Providers analyze an exact :class:`CommittedRepoView` and return facts that
are useful to Engineering Intelligence.  A fact is source-bound evidence of
what a provider observed; it is never repository authority.  The module has
no writer, cache, daemon, lifecycle dependency, or legacy import.
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import tempfile
import threading
import urllib.parse
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from bdb_shared.evidence import canonical_json_bytes, semantic_digest
from bdb_vnext.content_store import RepoViewBinding
from bdb_vnext.engineering_intelligence import (
    CoverageBinding,
    IntentBasis,
    Omission,
    RepositoryUnderstandingView,
    UnderstandingClaim,
    Unknown,
)
from bdb_vnext.repo_view import CommittedRepoView, RepoTreeEntry, RepoViewError


CODE_FACT_SCHEMA = "bdb-vnext-code-fact-v1"
PROVIDER_CONTRACT = "bdb-vnext-code-fact-provider-v1"
TREE_SITTER_PROVIDER_ID = "bdb-vnext.provider.tree-sitter-python"
TREE_SITTER_PROVIDER_VERSION = "tree-sitter-python-0.23.6/tree-sitter-0.24.0"
LSP_PROVIDER_ID = "bdb-vnext.provider.lsp"
LSP_PROVIDER_VERSION = "lsp-client-v1"
FALLBACK_PROVIDER_ID = "bdb-vnext.provider.lexical-fallback"
FALLBACK_PROVIDER_VERSION = "fallback-python-v1"
PROJECTION_PROVIDER_ID = "bdb-vnext.provider-fact-projection"
PROJECTION_PROVIDER_VERSION = "n5-projection-v1"
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+:-]{0,127}$")
_PYTHON_SUFFIXES = {".py", ".pyi"}
_MAX_FACTS = 10_000
_MAX_SOURCE_BYTES = 8 * 1024 * 1024
_MAX_MATERIALIZED_FILES = 2_000
_MAX_MATERIALIZED_BYTES = 64 * 1024 * 1024


class CodeIntelligenceError(ValueError):
    """Typed fail-closed provider error."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


class ProviderUnavailableError(CodeIntelligenceError):
    """Raised when an optional provider cannot be executed."""


def _text(value: object, *, field: str, max_length: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length or "\x00" in value:
        raise CodeIntelligenceError("malformed_fact", f"{field} must be bounded text")
    return value


def _digest(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise CodeIntelligenceError("malformed_fact", f"{field} must be a sha256 digest")
    return value


def _identifier(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise CodeIntelligenceError("malformed_fact", f"{field} is not a bounded identifier")
    return value


def _path(value: object) -> str:
    path = _text(value, field="path", max_length=4096).replace("\\", "/")
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise CodeIntelligenceError("unsafe_path", "provider paths must be relative")
    return str(parsed)


def _range_value(value: object, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > 10_000_000:
        raise CodeIntelligenceError("malformed_fact", f"{field} must be a non-negative bounded integer")
    return value


@dataclass(frozen=True)
class CodeFact:
    """One exact-source-bound mechanical observation."""

    fact_id: str
    repo_view: RepoViewBinding
    provider_id: str
    provider_version: str
    configuration_digest: str
    language: str
    coverage: tuple[str, ...]
    path: str
    source_object_id: str
    kind: str
    name: str
    start_line: int
    start_column: int
    end_line: int
    end_column: int
    details: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.repo_view, RepoViewBinding):
            raise CodeIntelligenceError("malformed_fact", "fact requires RepoViewBinding")
        _identifier(self.provider_id, field="provider_id")
        _identifier(self.provider_version, field="provider_version")
        _digest(self.configuration_digest, field="configuration_digest")
        _identifier(self.language, field="language")
        if not self.coverage or any(not isinstance(item, str) or not item for item in self.coverage):
            raise CodeIntelligenceError("malformed_fact", "fact coverage must be non-empty")
        _path(self.path)
        _text(self.source_object_id, field="source_object_id", max_length=128)
        _identifier(self.kind, field="kind")
        _text(self.name, field="name", max_length=1024)
        for field, value in (
            ("start_line", self.start_line),
            ("start_column", self.start_column),
            ("end_line", self.end_line),
            ("end_column", self.end_column),
        ):
            _range_value(value, field=field)
        if not isinstance(self.details, Mapping):
            raise CodeIntelligenceError("malformed_fact", "fact details must be an object")
        if self.fact_id != semantic_digest(self._identity_payload()):
            raise CodeIntelligenceError("fact_integrity_failure", "fact_id does not match its identity")

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema": CODE_FACT_SCHEMA,
            "repo_view": self.repo_view.as_dict(),
            "provider_id": self.provider_id,
            "provider_version": self.provider_version,
            "configuration_digest": self.configuration_digest,
            "language": self.language,
            "coverage": list(self.coverage),
            "path": self.path,
            "source_object_id": self.source_object_id,
            "kind": self.kind,
            "name": self.name,
            "range": {
                "start_line": self.start_line,
                "start_column": self.start_column,
                "end_line": self.end_line,
                "end_column": self.end_column,
            },
            "details": dict(self.details),
        }

    def as_dict(self) -> dict[str, Any]:
        return {"schema": CODE_FACT_SCHEMA, "fact_id": self.fact_id, **self._identity_payload()}

    to_dict = as_dict

    @classmethod
    def create(
        cls,
        repo_view: CommittedRepoView | RepoViewBinding,
        *,
        provider_id: str,
        provider_version: str,
        configuration_digest: str,
        language: str,
        coverage: Sequence[str],
        path: str,
        source_object_id: str,
        kind: str,
        name: str,
        start_line: int,
        start_column: int,
        end_line: int,
        end_column: int,
        details: Mapping[str, Any] | None = None,
    ) -> "CodeFact":
        binding = RepoViewBinding.from_view(repo_view) if isinstance(repo_view, CommittedRepoView) else repo_view
        if not isinstance(binding, RepoViewBinding):
            raise CodeIntelligenceError("repo_view_required", "provider facts require an exact RepoView")
        identity = {
            "schema": CODE_FACT_SCHEMA,
            "repo_view": binding.as_dict(),
            "provider_id": provider_id,
            "provider_version": provider_version,
            "configuration_digest": configuration_digest,
            "language": language,
            "coverage": list(coverage),
            "path": _path(path),
            "source_object_id": source_object_id,
            "kind": kind,
            "name": name,
            "range": {
                "start_line": start_line,
                "start_column": start_column,
                "end_line": end_line,
                "end_column": end_column,
            },
            "details": dict(details or {}),
        }
        return cls(
            semantic_digest(identity),
            binding,
            provider_id,
            provider_version,
            configuration_digest,
            language,
            tuple(coverage),
            identity["path"],
            source_object_id,
            kind,
            name,
            start_line,
            start_column,
            end_line,
            end_column,
            dict(details or {}),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CodeFact":
        if not isinstance(value, Mapping) or set(value) != {
            "schema", "fact_id", "repo_view", "provider_id", "provider_version",
            "configuration_digest", "language", "coverage", "path", "source_object_id",
            "kind", "name", "range", "details",
        }:
            raise CodeIntelligenceError("malformed_fact", "CodeFact has an unexpected field set")
        if value["schema"] != CODE_FACT_SCHEMA or not isinstance(value["range"], Mapping):
            raise CodeIntelligenceError("schema_mismatch", "unsupported CodeFact schema")
        rng = value["range"]
        if set(rng) != {"start_line", "start_column", "end_line", "end_column"}:
            raise CodeIntelligenceError("malformed_fact", "CodeFact range has an unexpected field set")
        return cls(
            value["fact_id"],
            RepoViewBinding.from_mapping(value["repo_view"]),
            value["provider_id"],
            value["provider_version"],
            value["configuration_digest"],
            value["language"],
            tuple(value["coverage"]),
            value["path"],
            value["source_object_id"],
            value["kind"],
            value["name"],
            rng["start_line"],
            rng["start_column"],
            rng["end_line"],
            rng["end_column"],
            value["details"],
        )


@dataclass(frozen=True)
class ProviderResult:
    """Facts plus explicit coverage and gaps for one exact provider call."""

    repo_view: RepoViewBinding
    provider_id: str
    provider_version: str
    configuration_digest: str
    language: str
    source_paths: tuple[str, ...]
    facts: tuple[CodeFact, ...]
    covered_dimensions: tuple[str, ...]
    gaps: tuple[str, ...]
    materialization_digest: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.repo_view, RepoViewBinding):
            raise CodeIntelligenceError("malformed_provider_result", "result requires RepoViewBinding")
        _identifier(self.provider_id, field="provider_id")
        _identifier(self.provider_version, field="provider_version")
        _digest(self.configuration_digest, field="configuration_digest")
        for path in self.source_paths:
            _path(path)
        if len(self.facts) > _MAX_FACTS:
            raise CodeIntelligenceError("fact_limit_exceeded", "provider returned too many facts")
        for fact in self.facts:
            if fact.repo_view != self.repo_view:
                raise CodeIntelligenceError("stale_provider_result", "a fact is bound to a different RepoView")
            if fact.provider_id != self.provider_id or fact.configuration_digest != self.configuration_digest:
                raise CodeIntelligenceError("provider_identity_mismatch", "fact provider identity differs from result")
        if self.materialization_digest is not None:
            _digest(self.materialization_digest, field="materialization_digest")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": PROVIDER_CONTRACT,
            "repo_view": self.repo_view.as_dict(),
            "provider_id": self.provider_id,
            "provider_version": self.provider_version,
            "configuration_digest": self.configuration_digest,
            "language": self.language,
            "source_paths": list(self.source_paths),
            "facts": [fact.as_dict() for fact in self.facts],
            "covered_dimensions": list(self.covered_dimensions),
            "gaps": list(self.gaps),
            "materialization_digest": self.materialization_digest,
        }

    def validate_against(self, view: CommittedRepoView) -> None:
        """Re-read exact Git objects and reject stale/foreign provider facts."""

        if not isinstance(view, CommittedRepoView) or RepoViewBinding.from_view(view) != self.repo_view:
            raise CodeIntelligenceError("stale_provider_result", "provider result is not bound to the requested RepoView")
        for fact in self.facts:
            try:
                entry = view.entry(fact.path)
            except RepoViewError as exc:
                raise CodeIntelligenceError("provider_path_missing", "provider fact path is absent from RepoView") from exc
            if entry.object_oid != fact.source_object_id:
                raise CodeIntelligenceError("source_digest_mismatch", "provider fact source object changed")


class CodeFactProvider(Protocol):
    """Minimal read-only provider port; no lifecycle or source authority methods."""

    provider_id: str
    provider_version: str

    def analyze(self, view: CommittedRepoView, paths: Sequence[str]) -> ProviderResult:
        ...


def _configuration_digest(provider_id: str, provider_version: str, config: Mapping[str, Any]) -> str:
    return semantic_digest({"provider_id": provider_id, "provider_version": provider_version, "configuration": dict(config)})


def _python_paths(view: CommittedRepoView, paths: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(view, CommittedRepoView):
        raise CodeIntelligenceError("repo_view_required", "provider analysis requires CommittedRepoView")
    normalized: list[str] = []
    for path in paths:
        normalized_path = _path(path)
        try:
            entry = view.entry(normalized_path)
        except RepoViewError as exc:
            raise CodeIntelligenceError(
                "provider_path_missing",
                "provider path is absent from the exact RepoView",
            ) from exc
        if not entry.is_regular_file:
            raise CodeIntelligenceError("unsupported_path", "provider analysis requires regular files")
        if Path(normalized_path).suffix.lower() not in _PYTHON_SUFFIXES:
            raise CodeIntelligenceError("unsupported_language", "only Python paths are supported by this provider")
        if normalized_path not in normalized:
            normalized.append(normalized_path)
    if not normalized:
        raise CodeIntelligenceError("no_source_paths", "at least one source path is required")
    return tuple(normalized)


class FallbackCodeFactProvider:
    """Lower-coverage regex fallback, intentionally removable and non-authoritative."""

    provider_id = FALLBACK_PROVIDER_ID
    provider_version = FALLBACK_PROVIDER_VERSION

    def analyze(self, view: CommittedRepoView, paths: Sequence[str]) -> ProviderResult:
        source_paths = _python_paths(view, paths)
        config_digest = _configuration_digest(self.provider_id, self.provider_version, {"mode": "regex"})
        binding = RepoViewBinding.from_view(view)
        facts: list[CodeFact] = []
        gaps: list[str] = ["semantic_definition_resolution"]
        for path in source_paths:
            entry = view.entry(path)
            text = view.read_text(path)
            for match in re.finditer(r"(?m)^(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(", text):
                line = text.count("\n", 0, match.start())
                column = match.start() - (text.rfind("\n", 0, match.start()) + 1)
                facts.append(CodeFact.create(
                    binding, provider_id=self.provider_id, provider_version=self.provider_version,
                    configuration_digest=config_digest, language="python", coverage=("definitions",),
                    path=path, source_object_id=entry.object_oid, kind="definition", name=match.group(1),
                    start_line=line, start_column=column, end_line=line, end_column=column + len(match.group(1)),
                    details={"definition_kind": "function", "resolution": "syntactic"},
                ))
            for match in re.finditer(r"(?m)^\s*(?:from\s+([\w.]+)\s+)?import\s+([^#\n]+)", text):
                name = (match.group(1) or match.group(2)).strip()
                line = text.count("\n", 0, match.start())
                facts.append(CodeFact.create(
                    binding, provider_id=self.provider_id, provider_version=self.provider_version,
                    configuration_digest=config_digest, language="python", coverage=("imports",),
                    path=path, source_object_id=entry.object_oid, kind="import", name=name,
                    start_line=line, start_column=0, end_line=line, end_column=len(match.group(0)),
                    details={"resolution": "syntactic"},
                ))
        covered = tuple(sorted({dimension for fact in facts for dimension in fact.coverage}))
        return ProviderResult(binding, self.provider_id, self.provider_version, config_digest, "python", source_paths, tuple(facts), covered, tuple(gaps))


class TreeSitterPythonProvider:
    """Exact Python syntax provider using maintained Tree-sitter bindings."""

    provider_id = TREE_SITTER_PROVIDER_ID
    provider_version = TREE_SITTER_PROVIDER_VERSION

    def _parser(self) -> Any:
        try:
            from tree_sitter import Language, Parser
            from tree_sitter_python import language
        except (ImportError, ModuleNotFoundError) as exc:
            raise ProviderUnavailableError("dependency_unavailable", "Tree-sitter Python bindings are unavailable") from exc
        try:
            return Parser(Language(language()))
        except Exception as exc:  # pragma: no cover - binding-specific failure
            raise ProviderUnavailableError("grammar_unavailable", "Tree-sitter Python grammar cannot be loaded") from exc

    @staticmethod
    def _node_text(source: bytes, node: Any) -> str:
        return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")

    def analyze(self, view: CommittedRepoView, paths: Sequence[str]) -> ProviderResult:
        source_paths = _python_paths(view, paths)
        parser = self._parser()
        config_digest = _configuration_digest(self.provider_id, self.provider_version, {"grammar": "python", "mode": "cst"})
        binding = RepoViewBinding.from_view(view)
        facts: list[CodeFact] = []
        gaps: list[str] = ["semantic_definition_resolution", "runtime_dynamic_dispatch"]
        for path in source_paths:
            entry = view.entry(path)
            source = view.read_bytes(path, max_bytes=_MAX_SOURCE_BYTES)
            tree = parser.parse(source)
            root = tree.root_node
            if root.has_error:
                gaps.append(f"parse_error:{path}")
            stack = [root]
            while stack:
                node = stack.pop()
                if node.type in {"function_definition", "class_definition"}:
                    name_node = node.child_by_field_name("name")
                    if name_node is not None:
                        kind = "class_definition" if node.type == "class_definition" else "definition"
                        facts.append(CodeFact.create(
                            binding, provider_id=self.provider_id, provider_version=self.provider_version,
                            configuration_digest=config_digest, language="python", coverage=("syntax_structure", "definitions"),
                            path=path, source_object_id=entry.object_oid, kind=kind,
                            name=self._node_text(source, name_node), start_line=node.start_point[0],
                            start_column=node.start_point[1], end_line=node.end_point[0], end_column=node.end_point[1],
                            details={"resolution": "syntactic", "node_type": node.type},
                        ))
                elif node.type in {"import_statement", "import_from_statement"}:
                    facts.append(CodeFact.create(
                        binding, provider_id=self.provider_id, provider_version=self.provider_version,
                        configuration_digest=config_digest, language="python", coverage=("syntax_structure", "imports"),
                        path=path, source_object_id=entry.object_oid, kind="import", name=self._node_text(source, node),
                        start_line=node.start_point[0], start_column=node.start_point[1], end_line=node.end_point[0],
                        end_column=node.end_point[1], details={"resolution": "syntactic", "node_type": node.type},
                    ))
                elif node.type == "call":
                    function_node = node.child_by_field_name("function")
                    if function_node is not None:
                        facts.append(CodeFact.create(
                            binding, provider_id=self.provider_id, provider_version=self.provider_version,
                            configuration_digest=config_digest, language="python", coverage=("references",),
                            path=path, source_object_id=entry.object_oid, kind="call", name=self._node_text(source, function_node),
                            start_line=node.start_point[0], start_column=node.start_point[1], end_line=node.end_point[0],
                            end_column=node.end_point[1], details={"resolution": "reference-shaped", "node_type": node.type},
                        ))
                stack.extend(reversed(node.children))
        if len(facts) > _MAX_FACTS:
            raise CodeIntelligenceError("fact_limit_exceeded", "Tree-sitter returned too many facts")
        covered = tuple(sorted({dimension for fact in facts for dimension in fact.coverage}))
        return ProviderResult(binding, self.provider_id, self.provider_version, config_digest, "python", source_paths, tuple(facts), covered, tuple(dict.fromkeys(gaps)))


def _file_uri(path: Path) -> str:
    return urllib.parse.urlunsplit(("file", "", urllib.parse.quote(str(path).replace("\\", "/"), safe="/:"), "", ""))


def _uri_path(uri: str) -> Path:
    parsed = urllib.parse.urlsplit(uri)
    if parsed.scheme != "file":
        raise CodeIntelligenceError("foreign_source", "LSP returned a non-file URI")
    return Path(urllib.parse.unquote(parsed.path.lstrip("/")) if os.name == "nt" else urllib.parse.unquote(parsed.path))


class _JsonRpcProcess:
    """Small stdio JSON-RPC transport used only for read-only LSP methods."""

    def __init__(self, command: Sequence[str], *, cwd: Path, timeout_seconds: float) -> None:
        if not command or any(not isinstance(item, str) or not item for item in command):
            raise ProviderUnavailableError("server_unavailable", "LSP command is empty")
        self.command = tuple(command)
        self.cwd = cwd
        self.timeout_seconds = timeout_seconds
        try:
            self.process = subprocess.Popen(
                list(self.command), cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, shell=False,
            )
        except (FileNotFoundError, OSError) as exc:
            raise ProviderUnavailableError("server_unavailable", "configured LSP executable is unavailable") from exc
        self._counter = 0

    def close(self) -> None:
        if self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                self.process.kill()
                try:
                    self.process.wait(timeout=1)
                except (OSError, subprocess.TimeoutExpired):
                    pass

    def _send(self, payload: Mapping[str, Any]) -> None:
        raw = canonical_json_bytes(payload)
        assert self.process.stdin is not None
        self.process.stdin.write(f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii") + raw)
        self.process.stdin.flush()

    def _read_message(self) -> Mapping[str, Any]:
        assert self.process.stdout is not None
        headers: dict[str, str] = {}
        while True:
            line = self.process.stdout.readline()
            if not line:
                raise CodeIntelligenceError("lsp_server_closed", "LSP server closed its output")
            decoded = line.decode("ascii", errors="replace").strip()
            if not decoded:
                break
            key, separator, value = decoded.partition(":")
            if separator:
                headers[key.lower()] = value.strip()
        try:
            length = int(headers["content-length"])
            payload = self.process.stdout.read(length)
            return json.loads(payload.decode("utf-8"))
        except (KeyError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise CodeIntelligenceError("lsp_protocol_error", "LSP response framing is invalid") from exc

    def request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        self._counter += 1
        request_id = self._counter
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)})
        result: queue.Queue[Mapping[str, Any] | BaseException] = queue.Queue(maxsize=1)

        def read() -> None:
            try:
                while True:
                    message = self._read_message()
                    if message.get("id") == request_id:
                        result.put(message)
                        return
            except BaseException as exc:  # propagated to caller
                result.put(exc)

        thread = threading.Thread(target=read, daemon=True)
        thread.start()
        try:
            response = result.get(timeout=self.timeout_seconds)
        except queue.Empty as exc:
            self.close()
            raise CodeIntelligenceError("lsp_timeout", f"LSP request timed out: {method}") from exc
        if isinstance(response, BaseException):
            raise response
        if "error" in response:
            raise CodeIntelligenceError("lsp_request_failed", f"LSP request failed: {method}", details={"error": response["error"]})
        return response

    def notify(self, method: str, params: Mapping[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": dict(params)})


def _materialize_view(view: CommittedRepoView) -> tuple[tempfile.TemporaryDirectory[str], Path, str]:
    try:
        entries = view.list_entries()
    except RepoViewError as exc:
        raise CodeIntelligenceError("repo_view_read_failed", "exact RepoView cannot be materialized") from exc
    regular = [entry for entry in entries if entry.is_regular_file]
    if len(regular) > _MAX_MATERIALIZED_FILES or sum(entry.size_bytes for entry in regular) > _MAX_MATERIALIZED_BYTES:
        raise CodeIntelligenceError("materialization_limit", "exact LSP workspace exceeds bounded limits")
    directory = tempfile.TemporaryDirectory(prefix="bdb-n5-lsp-")
    root = Path(directory.name).resolve(strict=True)
    records: list[dict[str, Any]] = []
    try:
        for entry in regular:
            target = root.joinpath(*PurePosixPath(entry.path).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(view.read_bytes(entry.path, max_bytes=_MAX_SOURCE_BYTES))
            records.append({"path": entry.path, "object_oid": entry.object_oid, "size": entry.size_bytes})
        digest = semantic_digest({"repo_view": view.to_dict(), "files": records})
        return directory, root, digest
    except Exception:
        directory.cleanup()
        raise


class LspCodeFactProvider:
    """Read-only JSON-RPC LSP adapter; no workspace edits are sent or accepted."""

    provider_id = LSP_PROVIDER_ID
    provider_version = LSP_PROVIDER_VERSION

    def __init__(self, command: Sequence[str], *, server_identity: str, timeout_seconds: float = 8.0) -> None:
        if not command:
            raise ProviderUnavailableError("server_unavailable", "an explicit LSP command is required")
        self.command = tuple(command)
        self.server_identity = _text(server_identity, field="server_identity")
        self.timeout_seconds = timeout_seconds

    def analyze(self, view: CommittedRepoView, paths: Sequence[str]) -> ProviderResult:
        source_paths = _python_paths(view, paths)
        directory, workspace, materialization_digest = _materialize_view(view)
        config_digest = _configuration_digest(
            self.provider_id,
            self.provider_version,
            {"server_identity": self.server_identity, "command": list(self.command), "capabilities": ["definition"]},
        )
        binding = RepoViewBinding.from_view(view)
        facts: list[CodeFact] = []
        gaps: list[str] = []
        try:
            rpc = _JsonRpcProcess(self.command, cwd=workspace, timeout_seconds=self.timeout_seconds)
            try:
                root_uri = _file_uri(workspace)
                rpc.request("initialize", {"processId": os.getpid(), "rootUri": root_uri, "capabilities": {"textDocument": {"definition": {}, "references": {}}}})
                rpc.notify("initialized", {})
                for path in source_paths:
                    text = view.read_text(path)
                    uri = _file_uri(workspace.joinpath(*PurePosixPath(path).parts))
                    rpc.notify("textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": "python", "version": 1, "text": text}})
                    positions: list[tuple[int, int]] = []
                    keywords = {"import", "from", "as", "def", "class", "return", "if", "else", "for", "in", "True", "False", "None"}
                    for match in re.finditer(r"(?m)\b[A-Za-z_]\w*\b", text):
                        token = match.group(0)
                        if token in keywords:
                            continue
                        line = text.count("\n", 0, match.start())
                        column = match.start() - (text.rfind("\n", 0, match.start()) + 1)
                        positions.append((line, column))
                        if len(positions) >= 32:
                            break
                    result: Any = None
                    for line, column in positions:
                        result = rpc.request(
                            "textDocument/definition",
                            {"textDocument": {"uri": uri}, "position": {"line": line, "character": column}},
                        ).get("result")
                        if result:
                            break
                    locations = result if isinstance(result, list) else ([result] if isinstance(result, Mapping) else [])
                    if not locations:
                        gaps.append(f"definition_unavailable:{path}")
                    for location in locations:
                        if not isinstance(location, Mapping) or not isinstance(location.get("uri"), str) or not isinstance(location.get("range"), Mapping):
                            gaps.append(f"malformed_location:{path}")
                            continue
                        target = _uri_path(location["uri"]).resolve(strict=False)
                        try:
                            relative = target.relative_to(workspace).as_posix()
                        except ValueError:
                            gaps.append("foreign_source")
                            continue
                        relative = _path(relative)
                        try:
                            entry = view.entry(relative)
                            target_text = view.read_text(relative, max_bytes=_MAX_SOURCE_BYTES)
                        except RepoViewError as exc:
                            gaps.append(f"stale_location:{relative}")
                            continue
                        range_value = location["range"]
                        start = range_value.get("start", {})
                        end = range_value.get("end", {})
                        if not isinstance(start, Mapping) or not isinstance(end, Mapping):
                            gaps.append(f"stale_location:{relative}")
                            continue
                        sl, sc = int(start.get("line", -1)), int(start.get("character", -1))
                        el, ec = int(end.get("line", -1)), int(end.get("character", -1))
                        if min(sl, sc, el, ec) < 0:
                            gaps.append(f"stale_location:{relative}")
                            continue
                        lines = target_text.splitlines()
                        if sl >= len(lines) or el >= len(lines) or sc > len(lines[sl]) or ec > len(lines[el]):
                            gaps.append(f"stale_location:{relative}")
                            continue
                        facts.append(CodeFact.create(
                            binding, provider_id=self.provider_id, provider_version=self.provider_version,
                            configuration_digest=config_digest, language="python", coverage=("definitions",),
                            path=relative, source_object_id=entry.object_oid, kind="definition", name=relative,
                            start_line=sl, start_column=sc, end_line=el, end_column=ec,
                            details={"resolution": "lsp", "server_identity": self.server_identity},
                        ))
            finally:
                rpc.close()
        except ProviderUnavailableError:
            raise
        except CodeIntelligenceError:
            raise
        except Exception as exc:
            raise CodeIntelligenceError("lsp_failure", "read-only LSP interaction failed") from exc
        finally:
            # A language server may briefly retain a materialized file on
            # Windows after termination.  Cleanup is best effort and never
            # turns an otherwise typed provider result into authority.
            try:
                directory.cleanup()
            except OSError:
                gaps.append("workspace_cleanup_unavailable")
        covered = tuple(sorted({dimension for fact in facts for dimension in fact.coverage}))
        return ProviderResult(binding, self.provider_id, self.provider_version, config_digest, "python", source_paths, tuple(facts), covered, tuple(dict.fromkeys(gaps)), materialization_digest)


def validate_provider_result(result: ProviderResult, view: CommittedRepoView) -> ProviderResult:
    result.validate_against(view)
    return result


def project_provider_facts(
    view: CommittedRepoView,
    intent_basis: IntentBasis,
    result: ProviderResult,
    *,
    requested_dimensions: Sequence[str],
    must_see_categories: Sequence[str] = (),
) -> RepositoryUnderstandingView:
    """Project facts into derived EI claims while preserving visible gaps."""

    validate_provider_result(result, view)
    binding = result.repo_view
    claims: list[UnderstandingClaim] = []
    coverage: list[CoverageBinding] = []
    dimension_claims: dict[str, list[str]] = {}
    for fact in result.facts:
        dimensions = tuple(fact.coverage)
        statement = f"{fact.kind} {fact.name} observed in {fact.path} by {fact.provider_id}"
        claim = UnderstandingClaim.create(
            binding,
            subject=f"{fact.path}:{fact.name}",
            dimension=dimensions[0],
            kind="INFERENCE",
            statement=statement,
            evidence_refs=[fact.fact_id],
            basis_refs=[fact.repo_view.view_id],
            producer_id=PROJECTION_PROVIDER_ID,
            producer_version=PROJECTION_PROVIDER_VERSION,
        )
        claims.append(claim)
        for dimension in dimensions:
            dimension_claims.setdefault(dimension, []).append(claim.claim_id)
    requested = tuple(requested_dimensions)
    covered = tuple(sorted(dimension for dimension in requested if dimension in dimension_claims))
    unknowns: list[Unknown] = []
    for dimension in requested:
        if dimension not in covered:
            unknowns.append(Unknown.create(
                binding,
                subject=dimension,
                dimension=dimension,
                reason="provider did not establish this dimension; coverage remains explicit",
            ))
    for dimension in covered:
        coverage.append(CoverageBinding.create(
            binding,
            target_kind="DIMENSION",
            target=dimension,
            supporting_claim_ids=dimension_claims[dimension],
        ))
    for gap in result.gaps:
        if gap.startswith("semantic_definition_resolution") and "definitions" in covered:
            unknowns.append(Unknown.create(
                binding,
                subject="semantic definition resolution",
                dimension="semantic_definition_resolution",
                reason="Tree-sitter facts are syntactic and do not prove semantic target resolution",
            ))
    return RepositoryUnderstandingView.create(
        intent_basis,
        view,
        claims=claims,
        requested_dimensions=requested,
        covered_dimensions=covered,
        must_see_categories=tuple(must_see_categories),
        covered_must_see=(),
        coverage_bindings=coverage,
        unknowns=unknowns,
        omissions=(),
        contradictions=(),
        producer_id=PROJECTION_PROVIDER_ID,
        producer_version=PROJECTION_PROVIDER_VERSION,
    )


def provider_status(*, tree_sitter: bool, lsp: bool) -> dict[str, Any]:
    """Build-only diagnostic status; absence lowers capability, never authority."""

    return {
        "schema": "bdb-vnext-code-intelligence-status-v1",
        "tree_sitter": "AVAILABLE" if tree_sitter else "UNAVAILABLE",
        "lsp": "AVAILABLE" if lsp else "UNAVAILABLE",
        "authority": "NONE",
        "writer_enabled": False,
        "cache_authority": False,
    }


__all__ = [
    "CODE_FACT_SCHEMA",
    "CodeFact",
    "CodeFactProvider",
    "CodeIntelligenceError",
    "FallbackCodeFactProvider",
    "FALLBACK_PROVIDER_ID",
    "LSP_PROVIDER_ID",
    "LspCodeFactProvider",
    "PROVIDER_CONTRACT",
    "ProviderResult",
    "ProviderUnavailableError",
    "TREE_SITTER_PROVIDER_ID",
    "TreeSitterPythonProvider",
    "project_provider_facts",
    "provider_status",
    "validate_provider_result",
]

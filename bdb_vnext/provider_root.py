"""Explicit, build-only BDB Next composition root.

The M1a manifest remains a read-only identity record.  This module adds the
small explicit object that target code receives when it needs a provider.  It
does not discover modules, install globals, create runtime state, or import
the frozen legacy generation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from bdb_shared.evidence import semantic_digest
from bdb_vnext import composition as _composition
from bdb_vnext.context_transport import BrowserTransportProvider, NativeTransportProvider
from bdb_vnext.repo_view import (
    DEFAULT_GIT_TIMEOUT_SECONDS,
    DEFAULT_MAX_BLOB_BYTES,
    CommittedRepoView,
    RepoViewQuery,
    RepositoryResource,
)


PROVIDER_ROOT_SCHEMA = "bdb-vnext-provider-root-v1"
PRODUCT_ID = "BDB Next 1.0"
PRODUCT_TOPOLOGY = "BDB LEGACY + BDB NEXT 1.0 — INDEPENDENT SIDE-BY-SIDE"
ROOT_GENERATION = _composition.GENERATION_ID
RUNTIME_STATE = "OFF"
WRITER_STATE = "OFF"
ACTIVATION_STATE = "OFF"

_KNOWN_PROVIDER_IDS = (
    _composition.COMPOSITION_PROVIDER_ID,
    _composition.CONTROL_PROVIDER_ID,
    _composition.ADMISSION_PROVIDER_ID,
    _composition.WORK_KERNEL_PROVIDER_ID,
    _composition.NATIVE_PROVIDER_ID,
    _composition.BROWSER_PROVIDER_ID,
    _composition.CONTROL_CENTER_PROVIDER_ID,
    _composition.REPO_VIEW_PROVIDER_ID,
)
_BOUND_PROVIDER_IDS = frozenset(
    {
        _composition.COMPOSITION_PROVIDER_ID,
        _composition.BROWSER_PROVIDER_ID,
        _composition.NATIVE_PROVIDER_ID,
        _composition.REPO_VIEW_PROVIDER_ID,
    }
)
_RESERVED_PROVIDER_IDS = frozenset(_KNOWN_PROVIDER_IDS) - _BOUND_PROVIDER_IDS
_PROVIDER_STATES = frozenset({"BOUND", "UNAVAILABLE", "RESERVED"})
_SHA40 = set("0123456789abcdef")


class ProviderRootError(ValueError):
    """Typed fail-closed error for explicit provider-root construction/use."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


@dataclass(frozen=True)
class ProviderBinding:
    """One explicit generation-qualified binding declaration."""

    provider_id: str
    generation: str
    component_id: str
    kind: str
    state: str
    implementation: object | None = field(default=None, repr=False, compare=False)
    provider_contract: str | None = None
    provider_contract_version: int | None = None
    implementation_identity: str | None = None
    implementation_module: str | None = None
    implementation_qualname: str | None = None
    implementation_revision: str | None = None

    def descriptor(self) -> dict[str, Any]:
        descriptor: dict[str, Any] = {
            "provider_id": self.provider_id,
            "generation": self.generation,
            "component_id": self.component_id,
            "kind": self.kind,
            "state": self.state,
            "implementation_bound": self.implementation is not None,
            "writer_enabled": False,
        }
        if self.provider_contract is not None:
            descriptor["provider_contract"] = self.provider_contract
        if self.provider_contract_version is not None:
            descriptor["provider_contract_version"] = self.provider_contract_version
        if self.implementation_identity is not None:
            descriptor["implementation_identity"] = self.implementation_identity
        if self.implementation_module is not None:
            descriptor["implementation_module"] = self.implementation_module
        if self.implementation_qualname is not None:
            descriptor["implementation_qualname"] = self.implementation_qualname
        if self.implementation_revision is not None:
            descriptor["implementation_revision"] = self.implementation_revision
        return descriptor


@dataclass(frozen=True)
class CompositionDiagnosticProvider:
    """Read-only projection of the accepted M1a identity/basis contract."""

    generation: str
    manifest_schema: str
    manifest_version: int
    manifest_digest: str
    source_branch: str
    source_commit: str

    def status(self) -> dict[str, Any]:
        return {
            "schema": self.manifest_schema,
            "manifest_version": self.manifest_version,
            "generation": self.generation,
            "semantic_digest": self.manifest_digest,
            "basis": {
                "source_branch": self.source_branch,
                "source_commit": self.source_commit,
            },
            "runtime_state": RUNTIME_STATE,
            "writer_state": WRITER_STATE,
            "activation_state": ACTIVATION_STATE,
        }


@dataclass(frozen=True)
class RepoViewProvider:
    """Explicit adapter over the accepted M2a RepoView API."""

    generation: str = ROOT_GENERATION

    def repository_resource(
        self,
        repo_path: str | Path,
        *,
        repository_id: str | None = None,
        timeout_seconds: int = DEFAULT_GIT_TIMEOUT_SECONDS,
    ) -> RepositoryResource:
        return RepositoryResource.from_path(
            repo_path,
            repository_id=repository_id,
            timeout_seconds=timeout_seconds,
        )

    from_path = repository_resource
    resource = repository_resource

    def resolve_committed(
        self,
        resource_or_path: RepositoryResource | str | Path,
        ref: str,
        *,
        repository_id: str | None = None,
        observed_at: str | None = None,
        timeout_seconds: int = DEFAULT_GIT_TIMEOUT_SECONDS,
    ) -> CommittedRepoView:
        resource = (
            resource_or_path
            if isinstance(resource_or_path, RepositoryResource)
            else self.repository_resource(
                resource_or_path,
                repository_id=repository_id,
                timeout_seconds=timeout_seconds,
            )
        )
        return resource.resolve_committed(
            ref,
            observed_at=observed_at,
            timeout_seconds=timeout_seconds,
        )

    resolve = resolve_committed
    committed = resolve_committed

    def query(self, resource: RepositoryResource, view: CommittedRepoView) -> RepoViewQuery:
        return resource.query(view)

    def read_text(
        self,
        resource: RepositoryResource,
        view: CommittedRepoView,
        path: str,
        *,
        encoding: str = "utf-8",
        max_bytes: int = DEFAULT_MAX_BLOB_BYTES,
    ) -> str:
        return self.query(resource, view).read_text(path, encoding=encoding, max_bytes=max_bytes)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _manifest_provider_documents(manifest: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    composition = manifest.get("composition")
    if not isinstance(composition, Mapping):
        raise ProviderRootError("manifest_identity_mismatch", "M1a composition declaration is missing")
    providers = composition.get("providers")
    if not isinstance(providers, list):
        raise ProviderRootError("manifest_identity_mismatch", "M1a provider declaration is malformed")
    documents: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for item in providers:
        if not isinstance(item, Mapping):
            raise ProviderRootError("manifest_identity_mismatch", "M1a provider declaration is malformed")
        provider_id = item.get("provider_id")
        if not isinstance(provider_id, str):
            raise ProviderRootError("manifest_identity_mismatch", "M1a provider ID is malformed")
        if provider_id in seen:
            raise ProviderRootError("duplicate_provider_id", "M1a provider IDs are not unique")
        seen.add(provider_id)
        documents.append(item)
    if tuple(item.get("provider_id") for item in documents) != _KNOWN_PROVIDER_IDS:
        raise ProviderRootError("manifest_identity_mismatch", "M1a provider namespace differs")
    return tuple(documents)


def _validated_manifest(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(manifest, Mapping):
        raise ProviderRootError("malformed_manifest", "composition manifest must be an object")
    try:
        _composition.validate_vnext_composition_manifest(manifest)
    except (_composition.VNextCompositionError, TypeError, ValueError) as exc:
        raise ProviderRootError(
            "manifest_identity_mismatch",
            "composition manifest does not match the accepted M1a identity contract",
        ) from exc
    _manifest_provider_documents(manifest)
    generation = manifest.get("generation")
    basis = manifest.get("basis")
    if not isinstance(generation, Mapping) or not isinstance(basis, Mapping):
        raise ProviderRootError("manifest_identity_mismatch", "M1a generation/basis declaration is malformed")
    source_commit = basis.get("source_commit")
    if (
        generation.get("generation_id") != ROOT_GENERATION
        or generation.get("mode") != "build_only"
        or generation.get("writer_enabled") is not False
        or basis.get("source_branch") != _composition.SOURCE_BRANCH
        or not isinstance(source_commit, str)
        or len(source_commit) != 40
        or any(character not in _SHA40 for character in source_commit)
    ):
        raise ProviderRootError("manifest_identity_mismatch", "M1a generation/basis identity differs")
    return manifest


def default_provider_bindings(manifest: Mapping[str, Any]) -> tuple[ProviderBinding, ...]:
    """Build the only default binding set; no discovery or global registry is used."""

    checked = _validated_manifest(manifest)
    basis = checked["basis"]
    generation = checked["generation"]
    assert isinstance(basis, Mapping)
    assert isinstance(generation, Mapping)
    source_commit = basis["source_commit"]
    source_branch = basis["source_branch"]
    manifest_digest = checked["semantic_digest"]
    assert isinstance(source_commit, str)
    assert isinstance(source_branch, str)
    assert isinstance(manifest_digest, str)
    result: list[ProviderBinding] = []
    for document in _manifest_provider_documents(checked):
        provider_id = document["provider_id"]
        component_id = document["component_id"]
        kind = document["kind"]
        assert isinstance(provider_id, str)
        assert isinstance(component_id, str)
        assert isinstance(kind, str)
        if provider_id == _composition.COMPOSITION_PROVIDER_ID:
            implementation: object | None = CompositionDiagnosticProvider(
                generation=ROOT_GENERATION,
                manifest_schema=str(checked["schema"]),
                manifest_version=int(checked["manifest_version"]),
                manifest_digest=manifest_digest,
                source_branch=source_branch,
                source_commit=source_commit,
            )
            state = "BOUND"
        elif provider_id == _composition.REPO_VIEW_PROVIDER_ID:
            implementation = RepoViewProvider()
            state = "BOUND"
        elif provider_id == _composition.BROWSER_PROVIDER_ID:
            implementation = BrowserTransportProvider()
            state = "BOUND"
        elif provider_id == _composition.NATIVE_PROVIDER_ID:
            implementation = NativeTransportProvider()
            state = "BOUND"
        else:
            implementation = None
            state = "RESERVED"
        provider_contract = getattr(implementation, "provider_contract", None)
        provider_contract_version = getattr(implementation, "provider_contract_version", None)
        implementation_identity = getattr(implementation, "implementation_identity", None)
        implementation_module = getattr(implementation, "implementation_module", None)
        implementation_qualname = getattr(implementation, "implementation_qualname", None)
        implementation_revision = getattr(implementation, "implementation_revision", None)
        result.append(
            ProviderBinding(
                provider_id=provider_id,
                generation=ROOT_GENERATION,
                component_id=component_id,
                kind=kind,
                state=state,
                implementation=implementation,
                provider_contract=provider_contract,
                provider_contract_version=provider_contract_version,
                implementation_identity=implementation_identity,
                implementation_module=implementation_module,
                implementation_qualname=implementation_qualname,
                implementation_revision=implementation_revision,
            )
        )
    return tuple(result)


@dataclass
class VNextControlPlane:
    """Build-only control-plane graph owned by one explicit root."""

    root: "VNextCompositionRoot"
    bindings: Any
    admission: Any
    work_kernel: Any
    candidate_store: Any
    evidence_store: Any
    publication_store: Any
    operator_query: Any

    @property
    def candidate(self) -> Any:
        """N2 Candidate repository built through this same root."""

        return self.candidate_store

    @property
    def evidence(self) -> Any:
        return self.evidence_store

    @property
    def publication(self) -> Any:
        return self.publication_store

    @property
    def query(self) -> Any:
        """The read-only canonical N4 operator query boundary."""

        return self.operator_query

    def close(self) -> None:
        self.publication_store.close()
        self.evidence_store.close()
        self.candidate_store.close()
        self.work_kernel.close()
        self.admission.close()
        self.bindings.close()

    def __enter__(self) -> "VNextControlPlane":
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()


class VNextCompositionRoot:
    """Explicit, immutable-in-practice provider selection for BDB Next."""

    __slots__ = ("_manifest_identity", "_providers", "_fingerprint", "_runtime_root", "_legacy_runtime_root")

    def __init__(
        self,
        manifest: Mapping[str, Any],
        *,
        bindings: Iterable[ProviderBinding] | None = None,
        expected_manifest_digest: str | None = None,
        expected_source_commit: str | None = None,
    ) -> None:
        checked = _validated_manifest(manifest)
        actual_digest = checked.get("semantic_digest")
        if expected_manifest_digest is not None and actual_digest != expected_manifest_digest:
            raise ProviderRootError(
                "manifest_identity_mismatch",
                "composition manifest digest differs from the expected identity",
            )
        basis = checked["basis"]
        assert isinstance(basis, Mapping)
        actual_source_commit = basis["source_commit"]
        if expected_source_commit is not None and actual_source_commit != expected_source_commit:
            raise ProviderRootError(
                "manifest_identity_mismatch",
                "composition manifest basis commit differs from the expected identity",
            )
        declarations = default_provider_bindings(checked) if bindings is None else self._coerce_bindings(bindings)
        providers = self._validate_bindings(checked, declarations)
        identity = {
            "schema": PROVIDER_ROOT_SCHEMA,
            "generation": ROOT_GENERATION,
            "product_id": PRODUCT_ID,
            "product_topology": PRODUCT_TOPOLOGY,
            "architecture_freeze": _composition.ARCHITECTURE_FREEZE,
            "manifest": {
                "schema": checked["schema"],
                "manifest_version": checked["manifest_version"],
                "semantic_digest": actual_digest,
                "basis": {
                    "source_branch": basis["source_branch"],
                    "source_commit": actual_source_commit,
                },
            },
            "providers": [
                item.descriptor() for item in sorted(providers, key=lambda item: item.provider_id)
            ],
            "runtime_state": RUNTIME_STATE,
            "writer_state": WRITER_STATE,
            "activation_state": ACTIVATION_STATE,
        }
        object.__setattr__(self, "_manifest_identity", _freeze(identity["manifest"]))
        object.__setattr__(self, "_runtime_root", Path(str(checked["paths"]["runtime_root"])).absolute())
        object.__setattr__(
            self,
            "_legacy_runtime_root",
            Path(str(checked["legacy_boundary"]["runtime_root"])).absolute(),
        )
        object.__setattr__(
            self,
            "_providers",
            MappingProxyType({item.provider_id: item for item in providers}),
        )
        object.__setattr__(self, "_fingerprint", semantic_digest(identity))

    @staticmethod
    def _coerce_bindings(bindings: Iterable[ProviderBinding]) -> tuple[ProviderBinding, ...]:
        try:
            declarations = tuple(bindings)
        except TypeError as exc:
            raise ProviderRootError("malformed_provider_declaration", "bindings must be iterable") from exc
        if not declarations:
            raise ProviderRootError("malformed_provider_declaration", "bindings must not be empty")
        return declarations

    @staticmethod
    def _validate_bindings(
        manifest: Mapping[str, Any],
        declarations: tuple[ProviderBinding, ...],
    ) -> tuple[ProviderBinding, ...]:
        documents = {
            item["provider_id"]: item for item in _manifest_provider_documents(manifest)
        }
        seen: set[str] = set()
        for declaration in declarations:
            if not isinstance(declaration, ProviderBinding):
                raise ProviderRootError("malformed_provider_declaration", "each binding must be ProviderBinding")
            provider_id = declaration.provider_id
            if not isinstance(provider_id, str) or not provider_id:
                raise ProviderRootError("malformed_provider_declaration", "provider ID is malformed")
            if provider_id not in documents:
                raise ProviderRootError("unknown_provider", f"unknown provider ID: {provider_id}")
            if provider_id in seen:
                raise ProviderRootError("duplicate_provider_id", f"provider ID is duplicated: {provider_id}")
            seen.add(provider_id)
            if declaration.generation != ROOT_GENERATION:
                raise ProviderRootError(
                    "provider_generation_mismatch",
                    f"provider generation differs for {provider_id}",
                )
            document = documents[provider_id]
            if (
                declaration.component_id != document.get("component_id")
                or declaration.kind != document.get("kind")
                or declaration.state not in _PROVIDER_STATES
            ):
                raise ProviderRootError(
                    "malformed_provider_declaration",
                    f"provider declaration is malformed for {provider_id}",
                )
            expected_state = "BOUND" if document.get("state") == "active_read_only" else "RESERVED"
            if declaration.state != expected_state:
                raise ProviderRootError(
                    "provider_binding_mismatch",
                    f"provider state differs from composition identity for {provider_id}",
                )
            if declaration.state == "BOUND":
                if declaration.implementation is None:
                    raise ProviderRootError(
                        "provider_binding_mismatch",
                        f"bound provider has no implementation: {provider_id}",
                    )
                if provider_id == _composition.COMPOSITION_PROVIDER_ID and not isinstance(
                    declaration.implementation, CompositionDiagnosticProvider
                ):
                    raise ProviderRootError(
                        "provider_binding_mismatch",
                        f"wrong implementation for {provider_id}",
                    )
                if provider_id == _composition.COMPOSITION_PROVIDER_ID:
                    diagnostic = declaration.implementation
                    assert isinstance(diagnostic, CompositionDiagnosticProvider)
                    if (
                        diagnostic.generation != ROOT_GENERATION
                        or diagnostic.manifest_digest != manifest["semantic_digest"]
                        or diagnostic.source_branch != manifest["basis"]["source_branch"]
                        or diagnostic.source_commit != manifest["basis"]["source_commit"]
                    ):
                        raise ProviderRootError(
                            "provider_binding_mismatch",
                            f"composition diagnostic identity differs for {provider_id}",
                        )
                if provider_id == _composition.REPO_VIEW_PROVIDER_ID and not isinstance(
                    declaration.implementation, RepoViewProvider
                ):
                    raise ProviderRootError(
                        "provider_binding_mismatch",
                        f"wrong implementation for {provider_id}",
                    )
                if provider_id == _composition.REPO_VIEW_PROVIDER_ID:
                    repo_view = declaration.implementation
                    assert isinstance(repo_view, RepoViewProvider)
                    if repo_view.generation != ROOT_GENERATION:
                        raise ProviderRootError(
                            "provider_generation_mismatch",
                            f"RepoView implementation generation differs for {provider_id}",
                        )
                if provider_id in {
                    _composition.BROWSER_PROVIDER_ID,
                    _composition.NATIVE_PROVIDER_ID,
                }:
                    expected_type = (
                        BrowserTransportProvider
                        if provider_id == _composition.BROWSER_PROVIDER_ID
                        else NativeTransportProvider
                    )
                    if type(declaration.implementation) is not expected_type:
                        raise ProviderRootError(
                            "provider_binding_mismatch",
                            f"provider implementation must be the exact canonical class for {provider_id}",
                        )
                    implementation = declaration.implementation
                    expected_implementation = expected_type()
                    if (
                        implementation.generation != expected_implementation.generation
                        or declaration.provider_contract != implementation.provider_contract
                        or declaration.provider_contract_version != implementation.provider_contract_version
                        or declaration.implementation_identity != implementation.implementation_identity
                        or declaration.implementation_module != implementation.implementation_module
                        or declaration.implementation_qualname != implementation.implementation_qualname
                        or declaration.implementation_revision != implementation.implementation_revision
                        or implementation.provider_contract != expected_implementation.provider_contract
                        or implementation.provider_contract_version != expected_implementation.provider_contract_version
                        or implementation.implementation_identity != expected_implementation.implementation_identity
                        or implementation.implementation_module != expected_implementation.implementation_module
                        or implementation.implementation_qualname != expected_implementation.implementation_qualname
                        or implementation.implementation_revision != expected_implementation.implementation_revision
                    ):
                        raise ProviderRootError(
                            "provider_identity_mismatch",
                            f"provider contract/implementation identity differs for {provider_id}",
                        )
                if provider_id not in _BOUND_PROVIDER_IDS:
                    raise ProviderRootError(
                        "provider_binding_mismatch",
                        f"reserved provider cannot be bound in M1c: {provider_id}",
                    )
            elif declaration.implementation is not None or any(
                value is not None
                for value in (
                    declaration.provider_contract,
                    declaration.provider_contract_version,
                    declaration.implementation_identity,
                    declaration.implementation_module,
                    declaration.implementation_qualname,
                    declaration.implementation_revision,
                )
            ):
                raise ProviderRootError(
                    "malformed_provider_declaration",
                    f"non-bound provider carries an implementation: {provider_id}",
                )
        missing = set(_KNOWN_PROVIDER_IDS) - seen
        if missing:
            raise ProviderRootError(
                "malformed_provider_declaration",
                f"provider declaration is incomplete: {sorted(missing)}",
            )
        by_id = {item.provider_id: item for item in declarations}
        for required_id in sorted(_BOUND_PROVIDER_IDS):
            if by_id[required_id].state != "BOUND":
                raise ProviderRootError(
                    "provider_binding_mismatch",
                    f"required M1c provider is not bound: {required_id}",
                )
        return declarations

    @classmethod
    def from_manifest(
        cls,
        manifest: Mapping[str, Any],
        *,
        bindings: Iterable[ProviderBinding] | None = None,
        expected_manifest_digest: str | None = None,
        expected_source_commit: str | None = None,
    ) -> "VNextCompositionRoot":
        return cls(
            manifest,
            bindings=bindings,
            expected_manifest_digest=expected_manifest_digest,
            expected_source_commit=expected_source_commit,
        )

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    @property
    def manifest_identity(self) -> dict[str, Any]:
        return _plain(self._manifest_identity)

    @property
    def provider_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))

    @property
    def bound_provider_ids(self) -> tuple[str, ...]:
        return tuple(sorted(item.provider_id for item in self._providers.values() if item.state == "BOUND"))

    @property
    def unavailable_provider_ids(self) -> tuple[str, ...]:
        return tuple(sorted(item.provider_id for item in self._providers.values() if item.state == "UNAVAILABLE"))

    @property
    def reserved_provider_ids(self) -> tuple[str, ...]:
        return tuple(sorted(item.provider_id for item in self._providers.values() if item.state == "RESERVED"))

    def status(self) -> dict[str, Any]:
        providers = [
            item.descriptor() for item in sorted(self._providers.values(), key=lambda item: item.provider_id)
        ]
        return {
            "schema": PROVIDER_ROOT_SCHEMA,
            "generation": ROOT_GENERATION,
            "product_id": PRODUCT_ID,
            "product_topology": PRODUCT_TOPOLOGY,
            "manifest": self.manifest_identity,
            "providers": providers,
            "provider_ids": list(self.provider_ids),
            "bound_provider_ids": list(self.bound_provider_ids),
            "unavailable_provider_ids": list(self.unavailable_provider_ids),
            "reserved_provider_ids": list(self.reserved_provider_ids),
            "runtime_state": RUNTIME_STATE,
            "writer_state": WRITER_STATE,
            "activation_state": ACTIVATION_STATE,
            "writer_enabled": False,
            "fingerprint": self.fingerprint,
        }

    def provider(self, provider_id: str) -> object:
        if not isinstance(provider_id, str) or not provider_id:
            raise ProviderRootError("unknown_provider", "provider ID must be a non-empty string")
        binding = self._providers.get(provider_id)
        if binding is None:
            raise ProviderRootError("unknown_provider", f"unknown provider ID: {provider_id}")
        if binding.state != "BOUND" or binding.implementation is None:
            raise ProviderRootError(
                "provider_unavailable",
                f"provider is not available in M1c: {provider_id}",
                details={"state": binding.state},
            )
        return binding.implementation

    get_provider = provider
    require_provider = provider

    def composition_diagnostic(self) -> CompositionDiagnosticProvider:
        provider = self.provider(_composition.COMPOSITION_PROVIDER_ID)
        if not isinstance(provider, CompositionDiagnosticProvider):
            raise ProviderRootError("provider_binding_mismatch", "composition diagnostic binding is invalid")
        return provider

    def repo_view_provider(self) -> RepoViewProvider:
        provider = self.provider(_composition.REPO_VIEW_PROVIDER_ID)
        if not isinstance(provider, RepoViewProvider):
            raise ProviderRootError("provider_binding_mismatch", "RepoView binding is invalid")
        return provider

    def browser_transport_provider(self) -> BrowserTransportProvider:
        provider = self.provider(_composition.BROWSER_PROVIDER_ID)
        if not isinstance(provider, BrowserTransportProvider):
            raise ProviderRootError("provider_binding_mismatch", "Browser transport binding is invalid")
        return provider

    def native_transport_provider(self) -> NativeTransportProvider:
        provider = self.provider(_composition.NATIVE_PROVIDER_ID)
        if not isinstance(provider, NativeTransportProvider):
            raise ProviderRootError("provider_binding_mismatch", "Native transport binding is invalid")
        return provider

    def repository_resource(
        self,
        repo_path: str | Path,
        *,
        repository_id: str | None = None,
        timeout_seconds: int = DEFAULT_GIT_TIMEOUT_SECONDS,
    ) -> RepositoryResource:
        return self.repo_view_provider().repository_resource(
            repo_path,
            repository_id=repository_id,
            timeout_seconds=timeout_seconds,
        )

    def resolve_committed(
        self,
        resource_or_path: RepositoryResource | str | Path,
        ref: str,
        *,
        repository_id: str | None = None,
        observed_at: str | None = None,
        timeout_seconds: int = DEFAULT_GIT_TIMEOUT_SECONDS,
    ) -> CommittedRepoView:
        return self.repo_view_provider().resolve_committed(
            resource_or_path,
            ref,
            repository_id=repository_id,
            observed_at=observed_at,
            timeout_seconds=timeout_seconds,
        )

    def query(self, resource: RepositoryResource, view: CommittedRepoView) -> RepoViewQuery:
        return self.repo_view_provider().query(resource, view)

    def fallback_code_fact_provider(self) -> Any:
        """Return the removable lower-coverage EI provider through this root."""

        from bdb_vnext.code_intelligence import FallbackCodeFactProvider

        return FallbackCodeFactProvider()

    def tree_sitter_code_fact_provider(self) -> Any:
        """Return the optional exact Python syntax provider through this root."""

        from bdb_vnext.code_intelligence import TreeSitterPythonProvider

        return TreeSitterPythonProvider()

    def lsp_code_fact_provider(
        self,
        command: Iterable[str],
        *,
        server_identity: str,
        timeout_seconds: float = 8.0,
    ) -> Any:
        """Construct one explicit read-only LSP adapter; no discovery is used."""

        from bdb_vnext.code_intelligence import LspCodeFactProvider

        return LspCodeFactProvider(
            tuple(command),
            server_identity=server_identity,
            timeout_seconds=timeout_seconds,
        )

    def open_control_plane(
        self,
        *,
        existing_outbox: bool = False,
        clock: Any | None = None,
    ) -> VNextControlPlane:
        """Construct the inactive M2/M3/M4 graph through this root only.

        The root is built from a manifest whose runtime and legacy paths are
        immutable.  Opening this graph is a build/test action; it does not
        enable external runtime, production admission, or activation.
        """

        from bdb_vnext.content_store import DurableBindingStore
        from bdb_vnext.candidate import CandidateStore
        from bdb_vnext.m4c_evidence import EvidenceStore
        from bdb_vnext.n4_publication import CanonicalOperatorQuery, PublicationStore
        from bdb_vnext.m3c_admission import _open_vnext_admission_composition
        from bdb_vnext.m4a_work_kernel import WorkKernelStore

        bindings = None
        admission = None
        candidate = None
        evidence = None
        publication = None
        work_kernel = None
        try:
            bindings = DurableBindingStore(self._runtime_root)
            admission = _open_vnext_admission_composition(
                self._runtime_root,
                legacy_root=self._legacy_runtime_root,
                existing_outbox=existing_outbox,
            )
            work_kernel = WorkKernelStore.open(
                self._runtime_root,
                task_authority=admission.authority,
                legacy_root=self._legacy_runtime_root,
                clock=clock,
            )
            candidate = CandidateStore(
                self._runtime_root,
                content_store=bindings.content_store,
                work_kernel=work_kernel,
            )
            evidence = EvidenceStore(self._runtime_root, content_store=bindings.content_store, candidate_store=candidate)
            publication = PublicationStore(
                self._runtime_root,
                content_store=bindings.content_store,
                task_authority=admission.authority,
                work_kernel=work_kernel,
                candidate_store=candidate,
                evidence_store=evidence,
            )
            operator_query = CanonicalOperatorQuery(
                self,
                admission=admission,
                work_kernel=work_kernel,
                candidate_store=candidate,
                evidence_store=evidence,
                publication_store=publication,
            )
            return VNextControlPlane(self, bindings, admission, work_kernel, candidate, evidence, publication, operator_query)
        except Exception:
            if publication is not None:
                publication.close()
            if evidence is not None:
                evidence.close()
            if candidate is not None:
                candidate.close()
            if work_kernel is not None:
                work_kernel.close()
            if admission is not None:
                admission.close()
            if bindings is not None:
                bindings.close()
            raise

    def __repr__(self) -> str:
        return f"VNextCompositionRoot(generation={ROOT_GENERATION!r}, fingerprint={self.fingerprint!r})"


def create_vnext_provider_root(
    manifest: Mapping[str, Any],
    *,
    bindings: Iterable[ProviderBinding] | None = None,
    expected_manifest_digest: str | None = None,
    expected_source_commit: str | None = None,
) -> VNextCompositionRoot:
    """Create a root explicitly; importing this function never creates state."""

    return VNextCompositionRoot.from_manifest(
        manifest,
        bindings=bindings,
        expected_manifest_digest=expected_manifest_digest,
        expected_source_commit=expected_source_commit,
    )


build_vnext_provider_root = create_vnext_provider_root


__all__ = [
    "ACTIVATION_STATE",
    "BrowserTransportProvider",
    "CompositionDiagnosticProvider",
    "NativeTransportProvider",
    "PRODUCT_ID",
    "PRODUCT_TOPOLOGY",
    "PROVIDER_ROOT_SCHEMA",
    "ProviderBinding",
    "ProviderRootError",
    "RepoViewProvider",
    "ROOT_GENERATION",
    "RUNTIME_STATE",
    "VNextCompositionRoot",
    "VNextControlPlane",
    "WRITER_STATE",
    "build_vnext_provider_root",
    "create_vnext_provider_root",
    "default_provider_bindings",
]

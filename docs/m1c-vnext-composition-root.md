# M1c-vNext — Explicit Composition Root

This note records the additive M1c contract. It is not a new execution plan
and does not turn the M1a manifest into a service locator.

`bdb_vnext.provider_root.VNextCompositionRoot` is constructed explicitly from
one validated M1a composition manifest. It keeps the M1a manifest as a
read-only identity/basis record and selects providers only from the existing
M1a provider IDs. Provider declarations are generation-qualified and are
validated for uniqueness, exact component/kind, supported state, and expected
implementation type. No module scan, entry point, global registry, import-time
registration, monkeypatch, or legacy fallback is used.

The M1c root identity is deterministic: `PROVIDER_ROOT_SCHEMA`, generation,
product/topology, Architecture Freeze identity, M1a semantic digest and basis,
sorted provider descriptors, and `OFF/OFF/OFF` runtime/writer/activation state
are canonicalized and hashed with the existing `semantic_digest` helper. No
object representation or import order contributes to the fingerprint.

Current explicit bindings:

- `devmaster.bdb.vnext.composition-manifest` → read-only M1a diagnostic provider;
- `devmaster.bdb.vnext.repo-view` → the accepted M2a `RepositoryResource` /
  `CommittedRepoView` API.

The Control Store, Browser transport, Native transport, and Control Center
query IDs remain `RESERVED` and fail closed with `provider_unavailable` when
requested. X1 and X2 experiment modules are not providers. The root creates no
runtime directory, store, lock, registration, process, or writer state.

Focused evidence is in `tests/test_vnext_provider_root.py`; M1a regression is
covered by `tests/test_vnext_composition.py`, and explicit RepoView regression
by `tests/test_vnext_repo_view.py`.

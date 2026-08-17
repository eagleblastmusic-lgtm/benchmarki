# BDB vNext M8b — Rebuildable RepoView-bound Index / Understanding Closure

Status: `CLOSED_BY_EXISTING_IMPLEMENTATION` for the build-only vNext line. Production runtime/writer/activation remain OFF/OFF/OFF.

## Closure decision

M8b does not require another Index database, Understanding writer, cache daemon or lifecycle primitive. The existing M2c/N5 implementation already has the frozen M8b ownership model:

```text
exact committed Git objects
        ↓
CommittedRepoView / RepoViewBinding
        ↓
read-only code-fact providers + exact source evidence
        ↓
RepositoryUnderstandingView
```

Git/RepoView remains source-byte authority. Provider facts and Understanding are immutable, rebuildable projections only.

## Exact basis and invalidation

`RepositoryUnderstandingView` binds its complete semantic identity to:

- exact `IntentBasis`;
- exact `RepoViewBinding`;
- exact claims and coverage bindings;
- explicit unknowns, omissions and contradictions;
- explicit invalidation predicates;
- producer identity/version.

Its default invalidation predicates include RepoView identity change, intent change and producer/schema change. `validate_source_grounding()` rejects a foreign/new RepoView instead of treating an old Understanding as current.

FACT claims require accepted repository-source evidence grounded in the exact committed Git object. A generic accepted content fragment is not enough. A fabricated fragment whose bytes do not match the exact Git object is rejected.

## Code-intelligence projections

`CodeFact` and `ProviderResult` bind:

- exact RepoView;
- provider/version/configuration identity;
- exact repository path;
- exact source Git object ID;
- deterministic fact identity.

`ProviderResult.validate_against(view)` re-reads the exact Git view and rejects stale/foreign RepoViews, missing paths or source-object mismatch.

Tree-sitter/LSP/fallback providers remain replaceable observation providers. They do not own repository bytes or lifecycle truth.

## Rebuildability proof

The focused M8b closure contract proves:

1. the same exact RepoView + intent + accepted exact source evidence rebuilds the same Evidence, Claim, CoverageBinding and Understanding identities;
2. canonical serialized Understanding bytes are identical for the same exact input;
3. a dirty physical checkout cannot alter a rebuild from the already-resolved committed RepoView;
4. provider results are likewise unchanged when only the mutable checkout changes;
5. a new commit creates a new RepoView and invalidates the old Understanding/provider projection;
6. rebuilding against the new RepoView produces a new exact projection;
7. Engineering Intelligence and code-intelligence modules do not gain Task/Work/Git-effect authority merely to satisfy M8b.

## Authority rule

Parsing or reconstructing a semantic record does not make it authoritative. Authority-sensitive use must re-establish exact RepoView/source grounding through the live binding store and committed RepoView.

This preserves Architecture Freeze separation:

```text
Git / CommittedRepoView = bytes authority
M2b accepted binding     = typed transport/content grounding
M2c/N5 projection        = rebuildable engineering understanding
```

## Out of scope

M8b closure does not:

- create a persistent semantic cache;
- create an index lifecycle writer;
- activate production runtime;
- mutate source Git refs or checkout state;
- make parser/provider output repository authority;
- replace M6 evidence or M7 Git promotion authority;
- implement CC1.

The next user-facing build-only gate is CC1: reuse the existing PySide6 Control Center shell, but replace its legacy `bdb_operator.OperatorApi` bootstrap/query dependency with a read-only vNext canonical projection boundary.

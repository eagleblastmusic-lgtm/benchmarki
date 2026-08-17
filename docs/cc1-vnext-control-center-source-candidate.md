# CC1 — vNext Control Center source closure candidate

Status: **SOURCE CANDIDATE — validation required before closure**

Basis:

- canonical branch basis: `bdb-vnext`
- basis commit: `634997470692795189a252ff995ee3a6a5f494d8`
- previous milestone: M8b PASS/CLOSED
- roadmap gate: B29 / CC1

## Intent

Adapt the existing PySide6 Control Center into a thin vNext projection client
without importing legacy runtime semantics into the new generation.

This slice deliberately does **not** activate vNext, publish a Control Center
provider, resume work, execute an effect, move a Git ref, push/merge a branch,
or deploy anything to Shopify.

## Authority boundary

`bdb_gui` no longer opens or calls `bdb_operator.OperatorApi` from its
application entrypoint.  The CC1 window consumes
`bdb_vnext.control_center_query`, which:

1. treats an absent vNext runtime as explicit `OFF`;
2. validates an existing Control DB through the existing external-seal and
   read-only preflight contract;
3. reopens the verified database with SQLite `mode=ro` and `query_only=ON`;
4. projects stored Work, Candidate effect, Evidence, RepoView identity and
   Publication facts without lifecycle mutation;
5. exposes only disabled mutation predicates with typed reason
   `cc1_read_only`;
6. never falls back to legacy data.

The reader is a core adapter, not GUI-owned SQL.  The GUI itself has no
SQLite dependency and does not reconstruct lifecycle state.

## UI semantics

The shell presents a vector instead of a synthetic magic health state:

- System
- Writer
- Activation
- Control Store

Per WorkItem it shows the stored Work state and, when present, the related
Candidate effect, Evidence/evaluation, repository view identities and latest
Publication.  Missing dimensions are rendered as `null` / N/A rather than
inferred.

The action row is intentionally disabled in CC1.  Later activation/cutover
milestones must bind actions to their own canonical predicates; CC1 does not
pre-authorize them.

## Validation required

Before this candidate may be marked PASS/CLOSED:

- Python syntax/import validation;
- focused CC1 query tests;
- PySide6 headless OFF and DEGRADED smoke;
- impacted Control Center tests;
- static proof that the vNext GUI entrypoint does not import `bdb_operator`
  and the GUI projection module does not import SQLite;
- source review for absence of create/migrate/resume/apply/publish calls;
- branch/commit comparison against the exact M8b basis;
- CI/check status.

Only after those checks pass should this file be promoted to a closure record.

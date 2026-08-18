# M12a — Compatibility Zero Gate

Status: **source/CI closure PASS; production closure pending maintenance upgrade**. M12a does not delete, contract, disable, activate, switch, install, start, stop, register, promote, or maintain any production surface.

Final source-gate implementation subject before this closure-only documentation commit: `80af8cb3fb4f46d11a1ef52b5697359ce9799414`. The documentation-only closure commit remains part of the same PR and is required to pass the identical gate set before merge.

Required workflow evidence for the final PR head:

- M12a Compatibility Zero CI — Windows PASS + Ubuntu PASS;
- M11c Cutover CI — Windows PASS + Ubuntu PASS;
- M11c Pre-staging CI — PASS;
- M11a Bootstrap CI — PASS;
- trusted BDB vNext CI — PASS.

This source/CI PASS is **not** a production M12a PASS. The currently deployed frozen production subject remains `bd634b85047674b74846ceaed959ac7883e3eb4a`, which the M12a exact-source test deliberately classifies as non-zero until a content-addressed maintenance upgrade is performed.

## Purpose

M12a proves that compatibility and migration bridges have zero production usage before M12b performs destructive cleanup or target-only release contraction.

The canonical gate has two layers:

1. `bdb-vnext-m12a` — base runtime/source compatibility-zero capture.
2. `bdb-vnext-m12a-closure` — full canonical M12a closure required to unlock M12b.

A base `PASS_CLOSED` is necessary but is not by itself M12a DONE. M12a DONE requires the full closure report to be `PASS_CLOSED` with `m12b_unlocked=true` and `unnamed_exceptions=[]`.

## Base compatibility-zero evidence

The base gate records two independent classes of evidence.

### Runtime zero-use

- External Bootstrap is already `ACTIVE` under `m11c-external-bootstrap`.
- M9b subordinate Browser/Native gate is `ACTIVE` with intake/writer enabled.
- Exact Browser launch verification remains bound to the active client plan.
- The dedicated vNext Native Messaging route is the only route for the takeover subject.
- M9a takeover-sensitive Legacy observations are revalidated twice across a bounded observation window.
- The complete content-addressed M9a archive and all child evidence objects remain readable.
- The M3c authority map still has exactly one supported accepting writer and no supported Legacy admission path.

### Source/package zero-dependency

- The scanner reads the exact Git commit recorded in the ACTIVE slot; it never substitutes the current checkout.
- The active Native Host transitive Python import closure must not depend on Legacy packages or migration-only M9a/M11a/M11c preparation modules.
- The exact Browser client-files manifest must not contain the Legacy Native host identity or an enabled Legacy fallback.
- Source-only Legacy packages, entrypoints, installers and historical recovery code may remain during M12a only with an explicit M12b disposition.

## Full canonical closure

`bdb-vnext-m12a-closure capture` first requires a fresh base `PASS_CLOSED`, then adds every remaining canonical M12a proof.

### Full compatibility inventory

The inventory scans the complete tracked Git subject for compatibility-sensitive tokens and known Legacy package families. Every hit must have a named class and an explicit M12b disposition. Unknown compatibility paths are not ignored: they populate `unclassified_compatibility_paths` and block closure.

The inventory distinguishes production target surfaces from historical/Legacy surfaces, including:

- Legacy runtime/operator/UI/POC/Browser packages — exclude or archive in M12b;
- target Browser package — retain in target-only release;
- Legacy Windows Native packaging entry — exclude in M12b;
- target Windows Native packaging entry — retain in target-only release;
- migration/compatibility vNext source — retain only if the final target closure requires it;
- benchmark assets — retain only as non-production benchmark archive evidence;
- scripts, tests, docs, schemas, CI and package-composition references — each with an explicit M12b disposition.

There is no catch-all disposition for unknown paths. The final source-gate inventory converged only after explicit classification of benchmark archive evidence and separate target/Legacy Browser/Native packaging surfaces.

### Stale-client behavior

The closure rehearses stale Browser/client input against the ACTIVE Native protocol without creating a Browser verification witness or modifying authority state:

- an old protocol generation must return the explicit `unsupported_protocol` upgrade/rejection class;
- a wrong extension identity must return `client_identity_mismatch`;
- External Bootstrap state digest, M9b record digest, client-plan digest and verification digest must be byte-identical before and after the rehearsal.

### Interrupted archive rehearsal

The real M9a archive is first verified in place. Its referenced content-addressed objects are then copied to a disposable scratch directory only.

The scratch rehearsal must prove:

- a missing child object fails closed as `evidence_missing`;
- a tampered child object fails closed as digest/format mismatch;
- no production archive object is modified;
- ACTIVE authority identity is unchanged before and after the rehearsal.

### Read-only soak and benchmark basis

A bounded post-ACTIVE soak repeatedly re-observes the authority/client identity and revalidates the M9a freeze digest. All iterations must pass and the authority identity must remain stable.

M12a records latency samples as observational telemetry but intentionally applies no arbitrary performance threshold. The final target-only performance benchmark belongs to M12b.

The benchmark basis binds the preserved local-browser benchmark harness by exact file digest and names the canonical scenario set:

1. historical local-browser functional/performance basis — history only; Legacy is not re-armed;
2. post-ACTIVE Browser/Native exact verification;
3. post-ACTIVE Legacy zero-write soak;
4. stale-client rejection;
5. interrupted archive recovery;
6. single-writer admission;
7. active Native import closure;
8. active Browser no-fallback.

The preserved benchmark harness tests run in CI. The old Legacy benchmark is not executed against production after cutover because doing so would re-arm the retired takeover route.

## Current production subject and maintenance boundary

The production cutover activated source `bd634b85047674b74846ceaed959ac7883e3eb4a`. M12a tests intentionally prove that this exact deployed source is **not** compatibility-zero because its Native Host imports the broad `m11c_cutover` module, which carries migration/preparation dependencies.

The cleaned source isolates production ACTIVE observation into `m11c_active_reader`, a minimal read-only reader of the same External Bootstrap pointer. The cleaned source subject passes the exact-source closure scan and can become a content-addressed maintenance candidate.

A source merge does not change the running frozen executable. Therefore M12a may not report production `PASS_CLOSED` until the cleaned source has been built, staged, verified and promoted by a dedicated maintenance path that preserves the **same External Bootstrap authority and single ACTIVE pointer**. An in-place executable replacement is not acceptable. The current M11a candidate-staging path accepts only pre-activation slot-state v1, so post-ACTIVE maintenance must be implemented and fault-tested explicitly rather than reusing the initial-cutover command.

## Evidence outputs

`bdb-vnext-m12a capture` writes only content-addressed evidence under the vNext runtime:

- `bdb-vnext-m12a-compatibility-zero-report-v1`;
- `bdb-vnext-m12a-deletion-plan-v1`.

The deletion plan is explicitly `PLANNED_NOT_APPLIED`. Every bridge row has a named M12b disposition and `unnamed_exceptions` must remain empty for base `PASS_CLOSED`.

`bdb-vnext-m12a-closure capture` adds a content-addressed:

- `bdb-vnext-m12a-full-closure-report-v1`.

Full `PASS_CLOSED` requires:

- complete compatibility inventory with zero unclassified paths;
- complete named benchmark basis;
- explicit stale-client rejection with unchanged ACTIVE authority;
- readable archive and successful interrupted/tamper rehearsal on scratch evidence;
- stable bounded post-ACTIVE soak;
- `unnamed_exceptions=[]`;
- `m12b_unlocked=true`;
- `production_mutation_performed=false`;
- `final_deletion_performed=false`.

Both `verify` commands reread content-addressed evidence and verify their digest/bindings. Neither performs a production mutation.

## M12b handoff

M12b may begin only after the **production** full M12a closure report is `PASS_CLOSED` with `m12b_unlocked=true`. M12b consumes the deletion/disposition manifest and full compatibility inventory to remove active Legacy/migration composition from the target-only production package while preserving the required historical archive and the vNext `PREVIOUS` recovery subject.

M12b must then run the final target-only benchmark and operator release proof. Final deletion/contract effects remain outside M12a.
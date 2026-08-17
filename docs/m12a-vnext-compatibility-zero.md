# M12a — Compatibility Zero Gate

Status: **post-activation validation only**. M12a does not delete, contract, disable, activate, switch, install, start, or stop any production surface.

## Purpose

M12a proves that compatibility and migration bridges have zero production usage before M12b performs destructive cleanup or target-only release contraction.

The gate records two independent classes of evidence:

1. **Runtime zero-use**
   - External Bootstrap is already `ACTIVE` under `m11c-external-bootstrap`.
   - M9b subordinate Browser/Native gate is `ACTIVE` with intake/writer enabled.
   - Exact Browser launch verification remains bound to the active client plan.
   - The dedicated vNext Native Messaging route is the only route for the takeover subject.
   - M9a takeover-sensitive Legacy observations are revalidated twice across a bounded observation window.
   - The complete content-addressed M9a archive and all child evidence objects remain readable.
   - The M3c authority map still has exactly one supported accepting writer and no supported Legacy admission path.

2. **Source/package zero-dependency**
   - The scanner reads the exact Git commit recorded in the ACTIVE slot; it never substitutes the current checkout.
   - The active Native Host transitive Python import closure must not depend on Legacy packages or migration-only M9a/M11a/M11c preparation modules.
   - The exact Browser client-files manifest must not contain the Legacy Native host identity or an enabled Legacy fallback.
   - Source-only Legacy packages, entrypoints, installers and historical recovery code may remain during M12a only with an explicit M12b disposition.

## Current production subject and maintenance boundary

The production cutover activated source `bd634b85047674b74846ceaed959ac7883e3eb4a`. M12a tests intentionally prove that this exact deployed source is **not** compatibility-zero because its Native Host imports the broad `m11c_cutover` module, which carries migration/preparation dependencies.

This PR isolates production ACTIVE observation into `m11c_active_reader`, a minimal read-only reader of the same external Bootstrap pointer. The new source subject must pass the exact-source closure scan before it can be built as a content-addressed maintenance candidate.

A source merge does not change the running frozen executable. Therefore M12a may not report `PASS_CLOSED` on the user machine until the cleaned source has been staged, verified and promoted through the existing External Bootstrap maintenance/cutover authority. No in-place replacement of the running frozen artifact is allowed.

## Evidence outputs

`bdb-vnext-m12a capture` writes only content-addressed evidence under the vNext runtime:

- `bdb-vnext-m12a-compatibility-zero-report-v1`
- `bdb-vnext-m12a-deletion-plan-v1`

The deletion plan is explicitly `PLANNED_NOT_APPLIED`. Every bridge row has a named M12b disposition and `unnamed_exceptions` must remain empty for `PASS_CLOSED`.

`bdb-vnext-m12a verify` rereads the content-addressed report and deletion plan and verifies their digests and PASS invariants. It performs no production mutation.

## M12b handoff

M12b may begin only after a production M12a report is `PASS_CLOSED`. M12b consumes the deletion/disposition manifest to remove active Legacy/migration composition from the target-only production package while preserving the required read-only historical archive and the vNext `PREVIOUS` recovery slot.
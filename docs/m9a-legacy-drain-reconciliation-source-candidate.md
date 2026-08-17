# M9a — legacy drain/reconciliation preflight source candidate

Status: **BOUNDED SOURCE CANDIDATE — NOT M9a CLOSED**

Basis:

- canonical branch: `bdb-vnext`
- basis commit: `cca0898676525dd255e22727aef6dc43263d56f4`
- CC1: PASS/CLOSED
- fresh R0b: STOP / RECONCILIATION_REQUIRED until local legacy authority is fully observed

## Scope of this slice

This slice implements only the read-only/classification part authorized when a
fresh R0b attempt is not yet SAFE/READY. It does not:

- stop or mutate legacy ingress;
- stop a legacy process or writer;
- touch the legacy Journal, receipts, spool, Browser storage, registry or Git;
- create/copy the archive candidate;
- create a generation/resource fence;
- activate vNext;
- authorize M9b.

`bdb_vnext.m9a_cutover` consumes one `runtime-inventory-v1` evidence document
and optional explicit operator collision dispositions. It returns a
deterministic preflight with one of:

- `READY_FOR_LOCAL_M9A_FREEZE`
- `DRAIN_REQUIRED`
- `RECONCILIATION_REQUIRED`
- `BLOCKED_INVALID`
- `BLOCKED_UNSUPPORTED`

`bdb_vnext.m9a_reconciliation` composes those preflights across every explicit
legacy profile that can collide with the repository cutover. Safety is
conjunctive: a clean profile cannot discharge an invalid, drain-required or
unclassified sibling profile. The aggregate emits bounded inspection
obligations for writer candidates, spool entries, invalid/missing archive
sources, unresolved collision subjects and cross-source blockers. It is still
read-only and creates no migration authority.

Even `READY_FOR_LOCAL_M9A_FREEZE` is only permission to perform the separate,
local effectful M9a freeze protocol. It is never a statement that ingress or
writers are already frozen and never permits M9b.

## Collision classification

Historical legacy rows are not automatically blockers. A still-enumerated
unresolved item must either be classified with evidence as:

- `TERMINAL`
- `DRAINED`
- `FENCED`
- `NO_LIVE_COLLISION_CAPABILITY`
- `BLOCK_RESOURCE_CUTOVER`

or remain a reconciliation blocker.

A truncated unresolved group cannot be discharged item-by-item. Active legacy
writer candidates, non-empty spool, or outstanding Native reservations result
in `DRAIN_REQUIRED` unless a higher-precedence invalid/unsupported source makes
the preflight fail closed earlier.

## Fresh Windows evidence observed during this candidate

A real Windows R0a run on the exact historical `runtime-inventory-v1` provider
identified two current legacy profiles bound to the same repository:

- `bartosz-dev-bridge`
- `bdb-self`

Both have an existing Journal and non-empty direct spool. Both inventories are
`INVALID`, not SAFE. The first profile reports one Journal active-writer
candidate, one spool entry and one unresolved Command/Session while the OS
process observation did not see a BDB process; that mismatch requires bounded
writer-candidate reconciliation rather than assuming either side is correct.
The second profile reports zero active-writer candidates but fourteen spool
entries and a large unresolved legacy set including effects and manual
reconciliation. For both profiles the receipt source is invalid/incomplete; the
promoter source is unavailable/incomplete for `bartosz-dev-bridge` and
invalid/incomplete for `bdb-self`.

No source candidate may transform those facts into `TERMINAL`, `DRAINED`,
`FENCED` or `NO_LIVE_COLLISION_CAPABILITY` without additional evidence. In
particular, absence of a currently visible OS process does not retroactively
prove a Journal writer candidate is harmless, and old unresolved rows are not
bulk-classified merely because they are old.

## Archive candidate

The preflight checks only whether the evidence set contains complete observed
inputs needed to plan an archive candidate. It does not copy them. The
effectful local M9a closure must produce an exact, integrity-checked,
generation-qualified archive candidate covering the required legacy DB/WAL
state, receipts/spool/promoter/config/bundle identities and retention policy.
No legacy lifecycle state is imported into vNext.

## Remaining M9a closure work

M9a may be marked PASS/CLOSED only after fresh local evidence proves:

1. every supported legacy ingress entrypoint is closed;
2. no new Command/Session/receipt/spool/promotion work can be admitted;
3. every collision-relevant unresolved effect is terminal, drained, fenced or
   explicitly blocks the affected resource;
4. an exact readable archive candidate exists and passes integrity/identity
   checks;
5. a fresh post-freeze observation shows no re-admission or writer resurrection;
6. vNext remains externally OFF throughout M9a.

CC2 follows only after the active legacy drain view is empty. M9b is the only
unit allowed to switch Browser/Native generation and turn vNext writer/intake
ON.

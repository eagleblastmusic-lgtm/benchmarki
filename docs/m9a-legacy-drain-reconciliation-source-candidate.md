# M9a — legacy drain/reconciliation preflight source candidate

Status: **BOUNDED SOURCE CANDIDATE — NOT M9a CLOSED**

Basis:

- canonical branch: `bdb-vnext`
- basis commit: `cca0898676525dd255e22727aef6dc43263d56f4`
- CC1: PASS/CLOSED
- fresh R0b: `BLOCKED_INVALID` over two observed legacy profiles; local mutation authority is not granted

## Scope of this slice

This slice implements only the read-only/classification work authorized while
fresh R0b is not SAFE/READY. It does not:

- stop or mutate legacy ingress;
- stop a legacy process or writer;
- touch the legacy Journal, receipts, spool, Browser storage, registry or Git for writing;
- create/copy the archive candidate from observed legacy state;
- create a generation/resource fence;
- activate vNext;
- authorize M9b.

`bdb_vnext.m9a_cutover` consumes a `runtime-inventory-v1` evidence document and
optional explicit operator collision dispositions. It returns a deterministic
single-profile preflight with one of:

- `READY_FOR_LOCAL_M9A_FREEZE`
- `DRAIN_REQUIRED`
- `RECONCILIATION_REQUIRED`
- `BLOCKED_INVALID`
- `BLOCKED_UNSUPPORTED`

`bdb_vnext.m9a_reconciliation` aggregates all observed legacy profiles
conjunctively. No profile can hide another profile's blocker. It emits typed
inspection obligations without creating new authority.

Even `READY_FOR_LOCAL_M9A_FREEZE` is only permission to perform the separate,
local effectful M9a freeze protocol. It is never a statement that ingress or
writers are already frozen and never permits M9b.

## Fresh local blocker evidence

The observed Windows installation currently has two current legacy profiles:

- `bartosz-dev-bridge`: one stale-looking service candidate whose PID was dead at
  observation time, one unresolved Command, one unresolved Session, one direct
  spool envelope, `receipts=INVALID/invalid_shape`, and a missing promoter input;
- `bdb-self`: no active writer candidate, 192 unresolved Commands, 43 persisted
  Effects, two unresolved Sessions, one manual-reconciliation row, 14 direct
  spool envelopes, `receipts=INVALID/invalid_shape`, and
  `promoter=INVALID/invalid_receipt`.

These counts are evidence for bounded investigation, not a requirement to
manually classify every historical row. The migration override requires
collision-relevant current/effect state, not semantic import of the full legacy
history.

## Blocker-specific probe

`bdb_vnext.m9a_blocker_probe` is a narrower follow-up observer, not a competing
R0a inventory provider. It is allowed only after R0a has already identified the
blocker classes. It:

- copies the observed Journal DB/WAL/SHM to caller-owned scratch after stable
  identity checks and opens only the copy read-only/query-only;
- groups unresolved commands by state, operation/profile/resource identity and
  persisted effect/outbox presence without returning `command_json`;
- correlates direct-spool command identities with Journal state without consuming
  or archiving spool entries;
- reports whether the legacy Windows wake event is currently present and whether
  recorded service PIDs are currently alive;
- reports Native alias binding, arm/expiry state and read-only Native Messaging
  manifest observations;
- diagnoses request-receipt top-level shape without exposing request payloads;
- validates promoter receipt identity/sequence rules while emitting only hashes
  and typed failure classes.

The probe never restarts the service, signals the wake event, acquires the legacy
instance lock, executes a command, repairs a receipt, moves/deletes spool data,
updates Journal state, or writes registry state. Its output always keeps
`vnext_activation_allowed=false` and `m9b_allowed=false`.

Exact legacy recovery/source review establishes these safety distinctions:

- `CLAIMED` and some `EXECUTING` rows retain potential filesystem-write recovery
  capability if the legacy service is restarted;
- a valid `EFFECT_RECORDED` row is recovered by idempotent replay when the
  persisted physical state matches, otherwise recovery diverges rather than
  blindly applying the effect again;
- direct spool files are passive evidence until a legacy service polls them;
- Native submit writes a spool envelope and only signals an already-existing wake
  event; it does not itself create the Bridge service;
- every real direct-spool service start still takes `bridge.instance.lock`, then
  performs recovery, ingest and execute phases.

Therefore a dead PID or absent wake event may prove that no service is executing
*now*, but cannot by itself authorize `NO_LIVE_COLLISION_CAPABILITY`; the M9a
freeze must also close/restrict the legacy restart/ingress routes.

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
in `DRAIN_REQUIRED` or a stricter invalid/incomplete status when their source
integrity is not yet established.

## Archive candidate

The preflight checks only whether the evidence set contains complete observed
inputs needed to plan an archive candidate. It does not copy them. The effectful
local M9a closure must produce an exact, integrity-checked, generation-qualified
archive candidate covering the required legacy DB/WAL state,
receipts/spool/promoter/config/bundle identities and retention policy. No legacy
lifecycle state is imported into vNext.

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

CC2 follows only after the active legacy drain view is empty/classified. M9b is
the only unit allowed to switch Browser/Native generation and turn vNext
writer/intake ON.

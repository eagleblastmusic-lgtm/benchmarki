# BDB vNext M7c — Canonical Git Promotion Closure

Status: build-only internal closure. Production runtime/writer/activation remain OFF/OFF/OFF. Legacy remains isolated and untouched.

## One physical Git writer

M7c does not introduce another promoter. The physical promotion effect remains exactly M7a:

```text
M6c current validation/policy authority
        ↓
M7c exact flow + capability/evidence binding
        ↓
M7a exact ref CAS
        ↓
Git ref observation = physical truth
```

M7b remains a second, separate effect for synchronizing one attached local checkout. A Git ref proven `AFTER` never implies checkout synchronization `AFTER`.

## Cutover enforcement

M7c installs a durable cutover marker for an exact M6c flow and an isolated `refs/bdb-vnext/...` namespace. The marker binds:

- active M6c flow revision;
- M6c policy digest;
- M6b CheckPlan digest;
- checker registry digest;
- exact effect scope;
- target ref prefix;
- cutover state `ACTIVE` or `PAUSED`.

M7a itself reads this marker immediately before CAS.

If a matching marker exists:

- `ACTIVE` requires the exact canonical M6c authority and exact M7c Evidence binding;
- `PAUSED` blocks promotion;
- missing M6c wiring blocks promotion;
- stale M6c flow/policy/plan/scope blocks promotion;
- there is no fallback to the pre-cutover M6a-only path.

The old M6a-only branch remains reachable only when no M7c marker exists, preserving the already-proven standalone M7a build-only contract before cutover.

## Exact promotion binding

M7c preparation accepts semantic capability-to-Evidence bindings, not a validation profile:

```text
capability_id
→ obligation_id
→ evidence_id
```

The capability set must exactly equal the active M6c flow. M7c derives from M6c rather than accepting caller overrides for:

- validation policy digest;
- CheckPlan digest;
- scope;
- obligation set.

The resulting M7a effect and M7c binding are immutable identities.

## Immediate authorization before CAS

On every M7a call under an active M7c cutover, M7a:

1. reconciles current Git ref truth;
2. verifies active Run/lease/fence/resource claim;
3. verifies current sealed Candidate;
4. verifies the M7c cutover and binding;
5. invokes current M6c authorization for the exact effect;
6. only after `ALLOW`, crosses durable `POSSIBLE` and performs exact `git update-ref <new> <old>`;
7. observes Git again and concludes from physical ref truth.

M6c itself re-observes current Evidence/Candidate applicability before `ALLOW`. Historical PASS, a watcher, a delivery receipt or a previous approval is therefore insufficient by itself.

## Crash and divergence

M7c does not add retry logic. M7a keeps its proven effect semantics:

- crash after durable `POSSIBLE` but before CAS → observe before any next call;
- crash after successful ref CAS but before close → observe `AFTER`, no second CAS;
- third ref OID → `DIVERGED`, never overwrite;
- ambiguous ref observation → reconciliation required;
- no force update or automatic ref undo.

M7b independently applies the same observe-before-retry discipline to checkout synchronization.

## Kill switch

`pause_cutover(flow_id)` changes the canonical M7c marker to `PAUSED`. A paused cutover fails closed inside M7a; it does not resurrect the earlier M6a-only path. Roll-forward reactivation binds the current M6c flow revision.

## Query

M7c query returns separate projections for:

- cutover state;
- exact M7c binding;
- M7a source promotion truth;
- M7b checkout synchronization truth or explicit `NOT_PREPARED`.

This prevents `ref AFTER` from being presented as `checkout AFTER`.

## Explicitly out of scope

M7c does not:

- push a remote;
- target production refs;
- deploy Shopify/LIVE;
- force-update a ref;
- merge/reset/stash a checkout;
- make watcher/seen/sequence/delivery state authoritative;
- activate vNext production runtime;
- drain or delete legacy during this build-only closure.

Those production cutover/deletion actions remain later gates. M7c closes the internal P2 validation/policy + Git CAS architecture only.

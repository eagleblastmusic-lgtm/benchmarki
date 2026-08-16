# BDB vNext M7b — Separate Checkout Synchronization Effect

Status: build-only implementation contract. Production runtime/writer/activation remain OFF/OFF/OFF.

## Purpose

M7a and M7b deliberately own different physical truths.

- M7a prepares immutable Git objects and performs an exact CAS on one isolated `refs/bdb-vnext/...` ref.
- M7b synchronizes one attached local checkout index/worktree to the commit already proven `AFTER` by M7a.

A moved Git ref does **not** imply that the checkout is synchronized. The query surface therefore keeps `source_promotion.effect_certainty` and `checkout_sync.effect_certainty` separate.

## Authority boundary

M7b may:

- read the already-`AFTER` M7a effect;
- observe exact symbolic HEAD, HEAD OID, index tree, tracked worktree state and untracked paths;
- claim one exact `git-checkout:<digest>` Work Kernel resource;
- durably record `BEFORE -> POSSIBLE -> AFTER` for checkout synchronization;
- run only the bounded mechanical `git read-tree -u -m <old-tree> <new-tree>` synchronization;
- reconcile after crash by observing Git/filesystem truth before any retry.

M7b may **not**:

- create or move the promotion ref;
- use `git push`, `git reset`, `git merge` or a broad checkout as recovery;
- rewrite M7a promotion certainty;
- overwrite dirty staged, unstaged or untracked user state;
- treat a failed/unknown observation as permission to retry;
- deploy or mutate LIVE/Shopify state.

## Exact precondition

Automatic synchronization is allowed only when all of these are observed exactly:

1. symbolic `HEAD` equals the source M7a target ref;
2. `HEAD` resolves to the M7a prepared commit;
3. tracked worktree bytes match the current index;
4. no untracked paths exist;
5. index tree equals either the exact old tree (`BEFORE`) or exact new tree (`AFTER`).

Anything else is `DIVERGED` or `AMBIGUOUS` and fails closed.

This is intentionally conservative. In particular, M7b does not delete or hide unrelated untracked files to make a synchronization succeed.

## Crash semantics

The durable boundary is:

```text
exact observation = BEFORE
→ durable POSSIBLE
→ re-assert exact active Run / lease / fence / resource claim
→ bounded read-tree synchronization
→ observe exact checkout
→ AFTER
```

Crash after `POSSIBLE` but before the Git command leaves the old checkout observable as `BEFORE`; recovery may then perform the physical synchronization once.

Crash after the Git command but before durable close leaves the new checkout observable as `AFTER`; recovery closes the effect without issuing the physical synchronization again.

`DIVERGED` and `AMBIGUOUS` never trigger blind retry.

## Relationship to M7c

M7b is not the canonical promotion gate. M7c must later compose:

- M6 evidence/policy authority,
- deterministic M6b CheckPlan,
- M7a exact Git ref truth,
- M7b checkout synchronization truth,
- active policy/intent/generation/fence requirements,

without merging those authorities into one ambiguous status.

## Validation

Focused contracts run on both Linux and Windows and cover:

- M7a `AFTER` while M7b remains `BEFORE`;
- exact synchronization to `AFTER`;
- staged/unstaged/untracked preservation and fail-closed behavior;
- wrong symbolic HEAD;
- crash after `POSSIBLE`;
- crash after physical checkout update;
- replay after `AFTER` without a second physical synchronization;
- foreign change after prepare;
- lost prepare response / stable effect identity;
- independent query projection for M7a and M7b;
- absence of ref/push/reset authority in M7b.

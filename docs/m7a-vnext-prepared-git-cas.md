# M7a — Prepared Git CAS Adapter

Status: `BUILD-ONLY / ISOLATED VNEXT REFS / NO CUTOVER`

M7a adds the first vNext Git ref effect. It does **not** synchronize a checkout, push a remote, merge a branch, write a legacy promotion receipt, or activate production authority.

## Exact boundary

The adapter consumes one current sealed `CandidateRepoView` and prepares Git objects directly in the bound repository object database. Candidate replacement bytes come from Candidate CAS; unchanged objects come from the exact Candidate base commit. A temporary `GIT_INDEX_FILE` is loaded from the Candidate base tree and is used only to calculate the new tree. The repository checkout and normal index are not read as promotion truth and are not mutated.

The prepared commit is a deterministic one-parent child of the exact Candidate base commit. Its tree is re-read from Git and must reproduce the Candidate semantic tree digest before an effect intent can be persisted.

M7a accepts only full refs below:

`refs/bdb-vnext/`

This restriction is deliberate for the build-only gate. `refs/heads/main`, deployment refs and legacy production refs are rejected.

## Durable effect identity

There is exactly one M7a Git effect row per canonical `WorkItem`. It lives in the same vNext Control DB and binds:

- Work / Task / Run / lease / fence;
- exact Candidate view and Candidate tree digest;
- repository identity and object format;
- prepared tree OID and commit OID;
- exact target full ref and expected old OID;
- deterministic commit metadata policy;
- exact M6a Candidate subject digest;
- intent revision, validation policy digest and M6b CheckPlan digest;
- M6a obligation IDs and scope;
- Work Kernel resource claim for the exact repository/ref resource.

No new lifecycle primitive, legacy receipt, repository sequence or watcher state is introduced.

## Ref effect protocol

The physical ref protocol is:

1. prepare immutable blobs/tree/commit without checkout mutation;
2. persist the exact effect intent as `BEFORE`;
3. re-observe the target ref;
4. re-evaluate the exact current M6a obligation/approval gate using the prepared `effect_id`;
5. durably mark `POSSIBLE`;
6. execute exactly one `git update-ref <full-ref> <new> <expected-old>` compare-and-swap;
7. observe Git ref truth and classify it.

Observation classes are:

- expected old OID (or exact absence when the expected old is the null OID) → `BEFORE`;
- prepared commit OID → `AFTER`;
- any third OID → `DIVERGED`;
- inability to observe exact ref truth → `AMBIGUOUS`.

A failed external call is never retried inside the same invocation. M7a first observes ref truth. If the old OID is still exact, a later invocation may retry after another observation; if a third OID is present, automatic retry is prohibited.

## Recovery

A crash after `POSSIBLE` but before `update-ref` leaves the ref at the expected old OID. Reconciliation proves `BEFORE` before a later retry.

A crash after the successful ref update but before the response/observation leaves the durable record at `POSSIBLE`; reconciliation observes the prepared commit and concludes `AFTER` without a second ref update.

A concurrent ref advance produces a third OID and therefore `DIVERGED`. M7a never resets, force-updates or silently overwrites it.

Immutable objects written before the durable intent may remain unreachable after a crash; they are not authority and do not move a ref. Repeating preparation is deterministic and safe.

## Evidence / policy

M7a does not replace M6a or M6b. The exact `effect_id` includes the M6b CheckPlan digest. Immediately before the ref effect, M7a calls the existing M6a `EvidencePolicyGate` with the exact Candidate subject, intent revision, prepared effect digest, validation-policy digest and scope. Missing, stale, wrong or expired approval blocks before `POSSIBLE`.

M6c remains responsible for the later canonical promotion gate cutover. M7a alone does not make vNext the production authority.

## Explicitly out of scope

- checkout synchronization — M7b;
- remote push;
- `git merge`, `git reset`, `git clean`, stash or force update;
- production/default branch mutation;
- legacy receipt/seen/sequence authority;
- deployment or Shopify mutation;
- runtime/writer/activation cutover.

Production runtime / writer / activation remain `OFF / OFF / OFF`.

Next: **M7b — checkout synchronization as a separate witnessed effect**.

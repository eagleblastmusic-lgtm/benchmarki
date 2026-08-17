# M11b Activation Fault Matrix — closure record

Status: **M11b CLOSURE CANDIDATE / SUBSTANTIVE GATES PASS / BUILD-ONLY / NOT ACTIVATED**

M11b tests the activation algorithm required by Sequence 38 without granting any production activation authority. It consumes one exact M11a prepared activation, snapshots it into an isolated experiment root, executes only disposable pointer/start/health/conclusion boundaries, injects failures, and performs cold-restart recovery classification.

## Exact substantive closure basis

- canonical base: M11a merge `95ca2de599b51ab38ed965b6dd9d44ecfb3c25f0`;
- substantive M11b head before this documentation-only closure commit: `1b7c2c18643ab1b350daf79e58d810504aeda198`;
- substantive tree: `2d13b12cd1a078df086cd6de5ef09150a1c164d6`;
- production Browser/Native/runtime/writer/intake activation remains OFF;
- Legacy remains an independent side-by-side product;
- experiment root cannot overlap M11a authority, immutable bundles, backup or recovery target.

## Executed gate evidence

On the exact substantive subject above:

- `M11b Activation Fault Matrix` run `32059934838`: Ubuntu PASS + Windows PASS;
- `M11b Windows Fault Stress` run `32059934868`: Windows PASS;
- `M11b Windows Evidence` run `32059934843`: Windows test matrix PASS, witness inventory PASS, artifact publication PASS;
- retained evidence artifact id `9297767894`, size `461602` bytes, digest `sha256:eb1cd2b05f4682460ed4e5ffd32eb94985ec897febc37a6f4584492ac233d1ac`, 30-day retention;
- trusted `BDB vNext CI` on the same substantive source is required as an independent portable regression gate; the documentation-only closure head must receive all applicable final checks again before merge.

The immediately preceding implementation round also proved the same fault matrix and trusted portable regression before the one-line evidence-upload workflow correction. The final source change before this closure record only enabled hidden-file inclusion for the retained evidence artifact.

## Activation phases covered

The machine-readable catalog covers every frozen phase:

- `STAGE` — exact/capability/self-activation failures reuse proven M11a prerequisite evidence;
- `BACKUP` — copy crash, disk-full, publication interruption and DB/WAL corruption reuse M1b/M11a recovery evidence;
- `MIGRATE` — explicitly `NOT_APPLICABLE`: current Next switch has no data/schema migration boundary; compatibility is machine-checked before switch;
- `SWITCH` — crash after intent, crash after pointer publication, torn/corrupt final pointer, atomic publication denial and Windows exclusive-handle unavailability;
- `START` — failed start and crash after start request;
- `HEALTH` — failed/timeout health, health ACK loss, stale false-positive health and corrupt/unavailable PREVIOUS;
- `CONCLUDE` — crash before/after durable conclusion;
- `CONCURRENCY` — second activation writer and stale prepared subject.

No catalog cell permits an `AMBIGUOUS` terminal class.

## Allowed terminal classes

Every executable cold restart resolves only to one of:

- `KNOWN_GOOD_ACTIVE`;
- `KNOWN_GOOD_CANDIDATE`;
- `RECOVERED_PREVIOUS`;
- `BLOCKED_QUARANTINED`.

There is no implicit Legacy fallback, recency guess, regenerated activation identity or dual-authority recovery.

## Durable experiment boundaries

The disposable harness records:

1. `INITIALIZED` — exact M11a preparation snapshot + original ACTIVE pointer;
2. `SWITCH_INTENT` — exact from/to/recovery manifests;
3. atomic experiment pointer publication to CANDIDATE;
4. `START_REQUESTED` with a durable `start_success` observation;
5. `HEALTH_VERIFIED` from a fresh independent bundle health observation;
6. `CONCLUDED`.

A hard crash can be injected after every boundary. Cold recovery re-opens disk state and revalidates the exact subject rather than trusting in-memory progress. A durable failed-start observation cannot be reinterpreted as success merely because a later candidate health probe happens to answer.

## Windows platform mechanics

Dedicated hosted-Windows gates include:

- real child-process termination with exit code 91 after durable boundaries;
- real exclusive Windows handle on the experiment pointer;
- typed `pointer_unavailable` classification while the final pointer cannot be observed;
- proof that the old ACTIVE pointer remains exact after the handle is released;
- cold recovery after lock/crash;
- repeated stress execution of disposable crash/lock cases;
- replay of M11a Windows TCB, M1b backup and Control Store seal read/write race contracts;
- retained, SHA-256-inventoried durable witnesses uploaded as a GitHub Actions artifact.

These are disposable fixtures. They do not touch the user's ProgramData, registry, Chrome, Native Host registration or production runtime.

## Safety boundary

M11b contains no production activation function. It never calls M9b `activate_generation`, never changes production M11a ACTIVE, never enables M3c intake/writer, and never registers/starts Browser or Native clients. Production activation remains M11c-only after M11b PASS.

## Failure semantics proven

- pointer intent without publication leaves ACTIVE known-good;
- candidate pointer without a durable successful start recovers PREVIOUS or blocks;
- a durable failed start recovers PREVIOUS or quarantines even if a later candidate probe would otherwise look healthy;
- ACK loss after start is resolved by a fresh candidate health observation;
- stale/false-positive health cannot override a fresh negative observation;
- candidate + PREVIOUS failure quarantines deterministically;
- corrupt/torn pointer never falls back by recency;
- Windows file-lock observation failure is typed and cannot silently become a switch success;
- a second activation writer is blocked by the external experiment lock;
- stale M11a preparation blocks the experiment before switch;
- prerequisite backup/Control Store failures remain fail-closed.

## Closure decision

The substantive M11b implementation satisfies the frozen fault-matrix acceptance on the exact subject recorded above. This documentation-only closure commit must receive the dedicated M11b and trusted `BDB vNext CI` gates before PR #87 is marked ready and merged.

When those final checks are green, M11b status becomes:

> **PASS / BUILD-ONLY / NOT ACTIVATED**

Next unit: **combined M9b + M11c production cutover preparation under External Bootstrap authority**. Actual user-machine activation still requires a fresh production inventory/preflight and remains a separate explicit effect boundary.

# M11b Activation Fault Matrix — source candidate

Status: **EXPERIMENT SOURCE CANDIDATE / NOT PRODUCTION ACTIVATION / M11b NOT YET CLOSED**

M11b tests the activation algorithm required by the frozen Sequence 38 without granting any production activation authority. It consumes one exact M11a prepared activation, snapshots it into an isolated experiment root, executes only disposable pointer/start/health/conclusion boundaries, injects failures, and performs cold-restart recovery classification.

## Exact basis

- canonical base: M11a merge `95ca2de599b51ab38ed965b6dd9d44ecfb3c25f0`;
- production Browser/Native/runtime/writer/intake activation remains OFF;
- Legacy remains an independent side-by-side product;
- experiment root must not overlap M11a authority, immutable bundles, backup or recovery target.

## Activation phases covered

The machine-readable catalog covers every frozen phase:

- `STAGE` — exact/capability/self-activation failures reuse proven M11a prerequisite evidence;
- `BACKUP` — copy crash, disk-full, publication interruption and DB/WAL corruption reuse M1b/M11a recovery evidence;
- `MIGRATE` — explicitly `NOT_APPLICABLE`: current Next switch has no data/schema migration boundary; compatibility is machine-checked before switch;
- `SWITCH` — crash after intent, crash after pointer publication, torn/corrupt final pointer, atomic publication denial;
- `START` — failed start and crash after start request;
- `HEALTH` — failed/timeout health, health ACK loss, stale false-positive health and corrupt/unavailable PREVIOUS;
- `CONCLUDE` — crash before/after durable conclusion;
- `CONCURRENCY` — second activation writer and stale prepared subject.

No catalog cell permits an `AMBIGUOUS` terminal class.

## Allowed terminal classes

Every executable cold restart must resolve only to one of:

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
4. `START_REQUESTED`;
5. `HEALTH_VERIFIED` from a fresh independent bundle health observation;
6. `CONCLUDED`.

A hard crash can be injected after every boundary. Cold recovery re-opens disk state in a new process or call and revalidates the exact subject rather than trusting in-memory progress.

## Windows platform mechanics

Dedicated hosted-Windows tests include:

- real child-process termination with exit code 91 after durable boundaries;
- real exclusive Windows handle on the experiment pointer to reproduce AV/file-lock publication denial;
- cold recovery after the lock/crash;
- replay of the M11a Windows TCB, M1b backup and Control Store seal read/write race contracts.

These are disposable fixtures. They do not touch the user's ProgramData, registry, Chrome, Native Host registration or production runtime.

## Safety boundary

M11b contains no production activation function. It never calls M9b `activate_generation`, never changes production M11a ACTIVE, never enables M3c intake/writer, and never registers/starts Browser or Native clients. Production activation remains M11c-only after M11b PASS.

## Gate to close M11b

M11b may close only when:

- dedicated `M11b Activation Fault Matrix` passes on Ubuntu and Windows;
- trusted `BDB vNext CI` passes on the exact same head;
- every machine-readable phase is covered or explicitly `NOT_APPLICABLE` with rationale;
- every executable fault cell resolves to an allowed deterministic terminal class;
- the real Windows lock/hard-crash tests pass;
- prerequisite M11a/M1b/Control Store recovery regressions remain green;
- production activation remains OFF.

On PASS, next unit is **M11c Bootstrap Authority + BDB Next production cutover preparation**. Actual user-machine activation still requires a fresh production preflight and explicit cutover boundary.

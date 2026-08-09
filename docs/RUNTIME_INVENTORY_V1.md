# BDB runtime-inventory-v1

`runtime-inventory-v1` is the reusable provider and CLI established by Execution Unit R0a. It produces bounded evidence about explicitly declared BDB sources. It is not a lifecycle database, a repair tool, or a decision that a production mutation is safe.

## Authority and side effects

Authority remains in Git/filesystem, deployed component manifests, the Journal, Native receipts/spool, promoter/Git, and OS process observations. The report only says what was observed during its recorded interval.

Collectors never call `Journal.open()`, acquire an instance lock, reserve a receipt, fetch/claim/archive a spool entry, acknowledge Browser state, run a promoter, checkpoint WAL, repair a store, or start/stop BDB. Git runs with optional locks disabled. Journal, WAL, and SHM bytes are copied to an explicit scratch root; SQLite opens only the copy with `mode=ro` and `PRAGMA query_only=ON`.

The only writes performed by the CLI are:

- private SQLite copies under `--scratch-dir`, removed before the provider returns;
- explicitly requested report artifacts, written through a temporary file plus `fsync` and atomic replace.

Report outputs are rejected when they overlap an observed repository, runtime, store, or deployed bundle root.

## Supported source formats

- Git worktree identity/status at the explicitly supplied repository;
- Bridge config schema `1.1`;
- BDB source package/module identity and service/Native runtime `0.4.7` at this revision;
- Browser Manifest V3 bundles;
- Journal migrations v1–v12 with exact names and checksums;
- Native receipt store `bdb-native-request-receipts-v1`;
- local spool envelopes `bdb-local-envelope-v1`;
- promoter state `bdb-workspace-promoter-state-v1`;
- promotion receipts `bdb-workspace-promotion-v1` and repository sequence v1;
- optional deployed Native Host and Browser bundle descriptors.

Any future/unknown schema is `UNSUPPORTED`; malformed content or failed integrity/checksum/containment is `INVALID`; missing, busy, permission-denied, or timed-out input is `UNAVAILABLE`; an identity change between pre/post observation is `UNSTABLE`.

## Completeness and result semantics

Every source contains:

- exact identity and observation interval;
- one status: `OBSERVED`, `UNAVAILABLE`, `UNSTABLE`, `INVALID`, or `UNSUPPORTED`;
- explicit `complete` and `truncated` fields;
- bounded facts and typed errors.

The overall result is one of:

- `READY_FOR_LOCAL_GATE` — required R0a observations are complete and no cross-source conflict was found;
- `INCOMPLETE` — a required observation is missing/unstable/truncated or correlation requires deeper inspection;
- `INVALID` — an observed required source violates its supported format/integrity/containment;
- `UNSUPPORTED` — a required source version is outside this reader contract.

`READY_FOR_LOCAL_GATE` only permits the separate R0b operator/local gate. `overall.safe_to_mutate` is always `false`. Missing/error is never converted to clean or empty.

Receipt-without-spool, spool-without-receipt, promoter/ref disagreement, repository/config binding disagreement, Native/Bridge binding or origin disagreement, and deployed/source bundle disagreement remain explicit blockers with bounded IDs. Active declared service PIDs are findings, not a global process census. Each active PID is sampled twice with a process-creation token; disappearance or PID reuse during the observation is `UNSTABLE`.

## Determinism and privacy

Canonical JSON uses UTF-8, sorted keys, and fixed separators. The semantic digest excludes inventory/run IDs, observation timestamps, durations, filesystem timestamps, and localized error messages. It includes platform/runtime identity, source identities, versions, content digests, completeness, truncation, typed error codes, and correlation blockers.

The private report retains exact paths and bounded stable IDs. A sanitized report is a deep-copy projection: secret-key fields and error messages are redacted, while paths, filenames, extension origins, Git object IDs, and operational IDs receive report-scoped pseudonyms. Pseudonyms are linkable within the same semantic report and are not claimed to be anonymous. Creating the sanitized export never mutates the private report.

Directory traversal and record collection stop at declared caps. A cap produces explicit truncation and prevents readiness; partial data is never used for cross-source correlation. Membership is checked again after each complete directory observation so concurrent add/remove/rename is `UNSTABLE`.

## CLI

```powershell
bdb-inventory `
  --repository C:\path\to\repo `
  --bridge-config C:\path\to\bridge-config.json `
  --native-config C:\path\to\native-host.json `
  --browser-bundle C:\path\to\deployed-extension `
  --native-manifest C:\path\to\com.bartosz.dev_bridge.json `
  --scratch-dir C:\path\to\private-scratch `
  --private-report C:\path\to\reports\inventory-private.json `
  --sanitized-report C:\path\to\reports\inventory-sanitized.json
```

Use `--json` to print the private machine-readable report. Otherwise the CLI prints a concise human summary. Exit codes are `0` ready for R0b, `2` incomplete, `3` invalid, `4` unsupported, and `1` for invocation/output failure.

## Explicit omissions

R0a does not export Browser storage/history, terminal payload history, full receipts/results/archive history, global process topology, dependency inventories, package credentials, source blobs, prompts, conversations, or secrets. It performs no R0b disposition, cleanup, drain, reconciliation, install, migration, Control Center, Bootstrap, or Event Ledger expansion.

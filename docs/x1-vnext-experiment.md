# X1-vNext — Dedicated SQLite Authority / Durability Gate

This is a build-only experiment for the isolated vNext Control Store. It does
not read or write the frozen legacy runtime, does not register Browser/Native
clients, and does not create a user-facing vNext runtime.

## Hypotheses and falsifiers

The following hypotheses were recorded before the X1 harness implementation.
The experiment may return `PASS`, `FAIL`, or `INCONCLUSIVE`; the harness must
preserve an evidence-backed negative result.

| ID | Hypothesis | Falsifier / explicit negative evidence |
|---|---|---|
| H1 | One canonical writer owns the writer boundary; a second writer cannot acquire the canonical writer lease, and a raw SQLite contender receives bounded busy/locked behavior while the lease holder is active. | A second canonical lease succeeds concurrently, a second writer commits through an alternate authority, or contention is unbounded/ambiguous. |
| H2 | Process-kill boundaries before mutation, after mutation before `COMMIT`, and after `COMMIT` before acknowledgement leave SQLite truth deterministic: uncommitted work is absent and committed work remains. | `PRAGMA integrity_check` fails, an uncommitted row becomes committed, a committed row disappears, or the boundary cannot be observed precisely. The harness does not claim to simulate physical power loss or a kill inside native `COMMIT`. |
| H3 | A real SQLite database in WAL mode can be backed up and restored with an exact DB/WAL subject set; corruption, truncation, or an inconsistent missing WAL is explicit, while a legally checkpointed absent WAL remains distinguishable. | Restore cannot open/integrity-check the database, DB/WAL identity is silently accepted after corruption, or legal WAL absence cannot be distinguished from loss of expected committed state. |
| H4 | The existing M1b external backup/restore floor is sufficient for a real SQLite fixture when the source is explicitly quiesced. | X1 requires a duplicate backup writer, an online hot-backup mechanism, or M1b restore cannot reopen and verify the real database. |
| H5 | After a killed writer/restart, SQLite is reopenable, integrity is green, committed state is present, uncommitted state is absent, and there is no second durable truth outside SQLite. | Reopen/integrity fails, application invariants disagree with SQLite rows, or recovery relies on a second store/legacy fallback. |
| H6 | Backup is allowed only at the M1b safe boundary (`source_is_quiesced=True`); the post-X1 requiredness decision is `control.db` required and WAL explicitly present or legally absent according to SQLite state. | A backup is accepted from an active writer, a missing DB is treated as valid post-X1 state, or WAL absence is treated as an unconditional failure despite a verified checkpointed state. |

## Experiment boundary

- Real `sqlite3` files, schema, transactions, WAL, subprocesses, and Windows
  file locking are required.
- The minimal schema is experiment-only and is not the production Control Store
  schema. It contains only the records and metadata needed for application
  invariants.
- Process-kill evidence is limited to boundaries the harness can coordinate;
  physical power-loss and an interruption inside the native SQLite commit are
  explicitly outside the claim.
- All databases, bundles, locks, backups, and restore targets are disposable
  temporary resources outside legacy and the user-facing vNext runtime.
- No activation pointer, dual writer, migration framework, ORM, lifecycle
  schema, or Browser/Native registration is part of X1.

## Evidence capsule — 2026-08-09

The isolated Windows/NTFS capsule returned `PASS`.

Environment and settings:

- Windows `win32`, Python `3.14.4`, SQLite `3.50.4`;
- journal mode `WAL`, `synchronous=FULL`, `wal_autocheckpoint=0`,
  `foreign_keys=ON`, bounded `busy_timeout=250ms`.

Crash/restart boundaries actually exercised with a killed subprocess:

| Boundary | Expected after reopen | Observed | Integrity | Recovery write |
|---|---|---|---|---|
| before `BEGIN` | no crash token | none | `ok` | committed |
| after `BEGIN`, before mutation | no crash token | none | `ok` | committed |
| after mutation, before `COMMIT` | no crash token | none | `ok` | committed |
| immediately before native `COMMIT` | no crash token | none | `ok` | committed |
| after `COMMIT`, before acknowledgement | crash token | present | `ok` | committed |

The experiment does not claim physical power-loss or interruption inside the
native SQLite `COMMIT` call.

Single-writer/contention evidence:

- canonical contender: blocked by the external writer lease with
  `concurrent_attempt`;
- raw SQLite contender: bounded `database is locked` / `busy`;
- no second authority committed; post-contention `PRAGMA integrity_check` was
  `ok`.

M1b integration evidence:

- real WAL backup manifest:
  `sha256:69a59df0a017c970a5f941bc65eb195b93917f78bc58efe7e1dde31b810a650c`;
- restored real SQLite integrity: `ok`, committed token `wal-committed`;
- restore receipt:
  `sha256:9a6dbdf04466ddf14df078368ad38b9f72c2a8f152bc142f5c652b5a3d0da63c`;
- checkpointed fixture backup manifest:
  `sha256:73bde851a83039517a31d2953236c9f0a5b68b16311b65cbee98739e61902585`;
- checkpointed restore receipt:
  `sha256:3500ad5bd2a1adc0fcc3b7f833cc27e48822519e03365ddd757a4e44b70a8f2f`;
- both restored databases reopened with `PRAGMA integrity_check = ok` and the
  expected application invariants.

Fault results (observed exact codes):

| Tamper case | Observed failure code |
|---|---|
| `missing_wal` | `backup_integrity_failure` |
| `truncated_db` | `backup_integrity_failure` |
| `truncated_wal` | `backup_integrity_failure` |
| `corrupt_db` | `backup_integrity_failure` |
| `corrupt_wal` | `backup_integrity_failure` |

Every missing, truncated, or corrupt DB/WAL case was explicitly rejected.
The missing-subject restore attempt was also fail-closed with
`backup_integrity_failure`.
- no silent fallback, second DB, legacy access, or production activation.

Post-X1 storage decision:

- `control.db` is `REQUIRED_PRESENT` for post-X1 canonical backups;
- `control.db-wal` is an optional subject: `present_and_verified` when an
  uncheckpointed WAL is part of the verified state, otherwise explicitly
  `declared_absent` only after the SQLite state is verified as legally
  checkpointed;
- `content/` and config remain declared subjects and were not made domain
  schema by X1.

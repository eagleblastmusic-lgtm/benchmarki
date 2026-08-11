# M3a vNext — Submission + Task Substrate (Shadow)

M3a is a build-only, disposable vNext substrate. `ShadowSubmissionStore` is
opened only with an explicit `shadow=True` assertion and writes a dedicated
`control/m3a-shadow.db` under the caller-provided isolated root. It is not
registered in the production composition root and does not use the legacy
Journal, Session, Command, receipt, spool, Browser ledger, or Native Host
state.

The canonical request generation is `bdb-vnext-canonical-request-v1`. Its
sorted canonical JSON representation is digested with the shared
`bdb_shared.evidence.semantic_digest` helper. An opaque submission key and
that exact digest are inserted atomically with one Task, one immutable intent
revision, and one conversation/consumer binding. A replay with the same key
and digest returns the existing admission. A different digest is an explicit
`submission_conflict`; unsupported generations, invalid canonicalization,
digest mismatch, stale intent revision, and retained tombstones fail closed.

The store is a test writer only. It has no production acceptance route, no
dual-write marker, no TTL that can reopen an effect, and no lifecycle entities
such as WorkItem, Run, Wait, Lease, Fence, scheduler, result subscription, or
Git effect. SQLite WAL/FULL settings and a bounded busy error are exercised by
the focused M3a tests. Crash points cover rollback before commit and replay
after a committed response loss. Disk-full injection is not claimed because
the fixture has no faithful disk-full harness.

## Validation

`tests/test_vnext_m3a_submission.py` covers deterministic/versioned
canonicalization, replay/conflict, concurrent same/same and same/different,
pre- and post-commit crash boundaries, retained tombstones, stale revisions,
busy handling, explicit shadow/legacy-overlap guards, and the absence of
legacy receipt/spool/lifecycle tables.

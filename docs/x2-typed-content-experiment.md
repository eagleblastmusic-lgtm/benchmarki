# X2 — Typed Content Durability Gate

**Status:** `PASS`
**Execution basis:** `bdb-vnext` after accepted parent
`7fe97f484b2009043d322e877e14442cb8d918be`
**Generation:** BDB Next 1.0 fixture-only, build-only

This document preserves the hypotheses/falsifiers recorded before
implementation and the evidence from the final real Windows capsule. X2 did
not create a production Content Store, activate runtime, register Browser or
Native components, add lifecycle/domain schema, or access BDB Legacy.

## Hypotheses and falsifiers recorded before implementation

| Hypothesis | Claim | Falsifier | Observed result |
|---|---|---|---|
| H1 — atomic publication | A ref becomes committed only after complete bytes are flushed, verified and published. | A pre-publication failure leaves a committed ref resolving to partial bytes, or the publication boundary is nondeterministic. | `PASS`: all injected pre-publication failures remained uncommitted; post-ref acknowledgement loss resolved exact bytes. |
| H2 — raw integrity | Resolve verifies exact raw digest and rejects missing, truncated, mutated or mismatched bytes. | Any such bytes are returned as valid content. | `PASS`: exact failure matrix below. |
| H3 — semantic type/schema integrity | Semantic identity is domain-separated by explicit type and schema; raw digest/path never infers semantic identity. | Same raw bytes under a wrong type/schema resolve successfully, or a malformed ref is accepted. | `PASS`: same raw digest had different semantic digests; wrong type/schema and malformed ref failed closed. |
| H4 — concurrent writers | Same-object writers converge only on exact identical bytes; conflicting identity/write is fail-closed. | Corruption, two authoritative byte variants, or overwrite of a committed object occurs. | `PASS`: `published` + `converged`; conflicting writer returned `content_ref_integrity_failure`. |
| H5 — orphan handling | Temp/orphan bytes after a pre-commit failure are deterministic non-committed evidence. | An orphan is returned as committed truth or classification is ambiguous. | `PASS`: temp/orphan classes were explicit and resolver was blocked. |
| H6 — backup/restore | Existing M1b coordinated backup/restore preserves exact committed content identity and detects missing/tampered content. | Restore reports success while the ref resolves to missing/wrong bytes, or tampered backup content is accepted. | `PASS`: valid identity survived; all content tamper cases and post-publish restore tamper failed closed. |

## Exact fixture contract

`ContentRef` has exactly these JSON fields:

```text
type
schema
semantic_digest
raw_digest
```

The tested domains are intentionally finite X2 fixtures:

- `type=text/plain`, `schema=x2-text-v1`: strict UTF-8 semantic value;
- `type=application/octet-stream`, `schema=x2-bytes-v1`: base64 semantic
  value.

`raw_digest` is `sha256` of the exact stored bytes. `semantic_digest` is
`sha256` of canonical JSON containing the X2 semantic domain, explicit type,
explicit schema and the domain-specific semantic representation. Thus identical
raw bytes may be shared by two typed refs, but their semantic identities differ.
The raw digest or a filename is never sufficient authority; resolution checks
the exact four-field ref, metadata binding, raw digest and semantic digest.

## Minimal disposable layout and publication

```text
content/objects/<raw_digest_hex>.bin
content/refs/<semantic_digest_hex>.json
content/tmp/*.partial
config/bdb-vnext.json                 # fixture-only ref capsule for M1b
```

Object and ref bytes are written to a flushed/fsynced temporary file and
published on the same volume with atomic `os.replace`. A committed target is
never replaced with different bytes; an exact duplicate converges. Temp files
and an object published before its ref are classified, never treated as
committed truth. No production GC or generic storage abstraction was added.

## Windows/filesystem environment

The final CLI capsule used real disposable files on:

- platform: `Windows-10-10.0.19045-SP0`;
- OS: `nt`;
- Python: `3.14.4`;
- real `fsync`, same-volume atomic `os.replace`, concurrent fixture writers;
- NTFS directory junction reparse-point guard (`reparse_point`).

File-symlink creation was unavailable under the current Windows privilege, so
the equivalent real NTFS junction boundary was used and rejected by the same
component guard. Physical power-loss and process-kill were not simulated;
deterministic injected failure boundaries were observed instead.

## Publication and resolver fault matrix

| Case | Observed failure/classification | Resolver result |
|---|---|---|
| before temp write | `publication_failed_before_temp` | `content_ref_missing` |
| during partial temp write | `publication_failed_during_temp` | `content_ref_missing` |
| complete temp, before publish | `publication_failed_before_publish` | `content_ref_missing` |
| after object publish, before ref | `publication_failed_before_ref` | `content_ref_not_committed`; orphan object classified |
| after ref publish, before acknowledgement | `publication_ack_lost` | exact bytes resolved; no orphan remained |
| missing committed object | `raw_object_missing` | rejected |
| truncated committed object | `raw_integrity_failure` | rejected |
| mutated committed object | `raw_integrity_failure` | rejected |
| wrong raw digest | `content_ref_identity_mismatch` | rejected |
| same raw bytes / wrong semantic type | `semantic_type_schema_mismatch` | rejected |
| same raw bytes / wrong schema | `semantic_type_schema_mismatch` | rejected |
| malformed ContentRef | `malformed_content_ref` | rejected |
| path escape | `path_escape` | rejected |
| NTFS junction/reparse path | `reparse_point` | rejected |
| orphan temp object | deterministic `orphan_temp` classification | never committed |
| orphan object | deterministic `orphan_objects` classification | `content_ref_not_committed` |
| foreign content file | `content/unexpected.bin` classification | resolver authority unchanged |

## Concurrency evidence

- Two same-object writers produced exactly `published` and `converged`; the
  resolved bytes were exact.
- A conflicting writer using different bytes against the same ref failed with
  `content_ref_integrity_failure`; the valid writer remained exact.
- Same raw bytes committed under the two explicit semantic domains had equal
  raw digests, different semantic digests, and both exact typed resolutions.

## M1b backup/restore integration

The existing M1b `create_coordinated_backup`, `verify_backup` and
`restore_backup` APIs were reused; no second backup authority was introduced.

- backup manifest: `sha256:c46bcd13bdc1c483c5be8f36b2d8bb33cfc82d46235f410158e46582146c2dc9`;
- restore receipt: `sha256:de6e49a47be3da5a1cede48129eae08a4d76fd2ccbebe4647ed3ff4cab96a16b`;
- valid restore: `verified=true`, exact `ContentRef=true`, exact raw digest
  `true`, exact semantic digest `true`;
- content subject: `objects/9aa156948ee0a8c567c6f565430dc9abecdcea0f820c5fd072eab21e7e9e45b4.bin`;
- `missing_content`, `truncated_content`, `corrupt_content` and
  `foreign_file` backup tamper cases: each `backup_integrity_failure`;
- post-publish restore tamper: `restore_integrity_failure`.

## Authority, limitations and handoff decision

The capsule recorded:

```text
second_authority = false
production_store = false
runtime_activation = false
legacy_touched = false
```

Known limitations are explicit: the metadata/ref model is fixture-only; no
production GC, ontology, cache, ORM or M2b transport was implemented; physical
power-loss and process-kill were not simulated; semantic canonicalization covers
only the two named X2 fixture domains; no disk-full or permission fault was
claimed without a bounded deterministic mechanism.

The minimal mechanism that may inform M2b is the exact typed `ContentRef`
contract plus immutable object/ref publication, resolve-time raw/semantic
verification, deterministic orphan classification and reuse of the existing
M1b backup/restore integrity floor. No production Content Store is enabled by
this evidence.

**X2 decision: `PASS`.** M1c, M2b and all later EUs remain unstarted.

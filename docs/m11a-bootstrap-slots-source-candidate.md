# M11a Bootstrap Slots + Compatibility — closure record

Status: **M11a PASS / BUILD-ONLY / NOT ACTIVATED**

M11a evolves the existing M1b external bootstrap/recovery floor into the BDB Next external activation substrate required before M11b. It does not perform Browser/Native installation, runtime start/stop, writer/intake activation, or any `CANDIDATE -> ACTIVE` switch.

## Exact closure basis

- canonical base after M9b and Windows concurrency hardening: `ff565224c982aec62c5eb430bce79c261dbbac67`;
- M11a source merge head before this closure-record update: `8cbbe93a716e0a029396618a0963f2d74756a806`;
- M11a source tree at that gate: `9a48c2495c29c9511a73162245cb831d433c35ab`;
- prior substantive dedicated `M11a Bootstrap CI` run `32055362952`: Ubuntu PASS and Windows PASS, including real hosted-Windows SID/ACL readback;
- prior substantive trusted `BDB vNext CI` run `32055362910`: PASS;
- canonical Control Store seal-read hardening PR #85: focused contract + portable regression PASS on Ubuntu and Windows in run `32056199104`;
- production Browser/Native/runtime/writer/intake activation remains OFF.

This final closure-record commit must receive the same trusted/dedicated M11a gates before merge. The evidence above records the substantive implementation and the additional Windows concurrency fix now inherited from canonical `bdb-vnext`.

## External ACTIVE/PREVIOUS/CANDIDATE substrate

- content-addressed `ACTIVE` / `PREVIOUS` / `CANDIDATE` slot manifests;
- exact bundle digest and source-commit binding using the M1b bundle inspector;
- exact compatibility identity for protocol generation, Control Store schema, supported Control DB schema range, Content Store schema, and explicit capabilities;
- external slot state under the Bootstrap authority root, outside the vNext Control DB;
- read-only query re-observes backing bundle bytes and fails closed if they move;
- CANDIDATE staging/discard preserves the exact ACTIVE pointer;
- incompatible, hostile/self-activating or duplicate candidate staging is rejected;
- query contract always reports `activate_candidate = false` and `activation_deferred_to = M11c`.

## Prepared activation / recovery substrate

- immutable prepared-activation evidence outside the Control DB;
- exact binding to `slot_state_sha256` and current ACTIVE/PREVIOUS/CANDIDATE manifest digests;
- PREVIOUS is mandatory and independently inspectable/compatible;
- bounded PREVIOUS health witness is required before preparation completes;
- coordinated M1b runtime backup is published and independently re-verified;
- recovery target identity is recorded without mutating production runtime;
- post-backup slot re-observation proves ACTIVE/state did not move during preparation;
- stale slot state, moving bundle, failed PREVIOUS health, non-quiesced runtime and backup tamper all fail closed;
- no preparation operation can switch ACTIVE.

## Windows Bootstrap TCB

M11a defines an external Windows authority boundary at `%PROGRAMDATA%\BartoszDevBridge-Next\bootstrap`.

The machine-checked policy requires:

- protected ACL inheritance;
- owner = SYSTEM or BUILTIN\Administrators by SID;
- write-capable ACEs only for SYSTEM (`S-1-5-18`) and Administrators (`S-1-5-32-544`);
- ordinary Users (`S-1-5-32-545`) receive read/execute only;
- Authenticated Users / Everyone / Users cannot receive a write-capable allow ACE;
- runtime, Legacy and mutable candidate roots cannot overlap the authority root;
- candidate/runtime token contract is standard non-elevated;
- `candidate_may_write_authority = false` and `activation_operation_available = false`.

The dedicated Windows Actions job creates a real NTFS fixture, applies the SID-based ACL with `icacls`, reads it back through PowerShell, converts identities to SIDs and validates the resulting witness. This is platform-targeted M11a evidence, not a claim that the user's production machine has already been modified.

## External admin/actions contract

Installed entrypoint: `bdb-vnext-bootstrap-admin`.

M11a exposes exactly:

- `status` — revalidate exact external slot/preparation state;
- `verify-tcb` — verify ProgramData topology + real ACL witness;
- `prepare` — verify TCB first, then create prepared-activation evidence.

There is deliberately no `activate`, `switch`, install, start or stop verb. CI inspects the installed parser and module exports to prove the activation operation is absent. Effectful activation/start-stop authority belongs to M11c after M11b PASS.

## Failure ownership boundary

Handled in M11a:

- missing/corrupt/moved exact subjects fail closed through manifest/digest revalidation;
- compatibility mismatch blocks staging/preparation;
- concurrent authority mutation is serialized by external Bootstrap OS lock;
- coordinated Control Store config/seal publication is concurrency-safe on Windows, including bounded retry of transient final-seal sharing violations;
- backup, health and authority publication failures remain typed/blocking;
- stale preparation cannot be reused after slot/backup drift;
- candidate self-activation is rejected by bundle contract, actions surface and Windows authority boundary.

Deferred to M11b by design:

- kill/crash after every durable activation boundary;
- pointer torn-write/reboot/AV lock/disk-full matrix across an actual switch algorithm;
- start/stop/health-ACK loss and previous-corrupt recovery combinations;
- forward-only boundary fault classification.

Those are activation-fault experiments, not missing M11a source authority.

## Side-by-side product boundary

Legacy remains an independent product. The Bootstrap root and slot authority described here are BDB Next-specific. M11a does not disable, uninstall or reinterpret Legacy and does not grant Next authority over unrelated Legacy subjects.

## M11a DONE decision

M11a is closed when this final closure head receives the Windows/Ubuntu dedicated and trusted CI gates. A real user-machine `%PROGRAMDATA%` install/ACL/registry/Chrome change is intentionally **not** part of this build-only closure and must be freshly revalidated at the later M11c production cutover.

Next unit after final green closure CI: **M11b Activation Fault Matrix**.

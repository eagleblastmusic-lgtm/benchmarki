# M9a blocker-probe compatibility correction — 2026-08-17

Status: SOURCE CORRECTION / LOCAL REVALIDATION REQUIRED

This note preserves the authority boundary around the first local blocker-probe
run.  The run was useful for service, command-capability, spool and Native-arm
facts, but two shape classifiers were stricter than the exact legacy readers and
must not be used as evidence of legacy corruption.

## Bound source authority

Exact legacy source subject:

- repository: `eagleblastmusic-lgtm/bartosz-dev-bridge`
- commit: `4998aa16ff68d728637d09639ac79ced886393f6`
- `bdb_bridge/native_request_receipts.py`
- `bdb_bridge/workspace_promoter.py`
- `tests/test_workspace_promoter.py`

## First local probe identities

The first local read-only run produced:

- `bartosz-dev-bridge` probe digest
  `sha256:7f0285fa404c1ea5a745994c788199689f2b06795d61d4fdf9381d5ed91b30fd`
- `bdb-self` probe digest
  `sha256:66d245c261d18046af9e55ec41a7710b8aac8aebdb697327a94efd548abbb5d8`

Those reports remain valid evidence for the unaffected observations below, but
their `receipts.status` and promoter sequence-error conclusions are superseded by
this correction.

## Findings that remain valid

`bartosz-dev-bridge`:

- no Windows wake event was observed;
- one Journal service candidate existed, but its PID was not alive;
- the only nonterminal command capability was
  `ACKNOWLEDGEMENT_ONLY_ON_SERVICE_RESTART` (count 1);
- one spool envelope correlated with that unresolved `result_published` command;
- Native arm was present and effectively DISARMED;
- Chrome and Edge Native Messaging manifests were registered in HKCU.

`bdb-self`:

- no Windows wake event was observed;
- no Journal service candidate was present;
- the only nonterminal command capabilities were
  `ACKNOWLEDGEMENT_ONLY_ON_SERVICE_RESTART` (count 191) and `MANUAL_ONLY`
  (count 1);
- no `CLAIMED`, `EXECUTING`, `EFFECT_RECORDED`, or `RESULT_STAGED` capability was
  observed;
- 14 spool envelopes were classified `NEW_INGRESS_IF_SERVICE_RESTARTS`;
- Native arm was present and effectively DISARMED;
- Chrome and Edge Native Messaging manifests were registered in HKCU.

The first run therefore does not show a live legacy writer or a currently
write-capable recovery path.  It does show future ingress capability if the
legacy service is restarted while the 14 unjournaled spool envelopes remain in
the active inbox.

## Correction 1 — Native request receipt store

The first probe required both `requests` and `submission_reservations` to exist
as mappings.  Exact legacy `NativeRequestReceiptStore._read()` instead validates
`requests`, then executes:

`raw.setdefault("submission_reservations", {})`

Therefore an existing store containing only:

- schema `bdb-native-request-receipts-v1`; and
- a valid `requests` mapping

is a supported legacy-compatible shape.  Missing `submission_reservations` is
not corruption and must be projected as an implicit empty reservation mapping.
No legacy JSON repair is authorized by this finding.

## Correction 2 — Workspace promotion receipts

The current legacy writer adds `repository_event_seq` when writing a promotion
receipt.  However exact legacy `WorkspacePromoter._read_existing_receipt()` does
not require that field when accepting an already-existing receipt.  Historical
receipts that predate the sequence field therefore remain supported evidence.

The corrected classifier:

- validates the historical receipt identity accepted by the exact reader;
- classifies an absent `repository_event_seq` as `LEGACY_UNSEQUENCED`, not an
  error;
- validates a sequence when it is present;
- accepts a repository-event counter that is ahead of persisted receipt
  sequences because the writer advances the counter before replacing the
  receipt and a crash can leave a gap;
- rejects a counter that is behind an actually persisted sequenced receipt.

No promotion receipt rewrite is authorized by this finding.

## Corrected source candidate

Added on the M9a branch:

- `bdb_vnext/m9a_blocker_probe_compat.py`
- `scripts/run_m9a_blocker_probe_compat.py`
- `tests/test_m9a_blocker_probe_compat.py`

The correction reuses the original read-only SQLite/spool/service/Native
observation mechanics.  It does not import legacy runtime code into vNext,
execute commands, mutate legacy stores or registry state, create the final M9a
archive, or grant M9b activation.

## Next gate

Run focused tests and the compatibility probe on the real Windows installation.
If receipt/promoter compatibility is confirmed and the unaffected collision
facts remain stable, proceed to the one-way local M9a ingress freeze/archive.
The freeze must preserve exact archive evidence, quarantine active spool without
execution, close the legacy Native Messaging route, prevent a normal legacy
service restart from the live config paths, and prove a post-freeze zero-new-write
observation.  vNext must remain externally OFF throughout M9a.

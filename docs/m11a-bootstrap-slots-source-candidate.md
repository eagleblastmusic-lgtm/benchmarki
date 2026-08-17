# M11a Bootstrap Slots + Compatibility — source candidate tranches 1–2

Status: **SOURCE CANDIDATE / BUILD-ONLY / M11a NOT DONE**

These tranches implement the source-level core of Sequence 37 by evolving the existing M1b external bootstrap floor into an exact external slot and preparation substrate for BDB Next. They do not perform installation, runtime start/stop, Browser/Native registration, product activation, or any CANDIDATE -> ACTIVE switch.

## Added in tranche 1 — exact external slots

- external, content-addressed `ACTIVE` / `PREVIOUS` / `CANDIDATE` slot manifests;
- exact bundle digest and source-commit binding using the existing M1b bundle inspector;
- exact compatibility identity for protocol generation, Control Store schema, supported Control DB schema range, Content Store schema, and explicit capabilities;
- external slot state under the Bootstrap authority root, outside the vNext Control DB;
- read-only query that re-observes backing bundle bytes and fails closed if they move;
- bounded CANDIDATE staging and discard while preserving the exact ACTIVE pointer;
- explicit query contract showing `activate_candidate = false` and `activation_deferred_to = M11c`;
- focused tests for exact ACTIVE identity, staging, compatibility rejection, candidate self-activation rejection, moving bundle rejection, retained immutable evidence, and duplicate staging.

## Added in tranche 2 — prepared activation preflight

- immutable prepared-activation evidence outside the Control DB;
- exact binding to `slot_state_sha256` and the current ACTIVE/PREVIOUS/CANDIDATE manifest digests;
- PREVIOUS is mandatory for preparation and must remain independently inspectable and compatible;
- independent bounded PREVIOUS health witness before preparation can complete;
- coordinated M1b runtime backup published under the external Bootstrap authority root;
- backup re-verification after publication and on every prepared-activation query;
- recovery target identity recorded without restoring into or mutating the production runtime;
- post-backup slot re-observation proving ACTIVE and the prepared slot state did not move during preparation;
- prepared query fails closed if slot bytes, slot state, or backup identity become stale/tampered;
- explicit `activate_candidate = false`; no activation function is exported.

## CI authority

Trusted `BDB vNext CI` is now installed on the canonical `bdb-vnext` base. Pull requests to `bdb-vnext` are validated on GitHub-hosted Windows and Ubuntu runners. This replaces routine local operator test loops; local Windows execution is reserved for machine-specific runtime/registry/Chrome/ACL/cold-start evidence that GitHub-hosted runners cannot establish for the user's installation.

## Frozen safety properties

- candidate business logic never chooses the ACTIVE pointer;
- M1b `candidate_may_write_final_pointer = false` remains enforced for staged candidates;
- production activation remains `OFF`;
- prepared activation is evidence/preflight only, not authority to switch;
- Legacy remains a separate side-by-side product and is not disabled or reinterpreted by this unit;
- the external slot state records only BDB Next identities; it is not a global Legacy activation authority;
- mutable backing bundle bytes invalidate the slot on query instead of silently retaining authority;
- a stale/tampered backup cannot remain a valid prepared activation.

## Explicitly not done yet

- no supported-Windows permissions/ACL proof for the external Bootstrap authority root;
- no packaged external launcher/start/stop integration proof;
- no stale PID/process reachability proof against the real Windows packaging surface;
- no real platform test that candidate code cannot write Bootstrap authority files;
- no fault injection or activation crash matrix (M11b);
- no final activation pointer writer or CANDIDATE -> ACTIVE transition (M11c);
- no production install, registration, or cutover.

## Next M11a loop

Use trusted GitHub Actions for source/regression validation, then add the platform-targeted Windows Bootstrap authority/permissions/launcher preflight required by M11a DONE. Only after that proof is green should M11a be closed and M11b begin.

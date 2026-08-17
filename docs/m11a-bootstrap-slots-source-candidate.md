# M11a Bootstrap Slots + Compatibility — source candidate tranche 1

Status: **SOURCE CANDIDATE / BUILD-ONLY / M11a NOT DONE**

This tranche starts Sequence 37 by evolving the existing M1b external bootstrap floor into an exact external slot substrate for BDB Next. It does not perform installation, runtime start/stop, Browser/Native registration, product activation, or any CANDIDATE -> ACTIVE switch.

## Added in tranche 1

- external, content-addressed `ACTIVE` / `PREVIOUS` / `CANDIDATE` slot manifests;
- exact bundle digest and source-commit binding using the existing M1b bundle inspector;
- exact compatibility identity for protocol generation, Control Store schema, supported Control DB schema range, Content Store schema, and explicit capabilities;
- external slot state under the Bootstrap authority root, outside the vNext Control DB;
- read-only query that re-observes backing bundle bytes and fails closed if they move;
- bounded CANDIDATE staging and discard while preserving the exact ACTIVE pointer;
- explicit query contract showing `activate_candidate = false` and `activation_deferred_to = M11c`;
- focused tests for exact ACTIVE identity, staging, compatibility rejection, candidate self-activation rejection, moving bundle rejection, retained immutable evidence, and duplicate staging.

## Frozen safety properties

- candidate business logic never chooses the ACTIVE pointer;
- M1b `candidate_may_write_final_pointer = false` remains enforced for staged candidates;
- production activation remains `OFF`;
- Legacy remains a separate side-by-side product and is not disabled or reinterpreted by this unit;
- the external slot state records only BDB Next identities; it is not a global Legacy activation authority;
- mutable backing bundle bytes invalidate the slot on query instead of silently retaining authority.

## Explicitly not done in tranche 1

- no prepared-activation record bound to coordinated backup/reachability preflight;
- no Windows permissions/ACL packaging proof for the external authority root;
- no launcher/start/stop integration;
- no fault injection or activation crash matrix (M11b);
- no final activation pointer writer or CANDIDATE -> ACTIVE transition (M11c);
- no production install, registration, or cutover.

## Next M11a loop

Bind a prepared activation record to the exact ACTIVE/CANDIDATE manifests, coordinated M1b backup identity, recovery reachability, and machine-checked compatibility. Then add platform-targeted authority-root/launcher tests before M11b.

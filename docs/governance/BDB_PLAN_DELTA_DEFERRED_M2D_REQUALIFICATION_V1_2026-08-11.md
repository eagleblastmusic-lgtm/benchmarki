# BDB vNext — Plan Delta: Deferred M2d Requalification

**Status:** `PLAN FREEZE DISCREPANCY — ACCEPTED`
**Architecture Freeze v1:** `UNCHANGED`
**Architecture Reopen Required:** `NO`
**Effective basis:** `bdb-vnext @ 89390dc2e053517a97427ab0ae76cefc9c227682`
**Scope:** M2d → M3a/M3b dependency semantics only

This is a narrow, versioned governance delta. It does not restate or edit
Etap 3, Etap 5, Etap 6, Plan v1.1, Plan v1.1.1 or Plan v1.1.2. It does not
create a formal Execution Unit and does not start M3a or M3b.

## Scoped source precedence

For the one subject covered by this document — whether bounded shadow
construction of M3a/M3b may occur before fresh M2d qualification, and the
hard gate before M3c — precedence is:

1. fresh observed repo/runtime/external authority;
2. Etap 3 — Architecture Freeze v1;
3. this versioned delta;
4. Plan v1.1.2 for product/topology and late-stage semantics, except for the
   scoped dependency delta stated here;
5. Plan v1.1.1;
6. Plan v1.1;
7. still-applicable Etap 6 and Etap 5 guidance;
8. latest verified Execution Handoff as evidence/status, never governance
   authority.

Outside this exact subject, the established governance precedence is
unchanged. This delta does not supersede any architecture, isolation,
authority, Browser-first, or final-cutover invariant.

## Preserved historical state

- `M2D Attempt 2 = FAIL` remains immutable historical evidence.
- `M2D_ATTEMPT_2_FORMAL_RESULT = FAIL` is not converted to PASS.
- `M2C_REMEDIATION = ACCEPT_WITH_FINDINGS`.
- `M2C_REMEDIATED_UNREQUALIFIED` remains the current treatment status.
- No Browser rerun, Attempt 3, or quality requalification is implied here.

The M2c findings remain in force: several newly introduced negative guards
are not yet directly exercised by tests; empty/unbound affordances remain a
compatibility mode; a request may omit selection when no applicable bound
affordance exists; and precision is guaranteed only for properly gap-bound
affordances. `ContextAffordance` remains guidance, `ContextRequest` remains a
request, and `ContextResolution` remains the gap-closure authority boundary
with exact RepoView and accepted M2b evidence requirements.

## Accepted bounded exception

The frozen dependency was:

```text
M2c → M2d → M3a → M3b → M3c
```

This delta changes only the implementation eligibility of the first two
shadow units:

### M3a — allowed after this delta, but not started here

`M3a` may be constructed only as:

```text
SHADOW / BUILD_ONLY / ISOLATED / DISPOSABLE_OR_STATE_FORWARD
NO_PRODUCTION_ROUTE / NO_PRODUCTION_AUTHORITY
```

It may use only the dedicated vNext substrate. It may not import legacy
Journal/state, dual-write, accept a production Task, or become an admission
authority.

### M3b — allowed after bounded M3a completion

`M3b` may be constructed only as isolated Browser/Native mechanics for:

- Browser admission mechanics;
- durable outbox and lookup;
- capability handshake;
- transport/recovery substrate;
- test-only acceptance;
- a possible future actual Browser benchmark harness.

Production acceptance remains `OFF`. M3b must not silently fall back to
legacy, route production submissions, or activate Browser/Native as a product.

## Hard non-propagation rule

The exception is limited to M3a and M3b. It does not propagate through the
dependency graph. Until `M2D-RQ1 = PASS`, the following remain blocked:

```text
M3c
M4a and all later lifecycle descendants
production Task admission
vNext lifecycle writer activation
external Browser/Native product activation
M4f
all authority cutovers
M9b product activation
```

In particular, `M3a allowed` does not make `M4a allowed`. The ordinary
dependencies of those units remain intact; this is an explicit ceiling on the
temporary exception.

## M2D-RQ1 — ACTUAL BROWSER QUALITY REQUALIFICATION

The new hard checkpoint is not a formal EU and does not rewrite M2d history.
Its graph position is:

```text
M3b
  ↓
M2D-RQ1 — ACTUAL BROWSER QUALITY REQUALIFICATION
  ↓
M3c Basis Check / READY
```

`M3c` cannot receive `READY` without `M2D-RQ1 = PASS`.

PASS requires an actual normal ChatGPT Browser paired benchmark, not an API
run, synthetic answer, transport-only unit test, or historical evidence reuse.
The evidence must bind:

- the repaired M2c treatment and exact implementation commit;
- exact RepoView and ContextPackage identities;
- the same model and reasoning setting across comparison arms;
- raw Browser answers and formal run records;
- credible model/settings/timing attestations;
- the frozen paired rubric without post-hoc changes;
- no small/mechanical regression;
- material improvement in at least one complex scenario;
- correct ContextRequest behavior;
- no hard failure.

M3b is not presumed to provide model-facing Browser orchestration. If no
actual automated normal-Browser path exists after M3b, `M2D-RQ1` is not
satisfied and M3c remains blocked. API-only execution cannot satisfy it.

## Death condition

The exception exists only until the first definitive M2D-RQ1 disposition.

If `M2D-RQ1 = PASS`, the exception expires, the repaired treatment becomes
properly requalified, and the normal M3c Basis Check may proceed.

If `M2D-RQ1 = FAIL` or `INCONCLUSIVE`, if actual Browser automation is absent,
or if material basis drift occurs, the exception expires and strict block
returns. M3c and every later lifecycle or authority action remain forbidden.

This is deferred requalification, not a benchmark waiver. It preserves the
original M2d purpose: no lifecycle authority or production routing may proceed
without evidence that Engineering Intelligence has real model-facing value.

## Frozen invariants

This delta leaves unchanged:

- Browser-first / no-required-API and semantic Browser parity;
- first-class Engineering Intelligence contracts;
- one lifecycle writer per semantic generation;
- exact RepoView/evidence grounding and deterministic content identity;
- Control Center non-authority and state-forward recovery;
- no semantic dual-write or legacy Journal import;
- isolated vNext generation;
- runtime/writer/activation `OFF / OFF / OFF` until explicit cutover;
- all formal production authority and final-cutover gates.

If satisfying this delta requires changing any frozen invariant, the correct
classification is `ARCHITECTURE REOPEN REQUIRED`, and this exception must not
be used.

## Current disposition

```text
PLAN_DELTA_DEFERRED_M2D_REQUALIFICATION
PLAN FREEZE DISCREPANCY = ACCEPTED
ARCHITECTURE REOPEN REQUIRED = NO
M3a = NOT STARTED / ALLOWED ONLY AS BOUNDED SHADOW
M3b = NOT STARTED / ALLOWED ONLY AFTER M3a
M2D-RQ1 = REQUIRED BEFORE M3c
M3c+ = BLOCKED UNTIL M2D-RQ1 PASS
runtime/writer/activation = OFF / OFF / OFF
```

**Next action:** `M3a — bounded shadow implementation under the accepted
deferred-M2d governance exception`.

This document authorizes that next action in principle only; it does not
execute it.

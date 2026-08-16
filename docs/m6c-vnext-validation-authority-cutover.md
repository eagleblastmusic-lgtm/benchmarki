# BDB vNext M6c — Validation / Policy Authority Cutover

Status: build-only internal authority closure. Production runtime/writer/activation remain OFF/OFF/OFF.

## Invariant

For every flow activated through M6c, the only validation/policy gate is:

```text
required capability
→ deterministic M6b CheckPlan
→ exact M4c Evidence
→ M6a Assessment / applicability / WaiverDecision / Approval
→ M6c canonical gate
```

A legacy profile, Browser field, model-selected command or acceptance shortcut cannot authorize or weaken an M6c flow.

M6c does not build a second Evidence system. It reuses the exact M4c/M6a Control DB connection and the M6b deterministic selector.

## Flow activation

Activation is explicit and versioned per `flow_id`.

The active immutable revision binds:

- policy revision;
- vNext generation;
- exact effect scope;
- required validation capabilities;
- exact M6b CheckPlan digest;
- exact checker registry digest;
- canonical M6c policy digest.

Changing the policy revision, capability set, executable binding, checker registry or scope creates a different policy identity. Existing approvals do not silently survive such a change because Approval binds the exact policy digest.

The first build-only flow is `git-promotion`; M7c must consume this authority before any target Git promotion can become canonical.

## Runtime-selected checks

The caller supplies only authoritative runtime capability facts such as the executable binding. `DeterministicCheckPlanSelector` selects the concrete checks. M6c has no `profile_id`, no legacy fixed-profile adapter and no fallback branch.

`validation_commands(flow_id)` is a projection of the active deterministic plan. It cannot select a different checker.

## Promotion-grade coverage

For every required capability the M6c gate requires an exact mapping:

```text
capability
→ M6a Obligation
→ exact Evidence
→ current Assessment
```

The Obligation contract must bind:

- checker id;
- checker version;
- checker code digest;
- exact environment fingerprint.

The Evidence must match those fields and the active M6b plan. Partial capability coverage, stale/missing Evidence, checker drift, environment drift, stale subject or stale Approval blocks the gate.

M6a remains the semantic authority for `SATISFIED / UNSATISFIED / UNKNOWN / STALE`, applicability, exact waivers and exact approvals. M6c does not rewrite failed assessments to `WAIVED` or `PASS`.

## Roll-forward rule

M6c is an authority cutover for an explicitly activated vNext flow. Recovery is roll-forward: stop intake, repair the canonical evidence/policy/checker facts and reactivate a new exact policy revision. Do not fall back to a legacy profile selector for that flow.

Legacy validation remains available only for legacy protocol/runtime until its own later drain/deletion gates. Historical validation records are advisory unless exact subject/environment provenance is re-established.

## Out of scope

M6c does not:

- activate production Git refs;
- delete the legacy runtime;
- create a Proof Engine or generic policy language;
- optimize checker scheduling or parallelism;
- make FULL the default;
- deploy or mutate Shopify/LIVE state.

## M7c dependency

M7c must bind its exact prepared Git effect to the active M6c policy and CheckPlan, invoke the M6c gate immediately before the Git effect, and prevent direct target promotion from bypassing that authority.

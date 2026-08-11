# M2d-vNext — Paired Engineering Quality Gate

M2d is a benchmark/experiment/checkpoint, not a production implementation
unit. This packet freezes five M2-relevant engineering scenarios against the
exact accepted vNext commit `4b724eda100345969eb236f877dd46f0bb91c0cb`.

The comparator is `BASELINE_FLAT_CONTEXT_V1`, a benchmark-only conventional
flat presentation over the same exact source universe. It is not Legacy, not a
runtime fallback, and is not registered in composition, provider, Browser, or
Native code. The other arm is `M2_VNEXT_CONTEXT_PACKAGE_V1`, using the accepted
M2a RepoView, M2b accepted fragments, M2c Understanding/ContextPackage and,
where required, ContextRequest/ContextResolution and RepoSourceEvidence.

The packet keeps source authority and evaluation authority separate. Scenario
files freeze task text, RepoView binding, source object identities, must-see
ground truth, constraints, unknowns, vector IDs, arm construction, and
deterministic scenario digests. Browser prompts contain no evaluator answer
key. Evaluator artifacts contain categorical judgments and raw counts, never a
single aggregate quality score.

## Pre-Browser payload and run contract

Materialize disposable browser assets before any execution with:

```text
python -m bdb_vnext.m2d_quality_gate materialize --repo-root . --output <disposable-dir>
```

The materializer resolves the exact frozen commit, renders deterministic
`BASELINE_FLAT_CONTEXT_V1` and `M2_VNEXT_CONTEXT_PACKAGE_V1` payloads from the
same frozen evidence-object set, and records prompt, payload, and manifest
identities in each scenario digest. It does not read evaluator ground truth or
write production state. S5 additionally freezes one neutral follow-up bundle
and operator message before any model answer exists.

The initial S5 treatment exposes `PARTIAL` coverage, both seeded visible gaps,
and only a generic affordance that more exact repository context may be
requested. It never renders a preconstructed `ContextRequest`, its reason, or
the follow-up file list. S1--S4 treatment construction uses only the exact
RepoView, committed source evidence, and generic M2c boundaries; benchmark
author unknowns, must-see annotations, and source/inference annotations remain
evaluator-side metadata.

Each paired observation is an immutable run record: exact RepoView, task and
evidence digests, prompt/payload/manifest digests, operator-selected visible
ChatGPT `model_id` and reasoning setting, browser attestation, requested source
paths, and a complete `conversation_steps` transcript. S1--S4 have one initial
step; S5 has an initial step plus the frozen follow-up step only when the model
requests the seeded context. The gate recomputes run and evaluation digests,
requires all ten X/Y runs and five exact run-linked evaluations, and derives
fairness from those records. There is no `browser_runs_present` or equivalent
boolean bypass; empty evidence is READY, partial or invalid evidence is never
PASS, and environment drift is INCONCLUSIVE.

Before policy evaluation, the evaluator validator requires exactly the frozen
scenario vector set with strict vector fields, allowed judgments, non-empty
evidence, object-shaped raw counts, and an applicability reason for `N/A`.
`material_improvement=true` requires a supporting `BETTER` judgment on a core
improvement vector. S5 `protocol_bookkeeping_required` must equal the derived
run-level protocol observation, so evaluator prose cannot contradict run
evidence.

## Frozen status

- `ACTUAL_BROWSER_RUNS = NOT YET EXECUTED`
- `M2d = READY_FOR_BROWSER_EXECUTION`
- `M3a = UNSTARTED`
- Runtime/writer/activation = `OFF / OFF / OFF`
- Legacy untouched

Run local apparatus validation with:

```text
python -m bdb_vnext.m2d_quality_gate validate
```

This validates strict JSON, deterministic identities, exact Git object reads,
M2c source grounding, frozen browser asset identities, and the synthetic gate
policy. It does not call an LLM, Browser, or OpenAI API and cannot produce M2d
PASS. Validate materialized bytes separately with the packet's
`validate_materialized_packet` helper before collecting run evidence.

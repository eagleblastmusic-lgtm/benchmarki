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
M2c source grounding, fairness metadata, and the synthetic gate policy. It does
not call an LLM, Browser, or OpenAI API and cannot produce M2d PASS.

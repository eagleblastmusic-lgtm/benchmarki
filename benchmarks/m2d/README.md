# M2d — Paired Engineering Quality Gate

This directory is a frozen, benchmark-only packet for the five-scenario M2d
gate. The baseline arm is explicitly:

`BENCHMARK_ONLY` · `NOT_RUNTIME_AUTHORITY` · `NOT_LEGACY_FALLBACK`

It is not a runtime authority, fallback provider, migration store, or cutover
path. The packet is pinned to the exact `COMMITTED` RepoView recorded
in each scenario and may be executed only in the normal ChatGPT Browser.

Current phase: `READY_FOR_BROWSER_EXECUTION`.

The Codex apparatus validates the frozen definitions, exact Git object reads,
M2b/M2c source grounding, paired-arm fairness metadata, and categorical gate
policy. It does not call an LLM and cannot establish M2d PASS. Actual Browser
answers must be captured later as immutable run/evaluation artifacts without
changing the scenario wording, ground truth, rubric, or gate policy.

## Contents

- `scenarios/` — five frozen scenario definitions and deterministic identities.
- `browser_runs/` — paste-ready neutral ARM X/Y prompts, evaluator ground truth,
  and blank evaluator sheets.
- `rubric.json` — separate categorical evaluation vectors.
- `gate_policy.json` — hard gates; no aggregate score.
- `runbook.md` — controlled Browser execution protocol.

Run the local apparatus check from the repository root:

```text
python -m bdb_vnext.m2d_quality_gate validate
```

The command reads the frozen commit through M2a and uses disposable M2b
storage only. It never edits the subject checkout, activates a provider, or
calls an API.

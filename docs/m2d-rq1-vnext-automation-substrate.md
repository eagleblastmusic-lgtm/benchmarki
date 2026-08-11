# M2D-RQ1 — normal-Browser automation substrate

This document describes the bounded bridge from the frozen M2d packet to a
future quality requalification in the normal ChatGPT Browser. It is an
execution substrate, not a benchmark run and not a production runtime.

## Boundary and authority

The packet remains the benchmark authority: its scenario digest, exact
committed RepoView (`4b724eda100345969eb236f877dd46f0bb91c0cb` / tree
`90ddd52fd997cb67a13767145fd387f7e0ad7141`), source-object identities, prompt
digests, and materialized payload/manifest digests are read and verified by
`bdb_vnext.m2d_quality_gate`. The normal ChatGPT Browser is only the execution
and evidence-capture surface. It cannot select a repository, resolve a moving
ref, create a ContextPackage, or decide PASS/FAIL.

`bdb_vnext.m2d_rq1_automation` adds the smallest missing link:

```text
exact packet + disposable materialization
  -> immutable execution plans
  -> external normal ChatGPT Browser operator boundary
  -> raw answer and visible attestation observation
  -> bdb-vnext-m2d-rq1-capture-v1 record
  -> embedded bdb-vnext-m2d-run-v2 input
  -> existing M2d evaluator/gate
```

The module never opens a Browser, calls an API, supplies a model answer, or
writes a vNext control store. The operator must use the real normal ChatGPT
conversation; an API response, test double, synthetic answer, or copied answer
is rejected by the capture contract.

## Preparation

Materialize and validate the existing packet in a disposable directory first:

```text
python -m bdb_vnext.m2d_quality_gate materialize --repo-root . --output <materialized>
python -m bdb_vnext.m2d_quality_gate validate --repo-root .
```

Prepare ten exact future-run plans without touching the repository:

```text
python -m bdb_vnext.m2d_rq1_automation prepare \
  --repo-root . \
  --packet-root benchmarks/m2d \
  --materialized-root <materialized> \
  --output <external-capture-root> \
  --attempt-id <operator-attempt-id>
```

Each plan binds the scenario/version digest, exact RepoView, task and
evidence-universe digests, prompt identity, payload and manifest identities,
and (for ARM Y) the exact M2 `ContextPackage` projection (`package_id`,
`understanding_id`, coverage, visible gaps, and source object identities).
ARM X records that a ContextPackage is not applicable. S5 also binds the
frozen follow-up bundle and operator-message identity. Plan output must be
outside the checkout; a tracked-file path is rejected.

## External Browser observation

The operator reaches the real `normal_chatgpt_browser` conversation for each
prepared plan. The observation supplied to `finalize_capture` must attest:

- expected ChatGPT conversation surface, fresh conversation, and unchanged
  visible capability class;
- prompt submission and completed response, with no unexpected navigation;
- `api_used = false`, `answer_source = normal_chatgpt_browser`, and
  `synthetic = false`;
- the visible model and reasoning setting, or the explicit value
  `UNKNOWN_NOT_OBSERVABLE` with source `not_observable` (never an inferred
  backend model ID);
- UTC Browser-observed run, submission, completion, and finalization times;
- every raw answer step unchanged as UTF-8 Markdown, including its digest and
  processing duration;
- S5 context-request flag and the exact paths actually requested. A one-step
  observation preserves an out-of-universe request as evaluator-visible
  evidence and does not admit a follow-up; the frozen-universe subset check
  applies only when a FOLLOWUP step is present.

Missing completion, partial answer, duplicate capture, unexpected model or
setting, API fallback, synthetic substitution, moving-ref substitution,
invalid timing, or identity mismatch fails closed. A Browser restart or other
interruption must use `abort_capture`, which writes an explicit `ABORTED`
record with no fabricated answer. A finalized record cannot be overwritten.

## Formal record and evaluation

`bdb-vnext-m2d-rq1-capture-v1` is an immutable wrapper around the existing
`bdb-vnext-m2d-run-v2` record. It preserves the raw assistant Markdown before
any evaluation, records Browser capture identity and observable attestations,
and links the exact run digest to the existing evaluator schema with status
`PENDING`. `evaluator_input()` returns only that existing run record; it never
classifies quality. `validate_capture_record()` reruns the frozen
`validate_arm_run()` checks and does not grant a gate outcome.

No actual M2D-RQ1 run is performed by this substrate. No M2d Attempt 3 is
created. Runtime, lifecycle writer, activation, production admission, and
legacy remain OFF/untouched. Actual quality requalification remains a
separate, explicitly authorized execution step after this preparation.

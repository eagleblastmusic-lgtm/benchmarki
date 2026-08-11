# M2d Browser execution runbook

This runbook is frozen with the scenario packet. Do not edit scenario wording,
evidence universes, ground truth, rubric, or gate policy after the first run.
If a genuine benchmark defect is found, mark the affected pair
`INCONCLUSIVE` and version/rerun the controlled factor.

For each scenario, run ARM X and ARM Y as a pair:

1. Use the normal ChatGPT Browser surface, the same visible ChatGPT product
   mode, the same operator-selected visible model, and the same user-visible
   reasoning/effort setting. Do not hardcode a model name: record the exact
   visible `model_id` and reasoning setting in every run record, and keep one
   environment digest across all ten runs.
2. Start a fresh conversation for each arm. Do not carry memory or messages
   from the paired arm.
3. Use the exact task wording in the two frozen prompt files. Do not add manual
   hints. Keep the same frozen RepoView and evidence universe.
4. Capture the full visible assistant answer unchanged, exact UTC start/end
   timestamps, selected model/settings, exact prompt/payload/manifest digests,
   requested source paths, and whether extra context was requested in the
   immutable run schema.
5. For S5, preserve the initial natural-language context request and supply
   only the exact frozen follow-up evidence and operator message. Record the
   complete two-step transcript and both answer digests. Do not expose fragment
   IDs, envelope sequence, retry bookkeeping, or protocol generations to the
   model.
6. Complete one categorical evaluator sheet per pair. Record raw counts and
   exact answer evidence for every applicable vector; use `N/A` with a reason
   when a vector does not apply.

If model, UI, settings, or visible capability changes between arms, mark the
pair `INCONCLUSIVE_ENVIRONMENT_DRIFT`; do not rerun only the worse-looking arm.
One paired attempt is the default. If variance or judge disagreement makes a
gate ambiguous, rerun the entire affected pair under the same setup, at most
two paired attempts before `INCONCLUSIVE` unless governance evidence justifies
more.

Actual Browser outputs are required for M2d PASS. Local synthetic vectors only
test the gate code and are never quality evidence.

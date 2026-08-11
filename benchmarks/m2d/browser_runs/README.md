# M2d Browser run artifacts

Each scenario directory contains two paste-ready prompts, one evaluator
ground-truth record, and one blank evaluator sheet. Run ARM X and ARM Y in
fresh normal ChatGPT Browser conversations with the same exact operator-selected
visible ChatGPT model and reasoning/settings values. Do not hardcode a model
name: record the exact visible `model_id` and reasoning setting in every run
record, and keep one environment digest across all ten runs. The prompts
intentionally expose the same frozen RepoView and evidence-universe identities;
only the presentation of that evidence differs.

Do not edit scenario wording, source identities, rubric, or policy during a
run. Save the complete model answer, timestamps, exact prompt/payload/manifest
digests, requested source paths, and the full conversation-step transcript in
the run schema, then complete the categorical evaluator sheet.
`evaluator_ground_truth.json` is for the evaluator only and must not be pasted
into a model conversation. S5 uses the frozen two-step contract: preserve the
initial ContextRequest, send only the frozen operator follow-up when requested,
and record the final answer and both step digests.

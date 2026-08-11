# M2D-RQ2 repaired-treatment frozen basis v2

This versioned packet freezes the generic M2c evidence-guidance repair for a fresh normal-Browser requalification. It is not a Browser run and it does not rewrite the historical RQ1 packet or any prior Browser records.

- `basis_manifest.json` binds treatment commit `5e3f12ceaf7f35e8421bc6ced6812738b04ab5fa`, the exact frozen benchmark RepoView, all scenario/arm/prompt/payload identities, the frozen evaluator/rubric/gate policy, and the external ten-run preparation manifest.
- `scenarios/`, `browser_runs/`, `rubric.json`, and `gate_policy.json` reuse the existing M2d contracts; only the deterministic asset/scenario identity binding for this repaired treatment is new.
- `assets/` contains the fresh deterministic materialization. S1–S4 remain equivalent to RQ1; S5 Y carries the generic gap-bound ContextRequest projection.
- The historical packet at [`../../m2d-rq1/repaired-treatment-v1`](../../m2d-rq1/repaired-treatment-v1) remains immutable `M2D-RQ1 = HISTORICAL FAIL` evidence.

Browser execution is explicitly `PREPARED_NOT_EXECUTED`; all ten fresh paired runs are required before any requalification decision. Runtime, writer, activation, M3c, and production routing remain off/blocking as governed.

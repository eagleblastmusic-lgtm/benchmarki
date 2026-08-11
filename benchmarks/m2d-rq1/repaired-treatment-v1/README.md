# M2D-RQ1 repaired-treatment frozen basis v1

This directory is the versioned benchmark packet for requalifying the repaired M2c treatment. It is not a Browser run and it does not replace the historical Attempt 2 corpus.

- `basis_manifest.json` binds the repaired M2c implementation, exact frozen RepoView, scenario/prompt identities, ContextPackage projections, and every materialized payload/manifest digest.
- `scenarios/` and `browser_runs/` reuse the existing M2d schemas, rubric, gate policy, prompts, and evaluator contracts; only the scenario/browser-asset identity binding is versioned for the repaired treatment.
- `assets/` contains the deterministic repaired-treatment payloads and existing `bdb-vnext-m2d-payload-manifest-v1` manifests.
- `rubric.json` and `gate_policy.json` are copied without semantic changes from the historical packet.

The historical packet at [`../../m2d`](../../m2d) remains immutable evidence: Attempt 2 is still `FAIL`. The ARM Y manifest identity changes in this packet are the expected consequence of M2c contract v2; ARM X and the S5 follow-up remain identical. No Browser run, Attempt 3, evaluation, production activation, or M3c is performed here.

Validate this packet with the existing `bdb_vnext.m2d_quality_gate` functions, passing this directory as `packet_root` and `assets/` (or a disposable re-materialization) as `materialized_root`.

# M6b — Deterministic CheckPlan Shadow

Status: `BUILD-ONLY / SHADOW / NO AUTHORITY CUTOVER`

M6b adds one deterministic validation-plan projection to BDB vNext. It does not run validation, replace the legacy profile mechanism, mutate Git, or authorize promotion. Production runtime / writer / activation remain `OFF / OFF / OFF`.

## Boundary

`bdb_vnext.m6b_check_plan.DeterministicCheckPlanSelector` is a pure function over authoritative capability facts and exact executable bindings. A model cannot submit argv, shell text, timeout values, or a legacy profile ID to alter the plan. The only executable commands come from the explicit in-code checker registry.

Unknown required capabilities and unknown executable bindings fail closed with typed `required_capability_unknown` errors.

The output is an immutable `CheckPlan` containing exact checker identity, argv, cwd, timeout, output budgets, process policy, registry digest, checker-set digest, execution-contract digest, and plan digest. The plan projects onto the existing `ValidationCommand` type; actual execution remains owned by the existing bounded `ValidationRunner`.

## Explicit checker registry

M6b currently shadows these exact capabilities:

- `python.pytest`
- `python.unittest`
- `dotnet.test`
- `shopify.theme_check`

The legacy staged pytest profile is intentionally **not** declared equivalent because its staged-selection semantics are broader than the fixed argv tuple. Unsupported legacy profiles return typed `legacy_profile_not_shadowable` rather than being approximated.

## Legacy shadow comparison

`LegacyFixedProfileAdapter` is comparison-only. It reads the exact existing legacy fixed-profile arguments from `bdb_bridge.fixed_test_profiles`; it is not accepted by, called from, or otherwise consulted by `DeterministicCheckPlanSelector`.

The comparison keeps two separate facts:

1. `checker_set_match` — whether legacy and vNext select the same normalized checker identities;
2. `execution_contract_match` — whether argv, cwd, timeout, output budgets, and process policy are also identical.

This separation is deliberate. The current legacy fixed profile runner uses `subprocess.run(..., shell=False, timeout=...)`, while the existing vNext `ValidationRunner` explicitly kills the process tree on timeout. Therefore the supported pytest shadow fixture has the same checker-set digest and exact argv, while the process-policy difference is visible instead of being hidden.

Any argv, cwd, timeout, output-budget, process-policy, ordering, or checker-count drift is reported explicitly by the shadow comparator.

## Authority

M6b is not a selector for production execution. It does not:

- consume model-selected profile IDs;
- execute shell/free-form commands;
- invoke subprocesses;
- change the current legacy profile authority;
- authorize M6a promotion gates by itself;
- create Git refs or synchronize checkouts;
- enable LIVE/deployment/Shopify mutation.

The old profile mechanism may remain the active production-side mechanism until M6c performs the canonical evidence/policy cutover. M6b can be disabled without changing production authority.

## DONE evidence

Focused M6b validation must prove:

- same deterministic inputs produce the same plan and digest;
- unknown required capability fails closed;
- a supported legacy fixture and vNext produce the same checker-set digest;
- argv/timeout differences are detected;
- the legacy adapter is not used as the selector;
- commands project onto the existing bounded `ValidationCommand` contract;
- unsupported staged legacy semantics are not falsely claimed as equivalent.

Next: **M6c — canonical Evidence/policy gate cutover**, after the required P2 Git/CAS witnesses are available in the intended execution order.

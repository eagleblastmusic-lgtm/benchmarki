# BDB Governance Packet

Navigation index only. The linked documents are authoritative records in their stated roles; this file adds no plan or rule.

## Documents

- [Etap 3 — Adversarial Architecture Review / Freeze v1](./BDB_ETAP3_ADVERSARIAL_ARCHITECTURE_REVIEW_FREEZE_V1_2026-08-09.md) — frozen canonical architecture authority.
- [Etap 5 — Implementation Readiness / Migration Plan Freeze v1](./BDB_ETAP5_IMPLEMENTATION_READINESS_MIGRATION_PLAN_FREEZE_V1_2026-08-09.md) — frozen historical readiness record.
- [Etap 6 — Canonical Execution Map / AI Implementation Playbook v1](./BDB_ETAP6_CANONICAL_EXECUTION_MAP_AI_IMPLEMENTATION_PLAYBOOK_V1_2026-08-09.md) — frozen execution guidance; only still-applicable portions remain in force.
- [Plan delta — Deferred M2d Requalification](./BDB_PLAN_DELTA_DEFERRED_M2D_REQUALIFICATION_V1_2026-08-11.md) — scoped accepted dependency delta; M3a/M3b shadow exception only, with M2D-RQ1 hard gate before M3c.
- [Plan v1.1.2 — BDB Next 1.0 Independent Side-by-Side Product Override](./BDB_MIGRATION_EXECUTION_PLAN_V1_1_2_BDB_NEXT_1_0_SIDE_BY_SIDE_PRODUCT_OVERRIDE_2026-08-09.md) — current canonical product/topology and late-stage execution override.
- [Plan v1.1.1 — Canonical Parallel vNext Execution Override](./BDB_MIGRATION_EXECUTION_PLAN_V1_1_1_CANONICAL_PARALLEL_VNEXT_EXECUTION_OVERRIDE_2026-08-09.md) — current canonical execution override.
- [Plan v1.1 — Parallel vNext Build](../BDB_MIGRATION_EXECUTION_PLAN_V1_1_PARALLEL_VNEXT_BUILD.md) — retained existing plan, unchanged.

## Source precedence

1. Fresh observed repo/runtime/external authority.
2. Etap 3 — Architecture Freeze v1.
3. Scoped Plan delta — Deferred M2d Requalification, only for M2d→M3c dependency semantics.
4. Plan v1.1.2 — BDB Next 1.0 Independent Side-by-Side Product Override.
5. Plan v1.1.1 — Canonical Parallel vNext Execution Override.
6. Plan v1.1 — Parallel vNext Build.
7. Still-applicable portions of Etap 6.
8. Still-applicable portions of Etap 5.
9. Latest verified Execution Handoff — evidence/status, not governance authority.

## Execution snapshot

- Architecture Freeze v1 = `UNCHANGED`
- canonical strategy = `PARALLEL_ISOLATED_VNEXT_BUILD`
- product topology = `BDB LEGACY + BDB NEXT 1.0 — INDEPENDENT SIDE-BY-SIDE`
- legacy = `FROZEN_OPERATIONAL`
- R0a = `DONE`
- R0b = `STOP / RECONCILIATION_REQUIRED / LEGACY EVIDENCE`; not a blocker for target-only vNext construction.
- M1a-vNext = `DONE`; implementation commit = `de340e2564b233b8b395bdcc1dc96e8f733f44a7`
- M2a / M1b-vNext / X1-vNext = `DONE`; X1 = `FINAL ACCEPT`
- M2c = `ACCEPT_WITH_FINDINGS / REMEDIATED_UNREQUALIFIED`; M2d Attempt 2 = `FAIL` historical and immutable.
- deferred M2d requalification = `PLAN FREEZE DISCREPANCY ACCEPTED`; M3a/M3b are bounded shadow exceptions only; `M2D-RQ1` is required before M3c.
- governance packet introduction commit = `688d2be232a8dbbcb8cc982847d6df23d6a30858`; current `bdb-vnext` HEAD must always be established by fresh inspection under source precedence
- vNext runtime/writer/activation = `OFF / OFF / OFF`
- next READY EU = `M3a — bounded shadow implementation under the accepted deferred-M2d governance exception`

## Current vNext implementation status (fresh observed living status)

- N1 / N2 / N3 / N4 / N5 / N6 = `DONE` (N6 remains build-only rehearsal infrastructure).
- Independent Sol post-repair audit = `MATERIAL_REPAIR_REQUIRED_BEFORE_STRATEGIC_PLANNING`; it invalidated the previous final closure and established `AUD-018` through `AUD-023` as concrete implementation blockers.
- Post-Sol blocker repair = `LOCAL IMPLEMENTATION + ADVERSARIAL VALIDATION COMPLETE`; the exact fresh package is a post-commit artifact and remains subject to the human gate and independent re-audit.
- Production runtime / writer / activation = `OFF / OFF / OFF`.
- Isolated rehearsal infrastructure = `ACTIVE ONLY WHEN EXPLICIT PACKAGE LOADED`; loaded test extension and registered test Native Host are not production activation.
- N6 package/native identity = compound execution identity binds package bytes, native config, execution manifest and an honest separately owned external-interpreter identity; no live-checkout import is authoritative for packaged Native semantics.
- M3 Task, M4 WorkItem, N2 Candidate, N3 Evidence/Disposition, and N4 Publication remain separate canonical authorities with no Browser/UI writer.
- Legacy = `OPERATIONAL + ISOLATED + UNTOUCHED`.
- M5+ / successor roadmap = `NOT STARTED`; no successor EU is started by this repair pass.
- Previous human N6 evidence = `HISTORICAL / INVALIDATED FOR NEW PACKAGE` because it is bound to pre-repair `6370d3f...`; fresh human Browser re-gate, deterministic runtime reconciliation and final independent Sol acceptance are `PENDING`.
- Strategic synthesis readiness = `NOT_READY_FOR_CHATGPT_WORK_STRATEGIC_SYNTHESIS` until all pending gates above pass.

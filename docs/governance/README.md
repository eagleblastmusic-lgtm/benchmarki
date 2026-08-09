# BDB Governance Packet

Navigation index only. The linked documents are authoritative records in their stated roles; this file adds no plan or rule.

## Documents

- [Etap 3 — Adversarial Architecture Review / Freeze v1](./BDB_ETAP3_ADVERSARIAL_ARCHITECTURE_REVIEW_FREEZE_V1_2026-08-09.md) — frozen canonical architecture authority.
- [Etap 5 — Implementation Readiness / Migration Plan Freeze v1](./BDB_ETAP5_IMPLEMENTATION_READINESS_MIGRATION_PLAN_FREEZE_V1_2026-08-09.md) — frozen historical readiness record.
- [Etap 6 — Canonical Execution Map / AI Implementation Playbook v1](./BDB_ETAP6_CANONICAL_EXECUTION_MAP_AI_IMPLEMENTATION_PLAYBOOK_V1_2026-08-09.md) — frozen execution guidance; only still-applicable portions remain in force.
- [Plan v1.1.2 — BDB Next 1.0 Independent Side-by-Side Product Override](./BDB_MIGRATION_EXECUTION_PLAN_V1_1_2_BDB_NEXT_1_0_SIDE_BY_SIDE_PRODUCT_OVERRIDE_2026-08-09.md) — current canonical product/topology and late-stage execution override.
- [Plan v1.1.1 — Canonical Parallel vNext Execution Override](./BDB_MIGRATION_EXECUTION_PLAN_V1_1_1_CANONICAL_PARALLEL_VNEXT_EXECUTION_OVERRIDE_2026-08-09.md) — current canonical execution override.
- [Plan v1.1 — Parallel vNext Build](../BDB_MIGRATION_EXECUTION_PLAN_V1_1_PARALLEL_VNEXT_BUILD.md) — retained existing plan, unchanged.

## Source precedence

1. Fresh observed repo/runtime/external authority.
2. Etap 3 — Architecture Freeze v1.
3. Plan v1.1.2 — BDB Next 1.0 Independent Side-by-Side Product Override.
4. Plan v1.1.1 — Canonical Parallel vNext Execution Override.
5. Plan v1.1 — Parallel vNext Build.
6. Still-applicable portions of Etap 6.
7. Still-applicable portions of Etap 5.
8. Latest verified Execution Handoff — evidence/status, not governance authority.

## Execution snapshot

- Architecture Freeze v1 = `UNCHANGED`
- canonical strategy = `PARALLEL_ISOLATED_VNEXT_BUILD`
- product topology = `BDB LEGACY + BDB NEXT 1.0 — INDEPENDENT SIDE-BY-SIDE`
- legacy = `FROZEN_OPERATIONAL`
- R0a = `DONE`
- R0b = `STOP / RECONCILIATION_REQUIRED / LEGACY EVIDENCE`; not a blocker for target-only vNext construction.
- M1a-vNext = `DONE`; implementation commit = `de340e2564b233b8b395bdcc1dc96e8f733f44a7`
- M2a / M1b-vNext / X1-vNext = `DONE`; X1 = `FINAL ACCEPT`
- governance packet introduction commit = `688d2be232a8dbbcb8cc982847d6df23d6a30858`; current `bdb-vnext` HEAD must always be established by fresh inspection under source precedence
- vNext runtime/writer/activation = `OFF / OFF / OFF`
- next READY EU = `X2 — NOT STARTED`

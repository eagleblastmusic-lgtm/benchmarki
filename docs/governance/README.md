# BDB Governance Packet

Status: **FROZEN / HISTORICAL GOVERNANCE INDEX**

Ten katalog przechowuje zamrożone dokumenty governance i migration plans. Ich role pozostają takie, jak zapisano w samych dokumentach. Ten README nie jest living execution dashboardem i nie próbuje przepisywać historycznych freeze records do bieżącego stanu runtime.

Bieżące zachowanie BDB vNext i klasyfikację dokumentacji opisują:

- [`../DOCUMENTATION_STATUS.md`](../DOCUMENTATION_STATUS.md)
- [`../VNEXT_CURRENT_ARCHITECTURE.md`](../VNEXT_CURRENT_ARCHITECTURE.md)
- [`../VNEXT_PROJECT_WORKFLOW.md`](../VNEXT_PROJECT_WORKFLOW.md)
- [`../VNEXT_AUTO_BROWSER_NATIVE.md`](../VNEXT_AUTO_BROWSER_NATIVE.md)
- [`../VNEXT_PRODUCTION_RUNTIME.md`](../VNEXT_PRODUCTION_RUNTIME.md)

## Dokumenty governance

- [Etap 3 — Adversarial Architecture Review / Freeze v1](./BDB_ETAP3_ADVERSARIAL_ARCHITECTURE_REVIEW_FREEZE_V1_2026-08-09.md) — frozen architecture authority w roli zdefiniowanej w dokumencie.
- [Etap 5 — Implementation Readiness / Migration Plan Freeze v1](./BDB_ETAP5_IMPLEMENTATION_READINESS_MIGRATION_PLAN_FREEZE_V1_2026-08-09.md) — frozen readiness/migration record.
- [Etap 6 — Canonical Execution Map / AI Implementation Playbook v1](./BDB_ETAP6_CANONICAL_EXECUTION_MAP_AI_IMPLEMENTATION_PLAYBOOK_V1_2026-08-09.md) — frozen execution guidance; późniejszy source może zawierać jawne implementacyjne successors/overrides.
- [Plan delta — Deferred M2d Requalification](./BDB_PLAN_DELTA_DEFERRED_M2D_REQUALIFICATION_V1_2026-08-11.md) — scoped accepted dependency delta.
- [Plan v1.1.2 — BDB Next 1.0 Independent Side-by-Side Product Override](./BDB_MIGRATION_EXECUTION_PLAN_V1_1_2_BDB_NEXT_1_0_SIDE_BY_SIDE_PRODUCT_OVERRIDE_2026-08-09.md) — frozen product/topology override.
- [Plan v1.1.1 — Canonical Parallel vNext Execution Override](./BDB_MIGRATION_EXECUTION_PLAN_V1_1_1_CANONICAL_PARALLEL_VNEXT_EXECUTION_OVERRIDE_2026-08-09.md) — frozen execution override.
- [Plan v1.1 — Parallel vNext Build](../BDB_MIGRATION_EXECUTION_PLAN_V1_1_PARALLEL_VNEXT_BUILD.md) — retained earlier plan.

## Jak czytać governance po wdrożeniu vNext

Governance records opisują wymagania, decyzje i kolejność migracji w momencie freeze. Nie należy wyciągać z ich historycznych sekcji `current status`, `next EU`, `production OFF/ON`, `legacy operational` itp. wniosku o stan dzisiejszego runtime bez świeżej obserwacji.

Przy sprzeczności należy rozdzielić dwie kategorie:

### Wymaganie / decyzja governance

Stosuj właściwy frozen document i jego jawne successors/overrides.

### Bieżący fakt implementacyjny/runtime

Sprawdź aktualny branch/source, testy, client/runtime evidence i dokumenty CURRENT. Source branch HEAD oraz installed production runtime mogą być różnymi wersjami.

## Zachowanie historii

Nie aktualizujemy treści zamrożonych dokumentów tylko dlatego, że implementacja poszła dalej. Historyczny plan/freeze ma pozostać audytowalny.

Jeżeli nowa implementacja świadomie zmienia obowiązującą decyzję architektoniczną, należy dodać jawny successor/override zamiast przepisywać stary freeze.

## Co zostało usunięte z tego README

Poprzednia wersja tego indeksu zawierała szybko starzejący się `Execution snapshot` z konkretnymi milestone statusami, source commitami, stanem Legacy i informacją o „next READY EU”. Taki snapshot nie należy do frozen governance indexu i został usunięty.

Aktualny stan projektu należy odczytywać ze źródeł CURRENT i fresh runtime observation, nie z tego pliku.

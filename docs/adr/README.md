# Architecture Decision Records

Status tego katalogu: **HISTORICAL ADR SET / PRE-vNext PRODUCTIZATION**, chyba że konkretny ADR został jawnie reaffirmowany przez bieżący vNext architecture contract.

ADR-y 0001–0018 dokumentują ważne decyzje wcześniejszego etapu Control Center / Operator API. Zachowujemy je dla audytu i historii decyzji, ale samo słowo `Accepted` w historycznym ADR nie oznacza, że jego każdy szczegół nadal opisuje `bdb-vnext`.

Bieżący model vNext opisują:

- [`../VNEXT_CURRENT_ARCHITECTURE.md`](../VNEXT_CURRENT_ARCHITECTURE.md)
- [`../VNEXT_PROJECT_WORKFLOW.md`](../VNEXT_PROJECT_WORKFLOW.md)
- [`../VNEXT_AUTO_BROWSER_NATIVE.md`](../VNEXT_AUTO_BROWSER_NATIVE.md)
- [`../VNEXT_PRODUCTION_RUNTIME.md`](../VNEXT_PRODUCTION_RUNTIME.md)
- [`../DOCUMENTATION_STATUS.md`](../DOCUMENTATION_STATUS.md)

## Historyczny indeks ADR

| ADR | Status historyczny | Decyzja w momencie przyjęcia |
|---|---|---|
| [0001](0001-thin-control-center-over-operator-api.md) | Accepted | Control Center jako cienkie GUI nad Operator API. |
| [0002](0002-local-only-operator-api.md) | Accepted | Operator API lokalne, bez publicznego transportu sieciowego w MVP. |
| [0003](0003-versioned-events-and-explicit-mutations.md) | Accepted | Versioned events i jawne mutacje. |
| [0004](0004-in-process-operator-api-with-json-cli.md) | Accepted | In-process Operator API z lokalnym JSON CLI. |
| [0005](0005-read-only-journal-event-projection.md) | Accepted | Read-only Journal projection. |
| [0006](0006-pyside6-qt-widgets-for-control-center-mvp.md) | Accepted | PySide6 + Qt Widgets dla Control Center MVP. |
| [0007](0007-read-only-asynchronous-gui-bootstrap.md) | Accepted | Read-only asynchronous GUI bootstrap. |
| [0008](0008-explicit-serialized-process-controls.md) | Accepted | Jawne serializowane process controls. |
| [0009](0009-read-only-current-operation-view.md) | Accepted | Read-only current-operation view. |
| [0010](0010-bounded-manual-journal-history.md) | Accepted | Bounded manual Journal history. |
| [0011](0011-explicit-sanitized-diagnostics-export.md) | Accepted | Jawny sanitizowany diagnostics export. |
| [0012](0012-two-gate-project-prepare-wizard.md) | Accepted | Two-gate project prepare wizard. |
| [0013](0013-event-driven-local-tray.md) | Accepted | Event-driven local tray. |
| [0014](0014-manual-verified-release-artifacts.md) | Accepted | Manual verified release artifacts. |
| [0015](0015-stateless-bartosz-os-adapter.md) | Accepted | Stateless Bartosz OS adapter. |
| [0016](0016-plan-only-gicleeapp-integration.md) | Accepted | Plan-only GicleeApp integration. |
| [0017](0017-bounded-session-history-without-inferred-repair-links.md) | Accepted | Bounded session history bez inferowanych repair links. |
| [0018](0018-explicit-durable-repair-correlation.md) | Accepted | Explicit durable repair correlation. |

## Jak interpretować te ADR-y dzisiaj

Przykłady elementów, które ewoluowały w vNext i dlatego nie wolno ich odczytywać wyłącznie ze starego ADR setu:

- Project Center jest dziś project-centric powierzchnią nad `ProjectCatalog`, `ProjectMemory` i `ProjectExecution`, a techniczny CC1 ma własną canonical read-only projection boundary;
- dedicated Native Host vNext to `com.bartosz.dev_bridge.vnext`;
- Browser vNext ma własny Project Execution/AUTO transport;
- Project Plan i immutable Project Memory history nie były pełnym modelem wcześniejszego Control Center;
- production activation ma vNext Bootstrap/M9b/M3c authority chain.

Historyczne ADR-y nadal są przydatne, gdy analizujemy dlaczego wcześniejszy interfejs lub policy powstały. Nie powinny jednak nadpisywać świeżego source behavior.

## Zasada dla nowych decyzji

Jeżeli bieżący vNext zmienia istotną decyzję architektoniczną:

1. nie przepisuj historycznego ADR;
2. dodaj successor ADR albo jawny current architecture override;
3. wskaż, co zostało zastąpione;
4. opisz wpływ na safety, compatibility i migration;
5. dodaj testy kontraktowe;
6. zaktualizuj odpowiedni dokument CURRENT.

Historyczny dokument nadrzędny wcześniejszego Control Center: [BDB Control Center — zamrożone granice](../BDB_CONTROL_CENTER_BOUNDARIES.md). Nie jest on automatycznie nadrzędny wobec current vNext Project/Execution architecture.

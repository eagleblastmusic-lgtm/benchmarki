# BDB vNext — status dokumentacji

Ten plik jest indeksem bieżącej dokumentacji na branchu `bdb-vnext`.

## Zasada podstawowa

Dokumentacja BDB powstała w wielu etapach: POC, Local Workspace Loop, Browser 0.4.x, Control Center 0.2/0.3, migracja side-by-side oraz docelowy BDB vNext. Nazwa pliku ani data nie oznaczają, że dokument opisuje aktualny runtime.

Dla bieżącego zachowania produktu używaj w pierwszej kolejności:

1. aktualnego kodu i testów na `bdb-vnext`;
2. dokumentów oznaczonych niżej jako **CURRENT**;
3. zamrożonych dokumentów governance wyłącznie w ich zadeklarowanej roli architektonicznej/planistycznej;
4. dokumentów historycznych wyłącznie jako evidence historii projektu.

Dokument historyczny nie może samodzielnie służyć do wnioskowania o bieżącym Browserze, Native Host, Project Memory, Project Execution, AUTO ani instalacji produkcyjnej.

## CURRENT — dokumentacja bieżącego BDB vNext

- [`../README.md`](../README.md) — wejście do aktualnego produktu i skrócona mapa systemu.
- [`VNEXT_CURRENT_ARCHITECTURE.md`](VNEXT_CURRENT_ARCHITECTURE.md) — bieżące authority, granice komponentów i model stanu.
- [`VNEXT_PROJECT_WORKFLOW.md`](VNEXT_PROJECT_WORKFLOW.md) — tworzenie projektu, planowanie, Work, Project Plan, execution i result flow.
- [`VNEXT_AUTO_BROWSER_NATIVE.md`](VNEXT_AUTO_BROWSER_NATIVE.md) — Browser vNext, Native Host, milestone AUTO, recovery i handoff `PENDING/SENT`.
- [`VNEXT_PRODUCTION_RUNTIME.md`](VNEXT_PRODUCTION_RUNTIME.md) — produkcyjny runtime Windows, client plan, Browser/Native identity i aktywacja.

Dokumenty CURRENT opisują implementację obserwowaną na implementation baseline `eae9fee9d171d61ded3c9cf539058559679aa9c8`. Późniejsze commity dokumentacyjne mogą znajdować się ponad tym commitem bez zmiany source semantics. Jeżeli kod wykonawczy pójdzie dalej, należy porównać zmianę źródła z tym baseline i ponownie zsynchronizować dokumentację.

## FROZEN GOVERNANCE — zachować

Katalog [`governance/`](governance/) zawiera zamrożone Architecture Freeze, execution/migration plans oraz accepted deltas. Są to historycznie ważne authority w rolach określonych wewnątrz tych dokumentów. Nie są living status dashboardem runtime.

Nie należy ich usuwać ani przepisywać w celu „dopasowania” do późniejszej implementacji. Bieżące odstępstwa i stan implementacji opisują dokumenty CURRENT oraz kod/testy.

## HISTORICAL EVIDENCE — zachować, ale nie traktować jako current

Do tej kategorii należą m.in.:

- `m*-vnext-*`, `n*-*`, `x*-*`, `cc*-*` — source-candidates, closure records, eksperymenty i etapy migracji;
- `VNEXT_1_0_CLOSURE_ASSESSMENT_2026-08-16.md` — assessment wykonany na starszym commicie, jawnie non-authoritative;
- `post-n6-full-system-technical-audit-updated.md`;
- dokumenty GHB0/GHB1/GHB2, Local Workspace Loop, Browser pilots, operator pilots i telemetry;
- dokumenty Control Center 0.2/0.3;
- ADR-y 0001–0018 — decyzje wcześniejszej generacji Control Center/Operator, chyba że bieżący dokument CURRENT jawnie je reafirmuje.

Te pliki pozostają w repo, ponieważ stanowią dowód ewolucji systemu i mogą być potrzebne do audytu regresji.

## LEGACY / RETIRED — usunięte z aktywnych nazw

Usunięto z bieżącego drzewa najbardziej mylące dokumenty o generycznych nazwach, które opisywały wcześniejszy Browser/Native 0.4.x lub POC, a nie `browser_extension_vnext` i `com.bartosz.dev_bridge.vnext`:

- `docs/BROWSER_EXTENSION_AUTO.md`;
- `docs/BROWSER_EXTENSION_ASSISTED.md`;
- `docs/NATIVE_MESSAGING_HOST.md`;
- `POC_0_WINDOWS_START.md`;
- `POC_0B_WINDOWS_START.md`.

Ich pełna treść pozostaje w historii Git. Zobacz [`legacy/README.md`](legacy/README.md).

## Jak utrzymywać dokumentację

Przy zmianie kontraktu BDB vNext, która dotyczy Project Memory, Project Execution, Project Plan, Browser, Native, AUTO, produkcyjnego runtime albo Control Center:

1. zmień kod i testy;
2. zaktualizuj odpowiedni dokument CURRENT w tym samym cyklu;
3. jeżeli zmiana zastępuje wcześniejszą decyzję architektoniczną, nie przepisuj starego ADR/governance — dodaj nową decyzję albo jawny current override;
4. nie wpisuj do CURRENT ścieżek z `.codex\visualizations` jako live runtime;
5. nie utożsamiaj branch HEAD z wersją aktualnie zainstalowaną — produkcyjną source identity należy odczytać z client/runtime evidence;
6. nie opisuj browserowego cache jako canonical authority; jest wyłącznie projekcją/transportem.

## Zweryfikowane repair semantics — baseline eae9fee9

Bounded repair `eae9fee9d171d61ded3c9cf539058559679aa9c8` usunął dwa wcześniej odnotowane defekty source:

- Browser receipt UI wymaga teraz `accepted === true` oraz `result_status === "PASS"`; FAIL/replayed FAIL nie są prezentowane jako accepted PASS.
- Milestone AUTO `blocked`/`review` zachowuje canonical cursor na tasku blokującym/review i nie projektuje `RUNNABLE`; tylko run `running` może przesuwać cursor przez `next_task_id`.

AUTO continuation jest fail-closed: FAIL, REVIEW i UNKNOWN nie uruchamiają następnego launchu.

Focused validation repairu: `50 passed`, plus Browser FAIL/replay-FAIL PASS, blocked/review cursor PASS, existing PASS flow PASS, STOP semantics PASS, `node --check` PASS, `py_compile` PASS i `git diff --check` PASS.

W ramach tego repairu production package nie był budowany, production/runtime nie był modyfikowany, a real ChatGPT Browser smoke nie był uruchamiany. Nie wolno więc z samego source baseline wnioskować, że identyczny build jest już wdrożony produkcyjnie.
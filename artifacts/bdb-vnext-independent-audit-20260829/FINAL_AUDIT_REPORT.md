# BDB vNext — niezależny audyt techniczny

## 1. Wynik wykonawczy

**Tryb:** `FIRST_PASS`  
**Id audytu:** `bdb-vnext-independent-audit-20260829-a3e111f`  
**Repozytorium:** `https://github.com/eagleblastmusic-lgtm/bartosz-dev-bridge`  
**Gałąź:** `bdb-vnext`  
**Źródło poddane audytowi:** `a3e111f19ebf8df803a92bee5734a9f03524501a` / tree `ab70143e69973f71f2965ffc09a5144a3d074757`

### Werdykt

| Oś | Werdykt | Uzasadnienie |
| --- | --- | --- |
| Bieżące źródło | **NOT QUALIFIED / NOT RELEASE READY** | Gate kwalifikacyjny może zwrócić `PASS` bez wiarygodnego, źródłowo związanego pomiaru; potwierdzono też błędy integralności stanu i odtwarzania. |
| Deklarowane źródło kwalifikowane | **QUALIFICATION INVALIDATED** | Kod runtime bieżącego `HEAD` jest identyczny z deklarowanym qualified source `a6aa681…`, ale artefakty i logika kwalifikacji nie dowodzą `PASS`. |
| Wdrożone/aktywne | **IDENTITY VERIFIED, FUNCTIONAL VERDICT UNKNOWN** | Zewnętrzny Bootstrap wskazuje starsze `abb5569…` / `9ffc7c…`; `CANDIDATE=null`. Bieżące źródło nie jest aktywne. Brak działającego procesu uniemożliwił ocenę live. |
| Zgodność z dwoma dokumentami kanonicznymi | **BLOCKED** | Dokładnie nazwane pliki kanoniczne nie występują w dostarczonych workspace'ach, załączniku ani lokalnych refach Git. |
| Zakres wyłączony na polecenie użytkownika | **NOT ASSESSED** | Nie wykonywano prób ani ocen wymagających dodatkowego dostępu. Nie przypisano temu zakresowi `PASS` ani `FAIL`. |

**Decyzja operacyjna:** nie promować bieżącego źródła i nie używać istniejącego `NX069_STATUS=PASS` jako podstawy wydania. Najpierw wykonać minimalny zestaw rekwalifikacyjny z rozdziału 13.

Audyt został ukończony dla jawnie włączonego zakresu. Formalna ocena kanonicznej zgodności pozostaje zablokowana, a zakres wyłączony pozostaje nieoceniony.

## 2. Granice i metoda

Włączony zakres obejmował zwykłą analizę inżynierską:

- identyfikację źródła, kwalifikowanego źródła, instalacji i aktywnego stanu;
- entrypointy i statyczną osiągalność;
- właścicieli trwałego stanu, punkty walidacji, mutacji, publikacji i odtwarzania;
- awarie częściowego zapisu, restart, retry, liveness, współbieżność, ABA i check-to-mutation;
- realizm testów, jakość oracle, pochodzenie dowodów i logikę gate;
- lokalne, odizolowane kontrprzykłady niezawodności;
- propagację klas błędów do semantycznych odpowiedników;
- analizę 10× gorzej, 10× lepiej i czułość werdyktu.

Na polecenie użytkownika pominięto wszelkie próby i oceny podlegające dodatkowemu dostępowi. Nie wykonywano testów sieciowych, prób obchodzenia uprawnień ani scenariuszy nadużyć. Jeden wcześniejszy, niepotrzebny artefakt z tego obszaru został usunięty i nie jest częścią dowodów ani wniosków.

Audyt był read-only wobec kodu, testów, wdrożenia, rejestru i zewnętrznego Bootstrapu. Zapisano wyłącznie artefakty audytu pod `artifacts/bdb-vnext-independent-audit-20260829/`.

## 3. Preflight i tożsamość źródła

Początkowa obserwacja `git ls-remote origin refs/heads/bdb-vnext` zwróciła `a3e111f19ebf8df803a92bee5734a9f03524501a`. Lokalny `HEAD` i `origin/bdb-vnext` wskazywały ten sam commit; ahead/behind wynosiło `0/0`. Tree wynosił `ab70143e69973f71f2965ffc09a5144a3d074757`, parent `47d0f3903d991bdae7b736b74656c7a1a27097b9`, a czas commitu `2026-08-27T22:58:43+02:00`. Końcowy odczyt zdalnego refa po walidacji artefaktów ponownie zwrócił dokładnie `a3e111f19ebf8df803a92bee5734a9f03524501a`; badany zdalny subject nie zmienił się podczas audytu.

Worktree był czysty w preflight. Późniejsze wpisy `?? artifacts/bdb-vnext-independent-audit-20260829/...` są wyłącznie artefaktami tego audytu i nie mogą być interpretowane jako stan źródła sprzed rozpoczęcia pracy.

Dokładnych nazw:

- `BDB_vNext_Project_Plan_v1.json`
- `BDB_vNext_Audit_i_Plan_Nastepnej_Iteracji.md`

nie znaleziono w obu dostarczonych workspace'ach, w załączniku ani w historii lokalnych refów Git. Nie zastępowano ich podobnie nazwanymi dokumentami repozytorium. Z tego powodu nie można wiarygodnie odtworzyć kanonicznego schematu, wersji, mianowników, kryteriów ani kolejności przejść.

## 4. Model systemu i osiągalność

Statyczny inwentarz objął wszystkie `1 208` śledzonych plików. Projekt zawiera `19` modułów console-entrypoint. `Start-BDB.ps1:17` uruchamia `python -m bdb_gui.app`; statyczne domknięcie importów wszystkich console-entrypointów obejmuje `133` moduły wewnętrzne: `68` z `bdb_vnext`, `51` z `bdb_bridge`, `8` z `bdb_operator`, `5` z `bdb_gui` i `1` z `bdb_shared`.

Kluczowa ścieżka zwykłego sterowania projektem jest osiągalna:

```text
Start-BDB.ps1 / bdb_gui.app
  -> Project Center / ProjectWorkflow
  -> ProjectCatalog + ProjectMemoryStore + ProjectExecutionCoordinator
  -> ProjectLaunchQueueAdapter
  -> browser_extension_vnext/content_adapter.js
  -> browser_extension_vnext/transport_worker.js
  -> m9b_native_host.py: project_execution_submit
  -> ProjectWorkflow.submit_project_execution_result
  -> ProjectExecutionCoordinator.record_result
  -> Project Memory / task status / AUTO cursor
```

Przejście browser/native jest widoczne w `browser_extension_vnext/transport_worker.js:355`, `bdb_vnext/m9b_native_host.py:364-378`, `bdb_vnext/project_workflow.py:481-530` oraz `bdb_vnext/project_execution.py:1298-1484`.

Główni właściciele stanu:

| Stan | Właściciel / authority | Mutacja | Odtwarzanie / konsument |
| --- | --- | --- | --- |
| Katalog projektów | `ProjectCatalog` | atomowy temp + `fsync` + `os.replace` | Project Center, Workflow, Execution |
| Pamięć projektu i plan | `ProjectMemoryStore` | transakcja z revision CAS, atomowy publish | Execution, AUTO, Workflow |
| Wyniki i bindingi wykonania | `ProjectExecutionCoordinator` w Project Memory | `record_result` | status zadania, milestone AUTO, katalog |
| Outbox uruchomień | `ProjectExecutionCoordinator` w Project Memory | PENDING → PUBLISHED | `ProjectWorkflow.publish/reconcile` |
| Projekcja kolejki | `ProjectLaunchQueueAdapter` | pojedynczy pending/claim, TTL | browser/native launch |
| Sloty produkcyjne | zewnętrzny Bootstrap authority | odrębny proces cutover/recovery | active reader, client promotion |
| Trasy klientów | client-plan, Native Host manifests i rejestr | M11c | Chrome/Native Host |
| Aktywacja intake | `runtime/config/m9b-activation.json` | M9b | native intake/admission |
| Dowody kwalifikacji | `runtime/evidence/*` | runner/gate/testy | dokumentacja i release decisions |

Osiągalność jest dolnym ograniczeniem statycznym: dynamiczne importy, zewnętrzne procesy i stan UI mogą dodawać ścieżki. Nie odejmowano modułów tylko dlatego, że nie wystąpiły w domknięciu statycznym; klasyfikowano je jako source/planned/unverified runtime reachability.

## 5. Mianowniki pokrycia

| Mianownik | Wartość | Sposób pomiaru |
| --- | ---: | --- |
| Śledzone pliki | 1 208 | `git ls-files -z` |
| Pliki Python | 732 | rozszerzenie śledzonych plików |
| Python source / test | 354 / 378 | położenie pliku |
| LOC Python | 256 630 | statyczny parser |
| Funkcje / klasy Python | 9 489 / 1 240 | AST, bez importowania projektu |
| Funkcje testowe Python | 2 551 | AST |
| Błędy parsowania Python | 0 | AST |
| Schematy JSON | 109 | `schemas/` |
| Workflow YAML | 24 | `.github/workflows` |
| Moduły console-entrypoint | 19 | `pyproject.toml` |
| Statycznie osiągalne moduły wewnętrzne | 133 | domknięcie importów |
| Funkcje `run_*gate/qualification` | 72 | wyszukiwanie definicji |
| Pliki z `_hardcoded_gate_fields` | 38 | wyszukiwanie definicji |
| Pliki z `SOURCE_BOUND_MACHINE_GATE` | 65 | wyszukiwanie śledzonego źródła |
| Śledzone artefakty runtime | 0 | `runtime/` jest ignorowany przez `.gitignore:35` |
| Obecne schematy / workflow | 109 / 24 | inwentarz śledzonych plików |
| Produkcyjne wywołania `reconcile_launch_outbox` | 0 | jedyny wpis poza testami to definicja |
| Testy w wybranym standardowym zestawie | 54 | liczba `test_*` i wynik pytest |

Rozkład głównych katalogów: `tests=380`, `bdb_vnext=144`, `benchmarks=136`, `bdb_bridge=127`, `docs=127`, `schemas=109`, `scripts=41`, `bdb_gui=35`, `browser_extension=27`, `.github=24`, `bdb_operator=12`, `browser_extension_vnext=7`.

## 6. Macierz lifecycle

| Oś | HEAD / tree | Stan | Dowód |
| --- | --- | --- | --- |
| CURRENT SOURCE | `a3e111f19ebf8df803a92bee5734a9f03524501a` / `ab70143e69973f71f2965ffc09a5144a3d074757` | lokalny i zdalny ref zgodne w preflight | Git ref/object/status |
| CLAIMED QUALIFIED SOURCE | `a6aa681ccbf40ca181834ed3fe628152a06dd406` / `a496aefa0667498985f0a117c5e13bf59f2be9ef` | deklarowany `PASS`, ale kwalifikacja unieważniona | CURRENT docs + gate/evidence audit |
| DEPLOYED / ACTIVE | `abb55690fcd583cfd9b2f1cd922e71709165b999` / `9ffc7cec9a2f131965ef12063ac892e7e63a0cae` | Bootstrap `ACTIVE`, generation `bdb-vnext-g1` | external slot state + manifest + client/M9b records |
| PREVIOUS | `38cdd038c59416f85caef8758bd7f879100c866a` | Bootstrap `PREVIOUS` | external slot state + docs |
| CANDIDATE | `null` | brak kandydata w zewnętrznym authority | external slot state |

`git diff a6aa681..HEAD` zmienia tylko README, dokumenty i testy; w katalogach runtime (`bdb_vnext`, `bdb_bridge`, `bdb_gui`, `bdb_operator`, oba rozszerzenia) nie ma różnicy. Oznacza to, że ustalenia dotyczące zachowania runtime bieżącego źródła odnoszą się również do deklarowanego qualified source `a6aa681…`, mimo że dokładny `HEAD/TREE` nie jest ten sam.

Aktywne `abb5569…` jest znacznie starsze: różnica do bieżącego źródła obejmuje `77` plików runtime i około `50 097` dodanych linii. Dlatego usterek bieżącego źródła nie wolno automatycznie przypisać aktywnej instalacji. Równie błędne byłoby uznanie aktywnego `abb5569…` za dowód wdrożenia bieżącego źródła.

Zewnętrzny slot state jest wewnętrznie spójny: `ACTIVE=sha256:dc2066…`, `PREVIOUS=sha256:b7ed44…`, `CANDIDATE=null`; matching ACTIVE manifest wskazuje `source_commit=abb5569…`. Lokalny `runtime/config/m9b-activation.json` także wskazuje `abb5569…` / `9ffc7c…`, `state=ACTIVE`, `writer_enabled=true`, `intake_enabled=true`. Lokalny client-plan opisuje ten sam source/tree, ale jego `production_activation_performed=false` jest faktem stagingowym, a nie zaprzeczeniem zewnętrznej, historycznie wykonanej aktywacji.

Nie wykryto działającego procesu BDB, Pythona ani Chrome w chwili obserwacji. Pliki i trasy klientów są obecne, lecz loaded-extension/current-DOM pozostają niezweryfikowane.

## 7. Rejestr ustaleń

### F-001 — CRITICAL — gate kwalifikacyjny sam wytwarza wynik zamiast go mierzyć

**Status:** potwierdzone.  
**Zakres:** assurance, evidence lineage, release decision.  
**Osiągalność:** aktywny mechanizm kwalifikacji źródła; wynik jest cytowany przez CURRENT docs.

Fakty:

1. `bdb_vnext/full_qualification_runner.py:106-127` konstruuje wszystkie `21` obszarów z literalnym `status="PASS"`; `build_qualification_manifest` w liniach `131-143` jedynie serializuje te deklaracje.
2. `run_windows_physical_suite` w liniach `447-467` wyprowadza liczbę „native calls” wyłącznie z `sys.platform`, ustawia wszystkie rozbieżności na `0` i zawsze zwraca `PASS`; nie wykonuje opisywanych wywołań.
3. `check_uac_source_equivalence` w liniach `474-493` liczy dwa hashe, ale nie porównuje ich z żadnym kwalifikowanym baseline; mimo to ustawia equivalence na `true`, requalification na `false` i `PASS`.
4. `tests/test_nx069_full_qualification.py:91-110` wykrywa tylko bezpośredni `ast.Constant` w trzech polach zwracanych. Nie śledzi producentów, aliasów, dataclass defaults ani lineage.
5. Linie `235-239` liczą `FRESH_PASS_SUITES=21` z wcześniej wpisanych statusów, nie z wykonania 21 suite'ów.
6. Linia `313` zwraca `SOURCE_BOUND_MACHINE_GATE=PASS` również przy brudnym worktree, jeżeli `all_pass=true`.
7. Utrwalony `nx069_qualification_report.json` ma `NX069_STATUS=PASS`, `SOURCE_BOUND_MACHINE_GATE=PASS`, ale równocześnie `PYTEST_FAILED=32` (`2743/2793` passed/collected). Jego source to starsze `856767b…` / `e6f4d3…`.
8. Utrwalony manifest tego samego source ma `21/21 PASS`, ale `20/21` zadeklarowanych destination nie istnieje.
9. JUnit XML jest późniejszą generacją (`2800` tests, `0` failures, `25` skipped, timestamp `2026-08-27T17:39:57+02:00`) i nie zawiera source HEAD/TREE, test-manifest digest ani environment binding. Report, manifest i XML nie tworzą jednej zamkniętej generacji dowodowej.
10. Bezpośrednie uruchomienie gate na bieżącym źródle przy worktree zabrudzonym wyłącznie artefaktami audytu zwróciło jednocześnie `WORKTREE_CLEAN=false`, `SOURCE_BOUND_MACHINE_GATE=PASS`, `NX069_STATUS=PASS`, `FRESH_PASS_SUITES=21`.

**Wpływ:** `PASS` nie dowodzi wykonania deklarowanych testów ani związania ich z ocenianym źródłem. Każda decyzja wydawnicza oparta na tym gate jest niewiarygodna.

**Propagacja:** projekt zawiera `72` funkcje gate/qualification, `38` plików z lokalnym syntaktycznym detektorem oraz `65` plików z `SOURCE_BOUND_MACHINE_GATE`. W próbce `6/6` (`NX069`, `NX068`, `NX043`, `NX046`, `NX048`, `NX049`) znaleziono tę samą klasę: kluczowe zera/liczniki są inicjalizowane jako oczekiwane wartości, a syntaktyczny detektor nie dowodzi ich pochodzenia. Nie ekstrapolowano automatycznie `6/6` na wszystkie `72`; pozostałe są ryzykiem nieweryfikowanym wymagającym lineage audit.

**Falsyfikacja false positive:** spróbowano obalić problem przez użycie aktualnego XML z zerem failures i przez zauważenie, że current runtime = qualified runtime. Nie usuwa to sprzecznego reportu, brakującego bindingu i samodzielnie wpisanych statusów. Ustalenie pozostaje potwierdzone.

### F-002 — HIGH — wynik wykonania może zakończyć zadanie bez obserwacji repozytorium i dowodów

**Status:** potwierdzone.  
**Zakres:** funkcjonalna integralność Project Memory/AUTO.  
**Osiągalność:** oficjalna ścieżka `project_execution_submit` przez Native Host.

`ProjectExecutionCoordinator.record_result` (`bdb_vnext/project_execution.py:1298-1424`) porównuje dostarczone `head_before` z wartością bindingu, ale nie odczytuje rzeczywistego HEAD repozytorium i nie waliduje `head_after`. `evidence_refs` oraz `canonical_refs` są przechowywane jako identyfikatory, lecz nie są dereferencjonowane. `_evaluate_acceptance` (`1278-1296`) domyślnie nadaje każdemu kryterium deterministycznemu `PASS`, gdy dostarczony globalny `validation_status` oznacza sukces. Brak kryteriów także prowadzi do `PASS`.

Następnie `record_result` ustawia binding na `ACCEPTED`, zadanie na `completed` i aktualizuje kursor AUTO (`1426-1484`). Testy integracyjne jawnie wywołują tę ścieżkę z `execution_status=PASS` i `validation_status=PASS`, czasem bez `criteria` i `evidence_refs` (`tests/test_project_execution_integration.py:55`, `155`, `172`, `196`).

Lokalny odizolowany kontrprzykład zarejestrował projekt bez rzeczywistego Git HEAD, podał fikcyjne `head_after`, puste `evidence_refs` i puste criteria oraz statusy `PASS`. Wynik miał `result_status=PASS`, zadanie `t1` zostało `completed`, a `t2` stało się dostępne.

**Wpływ:** canonical task state i sekwencja AUTO mogą wyprzedzić rzeczywisty stan repozytorium/testów. Błąd nie jest tylko raportowy; mutuje trwały stan projektu.

**Ograniczenie ustalenia:** binding, command, correlation, project/task/plan i podane `head_before` są sprawdzane względem canonical bindingu. Problem jest w brakującej obserwacji świata zewnętrznego i dowodów, nie w całkowitym braku kontroli tożsamości.

### F-003 — HIGH — blokady Project Memory i Catalog odbierają żywemu właścicielowi sekcję krytyczną

**Status:** potwierdzone.  
**Zakres:** współbieżność i integralność trwałego stanu.  
**Osiągalność:** oba mechanizmy są w oficjalnym domknięciu GUI/Workflow.

`ProjectMemoryStore._execution_lock` (`bdb_vnext/project_memory.py:533-575`) oraz `ProjectCatalog._lock` (`bdb_vnext/project_catalog.py:685-721`) tworzą plik przez `O_EXCL`, ale po `120s` usuwają go wyłącznie na podstawie mtime. Nie sprawdzają, czy PID nadal żyje, nie wiążą procesu z jego incarnation/start time i wykonują token-read → pathname-unlink bez stabilnego handle/CAS.

Kontrprzykład użył PID bieżącego, nadal żywego procesu i ustawił stary mtime. W obu mechanizmach drugi właściciel wszedł do sekcji krytycznej, a pierwotna żywa blokada nie została zachowana.

**Wpływ:** dwa procesy mogą równolegle wykonać transakcje nad katalogiem lub Project Memory. Revision CAS ogranicza część skutków w pamięci, ale nie zapewnia wzajemnego wykluczenia całej operacji; katalog nie ma równoważnego, zewnętrznego incarnation check.

### F-004 — HIGH — blokada kolejki ma crash-liveness i ABA check-to-unlink

**Status:** potwierdzone.  
**Zakres:** liveness, concurrency, restart recovery.  
**Osiągalność:** `ProjectLaunchQueueAdapter` jest używany przez Project Workflow.

`ProjectLaunchQueueAdapter._lock` (`bdb_vnext/project_launch.py:497-560`) tworzy docelowy plik `O_EXCL`, wykonuje niekontrolowane `os.write` bez `fsync`, a potem zamyka deskryptor. Crash/partial write pozostawia pusty lub uszkodzony lock. `_is_lock_stale(None, ...)` (`464-495`) świadomie nie odzyskuje nieparsowalnego pliku, więc kolejka może pozostać `queue_busy` bezterminowo.

Przy reclaim i release kod odczytuje token, a następnie usuwa nazwę ścieżki. Pomiędzy porównaniem a `unlink` inny właściciel może zastąpić plik. Kontrprzykład wstawił replacement między compare i unlink; replacement nie został zachowany, a challenger wszedł w sekcję krytyczną.

Test `test_replacement_lock_race_and_compare_before_reclaim` jest sekwencyjny (`tests/test_nx007_queue_locking.py:238-260`), a ThreadPool w gate współdzieli jeden obiekt kolejki i jego `_thread_lock`. Gate zwraca też literalne `COMPARE_BEFORE_RECLAIM=True` i `PERMISSION_ERROR_CLASSIFIED=True` (`419`, `427`).

**Wpływ:** możliwa trwała niedostępność kolejki albo jednoczesna własność po wyścigu replacement.

### F-005 — HIGH — recovery outboxu może skasować poprawny replacement i nie odbudowuje PUBLISHED po TTL

**Status:** potwierdzone.  
**Zakres:** cross-persistence recovery i delivery liveness.  
**Osiągalność:** publikacja jest osiągalna; reconciler nie ma produkcyjnego call site.

`publish_outbox_launch` i `reconcile_launch_outbox` odczytują pending i oceniają orphan poza lockiem, po czym pod lockiem bez ponownego odczytu/porównania wywołują `_write_state_unlocked(None, None)` (`bdb_vnext/project_workflow.py:572-590`, `639-653`). Kontrprzykład sprawdził `orphan-a`, wstawił prawidłowe `valid-b` przed mutacją, a reconciler skasował `valid-b` i zaraportował jeden usunięty orphan.

Druga luka wynika z asymetrii stanu. `pending_outbox_records` zwraca tylko `PENDING`; skuteczna publikacja ustawia `PUBLISHED`. Projekcja kolejki ma TTL 10 minut i `peek` usuwa wygasły wpis. Gdy wygasła projekcja `PUBLISHED`, `reconcile_launch_outbox` zwrócił `reconciled_count=0`, nie odbudował kolejki i pozostawił outbox w `PUBLISHED`.

W produkcyjnym źródle występuje `0` wywołań `reconcile_launch_outbox`; poza definicją wywołania znajdują się wyłącznie w testach i harnessie audytu. Test NX006 pokrywa sekwencyjne recovery PENDING, duplicate publish i sekwencyjne usunięcie orphan, ale nie interleaving replacement ani expiry projekcji PUBLISHED. Gate wpisuje `CRASH_BOUNDARY_LOST_LAUNCH=False` i `CRASH_BOUNDARY_DUPLICATE_LOGICAL_LAUNCH=False` literalnie (`tests/test_nx006_launch_outbox.py:441-442`).

**Wpływ:** poprawny launch może zostać utracony; AUTO może pozostać w stanie, który twierdzi „opublikowano”, mimo braku konsumentowalnej projekcji i bez osiągalnej ścieżki naprawy.

### F-006 — MEDIUM — CURRENT docs są starsze od bieżącego stanu gałęzi

**Status:** potwierdzone.  
**Zakres:** przejrzystość operacyjna i lifecycle.  
**Osiągalność:** dokument jest deklarowany jako CURRENT/VOLATILE i używany do opisu release state.

`docs/NX070_CURRENT_STATE.md:71` mówi `NX-071 NOT STARTED`, podczas gdy bieżący `HEAD` i repo-local evidence zawierają późniejsze prace NX071. Po qualified source `a6aa681…` istnieją trzy commity (`556d97c`, `47d0f39`, `a3e111f`), a `docs/DOCUMENTATION_STATUS.md:79` nakazuje aktualizować CURRENT w tym samym cyklu.

Dokument stwierdza też, że fault qualification jest source-bound i fail-closed (`NX070_CURRENT_STATE.md:104`), co obala F-001.

**Wpływ:** operator może poprawnie rozdzielać source/ACTIVE, ale otrzymuje nieaktualny status prac i zawyżoną pewność kwalifikacji.

### F-007 — BLOCKER — brak dokładnych kanonicznych authority artifacts

**Status:** potwierdzony brak dostępu, nie dowód nieistnienia upstream.  
**Zakres:** canonical conformance i completion validator.

Bez dwóch dokładnie wskazanych plików nie można uczciwie ocenić zgodności task/dependency/milestone/gate, acceptance criteria, wymaganych dowodów, invariantów ani kolejności transition. Repozytoryjne CURRENT docs są użyte jako obserwacje, nie jako substytut authority.

## 8. Kontrole, które przeszły próbę falsyfikacji

Poniższe fakty zawężają ustalenia i zapobiegają fałszywym uogólnieniom:

1. **Atomowy publish danych:** Project Memory, Catalog i Queue zapisują dokument do pliku tymczasowego, wykonują `fsync` i `os.replace` (`project_memory.py:106-117`, `project_catalog.py:662-671`, `project_launch.py:99-108`). Usterki F-003/F-004 dotyczą protokołu własności locka, nie samego publish dokumentu.
2. **OS-handle locks:** `BootstrapLock` (`bootstrap.py:414-465`) i legacy `InstanceLock` (`bdb_bridge/instance_lock.py:12-102`) używają systemowej blokady uchwytu zwalnianej po śmierci procesu. Klasa problemu pathname-lock nie jest uniwersalna dla projektu.
3. **Wiązanie wyniku:** Project Execution sprawdza binding, command, correlation, project/task/plan, repo alias i podane `head_before`. F-002 nie twierdzi, że nie ma żadnej walidacji; twierdzi, że brak obserwacji rzeczywistego HEAD i dereferencji dowodów przed mutacją canonical state.
4. **Lifecycle slots:** zewnętrzne `ACTIVE/PREVIOUS/CANDIDATE` są fizycznie rozdzielone, a `CANDIDATE=null`. Nie znaleziono dowodu, że audyt albo NX070 zmieniły ACTIVE.
5. **Current vs qualified runtime:** brak różnic runtime między `a6aa681…` a bieżącym `HEAD`. To redukuje ryzyko przypadkowego przypisania usterek tylko commitom dokumentacyjnym, lecz nie naprawia source/evidence bindingu.

## 9. Testy i jakość oracle

Standardowy zestaw:

```text
python -m pytest -q -p no:cacheprovider \
  tests/test_nx006_launch_outbox.py \
  tests/test_nx007_queue_locking.py \
  tests/test_project_execution_integration.py \
  tests/test_project_execution_submission.py
```

zakończył się wynikiem **54 passed**. Pierwsza próba w ograniczonym katalogu tymczasowym zakończyła się `PermissionError`; powtórzenie w dozwolonym środowisku przeszło. Błąd pierwszej próby sklasyfikowano jako ograniczenie środowiska audytu, nie defect projektu.

Przejście 54 testów potwierdza zachowanie opisane przez istniejące oracle. Nie falsyfikuje ustaleń, ponieważ:

- testy outboxu nie wykonują replacement między check i mutation oraz nie sprawdzają expiry projekcji `PUBLISHED`;
- test kolejki o nazwie „race” jest sekwencyjny, a ThreadPool współdzieli in-process lock;
- testy execution wprost akceptują caller-supplied PASS bez obowiązkowej dereferencji evidence;
- gate'y kwalifikacyjne mogą mierzyć własne zainicjalizowane wartości zamiast rezultatów testów;
- JUnit nie jest związany z dokładnym source/test/environment envelope.

Klasyfikacja dowodów:

| Typ | Przykład | Wartość dowodowa |
| --- | --- | --- |
| Fizyczna obserwacja | Git ref/object; external slot state; rzeczywiste pliki/hashy | wysoka dla konkretnego faktu |
| Wykonany kontrprzykład | local lock/outbox/result fixtures | wysoka dla potwierdzonej ścieżki |
| Standardowy test | 54 passed | dobra dla jawnie asertywnego zachowania |
| Utrwalony report bez envelope | NX069 report/manifest/XML | niska jako dowód całej kwalifikacji |
| Deklaracja/literal | prefilled PASS, counters initialized 0 | brak wartości jako obserwacja |
| Dokument CURRENT | lifecycle claim | pomocnicza; nie authority i może być stale |

## 10. Check-to-mutation i recovery matrix

| Operacja | Check | Mutacja | Okno awarii/wyścigu | Wynik |
| --- | --- | --- | --- | --- |
| Memory/Catalog lock | mtime >120s | pathname unlink | żywy, długo działający właściciel | **FAIL** — F-003 |
| Queue stale reclaim | token/PID/time read | pathname unlink | replacement po read | **FAIL** — F-004 |
| Queue lock creation | O_EXCL create | unchecked write | crash/short write | **FAIL liveness** — F-004 |
| Outbox orphan cleanup | outbox lookup poza lockiem | clear queue pod lockiem | poprawny replacement | **FAIL** — F-005 |
| PUBLISHED projection | status PUBLISHED | TTL deletes queue projection | restart/bez call site recovery | **FAIL** — F-005 |
| Project result | binding identity | task complete/AUTO advance | repo/evidence nieobserwowane | **FAIL** — F-002 |
| Memory/Catalog data publish | write temp + fsync | replace | crash przed/po replace | **PASS dla atomowości pliku** |
| Bootstrap/Instance lock | OS lock on stable handle | critical section | owner death | **PASS dla klasy owner-death** |

## 11. Analiza 10× i czułość werdyktu

### 10× gorzej

Gdyby bezkrytycznie przyjąć każdy utrwalony `PASS`, dokument CURRENT i nazwę testu jako pełny dowód, audyt uznałby source za gotowy do wydania. Byłby to błąd o rząd wielkości: gate może przejść przy brudnym źródle, sprzeczny report może zawierać 32 failures, a trwały task state może awansować na podstawie niezrealizowanych kryteriów.

### 10× lepiej

Najbardziej życzliwa interpretacja jest taka, że:

- aktualny JUnit faktycznie ma `0` failures;
- runtime current i claimed-qualified są byte-identical;
- aktywny Bootstrap jest spójny i świadomie starszy;
- wykryte błędy mogą nie być aktywne w obecnie wdrożonym `abb5569…`;
- część literalnych liczników może podsumowywać zachowanie sprawdzone w oddzielnych testach.

Nawet przy tej interpretacji gate nie wiąże tych faktów w jedną świeżą generację dowodową, a F-002–F-005 są wykonanymi kontrprzykładami bieżącego źródła. Werdykt source pozostaje `NOT QUALIFIED`.

### Czułość

- Usunięcie F-002, F-003, F-004 i F-005 nie zmienia decyzji wydawniczej: samo F-001 unieważnia kwalifikację.
- Usunięcie F-001 nie zmienia gotowości źródła: F-002–F-005 nadal wymagają naprawy i testów.
- Usunięcie F-006 nie wpływa na poprawność runtime, ale pozostawia ryzyko operacyjne.
- Dostarczenie dokumentów kanonicznych może zmienić ocenę conformance, lecz nie obali wykonanych kontrprzykładów.
- Aktywna instalacja może być lepsza lub gorsza od bieżącego źródła; bez osobnej kwalifikacji `abb5569…` jej funkcjonalny werdykt pozostaje `UNKNOWN`.

Werdykt jest zatem stabilny wobec pojedynczego false positive i wobec najbardziej życzliwej interpretacji dowodów.

## 12. Kompresja przyczyn źródłowych

| Root cause | Ustalenia | Opis |
| --- | --- | --- |
| RC-01: deklaracja zastępuje pomiar | F-001 | Statusy, zera i liczniki są konstruowane w kodzie gate, a oracle sprawdza ich kształt lub wartość, nie lineage. |
| RC-02: identyfikator zastępuje dereferencję | F-002 | HEAD/evidence/criteria są przyjmowane jako strings/statusy zamiast rozstrzygane przez authority przed canonical mutation. |
| RC-03: pathname zastępuje stabilną własność | F-003, F-004, F-005 | Compare/read i unlink/write dotyczą nazwy, która może już wskazywać replacement; age/PID nie identyfikuje incarnation. |
| RC-04: recovery nie jest częścią lifecycle | F-005 | Reconciler nie ma produkcyjnego call site i obsługuje PENDING, ale nie brak projekcji po PUBLISHED. |
| RC-05: projekcja dokumentacyjna dryfuje | F-006, F-007 | CURRENT nie jest aktualizowany wraz ze stanem, a dokładne authority nie zostały dostarczone. |

## 13. Minimalny zestaw napraw i rekwalifikacji

Kolejność jest istotna:

1. **Przebudować evidence envelope.** Każdy decisive artifact musi zawierać exact HEAD/TREE, digest zamkniętego test manifestu, environment/tool versions, timestamps, raw artifact digests i producer version. Generacja ma być atomowa i nie może mieszać reportu z innym JUnit.
2. **Usunąć prefilled PASS.** Status obszaru ma być wyprowadzany wyłącznie z wykonanego producer result; brak, stale, mismatch albo niewykonana platforma muszą dawać `BLOCKED/UNKNOWN/FAIL`, nigdy `PASS`.
3. **Naprawić source binding.** Brudny worktree musi powodować nie-PASS; test ma wykazać, że zmiana source, test manifestu lub raw evidence zmienia verdict. Detektor powinien śledzić lineage/closed schema, a nie tylko bezpośrednie `ast.Constant`.
4. **Wykonać świeże pełne pytest na dokładnie czystym HEAD.** Wygenerować nowy JUnit i manifest w tej samej generacji, bez użycia starszych artefaktów. Nie deklarować niewykonanego zakresu jako PASS.
5. **Naprawić Project Execution.** Przed task completion authority ma odczytać rzeczywisty HEAD z zarejestrowanego repo, zweryfikować dozwoloną relację `head_before/head_after`, dereferencjonować evidence/canonical refs i ocenić każde kryterium. Puste lub nierozstrzygnięte kryteria nie mogą awansować zadania.
6. **Zastąpić pathname locks.** Preferować OS-handle lock. Jeżeli plikowy protocol musi pozostać, potrzebuje process incarnation, fsync pełnego rekordu, stabilnego compare-delete/CAS i jawnego recovery uszkodzonego create. Dodać testy cross-process dla crash, partial write, pause > lease, PID reuse, ABA i replacement.
7. **Naprawić outbox recovery.** Ponownie odczytać i porównać identity pod tym samym lockiem przed clear; objąć `PUBLISHED` bez projekcji; uruchamiać reconciler na starcie i przed kontynuacją AUTO; zapisać explicit uncertain/block state, jeśli wynik jest niejednoznaczny.
8. **Dodać interleaving tests.** Wymuszać hook/barrier dokładnie między check i mutation, nie tylko sekwencyjnie. Osobne procesy, nie jeden współdzielony `_thread_lock`.
9. **Zaktualizować lifecycle docs.** CURRENT musi opisać aktualny NX071/source/qualified/ACTIVE/candidate i usunąć claim o source-bound qualification do czasu prawdziwego PASS.
10. **Dostarczyć dokładne authority artifacts.** Dopiero wtedy wykonać canonical conformance mapping, zamknąć OB-18 i powtórzyć completion validator.
11. **Dopiero po PASS powyższego** utworzyć/stage'ować kandydata i rozważyć cutover. Audyt nie rekomenduje bezpośredniej mutacji ACTIVE.

Minimalne kryteria rekwalifikacji:

- exact current HEAD/TREE i clean status związane kryptograficznie z jedną generacją;
- wszystkie wymagane obszary mają istniejący raw evidence albo jawny non-PASS;
- pełny test manifest i JUnit są zgodne, świeże i digest-bound;
- fake HEAD, brak evidence, brak criteria, stale evidence i dirty source są odrzucane;
- cross-process lock/recovery suite przechodzi wszystkie wymienione interleavingi;
- outbox po każdym crash point konwerguje do dokładnie jednej konsumowalnej projekcji albo jawnego `BLOCKED/UNCERTAIN`;
- CURRENT docs i external lifecycle readback zgadzają się z faktycznym candidate/ACTIVE;
- formalny canonical conformance PASS po dostarczeniu dwóch authority files.

## 14. Ledger obowiązków

| Zakres | Status |
| --- | --- |
| Canonical authority preflight | wykonano; brak dostępu potwierdzony |
| Fresh remote/local source preflight | wykonano w preflight |
| Lifecycle identity matrix | wykonano |
| System model i entrypoint reachability | wykonano, statyczna reachability jako dolne ograniczenie |
| Audit universe / denominators | wykonano |
| State/mutation/recovery inventory | wykonano dla authority i ścieżek oficjalnych |
| Failure/check-to-mutation | wykonano dla materialnych ścieżek |
| Concurrency/ownership/ABA | wykonano; kontrprzykłady potwierdzone |
| Cross-persistence recovery | wykonano; kontrprzykłady potwierdzone |
| Test/oracle/evidence/source binding | wykonano |
| Reliability | wykonano |
| Wyłączony zakres wymagający dodatkowego dostępu | świadomie nie oceniono |
| Cross-layer review | wykonano dla GUI/Workflow/Execution/Memory/Queue/Native/evidence/lifecycle |
| False-positive/false-negative challenges | wykonano |
| Semantic sibling propagation | wykonano z jawnymi mianownikami i próbką |
| Canonical conformance | zablokowane przez brak dwóch plików |
| 10×/sensitivity/root cause/requalification | wykonano |
| Markdown/JSON consistency validator | wykonano po zapisaniu obu artefaktów; wynik w JSON |

## 15. Ograniczenia i pozostałe ryzyka

- Nie wykonano formalnego canonical conformance z powodu braku dwóch exact-name authority files.
- Statyczne domknięcie importów jest dolnym ograniczeniem dla dynamicznych entrypointów.
- Jeden niedostępny podkatalog `runtime/evidence/pytest-nx071-run-2` zwracał `Access denied`; nie przypisano mu żadnego wyniku.
- Brak aktywnych procesów uniemożliwił potwierdzenie aktualnie załadowanego rozszerzenia i runtime live.
- Pełna ręczna analiza lineage wszystkich `72` gate'ów nie została wykonana; potwierdzono wspólny defect class w próbce `6/6`, a resztę pozostawiono jako jawny requalification obligation.
- Ustalenia o bieżącym source nie są automatycznie ustaleniami o starszym ACTIVE `abb5569…`.
- Wyłączony zakres pozostaje `NOT ASSESSED`, bez domniemanego PASS.

## 16. Artefakty i nienaruszalność

- Machine ledger: `audit-ledger.json`
- Reprodukowalny statyczny inwentarz: `static_inventory.py`
- Lokalny harness niezawodności: `adversarial_reliability_harness.py`
- Fixture'y kontrprzykładów: `reliability-fixtures-run1/` i `reliability-fixtures-run2/`
- Checkpoint roboczy: `checkpoint.md`

Nie zmieniono śledzonego kodu, testów, konfiguracji wdrożenia, rejestru ani zewnętrznego Bootstrap authority. Wszystkie materialne ustalenia mają odpowiedniki w `audit-ledger.json`; identyfikatory, tożsamości, severity i statusy są wspólne dla obu reprezentacji.

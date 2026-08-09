# BDB — Etap 5

## Implementation Readiness Review + Migration Plan Freeze v1 + Execution Granularity Freeze

**Status wejściowy:** Architecture Freeze v1 z Etapu 3 + Migration Master Plan z Etapu 4  
**Status wyjściowy:** plan migracji gotowy do wykonania po wprowadzeniu rewizji z tego raportu  
**Repozytorium:** `eagleblastmusic-lgtm/bartosz-dev-bridge`  
**Branch:** `main`  
**Zweryfikowany GitHub HEAD:** `03c44734da8829ff42c9c4859ac7b6afe2708a2a`  
**Commit time:** `2026-08-08T22:25:12+02:00`  
**Rewizja review:** `2026-08-09T03:55:39+02:00` (`Europe/Warsaw`)  
**Architecture reopen:** **NO**

Źródła użyte do review:

- aktualny publiczny GitHub `main` i odtworzony clean checkout tego samego commitu;
- rzeczywisty kod Journalu, Browser Extension, Native Hosta, executora, walidacji, promotera, projekcji operatora i Control Center;
- `BDB_ETAP3_ADVERSARIAL_ARCHITECTURE_REVIEW_FREEZE_V1_2026-08-09.md`;
- `BDB_ETAP4_MIGRATION_MASTER_PLAN_2026-08-09.md`;
- `BDB_INSTRUKCJA_GICLEEAPP_V10_2026-08-08.txt`;
- `plan_rozwoju_i_optymalizacji_BDB_v2.0_2026-08-08(1).md`.

---

## 1. Executive Readiness Verdict

> **READY WITH PLAN REVISIONS**

Architecture Freeze v1 pozostaje poprawnym targetem. Etap 4 trafnie wybrał incremental strangler, protocol partition, jeden Work Kernel, wczesne Engineering Intelligence, Browser-first vertical slice, prepared Git CAS i Control Center vNext. Nie znaleziono dowodu wymagającego ponownego projektowania BDB.

Plan Etapu 4 nie powinien jednak zostać wykonany dokładnie w zapisanej granularności. Pięć korekt jest warunkiem implementation readiness:

1. **R0 trzeba rozdzielić.** Minimalny reconciliation inventory pozostaje pierwszym krokiem, ale pełny Browser export, kompletna historia receipts, wszystkie benchmarki i pełna topologia procesów nie mogą blokować pierwszej bezpiecznej implementacji.
2. **M1 trzeba rozdzielić.** M2 nie zależy od pełnego composition cleanup. Do rozpoczęcia read-only intelligence wystarczą runtime identity i jawny composition manifest; minimalny bootstrap compatibility floor blokuje dopiero pierwszy produkcyjny schema write/cutover.
3. **Rollback schema musi być opisany uczciwie.** Obecny `Journal.open()` odrzuca schema nowsze niż binary. Po pierwszym canonical schema write nie ma automatycznego rollbacku do obecnego binary; jest `CODE REVERSIBLE / STATE FORWARD`, a po accepted canonical state — `ROLL-FORWARD ONLY`.
4. **Protocol partition wymaga external-resource exclusion.** Rozdzielenie identities nie wystarcza, jeżeli legacy i vNext mogłyby jednocześnie mutować ten sam checkout/ref/path. Przed pierwszym vNext effect cutoverem potrzebny jest repository/capability generation fence albo pełny drain tej klasy legacy effectów.
5. **XL milestones muszą stać się formalnymi Execution Units.** Writer cutover, rehearsal, deletion i benchmark nie mogą pozostawać tylko wewnętrznymi checkpointami jednego XL zadania.

Po zastosowaniu tych zmian:

> **Migration Plan Freeze v1 = READY TO EXECUTE**

Nie ma potrzeby nowego master planu ani dodatkowej fazy architektonicznej.

---

## 2. Current Baseline Check

### 2.1. Materialna delta od Etapu 4

| Pole | Wynik |
|---|---|
| GitHub repository | `eagleblastmusic-lgtm/bartosz-dev-bridge` |
| Default branch | `main` |
| GitHub HEAD | `03c44734da8829ff42c9c4859ac7b6afe2708a2a` |
| Etap 4 baseline | `03c44734da8829ff42c9c4859ac7b6afe2708a2a` |
| Publiczna delta | **0 commitów; 0 plików** |
| Odtworzony checkout | clean, `main...origin/main`, `+0/-0` |
| Local runtime użytkownika | **nieobserwowany** |

> **LOCAL STATE NOT OBSERVED**

Instrukcja V10 opisuje potwierdzony wówczas lokalny HEAD `ba9aac2fcd9145311af56f7551f641e276e3fbfb`, którego nie ma w obserwowanym publicznym `main`. Nie wolno z tego wywnioskować ani że lokalny runtime nadal jest na `ba9aac2…`, ani że działa na `03c44734…`. R0b pozostaje bezwarunkowym gate’em przed wdrożeniem na prawdziwej instalacji.

### 2.2. Re-check obszarów wskazanych przez użytkownika

Od Etapu 4 nie pojawiły się publiczne zmiany w:

- Journalu i migracjach (`LATEST_SCHEMA_VERSION = 12`);
- Browser Extension `0.4.7` i jej ledger/guards/watches/wrappers;
- Native Hoście i receipt-before-spool flow;
- command/session/task/attempt identities;
- repository index/context projections;
- staged validation;
- promoterze, `seen` i `repository_event_seq`;
- Control Center;
- import-time composition;
- recovery.

### 2.3. Świeży test baseline

Pełny suite uruchomiono na odtworzonym commicie przez interpreter środowiska testowego:

```text
999 passed
4 failed
20 skipped
1 warning
czas: 212.98 s
```

Cztery failures mają wspólną przyczynę środowiskową: checker uruchamia skonfigurowany interpreter, który w tym kontenerze rozwiązał się do bazowego Pythona bez modułu `pytest`. Trzy failure są w `test_profile_runner.py`, czwarty jest pochodną tego samego w `test_workspace_recovery_faults.py`. Wynik:

- nie jest dowodem regresji kodu względem Etapu 4;
- nie jest też nowym pełnym green runem;
- jest materialnym dowodem, że environment fingerprint i process/checker execution z M6 muszą obejmować rzeczywisty interpreter oraz dostępność checkera, nie samą ścieżkę/version string;
- wspierany Windows run pozostaje wymaganym checkpointem.

### 2.4. Materialne dowody z aktualnego kodu

| Dowód | Znaczenie dla planu |
|---|---|
| `bdb_bridge/migrations.py:15,352–446` — schema v12, `BEGIN IMMEDIATE`, jawne odrzucenie DB nowszej niż binary | Same-SQLite pozostaje wykonalne, ale recovery floor musi poprzedzić pierwszy canonical migration; stary binary nie jest rollbackiem. |
| `bdb_bridge/native_host.py:553` — receipt reserve poprzedza spool submit | Potwierdza ghost-window i potrzebę R0/R0b reconciliation oraz M3b lost-ACK lookup. |
| `browser_extension/background_task_controller.js` — `bdbTaskLedgerV1`, checkpoints i mutation guards | Browser nadal posiada lifecycle/admission mechanics, które muszą mieć writer-off i deletion points. |
| `browser_extension/background_full_entry.js` plus wrapper modules nad `submitAction`, `considerAuto`, replay i delivery | M1c explicit composition root jest realną prerequisite target path; import/wrapper order nie może pozostać authority. |
| `bdb_bridge/_public_api.py` i patch installers przez `setattr` | Composition cleanup musi być provider-by-provider; nie powinno blokować całego M2. |
| `bdb_bridge/service.py` — recovery/outbox/ingest/execute w jednej service loop + heartbeat | Potwierdza, że do M4 nie potrzeba scheduler/reconciler/dispatcher daemonów. |
| `bdb_operator/observability.py::_canonical_operation_status` oraz `session_projection.py::_canonical_attempt_status` | Active Control Center semantics są dziś inferowane w dwóch projekcjach; shell można reuse'ować, interpretation trzeba zastąpić. |

Numery linii są dowodem dla obserwowanego HEAD, nie instrukcją patchowania. Każda karta Etapu 6 ponawia symbol-level inspection.

---

## 3. Strongest Plan Risks

Maksymalnie istotne ryzyka, w kolejności wpływu:

1. **Nieobserwowany lokalny runtime.** Publiczny HEAD nie rozstrzyga aktywnego binary, extension bundle, DB schema, spool, receipts ani unresolved effects.
2. **Fałszywie odwracalna additive migration.** Obecny binary odrzuca wyższą wersję schema; backup bez kompatybilnego runtime nie daje bezpiecznego rollbacku.
3. **R0 zbyt szerokie jako pierwszy krok.** Pełny Browser diagnostic export, kompletna historia i wszystkie benchmarki wydłużałyby drogę do pierwszej wartości bez zwiększenia bezpieczeństwa pierwszej mutacji.
4. **M1 łączy trzy różne blast radii.** Runtime visibility, composition cleanup i bootstrap recovery nie powinny być jedną mutacją ani jednym gate’em dla M2.
5. **Cross-generation external conflict.** Legacy i vNext mogą mieć różne state authorities, a mimo to kolidować na tym samym repo/ref/checkout.
6. **M3 ghost/duplicate boundary.** Receipt jest dziś rezerwowany przed spool write i ma bounded eviction; silent fallback po nieobsługiwanym vNext protokole byłby niedopuszczalny.
7. **M4 zawiera zbyt wiele cutoverów.** WorkItem, candidate, effect, validation minimum, publication, presentation, Resume i MOV nie mogą wejść jako jeden XL diff.
8. **Browser parity może zostać pomylone z breadth parity.** Pierwszy slice może obsługiwać jedną klasę bezpiecznej zmiany, ale nie może mieć słabszych semantics transportu, contextu, recovery ani presentation.
9. **M6 może odtworzyć Proof Engine.** Minimalny target musi pozostać rekordami i deterministycznym planem checkerów, bez theorem graph/set-cover/ML impact.
10. **M7 był nadmiernie blokowany pełnym M6.** Git adapter można budować po exact Candidate/effect i promotion-grade evidence subset; production ref cutover nadal wymaga deterministycznego gate’u.
11. **M8 za szeroko blokował Browser demotion.** Advanced Index/Understanding enrichment i direct LIVE mutation nie są warunkiem usunięcia Browser lifecycle authority.
12. **Control Center cutover był za późny.** Query/MOV mogą powstawać od M4, a main navigation może przejść na vNext przed fizycznym usunięciem ostatniego legacy Browser store.
13. **Daemon explosion.** Rozdzielenie każdej responsibility na proces zwiększyłoby contention, activation surface i failure localization cost bez nowej authority.
14. **Compatibility readers mogą stać się ukrytą semantyką.** Legacy history musi być namespaced i nigdy nie może syntetyzować target Task/WorkItem statusu.
15. **Test runner identity nie jest dziś stabilnym evidence subject.** Świeży suite ujawnił, że „ten sam Python” nie oznacza automatycznie „ten sam checker environment”.

---

## 4. Dependency Graph Corrections

Etap 4 zachowuje właściwy kierunek. Korekta dotyczy precyzji dependencies, nie Architecture Freeze.

### 4.1. M1 → M2

**Etap 4:** całe M1 poprzedza M2.  
**Freeze Etapu 5:** rozdzielone są implementation start i production write.

```text
R0a/R0b
  ↓
M1a Runtime identity + composition manifest
  ├──→ M2a COMMITTED RepoView implementation
  └──→ M1b Bootstrap compatibility floor
             ↓
        pierwszy produkcyjny canonical schema/content write
```

- M2a oraz część M2c mogą być implementowane i testowane na fixture/shadow po M1a.
- M1b jest wymagane przed pierwszym zapisem nowej schema do produkcyjnego Journalu.
- pełny removal Python patches/Browser wrappers nie blokuje M2; target path musi jednak od początku używać explicit composition root.

### 4.2. M3 → M4

**Etap 4:** M3 jako całość poprzedza M4.  
**Freeze Etapu 5:** M4 implementation może rozpocząć się po stabilizacji M3a identity/schema w testach; M4 writer cutover nie może poprzedzać M3c admission cutover.

```text
M3a Submission + Task substrate (shadow)
  ├──→ M3b Browser outbox/lost ACK
  └──→ M4a WorkItem substrate (shadow)
M3b → M3c Admission cutover
M3c + M4 rehearsal → M4f WorkItem cutover
```

Nie powstają dwie admission authorities. Shadow oznacza fixture/feature-disabled target code, nie production dual-write.

### 4.3. M6 → M7

**Etap 4:** pełne M6 blokuje M7.  
**Freeze Etapu 5:** rozdzielono adapter implementation od writer cutoveru.

- M7a prepared Git CAS może rozpocząć się po M4b exact Candidate, M5a effect substrate i M6a promotion-grade evidence records.
- M7c production ref cutover wymaga:
  - exact sealed Candidate;
  - exact effect intent/certainty;
  - required Obligation;
  - deterministycznego CheckPlan dla tej klasy promocji;
  - exact Evidence subject + environment;
  - Assessment `PASS/FAIL/UNKNOWN/NOT_APPLICABLE` z applicability;
  - hard policy/approval/waiver gate zgodnego z Freeze.
- Evidence reuse, background assurance i pełny validation catalog nie blokują budowy adaptera.
- Model-facing profile authority dla wspieranej vNext ścieżki musi zniknąć przed jej produkcyjną promocją.

### 4.4. M8 → M9

**Etap 4:** pełne M8 blokuje M9.  
**Freeze Etapu 5:** M9 zależy tylko od M8a — exact RepoView semantics dla wszystkich target queries używanych przez wspierane flows.

Nie blokują Browser authority extinction:

- rich repository dialect enrichment;
- pełne IndexView enrichment;
- direct LIVE mutation;
- tuning context selectora po przejściu quality gate.

Honest LIVE pozostaje frozen capability, lecz może wejść niezależnie. Direct LIVE jest opcjonalne i nie może przedłużać życia Browser ledger/guards.

### 4.5. M9 → M10

**Etap 4:** finalny Control Center cutover po M9.  
**Freeze Etapu 5:** trzy momenty:

1. `CC0` Minimum Operator View zaczyna się po M4a canonical query v1.
2. `CC1` main navigation cutover następuje po stabilizacji target Work/Effect/Evidence/Repo/Publication queries; może poprzedzić pełne M9 deletion.
3. `CC2` legacy active interpretation delete następuje po M9a drain. Archived history reader może pozostać do M12, ale nie interpretuje active state.

### 4.6. Nowa jawna dependency: cross-generation resource fence

Przed M4f:

```text
legacy effectful intake dla repository/capability zatrzymany
→ active legacy effects terminal albo exact WAITING/reconciliation
→ repository/capability generation fence aktywny
→ dopiero wtedy vNext effect writer enabled
```

To nie jest sync layer ani nowa lifecycle authority. Jest cutover guardem chroniącym external resource przed dwoma generacjami executorów.

---

## 5. Milestone → Execution Unit Map

Logical milestones M1–M12 pozostają mapą komunikacyjną. Implementacja używa poniższych formalnych Execution Units.

| EU | Parent | Nazwa / invariant | Primary type | Status wobec Etapu 4 |
|---|---|---|---|---|
| `R0a` | R0 | **Minimal Reconciliation Inventory** — każda dalsza karta zna exact observed baseline albo zatrzymuje się | `IMPLEMENTATION` (`INSPECTION`) | `SPLIT` |
| `R0b` | R0 | **Observed Local Gate** — prawdziwy runtime otrzymuje `SAFE/DRAIN/RECONCILE/UNSUPPORTED` | `VALIDATION GATE` | `SPLIT` |
| `M1a` | M1 | **Runtime Identity + Composition Manifest** — aktywny provider/bundle/schema jest explainable | `IMPLEMENTATION` | `SPLIT` |
| `M1b` | M1 | **Expand-Compatible Bootstrap Floor** — istnieje bootowalny compatible recovery runtime i backup przed schema write | `BOOTSTRAP` | `SPLIT` |
| `M1c` | M1 | **Explicit vNext Composition Root** — target path nie zależy od import order/global wrappers | `MIGRATION` | `SPLIT` |
| `X1` | E1 | **SQLite Authority Gate** — same-DB/single-writer substrate przechodzi Windows crash/concurrency/restore matrix | `EXPERIMENT` | `REQUIRES EXPERIMENT` |
| `X2` | E2 | **Typed Content Durability Gate** — committed ContentRef nigdy nie wskazuje silent-missing/wrong-type bytes | `EXPERIMENT` | `REQUIRES EXPERIMENT` |
| `M2a` | M2 | **COMMITTED RepoView Foundation** — wszystkie reads mają exact commit/tree binding | `IMPLEMENTATION` | `SPLIT` |
| `M2b` | M2 | **Typed Context Transport** — exact typed fragments przechodzą Browser transport bez silent truncation | `IMPLEMENTATION` | `SPLIT` |
| `M2c` | M2 | **Understanding/Context/Decision Slice** — coverage, unknowns, ContextRequest i Decision są first-class | `IMPLEMENTATION` | `SPLIT` |
| `M2d` | M2 | **Paired Engineering Quality Gate** — real-repo result jest non-inferior na małych i lepszy/pełniejszy na złożonych | `BENCHMARK` | `SPLIT` |
| `M3a` | M3 | **Submission + Task Substrate (shadow)** — same key/digest invariants działają bez production routing | `IMPLEMENTATION` | `SPLIT` |
| `M3b` | M3 | **Restart-safe Browser Admission** — pre-send outbox + lookup przeżywa lost ACK/MV3 restart | `IMPLEMENTATION` | `SPLIT` |
| `M3c` | M3 | **Admission Authority Cutover** — accepted vNext istnieje tylko w canonical submission/Task tx; fallback sealed | `AUTHORITY CUTOVER` (`DELETION`) | `SPLIT` |
| `M4a` | M4 | **WorkItem Kernel Substrate** — current WorkItem/runs/waits/leases/fences/facts mają jeden writer i query v1 | `IMPLEMENTATION` | `SPLIT` |
| `M4b` | M4 | **Exact Candidate + Local Effect** — prepared exact mutation kończy się sealed CANDIDATE lub uczciwą uncertainty | `IMPLEMENTATION` | `SPLIT` |
| `M4c` | M4/M6 | **Minimum Candidate Evidence** — jeden checker daje exact-subject/environment/applicability assessment bez throwaway schema | `IMPLEMENTATION` (`VALIDATION GATE`) | `SPLIT` |
| `M4d` | M4 | **Publication/Presentation/Resume** — result, consumer observation i Resume nie są command/Browser lifecycle | `IMPLEMENTATION` | `SPLIT` |
| `CC0` | M4/M10 | **Minimum Operator View** — ten sam canonical query/view-model contract zasila techniczny i przyszły vNext UI | `CONTROL CENTER` | `SPLIT` |
| `M4e` | M4 | **Full-quality Browser Rehearsal** — real repo przechodzi restart/fault/quality flow bez writer cutoveru | `VALIDATION GATE` (`BENCHMARK`) | `SPLIT` |
| `M4f` | M4 | **WorkItem Authority Cutover** — generation fence aktywny; old Command/Session/Browser writers off dla allowlist | `AUTHORITY CUTOVER` (`DELETION`) | `SPLIT` |
| `M5a` | M5 | **Effect Certainty + Reconciler** — every core effect ma prepared intent, `POSSIBLE` i typed observation | `IMPLEMENTATION` | `SPLIT` |
| `M5b` | M5 | **Core Adapter Witness Cutover** — target-local retry/watch loops znikają | `MIGRATION` (`DELETION`) | `SPLIT` |
| `M6a` | M6 | **Promotion-grade Evidence Core** — obligations/evidence/assessment/applicability/env/waiver są exact | `IMPLEMENTATION` | `SPLIT` |
| `M6b` | M6 | **Deterministic CheckPlan Shadow** — runtime dobiera checkery i process policy bez model-facing profile | `IMPLEMENTATION` (`EXPERIMENT`) | `SPLIT` |
| `M6c` | M6 | **Validation/Policy Authority Cutover** — vNext gates używają canonical evidence; legacy profile authority off | `AUTHORITY CUTOVER` (`DELETION`) | `SPLIT` |
| `M7a` | M7 | **Prepared Git CAS Adapter** — tree/commit/expected ref są durable przed ref effect | `IMPLEMENTATION` | `SPLIT` |
| `M7b` | M7 | **Separate Checkout Synchronization** — ref result i checkout result są dwoma effectami | `IMPLEMENTATION` | `SPLIT` |
| `M7c` | M7 | **Git Promotion Cutover** — Git truth rozstrzyga crash; `seen/seq/watcher` target authority usunięte | `AUTHORITY CUTOVER` (`DELETION`) | `SPLIT` |
| `M8a` | M8 | **RepoView-required Target Queries** — żaden target response nie miesza COMMITTED/CANDIDATE/LIVE authorities | `MIGRATION` | `SPLIT` |
| `M8b` | M8 | **IndexView/Understanding Cutover** — index/enrichment są rebuildable projections exact RepoView | `MIGRATION` | `SPLIT` |
| `M8c` | M8 | **Honest LIVE Observation** — moving filesystem daje interval/coverage/unknown, nie fałszywy snapshot | `IMPLEMENTATION` (`EXPERIMENT`) | `SPLIT` |
| `CC1` | M10 | **Control Center vNext Main Cutover** — main UI czyta canonical queries, legacy history jest namespaced | `CONTROL CENTER` | `REVISED` |
| `M9a` | M9 | **Legacy Ingress Closure + Drain** — żaden nowy supported request nie wchodzi legacy | `AUTHORITY CUTOVER` (`MIGRATION`) | `SPLIT` |
| `CC2` | M10 | **Legacy Active Interpretation Delete** — old heuristics nie opisują active state | `DELETION` (`CONTROL CENTER`) | `REVISED` |
| `M9b` | M9 | **Browser Lifecycle Extinction** — stores/wrappers/watches/retries znikają po parity gate | `DELETION` (`BENCHMARK`) | `SPLIT` |
| `M11a` | M11 | **Bootstrap Slots + Compatibility Substrate** — active/previous/candidate identities są external i exact | `BOOTSTRAP` | `SPLIT` |
| `M11b` | M11 | **Activation Fault Matrix** — każdy crash daje known-good boot albo deterministic blocked state | `EXPERIMENT` (`BOOTSTRAP`) | `SPLIT` |
| `M11c` | M11 | **Bootstrap Authority + Self-host Cutover** — candidate nie może aktywować siebie | `AUTHORITY CUTOVER` (`BOOTSTRAP`) | `SPLIT` |
| `M12a` | M12 | **Compatibility Zero + Archive/Contract Gate** — bridges mają usage zero i complete archive/drop disposition | `VALIDATION GATE` (`MIGRATION`) | `SPLIT` |
| `M12b` | M12 | **Freeze Release + Final Deletion** — production bundle nie zawiera aktywnego legacy lifecycle/composition | `DELETION` (`BENCHMARK`) | `SPLIT` |

`Direct LIVE mutation` nie jest ukryte w M8c. Dostaje status `DEFERRED UNTIL CAPABILITY ENABLE`; candidate workflow pozostaje pełnym primary path.

---

## 6. Execution Unit Granularity Rules

### 6.1. Reguły rozmiaru

Execution Unit jest poprawnie sized, jeżeli:

1. ustanawia jeden spójny invariant albo wyłącza jedną konkretną authority/error class;
2. ma najwyżej jeden production writer cutover;
3. może mieć wiele plików/warstw, jeśli są konieczne dla jednego vertical invariant;
4. ma własny fresh inspection, acceptance, fault cases i rollback classification;
5. może zakończyć się `STOP — assumptions no longer valid` bez pozostawienia pół-cutoveru;
6. nie wymaga wpisywania z wyprzedzeniem nazw funkcji/linii, które Etap 6 powinien odkryć na świeżym kodzie;
7. bridge powstały w jednostce ma ownera, direction, telemetry, death condition i deletion EU;
8. deletion jest osobną EU tylko wtedy, gdy wymaga osobnego usage-zero/drain gate; prosty kill starego writera jest częścią cutover DoD;
9. schema powstaje tylko dla invariantów tej EU;
10. FULL nie jest domyślną walidacją.

### 6.2. Co jest za duże

Jednostka jest za duża, jeśli łączy:

- substrate implementation i production authority cutover;
- dwa różne external authorities, np. Git ref i checkout;
- semantic validation model i pełny checker/process hardening;
- Browser result delivery oraz całkowite usunięcie legacy stores;
- Control Center query contract i premium information architecture;
- bootstrap slot storage, activation crash matrix i production self-host switch.

### 6.3. Co jest za małe

Nie są Execution Units:

- pojedyncza tabela, metoda, DTO, endpoint lub ekran;
- samo dodanie telemetry bez invariant/cutover value;
- osobny test dla każdej crash boundary;
- dokumentacja istniejącego kontraktu bez niezależnego gate’u;
- kosmetyczny refactor bez usuwania ryzyka/authority.

### 6.4. Granularność dla BDB AUTO

| Klasa EU | Typowa granularność |
|---|---|
| Read-only contract/provider, jeden bounded adapter | `ONE AUTO LOOP` |
| Cross-process durability, browser restart, schema + migration | `MULTIPLE ORDERED AUTO LOOPS` |
| Empiryczne gate’y X1/X2 i paired quality | `EXPERIMENT ONLY` |
| Real-local reconciliation, drain approval, writer cutover, activation | zawiera `MANUAL / OPERATOR PREREQUISITE`; implementacja może użyć AUTO, samo przełączenie nie jest unattended |

Jedna AUTO loop ma dążyć do:

```text
fresh inspection → implementation → targeted confirmation
```

Jeżeli w inspection ujawnia się nowy writer, inna schema, active unknown effect albo potrzeba dual-write, poprawnym wynikiem jest `STOP`, nie lokalny workaround.

---

## 7. Experiment Consolidation Table

| E# | Klasyfikacja Etapu 5 | Gdzie trafia / dlaczego |
|---|---|---|
| **E1 SQLite durability/concurrency** | **STANDALONE GATE** | `X1` przed pierwszym produkcyjnym canonical SQLite write. Wynik może zmienić substrate implementation. |
| **E2 Typed Content Store durability** | **STANDALONE GATE** | `X2` przed M2b production persistence. Layout/durability muszą być znane przed store implementation. |
| **E3 CAS GC/backup/recovery** | **DEFER UNTIL CAPABILITY ENABLE** | Wczesny TCS jest append-only/no-GC. Gate wraca przed production GC i M11 coordinated restore. |
| **E4 Honest LIVE capture** | **DEFER UNTIL CAPABILITY ENABLE** | Wykonać bezpośrednio przed M8c; nie blokuje COMMITTED/CANDIDATE ani Browser authority deletion. |
| **E5 Direct LIVE mutation** | **DEFER UNTIL CAPABILITY ENABLE** | Nie należy do primary candidate path. Failure oznacza capability disabled, nie plan failure. |
| **E6 Windows filesystem + Git fault matrix** | **INTEGRATE INTO EXECUTION UNIT** | Podzielić według authority: M1b file/backup, M4b atomic replace/candidate, M7a ref, M7b checkout. Jeden gigantyczny eksperyment zaciemniałby ownership. |
| **E7 Browser transport limits** | **INTEGRATE INTO EXECUTION UNIT** | M2b exact fragment transport + M4d result/resume; naturalny acceptance/fault test protokołu. |
| **E8 Browser restart + lost ACK** | **INTEGRATE INTO EXECUTION UNIT** | M3b. Jest właściwą definicją acceptance restart-safe admission. |
| **E9 New-chat Resume Capsule** | **INTEGRATE INTO EXECUTION UNIT** | M4d/M4e; nie wymaga osobnego mini-projektu. |
| **E10 Presentation witness** | **INTEGRATE INTO EXECUTION UNIT** | M4d/M4e; bezpieczny fallback `UNKNOWN` pozwala iterować w tym samym adapterze. |
| **E11 Environment fingerprint** | **INTEGRATE INTO EXECUTION UNIT** | M6a; świeży test baseline pokazuje, że musi obejmować faktyczną checker availability. |
| **E12 Sandbox/egress/process behavior** | **INTEGRATE INTO EXECUTION UNIT** | M6b process adapter fault matrix. Failure zawęża checker applicability, nie tworzy osobnej platformy. |
| **E13 Self-host activation/rollback** | **INTEGRATE INTO EXECUTION UNIT** | Minimalny subset w M1b; pełna crash matrix w M11b przed M11c. |
| **E14 Context depth/quality selector** | **INTEGRATE INTO EXECUTION UNIT** | M2d paired benchmark; jest product gate, nie infrastrukturalny spike. |
| **E15 External adapter witnessability** | **REMOVE** jako umbrella experiment | Wymóg pozostaje frozen, ale każdy adapter ma własną witness/fault matrix w M5b/M7/M11. Generic pass nie dowodzi niczego o konkretnym target authority. |

Po konsolidacji istnieją tylko dwa obowiązkowe standalone experiment projects przed wczesnym product slice: `X1` i `X2`. Pozostałe testują konkretny adapter/capability w miejscu, w którym wynik ma znaczenie.

---

## 8. R0 Falsification Result

### Werdykt

> **R0 pozostaje pierwszym krokiem, ale Etap 4 R0 jest za szerokie.**

Formalny podział:

- `R0a Minimal Reconciliation Inventory` — implementacja trwałego, read-only providera;
- `R0b Observed Local Gate` — uruchomienie na prawdziwej instalacji i decyzja operacyjna;
- domain-specific inventory extensions — dopiero przed odpowiednimi cutoverami M3/M7/M9/M11.

### REQUIRED BEFORE FIRST MUTATION

Przed pierwszą zmianą source code przez AI wymagane jest fresh inspection dostępnego checkoutu. Przed pierwszym wdrożeniem/migration write wymagane R0b obejmuje:

- local source HEAD, branch/upstream/detached, staged/unstaged/untracked;
- active deployed kernel/native/extension/Control Center bundle identities i digests, jeśli komponent jest obecny;
- zadeklarowane config/store paths i aktywny lock/PID;
- SQLite schema version, migration checksums, integrity/WAL state i aktywne unresolved current/effect/outbox counts;
- tylko aktywne/unresolved receipts, spool entries i promotion entries potrzebne do wykrycia ghost/possible effects;
- relację local repo/ref/checkout dla unresolved promotion;
- completeness/instability/errors oraz sanitized semantic digest;
- wynik `SAFE_TO_MIGRATE`, `DRAIN_REQUIRED`, `RECONCILIATION_REQUIRED` albo `UNSUPPORTED_RUNTIME` z domain blockers.

### USEFUL LATER

Nie blokują implementacji R0a ani read-only M2a:

- pełny Browser diagnostic export wszystkich terminalnych ledger/checkpoint records;
- pełna enumeracja spool/archive/results i historycznych receipts;
- complete wrapper order — zastępuje go M1a composition manifest;
- cały historyczny promoter state — deep scan dopiero przed M7;
- wszystkie wersje pakietów i dependency tree — wystarczą active bundle/compat identities;
- complete process topology — R0a potrzebuje tylko procesów/locków mogących pisać obserwowane stores;
- baseline wszystkich Architecture Benchmark scenarios — paired subset jest M2d, pełny checkpoint później;
- premium Control Center/System Health UI;
- Browser transport R0a; provider może być najpierw CLI/local, a sanitized Browser projection dojść przez ten sam contract.

### Non-throwaway contract

R0a zostaje później jako:

- System Health source;
- Bootstrap preflight;
- migration inventory;
- operator diagnostic export;
- Etap 6 fresh-inspection input.

Nie tworzy lifecycle identity ani osobnej DB.

---

## 9. Early Engineering Intelligence Ordering

### Werdykt

Pierwszy materialny zysk jakości GPT może i powinien pojawić się wcześniej niż w literalnej kolejności Etapu 4.

Po `R0b + M1a` można rozpocząć:

1. `M2a` exact COMMITTED RepoView na istniejących Git object reads;
2. `X2`, a po pass `M2b` Typed Context Transport;
3. `M2c` minimal Repository Understanding, must-see/coverage/unknowns, ContextRequest i Engineering Decision;
4. `M2d` paired benchmark w normalnej rozmowie ChatGPT.

M1b musi skończyć się przed produkcyjnym zapisem nowych tabel/content roots, ale nie blokuje implementacji/fixture benchmarku M2a/M2c. M1c explicit target composition może rozwijać się równolegle, o ile M2 provider jest jawnie rejestrowany i nie instaluje nowego patcha przy imporcie.

Minimalny M2 nie wymaga:

- LIVE RepoView;
- full semantic index;
- repository dialect enrichment;
- Task/WorkItem lifecycle;
- Control Center premium UI;
- GC;
- API.

Hard exit M2d:

- mechanical/local scenarios nie są gorsze;
- component/architecture/diagnostic scenarios mają mniej pominiętego must-see albo lepszą ekspozycję unknowns/trade-offs;
- ContextRequest rzeczywiście naprawia luki;
- GPT nie musi znać fragment sequence/protocol bookkeeping;
- cały flow działa przez normalny Browser Mode.

---

## 10. Authority Cutover Corrections

Poniżej wyłącznie delta względem Etapu 4.

| Domena | Etap 4 | Migration Plan Freeze v1 |
|---|---|---|
| Admission | M3 | Writer cutover formalnie `M3c`; implementation M3a/M3b może iść wcześniej. Silent vNext→legacy fallback zakazany. |
| Spool dla vNext | M3/M4 | vNext admission nie zapisuje spool od `M3c`; spool pozostaje wyłącznie transportem legacy drain do M9a. |
| Task Browser ledger | writer off M3, delete M9 | writer off M3c; target read/checkpoint dependency usuwana najpóźniej M4f; fizyczny legacy store M9b. |
| WorkItem/Command | M4 | Writer cutover formalnie M4f, po rehearsal i repository generation fence. M4a–M4e nie są cutoverem. |
| External repository resource | implicit | Dodany obowiązkowy repository/capability generation fence/drain przed M4f; zapobiega cross-generation effect collision. |
| Core retry/reconcile | M5 | M5a substrate; M5b jest osobnym target retry/watch deletion gate. |
| Validation | pełne M6 przed M7 | M6a/M6b wystarczają do budowy promotion path; M6c odcina model-facing profile authority dla enabled vNext flows. |
| Git promotion | M7 | M7a adapter, M7b checkout, M7c single writer cutover; legacy watcher może umrzeć w M7c po promotion-specific drain, bez czekania na całe M9. |
| Repository Intelligence | pełne M8 przed M9 | tylko M8a blokuje M9; M8b enrichment i M8c LIVE nie utrzymują Browser authority. |
| Browser target lifecycle | pełne usunięcie M9 | target ledger/guard/watch dependencies umierają stopniowo w M3c/M4f/M5b; M9b usuwa ostatnie legacy stores/wrappers. |
| Control Center main | M10 po M9 | CC0 od M4a; CC1 main cutover przed/obok M9a; CC2 active legacy interpretation delete po drainie. |
| Python patch providers | M12 max | provider usuwany w EU, która przejmuje jego port; M12 tylko potwierdza zero. |

Żaden writer cutover nie może być cofnięty przez ponowne włączenie starego writera. Recovery oznacza stop intake + compatible runtime + reconcile/roll-forward.

---

## 11. Compatibility Horizon Corrections

Zasada po review: **compatibility reader może żyć dłużej niż writer, ale tylko z nazwanym konsumentem, kierunkiem danych i death condition**. Archiwalny odczyt historii nie jest powodem do utrzymywania aktywnego legacy runtime.

| Legacy mechanism / authority | Najwcześniejszy death point po review | Dokładny blocker wcześniejszego usunięcia |
|---|---|---|
| Browser task ledger | writer: M3c; zależność target runtime: M4f; store/code: M9b | Przyjęte, lecz niezakończone legacy submissions oraz recovery checkpointy ich generacji. |
| Browser mutation guards | writer dla vNext: M3c; target guards: M4f; legacy delete: M9b | Aktywne legacy operacje, których deduplikacja nadal jest wyłącznie w extension storage. |
| Browser command watches | target nie powstaje od M4d/M4f; legacy delete: M9b | Late results istniejących legacy commands i ich presentation acknowledgement. |
| Replay claims | target path przejęty M4d/M5b; legacy delete: M9b | Nierozliczone legacy delivery/replay claims. |
| Native receipts | vNext writer off: M3c; aktywny reader/store end: M9a/M9b | Lost-ACK lookup i drain legacy submissions przyjętych przed cutover. Po drainie może zostać tylko offline archive. |
| Session store | vNext nie tworzy sesji od M3c; aktywny odczyt end: M9a; runtime delete: M9b; schema/archive: M12 | Legacy Task/session musi dojść swoim protokołem do terminalnego disposition. |
| Spool | vNext writer off: M3c; legacy drain: M9a; runtime/files delete: M9b | Pliki spool już zaakceptowane przez legacy admission, w tym niepewne reserve-before-spool. |
| Command writer | vNext off: M4f; cały legacy writer off: M9a; transition code delete: M9b | Aktywne effectful legacy commands objęte generation fence muszą zostać sklasyfikowane i rozliczone. |
| Session writer | vNext off: M3c; cały legacy writer off: M9a; runtime delete: M9b | Intent revisions i wynik legacy sessions rozpoczętych przed admission cutover. |
| Legacy result/outbox | vNext replacement: M4d/M4f; legacy writer off: M9a; runtime delete: M9b; archive contract: M12 | Late results i PRESENTED/UNKNOWN dla aktywnych legacy consumers. |
| Promoter watcher | M7c | Promotion-specific queue musi być drained, a exact ref outcome rozliczony. Nie blokuje go M9. |
| Promoter `seen` | aktywna authority: M7c; archive: M12 | Dedup/history dla promotions rozpoczętych przed M7c. |
| Repository sequence | active writer/guard: M7c; archive: M12 | Rozliczenie old promoter effects; po CAS target sequence nie może już sterować promotion. |
| Old validation profile authority | dla enabled vNext flows: M6c; dla legacy: M9a; runtime delete: M9b | Nieprzeniesione obowiązki/check availability dla aktywnych legacy commands. |
| Legacy projections | main UI: CC1; active interpretation: CC2; archive reader: M12 | Operator musi mieć canonical replacement dla aktywnych target flows oraz czytelny legacy drain view. |
| Python patch providers | provider-by-provider, przy EU przejmującej port; zero gate: M12a | Konkretny nadal aktywny composition port. Sam fakt historycznego importu nie jest blockerem. |
| Browser global wrappers | target bypass: M1c; delete: M9b | Tylko obsługa aktywnego legacy protocol generation. |
| Legacy installer/self-host path | M11c/M12 | Bootstrap slot activation, contract compatibility i udowodniona recovery ścieżka. |

### Zasady fizycznego usuwania

1. Najpierw wyłączony zostaje writer.
2. Następnie canonical query przestaje zależeć od legacy store.
3. Potem drain/reconciliation potwierdza brak aktywnych efektów.
4. Dopiero wtedy usuwany jest runtime code/store.
5. Retained audit archive jest pasywny, immutable i nie może wrócić jako authority.

Nie ma bridge'a bez właściciela i daty śmierci. Jeśli podczas implementacji pojawi się bridge niewymieniony w Execution Unit, EU ma wynik `STOP — scope/authority expansion required`.

---

## 12. Same-SQLite Freeze

### Decyzja

**FROZEN WITH MIGRATION CONDITIONS:** canonical tables pozostają w tym samym fizycznym SQLite co legacy Journal.

Nie znaleziono materialnego powodu dla drugiej bazy. Druga DB zwiększyłaby liczbę transakcyjnych granic, recovery permutations i stanów „która baza wygrała”, a nie usuwałaby problemu cutoveru external effects.

### Dlaczego decyzja pozostaje poprawna

- additive schema pozwala budować target read/shadow path bez dual-write;
- jedna transakcja może atomowo zapisać target state, outbox/publication intent i effect certainty;
- obecny Journal już ma WAL, `BEGIN IMMEDIATE` i migration mechanism, które można wykorzystać;
- backup, integrity scan i operator diagnostics mają jeden spójny snapshot;
- Bootstrap ma jeden durable contract boundary;
- compatibility readers mogą być jawnie jednostronne: legacy → target query/import, nigdy target → legacy writer.

### Warunki zamrożenia

1. **Jeden writer process:** tylko Work Kernel otwiera canonical write connection. Browser, Native Host, Control Center i remote worker nie zapisują SQLite bezpośrednio.
2. **Brak target joins do legacy:** canonical repositories/queries nie mogą uzależniać target transition od legacy tabel. Jednorazowy importer/compatibility reader ma jawny port i death condition.
3. **Namespacing i ownership:** każda nowa tabela ma owner repository, classification i invariant; żadnych tabel „na przyszłość”.
4. **Expand-compatible bootstrap floor przed pierwszym production migration:** uruchomiony slot recovery musi rozumieć najwyższy schema version, nawet jeżeli aktywny feature zostanie wyłączony.
5. **Backup + invariant scan:** przed schema activation powstaje spójna kopia DB/WAL oraz sprawdzany jest migration checksum, integrity i unresolved effect inventory.
6. **Content bytes poza SQLite:** DB trzyma typed `ContentRef` i metadata; immutable bytes trafiają do kontrolowanego content store.
7. **Accidental legacy access gate:** test architektury blokuje nowe importy/SQL od target providerów do legacy domain tables.

### Materialny problem wykryty w kodzie

Aktualny migration runner odrzuca schema nowsze niż znana lista migracji. To znaczy, że po pierwszym canonical schema write stary binary **nie jest prawdziwym rollbackiem**, nawet gdy zmiana jest additive.

Wniosek:

> najpierw M1b instaluje expand-compatible recovery floor, dopiero potem X1 i pierwsza produkcyjna canonical migration.

Po tej granicy właściwe recovery to:

```text
stop intake
→ uruchom known-good compatible recovery runtime
→ inventory + reconcile
→ fix / roll-forward
```

Nie wolno opisywać tego jako „cofnij binary”.

### Wynik X1

X1 pozostaje standalone gate, ponieważ przed ostatecznym SQL designem musi potwierdzić na kopii aktualnego Journalu:

- lock/transaction behavior przy legacy readers;
- backup/restore z WAL;
- migration checksum i forward compatibility;
- brak niejawnych legacy joins;
- zachowanie po crashu w każdym migration boundary;
- akceptowalny blast radius i czas recovery.

X1 nie otwiera Architecture Freeze. Negatywny wynik najpierw wymaga korekty migration mechanics. Druga DB jest rozważana dopiero po materialnym, powtarzalnym dowodzie, że wspólna DB nie może spełnić invariants.

---

## 13. Protocol Partition Stress Test

Zasada Etapu 4 pozostaje zamrożona, ale otrzymuje jeden konieczny dodatek:

```text
legacy request → legacy semantic generation aż do terminalnego disposition
vNext request  → target semantic generation aż do terminalnego disposition

oraz

wspólny external resource → generation fence przed pierwszym vNext effect writerem
```

| Przypadek adversarial | Wymagane zachowanie | Wynik |
|---|---|---|
| User zmienia intent w legacy Task/session | Zmiana pozostaje legacy intent revision albo tworzy jawnie powiązany nowy vNext Task. Nie przenosi się in-flight Session do target state machine. | PASS po doprecyzowaniu identity/link contract. |
| Legacy late result po vNext activation | Wynik aktualizuje tylko nazwany legacy generation/history/publication. Nie może zmienić current target WorkItem ani target candidate. | PASS; wymaga generation namespace w canonical query. |
| Nowy Browser + stary Native Host | Capability handshake przed durable send. Brak vNext capability oznacza `UNSUPPORTED`; payload zostaje w client outbox jako nieprzyjęty. Zakaz silent fallback do legacy. | PASS; handshake jest prerequisite M3b. |
| Extension update podczas legacy operation | Accepted submission zachowuje immutable protocol generation. Nowa extension używa compatibility reader do statusu, nie retransmituje jako vNext. | PASS. |
| vNext WorkItem używa istniejącego legacy executora | Wolno reuse'ować pure mechanics przez typed adapter. Adapter nie tworzy Command/Session rows i nie czyta z nich transition truth. Jeśli mechanika wymaga legacy lifecycle state: `STOP`. | PASS z adapter boundary test. |
| Runtime rollback po pierwszym canonical write | Intake stop; compatible recovery runtime; reconcile; roll-forward. Stary binary nie jest uruchamiany na nowszym schema. | PASS po M1b; przed M1b production write jest zabroniony. |
| Legacy Control Center czyta target DB | Tylko przez versioned canonical query gateway i generation-qualified view models. Stare heurystyki nie interpretują target tables. | PASS po CC0/CC1. |
| Self-host zmienia protocol/schema compatibility | Bootstrap aktywuje prepared slot dopiero po preflight; poprzedni slot musi być schema-compatible recovery slot. Contract usuwa się dopiero po zero-consumer gate. | PASS po M11. |
| Legacy i vNext dotykają tego samego checkout/ref/path | Przed M4f: stop legacy effectful intake dla repo/capability, drain/classify active effects, acquire generation fence, dopiero potem enable target effect writer. | **Etap 4 wymagał korekty; po niej PASS.** |
| Legacy publication ma ten sam conversation target co vNext | Consumer key zawiera task/publication/generation; witness jest per-publication. Nie ma wspólnego „last delivered”. | PASS po M4d. |
| Request accepted, ACK lost podczas cutover | Lookup po immutable submission key jest routowany do authority generacji, która dokonała acceptance. Retry nie może utworzyć Task w drugiej generacji. | PASS po M3b/M3c. |

### Reuse implementation ≠ reuse semantic authority

Mechanika może być reuse'owana tylko wtedy, gdy wszystkie poniższe zdania są prawdziwe:

1. target przekazuje typed input i otrzymuje typed result;
2. target identity oraz transition pozostają zapisane wyłącznie przez Work Kernel;
3. legacy rows/files nie są warunkiem target transition;
4. retry/idempotency key pochodzi z target Effect/WorkItem;
5. adapter ma witness contract i może zostać usunięty bez zmiany target semantics;
6. fault test dowodzi, że legacy restart/replay nie duplikuje target effect.

Jeżeli choć jedno nie jest prawdziwe, mamy semantic bridge albo dual authority, a nie reuse.

---

## 14. Control Center Freeze

### Decyzja

**BUILD CONTROL CENTER vNEXT + CUTOVER — FROZEN.**

Kod potwierdza, że shell jest wartościowy, lecz active interpretation wymaga wymiany. Obecne osobne inferencje statusu w operator observability i session projection odtwarzają lifecycle z Commands/Sessions/outbox/receipt files. Nie mogą zostać target authority.

### REUSE

- PySide6 application shell, main window, navigation i `QStackedWidget`;
- styling, layout primitives, themes i non-semantic widgets;
- `QThreadPool`/`QRunnable` worker pattern dla non-blocking reads/actions;
- service protocol injection i lokalny `bdb_operator` façade;
- diagnostics/export/sanitization mechanics;
- packaging, smoke infrastructure, tray/lifecycle shell, jeżeli nie niesie domain authority;
- jawne operator actions z confirmation i kernel command boundary.

### REPLACE

- aktywne DTO/view-model oparte o `CurrentOperation`/legacy Session interpretation;
- `operation_flow` i status mapping wyprowadzany z Command/Session/outbox;
- `ObservabilityReader` oraz `SessionProjectionReader` jako aktywne semantic projections;
- receipt/promotion-file heuristics jako status authority;
- bezpośrednie joins/queries do legacy domain schema dla target UI;
- action enablement wynikające z heurystyk zamiast canonical capability/safe-action contract.

### MINIMUM OPERATOR VIEW — CC0

CC0 nie jest throwaway UI. Używa tych samych query i view-model contracts co CC1:

- runtime/composition identity oraz inventory freshness;
- Task + intent revision;
- WorkItem disposition/outcome/wait/run;
- Effect target/digest/certainty/witness;
- named RepoView oraz exact candidate;
- minimalny Obligation/CheckPlan/Evidence/Assessment status;
- Publication/consumer/PRESENTED certainty;
- causal timeline i unresolved recovery items;
- tylko canonical safe actions: inspect, retry/reconcile, cancel/request approval — zależnie od capability.

### CANONICAL QUERY CONTRACT — minimalny zakres

Każda odpowiedź zawiera:

- `contract_version`, runtime generation i projection watermark/freshness;
- stable IDs oraz causal links: Submission → Task → WorkItem → Run/Effect → Evidence → Publication;
- outcome/disposition/wait rozdzielone, bez jednego przeciążonego statusu;
- external authority observation i certainty;
- RepoView kind/identity/provenance;
- presentation certainty per consumer;
- explicit unknown/unavailable, nigdy domyślne „OK”;
- rebuildable projection marker oraz source semantic record links;
- dozwolone safe actions z precondition/fence, ale bez wykonywania ich przez query layer.

Query contract jest read-only. Control Center nie przyznaje leases, nie przeprowadza transitions i nie interpretuje raw rows jako authority.

### vNEXT BUILD START

`CC0` zaczyna się po `M4a`, gdy istnieje pierwszy canonical WorkItem query. Może rosnąć razem z M4b–M8a, nie czekając na M9.

### MAIN UI CUTOVER

`CC1` następuje po stabilizacji target query semantics dla włączonych flows:

- M5b effect certainty/reconciliation;
- M6a/M6b evidence/read model;
- M7c promotion, jeżeli Git promotion jest już enabled;
- M8a RepoView-required queries.

Cutover może poprzedzić pełne usunięcie Browser authority. Legacy drain jest osobną, jawnie oznaczoną zakładką/sekcją tego samego canonical gateway, a nie starym main interpretation.

### LEGACY INTERPRETATION DELETE

`CC2` po M9a usuwa aktywne legacy status heuristics i action paths. Pasywny, immutable archive reader może pozostać do M12, ale:

- nie zasila main status;
- nie oferuje mutating actions;
- jest oznaczony `ARCHIVED LEGACY`;
- ma finalny death/retention contract w M12.

---

## 15. Browser Minimum Full-Quality Contract

Pierwszy realny Browser engineering slice może mieć wąski capability breadth (jedna bezpieczna klasa zmiany), lecz nie może mieć gorszej semantic quality. Minimum przed benchmarkiem/cutoverem:

### Transport i integrity

- vNext capability/version handshake przed acceptance;
- typed Context Package transport;
- chunk sequence, total count/length, fragment digest i package digest;
- exact reassembly albo jawne `INCOMPLETE/CORRUPT`; żadnego silent truncation;
- bounded retry i durable client outbox przed wysłaniem;
- local-only transport, bez wymaganego OpenAI API.

### Identity i admission

- immutable submission key utrwalony przed first send;
- ACK oznacza dopiero durable Task admission;
- lookup/retry po lost ACK i MV3 restart;
- Task/conversation binding oraz jawna intent revision;
- protocol generation niezmienna po acceptance;
- kilka Tasks rozróżnialnych; effectful work serializowany/fenced per repo/capability.

### Engineering Intelligence

- named COMMITTED RepoView z provenance;
- Context Package z coverage, known unknowns i must-see;
- ContextRequest może żądać brakującego materiału bez znajomości fragment protocol;
- Repository Understanding jest wersjonowane i jawnie partial/stale;
- Engineering Decision wiąże intent, RepoView, constraints, options/risks i chosen direction.

### Work i evidence

- przynajmniej jeden realny WorkItem przechodzi target kernel;
- exact CANDIDATE RepoView/effect subject;
- minimalny runtime-selected CheckPlan i Evidence/Assessment;
- terminalny outcome nie jest mylony z disposition/wait;
- brak automatycznego Git promotion przed M7 cutover.

### Recovery i presentation

- MV3 restart na każdym boundary: pre-send, post-send/pre-ACK, running, result-ready, presented;
- lost ACK recovery przez lookup, bez duplicate Task;
- background work i result subscription/cursor; brak obowiązkowego ręcznego pollingu;
- Publication związana z Task/WorkItem/result;
- per-consumer witness odróżnia `PRESENTED` od `PRESENTATION_UNKNOWN`;
- Resume Capsule pozwala kontynuować w nowym czacie/po compaction;
- user typing/interruption nie gubi work/result: tworzy intent revision, WAITING albo jawne cancel semantics;
- duplicate event/reconnect nie duplikuje presentation ani effect.

### Czego first slice nie musi jeszcze rozwiązywać

- pełnej autonomii i wszystkich przyszłych narzędzi;
- dowolnej równoległości effectful work;
- bezpośredniej mutacji LIVE checkout;
- pełnego semantic indexu i wszystkich dialectów;
- zdalnych workerów;
- premium Control Center UI.

**Gate:** M4e fault matrix + M2d paired benchmark muszą potwierdzić brak degradacji mechanical/local cases i materialny zysk w co najmniej jednym realnym component/architecture/diagnostic case. Dopiero M4f jest authority cutoverem.

---

## 16. Validation Scope Freeze

### Frozen minimal target

Validation pozostaje systemem operacyjnych dowodów, nie Proof Engine. Minimalne first-class concepts:

- **Obligation** — co musi być prawdziwe dla exact subject;
- **CheckPlan** — deterministycznie wybrane checks, wersje, kolejność i process policy;
- **Evidence** — immutable observation związana z exact subject i environment;
- **Assessment** — `PASS | FAIL | UNKNOWN | NOT_APPLICABLE` per obligation;
- **Subject** — exact RepoView/candidate/content/effect identity, nigdy „aktualny workspace” bez binding;
- **EnvironmentFingerprint** — OS/arch/runtime/tool versions, repo/config identity, isolation/egress i faktyczna checker availability;
- **Applicability** — oddzielona od wyniku; brak checkera nie jest `PASS`;
- **WaiverDecision** — osobny immutable decision z approverem, scope, reason i expiry; nie nadpisuje Evidence;
- **runtime-selected checks** — runtime wybiera plan z policy/capability; model nie wybiera łagodniejszego profilu.

### Execution Units

| EU | Zakres | Dlaczego osobno |
|---|---|---|
| `M4c Minimum Candidate Evidence` | Jeden rzeczywisty checker dla exact CANDIDATE; minimalne records są docelowym subsetem, nie throwawayem. | First Browser slice potrzebuje uczciwej evidence wcześniej niż pełne M6. |
| `M6a Promotion-grade Evidence Core` | Pełne minimalne semantics: obligations, multi-check evidence, assessment, applicability, environment, approval/waiver. | Zamraża gate contract, którego potrzebuje Git CAS. |
| `M6b Deterministic CheckPlan Shadow` | Runtime selection, checker registry/capability, isolation/process limits, fail/unknown behavior; shadow comparison z legacy profiles. | Oddziela selection/process mechanics od semantic records. |
| `M6c Validation/Policy Authority Cutover` | Canonical evidence/policy staje się jedynym gate'em enabled flows; legacy profile writer/selector off. | Jeden writer/policy cutover z deletion DoD. |

### Promotion-grade frozen subset wymagany przez M7

M7a/M7b implementation może rozpocząć po M6a i kontrakcie M6b, jeżeli istnieją:

1. exact candidate;
2. obligation;
3. deterministyczny selected required CheckPlan;
4. Evidence związane z exact subject i environment;
5. jednoznaczne `PASS/FAIL/UNKNOWN/NOT_APPLICABLE`;
6. applicability;
7. twardy policy/approval gate oraz oddzielny WaiverDecision.

Production Git ref writer w M7c nie uruchamia się, dopóki M6c nie odciął model-facing/legacy validation authority dla tego flow.

### Explicit out of scope

- theorem/proof graph;
- Proof Engine;
- set-cover/minimal-proof optimizer;
- ML impact predictor;
- generalized formal logic/system;
- automatyczne dowodzenie semantycznej poprawności kodu;
- nieskończony checker taxonomy;
- `FULL` jako synonim `tests_required=true`.

### Validation levels dla pierwszego tranche

| EU | AUTO granularity | Oczekiwany poziom walidacji |
|---|---|---|
| `R0a` | `MULTIPLE ORDERED AUTO LOOPS` | `REGRESSION` — unit + bounded fixture integration + existing diagnostic regressions. |
| `R0b` | `MANUAL / OPERATOR PREREQUISITE` | `CHECKPOINT/FULL` na faktycznie wspieranym lokalnym runtime; nie na nieobserwowanym GitHub snapshot. |
| `M1a` | `ONE AUTO LOOP` | `TARGETED` + composition regressions. |
| `M1b` | `MULTIPLE ORDERED AUTO LOOPS` | `CHECKPOINT/FULL` przed pierwszą production activation/schema write. |
| `X1` | `EXPERIMENT ONLY` | `TARGETED` crash/concurrency/backup matrix. |
| `X2` | `EXPERIMENT ONLY` | `TARGETED` durability/corruption/type-integrity matrix. |
| `M1c` | `MULTIPLE ORDERED AUTO LOOPS` | `REGRESSION`, w tym import/composition isolation. |
| `M2a` | `ONE AUTO LOOP` | `TARGETED` RepoView identity/provenance tests. |
| `M2b` | `MULTIPLE ORDERED AUTO LOOPS` | `REGRESSION`, w tym chunk/loss/reorder/size bounds. |
| `M2c` | `MULTIPLE ORDERED AUTO LOOPS` | `TARGETED` semantic/contract tests + real fixture. |
| `M2d` | `EXPERIMENT ONLY` | `CHECKPOINT` paired benchmark; nie wymaga automatycznie całego test suite. |
| `M3a` | `MULTIPLE ORDERED AUTO LOOPS` | `REGRESSION` — schema/identity/dedup shadow bez production route. |
| `M3b` | `MULTIPLE ORDERED AUTO LOOPS` | `REGRESSION` — Browser/Native restart, lost ACK, duplicate send i version skew. |
| `M3c` | `MANUAL / OPERATOR PREREQUISITE` | `CHECKPOINT/FULL` — pierwszy admission authority cutover. |
| `M4a` | `MULTIPLE ORDERED AUTO LOOPS` | `REGRESSION` — state machine, lease/fence/restart i canonical query. |
| `M4b` | `MULTIPLE ORDERED AUTO LOOPS` | `REGRESSION` — exact candidate/effect oraz filesystem fault matrix. |
| `M4c` | `ONE AUTO LOOP` | `TARGETED` — jeden exact checker, environment i honest unknown/applicability. |
| `M4d` | `MULTIPLE ORDERED AUTO LOOPS` | `REGRESSION` — reconnect/replay/presentation/Resume boundaries. |
| `CC0` | `ONE AUTO LOOP` | `TARGETED` — canonical query/view-model parity; no direct legacy interpretation. |
| `M4e` | `EXPERIMENT ONLY` | `CHECKPOINT/FULL` — full-quality Browser vertical slice i fault rehearsal. |
| `M4f` | `MANUAL / OPERATOR PREREQUISITE` | `CHECKPOINT/FULL` — repository generation fence i WorkItem writer cutover. |

FULL jest ponawiane przy szerokich architecture checkpoints/cutovers i po materialnym accumulated validation debt, nie po każdej tabeli lub adapterze.

---

## 17. Minimal Schema Through M4

Zasada: **schema grows with invariants**. Poniżej są durable record families, nie polecenie stworzenia od razu wszystkich finalnych tabel ani frozen nazwy SQL.

### Klasy danych

| Klasa | Semantyka | Przykłady do M4 |
|---|---|---|
| Canonical mutable state | Bieżąca pozycja state machine; single writer, fenced/versioned | Task current revision pointer, WorkItem disposition, lease/fence, Effect certainty, consumer presentation state. |
| Immutable semantic records | Decyzje/fakty, których znaczenia nie wolno przepisywać | RepoView, intent revision, ContextRequest, Engineering Decision, Run attempt, evidence, assessment, publication. |
| ContentRef storage | Typed immutable bytes i ich metadata/integrity | context fragment, manifest, patch/candidate material, logs/artifacts. Bytes poza SQLite. |
| External authority observations | To, co widziano w Git/filesystem/browser/process; z czasem/coverage/certainty | exact ref observation, filesystem witness, presentation witness, process outcome. |
| Rebuildable projections | Cache/query acceleration bez lifecycle authority | timeline/read models, index projections, operator summaries. Mogą zostać usunięte i odtworzone. |

### R0

- **Brak nowej canonical DB table.**
- `runtime-inventory-v1` jest atomowo zapisanym, immutable report artifact poza Journalem.
- Stable semantic digest pomija observation timestamp, ale obejmuje observed identities/completeness.
- R0 nie tworzy inventory authority ani history database.

### M1

- Runtime/composition/slot manifests pozostają poza candidate-controlled DB, aby Bootstrap mógł je odczytać przed otwarciem Journalu.
- M1 nie tworzy domain lifecycle tables.
- Schema compatibility floor i migration metadata mogą rozszerzyć istniejący bootstrap/migration contract, ale tylko po X1/preflight.

### M2

Minimalnie potrzebne families:

- `Repository` — current resource identity/config, nie repo state snapshot;
- immutable `RepoView(COMMITTED)` — commit/tree, repository, provenance, creation facts;
- `ContentRef` metadata + typed content store bytes;
- immutable `RepositoryUnderstandingView` lub referencja do rebuildable projection związana z exact RepoView;
- immutable `ContextPackage` + ordered typed fragment refs/coverage;
- immutable `ContextRequest` + resolution link;
- immutable `EngineeringDecision` + intent/RepoView/context links.

Nie tworzymy jeszcze CANDIDATE/LIVE-specific tables, generic knowledge graph ani final index schema.

### M3

Minimalnie potrzebne families:

- immutable `Submission` z dedup key, payload digest, protocol generation, acceptance transaction i jawny rejection/tombstone;
- `Task` current record;
- immutable `TaskIntentRevision`;
- current consumer/conversation binding;
- minimalne immutable Facts związane z transition, tylko gdy konkretny invariant ich wymaga.

Nie tworzymy tabel Branch/Plan tylko dlatego, że frozen model je przewiduje. Powstają, gdy pierwsza capability potrzebuje ich odrębnych invariants.

### M4

Minimalnie potrzebne families:

- `WorkItem` current state;
- immutable `Run`/attempt oraz `Wait`/resolution facts;
- lease, fence i resource claim records w zakresie wymaganym przez single-writer/restart safety;
- `Effect` current certainty + prepared intent;
- immutable external effect observations/witnesses;
- `RepoView(CANDIDATE)` z sealed manifest ContentRef;
- minimalne `Obligation`, `CheckPlan`, `Evidence`, `Assessment` z M4c;
- immutable `Publication` + current per-consumer observation/presentation certainty;
- Resume Capsule jako typed content/semantic record związany z Task/WorkItem.

Projection tables powstają tylko po wykazaniu query need i zawsze mają source watermark/rebuild contract.

### Zakazy schema do M4

- target foreign keys/joins wymagające legacy Command/Session rows;
- dual-write sync markers;
- generic event log „na wszelki wypadek” bez replay contract;
- final tables dla direct LIVE mutation, remote workers, Proof Engine, CAS GC albo full self-host;
- status columns łączące outcome, disposition i wait;
- mutable overwrite immutable Decision/Evidence/RepoView.

---

## 18. Worker / Process Topology Through M4

### Role count

Rozważane role: scheduler, executor, reconciler, publication dispatcher, validation runner, index builder, background assurance, promoter, checkout sync, Bootstrap, Browser subscription.

Role nie implikują procesów.

| Rola | Do M4 | Rzeczywisty proces? |
|---|---|---|
| Scheduler | Kolejkuje runnable WorkItems | Nie; moduł/pętla w Work Kernel. |
| Worker/executor | Wykonuje typed WorkItem | Nie jako stały osobny daemon; wspólny worker runtime w Kernelu. |
| Reconciler | Skanuje unresolved effects | Nie; typ pracy/timer w Kernelu. |
| Publication dispatcher | Dostarcza wynik do consumer adaptera | Nie; Kernel + adapter, z durable publication state. |
| Validation runner | Uruchamia checker | Bounded child process na czas checka, nie daemon. |
| Index builder | Buduje projection dla exact RepoView | Typ WorkItem w wspólnym worker runtime; może być deferred. |
| Background assurance | Późniejszy periodic WorkItem | Nie przed potrzebą; bez osobnego daemonu. |
| Promoter | Legacy do M7 | Do M4 pozostaje legacy process/mechanism tylko dla legacy flow; target nie tworzy nowego promotera. |
| Checkout sync | Dopiero M7b | Target WorkItem/effect, nie daemon. |
| Bootstrap | Start/update/recovery | Zewnętrzny launcher uruchamiany na żądanie, nie stały daemon. |
| Browser subscription | Connection/event adapter | Extension + Native ingress/kernel connection; nie lifecycle worker. |

### Minimalna fizyczna topologia do M4

1. **Chrome/MV3 extension process/context** — UI/conversation adapter, durable client outbox i subscription cursor; nie lifecycle authority.
2. **Native Host ingress process** — capability handshake i lokalny transport; nie otwiera canonical SQLite do zapisu i nie tworzy drugiego admission ledger.
3. **Jeden lokalny BDB Work Kernel/service process** — jedyny canonical SQLite writer; scheduler, executor, reconciler i publication dispatch są rolami wewnętrznymi. Checker może uruchomić bounded child process.
4. **Opcjonalny Control Center GUI process** — read/query + confirmed operator commands przez Kernel; nie authority.
5. **Bootstrap launcher** — external start/update/recovery boundary, invocation-only.

To jest minimalne `3 + optional GUI + invoked Bootstrap`, a nie kilkanaście daemonów. Jeżeli później isolation lub throughput wymaga osobnego worker process, musi on leasingować WorkItems przez kernel contract i nie może otwierać canonical DB bezpośrednio.

---

## 19. Rollback Classification

`Rollback` oznacza tu technicznie wykonalny powrót bez utraty lub ponownej interpretacji accepted canonical state. Tam, gdzie to nieprawda, używamy state-forward/roll-forward.

| EU / boundary | Klasyfikacja | Recovery semantics |
|---|---|---|
| `R0a` | `FULLY REVERSIBLE` | Usunąć nowy read-only provider/report; brak domain/schema mutation. |
| `R0b` | `FULLY REVERSIBLE` | Gate/raport można powtórzyć; nie przełącza writerów. |
| `M1a` | `FULLY REVERSIBLE` | Manifest/diagnostics mogą zostać wyłączone; runtime state niezmieniony. |
| `M1b` przed użyciem | `FULLY REVERSIBLE` | Candidate recovery slot/backup można odrzucić przed schema activation. |
| Pierwsza schema activation po M1b/X1 | `CODE REVERSIBLE / STATE FORWARD` | Feature code można wyłączyć, ale wraca się tylko do schema-compatible runtime; rows/migration pozostają. |
| `M1c` przed target routing | `FULLY REVERSIBLE` | Explicit composition root może pozostać disabled; legacy composition działa dalej. |
| `M2a–M2c` shadow/additive | `CODE REVERSIBLE / STATE FORWARD` | Wyłączyć readers/features; immutable rows/content zostają i są ignorowane przez compatible runtime. |
| `M2d` | `FULLY REVERSIBLE` | Benchmark nie zmienia authority. |
| `M3a` | `CODE REVERSIBLE / STATE FORWARD` | Shadow submissions/tasks mogą zostać retained; brak production acceptance. |
| `M3b` przed first accepted vNext submission | `FULLY REVERSIBLE` | Routing pozostaje legacy; client outbox nie oznacza acceptance. |
| `M3b` po accepted test submission | `CODE REVERSIBLE / STATE FORWARD` | Accepted Task musi zostać dokończony/terminalnie sklasyfikowany przez compatible target runtime. |
| `M3c Admission Authority Cutover` | `ROLL-FORWARD ONLY` | Stop intake, target recovery/reconcile. Zakaz route accepted submission z powrotem do legacy. |
| `M4a–M4e` shadow/allowlist przed writer cutover | `CODE REVERSIBLE / STATE FORWARD` | Wyłączyć nową capability, dokończyć/terminalnie sklasyfikować istniejące WorkItems; canonical facts zostają. |
| `M4f WorkItem Authority Cutover` | `ROLL-FORWARD ONLY` | Generation fence pozostaje; stop intake + reconcile target effects. Legacy Command writer nie wraca dla tego repo/capability. |
| `M6c Validation authority cutover` | `ROLL-FORWARD ONLY` | Canonical policy/evidence naprawiane forward; nie przełącza się pojedynczego flow na stary profile selector. |
| `M7c Git promotion cutover` | `ROLL-FORWARD ONLY` | Git truth + prepared effect decyduje; reconcile/CAS forward, bez reaktywacji promoter watcher. |

### Operacyjna granica

Każda karta od pierwszej canonical migration ma rozróżniać:

- **disable feature** — kod przestaje przyjmować nowe requests;
- **drain/reconcile state** — accepted work dochodzi do znanego disposition;
- **binary rollback** — dozwolony wyłącznie do schema-compatible recovery slot;
- **authority rollback** — po cutoverze zakazany; recovery jest roll-forward.

---

## 20. Conceptual Load Review

Etap 4 wprowadzał słuszne pojęcia, lecz zbyt łatwo można było zaimplementować je wszystkie przed pierwszym zamknięciem legacy authority. Freeze v1 narzuca rytm:

| Nowy invariant | Natychmiastowa redukcja starego mechanizmu |
|---|---|
| R0a/R0b exact observed baseline | Kończy zgadywanie aktywnego bundle/store; nie powstaje jeszcze nowa domain abstraction. |
| M1a identity + M1c composition | Target provider nie używa import-order/global wrapper authority; provider patches umierają portami. |
| M2 named RepoView/context/decision | GPT przestaje operować na bezimiennym „repo teraz”; nie czeka na cały kernel. |
| M3 restart-safe Submission/Task | vNext Browser ledger/Native receipts/spool przestają być admission authority. |
| M4 WorkItem/effect/publication | Command/Session/result/watches przestają sterować enabled target flow. |
| M5 effect certainty/reconciler | Lokalne retry/watch loops znikają. |
| M6 evidence/policy | Model-facing validation profiles znikają. |
| M7 Git CAS | promoter watcher/seen/sequence znikają. |
| M8 exact RepoView queries | implicit repo reads i mieszanie COMMITTED/CANDIDATE/LIVE znikają. |
| M9 drain/extinction | Browser stores/wrappers i aktywny legacy runtime znikają. |

### Concept budget przed pierwszym legacy death

Przed M3c wprowadzamy tylko:

- runtime identity/composition manifest/bootstrap floor;
- RepoView/ContentRef;
- Context Package/Request, Understanding i Decision;
- Submission/Task potrzebne dokładnie do admission cutover.

WorkItem/Effect/Publication implementation może rozpocząć się shadow po M3a, ale nie rozszerza production authority przed M3c. Każda kolejna grupa pojęć zamyka konkretną legacy klasę najwyżej w następnym cutover EU.

### Adversarial sanity pass wszystkich EU

Każda jednostka z mapy w sekcji 5:

- ustanawia named invariant, rozstrzyga prerequisite albo usuwa konkretną authority/error class;
- ma najwyżej jeden writer cutover;
- może przejść `inspection → implementation/experiment → confirmation` bez całego XL milestone'u;
- nie jest pojedynczą metodą/tabelą/ekranem;
- ma następny unlocked unit i jawny stop condition;
- jeśli tworzy bridge, death condition znajduje się w tej samej karcie lub wskazanej deletion EU.

Nie znaleziono EU wymagającej usunięcia za brak niezależnej wartości. Nie znaleziono też pozostawionego bridge'a bez death condition. Największe jednostki (`M4d`, `M5b`, `M11b`) pozostają wielowarstwowe, ale każdy z nich obejmuje jeden cross-process invariant; Etap 6 może nadać im kilka ordered AUTO loops bez rozbijania na sztuczne mikrozadania.

---

## 21. Migration Plan Freeze v1

Poniżej ostateczna dependency-first sekwencja. To execution ordering, nie nowy architecture design.

### T0 — Observe before mutation

```text
R0a Minimal Reconciliation Inventory
→ R0b Observed Local Gate
```

R0b jest operator/local prerequisite. Wynik `DRAIN`, `RECONCILE` albo `UNSUPPORTED` zatrzymuje production mutation, ale nie musi blokować czystej implementacji na fixture.

### T1 — Composition and durable substrate floor

```text
M1a Runtime Identity
├─→ M1b Expand-Compatible Bootstrap Floor → X1 SQLite Gate
├─→ X2 Content Durability Gate
└─→ M1c Explicit vNext Composition Root
```

Dozwolona równoległość: po R0b/M1a można implementować M2a na fixture/shadow; pierwszy production canonical schema/content write czeka na M1b oraz odpowiedni X1/X2.

### T2 — Early Engineering Intelligence value

```text
M2a COMMITTED RepoView
→ M2b Typed Context Transport
→ M2c Understanding / ContextRequest / Decision
→ M2d Paired Engineering Quality Gate
```

To jest pierwszy mierzalny product slice i ma wystąpić przed dużym lifecycle cutoverem.

### T3 — Admission, with kernel shadow overlap

```text
M3a Submission + Task shadow
├─→ M3b Restart-safe Browser Admission → M3c Admission Cutover
└─→ M4a WorkItem Kernel shadow
```

M4a może rozwijać się po M3a; production WorkItem routing nie wyprzedza M3c. Nie istnieją dwie admission authorities dla jednej submission.

### T4 — Full-quality Browser work slice

```text
M4a
→ M4b Exact Candidate + Local Effect
→ M4c Minimum Candidate Evidence
→ M4d Publication / Presentation / Resume
├─→ CC0 Minimum Operator View
├─→ M8a RepoView-required query contract
└─→ M4e Full-quality Browser Rehearsal
    → M4f Generation-fenced WorkItem Cutover
```

CC0 i M8a mogą rosnąć równolegle po odpowiednich query records. M4f czeka na drain/fence i operator confirmation.

### T5 — Effect certainty and adapter simplification

```text
M5a Effect Certainty + Reconciler
→ M5b Core Adapter Witness Cutover / local retry deletion
```

### T6 — Validation and Git authority

```text
M6a Promotion-grade Evidence Core
├─→ M6b Deterministic CheckPlan Shadow → M6c Validation Cutover
└─→ M7a Prepared Git CAS Adapter → M7b Checkout Sync

M6c + M7a + M7b
→ M7c Git Promotion Cutover
```

M7 implementation nie czeka na enrichment całego M6, ale production ref effect czeka na canonical validation authority.

### T7 — Repository query enrichment and Control Center main

```text
M8a (już dostępne dla target path)
→ M8b IndexView / Understanding Cutover
→ CC1 Control Center vNext Main Cutover
```

`M8c Honest LIVE Observation` może powstać po E4 równolegle, lecz nie blokuje CC1 ani M9. Direct LIVE mutation pozostaje deferred.

### T8 — Compatibility drain and Browser authority extinction

```text
M9a Legacy Ingress Closure + Drain
→ CC2 Legacy Active Interpretation Delete
→ M9b Browser Lifecycle Extinction + parity benchmark
```

### T9 — Self-host hardening

```text
E3 CAS backup/GC gate, jeżeli GC ma zostać enabled
→ M11a Bootstrap Slots
→ M11b Activation Fault Matrix
→ M11c Bootstrap/Self-host Cutover
```

M1b zapewnia wcześniej minimum recovery; M11 dopiero pełną self-host authority.

### T10 — Compatibility zero and release

```text
M12a Compatibility Zero + Archive/Contract Gate
→ M12b Freeze Release + Final Deletion
```

### Freeze constraints na całej sekwencji

- Browser Mode pozostaje primary i nie wymaga API;
- single lifecycle writer i no dual admission/write;
- Engineering Intelligence jest w T2, nie po migracji infrastruktury;
- Control Center jest query/command client, nigdy authority;
- external effects mają prepared intent, witness i generation fence;
- state-forward granice używają compatible recovery + roll-forward;
- każda karta ma `STOP / ESCALATE` zamiast scope creep;
- legacy mechanism umiera przy pierwszym bezpiecznym usage-zero gate, nie „na końcu dla pewności”.

---

## 22. Delta From Etap 4

Każdy materialny element Etapu 4 otrzymuje dokładnie jeden status Freeze. `SPLIT` oznacza zachowanie celu, ale wykonanie przez formalne Execution Units. `MERGED` dla eksperymentu oznacza, że jego gate/fault cases są acceptance konkretnej EU, nie osobnym projektem.

### 22.1. Plan elements

| Element Etapu 4 | Status | Materialna delta / powód |
|---|---|---|
| Architecture target / one Work Kernel | `FROZEN` | Brak sprzeczności z kodem; bez reopen. |
| Browser-first primary workflow bez API | `FROZEN` | First slice ma pełny quality contract, choć wąski capability breadth. |
| Incremental strangler + protocol partition | `REVISED` | Dodano repository/capability generation fence dla współdzielonych external resources. |
| Same physical SQLite | `FROZEN` | Z warunkami single writer, no target legacy joins i expand-compatible recovery floor. |
| `R0 runtime-inventory-v1` | `SPLIT` | R0a reusable minimum + R0b real-local gate; deep inventories just-in-time. |
| `M1 Runtime/Composition/Bootstrap` | `SPLIT` | M1a identity, M1b recovery floor, M1c explicit composition; M2 nie czeka na cały cleanup. |
| `M2 Engineering Intelligence` | `SPLIT` | M2a–M2d; przeniesiony na najwcześniejszy bezpieczny product tranche. |
| `M3 Admission/Task` | `SPLIT` | Shadow substrate, restart-safe adapter i osobny authority cutover. |
| `M4 Work Kernel/Browser slice` | `SPLIT` | M4a–M4f + CC0; rehearsal i generation fence przed cutoverem. |
| `M5 Effect adapters/reconciliation` | `SPLIT` | M5a certainty/reconciler, M5b witness cutover/deletion. |
| `M6 Validation` | `SPLIT` | Early non-throwaway M4c; M6a semantic core, M6b plan shadow, M6c authority cutover. |
| `M7 Git promotion` | `SPLIT` | Prepared CAS, checkout sync i writer cutover oddzielone; pełne M6 nie blokuje implementation. |
| `M8 Repository Intelligence/LIVE` | `SPLIT` | M8a jest Browser deletion prerequisite; M8b enrichment i M8c LIVE nie są. |
| Direct LIVE mutation | `DEFERRED` | Nie blokuje primary candidate workflow ani M9. |
| `M9 Browser demotion/removal` | `SPLIT` | Ingress closure/drain i lifecycle extinction/parity oddzielone. |
| `M10 Control Center` | `REVISED` | CC0 od M4, CC1 przed/obok M9, CC2 po drain; shell reuse pozostaje. |
| `M11 Bootstrap/self-host` | `SPLIT` | Slot substrate, fault matrix i authority cutover osobno; M1b daje wcześniejsze minimum recovery. |
| `M12 final deletion/release` | `SPLIT` | Compatibility-zero/archive gate oddzielony od release/delete. |
| Worker/process topology | `REVISED` | Role skonsolidowane w jednym Kernel process; brak daemon explosion. |
| Final-schema-up-front tendency | `REMOVED` | Schema rośnie z invariantami R0–M4; brak future tables. |
| Control Center jako query/client, nie authority | `FROZEN` | Realny kod potwierdza reuse shell + replace interpretation. |
| Compatibility horizon do M12 „na wszelki wypadek” | `REVISED` | Writers/runtime code umierają wcześniej per-domain; M12 jest usage-zero/archive gate. |

### 22.2. Experiment elements E1–E15

| Element | Freeze status | Disposition |
|---|---|---|
| E1 | `REQUIRES EXPERIMENT` | Standalone `X1`. |
| E2 | `REQUIRES EXPERIMENT` | Standalone `X2`. |
| E3 | `DEFERRED` | Przed CAS GC / coordinated restore. |
| E4 | `DEFERRED` | Przed M8c. |
| E5 | `DEFERRED` | Dopiero przy enable direct LIVE mutation. |
| E6 | `MERGED` | Fault subsets w M1b/M4b/M7a/M7b. |
| E7 | `MERGED` | M2b/M4d. |
| E8 | `MERGED` | M3b. |
| E9 | `MERGED` | M4d/M4e. |
| E10 | `MERGED` | M4d/M4e. |
| E11 | `MERGED` | M6a. |
| E12 | `MERGED` | M6b. |
| E13 | `MERGED` | M1b minimum + M11b full matrix. |
| E14 | `MERGED` | M2d. |
| E15 | `REMOVED` | Umbrella project usunięty; adapter-specific witness tests pozostają obowiązkowe. |

### 22.3. Co nie zostało zmienione

- Task/WorkItem/Run/Effect/Publication separation;
- facts explainability zamiast event-sourcing dogmatu;
- ContentRef oraz typed immutable content;
- RepoView `COMMITTED/CANDIDATE/LIVE` authorities;
- prepared effects + external truth reconciliation;
- Git CAS i separate checkout effect;
- Bootstrap poza candidate-controlled runtime;
- Control Center bez lifecycle authority;
- zakaz trwałego dual-write i zakaz wymaganego OpenAI API.

---

## 23. First Execution Pack

### ID / Name

`R0a — Minimal Reconciliation Inventory`

### Parent milestone

`R0 — runtime-inventory-v1`

### Type

Primary: `IMPLEMENTATION`  
Secondary: `INSPECTION`

### Goal

Zbudować reusable, read-only provider/CLI `runtime-inventory-v1`, który przed dalszą mutacją daje bounded, versioned i fail-closed obraz obserwowanej instalacji albo jednoznacznie stwierdza, czego nie udało się zaobserwować.

### Invariant

> Żadna mutująca Execution Unit nie rozpoczyna się na prawdziwym BDB bez inventory o exact runtime/store identities i kompletnym disposition jej wymaganych authority domains; brak lub niestabilność obserwacji nigdy nie oznacza `SAFE`.

R0a ustanawia mechanizm. R0b stosuje go do rzeczywistej instalacji i podejmuje gate decision.

### Why now

Aktualny GitHub HEAD jest znany, ale lokalny runtime nie. Kod rozprasza authority pomiędzy Journal, Browser storage, Native receipts/spool, promoter state, composition patches i aktywne procesy. Bez wspólnego minimalnego inventory każda kolejna karta musiałaby zgadywać baseline albo pisać jednorazowe skrypty.

### Preconditions

- aktualny repo checkout i source commit są jawnie wskazane;
- praca implementacyjna odbywa się w izolowanym, czystym worktree;
- aktualne instrukcje repo/AGENTS i istniejące diagnostics/config contracts zostały odczytane;
- dostępne są fixtures lub kopie testowe obecnych store formats;
- R0a nie wymaga dostępu do prywatnej lokalnej instalacji użytkownika; taki dostęp jest prerequisite dopiero R0b.

### Required fresh inspection

Przed implementacją AI ma ponownie sprawdzić na aktualnym kodzie:

1. repository instructions, branch/HEAD/status i różnice względem frozen baseline;
2. wszystkie istniejące konfiguracje ścieżek do Journalu, spool, receipt store, promoter state, extension/native bundles;
3. aktualne migration list/schema checks, WAL/locking i integrity helpers;
4. istniejące operator diagnostics, export/sanitization i bounded-read utilities;
5. identity/version sources dla service, Native Host i Browser bundle;
6. aktualne terminal/non-terminal classifications używane przez legacy stores;
7. czy jakikolwiek collector wymagałby zapisu lub start/stop procesu.

Jeżeli świeży kod materialnie zmienia store ownership albo format, karta zatrzymuje się i aktualizuje scope przed patchem.

### Scope

R0a obejmuje:

- versioned inventory schema i deterministic serialization;
- exact source checkout identity: repository, branch/ref jeśli dostępny, HEAD, dirty/clean/unknown, worktree identity;
- declared/observed runtime composition: service/native/browser bundle IDs, versions i digests, jeśli lokalnie dostępne;
- jawnie skonfigurowane store paths oraz ich containment/type/existence;
- read-only SQLite observations: schema/migration versions/checksums, integrity outcome, WAL presence, lock/busy/active writer observation bez przejmowania locka;
- bounded unresolved/non-terminal counts i stable IDs dla obecnego Journalu, Native receipts, spool i promotion state w zakresie potrzebnym do sklasyfikowania pierwszych migracji;
- observed process/lock/PID identity tylko dla deklarowanych BDB resources, bez globalnego process census;
- per-source status: `OBSERVED | UNAVAILABLE | UNSTABLE | INVALID | UNSUPPORTED`;
- per-domain completeness i blocker reason;
- overall result vocabulary co najmniej `READY_FOR_LOCAL_GATE | INCOMPLETE | INVALID | UNSUPPORTED` — nie production `SAFE`;
- stable semantic digest wykluczający observation time, lecz obejmujący source identities, contents disposition i completeness;
- atomowy local report output, human-readable summary i machine-readable payload;
- private exact representation oraz sanitized export policy;
- provider interface możliwy do reuse przez System Health, Bootstrap preflight i Control Center diagnostics;
- unit/integration/fault/Windows tests oraz operator documentation kontraktu.

### Explicit out of scope

- pełny Browser diagnostic export i historia wszystkich extension keys;
- pełne receipt/archive stores oraz payload content;
- kompletna rekonstrukcja wrapper/import order;
- cała historia promotera i wszystkie archived results;
- wszystkie wersje pakietów systemowych;
- kompletny globalny process topology;
- uruchomienie wszystkich 12 benchmarków i pełnego test suite;
- cleanup, drain, reconcile, start/stop, install, schema migration lub cutover;
- UI Control Center;
- canonical lifecycle/database authority;
- automatyczna decyzja, że produkcyjna mutacja jest bezpieczna — to R0b.

### Current authority

Authority pozostaje w obserwowanych systemach: Git/filesystem, aktywnych bundle manifests, Journal, Native receipt/spool stores, promoter/Git oraz OS process/lock observations. Obecne ad-hoc diagnostics jedynie odczytują ich fragmenty.

### Authority after completion

Bez zmian domain authority. `runtime-inventory-v1` jest **evidence artifact**, nie source of truth i nie lifecycle state machine. Może powiedzieć „zaobserwowano X w czasie T”, ale nie może ustanowić, że X nadal jest aktualne ani dokonać transition.

### Existing mechanics to reuse

- istniejące bounded operator diagnostic readers;
- aktualne config/path parsers i default resolution, po jawnym oznaczeniu source;
- Git object/status readers bez mutacji;
- obecne sanitization/export helpers;
- read-only SQLite connection oraz schema/checksum/integrity helpers;
- istniejące terminal/non-terminal classifiers tylko po przypisaniu ich do konkretnej legacy generation;
- atomic file replacement utility, jeżeli jest już sprawdzona i nie dotyka domain store.

Reuse nie obejmuje legacy status heuristics jako target authority.

### Must preserve

- brak mutacji obserwowanych stores;
- bounded runtime i bounded output;
- fail-closed completeness;
- Windows path/locking semantics;
- path containment i brak przypadkowego skanowania innych repozytoriów/profili;
- działanie offline/local-only;
- prywatność payloadów, promptów, kodu i credentials;
- możliwość uruchomienia, gdy część BDB jest stopped lub uszkodzona;
- możliwość późniejszego reuse tego samego kontraktu przez System Health/Bootstrap/MOV.

### Must not do

- nie uruchamiać ani nie zatrzymywać BDB/Browser/Git procesów;
- nie wykonywać cleanup, stash, reset, checkout, migration, WAL checkpoint ani repair;
- nie rezerwować receipts, nie claimować spool/result i nie ackować watch;
- nie otwierać SQLite w trybie write ani przejmować production locka;
- nie odczytywać niezadeklarowanych profili/ścieżek przez szerokie globy;
- nie traktować missing/parse error/busy jako pusty lub clean;
- nie zbierać sekretów ani pełnych payloadów, gdy wystarczy identity/digest/count;
- nie tworzyć drugiej DB, inventory ledger ani lifecycle authority;
- nie maskować różnicy między GitHub HEAD, checkout i aktywnym binary.

### Implementation contract

1. Collector działa wyłącznie na jawnej allowliście source descriptors.
2. Każdy collector zwraca identity, observation interval, status, completeness, bounded facts, errors i sanitizer metadata.
3. Wyniki nie są scalane heurystycznie. Konflikt dwóch identities jest jawny.
4. Collector nie może dokonać zapisu nawet w celu „naprawy” locka/WAL/formatu.
5. Jeśli source zmieni się podczas obserwacji, wynik jest `UNSTABLE` i zawiera pre/post identity.
6. Stable digest jest policzony z canonical semantic payload; observation timestamp i durations są poza digestem.
7. Overall gate jest funkcją versioned policy nad per-domain completeness; partial nigdy nie staje się `READY_FOR_LOCAL_GATE` dla dotkniętej domeny.
8. Report posiada schema/version compatibility oraz explicit unsupported-version error.
9. Bounded IDs/counts muszą wystarczyć do następnego R0b/deep inspection bez kopiowania prywatnych contentów.
10. Ten sam provider contract jest wywoływalny z CLI, późniejszego Bootstrapu i Control Center bez trzech niezależnych interpretacji.

### Inputs / outputs

**Inputs:**

- explicit repository/worktree path;
- explicit or config-resolved BDB installation/profile root;
- declared Journal, receipt, spool, promotion i bundle descriptors;
- inventory policy/version;
- sanitization mode;
- optional bounded timeout/record caps, z bezpiecznymi defaults.

**Outputs:**

- machine-readable `runtime-inventory-v1` report;
- concise human summary;
- inventory ID, stable semantic digest, observation interval;
- per-domain completeness/status/blocker;
- sanitized export artifact, jeżeli jawnie żądany;
- process exit/result rozróżniający complete, incomplete, invalid i unsupported.

### Persistence implications

- brak zmian schema i brak zapisów do Journalu/legacy stores;
- report zapisywany atomowo jako immutable artifact poza canonical DB;
- retencja jest bounded/operator-controlled; nie powstaje append-only inventory database;
- w późniejszym etapie report może zostać zarejestrowany jako ContentRef, ale R0a tego nie implementuje;
- write failure reportu nie zmienia obserwowanych stores i zwraca jawny error.

### Security/privacy constraints

- local-only, bez network egress i bez wymaganego API;
- allowlisted roots, resolved-path containment i symlink/reparse-point safety;
- brak credential values, tokens, cookies, pełnych conversation payloads i source blobs;
- exact lokalne paths/IDs tylko w private report; sanitized export pseudonimizuje je stabilnie lub usuwa zgodnie z policy;
- digests nie mogą być przedstawiane jako anonimowe, jeśli umożliwiają korelację;
- error messages nie mogą przypadkowo wypisywać payloadów;
- report permissions powinny być co najmniej tak restrykcyjne jak istniejące diagnostics.

### Error semantics

- `UNAVAILABLE`: source deklarowany, ale nieosiągalny; nie oznacza empty;
- `UNSTABLE`: identity/content zmieniło się podczas observation;
- `INVALID`: source istnieje, lecz narusza format/integrity/containment;
- `UNSUPPORTED`: wersja/format poza znanym read contract;
- `INCOMPLETE`: brakuje required domain coverage;
- timeout/busy/permission denied pozostaje typed observation error;
- unknown unresolved-effect count blokuje affected cutover;
- partial success report jest zachowany diagnostycznie, ale nie daje safe gate.

### Stop conditions

AI kończy `STOP — assumptions no longer valid`, jeśli:

- current repo/branch materially różni się od baseline lub zawiera nieoczekiwane user changes w scope;
- authority/store ownership jest niejasne albo istnieje nowy writer;
- required collector wymaga write, repair, process control lub szerokiego skanu;
- path resolution może wyjść poza declared root;
- format/store nie ma bounded, side-effect-free read path;
- trzeba byłoby stworzyć dual-write lub inventory lifecycle DB;
- acceptance wymaga rozszerzenia do pełnego R0/Control Center/Bootstrap;
- existing invariant test nie przechodzi i przyczyna nie jest zlokalizowana;
- Architecture Freeze contract jest materialnie sprzeczny z obserwowanym kodem.

### Escalation conditions

Escalacja do Principal/operatora jest wymagana, gdy:

- real local HEAD/bundle/schema różni się materialnie od raportu Etapu 5;
- znaleziono active unknown binary/process trzymający domain lock;
- schema/migration checksum jest unsupported lub integrity nie jest clean;
- unresolved effect nie daje się sklasyfikować bez mutacji;
- receipt/spool/promoter identities konfliktują;
- safe Windows read-only observation jest niewykonalne;
- wspierany test environment/checker nie może zostać uruchomiony;
- kontynuacja wymaga zmiany authority, security boundary lub frozen architecture.

### Unit tests

- deterministic canonical serialization i stable digest;
- timestamps/durations nie zmieniają semantic digest;
- każda per-source status/error mapping;
- conflict i pre/post identity → `UNSTABLE`;
- missing/busy/permission/parse error nigdy nie daje empty/ready;
- bounded record/count truncation jest jawna i blokuje completeness, gdy potrzebna;
- sanitization usuwa secrets/payloads/paths zgodnie z policy;
- path containment, symlink/reparse escape i allowlist;
- overall gate truth table;
- unsupported version/schema handling;
- brak write calls w collector interfaces.

### Integration tests

- clean fixture zgodna z aktualnym HEAD/schema;
- fixture z Journal + WAL, active/non-terminal Command/Session/effect-like rows;
- receipt reserved bez spool file oraz spool bez receipt;
- promoter `seen`/sequence/ref disagreement;
- różne checkout HEAD i active bundle identity;
- stopped installation z kompletnymi durable stores;
- partial/corrupt/unsupported stores;
- CLI/provider dają semantycznie ten sam report;
- sanitized export nie zmienia prywatnego reportu ani source stores;
- żaden test nie pozostawia zmodyfikowanego fixture/store.

### Fault injection

- source zmienia identity pomiędzy pre/post observation;
- SQLite busy/locked/IO error/corrupt header;
- WAL pojawia się lub znika w trakcie odczytu;
- file truncate/partial JSON/rename podczas scan;
- permission denied i path disappearance;
- timeout w pojedynczym collectorze;
- ogromny spool/receipt store wymuszający cap;
- duplicate/conflicting IDs;
- output atomic-write failure/disk full;
- process identity znika lub PID zostaje reused podczas observation.

### Windows-specific tests

- backslash/case normalization bez utraty exact display path;
- junction/symlink/reparse-point containment;
- locked SQLite/WAL i sharing violation;
- atomic replace behavior przy antywirusie/otwartym readerze;
- long paths i Unicode profile/repository names;
- PID/process observation bez admin privileges;
- file timestamp granularity nie wpływa na semantic identity;
- CRLF/encoding w manifestach i configach;
- brak zależności od POSIX-only locks/signals.

### Browser verification if applicable

Nie jest wymagane do `R0a DONE`, ponieważ R0a nie zmienia extension. Fixture testuje tylko bounded odczyt deklarowanych Browser bundle/storage exports, jeżeli taki read adapter już istnieje.

W R0b operator może wykonać normalny Browser diagnostic/export handshake. Pełny Browser export pozostaje późniejszym domain inventory, nie blockerem implementacji R0a.

### Validation level

`REGRESSION`

Obejmuje targeted unit/integration/fault suite oraz istniejące diagnostics/config regressions. `FULL` nie jest wymagany dla read-only R0a; wspierany full local checkpoint jest częścią R0b przed pierwszą mutacją production runtime.

### Acceptance / DONE

R0a jest DONE, gdy:

1. versioned provider/CLI generuje deterministyczny report dla wszystkich required minimal sources;
2. każdy brak, konflikt, niestabilność i unsupported format jest fail-closed;
3. exact vs sanitized representation mają przechodzące privacy tests;
4. fixtures pokrywają ghost receipt/spool, schema/WAL, promoter disagreement i bundle/repo mismatch;
5. Windows-specific targeted suite przechodzi na wspieranym środowisku albo karta zatrzymuje się z jawnym platform blockerem;
6. istniejące diagnostics/config regressions przechodzą;
7. nie ma schema/domain/store mutation ani nowej authority;
8. provider ma jawny reuse contract dla R0b, System Health, Bootstrap preflight i CC0;
9. documentation wymienia dokładnie, czego R0a nie obserwuje;
10. fresh inspection nie ujawnił nieujętego authority albo został wykonany formalny STOP/escalation.

### Rollback classification

`FULLY REVERSIBLE`

Wyłączenie/usunięcie provider/CLI i report artifacts nie wymaga state migration. Source stores nigdy nie zostały zmienione.

### Legacy writer/read affected

- **Writers:** żaden.
- **Reads:** nowe bounded, read-only adapters do już istniejących sources.
- **Authority:** bez zmian.
- **Compatibility:** adapters muszą deklarować obsługiwane legacy format versions; nie wykonują auto-upgrade.

### Cleanup/deletion

- brak deletion legacy mechanism;
- test temp/report artifacts są usuwane lub retencjonowane według test policy;
- żaden one-off inventory script nie może pozostać równolegle — jeśli prototyp istnieje, ma zostać scalony do provider contract albo usunięty w tej EU;
- deep domain collectors dodawane później rozszerzają ten contract, nie tworzą `runtime-inventory-v2` jako konkurencyjnej authority.

### Telemetry

Local-only, bounded telemetry:

- inventory ID/schema version/semantic digest;
- collector status, completeness, duration i error class;
- observed source versions/digests zgodnie z privacy policy;
- truncation/cap/timeout flags;
- overall gate reason;
- zero full payloads, credentials, source content i conversation content;
- brak zewnętrznego wysyłania telemetry.

### AUTO granularity

`MULTIPLE ORDERED AUTO LOOPS`

Typowo dwie pętle:

1. fresh inspection + contract + pure collectors + unit confirmation;
2. integration/fault/Windows confirmation + reuse wiring + documentation.

Nie wolno łączyć z R0b real-local gate ani z M1a mutation. Operator/local observation pozostaje osobnym prerequisite.

### Next unlocked Execution Unit

`R0b — Observed Local Gate`

---

## 24. Next Two Execution Units

| ID / nazwa | Invariant | Dependency | Typ |
|---|---|---|---|
| `R0b — Observed Local Gate` | Rzeczywisty lokalny runtime i wszystkie authority domains potrzebne do następnego tranche mają exact disposition `SAFE/DRAIN/RECONCILE/UNSUPPORTED`; unknown nie przechodzi gate'u. | `R0a` | `VALIDATION GATE`; `MANUAL / OPERATOR PREREQUISITE` |
| `M1a — Runtime Identity + Composition Manifest` | Aktywny service/native/browser provider, bundle, schema i composition source są explainable i porównywalne; mismatch jest jawny. | `R0b` z bezpiecznym disposition albo zatwierdzonym drain/reconcile planem | `IMPLEMENTATION` |

---

## 25. Etap 6 Readiness

Granularność jest wystarczająca do przygotowania **Canonical Execution Map + AI Implementation Playbook** bez fałszywej precyzji:

- każda EU ma jeden invariant/authority boundary;
- substrate, rehearsal, cutover i deletion są rozdzielone tam, gdzie mają inne rollback semantics;
- dependencies wskazują implementation start osobno od production activation;
- każde bridge ma ownera i death EU;
- schema powstaje just-in-time;
- AUTO granularity rozróżnia one-loop, ordered loops, experiment i operator prerequisite;
- validation level nie domyśla się FULL;
- poprawnym wynikiem każdej karty może być `STOP — assumptions no longer valid`;
- Etap 6 może dodać świeże file/symbol inspection i patch scope bez wpisywania dziś kruchych nazw funkcji/linii.

Etap 6 powinien zachować dokładnie ID i invariants z sekcji 5/21, a dla każdej karty dopisać aktualne: dependencies, authority before/after, fresh inspection, stop/escalation, validation, rollback, deletion i next unlock. Nie powinien ponownie negocjować architecture ani łączyć całych XL milestones w jedną mutację.

### Final adversarial verdict

Tak — po korektach z tego review plan jest wystarczająco precyzyjny, prosty i bezpieczny, aby zakończyć projektowanie architektury migracji i rozpocząć realną implementację.

> **Migration Plan Freeze v1 = READY TO EXECUTE**

> **READY FOR ETAP 6 — CANONICAL EXECUTION MAP + AI IMPLEMENTATION PLAYBOOK**

Minimalny warunek operacyjny przed mutacją prawdziwej instalacji pozostaje jeden: wykonać R0a, a następnie uzyskać R0b na obserwowanym lokalnym runtime. To nie jest nowa faza architektoniczna; to pierwszy frozen execution gate.

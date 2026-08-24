# Bartosz Dev Bridge — BDB vNext

BDB vNext jest lokalnym systemem łączącym zwykły ChatGPT, planowanie w Work, canonical stan projektu, wykonanie zadań oraz Browser/Native transport na Windows.

Branch bieżącego rozwoju: `bdb-vnext`.

> Repo zawiera również dużą ilość kodu i dokumentacji wcześniejszych generacji (POC, Local Workspace Loop, Browser 0.4.x, Control Center 0.2/0.3). Nie należy traktować ich jako opisu aktualnego BDB vNext. Zacznij od [`docs/DOCUMENTATION_STATUS.md`](docs/DOCUMENTATION_STATUS.md).

## Jak działa vNext

Główny przepływ projektu:

```text
Project Center
→ Project Brief
→ zwykły ChatGPT: planning directive
→ BDB: Przygotuj dla Work
→ Work: project-plan.json
→ BDB validation/import
→ Project Memory: immutable plan history
→ Start / Continue / AUTO milestone
→ Project Execution binding
→ ChatGPT wykonuje bieżący task
→ bdb-project-execution-submission-v1
→ acceptance / replay / recovery
→ następny task lub stop
```

### Podział odpowiedzialności

- **zwykły ChatGPT** — analiza, architektura, planning directive i praca nad bieżącym zadaniem;
- **Work** — tworzenie lub aktualizacja kompletnego `project-plan.json`; nie jest execution authority;
- **ProjectCatalog** — metadane projektu i summary;
- **Project Memory** — canonical historia planów, decyzje, zdarzenia i execution subdocument;
- **Project Execution** — bindingi, attempts, acceptance, task statuses, milestone AUTO, watchdog i durable launch handoff;
- **Git** — authority dla rzeczywistych bytes/HEAD projektu;
- **Browser Extension vNext** — wykrywanie resultów, bezpieczny submit, prompt handoff i AUTO Send;
- **Native Host vNext** — pinned lokalny transport do canonical BDB runtime.

## Project Plan

Canonical format planu to JSON schema:

```text
bdb-project-plan-v1
```

Plan jest immutable planning baseline/history. Postęp wykonania nie jest księgowany przez ręczne przepisywanie planu — runtime progress należy do Project Memory / Project Execution.

Work dostaje canonical identity, Project Brief, bieżący plan (przy update), planning directive oraz dokładny schema contract. Ma zwrócić jeden kompletny JSON plan, bez implementacji kodu.

Szczegóły: [`docs/VNEXT_PROJECT_WORKFLOW.md`](docs/VNEXT_PROJECT_WORKFLOW.md).

## Project Execution

Każde wykonanie jest związane z dokładną identity, m.in.:

```text
project_id
plan_version
task_id
execution_binding_id
launch_id
correlation_id
command_id
repo_alias
expected_repo_head_before
conversation_id
```

ChatGPT kończy task jednym blokiem JSON:

```text
bdb-project-execution-submission-v1
```

BDB sprawdza identity, HEAD, result digest i acceptance criteria. Exact replay nie tworzy drugiego attemptu.

## Milestone AUTO

AUTO jest jawnie uruchamiane dla bieżącego milestone i działa sekwencyjnie — bez równoległego wykonywania tasków i bez automatycznego startu następnego milestone.

Docelowy happy path:

```text
canonical result
→ automatic result submit
→ PASS acceptance
→ ensure/recover one next launch
→ exact prompt insertion
→ composer/conversation/binding guard
→ automatic Send
→ durable handoff SENT / ACK
→ kolejny task
```

AUTO zatrzymuje się przy m.in. FAIL/blocked, review, NEEDS_USER, manual confirmation, gate/open question, stale state, policy stop, transport failure albo `STOP AUTO`.

Nie ma globalnego limitu liczby iteracji ani całkowitego czasu milestone; bounded pozostają pojedyncze operacje techniczne.

Szczegóły: [`docs/VNEXT_AUTO_BROWSER_NATIVE.md`](docs/VNEXT_AUTO_BROWSER_NATIVE.md).

## Browser vNext

Źródło:

```text
browser_extension_vnext/
```

Identity:

```text
Bartosz Dev Bridge vNext
Extension ID: mopnolkjddkmgojfjkenjobehhmmklll
Manifest V3
```

Rozszerzenie działa tylko na `https://chatgpt.com/*`, używa `nativeMessaging` i `storage` oraz nie traktuje DOM/local storage jako canonical Project authority.

Manual `Submit result` pozostaje fallbackiem; aktywny AUTO może wykonać tę samą canonical submission path automatycznie.

## Native Host vNext

Dedicated host:

```text
com.bartosz.dev_bridge.vnext
```

Bieżący Native transport obsługuje m.in. status/handshake, canonical admission, project launch claim/ack oraz project execution status/submit.

Legacy host `com.bartosz.dev_bridge` nie jest hostem vNext Project Execution.

## Produkcyjny runtime Windows

Canonical vNext runtime root:

```text
%LOCALAPPDATA%\BartoszDevBridge-vNext
```

Docelowy client layout:

```text
BartoszDevBridge-vNext\
├─ clients\
│  ├─ browser-extension\
│  ├─ native-host\
│  └─ client-plan.json
└─ config\
   └─ native-host.json
```

Branch HEAD i aktualnie zainstalowany runtime są odrębnymi faktami. Wersję produkcyjną należy potwierdzać przez source identity/client plan/digests i aktywne authority, a nie przez założenie, że najnowszy commit jest już wdrożony.

Production admission wymaga zgodności zewnętrznego Bootstrap ACTIVE, M9b Browser/Native gate oraz M3c intake/admission.

Szczegóły: [`docs/VNEXT_PRODUCTION_RUNTIME.md`](docs/VNEXT_PRODUCTION_RUNTIME.md).

## Control Center / Project Center

`bdb-control-center` uruchamia project-centric GUI z widokami projektu i technicznym vNext Control Center.

Project Center obsługuje m.in.:

- tworzenie/otwieranie projektu;
- import/update planu;
- planning prompt i Work;
- Start/Continue;
- `AUTO: bieżący milestone` i `STOP AUTO`;
- handoff;
- review.

Techniczny CC1 jest read-only projection nad canonical vNext state i nie ma Legacy fallback.

## CLI / entrypoints

Repo nadal zawiera wcześniejsze CLI oraz vNext entrypoints. Najważniejsze vNext entrypoints z pakietu to m.in.:

```text
bdb-control-center
bdb-vnext-manifest
bdb-vnext-bootstrap
bdb-vnext-bootstrap-admin
bdb-vnext-native-host
bdb-vnext-cutover
bdb-vnext-artifact
bdb-vnext-m9a-handoff
bdb-vnext-final-prepare
bdb-vnext-legacy-recovery
bdb-vnext-m12a
bdb-vnext-m12a-closure
bdb-vnext-maintenance
```

Ich obecność nie oznacza, że każda activation/writer/intake gate jest aktualnie włączona.

## Current documentation

- [Status i klasyfikacja dokumentacji](docs/DOCUMENTATION_STATUS.md)
- [Bieżąca architektura vNext](docs/VNEXT_CURRENT_ARCHITECTURE.md)
- [Project workflow](docs/VNEXT_PROJECT_WORKFLOW.md)
- [AUTO / Browser / Native](docs/VNEXT_AUTO_BROWSER_NATIVE.md)
- [Production runtime Windows](docs/VNEXT_PRODUCTION_RUNTIME.md)
- [Frozen governance packet](docs/governance/README.md)
- [ADR index](docs/adr/README.md)

## Historyczna dokumentacja

Milestone records, closure assessments, GHB/Local Workspace Loop, wcześniejsze Browser pilots i Control Center docs pozostają w repo jako historical evidence. Ich obecność nie nadaje im statusu bieżącego kontraktu.

Najbardziej mylące generyczne dokumenty legacy Browser/Native oraz root POC start guides zostały usunięte z aktywnego drzewa; ich treść pozostaje w historii Git. Zobacz [`docs/legacy/README.md`](docs/legacy/README.md).

## Znane luki bieżącego source baseline

Dokumenty CURRENT są oparte na baseline `56994a1b6abbfb275a974781c752d106fb48e201`. Na tym source istnieją znane rozjazdy wymagające naprawy implementacji:

- Browser receipt UI może myląco nazwać poprawnie przetworzony FAIL jako `Result accepted`;
- blocked/review milestone projection może niespójnie pokazać `RUNNABLE` lub przesunąć cursor.

Nie są to zamierzone reguły produktu. Canonical FAIL/blocked/review ma zatrzymywać AUTO.

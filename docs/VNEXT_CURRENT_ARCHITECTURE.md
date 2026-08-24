# BDB vNext — bieżąca architektura

Status: **CURRENT**  
Source baseline: `56994a1b6abbfb275a974781c752d106fb48e201`

Ten dokument opisuje działającą architekturę `bdb-vnext`, a nie historyczny POC, Browser 0.4.x ani wcześniejszy Local Workspace Loop.

## 1. Rola systemu

BDB vNext rozdziela cztery odpowiedzialności:

- **zwykły ChatGPT** — analiza, rozmowa, architektura, przygotowanie planning directive oraz wykonanie bieżącego zadania w kontekście projektu;
- **Work** — przygotowanie lub aktualizacja kompletnego `project-plan.json`; Work jest etapem planowania, nie authority wykonania;
- **BDB vNext** — canonical state projektu, plan history, Project Execution, bindingi, acceptance, AUTO orchestration, Browser/Native transport i projekcje GUI;
- **repozytorium projektu** — Git pozostaje authority dla rzeczywistych bytes/HEAD kodu projektu.

BDB nie powinien polegać na pamięci rozmowy ani browserowym DOM jako źródle prawdy o stanie projektu.

## 2. Canonical authority map

### ProjectCatalog

`bdb_vnext.project_catalog.ProjectCatalog`

Odpowiada za bounded metadane projektu:

- `project_id`;
- nazwę i `repo_alias`;
- lokalną ścieżkę repo;
- opcjonalne `owner/name` GitHuba;
- Project Brief;
- summary planu i ostatnie identity rozmowy/launchu.

Catalog nie jest runtime execution authority. Pola takie jak `completed_tasks` i `current_task` są projekcją/sumaryzacją i nie mogą przeważać nad Project Memory/Project Execution.

### Project Plan

Canonical schema: `bdb-project-plan-v1`.

Plan jest walidowany jako JSON. Project Memory zapisuje immutable plan history i pointer do bieżącej wersji. Aktualizacja planu używa preview/diff i tworzy następcę; nie nadpisuje historycznych planów.

Markdown planu może być renderowany dla człowieka, ale nie jest formatem wejściowym authority.

### Project Memory

`bdb_vnext.project_memory.ProjectMemoryStore`

Project Memory przechowuje:

- immutable plan history;
- events;
- decisions;
- inbox;
- risks;
- technical debt;
- attention;
- checkpoints;
- bounded `execution` subdocument.

Project Execution jest częścią canonical Project Memory, a nie osobną konkurencyjną bazą stanu.

### Project Execution

`bdb_vnext.project_execution.ProjectExecutionCoordinator`

Odpowiada za:

- `ProjectExecutionBinding`;
- task statuses;
- attempts;
- acceptance results;
- current task/binding cursor;
- milestone AUTO runs;
- checkpoints/watchdog;
- exact replay detection;
- durable launch handoff `PENDING/SENT`.

Wynik z modelu nie zmienia stanu na podstawie prose. Musi przejść strict `bdb-project-execution-submission-v1` i zgadzać się z canonical bindingiem.

### ProjectLaunchQueue

`bdb_vnext.project_launch.ProjectLaunchQueueAdapter`

To transportowa kolejka jednego pending promptu z claim lease. Nie jest semantic authority. Launch może zawierać exact project/task/binding identity oraz `auto_send`.

Historycznie plik kolejki znajduje się w kompatybilnościowej lokalizacji `%LOCALAPPDATA%\BartoszDevBridge\project-launch-queue.json`. Sama lokalizacja nie czyni starego runtime authority; canonical semantic state pozostaje w vNext Project Memory/Execution.

### Browser local storage

`chrome.storage.local` przechowuje bounded lokalną projekcję launch/bindingów dla transportu i recovery. Nie jest durable authority projektu.

Utrata browser storage nie może unieważnić istniejącego canonical Project Execution bindingu.

### Native Host

Host: `com.bartosz.dev_bridge.vnext`.

Native Host jest lokalnym, pinned transportem pomiędzy Browserem a BDB vNext. Odczytuje canonical runtime i wykonuje tylko jawnie obsługiwane akcje. Caller jest związany z extension ID `mopnolkjddkmgojfjkenjobehhmmklll`.

Native Host nie jest authority dla planu ani task selection — deleguje do ProjectWorkflow/ProjectExecution.

### Browser Extension vNext

Źródło: `browser_extension_vnext/`.

Browser:

- wykrywa canonical JSON result w odpowiedzi asystenta;
- tworzy idempotentny panel statusu/manual fallback;
- w AUTO może automatycznie submitować wynik;
- odbiera/pobiera kolejny canonical launch;
- chroni composer przed nadpisaniem tekstu użytkownika;
- w aktywnym AUTO może wstawić prompt i wywołać Send;
- nigdy nie wybiera kolejnego taska samodzielnie.

Legacy `browser_extension/` i dokumenty 0.4.x nie opisują tego komponentu.

## 3. Identity chain

Dla jednego execution taska BDB wiąże co najmniej:

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
conversation_id (po bezpiecznym bindzie)
```

Result musi powtórzyć dokładne identity z bindingu. `head_before` jest porównywany z expected binding HEAD, a `head_after` dokumentuje rezultat wykonania.

Browserowy panel jest deduplikowany semantycznie po identity submission, a nie po identity konkretnego DOM node.

## 4. Acceptance i replay

Canonical result flow:

```text
JSON result
→ strict parse
→ canonical binding lookup
→ identity/head checks
→ exact result digest
→ acceptance criteria evaluation
→ attempt + acceptance result
→ task transition
→ milestone transition
→ optional next AUTO launch
```

Exact replay nie tworzy drugiego attemptu. Receipt może zwrócić istniejący rezultat i — dla wcześniej zaakceptowanego PASS w aktywnym AUTO — zapewnić/recoverować usable current-task launch.

Transport `ok=true` nie oznacza automatycznie task `PASS`. `accepted` i `result_status` są odrębną semantyką.

## 5. Task status i milestone AUTO

Typowe task statuses:

```text
pending | active | review | completed | blocked | skipped
```

AUTO jest milestone-scoped i sekwencyjne. BDB nie ustala globalnego limitu liczby iteracji ani czasu całego taska/milestone; bounded są pojedyncze operacje techniczne, payloady i polling.

Zamierzona semantyka:

- `PASS` → task `completed`, przejście do następnego runnable taska w tym samym milestone;
- `REVIEW_REQUIRED`/odpowiedni unknown → `review`, AUTO stop;
- `FAIL` → `blocked` lub odpowiedni nie-PASS state, AUTO stop;
- `STOP AUTO` → brak dalszego auto-send;
- ukończony milestone → stop przed następnym milestone.

Browser nie może traktować samego faktu, że inny task jest technicznie runnable, jako pozwolenia na ominięcie blocked/review taska aktywnego runu.

## 6. Launch handoff

Current source posiada durable `bdb-project-launch-handoff-v1` ze stanami:

```text
PENDING
SENT
```

W AUTO `PENDING` oznacza, że canonical launch istnieje i nadal wymaga wysłania. `SENT` oznacza trwałe potwierdzenie handoffu powiązane z project/binding/launch/conversation.

Zamierzona kolejność AUTO:

```text
claim
→ canonical gate
→ insert exact prompt
→ verify composer
→ Send
→ ACK/mark sent
```

Manual Start/Continue może pozostać trybem insert-without-send.

## 7. Control Center

`bdb_vnext.control_center_query` stanowi read-only canonical projection boundary dla technicznego CC1. Otwiera istniejący vNext Control DB w trybie read-only i nie używa Legacy fallback.

`bdb_gui.project_center.ProjectCenterWindow` jest project-centric powierzchnią użytkownika: tworzenie/otwieranie projektu, plan, Work, Start/Continue, milestone AUTO, STOP, handoff i review.

GUI ma wyświetlać runtime progress na podstawie canonical Project Execution, a nie z przestarzałej kopii catalog summary.

## 8. Production activation authority

Source branch i zainstalowany runtime to dwie różne rzeczy.

Production admission vNext wymaga zgodności niezależnych authority:

1. zewnętrzny M11c Bootstrap ACTIVE;
2. M9b Browser/Native client gate ACTIVE;
3. M3c intake/admission enabled.

Sam staging Browser/Native bytes nie aktywuje produkcji.

Szczegóły: [`VNEXT_PRODUCTION_RUNTIME.md`](VNEXT_PRODUCTION_RUNTIME.md).

## 9. Znane defekty source baseline 56994a1

Na tym baseline dokumentacja odnotowuje dwa znane rozjazdy implementacji od powyższej zamierzonej semantyki:

1. Browser receipt UI może nazwać poprawnie obsłużony `FAIL` jako `Result accepted`, ponieważ obecna ścieżka UI nie rozróżnia jeszcze konsekwentnie transport success od task acceptance.
2. `reconcile()`/milestone projection może dla aktywnego runu `blocked` lub `review` policzyć kolejny runnable task i wystawić niespójny cursor/status `RUNNABLE`.

Dopóki source fix nie zostanie wdrożony, operator powinien traktować canonical `task_status=blocked/review` oraz run status jako stop, nawet jeśli UI/cursor sugeruje następny task.

## 10. Non-authorities

Nie są authority dla bieżącego execution state:

- tekst prose w rozmowie;
- sam DOM ChatGPT;
- browser local cache;
- ProjectCatalog summary, jeśli różni się od Execution;
- historyczne POC/Browser 0.4.x docs;
- `.codex\visualizations` jako live runtime;
- stary Legacy Native Host `com.bartosz.dev_bridge`.

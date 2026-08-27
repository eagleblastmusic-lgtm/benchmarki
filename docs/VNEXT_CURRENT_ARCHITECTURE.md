# BDB vNext — bieżąca architektura

Status: **CURRENT**  
Qualified source subject (NX-069): `a6aa681ccbf40ca181834ed3fe628152a06dd406`
Qualified source tree: `a496aefa0667498985f0a117c5e13bf59f2be9ef`
Current state and deployed slot observation: [`NX070_CURRENT_STATE.md`](NX070_CURRENT_STATE.md)

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

Canonical kolejka transportowa znajduje się w repo-local
`runtime/control/project-launch-queue.json`. Nie jest authority semantycznym;
canonical Task/Project state pozostaje w vNext Project Memory/Execution.

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

Exact replay nie tworzy drugiego attemptu. Receipt może zwrócić istniejący rezultat i — tylko dla wcześniej zaakceptowanego PASS w aktywnym AUTO — zapewnić/recoverować usable current-task launch.

Transport `ok=true` nie oznacza task `PASS`. Browser continuation wymaga równocześnie `receipt.accepted === true` oraz `receipt.result_status === "PASS"`.

FAIL, replayed FAIL, REVIEW_REQUIRED i UNKNOWN nie są accepted PASS i nie mogą uruchomić next AUTO launch.

## 5. Task status i milestone AUTO

Typowe task statuses:

```text
pending | active | review | completed | blocked | skipped
```

AUTO jest milestone-scoped i sekwencyjne. BDB nie ustala globalnego limitu liczby iteracji ani czasu całego taska/milestone; bounded są pojedyncze operacje techniczne, payloady i polling.

Semantyka bieżącego source:

- `PASS` → task `completed`, przejście do następnego runnable taska w tym samym milestone;
- `REVIEW_REQUIRED`/odpowiedni unknown → `review`, AUTO stop;
- `FAIL` → `blocked` lub odpowiedni nie-PASS state, AUTO stop;
- `STOP AUTO` → brak dalszego auto-send;
- ukończony milestone → stop przed następnym milestone.

Dla aktywnego milestone runu durable `run.status` jest authority dla continuation. Tylko `running` może przesuwać execution cursor przez wyliczone `next_task_id`.

Dla `blocked` i `review`:

- `current_task_id` pozostaje na tasku blokującym/review;
- milestone projection raportuje odpowiednio `BLOCKED` lub `REVIEW_REQUIRED`;
- `runnable_task_ids` jest puste;
- Browser nie może przeskoczyć do innego technicznie runnable taska.

Nieznany run state jest fail-closed i nie jest admission do Browser AUTO.

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

## 9. Zweryfikowane fail-stop semantics through NX-069

The historical pre-NX-070 repair is retained as history; the accepted
NX-069 qualified source revalidates these implementation semantics:

1. Browser receipt UI nie utożsamia już transport success z task acceptance. `Result accepted` jest możliwe wyłącznie przy `accepted === true` i `result_status === "PASS"`. FAIL/replayed FAIL są prezentowane jako failure.
2. `reconcile()` oraz milestone projection zachowują blocked/review authority. Cursor pozostaje na tasku blokującym/review, a projekcja nie wystawia `RUNNABLE`; tylko run `running` może korzystać z `next_task_id` do przesuwania kursora.

Dodatkowo Browser AUTO zatrzymuje chain na każdym nie-accepted-PASS, obejmując FAIL, REVIEW i UNKNOWN.

The source-bound NX-069 qualification and focused regression checks cover
FAIL/replayed FAIL, blocked/review cursor, the existing PASS flow, STOP
semantics, JavaScript/Python syntax, and diff integrity.

This is source-level evidence. Production package promotion and production
cutover remain separate operations; see the current-state snapshot for the
observed ACTIVE generation and the NX-070 boundary.

## 10. Non-authorities

Nie są authority dla bieżącego execution state:

- tekst prose w rozmowie;
- sam DOM ChatGPT;
- browser local cache;
- ProjectCatalog summary, jeśli różni się od Execution;
- historyczne POC/Browser 0.4.x docs;
- `.codex\visualizations` jako live runtime;
- stary Legacy Native Host `com.bartosz.dev_bridge`.

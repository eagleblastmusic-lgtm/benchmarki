# BDB vNext — Project workflow

Status: **CURRENT**  
Qualified source subject (NX-069): `a6aa681ccbf40ca181834ed3fe628152a06dd406`
Qualified source tree: `a496aefa0667498985f0a117c5e13bf59f2be9ef`
Current release/state boundary: [`NX070_CURRENT_STATE.md`](NX070_CURRENT_STATE.md)

## 1. Utworzenie projektu

Project Center tworzy lub otwiera projekt i rejestruje go w `ProjectCatalog`.

Dla nowego projektu BDB może utworzyć lokalne repo, początkowy commit, Project Brief oraz prywatne repo GitHub przez jawny adapter `gh`. Project Brief jest kontekstem wejściowym dla planowania; nie jest wykonywalnym planem.

## 2. Planning directive w zwykłym ChatGPT

Pierwszy prompt BDB do zwykłego ChatGPT prosi o analizę projektu i przygotowanie bounded planning directive.

Na tym etapie:

- nie implementuje się kodu;
- nie tworzy się jeszcze canonical `project-plan.json`;
- ChatGPT opisuje, co Work ma zaplanować, zachować lub poprawić.

## 3. „Przygotuj dla Work”

`WorkPlanningPromptBuilder` opakowuje:

- canonical project identity;
- Project Brief;
- aktualny canonical Project Plan, jeżeli istnieje;
- planning directive użytkownika/ChatGPT;
- dokładny JSON Schema `bdb-project-plan-v1`.

Tryb jest deterministyczny:

```text
brak planu      → CREATE_PROJECT_PLAN
istnieje plan   → UPDATE_PROJECT_PLAN
```

Wersja planu również jest deterministyczna: pierwszy plan ma wersję 1, a update jest następną wersją i wskazuje `supersedes_version`.

Work ma zwrócić jeden kompletny `project-plan.json`. Work nie implementuje kodu projektu w tym workflow i nie jest runtime execution authority.

## 4. Import i aktualizacja Project Plan

BDB czyta wyłącznie valid JSON zgodny z `bdb-project-plan-v1`.

Import:

1. waliduje schema, identity, milestones, tasks, dependencies, gates/open questions i planning context;
2. zapisuje pierwszy plan do immutable Project Memory history;
3. aktualizuje current-plan pointer;
4. zapisuje w ProjectCatalog tylko summary potrzebne do listowania.

Update:

1. jest najpierw previewowany;
2. porównuje candidate z current canonical plan;
3. chroni ukończone elementy i identity;
4. po zaakceptowaniu zapisuje pełny successor plan jako nową immutable wersję.

Nie należy ręcznie edytować Project Memory, żeby „naprawić” postęp.

## 5. Start / Continue

`ProjectWorkflow.queue_start_prompt()` i `queue_continue_prompt()` przygotowują execution launch.

Przed promptem BDB:

- odczytuje current repo HEAD;
- wybiera canonical runnable/current task przez Project Execution;
- tworzy lub odzyskuje `ProjectExecutionBinding`;
- wiąże exact `project_id`, `plan_version`, `task_id`, `execution_binding_id`, `launch_id`, `correlation_id`, `command_id`, `repo_alias` i expected HEAD.

Prompt wykonawczy zawiera dokładną identity, acceptance criteria i instrukcję zwrócenia jednego machine-readable resultu.

## 6. Format wyniku

Końcowy wynik zadania ma być jednym blokiem JSON zgodnym z:

```text
bdb-project-execution-submission-v1
```

Wymagane pola obejmują:

```text
schema
project_id
plan_version
task_id
execution_binding_id
correlation_id
command_id
repo_alias
head_before
head_after
execution_status
validation_status
promotion_status
result_summary
evidence_refs
criteria
```

`failure_code` i `canonical_refs` są opcjonalne, gdy rzeczywiście istnieją.

YAML, prose albo „podobny” JSON nie są canonical resultem.

## 7. Validation i acceptance

Po submission BDB:

1. odczytuje canonical binding;
2. sprawdza project/task/plan/correlation/command/repo/head identity;
3. oblicza semantic result digest;
4. rozpoznaje exact replay przed mutacją;
5. klasyfikuje acceptance criteria;
6. zapisuje attempt i acceptance result;
7. aktualizuje task/milestone state.

Deterministyczny PASS wymaga zgodnej walidacji i kryteriów. Kryteria manual/external mogą prowadzić do `REVIEW_REQUIRED`. FAIL lub niespełniona walidacja nie może być traktowana jak PASS tylko dlatego, że transport odebrał result.

Status typu `WAITING_EXTERNAL` nie jest sam w sobie successful validation status. Jeżeli canonical acceptance kończy się FAIL, task pozostaje blokujący i AUTO nie może przejść dalej.

## 8. Replay

Ponowne wysłanie dokładnie tego samego resultu nie tworzy nowego attemptu.

BDB zwraca receipt z `replayed=true`. Dla replayed PASS w aktywnym AUTO workflow może zapewnić lub odzyskać dokładnie jeden launch bieżącego następnego taska. Dla replayed FAIL nie wolno omijać zablokowanego zadania ani prezentować replayu jako accepted PASS.

## 9. Milestone AUTO

AUTO jest jawnie uruchamiane dla bieżącego milestone i działa sekwencyjnie.

Happy path:

```text
current task
→ wykonanie w ChatGPT
→ canonical JSON result
→ Browser auto-submit
→ BDB acceptance PASS
→ current task completed
→ ensure/recover exactly one next launch
→ Browser insert
→ Browser auto-Send
→ następny task
```

Po ukończeniu milestone AUTO zatrzymuje się. Następny milestone wymaga nowej decyzji/startu, jeśli bieżący kontrakt tak stanowi.

AUTO nie ma globalnego limitu liczby iteracji ani całkowitego czasu milestone. Poszczególne operacje techniczne mogą mieć bounded timeout, size limit i polling policy.

## 10. Stop conditions

AUTO zatrzymuje się przy co najmniej:

- `FAIL` / blocked;
- `REVIEW_REQUIRED` / review;
- `UNKNOWN` / niejednoznacznym nie-accepted result;
- manual visual confirmation;
- `NEEDS_USER`;
- gate/open question wymagającym interwencji;
- stale result/binding/launch;
- conversation/project/task mismatch;
- policy stop;
- unrecoverable transport error;
- jawny `STOP AUTO`;
- końcu milestone.

Browser continuation po result submission wymaga równocześnie `accepted === true` oraz `result_status === "PASS"`.

Browser nie może sam „przeskoczyć” do innego runnable taska, jeśli canonical active run jest blocked/review. W tych stanach execution cursor pozostaje na tasku blokującym/review, a projekcja nie raportuje `RUNNABLE`.

## 11. Review i poprawki

Project Center posiada jawne akcje review. Manual approval nie może nadpisać deterministic failure. Request changes wraca do tego samego taska zgodnie z canonical execution state, zamiast tworzyć arbitralny nowy task.

## 12. Watchdog / WAITING_EXTERNAL

Długi task sam w sobie nie jest failure. Watchdog rozróżnia aktywną pracę, oczekiwanie na zewnętrzne CI/build i brak postępu. Same-binding resume ma zachować project/task/binding identity.

`WAITING_EXTERNAL` może opisywać stan wykonania/oczekiwania, ale nie należy utożsamiać go z successful validation. Acceptance pozostaje zależne od canonical validation result i criteria.

Prompt wykonawczy instruuje, by nie robić bezproduktywnego, nieograniczonego pollingu zewnętrznych operacji.

## 13. Plan vs execution

To rozróżnienie jest krytyczne:

```text
project-plan.json = immutable planning baseline/history
Project Memory.execution = bieżący runtime execution authority
calculator/app result = derived from actual repo + validation
```

Nie należy aktualizować `project-plan.json`, aby księgować każdy completed task. Postęp wykonania należy do Project Execution/Memory.

## 14. Zweryfikowana fail-stop semantyka through NX-069

The accepted NX-069 qualified source retains the fail-stop behavior below;
the historical repair that first introduced it is not the current source or
deployment identity.

- FAIL/replayed FAIL nie są pokazywane jako `Result accepted`;
- Browser AUTO nie kontynuuje dla FAIL, REVIEW ani UNKNOWN;
- blocked/review zachowują current task na blockerze/review;
- tylko aktywny run `running` może przesunąć cursor przez `next_task_id`;
- blocked/review milestone projection nie raportuje `RUNNABLE`.

The source-bound NX-069 qualification and focused regressions cover these
conditions.

Nie jest to dowód deploymentu: qualified source and current production ACTIVE are separate
states; future candidate promotion is also separately documented in the current snapshot.

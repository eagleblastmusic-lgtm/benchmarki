# BDB vNext — AUTO, Browser i Native transport

Status: **CURRENT**  
Qualified source subject (NX-069): `a6aa681ccbf40ca181834ed3fe628152a06dd406`
Qualified source tree: `a496aefa0667498985f0a117c5e13bf59f2be9ef`
Current release/state boundary: [`NX070_CURRENT_STATE.md`](NX070_CURRENT_STATE.md)

Ten dokument dotyczy `browser_extension_vnext` oraz Native Host `com.bartosz.dev_bridge.vnext`. Nie opisuje legacy Browser 0.4.x.

## 1. Browser identity

Bieżące rozszerzenie:

```text
name: Bartosz Dev Bridge vNext
manifest: MV3
extension id: mopnolkjddkmgojfjkenjobehhmmklll
host: https://chatgpt.com/*
permissions: nativeMessaging, storage
```

Content script działa w isolated world. Browser nie uzyskuje authority do wyboru taska ani modyfikacji Project Memory poza jawnie obsługiwanymi transport actions.

## 2. Native identity

Dedicated vNext Native Host:

```text
com.bartosz.dev_bridge.vnext
```

Host akceptuje wyłącznie pinned Browser origin i protokół/generation zgodny z vNext config.

Aktualne Native actions obejmują:

```text
status
handshake
admission.submit
admission.lookup
project_launch_peek
project_launch_claim
project_launch_ack
project_execution_status
project_execution_submit
```

Browser nie powinien używać legacy hosta `com.bartosz.dev_bridge` dla vNext project execution.

## 3. Wykrywanie canonical resultu

Content adapter obserwuje odpowiedzi ChatGPT i rozpoznaje strict JSON result:

```text
bdb-project-execution-submission-v1
```

Wykrywanie używa:

- MutationObserver dla streamowanego/rerenderowanego DOM;
- okresowego safety sweep;
- strict JSON parse;
- assistant-message ownership;
- semantic identity resultu.

Panel jest montowany poza wewnętrznym CodeMirror/scrollerem ChatGPT, aby nie został przycięty przez layout edytora.

## 4. Deduplikacja panelu i submitu

DOM node nie jest logical identity. ChatGPT może wymienić `<code>` podczas rerenderu.

Project Execution panel jest deduplikowany w ramach assistant message po canonical identity zawierającym:

```text
schema
project_id
plan_version
task_id
execution_binding_id
correlation_id
command_id
```

Dwa równoważne code nodes mają jeden panel. Usunięty panel może zostać odtworzony. Różne canonical results mogą mieć różne panele.

AUTO result submit dodatkowo posiada guard przeciw wielokrotnemu submission tego samego logical resultu.

## 5. Manual fallback

Przycisk `BDB vNext: Submit result` pozostaje widocznym manual fallbackiem i narzędziem diagnostycznym.

W aktywnym, poprawnym milestone AUTO użytkownik nie powinien musieć go klikać — content adapter może uruchomić tę samą canonical submit path automatycznie po pozytywnym AUTO gate.

Nie istnieją dwie różne semantyki acceptance: manual i AUTO mają korzystać z tej samej Native/ProjectWorkflow ścieżki.

## 6. Canonical AUTO gate przed result submit

Przed automatycznym submit Browser odczytuje `project_execution_status` i sprawdza co najmniej:

- aktualną conversation;
- `project_id`;
- `task_id`;
- `execution_binding_id`;
- aktywny, niesuperseded binding;
- current binding/current task;
- aktywny milestone AUTO w stanie pozwalającym na kontynuację.

Mismatch zatrzymuje AUTO. Browser nie naprawia canonical state na podstawie DOM.

## 7. Result submit i receipt

Browser wysyła:

```text
result
conversation_id
opcjonalny launch_id hint
```

Local browser binding jest tylko hintem. Native odczytuje canonical binding z Project Execution. Jeżeli Browser poda `launch_id`, musi zgadzać się z canonical bindingiem; brak browser cache nie może sam w sobie blokować recovery.

Receipt rozróżnia:

- transport processing success;
- `accepted` task acceptance;
- `result_status` (`PASS`, `FAIL`, review itd.);
- `replayed`;
- milestone status/progress;
- optional `next_launch` i jego status.

**Transport `ok=true` nie jest równoznaczny z task PASS.**

Browser uznaje result za accepted PASS wyłącznie wtedy, gdy jednocześnie:

```text
receipt.accepted === true
receipt.result_status === "PASS"
```

FAIL/replayed FAIL, REVIEW_REQUIRED i UNKNOWN nie mogą zostać pokazane jako `Result accepted` i nie mogą uruchomić następnego AUTO launchu.

## 8. Ensure/recover next launch

Po accepted PASS w aktywnym AUTO `ProjectWorkflow` zapewnia jeden usable launch dla canonical current taska.

Recovery nie używa starego JSON resultu ani Browser storage jako authority. Weryfikuje:

- active milestone run;
- canonical current task;
- brak zakończonego attemptu tego current taska;
- current/pending binding;
- queue state;
- durable launch handoff.

Jeżeli istnieje poprawny pending launch, jest reużywany. Jeżeli istnieje current binding bez usable queue launchu, może zostać przywrócony i użyty do odtworzenia dokładnie jednego launchu. Repeated replay nie powinien tworzyć wielu bindingów/launchów.

Recovery nie jest uruchamiane po FAIL/REVIEW/UNKNOWN tylko dlatego, że inny task jest technicznie runnable.

## 9. Browser launch flow

`ProjectLaunch` zawiera `auto_send: bool`.

Manual launch może pozostać:

```text
claim → insert → ACK → użytkownik ręcznie Send
```

Aktywny AUTO używa innej granicy completion:

```text
claim
→ canonical AUTO status check
→ persist local CLAIMED hint
→ insert exact BDB prompt
→ verify current conversation/composer
→ verify canonical state ponownie
→ find semantic Send control
→ Send
→ canonical ACK + launch_handoff SENT
→ local ACKED projection
```

ACK w AUTO ma oznaczać wykonany browser handoff, nie sam fakt wstawienia tekstu.

## 10. Durable launch handoff

Project Execution przechowuje:

```text
bdb-project-launch-handoff-v1
```

Stany:

```text
PENDING — launch istnieje i nadal wymaga Send
SENT    — exact launch/binding/conversation został przekazany do ChatGPT
```

`SENT` jest idempotentne. Recovery może rozpoznać już wysłany handoff bez ponownego Send.

## 11. Composer protection

AUTO nigdy nie może wysłać przypadkowej wiadomości użytkownika.

Przed insertion:

- composer musi być jednoznacznie znaleziony;
- nie może zawierać obcego tekstu/attachment state.

Po insertion:

- tekst musi być exact canonical BDB prompt;
- zmiana przez użytkownika zatrzymuje AUTO;
- conversation/task/binding muszą pozostać zgodne;
- Send musi być semantycznie znaleziony i dostępny.

Rozszerzenie nie powinno opierać krytycznego flow na hashed CSS classes, współrzędnych ani arbitralnym sleepie.

## 12. STOP i race safety

Browser utrzymuje bounded AUTO state/epoch. STOP unieważnia pending browser continuation.

Po STOP nie wolno:

- submitować nowego resultu jako część starego AUTO chain;
- wysyłać oczekującego promptu;
- pozwolić observerowi/timerowi wznowić stary run.

Canonical milestone state pozostaje ostatecznym gate, a browserowy state jest dodatkową ochroną przed race.

## 13. Milestone boundary i fail-stop authority

AUTO działa tylko w bieżącym milestone i sekwencyjnie. Po `MILESTONE_COMPLETED` Browser nie uruchamia automatycznie następnego milestone.

Dla aktywnego runu:

- `running` może korzystać z deterministic `next_task_id`;
- `blocked` utrzymuje cursor na tasku blokującym i projekcję `BLOCKED`;
- `review` utrzymuje cursor na tasku wymagającym review i projekcję `REVIEW_REQUIRED`;
- unknown run state jest fail-closed;
- `STOPPED`, user/gate/open-question/policy/stale states przerywają chain.

Browser nie może interpretować istnienia innego runnable taska jako pozwolenia na ominięcie blocked/review current taska.

## 14. Browser polling

Content adapter okresowo sprawdza pending project launch. Polling jest transportowym mechanizmem delivery, a nie source of truth.

Claim ma krótki lease. Brak ACK nie powinien trwale zgubić PENDING AUTO handoffu; recovery ma być możliwe po refresh/restart, o ile canonical identity pozostaje aktualne.

## 15. Zweryfikowane fail-stop semantics through NX-069

The accepted NX-069 qualified source retains the following fail-stop
semantics. The historical repair that introduced them remains historical
evidence rather than a current source declaration:

### Receipt label / AUTO continuation

Po Native response Browser oblicza accepted PASS na podstawie obu warunków: `accepted === true` i `result_status === "PASS"`.

FAIL/replayed FAIL są pokazywane jako failure, nie jako `Result accepted`. Automatyczny chain zatrzymuje się dla każdego nie-accepted-PASS, w tym FAIL, REVIEW i UNKNOWN.

### blocked/review projection

Project Execution używa durable run status jako authority. `blocked` i `review` nie projektują `RUNNABLE`, mają puste `runnable_task_ids` i zachowują current task na blockerze/review. Tylko `running` może przesuwać cursor przez progress `next_task_id`.

The source-bound NX-069 qualification and focused regressions cover these
conditions.

Real ChatGPT Browser smoke and production promotion are separate operations;
source validation must not be presented as proof that the qualified source is
the currently installed production client.

## 16. Legacy distinction

Nie mylić z usuniętymi z aktywnej dokumentacji opisami:

```text
Browser extension 0.4.x
bdb-action-v1
loop_id / iteration legacy AUTO
com.bartosz.dev_bridge
native arm TTL legacy flow
```

To wcześniejsza generacja. Historia jest dostępna w Git; bieżący vNext używa Project Plan/Project Memory/Project Execution oraz dedicated vNext Browser/Native identity.

# BDB vNext — AUTO, Browser i Native transport

Status: **CURRENT**  
Source baseline: `56994a1b6abbfb275a974781c752d106fb48e201`

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

## 13. Milestone boundary

AUTO działa tylko w bieżącym milestone i sekwencyjnie. Po `MILESTONE_COMPLETED` Browser nie uruchamia automatycznie następnego milestone.

`blocked`, `review`, `STOPPED`, user/gate/open-question/policy/stale states muszą przerwać chain.

## 14. Browser polling

Content adapter okresowo sprawdza pending project launch. Polling jest transportowym mechanizmem delivery, a nie source of truth.

Claim ma krótki lease. Brak ACK nie powinien trwale zgubić PENDING AUTO handoffu; recovery ma być możliwe po refresh/restart, o ile canonical identity pozostaje aktualne.

## 15. Znane luki baseline 56994a1

### Receipt label

Bieżący source może po `response.ok=true` ustawić UI `BDB vNext: Result accepted` bez pełnego rozróżnienia `receipt.accepted/result_status`. To może wizualnie nazwać replayed FAIL „accepted”.

Kontrakt CURRENT jest jednoznaczny: FAIL/review nie są PASS i nie mogą uruchomić next AUTO launch.

### blocked/review projection

Bieżący Project Execution reconcile/snapshot może w pewnych stanach aktywnego `blocked`/`review` runu wystawić niespójny `RUNNABLE`/next cursor. Browser AUTO gate nie powinien traktować takiej projekcji jako pozwolenia na obejście blokady. Source fix jest wymagany.

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

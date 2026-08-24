# BDB vNext — produkcyjny runtime Windows

Status: **CURRENT**  
Implementation baseline: `eae9fee9d171d61ded3c9cf539058559679aa9c8`

## 1. Canonical runtime root

Domyślny vNext runtime root na Windows:

```text
%LOCALAPPDATA%\BartoszDevBridge-vNext
```

Legacy runtime jest osobny:

```text
%LOCALAPPDATA%\BartoszDevBridge
```

Te rooty nie mogą się nakładać.

## 2. Produkcyjny client layout

Current client staging/deployment primitives używają layoutu:

```text
%LOCALAPPDATA%\BartoszDevBridge-vNext\
├─ clients\
│  ├─ browser-extension\
│  ├─ native-host\
│  │  ├─ <native executable>
│  │  └─ com.bartosz.dev_bridge.vnext.json
│  └─ client-plan.json
└─ config\
   └─ native-host.json
```

Browser extension ładowana przez operatora powinna pochodzić z produkcyjnego `clients\browser-extension`, a nie z katalogu roboczego Codexa.

## 3. Identity

Canonical vNext identities:

```text
source branch: bdb-vnext
generation: bdb-vnext-g1
protocol: bdb-vnext-protocol-v1
runtime id: devmaster.bdb.vnext.runtime
native host: com.bartosz.dev_bridge.vnext
browser extension id: mopnolkjddkmgojfjkenjobehhmmklll
```

Browser `manifest.json` zawiera pinned public key, z którego wynika stały extension ID.

## 4. Source HEAD != installed runtime

GitHub branch HEAD opisuje najnowsze źródło. Nie dowodzi, że identyczne bytes są już uruchomione na komputerze.

Installed runtime należy weryfikować przez source identity i digests zapisane w client/runtime evidence, m.in. `client-plan.json`, manifestach i odpowiednich activation records.

Dokumentacja nie powinna mówić „produkcja jest na HEAD X” tylko dlatego, że branch wskazuje X.

Implementation baseline dokumentacji może być starszym commitem kodowym niż branch HEAD, jeżeli późniejsze commity dotyczą wyłącznie dokumentacji. To nadal nie mówi nic o zainstalowanym runtime.

## 5. Staging vs live production

`stage_client_plan()` przygotowuje dokładne Browser/Native bytes, config i client plan. Sam staging nie jest aktywacją produktu.

Katalogi takie jak:

```text
.codex\visualizations\...
staging\clients\<sha>\...
```

mogą być tymczasowym build/recovery inputem, ale nie powinny być opisywane jako finalny live runtime produktu.

Po świadomym deploymentcie docelowe live components powinny znajdować się pod canonical runtime root.

### 5.1 Canonical client promotion

Przejście ze staged client plan do stabilnego runtime wykonuje wyłącznie
`bdb_vnext.m11c_client_promotion.promote_client_plan()` (CLI:
`m11c_cutover_cli promote-clients`). Staged runtime jest wejściem
content-addressed; nie wolno nadpisywać istniejącego live client setu ani
kopiować plików ręcznie.

Promocja jest transakcją recoverable dla jednego runtime root:

```text
PREPARED
  -> LIVE_BACKED_UP       (old clients/config moved to recovery/<id>/previous)
  -> NEW_CLIENTS_INSTALLED
  -> VERIFIED              (path-bound plan/config/manifest + route readback)
  -> COMMITTED
```

Przed pierwszym ruchem wykonywany jest exact readback staged plan, bieżącego
live planu, HKCU 32/64 route i braku Legacy route. Staged Browser/native bytes
oraz produkcyjnie związane manifest/config/client-plan są budowane przez
istniejące serializery. Każdy stan transakcji ma digestowany zapis i pozostawia
`previous` jako recovery authority. Awaria po ruchu uruchamia dokładny rollback
do poprzedniego spójnego zestawu; rollback jest potwierdzany przez ponowny
client-plan i route readback. Awaria, której nie da się tak potwierdzić, kończy
się `promotion_recovery_required` bez cichego wyboru innego źródła.

Wznowienie sprawdza istniejącą transakcję przed normalnym live preflightem.
Jest to wymagane, ponieważ po przerwaniu podczas backupu live root może być
chwilowo niekompletny. Replay identycznego staged planu jest idempotentny;
inny plan lub niespójne bytes są odrzucane fail-closed. Promocja nie zmienia
HKCU route, Bootstrap, Legacy, Project Memory ani writer/intake/production
acceptance — te granice mają osobne canonical operacje i preflighty.

## 6. Native Messaging route

Dedicated vNext manifest:

```text
%LOCALAPPDATA%\BartoszDevBridge-vNext\clients\native-host\com.bartosz.dev_bridge.vnext.json
```

Na Windows należy weryfikować odpowiednie HKCU registry views używane przez Chrome. Route ma wskazywać dedicated vNext manifest i pinned extension origin.

Legacy host `com.bartosz.dev_bridge` nie jest vNext hostem i nie powinien być fallbackiem dla vNext Project Execution.

## 7. Native config

`config\native-host.json` używa schema `bdb-vnext-native-host-config-v2` i wiąże:

- `runtime_root`;
- `legacy_runtime_root`;
- zewnętrzny `bootstrap_authority_root`;
- generation/protocol;
- vNext native host name;
- pinned browser extension ID.

Config musi używać absolute paths i zachować izolację vNext/Legacy/Bootstrap.

## 8. Produkcyjna aktywacja

BDB vNext rozdziela zbudowanie klienta od production acceptance.

Native Host opisuje trzy niezależne warunki admission:

1. zewnętrzny M11c ProgramData Bootstrap jest `ACTIVE`;
2. M9b Browser/Native client gate jest `ACTIVE`;
3. canonical M3c intake/admission jest enabled.

Dopiero zgodność authority może oznaczać produkcyjny acceptance. Sam poprawny handshake albo zarejestrowany Native route nie wystarcza.

## 9. Control Center authority summary

Technical Control Center potrafi read-only obserwować:

- Bootstrap ACTIVE/source identity;
- M9b state i writer/intake;
- M3c admission;
- Native route;
- wyliczony production acceptance;
- warnings.

Projection nie ma Legacy fallback i nie powinna automatycznie naprawiać runtime podczas odczytu.

## 10. Browser installation/update

Bieżący model Browsera to operator-loaded unpacked extension. Po aktualizacji produkcyjnych bytes Chrome musi korzystać z:

```text
%LOCALAPPDATA%\BartoszDevBridge-vNext\clients\browser-extension
```

Po reload należy sprawdzić extension ID i real Browser smoke, ponieważ automated source tests nie dowodzą zgodności aktualnego DOM ChatGPT.

## 11. Build i disk guard

Build/deployment może mieć lokalne safety guards, np. minimalną ilość wolnego miejsca. Guard nie powinien być obchodzony przez obniżenie progu lub przypadkowe usuwanie danych użytkownika.

Jeżeli build jest zablokowany, source commit może być poprawny i wypchnięty na GitHub, podczas gdy installed runtime pozostaje na wcześniejszej wersji. Te dwa statusy muszą być raportowane oddzielnie.

Dla bounded repairu `eae9fee9d171d61ded3c9cf539058559679aa9c8` source validation zakończyła się pomyślnie, ale wolne miejsce na C było poniżej przyjętego progu 20 GB. Production package **nie został zbudowany**, production/runtime **nie został zmodyfikowany**, a real ChatGPT Browser smoke **nie został uruchomiony**. Z tego repairu nie wolno wyciągać wniosku, że live runtime używa bytes z `eae9fee9`.

## 12. Co sprawdzać po deploymentcie

Minimalna checklista:

```text
source HEAD/tree
browser bundle digest
native executable digest
native manifest digest
native config digest
client plan digest
extension ID
HKCU native route
Native status/handshake
Bootstrap state
M9b writer/intake state
M3c admission state
production acceptance
```

Real ChatGPT Browser E2E jest osobnym testem po deploymentcie i nie powinien być deklarowany jako PASS wyłącznie na podstawie unit/integration tests.

## 13. Project runtime state vs client deployment

Aktualizacja Browser/Native/GUI nie może sama w sobie ręcznie przepisywać Project Memory. Project Memory i Project Execution są danymi runtime projektów, natomiast client package jest deploymentem aplikacji BDB.

Recovery projektu powinno korzystać z canonical workflow/coordinator primitives, a nie z ręcznej edycji JSON state files.

## 14. Missing-M9b recovery

Jeżeli Bootstrap ACTIVE, zweryfikowany klient i canonical M3c są spójne, a
`config/m9b-activation.json` jest dokładnie nieobecny, nie wolno odtwarzać M9b
ręcznym zapisem ani kopiowaniem starego pliku. Operacja
`bdb-vnext-m9b-recovery` tworzy immutable, source-bound plan oraz journal i
wymaga exact plan SHA, jawnej zgody operatora i `bootstrap.lock`.

Recovery jest roll-forward-only: `PREPARED → CLIENTS_VERIFIED → ACTIVATING →
ACTIVE → COMPLETED`. Każdy zapis ponownie rewaliduje Bootstrap ACTIVE/PREVIOUS,
client plan i wszystkie digesty Browser/Native/config, M3c, route bez Legacy oraz
zaufaną historyczną linię M9a. Obcy lub zmieniony M9b, inny plan, stale
verification albo niepełna historia kończą się fail-closed. Crash po zapisie
rekordu jest bezpiecznie wznawiany przez readback i journal; ukończony plan jest
idempotentny. Recovery nie zmienia Bootstrap, route, Legacy ani Project Memory.

# BDB vNext — Kontrakt Przejść Cyklu Życia Powiązań (NX-003 Binding Lifecycle Transition Contract)

## 1. Wstęp i Cel Zadania
Zadanie **NX-003** rozwiązuje realny defekt poprawności (*correctness defect*) w dotychczasowym cyklu życia powiązań wykonania zadań (*execution bindings*):
- Wcześniej po wystąpieniu błędu (`FAIL`) i ponowieniu próby (`retry`), stary binding mógł nadal pozostawać w stanie `ACTIVE`.
- Prowadziło to do wieloznaczności wyboru bieżącego powiązania (`current_binding_id`) oraz ryzyka, że spóźniony wynik `PASS` dla starego bindingu zostanie omyłkowo zaakceptowany przez system i zmieni stan zadania na `completed`.

W ramach NX-003 wdrożono ścisły, kanoniczny model cyklu życia z monotoniczną generacją (`generation`) oraz atomowym wygaszaniem (`SUPERSEDED`) poprzednich powiązań.

---

## 2. Kanoniczne Stany Powiązania (*Binding Statuses*)

Każdy obiekt `ProjectExecutionBinding` znajduje się w jednym z 4 stanów:

| Stan | Typ | Opis |
| :--- | :--- | :--- |
| **`ACTIVE`** | Aktywny | Powiązanie aktualnie oczekujące na realizację próby przez Agenta / Browser. Dla danego zadania w danej wersji planu może istnieć **co najwyżej jedno** powiązanie w stanie `ACTIVE`. |
| **`ACCEPTED`**| Terminalny | Wynik próby dla tego powiązania został zweryfikowany i zaakceptowany z sukcesem (`PASS`). Zadanie przechodzi do stanu `completed`. |
| **`FAILED`** | Terminalny | Próba zakończyła się niepowodzeniem (`FAIL` / `BLOCKED`). Powiązanie nie przyjmuje już kolejnych wyników; ponowienie wymaga nowej generacji bindingu. |
| **`SUPERSEDED`**| Terminalny | Powiązanie zostało unieważnione przez utworzenie nowej generacji bindingu (np. ponowienie próby / restart) lub podczas procedury reconciliacji. Flaga `superseded = True`. |

---

## 3. Dozwolone Przejścia Stanów (*Legal State Transitions*)

```
            +-------------------+
            |      ACTIVE       |
            +-------------------+
              /       |       \
             /        |        \
    (PASS)  /   (FAIL)|         \ (Retry / New Gen)
           v          v          v
     +----------+ +--------+ +------------+
     | ACCEPTED | | FAILED | | SUPERSEDED |
     +----------+ +--------+ +------------+
     (TERMINAL)   (TERMINAL)   (TERMINAL)
```

1. **`ACTIVE` $\rightarrow$ `ACCEPTED`**:
   - Następuje w momencie rejestracji wyniku z `overall == "PASS"`.
   - Zapisywany jest znacznik `finished_at = _utc_now()`.
2. **`ACTIVE` $\rightarrow$ `FAILED`**:
   - Następuje w momencie rejestracji wyniku z `overall == "FAIL"` lub `BLOCKED`.
   - Zapisywany jest znacznik `finished_at = _utc_now()`.
3. **`ACTIVE` $\rightarrow$ `SUPERSEDED`**:
   - Następuje atomowo w transakcji `persist_binding()` w momencie powołania nowego bindingu dla tego samego zadania.
   - Ustawiane są flagi `status = "SUPERSEDED"`, `superseded = True`, `finished_at = _utc_now()`.
4. **Przejścia Niedozwolone (*Illegal Transitions*)**:
   - Wszelkie próby mutacji stanów terminalnych (`ACCEPTED -> ACTIVE`, `FAILED -> ACCEPTED`, `SUPERSEDED -> ACTIVE`, `ACCEPTED -> FAILED`) są bezwzględnie odrzucane (*fail-closed*) błędem `illegal_binding_transition` (`BindingLifecycleError`).

---

## 4. Kanoniczne Niezmienniki (*Canonical Invariants*)

1. **I1 (Max One Active):**
   Dla dowolnej pary `(task_id, plan_version)` liczba powiązań ze statusem `ACTIVE` i `superseded == False` wynosi $\le 1$.
2. **I2 (Monotonic Generation):**
   Numer generacji (`generation: int >= 1`) jest ściśle monotonicznie rosnący przy każdym kolejnym powiązaniu dla danego zadania.
3. **I3 (Atomic Supersede on Retry):**
   Utworzenie nowej generacji bindingu atomowo wygasza (`SUPERSEDED`) wszelkie istniejące powiązania `ACTIVE` dla danego zadania w ramach jednej transakcji `memory.execution_transaction()`.
4. **I4 (Late Old Result Rejection):**
   Wynik nadesłany dla powiązania, które nie jest aktualnym `ACTIVE` powiązaniem lub nie odpowiada `current_binding_id`, jest odrzucany z kodem `STALE_RESULT` (`execution_binding_stale`) i nie modyfikuje stanu zadania.
5. **I5 (Direct & Native Parity):**
   Zarówno bezpośrednia ścieżka koordynatora (`ProjectExecutionCoordinator.record_result`), jak i ścieżka Browser Native (`ProjectWorkflow.submit_project_execution_result` / `m9b_native_host`) stosują identyczny kanoniczny guard.
6. **I6 (Concurrency Atomicity):**
   Współbieżne wywołania `start()` / `persist_binding()` nie mogą doprowadzić do powstania więcej niż jednego `ACTIVE` bindingu.

---

## 5. Procedura Rekoncyliacji (*Deterministic Reconciliation*)

Dla stanów historycznych lub uszkodzonych w wyniku awarii (`reconcile_execution_bindings` / `reconcile_project_bindings`):
- Wszystkie powiązania są grupowane według `(task_id, plan_version)` i sortowane chronologicznie.
- Numery generacji są normalizowane do ciągu `1, 2, 3...`.
- Jeżeli wykryto $> 1$ powiązanie `ACTIVE`:
  - Priorytet zachowania stanu `ACTIVE` ma powiązanie wskazane przez `current_binding_id`.
  - W przypadku braku dopasowania wybierane jest najpóźniej utworzone powiązanie.
  - Wszystkie pozostałe powiązania `ACTIVE` zostają oznaczone jako `SUPERSEDED` (`superseded = True`).
- Operacja jest w 100% deterministyczna i idempotentna.

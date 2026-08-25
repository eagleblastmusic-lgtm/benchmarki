# BDB vNext — Kontrakt Identyczności Wyniku Wykonania v2 (NX-004 Result Identity v2 Contract)

## 1. Wstęp i Zdiagnozowany Defekt Poprawności
W dotychczasowym modelu identyczności wyniku wykonania (**Result Identity v1**):
- Pole `failure_code` **nie wchodziło** w skład słownika tożsamości wyniku (`_result_identity`).
- W konsekwencji dwa różne wyniki niepowodzenia (np. `FAIL` z `failure_code="COMPILATION_ERROR"` oraz `FAIL` z `failure_code="TEST_TIMEOUT"`) generowały **identyczny skrót kryptograficzny** (`result_digest`).
- Uniemożliwiało to jednoznaczną dyskryminację przyczyn błędów, poprawne rejestrowanie prób oraz precyzyjne rozpoznawanie powtórek (*exact replay*).

W ramach zadania **NX-004** wdrożono **Result Identity v2** z jawnym wersjonowaniem tożsamości oraz zasadą **Dual Read + v2 Write**.

---

## 2. Analiza Pól Semantycznych Result Identity v2

### Pola Włączone do Tożsamości (`INCLUDED_IN_V2_IDENTITY`):
1. **`identity_version`**: `"v2"` — jawny identyfikator wersji kontraktu tożsamości.
2. **`execution_binding_id`**: Identyfikator powiązania wykonania.
3. **`command_id`**: Identyfikator dyspozycji wykonawczej.
4. **`correlation_id`**: Identyfikator korelacji sesji.
5. **`project_id`**: Identyfikator projektu powiązanego.
6. **`task_id`**: Identyfikator zadania objętego powiązaniem.
7. **`plan_version`**: Wersja planu projektu.
8. **`repo_alias`**: Alias repozytorium kodu.
9. **`result_project_id`**: Identyfikator projektu zadeklarowany w payloadzie wyniku.
10. **`result_task_id`**: Identyfikator zadania zadeklarowany w payloadzie wyniku.
11. **`result_plan_version`**: Wersja planu zadeklarowana w payloadzie wyniku.
12. **`head_before`**: Commit OID (SHA) przed wykonaniem.
13. **`head_after`**: Commit OID (SHA) po wykonaniu.
14. **`execution_status`**: Dyspozycja wykonania (`PASS` / `FAIL`).
15. **`validation_status`**: Dyspozycja walidacji (`PASS` / `FAIL`).
16. **`promotion_status`**: Dyspozycja awansu/promocji (`NOT_RUN` / `RUN`).
17. **`failure_code`**: Kod przyczyny błędu (np. `COMPILATION_ERROR`, `TEST_TIMEOUT`, `LINT_FAILURE`) — **krytyczne pole różnicujące**.
18. **`summary`**: Tekst podsumowania wyniku (`result_summary`).
19. **`evidence_refs`**: Posortowana leksykograficznie lista identyfikatorów dowodów wykonania.
20. **`criteria`**: Znormalizowana lista ocen kryteriów akceptacji.
21. **`canonical_refs`**: Słownik referencji kanonicznych (`candidate_id`, `view_id`, `evidence_id`, `publication_id`).

### Pola Wyłączone z Tożsamości (`EXCLUDED_FROM_V2_IDENTITY`):
1. **`attempt_id`**: Losowy identyfikator koperty próby (wyłączony, aby umożliwić deterministyczne rozpoznawanie *exact replay* dla tego samego semantycznego payloadu).
2. **`started_at` / `finished_at` / `created_at`**: Znaczniki czasu wykonania (zmienne w czasie, nie stanowiące o tożsamości samego wyniku).
3. **`conversation_id`**: Identyfikator wątku komunikacyjnego Browser/GUI (metadana transportowa).
4. **`request_id` / `client_id`**: Ulotne identyfikatory sesji IPC Native Messaging.

---

## 3. Deterministyczna Serializacja Kanoniczna i Algorytm Skrótu

1. **Format:** Kanoniczny JSON zgodny ze standardem `bdb_shared.evidence.canonical_json_bytes`:
   - Posortowane klucze obiektów (`sort_keys=True`).
   - Brak spacji wokół separatorów (`separators=(',', ':')`).
   - Kodowanie UTF-8 bez ucieczki znaków Unicode (`ensure_ascii=False`).
   - Zakończenie bajtem nowej linii `\n`.
2. **Algorytm:** SHA-256 z prefiksem `sha256:`:
   $$\text{digest} = \text{"sha256:"} + \text{Hex}(\text{SHA256}(\text{canonical\_json\_bytes}(\text{payload})))$$

---

## 4. Strategia Zgodności: Dual Read + v2 Write

1. **Zapis Nowych Rekordów (*New Writes*):**
   - Wszystkie nowe próby rejestrowane przez `ProjectExecutionCoordinator.record_result` są zapisywane jako `identity_version = "v2"` i używają skrótu obliczonego funkcją `execution_result_digest_v2`.
2. **Odczyt Danych Historycznych (*Dual Read*):**
   - Istniejące rekordy w pamięci projektu (zapisane w wersji `v1`) zachowują swój historyczny skrót i nie są automatycznie przepisywane ani modyfikowane.
3. **Wersjonowane Rozpoznawanie Powtórek (*Version-Aware Replay*):**
   - Przy nadesłaniu wyniku `record_result` i `existing_result` obliczają `digest_v2` oraz `digest_v1`.
   - Dopasowanie następuje, jeżeli w pamięci istnieje rekord o `result_digest == digest_v2` LUB (`result_digest == digest_v1` dla rekordu historycznego `v1`).

# BDB vNext — Nota Kompatybilności NX-002 (Project Plan v1 Schema & Loader Parity)

## 1. Cel i Zakres
Zadanie **NX-002** miało na celu usunięcie wszystkich rozbieżności (*parity drift*) pomiędzy specyfikacją kontraktu w schemacie JSON (`schemas/bdb-project-plan-v1.schema.json`) a zachowaniem runtime loadera (`validate_project_plan()` w `bdb_vnext/project_catalog.py`).

W ramach zadania zdefiniowano centralną warstwę kontraktu (`bdb_vnext/project_plan_contract.py`), która eliminuje duplikację magicznych wartości i zapewnia deterministyczną zgodność semantyczną.

---

## 2. Zidentyfikowane i Usunięte Rozbieżności (Drifts)

W wyniku audytu zidentyfikowano i wyeliminowano następujące klasy rozbieżności:

1. **Nieznane pola w obiektach `milestone` (*Unknown Milestone Keys*):**
   - **Stan przed NX-002:** JSON Schema odrzucał nieznane właściwości (`"additionalProperties": false`), podczas gdy runtime loader nie sprawdzał kluczy w obiektach kamieni milowych i je ignorował.
   - **Stan po NX-002 (Canonical):** Runtime loader oraz JSON Schema bezwzględnie odrzucają wszelkie nieznane pola w obiektach `milestone` (`_reject_unknown_keys(raw, set(MILESTONE_ALLOWED_KEYS), "milestone")`).

2. **Granica długości kryteriów akceptacji (`task.acceptance_criteria`):**
   - **Stan przed NX-002:** JSON Schema dopuszczał do 8 000 znaków na kryterium akceptacji (definicja `#/$defs/textList64`), natomiast runtime loader używał domyślnego ograniczenia pomocniczego `_list_of_text` równego 1 000 znaków. Prawidłowy plan zawierający kryterium o długości np. 1 500 znaków przechodził walidację schematem, ale był odrzucany przez loader.
   - **Stan po NX-002 (Canonical):** Obie strony egzekwują identyczny, kanoniczny limit 8 000 znaków na element (`MAX_TEXT_LIST_64_STRING_LENGTH = 8_000`, `maxItems = 64`).

3. **Granica długości pola `severity` w `risk`:**
   - **Stan przed NX-002:** JSON Schema ograniczał `risk.severity` do 32 znaków (`maxLength: 32`), podczas gdy runtime loader w funkcji `_record_list` dopuszczał do 4 000 znaków.
   - **Stan po NX-002 (Canonical):** Runtime loader i JSON Schema egzekwują limit `MAX_RISK_SEVERITY_LENGTH = 32`.

4. **Niejednolita definicja dopuszczalnych kluczy na wszystkich poziomach zagnieżdżenia:**
   - **Stan po NX-002:** Wszystkie reguły `additionalProperties: false` (top-level, milestone, task, planning_context, requirements, scope, decisions, open_questions, specifications, architecture, components, interfaces, test_strategy, risks, gates, acceptance_scenarios) są rygorystycznie zsynchronizowane.

---

## 3. Kanoniczna Semantyka Kontraktu `bdb-project-plan-v1`

Wszystkie parametry walidacji zostały spięte w jednym module `bdb_vnext/project_plan_contract.py`:

| Element | Właściwość / Pole | Kanoniczny Limit / Typ |
| :--- | :--- | :--- |
| **ID** | `id`, `project_id`, `milestone_id`, `task_id` | Pattern `^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$`, max 96 znaków |
| **Top-level** | `project_name` | 1–200 znaków |
| **Top-level** | `plan_version`, `supersedes_version` | Dodatnia liczba całkowita (jako `int` lub ciąg cyfr) |
| **Top-level** | `created_at` | Format ISO 8601 UTC zakończony `Z`, max 64 znaki |
| **Top-level** | `revision_reason` | 1–1 000 znaków |
| **Top-level** | `revision_summary` | 1–4 000 znaków |
| **Kolekcje** | `milestones` | max 512 elementów |
| **Kolekcje** | `tasks` | max 2 048 elementów |
| **Milestone** | `title` / `description` | `title`: 1–300 znaków, `description`: 1–4 000 znaków |
| **Milestone / Task**| `status` | Enum: `pending`, `active`, `review`, `completed`, `blocked`, `skipped` |
| **Task** | `title` / `description` | `title`: 1–300 znaków, `description`: 1–4 000 znaków |
| **Task Text List** | `acceptance_criteria`, `deliverables`, `verification`, `tests` | max 64 elementy, 1–8 000 znaków na element |
| **Task ID List** | `dependencies`, `decision_ids`, `specification_ids`, `risk_ids` | max 64 unikalne identyfikatory |
| **Context Record** | `decisions`, `open_questions`, `specifications`, `risks`, `gates`, `acceptance_scenarios` | max 128 elementów |
| **Risk** | `severity` | 1–32 znaki |
| **Decision** | `classification` | 9 wartości enum (`architectural_decision`, `product_decision` itd.) |
| **Specification**| `category` | 12 wartości enum (`domain`, `data`, `ui`, `ux` itd.) |

---

## 4. Wpływ na Kompatybilność Istniejących Planów v1

- **Istniejące poprawne plany v1:**
  Wszystkie dotychczasowe, poprawne plany projektów (w tym produkcyjny plan w `runtime/control/project-memory/...` oraz plany testowe) **pozostają w 100% kompatybilne**.
- **Plany z historycznymi driftami:**
  Plany zawierające nieznane właściwości w obiektach `milestone` lub przekraczające limit 32 znaków w `risk.severity` zostaną odrzucone zgodnie z zasadą *fail-closed*.
- **Wydłużone kryteria akceptacji:**
  Plany zawierające obszerne kryteria akceptacji (pomiędzy 1 000 a 8 000 znaków), które wcześniej były odrzucane przez loader, są teraz poprawnie i zgodnie importowane.

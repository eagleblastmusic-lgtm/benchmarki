# CC1 — vNext Control Center closure

Status: **PASS / CLOSED**

Basis:

- canonical branch basis: `bdb-vnext`
- basis commit: `634997470692795189a252ff995ee3a6a5f494d8`
- previous milestone: M8b PASS/CLOSED
- roadmap gate: B29 / CC1
- validated feature head: `14cd3b916a7658547e568f4468cdc1aff64079db`
- validation date: `2026-08-17`

## Intent

Adapt the existing PySide6 Control Center into a thin vNext projection client
without importing legacy runtime semantics into the new generation.

CC1 does **not** activate vNext, publish a Control Center provider, resume work,
execute an effect, move a production Git ref, or deploy anything to Shopify.

## Authority boundary

The default `bdb_gui` application path no longer opens or calls
`bdb_operator.OperatorApi`. The vNext window consumes
`bdb_vnext.control_center_query`, which:

1. treats an absent vNext runtime as explicit `OFF`;
2. validates an existing Control DB through the external-seal/read-only
   preflight contract;
3. reopens the verified database with SQLite `mode=ro` and `query_only=ON`;
4. consumes Work lifecycle through the canonical M4a `WorkItemQuery` DTO;
5. projects Candidate effect, Evidence, RepoView identity and Publication facts
   as bounded canonical record sets;
6. exposes only disabled mutation predicates with typed reason
   `cc1_read_only`;
7. never invents a singular current Candidate when authority does not select
   one;
8. never automatically falls back to legacy data.

Legacy Control Center composition remains available only through the explicit
`--legacy-control-center` compatibility switch. It is not the default path and
is never selected as recovery/fallback from a failed vNext read.

The GUI itself has no SQLite dependency and does not reconstruct lifecycle
state.

## UI semantics

The vNext shell presents a status vector rather than a synthetic aggregate
health state:

- System
- Writer
- Activation
- Control Store

Per WorkItem it renders the canonical M4a query together with bounded related
Candidate/effect, Evidence/evaluation, repository-view identity and Publication
records. Missing dimensions remain missing; CC1 does not infer them.

The action row is intentionally disabled. Later activation/cutover milestones
must bind actions to their own canonical predicates; CC1 pre-authorizes none.

## Validation evidence

### Focused source/behavior gate

An earlier exact feature head passed the focused CC1 suite covering:

- canonical read-only projection behavior;
- M4a read-query DTO preservation;
- PySide6 OFF-state and DEGRADED-state smoke behavior;
- no legacy fallback;
- no GUI SQLite dependency;
- no mutation vocabulary in the CC1 query boundary.

Result: **15 / 15 PASS**.

### Full-repository diagnostic run

A full Windows repository pytest run exposed ten failures. Four were directly
attributable to the CC1 branch:

- three preserved Control Center composition/entrypoint architecture contracts;
- one M4a source-boundary contract forbidding alternate modules from owning
  `m4a_work_items` table knowledge.

All four were repaired before the final gate.

The remaining six failures were environment/tooling failures outside the CC1
change: subprocess profile tests selected `C:\Python314\python.exe`, where
`pytest` was not installed. They were not reclassified as CC1 PASS or silently
ignored as product behavior.

### Final impacted regression

On exact head `14cd3b916a7658547e568f4468cdc1aff64079db`, the impacted regression set
completed with exit code `0`, covering:

- `tests/test_cc1_control_center.py`
- `tests/test_m4a_read_query.py`
- `tests/test_gui_control_center_smoke.py`
- `tests/test_gui_project_prepare_architecture.py`
- `tests/test_gui_session_history_architecture.py`
- `tests/test_gui_tray_architecture.py`
- `tests/test_control_center_architecture_contract.py`
- `tests/test_vnext_m4a_work_kernel.py`

### Final Windows PySide6 smoke

Exact head: `14cd3b916a7658547e568f4468cdc1aff64079db`.

Observed on Windows with Python `3.14.4`, PySide6 `6.11.1`, Qt `6.11.1`:

- `status=success`
- `read_only_startup=true`
- `bootstrap_completed=true`
- `bootstrap_ok=true`
- `legacy_control_center=false`
- `legacy_fallback=false`
- `actions_enabled=false`
- `mutation_operations_invoked=0`
- `auto_resume_invoked=false`
- `operator_network_listener=null`
- absent vNext runtime remained absent
- status vector: System `OFF`, Writer `OFF`, Activation `OFF`, Control Store
  `ABSENT`
- headless event loop exited `0`

The validated feature worktree was clean and exactly synchronized with its
remote branch after the gate.

## CI qualification

No GitHub Actions run exists for this PR head because the repository workflow
configuration does not trigger the normal PR workflow for PRs targeting
`bdb-vnext`. Absence of CI was **not** treated as a green result. Closure is
based on the explicit local Windows evidence above plus the impacted
architecture regression gate.

## Closure decision

CC1 is **PASS / CLOSED** for its defined build-only/read-only scope.

This closure does not authorize vNext activation, legacy shutdown, production
mutation, Git publication beyond the normal source merge, or Shopify deploy.
The next migration decision remains dependent on fresh R0b local/runtime
evidence.

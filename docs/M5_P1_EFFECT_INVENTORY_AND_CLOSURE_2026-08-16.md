# BDB vNext — M5 / P1 Effect Inventory and Closure Boundary

Date: 2026-08-16

Status: `SOURCE-BACKED BUILD-ONLY CLOSURE RECORD / NO ACTIVATION`

This record reconciles the frozen M5a/M5b gates with the current build-only vNext implementation. It does not change Architecture Freeze, Plan v1.1.1/v1.1.2, or final-cutover rules. Production runtime / writer / activation remain `OFF / OFF / OFF`.

## 1. Current P1 interpretation

The practical P1 scope is the currently enabled reversible engineering path. No generic distributed effect framework, DBOS, Temporal, Kestra, remote deployment adapter, Git ref promoter, or LIVE mutation is introduced here.

The frozen M5 invariants still apply: exact effect identity, observe-before-retry, no blind retry from uncertain state, and target-local retry/watch authority must not own lifecycle/effect truth.

## 2. Enabled effect inventory

| Surface | Current classification | M5 treatment |
|---|---|---|
| Candidate/worktree exact replacement | `ENABLED EXTERNAL MUTATING EFFECT` | Covered by M4b durable exact effect intent plus M5a `KernelEffectReconciler`. This is the one current P1 physical mutation boundary. |
| Validation subprocess/checker | `BOUNDED OBSERVER / NOT PROMOTION AUTHORITY` | Exact allowlisted argv/cwd, bounded timeout/output, process-tree termination on timeout, outputs captured as Validation/Evidence. It does not authorize Candidate state or promotion. Any filesystem drift it causes is outside the prepared Candidate truth and must fail exact Candidate/seal checks. Re-running validation is a new observation, not a retry of a durable external mutation effect. |
| Publication / consumer cursor | `INTERNAL TRANSACTIONAL CONTROL-DB EFFECT` | Publication identity is deterministic, request replay is idempotent, and lost response after commit is resolved from canonical Control DB truth. There is no external POSSIBLE window requiring a second M5 effect record. |
| Browser presentation | `EXTERNAL OBSERVATION, NOT LIFECYCLE WRITER` | N4 already models presentation separately as `UNKNOWN` / `PRESENTED`, requires exact captured assistant Evidence, and replays the same witness idempotently. Browser does not own Task/Work/Candidate/Publication transitions. |
| Git ref / checkout synchronization | `NOT ENABLED IN P1` | Deferred to M7 / P2. It must become a separately witnessed effect before enablement. |
| LIVE / deployment / Shopify publication | `DISABLED` | Out of scope. No direct LIVE mutation is authorized. |

## 3. M5a status

The Candidate/worktree effect slice is implemented by `bdb_vnext/m5a_effects.py` over the existing durable M4b Candidate effect record. It adds no second effect store or second durable effect id.

The focused local validation at commit `56bd01a0e1096fb8303d11c722eb3e61b09ec4d6` passed:

- `tests/test_vnext_m5a_effects.py`: `5 passed`;
- `tests/test_vnext_m4b_candidate.py`: `34 passed, 1 skipped`;
- `python -m py_compile bdb_vnext/m5a_effects.py`: PASS;
- `git diff --check`: PASS;
- working tree: clean.

M5a is considered complete for the currently enabled P1 mutation surface only after M5b removes alternate target-local decision ownership. This record deliberately does not claim future Git/process/deploy effect coverage.

## 4. M5b exact cutover target

M5b is narrow. It must not create another scheduler or effect framework.

Required changes:

1. Candidate/worktree apply/reconcile decisions used by the active P1 engineering path must route through `KernelEffectReconciler`.
2. The generic N6 build-only Candidate rehearsal path must use the same reconciler rather than locally deciding `observe -> mark_possible -> apply -> observe`.
3. `EditorPort` may keep durable edit-batch bookkeeping, but it must not be an independent effect retry authority.
4. N4 Publication and Browser presentation keep their existing transactional/idempotent and witness contracts; no duplicate M5 store is added for them.
5. Validation subprocess execution remains a bounded observation surface; it does not receive lifecycle/effect transition authority.
6. No DBOS/Temporal/Kestra or generic effect IR is introduced.
7. No Git ref, checkout sync, LIVE or deployment effect is enabled by M5b.

## 5. M5b DONE evidence

M5b can be marked DONE only when all of the following are true on one exact commit:

- M5a focused tests remain green;
- M4b Candidate regressions remain green;
- engineering-loop focused tests remain green;
- N6 focused recovery/fault tests remain green;
- N4 Publication/presentation fault tests remain green;
- source contract confirms no active P1 Candidate path owns a parallel retry/reconcile decision outside the M5a reconciler;
- duplicate/lost-response/restart paths do not produce a second Candidate physical effect;
- production runtime / writer / activation remain `OFF / OFF / OFF`.

## 6. What comes next

After M5b closure, P1 is DONE. The next phase is P2: promotion-grade evidence/policy and Git CAS/promotion (`M6*` + `M7*`). Git ref and checkout effects are introduced there only after their own exact witnesses exist.

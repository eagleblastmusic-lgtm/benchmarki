# BDB vNext — Post-N6 Full-System Technical Audit — UPDATED AFTER FINDINGS REPAIR

## A. Audit basis

The repair pass started from `b57639bc93ef13f1585badb331df250aaa312cfc` on `bdb-vnext`, with `origin/bdb-vnext` at `89390dc2e053517a97427ab0ae76cefc9c227682`, clean worktree, and production runtime/writer/activation off. The report describes the final local repair state, not the historical pre-repair state. No push, successor EU, legacy mutation, or production activation occurred.

## B. Executive technical verdict

`TECHNICALLY_COHERENT_WITH_FINDINGS` pending the required fresh human Browser re-gate. The repaired code closes the concrete P0/P1 authority, lifecycle, package-identity, recovery, status, schema, and presentation defects that were deterministically repairable inside N1–N6. Automated Browser evidence is not claimed in this report until it is actually run; prior human evidence is stale for the changed package and remains pending.

## C. As-built system map

The vNext path remains layered: exact COMMITTED RepoView and CAS feed M2 context/EI; M3 Submission→Task is the sole admission authority; M3b owns durable Browser outbox recovery; M4 WorkKernel owns WorkItem/Run/Wait/lease/fence; N2 seals an isolated Candidate; N3 owns immutable Evidence/Evaluation and current Disposition; N4 owns Publication/consumer cursor/presentation/Resume; N6 is a build-only normal-Browser rehearsal adapter. The unified server Control DB remains canonical, with Browser outbox, Content CAS, and bootstrap/package resources physically separate.

## D. Canonical authority matrix

| Subject | Canonical writer/authority | Non-authoritative clients |
|---|---|---|
| Task admission | M3a/M3c canonical admission transaction | Browser, Native, N6 panel |
| Browser admission recovery | M3b durable outbox + canonical lookup | Chrome local cache |
| WorkItem lifecycle | one `WorkKernelStore` writer | Browser, Native, query |
| Candidate | one Candidate store/seal boundary | Browser, logs |
| Evidence/Evaluation/Disposition | N3 immutable rows and disposition head | Browser/operator projections |
| Publication/consumer/presentation | N4 `PublicationStore` | Browser DOM is physical witness only |
| repository source | exact COMMITTED/CANDIDATE RepoView | moving HEAD/worktree, UI |
| N6 package execution | package-bound native code digest and package manifest | live checkout |

## E. End-to-end lifecycle trace

The N6 vertical now exercises `Submission → Task → WorkItem READY → lease/fence → Run RUNNING → exact Candidate → Evidence/Evaluation → Publication → Run SUCCEEDED/CERTAIN → WorkItem FINISHED`. Replays recover canonical admission/publication rather than creating duplicate identities. A lost ACK is looked up from the durable outbox/Control DB before any resend.

## F. Architectural invariant assessment

Task, WorkItem, effect certainty, evidence applicability, publication, and presentation remain orthogonal. Facts are not replay-complete event sourcing. Browser/Native/query layers do not mutate lifecycle or evidence authority. N6 remains build-only and does not imply operational cutover.

## G. Findings register

| Finding | Original status | Repair action | Validation | Final status |
|---|---|---|---|---|
| AUD-001 | HIGH; stale Candidate PASS crossed Publication | Publication and Evidence require positive current Candidate applicability; exact base/workspace revalidation; no binding on stale evidence | tamper/publication regression in N4 tests | RESOLVED |
| AUD-002 | HIGH; N6 left READY Work with zero Runs | N6 uses canonical lease, Run start/adoption, finish, and release; lookup exposes Work query | N6 vertical recovery test and M4 regression | RESOLVED |
| AUD-003 | HIGH; N6 bypassed M3b outbox | N6 submits through `BrowserAdmissionClient`; SENT state recovers by canonical lookup | lost-admission ACK test; M3b regression | RESOLVED |
| AUD-004 | HIGH; marker/button could assert PRESENTED | DOM witness requires exact publication/result marker, conversation and digest; missing/wrong DOM fails closed | N4 positive/negative witness tests and JS checks | RESOLVED |
| AUD-005 | HIGH; Native shim imported live checkout | package archives exact code, records native-code digest, and executes package root with no user site | package identity and live-checkout isolation tests | RESOLVED |
| AUD-006 | HIGH; partial vertical recovery incomplete | deterministic checkpoints plus durable replay/reconciliation for admission, Candidate, evaluation, publication, and Work finish | N6 lost admission/publication response test; focused recovery suite | RESOLVED |
| AUD-007 | MEDIUM; pre-lock sequence/head races | publication sequence allocated under writer lock; evaluation head re-read/CAS under `BEGIN IMMEDIATE`; consumer conflict maps to typed replay/conflict | N4/M4C focused suite | RESOLVED |
| AUD-008 | MEDIUM; UNKNOWN response contradicted retained PRESENTED | N6 unknown response returns canonical retained binding status | N4/N6 regression | RESOLVED |
| AUD-009 | MEDIUM; user_version 0/weak schema identity | Control DB uses explicit user_version 2 and structural required-table/column digest, failing closed on mismatch | N1 schema-version/layout regression | RESOLVED |
| AUD-010 | UNKNOWN; historical RQ2 raw evidence absent | searched repo benchmark/artifact locations; no raw observations were recovered; no fabrication or rerun | evidence inventory remains absent | BLOCKED_BY_MISSING_EVIDENCE |
| AUD-011 | MEDIUM; weak Browser attestation | package records exact extension/native/package identity; model/reasoning/timing remain explicit operator/Browser observations where product does not expose stronger proof | package/config and JS completion checks | PARTIALLY_RESOLVED |
| AUD-012 | MEDIUM; pending Resume was browser-local pre-binding | two-phase blank-chat pending → stable canonical target binding preserved; no blank-chat Task ownership; stale/consumed states fail closed | N6 Resume focused tests and manual prior gate (stale for changed package) | PARTIALLY_RESOLVED |
| AUD-013 | MEDIUM; certain Candidate with idle Work | N6 terminal path requires Run SUCCEEDED/CERTAIN and FINISHED Work before canonical replay | N6 lifecycle regression | RESOLVED |
| AUD-014 | MEDIUM; governance status stale | living governance README updated; frozen historical records unchanged | documentation diff/status review | RESOLVED |
| AUD-015 | MEDIUM; retained worktree/temp accumulation | non-destructive Candidate retention inventory classifies referenced, recovery-required, historical, disposable; no evidence deletion | Candidate inventory test | PARTIALLY_RESOLVED |
| AUD-016 | MEDIUM; complexity/fragmentation | no cosmetic rewrite; only duplicated authority/failure paths touched where needed; physical topology retained | focused regression and negative scans | PARTIALLY_RESOLVED |
| AUD-017 | MEDIUM; OFF/OFF/OFF hid rehearsal activation | status/package/docs distinguish production OFF from explicit package-loaded rehearsal infrastructure and registered test host | package manifest/status assertions | RESOLVED |

No additional AUD-018+ correctness finding was established during this repair pass.

## H. Identity / idempotency / recovery

Exact request digests, deterministic N6 IDs, M3b outbox lookup, Publication idempotency, evaluation identity, and current-head supersession are retained. Lost response after durable commit is a lookup/replay condition, not permission to duplicate evidence or publication. Candidate observation precedes retry after POSSIBLE/UNKNOWN/DIVERGED.

## I. RepoView / Context / EI

The M2 authority remains exact and read-only. Candidate applicability now reopens the persisted exact committed object through the retained Git-native Candidate worktree and compares repository identity, commit, tree, and view IDs before consumers can rely on current PASS.

## J. Candidate / Effect / Evidence

Candidate state and effect certainty remain separate. Sealing requires exact tree equality. N3 evidence stores immutable raw observations and evaluations; a stale Candidate degrades current applicability/effective disposition to INCONCLUSIVE while historical rows remain unchanged. Publication rejects non-applicable evidence.

## K. Browser / Native / Publication / Resume

Browser is a transport/cache/presentation witness. The generated panel does not construct success from a marker: it requires a stable assistant DOM observation and exact publication DOM node before sending a witness. N6 package Native execution is bound to archived package code. Resume remains source/target-conversation scoped and cannot bind a blank chat before canonicalization.

## L. Control plane / persistence

One server Control DB is used for M3/M4/N2/N3/N4 semantic state. Browser outbox, CAS, bootstrap, and package roots remain separate. Schema layout/version verification is fail-closed; no destructive migrations were added.

## M. Test and evidence maturity

Focused N6, N4 Publication, M4 WorkKernel, M3b Browser admission, N1 Control DB, and M4b Candidate tests pass after repair; one pre-existing environment-dependent skip remains where applicable. Python source compilation via `compile(..., dont_inherit=True)`, generated JavaScript `node --check`, schema parsing, and `git diff --check` are required validation layers. The full repository suite was not used as a proxy for missing historical Browser evidence.

## N. Complexity / maintainability

The largest remaining cost is deliberate milestone boundary and retained Candidate/rehearsal artifact inventory. It is not safe to merge stores or authorities during this pass. Future cleanup should use the inventory and an explicit retention decision, not blind deletion.

## O. Legacy / migration / cutover readiness

Legacy remains operational, isolated, and untouched. No semantic legacy import, dual writer, ref movement, promotion, or production routing occurred. vNext activation remains explicit-package-only rehearsal.

## P. Safety / containment

Package/runtime/legacy roots are overlap-checked. Candidate workspaces use Git-native isolated worktrees and exact path/tree comparison. Native package code is archived from an exact source commit and has an independent digest. Production activation remains false.

## Q. External-orchestration seam

No external orchestration product or new daemon was introduced or evaluated. The current vertical is a typed synchronous/test harness over existing authorities.

## R. Remaining UNKNOWNs

Historical RQ2 raw Browser evidence remains unavailable. Model/reasoning/profile/timing claims that the normal ChatGPT product does not mechanically expose remain operator/Browser attestations. Fresh human N6 acceptance for this repaired package is still required.

## S. Claims current evidence does not justify

The repair does not justify production activation, human Browser PASS for the new package, a performance claim from M2D timing, historical RQ2 reconstruction, or a successor roadmap decision.

## T. Decision inputs for ChatGPT Work

Use the repaired authority map and the remaining evidence gaps above. The implementation direction is technically coherent for build-only vNext, while historical evidence and fresh human Browser acceptance remain explicit inputs.

## U. Suggested additional evidence

Run the minimal fresh N6 manual gate against the exact new package/HEAD after any extension/Native registration reload required by the changed package. Record model/reasoning, exact package identity, RUN-05 capture, positive/UNKNOWN witness, refresh recovery, and new-chat Resume.

## V. Final audit verdict

`TECHNICALLY_COHERENT_WITH_FINDINGS` — concrete repairable defects are addressed and focused regressions are green; final human Browser evidence is pending for the changed package, and AUD-010 remains blocked by missing historical raw evidence.

## W. Evidence appendix

Changed implementation paths: `bdb_vnext/candidate.py`, `control_store.py`, `m3b_browser_admission.py`, `m4a_work_kernel.py`, `m4c_evidence.py`, `n4_browser.py`, `n4_publication.py`, `n6_rehearsal.py`, `provider_root.py`. Changed tests cover Candidate retention, Control DB version/layout, Publication applicability/presentation/concurrency, and N6 package/lifecycle/recovery. Living status is in `docs/governance/README.md`. No push was performed.

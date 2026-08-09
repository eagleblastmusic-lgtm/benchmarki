# BDB — Migration & Execution Plan v1.1.1

## Canonical Parallel vNext Execution Override

**Status:** `CANONICAL EXECUTION DELTA — FROZEN`  
**Date:** `2026-08-09`  
**Architecture Freeze v1:** `UNCHANGED`  
**Canonical strategy:** `PARALLEL_ISOLATED_VNEXT_BUILD`  
**Current target branch:** `bdb-vnext`  
**Accepted vNext basis:** `de340e2564b233b8b395bdcc1dc96e8f733f44a7`  
**Accepted legacy main basis:** `4998aa16ff68d728637d09639ac79ced886393f6`  
**vNext runtime / writer / activation:** `OFF / OFF / OFF`

This document is a delta/addendum. It does not rewrite or historically edit Architecture Freeze v1, Migration Plan Freeze v1, or the Etap 6 Playbook. Where their incremental/in-place assumptions conflict with this document, this document is the canonical execution override.

> **Canonical override rule:** no AI may use the old `R0b → M1a → same-runtime incremental migration` chain to block target-only vNext construction, share mutable state between generations, import the legacy Journal semantically, or disable a legacy writer during a build-only vNext EU.

---

# 1. Architecture Reopen: NO

Parallel isolated vNext changes migration mechanics and activation order, not the frozen target architecture. It preserves and strengthens the frozen requirements: one lifecycle writer per semantic generation, no dual-authority, Browser-first/no-required-API, exact effect witnesses, Control Center non-authority, external Bootstrap authority, and state-forward recovery.

The Etap 5 decision to reuse the same physical SQLite/legacy runtime was an execution/substrate choice, not an Architecture Freeze invariant. Replacing it with a dedicated vNext Control Store and runtime generation is therefore `PLAN FREEZE DISCREPANCY` resolution, not `ARCHITECTURE REOPEN REQUIRED`.

No supplied evidence falsifies a frozen Architecture Freeze v1 invariant.

---

# 2. Canonical migration strategy

## 2.1. Strategy

The only canonical strategy is:

```text
keep legacy operational and frozen
→ build vNext in an isolated branch/worktree/runtime generation
→ validate vNext without production activation
→ close all internal vNext alternate authorities
→ perform one final legacy freeze/drain + generation switch
→ retain verified legacy state as READ_ONLY_ARCHIVE
→ finish self-host and compatibility-zero gates
```

There is no semantic dual-write, no in-place strangler over shared mutable state, and no full import of historical legacy lifecycle data.

## 2.2. Formal generation status

| Generation | Formal status | Source/runtime authority | Mutability and development rule | End state |
|---|---|---|---|---|
| Legacy | `FROZEN_OPERATIONAL` | `main` at accepted basis `4998aa16ff68d728637d09639ac79ced886393f6`; its deployed runtime/stores remain authority only for the legacy instance | May continue operating as a tool. No feature development. Only bounded critical maintenance when required to keep it usable or make final cutover observable/safe. | `READ_ONLY_ARCHIVE`; never imported as vNext lifecycle truth. |
| vNext | `BUILD_ONLY` | `bdb-vnext` at accepted basis `de340e2564b233b8b395bdcc1dc96e8f733f44a7`; no active runtime authority yet | Separate generation, stores, paths, identities and writers. Writer and external activation remain OFF until `M9b`. Test writers may operate only on isolated fixtures/resources. | Canonical active generation after final cutover and subsequent release gates. |

The accepted hashes are handoff evidence, not a substitute for fresh inspection. The next AI must verify branch, HEAD, upstream and worktree state before mutation.

## 2.3. Mandatory isolation contract

| Boundary | Canonical vNext rule |
|---|---|
| Runtime root | Dedicated `BartoszDevBridge-vNext`; not yet claimed to exist merely because M1a is DONE. |
| Control state | Dedicated vNext Control Store/DB; no shared legacy Journal and no semantic import. |
| Transport state | Dedicated spool/results/receipts beneath the vNext runtime root if the vNext design requires them; never legacy paths. |
| Process ownership | Dedicated locks/PIDs and generation-qualified writer identity. |
| Configuration | `bdb-vnext-config-v1`, under the vNext runtime root. |
| Browser | Dedicated vNext package identity; M1a evidence records extension ID `mopnolkjddkmgojfjkenjobehhmmklll`; no private packaging key in the repository. |
| Native Host | `com.bartosz.dev_bridge.vnext`, with dedicated manifest and registry keys. |
| Protocol | `bdb-vnext-protocol-v1`; unsupported generations fail closed, with no implicit legacy fallback. |
| Git/filesystem effects | Build/rehearsal uses isolated worktrees, fixtures and test refs. No vNext process may mutate an external resource still writable by legacy without the final generation fence. |

## 2.4. Reuse boundary

Keep the successful M1a-vNext mechanics: deterministic canonical JSON/digests, sanitization, bounded read-only observation, fail-closed path/identity collision checks, side-effect-free `bdb_vnext` and `bdb_shared` packages, and the isolated composition manifest/provider graph.

Reuse of a utility never imports legacy semantic authority. In particular, R0a receipt-shape compatibility defects found by R0b are not a reason to retrofit R0a or vNext unless fresh vNext isolation evidence or the final cutover inventory requires a bounded repair.

---

# 3. Source precedence

| Rank | Source | Binding role |
|---:|---|---|
| 1 | **Fresh observed repo/runtime/external authority** | Current facts: branch/HEAD/status, deployed bundles, stores, writers, locks, schema, effects, Git/FS/conversation state. Observation beats every report. |
| 2 | **Architecture Freeze v1 — Etap 3** | Frozen target invariants, authority boundaries, Browser quality and rejected architectures. |
| 3 | **This v1.1.1 canonical override** | Canonical migration strategy, supersession, vNext dependencies/order, status and final cutover floor. |
| 4 | **Migration/Execution Plan v1.1 — Parallel vNext Build** | Rationale and isolation details not changed by v1.1.1. |
| 5 | **Etap 6 — still-applicable parts** | Global AI Execution Protocol, STOP/escalation/drift rules, Basis Check, JIT Pack, validation/risk guidance, Handoff standard, and EU cards after applying this override. |
| 6 | **Etap 5 — still-applicable parts** | Existing EU IDs/invariants, fault intent, validation and rollback semantics after removing in-place/shared-store assumptions. |
| 7 | **Latest verified Execution Handoff for the active line** | Evidence of DONE/STOP/basis only; it cannot alter architecture or governance and must be revalidated. Current relevant record: M1a-vNext. |
| 8 | **R0b STOP analysis, V10, BDB 2.0 plan and older handoffs/materials** | Historical/legacy evidence and rationale only; never a route back to incremental migration. |

Conflict rules:

- Etap 6 saying “Etap 5 precedes” is superseded for migration governance by ranks 3–4 above.
- Etap 6 Initial Status Map is historical; Section 6 of this document is current.
- A verified newer Handoff may update execution status, but cannot silently change an invariant.
- A filename/symbol change is Level A drift. A changed EU dependency is Level B. Only concrete falsification of Architecture Freeze is Level C.

---

# 4. Supersession matrix

| Prior element | Disposition | Canonical meaning after v1.1.1 |
|---|---|---|
| Architecture Freeze v1 | `UNCHANGED` | All target invariants and authority boundaries remain binding. |
| Incremental in-place migration | `SUPERSEDED` | Replaced by parallel isolated vNext build. |
| Legacy R0b as prerequisite for vNext construction | `SUPERSEDED` | It does not block target-only work on `bdb-vnext`. |
| Existing R0b STOP result | `HISTORICAL ONLY` | Remains valid legacy evidence and remains `STOP`; it is not rewritten to DONE. |
| Fresh legacy observation using R0b semantics | `DEFERRED TO FINAL CUTOVER` | Re-run immediately before M9a; a STOP result may authorize only bounded M9a drain/reconciliation, never M9b activation. |
| Full reconciliation of all historical legacy state | `SUPERSEDED` | Only active/collision-relevant writers and unresolved external effects must be classified at final cutover. No requirement to reconcile all 192 commands. |
| Semantic migration of the old Journal | `SUPERSEDED` | Explicitly prohibited. Legacy state becomes a verified read-only archive. |
| Shared physical runtime, runtime root or mutable paths | `SUPERSEDED` | Each generation has dedicated identities and paths. |
| Same physical SQLite for legacy + target | `SUPERSEDED` | vNext uses a dedicated Control Store; X1 is reinterpreted for that store. |
| Existing formal EU IDs | `UNCHANGED` | All 41 IDs remain; no new ID is introduced and no formal EU is removed. |
| Etap 6 Global AI Protocol, STOP/drift, Basis Check, JIT Pack and Handoff | `UNCHANGED` | Apply them to exact `bdb-vnext` basis/generation and this dependency map. |
| Validation policy and exact-subject evidence | `UNCHANGED` | Evidence must bind exact vNext RepoView/Candidate/environment/checker; legacy evidence cannot prove vNext. |
| Early Engineering Intelligence | `UNCHANGED` | M2 remains early and begins now. |
| Browser-first/no-required-API | `UNCHANGED` | vNext Browser is a full primary mode, not fallback. |
| One writer/no dual-authority | `UNCHANGED` | During build: legacy writes only legacy, vNext production writer OFF. At cutover: fence, writer-off, then vNext writer-on. |
| M3c/M4f/M6c/M7c production cutover wording | `REINTERPRETED FOR VNEXT` | They close alternate authorities inside the isolated vNext generation; they do not disable operational legacy or activate vNext externally. |
| Legacy drain/freeze, Browser/Native generation switch and product activation | `DEFERRED TO FINAL CUTOVER` | Executed only in M9a/CC2/M9b after target build and fresh legacy inventory. |
| Full self-host activation authority | `DEFERRED TO FINAL CUTOVER` | M11a/b prepare and prove it; M11c performs the authority cutover. |
| Archive/compatibility-zero/final deletion | `DEFERRED TO FINAL CUTOVER` | M12a/b; archive is passive and never active truth. |
| Direct LIVE mutation | `UNCHANGED` | `DEFERRED UNTIL CAPABILITY ENABLE`; it does not block candidate workflow or cutover. |
| R0a compatibility changes motivated only by legacy receipt defects | `HISTORICAL ONLY` | Do not alter R0a unless a current vNext or final-cutover invariant requires a bounded change. |

---

# 5. Canonical vNext Execution Map

## 5.1. Interpretation rules

1. `BUILD-ONLY` means code, schema definitions, fixtures, disposable stores, isolated test refs and rehearsals may be created; no user-facing vNext runtime, production writer, Browser registration or Native Host routing is enabled.
2. `VNEXT INTERNAL CLOSURE` means one supported authority path inside the inactive vNext generation. It is not a legacy writer-off and not product activation.
3. All target-only EUs through `M8c` can proceed without legacy R0b reconciliation, subject to their target dependencies.
4. M1b/X1 remain activation floors for the dedicated vNext runtime/store; they no longer protect a shared legacy SQLite.
5. Actual legacy freeze, client generation switch and vNext writer/intake activation occur only in `M9a → CC2 → M9b`.
6. No EU may read legacy rows/files as vNext transition truth. A bounded archive/diagnostic reader is generation-qualified, read-only and mortal.

## 5.2. Disposition of all formal Execution Units

| Slot | EU | Treatment | Lane | Canonical vNext delta / justification |
|---|---|---|---|---|
| C0 | `R0a` | `KEEP AS-IS` | DONE/read-only | Its bounded inventory utilities remain reusable evidence mechanics; no authority change. |
| C1 | `M1a` | `REINTERPRET FOR VNEXT` | DONE/build-only | M1a-vNext established isolated composition/runtime identity at `de340e…`; runtime and writer remain OFF. |
| B01 | `M2a` | `KEEP WITH CHANGED PREREQUISITES` | BUILD-ONLY | Implementation now needs verified M1a-vNext only; persistence/activation still waits vNext M1b/X1/M1c. |
| B02 | `M1b` | `REINTERPRET FOR VNEXT` | BUILD-ONLY activation floor | Establish recovery/backup for the dedicated vNext runtime/store, not for an in-place legacy schema migration. |
| B03 | `X1` | `REINTERPRET FOR VNEXT` | EXPERIMENT/build-only | Test dedicated vNext SQLite single-writer, crash, backup and restore; remove legacy-reader/same-file assumptions. |
| B04 | `X2` | `KEEP WITH CHANGED PREREQUISITES` | EXPERIMENT/build-only | Typed content durability invariant is unchanged; its exact subject is the isolated vNext content store. |
| B05 | `M1c` | `REINTERPRET FOR VNEXT` | BUILD-ONLY | Construct only the explicit vNext provider root; do not rewrite or disable legacy composition. |
| B06 | `M2b` | `REINTERPRET FOR VNEXT` | BUILD-ONLY | Typed context transport uses dedicated vNext Browser/Native identities and never falls back to legacy protocol. |
| B07 | `M2c` | `KEEP AS-IS` | BUILD-ONLY | Understanding, ContextRequest and Decision contracts remain unchanged and bind exact vNext RepoViews. |
| B08 | `M2d` | `KEEP AS-IS` | BENCHMARK/build-only | Browser quality/non-inferiority gate remains mandatory before lifecycle work. |
| B09 | `M3a` | `REINTERPRET FOR VNEXT` | BUILD-ONLY | Create Submission/Task substrate only in the dedicated vNext store; no old Journal import or dual-write. |
| B10 | `M3b` | `REINTERPRET FOR VNEXT` | BUILD-ONLY | Build restart-safe admission for the dedicated vNext extension/host with unsupported legacy fallback sealed. |
| B11 | `M3c` | `REINTERPRET FOR VNEXT` | VNEXT INTERNAL CLOSURE | Prove canonical vNext admission is the only vNext path; operational legacy remains untouched and external activation stays OFF. |
| B12 | `M4a` | `REINTERPRET FOR VNEXT` | BUILD-ONLY | Build Work Kernel/current state in the dedicated vNext Control Store, not beside Command/Session rows. |
| B13 | `M4b` | `KEEP WITH CHANGED PREREQUISITES` | BUILD-ONLY | Exact Candidate/local effect semantics remain, but all writes use isolated vNext worktrees/resources. |
| B14 | `M4c` | `KEEP AS-IS` | BUILD-ONLY | Exact Candidate evidence/environment/applicability contract is unchanged. |
| B15 | `M4d` | `REINTERPRET FOR VNEXT` | BUILD-ONLY | Publication/Resume use vNext consumer/protocol identity; legacy result/outbox is not a target bridge. |
| B16 | `CC0` | `REINTERPRET FOR VNEXT` | BUILD-ONLY/read-only | MOV reads vNext canonical queries only; legacy may appear only as a clearly namespaced diagnostic/archive source. |
| B17 | `M8a` | `KEEP WITH CHANGED PREREQUISITES` | BUILD-ONLY | Migrate vNext consumers to exact RepoView queries; there is no target dependency on legacy mixed endpoints. |
| B18 | `M4e` | `REINTERPRET FOR VNEXT` | REHEARSAL/build-only | Full Browser rehearsal runs against isolated vNext resources without switching the user’s operational tool. |
| B19 | `M4f` | `REINTERPRET FOR VNEXT` | VNEXT INTERNAL CLOSURE | Make Work Kernel the sole vNext work writer in inactive/rehearsal generation; legacy writer-off is postponed to M9. |
| B20 | `M5a` | `KEEP WITH CHANGED PREREQUISITES` | BUILD-ONLY | Effect certainty/reconciliation remains unchanged and applies only to vNext effects after internal M4f closure. |
| B21 | `M5b` | `REINTERPRET FOR VNEXT` | VNEXT INTERNAL CLOSURE | Delete only transitional vNext retry/watch authority; do not delete legacy recovery paths while legacy operates. |
| B22 | `M6a` | `KEEP AS-IS` | BUILD-ONLY | Promotion-grade evidence semantics remain exact and target-bound. |
| B23 | `M6b` | `KEEP AS-IS` | BUILD-ONLY/shadow | Deterministic vNext CheckPlan shadow remains unchanged. |
| B24 | `M7a` | `KEEP WITH CHANGED PREREQUISITES` | BUILD-ONLY | Prepared CAS is tested on isolated vNext repos/refs; no production or legacy ref is touched. |
| B25 | `M7b` | `KEEP WITH CHANGED PREREQUISITES` | BUILD-ONLY | Checkout sync remains a separate effect in a dedicated vNext worktree with foreign state preserved. |
| B26 | `M6c` | `REINTERPRET FOR VNEXT` | VNEXT INTERNAL CLOSURE | Canonical evidence is the sole vNext gate; legacy validation remains authority only for legacy until freeze. |
| B27 | `M7c` | `REINTERPRET FOR VNEXT` | VNEXT INTERNAL CLOSURE | Close vNext promotion around Git truth/CAS on isolated refs; production ref activation and legacy promoter retirement wait for final cutover. |
| B28 | `M8b` | `KEEP AS-IS` | BUILD-ONLY | RepoView-bound rebuildable Index/Understanding projections are unchanged. |
| B29 | `CC1` | `REINTERPRET FOR VNEXT` | BUILD-ONLY/package | Build the vNext canonical-query main UI, but do not replace the operational legacy UI before M9b. |
| B30 | `M8c` | `KEEP AS-IS` | OPTIONAL/NON-BLOCKING | Honest LIVE observation remains valid and may run in parallel or defer; it does not block final cutover. |
| F01 | `R0b` | `MOVE TO FINAL CUTOVER` | LEGACY GATE | Current attempt remains STOP/legacy evidence; perform a fresh bounded attempt immediately before M9a. |
| F02 | `M9a` | `MOVE TO FINAL CUTOVER` | FINAL LEGACY FREEZE | Stop legacy ingress, identify writers, drain/classify only collision-relevant work/effects and produce a verified archive candidate. |
| F03 | `CC2` | `MOVE TO FINAL CUTOVER` | FINAL DEMOTION | Remove active legacy interpretation after freeze; preserve only namespaced read-only archive access. |
| F04 | `M9b` | `MOVE TO FINAL CUTOVER` | PRODUCT ACTIVATION | Under one fresh fence, switch Browser/Native generation, enable vNext writer/intake, disable legacy lifecycle writers and prove no duplicate route. |
| F05 | `M11a` | `REINTERPRET FOR VNEXT` | FINAL HARDENING | Build exact vNext A/P/C slots and compatibility substrate without importing legacy lifecycle state. |
| F06 | `M11b` | `REINTERPRET FOR VNEXT` | FINAL EXPERIMENT | Run activation fault matrix against vNext slots/store and the actual post-switch compatibility boundary. |
| F07 | `M11c` | `MOVE TO FINAL CUTOVER` | SELF-HOST CUTOVER | External Bootstrap becomes sole vNext activation authority; old installer/hotfix writer stays off. |
| F08 | `M12a` | `MOVE TO FINAL CUTOVER` | FINAL GATE | Verify archive/readability, bridge usage zero and every retention/drop/contract disposition; no semantic import. |
| F09 | `M12b` | `MOVE TO FINAL CUTOVER` | FINAL RELEASE | Release target-only bundle, perform approved final deletions/contracts and leave legacy only as read-only archive. |

No formal EU is `SUPERSEDE` or `REMOVE`. What is superseded is the old in-place meaning and dependency chain, not the frozen IDs/invariants.

## 5.3. Canonical single-worker order and dependency map

```text
COMPLETED BASIS
R0a DONE
→ M1a-vNext DONE @ de340e2564b233b8b395bdcc1dc96e8f733f44a7

TARGET-ONLY BUILD — legacy R0b is not on this critical path
→ M2a
→ M1b-vNext → X1-vNext
→ X2
→ M1c-vNext
→ M2b → M2c → M2d
→ M3a → M3b → M3c [vNext internal closure only]
→ M4a → M4b → M4c → M4d → CC0 → M8a → M4e
→ M4f [vNext internal closure only]
→ M5a → M5b
→ M6a → M6b → M7a → M7b → M6c → M7c
→ M8b → CC1
→ M8c [non-blocking; may be parallel/deferred]

FINAL CUTOVER TRANCHE
R0b [fresh final attempt; prior STOP remains historical]
→ M9a legacy ingress stop/drain/freeze + archive candidate
→ CC2 legacy active interpretation off
→ M9b Browser/Native generation switch + vNext writer/intake ON + legacy writers OFF
→ M11a → M11b → M11c
→ M12a archive/compatibility-zero gate
→ M12b target-only release/final deletion
```

Implementation-vs-activation rules:

- M2a may be implemented read-only before M1b/X1; persisted/installed vNext activation still waits for them.
- M3a→M8c may use disposable stores, isolated worktrees and test refs; no production legacy resource may be shared.
- M3c/M4f/M6c/M7c DONE proves internal vNext single-authority and writer-off of transitional **vNext** paths only.
- CC1 DONE means a build/package is ready, not that legacy main navigation has switched.
- Only M9b may turn the user-facing vNext writer/intake ON.
- M8c and Direct LIVE mutation do not block M9; M8a does.
- A fresh R0b STOP does not block the bounded read-only/drain work authorized by M9a; it does block M9b until M9a resolves or fences every material blocker.

---

# 6. Current execution status

| Item | Current canonical status | Evidence / consequence |
|---|---|---|
| `R0a` | `DONE` | Reusable read-only inventory/evidence utilities exist. |
| `R0b` | `STOP / RECONCILIATION_REQUIRED / LEGACY EVIDENCE` | Do not relabel DONE. It is removed from the vNext build critical path and must be refreshed at final cutover. |
| `M1a-vNext` | `DONE` | Accepted handoff basis `de340e2564b233b8b395bdcc1dc96e8f733f44a7`; isolated identity/composition checks implemented. |
| `M2a` | `READY` | Selected next single-worker EU; see Section 7. |
| All other B/F units | `PLANNED` | No supplied completion evidence. Final cutover units remain dependency-blocked. |
| Legacy generation | `FROZEN_OPERATIONAL` | May continue as a tool on its own runtime/stores. |
| vNext runtime | `NOT ACTIVATED` | Runtime root not proven created; writer and activation remain OFF. |

The accepted branch/commit/worktree facts are from the supplied Handoff. Fresh inspection is mandatory before M2a mutation.

---

# 7. Next READY EU

## `M2a — COMMITTED RepoView Foundation`

**Why READY:** M1a-vNext is DONE, and M2a implementation requires only the verified target composition/runtime identity basis. M2a is read-only/shadow and does not need legacy runtime access, R0b reconciliation, a vNext writer, or product activation.

**Implementation dependencies:**

- fresh `bdb-vnext` inspection confirms branch/HEAD/status/upstream and M1a-vNext handoff;
- accepted basis is still `de340e2564b233b8b395bdcc1dc96e8f733f44a7` or drift is classified before work;
- explicit vNext provider can be constructed side-effect-free;
- exact Git object reads and repository containment are available;
- no foreign change overlaps the JIT mutation scope.

**Activation dependencies (not implementation blockers):** vNext M1b + X1 + M1c, then the final M9b product activation gate.

**Scope:** minimal Repository resource and immutable `COMMITTED` RepoView with exact repository/commit/tree/provenance binding, typed read/query boundary, stale/moving-ref and wrong-repository tests.

**Out of scope:** legacy R0b repair, receipt-shape compatibility, old Journal import, vNext runtime/store activation, CANDIDATE/LIVE, index redesign, Task/WorkItem lifecycle, Browser install, Native Host registration, drain or cutover.

**Reasoning/risk:** `HIGH` — prefer `Bardzo wysoki`; constrain implementation with one fresh JIT Pack.

**Validation class:** `TARGETED` — RepoView identity/provenance/property/fault tests plus M1a composition regression needed by the changed provider boundary. No FULL by default.

Exactly one unit is selected `READY` under single-worker WIP=1. Other parallel-capable units remain `PLANNED` until the M2a Handoff or an explicit operator decision changes the selected lane; this is scheduling, not a false dependency.

---

# 8. Final cutover prerequisites

These gates block only final activation/release, not current target-only construction.

| Gate | Minimum required evidence | Owning EU(s) |
|---|---|---|
| Target readiness | Required supported vNext path, Browser quality, recovery, exact evidence/query and internal single-authority closures are DONE; active bundle/branch/basis is exact. | M1b/X1, M2d, M4e, M4f, M5b, M6c, M7c if Git enabled, M8a, CC1 |
| Fresh legacy inventory | Re-observe deployed legacy bundles, writers/locks, stores, protocol and collision-relevant work/effects. The old R0b report is not fresh enough. | R0b final attempt, M9a |
| Legacy ingress stop and writer freeze | Every supported legacy entrypoint is closed; no new Command/Session/receipt/spool/promotion work can be admitted. | M9a |
| Unresolved/ambiguous effects | Every effect able to touch a resource used by vNext is terminal, externally observed, safely drained, quarantined behind a durable fence, or blocks that resource’s cutover. Historical rows with no remaining writer/collision capability need not be semantically reconstructed. | M9a |
| Archive candidate before switch | Legacy DB/WAL/files/required receipts and manifests have a consistent, integrity-checked, readable archive candidate with exact identity and retention policy. No vNext import is performed. | M9a; final confirmation M12a |
| Browser/Native generation switch | Exact vNext extension/host/protocol identities are installed atomically enough to fail closed; old clients cannot silently fall back or submit twice. | M9b |
| Writer/resource fencing | Stop legacy intake → enumerate/drain/classify → acquire fresh generation/resource fence → enable vNext writer/intake → disable legacy writers → negative duplicate-path test. | M9a/M9b |
| vNext activation and health | Dedicated runtime/store/config/locks exist, M1b/X1 recovery floor is current, exact bundle starts healthy, canonical queries identify the active generation. | M1b/X1/M9b |
| Rollback boundary acknowledged | Before first accepted vNext work and before destructive archive/contract steps, abort may be possible if fresh evidence permits. After first accepted vNext Task/effect or state-forward boundary, authority recovery is roll-forward only through a compatible vNext runtime. Legacy may be restored only as read-only evidence, never as writer. | M9b, M11c, M12a/b |
| No dual-authority proof | Process/lock/store/path/registry/telemetry scans and negative tests show exactly one active admission/work/effect/promotion authority for each enabled resource. | M9b; reconfirm M11c/M12a |
| Bootstrap self-host | External A/P/C slots and fault matrix pass; candidate cannot activate itself; old installer/hotfix writer is disabled. | M11a/b/c |
| Compatibility zero and final archive | Every bridge has zero supported writer/consumer use or a named offline archive consumer; archive readability and final deletion/contract plan pass. | M12a/b |

Explicit non-requirements:

- do not reconcile all 192 legacy commands;
- do not semantically import the legacy Journal;
- do not retrofit R0a merely to normalize old receipt shapes;
- do not keep a legacy writer alive “for rollback” after vNext acceptance;
- do not let an unresolved historical record block vNext when it has no live writer, no external collision capability and an explicit archive disposition.

---

# 9. What a new AI must read before executing the next EU

Required bootstrap packet for M2a:

1. **Etap 3 — Architecture Freeze v1**.
2. **This v1.1.1 override** — especially Sections 2–8.
3. **Etap 6** — Global AI Execution Protocol, EU Basis Check, JIT Pack/Handoff standards and the `M2a` card, interpreted through v1.1.1.
4. **Latest Execution Handoff — M1a-vNext**.

Etap 5 and Plan v1.1 are reference material for invariant rationale and drift investigation; they may not restore R0b or shared-SQLite as M2a prerequisites. The R0b STOP analysis is legacy evidence and is out of M2a mutation scope.

Bootstrap instruction:

```text
Current EU is M2a on bdb-vnext. Treat Migration & Execution Plan v1.1.1 as the canonical execution override. Verify the M1a-vNext Handoff against the actual branch/HEAD/worktree, run the Etap 6 EU Basis Check, and create a JIT M2a Implementation Pack. Work only on the read-only/shadow COMMITTED RepoView foundation. Do not access or repair legacy runtime/state, activate vNext, create a shared store, or perform any cutover. If de340e… is no longer the actual basis, classify drift before mutation. Finish with an Execution Handoff.
```

No previous chat history is required.

---

# 10. Freeze verdict

The supplied evidence is sufficient to resolve the execution-plan discrepancy without reopening Architecture Freeze.

> **Architecture Freeze v1 = UNCHANGED**

> **Migration & Execution Plan v1.1.1 = FROZEN**

> **Canonical migration strategy = PARALLEL_ISOLATED_VNEXT_BUILD**

> **R0b = STOP / LEGACY EVIDENCE / NOT A VNEXT BUILD BLOCKER**

> **M1a-vNext = DONE @ de340e2564b233b8b395bdcc1dc96e8f733f44a7**

> **NEXT READY EU = M2a — COMMITTED RepoView Foundation**

> **READY FOR M2a JIT INSPECTION — NO IMPLEMENTATION PERFORMED IN THIS TASK**

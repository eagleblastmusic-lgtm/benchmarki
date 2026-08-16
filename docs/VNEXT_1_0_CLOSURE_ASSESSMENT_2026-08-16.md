# BDB vNext 1.0 — Closure Assessment

Date: 2026-08-16

Status: `ASSESSMENT / NON-AUTHORITATIVE / DOES NOT ALTER FROZEN GOVERNANCE`

Exact assessment basis:

- branch: `bdb-vnext`;
- commit: `c0f6ff0dd8b74a624915d331916953de6b861c73`;
- tree: `6009cbb05d8b3142746c0f3c2330a6b0374292ba`;
- production runtime / writer / activation remain `OFF / OFF / OFF`;
- legacy remains operational, isolated and untouched.

This document prevents three different meanings of “finish vNext” from being conflated. It records observed implementation/evidence against the frozen execution/product plans, but it does not replace Architecture Freeze, Plan v1.1.1, Plan v1.1.2, or any final cutover gate.

## 1. Three closure levels

### A. Native Engineering Baseline

Verdict: `PROVEN / STABLE BASELINE CANDIDATE`.

The current implementation has real evidence for the normal-ChatGPT engineering vertical: canonical Task/Work/Run identity, exact committed RepoView authority, bounded model-authored `BDB_EDIT_V1`, Candidate mutation/seal/recovery, bounded validation with typed feedback, Evidence/Evaluation/Disposition/Publication, same-conversation repair, Browser/Native transport, and exact Browser Runner verification. The calculator P1 pilot and two real Giclée pilots exercise this path beyond synthetic tests.

`P1-CWEL-1` is already recorded as `CLOSED`. Later commits additionally hardened exact replacements, accumulated multi-turn state, Windows paths, large unchanged tracked files, committed-byte model context, orphan Candidate recovery, thin exact base authority, and Browser Runner v1.1 plus deterministic package/repair-envelope support.

This level can be frozen as the stable **BDB Native engineering baseline** without claiming that the entire frozen BDB Next 1.0 product plan is complete.

### B. Frozen-plan build-only BDB Next 1.0

Verdict: `NOT YET FORMALLY CLOSED`.

The frozen v1.1.1 build queue continues after M4f through M5/M6/M7/M8 and CC1 before final cutover. The current repository contains substantial equivalent primitives, but several later gates are either only partially represented, lack an exact closure record, or have not been implemented as the vNext authority described by the frozen plan.

Current classification:

| Gate | Assessment at basis | Reason |
|---|---|---|
| C1–B19 through `M4f` / rehearsal | `SUBSTANTIALLY CLOSED / PROVEN` | Current N1–N6 vertical plus P1 and real Giclée pilots establish the core authority, Candidate, Evidence, Publication, Browser and recovery path. |
| B20 `M5a` effect certainty/reconciliation | `PARTIAL` | Run effect certainty is explicit and recovery is fail-closed, but no fresh formal M5a closure has been established against the current architecture. |
| B21 `M5b` internal recovery-authority closure | `NOT FORMALLY CLOSED` | Current recovery paths are hardened, but the frozen M5b transitional-authority deletion/closure has not been reconciled as a named current gate. |
| B22 `M6a` promotion-grade evidence | `SUBSTANTIALLY IMPLEMENTED / CLOSURE MISSING` | Exact Evidence applicability, evaluation and publication gates exist and are exercised, but no current M6a closure record binds them to the frozen gate. |
| B23 `M6b` deterministic CheckPlan shadow | `PARTIAL / EQUIVALENT PRIMITIVES EXIST` | Bounded checker/validation policy exists; equivalence to the frozen CheckPlan contract needs an exact source-backed decision rather than inference from naming. |
| B24 `M7a` prepared CAS | `PARTIAL` | CAS and exact Candidate authority exist, but prepared promotion-CAS behavior on isolated refs has not been closed as M7a. |
| B25 `M7b` checkout sync as separate effect | `PARTIAL / NOT EQUIVALENTLY PROVEN` | Candidate isolation and exact trees exist; that is not by itself proof of the frozen checkout-sync effect contract. |
| B26 `M6c` canonical evidence sole vNext gate | `SUBSTANTIALLY IMPLEMENTED` | Current Publication path requires current applicable evidence and fails closed; formal gate reconciliation is still desirable. |
| B27 `M7c` vNext promotion closure around Git truth/CAS | `NOT CLOSED` | Current proven engineering pilots deliberately leave source refs/checkouts unchanged; they do not prove canonical vNext promotion on isolated refs. |
| B28 `M8b` rebuildable RepoView-bound Index/Understanding | `SUBSTANTIALLY IMPLEMENTED` | RepoView/Understanding/engineering intelligence are exact-source-bound; confirm current rebuildability contract during final gate reconciliation. |
| B29 `CC1` vNext canonical-query main UI | `NOT CLOSED AS VNEXT CC1` | A substantial PySide6 Control Center exists, but its current bootstrap is through `bdb_operator.OperatorApi`; this is not evidence that CC1 is the vNext canonical-query UI required by the frozen plan. |
| B30 `M8c` honest LIVE observation | `OPTIONAL / DEFER` | Frozen plan explicitly marks it non-blocking. |

Therefore the proposed **new vNext interface is not merely post-vNext polish** if we continue to use the frozen Work plan literally: it is the natural implementation of `CC1` and belongs before declaring the build-only BDB Next 1.0 product closed.

### C. Product activation, legacy cutover and final release

Verdict: `INTENTIONALLY NOT STARTED / DO NOT ACTIVATE NOW`.

The frozen final gates remain separate:

- fresh legacy R0b observation immediately before cutover;
- M9a legacy ingress stop/drain/freeze and archive candidate;
- CC2 legacy active-interpretation demotion;
- M9b Browser/Native generation switch and vNext writer/intake activation;
- M11a/b/c activation-slot hardening, fault matrix and self-host authority cutover;
- M12a/b archive/compatibility-zero/final release.

Nothing in the successful engineering pilots authorizes these gates. Production remains `OFF / OFF / OFF`, and this assessment makes no activation or cutover claim.

## 2. Existing GUI/productization evidence

The repository already contains a substantial PySide6 Control Center and a Windows portable packaging path. The 0.3.0 release artifact process produces a version-bound PyInstaller package, manifest, headless smoke and standalone acceptance receipt. It explicitly does **not** provide MSI/MSIX installation, code signing, automatic update, production publishing or deploy.

The current GUI is useful implementation material for CC1, not proof that CC1 is already closed. Its bootstrap currently consumes the pre-existing `bdb_operator` read-only API, so a vNext-specific canonical projection boundary must be designed explicitly rather than assumed.

## 3. Recommended completion boundary

The recommended meaning of “finish vNext” for the current project is:

1. freeze the current proven Native engineering vertical as `NATIVE_ENGINEERING_BASELINE_STABLE`;
2. complete/reconcile only the remaining **build-only** frozen gates B20–B29;
3. implement the new vNext-oriented Control Center as the CC1 surface over canonical vNext projections;
4. keep production activation/cutover F01–F09 out of scope until an explicit later decision;
5. after the build-only baseline is closed, add `GITHUB_DIRECT` as a separate engineering mode rather than rewriting the Native authority path.

This preserves the value of the frozen Work plan while allowing the later GitHub mode to be an additive product capability.

## 4. Immediate next step

Do not add another broad feature yet. Perform a narrow source-backed reconciliation of B20–B28 against the exact current implementation and tests. For each gate, choose exactly one outcome:

- `CLOSED_BY_EXISTING_IMPLEMENTATION` — cite exact current code/tests/evidence;
- `SMALLEST_IMPLEMENTATION_REQUIRED` — identify the minimum missing authority/contract and implement only that;
- `DEFERRED_BY_FROZEN_PLAN` — only where the plan itself makes the gate optional or final-cutover-only.

CC1 should then be implemented as the first clearly user-facing vNext product surface, reusing the existing PySide6 Control Center where safe instead of creating a second GUI.

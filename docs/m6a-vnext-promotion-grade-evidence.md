# BDB vNext M6a — Promotion-Grade Evidence Core

## 1. Architectural Role & Boundary

`bdb_vnext.m6a_evidence_policy.EvidencePolicyGate` implements the deterministic evidence evaluation, obligation assessment, waiver decision, and approval gating layer over the canonical M4c Evidence infrastructure.

### Key Invariants:
1. **M4c Remains Canonical Evidence Authority**:
   `EvidenceStore` retains exclusive ownership over immutable raw observations, evaluations, dispositions, candidate view integrity, and verification of evaluator/checker identity.
2. **M6a is Strictly Additive**:
   M6a wraps M4c with structured semantic obligations, assessments, waivers, and approvals without modifying the core evidence storage or lifecycle primitives.
3. **No Database Split / Reused Connection**:
   M6a tables (`m6a_obligations`, `m6a_assessments`, `m6a_waiver_decisions`, `m6a_approvals`) reside directly in the unified Control DB and share `evidence_store._connection`.
4. **No "Proof" Terminology or Arbitrary Engines**:
   M6a introduces no generic proof graphs, no Temporal/DBOS workflows, and no non-deterministic rule engines.

---

## 2. Core Entities & Lifecycle

### 2.1. Obligation (`bdb-vnext-m6a-obligation-v1`)
Represents an immutable requirement bound to an exact subject (`subject_kind` + `subject_identity` $\rightarrow$ `subject_digest`) and an evidence contract (`evidence_type`, `coverage`, `freshness`, optional checker identity and environment fingerprint).
* **Waivability Levels**:
  - `NEVER`: Gating strictly requires `PASS` or `NOT_APPLICABLE`; all waivers are rejected.
  - `AUTHORIZED_USER`: Waivers by `USER` or `ADMIN` authority are permitted.
  - `ADMIN_ONLY`: Waivers require `ADMIN` authority.

### 2.2. Assessment (`bdb-vnext-m6a-assessment-v1`)
Records the evaluation of an obligation against M4c evidence:
* **Statuses**: `SATISFIED`, `UNSATISFIED`, `UNKNOWN`, `STALE`.
* **Applicability**: `APPLICABLE`, `NOT_APPLICABLE`, `UNKNOWN`.
* **Derived Verdicts**:
  - `SATISFIED` + `APPLICABLE` $\rightarrow$ `PASS`
  - `UNSATISFIED` + `APPLICABLE` $\rightarrow$ `FAIL`
  - `NOT_APPLICABLE` $\rightarrow$ `NOT_APPLICABLE`
  - `UNKNOWN` or `STALE` $\rightarrow$ `UNKNOWN`
* **Critical Rule**: `WAIVED` is **never** an Assessment status. An unsatisfied obligation remains `UNSATISFIED`.

### 2.3. WaiverDecision (`bdb-vnext-m6a-waiver-decision-v1`)
An immutable record issued by an actor with `USER` or `ADMIN` authority granting a scoped, time-bounded exception for an obligation. The promotion gate evaluates waiver applicability at gate evaluation time (`allowed_by_waiver = true`).

### 2.4. ApprovalRecord (`bdb-vnext-m6a-approval-v1`)
An immutable cryptographic approval bound to exact dimensions:
* `subject_digest`
* `intent_revision_id`
* `effect_digest`
* `policy_digest`
* `scope`
* `expires_at`

Any drift in these parameters or expiration immediately invalidates the approval.

---

## 3. Promotion Evidence Gate (`EvidencePolicyGate.promotion_gate`)

The promotion gate executes deterministic evaluation across all obligations and approval bindings:
* Evaluates all specified obligations against latest assessments.
* Validates matching unexpired waivers for unsatisfied waivable obligations.
* Validates the exact cryptographic approval binding.
* Outputs a deterministic decision (`ALLOW` or `BLOCK`) with a semantic `decision_digest`.

---

## 4. Production & Promotion Guardrails

* **No Production Authority Cutover**:
  M6a does not trigger Git ref updates, worktree checkouts, remote pushes, Shopify mutations, or runtime promotion.
* **Production Status**:
  All production writers, runtimes, and activation flags remain strictly `OFF / OFF / OFF`.
* **Next Step**:
  **M6b Deterministic CheckPlan Shadow** (generating deterministic candidate verification plans prior to promotion evaluation).

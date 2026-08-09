# BDB — Migration & Execution Plan v1.1.2

## BDB Next 1.0 Independent Side-by-Side Product Override

**Status:** `CANONICAL PRODUCT/EXECUTION OVERRIDE — FROZEN`
**Date:** `2026-08-09`
**Architecture Freeze v1:** `UNCHANGED`
**Canonical strategy:** `PARALLEL_ISOLATED_VNEXT_BUILD + INDEPENDENT_SIDE_BY_SIDE_PRODUCTS`
**Development branch:** `bdb-vnext`
**Accepted evidence basis before this docs-only change:** `f3b37da34782bac2422f3053f0b54d96672b5802`

This is an additive, delta-only override to the product and late-stage execution
meaning of Plan v1.1.1. It is not a new architecture review, roadmap, or BDB
redesign. It does not edit Architecture Freeze v1, Etap 5, Etap 6, Plan v1.1,
or Plan v1.1.1. Where their late-stage operational assumptions conflict with
this product decision, this document is authoritative for that conflict only.

## 1. Frozen product decision

BDB Next 1.0 is an independently installable and independently selectable
product generation that may coexist side-by-side with BDB Legacy. BDB Next does
not require retirement, archival-only conversion, or uninstall of BDB Legacy as
a condition of release or use.

BDB Legacy remains a normally operating, independent product. A user may
consciously choose BDB Legacy or BDB Next 1.0 for a piece of work. Coexistence
does not create a dual authority: one generation owns mutation/promotion
authority for a concrete repository/effect subject at a time. The other may be
installed and available, but must not perform a competing canonical effect.

This override does not introduce a general cross-generation lease system. The
later Git/effect execution units must provide bounded collision detection and
fail-closed behavior for the subjects they enable.

## 2. Non-negotiable side-by-side isolation

The two product generations have separate:

- Browser Extension identity;
- Native Host identity and registration;
- protocol generation;
- runtime root;
- Control Store/database;
- configuration generation;
- backups, logs, spool and receipts;
- locks, PIDs and lifecycle state.

There is no shared mutable SQLite, shared mutable runtime, semantic import of
legacy lifecycle state, automatic fallback in either direction, silent
cross-generation retry, or automatic Task/WorkItem/effect transfer. Every such
object remains generation-qualified.

Both products may be installed simultaneously. For the same repository, each
product uses isolated worktrees and resources. A cross-generation Git mutation
collision must be detected and fail closed before the real effect; its exact
mechanism belongs to the later Git/effect units and is not implemented here.

## 3. Source precedence

For product topology and the late-stage reinterpretations below, precedence is:

1. **Fresh observed repo/runtime/external authority**.
2. **Etap 3 — Architecture Freeze v1**.
3. **This Plan v1.1.2 — BDB Next 1.0 Side-by-Side Product Override**.
4. **Plan v1.1.1 — Canonical Parallel vNext Execution Override**.
5. **Plan v1.1 — Parallel vNext Build**.
6. **Still-applicable portions of Etap 6**.
7. **Still-applicable portions of Etap 5**.
8. **Latest verified Execution Handoff** as evidence/status, never governance
   authority.

The v1.1.2 rank supersedes only the v1.1.1 assumptions that required
`Legacy OFF → archive-only → BDB Next as the sole installed operational
product`. The parallel isolated build, frozen target invariants, formal EU IDs,
and earlier accepted evidence remain valid.

## 4. Earlier execution remains unchanged

The following accepted units retain their status, code and evidence without
reinterpretation or repair:

- `M1a-vNext — DONE`;
- `M2a — DONE`;
- `M1b-vNext — DONE`;
- `X1-vNext — DONE / FINAL ACCEPT`.

The target-only build path remains:

```text
X2 → M1c → M2b → M2c → M2d → …
```

`X2` is the next planned unit and is not started by this override. vNext
runtime, writer and product activation remain `OFF / OFF / OFF`.

## 5. Late-stage execution reinterpretation

The following meanings replace only the global-retirement reading of the named
units. They do not turn BDB Legacy into an archive-only product.

| Unit | Side-by-side meaning after v1.1.2 |
|---|---|
| **fresh R0b** | Not a mandatory inventory for retiring all Legacy. When Next will run on the same repository/machine boundary, perform a bounded coexistence/conflict inventory: active Legacy writers, shared external subjects, Browser/Native identity collision, repository mutation collision, and port/path/process collision. Legacy rows are not Next transition truth. |
| **M9a** | Coexistence safety and per-subject handoff only where Next will assume a concrete mutation authority. Identify, drain or fence the relevant subject; do not globally stop or freeze the Legacy product. |
| **CC2** | Remove ambiguity between Next and Legacy UI, not the Legacy Control Center. Use explicit generation names and statuses; neither UI may provide cross-generation canonical interpretation. |
| **M9b** | BDB Next 1.0 product activation: enable its own Browser/Native/runtime/intake/writer path after duplicate-route and collision checks. Legacy remains installed, selectable and operational. |
| **M11a** | Build the external Bootstrap slots and compatibility substrate for Next. Bootstrap is not a global activation authority for Legacy. |
| **M11b** | Execute the activation fault matrix for the Next slots/runtime/store and its compatibility boundary. It does not require disabling or uninstalling Legacy. |
| **M11c** | Make external Bootstrap the sole BDB Next activation authority. Candidate self-activation and old Next activation paths remain disabled; Legacy activation authority is outside this unit. |
| **M12a** | Final Next release gate: prove Next-specific bridges, compatibility paths and lifecycle routes have the required zero/retention disposition. Do not require Legacy to become archive-only or to be removed. |
| **M12b** | Seal and release BDB Next 1.0 as an independent product. Do not delete, uninstall, or globally demote the independently operating BDB Legacy product; remove only paths explicitly owned by the Next release contract. |

For any same-subject handoff, the simple invariant is: one generation owns
canonical mutation/promotion authority for that repository/effect subject at a
time; the other generation is blocked or read-only for that subject. No
implicit fallback or cross-generation retry is permitted.

## 6. BDB Next 1.0 release definition

`BDB Next 1.0 RELEASED` means that Next has, and can operate with, its own:

- installer/install identity;
- Browser Extension;
- Native Host and registration;
- Control Center;
- runtime root, Control Store and configuration;
- backups, logs, locks/PIDs and lifecycle state.

The release must be runnable without Legacy and must coexist with Legacy when
both are installed. Disabling or uninstalling Next must not damage Legacy;
disabling or uninstalling Legacy must not damage Next. There is no automatic
fallback, shared lifecycle authority, shared mutable runtime, or shared mutable
SQLite between the products.

## 7. Freeze and scope guard

This override preserves Architecture Freeze v1 and the single-authority,
Browser-first, exact-evidence, external-Bootstrap and state-forward invariants.
It does not authorize:

- production code, tests, schemas, runtime, installer or extension changes;
- X2, M1c or any subsequent execution unit in this task;
- legacy state migration or semantic Journal import;
- a global rename of `vnext` to `Next`;
- a shared writer, shared mutable store, silent compatibility bridge or
  cross-generation retry.

No broad legacy reconciliation is required for target-only construction. A
bounded coexistence inventory becomes relevant only before Next activation on a
shared repository/machine boundary, and only for live collision/authority
facts.

## 8. Freeze verdict

> **Architecture Freeze v1 = UNCHANGED**

> **Plan v1.1.2 = FROZEN CANONICAL SIDE-BY-SIDE PRODUCT OVERRIDE**

> **BDB Legacy = INDEPENDENTLY OPERATIONAL**

> **BDB Next 1.0 = INDEPENDENTLY INSTALLABLE / SELECTABLE / COEXISTING**

> **M1a / M2a / M1b / X1 = UNCHANGED DONE EVIDENCE**

> **NEXT READY UNIT = X2 — NOT STARTED**

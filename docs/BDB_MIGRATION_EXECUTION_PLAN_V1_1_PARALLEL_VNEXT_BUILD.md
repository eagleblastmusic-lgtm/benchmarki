# Migration/Execution Plan v1.1 — Parallel vNext Build

Status: `PLAN FREEZE DISCREPANCY` accepted. This is a delta to Migration Plan Freeze v1 and the Etap 6 Playbook; it does not reopen or restate Architecture Freeze v1.

## Delta only

- Legacy BDB remains a frozen, working GicléeApp tool. No legacy feature development or broad reconciliation is a prerequisite for building vNext.
- vNext is built from R0a commit `4998aa16ff68d728637d09639ac79ced886393f6` on long-lived branch `bdb-vnext`, in a separate worktree and runtime generation.
- R0a remains reusable read-only diagnostics. Historical Journal, ACK, receipt and promoter compatibility work is excluded unless a bounded fact is required by a vNext isolation or final cutover gate.
- Existing Execution Units and frozen invariants remain authoritative. Their implementation and activation dependencies are interpreted against the vNext generation; legacy R0b reconciliation is not a vNext implementation dependency.
- Legacy Journal/state remains current legacy authority while that tool is running, then becomes a read-only archive at final cutover. It is not semantically imported into the vNext Control Store.
- Drain, freeze, archive verification and authority cutover remain deferred to the final M9/M11/M12 gates. Parallel build never implies activation or dual-write.

## Mandatory generation isolation

| Boundary | vNext rule |
|---|---|
| Runtime root | Dedicated `BartoszDevBridge-vNext`; no overlap with legacy or source worktree. |
| Control state | Dedicated Control DB/store; no legacy Journal writer or semantic import. |
| Transport state | Dedicated spool, results and receipts below the vNext runtime root. |
| Process ownership | Dedicated lock and PID paths; generation-qualified writer identity. |
| Configuration | `bdb-vnext-config-v1`, stored only below the vNext runtime root. |
| Browser | Stable vNext component ID and a dedicated packaging key/extension ID before install. |
| Native Host | `com.bartosz.dev_bridge.vnext`, dedicated manifest and registry keys. |
| Protocol | `bdb-vnext-protocol-v1`; no implicit legacy protocol compatibility. |

Legacy and vNext must never share a writer or mutable runtime path. Until an explicit activation EU passes, vNext is build-only and its writer remains disabled.

## First Execution Unit now

`M1a — Runtime Identity + Composition Manifest` is the first existing EU, applied to vNext (not a new EU).

Minimal JIT pack:

- Basis: clean `bdb-vnext` at `4998aa16ff68d728637d09639ac79ced886393f6`; legacy `main` is out of mutation scope.
- Invariant: vNext generation, components, protocol, paths and registrations are deterministic and comparable; mismatch fails closed.
- Authority: the manifest is a read-only observation/expectation contract, never an activation pointer or lifecycle store.
- Scope: additive schema, deterministic builder, pinned public-key Browser identity, bounded read-only bundle digests, path/identity collision checks, sanitized query/export and targeted architecture tests.
- Reuse: R0a canonical JSON, semantic digest and sanitization utilities; existing source version/manifest facts where useful.
- Out: provider rewrite, private signing-key custody, Control DB creation, runtime start, Browser installation, Native Host registration, legacy reconciliation, drain or cutover.
- Validation: deterministic identity derivation, bundle tamper/overlap and import-side-effect tests, CLI/packaging smoke plus R0a regression; no legacy runtime access.
- Stop check: no Architecture Freeze conflict, authority overlap, foreign-state overlap or legacy mutation is required. `READY=YES`.

After M1a, use the existing queue for target-only implementation. Production activation dependencies remain gated; the final legacy drain/freeze/cutover remains last.

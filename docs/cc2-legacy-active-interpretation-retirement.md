# CC2 — Legacy Active Interpretation Retirement

Status: source candidate
Base: `4e0b0e3528718e79df043aa565f0c7c81819b4ed` (canonical M9a merge)
Milestone boundary: after M9a `PASS_CLOSED`, before M9b generation switch

## Purpose

CC2 removes legacy runtime heuristics from the active/default Control Center semantics after M9a froze legacy ingress and writers. It does not activate vNext, delete historical legacy source bytes, or convert archived legacy records into canonical vNext state.

## Canonical boundary

After M9a:

- legacy runtime/store state is passive history and is never interpreted as current/active Control Center authority;
- the default GUI continues to project only `bdb_vnext.control_center_query`;
- canonical query failure is rendered as vNext `DEGRADED` with `legacy_fallback=false`;
- writer and activation remain OFF until M9b;
- historical legacy source may remain physically present for later cleanup, but is unreachable from the active Control Center entrypoint.

## Retired CLI route

`--legacy-control-center` is retained only as a compatibility tombstone. Selecting it:

1. returns the deterministic error code `legacy_control_center_retired_cc2`;
2. exits before PySide or legacy GUI/runtime modules are imported;
3. states that legacy is archive-only/non-authority;
4. performs no mutation and cannot activate vNext;
5. never falls back to legacy semantics.

`--workspaces-root` likewise remains parseable only for CLI compatibility and is ignored as an active semantic source.

## What CC2 deliberately does not do

CC2 does not:

- physically delete legacy Journal/GUI/source/history bytes;
- restore, drain, acknowledge, execute, or reinterpret archived legacy commands;
- create a new archive UI or new authority layer;
- enable Browser/Native vNext generation;
- enable vNext writer/intake;
- deploy anything to Shopify.

Physical cleanup remains a later M9b/M12 concern. vNext activation remains exclusively an M9b concern.

## Validation contract

Focused validation must prove:

- the retired legacy flag fails closed before legacy/PySide runtime imports;
- active Control Center sources contain no `ObservabilityReader`, `OperatorApi`, `bdb_bridge`, legacy window/service/tray imports, or equivalent active legacy projection route;
- an absent canonical vNext store projects OFF with writer/activation OFF and `legacy_fallback=false`;
- canonical query failure stays vNext `DEGRADED` and has no hidden legacy fallback;
- legacy historical source files may remain on disk but are not imported by the active entrypoint;
- the CC2 change does not authorize vNext activation or mutation.

## Completion evidence

CC2 can be called DONE only after the focused source tests pass on the exact branch head and any required GUI smoke confirms the default vNext shell still starts read-only. M9b remains blocked until that evidence is accepted and CC2 is merged to canonical `bdb-vnext`.

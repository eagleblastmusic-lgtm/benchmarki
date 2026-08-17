# M9b generation switch — inert source candidate

Status: **SOURCE CANDIDATE / NOT ACTIVATED**

This tranche is derived from canonical `bdb-vnext` after CC2. It deliberately
creates no installation, registry mutation, Browser migration or runtime
activation on import/checkout.

## Frozen M9b invariants

- M9a `PASS_CLOSED` is prerequisite evidence only; it never grants M9b authority.
- Exact target identities are `bdb-vnext-g1`, `bdb-vnext-protocol-v1`,
  `mopnolkjddkmgojfjkenjobehhmmklll` and `com.bartosz.dev_bridge.vnext`.
- Legacy `com.bartosz.dev_bridge`, receipt/spool/Session/Command paths are never
  fallback for a vNext request.
- Browser can retain client outbox, UI/cursor/locator/presentation/context/Resume
  state but cannot own Task/Work/retry/recovery/delivery lifecycle authority.
- An uncertain Browser send is recovered by canonical lookup using the same
  `submission_key` and request digest. Browser never generates a replacement key
  for retry.
- External activation is two phase: `CLIENTS_VERIFIED -> ACTIVATING -> ACTIVE`.
  Production Native submission requires `ACTIVE` and an enabled M3c canonical
  intake gate.
- A crash after enabling the internal intake but before final `ACTIVE` remains
  externally fail closed because Native submission checks the external record.
- After first accepted vNext Task/state-forward boundary, recovery is
  roll-forward only. Legacy can remain only as passive evidence.

## Source included in this checkpoint

- `bdb_vnext/m9b_activation.py`: exact external activation identity and two-phase
  fail-closed transition.
- `bdb_vnext/m9b_native_host.py`: dedicated vNext Native Messaging transport;
  no legacy native host, aliases, receipt store, spool or wake path.
- `browser_extension_vnext/`: pinned MV3 target package with exact public key,
  canonical admission outbox/lookup recovery and thin ChatGPT content adapter.
- focused tests for activation fault windows, Native routing and Browser
  architecture/extinction properties.

## Explicitly not done yet

- no Windows Native Host install/registry mutation;
- no Chrome extension install/reload/migration;
- no `CLIENTS_VERIFIED` record on the user runtime;
- no M3c intake enable;
- no external `ACTIVE` record;
- no deletion of the historical `browser_extension/` source tree;
- no Control Center ON projection;
- no duplicate-route negative test against the installed system;
- no Browser parity/restart/presentation benchmark;
- no M9b DONE claim.

## Next gate

Run syntax + focused tests on the exact source head. Only after that passes may
an effectful Windows M9b installer/preflight/apply candidate be added. The
future apply must revalidate M9a freeze, CC2 basis, exact source/tree/bundle
identities and installed registry/extension state under a fresh fence before
any product activation.

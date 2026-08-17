# M9a legacy ingress freeze — execution contract

Status: **SOURCE CANDIDATE — local Windows `PASS_CLOSED` required before M9a closure**.

## Accepted pre-freeze evidence

Corrected source-compatible Windows probes established the following accepted identities:

- `bartosz-dev-bridge`: `sha256:20e3a41aa8aab821db9e369cbb4f3e8ea3039cf86ff95c7d54cfaa3b92ec2a45`
  - no live service PID;
  - no wake event;
  - Native Host DISARMED;
  - one `ACKNOWLEDGEMENT_ONLY_ON_SERVICE_RESTART` subject;
  - one spool envelope correlated with that unresolved ACK-only command;
  - Native request store is `VALID_LEGACY_COMPAT_SHAPE`;
  - promoter `promotions/` is absent, not corrupt.

- `bdb-self`: `sha256:21158c3a0742093f40b16ace2a23dbc046f96b5563ac61efea51a2593a23e59c`
  - no live service PID;
  - no wake event;
  - Native Host DISARMED;
  - 191 `ACKNOWLEDGEMENT_ONLY_ON_SERVICE_RESTART` subjects;
  - one `MANUAL_ONLY` subject;
  - 14 `NEW_INGRESS_IF_SERVICE_RESTARTS` spool envelopes;
  - Native request store is `VALID_LEGACY_COMPAT_SHAPE`;
  - promoter is `VALID_LEGACY_COMPAT`, with 85 historical unsequenced and 3 sequenced promotion receipts.

There is no observed `CLAIMED`, `EXECUTING`, `EFFECT_RECORDED`, or `RESULT_STAGED` capability that authorizes replaying a write during M9a.

## Effect boundary

`scripts/run_m9a_freeze.py` is Windows-only and requires explicit `--apply`.

Before the first ingress-closing mutation it must:

1. require the vNext runtime root to remain absent;
2. acquire the exact legacy byte-range `bridge.instance.lock` for every profile;
3. re-run the source-compatible blocker probe under those locks;
4. require the fresh probe digests to match the accepted identities above;
5. reject any live PID, wake event, armed Native Host, truncated evidence, unknown/write-capable recovery class, unsafe spool class, or incompatible receipt/promoter state;
6. fail on any HKLM Native Messaging binding or any HKCU binding that does not point exactly to the expected legacy manifest;
7. copy and hash-verify the legacy runtime trees, bridge configs, Native config/manifest/state stores, and deployed Native Host executable into a persistent archive candidate.

Only after the archive candidate is verified may the executor move forward.

## One-way freeze

The effect sequence is deliberately roll-forward-only:

1. unregister the verified HKCU Chrome/Edge `com.bartosz.dev_bridge` Native Messaging bindings;
2. freeze the live `native-host.json` path by same-volume rename;
3. freeze the live Native Messaging manifest path by same-volume rename;
4. freeze both live `bridge-config.json` paths by same-volume rename;
5. quarantine each live spool inbox by same-volume rename without executing any envelope;
6. keep both legacy instance locks held through a bounded zero-new-write observation interval.

The executor does not delete the extension, Journals, receipts, promotion receipts, results, worktrees, frozen configs, or quarantined spool bytes. Physical deletion belongs to later retirement work.

No automatic rollback or re-registration is permitted after the first ingress-closing mutation. A failure after that point is `PARTIAL_FREEZE_STOP` and must be reconciled by continuing the freeze, never by silently reopening legacy ingress.

## PASS_CLOSED requirements

M9a can close only when the emitted `bdb-vnext-m9a-freeze-report-v1` proves all of the following:

- archive candidate exists and its `bdb-vnext-m9a-archive-manifest-v1` hashes verify;
- both legacy instance locks were acquired before service startup could proceed;
- fresh probe identities matched the accepted evidence;
- no supported Native Messaging binding remains;
- the live Native config/manifest paths are absent/frozen;
- both live bridge-config paths are absent/frozen;
- both live spool inboxes are absent/quarantined;
- no legacy wake event appears;
- the bounded post-freeze observation sees zero writes to retained live legacy state;
- the vNext runtime root remains absent;
- `vnext_activation_allowed=false` and `m9b_allowed=false`.

A `PASS_CLOSED` result closes M9a only. It does not authorize M9b or activate a vNext writer. The next canonical unit is CC2.

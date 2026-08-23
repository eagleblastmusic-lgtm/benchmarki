# AUTO loop runtime reliability — 0.2.4

## Scope

This change closes four failures observed during the local `Kalkulator 2` workflow:

1. a Native Host response could stop at `accepted` even though the command continued asynchronously;
2. terminal pre-mutation states such as `state_mismatch` did not produce a durable browser-facing result;
3. Windows `core.autocrlf=true` could make per-file SHA-256 values differ between the source checkout and a new isolated worktree;
4. AUTO could insert `BDB_AUTO_RESULT` into the ChatGPT composer and report `sent=true` without proving that ChatGPT consumed the message.

## Accepted and pending command results

`background_async_result.js` wraps the existing submission path. An `accepted` or `pending` response is no longer treated immediately as `needs_user`.

The extension:

- parses the exact command ID;
- uses the already allowlisted Native Host `result` action;
- polls in bounded 30-second windows;
- stops after four result requests;
- preserves required-promotion verification after the durable result is observed;
- returns a visible bounded pending state when the polling budget is exhausted.

No additional host permissions or browser permissions are introduced.

## Terminal results before mutation

The Bridge can reach a valid terminal state before creating an operation checkpoint, for example:

- `state_mismatch`;
- `stale_revision`;
- `policy_denied`;
- `expired`;
- `rejected`.

`runtime_hardening.py` reads these durable states from the local Journal and publishes a compact result into the normal Direct Lane result path. The result:

- identifies the exact command and terminal state;
- contains no changed files;
- does not claim a rollback that did not happen;
- marks the outcome as requiring user or caller reconciliation;
- lets Native Host return `completed` instead of timing out indefinitely.

The existing successful-result staging and promotion path remains unchanged.

## Platform-independent file hashes

New isolated worktrees are created with command-local Git settings:

```text
core.autocrlf=false
core.eol=lf
```

This does not change the user's global Git configuration.

For a clean source checkout, `workspace_context` now exposes content and SHA-256 values derived from immutable Git blob bytes rather than platform-dependent working-tree bytes. Promotion hashes returned through context are normalized to the same Git-blob representation, while the original physical checkout hashes remain available as diagnostic metadata.

This makes the following contract stable across Windows and Unix:

```text
workspace_context file SHA
=
expected_sha256 in a new worktree
=
final promoted Git blob SHA-256
```

## Confirmed AUTO send

`content_auto_send.js` replaces fire-and-forget clicking with confirmed submission:

- wait longer for the exact enabled send button;
- click only while the exact AUTO marker remains in the composer;
- require the composer to consume the marker before reporting success;
- retry at most three times;
- return `send_not_confirmed` and fall back to assisted mode if the marker remains.

A successful click is therefore no longer confused with a successfully submitted ChatGPT message.

## Safety boundaries

- AUTO remains opt-in. Total iteration count and milestone-run duration are not limits; individual Native operations retain their bounded timeout and safety contracts.
- The replay guard remains keyed by `<loop_id>:<iteration>`.
- No arbitrary shell capability is added.
- No allowlist or path scope is expanded.
- No automatic merge or deployment is introduced.
- Promotion remains successful-result-only and fast-forward-only.

## Regression tests

The added tests cover:

- accepted → pending → completed Native Host polling;
- completed-response passthrough without extra polling;
- synthetic-click retry and composer-consumption confirmation;
- bounded fallback when a message is not consumed;
- terminal `state_mismatch` result construction;
- forced LF worktree creation arguments;
- canonical Git-blob snapshot and promotion hashes.

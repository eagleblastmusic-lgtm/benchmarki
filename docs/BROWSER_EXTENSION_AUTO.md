# Browser extension — AUTO mode

AUTO is an explicit opt-in layer above the default ASSISTED mode.

## Dual authorization

An action executes automatically only when both conditions are true:

1. the extension popup has `AUTO enabled`;
2. the action contains:

```json
{
  "automation": {
    "mode": "auto",
    "loop_id": "repair-cart-001",
    "iteration": 1
  }
}
```

Without both conditions, the normal `BDB: Wykonaj` button remains.

## Milestone-scoped state

The background worker stores per-tab/per-loop state in `chrome.storage.session`:

- start time;
- last accepted iteration;
- last command ID;
- running or terminal status.

AUTO has no total iteration ceiling and no total milestone-run duration ceiling. Iterations must arrive exactly in sequence. Duplicate, skipped or already terminal loops do not execute. Legacy total-run limit fields are ignored and are never used as limits.

A separate durable replay guard is stored in `chrome.storage.local` before Native Host submission. It prevents the same `<loop_id>:<iteration>` from executing again after a page reload, service-worker restart or browser restart. The guard retains at most 512 recent entries. A replay collision, storage failure or uncertain interrupted attempt falls back to ASSISTED. The transport may resend the same durably receipted `request_id` once to recover its original result, but AUTO never creates a second independent effect automatically.

Version 0.4.0 also stores a bounded durable task ledger and up to sixteen compact result checkpoints. If Chrome or the service worker stops after Native completion but before ChatGPT consumes the result, the same action panel recovers the exact checkpoint and delivers it without submitting another Native effect. The popup can explicitly stop or resume the latest task. Since 0.4.5, a replacement panel waits through the bounded Native operation and automatically claims the checkpoint. Explicit resume first recovers an undelivered result in the active ChatGPT tab; Project Memory/Execution remains the authority for the current task and milestone. Resume never enables global AUTO.

For visual changes, `bdb-acceptance-v1` may set `manual_visual_confirmation_required: true`. After all automated checks pass, the controller returns `needs_confirmation` and stops AUTO with `needs_user`; it does not infer that the rendered UI is correct from compilation or source searches alone. The resulting guidance is `await_user_visual_feedback`, which means that ChatGPT asks in normal prose and must not create a `manual_visual_confirmation` BDB operation.

If the user reports that the visual result is wrong, the next sequential action in the same loop may set `automation.continue_after_user_feedback: true`. Version 0.4.7 accepts that flag only when the preceding delivered checkpoint has `acceptance.status: needs_confirmation`. It reopens the same task without a total-run limit. The flag cannot resume policy failures, undelivered results, unrelated `needs_user` states or a different loop.

One user task uses one `loop_id` (up to 128 safe characters) and monotonically increasing iterations. A fresh `loop_id` is reserved for a new user task, not every BDB action. Short identifiers under 48 characters are recommended for readable result markers.

The 0.4.0 action compiler normalizes unsafe model-generated identifiers before the replay/state machinery sees them and returns the effective ID in `decision.compiler`. Missing iterations are filled from the durable task ledger. This prevents formatting mistakes from silently forcing ASSISTED while retaining a traceable original task identity.

## Milestone orchestration

AUTO may keep iterating until the current task reaches machine acceptance. When the canonical response reports a completed task and a runnable next task in the same milestone, the browser continues with that task one at a time. Selection/order is supplied by Project Memory/Execution; the Browser does not invent or parallelize work. When all tasks in the active milestone are completed/skipped, the response is terminal `milestone_completed` and the next milestone requires an explicit user start. Gate, open-question, prerequisite, manual review, policy, stale, reconciliation and cancellation stops remain authoritative.

`inspect_bundle` should consolidate repository state, up to eight searches and relevant reads. A single `multi_file_patch` should then apply one coherent atomic change and its fixed test profile. Independent searches inside one inspection run concurrently against the same immutable Git commit and reuse an in-process cache keyed by `HEAD`.

## Automatic continuation

After an exact durable `completed` result, and only when no terminal status is found:

1. the content script requires the ChatGPT composer to be empty;
2. it writes a unique `BDB_AUTO_RESULT:<loop_id>:<iteration>` marker and `BDB_RESULT`;
3. it requires the exact `button[data-testid='send-button']` inside the same form;
4. it verifies the marker is still present and the button is enabled;
5. it performs one click.

Any mismatch leaves the result visible and falls back to ASSISTED. The extension never searches broadly for buttons by label and never overwrites an existing draft.

## Hard stops

Recursive bounded result inspection stops AUTO for:

- `DONE`;
- `NEEDS_USER`;
- `POLICY_DENIED`;
- `MANUAL_RECONCILIATION_REQUIRED`;
- `FAILED`;
- `CANCELLED`;
- `ABORTED`.

Replay guard rejection, missing composer, non-empty draft, missing exact send button or a non-recoverable extension/native error also stops automatic continuation. Individual Native requests, commands/tests, payloads and result transfers remain bounded by their technical timeouts and size limits. Before iteration 1, AUTO checks the Native Host arm state. A disarmed host returns `native_host_disarmed` without consuming the iteration or creating a replay claim, so arming and retrying the same action is safe. A bounded `internal_error` remains a running AUTO planning loop: the exact receipted request is retried first, and the returned diagnostic can then drive a focused inspection without requiring a manual click.

Before `replace_exact_and_test`, the browser preflight searches the exact old text across the allowed repository scope. If it exists in more than one location, no mutation is submitted. AUTO returns `scope_incomplete` with candidate paths and continues the same loop so the next action can use one reviewed `multi_file_patch` for all relevant runtime sources.

`replace_exact_and_test` also accepts a bounded `replacements` list with 1–16 `{old,new}` items for related changes in one file. The executor applies them atomically in order, requires exactly one match for each item, treats LF and CRLF as equivalent for multiline matching, and preserves the target file's line endings. This avoids oversized contextual replacements and extra read/edit iterations for paired template and runtime-setting values.

Terminal diagnostics retain the specific sanitized `error_code` and detail (for example `replace_mismatch`) in the diagnostic event and task ledger `last_error`; a replayed result is labelled separately instead of being counted as another executed mutation.

Flight Recorder v1 derives bounded local timing evidence from the same sanitized diagnostic stream. Diagnostics expose `bdb-flight-recorder-v1` with per-stage sample counts plus p50/p90/p99/max timing summaries and a compact critical-path projection. Action/AUTO events also retain sanitized repository alias, command identity and base/result Git evidence when the runtime response provides them. Source code, action payloads and credentials remain excluded. This is the first measurable baseline; later protocol stages add task/attempt identity and finer native, queue, validation, promotion and delivery timings.

AUTO does not weaken Native Host ARMED TTL, repository aliases, Direct Lane policy, fixed profiles, worktree isolation, checkpoint, rollback or recovery.

## Acceptance criteria

A mutating action may include machine-checkable completion rules:

```json
{
  "acceptance": {
    "schema": "bdb-acceptance-v1",
    "result_status": "success",
    "changed_files_include": ["sections/section.liquid"],
    "promotion_required": true,
    "tests_required": true,
    "search_assertions": [
      {
        "query": "unwanted placeholder",
        "path": "sections/section.liquid",
        "min_matches": 0,
        "max_matches": 0,
        "case_sensitive": true
      }
    ]
  }
}
```

The result contains `bdb-acceptance-result-v1` with `passed` or `unmet`. An unmet assertion is deliberately nonterminal so ChatGPT can prepare one focused repair in the same bounded AUTO loop. A passed result recommends `complete`.

Before a mutating request reaches Native Host submission, the browser preflight validates the acceptance contract itself. It rejects unsupported schemas and keys, impossible `changed_files_include` paths, oversized assertion lists, malformed search bounds, policy-forbidden assertion paths and acceptance objects that contain no machine-checkable criterion. These failures use structured client-preflight details with `effect_started: false`, so impossible completion rules cannot consume a mutation attempt and fail only after execution.

## Adaptive AUTO result transport

Version 0.4.7 uses one consistent set of composer budgets:

- 12 KiB is the preferred result target;
- 16 KiB is the hard ceiling when the current contenteditable composer supports one-shot `replaceChildren` insertion;
- 4 KiB remains the hard ceiling for the legacy insertion fallback.

`inspect_bundle` progressively selects `rich`, `compact`, `tight` or `minimal` output. It removes repeated metadata and shortens excerpts before dropping repository paths, query totals or line locations. The sender passes its actual fast/legacy ceiling into the formatter, so a result accepted by the formatter cannot be rejected solely because another layer uses a smaller byte limit. This avoids the former `execCommand` renderer stall without forcing every reconnaissance result into the 4 KiB fallback.

## Cache, deduplication and risk

- read cache entries live in `chrome.storage.session`, expire after two minutes and are reused only after a fresh Native context proves that `HEAD` is unchanged;
- an exact mutating action is deduplicated for five minutes only when the current source commit equals the cached promoted commit;
- cache entries and durable checkpoints are size-bounded;
- `delete_file`, `move_file` and `rename_file` are classified as high risk and always return `high_risk_requires_assisted` in AUTO;
- ordinary allowed replacements/creates remain bounded mutations behind the existing preflight, fixed profile, checkpoint and promotion gates.

## Health, shadow mode and diagnostics

The popup provides:

- a version/Native Host handshake;
- stale ChatGPT content-script detection;
- shadow mode, which predicts the decision without execution;
- an explicit end-to-end AUTO test using read-only `workspace_context` and the real ChatGPT composer;
- the latest durable task with stop/resume controls;
- cache clearing;
- a sanitized ZIP containing bounded events, aggregate counters and task metadata.

Diagnostics never include action payloads, source code or credentials. Each event carries the effective loop, iteration, operation, reason, duration, extension version and trace ID when available.

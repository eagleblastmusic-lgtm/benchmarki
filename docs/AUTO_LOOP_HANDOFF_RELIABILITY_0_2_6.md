# AUTO loop handoff reliability — 0.2.6

## Scope

This change closes two failures observed during the .NET population-variance repair pilot:

1. the next AUTO action could be rendered while the canonical loop counter still exposed an earlier expected iteration, producing a transient `non_sequential_iteration` and requiring a page refresh;
2. an AUTO result could remain in the ChatGPT composer after a synthetic send-button click, requiring the user to press Enter manually.

The Bridge execution path itself remained correct: the intentionally failing .NET patch was fully rolled back, the corrected patch passed all tests, and only the green result was promoted. The failure boundary was the browser handoff between consecutive ChatGPT turns.

## Bounded decision catch-up

`content_auto_retry.js` wraps only the content-script AUTO decision call.

When the background worker returns:

```text
executed = false
reason = non_sequential_iteration
expectedIteration <= action.automation.iteration
```

the content script performs a bounded retry:

- at most 24 decisions;
- 250 ms between decisions;
- no direct access to Chrome storage;
- no Native Messaging request;
- no replay-guard changes;
- no retry for terminal, disabled, stale, or already-processed decisions.

The background worker remains the sole owner of AUTO opt-in, canonical loop state, and `<loop_id>:<iteration>` replay claims. Total milestone-run iteration and duration are not limits; only individual operation timeouts and safety guards remain bounded.

## Confirmed multi-strategy send

`content_auto_send.js` now attempts the exact continuation through three bounded strategies:

1. the current enabled send button;
2. `form.requestSubmit()` using the current form and button;
3. an Enter-key event sequence on the current live composer.

After every attempted strategy, success requires one of two observable outcomes:

- the exact `BDB_AUTO_RESULT:<loop_id>:<iteration>` marker appears in a submitted user message; or
- the live composer consumes the exact marker for three consecutive polls.

If none of the strategies is confirmed, AUTO returns `send_not_confirmed` and falls back to assisted mode. It never reports success solely because `button.click()` returned.

## Safety boundaries

- AUTO remains local, explicit, and disabled by default.
- No browser permissions or host permissions are added.
- No allowlist, test-profile, rollback, or promotion rule changes.
- The manual assisted button still uses the original submission path.
- No automatic Git push, merge, release, or deployment is introduced.

## Regression coverage

The focused runtime tests cover:

- a transient counter gap that catches up and executes;
- a stale action whose expected iteration is already higher and is not retried;
- disabled AUTO without retry;
- successful button-click submission;
- successful `requestSubmit()` fallback;
- successful Enter-key fallback;
- confirmation through a submitted user message;
- composer replacement after insertion;
- bounded `send_not_confirmed` fallback when all strategies fail.

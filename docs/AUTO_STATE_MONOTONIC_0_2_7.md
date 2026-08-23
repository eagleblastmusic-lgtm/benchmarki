# AUTO canonical state monotonicity — 0.2.7

## Observed failure

During the strict .NET sample-variance acceptance run, iteration 3 was rendered after iteration 2 completed, but the content script exhausted its bounded `non_sequential_iteration` retry and exposed an ASSISTED button. The popup still reported `lastIteration = 2` and `expectedIteration = 3`.

The run therefore failed the strict one-message/no-touch criterion. No source mutation had started: the completed actions were `workspace_context` and two `open_read` operations, and the workspace remained at revision 0.

## Root cause

`background_entry.js` synchronized the canonical AUTO state before every decision. Even when a canonical state already existed, it performed a read-copy-write cycle only to refresh diagnostic metadata.

Two worker invocations can overlap:

1. invocation A reads canonical `lastIteration = 1`;
2. invocation B completes iteration 2 and writes `lastIteration = 2`;
3. invocation A writes its stale copy and regresses the canonical state to iteration 1;
4. iteration 3 is rejected as non-sequential.

A content-side retry cannot reliably repair a counter that is being regressed by a stale background write.

## Fix

The synchronization layer is now monotonic:

- an existing canonical state is never rewritten by the migration/synchronization path;
- obsolete tab-scoped keys may still be removed;
- legacy state is written only when no canonical state exists;
- the canonical key is re-read immediately before a legacy migration write;
- `background.js` remains the sole writer that advances normal AUTO execution state.

Replay claims, AUTO opt-in, Native Messaging, rollback and promotion behavior are unchanged. AUTO now follows the canonical current-milestone cursor without a task-level time or iteration ceiling.

## Windows helper-process visibility

The same operator run exposed a separate usability defect: Control Center could launch synchronous PowerShell or Python helper commands from a GUI process without suppressing their console subsystem. Windows could therefore display a blank `python.exe` or PowerShell window during Start, Stop, Status or Re-arm.

`SubprocessCommandRunner` now adds Windows `CREATE_NO_WINDOW` only when `os.name == "nt"`:

- no shell is introduced;
- stdout and stderr remain captured in temporary files;
- timeout and exit-code handling are unchanged;
- POSIX execution receives no Windows creation flags;
- long-lived Bridge and promoter logging remain unchanged.

The change affects only helper-process presentation. It does not weaken process, Native Host, allowlist, profile, rollback, promotion or authorization gates.

## Regression coverage

`tests/test_browser_auto_state_monotonic_runtime.py` deterministically injects the race:

1. synchronization reads a stale canonical snapshot at iteration 1;
2. the live store advances concurrently to iteration 2;
3. iteration 3 is submitted;
4. the test requires iteration 3 to execute and the final canonical state to become 3;
5. the test rejects any synchronization write that restores iteration 1.

The test fails against the preceding read-copy-write implementation and passes only when canonical state cannot regress.

`tests/test_operator_runner.py` additionally verifies that:

- Windows helper commands receive `CREATE_NO_WINDOW`;
- `shell=False`, redirected output and bounded timeout remain in place;
- non-Windows execution receives no Windows-only creation flag.

## Operator acceptance

After CI and merge:

1. update the clean local `main` by fast-forward only;
2. rebuild/reinstall Control Center from the merged source so the windowless runner is active;
3. reload the unpacked extension and confirm version `0.2.7`;
4. reload the ChatGPT tab once before starting a new loop;
5. Start and arm the Control Center;
6. confirm that Start, Status and Re-arm do not display a console window;
7. enable AUTO with bounded limits;
8. run a fresh unique loop without clicking, pressing Enter, sending another message, or refreshing the page.

No Git push, merge, release or deployment capability is added.

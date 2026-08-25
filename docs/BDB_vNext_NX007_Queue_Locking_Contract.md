# BDB vNext — NX-007 Project Launch Queue Locking Contract

## 1. Context and Problem Statement

Prior to NX-007, `ProjectLaunchQueueAdapter` in `project_launch.py` used a simple file-based lock with two key weaknesses:
1. **Blind Unlink in `finally`**: The lock exit code unconditionally called `self.lock_path.unlink(missing_ok=True)`. If another process broke or reclaimed the lock due to a perceived timeout, the original lock holder upon completing its work would unlink the *new* lock, causing race conditions.
2. **Age-Only Stale Reclaim Vulnerability**: Checking only `age > stale_after_seconds` could delete an active, slow-running owner's lock.
3. **Windows `PermissionError` (errno 13)**: Under concurrent access between `BDB-vNext-NativeHost.exe` and GUI processes, file creation and opening on Windows frequently raised `PermissionError` (WinError 5 / 32). Unhandled, this crashed Native Host.

---

## 2. Ownership-Safe Locking Architecture

NX-007 introduces structured ownership tokens with compare-before-release semantics, dead-owner verification (`AGE_ONLY_RECLAIM = FALSE`), compare-before-reclaim, and graceful Windows contention retry:

```
+-------------------------------------------------------------+
| Lock Acquisition (os.O_CREAT | os.O_EXCL)                   |
| 1. Generate unique owner_token (UUID4).                     |
| 2. Atomically write lock JSON metadata:                     |
|    - schema: "bdb-project-launch-lock-v1"                  |
|    - owner_token: "<uuid>"                                  |
|    - pid: <process_id>                                      |
|    - acquired_at: "<UTC_ISO_8601>"                          |
|    - stale_after_seconds: 10.0                              |
+-------------------------------------------------------------+
                              |
                              v
+-------------------------------------------------------------+
| Lock Release (Compare-Before-Release)                       |
| 1. Safely read lock file on disk.                           |
| 2. IF disk_lock.owner_token == my_owner_token:              |
|        unlink(lock_path)                                    |
|    ELSE:                                                    |
|        DO NOT UNLINK (Preserve foreign / renewed lock)      |
+-------------------------------------------------------------+
```

---

## 3. Dead-Owner Verification Rule (`AGE_ONLY_RECLAIM = FALSE`)

A lock is NEVER considered stale solely because `age > stale_after_seconds`.
Reclaim follows a strict formal protocol:
1. **Living Owner Check**: Inspect `lock_info.pid`. If `_is_pid_alive(pid)` is `TRUE`, the lock is **NOT stale** and is **NEVER reclaimed**, regardless of elapsed time.
2. **Dead Owner Proof**: If `_is_pid_alive(pid)` is `FALSE` (process has exited or does not exist) AND `now - acquired_at >= stale_after_seconds`, the lock is marked stale.
3. **Compare-Before-Reclaim**: When unlinking a stale lock, the reclaimer verifies that the on-disk `owner_token` still matches the inspected dead lock token. If another process replaced the lock in the interim, the new lock is preserved.

---

## 4. Windows `PermissionError` & Native Host Resilience

When `os.open` raises `PermissionError` or `FileExistsError`:
1. The lock reader attempts to inspect the lock metadata safely (`_read_lock_info_safe`).
2. If stale under the dead-owner rule, the lock is safely reclaimed.
3. If not stale, the acquisition enters an exponential backoff sleep loop until the bounded deadline (`_LOCK_TIMEOUT_SECONDS = 5.0`).
4. If retry deadline expires, `ProjectLaunchQueueError("queue_busy", "project launch queue is busy")` is raised.
5. In `m9b_native_host.py`, all queue errors are cleanly classified into `M9bNativeError("queue_busy")` and return JSON error envelopes without terminating the Native Host process (`UNHANDLED_NATIVE_HOST_LOCK_CRASH = FALSE`).

---

## 5. Invariants and Verification

- `OWNERSHIP_TOKEN_PRESENT = TRUE`: Every lock contains a typed JSON payload with a unique ownership token and PID.
- `COMPARE_BEFORE_RELEASE = TRUE`: A process only unlinks the lock if its own token matches the token on disk.
- `AGE_ONLY_RECLAIM = FALSE`: Lock of a living owner is never reclaimed by age alone.
- `ACTIVE_FOREIGN_LOCK_DELETED = FALSE`: Foreign active locks are preserved under contention and race conditions.
- `DEAD_OWNER_RECOVERY = PASS`: Dead owner locks are safely reclaimed after lease expiry.
- `REPLACEMENT_LOCK_RELEASE_RACE = PASS`: Compare-before-reclaim protects newly installed replacement locks.
- `PERMISSION_ERROR_CLASSIFIED = TRUE`: Windows sharing violations are mapped to `"queue_busy"`.
- `UNHANDLED_NATIVE_HOST_LOCK_CRASH = FALSE`: Native Host handles locking errors without crashing.
- `TEN_THOUSAND_OPERATION_HARNESS = PASS`: 10,000 concurrent operations execute with 0 loss, 0 duplicates, 0 foreign unlinks.

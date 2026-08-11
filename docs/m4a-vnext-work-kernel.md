# M4a vNext — WorkItem Kernel Substrate

M4a is a build-only, isolated vNext kernel.  It does not activate production
work routing and it does not extend or mirror the legacy Journal.

The authority chain is:

```text
canonical M3 Submission → Task
          ↓ accepted Task identity
M4a WorkKernelStore (one transactional writer)
          ↓
canonical WorkItem query v1
```

`WorkKernelStore` writes only `control/m4a-work-kernel.db` below the supplied
vNext runtime root.  It requires an already accepted canonical M3 Task and
rejects a missing or foreign task.  The store is explicit `SHADOW_ONLY`, has
no legacy import or dual-write mode, and requires a non-overlapping legacy
runtime root.

The current WorkItem row owns only lifecycle disposition and `state_version`.
Runs preserve execution identity, Waits preserve explicit durable blocking
reasons, leases/fences protect ownership, resource claims coordinate a bounded
internal resource, and TransitionFacts preserve causal facts in the same
transaction as the current-row mutation.  Run outcome and effect certainty
remain separate dimensions; facts are not a replay-complete event store.

Every supported mutation enters through `WorkKernelStore`: create, lease,
resource claim, Run start/finish, and Wait open/resolve.  Browser and Native
remain transport/recovery surfaces, and scheduler/executor mechanics have no
direct WorkItem writer.  Stale state versions, leases or fences fail closed.
Duplicate requests are deterministic replay/no-op or typed conflict.  Before,
during and after-commit fault points prove rollback and state-forward recovery.

The M4a provider is present in the composition manifest as
`reserved_disabled`; it is not a production route.  Runtime, writer and
external activation remain `OFF / OFF / OFF`.  M4b, CC0, M4f and legacy
cutover are outside this unit.

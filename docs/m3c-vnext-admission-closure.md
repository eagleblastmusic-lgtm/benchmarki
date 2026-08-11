# M3c vNext — Internal Admission Authority Closure

M3c closes admission inside the inactive, isolated vNext generation. It is
not a product activation and it does not stop or modify the operational
legacy generation.

The supported internal path is:

```text
Browser durable outbox (recovery only)
  -> Native protocol transport (transport and lookup only)
  -> canonical Submission -> Task transaction
  -> canonical query
```

`CanonicalVNextAdmissionAuthority` is the only supported writer that can
create a vNext Task. It reuses the already validated M3a atomic transaction
inside a dedicated vNext Control Store root. The M3c control marker records
the vNext protocol generation, canonical writer identity, build-only mode,
and the absence of legacy import or alternate admission.

The Browser outbox may durably preserve a submission key, request digest and
transport state before send, and it may recover a lost ACK through canonical
lookup. Native may perform generation checks, submission transport and
lookup. Neither component can create or certify a Task independently. An
unsupported protocol fails closed; there is no legacy fallback.

The durable M3c kill switch disables new canonical admission while leaving
canonical lookup/read-reconciliation available. It never deletes accepted
Tasks and never reopens an alternate writer. A canonical query binds the
submission key and exact request digest to the stored disposition and Task
identity.

The M3a/M3b shadow fixtures remain available only for their focused regression
tests. They are not reachable from the supported M3c composition. The
post-closure authority scan must report exactly one supported accepting writer
and no supported receipt, spool, Browser-ledger, Session/Command, test-helper
or legacy fallback writer.

Runtime, writer and external activation remain `OFF / OFF / OFF`. M4a
WorkItem lifecycle, production admission, Browser/Native registration and
legacy drain are outside this unit.

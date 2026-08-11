# M3b vNext — Restart-safe Browser Admission (Shadow)

M3b is a test-only Browser MV3/Native Host mechanics layer. The
`BrowserAdmissionOutbox` is durable client recovery state under an explicit
isolated root; it is created and reopened with `shadow=True`, has a pinned
`bdb-vnext-protocol-v1` generation, and has an immutable anchor/config pair.
It cannot be used as a Task or lifecycle authority.

`BrowserAdmissionClient` writes the opaque submission key, canonical request
digest and protocol generation to the outbox before calling the
`ShadowNativeAdmissionBridge`. The bridge delegates to the committed M3a
shadow store for acceptance and lookup; it never creates a second writer.
Canonical ACK or lookup is the only way the Browser marks an outbox item
`ACKED`. A lost ACK therefore recovers the same key and digest, while a retry
is never allowed to allocate a new key. Same-key/different-digest is an
explicit conflict. Unsupported generations fail during capability handshake
and there is no legacy fallback.

The focused tests cover durable-before-send, crash before send, lost ACK,
duplicate transport, same-key replay/conflict, host restart, extension
update/reopen, old-host/new-extension version skew, quota/full, corrupt
outbox, total anchor loss, and the Browser non-authority boundary. Disk-full
injection is not claimed because the fixture has no faithful disk-full
harness. No Chrome package, Native registration, production acceptance route,
runtime writer, or activation was changed or enabled.

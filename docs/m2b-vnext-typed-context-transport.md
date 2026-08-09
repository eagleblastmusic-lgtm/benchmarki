# M2b-vNext — Typed Context Transport

M2b establishes a build-only exact-or-fail chain from an accepted hardened
M2a `COMMITTED` RepoView to a typed Browser/Native transport envelope. It does
not activate the runtime, install the extension, register the Native Host, or
introduce task/submission/work lifecycle state.

## Exact contracts

`ContentRef` preserves the accepted X2 four-field contract exactly:

```json
{
  "type": "...",
  "schema": "...",
  "semantic_digest": "sha256:...",
  "raw_digest": "sha256:..."
}
```

`TypedContextFragment` is defined by
[`bdb-vnext-context-fragment-v1.schema.json`](../schemas/bdb-vnext-context-fragment-v1.schema.json).
It binds:

- `repository_id`, `repository_identity_digest`, `object_format`,
  `commit_oid`, `tree_oid`, and `view_id` from the exact M2a RepoView;
- `fragment_type` and `fragment_schema`;
- the complete four-field `ContentRef`;
- bounded `payload_size_bytes` (maximum `1,048,576`);
- deterministic `fragment_id` over all of the above.

The payload bytes are never reconstructed from a filename or a moving ref.
They are read from the immutable M2b content object addressed by `raw_digest`.

The transport envelope is defined by
[`bdb-vnext-transport-envelope-v1.schema.json`](../schemas/bdb-vnext-transport-envelope-v1.schema.json):

- protocol generation: `bdb-vnext-protocol-v1`;
- protocol version: `1`;
- message kind: `typed_context_fragment`;
- deterministic `message_id`;
- exact fragment metadata;
- exact `payload_length_bytes` and strict base64 payload;
- maximum envelope size: `2,097,152` bytes.

Browser encoding is provided by
`devmaster.bdb.vnext.browser-transport`; Native decoding is provided by
`devmaster.bdb.vnext.native-transport`. Both are read-only adapters in one
testable Python harness. No Browser process, extension installation, Native
Host registration, or user activation occurs.

## Durable acceptance and recovery

`bdb_vnext/content_store.py` contains the isolated M2b substrate. Its
`control/control.db` is a versioned `m2b_accepted_bindings` SQLite store, not
the future canonical Control Store: it contains no Task, Submission, WorkItem,
effect, scheduler, or lifecycle tables.

Acceptance ordering is fixed:

1. publish the complete content object to the same-volume immutable object
   path (`os.link(temp, target, follow_symlinks=False)`; no committed target is
   overwritten);
2. publish and verify the exact ContentRef metadata;
3. verify the bytes and exact RepoView binding, then atomically accept the
   fragment row in SQLite (`BEGIN IMMEDIATE`, `synchronous=FULL`);
4. only an accepted binding may be emitted by the Browser adapter.

Duplicate exact publication converges. An incompatible existing object/ref or
conflicting accepted binding fails closed. An object without an accepted
binding is non-authoritative.

M2b-R2 uses handle-bound reads: the path is checked for symlink/reparse and
regular-file identity, opened with `O_NOFOLLOW` when available, read through
the descriptor, and revalidated against the open handle and pathname after the
read. Publication uses a no-overwrite hard-link create-if-absent primitive.
This is a bounded same-root pathname/reparse defence, not a hostile-admin
sandbox.

The recovery test uses the M1b coordinated backup/restore API with the fixed
M1b subjects (`control/control.db`, optional WAL, `content`, and
`config/bdb-vnext.json`). After restore it reopens M2b storage and verifies
exact RepoView identity, ContentRef, both digests, and bytes. Tamper cases for
wrong ContentRef metadata, missing object, wrong object bytes, and RepoView
binding mismatch all fail closed.

## Provider identity and isolation

M1c remains the only explicit composition root. The current build-only root
binds the tested Browser and Native adapters, plus the existing composition
diagnostic and RepoView providers. Control Store and Control Center remain
`RESERVED`.

Each transport binding contributes deterministic:

- provider contract;
- contract version;
- exact implementation module and qualname;
- explicit implementation revision;
- implementation identity digest derived from that stable, versioned descriptor.

No identity depends on object representation, memory address, import order, or
mutable global state. Runtime, writer, and activation remain `OFF / OFF / OFF`.

## Exact-or-fail matrix

Focused tests cover:

- exact Browser→Native roundtrip;
- deterministic envelope and strict canonical framing;
- wrong generation, unsupported version, legacy generation, unknown kind;
- malformed/truncated/invalid UTF-8/extra-field/trailing payload;
- payload and envelope limits, including boundary and boundary+1;
- wrong fragment type/schema, semantic/raw digest, and RepoView identity;
- missing/unbound/corrupt object and immutable conflicting publication;
- path/reparse or foreign pathname substitution;
- duplicate/conflicting durable binding;
- M1b backup/restore and post-restore logical-binding tamper cases;
- unknown/unavailable provider behavior and deterministic provider identity;
- AST/import-negative and side-effect-free import checks.

M2c Understanding, M2d benchmark, lifecycle/task/submission state, outbox/ACK
ledger, retry/effects, promotion, Control Center, installer, and production
activation remain unstarted.

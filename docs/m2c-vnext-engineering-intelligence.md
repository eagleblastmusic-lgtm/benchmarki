# M2c-vNext — Engineering Intelligence Slice

M2c is a build-only, additive semantic layer. It introduces immutable,
rebuildable records bound to an exact M2a `COMMITTED` RepoView and uses M2b
typed content/transport without creating a new mutable authority.

## Record families

- `RepositoryUnderstandingView` — claim projection with explicit `FACT`,
  `INFERENCE`, `ASSUMPTION`, and `HYPOTHESIS` kinds; coverage, must-see
  categories, unknowns, omissions, contradictions, provenance and
  invalidation predicates.
- `ContextPackage` — categorical quality capsule with requested versus covered
  dimensions, explicit epistemic claim classifications, fragment IDs,
  architecture constraints and on-demand affordances. `COMPLETE`, `PARTIAL`
  and `BLOCKED` are mechanically derived; no token count or quality score is
  authoritative.
- `ContextRequest` — immutable request for a visible gap, bound to the exact
  intent, RepoView and source package. It is not a Task, WorkItem, effect,
  retry, capability grant or lifecycle record.
- `ContextResolution` — immutable linkage from request and prior package to a
  resulting package, added accepted fragments, resolved gaps and remaining
  gaps. Denial/unavailability keeps the gap visible.
- `EngineeringDecision` — concise review/resume evidence containing exact
  intent/RepoView/package bases, claim classifications, alternatives,
  trade-offs, selected option, obligations, uncertainty and revisit triggers.

`IntentBasis.task_id` is opaque caller input. M2c consumes
`task_id`/`intent_revision`/`intent_digest` and never allocates Task IDs,
creates Task tables or owns lifecycle/admission.

## Authority and epistemics

Git object data exposed by exact RepoView remains source authority. Understanding
is a projection/claim view, not source bytes authority. Conflicting claims are
preserved as explicit contradictions; when an exact-source `FACT` conflicts
with a derived claim, the record states `EXACT_SOURCE_WINS` without silently
rewriting either claim. Private chain-of-thought, hidden reasoning traces and
token-by-token reasoning are never stored.

Every record identity is derived from stable canonical fields, including exact
intent/RepoView basis and producer/schema versions. Rebuilding the same inputs
converges; changing a material basis or producer/schema identity changes the
record identity and stale decision applicability is rejected.

FACT claims require `RepoSourceEvidence`, a distinct descriptor that binds a
repo-relative source path and committed tree-entry object ID to an accepted M2b
fragment. `publish_repo_source_evidence()` reads the bytes itself through the
exact M2a `CommittedRepoView.query().get_entry()/read_bytes()` boundary, then
publishes the immutable `ContentRef` and accepted fragment. Live validation
proves the complete chain: exact RepoView and source object ID, M2a reread
bytes, `ContentRef`, and accepted fragment raw bytes are all equal. A generic
accepted `SourceEvidenceRef` remains useful for parser/negative cases but can
never promote a claim to `FACT`; parsing a serialized descriptor is not
authority verification. `ContextPackage` and `EngineeringDecision` source
grounding likewise require the exact CommittedRepoView plus live M2b binding
store. `CoverageBinding` records explicitly bind every covered dimension and
must-see target to actual claim IDs and/or accepted fragment IDs, so
`COMPLETE` cannot be self-attested.

`ContextResolution` carries one explicit gap-to-evidence edge per resolved
gap. It enforces `resulting_gap_ids = prior_gap_ids - resolved_gap_ids +
introduced_gap_ids`, keeps unrelated gaps visible, and requires the edge to
name resulting coverage for the resolved gap and the newly accepted evidence.
Denial and unavailability preserve the complete prior gap set. An
`EngineeringDecision` may classify only claim IDs present in the supplied
Understanding basis and requires exact equality of the current RepoView set,
ContextPackage set and opaque IntentBasis when applicability is checked.

## M2b boundary

`publish_semantic_record()` creates canonical JSON bytes, an M2b `ContentRef`,
an exact RepoView-bound `TypedContextFragment`, and a durable accepted binding.
`transport_semantic_record()` exercises Browser encode → Native decode with the
binding store and reconstructs the exact record. Semantic records contain no
protocol/chunk bookkeeping.

No daemon, database, writer, scheduler, API requirement, Browser UI,
production activation or Control Center authority is introduced. Runtime,
writer and activation remain `OFF / OFF / OFF`. M2d remains unstarted.

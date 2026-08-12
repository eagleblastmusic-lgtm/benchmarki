# N5 — Engineering Intelligence Provider Experiment

N5 adds a narrow, build-only read-only `CodeFactProvider` port. Each fact is
bound to the exact `COMMITTED` RepoView, provider/version/configuration
identity, language, source path/object and observed coverage. Provider output
is derived Engineering Intelligence evidence, not repository, lifecycle,
Candidate, Evidence, Publication or Git authority.

## Implemented path

- `TreeSitterPythonProvider` extracts Python syntax structure, definitions,
  imports and reference-shaped calls. Semantic definition resolution and
  runtime dispatch remain explicit gaps.
- `FallbackCodeFactProvider` provides a lower-coverage regex baseline and is
  safe to remove. It preserves RepoView binding and explicit gaps.
- `LspCodeFactProvider` is a real stdio JSON-RPC adapter for one explicitly
  configured language-server command. It sends only `initialize`,
  `initialized`, `textDocument/didOpen` and read-only definition requests; no
  workspace edit, rename, formatting, code-action or execute-command method is
  used. Returned locations are checked against the exact materialized RepoView.
- `VNextCompositionRoot` constructs these adapters explicitly; there is no
  discovery registry, daemon or new store. Optional Tree-sitter dependencies
  are pinned in the `code-intelligence` extra.

## Evidence and quality disposition

The deterministic fixture comparison shows the fallback covers definitions and
imports while Tree-sitter additionally covers syntax structure and
reference-shaped calls, with semantic resolution still a gap. The provider
projection keeps unsupported requested dimensions as explicit `Unknown`
records and creates only `DERIVED` claims. Stale RepoViews, foreign paths,
unsupported language and unavailable providers fail closed.

### Gate disposition

- GATE A — Provider contract + exact source binding: `PASS`.
- GATE B — Tree-sitter minimum provider: `PASS`.
- GATE C — real LSP provider: `VALID INCONCLUSIVE`. Pyright 1.1.390 was
  started through its real stdio JSON-RPC server and returned one exact
  definition on a disposable fixture. The adapter recorded a Windows
  materialized-workspace cleanup limitation instead of upgrading it to
  authority.
- GATE D — Engineering Intelligence integration: `PASS`; provider facts are
  projected only as `DERIVED` claims and unsupported dimensions remain
  explicit `Unknown` records.
- GATE E — quality/regression evaluation: `INCONCLUSIVE`. The bounded complex
  fixture demonstrates additional syntax/reference coverage, but it is not a
  representative S1–S4 quality corpus and cannot establish the full roadmap
  quality gate or an adoption claim.
- GATE F — fallback/removal/closure: `PASS`.

The bounded comparison is directional, not an aggregate score. On the exact
fixture, the lexical fallback covers `definitions` and `imports`; Tree-sitter
adds `syntax_structure` and `references`, including a call-shaped `helper`
fact. On the real committed M4a module, Tree-sitter produced 638 facts with
`definitions`, `imports`, `references`, and `syntax_structure`; the fallback
produced 23 facts with `definitions` and `imports`. Both retain explicit
semantic-resolution gaps. This is evidence for a useful pilot, not proof that
S1–S4 have no regression, ownership is resolved, or architecture constraints
are covered.

Accordingly the N5 result is `INCONCLUSIVE_AND_DEFERRED`: the reusable
read-only provider contracts remain, but no provider output is admitted to the
model-visible path until a later representative quality gate resolves the
remaining evidence uncertainty.

The current environment can run the pinned Tree-sitter Python binding in an
isolated temporary path. A real Pyright language server was independently
invoked through its stdio entry point, but no project-owned server/runtime is
committed; the LSP adapter therefore remains explicitly configured and its
availability is `VALID INCONCLUSIVE` when absent. This N5 slice does not make
provider facts model-visible; a later N6 Browser rehearsal must be the first
quality gate for any model-facing provider projection.

Technology disposition for this bounded experiment:

- Tree-sitter = `PILOT_CONTINUE` (useful syntax/reference coverage; quality
  value beyond syntax needs a larger representative corpus).
- selected LSP = `PILOT_CONTINUE` / `VALID INCONCLUSIVE` for the observed
  Windows materialization-cleanup limitation; the boundary is implemented but
  not silently treated as authority.
- ast-grep = `DEFER` (no demonstrated need beyond the provider contract).
- SCIP = `DEFER` (no naturally available artifact).
- Zoekt = `NOT_JUSTIFIED` (repository-scale lexical threshold not crossed).

Runtime, writer and activation remain `OFF / OFF / OFF`; legacy state is
untouched; no Git refs or production state are changed.

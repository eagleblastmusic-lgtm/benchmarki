# BDB vNext — NX-070 current-state snapshot

Snapshot version: `bdb-vnext-current-snapshot-v1`  
Status: **CURRENT / VOLATILE SNAPSHOT**  
Observation date: `2026-08-27`

This is the repository's explicit current-state surface for the NX-070
documentation synchronization. It separates the qualified repository source
from the currently deployed production generation. The authority order is:
qualified source/tests and fresh runtime evidence, then this snapshot for the
volatile state summary, then the CURRENT documentation set, with historical
and governance records retained in their declared roles.

## 1. Qualified source state

The accepted NX-069 qualification input is a source subject, not a production
deployment claim:

| Field | Value |
| --- | --- |
| Branch | `bdb-vnext` |
| Qualified source HEAD | `a6aa681ccbf40ca181834ed3fe628152a06dd406` |
| Qualified source TREE | `a496aefa0667498985f0a117c5e13bf59f2be9ef` |
| Qualification level | `NX-068 = ACCEPTED PASS`; `NX-069 = ACCEPTED PASS` |
| Latest accepted source gate | `NX-069` |
| Current documentation task | `NX-070` |

The qualified source identity is supported by the [NX-068 qualification
test](../tests/test_nx068_cross_subsystem_failure_injection.py), the [NX-069
qualification test](../tests/test_nx069_full_qualification.py), and the
source-bound evidence under `runtime/evidence/` (including the NX-069
adversarial, Windows, UAC, and operational-learning reports). Those evidence
artifacts retain their original source identity and are not rewritten by
NX-070.

The final documentation commit's `HEAD` and `TREE` are derived from the
committed repository by the [NX-070 consistency gate](../tests/test_nx070_documentation_consistency.py).
They are intentionally not copied into this tracked snapshot: embedding a
commit that contains the snapshot would create an impossible self-reference.
The gate reports the actual committed `SOURCE_HEAD` and `SOURCE_TREE` and
checks this pre-documentation qualified source subject separately.

## 2. Current production state

The current production observation is the external Bootstrap authority, not
the branch HEAD and not a generated staging directory. Readback was taken
from `C:\ProgramData\BartoszDevBridge-Next\bootstrap\slot-state.json` and its
matching slot manifests.

| Slot/state | Observed identity | Evidence |
| --- | --- | --- |
| `ACTIVE` | generation `bdb-vnext-g1`; source `abb55690fcd583cfd9b2f1cd922e71709165b999`; tree `9ffc7cec9a2f131965ef12063ac892e7e63a0cae`; manifest `sha256:dc20667e3dad7a90de24fb5ea72d2eb257ca393a23c7ed0afb46e7a3f1231e67` | `C:\ProgramData\BartoszDevBridge-Next\bootstrap\slot-state.json`; matching `slot-manifests\dc20667e3dad7a90de24fb5ea72d2eb257ca393a23c7ed0afb46e7a3f1231e67.json` |
| `PREVIOUS` | generation `bdb-vnext-g1`; source `38cdd038c59416f85caef8758bd7f879100c866a`; manifest `sha256:b7ed449fdf18d97dea91880c4f1f936889649e79654e192cbaf1a0f91bae7f5a` | external Bootstrap `previous_manifest_sha256` and matching slot manifest |
| `CANDIDATE` | `null` / no candidate manifest in the observed slot state | external Bootstrap `candidate_manifest_sha256` |

The external record says `production_activation_performed: true` for the
previously established ACTIVE generation. That is historical deployment
state; it is not an effect of NX-070. NX-070 performed no production
promotion, no candidate staging, no cutover, and no Bootstrap ACTIVE
modification. A repo-local `runtime\clients\client-plan.json` or a temporary
`runtime\bootstrap\bundles\candidate-*` directory is build/runtime evidence,
not a permission to rewrite the external ACTIVE pointer.

## 3. Release status and hard boundaries

| Item | Current status |
| --- | --- |
| `NX-068` | `ACCEPTED PASS` |
| `NX-069` | `ACCEPTED PASS` on the qualified source subject above |
| `NX-070` | Documentation synchronization; the committed gate is the acceptance authority |
| `NX-071` | `NOT STARTED` |
| `NX-G7` | `NOT STARTED` |
| Candidate staging/promotion | `NOT PERFORMED` |
| Production deployment/cutover of the qualified source | `NOT PERFORMED` |
| Bootstrap ACTIVE modification by NX-070 | `NOT PERFORMED` |
| Premium Calculator P3 | `NOT STARTED` |

Source/production distinction is mandatory: a qualified source PASS does not
make that source the deployed production runtime. `ACTIVE`, `PREVIOUS`, and
`CANDIDATE` remain separate Bootstrap slots; activation is an ACTIVE state
transition, while production intake/admission remains a separate M9b/M3c
concept.

## 4. Documentation/implementation parity snapshot

The following claims are limited to behavior already implemented and
qualified through NX-069. Each row points to the source contract or focused
evidence used for the documentation statement.

| Surface | Synchronized current statement | Source/evidence |
| --- | --- | --- |
| Project Memory and migration | Project Memory v2 source/architecture is implemented and qualified; the v1→v2 shadow migration/importer is qualified; v1 remains the production/reference authority before cutover. | [v2 contract](../bdb_vnext/project_memory_v2_contract.py), [v2 store](../bdb_vnext/project_memory_v2_store.py), [shadow migration](../bdb_vnext/v1_v2_shadow_migration.py), [NX-066 tests](../tests/test_nx066_v1_v2_shadow_migration.py) |
| Identity, binding, attempts, generations, and results | Execution preserves exact project/task/binding identity; identity preservation and binding lifecycle are single-active; attempt/generation semantics, result identity, and failure taxonomy remain source-bound. | [execution coordinator](../bdb_vnext/project_execution.py), [result identity](../bdb_vnext/result_identity.py), [NX-003 contract](BDB_vNext_NX003_Binding_Lifecycle_Transition_Contract.md), [NX-004 contract](BDB_vNext_NX004_Result_Identity_v2_Contract.md) |
| Failure, continuation, re-entry, and CI | `CI_WAITING` is a waiting state rather than a failure; continuation/re-entry remains bounded and identity-bound; unknown or unsafe state fails closed. | [failure taxonomy](../bdb_vnext/failure_taxonomy.py), [CI_WAITING](../bdb_vnext/ci_waiting.py), [session re-entry](../bdb_vnext/session_reentry.py), [startup recovery](../bdb_vnext/startup_recovery.py) |
| AUTO scopes | AUTO is sequential and milestone-scoped by default; the default `AUTO` scope remains `MILESTONE`; completed milestones do not auto-start the next milestone. | [AUTO scope contract](../bdb_vnext/auto_scope_contract.py), [scope orchestrator](../bdb_vnext/scope_orchestrator.py), [NX-G2 qualification](../tests/test_nxg2_m2_final_qualification.py) |
| Environment and Local Execution | Environment handling is typed/narrow and source-bound; Local Execution uses policy-bound authenticated IPC and does not bypass the canonical execution authority. | [environment contract](../bdb_vnext/m4c_environment.py), [Local Execution contract](../bdb_vnext/local_execution_contract.py), [authenticated worker IPC](../bdb_vnext/local_execution_worker.py) |
| Browser result identity | Browser result detection and panels use canonical result/binding identity rather than a DOM node; submission requires the canonical accepted result path. | [Browser content adapter](../browser_extension_vnext/content_adapter.js), [NX-005 contract](BDB_vNext_NX005_Browser_Semantic_Identity_Contract.md), [Browser identity tests](../tests/test_nx005_browser_semantic_identity.py) |
| Native Host routing and IPC | The dedicated `com.bartosz.dev_bridge.vnext` Native Host is the pinned vNext route; authenticated IPC and canonical admission remain separate from task selection authority. | [Native Host](../bdb_vnext/m9b_native_host.py), [Native messaging](../bdb_bridge/native_messaging.py), [Native tests](../tests/test_m9b_native_host.py) |
| Windows Witness | The real Microsoft UIAutomationCore/IUIAutomation backend is UIA-first; process/window/control identity and PRE/ACTION/POST evidence are required; coordinate fallback is deny-by-default. | [Witness contract](../bdb_vnext/windows_witness_contract.py), [UIA backend](../bdb_vnext/microsoft_uia_backend.py), [UIA action driver](../bdb_vnext/uia_action_driver.py), [Witness evidence](../bdb_vnext/witness_evidence.py) |
| Operator checkpoint and UAC | Operator checkpoint semantics remain explicit; secure-desktop restrictions prohibit automation, there is no secure-desktop automation and no credential injection; UAC remains operator-controlled, with source-equivalent manual evidence reusable only under qualified source-equivalence rules. | [UAC checkpoint](../bdb_vnext/uac_elevation_checkpoint.py), [Witness acceptance mapping](../bdb_vnext/witness_acceptance_mapping.py), [NX-G5 tests](../tests/test_nxg5_witness_operator_gate.py) |
| Operational learning authority | Structured records = authority. Markdown friction/improvement logs = deterministic projections only. Global learning is default OFF, requires explicit opt-in, and exports sanitized projections only; it does not edit the Project Plan, project code, or local authority. | [friction contract](../bdb_vnext/friction_improvement_contract.py), [Markdown projections](../bdb_vnext/learning_markdown_projections.py), [global learning view](../bdb_vnext/learning_retention_global_view.py), [NX-G6 tests](../tests/test_nxg6_operational_learning_gate.py) |
| Retention | Retention preserves active/unresolved authority and applies bounded policy to terminal, diagnostic, and sanitized global projections. | [retention compaction](../bdb_vnext/retention_compaction.py), [learning retention tests](../tests/test_nx064_learning_retention_global_view.py) |
| Feature flags and canary | Feature flags are default-off and legacy-compatible by default; the synthetic canary is isolated; Premium Calculator is not the canary; Premium Calculator P3 remains not started; canary rollback does not mutate Bootstrap ACTIVE. | [feature flags/canary](../bdb_vnext/feature_flags_synthetic_canary.py), [NX-067 tests](../tests/test_nx067_feature_flags_synthetic_canary.py) |
| Fault qualification | Cross-subsystem fault qualification is source-bound and fail-closed; it records bounded recovery outcomes without starting Premium Calculator P3 or changing Bootstrap ACTIVE. | [fault harness](../bdb_vnext/cross_subsystem_fault_injection.py), [NX-068 tests](../tests/test_nx068_cross_subsystem_failure_injection.py) |
| Bootstrap and release slots | Bootstrap preserves the `ACTIVE`/`PREVIOUS`/`CANDIDATE` distinction; staging/build evidence is not activation; this documentation task changes no runtime slot. | [Bootstrap](../bdb_vnext/bootstrap.py), [M11a slots](../bdb_vnext/m11a_bootstrap_slots.py), [M11c active reader](../bdb_vnext/m11c_active_reader.py), [production runtime guide](VNEXT_PRODUCTION_RUNTIME.md) |
| Schema examples and paths | The maintained documentation example is checked through the current local-envelope runtime validator; current documentation links and source/schema paths are checked by the NX-070 gate. | [example](examples/bdb-local-envelope-v1.json), [example guide](examples/README.md), [local-envelope validator](../bdb_bridge/local_spool_transport.py), [NX-070 gate](../tests/test_nx070_documentation_consistency.py) |

## 5. Operator reading rules

- The qualified source subject and the deployed ACTIVE source may differ.
- Historical values such as `eae9fee…`, `abb5569…`, and `bd634b8…` are not
  current qualified-source declarations unless explicitly labeled historical,
  superseded, previous production, or archived. The deployed `abb5569…` value
  above is intentionally retained as the observed current production ACTIVE
  identity.
- The current snapshot is a projection of source/evidence observations. It is
  not an authority that can mutate Project Memory, Bootstrap, production
  intake, or project code.
- Markdown is not authoritative; the global view does not own local structured
  records, and learning does not automatically edit the Project Plan or
  project code.
- Markdown learning/friction views are projection-only; structured records
  remain the authority.

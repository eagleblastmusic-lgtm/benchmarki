# BDB vNext — NX-070 supersession map

Map version: `bdb-vnext-supersession-map-v1`  
Status: **CURRENT / VERSIONED**  
Owner task: `NX-070`

This map records every stale current-source statement replaced by the NX-070
documentation synchronization. Historical records are not deleted or
rewritten; each replacement has an explicit disposition and source/evidence
reason.

| ID | Document/path | Old current statement/baseline | Replacement/current statement | Reason and supporting evidence | Historical-preservation disposition |
| --- | --- | --- | --- | --- | --- |
| `SM-001` | `README.md` — verified semantics section | `Implementation baseline eae9fee9…` presented as the current implementation baseline. | Qualified source subject is the exact NX-069 source `a6aa681…` / tree `a496aefa…`; final documentation commit identity is derived by the NX-070 gate. | Stale current metadata; [NX-069 qualification test](../tests/test_nx069_full_qualification.py) and [current snapshot](NX070_CURRENT_STATE.md). | `REPLACED_BY_CURRENT_SNAPSHOT`; historical repair semantics retained as qualified behavior. |
| `SM-002` | `docs/DOCUMENTATION_STATUS.md` — CURRENT classification | CURRENT documents were described as observing `eae9fee9…`. | CURRENT documents are synchronized to the versioned NX-070 snapshot and the accepted qualified source subject. | Removes `DOCUMENTATION_IMPLEMENTATION_DIVERGENCE`; [documentation status](DOCUMENTATION_STATUS.md) and [NX-070 gate](../tests/test_nx070_documentation_consistency.py). | `REPLACED_BY_CURRENT_SNAPSHOT`. |
| `SM-003` | `docs/DOCUMENTATION_STATUS.md` — repair note | The repair note used `eae9fee9…` as the current baseline. | The repair is labeled historical/pre-NX-070; its behavior is revalidated on the accepted NX-069 source. | Prevents a historical repair commit from becoming the current source declaration. | `HISTORICAL_RETAINED`. |
| `SM-004` | `docs/VNEXT_CURRENT_ARCHITECTURE.md` — header | `Implementation baseline eae9fee9…`. | Qualified source subject and final committed documentation identity are separated by the NX-070 snapshot/gate. | The implementation moved through NX-068/NX-069 after the old documentation baseline. | `REPLACED_BY_CURRENT_SNAPSHOT`. |
| `SM-005` | `docs/VNEXT_CURRENT_ARCHITECTURE.md` — fail-stop section | The eae9 repair was presented as the current implementation anchor. | Fail-stop semantics are stated as retained and qualified through NX-069, with source/runtime distinction. | Avoids claiming that a historical source-level repair is the deployed runtime. | `HISTORICAL_RETAINED`. |
| `SM-006` | `docs/VNEXT_PROJECT_WORKFLOW.md` — header | `Implementation baseline eae9fee9…`. | Workflow documentation points to the qualified-source snapshot and committed NX-070 gate. | Current workflow docs must follow the accepted source, not the old baseline. | `REPLACED_BY_CURRENT_SNAPSHOT`. |
| `SM-007` | `docs/VNEXT_PROJECT_WORKFLOW.md` — fail-stop section | The eae9 repair was presented as the current workflow baseline. | The accepted source preserves the fail-stop workflow semantics; production state remains separately observed. | Removes source/runtime conflation. | `HISTORICAL_RETAINED`. |
| `SM-008` | `docs/VNEXT_AUTO_BROWSER_NATIVE.md` — header | `Implementation baseline eae9fee9…`. | AUTO/Browser/Native behavior is bound to the qualified-source snapshot and gate. | The source has advanced through later qualified AUTO, canary, and fault changes. | `REPLACED_BY_CURRENT_SNAPSHOT`. |
| `SM-009` | `docs/VNEXT_AUTO_BROWSER_NATIVE.md` — fail-stop section | The eae repair was presented as the current AUTO implementation anchor. | AUTO fail-stop and result acceptance semantics are stated as retained through NX-069. | Keeps the semantic claim while removing the stale current identity. | `HISTORICAL_RETAINED`. |

`SUPERSEDED_CURRENT_ITEMS_WITHOUT_MAP` is zero when the current documentation
surfaces contain no unlabeled stale-current baseline. The external production
ACTIVE source is not a superseded current statement: it is intentionally
retained in [the current snapshot](NX070_CURRENT_STATE.md) as the observed
deployed generation until a later explicit promotion.


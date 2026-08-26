"""NX-008: Writer/CAS, monotonic revision, and rebuildable Catalog/Memory projections.

Covers:
1. SINGLE_CANONICAL_WRITE_TRANSACTION_API
2. REVISION_MONOTONIC
3. STALE_REVISION_REJECTED
4. STALE_REJECTION_PARTIAL_WRITE
5. CONCURRENT_WRITER_LOST_UPDATE
6. EVENT_IDS_COMPLETE
7. DUPLICATE_EVENT_IDS
8. PROJECT_CATALOG_IS_REBUILDABLE_PROJECTION
9. CATALOG_MEMORY_SPLIT_BRAIN
10. PROJECTION_CURSOR_MONOTONIC
11. INTERRUPTED_WRITE_RECOVERABLE (Scenarios A, B, C)
12. CORRUPT_TAIL_HANDLING
13. FINAL_STATE_DIGEST_DETERMINISTIC (Isolated Store A vs Store B)
14. CONCURRENCY_HARNESS (Real write counters)
15. SOURCE_BOUND_MACHINE_GATE (Source binding & derived evidence)
"""

from __future__ import annotations

import ast
import concurrent.futures
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from bdb_shared.evidence import canonical_json_bytes, semantic_digest
from bdb_vnext.project_catalog import (
    PROJECT_PLAN_SCHEMA,
    ProjectBrief,
    ProjectCatalog,
    ProjectCatalogError,
    new_project_record,
    validate_project_plan,
)
from bdb_vnext.project_memory import (
    ProjectMemoryError,
    ProjectMemoryState,
    ProjectMemoryStore,
)

MUTATOR_METHOD_NAMES = (
    "append_event",
    "add_decision",
    "add_inbox",
    "update_inbox",
    "add_risk",
    "resolve_risk",
    "add_debt",
    "resolve_debt",
    "add_attention",
    "resolve_attention",
    "create_checkpoint",
    "ensure_initial_plan",
    "apply_update",
    "execution_transaction",
)

FOCUSED_TEST_MANIFEST = (
    "tests/test_nx008_writer_cas.py",
    "tests/test_cc3_project_memory_slice3.py",
    "tests/test_cc3_project_slice2.py",
    "tests/test_project_execution_integration.py",
    "tests/test_nx006_launch_outbox.py",
    "tests/test_nx007_queue_locking.py",
)


def detect_memory_state_anomalies(store: ProjectMemoryStore, expected_revision: int, expected_event_count: int) -> int:
    """Inspect disk memory state and return count of detected partial-write / integrity anomalies."""
    anomalies = 0
    if not store.memory_path.exists():
        return 1
    try:
        raw = store.memory_path.read_bytes()
        doc = json.loads(raw.decode("utf-8-sig"))
    except Exception:
        return 1

    if doc.get("revision") != expected_revision:
        anomalies += 1
    events = doc.get("events", [])
    if len(events) != expected_event_count:
        anomalies += 1
    for idx, ev in enumerate(events, 1):
        if ev.get("event_id") != f"{store.project_id}:e{idx:06d}":
            anomalies += 1

    parent = store.memory_path.parent
    if parent.exists():
        temp_files = list(parent.glob(".memory.json.*.tmp"))
        if len(temp_files) > 0:
            anomalies += len(temp_files)

    return anomalies


def verify_unprotected_mutator_paths() -> tuple[bool, int, list[str]]:
    """AST-based structural verification that all mutator methods route through write_transaction."""
    src_file = Path(__file__).resolve().parent.parent / "bdb_vnext" / "project_memory.py"
    tree = ast.parse(src_file.read_text(encoding="utf-8"))
    cls_node = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "ProjectMemoryStore")
    methods = {n.name: n for n in cls_node.body if isinstance(n, ast.FunctionDef)}

    def routes_to_write_tx(name: str, visited: set[str] | None = None) -> bool:
        if visited is None:
            visited = set()
        if name in visited:
            return False
        visited.add(name)
        if name == "write_transaction":
            return True
        fn = methods.get(name)
        if not fn:
            return False
        calls = [
            call.func.attr
            for call in ast.walk(fn)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name) and call.func.value.id == "self"
        ]
        return any(routes_to_write_tx(c, visited) for c in calls)

    unprotected = []
    for m in MUTATOR_METHOD_NAMES:
        if m not in methods or not routes_to_write_tx(m):
            unprotected.append(m)

    for m_name, fn in methods.items():
        calls = [
            call.func.attr
            for call in ast.walk(fn)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "_write_state"
        ]
        if calls and m_name != "write_transaction":
            unprotected.append(f"{m_name} calls _write_state directly")

    return (len(unprotected) == 0, len(unprotected), unprotected)


def test_ast_unprotected_mutator_paths() -> None:
    """Requirement: All 14 mutators in ProjectMemoryStore must route through write_transaction."""
    is_protected, count, unprotected = verify_unprotected_mutator_paths()
    assert is_protected is True
    assert count == 0
    assert unprotected == []


REQUIRED_GATE_OUTCOME_FIELDS = (
    "SINGLE_CANONICAL_WRITE_TRANSACTION_API",
    "UNPROTECTED_MUTATOR_PATHS",
    "REVISION_MONOTONIC",
    "STALE_REVISION_REJECTED",
    "STALE_REJECTION_PARTIAL_WRITE",
    "CONCURRENT_WRITER_LOST_UPDATE",
    "EVENT_IDS_COMPLETE",
    "DUPLICATE_EVENT_IDS",
    "PROJECT_CATALOG_IS_REBUILDABLE_PROJECTION",
    "CATALOG_MEMORY_SPLIT_BRAIN",
    "PROJECTION_CURSOR_MONOTONIC",
    "INTERRUPTED_WRITE_RECOVERABLE",
    "PARTIAL_WRITE_ANOMALIES_DETECTED",
    "CORRUPT_TAIL_HANDLING",
    "FINAL_STATE_DIGEST_DETERMINISTIC",
    "SEMANTICALLY_EQUIVALENT",
    "THREAD_CONCURRENCY_HARNESS",
    "CROSS_PROCESS_WRITER_HARNESS",
    "SOURCE_BOUND_MACHINE_GATE",
    "NO_HARDCODED_GATE_RESULTS",
    "status",
)


def inspect_gate_for_hardcoded_results() -> tuple[bool, list[str]]:
    """AST inspection of run_nx008_machine_gate ensuring all outcome fields are derived rather than literal constants."""
    src_file = Path(__file__).resolve()
    tree = ast.parse(src_file.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "run_nx008_machine_gate")
    dict_node = next(
        n.value
        for n in ast.walk(fn)
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "gate_result" for t in n.targets)
    )

    hardcoded_fields = []
    for k, v in zip(dict_node.keys, dict_node.values):
        if isinstance(k, ast.Constant) and k.value in REQUIRED_GATE_OUTCOME_FIELDS:
            if isinstance(v, ast.Constant):
                hardcoded_fields.append(str(k.value))

    return (len(hardcoded_fields) == 0, hardcoded_fields)


def test_no_hardcoded_gate_results_ast() -> None:
    """Requirement: AST analysis proves zero hardcoded machine gate outcome fields."""
    no_hardcoded, hardcoded_list = inspect_gate_for_hardcoded_results()
    assert no_hardcoded is True
    assert hardcoded_list == []



def _make_sample_plan(project_id: str, version: int = 1, project_name: str = "Sample Project") -> dict[str, Any]:
    return {
        "schema": PROJECT_PLAN_SCHEMA,
        "project_id": project_id,
        "project_name": project_name,
        "plan_version": version,
        "milestones": [
            {"id": "m1", "title": "Milestone 1", "description": "First milestone", "status": "active"}
        ],
        "tasks": [
            {
                "id": "t1",
                "milestone_id": "m1",
                "title": "Task 1",
                "description": "First task",
                "status": "active",
                "dependencies": [],
                "acceptance_criteria": ["criterion 1"],
            },
            {
                "id": "t2",
                "milestone_id": "m1",
                "title": "Task 2",
                "description": "Second task",
                "status": "pending",
                "dependencies": ["t1"],
                "acceptance_criteria": ["criterion 2"],
            },
        ],
        "current_task_id": "t1",
    }


def test_monotonic_revision_increment_on_write(tmp_path: Path) -> None:
    """Requirement: Each committed transaction increments revision monotonically."""
    store = ProjectMemoryStore(tmp_path / "runtime", "p-test")
    state0 = store.read_state()
    assert state0.revision == 1

    ev1 = store.append_event("PROJECT_CREATED", "Initialized project")
    state1 = store.read_state()
    assert state1.revision == 2
    assert len(state1.events) == 1
    assert state1.events[0].event_id == ev1.event_id

    dec = store.add_decision(title="Architecture", decision="Modular", reason="Maintainability")
    state2 = store.read_state()
    assert state2.revision == 3
    assert len(state2.decisions) == 1
    assert state2.decisions[0].decision_id == dec.decision_id


def test_concurrent_same_revision_cas_rejection(tmp_path: Path) -> None:
    """Requirement: Two writers starting from same revision -> one commits, other gets stale CAS reject."""
    store = ProjectMemoryStore(tmp_path / "runtime", "p-test")
    store.append_event("PROJECT_CREATED", "Initialized project")  # rev becomes 2

    current = store.read_state()
    start_rev = current.revision  # 2

    def op_a(state: ProjectMemoryState) -> tuple[ProjectMemoryState, str]:
        return state, "A_DONE"

    res_a = store.write_transaction(op_a, expected_revision=start_rev)
    assert res_a == "A_DONE"

    def op_b(state: ProjectMemoryState) -> tuple[ProjectMemoryState, str]:
        return state, "B_DONE"

    with pytest.raises(ProjectMemoryError) as exc_info:
        store.write_transaction(op_b, expected_revision=start_rev)
    assert exc_info.value.code == "stale_revision_rejected"

    after_state = store.read_state()
    assert after_state.revision == 3


def test_stale_cas_rejection_leaves_state_digest_unchanged(tmp_path: Path) -> None:
    """Requirement: Stale CAS rejection does not alter state or leave partial writes."""
    store = ProjectMemoryStore(tmp_path / "runtime", "p-test")
    store.append_event("PROJECT_CREATED", "Initialized project")
    before_state = store.read_state()
    before_digest = semantic_digest(before_state.to_dict())

    with pytest.raises(ProjectMemoryError) as exc_info:
        store.write_transaction(lambda s: (s, "fail"), expected_revision=before_state.revision - 1)
    assert exc_info.value.code == "stale_revision_rejected"

    after_state = store.read_state()
    after_digest = semantic_digest(after_state.to_dict())
    assert before_digest == after_digest


def test_failed_transaction_leaves_no_partial_state(tmp_path: Path) -> None:
    """Requirement: If mutation/validation raises error, no state is written to disk."""
    store = ProjectMemoryStore(tmp_path / "runtime", "p-test")
    store.append_event("PROJECT_CREATED", "Initialized")
    before_state = store.read_state()
    before_revision = before_state.revision

    def bad_op(state: ProjectMemoryState) -> tuple[ProjectMemoryState, None]:
        raise ValueError("simulated crash inside transaction callback")

    with pytest.raises(ValueError, match="simulated crash"):
        store.write_transaction(bad_op)

    after_state = store.read_state()
    assert after_state.revision == before_revision
    assert len(after_state.events) == len(before_state.events)


def test_interrupted_write_scenarios(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Requirement: All 3 interrupted write scenarios recover cleanly."""
    runtime = tmp_path / "runtime"
    store = ProjectMemoryStore(runtime, "p-interrupted")
    catalog = ProjectCatalog(runtime)

    # Scenario A: Failure before os.replace
    store.append_event("PROJECT_CREATED", "Initial state")
    before_state = store.read_state()
    original_replace = os.replace

    def crashing_replace(src: Any, dst: Any) -> None:
        raise OSError("simulated I/O crash before atomic replace")

    monkeypatch.setattr(os, "replace", crashing_replace)
    with pytest.raises(OSError, match="simulated I/O crash"):
        store.append_event("TASK_STARTED", "Starting t1")

    monkeypatch.setattr(os, "replace", original_replace)
    state_after_a = store.read_state()
    assert state_after_a.revision == before_state.revision
    assert len(state_after_a.events) == 1

    # Scenario B: Failure during temp write
    original_open = Path.open

    def crashing_open(path_obj: Path, *args: Any, **kwargs: Any) -> Any:
        if ".tmp" in path_obj.name:
            raise OSError("simulated disk full during temp write")
        return original_open(path_obj, *args, **kwargs)

    monkeypatch.setattr(Path, "open", crashing_open)
    with pytest.raises(OSError, match="simulated disk full"):
        store.append_event("TASK_STARTED", "Starting t1")

    monkeypatch.setattr(Path, "open", original_open)
    state_after_b = store.read_state()
    assert state_after_b.revision == before_state.revision
    assert len(state_after_b.events) == 1

    # Scenario C: Canonical replace succeeded, projection lagging/interrupted
    plan = validate_project_plan(_make_sample_plan("p-interrupted", version=1, project_name="Interrupted Project"))
    store.ensure_initial_plan(plan)
    p_rec = new_project_record(
        project_id="p-interrupted",
        display_name="Interrupted Project",
        repo_alias="p-interrupted",
        local_repo_path=tmp_path / "repo",
        github_repo=None,
        brief=ProjectBrief("Interrupted Project", "Goal", "Desc", "tool"),
    )
    catalog.upsert(p_rec)

    # Mutate memory directly (simulate lag before catalog sync)
    store.append_event("TASK_STARTED", "Task 1 started", task_id="t1")
    canonical_rev = store.read_state().revision

    # Lagging catalog
    cat_before = catalog.get("p-interrupted")
    assert cat_before is not None
    assert cat_before.projection_cursor != canonical_rev

    # Catch-up sync
    cat_after = catalog.sync_projection("p-interrupted")
    assert cat_after is not None
    assert cat_after.projection_cursor == canonical_rev


def test_projection_cursor_monotonic(tmp_path: Path) -> None:
    """Requirement: Projection cursor advances monotonically, detects stale cursor, and deduplicates same revision."""
    runtime = tmp_path / "runtime"
    catalog = ProjectCatalog(runtime)
    store = ProjectMemoryStore(runtime, "p-cursor")

    p_rec = new_project_record(
        project_id="p-cursor",
        display_name="Cursor Project",
        repo_alias="p-cursor",
        local_repo_path=tmp_path / "repo",
        github_repo=None,
        brief=ProjectBrief("Cursor Project", "Goal", "Desc", "tool"),
    )
    catalog.upsert(p_rec)
    plan = validate_project_plan(_make_sample_plan("p-cursor", version=1, project_name="Cursor Project"))
    store.ensure_initial_plan(plan)

    # Initial plan bumps revision to 2 -> sync -> cursor is 2
    rec1 = catalog.sync_projection("p-cursor")
    assert rec1 is not None
    assert rec1.projection_cursor == 2

    # Advance revision from 2 to 3 -> sync -> cursor is 3
    store.append_event("TASK_STARTED", "Starting t1", task_id="t1")
    rec2 = catalog.sync_projection("p-cursor")
    assert rec2 is not None
    assert rec2.projection_cursor == 3
    assert rec2.projection_cursor > rec1.projection_cursor

    # Stale cursor check: attempt to sync with older state revision (1 < 3)
    stale_state = ProjectMemoryState("p-cursor", (), (), (), (), (), (), (), {}, revision=1)
    with pytest.raises(ProjectCatalogError) as exc_info:
        catalog.sync_projection("p-cursor", memory_state=stale_state)
    assert exc_info.value.code == "stale_projection_cursor"

    # Same revision re-sync -> no duplication, cursor unchanged
    rec2_resync = catalog.sync_projection("p-cursor")
    assert rec2_resync is not None
    assert rec2_resync.projection_cursor == 3
    all_projects = catalog.read()
    assert len([p for p in all_projects if p.project_id == "p-cursor"]) == 1


def test_catalog_rebuild_from_canonical_memory(tmp_path: Path) -> None:
    """Requirement: ProjectCatalog is a rebuildable projection from ProjectMemory authority."""
    runtime = tmp_path / "runtime"
    catalog = ProjectCatalog(runtime)

    p1 = new_project_record(
        project_id="p1",
        display_name="Project One",
        repo_alias="p1",
        local_repo_path=tmp_path / "repo1",
        github_repo=None,
        brief=ProjectBrief("Project One", "Goal 1", "Desc 1", "tool"),
    )
    catalog.upsert(p1)

    plan1 = validate_project_plan(_make_sample_plan("p1", version=1, project_name="Project One"))
    mem1 = ProjectMemoryStore(runtime, "p1")
    mem1.ensure_initial_plan(plan1)
    mem1.append_event("TASK_STARTED", "Started t1", task_id="t1")

    catalog.sync_projection("p1")
    rec_before = catalog.get("p1")
    assert rec_before is not None
    assert rec_before.plan_imported is True
    assert rec_before.total_tasks == 2
    assert rec_before.plan_version == "1"

    # Delete catalog file
    catalog.path.unlink()
    assert catalog.read() == ()

    # Rebuild
    rebuilt = catalog.rebuild()
    assert len(rebuilt) >= 1
    rec_rebuilt = catalog.get("p1")
    assert rec_rebuilt is not None
    assert rec_rebuilt.plan_imported is True
    assert rec_rebuilt.total_tasks == 2
    assert rec_rebuilt.plan_version == "1"
    assert rec_rebuilt.display_name == "Project One"
    assert rec_rebuilt.projection_cursor == mem1.read_state().revision


def test_corrupt_memory_file_fails_closed(tmp_path: Path) -> None:
    """Requirement: Corrupt JSON in memory.json fails closed with memory_corrupt error."""
    store = ProjectMemoryStore(tmp_path / "runtime", "p-test")
    store.root.mkdir(parents=True, exist_ok=True)
    store.memory_path.write_bytes(b"{\xff\xfe corrupt bytes NOT JSON")

    with pytest.raises(ProjectMemoryError) as exc_info:
        store.read_state()
    assert exc_info.value.code == "memory_corrupt"


def test_complete_and_unique_event_ids(tmp_path: Path) -> None:
    """Requirement: Appended events form contiguous unique monotonic IDs {project_id}:e{index:06d}."""
    store = ProjectMemoryStore(tmp_path / "runtime", "p-test")
    for i in range(25):
        store.append_event("TASK_STARTED", f"Event #{i}")

    state = store.read_state()
    assert len(state.events) == 25
    expected_ids = [f"p-test:e{i:06d}" for i in range(1, 26)]
    actual_ids = [e.event_id for e in state.events]
    assert actual_ids == expected_ids
    assert len(set(actual_ids)) == 25


def _semantic_state_projection(state: ProjectMemoryState) -> dict[str, Any]:
    """Order-independent projection of semantic memory collections."""
    return {
        "project_id": state.project_id,
        "decisions": sorted(
            [
                {k: ("<time>" if k == "created_at" else v) for k, v in d.items()}
                for d in [x.to_dict() for x in state.decisions]
            ],
            key=lambda x: x["decision_id"],
        ),
        "risks": sorted(
            [
                {k: ("<time>" if k in {"created_at", "updated_at"} else v) for k, v in r.items()}
                for r in [x.to_dict() for x in state.risks]
            ],
            key=lambda x: x["risk_id"],
        ),
        "technical_debt": sorted(
            [
                {k: ("<time>" if k in {"created_at", "updated_at"} else v) for k, v in td.items()}
                for td in [x.to_dict() for x in state.technical_debt]
            ],
            key=lambda x: x["debt_id"],
        ),
    }


def test_isolated_stores_final_state_digest_different_interleavings(tmp_path: Path) -> None:
    """Requirement: Two independent isolated stores executing identical semantic operations in different orders yield identical semantic digests."""
    runtime_a = tmp_path / "runtime_a"
    runtime_b = tmp_path / "runtime_b"

    store_a = ProjectMemoryStore(runtime_a, "p-det")
    store_b = ProjectMemoryStore(runtime_b, "p-det")

    plan_a = validate_project_plan(_make_sample_plan("p-det", version=1, project_name="Deterministic Project"))
    plan_b = validate_project_plan(_make_sample_plan("p-det", version=1, project_name="Deterministic Project"))

    store_a.ensure_initial_plan(plan_a)
    store_b.ensure_initial_plan(plan_b)

    # Workload Order A: decision -> risk -> debt
    store_a.add_decision(title="Dec 1", decision="Opt A", reason="Reason A")
    store_a.add_risk(title="Risk 1", description="Risk Desc 1", severity="medium")
    store_a.add_debt(title="Debt 1", description="Debt Desc 1")

    # Workload Order B: debt -> decision -> risk (different interleaving)
    store_b.add_debt(title="Debt 1", description="Debt Desc 1")
    store_b.add_decision(title="Dec 1", decision="Opt A", reason="Reason A")
    store_b.add_risk(title="Risk 1", description="Risk Desc 1", severity="medium")

    state_a = store_a.read_state()
    state_b = store_b.read_state()

    audit_events_a = [e.event_type for e in state_a.events]
    audit_events_b = [e.event_type for e in state_b.events]
    assert audit_events_a == ["PLAN_IMPORTED", "DECISION_ADDED", "RISK_ADDED", "TECH_DEBT_ADDED"]
    assert audit_events_b == ["PLAN_IMPORTED", "TECH_DEBT_ADDED", "DECISION_ADDED", "RISK_ADDED"]
    assert audit_events_a != audit_events_b

    proj_a = _semantic_state_projection(state_a)
    proj_b = _semantic_state_projection(state_b)
    digest_a = semantic_digest(proj_a)
    digest_b = semantic_digest(proj_b)

    assert digest_a == digest_b
    assert state_a.revision == state_b.revision


def test_thread_concurrency_harness(tmp_path: Path) -> None:
    """Requirement: Multi-threaded concurrency harness with exact write accounting."""
    store = ProjectMemoryStore(tmp_path / "runtime", "p-thread-concurrency")
    num_threads = 8
    writes_per_thread = 25
    expected_logical_writes = num_threads * writes_per_thread

    def worker(worker_id: int) -> list[str]:
        event_ids = []
        for i in range(writes_per_thread):
            ev = store.append_event("TASK_STARTED", f"Worker {worker_id} item {i}")
            event_ids.append(ev.event_id)
        return event_ids

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(worker, w) for w in range(num_threads)]
        all_results = []
        for f in concurrent.futures.as_completed(futures):
            all_results.extend(f.result())

    final_state = store.read_state()
    event_ids = [e.event_id for e in final_state.events]

    committed_writes = len(final_state.events)
    lost_writes = expected_logical_writes - committed_writes
    duplicate_event_ids = len(event_ids) - len(set(event_ids))
    expected_sequence = [f"p-thread-concurrency:e{i:06d}" for i in range(1, committed_writes + 1)]
    missing_event_ids = sum(1 for i, expected_id in enumerate(expected_sequence) if event_ids[i] != expected_id)
    partial_write_anomalies = detect_memory_state_anomalies(store, 1 + committed_writes, committed_writes)

    assert expected_logical_writes == 200
    assert committed_writes == 200
    assert lost_writes == 0
    assert duplicate_event_ids == 0
    assert missing_event_ids == 0
    assert partial_write_anomalies == 0
    assert final_state.revision == 1 + committed_writes


def test_cross_process_writer_harness(tmp_path: Path) -> None:
    """Requirement: Cross-process writer harness testing file-level execution.lock and atomic replacement."""
    runtime = tmp_path / "runtime"
    project_id = "p-proc-concurrency"
    store = ProjectMemoryStore(runtime, project_id)
    num_processes = 2
    writes_per_proc = 25
    expected_logical_writes = num_processes * writes_per_proc

    worker_code = """
import sys, pathlib
from bdb_vnext.project_memory import ProjectMemoryStore
rt = pathlib.Path(sys.argv[1])
pid = sys.argv[2]
wid = sys.argv[3]
n = int(sys.argv[4])
store = ProjectMemoryStore(rt, pid)
for i in range(n):
    store.append_event('TASK_STARTED', f'w_{wid}_{i}')
"""

    procs = []
    for w in range(num_processes):
        p = subprocess.Popen(
            [sys.executable, "-c", worker_code, str(runtime), project_id, str(w), str(writes_per_proc)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        procs.append(p)

    for p in procs:
        out, err = p.communicate()
        assert p.returncode == 0, f"Worker process failed with error: {err}"

    final_state = store.read_state()
    event_ids = [e.event_id for e in final_state.events]
    committed_writes = len(event_ids)
    lost_writes = expected_logical_writes - committed_writes
    duplicate_event_ids = len(event_ids) - len(set(event_ids))
    expected_sequence = [f"{project_id}:e{i:06d}" for i in range(1, committed_writes + 1)]
    missing_event_ids = sum(1 for i, expected_id in enumerate(expected_sequence) if event_ids[i] != expected_id)
    partial_write_anomalies = detect_memory_state_anomalies(store, 1 + committed_writes, committed_writes)

    assert expected_logical_writes == 50
    assert committed_writes == 50
    assert lost_writes == 0
    assert duplicate_event_ids == 0
    assert missing_event_ids == 0
    assert partial_write_anomalies == 0
    assert final_state.revision == 1 + committed_writes


def compute_test_manifest_digest(repo_root: Path) -> str:
    """Deterministic hash of all test files in the focused test manifest."""
    entries = []
    for rel_path in FOCUSED_TEST_MANIFEST:
        full_path = repo_root / rel_path
        if full_path.exists():
            file_hash = hashlib.sha256(full_path.read_bytes()).hexdigest()
            entries.append({"path": rel_path, "sha256": file_hash})
    return semantic_digest({"manifest": entries})


def run_nx008_machine_gate(tmp_path: Path) -> dict[str, Any]:
    """Execute the full NX-008 source-bound machine gate where EVERY field is derived from evidence."""
    runtime = tmp_path / "gate_runtime"
    store = ProjectMemoryStore(runtime, "p-gate")
    catalog = ProjectCatalog(runtime)

    measured_partial_write_anomalies = 0

    # 1. Single canonical write transaction API & AST mutator proof
    is_protected, unprotected_count, unprotected_list = verify_unprotected_mutator_paths()
    rev_init = store.read_state().revision
    store.append_event("PROJECT_CREATED", "Init")
    rev_after_one = store.read_state().revision
    single_canonical_api = bool(is_protected and rev_after_one == rev_init + 1 and len(store.read_state().events) == 1)

    # 2. Revision monotonic
    revs = [rev_after_one]
    for k in range(4):
        store.append_event("TASK_STARTED", f"Progress {k}")
        revs.append(store.read_state().revision)
    revision_monotonic = bool(
        len(revs) == 5 and all(revs[i] < revs[i + 1] for i in range(len(revs) - 1)) and all(revs[i + 1] == revs[i] + 1 for i in range(len(revs) - 1))
    )

    # 3 & 4. Stale CAS rejection and Stale rejection partial write
    current_rev = store.read_state().revision
    state_before_stale = store.read_state()
    digest_before_stale = semantic_digest(state_before_stale.to_dict())
    stale_rejected = False
    try:
        store.write_transaction(lambda s: (store._append_event(s, "TASK_STARTED", "Partial write"), None), expected_revision=current_rev - 1)
    except ProjectMemoryError as e:
        if e.code == "stale_revision_rejected":
            stale_rejected = True
    state_after_stale = store.read_state()
    digest_after_stale = semantic_digest(state_after_stale.to_dict())
    stale_rejection_partial_write = bool(digest_before_stale != digest_after_stale or state_after_stale.revision != current_rev)
    measured_partial_write_anomalies += detect_memory_state_anomalies(store, current_rev, len(state_before_stale.events))

    # 5. Concurrent writer lost update (2 writers starting at same rev)
    rev_shared = store.read_state().revision
    succ_count = 0
    stale_cas_count = 0

    def concurrent_cas_writer(val: str) -> str:
        nonlocal succ_count, stale_cas_count
        try:
            store.write_transaction(lambda s: (store._append_event(s, "TASK_STARTED", val), val), expected_revision=rev_shared)
            succ_count += 1
            return "OK"
        except ProjectMemoryError as exc:
            if exc.code == "stale_revision_rejected":
                stale_cas_count += 1
                return "STALE"
            raise

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        futs = [ex.submit(concurrent_cas_writer, f"writer_{i}") for i in range(2)]
        for f in concurrent.futures.as_completed(futs):
            f.result()

    concurrent_writer_lost_update = bool(succ_count != 1 or stale_cas_count != 1)

    # 6 & 7. Event IDs completeness & uniqueness
    current_events = store.read_state().events
    actual_event_ids = [e.event_id for e in current_events]
    expected_event_ids = [f"p-gate:e{i:06d}" for i in range(1, len(actual_event_ids) + 1)]
    event_ids_complete = bool(actual_event_ids == expected_event_ids)
    duplicate_event_ids = bool(len(actual_event_ids) != len(set(actual_event_ids)))

    # 8 & 9. Catalog rebuildability & split-brain prevention
    p_rec = new_project_record(
        project_id="p-gate",
        display_name="Gate Project",
        repo_alias="p-gate",
        local_repo_path=tmp_path / "repo",
        github_repo=None,
        brief=ProjectBrief("Gate Project", "Goal", "Desc", "tool"),
    )
    catalog.upsert(p_rec)
    plan = validate_project_plan(_make_sample_plan("p-gate", version=1, project_name="Gate Project"))
    store.ensure_initial_plan(plan)
    catalog.sync_projection("p-gate")

    # Rebuild after catalog deletion
    catalog.path.unlink()
    rebuilt = catalog.rebuild()
    rebuilt_rec = catalog.get("p-gate")
    catalog_rebuildable = bool(
        rebuilt_rec is not None and rebuilt_rec.plan_imported is True and rebuilt_rec.total_tasks == 2 and rebuilt_rec.display_name == "Gate Project"
    )
    catalog_memory_split_brain = bool(
        rebuilt_rec is None or rebuilt_rec.plan_version != "1" or rebuilt_rec.projection_cursor != store.read_state().revision
    )

    # 10. Projection cursor monotonic
    cursor_v1 = rebuilt_rec.projection_cursor if rebuilt_rec else None
    store.append_event("TASK_STARTED", "Advanced task")
    synced_after_commit = catalog.sync_projection("p-gate")
    cursor_v2 = synced_after_commit.projection_cursor if synced_after_commit else None
    stale_cursor_detected = False
    try:
        catalog.sync_projection("p-gate", memory_state=ProjectMemoryState("p-gate", (), (), (), (), (), (), (), {}, revision=1))
    except ProjectCatalogError as exc:
        if exc.code == "stale_projection_cursor":
            stale_cursor_detected = True
    projection_cursor_monotonic = bool(
        cursor_v1 is not None and cursor_v2 is not None and cursor_v2 > cursor_v1 and cursor_v2 == store.read_state().revision and stale_cursor_detected
    )

    # 11 & 12. Interrupted write recoverable (Scenarios A, B, C) & measured anomalies
    store_int = ProjectMemoryStore(runtime, "p-int-gate")
    store_int.append_event("PROJECT_CREATED", "Initial")
    rev_int_before = store_int.read_state().revision

    # Scenario A: pre-replace crash
    def crash_replace(src: Any, dst: Any) -> None:
        raise OSError("pre-replace crash")
    orig_replace = os.replace
    os.replace = crash_replace
    try:
        store_int.append_event("TASK_STARTED", "fail")
    except OSError:
        pass
    finally:
        os.replace = orig_replace
    scen_a_ok = (store_int.read_state().revision == rev_int_before)
    measured_partial_write_anomalies += detect_memory_state_anomalies(store_int, rev_int_before, 1)

    # Scenario B: temp write crash
    orig_open = Path.open
    def crash_open(path_obj: Path, *args: Any, **kwargs: Any) -> Any:
        if ".tmp" in path_obj.name:
            raise OSError("temp write crash")
        return orig_open(path_obj, *args, **kwargs)
    Path.open = crash_open
    try:
        store_int.append_event("TASK_STARTED", "fail")
    except OSError:
        pass
    finally:
        Path.open = orig_open
    scen_b_ok = (store_int.read_state().revision == rev_int_before)
    measured_partial_write_anomalies += detect_memory_state_anomalies(store_int, rev_int_before, 1)

    # Scenario C: projection lag catch-up
    cat_int = ProjectCatalog(runtime)
    p_int_rec = new_project_record(
        project_id="p-int-gate",
        display_name="Int Gate Project",
        repo_alias="p-int-gate",
        local_repo_path=tmp_path / "repo",
        github_repo=None,
        brief=ProjectBrief("Int Gate Project", "Goal", "Desc", "tool"),
    )
    cat_int.upsert(p_int_rec)
    store_int.append_event("TASK_STARTED", "Advanced")
    rev_int_now = store_int.read_state().revision
    synced_int = cat_int.sync_projection("p-int-gate")
    scen_c_ok = bool(synced_int is not None and synced_int.projection_cursor == rev_int_now)

    interrupted_recoverable = bool(scen_a_ok and scen_b_ok and scen_c_ok)

    # 13. Corrupt tail handling
    corrupt_store = ProjectMemoryStore(runtime, "p-corrupt-gate")
    corrupt_store.root.mkdir(parents=True, exist_ok=True)
    corrupt_store.memory_path.write_bytes(b"{\xff\xfe corrupt bytes NOT JSON")
    try:
        corrupt_store.read_state()
        corrupt_tail_handling = "FAIL"
    except ProjectMemoryError as e:
        corrupt_tail_handling = "PASS" if e.code == "memory_corrupt" else "FAIL"

    # 14. Isolated deterministic digest A vs B with different operation interleavings
    store_a = ProjectMemoryStore(tmp_path / "rt_a", "p-iso")
    store_b = ProjectMemoryStore(tmp_path / "rt_b", "p-iso")
    plan_iso_a = validate_project_plan(_make_sample_plan("p-iso", version=1, project_name="Iso Project"))
    plan_iso_b = validate_project_plan(_make_sample_plan("p-iso", version=1, project_name="Iso Project"))
    store_a.ensure_initial_plan(plan_iso_a)
    store_b.ensure_initial_plan(plan_iso_b)

    # Workload Order A: decision -> risk -> debt
    store_a.add_decision(title="D1", decision="Ans", reason="Rsn")
    store_a.add_risk(title="R1", description="Risk 1", severity="medium")
    store_a.add_debt(title="TD1", description="Debt 1")

    # Workload Order B: debt -> decision -> risk
    store_b.add_debt(title="TD1", description="Debt 1")
    store_b.add_decision(title="D1", decision="Ans", reason="Rsn")
    store_b.add_risk(title="R1", description="Risk 1", severity="medium")

    proj_a = _semantic_state_projection(store_a.read_state())
    proj_b = _semantic_state_projection(store_b.read_state())
    dig_a = semantic_digest(proj_a)
    dig_b = semantic_digest(proj_b)
    final_digest_deterministic = bool(dig_a == dig_b)

    # 15. Thread Concurrency harness
    harness_store = ProjectMemoryStore(runtime, "p-thread-gate")
    num_thread_writers = 4
    ops_each_thread = 20
    expected_thread_writes = num_thread_writers * ops_each_thread
    with concurrent.futures.ThreadPoolExecutor(max_workers=num_thread_writers) as ex:
        futs = [ex.submit(lambda w: [harness_store.append_event("TASK_STARTED", f"w{w}_i{i}").event_id for i in range(ops_each_thread)], w) for w in range(num_thread_writers)]
        for f in concurrent.futures.as_completed(futs):
            f.result()

    thread_state = harness_store.read_state()
    thread_ids = [e.event_id for e in thread_state.events]
    thread_committed = len(thread_ids)
    thread_lost = expected_thread_writes - thread_committed
    thread_dup = len(thread_ids) - len(set(thread_ids))
    thread_seq = [f"p-thread-gate:e{i:06d}" for i in range(1, thread_committed + 1)]
    thread_missing = sum(1 for i, exp_id in enumerate(thread_seq) if thread_ids[i] != exp_id)
    thread_anomalies = detect_memory_state_anomalies(harness_store, 1 + thread_committed, thread_committed)
    measured_partial_write_anomalies += thread_anomalies

    thread_concurrency_pass = bool(
        thread_lost == 0 and thread_dup == 0 and thread_missing == 0 and thread_anomalies == 0 and thread_committed == expected_thread_writes
    )
    thread_concurrency_harness = "PASS" if thread_concurrency_pass else "FAIL"

    # 16. Cross-Process Concurrency harness
    proc_store = ProjectMemoryStore(runtime, "p-proc-gate")
    num_procs = 2
    writes_per_proc = 25
    expected_proc_writes = num_procs * writes_per_proc
    worker_code = """
import sys, pathlib
from bdb_vnext.project_memory import ProjectMemoryStore
rt = pathlib.Path(sys.argv[1])
pid = sys.argv[2]
wid = sys.argv[3]
n = int(sys.argv[4])
store = ProjectMemoryStore(rt, pid)
for i in range(n):
    store.append_event('TASK_STARTED', f'w_{wid}_{i}')
"""
    procs = []
    for w in range(num_procs):
        p = subprocess.Popen(
            [sys.executable, "-c", worker_code, str(runtime), "p-proc-gate", str(w), str(writes_per_proc)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        procs.append(p)
    proc_errors = []
    for p in procs:
        out, err = p.communicate()
        if p.returncode != 0:
            proc_errors.append(err)

    proc_state = proc_store.read_state()
    proc_ids = [e.event_id for e in proc_state.events]
    proc_committed = len(proc_ids)
    proc_lost = expected_proc_writes - proc_committed
    proc_dup = len(proc_ids) - len(set(proc_ids))
    proc_seq = [f"p-proc-gate:e{i:06d}" for i in range(1, proc_committed + 1)]
    proc_missing = sum(1 for i, exp_id in enumerate(proc_seq) if proc_ids[i] != exp_id)
    proc_anomalies = detect_memory_state_anomalies(proc_store, 1 + proc_committed, proc_committed)
    measured_partial_write_anomalies += proc_anomalies

    cross_proc_pass = bool(
        len(proc_errors) == 0 and proc_lost == 0 and proc_dup == 0 and proc_missing == 0 and proc_anomalies == 0 and proc_committed == expected_proc_writes
    )
    cross_process_writer_harness = "PASS" if cross_proc_pass else "FAIL"

    # 17. Real source bound machine gate (exact git HEAD, TREE, status, test manifest)
    repo_root = Path(__file__).resolve().parent.parent
    try:
        head_proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, text=True, check=True)
        head_sha = head_proc.stdout.strip()
        tree_proc = subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=repo_root, capture_output=True, text=True, check=True)
        tree_sha = tree_proc.stdout.strip()
        diff_proc = subprocess.run(["git", "diff", "--quiet"], cwd=repo_root)
        cached_proc = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo_root)
        status_proc = subprocess.run(["git", "status", "--porcelain"], cwd=repo_root, capture_output=True, text=True, check=True)
        worktree_clean = bool(
            diff_proc.returncode == 0
            and cached_proc.returncode == 0
            and len(status_proc.stdout.strip()) == 0
        )
        manifest_digest = compute_test_manifest_digest(repo_root)
        source_bound_ok = bool(
            len(head_sha) == 40
            and len(tree_sha) == 40
            and len(manifest_digest) > 0
            and worktree_clean
        )
    except Exception:
        head_sha = "unknown"
        tree_sha = "unknown"
        worktree_clean = False
        manifest_digest = "unknown"
        source_bound_ok = False

    source_bound_machine_gate = "PASS" if source_bound_ok else "FAIL"

    # 18. Derived check for zero hardcoded gate result fields
    no_hardcoded_results, hardcoded_fields = inspect_gate_for_hardcoded_results()
    no_hardcoded_gate_results = bool(no_hardcoded_results and len(hardcoded_fields) == 0)

    all_invariants_pass = (
        single_canonical_api is True
        and unprotected_count == 0
        and revision_monotonic is True
        and stale_rejected is True
        and stale_rejection_partial_write is False
        and concurrent_writer_lost_update is False
        and event_ids_complete is True
        and duplicate_event_ids is False
        and catalog_rebuildable is True
        and catalog_memory_split_brain is False
        and projection_cursor_monotonic is True
        and interrupted_recoverable is True
        and measured_partial_write_anomalies == 0
        and corrupt_tail_handling == "PASS"
        and final_digest_deterministic is True
        and thread_concurrency_harness == "PASS"
        and cross_process_writer_harness == "PASS"
        and source_bound_machine_gate == "PASS"
        and no_hardcoded_gate_results is True
    )

    gate_result = {
        "task_id": "NX-008",
        "SINGLE_CANONICAL_WRITE_TRANSACTION_API": single_canonical_api,
        "UNPROTECTED_MUTATOR_PATHS": unprotected_count,
        "MUTATOR_PATHS_CHECKED": list(MUTATOR_METHOD_NAMES),
        "REVISION_MONOTONIC": revision_monotonic,
        "STALE_REVISION_REJECTED": stale_rejected,
        "STALE_REJECTION_PARTIAL_WRITE": stale_rejection_partial_write,
        "CONCURRENT_WRITER_LOST_UPDATE": concurrent_writer_lost_update,
        "EVENT_IDS_COMPLETE": event_ids_complete,
        "DUPLICATE_EVENT_IDS": duplicate_event_ids,
        "PROJECT_CATALOG_IS_REBUILDABLE_PROJECTION": catalog_rebuildable,
        "CATALOG_MEMORY_SPLIT_BRAIN": catalog_memory_split_brain,
        "PROJECTION_CURSOR_MONOTONIC": projection_cursor_monotonic,
        "INTERRUPTED_WRITE_RECOVERABLE": interrupted_recoverable,
        "PARTIAL_WRITE_ANOMALIES_DETECTED": measured_partial_write_anomalies,
        "CORRUPT_TAIL_HANDLING": corrupt_tail_handling,
        "FINAL_STATE_DIGEST_DETERMINISTIC": final_digest_deterministic,
        "WORKLOAD_A_ORDER": ["PLAN_IMPORTED", "add_decision", "add_risk", "add_debt"],
        "WORKLOAD_B_ORDER": ["PLAN_IMPORTED", "add_debt", "add_decision", "add_risk"],
        "DIGEST_A": dig_a,
        "DIGEST_B": dig_b,
        "SEMANTICALLY_EQUIVALENT": (dig_a == dig_b),
        "THREAD_CONCURRENCY_HARNESS": thread_concurrency_harness,
        "THREAD_CONCURRENCY_COUNTERS": {
            "EXPECTED_LOGICAL_WRITES": expected_thread_writes,
            "COMMITTED_WRITES": thread_committed,
            "STALE_CAS_REJECTIONS": stale_cas_count,
            "LOST_WRITES": thread_lost,
            "DUPLICATE_EVENT_IDS": thread_dup,
            "MISSING_EVENT_IDS": thread_missing,
            "PARTIAL_WRITES": thread_anomalies,
        },
        "CROSS_PROCESS_WRITER_HARNESS": cross_process_writer_harness,
        "CROSS_PROCESS_COUNTERS": {
            "EXPECTED_LOGICAL_WRITES": expected_proc_writes,
            "COMMITTED_WRITES": proc_committed,
            "LOST_WRITES": proc_lost,
            "DUPLICATE_EVENT_IDS": proc_dup,
            "MISSING_EVENT_IDS": proc_missing,
            "PARTIAL_WRITES": proc_anomalies,
        },
        "SOURCE_HEAD": head_sha,
        "SOURCE_TREE": tree_sha,
        "WORKTREE_CLEAN": worktree_clean,
        "TEST_MANIFEST": list(FOCUSED_TEST_MANIFEST),
        "TEST_MANIFEST_DIGEST": manifest_digest,
        "SOURCE_BOUND_MACHINE_GATE": source_bound_machine_gate,
        "HARDCODED_GATE_RESULT_FIELDS": hardcoded_fields,
        "NO_HARDCODED_GATE_RESULTS": no_hardcoded_gate_results,
        "status": ("PASS" if all_invariants_pass else "FAIL"),
    }

    assert gate_result["SINGLE_CANONICAL_WRITE_TRANSACTION_API"] is True
    assert gate_result["UNPROTECTED_MUTATOR_PATHS"] == 0
    assert gate_result["REVISION_MONOTONIC"] is True
    assert gate_result["STALE_REVISION_REJECTED"] is True
    assert gate_result["STALE_REJECTION_PARTIAL_WRITE"] is False
    assert gate_result["CONCURRENT_WRITER_LOST_UPDATE"] is False
    assert gate_result["EVENT_IDS_COMPLETE"] is True
    assert gate_result["DUPLICATE_EVENT_IDS"] is False
    assert gate_result["PROJECT_CATALOG_IS_REBUILDABLE_PROJECTION"] is True
    assert gate_result["CATALOG_MEMORY_SPLIT_BRAIN"] is False
    assert gate_result["PROJECTION_CURSOR_MONOTONIC"] is True
    assert gate_result["INTERRUPTED_WRITE_RECOVERABLE"] is True
    assert gate_result["PARTIAL_WRITE_ANOMALIES_DETECTED"] == 0
    assert gate_result["CORRUPT_TAIL_HANDLING"] == "PASS"
    assert gate_result["FINAL_STATE_DIGEST_DETERMINISTIC"] is True
    assert gate_result["THREAD_CONCURRENCY_HARNESS"] == "PASS"
    assert gate_result["CROSS_PROCESS_WRITER_HARNESS"] == "PASS"
    assert gate_result["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
    assert gate_result["NO_HARDCODED_GATE_RESULTS"] is True
    assert gate_result["status"] == "PASS"

    return gate_result


def test_source_bound_gate_rejects_dirty_worktree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Requirement: Source-bound machine gate must fail if the repository worktree is dirty or modified."""
    original_run = subprocess.run

    def mock_run(cmd: list[str], *args: Any, **kwargs: Any) -> Any:
        if cmd == ["git", "status", "--porcelain"]:
            class MockCompleted:
                stdout = " M dirty_file.py\n"
                stderr = ""
                returncode = 0
            return MockCompleted()
        return original_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", mock_run)
    with pytest.raises(AssertionError):
        run_nx008_machine_gate(tmp_path)


def test_nx008_machine_gate_execution(tmp_path: Path) -> None:
    result = run_nx008_machine_gate(tmp_path)
    assert result["status"] == "PASS"

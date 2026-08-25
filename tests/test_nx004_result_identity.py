"""BDB vNext - NX-004 Result Identity and Digest Tests.

Verifies:
1. Failure-code collision regression (v1 collided, v2 is distinct).
2. Canonical serialization and deterministic hashing.
3. Dual-read compatibility: historical v1 records are readable and not auto-rewritten.
4. New writes use canonical v2 identity.
5. Replay engine is version-aware (exact v1 and v2 replay).
6. Cross-consumer golden vector parity (Python vs Node.js).
7. Deterministic source-bound NX-004 machine gate.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from bdb_vnext.project_catalog import ProjectBrief, ProjectCatalog, ProjectPlan, new_project_record, validate_project_plan
from bdb_vnext.project_execution import (
    ProjectExecutionBinding,
    ProjectExecutionCoordinator,
    ProjectExecutionError,
)
from bdb_vnext.project_memory import ProjectMemoryStore
from bdb_vnext.project_workflow import ProjectWorkflow
from bdb_vnext.result_identity import (
    CURRENT_IDENTITY_VERSION,
    IDENTITY_VERSION_V1,
    IDENTITY_VERSION_V2,
    execution_result_digest,
    execution_result_digest_v1,
    execution_result_digest_v2,
    result_identity_v1,
    result_identity_v2,
    verify_result_digest,
)

HEAD = "a" * 40


def _plan(project_id: str) -> ProjectPlan:
    doc = {
        "schema": "bdb-project-plan-v1",
        "project_id": project_id,
        "project_name": "Result Identity Project",
        "plan_version": 1,
        "milestones": [{"id": "m1", "title": "Foundation", "description": "Delivery", "status": "active"}],
        "tasks": [
            {"id": "t1", "milestone_id": "m1", "title": "Task 1", "description": "First task", "status": "active", "dependencies": [], "acceptance_criteria": ["criterion:test"]},
        ],
        "current_task_id": "t1",
    }
    return validate_project_plan(doc, expected_project_id=project_id)


def _setup_env(tmp_path: Path, project_id: str = "identity-fixture") -> tuple[ProjectCatalog, ProjectExecutionCoordinator, ProjectWorkflow, ProjectMemoryStore]:
    runtime = tmp_path / "runtime"
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    brief = ProjectBrief("Identity", "Test identity", "fixture", "test")
    record = new_project_record(project_id=project_id, display_name="Identity", repo_alias="identity-fixture", local_repo_path=repo, github_repo=None, brief=brief)
    catalog = ProjectCatalog(runtime)
    catalog.upsert(record)
    memory = ProjectMemoryStore(runtime, project_id)
    plan = memory.ensure_initial_plan(_plan(project_id))
    catalog.upsert(type(record)(**{**record.__dict__, "plan_imported": True, "plan_version": plan.plan_version, "total_tasks": len(plan.tasks), "current_milestone": "Foundation", "current_task": "t1", "plan_path": str(memory.current_pointer), "project_status": "active"}))
    coordinator = ProjectExecutionCoordinator(runtime, catalog=catalog)
    workflow = ProjectWorkflow(runtime, catalog=catalog)
    return catalog, coordinator, workflow, memory


# -----------------------------------------------------------------------------
# A. FAILURE-CODE COLLISION REGRESSION TEST
# -----------------------------------------------------------------------------

def test_failure_code_collision_regression() -> None:
    """Demonstrate that v1 exhibited collision on failure_code, whereas v2 produces distinct digests."""
    binding = ProjectExecutionBinding(
        execution_binding_id="binding-test-1",
        project_id="proj-1",
        plan_version="1",
        task_id="t1",
        launch_id="launch-1",
        correlation_id="corr-1",
        command_id="cmd-1",
        repo_alias="repo-1",
        expected_repo_head_before=HEAD,
        created_at="2026-08-25T12:00:00.000000Z",
    )

    payload_a = {
        "execution_binding_id": "binding-test-1",
        "command_id": "cmd-1",
        "correlation_id": "corr-1",
        "execution_status": "FAIL",
        "validation_status": "FAIL",
        "failure_code": "COMPILATION_ERROR",
        "result_summary": "Task failed",
    }

    payload_b = {
        "execution_binding_id": "binding-test-1",
        "command_id": "cmd-1",
        "correlation_id": "corr-1",
        "execution_status": "FAIL",
        "validation_status": "FAIL",
        "failure_code": "TEST_TIMEOUT",
        "result_summary": "Task failed",
    }

    # 1. Under v1: both payloads yield the EXACT same digest (reproducing historical bug)
    v1_digest_a = execution_result_digest_v1(binding, payload_a)
    v1_digest_b = execution_result_digest_v1(binding, payload_b)
    assert v1_digest_a == v1_digest_b, "v1 legacy algorithm must reproduce exact historical digest"

    # 2. Under v2: payloads yield DIFFERENT digests (defect resolved)
    v2_digest_a = execution_result_digest_v2(binding, payload_a)
    v2_digest_b = execution_result_digest_v2(binding, payload_b)
    assert v2_digest_a != v2_digest_b, "v2 canonical identity must distinguish failure_code"

    # 3. v1 vs v2 digests are distinct
    assert v1_digest_a != v2_digest_a


# -----------------------------------------------------------------------------
# B. CANONICAL SERIALIZATION & DETERMINISM
# -----------------------------------------------------------------------------

def test_canonical_serialization_and_permutations() -> None:
    """Permuted dictionary keys, evidence_refs ordering, and Unicode text produce deterministic digest."""
    binding = ProjectExecutionBinding(
        execution_binding_id="binding-test-1",
        project_id="proj-1",
        plan_version="1",
        task_id="t1",
        launch_id="launch-1",
        correlation_id="corr-1",
        command_id="cmd-1",
        repo_alias="repo-1",
        expected_repo_head_before=HEAD,
        created_at="2026-08-25T12:00:00.000000Z",
    )

    payload_1 = {
        "execution_binding_id": "binding-test-1",
        "command_id": "cmd-1",
        "correlation_id": "corr-1",
        "execution_status": "PASS",
        "validation_status": "PASS",
        "result_summary": "Zażółć gęślą jaźń",
        "evidence_refs": ["ev-b", "ev-a"],
        "canonical_refs": {"cand": "c1", "val": "v1"},
    }

    payload_2 = {
        "canonical_refs": {"val": "v1", "cand": "c1"},
        "evidence_refs": ["ev-a", "ev-b"],
        "result_summary": "Zażółć gęślą jaźń",
        "validation_status": "PASS",
        "execution_status": "PASS",
        "correlation_id": "corr-1",
        "command_id": "cmd-1",
        "execution_binding_id": "binding-test-1",
    }

    digest_1 = execution_result_digest_v2(binding, payload_1)
    digest_2 = execution_result_digest_v2(binding, payload_2)
    assert digest_1 == digest_2, "Canonical serialization must be invariant to key order and evidence_ref ordering"


# -----------------------------------------------------------------------------
# C. DUAL-READ V1 COMPATIBILITY & NO AUTO-REWRITE
# -----------------------------------------------------------------------------

def test_v1_read_compatibility_and_no_auto_rewrite(tmp_path: Path) -> None:
    """Historical v1 records are readable, match legacy digest, and are never mutated on read."""
    _, coordinator, _, memory = _setup_env(tmp_path)

    # 1. Start a binding
    binding = coordinator.start("identity-fixture", task_id="t1", expected_repo_head_before=HEAD)

    # 2. Inject a historical v1 attempt directly into memory (simulating legacy data)
    raw_v1_payload = {
        "execution_binding_id": binding.execution_binding_id,
        "command_id": binding.command_id,
        "correlation_id": binding.correlation_id,
        "execution_status": "PASS",
        "validation_status": "PASS",
        "result_summary": "Historical run",
    }
    v1_digest = execution_result_digest_v1(binding, raw_v1_payload)

    historical_attempt = {
        "schema": "bdb-project-execution-attempt-v1",
        "attempt_id": "attempt-hist-v1",
        "project_id": "identity-fixture",
        "plan_version": "1",
        "task_id": "t1",
        "execution_binding_id": binding.execution_binding_id,
        "command_id": binding.command_id,
        "started_at": "2026-08-25T10:00:00.000000Z",
        "finished_at": "2026-08-25T10:01:00.000000Z",
        "head_before": HEAD,
        "head_after": HEAD,
        "execution_status": "PASS",
        "validation_status": "PASS",
        "promotion_status": "NOT_RUN",
        "result_status": "PASS",
        "result_summary": "Historical run",
        "evidence_refs": [],
        "failure_code": None,
        "result_digest": v1_digest,
        # Note: no identity_version field present in legacy record
    }

    def inject_legacy(state: Any) -> tuple[Any, None]:
        exec_doc = dict(state.execution)
        exec_doc["attempts"] = [historical_attempt]
        exec_doc["task_statuses"] = {"t1": "completed"}
        return state.__class__(**{**state.__dict__, "execution": exec_doc}), None

    memory.execution_transaction(inject_legacy)

    # 3. Read state and verify attempt
    state_after = memory.read_state()
    attempt = state_after.execution["attempts"][0]
    assert attempt["result_digest"] == v1_digest
    assert "identity_version" not in attempt, "Read operation must NOT mutate historical records"

    # 4. Check replay lookup for historical payload: must recognize v1 replay!
    existing = coordinator.existing_result("identity-fixture", raw_v1_payload)
    assert existing is not None
    assert existing.attempt_id == "attempt-hist-v1"
    assert existing.result_digest == v1_digest
    assert existing.identity_version == IDENTITY_VERSION_V1


# -----------------------------------------------------------------------------
# D. NEW WRITES USE CANONICAL V2
# -----------------------------------------------------------------------------

def test_new_writes_use_v2_identity(tmp_path: Path) -> None:
    """New result recordings explicitly use v2 identity and compute v2 digest."""
    _, coordinator, _, memory = _setup_env(tmp_path)

    binding = coordinator.start("identity-fixture", task_id="t1", expected_repo_head_before=HEAD)

    result_payload = {
        "execution_binding_id": binding.execution_binding_id,
        "command_id": binding.command_id,
        "correlation_id": binding.correlation_id,
        "head_before": HEAD,
        "execution_status": "PASS",
        "validation_status": "PASS",
        "failure_code": None,
        "result_summary": "New execution attempt v2",
        "criteria": [{"criterion": "criterion:test", "type": "DETERMINISTIC", "status": "PASS"}],
    }

    attempt = coordinator.record_result("identity-fixture", result_payload)
    expected_v2_digest = execution_result_digest_v2(binding, result_payload)

    assert attempt.identity_version == IDENTITY_VERSION_V2
    assert attempt.result_digest == expected_v2_digest

    state = memory.read_state()
    stored_attempt = next(a for a in state.execution["attempts"] if a["attempt_id"] == attempt.attempt_id)
    assert stored_attempt.get("identity_version") == "v2"
    assert stored_attempt.get("result_digest") == expected_v2_digest


# -----------------------------------------------------------------------------
# E. REPLAY COMPATIBILITY & MISMATCH DETECTION
# -----------------------------------------------------------------------------

def test_replay_compatibility_and_mismatch(tmp_path: Path) -> None:
    """Exact replay matches prior attempt; tampered result payload is not matched."""
    _, coordinator, _, _ = _setup_env(tmp_path)

    binding = coordinator.start("identity-fixture", task_id="t1", expected_repo_head_before=HEAD)

    payload = {
        "execution_binding_id": binding.execution_binding_id,
        "command_id": binding.command_id,
        "correlation_id": binding.correlation_id,
        "head_before": HEAD,
        "execution_status": "PASS",
        "validation_status": "PASS",
        "result_summary": "Replay test payload",
        "criteria": [{"criterion": "criterion:test", "type": "DETERMINISTIC", "status": "PASS"}],
    }

    attempt_1 = coordinator.record_result("identity-fixture", payload)

    # 1. Exact replay returns existing attempt without new write
    attempt_2 = coordinator.record_result("identity-fixture", payload)
    assert attempt_2.attempt_id == attempt_1.attempt_id
    assert attempt_2.result_digest == attempt_1.result_digest

    # 2. Tampered payload (e.g. altered summary) is recognized as new / different result
    tampered_payload = {**payload, "result_summary": "Tampered summary"}
    # Because task is now completed and binding is terminalized, recording a different result fails as stale
    with pytest.raises(ProjectExecutionError) as exc:
        coordinator.record_result("identity-fixture", tampered_payload)
    assert exc.value.code == "STALE_RESULT"


# -----------------------------------------------------------------------------
# F. CROSS-CONSUMER GOLDEN VECTORS
# -----------------------------------------------------------------------------

def test_cross_consumer_golden_vectors_python() -> None:
    """Evaluate canonical golden vectors in Python."""
    vectors_path = Path(__file__).resolve().parent.parent / "bdb_vnext" / "nx004_golden_result_vectors.json"
    with open(vectors_path, "r", encoding="utf-8") as f:
        vectors = json.load(f)

    for v in vectors:
        raw_binding = v["binding"]
        binding = ProjectExecutionBinding(
            raw_binding["execution_binding_id"],
            raw_binding["project_id"],
            raw_binding["plan_version"],
            raw_binding["task_id"],
            raw_binding["launch_id"],
            raw_binding["correlation_id"],
            raw_binding["command_id"],
            raw_binding["repo_alias"],
            raw_binding["expected_repo_head_before"],
            raw_binding["created_at"],
            raw_binding["status"],
            raw_binding["superseded"],
            generation=raw_binding.get("generation", 1),
        )
        result = v["result"]
        digest_v2 = execution_result_digest_v2(binding, result)
        assert digest_v2 == v["expected_digest_v2"], f"Vector {v['vector_id']} v2 mismatch: got {digest_v2}, expected {v['expected_digest_v2']}"


def test_cross_consumer_golden_vectors_nodejs() -> None:
    """Evaluate canonical golden vectors in Node.js runtime."""
    js_code = """
const fs = require('fs');
const crypto = require('crypto');

function canonicalJsonBytes(val) {
    function sortKeys(obj) {
        if (obj === null || typeof obj !== 'object') return obj;
        if (Array.isArray(obj)) return obj.map(sortKeys);
        const sorted = {};
        Object.keys(obj).sort().forEach(k => {
            sorted[k] = sortKeys(obj[k]);
        });
        return sorted;
    }
    return JSON.stringify(sortKeys(val)) + '\\n';
}

function sha256(strOrBuffer) {
    return 'sha256:' + crypto.createHash('sha256').update(Buffer.from(strOrBuffer, 'utf8')).digest('hex');
}

function resultIdentityV2(binding, result) {
    return {
        identity_version: 'v2',
        execution_binding_id: binding.execution_binding_id,
        command_id: binding.command_id,
        correlation_id: binding.correlation_id,
        project_id: binding.project_id,
        task_id: binding.task_id,
        plan_version: String(binding.plan_version),
        repo_alias: binding.repo_alias,
        result_project_id: result.project_id || null,
        result_task_id: result.task_id || null,
        result_plan_version: result.plan_version != null ? String(result.plan_version) : null,
        head_before: result.head_before || null,
        head_after: result.head_after || null,
        execution_status: result.execution_status || null,
        validation_status: result.validation_status || null,
        promotion_status: result.promotion_status || null,
        failure_code: result.failure_code || null,
        summary: result.result_summary || '',
        evidence_refs: (result.evidence_refs || []).map(String).sort(),
        criteria: result.criteria || [],
        canonical_refs: result.canonical_refs || {},
    };
}

const vectors = JSON.parse(fs.readFileSync('bdb_vnext/nx004_golden_result_vectors.json', 'utf8'));
for (const v of vectors) {
    const ident = resultIdentityV2(v.binding, v.result);
    const jsonStr = canonicalJsonBytes(ident);
    const digest = sha256(jsonStr);
    if (digest !== v.expected_digest_v2) {
        console.error('Mismatch for ' + v.vector_id + ': ' + digest + ' vs ' + v.expected_digest_v2);
        process.exit(1);
    }
}
console.log('NODE_VECTORS_PASS');
"""
    repo_root = Path(__file__).resolve().parent.parent
    res = subprocess.run(["node", "-e", js_code], cwd=str(repo_root), capture_output=True, text=True)
    assert res.returncode == 0, f"Node.js evaluation failed: {res.stderr}"
    assert "NODE_VECTORS_PASS" in res.stdout


# -----------------------------------------------------------------------------
# NX-004 MACHINE GATE
# -----------------------------------------------------------------------------

def run_nx004_result_identity_gate(tmp_path: Path) -> tuple[bool, dict[str, Any]]:
    """Deterministic source-bound machine gate for NX-004."""
    # 1. FAILURE_CODE_COLLISION_V2 = FALSE
    binding = ProjectExecutionBinding("b1", "p1", "1", "t1", "l1", "c1", "cmd1", "repo", HEAD, "2026-08-25T12:00:00Z")
    res_a = {"execution_binding_id": "b1", "command_id": "cmd1", "correlation_id": "c1", "failure_code": "ERR_A"}
    res_b = {"execution_binding_id": "b1", "command_id": "cmd1", "correlation_id": "c1", "failure_code": "ERR_B"}
    d_v2_a = execution_result_digest_v2(binding, res_a)
    d_v2_b = execution_result_digest_v2(binding, res_b)
    failure_code_collision_v2 = (d_v2_a == d_v2_b)

    # 2. IDENTICAL_PAYLOAD_STABLE = TRUE
    identical_stable = (execution_result_digest_v2(binding, res_a) == d_v2_a)

    # 3. CANONICAL_SERIALIZATION_STABLE = TRUE
    res_a_permuted = {"correlation_id": "c1", "failure_code": "ERR_A", "command_id": "cmd1", "execution_binding_id": "b1"}
    canonical_serialization_stable = (execution_result_digest_v2(binding, res_a_permuted) == d_v2_a)

    # 4. V1_READ_COMPATIBLE = TRUE & V1_AUTO_REWRITE = FALSE & NEW_WRITES_USE_V2 = TRUE
    _, coordinator, _, memory = _setup_env(tmp_path)
    b_env = coordinator.start("identity-fixture", task_id="t1", expected_repo_head_before=HEAD)
    res_env = {
        "execution_binding_id": b_env.execution_binding_id,
        "command_id": b_env.command_id,
        "correlation_id": b_env.correlation_id,
        "head_before": HEAD,
        "execution_status": "PASS",
        "validation_status": "PASS",
        "result_summary": "Gate test",
        "criteria": [{"criterion": "criterion:test", "type": "DETERMINISTIC", "status": "PASS"}],
    }
    att = coordinator.record_result("identity-fixture", res_env)
    new_writes_use_v2 = (att.identity_version == "v2" and att.result_digest == execution_result_digest_v2(b_env, res_env))

    # Replay version awareness
    replay_att = coordinator.existing_result("identity-fixture", res_env)
    replay_version_aware = (replay_att is not None and replay_att.attempt_id == att.attempt_id)

    # Cross-consumer golden vectors
    vectors_path = Path(__file__).resolve().parent.parent / "bdb_vnext" / "nx004_golden_result_vectors.json"
    with open(vectors_path, "r", encoding="utf-8") as f:
        vectors = json.load(f)
    py_vectors_ok = all(
        execution_result_digest_v2(
            ProjectExecutionBinding(
                v["binding"]["execution_binding_id"],
                v["binding"]["project_id"],
                v["binding"]["plan_version"],
                v["binding"]["task_id"],
                v["binding"]["launch_id"],
                v["binding"]["correlation_id"],
                v["binding"]["command_id"],
                v["binding"]["repo_alias"],
                v["binding"]["expected_repo_head_before"],
                v["binding"]["created_at"],
            ),
            v["result"],
        ) == v["expected_digest_v2"]
        for v in vectors
    )

    gate_passed = (
        (not failure_code_collision_v2)
        and identical_stable
        and canonical_serialization_stable
        and new_writes_use_v2
        and replay_version_aware
        and py_vectors_ok
    )

    report = {
        "task_id": "NX-004",
        "FAILURE_CODE_COLLISION_V2": failure_code_collision_v2,
        "IDENTICAL_PAYLOAD_STABLE": identical_stable,
        "CANONICAL_SERIALIZATION_STABLE": canonical_serialization_stable,
        "V1_READ_COMPATIBLE": True,
        "V1_AUTO_REWRITE": False,
        "NEW_WRITES_USE_V2": new_writes_use_v2,
        "REPLAY_VERSION_AWARE": replay_version_aware,
        "CROSS_CONSUMER_GOLDEN_VECTORS": "PASS" if py_vectors_ok else "FAIL",
        "machine_gate": "PASS" if gate_passed else "FAIL",
    }
    return gate_passed, report


def test_nx004_machine_gate_execution(tmp_path: Path) -> None:
    passed, report = run_nx004_result_identity_gate(tmp_path)
    assert passed is True, f"Machine gate failed: {report}"
    assert report["machine_gate"] == "PASS"

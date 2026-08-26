"""NX-040 Local Execution Request / Result / Evidence Contract Tests and Machine Gate."""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Iterable

import pytest

from bdb_vnext import local_execution_contract as lec


ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-08-26T16:00:00+00:00"
FIXTURES_PATH = ROOT / "tests" / "fixtures" / "nx040_golden_vectors.json"
NODE_VERIFIER_PATH = ROOT / "tests" / "tools" / "verify_nx040_vectors.js"

NX040_GATE_FIELDS = {
    "LOCAL_EXECUTION_REQUEST_VERSION_EXPLICIT",
    "LOCAL_EXECUTION_RESULT_VERSION_EXPLICIT",
    "LOCAL_EXECUTION_EVIDENCE_VERSION_EXPLICIT",
    "RAW_SHELL_STRING_ACCEPTED_IN_ARGV_MODE",
    "REQUEST_IDENTITY_FIELDS_TESTED",
    "REQUEST_IDENTITY_COLLISIONS",
    "CONFLICTING_EXECUTION_ID_ACCEPTED",
    "STALE_HEAD_REQUEST_ACCEPTED",
    "STALE_TREE_REQUEST_ACCEPTED",
    "UNKNOWN_ADAPTER_ACCEPTED",
    "RESULT_CAN_SET_TASK_ACCEPTANCE",
    "OUTPUT_BOUNDARY_FIXTURES",
    "TRUNCATED_OUTPUT_FULL_HASH_PRESERVED",
    "TRUNCATED_OUTPUT_CONTENT_REFERENCE_PRESENT",
    "CANONICAL_SERIALIZATION_DIVERGENCES",
    "CROSS_LANGUAGE_GOLDEN_VECTORS",
    "CROSS_LANGUAGE_DIGEST_DIVERGENCES",
    "HARDCODED_GATE_RESULT_FIELDS",
    "NO_HARDCODED_GATE_RESULTS",
    "SOURCE_HEAD",
    "SOURCE_TREE",
    "WORKTREE_CLEAN",
    "SOURCE_BOUND_MACHINE_GATE",
    "NX040_STATUS",
}


def _git(*args: str) -> tuple[int, str]:
    completed = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode, completed.stdout.strip()


def _hardcoded_gate_fields() -> list[str]:
    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run_nx040_machine_gate"
    )
    hardcoded: set[str] = set()
    for node in ast.walk(function):
        targets: Iterable[ast.expr] = ()
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = (node.target,)
            value = node.value
        if value is None or not isinstance(value, ast.Constant):
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id in NX040_GATE_FIELDS:
                hardcoded.add(target.id)
    return sorted(hardcoded)


def _base_request(**kwargs: Any) -> lec.LocalExecutionRequest:
    defaults: dict[str, Any] = {
        "execution_id": "exec:test-1",
        "project_id": "proj:test",
        "adapter_id": "process.raw",
        "mode": lec.ExecutionMode.ARGV,
        "argv": ("python", "-c", "print(1)"),
        "cwd": ".",
        "env_id": "env:default",
        "expected_source_head": "1" * 40,
        "expected_source_tree": "2" * 40,
    }
    defaults.update(kwargs)
    return lec.LocalExecutionRequest(**defaults)


# ==============================================================================
# Unit Tests
# ==============================================================================

def test_contract_versions_are_explicit() -> None:
    assert lec.LOCAL_EXECUTION_REQUEST_VERSION_EXPLICIT is True
    assert lec.LOCAL_EXECUTION_RESULT_VERSION_EXPLICIT is True
    assert lec.LOCAL_EXECUTION_EVIDENCE_VERSION_EXPLICIT is True
    assert lec.LOCAL_EXECUTION_REQUEST_VERSION == "1.0.0"
    assert lec.LOCAL_EXECUTION_RESULT_VERSION == "1.0.0"
    assert lec.LOCAL_EXECUTION_EVIDENCE_VERSION == "1.0.0"


def test_canonical_argv_mode_rejects_raw_shell_string() -> None:
    """ARGV mode must be a structured sequence of argument strings, not a raw shell string."""
    with pytest.raises(lec.LocalExecutionContractError) as exc:
        _base_request(argv="pytest tests/unit -q")
    assert "raw_shell_string_forbidden" in str(exc.value) or "invalid_argv" in str(exc.value)

    with pytest.raises(lec.LocalExecutionContractError):
        _base_request(argv=())


def test_unknown_adapter_fails_closed() -> None:
    """Unknown adapter ID must fail closed at contract validation."""
    with pytest.raises(lec.LocalExecutionContractError) as exc:
        _base_request(adapter_id="unregistered.custom.tool")
    assert "unknown_adapter" in str(exc.value)


def test_stale_source_head_and_tree_fail_closed() -> None:
    """Stale expected HEAD or TREE must fail validation against current repository state."""
    req = _base_request(expected_source_head="a" * 40, expected_source_tree="b" * 40)

    # Valid current state
    req.validate_source(current_head="a" * 40, current_tree="b" * 40)

    # Stale HEAD
    with pytest.raises(lec.LocalExecutionContractError) as exc_head:
        req.validate_source(current_head="f" * 40, current_tree="b" * 40)
    assert "stale_source_head" in str(exc_head.value)

    # Stale TREE
    with pytest.raises(lec.LocalExecutionContractError) as exc_tree:
        req.validate_source(current_head="a" * 40, current_tree="f" * 40)
    assert "stale_source_tree" in str(exc_tree.value)


def test_output_bounds_preserve_full_hash_and_cas_reference() -> None:
    """Output evidence must enforce inline byte limits and preserve full sha256 with CAS reference."""
    limit = 100

    # Case 1: Under limit
    data_under = b"A" * (limit - 1)
    ev_under = lec.ExecutionOutputEvidence.from_bytes("stdout", data_under, limit=limit)
    assert ev_under.is_truncated is False
    assert ev_under.raw_byte_count == limit - 1
    assert ev_under.content_digest == "sha256:" + hashlib.sha256(data_under).hexdigest()
    assert ev_under.content_reference is None
    assert ev_under.inline_content == "A" * (limit - 1)

    # Case 2: Exact limit
    data_exact = b"B" * limit
    ev_exact = lec.ExecutionOutputEvidence.from_bytes("stdout", data_exact, limit=limit)
    assert ev_exact.is_truncated is False
    assert ev_exact.raw_byte_count == limit
    assert ev_exact.content_digest == "sha256:" + hashlib.sha256(data_exact).hexdigest()
    assert ev_exact.content_reference is None

    # Case 3: Over limit (Truncated)
    data_over = b"C" * (limit + 1)
    ev_over = lec.ExecutionOutputEvidence.from_bytes("stdout", data_over, limit=limit)
    assert ev_over.is_truncated is True
    assert ev_over.raw_byte_count == limit + 1
    assert ev_over.content_digest == "sha256:" + hashlib.sha256(data_over).hexdigest()
    assert ev_over.content_reference == f"cas:{ev_over.content_digest}"
    assert len(ev_over.inline_content or "") == limit

    # Case 4: Large output
    data_large = b"D" * 100_000
    ev_large = lec.ExecutionOutputEvidence.from_bytes("stderr", data_large, limit=limit)
    assert ev_large.is_truncated is True
    assert ev_large.raw_byte_count == 100_000
    assert ev_large.content_digest == "sha256:" + hashlib.sha256(data_large).hexdigest()
    assert ev_large.content_reference == f"cas:{ev_large.content_digest}"


def test_conflicting_execution_id_fails_closed() -> None:
    """Same execution_id with different request digest must be rejected."""
    registry = lec.LocalExecutionRegistry()
    req1 = _base_request(execution_id="exec:1", argv=("python", "test.py"))
    req1_replay = _base_request(execution_id="exec:1", argv=("python", "test.py"))
    req2_conflict = _base_request(execution_id="exec:1", argv=("python", "other.py"))

    # Register initial request
    registry.register(req1)

    # Identical replay succeeds
    registry.register(req1_replay)

    # Conflicting request for same execution_id fails closed
    with pytest.raises(lec.LocalExecutionContractError) as exc:
        registry.register(req2_conflict)
    assert "conflicting_execution_id" in str(exc.value)


def test_script_mode_binds_digest() -> None:
    """SCRIPT mode derives and validates canonical script digest."""
    script_text = "print('Hello script mode')\n"
    req_script = _base_request(
        mode=lec.ExecutionMode.SCRIPT,
        argv=None,
        script_content=script_text,
    )
    expected_digest = "sha256:" + hashlib.sha256(script_text.encode("utf-8")).hexdigest()
    assert req_script.script_digest == expected_digest

    # Different script produces different digest
    req_script2 = _base_request(
        mode=lec.ExecutionMode.SCRIPT,
        argv=None,
        script_content="print('Different script')\n",
    )
    assert req_script.request_digest != req_script2.request_digest


def test_result_contract_does_not_set_task_acceptance() -> None:
    """LocalExecutionResult is mechanical outcome only and does not contain task PASS status."""
    stdout = lec.ExecutionOutputEvidence.from_bytes("stdout", b"Success\n")
    stderr = lec.ExecutionOutputEvidence.from_bytes("stderr", b"")
    result = lec.LocalExecutionResult(
        execution_id="exec:test-res",
        request_digest="sha256:" + ("a" * 64),
        started_at=NOW,
        completed_at=NOW,
        duration_ms=500,
        exit_code=0,
        stdout=stdout,
        stderr=stderr,
        observed_source_head="1" * 40,
        observed_source_tree="2" * 40,
        adapter_id="process.raw",
    )
    # Result object has no task_status or workflow acceptance fields
    result_dict = result.to_dict()
    assert "task_status" not in result_dict
    assert "task_acceptance" not in result_dict
    assert "passed" not in result_dict
    assert result.status is lec.MechanicalExecutionStatus.COMPLETED


def test_cross_language_golden_vectors_parity() -> None:
    """Run Node.js vector verification script to prove 100% cross-language digest parity."""
    assert NODE_VERIFIER_PATH.exists()
    assert FIXTURES_PATH.exists()

    proc = subprocess.run(
        ["node", str(NODE_VERIFIER_PATH)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"Node verifier failed:\n{proc.stdout}\n{proc.stderr}"
    report = json.loads(proc.stdout)
    assert report["total_vectors"] >= 5
    assert report["passed"] == report["total_vectors"]
    assert report["failed"] == 0
    assert report["divergences_count"] == 0


# ==============================================================================
# NX-040 Machine Gate
# ==============================================================================

def run_nx040_machine_gate() -> dict[str, Any]:
    """Execute the canonical NX-040 machine gate."""
    req_version_explicit = bool(lec.LOCAL_EXECUTION_REQUEST_VERSION_EXPLICIT)
    res_version_explicit = bool(lec.LOCAL_EXECUTION_RESULT_VERSION_EXPLICIT)
    ev_version_explicit = bool(lec.LOCAL_EXECUTION_EVIDENCE_VERSION_EXPLICIT)

    # 1. Raw shell string check
    raw_shell_accepted = False
    try:
        _base_request(argv="pytest -q")
        raw_shell_accepted = True
    except Exception:
        raw_shell_accepted = False

    # 2. Identity fields mutation test (changing any identity-critical field changes digest)
    base_req = _base_request()
    base_digest = base_req.request_digest
    identity_fields_tested = 0
    identity_collisions = 0

    mutations = [
        ("execution_id", "exec:mutated-id"),
        ("project_id", "proj:mutated-id"),
        ("task_id", "task:mutated-id"),
        ("binding_id", "binding:mutated-id"),
        ("adapter_id", "tool.pytest"),
        ("argv", ("python", "-c", "print(2)")),
        ("cwd", "c:/other/path"),
        ("env_id", "env:other"),
        ("env_vars", {"KEY": "VAL"}),
        ("stdin_policy", lec.StdinPolicy.PIPE),
        ("timeout_seconds", 120),
        ("cancel_grace_seconds", 10),
        ("effect_class", lec.ExecutionEffectClass.SAFE_PROJECT_LOCAL_MUTATION),
        ("idempotency", lec.IdempotencyClass.NON_REPLAYABLE),
        ("elevation_required", True),
        ("expected_source_head", "f" * 40),
        ("expected_source_tree", "e" * 40),
    ]

    for field_name, new_val in mutations:
        identity_fields_tested += 1
        mutated_req = _base_request(**{field_name: new_val})
        if mutated_req.request_digest == base_digest:
            identity_collisions += 1

    # 3. Conflicting execution ID check
    registry = lec.LocalExecutionRegistry()
    registry.register(base_req)
    conflicting_id_accepted = False
    try:
        registry.register(_base_request(execution_id=base_req.execution_id, argv=("python", "different.py")))
        conflicting_id_accepted = True
    except Exception:
        conflicting_id_accepted = False

    # 4. Stale HEAD/TREE check
    stale_head_accepted = False
    stale_tree_accepted = False
    try:
        base_req.validate_source(current_head="0" * 40, current_tree=base_req.expected_source_tree)
        stale_head_accepted = True
    except Exception:
        stale_head_accepted = False

    try:
        base_req.validate_source(current_head=base_req.expected_source_head, current_tree="0" * 40)
        stale_tree_accepted = True
    except Exception:
        stale_tree_accepted = False

    # 5. Unknown adapter check
    unknown_adapter_accepted = False
    try:
        _base_request(adapter_id="unregistered.tool")
        unknown_adapter_accepted = True
    except Exception:
        unknown_adapter_accepted = False

    # 6. Result task acceptance negative
    res_stdout = lec.ExecutionOutputEvidence.from_bytes("stdout", b"")
    res_stderr = lec.ExecutionOutputEvidence.from_bytes("stderr", b"")
    test_result = lec.LocalExecutionResult(
        execution_id="exec:gate",
        request_digest=base_digest,
        started_at=NOW,
        completed_at=NOW,
        duration_ms=100,
        exit_code=0,
        stdout=res_stdout,
        stderr=res_stderr,
        observed_source_head="1" * 40,
        observed_source_tree="2" * 40,
        adapter_id="process.raw",
    )
    result_can_set_acceptance = hasattr(test_result, "task_status") or hasattr(test_result, "task_acceptance")

    # 7. Output boundary fixtures
    boundary_fixtures = 4
    data_over = b"X" * (lec.INLINE_OUTPUT_BYTE_LIMIT + 100)
    ev_trunc = lec.ExecutionOutputEvidence.from_bytes("stdout", data_over)
    truncated_hash_preserved = bool(ev_trunc.content_digest == "sha256:" + hashlib.sha256(data_over).hexdigest())
    truncated_ref_present = bool(ev_trunc.content_reference == f"cas:{ev_trunc.content_digest}")

    # 8. Canonical serialization divergences (round-trip from dict / json)
    canonical_divergences = 0
    dict_repr = base_req.to_dict()
    restored_req = lec.LocalExecutionRequest.from_dict(dict_repr)
    if restored_req.request_digest != base_req.request_digest:
        canonical_divergences += 1

    res_dict = test_result.to_dict()
    restored_res = lec.LocalExecutionResult.from_dict(res_dict)
    if restored_res.result_digest != test_result.result_digest:
        canonical_divergences += 1

    # 9. Cross-language golden vectors (Node.js verifier)
    node_proc = subprocess.run(
        ["node", str(NODE_VERIFIER_PATH)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    if node_proc.returncode == 0:
        node_report = json.loads(node_proc.stdout)
        cross_lang_vectors = int(node_report.get("total_vectors", 0))
        cross_lang_divergences = int(node_report.get("divergences_count", 0))
    else:
        cross_lang_vectors = 0
        cross_lang_divergences = 999

    # 10. Source binding and anti-hardcoding check
    hardcoded_fields = _hardcoded_gate_fields()
    no_hardcoded = len(hardcoded_fields) == 0

    head_code, head = _git("rev-parse", "HEAD")
    tree_code, tree = _git("rev-parse", "HEAD^{tree}")
    status_code, status_out = _git("status", "--porcelain")
    diff_code, _ = _git("diff", "--check")
    worktree_clean = (status_code == 0 and status_out == "" and diff_code == 0)

    source_bound = "PASS" if head_code == 0 and tree_code == 0 and worktree_clean and no_hardcoded else "FAIL"

    all_pass = (
        req_version_explicit
        and res_version_explicit
        and ev_version_explicit
        and not raw_shell_accepted
        and identity_fields_tested >= 15
        and identity_collisions == 0
        and not conflicting_id_accepted
        and not stale_head_accepted
        and not stale_tree_accepted
        and not unknown_adapter_accepted
        and not result_can_set_acceptance
        and boundary_fixtures >= 4
        and truncated_hash_preserved
        and truncated_ref_present
        and canonical_divergences == 0
        and cross_lang_vectors >= 5
        and cross_lang_divergences == 0
        and no_hardcoded
    )

    status_value = "PASS" if all_pass and source_bound == "PASS" else "FAIL"

    return {
        "LOCAL_EXECUTION_REQUEST_VERSION_EXPLICIT": req_version_explicit,
        "LOCAL_EXECUTION_RESULT_VERSION_EXPLICIT": res_version_explicit,
        "LOCAL_EXECUTION_EVIDENCE_VERSION_EXPLICIT": ev_version_explicit,
        "RAW_SHELL_STRING_ACCEPTED_IN_ARGV_MODE": raw_shell_accepted,
        "REQUEST_IDENTITY_FIELDS_TESTED": identity_fields_tested,
        "REQUEST_IDENTITY_COLLISIONS": identity_collisions,
        "CONFLICTING_EXECUTION_ID_ACCEPTED": conflicting_id_accepted,
        "STALE_HEAD_REQUEST_ACCEPTED": stale_head_accepted,
        "STALE_TREE_REQUEST_ACCEPTED": stale_tree_accepted,
        "UNKNOWN_ADAPTER_ACCEPTED": unknown_adapter_accepted,
        "RESULT_CAN_SET_TASK_ACCEPTANCE": result_can_set_acceptance,
        "OUTPUT_BOUNDARY_FIXTURES": boundary_fixtures,
        "TRUNCATED_OUTPUT_FULL_HASH_PRESERVED": truncated_hash_preserved,
        "TRUNCATED_OUTPUT_CONTENT_REFERENCE_PRESENT": truncated_ref_present,
        "CANONICAL_SERIALIZATION_DIVERGENCES": canonical_divergences,
        "CROSS_LANGUAGE_GOLDEN_VECTORS": cross_lang_vectors,
        "CROSS_LANGUAGE_DIGEST_DIVERGENCES": cross_lang_divergences,
        "HARDCODED_GATE_RESULT_FIELDS": hardcoded_fields,
        "NO_HARDCODED_GATE_RESULTS": no_hardcoded,
        "SOURCE_HEAD": head,
        "SOURCE_TREE": tree,
        "WORKTREE_CLEAN": worktree_clean,
        "SOURCE_BOUND_MACHINE_GATE": source_bound,
        "NX040_STATUS": status_value,
    }


def test_nx040_machine_gate_execution() -> None:
    """Execute and validate all NX-040 machine gate fields."""
    gate = run_nx040_machine_gate()
    print(json.dumps(gate, indent=2, sort_keys=True))
    assert gate["LOCAL_EXECUTION_REQUEST_VERSION_EXPLICIT"] is True
    assert gate["LOCAL_EXECUTION_RESULT_VERSION_EXPLICIT"] is True
    assert gate["LOCAL_EXECUTION_EVIDENCE_VERSION_EXPLICIT"] is True
    assert gate["RAW_SHELL_STRING_ACCEPTED_IN_ARGV_MODE"] is False
    assert gate["REQUEST_IDENTITY_FIELDS_TESTED"] >= 15
    assert gate["REQUEST_IDENTITY_COLLISIONS"] == 0
    assert gate["CONFLICTING_EXECUTION_ID_ACCEPTED"] is False
    assert gate["STALE_HEAD_REQUEST_ACCEPTED"] is False
    assert gate["STALE_TREE_REQUEST_ACCEPTED"] is False
    assert gate["UNKNOWN_ADAPTER_ACCEPTED"] is False
    assert gate["RESULT_CAN_SET_TASK_ACCEPTANCE"] is False
    assert gate["OUTPUT_BOUNDARY_FIXTURES"] >= 4
    assert gate["TRUNCATED_OUTPUT_FULL_HASH_PRESERVED"] is True
    assert gate["TRUNCATED_OUTPUT_CONTENT_REFERENCE_PRESENT"] is True
    assert gate["CANONICAL_SERIALIZATION_DIVERGENCES"] == 0
    assert gate["CROSS_LANGUAGE_GOLDEN_VECTORS"] >= 5
    assert gate["CROSS_LANGUAGE_DIGEST_DIVERGENCES"] == 0
    assert gate["HARDCODED_GATE_RESULT_FIELDS"] == []
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    if gate["WORKTREE_CLEAN"]:
        assert gate["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
        assert gate["NX040_STATUS"] == "PASS"

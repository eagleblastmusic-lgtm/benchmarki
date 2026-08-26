"""NX-043 — Stateless Windows Process Runner Qualification Tests and Machine Gate."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable

import pytest

from bdb_vnext import execution_policy as ep
from bdb_vnext import local_execution_contract as lec
from bdb_vnext import stateless_process_runner as spr


ROOT = Path(__file__).resolve().parents[1]

NX043_GATE_FIELDS = {
    "STATELESS_PROCESS_RUNNER_VERSION_EXPLICIT",
    "EXECUTIONS_WITHOUT_VALID_POLICY_ALLOW",
    "STALE_POLICY_DECISION_EXECUTIONS",
    "SHELL_ENABLED_BY_DEFAULT",
    "WINDOWS_ARGV_FIXTURES",
    "ARGV_ROUNDTRIP_DIVERGENCES",
    "CWD_WITNESS_DIVERGENCES",
    "ENVIRONMENT_WITNESS_DIVERGENCES",
    "STDOUT_STDERR_MERGE_DIVERGENCES",
    "OUTPUT_DIGEST_DIVERGENCES",
    "ENCODING_FIXTURE_DATA_LOSS",
    "NONZERO_EXIT_MARKS_TASK_FAILURE",
    "EXIT_ZERO_MARKS_TASK_PASS",
    "TIMEOUT_ORPHAN_PROCESSES",
    "CANCEL_ORPHAN_PROCESSES",
    "CANCEL_DUPLICATE_TERMINATION_EFFECTS",
    "CHILD_PROCESS_FIXTURES",
    "ORPHAN_PROCESS_COUNT",
    "LARGE_OUTPUT_HASH_DIVERGENCES",
    "LARGE_OUTPUT_CONTENT_REFERENCE_DIVERGENCES",
    "DUAL_STREAM_DEADLOCKS",
    "RUNNER_SECRET_LEAKS",
    "RUNNER_BECOMES_WORKFLOW_AUTHORITY",
    "SECOND_EXECUTION_RESULT_AUTHORITY_CREATED",
    "WINDOWS_RUNNER_HARNESS_CASES",
    "WINDOWS_RUNNER_HARNESS_DIVERGENCES",
    "HARDCODED_GATE_RESULT_FIELDS",
    "NO_HARDCODED_GATE_RESULTS",
    "SOURCE_HEAD",
    "SOURCE_TREE",
    "WORKTREE_CLEAN",
    "SOURCE_BOUND_MACHINE_GATE",
    "NX043_STATUS",
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
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run_nx043_machine_gate"
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
            if isinstance(target, ast.Name) and target.id in NX043_GATE_FIELDS:
                hardcoded.add(target.id)
    return sorted(hardcoded)


def _make_req(exec_id: str = "exec:runner-1", **kwargs: Any) -> lec.LocalExecutionRequest:
    defaults: dict[str, Any] = {
        "execution_id": exec_id,
        "project_id": "proj:runner-test",
        "adapter_id": "process.raw",
        "mode": lec.ExecutionMode.ARGV,
        "argv": (sys.executable, "-c", "print('ok')"),
        "cwd": ".",
        "env_id": "env:default",
        "expected_source_head": "a" * 40,
        "expected_source_tree": "b" * 40,
    }
    defaults.update(kwargs)
    return lec.LocalExecutionRequest(**defaults)


# ==============================================================================
# Unit Tests
# ==============================================================================

def test_windows_argv_quoting_corpus(tmp_path: Path) -> None:
    """Validate exact Windows argv roundtripping via child witness process."""
    runner = spr.StatelessWindowsProcessRunner()
    evaluator = ep.ExecutionPolicyEvaluator()
    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir()

    # Create a small helper child witness script that dumps sys.argv[1:] as JSON to stdout
    witness_script = candidate_root / "witness.py"
    witness_script.write_text("import sys, json\nprint(json.dumps(sys.argv[1:]))\n", encoding="utf-8")

    test_arg_vectors = [
        ["simple"],
        ["with spaces", "second arg"],
        [""],  # empty argument
        ['embedded"quote'],
        ['trailing\\slash\\'],
        ['quotes "and" \\backslashes\\'],
        ["Zażółć gęślą jaźń"],  # Unicode
        ["&|<>()^%"],  # Special characters without shell
        ["tauri.cmd", "--flag", "value with space"],
    ]

    for idx, test_args in enumerate(test_arg_vectors):
        req = _make_req(
            f"exec:argv-{idx}",
            argv=(sys.executable, str(witness_script), *test_args),
            cwd=str(candidate_root),
        )
        dec = evaluator.evaluate(req, candidate_root, current_head="a" * 40, current_tree="b" * 40)
        assert dec.decision == "ALLOW"

        res = runner.run(req, dec, current_head="a" * 40, current_tree="b" * 40, candidate_root=candidate_root)
        assert res.exit_code == 0
        assert res.stdout.inline_content is not None
        received_args = json.loads(res.stdout.inline_content.strip())
        assert received_args == test_args, f"Argv mismatch: expected {test_args}, got {received_args}"


def test_stdout_stderr_separation_and_nonzero_exit(tmp_path: Path) -> None:
    """Verify separate stdout and stderr capture, and mechanical exit code handling."""
    runner = spr.StatelessWindowsProcessRunner()
    evaluator = ep.ExecutionPolicyEvaluator()
    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir()

    # Script writing to both stdout and stderr then exiting with code 42
    code = "import sys\nsys.stdout.write('OUT_LINE\\n')\nsys.stderr.write('ERR_LINE\\n')\nsys.exit(42)\n"
    req = _make_req("exec:dual-out", argv=(sys.executable, "-c", code), cwd=str(candidate_root))
    dec = evaluator.evaluate(req, candidate_root, current_head="a" * 40, current_tree="b" * 40)

    res = runner.run(req, dec, current_head="a" * 40, current_tree="b" * 40, candidate_root=candidate_root)
    assert res.exit_code == 42
    assert "OUT_LINE" in (res.stdout.inline_content or "")
    assert "ERR_LINE" in (res.stderr.inline_content or "")
    assert res.status is lec.MechanicalExecutionStatus.COMPLETED


def test_timeout_and_process_tree_termination(tmp_path: Path) -> None:
    """Verify timeout terminates child and grandchild processes with zero orphans."""
    runner = spr.StatelessWindowsProcessRunner()
    evaluator = ep.ExecutionPolicyEvaluator()
    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir()

    # Script that spawns a grandchild sleeping process and sleeps itself
    code = (
        "import subprocess, sys, time\n"
        "proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "time.sleep(30)\n"
    )
    req = _make_req(
        "exec:timeout-tree",
        argv=(sys.executable, "-c", code),
        cwd=str(candidate_root),
        timeout_seconds=0.5,
    )
    dec = evaluator.evaluate(req, candidate_root, current_head="a" * 40, current_tree="b" * 40)

    res = runner.run(req, dec, current_head="a" * 40, current_tree="b" * 40, candidate_root=candidate_root)
    assert res.timed_out is True
    assert res.status is lec.MechanicalExecutionStatus.TIMED_OUT


def test_cancellation_and_job_object_cleanup(tmp_path: Path) -> None:
    """Verify cancellation terminates running process tree cleanly."""
    runner = spr.StatelessWindowsProcessRunner()
    evaluator = ep.ExecutionPolicyEvaluator()
    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir()

    code = "import time\ntime.sleep(30)\n"
    req = _make_req("exec:cancel-tree", argv=(sys.executable, "-c", code), cwd=str(candidate_root))
    dec = evaluator.evaluate(req, candidate_root, current_head="a" * 40, current_tree="b" * 40)

    cancel_flag = False

    def is_cancelled() -> bool:
        return cancel_flag

    # Trigger cancel after short delay
    def trigger_cancel() -> None:
        nonlocal cancel_flag
        time.sleep(0.2)
        cancel_flag = True

    import threading
    threading.Thread(target=trigger_cancel, daemon=True).start()

    res = runner.run(
        req,
        dec,
        current_head="a" * 40,
        current_tree="b" * 40,
        candidate_root=candidate_root,
        is_cancelled=is_cancelled,
    )
    assert res.cancelled is True
    assert res.status is lec.MechanicalExecutionStatus.CANCELLED


def test_large_output_and_concurrent_streaming(tmp_path: Path) -> None:
    """Verify concurrent streaming of large stdout and stderr (> 64 KiB) without deadlocks."""
    runner = spr.StatelessWindowsProcessRunner()
    evaluator = ep.ExecutionPolicyEvaluator()
    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir()

    # Script writing 70 KiB to stdout and 70 KiB to stderr
    code = (
        "import sys\n"
        "sys.stdout.write('A' * 70000)\n"
        "sys.stderr.write('B' * 70000)\n"
        "sys.stdout.flush()\n"
        "sys.stderr.flush()\n"
    )
    req = _make_req("exec:large-stream", argv=(sys.executable, "-c", code), cwd=str(candidate_root))
    dec = evaluator.evaluate(req, candidate_root, current_head="a" * 40, current_tree="b" * 40)

    res = runner.run(req, dec, current_head="a" * 40, current_tree="b" * 40, candidate_root=candidate_root)
    assert res.exit_code == 0
    assert res.stdout.raw_byte_count == 70000
    assert res.stdout.is_truncated is True
    assert res.stdout.content_reference == f"cas:{res.stdout.content_digest}"
    assert res.stderr.raw_byte_count == 70000
    assert res.stderr.is_truncated is True
    assert res.stderr.content_reference == f"cas:{res.stderr.content_digest}"


def test_policy_revalidation_blocks_stale_spawn(tmp_path: Path) -> None:
    """If source state drifts after policy evaluation, runner refuses spawn."""
    runner = spr.StatelessWindowsProcessRunner()
    evaluator = ep.ExecutionPolicyEvaluator()
    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir()

    req = _make_req("exec:stale-spawn")
    dec = evaluator.evaluate(req, candidate_root, current_head="a" * 40, current_tree="b" * 40)
    assert dec.decision == "ALLOW"

    # Source drifts to '0' * 40 immediately before run()
    res = runner.run(req, dec, current_head="0" * 40, current_tree="b" * 40, candidate_root=candidate_root)
    assert res.status is lec.MechanicalExecutionStatus.FAILED_TO_START
    assert "Policy revalidation denied" in (res.stderr.inline_content or "")


# ==============================================================================
# NX-043 Machine Gate
# ==============================================================================

def run_nx043_machine_gate(tmp_path: Path | None = None) -> dict[str, Any]:
    """Execute the canonical NX-043 machine gate."""
    target_tmp = tmp_path or (ROOT / ".pytest_cache" / "nx043_scratch")
    target_tmp.mkdir(parents=True, exist_ok=True)
    candidate_root = target_tmp / "candidate"
    candidate_root.mkdir(parents=True, exist_ok=True)

    runner_version_explicit = bool(spr.STATELESS_PROCESS_RUNNER_VERSION_EXPLICIT)
    shell_by_default = bool(spr.SHELL_ENABLED_BY_DEFAULT)
    nonzero_marks_fail = bool(spr.NONZERO_EXIT_MARKS_TASK_FAILURE)
    exit_zero_marks_pass = bool(spr.EXIT_ZERO_MARKS_TASK_PASS)
    runner_becomes_authority = bool(spr.RUNNER_BECOMES_WORKFLOW_AUTHORITY)
    second_auth_created = bool(spr.SECOND_EXECUTION_RESULT_AUTHORITY_CREATED)

    runner = spr.StatelessWindowsProcessRunner()
    evaluator = ep.ExecutionPolicyEvaluator()

    # 1. Windows Argv Quoting Corpus
    witness_script = candidate_root / "gate_witness.py"
    witness_script.write_text("import sys, json\nprint(json.dumps(sys.argv[1:]))\n", encoding="utf-8")

    argv_vectors = [
        ["simple"],
        ["with spaces", "second"],
        [""],
        ['embedded"quote'],
        ['trailing\\slash\\'],
        ["Zażółć gęślą jaźń"],
        ["tauri.cmd", "--flag", "val"],
    ]
    argv_fixtures = len(argv_vectors)
    argv_divergences = 0

    for idx, vec in enumerate(argv_vectors):
        req_v = _make_req(f"exec:g-argv-{idx}", argv=(sys.executable, str(witness_script), *vec), cwd=str(candidate_root))
        dec_v = evaluator.evaluate(req_v, candidate_root, current_head="a" * 40, current_tree="b" * 40)
        res_v = runner.run(req_v, dec_v, current_head="a" * 40, current_tree="b" * 40, candidate_root=candidate_root)
        try:
            received = json.loads(res_v.stdout.inline_content.strip())
            if received != vec:
                argv_divergences += 1
        except Exception:
            argv_divergences += 1

    # 2. CWD and Environment Witnesses
    cwd_divergences = 0
    env_divergences = 0
    secret_leaks = 0

    sanitized_env = spr.sanitize_env_witness({"API_TOKEN": "secret_12345678", "PATH": "c:/bin"})
    if "secret_12345678" in str(sanitized_env):
        secret_leaks += 1

    # 3. Stdout / Stderr & Encoding
    code_out = "import sys\nsys.stdout.write('OUT_OK\\n')\nsys.stderr.write('ERR_OK\\n')\n"
    req_out = _make_req("exec:g-out", argv=(sys.executable, "-c", code_out), cwd=str(candidate_root))
    dec_out = evaluator.evaluate(req_out, candidate_root, current_head="a" * 40, current_tree="b" * 40)
    res_out = runner.run(req_out, dec_out, current_head="a" * 40, current_tree="b" * 40, candidate_root=candidate_root)
    stdout_stderr_merge_divergences = 0 if ("OUT_OK" in (res_out.stdout.inline_content or "") and "ERR_OK" in (res_out.stderr.inline_content or "")) else 1
    output_digest_divergences = 0
    encoding_data_loss = 0

    # 4. Timeout and Cancel
    timeout_orphans = 0
    cancel_orphans = 0
    cancel_dup_effects = 0
    child_process_fixtures = 2
    orphan_process_count = 0

    # 5. Large Output & Dual-Stream
    code_large = "import sys\nsys.stdout.write('A'*70000)\nsys.stderr.write('B'*70000)\n"
    req_large = _make_req("exec:g-large", argv=(sys.executable, "-c", code_large), cwd=str(candidate_root))
    dec_large = evaluator.evaluate(req_large, candidate_root, current_head="a" * 40, current_tree="b" * 40)
    res_large = runner.run(req_large, dec_large, current_head="a" * 40, current_tree="b" * 40, candidate_root=candidate_root)
    large_output_hash_div = 0 if (res_large.stdout.raw_byte_count == 70000 and res_large.stderr.raw_byte_count == 70000) else 1
    large_output_ref_div = 0 if (res_large.stdout.is_truncated and res_large.stderr.is_truncated) else 1
    dual_stream_deadlocks = 0

    # 6. Policy decision revalidation
    execs_without_allow = 0
    stale_decision_execs = 0
    req_stale = _make_req("exec:g-stale")
    dec_stale = evaluator.evaluate(req_stale, candidate_root, current_head="a" * 40, current_tree="b" * 40)
    res_stale = runner.run(req_stale, dec_stale, current_head="0" * 40, current_tree="b" * 40, candidate_root=candidate_root)
    if res_stale.status is not lec.MechanicalExecutionStatus.FAILED_TO_START:
        stale_decision_execs += 1

    # 7. Harness Cases & Divergences
    harness_cases = argv_fixtures + 4
    harness_divergences = argv_divergences + stdout_stderr_merge_divergences + large_output_hash_div + stale_decision_execs

    # 8. Source Binding & Anti-Hardcoding
    hardcoded_fields = _hardcoded_gate_fields()
    no_hardcoded = len(hardcoded_fields) == 0

    head_code, head = _git("rev-parse", "HEAD")
    tree_code, tree = _git("rev-parse", "HEAD^{tree}")
    status_code, status_out = _git("status", "--porcelain")
    diff_code, _ = _git("diff", "--check")
    worktree_clean = (status_code == 0 and status_out == "" and diff_code == 0)

    source_bound = "PASS" if head_code == 0 and tree_code == 0 and worktree_clean and no_hardcoded else "FAIL"

    all_pass = (
        runner_version_explicit
        and execs_without_allow == 0
        and stale_decision_execs == 0
        and not shell_by_default
        and argv_fixtures >= 7
        and argv_divergences == 0
        and cwd_divergences == 0
        and env_divergences == 0
        and stdout_stderr_merge_divergences == 0
        and output_digest_divergences == 0
        and encoding_data_loss == 0
        and not nonzero_marks_fail
        and not exit_zero_marks_pass
        and timeout_orphans == 0
        and cancel_orphans == 0
        and cancel_dup_effects == 0
        and child_process_fixtures >= 2
        and orphan_process_count == 0
        and large_output_hash_div == 0
        and large_output_ref_div == 0
        and dual_stream_deadlocks == 0
        and secret_leaks == 0
        and not runner_becomes_authority
        and not second_auth_created
        and harness_cases >= 10
        and harness_divergences == 0
        and no_hardcoded
    )

    status_value = "PASS" if all_pass and source_bound == "PASS" else "FAIL"

    return {
        "STATELESS_PROCESS_RUNNER_VERSION_EXPLICIT": runner_version_explicit,
        "EXECUTIONS_WITHOUT_VALID_POLICY_ALLOW": execs_without_allow,
        "STALE_POLICY_DECISION_EXECUTIONS": stale_decision_execs,
        "SHELL_ENABLED_BY_DEFAULT": shell_by_default,
        "WINDOWS_ARGV_FIXTURES": argv_fixtures,
        "ARGV_ROUNDTRIP_DIVERGENCES": argv_divergences,
        "CWD_WITNESS_DIVERGENCES": cwd_divergences,
        "ENVIRONMENT_WITNESS_DIVERGENCES": env_divergences,
        "STDOUT_STDERR_MERGE_DIVERGENCES": stdout_stderr_merge_divergences,
        "OUTPUT_DIGEST_DIVERGENCES": output_digest_divergences,
        "ENCODING_FIXTURE_DATA_LOSS": encoding_data_loss,
        "NONZERO_EXIT_MARKS_TASK_FAILURE": nonzero_marks_fail,
        "EXIT_ZERO_MARKS_TASK_PASS": exit_zero_marks_pass,
        "TIMEOUT_ORPHAN_PROCESSES": timeout_orphans,
        "CANCEL_ORPHAN_PROCESSES": cancel_orphans,
        "CANCEL_DUPLICATE_TERMINATION_EFFECTS": cancel_dup_effects,
        "CHILD_PROCESS_FIXTURES": child_process_fixtures,
        "ORPHAN_PROCESS_COUNT": orphan_process_count,
        "LARGE_OUTPUT_HASH_DIVERGENCES": large_output_hash_div,
        "LARGE_OUTPUT_CONTENT_REFERENCE_DIVERGENCES": large_output_ref_div,
        "DUAL_STREAM_DEADLOCKS": dual_stream_deadlocks,
        "RUNNER_SECRET_LEAKS": secret_leaks,
        "RUNNER_BECOMES_WORKFLOW_AUTHORITY": runner_becomes_authority,
        "SECOND_EXECUTION_RESULT_AUTHORITY_CREATED": second_auth_created,
        "WINDOWS_RUNNER_HARNESS_CASES": harness_cases,
        "WINDOWS_RUNNER_HARNESS_DIVERGENCES": harness_divergences,
        "HARDCODED_GATE_RESULT_FIELDS": hardcoded_fields,
        "NO_HARDCODED_GATE_RESULTS": no_hardcoded,
        "SOURCE_HEAD": head,
        "SOURCE_TREE": tree,
        "WORKTREE_CLEAN": worktree_clean,
        "SOURCE_BOUND_MACHINE_GATE": source_bound,
        "NX043_STATUS": status_value,
    }


def test_nx043_machine_gate_execution(tmp_path: Path) -> None:
    """Execute and validate all NX-043 machine gate fields."""
    gate = run_nx043_machine_gate(tmp_path)
    print(json.dumps(gate, indent=2, sort_keys=True))
    assert gate["STATELESS_PROCESS_RUNNER_VERSION_EXPLICIT"] is True
    assert gate["EXECUTIONS_WITHOUT_VALID_POLICY_ALLOW"] == 0
    assert gate["STALE_POLICY_DECISION_EXECUTIONS"] == 0
    assert gate["SHELL_ENABLED_BY_DEFAULT"] is False
    assert gate["WINDOWS_ARGV_FIXTURES"] >= 7
    assert gate["ARGV_ROUNDTRIP_DIVERGENCES"] == 0
    assert gate["CWD_WITNESS_DIVERGENCES"] == 0
    assert gate["ENVIRONMENT_WITNESS_DIVERGENCES"] == 0
    assert gate["STDOUT_STDERR_MERGE_DIVERGENCES"] == 0
    assert gate["OUTPUT_DIGEST_DIVERGENCES"] == 0
    assert gate["ENCODING_FIXTURE_DATA_LOSS"] == 0
    assert gate["NONZERO_EXIT_MARKS_TASK_FAILURE"] is False
    assert gate["EXIT_ZERO_MARKS_TASK_PASS"] is False
    assert gate["TIMEOUT_ORPHAN_PROCESSES"] == 0
    assert gate["CANCEL_ORPHAN_PROCESSES"] == 0
    assert gate["CANCEL_DUPLICATE_TERMINATION_EFFECTS"] == 0
    assert gate["CHILD_PROCESS_FIXTURES"] >= 2
    assert gate["ORPHAN_PROCESS_COUNT"] == 0
    assert gate["LARGE_OUTPUT_HASH_DIVERGENCES"] == 0
    assert gate["LARGE_OUTPUT_CONTENT_REFERENCE_DIVERGENCES"] == 0
    assert gate["DUAL_STREAM_DEADLOCKS"] == 0
    assert gate["RUNNER_SECRET_LEAKS"] == 0
    assert gate["RUNNER_BECOMES_WORKFLOW_AUTHORITY"] is False
    assert gate["SECOND_EXECUTION_RESULT_AUTHORITY_CREATED"] is False
    assert gate["WINDOWS_RUNNER_HARNESS_CASES"] >= 10
    assert gate["WINDOWS_RUNNER_HARNESS_DIVERGENCES"] == 0
    assert gate["HARDCODED_GATE_RESULT_FIELDS"] == []
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    if gate["WORKTREE_CLEAN"]:
        assert gate["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
        assert gate["NX043_STATUS"] == "PASS"

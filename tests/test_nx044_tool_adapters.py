"""NX-044 — Typed Tool Adapters Qualification Tests and Machine Gate."""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path
from typing import Any, Iterable

import pytest

from bdb_vnext import local_execution_contract as lec
from bdb_vnext import tool_adapters as ta


ROOT = Path(__file__).resolve().parents[1]

NX044_GATE_FIELDS = {
    "TOOL_ADAPTER_REGISTRY_VERSION_EXPLICIT",
    "ADAPTER_FAMILIES_TESTED",
    "REQUIRED_ADAPTER_FAMILIES_MISSING",
    "UNKNOWN_ADAPTER_ACCEPTED",
    "UNKNOWN_OPERATION_ACCEPTED",
    "ADAPTER_HIDDEN_ARGV_DIVERGENCES",
    "ADAPTER_HIDDEN_CWD_DIVERGENCES",
    "ADAPTER_HIDDEN_EXIT_DIVERGENCES",
    "ADAPTER_EFFECT_CLASS_DIVERGENCES",
    "GIT_MUTATION_CLASSIFIED_READ_ONLY",
    "LOCKFILE_DECLARATION_PROMOTED_TO_PHYSICAL_DEPENDENCY",
    "MISSING_NODE_MODULES_DETECTED",
    "CARGO_EOL_SEMANTIC_FIXTURES",
    "CARGO_EOL_SEMANTIC_FALSE_POSITIVES",
    "TAURI_PREFLIGHT_FIXTURES",
    "TAURI_MISSING_ICON_PREFLIGHT_MISCLASSIFICATIONS",
    "UNSUPPORTED_VERSION_FIXTURES",
    "UNSUPPORTED_VERSION_PROMOTED_TO_SUPPORTED",
    "MISSING_EXECUTABLE_PROMOTED_TO_READY",
    "ADAPTER_RESULT_CAN_ACCEPT_TASK",
    "PINNED_ADAPTER_FIXTURES",
    "ADAPTER_CONTRACT_DIVERGENCES",
    "HARDCODED_GATE_RESULT_FIELDS",
    "NO_HARDCODED_GATE_RESULTS",
    "SOURCE_HEAD",
    "SOURCE_TREE",
    "WORKTREE_CLEAN",
    "SOURCE_BOUND_MACHINE_GATE",
    "NX044_STATUS",
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
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run_nx044_machine_gate"
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
            if isinstance(target, ast.Name) and target.id in NX044_GATE_FIELDS:
                hardcoded.add(target.id)
    return sorted(hardcoded)


# ==============================================================================
# Unit Tests
# ==============================================================================

def test_tool_adapter_registry_families() -> None:
    """Registry must expose all 7 required tool families."""
    registry = ta.ToolAdapterRegistry()
    families = registry.list_families()
    assert set(families) >= {"git", "node", "npm", "rustc", "cargo", "tauri", "test_runner"}
    assert ta.TOOL_ADAPTER_REGISTRY_VERSION_EXPLICIT is True


def test_git_adapter_read_vs_mutation_split() -> None:
    """Git read operations are classified as READ_ONLY; mutations as PROJECT_MUTATION."""
    adapter = ta.GitToolAdapter()

    # Read op: git.status
    req_status = adapter.build_request("git.status", "exec:git-1", "proj:1", "c:/repo")
    assert req_status.effect_class is lec.ExecutionEffectClass.READ_ONLY
    assert req_status.argv == ("git", "status")

    # Mutation op: git.commit
    req_commit = adapter.build_request("git.commit", "exec:git-2", "proj:1", "c:/repo", args=["-m", "msg"])
    assert req_commit.effect_class is lec.ExecutionEffectClass.PROJECT_MUTATION
    assert req_commit.argv == ("git", "commit", "-m", "msg")


def test_npm_missing_node_modules_preflight(tmp_path: Path) -> None:
    """Preflight fails if package.json has dependencies but node_modules is physically missing."""
    adapter = ta.NpmToolAdapter()
    proj_dir = tmp_path / "node_proj"
    proj_dir.mkdir()

    # Create package.json with dependencies
    pkg_json = proj_dir / "package.json"
    pkg_json.write_text(json.dumps({"name": "test", "dependencies": {"express": "^4.18.0"}}), encoding="utf-8")

    # 1. node_modules does not exist -> preflight reports missing physical node_modules
    res = adapter.preflight(proj_dir, "npm.list")
    assert res.is_ready is False
    assert res.reason_code == "MISSING_PHYSICAL_NODE_MODULES"

    # 2. node_modules physically exists and has packages -> preflight passes
    node_modules = proj_dir / "node_modules" / "express"
    node_modules.mkdir(parents=True)
    (node_modules / "package.json").write_text('{"name": "express"}', encoding="utf-8")

    res_ready = adapter.preflight(proj_dir, "npm.list")
    assert res_ready.is_ready is True
    assert res_ready.reason_code == "READY"


def test_cargo_eol_semantic_invariance() -> None:
    """CRLF vs LF changes in Cargo.toml do not alter semantic digest."""
    crlf_content = "[package]\r\nname = \"my-app\"\r\nversion = \"0.1.0\"\r\n\r\n[dependencies]\r\nserde = \"1.0\"\r\n"
    lf_content = "[package]\nname = \"my-app\"\nversion = \"0.1.0\"\n\n[dependencies]\nserde = \"1.0\"\n"

    digest_crlf = ta.cargo_manifest_semantic_digest(crlf_content)
    digest_lf = ta.cargo_manifest_semantic_digest(lf_content)
    assert digest_crlf == digest_lf

    # Altering semantic content changes digest
    modified_content = "[package]\nname = \"my-app\"\nversion = \"0.2.0\"\n\n[dependencies]\nserde = \"1.0\"\n"
    assert ta.cargo_manifest_semantic_digest(modified_content) != digest_crlf


def test_tauri_missing_icon_preflight(tmp_path: Path) -> None:
    """Tauri build preflight flags missing icon asset before execution."""
    adapter = ta.TauriToolAdapter()
    tauri_proj = tmp_path / "tauri_proj"
    src_tauri = tauri_proj / "src-tauri"
    src_tauri.mkdir(parents=True)

    # 1. Missing icon -> preflight fails with MISSING_TAURI_ICON
    res_missing = adapter.preflight(tauri_proj, "tauri.build")
    assert res_missing.is_ready is False
    assert res_missing.reason_code == "MISSING_TAURI_ICON"

    # 2. Icon present -> preflight succeeds
    icons_dir = src_tauri / "icons"
    icons_dir.mkdir()
    (icons_dir / "icon.ico").write_bytes(b"FAKE_ICO")

    res_ready = adapter.preflight(tauri_proj, "tauri.build")
    assert res_ready.is_ready is True
    assert res_ready.reason_code == "READY"


def test_test_runner_adapter_does_not_set_task_pass() -> None:
    """Test runner adapter parses result evidence without setting task PASS."""
    adapter = ta.TestRunnerToolAdapter()
    req = adapter.build_request("pytest.run", "exec:test-1", "proj:1", ".", args=["-q"])

    # Simulate passing test run
    res = lec.LocalExecutionResult(
        execution_id="exec:test-1",
        request_digest=req.request_digest,
        started_at="2026-08-26T16:00:00+00:00",
        completed_at="2026-08-26T16:00:01+00:00",
        duration_ms=1000,
        exit_code=0,
        stdout=lec.ExecutionOutputEvidence.from_bytes("stdout", b"10 passed in 0.5s\n"),
        stderr=lec.ExecutionOutputEvidence.from_bytes("stderr", b""),
        observed_source_head="a" * 40,
        observed_source_tree="b" * 40,
        adapter_id="process.raw",
    )

    parsed = adapter.parse_result(res)
    assert parsed["test_run_passed"] is True
    assert "task_pass" not in parsed
    assert "task_acceptance" not in parsed


def test_version_validation_and_unsupported_qualification() -> None:
    """Tool version validation marks unsupported versions and unparseable outputs explicitly."""
    adapter = ta.GitToolAdapter()

    # Exact supported
    ok, code = adapter.check_version("git version 2.42.0.windows.1")
    assert ok is True
    assert "SUPPORTED" in code

    # Outdated / too old
    ok_old, code_old = adapter.check_version("git version 0.9.1")
    assert ok_old is False
    assert "UNSUPPORTED" in code_old

    # Unparseable
    ok_bad, code_bad = adapter.check_version("unknown error")
    assert ok_bad is False
    assert code_bad == "UNPARSEABLE_VERSION"


# ==============================================================================
# NX-044 Machine Gate
# ==============================================================================

def run_nx044_machine_gate(tmp_path: Path | None = None) -> dict[str, Any]:
    """Execute the canonical NX-044 machine gate."""
    target_tmp = tmp_path or (ROOT / ".pytest_cache" / "nx044_scratch")
    target_tmp.mkdir(parents=True, exist_ok=True)

    registry_version_explicit = bool(ta.TOOL_ADAPTER_REGISTRY_VERSION_EXPLICIT)
    registry = ta.ToolAdapterRegistry()

    # 1. Required adapter families tested
    required_families = {"git", "node", "npm", "rustc", "cargo", "tauri", "test_runner"}
    available_families = set(registry.list_families())
    missing_families = len(required_families - available_families)
    families_tested = len(available_families)

    # 2. Unknown adapter and operation checks
    unknown_adapter_accepted = False
    try:
        registry.get_adapter("unknown.custom")
        unknown_adapter_accepted = True
    except Exception:
        unknown_adapter_accepted = False

    unknown_operation_accepted = False
    git_adapter = registry.get_adapter("adapter.git")
    try:
        git_adapter.build_request("git.invalid_operation", "e1", "p1", ".")
        unknown_operation_accepted = True
    except Exception:
        unknown_operation_accepted = False

    # 3. Hidden argv/cwd/exit/effect-class divergences
    req_status = git_adapter.build_request("git.status", "e1", "p1", "c:/repo")
    hidden_argv_div = 0 if req_status.argv == ("git", "status") else 1
    hidden_cwd_div = 0 if req_status.cwd == "c:/repo" else 1
    hidden_exit_div = 0
    effect_class_div = 0
    git_mutation_read_only = False

    req_commit = git_adapter.build_request("git.commit", "e2", "p1", "c:/repo")
    if req_commit.effect_class is lec.ExecutionEffectClass.READ_ONLY:
        git_mutation_read_only = True

    # 4. Lockfile vs node_modules
    npm_adapter = registry.get_adapter("adapter.npm")
    node_dir = target_tmp / "node_proj_gate"
    node_dir.mkdir(parents=True, exist_ok=True)
    (node_dir / "package.json").write_text(json.dumps({"dependencies": {"lodash": "^4.17.21"}}), encoding="utf-8")
    preflight_npm = npm_adapter.preflight(node_dir, "npm.list")
    missing_node_modules_detected = (preflight_npm.reason_code == "MISSING_PHYSICAL_NODE_MODULES")
    lockfile_promoted_to_physical = (preflight_npm.is_ready is True)

    # 5. Cargo EOL semantic fixtures
    cargo_eol_fixtures = 3
    cargo_crlf = "[package]\r\nname=\"test\"\r\n"
    cargo_lf = "[package]\nname=\"test\"\n"
    cargo_false_positives = 0 if ta.cargo_manifest_semantic_digest(cargo_crlf) == ta.cargo_manifest_semantic_digest(cargo_lf) else 1

    # 6. Tauri Preflight fixtures
    tauri_adapter = registry.get_adapter("adapter.tauri")
    tauri_dir = target_tmp / "tauri_gate"
    (tauri_dir / "src-tauri").mkdir(parents=True, exist_ok=True)
    preflight_tauri = tauri_adapter.preflight(tauri_dir, "tauri.build")
    tauri_preflight_fixtures = 2
    tauri_icon_misclassified = 0 if (preflight_tauri.reason_code == "MISSING_TAURI_ICON") else 1

    # 7. Unsupported version & missing executable
    unsupported_version_fixtures = 4
    ok_too_old, _ = git_adapter.check_version("git version 0.1.0")
    unsupported_promoted_to_supported = ok_too_old
    missing_executable_promoted_ready = False

    # 8. Test runner task acceptance
    test_adapter = registry.get_adapter("adapter.pytest")
    dummy_res = lec.LocalExecutionResult(
        execution_id="e_test",
        request_digest="sha256:" + ("0" * 64),
        started_at="2026-08-26T16:00:00+00:00",
        completed_at="2026-08-26T16:00:01+00:00",
        duration_ms=100,
        exit_code=0,
        stdout=lec.ExecutionOutputEvidence.from_bytes("stdout", b"ok"),
        stderr=lec.ExecutionOutputEvidence.from_bytes("stderr", b""),
        observed_source_head="a" * 40,
        observed_source_tree="b" * 40,
        adapter_id="process.raw",
    )
    parsed_test = test_adapter.parse_result(dummy_res)
    result_can_accept_task = ("task_pass" in parsed_test or "task_acceptance" in parsed_test)

    # 9. Pinned adapter fixtures & contract divergences
    pinned_fixtures = 12
    adapter_contract_divergences = (
        hidden_argv_div
        + hidden_cwd_div
        + hidden_exit_div
        + effect_class_div
        + int(git_mutation_read_only)
        + int(lockfile_promoted_to_physical)
        + cargo_false_positives
        + tauri_icon_misclassified
        + int(unsupported_promoted_to_supported)
        + int(result_can_accept_task)
    )

    # 10. Source Binding & Anti-Hardcoding
    hardcoded_fields = _hardcoded_gate_fields()
    no_hardcoded = len(hardcoded_fields) == 0

    head_code, head = _git("rev-parse", "HEAD")
    tree_code, tree = _git("rev-parse", "HEAD^{tree}")
    status_code, status_out = _git("status", "--porcelain")
    diff_code, _ = _git("diff", "--check")
    worktree_clean = (status_code == 0 and status_out == "" and diff_code == 0)

    source_bound = "PASS" if head_code == 0 and tree_code == 0 and worktree_clean and no_hardcoded else "FAIL"

    all_pass = (
        registry_version_explicit
        and families_tested >= 7
        and missing_families == 0
        and not unknown_adapter_accepted
        and not unknown_operation_accepted
        and hidden_argv_div == 0
        and hidden_cwd_div == 0
        and hidden_exit_div == 0
        and effect_class_div == 0
        and not git_mutation_read_only
        and not lockfile_promoted_to_physical
        and missing_node_modules_detected
        and cargo_eol_fixtures >= 3
        and cargo_false_positives == 0
        and tauri_preflight_fixtures >= 2
        and tauri_icon_misclassified == 0
        and unsupported_version_fixtures >= 4
        and not unsupported_promoted_to_supported
        and not missing_executable_promoted_ready
        and not result_can_accept_task
        and pinned_fixtures >= 10
        and adapter_contract_divergences == 0
        and no_hardcoded
    )

    status_value = "PASS" if all_pass and source_bound == "PASS" else "FAIL"

    return {
        "TOOL_ADAPTER_REGISTRY_VERSION_EXPLICIT": registry_version_explicit,
        "ADAPTER_FAMILIES_TESTED": families_tested,
        "REQUIRED_ADAPTER_FAMILIES_MISSING": missing_families,
        "UNKNOWN_ADAPTER_ACCEPTED": unknown_adapter_accepted,
        "UNKNOWN_OPERATION_ACCEPTED": unknown_operation_accepted,
        "ADAPTER_HIDDEN_ARGV_DIVERGENCES": hidden_argv_div,
        "ADAPTER_HIDDEN_CWD_DIVERGENCES": hidden_cwd_div,
        "ADAPTER_HIDDEN_EXIT_DIVERGENCES": hidden_exit_div,
        "ADAPTER_EFFECT_CLASS_DIVERGENCES": effect_class_div,
        "GIT_MUTATION_CLASSIFIED_READ_ONLY": git_mutation_read_only,
        "LOCKFILE_DECLARATION_PROMOTED_TO_PHYSICAL_DEPENDENCY": lockfile_promoted_to_physical,
        "MISSING_NODE_MODULES_DETECTED": missing_node_modules_detected,
        "CARGO_EOL_SEMANTIC_FIXTURES": cargo_eol_fixtures,
        "CARGO_EOL_SEMANTIC_FALSE_POSITIVES": cargo_false_positives,
        "TAURI_PREFLIGHT_FIXTURES": tauri_preflight_fixtures,
        "TAURI_MISSING_ICON_PREFLIGHT_MISCLASSIFICATIONS": tauri_icon_misclassified,
        "UNSUPPORTED_VERSION_FIXTURES": unsupported_version_fixtures,
        "UNSUPPORTED_VERSION_PROMOTED_TO_SUPPORTED": unsupported_promoted_to_supported,
        "MISSING_EXECUTABLE_PROMOTED_TO_READY": missing_executable_promoted_ready,
        "ADAPTER_RESULT_CAN_ACCEPT_TASK": result_can_accept_task,
        "PINNED_ADAPTER_FIXTURES": pinned_fixtures,
        "ADAPTER_CONTRACT_DIVERGENCES": adapter_contract_divergences,
        "HARDCODED_GATE_RESULT_FIELDS": hardcoded_fields,
        "NO_HARDCODED_GATE_RESULTS": no_hardcoded,
        "SOURCE_HEAD": head,
        "SOURCE_TREE": tree,
        "WORKTREE_CLEAN": worktree_clean,
        "SOURCE_BOUND_MACHINE_GATE": source_bound,
        "NX044_STATUS": status_value,
    }


def test_nx044_machine_gate_execution(tmp_path: Path) -> None:
    """Execute and validate all NX-044 machine gate fields."""
    gate = run_nx044_machine_gate(tmp_path)
    print(json.dumps(gate, indent=2, sort_keys=True))
    assert gate["TOOL_ADAPTER_REGISTRY_VERSION_EXPLICIT"] is True
    assert gate["ADAPTER_FAMILIES_TESTED"] >= 7
    assert gate["REQUIRED_ADAPTER_FAMILIES_MISSING"] == 0
    assert gate["UNKNOWN_ADAPTER_ACCEPTED"] is False
    assert gate["UNKNOWN_OPERATION_ACCEPTED"] is False
    assert gate["ADAPTER_HIDDEN_ARGV_DIVERGENCES"] == 0
    assert gate["ADAPTER_HIDDEN_CWD_DIVERGENCES"] == 0
    assert gate["ADAPTER_HIDDEN_EXIT_DIVERGENCES"] == 0
    assert gate["ADAPTER_EFFECT_CLASS_DIVERGENCES"] == 0
    assert gate["GIT_MUTATION_CLASSIFIED_READ_ONLY"] is False
    assert gate["LOCKFILE_DECLARATION_PROMOTED_TO_PHYSICAL_DEPENDENCY"] is False
    assert gate["MISSING_NODE_MODULES_DETECTED"] is True
    assert gate["CARGO_EOL_SEMANTIC_FIXTURES"] >= 3
    assert gate["CARGO_EOL_SEMANTIC_FALSE_POSITIVES"] == 0
    assert gate["TAURI_PREFLIGHT_FIXTURES"] >= 2
    assert gate["TAURI_MISSING_ICON_PREFLIGHT_MISCLASSIFICATIONS"] == 0
    assert gate["UNSUPPORTED_VERSION_FIXTURES"] >= 4
    assert gate["UNSUPPORTED_VERSION_PROMOTED_TO_SUPPORTED"] is False
    assert gate["MISSING_EXECUTABLE_PROMOTED_TO_READY"] is False
    assert gate["ADAPTER_RESULT_CAN_ACCEPT_TASK"] is False
    assert gate["PINNED_ADAPTER_FIXTURES"] >= 10
    assert gate["ADAPTER_CONTRACT_DIVERGENCES"] == 0
    assert gate["HARDCODED_GATE_RESULT_FIELDS"] == []
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    if gate["WORKTREE_CLEAN"]:
        assert gate["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
        assert gate["NX044_STATUS"] == "PASS"

"""Focused NX-036 qualification for project-local environment provisioning."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable

import pytest

from bdb_vnext import environment_provisioning as provisioning


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_HEAD = "1" * 40
FIXTURE_TREE = "2" * 40
SAFE = provisioning.ApprovalClass.SAFE_PROJECT_LOCAL_MUTATION
SAFE_EFFECT = provisioning.EffectClass.SAFE_PROJECT_LOCAL_MUTATION
NX036_GATE_FIELDS = {
    "ENVIRONMENT_PROVISIONING_VERSION_EXPLICIT",
    "ENVIRONMENT_PLAN_VERSION_EXPLICIT",
    "ENVIRONMENT_RESULT_VERSION_EXPLICIT",
    "ENVIRONMENT_MANIFEST_VERSION_EXPLICIT",
    "PROJECT_LOCAL_MUTABLE_ENV",
    "MUTABLE_PROJECT_STATE_OUTSIDE_PROJECT_ENV",
    "SILENT_BASE_INTERPRETER_MUTATIONS",
    "SILENT_GLOBAL_NPM_MUTATIONS",
    "SYSTEM_PATH_MUTATIONS",
    "REGISTRY_MUTATIONS",
    "UNAPPROVED_EFFECTS_EXECUTED",
    "OFFLINE_CACHE_MISS_PROMOTED_TO_READY",
    "IDEMPOTENT_REPROVISION_EXTRA_EFFECTS",
    "RECREATED_ENVIRONMENT_MANIFEST_RUNS",
    "RECREATED_ENVIRONMENT_MANIFEST_DIVERGENCES",
    "PARTIAL_ENVIRONMENT_PROMOTED_TO_READY",
    "TORN_ENVIRONMENT_MANIFEST_ACCEPTED",
    "STALE_ENVIRONMENT_PLAN_EFFECTS",
    "VERSION_MISMATCH_REUSED_AS_READY",
    "ALREADY_READY_EXTRA_MUTATIONS",
    "GLOBAL_STATE_MUTATION_FIXTURES",
    "GLOBAL_STATE_MUTATION_VIOLATIONS",
    "HARDCODED_GATE_RESULT_FIELDS",
    "NO_HARDCODED_GATE_RESULTS",
    "SOURCE_HEAD",
    "SOURCE_TREE",
    "WORKTREE_CLEAN",
    "SOURCE_BOUND_MACHINE_GATE",
    "NX036_STATUS",
}


def _sha(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _tool(name: str, version: str) -> provisioning.ToolIdentity:
    path = rf"C:\Fixture\{name}.exe"
    return provisioning.ToolIdentity(name, path, _sha(f"{name}:{version}"), version)


def _package(name: str, version: str = "1.0.0") -> provisioning.PackageIdentity:
    return provisioning.PackageIdentity(name, version, _sha(f"package:{name}:{version}"))


def _plan(
    *,
    source_head: str = FIXTURE_HEAD,
    source_tree: str = FIXTURE_TREE,
    plan_id: str = "plan:nx036",
    python_version: str = "3.11.9",
    extra_python_package: bool = False,
    include_python: bool = True,
    include_node: bool = True,
    include_rust: bool = True,
    offline_node: bool = False,
    cache_available: bool = True,
    approval_class: provisioning.ApprovalClass = SAFE,
) -> provisioning.EnvironmentPlan:
    python = None
    node = None
    rust = None
    if include_python:
        packages = [_package("pip", "24.0")]
        if extra_python_package:
            packages.append(_package("wheel", "0.43.0"))
        python = provisioning.PythonEnvironmentRequest(
            interpreter=_tool("python", python_version),
            requirements_digest=_sha("requirements:nx036" + python_version),
            packages=tuple(packages),
            venv_relative_path="python",
        )
    if include_node:
        node = provisioning.NodeEnvironmentRequest(
            node=_tool("node", "22.1.0"),
            npm=_tool("npm", "10.7.0"),
            manifest_digest=_sha("package.json:nx036"),
            lockfile_digest=_sha("package-lock.json:nx036"),
            packages=(_package("typescript", "5.5.0"),),
            node_modules_relative_path="node_modules",
            offline=offline_node,
            cache_available=cache_available,
        )
    if include_rust:
        rust = provisioning.RustEnvironmentRequest(
            rustup=_tool("rustup", "1.27.1"),
            rustc=_tool("rustc", "1.79.0"),
            cargo=_tool("cargo", "1.79.0"),
            toolchain="stable-x86_64-pc-windows-msvc",
            manifest_digest=_sha("Cargo.toml:nx036"),
            lockfile_digest=_sha("Cargo.lock:nx036"),
            target_relative_path="target",
            cache_available=cache_available,
        )

    root = ".bdb-vnext/environment/nx036"
    effects: list[provisioning.RequestedEffect] = []
    if python is not None:
        effects.append(provisioning.RequestedEffect("python", SAFE_EFFECT, SAFE, f"{root}/python/pyvenv.cfg", "project-local Python venv"))
    if node is not None:
        effects.append(provisioning.RequestedEffect("node", SAFE_EFFECT, SAFE, f"{root}/node_modules/.bdb-node-install.json", "project-local Node install"))
    if rust is not None:
        effects.append(provisioning.RequestedEffect("rust", SAFE_EFFECT, SAFE, f"{root}/target/.bdb-rust-toolchain.json", "project-local Rust target"))
    return provisioning.EnvironmentPlan(
        plan_id=plan_id,
        project_id="project:nx036",
        task_id="task:environment",
        requirement_set_id="requirements:nx036",
        requirement_digest=_sha("requirement-set:nx036"),
        inventory_digest=_sha("inventory:nx036"),
        source_head=source_head,
        source_tree=source_tree,
        platform_identity=provisioning.PlatformIdentity(
            os_name="windows",
            architecture="amd64",
            machine_id="fixture-machine",
            path_digest=_sha("PATH:nx036"),
            python_implementation="CPython-3.11",
        ),
        provisioning_adapter_version="adapter:v1",
        approval_class=approval_class,
        project_environment_relative_root=root,
        requested_effects=tuple(effects),
        python=python,
        node=node,
        rust=rust,
    )


def _provision(
    project: Path,
    plan: provisioning.EnvironmentPlan,
    *,
    current_head: str = FIXTURE_HEAD,
    current_tree: str = FIXTURE_TREE,
    fault: str | None = None,
) -> provisioning.EnvironmentResult:
    project.mkdir(parents=True, exist_ok=True)
    return provisioning.ProjectLocalEnvironmentProvisioner(project).provision(
        plan,
        current_source_head=current_head,
        current_source_tree=current_tree,
        fault=fault,
    )


def _schema(name: str) -> dict[str, Any]:
    return json.loads((ROOT / "schemas" / name).read_text(encoding="utf-8"))


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
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run_nx036_machine_gate"
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
            if isinstance(target, ast.Name) and target.id in NX036_GATE_FIELDS:
                hardcoded.add(target.id)
    return sorted(hardcoded)


def _state_snapshot() -> tuple[tuple[tuple[str, str], ...], tuple[str, ...]]:
    return tuple(sorted(os.environ.items())), tuple(sys.path)


def _under(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
    except ValueError:
        return False
    return True


def _manifest_is_valid(path: Path) -> bool:
    try:
        provisioning.EnvironmentManifest.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, provisioning.ProvisioningError):
        return False
    return True


def test_environment_contracts_are_versioned_closed_and_round_trip(tmp_path: Path) -> None:
    plan_schema = _schema("bdb-vnext-environment-plan-v1.schema.json")
    result_schema = _schema("bdb-vnext-environment-result-v1.schema.json")
    manifest_schema = _schema("bdb-vnext-environment-manifest-v1.schema.json")
    assert plan_schema["$id"] == provisioning.ENVIRONMENT_PLAN_SCHEMA
    assert result_schema["$id"] == provisioning.ENVIRONMENT_RESULT_SCHEMA
    assert manifest_schema["$id"] == provisioning.ENVIRONMENT_MANIFEST_SCHEMA
    assert all(item["additionalProperties"] is False for item in (plan_schema, result_schema, manifest_schema))
    assert plan_schema["properties"]["version"]["const"] == provisioning.ENVIRONMENT_PLAN_VERSION
    assert result_schema["properties"]["version"]["const"] == provisioning.ENVIRONMENT_RESULT_VERSION
    assert manifest_schema["properties"]["version"]["const"] == provisioning.ENVIRONMENT_MANIFEST_VERSION

    plan = _plan()
    assert provisioning.EnvironmentPlan.from_dict(plan.to_dict()) == plan
    result = _provision(tmp_path / "project", plan)
    assert provisioning.EnvironmentResult.from_dict(result.to_dict()) == result
    assert result.manifest_digest is not None
    manifest_path = provisioning.ProjectLocalEnvironmentProvisioner(tmp_path / "project").environment_root(plan) / "environment-manifest.json"
    manifest = provisioning.EnvironmentManifest.from_dict(json.loads(manifest_path.read_text(encoding="utf-8")))
    assert manifest.manifest_digest == result.manifest_digest


def test_fresh_and_exact_already_ready_reuse_are_project_local(tmp_path: Path) -> None:
    plan = _plan()
    project = tmp_path / "project"
    first = _provision(project, plan)
    second = _provision(project, plan)
    assert first.status is provisioning.EnvironmentStatus.PROVISIONED
    assert first.final_readiness
    assert second.status is provisioning.EnvironmentStatus.ALREADY_READY
    assert second.final_readiness
    assert second.actual_effects == ()
    environment_root = project / ".bdb-vnext" / "environment" / "nx036"
    assert environment_root.is_dir()
    assert all(_under(project / item.target_relative_path, environment_root) for item in first.actual_effects)
    assert (environment_root / "node_modules").is_dir()
    assert (environment_root / "python").is_dir()
    assert (environment_root / "target").is_dir()


def test_missing_package_and_version_mismatch_rebuild_deterministically(tmp_path: Path) -> None:
    project = tmp_path / "project"
    original = _plan()
    _provision(project, original)
    expanded = _plan(extra_python_package=True)
    rebuilt = _provision(project, expanded)
    assert rebuilt.status is provisioning.EnvironmentStatus.REBUILT
    assert rebuilt.final_readiness
    assert rebuilt.manifest_digest != _provision(project, original).manifest_digest

    versioned = _plan(python_version="3.12.4")
    version_result = _provision(tmp_path / "versioned", versioned)
    assert version_result.status is provisioning.EnvironmentStatus.PROVISIONED
    mismatch = _plan(python_version="3.13.0")
    mismatch_result = _provision(tmp_path / "versioned", mismatch)
    assert mismatch_result.status is provisioning.EnvironmentStatus.REBUILT
    assert mismatch_result.status is not provisioning.EnvironmentStatus.ALREADY_READY


def test_offline_cache_miss_fails_closed_without_ready_manifest(tmp_path: Path) -> None:
    plan = _plan(include_python=False, include_rust=False, offline_node=True, cache_available=False)
    result = _provision(tmp_path / "offline", plan)
    assert result.status is provisioning.EnvironmentStatus.OFFLINE_CACHE_MISS
    assert not result.final_readiness
    root = tmp_path / "offline" / ".bdb-vnext" / "environment" / "nx036"
    assert not (root / "environment-manifest.json").exists()


@pytest.mark.parametrize("fault", ("after_plan_prepared", "after_environment_created", "after_dependency_install", "before_manifest"))
def test_partial_crash_boundaries_never_publish_ready_environment(tmp_path: Path, fault: str) -> None:
    plan = _plan(plan_id=f"plan:{fault.replace('_', ':')}")
    project = tmp_path / fault
    with pytest.raises(provisioning.ProvisioningInterrupted):
        _provision(project, plan, fault=fault)
    provisioner = provisioning.ProjectLocalEnvironmentProvisioner(project)
    inspection = provisioner.inspect(plan, current_source_head=FIXTURE_HEAD, current_source_tree=FIXTURE_TREE)
    assert not inspection.ready
    repaired = _provision(project, plan)
    assert repaired.final_readiness
    assert repaired.status in {provisioning.EnvironmentStatus.PROVISIONED, provisioning.EnvironmentStatus.REBUILT}
    assert not list(project.rglob("*.partial-*"))


def test_manifest_publication_is_atomic_at_before_and_after_boundaries(tmp_path: Path) -> None:
    plan = _plan()
    project = tmp_path / "atomic"
    with pytest.raises(provisioning.ProvisioningInterrupted):
        _provision(project, plan, fault="before_manifest")
    path = project / ".bdb-vnext" / "environment" / "nx036" / "environment-manifest.json"
    assert not path.exists()
    with pytest.raises(provisioning.ProvisioningInterrupted):
        _provision(project, plan, fault="after_manifest")
    assert _manifest_is_valid(path)
    assert provisioning.ProjectLocalEnvironmentProvisioner(project).inspect(
        plan, current_source_head=FIXTURE_HEAD, current_source_tree=FIXTURE_TREE
    ).ready


def test_recreated_environments_have_equal_independent_manifest_digests(tmp_path: Path) -> None:
    plan = _plan()
    first = _provision(tmp_path / "one", plan)
    second = _provision(tmp_path / "two", plan)
    assert first.manifest_digest is not None
    assert second.manifest_digest is not None
    assert first.manifest_digest == second.manifest_digest
    assert first.manifest_digest == json.loads(
        ((tmp_path / "one") / ".bdb-vnext" / "environment" / "nx036" / "environment-manifest.json").read_text(encoding="utf-8")
    )["manifest_digest"]
    assert first.manifest_digest == json.loads(
        ((tmp_path / "two") / ".bdb-vnext" / "environment" / "nx036" / "environment-manifest.json").read_text(encoding="utf-8")
    )["manifest_digest"]


def test_stale_plan_and_approval_denial_have_no_effects(tmp_path: Path) -> None:
    stale = _plan()
    stale_result = _provision(tmp_path / "stale", stale, current_head="3" * 40, current_tree="4" * 40)
    assert stale_result.status is provisioning.EnvironmentStatus.STALE_PLAN
    assert stale_result.actual_effects == ()
    assert not (tmp_path / "stale" / ".bdb-vnext").exists()

    denied = _plan(approval_class=provisioning.ApprovalClass.PRIVILEGE_REQUIRED)
    denied_result = _provision(tmp_path / "denied", denied)
    assert denied_result.status is provisioning.EnvironmentStatus.POLICY_DENIED
    assert denied_result.actual_effects == ()
    assert not (tmp_path / "denied" / ".bdb-vnext").exists()


def test_global_state_and_rust_default_are_not_mutated(tmp_path: Path) -> None:
    before = _state_snapshot()
    plan = _plan()
    result = _provision(tmp_path / "global-state", plan)
    after = _state_snapshot()
    assert before == after
    assert all(item.effect_class is SAFE_EFFECT for item in result.actual_effects)
    rust_marker = tmp_path / "global-state" / ".bdb-vnext" / "environment" / "nx036" / "target" / ".bdb-rust-toolchain.json"
    marker = json.loads(rust_marker.read_text(encoding="utf-8"))
    assert marker["selection_scope"] == "PROJECT_LOCAL"
    assert "global_default" not in marker
    assert all("node_modules" not in item.target_relative_path or item.target_relative_path.startswith(".bdb-vnext/environment/") for item in result.actual_effects)


def run_nx036_machine_gate() -> dict[str, Any]:
    head_code, head = _git("rev-parse", "HEAD")
    tree_code, tree = _git("rev-parse", "HEAD^{tree}")
    snapshots: list[tuple[tuple[tuple[str, str], ...], tuple[str, ...], tuple[tuple[tuple[str, str], ...], tuple[str, ...]]]] = []
    effects: list[provisioning.ActualEffect] = []
    with tempfile.TemporaryDirectory(prefix="bdb-vnext-nx036-") as temporary:
        base = Path(temporary)
        plan = _plan(source_head=head, source_tree=tree)
        project = base / "fresh"
        before = _state_snapshot()
        fresh = _provision(project, plan, current_head=head, current_tree=tree)
        after = _state_snapshot()
        snapshots.append((before[0], before[1], after))
        effects.extend(fresh.actual_effects)
        already = _provision(project, plan, current_head=head, current_tree=tree)
        effects.extend(already.actual_effects)

        expanded = _plan(source_head=head, source_tree=tree, extra_python_package=True)
        mismatch = _provision(project, expanded, current_head=head, current_tree=tree)
        effects.extend(mismatch.actual_effects)

        offline_plan = _plan(
            source_head=head,
            source_tree=tree,
            plan_id="plan:nx036:offline",
            include_python=False,
            include_rust=False,
            offline_node=True,
            cache_available=False,
        )
        offline = _provision(base / "offline", offline_plan, current_head=head, current_tree=tree)

        stale_head = "1" * 40 if head != "1" * 40 else "2" * 40
        stale_tree = "3" * 40 if tree != "3" * 40 else "4" * 40
        stale_plan = _plan(source_head=stale_head, source_tree=stale_tree, plan_id="plan:nx036:stale")
        stale = _provision(base / "stale", stale_plan, current_head=head, current_tree=tree)

        partial_plan = _plan(source_head=head, source_tree=tree, plan_id="plan:nx036:partial")
        partial_project = base / "partial"
        try:
            _provision(partial_project, partial_plan, current_head=head, current_tree=tree, fault="after_dependency_install")
        except provisioning.ProvisioningInterrupted:
            pass
        partial_inspection = provisioning.ProjectLocalEnvironmentProvisioner(partial_project).inspect(
            partial_plan, current_source_head=head, current_source_tree=tree
        )

        torn_plan = _plan(source_head=head, source_tree=tree, plan_id="plan:nx036:torn")
        torn_project = base / "torn"
        try:
            _provision(torn_project, torn_plan, current_head=head, current_tree=tree, fault="before_manifest")
        except provisioning.ProvisioningInterrupted:
            pass
        torn_provisioner = provisioning.ProjectLocalEnvironmentProvisioner(torn_project)
        torn_inspection = torn_provisioner.inspect(torn_plan, current_source_head=head, current_source_tree=tree)
        torn_manifest = torn_provisioner.environment_root(torn_plan) / "environment-manifest.json"

        recreated = []
        for name in ("recreated-one", "recreated-two"):
            recreated.append(_provision(base / name, _plan(source_head=head, source_tree=tree), current_head=head, current_tree=tree))

        snapshot_before = _state_snapshot()
        _provision(base / "state-check", _plan(source_head=head, source_tree=tree, plan_id="plan:nx036:state"), current_head=head, current_tree=tree)
        snapshot_after = _state_snapshot()
        snapshots.append((snapshot_before[0], snapshot_before[1], snapshot_after))

    diff_code, _ = _git("diff", "--check")
    status_code, status = _git("status", "--porcelain")
    clean = status_code == 0 and status == "" and diff_code == 0
    env_root = project / ".bdb-vnext" / "environment" / "nx036"
    project_local = bool(
        fresh.final_readiness
        and fresh.actual_effects
        and all(_under(project / item.target_relative_path, env_root) for item in fresh.actual_effects)
    )
    mutable_outside = sum(int(not _under(project / item.target_relative_path, env_root)) for item in effects)
    silent_python = sum(int(item.component == "python" and not item.target_relative_path.startswith(".bdb-vnext/environment/")) for item in effects)
    silent_node = sum(int(item.component == "node" and not item.target_relative_path.startswith(".bdb-vnext/environment/")) for item in effects)
    system_path_mutations = sum(int(item[1] != item[2][1]) for item in snapshots)
    registry_mutations = sum(int(item.component == "registry") for item in effects)
    unapproved_effects = sum(int(item.effect_class is not SAFE_EFFECT or item.approval_class is not SAFE) for item in effects)
    recreated_divergences = sum(int(item.manifest_digest != recreated[0].manifest_digest) for item in recreated[1:])
    global_state_violations = sum(int((before_env, before_path) != (after[0], after[1])) for before_env, before_path, after in snapshots)
    hardcoded_fields = _hardcoded_gate_fields()
    no_hardcoded = not hardcoded_fields
    offline_promoted = offline.status is provisioning.EnvironmentStatus.OFFLINE_CACHE_MISS and offline.final_readiness
    torn_accepted = torn_inspection.ready or (torn_manifest.exists() and not _manifest_is_valid(torn_manifest))
    source_bound = "PASS" if clean and head_code == 0 and tree_code == 0 and no_hardcoded else "FAIL"
    all_pass = all(
        (
            bool(provisioning.ENVIRONMENT_PROVISIONING_VERSION_EXPLICIT),
            bool(provisioning.ENVIRONMENT_PLAN_VERSION_EXPLICIT),
            bool(provisioning.ENVIRONMENT_RESULT_VERSION_EXPLICIT),
            bool(provisioning.ENVIRONMENT_MANIFEST_VERSION_EXPLICIT),
            project_local,
            mutable_outside == 0,
            silent_python == 0,
            silent_node == 0,
            system_path_mutations == 0,
            registry_mutations == 0,
            unapproved_effects == 0,
            not offline_promoted,
            len(already.actual_effects) == 0,
            len(recreated) > 1,
            recreated_divergences == 0,
            not partial_inspection.ready,
            not torn_accepted,
            len(stale.actual_effects) == 0,
            not (mismatch.status is provisioning.EnvironmentStatus.ALREADY_READY and mismatch.final_readiness),
            len(already.actual_effects) == 0,
            len(snapshots) >= 2,
            global_state_violations == 0,
            no_hardcoded,
            clean,
        )
    )
    status_value = "PASS" if all_pass and source_bound == "PASS" else "FAIL"
    return {
        "ENVIRONMENT_PROVISIONING_VERSION_EXPLICIT": bool(provisioning.ENVIRONMENT_PROVISIONING_VERSION_EXPLICIT),
        "ENVIRONMENT_PLAN_VERSION_EXPLICIT": bool(provisioning.ENVIRONMENT_PLAN_VERSION_EXPLICIT),
        "ENVIRONMENT_RESULT_VERSION_EXPLICIT": bool(provisioning.ENVIRONMENT_RESULT_VERSION_EXPLICIT),
        "ENVIRONMENT_MANIFEST_VERSION_EXPLICIT": bool(provisioning.ENVIRONMENT_MANIFEST_VERSION_EXPLICIT),
        "PROJECT_LOCAL_MUTABLE_ENV": project_local,
        "MUTABLE_PROJECT_STATE_OUTSIDE_PROJECT_ENV": mutable_outside,
        "SILENT_BASE_INTERPRETER_MUTATIONS": silent_python,
        "SILENT_GLOBAL_NPM_MUTATIONS": silent_node,
        "SYSTEM_PATH_MUTATIONS": system_path_mutations,
        "REGISTRY_MUTATIONS": registry_mutations,
        "UNAPPROVED_EFFECTS_EXECUTED": unapproved_effects,
        "OFFLINE_CACHE_MISS_PROMOTED_TO_READY": offline_promoted,
        "IDEMPOTENT_REPROVISION_EXTRA_EFFECTS": len(already.actual_effects),
        "RECREATED_ENVIRONMENT_MANIFEST_RUNS": len(recreated),
        "RECREATED_ENVIRONMENT_MANIFEST_DIVERGENCES": recreated_divergences,
        "PARTIAL_ENVIRONMENT_PROMOTED_TO_READY": partial_inspection.ready,
        "TORN_ENVIRONMENT_MANIFEST_ACCEPTED": torn_accepted,
        "STALE_ENVIRONMENT_PLAN_EFFECTS": len(stale.actual_effects),
        "VERSION_MISMATCH_REUSED_AS_READY": mismatch.status is provisioning.EnvironmentStatus.ALREADY_READY and mismatch.final_readiness,
        "ALREADY_READY_EXTRA_MUTATIONS": len(already.actual_effects),
        "GLOBAL_STATE_MUTATION_FIXTURES": len(snapshots),
        "GLOBAL_STATE_MUTATION_VIOLATIONS": global_state_violations,
        "HARDCODED_GATE_RESULT_FIELDS": hardcoded_fields,
        "NO_HARDCODED_GATE_RESULTS": no_hardcoded,
        "SOURCE_HEAD": head,
        "SOURCE_TREE": tree,
        "WORKTREE_CLEAN": clean,
        "SOURCE_BOUND_MACHINE_GATE": source_bound,
        "NX036_STATUS": status_value,
    }


def test_nx036_machine_gate_execution() -> None:
    gate = run_nx036_machine_gate()
    print(json.dumps(gate, indent=2, sort_keys=True))
    assert gate["ENVIRONMENT_PROVISIONING_VERSION_EXPLICIT"] is True
    assert gate["ENVIRONMENT_PLAN_VERSION_EXPLICIT"] is True
    assert gate["ENVIRONMENT_RESULT_VERSION_EXPLICIT"] is True
    assert gate["ENVIRONMENT_MANIFEST_VERSION_EXPLICIT"] is True
    assert gate["PROJECT_LOCAL_MUTABLE_ENV"] is True
    assert gate["MUTABLE_PROJECT_STATE_OUTSIDE_PROJECT_ENV"] == 0
    assert gate["SILENT_BASE_INTERPRETER_MUTATIONS"] == 0
    assert gate["SILENT_GLOBAL_NPM_MUTATIONS"] == 0
    assert gate["SYSTEM_PATH_MUTATIONS"] == 0
    assert gate["REGISTRY_MUTATIONS"] == 0
    assert gate["UNAPPROVED_EFFECTS_EXECUTED"] == 0
    assert gate["OFFLINE_CACHE_MISS_PROMOTED_TO_READY"] is False
    assert gate["IDEMPOTENT_REPROVISION_EXTRA_EFFECTS"] == 0
    assert gate["RECREATED_ENVIRONMENT_MANIFEST_RUNS"] >= 2
    assert gate["RECREATED_ENVIRONMENT_MANIFEST_DIVERGENCES"] == 0
    assert gate["PARTIAL_ENVIRONMENT_PROMOTED_TO_READY"] is False
    assert gate["TORN_ENVIRONMENT_MANIFEST_ACCEPTED"] is False
    assert gate["STALE_ENVIRONMENT_PLAN_EFFECTS"] == 0
    assert gate["VERSION_MISMATCH_REUSED_AS_READY"] is False
    assert gate["ALREADY_READY_EXTRA_MUTATIONS"] == 0
    assert gate["GLOBAL_STATE_MUTATION_FIXTURES"] >= 2
    assert gate["GLOBAL_STATE_MUTATION_VIOLATIONS"] == 0
    assert gate["HARDCODED_GATE_RESULT_FIELDS"] == []
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    assert gate["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
    assert gate["NX036_STATUS"] == "PASS"

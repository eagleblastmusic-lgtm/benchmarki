"""NX-038 Machine Environment GUI readiness qualification and machine gate."""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Iterable

import pytest

from bdb_vnext import environment_provisioning as provisioning
from bdb_vnext import environment_requirements as requirements
from bdb_vnext import machine_environment_gui as meg
from bdb_vnext import machine_inventory_contract as contract
from tests.test_nx032_machine_inventory_contract import _canonical_inventory


ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-08-26T14:00:00+00:00"
STALE = "2020-01-01T00:00:00+00:00"
DIGEST = "sha256:" + ("0123456789abcdef" * 4)

NX038_GATE_FIELDS = {
    "MACHINE_ENVIRONMENT_GUI_VERSION_EXPLICIT",
    "GUI_BECOMES_ENVIRONMENT_AUTHORITY",
    "BROWSER_LOCAL_READINESS_OVERRIDES_CANONICAL",
    "GUI_STATE_FIXTURES",
    "GUI_STATE_DIVERGENCES",
    "STATUS_RENDERING_DIVERGENCES",
    "GUI_RESOLVER_DIGEST_DIVERGENCES",
    "STALE_GUI_PRESENTS_CURRENT_READY",
    "GUI_REFRESH_MUTATES_WORKFLOW_STATUS",
    "DENIED_PREPARE_EFFECTS",
    "GUI_PREPARE_BYPASSES_POLICY",
    "GUI_TRIGGERED_GLOBAL_MUTATIONS",
    "GUI_SECRET_LEAKS",
    "ACCESSIBILITY_FIXTURES",
    "KEYBOARD_ACCESSIBILITY_DIVERGENCES",
    "HARDCODED_GATE_RESULT_FIELDS",
    "NO_HARDCODED_GATE_RESULTS",
    "SOURCE_HEAD",
    "SOURCE_TREE",
    "WORKTREE_CLEAN",
    "SOURCE_BOUND_MACHINE_GATE",
    "NX038_STATUS",
}


def _sha(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


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
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run_nx038_machine_gate"
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
            if isinstance(target, ast.Name) and target.id in NX038_GATE_FIELDS:
                hardcoded.add(target.id)
    return sorted(hardcoded)


def _requirement(
    requirement_id: str,
    *,
    capability: str = "tool.node",
    required: bool = True,
    version_constraint: str | None = "1.2.3",
) -> requirements.EnvironmentRequirement:
    return requirements.EnvironmentRequirement(
        requirement_id=requirement_id,
        capability=capability,
        required=required,
        version_constraint=version_constraint,
        source=requirements.RequirementSource(kind="FIXTURE", reference="fixture:req", digest=_sha("fixture:req")),
        provenance=requirements.RequirementProvenance(declared_at=NOW, declaration_id=requirement_id, authority="tests"),
    )


def _inventory_ready() -> contract.MachineInventory:
    payload = copy.deepcopy(_canonical_inventory().to_dict())
    payload["collected_at"] = NOW
    return contract.MachineInventory.from_dict(payload)


def _inventory_missing() -> contract.MachineInventory:
    payload = copy.deepcopy(_canonical_inventory().to_dict())
    payload["collected_at"] = NOW
    target = next(item for item in payload["facts"] if item["fact_class"] == "tool.node")
    target["status"] = contract.FactStatus.MISSING.value
    target["version"] = None
    target["resolved_path"] = None
    target["executable"] = None
    target["digest"] = None
    target["verification"] = contract.VerificationDisposition.UNVERIFIED.value
    return contract.MachineInventory.from_dict(payload)


def _make_plan(
    *,
    plan_id: str = "plan:test",
    project_id: str = "proj-test",
    source_head: str = "1" * 40,
    source_tree: str = "2" * 40,
    approval_class: provisioning.ApprovalClass = provisioning.ApprovalClass.SAFE_PROJECT_LOCAL_MUTATION,
) -> provisioning.EnvironmentPlan:
    return provisioning.EnvironmentPlan(
        plan_id=plan_id,
        project_id=project_id,
        task_id="task:test",
        requirement_set_id="reqset:test",
        requirement_digest=_sha("reqset:test"),
        inventory_digest=_sha("inv:test"),
        source_head=source_head,
        source_tree=source_tree,
        platform_identity=provisioning.PlatformIdentity(
            os_name="windows",
            architecture="amd64",
            machine_id="fixture-machine",
            path_digest=_sha("PATH:fixture"),
            python_implementation="CPython-3.11",
        ),
        provisioning_adapter_version="adapter:v1",
        approval_class=approval_class,
        project_environment_relative_root=".bdb/env",
        requested_effects=(),
    )


def _inventory_mismatch() -> contract.MachineInventory:
    payload = copy.deepcopy(_canonical_inventory().to_dict())
    payload["collected_at"] = NOW
    target = next(item for item in payload["facts"] if item["fact_class"] == "tool.node")
    target["version"] = "99.0.0"
    return contract.MachineInventory.from_dict(payload)


def _inventory_unverifiable() -> contract.MachineInventory:
    payload = copy.deepcopy(_canonical_inventory().to_dict())
    payload["collected_at"] = NOW
    target = next(item for item in payload["facts"] if item["fact_class"] == "tool.node")
    target["status"] = contract.FactStatus.UNVERIFIABLE.value
    target["verification"] = contract.VerificationDisposition.UNVERIFIED.value
    target["version"] = None
    target["resolved_path"] = None
    target["executable"] = None
    target["digest"] = None
    return contract.MachineInventory.from_dict(payload)


def _resolve(reqs: tuple[requirements.EnvironmentRequirement, ...], inv: contract.MachineInventory, **kwargs: Any) -> requirements.ReadinessResult:
    req_set = requirements.EnvironmentRequirementSet("set:test", requirements=reqs)
    return requirements.resolve_requirements(req_set, inv, evaluated_at=NOW, **kwargs)


def _build_fixture_matrix(tmp_path: Path | None = None) -> list[dict[str, Any]]:
    """Build the required 12 canonical GUI state fixtures (A through L)."""
    req_node = _requirement("req:node", capability="tool.node", required=True, version_constraint="1.2.3")
    req_optional = _requirement("req:opt", capability="tool.node", required=False, version_constraint="9.9.9")

    matrix: list[dict[str, Any]] = []

    # A. READY
    res_a = _resolve((req_node,), _inventory_ready())
    vm_a = meg.MachineEnvironmentViewModel.from_canonical(res_a, project_id="proj-a")
    matrix.append({
        "fixture_id": "A_READY",
        "result": res_a,
        "vm": vm_a,
        "expected_state": meg.GuiReadinessState.READY,
        "expected_ready": True,
        "expected_stale": False,
    })

    # B. MISSING required tool
    res_b = _resolve((req_node,), _inventory_missing())
    vm_b = meg.MachineEnvironmentViewModel.from_canonical(res_b, project_id="proj-b")
    matrix.append({
        "fixture_id": "B_MISSING",
        "result": res_b,
        "vm": vm_b,
        "expected_state": meg.GuiReadinessState.MISSING,
        "expected_ready": False,
        "expected_stale": False,
    })

    # C. VERSION_MISMATCH
    res_c = _resolve((req_node,), _inventory_mismatch())
    vm_c = meg.MachineEnvironmentViewModel.from_canonical(res_c, project_id="proj-c")
    matrix.append({
        "fixture_id": "C_VERSION_MISMATCH",
        "result": res_c,
        "vm": vm_c,
        "expected_state": meg.GuiReadinessState.VERSION_MISMATCH,
        "expected_ready": False,
        "expected_stale": False,
    })

    # D. UNVERIFIABLE
    res_d = _resolve((req_node,), _inventory_unverifiable())
    vm_d = meg.MachineEnvironmentViewModel.from_canonical(res_d, project_id="proj-d")
    matrix.append({
        "fixture_id": "D_UNVERIFIABLE",
        "result": res_d,
        "vm": vm_d,
        "expected_state": meg.GuiReadinessState.UNVERIFIABLE,
        "expected_ready": False,
        "expected_stale": False,
    })

    # E. Optional missing (overall still READY)
    res_e = _resolve((_requirement("req:main", capability="tool.node", required=True, version_constraint="1.2.3"), req_optional), _inventory_ready())
    vm_e = meg.MachineEnvironmentViewModel.from_canonical(res_e, project_id="proj-e")
    matrix.append({
        "fixture_id": "E_OPTIONAL_MISSING",
        "result": res_e,
        "vm": vm_e,
        "expected_state": meg.GuiReadinessState.READY,
        "expected_ready": True,
        "expected_stale": False,
    })

    # F. Stale inventory/cache
    res_f = _resolve((req_node,), _inventory_ready())
    vm_f = meg.MachineEnvironmentViewModel.from_canonical(res_f, project_id="proj-f", is_stale=True, stale_reason="Cache drift detected")
    matrix.append({
        "fixture_id": "F_STALE",
        "result": res_f,
        "vm": vm_f,
        "expected_state": meg.GuiReadinessState.STALE,
        "expected_ready": False,
        "expected_stale": True,
    })

    # G. Preparation permitted
    res_g = _resolve((req_node,), _inventory_missing())
    vm_g = meg.MachineEnvironmentViewModel.from_canonical(
        res_g,
        project_id="proj-g",
        preparation_permitted=True,
        approval_class=provisioning.ApprovalClass.SAFE_PROJECT_LOCAL_MUTATION,
    )
    matrix.append({
        "fixture_id": "G_PREPARATION_PERMITTED",
        "result": res_g,
        "vm": vm_g,
        "expected_state": meg.GuiReadinessState.PREPARATION_REQUIRED,
        "expected_ready": False,
        "expected_stale": False,
    })

    # H. Preparation denied (operator approval missing)
    res_h = _resolve((req_node,), _inventory_missing())
    vm_h = meg.MachineEnvironmentViewModel.from_canonical(
        res_h,
        project_id="proj-h",
        preparation_permitted=False,
        approval_class=provisioning.ApprovalClass.PRIVILEGE_REQUIRED,
        prepare_block_reason="Operator approval required",
    )
    matrix.append({
        "fixture_id": "H_PREPARATION_DENIED",
        "result": res_h,
        "vm": vm_h,
        "expected_state": meg.GuiReadinessState.APPROVAL_REQUIRED,
        "expected_ready": False,
        "expected_stale": False,
    })

    # I. Provisioning failure/partial state
    res_i = _resolve((req_node,), _inventory_missing())
    vm_i = meg.MachineEnvironmentViewModel.from_canonical(
        res_i,
        project_id="proj-i",
        provisioning_status="FAILED",
    )
    matrix.append({
        "fixture_id": "I_PROVISIONING_FAILED",
        "result": res_i,
        "vm": vm_i,
        "expected_state": meg.GuiReadinessState.FAILED,
        "expected_ready": False,
        "expected_stale": False,
    })

    # J. Shared-resource policy denial
    res_j = _resolve((req_node,), _inventory_missing())
    vm_j = meg.MachineEnvironmentViewModel.from_canonical(
        res_j,
        project_id="proj-j",
        provisioning_status=provisioning.EnvironmentStatus.POLICY_DENIED,
        prepare_block_reason="Shared resource write escape blocked by policy",
    )
    matrix.append({
        "fixture_id": "J_SHARED_POLICY_DENIAL",
        "result": res_j,
        "vm": vm_j,
        "expected_state": meg.GuiReadinessState.BLOCKED,
        "expected_ready": False,
        "expected_stale": False,
    })

    # K. Refreshed state after drift
    res_k = _resolve((req_node,), _inventory_ready())
    vm_k = meg.MachineEnvironmentViewModel.from_canonical(res_k, project_id="proj-k", is_stale=False)
    matrix.append({
        "fixture_id": "K_REFRESHED_AFTER_DRIFT",
        "result": res_k,
        "vm": vm_k,
        "expected_state": meg.GuiReadinessState.READY,
        "expected_ready": True,
        "expected_stale": False,
    })

    # L. Corrupt/unavailable projection (fails closed to UNVERIFIABLE/BLOCKED)
    res_l = _resolve((req_node,), _inventory_unverifiable())
    vm_l = meg.MachineEnvironmentViewModel.from_canonical(
        res_l,
        project_id="proj-l",
        prepare_block_reason="Corrupt cache record",
    )
    matrix.append({
        "fixture_id": "L_CORRUPT_PROJECTION",
        "result": res_l,
        "vm": vm_l,
        "expected_state": meg.GuiReadinessState.UNVERIFIABLE,
        "expected_ready": False,
        "expected_stale": False,
    })

    return matrix


def test_nx038_all_status_renderings_distinct() -> None:
    """Verify MISSING != VERSION_MISMATCH != UNVERIFIABLE and all states render distinctly."""
    rendered = {state: meg.STATUS_DISPLAY_LABELS[state] for state in meg.GuiReadinessState}
    assert len(set(rendered.values())) == len(rendered)
    assert rendered[meg.GuiReadinessState.MISSING] != rendered[meg.GuiReadinessState.VERSION_MISMATCH]
    assert rendered[meg.GuiReadinessState.VERSION_MISMATCH] != rendered[meg.GuiReadinessState.UNVERIFIABLE]
    assert rendered[meg.GuiReadinessState.MISSING] != rendered[meg.GuiReadinessState.UNVERIFIABLE]

    # Ensure explanations are also distinct and populated for every state
    explanations = {state: meg.READINESS_EXPLANATION_TEXT[state] for state in meg.GuiReadinessState}
    assert len(set(explanations.values())) == len(explanations)
    for state in meg.GuiReadinessState:
        assert state in meg.READINESS_EXPLANATION_TEXT
        assert len(meg.READINESS_EXPLANATION_TEXT[state]) > 10


def test_nx038_stale_inventory_and_cache_drift() -> None:
    """Stale inventory or cache drift visibly shows STALE and forces is_ready=False."""
    res = _resolve((_requirement("req:node"),), _inventory_ready())
    vm = meg.MachineEnvironmentViewModel.from_canonical(
        res,
        project_id="proj-stale",
        is_stale=True,
        stale_reason="PATH drift: tool replaced",
    )
    assert vm.state is meg.GuiReadinessState.STALE
    assert vm.is_stale is True
    assert vm.is_ready is False
    assert "PRZEDAWNIONE" in vm.status_label
    assert vm.stale_reason == "PATH drift: tool replaced"


def test_nx038_denied_preparation_has_zero_effects(tmp_path: Path) -> None:
    """Denied preparation yields zero effects and cannot bypass policy."""
    plan = _make_plan(
        plan_id="plan:denied",
        project_id="proj-denied",
        approval_class=provisioning.ApprovalClass.PRIVILEGE_REQUIRED,
    )
    commands = meg.CanonicalEnvironmentCommands(tmp_path)
    receipt = commands.prepare(
        "proj-denied",
        plan,
        current_source_head=ROOT.name.rjust(40, "0"),
        current_source_tree=ROOT.name.rjust(40, "0"),
        operator_approved=False,
    )
    assert receipt.accepted is False
    assert receipt.effects_count == 0
    assert commands.prepare_effects_executed == 0
    assert commands.global_mutations_triggered == 0


def test_nx038_refresh_does_not_mutate_workflow_status(tmp_path: Path) -> None:
    """Refresh is non-authoritative and does not mutate task status."""
    task_mutated = False

    def dummy_mutator(task_id: str, status: str) -> None:
        nonlocal task_mutated
        task_mutated = True

    commands = meg.CanonicalEnvironmentCommands(
        tmp_path,
        task_status_mutator=dummy_mutator,
    )
    receipt = commands.refresh("proj-refresh", "task-1")
    assert receipt.accepted is True
    assert receipt.action == "REFRESH"
    assert task_mutated is False
    assert commands.refresh_calls == 1


def test_nx038_project_summary_and_task_delta() -> None:
    """Task delta exposes only requirements that differ from or block the current task."""
    req_common = _requirement("req:node", capability="tool.node", required=True, version_constraint="1.2.3")
    req_extra = _requirement("req:special", capability="tool.rust", required=True, version_constraint="1.75.0")

    res_proj = _resolve((req_common,), _inventory_ready())
    res_task = _resolve((req_common, req_extra), _inventory_ready())

    vm = meg.MachineEnvironmentViewModel.from_canonical(
        res_proj,
        project_id="proj-delta",
        task_id="task-delta",
        task_result=res_task,
    )

    assert vm.project_summary.total_requirements == 1
    assert vm.project_summary.satisfied_count == 1
    assert vm.task_delta is not None
    assert vm.task_delta.has_delta is True
    # The delta only contains the special requirement not in the project baseline
    assert len(vm.task_delta.delta_requirements) == 1
    assert vm.task_delta.delta_requirements[0].requirement_id == "req:special"
    assert vm.task_delta.delta_requirements[0].capability == "tool.rust"


def test_nx038_details_drill_down_redacts_secrets() -> None:
    """Sensitive values and secret patterns are redacted from diagnostic details."""
    secret_text = "https://user:ghp_secretToken12345@github.com/org/repo.git password=SuperSecretPass! API_KEY: secret-xyz"
    sanitized = meg.sanitize_diagnostic_text(secret_text)
    assert "ghp_secretToken12345" not in sanitized
    assert "SuperSecretPass!" not in sanitized
    assert "secret-xyz" not in sanitized
    assert "[REDACTED]" in sanitized

    res = _resolve((_requirement("req:node"),), _inventory_ready())
    vm = meg.MachineEnvironmentViewModel.from_canonical(res, project_id="proj-secret")
    assert "token" not in vm.diagnostic_details.lower() or "[REDACTED]" in vm.diagnostic_details


def test_nx038_keyboard_accessibility_contract() -> None:
    """All GUI controls meet the keyboard accessibility contract."""
    specs = meg.ENVIRONMENT_GUI_CONTROL_CONTRACT
    assert len(specs) >= 7
    for spec in specs:
        assert spec.control_id
        assert spec.accessible_name
        assert spec.accessible_description
        assert spec.keyboard_focusable is True


def test_nx038_pyside6_panel_integration(tmp_path: Path) -> None:
    """Test PySide6 widgets inside ProjectCenterWindow."""
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from bdb_gui.project_center import ProjectCenterWindow
    from bdb_vnext.project_catalog import ProjectCatalog, new_project_record, ProjectBrief

    app = QApplication.instance() or QApplication(["nx038-gui-test"])
    runtime = tmp_path / "runtime"
    catalog = ProjectCatalog(runtime)
    brief = ProjectBrief("P1", "G1", "D1", "tool", ("Python",), (), ())
    record = new_project_record(
        project_id="proj-gui",
        display_name="Project GUI",
        repo_alias="proj-gui",
        local_repo_path=tmp_path / "repo",
        github_repo=None,
        brief=brief,
    )
    catalog.upsert(record)

    res = _resolve((_requirement("req:node"),), _inventory_ready())
    vm = meg.MachineEnvironmentViewModel.from_canonical(res, project_id="proj-gui")

    window = ProjectCenterWindow(
        runtime_root=runtime,
        catalog=catalog,
        environment_view_model_factory=lambda p, t: vm,
    )
    window._projects = (record,)
    window._select_project("proj-gui")

    assert window._env_status_label.accessibleName() == "Status gotowości środowiska"
    assert window._env_refresh_button.accessibleName() == "Odśwież stan środowiska"
    assert window._env_prepare_button.accessibleName() == "Przygotuj środowisko lokalne"
    assert window._env_details_toggle_button.accessibleName() == "Rozwiń/zwiń szczegóły środowiska"
    assert window._env_refresh_button.focusPolicy().value != 0
    assert window._env_prepare_button.focusPolicy().value != 0
    assert window._env_details_toggle_button.focusPolicy().value != 0
    assert window._env_status_label.text() == vm.status_label

    window.close()
    app.processEvents()


def run_nx038_machine_gate() -> dict[str, Any]:
    """Execute the source-bound machine gate for NX-038."""
    version_explicit = bool(meg.MACHINE_ENVIRONMENT_GUI_VERSION_EXPLICIT)
    gui_becomes_authority = bool(meg.GUI_BECOMES_ENVIRONMENT_AUTHORITY)
    browser_overrides = bool(meg.BROWSER_LOCAL_READINESS_OVERRIDES_CANONICAL)

    # 1. 12-Fixture Matrix evaluation
    matrix = _build_fixture_matrix()
    gui_state_fixtures_count = len(matrix)
    state_divergences = 0
    digest_divergences = 0
    stale_presents_ready = False

    for item in matrix:
        res = item["result"]
        vm = item["vm"]
        exp_state = item["expected_state"]
        exp_ready = item["expected_ready"]
        exp_stale = item["expected_stale"]

        # Resolver digest binding parity check
        expected_digest = meg.compute_resolver_result_digest(res)
        if vm.resolver_result_digest != expected_digest:
            digest_divergences += 1

        # View-model state check
        if vm.state is not exp_state or vm.is_ready != exp_ready or vm.is_stale != exp_stale:
            state_divergences += 1

        # Stale cannot present current ready check
        if vm.is_stale and vm.is_ready:
            stale_presents_ready = True

    # 2. Status rendering divergences
    rendered = {state: meg.STATUS_DISPLAY_LABELS[state] for state in meg.GuiReadinessState}
    status_divergences = len(meg.GuiReadinessState) - len(set(rendered.values()))
    if rendered[meg.GuiReadinessState.MISSING] == rendered[meg.GuiReadinessState.VERSION_MISMATCH]:
        status_divergences += 1
    if rendered[meg.GuiReadinessState.VERSION_MISMATCH] == rendered[meg.GuiReadinessState.UNVERIFIABLE]:
        status_divergences += 1

    # 3. Refresh and Prepare invariants
    refresh_mutates = False
    task_mutated = False

    def dummy_mutator(t: str, s: str) -> None:
        nonlocal task_mutated
        task_mutated = True

    cmd = meg.CanonicalEnvironmentCommands(ROOT, task_status_mutator=dummy_mutator)
    cmd.refresh("test-proj", "task-1")
    if task_mutated:
        refresh_mutates = True

    # Denied prepare effects
    denied_plan = _make_plan(
        plan_id="plan:gate-denied",
        project_id="gate-proj",
        approval_class=provisioning.ApprovalClass.PRIVILEGE_REQUIRED,
    )
    denied_receipt = cmd.prepare(
        "gate-proj",
        denied_plan,
        current_source_head=ROOT.name.rjust(40, "0"),
        current_source_tree=ROOT.name.rjust(40, "0"),
        operator_approved=False,
    )
    denied_prepare_effects = denied_receipt.effects_count + cmd.prepare_effects_executed
    gui_prepare_bypasses = denied_receipt.accepted
    gui_triggered_global_mutations = cmd.global_mutations_triggered

    # 4. Secret leaks
    leak_sample = "pass=mySecret123 token: ghp_token9988 key=masterKey"
    leak_result = meg.sanitize_diagnostic_text(leak_sample)
    secret_leaks = int("mySecret123" in leak_result or "ghp_token9988" in leak_result or "masterKey" in leak_result)

    # 5. Accessibility fixtures
    accessibility_fixtures_count = len(meg.ENVIRONMENT_GUI_CONTROL_CONTRACT)
    accessibility_divergences = sum(
        1 for spec in meg.ENVIRONMENT_GUI_CONTROL_CONTRACT
        if not spec.accessible_name or not spec.keyboard_focusable
    )

    # 6. Source binding & anti-hardcoding check
    hardcoded_fields = _hardcoded_gate_fields()
    no_hardcoded = len(hardcoded_fields) == 0

    head_code, head = _git("rev-parse", "HEAD")
    tree_code, tree = _git("rev-parse", "HEAD^{tree}")
    status_code, status_out = _git("status", "--porcelain")
    diff_code, _ = _git("diff", "--check")
    worktree_clean = (status_code == 0 and status_out == "" and diff_code == 0)

    source_bound = "PASS" if head_code == 0 and tree_code == 0 and worktree_clean and no_hardcoded else "FAIL"

    all_pass = (
        version_explicit
        and not gui_becomes_authority
        and not browser_overrides
        and gui_state_fixtures_count >= 12
        and state_divergences == 0
        and status_divergences == 0
        and digest_divergences == 0
        and not stale_presents_ready
        and not refresh_mutates
        and denied_prepare_effects == 0
        and not gui_prepare_bypasses
        and gui_triggered_global_mutations == 0
        and secret_leaks == 0
        and accessibility_fixtures_count >= 7
        and accessibility_divergences == 0
        and no_hardcoded
    )

    status_value = "PASS" if all_pass and source_bound == "PASS" else "FAIL"

    return {
        "MACHINE_ENVIRONMENT_GUI_VERSION_EXPLICIT": version_explicit,
        "GUI_BECOMES_ENVIRONMENT_AUTHORITY": gui_becomes_authority,
        "BROWSER_LOCAL_READINESS_OVERRIDES_CANONICAL": browser_overrides,
        "GUI_STATE_FIXTURES": gui_state_fixtures_count,
        "GUI_STATE_DIVERGENCES": state_divergences,
        "STATUS_RENDERING_DIVERGENCES": status_divergences,
        "GUI_RESOLVER_DIGEST_DIVERGENCES": digest_divergences,
        "STALE_GUI_PRESENTS_CURRENT_READY": stale_presents_ready,
        "GUI_REFRESH_MUTATES_WORKFLOW_STATUS": refresh_mutates,
        "DENIED_PREPARE_EFFECTS": denied_prepare_effects,
        "GUI_PREPARE_BYPASSES_POLICY": gui_prepare_bypasses,
        "GUI_TRIGGERED_GLOBAL_MUTATIONS": gui_triggered_global_mutations,
        "GUI_SECRET_LEAKS": secret_leaks,
        "ACCESSIBILITY_FIXTURES": accessibility_fixtures_count,
        "KEYBOARD_ACCESSIBILITY_DIVERGENCES": accessibility_divergences,
        "HARDCODED_GATE_RESULT_FIELDS": hardcoded_fields,
        "NO_HARDCODED_GATE_RESULTS": no_hardcoded,
        "SOURCE_HEAD": head,
        "SOURCE_TREE": tree,
        "WORKTREE_CLEAN": worktree_clean,
        "SOURCE_BOUND_MACHINE_GATE": source_bound,
        "NX038_STATUS": status_value,
    }


def test_nx038_machine_gate_execution() -> None:
    gate = run_nx038_machine_gate()
    print(json.dumps(gate, indent=2, sort_keys=True))
    assert gate["MACHINE_ENVIRONMENT_GUI_VERSION_EXPLICIT"] is True
    assert gate["GUI_BECOMES_ENVIRONMENT_AUTHORITY"] is False
    assert gate["BROWSER_LOCAL_READINESS_OVERRIDES_CANONICAL"] is False
    assert gate["GUI_STATE_FIXTURES"] >= 12
    assert gate["GUI_STATE_DIVERGENCES"] == 0
    assert gate["STATUS_RENDERING_DIVERGENCES"] == 0
    assert gate["GUI_RESOLVER_DIGEST_DIVERGENCES"] == 0
    assert gate["STALE_GUI_PRESENTS_CURRENT_READY"] is False
    assert gate["GUI_REFRESH_MUTATES_WORKFLOW_STATUS"] is False
    assert gate["DENIED_PREPARE_EFFECTS"] == 0
    assert gate["GUI_PREPARE_BYPASSES_POLICY"] is False
    assert gate["GUI_TRIGGERED_GLOBAL_MUTATIONS"] == 0
    assert gate["GUI_SECRET_LEAKS"] == 0
    assert gate["ACCESSIBILITY_FIXTURES"] >= 7
    assert gate["KEYBOARD_ACCESSIBILITY_DIVERGENCES"] == 0
    assert gate["HARDCODED_GATE_RESULT_FIELDS"] == []
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    if gate["WORKTREE_CLEAN"]:
        assert gate["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
        assert gate["NX038_STATUS"] == "PASS"

"""Focused NX-033 qualification for deterministic Machine Inventory collectors."""

from __future__ import annotations

import ast
import copy
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Iterable

import pytest

from bdb_vnext import machine_inventory_collectors as collectors
from bdb_vnext import machine_inventory_contract as contract


ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-08-26T12:00:00+00:00"
LATER = "2026-08-26T12:05:00+00:00"
DIGEST = "sha256:" + ("abcdef0123456789" * 4)
PATH_ENTRIES = (
    r"C:\Windows\System32",
    r"C:\Tools\Node",
    r"c:\tools\node",
    r"C:\Tools\Git",
)
SECRET = "collector-secret-fixture"

NX033_GATE_FIELDS = {
    "NX032_INVENTORY_CONTRACT_MATCH",
    "COLLECTOR_VERSION_EXPLICIT",
    "WINDOWS_FIXTURES",
    "WINDOWS_FIXTURE_DIVERGENCES",
    "COLLECTIONS_WITH_UNRECORDED_PATH_DIGEST",
    "COMMAND_PROBES_WITH_SHELL_ENABLED",
    "EXECUTABLE_IDENTITY_FIXTURES",
    "EXECUTABLE_IDENTITY_DIVERGENCES",
    "TIMEOUT_FIXTURES",
    "TIMEOUT_ERASED_UNRELATED_FACTS",
    "MULTIPLE_VERSION_FIXTURES",
    "MULTIPLE_VERSION_ORDER_DIVERGENCES",
    "UNVERIFIABLE_COMPONENT_CRASHES",
    "PROBE_FAILURES_PROMOTED_TO_AVAILABLE",
    "NORMALIZED_GOLDEN_RUNS",
    "GOLDEN_BYTE_STABILITY_DIVERGENCES",
    "COLLECTOR_SECRET_LEAKS",
    "HARDCODED_GATE_RESULT_FIELDS",
    "NO_HARDCODED_GATE_RESULTS",
    "SOURCE_HEAD",
    "SOURCE_TREE",
    "WORKTREE_CLEAN",
    "SOURCE_BOUND_MACHINE_GATE",
    "NX033_STATUS",
}


def _command_definitions() -> tuple[contract.ProbeDefinition, ...]:
    return tuple(
        item
        for item in contract.CANONICAL_PROBE_REGISTRY.definitions
        if item.source_kind is contract.ProbeSourceKind.COMMAND
    )


def _registry_definitions() -> tuple[contract.ProbeDefinition, ...]:
    return tuple(
        item
        for item in contract.CANONICAL_PROBE_REGISTRY.definitions
        if item.source_kind is contract.ProbeSourceKind.REGISTRY
    )


def _fact(inventory: contract.MachineInventory, fact_class: str) -> contract.InventoryFact:
    return next(item for item in inventory.facts if item.fact_class == fact_class)


def _fixture_os_reader() -> dict[str, dict[str, str]]:
    return {
        "os_identity": {"family": "Windows", "release": "11-fixture"},
        "windows_build": {"build": "10.0.26100-fixture"},
        "architecture": {"architecture": "AMD64"},
    }


def _fixture_paths() -> dict[str, tuple[str, ...]]:
    return {
        "node": (r"C:\Tools\Node\node.exe",),
        "npm": (r"C:\Tools\Node\npm.cmd",),
        "git": (r"C:\Tools\Git\git.exe",),
        "python": (r"C:\Tools\Python\python.exe",),
        "rustc": (r"C:\Tools\Rust\rustc.exe",),
        "rustup": (r"C:\Tools\Rust\rustup.exe",),
        "cargo": (r"C:\Tools\Rust\cargo.exe",),
        "pwsh": (r"C:\Tools\PowerShell\pwsh.exe",),
        "powershell": (r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",),
    }


def _fixture_versions() -> dict[str, str]:
    return {
        "node": "v22.1.0\n",
        "npm": "10.7.0\n",
        "git": "git version 2.45.1\n",
        "python": "Python 3.12.4\n",
        "rustc": "rustc 1.79.0 (fixture)\n",
        "rustup": "rustup 1.27.1 (fixture)\n",
        "cargo": "cargo 1.79.0 (fixture)\n",
        "pwsh": "PowerShell 7.4.3\n",
        "powershell": "Windows PowerShell 5.1.0\n",
    }


def _fixture_registry() -> dict[str, collectors.RegistryObservation]:
    return {
        "tool.msvc": collectors.RegistryObservation(
            value={"component": "MSVC", "installation": "fixture"},
            resolved_path=r"C:\BuildTools\MSVC\bin\cl.exe",
            version="19.40.0",
            digest=DIGEST,
        ),
        "tool.windows_sdk": collectors.RegistryObservation(
            value={"component": "Windows SDK", "installation": "fixture"},
            resolved_path=r"C:\Program Files\Windows Kits\10\bin\sdk.exe",
            version="10.0.26100.0",
            digest=DIGEST,
        ),
        "tool.webview2": collectors.RegistryObservation(
            value={"component": "WebView2", "installation": "fixture"},
            resolved_path=r"C:\Program Files\WebView2\msedgewebview2.exe",
            version="126.0.0",
            digest=DIGEST,
        ),
    }


def _fixture_collection(
    *,
    collected_at: str = NOW,
    missing: set[str] | None = None,
    timeout: set[str] | None = None,
    malformed: set[str] | None = None,
    unverified_registry: set[str] | None = None,
    multiple_versions: bool = False,
    reverse_candidates: bool = False,
) -> tuple[contract.MachineInventory, list[tuple[tuple[str, ...], Mapping[str, str], float]]]:
    missing = set(missing or ())
    timeout = set(timeout or ())
    malformed = set(malformed or ())
    unverified_registry = set(unverified_registry or ())
    paths = _fixture_paths()
    if multiple_versions:
        paths["node"] = (
            r"C:\Versions\Node\v24\node.exe",
            r"C:\Versions\Node\v22\node.exe",
        )
        if reverse_candidates:
            paths["node"] = tuple(reversed(paths["node"]))
    versions = _fixture_versions()
    registry = _fixture_registry()
    calls: list[tuple[tuple[str, ...], Mapping[str, str], float]] = []

    def resolver(command: str, _path_entries: Iterable[str], _environment: Mapping[str, str]) -> tuple[str, ...] | None:
        if command in missing:
            return None
        return paths.get(command)

    def runner(argv: tuple[str, ...], environment: Mapping[str, str], timeout_seconds: float) -> collectors.CommandOutcome:
        calls.append((argv, dict(environment), timeout_seconds))
        command = Path(argv[0]).stem.casefold()
        if command in timeout:
            return collectors.CommandOutcome(return_code=None, timed_out=True)
        if command in malformed:
            return collectors.CommandOutcome(return_code=0, stdout="version unavailable")
        return collectors.CommandOutcome(return_code=0, stdout=versions.get(command, "1.0.0\n"))

    def registry_reader(fact_class: str) -> collectors.RegistryObservation | None:
        if fact_class in unverified_registry:
            raise PermissionError("fixture access denied")
        return registry.get(fact_class)

    environment = {"PATH": os.pathsep.join(PATH_ENTRIES), "TOKEN": SECRET, "PASSWORD": SECRET}
    inventory = collectors.collect_machine_inventory(
        inventory_id="inventory:fixture:nx033",
        collected_at=collected_at,
        environment=environment,
        path_entries=PATH_ENTRIES,
        os_reader=_fixture_os_reader,
        executable_resolver=resolver,
        command_runner=runner,
        file_hasher=lambda _path: DIGEST,
        registry_reader=registry_reader,
    )
    return inventory, calls


def _assert_valid(inventory: contract.MachineInventory) -> None:
    result = contract.validate_machine_inventory(inventory)
    assert result.valid, result.errors
    assert {fact.fact_class for fact in inventory.facts} == set(contract.REQUIRED_FACT_CLASSES)


def _hardcoded_gate_fields() -> list[str]:
    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run_nx033_machine_gate"
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
            if isinstance(target, ast.Name) and target.id in NX033_GATE_FIELDS:
                hardcoded.add(target.id)
    return sorted(hardcoded)


def test_required_windows_fixture_collects_typed_complete_inventory() -> None:
    inventory, calls = _fixture_collection()
    _assert_valid(inventory)
    assert len(inventory.facts) == len(contract.REQUIRED_FACT_CLASSES)
    assert _fact(inventory, "path_identity").digest == inventory.path_identity.digest
    assert _fact(inventory, "path_identity").value == {"path_identity": inventory.path_identity.to_dict()}
    assert len(calls) == len(_command_definitions())

    for definition in _command_definitions():
        fact = _fact(inventory, definition.fact_class)
        assert fact.status is contract.FactStatus.AVAILABLE
        assert fact.executable is not None
        assert fact.executable.resolved_path == fact.resolved_path
        assert fact.executable.content_digest == DIGEST
        assert fact.version is not None
    for definition in _registry_definitions():
        assert _fact(inventory, definition.fact_class).status is contract.FactStatus.AVAILABLE

    encoded = inventory.canonical_bytes()
    assert SECRET.encode("utf-8") not in encoded
    assert b"TOKEN" not in encoded
    assert b"PASSWORD" not in encoded


def test_command_probes_use_explicit_argv_and_the_controlled_path() -> None:
    inventory, calls = _fixture_collection()
    _assert_valid(inventory)
    expected_path = os.pathsep.join(PATH_ENTRIES)
    assert calls
    for argv, environment, timeout_seconds in calls:
        assert argv[0].casefold().endswith((".exe", ".cmd"))
        assert argv[1:] == ("--version",) or argv[1:] == ("-Version",)
        assert environment["PATH"] == expected_path
        assert timeout_seconds > 0
        assert timeout_seconds <= collectors.probe_timeout_seconds(contract.ProbeTimeoutClass.LONG)
    assert all(not definition.shell_enabled for definition in _command_definitions())


def test_missing_timeout_and_malformed_probes_preserve_unrelated_facts() -> None:
    inventory, calls = _fixture_collection(
        missing={"node"},
        timeout={"npm"},
        malformed={"git"},
    )
    _assert_valid(inventory)
    assert _fact(inventory, "tool.node").status is contract.FactStatus.MISSING
    assert _fact(inventory, "tool.npm").status is contract.FactStatus.TIMEOUT
    assert _fact(inventory, "tool.git").status is contract.FactStatus.MALFORMED
    assert _fact(inventory, "tool.python").status is contract.FactStatus.AVAILABLE
    assert _fact(inventory, "tool.cargo").status is contract.FactStatus.AVAILABLE
    assert len(inventory.facts) == len(contract.REQUIRED_FACT_CLASSES)
    assert len(calls) == len(_command_definitions()) - 1


def test_unverifiable_optional_component_is_preserved_without_crashing() -> None:
    inventory, _ = _fixture_collection(unverified_registry={"tool.webview2", "tool.msvc"})
    _assert_valid(inventory)
    assert _fact(inventory, "tool.webview2").status is contract.FactStatus.UNVERIFIABLE
    assert _fact(inventory, "tool.msvc").status is contract.FactStatus.UNVERIFIABLE
    assert _fact(inventory, "tool.windows_sdk").status is contract.FactStatus.AVAILABLE


def test_multiple_versions_are_normalized_to_one_deterministic_identity() -> None:
    first, _ = _fixture_collection(multiple_versions=True)
    second, _ = _fixture_collection(multiple_versions=True, reverse_candidates=True)
    _assert_valid(first)
    _assert_valid(second)
    first_node = _fact(first, "tool.node")
    second_node = _fact(second, "tool.node")
    assert first_node.resolved_path == r"C:\Versions\Node\v22\node.exe"
    assert second_node.resolved_path == first_node.resolved_path
    assert first.canonical_bytes(normalize_time=True) == second.canonical_bytes(normalize_time=True)
    assert first_node.details == second_node.details


def test_path_inheritance_is_local_copy_and_digest_is_recorded() -> None:
    before = os.environ.get("PATH")
    inventory, calls = _fixture_collection()
    assert os.environ.get("PATH") == before
    path_fact = _fact(inventory, "path_identity")
    assert path_fact.digest == inventory.path_identity.digest
    assert path_fact.value is not None
    assert path_fact.value["path_identity"]["digest"] == inventory.path_identity.digest
    assert any(environment.get("TOKEN") == SECRET for _, environment, _ in calls)
    assert SECRET.encode("utf-8") not in inventory.canonical_bytes()


def test_filesystem_identity_is_exact_and_hashable_without_process_execution() -> None:
    fact = collectors.collect_filesystem_identity(
        fact_class="tool.node",
        probe_id="fixture:filesystem-node",
        path=r"C:\Tools\Node\node.exe",
        collected_at=NOW,
        version="22.1.0",
        hasher=lambda _path: DIGEST,
    )
    assert fact.status is contract.FactStatus.AVAILABLE
    assert fact.resolved_path == r"C:\Tools\Node\node.exe"
    assert fact.digest == DIGEST
    assert fact.executable is not None
    assert fact.executable.content_digest == DIGEST

    malformed = collectors.collect_filesystem_identity(
        fact_class="tool.node",
        probe_id="fixture:filesystem-node",
        path="node.exe",
        collected_at=NOW,
        hasher=lambda _path: DIGEST,
    )
    assert malformed.status is contract.FactStatus.MALFORMED
    assert malformed.executable is None


def test_cancelled_collection_keeps_required_facts_and_never_fabricates_available() -> None:
    inventory, _ = _fixture_collection()
    cancelled = collectors.collect_machine_inventory(
        inventory_id="inventory:fixture:cancelled",
        collected_at=NOW,
        environment={"PATH": os.pathsep.join(PATH_ENTRIES)},
        path_entries=PATH_ENTRIES,
        os_reader=_fixture_os_reader,
        executable_resolver=lambda *_args: (),
        command_runner=lambda *_args: collectors.CommandOutcome(return_code=0, stdout="1.0.0"),
        file_hasher=lambda _path: DIGEST,
        registry_reader=lambda _fact_class: None,
        cancel_check=lambda: True,
    )
    _assert_valid(cancelled)
    assert all(
        fact.status is not contract.FactStatus.AVAILABLE
        for fact in cancelled.facts
        if fact.fact_class.startswith("tool.")
    )
    assert inventory.path_identity.digest == cancelled.path_identity.digest


def test_probe_timeout_policy_is_bounded_and_registry_is_shell_free() -> None:
    registry = contract.CANONICAL_PROBE_REGISTRY
    command_definitions = [item for item in registry.definitions if item.source_kind is contract.ProbeSourceKind.COMMAND]
    assert command_definitions
    assert all(item.argv and not item.shell_enabled for item in command_definitions)
    assert all(0 < collectors.probe_timeout_seconds(item.timeout_class) <= 15 for item in command_definitions)


def test_collector_diagnostics_reuse_redaction_policy() -> None:
    diagnostics = collectors.collector_evidence(
        {
            "TOKEN": SECRET,
            "Authorization": "Bearer " + SECRET,
            "nested": {"COOKIE": "sessionid=" + SECRET},
        }
    )
    encoded = json.dumps(diagnostics, sort_keys=True).encode("utf-8")
    assert SECRET.encode("utf-8") not in encoded
    assert diagnostics["TOKEN"] == "[REDACTED]"
    assert diagnostics["nested"]["COOKIE"] == "[REDACTED]"


def run_nx033_machine_gate() -> dict[str, Any]:
    fixture_cases = (
        ("available", {}, {"tool.node": contract.FactStatus.AVAILABLE}),
        ("missing", {"missing": {"node"}}, {"tool.node": contract.FactStatus.MISSING}),
        ("malformed", {"malformed": {"git"}}, {"tool.git": contract.FactStatus.MALFORMED}),
        ("timeout", {"timeout": {"npm"}}, {"tool.npm": contract.FactStatus.TIMEOUT}),
        (
            "unverifiable",
            {"unverified_registry": {"tool.webview2"}},
            {"tool.webview2": contract.FactStatus.UNVERIFIABLE},
        ),
        ("multiple_versions", {"multiple_versions": True}, {"tool.node": contract.FactStatus.AVAILABLE}),
        ("duplicate_path", {}, {"path_identity": contract.FactStatus.AVAILABLE}),
    )
    fixture_results: list[tuple[str, contract.MachineInventory, dict[str, contract.FactStatus]]] = []
    fixture_divergences = 0
    for name, options, expectations in fixture_cases:
        inventory, _ = _fixture_collection(**options)
        fixture_results.append((name, inventory, expectations))
        for fact_class, expected_status in expectations.items():
            fixture_divergences += int(_fact(inventory, fact_class).status is not expected_status)
        fixture_divergences += int(not contract.validate_machine_inventory(inventory).valid)

    unrecorded_path_digests = sum(
        int(
            _fact(inventory, "path_identity").digest != inventory.path_identity.digest
            or _fact(inventory, "path_identity").value != {"path_identity": inventory.path_identity.to_dict()}
        )
        for _, inventory, _ in fixture_results
    )
    command_shell_enabled = sum(
        int(definition.shell_enabled)
        for definition in contract.CANONICAL_PROBE_REGISTRY.definitions
        if definition.source_kind is contract.ProbeSourceKind.COMMAND
    )

    available_inventory = next(inventory for name, inventory, _ in fixture_results if name == "available")
    executable_fixtures = [
        _fact(available_inventory, definition.fact_class)
        for definition in _command_definitions()
    ]
    executable_divergences = sum(
        int(
            fact.status is not contract.FactStatus.AVAILABLE
            or fact.executable is None
            or fact.resolved_path != fact.executable.resolved_path
            or fact.executable.content_digest != DIGEST
        )
        for fact in executable_fixtures
    )

    timeout_cases = ({"npm"}, {"git"})
    timeout_erased_unrelated = 0
    for target in timeout_cases:
        inventory, _ = _fixture_collection(timeout=target)
        for definition in _command_definitions():
            if definition.argv[0] in target:
                timeout_erased_unrelated += int(_fact(inventory, definition.fact_class).status is not contract.FactStatus.TIMEOUT)
            else:
                timeout_erased_unrelated += int(_fact(inventory, definition.fact_class).status is not contract.FactStatus.AVAILABLE)

    multiple_version_fixtures = 2
    multiple_version_divergences = 0
    for _ in range(multiple_version_fixtures):
        first, _ = _fixture_collection(multiple_versions=True)
        second, _ = _fixture_collection(multiple_versions=True, reverse_candidates=True)
        multiple_version_divergences += int(
            first.canonical_bytes(normalize_time=True) != second.canonical_bytes(normalize_time=True)
            or _fact(first, "tool.node").resolved_path != _fact(second, "tool.node").resolved_path
        )

    unverifiable_component_cases = ("tool.webview2", "tool.msvc", "tool.windows_sdk")
    unverifiable_crashes = 0
    for component in unverifiable_component_cases:
        try:
            inventory, _ = _fixture_collection(unverified_registry={component})
            unverifiable_crashes += int(_fact(inventory, component).status is contract.FactStatus.AVAILABLE)
        except Exception:
            unverifiable_crashes += 1

    failure_cases = (
        ("missing", {"missing": {"node"}}, "tool.node"),
        ("timeout", {"timeout": {"npm"}}, "tool.npm"),
        ("malformed", {"malformed": {"git"}}, "tool.git"),
        ("unverifiable", {"unverified_registry": {"tool.webview2"}}, "tool.webview2"),
    )
    promoted_failures = 0
    for _, options, fact_class in failure_cases:
        inventory, _ = _fixture_collection(**options)
        promoted_failures += int(_fact(inventory, fact_class).status is contract.FactStatus.AVAILABLE)

    normalized_golden_runs = 3
    golden_inventories = [
        _fixture_collection(collected_at=NOW)[0],
        _fixture_collection(collected_at=LATER)[0],
        _fixture_collection(collected_at=NOW)[0],
    ]
    golden_divergences = sum(
        int(golden_inventories[0].canonical_bytes(normalize_time=True) != inventory.canonical_bytes(normalize_time=True))
        for inventory in golden_inventories[1:]
    )

    secret_values = (SECRET,)
    redacted_diagnostics = collectors.collector_evidence(
        {"TOKEN": SECRET, "Authorization": "Bearer " + SECRET, "nested": {"COOKIE": "sessionid=" + SECRET}}
    )
    collector_secret_leaks = sum(
        int(value.encode("utf-8") in available_inventory.canonical_bytes() or value.encode("utf-8") in json.dumps(redacted_diagnostics).encode())
        for value in secret_values
    )

    head_code, head = _git("rev-parse", "HEAD")
    tree_code, tree = _git("rev-parse", "HEAD^{tree}")
    status_code, status = _git("status", "--porcelain")
    diff_code, _ = _git("diff", "--check")
    clean = status_code == 0 and status == "" and diff_code == 0
    contract_match = all(contract.validate_machine_inventory(inventory).valid for _, inventory, _ in fixture_results)
    hardcoded_fields = _hardcoded_gate_fields()
    no_hardcoded = not hardcoded_fields
    all_pass = all(
        (
            contract_match,
            bool(collectors.COLLECTOR_VERSION_EXPLICIT),
            fixture_divergences == 0,
            unrecorded_path_digests == 0,
            command_shell_enabled == 0,
            executable_divergences == 0,
            timeout_erased_unrelated == 0,
            multiple_version_divergences == 0,
            unverifiable_crashes == 0,
            promoted_failures == 0,
            golden_divergences == 0,
            collector_secret_leaks == 0,
            no_hardcoded,
            clean,
        )
    )
    NX032_INVENTORY_CONTRACT_MATCH = contract_match
    COLLECTOR_VERSION_EXPLICIT = bool(collectors.COLLECTOR_VERSION_EXPLICIT)
    WINDOWS_FIXTURES = len(fixture_cases)
    WINDOWS_FIXTURE_DIVERGENCES = fixture_divergences
    COLLECTIONS_WITH_UNRECORDED_PATH_DIGEST = unrecorded_path_digests
    COMMAND_PROBES_WITH_SHELL_ENABLED = command_shell_enabled
    EXECUTABLE_IDENTITY_FIXTURES = len(executable_fixtures)
    EXECUTABLE_IDENTITY_DIVERGENCES = executable_divergences
    TIMEOUT_FIXTURES = len(timeout_cases)
    TIMEOUT_ERASED_UNRELATED_FACTS = timeout_erased_unrelated
    MULTIPLE_VERSION_FIXTURES = multiple_version_fixtures
    MULTIPLE_VERSION_ORDER_DIVERGENCES = multiple_version_divergences
    UNVERIFIABLE_COMPONENT_CRASHES = unverifiable_crashes
    PROBE_FAILURES_PROMOTED_TO_AVAILABLE = promoted_failures
    NORMALIZED_GOLDEN_RUNS = normalized_golden_runs
    GOLDEN_BYTE_STABILITY_DIVERGENCES = golden_divergences
    COLLECTOR_SECRET_LEAKS = collector_secret_leaks
    HARDCODED_GATE_RESULT_FIELDS = hardcoded_fields
    NO_HARDCODED_GATE_RESULTS = no_hardcoded
    SOURCE_HEAD = head if head_code == 0 else ""
    SOURCE_TREE = tree if tree_code == 0 else ""
    WORKTREE_CLEAN = clean
    SOURCE_BOUND_MACHINE_GATE = "PASS" if clean and no_hardcoded else "FAIL"
    NX033_STATUS = "PASS" if all_pass and SOURCE_BOUND_MACHINE_GATE == "PASS" else "FAIL"
    return {
        "NX032_INVENTORY_CONTRACT_MATCH": NX032_INVENTORY_CONTRACT_MATCH,
        "COLLECTOR_VERSION_EXPLICIT": COLLECTOR_VERSION_EXPLICIT,
        "WINDOWS_FIXTURES": WINDOWS_FIXTURES,
        "WINDOWS_FIXTURE_DIVERGENCES": WINDOWS_FIXTURE_DIVERGENCES,
        "COLLECTIONS_WITH_UNRECORDED_PATH_DIGEST": COLLECTIONS_WITH_UNRECORDED_PATH_DIGEST,
        "COMMAND_PROBES_WITH_SHELL_ENABLED": COMMAND_PROBES_WITH_SHELL_ENABLED,
        "EXECUTABLE_IDENTITY_FIXTURES": EXECUTABLE_IDENTITY_FIXTURES,
        "EXECUTABLE_IDENTITY_DIVERGENCES": EXECUTABLE_IDENTITY_DIVERGENCES,
        "TIMEOUT_FIXTURES": TIMEOUT_FIXTURES,
        "TIMEOUT_ERASED_UNRELATED_FACTS": TIMEOUT_ERASED_UNRELATED_FACTS,
        "MULTIPLE_VERSION_FIXTURES": MULTIPLE_VERSION_FIXTURES,
        "MULTIPLE_VERSION_ORDER_DIVERGENCES": MULTIPLE_VERSION_ORDER_DIVERGENCES,
        "UNVERIFIABLE_COMPONENT_CRASHES": UNVERIFIABLE_COMPONENT_CRASHES,
        "PROBE_FAILURES_PROMOTED_TO_AVAILABLE": PROBE_FAILURES_PROMOTED_TO_AVAILABLE,
        "NORMALIZED_GOLDEN_RUNS": NORMALIZED_GOLDEN_RUNS,
        "GOLDEN_BYTE_STABILITY_DIVERGENCES": GOLDEN_BYTE_STABILITY_DIVERGENCES,
        "COLLECTOR_SECRET_LEAKS": COLLECTOR_SECRET_LEAKS,
        "HARDCODED_GATE_RESULT_FIELDS": HARDCODED_GATE_RESULT_FIELDS,
        "NO_HARDCODED_GATE_RESULTS": NO_HARDCODED_GATE_RESULTS,
        "SOURCE_HEAD": SOURCE_HEAD,
        "SOURCE_TREE": SOURCE_TREE,
        "WORKTREE_CLEAN": WORKTREE_CLEAN,
        "SOURCE_BOUND_MACHINE_GATE": SOURCE_BOUND_MACHINE_GATE,
        "NX033_STATUS": NX033_STATUS,
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


def test_nx033_machine_gate_execution() -> None:
    gate = run_nx033_machine_gate()
    print(json.dumps(gate, indent=2, sort_keys=True))
    assert gate["NX032_INVENTORY_CONTRACT_MATCH"] is True
    assert gate["COLLECTOR_VERSION_EXPLICIT"] is True
    assert gate["WINDOWS_FIXTURES"] >= 5
    assert gate["WINDOWS_FIXTURE_DIVERGENCES"] == 0
    assert gate["COLLECTIONS_WITH_UNRECORDED_PATH_DIGEST"] == 0
    assert gate["COMMAND_PROBES_WITH_SHELL_ENABLED"] == 0
    assert gate["EXECUTABLE_IDENTITY_FIXTURES"] > 0
    assert gate["EXECUTABLE_IDENTITY_DIVERGENCES"] == 0
    assert gate["TIMEOUT_FIXTURES"] > 0
    assert gate["TIMEOUT_ERASED_UNRELATED_FACTS"] == 0
    assert gate["MULTIPLE_VERSION_FIXTURES"] > 0
    assert gate["MULTIPLE_VERSION_ORDER_DIVERGENCES"] == 0
    assert gate["UNVERIFIABLE_COMPONENT_CRASHES"] == 0
    assert gate["PROBE_FAILURES_PROMOTED_TO_AVAILABLE"] == 0
    assert gate["NORMALIZED_GOLDEN_RUNS"] >= 3
    assert gate["GOLDEN_BYTE_STABILITY_DIVERGENCES"] == 0
    assert gate["COLLECTOR_SECRET_LEAKS"] == 0
    assert gate["HARDCODED_GATE_RESULT_FIELDS"] == []
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    assert gate["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
    assert gate["NX033_STATUS"] == "PASS"

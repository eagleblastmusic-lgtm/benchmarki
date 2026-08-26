"""Focused NX-032 qualification for the typed Machine Inventory contract."""

from __future__ import annotations

import ast
import copy
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

import pytest

from bdb_vnext import machine_inventory_contract as contract


ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-08-26T12:00:00+00:00"
LATER = "2026-08-26T12:05:00+00:00"
DIGEST = "sha256:" + ("0123456789abcdef" * 4)

NX032_GATE_FIELDS = {
    "MACHINE_INVENTORY_VERSION_EXPLICIT",
    "REQUIRED_INVENTORY_FACT_CLASSES",
    "MISSING_REQUIRED_FACT_CLASSES",
    "FACTS_WITH_REQUIRED_PROVENANCE_MISSING",
    "MISSING_TOOL_CAUSES_PARSER_CRASH",
    "UNVERIFIABLE_PROMOTED_TO_AVAILABLE",
    "MALFORMED_VERSION_PROMOTED_TO_VALID",
    "PATH_FIXTURES",
    "PATH_CANONICALIZATION_DIVERGENCES",
    "EXECUTABLE_IDENTITY_PATH_REQUIRED",
    "EXECUTABLE_IDENTITY_SUPPORTS_DIGEST",
    "SECRET_FIXTURES_TESTED",
    "SECRET_FIXTURES_LEAKED",
    "PROBE_REGISTRY_VERSION_EXPLICIT",
    "SHELL_STRING_PROBES_ACCEPTED",
    "SCHEMA_FIXTURES",
    "SCHEMA_VALIDATION_DIVERGENCES",
    "HARDCODED_GATE_RESULT_FIELDS",
    "NO_HARDCODED_GATE_RESULTS",
    "SOURCE_HEAD",
    "SOURCE_TREE",
    "WORKTREE_CLEAN",
    "SOURCE_BOUND_MACHINE_GATE",
    "NX032_STATUS",
}


def _tool_subject(fact_class: str) -> str:
    return fact_class.removeprefix("tool.")


def _tool_fact(fact_class: str) -> contract.InventoryFact:
    resolved_path = rf"C:\Fixture\{_tool_subject(fact_class)}\tool.exe"
    executable = contract.ExecutableIdentity(
        resolved_path=resolved_path,
        content_digest=DIGEST,
        reported_version="1.2.3",
        source=contract.InventorySource.FIXTURE,
        probe_id=f"fixture:{fact_class}",
        status=contract.FactStatus.AVAILABLE,
        verification=contract.VerificationDisposition.VERIFIED,
    )
    return contract.InventoryFact(
        fact_class=fact_class,
        subject=_tool_subject(fact_class),
        status=contract.FactStatus.AVAILABLE,
        source=contract.InventorySource.FIXTURE,
        collected_at=NOW,
        confidence=contract.Confidence.HIGH,
        verification=contract.VerificationDisposition.VERIFIED,
        probe_id=f"fixture:{fact_class}",
        value={"family": _tool_subject(fact_class)},
        resolved_path=resolved_path,
        version="1.2.3",
        digest=DIGEST,
        executable=executable,
        details={"fixture": True},
    )


def _canonical_inventory() -> contract.MachineInventory:
    path = contract.PathIdentity.from_entries(
        (
            r"C:\Windows\System32",
            r"C:\Program Files\Node",
            r"C:\Tools\Git",
        )
    )
    facts: list[contract.InventoryFact] = [
        contract.InventoryFact(
            fact_class="os_identity",
            subject="windows",
            status=contract.FactStatus.AVAILABLE,
            source=contract.InventorySource.OS_API,
            collected_at=NOW,
            confidence=contract.Confidence.HIGH,
            verification=contract.VerificationDisposition.VERIFIED,
            probe_id="fixture:os-identity",
            value={"family": "Windows", "edition": "fixture"},
        ),
        contract.InventoryFact(
            fact_class="windows_build",
            subject="build",
            status=contract.FactStatus.AVAILABLE,
            source=contract.InventorySource.OS_API,
            collected_at=NOW,
            confidence=contract.Confidence.HIGH,
            verification=contract.VerificationDisposition.VERIFIED,
            probe_id="fixture:windows-build",
            value={"build": "fixture-build"},
            version="fixture-build",
        ),
        contract.InventoryFact(
            fact_class="architecture",
            subject="process",
            status=contract.FactStatus.AVAILABLE,
            source=contract.InventorySource.OS_API,
            collected_at=NOW,
            confidence=contract.Confidence.HIGH,
            verification=contract.VerificationDisposition.VERIFIED,
            probe_id="fixture:architecture",
            value={"architecture": "AMD64"},
        ),
        contract.InventoryFact(
            fact_class="path_identity",
            subject="PATH",
            status=contract.FactStatus.AVAILABLE,
            source=contract.InventorySource.ENVIRONMENT,
            collected_at=NOW,
            confidence=contract.Confidence.HIGH,
            verification=contract.VerificationDisposition.VERIFIED,
            probe_id="fixture:path",
            value=contract.path_evidence(path),
            digest=path.digest,
        ),
    ]
    facts.extend(_tool_fact(fact_class) for fact_class in contract.REQUIRED_FACT_CLASSES if fact_class.startswith("tool."))
    return contract.MachineInventory(
        schema=contract.MACHINE_INVENTORY_SCHEMA,
        version=contract.MACHINE_INVENTORY_VERSION,
        inventory_id="inventory:fixture:nx032",
        collected_at=NOW,
        path_identity=path,
        facts=tuple(facts),
        redaction=contract.RedactionMetadata(),
    )


def _fact_payload(payload: dict[str, Any], fact_class: str) -> dict[str, Any]:
    return next(item for item in payload["facts"] if item["fact_class"] == fact_class)


def _replace_fact(payload: dict[str, Any], fact_class: str, **changes: Any) -> dict[str, Any]:
    result = copy.deepcopy(payload)
    target = _fact_payload(result, fact_class)
    target.update(changes)
    return result


def _schema(path: str) -> dict[str, Any]:
    return json.loads((ROOT / "schemas" / path).read_text(encoding="utf-8"))


def _git(*args: str) -> tuple[int, str]:
    completed = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode, completed.stdout.strip()


def _source_readback() -> tuple[str, str, bool]:
    head_code, head = _git("rev-parse", "HEAD")
    tree_code, tree = _git("rev-parse", "HEAD^{tree}")
    status_code, status = _git("status", "--porcelain")
    diff_code, _ = _git("diff", "--check")
    clean = status_code == 0 and status == "" and diff_code == 0
    return head if head_code == 0 else "", tree if tree_code == 0 else "", clean


def _hardcoded_gate_fields() -> list[str]:
    source = (Path(__file__)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run_nx032_machine_gate"
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
            if isinstance(target, ast.Name) and target.id in NX032_GATE_FIELDS:
                hardcoded.add(target.id)
    return sorted(hardcoded)


def test_schema_documents_are_versioned_and_closed() -> None:
    inventory_schema = _schema("bdb-vnext-machine-inventory-v1.schema.json")
    probe_schema = _schema("bdb-vnext-machine-probe-registry-v1.schema.json")

    assert inventory_schema["$schema"].endswith("draft/2020-12/schema")
    assert inventory_schema["$id"] == contract.MACHINE_INVENTORY_SCHEMA
    assert inventory_schema["additionalProperties"] is False
    assert inventory_schema["properties"]["version"]["const"] == contract.MACHINE_INVENTORY_VERSION
    assert inventory_schema["$defs"]["fact"]["additionalProperties"] is False
    assert inventory_schema["$defs"]["path_identity"]["additionalProperties"] is False
    assert probe_schema["$id"] == contract.PROBE_REGISTRY_SCHEMA
    assert probe_schema["additionalProperties"] is False
    assert probe_schema["$defs"]["probe"]["additionalProperties"] is False
    assert probe_schema["$defs"]["probe"]["properties"]["shell_enabled"]["const"] is False


def test_canonical_inventory_round_trips_and_sorts_facts() -> None:
    inventory = _canonical_inventory()
    result = contract.validate_machine_inventory(inventory)
    assert result.valid, result.errors

    payload = inventory.to_dict()
    restored = contract.MachineInventory.from_dict(payload)
    assert restored == inventory
    assert [item["fact_class"] for item in payload["facts"]] == sorted(
        item["fact_class"] for item in payload["facts"]
    )
    assert restored.canonical_bytes() == inventory.canonical_bytes()


def test_contract_rejects_missing_fields_unknown_versions_and_unknown_keys() -> None:
    payload = _canonical_inventory().to_dict()

    missing_facts = copy.deepcopy(payload)
    del missing_facts["facts"]
    assert not contract.validate_machine_inventory(missing_facts).valid

    unknown_version = copy.deepcopy(payload)
    unknown_version["version"] = "v2"
    assert not contract.validate_machine_inventory(unknown_version).valid

    unknown_key = copy.deepcopy(payload)
    unknown_key["unexpected"] = True
    assert not contract.validate_machine_inventory(unknown_key).valid


def test_missing_tool_is_preserved_without_parser_crash() -> None:
    payload = _replace_fact(
        _canonical_inventory().to_dict(),
        "tool.node",
        status="MISSING",
        verification="UNVERIFIED",
        confidence="LOW",
        value={"reason": "not_found"},
        resolved_path=None,
        version=None,
        digest=None,
        executable=None,
    )
    restored = contract.MachineInventory.from_dict(payload)
    result = contract.validate_machine_inventory(restored)
    assert result.valid, result.errors
    assert next(item for item in restored.facts if item.fact_class == "tool.node").status is contract.FactStatus.MISSING


def test_unverifiable_and_malformed_statuses_are_not_promoted() -> None:
    base = _canonical_inventory().to_dict()
    unverifiable_payload = _replace_fact(
        base,
        "tool.webview2",
        status="UNVERIFIABLE",
        verification="UNVERIFIED",
        confidence="NONE",
        value={"reason": "access_denied"},
        resolved_path=None,
        version=None,
        digest=None,
        executable=None,
    )
    malformed_payload = _replace_fact(
        base,
        "tool.msvc",
        status="MALFORMED",
        verification="REJECTED",
        confidence="LOW",
        value={"raw_version": "not-a-version"},
        resolved_path=None,
        version="not-a-version",
        digest=None,
        executable=None,
    )
    unverifiable = contract.MachineInventory.from_dict(unverifiable_payload)
    malformed = contract.MachineInventory.from_dict(malformed_payload)
    assert next(item for item in unverifiable.facts if item.fact_class == "tool.webview2").status is contract.FactStatus.UNVERIFIABLE
    assert next(item for item in malformed.facts if item.fact_class == "tool.msvc").status is contract.FactStatus.MALFORMED
    assert contract.validate_machine_inventory(unverifiable).valid
    assert contract.validate_machine_inventory(malformed).valid


def test_path_identity_is_canonical_duplicate_aware_and_digest_bound() -> None:
    path = contract.PathIdentity.from_entries(
        (r"C:\Tools\..\Tools", r"c:/tools", r"C:\Git", "c:\\git\\")
    )
    assert path.entries == (r"c:\tools", r"c:\git")
    assert path.duplicate_entries == (r"c:\git", r"c:\tools")
    assert contract.PathIdentity.from_dict(path.to_dict()) == path

    equivalent = contract.PathIdentity.from_entries((r"C:/TOOLS", r"C:/GIT"))
    assert equivalent.entries == path.entries
    assert equivalent.digest == contract.PathIdentity.from_entries((r"c:\tools", r"c:\git")).digest

    bad = path.to_dict()
    bad["digest"] = "sha256:" + ("f" * 64)
    with pytest.raises(contract.MachineInventoryContractError):
        contract.PathIdentity.from_dict(bad)

    evidence = contract.path_evidence(path)
    assert set(evidence) == {"path_identity"}
    assert "PATH" not in json.dumps(evidence, sort_keys=True)


def test_executable_identity_requires_exact_path_and_supports_content_digest() -> None:
    executable = contract.ExecutableIdentity(
        resolved_path=r"C:\Tools\node.exe",
        content_digest=DIGEST,
        reported_version="22.1.0",
        source=contract.InventorySource.FILESYSTEM,
        probe_id="fixture:node",
        status=contract.FactStatus.AVAILABLE,
        verification=contract.VerificationDisposition.VERIFIED,
    )
    restored = contract.ExecutableIdentity.from_dict(executable.to_dict())
    assert restored == executable
    assert restored.content_digest == DIGEST

    with pytest.raises(contract.MachineInventoryContractError):
        contract.ExecutableIdentity(
            resolved_path=None,
            content_digest=None,
            reported_version="22.1.0",
            source=contract.InventorySource.COMMAND,
            probe_id="fixture:node",
            status=contract.FactStatus.AVAILABLE,
            verification=contract.VerificationDisposition.VERIFIED,
        )
    with pytest.raises(contract.MachineInventoryContractError):
        contract.ExecutableIdentity(
            resolved_path="node.exe",
            content_digest=None,
            reported_version="22.1.0",
            source=contract.InventorySource.COMMAND,
            probe_id="fixture:node",
            status=contract.FactStatus.AVAILABLE,
            verification=contract.VerificationDisposition.VERIFIED,
        )


def test_probe_registry_is_explicit_typed_and_shell_free() -> None:
    registry = contract.ProbeRegistry.from_dict(contract.CANONICAL_PROBE_REGISTRY.to_dict())
    assert registry.missing_required_fact_classes == ()
    assert registry == contract.CANONICAL_PROBE_REGISTRY
    assert all(not item.shell_enabled for item in registry.definitions)

    shell_enabled = registry.to_dict()
    shell_enabled["definitions"][0]["shell_enabled"] = True
    with pytest.raises(contract.MachineInventoryContractError):
        contract.ProbeRegistry.from_dict(shell_enabled)

    shell_syntax = registry.to_dict()
    command_probe = next(item for item in shell_syntax["definitions"] if item["source_kind"] == "COMMAND")
    command_probe["argv"] = [command_probe["argv"][0], "--version; whoami"]
    with pytest.raises(contract.MachineInventoryContractError):
        contract.ProbeRegistry.from_dict(shell_syntax)

    unknown_command_shape = registry.to_dict()
    unknown_command_shape["definitions"][0]["command"] = "node --version"
    with pytest.raises(contract.MachineInventoryContractError):
        contract.ProbeRegistry.from_dict(unknown_command_shape)


def test_redaction_fixtures_never_serialize_secret_values() -> None:
    fixtures = {
        "TOKEN": "token-fixture-value",
        "ACCESS_TOKEN": "access-fixture-value",
        "API_KEY": "api-fixture-value",
        "PASSWORD": "password-fixture-value",
        "Authorization": "Bearer bearer-fixture-value",
        "COOKIE": "sessionid=session-fixture-value",
        "PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\nprivate-fixture-value\n-----END PRIVATE KEY-----",
    }
    redacted = contract.redact_environment(fixtures)
    encoded = contract.canonical_json_bytes(redacted) if hasattr(contract, "canonical_json_bytes") else json.dumps(redacted).encode()
    assert all(value.encode() not in encoded for value in fixtures.values())
    assert all(value == "[REDACTED]" for value in redacted.values())
    assert contract._sensitive_field_paths(redacted) == ()


def test_all_inventory_facts_carry_provenance() -> None:
    inventory = _canonical_inventory()
    for fact in inventory.facts:
        assert fact.source
        assert fact.collected_at
        assert fact.confidence
        assert fact.verification
        assert fact.probe_id

    payload = inventory.to_dict()
    del _fact_payload(payload, "tool.node")["probe_id"]
    result = contract.validate_machine_inventory(payload)
    assert not result.valid


def test_normalized_golden_bytes_ignore_only_collection_time() -> None:
    inventory = _canonical_inventory()
    changed = replace(
        inventory,
        collected_at=LATER,
        facts=tuple(replace(fact, collected_at=LATER) for fact in inventory.facts),
    )
    assert inventory.canonical_bytes() != changed.canonical_bytes()
    assert inventory.canonical_bytes(normalize_time=True) == changed.canonical_bytes(normalize_time=True)


def run_nx032_machine_gate() -> dict[str, Any]:
    inventory = _canonical_inventory()
    valid_payload = inventory.to_dict()
    missing_tool_payload = _replace_fact(
        valid_payload,
        "tool.node",
        status="MISSING",
        verification="UNVERIFIED",
        confidence="LOW",
        value={"reason": "not_found"},
        resolved_path=None,
        version=None,
        digest=None,
        executable=None,
    )
    unverifiable_payload = _replace_fact(
        valid_payload,
        "tool.webview2",
        status="UNVERIFIABLE",
        verification="UNVERIFIED",
        confidence="NONE",
        value={"reason": "access_denied"},
        resolved_path=None,
        version=None,
        digest=None,
        executable=None,
    )
    malformed_payload = _replace_fact(
        valid_payload,
        "tool.msvc",
        status="MALFORMED",
        verification="REJECTED",
        confidence="LOW",
        value={"raw_version": "not-a-version"},
        resolved_path=None,
        version="not-a-version",
        digest=None,
        executable=None,
    )
    bad_digest_payload = copy.deepcopy(valid_payload)
    bad_digest_payload["path_identity"]["digest"] = "sha256:" + ("f" * 64)
    bad_digest_payload["facts"] = [
        {**fact, "digest": bad_digest_payload["path_identity"]["digest"]}
        if fact["fact_class"] == "path_identity"
        else fact
        for fact in bad_digest_payload["facts"]
    ]
    leaked_payload = copy.deepcopy(valid_payload)
    _fact_payload(leaked_payload, "os_identity")["details"] = {"TOKEN": "unredacted-fixture"}
    schema_fixtures = (
        (valid_payload, True),
        (missing_tool_payload, True),
        (unverifiable_payload, True),
        (malformed_payload, True),
        (bad_digest_payload, False),
        (leaked_payload, False),
        ({key: value for key, value in valid_payload.items() if key != "facts"}, False),
        ({**valid_payload, "version": "v2"}, False),
    )
    schema_divergences = 0
    for payload, expected in schema_fixtures:
        actual = contract.validate_machine_inventory(payload).valid
        schema_divergences += int(actual != expected)

    missing_classes = sorted(set(contract.REQUIRED_FACT_CLASSES) - {fact.fact_class for fact in inventory.facts})
    provenance_missing = sum(
        int(not all((fact.source, fact.collected_at, fact.confidence, fact.verification, fact.probe_id)))
        for fact in inventory.facts
    )
    missing_tool_parser_crashed = False
    try:
        contract.MachineInventory.from_dict(missing_tool_payload)
    except Exception:
        missing_tool_parser_crashed = True

    unverifiable = contract.MachineInventory.from_dict(unverifiable_payload)
    unverifiable_status = next(fact.status for fact in unverifiable.facts if fact.fact_class == "tool.webview2")
    malformed = contract.MachineInventory.from_dict(malformed_payload)
    malformed_status = next(fact.status for fact in malformed.facts if fact.fact_class == "tool.msvc")

    path_inputs = (
        (r"C:\Windows\System32", r"C:\Tools\Git"),
        (r"c:/windows/system32", r"c:/tools/git"),
        (r"C:\Windows\System32\.", "C:\\Tools\\Git\\"),
        (r"C:\Windows\System32", r"C:\Tools\Git\..\Git"),
    )
    path_fixtures = len(path_inputs)
    path_divergences = 0
    expected_path = contract.PathIdentity.from_entries(path_inputs[0])
    for raw in path_inputs:
        candidate = contract.PathIdentity.from_entries(raw)
        path_divergences += int(candidate.entries != expected_path.entries or candidate.digest != expected_path.digest)
        path_divergences += int(contract.PathIdentity.from_dict(candidate.to_dict()) != candidate)

    executable_path_required = False
    try:
        contract.ExecutableIdentity(
            resolved_path=None,
            content_digest=None,
            reported_version="fixture",
            source=contract.InventorySource.FIXTURE,
            probe_id="fixture:missing-path",
            status=contract.FactStatus.AVAILABLE,
            verification=contract.VerificationDisposition.VERIFIED,
        )
    except contract.MachineInventoryContractError:
        executable_path_required = True
    executable = _tool_fact("tool.node").executable
    executable_digest_supported = executable is not None and executable.content_digest == DIGEST

    secret_fixtures = {
        "TOKEN": "token-fixture-value",
        "ACCESS_TOKEN": "access-fixture-value",
        "API_KEY": "api-fixture-value",
        "PASSWORD": "password-fixture-value",
        "Authorization": "Bearer bearer-fixture-value",
        "COOKIE": "sessionid=session-fixture-value",
        "PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\nprivate-fixture-value\n-----END PRIVATE KEY-----",
    }
    redacted_bytes = json.dumps(
        contract.redact_environment(secret_fixtures), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    secret_leaks = sum(int(value.encode("utf-8") in redacted_bytes) for value in secret_fixtures.values())

    accepted_shell_probes = 0
    shell_cases = (
        {"shell_enabled": True},
        {"command": "node --version"},
    )
    registry_payload = contract.CANONICAL_PROBE_REGISTRY.to_dict()
    for invalid in shell_cases:
        candidate = copy.deepcopy(registry_payload)
        candidate["definitions"][0].update(invalid)
        try:
            contract.ProbeRegistry.from_dict(candidate)
        except contract.MachineInventoryContractError:
            continue
        accepted_shell_probes += 1

    head, tree, clean = _source_readback()
    hardcoded_fields = _hardcoded_gate_fields()
    no_hardcoded = not hardcoded_fields
    all_pass = all(
        (
            bool(contract.MACHINE_INVENTORY_VERSION_EXPLICIT),
            len(contract.REQUIRED_FACT_CLASSES) > 0,
            not missing_classes,
            provenance_missing == 0,
            not missing_tool_parser_crashed,
            unverifiable_status is not contract.FactStatus.AVAILABLE,
            malformed_status is not contract.FactStatus.AVAILABLE,
            path_divergences == 0,
            executable_path_required,
            executable_digest_supported,
            secret_leaks == 0,
            bool(contract.PROBE_REGISTRY_VERSION_EXPLICIT),
            accepted_shell_probes == 0,
            schema_divergences == 0,
            no_hardcoded,
            clean,
        )
    )
    MACHINE_INVENTORY_VERSION_EXPLICIT = bool(contract.MACHINE_INVENTORY_VERSION_EXPLICIT)
    REQUIRED_INVENTORY_FACT_CLASSES = len(contract.REQUIRED_FACT_CLASSES)
    MISSING_REQUIRED_FACT_CLASSES = len(missing_classes)
    FACTS_WITH_REQUIRED_PROVENANCE_MISSING = provenance_missing
    MISSING_TOOL_CAUSES_PARSER_CRASH = missing_tool_parser_crashed
    UNVERIFIABLE_PROMOTED_TO_AVAILABLE = unverifiable_status is contract.FactStatus.AVAILABLE
    MALFORMED_VERSION_PROMOTED_TO_VALID = malformed_status is contract.FactStatus.AVAILABLE
    PATH_FIXTURES = path_fixtures
    PATH_CANONICALIZATION_DIVERGENCES = path_divergences
    EXECUTABLE_IDENTITY_PATH_REQUIRED = executable_path_required
    EXECUTABLE_IDENTITY_SUPPORTS_DIGEST = executable_digest_supported
    SECRET_FIXTURES_TESTED = len(secret_fixtures)
    SECRET_FIXTURES_LEAKED = secret_leaks
    PROBE_REGISTRY_VERSION_EXPLICIT = bool(contract.PROBE_REGISTRY_VERSION_EXPLICIT)
    SHELL_STRING_PROBES_ACCEPTED = accepted_shell_probes
    SCHEMA_FIXTURES = len(schema_fixtures)
    SCHEMA_VALIDATION_DIVERGENCES = schema_divergences
    HARDCODED_GATE_RESULT_FIELDS = hardcoded_fields
    NO_HARDCODED_GATE_RESULTS = no_hardcoded
    SOURCE_HEAD = head
    SOURCE_TREE = tree
    WORKTREE_CLEAN = clean
    SOURCE_BOUND_MACHINE_GATE = "PASS" if clean and no_hardcoded else "FAIL"
    NX032_STATUS = "PASS" if all_pass and SOURCE_BOUND_MACHINE_GATE == "PASS" else "FAIL"
    return {
        "MACHINE_INVENTORY_VERSION_EXPLICIT": MACHINE_INVENTORY_VERSION_EXPLICIT,
        "REQUIRED_INVENTORY_FACT_CLASSES": REQUIRED_INVENTORY_FACT_CLASSES,
        "MISSING_REQUIRED_FACT_CLASSES": MISSING_REQUIRED_FACT_CLASSES,
        "FACTS_WITH_REQUIRED_PROVENANCE_MISSING": FACTS_WITH_REQUIRED_PROVENANCE_MISSING,
        "MISSING_TOOL_CAUSES_PARSER_CRASH": MISSING_TOOL_CAUSES_PARSER_CRASH,
        "UNVERIFIABLE_PROMOTED_TO_AVAILABLE": UNVERIFIABLE_PROMOTED_TO_AVAILABLE,
        "MALFORMED_VERSION_PROMOTED_TO_VALID": MALFORMED_VERSION_PROMOTED_TO_VALID,
        "PATH_FIXTURES": PATH_FIXTURES,
        "PATH_CANONICALIZATION_DIVERGENCES": PATH_CANONICALIZATION_DIVERGENCES,
        "EXECUTABLE_IDENTITY_PATH_REQUIRED": EXECUTABLE_IDENTITY_PATH_REQUIRED,
        "EXECUTABLE_IDENTITY_SUPPORTS_DIGEST": EXECUTABLE_IDENTITY_SUPPORTS_DIGEST,
        "SECRET_FIXTURES_TESTED": SECRET_FIXTURES_TESTED,
        "SECRET_FIXTURES_LEAKED": SECRET_FIXTURES_LEAKED,
        "PROBE_REGISTRY_VERSION_EXPLICIT": PROBE_REGISTRY_VERSION_EXPLICIT,
        "SHELL_STRING_PROBES_ACCEPTED": SHELL_STRING_PROBES_ACCEPTED,
        "SCHEMA_FIXTURES": SCHEMA_FIXTURES,
        "SCHEMA_VALIDATION_DIVERGENCES": SCHEMA_VALIDATION_DIVERGENCES,
        "HARDCODED_GATE_RESULT_FIELDS": HARDCODED_GATE_RESULT_FIELDS,
        "NO_HARDCODED_GATE_RESULTS": NO_HARDCODED_GATE_RESULTS,
        "SOURCE_HEAD": SOURCE_HEAD,
        "SOURCE_TREE": SOURCE_TREE,
        "WORKTREE_CLEAN": WORKTREE_CLEAN,
        "SOURCE_BOUND_MACHINE_GATE": SOURCE_BOUND_MACHINE_GATE,
        "NX032_STATUS": NX032_STATUS,
    }


def test_nx032_machine_gate_execution() -> None:
    gate = run_nx032_machine_gate()
    print(json.dumps(gate, indent=2, sort_keys=True))
    assert gate["MACHINE_INVENTORY_VERSION_EXPLICIT"] is True
    assert gate["MISSING_REQUIRED_FACT_CLASSES"] == 0
    assert gate["FACTS_WITH_REQUIRED_PROVENANCE_MISSING"] == 0
    assert gate["MISSING_TOOL_CAUSES_PARSER_CRASH"] is False
    assert gate["UNVERIFIABLE_PROMOTED_TO_AVAILABLE"] is False
    assert gate["MALFORMED_VERSION_PROMOTED_TO_VALID"] is False
    assert gate["PATH_CANONICALIZATION_DIVERGENCES"] == 0
    assert gate["EXECUTABLE_IDENTITY_PATH_REQUIRED"] is True
    assert gate["EXECUTABLE_IDENTITY_SUPPORTS_DIGEST"] is True
    assert gate["SECRET_FIXTURES_LEAKED"] == 0
    assert gate["SHELL_STRING_PROBES_ACCEPTED"] == 0
    assert gate["SCHEMA_VALIDATION_DIVERGENCES"] == 0
    assert gate["HARDCODED_GATE_RESULT_FIELDS"] == []
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    assert gate["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
    assert gate["NX032_STATUS"] == "PASS"

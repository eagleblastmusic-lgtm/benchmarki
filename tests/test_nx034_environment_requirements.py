"""Focused NX-034 qualification for requirements and readiness resolution."""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Iterable

import pytest

from bdb_vnext import environment_requirements as requirements
from bdb_vnext import machine_inventory_contract as contract
from tests.test_nx032_machine_inventory_contract import _canonical_inventory


ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-08-26T13:00:00+00:00"
STALE = "2020-01-01T00:00:00+00:00"
DIGEST = "sha256:" + ("0123456789abcdef" * 4)
NX034_GATE_FIELDS = {
    "ENVIRONMENT_REQUIREMENT_VERSION_EXPLICIT",
    "REQUIRED_DISPOSITIONS_DEFINED",
    "MISSING_REQUIRED_DISPOSITIONS",
    "UNVERIFIABLE_PROMOTED_TO_READY",
    "REQUIRED_MISSING_RETURNS_READY",
    "REQUIRED_VERSION_MISMATCH_RETURNS_READY",
    "REQUIRED_UNVERIFIABLE_RETURNS_READY",
    "OPTIONAL_MISSING_BLOCKS_READY",
    "ALTERNATIVE_CAPABILITY_FIXTURES",
    "ALTERNATIVE_CAPABILITY_FALSE_POSITIVES",
    "VERSION_RANGE_FIXTURES",
    "VERSION_RANGE_DIVERGENCES",
    "STALE_INVENTORY_RETURNS_READY",
    "REQUIREMENT_DIGEST_DIVERGENCES",
    "RESOLVER_FIXTURES",
    "RESOLVER_DISPOSITION_DIVERGENCES",
    "RESOLVER_REPEAT_DIVERGENCES",
    "BLOCKING_REQUIREMENT_TASK_STARTS",
    "HARDCODED_GATE_RESULT_FIELDS",
    "NO_HARDCODED_GATE_RESULTS",
    "SOURCE_HEAD",
    "SOURCE_TREE",
    "WORKTREE_CLEAN",
    "SOURCE_BOUND_MACHINE_GATE",
    "NX034_STATUS",
}


def _sha(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _source(reference: str = "fixture:requirements") -> requirements.RequirementSource:
    return requirements.RequirementSource(
        kind="FIXTURE",
        reference=reference,
        digest=_sha(reference),
    )


def _provenance(requirement_id: str) -> requirements.RequirementProvenance:
    return requirements.RequirementProvenance(
        declared_at="2026-08-26T12:00:00+00:00",
        declaration_id=f"declaration:{requirement_id}",
        authority="fixture-authority",
    )


def _requirement(
    requirement_id: str,
    *,
    capability: str = "tool.node",
    required: bool = True,
    version_constraint: str | None = "1.2.3",
    alternatives: tuple[requirements.AlternativeCapability, ...] = (),
    exact_executable_path: str | None = None,
    exact_executable_digest: str | None = None,
    source_reference: str = "fixture:requirements",
) -> requirements.EnvironmentRequirement:
    return requirements.EnvironmentRequirement(
        requirement_id=requirement_id,
        capability=capability,
        required=required,
        version_constraint=version_constraint,
        source=_source(source_reference),
        provenance=_provenance(requirement_id),
        alternatives=alternatives,
        exact_executable_path=exact_executable_path,
        exact_executable_digest=exact_executable_digest,
    )


def _requirement_set(
    *items: requirements.EnvironmentRequirement,
    max_age: int = 86_400,
    expected_path_digest: str | None = None,
) -> requirements.EnvironmentRequirementSet:
    return requirements.EnvironmentRequirementSet(
        set_id="set:nx034-fixture",
        requirements=tuple(items),
        max_inventory_age_seconds=max_age,
        expected_path_digest=expected_path_digest,
    )


def _inventory_with(*, fact_class: str, **changes: Any) -> contract.MachineInventory:
    payload = copy.deepcopy(_canonical_inventory().to_dict())
    inventory_collected_at = changes.pop("inventory_collected_at", None)
    if inventory_collected_at is not None:
        payload["collected_at"] = inventory_collected_at
    target = next(item for item in payload["facts"] if item["fact_class"] == fact_class)
    target.update(changes)
    return contract.MachineInventory.from_dict(payload)


def _node_inventory() -> contract.MachineInventory:
    return _canonical_inventory()


def _resolve(
    requirement_set: requirements.EnvironmentRequirementSet,
    inventory: contract.MachineInventory | None = None,
    *,
    evaluated_at: str = NOW,
    current_path_digest: str | None = None,
) -> requirements.ReadinessResult:
    return requirements.resolve_requirements(
        requirement_set,
        _node_inventory() if inventory is None else inventory,
        evaluated_at=evaluated_at,
        current_path_digest=current_path_digest,
    )


def _schema() -> dict[str, Any]:
    return json.loads(
        (ROOT / "schemas" / "bdb-vnext-environment-requirement-v1.schema.json").read_text(encoding="utf-8")
    )


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
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run_nx034_machine_gate"
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
            if isinstance(target, ast.Name) and target.id in NX034_GATE_FIELDS:
                hardcoded.add(target.id)
    return sorted(hardcoded)


def test_requirement_schema_is_versioned_closed_and_round_trips() -> None:
    schema = _schema()
    assert schema["$schema"].endswith("draft/2020-12/schema")
    assert schema["$id"] == requirements.REQUIREMENT_SCHEMA
    assert schema["additionalProperties"] is False
    assert schema["properties"]["version"]["const"] == requirements.REQUIREMENT_VERSION
    assert schema["$defs"]["requirement"]["additionalProperties"] is False
    assert schema["$defs"]["source"]["additionalProperties"] is False
    assert schema["$defs"]["alternative"]["additionalProperties"] is False

    requirement_set = _requirement_set(_requirement("req:node"))
    restored = requirements.EnvironmentRequirementSet.from_dict(requirement_set.to_dict())
    assert restored == requirement_set
    assert restored.requirement_digest == requirement_set.requirement_digest

    invalid = requirement_set.to_dict()
    invalid["unknown"] = True
    with pytest.raises(requirements.RequirementContractError):
        requirements.EnvironmentRequirementSet.from_dict(invalid)


def test_version_ranges_cover_exact_bounds_compatible_and_malformed_inputs() -> None:
    cases = (
        ("1.2.3", "1.2.3", True),
        ("1.2.3", ">=1.2.3", True),
        ("1.2.4", ">=1.2.3", True),
        ("1.2.3", "<=1.2.3", True),
        ("1.2.4", "<=1.2.3", False),
        ("1.5.0", ">=1.0,<2.0", True),
        ("2.0.0", ">=1.0,<2.0", False),
        ("1.5.0", "^1.2.0", True),
        ("2.0.0", "^1.2.0", False),
        ("1.2.9", "~1.2.0", True),
        ("1.3.0", "~1.2.0", False),
        ("1.9.0", "1.x", True),
        ("2.0.0", "1.x", False),
        ("1.0.0", ">=1.1.0", False),
        ("3.0.0", "<2.0.0", False),
    )
    for observed, expression, expected in cases:
        assert requirements.VersionConstraint.parse(expression).matches(observed) is expected
    with pytest.raises(requirements.RequirementContractError):
        requirements.Version.parse("not-a-version")
    with pytest.raises(requirements.RequirementContractError):
        requirements.VersionConstraint.parse(">=not-a-version")
    with pytest.raises(requirements.RequirementContractError):
        requirements.VersionConstraint.parse("1..2")
    with pytest.raises(requirements.RequirementContractError):
        requirements.VersionConstraint.parse("*.x")


def test_required_dispositions_are_distinct_and_gate_readiness() -> None:
    satisfied = _resolve(_requirement_set(_requirement("req:node")))
    missing = _resolve(
        _requirement_set(_requirement("req:missing", capability="tool.not_installed"))
    )
    mismatch = _resolve(
        _requirement_set(_requirement("req:mismatch", version_constraint=">=99.0.0"))
    )
    unverifiable = _resolve(
        _requirement_set(_requirement("req:unverified", capability="tool.webview2")),
        _inventory_with(
            fact_class="tool.webview2",
            status="UNVERIFIABLE",
            verification="UNVERIFIED",
            confidence="NONE",
            resolved_path=None,
            version=None,
            digest=None,
            executable=None,
        ),
    )
    assert satisfied.ready
    assert missing.status is requirements.ReadinessStatus.ENVIRONMENT_NOT_READY
    assert mismatch.status is requirements.ReadinessStatus.ENVIRONMENT_NOT_READY
    assert unverifiable.status is requirements.ReadinessStatus.ENVIRONMENT_NOT_READY
    assert missing.requirements[0].disposition is requirements.RequirementDisposition.MISSING
    assert mismatch.requirements[0].disposition is requirements.RequirementDisposition.VERSION_MISMATCH
    assert unverifiable.requirements[0].disposition is requirements.RequirementDisposition.UNVERIFIABLE
    assert not requirements.task_start_allowed(missing)
    assert not requirements.task_start_allowed(mismatch)
    assert not requirements.task_start_allowed(unverifiable)


def test_optional_unmet_requirements_do_not_block_ready() -> None:
    missing = _resolve(
        _requirement_set(
            _requirement("req:optional", capability="tool.not_installed", required=False)
        )
    )
    mismatch = _resolve(
        _requirement_set(
            _requirement("req:optional", required=False, version_constraint=">=99.0.0")
        )
    )
    assert missing.ready
    assert mismatch.ready
    assert missing.blocking_requirement_ids == ()
    assert mismatch.blocking_requirement_ids == ()


def test_alternative_capabilities_require_exact_approved_fact_identity() -> None:
    alternatives = (
        requirements.AlternativeCapability("tool.not_installed"),
            requirements.AlternativeCapability("tool.git", ">=1.0.0"),
    )
    result = _resolve(
        _requirement_set(
            _requirement(
                "req:node-or-git",
                capability="tool.not_installed",
                alternatives=(requirements.AlternativeCapability("tool.git", ">=1.0.0"),),
            )
        )
    )
    assert result.ready
    assert result.requirements[0].selected_capability == "tool.git"
    assert result.requirements[0].observed_fact_class == "tool.git"

    false_positive = _resolve(
        _requirement_set(
            _requirement(
                "req:exact-name",
                capability="node",
                alternatives=(requirements.AlternativeCapability("gitty"),),
            )
        )
    )
    assert false_positive.requirements[0].disposition is requirements.RequirementDisposition.MISSING
    assert not false_positive.ready

    all_missing = _resolve(
        _requirement_set(
            _requirement(
                "req:all-missing",
                capability="tool.not_installed",
                alternatives=(requirements.AlternativeCapability("tool.also_missing"),),
            )
        )
    )
    assert all_missing.requirements[0].disposition is requirements.RequirementDisposition.MISSING


def test_stale_inventory_and_current_path_identity_fail_closed() -> None:
    stale_inventory = _inventory_with(fact_class="os_identity", inventory_collected_at=STALE)
    stale = _resolve(_requirement_set(_requirement("req:node"), max_age=60), stale_inventory)
    assert stale.stale
    assert stale.status is requirements.ReadinessStatus.ENVIRONMENT_NOT_READY
    assert stale.requirements[0].disposition is requirements.RequirementDisposition.UNVERIFIABLE
    assert not requirements.task_start_allowed(stale)

    changed_path = contract.PathIdentity.from_entries((r"C:\Changed\Path",))
    path_stale = _resolve(
        _requirement_set(_requirement("req:node")),
        current_path_digest=changed_path.digest,
    )
    assert path_stale.stale
    assert not path_stale.ready
    assert path_stale.inventory_freshness == "STALE"


def test_malformed_observed_version_and_exact_executable_identity_fail_closed() -> None:
    malformed = _inventory_with(fact_class="tool.node", version="not-a-version")
    malformed_result = _resolve(_requirement_set(_requirement("req:node")), malformed)
    assert malformed_result.requirements[0].disposition is requirements.RequirementDisposition.UNVERIFIABLE
    assert not malformed_result.ready

    base_node = next(item for item in _node_inventory().facts if item.fact_class == "tool.node")
    assert base_node.resolved_path is not None
    assert base_node.executable is not None
    exact = _resolve(
        _requirement_set(
            _requirement(
                "req:exact-node",
                exact_executable_path=base_node.resolved_path,
                exact_executable_digest=base_node.executable.content_digest,
            )
        )
    )
    wrong_path = _resolve(
        _requirement_set(
            _requirement(
                "req:wrong-node",
                exact_executable_path=r"C:\Other\node.exe",
            )
        )
    )
    assert exact.ready
    assert wrong_path.requirements[0].disposition is requirements.RequirementDisposition.VERSION_MISMATCH
    assert not wrong_path.ready


def test_requirement_digest_changes_only_for_identity_critical_changes() -> None:
    first = _requirement_set(_requirement("req:node"), max_age=60)
    reordered = _requirement_set(_requirement("req:node"), max_age=60)
    changed_range = _requirement_set(_requirement("req:node", version_constraint=">=20.0.0"), max_age=60)
    changed_required = _requirement_set(_requirement("req:node", required=False), max_age=60)
    changed_source = _requirement_set(_requirement("req:node", source_reference="fixture:other"), max_age=60)
    changed_path = _requirement_set(_requirement("req:node"), max_age=60, expected_path_digest=DIGEST)
    assert first.requirement_digest == reordered.requirement_digest
    assert first.requirement_digest != changed_range.requirement_digest
    assert first.requirement_digest != changed_required.requirement_digest
    assert first.requirement_digest != changed_source.requirement_digest
    assert first.requirement_digest != changed_path.requirement_digest

    tampered = first.to_dict()
    tampered["requirement_digest"] = DIGEST
    with pytest.raises(requirements.RequirementContractError):
        requirements.EnvironmentRequirementSet.from_dict(tampered)


def test_explanation_contains_diagnostic_identity_without_inventory_secrets() -> None:
    result = _resolve(_requirement_set(_requirement("req:node", version_constraint=">=99.0.0")))
    repeated = _resolve(_requirement_set(_requirement("req:node", version_constraint=">=99.0.0")))
    explanation = result.requirements[0].explanation
    assert "requirement=req:node" in explanation
    assert "disposition=VERSION_MISMATCH" in explanation
    assert "blocking=True" in explanation
    assert "version=1.2.3" in explanation
    assert result.requirement_digest in explanation
    assert result.inventory_id in explanation
    assert "TOKEN" not in explanation
    assert "PASSWORD" not in explanation
    assert result.canonical_bytes(normalize_time=True) == repeated.canonical_bytes(normalize_time=True)


def run_nx034_machine_gate() -> dict[str, Any]:
    source = _source()
    requirements_set = _requirement_set(_requirement("req:node"))
    missing_result = _resolve(
        _requirement_set(_requirement("req:missing", capability="tool.not_installed"))
    )
    mismatch_result = _resolve(
        _requirement_set(_requirement("req:mismatch", version_constraint=">=99.0.0"))
    )
    unverifiable_result = _resolve(
        _requirement_set(_requirement("req:unverified", capability="tool.webview2")),
        _inventory_with(
            fact_class="tool.webview2",
            status="UNVERIFIABLE",
            verification="UNVERIFIED",
            confidence="NONE",
            resolved_path=None,
            version=None,
            digest=None,
            executable=None,
        ),
    )
    optional_missing_result = _resolve(
        _requirement_set(_requirement("req:optional", capability="tool.not_installed", required=False))
    )
    stale_result = _resolve(
        _requirement_set(_requirement("req:stale"), max_age=60)
    )
    alternative_cases = (
        _requirement(
            "req:alt-a",
            capability="tool.not_installed",
            alternatives=(requirements.AlternativeCapability("tool.git"),),
        ),
        _requirement(
            "req:alt-b",
            capability="tool.not_installed",
            alternatives=(
                requirements.AlternativeCapability("tool.also_missing"),
                requirements.AlternativeCapability("tool.python"),
            ),
        ),
        _requirement(
            "req:alt-none",
            capability="tool.not_installed",
            alternatives=(requirements.AlternativeCapability("tool.also_missing"),),
        ),
    )
    alternative_results = tuple(_resolve(_requirement_set(item)) for item in alternative_cases)
    false_positive_result = _resolve(
        _requirement_set(
            _requirement("req:false-positive", capability="node", alternatives=(requirements.AlternativeCapability("gitty"),))
        )
    )

    version_cases = (
        ("1.2.3", "1.2.3", True),
        ("1.2.4", ">=1.2.3", True),
        ("1.2.3", "<=1.2.3", True),
        ("1.5.0", ">=1.0,<2.0", True),
        ("2.0.0", ">=1.0,<2.0", False),
        ("1.5.0", "^1.2.0", True),
        ("2.0.0", "^1.2.0", False),
        ("1.2.9", "~1.2.0", True),
        ("1.3.0", "~1.2.0", False),
        ("not-a-version", ">=1.0", False),
    )
    version_divergences = 0
    for observed, expression, expected in version_cases:
        try:
            actual = requirements.VersionConstraint.parse(expression).matches(observed)
        except requirements.RequirementContractError:
            actual = False
        version_divergences += int(actual != expected)
    malformed_range_rejected = False
    try:
        requirements.VersionConstraint.parse(">=broken")
    except requirements.RequirementContractError:
        malformed_range_rejected = True
    version_divergences += int(not malformed_range_rejected)

    equivalent = requirements.EnvironmentRequirementSet(
        set_id=requirements_set.set_id,
        requirements=tuple(reversed(requirements_set.requirements)),
        max_inventory_age_seconds=requirements_set.max_inventory_age_seconds,
        expected_path_digest=requirements_set.expected_path_digest,
    )
    changed_requirement = _requirement_set(_requirement("req:node", version_constraint=">=20.0.0"))
    digest_divergences = int(requirements_set.requirement_digest != equivalent.requirement_digest)
    digest_divergences += int(requirements_set.requirement_digest == changed_requirement.requirement_digest)

    matrix = (
        ("satisfied", _resolve(requirements_set), requirements.RequirementDisposition.ALREADY_AVAILABLE, True, ()),
        ("missing", missing_result, requirements.RequirementDisposition.MISSING, False, ("req:missing",)),
        ("mismatch", mismatch_result, requirements.RequirementDisposition.VERSION_MISMATCH, False, ("req:mismatch",)),
        ("unverifiable", unverifiable_result, requirements.RequirementDisposition.UNVERIFIABLE, False, ("req:unverified",)),
        ("optional", optional_missing_result, requirements.RequirementDisposition.MISSING, True, ()),
        ("alternative-a", alternative_results[0], requirements.RequirementDisposition.ALREADY_AVAILABLE, True, ()),
        ("alternative-b", alternative_results[1], requirements.RequirementDisposition.ALREADY_AVAILABLE, True, ()),
        ("alternative-none", alternative_results[2], requirements.RequirementDisposition.MISSING, False, ("req:alt-none",)),
        ("stale", stale_result, requirements.RequirementDisposition.UNVERIFIABLE, False, ("req:stale",)),
        ("false-positive", false_positive_result, requirements.RequirementDisposition.MISSING, False, ("req:false-positive",)),
    )
    disposition_divergences = sum(
        int(
            result.requirements[0].disposition is not expected_disposition
            or result.ready is not expected_ready
            or result.blocking_requirement_ids != expected_blocking
        )
        for _, result, expected_disposition, expected_ready, expected_blocking in matrix
    )
    repeat_results = tuple(_resolve(requirements_set) for _ in range(3))
    repeat_divergences = sum(
        int(repeat_results[0].canonical_bytes(normalize_time=True) != result.canonical_bytes(normalize_time=True))
        for result in repeat_results[1:]
    )
    blocking_starts = sum(
        int(requirements.task_start_allowed(result))
        for result in (missing_result, mismatch_result, unverifiable_result, stale_result, alternative_results[2])
    )

    alternative_false_positives = int(false_positive_result.requirements[0].disposition is requirements.RequirementDisposition.ALREADY_AVAILABLE)
    source_digest_used = int(source.digest != "")
    source_digest_used += int(requirements_set.requirements[0].source.digest != "")
    head_code, head = _git("rev-parse", "HEAD")
    tree_code, tree = _git("rev-parse", "HEAD^{tree}")
    status_code, status = _git("status", "--porcelain")
    diff_code, _ = _git("diff", "--check")
    clean = status_code == 0 and status == "" and diff_code == 0
    hardcoded_fields = _hardcoded_gate_fields()
    no_hardcoded = not hardcoded_fields
    all_pass = all(
        (
            bool(requirements.ENVIRONMENT_REQUIREMENT_VERSION_EXPLICIT),
            len(tuple(requirements.RequirementDisposition)) > 0,
            len(set(item.value for item in requirements.RequirementDisposition)) == len(tuple(requirements.RequirementDisposition)),
            not unverifiable_result.ready,
            not missing_result.ready,
            not mismatch_result.ready,
            not unverifiable_result.ready,
            not optional_missing_result.blocking_requirement_ids,
            len(alternative_cases) > 0,
            alternative_false_positives == 0,
            version_divergences == 0,
            not stale_result.ready,
            digest_divergences == 0,
            disposition_divergences == 0,
            repeat_divergences == 0,
            blocking_starts == 0,
            source_digest_used == 2,
            no_hardcoded,
            clean,
        )
    )
    ENVIRONMENT_REQUIREMENT_VERSION_EXPLICIT = bool(requirements.ENVIRONMENT_REQUIREMENT_VERSION_EXPLICIT)
    REQUIRED_DISPOSITIONS_DEFINED = len(tuple(requirements.RequirementDisposition))
    required_disposition_names = {
        "ALREADY_AVAILABLE",
        "MISSING",
        "VERSION_MISMATCH",
        "UNVERIFIABLE",
    }
    MISSING_REQUIRED_DISPOSITIONS = len(
        required_disposition_names
        - {item.value for item in requirements.RequirementDisposition}
    )
    UNVERIFIABLE_PROMOTED_TO_READY = unverifiable_result.ready
    REQUIRED_MISSING_RETURNS_READY = missing_result.ready
    REQUIRED_VERSION_MISMATCH_RETURNS_READY = mismatch_result.ready
    REQUIRED_UNVERIFIABLE_RETURNS_READY = unverifiable_result.ready
    OPTIONAL_MISSING_BLOCKS_READY = bool(optional_missing_result.blocking_requirement_ids)
    ALTERNATIVE_CAPABILITY_FIXTURES = len(alternative_cases)
    ALTERNATIVE_CAPABILITY_FALSE_POSITIVES = alternative_false_positives
    VERSION_RANGE_FIXTURES = len(version_cases) + 1
    VERSION_RANGE_DIVERGENCES = version_divergences
    STALE_INVENTORY_RETURNS_READY = stale_result.ready
    REQUIREMENT_DIGEST_DIVERGENCES = digest_divergences
    RESOLVER_FIXTURES = len(matrix)
    RESOLVER_DISPOSITION_DIVERGENCES = disposition_divergences
    RESOLVER_REPEAT_DIVERGENCES = repeat_divergences
    BLOCKING_REQUIREMENT_TASK_STARTS = blocking_starts
    HARDCODED_GATE_RESULT_FIELDS = hardcoded_fields
    NO_HARDCODED_GATE_RESULTS = no_hardcoded
    SOURCE_HEAD = head if head_code == 0 else ""
    SOURCE_TREE = tree if tree_code == 0 else ""
    WORKTREE_CLEAN = clean
    SOURCE_BOUND_MACHINE_GATE = "PASS" if clean and no_hardcoded else "FAIL"
    NX034_STATUS = "PASS" if all_pass and SOURCE_BOUND_MACHINE_GATE == "PASS" else "FAIL"
    return {
        "ENVIRONMENT_REQUIREMENT_VERSION_EXPLICIT": ENVIRONMENT_REQUIREMENT_VERSION_EXPLICIT,
        "REQUIRED_DISPOSITIONS_DEFINED": REQUIRED_DISPOSITIONS_DEFINED,
        "MISSING_REQUIRED_DISPOSITIONS": MISSING_REQUIRED_DISPOSITIONS,
        "UNVERIFIABLE_PROMOTED_TO_READY": UNVERIFIABLE_PROMOTED_TO_READY,
        "REQUIRED_MISSING_RETURNS_READY": REQUIRED_MISSING_RETURNS_READY,
        "REQUIRED_VERSION_MISMATCH_RETURNS_READY": REQUIRED_VERSION_MISMATCH_RETURNS_READY,
        "REQUIRED_UNVERIFIABLE_RETURNS_READY": REQUIRED_UNVERIFIABLE_RETURNS_READY,
        "OPTIONAL_MISSING_BLOCKS_READY": OPTIONAL_MISSING_BLOCKS_READY,
        "ALTERNATIVE_CAPABILITY_FIXTURES": ALTERNATIVE_CAPABILITY_FIXTURES,
        "ALTERNATIVE_CAPABILITY_FALSE_POSITIVES": ALTERNATIVE_CAPABILITY_FALSE_POSITIVES,
        "VERSION_RANGE_FIXTURES": VERSION_RANGE_FIXTURES,
        "VERSION_RANGE_DIVERGENCES": VERSION_RANGE_DIVERGENCES,
        "STALE_INVENTORY_RETURNS_READY": STALE_INVENTORY_RETURNS_READY,
        "REQUIREMENT_DIGEST_DIVERGENCES": REQUIREMENT_DIGEST_DIVERGENCES,
        "RESOLVER_FIXTURES": RESOLVER_FIXTURES,
        "RESOLVER_DISPOSITION_DIVERGENCES": RESOLVER_DISPOSITION_DIVERGENCES,
        "RESOLVER_REPEAT_DIVERGENCES": RESOLVER_REPEAT_DIVERGENCES,
        "BLOCKING_REQUIREMENT_TASK_STARTS": BLOCKING_REQUIREMENT_TASK_STARTS,
        "HARDCODED_GATE_RESULT_FIELDS": HARDCODED_GATE_RESULT_FIELDS,
        "NO_HARDCODED_GATE_RESULTS": NO_HARDCODED_GATE_RESULTS,
        "SOURCE_HEAD": SOURCE_HEAD,
        "SOURCE_TREE": SOURCE_TREE,
        "WORKTREE_CLEAN": WORKTREE_CLEAN,
        "SOURCE_BOUND_MACHINE_GATE": SOURCE_BOUND_MACHINE_GATE,
        "NX034_STATUS": NX034_STATUS,
    }


def test_nx034_machine_gate_execution() -> None:
    gate = run_nx034_machine_gate()
    print(json.dumps(gate, indent=2, sort_keys=True))
    assert gate["ENVIRONMENT_REQUIREMENT_VERSION_EXPLICIT"] is True
    assert gate["REQUIRED_DISPOSITIONS_DEFINED"] >= 4
    assert gate["MISSING_REQUIRED_DISPOSITIONS"] == 0
    assert gate["UNVERIFIABLE_PROMOTED_TO_READY"] is False
    assert gate["REQUIRED_MISSING_RETURNS_READY"] is False
    assert gate["REQUIRED_VERSION_MISMATCH_RETURNS_READY"] is False
    assert gate["REQUIRED_UNVERIFIABLE_RETURNS_READY"] is False
    assert gate["OPTIONAL_MISSING_BLOCKS_READY"] is False
    assert gate["ALTERNATIVE_CAPABILITY_FIXTURES"] >= 3
    assert gate["ALTERNATIVE_CAPABILITY_FALSE_POSITIVES"] == 0
    assert gate["VERSION_RANGE_FIXTURES"] >= 10
    assert gate["VERSION_RANGE_DIVERGENCES"] == 0
    assert gate["STALE_INVENTORY_RETURNS_READY"] is False
    assert gate["REQUIREMENT_DIGEST_DIVERGENCES"] == 0
    assert gate["RESOLVER_FIXTURES"] >= 8
    assert gate["RESOLVER_DISPOSITION_DIVERGENCES"] == 0
    assert gate["RESOLVER_REPEAT_DIVERGENCES"] == 0
    assert gate["BLOCKING_REQUIREMENT_TASK_STARTS"] == 0
    assert gate["HARDCODED_GATE_RESULT_FIELDS"] == []
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    assert gate["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
    assert gate["NX034_STATUS"] == "PASS"

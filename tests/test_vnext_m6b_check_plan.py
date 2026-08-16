from __future__ import annotations

from dataclasses import replace
import inspect

import pytest

from bdb_vnext.m6b_check_plan import (
    CHECKER_REGISTRY,
    DeterministicCheckPlanSelector,
    LegacyFixedProfileAdapter,
    LegacyProfileSnapshot,
    M6bError,
    ProcessPolicy,
    compare_legacy_profile,
    registry_digest,
)


EXECUTABLES = {
    "python": "C:/Python/python.exe",
    "dotnet": "C:/Program Files/dotnet/dotnet.exe",
    "shopify": "C:/Tools/shopify.cmd",
}


def test_same_inputs_produce_same_plan_and_canonical_order() -> None:
    selector = DeterministicCheckPlanSelector()
    first = selector.plan(
        required_capabilities=("python.unittest", "python.pytest", "python.pytest"),
        executable_bindings=EXECUTABLES,
    )
    second = selector.plan(
        required_capabilities=("python.pytest", "python.unittest"),
        executable_bindings=dict(reversed(tuple(EXECUTABLES.items()))),
    )
    assert first.plan_digest == second.plan_digest
    assert first.as_dict() == second.as_dict()
    assert first.required_capabilities == ("python.pytest", "python.unittest")
    assert tuple(item.capability_id for item in first.checks) == first.required_capabilities


def test_unknown_required_capability_fails_typed_and_closed() -> None:
    selector = DeterministicCheckPlanSelector()
    with pytest.raises(M6bError) as caught:
        selector.plan(
            required_capabilities=("unknown.required",),
            executable_bindings=EXECUTABLES,
        )
    assert caught.value.code == "required_capability_unknown"

    with pytest.raises(M6bError) as caught:
        selector.plan(
            required_capabilities=("dotnet.test",),
            executable_bindings={"python": EXECUTABLES["python"]},
        )
    assert caught.value.code == "required_capability_unknown"
    assert caught.value.details["executable_binding"] == "dotnet"


def test_supported_legacy_pytest_fixture_selects_same_checker_set_and_exact_argv() -> None:
    selector = DeterministicCheckPlanSelector()
    plan = selector.plan(
        required_capabilities=("python.pytest",),
        executable_bindings=EXECUTABLES,
    )
    legacy = LegacyFixedProfileAdapter().snapshot(
        profile_id="poc_pytest",
        executable_bindings=EXECUTABLES,
        timeout_seconds=30.0,
    )
    comparison = compare_legacy_profile(plan, legacy)

    assert plan.checks[0].argv == (
        "C:/Python/python.exe",
        "-m",
        "pytest",
        "-q",
    )
    assert comparison.checker_set_match is True
    assert plan.checker_set_digest == legacy.checker_set_digest
    # The old subprocess.run timeout owns only the direct child, while the
    # vNext ValidationRunner kills the process tree.  M6b detects this exact
    # safer execution-contract difference while remaining shadow-only.
    assert comparison.execution_contract_match is False
    assert comparison.status == "DIFFERENT"
    assert comparison.reasons == ("process_policy_mismatch:0",)


def test_shadow_detects_argv_and_timeout_difference() -> None:
    selector = DeterministicCheckPlanSelector()
    plan = selector.plan(
        required_capabilities=("python.pytest",),
        executable_bindings=EXECUTABLES,
    )
    legacy = LegacyFixedProfileAdapter().snapshot(
        profile_id="poc_pytest",
        executable_bindings=EXECUTABLES,
        timeout_seconds=31.0,
    )
    changed_check = replace(
        legacy.checks[0],
        argv=("C:/Python/python.exe", "-m", "pytest", "-qq"),
    )
    changed = LegacyProfileSnapshot(
        profile_id=legacy.profile_id,
        checks=(changed_check,),
        checker_set_digest=legacy.checker_set_digest,
        execution_contract_digest="sha256:" + "0" * 64,
    )
    comparison = compare_legacy_profile(plan, changed)
    assert "argv_mismatch:0" in comparison.reasons
    assert "timeout_mismatch:0" in comparison.reasons
    assert comparison.execution_contract_match is False


def test_legacy_adapter_is_not_part_of_selector_path(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("legacy adapter must not select the vNext plan")

    monkeypatch.setattr(LegacyFixedProfileAdapter, "snapshot", forbidden)
    plan = DeterministicCheckPlanSelector().plan(
        required_capabilities=("python.pytest",),
        executable_bindings=EXECUTABLES,
    )
    assert plan.checks[0].capability_id == "python.pytest"
    assert "LegacyFixedProfileAdapter" not in inspect.getsource(DeterministicCheckPlanSelector.plan)


def test_plan_contains_only_registry_commands_and_existing_validation_command_contract() -> None:
    selector = DeterministicCheckPlanSelector()
    plan = selector.plan(
        required_capabilities=(
            "dotnet.test",
            "python.pytest",
            "python.unittest",
            "shopify.theme_check",
        ),
        executable_bindings=EXECUTABLES,
    )
    assert plan.registry_digest == registry_digest(CHECKER_REGISTRY)
    for check in plan.checks:
        command = check.validation_command()
        assert command.argv == check.argv
        assert command.cwd == "."
        assert command.timeout_seconds == 30.0
        assert check.process_policy == ProcessPolicy(
            shell=False,
            stdin="DEVNULL",
            capture_stdout=True,
            capture_stderr=True,
            timeout_kill_scope="PROCESS_TREE",
        )


def test_staged_legacy_profile_is_not_falsely_claimed_as_equivalent() -> None:
    with pytest.raises(M6bError) as caught:
        LegacyFixedProfileAdapter().snapshot(
            profile_id="bdb_pytest_staged_v1",
            executable_bindings=EXECUTABLES,
            timeout_seconds=30.0,
        )
    assert caught.value.code == "legacy_profile_not_shadowable"


def test_registry_identity_is_order_independent_but_duplicate_capability_is_rejected() -> None:
    assert registry_digest(CHECKER_REGISTRY) == registry_digest(reversed(CHECKER_REGISTRY))
    duplicated = (*CHECKER_REGISTRY, CHECKER_REGISTRY[0])
    with pytest.raises(M6bError) as caught:
        DeterministicCheckPlanSelector(duplicated)
    assert caught.value.code == "duplicate_checker_capability"

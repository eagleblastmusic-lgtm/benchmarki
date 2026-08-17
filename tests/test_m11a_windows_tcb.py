from __future__ import annotations

import os
from pathlib import Path

import pytest

from bdb_vnext.m11a_windows_tcb import (
    ADMINISTRATORS_SID,
    M11aWindowsTcbError,
    SYSTEM_SID,
    USERS_SID,
    WINDOWS_ACL_WITNESS_SCHEMA,
    build_windows_tcb_witness,
    default_windows_authority_root,
    query_windows_acl,
    validate_acl_witness,
    validate_windows_tcb_topology,
)


def _safe_acl() -> dict[str, object]:
    return {
        "schema": WINDOWS_ACL_WITNESS_SCHEMA,
        "owner_sid": ADMINISTRATORS_SID,
        "inheritance_protected": True,
        "entries": [
            {"sid": SYSTEM_SID, "type": "Allow", "rights": ["FullControl"], "inherited": False},
            {"sid": ADMINISTRATORS_SID, "type": "Allow", "rights": ["FullControl"], "inherited": False},
            {"sid": USERS_SID, "type": "Allow", "rights": ["ReadAndExecute", "Synchronize"], "inherited": False},
        ],
    }


def test_acl_policy_allows_only_tcb_writers() -> None:
    observed = validate_acl_witness(_safe_acl())
    assert observed["owner_sid"] == ADMINISTRATORS_SID
    assert observed["inheritance_protected"] is True


def test_low_privilege_write_ace_is_fail_closed() -> None:
    value = _safe_acl()
    entries = list(value["entries"])  # type: ignore[arg-type]
    entries[-1] = {"sid": USERS_SID, "type": "Allow", "rights": ["Modify"], "inherited": False}
    value["entries"] = entries

    with pytest.raises(M11aWindowsTcbError) as caught:
        validate_acl_witness(value)

    assert caught.value.code == "candidate_write_authority"


def test_inherited_acl_is_fail_closed() -> None:
    value = _safe_acl()
    value["inheritance_protected"] = False
    with pytest.raises(M11aWindowsTcbError) as caught:
        validate_acl_witness(value)
    assert caught.value.code == "unsafe_acl_inheritance"


def test_programdata_topology_is_exact_and_external(tmp_path: Path) -> None:
    program_data = tmp_path / "ProgramData"
    authority = default_windows_authority_root(program_data)
    runtime = tmp_path / "LocalAppData" / "BartoszDevBridge-vNext"
    legacy = tmp_path / "LocalAppData" / "BartoszDevBridge"
    candidate = tmp_path / "bundles" / "candidate"

    topology = validate_windows_tcb_topology(
        authority_root=authority,
        program_data=program_data,
        runtime_root=runtime,
        legacy_runtime_root=legacy,
        mutable_roots=(candidate,),
    )

    assert topology["authority_root"] == str(authority.absolute())


def test_noncanonical_or_overlapping_authority_is_blocked(tmp_path: Path) -> None:
    program_data = tmp_path / "ProgramData"
    with pytest.raises(M11aWindowsTcbError) as wrong:
        validate_windows_tcb_topology(
            authority_root=tmp_path / "other",
            program_data=program_data,
            runtime_root=tmp_path / "runtime",
            legacy_runtime_root=tmp_path / "legacy",
        )
    assert wrong.value.code == "authority_root_mismatch"

    authority = default_windows_authority_root(program_data)
    with pytest.raises(M11aWindowsTcbError) as overlap:
        validate_windows_tcb_topology(
            authority_root=authority,
            program_data=program_data,
            runtime_root=authority / "runtime",
            legacy_runtime_root=tmp_path / "legacy",
        )
    assert overlap.value.code == "authority_overlap"


def test_build_witness_keeps_activation_unavailable(tmp_path: Path) -> None:
    program_data = tmp_path / "ProgramData"
    authority = default_windows_authority_root(program_data)
    witness = build_windows_tcb_witness(
        authority_root=authority,
        program_data=program_data,
        runtime_root=tmp_path / "runtime",
        legacy_runtime_root=tmp_path / "legacy",
        mutable_roots=(tmp_path / "candidate",),
        acl_witness=_safe_acl(),
    )

    assert witness["candidate_token"] == "STANDARD_USER_NON_ELEVATED"
    assert witness["candidate_may_write_authority"] is False
    assert witness["activation_operation_available"] is False
    assert witness["activation_deferred_to"] == "M11c"
    assert str(witness["witness_sha256"]).startswith("sha256:")


@pytest.mark.skipif(os.name != "nt", reason="real ACL observation is Windows-only")
def test_actions_windows_fixture_matches_tcb_policy() -> None:
    root = os.environ.get("BDB_VNEXT_TCB_FIXTURE")
    program_data = os.environ.get("BDB_VNEXT_TCB_PROGRAMDATA")
    if not root or not program_data:
        pytest.skip("Windows TCB fixture is supplied by the dedicated Actions job")

    acl = query_windows_acl(root)
    witness = build_windows_tcb_witness(
        authority_root=root,
        program_data=program_data,
        runtime_root=Path(program_data) / "runtime",
        legacy_runtime_root=Path(program_data) / "legacy",
        mutable_roots=(Path(program_data) / "candidate",),
        acl_witness=acl,
    )

    assert witness["acl"]["owner_sid"] in {ADMINISTRATORS_SID, SYSTEM_SID}
    assert witness["candidate_may_write_authority"] is False
    assert witness["activation_operation_available"] is False

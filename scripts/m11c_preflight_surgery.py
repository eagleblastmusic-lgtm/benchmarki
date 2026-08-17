from __future__ import annotations

import re
from pathlib import Path


PATH = Path("bdb_vnext/m11c_cutover.py")
SOURCE = PATH.read_text(encoding="utf-8")

REPLACEMENT = '''def apply_windows_cutover(
    *,
    authority_root: str | Path,
    cutover_id: str,
    expected_plan_sha256: str,
    operator_approved: bool,
) -> dict[str, Any]:
    """The only production-scoped M11c effect entrypoint; never runs off Windows."""

    if os.name != "nt":
        _fail("windows_required", "production M11c cutover requires Windows")
    if operator_approved is not True:
        _fail("operator_approval_required", "M11c cutover requires explicit operator approval")

    authority = _absolute_path(authority_root, field="authority_root")
    checked_cutover_id = _check_id(cutover_id, "cutover_id")
    expected = _check_digest(expected_plan_sha256, "expected_plan_sha256")
    plan = _load_plan(authority, checked_cutover_id)
    if plan["cutover_plan_sha256"] != expected:
        _fail("cutover_plan_stale", "operator approval is bound to a different cutover plan")

    runtime = _absolute_path(plan["runtime_root"], field="runtime_root")
    legacy = _absolute_path(plan["legacy_runtime_root"], field="legacy_runtime_root")
    try:
        require_client_verification(
            runtime_root=runtime,
            expected_client_plan_sha256=plan["client_plan_sha256"],
        )
    except M11cClientError as exc:
        raise M11cCutoverError(exc.code, str(exc)) from exc

    observed = observe_bootstrap_activation(authority_root=authority)
    if observed["status"] == "PREPARED":
        prepared_query = query_prepared_activation(
            authority_root=authority,
            preparation_id=plan["preparation_id"],
        )
        prepared = prepared_query["prepared"]
        if (
            prepared["preparation_sha256"] != plan["preparation_sha256"]
            or prepared["slot_binding"] != plan["slot_binding"]
        ):
            _fail("prepared_activation_stale", "M11a preparation differs from the approved cutover plan")
        backup = verify_backup(prepared["backup"]["path"])
        if backup.manifest_sha256 != prepared["backup"]["manifest_sha256"]:
            _fail("prepared_backup_stale", "prepared backup identity differs")
    elif observed["status"] == "ACTIVE":
        require_bootstrap_active(authority, expected_source_head=plan["source_head"])
    else:
        _fail("preactivation_state_required", "M11c cutover requires PREPARED or exact ACTIVE state")

    program_data = os.environ.get("PROGRAMDATA")
    if not program_data:
        _fail("programdata_unavailable", "PROGRAMDATA is required for production M11c")
    witness = build_windows_tcb_witness(
        authority_root=authority,
        program_data=program_data,
        runtime_root=runtime,
        legacy_runtime_root=legacy,
    )
    if _validate_tcb_witness(
        witness,
        authority=authority,
        runtime=runtime,
        legacy=legacy,
    ) != plan["tcb_witness_sha256"]:
        _fail("tcb_witness_stale", "current Windows TCB differs from the approved plan")

    try:
        routes = observe_windows_native_routes(runtime_root=runtime)
        if routes["target_conflict"] or not routes["target_registered"]:
            _fail("target_native_route_unverified", "exact vNext Native route is not registered")
        disable_windows_legacy_native_route(runtime_root=runtime)
    except M11cClientError as exc:
        raise M11cCutoverError(exc.code, str(exc)) from exc

    return _apply_cutover(
        authority_root=authority,
        cutover_id=checked_cutover_id,
        expected_plan_sha256=expected,
        operator_approved=True,
        tcb_witness=witness,
    )
'''

PATTERN = r"def apply_windows_cutover\([\s\S]*?\n\n__all__ ="
UPDATED, COUNT = re.subn(PATTERN, REPLACEMENT + "\n\n__all__ =", SOURCE, count=1)
assert COUNT == 1, f"expected one public apply replacement, observed {COUNT}"

apply_source = UPDATED.split("def apply_windows_cutover(", 1)[1].split("__all__ =", 1)[0]
assert apply_source.index("cutover_plan_stale") < apply_source.index("build_windows_tcb_witness")
assert apply_source.index("require_client_verification") < apply_source.index("build_windows_tcb_witness")
assert apply_source.index("verify_backup") < apply_source.index("disable_windows_legacy_native_route")
assert apply_source.index("build_windows_tcb_witness") < apply_source.index("disable_windows_legacy_native_route")

PATH.write_text(UPDATED, encoding="utf-8", newline="\n")

"""External Bootstrap admin surface with no activation command.

M11a originally exposed observation/TCB/preparation only. At the later M11c
pre-staging boundary this same authority now also exposes TCB-gated slot
initialization, candidate staging and candidate discard. None of these verbs
can promote CANDIDATE to ACTIVE; production switching remains M11c-only.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from bdb_shared.evidence import canonical_json_bytes
from bdb_vnext.bootstrap import BootstrapError, _absolute_path
from bdb_vnext.composition import GENERATION_ID, RUNTIME_ID
from bdb_vnext.m11a_bootstrap_slots import (
    SlotSource,
    discard_candidate_slot,
    initialize_slot_authority,
    query_slot_authority,
    stage_candidate_slot,
)
from bdb_vnext.m11a_prepared_activation import prepare_candidate_activation, query_prepared_activation
from bdb_vnext.m11a_windows_tcb import M11aWindowsTcbError, build_windows_tcb_witness


ADMIN_RESULT_SCHEMA = "bdb-vnext-bootstrap-admin-result-v1"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bdb-vnext-bootstrap-admin",
        description="External BDB Next Bootstrap preflight surface. Activation is not available here.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="revalidate external slot/preparation state")
    status.add_argument("--authority-root", required=True)
    status.add_argument("--preparation-id")

    verify = subparsers.add_parser("verify-tcb", help="verify exact ProgramData topology and ACL authority")
    verify.add_argument("--authority-root", required=True)
    verify.add_argument("--runtime-root", required=True)
    verify.add_argument("--legacy-runtime-root", required=True)
    verify.add_argument("--mutable-root", action="append", default=[])

    initialize = subparsers.add_parser("initialize", help="record initial ACTIVE/PREVIOUS build subjects; never activates")
    initialize.add_argument("--authority-root", required=True)
    initialize.add_argument("--runtime-root", required=True)
    initialize.add_argument("--legacy-runtime-root", required=True)
    initialize.add_argument("--active-bundle-root", required=True)
    initialize.add_argument("--active-bundle-sha256", required=True)
    initialize.add_argument("--active-bundle-role", choices=("candidate", "recovery"), default="candidate")
    initialize.add_argument("--previous-bundle-root", required=True)
    initialize.add_argument("--previous-bundle-sha256", required=True)
    initialize.add_argument("--previous-bundle-role", choices=("candidate", "recovery"), default="recovery")
    initialize.add_argument("--required-control-schema", type=int, required=True)
    initialize.add_argument("--capability", action="append", default=[])

    stage = subparsers.add_parser("stage-candidate", help="stage one exact compatible CANDIDATE; preserves ACTIVE")
    stage.add_argument("--authority-root", required=True)
    stage.add_argument("--runtime-root", required=True)
    stage.add_argument("--legacy-runtime-root", required=True)
    stage.add_argument("--candidate-bundle-root", required=True)
    stage.add_argument("--candidate-bundle-sha256", required=True)

    discard = subparsers.add_parser("discard-candidate", help="discard staged CANDIDATE; preserves ACTIVE")
    discard.add_argument("--authority-root", required=True)
    discard.add_argument("--runtime-root", required=True)
    discard.add_argument("--legacy-runtime-root", required=True)

    prepare = subparsers.add_parser("prepare", help="create M11a preparation evidence without activation")
    prepare.add_argument("--authority-root", required=True)
    prepare.add_argument("--runtime-root", required=True)
    prepare.add_argument("--legacy-runtime-root", required=True)
    prepare.add_argument("--recovery-target", required=True)
    prepare.add_argument("--preparation-id", required=True)
    prepare.add_argument("--health-timeout", type=float, default=10.0)
    prepare.add_argument("--source-quiesced", action="store_true")
    prepare.add_argument("--include-control-identity", action="store_true")
    return parser


def _program_data() -> Path:
    value = os.environ.get("PROGRAMDATA")
    if not value:
        raise M11aWindowsTcbError("programdata_unavailable", "ProgramData is required for Bootstrap admin")
    return Path(value).expanduser().absolute()


def _mutable_roots_from_slots(query: Mapping[str, Any]) -> tuple[str, ...]:
    roots: list[str] = []
    candidate = query.get("slots", {}).get("CANDIDATE") if isinstance(query.get("slots"), Mapping) else None
    if isinstance(candidate, Mapping):
        root = candidate.get("bundle_root")
        if isinstance(root, str) and root:
            roots.append(root)
    return tuple(roots)


def _assert_legacy_binding(query: Mapping[str, Any], supplied: str | Path) -> None:
    state = query.get("state")
    if not isinstance(state, Mapping):
        raise M11aWindowsTcbError("slot_state_missing", "external slot state is unavailable")
    expected = state.get("legacy_runtime_root")
    actual = str(_absolute_path(supplied, field="legacy_runtime_root"))
    if expected != actual:
        raise M11aWindowsTcbError("legacy_runtime_mismatch", "Bootstrap admin legacy root differs from slot authority")


def _tcb_for_current_slots(
    *,
    authority_root: str | Path,
    runtime_root: str | Path,
    legacy_runtime_root: str | Path,
    extra_mutable_roots: Sequence[str | Path] = (),
) -> tuple[dict[str, Any], dict[str, Any]]:
    slots = query_slot_authority(authority_root=authority_root)
    _assert_legacy_binding(slots, legacy_runtime_root)
    mutable = (*_mutable_roots_from_slots(slots), *tuple(extra_mutable_roots))
    witness = build_windows_tcb_witness(
        authority_root=authority_root,
        program_data=_program_data(),
        runtime_root=runtime_root,
        legacy_runtime_root=legacy_runtime_root,
        mutable_roots=mutable,
    )
    return slots, witness


def _result(operation: str, **values: Any) -> dict[str, Any]:
    return {
        "schema": ADMIN_RESULT_SCHEMA,
        "runtime_id": RUNTIME_ID,
        "generation_id": GENERATION_ID,
        "operation": operation,
        **values,
        "activation_operation_available": False,
        "activation_deferred_to": "M11c",
    }


def _execute(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "status":
        slots = query_slot_authority(authority_root=args.authority_root)
        prepared = None
        if args.preparation_id:
            prepared = query_prepared_activation(authority_root=args.authority_root, preparation_id=args.preparation_id)
        return _result("STATUS", slots=slots, prepared=prepared)

    if args.command == "verify-tcb":
        slots, witness = _tcb_for_current_slots(
            authority_root=args.authority_root,
            runtime_root=args.runtime_root,
            legacy_runtime_root=args.legacy_runtime_root,
            extra_mutable_roots=tuple(args.mutable_root),
        )
        return _result("VERIFY_TCB", slots=slots, tcb=witness)

    if args.command == "initialize":
        active = SlotSource(
            "ACTIVE",
            Path(args.active_bundle_root).expanduser().absolute(),
            args.active_bundle_sha256,
            args.active_bundle_role,
            tuple(args.capability),
        )
        previous = SlotSource(
            "PREVIOUS",
            Path(args.previous_bundle_root).expanduser().absolute(),
            args.previous_bundle_sha256,
            args.previous_bundle_role,
            tuple(args.capability),
        )
        witness = build_windows_tcb_witness(
            authority_root=args.authority_root,
            program_data=_program_data(),
            runtime_root=args.runtime_root,
            legacy_runtime_root=args.legacy_runtime_root,
            mutable_roots=(active.bundle_root, previous.bundle_root),
        )
        slots = initialize_slot_authority(
            authority_root=args.authority_root,
            legacy_runtime_root=args.legacy_runtime_root,
            active=active,
            previous=previous,
            required_control_schema=args.required_control_schema,
            required_capabilities=tuple(args.capability),
        )
        return _result("INITIALIZE", slots=slots, tcb=witness)

    if args.command == "stage-candidate":
        slots, witness = _tcb_for_current_slots(
            authority_root=args.authority_root,
            runtime_root=args.runtime_root,
            legacy_runtime_root=args.legacy_runtime_root,
            extra_mutable_roots=(args.candidate_bundle_root,),
        )
        state = slots.get("state")
        if not isinstance(state, Mapping):
            raise M11aWindowsTcbError("slot_state_missing", "external slot state is unavailable")
        capabilities = state.get("required_capabilities")
        if not isinstance(capabilities, list) or any(not isinstance(item, str) for item in capabilities):
            raise M11aWindowsTcbError("slot_state_invalid", "required capabilities are invalid")
        candidate = SlotSource(
            "CANDIDATE",
            Path(args.candidate_bundle_root).expanduser().absolute(),
            args.candidate_bundle_sha256,
            "candidate",
            tuple(capabilities),
        )
        staged = stage_candidate_slot(authority_root=args.authority_root, candidate=candidate)
        return _result("STAGE_CANDIDATE", slots=staged, tcb=witness)

    if args.command == "discard-candidate":
        _slots, witness = _tcb_for_current_slots(
            authority_root=args.authority_root,
            runtime_root=args.runtime_root,
            legacy_runtime_root=args.legacy_runtime_root,
        )
        discarded = discard_candidate_slot(authority_root=args.authority_root)
        return _result("DISCARD_CANDIDATE", slots=discarded, tcb=witness)

    if args.command == "prepare":
        slots, witness = _tcb_for_current_slots(
            authority_root=args.authority_root,
            runtime_root=args.runtime_root,
            legacy_runtime_root=args.legacy_runtime_root,
        )
        if slots.get("slots", {}).get("CANDIDATE") is None:
            raise M11aWindowsTcbError("candidate_required", "Bootstrap admin prepare requires a staged CANDIDATE")
        prepared = prepare_candidate_activation(
            authority_root=args.authority_root,
            runtime_root=args.runtime_root,
            recovery_target=args.recovery_target,
            preparation_id=args.preparation_id,
            source_is_quiesced=bool(args.source_quiesced),
            health_timeout_seconds=float(args.health_timeout),
            include_control_identity=bool(args.include_control_identity),
        )
        return _result("PREPARE", tcb=witness, prepared=prepared)

    raise M11aWindowsTcbError("unsupported_operation", "Bootstrap admin operation is unsupported")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        output: Mapping[str, Any] = _execute(args)
        exit_code = 0
    except (BootstrapError, M11aWindowsTcbError, OSError, ValueError) as error:
        code = getattr(error, "code", "bootstrap_admin_failed")
        output = _result("BLOCKED", error={"code": str(code), "message": str(error)})
        exit_code = 20
    sys.stdout.buffer.write(canonical_json_bytes(output))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["ADMIN_RESULT_SCHEMA", "main"]

"""External M11a Bootstrap admin surface; deliberately no activation command.

The CLI exposes observation, Windows TCB verification and prepared-activation
preflight.  It never changes ACTIVE and has no CANDIDATE -> ACTIVE verb.  The
actual production switch remains an M11c-only capability.
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
from bdb_vnext.m11a_bootstrap_slots import query_slot_authority
from bdb_vnext.m11a_prepared_activation import prepare_candidate_activation, query_prepared_activation
from bdb_vnext.m11a_windows_tcb import M11aWindowsTcbError, build_windows_tcb_witness


ADMIN_RESULT_SCHEMA = "bdb-vnext-bootstrap-admin-result-v1"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bdb-vnext-bootstrap-admin",
        description="External BDB Next Bootstrap preflight surface. Activation is not available in M11a.",
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


def _execute(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "status":
        slots = query_slot_authority(authority_root=args.authority_root)
        prepared = None
        if args.preparation_id:
            prepared = query_prepared_activation(
                authority_root=args.authority_root,
                preparation_id=args.preparation_id,
            )
        return {
            "schema": ADMIN_RESULT_SCHEMA,
            "runtime_id": RUNTIME_ID,
            "generation_id": GENERATION_ID,
            "operation": "STATUS",
            "slots": slots,
            "prepared": prepared,
            "activation_operation_available": False,
            "activation_deferred_to": "M11c",
        }

    if args.command == "verify-tcb":
        slots, witness = _tcb_for_current_slots(
            authority_root=args.authority_root,
            runtime_root=args.runtime_root,
            legacy_runtime_root=args.legacy_runtime_root,
            extra_mutable_roots=tuple(args.mutable_root),
        )
        return {
            "schema": ADMIN_RESULT_SCHEMA,
            "runtime_id": RUNTIME_ID,
            "generation_id": GENERATION_ID,
            "operation": "VERIFY_TCB",
            "slots": slots,
            "tcb": witness,
            "activation_operation_available": False,
            "activation_deferred_to": "M11c",
        }

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
        return {
            "schema": ADMIN_RESULT_SCHEMA,
            "runtime_id": RUNTIME_ID,
            "generation_id": GENERATION_ID,
            "operation": "PREPARE",
            "tcb": witness,
            "prepared": prepared,
            "activation_operation_available": False,
            "activation_deferred_to": "M11c",
        }

    raise M11aWindowsTcbError("unsupported_operation", "Bootstrap admin operation is unsupported")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        output: Mapping[str, Any] = _execute(args)
        exit_code = 0
    except (BootstrapError, M11aWindowsTcbError, OSError, ValueError) as error:
        code = getattr(error, "code", "bootstrap_admin_failed")
        output = {
            "schema": ADMIN_RESULT_SCHEMA,
            "runtime_id": RUNTIME_ID,
            "generation_id": GENERATION_ID,
            "operation": "BLOCKED",
            "error": {"code": str(code), "message": str(error)},
            "activation_operation_available": False,
            "activation_deferred_to": "M11c",
        }
        exit_code = 20
    sys.stdout.buffer.write(canonical_json_bytes(output))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["ADMIN_RESULT_SCHEMA", "main"]

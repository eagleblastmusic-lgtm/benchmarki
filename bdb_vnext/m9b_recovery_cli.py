"""Operator CLI for the exact missing-M9b recovery boundary."""

from __future__ import annotations

import argparse
from typing import Sequence

from bdb_shared.evidence import canonical_json_bytes
from bdb_vnext.m9b_recovery import (
    M9bRecoveryError,
    prepare_missing_m9b_recovery,
    query_missing_m9b_recovery,
    recover_missing_m9b,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bdb-vnext-m9b-recovery")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="prepare an immutable missing-M9b subject")
    prepare.add_argument("--authority-root", required=True)
    prepare.add_argument("--deployed-runtime-root", required=True)
    prepare.add_argument("--recovery-id", required=True)
    prepare.add_argument("--historical-reconciliation-id", required=True)
    prepare.add_argument("--historical-reconciliation-plan-sha256", required=True)

    query = sub.add_parser("query", help="read an immutable recovery subject")
    query.add_argument("--authority-root", required=True)
    query.add_argument("--deployed-runtime-root", required=True)
    query.add_argument("--recovery-id", required=True)
    query.add_argument("--plan-sha256")

    recover = sub.add_parser("recover", help="apply one exact approved recovery plan")
    recover.add_argument("--authority-root", required=True)
    recover.add_argument("--deployed-runtime-root", required=True)
    recover.add_argument("--recovery-id", required=True)
    recover.add_argument("--plan-sha256", required=True)
    recover.add_argument("--approve", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "prepare":
            result = prepare_missing_m9b_recovery(
                authority_root=args.authority_root,
                deployed_runtime_root=args.deployed_runtime_root,
                recovery_id=args.recovery_id,
                historical_reconciliation_id=args.historical_reconciliation_id,
                historical_reconciliation_plan_sha256=args.historical_reconciliation_plan_sha256,
            )
        elif args.command == "query":
            result = query_missing_m9b_recovery(
                authority_root=args.authority_root,
                deployed_runtime_root=args.deployed_runtime_root,
                recovery_id=args.recovery_id,
                expected_plan_sha256=args.plan_sha256,
            )
        else:
            result = recover_missing_m9b(
                authority_root=args.authority_root,
                deployed_runtime_root=args.deployed_runtime_root,
                recovery_id=args.recovery_id,
                expected_plan_sha256=args.plan_sha256,
                operator_approved=args.approve,
            )
        print(canonical_json_bytes(result).decode("utf-8"))
        return 0
    except (M9bRecoveryError, OSError, ValueError) as exc:
        print(canonical_json_bytes({
            "schema": "bdb-vnext-m9b-missing-recovery-error-v1",
            "status": "BLOCKED",
            "error_code": getattr(exc, "code", "m9b_recovery_failed"),
            "error": str(exc),
            "production_activation_performed": False,
        }).decode("utf-8"))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]

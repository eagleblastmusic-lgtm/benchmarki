"""Operator CLI for the bounded post-ACTIVE maintenance boundary."""

from __future__ import annotations

import argparse
from typing import Sequence

from bdb_shared.evidence import canonical_json_bytes
from bdb_vnext.m11c_post_active_maintenance import (
    M11cMaintenanceError,
    apply_post_active_maintenance,
    prepare_post_active_maintenance,
    query_post_active_maintenance,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bdb-vnext-maintenance")
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    for name in ("authority-root", "candidate-bundle-root", "candidate-client-runtime-root", "source-head", "source-tree", "native-artifact-manifest-sha256", "maintenance-id"):
        prepare.add_argument(f"--{name}", required=True)
    prepare.add_argument("--candidate-bundle-sha256", required=True)
    query = sub.add_parser("query")
    query.add_argument("--authority-root", required=True)
    query.add_argument("--maintenance-id", required=True)
    apply = sub.add_parser("apply")
    apply.add_argument("--authority-root", required=True)
    apply.add_argument("--maintenance-id", required=True)
    apply.add_argument("--plan-sha256", required=True)
    apply.add_argument("--approve", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "prepare":
            result = prepare_post_active_maintenance(
                authority_root=args.authority_root,
                candidate_bundle_root=args.candidate_bundle_root,
                candidate_bundle_sha256=args.candidate_bundle_sha256,
                candidate_client_runtime_root=args.candidate_client_runtime_root,
                source_head=args.source_head,
                source_tree=args.source_tree,
                native_artifact_manifest_sha256=args.native_artifact_manifest_sha256,
                maintenance_id=args.maintenance_id,
            )
        elif args.command == "query":
            result = query_post_active_maintenance(authority_root=args.authority_root, maintenance_id=args.maintenance_id)
        else:
            result = apply_post_active_maintenance(
                authority_root=args.authority_root,
                maintenance_id=args.maintenance_id,
                expected_plan_sha256=args.plan_sha256,
                operator_approved=args.approve,
            )
        print(canonical_json_bytes(result).decode("utf-8"))
        return 0
    except (M11cMaintenanceError, OSError, ValueError) as exc:
        print(canonical_json_bytes({"status": "BLOCKED", "error_code": getattr(exc, "code", "maintenance_failed"), "error": str(exc)}).decode("utf-8"))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]

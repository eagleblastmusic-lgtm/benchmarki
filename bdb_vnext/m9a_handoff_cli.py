"""Operator CLI for revised side-by-side M9a evidence.

The CLI has no activation, disable, install, or global Legacy freeze verb.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from bdb_shared.evidence import canonical_json_bytes
from bdb_vnext.m9a_handoff import (
    M9aHandoffError,
    capture_side_by_side_handoff,
    revalidate_side_by_side_digest,
    verify_side_by_side_report,
)


def _emit(value: Any) -> None:
    print(canonical_json_bytes(value).decode("utf-8"))


def _load_report(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser().absolute()
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise M9aHandoffError("report_unreadable", "M9a report is not valid readable JSON") from exc
    if not isinstance(value, dict):
        raise M9aHandoffError("report_invalid", "M9a report must be a JSON object")
    if value.get("schema") == "bdb-vnext-m9a-side-by-side-result-v1":
        nested = value.get("report")
        if not isinstance(nested, dict):
            raise M9aHandoffError("report_invalid", "M9a capture result has no report object")
        return nested
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BDB Next side-by-side M9a handoff evidence")
    sub = parser.add_subparsers(dest="command", required=True)

    capture = sub.add_parser("capture", help="capture scoped PASS_CLOSED/BLOCKED handoff evidence")
    capture.add_argument("--runtime-root", required=True)
    capture.add_argument("--legacy-runtime-root", required=True)
    capture.add_argument("--observation-seconds", type=float, default=2.0)

    verify = sub.add_parser("verify", help="verify a report against local content-addressed evidence")
    verify.add_argument("--runtime-root", required=True)
    verify.add_argument("--report", required=True)

    revalidate = sub.add_parser("revalidate", help="re-observe the scoped handoff fence")
    revalidate.add_argument("--runtime-root", required=True)
    revalidate.add_argument("--legacy-runtime-root", required=True)
    revalidate.add_argument("--freeze-digest", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "capture":
            _emit(
                capture_side_by_side_handoff(
                    runtime_root=args.runtime_root,
                    legacy_runtime_root=args.legacy_runtime_root,
                    observation_seconds=args.observation_seconds,
                )
            )
            return 0
        if args.command == "verify":
            report = _load_report(args.report)
            digest = verify_side_by_side_report(runtime_root=args.runtime_root, report=report)
            _emit(
                {
                    "schema": "bdb-vnext-m9a-side-by-side-verify-v1",
                    "status": "VERIFIED",
                    "freeze_digest": digest,
                    "production_activation_performed": False,
                    "legacy_product_globally_disabled": False,
                }
            )
            return 0
        digest = revalidate_side_by_side_digest(
            runtime_root=args.runtime_root,
            legacy_runtime_root=args.legacy_runtime_root,
            freeze_digest=args.freeze_digest,
        )
        _emit(
            {
                "schema": "bdb-vnext-m9a-side-by-side-revalidate-v1",
                "status": "CURRENT",
                "freeze_digest": digest,
                "production_activation_performed": False,
                "legacy_product_globally_disabled": False,
            }
        )
        return 0
    except M9aHandoffError as exc:
        _emit(
            {
                "schema": "bdb-vnext-m9a-side-by-side-error-v1",
                "status": "BLOCKED",
                "error_code": exc.code,
                "error": str(exc),
                "production_activation_performed": False,
                "legacy_product_globally_disabled": False,
            }
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Build-only CLI for verified vNext Native Host artifacts and slot bundles."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from typing import Any

from bdb_shared.evidence import canonical_json_bytes
from bdb_vnext.bootstrap import BootstrapError
from bdb_vnext.m11c_native_artifact import (
    M11cArtifactError,
    build_windows_native_artifact,
    materialize_runtime_bundle,
    verify_native_artifact,
)


CLI_SCHEMA = "bdb-vnext-native-artifact-cli-v1"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bdb-vnext-artifact", description="Build/verify frozen vNext Native Host subjects; never activates")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build-native")
    build.add_argument("--repo-root", required=True)
    build.add_argument("--output-root", required=True)

    verify = sub.add_parser("verify-native")
    verify.add_argument("--manifest", required=True)
    verify.add_argument("--source-head")
    verify.add_argument("--source-tree")

    bundle = sub.add_parser("materialize-bundle")
    bundle.add_argument("--artifact-manifest", required=True)
    bundle.add_argument("--output-root", required=True)
    bundle.add_argument("--legacy-runtime-root", required=True)
    bundle.add_argument("--role", required=True, choices=("candidate", "recovery"))
    bundle.add_argument("--bundle-id", required=True)
    bundle.add_argument("--known-good", action="store_true")
    bundle.add_argument("--control-schema-min", type=int, default=1)
    bundle.add_argument("--control-schema-max", type=int, default=1)
    return parser


def _artifact(value: Any) -> dict[str, Any]:
    return {
        "manifest_path": str(value.manifest_path),
        "executable_path": str(value.executable_path),
        "source_head": value.source_head,
        "source_tree": value.source_tree,
        "executable_sha256": value.executable_sha256,
        "executable_size_bytes": value.executable_size_bytes,
        "manifest_sha256": value.manifest_sha256,
    }


def _execute(args: argparse.Namespace) -> Mapping[str, Any]:
    if args.command == "build-native":
        artifact = build_windows_native_artifact(repo_root=args.repo_root, output_root=args.output_root)
        return {"schema": CLI_SCHEMA, "operation": "BUILD_NATIVE", "artifact": _artifact(artifact), "production_activation_performed": False}
    if args.command == "verify-native":
        artifact = verify_native_artifact(args.manifest, expected_source_head=args.source_head, expected_source_tree=args.source_tree)
        return {"schema": CLI_SCHEMA, "operation": "VERIFY_NATIVE", "artifact": _artifact(artifact), "production_activation_performed": False}
    if args.command == "materialize-bundle":
        bundle = materialize_runtime_bundle(
            artifact_manifest=args.artifact_manifest,
            output_root=args.output_root,
            legacy_runtime_root=args.legacy_runtime_root,
            role=args.role,
            bundle_id=args.bundle_id,
            known_good=bool(args.known_good),
            supported_control_schema=(args.control_schema_min, args.control_schema_max),
        )
        return {"schema": CLI_SCHEMA, "operation": "MATERIALIZE_BUNDLE", "bundle": bundle, "production_activation_performed": False}
    raise M11cArtifactError("unsupported_operation", "artifact operation is unsupported")


def main(argv: list[str] | None = None) -> int:
    try:
        output = _execute(_parser().parse_args(argv))
        code = 0
    except (M11cArtifactError, BootstrapError, OSError, ValueError) as exc:
        output = {"schema": CLI_SCHEMA, "operation": "BLOCKED", "error_code": getattr(exc, "code", "artifact_failed"), "error": str(exc), "production_activation_performed": False}
        code = 20
    print(canonical_json_bytes(output).decode("utf-8"))
    return code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["CLI_SCHEMA", "main"]

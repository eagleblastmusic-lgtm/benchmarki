"""Operator CLI for the single M11c External Bootstrap cutover path.

Staging consumes a verified frozen Native Host artifact. The one production
apply boundary still requires an exact reviewed cutover-plan SHA.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from bdb_shared.evidence import canonical_json_bytes
from bdb_vnext.bootstrap import BootstrapError
from bdb_vnext.m11c_cutover import (
    M11cCutoverError,
    apply_windows_cutover,
    observe_bootstrap_activation,
    prepare_windows_cutover_plan,
    query_cutover_plan,
)
from bdb_vnext.m11c_native_artifact import M11cArtifactError, verify_native_artifact
from bdb_vnext.m11c_windows_clients import (
    M11cClientError,
    observe_windows_native_routes,
    query_client_plan,
    register_windows_target_native_host,
    require_client_verification,
    stage_client_plan,
)
from bdb_vnext.m9b_activation import M9bActivationError, read_activation


CLI_SCHEMA = "bdb-vnext-m11c-cutover-cli-v1"


def _load_json_file(path: str | Path, *, field: str) -> dict[str, Any]:
    source = Path(path).expanduser().absolute()
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise M11cCutoverError("invalid_operator_input", f"{field} is not readable JSON") from exc
    if not isinstance(value, Mapping):
        raise M11cCutoverError("invalid_operator_input", f"{field} must be a JSON object")
    return {str(key): item for key, item in value.items()}


def _emit(document: Mapping[str, Any]) -> None:
    print(canonical_json_bytes(document).decode("utf-8"))


def _stage_clients(args: argparse.Namespace) -> dict[str, Any]:
    artifact = verify_native_artifact(
        args.native_host_artifact_manifest,
        expected_source_head=args.source_head,
        expected_source_tree=args.source_tree,
    )
    result = stage_client_plan(
        runtime_root=args.runtime_root,
        legacy_runtime_root=args.legacy_runtime_root,
        bootstrap_authority_root=args.authority_root,
        browser_source_root=args.browser_source_root,
        native_host_executable=artifact.executable_path,
        source_head=args.source_head,
        source_tree=args.source_tree,
    )
    plan = result["plan"]
    if plan["native_host_executable_sha256"] != artifact.executable_sha256:
        raise M11cArtifactError("client_artifact_binding_mismatch", "client plan does not bind the verified Native Host bytes")
    return {
        "schema": CLI_SCHEMA,
        "action": "stage-clients",
        "status": "STAGED_NOT_ACTIVATED",
        "client_plan_sha256": plan["client_plan_sha256"],
        "native_artifact_manifest_sha256": artifact.manifest_sha256,
        "native_executable_sha256": artifact.executable_sha256,
        "browser_extension_id": plan["browser_extension_id"],
        "browser_load_unpacked_root": plan["browser_bundle_root"],
        "browser_operator_action_required": True,
        "native_manifest_path": plan["native_manifest_path"],
        "production_activation_performed": False,
    }


def _register_native(args: argparse.Namespace) -> dict[str, Any]:
    routes = register_windows_target_native_host(runtime_root=args.runtime_root, replace_existing_target=args.replace_existing_target)
    return {
        "schema": CLI_SCHEMA,
        "action": "register-native",
        "status": "TARGET_REGISTERED_NOT_ACTIVATED",
        "stale_target_replacement_requested": bool(args.replace_existing_target),
        "routes": routes,
        "production_activation_performed": False,
    }


def _client_status(args: argparse.Namespace) -> dict[str, Any]:
    plan = query_client_plan(runtime_root=args.runtime_root)["plan"]
    try:
        verification = require_client_verification(runtime_root=args.runtime_root, expected_client_plan_sha256=plan["client_plan_sha256"])
    except FileNotFoundError:
        verification = None
    except BootstrapError as exc:
        if exc.code == "missing_file":
            verification = None
        else:
            raise M11cClientError(exc.code, str(exc)) from exc
    try:
        routes = observe_windows_native_routes(runtime_root=args.runtime_root)
    except M11cClientError as exc:
        if exc.code == "windows_required":
            routes = {"status": "WINDOWS_ONLY", "target_registered": False, "legacy_route_present": None}
        else:
            raise
    return {
        "schema": CLI_SCHEMA,
        "action": "client-status",
        "status": "VERIFIED" if verification is not None else "WAITING_FOR_BROWSER_VERIFICATION",
        "client_plan": plan,
        "browser_verification": verification,
        "native_routes": routes,
        "production_activation_performed": False,
    }


def _prepare(args: argparse.Namespace) -> dict[str, Any]:
    report = _load_json_file(args.m9a_report, field="m9a_report")
    client_plan = query_client_plan(runtime_root=args.runtime_root)["plan"]
    result = prepare_windows_cutover_plan(
        authority_root=args.authority_root,
        runtime_root=args.runtime_root,
        legacy_runtime_root=args.legacy_runtime_root,
        preparation_id=args.preparation_id,
        cutover_id=args.cutover_id,
        source_head=args.source_head,
        source_tree=args.source_tree,
        m9a_report=report,
        browser_bundle_digest=client_plan["browser_bundle_digest"],
        native_manifest_digest=client_plan["native_manifest_sha256"],
    )
    return {"schema": CLI_SCHEMA, "action": "prepare", "status": "PREPARED_NOT_ACTIVATED", "cutover_plan": result["plan"], "production_activation_performed": False}


def _status(args: argparse.Namespace) -> dict[str, Any]:
    bootstrap = observe_bootstrap_activation(authority_root=args.authority_root)
    try:
        cutover = query_cutover_plan(authority_root=args.authority_root, cutover_id=args.cutover_id)
    except (M11cCutoverError, FileNotFoundError):
        cutover = None
    client_gate = read_activation(args.runtime_root)
    return {"schema": CLI_SCHEMA, "action": "status", "bootstrap": bootstrap, "cutover_plan": cutover, "client_gate": client_gate.as_dict() if client_gate is not None else None, "production_activation_performed": bootstrap.get("production_activation_performed") is True}


def _apply(args: argparse.Namespace) -> dict[str, Any]:
    result = apply_windows_cutover(authority_root=args.authority_root, cutover_id=args.cutover_id, expected_plan_sha256=args.approve_plan_sha256, operator_approved=True)
    return {"schema": CLI_SCHEMA, "action": "apply", "status": result["status"], "result": result, "production_activation_performed": result["production_activation_performed"]}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BDB Next M11c External Bootstrap cutover launcher")
    sub = parser.add_subparsers(dest="command", required=True)

    stage = sub.add_parser("stage-clients", help="stage exact Browser/Native client subjects only")
    stage.add_argument("--runtime-root", required=True)
    stage.add_argument("--legacy-runtime-root", required=True)
    stage.add_argument("--authority-root", required=True)
    stage.add_argument("--browser-source-root", required=True)
    stage.add_argument("--native-host-artifact-manifest", required=True)
    stage.add_argument("--source-head", required=True)
    stage.add_argument("--source-tree", required=True)
    stage.set_defaults(handler=_stage_clients)

    register = sub.add_parser("register-native", help="register only the dedicated vNext Native host")
    register.add_argument("--runtime-root", required=True)
    register.add_argument("--replace-existing-target", action="store_true", help="backup and replace an existing conflicting HKCU vNext rehearsal registration; HKLM still blocks")
    register.set_defaults(handler=_register_native)

    client_status = sub.add_parser("client-status", help="observe staged clients and Browser launch witness")
    client_status.add_argument("--runtime-root", required=True)
    client_status.set_defaults(handler=_client_status)

    prepare = sub.add_parser("prepare", help="prepare exact final cutover plan; does not activate")
    prepare.add_argument("--authority-root", required=True)
    prepare.add_argument("--runtime-root", required=True)
    prepare.add_argument("--legacy-runtime-root", required=True)
    prepare.add_argument("--preparation-id", required=True)
    prepare.add_argument("--cutover-id", required=True)
    prepare.add_argument("--source-head", required=True)
    prepare.add_argument("--source-tree", required=True)
    prepare.add_argument("--m9a-report", required=True)
    prepare.set_defaults(handler=_prepare)

    status = sub.add_parser("status", help="observe exact external/client cutover state")
    status.add_argument("--authority-root", required=True)
    status.add_argument("--runtime-root", required=True)
    status.add_argument("--cutover-id", required=True)
    status.set_defaults(handler=_status)

    apply = sub.add_parser("apply", help="perform the one M11c production cutover effect")
    apply.add_argument("--authority-root", required=True)
    apply.add_argument("--cutover-id", required=True)
    apply.add_argument("--approve-plan-sha256", required=True, help="exact sha256 from the previously reviewed immutable cutover plan")
    apply.set_defaults(handler=_apply)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        result = args.handler(args)
        _emit(result)
        return 0
    except (M11cCutoverError, M11cClientError, M11cArtifactError, M9bActivationError) as exc:
        _emit({"schema": CLI_SCHEMA, "status": "BLOCKED", "error_code": getattr(exc, "code", "cutover_failed"), "error": str(exc), "production_activation_performed": False})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["CLI_SCHEMA", "main"]

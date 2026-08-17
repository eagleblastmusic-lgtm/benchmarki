"""One-shot, non-activating final preparation for BDB Next.

This module intentionally stops immediately before the M11c production effect.
It composes already-authoritative primitives instead of creating another
lifecycle or activation authority:

1. prove the staged Browser/Native client subject is VERIFIED;
2. prove External Bootstrap is still PREPARED/OFF and no M9b activation record
   exists;
3. capture and revalidate revised side-by-side M9a handoff evidence;
4. create/reuse the immutable M11a coordinated backup/preparation; and
5. create/reuse the immutable M11c cutover plan.

The source identity is derived from the verified client plan.  It is never
copied from the operator's current Git checkout because the reviewed staged
candidate may intentionally lag later operator-only tooling commits.

No function in this module can activate BDB Next, enable M3c intake, disable a
Legacy product, or mutate the M11c ACTIVE pointer.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NoReturn

from bdb_shared.evidence import canonical_json_bytes
from bdb_vnext.bootstrap import BootstrapError, _absolute_path
from bdb_vnext.m11a_prepared_activation import (
    M11aPreparationError,
    prepare_candidate_activation,
    query_prepared_activation,
)
from bdb_vnext.m11c_cutover import (
    M11cCutoverError,
    observe_bootstrap_activation,
    prepare_windows_cutover_plan,
    query_cutover_plan,
)
from bdb_vnext.m11c_windows_clients import (
    M11cClientError,
    query_client_plan,
    require_client_verification,
)
from bdb_vnext.m9a_handoff import (
    M9aHandoffError,
    capture_side_by_side_handoff,
    revalidate_side_by_side_digest,
    verify_side_by_side_report,
)
from bdb_vnext.m9b_activation import M9bActivationError, read_activation


FINAL_PREP_SCHEMA = "bdb-vnext-m11c-final-preparation-v1"


class M11cFinalPreparationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> NoReturn:
    raise M11cFinalPreparationError(code, message)


def _client_identity(runtime: Path) -> tuple[dict[str, Any], str, str]:
    try:
        plan = query_client_plan(runtime_root=runtime)["plan"]
        require_client_verification(
            runtime_root=runtime,
            expected_client_plan_sha256=plan["client_plan_sha256"],
        )
    except (M11cClientError, BootstrapError, KeyError, TypeError) as exc:
        code = getattr(exc, "code", "client_verification_required")
        raise M11cFinalPreparationError(str(code), "final preparation requires exact VERIFIED clients") from exc
    source_head = plan.get("source_head")
    source_tree = plan.get("source_tree")
    client_sha = plan.get("client_plan_sha256")
    if not isinstance(source_head, str) or len(source_head) != 40:
        _fail("client_source_invalid", "verified client plan has no exact source HEAD")
    if not isinstance(source_tree, str) or len(source_tree) != 40:
        _fail("client_source_invalid", "verified client plan has no exact source tree")
    if not isinstance(client_sha, str) or not client_sha.startswith("sha256:") or len(client_sha) != 71:
        _fail("client_plan_invalid", "verified client plan has no exact semantic digest")
    return plan, source_head, source_tree


def _ids(source_head: str, client_plan_sha256: str) -> tuple[str, str]:
    suffix = f"{source_head[:12]}-{client_plan_sha256[7:19]}"
    return f"final-prep-{suffix}", f"final-cutover-{suffix}"


def _assert_pre_activation(authority: Path, runtime: Path) -> dict[str, Any]:
    try:
        observed = observe_bootstrap_activation(authority_root=authority)
    except (M11cCutoverError, BootstrapError) as exc:
        code = getattr(exc, "code", "bootstrap_observation_failed")
        raise M11cFinalPreparationError(str(code), "external Bootstrap state cannot be verified") from exc
    if observed.get("status") != "PREPARED" or observed.get("production_activation_performed") is not False:
        _fail("preactivation_state_required", "final preparation requires External Bootstrap PREPARED/OFF")
    try:
        gate = read_activation(runtime)
    except M9bActivationError as exc:
        raise M11cFinalPreparationError(exc.code, str(exc)) from exc
    if gate is not None:
        _fail("client_gate_already_present", "final preparation requires the M9b activation record to remain absent")
    return observed


def _existing_preparation(authority: Path, preparation_id: str) -> dict[str, Any] | None:
    try:
        return query_prepared_activation(authority_root=authority, preparation_id=preparation_id)
    except (BootstrapError, M11aPreparationError) as exc:
        if getattr(exc, "code", None) == "missing_file":
            return None
        raise


def _existing_plan(authority: Path, cutover_id: str) -> dict[str, Any] | None:
    try:
        return query_cutover_plan(authority_root=authority, cutover_id=cutover_id)
    except (BootstrapError, M11cCutoverError) as exc:
        if getattr(exc, "code", None) == "missing_file":
            return None
        raise


def _final_result(
    *,
    authority: Path,
    runtime: Path,
    legacy: Path,
    preparation_id: str,
    cutover_id: str,
    report: Mapping[str, Any] | None,
    freeze_digest: str,
    recovery_target: Path,
) -> dict[str, Any]:
    prepared = query_prepared_activation(authority_root=authority, preparation_id=preparation_id)
    cutover = query_cutover_plan(authority_root=authority, cutover_id=cutover_id)
    plan = cutover["plan"]
    client = query_client_plan(runtime_root=runtime)["plan"]
    require_client_verification(
        runtime_root=runtime,
        expected_client_plan_sha256=client["client_plan_sha256"],
    )
    revalidate_side_by_side_digest(
        runtime_root=runtime,
        legacy_runtime_root=legacy,
        freeze_digest=freeze_digest,
    )
    bootstrap = observe_bootstrap_activation(authority_root=authority)
    if bootstrap.get("status") != "PREPARED" or bootstrap.get("production_activation_performed") is not False:
        _fail("preparation_changed_activation", "final preparation unexpectedly changed production activation")
    if plan.get("source_head") != client.get("source_head") or plan.get("source_tree") != client.get("source_tree"):
        _fail("final_plan_source_mismatch", "final cutover plan differs from the verified staged source")
    if plan.get("client_plan_sha256") != client.get("client_plan_sha256"):
        _fail("final_plan_client_mismatch", "final cutover plan differs from the verified client plan")
    return {
        "schema": FINAL_PREP_SCHEMA,
        "status": "PREPARED_NOT_ACTIVATED",
        "authority_root": str(authority),
        "runtime_root": str(runtime),
        "legacy_runtime_root": str(legacy),
        "recovery_target": str(recovery_target),
        "source_head": plan["source_head"],
        "source_tree": plan["source_tree"],
        "client_plan_sha256": plan["client_plan_sha256"],
        "m9a_freeze_digest": freeze_digest,
        "m9a_report": dict(report) if report is not None else None,
        "preparation_id": preparation_id,
        "preparation_sha256": prepared["prepared"]["preparation_sha256"],
        "backup": dict(prepared["prepared"]["backup"]),
        "cutover_id": cutover_id,
        "cutover_plan_sha256": plan["cutover_plan_sha256"],
        "operator_approval_required": True,
        "next_effect_boundary": "bdb-vnext-cutover apply --approve-plan-sha256 <exact cutover_plan_sha256>",
        "production_activation_performed": False,
        "legacy_product_globally_disabled": False,
    }


def prepare_final_cutover(
    *,
    authority_root: str | Path,
    runtime_root: str | Path,
    legacy_runtime_root: str | Path,
    recovery_target: str | Path | None = None,
    observation_seconds: float = 2.0,
) -> dict[str, Any]:
    """Create all final evidence and the immutable cutover plan, but never apply it."""

    if os.name != "nt":
        _fail("windows_required", "final production preparation requires Windows")
    authority = _absolute_path(authority_root, field="authority_root")
    runtime = _absolute_path(runtime_root, field="runtime_root")
    legacy = _absolute_path(legacy_runtime_root, field="legacy_runtime_root")
    client, source_head, source_tree = _client_identity(runtime)
    preparation_id, cutover_id = _ids(source_head, client["client_plan_sha256"])
    recovery = (
        _absolute_path(recovery_target, field="recovery_target")
        if recovery_target is not None
        else runtime.parent / "recovery" / preparation_id
    )
    _assert_pre_activation(authority, runtime)

    existing_plan = _existing_plan(authority, cutover_id)
    if existing_plan is not None:
        plan = existing_plan["plan"]
        freeze_digest = plan["m9a_freeze_digest"]
        return _final_result(
            authority=authority,
            runtime=runtime,
            legacy=legacy,
            preparation_id=preparation_id,
            cutover_id=cutover_id,
            report=None,
            freeze_digest=freeze_digest,
            recovery_target=recovery,
        )

    try:
        captured = capture_side_by_side_handoff(
            runtime_root=runtime,
            legacy_runtime_root=legacy,
            observation_seconds=observation_seconds,
        )
        report = captured["report"]
        if captured.get("status") != "PASS_CLOSED" or not isinstance(report, Mapping):
            _fail("m9a_not_pass_closed", "M9a side-by-side handoff did not PASS_CLOSED")
        freeze_digest = verify_side_by_side_report(runtime_root=runtime, report=report)
        revalidate_side_by_side_digest(
            runtime_root=runtime,
            legacy_runtime_root=legacy,
            freeze_digest=freeze_digest,
        )
    except M9aHandoffError as exc:
        raise M11cFinalPreparationError(exc.code, str(exc)) from exc

    prepared = _existing_preparation(authority, preparation_id)
    if prepared is None:
        try:
            prepared = prepare_candidate_activation(
                authority_root=authority,
                runtime_root=runtime,
                recovery_target=recovery,
                preparation_id=preparation_id,
                # Bootstrap and M9b gates above prove that no supported
                # production writer can reach this pre-activation runtime.
                source_is_quiesced=True,
                include_control_identity=False,
            )
        except (BootstrapError, M11aPreparationError) as exc:
            code = getattr(exc, "code", "m11a_preparation_failed")
            raise M11cFinalPreparationError(str(code), str(exc)) from exc

    if prepared["prepared"]["slot_binding"]["candidate_manifest_sha256"] != prepared["slots"]["state"]["candidate_manifest_sha256"]:
        _fail("prepared_candidate_mismatch", "M11a preparation does not bind the current candidate")

    try:
        prepare_windows_cutover_plan(
            authority_root=authority,
            runtime_root=runtime,
            legacy_runtime_root=legacy,
            preparation_id=preparation_id,
            cutover_id=cutover_id,
            source_head=source_head,
            source_tree=source_tree,
            m9a_report=report,
            browser_bundle_digest=client["browser_bundle_digest"],
            native_manifest_digest=client["native_manifest_sha256"],
        )
    except (M11cCutoverError, BootstrapError) as exc:
        code = getattr(exc, "code", "m11c_plan_failed")
        raise M11cFinalPreparationError(str(code), str(exc)) from exc

    return _final_result(
        authority=authority,
        runtime=runtime,
        legacy=legacy,
        preparation_id=preparation_id,
        cutover_id=cutover_id,
        report=report,
        freeze_digest=freeze_digest,
        recovery_target=recovery,
    )


def query_final_preparation(
    *,
    authority_root: str | Path,
    runtime_root: str | Path,
    legacy_runtime_root: str | Path,
) -> dict[str, Any]:
    """Revalidate an already-created final preparation without creating evidence."""

    authority = _absolute_path(authority_root, field="authority_root")
    runtime = _absolute_path(runtime_root, field="runtime_root")
    legacy = _absolute_path(legacy_runtime_root, field="legacy_runtime_root")
    client, source_head, _source_tree = _client_identity(runtime)
    preparation_id, cutover_id = _ids(source_head, client["client_plan_sha256"])
    plan_query = query_cutover_plan(authority_root=authority, cutover_id=cutover_id)
    recovery = runtime.parent / "recovery" / preparation_id
    return _final_result(
        authority=authority,
        runtime=runtime,
        legacy=legacy,
        preparation_id=preparation_id,
        cutover_id=cutover_id,
        report=None,
        freeze_digest=plan_query["plan"]["m9a_freeze_digest"],
        recovery_target=recovery,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bdb-vnext-final-prepare",
        description="Prepare the exact final BDB Next cutover plan without activation.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare", help="capture M9a + create M11a backup/preparation + immutable M11c plan")
    prepare.add_argument("--authority-root", required=True)
    prepare.add_argument("--runtime-root", required=True)
    prepare.add_argument("--legacy-runtime-root", required=True)
    prepare.add_argument("--recovery-target")
    prepare.add_argument("--observation-seconds", type=float, default=2.0)
    status = sub.add_parser("status", help="revalidate the existing final preparation")
    status.add_argument("--authority-root", required=True)
    status.add_argument("--runtime-root", required=True)
    status.add_argument("--legacy-runtime-root", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "prepare":
            result = prepare_final_cutover(
                authority_root=args.authority_root,
                runtime_root=args.runtime_root,
                legacy_runtime_root=args.legacy_runtime_root,
                recovery_target=args.recovery_target,
                observation_seconds=args.observation_seconds,
            )
        else:
            result = query_final_preparation(
                authority_root=args.authority_root,
                runtime_root=args.runtime_root,
                legacy_runtime_root=args.legacy_runtime_root,
            )
        exit_code = 0
    except (
        M11cFinalPreparationError,
        M11cCutoverError,
        M11cClientError,
        M11aPreparationError,
        M9aHandoffError,
        M9bActivationError,
        BootstrapError,
        OSError,
        ValueError,
    ) as exc:
        result = {
            "schema": FINAL_PREP_SCHEMA,
            "status": "BLOCKED",
            "error_code": getattr(exc, "code", "final_preparation_failed"),
            "error": str(exc),
            "production_activation_performed": False,
            "legacy_product_globally_disabled": False,
        }
        exit_code = 2
    print(canonical_json_bytes(result).decode("utf-8"))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "FINAL_PREP_SCHEMA",
    "M11cFinalPreparationError",
    "prepare_final_cutover",
    "query_final_preparation",
    "main",
]

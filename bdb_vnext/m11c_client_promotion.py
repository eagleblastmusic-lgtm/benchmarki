"""Rollback-safe migration/recovery of an immutable historical client stage.

``stage_client_plan`` deliberately refuses to overwrite a path-bound stage.
This migration-only module is the separate, explicit production-boundary operation: it
validates one immutable stage, builds a production-path-bound document set,
and swaps the coherent client set under a recoverable transaction.

The operation never changes Bootstrap, writer/intake, Project Memory, or the
Native Messaging registry.  The stable production manifest path is preserved,
so registry readback is a safety check rather than an activation operation.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, NoReturn

from bdb_shared.evidence import canonical_json_bytes
from bdb_vnext.bootstrap import _absolute_path, _load_json
from bdb_vnext.m11c_windows_clients import (
    CLIENT_PLAN_SCHEMA,
    M11cClientError,
    _atomic_json,
    _client_plan_document,
    _copy_browser_bundle,
    _copy_native_executable,
    _copy_native_payload,
    _digest,
    _digest_bytes,
    _native_config,
    _native_manifest,
    inspect_browser_bundle,
    observe_windows_native_routes,
    query_client_plan,
)
from bdb_vnext.project_launch import PROJECT_LAUNCH_QUEUE_SCHEMA


PROMOTION_SCHEMA = "bdb-vnext-m11c-client-promotion-v1"
NATIVE_RUNTIME_RESIDUE_RECOVERY_SCHEMA = "bdb-vnext-native-runtime-residue-recovery-v1"
PROMOTION_STATES = frozenset(
    {
        "PREPARED",
        "LIVE_BACKED_UP",
        "NEW_CLIENTS_INSTALLED",
        "VERIFIED",
        "COMMITTED",
        "ROLLED_BACK",
        "RECOVERY_REQUIRED",
    }
)


def _native_payload_for_root(plan: Mapping[str, Any], root: Path) -> dict[str, Any] | None:
    if "native_artifact_manifest_path" not in plan:
        return None
    return {
        "native_artifact_kind": plan["native_artifact_kind"],
        "native_payload_sha256": plan["native_payload_sha256"],
        "native_payload_size_bytes": plan["native_payload_size_bytes"],
        "native_artifact_manifest_path": str(root / "clients" / "native-host" / Path(plan["native_artifact_manifest_path"]).name),
        "native_artifact_manifest_sha256": plan["native_artifact_manifest_sha256"],
    }


_MUTATING_STATES = frozenset({"LIVE_BACKED_UP", "NEW_CLIENTS_INSTALLED", "VERIFIED"})
FaultInjector = Callable[[str], None]


def _fail(code: str, message: str) -> NoReturn:
    raise M11cClientError(code, message)


def _contained(root: Path, child: Path) -> bool:
    try:
        child.relative_to(root)
    except ValueError:
        return False
    return child != root


def _regular_file(path: Path, *, field: str) -> Path:
    if path.is_symlink() or not path.is_file():
        _fail("promotion_path_invalid", f"{field} must be a regular file")
    return path


def _regular_dir(path: Path, *, field: str) -> Path:
    if path.is_symlink() or not path.is_dir():
        _fail("promotion_path_invalid", f"{field} must be a regular directory")
    return path


def _move_exact(source: Path, target: Path, *, field: str) -> None:
    if not source.exists() or source.is_symlink():
        _fail("promotion_move_failed", f"{field} source is missing or symbolic")
    if target.exists() or target.is_symlink():
        _fail("promotion_move_failed", f"{field} target already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(source, target)
    except OSError as exc:
        raise M11cClientError("promotion_move_failed", f"{field} move failed") from exc


def _transaction_root(production: Path) -> Path:
    return production / "recovery" / "client-promotions"


def _transaction_id(stage_plan_sha256: str) -> str:
    return f"promotion-{stage_plan_sha256.split(':', 1)[1]}"


def _transaction_path(production: Path, transaction_id: str) -> Path:
    return _transaction_root(production) / transaction_id


def _native_residue_recovery_root(production: Path) -> Path:
    return _transaction_root(production) / "native-runtime-residue"


def _native_runtime_residue(production: Path) -> Path:
    return production / "clients" / "native-host" / "_internal" / "runtime"


def _load_native_residue_queue(root: Path) -> tuple[Path, bytes]:
    if root.is_symlink() or not root.is_dir():
        _fail("promotion_native_residue_unsupported", "Native runtime residue is not a regular directory")
    entries = sorted(path for path in root.rglob("*") if path.is_file() or path.is_symlink())
    queue = root / "control" / "project-launch-queue.json"
    if entries != [queue] or queue.is_symlink() or not queue.is_file():
        _fail("promotion_native_residue_unsupported", "Native runtime residue contains unsupported state")
    raw = queue.read_bytes()
    document = _load_json(queue, field="native runtime residue queue")
    if set(document) != {"schema", "pending", "claim"} or document.get("schema") != PROJECT_LAUNCH_QUEUE_SCHEMA:
        _fail("promotion_native_residue_unsupported", "Native runtime residue queue identity differs")
    return queue, raw


def _native_residue_subject(production: Path, residue: Path, raw: bytes) -> dict[str, Any]:
    plan = _load_json(production / "clients" / "client-plan.json", field="production client plan")
    plan_sha = plan.get("client_plan_sha256")
    if not isinstance(plan_sha, str) or _digest({key: value for key, value in plan.items() if key != "client_plan_sha256"}) != plan_sha:
        _fail("promotion_native_residue_subject_invalid", "production client plan identity differs")
    payload = {
        "schema": NATIVE_RUNTIME_RESIDUE_RECOVERY_SCHEMA,
        "production_root": str(production),
        "source_head": plan.get("source_head"),
        "source_tree": plan.get("source_tree"),
        "client_plan_sha256": plan_sha,
        "residue_path": str(residue),
        "queue_sha256": _digest_bytes(raw),
    }
    return {**payload, "subject_sha256": _digest(payload)}


def _load_native_residue_subject(path: Path, production: Path) -> dict[str, Any]:
    subject = _load_json(path, field="native runtime residue recovery subject")
    supplied = subject.get("subject_sha256")
    payload = dict(subject)
    payload.pop("subject_sha256", None)
    if (
        subject.get("schema") != NATIVE_RUNTIME_RESIDUE_RECOVERY_SCHEMA
        or subject.get("production_root") != str(production)
        or not isinstance(supplied, str)
        or _digest(payload) != supplied
    ):
        _fail("promotion_native_residue_recovery_corrupt", "Native runtime residue recovery subject differs")
    return subject


def _complete_native_residue_recovery(production: Path, transaction: Path, subject: Mapping[str, Any]) -> None:
    source = _absolute_path(subject["residue_path"], field="native residue source")
    expected_source = _native_runtime_residue(production)
    archive = transaction / "preserved-runtime"
    if source != expected_source:
        _fail("promotion_native_residue_recovery_corrupt", "Native runtime residue source differs")
    if source.exists() and not archive.exists():
        _queue, raw = _load_native_residue_queue(source)
        if _digest_bytes(raw) != subject.get("queue_sha256"):
            _fail("promotion_native_residue_recovery_corrupt", "Native runtime residue changed before preservation")
        _move_exact(source, archive, field="preserve Native runtime residue")
    elif source.exists() or not archive.is_dir() or archive.is_symlink():
        _fail("promotion_native_residue_recovery_corrupt", "Native runtime residue recovery paths conflict")
    _queue, archived = _load_native_residue_queue(archive)
    if _digest_bytes(archived) != subject.get("queue_sha256"):
        _fail("promotion_native_residue_recovery_corrupt", "preserved Native runtime residue differs")
    query_client_plan(runtime_root=production)
    completion_path = transaction / "completed.json"
    completion_payload = {
        "schema": NATIVE_RUNTIME_RESIDUE_RECOVERY_SCHEMA,
        "subject_sha256": subject["subject_sha256"],
        "queue_sha256": subject["queue_sha256"],
        "status": "PRESERVED_NOT_AUTHORITY",
    }
    completion = {**completion_payload, "completion_sha256": _digest(completion_payload)}
    if completion_path.exists():
        if _load_json(completion_path, field="native runtime residue recovery completion") != completion:
            _fail("promotion_native_residue_recovery_corrupt", "Native runtime residue completion differs")
    else:
        _atomic_json(completion_path, completion, immutable=True)


def _recover_native_runtime_residue(production: Path) -> None:
    recovery_root = _native_residue_recovery_root(production)
    if recovery_root.exists():
        if recovery_root.is_symlink() or not recovery_root.is_dir():
            _fail("promotion_native_residue_recovery_corrupt", "Native runtime residue recovery root differs")
        transactions = sorted(item for item in recovery_root.iterdir() if item.is_dir() and not item.is_symlink())
        if len(transactions) > 8:
            _fail("promotion_native_residue_recovery_corrupt", "too many Native runtime residue recoveries")
        for transaction in transactions:
            subject = _load_native_residue_subject(transaction / "subject.json", production)
            _complete_native_residue_recovery(production, transaction, subject)

    residue = _native_runtime_residue(production)
    if not residue.exists():
        return
    _queue, raw = _load_native_residue_queue(residue)
    subject = _native_residue_subject(production, residue, raw)
    transaction = recovery_root / subject["subject_sha256"].split(":", 1)[1]
    if transaction.exists():
        _fail("promotion_native_residue_recovery_corrupt", "Native runtime residue transaction already exists")
    transaction.mkdir(parents=True)
    _atomic_json(transaction / "subject.json", subject, immutable=True)
    _complete_native_residue_recovery(production, transaction, subject)


def _state_path(transaction: Path) -> Path:
    return transaction / "transaction.json"


def _write_state(transaction: Path, state: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(state)
    payload.pop("state_sha256", None)
    document = {**payload, "state_sha256": _digest(payload)}
    try:
        _atomic_json(_state_path(transaction), document)
    except M11cClientError as exc:
        if exc.code == "client_evidence_write_failed":
            raise M11cClientError("promotion_transaction_write_failed", str(exc)) from exc
        raise
    return document


def _load_state(transaction: Path) -> dict[str, Any]:
    path = _state_path(transaction)
    if not path.is_file() or path.is_symlink():
        _fail("promotion_transaction_corrupt", "promotion transaction state is missing")
    document = _load_json(path, field="promotion_transaction")
    if document.get("schema") != PROMOTION_SCHEMA:
        _fail("promotion_transaction_corrupt", "promotion transaction schema differs")
    supplied = document.get("state_sha256")
    if not isinstance(supplied, str):
        _fail("promotion_transaction_corrupt", "promotion transaction digest is missing")
    payload = dict(document)
    payload.pop("state_sha256", None)
    if _digest(payload) != supplied:
        _fail("promotion_transaction_corrupt", "promotion transaction digest differs")
    state = document.get("state")
    if state not in PROMOTION_STATES:
        _fail("promotion_transaction_corrupt", "promotion transaction state is invalid")
    return document


def _advance(transaction: Path, state: Mapping[str, Any], new_state: str) -> dict[str, Any]:
    if new_state not in PROMOTION_STATES:
        _fail("promotion_transaction_corrupt", "promotion transition state is invalid")
    payload = dict(state)
    history = list(payload.get("state_history", []))
    if not history or history[-1] != new_state:
        history.append(new_state)
    payload["state"] = new_state
    payload["state_history"] = history
    return _write_state(transaction, payload)


def _fault(fault_injector: FaultInjector | None, point: str) -> None:
    if fault_injector is not None:
        fault_injector(point)


def _stage_subject(stage_root: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    stage = _regular_dir(stage_root, field="staged_runtime_root")
    observed = query_client_plan(runtime_root=stage)
    plan = observed["plan"]
    path_fields = {
        "browser_bundle_root": Path(plan["browser_bundle_root"]),
        "native_host_executable": Path(plan["native_host_executable"]),
        "native_config_path": Path(plan["native_config_path"]),
        "native_manifest_path": Path(plan["native_manifest_path"]),
        "client_plan_path": stage / "clients" / "client-plan.json",
    }
    for field, path in path_fields.items():
        if not _contained(stage, path):
            _fail("promotion_stage_path_escape", f"stage {field} escapes staged runtime root")

    config_path = _regular_file(path_fields["native_config_path"], field="stage native config")
    config = _load_json(config_path, field="stage native config")
    required = {
        "schema",
        "generation_id",
        "protocol_generation",
        "native_host_name",
        "browser_extension_id",
        "runtime_root",
        "legacy_runtime_root",
        "bootstrap_authority_root",
    }
    if set(config) != required:
        _fail("promotion_stage_config_invalid", "stage Native config fields differ")
    expected_config = _native_config(
        runtime=stage,
        legacy=_absolute_path(config["legacy_runtime_root"], field="legacy_runtime_root"),
        bootstrap=_absolute_path(config["bootstrap_authority_root"], field="bootstrap_authority_root"),
    )
    if config != expected_config:
        _fail("promotion_stage_config_invalid", "stage Native config identity differs")
    return plan, observed["browser"], config


def _production_documents(
    *,
    production: Path,
    plan: Mapping[str, Any],
    browser_digest: str,
    legacy: Path,
    bootstrap: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    browser_root = production / "clients" / "browser-extension"
    executable = production / "clients" / "native-host" / Path(plan["native_host_executable"]).name
    config_path = production / "config" / "native-host.json"
    manifest_path = production / "clients" / "native-host" / Path(plan["native_manifest_path"]).name
    executable_digest = plan["native_host_executable_sha256"]
    config = _native_config(runtime=production, legacy=legacy, bootstrap=bootstrap)
    manifest = _native_manifest(executable)
    production_plan = _client_plan_document(
        runtime=production,
        source_head=plan["source_head"],
        source_tree=plan["source_tree"],
        browser_bundle_root=browser_root,
        browser_bundle_digest=browser_digest,
        executable=executable,
        executable_digest=executable_digest,
        config_path=config_path,
        config=config,
        manifest_path=manifest_path,
        native_payload=_native_payload_for_root(plan, production),
    )
    if production_plan["native_manifest_sha256"] != _digest_bytes(canonical_json_bytes(manifest)):
        _fail("promotion_document_mismatch", "production Native manifest digest differs")
    return production_plan, config, manifest


def _production_matches(
    production: Path,
    *,
    expected_plan: Mapping[str, Any],
    expected_config: Mapping[str, Any],
    expected_manifest: Mapping[str, Any],
) -> bool:
    try:
        observed = query_client_plan(runtime_root=production)
        if observed["plan"] != dict(expected_plan):
            return False
        config = _load_json(Path(expected_plan["native_config_path"]), field="production native config")
        manifest = _load_json(Path(expected_plan["native_manifest_path"]), field="production native manifest")
        return config == dict(expected_config) and manifest == dict(expected_manifest)
    except (M11cClientError, FileNotFoundError, OSError, ValueError, KeyError):
        return False


def _route_is_coherent(production: Path) -> dict[str, Any]:
    routes = observe_windows_native_routes(runtime_root=production)
    if routes.get("target_conflict") or routes.get("target_registered") is not True:
        _fail("promotion_registry_mismatch", "HKCU vNext Native routes are not exact and stable")
    if routes.get("legacy_route_present"):
        _fail("promotion_legacy_route_present", "Legacy Native route is present")
    return routes


def _build_candidate(
    *,
    transaction: Path,
    staged_root: Path,
    production: Path,
    stage_plan: Mapping[str, Any],
    browser: Mapping[str, Any],
    legacy: Path,
    bootstrap: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    candidate = transaction / "candidate"
    candidate_browser = candidate / "clients" / "browser-extension"
    candidate_executable = candidate / "clients" / "native-host" / Path(stage_plan["native_host_executable"]).name
    candidate_config_path = candidate / "config" / "native-host.json"
    candidate_manifest_path = candidate / "clients" / "native-host" / Path(stage_plan["native_manifest_path"]).name
    stage_browser = Path(stage_plan["browser_bundle_root"])
    stage_executable = Path(stage_plan["native_host_executable"])
    if candidate.exists():
        _fail("promotion_transaction_corrupt", "promotion candidate already exists before preparation")
    candidate.mkdir(parents=True)
    _copy_browser_bundle(stage_browser, candidate_browser, browser["bundle_digest"])
    native_payload = _copy_native_payload(
        stage_executable,
        candidate_executable.parent,
        source_head=stage_plan["source_head"],
        source_tree=stage_plan["source_tree"],
    )
    if native_payload is None:
        _copy_native_executable(stage_executable, candidate_executable, stage_plan["native_host_executable_sha256"])

    # First validate a complete candidate with candidate-bound documents.
    candidate_config = _native_config(runtime=candidate, legacy=legacy, bootstrap=bootstrap)
    candidate_manifest = _native_manifest(candidate_executable)
    _atomic_json(candidate_config_path, candidate_config)
    _atomic_json(candidate_manifest_path, candidate_manifest)
    candidate_plan = _client_plan_document(
        runtime=candidate,
        source_head=stage_plan["source_head"],
        source_tree=stage_plan["source_tree"],
        browser_bundle_root=candidate_browser,
        browser_bundle_digest=browser["bundle_digest"],
        executable=candidate_executable,
        executable_digest=stage_plan["native_host_executable_sha256"],
        config_path=candidate_config_path,
        config=candidate_config,
        manifest_path=candidate_manifest_path,
        native_payload=_native_payload_for_root(stage_plan, candidate),
    )
    _atomic_json(candidate / "clients" / "client-plan.json", candidate_plan)
    query_client_plan(runtime_root=candidate)

    # Replace only the path-bound documents with production-bound versions.
    production_plan, production_config, production_manifest = _production_documents(
        production=production,
        plan=stage_plan,
        browser_digest=browser["bundle_digest"],
        legacy=legacy,
        bootstrap=bootstrap,
    )
    _atomic_json(candidate_config_path, production_config)
    _atomic_json(candidate_manifest_path, production_manifest)
    _atomic_json(candidate / "clients" / "client-plan.json", production_plan)
    return production_plan, production_config, production_manifest


def _candidate_documents_match(transaction: Path, state: Mapping[str, Any]) -> bool:
    candidate = transaction / "candidate"
    try:
        browser = inspect_browser_bundle(candidate / "clients" / "browser-extension")
        plan = _load_json(candidate / "clients" / "client-plan.json", field="promotion candidate plan")
        config = _load_json(candidate / "config" / "native-host.json", field="promotion candidate config")
        manifest = _load_json(candidate / "clients" / "native-host" / Path(state["native_manifest_name"]), field="promotion candidate manifest")
        executable = candidate / "clients" / "native-host" / Path(state["native_executable_name"])
        return (
            browser["bundle_digest"] == state["expected_plan"]["browser_bundle_digest"]
            and executable.is_file()
            and _digest_bytes(executable.read_bytes()) == state["expected_plan"]["native_host_executable_sha256"]
            and plan == state["expected_plan"]
            and config == state["expected_config"]
            and manifest == state["expected_manifest"]
        )
    except (M11cClientError, OSError, KeyError, TypeError):
        return False


def _rollback(transaction: Path, state: Mapping[str, Any], *, reason: str) -> dict[str, Any]:
    production = Path(state["production_root"])
    previous = transaction / "previous"
    failed = transaction / "failed"
    failed.mkdir(parents=True, exist_ok=True)
    try:
        live_clients = production / "clients"
        live_config = production / "config" / "native-host.json"
        if live_clients.exists():
            _move_exact(live_clients, failed / "clients", field="failed live clients")
        if live_config.exists():
            _move_exact(live_config, failed / "config" / "native-host.json", field="failed live config")
        _move_exact(previous / "clients", live_clients, field="restore previous clients")
        _move_exact(previous / "config" / "native-host.json", live_config, field="restore previous config")
        query_client_plan(runtime_root=production)
        _route_is_coherent(production)
    except Exception as exc:
        recovery = dict(state)
        recovery["rollback_error"] = str(exc)
        recovery["rollback_reason"] = reason
        _advance(transaction, recovery, "RECOVERY_REQUIRED")
        raise M11cClientError("promotion_recovery_required", "production rollback could not be verified") from exc
    rolled = dict(state)
    rolled["rollback_reason"] = reason
    rolled["failed_root"] = str(failed)
    return _advance(transaction, rolled, "ROLLED_BACK")


def _ensure_backup(
    transaction: Path,
    state: Mapping[str, Any],
    *,
    fault_injector: FaultInjector | None,
) -> dict[str, Any]:
    production = Path(state["production_root"])
    previous = transaction / "previous"
    previous_clients = previous / "clients"
    previous_config = previous / "config" / "native-host.json"
    live_clients = production / "clients"
    live_config = production / "config" / "native-host.json"
    previous.mkdir(parents=True, exist_ok=True)
    _fault(fault_injector, "before_backup")
    if not previous_clients.exists():
        _move_exact(live_clients, previous_clients, field="backup live clients")
    elif live_clients.exists():
        _fail("promotion_ambiguous", "live and previous client directories both exist")
    if not previous_config.exists():
        _move_exact(live_config, previous_config, field="backup live config")
    elif live_config.exists():
        _fail("promotion_ambiguous", "live and previous config files both exist")
    _fault(fault_injector, "after_backup")
    return _advance(transaction, state, "LIVE_BACKED_UP")


def _ensure_install(
    transaction: Path,
    state: Mapping[str, Any],
    *,
    fault_injector: FaultInjector | None,
) -> dict[str, Any]:
    production = Path(state["production_root"])
    candidate = transaction / "candidate"
    live_clients = production / "clients"
    live_config = production / "config" / "native-host.json"
    candidate_clients = candidate / "clients"
    candidate_config = candidate / "config" / "native-host.json"
    if not live_clients.exists():
        _fault(fault_injector, "before_install_clients")
        _move_exact(candidate_clients, live_clients, field="install production clients")
        _fault(fault_injector, "after_install_clients")
    elif candidate_clients.exists():
        _fail("promotion_ambiguous", "candidate and live client directories both exist")
    if not live_config.exists():
        _fault(fault_injector, "before_install_config")
        _move_exact(candidate_config, live_config, field="install production config")
        _fault(fault_injector, "after_install_config")
    elif candidate_config.exists():
        _fail("promotion_ambiguous", "candidate and live config files both exist")
    return _advance(transaction, state, "NEW_CLIENTS_INSTALLED")


def _verify_and_commit(transaction: Path, state: Mapping[str, Any], *, fault_injector: FaultInjector | None) -> dict[str, Any]:
    production = Path(state["production_root"])
    _fault(fault_injector, "before_verify")
    if not _production_matches(
        production,
        expected_plan=state["expected_plan"],
        expected_config=state["expected_config"],
        expected_manifest=state["expected_manifest"],
    ):
        _fail("promotion_readback_mismatch", "production-bound client readback differs")
    routes = _route_is_coherent(production)
    _fault(fault_injector, "after_verify")
    verified = dict(state)
    verified["routes"] = routes
    verified = _advance(transaction, verified, "VERIFIED")
    committed = dict(verified)
    committed["production_plan_sha256"] = state["expected_plan"]["client_plan_sha256"]
    return _advance(transaction, committed, "COMMITTED")


def _resume_transaction(transaction: Path, state: dict[str, Any], *, fault_injector: FaultInjector | None) -> dict[str, Any]:
    current = state
    try:
        if current["state"] == "COMMITTED":
            if not _production_matches(
                Path(current["production_root"]),
                expected_plan=current["expected_plan"],
                expected_config=current["expected_config"],
                expected_manifest=current["expected_manifest"],
            ):
                _fail("promotion_commit_mismatch", "committed promotion no longer matches production")
            _route_is_coherent(Path(current["production_root"]))
            return {**current, "status": "IDEMPOTENT_COMMITTED"}
        if current["state"] in {"ROLLED_BACK", "RECOVERY_REQUIRED"}:
            _fail("promotion_recovery_required", f"promotion transaction is {current['state']}")
        # PREPARED still owns the complete candidate tree.  Once installation
        # has started, one or both candidate subtrees may already have moved
        # into production; the final production readback is then the source
        # of truth for replay.  Requiring the now-missing candidate directory
        # here would turn a recoverable mid-install crash into a false
        # candidate-mismatch failure.
        if current["state"] == "PREPARED" and not _candidate_documents_match(transaction, current):
            _fail("promotion_candidate_mismatch", "promotion candidate documents or bytes differ")
        if current["state"] == "PREPARED":
            current = _ensure_backup(transaction, current, fault_injector=fault_injector)
        if current["state"] == "LIVE_BACKED_UP":
            current = _ensure_install(transaction, current, fault_injector=fault_injector)
        if current["state"] in {"NEW_CLIENTS_INSTALLED", "VERIFIED"}:
            current = _verify_and_commit(transaction, current, fault_injector=fault_injector)
        if current["state"] != "COMMITTED":
            _fail("promotion_transaction_corrupt", "promotion did not reach COMMITTED")
        return {**current, "status": "COMMITTED"}
    except Exception as exc:
        # A failure before moving the old set leaves a recoverable PREPARED
        # transaction.  Once any physical move occurred, restore the old set.
        previous = transaction / "previous"
        mutated = current["state"] in _MUTATING_STATES or previous.exists() and any(previous.rglob("*"))
        if mutated and current["state"] not in {"ROLLED_BACK", "RECOVERY_REQUIRED"}:
            _rollback(transaction, current, reason=str(exc))
        if isinstance(exc, M11cClientError):
            raise
        raise M11cClientError("promotion_failed", "client promotion failed") from exc


def promote_client_plan(
    *,
    staged_runtime_root: str | Path,
    production_runtime_root: str | Path,
    fault_injector: FaultInjector | None = None,
) -> dict[str, Any]:
    """Promote one immutable staged client plan into the stable production root."""

    stage = _absolute_path(staged_runtime_root, field="staged_runtime_root")
    production = _absolute_path(production_runtime_root, field="production_runtime_root")
    if stage == production:
        _fail("promotion_scope_invalid", "staged and production runtime roots must differ")
    stage_plan, browser, stage_config = _stage_subject(stage)
    legacy = _absolute_path(stage_config["legacy_runtime_root"], field="legacy_runtime_root")
    bootstrap = _absolute_path(stage_config["bootstrap_authority_root"], field="bootstrap_authority_root")
    production.mkdir(parents=True, exist_ok=True)

    # Recovery must run before the normal live preflight.  After a crash during
    # backup the old client set is intentionally parked under ``previous`` and
    # the production root is temporarily incomplete; treating that state as a
    # fresh promotion would hide the recoverable transaction behind a generic
    # preflight failure.
    transaction_id = _transaction_id(stage_plan["client_plan_sha256"])
    transaction = _transaction_path(production, transaction_id)
    transaction_root = _transaction_root(production)
    if _state_path(transaction).exists():
        current = _load_state(transaction)
        if current.get("stage_plan_sha256") != stage_plan["client_plan_sha256"]:
            _fail("promotion_stage_changed", "existing promotion transaction targets another stage")
        result = _resume_transaction(transaction, current, fault_injector=fault_injector)
        return {
            "schema": PROMOTION_SCHEMA,
            "status": result["status"],
            "transaction_id": transaction_id,
            "production_root": str(production),
            "client_plan_sha256": result["expected_plan"]["client_plan_sha256"],
            "source_head": result["expected_plan"]["source_head"],
            "source_tree": result["expected_plan"]["source_tree"],
            "production_activation_performed": False,
            "registry_mutation_performed": False,
            "transaction_state": result["state"],
        }
    if transaction.exists():
        _fail("promotion_transaction_corrupt", "promotion transaction directory exists without state")

    # Production must be coherent before any transaction is created.
    try:
        previous_result = query_client_plan(runtime_root=production)
    except M11cClientError as exc:
        if exc.code != "native_payload_stale":
            raise M11cClientError("production_preflight_failed", "existing production client set is not coherent") from exc
        _recover_native_runtime_residue(production)
        try:
            previous_result = query_client_plan(runtime_root=production)
        except Exception as recovered_exc:
            raise M11cClientError("production_preflight_failed", "existing production client set is not coherent") from recovered_exc
    except Exception as exc:
        raise M11cClientError("production_preflight_failed", "existing production client set is not coherent") from exc
    _route_is_coherent(production)
    expected_plan, expected_config, expected_manifest = _production_documents(
        production=production,
        plan=stage_plan,
        browser_digest=browser["bundle_digest"],
        legacy=legacy,
        bootstrap=bootstrap,
    )
    if _production_matches(
        production,
        expected_plan=expected_plan,
        expected_config=expected_config,
        expected_manifest=expected_manifest,
    ):
        return {
            "schema": PROMOTION_SCHEMA,
            "status": "IDEMPOTENT_COMMITTED",
            "transaction_id": None,
            "production_root": str(production),
            "client_plan_sha256": expected_plan["client_plan_sha256"],
            "previous_source_head": previous_result["plan"]["source_head"],
            "source_head": expected_plan["source_head"],
            "source_tree": expected_plan["source_tree"],
            "production_activation_performed": False,
            "registry_mutation_performed": False,
        }

    transaction_root.mkdir(parents=True, exist_ok=True)

    transaction.mkdir(parents=True)
    _build_candidate(
        transaction=transaction,
        staged_root=stage,
        production=production,
        stage_plan=stage_plan,
        browser=browser,
        legacy=legacy,
        bootstrap=bootstrap,
    )
    native_manifest_name = Path(stage_plan["native_manifest_path"]).name
    native_executable_name = Path(stage_plan["native_host_executable"]).name
    initial = {
        "schema": PROMOTION_SCHEMA,
        "transaction_id": transaction_id,
        "state": "PREPARED",
        "state_history": ["PREPARED"],
        "stage_root": str(stage),
        "stage_plan_sha256": stage_plan["client_plan_sha256"],
        "production_root": str(production),
        "expected_plan": expected_plan,
        "expected_config": expected_config,
        "expected_manifest": expected_manifest,
        "native_manifest_name": native_manifest_name,
        "native_executable_name": native_executable_name,
        "previous_source_head": previous_result["plan"]["source_head"],
        "previous_source_tree": previous_result["plan"]["source_tree"],
        "production_activation_performed": False,
        "registry_mutation_performed": False,
    }
    state = _write_state(transaction, initial)
    result = _resume_transaction(transaction, state, fault_injector=fault_injector)
    return {
        "schema": PROMOTION_SCHEMA,
        "status": result["status"],
        "transaction_id": transaction_id,
        "production_root": str(production),
        "client_plan_sha256": result["expected_plan"]["client_plan_sha256"],
        "source_head": result["expected_plan"]["source_head"],
        "source_tree": result["expected_plan"]["source_tree"],
        "production_activation_performed": False,
        "registry_mutation_performed": False,
        "transaction_state": result["state"],
    }


__all__ = ["PROMOTION_SCHEMA", "promote_client_plan"]

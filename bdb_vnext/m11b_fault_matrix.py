"""M11b disposable activation fault matrix for the external BDB Next Bootstrap.

This module intentionally cannot activate production. It snapshots one exact
M11a prepared activation into an isolated experiment root, executes the future
switch boundaries only against that disposable root, injects crashes/failures,
and performs cold-restart classification. Production ACTIVE, Browser, Native
Host, registry and runtime writer/intake are never mutated here.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, NoReturn

from bdb_shared.evidence import canonical_json_bytes
from bdb_vnext.bootstrap import (
    BootstrapError,
    BootstrapLock,
    _absolute_path,
    _assert_no_reparse_components,
    _load_json,
    _overlaps,
    inspect_runtime_bundle,
    run_health_check,
)
from bdb_vnext.composition import GENERATION_ID, RUNTIME_ID
from bdb_vnext.m11a_prepared_activation import query_prepared_activation


EXPERIMENT_SCHEMA = "bdb-vnext-m11b-experiment-v1"
POINTER_SCHEMA = "bdb-vnext-m11b-pointer-v1"
EVENT_SCHEMA = "bdb-vnext-m11b-event-v1"
RECOVERY_SCHEMA = "bdb-vnext-m11b-recovery-v1"
MATRIX_RESULT_SCHEMA = "bdb-vnext-m11b-matrix-result-v1"

Boundary = Literal[
    "INITIALIZED",
    "SWITCH_INTENT",
    "POINTER_PUBLISHED",
    "START_REQUESTED",
    "HEALTH_VERIFIED",
    "CONCLUDED",
]
RecoveryOutcome = Literal[
    "KNOWN_GOOD_ACTIVE",
    "KNOWN_GOOD_CANDIDATE",
    "RECOVERED_PREVIOUS",
    "BLOCKED_QUARANTINED",
]
SlotName = Literal["ACTIVE", "PREVIOUS", "CANDIDATE"]
HealthProbe = Callable[[Mapping[str, Any], Mapping[str, Any]], bool]
WriteHook = Callable[[Path, Mapping[str, Any]], None]

_BOUNDARIES: tuple[Boundary, ...] = (
    "INITIALIZED",
    "SWITCH_INTENT",
    "POINTER_PUBLISHED",
    "START_REQUESTED",
    "HEALTH_VERIFIED",
    "CONCLUDED",
)
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class M11bFaultError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class InjectedCrash(BaseException):
    """Test-only abrupt-stop marker raised after a durable experiment boundary."""

    def __init__(self, boundary: Boundary) -> None:
        super().__init__(f"injected crash after {boundary}")
        self.boundary = boundary


def _fail(code: str, message: str) -> NoReturn:
    raise M11bFaultError(code, message)


def _digest(value: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _check_digest(value: object, field: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        _fail("invalid_digest", f"{field} must be an exact sha256 digest")
    return value


def _check_id(value: object, field: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        _fail("invalid_identifier", f"{field} is invalid")
    return value


def _experiment_path(root: Path) -> Path:
    return root / "experiment.json"


def _pointer_path(root: Path) -> Path:
    return root / "active-pointer.json"


def _events_root(root: Path) -> Path:
    return root / "events"


def _recovery_path(root: Path) -> Path:
    return root / "recovery.json"


def _atomic_replace_json(path: Path, document: Mapping[str, Any], *, hook: WriteHook | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if hook is not None:
        hook(path, document)
    staging = path.parent / f".{path.name}.partial-{uuid.uuid4().hex}"
    try:
        with staging.open("xb") as handle:
            handle.write(canonical_json_bytes(document))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, path)
    except OSError as exc:
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            pass
        raise M11bFaultError("experiment_write_failed", "durable experiment publication failed") from exc


def _write_immutable(path: Path, document: Mapping[str, Any], *, hook: WriteHook | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            current = _load_json(path, field="m11b_immutable")
        except OSError as exc:
            raise M11bFaultError("immutable_unavailable", "immutable experiment evidence cannot be read") from exc
        if current != document:
            _fail("experiment_identity_conflict", "immutable experiment identity already differs")
        return
    if hook is not None:
        hook(path, document)
    staging = path.parent / f".{path.name}.partial-{uuid.uuid4().hex}"
    try:
        with staging.open("xb") as handle:
            handle.write(canonical_json_bytes(document))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, path)
    except OSError as exc:
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            pass
        raise M11bFaultError("experiment_write_failed", "immutable experiment publication failed") from exc


def _event_path(root: Path, sequence: int, boundary: Boundary) -> Path:
    return _events_root(root) / f"{sequence:04d}-{boundary.lower()}.json"


def _event_documents(root: Path) -> list[dict[str, Any]]:
    events_root = _events_root(root)
    if not events_root.exists():
        return []
    documents: list[dict[str, Any]] = []
    for path in sorted(events_root.glob("*.json")):
        try:
            documents.append(_load_json(path, field="m11b_event"))
        except OSError as exc:
            raise M11bFaultError("event_unavailable", "M11b event cannot be read") from exc
    previous: str | None = None
    for sequence, document in enumerate(documents, start=1):
        if (
            document.get("schema") != EVENT_SCHEMA
            or document.get("sequence") != sequence
            or document.get("previous_event_sha256") != previous
        ):
            _fail("event_chain_invalid", "M11b event chain identity differs")
        supplied = _check_digest(document.get("event_sha256"), "event_sha256")
        payload = dict(document)
        payload.pop("event_sha256", None)
        if _digest(payload) != supplied:
            _fail("event_chain_invalid", "M11b event digest differs")
        previous = supplied
    return documents


def _append_event(root: Path, *, boundary: Boundary, details: Mapping[str, Any] | None = None) -> dict[str, Any]:
    documents = _event_documents(root)
    sequence = len(documents) + 1
    previous = None if not documents else documents[-1]["event_sha256"]
    payload = {
        "schema": EVENT_SCHEMA,
        "sequence": sequence,
        "boundary": boundary,
        "previous_event_sha256": previous,
        "details": dict(details or {}),
    }
    document = {**payload, "event_sha256": _digest(payload)}
    _write_immutable(_event_path(root, sequence, boundary), document)
    return document


def _maybe_crash(boundary: Boundary, crash_after: Boundary | None, *, hard: bool = False) -> None:
    if crash_after != boundary:
        return
    if hard:
        os._exit(91)
    raise InjectedCrash(boundary)


def _topology(
    experiment_root: str | Path,
    authority_root: str | Path,
    prepared_query: Mapping[str, Any],
) -> tuple[Path, Path]:
    experiment = _absolute_path(experiment_root, field="experiment_root")
    authority = _absolute_path(authority_root, field="authority_root")
    _assert_no_reparse_components(experiment, field="experiment_root")
    _assert_no_reparse_components(authority, field="authority_root")
    if _overlaps(experiment, authority):
        _fail("experiment_overlap", "M11b experiment root overlaps M11a authority")
    slots = prepared_query.get("slots")
    if not isinstance(slots, Mapping):
        _fail("prepared_subject_invalid", "prepared slots are missing")
    slot_map = slots.get("slots")
    for slot in ("ACTIVE", "PREVIOUS", "CANDIDATE"):
        document = slot_map.get(slot) if isinstance(slot_map, Mapping) else None
        if isinstance(document, Mapping):
            root = _absolute_path(document.get("bundle_root"), field=f"{slot.lower()}_bundle_root")
            if _overlaps(experiment, root):
                _fail("experiment_overlap", "M11b experiment overlaps immutable bundle bytes")
    prepared = prepared_query.get("prepared")
    if isinstance(prepared, Mapping):
        backup = prepared.get("backup")
        recovery = prepared.get("recovery")
        for value, field in (
            (backup.get("path") if isinstance(backup, Mapping) else None, "prepared_backup"),
            (recovery.get("target_root") if isinstance(recovery, Mapping) else None, "recovery_target"),
        ):
            if isinstance(value, str) and _overlaps(experiment, _absolute_path(value, field=field)):
                _fail("experiment_overlap", f"M11b experiment overlaps {field}")
    return experiment, authority


def initialize_fault_experiment(
    *,
    authority_root: str | Path,
    preparation_id: str,
    experiment_root: str | Path,
    experiment_id: str,
) -> dict[str, Any]:
    """Snapshot one exact M11a preparation into an isolated non-production experiment."""

    experiment_id = _check_id(experiment_id, "experiment_id")
    preparation_id = _check_id(preparation_id, "preparation_id")
    prepared_query = query_prepared_activation(
        authority_root=authority_root,
        preparation_id=preparation_id,
    )
    root, authority = _topology(experiment_root, authority_root, prepared_query)
    if root.exists() and any(root.iterdir()):
        _fail("experiment_exists", "M11b experiment root must start empty")
    root.mkdir(parents=True, exist_ok=True)
    slots = prepared_query["slots"]
    prepared = prepared_query["prepared"]
    slot_documents = slots["slots"]
    binding = dict(prepared["slot_binding"])
    payload = {
        "schema": EXPERIMENT_SCHEMA,
        "runtime_id": RUNTIME_ID,
        "generation_id": GENERATION_ID,
        "experiment_id": experiment_id,
        "experiment_only": True,
        "production_activation_performed": False,
        "authority_root": str(authority),
        "preparation_id": preparation_id,
        "preparation_sha256": prepared["preparation_sha256"],
        "slot_binding": binding,
        "required_control_schema": prepared["required_control_schema"],
        "slots": {
            slot: {
                key: slot_documents[slot][key]
                for key in ("bundle_root", "bundle_id", "bundle_role", "bundle_sha256", "manifest_sha256", "known_good")
            }
            for slot in ("ACTIVE", "PREVIOUS", "CANDIDATE")
        },
    }
    document = {**payload, "experiment_sha256": _digest(payload)}
    _write_immutable(_experiment_path(root), document)
    pointer_payload = {
        "schema": POINTER_SCHEMA,
        "experiment_id": experiment_id,
        "slot": "ACTIVE",
        "manifest_sha256": binding["active_manifest_sha256"],
        "epoch": 0,
    }
    pointer = {**pointer_payload, "pointer_sha256": _digest(pointer_payload)}
    _atomic_replace_json(_pointer_path(root), pointer)
    _append_event(root, boundary="INITIALIZED", details={"pointer_sha256": pointer["pointer_sha256"]})
    return query_fault_experiment(experiment_root=root)


def _load_experiment(root: Path) -> dict[str, Any]:
    try:
        document = _load_json(_experiment_path(root), field="m11b_experiment")
    except OSError as exc:
        raise M11bFaultError("experiment_unavailable", "M11b experiment identity cannot be read") from exc
    if (
        document.get("schema") != EXPERIMENT_SCHEMA
        or document.get("runtime_id") != RUNTIME_ID
        or document.get("generation_id") != GENERATION_ID
        or document.get("experiment_only") is not True
        or document.get("production_activation_performed") is not False
    ):
        _fail("experiment_identity_invalid", "M11b experiment identity differs")
    supplied = _check_digest(document.get("experiment_sha256"), "experiment_sha256")
    payload = dict(document)
    payload.pop("experiment_sha256", None)
    if _digest(payload) != supplied:
        _fail("experiment_identity_invalid", "M11b experiment digest differs")
    return document


def _load_pointer(root: Path, experiment: Mapping[str, Any]) -> dict[str, Any]:
    try:
        document = _load_json(_pointer_path(root), field="m11b_pointer")
    except OSError as exc:
        raise M11bFaultError("pointer_unavailable", "M11b pointer is temporarily unavailable") from exc
    except BootstrapError as exc:
        raise M11bFaultError("pointer_invalid", "M11b pointer cannot be validated") from exc
    if document.get("schema") != POINTER_SCHEMA or document.get("experiment_id") != experiment["experiment_id"]:
        _fail("pointer_invalid", "M11b pointer identity differs")
    slot = document.get("slot")
    if slot not in {"ACTIVE", "PREVIOUS", "CANDIDATE"}:
        _fail("pointer_invalid", "M11b pointer slot is invalid")
    expected = experiment["slot_binding"][f"{str(slot).lower()}_manifest_sha256"]
    if document.get("manifest_sha256") != expected:
        _fail("pointer_invalid", "M11b pointer manifest does not match its slot")
    supplied = _check_digest(document.get("pointer_sha256"), "pointer_sha256")
    payload = dict(document)
    payload.pop("pointer_sha256", None)
    if _digest(payload) != supplied:
        _fail("pointer_invalid", "M11b pointer digest differs")
    return document


def _publish_pointer(
    root: Path,
    experiment: Mapping[str, Any],
    slot: SlotName,
    *,
    write_hook: WriteHook | None = None,
) -> dict[str, Any]:
    current = _load_pointer(root, experiment)
    payload = {
        "schema": POINTER_SCHEMA,
        "experiment_id": experiment["experiment_id"],
        "slot": slot,
        "manifest_sha256": experiment["slot_binding"][f"{slot.lower()}_manifest_sha256"],
        "epoch": int(current["epoch"]) + 1,
    }
    document = {**payload, "pointer_sha256": _digest(payload)}
    _atomic_replace_json(_pointer_path(root), document, hook=write_hook)
    return _load_pointer(root, experiment)


def _default_health_probe(experiment: Mapping[str, Any], slot_document: Mapping[str, Any]) -> bool:
    authority = _absolute_path(experiment["authority_root"], field="authority_root")
    prepared = query_prepared_activation(
        authority_root=authority,
        preparation_id=experiment["preparation_id"],
    )
    slot = next(
        name
        for name in ("ACTIVE", "PREVIOUS", "CANDIDATE")
        if prepared["slots"]["slots"][name]["manifest_sha256"] == slot_document["manifest_sha256"]
    )
    source = prepared["slots"]["slots"][slot]
    legacy = prepared["slots"]["state"]["legacy_runtime_root"]
    bundle = inspect_runtime_bundle(
        source["bundle_root"],
        expected_role=source["bundle_role"],
        expected_sha256=source["bundle_sha256"],
        legacy_runtime_root=legacy,
    )
    try:
        run_health_check(
            bundle,
            required_control_schema=experiment["required_control_schema"],
            legacy_runtime_root=legacy,
            timeout_seconds=2.0,
        )
    except BootstrapError:
        return False
    return True


def _slot_document(experiment: Mapping[str, Any], slot: SlotName) -> Mapping[str, Any]:
    return experiment["slots"][slot]


def _latest_start_success(events: Sequence[Mapping[str, Any]]) -> bool | None:
    """Return the durable start observation, if one exists.

    False is authoritative evidence that the start call failed. A later cold
    health probe must not reinterpret that failed start as a successful one.
    """

    for event in reversed(events):
        if event.get("boundary") != "START_REQUESTED":
            continue
        details = event.get("details")
        if not isinstance(details, Mapping):
            return None
        value = details.get("start_success")
        return value if isinstance(value, bool) else None
    return None


def query_fault_experiment(*, experiment_root: str | Path) -> dict[str, Any]:
    root = _absolute_path(experiment_root, field="experiment_root")
    experiment = _load_experiment(root)
    events = _event_documents(root)
    pointer: Mapping[str, Any] | None
    pointer_error: str | None = None
    try:
        pointer = _load_pointer(root, experiment)
    except M11bFaultError as exc:
        pointer = None
        pointer_error = exc.code
    return {
        "schema": MATRIX_RESULT_SCHEMA,
        "experiment": experiment,
        "pointer": pointer,
        "pointer_error": pointer_error,
        "events": events,
        "production_activation_performed": False,
    }


def advance_fault_experiment(
    *,
    experiment_root: str | Path,
    crash_after: Boundary | None = None,
    hard_crash: bool = False,
    start_success: bool = True,
    health_probe: HealthProbe | None = None,
    pointer_write_hook: WriteHook | None = None,
) -> dict[str, Any]:
    """Execute future switch boundaries only inside the disposable experiment root."""

    if crash_after is not None and crash_after not in _BOUNDARIES:
        _fail("invalid_fault_boundary", "fault boundary is not part of M11b")
    root = _absolute_path(experiment_root, field="experiment_root")
    with BootstrapLock(root / "experiment.lock"):
        experiment = _load_experiment(root)
        query_prepared_activation(
            authority_root=experiment["authority_root"],
            preparation_id=experiment["preparation_id"],
        )
        events = _event_documents(root)
        boundaries = {event["boundary"] for event in events}
        _maybe_crash("INITIALIZED", crash_after, hard=hard_crash)

        if "SWITCH_INTENT" not in boundaries:
            _append_event(
                root,
                boundary="SWITCH_INTENT",
                details={
                    "from_manifest_sha256": experiment["slot_binding"]["active_manifest_sha256"],
                    "to_manifest_sha256": experiment["slot_binding"]["candidate_manifest_sha256"],
                    "recovery_manifest_sha256": experiment["slot_binding"]["previous_manifest_sha256"],
                },
            )
            _maybe_crash("SWITCH_INTENT", crash_after, hard=hard_crash)

        pointer = _load_pointer(root, experiment)
        if pointer["slot"] == "ACTIVE":
            pointer = _publish_pointer(root, experiment, "CANDIDATE", write_hook=pointer_write_hook)
        _maybe_crash("POINTER_PUBLISHED", crash_after, hard=hard_crash)

        boundaries = {event["boundary"] for event in _event_documents(root)}
        if "START_REQUESTED" not in boundaries:
            _append_event(root, boundary="START_REQUESTED", details={"start_success": bool(start_success)})
            _maybe_crash("START_REQUESTED", crash_after, hard=hard_crash)
        if not start_success:
            return cold_recover_fault_experiment(
                experiment_root=root,
                health_probe=health_probe,
                reason="start_failed",
            )

        probe = health_probe or _default_health_probe
        candidate_ok = probe(experiment, _slot_document(experiment, "CANDIDATE"))
        if not candidate_ok:
            return cold_recover_fault_experiment(
                experiment_root=root,
                health_probe=health_probe,
                reason="candidate_health_failed",
            )
        boundaries = {event["boundary"] for event in _event_documents(root)}
        if "HEALTH_VERIFIED" not in boundaries:
            _append_event(
                root,
                boundary="HEALTH_VERIFIED",
                details={"candidate_manifest_sha256": experiment["slot_binding"]["candidate_manifest_sha256"]},
            )
            _maybe_crash("HEALTH_VERIFIED", crash_after, hard=hard_crash)
        boundaries = {event["boundary"] for event in _event_documents(root)}
        if "CONCLUDED" not in boundaries:
            _append_event(root, boundary="CONCLUDED", details={"outcome": "KNOWN_GOOD_CANDIDATE"})
            _maybe_crash("CONCLUDED", crash_after, hard=hard_crash)
        return cold_recover_fault_experiment(experiment_root=root, health_probe=health_probe)


def _publish_recovery(
    root: Path,
    *,
    outcome: RecoveryOutcome,
    reason: str,
    pointer: Mapping[str, Any] | None,
) -> dict[str, Any]:
    payload = {
        "schema": RECOVERY_SCHEMA,
        "outcome": outcome,
        "reason": reason,
        "pointer": None if pointer is None else dict(pointer),
        "production_activation_performed": False,
    }
    document = {**payload, "recovery_sha256": _digest(payload)}
    _atomic_replace_json(_recovery_path(root), document)
    return document


def _recover_previous_or_block(
    root: Path,
    experiment: Mapping[str, Any],
    pointer: Mapping[str, Any],
    probe: HealthProbe,
    *,
    reason: str,
) -> dict[str, Any]:
    previous_ok = probe(experiment, _slot_document(experiment, "PREVIOUS"))
    if previous_ok:
        pointer = _publish_pointer(root, experiment, "PREVIOUS")
        outcome: RecoveryOutcome = "RECOVERED_PREVIOUS"
    else:
        outcome = "BLOCKED_QUARANTINED"
    recovery = _publish_recovery(root, outcome=outcome, reason=reason, pointer=pointer)
    return {"outcome": outcome, "recovery": recovery, "experiment": experiment}


def cold_recover_fault_experiment(
    *,
    experiment_root: str | Path,
    health_probe: HealthProbe | None = None,
    reason: str = "cold_restart",
) -> dict[str, Any]:
    """Classify/recover after an abrupt stop; never consult or mutate production ACTIVE."""

    root = _absolute_path(experiment_root, field="experiment_root")
    experiment = _load_experiment(root)
    probe = health_probe or _default_health_probe
    events = _event_documents(root)
    boundaries = {event["boundary"] for event in events}
    try:
        pointer = _load_pointer(root, experiment)
    except M11bFaultError as exc:
        recovery = _publish_recovery(
            root,
            outcome="BLOCKED_QUARANTINED",
            reason=exc.code,
            pointer=None,
        )
        return {"outcome": recovery["outcome"], "recovery": recovery, "experiment": experiment}

    slot = pointer["slot"]
    if slot == "ACTIVE":
        active_ok = probe(experiment, _slot_document(experiment, "ACTIVE"))
        if active_ok:
            outcome: RecoveryOutcome = "KNOWN_GOOD_ACTIVE"
            recovery = _publish_recovery(root, outcome=outcome, reason=reason, pointer=pointer)
            return {"outcome": outcome, "recovery": recovery, "experiment": experiment}
        return _recover_previous_or_block(
            root,
            experiment,
            pointer,
            probe,
            reason="active_unhealthy",
        )

    if slot == "PREVIOUS":
        previous_ok = probe(experiment, _slot_document(experiment, "PREVIOUS"))
        outcome = "RECOVERED_PREVIOUS" if previous_ok else "BLOCKED_QUARANTINED"
        recovery = _publish_recovery(root, outcome=outcome, reason=reason, pointer=pointer)
        return {"outcome": outcome, "recovery": recovery, "experiment": experiment}

    start_success = _latest_start_success(events)
    if start_success is None:
        return _recover_previous_or_block(
            root,
            experiment,
            pointer,
            probe,
            reason="candidate_not_started",
        )
    if start_success is False:
        return _recover_previous_or_block(
            root,
            experiment,
            pointer,
            probe,
            reason="candidate_start_failed",
        )

    candidate_ok = probe(experiment, _slot_document(experiment, "CANDIDATE"))
    if candidate_ok:
        if "HEALTH_VERIFIED" not in boundaries:
            _append_event(
                root,
                boundary="HEALTH_VERIFIED",
                details={"recovered_after_ack_loss": True},
            )
        if "CONCLUDED" not in {event["boundary"] for event in _event_documents(root)}:
            _append_event(
                root,
                boundary="CONCLUDED",
                details={"outcome": "KNOWN_GOOD_CANDIDATE", "cold_recovery": True},
            )
        outcome = "KNOWN_GOOD_CANDIDATE"
        recovery = _publish_recovery(root, outcome=outcome, reason=reason, pointer=pointer)
        return {"outcome": outcome, "recovery": recovery, "experiment": experiment}

    return _recover_previous_or_block(
        root,
        experiment,
        pointer,
        probe,
        reason="candidate_unhealthy",
    )


def matrix_pass(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """PASS only when every case ends in an allowed deterministic M11b class."""

    allowed = {
        "KNOWN_GOOD_ACTIVE",
        "KNOWN_GOOD_CANDIDATE",
        "RECOVERED_PREVIOUS",
        "BLOCKED_QUARANTINED",
    }
    outcomes = [item.get("outcome") for item in results]
    passed = bool(results) and all(outcome in allowed for outcome in outcomes)
    payload = {
        "schema": MATRIX_RESULT_SCHEMA,
        "status": "PASS" if passed else "FAIL",
        "case_count": len(results),
        "outcomes": outcomes,
        "production_activation_performed": False,
    }
    return {**payload, "matrix_sha256": _digest(payload)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bdb-vnext-m11b-fault-matrix")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--authority-root", required=True)
    init.add_argument("--preparation-id", required=True)
    init.add_argument("--experiment-root", required=True)
    init.add_argument("--experiment-id", required=True)
    run = sub.add_parser("run")
    run.add_argument("--experiment-root", required=True)
    run.add_argument("--crash-after", choices=_BOUNDARIES)
    run.add_argument("--hard-crash", action="store_true")
    recover = sub.add_parser("recover")
    recover.add_argument("--experiment-root", required=True)
    status = sub.add_parser("status")
    status.add_argument("--experiment-root", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "init":
            result = initialize_fault_experiment(
                authority_root=args.authority_root,
                preparation_id=args.preparation_id,
                experiment_root=args.experiment_root,
                experiment_id=args.experiment_id,
            )
        elif args.command == "run":
            result = advance_fault_experiment(
                experiment_root=args.experiment_root,
                crash_after=args.crash_after,
                hard_crash=bool(args.hard_crash),
            )
        elif args.command == "recover":
            result = cold_recover_fault_experiment(experiment_root=args.experiment_root)
        else:
            result = query_fault_experiment(experiment_root=args.experiment_root)
        exit_code = 0
    except InjectedCrash:
        raise
    except (M11bFaultError, BootstrapError, OSError, ValueError) as error:
        result = {
            "schema": MATRIX_RESULT_SCHEMA,
            "status": "BLOCKED",
            "error": {"code": str(getattr(error, "code", "m11b_failed")), "message": str(error)},
            "production_activation_performed": False,
        }
        exit_code = 20
    sys.stdout.buffer.write(canonical_json_bytes(result))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EXPERIMENT_SCHEMA",
    "InjectedCrash",
    "M11bFaultError",
    "advance_fault_experiment",
    "cold_recover_fault_experiment",
    "initialize_fault_experiment",
    "matrix_pass",
    "query_fault_experiment",
]

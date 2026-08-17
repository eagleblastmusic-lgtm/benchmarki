"""Legacy-compatible correction for the bounded M9a blocker probe.

The first blocker probe intentionally failed closed, but two of its shape checks
were stricter than the exact legacy readers at commit
4998aa16ff68d728637d09639ac79ced886393f6:

* NativeRequestReceiptStore._read() accepts an absent
  ``submission_reservations`` member and treats it as an empty mapping.
* WorkspacePromoter._read_existing_receipt() accepts historical promotion
  receipts that predate ``repository_event_seq``.

This module reuses the original read-only observation mechanics and replaces
only those two compatibility-sensitive classifiers.  It does not import legacy
runtime modules, open legacy authority for writing, mutate registry/runtime
state, execute commands, or grant vNext activation authority.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import m9a_blocker_probe as _base


PROBE_SCHEMA = "bdb-vnext-m9a-blocker-probe-v1"
REQUEST_RECEIPT_SCHEMA = "bdb-native-request-receipts-v1"
PROMOTION_RECEIPT_SCHEMA = "bdb-workspace-promotion-v1"
PROMOTER_STATE_SCHEMA = "bdb-workspace-promoter-state-v1"
REPOSITORY_EVENT_SEQ_SCHEMA = "bdb-repository-event-seq-v1"
_MAX_REQUEST_RECEIPTS = 1_024


def _receipt_shape(path: Path) -> dict[str, Any]:
    """Mirror the exact compatibility rules of NativeRequestReceiptStore._read."""

    if not path.exists():
        return {"status": "MISSING"}
    try:
        raw, digest = _base._read_json(path, max_bytes=4 * 1024 * 1024)
    except _base.BlockerProbeError as exc:
        return {"status": "INVALID", "issue": str(exc)}

    requests = raw.get("requests")
    reservations_present = "submission_reservations" in raw
    reservations = raw.get("submission_reservations", {})
    issues: list[str] = []
    if raw.get("schema") != REQUEST_RECEIPT_SCHEMA:
        issues.append("unsupported_schema")
    if not isinstance(requests, dict) or len(requests) > _MAX_REQUEST_RECEIPTS:
        issues.append("invalid_requests_store")
    if not isinstance(reservations, dict) or len(reservations) > _MAX_REQUEST_RECEIPTS:
        issues.append("invalid_submission_reservations_store")

    return {
        "status": "INVALID_SHAPE" if issues else "VALID_LEGACY_COMPAT_SHAPE",
        "digest": digest,
        "schema": raw.get("schema"),
        "top_level_keys": sorted(str(key) for key in raw)[:64],
        "requests_type": type(requests).__name__,
        "requests_count": len(requests) if isinstance(requests, dict) else None,
        "submission_reservations_present": reservations_present,
        "submission_reservations_type": type(reservations).__name__,
        "submission_reservations_count": len(reservations) if isinstance(reservations, dict) else None,
        "compatibility_rule": (
            "missing_submission_reservations_is_implicit_empty"
            if not reservations_present
            else "explicit_submission_reservations"
        ),
        "issues": issues,
    }


def _promotion_receipt_identity(document: dict[str, Any]) -> tuple[tuple[str, int] | None, str | None]:
    if document.get("schema") != PROMOTION_RECEIPT_SCHEMA:
        return None, "unsupported_receipt_schema"
    session_id = document.get("session_id")
    command_sequence = document.get("sequence")
    changed_files = document.get("changed_files")
    source_commit = document.get("source_commit")
    if (
        not isinstance(session_id, str)
        or not session_id
        or isinstance(command_sequence, bool)
        or not isinstance(command_sequence, int)
        or command_sequence <= 0
    ):
        return None, "invalid_command_identity"
    if not isinstance(changed_files, list) or not all(isinstance(value, str) for value in changed_files):
        return None, "invalid_changed_files"
    if not isinstance(source_commit, str) or not source_commit:
        return None, "missing_source_commit"
    parent_commit = document.get("parent_commit")
    if parent_commit is not None and (
        not isinstance(parent_commit, str) or _base._SHA40.fullmatch(parent_commit) is None
    ):
        return None, "invalid_parent_commit"
    return (session_id, command_sequence), None


def _promoter_observation(runtime: Path, *, max_records: int) -> dict[str, Any]:
    """Validate current and historical receipts using exact legacy read semantics.

    ``repository_event_seq`` was added by the current writer, but the exact
    existing-receipt reader does not require it.  Missing values are therefore
    classified as historical compatibility evidence, not corruption.
    """

    state_path = runtime / "workspace-promoter-state.json"
    receipts_root = runtime / "promotions"
    missing: list[str] = []
    if not state_path.is_file():
        missing.append("workspace-promoter-state.json")
    if not receipts_root.is_dir():
        missing.append("promotions/")
    if missing:
        return {"status": "MISSING", "missing_components": missing}

    issues: list[dict[str, Any]] = []
    try:
        state, state_digest = _base._read_json(state_path, max_bytes=4 * 1024 * 1024)
    except _base.BlockerProbeError as exc:
        return {"status": "INVALID", "issues": [{"code": str(exc)}]}
    if state.get("schema") != PROMOTER_STATE_SCHEMA:
        issues.append({"code": "unsupported_state_schema"})
    if not isinstance(state.get("initialized"), bool) or not isinstance(state.get("seen"), dict):
        issues.append({"code": "invalid_promoter_state"})

    receipt_files = sorted(
        (
            item
            for item in receipts_root.iterdir()
            if item.is_file()
            and not item.is_symlink()
            and item.suffix == ".json"
            and not item.name.startswith(".")
        ),
        key=lambda item: item.name,
    )
    truncated = len(receipt_files) > max_records
    identities: set[tuple[str, int]] = set()
    event_sequences: set[int] = set()
    sequenced_receipts = 0
    legacy_unsequenced_receipts = 0
    receipt_classes: Counter[str] = Counter()

    for path in receipt_files[:max_records]:
        try:
            document, digest = _base._read_json(path, max_bytes=4 * 1024 * 1024)
        except _base.BlockerProbeError as exc:
            code = str(exc)
            issues.append({"code": code, "file_id": _base._hash_text(path.name)})
            receipt_classes["INVALID"] += 1
            continue

        identity, code = _promotion_receipt_identity(document)
        if code is None and identity in identities:
            code = "duplicate_command_identity"
        if code is not None:
            issues.append({"code": code, "file_id": _base._hash_text(path.name), "digest": digest})
            receipt_classes["INVALID"] += 1
            continue
        assert identity is not None
        identities.add(identity)

        repository_event_seq = document.get("repository_event_seq")
        if repository_event_seq is None:
            legacy_unsequenced_receipts += 1
            receipt_classes["LEGACY_UNSEQUENCED"] += 1
            continue
        if (
            isinstance(repository_event_seq, bool)
            or not isinstance(repository_event_seq, int)
            or repository_event_seq <= 0
        ):
            issues.append(
                {
                    "code": "invalid_repository_event_seq",
                    "file_id": _base._hash_text(path.name),
                    "digest": digest,
                }
            )
            receipt_classes["INVALID"] += 1
            continue
        if repository_event_seq in event_sequences:
            issues.append(
                {
                    "code": "duplicate_repository_event_seq",
                    "file_id": _base._hash_text(path.name),
                    "digest": digest,
                }
            )
            receipt_classes["INVALID"] += 1
            continue
        event_sequences.add(repository_event_seq)
        sequenced_receipts += 1
        receipt_classes["SEQUENCED"] += 1

    sequence_path = receipts_root / ".repository-event-seq.json"
    sequence_value: int | None = None
    sequence_digest: str | None = None
    if sequence_path.exists():
        try:
            seq_doc, sequence_digest = _base._read_json(sequence_path, max_bytes=1024 * 1024)
            candidate = seq_doc.get("repository_event_seq")
            if seq_doc.get("schema") != REPOSITORY_EVENT_SEQ_SCHEMA:
                issues.append({"code": "unsupported_sequence_schema", "digest": sequence_digest})
            elif isinstance(candidate, bool) or not isinstance(candidate, int) or candidate < 0:
                issues.append({"code": "invalid_repository_sequence", "digest": sequence_digest})
            else:
                sequence_value = candidate
        except _base.BlockerProbeError as exc:
            issues.append({"code": str(exc), "file_id": _base._hash_text(sequence_path.name)})

    # The writer advances the sequence file before replacing the receipt, so a
    # crash may legitimately leave the counter ahead of all surviving receipts.
    # It may never be behind a sequence that was successfully persisted.
    if (
        sequence_value is not None
        and event_sequences
        and max(event_sequences) > sequence_value
    ):
        issues.append({"code": "sequence_counter_behind_persisted_receipt"})
    if sequence_value is None and event_sequences:
        issues.append({"code": "sequence_state_missing_for_sequenced_receipts"})

    status = "TRUNCATED" if truncated else "INVALID" if issues else "VALID_LEGACY_COMPAT"
    return {
        "status": status,
        "state_digest": state_digest,
        "initialized": state.get("initialized"),
        "seen_count": len(state.get("seen", {})) if isinstance(state.get("seen"), dict) else None,
        "receipt_count": len(receipt_files),
        "receipt_classification_counts": dict(sorted(receipt_classes.items())),
        "sequenced_receipts": sequenced_receipts,
        "legacy_unsequenced_receipts": legacy_unsequenced_receipts,
        "repository_event_seq": sequence_value,
        "repository_event_seq_digest": sequence_digest,
        "issues": issues[:max_records],
        "truncated": truncated,
        "compatibility_rule": "historical_receipts_may_omit_repository_event_seq",
    }


def probe_profile(
    *,
    profile_id: str,
    bridge_config_path: str | Path,
    native_config_path: str | Path,
    scratch_dir: str | Path,
    max_records: int = 500,
    now_fn: Callable[[], datetime] | None = None,
    pid_alive_fn: Callable[[int], bool] = _base._pid_alive,
    wake_event_fn: Callable[[Path], bool] = _base._wake_event_exists,
) -> _base.BlockerProbe:
    """Run the original bounded observation with corrected legacy shape rules."""

    if not profile_id or len(profile_id) > 128:
        raise ValueError("profile_id must be bounded")
    if not 1 <= max_records <= 5000:
        raise ValueError("max_records must be between 1 and 5000")

    bridge_path = Path(bridge_config_path).expanduser().resolve(strict=True)
    native_path = Path(native_config_path).expanduser().resolve(strict=True)
    scratch = Path(scratch_dir).expanduser().resolve(strict=False)
    bridge = _base._effective_bridge_paths(bridge_path)
    now = (now_fn or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc)
    native = _base._native_observation(native_path, bridge_path, now=now)
    snapshot, snapshot_identity = _base._stable_copy_journal(
        bridge["journal"], scratch / profile_id
    )
    journal, command_states = _base._journal_observation(
        snapshot,
        max_records=max_records,
        pid_alive_fn=pid_alive_fn,
    )
    spool = _base._spool_observation(
        bridge["spool"], command_states, max_records=max_records
    )
    receipts = _receipt_shape(native["request_store_path"])
    promoter = _promoter_observation(bridge["runtime"], max_records=max_records)

    return _base.BlockerProbe(
        profile_id=profile_id,
        bridge_config_digest=bridge["digest"],
        journal_snapshot=snapshot_identity,
        journal=journal,
        spool=spool,
        native={key: value for key, value in native.items() if key != "request_store_path"},
        receipts=receipts,
        promoter=promoter,
        wake_event_present=(
            wake_event_fn(bridge["runtime"])
            if bridge["direct_spool_enabled"]
            else False
        ),
    )


__all__ = [
    "PROBE_SCHEMA",
    "probe_profile",
    "_promoter_observation",
    "_receipt_shape",
]

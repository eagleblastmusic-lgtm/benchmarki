"""M9b Browser/Native client-gate record for the isolated Next generation.

Before M11c this record modeled the final external activation fence.  The
frozen M11c contract deliberately removes that authority: the ProgramData
Bootstrap slot state is now the sole product activation truth.  M9b remains a
subordinate route gate that binds exact Browser/Native/freeze/source identity.

The client-gate transition remains two phase::

    CLIENTS_VERIFIED -> ACTIVATING -> ACTIVE

but there is intentionally no public ``activate`` helper anymore.  M11c alone
coordinates the private gate transition with the external slot switch and M3c
intake.  Even a forged/runtime-local M9b ACTIVE record cannot open production
admission because Native also requires the independent M11c Bootstrap ACTIVE
state.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from bdb_shared.evidence import canonical_json_bytes, semantic_digest
from bdb_vnext.composition import (
    BROWSER_EXTENSION_ID,
    CONFIG_GENERATION,
    GENERATION_ID,
    NATIVE_HOST_NAME,
    PROTOCOL_GENERATION,
)


M9B_ACTIVATION_SCHEMA = "bdb-vnext-m9b-activation-v1"
M9B_ACTIVATION_FILENAME = "m9b-activation.json"
M9B_STATES = frozenset({"CLIENTS_VERIFIED", "ACTIVATING", "ACTIVE"})
M9B_ROLLBACK_MODE = "ROLL_FORWARD_ONLY"
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


class M9bActivationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> NoReturn:
    raise M9bActivationError(code, message)


def _sha40(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _SHA40.fullmatch(value) is None:
        _fail("invalid_source_identity", f"{field} must be a lowercase 40-character Git SHA")
    return value


def _sha256(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        _fail("invalid_digest", f"{field} must be a lowercase sha256 digest")
    return value


def activation_path(runtime_root: str | Path) -> Path:
    root = Path(runtime_root).expanduser().absolute()
    return root / "config" / M9B_ACTIVATION_FILENAME


def _stable_read(path: Path, *, max_bytes: int = 64 * 1024) -> bytes:
    try:
        before = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise M9bActivationError("activation_read_failed", "M9b client-gate record cannot be inspected") from exc
    if path.is_symlink() or not path.is_file() or before.st_size > max_bytes:
        _fail("activation_record_invalid", "M9b client-gate record must be a bounded regular file")
    try:
        payload = path.read_bytes()
        after = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise M9bActivationError("activation_read_failed", "M9b client-gate record cannot be read") from exc
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        _fail("activation_record_unstable", "M9b client-gate record changed during observation")
    if len(payload) > max_bytes:
        _fail("activation_record_invalid", "M9b client-gate record exceeds its bounded size")
    return payload


def _record_digest(document: Mapping[str, Any]) -> str:
    return semantic_digest({key: value for key, value in document.items() if key != "record_digest"})


@dataclass(frozen=True)
class ActivationRecord:
    activation_id: str
    state: str
    source_head: str
    source_tree: str
    m9a_freeze_digest: str
    browser_bundle_digest: str
    native_manifest_digest: str
    writer_enabled: bool
    intake_enabled: bool
    schema: str = M9B_ACTIVATION_SCHEMA
    generation_id: str = GENERATION_ID
    config_generation: str = CONFIG_GENERATION
    protocol_generation: str = PROTOCOL_GENERATION
    browser_extension_id: str = BROWSER_EXTENSION_ID
    native_host_name: str = NATIVE_HOST_NAME
    rollback_mode: str = M9B_ROLLBACK_MODE

    def __post_init__(self) -> None:
        if self.schema != M9B_ACTIVATION_SCHEMA:
            _fail("activation_schema_mismatch", "M9b client-gate schema differs")
        if self.state not in M9B_STATES:
            _fail("activation_state_invalid", "M9b client-gate state is unsupported")
        if not isinstance(self.activation_id, str) or not self.activation_id.startswith("m9b-") or len(self.activation_id) > 96:
            _fail("activation_identity_invalid", "M9b activation identity is invalid")
        _sha40(self.source_head, field="source_head")
        _sha40(self.source_tree, field="source_tree")
        _sha256(self.m9a_freeze_digest, field="m9a_freeze_digest")
        _sha256(self.browser_bundle_digest, field="browser_bundle_digest")
        _sha256(self.native_manifest_digest, field="native_manifest_digest")
        if self.generation_id != GENERATION_ID or self.config_generation != CONFIG_GENERATION:
            _fail("generation_identity_mismatch", "M9b generation identity differs")
        if self.protocol_generation != PROTOCOL_GENERATION:
            _fail("protocol_generation_mismatch", "M9b protocol generation differs")
        if self.browser_extension_id != BROWSER_EXTENSION_ID or self.native_host_name != NATIVE_HOST_NAME:
            _fail("client_identity_mismatch", "M9b Browser/Native identity differs")
        if self.rollback_mode != M9B_ROLLBACK_MODE:
            _fail("rollback_mode_mismatch", "M9b rollback class differs")
        expected_enabled = self.state == "ACTIVE"
        if self.writer_enabled is not expected_enabled or self.intake_enabled is not expected_enabled:
            _fail("activation_flags_invalid", "M9b writer/intake flags do not match client-gate state")

    def as_dict(self) -> dict[str, Any]:
        document = {
            "schema": self.schema,
            "activation_id": self.activation_id,
            "state": self.state,
            "generation_id": self.generation_id,
            "config_generation": self.config_generation,
            "protocol_generation": self.protocol_generation,
            "browser_extension_id": self.browser_extension_id,
            "native_host_name": self.native_host_name,
            "source_head": self.source_head,
            "source_tree": self.source_tree,
            "m9a_freeze_digest": self.m9a_freeze_digest,
            "browser_bundle_digest": self.browser_bundle_digest,
            "native_manifest_digest": self.native_manifest_digest,
            "writer_enabled": self.writer_enabled,
            "intake_enabled": self.intake_enabled,
            "rollback_mode": self.rollback_mode,
        }
        document["record_digest"] = _record_digest(document)
        return document

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ActivationRecord":
        document = {str(key): item for key, item in value.items()}
        expected_keys = {
            "schema",
            "activation_id",
            "state",
            "generation_id",
            "config_generation",
            "protocol_generation",
            "browser_extension_id",
            "native_host_name",
            "source_head",
            "source_tree",
            "m9a_freeze_digest",
            "browser_bundle_digest",
            "native_manifest_digest",
            "writer_enabled",
            "intake_enabled",
            "rollback_mode",
            "record_digest",
        }
        if set(document) != expected_keys:
            _fail("activation_record_invalid", "M9b client-gate record fields differ")
        supplied_digest = document.pop("record_digest")
        if supplied_digest != _record_digest(document):
            _fail("activation_digest_mismatch", "M9b client-gate record digest differs")
        return cls(
            activation_id=document["activation_id"],
            state=document["state"],
            source_head=document["source_head"],
            source_tree=document["source_tree"],
            m9a_freeze_digest=document["m9a_freeze_digest"],
            browser_bundle_digest=document["browser_bundle_digest"],
            native_manifest_digest=document["native_manifest_digest"],
            writer_enabled=document["writer_enabled"],
            intake_enabled=document["intake_enabled"],
            schema=document["schema"],
            generation_id=document["generation_id"],
            config_generation=document["config_generation"],
            protocol_generation=document["protocol_generation"],
            browser_extension_id=document["browser_extension_id"],
            native_host_name=document["native_host_name"],
            rollback_mode=document["rollback_mode"],
        )


def read_activation(runtime_root: str | Path) -> ActivationRecord | None:
    path = activation_path(runtime_root)
    if not path.exists():
        return None
    try:
        document = json.loads(_stable_read(path).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise M9bActivationError("activation_record_invalid", "M9b client-gate record is not valid JSON") from exc
    if not isinstance(document, Mapping):
        _fail("activation_record_invalid", "M9b client-gate record must be an object")
    return ActivationRecord.from_mapping(document)


def require_active(runtime_root: str | Path) -> ActivationRecord:
    """Require only the subordinate M9b client gate, never product activation."""

    record = read_activation(runtime_root)
    if record is None or record.state != "ACTIVE" or not record.writer_enabled or not record.intake_enabled:
        _fail("vnext_not_active", "vNext Browser/Native client gate is not ACTIVE")
    return record


def validate_m9a_freeze_report(value: Mapping[str, Any]) -> str:
    report = {str(key): item for key, item in value.items()}
    if report.get("schema") != "bdb-vnext-m9a-freeze-report-v1":
        _fail("m9a_evidence_invalid", "M9a freeze report schema differs")
    if report.get("status") != "PASS_CLOSED":
        _fail("m9a_not_closed", "M9a freeze report is not PASS_CLOSED")
    required_true = ("legacy_ingress_frozen", "legacy_writer_frozen", "zero_new_write_observed", "archive_created")
    if any(report.get(field) is not True for field in required_true):
        _fail("m9a_not_closed", "M9a freeze report does not prove the required legacy fences")
    if report.get("partial_freeze_requires_roll_forward") is not False:
        _fail("m9a_evidence_invalid", "M9a freeze report still requires partial-freeze reconciliation")
    if report.get("vnext_activation_allowed") is not False or report.get("m9b_allowed") is not False:
        _fail("m9a_evidence_invalid", "M9a must remain non-authoritative for vNext activation")
    freeze_digest = report.get("freeze_digest")
    return _sha256(freeze_digest, field="freeze_digest")


def _atomic_write(
    path: Path,
    record: ActivationRecord,
    *,
    fault_hook: Callable[[str], None] | None = None,
) -> None:
    payload = canonical_json_bytes(record.as_dict())
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            if fault_hook:
                fault_hook("during_temp_write")
            handle.flush()
            os.fsync(handle.fileno())
            if fault_hook:
                fault_hook("after_fsync")
        os.replace(temporary, path)
        if fault_hook:
            fault_hook("after_replace")
    except Exception as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise M9bActivationError("activation_write_failed", "M9b client-gate record could not be written atomically") from exc


def write_activation(
    runtime_root: str | Path,
    record: ActivationRecord,
    *,
    fault_hook: Callable[[str], None] | None = None,
) -> ActivationRecord:
    """Persist one already-validated M9b record through the canonical writer.

    This narrow boundary is used by post-maintenance reconciliation.  It is
    not a product activation API and cannot construct an invalid record.
    """

    _atomic_write(activation_path(runtime_root), record, fault_hook=fault_hook)
    return record


def record_clients_verified(
    runtime_root: str | Path,
    *,
    m9a_report: Mapping[str, Any],
    source_head: str,
    source_tree: str,
    browser_bundle_digest: str,
    native_manifest_digest: str,
    activation_id: str | None = None,
) -> ActivationRecord:
    """Persist exact client/freeze evidence while keeping the route gate OFF."""

    path = activation_path(runtime_root)
    if read_activation(runtime_root) is not None:
        _fail("activation_already_exists", "M9b client-gate record already exists")
    record = ActivationRecord(
        activation_id=activation_id or f"m9b-{secrets.token_hex(16)}",
        state="CLIENTS_VERIFIED",
        source_head=_sha40(source_head, field="source_head"),
        source_tree=_sha40(source_tree, field="source_tree"),
        m9a_freeze_digest=validate_m9a_freeze_report(m9a_report),
        browser_bundle_digest=_sha256(browser_bundle_digest, field="browser_bundle_digest"),
        native_manifest_digest=_sha256(native_manifest_digest, field="native_manifest_digest"),
        writer_enabled=False,
        intake_enabled=False,
    )
    _atomic_write(path, record)
    return record


def _begin_bootstrap_client_gate(
    runtime_root: str | Path,
    *,
    expected_activation_id: str,
) -> ActivationRecord:
    """M11c-private transition to ACTIVATING; not a product activation API."""

    current = read_activation(runtime_root)
    if current is None:
        _fail("activation_missing", "M9b clients have not been verified")
    if current.activation_id != expected_activation_id:
        _fail("activation_identity_mismatch", "M9b activation identity changed")
    if current.state != "CLIENTS_VERIFIED":
        _fail("activation_state_conflict", "M9b client gate is not at CLIENTS_VERIFIED")
    activating = ActivationRecord(
        activation_id=current.activation_id,
        state="ACTIVATING",
        source_head=current.source_head,
        source_tree=current.source_tree,
        m9a_freeze_digest=current.m9a_freeze_digest,
        browser_bundle_digest=current.browser_bundle_digest,
        native_manifest_digest=current.native_manifest_digest,
        writer_enabled=False,
        intake_enabled=False,
    )
    _atomic_write(activation_path(runtime_root), activating)
    return activating


def _finalize_bootstrap_client_gate(
    runtime_root: str | Path,
    *,
    expected_activation_id: str,
    canonical_intake_is_enabled: Callable[[], bool],
) -> ActivationRecord:
    """M11c-private finalization after external Bootstrap ACTIVE is proven."""

    current = read_activation(runtime_root)
    if current is None or current.activation_id != expected_activation_id or current.state != "ACTIVATING":
        _fail("activation_state_conflict", "M9b client gate is not the expected ACTIVATING state")
    if canonical_intake_is_enabled() is not True:
        _fail("canonical_intake_not_enabled", "canonical intake is not enabled; client gate cannot finalize")
    active = ActivationRecord(
        activation_id=current.activation_id,
        state="ACTIVE",
        source_head=current.source_head,
        source_tree=current.source_tree,
        m9a_freeze_digest=current.m9a_freeze_digest,
        browser_bundle_digest=current.browser_bundle_digest,
        native_manifest_digest=current.native_manifest_digest,
        writer_enabled=True,
        intake_enabled=True,
    )
    _atomic_write(activation_path(runtime_root), active)
    return active


__all__ = [
    "ActivationRecord",
    "M9B_ACTIVATION_FILENAME",
    "M9B_ACTIVATION_SCHEMA",
    "M9B_ROLLBACK_MODE",
    "M9B_STATES",
    "M9bActivationError",
    "activation_path",
    "read_activation",
    "record_clients_verified",
    "require_active",
    "validate_m9a_freeze_report",
    "write_activation",
]

"""M3c internal vNext admission authority closure.

The supported inactive-generation route is deliberately small:

    Browser outbox -> Native transport -> this authority -> M3a transaction

The M3a transaction remains the only operation that can create a vNext Task.
The Browser outbox is recovery state, the Native bridge is transport, and the
kill switch only disables new admission.  This module never opens legacy
state, never provides a legacy fallback, and never enables production intake.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NoReturn

from bdb_shared.evidence import canonical_json_bytes
from bdb_vnext.composition import VNextLayout, _provider_registry
from bdb_vnext.m3a_submission import (
    AdmissionReceipt,
    M3aError,
    ShadowSubmissionRequest,
    ShadowSubmissionStore,
    ShadowTask,
)
from bdb_vnext.control_store import ControlStoreError, commit_control_write
from bdb_vnext.m3b_browser_admission import (
    AdmissionEnvelope,
    BrowserAdmissionClient,
    BrowserAdmissionOutbox,
    M3B_PROTOCOL_GENERATION,
    ProtocolCapability,
)


M3C_SCHEMA = "bdb-vnext-m3c-admission-v1"
M3C_CONTROL_SCHEMA_V1 = "bdb-vnext-m3c-control-v1"
M3C_CONTROL_SCHEMA = "bdb-vnext-m3c-control-v2"
M3C_KILL_SWITCH_SCHEMA = "bdb-vnext-m3c-kill-switch-v1"
M3C_PROTOCOL_GENERATION = M3B_PROTOCOL_GENERATION
M3C_AUTHORITY_ID = "devmaster.bdb.vnext.canonical-submission-task"
M3C_WRITER_ID = "m3c-vnext-canonical-admission-writer"
M3C_CANONICAL_ROLE = "canonical_submission_task"

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}$")
_GATE_LOCKS: dict[str, threading.RLock] = {}
_GATE_LOCKS_LOCK = threading.Lock()


class M3cError(RuntimeError):
    """Bounded, machine-readable M3c failure."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def _fail(code: str, message: str, *, details: Mapping[str, Any] | None = None) -> NoReturn:
    raise M3cError(code, message, details=details)


def _digest(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        _fail("invalid_digest", f"{field} must be a lowercase sha256 digest")
    return value


def _key(value: object, *, field: str = "submission_key") -> str:
    if not isinstance(value, str) or _KEY.fullmatch(value) is None:
        _fail("invalid_submission_key", f"{field} is not a valid opaque submission key")
    return value


def _gate_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(os.path.normpath(os.fspath(path)))
    with _GATE_LOCKS_LOCK:
        return _GATE_LOCKS.setdefault(key, threading.RLock())


def _write_canonical(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("xb") as handle:
            handle.write(canonical_json_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        _fail("control_state_write_failed", f"cannot write {path.name}")


def _read_object(path: Path, *, code: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(code, f"cannot read {path.name}")
    if not isinstance(value, Mapping):
        _fail(code, f"{path.name} must contain an object")
    return {str(key): item for key, item in value.items()}


@dataclass(frozen=True)
class CanonicalAdmissionQuery:
    """Canonical read projection; it is never sourced from Browser state."""

    submission_key: str
    request_digest: str
    status: str
    disposition: str
    task_id: str | None
    intent_revision_id: str | None
    outcome: str

    def __post_init__(self) -> None:
        _key(self.submission_key)
        _digest(self.request_digest, field="request_digest")
        if self.status not in {"ACCEPTED", "TOMBSTONED"}:
            _fail("invalid_query", "canonical query status is unsupported")
        if self.disposition not in {"ACCEPTED", "REJECTED"}:
            _fail("invalid_query", "canonical query disposition is unsupported")
        if self.status == "ACCEPTED" and (self.disposition != "ACCEPTED" or self.task_id is None):
            _fail("invalid_query", "accepted query must bind a Task")
        if self.status == "TOMBSTONED" and (self.disposition != "REJECTED" or self.task_id is not None):
            _fail("invalid_query", "tombstone query must not bind a Task")

    @classmethod
    def from_receipt(cls, receipt: AdmissionReceipt) -> "CanonicalAdmissionQuery":
        return cls(
            submission_key=receipt.submission_key,
            request_digest=receipt.request_digest,
            status=receipt.status,
            disposition=receipt.disposition,
            task_id=receipt.task_id,
            intent_revision_id=receipt.intent_revision_id,
            outcome=receipt.outcome,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": M3C_SCHEMA,
            "authority": M3C_CANONICAL_ROLE,
            "protocol_generation": M3C_PROTOCOL_GENERATION,
            "submission_key": self.submission_key,
            "request_digest": self.request_digest,
            "status": self.status,
            "disposition": self.disposition,
            "task_id": self.task_id,
            "intent_revision_id": self.intent_revision_id,
            "outcome": self.outcome,
        }


@dataclass(frozen=True)
class AdmissionPath:
    """Evidence map used by the post-closure alternate-path scan."""

    path_id: str
    role: str
    can_accept: bool
    can_persist: bool
    authority: str
    supported: bool
    generation: str
    owner: str
    closure_action: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "path_id": self.path_id,
            "role": self.role,
            "can_accept": self.can_accept,
            "can_persist": self.can_persist,
            "authority": self.authority,
            "supported": self.supported,
            "generation": self.generation,
            "owner": self.owner,
            "closure_action": self.closure_action,
        }


def postclosure_authority_map() -> tuple[AdmissionPath, ...]:
    """Return the explicit supported vNext admission topology after M3c."""

    return (
        AdmissionPath(
            "canonical-submission-task",
            "submission_to_task_transaction",
            True,
            True,
            M3C_CANONICAL_ROLE,
            True,
            M3C_PROTOCOL_GENERATION,
            M3C_WRITER_ID,
            "sole_supported_admission_writer",
        ),
        AdmissionPath(
            "browser-outbox",
            "client_transport_recovery",
            False,
            True,
            "none",
            True,
            M3C_PROTOCOL_GENERATION,
            "m3b-shadow-browser-outbox",
            "retain_recovery_only",
        ),
        AdmissionPath(
            "native-transport",
            "protocol_transport_and_lookup",
            False,
            False,
            "none",
            True,
            M3C_PROTOCOL_GENERATION,
            "com.bartosz.dev_bridge.vnext",
            "retain_transport_only",
        ),
        AdmissionPath(
            "m3a-shadow-fixture",
            "historical_test_fixture",
            True,
            True,
            "test_only",
            False,
            M3C_PROTOCOL_GENERATION,
            "m3a-shadow-test-writer",
            "not_reachable_from_supported_composition",
        ),
        AdmissionPath(
            "m3b-shadow-bridge",
            "historical_test_transport",
            True,
            False,
            "test_only",
            False,
            M3C_PROTOCOL_GENERATION,
            "m3b-shadow-native-bridge",
            "not_reachable_from_supported_composition",
        ),
        AdmissionPath(
            "legacy-receipt-spool",
            "legacy_generation_transport",
            True,
            True,
            "legacy_only",
            False,
            "legacy-generation",
            "legacy-runtime",
            "operational_legacy_untouched",
        ),
    )


def scan_supported_vnext_admission_paths() -> dict[str, Any]:
    """Mechanically prove the supported composition has one accepting writer."""

    paths = postclosure_authority_map()
    supported_writers = [path for path in paths if path.supported and path.can_accept]
    alternates = [
        path
        for path in supported_writers
        if path.authority != M3C_CANONICAL_ROLE or path.owner != M3C_WRITER_ID
    ]
    providers = _provider_registry()
    canonical_providers = [
        provider
        for provider in providers
        if provider.get("kind") == "canonical_admission_authority"
    ]
    return {
        "schema": M3C_SCHEMA,
        "supported_accepting_writers": [path.as_dict() for path in supported_writers],
        "alternate_accepting_writers": [path.as_dict() for path in alternates],
        "canonical_provider_ids": [provider.get("provider_id") for provider in canonical_providers],
        "legacy_paths_supported": any(path.supported and path.generation == "legacy-generation" for path in paths),
        "pass": len(supported_writers) == 1 and not alternates and len(canonical_providers) == 1,
    }


class CanonicalVNextAdmissionAuthority:
    """Internal vNext authority wrapping one dedicated M3a transaction store."""

    def __init__(self, store: ShadowSubmissionStore) -> None:
        if not isinstance(store, ShadowSubmissionStore):
            _fail("invalid_control_store", "M3c requires the dedicated vNext M3a store")
        if store.writer_id != "m3a-shadow-test-writer":
            _fail("control_store_identity_mismatch", "M3c control store writer identity differs")
        self._store = store
        self._control_root = Path(store.control_root)
        self._control_marker = self._control_root / "m3c-control.json"
        self._kill_switch_path = self._control_root / "m3c-kill-switch.json"
        self._lock = _gate_lock(self._kill_switch_path)
        self._ensure_control_marker()
        self._ensure_kill_switch()

    @classmethod
    def open(cls, runtime_root: str | Path, *, legacy_root: str | Path) -> "CanonicalVNextAdmissionAuthority":
        layout = VNextLayout.create(runtime_root)
        layout.assert_isolated(legacy_runtime_root=legacy_root)
        store = ShadowSubmissionStore(runtime_root, shadow=True, legacy_root=legacy_root)
        try:
            return cls(store)
        except Exception:
            store.close()
            raise

    @property
    def control_database_path(self) -> Path:
        return self._store.database_path

    @property
    def runtime_root(self) -> Path:
        """The exact isolated root shared by vNext shadow substrates."""

        return self._store.root

    def _ensure_control_marker(self) -> None:
        expected = {
            "schema": M3C_CONTROL_SCHEMA,
            "authority_id": M3C_AUTHORITY_ID,
            "writer_id": M3C_WRITER_ID,
            "protocol_generation": M3C_PROTOCOL_GENERATION,
            "mode": "INTERNAL_CANONICAL_ONLY",
            "legacy_import": False,
            "alternate_admission": False,
        }
        legacy = {
            **expected,
            "schema": M3C_CONTROL_SCHEMA_V1,
            "production_intake": False,
        }
        with self._lock:
            if self._control_marker.exists():
                current = _read_object(self._control_marker, code="control_state_invalid")
                if current == legacy:
                    _write_canonical(self._control_marker, expected)
                    return
                if current != expected:
                    _fail("control_store_identity_mismatch", "M3c control marker differs")
                return
            _write_canonical(self._control_marker, expected)

    def _ensure_kill_switch(self) -> None:
        expected_static = {
            "schema": M3C_KILL_SWITCH_SCHEMA,
            "authority_id": M3C_AUTHORITY_ID,
            "protocol_generation": M3C_PROTOCOL_GENERATION,
            "writer_id": M3C_WRITER_ID,
        }
        connection = self._store.control_connection
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS m3c_kill_switch ("
                "id INTEGER PRIMARY KEY CHECK(id = 1),"
                "schema TEXT NOT NULL, authority_id TEXT NOT NULL,"
                "protocol_generation TEXT NOT NULL, writer_id TEXT NOT NULL,"
                "admission_enabled INTEGER NOT NULL CHECK(admission_enabled IN (0,1)))"
            )
            row = connection.execute(
                "SELECT schema,authority_id,protocol_generation,writer_id,admission_enabled "
                "FROM m3c_kill_switch WHERE id=1"
            ).fetchone()
        except Exception as exc:
            _fail("kill_switch_invalid", "M3c kill-switch state could not be read")
        if self._kill_switch_path.exists():
            current = _read_object(self._kill_switch_path, code="kill_switch_invalid")
            if any(current.get(key) != value for key, value in expected_static.items()) or not isinstance(
                current.get("admission_enabled"), bool
            ):
                _fail("kill_switch_invalid", "M3c kill switch identity differs")
            file_enabled = bool(current["admission_enabled"])
        else:
            file_enabled = True
        if row is None:
            try:
                connection.execute(
                    "INSERT INTO m3c_kill_switch(id,schema,authority_id,protocol_generation,writer_id,admission_enabled) "
                    "VALUES (1,?,?,?,?,?)",
                    (
                        expected_static["schema"],
                        expected_static["authority_id"],
                        expected_static["protocol_generation"],
                        expected_static["writer_id"],
                        int(file_enabled),
                    ),
                )
                connection.commit()
            except Exception as exc:
                _fail("kill_switch_invalid", "M3c kill-switch state could not be initialized")
            enabled = file_enabled
        else:
            observed = {
                "schema": str(row[0]),
                "authority_id": str(row[1]),
                "protocol_generation": str(row[2]),
                "writer_id": str(row[3]),
            }
            if observed != expected_static or int(row[4]) not in {0, 1}:
                _fail("kill_switch_invalid", "M3c kill switch database identity differs")
            enabled = bool(row[4])
        self._write_kill_switch_projection(enabled)

    def _write_kill_switch_projection(self, enabled: bool) -> None:
        _write_canonical(
            self._kill_switch_path,
            {
                "schema": M3C_KILL_SWITCH_SCHEMA,
                "authority_id": M3C_AUTHORITY_ID,
                "protocol_generation": M3C_PROTOCOL_GENERATION,
                "writer_id": M3C_WRITER_ID,
                "admission_enabled": bool(enabled),
            },
        )

    def _kill_switch(self) -> dict[str, Any]:
        try:
            row = self._store.control_connection.execute(
                "SELECT schema,authority_id,protocol_generation,writer_id,admission_enabled "
                "FROM m3c_kill_switch WHERE id=1"
            ).fetchone()
        except Exception as exc:
            _fail("kill_switch_invalid", "M3c kill-switch state could not be read")
        if row is None:
            _fail("kill_switch_invalid", "M3c kill-switch row is missing")
        current = {
            "schema": str(row[0]),
            "authority_id": str(row[1]),
            "protocol_generation": str(row[2]),
            "writer_id": str(row[3]),
            "admission_enabled": bool(row[4]),
        }
        if current["schema"] != M3C_KILL_SWITCH_SCHEMA or current["authority_id"] != M3C_AUTHORITY_ID or current["protocol_generation"] != M3C_PROTOCOL_GENERATION or current["writer_id"] != M3C_WRITER_ID:
            _fail("kill_switch_invalid", "M3c kill switch database identity differs")
        return current

    def _assert_intake_enabled(self) -> None:
        if not self._kill_switch()["admission_enabled"]:
            _fail("admission_disabled", "vNext admission is disabled by the kill switch")

    @property
    def admission_enabled(self) -> bool:
        with self._lock:
            return bool(self._kill_switch()["admission_enabled"])

    def set_intake_enabled(self, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            _fail("invalid_kill_switch", "admission kill switch requires a boolean")
        with self._lock:
            try:
                # The same SQLite write boundary used by canonical admission
                # linearizes this external control file across Native Host
                # processes; process-local locks alone are insufficient.
                with self._store.hold_write_lock():
                    current = self._kill_switch()
                    self._store.control_connection.execute(
                        "UPDATE m3c_kill_switch SET admission_enabled=? WHERE id=1",
                        (int(enabled),),
                    )
                    commit_control_write(self._store.control_connection)
                    self._write_kill_switch_projection(enabled)
            except (M3aError, ControlStoreError) as exc:
                _fail(exc.code, str(exc), details=getattr(exc, "details", None))

    def disable_intake(self) -> None:
        self.set_intake_enabled(False)

    def enable_intake(self) -> None:
        self.set_intake_enabled(True)

    def admit(
        self,
        request: ShadowSubmissionRequest,
        *,
        failpoint: Literal["before_commit", "after_commit"] | None = None,
    ) -> AdmissionReceipt:
        if not isinstance(request, ShadowSubmissionRequest):
            _fail("invalid_canonical_request", "canonical admission requires an M3a request")
        with self._lock:
            self._assert_intake_enabled()
            try:
                return self._store.admit(
                    request,
                    failpoint=failpoint,
                    admission_guard=self._assert_intake_enabled,
                )
            except M3aError as exc:
                _fail(exc.code, str(exc), details=exc.details)

    def lookup(self, submission_key: str, request_digest: str) -> AdmissionReceipt | None:
        _key(submission_key)
        _digest(request_digest, field="request_digest")
        try:
            receipt = self._store.lookup(submission_key)
        except M3aError as exc:
            _fail(exc.code, str(exc), details=exc.details)
        if receipt is not None and receipt.request_digest != request_digest:
            _fail(
                "submission_conflict",
                "canonical lookup found a different digest for the same key",
                details={"stored_digest": receipt.request_digest, "received_digest": request_digest},
            )
        return receipt

    def task(self, task_id: str) -> ShadowTask | None:
        """Read canonical M3 Task identity without creating another authority."""

        try:
            return self._store.task(task_id)
        except M3aError as exc:
            _fail(exc.code, str(exc), details=exc.details)

    def query(self, submission_key: str, request_digest: str) -> CanonicalAdmissionQuery | None:
        receipt = self.lookup(submission_key, request_digest)
        return CanonicalAdmissionQuery.from_receipt(receipt) if receipt is not None else None

    def counts(self) -> dict[str, int]:
        return self._store.counts()

    @contextmanager
    def hold_write_lock(self) -> Iterator[None]:
        with self._store.hold_write_lock():
            yield

    def close(self) -> None:
        self._store.close()


class CanonicalNativeAdmissionBridge:
    """Native transport boundary; it owns no admission state or writer."""

    def __init__(
        self,
        authority: CanonicalVNextAdmissionAuthority,
        *,
        protocol_generation: str = M3C_PROTOCOL_GENERATION,
    ) -> None:
        if not isinstance(authority, CanonicalVNextAdmissionAuthority):
            _fail("invalid_native_bridge", "Native transport requires canonical vNext authority")
        self.authority = authority
        self.protocol_generation = protocol_generation

    def handshake(self, client_protocol_generation: str) -> ProtocolCapability:
        if client_protocol_generation != self.protocol_generation or client_protocol_generation != M3C_PROTOCOL_GENERATION:
            _fail(
                "unsupported_protocol",
                "Browser and Native transport do not share the vNext protocol generation",
                details={"client": client_protocol_generation, "host": self.protocol_generation},
            )
        return ProtocolCapability(
            M3C_PROTOCOL_GENERATION,
            "devmaster.bdb.vnext.browser-transport",
            "com.bartosz.dev_bridge.vnext",
            canonical_lookup=True,
            production_acceptance=False,
        )

    def send(self, envelope: AdmissionEnvelope, *, lose_ack: bool = False) -> AdmissionReceipt:
        if not isinstance(envelope, AdmissionEnvelope):
            _fail("invalid_envelope", "Native transport requires an admission envelope")
        self.handshake(envelope.protocol_generation)
        receipt = self.authority.admit(envelope.request)
        if lose_ack:
            _fail("ack_lost", "Native transport lost the ACK after canonical admission")
        return receipt

    def lookup(self, submission_key: str, request_digest: str, *, protocol_generation: str) -> AdmissionReceipt | None:
        self.handshake(protocol_generation)
        return self.authority.lookup(submission_key, request_digest)


@dataclass
class CanonicalVNextAdmissionComposition:
    """Bounded internal composition; no production activation is performed."""

    authority: CanonicalVNextAdmissionAuthority
    bridge: CanonicalNativeAdmissionBridge
    outbox: BrowserAdmissionOutbox
    client: BrowserAdmissionClient

    def query(self, submission_key: str, request_digest: str) -> CanonicalAdmissionQuery | None:
        return self.authority.query(submission_key, request_digest)

    def close(self) -> None:
        self.outbox.close()
        self.authority.close()

    def __enter__(self) -> "CanonicalVNextAdmissionComposition":
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()


def _open_vnext_admission_composition(
    runtime_root: str | Path,
    *,
    legacy_root: str | Path,
    existing_outbox: bool = False,
) -> CanonicalVNextAdmissionComposition:
    """Internal constructor used by :class:`VNextCompositionRoot`."""

    authority = CanonicalVNextAdmissionAuthority.open(runtime_root, legacy_root=legacy_root)
    try:
        outbox = BrowserAdmissionOutbox(
            runtime_root,
            shadow=True,
            existing=existing_outbox,
            legacy_root=legacy_root,
        )
        bridge = CanonicalNativeAdmissionBridge(authority)
        client = BrowserAdmissionClient(outbox, bridge)
        return CanonicalVNextAdmissionComposition(authority, bridge, outbox, client)
    except Exception:
        authority.close()
        raise


def open_vnext_admission_composition(
    runtime_root: str | Path,
    *,
    legacy_root: str | Path,
    existing_outbox: bool = False,
) -> CanonicalVNextAdmissionComposition:
    """Test-fixture compatibility helper; production code enters via the root."""

    return _open_vnext_admission_composition(
        runtime_root,
        legacy_root=legacy_root,
        existing_outbox=existing_outbox,
    )


__all__ = [
    "AdmissionPath",
    "CanonicalAdmissionQuery",
    "CanonicalNativeAdmissionBridge",
    "CanonicalVNextAdmissionAuthority",
    "CanonicalVNextAdmissionComposition",
    "M3C_AUTHORITY_ID",
    "M3C_CANONICAL_ROLE",
    "M3C_CONTROL_SCHEMA",
    "M3C_CONTROL_SCHEMA_V1",
    "M3C_KILL_SWITCH_SCHEMA",
    "M3C_PROTOCOL_GENERATION",
    "M3C_SCHEMA",
    "M3C_WRITER_ID",
    "M3cError",
    "open_vnext_admission_composition",
    "postclosure_authority_map",
    "scan_supported_vnext_admission_paths",
]

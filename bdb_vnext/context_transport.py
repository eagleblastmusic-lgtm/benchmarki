"""Build-only exact typed-context transport for the BDB Next boundary."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from bdb_shared.evidence import canonical_json_bytes, semantic_digest
from bdb_vnext.composition import GENERATION_ID, PROTOCOL_GENERATION
from bdb_vnext.content_store import (
    ContentStoreError,
    DurableBindingStore,
    MAX_CONTEXT_FRAGMENT_BYTES,
    TypedContextFragment,
)
from bdb_vnext.repo_view import CommittedRepoView


TRANSPORT_SCHEMA = "bdb-vnext-transport-envelope-v1"
PROTOCOL_VERSION = 1
MESSAGE_KIND = "typed_context_fragment"
MAX_TRANSPORT_PAYLOAD_BYTES = MAX_CONTEXT_FRAGMENT_BYTES
MAX_TRANSPORT_ENVELOPE_BYTES = 2 * 1024 * 1024
BROWSER_PROVIDER_CONTRACT = "bdb-vnext-browser-transport-contract-v1"
NATIVE_PROVIDER_CONTRACT = "bdb-vnext-native-transport-contract-v1"
IMPLEMENTATION_IDENTITY_SCHEMA = "bdb-vnext-transport-implementation-identity-v1"
BROWSER_IMPLEMENTATION_REVISION = "bdb-vnext-browser-transport-implementation-r1"
NATIVE_IMPLEMENTATION_REVISION = "bdb-vnext-native-transport-implementation-r1"
_IMPLEMENTATION_MODULE = "bdb_vnext.context_transport"
_BROWSER_PROVIDER_ID = "devmaster.bdb.vnext.browser-transport"
_NATIVE_PROVIDER_ID = "devmaster.bdb.vnext.native-transport"


class TransportError(ValueError):
    """Typed fail-closed transport error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> None:
    raise TransportError(code, message)


def _message_id(core: dict[str, Any]) -> str:
    return f"sha256:{hashlib.sha256(canonical_json_bytes(core)).hexdigest()}"


def _provider_identity(
    provider_id: str,
    contract: str,
    implementation_module: str,
    implementation_qualname: str,
    implementation_revision: str,
) -> str:
    return semantic_digest(
        {
            "identity_schema": IMPLEMENTATION_IDENTITY_SCHEMA,
            "provider_id": provider_id,
            "provider_contract": contract,
            "provider_contract_version": PROTOCOL_VERSION,
            "protocol_generation": PROTOCOL_GENERATION,
            "implementation_module": implementation_module,
            "implementation_qualname": implementation_qualname,
            "implementation_revision": implementation_revision,
        }
    )


@dataclass(frozen=True)
class DecodedTransport:
    fragment: TypedContextFragment
    raw: bytes
    message_id: str


def encode_envelope(fragment: TypedContextFragment, raw: bytes) -> bytes:
    if not isinstance(fragment, TypedContextFragment):
        _fail("invalid_fragment", "transport encoding requires TypedContextFragment")
    if not isinstance(raw, bytes):
        _fail("invalid_payload", "transport payload must be bytes")
    if len(raw) > MAX_TRANSPORT_PAYLOAD_BYTES:
        _fail("payload_too_large", "transport payload exceeds the bounded limit")
    try:
        fragment.verify_payload(raw)
    except ContentStoreError as exc:
        raise TransportError(exc.code, str(exc)) from exc
    core: dict[str, Any] = {
        "schema": TRANSPORT_SCHEMA,
        "protocol_generation": PROTOCOL_GENERATION,
        "protocol_version": PROTOCOL_VERSION,
        "message_kind": MESSAGE_KIND,
        "fragment": fragment.as_dict(),
        "payload_length_bytes": len(raw),
        "payload_base64": base64.b64encode(raw).decode("ascii"),
    }
    document = {**core, "message_id": _message_id(core)}
    serialized = canonical_json_bytes(document)
    if len(serialized) > MAX_TRANSPORT_ENVELOPE_BYTES:
        _fail("envelope_too_large", "transport envelope exceeds the bounded limit")
    return serialized


def decode_envelope(payload: bytes) -> DecodedTransport:
    if not isinstance(payload, bytes):
        _fail("invalid_envelope", "transport envelope must be bytes")
    if len(payload) > MAX_TRANSPORT_ENVELOPE_BYTES:
        _fail("envelope_too_large", "transport envelope exceeds the bounded limit")
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TransportError("malformed_envelope", "transport envelope is not valid UTF-8 JSON") from exc
    if not isinstance(document, dict):
        _fail("malformed_envelope", "transport envelope must be a JSON object")
    required = {
        "schema",
        "protocol_generation",
        "protocol_version",
        "message_kind",
        "message_id",
        "fragment",
        "payload_length_bytes",
        "payload_base64",
    }
    if set(document) != required:
        _fail("malformed_envelope", "transport envelope has an unexpected field set")
    if canonical_json_bytes(document) != payload:
        _fail("malformed_envelope", "transport envelope is not canonical or contains trailing bytes")
    if document["schema"] != TRANSPORT_SCHEMA:
        _fail("unsupported_envelope_schema", "transport envelope schema is unsupported")
    if document["protocol_generation"] != PROTOCOL_GENERATION:
        _fail("unsupported_protocol_generation", "transport protocol generation is unsupported")
    if document["protocol_version"] != PROTOCOL_VERSION:
        _fail("unsupported_protocol_version", "transport protocol version is unsupported")
    if document["message_kind"] != MESSAGE_KIND:
        _fail("unknown_message_kind", "transport message kind is unsupported")
    message_id = document["message_id"]
    if (
        not isinstance(message_id, str)
        or len(message_id) != 71
        or not message_id.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in message_id[7:])
    ):
        _fail("message_integrity_failure", "transport message identity is malformed")
    length = document["payload_length_bytes"]
    if not isinstance(length, int) or isinstance(length, bool) or not 0 <= length <= MAX_TRANSPORT_PAYLOAD_BYTES:
        _fail("payload_too_large", "transport payload length is outside the bounded range")
    encoded = document["payload_base64"]
    if not isinstance(encoded, str):
        _fail("malformed_envelope", "transport payload encoding is malformed")
    try:
        raw = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise TransportError("malformed_payload_encoding", "transport payload is not strict base64") from exc
    if base64.b64encode(raw).decode("ascii") != encoded:
        _fail("malformed_payload_encoding", "transport payload base64 is not canonical")
    if len(raw) != length:
        _fail("payload_length_mismatch", "transport payload length does not match envelope metadata")
    core = {key: document[key] for key in required if key != "message_id"}
    if message_id != _message_id(core):
        _fail("message_integrity_failure", "transport message identity does not match exact envelope")
    try:
        fragment = TypedContextFragment.from_mapping(document["fragment"])
        fragment.verify_payload(raw)
    except ContentStoreError as exc:
        raise TransportError(exc.code, str(exc)) from exc
    return DecodedTransport(fragment, raw, message_id)


@dataclass(frozen=True)
class BrowserTransportProvider:
    """Read-only adapter that emits only an already accepted binding."""

    generation: str = GENERATION_ID
    provider_contract: str = BROWSER_PROVIDER_CONTRACT
    provider_contract_version: int = PROTOCOL_VERSION
    implementation_module: str = _IMPLEMENTATION_MODULE
    implementation_qualname: str = "BrowserTransportProvider"
    implementation_revision: str = BROWSER_IMPLEMENTATION_REVISION
    implementation_identity: str = _provider_identity(
        _BROWSER_PROVIDER_ID,
        BROWSER_PROVIDER_CONTRACT,
        _IMPLEMENTATION_MODULE,
        "BrowserTransportProvider",
        BROWSER_IMPLEMENTATION_REVISION,
    )

    def encode(
        self,
        bindings: DurableBindingStore,
        fragment: TypedContextFragment,
        *,
        expected_view: CommittedRepoView | None = None,
    ) -> bytes:
        accepted = bindings.resolve_accepted(fragment.fragment_id, expected_view=expected_view)
        if accepted.fragment != fragment:
            _fail("binding_integrity_failure", "transport fragment differs from accepted durable binding")
        return encode_envelope(fragment, accepted.raw)


@dataclass(frozen=True)
class NativeTransportProvider:
    """Read-only adapter that decodes exact envelopes and rejects unbound data."""

    generation: str = GENERATION_ID
    provider_contract: str = NATIVE_PROVIDER_CONTRACT
    provider_contract_version: int = PROTOCOL_VERSION
    implementation_module: str = _IMPLEMENTATION_MODULE
    implementation_qualname: str = "NativeTransportProvider"
    implementation_revision: str = NATIVE_IMPLEMENTATION_REVISION
    implementation_identity: str = _provider_identity(
        _NATIVE_PROVIDER_ID,
        NATIVE_PROVIDER_CONTRACT,
        _IMPLEMENTATION_MODULE,
        "NativeTransportProvider",
        NATIVE_IMPLEMENTATION_REVISION,
    )

    def decode(
        self,
        payload: bytes,
        *,
        bindings: DurableBindingStore | None = None,
        expected_view: CommittedRepoView | None = None,
    ) -> DecodedTransport:
        if bindings is None:
            _fail(
                "binding_store_required",
                "bound Native transport decoding requires a durable binding store",
            )
        decoded = decode_envelope(payload)
        accepted = bindings.resolve_accepted(
            decoded.fragment.fragment_id,
            expected_view=expected_view,
        )
        if accepted.fragment != decoded.fragment or accepted.raw != decoded.raw:
            _fail("binding_integrity_failure", "decoded envelope differs from accepted durable binding")
        return decoded


__all__ = [
    "BROWSER_PROVIDER_CONTRACT",
    "BROWSER_IMPLEMENTATION_REVISION",
    "BrowserTransportProvider",
    "DecodedTransport",
    "MESSAGE_KIND",
    "MAX_TRANSPORT_ENVELOPE_BYTES",
    "MAX_TRANSPORT_PAYLOAD_BYTES",
    "NATIVE_PROVIDER_CONTRACT",
    "NATIVE_IMPLEMENTATION_REVISION",
    "NativeTransportProvider",
    "PROTOCOL_VERSION",
    "TRANSPORT_SCHEMA",
    "TransportError",
    "decode_envelope",
    "encode_envelope",
]

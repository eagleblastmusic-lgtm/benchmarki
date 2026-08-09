from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from bdb_shared.evidence import canonical_json_bytes
from bdb_vnext.content_store import (
    ContentStoreError,
    DurableBindingStore,
    ImmutableContentStore,
    TypedContextFragment,
    make_content_ref,
)
from bdb_vnext.context_transport import (
    MAX_TRANSPORT_ENVELOPE_BYTES,
    MAX_TRANSPORT_PAYLOAD_BYTES,
    NativeTransportProvider,
    TransportError,
    decode_envelope,
    encode_envelope,
)
from bdb_vnext.repo_view import RepositoryResource


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _fixture(tmp_path: Path) -> tuple[Path, object, DurableBindingStore, TypedContextFragment, bytes]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.name", "Transport Test")
    _git(repo, "config", "user.email", "transport@example.invalid")
    (repo / "README.md").write_text("transport source\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "transport fixture")
    view = RepositoryResource.from_path(repo, repository_id="transport-fixture").resolve_committed(
        "refs/heads/main",
        observed_at="2026-08-09T00:00:00Z",
    )
    runtime = tmp_path / "runtime"
    content = ImmutableContentStore(runtime)
    bindings = DurableBindingStore(runtime, content_store=content)
    raw = b"exact transport bytes\n"
    ref = make_content_ref("text/plain", "bdb-vnext-context-text-v1", raw)
    content.publish(ref, raw)
    fragment = TypedContextFragment.create(
        view,
        ref,
        fragment_type="text/plain",
        fragment_schema="bdb-vnext-context-fragment-text-v1",
        payload_size_bytes=len(raw),
    )
    bindings.accept(fragment, view=view)
    return runtime, view, bindings, fragment, raw


def _document(envelope: bytes) -> dict[str, object]:
    return json.loads(envelope.decode("utf-8"))


def _resign(document: dict[str, object]) -> bytes:
    from bdb_vnext.context_transport import _message_id

    core = {key: value for key, value in document.items() if key != "message_id"}
    document["message_id"] = _message_id(core)
    return canonical_json_bytes(document)


def test_browser_to_native_exact_roundtrip_preserves_binding_and_bytes(tmp_path: Path) -> None:
    _runtime, view, bindings, fragment, raw = _fixture(tmp_path)
    from bdb_vnext.context_transport import BrowserTransportProvider

    browser = BrowserTransportProvider()
    native = NativeTransportProvider()
    envelope = browser.encode(bindings, fragment, expected_view=view)
    decoded = native.decode(envelope, bindings=bindings, expected_view=view)

    assert decoded.raw == raw
    assert decoded.fragment == fragment
    assert decoded.fragment.content_ref == fragment.content_ref
    assert decoded.fragment.repo_view == fragment.repo_view
    assert decoded.message_id.startswith("sha256:")
    bindings.close()


def test_transport_is_deterministic_and_rejects_trailing_payload(tmp_path: Path) -> None:
    _runtime, _view, bindings, fragment, raw = _fixture(tmp_path)
    first = encode_envelope(fragment, raw)
    second = encode_envelope(fragment, raw)
    assert first == second
    with pytest.raises(TransportError) as trailing:
        decode_envelope(first + b"x")
    assert trailing.value.code == "malformed_envelope"
    bindings.close()


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("protocol_generation", "bdb-legacy-protocol-v1", "unsupported_protocol_generation"),
        ("protocol_version", 99, "unsupported_protocol_version"),
        ("message_kind", "legacy_message", "unknown_message_kind"),
    ],
)
def test_transport_generation_version_and_kind_fail_closed(
    tmp_path: Path,
    field: str,
    value: object,
    code: str,
) -> None:
    _runtime, _view, bindings, fragment, raw = _fixture(tmp_path)
    document = _document(encode_envelope(fragment, raw))
    document[field] = value
    with pytest.raises(TransportError) as failure:
        decode_envelope(canonical_json_bytes(document))
    assert failure.value.code == code
    bindings.close()


def test_transport_malformed_truncated_and_extra_fields_fail_closed(tmp_path: Path) -> None:
    _runtime, _view, bindings, fragment, raw = _fixture(tmp_path)
    envelope = encode_envelope(fragment, raw)
    with pytest.raises(TransportError) as truncated:
        decode_envelope(envelope[:-1])
    assert truncated.value.code == "malformed_envelope"
    with pytest.raises(TransportError) as invalid_utf8:
        decode_envelope(b"\xff")
    assert invalid_utf8.value.code == "malformed_envelope"

    extra = _document(envelope)
    extra["unexpected"] = True
    with pytest.raises(TransportError) as unknown:
        decode_envelope(canonical_json_bytes(extra))
    assert unknown.value.code == "malformed_envelope"
    bindings.close()


def test_transport_payload_bounds_are_explicit(tmp_path: Path) -> None:
    _runtime, view, bindings, _fragment, _raw = _fixture(tmp_path)
    boundary = b"x" * MAX_TRANSPORT_PAYLOAD_BYTES
    ref = make_content_ref("application/octet-stream", "bdb-vnext-context-boundary-v1", boundary)
    fragment = TypedContextFragment.create(
        view,
        ref,
        fragment_type="application/octet-stream",
        fragment_schema="bdb-vnext-context-fragment-bytes-v1",
        payload_size_bytes=len(boundary),
    )
    assert len(encode_envelope(fragment, boundary)) <= MAX_TRANSPORT_ENVELOPE_BYTES
    with pytest.raises(TransportError) as oversized:
        encode_envelope(fragment, boundary + b"x")
    assert oversized.value.code == "payload_too_large"
    with pytest.raises(TransportError) as envelope_oversized:
        decode_envelope(b"{" + b"x" * MAX_TRANSPORT_ENVELOPE_BYTES)
    assert envelope_oversized.value.code == "envelope_too_large"
    bindings.close()


def test_transport_wrong_fragment_identity_and_digest_fail_closed(tmp_path: Path) -> None:
    _runtime, view, bindings, fragment, raw = _fixture(tmp_path)
    document = _document(encode_envelope(fragment, raw))
    nested = document["fragment"]
    assert isinstance(nested, dict)
    nested["fragment_type"] = "legacy/plain"
    with pytest.raises(TransportError) as wrong_type:
        decode_envelope(_resign(document))
    assert wrong_type.value.code == "fragment_integrity_failure"

    document = _document(encode_envelope(fragment, raw))
    nested = document["fragment"]
    assert isinstance(nested, dict)
    nested["fragment_schema"] = "legacy-fragment-v1"
    with pytest.raises(TransportError) as wrong_schema:
        decode_envelope(_resign(document))
    assert wrong_schema.value.code == "fragment_integrity_failure"

    document = _document(encode_envelope(fragment, raw))
    nested = document["fragment"]
    assert isinstance(nested, dict)
    content_ref = nested["content_ref"]
    assert isinstance(content_ref, dict)
    content_ref["raw_digest"] = "sha256:" + "0" * 64
    with pytest.raises(TransportError) as wrong_raw:
        decode_envelope(_resign(document))
    assert wrong_raw.value.code == "fragment_integrity_failure"

    document = _document(encode_envelope(fragment, raw))
    nested = document["fragment"]
    assert isinstance(nested, dict)
    content_ref = nested["content_ref"]
    assert isinstance(content_ref, dict)
    content_ref["semantic_digest"] = "sha256:" + "0" * 64
    with pytest.raises(TransportError) as wrong_semantic:
        decode_envelope(_resign(document))
    assert wrong_semantic.value.code == "fragment_integrity_failure"
    bindings.close()


def test_transport_repo_view_mismatch_and_unbound_fragment_fail_closed(tmp_path: Path) -> None:
    _runtime, view, bindings, fragment, raw = _fixture(tmp_path)
    envelope = encode_envelope(fragment, raw)
    other_repo = tmp_path / "other"
    other_repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(other_repo)], check=True)
    _git(other_repo, "config", "user.name", "Other")
    _git(other_repo, "config", "user.email", "other@example.invalid")
    (other_repo / "README.md").write_text("other\n", encoding="utf-8")
    _git(other_repo, "add", "README.md")
    _git(other_repo, "commit", "-qm", "other")
    other_view = RepositoryResource.from_path(other_repo, repository_id="other").resolve_committed(
        "refs/heads/main",
        observed_at="2026-08-09T00:00:00Z",
    )
    native = NativeTransportProvider()
    with pytest.raises(ContentStoreError) as mismatch:
        native.decode(envelope, bindings=bindings, expected_view=other_view)
    assert mismatch.value.code == "repo_view_binding_mismatch"

    bindings.close()
    with pytest.raises(ContentStoreError) as missing:
        DurableBindingStore(tmp_path / "unbound").resolve_accepted(fragment.fragment_id, expected_view=view)
    assert missing.value.code == "binding_missing"


def test_transport_missing_or_corrupt_object_fails_closed(tmp_path: Path) -> None:
    _runtime, _view, bindings, fragment, raw = _fixture(tmp_path)
    envelope = encode_envelope(fragment, raw)
    object_path = bindings.content_store.object_path(fragment.content_ref)
    object_path.write_bytes(b"wrong")
    with pytest.raises(ContentStoreError) as corrupt:
        NativeTransportProvider().decode(envelope, bindings=bindings)
    assert corrupt.value.code == "raw_integrity_failure"
    object_path.unlink()
    with pytest.raises(ContentStoreError) as missing:
        NativeTransportProvider().decode(envelope, bindings=bindings)
    assert missing.value.code == "raw_object_missing"
    bindings.close()

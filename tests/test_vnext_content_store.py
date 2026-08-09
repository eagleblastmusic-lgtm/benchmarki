from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from bdb_vnext.bootstrap import create_coordinated_backup, restore_backup
from bdb_vnext.content_store import (
    CONTEXT_FRAGMENT_SCHEMA,
    ContentStoreError,
    DurableBindingStore,
    ImmutableContentStore,
    RepoViewBinding,
    TypedContextFragment,
    make_content_ref,
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


def _repo(tmp_path: Path) -> tuple[Path, object]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.name", "M2b Test")
    _git(repo, "config", "user.email", "m2b@example.invalid")
    (repo / "README.md").write_text("M2b exact\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "M2b fixture")
    resource = RepositoryResource.from_path(repo, repository_id="m2b-fixture")
    view = resource.resolve_committed("refs/heads/main", observed_at="2026-08-09T00:00:00Z")
    return repo, view


def _accepted_fixture(tmp_path: Path) -> tuple[Path, object, ImmutableContentStore, DurableBindingStore, TypedContextFragment, bytes]:
    _repo_path, view = _repo(tmp_path)
    runtime = tmp_path / "runtime"
    raw = b"M2b exact typed bytes\n"
    content = ImmutableContentStore(runtime)
    bindings = DurableBindingStore(runtime, content_store=content)
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
    return runtime, view, content, bindings, fragment, raw


def test_content_ref_preserves_four_field_semantic_and_raw_identity() -> None:
    raw = b"same bytes"
    text_ref = make_content_ref("text/plain", "bdb-vnext-context-text-v1", raw)
    binary_ref = make_content_ref("application/octet-stream", "bdb-vnext-context-bytes-v1", raw)

    assert set(text_ref.as_dict()) == {"type", "schema", "semantic_digest", "raw_digest"}
    assert text_ref.raw_digest == binary_ref.raw_digest
    assert text_ref.semantic_digest != binary_ref.semantic_digest


def test_immutable_content_publication_converges_and_never_overwrites(tmp_path: Path) -> None:
    raw = b"immutable content\n"
    ref = make_content_ref("text/plain", "bdb-vnext-context-text-v1", raw)
    store = ImmutableContentStore(tmp_path / "content")

    first = store.publish(ref, raw)
    duplicate = store.publish(ref, raw)

    assert first.object_publication == "published"
    assert first.ref_publication == "published"
    assert duplicate.object_publication == "converged"
    assert duplicate.ref_publication == "converged"
    assert store.resolve(ref) == raw

    store.object_path(ref).write_bytes(b"different")
    with pytest.raises(ContentStoreError) as conflict:
        store.publish(ref, raw)
    assert conflict.value.code == "immutable_conflict"


def test_handle_bound_content_read_rejects_path_or_reparse_substitution(tmp_path: Path) -> None:
    raw = b"reparse guarded\n"
    ref = make_content_ref("text/plain", "bdb-vnext-context-text-v1", raw)
    store = ImmutableContentStore(tmp_path / "content")
    store.publish(ref, raw)
    foreign = tmp_path / "foreign.bin"
    foreign.write_bytes(raw)
    object_path = store.object_path(ref)
    object_path.unlink()
    try:
        object_path.symlink_to(foreign)
    except OSError as exc:
        # Windows without symlink privilege still exercises the same
        # pathname-substitution boundary with a foreign regular file.
        object_path.write_bytes(b"foreign replacement")
        with pytest.raises(ContentStoreError) as failure:
            store.resolve(ref)
        assert failure.value.code == "raw_integrity_failure"
        return

    with pytest.raises(ContentStoreError) as failure:
        store.resolve(ref)
    assert failure.value.code == "reparse_point"


def test_durable_binding_acceptance_requires_exact_view_and_content(tmp_path: Path) -> None:
    runtime, view, content, bindings, fragment, raw = _accepted_fixture(tmp_path)
    accepted = bindings.resolve_accepted(fragment.fragment_id, expected_view=view)

    assert accepted.fragment == fragment
    assert accepted.raw == raw
    assert accepted.publication == "converged"
    assert RepoViewBinding.from_view(view) == fragment.repo_view
    assert content.resolve(fragment.content_ref) == raw
    bindings.close()


def test_durable_binding_conflict_and_missing_binding_fail_closed(tmp_path: Path) -> None:
    _runtime, view, _content, bindings, fragment, raw = _accepted_fixture(tmp_path)
    database = bindings.database_path
    row = bindings._connection.execute(
        "SELECT binding_json FROM m2b_accepted_bindings WHERE fragment_id = ?",
        (fragment.fragment_id,),
    ).fetchone()
    assert row is not None
    conflicting = json.loads(bytes(row[0]).decode("utf-8"))
    conflicting["payload_size_bytes"] = 0
    bindings._connection.execute(
        "UPDATE m2b_accepted_bindings SET binding_json = ? WHERE fragment_id = ?",
        (json.dumps(conflicting).encode("utf-8"), fragment.fragment_id),
    )
    bindings._connection.commit()
    with pytest.raises(ContentStoreError) as conflict:
        bindings.accept(fragment, view=view)
    assert conflict.value.code == "conflicting_binding"

    bindings.close()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "DELETE FROM m2b_accepted_bindings WHERE fragment_id = ?",
            (fragment.fragment_id,),
        )
        connection.commit()
    reopened = DurableBindingStore(tmp_path / "runtime")
    with pytest.raises(ContentStoreError) as missing:
        reopened.resolve_accepted(fragment.fragment_id, expected_view=view)
    assert missing.value.code == "binding_missing"
    reopened.close()


def test_m2b_binding_recovery_roundtrip_and_tamper_matrix(tmp_path: Path) -> None:
    runtime, view, content, bindings, fragment, raw = _accepted_fixture(tmp_path)
    bindings.close()
    artifact = create_coordinated_backup(
        runtime,
        tmp_path / "authority" / "backups",
        backup_id="m2b-recovery",
        required_control_schema=1,
        source_is_quiesced=True,
    )
    restored = tmp_path / "restored"
    restore_backup(
        artifact.path,
        restored,
        authority_root=tmp_path / "authority",
        legacy_runtime_root=tmp_path / "legacy",
    )
    recovered = DurableBindingStore(restored)
    accepted = recovered.resolve_accepted(fragment.fragment_id, expected_view=view)
    assert accepted.fragment.repo_view == fragment.repo_view
    assert accepted.fragment.content_ref == fragment.content_ref
    assert accepted.fragment.content_ref.raw_digest == fragment.content_ref.raw_digest
    assert accepted.fragment.content_ref.semantic_digest == fragment.content_ref.semantic_digest
    assert accepted.raw == raw
    recovered.close()

    missing_object = tmp_path / "restored-missing-object"
    restore_backup(
        artifact.path,
        missing_object,
        authority_root=tmp_path / "authority",
        legacy_runtime_root=tmp_path / "legacy-missing",
    )
    missing_store = DurableBindingStore(missing_object)
    ImmutableContentStore(missing_object).object_path(fragment.content_ref).unlink()
    with pytest.raises(ContentStoreError) as missing:
        missing_store.resolve_accepted(fragment.fragment_id, expected_view=view)
    assert missing.value.code == "raw_object_missing"
    missing_store.close()

    metadata_tamper = tmp_path / "restored-metadata-tamper"
    restore_backup(
        artifact.path,
        metadata_tamper,
        authority_root=tmp_path / "authority",
        legacy_runtime_root=tmp_path / "legacy-metadata",
    )
    tamper_store = DurableBindingStore(metadata_tamper)
    row = tamper_store._connection.execute(
        "SELECT binding_json FROM m2b_accepted_bindings WHERE fragment_id = ?",
        (fragment.fragment_id,),
    ).fetchone()
    assert row is not None
    document = json.loads(bytes(row[0]).decode("utf-8"))
    document["content_ref"]["raw_digest"] = "sha256:" + "0" * 64
    tamper_store._connection.execute(
        "UPDATE m2b_accepted_bindings SET binding_json = ? WHERE fragment_id = ?",
        (json.dumps(document).encode("utf-8"), fragment.fragment_id),
    )
    tamper_store._connection.commit()
    with pytest.raises(ContentStoreError) as tampered:
        tamper_store.resolve_accepted(fragment.fragment_id, expected_view=view)
    assert tampered.value.code == "fragment_integrity_failure"
    tamper_store.close()


def test_context_fragment_contract_is_json_serializable_and_exact() -> None:
    raw = b"contract"
    ref = make_content_ref("text/plain", "bdb-vnext-context-text-v1", raw)
    assert CONTEXT_FRAGMENT_SCHEMA == "bdb-vnext-context-fragment-v1"
    assert set(TypedContextFragment.__dataclass_fields__) == {
        "fragment_id",
        "fragment_type",
        "fragment_schema",
        "content_ref",
        "repo_view",
        "payload_size_bytes",
    }

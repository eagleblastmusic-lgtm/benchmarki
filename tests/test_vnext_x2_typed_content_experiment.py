from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from bdb_vnext.x2_typed_content_experiment import (
    X2_SCHEMA,
    ContentRef,
    TypedContentStore,
    X2ExperimentError,
    make_content_ref,
    run_experiment,
    validate_content_relative_path,
)


def _fixture() -> tuple[ContentRef, bytes]:
    raw = b"typed content fixture\n"
    return make_content_ref("text/plain", "x2-text-v1", raw), raw


def test_content_ref_separates_raw_and_semantic_identity() -> None:
    ref, raw = _fixture()
    binary = make_content_ref("application/octet-stream", "x2-bytes-v1", raw)

    assert ref.raw_digest == binary.raw_digest
    assert ref.semantic_digest != binary.semantic_digest
    assert set(ref.as_dict()) == {"type", "schema", "semantic_digest", "raw_digest"}


def test_store_requires_exact_ref_and_rejects_path_escape(tmp_path: Path) -> None:
    ref, raw = _fixture()
    store = TypedContentStore(tmp_path / "store")
    store.commit(ref, raw)
    wrong_type = ContentRef("application/octet-stream", ref.schema, ref.semantic_digest, ref.raw_digest)
    with pytest.raises(X2ExperimentError) as type_error:
        store.resolve(wrong_type)
    assert type_error.value.code == "semantic_type_schema_mismatch"

    with pytest.raises(X2ExperimentError) as malformed:
        ContentRef.from_mapping({"type": ref.type})
    assert malformed.value.code == "malformed_content_ref"

    with pytest.raises(X2ExperimentError) as escaped:
        validate_content_relative_path("../outside.bin")
    assert escaped.value.code == "path_escape"


def test_complete_x2_capsule_is_pass_with_exact_fault_matrix(tmp_path: Path) -> None:
    evidence = run_experiment(tmp_path / "capsule")

    assert evidence["schema"] == X2_SCHEMA
    assert evidence["status"] == "PASS"
    assert evidence["hypotheses"] == {
        "H1": "PASS",
        "H2": "PASS",
        "H3": "PASS",
        "H4": "PASS",
        "H5": "PASS",
        "H6": "PASS",
    }
    assert evidence["content_ref_contract"]["raw_path_authority"] is False
    assert evidence["normal_commit"] == {
        "first_publication": "published",
        "duplicate_publication": "converged",
        "resolved_exactly": True,
    }
    assert {
        name: result["failure_code"]
        for name, result in evidence["publication_faults"].items()
    } == {
        "before_temp_write": "publication_failed_before_temp",
        "during_temp_write": "publication_failed_during_temp",
        "after_temp_write_before_publish": "publication_failed_before_publish",
        "after_object_publish_before_ref": "publication_failed_before_ref",
        "after_ref_publish": "publication_ack_lost",
    }
    assert evidence["publication_faults"]["after_ref_publish"]["resolve"] == "exact_bytes"
    assert {
        name: code for name, code in evidence["integrity_faults"].items()
    } == {
        "missing_committed_object": "raw_object_missing",
        "truncated_committed_object": "raw_integrity_failure",
        "mutated_committed_object": "raw_integrity_failure",
        "wrong_raw_digest": "content_ref_identity_mismatch",
        "wrong_semantic_type": "semantic_type_schema_mismatch",
        "wrong_schema": "semantic_type_schema_mismatch",
        "malformed_content_ref": "malformed_content_ref",
        "path_escape": "path_escape",
    }
    assert evidence["semantic_and_concurrency"]["same_raw_different_semantic_domain"] == {
        "raw_digest_equal": True,
        "semantic_digest_equal": False,
        "both_exactly_resolved": True,
    }
    assert evidence["semantic_and_concurrency"]["independent_instances"]["publication_results"] == [
        "converged",
        "published",
    ]
    assert evidence["semantic_and_concurrency"]["independent_instances"]["resolved_exactly"] is True
    assert evidence["semantic_and_concurrency"]["independent_instances"]["orphan_temp"] == []
    assert evidence["semantic_and_concurrency"]["independent_subprocesses"]["publication_results"] == [
        "converged",
        "published",
    ]
    assert evidence["semantic_and_concurrency"]["independent_subprocesses"]["resolved_exactly"] is True
    assert evidence["semantic_and_concurrency"]["independent_subprocesses"]["orphan_temp"] == []
    assert evidence["semantic_and_concurrency"]["independent_subprocesses"]["primitive"] == (
        "atomic NTFS os.link(temp, target) publish-if-absent"
    )
    assert sorted(evidence["semantic_and_concurrency"]["conflicting_writer"]["results"]) == [
        "committed",
        "content_ref_integrity_failure",
    ]
    assert evidence["existing_target_cases"] == {
        "different_bytes": {
            "failure_code": "immutable_object_conflict",
            "unchanged": True,
        },
        "wrong_file_type": {
            "failure_code": "unexpected_file_type",
            "unchanged": True,
        },
        "reparse_target": {
            "failure_code": "reparse_point",
            "unchanged": True,
        },
    }
    assert evidence["orphan_and_foreign"]["orphan"]["resolve_blocked"] is True
    assert evidence["orphan_and_foreign"]["foreign_file"]["resolve_authority_unchanged"] is True
    assert evidence["orphan_and_foreign"]["symlink_reparse"]["available"] is True
    assert evidence["orphan_and_foreign"]["symlink_reparse"]["kind"] in {"symlink", "junction"}
    assert evidence["orphan_and_foreign"]["symlink_reparse"]["failure_code"] == "reparse_point"
    assert evidence["m1b_backup_restore"]["valid_restore"] == {
        "verified": True,
        "exact_ref": True,
        "exact_raw_digest": True,
        "exact_semantic_digest": True,
    }
    assert evidence["m1b_backup_restore"]["tamper_failure_codes"] == {
        "missing_content": "backup_integrity_failure",
        "truncated_content": "backup_integrity_failure",
        "corrupt_content": "backup_integrity_failure",
        "foreign_file": "backup_integrity_failure",
    }
    assert evidence["m1b_backup_restore"]["restore_integrity_failure_code"] == "restore_integrity_failure"
    assert evidence["authority"] == {
        "second_authority": False,
        "production_store": False,
        "runtime_activation": False,
        "legacy_touched": False,
    }
    json.dumps(evidence, ensure_ascii=False, sort_keys=True)


def test_x2_module_has_no_legacy_or_activation_import_dependency() -> None:
    root = Path(__file__).resolve().parents[1]
    source_path = root / "bdb_vnext" / "x2_typed_content_experiment.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert "bdb_bridge" not in imported
    assert "bdb_legacy" not in imported
    assert "execute_bootstrap" not in imported

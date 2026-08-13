from __future__ import annotations

import base64
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from bdb_shared.evidence import semantic_digest
from bdb_vnext.candidate import CandidateStore
from bdb_vnext.engineering_loop import (
    EditBatch,
    EditorPort,
    EngineeringLoop,
    EngineeringLoopError,
    ValidationCommand,
    ValidationPolicy,
    ValidationRunner,
)
from bdb_vnext.m3a_submission import ShadowSubmissionRequest
from bdb_vnext.m3c_admission import open_vnext_admission_composition
from bdb_vnext.m4c_evidence import EvidenceStore
from bdb_vnext.m4a_work_kernel import WorkKernelStore
from bdb_vnext.n4_publication import PublicationStore
from bdb_vnext.repo_view import RepositoryResource


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _subject(tmp_path: Path) -> Path:
    repo = tmp_path / "subject"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "p1@example.invalid")
    _git(repo, "config", "user.name", "P1 Test")
    (repo / "target.txt").write_text("TODO\n", encoding="utf-8")
    (repo / "checker.py").write_text(
        "from pathlib import Path\n"
        "raise SystemExit(0 if Path('target.txt').read_text(encoding='utf-8') == 'PASS\\n' else 1)\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return repo


@contextmanager
def _stack(tmp_path: Path):
    runtime = tmp_path / "runtime"
    legacy = tmp_path / "legacy"
    subject = _subject(tmp_path)
    admission = open_vnext_admission_composition(runtime, legacy_root=legacy)
    receipt = admission.authority.admit(
        ShadowSubmissionRequest(
            submission_key="browser:p1",
            intent_revision="r1",
            intent={"operation": "engineering-loop"},
            conversation_binding={"conversation_id": "p1"},
            consumer_binding={"consumer_id": "p1", "kind": "browser"},
        )
    )
    kernel = WorkKernelStore.open(runtime, task_authority=admission.authority, legacy_root=legacy, clock=lambda: 100.0)
    store = CandidateStore(runtime, work_kernel=kernel)
    evidence = EvidenceStore(runtime, content_store=store.content_store, candidate_store=store)
    publication = PublicationStore(runtime, content_store=store.content_store, task_authority=admission.authority, work_kernel=kernel, candidate_store=store, evidence_store=evidence)
    view = RepositoryResource.from_path(subject, repository_id="p1-subject").resolve_committed("HEAD")
    work = kernel.create_work_item("work:p1", receipt.task_id)
    lease = kernel.acquire_lease(work.work_id, "lease:p1", "worker:p1")
    kernel.start_run(work.work_id, "run:p1", lease.lease_id, lease.fence, work.state_version)
    try:
        yield subject, view, receipt, kernel, store, evidence, publication, work.work_id, lease
    finally:
        publication.close()
        evidence.close()
        store.close()
        kernel.close()
        admission.close()


def _artifact(view_id: str, tree_digest: str, *, candidate_id: str, task_id: str, work_id: str, run_id: str, lease_id: str, fence: int, generation: str, operations: list[dict[str, object]]) -> EditBatch:
    return EditBatch.from_mapping(
        {
            "schema": "bdb-vnext-edit-v1",
            "base_view_id": view_id,
            "expected_tree_digest": tree_digest,
            "task_id": task_id,
            "work_id": work_id,
            "run_id": run_id,
            "lease_id": lease_id,
            "fence": fence,
            "candidate_id": candidate_id,
            "workspace_generation": generation,
            "operations": operations,
            "budget": {"max_operations": 8, "max_bytes": 1024},
        }
    )


def test_iterative_model_edit_validation_candidate_evidence_publication_resume(tmp_path: Path) -> None:
    with _stack(tmp_path) as (subject, view, receipt, kernel, store, evidence, publication, work_id, lease):
        candidate_id = "candidate:p1"
        workspace = store.create_workspace(candidate_id=candidate_id, base_view=view)
        base_tree = store._tree_digest(store._base_entries(view))
        command = ValidationCommand("fixture-checker", "v1", (sys.executable, "checker.py"))
        runner = ValidationRunner(ValidationPolicy(allowed_argv=(command.argv,)))
        loop = EngineeringLoop(EditorPort(store, evidence_store=evidence), runner, evidence_store=evidence, publication_store=publication)

        first = _artifact(view.view_id, base_tree, candidate_id=candidate_id, task_id=receipt.task_id, work_id=work_id, run_id="run:p1", lease_id=lease.lease_id, fence=lease.fence, generation=store.generation, operations=[{"operation": "MODIFY", "path": "target.txt", "content_b64": base64.b64encode(b"FAIL\n").decode("ascii")}])
        failed = loop.iteration(first, base_view=view, workspace=workspace, command=command)
        assert failed.candidate.state == "OBSERVED"
        assert failed.validation.status == "FAIL"
        assert failed.validation.evidence_id is not None

        second_tree = store._tree_digest(store._workspace_entries(workspace, object_format=view.object_format))
        second = _artifact(view.view_id, second_tree, candidate_id=candidate_id, task_id=receipt.task_id, work_id=work_id, run_id="run:p1", lease_id=lease.lease_id, fence=lease.fence, generation=store.generation, operations=[{"operation": "MODIFY", "path": "target.txt", "content_b64": base64.b64encode(b"PASS\n").decode("ascii")}])
        passed = loop.iteration(second, base_view=view, workspace=workspace, command=command)
        assert passed.validation.status == "PASS"
        assert store._connection.execute("SELECT COUNT(*) FROM m4b_candidate_effects").fetchone()[0] == 1
        assert store._connection.execute("SELECT COUNT(*) FROM p1_edit_batches").fetchone()[0] == 2

        final = loop.finalize(
            second,
            base_view=view,
            workspace=workspace,
            command=command,
            publication={
                "request_id": "publication:p1",
                "intent_revision_id": receipt.intent_revision_id,
                "consumer_id": "p1",
                "consumer_kind": "BROWSER",
                "conversation_id": "p1",
            },
        )
        assert final.candidate.state == "SEALED"
        assert final.validation.status == "PASS"
        assert final.evaluation is not None and final.evaluation.result == "PASS"
        assert final.publication is not None
        publication.bind_consumer(publication_id=final.publication.publication_id, consumer_id="p1-new", consumer_kind="BROWSER", conversation_id="p1-new")
        capsule = publication.create_resume_capsule(publication_id=final.publication.publication_id, source_consumer_id="p1", target_consumer_id="p1-new", payload={"task_id": receipt.task_id, "publication_id": final.publication.publication_id})
        assert publication.resume(capsule.capsule_id) is not None
        assert final.candidate_view.read_bytes("target.txt") == b"PASS\n"


def test_edit_operations_create_delete_rename_have_exact_tree_proof(tmp_path: Path) -> None:
    with _stack(tmp_path) as (subject, view, receipt, kernel, store, evidence, _publication, work_id, lease):
        (subject / "delete.txt").write_text("delete\n", encoding="utf-8")
        (subject / "rename.txt").write_text("rename\n", encoding="utf-8")
        _git(subject, "add", ".")
        _git(subject, "commit", "-qm", "operation fixture")
        view = RepositoryResource.from_path(subject, repository_id="p1-subject").resolve_committed("HEAD")
        candidate_id = "candidate:p1:ops"
        workspace = store.create_workspace(candidate_id=candidate_id, base_view=view)
        tree = store._tree_digest(store._base_entries(view))
        artifact = _artifact(view.view_id, tree, candidate_id=candidate_id, task_id=receipt.task_id, work_id=work_id, run_id="run:p1", lease_id=lease.lease_id, fence=lease.fence, generation=store.generation, operations=[
            {"operation": "CREATE", "path": "created.txt", "content_b64": base64.b64encode(b"created\n").decode("ascii")},
            {"operation": "DELETE", "path": "delete.txt"},
            {"operation": "RENAME", "path": "renamed.txt", "source_path": "rename.txt"},
        ])
        editor = EditorPort(store, evidence_store=evidence)
        prepared = editor.prepare_batch(artifact, base_view=view, workspace=workspace)
        observed = editor.apply_batch(artifact)
        assert prepared.state == "PREPARED"
        assert observed.state == "OBSERVED"
        sealed, candidate = store.seal(candidate_id, base_view=view)
        paths = {entry.path for entry in candidate.list_entries()}
        assert "created.txt" in paths and "delete.txt" not in paths and "renamed.txt" in paths and "rename.txt" not in paths
        assert candidate.read_bytes("renamed.txt") == b"rename\n"
        assert sealed.planned_tree_digest == sealed.observed_tree_digest


def test_edit_artifact_and_validation_boundaries_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(EngineeringLoopError) as caught:
        EditBatch.from_mapping({"schema": "bdb-vnext-edit-v1", "base_view_id": "x", "expected_tree_digest": "sha256:" + "0" * 64, "task_id": "t", "work_id": "w", "run_id": "r", "lease_id": "l", "fence": 1, "candidate_id": "c", "workspace_generation": "g", "operations": [{"operation": "MODIFY", "path": "../escape", "content_b64": "eA=="}], "budget": {}})
    assert caught.value.code == "unsafe_edit_path"

    script = tmp_path / "checker.py"
    script.write_text("raise SystemExit(0)\n", encoding="utf-8")
    command = ValidationCommand("checker", "v1", (sys.executable, "checker.py"))
    runner = ValidationRunner(ValidationPolicy(allowed_argv=((sys.executable, "other.py"),)))
    with pytest.raises(EngineeringLoopError) as caught:
        runner.run(command, tmp_path)
    assert caught.value.code == "validation_command_not_allowed"


def test_stale_tree_and_identity_are_rejected_without_mutation(tmp_path: Path) -> None:
    with _stack(tmp_path) as (_subject, view, receipt, _kernel, store, evidence, _publication, work_id, lease):
        candidate_id = "candidate:p1:stale"
        workspace = store.create_workspace(candidate_id=candidate_id, base_view=view)
        tree = store._tree_digest(store._base_entries(view))
        batch = _artifact(view.view_id, tree, candidate_id=candidate_id, task_id=receipt.task_id, work_id=work_id, run_id="run:p1", lease_id=lease.lease_id, fence=lease.fence, generation=store.generation, operations=[{"operation": "MODIFY", "path": "target.txt", "content_b64": base64.b64encode(b"one\n").decode("ascii")}])
        editor = EditorPort(store, evidence_store=evidence)
        editor.prepare_batch(batch, base_view=view, workspace=workspace)
        assert editor.prepare_batch(batch, base_view=view, workspace=workspace).candidate_id == candidate_id
        (workspace / "target.txt").write_text("foreign\n", encoding="utf-8")
        with pytest.raises(EngineeringLoopError) as caught:
            editor.prepare_batch(batch, base_view=view, workspace=workspace)
        assert caught.value.code == "tree_precondition_mismatch"

        wrong = _artifact("sha256:" + "f" * 64, tree, candidate_id="candidate:p1:wrong", task_id=receipt.task_id, work_id=work_id, run_id="run:p1", lease_id=lease.lease_id, fence=lease.fence, generation=store.generation, operations=[{"operation": "MODIFY", "path": "target.txt", "content_b64": base64.b64encode(b"two\n").decode("ascii")}])
        with pytest.raises(EngineeringLoopError) as caught:
            editor.prepare_batch(wrong, base_view=view, workspace=workspace)
        assert caught.value.code == "repo_view_mismatch"


def test_editor_port_is_composed_by_the_single_vnext_root(tmp_path: Path) -> None:
    # Use the repository's normal manifest helper so all frozen provider
    # identity checks remain exercised by this assertion.
    from tests.test_vnext_n1_control_plane import _manifest
    root, _runtime, _legacy = _manifest(tmp_path)
    with root.open_control_plane() as plane:
        assert plane.editor is not None
        assert plane.editor.candidate_store is plane.candidate_store
        assert plane.editor.evidence_store is plane.evidence_store


def test_recovery_observes_before_any_retry(tmp_path: Path) -> None:
    with _stack(tmp_path) as (_subject, view, receipt, _kernel, store, evidence, _publication, work_id, lease):
        candidate_id = "candidate:p1:recover"
        workspace = store.create_workspace(candidate_id=candidate_id, base_view=view)
        tree = store._tree_digest(store._base_entries(view))
        batch = _artifact(view.view_id, tree, candidate_id=candidate_id, task_id=receipt.task_id, work_id=work_id, run_id="run:p1", lease_id=lease.lease_id, fence=lease.fence, generation=store.generation, operations=[{"operation": "MODIFY", "path": "target.txt", "content_b64": base64.b64encode(b"recover\n").decode("ascii")}])
        editor = EditorPort(store, evidence_store=evidence)
        prepared = editor.prepare_batch(batch, base_view=view, workspace=workspace)
        store.mark_possible(prepared.candidate_id)
        recovered = editor.recover_candidate(candidate_id, base_view=view)
        assert recovered.state == "PREPARED"
        assert (workspace / "target.txt").read_text(encoding="utf-8") == "TODO\n"

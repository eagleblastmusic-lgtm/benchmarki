from __future__ import annotations

import json
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from bdb_vnext.candidate import (
    CANDIDATE_DIVERGED,
    CANDIDATE_INVALIDATED,
    CANDIDATE_OBSERVED,
    CANDIDATE_POSSIBLE,
    CANDIDATE_PREPARED,
    CANDIDATE_SEALED,
    CandidateError,
    CandidateStore,
)
from bdb_vnext.control_store import read_identity
from bdb_vnext.bootstrap import create_coordinated_backup, restore_backup, verify_backup
from bdb_vnext.m3a_submission import ShadowSubmissionRequest
from bdb_vnext.m3c_admission import open_vnext_admission_composition
from bdb_vnext.m4a_work_kernel import WorkKernelStore
from bdb_vnext.repo_view import RepositoryResource


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _subject(tmp_path: Path) -> Path:
    repo = tmp_path / "subject"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "m4b@example.invalid")
    _git(repo, "config", "user.name", "M4b Test")
    (repo / "one.txt").write_bytes(b"one\r\n")
    (repo / "two.txt").write_bytes(b"two\n")
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
            submission_key="browser:m4b",
            intent_revision="r1",
            intent={"operation": "candidate"},
            conversation_binding={"conversation_id": "m4b"},
            consumer_binding={"consumer_id": "m4b", "kind": "browser"},
        )
    )
    assert receipt.task_id
    kernel = WorkKernelStore.open(runtime, task_authority=admission.authority, legacy_root=legacy, clock=lambda: 100.0)
    store = CandidateStore(runtime, work_kernel=kernel)
    view = RepositoryResource.from_path(subject, repository_id="m4b-subject").resolve_committed("HEAD")
    work = kernel.create_work_item("work:m4b", receipt.task_id)
    lease = kernel.acquire_lease(work.work_id, "lease:m4b", "worker:m4b")
    try:
        yield subject, view, kernel, store, work.work_id, receipt.task_id, lease
    finally:
        store.close()
        kernel.close()
        admission.close()


def _remove_candidate_worktree(subject: Path, workspace: Path) -> None:
    subprocess.run(["git", "-C", str(subject), "worktree", "remove", "--force", str(workspace)], capture_output=True, text=True, check=False)


def test_single_file_exact_candidate_seals_and_reads_cas(tmp_path: Path) -> None:
    with _stack(tmp_path) as (subject, view, _kernel, store, work_id, task_id, lease):
        workspace = store.create_workspace(candidate_id="candidate:single", base_view=view)
        before = (workspace / "one.txt").read_bytes()
        prepared = store.prepare(
            candidate_id="candidate:single",
            work_id=work_id,
            task_id=task_id,
            lease_id=lease.lease_id,
            fence=lease.fence,
            base_view=view,
            workspace_root=workspace,
            replacements={"one.txt": b"ONE\r\n"},
        )
        assert prepared.state == CANDIDATE_PREPARED
        assert prepared.effect_certainty == "NOT_ASSESSED"
        applied = store.apply(prepared.candidate_id)
        assert applied.state == CANDIDATE_OBSERVED
        assert applied.effect_certainty == "CERTAIN"
        sealed, candidate = store.seal(prepared.candidate_id, base_view=view)
        assert sealed.state == CANDIDATE_SEALED
        assert candidate.read_bytes("one.txt") == b"ONE\r\n"
        assert candidate.read_bytes("two.txt") == b"two\n"
        assert candidate.entry("one.txt").object_oid != view.entry("one.txt").object_oid
        assert {entry.path for entry in candidate.list_entries()} == {"one.txt", "two.txt"}
        assert before == view.read_bytes("one.txt")
        assert candidate.candidate_tree_digest == sealed.planned_tree_digest
        _remove_candidate_worktree(subject, workspace)


def test_windows_filesystem_mode_does_not_override_committed_git_mode(tmp_path: Path) -> None:
    with _stack(tmp_path) as (subject, _view, _kernel, store, _work_id, _task_id, _lease):
        (subject / "tool.cmd").write_bytes(b"@echo off\r\n")
        _git(subject, "add", "tool.cmd")
        _git(subject, "commit", "-qm", "cmd fixture")
        view = RepositoryResource.from_path(subject, repository_id="m4b-subject").resolve_committed("HEAD")
        workspace = store.create_workspace(candidate_id="candidate:windows-mode", base_view=view)
        try:
            os.chmod(workspace / "tool.cmd", 0o755)
            assert store._workspace_entries(workspace, object_format=view.object_format) == store._base_entries(view)
        finally:
            _remove_candidate_worktree(subject, workspace)


def test_restart_query_and_source_worktree_are_exactly_preserved(tmp_path: Path) -> None:
    with _stack(tmp_path) as (subject, view, kernel, store, work_id, task_id, lease):
        source_head = _git(subject, "rev-parse", "HEAD")
        source_status = _git(subject, "status", "--short", "--branch")
        workspace = store.create_workspace(candidate_id="candidate:restart", base_view=view)
        prepared = store.prepare(
            candidate_id="candidate:restart", work_id=work_id, task_id=task_id,
            lease_id=lease.lease_id, fence=lease.fence, base_view=view,
            workspace_root=workspace, replacements={"one.txt": b"restart\n"},
        )
        store.mark_possible(prepared.candidate_id)
        assert store.get(prepared.candidate_id).effect_certainty == "POSSIBLE"  # type: ignore[union-attr]
        store.apply(prepared.candidate_id)
        sealed, candidate = store.seal(prepared.candidate_id, base_view=view)
        manifest = sealed.manifest_digest
        store.close()
        reopened = CandidateStore(tmp_path / "runtime", work_kernel=kernel)
        try:
            record = reopened.get(prepared.candidate_id)
            assert record is not None and record.state == CANDIDATE_SEALED
            assert record.manifest_digest == manifest
            reopened_view = reopened.get_view(prepared.candidate_id, view)
            assert reopened_view.view_id == candidate.view_id
            assert reopened_view.read_bytes("one.txt") == b"restart\n"
        finally:
            reopened.close()
        assert _git(subject, "rev-parse", "HEAD") == source_head
        assert _git(subject, "status", "--short", "--branch") == source_status
        assert _git(subject, "symbolic-ref", "--short", "HEAD") == "master" or _git(subject, "symbolic-ref", "--short", "HEAD") == "main"
        _remove_candidate_worktree(subject, workspace)


def test_wrong_base_and_wrong_task_fail_closed(tmp_path: Path) -> None:
    with _stack(tmp_path) as (subject, view, _kernel, store, work_id, task_id, lease):
        workspace = store.create_workspace(candidate_id="candidate:wrong", base_view=view)
        with pytest.raises(CandidateError) as caught:
            store.prepare(
                candidate_id="candidate:wrong", work_id=work_id, task_id="task:foreign",
                lease_id=lease.lease_id, fence=lease.fence, base_view=view,
                workspace_root=workspace, replacements={"one.txt": b"x"},
            )
        assert caught.value.code == "task_binding_mismatch"
        _remove_candidate_worktree(subject, workspace)
        workspace = store.create_workspace(candidate_id="candidate:wrong2", base_view=view)
        (workspace / "one.txt").write_bytes(b"foreign-before")
        with pytest.raises(CandidateError) as caught:
            store.prepare(
                candidate_id="candidate:wrong2", work_id=work_id, task_id=task_id,
                lease_id=lease.lease_id, fence=lease.fence, base_view=view,
                workspace_root=workspace, replacements={"one.txt": b"x"},
            )
        assert caught.value.code == "candidate_base_mismatch"
        _remove_candidate_worktree(subject, workspace)


def test_unfinished_effect_can_be_adopted_only_by_current_fence(tmp_path: Path) -> None:
    with _stack(tmp_path) as (subject, view, kernel, store, work_id, task_id, lease):
        workspace = store.create_workspace(candidate_id="candidate:adopt", base_view=view)
        prepared = store.prepare(
            candidate_id="candidate:adopt", work_id=work_id, task_id=task_id,
            lease_id=lease.lease_id, fence=lease.fence, base_view=view,
            workspace_root=workspace, replacements={"one.txt": b"adopt\n"},
        )
        with pytest.raises(CandidateError) as caught:
            store.apply(prepared.candidate_id, fault="before_write")
        assert caught.value.code == "candidate_apply_interrupted"
        kernel.release_lease(work_id, lease.lease_id, lease.fence)
        newer = kernel.acquire_lease(work_id, "lease:adopt-new", "worker:adopt-new")
        adopted = store.adopt_lease(prepared.candidate_id, lease_id=newer.lease_id, fence=newer.fence)
        assert adopted.lease_id == newer.lease_id
        assert store.apply(prepared.candidate_id).state == CANDIDATE_OBSERVED
        _remove_candidate_worktree(subject, workspace)


def test_schema_documents_are_closed_and_control_identity_is_unified(tmp_path: Path) -> None:
    schema_names = ["bdb-vnext-m4b-candidate-v1.schema.json", "bdb-vnext-candidate-repo-view-v1.schema.json"]
    for name in schema_names:
        document = json.loads((Path(__file__).parents[1] / "schemas" / name).read_text(encoding="utf-8"))
        assert document["$schema"].endswith("2020-12/schema")
        assert document["additionalProperties"] is False
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    with CandidateStore(runtime) as store:
        identity = read_identity(store._connection)
        assert identity["schema_checksum"].startswith("sha256:")
        assert store.database_path == runtime / "control" / "control.db"


def test_sealed_candidate_manifest_and_cas_survive_cold_restore(tmp_path: Path) -> None:
    with _stack(tmp_path) as (subject, view, _kernel, store, work_id, task_id, lease):
        workspace = store.create_workspace(candidate_id="candidate:backup", base_view=view)
        prepared = store.prepare(
            candidate_id="candidate:backup", work_id=work_id, task_id=task_id,
            lease_id=lease.lease_id, fence=lease.fence, base_view=view,
            workspace_root=workspace, replacements={"one.txt": b"backup\n"},
        )
        store.apply(prepared.candidate_id)
        sealed, _view = store.seal(prepared.candidate_id, base_view=view)
        backup = create_coordinated_backup(
            tmp_path / "runtime", tmp_path / "authority" / "backups", backup_id="m4b-backup",
            required_control_schema=1, source_is_quiesced=True, include_control_identity=True,
        )
        verified = verify_backup(backup.path)
        restored = tmp_path / "restored"
        restore_backup(backup.path, restored, authority_root=tmp_path / "authority", legacy_runtime_root=tmp_path / "legacy", forbidden_roots=(tmp_path / "runtime",))
        reopened = CandidateStore(restored)
        try:
            record = reopened.get(prepared.candidate_id)
            assert record is not None and record.manifest_digest == sealed.manifest_digest
            assert record.state == CANDIDATE_SEALED
            assert record.candidate_view_id is not None
            restored_view = reopened.get_view(prepared.candidate_id, view)
            assert restored_view.read_bytes("one.txt") == b"backup\n"
        finally:
            reopened.close()
        _remove_candidate_worktree(subject, workspace)


def test_multifile_tree_proof_rejects_foreign_state_and_partial_apply_is_not_retried(tmp_path: Path) -> None:
    with _stack(tmp_path) as (subject, view, _kernel, store, work_id, task_id, lease):
        workspace = store.create_workspace(candidate_id="candidate:multi", base_view=view)
        prepared = store.prepare(
            candidate_id="candidate:multi", work_id=work_id, task_id=task_id,
            lease_id=lease.lease_id, fence=lease.fence, base_view=view,
            workspace_root=workspace, replacements={"one.txt": b"A\r\n", "two.txt": b"B\n"},
        )
        partial = store.apply(prepared.candidate_id, fail_after_paths=1)
        assert partial.state == CANDIDATE_POSSIBLE
        observed = store.observe(prepared.candidate_id)
        assert observed.state == CANDIDATE_DIVERGED
        with pytest.raises(CandidateError) as caught:
            store.apply(prepared.candidate_id)
        assert caught.value.code == "candidate_state_conflict"
        _remove_candidate_worktree(subject, workspace)

        workspace = store.create_workspace(candidate_id="candidate:foreign", base_view=view)
        prepared = store.prepare(
            candidate_id="candidate:foreign", work_id=work_id, task_id=task_id,
            lease_id=lease.lease_id, fence=lease.fence, base_view=view,
            workspace_root=workspace, replacements={"one.txt": b"A\r\n"},
        )
        store.apply(prepared.candidate_id)
        (workspace / "foreign.txt").write_bytes(b"foreign")
        with pytest.raises(CandidateError) as caught:
            store.seal(prepared.candidate_id, base_view=view)
        assert caught.value.code == "candidate_tree_mismatch"
        assert store.get(prepared.candidate_id).state == CANDIDATE_DIVERGED  # type: ignore[union-attr]
        _remove_candidate_worktree(subject, workspace)


def test_wrong_task_path_escape_stale_fence_and_post_seal_invalidation(tmp_path: Path) -> None:
    with _stack(tmp_path) as (subject, view, kernel, store, work_id, task_id, lease):
        workspace = store.create_workspace(candidate_id="candidate:bad", base_view=view)
        with pytest.raises(CandidateError) as caught:
            store.prepare(
                candidate_id="candidate:bad", work_id=work_id, task_id=task_id,
                lease_id=lease.lease_id, fence=lease.fence, base_view=view,
                workspace_root=workspace, replacements={"../outside": b"x"},
            )
        assert caught.value.code == "unsafe_candidate_path"
        _remove_candidate_worktree(subject, workspace)


def test_reparse_ads_and_case_collision_fail_closed(tmp_path: Path) -> None:
    with _stack(tmp_path) as (subject, view, _kernel, store, work_id, task_id, lease):
        workspace = store.create_workspace(candidate_id="candidate:safety", base_view=view)
        with pytest.raises(CandidateError) as caught:
            store.prepare(
                candidate_id="candidate:safety", work_id=work_id, task_id=task_id,
                lease_id=lease.lease_id, fence=lease.fence, base_view=view,
                workspace_root=workspace, replacements={"one.txt:stream": b"x"},
            )
        assert caught.value.code == "unsafe_candidate_path"
        with pytest.raises(CandidateError) as caught:
            store.prepare(
                candidate_id="candidate:safety", work_id=work_id, task_id=task_id,
                lease_id=lease.lease_id, fence=lease.fence, base_view=view,
                workspace_root=workspace, replacements={"one.txt": b"x", "ONE.TXT": b"y"},
            )
        assert caught.value.code == "case_collision"
        _remove_candidate_worktree(subject, workspace)

        workspace = store.create_workspace(candidate_id="candidate:reparse", base_view=view)
        outside = tmp_path / "outside.txt"
        outside.write_bytes(b"outside")
        target = workspace / "one.txt"
        target.unlink()
        try:
            os.symlink(outside, target)
        except (OSError, NotImplementedError):
            _remove_candidate_worktree(subject, workspace)
            pytest.skip("Windows symlink privilege is unavailable in this environment")
        try:
            with pytest.raises(CandidateError) as caught:
                store.prepare(
                    candidate_id="candidate:reparse", work_id=work_id, task_id=task_id,
                    lease_id=lease.lease_id, fence=lease.fence, base_view=view,
                    workspace_root=workspace, replacements={"one.txt": b"x"},
                )
            assert caught.value.code == "workspace_reparse_point"
        finally:
            _remove_candidate_worktree(subject, workspace)

        workspace = store.create_workspace(candidate_id="candidate:stale", base_view=view)
        prepared = store.prepare(
            candidate_id="candidate:stale", work_id=work_id, task_id=task_id,
            lease_id=lease.lease_id, fence=lease.fence, base_view=view,
            workspace_root=workspace, replacements={"one.txt": b"A\r\n"},
        )
        kernel.release_lease(work_id, lease.lease_id, lease.fence)
        newer = kernel.acquire_lease(work_id, "lease:new", "worker:new")
        assert newer.fence > lease.fence
        with pytest.raises(CandidateError) as caught:
            store.apply(prepared.candidate_id)
        assert caught.value.code == "stale_fence"
        _remove_candidate_worktree(subject, workspace)

        # Fresh owner for the positive seal/invalidation path.
        lease = newer
        workspace = store.create_workspace(candidate_id="candidate:sealed", base_view=view)
        prepared = store.prepare(
            candidate_id="candidate:sealed", work_id=work_id, task_id=task_id,
            lease_id=lease.lease_id, fence=lease.fence, base_view=view,
            workspace_root=workspace, replacements={"one.txt": b"A\r\n"},
        )
        store.apply(prepared.candidate_id)
        _sealed, candidate = store.seal(prepared.candidate_id, base_view=view)
        (workspace / "one.txt").write_bytes(b"tampered")
        invalidated = store.invalidate_if_changed(prepared.candidate_id)
        assert invalidated.state == CANDIDATE_INVALIDATED
        with pytest.raises(CandidateError) as caught:
            candidate.read_bytes("one.txt")
        assert caught.value.code == "candidate_not_sealed"
        _remove_candidate_worktree(subject, workspace)


@pytest.mark.parametrize("fault", ["locked_file", "permission_denied", "disk_full", "during_temp_create", "after_temp_write"])
def test_filesystem_faults_are_typed_and_not_certainty(tmp_path: Path, fault: str) -> None:
    with _stack(tmp_path) as (subject, view, _kernel, store, work_id, task_id, lease):
        workspace = store.create_workspace(candidate_id="candidate:fault-" + fault, base_view=view)
        prepared = store.prepare(
            candidate_id="candidate:fault-" + fault, work_id=work_id, task_id=task_id,
            lease_id=lease.lease_id, fence=lease.fence, base_view=view,
            workspace_root=workspace, replacements={"one.txt": b"A\r\n"},
        )
        with pytest.raises(CandidateError) as caught:
            store.apply(prepared.candidate_id, fault=fault)
        assert caught.value.code == "candidate_apply_failed"
        record = store.get(prepared.candidate_id)
        assert record is not None and record.effect_certainty in {"POSSIBLE", "UNKNOWN"}
        assert record.state != CANDIDATE_SEALED
        _remove_candidate_worktree(subject, workspace)


def test_crash_points_are_recoverable_without_guessing(tmp_path: Path) -> None:
    with _stack(tmp_path) as (subject, view, _kernel, store, work_id, task_id, lease):
        workspace = store.create_workspace(candidate_id="candidate:crash", base_view=view)
        prepared = store.prepare(
            candidate_id="candidate:crash", work_id=work_id, task_id=task_id,
            lease_id=lease.lease_id, fence=lease.fence, base_view=view,
            workspace_root=workspace, replacements={"one.txt": b"crash\n"},
        )
        with pytest.raises(CandidateError) as caught:
            store.apply(prepared.candidate_id, fault="after_write_before_observe")
        assert caught.value.code == "candidate_apply_interrupted"
        assert store.get(prepared.candidate_id).effect_certainty == "POSSIBLE"  # type: ignore[union-attr]
        assert store.observe(prepared.candidate_id).state == CANDIDATE_OBSERVED
        with pytest.raises(CandidateError) as caught:
            store.seal(prepared.candidate_id, base_view=view, fault="after_seal_commit")
        assert caught.value.code == "candidate_seal_response_lost"
        assert store.get(prepared.candidate_id).state == CANDIDATE_SEALED  # type: ignore[union-attr]
        _remove_candidate_worktree(subject, workspace)


def test_observation_and_preseal_interruptions_remain_recoverable(tmp_path: Path) -> None:
    with _stack(tmp_path) as (subject, view, _kernel, store, work_id, task_id, lease):
        workspace = store.create_workspace(candidate_id="candidate:observe-fault", base_view=view)
        prepared = store.prepare(
            candidate_id="candidate:observe-fault", work_id=work_id, task_id=task_id,
            lease_id=lease.lease_id, fence=lease.fence, base_view=view,
            workspace_root=workspace, replacements={"one.txt": b"observe\n"},
        )
        store.mark_possible(prepared.candidate_id)
        with pytest.raises(CandidateError) as caught:
            store.observe(prepared.candidate_id, fault="during_observation")
        assert caught.value.code == "candidate_observation_interrupted"
        assert store.get(prepared.candidate_id).state == CANDIDATE_POSSIBLE  # type: ignore[union-attr]
        store.apply(prepared.candidate_id)
        _remove_candidate_worktree(subject, workspace)

        workspace = store.create_workspace(candidate_id="candidate:preseal-fault", base_view=view)
        prepared = store.prepare(
            candidate_id="candidate:preseal-fault", work_id=work_id, task_id=task_id,
            lease_id=lease.lease_id, fence=lease.fence, base_view=view,
            workspace_root=workspace, replacements={"one.txt": b"preseal\n"},
        )
        store.apply(prepared.candidate_id)
        with pytest.raises(CandidateError) as caught:
            store.seal(prepared.candidate_id, base_view=view, fault="before_seal_commit")
        assert caught.value.code == "candidate_seal_interrupted"
        record = store.get(prepared.candidate_id)
        assert record is not None and record.state == CANDIDATE_OBSERVED and record.effect_certainty == "CERTAIN"
        sealed, _view = store.seal(prepared.candidate_id, base_view=view)
        assert sealed.state == CANDIDATE_SEALED
        _remove_candidate_worktree(subject, workspace)


def test_missing_workspace_is_unknown_not_certainty(tmp_path: Path) -> None:
    with _stack(tmp_path) as (subject, view, _kernel, store, work_id, task_id, lease):
        workspace = store.create_workspace(candidate_id="candidate:missing", base_view=view)
        prepared = store.prepare(
            candidate_id="candidate:missing", work_id=work_id, task_id=task_id,
            lease_id=lease.lease_id, fence=lease.fence, base_view=view,
            workspace_root=workspace, replacements={"one.txt": b"missing\n"},
        )
        _remove_candidate_worktree(subject, workspace)
        recovered = store.observe(prepared.candidate_id)
        assert recovered.state == "UNKNOWN"
        assert recovered.effect_certainty == "UNKNOWN"

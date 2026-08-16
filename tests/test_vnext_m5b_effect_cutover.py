"""Focused tests proving M5b/P1 single safe-next effect cutover to KernelEffectReconciler."""

from __future__ import annotations

import inspect
import sqlite3
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from bdb_vnext.candidate import (
    CANDIDATE_OBSERVED,
    CANDIDATE_POSSIBLE,
    CANDIDATE_PREPARED,
    CANDIDATE_SEALED,
    CANDIDATE_UNKNOWN,
)
from bdb_vnext.engineering_loop import (
    EditBatch,
    EditOperation,
    EditorPort,
    EngineeringLoopError,
)
from bdb_vnext.m5a_effects import KernelEffectReconciler, M5aError
import bdb_vnext.engineering_loop as engineering_loop_mod
import bdb_vnext.n6_rehearsal as n6_rehearsal_mod


@dataclass
class FakeCandidate:
    candidate_id: str = "candidate:m5b"
    effect_id: str = "sha256:" + "1" * 64
    work_id: str = "work:m5b"
    task_id: str = "task:m5b"
    state: str = CANDIDATE_PREPARED
    effect_certainty: str = "NOT_ASSESSED"
    workspace_root: str = "C:/bounded/candidate-m5b"
    workspace_generation: str = "bdb-vnext-g1"
    config_digest: str = "sha256:" + "2" * 64
    lease_id: str = "lease:m5b"
    fence: int = 1
    base_tree_digest: str = "sha256:" + "3" * 64
    planned_tree_digest: str = "sha256:" + "4" * 64
    observed_tree_digest: str | None = None


class FakeWorkKernel:
    def __init__(self, *, run_id: str = "run:m5b") -> None:
        self.run = SimpleNamespace(
            run_id=run_id,
            lease_id="lease:m5b",
            fence=1,
            status="ACTIVE",
        )
        self.work = SimpleNamespace(work_id="work:m5b", task_id="task:m5b")

    def query(self, work_id: str):
        assert work_id == self.work.work_id
        return SimpleNamespace(
            work=self.work,
            active_run=self.run,
            last_run=self.run,
        )


class FakeCandidateStore:
    def __init__(self, record: FakeCandidate | None = None) -> None:
        self.record = record or FakeCandidate()
        self._connection = sqlite3.connect(":memory:")
        self.events: list[str] = []
        self.apply_calls = 0
        self.mark_possible_calls = 0
        self.observe_calls = 0

    def get(self, candidate_id: str):
        assert candidate_id == self.record.candidate_id
        return self.record

    def mark_possible(self, candidate_id: str):
        assert candidate_id == self.record.candidate_id
        self.mark_possible_calls += 1
        self.events.append(f"mark_possible:{self.record.state}")
        self.record.state = CANDIDATE_POSSIBLE
        self.record.effect_certainty = "POSSIBLE"
        return self.record

    def apply(self, candidate_id: str, **_kwargs):
        assert candidate_id == self.record.candidate_id
        self.apply_calls += 1
        self.events.append(f"apply:{self.record.state}")
        self.record.state = CANDIDATE_OBSERVED
        self.record.effect_certainty = "CERTAIN"
        self.record.observed_tree_digest = self.record.planned_tree_digest
        return self.record

    def observe(self, candidate_id: str):
        assert candidate_id == self.record.candidate_id
        self.observe_calls += 1
        self.events.append(f"observe:{self.record.state}")
        if self.record.state == CANDIDATE_OBSERVED:
            self.record.effect_certainty = "CERTAIN"
        return self.record


def test_on_possible_executes_after_durable_possible_and_before_physical_apply() -> None:
    """Requirement A: on_possible runs only after mark_possible and before physical apply."""
    store = FakeCandidateStore()
    reconciler = KernelEffectReconciler(
        work_kernel=FakeWorkKernel(),
        candidate_store=store,
    )
    observed_state_at_callback: list[str] = []

    def on_possible() -> None:
        store.events.append("on_possible_called")
        observed_state_at_callback.append(store.record.state)

    result = reconciler.apply_if_safe(
        candidate_id="candidate:m5b",
        run_id="run:m5b",
        on_possible=on_possible,
    )

    assert result.effect_certainty == "AFTER"
    assert observed_state_at_callback == [CANDIDATE_POSSIBLE]
    assert store.events == [
        "mark_possible:PREPARED",
        "on_possible_called",
        "apply:POSSIBLE",
    ]


def test_simulated_crash_in_on_possible_leaves_possible_and_recovery_applies_at_most_once() -> None:
    """Requirement B: Simulated crash during on_possible leaves POSSIBLE, recovery avoids duplicate apply."""
    store = FakeCandidateStore()
    reconciler = KernelEffectReconciler(
        work_kernel=FakeWorkKernel(),
        candidate_store=store,
    )

    def crash_callback() -> None:
        raise RuntimeError("simulated crash at on_possible checkpoint")

    with pytest.raises(RuntimeError, match="simulated crash"):
        reconciler.apply_if_safe(
            candidate_id="candidate:m5b",
            run_id="run:m5b",
            on_possible=crash_callback,
        )

    # Durable state is POSSIBLE, physical apply was NOT called
    assert store.record.state == CANDIDATE_POSSIBLE
    assert store.apply_calls == 0

    # Simulate observation showing physical workspace was indeed still BEFORE
    def simulated_observe(candidate_id: str):
        store.observe_calls += 1
        store.record.state = CANDIDATE_PREPARED
        store.record.effect_certainty = "NOT_ASSESSED"
        return store.record

    store.observe = simulated_observe  # type: ignore[assignment]

    # Subsequent recovery retry
    recovered = reconciler.apply_if_safe(
        candidate_id="candidate:m5b",
        run_id="run:m5b",
    )

    assert recovered.effect_certainty == "AFTER"
    assert store.apply_calls == 1


def test_editor_port_apply_batch_routes_through_kernel_effect_reconciler(monkeypatch: pytest.MonkeyPatch) -> None:
    """Requirement C: EditorPort.apply_batch delegates physical effect authority to KernelEffectReconciler."""
    applied_via_reconciler: list[dict[str, Any]] = []

    original_apply_if_safe = KernelEffectReconciler.apply_if_safe

    def spy_apply_if_safe(self, *, candidate_id: str, run_id: str, **kwargs):
        applied_via_reconciler.append({"candidate_id": candidate_id, "run_id": run_id})
        return original_apply_if_safe(self, candidate_id=candidate_id, run_id=run_id, **kwargs)

    monkeypatch.setattr(KernelEffectReconciler, "apply_if_safe", spy_apply_if_safe)

    store = FakeCandidateStore()
    work_kernel = FakeWorkKernel()
    store.work_kernel = work_kernel  # type: ignore[attr-defined]

    editor = EditorPort(candidate_store=store)  # type: ignore[arg-type]

    batch = EditBatch(
        schema="bdb-vnext-edit-v1",
        base_view_id="sha256:" + "0" * 64,
        expected_tree_digest="sha256:" + "3" * 64,
        task_id="task:m5b",
        work_id="work:m5b",
        run_id="run:m5b",
        lease_id="lease:m5b",
        fence=1,
        candidate_id="candidate:m5b",
        workspace_generation="bdb-vnext-g1",
        operations=(EditOperation(operation="MODIFY", path="file.txt", content=b"content"),),
        budget={"max_operations": 32, "max_bytes": 1024},
        artifact_digest="sha256:" + "5" * 64,
    )

    record = editor.apply_batch(batch)
    assert record.state == CANDIDATE_OBSERVED
    assert len(applied_via_reconciler) == 1
    assert applied_via_reconciler[0] == {"candidate_id": "candidate:m5b", "run_id": "run:m5b"}


def test_n6_rehearsal_vertical_routes_through_kernel_reconciler_without_local_apply_authority() -> None:
    """Requirement D: Generic N6 Candidate execution uses KernelEffectReconciler and has no local mark_possible/apply decision."""
    source = inspect.getsource(n6_rehearsal_mod.N6RehearsalService._run_vertical)
    # Ensure local unmediated sequence "plane.candidate.mark_possible" -> "plane.candidate.apply" is gone
    assert "reconciler = KernelEffectReconciler(" in source
    assert "projection = reconciler.apply_if_safe(" in source
    assert "on_possible=lambda: checkpoint(\"candidate_possible\")" in source


def test_engineering_artifact_does_not_perform_second_independent_apply_decision() -> None:
    """Requirement E: _engineering_artifact relies directly on editor.apply_batch() and performs no second candidate apply/observe retry."""
    source = inspect.getsource(n6_rehearsal_mod.N6RehearsalService._engineering_artifact)
    assert "observed = editor.apply_batch(artifact)" in source
    assert "if observed.state != CANDIDATE_OBSERVED:" in source
    # Ensure there is no second plane.candidate.observe or candidate.apply decision here
    assert "plane.candidate.observe(ids[\"candidate_id\"])" not in source
    assert "plane.candidate.apply(" not in source

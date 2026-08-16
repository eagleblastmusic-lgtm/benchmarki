from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from bdb_vnext.candidate import (
    CANDIDATE_OBSERVED,
    CANDIDATE_POSSIBLE,
    CANDIDATE_PREPARED,
    CANDIDATE_UNKNOWN,
)
from bdb_vnext.m5a_effects import KernelEffectReconciler, M5aError


@dataclass
class FakeCandidate:
    candidate_id: str = "candidate:m5a"
    effect_id: str = "sha256:" + "1" * 64
    work_id: str = "work:m5a"
    task_id: str = "task:m5a"
    state: str = CANDIDATE_PREPARED
    effect_certainty: str = "NOT_ASSESSED"
    workspace_root: str = "C:/bounded/candidate-m5a"
    workspace_generation: str = "bdb-vnext-g1"
    config_digest: str = "sha256:" + "2" * 64
    lease_id: str = "lease:m5a"
    fence: int = 7
    base_tree_digest: str = "sha256:" + "3" * 64
    observed_tree_digest: str | None = None


class FakeWorkKernel:
    def __init__(self, *, run_id: str = "run:m5a") -> None:
        self.run = SimpleNamespace(
            run_id=run_id,
            lease_id="lease:m5a",
            fence=7,
            status="ACTIVE",
        )
        self.work = SimpleNamespace(work_id="work:m5a", task_id="task:m5a")

    def query(self, work_id: str):
        assert work_id == self.work.work_id
        return SimpleNamespace(
            work=self.work,
            active_run=self.run,
            last_run=self.run,
        )


class FakeCandidateStore:
    def __init__(
        self,
        record: FakeCandidate | None = None,
        *,
        crash_after_effect_once: bool = False,
        observation_stays_unknown: bool = False,
    ) -> None:
        self.record = record or FakeCandidate()
        self.crash_after_effect_once = crash_after_effect_once
        self.observation_stays_unknown = observation_stays_unknown
        self.physical_after = self.record.state == CANDIDATE_OBSERVED
        self.apply_calls = 0
        self.observe_calls = 0
        self.mark_possible_calls = 0

    def get(self, candidate_id: str):
        assert candidate_id == self.record.candidate_id
        return self.record

    def mark_possible(self, candidate_id: str):
        assert candidate_id == self.record.candidate_id
        self.mark_possible_calls += 1
        assert self.record.state == CANDIDATE_PREPARED
        self.record.state = CANDIDATE_POSSIBLE
        self.record.effect_certainty = "POSSIBLE"
        return self.record

    def apply(self, candidate_id: str, **_kwargs):
        assert candidate_id == self.record.candidate_id
        self.apply_calls += 1
        self.physical_after = True
        self.record.state = CANDIDATE_POSSIBLE
        self.record.effect_certainty = "POSSIBLE"
        if self.crash_after_effect_once and self.apply_calls == 1:
            raise RuntimeError("simulated response loss after external effect")
        self.record.state = CANDIDATE_OBSERVED
        self.record.effect_certainty = "CERTAIN"
        self.record.observed_tree_digest = "sha256:" + "4" * 64
        return self.record

    def observe(self, candidate_id: str):
        assert candidate_id == self.record.candidate_id
        self.observe_calls += 1
        if self.observation_stays_unknown:
            self.record.state = CANDIDATE_UNKNOWN
            self.record.effect_certainty = "UNKNOWN"
            return self.record
        if self.physical_after:
            self.record.state = CANDIDATE_OBSERVED
            self.record.effect_certainty = "CERTAIN"
            self.record.observed_tree_digest = "sha256:" + "4" * 64
        else:
            self.record.state = CANDIDATE_PREPARED
            self.record.effect_certainty = "NOT_ASSESSED"
        return self.record


def _reconciler(store: FakeCandidateStore, *, run_id: str = "run:m5a") -> KernelEffectReconciler:
    return KernelEffectReconciler(
        work_kernel=FakeWorkKernel(run_id=run_id),
        candidate_store=store,
    )


def test_double_apply_same_run_no_second_effect() -> None:
    store = FakeCandidateStore()
    reconciler = _reconciler(store)

    first = reconciler.apply_if_safe(candidate_id=store.record.candidate_id, run_id="run:m5a")
    second = reconciler.apply_if_safe(candidate_id=store.record.candidate_id, run_id="run:m5a")

    assert first.effect_certainty == "AFTER"
    assert second.effect_certainty == "AFTER"
    assert second.safe_next_action == "COMPLETE_ALLOWED"
    assert store.mark_possible_calls == 1
    assert store.apply_calls == 1


def test_crash_after_effect_before_close_reconciles_without_second_apply() -> None:
    store = FakeCandidateStore(crash_after_effect_once=True)
    reconciler = _reconciler(store)

    with pytest.raises(RuntimeError, match="response loss"):
        reconciler.apply_if_safe(candidate_id=store.record.candidate_id, run_id="run:m5a")

    assert store.record.state == CANDIDATE_POSSIBLE
    assert store.apply_calls == 1

    recovered = reconciler.apply_if_safe(
        candidate_id=store.record.candidate_id,
        run_id="run:m5a",
    )

    assert recovered.effect_certainty == "AFTER"
    assert store.observe_calls >= 1
    assert store.apply_calls == 1


def test_unknown_external_state_blocks_retry() -> None:
    record = FakeCandidate(state=CANDIDATE_UNKNOWN, effect_certainty="UNKNOWN")
    store = FakeCandidateStore(record, observation_stays_unknown=True)
    reconciler = _reconciler(store)

    with pytest.raises(M5aError) as caught:
        reconciler.apply_if_safe(candidate_id=record.candidate_id, run_id="run:m5a")

    assert caught.value.code == "effect_reconciliation_required"
    assert caught.value.details["effect_certainty"] == "AMBIGUOUS"
    assert store.observe_calls == 1
    assert store.mark_possible_calls == 0
    assert store.apply_calls == 0


def test_recovery_is_run_centric() -> None:
    record = FakeCandidate(state=CANDIDATE_POSSIBLE, effect_certainty="POSSIBLE")
    store = FakeCandidateStore(record)
    reconciler = _reconciler(store)

    with pytest.raises(M5aError) as caught:
        reconciler.reconcile(candidate_id=record.candidate_id, run_id="run:foreign")

    assert caught.value.code == "run_identity_mismatch"
    assert store.observe_calls == 0
    assert store.apply_calls == 0


def test_projection_maps_existing_candidate_truth_without_new_effect_identity() -> None:
    store = FakeCandidateStore()
    projection = _reconciler(store).project(
        candidate_id=store.record.candidate_id,
        run_id="run:m5a",
    )

    document = projection.as_dict()
    assert projection.effect_certainty == "BEFORE"
    assert projection.safe_next_action == "SAFE_TO_APPLY"
    assert projection.exact_effect_digest == store.record.effect_id
    assert document["query_digest"].startswith("sha256:")
    assert document["candidate_id"] == store.record.candidate_id

"""M5a build-only effect certainty projection and Run-centric reconciliation.

M4b CandidateStore already owns the durable exact Candidate effect intent and
the physical filesystem witness.  This module adds no second store, lifecycle
identifier, effect identifier, or retry authority.  It projects that existing
record onto the frozen M5a certainty lattice and centralises the safe-next
decision around the canonical Work/Run identity.

The reconciler may observe an uncertain effect, but it never retries a
POSSIBLE/AMBIGUOUS/DIVERGED effect blindly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping, NoReturn

from bdb_shared.evidence import semantic_digest
from bdb_vnext.candidate import (
    CANDIDATE_APPLIED,
    CANDIDATE_DIVERGED,
    CANDIDATE_EFFECT_CLASS,
    CANDIDATE_INVALIDATED,
    CANDIDATE_OBSERVED,
    CANDIDATE_POSSIBLE,
    CANDIDATE_PREPARED,
    CANDIDATE_SEALED,
    CANDIDATE_UNKNOWN,
)


M5A_QUERY_SCHEMA = "bdb-vnext-m5a-effect-query-v1"
M5A_AUTHORITY_ID = "devmaster.bdb.vnext.work-kernel.effect-reconciler"

EffectCertainty = Literal["BEFORE", "POSSIBLE", "AFTER", "AMBIGUOUS", "DIVERGED"]
SafeNextAction = Literal[
    "SAFE_TO_APPLY",
    "OBSERVE_REQUIRED",
    "COMPLETE_ALLOWED",
    "MANUAL_RECONCILIATION",
]


class M5aError(RuntimeError):
    """Bounded, machine-readable M5a reconciliation failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def _fail(
    code: str,
    message: str,
    *,
    details: Mapping[str, Any] | None = None,
) -> NoReturn:
    raise M5aError(code, message, details=details)


def _certainty(candidate_state: str) -> EffectCertainty:
    mapping: dict[str, EffectCertainty] = {
        CANDIDATE_PREPARED: "BEFORE",
        CANDIDATE_POSSIBLE: "POSSIBLE",
        CANDIDATE_APPLIED: "POSSIBLE",
        CANDIDATE_OBSERVED: "AFTER",
        CANDIDATE_SEALED: "AFTER",
        CANDIDATE_UNKNOWN: "AMBIGUOUS",
        CANDIDATE_DIVERGED: "DIVERGED",
        CANDIDATE_INVALIDATED: "DIVERGED",
    }
    try:
        return mapping[candidate_state]
    except KeyError:
        _fail(
            "candidate_state_unsupported",
            "Candidate state cannot be projected onto the frozen M5a certainty lattice",
            details={"candidate_state": candidate_state},
        )


def _safe_next(certainty: EffectCertainty) -> SafeNextAction:
    if certainty == "BEFORE":
        return "SAFE_TO_APPLY"
    if certainty == "POSSIBLE":
        return "OBSERVE_REQUIRED"
    if certainty == "AFTER":
        return "COMPLETE_ALLOWED"
    return "MANUAL_RECONCILIATION"


@dataclass(frozen=True)
class EffectProjection:
    """Canonical read/decision view over one existing durable Candidate effect."""

    work_id: str
    run_id: str
    task_id: str
    candidate_id: str
    effect_kind: str
    exact_effect_digest: str
    target_resource_key: str
    target_precondition: str
    candidate_state: str
    effect_certainty: EffectCertainty
    safe_next_action: SafeNextAction
    lease_id: str
    fence: int
    workspace_generation: str
    config_digest: str
    observed_tree_digest: str | None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": M5A_QUERY_SCHEMA,
            "authority": M5A_AUTHORITY_ID,
            "work_id": self.work_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "candidate_id": self.candidate_id,
            "effect_kind": self.effect_kind,
            "exact_effect_digest": self.exact_effect_digest,
            "target_resource_key": self.target_resource_key,
            "target_precondition": self.target_precondition,
            "candidate_state": self.candidate_state,
            "effect_certainty": self.effect_certainty,
            "safe_next_action": self.safe_next_action,
            "lease_id": self.lease_id,
            "fence": self.fence,
            "workspace_generation": self.workspace_generation,
            "config_digest": self.config_digest,
            "observed_tree_digest": self.observed_tree_digest,
        }
        payload["query_digest"] = semantic_digest(payload)
        return payload


class KernelEffectReconciler:
    """Run-centric safe-next authority over the existing M4b physical witness."""

    _OBSERVE_STATES = {
        CANDIDATE_POSSIBLE,
        CANDIDATE_APPLIED,
        CANDIDATE_UNKNOWN,
        CANDIDATE_DIVERGED,
    }

    def __init__(self, *, work_kernel: Any, candidate_store: Any) -> None:
        if work_kernel is None or candidate_store is None:
            _fail(
                "effect_dependencies_required",
                "M5a requires the canonical Work Kernel and Candidate store",
            )
        self.work_kernel = work_kernel
        self.candidate_store = candidate_store

    def _record(self, candidate_id: str) -> Any:
        record = self.candidate_store.get(candidate_id)
        if record is None:
            _fail(
                "candidate_missing",
                "M5a effect projection requires an existing Candidate",
                details={"candidate_id": candidate_id},
            )
        return record

    def _assert_run(self, record: Any, run_id: str, *, require_active: bool) -> Any:
        query = self.work_kernel.query(record.work_id)
        if query is None:
            _fail(
                "work_missing",
                "Candidate effect is not bound to a canonical WorkItem",
                details={"work_id": record.work_id},
            )
        if query.work.task_id != record.task_id:
            _fail(
                "task_binding_mismatch",
                "Candidate effect and canonical WorkItem disagree on Task identity",
            )

        active = query.active_run
        last = query.last_run
        run = active if active is not None and active.run_id == run_id else None
        if run is None and last is not None and last.run_id == run_id:
            run = last
        if run is None:
            _fail(
                "run_identity_mismatch",
                "Effect reconciliation requires the exact canonical Run",
                details={"work_id": record.work_id, "run_id": run_id},
            )
        if require_active and (active is None or active.run_id != run_id):
            _fail(
                "active_run_required",
                "A physical effect may start only under the exact active Run",
                details={"work_id": record.work_id, "run_id": run_id},
            )
        if active is not None and active.run_id == run_id:
            if record.lease_id != active.lease_id or int(record.fence) != int(active.fence):
                _fail(
                    "effect_owner_mismatch",
                    "Candidate effect is not owned by the active Run lease/fence",
                    details={
                        "candidate_lease_id": record.lease_id,
                        "candidate_fence": int(record.fence),
                        "run_lease_id": active.lease_id,
                        "run_fence": int(active.fence),
                    },
                )
        return run

    def _project_record(self, record: Any, run_id: str) -> EffectProjection:
        certainty = _certainty(str(record.state))
        return EffectProjection(
            work_id=str(record.work_id),
            run_id=run_id,
            task_id=str(record.task_id),
            candidate_id=str(record.candidate_id),
            effect_kind=CANDIDATE_EFFECT_CLASS,
            exact_effect_digest=str(record.effect_id),
            target_resource_key=str(record.workspace_root),
            target_precondition=str(record.base_tree_digest),
            candidate_state=str(record.state),
            effect_certainty=certainty,
            safe_next_action=_safe_next(certainty),
            lease_id=str(record.lease_id),
            fence=int(record.fence),
            workspace_generation=str(record.workspace_generation),
            config_digest=str(record.config_digest),
            observed_tree_digest=(
                str(record.observed_tree_digest)
                if record.observed_tree_digest is not None
                else None
            ),
        )

    def project(self, *, candidate_id: str, run_id: str) -> EffectProjection:
        """Project durable truth without touching the external resource."""

        record = self._record(candidate_id)
        self._assert_run(record, run_id, require_active=False)
        return self._project_record(record, run_id)

    def reconcile(self, *, candidate_id: str, run_id: str) -> EffectProjection:
        """Observe uncertain external truth once; never apply or retry it."""

        record = self._record(candidate_id)
        self._assert_run(record, run_id, require_active=False)
        if str(record.state) in self._OBSERVE_STATES:
            record = self.candidate_store.observe(candidate_id)
            self._assert_run(record, run_id, require_active=False)
        return self._project_record(record, run_id)

    def apply_if_safe(
        self,
        *,
        candidate_id: str,
        run_id: str,
        on_possible: Callable[[], None] | None = None,
        fault: str | None = None,
        fail_after_paths: int | None = None,
    ) -> EffectProjection:
        """Apply only from exact BEFORE; all uncertain states must reconcile first."""

        projection = self.reconcile(candidate_id=candidate_id, run_id=run_id)
        if projection.effect_certainty == "AFTER":
            return projection
        if projection.effect_certainty != "BEFORE":
            _fail(
                "effect_reconciliation_required",
                "External effect is not proven BEFORE; blind retry is prohibited",
                details={
                    "candidate_id": candidate_id,
                    "run_id": run_id,
                    "effect_certainty": projection.effect_certainty,
                    "safe_next_action": projection.safe_next_action,
                },
            )

        record = self._record(candidate_id)
        self._assert_run(record, run_id, require_active=True)

        # The boundary marker is durable before CandidateStore reaches the
        # physical filesystem apply path.
        record = self.candidate_store.mark_possible(candidate_id)
        self._assert_run(record, run_id, require_active=True)

        if on_possible is not None:
            on_possible()

        kwargs: dict[str, Any] = {}
        if fault is not None:
            kwargs["fault"] = fault
        if fail_after_paths is not None:
            kwargs["fail_after_paths"] = fail_after_paths
        self.candidate_store.apply(candidate_id, **kwargs)

        projection = self.reconcile(candidate_id=candidate_id, run_id=run_id)
        if projection.effect_certainty != "AFTER":
            _fail(
                "effect_reconciliation_required",
                "External effect did not reconcile to exact AFTER",
                details={
                    "candidate_id": candidate_id,
                    "run_id": run_id,
                    "effect_certainty": projection.effect_certainty,
                    "safe_next_action": projection.safe_next_action,
                },
            )
        return projection


__all__ = [
    "EffectProjection",
    "EffectCertainty",
    "KernelEffectReconciler",
    "M5A_AUTHORITY_ID",
    "M5A_QUERY_SCHEMA",
    "M5aError",
    "SafeNextAction",
]

"""Machine-readable M11b fault catalog and coverage contract.

The catalog maps every frozen activation phase to executable experiment cases
or to an explicit not-applicable proof.  It is data only: no production
activation capability is introduced here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal


Phase = Literal["STAGE", "BACKUP", "MIGRATE", "SWITCH", "START", "HEALTH", "CONCLUDE", "CONCURRENCY"]
Disposition = Literal["EXECUTABLE", "PROVEN_BY_PREREQUISITE", "NOT_APPLICABLE"]

REQUIRED_PHASES: tuple[Phase, ...] = (
    "STAGE",
    "BACKUP",
    "MIGRATE",
    "SWITCH",
    "START",
    "HEALTH",
    "CONCLUDE",
    "CONCURRENCY",
)

ALLOWED_TERMINAL_CLASSES = frozenset(
    {
        "KNOWN_GOOD_ACTIVE",
        "KNOWN_GOOD_CANDIDATE",
        "RECOVERED_PREVIOUS",
        "BLOCKED_QUARANTINED",
    }
)


@dataclass(frozen=True)
class FaultCell:
    cell_id: str
    phase: Phase
    fault: str
    disposition: Disposition
    expected: tuple[str, ...]
    evidence: str
    rationale: str


FAULT_CELLS: tuple[FaultCell, ...] = (
    FaultCell(
        "STAGE-CORRUPT-BUNDLE", "STAGE", "corrupt_or_moved_candidate", "PROVEN_BY_PREREQUISITE",
        ("BLOCKED_QUARANTINED",),
        "tests/test_m11a_bootstrap_slots.py::test_query_fails_closed_when_staged_bundle_bytes_move",
        "M11a exact slot re-observation blocks moved candidate bytes before activation.",
    ),
    FaultCell(
        "STAGE-INCOMPATIBLE", "STAGE", "schema_or_capability_mismatch", "PROVEN_BY_PREREQUISITE",
        ("BLOCKED_QUARANTINED",),
        "tests/test_m11a_bootstrap_slots.py::test_incompatible_candidate_is_blocked_without_changing_external_state",
        "Incompatible CANDIDATE never enters prepared activation.",
    ),
    FaultCell(
        "STAGE-SELF-ACTIVATE", "STAGE", "candidate_requests_pointer_authority", "PROVEN_BY_PREREQUISITE",
        ("BLOCKED_QUARANTINED",),
        "tests/test_m11a_bootstrap_slots.py::test_candidate_requesting_self_activation_is_rejected_by_m1b_bundle_contract",
        "Candidate self-activation is rejected before health or switch.",
    ),
    FaultCell(
        "BACKUP-COPY-CRASH", "BACKUP", "crash_during_copy", "PROVEN_BY_PREREQUISITE",
        ("BLOCKED_QUARANTINED",),
        "tests/test_vnext_bootstrap.py::test_copy_crash_leaves_only_unpublished_partial_backup",
        "Partial backup is never published as recovery authority.",
    ),
    FaultCell(
        "BACKUP-DISK-FULL", "BACKUP", "disk_full", "PROVEN_BY_PREREQUISITE",
        ("BLOCKED_QUARANTINED",),
        "tests/test_vnext_bootstrap.py::test_disk_full_write_failure_is_explicit_and_never_publishes_backup",
        "Disk-full before backup publication is explicit and fail-closed.",
    ),
    FaultCell(
        "BACKUP-PUBLISH-INTERRUPT", "BACKUP", "crash_before_atomic_publish", "PROVEN_BY_PREREQUISITE",
        ("BLOCKED_QUARANTINED",),
        "tests/test_vnext_bootstrap.py::test_interruption_before_atomic_backup_publish_returns_blocked",
        "Interrupted staging remains non-authoritative.",
    ),
    FaultCell(
        "BACKUP-DB-WAL", "BACKUP", "corrupt_or_incomplete_db_wal", "PROVEN_BY_PREREQUISITE",
        ("BLOCKED_QUARANTINED",),
        "tests/test_vnext_bootstrap.py::test_incomplete_or_mismatched_db_wal_fixture_is_rejected",
        "An invalid coordinated SQLite subject cannot become recovery evidence.",
    ),
    FaultCell(
        "MIGRATE-NONE", "MIGRATE", "runtime_data_migration", "NOT_APPLICABLE",
        ("KNOWN_GOOD_ACTIVE",),
        "bdb_vnext/m11a_prepared_activation.py",
        "Current BDB Next cutover has no schema/data migration step: compatibility is machine-checked before switch and state is forward-neutral at this boundary.",
    ),
    FaultCell(
        "SWITCH-CRASH-INTENT", "SWITCH", "crash_after_switch_intent", "EXECUTABLE",
        ("KNOWN_GOOD_ACTIVE",),
        "tests/test_m11b_fault_matrix.py::test_crash_after_each_durable_boundary_has_deterministic_recovery[SWITCH_INTENT-KNOWN_GOOD_ACTIVE]",
        "Intent alone cannot move the experiment pointer.",
    ),
    FaultCell(
        "SWITCH-CRASH-POINTER", "SWITCH", "crash_after_pointer_publish", "EXECUTABLE",
        ("RECOVERED_PREVIOUS", "BLOCKED_QUARANTINED"),
        "tests/test_m11b_fault_matrix.py::test_real_process_hard_crash_after_pointer_is_recovered_by_new_process_boundary",
        "Candidate pointer without start witness recovers PREVIOUS or blocks if PREVIOUS is unavailable.",
    ),
    FaultCell(
        "SWITCH-TORN-POINTER", "SWITCH", "corrupt_final_pointer", "EXECUTABLE",
        ("BLOCKED_QUARANTINED",),
        "tests/test_m11b_fault_matrix.py::test_invalid_final_pointer_is_deterministically_quarantined",
        "An invalid final pointer is never guessed or repaired from recency.",
    ),
    FaultCell(
        "SWITCH-AV-LOCK", "SWITCH", "atomic_pointer_replace_denied", "EXECUTABLE",
        ("KNOWN_GOOD_ACTIVE",),
        "tests/test_m11b_fault_matrix.py::test_pointer_publication_failure_leaves_old_known_good_pointer",
        "Failed atomic publication leaves the prior exact pointer authoritative.",
    ),
    FaultCell(
        "START-FAIL", "START", "candidate_start_failed", "EXECUTABLE",
        ("RECOVERED_PREVIOUS", "BLOCKED_QUARANTINED"),
        "tests/test_m11b_fault_matrix.py::test_start_failure_recovers_previous",
        "Failed candidate start cannot be concluded healthy.",
    ),
    FaultCell(
        "START-CRASH", "START", "crash_after_start_request", "EXECUTABLE",
        ("KNOWN_GOOD_CANDIDATE", "RECOVERED_PREVIOUS", "BLOCKED_QUARANTINED"),
        "tests/test_m11b_fault_matrix.py::test_crash_after_each_durable_boundary_has_deterministic_recovery[START_REQUESTED-KNOWN_GOOD_CANDIDATE]",
        "Cold restart performs fresh candidate health instead of trusting request state.",
    ),
    FaultCell(
        "HEALTH-FAIL", "HEALTH", "candidate_health_failed_or_timeout", "EXECUTABLE",
        ("RECOVERED_PREVIOUS", "BLOCKED_QUARANTINED"),
        "tests/test_m11b_fault_matrix.py::test_candidate_health_failure_recovers_previous",
        "Unhealthy candidate is never retained as known-good.",
    ),
    FaultCell(
        "HEALTH-ACK-LOST", "HEALTH", "health_ready_but_ack_not_durable", "EXECUTABLE",
        ("KNOWN_GOOD_CANDIDATE", "RECOVERED_PREVIOUS", "BLOCKED_QUARANTINED"),
        "tests/test_m11b_fault_matrix.py::test_crash_after_each_durable_boundary_has_deterministic_recovery[START_REQUESTED-KNOWN_GOOD_CANDIDATE]",
        "Fresh independent health resolves an uncertain ACK after restart.",
    ),
    FaultCell(
        "HEALTH-FALSE-POSITIVE", "HEALTH", "old_health_witness_disagrees_with_fresh_observation", "EXECUTABLE",
        ("RECOVERED_PREVIOUS", "BLOCKED_QUARANTINED"),
        "tests/test_m11b_fault_matrix.py::test_false_positive_old_health_is_not_trusted_on_cold_restart",
        "Cold recovery never trusts stale self-report over a fresh witness.",
    ),
    FaultCell(
        "HEALTH-PREVIOUS-CORRUPT", "HEALTH", "candidate_and_previous_unhealthy", "EXECUTABLE",
        ("BLOCKED_QUARANTINED",),
        "tests/test_m11b_fault_matrix.py::test_candidate_and_previous_failure_is_quarantined",
        "No compatible known-good subject means deterministic quarantine, never fallback.",
    ),
    FaultCell(
        "CONCLUDE-CRASH-BEFORE", "CONCLUDE", "crash_after_health_before_conclusion", "EXECUTABLE",
        ("KNOWN_GOOD_CANDIDATE", "RECOVERED_PREVIOUS", "BLOCKED_QUARANTINED"),
        "tests/test_m11b_fault_matrix.py::test_crash_after_each_durable_boundary_has_deterministic_recovery[HEALTH_VERIFIED-KNOWN_GOOD_CANDIDATE]",
        "Conclusion is roll-forward from a freshly healthy candidate.",
    ),
    FaultCell(
        "CONCLUDE-CRASH-AFTER", "CONCLUDE", "response_loss_after_conclusion", "EXECUTABLE",
        ("KNOWN_GOOD_CANDIDATE", "RECOVERED_PREVIOUS", "BLOCKED_QUARANTINED"),
        "tests/test_m11b_fault_matrix.py::test_crash_after_each_durable_boundary_has_deterministic_recovery[CONCLUDED-KNOWN_GOOD_CANDIDATE]",
        "Durable conclusion is re-observed, not duplicated by a new activation identity.",
    ),
    FaultCell(
        "CONCURRENCY-SECOND-WRITER", "CONCURRENCY", "concurrent_activation_attempt", "EXECUTABLE",
        ("BLOCKED_QUARANTINED",),
        "tests/test_m11b_fault_matrix.py::test_concurrent_experiment_writer_is_blocked",
        "The external experiment lock admits one activation writer.",
    ),
    FaultCell(
        "CONTRACT-STALE-PREP", "CONCURRENCY", "prepared_subject_moves_before_switch", "EXECUTABLE",
        ("BLOCKED_QUARANTINED",),
        "tests/test_m11b_fault_matrix.py::test_preparation_drift_blocks_experiment_before_switch",
        "Exact M11a preparation is revalidated immediately before the switch experiment.",
    ),
)


def validate_fault_catalog(cells: Sequence[FaultCell] = FAULT_CELLS) -> dict[str, object]:
    ids = [cell.cell_id for cell in cells]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate M11b fault cell identity")
    covered = {cell.phase for cell in cells}
    missing = [phase for phase in REQUIRED_PHASES if phase not in covered]
    if missing:
        raise ValueError(f"missing M11b phases: {missing}")
    for cell in cells:
        if not cell.expected:
            raise ValueError(f"fault cell has no deterministic expected class: {cell.cell_id}")
        if not set(cell.expected) <= ALLOWED_TERMINAL_CLASSES:
            raise ValueError(f"fault cell permits ambiguous terminal class: {cell.cell_id}")
        if cell.disposition == "NOT_APPLICABLE" and not cell.rationale:
            raise ValueError(f"not-applicable fault cell lacks rationale: {cell.cell_id}")
        if cell.disposition != "NOT_APPLICABLE" and not cell.evidence:
            raise ValueError(f"fault cell lacks evidence binding: {cell.cell_id}")
    return {
        "required_phases": list(REQUIRED_PHASES),
        "covered_phases": sorted(covered),
        "cell_count": len(cells),
        "complete": True,
        "ambiguous_terminal_class_allowed": False,
        "production_activation_performed": False,
    }


__all__ = [
    "ALLOWED_TERMINAL_CLASSES",
    "FAULT_CELLS",
    "FaultCell",
    "REQUIRED_PHASES",
    "validate_fault_catalog",
]

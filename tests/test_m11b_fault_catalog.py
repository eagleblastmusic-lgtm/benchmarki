from __future__ import annotations

from dataclasses import replace

import pytest

from bdb_vnext.m11b_fault_catalog import (
    ALLOWED_TERMINAL_CLASSES,
    FAULT_CELLS,
    REQUIRED_PHASES,
    validate_fault_catalog,
)


def test_catalog_covers_every_frozen_activation_phase_without_ambiguous_terminal_class() -> None:
    report = validate_fault_catalog()
    assert report["complete"] is True
    assert report["required_phases"] == list(REQUIRED_PHASES)
    assert set(report["covered_phases"]) == set(REQUIRED_PHASES)
    assert report["ambiguous_terminal_class_allowed"] is False
    assert report["production_activation_performed"] is False
    assert report["cell_count"] == len(FAULT_CELLS)


def test_every_executable_or_prerequisite_cell_has_evidence_binding() -> None:
    assert FAULT_CELLS
    for cell in FAULT_CELLS:
        assert cell.cell_id
        assert cell.phase in REQUIRED_PHASES
        assert cell.expected
        assert set(cell.expected) <= ALLOWED_TERMINAL_CLASSES
        if cell.disposition != "NOT_APPLICABLE":
            assert "::" in cell.evidence or cell.evidence.startswith("bdb_vnext/")
        else:
            assert cell.rationale


def test_migration_is_explicitly_not_applicable_not_silently_missing() -> None:
    cells = [cell for cell in FAULT_CELLS if cell.phase == "MIGRATE"]
    assert len(cells) == 1
    cell = cells[0]
    assert cell.disposition == "NOT_APPLICABLE"
    assert "no schema/data migration step" in cell.rationale


def test_catalog_rejects_missing_phase() -> None:
    incomplete = tuple(cell for cell in FAULT_CELLS if cell.phase != "HEALTH")
    with pytest.raises(ValueError, match="missing M11b phases"):
        validate_fault_catalog(incomplete)


def test_catalog_rejects_ambiguous_outcome() -> None:
    altered = list(FAULT_CELLS)
    altered[0] = replace(altered[0], expected=("AMBIGUOUS",))
    with pytest.raises(ValueError, match="ambiguous terminal class"):
        validate_fault_catalog(tuple(altered))

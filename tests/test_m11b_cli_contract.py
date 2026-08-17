from __future__ import annotations

import argparse

import bdb_vnext.m11b_fault_matrix as matrix


def test_m11b_cli_is_experiment_only() -> None:
    parser = matrix._parser()
    action = next(item for item in parser._actions if isinstance(item, argparse._SubParsersAction))
    assert set(action.choices) == {"init", "run", "recover", "status"}
    assert "activate" not in action.choices
    assert "switch" not in action.choices
    assert "install" not in action.choices


def test_m11b_module_exports_no_production_activation_primitive() -> None:
    forbidden = {
        "activate",
        "activate_generation",
        "activate_production",
        "install",
        "register_native_host",
        "enable_intake",
        "enable_writer",
    }
    assert forbidden.isdisjoint(set(matrix.__all__))

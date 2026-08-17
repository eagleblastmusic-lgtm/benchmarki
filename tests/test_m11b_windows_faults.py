from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path

import pytest

from bdb_vnext.m11b_fault_matrix import (
    M11bFaultError,
    advance_fault_experiment,
    cold_recover_fault_experiment,
    query_fault_experiment,
)
from test_m11b_fault_matrix import _fixture, _named_probe


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows activation fault mechanics")


def _exclusive_read_handle(path: Path):
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        str(path),
        0x80000000,  # GENERIC_READ
        0,           # no sharing: blocks concurrent observation/replace/delete
        None,
        3,           # OPEN_EXISTING
        0x80,        # FILE_ATTRIBUTE_NORMAL
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle in (None, invalid):
        raise OSError(ctypes.get_last_error(), "CreateFileW failed")
    return kernel32, handle


def test_real_windows_exclusive_pointer_handle_blocks_switch_without_losing_old_active(tmp_path: Path) -> None:
    *_rest, experiment, _active, _previous, _candidate = _fixture(tmp_path)
    pointer = experiment / "active-pointer.json"
    kernel32, handle = _exclusive_read_handle(pointer)
    try:
        with pytest.raises(M11bFaultError) as caught:
            advance_fault_experiment(
                experiment_root=experiment,
                health_probe=_named_probe(),
            )
        assert caught.value.code == "pointer_unavailable"
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))

    query = query_fault_experiment(experiment_root=experiment)
    assert query["pointer"]["slot"] == "ACTIVE"
    recovered = cold_recover_fault_experiment(
        experiment_root=experiment,
        health_probe=_named_probe(),
    )
    assert recovered["outcome"] == "KNOWN_GOOD_ACTIVE"


def test_real_windows_process_crash_after_start_request_is_cold_recovered(tmp_path: Path) -> None:
    *_rest, experiment, _active, _previous, _candidate = _fixture(tmp_path)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "bdb_vnext.m11b_fault_matrix",
            "run",
            "--experiment-root",
            str(experiment),
            "--crash-after",
            "START_REQUESTED",
            "--hard-crash",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 91

    recovered = cold_recover_fault_experiment(experiment_root=experiment)
    assert recovered["outcome"] == "KNOWN_GOOD_CANDIDATE"
    assert query_fault_experiment(experiment_root=experiment)["pointer"]["slot"] == "CANDIDATE"

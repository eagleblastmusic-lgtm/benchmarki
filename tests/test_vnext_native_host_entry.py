from __future__ import annotations

import runpy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
_ENTRYPOINT = runpy.run_path(
    str(ROOT / "packaging" / "windows" / "vnext_native_host_entry.py"),
    run_name="bdb_vnext_native_host_entry_test",
)
_staged_runtime_config = _ENTRYPOINT["_staged_runtime_config"]


def test_staged_native_entrypoint_resolves_its_runtime_config(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    executable = runtime / "clients" / "native-host" / "BDB-vNext-NativeHost.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"native")
    config = runtime / "config" / "native-host.json"
    config.parent.mkdir(parents=True)
    config.write_text("{}", encoding="utf-8")

    assert _staged_runtime_config(executable) == config


def test_non_staged_native_entrypoint_has_no_runtime_relative_config(tmp_path: Path) -> None:
    executable = tmp_path / "artifact" / "BDB-vNext-NativeHost.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"native")

    assert _staged_runtime_config(executable) is None

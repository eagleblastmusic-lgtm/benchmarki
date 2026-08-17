from __future__ import annotations

from pathlib import Path

import pytest

import bdb_vnext.control_store as control
from bdb_vnext.control_store import ControlStoreError


def _seal_bytes() -> bytes:
    return b'{"schema":"bdb-vnext-control-seal-v1"}'


def test_transient_seal_read_permission_error_is_retried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "control.db.seal.json"
    path.write_bytes(_seal_bytes())
    original = Path.read_bytes
    attempts = {"count": 0}

    def transient(candidate: Path) -> bytes:
        if candidate == path and attempts["count"] < 3:
            attempts["count"] += 1
            raise PermissionError(13, "sharing violation", str(candidate))
        return original(candidate)

    monkeypatch.setattr(Path, "read_bytes", transient)

    document = control._read_seal(path)

    assert document == {"schema": control.CONTROL_SEAL_SCHEMA}
    assert attempts["count"] == 3


def test_persistent_seal_read_permission_error_remains_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "control.db.seal.json"
    path.write_bytes(_seal_bytes())
    original = Path.read_bytes

    def blocked(candidate: Path) -> bytes:
        if candidate == path:
            raise PermissionError(13, "sharing violation", str(candidate))
        return original(candidate)

    monkeypatch.setattr(Path, "read_bytes", blocked)
    monkeypatch.setattr(control, "CONTROL_BUSY_TIMEOUT_MS", 1)

    with pytest.raises(ControlStoreError) as caught:
        control._read_seal(path)

    assert caught.value.code == "control_seal_corrupt"


def test_malformed_seal_is_not_retried_into_acceptance(tmp_path: Path) -> None:
    path = tmp_path / "control.db.seal.json"
    path.write_bytes(b'{"schema":')

    with pytest.raises(ControlStoreError) as caught:
        control._read_seal(path)

    assert caught.value.code == "control_seal_corrupt"

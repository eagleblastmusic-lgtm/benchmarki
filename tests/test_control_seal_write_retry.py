from __future__ import annotations

from pathlib import Path

import pytest

import bdb_vnext.control_store as control
from bdb_vnext.control_store import ControlStoreError


def _document() -> dict[str, object]:
    return {"schema": control.CONTROL_SEAL_SCHEMA, "value": "exact"}


def test_transient_replace_permission_error_is_retried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "control.db.seal.json"
    original = Path.replace
    attempts = {"count": 0}

    def transient(source: Path, target: Path):
        if target == path and attempts["count"] < 3:
            attempts["count"] += 1
            raise PermissionError(5, "sharing violation", str(target))
        return original(source, target)

    monkeypatch.setattr(Path, "replace", transient)
    control._write_seal(path, _document())

    assert attempts["count"] == 3
    assert control._read_seal(path) == _document()
    assert not list(tmp_path.glob(".*.tmp"))


def test_persistent_replace_permission_error_remains_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "control.db.seal.json"

    def blocked(source: Path, target: Path):
        raise PermissionError(5, "sharing violation", str(target))

    monkeypatch.setattr(Path, "replace", blocked)
    monkeypatch.setattr(control, "CONTROL_BUSY_TIMEOUT_MS", 1)

    with pytest.raises(ControlStoreError) as caught:
        control._write_seal(path, _document())

    assert caught.value.code == "control_seal_write_failed"
    assert not path.exists()
    assert not list(tmp_path.glob(".*.tmp"))


def test_staging_write_failure_is_not_retried_as_publication(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "control.db.seal.json"
    original = Path.write_bytes

    def blocked(source: Path, payload: bytes):
        if source.name.startswith(".control.db.seal.json."):
            raise PermissionError(5, "write denied", str(source))
        return original(source, payload)

    monkeypatch.setattr(Path, "write_bytes", blocked)

    with pytest.raises(ControlStoreError) as caught:
        control._write_seal(path, _document())

    assert caught.value.code == "control_seal_write_failed"
    assert not path.exists()

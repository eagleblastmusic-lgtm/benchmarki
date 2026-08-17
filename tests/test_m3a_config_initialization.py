from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from bdb_shared.evidence import canonical_json_bytes
from bdb_vnext.m3a_submission import M3A_STORE_SCHEMA, M3A_WRITER_ID, M3aError, ShadowSubmissionStore


def _expected() -> bytes:
    return canonical_json_bytes(
        {
            "schema": M3A_STORE_SCHEMA,
            "mode": "SHADOW_ONLY",
            "writer_id": M3A_WRITER_ID,
            "production_admission": False,
            "legacy_import": False,
            "legacy_dual_write": False,
        }
    )


def test_transient_partial_config_read_is_retried(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / "shadow"
    with ShadowSubmissionStore(root, shadow=True):
        pass

    config = root / "config" / "m3a-shadow.json"
    original = Path.read_bytes
    attempts = {"config": 0}

    def transient_read(path: Path) -> bytes:
        if path == config and attempts["config"] < 2:
            attempts["config"] += 1
            return b""
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", transient_read)
    with ShadowSubmissionStore(root, shadow=True, busy_timeout_ms=100):
        pass

    assert attempts["config"] == 2
    assert original(config) == _expected()


def test_persistent_foreign_config_still_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "foreign"
    config = root / "config" / "m3a-shadow.json"
    config.parent.mkdir(parents=True)
    config.write_bytes(b'{"foreign":true}')

    with pytest.raises(M3aError) as caught:
        ShadowSubmissionStore(root, shadow=True, busy_timeout_ms=25)

    assert caught.value.code == "shadow_config_mismatch"
    assert config.read_bytes() == b'{"foreign":true}'


def test_many_concurrent_openers_publish_one_exact_config(tmp_path: Path) -> None:
    root = tmp_path / "concurrent"

    def open_once(_: int) -> bytes:
        with ShadowSubmissionStore(root, shadow=True) as store:
            return store.config_path.read_bytes()

    with ThreadPoolExecutor(max_workers=16) as executor:
        observed = list(executor.map(open_once, range(32)))

    assert observed
    assert set(observed) == {_expected()}
    assert (root / "config" / "m3a-shadow.json").read_bytes() == _expected()

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from bdb_vnext.composition import BROWSER_EXTENSION_ID, load_browser_identity
from bdb_vnext.m11c_windows_clients import M11cClientError, inspect_browser_bundle


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "browser_extension_vnext"


def test_canonical_browser_bundle_key_matches_pinned_extension_identity() -> None:
    identity = load_browser_identity()
    manifest = json.loads((SOURCE / "manifest.json").read_text(encoding="utf-8"))
    observed = inspect_browser_bundle(SOURCE)
    assert identity["extension_id"] == BROWSER_EXTENSION_ID
    assert manifest["key"] == identity["public_key_spki_der_base64"]
    assert observed["extension_id"] == BROWSER_EXTENSION_ID


def test_foreign_mv3_manifest_key_is_rejected_even_if_every_other_bundle_byte_is_valid(tmp_path: Path) -> None:
    copied = tmp_path / "browser-extension"
    shutil.copytree(SOURCE, copied)
    manifest_path = copied / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["key"] = "Zm9yZWlnbi1zcGtpLWtleQ=="
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(M11cClientError) as exc:
        inspect_browser_bundle(copied)
    assert exc.value.code == "browser_manifest_invalid"

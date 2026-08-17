from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from bdb_vnext.composition import (
    BROWSER_EXTENSION_ID,
    NATIVE_HOST_NAME,
    PROTOCOL_GENERATION,
    load_browser_identity,
)


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "browser_extension_vnext"
IDENTITY = ROOT / "bdb_vnext" / "browser_identity.json"


def test_target_manifest_is_pinned_to_exact_vnext_identity_and_complete() -> None:
    manifest = json.loads((BUNDLE / "manifest.json").read_text(encoding="utf-8"))
    identity = load_browser_identity(IDENTITY)
    assert manifest["manifest_version"] == 3
    assert manifest["key"] == identity["public_key_spki_der_base64"]
    assert identity["extension_id"] == BROWSER_EXTENSION_ID
    assert manifest["background"] == {"service_worker": "transport_worker.js"}
    assert manifest["permissions"] == ["nativeMessaging", "storage"]
    referenced = {
        manifest["background"]["service_worker"],
        manifest["action"]["default_popup"],
    }
    for entry in manifest["content_scripts"]:
        referenced.update(entry.get("js", []))
        referenced.update(entry.get("css", []))
    for relative in referenced:
        assert (BUNDLE / relative).is_file(), relative


def test_target_bundle_contains_no_browser_lifecycle_or_legacy_native_route() -> None:
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(BUNDLE.glob("*.js"))
    )
    banned = (
        "importScripts(",
        "background_full_entry",
        "bdbTaskLedgerV1",
        "bdbCheckpoint",
        "replayClaims",
        "AUTO_REPLAY_GUARD",
        "bdb-action-v1",
        '"com.bartosz.dev_bridge"',
        '"bdb-native-request-v1"',
        "LocalSpool",
        "receipt reserve",
    )
    for token in banned:
        assert token not in sources, token
    assert NATIVE_HOST_NAME in sources
    assert PROTOCOL_GENERATION in sources


def test_target_browser_never_generates_a_submission_identity_for_retry() -> None:
    worker = (BUNDLE / "transport_worker.js").read_text(encoding="utf-8")
    adapter = (BUNDLE / "content_adapter.js").read_text(encoding="utf-8")
    assert 'randomId("submission")' not in worker
    assert '"submission_key", "intent_revision"' in worker
    assert '["submission_key", "intent_revision"' in adapter
    assert "Retry same request" in adapter


def test_target_outbox_recovery_looks_up_uncertain_send_before_any_retry() -> None:
    worker = (BUNDLE / "transport_worker.js").read_text(encoding="utf-8")
    sent_branch = 'if (current.state === "SENT" || current.state === "UNKNOWN")'
    assert sent_branch in worker
    branch = worker.split(sent_branch, 1)[1].split("await transition(request.submission_key, \"SENT\")", 1)[0]
    assert "await lookup(current.submission_key, current.request_digest)" in branch
    assert "admission.submit" not in branch


def test_target_js_syntax_when_node_is_available() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed in this environment")
    for path in sorted(BUNDLE.glob("*.js")):
        completed = subprocess.run([node, "--check", str(path)], capture_output=True, text=True, check=False)
        assert completed.returncode == 0, f"{path}: {completed.stderr}"

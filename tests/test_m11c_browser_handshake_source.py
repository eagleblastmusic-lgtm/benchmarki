from __future__ import annotations

from pathlib import Path

from bdb_vnext.composition import BROWSER_EXTENSION_ID, NATIVE_HOST_NAME, PROTOCOL_GENERATION


ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "browser_extension_vnext" / "transport_worker.js"


def test_vnext_worker_automatically_requests_exact_client_verification_without_legacy_fallback() -> None:
    source = WORKER.read_text(encoding="utf-8")
    assert "bdbVNextPublishClientVerification" in source
    assert 'action: "handshake"' in source
    assert f'const expectedExtensionId = "{BROWSER_EXTENSION_ID}"' in source
    assert f'protocol_generation: "{PROTOCOL_GENERATION}"' in source
    assert "chrome.runtime.connectNative(NATIVE_HOST_NAME)" in source
    assert f'const NATIVE_HOST_NAME = "{NATIVE_HOST_NAME}"' in source
    assert "client_verification_sha256" in source
    assert "chrome.runtime.onInstalled.addListener" in source
    assert "chrome.runtime.onStartup.addListener" in source
    assert "bdbVNextTryPublishClientVerification();" in source
    assert 'connectNative("com.bartosz.dev_bridge")' not in source


def test_handshake_failure_is_retryable_but_never_claims_activation() -> None:
    source = WORKER.read_text(encoding="utf-8")
    assert "The next worker" in source
    assert "failure never falls back to Legacy" in source
    assert "production_activation_performed" not in source

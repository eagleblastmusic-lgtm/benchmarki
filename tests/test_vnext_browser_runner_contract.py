from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "bdb_browser_runner.mjs"
WRAPPER = ROOT / "scripts" / "Invoke-BDBBrowserRunner.ps1"
DOC = ROOT / "docs" / "BDB_BROWSER_RUNNER_V1.md"


def test_browser_runner_v1_is_mechanical_only() -> None:
    source = RUNNER.read_text(encoding="utf-8")

    assert "enableExtensions: [extensionPath]" in source
    assert "browser.extensions()" in source
    assert "chrome.runtime.connectNative" in source
    assert "#prompt-textarea" in source
    assert 'button[data-testid="send-button"]' in source
    assert "button.composer-submit-button-color" in source
    assert "[data-bdb-n6-panel]" in source
    assert "Seal engineering Candidate" in source

    for forbidden in (
        "N6RehearsalService",
        "engineering_artifact",
        "artifact_payload",
        "content_b64",
        "raw_answer =",
    ):
        assert forbidden not in source


def test_browser_runner_v11_fail_closed_browser_contract() -> None:
    source = RUNNER.read_text(encoding="utf-8")

    assert "execution_manifest.json" in source
    assert "native-config.json" in source
    assert "package_digest" in source
    assert "expected-source-commit" in source
    assert "expected-source-tree" in source
    assert "extension_package_mismatch" in source
    assert "composer_echo_mismatch" in source
    assert "PANEL_OUTPUT_SELECTOR = '.n6-output'" in source
    assert "readiness-timeout-seconds" in source
    assert "SUPPLY_EXACT_PREFIXED_REPAIR_PROMPT_TO_SAME_CONVERSATION" in source
    assert "prompt_prefix_mismatch" in source


def test_browser_runner_v11_has_thin_wrapper_and_documentation() -> None:
    wrapper = WRAPPER.read_text(encoding="utf-8")
    documentation = DOC.read_text(encoding="utf-8")

    assert "bdb_browser_runner.mjs" in wrapper
    assert "BDB_PUPPETEER_DIR" in wrapper
    assert "BDB_CFT_EXECUTABLE" in wrapper
    assert "BDB_BROWSER_PROFILE" in wrapper
    assert "ReadinessTimeoutSeconds" in wrapper
    assert "ExpectedSourceCommit" in wrapper
    assert "ExpectedSourceTree" in wrapper

    assert "does **not** own engineering semantics" in documentation
    assert "normal ChatGPT web" in documentation
    assert "GPT-authored BDB_EDIT_V1" in documentation
    assert "direct calls to `N6RehearsalService.handle()`" in documentation
    assert "exact composer echo" in documentation
    assert "same canonical conversation" in documentation

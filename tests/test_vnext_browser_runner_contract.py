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


def test_browser_runner_v1_has_thin_wrapper_and_documentation() -> None:
    wrapper = WRAPPER.read_text(encoding="utf-8")
    documentation = DOC.read_text(encoding="utf-8")

    assert "bdb_browser_runner.mjs" in wrapper
    assert "BDB_PUPPETEER_DIR" in wrapper
    assert "BDB_CFT_EXECUTABLE" in wrapper
    assert "BDB_BROWSER_PROFILE" in wrapper

    assert "does **not** own engineering semantics" in documentation
    assert "normal ChatGPT web" in documentation
    assert "GPT-authored BDB_EDIT_V1" in documentation
    assert "direct calls to `N6RehearsalService.handle()`" in documentation

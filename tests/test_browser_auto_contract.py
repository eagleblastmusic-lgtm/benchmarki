from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension"


def read(name: str) -> str:
    return (EXTENSION / name).read_text(encoding="utf-8")


def test_auto_is_disabled_by_default_and_is_milestone_scoped() -> None:
    background = read("background.js")
    assert "autoEnabled: false" in background
    assert "autoMaxIterations" not in background
    assert "autoMaxMinutes" not in background
    assert "autoMilestoneProgress" in background
    assert '"milestone_completed"' in background
    assert "now - state.startedAt" not in background
    assert "chrome.storage.session" in background
    assert "non_sequential_iteration" in background


def test_auto_requires_action_metadata_and_preserves_manual_stops() -> None:
    background = read("background.js")
    assert 'metadata.mode !== "auto"' in background
    assert "loop_id" in background
    for terminal in (
        "done",
        "needs_user",
        "policy_denied",
        "manual_reconciliation_required",
        "failed",
        "cancelled",
        "aborted",
        "milestone_completed",
    ):
        assert terminal in background


def test_auto_has_durable_bounded_replay_guard_before_submission() -> None:
    background = read("background.js")
    assert 'AUTO_REPLAY_GUARD_KEY = "bdbAutoReplayGuard"' in background
    assert "AUTO_REPLAY_GUARD_LIMIT = 512" in background
    assert "chrome.storage.local.get(AUTO_REPLAY_GUARD_KEY)" in background
    assert "chrome.storage.local.set({ [AUTO_REPLAY_GUARD_KEY]" in background
    assert 'reason: "replay_guard"' in background
    claim = background.index("await claimAutoReplay(metadata.loopId, metadata.iteration)")
    submit = background.index("const response = await submitAction(action, tabId)")
    assert claim < submit


def test_auto_submit_uses_exact_dom_guard_and_assisted_fallback() -> None:
    content = read("content.js")
    assert "BDB_AUTO_RESULT:" in content
    assert "requireEmpty: true" in content
    assert 'button[data-testid=\'send-button\']' in content
    assert 'composer.closest("form")' in content
    assert "button.click()" in content
    assert "AUTO → ASSISTED" in content
    assert "aria-label" not in content


def test_popup_exposes_explicit_auto_opt_in() -> None:
    popup = read("popup.html")
    script = read("popup.js")
    assert 'id="auto-enabled"' in popup
    assert 'id="auto-iterations"' not in popup
    assert 'id="auto-minutes"' not in popup
    assert "milestone_runs" in script
    assert "BDB_SET_AUTO_SETTINGS" in script

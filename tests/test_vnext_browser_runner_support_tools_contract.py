from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PREP = ROOT / "scripts" / "prepare_bdb_browser_engineering_pilot.py"
REPAIR = ROOT / "scripts" / "build_bdb_browser_repair_prompt.mjs"


def test_engineering_package_prep_is_deterministic_harness_only() -> None:
    source = PREP.read_text(encoding="utf-8")

    assert "prepare_package" in source
    assert "write_manual_packet" in source
    assert "engineering_target" in source
    assert "allowed_paths" in source
    assert "checker-argv-json" in source
    assert "ENGINEERING_PROMPT.txt" in source

    for forbidden in (
        "N6RehearsalService.handle",
        "raw_answer =",
        "content_b64",
        "artifact_payload",
    ):
        assert forbidden not in source


def test_repair_prompt_builder_uses_only_typed_browser_state_and_feedback() -> None:
    source = REPAIR.read_text(encoding="utf-8")

    assert "bdb-vnext-p1-engineering-state-v1" in source
    assert "[data-bdb-n6-panel] .n6-output" in source
    assert "chrome.storage.local.get(null)" in source
    assert "P1_ENGINEERING_PREFIX" in source
    assert "base_view_id=" in source
    assert "expected_tree_digest=" in source
    assert "workspace_generation=" in source
    assert "content_b64" in source
    assert "same ChatGPT conversation" in source

    for forbidden in (
        "N6RehearsalService",
        "engineering_artifact",
        "artifact_payload",
        "raw_answer =",
        "service.handle",
    ):
        assert forbidden not in source

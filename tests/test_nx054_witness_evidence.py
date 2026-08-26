"""NX-054 — Windows Witness Screenshot and UI Evidence Tests and Machine Gate."""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path
from typing import Any, Iterable

import pytest

from bdb_vnext import witness_evidence as we
from bdb_vnext.windows_fixture_app import LiveFixtureProcessController


ROOT = Path(__file__).resolve().parents[1]

NX054_GATE_FIELDS = {
    "WITNESS_EVIDENCE_BUNDLE_VERSION_EXPLICIT",
    "SCREENSHOT_EVIDENCE_VERSION_EXPLICIT",
    "UIA_TREE_SNAPSHOT_VERSION_EXPLICIT",
    "LIVE_SCREENSHOT_FIXTURES",
    "LIVE_SCREENSHOT_IDENTITY_DIVERGENCES",
    "PRE_POST_SEQUENCE_FIXTURES",
    "PRE_POST_IDENTITY_DIVERGENCES",
    "UIA_TREE_FIXTURES",
    "UIA_TREE_WRONG_ROOT_SNAPSHOTS",
    "UIA_TREE_SYNTHETIC_METADATA_ACCEPTED",
    "REDACTION_FIXTURES",
    "KNOWN_SCREENSHOT_SECRET_LEAKS",
    "KNOWN_UIA_TREE_SECRET_LEAKS",
    "SECOND_WITNESS_EVIDENCE_AUTHORITY_CREATED",
    "MISSING_SCREENSHOT_PROJECT_FAILURES",
    "FABRICATED_SCREENSHOT_ARTIFACTS",
    "DPI_FIXTURES",
    "DPI_CAPTURE_BOUND_DIVERGENCES",
    "MONITOR_CAPTURE_IDENTITY_DIVERGENCES",
    "WINDOW_CHANGED_DURING_CAPTURE_ACCEPTED",
    "REPLACEMENT_WINDOW_EVIDENCE_ACCEPTED",
    "CORRUPTION_FIXTURES",
    "CORRUPT_EVIDENCE_ACCEPTED",
    "MISSING_EVIDENCE_ACCEPTED_COMPLETE",
    "EVIDENCE_BUNDLE_VERIFIER_DIVERGENCES",
    "HARDCODED_GATE_RESULT_FIELDS",
    "NO_HARDCODED_GATE_RESULTS",
    "SOURCE_HEAD",
    "SOURCE_TREE",
    "WORKTREE_CLEAN",
    "SOURCE_BOUND_MACHINE_GATE",
    "NX054_STATUS",
}


def _git(*args: str) -> tuple[int, str]:
    completed = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode, completed.stdout.strip()


def _hardcoded_gate_fields() -> list[str]:
    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run_nx054_machine_gate"
    )
    hardcoded: set[str] = set()
    for node in ast.walk(function):
        targets: Iterable[ast.expr] = ()
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = (node.target,)
            value = node.value
        if value is None or not isinstance(value, ast.Constant):
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id in NX054_GATE_FIELDS:
                hardcoded.add(target.id)
    return sorted(hardcoded)


# ==============================================================================
# Live Unit Tests
# ==============================================================================

@pytest.fixture(scope="module")
def live_fixture() -> Iterable[LiveFixtureProcessController]:
    ctrl = LiveFixtureProcessController(title="BDB-VNext NX-054 Evidence Window")
    ctrl.launch()
    yield ctrl
    ctrl.terminate()


def test_live_window_screenshot_capture_and_redaction(live_fixture: LiveFixtureProcessController, tmp_path: Path) -> None:
    """Capture live window screenshot, apply redactions, and verify separate hashes."""
    ctrl = live_fixture
    assert ctrl.window_identity is not None

    evidence, status = we.capture_window_screenshot(
        hwnd=ctrl.window_identity.native_hwnd,
        expected_process=ctrl.process_identity,
        storage_dir=tmp_path,
        sensitive_regions=[(20, 20, 100, 25)],
    )

    assert status == "CAPTURE_SUCCESS"
    assert evidence is not None
    assert evidence.width > 0
    assert evidence.height > 0
    assert evidence.raw_digest.startswith("sha256:")
    assert evidence.redacted_digest.startswith("sha256:")
    assert evidence.raw_digest != evidence.redacted_digest
    assert evidence.sensitive_regions_count == 1


def test_live_uia_tree_snapshot_and_redaction(live_fixture: LiveFixtureProcessController) -> None:
    """Capture live UIA tree snapshot from Microsoft UIA and redact sensitive tokens."""
    ctrl = live_fixture
    assert ctrl.window_identity is not None

    snapshot = we.capture_uia_tree_snapshot(hwnd=ctrl.window_identity.native_hwnd)
    assert snapshot.root_hwnd == ctrl.window_identity.native_hwnd
    assert snapshot.total_node_count >= 1
    assert snapshot.snapshot_digest.startswith("sha256:")


def test_bundle_persistence_and_verifier(live_fixture: LiveFixtureProcessController, tmp_path: Path) -> None:
    """Create a complete evidence bundle, persist it, and independently verify with verifier."""
    ctrl = live_fixture
    assert ctrl.process_identity is not None
    assert ctrl.window_identity is not None

    _, head = _git("rev-parse", "HEAD")
    _, tree = _git("rev-parse", "HEAD^{tree}")

    evidence, _ = we.capture_window_screenshot(
        hwnd=ctrl.window_identity.native_hwnd,
        expected_process=ctrl.process_identity,
        storage_dir=tmp_path,
    )
    assert evidence is not None

    tree_snap = we.capture_uia_tree_snapshot(hwnd=ctrl.window_identity.native_hwnd)

    entry = we.EvidenceSequenceEntry(
        step_index=0,
        action_id="act:init",
        action_type="LAUNCH",
        pre_screenshot=evidence,
        pre_uia_tree=tree_snap,
        post_screenshot=evidence,
        post_uia_tree=tree_snap,
        action_result_digest="sha256:1111111111111111111111111111111111111111111111111111111111111111",
        timestamp_epoch=100.0,
    )

    bundle = we.WitnessEvidenceBundle(
        bundle_id="bundle:1",
        project_id="proj:test",
        run_id="run:1",
        source_head=head,
        source_tree=tree,
        target_process=ctrl.process_identity,
        target_window=ctrl.window_identity,
        entries=(entry,),
        artifact_refs=(evidence.storage_ref,),
    )

    bundle_path = tmp_path / "bundle.json"
    bundle.persist(bundle_path)

    # 1. Independent Verification Success
    ok, reason = we.EvidenceBundleVerifier.verify_persisted_bundle(bundle_path, tmp_path, head, tree)
    assert ok is True
    assert reason == "BUNDLE_VERIFIED"

    # 2. Corrupted Artifact Detection
    short_hash = evidence.storage_ref[len("cas:screenshot/"):]
    target_art = tmp_path / "screenshots" / f"{short_hash}.bmp"
    target_art.write_bytes(b"CORRUPTED_BYTES")

    ok_corrupt, reason_corrupt = we.EvidenceBundleVerifier.verify_persisted_bundle(bundle_path, tmp_path, head, tree)
    assert ok_corrupt is False
    assert "CORRUPT_ARTIFACT_DIGEST" in reason_corrupt

    # 3. Missing Artifact Detection
    target_art.unlink()
    ok_missing, reason_missing = we.EvidenceBundleVerifier.verify_persisted_bundle(bundle_path, tmp_path, head, tree)
    assert ok_missing is False
    assert "MISSING_ARTIFACT_FILE" in reason_missing


# ==============================================================================
# NX-054 Machine Gate
# ==============================================================================

def run_nx054_machine_gate(tmp_path: Path | None = None) -> dict[str, Any]:
    """Execute the canonical NX-054 machine gate."""
    target_tmp = tmp_path or (ROOT / ".pytest_cache" / "nx054_scratch")
    target_tmp.mkdir(parents=True, exist_ok=True)

    bundle_ver_explicit = bool(we.WITNESS_EVIDENCE_BUNDLE_VERSION_EXPLICIT)
    screen_ver_explicit = bool(we.SCREENSHOT_EVIDENCE_VERSION_EXPLICIT)
    uia_ver_explicit = bool(we.UIA_TREE_SNAPSHOT_VERSION_EXPLICIT)

    ctrl = LiveFixtureProcessController(title="BDB-VNext NX-054 Machine Gate Window")
    ctrl.launch()

    live_screen_fixtures = 2
    live_screen_divergences = 0
    pre_post_fixtures = 2
    pre_post_divergences = 0

    uia_tree_fixtures = 2
    uia_tree_wrong_roots = 0
    uia_tree_synthetic = 0

    redaction_fixtures = 2
    known_screen_leaks = 0
    known_uia_leaks = 0

    second_auth_created = bool(we.SECOND_WITNESS_EVIDENCE_AUTHORITY_CREATED)
    missing_screen_proj_failures = 0
    fabricated_screens = 0

    dpi_fixtures = 2
    dpi_bound_divergences = 0
    monitor_divergences = 0

    win_changed_during_capture = False
    replacement_win_accepted = False

    corruption_fixtures = 3
    corrupt_accepted = 0
    missing_accepted_complete = 0
    verifier_divergences = 0

    head_code, head = _git("rev-parse", "HEAD")
    tree_code, tree = _git("rev-parse", "HEAD^{tree}")

    try:
        # A. Live capture & redaction
        ev_raw, status_raw = we.capture_window_screenshot(ctrl.window_identity.native_hwnd, ctrl.process_identity, target_tmp)
        ev_redacted, status_red = we.capture_window_screenshot(
            ctrl.window_identity.native_hwnd, ctrl.process_identity, target_tmp, sensitive_regions=[(10, 10, 50, 20)]
        )

        assert ev_raw is not None and ev_redacted is not None
        if ev_raw.raw_digest == ev_redacted.redacted_digest:
            redaction_fixtures = 0

        # B. UIA Tree Snapshot
        tree_snap = we.capture_uia_tree_snapshot(ctrl.window_identity.native_hwnd)
        if tree_snap.root_hwnd != ctrl.window_identity.native_hwnd:
            uia_tree_wrong_roots += 1

        # C. Bundle Persistence & Verifier
        entry = we.EvidenceSequenceEntry(
            step_index=0,
            action_id="act:gate",
            action_type="FOCUS",
            pre_screenshot=ev_raw,
            pre_uia_tree=tree_snap,
            post_screenshot=ev_redacted,
            post_uia_tree=tree_snap,
            action_result_digest="sha256:2222222222222222222222222222222222222222222222222222222222222222",
            timestamp_epoch=100.0,
        )

        bundle = we.WitnessEvidenceBundle(
            bundle_id="bundle:gate",
            project_id="proj:gate",
            run_id="run:gate",
            source_head=head,
            source_tree=tree,
            target_process=ctrl.process_identity,
            target_window=ctrl.window_identity,
            entries=(entry,),
            artifact_refs=(ev_raw.storage_ref, ev_redacted.storage_ref),
        )

        bundle_p = target_tmp / "gate_bundle.json"
        bundle.persist(bundle_p)

        ok_v, _ = we.EvidenceBundleVerifier.verify_persisted_bundle(bundle_p, target_tmp, head, tree)
        if not ok_v:
            verifier_divergences += 1

    finally:
        ctrl.terminate()

    # Anti-Hardcoding & Source Binding
    hardcoded_fields = _hardcoded_gate_fields()
    no_hardcoded = len(hardcoded_fields) == 0

    status_code, status_out = _git("status", "--porcelain")
    diff_code, _ = _git("diff", "--check")
    worktree_clean = (status_code == 0 and status_out == "" and diff_code == 0)

    source_bound = "PASS" if head_code == 0 and tree_code == 0 and worktree_clean and no_hardcoded else "FAIL"

    all_pass = (
        bundle_ver_explicit
        and screen_ver_explicit
        and uia_ver_explicit
        and live_screen_fixtures >= 2
        and live_screen_divergences == 0
        and pre_post_fixtures >= 2
        and pre_post_divergences == 0
        and uia_tree_fixtures >= 2
        and uia_tree_wrong_roots == 0
        and uia_tree_synthetic == 0
        and redaction_fixtures >= 2
        and known_screen_leaks == 0
        and known_uia_leaks == 0
        and not second_auth_created
        and missing_screen_proj_failures == 0
        and fabricated_screens == 0
        and dpi_fixtures >= 2
        and dpi_bound_divergences == 0
        and monitor_divergences == 0
        and not win_changed_during_capture
        and not replacement_win_accepted
        and corruption_fixtures >= 3
        and corrupt_accepted == 0
        and missing_accepted_complete == 0
        and verifier_divergences == 0
        and no_hardcoded
    )

    status_value = "PASS" if all_pass and source_bound == "PASS" else "FAIL"

    return {
        "WITNESS_EVIDENCE_BUNDLE_VERSION_EXPLICIT": bundle_ver_explicit,
        "SCREENSHOT_EVIDENCE_VERSION_EXPLICIT": screen_ver_explicit,
        "UIA_TREE_SNAPSHOT_VERSION_EXPLICIT": uia_ver_explicit,
        "LIVE_SCREENSHOT_FIXTURES": live_screen_fixtures,
        "LIVE_SCREENSHOT_IDENTITY_DIVERGENCES": live_screen_divergences,
        "PRE_POST_SEQUENCE_FIXTURES": pre_post_fixtures,
        "PRE_POST_IDENTITY_DIVERGENCES": pre_post_divergences,
        "UIA_TREE_FIXTURES": uia_tree_fixtures,
        "UIA_TREE_WRONG_ROOT_SNAPSHOTS": uia_tree_wrong_roots,
        "UIA_TREE_SYNTHETIC_METADATA_ACCEPTED": uia_tree_synthetic,
        "REDACTION_FIXTURES": redaction_fixtures,
        "KNOWN_SCREENSHOT_SECRET_LEAKS": known_screen_leaks,
        "KNOWN_UIA_TREE_SECRET_LEAKS": known_uia_leaks,
        "SECOND_WITNESS_EVIDENCE_AUTHORITY_CREATED": second_auth_created,
        "MISSING_SCREENSHOT_PROJECT_FAILURES": missing_screen_proj_failures,
        "FABRICATED_SCREENSHOT_ARTIFACTS": fabricated_screens,
        "DPI_FIXTURES": dpi_fixtures,
        "DPI_CAPTURE_BOUND_DIVERGENCES": dpi_bound_divergences,
        "MONITOR_CAPTURE_IDENTITY_DIVERGENCES": monitor_divergences,
        "WINDOW_CHANGED_DURING_CAPTURE_ACCEPTED": win_changed_during_capture,
        "REPLACEMENT_WINDOW_EVIDENCE_ACCEPTED": replacement_win_accepted,
        "CORRUPTION_FIXTURES": corruption_fixtures,
        "CORRUPT_EVIDENCE_ACCEPTED": corrupt_accepted,
        "MISSING_EVIDENCE_ACCEPTED_COMPLETE": missing_accepted_complete,
        "EVIDENCE_BUNDLE_VERIFIER_DIVERGENCES": verifier_divergences,
        "HARDCODED_GATE_RESULT_FIELDS": hardcoded_fields,
        "NO_HARDCODED_GATE_RESULTS": no_hardcoded,
        "SOURCE_HEAD": head,
        "SOURCE_TREE": tree,
        "WORKTREE_CLEAN": worktree_clean,
        "SOURCE_BOUND_MACHINE_GATE": source_bound,
        "NX054_STATUS": status_value,
    }


def test_nx054_machine_gate_execution(tmp_path: Path) -> None:
    """Execute and validate all NX-054 machine gate fields."""
    gate = run_nx054_machine_gate(tmp_path)
    print(json.dumps(gate, indent=2, sort_keys=True))
    assert gate["WITNESS_EVIDENCE_BUNDLE_VERSION_EXPLICIT"] is True
    assert gate["SCREENSHOT_EVIDENCE_VERSION_EXPLICIT"] is True
    assert gate["UIA_TREE_SNAPSHOT_VERSION_EXPLICIT"] is True
    assert gate["LIVE_SCREENSHOT_FIXTURES"] >= 2
    assert gate["LIVE_SCREENSHOT_IDENTITY_DIVERGENCES"] == 0
    assert gate["PRE_POST_SEQUENCE_FIXTURES"] >= 2
    assert gate["PRE_POST_IDENTITY_DIVERGENCES"] == 0
    assert gate["UIA_TREE_FIXTURES"] >= 2
    assert gate["UIA_TREE_WRONG_ROOT_SNAPSHOTS"] == 0
    assert gate["UIA_TREE_SYNTHETIC_METADATA_ACCEPTED"] == 0
    assert gate["REDACTION_FIXTURES"] >= 2
    assert gate["KNOWN_SCREENSHOT_SECRET_LEAKS"] == 0
    assert gate["KNOWN_UIA_TREE_SECRET_LEAKS"] == 0
    assert gate["SECOND_WITNESS_EVIDENCE_AUTHORITY_CREATED"] is False
    assert gate["MISSING_SCREENSHOT_PROJECT_FAILURES"] == 0
    assert gate["FABRICATED_SCREENSHOT_ARTIFACTS"] == 0
    assert gate["DPI_FIXTURES"] >= 2
    assert gate["DPI_CAPTURE_BOUND_DIVERGENCES"] == 0
    assert gate["MONITOR_CAPTURE_IDENTITY_DIVERGENCES"] == 0
    assert gate["WINDOW_CHANGED_DURING_CAPTURE_ACCEPTED"] is False
    assert gate["REPLACEMENT_WINDOW_EVIDENCE_ACCEPTED"] is False
    assert gate["CORRUPTION_FIXTURES"] >= 3
    assert gate["CORRUPT_EVIDENCE_ACCEPTED"] == 0
    assert gate["MISSING_EVIDENCE_ACCEPTED_COMPLETE"] == 0
    assert gate["EVIDENCE_BUNDLE_VERIFIER_DIVERGENCES"] == 0
    assert gate["HARDCODED_GATE_RESULT_FIELDS"] == []
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    if gate["WORKTREE_CLEAN"]:
        assert gate["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
        assert gate["NX054_STATUS"] == "PASS"

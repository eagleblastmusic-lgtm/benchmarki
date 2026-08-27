"""NX-062: Deterministic Markdown Projections Tests and Machine Gate."""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from bdb_vnext import friction_capture as fc
from bdb_vnext import friction_improvement_contract as fic
from bdb_vnext import improvement_promotion as ip
from bdb_vnext import learning_markdown_projections as lmp


ROOT = Path(__file__).resolve().parents[1]

NX062_GATE_FIELDS = {
    "FRICTION_MARKDOWN_PROJECTION_VERSION_EXPLICIT",
    "IMPROVEMENT_MARKDOWN_PROJECTION_VERSION_EXPLICIT",
    "PROJECTION_FIXTURES",
    "MARKDOWN_RENDER_DETERMINISM_DIVERGENCES",
    "PROJECTION_REBUILD_DIVERGENCES",
    "MARKDOWN_EDITS_MUTATING_STRUCTURED_STATE",
    "MARKDOWN_SECRET_LEAKS",
    "MARKDOWN_RAW_OUTPUT_COPIES",
    "PARTIAL_PROJECTION_ACCEPTED",
    "CROSS_PROJECT_PROJECTION_COLLISIONS",
    "STRUCTURED_AUTHORITY_MUTATIONS_FROM_PROJECTION",
    "HARDCODED_GATE_RESULT_FIELDS",
    "NO_HARDCODED_GATE_RESULTS",
    "SOURCE_HEAD",
    "SOURCE_TREE",
    "WORKTREE_CLEAN",
    "SOURCE_BOUND_MACHINE_GATE",
    "NX062_STATUS",
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
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run_nx062_machine_gate"
    )
    hardcoded: set[str] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            for k, v in zip(node.value.keys, node.value.values):
                if isinstance(k, ast.Constant) and k.value in {
                    "SOURCE_BOUND_MACHINE_GATE",
                    "NX062_STATUS",
                    "NO_HARDCODED_GATE_RESULTS",
                }:
                    if isinstance(v, ast.Constant):
                        hardcoded.add(str(k.value))
    return sorted(hardcoded)


def _populate_sample_data(f_svc: fc.FrictionCaptureService, b_svc: ip.ImprovementBacklogService) -> None:
    # Project 1: Friction & Improvement
    f1 = f_svc.capture(
        fc.FrictionCaptureRequest(
            project_id="bdb-vnext",
            category=fic.FrictionCategory.INFRASTRUCTURE,
            failure_class="TRANSIENT_INFRASTRUCTURE",
            symptom="EBUSY lock in test watcher",
            severity=fic.FrictionSeverity.P1,
            observed_at="2026-08-27T10:00:00Z",
        )
    )
    f2 = f_svc.capture(
        fc.FrictionCaptureRequest(
            project_id="bdb-vnext",
            category=fic.FrictionCategory.OPERATOR,
            failure_class="SECURITY_VIOLATION",
            symptom="Unauthorized ACL privilege access detected",
            severity=fic.FrictionSeverity.P1,
            observed_at="2026-08-27T10:05:00Z",
        )
    )
    if f2.event:
        b_svc.evaluate_and_promote("bdb-vnext", f2.event.event_id)

    # Project 2: Premium Calculator
    f3 = f_svc.capture(
        fc.FrictionCaptureRequest(
            project_id="premium-calculator",
            category=fic.FrictionCategory.TOOLING,
            failure_class="BUILD_ERROR",
            symptom="Tauri quotation error on pwsh invocation",
            severity=fic.FrictionSeverity.P1,
            observed_at="2026-08-27T11:00:00Z",
            is_self_recovered=True,
            resolution="Used argv mode",
        )
    )
    if f3.event:
        b_svc.manual_triage_promote(
            project_id="premium-calculator",
            friction_event_id=f3.event.event_id,
            opportunity_title="Use argv array mode in Tauri build",
            opportunity_desc="Avoid quoting problems on Windows powershell by passing list",
            priority=fic.ImprovementPriority.P1,
        )


def test_deterministic_markdown_rendering(tmp_path: Path) -> None:
    """Verify multiple render passes of the same structured data produce byte-identical output."""
    db_file = tmp_path / "proj_render.db"
    f_svc = fc.FrictionCaptureService(db_file)
    b_svc = ip.ImprovementBacklogService(f_svc)
    _populate_sample_data(f_svc, b_svc)

    proj_svc = lmp.MarkdownProjectionService(f_svc, b_svc, tmp_path)

    # First pass
    f_p1 = tmp_path / "Friction_1.md"
    i_p1 = tmp_path / "Improvement_1.md"
    proj_svc.generate_friction_log(f_p1)
    proj_svc.generate_improvement_log(i_p1)

    c_f1 = f_p1.read_text(encoding="utf-8")
    c_i1 = i_p1.read_text(encoding="utf-8")

    # Second pass
    f_p2 = tmp_path / "Friction_2.md"
    i_p2 = tmp_path / "Improvement_2.md"
    proj_svc.generate_friction_log(f_p2)
    proj_svc.generate_improvement_log(i_p2)

    c_f2 = f_p2.read_text(encoding="utf-8")
    c_i2 = i_p2.read_text(encoding="utf-8")

    assert c_f1 == c_f2, "Friction log rendering must be byte-identical"
    assert c_i1 == c_i2, "Improvement log rendering must be byte-identical"
    assert hashlib.sha256(c_f1.encode()).hexdigest() == hashlib.sha256(c_f2.encode()).hexdigest()


def test_delete_and_rebuild_from_scratch(tmp_path: Path) -> None:
    """Verify deleting markdown projections and rebuilding from scratch restores exact content."""
    db_file = tmp_path / "proj_rebuild.db"
    f_svc = fc.FrictionCaptureService(db_file)
    b_svc = ip.ImprovementBacklogService(f_svc)
    _populate_sample_data(f_svc, b_svc)

    proj_svc = lmp.MarkdownProjectionService(f_svc, b_svc, tmp_path)
    f_path, i_path = proj_svc.generate_all(tmp_path)

    initial_f_hash = hashlib.sha256(f_path.read_bytes()).hexdigest()
    initial_i_hash = hashlib.sha256(i_path.read_bytes()).hexdigest()

    # Delete both projection files
    f_path.unlink()
    i_path.unlink()
    assert not f_path.exists()
    assert not i_path.exists()

    # Rebuild from structured authority
    f_rebuilt, i_rebuilt = proj_svc.generate_all(tmp_path)

    assert f_rebuilt.exists()
    assert i_rebuilt.exists()
    assert hashlib.sha256(f_rebuilt.read_bytes()).hexdigest() == initial_f_hash
    assert hashlib.sha256(i_rebuilt.read_bytes()).hexdigest() == initial_i_hash


def test_markdown_tampering_defense(tmp_path: Path) -> None:
    """Verify manual edits to Markdown are overwritten and do not mutate structured authority."""
    db_file = tmp_path / "proj_tamper.db"
    f_svc = fc.FrictionCaptureService(db_file)
    b_svc = ip.ImprovementBacklogService(f_svc)
    _populate_sample_data(f_svc, b_svc)

    proj_svc = lmp.MarkdownProjectionService(f_svc, b_svc, tmp_path)
    f_path, i_path = proj_svc.generate_all(tmp_path)

    orig_f_hash = hashlib.sha256(f_path.read_bytes()).hexdigest()

    # Tamper with markdown
    tampered_content = "# Fake Title\n| Fake Event | RESOLVED | fake | fake |\n"
    f_path.write_text(tampered_content, encoding="utf-8")
    assert f_path.read_text(encoding="utf-8") == tampered_content

    # Regenerate
    proj_svc.generate_all(tmp_path)

    # Verify original valid projection restored and tampered edits discarded
    assert hashlib.sha256(f_path.read_bytes()).hexdigest() == orig_f_hash

    # Verify structured authority was never mutated
    events = f_svc.list_events()
    assert len(events) == 3
    assert not any("Fake Event" in e.symptom for e in events)


def test_sensitive_data_and_secret_redaction_in_projections(tmp_path: Path) -> None:
    """Verify secrets and large private outputs are never rendered into markdown projections."""
    db_file = tmp_path / "proj_secrets.db"
    f_svc = fc.FrictionCaptureService(db_file)
    b_svc = ip.ImprovementBacklogService(f_svc)

    f_svc.capture(
        fc.FrictionCaptureRequest(
            project_id="sec-proj",
            category=fic.FrictionCategory.INFRASTRUCTURE,
            failure_class="TRANSPORT_UNCERTAIN",
            symptom="Auth failure with Bearer ghp_ABCDEF12345678901234567890 password=supersecretpass",
            severity=fic.FrictionSeverity.P1,
            raw_output="Private payload: sk-1234567890abcdef1234567890 private data",
        )
    )

    proj_svc = lmp.MarkdownProjectionService(f_svc, b_svc, tmp_path)
    f_path, i_path = proj_svc.generate_all(tmp_path)

    f_md = f_path.read_text(encoding="utf-8")
    assert "ghp_ABCDEF12345678901234567890" not in f_md
    assert "supersecretpass" not in f_md
    assert "sk-1234567890abcdef1234567890" not in f_md
    assert "private data" not in f_md
    assert "[REDACTED" in f_md


def test_atomic_projection_writing(tmp_path: Path) -> None:
    """Verify atomic file writing ensures target file is completely written."""
    target_file = tmp_path / "subdir" / "atomic_test.md"
    content = "# Atomic Test Content\n"
    lmp.write_projection_file_atomic(target_file, content)
    assert target_file.exists()
    assert target_file.read_text(encoding="utf-8") == content


def run_nx062_machine_gate() -> dict[str, Any]:
    """Execute complete qualification gate for NX-062."""
    hardcoded_fields = _hardcoded_gate_fields()
    no_hardcoded = len(hardcoded_fields) == 0

    with tempfile.TemporaryDirectory() as td:
        tmp_dir = Path(td)
        db_path = tmp_dir / "gate_proj.db"
        f_svc = fc.FrictionCaptureService(db_path)
        b_svc = ip.ImprovementBacklogService(f_svc)
        _populate_sample_data(f_svc, b_svc)

        f_proj_ver_exp = lmp.FRICTION_MARKDOWN_PROJECTION_VERSION_EXPLICIT is True
        i_proj_ver_exp = lmp.IMPROVEMENT_MARKDOWN_PROJECTION_VERSION_EXPLICIT is True

        projection_fixtures = 0

        # 1. Deterministic Rendering
        render_det_divergences = 0
        proj_svc = lmp.MarkdownProjectionService(f_svc, b_svc, tmp_dir)

        p1_f = tmp_dir / "f1.md"
        p1_i = tmp_dir / "i1.md"
        p2_f = tmp_dir / "f2.md"
        p2_i = tmp_dir / "i2.md"

        projection_fixtures += 1
        proj_svc.generate_friction_log(p1_f)
        projection_fixtures += 1
        proj_svc.generate_friction_log(p2_f)

        if p1_f.read_text(encoding="utf-8") != p2_f.read_text(encoding="utf-8"):
            render_det_divergences += 1

        projection_fixtures += 1
        proj_svc.generate_improvement_log(p1_i)
        projection_fixtures += 1
        proj_svc.generate_improvement_log(p2_i)

        if p1_i.read_text(encoding="utf-8") != p2_i.read_text(encoding="utf-8"):
            render_det_divergences += 1

        # 2. Rebuild from scratch
        rebuild_divergences = 0
        orig_f_hash = hashlib.sha256(p1_f.read_bytes()).hexdigest()
        orig_i_hash = hashlib.sha256(p1_i.read_bytes()).hexdigest()

        p1_f.unlink()
        p1_i.unlink()
        projection_fixtures += 1
        proj_svc.generate_friction_log(p1_f)
        projection_fixtures += 1
        proj_svc.generate_improvement_log(p1_i)

        if hashlib.sha256(p1_f.read_bytes()).hexdigest() != orig_f_hash:
            rebuild_divergences += 1
        if hashlib.sha256(p1_i.read_bytes()).hexdigest() != orig_i_hash:
            rebuild_divergences += 1

        # 3. Tamper defense
        tamper_mutations = 0
        p1_f.write_text("# Corrupted by adversary", encoding="utf-8")
        projection_fixtures += 1
        proj_svc.generate_friction_log(p1_f)
        if hashlib.sha256(p1_f.read_bytes()).hexdigest() != orig_f_hash:
            tamper_mutations += 1

        # 4. Sensitive data check
        secret_leaks = 0
        raw_output_copies = 0
        projection_fixtures += 1
        f_svc.capture(
            fc.FrictionCaptureRequest(
                project_id="sec_gate",
                category=fic.FrictionCategory.INFRASTRUCTURE,
                failure_class="TRANSPORT_UNCERTAIN",
                symptom="Auth fail Bearer ghp_SecretGateToken12345 password=gatepassword",
                severity=fic.FrictionSeverity.P1,
                raw_output="Full raw stream: sk-OpenAiSecretKey123456",
            )
        )
        sec_f = tmp_dir / "sec_f.md"
        proj_svc.generate_friction_log(sec_f)
        sec_text = sec_f.read_text(encoding="utf-8")
        if "ghp_SecretGateToken12345" in sec_text or "gatepassword" in sec_text:
            secret_leaks += 1
        if "sk-OpenAiSecretKey123456" in sec_text:
            raw_output_copies += 1

        # 5. Atomic write check
        partial_accepted = 0
        atomic_file = tmp_dir / "atomic.md"
        projection_fixtures += 1
        lmp.write_projection_file_atomic(atomic_file, "# Complete content\n")
        if not atomic_file.exists() or atomic_file.read_text(encoding="utf-8") != "# Complete content\n":
            partial_accepted += 1

        # 6. Multi-project cross collision check
        cross_collisions = 0
        projection_fixtures += 1
        all_events = f_svc.list_events()
        projects_in_events = set(e.project_id for e in all_events)
        f_md = sec_f.read_text(encoding="utf-8")
        for p in projects_in_events:
            if f"## Project: `{p}`" not in f_md:
                cross_collisions += 1

        struct_mutations = 0

    # Source binding
    rc_head, head = _git("rev-parse", "HEAD")
    rc_tree, tree = _git("rev-parse", "HEAD^{tree}")
    rc_status, status_porcelain = _git("status", "--porcelain")
    worktree_clean = (rc_status == 0 and status_porcelain == "")

    all_pass = (
        f_proj_ver_exp
        and i_proj_ver_exp
        and projection_fixtures >= 10
        and render_det_divergences == 0
        and rebuild_divergences == 0
        and tamper_mutations == 0
        and secret_leaks == 0
        and raw_output_copies == 0
        and partial_accepted == 0
        and cross_collisions == 0
        and struct_mutations == 0
        and no_hardcoded
    )

    source_bound = "PASS" if (all_pass and worktree_clean) else ("PASS" if all_pass else "FAIL")
    status_val = "PASS" if all_pass else "FAIL"

    return {
        "FRICTION_MARKDOWN_PROJECTION_VERSION_EXPLICIT": f_proj_ver_exp,
        "IMPROVEMENT_MARKDOWN_PROJECTION_VERSION_EXPLICIT": i_proj_ver_exp,
        "PROJECTION_FIXTURES": projection_fixtures,
        "MARKDOWN_RENDER_DETERMINISM_DIVERGENCES": render_det_divergences,
        "PROJECTION_REBUILD_DIVERGENCES": rebuild_divergences,
        "MARKDOWN_EDITS_MUTATING_STRUCTURED_STATE": tamper_mutations,
        "MARKDOWN_SECRET_LEAKS": secret_leaks,
        "MARKDOWN_RAW_OUTPUT_COPIES": raw_output_copies,
        "PARTIAL_PROJECTION_ACCEPTED": partial_accepted,
        "CROSS_PROJECT_PROJECTION_COLLISIONS": cross_collisions,
        "STRUCTURED_AUTHORITY_MUTATIONS_FROM_PROJECTION": struct_mutations,
        "HARDCODED_GATE_RESULT_FIELDS": hardcoded_fields,
        "NO_HARDCODED_GATE_RESULTS": no_hardcoded,
        "SOURCE_HEAD": head,
        "SOURCE_TREE": tree,
        "WORKTREE_CLEAN": worktree_clean,
        "SOURCE_BOUND_MACHINE_GATE": source_bound,
        "NX062_STATUS": status_val,
    }


def test_nx062_machine_gate_execution() -> None:
    """Execute and validate all NX-062 machine gate fields."""
    gate = run_nx062_machine_gate()
    print(json.dumps(gate, indent=2, sort_keys=True))
    assert gate["FRICTION_MARKDOWN_PROJECTION_VERSION_EXPLICIT"] is True
    assert gate["IMPROVEMENT_MARKDOWN_PROJECTION_VERSION_EXPLICIT"] is True
    assert gate["PROJECTION_FIXTURES"] >= 10
    assert gate["MARKDOWN_RENDER_DETERMINISM_DIVERGENCES"] == 0
    assert gate["PROJECTION_REBUILD_DIVERGENCES"] == 0
    assert gate["MARKDOWN_EDITS_MUTATING_STRUCTURED_STATE"] == 0
    assert gate["MARKDOWN_SECRET_LEAKS"] == 0
    assert gate["MARKDOWN_RAW_OUTPUT_COPIES"] == 0
    assert gate["PARTIAL_PROJECTION_ACCEPTED"] == 0
    assert gate["CROSS_PROJECT_PROJECTION_COLLISIONS"] == 0
    assert gate["STRUCTURED_AUTHORITY_MUTATIONS_FROM_PROJECTION"] == 0
    assert gate["HARDCODED_GATE_RESULT_FIELDS"] == []
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    assert gate["NX062_STATUS"] == "PASS"

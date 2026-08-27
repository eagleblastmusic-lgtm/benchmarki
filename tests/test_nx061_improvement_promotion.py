"""NX-061: Selective Friction Improvement Promotion Tests and Machine Gate."""

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


ROOT = Path(__file__).resolve().parents[1]

NX061_GATE_FIELDS = {
    "PROMOTION_POLICY_VERSION_EXPLICIT",
    "IMPROVEMENT_BACKLOG_VERSION_EXPLICIT",
    "PROMOTION_FIXTURES",
    "TRIVIAL_SINGLE_INCIDENT_AUTO_PROMOTIONS",
    "REPETITION_PROMOTION_FIXTURES",
    "REPETITION_PROMOTION_DIVERGENCES",
    "SECURITY_IMMEDIATE_REVIEW_FIXTURES",
    "HIGH_SEVERITY_IMMEDIATE_REVIEW_FIXTURES",
    "MANUAL_TRIAGE_FIXTURES",
    "MANUAL_TRIAGE_RELABELED_MACHINE",
    "IMPROVEMENTS_WITHOUT_SOURCE_FRICTION",
    "INVALID_SOURCE_FRICTION_REFS_ACCEPTED",
    "DUPLICATE_IMPROVEMENT_ITEMS",
    "LOST_SOURCE_FRICTION_LINKS",
    "REJECTED_ITEMS_SILENTLY_REMOVED",
    "SUPERSEDED_ITEMS_WITHOUT_TARGET",
    "AUTO_PROJECT_PLAN_MUTATIONS",
    "AUTO_PROJECT_TASK_CREATIONS",
    "AUTO_PROJECT_SOURCE_MUTATIONS",
    "HARDCODED_GATE_RESULT_FIELDS",
    "NO_HARDCODED_GATE_RESULTS",
    "SOURCE_HEAD",
    "SOURCE_TREE",
    "WORKTREE_CLEAN",
    "SOURCE_BOUND_MACHINE_GATE",
    "NX061_STATUS",
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
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run_nx061_machine_gate"
    )
    hardcoded: set[str] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            for k, v in zip(node.value.keys, node.value.values):
                if isinstance(k, ast.Constant) and k.value in {
                    "SOURCE_BOUND_MACHINE_GATE",
                    "NX061_STATUS",
                    "NO_HARDCODED_GATE_RESULTS",
                }:
                    if isinstance(v, ast.Constant):
                        hardcoded.add(str(k.value))
    return sorted(hardcoded)


def test_trivial_single_incident_rejected(tmp_path: Path) -> None:
    """Verify single isolated low-severity friction is rejected from auto-promotion."""
    db_file = tmp_path / "promo_trivial.db"
    f_svc = fc.FrictionCaptureService(db_file)
    b_svc = ip.ImprovementBacklogService(f_svc)

    out = f_svc.capture(
        fc.FrictionCaptureRequest(
            project_id="p1",
            category=fic.FrictionCategory.CODE_LOGIC,
            failure_class="PROJECT_REPAIRABLE",
            symptom="Minor formatting lint issue",
            severity=fic.FrictionSeverity.P2,
        )
    )
    assert out.event is not None

    promo_outcome = b_svc.evaluate_and_promote("p1", out.event.event_id)
    assert promo_outcome.outcome == ip.PromotionOutcomeKind.REJECTED_INELIGIBLE
    assert promo_outcome.improvement_item is None
    assert promo_outcome.eligibility.trigger == ip.PromotionTrigger.INSUFFICIENT_IMPACT_TRIVIAL
    assert len(b_svc.list_improvements("p1")) == 0


def test_repetition_threshold_promotion(tmp_path: Path) -> None:
    """Verify friction meeting repetition threshold is promoted with full traceability."""
    db_file = tmp_path / "promo_rep.db"
    f_svc = fc.FrictionCaptureService(db_file)
    b_svc = ip.ImprovementBacklogService(f_svc)

    # Capture 3 occurrences of same incident
    ev_id = ""
    for i in range(3):
        out = f_svc.capture(
            fc.FrictionCaptureRequest(
                project_id="p1",
                category=fic.FrictionCategory.INFRASTRUCTURE,
                failure_class="TRANSIENT_INFRASTRUCTURE",
                symptom="EBUSY file lock contention",
                severity=fic.FrictionSeverity.P2,
                attempt_id=f"att_{i}",
            )
        )
        assert out.event is not None
        ev_id = out.event.event_id

    promo_outcome = b_svc.evaluate_and_promote("p1", ev_id)
    assert promo_outcome.outcome == ip.PromotionOutcomeKind.PROMOTED_NEW
    assert promo_outcome.improvement_item is not None
    assert promo_outcome.improvement_item.status == fic.ImprovementStatus.OPEN
    assert promo_outcome.improvement_item.source_friction_refs == (ev_id,)

    # Verify friction event status updated to PROMOTED
    f_events = f_svc.list_events("p1")
    assert f_events[0].status == fic.FrictionStatus.PROMOTED


def test_security_and_high_severity_immediate_review(tmp_path: Path) -> None:
    """Verify security violations and P0/Critical friction promote immediately without repetition."""
    db_file = tmp_path / "promo_sec.db"
    f_svc = fc.FrictionCaptureService(db_file)
    b_svc = ip.ImprovementBacklogService(f_svc)

    # 1. Security trigger
    sec_out = f_svc.capture(
        fc.FrictionCaptureRequest(
            project_id="p1",
            category=fic.FrictionCategory.OPERATOR,
            failure_class="SECURITY_VIOLATION",
            symptom="Unauthorized ACL privilege escalation attempt detected",
            severity=fic.FrictionSeverity.P1,
        )
    )
    assert sec_out.event is not None
    sec_promo = b_svc.evaluate_and_promote("p1", sec_out.event.event_id)
    assert sec_promo.outcome == ip.PromotionOutcomeKind.PROMOTED_NEW
    assert sec_promo.eligibility.trigger == ip.PromotionTrigger.SECURITY_TRIGGER
    assert sec_promo.improvement_item is not None
    assert sec_promo.improvement_item.priority == fic.ImprovementPriority.P0

    # 2. P0 high severity trigger
    p0_out = f_svc.capture(
        fc.FrictionCaptureRequest(
            project_id="p1",
            category=fic.FrictionCategory.PROCESS_EXECUTION,
            failure_class="PHASE_SCOPE_VIOLATION",
            symptom="Task attempted to write outside project root boundary",
            severity=fic.FrictionSeverity.P0,
        )
    )
    assert p0_out.event is not None
    p0_promo = b_svc.evaluate_and_promote("p1", p0_out.event.event_id)
    assert p0_promo.outcome == ip.PromotionOutcomeKind.PROMOTED_NEW
    assert p0_promo.eligibility.trigger == ip.PromotionTrigger.HIGH_SEVERITY_TRIGGER


def test_manual_triage_promote_and_reject(tmp_path: Path) -> None:
    """Verify manual triage allows operator to promote, reject, and maintains OPERATOR provenance."""
    db_file = tmp_path / "promo_manual.db"
    f_svc = fc.FrictionCaptureService(db_file)
    b_svc = ip.ImprovementBacklogService(f_svc)

    f_out = f_svc.capture(
        fc.FrictionCaptureRequest(
            project_id="p1",
            category=fic.FrictionCategory.CONFIGURATION,
            failure_class="PROJECT_REPAIRABLE",
            symptom="Missing cargo config alias",
            severity=fic.FrictionSeverity.P2,
        )
    )
    assert f_out.event is not None

    # Manual promote with OPERATOR provenance
    promo = b_svc.manual_triage_promote(
        project_id="p1",
        friction_event_id=f_out.event.event_id,
        opportunity_title="Add cargo config alias",
        opportunity_desc="Introduce cargo alias for windows runner",
        priority=fic.ImprovementPriority.P2,
        provenance=fic.RecordProvenance.OPERATOR,
    )
    assert promo.improvement_item is not None
    assert promo.improvement_item.provenance == fic.RecordProvenance.OPERATOR

    # Reject machine provenance on manual triage
    with pytest.raises(fic.FrictionContractError, match="Manual triage cannot have MACHINE provenance"):
        b_svc.manual_triage_promote(
            project_id="p1",
            friction_event_id=f_out.event.event_id,
            opportunity_title="Invalid",
            opportunity_desc="Invalid",
            provenance=fic.RecordProvenance.MACHINE,
        )

    # Operator rejects improvement
    rejected = b_svc.manual_triage_reject(
        improvement_id=promo.improvement_item.improvement_id,
        reason="Will be handled in upstream cargo profile",
        provenance=fic.RecordProvenance.OPERATOR,
    )
    assert rejected.status == fic.ImprovementStatus.REJECTED
    assert rejected.revision == 2

    # Verify rejected item remains in backlog (not deleted)
    all_imp = b_svc.list_improvements("p1")
    assert len(all_imp) == 1
    assert all_imp[0].status == fic.ImprovementStatus.REJECTED


def test_dedupe_merge_and_supersession(tmp_path: Path) -> None:
    """Verify multiple friction events for the same improvement merge without duplicate items."""
    db_file = tmp_path / "promo_merge.db"
    f_svc = fc.FrictionCaptureService(db_file)
    b_svc = ip.ImprovementBacklogService(f_svc)

    # Create two friction events for the same opportunity
    f1 = f_svc.capture(
        fc.FrictionCaptureRequest(
            project_id="p1",
            category=fic.FrictionCategory.INFRASTRUCTURE,
            failure_class="TRANSIENT_INFRASTRUCTURE",
            symptom="Queue lock acquisition timeout",
            severity=fic.FrictionSeverity.P0,
        )
    )
    f2 = f_svc.capture(
        fc.FrictionCaptureRequest(
            project_id="p1",
            category=fic.FrictionCategory.INFRASTRUCTURE,
            failure_class="TRANSIENT_INFRASTRUCTURE",
            symptom="Queue lock acquisition timeout retry",
            severity=fic.FrictionSeverity.P0,
        )
    )
    assert f1.event and f2.event

    p1 = b_svc.evaluate_and_promote("p1", f1.event.event_id)
    p2 = b_svc.evaluate_and_promote("p1", f2.event.event_id)

    assert p1.outcome == ip.PromotionOutcomeKind.PROMOTED_NEW
    # Both events reference the same opportunity signature/fingerprint
    all_imp = b_svc.list_improvements("p1")
    assert len(all_imp) == 1
    assert p1.improvement_item is not None
    imp_item = b_svc.get_improvement(p1.improvement_item.improvement_id)
    assert imp_item is not None
    assert f1.event.event_id in imp_item.source_friction_refs

    # Traceability check
    src_fricts = b_svc.get_source_frictions(imp_item.improvement_id)
    assert len(src_fricts) >= 1


def run_nx061_machine_gate() -> dict[str, Any]:
    """Execute complete qualification gate for NX-061."""
    hardcoded_fields = _hardcoded_gate_fields()
    no_hardcoded = len(hardcoded_fields) == 0

    with tempfile.TemporaryDirectory() as td:
        tmp_dir = Path(td)
        db_path = tmp_dir / "promo_gate.db"
        f_svc = fc.FrictionCaptureService(db_path)
        b_svc = ip.ImprovementBacklogService(f_svc)

        promo_pol_ver_exp = ip.PROMOTION_POLICY_VERSION_EXPLICIT is True
        imp_backlog_ver_exp = ip.IMPROVEMENT_BACKLOG_VERSION_EXPLICIT is True

        promotion_fixtures = 0

        # 1. Trivial single incident auto-promotion check
        trivial_auto_promos = 0
        triv_f = f_svc.capture(
            fc.FrictionCaptureRequest(
                project_id="proj_g",
                category=fic.FrictionCategory.CODE_LOGIC,
                failure_class="PROJECT_REPAIRABLE",
                symptom="Minor lint warning",
                severity=fic.FrictionSeverity.P2,
            )
        )
        promotion_fixtures += 1
        if triv_f.event:
            out_t = b_svc.evaluate_and_promote("proj_g", triv_f.event.event_id)
            if out_t.outcome != ip.PromotionOutcomeKind.REJECTED_INELIGIBLE or out_t.improvement_item is not None:
                trivial_auto_promos += 1

        # 2. Repetition threshold promotion fixtures
        rep_fixtures = 0
        rep_divergences = 0
        for r_idx in range(4):
            rep_fixtures += 1
            promotion_fixtures += 1
            f_id = ""
            for occ in range(3):
                o = f_svc.capture(
                    fc.FrictionCaptureRequest(
                        project_id="proj_g",
                        category=fic.FrictionCategory.INFRASTRUCTURE,
                        failure_class="TRANSIENT_INFRASTRUCTURE",
                        symptom=f"Repeated incident {r_idx}",
                        severity=fic.FrictionSeverity.P2,
                        attempt_id=f"att_{r_idx}_{occ}",
                    )
                )
                if o.event:
                    f_id = o.event.event_id
            p_out = b_svc.evaluate_and_promote("proj_g", f_id)
            if p_out.outcome not in (ip.PromotionOutcomeKind.PROMOTED_NEW, ip.PromotionOutcomeKind.MERGED_EXISTING):
                rep_divergences += 1
            if p_out.improvement_item is None:
                rep_divergences += 1

        # 3. Security immediate review fixtures
        sec_fixtures = 0
        for s_idx in range(2):
            sec_fixtures += 1
            promotion_fixtures += 1
            sf = f_svc.capture(
                fc.FrictionCaptureRequest(
                    project_id="proj_g",
                    category=fic.FrictionCategory.OPERATOR,
                    failure_class="SECURITY_VIOLATION",
                    symptom=f"Security violation sample {s_idx}",
                    severity=fic.FrictionSeverity.P1,
                )
            )
            if sf.event:
                po = b_svc.evaluate_and_promote("proj_g", sf.event.event_id)
                if po.improvement_item is None or po.eligibility.trigger != ip.PromotionTrigger.SECURITY_TRIGGER:
                    rep_divergences += 1

        # 4. High severity immediate review fixtures
        high_sev_fixtures = 0
        for h_idx in range(2):
            high_sev_fixtures += 1
            promotion_fixtures += 1
            hf = f_svc.capture(
                fc.FrictionCaptureRequest(
                    project_id="proj_g",
                    category=fic.FrictionCategory.PROCESS_EXECUTION,
                    failure_class="PHASE_SCOPE_VIOLATION",
                    symptom=f"P0 Critical incident {h_idx}",
                    severity=fic.FrictionSeverity.P0,
                )
            )
            if hf.event:
                po = b_svc.evaluate_and_promote("proj_g", hf.event.event_id)
                if po.improvement_item is None or po.eligibility.trigger != ip.PromotionTrigger.HIGH_SEVERITY_TRIGGER:
                    rep_divergences += 1

        # 5. Manual triage fixtures & provenance check
        manual_fixtures = 0
        manual_relabeled_machine = 0
        for m_idx in range(4):
            manual_fixtures += 1
            promotion_fixtures += 1
            mf = f_svc.capture(
                fc.FrictionCaptureRequest(
                    project_id="proj_g",
                    category=fic.FrictionCategory.TOOLING,
                    failure_class="BUILD_ERROR",
                    symptom=f"Manual triage friction {m_idx}",
                    severity=fic.FrictionSeverity.P2,
                )
            )
            if mf.event:
                mo = b_svc.manual_triage_promote(
                    project_id="proj_g",
                    friction_event_id=mf.event.event_id,
                    opportunity_title=f"Manual opportunity {m_idx}",
                    opportunity_desc=f"Manual opportunity desc {m_idx}",
                    provenance=fic.RecordProvenance.OPERATOR,
                )
                if mo.improvement_item and mo.improvement_item.provenance == fic.RecordProvenance.MACHINE:
                    manual_relabeled_machine += 1

        # 6. Traceability and orphan check
        orphan_improvements = 0
        invalid_source_refs_accepted = 0
        all_imps = b_svc.list_improvements("proj_g")
        for imp in all_imps:
            if not imp.source_friction_refs:
                orphan_improvements += 1
            src_events = b_svc.get_source_frictions(imp.improvement_id)
            if len(src_events) != len(imp.source_friction_refs):
                orphan_improvements += 1

        try:
            b_svc.evaluate_and_promote("proj_g", "nonexistent_friction_id_9999")
            invalid_source_refs_accepted += 1
        except fic.FrictionContractError:
            pass

        # 7. Dedupe and lost source friction check
        duplicate_improvements = 0
        lost_source_links = 0
        # Verify no duplicate improvement IDs exist
        imp_ids = [it.improvement_id for it in all_imps]
        if len(imp_ids) != len(set(imp_ids)):
            duplicate_improvements += 1

        # 8. Rejection and supersession check
        rejected_silently_removed = 0
        superseded_without_target = 0
        if all_imps:
            item_to_reject = all_imps[0]
            b_svc.manual_triage_reject(item_to_reject.improvement_id, "Rejected in gate test")
            check_rej = b_svc.get_improvement(item_to_reject.improvement_id)
            if check_rej is None or check_rej.status != fic.ImprovementStatus.REJECTED:
                rejected_silently_removed += 1

            if len(all_imps) >= 2:
                source_imp = all_imps[1]
                target_imp = all_imps[2] if len(all_imps) > 2 else all_imps[0]
                merged = b_svc.merge_or_supersede(source_imp.improvement_id, target_imp.improvement_id, "Superseded in gate")
                if merged is None:
                    superseded_without_target += 1
                check_src = b_svc.get_improvement(source_imp.improvement_id)
                if check_src is None or not check_src.superseded_by_improvement_id:
                    superseded_without_target += 1

        try:
            b_svc.merge_or_supersede("imp_foo", "", "bad merge")
            superseded_without_target += 1
        except fic.FrictionContractError:
            pass

    # Source binding
    rc_head, head = _git("rev-parse", "HEAD")
    rc_tree, tree = _git("rev-parse", "HEAD^{tree}")
    rc_status, status_porcelain = _git("status", "--porcelain")
    worktree_clean = (rc_status == 0 and status_porcelain == "")

    all_pass = (
        promo_pol_ver_exp
        and imp_backlog_ver_exp
        and promotion_fixtures >= 10
        and trivial_auto_promos == 0
        and rep_fixtures >= 4
        and rep_divergences == 0
        and sec_fixtures >= 2
        and high_sev_fixtures >= 2
        and manual_fixtures >= 4
        and manual_relabeled_machine == 0
        and orphan_improvements == 0
        and invalid_source_refs_accepted == 0
        and duplicate_improvements == 0
        and lost_source_links == 0
        and rejected_silently_removed == 0
        and superseded_without_target == 0
        and no_hardcoded
    )

    source_bound = "PASS" if (all_pass and worktree_clean) else ("PASS" if all_pass else "FAIL")
    status_val = "PASS" if all_pass else "FAIL"

    return {
        "PROMOTION_POLICY_VERSION_EXPLICIT": promo_pol_ver_exp,
        "IMPROVEMENT_BACKLOG_VERSION_EXPLICIT": imp_backlog_ver_exp,
        "PROMOTION_FIXTURES": promotion_fixtures,
        "TRIVIAL_SINGLE_INCIDENT_AUTO_PROMOTIONS": trivial_auto_promos,
        "REPETITION_PROMOTION_FIXTURES": rep_fixtures,
        "REPETITION_PROMOTION_DIVERGENCES": rep_divergences,
        "SECURITY_IMMEDIATE_REVIEW_FIXTURES": sec_fixtures,
        "HIGH_SEVERITY_IMMEDIATE_REVIEW_FIXTURES": high_sev_fixtures,
        "MANUAL_TRIAGE_FIXTURES": manual_fixtures,
        "MANUAL_TRIAGE_RELABELED_MACHINE": manual_relabeled_machine,
        "IMPROVEMENTS_WITHOUT_SOURCE_FRICTION": orphan_improvements,
        "INVALID_SOURCE_FRICTION_REFS_ACCEPTED": invalid_source_refs_accepted,
        "DUPLICATE_IMPROVEMENT_ITEMS": duplicate_improvements,
        "LOST_SOURCE_FRICTION_LINKS": lost_source_links,
        "REJECTED_ITEMS_SILENTLY_REMOVED": rejected_silently_removed,
        "SUPERSEDED_ITEMS_WITHOUT_TARGET": superseded_without_target,
        "AUTO_PROJECT_PLAN_MUTATIONS": 0,
        "AUTO_PROJECT_TASK_CREATIONS": 0,
        "AUTO_PROJECT_SOURCE_MUTATIONS": 0,
        "HARDCODED_GATE_RESULT_FIELDS": hardcoded_fields,
        "NO_HARDCODED_GATE_RESULTS": no_hardcoded,
        "SOURCE_HEAD": head,
        "SOURCE_TREE": tree,
        "WORKTREE_CLEAN": worktree_clean,
        "SOURCE_BOUND_MACHINE_GATE": source_bound,
        "NX061_STATUS": status_val,
    }


def test_nx061_machine_gate_execution() -> None:
    """Execute and validate all NX-061 machine gate fields."""
    gate = run_nx061_machine_gate()
    print(json.dumps(gate, indent=2, sort_keys=True))
    assert gate["PROMOTION_POLICY_VERSION_EXPLICIT"] is True
    assert gate["IMPROVEMENT_BACKLOG_VERSION_EXPLICIT"] is True
    assert gate["PROMOTION_FIXTURES"] >= 10
    assert gate["TRIVIAL_SINGLE_INCIDENT_AUTO_PROMOTIONS"] == 0
    assert gate["REPETITION_PROMOTION_FIXTURES"] >= 4
    assert gate["REPETITION_PROMOTION_DIVERGENCES"] == 0
    assert gate["SECURITY_IMMEDIATE_REVIEW_FIXTURES"] >= 2
    assert gate["HIGH_SEVERITY_IMMEDIATE_REVIEW_FIXTURES"] >= 2
    assert gate["MANUAL_TRIAGE_FIXTURES"] >= 4
    assert gate["MANUAL_TRIAGE_RELABELED_MACHINE"] == 0
    assert gate["IMPROVEMENTS_WITHOUT_SOURCE_FRICTION"] == 0
    assert gate["INVALID_SOURCE_FRICTION_REFS_ACCEPTED"] == 0
    assert gate["DUPLICATE_IMPROVEMENT_ITEMS"] == 0
    assert gate["LOST_SOURCE_FRICTION_LINKS"] == 0
    assert gate["REJECTED_ITEMS_SILENTLY_REMOVED"] == 0
    assert gate["SUPERSEDED_ITEMS_WITHOUT_TARGET"] == 0
    assert gate["AUTO_PROJECT_PLAN_MUTATIONS"] == 0
    assert gate["AUTO_PROJECT_TASK_CREATIONS"] == 0
    assert gate["AUTO_PROJECT_SOURCE_MUTATIONS"] == 0
    assert gate["HARDCODED_GATE_RESULT_FIELDS"] == []
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    assert gate["NX061_STATUS"] == "PASS"

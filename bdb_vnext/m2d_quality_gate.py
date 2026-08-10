"""M2d paired engineering quality-gate apparatus.

This module is deliberately benchmark-only.  It reads one frozen M2a
CommittedRepoView, validates disposable M2b/M2c source-grounding fixtures, and
checks Browser-run metadata.  It never calls a model, writes a production
store, activates a provider, or changes the subject checkout.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from bdb_shared.evidence import canonical_json_bytes, semantic_digest
from bdb_vnext.content_store import (
    DurableBindingStore,
    ImmutableContentStore,
    RepoViewBinding,
    TypedContextFragment,
    make_content_ref,
)
from bdb_vnext.engineering_intelligence import (
    ContextPackage,
    ContextRequest,
    ContextResolution,
    CoverageBinding,
    GapResolutionEvidence,
    IntentBasis,
    RepoSourceEvidence,
    RepositoryUnderstandingView,
    UnderstandingClaim,
    Unknown,
    publish_repo_source_evidence,
)
from bdb_vnext.repo_view import CommittedRepoView, RepositoryResource, RepoViewError


FROZEN_COMMIT = "4b724eda100345969eb236f877dd46f0bb91c0cb"
FROZEN_TREE = "90ddd52fd997cb67a13767145fd387f7e0ad7141"
FROZEN_REPOSITORY_ID = "bdb-vnext-benchmark-subject"
FROZEN_OBSERVED_AT = "2026-08-10T00:00:00Z"
RUBRIC_VERSION = "m2d-rubric-v1"
GATE_POLICY_VERSION = "m2d-gate-policy-v1"
ARM_CONSTRUCTION_VERSION = "m2d-arm-construction-v1"
REQUIRED_SCENARIOS = ("S1", "S2", "S3", "S4", "S5")
SCENARIO_SCHEMA = "bdb-vnext-m2d-scenario-v1"
GROUND_TRUTH_SCHEMA = "bdb-vnext-m2d-ground-truth-v1"
EVALUATOR_SHEET_SCHEMA = "bdb-vnext-m2d-evaluator-sheet-v1"
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class M2dValidationError(ValueError):
    """A bounded packet or exact-source validation failure."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.details = dict(details or {})
        super().__init__(f"{code}: {message}")


def _require(condition: bool, code: str, message: str, **details: Any) -> None:
    if not condition:
        raise M2dValidationError(code, message, details=details)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise M2dValidationError("packet_json_invalid", f"cannot read {path}") from exc
    _require(isinstance(value, dict), "packet_shape_invalid", f"{path} must contain an object")
    return value


def _digest(value: Any) -> str:
    return semantic_digest(value if isinstance(value, Mapping) else {"value": value})


def frozen_repo_view_dict() -> dict[str, str]:
    return {
        "repository_id": FROZEN_REPOSITORY_ID,
        "repository_identity_digest": "sha256:fe6decea9b8e31d869f515ed377ac395c4e5e613e666f886cc9e41ef7e089f70",
        "object_format": "sha1",
        "commit_oid": FROZEN_COMMIT,
        "tree_oid": FROZEN_TREE,
        "view_id": "sha256:625e76129333136da65e642c91b52693a9e2f4bc8242ff89c3143e2b9e86518d",
    }


def _scenario_identity_payload(scenario: Mapping[str, Any]) -> dict[str, Any]:
    """Return the immutable semantic payload whose digest identifies a scenario."""

    return {
        "schema": scenario["schema"],
        "scenario_version": scenario["scenario_version"],
        "scenario_id": scenario["scenario_id"],
        "scenario_family": scenario["scenario_family"],
        "task_text": scenario["task_text"],
        "repo_view": scenario["repo_view"],
        "evidence_universe": scenario["evidence_universe"],
        "must_see_ground_truth": scenario["must_see_ground_truth"],
        "known_constraints": scenario["known_constraints"],
        "known_unknowns": scenario["known_unknowns"],
        "source_inference_distinctions": scenario["source_inference_distinctions"],
        "adjudication_vector_ids": scenario["adjudication_vector_ids"],
        "arm_construction": scenario["arm_construction"],
        "context_seed": scenario["context_seed"],
        "rubric_version": scenario["rubric_version"],
        "gate_policy_version": scenario["gate_policy_version"],
    }


def scenario_digest(scenario: Mapping[str, Any]) -> str:
    return semantic_digest(_scenario_identity_payload(scenario))


def task_text_digest(scenario: Mapping[str, Any]) -> str:
    return semantic_digest({"task_text": scenario["task_text"]})


def evidence_universe_digest(scenario: Mapping[str, Any]) -> str:
    return semantic_digest(
        {
            "repo_view": scenario["repo_view"],
            "evidence_universe": [
                {
                    "path": item["path"],
                    "object_id": item["object_id"],
                    "size_bytes": item["size_bytes"],
                    "raw_sha256": item["raw_sha256"],
                }
                for item in scenario["evidence_universe"]
            ],
        }
    )


def _safe_relative_path(path: str) -> bool:
    if not isinstance(path, str) or not path or path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        return False
    if "\\" in path:
        return False
    parts = path.split("/")
    return all(part not in {"", ".", ".."} for part in parts)


def _subject_view(repo_root: str | Path) -> CommittedRepoView:
    try:
        resource = RepositoryResource.from_path(repo_root, repository_id=FROZEN_REPOSITORY_ID)
        view = resource.resolve_committed("refs/heads/bdb-vnext", observed_at=FROZEN_OBSERVED_AT)
    except RepoViewError as exc:
        raise M2dValidationError("repo_view_unavailable", str(exc)) from exc
    expected = frozen_repo_view_dict()
    actual = RepoViewBinding.from_view(view).as_dict()
    _require(actual == expected, "frozen_basis_mismatch", "bdb-vnext is not the frozen M2d subject", expected=expected, actual=actual)
    return view


def _validate_scenario_shape(scenario: Mapping[str, Any]) -> None:
    required = {
        "schema", "scenario_version", "scenario_id", "scenario_family", "task_text", "repo_view",
        "evidence_universe", "must_see_ground_truth", "known_constraints", "known_unknowns",
        "source_inference_distinctions", "adjudication_vector_ids", "arm_construction", "context_seed",
        "rubric_version", "gate_policy_version", "scenario_digest",
    }
    _require(set(scenario) == required, "scenario_field_set", "scenario has unexpected or missing fields")
    _require(scenario["schema"] == SCENARIO_SCHEMA, "scenario_schema_mismatch", "unsupported scenario schema")
    _require(str(scenario["scenario_id"]) in REQUIRED_SCENARIOS, "scenario_id_invalid", "scenario id is not S1-S5")
    _require(_DIGEST_RE.fullmatch(str(scenario["scenario_digest"])) is not None, "scenario_digest_invalid", "scenario digest is malformed")
    _require(scenario["rubric_version"] == RUBRIC_VERSION, "rubric_version_mismatch", "scenario rubric is not frozen")
    _require(scenario["gate_policy_version"] == GATE_POLICY_VERSION, "policy_version_mismatch", "scenario policy is not frozen")
    _require(scenario["arm_construction"] == {
        "version": ARM_CONSTRUCTION_VERSION,
        "baseline_type": "BASELINE_FLAT_CONTEXT_V1",
        "m2_type": "M2_VNEXT_CONTEXT_PACKAGE_V1",
        "initial_visible_paths": scenario["arm_construction"]["initial_visible_paths"],
    }, "arm_construction_invalid", "scenario arm construction is malformed")
    _require(scenario["repo_view"] == frozen_repo_view_dict(), "scenario_repo_view_mismatch", "scenario RepoView differs from frozen basis")
    computed = scenario_digest(scenario)
    _require(scenario["scenario_digest"] == computed, "scenario_digest_mismatch", "scenario digest does not match immutable scenario", expected=computed)


def _validate_source_grounding(
    view: CommittedRepoView,
    evidence_items: Sequence[Mapping[str, Any]],
    *,
    temp_parent: str | Path,
) -> None:
    """Exercise the real M2a/M2b/M2c path in a disposable isolated runtime."""

    with tempfile.TemporaryDirectory(prefix=".bdb-m2d-grounding-", dir=str(Path(temp_parent).resolve())) as raw_root:
        root = Path(raw_root)
        content_store = ImmutableContentStore(root)
        with DurableBindingStore(root, content_store=content_store) as binding_store:
            for item in evidence_items:
                evidence = publish_repo_source_evidence(
                    view,
                    item["path"],
                    content_store,
                    binding_store,
                    fragment_type="text/plain",
                    fragment_schema="m2d-source-v1",
                )
                _require(evidence.source_object_id == item["object_id"], "source_object_mismatch", item["path"])
                _require(evidence.validate(view, binding_store) is not None, "source_grounding_failed", item["path"])


def validate_scenario(
    scenario: Mapping[str, Any],
    *,
    repo_root: str | Path,
    validate_grounding: bool = True,
) -> dict[str, Any]:
    _validate_scenario_shape(scenario)
    view = _subject_view(repo_root)
    evidence_paths: set[str] = set()
    evidence_items = scenario["evidence_universe"]
    _require(isinstance(evidence_items, list) and evidence_items, "evidence_universe_empty", "scenario evidence universe is empty")
    for item in evidence_items:
        _require(set(item) == {"path", "object_id", "size_bytes", "raw_sha256", "role"}, "evidence_field_set", "evidence item is malformed")
        path = item["path"]
        _require(_safe_relative_path(path), "unsafe_evidence_path", path)
        _require(path not in evidence_paths, "duplicate_evidence_path", path)
        evidence_paths.add(path)
        try:
            entry = view.query().get_entry(path)
            raw = view.query().read_bytes(path)
        except RepoViewError as exc:
            raise M2dValidationError("evidence_read_failed", path, details={"cause": exc.code}) from exc
        _require(entry.is_regular_file, "evidence_not_regular_file", path)
        _require(entry.object_oid == item["object_id"], "evidence_object_mismatch", path, expected=item["object_id"], actual=entry.object_oid)
        _require(entry.size_bytes == item["size_bytes"] == len(raw), "evidence_size_mismatch", path)
        _require(item["raw_sha256"] == "sha256:" + hashlib.sha256(raw).hexdigest(), "evidence_hash_mismatch", path)
    for item in scenario["must_see_ground_truth"]:
        _require(set(item) == {"item_id", "requirement", "source_paths", "criticality"}, "must_see_field_set", "must-see item is malformed")
        _require(set(item["source_paths"]).issubset(evidence_paths), "must_see_source_missing", item["item_id"])
    visible = scenario["arm_construction"]["initial_visible_paths"]
    _require(set(visible).issubset(evidence_paths), "arm_visible_source_missing", "initial visible path is outside universe")
    seed = scenario["context_seed"]
    if seed is not None:
        _require(set(seed["requested_source_paths"]).issubset(evidence_paths), "context_seed_source_missing", "requested source is outside universe")
    if validate_grounding:
        _validate_source_grounding(view, evidence_items, temp_parent=repo_root)
    return {
        "scenario_id": scenario["scenario_id"],
        "scenario_digest": scenario["scenario_digest"],
        "evidence_universe_digest": evidence_universe_digest(scenario),
        "task_text_digest": task_text_digest(scenario),
        "repo_view": scenario["repo_view"],
        "source_grounding": "M2A_M2B_M2C_DISPOSABLE_CHECK_PASS",
    }


def _seed_s5_resolution(view: CommittedRepoView, scenario: Mapping[str, Any], *, temp_parent: str | Path) -> dict[str, Any]:
    """Build the seeded S5 request/resolution through real M2c constructors."""

    seed = scenario["context_seed"]
    _require(seed is not None, "context_seed_missing", "S5 must carry a context seed")
    with tempfile.TemporaryDirectory(prefix=".bdb-m2d-s5-", dir=str(Path(temp_parent).resolve())) as raw_root:
        root = Path(raw_root)
        content_store = ImmutableContentStore(root)
        with DurableBindingStore(root, content_store=content_store) as bindings:
            intent = IntentBasis(
                "m2d:S5",
                "v1",
                semantic_digest({"scenario": "S5", "commit": FROZEN_COMMIT}),
            )
            gap = Unknown.create(view, subject="ContextPackage grounding", dimension=seed["gap_dimension"], reason=seed["gap_reason"])
            unrelated = Unknown.create(view, subject="decision applicability", dimension=seed["unrelated_gap_dimension"], reason=seed["unrelated_gap_reason"])
            initial = RepositoryUnderstandingView.create(
                intent,
                view,
                requested_dimensions=[seed["gap_dimension"], seed["unrelated_gap_dimension"]],
                must_see_categories=[seed["gap_dimension"]],
                unknowns=[gap, unrelated],
            )
            prior = ContextPackage.from_understanding(initial, horizon="COMPONENT")
            request = ContextRequest.create(
                prior,
                gap_ids=[gap.unknown_id],
                horizon="COMPONENT",
                requested_dimensions=[seed["gap_dimension"]],
                requested_evidence=seed["requested_source_paths"],
                question="Which exact accepted M2b source edges establish package grounding?",
                reason="repair only the visible package-grounding gap",
            )
            evidences: list[RepoSourceEvidence] = []
            fragments: list[TypedContextFragment] = []
            for path in seed["requested_source_paths"]:
                evidence = publish_repo_source_evidence(
                    view,
                    path,
                    content_store,
                    bindings,
                    fragment_type="text/plain",
                    fragment_schema="m2d-source-v1",
                )
                evidences.append(evidence)
                fragments.append(bindings.resolve_accepted(evidence.fragment_id, expected_view=view).fragment)
            claim = UnderstandingClaim.create(
                view,
                subject="ContextPackage source grounding",
                dimension=seed["gap_dimension"],
                kind="FACT",
                statement="Package grounding requires live validation against the exact RepoView and accepted M2b bindings.",
                source_evidence=evidences,
                binding_store=bindings,
            )
            dimension_binding = CoverageBinding.create(
                view,
                target_kind="DIMENSION",
                target=seed["gap_dimension"],
                supporting_claim_ids=[claim.claim_id],
                supporting_fragment_ids=[fragment.fragment_id for fragment in fragments],
            )
            must_see_binding = CoverageBinding.create(
                view,
                target_kind="MUST_SEE",
                target=seed["gap_dimension"],
                supporting_claim_ids=[claim.claim_id],
                supporting_fragment_ids=[fragment.fragment_id for fragment in fragments],
            )
            resulting_understanding = RepositoryUnderstandingView.create(
                intent,
                view,
                claims=[claim],
                requested_dimensions=[seed["gap_dimension"], seed["unrelated_gap_dimension"]],
                covered_dimensions=[seed["gap_dimension"]],
                must_see_categories=[seed["gap_dimension"]],
                covered_must_see=[seed["gap_dimension"]],
                coverage_bindings=[dimension_binding, must_see_binding],
                unknowns=[unrelated],
                binding_store=bindings,
            )
            resulting = ContextPackage.from_understanding(
                resulting_understanding,
                horizon="COMPONENT",
                included_fragment_ids=[fragment.fragment_id for fragment in fragments],
            )
            resulting.validate_source_grounding(resulting_understanding, view, bindings)
            gap_evidence = GapResolutionEvidence.create(
                gap_id=gap.unknown_id,
                added_fragment_ids=[fragment.fragment_id for fragment in fragments],
                supporting_claim_ids=[claim.claim_id],
                coverage_binding_ids=[dimension_binding.coverage_binding_id, must_see_binding.coverage_binding_id],
            )
            resolution = ContextResolution.create(
                request,
                prior,
                resulting_package=resulting,
                added_fragments=fragments,
                binding_store=bindings,
                resolved_gap_ids=[gap.unknown_id],
                gap_resolution_evidence=[gap_evidence],
            )
            _require(gap.unknown_id not in resulting.gap_ids, "s5_gap_not_resolved", "requested gap remains visible")
            _require(unrelated.unknown_id in resulting.gap_ids, "s5_unrelated_gap_lost", "unrelated gap was suppressed")
            _require(resolution.outcome == "RESOLVED", "s5_resolution_not_resolved", "S5 fixture did not resolve")
            return {
                "initial_coverage_status": initial.coverage_status,
                "initial_gap_id": gap.unknown_id,
                "unrelated_gap_id": unrelated.unknown_id,
                "request_id": request.request_id,
                "resulting_coverage_status": resulting.coverage_status,
                "resolution_id": resolution.resolution_id,
                "resolved_gap_ids": list(resolution.resolved_gap_ids),
                "unresolved_gap_ids": list(resolution.unresolved_gap_ids),
            }


def _validate_prompt_pair(scenario: Mapping[str, Any], run_dir: Path) -> None:
    x = run_dir / "arm_x_prompt.md"
    y = run_dir / "arm_y_prompt.md"
    ground = run_dir / "evaluator_ground_truth.json"
    sheet = run_dir / "evaluator_sheet.json"
    for path in (x, y, ground, sheet):
        _require(path.is_file(), "browser_artifact_missing", str(path))
    x_text = x.read_text(encoding="utf-8")
    y_text = y.read_text(encoding="utf-8")
    forbidden = ("evaluator_ground_truth", "answer_key", "preferred option", "BASELINE_FLAT_CONTEXT_V1", "M2_VNEXT_CONTEXT_PACKAGE_V1")
    for text in (x_text, y_text):
        _require(scenario["repo_view"]["commit_oid"] in text, "prompt_basis_missing", scenario["scenario_id"])
        for item in scenario["arm_construction"]["initial_visible_paths"]:
            _require(item in text, "prompt_source_missing", item)
        _require(not any(term in text for term in forbidden), "prompt_leaks_arm_or_answer_key", scenario["scenario_id"])
        _require(not any(term in text.casefold() for term in ("fragment_id", "envelope", "protocol generation", "retry token")), "prompt_leaks_protocol", scenario["scenario_id"])
    _require(x_text != y_text, "prompt_arms_identical", scenario["scenario_id"])
    ground_truth = _load_json(ground)
    _require(ground_truth.get("schema") == GROUND_TRUTH_SCHEMA, "ground_truth_schema_mismatch", scenario["scenario_id"])
    _require(ground_truth.get("scenario_id") == scenario["scenario_id"], "ground_truth_scenario_mismatch", scenario["scenario_id"])
    _require(ground_truth.get("scenario_digest") == scenario["scenario_digest"], "ground_truth_digest_mismatch", scenario["scenario_id"])
    _require(ground_truth.get("evidence_universe_digest") == evidence_universe_digest(scenario), "ground_truth_evidence_digest_mismatch", scenario["scenario_id"])
    sheet_data = _load_json(sheet)
    _require(sheet_data.get("schema") == EVALUATOR_SHEET_SCHEMA, "evaluator_sheet_schema_mismatch", scenario["scenario_id"])
    _require(sheet_data.get("scenario_digest") == scenario["scenario_digest"], "evaluator_sheet_digest_mismatch", scenario["scenario_id"])


def validate_packet(repo_root: str | Path, *, packet_root: str | Path | None = None) -> dict[str, Any]:
    root = Path(packet_root) if packet_root is not None else Path(repo_root) / "benchmarks" / "m2d"
    scenarios_root = root / "scenarios"
    results: list[dict[str, Any]] = []
    loaded: list[dict[str, Any]] = []
    for scenario_id in REQUIRED_SCENARIOS:
        candidates = list(scenarios_root.glob(f"{scenario_id}-*.json"))
        _require(len(candidates) == 1, "scenario_file_missing_or_ambiguous", scenario_id)
        scenario = _load_json(candidates[0])
        _require(scenario.get("scenario_id") == scenario_id, "scenario_id_filename_mismatch", scenario_id)
        loaded.append(scenario)
    view = _subject_view(repo_root)
    union_by_path = {
        item["path"]: item
        for scenario in loaded
        for item in scenario["evidence_universe"]
    }
    _validate_source_grounding(view, tuple(union_by_path.values()), temp_parent=repo_root)
    for scenario in loaded:
        result = validate_scenario(scenario, repo_root=repo_root, validate_grounding=False)
        _validate_prompt_pair(scenario, root / "browser_runs" / scenario["scenario_id"])
        if scenario["scenario_id"] == "S5":
            result["context_seed_validation"] = _seed_s5_resolution(view, scenario, temp_parent=repo_root)
        results.append(result)
    return {
        "status": "READY_FOR_BROWSER_EXECUTION",
        "frozen_commit": FROZEN_COMMIT,
        "repo_view": frozen_repo_view_dict(),
        "scenario_count": len(results),
        "scenarios": results,
        "browser_runs": "NOT_YET_EXECUTED",
        "production_activation": {"runtime": "OFF", "writer": "OFF", "activation": "OFF"},
    }


def derive_gate_decision(evaluations: Sequence[Mapping[str, Any]], *, browser_runs_present: bool = True) -> dict[str, Any]:
    """Apply categorical M2d policy without creating an aggregate score."""

    by_id = {item.get("scenario_id"): item for item in evaluations}
    missing = [item for item in REQUIRED_SCENARIOS if item not in by_id]
    if missing or not browser_runs_present:
        return {"outcome": "READY_FOR_BROWSER_EXECUTION", "missing_scenarios": missing, "no_aggregate_score": True}
    for scenario_id, evaluation in by_id.items():
        fairness = evaluation.get("fairness", {})
        if not all(fairness.get(key) is True for key in ("same_model", "same_settings", "same_repo_view", "same_evidence_universe", "same_task", "browser_parity")):
            return {"outcome": "INCONCLUSIVE", "reason": f"fairness failure in {scenario_id}", "no_aggregate_score": True}
        if evaluation.get("hard_failures"):
            return {"outcome": "FAIL", "reason": f"hard failure in {scenario_id}", "no_aggregate_score": True}
    for scenario_id in ("S1", "S2"):
        judgments = [item.get("judgment") for item in by_id[scenario_id].get("vector_evaluations", [])]
        if any(value == "WORSE" for value in judgments):
            return {"outcome": "FAIL", "reason": f"small-task regression in {scenario_id}", "no_aggregate_score": True}
        if any(value == "INCONCLUSIVE" for value in judgments) or not judgments or any(value not in {"BETTER", "EQUIVALENT", "N/A"} for value in judgments):
            return {"outcome": "INCONCLUSIVE", "reason": f"small-task ambiguity in {scenario_id}", "no_aggregate_score": True}
    s5 = by_id["S5"].get("context_request", {})
    required_context = all(s5.get(key) is True for key in ("gap_visible", "requested_exact_evidence", "unrelated_gaps_preserved", "answer_improved_or_narrowed")) and s5.get("outcome") == "RESOLVED" and s5.get("protocol_bookkeeping_required") is False
    complex_improvement = any(by_id[item].get("material_improvement") is True for item in ("S3", "S4", "S5"))
    if not required_context:
        return {"outcome": "FAIL", "reason": "S5 ContextRequest rule not satisfied", "no_aggregate_score": True}
    if not complex_improvement:
        return {"outcome": "FAIL", "reason": "no complex scenario material improvement", "no_aggregate_score": True}
    return {"outcome": "PASS", "no_aggregate_score": True}


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("validate", help="validate the frozen packet and disposable grounding")
    check.add_argument("--repo-root", default=".")
    check.add_argument("--packet-root", default=None)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            result = validate_packet(args.repo_root, packet_root=args.packet_root)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
    except M2dValidationError as exc:
        print(json.dumps({"status": "INVALID", "code": exc.code, "message": str(exc), "details": exc.details}, indent=2), file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())

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
import datetime as _datetime
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
    ContextAffordance,
    CoverageBinding,
    GapResolutionEvidence,
    IntentBasis,
    Omission,
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
ASSET_MANIFEST_SCHEMA = "bdb-vnext-m2d-payload-manifest-v1"
ASSET_CONTRACT_VERSION = "m2d-browser-assets-v1"
RUN_SCHEMA = "bdb-vnext-m2d-run-v2"
FOLLOWUP_OPERATOR_MESSAGE = "Here is the additional exact source context available for the requested package-grounding question."
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ALLOWED_JUDGMENTS = frozenset({"BETTER", "EQUIVALENT", "WORSE", "INCONCLUSIVE", "N/A"})
_CORE_IMPROVEMENT_VECTOR_IDS = frozenset({
    "repository_understanding_must_see_coverage",
    "root_cause_or_ownership_accuracy",
    "explicit_unknowns_uncertainty",
    "constraint_violations",
    "engineering_decision_quality_tradeoffs",
    "validation_relevance_missed_risks",
    "rework_required",
})
_CONTEXT_REQUEST_OUTCOMES = frozenset({"NOT_APPLICABLE", "NOT_REQUESTED", "RESOLVED", "DENIED", "UNAVAILABLE", "INCONCLUSIVE"})
_EXACT_SHA256_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])sha256:[0-9a-f]{64}(?![A-Za-z0-9])")


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
        "browser_assets": scenario["browser_assets"],
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
        # The benchmark subject is an immutable Git object, never the moving
        # bdb-vnext branch or the checkout filesystem.
        view = resource.resolve_committed(FROZEN_COMMIT, observed_at=FROZEN_OBSERVED_AT)
    except RepoViewError as exc:
        raise M2dValidationError("repo_view_unavailable", str(exc)) from exc
    expected = frozen_repo_view_dict()
    actual = RepoViewBinding.from_view(view).as_dict()
    _require(actual == expected, "frozen_basis_mismatch", "bdb-vnext is not the frozen M2d subject", expected=expected, actual=actual)
    return view


def _validate_scenario_shape(scenario: Mapping[str, Any], *, allow_asset_placeholder: bool = False) -> None:
    required = {
        "schema", "scenario_version", "scenario_id", "scenario_family", "task_text", "repo_view",
        "evidence_universe", "must_see_ground_truth", "known_constraints", "known_unknowns",
        "source_inference_distinctions", "adjudication_vector_ids", "arm_construction", "context_seed",
        "rubric_version", "gate_policy_version", "scenario_digest",
        "browser_assets",
    }
    _require(set(scenario) == required, "scenario_field_set", "scenario has unexpected or missing fields")
    _require(scenario["schema"] == SCENARIO_SCHEMA, "scenario_schema_mismatch", "unsupported scenario schema")
    _require(str(scenario["scenario_id"]) in REQUIRED_SCENARIOS, "scenario_id_invalid", "scenario id is not S1-S5")
    _require(_DIGEST_RE.fullmatch(str(scenario["scenario_digest"])) is not None, "scenario_digest_invalid", "scenario digest is malformed")
    _require(scenario["rubric_version"] == RUBRIC_VERSION, "rubric_version_mismatch", "scenario rubric is not frozen")
    _require(scenario["gate_policy_version"] == GATE_POLICY_VERSION, "policy_version_mismatch", "scenario policy is not frozen")
    _validate_browser_assets_shape(scenario["browser_assets"], scenario["scenario_id"])
    _require(scenario["arm_construction"] == {
        "version": ARM_CONSTRUCTION_VERSION,
        "baseline_type": "BASELINE_FLAT_CONTEXT_V1",
        "m2_type": "M2_VNEXT_CONTEXT_PACKAGE_V1",
        "initial_visible_paths": scenario["arm_construction"]["initial_visible_paths"],
    }, "arm_construction_invalid", "scenario arm construction is malformed")
    _require(scenario["repo_view"] == frozen_repo_view_dict(), "scenario_repo_view_mismatch", "scenario RepoView differs from frozen basis")
    computed = scenario_digest(scenario)
    if not allow_asset_placeholder:
        _require(scenario["scenario_digest"] == computed, "scenario_digest_mismatch", "scenario digest does not match immutable scenario", expected=computed)


def _validate_browser_assets_shape(assets: Mapping[str, Any], scenario_id: str) -> None:
    required = {
        "version",
        "arm_x_prompt_sha256",
        "arm_y_prompt_sha256",
        "arm_x_initial_payload_manifest_digest",
        "arm_x_initial_payload_manifest_sha256",
        "arm_x_initial_payload_digest",
        "arm_y_initial_payload_manifest_digest",
        "arm_y_initial_payload_manifest_sha256",
        "arm_y_initial_payload_digest",
        "s5_followup_operator_message_sha256",
        "s5_followup_payload_manifest_digest",
        "s5_followup_payload_manifest_sha256",
        "s5_followup_payload_digest",
    }
    _require(isinstance(assets, Mapping) and set(assets) == required, "browser_assets_field_set", scenario_id)
    _require(assets["version"] == ASSET_CONTRACT_VERSION, "browser_assets_version_mismatch", scenario_id)
    for field, value in assets.items():
        if field == "version":
            continue
        if value is not None:
            _require(_DIGEST_RE.fullmatch(str(value)) is not None, "browser_asset_digest_invalid", f"{scenario_id}:{field}")
    if scenario_id == "S5":
        _require(assets["s5_followup_operator_message_sha256"] is not None, "s5_followup_message_missing", scenario_id)
        _require(assets["s5_followup_payload_manifest_digest"] is not None, "s5_followup_manifest_missing", scenario_id)
        _require(assets["s5_followup_payload_digest"] is not None, "s5_followup_payload_missing", scenario_id)
    else:
        _require(all(assets[field] is None for field in (
            "s5_followup_operator_message_sha256",
            "s5_followup_payload_manifest_digest",
            "s5_followup_payload_digest",
        )), "non_s5_followup_assets_present", scenario_id)


def _validate_source_grounding(
    view: CommittedRepoView | None,
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
            initial_context = _build_s5_initial_context(view, scenario)
            intent = initial_context["intent"]
            gap = initial_context["gap"]
            unrelated = initial_context["unrelated"]
            initial = initial_context["understanding"]
            prior = initial_context["package"]
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
                "initial_understanding_id": initial.understanding_id,
                "initial_package_id": prior.package_id,
                "initial_gap_id": gap.unknown_id,
                "unrelated_gap_id": unrelated.unknown_id,
                "request_id": request.request_id,
                "request_source_package_id": request.source_package_id,
                "resulting_coverage_status": resulting.coverage_status,
                "resolution_id": resolution.resolution_id,
                "resolution_prior_package_id": resolution.prior_package_id,
                "resolution_resulting_package_id": resolution.resulting_package_id,
                "resolved_gap_ids": list(resolution.resolved_gap_ids),
                "unresolved_gap_ids": list(resolution.unresolved_gap_ids),
            }


def _sha256_bytes(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _scenario_path_map(scenario: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {item["path"]: item for item in scenario["evidence_universe"]}


def _source_objects(view: CommittedRepoView, scenario: Mapping[str, Any], paths: Sequence[str]) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    items = _scenario_path_map(scenario)
    records: list[dict[str, Any]] = []
    raw_by_path: dict[str, bytes] = {}
    for path in paths:
        _require(path in items, "payload_source_outside_universe", path)
        raw = view.query().read_bytes(path)
        item = items[path]
        _require(_sha256_bytes(raw) == item["raw_sha256"], "payload_source_hash_mismatch", path)
        records.append({
            "path": path,
            "object_id": item["object_id"],
            "size_bytes": item["size_bytes"],
            "raw_sha256": item["raw_sha256"],
        })
        raw_by_path[path] = raw
    return records, raw_by_path


def _new_m2_intent(scenario: Mapping[str, Any]) -> IntentBasis:
    return IntentBasis(
        f"m2d:{scenario['scenario_id']}",
        "v1",
        semantic_digest({"scenario_id": scenario["scenario_id"], "commit_oid": FROZEN_COMMIT}),
    )


def _build_s5_initial_context(
    view: CommittedRepoView,
    scenario: Mapping[str, Any],
    *,
    intent: IntentBasis | None = None,
) -> dict[str, Any]:
    """Construct the one canonical, model-facing S5 initial package.

    This helper intentionally stops at the immutable partial ContextPackage.
    A ContextRequest is created only by the internal follow-up fixture after
    this exact package has been rendered to the treatment manifest.
    """

    seed = scenario.get("context_seed")
    _require(scenario["scenario_id"] == "S5" and seed is not None, "context_seed_missing", "S5 must carry a context seed")
    basis = _new_m2_intent(scenario) if intent is None else intent
    gap = Unknown.create(
        view,
        subject="ContextPackage grounding",
        dimension=seed["gap_dimension"],
        reason=seed["gap_reason"],
    )
    unrelated = Unknown.create(
        view,
        subject="decision applicability",
        dimension=seed["unrelated_gap_dimension"],
        reason=seed["unrelated_gap_reason"],
    )
    understanding = RepositoryUnderstandingView.create(
        basis,
        view,
        # Keep the initial package genuinely partial and leak-free: visible
        # unknowns are preserved, but no task-specific request is predeclared.
        requested_dimensions=[],
        must_see_categories=[],
        unknowns=[gap, unrelated],
    )
    package = ContextPackage.from_understanding(
        understanding,
        horizon="COMPONENT",
        affordances=[ContextAffordance.create(
            dimension=seed["gap_dimension"],
            horizon="COMPONENT",
            evidence_type="repository-source",
            reason="Request exact committed source context when package grounding is incomplete.",
        )],
    )
    return {
        "intent": basis,
        "gap": gap,
        "unrelated": unrelated,
        "understanding": understanding,
        "package": package,
    }


def _m2_context_records(
    view: CommittedRepoView,
    scenario: Mapping[str, Any],
    paths: Sequence[str],
    *,
    runtime_root: Path,
    initial: bool,
) -> dict[str, Any]:
    """Build a model-facing projection from the accepted M2c contracts.

    This is disposable benchmark construction.  It deliberately renders only
    human-facing semantic fields; internal fragment/message identifiers remain
    in the machine manifest and are not presented as model reasoning tasks.
    """

    content_store = ImmutableContentStore(runtime_root)
    with DurableBindingStore(runtime_root, content_store=content_store) as binding_store:
        intent = _new_m2_intent(scenario)
        evidences: list[RepoSourceEvidence] = []
        fragments: list[TypedContextFragment] = []
        for path in paths:
            evidence = publish_repo_source_evidence(
                view,
                path,
                content_store,
                binding_store,
                fragment_type="text/plain",
                fragment_schema="m2d-source-v1",
            )
            evidences.append(evidence)
            fragments.append(binding_store.resolve_accepted(evidence.fragment_id, expected_view=view).fragment)

        seed = scenario.get("context_seed")
        if scenario["scenario_id"] == "S5":
            initial_context = _build_s5_initial_context(view, scenario, intent=intent)
            gap = initial_context["gap"]
            unrelated = initial_context["unrelated"]
            if initial:
                return {
                    "understanding": initial_context["understanding"],
                    "package": initial_context["package"],
                    "initial_context": initial_context,
                    "claims": (),
                    "evidences": tuple(evidences),
                    "fragments": tuple(fragments),
                    # The model must formulate this request.  The canonical
                    # request is constructed only by the internal S5 fixture
                    # and by the post-request follow-up construction below.
                    "context_request": None,
                    "context_resolution": None,
                }
            claim = UnderstandingClaim.create(
                view,
                subject="ContextPackage source grounding",
                dimension=seed["gap_dimension"],
                kind="FACT",
                statement="Package grounding is validated against the exact RepoView and accepted M2b source evidence.",
                source_evidence=evidences,
                binding_store=binding_store,
            )
            coverage = CoverageBinding.create(
                view,
                target_kind="DIMENSION",
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
                coverage_bindings=[coverage],
                unknowns=[unrelated],
                binding_store=binding_store,
            )
            resulting_package = ContextPackage.from_understanding(
                resulting_understanding,
                horizon="COMPONENT",
                included_fragment_ids=[fragment.fragment_id for fragment in fragments],
                affordances=[ContextAffordance.create(
                    dimension=seed["unrelated_gap_dimension"],
                    horizon="COMPONENT",
                    evidence_type="repository-source",
                    reason="Decision applicability remains an explicit visible gap.",
                )],
            )
            # The request/resolution edge must start from the exact initial
            # package rendered by the model-facing initial treatment.
            prior_package = initial_context["package"]
            request = ContextRequest.create(
                prior_package,
                gap_ids=[gap.unknown_id],
                horizon="COMPONENT",
                requested_dimensions=[seed["gap_dimension"]],
                requested_evidence=seed["requested_source_paths"],
                question="Which exact accepted M2b source edges establish package grounding?",
                reason="repair only the visible package-grounding gap",
            )
            resolution_evidence = GapResolutionEvidence.create(
                gap_id=gap.unknown_id,
                added_fragment_ids=[fragment.fragment_id for fragment in fragments],
                supporting_claim_ids=[claim.claim_id],
                coverage_binding_ids=[coverage.coverage_binding_id],
            )
            resolution = ContextResolution.create(
                request,
                prior_package,
                resulting_package=resulting_package,
                added_fragments=fragments,
                binding_store=binding_store,
                resolved_gap_ids=[gap.unknown_id],
                gap_resolution_evidence=[resolution_evidence],
            )
            resulting_package.validate_source_grounding(resulting_understanding, view, binding_store)
            return {
                "understanding": resulting_understanding,
                "package": resulting_package,
                "initial_context": initial_context,
                "claims": (claim,),
                "evidences": tuple(evidences),
                "fragments": tuple(fragments),
                "context_request": request,
                "context_resolution": resolution,
            }

        claims: list[UnderstandingClaim] = []
        for evidence in evidences:
            claims.append(UnderstandingClaim.create(
                view,
                subject=evidence.source_path,
                dimension="source-context",
                kind="FACT",
                statement=f"The exact committed source bytes for {evidence.source_path} are available.",
                source_evidence=[evidence],
                binding_store=binding_store,
            ))
        coverage = CoverageBinding.create(
            view,
            target_kind="DIMENSION",
            target="source-context",
            supporting_claim_ids=[claim.claim_id for claim in claims],
            supporting_fragment_ids=[fragment.fragment_id for fragment in fragments],
        )
        # S1-S4 treatment construction is source-grounded and epistemically
        # neutral. Benchmark-author evaluator annotations are not model-facing
        # treatment input.
        unknowns: list[Unknown] = []
        omission = Omission.create(
            view,
            dimension="task-boundary",
            reason="This read-only benchmark context does not authorize implementation or outcome claims.",
        )
        understanding = RepositoryUnderstandingView.create(
            intent,
            view,
            claims=claims,
            requested_dimensions=["source-context"],
            covered_dimensions=["source-context"],
            coverage_bindings=[coverage],
            unknowns=unknowns,
            omissions=[omission],
            binding_store=binding_store,
        )
        package = ContextPackage.from_understanding(
            understanding,
            horizon="COMPONENT",
            included_fragment_ids=[fragment.fragment_id for fragment in fragments],
            affordances=[ContextAffordance.create(
                dimension="source-context",
                horizon="COMPONENT",
                evidence_type="repository-source",
                reason="Request exact committed source context if a task gap remains.",
            )],
        )
        package.validate_source_grounding(understanding, view, binding_store)
        return {
            "understanding": understanding,
            "package": package,
            "claims": tuple(claims),
            "evidences": tuple(evidences),
            "fragments": tuple(fragments),
            "context_request": None,
            "context_resolution": None,
        }


def _flat_payload(scenario: Mapping[str, Any], view: CommittedRepoView, paths: Sequence[str], *, phase: str) -> bytes:
    records, raw_by_path = _source_objects(view, scenario, paths)
    lines = [
        "# BDB vNext M2d benchmark context",
        "",
        "BENCHMARK_ONLY",
        "NOT_RUNTIME_AUTHORITY",
        "NOT_LEGACY_FALLBACK",
        "ARM X — conventional flat context",
        "",
        f"Task: {scenario['task_text']}",
        f"Phase: {phase}",
        f"Repository: {scenario['repo_view']['repository_id']}",
        f"Committed commit: {scenario['repo_view']['commit_oid']}",
        f"Committed tree: {scenario['repo_view']['tree_oid']}",
        f"RepoView: {scenario['repo_view']['view_id']}",
        "",
        "The following exact committed source subjects are available:",
    ]
    for record in records:
        lines.extend([
            "",
            f"## SOURCE {record['path']}",
            f"object: {record['object_id']}",
            f"size_bytes: {record['size_bytes']}",
            f"raw_sha256: {record['raw_sha256']}",
            "```text",
            raw_by_path[record["path"]].decode("utf-8"),
            "```",
        ])
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def _m2_payload(scenario: Mapping[str, Any], view: CommittedRepoView, paths: Sequence[str], *, phase: str, runtime_root: Path) -> tuple[bytes, dict[str, Any]]:
    records, raw_by_path = _source_objects(view, scenario, paths)
    context = _m2_context_records(view, scenario, paths, runtime_root=runtime_root, initial=phase == "INITIAL")
    understanding: RepositoryUnderstandingView = context["understanding"]
    package: ContextPackage = context["package"]
    lines = [
        "# BDB vNext M2d benchmark context",
        "",
        "BENCHMARK_ONLY",
        "M2 context package presentation",
        "ARM Y — typed read-only engineering context",
        "",
        f"Task: {scenario['task_text']}",
        f"Phase: {phase}",
        f"Repository: {package.repo_view.repository_id}",
        f"Committed commit: {package.repo_view.commit_oid}",
        f"Committed tree: {package.repo_view.tree_oid}",
        f"RepoView: {package.repo_view.view_id}",
        "Source authority: exact Git object database through RepoSourceEvidence and accepted M2b bindings.",
        "",
        "## ContextPackage semantic projection",
        f"coverage_status: {package.coverage_status}",
        f"requested_dimensions: {', '.join(package.requested_dimensions) or '(none)'}",
        f"covered_dimensions: {', '.join(package.covered_dimensions) or '(none)'}",
        f"visible_unknowns: {len(package.unknowns)}",
        f"visible_omissions: {len(package.omissions)}",
        f"claim_classes: FACT={len(package.fact_claim_ids)}, INFERENCE={len(package.inference_claim_ids)}, ASSUMPTION={len(package.assumption_claim_ids)}, HYPOTHESIS={len(package.hypothesis_claim_ids)}",
        f"included source subjects: {len(package.included_fragment_ids)}",
        "",
        "### Visible unknowns",
    ]
    if package.unknowns:
        lines.extend(f"- {item.dimension}: {item.reason}" for item in package.unknowns)
    else:
        lines.append("- none")
    lines.extend(["", "### Visible omissions"])
    if package.omissions:
        lines.extend(f"- {item.dimension}: {item.reason}" for item in package.omissions)
    else:
        lines.append("- none")
    lines.extend(["", "### Source-backed claim classes"])
    for claim in context["claims"]:
        lines.append(f"- FACT / EXACT_SOURCE: committed source evidence for {claim.subject}")
    if not context["claims"]:
        lines.append("- no source-backed claim is asserted for the initial partial package")
    lines.extend(["", "### Context affordances"])
    for affordance in package.affordances:
        lines.append(f"- {affordance.dimension} ({affordance.horizon}): {affordance.reason}")
    if context["context_request"] is not None:
        request: ContextRequest = context["context_request"]
        lines.extend(["", "### Natural-language context request"])
        lines.append(f"- requested source paths: {', '.join(request.requested_evidence)}")
        lines.append(f"- reason: {request.reason}")
    if context["context_resolution"] is not None:
        resolution: ContextResolution = context["context_resolution"]
        lines.extend(["", "### Context resolution"])
        lines.append(f"- outcome: {resolution.outcome}")
        lines.append(f"- resolved gaps: {len(resolution.resolved_gap_ids)}")
        lines.append(f"- remaining gaps: {len(resolution.unresolved_gap_ids)}")
    lines.extend(["", "### Source-vs-inference boundary", "FACT claims above are limited to exact committed source evidence. Any engineering conclusion beyond those records remains an inference or an explicit unknown.", "", "## Exact committed source subjects"])
    for record in records:
        lines.extend([
            "",
            f"## SOURCE {record['path']}",
            f"object: {record['object_id']}",
            f"size_bytes: {record['size_bytes']}",
            f"raw_sha256: {record['raw_sha256']}",
            "```text",
            raw_by_path[record["path"]].decode("utf-8"),
            "```",
        ])
    extra = {
        "understanding_id": understanding.understanding_id,
        "package_id": package.package_id,
        "coverage_status": package.coverage_status,
        "requested_dimensions": list(package.requested_dimensions),
        "covered_dimensions": list(package.covered_dimensions),
        "visible_unknown_dimensions": [item.dimension for item in package.unknowns],
        "visible_omission_dimensions": [item.dimension for item in package.omissions],
        "claim_classes": {
            "FACT": len(package.fact_claim_ids),
            "INFERENCE": len(package.inference_claim_ids),
            "ASSUMPTION": len(package.assumption_claim_ids),
            "HYPOTHESIS": len(package.hypothesis_claim_ids),
        },
        "context_request": context["context_request"] is not None,
        "context_resolution": None if context["context_resolution"] is None else context["context_resolution"].outcome,
    }
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8"), extra


def _payload_manifest(core: Mapping[str, Any]) -> tuple[bytes, str]:
    manifest_digest = semantic_digest(core)
    document = {**core, "manifest_digest": manifest_digest}
    return canonical_json_bytes(document), manifest_digest


def _materialize_one(
    view: CommittedRepoView,
    scenario: Mapping[str, Any],
    *,
    output: Path,
    arm_id: str,
    paths: Sequence[str],
    phase: str,
    runtime_root: Path | None,
) -> dict[str, Any]:
    scenario_id = scenario["scenario_id"]
    arm_type = "BASELINE_FLAT_CONTEXT_V1" if arm_id == "X" else "M2_VNEXT_CONTEXT_PACKAGE_V1"
    relative_context = f"{scenario_id}/{('arm_x' if arm_id == 'X' else 'arm_y')}_{'initial_' if phase == 'INITIAL' else ''}context.md"
    relative_manifest = f"{scenario_id}/{('arm_x' if arm_id == 'X' else 'arm_y')}_{'initial_' if phase == 'INITIAL' else ''}manifest.json"
    if arm_id == "X":
        payload = _flat_payload(scenario, view, paths, phase=phase)
        semantic_extra: dict[str, Any] = {}
    else:
        _require(runtime_root is not None, "m2_runtime_missing", "ARM Y materialization requires disposable M2c runtime")
        payload, semantic_extra = _m2_payload(scenario, view, paths, phase=phase, runtime_root=runtime_root)
    records, _ = _source_objects(view, scenario, paths)
    core = {
        "schema": ASSET_MANIFEST_SCHEMA,
        "asset_contract_version": ASSET_CONTRACT_VERSION,
        "scenario_id": scenario_id,
        "arm_id": arm_id,
        "arm_type": arm_type,
        "phase": phase,
        "repo_view": scenario["repo_view"],
        "evidence_universe_digest": evidence_universe_digest(scenario),
        "source_objects": records,
        "source_object_set": sorted(item["object_id"] for item in records),
        "payload_relative_path": relative_context,
        "manifest_relative_path": relative_manifest,
        "benchmark_markers": ["BENCHMARK_ONLY", "NOT_RUNTIME_AUTHORITY", "NOT_LEGACY_FALLBACK"],
    }
    if semantic_extra:
        core["m2_context"] = semantic_extra
    manifest_bytes, manifest_digest = _payload_manifest(core)
    (output / relative_context).parent.mkdir(parents=True, exist_ok=True)
    (output / relative_context).write_bytes(payload)
    (output / relative_manifest).write_bytes(manifest_bytes)
    return {
        "context_path": relative_context,
        "manifest_path": relative_manifest,
        "payload_digest": _sha256_bytes(payload),
        "manifest_digest": manifest_digest,
        "manifest_sha256": _sha256_bytes(manifest_bytes),
        "source_object_set": sorted(item["object_id"] for item in records),
        "source_paths": [item["path"] for item in records],
    }


def _materialize_followup(view: CommittedRepoView, scenario: Mapping[str, Any], *, output: Path) -> dict[str, Any]:
    seed = scenario["context_seed"]
    _require(seed is not None, "context_seed_missing", "S5 follow-up requires context seed")
    paths = list(seed["requested_source_paths"])
    records, raw_by_path = _source_objects(view, scenario, paths)
    lines = [
        "# BDB vNext M2d exact follow-up source context",
        "",
        "BENCHMARK_ONLY",
        "This is the same neutral exact source bundle for either arm.",
        "",
        f"Committed commit: {scenario['repo_view']['commit_oid']}",
        f"Committed tree: {scenario['repo_view']['tree_oid']}",
    ]
    for record in records:
        lines.extend([
            "",
            f"## SOURCE {record['path']}",
            f"object: {record['object_id']}",
            f"size_bytes: {record['size_bytes']}",
            f"raw_sha256: {record['raw_sha256']}",
            "```text",
            raw_by_path[record["path"]].decode("utf-8"),
            "```",
        ])
    payload = ("\n".join(lines).rstrip() + "\n").encode("utf-8")
    relative_context = "S5/followup_context.md"
    relative_manifest = "S5/followup_manifest.json"
    core = {
        "schema": ASSET_MANIFEST_SCHEMA,
        "asset_contract_version": ASSET_CONTRACT_VERSION,
        "scenario_id": "S5",
        "arm_id": "COMMON",
        "arm_type": "COMMON_FOLLOWUP_SOURCE_BUNDLE_V1",
        "phase": "FOLLOWUP",
        "repo_view": scenario["repo_view"],
        "evidence_universe_digest": evidence_universe_digest(scenario),
        "source_objects": records,
        "source_object_set": sorted(item["object_id"] for item in records),
        "payload_relative_path": relative_context,
        "manifest_relative_path": relative_manifest,
        "benchmark_markers": ["BENCHMARK_ONLY", "NOT_RUNTIME_AUTHORITY", "NOT_LEGACY_FALLBACK"],
    }
    manifest_bytes, manifest_digest = _payload_manifest(core)
    (output / relative_context).parent.mkdir(parents=True, exist_ok=True)
    (output / relative_context).write_bytes(payload)
    (output / relative_manifest).write_bytes(manifest_bytes)
    return {
        "context_path": relative_context,
        "manifest_path": relative_manifest,
        "payload_digest": _sha256_bytes(payload),
        "manifest_digest": manifest_digest,
        "manifest_sha256": _sha256_bytes(manifest_bytes),
        "source_object_set": sorted(item["object_id"] for item in records),
        "source_paths": [item["path"] for item in records],
    }


def _prompt_digest(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _expected_asset_fields(scenario: Mapping[str, Any], *, packet_root: Path, materialized: Mapping[str, Any]) -> dict[str, Any]:
    scenario_id = scenario["scenario_id"]
    x_prompt = _prompt_digest(packet_root / "browser_runs" / scenario_id / "arm_x_prompt.md")
    y_prompt = _prompt_digest(packet_root / "browser_runs" / scenario_id / "arm_y_prompt.md")
    x_initial = materialized["X_INITIAL"]
    y_initial = materialized["Y_INITIAL"]
    followup = materialized.get("FOLLOWUP")
    return {
        "version": ASSET_CONTRACT_VERSION,
        "arm_x_prompt_sha256": x_prompt,
        "arm_y_prompt_sha256": y_prompt,
        "arm_x_initial_payload_manifest_digest": x_initial["manifest_digest"],
        "arm_x_initial_payload_manifest_sha256": x_initial["manifest_sha256"],
        "arm_x_initial_payload_digest": x_initial["payload_digest"],
        "arm_y_initial_payload_manifest_digest": y_initial["manifest_digest"],
        "arm_y_initial_payload_manifest_sha256": y_initial["manifest_sha256"],
        "arm_y_initial_payload_digest": y_initial["payload_digest"],
        "s5_followup_operator_message_sha256": _sha256_bytes(FOLLOWUP_OPERATOR_MESSAGE.encode("utf-8")) if scenario_id == "S5" else None,
        "s5_followup_payload_manifest_digest": None if followup is None else followup["manifest_digest"],
        "s5_followup_payload_manifest_sha256": None if followup is None else followup["manifest_sha256"],
        "s5_followup_payload_digest": None if followup is None else followup["payload_digest"],
    }


def _validate_materialized_manifest(
    scenario: Mapping[str, Any],
    *,
    view: CommittedRepoView,
    output_root: Path,
    arm_id: str,
    phase: str,
    expected_manifest_digest: str,
    expected_manifest_sha256: str,
    expected_payload_digest: str,
    expected_paths: Sequence[str],
    packet_root: Path,
) -> dict[str, Any]:
    scenario_id = scenario["scenario_id"]
    prefix = "arm_x" if arm_id == "X" else "arm_y"
    stem = f"{prefix}_{'initial_' if phase == 'INITIAL' else ''}"
    manifest_path = output_root / scenario_id / f"{stem}manifest.json"
    _require(manifest_path.is_file(), "payload_manifest_missing", str(manifest_path))
    manifest_raw = manifest_path.read_bytes()
    _require(_sha256_bytes(manifest_raw) == expected_manifest_sha256, "payload_manifest_drift", str(manifest_path))
    manifest = _load_json(manifest_path)
    manifest_digest_value = manifest.pop("manifest_digest", None)
    _require(manifest_digest_value == semantic_digest(manifest), "payload_manifest_integrity_failure", str(manifest_path))
    _require(manifest_digest_value == expected_manifest_digest, "payload_manifest_digest_mismatch", str(manifest_path))
    _require(manifest.get("scenario_id") == scenario_id, "payload_manifest_scenario_mismatch", str(manifest_path))
    _require(manifest.get("arm_id") == arm_id, "payload_manifest_arm_mismatch", str(manifest_path))
    _require(manifest.get("phase") == phase, "payload_manifest_phase_mismatch", str(manifest_path))
    _require(manifest.get("repo_view") == scenario["repo_view"], "payload_manifest_repo_view_mismatch", scenario_id)
    _require(manifest.get("evidence_universe_digest") == evidence_universe_digest(scenario), "payload_manifest_universe_mismatch", scenario_id)
    actual_paths = [item["path"] for item in manifest.get("source_objects", [])]
    _require(actual_paths == list(expected_paths), "payload_source_paths_mismatch", scenario_id, expected=list(expected_paths), actual=actual_paths)
    expected_objects = _scenario_path_map(scenario)
    _require(set(manifest.get("source_object_set", [])) == {expected_objects[path]["object_id"] for path in expected_paths}, "payload_source_object_set_mismatch", scenario_id)
    context_path = output_root / manifest["payload_relative_path"]
    _require(context_path.is_file(), "payload_missing", str(context_path))
    payload_raw = context_path.read_bytes()
    _require(_sha256_bytes(payload_raw) == expected_payload_digest, "payload_drift", str(context_path))
    payload_text = payload_raw.decode("utf-8")
    _require("evaluator_ground_truth" not in payload_text and "evaluator_sheet" not in payload_text, "payload_answer_key_leak", scenario_id)
    _require("preferred answer" not in payload_text and "categorical verdict" not in payload_text, "payload_answer_key_leak", scenario_id)
    _require(re.search(r"S[1-5]-MS-", payload_text) is None, "payload_answer_key_leak", scenario_id)
    for item in scenario["must_see_ground_truth"]:
        requirement = item["requirement"]
        if requirement in payload_text and view is not None:
            source_text = "".join(view.read_text(source_path) for source_path in expected_paths)
            _require(requirement in source_text, "payload_ground_truth_leak", f"{scenario_id}:{item['item_id']}")
    return {
        "manifest_digest": manifest_digest_value,
        "manifest_sha256": _sha256_bytes(manifest_raw),
        "payload_digest": _sha256_bytes(payload_raw),
        "source_object_set": sorted(manifest.get("source_object_set", [])),
    }


def validate_materialized_packet(
    repo_root: str | Path,
    output_root: str | Path,
    *,
    packet_root: str | Path | None = None,
) -> dict[str, Any]:
    """Validate generated payload bytes and manifests against frozen identities."""

    output = Path(output_root).resolve()
    packet = Path(packet_root).resolve() if packet_root is not None else Path(repo_root).resolve() / "benchmarks" / "m2d"
    view = _subject_view(repo_root)
    results = []
    for scenario_id in REQUIRED_SCENARIOS:
        scenario_path = next(packet.joinpath("scenarios").glob(f"{scenario_id}-*.json"))
        scenario = _load_json(scenario_path)
        _validate_scenario_shape(scenario)
        initial_paths = list(scenario["arm_construction"]["initial_visible_paths"])
        assets = scenario["browser_assets"]
        x = _validate_materialized_manifest(
            scenario,
            view=view,
            output_root=output,
            arm_id="X",
            phase="INITIAL",
            expected_manifest_digest=assets["arm_x_initial_payload_manifest_digest"],
            expected_manifest_sha256=assets["arm_x_initial_payload_manifest_sha256"],
            expected_payload_digest=assets["arm_x_initial_payload_digest"],
            expected_paths=initial_paths,
            packet_root=packet,
        )
        y = _validate_materialized_manifest(
            scenario,
            view=view,
            output_root=output,
            arm_id="Y",
            phase="INITIAL",
            expected_manifest_digest=assets["arm_y_initial_payload_manifest_digest"],
            expected_manifest_sha256=assets["arm_y_initial_payload_manifest_sha256"],
            expected_payload_digest=assets["arm_y_initial_payload_digest"],
            expected_paths=initial_paths,
            packet_root=packet,
        )
        _require(x["source_object_set"] == y["source_object_set"], "paired_source_object_set_mismatch", scenario_id)
        if scenario_id in {"S1", "S2", "S3", "S4"}:
            _require(x["source_object_set"] == sorted(item["object_id"] for item in scenario["evidence_universe"]), "full_source_universe_missing", scenario_id)
        followup = None
        if scenario_id == "S5":
            seed_paths = list(scenario["context_seed"]["requested_source_paths"])
            manifest_path = output / "S5" / "followup_manifest.json"
            manifest_raw = manifest_path.read_bytes()
            _require(_sha256_bytes(manifest_raw) == assets["s5_followup_payload_manifest_sha256"], "s5_followup_manifest_drift", "S5")
            manifest = _load_json(manifest_path)
            digest = manifest.pop("manifest_digest", None)
            _require(digest == semantic_digest(manifest) == assets["s5_followup_payload_manifest_digest"], "s5_followup_manifest_digest_mismatch", "S5")
            _require([item["path"] for item in manifest["source_objects"]] == seed_paths, "s5_followup_paths_mismatch", "S5")
            followup_payload = output / "S5" / "followup_context.md"
            _require(_sha256_bytes(followup_payload.read_bytes()) == assets["s5_followup_payload_digest"], "s5_followup_payload_drift", "S5")
            _require(set(x["source_object_set"]) | {item["object_id"] for item in manifest["source_objects"]} == {item["object_id"] for item in scenario["evidence_universe"]}, "s5_universe_not_closed", "S5")
            followup = {"manifest_digest": digest, "payload_digest": _sha256_bytes(followup_payload.read_bytes())}
        results.append({"scenario_id": scenario_id, "arm_x": x, "arm_y": y, "followup": followup})
    return {"status": "M2D_PAYLOAD_VALIDATION_PASS", "scenarios": results, "frozen_commit": FROZEN_COMMIT}


def execution_environment_digest(environment: Mapping[str, Any]) -> str:
    return semantic_digest(dict(environment))


def _run_identity_payload(run: Mapping[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(dict(run))
    payload.pop("run_digest", None)
    return payload


def run_digest(run: Mapping[str, Any]) -> str:
    return semantic_digest(_run_identity_payload(run))


def _timestamp_ok(value: Any) -> bool:
    if not isinstance(value, str) or not re.fullmatch(r"20[0-9]{2}-[0-9]{2}-[0-9]{2}T[^ ]+Z", value):
        return False
    try:
        _datetime.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return True


def _validate_step_timing(step: Mapping[str, Any]) -> None:
    duration = step["processing_duration_seconds"]
    _require(
        isinstance(duration, int) and not isinstance(duration, bool) and duration >= 0,
        "run_processing_duration_invalid",
        "processing duration must be a non-negative integer",
    )
    started = step["started_at"]
    finished = step["finished_at"]
    if started is None and finished is None:
        return
    _require(
        started is not None and finished is not None,
        "run_timestamp_pair_invalid",
        "started_at and finished_at must both be present or both be null",
    )
    _require(_timestamp_ok(started) and _timestamp_ok(finished), "run_timestamp_invalid", "step timestamp is malformed")
    started_dt = _datetime.datetime.fromisoformat(started[:-1] + "+00:00")
    finished_dt = _datetime.datetime.fromisoformat(finished[:-1] + "+00:00")
    _require(finished_dt >= started_dt, "run_timestamp_order_invalid", "step finished before it started")


def _expected_step_asset(scenario: Mapping[str, Any], arm_id: str, phase: str) -> tuple[str | None, str, str, str | None]:
    assets = scenario["browser_assets"]
    if phase == "INITIAL":
        if arm_id == "X":
            return assets["arm_x_prompt_sha256"], assets["arm_x_initial_payload_manifest_digest"], assets["arm_x_initial_payload_digest"], None
        return assets["arm_y_prompt_sha256"], assets["arm_y_initial_payload_manifest_digest"], assets["arm_y_initial_payload_digest"], None
    _require(scenario["scenario_id"] == "S5", "followup_only_s5", scenario["scenario_id"])
    return None, assets["s5_followup_payload_manifest_digest"], assets["s5_followup_payload_digest"], assets["s5_followup_operator_message_sha256"]


def validate_arm_run(
    run: Mapping[str, Any],
    scenario: Mapping[str, Any],
    *,
    packet_root: str | Path,
    materialized_root: str | Path,
) -> dict[str, Any]:
    """Validate one immutable Browser attestation and recompute every digest."""

    required = {
        "schema", "scenario_id", "scenario_digest", "arm_id", "arm_type", "repo_view",
        "evidence_universe_digest", "task_text_digest", "environment", "environment_digest",
        "conversation_steps", "context_request_used", "requested_source_paths", "protocol_burden_visible", "run_digest",
    }
    _require(set(run) == required, "run_field_set", "run has unexpected or missing fields")
    _require(run["schema"] == RUN_SCHEMA, "run_schema_mismatch", "unsupported arm run schema")
    _require(run["scenario_id"] == scenario["scenario_id"], "run_scenario_mismatch", scenario["scenario_id"])
    _require(run["scenario_digest"] == scenario["scenario_digest"], "run_scenario_digest_mismatch", scenario["scenario_id"])
    _require(run["repo_view"] == scenario["repo_view"], "run_repo_view_mismatch", scenario["scenario_id"])
    _require(run["evidence_universe_digest"] == evidence_universe_digest(scenario), "run_evidence_universe_mismatch", scenario["scenario_id"])
    _require(run["task_text_digest"] == task_text_digest(scenario), "run_task_digest_mismatch", scenario["scenario_id"])
    _require(run["arm_id"] in {"X", "Y"}, "run_arm_invalid", scenario["scenario_id"])
    expected_arm_type = "BASELINE_FLAT_CONTEXT_V1" if run["arm_id"] == "X" else "M2_VNEXT_CONTEXT_PACKAGE_V1"
    _require(run["arm_type"] == expected_arm_type, "run_arm_type_mismatch", scenario["scenario_id"])
    environment = run["environment"]
    _require(set(environment) == {"product", "mode", "model_id", "reasoning_setting", "surface", "fresh_conversation", "same_visible_capability_class", "api_used"}, "run_environment_field_set", scenario["scenario_id"])
    _require(environment["product"] == "ChatGPT", "run_product_invalid", scenario["scenario_id"])
    _require(environment["surface"] == "normal_chatgpt_browser" and environment["api_used"] is False, "run_browser_surface_invalid", scenario["scenario_id"])
    _require(environment["fresh_conversation"] is True and environment["same_visible_capability_class"] is True, "run_browser_continuity_invalid", scenario["scenario_id"])
    _require(run["environment_digest"] == execution_environment_digest(environment), "run_environment_digest_mismatch", scenario["scenario_id"])
    steps = run["conversation_steps"]
    _require(isinstance(steps, list) and steps, "run_steps_missing", scenario["scenario_id"])
    _require(isinstance(run["context_request_used"], bool), "run_context_request_invalid", scenario["scenario_id"])
    requested_paths = run["requested_source_paths"]
    _require(isinstance(requested_paths, list), "run_requested_paths_invalid", scenario["scenario_id"])
    _require(all(isinstance(path, str) and bool(path) for path in requested_paths), "run_requested_paths_invalid", scenario["scenario_id"])
    if scenario["scenario_id"] != "S5":
        _require(len(steps) == 1 and steps[0]["phase"] == "INITIAL", "run_step_count_invalid", scenario["scenario_id"])
        _require(run["context_request_used"] is False, "run_unexpected_context_request", scenario["scenario_id"])
        _require(requested_paths == [], "run_requested_paths_mismatch", scenario["scenario_id"])
    else:
        _require(len(steps) in {1, 2} and steps[0]["phase"] == "INITIAL", "s5_step_count_invalid", "S5")
        if run["context_request_used"]:
            _require(requested_paths, "s5_requested_paths_missing", "S5 context request must preserve observed paths")
            if len(steps) == 2:
                _require(steps[1]["phase"] == "FOLLOWUP", "s5_followup_missing", "S5")
                frozen_followup_paths = set(scenario["context_seed"]["requested_source_paths"])
                _require(
                    all(path in frozen_followup_paths for path in requested_paths),
                    "s5_followup_request_outside_universe",
                    "operator follow-up is admitted only for an observed subset of the frozen follow-up paths",
                )
        else:
            _require(len(steps) == 1, "s5_unexpected_followup", "S5")
            _require(requested_paths == [], "run_requested_paths_mismatch", "S5")
    expected_paths = list(scenario["arm_construction"]["initial_visible_paths"])
    for index, step in enumerate(steps):
        _require(set(step) == {"phase", "processing_duration_seconds", "started_at", "finished_at", "prompt_digest", "payload_manifest_digest", "payload_digest", "operator_message_digest", "assistant_answer_markdown", "assistant_answer_sha256"}, "run_step_field_set", scenario["scenario_id"])
        _validate_step_timing(step)
        phase = step["phase"]
        prompt_expected, manifest_expected, payload_expected, operator_expected = _expected_step_asset(scenario, run["arm_id"], phase)
        _require(step["prompt_digest"] == prompt_expected, "run_prompt_digest_mismatch", scenario["scenario_id"])
        if phase == "INITIAL":
            prompt_name = "arm_x_prompt.md" if run["arm_id"] == "X" else "arm_y_prompt.md"
            prompt_path = Path(packet_root).resolve() / "browser_runs" / scenario["scenario_id"] / prompt_name
            _require(prompt_path.is_file(), "browser_artifact_missing", str(prompt_path))
            _require(_prompt_digest(prompt_path) == prompt_expected, "browser_asset_drift", str(prompt_path))
        _require(step["payload_manifest_digest"] == manifest_expected and step["payload_digest"] == payload_expected, "run_payload_digest_mismatch", scenario["scenario_id"])
        _require(step["operator_message_digest"] == operator_expected, "run_operator_message_mismatch", scenario["scenario_id"])
        answer = step["assistant_answer_markdown"]
        _require(step["assistant_answer_sha256"] == _sha256_bytes(answer.encode("utf-8")), "run_answer_digest_mismatch", scenario["scenario_id"])
        asset = scenario["browser_assets"]
        if phase == "INITIAL":
            asset_prefix = "arm_x" if run["arm_id"] == "X" else "arm_y"
            _validate_materialized_manifest(
                scenario,
                view=None,
                output_root=Path(materialized_root).resolve(),
                arm_id=run["arm_id"],
                phase=phase,
                expected_manifest_digest=asset[f"{asset_prefix}_initial_payload_manifest_digest"],
                expected_manifest_sha256=asset[f"{asset_prefix}_initial_payload_manifest_sha256"],
                expected_payload_digest=asset[f"{asset_prefix}_initial_payload_digest"],
                expected_paths=expected_paths,
                packet_root=Path(packet_root).resolve(),
            )
        else:
            _require(_sha256_bytes((Path(materialized_root).resolve() / "S5" / "followup_manifest.json").read_bytes()) == asset["s5_followup_payload_manifest_sha256"], "s5_followup_manifest_drift", "S5")
            _require(_sha256_bytes((Path(materialized_root).resolve() / "S5" / "followup_context.md").read_bytes()) == asset["s5_followup_payload_digest"], "s5_followup_payload_drift", "S5")
        _require(index == 0 or phase == "FOLLOWUP", "run_phase_order_invalid", scenario["scenario_id"])
    _require(run["run_digest"] == run_digest(run), "run_digest_mismatch", scenario["scenario_id"])
    return {
        "scenario_id": scenario["scenario_id"],
        "arm_id": run["arm_id"],
        "run_digest": run["run_digest"],
        "environment_digest": run["environment_digest"],
        "model_id": environment["model_id"],
        "reasoning_setting": environment["reasoning_setting"],
        "repo_view": run["repo_view"],
        "evidence_universe_digest": run["evidence_universe_digest"],
        "task_text_digest": run["task_text_digest"],
        "step_count": len(steps),
        "context_request_used": run["context_request_used"],
        "protocol_burden_visible": run["protocol_burden_visible"],
    }


def derive_fairness_from_runs(run_x: Mapping[str, Any], run_y: Mapping[str, Any]) -> dict[str, bool]:
    """Derive fairness solely from validated run evidence; no caller booleans."""

    return {
        "same_model": run_x["model_id"] == run_y["model_id"],
        "same_settings": run_x["reasoning_setting"] == run_y["reasoning_setting"],
        "same_repo_view": run_x["repo_view"] == run_y["repo_view"],
        "same_evidence_universe": run_x["evidence_universe_digest"] == run_y["evidence_universe_digest"],
        "same_task": run_x["task_text_digest"] == run_y["task_text_digest"],
        "browser_parity": run_x["environment_digest"] == run_y["environment_digest"],
    }


def _evaluation_identity_payload(evaluation: Mapping[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(dict(evaluation))
    payload.pop("evaluation_digest", None)
    return payload


def evaluation_digest(evaluation: Mapping[str, Any]) -> str:
    return semantic_digest(_evaluation_identity_payload(evaluation))


def validate_pair_evaluation(
    evaluation: Mapping[str, Any],
    scenario: Mapping[str, Any],
    *,
    validated_runs: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    required = {
        "schema", "scenario_id", "scenario_digest", "arm_x_run_digest", "arm_y_run_digest", "rubric_version",
        "vector_evaluations", "hard_failures", "context_request", "material_improvement", "evaluator_evidence", "evaluation_digest",
    }
    _require(set(evaluation) == required, "evaluation_field_set", "evaluation has unexpected or missing fields")
    _require(evaluation["schema"] == "bdb-vnext-m2d-evaluation-v1", "evaluation_schema_mismatch", "unsupported evaluation schema")
    _require(evaluation["scenario_id"] == scenario["scenario_id"] and evaluation["scenario_digest"] == scenario["scenario_digest"], "evaluation_scenario_mismatch", scenario["scenario_id"])
    _require(evaluation["rubric_version"] == RUBRIC_VERSION, "evaluation_rubric_mismatch", scenario["scenario_id"])
    vectors = evaluation["vector_evaluations"]
    expected_vector_ids = list(scenario["adjudication_vector_ids"])
    _require(isinstance(vectors, list) and len(vectors) == len(expected_vector_ids), "evaluation_vector_set_invalid", scenario["scenario_id"])
    seen_vector_ids: set[str] = set()
    for vector in vectors:
        _require(isinstance(vector, Mapping), "evaluation_vector_shape_invalid", scenario["scenario_id"])
        _require(set(vector) == {"vector_id", "judgment", "evidence", "raw_counts"}, "evaluation_vector_field_set", scenario["scenario_id"])
        vector_id = vector["vector_id"]
        _require(isinstance(vector_id, str) and bool(vector_id), "evaluation_vector_id_invalid", scenario["scenario_id"])
        _require(vector_id in expected_vector_ids, "evaluation_vector_unknown", str(vector_id))
        _require(vector_id not in seen_vector_ids, "evaluation_vector_duplicate", str(vector_id))
        seen_vector_ids.add(vector_id)
        _require(isinstance(vector["judgment"], str), "evaluation_judgment_invalid", str(vector_id))
        _require(vector["judgment"] in _ALLOWED_JUDGMENTS, "evaluation_judgment_invalid", str(vector_id))
        _require(isinstance(vector["evidence"], str) and bool(vector["evidence"].strip()), "evaluation_vector_evidence_missing", str(vector_id))
        _require(isinstance(vector["raw_counts"], Mapping), "evaluation_vector_raw_counts_invalid", str(vector_id))
        if vector["judgment"] == "N/A":
            _require("not applicable" in vector["evidence"].casefold() or "does not apply" in vector["evidence"].casefold(), "evaluation_na_reason_missing", str(vector_id))
    _require(seen_vector_ids == set(expected_vector_ids), "evaluation_vector_set_invalid", scenario["scenario_id"])
    hard_failures = evaluation["hard_failures"]
    _require(isinstance(hard_failures, list) and all(isinstance(item, str) and bool(item.strip()) for item in hard_failures), "evaluation_hard_failure_shape_invalid", scenario["scenario_id"])
    context = evaluation["context_request"]
    _require(isinstance(context, Mapping) and set(context) == {"required", "outcome", "gap_visible", "requested_exact_evidence", "unrelated_gaps_preserved", "answer_improved_or_narrowed", "protocol_bookkeeping_required"}, "evaluation_context_request_shape_invalid", scenario["scenario_id"])
    _require(context["outcome"] in _CONTEXT_REQUEST_OUTCOMES, "evaluation_context_outcome_invalid", scenario["scenario_id"])
    _require(all(isinstance(context[key], bool) for key in ("required", "gap_visible", "requested_exact_evidence", "unrelated_gaps_preserved", "answer_improved_or_narrowed", "protocol_bookkeeping_required")), "evaluation_context_bool_invalid", scenario["scenario_id"])
    _require(isinstance(evaluation["material_improvement"], bool), "evaluation_material_improvement_invalid", scenario["scenario_id"])
    _require(isinstance(evaluation["evaluator_evidence"], str) and bool(evaluation["evaluator_evidence"].strip()), "evaluation_evidence_missing", scenario["scenario_id"])
    if evaluation["material_improvement"]:
        _require(any(vector["vector_id"] in _CORE_IMPROVEMENT_VECTOR_IDS and vector["judgment"] == "BETTER" for vector in vectors), "evaluation_material_improvement_unsupported", scenario["scenario_id"])
    x = validated_runs.get("X")
    y = validated_runs.get("Y")
    _require(x is not None and y is not None, "evaluation_runs_missing", scenario["scenario_id"])
    _require(evaluation["arm_x_run_digest"] == x["run_digest"] and evaluation["arm_y_run_digest"] == y["run_digest"], "evaluation_run_link_mismatch", scenario["scenario_id"])
    _require(evaluation["evaluation_digest"] == evaluation_digest(evaluation), "evaluation_digest_mismatch", scenario["scenario_id"])
    fairness = derive_fairness_from_runs(x, y)
    derived_protocol_burden = x["protocol_burden_visible"] or y["protocol_burden_visible"]
    if scenario["scenario_id"] == "S5":
        _require(context["protocol_bookkeeping_required"] is derived_protocol_burden, "evaluation_protocol_burden_mismatch", "S5")
    protocol_vector = next((vector for vector in vectors if vector["vector_id"] == "protocol_burden_visible_to_gpt"), None)
    if protocol_vector is not None and derived_protocol_burden:
        _require(protocol_vector["judgment"] != "N/A", "evaluation_protocol_vector_inconsistent", scenario["scenario_id"])
    if scenario["scenario_id"] == "S5" and evaluation["context_request"].get("outcome") == "RESOLVED":
        _require(x["step_count"] == 2 and y["step_count"] == 2, "s5_incomplete_transcript", "S5")
    derived_hard_failures = ["protocol_bookkeeping_required"] if derived_protocol_burden else []
    return {
        "scenario_id": scenario["scenario_id"],
        "fairness": fairness,
        "evaluation": evaluation,
        "derived_hard_failures": derived_hard_failures,
    }


def materialize_packet(
    repo_root: str | Path,
    output_root: str | Path,
    *,
    packet_root: str | Path | None = None,
    verify_assets: bool = True,
) -> dict[str, Any]:
    """Materialize deterministic disposable X/Y payloads from exact Git objects."""

    output = Path(output_root).resolve()
    output.mkdir(parents=True, exist_ok=True)
    packet = Path(packet_root).resolve() if packet_root is not None else Path(repo_root).resolve() / "benchmarks" / "m2d"
    view = _subject_view(repo_root)
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix=".bdb-m2d-materialize-", dir=str(output.parent.resolve())) as temp_root:
        for scenario_id in REQUIRED_SCENARIOS:
            scenario_path = next(packet.joinpath("scenarios").glob(f"{scenario_id}-*.json"))
            scenario = _load_json(scenario_path)
            _validate_scenario_shape(scenario, allow_asset_placeholder=not verify_assets)
            initial_paths = list(scenario["arm_construction"]["initial_visible_paths"])
            materialized: dict[str, Any] = {}
            materialized["X_INITIAL"] = _materialize_one(view, scenario, output=output, arm_id="X", paths=initial_paths, phase="INITIAL", runtime_root=None)
            materialized["Y_INITIAL"] = _materialize_one(view, scenario, output=output, arm_id="Y", paths=initial_paths, phase="INITIAL", runtime_root=Path(temp_root) / scenario_id / "y-initial")
            if scenario_id == "S5":
                materialized["FOLLOWUP"] = _materialize_followup(view, scenario, output=output)
            expected = _expected_asset_fields(scenario, packet_root=packet, materialized=materialized)
            if verify_assets:
                _require(scenario["browser_assets"] == expected, "browser_asset_drift", scenario_id, expected=expected, actual=scenario["browser_assets"])
            results.append({"scenario_id": scenario_id, "browser_assets": expected, "materialized": materialized})
    execution_manifest = {
        "schema": "bdb-vnext-m2d-execution-manifest-v1",
        "asset_contract_version": ASSET_CONTRACT_VERSION,
        "frozen_commit": FROZEN_COMMIT,
        "repo_view": frozen_repo_view_dict(),
        "scenarios": results,
    }
    (output / "execution_manifest.json").write_bytes(canonical_json_bytes(execution_manifest))
    return execution_manifest


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
    expected_view_id = scenario["repo_view"]["view_id"]
    for text in (x_text, y_text):
        _require(scenario["repo_view"]["commit_oid"] in text, "prompt_basis_missing", scenario["scenario_id"])
        _require(expected_view_id in _EXACT_SHA256_TOKEN_RE.findall(text), "prompt_basis_mismatch", scenario["scenario_id"])
        for item in scenario["arm_construction"]["initial_visible_paths"]:
            _require(item in text, "prompt_source_missing", item)
        _require(not any(term in text for term in forbidden), "prompt_leaks_arm_or_answer_key", scenario["scenario_id"])
        _require(not any(term in text.casefold() for term in ("fragment_id", "envelope", "protocol generation", "retry token")), "prompt_leaks_protocol", scenario["scenario_id"])
    _require(x_text != y_text, "prompt_arms_identical", scenario["scenario_id"])
    _require(_prompt_digest(x) == scenario["browser_assets"]["arm_x_prompt_sha256"], "browser_asset_drift", f"{scenario['scenario_id']}:arm_x_prompt")
    _require(_prompt_digest(y) == scenario["browser_assets"]["arm_y_prompt_sha256"], "browser_asset_drift", f"{scenario['scenario_id']}:arm_y_prompt")
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


def _quality_policy_from_validated(evaluations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Apply frozen categorical policy after real run/evaluation validation."""

    by_id = {item["scenario_id"]: item for item in evaluations}
    for scenario_id, item in by_id.items():
        fairness = item["fairness"]
        if not all(fairness.values()):
            return {"outcome": "INCONCLUSIVE", "reason": f"derived fairness failure in {scenario_id}", "no_aggregate_score": True}
        evaluation = item["evaluation"]
        effective_hard_failures = list(dict.fromkeys([
            *evaluation["hard_failures"],
            *item.get("derived_hard_failures", []),
        ]))
        if effective_hard_failures:
            return {
                "outcome": "FAIL",
                "reason": f"hard failure in {scenario_id}",
                "effective_hard_failures": effective_hard_failures,
                "no_aggregate_score": True,
            }
    for scenario_id in ("S1", "S2"):
        judgments = [item["judgment"] for item in by_id[scenario_id]["evaluation"]["vector_evaluations"]]
        if any(value == "WORSE" for value in judgments):
            return {"outcome": "FAIL", "reason": f"small-task regression in {scenario_id}", "no_aggregate_score": True}
        if any(value == "INCONCLUSIVE" for value in judgments) or not judgments or any(value not in {"BETTER", "EQUIVALENT", "N/A"} for value in judgments):
            return {"outcome": "INCONCLUSIVE", "reason": f"small-task ambiguity in {scenario_id}", "no_aggregate_score": True}
    s5 = by_id["S5"]["evaluation"]["context_request"]
    required_context = all(s5.get(key) is True for key in ("gap_visible", "requested_exact_evidence", "unrelated_gaps_preserved", "answer_improved_or_narrowed")) and s5.get("outcome") == "RESOLVED" and s5.get("protocol_bookkeeping_required") is False
    complex_improvement = any(by_id[item]["evaluation"]["material_improvement"] is True for item in ("S3", "S4", "S5"))
    if not required_context:
        return {"outcome": "FAIL", "reason": "S5 ContextRequest rule not satisfied", "no_aggregate_score": True}
    if not complex_improvement:
        return {"outcome": "FAIL", "reason": "no complex scenario material improvement", "no_aggregate_score": True}
    return {"outcome": "PASS", "no_aggregate_score": True}


def derive_gate_decision(
    evaluations: Sequence[Mapping[str, Any]],
    *,
    run_records: Sequence[Mapping[str, Any]] | None = None,
    scenarios: Mapping[str, Mapping[str, Any]] | None = None,
    packet_root: str | Path | None = None,
    materialized_root: str | Path | None = None,
) -> dict[str, Any]:
    """Derive M2d outcome only after validating all ten Browser run records.

    There is intentionally no boolean Browser-presence or assume/fixture bypass
    argument.  Empty evidence is READY; partial or invalid evidence is never
    PASS.
    """

    if not run_records:
        return {"outcome": "READY_FOR_BROWSER_EXECUTION", "missing_runs": 10, "no_aggregate_score": True}
    if len(run_records) < 10:
        return {"outcome": "INCONCLUSIVE", "reason": "INCOMPLETE_BROWSER_EVIDENCE", "run_count": len(run_records), "no_aggregate_score": True}
    if scenarios is None or packet_root is None or materialized_root is None:
        return {"outcome": "INCONCLUSIVE", "reason": "RUN_VALIDATION_CONTEXT_MISSING", "no_aggregate_score": True}
    validated: dict[tuple[str, str], dict[str, Any]] = {}
    try:
        for run in run_records:
            scenario = scenarios[run["scenario_id"]]
            result = validate_arm_run(run, scenario, packet_root=packet_root, materialized_root=materialized_root)
            key = (result["scenario_id"], result["arm_id"])
            _require(key not in validated, "duplicate_run_record", str(key))
            validated[key] = result
        _require(len(validated) == 10, "run_record_count_invalid", "exactly ten unique Browser runs are required")
        _require(len({item["environment_digest"] for item in validated.values()}) == 1, "environment_drift", "all ten runs must use one exact execution environment record")
        validated_evaluations = []
        for evaluation in evaluations:
            scenario = scenarios[evaluation["scenario_id"]]
            x = validated[(scenario["scenario_id"], "X")]
            y = validated[(scenario["scenario_id"], "Y")]
            validated_evaluations.append(validate_pair_evaluation(evaluation, scenario, validated_runs={"X": x, "Y": y}))
        _require(len(validated_evaluations) == 5 and {item["scenario_id"] for item in validated_evaluations} == set(REQUIRED_SCENARIOS), "evaluation_count_invalid", "exactly five paired evaluations are required")
    except (KeyError, M2dValidationError) as exc:
        return {"outcome": "INCONCLUSIVE", "reason": "INVALID_BROWSER_EVIDENCE", "details": str(exc), "no_aggregate_score": True}
    return _quality_policy_from_validated(validated_evaluations)


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("validate", help="validate the frozen packet and disposable grounding")
    check.add_argument("--repo-root", default=".")
    check.add_argument("--packet-root", default=None)
    materialize = sub.add_parser("materialize", help="materialize deterministic disposable Browser payloads")
    materialize.add_argument("--repo-root", default=".")
    materialize.add_argument("--packet-root", default=None)
    materialize.add_argument("--output", required=True)
    gate = sub.add_parser("gate", help="derive an outcome from validated Browser runs and evaluations")
    gate.add_argument("--repo-root", default=".")
    gate.add_argument("--packet-root", default=None)
    gate.add_argument("--materialized-root", required=True)
    gate.add_argument("--runs-dir", required=True)
    gate.add_argument("--evaluations-dir", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            result = validate_packet(args.repo_root, packet_root=args.packet_root)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        if args.command == "materialize":
            result = materialize_packet(args.repo_root, args.output, packet_root=args.packet_root, verify_assets=True)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        if args.command == "gate":
            packet = Path(args.packet_root).resolve() if args.packet_root is not None else Path(args.repo_root).resolve() / "benchmarks" / "m2d"
            scenarios = {
                scenario["scenario_id"]: scenario
                for scenario_path in sorted((packet / "scenarios").glob("S[1-5]-*.json"))
                for scenario in [_load_json(scenario_path)]
            }
            runs = [_load_json(path) for path in sorted(Path(args.runs_dir).glob("*.json"))]
            evaluations = [_load_json(path) for path in sorted(Path(args.evaluations_dir).glob("*.json"))]
            result = derive_gate_decision(
                evaluations,
                run_records=runs,
                scenarios=scenarios,
                packet_root=packet,
                materialized_root=args.materialized_root,
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
    except M2dValidationError as exc:
        print(json.dumps({"status": "INVALID", "code": exc.code, "message": str(exc), "details": exc.details}, indent=2), file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())

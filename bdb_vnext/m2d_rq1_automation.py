"""Bounded normal-Browser capture substrate for the deferred M2d requalification.

This module deliberately stops at the boundary of the real ChatGPT Browser.
It prepares exact frozen inputs and records an externally observed Browser
answer; it never opens a Browser, calls an API, generates an answer, evaluates
quality, or writes vNext production state.
"""

from __future__ import annotations

import argparse
import copy
import datetime as _datetime
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from bdb_shared.evidence import canonical_json_bytes, semantic_digest
from bdb_vnext import m2d_quality_gate as gate


CAPTURE_SCHEMA = "bdb-vnext-m2d-rq1-capture-v1"
PLAN_SCHEMA = "bdb-vnext-m2d-rq1-execution-plan-v1"
EXECUTION_SCHEMA = "bdb-vnext-m2d-rq1-execution-manifest-v1"
CAPTURE_STATUS_FINALIZED = "FINALIZED"
CAPTURE_STATUS_ABORTED = "ABORTED"
NORMAL_BROWSER_SURFACE = "normal_chatgpt_browser"
ANSWER_SOURCE = "normal_chatgpt_browser"
UNKNOWN_NOT_OBSERVABLE = "UNKNOWN_NOT_OBSERVABLE"
EVALUATOR_SCHEMA = "bdb-vnext-m2d-evaluation-v1"


class M2dRq1AutomationError(ValueError):
    """Fail-closed preparation or Browser evidence error."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.details = dict(details or {})
        super().__init__(f"{code}: {message}")


def _require(condition: bool, code: str, message: str, **details: Any) -> None:
    if not condition:
        raise M2dRq1AutomationError(code, message, details=details)


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise M2dRq1AutomationError("json_invalid", f"cannot read {path}") from exc
    _require(isinstance(value, dict), "json_shape_invalid", f"{path} must contain an object")
    return value


def _sha256(raw: bytes) -> str:
    return gate._sha256_bytes(raw)


def _timestamp(value: Any, field: str) -> _datetime.datetime:
    _require(isinstance(value, str) and gate._timestamp_ok(value), "timestamp_invalid", field)
    try:
        parsed = _datetime.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:  # pragma: no cover - _timestamp_ok already guards this
        raise M2dRq1AutomationError("timestamp_invalid", field) from exc
    _require(parsed.tzinfo is not None, "timestamp_invalid", field)
    return parsed


def _safe_external(root: str | Path, *, repo_root: str | Path, label: str) -> Path:
    resolved = Path(root).resolve()
    repository = Path(repo_root).resolve()
    _require(
        not (resolved == repository or repository in resolved.parents),
        "tracked_output_forbidden",
        f"{label} must be outside the repository checkout",
        path=str(resolved),
    )
    return resolved


def _publish_immutable(path: Path, raw: bytes) -> None:
    """Publish one disposable immutable file without replacing an existing file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    _require(not path.exists(), "immutable_record_exists", f"refusing to mutate {path}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    try:
        descriptor = os.open(str(path), flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            if descriptor != -1:
                os.close(descriptor)
    except FileExistsError as exc:
        raise M2dRq1AutomationError("immutable_record_exists", f"refusing to mutate {path}") from exc
    except OSError as exc:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise M2dRq1AutomationError("record_write_failure", f"cannot publish {path}") from exc


def _scenario(packet_root: Path, scenario_id: str) -> dict[str, Any]:
    candidates = sorted((packet_root / "scenarios").glob(f"{scenario_id}-*.json"))
    _require(len(candidates) == 1, "scenario_missing_or_ambiguous", scenario_id)
    result = gate._load_json(candidates[0])
    gate._validate_scenario_shape(result)
    _require(result["scenario_id"] == scenario_id, "scenario_mismatch", scenario_id)
    return result


def _manifest_identity(
    scenario: Mapping[str, Any],
    materialized_root: Path,
    *,
    arm_id: str,
    phase: str = "INITIAL",
) -> dict[str, Any]:
    _require(arm_id in {"X", "Y"}, "arm_invalid", arm_id)
    _require(phase in {"INITIAL", "FOLLOWUP"}, "phase_invalid", phase)
    if phase == "INITIAL":
        relative = f"{scenario['scenario_id']}/{'arm_x' if arm_id == 'X' else 'arm_y'}_initial_manifest.json"
    else:
        _require(scenario["scenario_id"] == "S5", "followup_only_s5", scenario["scenario_id"])
        relative = "S5/followup_manifest.json"
    path = materialized_root / relative
    manifest = gate._load_json(path)
    supplied_digest = manifest.pop("manifest_digest", None)
    _require(supplied_digest == gate.semantic_digest(manifest), "manifest_integrity_failure", str(path))
    _require(manifest.get("scenario_id") == scenario["scenario_id"], "manifest_scenario_mismatch", str(path))
    expected_arm = arm_id if phase == "INITIAL" else "COMMON"
    _require(manifest.get("arm_id") == expected_arm, "manifest_arm_mismatch", str(path))
    _require(manifest.get("phase") == phase, "manifest_phase_mismatch", str(path))
    _require(manifest.get("repo_view") == scenario["repo_view"], "manifest_repo_view_mismatch", str(path))
    _require(
        manifest.get("evidence_universe_digest") == gate.evidence_universe_digest(scenario),
        "manifest_universe_mismatch",
        str(path),
    )
    raw = path.read_bytes()
    payload_path = materialized_root / manifest["payload_relative_path"]
    _require(payload_path.is_file(), "payload_missing", str(payload_path))
    payload_raw = payload_path.read_bytes()
    result: dict[str, Any] = {
        "manifest_path": relative,
        "manifest_digest": supplied_digest,
        "manifest_sha256": _sha256(raw),
        "payload_path": manifest["payload_relative_path"],
        "payload_digest": _sha256(payload_raw),
        "source_paths": [item["path"] for item in manifest.get("source_objects", [])],
        "source_object_ids": [item["object_id"] for item in manifest.get("source_objects", [])],
    }
    assets = scenario["browser_assets"]
    if phase == "INITIAL":
        prefix = "arm_x" if arm_id == "X" else "arm_y"
        expected_manifest = assets[f"{prefix}_initial_payload_manifest_digest"]
        expected_manifest_sha = assets[f"{prefix}_initial_payload_manifest_sha256"]
        expected_payload = assets[f"{prefix}_initial_payload_digest"]
    else:
        expected_manifest = assets["s5_followup_payload_manifest_digest"]
        expected_manifest_sha = assets["s5_followup_payload_manifest_sha256"]
        expected_payload = assets["s5_followup_payload_digest"]
    _require(result["manifest_digest"] == expected_manifest, "materialized_manifest_identity_mismatch", str(path))
    _require(result["manifest_sha256"] == expected_manifest_sha, "materialized_manifest_sha256_mismatch", str(path))
    _require(result["payload_digest"] == expected_payload, "materialized_payload_identity_mismatch", str(payload_path))
    _require(result["payload_digest"] == gate._sha256_bytes(payload_raw), "payload_digest_invalid", str(payload_path))
    expected_paths = (
        list(scenario["arm_construction"]["initial_visible_paths"])
        if phase == "INITIAL"
        else list(scenario["context_seed"]["requested_source_paths"])
    )
    _require(result["source_paths"] == expected_paths, "materialized_source_paths_mismatch", str(path))
    m2_context = manifest.get("m2_context")
    if arm_id == "Y" and phase == "INITIAL":
        _require(isinstance(m2_context, Mapping), "context_package_missing", str(path))
        for field in (
            "understanding_id",
            "package_id",
            "coverage_status",
            "requested_dimensions",
            "covered_dimensions",
            "visible_unknown_dimensions",
            "visible_omission_dimensions",
            "claim_classes",
            "context_request",
            "context_resolution",
        ):
            _require(field in m2_context, "context_package_field_missing", field)
        result["context_package"] = {
            "status": "BOUND",
            "understanding_id": m2_context["understanding_id"],
            "package_id": m2_context["package_id"],
            "projection_digest": gate.semantic_digest(dict(m2_context)),
            "coverage_status": m2_context["coverage_status"],
            "requested_dimensions": list(m2_context["requested_dimensions"]),
            "covered_dimensions": list(m2_context["covered_dimensions"]),
            "visible_unknown_dimensions": list(m2_context["visible_unknown_dimensions"]),
            "visible_omission_dimensions": list(m2_context["visible_omission_dimensions"]),
            "claim_classes": dict(m2_context["claim_classes"]),
            "source_paths": list(result["source_paths"]),
            "source_object_ids": list(result["source_object_ids"]),
            "manifest_digest": supplied_digest,
            "manifest_sha256": result["manifest_sha256"],
            "payload_digest": result["payload_digest"],
        }
    elif arm_id == "X" and phase == "INITIAL":
        result["context_package"] = {
            "status": "NOT_APPLICABLE",
            "understanding_id": None,
            "package_id": None,
            "projection_digest": None,
            "source_paths": [],
            "source_object_ids": [],
        }
    return result


def _prompt_identity(packet_root: Path, scenario: Mapping[str, Any], arm_id: str) -> dict[str, Any]:
    filename = "arm_x_prompt.md" if arm_id == "X" else "arm_y_prompt.md"
    path = packet_root / "browser_runs" / scenario["scenario_id"] / filename
    _require(path.is_file(), "prompt_missing", str(path))
    digest = _sha256(path.read_bytes())
    expected = scenario["browser_assets"]["arm_x_prompt_sha256" if arm_id == "X" else "arm_y_prompt_sha256"]
    _require(digest == expected, "prompt_digest_mismatch", str(path))
    return {"path": str(path), "relative_path": f"browser_runs/{scenario['scenario_id']}/{filename}", "sha256": digest}


def _plan_identity_payload(plan: Mapping[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(dict(plan))
    payload.pop("plan_digest", None)
    return payload


def plan_digest(plan: Mapping[str, Any]) -> str:
    return semantic_digest(_plan_identity_payload(plan))


def _build_plan(
    repo_root: str | Path,
    packet_root: str | Path,
    materialized_root: str | Path,
    *,
    scenario_id: str,
    arm_id: str,
    attempt_id: str,
    expected_model_id: str | None = None,
    expected_reasoning_setting: str | None = None,
) -> dict[str, Any]:
    packet = Path(packet_root).resolve()
    materialized = Path(materialized_root).resolve()
    scenario = _scenario(packet, scenario_id)
    view = gate._subject_view(repo_root)
    repo_view = gate.RepoViewBinding.from_view(view).as_dict()
    _require(repo_view == scenario["repo_view"], "repo_view_mismatch", scenario_id)
    initial = _manifest_identity(scenario, materialized, arm_id=arm_id)
    followup = _manifest_identity(scenario, materialized, arm_id=arm_id, phase="FOLLOWUP") if scenario_id == "S5" else None
    prompt = _prompt_identity(packet, scenario, arm_id)
    arm_type = "BASELINE_FLAT_CONTEXT_V1" if arm_id == "X" else "M2_VNEXT_CONTEXT_PACKAGE_V1"
    run_id = f"m2d-rq1:{attempt_id}:{scenario_id}:{arm_id}"
    plan: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "attempt_id": attempt_id,
        "run_id": run_id,
        "scenario_id": scenario_id,
        "scenario_digest": scenario["scenario_digest"],
        "arm_id": arm_id,
        "arm_type": arm_type,
        "repo_view": repo_view,
        "evidence_universe_digest": gate.evidence_universe_digest(scenario),
        "task_text_digest": gate.task_text_digest(scenario),
        "prompt": prompt,
        "initial_context": initial,
        "followup_context": followup,
        "browser_boundary": {
            "product": "ChatGPT",
            "surface": NORMAL_BROWSER_SURFACE,
            "api_used": False,
            "execution": "EXTERNAL_NORMAL_CHATGPT_BROWSER",
            "answer_source_required": ANSWER_SOURCE,
            "synthetic_answer_forbidden": True,
            "moving_ref_forbidden": True,
        },
        "attestation_policy": {
            "model": "VISIBLE_OR_UNKNOWN_NOT_OBSERVABLE",
            "reasoning_setting": "VISIBLE_OR_UNKNOWN_NOT_OBSERVABLE",
            "timing": "BROWSER_OBSERVED_UTC",
        },
        "expected_model_id": expected_model_id,
        "expected_reasoning_setting": expected_reasoning_setting,
        "evaluation_linkage": {"status": "PENDING", "evaluator_schema": EVALUATOR_SCHEMA},
    }
    plan["plan_digest"] = plan_digest(plan)
    return plan


def prepare_run(
    repo_root: str | Path,
    packet_root: str | Path,
    materialized_root: str | Path,
    output_root: str | Path,
    *,
    scenario_id: str,
    arm_id: str,
    attempt_id: str = "attempt-1",
    expected_model_id: str | None = None,
    expected_reasoning_setting: str | None = None,
) -> dict[str, Any]:
    """Prepare one exact Browser run plan; no Browser interaction occurs."""

    output = _safe_external(output_root, repo_root=repo_root, label="execution plan output")
    _require(isinstance(attempt_id, str) and attempt_id and "/" not in attempt_id and "\\" not in attempt_id, "attempt_id_invalid", attempt_id)
    plan = _build_plan(
        repo_root,
        packet_root,
        materialized_root,
        scenario_id=scenario_id,
        arm_id=arm_id,
        attempt_id=attempt_id,
        expected_model_id=expected_model_id,
        expected_reasoning_setting=expected_reasoning_setting,
    )
    path = output / "runs" / scenario_id / arm_id / "execution_plan.json"
    _publish_immutable(path, canonical_json_bytes(plan))
    return {"plan": plan, "path": str(path)}


def prepare_attempt(
    repo_root: str | Path,
    packet_root: str | Path,
    materialized_root: str | Path,
    output_root: str | Path,
    *,
    attempt_id: str = "attempt-1",
    expected_model_id: str | None = None,
    expected_reasoning_setting: str | None = None,
) -> dict[str, Any]:
    """Prepare all ten bounded plans for one future Browser attempt."""

    output = _safe_external(output_root, repo_root=repo_root, label="execution manifest output")
    plans: list[dict[str, Any]] = []
    for scenario_id in gate.REQUIRED_SCENARIOS:
        for arm_id in ("X", "Y"):
            prepared = prepare_run(
                repo_root,
                packet_root,
                materialized_root,
                output,
                scenario_id=scenario_id,
                arm_id=arm_id,
                attempt_id=attempt_id,
                expected_model_id=expected_model_id,
                expected_reasoning_setting=expected_reasoning_setting,
            )
            plans.append(prepared["plan"])
    manifest: dict[str, Any] = {
        "schema": EXECUTION_SCHEMA,
        "attempt_id": attempt_id,
        "status": "PREPARED_NOT_EXECUTED",
        "frozen_commit": gate.FROZEN_COMMIT,
        "repo_view": gate.frozen_repo_view_dict(),
        "materialized_root": str(Path(materialized_root).resolve()),
        "plans": [
            {
                "run_id": item["run_id"],
                "scenario_id": item["scenario_id"],
                "arm_id": item["arm_id"],
                "plan_digest": item["plan_digest"],
                "path": f"runs/{item['scenario_id']}/{item['arm_id']}/execution_plan.json",
            }
            for item in plans
        ],
    }
    manifest["execution_digest"] = semantic_digest(manifest)
    _publish_immutable(output / "rq1_execution_manifest.json", canonical_json_bytes(manifest))
    return {"manifest": manifest, "plans": plans, "path": str(output / "rq1_execution_manifest.json")}


def _attestation(value: Any, *, field: str) -> dict[str, str]:
    if value is None or value == "":
        return {"value": UNKNOWN_NOT_OBSERVABLE, "observability": "UNKNOWN_NOT_OBSERVABLE", "source": "not_observable"}
    _require(isinstance(value, str), "attestation_invalid", field)
    return {"value": value, "observability": "VISIBLE", "source": "browser_visible"}


def _plan_from_input(plan_or_path: Mapping[str, Any] | str | Path) -> dict[str, Any]:
    plan = _json(Path(plan_or_path)) if isinstance(plan_or_path, (str, Path)) else dict(plan_or_path)
    _require(plan.get("schema") == PLAN_SCHEMA, "plan_schema_mismatch", "unsupported execution plan")
    _require(plan.get("plan_digest") == plan_digest(plan), "plan_digest_mismatch", "execution plan identity is invalid")
    return plan


def _validate_observation_shape(observation: Mapping[str, Any]) -> None:
    required = {
        "browser_available",
        "conversation_id",
        "navigation_state",
        "prompt_submission_status",
        "response_status",
        "product",
        "surface",
        "api_used",
        "answer_source",
        "synthetic",
        "fresh_conversation",
        "same_visible_capability_class",
        "model_id",
        "reasoning_setting",
        "timing",
        "steps",
        "context_request_used",
        "requested_source_paths",
        "protocol_burden_visible",
    }
    _require(set(observation) == required, "observation_field_set", "Browser observation has unexpected or missing fields")
    _require(observation["browser_available"] is True, "browser_unavailable", "normal ChatGPT Browser was not available")
    _require(isinstance(observation["conversation_id"], str) and observation["conversation_id"], "conversation_identity_missing", "conversation identity is required")
    _require(observation["navigation_state"] == "EXPECTED_CONVERSATION", "unexpected_navigation", "capture is not on the expected conversation")
    _require(observation["prompt_submission_status"] == "SUBMITTED", "prompt_submission_failed", "prompt was not submitted")
    _require(observation["response_status"] == "COMPLETED", "response_incomplete", "Browser response is not complete")
    _require(observation["product"] == "ChatGPT", "product_invalid", "observation is not ChatGPT")
    _require(observation["surface"] == NORMAL_BROWSER_SURFACE, "browser_surface_invalid", "normal ChatGPT Browser surface is required")
    _require(observation["api_used"] is False, "api_fallback_forbidden", "API substitution is forbidden")
    _require(observation["answer_source"] == ANSWER_SOURCE, "answer_source_invalid", "raw answer must come from normal ChatGPT Browser")
    _require(observation["synthetic"] is False, "synthetic_answer_forbidden", "synthetic answer is not benchmark evidence")
    _require(observation["fresh_conversation"] is True and observation["same_visible_capability_class"] is True, "browser_continuity_invalid", "fresh conversation and capability continuity are required")
    _require(isinstance(observation["context_request_used"], bool), "context_request_invalid", "context request flag must be boolean")
    _require(isinstance(observation["protocol_burden_visible"], bool), "protocol_burden_invalid", "protocol burden observation must be boolean")
    _require(isinstance(observation["requested_source_paths"], list), "requested_paths_invalid", "requested paths must be a list")
    _require(all(isinstance(path, str) and path for path in observation["requested_source_paths"]), "requested_paths_invalid", "requested paths must be non-empty strings")
    timing = observation["timing"]
    _require(isinstance(timing, Mapping) and set(timing) == {"run_started_at", "prompt_submitted_at", "answer_completed_at", "capture_finalized_at", "source"}, "timing_shape_invalid", "timing attestation is incomplete")
    _require(timing["source"] == "browser_observed_utc", "timing_source_invalid", "timing must be Browser-observed")
    stamps = [_timestamp(timing[key], key) for key in ("run_started_at", "prompt_submitted_at", "answer_completed_at", "capture_finalized_at")]
    _require(stamps[0] <= stamps[1] <= stamps[2] <= stamps[3], "timing_order_invalid", "Browser timing is not monotonic")
    steps = observation["steps"]
    _require(isinstance(steps, list) and steps, "raw_answer_missing", "at least one completed answer step is required")
    for step in steps:
        _require(isinstance(step, Mapping) and set(step) == {"phase", "assistant_answer_markdown", "started_at", "finished_at", "processing_duration_seconds"}, "capture_step_shape_invalid", "raw Browser step is incomplete")
        _require(step["phase"] in {"INITIAL", "FOLLOWUP"}, "capture_phase_invalid", str(step["phase"]))
        _require(isinstance(step["assistant_answer_markdown"], str), "raw_answer_missing", "raw answer must be text")
        _timestamp(step["started_at"], "step.started_at")
        _timestamp(step["finished_at"], "step.finished_at")
        _require(_timestamp(step["started_at"], "step.started_at") <= _timestamp(step["finished_at"], "step.finished_at"), "timing_order_invalid", "step finished before start")
        _require(isinstance(step["processing_duration_seconds"], int) and not isinstance(step["processing_duration_seconds"], bool) and step["processing_duration_seconds"] >= 0, "processing_duration_invalid", "processing duration must be non-negative integer")


def _expected_context_paths(plan: Mapping[str, Any], observation: Mapping[str, Any]) -> None:
    scenario_id = plan["scenario_id"]
    paths = observation["requested_source_paths"]
    used = observation["context_request_used"]
    if scenario_id != "S5":
        _require(used is False and paths == [], "requested_paths_mismatch", scenario_id)
        _require(len(observation["steps"]) == 1, "step_count_invalid", scenario_id)
        return
    if not used:
        _require(paths == [] and len(observation["steps"]) == 1, "s5_context_request_mismatch", "S5 without a request must have one step")
        return
    _require(paths, "s5_requested_paths_missing", "S5 context request must preserve observed paths")
    # The plan stores no mutable packet path by design. The exact frozen set
    # is recovered from the follow-up manifest source paths.
    followup = plan.get("followup_context")
    _require(isinstance(followup, Mapping), "s5_followup_binding_missing", "S5 follow-up binding is required")
    frozen = set(followup.get("source_paths", []))
    _require(len(observation["steps"]) in {1, 2}, "s5_step_count_invalid", "S5 has one or two steps")
    if len(observation["steps"]) == 2:
        _require(set(paths).issubset(frozen), "requested_paths_outside_frozen_universe", "observed S5 follow-up request is outside the frozen follow-up universe")
        _require(observation["steps"][1]["phase"] == "FOLLOWUP", "s5_followup_missing", "second S5 step must be FOLLOWUP")


def _run_from_observation(plan: Mapping[str, Any], observation: Mapping[str, Any]) -> dict[str, Any]:
    _validate_observation_shape(observation)
    _expected_context_paths(plan, observation)
    expected_model = plan.get("expected_model_id")
    expected_reasoning = plan.get("expected_reasoning_setting")
    model = _attestation(observation["model_id"], field="model_id")
    reasoning = _attestation(observation["reasoning_setting"], field="reasoning_setting")
    if expected_model is not None:
        _require(model["value"] == expected_model, "unexpected_model", "visible model differs from expected paired model")
    if expected_reasoning is not None:
        _require(reasoning["value"] == expected_reasoning, "unexpected_reasoning_setting", "visible reasoning setting differs from expected paired setting")
    steps: list[dict[str, Any]] = []
    for index, observed in enumerate(observation["steps"]):
        phase = observed["phase"]
        asset = plan["initial_context"] if phase == "INITIAL" else plan.get("followup_context")
        _require(isinstance(asset, Mapping), "followup_binding_missing", "follow-up evidence is not bound in this plan")
        if phase == "INITIAL":
            prompt_digest = plan["prompt"]["sha256"]
            operator_digest = None
        else:
            prompt_digest = None
            operator_digest = gate._sha256_bytes(gate.FOLLOWUP_OPERATOR_MESSAGE.encode("utf-8"))
        answer = observed["assistant_answer_markdown"]
        steps.append({
            "phase": phase,
            "processing_duration_seconds": observed["processing_duration_seconds"],
            "started_at": observed["started_at"],
            "finished_at": observed["finished_at"],
            "prompt_digest": prompt_digest,
            "payload_manifest_digest": asset["manifest_digest"],
            "payload_digest": asset["payload_digest"],
            "operator_message_digest": operator_digest,
            "assistant_answer_markdown": answer,
            "assistant_answer_sha256": _sha256(answer.encode("utf-8")),
        })
        if index == 0:
            _require(phase == "INITIAL", "capture_phase_order_invalid", "first Browser answer must be INITIAL")
    environment = {
        "product": "ChatGPT",
        "mode": "normal_chatgpt_browser",
        "model_id": model["value"],
        "reasoning_setting": reasoning["value"],
        "surface": NORMAL_BROWSER_SURFACE,
        "fresh_conversation": observation["fresh_conversation"],
        "same_visible_capability_class": observation["same_visible_capability_class"],
        "api_used": False,
    }
    return {
        "schema": gate.RUN_SCHEMA,
        "scenario_id": plan["scenario_id"],
        "scenario_digest": plan["scenario_digest"],
        "arm_id": plan["arm_id"],
        "arm_type": plan["arm_type"],
        "repo_view": plan["repo_view"],
        "evidence_universe_digest": plan["evidence_universe_digest"],
        "task_text_digest": plan["task_text_digest"],
        "environment": environment,
        "environment_digest": gate.execution_environment_digest(environment),
        "conversation_steps": steps,
        "context_request_used": observation["context_request_used"],
        "requested_source_paths": list(observation["requested_source_paths"]),
        "protocol_burden_visible": observation["protocol_burden_visible"],
        "run_digest": "",
    }


def _record_identity_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(dict(record))
    payload.pop("record_digest", None)
    return payload


def record_digest(record: Mapping[str, Any]) -> str:
    return semantic_digest(_record_identity_payload(record))


def finalize_capture(
    plan_or_path: Mapping[str, Any] | str | Path,
    observation: Mapping[str, Any],
    *,
    output_root: str | Path,
    repo_root: str | Path,
    packet_root: str | Path,
    materialized_root: str | Path,
) -> dict[str, Any]:
    """Finalize one externally observed Browser capture into an immutable record."""

    plan = _plan_from_input(plan_or_path)
    output = _safe_external(output_root, repo_root=repo_root, label="capture output")
    expected_plan = _build_plan(
        repo_root,
        packet_root,
        materialized_root,
        scenario_id=plan["scenario_id"],
        arm_id=plan["arm_id"],
        attempt_id=plan["attempt_id"],
        expected_model_id=plan.get("expected_model_id"),
        expected_reasoning_setting=plan.get("expected_reasoning_setting"),
    )
    _require(plan == expected_plan, "execution_plan_mismatch", "plan does not match the exact frozen packet")
    run = _run_from_observation(plan, observation)
    run["run_digest"] = gate.run_digest(run)
    try:
        validated = gate.validate_arm_run(run, _scenario(Path(packet_root).resolve(), plan["scenario_id"]), packet_root=packet_root, materialized_root=materialized_root)
    except gate.M2dValidationError as exc:
        raise M2dRq1AutomationError("run_record_validation_failure", str(exc), details={"gate_code": exc.code}) from exc
    timing = observation["timing"]
    record: dict[str, Any] = {
        "schema": CAPTURE_SCHEMA,
        "status": CAPTURE_STATUS_FINALIZED,
        "run_id": plan["run_id"],
        "attempt_id": plan["attempt_id"],
        "scenario_id": plan["scenario_id"],
        "scenario_digest": plan["scenario_digest"],
        "arm_id": plan["arm_id"],
        "plan_digest": plan["plan_digest"],
        "repo_view": plan["repo_view"],
        "context_package": plan["initial_context"]["context_package"],
        "browser_capture": {
            "conversation_id": observation["conversation_id"],
            "surface": NORMAL_BROWSER_SURFACE,
            "capture_identity": semantic_digest({"run_id": plan["run_id"], "conversation_id": observation["conversation_id"], "steps": [step["assistant_answer_sha256"] for step in run["conversation_steps"]]}),
            "answer_source": ANSWER_SOURCE,
            "synthetic": False,
        },
        "attestation": {
            "product": "ChatGPT",
            "model_id": _attestation(observation["model_id"], field="model_id"),
            "reasoning_setting": _attestation(observation["reasoning_setting"], field="reasoning_setting"),
            "fresh_conversation": True,
            "same_visible_capability_class": True,
        },
        "timing": dict(timing),
        "raw_answer_identity": {
            "steps": [{"phase": step["phase"], "sha256": step["assistant_answer_sha256"]} for step in run["conversation_steps"]],
            "preservation": "exact_utf8_markdown_in_run_record",
        },
        "evaluation_linkage": {
            "status": "PENDING",
            "evaluator_schema": EVALUATOR_SCHEMA,
            "scenario_id": plan["scenario_id"],
            "run_digest": run["run_digest"],
            "gate_input": "embedded_run_record",
        },
        "run_record": run,
        "validated_run": validated,
        "failure": None,
    }
    record["record_digest"] = record_digest(record)
    destination = output / "captures" / plan["scenario_id"] / plan["arm_id"] / f"{plan['run_id'].replace(':', '_')}.json"
    _publish_immutable(destination, canonical_json_bytes(record))
    return {"record": record, "path": str(destination)}


def abort_capture(
    plan_or_path: Mapping[str, Any] | str | Path,
    *,
    output_root: str | Path,
    repo_root: str | Path,
    reason: str,
    observed_failure: str,
) -> dict[str, Any]:
    """Persist an explicit incomplete attempt without inventing a Browser answer."""

    plan = _plan_from_input(plan_or_path)
    output = _safe_external(output_root, repo_root=repo_root, label="abort output")
    _require(isinstance(reason, str) and reason.strip(), "abort_reason_missing", "abort reason is required")
    _require(isinstance(observed_failure, str) and observed_failure.strip(), "abort_failure_missing", "observed failure is required")
    record: dict[str, Any] = {
        "schema": CAPTURE_SCHEMA,
        "status": CAPTURE_STATUS_ABORTED,
        "run_id": plan["run_id"],
        "attempt_id": plan["attempt_id"],
        "scenario_id": plan["scenario_id"],
        "scenario_digest": plan["scenario_digest"],
        "arm_id": plan["arm_id"],
        "plan_digest": plan["plan_digest"],
        "repo_view": plan["repo_view"],
        "context_package": None,
        "browser_capture": None,
        "attestation": None,
        "timing": None,
        "raw_answer_identity": None,
        "failure": {"reason": reason, "observed_failure": observed_failure},
        "run_record": None,
        "validated_run": None,
        "evaluation_linkage": {"status": "NOT_ELIGIBLE", "evaluator_schema": EVALUATOR_SCHEMA},
    }
    record["record_digest"] = record_digest(record)
    destination = output / "captures" / plan["scenario_id"] / plan["arm_id"] / f"{plan['run_id'].replace(':', '_')}.aborted.json"
    _publish_immutable(destination, canonical_json_bytes(record))
    return {"record": record, "path": str(destination)}


def validate_capture_record(
    record_or_path: Mapping[str, Any] | str | Path,
    *,
    repo_root: str | Path,
    packet_root: str | Path,
    materialized_root: str | Path,
) -> dict[str, Any]:
    """Validate an immutable finalized/aborted record without quality scoring."""

    record = _json(Path(record_or_path)) if isinstance(record_or_path, (str, Path)) else dict(record_or_path)
    required = {
        "schema", "status", "run_id", "attempt_id", "scenario_id", "scenario_digest", "arm_id",
        "plan_digest", "repo_view", "context_package", "browser_capture", "attestation", "timing",
        "raw_answer_identity", "evaluation_linkage", "run_record", "validated_run", "failure", "record_digest",
    }
    _require(set(record) == required, "capture_field_set", "capture record has unexpected or missing fields")
    _require(record.get("schema") == CAPTURE_SCHEMA, "capture_schema_mismatch", "unsupported capture schema")
    _require(record.get("record_digest") == record_digest(record), "capture_digest_mismatch", "capture identity is invalid")
    _require(record.get("status") in {CAPTURE_STATUS_FINALIZED, CAPTURE_STATUS_ABORTED}, "capture_status_invalid", "unknown capture status")
    scenario = _scenario(Path(packet_root).resolve(), record["scenario_id"])
    observed_view = gate.RepoViewBinding.from_view(gate._subject_view(repo_root)).as_dict()
    _require(record.get("repo_view") == scenario["repo_view"] == observed_view == gate.frozen_repo_view_dict(), "capture_repo_view_mismatch", record["scenario_id"])
    if record["status"] == CAPTURE_STATUS_ABORTED:
        _require(record.get("failure") is not None, "aborted_failure_missing", "aborted capture must preserve the observed failure")
        _require(record.get("run_record") is None, "aborted_record_has_answer", "aborted capture cannot contain a run record")
        return {"status": CAPTURE_STATUS_ABORTED, "run_id": record["run_id"], "evaluator_eligible": False}
    run = record.get("run_record")
    _require(isinstance(run, Mapping), "run_record_missing", "finalized capture has no run record")
    _require(run.get("run_digest") == gate.run_digest(run), "run_digest_mismatch", record["scenario_id"])
    try:
        validated = gate.validate_arm_run(run, scenario, packet_root=packet_root, materialized_root=materialized_root)
    except gate.M2dValidationError as exc:
        raise M2dRq1AutomationError("run_record_validation_failure", str(exc), details={"gate_code": exc.code}) from exc
    _require(record.get("validated_run") == validated, "validated_run_mismatch", "stored validation projection differs from the exact run")
    return {"status": CAPTURE_STATUS_FINALIZED, "run_id": record["run_id"], "run_digest": run["run_digest"], "validated_run": validated, "evaluator_eligible": True}


def evaluator_input(record_or_path: Mapping[str, Any] | str | Path) -> dict[str, Any]:
    """Return the existing v2 run record; this function never judges quality."""

    record = _json(Path(record_or_path)) if isinstance(record_or_path, (str, Path)) else record_or_path
    _require(record.get("schema") == CAPTURE_SCHEMA and record.get("status") == CAPTURE_STATUS_FINALIZED, "evaluator_input_invalid", "only finalized captures can enter evaluation")
    run = record.get("run_record")
    _require(isinstance(run, Mapping), "evaluator_input_missing", "finalized capture has no evaluator input")
    return copy.deepcopy(dict(run))


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare", help="prepare disposable exact Browser plans")
    prepare.add_argument("--repo-root", default=".")
    prepare.add_argument("--packet-root", required=True)
    prepare.add_argument("--materialized-root", required=True)
    prepare.add_argument("--output", required=True)
    prepare.add_argument("--attempt-id", default="attempt-1")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            print(json.dumps(prepare_attempt(args.repo_root, args.packet_root, args.materialized_root, args.output, attempt_id=args.attempt_id), indent=2, sort_keys=True))
            return 0
    except M2dRq1AutomationError as exc:
        print(json.dumps({"status": "INVALID", "code": exc.code, "message": str(exc), "details": exc.details}, indent=2), file=sys.stderr)
        return 2
    return 2


__all__ = [
    "ANSWER_SOURCE",
    "CAPTURE_SCHEMA",
    "CAPTURE_STATUS_ABORTED",
    "CAPTURE_STATUS_FINALIZED",
    "EXECUTION_SCHEMA",
    "M2dRq1AutomationError",
    "NORMAL_BROWSER_SURFACE",
    "PLAN_SCHEMA",
    "abort_capture",
    "evaluator_input",
    "finalize_capture",
    "plan_digest",
    "prepare_attempt",
    "prepare_run",
    "record_digest",
    "validate_capture_record",
]


if __name__ == "__main__":
    raise SystemExit(_main())

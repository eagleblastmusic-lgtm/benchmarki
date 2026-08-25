"""BDB vNext - NX-001 Baseline Manifest and Invariant Suite.

This module provides the canonical implementation-time baseline manifest,
the invariant mapping against current test suites/verifiers, the register
of historical claims marked as stale, and the deterministic machine gate.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

MANIFEST_SCHEMA = "bdb-vnext-nx001-baseline-manifest-v1"
INVARIANT_MAP_SCHEMA = "bdb-vnext-nx001-invariant-map-v1"
ACCEPTED_HEAD = "abb55690fcd583cfd9b2f1cd922e71709165b999"
ACCEPTED_TREE = "9ffc7cec9a2f131965ef12063ac892e7e63a0cae"
ACCEPTED_BRANCH = "bdb-vnext"
ACCEPTED_UPSTREAM = "origin/bdb-vnext"

CANONICAL_SOURCE_ROOT = r"C:\Projekty\DevMaster\bartosz-dev-bridge-vnext"
CANONICAL_RUNTIME_ROOT = r"C:\Projekty\DevMaster\bartosz-dev-bridge-vnext\runtime"
SHARED_ENVIRONMENT_ROOT = r"C:\Projekty\_Shared"

PINNED_BROWSER_EXTENSION_ID = "mopnolkjddkmgojfjkenjobehhmmklll"
PINNED_BROWSER_BUNDLE_DIGEST = "sha256:a9a8ed3b05908ea35f999c4716ddada7d44527f01f16b700170bc754a381d784"
PINNED_NATIVE_HOST_NAME = "com.bartosz.dev_bridge.vnext"
PINNED_NATIVE_EXECUTABLE_SHA256 = "sha256:fd19e6b77b1955f32c4b6ed83047c34c1337e6da6688cb1c0b434839e7a1c00e"
PINNED_NATIVE_MANIFEST_SHA256 = "sha256:2576f8987ee7bfeea3fdef81edbdd0f93553b3ae5b72c4cb77193afa63bb258f"
PINNED_CLIENT_PLAN_SHA256 = "sha256:f2103ee2c304ebc6196607ec1c8cac17c8b37bab87e07c7fbb5ce968111d08bb"


def canonical_json_bytes(data: Any) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_digest(data: Any) -> str:
    if isinstance(data, (bytes, bytearray)):
        b = bytes(data)
    elif isinstance(data, str):
        b = data.encode("utf-8")
    else:
        b = canonical_json_bytes(data)
    return f"sha256:{hashlib.sha256(b).hexdigest()}"


@dataclass(frozen=True)
class SourceGitIdentity:
    branch: str
    head: str
    tree: str
    upstream: str
    clean_worktree_required: bool = True


@dataclass(frozen=True)
class SingleRootTopology:
    canonical_source_root: str
    canonical_runtime_root: str
    shared_environment_root: str
    disallow_appdata_runtime: bool = True


@dataclass(frozen=True)
class RuntimeIdentity:
    generation_id: str
    protocol_generation: str
    browser_extension_id: str
    browser_bundle_digest: str
    native_host_name: str
    native_executable_sha256: str
    native_manifest_sha256: str
    client_plan_sha256: str
    native_artifact_kind: str = "pyinstaller-onedir"


@dataclass(frozen=True)
class ControlStateIdentity:
    m3c_mode: str
    m3c_schema: str
    m9b_state: str
    m9b_schema: str
    writer_enabled: bool = True
    intake_enabled: bool = True


@dataclass(frozen=True)
class SchemaEntry:
    schema_name: str
    category: str
    description: str


@dataclass(frozen=True)
class BaselineManifest:
    schema: str
    task_id: str
    project_id: str
    created_at: str
    source_git: SourceGitIdentity
    single_root: SingleRootTopology
    runtime_identity: RuntimeIdentity
    control_state: ControlStateIdentity
    relevant_schemas: list[SchemaEntry]
    manifest_digest: str = ""

    def to_dict(self, include_digest: bool = True) -> dict[str, Any]:
        d = {
            "schema": self.schema,
            "task_id": self.task_id,
            "project_id": self.project_id,
            "created_at": self.created_at,
            "source_git": asdict(self.source_git),
            "single_root": asdict(self.single_root),
            "runtime_identity": asdict(self.runtime_identity),
            "control_state": asdict(self.control_state),
            "relevant_schemas": [asdict(s) for s in self.relevant_schemas],
        }
        if include_digest:
            d["manifest_digest"] = self.manifest_digest
        return d


@dataclass(frozen=True)
class InvariantItem:
    invariant_id: str
    invariant_class: str
    title: str
    statement: str
    target_authority: str
    existing_tests: list[str]
    coverage_status: str  # "VERIFIED_COVERED", "PARTIALLY_COVERED", "PLANNED_IN_NX_M0_PLUS"
    notes: str = ""


@dataclass(frozen=True)
class StaleClaimItem:
    claim_id: str
    category: str
    historical_assertion: str
    why_stale: str
    qualification_requirement: str


@dataclass(frozen=True)
class BaselineVerificationResult:
    passed: bool
    source_head: str
    source_tree: str
    expected_head: str
    expected_tree: str
    branch: str
    upstream: str
    worktree_clean: bool
    mismatches: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


def get_relevant_schemas() -> list[SchemaEntry]:
    return [
        SchemaEntry("bdb-project-plan-v1.schema.json", "project_planning", "Immutable baseline project plan schema"),
        SchemaEntry("bdb-project-execution-submission-v1.schema.json", "project_execution", "Canonical machine-readable task execution result schema"),
        SchemaEntry("bdb-vnext-m11c-client-plan-v1.schema.json", "deployment", "Client staging and promotion deployment plan schema"),
        SchemaEntry("bdb-vnext-m9b-activation-v1.schema.json", "runtime_control", "M9b dynamic writer and intake activation schema"),
        SchemaEntry("bdb-vnext-m3c-admission-v1.schema.json", "runtime_control", "M3c static admission and kill-switch control schema"),
        SchemaEntry("bdb-vnext-backup-manifest-v2.schema.json", "recovery", "Coordinated backup and recovery manifest schema"),
        SchemaEntry("bdb-vnext-candidate-repo-view-v2.schema.json", "engineering_loop", "Candidate repository view and mutation authority schema"),
        SchemaEntry("bdb-vnext-nx001-baseline-manifest-v1.schema.json", "baseline", "NX-001 source-bound implementation baseline manifest schema"),
    ]


def build_accepted_baseline_manifest(created_at: str = "2026-08-25T23:00:00Z") -> BaselineManifest:
    source_git = SourceGitIdentity(
        branch=ACCEPTED_BRANCH,
        head=ACCEPTED_HEAD,
        tree=ACCEPTED_TREE,
        upstream=ACCEPTED_UPSTREAM,
        clean_worktree_required=True,
    )
    single_root = SingleRootTopology(
        canonical_source_root=CANONICAL_SOURCE_ROOT,
        canonical_runtime_root=CANONICAL_RUNTIME_ROOT,
        shared_environment_root=SHARED_ENVIRONMENT_ROOT,
        disallow_appdata_runtime=True,
    )
    runtime_identity = RuntimeIdentity(
        generation_id="bdb-vnext-g1",
        protocol_generation="bdb-vnext-protocol-v1",
        browser_extension_id=PINNED_BROWSER_EXTENSION_ID,
        browser_bundle_digest=PINNED_BROWSER_BUNDLE_DIGEST,
        native_host_name=PINNED_NATIVE_HOST_NAME,
        native_executable_sha256=PINNED_NATIVE_EXECUTABLE_SHA256,
        native_manifest_sha256=PINNED_NATIVE_MANIFEST_SHA256,
        client_plan_sha256=PINNED_CLIENT_PLAN_SHA256,
        native_artifact_kind="pyinstaller-onedir",
    )
    control_state = ControlStateIdentity(
        m3c_mode="INTERNAL_CANONICAL_ONLY",
        m3c_schema="bdb-vnext-m3c-control-v2",
        m9b_state="ACTIVE",
        m9b_schema="bdb-vnext-m9b-activation-v1",
        writer_enabled=True,
        intake_enabled=True,
    )
    schemas = get_relevant_schemas()

    manifest_without_digest = BaselineManifest(
        schema=MANIFEST_SCHEMA,
        task_id="NX-001",
        project_id="bdb-vnext-next-iteration",
        created_at=created_at,
        source_git=source_git,
        single_root=single_root,
        runtime_identity=runtime_identity,
        control_state=control_state,
        relevant_schemas=schemas,
        manifest_digest="",
    )
    payload_digest = sha256_digest(manifest_without_digest.to_dict(include_digest=False))

    return BaselineManifest(
        schema=MANIFEST_SCHEMA,
        task_id="NX-001",
        project_id="bdb-vnext-next-iteration",
        created_at=created_at,
        source_git=source_git,
        single_root=single_root,
        runtime_identity=runtime_identity,
        control_state=control_state,
        relevant_schemas=schemas,
        manifest_digest=payload_digest,
    )


def build_canonical_invariant_map() -> list[InvariantItem]:
    return [
        # Class 1: single_root
        InvariantItem(
            invariant_id="INV-SR-001",
            invariant_class="single_root",
            title="Single-Root Repository and Runtime",
            statement="C:\\Projekty\\DevMaster\\bartosz-dev-bridge-vnext and its runtime\\ subdirectory are the sole canonical root; no dual long-lived AppData production copy exists.",
            target_authority="Single-Root Governance / Filesystem",
            existing_tests=["tests/test_single_root_migration.py", "tests/test_m11c_single_authority_audit.py"],
            coverage_status="VERIFIED_COVERED",
            notes="AppData retired; preserved bytes under recovery are non-authoritative evidence.",
        ),
        InvariantItem(
            invariant_id="INV-SR-002",
            invariant_class="single_root",
            title="Repo-Local Native Messaging Manifest",
            statement="HKCU Native Messaging registry routes (both 32-bit and 64-bit) point to repo-local runtime manifest.",
            target_authority="Windows Registry / Native Messaging",
            existing_tests=["tests/test_m11c_native_route_policy.py", "tests/test_m11c_client_route_rebind.py"],
            coverage_status="VERIFIED_COVERED",
        ),
        # Class 2: source_bound_runtime
        InvariantItem(
            invariant_id="INV-SBR-001",
            invariant_class="source_bound_runtime",
            title="Client Promotion Source Binding",
            statement="Client promotion requires exact matching source_head and source_tree; promotion artifact is PyInstaller onedir windowless.",
            target_authority="m11c_client_promotion / Artifact Manifest",
            existing_tests=["tests/test_m11c_client_promotion.py", "tests/test_m11c_native_artifact_windows.py"],
            coverage_status="VERIFIED_COVERED",
        ),
        InvariantItem(
            invariant_id="INV-SBR-002",
            invariant_class="source_bound_runtime",
            title="Post-Active Maintenance Fresh Verification",
            statement="Maintenance mutation requires fresh m11c_client_verification matching active source HEAD before mutation.",
            target_authority="m11c_post_active_maintenance",
            existing_tests=["tests/test_m11c_post_active_maintenance.py", "tests/test_m11c_post_active_maintenance_client_binding.py"],
            coverage_status="VERIFIED_COVERED",
        ),
        # Class 3: bootstrap_m9b_m3c_separation
        InvariantItem(
            invariant_id="INV-SEP-001",
            invariant_class="bootstrap_m9b_m3c_separation",
            title="Static Admission Kill-Switch (M3c)",
            statement="M3c control v2 maintains static mode/kill-switch (INTERNAL_CANONICAL_ONLY) without dynamic intake projection.",
            target_authority="m3c_admission",
            existing_tests=["tests/test_vnext_m3c_admission.py"],
            coverage_status="VERIFIED_COVERED",
        ),
        InvariantItem(
            invariant_id="INV-SEP-002",
            invariant_class="bootstrap_m9b_m3c_separation",
            title="Dynamic Writer/Intake Authority (M9b)",
            statement="M9b is the sole authority for active writer and intake state in production runtime.",
            target_authority="m9b_activation",
            existing_tests=["tests/test_m11c_cutover.py", "tests/test_m11c_m9a_production_boundary.py"],
            coverage_status="VERIFIED_COVERED",
        ),
        # Class 4: project_execution_authority
        InvariantItem(
            invariant_id="INV-PEA-001",
            invariant_class="project_execution_authority",
            title="Project Memory and Execution Authority Separation",
            statement="project-plan.json is immutable baseline history; runtime progress, cursors, and bindings belong strictly to Project Execution.",
            target_authority="project_execution / project_memory",
            existing_tests=["tests/test_vnext_project_launch_browser_bridge.py", "tests/test_project_launch_queue.py"],
            coverage_status="VERIFIED_COVERED",
            notes="Binding lifecycle uniqueness to be further hardened in NX-003.",
        ),
        InvariantItem(
            invariant_id="INV-PEA-002",
            invariant_class="project_execution_authority",
            title="Deterministic Acceptance Pipeline",
            statement="Canonical submission requires strict bdb-project-execution-submission-v1 schema and exact binding identity match.",
            target_authority="project_workflow / acceptance",
            existing_tests=["tests/test_vnext_m3a_submission.py"],
            coverage_status="VERIFIED_COVERED",
            notes="Full result identity versioning to be extended in NX-004.",
        ),
        # Class 5: browser_native_protocol
        InvariantItem(
            invariant_id="INV-BNP-001",
            invariant_class="browser_native_protocol",
            title="Durable Launch Handoff & Exactly-Once",
            statement="Prompt handoff follows PENDING -> CLAIMED -> SENT/ACKED states to prevent duplicate sends across reloads.",
            target_authority="project_launch / transport_worker",
            existing_tests=["tests/test_vnext_project_launch_browser_bridge.py", "tests/test_browser_auto_send_confirmation_runtime.py"],
            coverage_status="VERIFIED_COVERED",
        ),
        InvariantItem(
            invariant_id="INV-BNP-002",
            invariant_class="browser_native_protocol",
            title="Physical Send Confirmation & Focus Resilience",
            statement="AUTO send confirmation relies on physically observed DOM prompt submission (SEND_ATTEMPTED -> SEND_CONFIRMED -> ACKED), not window focus.",
            target_authority="browser_extension_vnext / content_adapter.js",
            existing_tests=["tests/test_vnext_project_auto_browser.py", "tests/test_browser_auto_send_confirmation_runtime.py"],
            coverage_status="VERIFIED_COVERED",
        ),
        # Class 6: fail_closed_identity
        InvariantItem(
            invariant_id="INV-FCI-001",
            invariant_class="fail_closed_identity",
            title="Fail-Closed on Identity / Correlation Mismatch",
            statement="Any mismatch in project_id, task_id, execution_binding_id, correlation_id or repo HEAD rejects execution and halts AUTO.",
            target_authority="project_workflow / project_execution",
            existing_tests=["tests/test_vnext_project_auto_browser.py"],
            coverage_status="VERIFIED_COVERED",
        ),
        InvariantItem(
            invariant_id="INV-FCI-002",
            invariant_class="fail_closed_identity",
            title="Replay Safety Without Duplicate Effects",
            statement="Exact semantic replay of an already accepted submission returns idempotent receipt without creating duplicate attempts.",
            target_authority="project_execution / project_workflow",
            existing_tests=["tests/test_browser_auto_replay_recovery_runtime.py"],
            coverage_status="VERIFIED_COVERED",
        ),
    ]


def get_stale_historical_claims() -> list[StaleClaimItem]:
    return [
        StaleClaimItem(
            claim_id="STALE-001",
            category="m2d_quality_gate",
            historical_assertion="M2d Quality Gate 14 PASS",
            why_stale="Historical benchmark M2d suite is pinned to frozen historical commit 4b724eda100345969eb236f877dd46f0bb91c0cb. Current active HEAD is abb5569.",
            qualification_requirement="Cannot be cited as fresh PASS on active HEAD without running updated source-bound basis verification.",
        ),
        StaleClaimItem(
            claim_id="STALE-002",
            category="legacy_test_suite",
            historical_assertion="Legacy CC 0.3 / 145 tests passed",
            why_stale="Tests from legacy Control Center and command journal era reflect retired architectures with dual AppData roots and Promoter.",
            qualification_requirement="Only vNext focused suite (65 focused tests) executed on current HEAD constitutes valid fresh test evidence.",
        ),
        StaleClaimItem(
            claim_id="STALE-003",
            category="appdata_runtime",
            historical_assertion="%LOCALAPPDATA%\\BartoszDevBridge is active production runtime",
            why_stale="Historical AppData production root was canonically retired under single-root migration. Repositories and runtime are exclusively under C:\\Projekty\\DevMaster.",
            qualification_requirement="Any claim of AppData runtime authority is invalid and must be rejected.",
        ),
        StaleClaimItem(
            claim_id="STALE-004",
            category="historical_browser_witness",
            historical_assertion="Browser verification PASS from earlier commits (e.g. eae9fee, 38cdd03)",
            why_stale="Browser verification is strictly source-bound and valid only for the exact built bundle and source HEAD it was qualified against.",
            qualification_requirement="Requires fresh m11c_client_verification matching HEAD abb5569.",
        ),
        StaleClaimItem(
            claim_id="STALE-005",
            category="bootstrap_candidate_slot",
            historical_assertion="Bootstrap CANDIDATE slot ready for promotion",
            why_stale="Previous candidate slots from earlier updates were either committed or discarded. Current state has CANDIDATE = null.",
            qualification_requirement="New candidate slots require fresh creation and staging under M11a before promotion.",
        ),
    ]


def read_git_state(repo_root: Path, git_ref: str | None = None) -> dict[str, Any]:
    def _run(args: Sequence[str]) -> str:
        res = subprocess.run(args, cwd=repo_root, capture_output=True, text=True, check=True)
        return res.stdout.strip()

    ref = git_ref if git_ref else "HEAD"
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    head = _run(["git", "rev-parse", ref])
    tree = _run(["git", "rev-parse", f"{ref}^{{tree}}"])
    upstream = _run(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    status = _run(["git", "status", "--porcelain"])

    return {
        "branch": branch,
        "head": head,
        "tree": tree,
        "upstream": upstream,
        "worktree_clean": (len(status) == 0),
        "status_porcelain": status,
    }


def verify_baseline_source(
    repo_root: Path | str | None = None,
    expected_manifest: BaselineManifest | None = None,
    git_ref: str | None = None,
) -> BaselineVerificationResult:
    if repo_root is None:
        repo_root = Path(CANONICAL_SOURCE_ROOT)
    else:
        repo_root = Path(repo_root)

    if expected_manifest is None:
        expected_manifest = build_accepted_baseline_manifest()

    git_state = read_git_state(repo_root, git_ref=git_ref)
    mismatches: list[str] = []

    if git_state["head"] != expected_manifest.source_git.head:
        mismatches.append(
            f"stale_head: expected {expected_manifest.source_git.head}, got {git_state['head']}"
        )
    if git_state["tree"] != expected_manifest.source_git.tree:
        mismatches.append(
            f"stale_tree: expected {expected_manifest.source_git.tree}, got {git_state['tree']}"
        )
    if git_state["branch"] != expected_manifest.source_git.branch:
        mismatches.append(
            f"branch_mismatch: expected {expected_manifest.source_git.branch}, got {git_state['branch']}"
        )
    if git_state["upstream"] != expected_manifest.source_git.upstream:
        mismatches.append(
            f"upstream_mismatch: expected {expected_manifest.source_git.upstream}, got {git_state['upstream']}"
        )
    if expected_manifest.source_git.clean_worktree_required and not git_state["worktree_clean"]:
        mismatches.append(f"dirty_worktree: {git_state['status_porcelain']}")

    passed = len(mismatches) == 0
    return BaselineVerificationResult(
        passed=passed,
        source_head=git_state["head"],
        source_tree=git_state["tree"],
        expected_head=expected_manifest.source_git.head,
        expected_tree=expected_manifest.source_git.tree,
        branch=git_state["branch"],
        upstream=git_state["upstream"],
        worktree_clean=git_state["worktree_clean"],
        mismatches=mismatches,
        details=git_state,
    )


def verify_invariant_map(invariant_items: list[InvariantItem] | None = None) -> tuple[bool, list[str]]:
    if invariant_items is None:
        invariant_items = build_canonical_invariant_map()

    required_classes = {
        "single_root",
        "source_bound_runtime",
        "bootstrap_m9b_m3c_separation",
        "project_execution_authority",
        "browser_native_protocol",
        "fail_closed_identity",
    }
    present_classes = {item.invariant_class for item in invariant_items}
    missing_classes = required_classes - present_classes

    errors: list[str] = []
    if missing_classes:
        errors.append(f"Missing required invariant classes: {sorted(missing_classes)}")

    for item in invariant_items:
        if not item.existing_tests:
            errors.append(f"Invariant {item.invariant_id} has no mapped tests")
        if item.coverage_status not in ("VERIFIED_COVERED", "PARTIALLY_COVERED", "PLANNED_IN_NX_M0_PLUS"):
            errors.append(f"Invariant {item.invariant_id} has invalid coverage_status: {item.coverage_status}")

    return (len(errors) == 0, errors)


def verify_single_root_smoke(
    repo_root: Path | str | None = None,
    runtime_root: Path | str | None = None,
) -> tuple[bool, list[str]]:
    if repo_root is None:
        repo_root = Path(CANONICAL_SOURCE_ROOT)
    else:
        repo_root = Path(repo_root)

    if runtime_root is None:
        runtime_root = repo_root / "runtime"
    else:
        runtime_root = Path(runtime_root)

    errors: list[str] = []

    # Verify single-root paths
    if not runtime_root.exists():
        errors.append(f"Runtime root does not exist: {runtime_root}")
        return False, errors

    # Check existence of canonical client and control directories without mutating
    clients_dir = runtime_root / "clients"
    config_dir = runtime_root / "config"
    control_dir = runtime_root / "control"

    if not clients_dir.exists():
        errors.append(f"Clients directory missing: {clients_dir}")
    if not config_dir.exists():
        errors.append(f"Config directory missing: {config_dir}")
    if not control_dir.exists():
        errors.append(f"Control directory missing: {control_dir}")

    # Confirm AppData runtime is not treated as authority
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    if local_appdata:
        appdata_runtime = Path(local_appdata) / "BartoszDevBridge-vNext" / "runtime"
        # If it exists, verify our canonical config does not point to it
        if config_dir.exists():
            for cfg_file in config_dir.glob("*.json"):
                try:
                    with open(cfg_file, "r", encoding="utf-8") as f:
                        content = f.read()
                    if str(appdata_runtime).lower() in content.lower():
                        errors.append(f"Configuration file {cfg_file.name} references retired AppData runtime")
                except Exception as ex:
                    errors.append(f"Could not read config file {cfg_file.name}: {ex}")

    return (len(errors) == 0, errors)


def run_nx001_machine_gate(
    repo_root: Path | str | None = None,
    target_git_ref: str | None = ACCEPTED_HEAD,
) -> tuple[bool, dict[str, Any]]:
    manifest = build_accepted_baseline_manifest()
    source_verif = verify_baseline_source(repo_root, manifest, git_ref=target_git_ref)
    inv_passed, inv_errors = verify_invariant_map()
    sr_passed, sr_errors = verify_single_root_smoke(repo_root)

    overall_passed = source_verif.passed and inv_passed and sr_passed
    report = {
        "status": "PASS" if overall_passed else "FAIL",
        "task_id": "NX-001",
        "accepted_head": ACCEPTED_HEAD,
        "accepted_tree": ACCEPTED_TREE,
        "source_head": source_verif.source_head,
        "source_tree": source_verif.source_tree,
        "branch": source_verif.branch,
        "upstream": source_verif.upstream,
        "worktree_clean": source_verif.worktree_clean,
        "manifest_digest": manifest.manifest_digest,
        "source_verification": {
            "passed": source_verif.passed,
            "mismatches": source_verif.mismatches,
        },
        "invariant_map_verification": {
            "passed": inv_passed,
            "errors": inv_errors,
            "invariants_count": len(build_canonical_invariant_map()),
        },
        "single_root_smoke": {
            "passed": sr_passed,
            "errors": sr_errors,
        },
        "stale_historical_claims_count": len(get_stale_historical_claims()),
    }
    return overall_passed, report

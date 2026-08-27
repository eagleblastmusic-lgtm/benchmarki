"""NX-070 — source-bound current documentation consistency gate.

This suite deliberately inspects only the current documentation surfaces. It
does not run the repository qualification suites or mutate runtime state.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote

import pytest


ROOT = Path(__file__).resolve().parents[1]

QUALIFIED_SOURCE_HEAD = "a6aa681ccbf40ca181834ed3fe628152a06dd406"
QUALIFIED_SOURCE_TREE = "a496aefa0667498985f0a117c5e13bf59f2be9ef"
BOOTSTRAP_ROOT = Path(r"C:\ProgramData\BartoszDevBridge-Next\bootstrap")
BOOTSTRAP_STATE_PATH = BOOTSTRAP_ROOT / "slot-state.json"
BOOTSTRAP_MANIFESTS_PATH = BOOTSTRAP_ROOT / "slot-manifests"
LOCAL_ENVELOPE_SCHEMA = "bdb-local-envelope-v1"

CURRENT_DOCUMENT_PATHS = (
    Path("README.md"),
    Path("docs/DOCUMENTATION_STATUS.md"),
    Path("docs/VNEXT_CURRENT_ARCHITECTURE.md"),
    Path("docs/VNEXT_PROJECT_WORKFLOW.md"),
    Path("docs/VNEXT_AUTO_BROWSER_NATIVE.md"),
    Path("docs/VNEXT_PRODUCTION_RUNTIME.md"),
    Path("docs/NX070_CURRENT_STATE.md"),
    Path("docs/NX070_SUPERSESSION_MAP.md"),
)
CURRENT_STATE_PATHS = CURRENT_DOCUMENT_PATHS[:-1]
SCHEMA_EXAMPLE_DIRECTORY = ROOT / "docs" / "examples"

STALE_BASELINE_PREFIXES = (
    "eae9fee",
    "abb5569",
    "9ffc7ce",
    "bd634b8",
)
HISTORICAL_MARKERS = (
    "historical",
    "superseded",
    "previous production",
    "archived",
    "pre-vnext",
    "pre-nx-070",
    "history",
)

PARITY_REQUIREMENTS: Mapping[str, tuple[str, ...]] = {
    "project-memory": (
        "project memory v2",
        "v1→v2 shadow migration",
        "v1 remains the production/reference authority before cutover",
    ),
    "identity-and-lifecycle": (
        "identity preservation",
        "binding lifecycle",
        "attempt/generation semantics",
        "result identity",
        "failure taxonomy",
    ),
    "continuation-and-environment": (
        "continuation/re-entry",
        "CI_WAITING",
        "environment handling",
        "local execution",
        "authenticated IPC",
    ),
    "browser-and-native": (
        "browser result identity",
        "native host routing",
        "com.bartosz.dev_bridge.vnext",
    ),
    "witness-and-uac": (
        "operator checkpoint semantics",
        "UAC remains operator-controlled",
        "secure-desktop restrictions",
    ),
    "learning-authority": (
        "structured records = authority",
        "markdown friction/improvement logs = deterministic projections only",
        "global learning is default off",
        "explicit opt-in",
        "sanitized projections only",
        "retention",
    ),
    "release-contract": (
        "feature flags are default-off",
        "legacy-compatible by default",
        "synthetic canary is isolated",
        "premium calculator is not the canary",
        "premium calculator p3 remains not started",
        "canary rollback does not mutate bootstrap active",
        "fault qualification",
        "release status",
    ),
}

WITNESS_REQUIREMENTS = (
    "real microsoft uiautomationcore/iuiautomation backend",
    "process/window/control identity",
    "pre/action/post evidence",
    "operator checkpoint",
    "coordinate fallback is deny-by-default",
    "no secure-desktop automation",
    "no credential injection",
    "uac remains operator-controlled",
    "source-equivalent manual evidence reusable only under qualified source-equivalence rules",
)

GATE_RESULT_FIELDS = {
    "DOCUMENTATION_CURRENT_SNAPSHOT_VERSION_EXPLICIT",
    "SUPERSESSION_MAP_VERSION_EXPLICIT",
    "DOCUMENTATION_FILES_CHECKED",
    "STALE_CURRENT_BASELINE_REFERENCES",
    "SOURCE_RUNTIME_CONFLATIONS",
    "CURRENT_SHA_DIVERGENCES",
    "BROKEN_DOCUMENTATION_PATHS",
    "BROKEN_INTERNAL_DOCUMENTATION_LINKS",
    "SCHEMA_EXAMPLES_CHECKED",
    "INVALID_SCHEMA_DOCUMENTATION_EXAMPLES",
    "SUPERSEDED_ITEMS",
    "SUPERSEDED_CURRENT_ITEMS_WITHOUT_MAP",
    "HISTORY_OVERWRITTEN_AS_CURRENT",
    "DOCUMENTATION_IMPLEMENTATION_DIVERGENCES",
    "PREMATURE_V2_PRODUCTION_AUTHORITY_CLAIMS",
    "CANARY_PRODUCTION_CONFLATIONS",
    "PREMIUM_P3_FALSE_START_CLAIMS",
    "LEARNING_AUTHORITY_DOCUMENTATION_DIVERGENCES",
    "WITNESS_DOCUMENTATION_DIVERGENCES",
    "BOOTSTRAP_STATE_DOCUMENTATION_DIVERGENCES",
    "HISTORICAL_EVIDENCE_REWRITES",
    "BOOTSTRAP_ACTIVE_MUTATIONS",
    "PRODUCTION_PROMOTION_EFFECTS",
    "PREMIUM_P3_START_EFFECTS",
    "HARDCODED_GATE_RESULT_FIELDS",
    "NO_HARDCODED_GATE_RESULTS",
    "SOURCE_HEAD",
    "SOURCE_TREE",
    "WORKTREE_CLEAN",
    "SOURCE_BOUND_MACHINE_GATE",
    "NX070_STATUS",
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


def _read_scoped_documents() -> dict[Path, str]:
    return {
        relative: (ROOT / relative).read_text(encoding="utf-8")
        for relative in CURRENT_DOCUMENT_PATHS
        if (ROOT / relative).is_file()
    }


def _stale_current_baseline_references(text: str) -> int:
    """Count obsolete identities still used as current qualified-source facts."""
    count = 0
    for line in text.splitlines():
        lowered = line.lower()
        for prefix in STALE_BASELINE_PREFIXES:
            if prefix not in lowered:
                continue
            if any(marker in lowered for marker in HISTORICAL_MARKERS):
                continue
            if prefix in {"abb5569", "9ffc7ce"}:
                source_context = re.search(
                    r"qualified|implementation baseline|current source|current qualified|source declaration",
                    lowered,
                )
                production_context = re.search(
                    r"current production|previous production|deployed|production runtime|\bactive\b",
                    lowered,
                )
                if not source_context or production_context:
                    continue
            count += 1
    return count


def _source_runtime_conflations(text: str) -> int:
    """Count positive claims that the qualified source is already production."""
    count = 0
    for line in text.splitlines():
        lowered = line.lower()
        mentions_source = QUALIFIED_SOURCE_HEAD.lower() in lowered or "qualified source" in lowered
        mentions_runtime = bool(re.search(r"production|deployed|installed|active runtime", lowered))
        if not (mentions_source and mentions_runtime):
            continue
        safe_distinction = any(
            marker in lowered
            for marker in (
                "not",
                "separate",
                "distinct",
                "does not",
                "never",
                "rather than",
                "different",
                "differ",
                "separately",
                "not automatically",
            )
        )
        if not safe_distinction:
            count += 1
    return count


def _premature_v2_production_authority_claims(text: str) -> int:
    count = 0
    for line in text.splitlines():
        lowered = line.lower()
        if "v2" not in lowered or "production" not in lowered:
            continue
        if not re.search(r"authority|current|active|replace|cutover", lowered):
            continue
        if any(marker in lowered for marker in ("v1 remains", "before cutover", "not", "separate", "shadow")):
            continue
        count += 1
    return count


def _canary_production_conflations(text: str) -> int:
    count = 0
    for line in text.splitlines():
        lowered = line.lower()
        if "canary" not in lowered or "production" not in lowered:
            continue
        if any(marker in lowered for marker in ("isolated", "not", "separate", "does not", "never")):
            continue
        count += 1
    return count


def _premium_p3_false_start_claims(text: str) -> int:
    count = 0
    for line in text.splitlines():
        lowered = line.lower()
        normalized = re.sub(r"[^a-z0-9]+", " ", lowered).strip()
        if "p3" not in normalized:
            continue
        if not re.search(r"started|running|active|launched|completed|performed|in progress", normalized):
            continue
        if re.search(
            r"\bnot\s+(?:yet\s+)?(?:started|running|active|launched|completed|performed)\b|\bremains\s+not\b|\bwithout\s+starting\b",
            normalized,
        ):
            continue
        count += 1
    return count


def _learning_authority_conflicts(text: str) -> int:
    count = 0
    forbidden_patterns = (
        r"markdown(?:\s+files?|\s+logs?)?\s+(?:is|are)\s+authoritative",
        r"global learning.*(?:automatically\s+)?(?:edits?|modifies?|writes?).*(?:plan|code)",
        r"global view owns local structured records",
        r"learning automatically edits (?:the )?project plan",
    )
    for line in text.splitlines():
        lowered = line.lower()
        for pattern in forbidden_patterns:
            if re.search(pattern, lowered) and "not" not in lowered:
                count += 1
    return count


def _witness_documentation_divergences(text: str) -> int:
    lowered = text.lower()
    missing = sum(1 for requirement in WITNESS_REQUIREMENTS if requirement not in lowered)
    unsafe = 0
    for line in text.splitlines():
        current = line.lower()
        if "secure-desktop automation" in current and not re.search(r"no|not|never", current):
            unsafe += 1
        if "credential injection" in current and not re.search(r"no|not|never", current):
            unsafe += 1
        if "coordinate fallback" in current and "deny-by-default" not in current:
            unsafe += 1
    return missing + unsafe


def _bootstrap_documentation_divergences(snapshot: str) -> tuple[int, dict[str, Any]]:
    if not BOOTSTRAP_STATE_PATH.is_file():
        return 1, {"errors": ["external Bootstrap slot-state.json is missing"]}
    try:
        state = json.loads(BOOTSTRAP_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return 1, {"errors": [f"cannot read Bootstrap slot state: {exc}"]}

    errors: list[str] = []
    manifests: dict[str, dict[str, Any]] = {}
    for field in ("active_manifest_sha256", "previous_manifest_sha256"):
        digest = state.get(field)
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            errors.append(f"Bootstrap {field} is missing or malformed")
            continue
        path = BOOTSTRAP_MANIFESTS_PATH / f"{digest.removeprefix('sha256:')}.json"
        if not path.is_file():
            errors.append(f"Bootstrap manifest is missing: {path}")
            continue
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            errors.append(f"Bootstrap manifest cannot be read: {exc}")
            continue
        if manifest.get("manifest_sha256") != digest:
            errors.append(f"Bootstrap manifest digest mismatch for {field}")
        manifests[field] = manifest

    if errors:
        return len(errors), {"errors": errors, "state": state, "manifests": manifests}

    active = manifests["active_manifest_sha256"]
    previous = manifests["previous_manifest_sha256"]
    active_source = active.get("source_commit")
    active_tree = ""
    if isinstance(active_source, str):
        rc_tree, active_tree = _git("rev-parse", f"{active_source}^{{tree}}")
        if rc_tree != 0:
            errors.append("ACTIVE source tree cannot be resolved from the repository")

    checks = (
        state.get("candidate_manifest_sha256") is None and "`CANDIDATE` | `null`" in snapshot,
        state.get("generation_id") in snapshot,
        state.get("active_manifest_sha256") in snapshot,
        active_source in snapshot,
        active_tree in snapshot,
        state.get("previous_manifest_sha256") in snapshot,
        previous.get("source_commit") in snapshot,
        state.get("production_activation_performed") is True
        and "production_activation_performed: true" in snapshot,
        "NX-070 performed no production" in snapshot,
    )
    divergences = len(errors) + sum(1 for check in checks if not check)
    return divergences, {
        "state": state,
        "manifests": manifests,
        "active_tree": active_tree,
        "errors": errors,
    }


def _markdown_link_issues(documents: Mapping[Path, str]) -> list[str]:
    issues: list[str] = []
    root = ROOT.resolve()
    link_pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
    for relative, content in documents.items():
        source = (ROOT / relative).resolve()
        for raw_target in link_pattern.findall(content):
            target = unquote(raw_target.strip())
            if target.startswith("<") and ">" in target:
                target = target[1 : target.index(">")]
            target = target.split("#", 1)[0].split("?", 1)[0].strip()
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue
            candidate = (source.parent / target).resolve()
            if not candidate.is_relative_to(root):
                issues.append(f"{relative}: link escapes repository: {raw_target}")
            elif not candidate.exists():
                issues.append(f"{relative}: missing link target: {target}")
    return issues


def _supersession_rows(map_text: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in map_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or stripped.startswith("| ---"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if cells and cells[0] != "ID":
            rows.append(cells)
    return rows


def _supersession_audit(current_text: str, map_text: str) -> tuple[int, int, int, bool]:
    rows = _supersession_rows(map_text)
    invalid_rows = 0
    for row in rows:
        if len(row) != 6:
            invalid_rows += 1
            continue
        document_match = re.search(r"`([^`]+)`", row[1])
        if not document_match or not (ROOT / document_match.group(1)).is_file():
            invalid_rows += 1
        if not all(row[2:]):
            invalid_rows += 1
    stale_current = _stale_current_baseline_references(current_text)
    unmapped = stale_current + invalid_rows
    history_preserved = (
        "current production" in current_text.lower()
        and "abb55690fcd583cfd9b2f1cd922e71709165b999" in current_text
        and (ROOT / "docs/m12a-vnext-compatibility-zero.md").read_text(encoding="utf-8").find("bd634b85047674b74846ceaed959ac7883e3eb4a") >= 0
    )
    return len(rows), unmapped, (0 if history_preserved else 1), history_preserved


def _validate_example_payload(payload: Mapping[str, Any]) -> str | None:
    schema = payload.get("schema")
    if schema != LOCAL_ENVELOPE_SCHEMA:
        return f"unsupported documentation example schema: {schema!r}"

    try:
        from bdb_bridge.ingestion_validate import parse_command_envelope, parse_manifest_envelope
        from bdb_bridge.protocol import (
            command_path_for,
            manifest_path_for,
            parse_strict_utc_timestamp,
            require_int,
            require_string,
        )

        parse_strict_utc_timestamp(require_string(payload, "submitted_at"), field="submitted_at")
        manifest = payload.get("manifest")
        command = payload.get("command")
        if not isinstance(manifest, dict) or not isinstance(command, dict):
            return "local envelope manifest and command must be objects"
        manifest_session_id = require_string(manifest, "session_id")
        command_session_id = require_string(command, "session_id")
        if manifest_session_id != command_session_id:
            return "local envelope manifest and command sessions differ"
        sequence = require_int(command, "sequence")

        # Validate the documented envelope through the same manifest/command
        # parsers used by local-spool ingestion, without creating runtime or
        # journal state during a documentation-only gate.
        parse_manifest_envelope(
            json.dumps(manifest),
            source_path=manifest_path_for(manifest_session_id),
        )
        parse_command_envelope(
            json.dumps(command),
            source_path=command_path_for(manifest_session_id, sequence),
        )
    except Exception as exc:  # pragma: no cover - surfaced as a gate finding
        return f"documentation example validation raised {type(exc).__name__}: {exc}"
    return None


def _schema_example_audit() -> tuple[int, int, list[str]]:
    examples = sorted(SCHEMA_EXAMPLE_DIRECTORY.glob("*.json"))
    invalid = 0
    errors: list[str] = []
    for path in examples:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            invalid += 1
            errors.append(f"{path.relative_to(ROOT)}: cannot parse JSON: {exc}")
            continue
        error = _validate_example_payload(payload)
        if error is not None:
            invalid += 1
            errors.append(f"{path.relative_to(ROOT)}: {error}")
    return len(examples), invalid, errors


def _changed_paths_from_qualified_source() -> list[str]:
    rc, output = _git("diff", "--name-only", f"{QUALIFIED_SOURCE_HEAD}..HEAD")
    if rc != 0:
        return []
    return [line.strip().replace("\\", "/") for line in output.splitlines() if line.strip()]


def _hardcoded_gate_result_fields() -> list[str]:
    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run_nx070_machine_gate"
    )
    hardcoded: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Dict):
            continue
        for key, value in zip(node.value.keys, node.value.values):
            if isinstance(key, ast.Constant) and key.value in GATE_RESULT_FIELDS and isinstance(value, ast.Constant):
                hardcoded.add(str(key.value))
    return sorted(hardcoded)


def _current_sha_divergences(snapshot: str, head: str, tree: str, changed_paths: list[str]) -> int:
    head_match = re.search(r"Qualified source HEAD\s*\|\s*`([0-9a-f]{40})`", snapshot)
    tree_match = re.search(r"Qualified source TREE\s*\|\s*`([0-9a-f]{40})`", snapshot)
    convention_present = "final documentation commit's `HEAD` and `TREE` are derived" in snapshot
    documentation_change_present = any(
        path in {str(item).replace("\\", "/") for item in CURRENT_DOCUMENT_PATHS if item != Path("docs/NX070_SUPERSESSION_MAP.md")}
        for path in changed_paths
    )
    checks = (
        head_match is not None and head_match.group(1) == QUALIFIED_SOURCE_HEAD,
        tree_match is not None and tree_match.group(1) == QUALIFIED_SOURCE_TREE,
        convention_present,
        bool(re.fullmatch(r"[0-9a-f]{40}", head)),
        bool(re.fullmatch(r"[0-9a-f]{40}", tree)),
        head != QUALIFIED_SOURCE_HEAD,
        tree != QUALIFIED_SOURCE_TREE,
        documentation_change_present,
    )
    return sum(1 for check in checks if not check)


def _parity_divergences(snapshot: str) -> int:
    lowered = snapshot.lower()
    return sum(
        1
        for requirements in PARITY_REQUIREMENTS.values()
        if not all(requirement.lower() in lowered for requirement in requirements)
    )


def _path_mutation_counts(changed_paths: list[str]) -> tuple[int, int, int, int]:
    bootstrap_mutations = sum(
        1
        for path in changed_paths
        if path.startswith("runtime/")
        or (path.startswith("bdb_vnext/") and re.search(r"bootstrap|m11c|promotion|cutover", path, re.IGNORECASE))
    )
    production_effects = sum(
        1
        for path in changed_paths
        if path.startswith("bdb_vnext/") and re.search(r"m11c|promotion|cutover", path, re.IGNORECASE)
    )
    premium_effects = sum(1 for path in changed_paths if re.search(r"premium", path, re.IGNORECASE))
    historical_patterns = (
        re.compile(r"^docs/(?:m|n|x|cc)[^/]*\.md$", re.IGNORECASE),
        re.compile(r"^docs/governance/", re.IGNORECASE),
        re.compile(r"^docs/legacy/", re.IGNORECASE),
    )
    history_rewrites = sum(
        1 for path in changed_paths if any(pattern.search(path) for pattern in historical_patterns)
    )
    return bootstrap_mutations, production_effects, premium_effects, history_rewrites


def run_nx070_machine_gate() -> dict[str, Any]:
    """Derive the complete NX-070 gate from the committed repository and docs."""
    documents = _read_scoped_documents()
    snapshot = documents.get(Path("docs/NX070_CURRENT_STATE.md"), "")
    supersession_map = documents.get(Path("docs/NX070_SUPERSESSION_MAP.md"), "")
    current_text = "\n".join(
        documents.get(relative, "") for relative in CURRENT_STATE_PATHS
    )

    rc_head, head = _git("rev-parse", "HEAD")
    rc_tree, tree = _git("rev-parse", "HEAD^{tree}")
    rc_status, status_porcelain = _git("status", "--porcelain")
    worktree_clean = rc_status == 0 and status_porcelain == ""
    changed_paths = _changed_paths_from_qualified_source()

    link_issues = _markdown_link_issues(documents)
    schema_checked, invalid_schema_examples, schema_errors = _schema_example_audit()
    superseded_items, unmapped_items, history_overwritten, _history_preserved = _supersession_audit(
        current_text, supersession_map
    )
    bootstrap_doc_divergences, bootstrap_observation = _bootstrap_documentation_divergences(snapshot)

    stale_current = _stale_current_baseline_references(current_text)
    source_runtime = _source_runtime_conflations(current_text)
    current_sha = _current_sha_divergences(snapshot, head, tree, changed_paths)
    parity_divergences = _parity_divergences(snapshot)
    learning_conflicts = _learning_authority_conflicts(current_text)
    v2_claims = _premature_v2_production_authority_claims(current_text)
    canary_conflicts = _canary_production_conflations(current_text)
    p3_claims = _premium_p3_false_start_claims(current_text)
    witness_divergences = _witness_documentation_divergences(snapshot.lower())

    bootstrap_mutations, production_effects, premium_effects, history_rewrites = _path_mutation_counts(changed_paths)
    hardcoded_fields = _hardcoded_gate_result_fields()
    no_hardcoded = len(hardcoded_fields) == 0

    snapshot_version_explicit = bool(re.search(r"Snapshot version:\s*`bdb-vnext-current-snapshot-v1`", snapshot))
    map_version_explicit = bool(re.search(r"Map version:\s*`bdb-vnext-supersession-map-v1`", supersession_map))
    documentation_files_checked = sum(1 for relative in CURRENT_DOCUMENT_PATHS if relative in documents)
    broken_paths = len({issue.split(": ", 1)[-1] for issue in link_issues})

    all_pass = all(
        (
            snapshot_version_explicit,
            map_version_explicit,
            documentation_files_checked == len(CURRENT_DOCUMENT_PATHS),
            stale_current == 0,
            source_runtime == 0,
            current_sha == 0,
            broken_paths == 0,
            len(link_issues) == 0,
            schema_checked > 0,
            invalid_schema_examples == 0,
            superseded_items > 0,
            unmapped_items == 0,
            history_overwritten == 0,
            parity_divergences == 0,
            v2_claims == 0,
            canary_conflicts == 0,
            p3_claims == 0,
            learning_conflicts == 0,
            witness_divergences == 0,
            bootstrap_doc_divergences == 0,
            history_rewrites == 0,
            bootstrap_mutations == 0,
            production_effects == 0,
            premium_effects == 0,
            no_hardcoded,
            rc_head == 0,
            rc_tree == 0,
            bool(re.fullmatch(r"[0-9a-f]{40}", head)),
            bool(re.fullmatch(r"[0-9a-f]{40}", tree)),
            worktree_clean,
        )
    )
    source_bound = "PASS" if all_pass and worktree_clean else "FAIL"
    status = "PASS" if all_pass else "FAIL"

    return {
        "DOCUMENTATION_CURRENT_SNAPSHOT_VERSION_EXPLICIT": snapshot_version_explicit,
        "SUPERSESSION_MAP_VERSION_EXPLICIT": map_version_explicit,
        "DOCUMENTATION_FILES_CHECKED": documentation_files_checked,
        "STALE_CURRENT_BASELINE_REFERENCES": stale_current,
        "SOURCE_RUNTIME_CONFLATIONS": source_runtime,
        "CURRENT_SHA_DIVERGENCES": current_sha,
        "BROKEN_DOCUMENTATION_PATHS": broken_paths,
        "BROKEN_INTERNAL_DOCUMENTATION_LINKS": len(link_issues),
        "SCHEMA_EXAMPLES_CHECKED": schema_checked,
        "INVALID_SCHEMA_DOCUMENTATION_EXAMPLES": invalid_schema_examples,
        "SCHEMA_EXAMPLE_ERRORS": schema_errors,
        "SUPERSEDED_ITEMS": superseded_items,
        "SUPERSEDED_CURRENT_ITEMS_WITHOUT_MAP": unmapped_items,
        "HISTORY_OVERWRITTEN_AS_CURRENT": history_overwritten,
        "DOCUMENTATION_IMPLEMENTATION_DIVERGENCES": parity_divergences,
        "PREMATURE_V2_PRODUCTION_AUTHORITY_CLAIMS": v2_claims,
        "CANARY_PRODUCTION_CONFLATIONS": canary_conflicts,
        "PREMIUM_P3_FALSE_START_CLAIMS": p3_claims,
        "LEARNING_AUTHORITY_DOCUMENTATION_DIVERGENCES": learning_conflicts,
        "WITNESS_DOCUMENTATION_DIVERGENCES": witness_divergences,
        "BOOTSTRAP_STATE_DOCUMENTATION_DIVERGENCES": bootstrap_doc_divergences,
        "HISTORICAL_EVIDENCE_REWRITES": history_rewrites,
        "BOOTSTRAP_ACTIVE_MUTATIONS": bootstrap_mutations,
        "PRODUCTION_PROMOTION_EFFECTS": production_effects,
        "PREMIUM_P3_START_EFFECTS": premium_effects,
        "HARDCODED_GATE_RESULT_FIELDS": hardcoded_fields,
        "NO_HARDCODED_GATE_RESULTS": no_hardcoded,
        "SOURCE_HEAD": head,
        "SOURCE_TREE": tree,
        "WORKTREE_CLEAN": worktree_clean,
        "SOURCE_BOUND_MACHINE_GATE": source_bound,
        "NX070_STATUS": status,
        "BOOTSTRAP_OBSERVATION": bootstrap_observation,
    }


def test_nx070_machine_gate_execution() -> None:
    gate = run_nx070_machine_gate()
    print(json.dumps(gate, indent=2, sort_keys=True, default=str))
    assert gate["DOCUMENTATION_CURRENT_SNAPSHOT_VERSION_EXPLICIT"] is True
    assert gate["SUPERSESSION_MAP_VERSION_EXPLICIT"] is True
    assert gate["DOCUMENTATION_FILES_CHECKED"] == len(CURRENT_DOCUMENT_PATHS)
    assert gate["STALE_CURRENT_BASELINE_REFERENCES"] == 0
    assert gate["SOURCE_RUNTIME_CONFLATIONS"] == 0
    assert gate["CURRENT_SHA_DIVERGENCES"] == 0
    assert gate["BROKEN_DOCUMENTATION_PATHS"] == 0
    assert gate["BROKEN_INTERNAL_DOCUMENTATION_LINKS"] == 0
    assert gate["SCHEMA_EXAMPLES_CHECKED"] > 0
    assert gate["INVALID_SCHEMA_DOCUMENTATION_EXAMPLES"] == 0
    assert gate["SUPERSEDED_ITEMS"] > 0
    assert gate["SUPERSEDED_CURRENT_ITEMS_WITHOUT_MAP"] == 0
    assert gate["HISTORY_OVERWRITTEN_AS_CURRENT"] == 0
    assert gate["DOCUMENTATION_IMPLEMENTATION_DIVERGENCES"] == 0
    assert gate["PREMATURE_V2_PRODUCTION_AUTHORITY_CLAIMS"] == 0
    assert gate["CANARY_PRODUCTION_CONFLATIONS"] == 0
    assert gate["PREMIUM_P3_FALSE_START_CLAIMS"] == 0
    assert gate["LEARNING_AUTHORITY_DOCUMENTATION_DIVERGENCES"] == 0
    assert gate["WITNESS_DOCUMENTATION_DIVERGENCES"] == 0
    assert gate["BOOTSTRAP_STATE_DOCUMENTATION_DIVERGENCES"] == 0
    assert gate["HISTORICAL_EVIDENCE_REWRITES"] == 0
    assert gate["BOOTSTRAP_ACTIVE_MUTATIONS"] == 0
    assert gate["PRODUCTION_PROMOTION_EFFECTS"] == 0
    assert gate["PREMIUM_P3_START_EFFECTS"] == 0
    assert gate["HARDCODED_GATE_RESULT_FIELDS"] == []
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    assert gate["WORKTREE_CLEAN"] is True
    assert gate["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
    assert gate["NX070_STATUS"] == "PASS"


def test_negative_stale_current_baseline_is_rejected() -> None:
    assert _stale_current_baseline_references("Implementation baseline: eae9fee9d171d61ded3c9cf539058559679aa9c8") == 1


def test_negative_source_runtime_conflation_is_rejected() -> None:
    assert _source_runtime_conflations(f"The qualified source {QUALIFIED_SOURCE_HEAD} is deployed in production.") == 1


def test_negative_broken_internal_link_is_rejected() -> None:
    issues = _markdown_link_issues({Path("README.md"): "[missing](docs/does-not-exist.md)"})
    assert len(issues) == 1


def test_negative_invalid_schema_example_is_rejected() -> None:
    example = json.loads((SCHEMA_EXAMPLE_DIRECTORY / "bdb-local-envelope-v1.json").read_text(encoding="utf-8"))
    example["submitted_at"] = "not-an-utc-timestamp"
    assert _validate_example_payload(example) is not None


def test_negative_unmapped_superseded_statement_is_rejected() -> None:
    current = "Implementation baseline: eae9fee9d171d61ded3c9cf539058559679aa9c8"
    rows, unmapped, history, _ = _supersession_audit(current, "")
    assert rows == 0
    assert unmapped == 1
    assert history == 1


def test_negative_false_production_deployment_claim_is_rejected() -> None:
    assert _source_runtime_conflations("The qualified source is the installed production runtime.") == 1


def test_negative_false_premium_p3_started_claim_is_rejected() -> None:
    assert _premium_p3_false_start_claims("Premium Calculator P3 is started and running.") == 1

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import bdb_vnext.m11c_active_reader as active_reader
import bdb_vnext.m11c_cutover as broad_reader
import bdb_vnext.m12a_compatibility_zero as m12a
from test_m11c_cutover import _apply, _fixture, _plan


ROOT = Path(__file__).resolve().parents[1]
ACTIVE_PRODUCTION_SOURCE = "bd634b85047674b74846ceaed959ac7883e3eb4a"
SHA = "sha256:" + "a" * 64
PLAN_SHA = "sha256:" + "b" * 64
FREEZE_SHA = "sha256:" + "c" * 64
VERIFY_SHA = "sha256:" + "d" * 64
BUNDLE_SHA = "sha256:" + "e" * 64
PREVIOUS_SHA = "sha256:" + "f" * 64


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return completed.stdout.decode("utf-8").strip()


def _commit_fixture(tmp_path: Path, files: dict[str, str]) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "M12a Test")
    _git(repo, "config", "user.email", "m12a@example.invalid")
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")
    return repo, _git(repo, "rev-parse", "HEAD")


def test_active_reader_matches_broad_m11c_reader_after_cutover(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    plan = _plan(fixture)
    result = _apply(fixture, plan)
    assert result["status"] == "ACTIVE"

    broad = broad_reader.observe_bootstrap_activation(authority_root=fixture["authority"])
    minimal = active_reader.observe_bootstrap_activation(authority_root=fixture["authority"])

    assert broad["status"] == minimal["status"] == "ACTIVE"
    assert broad["production_activation_performed"] is minimal["production_activation_performed"] is True
    assert broad["state"] == minimal["state"]
    assert broad["slots"] == minimal["slots"]
    required = active_reader.require_bootstrap_active(
        fixture["authority"],
        expected_source_head=plan["source_head"],
    )
    assert required["state"]["cutover_plan_sha256"] == plan["cutover_plan_sha256"]


def test_scanner_detects_migration_only_and_legacy_imports(tmp_path: Path) -> None:
    repo, head = _commit_fixture(
        tmp_path,
        {
            "bdb_vnext/m9b_native_host.py": (
                "import bdb_bridge.native_host\n"
                "from bdb_vnext.m11c_cutover import observe_bootstrap_activation\n"
            ),
            "bdb_vnext/m11c_cutover.py": "VALUE = 1\n",
        },
    )
    scan = m12a.scan_active_python_closure(repo_root=repo, source_commit=head)
    assert scan["compatibility_usage_zero"] is False
    assert "bdb_vnext.m11c_cutover" in scan["migration_only_modules"]
    assert "bdb_bridge.native_host" in scan["legacy_package_imports"]


def test_scanner_accepts_minimal_read_only_vnext_closure(tmp_path: Path) -> None:
    repo, head = _commit_fixture(
        tmp_path,
        {
            "bdb_vnext/m9b_native_host.py": "from bdb_vnext.m11c_active_reader import require_bootstrap_active\n",
            "bdb_vnext/m11c_active_reader.py": "from bdb_vnext.bootstrap import read_only\n",
            "bdb_vnext/bootstrap.py": "read_only = True\n",
        },
    )
    scan = m12a.scan_active_python_closure(repo_root=repo, source_commit=head)
    assert scan["compatibility_usage_zero"] is True
    assert scan["migration_only_modules"] == []
    assert scan["legacy_package_imports"] == []


def test_exact_current_branch_native_closure_is_compatibility_zero() -> None:
    head = _git(ROOT, "rev-parse", "HEAD")
    scan = m12a.scan_active_python_closure(repo_root=ROOT, source_commit=head)
    assert scan["compatibility_usage_zero"] is True, scan
    assert "bdb_vnext.m11c_active_reader" in scan["modules"]
    assert "bdb_vnext.m11c_cutover" not in scan["modules"]
    assert scan["migration_only_modules"] == []
    assert scan["legacy_package_imports"] == []


def test_exact_deployed_source_records_the_expected_pre_m12a_dependency() -> None:
    # The user-machine ACTIVE slot is content-addressed to this exact source.
    # M12a must not mistake a later source cleanup for a cleanup of the running
    # frozen artifact.
    _git(ROOT, "cat-file", "-e", f"{ACTIVE_PRODUCTION_SOURCE}^{{commit}}")
    scan = m12a.scan_active_python_closure(
        repo_root=ROOT,
        source_commit=ACTIVE_PRODUCTION_SOURCE,
    )
    assert scan["compatibility_usage_zero"] is False
    assert "bdb_vnext.m11c_cutover" in scan["migration_only_modules"]


def test_browser_scan_detects_legacy_host_literal_and_fallback(tmp_path: Path) -> None:
    repo, head = _commit_fixture(
        tmp_path,
        {
            "browser_extension_vnext/client_files.json": (
                '{"schema":"bdb-vnext-browser-client-files-v1",'
                '"files":["client_files.json","manifest.json","transport_worker.js"]}\n'
            ),
            "browser_extension_vnext/manifest.json": '{"manifest_version":3}\n',
            "browser_extension_vnext/transport_worker.js": (
                'const host = "com.bartosz.dev_bridge";\n'
                "const legacy_fallback = true;\n"
            ),
        },
    )
    scan = m12a.scan_active_browser_bundle(repo_root=repo, source_commit=head)
    assert scan["compatibility_usage_zero"] is False
    assert scan["legacy_native_host_references"] == ["browser_extension_vnext/transport_worker.js"]
    assert scan["legacy_fallback_enabled"] == ["browser_extension_vnext/transport_worker.js"]


def test_exact_current_branch_browser_bundle_is_compatibility_zero() -> None:
    head = _git(ROOT, "rev-parse", "HEAD")
    scan = m12a.scan_active_browser_bundle(repo_root=ROOT, source_commit=head)
    assert scan["compatibility_usage_zero"] is True, scan
    assert scan["legacy_native_host_references"] == []
    assert scan["legacy_fallback_enabled"] == []


def _capture_stubs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, source_zero: bool = True) -> tuple[Path, Path, Path, Path]:
    authority = tmp_path / "authority"
    runtime = tmp_path / "runtime"
    legacy = tmp_path / "legacy"
    repo = tmp_path / "repo"
    for path in (authority, runtime, legacy, repo):
        path.mkdir()

    state = {
        "activation_id": "m11c-final-1",
        "cutover_plan_sha256": PLAN_SHA,
    }
    monkeypatch.setattr(
        m12a,
        "observe_bootstrap_activation",
        lambda **_: {
            "status": "ACTIVE",
            "production_activation_performed": True,
            "state": state,
            "slots": {
                "ACTIVE": {
                    "source_commit": "1" * 40,
                    "bundle_sha256": BUNDLE_SHA,
                    "known_good": True,
                },
                "PREVIOUS": {
                    "source_commit": "2" * 40,
                    "bundle_sha256": PREVIOUS_SHA,
                    "known_good": True,
                },
                "CANDIDATE": None,
            },
        },
    )
    monkeypatch.setattr(
        m12a,
        "read_activation",
        lambda _: SimpleNamespace(
            state="ACTIVE",
            writer_enabled=True,
            intake_enabled=True,
            source_head="1" * 40,
        ),
    )
    monkeypatch.setattr(
        m12a,
        "query_client_plan",
        lambda **_: {
            "plan": {
                "client_plan_sha256": SHA,
                "source_head": "1" * 40,
            }
        },
    )
    monkeypatch.setattr(
        m12a,
        "require_client_verification",
        lambda **_: {
            "verification_sha256": VERIFY_SHA,
            "native_launch_verified": True,
        },
    )
    monkeypatch.setattr(
        m12a,
        "observe_windows_native_routes",
        lambda **_: {
            "target_registered": True,
            "target_conflict": False,
            "legacy_route_present": False,
        },
    )
    monkeypatch.setattr(
        m12a,
        "query_cutover_plan",
        lambda **_: {
            "plan": {
                "cutover_plan_sha256": PLAN_SHA,
                "m9a_freeze_digest": FREEZE_SHA,
            }
        },
    )
    monkeypatch.setattr(m12a, "revalidate_side_by_side_digest", lambda **_: FREEZE_SHA)
    monkeypatch.setattr(
        m12a,
        "verify_side_by_side_archive",
        lambda **_: {
            "archive_readable": True,
            "freeze_digest": FREEZE_SHA,
            "evidence_refs": [FREEZE_SHA],
        },
    )
    monkeypatch.setattr(
        m12a,
        "scan_supported_vnext_admission_paths",
        lambda: {
            "pass": True,
            "legacy_paths_supported": False,
            "alternate_accepting_writers": [],
        },
    )
    monkeypatch.setattr(
        m12a,
        "scan_active_python_closure",
        lambda **_: {
            "compatibility_usage_zero": source_zero,
            "legacy_package_imports": [],
            "migration_only_modules": [] if source_zero else ["bdb_vnext.m11c_cutover"],
        },
    )
    monkeypatch.setattr(
        m12a,
        "scan_active_browser_bundle",
        lambda **_: {
            "compatibility_usage_zero": True,
            "legacy_native_host_references": [],
            "legacy_fallback_enabled": [],
        },
    )
    monkeypatch.setattr(
        m12a,
        "_source_disposition",
        lambda *_: {
            "legacy_entrypoints_present": ["bdb"],
            "legacy_package_patterns_present": ["bdb_bridge*"],
            "disposition": "REMOVE_FROM_TARGET_ONLY_PACKAGE_IN_M12B",
            "presence_allowed_in_m12a": True,
        },
    )
    return authority, runtime, legacy, repo


def test_capture_pass_closed_writes_only_evidence_and_deletion_plan(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    authority, runtime, legacy, repo = _capture_stubs(monkeypatch, tmp_path)
    result = m12a.capture_compatibility_zero(
        authority_root=authority,
        runtime_root=runtime,
        legacy_runtime_root=legacy,
        repo_root=repo,
        observation_seconds=0,
    )
    assert result["status"] == "PASS_CLOSED"
    assert result["production_activation_performed"] is True
    assert result["production_mutation_performed"] is False
    assert result["final_deletion_performed"] is False
    assert result["report"]["compatibility_usage_zero"] is True
    assert result["report"]["archive_readable"] is True
    assert result["report"]["unnamed_exceptions"] == []
    assert result["report"]["legacy_product_globally_disabled"] is False
    assert result["report"]["deletion_plan_sha256"] == result["deletion_plan_sha256"]

    verified = m12a.verify_compatibility_zero(
        runtime_root=runtime,
        report_sha256=result["report_sha256"],
    )
    assert verified["status"] == "PASS_CLOSED"
    assert verified["deletion_plan"]["status"] == "PLANNED_NOT_APPLIED"
    assert verified["final_deletion_performed"] is False


def test_capture_blocks_when_active_source_still_uses_migration_bridge(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    authority, runtime, legacy, repo = _capture_stubs(monkeypatch, tmp_path, source_zero=False)
    result = m12a.capture_compatibility_zero(
        authority_root=authority,
        runtime_root=runtime,
        legacy_runtime_root=legacy,
        repo_root=repo,
        observation_seconds=0,
    )
    assert result["status"] == "BLOCKED"
    assert result["report"]["compatibility_usage_zero"] is False
    matrix = {item["bridge_id"]: item for item in result["report"]["bridge_matrix"]}
    assert matrix["migration-only-vnext-modules"]["usage_zero"] is False
    assert result["final_deletion_performed"] is False


def test_verify_detects_tampered_m12a_report(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    authority, runtime, legacy, repo = _capture_stubs(monkeypatch, tmp_path)
    result = m12a.capture_compatibility_zero(
        authority_root=authority,
        runtime_root=runtime,
        legacy_runtime_root=legacy,
        repo_root=repo,
        observation_seconds=0,
    )
    report_path = Path(result["report_path"])
    report_path.write_text(json.dumps({"tampered": True}), encoding="utf-8")
    with pytest.raises(m12a.M12aCompatibilityError) as caught:
        m12a.verify_compatibility_zero(
            runtime_root=runtime,
            report_sha256=result["report_sha256"],
        )
    assert caught.value.code == "evidence_digest_mismatch"


def test_cli_surface_is_observation_only() -> None:
    parser = m12a._parser()
    actions = parser._subparsers._group_actions[0].choices
    assert set(actions) == {"capture", "verify"}
    forbidden = {
        "delete",
        "drop",
        "contract",
        "apply",
        "activate",
        "switch",
        "install",
        "start",
        "stop",
        "disable",
    }
    assert forbidden.isdisjoint(actions)

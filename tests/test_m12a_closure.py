from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import bdb_vnext.m12a_closure as closure
from bdb_vnext.composition import (
    BROWSER_EXTENSION_ID,
    GENERATION_ID,
    NATIVE_HOST_NAME,
    PROTOCOL_GENERATION,
)
from test_m9a_handoff import _install_stubs, _routes
import bdb_vnext.m9a_handoff as handoff


ROOT = Path(__file__).resolve().parents[1]
SHA = "sha256:" + "a" * 64
FREEZE = "sha256:" + "b" * 64


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
    _git(repo, "config", "user.name", "M12a Closure Test")
    _git(repo, "config", "user.email", "m12a-closure@example.invalid")
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")
    return repo, _git(repo, "rev-parse", "HEAD")


def _benchmark_files() -> dict[str, str]:
    return {
        "docs/LOCAL_BROWSER_BENCHMARK.md": "legacy benchmark history\n",
        "scripts/run_local_browser_benchmark.py": "print('historical')\n",
        "tests/test_local_browser_benchmark.py": "def test_basis(): assert True\n",
    }


def test_inventory_scans_whole_subject_and_classifies_compatibility_surfaces(tmp_path: Path) -> None:
    files = {
        "bdb_bridge/native_host.py": "NAME = 'com.bartosz.dev_bridge'\n",
        "bdb_vnext/m11c_cutover.py": "LEGACY = 'legacy'\n",
        "scripts/Install-BDBNativeHost.ps1": "$root = 'BartoszDevBridge'\n",
        "docs/history.md": "bdb_bridge legacy\n",
        "README.md": "unrelated\n",
        **_benchmark_files(),
    }
    repo, head = _commit_fixture(tmp_path, files)
    inventory = closure.inventory_compatibility_surfaces(repo_root=repo, source_commit=head)
    assert inventory["inventory_complete"] is True
    assert inventory["tracked_path_count"] == len(files)
    assert inventory["unclassified_compatibility_paths"] == []
    paths = {item["path"]: item for item in inventory["entries"]}
    assert paths["bdb_bridge/native_host.py"]["surface_class"] == "LEGACY_RUNTIME_PACKAGE"
    assert paths["bdb_vnext/m11c_cutover.py"]["surface_class"] == "VNEXT_MIGRATION_OR_COMPATIBILITY_SOURCE"
    assert paths["scripts/Install-BDBNativeHost.ps1"]["surface_class"] == "INSTALLER_WRAPPER_SCRIPT"
    assert paths["docs/history.md"]["surface_class"] == "DOCUMENTATION_HISTORY_REFERENCE"


def test_inventory_blocks_unclassified_compatibility_hit(tmp_path: Path) -> None:
    repo, head = _commit_fixture(
        tmp_path,
        {
            "mystery/surface.xyz": "bdb_bridge compatibility\n",
            **_benchmark_files(),
        },
    )
    inventory = closure.inventory_compatibility_surfaces(repo_root=repo, source_commit=head)
    assert inventory["inventory_complete"] is False
    assert inventory["unclassified_compatibility_paths"] == ["mystery/surface.xyz"]


def test_exact_pr_head_has_complete_fresh_compatibility_inventory() -> None:
    head = _git(ROOT, "rev-parse", "HEAD")
    inventory = closure.inventory_compatibility_surfaces(repo_root=ROOT, source_commit=head)
    assert inventory["inventory_complete"] is True, inventory["unclassified_compatibility_paths"]
    assert inventory["tracked_path_count"] > inventory["compatibility_surface_count"] > 0
    assert inventory["unclassified_compatibility_paths"] == []


def test_benchmark_basis_is_named_versioned_and_does_not_rearm_legacy(tmp_path: Path) -> None:
    repo, head = _commit_fixture(tmp_path, {"README.md": "ok\n", **_benchmark_files()})
    basis = closure.build_benchmark_basis(repo_root=repo, source_commit=head)
    assert basis["basis_complete"] is True
    assert basis["missing_basis_files"] == []
    assert basis["scenario_count"] == 8
    assert basis["legacy_benchmark_rearmed"] is False
    assert basis["final_target_only_benchmark_deferred_to_m12b"] is True
    assert all(item["sha256"].startswith("sha256:") for item in basis["historical_basis_files"])


def test_exact_pr_head_has_complete_benchmark_basis() -> None:
    head = _git(ROOT, "rev-parse", "HEAD")
    basis = closure.build_benchmark_basis(repo_root=ROOT, source_commit=head)
    assert basis["basis_complete"] is True, basis
    assert basis["scenario_count"] == 8


def _native_config(runtime: Path, legacy: Path, authority: Path) -> None:
    path = runtime / "config" / "native-host.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema": "bdb-vnext-native-host-config-v2",
                "generation_id": GENERATION_ID,
                "protocol_generation": PROTOCOL_GENERATION,
                "native_host_name": NATIVE_HOST_NAME,
                "browser_extension_id": BROWSER_EXTENSION_ID,
                "runtime_root": str(runtime),
                "legacy_runtime_root": str(legacy),
                "bootstrap_authority_root": str(authority),
            }
        ),
        encoding="utf-8",
    )


def test_stale_client_rehearsal_returns_explicit_upgrade_errors_without_state_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    authority = tmp_path / "authority"
    runtime = tmp_path / "runtime"
    legacy = tmp_path / "legacy"
    for path in (authority, runtime, legacy):
        path.mkdir()
    _native_config(runtime, legacy, authority)
    identity = {
        "bootstrap_state_sha256": SHA,
        "m9b_record_digest": "sha256:" + "c" * 64,
        "client_plan_sha256": "sha256:" + "d" * 64,
        "client_verification_sha256": "sha256:" + "e" * 64,
    }
    monkeypatch.setattr(closure, "_runtime_snapshot", lambda **_: dict(identity))

    result = closure.rehearse_stale_client_rejection(authority_root=authority, runtime_root=runtime)
    assert result["old_protocol_explicit_upgrade_error"] is True
    assert result["active_state_unchanged"] is True
    assert result["cases"] == [
        {"case_id": "old-protocol", "error_code": "unsupported_protocol"},
        {"case_id": "wrong-extension", "error_code": "client_identity_mismatch"},
    ]
    assert result["production_mutation_performed"] is False


def test_interrupted_archive_rehearsal_fails_closed_on_missing_and_tampered_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime, legacy = _install_stubs(monkeypatch, tmp_path)
    captured = handoff.capture_side_by_side_handoff(
        runtime_root=runtime,
        legacy_runtime_root=legacy,
        observation_seconds=0,
        route_observer=lambda **_: _routes(),
    )
    authority = tmp_path / "authority"
    authority.mkdir()
    identity = {
        "bootstrap_state_sha256": SHA,
        "m9b_record_digest": "sha256:" + "c" * 64,
        "client_plan_sha256": "sha256:" + "d" * 64,
        "client_verification_sha256": "sha256:" + "e" * 64,
    }
    monkeypatch.setattr(closure, "_runtime_snapshot", lambda **_: dict(identity))

    result = closure.rehearse_interrupted_archive(
        authority_root=authority,
        runtime_root=runtime,
        freeze_digest=captured["report"]["freeze_digest"],
    )
    assert result["baseline_archive_readable"] is True
    assert result["missing_object_error_code"] == "evidence_missing"
    assert result["tampered_object_error_code"] in {"evidence_digest_mismatch", "evidence_invalid"}
    assert result["scratch_only"] is True
    assert result["active_state_unchanged"] is True


def test_read_only_soak_requires_stable_authority_identity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    authority = tmp_path / "authority"
    runtime = tmp_path / "runtime"
    legacy = tmp_path / "legacy"
    for path in (authority, runtime, legacy):
        path.mkdir()
    identity = {
        "bootstrap_state_sha256": SHA,
        "m9b_record_digest": "sha256:" + "c" * 64,
        "client_plan_sha256": "sha256:" + "d" * 64,
        "client_verification_sha256": "sha256:" + "e" * 64,
    }
    monkeypatch.setattr(closure, "_runtime_snapshot", lambda **_: dict(identity))
    monkeypatch.setattr(closure, "revalidate_side_by_side_digest", lambda **_: FREEZE)

    soak = closure.capture_read_only_soak(
        authority_root=authority,
        runtime_root=runtime,
        legacy_runtime_root=legacy,
        freeze_digest=FREEZE,
        iterations=5,
        interval_seconds=0,
    )
    assert soak["all_iterations_passed"] is True
    assert soak["identity_stable"] is True
    assert soak["iterations"] == 5
    assert soak["latency_ms"]["p95"] >= soak["latency_ms"]["p50"]
    assert soak["performance_threshold_applied"] is False


def test_read_only_soak_blocks_identity_drift(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    authority = tmp_path / "authority"
    runtime = tmp_path / "runtime"
    legacy = tmp_path / "legacy"
    for path in (authority, runtime, legacy):
        path.mkdir()
    counter = {"value": 0}

    def snapshot(**_: object) -> dict[str, str]:
        counter["value"] += 1
        return {
            "bootstrap_state_sha256": SHA if counter["value"] < 3 else "sha256:" + "f" * 64,
            "m9b_record_digest": "sha256:" + "c" * 64,
            "client_plan_sha256": "sha256:" + "d" * 64,
            "client_verification_sha256": "sha256:" + "e" * 64,
        }

    monkeypatch.setattr(closure, "_runtime_snapshot", snapshot)
    monkeypatch.setattr(closure, "revalidate_side_by_side_digest", lambda **_: FREEZE)
    with pytest.raises(closure.M12aClosureError) as caught:
        closure.capture_read_only_soak(
            authority_root=authority,
            runtime_root=runtime,
            legacy_runtime_root=legacy,
            freeze_digest=FREEZE,
            iterations=3,
            interval_seconds=0,
        )
    assert caught.value.code == "soak_identity_drift"


def test_full_closure_requires_every_proof_and_writes_immutable_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    authority = tmp_path / "authority"
    runtime = tmp_path / "runtime"
    legacy = tmp_path / "legacy"
    repo = tmp_path / "repo"
    for path in (authority, runtime, legacy, repo):
        path.mkdir()

    base_report = {
        "active_source_commit": "1" * 40,
        "m9a_freeze_digest": FREEZE,
    }
    monkeypatch.setattr(
        closure,
        "capture_compatibility_zero",
        lambda **_: {
            "status": "PASS_CLOSED",
            "report_sha256": "sha256:" + "1" * 64,
            "deletion_plan_sha256": "sha256:" + "2" * 64,
            "report": base_report,
        },
    )
    monkeypatch.setattr(
        closure,
        "inventory_compatibility_surfaces",
        lambda **_: {"inventory_complete": True, "unclassified_compatibility_paths": []},
    )
    monkeypatch.setattr(closure, "build_benchmark_basis", lambda **_: {"basis_complete": True})
    monkeypatch.setattr(
        closure,
        "rehearse_stale_client_rejection",
        lambda **_: {"old_protocol_explicit_upgrade_error": True, "active_state_unchanged": True},
    )
    monkeypatch.setattr(
        closure,
        "rehearse_interrupted_archive",
        lambda **_: {"baseline_archive_readable": True, "active_state_unchanged": True},
    )
    monkeypatch.setattr(
        closure,
        "capture_read_only_soak",
        lambda **_: {"all_iterations_passed": True, "identity_stable": True},
    )

    result = closure.capture_full_closure(
        authority_root=authority,
        runtime_root=runtime,
        legacy_runtime_root=legacy,
        repo_root=repo,
        observation_seconds=0,
        soak_iterations=3,
    )
    assert result["status"] == "PASS_CLOSED"
    assert result["report"]["m12b_unlocked"] is True
    assert result["report"]["unnamed_exceptions"] == []
    assert result["production_mutation_performed"] is False
    assert result["final_deletion_performed"] is False

    monkeypatch.setattr(
        closure,
        "verify_compatibility_zero",
        lambda **_: {"status": "PASS_CLOSED", "report": base_report},
    )
    verified = closure.verify_full_closure(
        runtime_root=runtime,
        closure_report_sha256=result["closure_report_sha256"],
    )
    assert verified["status"] == "PASS_CLOSED"
    assert verified["report"]["m12b_unlocked"] is True


def test_full_closure_operator_has_no_effect_surface() -> None:
    parser = closure._parser()
    actions = parser._subparsers._group_actions[0].choices
    assert set(actions) == {"capture", "verify"}
    forbidden = {
        "delete",
        "drop",
        "contract",
        "apply",
        "activate",
        "switch",
        "maintenance",
        "promote",
        "install",
        "register",
        "start",
        "stop",
        "disable",
    }
    assert forbidden.isdisjoint(actions)
    assert not any(name.startswith(tuple(forbidden)) for name in closure.__all__)

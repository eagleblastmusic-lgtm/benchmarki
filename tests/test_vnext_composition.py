from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from bdb_shared.evidence import canonical_json_bytes, semantic_digest
from bdb_vnext.composition import (
    BROWSER_COMPONENT_ID,
    BROWSER_EXTENSION_ID,
    BROWSER_PROVIDER_ID,
    COMPOSITION_PROVIDER_ID,
    COMPOSITION_SCHEMA,
    CONFIG_GENERATION,
    NATIVE_HOST_NAME,
    NATIVE_PROVIDER_ID,
    PROTOCOL_GENERATION,
    REPO_VIEW_COMPONENT_ID,
    REPO_VIEW_PROVIDER_ID,
    RUNTIME_ID,
    VNextCompositionError,
    build_vnext_composition_manifest,
    composition_status,
    load_browser_identity,
    load_vnext_composition_manifest,
    main,
    validate_vnext_composition_manifest,
)


BASIS = "4998aa16ff68d728637d09639ac79ced886393f6"
LEGACY_EXTENSION_ID = "b" * 32
ROOT = Path(__file__).resolve().parents[1]


def browser_bundle(root: Path, *, key: str | None = None) -> Path:
    identity = load_browser_identity()
    root.mkdir()
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "manifest_version": 3,
                "name": "Bartosz Dev Bridge vNext",
                "version": "0.1.0",
                "key": key if key is not None else identity["public_key_spki_der_base64"],
                "background": {"service_worker": "background.js"},
            }
        ),
        encoding="utf-8",
    )
    (root / "background.js").write_text("const generation = 'bdb-vnext-g1';\n", encoding="utf-8")
    return root


def build(tmp_path: Path, **updates: object) -> dict[str, object]:
    arguments: dict[str, object] = {
        "source_commit": BASIS,
        "runtime_root": tmp_path / "vnext",
        "legacy_runtime_root": tmp_path / "legacy",
        "forbidden_roots": [ROOT],
    }
    arguments.update(updates)
    return build_vnext_composition_manifest(**arguments)


def test_manifest_is_deterministic_isolated_and_has_no_side_effects(tmp_path: Path) -> None:
    before = set(tmp_path.iterdir())
    first = build(tmp_path)
    second = build(tmp_path)

    assert first == second
    assert set(tmp_path.iterdir()) == before
    assert first["schema"] == COMPOSITION_SCHEMA
    assert first["generation"] == {
        "generation_id": "bdb-vnext-g1",
        "runtime_id": "devmaster.bdb.vnext.runtime",
        "protocol_generation": PROTOCOL_GENERATION,
        "config_generation": CONFIG_GENERATION,
        "mode": "build_only",
        "writer_enabled": False,
    }
    assert first["activation"]["writer_enabled"] is False
    assert first["activation"]["manifest_is_activation_authority"] is False
    assert first["activation"]["blockers"] == ["explicit_activation_execution_unit_required"]
    assert first["legacy_boundary"]["vnext_access"] == "none"
    assert first["legacy_boundary"]["cutover_role"] == "read_only_archive"
    assert first["semantic_digest"] == semantic_digest(first)

    paths = first["paths"]
    assert paths["runtime_root"] != first["legacy_boundary"]["runtime_root"]
    assert Path(paths["control_store"]).is_relative_to(Path(paths["runtime_root"]))
    assert Path(paths["spool_inbox"]).is_relative_to(Path(paths["runtime_root"]))
    assert Path(paths["receipts_store"]).is_relative_to(Path(paths["runtime_root"]))
    assert Path(paths["instance_lock"]).is_relative_to(Path(paths["runtime_root"]))
    assert Path(paths["pid_file"]).is_relative_to(Path(paths["runtime_root"]))


def test_vnext_composition_import_does_not_load_legacy_package() -> None:
    check = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import bdb_vnext.composition; "
                "assert not any(name == 'bdb_bridge' or name.startswith('bdb_bridge.') "
                "for name in sys.modules)"
            ),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert check.returncode == 0, check.stderr


def test_provider_and_component_identities_are_explicit_and_disabled(tmp_path: Path) -> None:
    manifest = build(tmp_path)
    providers = manifest["composition"]["providers"]
    provider_ids = {provider["provider_id"] for provider in providers}

    assert manifest["composition"]["root_provider_id"] == COMPOSITION_PROVIDER_ID
    assert len(provider_ids) == len(providers)
    assert all(provider["writer_enabled"] is False for provider in providers)
    repo_view = next(provider for provider in providers if provider["provider_id"] == REPO_VIEW_PROVIDER_ID)
    assert repo_view["component_id"] == REPO_VIEW_COMPONENT_ID
    assert repo_view["kind"] == "repo_view"
    assert repo_view["state"] == "active_read_only"
    assert {
        provider["provider_id"]
        for provider in providers
        if provider["kind"] in {"browser_transport", "native_transport", "repo_view"}
    } == {BROWSER_PROVIDER_ID, NATIVE_PROVIDER_ID, REPO_VIEW_PROVIDER_ID}
    assert manifest["identities"]["browser_extension"]["component_id"] == BROWSER_COMPONENT_ID
    assert manifest["identities"]["browser_extension"]["identity_state"] == "bound"
    assert manifest["identities"]["browser_extension"]["extension_id"] == BROWSER_EXTENSION_ID
    assert manifest["identities"]["browser_extension"]["private_key_in_repository"] is False
    assert manifest["identities"]["native_host"]["host_name"] == NATIVE_HOST_NAME
    assert manifest["identities"]["native_host"]["registration_state"] == "unregistered"
    assert manifest["identities"]["protocol"]["legacy_compatible"] is False
    assert len(manifest["bundles"]) == 4
    assert all(
        bundle["state"] == "not_built"
        and bundle["path"] is None
        and bundle["sha256"] is None
        for bundle in manifest["bundles"]
    )


def test_bound_browser_identity_is_distinct_and_origin_exact(tmp_path: Path) -> None:
    manifest = build(tmp_path, legacy_extension_ids=[LEGACY_EXTENSION_ID])
    browser = manifest["identities"]["browser_extension"]

    assert browser["identity_state"] == "bound"
    assert browser["origin"] == f"chrome-extension://{BROWSER_EXTENSION_ID}/"
    assert "browser_extension_identity_unbound" not in manifest["activation"]["blockers"]

    with pytest.raises(VNextCompositionError, match="differ"):
        build(
            tmp_path,
            legacy_extension_ids=[BROWSER_EXTENSION_ID],
        )


def test_checked_in_browser_identity_is_self_consistent_and_public_only() -> None:
    identity = load_browser_identity()

    assert identity["extension_id"] == BROWSER_EXTENSION_ID
    assert identity["private_key_in_repository"] is False
    assert identity["public_key_sha256"].startswith("sha256:")
    assert identity["semantic_digest"] == semantic_digest(identity)


def test_browser_and_native_bundle_digests_are_deterministic_and_read_only(tmp_path: Path) -> None:
    browser = browser_bundle(tmp_path / "browser")
    native = tmp_path / "bdb-vnext-native-host.exe"
    native.write_bytes(b"synthetic native host bundle")
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (browser / "manifest.json", browser / "background.js", native)
    }

    first = build(
        tmp_path,
        bundle_paths={BROWSER_COMPONENT_ID: browser, "devmaster.bdb.vnext.native-host": native},
    )
    second = build(
        tmp_path,
        bundle_paths={BROWSER_COMPONENT_ID: browser, "devmaster.bdb.vnext.native-host": native},
    )

    assert first == second
    observed = {bundle["component_id"]: bundle for bundle in first["bundles"]}
    assert observed[BROWSER_COMPONENT_ID]["state"] == "observed"
    assert observed[BROWSER_COMPONENT_ID]["kind"] == "directory"
    assert observed[BROWSER_COMPONENT_ID]["file_count"] == 2
    assert observed["devmaster.bdb.vnext.native-host"]["kind"] == "file"
    assert observed[RUNTIME_ID]["state"] == "not_built"
    assert all(
        path.read_bytes() == content and path.stat().st_mtime_ns == mtime
        for path, (content, mtime) in before.items()
    )


def test_browser_packaging_smoke_rejects_wrong_key(tmp_path: Path) -> None:
    browser = browser_bundle(tmp_path / "browser", key="not-the-vnext-key")

    with pytest.raises(VNextCompositionError) as failure:
        build(tmp_path, bundle_paths={BROWSER_COMPONENT_ID: browser})

    assert failure.value.code == "browser_bundle_identity_mismatch"


def test_bundle_tamper_is_visible_as_composition_mismatch(tmp_path: Path) -> None:
    browser = browser_bundle(tmp_path / "browser")
    expected = build(tmp_path, bundle_paths={BROWSER_COMPONENT_ID: browser})
    (browser / "background.js").write_text("const generation = 'tampered';\n", encoding="utf-8")
    observed = build(tmp_path, bundle_paths={BROWSER_COMPONENT_ID: browser})

    status = composition_status(expected, observed)

    assert status["result"] == "MISMATCH"
    assert status["blockers"] == [
        {"code": "composition_digest_mismatch", "field": "semantic_digest"}
    ]


def test_bundle_observation_rejects_legacy_and_overlapping_paths(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    legacy_bundle = legacy / "native.exe"
    legacy_bundle.write_bytes(b"legacy")
    with pytest.raises(VNextCompositionError) as legacy_failure:
        build(
            tmp_path,
            legacy_runtime_root=legacy,
            bundle_paths={"devmaster.bdb.vnext.native-host": legacy_bundle},
        )
    assert legacy_failure.value.code == "legacy_bundle_overlap"

    shared = browser_bundle(tmp_path / "shared")
    with pytest.raises(VNextCompositionError) as overlap_failure:
        build(
            tmp_path,
            bundle_paths={
                BROWSER_COMPONENT_ID: shared,
                "devmaster.bdb.vnext.native-host": shared,
            },
        )
    assert overlap_failure.value.code == "bundle_path_overlap"


def test_bundle_observation_rejects_symlinked_root(tmp_path: Path) -> None:
    browser = browser_bundle(tmp_path / "browser")
    link = tmp_path / "browser-link"
    try:
        link.symlink_to(browser, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable in this Windows test context")

    with pytest.raises(VNextCompositionError) as failure:
        build(tmp_path, bundle_paths={BROWSER_COMPONENT_ID: link})

    assert failure.value.code == "bundle_reparse_point"


@pytest.mark.parametrize("runtime_suffix, legacy_suffix", [("same", "same"), ("legacy/child", "legacy")])
def test_legacy_runtime_overlap_fails_closed(
    tmp_path: Path,
    runtime_suffix: str,
    legacy_suffix: str,
) -> None:
    with pytest.raises(VNextCompositionError) as failure:
        build_vnext_composition_manifest(
            source_commit=BASIS,
            runtime_root=tmp_path / runtime_suffix,
            legacy_runtime_root=tmp_path / legacy_suffix,
        )

    assert failure.value.code == "legacy_runtime_overlap"


def test_source_or_foreign_state_overlap_fails_closed(tmp_path: Path) -> None:
    runtime = tmp_path / "vnext"
    with pytest.raises(VNextCompositionError) as failure:
        build_vnext_composition_manifest(
            source_commit=BASIS,
            runtime_root=runtime,
            legacy_runtime_root=tmp_path / "legacy",
            forbidden_roots=[runtime / "source"],
        )

    assert failure.value.code == "foreign_state_overlap"


def test_status_reports_exact_match_missing_and_digest_mismatch(tmp_path: Path) -> None:
    expected = build(tmp_path)
    matched = composition_status(expected, deepcopy(expected))
    missing = composition_status(expected, None)
    other = build_vnext_composition_manifest(
        source_commit="a" * 40,
        runtime_root=tmp_path / "vnext",
        legacy_runtime_root=tmp_path / "legacy",
    )
    mismatched = composition_status(expected, other)

    assert matched["result"] == "MATCH"
    assert matched["compatible"] is True
    assert matched["activation_ready"] is False
    assert missing["blockers"] == [{"code": "manifest_missing", "field": "observed"}]
    assert mismatched["blockers"] == [
        {"code": "composition_digest_mismatch", "field": "semantic_digest"}
    ]


def test_tampered_generation_and_digest_are_rejected(tmp_path: Path) -> None:
    manifest = build(tmp_path)
    manifest["generation"]["writer_enabled"] = True
    manifest["semantic_digest"] = semantic_digest(manifest)

    with pytest.raises(VNextCompositionError) as failure:
        validate_vnext_composition_manifest(manifest)

    assert failure.value.code == "generation_mismatch"


def test_unknown_manifest_fields_fail_closed(tmp_path: Path) -> None:
    manifest = build(tmp_path)
    manifest["unexpected"] = True
    manifest["semantic_digest"] = semantic_digest(manifest)

    with pytest.raises(VNextCompositionError) as failure:
        validate_vnext_composition_manifest(manifest)

    assert failure.value.code == "invalid_manifest_fields"


def test_load_is_bounded_and_validates_digest(tmp_path: Path) -> None:
    manifest = build(tmp_path)
    path = tmp_path / "manifest.json"
    path.write_bytes(canonical_json_bytes(manifest))

    assert load_vnext_composition_manifest(path) == manifest

    manifest["semantic_digest"] = "sha256:" + "0" * 64
    path.write_bytes(canonical_json_bytes(manifest))
    with pytest.raises(VNextCompositionError) as failure:
        load_vnext_composition_manifest(path)
    assert failure.value.code == "digest_mismatch"


def test_cli_exports_sanitized_manifest_without_creating_runtime_state(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    runtime = tmp_path / "vnext"
    legacy = tmp_path / "legacy"
    browser = browser_bundle(tmp_path / "browser")
    code = main(
        [
            "--source-commit",
            BASIS,
            "--runtime-root",
            str(runtime),
            "--legacy-runtime-root",
            str(legacy),
            "--bundle",
            f"{BROWSER_COMPONENT_ID}={browser}",
            "--sanitized",
        ]
    )
    output = json.loads(capfd.readouterr().out)

    assert code == 0
    assert output["representation"] == "SANITIZED"
    assert str(runtime) not in json.dumps(output)
    assert str(browser) not in json.dumps(output)
    assert any(bundle["state"] == "observed" for bundle in output["bundles"])
    assert not runtime.exists()
    assert not legacy.exists()


def test_checked_in_schema_tracks_manifest_contract() -> None:
    schema = json.loads(
        (ROOT / "schemas" / "bdb-vnext-composition-v1.schema.json").read_text(encoding="utf-8")
    )

    assert schema["$id"] == COMPOSITION_SCHEMA
    assert schema["properties"]["generation"]["properties"]["protocol_generation"]["const"] == PROTOCOL_GENERATION
    assert schema["properties"]["identities"]["properties"]["native_host"]["properties"]["host_name"]["const"] == NATIVE_HOST_NAME
    browser_schema = schema["properties"]["identities"]["properties"]["browser_extension"]
    assert browser_schema["properties"]["extension_id"]["const"] == BROWSER_EXTENSION_ID
    assert schema["properties"]["bundles"]["minItems"] == 4
    assert 'bdb_vnext = ["browser_identity.json"]' in (ROOT / "pyproject.toml").read_text(
        encoding="utf-8"
    )

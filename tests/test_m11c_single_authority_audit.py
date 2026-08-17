from __future__ import annotations

import ast
from pathlib import Path

from bdb_vnext.composition import BROWSER_EXTENSION_ID, NATIVE_HOST_NAME


ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _public_functions(path: str) -> set[str]:
    tree = ast.parse(_source(path), filename=path)
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_")
    }


def test_m9b_has_no_public_product_activation_writer() -> None:
    public = _public_functions("bdb_vnext/m9b_activation.py")
    assert "activate" not in public
    assert "finalize_interrupted_activation" not in public
    source = _source("bdb_vnext/m9b_activation.py")
    assert "M11c alone" in source
    assert "subordinate" in source


def test_m11a_admin_still_has_no_activate_or_switch_command() -> None:
    source = _source("bdb_vnext/m11a_bootstrap_admin.py")
    assert 'add_parser("activate"' not in source
    assert 'add_parser("switch"' not in source
    assert "activation_deferred_to" in source


def test_m11c_is_the_only_exported_product_cutover_surface() -> None:
    source = _source("bdb_vnext/m11c_cutover.py")
    assert "def apply_windows_cutover(" in source
    assert '"apply_windows_cutover"' in source
    assert 'M11C_ACTIVATION_AUTHORITY = "m11c-external-bootstrap"' in source
    assert "def _apply_cutover(" in source
    assert '"_apply_cutover"' not in source.split("__all__ =", 1)[1]


def test_cutover_cli_requires_exact_plan_sha_and_has_no_force_shortcut() -> None:
    source = _source("bdb_vnext/m11c_cutover_cli.py")
    assert '"--approve-plan-sha256"' in source
    assert "required=True" in source
    assert '"--yes"' not in source
    assert '"--force"' not in source
    assert "expected_plan_sha256=args.approve_plan_sha256" in source


def test_native_admission_requires_external_bootstrap_and_target_identity() -> None:
    source = _source("bdb_vnext/m9b_native_host.py")
    assert "require_bootstrap_active" in source
    assert "canonical_intake_disabled" in source
    assert "bootstrap_authority_root" in source
    assert "NATIVE_HOST_NAME" in source
    assert "BROWSER_EXTENSION_ID" in source
    assert NATIVE_HOST_NAME == "com.bartosz.dev_bridge.vnext"
    assert "legacy_fallback" in source
    assert '"legacy_fallback": False' in source


def test_browser_bundle_uses_only_dedicated_vnext_native_route() -> None:
    worker = _source("browser_extension_vnext/transport_worker.js")
    manifest = _source("browser_extension_vnext/manifest.json")
    assert f'const NATIVE_HOST_NAME = "{NATIVE_HOST_NAME}"' in worker
    assert "com.bartosz.dev_bridge\"" not in worker
    assert "connectNative(NATIVE_HOST_NAME)" in worker
    assert '"nativeMessaging"' in manifest
    assert BROWSER_EXTENSION_ID == "mopnolkjddkmgojfjkenjobehhmmklll"


def test_postcutover_slot_state_has_one_external_authority_and_no_candidate_pointer() -> None:
    schema = _source("schemas/bdb-vnext-bootstrap-slot-state-v2.schema.json")
    assert '"activation_authority": {"const": "m11c-external-bootstrap"}' in schema
    assert '"candidate_manifest_sha256": {"type": "null"}' in schema
    assert '"production_activation_performed": {"const": true}' in schema
    assert '"rollback_mode": {"const": "ROLL_FORWARD_ONLY"}' in schema

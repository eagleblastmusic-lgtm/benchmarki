from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_m2b_json_schemas_are_strict_and_cross_reference_exactly() -> None:
    fragment = json.loads(
        (ROOT / "schemas" / "bdb-vnext-context-fragment-v1.schema.json").read_text(encoding="utf-8")
    )
    transport = json.loads(
        (ROOT / "schemas" / "bdb-vnext-transport-envelope-v1.schema.json").read_text(encoding="utf-8")
    )
    assert fragment["additionalProperties"] is False
    assert transport["additionalProperties"] is False
    assert fragment["properties"]["schema"]["const"] == "bdb-vnext-context-fragment-v1"
    assert transport["properties"]["schema"]["const"] == "bdb-vnext-transport-envelope-v1"
    assert transport["properties"]["fragment"]["$ref"] == "bdb-vnext-context-fragment-v1.schema.json"
    assert transport["properties"]["payload_length_bytes"]["maximum"] == 1048576
    assert set(fragment["properties"]) == set(fragment["required"])
    assert set(transport["properties"]) == set(transport["required"])


def test_m2b_modules_are_legacy_free_and_import_side_effect_free() -> None:
    for relative in ("bdb_vnext/content_store.py", "bdb_vnext/context_transport.py"):
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        imported = {
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported.update(
            node.module.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        )
        assert "bdb_bridge" not in imported
        assert "bdb_legacy" not in imported
        assert "bdb_vnext.x2_typed_content_experiment" not in imported

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import bdb_vnext.content_store; import bdb_vnext.context_transport; "
                "assert not any(name == 'bdb_bridge' or name.startswith('bdb_bridge.') for name in sys.modules); "
                "assert not any(name.startswith('bdb_vnext.x1_sqlite_experiment') or "
                "name.startswith('bdb_vnext.x2_typed_content_experiment') for name in sys.modules)"
            ),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr

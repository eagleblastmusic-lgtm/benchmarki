from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from bdb_vnext.engineering_intelligence import (
    CONTEXT_PACKAGE_SCHEMA,
    CONTEXT_REQUEST_SCHEMA,
    CONTEXT_RESOLUTION_SCHEMA,
    ENGINEERING_DECISION_SCHEMA,
    UNDERSTANDING_SCHEMA,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_FILES = {
    UNDERSTANDING_SCHEMA: "bdb-vnext-repository-understanding-v1.schema.json",
    CONTEXT_PACKAGE_SCHEMA: "bdb-vnext-context-package-v1.schema.json",
    CONTEXT_REQUEST_SCHEMA: "bdb-vnext-context-request-v1.schema.json",
    CONTEXT_RESOLUTION_SCHEMA: "bdb-vnext-context-resolution-v1.schema.json",
    ENGINEERING_DECISION_SCHEMA: "bdb-vnext-engineering-decision-v1.schema.json",
}


def test_m2c_schemas_parse_strictly_and_use_exact_top_level_contracts() -> None:
    for schema_name, filename in SCHEMA_FILES.items():
        document = json.loads((ROOT / "schemas" / filename).read_text(encoding="utf-8"))
        assert document["additionalProperties"] is False
        assert document["properties"]["schema"]["const"] == schema_name
        assert set(document["required"]) == set(document["properties"])


def test_m2c_module_is_legacy_free_and_import_side_effect_free() -> None:
    source = (ROOT / "bdb_vnext" / "engineering_intelligence.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    imported.update(
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    assert "bdb_bridge" not in imported
    assert "bdb_legacy" not in imported
    assert "sqlite3" not in imported
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; import sys; before=set(Path.cwd().iterdir()); "
                "import bdb_vnext.engineering_intelligence; "
                "assert set(Path.cwd().iterdir()) == before; "
                "assert not any(name.startswith('bdb_bridge') for name in sys.modules); "
                "assert not any(name.startswith('bdb_legacy') for name in sys.modules); "
                "assert 'bdb_vnext.provider_root' not in sys.modules; "
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


@pytest.mark.parametrize("filename", SCHEMA_FILES.values())
def test_m2c_schema_bytes_are_stable_json(filename: str) -> None:
    document = json.loads((ROOT / "schemas" / filename).read_text(encoding="utf-8"))
    assert json.loads(json.dumps(document, ensure_ascii=False, sort_keys=True)) == document

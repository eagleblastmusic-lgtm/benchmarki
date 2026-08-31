from __future__ import annotations

import ast
import json
import subprocess
import tomllib
from collections import Counter, defaultdict, deque
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOTS = {
    "bdb_bridge",
    "bdb_operator",
    "bdb_gui",
    "bdb_release",
    "bdb_bartosz_os",
    "bdb_shared",
    "bdb_vnext",
    "bdb_integrations",
    "bdb_poc",
}


def tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, check=True, capture_output=True, text=True
    )
    return [line.strip().replace("\\", "/") for line in result.stdout.splitlines() if line.strip()]


def module_name(path: str) -> str | None:
    p = Path(path)
    if p.suffix != ".py" or not p.parts or p.parts[0] not in RUNTIME_ROOTS:
        return None
    parts = list(p.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def resolve_from(current: str, level: int, target: str | None) -> str:
    if level == 0:
        return target or ""
    package = current.split(".")[:-1]
    keep = max(0, len(package) - level + 1)
    package = package[:keep]
    if target:
        package.extend(target.split("."))
    return ".".join(package)


files = tracked_files()
module_to_file = {
    name: path for path in files if (name := module_name(path)) is not None
}
internal_modules = set(module_to_file)
imports: dict[str, set[str]] = defaultdict(set)
syntax_errors: dict[str, str] = {}
symbol_counts: dict[str, dict[str, int]] = {}
call_sites: list[dict[str, object]] = []
state_enums: list[dict[str, object]] = []
literal_sql_mutations: list[dict[str, object]] = []


def dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        left = dotted_name(node.value)
        return f"{left}.{node.attr}" if left else node.attr
    return ""


interesting_call_suffixes = (
    "open",
    "write_text",
    "write_bytes",
    "replace",
    "rename",
    "unlink",
    "mkdir",
    "touch",
    "rmtree",
    "remove",
    "copy",
    "copy2",
    "move",
    "connect",
    "Popen",
    "run",
    "check_call",
    "check_output",
    "CreateKey",
    "SetValueEx",
    "DeleteKey",
    "DeleteValue",
    "CreateFileW",
    "MoveFileExW",
    "ReplaceFileW",
    "fsync",
    "flock",
    "send",
    "sendall",
    "recv",
)


for module, path in sorted(module_to_file.items()):
    text = (ROOT / path).read_text(encoding="utf-8")
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError as exc:
        syntax_errors[path] = str(exc)
        continue
    symbol_counts[path] = {
        "functions": sum(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) for n in ast.walk(tree)),
        "classes": sum(isinstance(n, ast.ClassDef) for n in ast.walk(tree)),
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                candidate = alias.name
                for internal in internal_modules:
                    if candidate == internal or candidate.startswith(internal + ".") or internal.startswith(candidate + "."):
                        imports[module].add(internal if candidate.startswith(internal + ".") else candidate)
        elif isinstance(node, ast.ImportFrom):
            base = resolve_from(module, node.level, node.module)
            if base in internal_modules:
                imports[module].add(base)
            for alias in node.names:
                candidate = f"{base}.{alias.name}" if base else alias.name
                if candidate in internal_modules:
                    imports[module].add(candidate)
        elif isinstance(node, ast.Call):
            name = dotted_name(node.func)
            if any(name == suffix or name.endswith("." + suffix) for suffix in interesting_call_suffixes):
                call_sites.append({"path": path, "line": node.lineno, "call": name})
        elif isinstance(node, ast.ClassDef):
            bases = {dotted_name(base) for base in node.bases}
            if {"Enum", "StrEnum", "enum.Enum", "enum.StrEnum"} & bases:
                members = [n.targets[0].id for n in node.body if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)]
                state_enums.append({"path": path, "line": node.lineno, "class": node.name, "members": members})
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            normalized = " ".join(node.value.strip().upper().split())
            if normalized.startswith(("INSERT ", "UPDATE ", "DELETE ", "REPLACE ", "CREATE ", "ALTER ", "DROP ")):
                literal_sql_mutations.append({"path": path, "line": node.lineno, "sql_prefix": normalized[:100]})


pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
console_scripts = pyproject["project"]["scripts"]
entry_modules = sorted({value.split(":", 1)[0] for value in console_scripts.values()})
reachable: set[str] = set()
queue = deque(entry_modules)
while queue:
    module = queue.popleft()
    if module in reachable:
        continue
    reachable.add(module)
    for dependency in imports.get(module, ()):
        if dependency not in reachable:
            queue.append(dependency)


tests = [path for path in files if path.startswith("tests/") and path.endswith(".py")]
test_functions = 0
test_imports: dict[str, set[str]] = defaultdict(set)
for path in tests:
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"), filename=path)
    test_functions += sum(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
        for node in ast.walk(tree)
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        for name in names:
            if name == "bdb_vnext" or name.startswith("bdb_vnext."):
                test_imports[path].add(name)


call_counter = Counter(item["call"] for item in call_sites)
output = {
    "tracked_files": len(files),
    "tracked_python_files": sum(path.endswith(".py") for path in files),
    "runtime_python_modules": len(internal_modules),
    "runtime_modules_by_root": dict(sorted(Counter(name.split(".")[0] for name in internal_modules).items())),
    "bdb_vnext_python_modules": sum(name == "bdb_vnext" or name.startswith("bdb_vnext.") for name in internal_modules),
    "console_script_count": len(console_scripts),
    "console_scripts": console_scripts,
    "console_entry_modules": entry_modules,
    "console_reachable_internal_module_count": len(reachable),
    "console_reachable_internal_modules": sorted(reachable),
    "bdb_vnext_console_reachable_count": sum(name == "bdb_vnext" or name.startswith("bdb_vnext.") for name in reachable),
    "bdb_vnext_not_console_reachable": sorted(name for name in internal_modules if (name == "bdb_vnext" or name.startswith("bdb_vnext.")) and name not in reachable),
    "internal_import_edges": sum(len(edges) for edges in imports.values()),
    "symbol_totals": {
        "functions": sum(item["functions"] for item in symbol_counts.values()),
        "classes": sum(item["classes"] for item in symbol_counts.values()),
    },
    "state_enum_count": len(state_enums),
    "state_enums": state_enums,
    "interesting_call_site_count": len(call_sites),
    "interesting_call_counts": dict(call_counter.most_common()),
    "interesting_call_sites": call_sites,
    "literal_sql_mutation_count": len(literal_sql_mutations),
    "literal_sql_mutations": literal_sql_mutations,
    "test_python_files": len(tests),
    "test_function_count": test_functions,
    "test_files_importing_bdb_vnext_count": len(test_imports),
    "test_files_importing_bdb_vnext": sorted(test_imports),
    "syntax_errors": syntax_errors,
}
print(json.dumps(output, indent=2, sort_keys=True))

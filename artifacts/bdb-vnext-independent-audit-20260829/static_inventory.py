"""Read-only static inventory used by the independent BDB vNext audit.

The script parses tracked source without importing project modules or writing
project/runtime state. It prints a JSON summary to stdout.
"""

from __future__ import annotations

import ast
import collections
import json
import pathlib
import re
import subprocess
import tomllib


ROOT = pathlib.Path(__file__).resolve().parents[2]


def tracked_files() -> list[str]:
    raw = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
    return [item.decode("utf-8") for item in raw.split(b"\0") if item]


def module_name(path: str) -> str | None:
    candidate = pathlib.PurePosixPath(path)
    if candidate.suffix != ".py":
        return None
    parts = list(candidate.with_suffix("").parts)
    if not parts or parts[0] in {"tests", "scripts", "spikes", "packaging"}:
        return None
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) if parts else None


def imported_modules(tree: ast.AST, current: str) -> set[str]:
    result: set[str] = set()
    package = current.split(".")[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: max(0, len(package) - node.level + 1)]
                suffix = node.module.split(".") if node.module else []
                resolved = ".".join(base + suffix)
            else:
                resolved = node.module or ""
            if resolved:
                result.add(resolved)
                result.update(f"{resolved}.{alias.name}" for alias in node.names if alias.name != "*")
    return result


def dotted_call(node: ast.Call) -> str:
    parts: list[str] = []
    current: ast.AST = node.func
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def main() -> None:
    files = tracked_files()
    top_level = collections.Counter(path.split("/", 1)[0] for path in files)
    extensions = collections.Counter(pathlib.PurePosixPath(path).suffix.lower() or "<none>" for path in files)

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = pyproject["project"]["scripts"]
    console_roots = sorted({value.split(":", 1)[0] for value in scripts.values()})

    modules: dict[str, str] = {}
    imports: dict[str, set[str]] = {}
    parse_errors: dict[str, str] = {}
    main_guard_paths: list[str] = []
    function_count = 0
    class_count = 0
    test_function_count = 0
    calls_by_leaf: collections.Counter[str] = collections.Counter()
    calls_by_full: collections.Counter[str] = collections.Counter()
    call_paths: dict[str, set[str]] = collections.defaultdict(set)
    constant_gate_assignments: list[dict[str, object]] = []
    loc = 0

    interesting_leaves = {
        "write_text", "write_bytes", "open", "replace", "rename", "unlink", "rmdir",
        "mkdir", "makedirs", "remove", "rmtree", "move", "copy", "copy2", "copytree",
        "fsync", "flush", "commit", "rollback", "execute", "executemany", "connect",
        "Popen", "run", "check_call", "check_output", "kill", "terminate", "startfile",
        "CreateKey", "SetValueEx", "DeleteKey", "DeleteValue", "OpenKey", "send", "sendall",
        "recv", "bind", "listen", "accept", "request", "urlopen", "chmod", "stat",
    }
    gate_name = re.compile(r"(?:pass|passed|status|verdict|qualified|accepted|evidence|gate|success|ok)$", re.I)

    for path in files:
        if not path.endswith(".py"):
            continue
        text = (ROOT / path).read_text(encoding="utf-8")
        loc += len(text.splitlines())
        try:
            tree = ast.parse(text, filename=path)
        except SyntaxError as exc:
            parse_errors[path] = f"{exc.msg}:{exc.lineno}"
            continue
        module = module_name(path)
        if module:
            modules[module] = path
            imports[module] = imported_modules(tree, module)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                function_count += 1
                if path.startswith("tests/") and node.name.startswith("test_"):
                    test_function_count += 1
            elif isinstance(node, ast.ClassDef):
                class_count += 1
            elif isinstance(node, ast.Call):
                full = dotted_call(node)
                if full:
                    leaf = full.rsplit(".", 1)[-1]
                    calls_by_leaf[leaf] += 1
                    if leaf in interesting_leaves and not path.startswith("tests/"):
                        calls_by_full[full] += 1
                        call_paths[full].add(path)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if isinstance(value, ast.Constant) and isinstance(value.value, (bool, str, int, float)):
                    for target in targets:
                        if (
                            isinstance(target, ast.Name)
                            and gate_name.search(target.id)
                            and not path.startswith("tests/")
                        ):
                            constant_gate_assignments.append({
                                "path": path,
                                "line": node.lineno,
                                "name": target.id,
                                "value": value.value,
                            })
        if re.search(r"if\s+__name__\s*==\s*['\"]__main__['\"]", text):
            main_guard_paths.append(path)

    internal_names = set(modules)
    graph: dict[str, set[str]] = {}
    for module, imported in imports.items():
        resolved: set[str] = set()
        for candidate in imported:
            probe = candidate
            while probe:
                if probe in internal_names:
                    resolved.add(probe)
                    break
                probe = probe.rpartition(".")[0]
        graph[module] = resolved

    def closure(roots: list[str]) -> set[str]:
        reachable: set[str] = set()
        stack = list(roots)
        while stack:
            module = stack.pop()
            if module in reachable or module not in modules:
                continue
            reachable.add(module)
            stack.extend(graph.get(module, ()))
        return reachable

    reachable = closure(console_roots)
    per_root = {root: sorted(closure([root])) for root in console_roots}

    test_files = [path for path in files if path.startswith("tests/") and path.endswith(".py")]
    source_py = [path for path in files if path.endswith(".py") and not path.startswith("tests/")]
    schema_files = [path for path in files if path.startswith("schemas/") and path.endswith(".json")]
    workflow_files = [path for path in files if path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml"))]
    runtime_files = [path for path in files if path.startswith("runtime/")]

    output = {
        "tracked_files": len(files),
        "top_level_counts": dict(top_level.most_common()),
        "extension_counts": dict(extensions.most_common()),
        "python": {
            "files": sum(1 for path in files if path.endswith(".py")),
            "source_files": len(source_py),
            "test_files": len(test_files),
            "loc": loc,
            "functions": function_count,
            "classes": class_count,
            "test_functions": test_function_count,
            "parse_errors": parse_errors,
            "importable_internal_modules": len(modules),
            "console_entrypoint_names": len(scripts),
            "console_entrypoint_modules": console_roots,
            "console_reachable_modules": len(reachable),
            "console_reachable_by_package": dict(collections.Counter(name.split(".", 1)[0] for name in reachable)),
            "console_reachable_modules_list": sorted(reachable),
            "console_unreachable_by_package": dict(collections.Counter(name.split(".", 1)[0] for name in internal_names - reachable)),
            "per_console_root_reachability": {root: {"count": len(items), "modules": items} for root, items in per_root.items()},
            "main_guard_paths": sorted(main_guard_paths),
        },
        "schemas": len(schema_files),
        "workflows": len(workflow_files),
        "tracked_runtime_files": len(runtime_files),
        "interesting_calls": [
            {"call": call, "count": count, "paths": sorted(call_paths[call])[:20], "path_count": len(call_paths[call])}
            for call, count in calls_by_full.most_common()
        ],
        "constant_gate_assignment_candidates": constant_gate_assignments,
    }
    print(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

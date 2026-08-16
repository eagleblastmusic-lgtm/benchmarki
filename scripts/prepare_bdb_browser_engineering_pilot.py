#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bdb_vnext.n6_rehearsal import prepare_package, write_manual_packet


ENGINEERING_PREFIX = "BDB-P1-ENGINEERING"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare one exact build-only BDB Browser engineering package without authoring the engineering edit."
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--legacy-runtime-root", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--target-repo-root", required=True)
    parser.add_argument("--target-repository-id", default="bdb-browser-engineering-target")
    parser.add_argument("--target-commit", required=True)
    parser.add_argument("--target-branch")
    parser.add_argument("--allowed-path", action="append", required=True)
    parser.add_argument("--checker-id", required=True)
    parser.add_argument("--checker-version", default="1")
    parser.add_argument("--checker-argv-json", required=True)
    parser.add_argument("--checker-cwd", default=".")
    parser.add_argument("--checker-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--task-file", required=True)
    parser.add_argument("--prompt-output")
    parser.add_argument("--python")
    return parser


def _read_task(path: Path) -> str:
    if not path.is_file():
        raise SystemExit(f"task-file does not exist: {path}")
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text.startswith(ENGINEERING_PREFIX + "\n"):
        raise SystemExit(f"task-file must start with exact prefix {ENGINEERING_PREFIX!r} followed by a newline")
    return text


def _checker_argv(raw: str) -> list[str]:
    try:
        value: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"checker-argv-json is invalid JSON: {exc}") from exc
    if not isinstance(value, list) or not value or any(not isinstance(item, str) or not item for item in value):
        raise SystemExit("checker-argv-json must be a non-empty JSON array of non-empty strings")
    return value


def _require_fresh_output(path: Path) -> None:
    if path.exists() and any(path.iterdir()):
        raise SystemExit(f"refusing to reuse non-empty package output: {path}")


def main() -> int:
    args = _parser().parse_args()
    output = Path(args.output).expanduser().absolute()
    runtime_root = Path(args.runtime_root).expanduser().absolute()
    legacy_runtime_root = Path(args.legacy_runtime_root).expanduser().absolute()
    target_repo_root = Path(args.target_repo_root).expanduser().absolute()
    task_file = Path(args.task_file).expanduser().absolute()
    prompt_output = (
        Path(args.prompt_output).expanduser().absolute()
        if args.prompt_output
        else output.parent / "ENGINEERING_PROMPT.txt"
    )

    _require_fresh_output(output)
    task = _read_task(task_file)
    checker_argv = _checker_argv(args.checker_argv_json)

    engineering_target: dict[str, Any] = {
        "repository_id": args.target_repository_id,
        "repo_root": str(target_repo_root),
        "commit": args.target_commit,
        "allowed_paths": list(dict.fromkeys(args.allowed_path)),
        "checker": {
            "checker_id": args.checker_id,
            "checker_version": args.checker_version,
            "argv": checker_argv,
            "cwd": args.checker_cwd,
            "timeout_seconds": args.checker_timeout_seconds,
        },
    }
    if args.target_branch:
        engineering_target["branch"] = args.target_branch

    execution = prepare_package(
        repo_root=ROOT,
        output=output,
        runtime_root=runtime_root,
        legacy_runtime_root=legacy_runtime_root,
        source_commit=args.source_commit,
        python_executable=args.python,
        engineering_target=engineering_target,
    )
    packet = write_manual_packet(execution, output / "MANUAL_BROWSER_REHEARSAL_PACKET.md")
    prompt_output.parent.mkdir(parents=True, exist_ok=True)
    prompt_output.write_text(task + "\n", encoding="utf-8")

    package = execution["package"]
    target = package["engineering_target"]
    subject = execution["subject"]
    result = {
        "schema": "bdb-browser-engineering-package-prep-v1",
        "status": "READY",
        "package_root": str(output),
        "package_digest": package["digest"],
        "source_commit": subject["commit"],
        "source_tree": subject["tree"],
        "source_view_id": subject["view_id"],
        "target_repository_id": target["repository_id"],
        "target_commit": target["commit"],
        "target_tree": target["tree"],
        "target_view_id": target["view_id"],
        "allowed_paths": target["allowed_paths"],
        "checker": target["checker"],
        "browser_extension_id": package["browser_extension"]["extension_id"],
        "browser_native_binding_digest": package["browser_native_binding"]["binding_digest"],
        "native_host_manifest": package["native_host"]["manifest"],
        "native_host_switch_script": package["native_host"].get("switch_registration_script"),
        "native_executable_ready": package["native_host"]["executable_ready"],
        "prompt_file": str(prompt_output),
        "manual_packet": str(packet),
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

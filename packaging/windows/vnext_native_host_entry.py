from __future__ import annotations

import os
import sys
from pathlib import Path

from bdb_vnext.m9b_native_host import main


def _staged_runtime_config(executable: Path) -> Path | None:
    """Return the config structurally bound to a staged Native executable."""

    resolved = executable.resolve()
    if resolved.parent.name != "native-host" or resolved.parent.parent.name != "clients":
        return None
    runtime = resolved.parent.parent.parent
    config = runtime / "config" / "native-host.json"
    return config if config.is_file() else None


def _chrome_argv() -> list[str]:
    """Bind a Chrome-launched frozen host to the canonical staged config.

    Chrome Native Messaging supplies the caller origin and may supply
    --parent-window on Windows, but it does not know BDB's --config argument.
    The frozen host therefore injects the canonical per-user vNext config only
    when an explicit config was not already supplied by a bounded diagnostic.
    """

    argv = list(sys.argv[1:])
    if any(value == "--config" or value.startswith("--config=") for value in argv):
        return argv
    staged = _staged_runtime_config(Path(sys.executable))
    if staged is not None:
        return [*argv, "--config", str(staged)]
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return argv
    config = Path(local_app_data) / "BartoszDevBridge-Next" / "runtime" / "config" / "native-host.json"
    return [*argv, "--config", str(config)]


if __name__ == "__main__":
    raise SystemExit(main(_chrome_argv()))

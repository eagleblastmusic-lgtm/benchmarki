from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bdb_vnext.m9a_blocker_probe_compat import probe_profile


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the source-compatible read-only M9a legacy blocker probe"
    )
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--bridge-config", required=True)
    parser.add_argument("--native-config", required=True)
    parser.add_argument("--scratch-dir", required=True)
    parser.add_argument("--json-out")
    parser.add_argument("--max-records", type=int, default=500)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        result = probe_profile(
            profile_id=args.profile_id,
            bridge_config_path=args.bridge_config,
            native_config_path=args.native_config,
            scratch_dir=args.scratch_dir,
            max_records=args.max_records,
        ).as_dict()
    except Exception as exc:
        sys.stderr.write(
            f"M9a compatibility blocker probe failed: {type(exc).__name__}: {exc}\n"
        )
        return 2

    rendered = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.json_out:
        destination = Path(args.json_out).expanduser().resolve(strict=False)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

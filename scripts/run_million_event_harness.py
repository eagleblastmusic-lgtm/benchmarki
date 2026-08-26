"""NX-018: Million-Event Synthetic Qualification Harness Script.

Executes exactly 1,000,000 synthetic events through the NX-018 segmented audit storage,
sealing segments, performing compaction with cryptographic proofs, and measuring
integrity, loss, duplicates, and logical digest parity.
Binds output explicitly to the executing source HEAD, tree, and implementation digests.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from bdb_vnext.retention_compaction import (
    compute_million_event_artifact_digest,
    run_million_event_synthetic_harness,
)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    report_path = repo_root / "runtime" / "retention" / "million_event_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)

    # Gather source binding
    try:
        head_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        tree_sha = subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        status_out = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        worktree_clean = len(status_out) == 0
    except Exception:
        head_sha = "unknown"
        tree_sha = "unknown"
        worktree_clean = False

    script_path = Path(__file__).resolve()
    harness_script_sha = _sha256_file(script_path)
    nx018_impl_path = repo_root / "bdb_vnext" / "retention_compaction.py"
    nx018_impl_sha = _sha256_file(nx018_impl_path)

    print("Starting 1,000,000 synthetic events run...")
    conn = sqlite3.connect(":memory:")
    results = run_million_event_synthetic_harness(
        conn, project_id="p-million-qual", total_events=1_000_000, batch_size=50_000
    )
    conn.close()

    report_payload = {
        "REPORT_SCHEMA_VERSION": "1.0.0",
        "SOURCE_HEAD": head_sha,
        "SOURCE_TREE": tree_sha,
        "WORKTREE_CLEAN": worktree_clean,
        "HARNESS_SCRIPT_SHA256": harness_script_sha,
        "NX018_IMPLEMENTATION_SHA256": nx018_impl_sha,
        "SYNTHETIC_EVENTS_REQUESTED": results["synthetic_events_requested"],
        "SYNTHETIC_EVENTS_ACCOUNTED": results["synthetic_events_accounted"],
        "LOST_EVENTS": results["lost_events"],
        "DUPLICATE_EVENTS": results["duplicate_events"],
        "CHAIN_VALID": results["chain_valid"],
        "INITIAL_LOGICAL_DIGEST": results["initial_logical_digest"],
        "FINAL_LOGICAL_DIGEST": results["final_logical_digest"],
        "LOGICAL_DIGEST_PARITY": results["logical_digest_parity"],
        "ELAPSED_SECONDS": results["elapsed_seconds"],
        "SEGMENT_COUNT": results["segment_count"],
        "EVENTS_PER_SECOND": results["events_per_second"],
    }

    report_payload["MILLION_EVENT_ARTIFACT_DIGEST"] = compute_million_event_artifact_digest(
        report_payload
    )

    report_path.write_text(json.dumps(report_payload, indent=2), encoding="utf-8")
    print(
        f"Million-event run completed in {results['elapsed_seconds']}s. Report saved to: {report_path}"
    )
    print(json.dumps(report_payload, indent=2))


if __name__ == "__main__":
    main()

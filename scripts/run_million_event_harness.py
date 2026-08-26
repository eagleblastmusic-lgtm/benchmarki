"""NX-018: Million-Event Synthetic Qualification Harness Script.

Executes exactly 1,000,000 synthetic events through the NX-018 segmented audit storage,
sealing segments, performing compaction with cryptographic proofs, and measuring
integrity, loss, duplicates, and logical digest parity.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from bdb_vnext.retention_compaction import run_million_event_synthetic_harness


def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    report_path = repo_root / "runtime" / "retention" / "million_event_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)

    print("Starting 1,000,000 synthetic events run...")
    conn = sqlite3.connect(":memory:")
    results = run_million_event_synthetic_harness(conn, project_id="p-million-qual", total_events=1_000_000, batch_size=50_000)
    conn.close()

    report_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"Million-event run completed in {results['elapsed_seconds']}s. Report saved to: {report_path}")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()

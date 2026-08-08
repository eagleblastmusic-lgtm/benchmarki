from __future__ import annotations

import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension"


class AutoPendingMutationSafetyRuntimeTests(unittest.TestCase):
    def test_ambiguous_mutation_results_fail_closed(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required")

        with tempfile.TemporaryDirectory() as temporary:
            harness = Path(temporary) / "auto-pending-mutation-safety.cjs"
            harness.write_text(
                textwrap.dedent(
                    r"""
                    "use strict";
                    const assert = require("node:assert/strict");
                    const fs = require("node:fs");
                    const path = require("node:path");
                    const vm = require("node:vm");

                    const extensionDir = process.argv[2];
                    let nextDecision = null;
                    let reconciliationCalls = 0;

                    const context = {
                      console,
                      Set,
                      Array,
                      Object,
                      String,
                      Boolean,
                      Error,
                      automationMetadata(action) {
                        const automation = action && action.automation;
                        return automation
                          ? {
                              loopId: automation.loop_id,
                              iteration: automation.iteration
                            }
                          : null;
                      },
                      bdbAutoMutationIsUnsafeToReplay(action) {
                        return [
                          "multi_file_patch",
                          "replace_exact_and_test"
                        ].includes(action && action.operation);
                      },
                      async bdbAutoRecordMutationReconciliation(
                        action,
                        tabId,
                        metadata,
                        error
                      ) {
                        reconciliationCalls += 1;
                        return {
                          executed: true,
                          stopReason: "manual_reconciliation_required",
                          uncertainExecution: true,
                          operation: action.operation,
                          tabId,
                          metadata,
                          detail: error.message
                        };
                      },
                      async considerAuto() {
                        return nextDecision;
                      }
                    };
                    context.globalThis = context;
                    vm.createContext(context);

                    const scriptPath = path.join(
                      extensionDir,
                      "background_auto_pending_mutation_safety.js"
                    );
                    vm.runInContext(
                      fs.readFileSync(scriptPath, "utf8"),
                      context,
                      { filename: scriptPath }
                    );

                    function action(operation, loopId) {
                      return {
                        schema: "bdb-action-v1",
                        repo_alias: "bdb-self",
                        operation,
                        automation: {
                          mode: "auto",
                          loop_id: loopId,
                          iteration: 1
                        }
                      };
                    }

                    async function run() {
                      nextDecision = {
                        executed: true,
                        response: {
                          status: "pending",
                          async_poll_exhausted: true,
                          command_id: "pending-session:000001"
                        }
                      };
                      const pending = await context.considerAuto(
                        action("multi_file_patch", "pending-mutation"),
                        10
                      );
                      assert.equal(
                        pending.stopReason,
                        "manual_reconciliation_required"
                      );
                      assert.equal(pending.uncertainExecution, true);
                      assert.match(
                        pending.detail,
                        /async_poll_exhausted=true/
                      );
                      assert.equal(reconciliationCalls, 1);

                      nextDecision = {
                        executed: true,
                        response: {
                          status: "failed",
                          error: { code: "internal_error" }
                        }
                      };
                      const internal = await context.considerAuto(
                        action("replace_exact_and_test", "internal-mutation"),
                        11
                      );
                      assert.equal(
                        internal.stopReason,
                        "manual_reconciliation_required"
                      );
                      assert.match(internal.detail, /error=internal_error/);
                      assert.equal(reconciliationCalls, 2);

                      nextDecision = {
                        executed: true,
                        response: {
                          status: "failed",
                          error: { code: "timeout" }
                        }
                      };
                      const timeout = await context.considerAuto(
                        action("multi_file_patch", "timeout-mutation"),
                        12
                      );
                      assert.equal(
                        timeout.stopReason,
                        "manual_reconciliation_required"
                      );
                      assert.equal(reconciliationCalls, 3);

                      nextDecision = {
                        executed: true,
                        response: {
                          status: "failed",
                          error: { code: "invalid_payload" }
                        }
                      };
                      const invalid = await context.considerAuto(
                        action("multi_file_patch", "invalid-mutation"),
                        13
                      );
                      assert.equal(
                        invalid.response.error.code,
                        "invalid_payload"
                      );
                      assert.equal(reconciliationCalls, 3);

                      nextDecision = {
                        executed: true,
                        response: {
                          status: "pending",
                          async_poll_exhausted: true
                        }
                      };
                      const read = await context.considerAuto(
                        action("open_read", "pending-read"),
                        14
                      );
                      assert.equal(read.response.status, "pending");
                      assert.equal(reconciliationCalls, 3);
                    }

                    run().catch((error) => {
                      console.error(error && error.stack ? error.stack : error);
                      process.exitCode = 1;
                    });
                    """
                ),
                encoding="utf-8",
            )

            completed = subprocess.run(
                [node, str(harness), str(EXTENSION)],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(
                completed.returncode,
                0,
                completed.stdout + completed.stderr,
            )

    def test_pending_safety_loads_before_task_controller(self) -> None:
        entry = (EXTENSION / "background_full_entry.js").read_text(
            encoding="utf-8"
        )
        self.assertLess(
            entry.index('"background_auto_pending_mutation_safety.js"'),
            entry.index('"background_task_controller.js"'),
        )


if __name__ == "__main__":
    unittest.main()

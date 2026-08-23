from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension"


def test_browser_auto_is_unlimited_within_one_milestone_and_stops_at_completion(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the browser service-worker runtime contract")

    harness = tmp_path / "auto-milestone-runtime.cjs"
    harness.write_text(
        textwrap.dedent(
            r'''
            "use strict";

            const assert = require("node:assert/strict");
            const fs = require("node:fs");
            const path = require("node:path");
            const vm = require("node:vm");

            const extensionDir = process.argv[2];
            const manifest = JSON.parse(fs.readFileSync(path.join(extensionDir, "manifest.json"), "utf8"));

            function storageArea(store) {
              return {
                async get(keys) {
                  if (keys === null || keys === undefined) return { ...store };
                  if (typeof keys === "string") return Object.prototype.hasOwnProperty.call(store, keys) ? { [keys]: store[keys] } : {};
                  const result = Array.isArray(keys) ? {} : { ...keys };
                  for (const key of (Array.isArray(keys) ? keys : Object.keys(keys))) {
                    if (Object.prototype.hasOwnProperty.call(store, key)) result[key] = store[key];
                  }
                  return result;
                },
                async set(values) { Object.assign(store, values); },
                async remove(keys) { for (const key of Array.isArray(keys) ? keys : [keys]) delete store[key]; }
              };
            }

            function createWorker(shared) {
              let messageListener = null;
              const DateCtor = Date;
              DateCtor.now = () => shared.now;
              const context = {
                console,
                TextEncoder,
                Uint8Array,
                Date: DateCtor,
                setTimeout,
                clearTimeout,
                crypto: { getRandomValues(buffer) { buffer.fill(7); return buffer; } },
                chrome: {
                  storage: { local: storageArea(shared.local), session: storageArea(shared.session) },
                  runtime: {
                    lastError: null,
                    onMessage: { addListener(listener) { messageListener = listener; } },
                    sendNativeMessage(_host, request, callback) {
                      shared.nativeRequests.push(request);
                      if (request.action === "context") {
                        callback({ schema: "bdb-native-response-v1", request_id: request.request_id, context: { source_changes: [], latest_promotion: null }, arm: { armed: true } });
                        return;
                      }
                      if (request.action === "submit_action") {
                        const action = request.bdb_action;
                        const taskId = action.automation.task_id;
                        const blockedTask = taskId === "blocked";
                        const finalTask = taskId === "t3";
                        shared.commandCounter += 1;
                        callback({
                          schema: "bdb-native-response-v1",
                          request_id: request.request_id,
                          command_id: `command-${shared.commandCounter}`,
                          status: "completed",
                          result: {
                            status: "success",
                            acceptance: { status: "passed" },
                            milestone: blockedTask
                              ? { milestone_id: "m1", milestone_run_id: "run-blocked", status: "gate_required", completed_tasks: 0, total_tasks: 3, runnable_task_ids: [], blocker: { id: "G0", kind: "gate", status: "required" } }
                              : finalTask
                              ? { milestone_id: "m1", milestone_run_id: "run-1", status: "completed", completed_tasks: 3, total_tasks: 3, runnable_task_ids: [] }
                              : { milestone_id: "m1", milestone_run_id: "run-1", status: "running", next_task_id: taskId === "t1" ? "t2" : "t3", completed_tasks: taskId === "t1" ? 1 : 2, total_tasks: 3, runnable_task_ids: [taskId === "t1" ? "t2" : "t3"] },
                            data: { operation: action.operation }
                          }
                        });
                        return;
                      }
                      throw new Error(`Unexpected native action: ${request.action}`);
                    }
                  }
                }
              };
              context.globalThis = context;
              context.self = context;
              vm.createContext(context);
              context.importScripts = (...names) => names.forEach((name) => vm.runInContext(fs.readFileSync(path.join(extensionDir, name), "utf8"), context, { filename: name }));
              const workerPath = path.join(extensionDir, manifest.background.service_worker);
              vm.runInContext(fs.readFileSync(workerPath, "utf8"), context, { filename: workerPath });
              assert.equal(typeof messageListener, "function");
              function dispatch(message, tabId) {
                return new Promise((resolve, reject) => {
                  const keepOpen = messageListener(message, { tab: { id: tabId } }, (reply) => reply && reply.ok === true ? resolve(reply.response) : reject(new Error(reply && reply.error || "AUTO failed")));
                  assert.equal(keepOpen, true);
                });
              }
              return {
                send(action, tabId) { return dispatch({ type: "BDB_CONSIDER_AUTO", action }, tabId); },
                mark(loopId, iteration, tabId) { return dispatch({ type: "BDB_MARK_AUTO_RESULT_DELIVERED", loopId, iteration }, tabId); }
              };
            }

            function action(loopId, iteration, taskId, milestoneRunId = "run-1") {
              return {
                schema: "bdb-action-v1",
                repo_alias: "fixture",
                operation: "open_read",
                payload: { path: "README.md" },
                automation: { mode: "auto", loop_id: loopId, iteration, milestone_run_id: milestoneRunId, milestone_id: "m1", task_id: taskId },
                presentation: { mode: "compact" }
              };
            }

            async function main() {
              const shared = { local: { autoEnabled: true, autoMaxIterations: 1, autoMaxMinutes: 1 }, session: {}, nativeRequests: [], commandCounter: 0, now: 1 };
              const worker = createWorker(shared);
              const loop = "milestone-loop";
              const first = await worker.send(action(loop, 1, "t1"), 11);
              assert.equal(first.shouldContinue, true, JSON.stringify(first));
              assert.equal(first.taskCompleted, true, JSON.stringify(first));
              await worker.mark(loop, 1, 11);
              shared.now = 10 ** 15;
              const second = await worker.send(action(loop, 2, "t2"), 12);
              assert.equal(second.shouldContinue, true, JSON.stringify(second));
              await worker.mark(loop, 2, 12);
              const third = await worker.send(action(loop, 3, "t3"), 13);
              assert.equal(third.shouldContinue, false, JSON.stringify(third));
              assert.equal(third.taskCompleted, true, JSON.stringify(third));
              assert.equal(third.stopReason, "milestone_completed", JSON.stringify(third));
              assert.equal(third.state.status, "milestone_completed", JSON.stringify(third));
              await worker.mark(loop, 3, 13);
              const duplicate = await worker.send(action(loop, 3, "t3"), 14);
              assert.equal(duplicate.reason, "iteration_already_processed", JSON.stringify(duplicate));

              const blocked = await worker.send(action("blocked-loop", 1, "blocked"), 15);
              assert.equal(blocked.shouldContinue, false, JSON.stringify(blocked));
              assert.equal(blocked.stopReason, "gate_required", JSON.stringify(blocked));
              assert.equal(blocked.state.status, "gate_required", JSON.stringify(blocked));

              const longLoop = "unbounded-loop";
              for (let iteration = 1; iteration <= 31; iteration += 1) {
                const decision = await worker.send(action(longLoop, iteration, "t1"), 21);
                assert.equal(decision.executed, true, JSON.stringify(decision));
                assert.equal(decision.shouldContinue, true, JSON.stringify(decision));
                await worker.mark(longLoop, iteration, 21);
              }
              assert.equal(shared.nativeRequests.filter((request) => request.action === "submit_action").length >= 34, true);
            }

            main().catch((error) => { console.error(error && error.stack ? error.stack : error); process.exitCode = 1; });
            '''
        ),
        encoding="utf-8",
    )
    completed = subprocess.run([node, str(harness), str(EXTENSION)], check=False, capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stdout + completed.stderr

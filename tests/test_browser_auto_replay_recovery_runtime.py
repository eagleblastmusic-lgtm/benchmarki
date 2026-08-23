from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension"


def test_auto_replay_claim_recovers_after_failure_and_deduplicates_in_flight(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the browser service-worker runtime contract")

    harness = tmp_path / "auto-replay-recovery-runtime.cjs"
    harness.write_text(
        textwrap.dedent(
            r"""
            "use strict";

            const assert = require("node:assert/strict");
            const fs = require("node:fs");
            const path = require("node:path");
            const vm = require("node:vm");

            const extensionDir = process.argv[2];
            const localStore = {
              autoEnabled: true,
              autoMaxIterations: 6,
              autoMaxMinutes: 15
            };
            const sessionStore = {};
            let messageListener = null;
            let nativeMode = "success";
            let deferredNativeCallback = null;

            function clone(value) {
              return JSON.parse(JSON.stringify(value));
            }

            function standardGet(store, keys) {
              if (keys === null || keys === undefined) return clone(store);
              if (typeof keys === "string") {
                return Object.prototype.hasOwnProperty.call(store, keys)
                  ? { [keys]: clone(store[keys]) }
                  : {};
              }
              if (Array.isArray(keys)) {
                const result = {};
                for (const key of keys) {
                  if (Object.prototype.hasOwnProperty.call(store, key)) {
                    result[key] = clone(store[key]);
                  }
                }
                return result;
              }
              const result = clone(keys);
              for (const key of Object.keys(keys)) {
                if (Object.prototype.hasOwnProperty.call(store, key)) {
                  result[key] = clone(store[key]);
                }
              }
              return result;
            }

            function storageArea(store) {
              return {
                async get(keys) {
                  return standardGet(store, keys);
                },
                async set(values) {
                  Object.assign(store, clone(values));
                },
                async remove(keys) {
                  for (const key of Array.isArray(keys) ? keys : [keys]) {
                    delete store[key];
                  }
                }
              };
            }

            function completedNativeResponse(request, commandId) {
              return {
                schema: "bdb-native-response-v1",
                request_id: request.request_id,
                command_id: commandId,
                status: "completed",
                result: {
                  status: "success",
                  data: { operation: request.bdb_action.operation }
                }
              };
            }

            const context = {
              console,
              TextEncoder,
              Uint8Array,
              setTimeout,
              clearTimeout,
              crypto: {
                getRandomValues(buffer) {
                  for (let index = 0; index < buffer.length; index += 1) {
                    buffer[index] = index + 1;
                  }
                  return buffer;
                }
              },
              chrome: {
                storage: {
                  local: storageArea(localStore),
                  session: storageArea(sessionStore)
                },
                runtime: {
                  lastError: null,
                  onMessage: {
                    addListener(listener) {
                      messageListener = listener;
                    }
                  },
                  sendNativeMessage(_host, request, callback) {
                    assert.equal(request.action, "submit_action");
                    if (nativeMode === "fail") {
                      context.chrome.runtime.lastError = { message: "simulated native failure" };
                      callback(undefined);
                      context.chrome.runtime.lastError = null;
                      return;
                    }
                    if (nativeMode === "deferred") {
                      assert.equal(deferredNativeCallback, null);
                      deferredNativeCallback = () => callback(
                        completedNativeResponse(request, "command-concurrent-1")
                      );
                      return;
                    }
                    callback(completedNativeResponse(request, "command-recovery-6"));
                  }
                }
              }
            };
            context.globalThis = context;
            context.self = context;
            vm.createContext(context);
            context.importScripts = (...scriptNames) => {
              for (const scriptName of scriptNames) {
                const scriptPath = path.join(extensionDir, scriptName);
                vm.runInContext(fs.readFileSync(scriptPath, "utf8"), context, {
                  filename: scriptPath
                });
              }
            };

            const entryPath = path.join(extensionDir, "background_full_entry.js");
            vm.runInContext(fs.readFileSync(entryPath, "utf8"), context, {
              filename: entryPath
            });
            assert.equal(typeof messageListener, "function");
            assert.equal(typeof context.considerAuto, "function");

            function action(loopId, iteration) {
              return {
                schema: "bdb-action-v1",
                repo_alias: "calculator",
                operation: "open_read",
                payload: { path: "calculator.py" },
                automation: {
                  mode: "auto",
                  loop_id: loopId,
                  iteration
                },
                presentation: { mode: "compact" }
              };
            }

            async function run() {
              const recoveryLoop = "auto-replay-recovery";
              const recoveryStateKey = `bdbAuto:${recoveryLoop}`;
              const recoveryReplayKey = `${recoveryLoop}:6`;
              sessionStore[recoveryStateKey] = {
                startedAt: Date.now(),
                lastIteration: 5,
                status: "running",
                updatedAt: Date.now()
              };

              nativeMode = "fail";
              await assert.rejects(
                context.considerAuto(action(recoveryLoop, 6), 101),
                /simulated native failure/
              );
              const guardAfterFailure = localStore.bdbAutoReplayGuard || {};
              assert.equal(
                Object.prototype.hasOwnProperty.call(guardAfterFailure, recoveryReplayKey),
                false,
                JSON.stringify(guardAfterFailure)
              );
              assert.equal(sessionStore[recoveryStateKey].lastIteration, 5);

              nativeMode = "success";
              const recovered = await context.considerAuto(action(recoveryLoop, 6), 101);
              assert.equal(recovered.executed, true, JSON.stringify(recovered));
              assert.equal(recovered.shouldContinue, true, JSON.stringify(recovered));
              assert.equal(sessionStore[recoveryStateKey].lastIteration, 6);
              assert.equal(
                localStore.bdbAutoReplayGuard[recoveryReplayKey].status,
                "completed"
              );

              assert.equal(
                (
                  await context.markAutoResultDelivered(
                    recoveryLoop,
                    6,
                    101
                  )
                ).marked,
                true
              );

              const duplicateCompleted = await context.considerAuto(
                action(recoveryLoop, 6),
                101
              );
              assert.equal(duplicateCompleted.executed, false);
              assert.equal(duplicateCompleted.reason, "iteration_already_processed");
              assert.equal(duplicateCompleted.expectedIteration, 7);

              const concurrentLoop = "auto-replay-concurrent";
              const concurrentStateKey = `bdbAuto:${concurrentLoop}`;
              sessionStore[concurrentStateKey] = {
                startedAt: Date.now(),
                lastIteration: 0,
                status: "running",
                updatedAt: Date.now()
              };

              nativeMode = "deferred";
              const first = context.considerAuto(action(concurrentLoop, 1), 202);
              for (let attempt = 0; attempt < 20 && deferredNativeCallback === null; attempt += 1) {
                await new Promise((resolve) => setTimeout(resolve, 0));
              }
              assert.equal(typeof deferredNativeCallback, "function");

              const duplicateInFlight = await context.considerAuto(
                action(concurrentLoop, 1),
                202
              );
              assert.equal(duplicateInFlight.executed, false);
              assert.equal(duplicateInFlight.reason, "iteration_in_progress");
              assert.equal(duplicateInFlight.expectedIteration, 1);

              deferredNativeCallback();
              deferredNativeCallback = null;
              const firstCompleted = await first;
              assert.equal(firstCompleted.executed, true, JSON.stringify(firstCompleted));
              assert.equal(sessionStore[concurrentStateKey].lastIteration, 1);

              assert.equal(
                (
                  await context.markAutoResultDelivered(
                    concurrentLoop,
                    1,
                    202
                  )
                ).marked,
                true
              );

              const duplicateAfterCompletion = await context.considerAuto(
                action(concurrentLoop, 1),
                202
              );
              assert.equal(duplicateAfterCompletion.executed, false);
              assert.equal(duplicateAfterCompletion.reason, "iteration_already_processed");
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
    assert completed.returncode == 0, completed.stdout + completed.stderr

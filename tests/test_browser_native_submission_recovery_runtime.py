from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension"


def test_background_exposes_submission_nonce_lookup_runtime_message() -> None:
    source = (EXTENSION / "background.js").read_text(encoding="utf-8")
    assert 'case "BDB_LOOKUP_SUBMISSION_NONCE":' in source
    assert 'action: "lookup_submission_nonce"' in source
    assert 'client_submission_nonce: clientSubmissionNonce' in source
    assert 'requestId("submission-nonce-lookup")' in source


def test_submission_retries_same_receipted_request_after_internal_error(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for Native submission recovery validation")
    harness = tmp_path / "native-submission-recovery.cjs"
    harness.write_text(
        textwrap.dedent(
            r'''
            "use strict";
            const assert = require("node:assert/strict");
            const fs = require("node:fs");
            const vm = require("node:vm");
            const { webcrypto, randomUUID } = require("node:crypto");
            const listeners = [];
            const posts = [];
            const context = {
              console, TextEncoder, Uint8Array, Set, Map, Date, JSON, Promise,
              setTimeout, clearTimeout,
              crypto: {
                subtle: webcrypto.subtle,
                getRandomValues: webcrypto.getRandomValues.bind(webcrypto),
                randomUUID
              },
              chrome: {
                storage: {
                  local: { async get() { return {}; }, async set() {} },
                  session: { async get() { return {}; }, async set() {}, async remove() {} }
                },
                runtime: {
                  lastError: null,
                  getManifest() { return { version: "0.4.7" }; },
                  onMessage: { addListener() {} },
                  connectNative() {
                    return {
                      onMessage: { addListener(listener) { listeners.push(listener); } },
                      onDisconnect: { addListener() {} },
                      postMessage(request) {
                        posts.push(JSON.parse(JSON.stringify(request)));
                        const response = posts.length === 1
                          ? {
                              schema: "bdb-native-response-v1",
                              host_version: "0.4.7",
                              request_id: request.request_id,
                              status: "failed",
                              error: { code: "internal_error" }
                            }
                          : {
                              schema: "bdb-native-response-v1",
                              host_version: "0.4.7",
                              request_id: request.request_id,
                              status: "completed",
                              command_id: `${request.bdb_action.session_id}:000001`,
                              request_recovered: true,
                              result: { status: "success", data: { operation: "open_read" } }
                            };
                        Promise.resolve().then(() => listeners[0](response));
                      }
                    };
                  },
                  sendNativeMessage() { throw new Error("one-shot fallback must not run"); }
                }
              }
            };
            context.globalThis = context;
            vm.createContext(context);
            vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), context);
            context.submitAction({
              schema: "bdb-action-v1",
              repo_alias: "gicleeapp",
              operation: "open_read",
              payload: { path: "cursor-api/Komponenty/example.py" }
            }, 7).then((response) => {
              assert.equal(response.status, "completed");
              assert.equal(response.request_recovered, true);
              assert.equal(posts.length, 2);
              assert.equal(posts[0].request_id, posts[1].request_id);
              assert.equal(posts[0].bdb_action.session_id, posts[1].bdb_action.session_id);
              assert.equal(posts[0].client_version, "0.4.7");
            }).catch((error) => { console.error(error); process.exitCode = 1; });
            '''
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [node, str(harness), str(EXTENSION / "background.js")],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_auto_loop_stays_running_after_bounded_internal_error_recovery(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for AUTO internal-error recovery validation")
    harness = tmp_path / "auto-internal-error-continuation.cjs"
    harness.write_text(
        textwrap.dedent(
            r'''
            "use strict";
            const assert = require("node:assert/strict");
            const fs = require("node:fs");
            const vm = require("node:vm");
            const { webcrypto, randomUUID } = require("node:crypto");
            const local = { autoEnabled: true, autoMaxIterations: 4, autoMaxMinutes: 10 };
            const session = {};
            const nativeRequests = [];

            function area(store) {
              return {
                async get(keys) {
                  if (keys === null) return { ...store };
                  if (typeof keys === "string") {
                    return Object.prototype.hasOwnProperty.call(store, keys)
                      ? { [keys]: store[keys] }
                      : {};
                  }
                  const result = {};
                  for (const key of keys || []) {
                    if (Object.prototype.hasOwnProperty.call(store, key)) result[key] = store[key];
                  }
                  return result;
                },
                async set(values) { Object.assign(store, values); },
                async remove(keys) {
                  for (const key of Array.isArray(keys) ? keys : [keys]) delete store[key];
                }
              };
            }

            const context = {
              console, TextEncoder, Uint8Array, Set, Map, Date, JSON, Promise,
              setTimeout, clearTimeout,
              crypto: {
                subtle: webcrypto.subtle,
                getRandomValues: webcrypto.getRandomValues.bind(webcrypto),
                randomUUID
              },
              chrome: {
                storage: { local: area(local), session: area(session) },
                runtime: {
                  lastError: null,
                  getManifest() { return { version: "0.4.7" }; },
                  onMessage: { addListener() {} },
                      sendNativeMessage(_host, request, callback) {
                        nativeRequests.push(JSON.parse(JSON.stringify(request)));
                        if (request.action === "context") {
                          callback({
                            schema: "bdb-native-response-v1",
                            host_version: "0.4.7",
                            request_id: request.request_id,
                            status: "context",
                            context: { allowed_paths: ["**"] },
                            arm: { armed: true }
                          });
                          return;
                        }
                        callback({
                      schema: "bdb-native-response-v1",
                      host_version: "0.4.7",
                      request_id: request.request_id,
                      status: "failed",
                      error: { code: "internal_error" }
                    });
                  }
                }
              }
            };
            context.globalThis = context;
            vm.createContext(context);
            vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), context);

            const action = {
              schema: "bdb-action-v1",
              repo_alias: "gicleeapp",
              operation: "open_read",
              payload: { path: "sections/section.liquid" },
              automation: {
                mode: "auto",
                loop_id: "recover-internal-error",
                iteration: 1,
                continue_on_failure: false
              }
            };

            context.considerAuto(action, 7).then((decision) => {
              assert.equal(decision.executed, true, JSON.stringify(decision));
              assert.equal(decision.recoverableNativeError, true, JSON.stringify(decision));
              assert.equal(decision.shouldContinue, true, JSON.stringify(decision));
              assert.equal(decision.stopReason, null, JSON.stringify(decision));
              assert.equal(decision.state.status, "running", JSON.stringify(decision));
                  assert.equal(nativeRequests.length, 3);
                  assert.equal(nativeRequests[1].request_id, nativeRequests[2].request_id);
            }).catch((error) => { console.error(error); process.exitCode = 1; });
            '''
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [node, str(harness), str(EXTENSION / "background.js")],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension"


def test_browser_repair_correlation_runtime(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for repair-correlation runtime validation")

    harness = tmp_path / "repair-correlation-runtime.cjs"
    harness.write_text(
        textwrap.dedent(
            r'''
            "use strict";

            const assert = require("node:assert/strict");
            const fs = require("node:fs");
            const path = require("node:path");
            const vm = require("node:vm");
            const { webcrypto } = require("node:crypto");

            const extensionDir = process.argv[2];
            const localStore = {};
            const sessionStore = {};
            const nativeRequests = [];
            const uuids = [
              "11111111-1111-4111-8111-111111111111",
              "22222222-2222-4222-8222-222222222222",
              "33333333-3333-4333-8333-333333333333",
              "44444444-4444-4444-8444-444444444444"
            ];

            function clone(value) {
              return JSON.parse(JSON.stringify(value));
            }

            function storageArea(store) {
              return {
                async get(keys) {
                  if (keys === null || keys === undefined) return clone(store);
                  if (typeof keys === "string") {
                    return Object.prototype.hasOwnProperty.call(store, keys)
                      ? { [keys]: clone(store[keys]) }
                      : {};
                  }
                  return {};
                },
                async set(values) { Object.assign(store, clone(values)); },
                async remove(keys) {
                  for (const key of Array.isArray(keys) ? keys : [keys]) delete store[key];
                }
              };
            }

            const crypto = {
              subtle: webcrypto.subtle,
              getRandomValues(buffer) { return webcrypto.getRandomValues(buffer); },
              randomUUID() {
                const value = uuids.shift();
                if (!value) throw new Error("UUID fixture exhausted");
                return value;
              }
            };

            const context = {
              console,
              TextEncoder,
              Uint8Array,
              Set,
              Map,
              Date,
              JSON,
              Promise,
              setTimeout,
              clearTimeout,
              crypto,
              atob(value) { return Buffer.from(value, "base64").toString("binary"); },
              btoa(value) { return Buffer.from(value, "binary").toString("base64"); },
              chrome: {
                storage: {
                  local: storageArea(localStore),
                  session: storageArea(sessionStore)
                },
                runtime: {
                  lastError: null,
                  onMessage: { addListener() {} },
                  sendNativeMessage(_host, request, callback) {
                    nativeRequests.push(clone(request));
                    if (request.action === "context") {
                      callback({
                        schema: "bdb-native-response-v1",
                        request_id: request.request_id,
                        status: "context",
                        context: { allowed_paths: ["src/**"] },
                        arm: { armed: true }
                      });
                      return;
                    }
                    if (request.action === "submit_action") {
                      const action = request.bdb_action;
                      callback({
                        schema: "bdb-native-response-v1",
                        request_id: request.request_id,
                        status: "completed",
                        command_id: `${action.session_id}:000001`,
                        result: {
                          status: "policy_denied",
                          command_id: `${action.session_id}:000001`,
                          data: {
                            terminal: "needs_user",
                            terminal_detail: "synthetic failure"
                          }
                        }
                      });
                      return;
                    }
                    throw new Error(`unexpected request ${request.action}`);
                  }
                }
              }
            };
            context.globalThis = context;
            context.self = context;
            vm.createContext(context);

            for (const scriptName of [
              "background.js",
              "background_action_preflight.js",
              "background_repair_correlation.js"
            ]) {
              const scriptPath = path.join(extensionDir, scriptName);
              vm.runInContext(fs.readFileSync(scriptPath, "utf8"), context, { filename: scriptPath });
            }
            vm.runInContext("globalThis.__submit = submitAction;", context);

            async function digest(content) {
              const bytes = new TextEncoder().encode(content);
              const hash = await webcrypto.subtle.digest("SHA-256", bytes);
              return `sha256:${Buffer.from(hash).toString("hex")}`;
            }

            function action(content, contentSha) {
              return {
                schema: "bdb-action-v1",
                repo_alias: "synthetic",
                operation: "multi_file_patch",
                sequence: 1,
                expected_revision: 0,
                payload: {
                  profile_id: "poc_pytest",
                  patch: {
                    schema: "bdb-multi-file-patch-v1",
                    operations: [{
                      schema: "bdb-file-replacement-v1",
                      kind: "replace_file",
                      path: "src/app.py",
                      expected_sha256: `sha256:${"0".repeat(64)}`,
                      content_base64: Buffer.from(content, "utf8").toString("base64"),
                      content_sha256: contentSha
                    }]
                  }
                }
              };
            }

            function repairState() {
              return localStore.bdbRepairActionsV1["7:synthetic"];
            }

            function peekAction() {
              return {
                schema: "bdb-action-v1",
                operation: "repair_state_peek",
                repo_alias: "synthetic"
              };
            }

            async function run() {
              const content = "print('ok')\n";
              await assert.rejects(
                () => context.__submit(action(content, `sha256:${"f".repeat(64)}`), 7),
                /content_sha256 mismatch/
              );
              assert.equal(
                nativeRequests.filter((request) => request.action === "submit_action").length,
                0,
                "preflight failure reached Native Host"
              );

              const failedPreflight = repairState();
              assert.equal(failedPreflight.action.session_id, "11111111-1111-4111-8111-111111111111");
              assert.deepEqual(failedPreflight.action.repair_correlation, {
                schema: "bdb-repair-correlation-v1",
                correlation_id: "22222222-2222-4222-8222-222222222222",
                role: "initial",
                predecessor_session_id: null
              });

              const ownPeek = await context.__submit(peekAction(), 7);
              assert.equal(ownPeek.status, "repair_state");
              assert.equal(ownPeek.key, "7:synthetic");
              assert.equal(ownPeek.entry.tab_id, 7);
              const foreignPeek = await context.__submit(peekAction(), 8);
              assert.equal(foreignPeek.status, "repair_state_empty");
              assert.equal(foreignPeek.entry, null);

              failedPreflight.awaiting_corrected_action = true;

              await context.__submit(action(content, await digest(content)), 7);
              const firstSubmit = nativeRequests.filter((request) => request.action === "submit_action").at(-1);
              assert.equal(firstSubmit.bdb_action.session_id, "11111111-1111-4111-8111-111111111111");
              assert.match(firstSubmit.bdb_action.client_submission_nonce, /^[0-9a-f-]{36}$/);
              assert.equal(firstSubmit.bdb_action.repair_correlation.role, "initial");

              const terminal = repairState();
              terminal.awaiting_corrected_action = true;
              await context.__submit(action("print('repair')\n", await digest("print('repair')\n")), 7);
              const repairSubmit = nativeRequests.filter((request) => request.action === "submit_action").at(-1);
              assert.equal(repairSubmit.bdb_action.session_id, "33333333-3333-4333-8333-333333333333");
              assert.match(repairSubmit.bdb_action.client_submission_nonce, /^[0-9a-f-]{36}$/);
              assert.notEqual(
                repairSubmit.bdb_action.client_submission_nonce,
                firstSubmit.bdb_action.client_submission_nonce
              );
              assert.deepEqual(repairSubmit.bdb_action.repair_correlation, {
                schema: "bdb-repair-correlation-v1",
                correlation_id: "22222222-2222-4222-8222-222222222222",
                role: "repair",
                predecessor_session_id: "11111111-1111-4111-8111-111111111111"
              });
            }

            run().catch((error) => {
              console.error(error && error.stack ? error.stack : error);
              process.exitCode = 1;
            });
            '''
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [node, str(harness), str(EXTENSION)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr

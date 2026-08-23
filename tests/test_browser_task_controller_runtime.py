from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension"


def test_task_controller_compiles_recovers_caches_accepts_and_gates_risk(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for browser task-controller runtime validation")

    harness = tmp_path / "task-controller-runtime.cjs"
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
            const localStore = {
              autoEnabled: true,
              autoMaxIterations: 8,
              autoMaxMinutes: 15,
              autoShadowMode: false
            };
            const sessionStore = {};
            let nativeArmed = true;
            const nativeCounts = { context: 0, search_text: 0, submit_action: 0, status: 0 };

            function clone(value) {
              return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
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
                  if (Object.prototype.hasOwnProperty.call(store, key)) result[key] = clone(store[key]);
                }
                return result;
              }
              const result = clone(keys);
              for (const key of Object.keys(keys || {})) {
                if (Object.prototype.hasOwnProperty.call(store, key)) result[key] = clone(store[key]);
              }
              return result;
            }

            function storageArea(store) {
              return {
                async get(keys) { return standardGet(store, keys); },
                async set(values) { Object.assign(store, clone(values)); },
                async remove(keys) {
                  for (const key of Array.isArray(keys) ? keys : [keys]) delete store[key];
                }
              };
            }

            function response(request, body) {
              return {
                schema: "bdb-native-response-v1",
                host_version: "0.4.7",
                request_id: request.request_id,
                ...body
              };
            }

            const context = {
              console,
              TextEncoder,
              Uint8Array,
              Date,
              JSON,
              Math,
              Number,
              Promise,
              Map,
              Set,
              setTimeout,
              clearTimeout,
              crypto: webcrypto,
              chrome: {
                storage: {
                  local: storageArea(localStore),
                  session: storageArea(sessionStore)
                },
                runtime: {
                  lastError: null,
                  getManifest() { return { version: "0.4.7" }; },
                  onMessage: { addListener() {} },
                  sendNativeMessage(_host, request, callback) {
                    nativeCounts[request.action] = (nativeCounts[request.action] || 0) + 1;
                    if (request.action === "context") {
                      callback(response(request, {
                        status: "context",
                        context: {
                          base_sha: "a".repeat(40),
                          allowed_paths: ["src/**"],
                          source_clean: true,
                          source_changes: []
                        },
                        arm: { armed: nativeArmed }
                      }));
                      return;
                    }
                    if (request.action === "search_text") {
                      const query = request.bdb_action.payload.query;
                      const count = query === "forbidden" ? 0 : 1;
                      callback(response(request, {
                        status: "completed",
                        result: {
                          status: "success",
                          operation: "search_text",
                          query,
                          matches: count ? [{ kind: "content", path: "src/app.py", line: 1, text: query }] : [],
                          total_matches: count,
                          returned_matches: count,
                          truncated: false,
                          base_sha: "a".repeat(40),
                          changed_files: []
                        }
                      }));
                      return;
                    }
                    if (request.action === "submit_action") {
                      callback(response(request, {
                        status: "completed",
                        command_id: "11111111-1111-4111-8111-111111111111:000001",
                        result: {
                          status: "success",
                          changed_files: ["src/app.py"],
                          promotion: {
                            status: "promoted",
                            source_commit: "a".repeat(40),
                            changed_files: ["src/app.py"]
                          },
                          verification: { tests: { status: "success" } }
                        }
                      }));
                      return;
                    }
                    if (request.action === "status") {
                      callback(response(request, { status: "ready", arm: { armed: true } }));
                      return;
                    }
                    throw new Error(`unexpected native request ${request.action}`);
                  }
                }
              }
            };
            context.globalThis = context;
            context.self = context;
            vm.createContext(context);
            for (const scriptName of ["background.js", "background_task_controller.js"]) {
              const scriptPath = path.join(extensionDir, scriptName);
              vm.runInContext(fs.readFileSync(scriptPath, "utf8"), context, { filename: scriptPath });
            }
            vm.runInContext(
              "globalThis.__consider = considerAuto; globalThis.__submit = submitAction; globalThis.__fingerprintAction = async (action) => bdbTaskFingerprint(bdbTaskActionIdentity(action));",
              context
            );

            function readAction(query) {
              return {
                schema: "bdb-action-v1",
                repo_alias: "synthetic",
                operation: "search_text",
                payload: { query, max_results: 10 },
                presentation: { mode: "compact" }
              };
            }

            async function run() {
              const automatic = {
                ...readAction("auto-needle"),
                automation: {
                  mode: "auto",
                  loop_id: "pętla AUTO ze spacjami / 2026",
                  iteration: 1
                }
              };
              const first = await context.__consider(automatic, 7);
              assert.equal(first.executed, true, JSON.stringify(first));
              assert.equal(first.compiler.loop_id_changed, true);
              assert.match(first.loopId, /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/);
              assert.equal(first.response.result.task_guidance.cache, "miss");

              const checkpointKey = `${first.loopId}:1`;
              const checkpointBeforeReplay =
                localStore.bdbTaskCheckpointsV1[checkpointKey];

              const restored = await context.__consider(automatic, 7);
              assert.equal(restored.executed, true, JSON.stringify(restored));
              assert.equal(restored.durableCheckpoint, true);
              assert.equal(restored.recoveredResult, true);
              assert.strictEqual(
                localStore.bdbTaskCheckpointsV1[checkpointKey],
                checkpointBeforeReplay,
                "replaying a durable result must not rewrite its checkpoint"
              );

              const deliveredLoop = "delivered-checkpoint-loop";
              const deliveredCheckpointKey = `${deliveredLoop}:1`;
              const deliveredAt = Date.now() - 1000;
              const deliveredResponse = JSON.parse(JSON.stringify(first.response));
              localStore.bdbTaskCheckpointsV1[deliveredCheckpointKey] = {
                created_at: Date.now(),
                loop_id: deliveredLoop,
                iteration: 1,
                delivered: false,
                should_continue: true,
                stop_reason: null,
                state_status: "running",
                response: deliveredResponse
              };
              sessionStore[`bdbAuto:7:${deliveredLoop}`] = {
                startedAt: Date.now() - 2000,
                lastIteration: 1,
                status: "running",
                lastResponse: deliveredResponse,
                lastResponseIteration: 1,
                lastResponseDelivered: true,
                lastResponseDeliveredAt: deliveredAt,
                updatedAt: Date.now()
              };

              const submitsBeforeDeliveredCheckpoint = nativeCounts.submit_action;
              const deliveredCheckpoint = await context.__consider(
                {
                  ...readAction("delivered-checkpoint"),
                  automation: {
                    mode: "auto",
                    loop_id: deliveredLoop,
                    iteration: 1
                  }
                },
                7
              );
              assert.equal(deliveredCheckpoint.executed, false, JSON.stringify(deliveredCheckpoint));
              assert.equal(deliveredCheckpoint.reason, "iteration_already_processed", JSON.stringify(deliveredCheckpoint));
              assert.equal(deliveredCheckpoint.expectedIteration, 2, JSON.stringify(deliveredCheckpoint));
              assert.equal(deliveredCheckpoint.durableCheckpoint, true, JSON.stringify(deliveredCheckpoint));
              assert.equal(deliveredCheckpoint.alreadyDelivered, true, JSON.stringify(deliveredCheckpoint));
              assert.equal(nativeCounts.submit_action, submitsBeforeDeliveredCheckpoint);
              assert.equal(sessionStore[`bdbAuto:7:${deliveredLoop}`].lastResponseDelivered, true);
              assert.equal(sessionStore[`bdbAuto:7:${deliveredLoop}`].lastResponseDeliveredAt, deliveredAt);
              assert.equal(localStore.bdbTaskCheckpointsV1[deliveredCheckpointKey].delivered, true);
              assert.equal(localStore.bdbTaskCheckpointsV1[deliveredCheckpointKey].delivered_at, deliveredAt);

              const beforeCacheSearches = nativeCounts.search_text;
              const cacheFirst = await context.__submit(readAction("cache-me"), 7);
              const cacheSecond = await context.__submit(readAction("cache-me"), 7);
              assert.equal(cacheFirst.result.task_guidance.cache, "miss");
              assert.equal(cacheSecond.result.task_guidance.cache, "hit");
              assert.equal(nativeCounts.search_text - beforeCacheSearches, 1);
              assert.equal(cacheSecond.result.execution_cache.status, "hit");

              const mutating = {
                schema: "bdb-action-v1",
                repo_alias: "synthetic",
                operation: "replace_exact_and_test",
                payload: {
                  path: "src/app.py",
                  old: "old",
                  new: "new",
                  profile_id: "poc_pytest"
                },
                acceptance: {
                  schema: "bdb-acceptance-v1",
                  result_status: "success",
                  changed_files_include: ["src/app.py"],
                  promotion_required: true,
                  tests_required: true,
                  search_assertions: [
                    { query: "forbidden", path: "src/app.py", min_matches: 0, max_matches: 0 }
                  ]
                }
              };
              const accepted = await context.__submit(mutating, 7);
              assert.equal(accepted.result.acceptance.status, "passed", JSON.stringify(accepted));
              assert.equal(accepted.result.task_guidance.next_operation, "complete");
              assert.ok(localStore.bdbMutationGuardsV1);
              assert.deepEqual(
                Object.keys(localStore.bdbMutationGuardsV1.entries),
                [],
                JSON.stringify(localStore.bdbMutationGuardsV1)
              );

              const mutationFingerprint = await context.__fingerprintAction(mutating);
              const mutationGuardKey = `synthetic:${mutationFingerprint}`;
              const mutationGuardCreatedAt = Date.now();
              localStore.bdbMutationGuardsV1.entries[mutationGuardKey] = {
                repo_alias: "synthetic",
                operation: "replace_exact_and_test",
                fingerprint: mutationFingerprint,
                status: "submitting",
                command_id: null,
                response: null,
                created_at: mutationGuardCreatedAt,
                updated_at: mutationGuardCreatedAt
              };
              delete sessionStore.bdbActionCacheV1;

              const submitsBeforeGuardedRepeat = nativeCounts.submit_action;
              const guardedRepeat = await context.__submit(mutating, 7);
              assert.equal(guardedRepeat.status, "pending", JSON.stringify(guardedRepeat));
              assert.equal(guardedRepeat.mutation_guard.status, "blocked");
              assert.equal(
                guardedRepeat.mutation_guard.reason,
                "mutation_submission_state_unknown"
              );
              assert.equal(nativeCounts.submit_action, submitsBeforeGuardedRepeat);
              assert.ok(localStore.bdbMutationGuardsV1.entries[mutationGuardKey]);

              const guardedRepeatAgain = await context.__submit(mutating, 7);
              assert.equal(guardedRepeatAgain.status, "pending", JSON.stringify(guardedRepeatAgain));
              assert.equal(guardedRepeatAgain.mutation_guard.status, "blocked");
              assert.equal(nativeCounts.submit_action, submitsBeforeGuardedRepeat);
              assert.equal(
                localStore.bdbMutationGuardsV1.entries[mutationGuardKey].created_at,
                mutationGuardCreatedAt
              );

              delete localStore.bdbMutationGuardsV1.entries[mutationGuardKey];

              const acceptedAuto = {
                ...mutating,
                automation: { mode: "auto", loop_id: "accepted-auto-loop", iteration: 1 }
              };
              localStore.bdbConversationBindingsV1 = {
                "conversation-accepted": {
                  schema: "bdb-conversation-binding-v1",
                  conversation_id: "conversation-accepted",
                  tab_id: 7,
                  repo_alias: acceptedAuto.repo_alias,
                  updated_at: Date.now()
                }
              };
              const acceptedDecision = await context.__consider(acceptedAuto, 7);
              const acceptedTaskSnapshot = await context.bdbTaskSnapshot();
              const acceptedTask = acceptedTaskSnapshot.tasks.find(
                (task) => task.loop_id === "accepted-auto-loop"
              );
              assert.ok(acceptedTask, JSON.stringify(acceptedTaskSnapshot));
              assert.equal(acceptedTask.conversation_id, "conversation-accepted");
              assert.equal(acceptedTask.conversation_tab_id, 7);
              assert.equal(acceptedDecision.executed, true, JSON.stringify(acceptedDecision));
              assert.equal(acceptedDecision.state.status, "done");
              assert.equal(acceptedDecision.shouldContinue, false);

              const acceptedDelivered = await context.markAutoResultDelivered(
                "accepted-auto-loop",
                1,
                7
              );
              assert.equal(acceptedDelivered.marked, true);

              const resumed = await context.bdbResumeTask("accepted-auto-loop", 7);
              assert.equal(resumed.status, "running");
              assert.equal(resumed.expected_iteration, 2);
              assert.equal(resumed.unlimited, true);
              assert.equal(sessionStore["bdbAuto:7:accepted-auto-loop"].lastIteration, 1);

              await context.bdbTaskUpsert("bound-resume-loop", {
                status: "stopped",
                last_iteration: 1,
                expected_iteration: 2,
                repo_alias: "synthetic",
                conversation_id: "conversation-bound",
                conversation_tab_id: 7
              });
              const mismatchedResume = await context.bdbResumeTask("bound-resume-loop", 8);
              assert.equal(mismatchedResume.status, "conversation_mismatch");
              assert.equal(mismatchedResume.expected_tab_id, 7);
              assert.equal(mismatchedResume.conversation_id, "conversation-bound");
              assert.equal(sessionStore["bdbAuto:8:bound-resume-loop"], undefined);

              const boundResume = await context.bdbResumeTask("bound-resume-loop", 7);
              assert.equal(boundResume.status, "running");
              assert.equal(boundResume.expected_iteration, 2);

              await context.bdbTaskUpsert("monotonic-loop", {
                status: "running",
                last_iteration: 12,
                expected_iteration: 13
              });
              await context.bdbTaskUpsert("monotonic-loop", {
                status: "running",
                last_iteration: 4,
                expected_iteration: 5
              });
              const monotonicLedger = await context.bdbTaskLedger();
              assert.equal(monotonicLedger.tasks["monotonic-loop"].last_iteration, 12);
              assert.equal(monotonicLedger.tasks["monotonic-loop"].expected_iteration, 13);

              const visual = {
                ...mutating,
                payload: { ...mutating.payload, old: "visual-old", new: "visual-new" },
                acceptance: {
                  ...mutating.acceptance,
                  manual_visual_confirmation_required: true
                },
                automation: { mode: "auto", loop_id: "visual-loop", iteration: 1 }
              };
              const visualDecision = await context.__consider(visual, 7);
              assert.equal(visualDecision.executed, true, JSON.stringify(visualDecision));
              assert.equal(visualDecision.response.result.acceptance.status, "needs_confirmation");
              assert.equal(
                visualDecision.response.result.task_guidance.next_operation,
                "await_user_visual_feedback"
              );
              assert.equal(visualDecision.state.status, "needs_user");
              assert.equal(visualDecision.shouldContinue, false);

              const pendingSnapshot = await context.bdbTaskSnapshot();
              const pendingVisualTask = pendingSnapshot.tasks.find((task) => task.loop_id === "visual-loop");
              assert.ok(pendingVisualTask, JSON.stringify(pendingSnapshot));
              assert.equal(pendingVisualTask.recovery_pending, true);
              assert.equal(pendingVisualTask.recovery_iteration, 1);

              const visualRecovery = await context.bdbResumeTask("visual-loop", 7);
              assert.equal(visualRecovery.status, "recovering_result");
              assert.equal(visualRecovery.task_status, "needs_user");
              assert.equal(visualRecovery.expected_iteration, 1);
              assert.equal(visualRecovery.recovery_only, true);
              assert.equal(
                visualRecovery.recovery_response.result.acceptance.status,
                "needs_confirmation"
              );
              assert.equal(sessionStore["bdbAuto:7:visual-loop"].status, "needs_user");

              const restoredVisual = await context.__consider(visual, 7);
              assert.equal(restoredVisual.executed, true);
              assert.equal(restoredVisual.recoveredResult, true);
              assert.equal(restoredVisual.durableCheckpoint, true);
              const visualLedger = await context.bdbTaskLedger();
              assert.equal(visualLedger.tasks["visual-loop"].status, "needs_user");
              assert.equal(visualLedger.tasks["visual-loop"].awaiting_visual_feedback, true);

              const blockedWithoutFeedback = await context.__consider({
                ...readAction("visual-follow-up-blocked"),
                automation: { mode: "auto", loop_id: "visual-loop", iteration: 2 }
              }, 7);
              assert.equal(blockedWithoutFeedback.executed, false);
              assert.equal(blockedWithoutFeedback.reason, "loop_not_running");

              const blockedBeforeDelivery = await context.__consider({
                ...readAction("visual-follow-up-too-early"),
                automation: {
                  mode: "auto",
                  loop_id: "visual-loop",
                  iteration: 99,
                  continue_after_user_feedback: true
                }
              }, 7);
              assert.equal(blockedBeforeDelivery.executed, false);
              assert.equal(blockedBeforeDelivery.reason, "visual_feedback_result_not_delivered");
              assert.equal(blockedBeforeDelivery.compiler.iteration, 2);

              const visualDelivered = await context.markAutoResultDelivered("visual-loop", 1, 7);
              assert.equal(visualDelivered.marked, true);
              const deliveredSnapshot = await context.bdbTaskSnapshot();
              const deliveredVisualTask = deliveredSnapshot.tasks.find((task) => task.loop_id === "visual-loop");
              assert.ok(deliveredVisualTask, JSON.stringify(deliveredSnapshot));
              assert.equal(deliveredVisualTask.recovery_pending, false);
              assert.equal(deliveredVisualTask.recovery_iteration, null);
              const feedbackDecision = await context.__consider({
                ...readAction("visual-follow-up-after-user-feedback"),
                automation: {
                  mode: "auto",
                  loop_id: "visual-loop",
                  iteration: 99,
                  continue_after_user_feedback: true
                }
              }, 7);
              assert.equal(feedbackDecision.executed, true, JSON.stringify(feedbackDecision));
              assert.equal(feedbackDecision.iteration, 2);
              assert.equal(feedbackDecision.compiler.iteration, 2);
              assert.equal(feedbackDecision.state.status, "running");
              assert.equal(sessionStore["bdbAuto:7:visual-loop"].resumedAfterVisualFeedback, true);
              const feedbackLedger = await context.bdbTaskLedger();
              assert.equal(feedbackLedger.tasks["visual-loop"].awaiting_visual_feedback, false);
              assert.equal(feedbackLedger.tasks["visual-loop"].status, "running");

              const risky = {
                schema: "bdb-action-v1",
                repo_alias: "synthetic",
                operation: "multi_file_patch",
                payload: {
                  profile_id: "poc_pytest",
                  patch: {
                    schema: "bdb-multi-file-patch-v1",
                    operations: [{ kind: "delete_file", path: "src/old.py" }]
                  }
                },
                automation: { mode: "auto", loop_id: "high-risk-loop", iteration: 1 }
              };
              const submitsBeforeRisk = nativeCounts.submit_action;
              const stopped = await context.__consider(risky, 7);
              assert.equal(stopped.executed, false);
              assert.equal(stopped.reason, "high_risk_requires_assisted");
              assert.equal(nativeCounts.submit_action, submitsBeforeRisk);

              const armAction = {
                ...readAction("arm-check"),
                automation: { mode: "auto", loop_id: "arm-check-loop", iteration: 1 }
              };
              nativeArmed = false;
              const submitsBeforeArm = nativeCounts.submit_action;
              const disarmed = await context.__consider(armAction, 7);
              assert.equal(disarmed.executed, false);
              assert.equal(disarmed.reason, "native_host_disarmed");
              assert.equal(nativeCounts.submit_action, submitsBeforeArm);
              nativeArmed = true;
              const armedRetry = await context.__consider(armAction, 7);
              assert.equal(armedRetry.executed, true, JSON.stringify(armedRetry));

              const health = await context.bdbHealthSnapshot({ probeNative: true, contentVersion: "0.4.7" });
              assert.equal(health.status, "ready");
              assert.equal(health.content_version_match, true);
              assert.equal(health.capabilities.durable_resume, true);
              assert.equal(health.capabilities.visual_feedback_resume, true);

              const diagnostics = await context.bdbDiagnosticsSnapshot();
              assert.equal(diagnostics.privacy.source_code_included, false);
              assert.ok(diagnostics.events.length > 0);
              assert.ok(diagnostics.tasks.length > 0);
              assert.equal(diagnostics.flight_recorder.schema, "bdb-flight-recorder-v1");
              assert.ok(diagnostics.flight_recorder.sample_count > 0, JSON.stringify(diagnostics));
              assert.ok(diagnostics.flight_recorder.stages.action, JSON.stringify(diagnostics));
              assert.ok(diagnostics.flight_recorder.stages.auto, JSON.stringify(diagnostics));
              assert.ok(Number.isInteger(diagnostics.flight_recorder.stages.action.p50_ms));
              assert.ok(Number.isInteger(diagnostics.flight_recorder.stages.action.p90_ms));
              assert.ok(Number.isInteger(diagnostics.flight_recorder.stages.action.p99_ms));
              assert.ok(Array.isArray(diagnostics.flight_recorder.critical_path));
              const recordedAction = diagnostics.events.find(
                (event) => event.event === "action_completed" && event.repo_alias === "synthetic"
              );
              assert.ok(recordedAction, JSON.stringify(diagnostics));
              assert.equal(recordedAction.stage, "action");
              assert.ok(Number.isInteger(recordedAction.duration_ms));
            }

            run().catch((error) => {
              console.error(error);
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
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr

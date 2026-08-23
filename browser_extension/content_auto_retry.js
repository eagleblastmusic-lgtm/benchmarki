"use strict";

// ChatGPT can render the next AUTO action while the previous decision is still
// publishing its canonical loop state. A duplicate live panel can also observe
// the same iteration while its durable replay lease is still processing. Retry
// only those exact transient gaps; all replay, sequence and opt-in gates
// remain owned by the background worker.
const BDB_AUTO_DECISION_RETRY_ATTEMPTS = 24;
const BDB_AUTO_DECISION_RETRY_MS = 250;
// Keep the in-progress retry window longer than the 180-second replay claim lease.
// 280 attempts x 750 ms = 210 seconds, leaving 30 seconds for abandoned-claim recovery.
const BDB_AUTO_IN_PROGRESS_RETRY_ATTEMPTS = 280;
const BDB_AUTO_IN_PROGRESS_RETRY_MS = 750;
// AUTO result delivery may race a transiently unavailable or still-busy composer.
// Retry only pre-insertion states so user text is never replaced or submitted.
const BDB_AUTO_DELIVERY_RETRY_ATTEMPTS = 24;
const BDB_AUTO_DELIVERY_RETRY_MS = 250;
const BDB_AUTO_DELIVERY_TRANSIENT_REASONS = new Set([
  "composer_missing",
  "composer_not_empty"
]);
const BDB_AUTO_TRANSIENT_REASONS = new Set([
  "non_sequential_iteration",
  "iteration_in_progress"
]);

const BDB_AUTO_ACTIVE_RUNS = new Map();

function bdbReportAutoDelivery(action, event, reason, status, metric) {
  if (typeof globalThis.bdbContentRecord !== "function") {
    return;
  }
  const automation = action && action.automation;
  globalThis.bdbContentRecord({
    event,
    loopId: automation && automation.loop_id,
    iteration: automation && automation.iteration,
    operation: action && action.operation,
    reason,
    status,
    traceId: action && action.trace_id,
    metric
  });
}

function bdbAutoRunKey(action) {
  const automation = action && action.automation;
  if (
    !automation ||
    typeof automation.loop_id !== "string" ||
    !Number.isInteger(automation.iteration)
  ) {
    return null;
  }
  return `${automation.loop_id}:${automation.iteration}`;
}

function bdbAutoPanelDetached(button) {
  if (!button) {
    return true;
  }
  if (button.isConnected === false) {
    return true;
  }

  // Runtime DOM exposes isConnected, but the extension's lightweight browser
  // harnesses intentionally implement only parentElement/classList. This fallback
  // also covers a React-removed panel before all descendant references are cleared.
  const directParent = button.parentElement;
  const isAssistedPanel = Boolean(
    directParent &&
    (
      (
        directParent.classList &&
        directParent.classList.contains("bdb-assisted")
      ) ||
      (
        typeof directParent.className === "string" &&
        directParent.className.split(/\s+/).includes("bdb-assisted")
      )
    )
  );
  return Boolean(
    isAssistedPanel &&
    directParent.parentElement === null
  );
}

function bdbSetAutoButtonText(button, text) {
  if (!button || bdbAutoPanelDetached(button)) {
    return;
  }
  if (button.textContent !== text) {
    button.textContent = text;
  }
}


async function bdbAutoDecisionSleep(milliseconds) {
  if (
    typeof chrome === "object" &&
    chrome.runtime &&
    typeof chrome.runtime.sendMessage === "function"
  ) {
    try {
      const waited = await chrome.runtime.sendMessage({
        type: "BDB_AUTO_WAIT",
        milliseconds
      });
      if (waited && waited.ok === true) {
        return;
      }
    } catch (_error) {
      // Fall back only when the background wait broker is unavailable.
    }
  }
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function bdbAutoActionIteration(action) {
  const automation = action && action.automation;
  return automation && Number.isInteger(automation.iteration)
    ? automation.iteration
    : null;
}

function bdbAutoDecisionNeedsCatchUp(auto, iteration) {
  return Boolean(
    auto &&
    auto.executed === false &&
    BDB_AUTO_TRANSIENT_REASONS.has(auto.reason) &&
    Number.isInteger(iteration) &&
    Number.isInteger(auto.expectedIteration) &&
    auto.expectedIteration <= iteration
  );
}

function bdbRecoverDetachedAutoPanel(action, phase) {
  bdbReportAutoDelivery(
    action,
    "auto_panel_detached",
    phase,
    "recovering",
    "auto_panel_detachments"
  );
  if (typeof scheduleBdbDocumentReconciliation === "function") {
    scheduleBdbDocumentReconciliation();
  } else if (typeof scan === "function" && typeof document !== "undefined") {
    setTimeout(() => scan(document), 600);
  }
  return {
    executed: false,
    reason: "panel_detached",
    retryCancelled: true,
    retryForReplacement: true
  };
}

function bdbAutoTerminalStatus(auto) {
  const state = auto && auto.state;
  return state && typeof state.status === "string" && state.status.length > 0
    ? state.status
    : null;
}

function bdbAutoStopLabel(reason, auto = null) {
  if (reason === "native_host_disarmed") {
    return "uzbrój sesję BDB i spróbuj ponownie";
  }
  if (reason === "native_host_unavailable") {
    return "Native Host jest niedostępny";
  }
  if (reason === "iteration_already_processed") {
    return "już wykonano";
  }
  if (reason === "loop_not_running") {
    const status = bdbAutoTerminalStatus(auto);
    if (status) {
      return `pętla zakończona (${status}) — użyj nowego loop_id; nie zwiększaj iteration`;
    }
    return "pętla zakończona — użyj nowego loop_id; nie zwiększaj iteration";
  }
  if (reason === "visual_feedback_not_expected") {
    return "brak oczekującej oceny wizualnej";
  }
  if (reason === "visual_feedback_result_not_delivered") {
    return "najpierw dostarcz poprzedni wynik";
  }
  if (reason === "invalid_visual_feedback_resume") {
    return "nieprawidłowe wznowienie po ocenie";
  }
  if (reason === "milestone_completed") {
    return "milestone ukończony — dalszy etap wymaga uruchomienia przez użytkownika";
  }
  return reason || "ASSISTED";
}

async function bdbConsiderAutoWithCatchUp(action, button) {
  const iteration = bdbAutoActionIteration(action);
  let latest = null;

  for (let attempt = 0; attempt < BDB_AUTO_IN_PROGRESS_RETRY_ATTEMPTS; attempt += 1) {
    if (bdbAutoPanelDetached(button)) {
      return bdbRecoverDetachedAutoPanel(action, "while_waiting_for_decision");
    }

    const decision = await chrome.runtime.sendMessage({ type: "BDB_CONSIDER_AUTO", action });
    if (!decision || decision.ok !== true) {
      throw new Error(decision && decision.error ? decision.error : "Brak decyzji AUTO");
    }

    latest = decision.response;
    if (!bdbAutoDecisionNeedsCatchUp(latest, iteration)) {
      return latest;
    }

    const retryAttempts = latest.reason === "iteration_in_progress"
      ? BDB_AUTO_IN_PROGRESS_RETRY_ATTEMPTS
      : BDB_AUTO_DECISION_RETRY_ATTEMPTS;
    if (attempt + 1 >= retryAttempts) {
      return { ...latest, retryExhausted: true };
    }

    if (attempt === 0 || (attempt + 1) % 4 === 0) {
      bdbSetAutoButtonText(
        button,
        `BDB AUTO: synchronizacja ${latest.expectedIteration}→${iteration}…`
      );
    }
    await bdbAutoDecisionSleep(
      latest.reason === "iteration_in_progress"
        ? BDB_AUTO_IN_PROGRESS_RETRY_MS
        : BDB_AUTO_DECISION_RETRY_MS
    );
  }

  return latest;
}

async function bdbRunAutoPanel(action, button, output, compact) {
  let keepDisabled = false;
  button.disabled = true;
  bdbSetAutoButtonText(button, "BDB AUTO: sprawdzanie…");
  try {
    const auto = await bdbConsiderAutoWithCatchUp(action, button);
    if (auto && auto.retryCancelled === true) {
      return { retryForReplacement: true };
    }
    if (bdbAutoPanelDetached(button)) {
      bdbRecoverDetachedAutoPanel(action, "after_decision");
      return { retryForReplacement: true };
    }
    if (!auto.executed) {
      if (auto.reason === "loop_not_running") {
        const automation = action && action.automation;
        const status = bdbAutoTerminalStatus(auto) || "terminal";
        const handoffResponse = {
          schema: "bdb-auto-loop-handoff-v1",
          status: "failed",
          error: {
            code: "loop_not_running",
            message: `AUTO loop is terminal (${status}); continue automatically with a new loop_id at iteration 1.`
          },
          previous_loop_id: automation && automation.loop_id,
          rejected_iteration: automation && automation.iteration,
          task_guidance: {
            schema: "bdb-task-guidance-v1",
            phase: "handoff",
            next_operation: "start_new_loop"
          }
        };
        let handoff = null;
        for (let attempt = 0; attempt < BDB_AUTO_DELIVERY_RETRY_ATTEMPTS; attempt += 1) {
          handoff = await autoSend(
            handoffResponse,
            automation && automation.loop_id,
            automation && automation.iteration
          );
          if (
            handoff &&
            handoff.sent === true &&
            handoff.confirmed === true &&
            handoff.confirmedVia === "user_message"
          ) {
            break;
          }
          if (!handoff || !BDB_AUTO_DELIVERY_TRANSIENT_REASONS.has(handoff.reason)) {
            break;
          }
          if (attempt + 1 >= BDB_AUTO_DELIVERY_RETRY_ATTEMPTS) {
            break;
          }
          bdbSetAutoButtonText(
            button,
            `BDB AUTO: przekazuję zakończenie pętli do ChatGPT… (${attempt + 1}/${BDB_AUTO_DELIVERY_RETRY_ATTEMPTS})`
          );
          await bdbAutoDecisionSleep(BDB_AUTO_DELIVERY_RETRY_MS);
        }
        if (
          handoff &&
          handoff.sent === true &&
          handoff.confirmed === true &&
          handoff.confirmedVia === "user_message"
        ) {
          bdbSetAutoButtonText(button, "BDB AUTO: przekazano zakończenie pętli do ChatGPT");
          keepDisabled = true;
          return { retryForReplacement: false };
        }
        const handoffReason = handoff && handoff.reason ? handoff.reason : "send_not_confirmed";
        bdbSetAutoButtonText(
          button,
          `BDB AUTO: automatyczne przekazanie pętli oczekuje na ponowienie (${handoffReason})`
        );
        return { retryForReplacement: false };
      }
      const suffix = auto.retryExhausted
        ? `${bdbAutoStopLabel(auto.reason, auto)}, retry exhausted`
        : bdbAutoStopLabel(auto.reason, auto);
      bdbSetAutoButtonText(button, `BDB: Wykonaj (${suffix})`);
      keepDisabled = auto.reason === "iteration_already_processed";
      return { retryForReplacement: false };
    }

    renderResult(output, auto.response, { compact });
    if (auto.resultDelivered === true) {
      bdbSetAutoButtonText(
        button,
        `BDB AUTO: wynik odtworzony (${auto.stopReason || "zakończono"})`
      );
      return { retryForReplacement: false };
    }

    if (bdbAutoPanelDetached(button)) {
      bdbRecoverDetachedAutoPanel(action, "before_result_delivery");
      return { retryForReplacement: true };
    }
    let sent = null;
    for (let attempt = 0; attempt < BDB_AUTO_DELIVERY_RETRY_ATTEMPTS; attempt += 1) {
      if (bdbAutoPanelDetached(button)) {
        bdbRecoverDetachedAutoPanel(action, "while_waiting_for_result_delivery");
        return { retryForReplacement: true };
      }
      sent = await autoSend(auto.response, auto.loopId, auto.iteration);
      if (
        sent.sent &&
        sent.confirmed === true &&
        sent.confirmedVia === "user_message"
      ) {
        break;
      }
      if (!BDB_AUTO_DELIVERY_TRANSIENT_REASONS.has(sent.reason)) {
        break;
      }
      if (attempt + 1 >= BDB_AUTO_DELIVERY_RETRY_ATTEMPTS) {
        sent = { ...sent, deliveryRetryExhausted: true };
        break;
      }
      bdbSetAutoButtonText(
        button,
        `BDB AUTO: oczekiwanie na wolny edytor… (${attempt + 1}/${BDB_AUTO_DELIVERY_RETRY_ATTEMPTS})`
      );
      await bdbAutoDecisionSleep(BDB_AUTO_DELIVERY_RETRY_MS);
    }
    if (sent.sent && sent.confirmed === true && sent.confirmedVia === "user_message") {
      bdbReportAutoDelivery(action, "composer_send_confirmed", sent.confirmedVia, "sent", "composer_send_successes");
      try {
        await chrome.runtime.sendMessage({
          type: "BDB_MARK_AUTO_RESULT_DELIVERED",
          loopId: auto.loopId,
          iteration: auto.iteration
        });
      } catch (_error) {
      }
      bdbSetAutoButtonText(
        button,
        auto.shouldContinue
          ? `BDB AUTO: wysłano ${auto.iteration}`
          : `BDB AUTO: wynik wysłany; zatrzymano (${auto.stopReason || "zakończono"})`
      );
      return { retryForReplacement: false };
    }
    bdbSetAutoButtonText(
      button,
      auto.shouldContinue
        ? `BDB AUTO → ASSISTED (${sent.reason})`
        : `BDB AUTO: zatrzymano; wynik oczekuje na ponowienie (${sent.reason})`
    );
    bdbReportAutoDelivery(action, "composer_send_failed", sent.reason, "assisted", "composer_send_failures");
    return { retryForReplacement: false };
  } catch (error) {
    if (!bdbAutoPanelDetached(button)) {
      output.textContent = `BDB AUTO error: ${String(error && error.message ? error.message : error)}`;
      bdbSetAutoButtonText(button, "BDB AUTO → ASSISTED");
    }
    bdbReportAutoDelivery(
      action,
      "auto_panel_error",
      String(error && error.message ? error.message : error),
      "error",
      "auto_panel_errors"
    );
    return { retryForReplacement: false };
  } finally {
    button.disabled = keepDisabled;
  }
}

function bdbResumeConversationId() {
  const match = location.pathname.match(/(?:^|\/)c\/([A-Za-z0-9-]{8,})(?:\/|$)/);
  return match ? match[1] : null;
}

async function bdbRetryResumedTask(loopId, expectedIteration, expectedConversationId = null, recoveryResponse = null) {
  if (typeof loopId !== "string" || !Number.isInteger(expectedIteration)) {
    return { retried: false, reason: "invalid_resume_message" };
  }
  if (
    expectedConversationId !== null &&
    (
      typeof expectedConversationId !== "string" ||
      bdbResumeConversationId() !== expectedConversationId
    )
  ) {
    return {
      retried: false,
      reason: "conversation_mismatch",
      expected_conversation_id: expectedConversationId,
      actual_conversation_id: bdbResumeConversationId()
    };
  }
  if (
    recoveryResponse &&
    typeof recoveryResponse === "object" &&
    !Array.isArray(recoveryResponse)
  ) {
    let sent = null;
    for (let attempt = 0; attempt < BDB_AUTO_DELIVERY_RETRY_ATTEMPTS; attempt += 1) {
      sent = await autoSend(recoveryResponse, loopId, expectedIteration);
      if (
        sent &&
        sent.sent === true &&
        sent.confirmed === true &&
        sent.confirmedVia === "user_message"
      ) {
        break;
      }
      if (!sent || !BDB_AUTO_DELIVERY_TRANSIENT_REASONS.has(sent.reason)) {
        break;
      }
      if (attempt + 1 >= BDB_AUTO_DELIVERY_RETRY_ATTEMPTS) {
        break;
      }
      await bdbAutoDecisionSleep(BDB_AUTO_DELIVERY_RETRY_MS);
    }
    if (
      sent &&
      sent.sent === true &&
      sent.confirmed === true &&
      sent.confirmedVia === "user_message"
    ) {
      try {
        await chrome.runtime.sendMessage({
          type: "BDB_MARK_AUTO_RESULT_DELIVERED",
          loopId,
          iteration: expectedIteration
        });
      } catch (_error) {
      }
      return {
        retried: true,
        recovered: true,
        iteration: expectedIteration
      };
    }
    return {
      retried: false,
      reason: sent && sent.reason ? sent.reason : "send_not_confirmed",
      recovery_pending: true
    };
  }
  const blocks = document.querySelectorAll("pre code, code");
  for (const block of blocks) {
    if (!(block instanceof HTMLElement)) continue;
    const action = parseAction(block);
    const automation = action && action.automation;
    if (
      !automation ||
      automation.loop_id !== loopId ||
      automation.iteration !== expectedIteration
    ) {
      continue;
    }
    const host = block.closest("pre") || block.parentElement;
    const panel = host && host.querySelector(":scope > .bdb-assisted");
    const button = panel && panel.querySelector(".bdb-execute");
    const output = panel && panel.querySelector(".bdb-output");
    if (!(button instanceof HTMLButtonElement) || !(output instanceof HTMLElement)) {
      continue;
    }
    button.disabled = false;
    await maybeAuto(action, button, output, compactAction(action));
    return { retried: true, iteration: expectedIteration };
  }
  return { retried: false, reason: "expected_action_not_visible" };
}

if (
  chrome.runtime.onMessage &&
  typeof chrome.runtime.onMessage.addListener === "function"
) {
  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!message || message.type !== "BDB_CONTENT_RESUME_TASK") {
      return undefined;
    }
    bdbRetryResumedTask(
      message.loopId,
      message.expectedIteration,
      message.conversationId,
      message.recoveryResponse
    )
      .then(sendResponse)
      .catch((error) => sendResponse({
        retried: false,
        reason: String(error && error.message ? error.message : error)
      }));
    return true;
  });
}

maybeAuto = async function maybeAutoWithDecisionCatchUp(action, button, output, compact) {
  const automation = action && action.automation;
  if (!automation || automation.mode !== "auto") {
    return;
  }

  const runKey = bdbAutoRunKey(action);
  if (runKey) {
    const active = BDB_AUTO_ACTIVE_RUNS.get(runKey);
    if (active && !bdbAutoPanelDetached(active.button)) {
      button.disabled = true;
      bdbSetAutoButtonText(button, "BDB AUTO: oczekiwanie na aktywną operację…");
      try {
        await active.promise;
      } finally {
        button.disabled = false;
      }
      bdbSetAutoButtonText(button, "BDB AUTO: obsłużono w aktywnym panelu");
      return;
    }
  }

  // A live duplicate shares the active promise above. A replacement panel whose
  // previous owner was detached starts immediately, preserving ChatGPT rerender
  // recovery while the background replay guard prevents duplicate execution.
  const promise = bdbRunAutoPanel(action, button, output, compact);
  const entry = { button, promise };
  if (runKey) {
    BDB_AUTO_ACTIVE_RUNS.set(runKey, entry);
  }
  try {
    await promise;
  } finally {
    if (runKey && BDB_AUTO_ACTIVE_RUNS.get(runKey) === entry) {
      BDB_AUTO_ACTIVE_RUNS.delete(runKey);
    }
  }
};

"use strict";

// A transport or wrapper failure after a mutating AUTO submission is ambiguous:
// the command may already be committed even when the browser did not receive its
// final response. Fail closed instead of releasing the iteration for replay.
const BDB_AUTO_MUTATION_UNCERTAIN_STATUS = "uncertain";
const BDB_AUTO_MUTATION_OPERATIONS = new Set([
  "replace_exact_and_test",
  "multi_file_patch"
]);
const bdbConsiderAutoBeforeMutationSafety = considerAuto;
const bdbClaimAutoReplayBeforeMutationSafety = claimAutoReplay;

function bdbAutoMutationIsUnsafeToReplay(action) {
  return Boolean(
    action &&
    typeof action.operation === "string" &&
    BDB_AUTO_MUTATION_OPERATIONS.has(action.operation)
  );
}

function bdbAutoMutationFailureDetail(error) {
  return String(error && error.message ? error.message : error).slice(0, 240);
}

async function bdbAutoMutationReplayRecord(loopId, iteration) {
  const key = autoReplayKey(loopId, iteration);
  const stored = await chrome.storage.local.get(AUTO_REPLAY_GUARD_KEY);
  const raw = stored[AUTO_REPLAY_GUARD_KEY];
  const guard = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
  return guard[key];
}

async function bdbAutoMarkMutationUncertain(loopId, iteration, detail) {
  const key = autoReplayKey(loopId, iteration);
  const stored = await chrome.storage.local.get(AUTO_REPLAY_GUARD_KEY);
  const raw = stored[AUTO_REPLAY_GUARD_KEY];
  const guard = raw && typeof raw === "object" && !Array.isArray(raw)
    ? { ...raw }
    : {};
  guard[key] = {
    status: BDB_AUTO_MUTATION_UNCERTAIN_STATUS,
    uncertainAt: Date.now(),
    detail
  };
  const entries = Object.entries(guard)
    .filter(([entryKey, record]) => (
      typeof entryKey === "string" &&
      record &&
      typeof record === "object" &&
      Number.isFinite(record.uncertainAt || record.completedAt || record.claimedAt)
    ))
    .sort((left, right) => (
      (left[1].uncertainAt || left[1].completedAt || left[1].claimedAt) -
      (right[1].uncertainAt || right[1].completedAt || right[1].claimedAt)
    ))
    .slice(-AUTO_REPLAY_GUARD_LIMIT);
  await chrome.storage.local.set({
    [AUTO_REPLAY_GUARD_KEY]: Object.fromEntries(entries)
  });
}

claimAutoReplay = async function claimAutoReplayWithMutationSafety(loopId, iteration) {
  const record = await bdbAutoMutationReplayRecord(loopId, iteration);
  if (
    record &&
    typeof record === "object" &&
    record.status === BDB_AUTO_MUTATION_UNCERTAIN_STATUS
  ) {
    return false;
  }
  return bdbClaimAutoReplayBeforeMutationSafety(loopId, iteration);
};

async function bdbAutoRecordMutationReconciliation(action, tabId, metadata, error) {
  const detail = bdbAutoMutationFailureDetail(error);
  await bdbAutoMarkMutationUncertain(
    metadata.loopId,
    metadata.iteration,
    detail
  );

  const response = {
    status: "failed",
    operation: action.operation,
    uncertain_execution: true,
    error: {
      code: "manual_reconciliation_required",
      message: "AUTO mutation may have executed. Inspect repository state before retrying.",
      detail
    }
  };
  const key = autoStateKey(tabId, metadata.loopId);
  const stored = await chrome.storage.session.get(key);
  const now = Date.now();
  const current = stored[key];
  const state = current && typeof current === "object" && !Array.isArray(current)
    ? { ...current }
    : {
      startedAt: now,
      lastIteration: 0,
      status: "running",
      lastUncertainIteration: metadata.iteration
    };

  state.lastIteration = metadata.iteration;
  state.lastCommandId = null;
  state.lastResponse = response;
  state.lastResponseIteration = metadata.iteration;
  state.lastResponseDelivered = false;
  state.status = "manual_reconciliation_required";
  state.updatedAt = now;
  // Diagnostic marker only: it records the last uncertain mutation and is
  // never consulted as an AUTO continuation limit.
  state.lastUncertainIteration = Math.max(
    Number.isInteger(state.lastUncertainIteration) ? state.lastUncertainIteration : 0,
    metadata.iteration
  );
  await chrome.storage.session.set({ [key]: state });

  return {
    executed: true,
    response,
    loopId: metadata.loopId,
    iteration: metadata.iteration,
    recoveredResult: false,
    resultDelivered: false,
    shouldContinue: false,
    stopReason: "manual_reconciliation_required",
    uncertainExecution: true,
    state
  };
}

considerAuto = async function considerAutoWithMutationSafety(action, tabId) {
  const metadata = automationMetadata(action);
  const mutating = metadata && bdbAutoMutationIsUnsafeToReplay(action);
  if (!mutating) {
    return bdbConsiderAutoBeforeMutationSafety(action, tabId);
  }

  try {
    const decision = await bdbConsiderAutoBeforeMutationSafety(action, tabId);
    if (decision && decision.reason === "replay_guard") {
      const record = await bdbAutoMutationReplayRecord(
        metadata.loopId,
        metadata.iteration
      );
      if (
        record &&
        typeof record === "object" &&
        record.status === BDB_AUTO_MUTATION_UNCERTAIN_STATUS
      ) {
        return {
          ...decision,
          reason: "manual_reconciliation_required",
          uncertainExecution: true
        };
      }
    }
    return decision;
  } catch (error) {
    try {
      return await bdbAutoRecordMutationReconciliation(
        action,
        tabId,
        metadata,
        error
      );
    } catch (_recordError) {
      try {
        await bdbAutoMarkMutationUncertain(
          metadata.loopId,
          metadata.iteration,
          bdbAutoMutationFailureDetail(error)
        );
      } catch (_guardError) {
      }
      return {
        executed: false,
        reason: "manual_reconciliation_required",
        expectedIteration: metadata.iteration,
        uncertainExecution: true,
        detail: bdbAutoMutationFailureDetail(error)
      };
    }
  }
};

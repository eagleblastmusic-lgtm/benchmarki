"use strict";

// A bounded, local-only control plane around the mature BDB transport. It keeps
// task progress and diagnostics out of the chat transcript, restores an
// undelivered AUTO result after a browser/service-worker restart, deduplicates
// exact actions against the same Git HEAD, and evaluates optional acceptance
// criteria after execution. It never enables AUTO and never bypasses policy,
// replay, promotion or high-risk gates. Milestone progression remains a
// projection of canonical Project Memory/Execution responses; this controller
// only records the durable transport cursor and never selects work itself.
const BDB_TASK_CONTROLLER_SCHEMA = "bdb-task-controller-v1";
const BDB_TASK_LEDGER_KEY = "bdbTaskLedgerV1";
const BDB_TASK_DIAGNOSTICS_KEY = "bdbAutoDiagnosticsV1";
const BDB_TASK_METRICS_KEY = "bdbTaskMetricsV1";
const BDB_TASK_CHECKPOINTS_KEY = "bdbTaskCheckpointsV1";
const BDB_TASK_CACHE_KEY = "bdbActionCacheV1";
const BDB_TASK_CONVERSATION_BINDINGS_KEY = "bdbConversationBindingsV1";
const BDB_TASK_RELEASE_CHANNEL = "stable";
const BDB_TASK_MAX_LEDGER = 64;
const BDB_TASK_MAX_DIAGNOSTICS = 200;
const BDB_TASK_MAX_CHECKPOINTS = 16;
const BDB_TASK_MAX_CACHE_ENTRIES = 32;
const BDB_TASK_MAX_CHECKPOINT_BYTES = 160 * 1024;
const BDB_TASK_MAX_CACHE_BYTES = 160 * 1024;
const BDB_TASK_READ_CACHE_MS = 2 * 60 * 1000;
const BDB_TASK_MUTATION_DEDUP_MS = 5 * 60 * 1000;
const BDB_TASK_CHECKPOINT_MS = 24 * 60 * 60 * 1000;
const BDB_TASK_LOOP_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const BDB_TASK_READ_OPERATIONS = new Set([
  "workspace_context",
  "search_text",
  "inspect_bundle",
  "open_read"
]);
const BDB_TASK_MUTATING_OPERATIONS = new Set([
  "replace_exact_and_test",
  "multi_file_patch"
]);
const BDB_TASK_HIGH_RISK_KINDS = new Set([
  "delete_file",
  "move_file",
  "rename_file"
]);
const BDB_TASK_TERMINAL_STATUSES = new Set([
  "done",
  "needs_user",
  "policy_denied",
  "manual_reconciliation_required",
  "failed",
  "cancelled",
  "aborted"
]);
const BDB_TASK_BENIGN_REPEAT_STOPS = new Set([
  "iteration_already_processed",
  "iteration_in_progress",
  "replay_guard",
  "loop_not_running"
]);

const bdbSubmitActionBeforeTaskController = submitAction;
const bdbConsiderAutoBeforeTaskController = considerAuto;
const bdbMarkAutoResultDeliveredBeforeTaskController = markAutoResultDelivered;
let bdbTaskStorageChain = Promise.resolve();

function bdbTaskClone(value) {
  return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
}

function bdbTaskSerializedBytes(value) {
  return new TextEncoder().encode(JSON.stringify(value)).byteLength;
}

function bdbTaskWithStorageLock(callback) {
  const run = bdbTaskStorageChain.then(callback, callback);
  bdbTaskStorageChain = run.catch(() => undefined);
  return run;
}

function bdbTaskSafeText(value, limit = 160) {
  if (typeof value !== "string") {
    return null;
  }
  const compact = value.replace(/[\r\n\t]+/g, " ").trim();
  return compact.length > limit ? `${compact.slice(0, limit - 1)}…` : compact;
}

function bdbTaskCanonical(value) {
  if (Array.isArray(value)) {
    return value.map(bdbTaskCanonical);
  }
  if (value && typeof value === "object") {
    const result = {};
    for (const key of Object.keys(value).sort()) {
      result[key] = bdbTaskCanonical(value[key]);
    }
    return result;
  }
  return value;
}

function bdbTaskFnv1a(value) {
  let hash = 0x811c9dc5;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return (hash >>> 0).toString(16).padStart(8, "0");
}

async function bdbTaskFingerprint(value) {
  const text = JSON.stringify(bdbTaskCanonical(value));
  if (crypto.subtle && typeof crypto.subtle.digest === "function") {
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
    return Array.from(new Uint8Array(digest), (item) => item.toString(16).padStart(2, "0")).join("");
  }
  return bdbTaskFnv1a(text);
}

function bdbTaskActionIdentity(action) {
  const copy = bdbTaskClone(action || {});
  for (const key of ["automation", "presentation", "acceptance", "task", "risk", "trace_id"] ) {
    delete copy[key];
  }
  return copy;
}

function bdbTaskNormalizeLoopId(raw, action) {
  if (typeof raw === "string" && BDB_TASK_LOOP_ID_RE.test(raw)) {
    return { loopId: raw, changed: false, reason: null };
  }
  const fallback = action && action.task && typeof action.task.id === "string"
    ? action.task.id
    : `${(action && action.repo_alias) || "bdb"}-${(action && action.operation) || "task"}-${bdbRandomUuid()}`;
  const source = typeof raw === "string" && raw.trim() ? raw : fallback;
  let normalized = source;
  try {
    normalized = normalized.normalize("NFKD").replace(/[\u0300-\u036f]/g, "");
  } catch (_error) {
  }
  normalized = normalized
    .replace(/[^A-Za-z0-9._:-]+/g, "-")
    .replace(/^[^A-Za-z0-9]+/, "")
    .replace(/-+/g, "-")
    .replace(/[-._:]+$/, "");
  if (!normalized) {
    normalized = `bdb-task-${bdbRandomUuid()}`;
  }
  if (normalized.length > 118) {
    normalized = `${normalized.slice(0, 109).replace(/[-._:]+$/, "")}-${bdbTaskFnv1a(source)}`;
  }
  if (!BDB_TASK_LOOP_ID_RE.test(normalized)) {
    normalized = `bdb-task-${bdbTaskFnv1a(source)}-${bdbRandomUuid().slice(0, 8)}`;
  }
  return {
    loopId: normalized,
    changed: normalized !== raw,
    reason: typeof raw === "string" && raw.trim() ? "unsafe_loop_id_normalized" : "loop_id_generated"
  };
}

function bdbTaskComplexity(action) {
  const operation = action && action.operation;
  const patch = action && action.payload && action.payload.patch;
  const operations = patch && Array.isArray(patch.operations) ? patch.operations.length : 0;
  const acceptance = action && action.acceptance;
  const assertions = acceptance && Array.isArray(acceptance.search_assertions)
    ? acceptance.search_assertions.length
    : 0;
  const searches = action && action.payload && Array.isArray(action.payload.searches)
    ? action.payload.searches.length
    : 0;
  const reads = action && action.payload && Array.isArray(action.payload.reads)
    ? action.payload.reads.length
    : 0;
  const iteration = action && action.automation && Number.isInteger(action.automation.iteration)
    ? action.automation.iteration
    : 1;
  let score = BDB_TASK_READ_OPERATIONS.has(operation) ? 1 : 3;
  score += Math.min(5, Math.ceil(operations / 3));
  score += Math.min(2, Math.ceil(assertions / 3));
  score += Math.min(2, Math.floor(searches / 4));
  score += Math.min(2, Math.floor(reads / 3));
  score += Math.min(6, Math.max(0, iteration - 2));
  return {
    score,
    class: score <= 2 ? "small" : (score <= 5 ? "medium" : "large"),
    suggested_iterations: score <= 2 ? 3 : (score <= 5 ? 6 : 8)
  };
}

function bdbTaskRisk(action) {
  if (!action || !BDB_TASK_MUTATING_OPERATIONS.has(action.operation)) {
    return { level: "read_only", reason: null };
  }
  const operations = action.payload && action.payload.patch && action.payload.patch.operations;
  const risky = Array.isArray(operations)
    ? operations.find((item) => item && BDB_TASK_HIGH_RISK_KINDS.has(item.kind))
    : null;
  if (risky) {
    return { level: "high", reason: `${risky.kind}_requires_assisted` };
  }
  return { level: "bounded_mutation", reason: null };
}

async function bdbTaskConversationBinding(repoAlias, tabId) {
  if (typeof repoAlias !== "string" || !Number.isInteger(tabId) || tabId < 0) {
    return null;
  }
  const stored = await chrome.storage.local.get(BDB_TASK_CONVERSATION_BINDINGS_KEY);
  const raw = stored[BDB_TASK_CONVERSATION_BINDINGS_KEY];
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return null;
  }
  const candidates = Object.values(raw)
    .filter((binding) => (
      binding &&
      typeof binding === "object" &&
      typeof binding.conversation_id === "string" &&
      binding.repo_alias === repoAlias &&
      binding.tab_id === tabId &&
      Number.isFinite(binding.updated_at)
    ))
    .sort((left, right) => right.updated_at - left.updated_at);
  return candidates.length > 0 ? bdbTaskClone(candidates[0]) : null;
}

async function bdbTaskLedger() {
  const stored = await chrome.storage.local.get(BDB_TASK_LEDGER_KEY);
  const raw = stored[BDB_TASK_LEDGER_KEY];
  return raw && typeof raw === "object" && !Array.isArray(raw)
    ? bdbTaskClone(raw)
    : { schema: BDB_TASK_CONTROLLER_SCHEMA, tasks: {}, milestone_runs: {} };
}

async function bdbTaskUpsert(loopId, patch) {
  if (!BDB_TASK_LOOP_ID_RE.test(loopId)) {
    return null;
  }
  return bdbTaskWithStorageLock(async () => {
    const ledger = await bdbTaskLedger();
    const now = Date.now();
    const current = ledger.tasks[loopId] || {
      loop_id: loopId,
      created_at: now,
      last_iteration: 0,
      status: "running",
      operations: []
    };
    const requested = bdbTaskClone(patch);
    const forceStatus = requested.force_status === true;
    delete requested.force_status;
    if (Number.isInteger(requested.last_iteration)) {
      requested.last_iteration = Math.max(current.last_iteration || 0, requested.last_iteration);
    }
    if (Number.isInteger(requested.expected_iteration)) {
      requested.expected_iteration = Math.max(
        current.expected_iteration || 0,
        requested.expected_iteration
      );
    }
    const next = { ...current, ...requested, updated_at: now };
    if (
      !forceStatus &&
      BDB_TASK_TERMINAL_STATUSES.has(current.status) &&
      !BDB_TASK_TERMINAL_STATUSES.has(requested.status)
    ) {
      next.status = current.status;
    }
    if (requested.operation) {
      next.operations = [
        ...(Array.isArray(current.operations) ? current.operations : []),
        {
          iteration: requested.last_iteration || current.last_iteration || 0,
          operation: requested.operation,
          status: requested.last_operation_status || requested.status || "unknown",
          at: now
        }
      ].slice(-20);
      delete next.operation;
      delete next.last_operation_status;
    }
    ledger.tasks[loopId] = next;
    const retained = Object.entries(ledger.tasks)
      .sort((left, right) => (left[1].updated_at || 0) - (right[1].updated_at || 0))
      .slice(-BDB_TASK_MAX_LEDGER);
    ledger.tasks = Object.fromEntries(retained);
    await chrome.storage.local.set({ [BDB_TASK_LEDGER_KEY]: ledger });
    return next;
  });
}

async function bdbTaskMilestoneRunUpsert(milestoneRunId, patch) {
  if (!BDB_TASK_LOOP_ID_RE.test(milestoneRunId || "")) {
    return null;
  }
  return bdbTaskWithStorageLock(async () => {
    const ledger = await bdbTaskLedger();
    const now = Date.now();
    const current = ledger.milestone_runs && ledger.milestone_runs[milestoneRunId]
      ? ledger.milestone_runs[milestoneRunId]
      : {
        milestone_run_id: milestoneRunId,
        status: "running",
        completed_task_ids: [],
        created_at: now
      };
    const requested = bdbTaskClone(patch) || {};
    const next = { ...current, ...requested, updated_at: now };
    if (Array.isArray(next.completed_task_ids)) {
      next.completed_task_ids = [...new Set([
        ...(Array.isArray(current.completed_task_ids) ? current.completed_task_ids : []),
        ...next.completed_task_ids
      ].filter((item) => typeof item === "string"))].slice(-256);
    }
    ledger.milestone_runs = ledger.milestone_runs && typeof ledger.milestone_runs === "object"
      ? { ...ledger.milestone_runs, [milestoneRunId]: next }
      : { [milestoneRunId]: next };
    ledger.milestone_runs = Object.fromEntries(
      Object.entries(ledger.milestone_runs)
        .sort((left, right) => (left[1].updated_at || 0) - (right[1].updated_at || 0))
        .slice(-BDB_TASK_MAX_LEDGER)
    );
    await chrome.storage.local.set({ [BDB_TASK_LEDGER_KEY]: ledger });
    return next;
  });
}

async function bdbTaskRecordDiagnostic(event) {
  const safe = {
    at: Date.now(),
    event: bdbTaskSafeText(event.event, 64) || "unknown",
    loop_id: bdbTaskSafeText(event.loopId, 128),
    iteration: Number.isInteger(event.iteration) ? event.iteration : null,
    operation: bdbTaskSafeText(event.operation, 64),
    reason: bdbTaskSafeText(event.reason, 160),
    status: bdbTaskSafeText(event.status, 64),
    error_code: bdbTaskSafeText(event.errorCode, 64),
    detail: bdbTaskSafeText(event.detail, 300),
    duration_ms: Number.isFinite(event.durationMs) ? Math.max(0, Math.round(event.durationMs)) : null,
    tab_id: Number.isInteger(event.tabId) ? event.tabId : null,
    trace_id: bdbTaskSafeText(event.traceId, 160),
    extension_version: currentExtensionVersion(),
    cache: bdbTaskSafeText(event.cache, 32),
    stage: bdbTaskSafeText(event.stage, 64),
    repo_alias: bdbTaskSafeText(event.repoAlias, 128),
    command_id: bdbTaskSafeText(event.commandId, 160),
    base_sha: bdbTaskSafeText(event.baseSha, 80),
    result_sha: bdbTaskSafeText(event.resultSha, 80)
  };
  await bdbTaskWithStorageLock(async () => {
    const stored = await chrome.storage.local.get(BDB_TASK_DIAGNOSTICS_KEY);
    const diagnostics = Array.isArray(stored[BDB_TASK_DIAGNOSTICS_KEY])
      ? stored[BDB_TASK_DIAGNOSTICS_KEY]
      : [];
    diagnostics.push(safe);
    await chrome.storage.local.set({
      [BDB_TASK_DIAGNOSTICS_KEY]: diagnostics.slice(-BDB_TASK_MAX_DIAGNOSTICS)
    });
  });
}

function bdbTaskResponseError(response) {
  const result = response && response.result;
  const data = result && result.data;
  const error = response && response.error;
  const errorCode = (error && error.code) ||
    (data && data.terminal_error_code) ||
    (result && result.error_code) ||
    null;
  const detail = (data && data.terminal_detail) ||
    (error && error.message) ||
    (result && result.summary) ||
    null;
  if (!errorCode && !detail) {
    return null;
  }
  return {
    error_code: bdbTaskSafeText(errorCode, 64),
    detail: bdbTaskSafeText(detail, 300)
  };
}

function bdbTaskNeedsVisualFeedback(response) {
  return Boolean(
    response &&
    response.result &&
    response.result.acceptance &&
    response.result.acceptance.status === "needs_confirmation"
  );
}

async function bdbTaskMetric(name, amount = 1) {
  await bdbTaskWithStorageLock(async () => {
    const stored = await chrome.storage.local.get(BDB_TASK_METRICS_KEY);
    const metrics = stored[BDB_TASK_METRICS_KEY] && typeof stored[BDB_TASK_METRICS_KEY] === "object"
      ? { ...stored[BDB_TASK_METRICS_KEY] }
      : { schema: "bdb-task-metrics-v1", since: Date.now(), counters: {} };
    metrics.counters = metrics.counters && typeof metrics.counters === "object"
      ? { ...metrics.counters }
      : {};
    metrics.counters[name] = (Number(metrics.counters[name]) || 0) + amount;
    metrics.updated_at = Date.now();
    await chrome.storage.local.set({ [BDB_TASK_METRICS_KEY]: metrics });
  });
}

function bdbTaskPercentile(sortedValues, percentile) {
  if (!Array.isArray(sortedValues) || sortedValues.length === 0) {
    return null;
  }
  const rank = Math.ceil((percentile / 100) * sortedValues.length) - 1;
  return sortedValues[Math.max(0, Math.min(sortedValues.length - 1, rank))];
}

function bdbTaskTimingSummary(events) {
  const buckets = new Map();
  for (const event of events) {
    if (!event || !Number.isFinite(event.duration_ms)) {
      continue;
    }
    const stage = bdbTaskSafeText(event.stage || event.event, 64) || "unknown";
    const current = buckets.get(stage) || [];
    current.push(Math.max(0, Math.round(event.duration_ms)));
    buckets.set(stage, current);
  }
  const stages = {};
  let sampleCount = 0;
  for (const [stage, values] of buckets.entries()) {
    const sorted = [...values].sort((left, right) => left - right);
    sampleCount += sorted.length;
    stages[stage] = {
      count: sorted.length,
      p50_ms: bdbTaskPercentile(sorted, 50),
      p90_ms: bdbTaskPercentile(sorted, 90),
      p99_ms: bdbTaskPercentile(sorted, 99),
      max_ms: sorted[sorted.length - 1]
    };
  }
  const criticalOrder = ["action", "auto", "delivery"];
  return {
    schema: "bdb-flight-recorder-v1",
    sample_count: sampleCount,
    stages,
    critical_path: criticalOrder
      .filter((stage) => stages[stage])
      .map((stage) => ({ stage, ...stages[stage] }))
  };
}

async function bdbTaskCompileAction(action) {
  if (!action || typeof action !== "object" || Array.isArray(action)) {
    return { action, compiler: { changed: false } };
  }
  const automation = action.automation;
  if (!automation || typeof automation !== "object" || automation.mode !== "auto") {
    return { action, compiler: { changed: false } };
  }
  const compiled = bdbTaskClone(action);
  const normalized = bdbTaskNormalizeLoopId(automation.loop_id, action);
  const ledger = await bdbTaskLedger();
  const task = ledger.tasks[normalized.loopId];
  const feedbackContinuation = Boolean(
    automation.continue_after_user_feedback === true &&
    task &&
    task.status === "needs_user" &&
    Number.isInteger(task.last_iteration)
  );
  const iteration = feedbackContinuation
    ? task.last_iteration + 1
    : (Number.isInteger(automation.iteration) && automation.iteration > 0
      ? automation.iteration
      : ((task && Number.isInteger(task.last_iteration) ? task.last_iteration : 0) + 1));
  compiled.automation = {
    ...compiled.automation,
    loop_id: normalized.loopId,
    iteration
  };
  const traceId = typeof compiled.trace_id === "string" && compiled.trace_id
    ? compiled.trace_id
    : `${normalized.loopId}:${iteration}`;
  compiled.trace_id = traceId;
  return {
    action: compiled,
    compiler: {
      changed: normalized.changed || iteration !== automation.iteration || traceId !== action.trace_id,
      loop_id_changed: normalized.changed,
      reason: normalized.reason,
      loop_id: normalized.loopId,
      iteration,
      trace_id: traceId
    }
  };
}

function bdbTaskResponseBaseSha(response) {
  const result = response && response.result;
  if (!result || typeof result !== "object") {
    return null;
  }
  if (typeof result.base_sha === "string") {
    return result.base_sha;
  }
  if (result.context && typeof result.context.base_sha === "string") {
    return result.context.base_sha;
  }
  if (result.data && typeof result.data.base_sha === "string") {
    return result.data.base_sha;
  }
  if (result.promotion && typeof result.promotion.source_commit === "string") {
    return result.promotion.source_commit;
  }
  if (result.verification && typeof result.verification.source_commit === "string") {
    return result.verification.source_commit;
  }
  return null;
}

function bdbTaskResponseResultSha(response) {
  const result = response && response.result;
  if (!result || typeof result !== "object") {
    return null;
  }
  if (typeof result.result_sha === "string") {
    return result.result_sha;
  }
  if (result.data && typeof result.data.result_sha === "string") {
    return result.data.result_sha;
  }
  if (result.promotion && typeof result.promotion.source_commit === "string") {
    return result.promotion.source_commit;
  }
  if (result.verification && typeof result.verification.source_commit === "string") {
    return result.verification.source_commit;
  }
  return null;
}

const BDB_TASK_MUTATION_GUARDS_KEY = "bdbMutationGuardsV1";
const BDB_TASK_MUTATION_GUARD_SCHEMA = "bdb-mutation-guards-v1";

function bdbTaskMutationGuardKey(action, fingerprint) {
  return `${action.repo_alias}:${fingerprint}`;
}

function bdbTaskMutationCommandId(response) {
  if (!response || typeof response !== "object") {
    return null;
  }
  if (typeof response.command_id === "string" && response.command_id.length > 0) {
    return response.command_id;
  }
  const result = response.result;
  return result && typeof result.command_id === "string" && result.command_id.length > 0
    ? result.command_id
    : null;
}

function bdbTaskMutationGuardResolved(response) {
  if (!response || typeof response !== "object") {
    return false;
  }
  if (response.status === "failed") {
    return true;
  }
  if (response.status !== "completed") {
    return false;
  }
  const result = response.result;
  if (!result || typeof result !== "object" || result.status !== "success") {
    return true;
  }
  const promotion = result.promotion;
  const commandId = bdbTaskMutationCommandId(response);
  return Boolean(
    promotion &&
    promotion.status === "promoted" &&
    (typeof promotion.command_id !== "string" || promotion.command_id === commandId)
  );
}

async function bdbTaskMutationGuardDocument() {
  const stored = await chrome.storage.local.get(BDB_TASK_MUTATION_GUARDS_KEY);
  const raw = stored[BDB_TASK_MUTATION_GUARDS_KEY];
  return raw && typeof raw === "object" && !Array.isArray(raw)
    ? bdbTaskClone(raw)
    : { schema: BDB_TASK_MUTATION_GUARD_SCHEMA, entries: {} };
}

async function bdbTaskMutationGuardAcquire(action, fingerprint) {
  return bdbTaskWithStorageLock(async () => {
    const document = await bdbTaskMutationGuardDocument();
    document.entries = document.entries && typeof document.entries === "object"
      ? document.entries
      : {};
    const key = bdbTaskMutationGuardKey(action, fingerprint);
    const existing = document.entries[key];
    if (existing && typeof existing === "object") {
      return { acquired: false, entry: bdbTaskClone(existing) };
    }
    const now = Date.now();
    const entry = {
      repo_alias: action.repo_alias,
      operation: action.operation,
      fingerprint,
      status: "submitting",
      command_id: null,
      response: null,
      created_at: now,
      updated_at: now
    };
    document.entries[key] = entry;
    await chrome.storage.local.set({ [BDB_TASK_MUTATION_GUARDS_KEY]: document });
    return { acquired: true, entry: bdbTaskClone(entry) };
  });
}

async function bdbTaskMutationGuardRecord(action, fingerprint, response) {
  await bdbTaskWithStorageLock(async () => {
    const document = await bdbTaskMutationGuardDocument();
    document.entries = document.entries && typeof document.entries === "object"
      ? document.entries
      : {};
    const key = bdbTaskMutationGuardKey(action, fingerprint);
    const existing = document.entries[key];
    if (!existing || typeof existing !== "object") {
      return;
    }
    if (bdbTaskMutationGuardResolved(response)) {
      delete document.entries[key];
      await chrome.storage.local.set({ [BDB_TASK_MUTATION_GUARDS_KEY]: document });
      return;
    }
    const commandId = bdbTaskMutationCommandId(response) || existing.command_id || null;
    const responseBytes = response && typeof response === "object"
      ? bdbTaskSerializedBytes(response)
      : Number.POSITIVE_INFINITY;
    document.entries[key] = {
      ...existing,
      status: response && typeof response.status === "string" ? response.status : "pending",
      command_id: commandId,
      response: responseBytes <= BDB_TASK_MAX_CACHE_BYTES ? bdbTaskClone(response) : null,
      updated_at: Date.now()
    };
    await chrome.storage.local.set({ [BDB_TASK_MUTATION_GUARDS_KEY]: document });
  });
}

function bdbTaskMutationGuardBlocked(entry, reason) {
  return {
    status: "pending",
    command_id: entry && typeof entry.command_id === "string" ? entry.command_id : null,
    mutation_guard: {
      schema: "bdb-mutation-guard-v1",
      status: "blocked",
      reason,
      operation: entry && entry.operation,
      created_at: entry && entry.created_at,
      updated_at: entry && entry.updated_at
    }
  };
}

async function bdbTaskMutationGuardResume(action, fingerprint) {
  const document = await bdbTaskMutationGuardDocument();
  const entry = document.entries && document.entries[bdbTaskMutationGuardKey(action, fingerprint)];
  if (!entry || typeof entry !== "object") {
    return null;
  }

  let latest = entry.response && typeof entry.response === "object"
    ? bdbTaskClone(entry.response)
    : null;
  try {
    if (latest && latest.status === "completed" && typeof waitForRequiredPromotion === "function") {
      latest = await waitForRequiredPromotion(action, latest);
    } else if (
      typeof entry.command_id === "string" &&
      entry.command_id.length > 0 &&
      typeof pollBdbCommandResult === "function"
    ) {
      latest = await pollBdbCommandResult(action, {
        status: "pending",
        command_id: entry.command_id
      });
    }
  } catch (_error) {
    latest = null;
  }

  if (!latest) {
    return bdbTaskMutationGuardBlocked(
      entry,
      entry.command_id ? "mutation_result_still_unavailable" : "mutation_submission_state_unknown"
    );
  }
  await bdbTaskMutationGuardRecord(action, fingerprint, latest);
  if (bdbTaskMutationGuardResolved(latest)) {
    return latest;
  }
  const guarded = bdbTaskClone(latest);
  guarded.mutation_guard = {
    schema: "bdb-mutation-guard-v1",
    status: "reconciling",
    reason: "existing_mutation_not_yet_terminal",
    command_id: bdbTaskMutationCommandId(latest) || entry.command_id || null
  };
  return guarded;
}

async function bdbTaskCacheDocument() {
  const stored = await chrome.storage.session.get(BDB_TASK_CACHE_KEY);
  const raw = stored[BDB_TASK_CACHE_KEY];
  return raw && typeof raw === "object" && !Array.isArray(raw)
    ? bdbTaskClone(raw)
    : { schema: "bdb-action-cache-v1", entries: {} };
}

async function bdbTaskCacheLookup(action, fingerprint) {
  if (BDB_TASK_MUTATING_OPERATIONS.has(action.operation)) {
    const guarded = await bdbTaskMutationGuardResume(action, fingerprint);
    if (guarded) {
      return guarded;
    }
  }
  const cache = await bdbTaskCacheDocument();
  const entry = cache.entries[fingerprint];
  if (!entry || typeof entry !== "object") {
    return null;
  }
  const mutating = BDB_TASK_MUTATING_OPERATIONS.has(action.operation);
  const ttl = mutating ? BDB_TASK_MUTATION_DEDUP_MS : BDB_TASK_READ_CACHE_MS;
  if (Date.now() - entry.created_at > ttl || typeof entry.base_sha !== "string") {
    return null;
  }
  let current;
  try {
    current = await nativeContext(action.repo_alias);
  } catch (_error) {
    return null;
  }
  if (!current || !current.context || current.context.base_sha !== entry.base_sha) {
    return null;
  }
  const response = bdbTaskClone(entry.response);
  if (response && response.result && typeof response.result === "object") {
    response.result.execution_cache = {
      schema: "bdb-execution-cache-v1",
      status: "hit",
      deduplicated: mutating,
      base_sha: entry.base_sha,
      age_ms: Date.now() - entry.created_at
    };
  }
  return response;
}

async function bdbTaskCacheStore(action, fingerprint, response) {
  if (BDB_TASK_MUTATING_OPERATIONS.has(action.operation)) {
    await bdbTaskMutationGuardRecord(action, fingerprint, response);
  }
  if (!response || response.status !== "completed") {
    return;
  }
  const baseSha = bdbTaskResponseBaseSha(response);
  if (typeof baseSha !== "string" || bdbTaskSerializedBytes(response) > BDB_TASK_MAX_CACHE_BYTES) {
    return;
  }
  const cache = await bdbTaskCacheDocument();
  cache.entries[fingerprint] = {
    created_at: Date.now(),
    base_sha: baseSha,
    operation: action.operation,
    response: bdbTaskClone(response)
  };
  cache.entries = Object.fromEntries(
    Object.entries(cache.entries)
      .sort((left, right) => left[1].created_at - right[1].created_at)
      .slice(-BDB_TASK_MAX_CACHE_ENTRIES)
  );
  await chrome.storage.session.set({ [BDB_TASK_CACHE_KEY]: cache });
}

function bdbTaskChangedFiles(response) {
  const result = response && response.result;
  if (!result || typeof result !== "object") {
    return [];
  }
  if (Array.isArray(result.changed_files)) {
    return result.changed_files.filter((item) => typeof item === "string");
  }
  if (result.promotion && Array.isArray(result.promotion.changed_files)) {
    return result.promotion.changed_files.filter((item) => typeof item === "string");
  }
  return [];
}

async function bdbTaskEvaluateAcceptance(action, response) {
  const acceptance = action && action.acceptance;
  if (!acceptance || typeof acceptance !== "object" || Array.isArray(acceptance)) {
    return null;
  }
  const checks = [];
  const add = (name, passed, detail) => checks.push({ name, passed, detail });
  if (acceptance.schema !== "bdb-acceptance-v1") {
    add("schema", false, "acceptance must use bdb-acceptance-v1");
  }
  const result = response && response.result && typeof response.result === "object"
    ? response.result
    : {};
  if (typeof acceptance.result_status === "string") {
    add(
      "result_status",
      result.status === acceptance.result_status,
      `expected=${acceptance.result_status} actual=${result.status || "missing"}`
    );
  }
  const changed = bdbTaskChangedFiles(response);
  if (Array.isArray(acceptance.changed_files_include)) {
    for (const path of acceptance.changed_files_include.slice(0, 32)) {
      add(`changed:${path}`, changed.includes(path), changed.includes(path) ? "present" : "missing");
    }
  }
  if (acceptance.promotion_required === true) {
    const promoted = Boolean(result.promotion && result.promotion.status === "promoted");
    add("promotion", promoted, promoted ? "promoted" : "promotion missing");
  }
  if (acceptance.tests_required === true) {
    const verified = Boolean(
      result.verification &&
      result.verification.tests &&
      result.verification.tests.status === "success"
    );
    add("tests", verified, verified ? "verified" : "verified successful tests missing");
  }
  const assertions = Array.isArray(acceptance.search_assertions)
    ? acceptance.search_assertions.slice(0, 8)
    : [];
  for (let index = 0; index < assertions.length; index += 1) {
    const assertion = assertions[index];
    if (!assertion || typeof assertion !== "object" || typeof assertion.query !== "string") {
      add(`search:${index}`, false, "invalid search assertion");
      continue;
    }
    const payload = {
      query: assertion.query,
      case_sensitive: assertion.case_sensitive === true,
      max_results: 20
    };
    if (typeof assertion.path === "string" && assertion.path) {
      payload.path_prefixes = [assertion.path];
    }
    try {
      const searchResponse = await repositorySearch({
        schema: ACTION_SCHEMA,
        repo_alias: action.repo_alias,
        operation: SEARCH_TEXT_OPERATION,
        payload,
        presentation: { mode: "compact" }
      });
      const search = searchResponse && searchResponse.result;
      const count = search && Number.isInteger(search.total_matches) ? search.total_matches : -1;
      const minimum = Number.isInteger(assertion.min_matches) ? assertion.min_matches : 0;
      const maximum = Number.isInteger(assertion.max_matches) ? assertion.max_matches : Number.MAX_SAFE_INTEGER;
      const passed = count >= minimum && count <= maximum;
      add(
        `search:${index}`,
        passed,
        `query=${JSON.stringify(assertion.query)} count=${count} expected=${minimum}..${maximum}`
      );
    } catch (error) {
      add(`search:${index}`, false, `search failed: ${bdbTaskSafeText(String(error), 120)}`);
    }
  }
  const passed = checks.length > 0 && checks.every((check) => check.passed);
  const needsVisualConfirmation = acceptance.manual_visual_confirmation_required === true && passed;
  return {
    schema: "bdb-acceptance-result-v1",
    status: needsVisualConfirmation ? "needs_confirmation" : (passed ? "passed" : "unmet"),
    checked_at: Date.now(),
    checks,
    recommended_operation: needsVisualConfirmation
      ? "await_user_visual_feedback"
      : (passed ? "complete" : "inspect_bundle_or_multi_file_patch"),
    ...(needsVisualConfirmation ? {
      confirmation: {
        kind: "visual",
        status: "required",
        instruction: "Poproś użytkownika zwykłą wiadomością o sprawdzenie aplikacji. Nie twórz operacji BDB. Po negatywnej ocenie następna akcja może ustawić automation.continue_after_user_feedback=true."
      }
    } : {})
  };
}

function bdbTaskAttachGuidance(action, response, acceptance, cacheStatus) {
  const copy = bdbTaskClone(response);
  if (!copy || typeof copy !== "object") {
    return response;
  }
  const hasObjectResult = Boolean(
    copy.result && typeof copy.result === "object" && !Array.isArray(copy.result)
  );
  const result = hasObjectResult ? copy.result : null;
  const complexity = bdbTaskComplexity(action);
  const changedFiles = bdbTaskChangedFiles(copy);
  const responseError = bdbTaskResponseError(copy);
  const nextOperation = acceptance
    ? acceptance.recommended_operation
    : (responseError
      ? "recover_from_error"
      : (BDB_TASK_READ_OPERATIONS.has(action.operation)
        ? "multi_file_patch_or_focused_read"
        : "verify_acceptance"));
  const guidance = {
    schema: "bdb-task-guidance-v1",
    trace_id: action.trace_id || null,
    phase: action.task && action.task.phase
      ? action.task.phase
      : (BDB_TASK_READ_OPERATIONS.has(action.operation) ? "analysis" : "implementation"),
    complexity,
    changed_files: changedFiles,
    next_operation: nextOperation,
    cache: cacheStatus
  };
  if (hasObjectResult) {
    copy.result = {
      ...result,
      ...(acceptance ? { acceptance } : {}),
      task_guidance: guidance
    };
  } else {
    if (acceptance) {
      copy.acceptance = acceptance;
    }
    copy.task_guidance = guidance;
  }
  return copy;
}

async function bdbTaskCheckpointStore(decision, action) {
  if (!decision || decision.executed !== true || !decision.response) {
    return;
  }
  if (bdbTaskSerializedBytes(decision.response) > BDB_TASK_MAX_CHECKPOINT_BYTES) {
    await bdbTaskRecordDiagnostic({
      event: "checkpoint_skipped",
      loopId: decision.loopId,
      iteration: decision.iteration,
      operation: action.operation,
      reason: "response_too_large"
    });
    return;
  }
  await bdbTaskWithStorageLock(async () => {
    const stored = await chrome.storage.local.get(BDB_TASK_CHECKPOINTS_KEY);
    const checkpoints = stored[BDB_TASK_CHECKPOINTS_KEY] && typeof stored[BDB_TASK_CHECKPOINTS_KEY] === "object"
      ? { ...stored[BDB_TASK_CHECKPOINTS_KEY] }
      : {};
    const key = `${decision.loopId}:${decision.iteration}`;
    checkpoints[key] = {
      created_at: Date.now(),
      loop_id: decision.loopId,
      iteration: decision.iteration,
      delivered: decision.resultDelivered === true,
      should_continue: decision.shouldContinue === true,
      stop_reason: decision.stopReason || null,
      state_status: decision.state && typeof decision.state.status === "string"
        ? decision.state.status
        : null,
      response: bdbTaskClone(decision.response)
    };
    const retained = Object.entries(checkpoints)
      .sort((left, right) => left[1].created_at - right[1].created_at)
      .slice(-BDB_TASK_MAX_CHECKPOINTS);
    await chrome.storage.local.set({ [BDB_TASK_CHECKPOINTS_KEY]: Object.fromEntries(retained) });
  });
}

async function bdbTaskCheckpointRestore(loopId, iteration) {
  const stored = await chrome.storage.local.get(BDB_TASK_CHECKPOINTS_KEY);
  const checkpoints = stored[BDB_TASK_CHECKPOINTS_KEY];
  const checkpointKey = `${loopId}:${iteration}`;
  const checkpoint = checkpoints && checkpoints[checkpointKey];
  if (!checkpoint) {
    return null;
  }

  const session = await chrome.storage.session.get(null);
  const deliveredState = Object.entries(session)
    .filter(([key, state]) => (
      key.startsWith("bdbAuto:") &&
      key.endsWith(`:${loopId}`) &&
      state &&
      typeof state === "object" &&
      !Array.isArray(state) &&
      state.lastResponseDelivered === true &&
      state.lastResponseIteration === iteration &&
      iteration <= (state.lastIteration || 0) &&
      state.lastResponse
    ))
    .map(([, state]) => state)
    .sort((left, right) => (
      (right.lastResponseDeliveredAt || right.updatedAt || 0) -
      (left.lastResponseDeliveredAt || left.updatedAt || 0)
    ))[0];

  if (deliveredState) {
    if (checkpoint.delivered !== true) {
      await bdbTaskWithStorageLock(async () => {
        const latestStored = await chrome.storage.local.get(BDB_TASK_CHECKPOINTS_KEY);
        const latest = latestStored[BDB_TASK_CHECKPOINTS_KEY] &&
          typeof latestStored[BDB_TASK_CHECKPOINTS_KEY] === "object"
          ? { ...latestStored[BDB_TASK_CHECKPOINTS_KEY] }
          : {};
        if (latest[checkpointKey]) {
          latest[checkpointKey] = {
            ...latest[checkpointKey],
            delivered: true,
            delivered_at: deliveredState.lastResponseDeliveredAt || Date.now()
          };
          await chrome.storage.local.set({ [BDB_TASK_CHECKPOINTS_KEY]: latest });
        }
      });
    }
    return {
      executed: false,
      reason: "iteration_already_processed",
      expectedIteration: Math.max(deliveredState.lastIteration || 0, iteration) + 1,
      loopId,
      iteration,
      durableCheckpoint: true,
      alreadyDelivered: true,
      state: bdbTaskClone(deliveredState)
    };
  }

  if (
    checkpoint.delivered === true ||
    Date.now() - checkpoint.created_at > BDB_TASK_CHECKPOINT_MS ||
    !checkpoint.response
  ) {
    return null;
  }
  return {
    executed: true,
    response: bdbTaskClone(checkpoint.response),
    loopId,
    iteration,
    recoveredResult: true,
    durableCheckpoint: true,
    resultDelivered: false,
    shouldContinue: checkpoint.should_continue === true,
    stopReason: checkpoint.stop_reason,
    state_status: checkpoint.state_status
  };
}

function bdbTaskCheckpointRuntimeStatus(checkpoint) {
  if (
    checkpoint &&
    typeof checkpoint.state_status === "string" &&
    checkpoint.state_status
  ) {
    return checkpoint.state_status;
  }
  const stopReason = checkpoint && typeof checkpoint.stop_reason === "string"
    ? checkpoint.stop_reason
    : (checkpoint && typeof checkpoint.stopReason === "string" ? checkpoint.stopReason : null);
  if (BDB_TASK_TERMINAL_STATUSES.has(stopReason)) {
    return stopReason;
  }
  if (stopReason === "result_not_completed") {
    return "needs_user";
  }
  return "running";
}

async function bdbTaskRestoreCheckpointState(loopId, iteration, tabId, checkpoint) {
  if (checkpoint && checkpoint.alreadyDelivered === true) {
    await bdbTaskUpsert(loopId, {
      last_iteration: iteration,
      expected_iteration: iteration + 1
    });
    return checkpoint.state && typeof checkpoint.state.status === "string"
      ? checkpoint.state.status
      : "running";
  }

  const status = bdbTaskCheckpointRuntimeStatus(checkpoint);
  if (Number.isInteger(tabId) && tabId >= 0) {
    const key = autoStateKey(tabId, loopId);
    const stored = await chrome.storage.session.get(key);
    const current = stored[key] && typeof stored[key] === "object"
      ? stored[key]
      : {};
    const delivered = Boolean(
      current.lastResponseDelivered === true &&
      current.lastResponseIteration === iteration
    );
    await chrome.storage.session.set({
      [key]: {
        ...current,
        lastIteration: Math.max(current.lastIteration || 0, iteration),
        lastResponse: bdbTaskClone(checkpoint.response),
        lastResponseIteration: iteration,
        lastResponseDelivered: delivered,
        ...(delivered && Number.isFinite(current.lastResponseDeliveredAt)
          ? { lastResponseDeliveredAt: current.lastResponseDeliveredAt }
          : {}),
        status,
        updatedAt: Date.now()
      }
    });
  }
  await bdbTaskUpsert(loopId, {
    status,
    last_iteration: iteration,
    expected_iteration: iteration + 1,
    force_status: true
  });
  return status;
}

async function bdbTaskLatestPendingCheckpoint(loopId) {
  const stored = await chrome.storage.local.get(BDB_TASK_CHECKPOINTS_KEY);
  const checkpoints = stored[BDB_TASK_CHECKPOINTS_KEY];
  if (!checkpoints || typeof checkpoints !== "object" || Array.isArray(checkpoints)) {
    return null;
  }
  const pending = Object.values(checkpoints)
    .filter((checkpoint) => (
      checkpoint &&
      checkpoint.loop_id === loopId &&
      checkpoint.delivered !== true &&
      checkpoint.response &&
      Number.isInteger(checkpoint.iteration) &&
      Number.isFinite(checkpoint.created_at) &&
      Date.now() - checkpoint.created_at <= BDB_TASK_CHECKPOINT_MS
    ))
    .sort((left, right) => (
      (right.iteration - left.iteration) || (right.created_at - left.created_at)
    ));
  return pending.length > 0 ? bdbTaskClone(pending[0]) : null;
}

async function bdbTaskResumeAfterVisualFeedback(action, tabId) {
  const metadata = action && action.automation;
  if (
    !metadata ||
    metadata.mode !== "auto" ||
    metadata.continue_after_user_feedback !== true
  ) {
    return { requested: false, resumed: false, reason: null };
  }
  const loopId = metadata.loop_id;
  const iteration = metadata.iteration;
  if (
    !BDB_TASK_LOOP_ID_RE.test(loopId || "") ||
    !Number.isInteger(iteration) ||
    iteration < 2 ||
    !Number.isInteger(tabId) ||
    tabId < 0
  ) {
    return { requested: true, resumed: false, reason: "invalid_visual_feedback_resume" };
  }

  const ledger = await bdbTaskLedger();
  const task = ledger.tasks[loopId];
  const previousIteration = task && Number.isInteger(task.visual_confirmation_iteration)
    ? task.visual_confirmation_iteration
    : (task && Number.isInteger(task.last_iteration) ? task.last_iteration : null);
  if (
    !task ||
    task.status !== "needs_user" ||
    !Number.isInteger(previousIteration) ||
    iteration !== previousIteration + 1
  ) {
    return { requested: true, resumed: false, reason: "visual_feedback_not_expected" };
  }
  const key = autoStateKey(tabId, loopId);
  const session = await chrome.storage.session.get(key);
  const current = session[key] && typeof session[key] === "object"
    ? session[key]
    : {};
  const sessionProof = Boolean(
    current.status === "needs_user" &&
    current.lastIteration === previousIteration &&
    current.lastResponseDelivered === true &&
    bdbTaskNeedsVisualFeedback(current.lastResponse)
  );

  const stored = await chrome.storage.local.get(BDB_TASK_CHECKPOINTS_KEY);
  const checkpoints = stored[BDB_TASK_CHECKPOINTS_KEY];
  const durable = checkpoints && typeof checkpoints === "object"
    ? checkpoints[`${loopId}:${previousIteration}`]
    : null;
  const durableProof = Boolean(
    durable &&
    durable.delivered === true &&
    Number.isFinite(durable.created_at) &&
    Date.now() - durable.created_at <= BDB_TASK_CHECKPOINT_MS &&
    bdbTaskNeedsVisualFeedback(durable.response)
  );
  if (!sessionProof && !durableProof) {
    return { requested: true, resumed: false, reason: "visual_feedback_result_not_delivered" };
  }

  const settings = await getAutoSettings();
  if (!settings.autoEnabled) {
    return { requested: true, resumed: false, reason: "auto_disabled" };
  }
  const now = Date.now();
  const previousResponse = sessionProof ? current.lastResponse : durable.response;
  await chrome.storage.session.set({
    [key]: {
      ...current,
      startedAt: now,
      lastIteration: previousIteration,
      lastResponse: bdbTaskClone(previousResponse),
      lastResponseIteration: previousIteration,
      lastResponseDelivered: true,
      status: "running",
      resumedAfterVisualFeedback: true,
      updatedAt: now
    }
  });
  await bdbTaskUpsert(loopId, {
    status: "running",
    expected_iteration: iteration,
    awaiting_visual_feedback: false,
    visual_feedback_received_at: now,
    force_status: true
  });
  await bdbTaskMetric("visual_feedback_resumes");
  await bdbTaskRecordDiagnostic({
    event: "visual_feedback_resumed",
    loopId,
    iteration,
    operation: action.operation,
    status: "running",
    tabId,
    traceId: action.trace_id
  });
  return {
    requested: true,
    resumed: true,
    reason: null,
    previousIteration,
    unlimited: true
  };
}

submitAction = async function submitActionWithTaskController(action, tabId) {
  const started = Date.now();
  const fingerprint = await bdbTaskFingerprint(bdbTaskActionIdentity(action));
  const mutating = BDB_TASK_MUTATING_OPERATIONS.has(action.operation);
  const cached = await bdbTaskCacheLookup(action, fingerprint);
  if (cached) {
    await bdbTaskMetric(BDB_TASK_MUTATING_OPERATIONS.has(action.operation) ? "deduplicated_mutations" : "cache_hits");
    await bdbTaskRecordDiagnostic({
      event: "action_reused",
      operation: action.operation,
      status: "completed",
      durationMs: Date.now() - started,
      tabId,
      traceId: action.trace_id,
      cache: "hit"
    });
    return bdbTaskAttachGuidance(action, cached, cached.result && cached.result.acceptance, "hit");
  }

  await bdbTaskMetric("cache_misses");
  if (mutating) {
    const guard = await bdbTaskMutationGuardAcquire(action, fingerprint);
    if (!guard.acquired) {
      const guarded = await bdbTaskMutationGuardResume(action, fingerprint);
      if (guarded) {
        await bdbTaskMetric("deduplicated_mutations");
        return bdbTaskAttachGuidance(
          action,
          guarded,
          guarded.result && guarded.result.acceptance,
          "guard"
        );
      }
      return bdbTaskMutationGuardBlocked(guard.entry, "mutation_guard_race");
    }
  }
  let response = await bdbSubmitActionBeforeTaskController(action, tabId);
  const acceptance = await bdbTaskEvaluateAcceptance(action, response);
  response = bdbTaskAttachGuidance(action, response, acceptance, "miss");
  await bdbTaskCacheStore(action, fingerprint, response);
  await bdbTaskMetric("actions_executed");
  await bdbTaskRecordDiagnostic({
    event: "action_completed",
    stage: "action",
    operation: action.operation,
    status: response && response.status,
    errorCode: bdbTaskResponseError(response) && bdbTaskResponseError(response).error_code,
    detail: bdbTaskResponseError(response) && bdbTaskResponseError(response).detail,
    durationMs: Date.now() - started,
    tabId,
    traceId: action.trace_id,
    repoAlias: action.repo_alias,
    commandId: bdbTaskMutationCommandId(response),
    baseSha: bdbTaskResponseBaseSha(response),
    resultSha: bdbTaskResponseResultSha(response),
    cache: "miss"
  });
  return response;
};

considerAuto = async function considerAutoWithTaskController(action, tabId) {
  const started = Date.now();
  const compiled = await bdbTaskCompileAction(action);
  const effective = compiled.action;
  const metadata = effective && effective.automation;
  const loopId = metadata && metadata.loop_id;
  const iteration = metadata && metadata.iteration;
  const operation = effective && effective.operation;
  const traceId = effective && effective.trace_id;
  const milestoneRunId = metadata && typeof metadata.milestone_run_id === "string"
    ? metadata.milestone_run_id
    : null;
  const milestoneId = metadata && typeof metadata.milestone_id === "string"
    ? metadata.milestone_id
    : (effective && effective.task && typeof effective.task.milestone_id === "string"
      ? effective.task.milestone_id
      : null);
  const taskId = metadata && typeof metadata.task_id === "string"
    ? metadata.task_id
    : (effective && effective.task && typeof effective.task.id === "string"
      ? effective.task.id
      : null);

  const feedbackResume = await bdbTaskResumeAfterVisualFeedback(effective, tabId);
  if (feedbackResume.requested && !feedbackResume.resumed) {
    await bdbTaskRecordDiagnostic({
      event: "visual_feedback_resume_rejected",
      loopId,
      iteration,
      operation,
      reason: feedbackResume.reason,
      status: "assisted",
      tabId,
      traceId
    });
    return {
      executed: false,
      reason: feedbackResume.reason,
      expectedIteration: iteration,
      compiler: compiled.compiler
    };
  }

  if (metadata && metadata.mode === "auto") {
    const checkpoint = await bdbTaskCheckpointRestore(loopId, iteration);
    if (checkpoint) {
      const restoredStatus = await bdbTaskRestoreCheckpointState(
        loopId,
        iteration,
        tabId,
        checkpoint
      );
      await bdbTaskMetric("checkpoints_restored");
      await bdbTaskRecordDiagnostic({
        event: "checkpoint_restored",
        loopId,
        iteration,
        operation,
        status: restoredStatus,
        tabId,
        traceId
      });
      return { ...checkpoint, compiler: compiled.compiler };
    }

    const settings = await getAutoSettings();
    if (settings.autoEnabled && settings.autoShadowMode) {
      const shadow = {
        executed: false,
        reason: "shadow_mode",
        expectedIteration: iteration,
        shadow: {
          would_execute: true,
          risk: bdbTaskRisk(effective),
          complexity: bdbTaskComplexity(effective)
        },
        compiler: compiled.compiler
      };
      await bdbTaskMetric("shadow_decisions");
      await bdbTaskRecordDiagnostic({
        event: "auto_shadow_decision",
        loopId,
        iteration,
        operation,
        reason: "shadow_mode",
        tabId,
        traceId
      });
      return shadow;
    }

    const risk = bdbTaskRisk(effective);
    if (settings.autoEnabled && risk.level === "high") {
      await bdbTaskMetric("high_risk_stops");
      await bdbTaskRecordDiagnostic({
        event: "auto_stopped",
        loopId,
        iteration,
        operation,
        reason: "high_risk_requires_assisted",
        tabId,
        traceId
      });
      return {
        executed: false,
        reason: "high_risk_requires_assisted",
        expectedIteration: iteration,
        risk,
        compiler: compiled.compiler
      };
    }
  }

  try {
    const ledgerBefore = loopId ? await bdbTaskLedger() : null;
    const taskBefore = ledgerBefore && ledgerBefore.tasks
      ? ledgerBefore.tasks[loopId]
      : null;
    const decision = await bdbConsiderAutoBeforeTaskController(effective, tabId);
    const replayed = Boolean(decision && decision.executed && decision.recoveredResult);
    const lastError = bdbTaskResponseError(decision && decision.response);
    const stopReason = decision && decision.reason;
    const repeatedBenignStop = Boolean(
      decision &&
      decision.executed !== true &&
      taskBefore &&
      (
        (
          BDB_TASK_BENIGN_REPEAT_STOPS.has(stopReason) &&
          Number.isInteger(iteration) &&
          iteration <= (taskBefore.last_iteration || 0)
        ) ||
        taskBefore.status === stopReason ||
        BDB_TASK_TERMINAL_STATUSES.has(taskBefore.status)
      )
    );
    if (decision && decision.executed === true) {
      // A recovered checkpoint already exists in durable storage. Re-storing the
      // stale recovered decision can race with result delivery and overwrite a
      // freshly persisted `delivered: true` marker back to false, causing an
      // already delivered result to be replayed again. Only original executions
      // create or replace their checkpoint; recovered results remain read-only.
      if (!replayed) {
        await bdbTaskCheckpointStore(decision, effective);
      }
      await bdbTaskMetric(replayed ? "auto_results_replayed" : "auto_executed");
    } else if (!repeatedBenignStop) {
      await bdbTaskMetric(`auto_stop_${(decision && decision.reason) || "unknown"}`);
    }
    if (loopId && !repeatedBenignStop) {
      const awaitingVisualFeedback = Boolean(
        decision &&
        decision.executed === true &&
        bdbTaskNeedsVisualFeedback(decision.response)
      );
      const taskConversationBinding = await bdbTaskConversationBinding(effective.repo_alias, tabId);
      const taskPatch = {
        title: bdbTaskSafeText(effective.task && effective.task.title, 120),
        phase: bdbTaskSafeText(effective.task && effective.task.phase, 64) || (BDB_TASK_READ_OPERATIONS.has(operation) ? "analysis" : "implementation"),
        repo_alias: effective.repo_alias,
        ...(taskConversationBinding ? {
          conversation_id: taskConversationBinding.conversation_id,
          conversation_tab_id: taskConversationBinding.tab_id
        } : {}),
        status: decision && decision.executed
          ? ((decision.state && decision.state.status) || (decision.shouldContinue ? "running" : "stopped"))
          : ((decision && decision.reason) || "stopped"),
        expected_iteration: decision && Number.isInteger(decision.expectedIteration)
          ? decision.expectedIteration
          : ((Number.isInteger(iteration) ? iteration : 0) + 1),
        trace_id: traceId,
        complexity: bdbTaskComplexity(effective),
        risk: bdbTaskRisk(effective),
        last_error: lastError,
        ...(awaitingVisualFeedback ? {
          awaiting_visual_feedback: true,
          visual_confirmation_iteration: iteration
        } : ((feedbackResume && feedbackResume.resumed) ? {
          awaiting_visual_feedback: false
        } : {})),
        ...((decision && decision.executed === true && !replayed) ? {
          last_iteration: Number.isInteger(iteration) ? iteration : 0,
          operation,
          last_operation_status: "executed"
        } : {})
      };
      await bdbTaskUpsert(loopId, taskPatch);
    }
    if (milestoneRunId && !repeatedBenignStop) {
      const progress = decision && decision.milestoneProgress;
      const completedTaskIds = decision && decision.taskCompleted === true && taskId
        ? [taskId]
        : [];
      await bdbTaskMilestoneRunUpsert(milestoneRunId, {
        milestone_id: milestoneId,
        current_task_id: progress && progress.next_task_id
          ? progress.next_task_id
          : taskId,
        status: progress && progress.status
          ? progress.status
          : (decision && decision.stopReason === "milestone_completed" ? "completed" : "running"),
        completed_task_ids: completedTaskIds,
        progress: progress || null,
        last_loop_id: loopId,
        last_iteration: Number.isInteger(iteration) ? iteration : null
      });
    }
    if (!repeatedBenignStop) {
      await bdbTaskRecordDiagnostic({
        event: replayed ? "auto_result_replayed" : (decision && decision.executed ? "auto_executed" : "auto_stopped"),
        stage: "auto",
        loopId,
        iteration,
        operation,
        reason: decision && decision.reason,
        status: decision && decision.executed ? "executed" : "assisted",
        errorCode: lastError && lastError.error_code,
        detail: lastError && lastError.detail,
        durationMs: Date.now() - started,
        tabId,
        traceId,
        repoAlias: effective && effective.repo_alias,
        commandId: bdbTaskMutationCommandId(decision && decision.response),
        baseSha: bdbTaskResponseBaseSha(decision && decision.response),
        resultSha: bdbTaskResponseResultSha(decision && decision.response)
      });
    }
    return { ...decision, compiler: compiled.compiler };
  } catch (error) {
    await bdbTaskMetric("auto_errors");
    await bdbTaskRecordDiagnostic({
      event: "auto_error",
      loopId,
      iteration,
      operation,
      reason: String(error && error.message ? error.message : error),
      status: "error",
      durationMs: Date.now() - started,
      tabId,
      traceId
    });
    throw error;
  }
};

markAutoResultDelivered = async function markAutoResultDeliveredWithTaskCheckpoint(loopId, iteration, tabId) {
  const result = await bdbMarkAutoResultDeliveredBeforeTaskController(loopId, iteration, tabId);
  if (result && result.marked) {
    await bdbTaskWithStorageLock(async () => {
      const stored = await chrome.storage.local.get(BDB_TASK_CHECKPOINTS_KEY);
      const checkpoints = stored[BDB_TASK_CHECKPOINTS_KEY] && typeof stored[BDB_TASK_CHECKPOINTS_KEY] === "object"
        ? { ...stored[BDB_TASK_CHECKPOINTS_KEY] }
        : {};
      const key = `${loopId}:${iteration}`;
      if (checkpoints[key]) {
        checkpoints[key] = { ...checkpoints[key], delivered: true, delivered_at: Date.now() };
        await chrome.storage.local.set({ [BDB_TASK_CHECKPOINTS_KEY]: checkpoints });
      }
    });
    await bdbTaskMetric("results_delivered");
    await bdbTaskRecordDiagnostic({
      event: "auto_result_delivered",
      loopId,
      iteration,
      status: "delivered",
      tabId
    });
  }
  return result;
};

async function bdbTaskSnapshot() {
  const ledger = await bdbTaskLedger();
  const stored = await chrome.storage.local.get(BDB_TASK_CHECKPOINTS_KEY);
  const checkpoints = stored[BDB_TASK_CHECKPOINTS_KEY] && typeof stored[BDB_TASK_CHECKPOINTS_KEY] === "object"
    ? stored[BDB_TASK_CHECKPOINTS_KEY]
    : {};
  const now = Date.now();
  const pendingByLoop = new Map();
  for (const checkpoint of Object.values(checkpoints)) {
    if (
      !checkpoint ||
      checkpoint.delivered === true ||
      !checkpoint.response ||
      typeof checkpoint.loop_id !== "string" ||
      !Number.isInteger(checkpoint.iteration) ||
      !Number.isFinite(checkpoint.created_at) ||
      now - checkpoint.created_at > BDB_TASK_CHECKPOINT_MS
    ) {
      continue;
    }
    const current = pendingByLoop.get(checkpoint.loop_id);
    if (
      !current ||
      checkpoint.iteration > current.iteration ||
      (
        checkpoint.iteration === current.iteration &&
        checkpoint.created_at > current.created_at
      )
    ) {
      pendingByLoop.set(checkpoint.loop_id, checkpoint);
    }
  }
  const tasks = Object.values(ledger.tasks)
    .map((task) => {
      const pending = pendingByLoop.get(task.loop_id);
      return {
        ...task,
        recovery_pending: Boolean(pending),
        recovery_iteration: pending ? pending.iteration : null
      };
    })
    .sort((left, right) => (right.updated_at || 0) - (left.updated_at || 0));
  return {
    schema: "bdb-task-snapshot-v1",
    tasks,
    total: tasks.length,
    milestone_runs: ledger.milestone_runs && typeof ledger.milestone_runs === "object"
      ? Object.values(ledger.milestone_runs).slice(-BDB_TASK_MAX_LEDGER)
      : []
  };
}

async function bdbDiagnosticsSnapshot() {
  const stored = await chrome.storage.local.get([
    BDB_TASK_DIAGNOSTICS_KEY,
    BDB_TASK_METRICS_KEY
  ]);
  const events = Array.isArray(stored[BDB_TASK_DIAGNOSTICS_KEY])
    ? stored[BDB_TASK_DIAGNOSTICS_KEY].slice(-100)
    : [];
  return {
    schema: "bdb-sanitized-browser-diagnostics-v1",
    generated_at: Date.now(),
    extension_version: currentExtensionVersion(),
    release_channel: BDB_TASK_RELEASE_CHANNEL,
    metrics: stored[BDB_TASK_METRICS_KEY] || null,
    flight_recorder: bdbTaskTimingSummary(events),
    events,
    tasks: (await bdbTaskSnapshot()).tasks.slice(0, 20),
    privacy: {
      source_code_included: false,
      action_payloads_included: false,
      credentials_included: false
    }
  };
}

async function bdbHealthSnapshot({ probeNative = false, contentVersion = null } = {}) {
  const settings = await getAutoSettings();
  let native = null;
  if (probeNative) {
    try {
      const response = await sendNative({
        schema: REQUEST_SCHEMA,
        request_id: requestId("health"),
        action: "status"
      });
      native = {
        status: response.status,
        host_version: response.host_version,
        armed: Boolean(response.arm && response.arm.armed)
      };
    } catch (error) {
      native = { status: "unavailable", error: bdbTaskSafeText(String(error), 180) };
    }
  }
  const extensionVersion = currentExtensionVersion();
  return {
    schema: "bdb-health-v1",
    status: native && native.status === "unavailable" ? "degraded" : "ready",
    extension_version: extensionVersion,
    content_version: typeof contentVersion === "string" ? contentVersion : null,
    content_version_match: typeof contentVersion === "string" ? contentVersion === extensionVersion : null,
    release_channel: BDB_TASK_RELEASE_CHANNEL,
    auto: settings,
    native,
    capabilities: {
      action_compiler: true,
      acceptance_checks: true,
      durable_resume: true,
      duplicate_guard: true,
      read_cache: true,
      risk_tiers: true,
      shadow_mode: true,
      visual_feedback_resume: true,
      sanitized_diagnostics: true
    }
  };
}

async function bdbCancelTask(loopId, tabId = null) {
  if (!BDB_TASK_LOOP_ID_RE.test(loopId || "")) {
    throw new Error("Task loop_id has an unsafe format");
  }
  const stored = await chrome.storage.session.get(null);
  const matching = Object.entries(stored).filter(([key, value]) => (
    key.startsWith("bdbAuto:") &&
    key.endsWith(`:${loopId}`) &&
    value &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    (!Number.isInteger(tabId) || key === autoStateKey(tabId, loopId))
  ));
  if (matching.length > 0) {
    await chrome.storage.session.set(Object.fromEntries(matching.map(([key, value]) => [
      key,
      { ...value, status: "cancelled", updatedAt: Date.now() }
    ])));
  }
  await bdbTaskUpsert(loopId, { status: "cancelled", force_status: true });
  await bdbTaskRecordDiagnostic({ event: "task_cancelled", loopId, status: "cancelled" });
  return { schema: "bdb-task-control-v1", loop_id: loopId, status: "cancelled" };
}

async function bdbResumeTask(loopId, tabId) {
  if (!BDB_TASK_LOOP_ID_RE.test(loopId || "")) {
    throw new Error("Task loop_id has an unsafe format");
  }
  const ledger = await bdbTaskLedger();
  const task = ledger.tasks[loopId];
  if (!task) {
    throw new Error("Task is not present in the durable ledger");
  }
  if (!Number.isInteger(tabId) || tabId < 0) {
    throw new Error("Task resume requires a concrete ChatGPT tab");
  }
  const conversationId = typeof task.conversation_id === "string" ? task.conversation_id : null;
  const conversationTabId = Number.isInteger(task.conversation_tab_id)
    ? task.conversation_tab_id
    : null;
  if (conversationId && conversationTabId !== null && tabId !== conversationTabId) {
    await bdbTaskRecordDiagnostic({
      event: "task_resume_conversation_mismatch",
      loopId,
      status: "conversation_mismatch",
      tabId
    });
    return {
      schema: "bdb-task-control-v1",
      loop_id: loopId,
      status: "conversation_mismatch",
      expected_tab_id: conversationTabId,
      conversation_id: conversationId,
      instruction: "Wznów zadanie w rozmowie ChatGPT przypisanej do tego zadania."
    };
  }
  const pendingCheckpoint = await bdbTaskLatestPendingCheckpoint(loopId);
  if (pendingCheckpoint) {
    const restoredStatus = await bdbTaskRestoreCheckpointState(
      loopId,
      pendingCheckpoint.iteration,
      tabId,
      pendingCheckpoint
    );
    await bdbTaskMetric("checkpoint_recovery_requests");
    await bdbTaskRecordDiagnostic({
      event: "task_result_recovery_requested",
      loopId,
      iteration: pendingCheckpoint.iteration,
      status: restoredStatus,
      tabId
    });
    return {
      schema: "bdb-task-control-v1",
      loop_id: loopId,
      status: "recovering_result",
      task_status: restoredStatus,
      expected_iteration: pendingCheckpoint.iteration,
      recovery_only: true,
      recovery_response: bdbTaskClone(pendingCheckpoint.response),
      instruction: "BDB odzyska zapisany wynik bez ponownego wykonania operacji."
    };
  }
  const allSession = await chrome.storage.session.get(null);
  const matchingStates = Object.entries(allSession)
    .filter(([key, value]) => (
      key.startsWith("bdbAuto:") &&
      key.endsWith(`:${loopId}`) &&
      value &&
      typeof value === "object" &&
      !Array.isArray(value)
    ))
    .map(([, value]) => value);
  const key = autoStateKey(tabId, loopId);
  const current = allSession[key] && typeof allSession[key] === "object"
    ? allSession[key]
    : {};
  const observedIterations = matchingStates
    .map((value) => value.lastIteration)
    .filter((value) => Number.isInteger(value));
  const lastIteration = Math.max(
    Number.isInteger(task.last_iteration) ? task.last_iteration : 0,
    ...observedIterations,
    0
  );
  await chrome.storage.session.set({
    [key]: {
      ...current,
      startedAt: Date.now(),
      lastIteration,
      status: "running",
      restoredFromTaskLedger: true,
      updatedAt: Date.now()
    }
  });
  await bdbTaskUpsert(loopId, {
    status: "running",
    expected_iteration: lastIteration + 1,
    force_status: true
  });
  await bdbTaskRecordDiagnostic({ event: "task_resumed", loopId, status: "running" });
  return {
    schema: "bdb-task-control-v1",
    loop_id: loopId,
    status: "running",
    expected_iteration: lastIteration + 1,
    unlimited: true,
    instruction: "Reload the ChatGPT tab if its action panel is not visible."
  };
}

async function bdbClearReadCache() {
  await chrome.storage.session.remove(BDB_TASK_CACHE_KEY);
  await bdbTaskRecordDiagnostic({ event: "cache_cleared", status: "completed" });
  return { schema: "bdb-cache-control-v1", status: "cleared" };
}

async function bdbRecordContentEvent(event, tabId) {
  const value = event && typeof event === "object" && !Array.isArray(event) ? event : {};
  await bdbTaskRecordDiagnostic({
    event: value.event || "content_event",
    loopId: value.loopId,
    iteration: value.iteration,
    operation: value.operation,
    reason: value.reason,
    status: value.status,
    durationMs: value.durationMs,
    tabId,
    traceId: value.traceId
  });
  if (typeof value.metric === "string" && /^[a-z0-9_]{1,64}$/.test(value.metric)) {
    await bdbTaskMetric(value.metric);
  }
  return { recorded: true };
}

globalThis.bdbTaskSnapshot = bdbTaskSnapshot;
globalThis.bdbDiagnosticsSnapshot = bdbDiagnosticsSnapshot;
globalThis.bdbHealthSnapshot = bdbHealthSnapshot;
globalThis.bdbCancelTask = bdbCancelTask;
globalThis.bdbResumeTask = bdbResumeTask;
globalThis.bdbClearReadCache = bdbClearReadCache;
globalThis.bdbRecordContentEvent = bdbRecordContentEvent;

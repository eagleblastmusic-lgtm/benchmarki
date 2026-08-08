"use strict";

const HOST_NAME = "com.bartosz.dev_bridge";
const REQUEST_SCHEMA = "bdb-native-request-v1";
const ACTION_SCHEMA = "bdb-action-v1";
const WORKSPACE_CONTEXT_OPERATION = "workspace_context";
const SEARCH_TEXT_OPERATION = "search_text";
const INSPECT_BUNDLE_OPERATION = "inspect_bundle";
const BDB_EXTENSION_VERSION = "0.4.7";
const MAX_SERIALIZED_BYTES = 1024 * 1024;
const INSPECT_MAX_SEARCHES = 8;
const INSPECT_MAX_READS = 20;
const INSPECT_MAX_READ_LINES = 1000;
const INSPECT_MAX_TOP_MATCHES = 12;
const SEARCH_MAX_QUERY_CHARS = 200;
const SEARCH_MAX_RESULTS = 20;
const SEARCH_MAX_PATH_PREFIXES = 8;
const SEARCH_MAX_EXTENSIONS = 12;
const DEFAULT_WAIT_SECONDS = 30;
const PROMOTION_WAIT_ATTEMPTS = 300;
const PROMOTION_WAIT_MILLISECONDS = 100;
const DEFAULT_AUTO_SETTINGS = Object.freeze({
  autoEnabled: false,
  autoMaxIterations: 4,
  autoMaxMinutes: 10,
  autoShadowMode: false
});
const AUTO_REPLAY_GUARD_KEY = "bdbAutoReplayGuard";
const AUTO_REPLAY_GUARD_LIMIT = 512;
const LOOP_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const REPO_ALIAS_RE = /^[a-z][a-z0-9-]{0,31}$/;
const TERMINAL_VALUES = new Set([
  "done",
  "needs_user",
  "policy_denied",
  "manual_reconciliation_required",
  "failed",
  "cancelled",
  "aborted"
]);
const AUTO_RECOVERABLE_READ_OPERATIONS = new Set([
  WORKSPACE_CONTEXT_OPERATION,
  SEARCH_TEXT_OPERATION,
  INSPECT_BUNDLE_OPERATION,
  "open_read"
]);
const AUTO_RECOVERABLE_READ_ERROR_CODES = new Set([
  "invalid_payload",
  "policy_denied",
  "dirty_source_checkout",
  "mirror_sync_failed",
  "result_too_large"
]);
const inFlightTabs = new Set();
const replayClaimsInFlight = new Set();
const nativeRequests = new Map();
let nativePort = null;
const NATIVE_REQUEST_TIMEOUT_MILLISECONDS = 125 * 1000;

function requestId(prefix) {
  const bytes = new Uint8Array(12);
  crypto.getRandomValues(bytes);
  return `${prefix}-${Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("")}`;
}

function bdbRandomUuidFromRandomValues() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0"));
  return `${hex.slice(0, 4).join("")}-${hex.slice(4, 6).join("")}-${hex
    .slice(6, 8)
    .join("")}-${hex.slice(8, 10).join("")}-${hex.slice(10).join("")}`;
}

function bdbRandomUuid() {
  if (typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return bdbRandomUuidFromRandomValues();
}

function serializedSize(value) {
  return new TextEncoder().encode(JSON.stringify(value)).byteLength;
}

function validateJsonObject(value, field) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${field} must be an object`);
  }
  if (serializedSize(value) > MAX_SERIALIZED_BYTES) {
    throw new Error(`${field} exceeds the 1 MiB limit`);
  }
}

function validateRepoAlias(value) {
  if (typeof value !== "string" || !REPO_ALIAS_RE.test(value)) {
    throw new Error("Repository alias has an unsafe format");
  }
  return value;
}

function currentExtensionVersion() {
  if (chrome.runtime && typeof chrome.runtime.getManifest === "function") {
    const manifest = chrome.runtime.getManifest();
    return manifest && typeof manifest.version === "string"
      ? manifest.version
      : BDB_EXTENSION_VERSION;
  }
  return null;
}

function versionedNativeRequest(request) {
  const version = currentExtensionVersion();
  return version ? { ...request, client_version: version } : request;
}

function validateNativeVersion(response) {
  const version = currentExtensionVersion();
  if (version && response.host_version !== version) {
    throw new Error(
      `BDB version mismatch: extension=${version}, native_host=${response.host_version || "missing"}. ` +
      "Reload the extension and the ChatGPT tab."
    );
  }
}

function sendNativeOnce(request) {
  const nativeRequest = versionedNativeRequest(request);
  validateJsonObject(nativeRequest, "native request");
  return new Promise((resolve, reject) => {
    chrome.runtime.sendNativeMessage(HOST_NAME, nativeRequest, (response) => {
      const runtimeError = chrome.runtime.lastError;
      if (runtimeError) {
        reject(new Error(runtimeError.message || "Native host unavailable"));
        return;
      }
      try {
        validateJsonObject(response, "native response");
        validateNativeVersion(response);
        resolve(response);
      } catch (error) {
        reject(error);
      }
    });
  });
}

function rejectNativeRequests(message) {
  for (const pending of nativeRequests.values()) {
    clearTimeout(pending.timer);
    pending.reject(new Error(message));
  }
  nativeRequests.clear();
}

function persistentNativePort() {
  if (nativePort) {
    return nativePort;
  }
  if (typeof chrome.runtime.connectNative !== "function") {
    return null;
  }
  const port = chrome.runtime.connectNative(HOST_NAME);
  port.onMessage.addListener((response) => {
    try {
      validateJsonObject(response, "native response");
      validateNativeVersion(response);
      const pending = nativeRequests.get(response.request_id);
      if (!pending) {
        return;
      }
      nativeRequests.delete(response.request_id);
      clearTimeout(pending.timer);
      pending.resolve(response);
    } catch (error) {
      rejectNativeRequests(error instanceof Error ? error.message : String(error));
    }
  });
  port.onDisconnect.addListener(() => {
    const runtimeError = chrome.runtime.lastError;
    nativePort = null;
    rejectNativeRequests(
      (runtimeError && runtimeError.message) || "Persistent Native Host disconnected"
    );
  });
  nativePort = port;
  return nativePort;
}

function sendNative(request) {
  const nativeRequest = versionedNativeRequest(request);
  validateJsonObject(nativeRequest, "native request");
  const port = persistentNativePort();
  if (!port) {
    return sendNativeOnce(nativeRequest);
  }
  if (nativeRequests.has(nativeRequest.request_id)) {
    return Promise.reject(new Error("Duplicate native request_id"));
  }
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      nativeRequests.delete(nativeRequest.request_id);
      reject(new Error("Persistent Native Host request timed out"));
    }, NATIVE_REQUEST_TIMEOUT_MILLISECONDS);
    nativeRequests.set(nativeRequest.request_id, { resolve, reject, timer });
    try {
      port.postMessage(nativeRequest);
    } catch (error) {
      nativeRequests.delete(nativeRequest.request_id);
      clearTimeout(timer);
      nativePort = null;
      reject(error);
    }
  });
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function recoverableNativeFailure(response) {
  return Boolean(
    response &&
    response.status === "failed" &&
    response.error &&
    response.error.code === "internal_error"
  );
}

async function sendNativeSubmission(request) {
  let response;
  try {
    response = await sendNative(request);
  } catch (_firstError) {
    const recovered = await recoverSubmissionByNonce(request);
    if (recovered !== null) {
      return recovered;
    }
    await sleep(100);
    return sendNative(request);
  }
  if (!recoverableNativeFailure(response)) {
    return response;
  }
  await sleep(100);
  return sendNative(request);
}

async function nativeContext(repoAlias, { syncMirror = false } = {}) {
  return sendNative({
    schema: REQUEST_SCHEMA,
    request_id: requestId("workspace-context"),
    action: "context",
    repo_alias: validateRepoAlias(repoAlias),
    sync_mirror: syncMirror === true
  });
}

async function nativeSubmissionNonceLookup(repoAlias, clientSubmissionNonce) {
  if (typeof clientSubmissionNonce !== "string" || clientSubmissionNonce.length === 0) {
    throw new Error("client_submission_nonce must be a non-empty string");
  }
  return sendNative({
    schema: REQUEST_SCHEMA,
    request_id: requestId("submission-nonce-lookup"),
    action: "lookup_submission_nonce",
    repo_alias: validateRepoAlias(repoAlias),
    client_submission_nonce: clientSubmissionNonce
  });
}

async function recoverSubmissionByNonce(request) {
  const action = request && request.bdb_action;
  if (
    !action ||
    typeof action.repo_alias !== "string" ||
    typeof action.client_submission_nonce !== "string" ||
    action.client_submission_nonce.length === 0
  ) {
    return null;
  }

  let lookup;
  try {
    lookup = await nativeSubmissionNonceLookup(
      action.repo_alias,
      action.client_submission_nonce
    );
  } catch (_lookupError) {
    return null;
  }
  if (
    !lookup ||
    lookup.status !== "submission_nonce_found" ||
    typeof lookup.command_id !== "string" ||
    lookup.command_id.length === 0
  ) {
    return null;
  }
  return {
    ...lookup,
    request_id: request.request_id,
    status: "accepted",
    submission_nonce_recovered: true,
    lookup_request_id: lookup.request_id
  };
}

async function workspaceContext(action) {
  const repoAlias = validateRepoAlias(action.repo_alias);
  const native = await nativeContext(repoAlias, { syncMirror: true });
  return {
    schema: native.schema,
    request_id: native.request_id,
    status: "completed",
    repo_alias: repoAlias,
    result: {
      status: "success",
      operation: WORKSPACE_CONTEXT_OPERATION,
      context: native.context,
      arm: native.arm
    }
  };
}

async function repositorySearch(action) {
  const repoAlias = validateRepoAlias(action.repo_alias);
  return sendNative({
    schema: REQUEST_SCHEMA,
    request_id: requestId("search-text"),
    action: "search_text",
    wait_seconds: DEFAULT_WAIT_SECONDS,
    bdb_action: { ...action, repo_alias: repoAlias }
  });
}

function inspectBundlePreflight(action) {
  const payload = action && action.payload !== undefined ? action.payload : {};
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return "inspect_bundle payload must be an object";
  }

  const searches = payload.searches === undefined ? [] : payload.searches;
  if (!Array.isArray(searches) || searches.length > INSPECT_MAX_SEARCHES) {
    return `inspect_bundle searches must contain at most ${INSPECT_MAX_SEARCHES} items`;
  }
  for (const item of searches) {
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      return "inspect_bundle search items must be objects";
    }
    const query = item.query;
    if (
      typeof query !== "string" ||
      !query.trim() ||
      query.length > SEARCH_MAX_QUERY_CHARS ||
      query.includes("\0") ||
      query.includes("\r") ||
      query.includes("\n")
    ) {
      return `search_text payload.query must contain 1-${SEARCH_MAX_QUERY_CHARS} characters on one line`;
    }
    if (item.case_sensitive !== undefined && typeof item.case_sensitive !== "boolean") {
      return "search_text payload.case_sensitive must be boolean";
    }
    if (
      item.max_results !== undefined &&
      (!Number.isInteger(item.max_results) || item.max_results < 1 || item.max_results > SEARCH_MAX_RESULTS)
    ) {
      return `search_text payload.max_results must be between 1 and ${SEARCH_MAX_RESULTS}`;
    }
    const pathPrefixes = item.path_prefixes === undefined ? [] : item.path_prefixes;
    if (
      !Array.isArray(pathPrefixes) ||
      pathPrefixes.length > SEARCH_MAX_PATH_PREFIXES ||
      !pathPrefixes.every((value) => typeof value === "string")
    ) {
      return `search_text payload.path_prefixes must be a list of at most ${SEARCH_MAX_PATH_PREFIXES} strings`;
    }
    if (
      pathPrefixes.some(
        (value) => !value.trim().replace(/\\/g, "/").replace(/\/+$/, "")
      )
    ) {
      return "search_text path prefixes must not be empty";
    }
    const extensions = item.extensions === undefined ? [] : item.extensions;
    if (
      !Array.isArray(extensions) ||
      extensions.length > SEARCH_MAX_EXTENSIONS ||
      !extensions.every((value) => typeof value === "string")
    ) {
      return `search_text payload.extensions must be a list of at most ${SEARCH_MAX_EXTENSIONS} strings`;
    }
    for (const extension of extensions) {
      if (!/^\.[a-z0-9]{1,12}$/i.test(extension)) {
        return `Unsafe search_text extension: ${extension}`;
      }
    }
  }

  const reads = payload.reads === undefined ? [] : payload.reads;
  if (!Array.isArray(reads) || reads.length > INSPECT_MAX_READS) {
    return `inspect_bundle reads must contain at most ${INSPECT_MAX_READS} items`;
  }
  for (const item of reads) {
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      return "inspect_bundle read items must be objects";
    }
    if (typeof item.path !== "string") {
      return "inspect_bundle read.path must be a string";
    }
    const start = item.start_line === undefined ? 1 : item.start_line;
    const end = item.end_line === undefined
      ? (Number.isInteger(start) ? start + 399 : 0)
      : item.end_line;
    if (
      !Number.isInteger(start) ||
      !Number.isInteger(end) ||
      start < 1 ||
      end < start ||
      end - start + 1 > INSPECT_MAX_READ_LINES
    ) {
      return `inspect_bundle read ranges may contain at most ${INSPECT_MAX_READ_LINES} lines`;
    }
  }

  const includeTree = payload.include_tree;
  const includeSymbols = payload.include_symbols;
  if (
    (includeTree !== undefined && typeof includeTree !== "boolean") ||
    (includeSymbols !== undefined && typeof includeSymbols !== "boolean")
  ) {
    return "inspect_bundle include_tree/include_symbols must be boolean";
  }

  const readTopMatches = payload.read_top_matches;
  if (
    readTopMatches !== undefined &&
    typeof readTopMatches !== "boolean" &&
    !(
      Number.isInteger(readTopMatches) &&
      readTopMatches >= 0 &&
      readTopMatches <= INSPECT_MAX_TOP_MATCHES
    )
  ) {
    return `inspect_bundle read_top_matches must be boolean or 0-${INSPECT_MAX_TOP_MATCHES}`;
  }
  return null;
}

async function repositoryInspection(action) {
  const repoAlias = validateRepoAlias(action.repo_alias);
  const preflightError = inspectBundlePreflight(action);
  if (preflightError) {
    return {
      schema: "bdb-native-response-v1",
      host_version: currentExtensionVersion() || BDB_EXTENSION_VERSION,
      request_id: requestId("inspect-bundle-preflight"),
      status: "failed",
      client_preflight: true,
      error: {
        code: "invalid_payload",
        message: preflightError,
        details: {
          rule_id: "inspect_bundle.client_preflight",
          phase: "client_preflight",
          effect_started: false
        }
      }
    };
  }
  return sendNative({
    schema: REQUEST_SCHEMA,
    request_id: requestId("inspect-bundle"),
    action: "inspect_bundle",
    wait_seconds: DEFAULT_WAIT_SECONDS,
    bdb_action: { ...action, repo_alias: repoAlias }
  });
}

function requiresPromotion(action) {
  const promotion = action && action.promotion;
  return Boolean(
    promotion &&
    typeof promotion === "object" &&
    !Array.isArray(promotion) &&
    promotion.mode === "required"
  );
}

function withPromotion(response, promotion) {
  const result = response && response.result && typeof response.result === "object"
    ? response.result
    : {};
  const verification = {
    schema: "bdb-post-action-verification-v1",
    status: promotion && promotion.status === "promoted" && result.status === "success"
      ? "verified"
      : "needs_user",
    command_id: promotion && promotion.command_id,
    changed_files: promotion && promotion.changed_files,
    file_sha256: promotion && promotion.file_sha256,
    tests: {
      status: result.status,
      exit_code: result.exit_code,
      stdout_sha256: result.stdout_sha256,
      stderr_sha256: result.stderr_sha256
    },
    source_commit: promotion && promotion.source_commit,
    mirror_sync: promotion && promotion.mirror_sync
  };
  return { ...response, result: { ...result, promotion, verification } };
}

async function waitForRequiredPromotion(action, response) {
  if (!requiresPromotion(action)) {
    return response;
  }
  const result = response && response.result;
  const dataOperation =
    result &&
    result.data &&
    typeof result.data === "object" &&
    !Array.isArray(result.data)
      ? result.data.operation
      : null;

  const legacyExactResult = Boolean(
    result &&
    result.status === "success" &&
    result.executor_version === "0.5.0-ghb0" &&
    [
      "Command effect recorded",
      "Idempotent effect replay",
      "Recovered PLANNED-AFTER effect"
    ].includes(result.summary) &&
    Number.isInteger(result.workspace_revision_before) &&
    result.workspace_revision_after ===
      result.workspace_revision_before + 1 &&
    Array.isArray(result.changed_files) &&
    result.changed_files.length === 1 &&
    typeof result.changed_files[0] === "string" &&
    typeof result.diff === "string" &&
    result.diff.length > 0
  );

  const successfulPatch = Boolean(
    response &&
    response.status === "completed" &&
    result &&
    result.status === "success" &&
    (
      dataOperation === "multi_file_patch" ||
      dataOperation === "replace_exact_and_test" ||
      legacyExactResult
    )
  );

  if (!successfulPatch) {
    return response;
  }

  const commandId = response.command_id || result.command_id;
  if (typeof commandId !== "string" || commandId.length === 0) {
    return withPromotion(response, {
      status: "needs_user",
      reason: "completed_result_has_no_command_id"
    });
  }

  for (let attempt = 0; attempt < PROMOTION_WAIT_ATTEMPTS; attempt += 1) {
    const contextResponse = await nativeContext(action.repo_alias);
    const context = contextResponse && contextResponse.context;
    const receipt = context && context.latest_promotion;
    if (
      receipt &&
      receipt.status === "promoted" &&
      receipt.command_id === commandId &&
      Array.isArray(context.source_changes) &&
      context.source_changes.length === 0
    ) {
      return withPromotion(response, receipt);
    }
    await sleep(PROMOTION_WAIT_MILLISECONDS);
  }

  return withPromotion(response, {
    status: "needs_user",
    reason: "promotion_not_observed",
    command_id: commandId
  });
}

async function submitAction(action, tabId) {
  validateJsonObject(action, "BDB action");
  if (action.schema !== ACTION_SCHEMA) {
    throw new Error(`Only ${ACTION_SCHEMA} is supported`);
  }
  if (!Number.isInteger(tabId) || tabId < 0) {
    throw new Error("A concrete sender tab is required");
  }
  if (inFlightTabs.has(tabId)) {
    throw new Error("This tab already has a BDB action in progress");
  }
  inFlightTabs.add(tabId);
  try {
    if (action.operation === WORKSPACE_CONTEXT_OPERATION) {
      return await workspaceContext(action);
    }
    if (action.operation === SEARCH_TEXT_OPERATION) {
      return await repositorySearch(action);
    }
    if (action.operation === INSPECT_BUNDLE_OPERATION) {
      return await repositoryInspection(action);
    }
    const actionWithSession = typeof action.session_id === "string" && action.session_id.length > 0
      ? action
      : { ...action, session_id: bdbRandomUuid(), sequence: action.sequence || 1 };
    const preparedAction = (
      typeof actionWithSession.client_submission_nonce === "string" &&
      actionWithSession.client_submission_nonce.length > 0
    )
      ? actionWithSession
      : { ...actionWithSession, client_submission_nonce: bdbRandomUuidFromRandomValues() };
    const request = {
      schema: REQUEST_SCHEMA,
      request_id: requestId("submit"),
      action: "submit_action",
      wait_seconds: DEFAULT_WAIT_SECONDS,
      bdb_action: preparedAction
    };
    const response = await sendNativeSubmission(request);
    return await waitForRequiredPromotion(preparedAction, response);
  } finally {
    inFlightTabs.delete(tabId);
  }
}

function normalizeAutoSettings(raw) {
  const enabled = raw.autoEnabled === true;
  const shadowMode = raw.autoShadowMode === true;
  const iterations = Number.isInteger(raw.autoMaxIterations) ? raw.autoMaxIterations : DEFAULT_AUTO_SETTINGS.autoMaxIterations;
  const minutes = Number.isInteger(raw.autoMaxMinutes) ? raw.autoMaxMinutes : DEFAULT_AUTO_SETTINGS.autoMaxMinutes;
  if (iterations < 1 || iterations > 30 || minutes < 1 || minutes > 30) {
    throw new Error("AUTO limits are outside the allowed range");
  }
  return {
    autoEnabled: enabled,
    autoMaxIterations: iterations,
    autoMaxMinutes: minutes,
    autoShadowMode: shadowMode
  };
}

async function getAutoSettings() {
  const stored = await chrome.storage.local.get(Object.keys(DEFAULT_AUTO_SETTINGS));
  return normalizeAutoSettings({ ...DEFAULT_AUTO_SETTINGS, ...stored });
}

async function setAutoSettings(settings) {
  const normalized = normalizeAutoSettings(settings);
  await chrome.storage.local.set(normalized);
  return normalized;
}

function automationMetadata(action) {
  const metadata = action && action.automation;
  if (!metadata || typeof metadata !== "object" || Array.isArray(metadata) || metadata.mode !== "auto") {
    return null;
  }
  if (typeof metadata.loop_id !== "string" || !LOOP_ID_RE.test(metadata.loop_id)) {
    throw new Error("AUTO loop_id has an unsafe format");
  }
  if (!Number.isInteger(metadata.iteration) || metadata.iteration < 1) {
    throw new Error("AUTO iteration must be a positive integer");
  }
  if (
    metadata.continue_on_failure !== undefined &&
    typeof metadata.continue_on_failure !== "boolean"
  ) {
    throw new Error("AUTO continue_on_failure must be boolean when present");
  }
  return {
    loopId: metadata.loop_id,
    iteration: metadata.iteration,
    continueOnFailure: metadata.continue_on_failure === true
  };
}

function autoStateKey(tabId, loopId) {
  return `bdbAuto:${tabId}:${loopId}`;
}

async function markAutoResultDelivered(loopId, iteration, tabId) {
  if (typeof loopId !== "string" || !LOOP_ID_RE.test(loopId)) {
    throw new Error("AUTO loop_id has an unsafe format");
  }
  if (!Number.isInteger(iteration) || iteration < 1) {
    throw new Error("AUTO iteration must be a positive integer");
  }
  if (!Number.isInteger(tabId) || tabId < 0) {
    throw new Error("AUTO delivery receipt requires a concrete sender tab");
  }

  const key = autoStateKey(tabId, loopId);
  const stored = await chrome.storage.session.get(key);
  const state = stored[key];
  if (!state || typeof state !== "object" || Array.isArray(state)) {
    return { marked: false, reason: "auto_state_missing" };
  }
  if (state.lastResponseIteration !== iteration || !state.lastResponse) {
    return { marked: false, reason: "cached_result_missing" };
  }

  state.lastResponseDelivered = true;
  state.lastResponseDeliveredAt = Date.now();
  state.updatedAt = Date.now();
  await chrome.storage.session.set({ [key]: state });
  return { marked: true };
}

function autoReplayKey(loopId, iteration) {
  return `${loopId}:${iteration}`;
}

async function claimAutoReplay(loopId, iteration) {
  const key = autoReplayKey(loopId, iteration);
  if (replayClaimsInFlight.has(key)) {
    return false;
  }
  replayClaimsInFlight.add(key);
  try {
    const stored = await chrome.storage.local.get(AUTO_REPLAY_GUARD_KEY);
    const raw = stored[AUTO_REPLAY_GUARD_KEY];
    const guard = raw && typeof raw === "object" && !Array.isArray(raw) ? { ...raw } : {};
    if (Object.prototype.hasOwnProperty.call(guard, key)) {
      return false;
    }
    guard[key] = Date.now();
    const entries = Object.entries(guard)
      .filter(([entryKey, timestamp]) => typeof entryKey === "string" && Number.isFinite(timestamp))
      .sort((left, right) => left[1] - right[1])
      .slice(-AUTO_REPLAY_GUARD_LIMIT);
    await chrome.storage.local.set({ [AUTO_REPLAY_GUARD_KEY]: Object.fromEntries(entries) });
    return true;
  } finally {
    replayClaimsInFlight.delete(key);
  }
}

function bdbExplicitTerminalValue(value) {
  if (typeof value !== "string") {
    return null;
  }
  const normalized = value.trim().toLowerCase();
  return TERMINAL_VALUES.has(normalized) ? normalized : null;
}

function structuredTerminalValue(response) {
  const result = response && response.result;
  const resultObject = result && typeof result === "object" && !Array.isArray(result)
    ? result
    : null;
  const data = resultObject && resultObject.data && typeof resultObject.data === "object"
    ? resultObject.data
    : null;
  const error = response && response.error && typeof response.error === "object"
    ? response.error
    : null;

  if (resultObject) {
    if (resultObject.acceptance && resultObject.acceptance.status === "passed") {
      return "done";
    }
    if (resultObject.acceptance && resultObject.acceptance.status === "needs_confirmation") {
      return "needs_user";
    }
    if (
      resultObject.task_guidance &&
      resultObject.task_guidance.next_operation === "complete"
    ) {
      return "done";
    }
  }

  // Only protocol-owned status fields may control the AUTO state machine.
  // Repository contents, search queries, diffs, logs and arbitrary nested text
  // are data and must never be interpreted as execution status.
  for (const candidate of [
    error && error.code,
    data && data.terminal_error_code,
    resultObject && resultObject.error_code,
    resultObject && resultObject.status,
    response && response.status
  ]) {
    const terminal = bdbExplicitTerminalValue(candidate);
    if (terminal) {
      return terminal;
    }
  }

  if (response && response.status === "failed") {
    return "failed";
  }
  if (resultObject && resultObject.status === "failed") {
    return "failed";
  }
  return null;
}

function isRecoverableProfileFailure(metadata, response) {
  const result = response && response.result;
  const data = result && result.data;
  return Boolean(
    metadata.continueOnFailure &&
    response &&
    response.status === "completed" &&
    result &&
    (result.status === "failed" || result.status === "timeout") &&
    data &&
    data.operation === "multi_file_patch" &&
    data.rollback_performed === true &&
    data.checkpoint_state === "rolled_back"
  );
}

function isRecoverableReadFailure(action, metadata, response) {
  const errorCode = response && response.error && response.error.code;
  return Boolean(
    metadata.continueOnFailure &&
    action &&
    AUTO_RECOVERABLE_READ_OPERATIONS.has(action.operation) &&
    response &&
    response.status === "failed" &&
    typeof errorCode === "string" &&
    AUTO_RECOVERABLE_READ_ERROR_CODES.has(errorCode)
  );
}

async function considerAuto(action, tabId) {
  const metadata = automationMetadata(action);
  if (!metadata) {
    return { executed: false, reason: "action_not_auto" };
  }
  const settings = await getAutoSettings();
  if (!settings.autoEnabled) {
    return { executed: false, reason: "auto_disabled" };
  }
  if (!Number.isInteger(tabId) || tabId < 0) {
    throw new Error("AUTO requires a concrete sender tab");
  }
  const key = autoStateKey(tabId, metadata.loopId);
  const stored = await chrome.storage.session.get(key);
  const storedState = stored[key];
  if (metadata.iteration === 1 && !storedState) {
    let native;
    try {
      native = await nativeContext(action.repo_alias);
    } catch (error) {
      return {
        executed: false,
        reason: "native_host_unavailable",
        expectedIteration: metadata.iteration,
        detail: String(error && error.message ? error.message : error).slice(0, 240)
      };
    }
    if (!native || !native.arm || native.arm.armed !== true) {
      return {
        executed: false,
        reason: "native_host_disarmed",
        expectedIteration: metadata.iteration,
        arm: native && native.arm ? native.arm : { armed: false }
      };
    }
  }
  const now = Date.now();
  const state = storedState || {
    startedAt: now,
    lastIteration: 0,
    status: "running",
    iterationCeiling: settings.autoMaxIterations
  };
  if (!Number.isInteger(state.iterationCeiling) || state.iterationCeiling < state.lastIteration) {
    state.iterationCeiling = Math.max(state.lastIteration, settings.autoMaxIterations);
  }
  if (
    state.lastResponse &&
    typeof state.lastResponse === "object" &&
    !Array.isArray(state.lastResponse) &&
    state.lastResponseDelivered === true &&
    state.lastResponseIteration === metadata.iteration &&
    metadata.iteration <= state.lastIteration
  ) {
    return {
      executed: false,
      reason: "iteration_already_processed",
      expectedIteration: state.lastIteration + 1,
      state
    };
  }
  if (
    state.lastResponse &&
    typeof state.lastResponse === "object" &&
    !Array.isArray(state.lastResponse) &&
    state.lastResponseDelivered !== true &&
    state.lastResponseIteration === metadata.iteration &&
    metadata.iteration <= state.lastIteration
  ) {
    return {
      executed: true,
      response: state.lastResponse,
      loopId: metadata.loopId,
      iteration: metadata.iteration,
      recoveredResult: true,
      resultDelivered: false,
      shouldContinue: false,
      stopReason: state.status === "running" ? "iteration_already_processed" : state.status,
      state
    };
  }
  if (state.status !== "running") {
    return { executed: false, reason: "loop_not_running", state };
  }
  if (metadata.iteration > state.iterationCeiling) {
    return {
      executed: false,
      reason: "iteration_limit",
      expectedIteration: state.lastIteration + 1,
      allowedThrough: state.iterationCeiling,
      state
    };
  }
  if (now - state.startedAt > settings.autoMaxMinutes * 60 * 1000) {
    state.status = "time_limit";
    await chrome.storage.session.set({ [key]: state });
    return { executed: false, reason: "time_limit", state };
  }
  if (metadata.iteration !== state.lastIteration + 1) {
    return { executed: false, reason: "non_sequential_iteration", state };
  }
  if (!await claimAutoReplay(metadata.loopId, metadata.iteration)) {
    return { executed: false, reason: "replay_guard", state };
  }

  const response = await submitAction(action, tabId);
  const recoverableFailure = isRecoverableProfileFailure(metadata, response);
  const recoverableReadFailure = isRecoverableReadFailure(action, metadata, response);
  const recoverableNativeError = Boolean(
    response &&
    response.status === "failed" &&
    response.error &&
    response.error.code === "internal_error"
  );
  const terminal = recoverableFailure || recoverableReadFailure || recoverableNativeError
    ? null
    : structuredTerminalValue(response);
  const completed = response.status === "completed";
  const canContinue = completed || recoverableReadFailure || recoverableNativeError;
  state.lastIteration = metadata.iteration;
  state.lastCommandId = response.command_id || null;
  state.lastResponse = response;
  state.lastResponseIteration = metadata.iteration;
  state.lastResponseDelivered = false;
  state.updatedAt = Date.now();
  state.status = terminal || (canContinue ? "running" : "needs_user");
  await chrome.storage.session.set({ [key]: state });

  const shouldContinue = canContinue && !terminal && metadata.iteration < state.iterationCeiling;
  return {
    executed: true,
    response,
    loopId: metadata.loopId,
    iteration: metadata.iteration,
    recoverableFailure,
    recoverableReadFailure,
    recoverableNativeError,
    shouldContinue,
    stopReason: terminal || (canContinue ? null : "result_not_completed"),
    state
  };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  const handle = async () => {
    validateJsonObject(message, "extension message");
    switch (message.type) {
      case "BDB_SUBMIT_ACTION":
        return submitAction(message.action, sender.tab && sender.tab.id);
      case "BDB_SUBMIT_ASSISTED":
        if (typeof globalThis.bdbSubmitAssistedAction !== "function") {
          throw new Error("Assisted submit protocol is unavailable");
        }
        return globalThis.bdbSubmitAssistedAction(
          message.action,
          sender.tab && sender.tab.id
        );
      case "BDB_POLL_ASSISTED":
        if (typeof globalThis.bdbPollAssistedActionResult !== "function") {
          throw new Error("Assisted result protocol is unavailable");
        }
        return globalThis.bdbPollAssistedActionResult(
          message.action,
          message.commandId
        );
      case "BDB_CONSIDER_AUTO":
        return considerAuto(message.action, sender.tab && sender.tab.id);
      case "BDB_AUTO_WAIT":
        if (
          !Number.isInteger(message.milliseconds) ||
          message.milliseconds < 0 ||
          message.milliseconds > 5000
        ) {
          throw new Error("AUTO wait milliseconds must be an integer between 0 and 5000");
        }
        await sleep(message.milliseconds);
        return { waited: true, milliseconds: message.milliseconds };
      case "BDB_GET_AUTO_SETTINGS":
        return getAutoSettings();
      case "BDB_SET_AUTO_SETTINGS":
        validateJsonObject(message.settings, "AUTO settings");
        return setAutoSettings(message.settings);
      case "BDB_HEALTH":
        if (typeof globalThis.bdbHealthSnapshot !== "function") {
          throw new Error("BDB health controller is unavailable");
        }
        return globalThis.bdbHealthSnapshot({
          probeNative: message.probeNative === true,
          contentVersion: message.contentVersion
        });
      case "BDB_TASKS":
        if (typeof globalThis.bdbTaskSnapshot !== "function") {
          throw new Error("BDB task controller is unavailable");
        }
        return globalThis.bdbTaskSnapshot();
      case "BDB_AUTO_DIAGNOSTICS":
        if (typeof globalThis.bdbDiagnosticsSnapshot !== "function") {
          throw new Error("BDB diagnostics controller is unavailable");
        }
        return globalThis.bdbDiagnosticsSnapshot();
      case "BDB_CANCEL_TASK":
        if (typeof globalThis.bdbCancelTask !== "function") {
          throw new Error("BDB task controller is unavailable");
        }
        return globalThis.bdbCancelTask(message.loopId, message.tabId);
      case "BDB_RESUME_TASK":
        if (typeof globalThis.bdbResumeTask !== "function") {
          throw new Error("BDB task controller is unavailable");
        }
        return globalThis.bdbResumeTask(message.loopId, message.tabId);
      case "BDB_CLEAR_READ_CACHE":
        if (typeof globalThis.bdbClearReadCache !== "function") {
          throw new Error("BDB read cache is unavailable");
        }
        return globalThis.bdbClearReadCache();
      case "BDB_CONTENT_EVENT":
        if (typeof globalThis.bdbRecordContentEvent !== "function") {
          throw new Error("BDB diagnostics controller is unavailable");
        }
        return globalThis.bdbRecordContentEvent(message.event, sender.tab && sender.tab.id);
      case "BDB_MARK_AUTO_RESULT_DELIVERED":
        return markAutoResultDelivered(
          message.loopId,
          message.iteration,
          sender.tab && sender.tab.id
        );
      case "BDB_STATUS":
        return sendNative({
          schema: REQUEST_SCHEMA,
          request_id: requestId("status"),
          action: "status"
        });
      case "BDB_CONTEXT":
        return nativeContext(message.repoAlias);
      case "BDB_LOOKUP_SUBMISSION_NONCE":
        return nativeSubmissionNonceLookup(
          message.repoAlias,
          message.clientSubmissionNonce
        );
      default:
        throw new Error("Unsupported extension message");
    }
  };

  handle()
    .then((response) => sendResponse({ ok: true, response }))
    .catch((error) => sendResponse({ ok: false, error: String(error && error.message ? error.message : error) }));
  return true;
});

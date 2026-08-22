"use strict";

const HOST_NAME = "com.bartosz.dev_bridge.vnext";
const NATIVE_HOST_NAME = "com.bartosz.dev_bridge.vnext";
const REQUEST_SCHEMA = "bdb-vnext-native-request-v1";
const RESPONSE_SCHEMA = "bdb-vnext-native-response-v1";
const PROTOCOL = "bdb-vnext-protocol-v1";
const GENERATION = "bdb-vnext-g1";
const EXTENSION_ID = "mopnolkjddkmgojfjkenjobehhmmklll";
const OUTBOX_KEY = "bdbVnextOutboxV1";
const OUTBOX_SCHEMA = "bdb-vnext-browser-outbox-v1";
const BROWSER_BUNDLE_SCHEMA = "bdb-vnext-m11c-browser-bundle-v1";
const CLIENT_FILES_SCHEMA = "bdb-vnext-browser-client-files-v1";
const MAX_ENTRIES = 128;
const MAX_REQUEST_BYTES = 256 * 1024;
const TYPES = new Set([
  "bdb-vnext-status",
  "bdb-vnext-submit",
  "bdb-vnext-lookup",
  "bdb-vnext-resume-outbox",
  "bdb-vnext-project-launch-peek",
  "bdb-vnext-project-launch-claim",
  "bdb-vnext-project-launch-ack"
]);
let storageChain = Promise.resolve();

function requestId(prefix) {
  const bytes = new Uint8Array(12);
  crypto.getRandomValues(bytes);
  return `${prefix}-${Array.from(bytes, (v) => v.toString(16).padStart(2, "0")).join("")}`;
}

function object(value, field) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${field} must be an object`);
  }
  return value;
}

function canonicalJson(value) {
  if (value === null || typeof value === "string" || typeof value === "boolean") return JSON.stringify(value);
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new Error("Non-finite number in canonical request");
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
  }
  throw new Error("Unsupported canonical request value");
}

async function digest(value) {
  const raw = new TextEncoder().encode(canonicalJson(value));
  const hash = new Uint8Array(await crypto.subtle.digest("SHA-256", raw));
  return `sha256:${Array.from(hash, (v) => v.toString(16).padStart(2, "0")).join("")}`;
}

async function digestBytes(bytes) {
  const hash = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
  return `sha256:${Array.from(hash, (v) => v.toString(16).padStart(2, "0")).join("")}`;
}

function validateNativeResponse(response) {
  const value = object(response, "native response");
  if (
    value.schema !== RESPONSE_SCHEMA || value.generation_id !== GENERATION ||
    value.protocol_generation !== PROTOCOL || value.native_host_name !== HOST_NAME ||
    value.browser_extension_id !== EXTENSION_ID
  ) throw new Error("vNext Native Host identity mismatch");
  return value;
}

async function canonicalRequest(input) {
  const source = object(input, "request");
  for (const field of ["submission_key", "intent_revision", "intent", "conversation_binding", "consumer_binding"]) {
    if (!(field in source)) throw new Error(`request.${field} is required`);
  }
  if (typeof source.submission_key !== "string" || source.submission_key.length === 0 || source.submission_key.length > 192) {
    throw new Error("request.submission_key must be a stable bounded identity");
  }
  const payload = {
    schema: "bdb-vnext-m3a-submission-v1",
    canonicalization_version: "bdb-vnext-canonical-request-v1",
    submission_key: source.submission_key,
    intent_revision: source.intent_revision,
    intent: object(source.intent, "request.intent"),
    conversation_binding: object(source.conversation_binding, "request.conversation_binding"),
    consumer_binding: object(source.consumer_binding, "request.consumer_binding")
  };
  if (typeof source.task_id === "string" && source.task_id) payload.task_id = source.task_id;
  if (typeof source.expected_intent_revision_id === "string" && source.expected_intent_revision_id) {
    payload.expected_intent_revision_id = source.expected_intent_revision_id;
  }
  const request_digest = await digest(payload);
  const request = { ...payload, request_digest };
  if (new TextEncoder().encode(JSON.stringify(request)).byteLength > MAX_REQUEST_BYTES) {
    throw new Error("Canonical request exceeds Browser bound");
  }
  return request;
}

function native(action, extra = {}) {
  return {
    schema: REQUEST_SCHEMA,
    request_id: requestId("native"),
    action,
    protocol_generation: PROTOCOL,
    browser_extension_id: EXTENSION_ID,
    ...extra
  };
}

function sendNative(message) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendNativeMessage(HOST_NAME, message, (response) => {
      const runtimeError = chrome.runtime.lastError;
      if (runtimeError) return reject(new Error(runtimeError.message || "vNext Native Host unavailable"));
      try {
        resolve(validateNativeResponse(response));
      } catch (error) {
        reject(error);
      }
    });
  });
}

async function bdbVNextObserveOwnBundle() {
  const indexResponse = await fetch(chrome.runtime.getURL("client_files.json"), { cache: "no-store" });
  if (!indexResponse.ok) throw new Error("vNext Browser client file index is unavailable");
  const index = await indexResponse.json();
  if (!index || index.schema !== CLIENT_FILES_SCHEMA || !Array.isArray(index.files) || index.files.length === 0) {
    throw new Error("vNext Browser client file index is invalid");
  }
  const paths = [...index.files].sort();
  if (new Set(paths).size !== paths.length || paths.some((path) => typeof path !== "string" || !path || path.includes(".."))) {
    throw new Error("vNext Browser client file index contains an invalid path");
  }
  const files = [];
  let totalBytes = 0;
  for (const path of paths) {
    const response = await fetch(chrome.runtime.getURL(path), { cache: "no-store" });
    if (!response.ok) throw new Error(`vNext Browser package file unavailable: ${path}`);
    const bytes = await response.arrayBuffer();
    totalBytes += bytes.byteLength;
    files.push({ path, size: bytes.byteLength, sha256: await digestBytes(bytes) });
  }
  return {
    schema: BROWSER_BUNDLE_SCHEMA,
    extension_id: EXTENSION_ID,
    file_count: files.length,
    total_bytes: totalBytes,
    files
  };
}

async function bdbVNextPublishClientVerification() {
  const expectedExtensionId = "mopnolkjddkmgojfjkenjobehhmmklll";
  if (chrome.runtime.id !== expectedExtensionId) throw new Error("vNext Browser runtime extension ID differs");
  const browserObservation = await bdbVNextObserveOwnBundle();
  return new Promise((resolve, reject) => {
    let settled = false;
    const port = chrome.runtime.connectNative(NATIVE_HOST_NAME);
    const finish = (error, value) => {
      if (settled) return;
      settled = true;
      try { port.disconnect(); } catch (_) { /* already closed */ }
      if (error) reject(error); else resolve(value);
    };
    port.onMessage.addListener((response) => {
      try {
        const value = validateNativeResponse(response);
        if (value.status !== "success" || typeof value.client_verification_sha256 !== "string" || !value.client_verification_sha256.startsWith("sha256:")) {
          throw new Error("vNext Native Host did not publish client verification");
        }
        finish(null, value);
      } catch (error) {
        finish(error);
      }
    });
    port.onDisconnect.addListener(() => {
      const runtimeError = chrome.runtime.lastError;
      if (!settled) finish(new Error(runtimeError?.message || "vNext Native Host disconnected before verification"));
    });
    port.postMessage({
      schema: REQUEST_SCHEMA,
      request_id: requestId("verify"),
      action: "handshake",
      protocol_generation: "bdb-vnext-protocol-v1",
      browser_extension_id: expectedExtensionId,
      browser_observation: browserObservation
    });
  });
}

function bdbVNextTryPublishClientVerification() {
  bdbVNextPublishClientVerification().catch(() => {
    // The next worker install/startup/load retries this proof; failure never falls back to Legacy.
  });
}

function locked(operation) {
  const next = storageChain.then(operation, operation);
  storageChain = next.catch(() => undefined);
  return next;
}

async function readOutbox() {
  const stored = await chrome.storage.local.get(OUTBOX_KEY);
  const value = stored[OUTBOX_KEY];
  if (value === undefined) return { schema: OUTBOX_SCHEMA, entries: {} };
  if (!value || typeof value !== "object" || Array.isArray(value) || value.schema !== OUTBOX_SCHEMA ||
      !value.entries || typeof value.entries !== "object" || Array.isArray(value.entries)) {
    throw new Error("vNext Browser outbox is corrupt");
  }
  return value;
}

async function mutateEntry(key, updater) {
  return locked(async () => {
    const outbox = await readOutbox();
    const entries = { ...outbox.entries };
    const next = updater(entries[key] || null);
    if (next === null) delete entries[key]; else entries[key] = next;
    if (Object.keys(entries).length > MAX_ENTRIES) throw new Error("vNext Browser outbox quota is full");
    await chrome.storage.local.set({ [OUTBOX_KEY]: { schema: OUTBOX_SCHEMA, entries } });
    return next;
  });
}

async function prepare(request) {
  return mutateEntry(request.submission_key, (current) => {
    if (current) {
      if (current.request_digest !== request.request_digest) throw new Error("submission_key digest conflict");
      return current;
    }
    return { submission_key: request.submission_key, request_digest: request.request_digest, state: "PENDING", request, receipt: null };
  });
}

async function transition(key, state, receipt = null) {
  return mutateEntry(key, (current) => {
    if (!current) throw new Error("vNext Browser outbox entry is missing");
    return { ...current, state, receipt };
  });
}

async function submit(input) {
  const request = await canonicalRequest(input);
  const current = await prepare(request);
  if (current.state === "ACKED" && current.receipt) return { ok: true, replay: true, receipt: current.receipt };
  if (current.state === "SENT" || current.state === "UNKNOWN") {
    const recovered = await lookup(current.submission_key, current.request_digest);
    if (recovered.receipt) return { ok: true, replay: true, receipt: recovered.receipt };
    return { ok: false, uncertain: true, submission_key: current.submission_key, request_digest: current.request_digest };
  }
  await transition(request.submission_key, "SENT");
  try {
    const response = await sendNative(native("admission.submit", { request }));
    if (response.status !== "success" || !response.receipt) {
      await transition(request.submission_key, "UNKNOWN");
      return { ok: false, uncertain: true, response };
    }
    await transition(request.submission_key, "ACKED", response.receipt);
    return { ok: true, replay: false, receipt: response.receipt };
  } catch (error) {
    await transition(request.submission_key, "UNKNOWN");
    throw error;
  }
}

async function lookup(submissionKey, requestDigest) {
  if (typeof submissionKey !== "string" || !submissionKey || typeof requestDigest !== "string" || !requestDigest.startsWith("sha256:")) {
    throw new Error("lookup requires exact submission identity and digest");
  }
  const response = await sendNative(native("admission.lookup", { submission_key: submissionKey, request_digest: requestDigest }));
  if (response.status !== "success") return { ok: false, response };
  if (response.receipt) await transition(submissionKey, "ACKED", response.receipt);
  return { ok: true, receipt: response.receipt || null };
}

async function resumeOutbox() {
  const outbox = await readOutbox();
  const results = [];
  for (const entry of Object.values(outbox.entries)) {
    if (!entry || entry.state === "ACKED") continue;
    try {
      const result = await lookup(entry.submission_key, entry.request_digest);
      if (!result.receipt) await transition(entry.submission_key, "UNKNOWN");
      results.push({ submission_key: entry.submission_key, ...result });
    } catch (error) {
      results.push({ submission_key: entry.submission_key, ok: false, error: error instanceof Error ? error.message : String(error) });
    }
  }
  return results;
}

async function status() {
  const response = await sendNative({ schema: REQUEST_SCHEMA, request_id: requestId("status"), action: "status" });
  return { ok: response.status === "success", response };
}

function boundedId(value, field) {
  if (typeof value !== "string" || value.length === 0 || value.length > 128 || /[\u0000]/.test(value)) {
    throw new Error(`${field} must be bounded text`);
  }
  return value;
}

async function projectLaunchPeek() {
  const response = await sendNative(native("project_launch_peek"));
  return { ok: response.status === "project_launch" || response.status === "empty", response };
}

async function projectLaunchClaim(launchId, claimId) {
  const response = await sendNative(native("project_launch_claim", {
    launch_id: boundedId(launchId, "launch_id"),
    claim_id: boundedId(claimId, "claim_id")
  }));
  return { ok: response.status === "claimed", response };
}

async function projectLaunchAck(launchId, claimId) {
  const response = await sendNative(native("project_launch_ack", {
    launch_id: boundedId(launchId, "launch_id"),
    claim_id: boundedId(claimId, "claim_id")
  }));
  return { ok: response.status === "acknowledged", response };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || typeof message !== "object" || !TYPES.has(message.type)) return false;
  if (sender.id !== chrome.runtime.id) {
    sendResponse({ ok: false, error: "Unexpected extension sender" });
    return false;
  }
  const operation = message.type === "bdb-vnext-status" ? status()
    : message.type === "bdb-vnext-submit" ? submit(message.request)
    : message.type === "bdb-vnext-lookup" ? lookup(message.submission_key, message.request_digest)
    : message.type === "bdb-vnext-resume-outbox" ? resumeOutbox()
    : message.type === "bdb-vnext-project-launch-peek" ? projectLaunchPeek()
    : message.type === "bdb-vnext-project-launch-claim" ? projectLaunchClaim(message.launch_id, message.claim_id)
    : projectLaunchAck(message.launch_id, message.claim_id);
  operation.then(
    (result) => sendResponse(result),
    (error) => sendResponse({ ok: false, error: error instanceof Error ? error.message : String(error) })
  );
  return true;
});

chrome.runtime.onInstalled.addListener(() => {
  bdbVNextTryPublishClientVerification();
});

chrome.runtime.onStartup.addListener(() => {
  bdbVNextTryPublishClientVerification();
});

bdbVNextTryPublishClientVerification();

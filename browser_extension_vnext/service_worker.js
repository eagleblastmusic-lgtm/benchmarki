"use strict";

const HOST_NAME = "com.bartosz.dev_bridge.vnext";
const REQUEST_SCHEMA = "bdb-vnext-native-request-v1";
const PROTOCOL_GENERATION = "bdb-vnext-protocol-v1";
const BROWSER_EXTENSION_ID = "mopnolkjddkmgojfjkenjobehhmmklll";
const OUTBOX_KEY = "bdbVnextOutboxV1";
const OUTBOX_SCHEMA = "bdb-vnext-browser-outbox-v1";
const MAX_OUTBOX_ENTRIES = 128;
const MAX_REQUEST_BYTES = 256 * 1024;
const MESSAGE_TYPES = new Set([
  "bdb-vnext-status",
  "bdb-vnext-submit",
  "bdb-vnext-lookup",
  "bdb-vnext-resume-outbox"
]);
let storageChain = Promise.resolve();

function randomId(prefix) {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return `${prefix}-${Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("")}`;
}

function canonicalJson(value) {
  if (value === null || typeof value === "boolean" || typeof value === "string") {
    return JSON.stringify(value);
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw new Error("Non-finite numbers are not canonical JSON");
    }
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map(canonicalJson).join(",")}]`;
  }
  if (value && typeof value === "object") {
    const keys = Object.keys(value).sort();
    return `{${keys.map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
  }
  throw new Error("Unsupported canonical JSON value");
}

async function semanticDigest(value) {
  const bytes = new TextEncoder().encode(canonicalJson(value));
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
  return `sha256:${Array.from(digest, (item) => item.toString(16).padStart(2, "0")).join("")}`;
}

function serializedBytes(value) {
  return new TextEncoder().encode(JSON.stringify(value)).byteLength;
}

function assertObject(value, field) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${field} must be an object`);
  }
  return value;
}

function exactRequestPayload(request) {
  const source = assertObject(request, "request");
  const required = ["intent_revision", "intent", "conversation_binding", "consumer_binding"];
  for (const field of required) {
    if (!(field in source)) {
      throw new Error(`request.${field} is required`);
    }
  }
  const payload = {
    schema: "bdb-vnext-m3a-submission-v1",
    canonicalization_version: "bdb-vnext-canonical-request-v1",
    submission_key:
      typeof source.submission_key === "string" && source.submission_key.length > 0
        ? source.submission_key
        : randomId("submission"),
    intent_revision: source.intent_revision,
    intent: assertObject(source.intent, "request.intent"),
    conversation_binding: assertObject(source.conversation_binding, "request.conversation_binding"),
    consumer_binding: assertObject(source.consumer_binding, "request.consumer_binding")
  };
  if (typeof source.task_id === "string" && source.task_id.length > 0) {
    payload.task_id = source.task_id;
  }
  if (typeof source.expected_intent_revision_id === "string" && source.expected_intent_revision_id.length > 0) {
    payload.expected_intent_revision_id = source.expected_intent_revision_id;
  }
  return payload;
}

async function canonicalRequest(request) {
  const payload = exactRequestPayload(request);
  const request_digest = await semanticDigest(payload);
  const result = { ...payload, request_digest };
  if (serializedBytes(result) > MAX_REQUEST_BYTES) {
    throw new Error("Canonical request exceeds the bounded Browser size");
  }
  return result;
}

function nativeMessage(action, fields = {}) {
  return {
    schema: REQUEST_SCHEMA,
    request_id: randomId("native"),
    action,
    protocol_generation: PROTOCOL_GENERATION,
    browser_extension_id: BROWSER_EXTENSION_ID,
    ...fields
  };
}

function sendNative(message) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendNativeMessage(HOST_NAME, message, (response) => {
      const runtimeError = chrome.runtime.lastError;
      if (runtimeError) {
        reject(new Error(runtimeError.message || "vNext Native Host unavailable"));
        return;
      }
      try {
        const value = assertObject(response, "native response");
        if (
          value.schema !== "bdb-vnext-native-response-v1" ||
          value.generation_id !== "bdb-vnext-g1" ||
          value.protocol_generation !== PROTOCOL_GENERATION ||
          value.native_host_name !== HOST_NAME ||
          value.browser_extension_id !== BROWSER_EXTENSION_ID
        ) {
          throw new Error("vNext Native Host identity mismatch");
        }
        resolve(value);
      } catch (error) {
        reject(error);
      }
    });
  });
}

async function readOutbox() {
  const value = await chrome.storage.local.get(OUTBOX_KEY);
  const document = value[OUTBOX_KEY];
  if (document === undefined) {
    return { schema: OUTBOX_SCHEMA, entries: {} };
  }
  if (
    !document ||
    typeof document !== "object" ||
    Array.isArray(document) ||
    document.schema !== OUTBOX_SCHEMA ||
    !document.entries ||
    typeof document.entries !== "object" ||
    Array.isArray(document.entries)
  ) {
    throw new Error("vNext Browser outbox is corrupt");
  }
  return document;
}

function withStorageLock(operation) {
  const next = storageChain.then(operation, operation);
  storageChain = next.catch(() => undefined);
  return next;
}

async function updateOutbox(submissionKey, updater) {
  return withStorageLock(async () => {
    const outbox = await readOutbox();
    const entries = { ...outbox.entries };
    const updated = updater(entries[submissionKey] || null, entries);
    if (updated === null) {
      delete entries[submissionKey];
    } else {
      entries[submissionKey] = updated;
    }
    if (Object.keys(entries).length > MAX_OUTBOX_ENTRIES) {
      throw new Error("vNext Browser outbox quota is full");
    }
    await chrome.storage.local.set({
      [OUTBOX_KEY]: { schema: OUTBOX_SCHEMA, entries }
    });
    return updated;
  });
}

async function prepareOutbox(request) {
  return updateOutbox(request.submission_key, (current) => {
    if (current) {
      if (current.request_digest !== request.request_digest) {
        throw new Error("Submission key is already bound to another digest");
      }
      return current;
    }
    return {
      submission_key: request.submission_key,
      request_digest: request.request_digest,
      state: "PENDING",
      request,
      receipt: null
    };
  });
}

async function markOutbox(submissionKey, state, receipt = null) {
  return updateOutbox(submissionKey, (current) => {
    if (!current) {
      throw new Error("vNext Browser outbox entry disappeared");
    }
    return { ...current, state, receipt };
  });
}

async function submit(requestLike) {
  const request = await canonicalRequest(requestLike);
  const prepared = await prepareOutbox(request);
  if (prepared.state === "ACKED" && prepared.receipt) {
    return { ok: true, replay: true, receipt: prepared.receipt };
  }
  await markOutbox(request.submission_key, "SENT");
  try {
    const response = await sendNative(nativeMessage("admission.submit", { request }));
    if (response.status !== "success" || !response.receipt) {
      await markOutbox(request.submission_key, "UNKNOWN");
      return { ok: false, uncertain: true, response };
    }
    await markOutbox(request.submission_key, "ACKED", response.receipt);
    return { ok: true, replay: false, receipt: response.receipt };
  } catch (error) {
    await markOutbox(request.submission_key, "UNKNOWN");
    throw error;
  }
}

async function lookup(submissionKey, requestDigest) {
  const response = await sendNative(
    nativeMessage("admission.lookup", {
      submission_key: submissionKey,
      request_digest: requestDigest
    })
  );
  if (response.status !== "success") {
    return { ok: false, response };
  }
  if (response.receipt) {
    await markOutbox(submissionKey, "ACKED", response.receipt);
  }
  return { ok: true, receipt: response.receipt || null };
}

async function resumeOutbox() {
  const outbox = await readOutbox();
  const results = [];
  for (const entry of Object.values(outbox.entries)) {
    if (!entry || entry.state === "ACKED") {
      continue;
    }
    try {
      const result = await lookup(entry.submission_key, entry.request_digest);
      if (!result.receipt) {
        await markOutbox(entry.submission_key, "UNKNOWN");
      }
      results.push({ submission_key: entry.submission_key, ...result });
    } catch (error) {
      results.push({
        submission_key: entry.submission_key,
        ok: false,
        error: error instanceof Error ? error.message : String(error)
      });
    }
  }
  return results;
}

async function status() {
  const response = await sendNative({
    schema: REQUEST_SCHEMA,
    request_id: randomId("status"),
    action: "status"
  });
  return { ok: response.status === "success", response };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || typeof message !== "object" || !MESSAGE_TYPES.has(message.type)) {
    return false;
  }
  if (sender.id !== chrome.runtime.id) {
    sendResponse({ ok: false, error: "Unexpected extension sender" });
    return false;
  }
  const operation =
    message.type === "bdb-vnext-status"
      ? status()
      : message.type === "bdb-vnext-submit"
        ? submit(message.request)
        : message.type === "bdb-vnext-lookup"
          ? lookup(message.submission_key, message.request_digest)
          : resumeOutbox();
  operation.then(
    (result) => sendResponse(result),
    (error) =>
      sendResponse({
        ok: false,
        error: error instanceof Error ? error.message : String(error)
      })
  );
  return true;
});

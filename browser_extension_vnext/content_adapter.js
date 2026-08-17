"use strict";

const SUBMISSION_SCHEMA = "bdb-vnext-submission-v1";
const MAX_SUBMISSION_TEXT = 256 * 1024;
const decorated = new WeakSet();

function parseSubmission(block) {
  const text = typeof block.textContent === "string" ? block.textContent.trim() : "";
  if (!text || text.length > MAX_SUBMISSION_TEXT || !text.includes(SUBMISSION_SCHEMA)) {
    return null;
  }
  try {
    const value = JSON.parse(text);
    if (!value || typeof value !== "object" || Array.isArray(value) || value.schema !== SUBMISSION_SCHEMA) {
      return null;
    }
    for (const field of ["submission_key", "intent_revision", "intent", "conversation_binding", "consumer_binding"]) {
      if (!(field in value)) {
        return null;
      }
    }
    if (typeof value.submission_key !== "string" || value.submission_key.length === 0) {
      return null;
    }
    return value;
  } catch (_error) {
    return null;
  }
}

function requestFromSubmission(value) {
  const request = {
    submission_key: value.submission_key,
    intent_revision: value.intent_revision,
    intent: value.intent,
    conversation_binding: value.conversation_binding,
    consumer_binding: value.consumer_binding
  };
  for (const field of ["task_id", "expected_intent_revision_id"]) {
    if (typeof value[field] === "string" && value[field].length > 0) {
      request[field] = value[field];
    }
  }
  return request;
}

function assistantOwner(block) {
  return block.closest("[data-message-author-role='assistant']");
}

function setResult(output, message, state = "neutral") {
  output.textContent = message;
  output.dataset.state = state;
}

function decorate(block, submission) {
  if (decorated.has(block) || !assistantOwner(block)) {
    return;
  }
  decorated.add(block);
  const host = block.closest("pre") || block.parentElement;
  if (!(host instanceof HTMLElement)) {
    return;
  }
  const panel = document.createElement("div");
  panel.className = "bdb-vnext-panel";
  const button = document.createElement("button");
  button.type = "button";
  button.className = "bdb-vnext-submit";
  button.textContent = "BDB vNext: Submit";
  button.setAttribute("aria-label", "Submit this request to the canonical BDB vNext generation");
  const output = document.createElement("div");
  output.className = "bdb-vnext-output";
  output.setAttribute("role", "status");
  output.setAttribute("aria-live", "polite");

  button.addEventListener("click", async () => {
    button.disabled = true;
    setResult(output, "Submitting through canonical vNext transport…");
    try {
      const response = await chrome.runtime.sendMessage({
        type: "bdb-vnext-submit",
        request: requestFromSubmission(submission)
      });
      if (response && response.ok === true && response.receipt) {
        const taskId = response.receipt.task_id || "accepted";
        setResult(output, `Accepted by vNext: ${taskId}`, "success");
        button.textContent = "BDB vNext: Accepted";
        return;
      }
      if (response && response.uncertain === true) {
        setResult(output, "Delivery is uncertain. Use BDB vNext Resume/lookup; do not create a new submission.", "warning");
        button.textContent = "BDB vNext: Lookup required";
        return;
      }
      throw new Error(response && response.error ? response.error : "vNext submission failed closed");
    } catch (error) {
      setResult(output, error instanceof Error ? error.message : String(error), "error");
      button.textContent = "BDB vNext: Retry same request";
      button.disabled = false;
    }
  });

  panel.append(button, output);
  host.insertAdjacentElement("afterend", panel);
}

function scan(root = document) {
  if (!root || typeof root.querySelectorAll !== "function") {
    return;
  }
  for (const block of root.querySelectorAll("pre code")) {
    if (!(block instanceof HTMLElement) || decorated.has(block)) {
      continue;
    }
    const submission = parseSubmission(block);
    if (submission) {
      decorate(block, submission);
    }
  }
}

scan(document);
const observer = new MutationObserver((records) => {
  for (const record of records) {
    for (const node of record.addedNodes) {
      if (node instanceof HTMLElement) {
        scan(node);
      }
    }
  }
});
if (document.documentElement) {
  observer.observe(document.documentElement, { childList: true, subtree: true });
}

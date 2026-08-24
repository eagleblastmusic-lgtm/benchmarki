"use strict";

const SUBMISSION_SCHEMA = "bdb-vnext-submission-v1";
const PROJECT_EXECUTION_SCHEMA = "bdb-project-execution-submission-v1";
const MAX_SUBMISSION_TEXT = 256 * 1024;
const CONTENT_ADAPTER_RUNTIME_FINGERPRINT = "bdb-vnext-content-adapter-live-sweep-v1";
const CANONICAL_RESULT_SWEEP_MS = 750;
const MAX_CANONICAL_SWEEP_BLOCKS = 256;
const PROJECT_EXECUTION_PANEL_KIND = "project-execution";
const GENERIC_SUBMISSION_PANEL_KIND = "generic-submission";
const PROJECT_EXECUTION_IDENTITY_FIELDS = [
  "schema", "project_id", "plan_version", "task_id", "execution_binding_id", "correlation_id", "command_id"
];
const decorated = new WeakSet();
const executionDecorated = new WeakSet();
const decoratedPanels = new WeakMap();
const executionPanels = new WeakMap();
const projectAutoSubmissions = new Map();
let projectAutoEpoch = 0;
let projectAutoState = { phase: "awaiting_next_launch", launch_id: null, execution_binding_id: null, token: null };
const PROJECT_AUTO_STOP_MESSAGE = "bdb-vnext-project-auto-stop";
const PROJECT_EXECUTION_STATUS_MESSAGE = "bdb-vnext-project-execution-status";

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

function parseProjectExecutionResult(block) {
  const text = typeof block.textContent === "string" ? block.textContent.trim() : "";
  if (!text || text.length > MAX_SUBMISSION_TEXT || !text.includes(PROJECT_EXECUTION_SCHEMA)) return null;
  try {
    const value = JSON.parse(text);
    if (!value || typeof value !== "object" || Array.isArray(value) || value.schema !== PROJECT_EXECUTION_SCHEMA) return null;
    const required = [
      "project_id", "plan_version", "task_id", "execution_binding_id", "correlation_id", "command_id",
      "repo_alias", "head_before", "head_after", "execution_status", "validation_status",
      "promotion_status", "result_summary", "evidence_refs", "criteria"
    ];
    if (required.some((field) => !(field in value))) return null;
    if (typeof value.project_id !== "string" || typeof value.task_id !== "string" || typeof value.execution_binding_id !== "string") return null;
    if (!Array.isArray(value.evidence_refs) || !Array.isArray(value.criteria)) return null;
    return value;
  } catch (_error) {
    // YAML and prose are intentionally not accepted as a canonical result.
    return null;
  }
}

function assistantOwner(block) {
  return block.closest("[data-message-author-role='assistant']");
}

function panelIsConnected(panel) {
  if (!panel) return false;
  if (typeof panel.isConnected === "boolean") return panel.isConnected;
  return Boolean(panel.parentElement);
}

function panelMountOwner(block) {
  const owner = assistantOwner(block);
  return owner instanceof HTMLElement ? owner : null;
}

function semanticSubmissionKey(kind, value) {
  if (kind === PROJECT_EXECUTION_PANEL_KIND) {
    return JSON.stringify(PROJECT_EXECUTION_IDENTITY_FIELDS.map((field) => [field, value[field]]));
  }
  return JSON.stringify([value.schema, value.submission_key]);
}

function setPanelIdentity(panel, kind, key) {
  panel.dataset.bdbSubmissionKind = kind;
  panel.dataset.bdbSubmissionKey = key;
}

function panelMatchesIdentity(panel, panelClass, kind, key) {
  return Boolean(
    panel instanceof HTMLElement &&
    panel.className === panelClass &&
    panel.dataset &&
    panel.dataset.bdbSubmissionKind === kind &&
    panel.dataset.bdbSubmissionKey === key
  );
}

function connectedPanelsFor(owner, panelClass, kind, key) {
  const matches = [];
  for (const candidate of Array.from(owner.children || [])) {
    if (panelIsConnected(candidate) && panelMatchesIdentity(candidate, panelClass, kind, key)) {
      matches.push(candidate);
    }
  }
  return matches;
}

function removePanel(panel) {
  if (typeof panel.remove === "function") {
    panel.remove();
  } else if (panel.parentElement && typeof panel.parentElement.removeChild === "function") {
    panel.parentElement.removeChild(panel);
  }
}

function reusePanel(block, panelClass, kind, key, panels, decoratedBlocks) {
  const owner = panelMountOwner(block);
  if (!owner) return false;
  const previous = panels.get(block);
  const matches = connectedPanelsFor(owner, panelClass, kind, key);
  if (matches.length > 0) {
    const canonical = panelIsConnected(previous) && matches.includes(previous) ? previous : matches[0];
    for (const duplicate of matches) {
      if (duplicate !== canonical) removePanel(duplicate);
    }
    panels.set(block, canonical);
    decoratedBlocks.add(block);
    return true;
  }
  return false;
}

function mountPanel(block, panel, panelClass, kind, key, panels, decoratedBlocks) {
  if (reusePanel(block, panelClass, kind, key, panels, decoratedBlocks)) return false;
  const owner = panelMountOwner(block);
  if (!owner) return false;
  setPanelIdentity(panel, kind, key);
  if (typeof owner.appendChild === "function") {
    owner.appendChild(panel);
  } else if (typeof owner.append === "function") {
    owner.append(panel);
  } else {
    return false;
  }
  panels.set(block, panel);
  decoratedBlocks.add(block);
  return true;
}

function setResult(output, message, state = "neutral") {
  output.textContent = message;
  output.dataset.state = state;
}

function decorate(block, submission) {
  const panelClass = "bdb-vnext-panel";
  const panelKind = GENERIC_SUBMISSION_PANEL_KIND;
  const panelKey = semanticSubmissionKey(panelKind, submission);
  if (reusePanel(block, panelClass, panelKind, panelKey, decoratedPanels, decorated)) {
    return;
  }
  const panel = document.createElement("div");
  panel.className = panelClass;
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
  mountPanel(block, panel, panelClass, panelKind, panelKey, decoratedPanels, decorated);
}

async function projectExecutionBindingForConversation(conversationId, result) {
  if (!conversationId || !result || typeof result.project_id !== "string" || typeof result.execution_binding_id !== "string") return null;
  const bindings = await projectReadBindings();
  return Object.values(bindings).find((value) => value && value.conversation_id === conversationId && value.project_id === result.project_id && value.execution_binding_id === result.execution_binding_id && typeof value.launch_id === "string" && value.launch_id.length > 0) || null;
}

async function projectExecutionStatusFor(projectId, executionBindingId, conversationId) {
  if (!projectId || !executionBindingId || !conversationId) return null;
  try {
    const response = await chrome.runtime.sendMessage({
      type: PROJECT_EXECUTION_STATUS_MESSAGE,
      project_id: projectId,
      execution_binding_id: executionBindingId,
      conversation_id: conversationId
    });
    return response && response.ok === true && response.response && response.response.status === "project_execution_status"
      ? response.response
      : null;
  } catch (_error) {
    return null;
  }
}

function projectAutoStop(reason) {
  const previous = projectAutoState;
  projectAutoEpoch += 1;
  projectAutoState = {
    phase: "stopped",
    launch_id: previous.launch_id || null,
    execution_binding_id: previous.execution_binding_id || null,
    token: String(reason || "stopped")
  };
}

function projectAutoGateMatches(status, result, conversationId) {
  const binding = status && status.binding;
  const auto = status && status.milestone_auto;
  return Boolean(
    status &&
    status.current_binding_id === result.execution_binding_id &&
    status.current_task_id === result.task_id &&
    binding &&
    binding.project_id === result.project_id &&
    binding.execution_binding_id === result.execution_binding_id &&
    binding.task_id === result.task_id &&
    binding.conversation_id === conversationId &&
    binding.status === "ACTIVE" &&
    binding.superseded !== true &&
    auto &&
    auto.status === "RUNNABLE" &&
    auto.current_task_id === result.task_id &&
    typeof auto.milestone_run_id === "string" &&
    auto.milestone_run_id.length > 0
  );
}

function projectExecutionResultRefs(panel, button, output) {
  return { panel, button, output };
}

async function submitProjectExecutionResult(block, result, refs, { automatic = false, gate = null } = {}) {
  const { button, output } = refs;
  const conversationId = projectConversationId();
  if (!conversationId) {
    setResult(output, "Project execution requires a canonical conversation.", "error");
    return false;
  }
  let binding = await projectExecutionBindingForConversation(conversationId, result);
  if (automatic && !projectAutoGateMatches(gate, result, conversationId)) {
    projectAutoStop("canonical_auto_gate_rejected");
    return false;
  }
  const autoEpoch = projectAutoEpoch;
  if (automatic && projectAutoState.phase === "stopped") return false;
  const request = {
    type: "bdb-vnext-project-execution-submit",
    result,
    conversation_id: conversationId
  };
  if (binding) request.launch_id = binding.launch_id;
  button.disabled = true;
  setResult(output, automatic ? "BDB vNext: Submitting…" : "Submitting project result through canonical vNext transport…");
  try {
    if (automatic && (autoEpoch !== projectAutoEpoch || projectAutoState.phase === "stopped")) {
      button.disabled = false;
      return false;
    }
    const response = await chrome.runtime.sendMessage(request);
    if (response && response.ok === true && response.receipt) {
      const receipt = response.receipt;
      const next = receipt.current_task_id ? ` · Next: ${receipt.current_task_id}` : "";
      const resultStatus = String(receipt.result_status || "UNKNOWN").toUpperCase();
      const acceptedPass = receipt.accepted === true && resultStatus === "PASS";
      const blocked = receipt.task_status === "blocked";
      const label = acceptedPass
        ? "BDB vNext: Result accepted"
        : blocked
          ? "BDB vNext: Result blocked"
          : resultStatus === "REVIEW_REQUIRED"
            ? "BDB vNext: Review required"
            : resultStatus === "UNKNOWN"
              ? "BDB vNext: Result unknown"
              : "BDB vNext: Result failed";
      const prefix = receipt.replayed ? `Replayed ${resultStatus}` : acceptedPass ? "Accepted" : "Failed";
      const suffix = blocked ? " · blocked" : next;
      setResult(output, `${prefix}: ${receipt.task_id}${suffix}`, acceptedPass ? "success" : "error");
      button.textContent = label;
      if (automatic) {
        if (autoEpoch !== projectAutoEpoch || projectAutoState.phase === "stopped") {
          projectAutoStop("user_stop_during_result_submit");
          return true;
        }
        if (!acceptedPass) {
          projectAutoStop("result_not_accepted");
          return true;
        }
        projectAutoState = {
          phase: "result_accepted",
          launch_id: binding ? binding.launch_id : null,
          execution_binding_id: result.execution_binding_id,
          token: receipt.replayed ? "replayed" : "accepted"
        };
        const nextLaunch = receipt.next_launch;
        if (acceptedPass && receipt.milestone_status === "RUNNABLE" && nextLaunch && nextLaunch.project_id === result.project_id && nextLaunch.task_id && nextLaunch.execution_binding_id) {
          projectAutoState.phase = "awaiting_next_launch";
          void projectHandleLaunch(nextLaunch, { automatic: true }).catch(() => projectAutoStop("next_launch_failed"));
        } else if (receipt.next_launch_status === "already_sent" || receipt.milestone_status === "MILESTONE_COMPLETED") {
          projectAutoState.phase = receipt.milestone_status === "MILESTONE_COMPLETED" ? "stopped" : "sent";
        } else {
          projectAutoStop(receipt.milestone_status === "RUNNABLE" ? "next_launch_recovery_required" : "next_launch_not_runnable");
        }
      }
      return true;
    }
    throw new Error(response && response.error ? response.error : "project execution result rejected");
  } catch (error) {
    setResult(output, error instanceof Error ? error.message : String(error), "error");
    button.textContent = "BDB vNext: Retry result";
    button.disabled = false;
    if (automatic) projectAutoState.phase = "error";
    return false;
  }
}

async function autoSubmitProjectExecution(block, result, refs, panelKey) {
  if (projectAutoSubmissions.has(panelKey)) return;
  const record = { status: "checking" };
  projectAutoSubmissions.set(panelKey, record);
  try {
    projectAutoState = {
      phase: "detected",
      launch_id: null,
      execution_binding_id: result.execution_binding_id,
      token: panelKey
    };
    const conversationId = projectConversationId();
    const status = await projectExecutionStatusFor(result.project_id, result.execution_binding_id, conversationId);
    if (!status) {
      record.status = "manual";
      return;
    }
    if (!projectAutoGateMatches(status, result, conversationId)) {
      record.status = "stopped";
      return;
    }
    projectAutoState = {
      phase: "submitting_result",
      launch_id: status.binding.launch_id,
      execution_binding_id: result.execution_binding_id,
      token: panelKey
    };
    record.status = "submitting";
    record.result = await submitProjectExecutionResult(block, result, refs, { automatic: true, gate: status });
    record.status = record.result ? "accepted" : "error";
  } catch (_error) {
    record.status = "error";
  }
}

function decorateProjectExecution(block, result) {
  const panelClass = "bdb-vnext-project-execution-panel";
  const panelKind = PROJECT_EXECUTION_PANEL_KIND;
  const panelKey = semanticSubmissionKey(panelKind, result);
  if (reusePanel(block, panelClass, panelKind, panelKey, executionPanels, executionDecorated)) return;
  const panel = document.createElement("div");
  panel.className = panelClass;
  const button = document.createElement("button");
  button.type = "button";
  button.className = "bdb-vnext-project-execution-submit";
  button.textContent = "BDB vNext: Submit result";
  button.setAttribute("aria-label", "Submit this project execution result to canonical BDB Project Memory");
  const output = document.createElement("div");
  output.className = "bdb-vnext-project-execution-output";
  output.setAttribute("role", "status");
  output.setAttribute("aria-live", "polite");
  button.addEventListener("click", async () => {
    await submitProjectExecutionResult(block, result, projectExecutionResultRefs(panel, button, output));
  });
  panel.append(button, output);
  mountPanel(block, panel, panelClass, panelKind, panelKey, executionPanels, executionDecorated);
  void autoSubmitProjectExecution(block, result, projectExecutionResultRefs(panel, button, output), panelKey);
}

// Project launch is a transport handoff from the canonical GUI. It never
// creates BDB semantic state and it never submits the ChatGPT form.
const PROJECT_LAUNCH_SCHEMA = "bdb-project-launch-v1";
const PROJECT_BINDINGS_KEY = "bdbVnextProjectLaunchBindingsV1";
const PROJECT_BINDINGS_LIMIT = 128;
const PROJECT_POLL_MS = 1200;
const PROJECT_LEASE_SECONDS = 30;
const PROJECT_LAUNCH_INSERT_MESSAGE = "bdb-vnext-project-launch-insert";
const PROJECT_UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const PROJECT_TAB_INSTANCE_KEY = "bdbVnextProjectTabInstanceV1";
let projectPollActive = false;
let projectInsertionActive = false;
const projectClaims = new Map();
let projectTabInstance;

function projectConversationId() {
  if (location.protocol !== "https:" || location.hostname !== "chatgpt.com") return null;
  const match = location.pathname.match(/(?:^|\/)c\/([A-Za-z0-9-]{8,128})(?:\/|$)/);
  return match ? match[1] : null;
}

function projectBlankNewChat() {
  return location.protocol === "https:" && location.hostname === "chatgpt.com" && (location.pathname === "/" || location.pathname === "");
}

function projectPageEligible({ selectedByUser = false } = {}) {
  return Boolean(
    (projectConversationId() || (selectedByUser && projectBlankNewChat())) &&
    document.visibilityState === "visible"
  );
}

function projectVisible(element) {
  if (!(element instanceof HTMLElement)) return false;
  const style = window.getComputedStyle(element);
  const rect = element.getBoundingClientRect();
  return style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
}

function projectFindComposer() {
  const selectors = [
    "#prompt-textarea",
    "textarea[data-testid='textbox']",
    "textarea[placeholder*='Message']",
    "[contenteditable='true'][role='textbox']",
    "[contenteditable='true']"
  ];
  for (const selector of selectors) {
    const found = [];
    for (const element of document.querySelectorAll(selector)) {
      if (!projectVisible(element)) continue;
      const messageOwner = typeof element.closest === "function"
        ? element.closest("[data-message-author-role='assistant'], [data-message-author-role='user']")
        : null;
      if (messageOwner) continue;
      found.push(element);
    }
    if (found.length === 1) return found[0];
    if (found.length > 1) return null;
  }
  return null;
}

function projectComposerText(composer) {
  if (!composer) return null;
  if (composer instanceof HTMLTextAreaElement || composer instanceof HTMLInputElement) {
    return composer.value;
  }
  return typeof composer.innerText === "string" ? composer.innerText : composer.textContent || "";
}

function projectComposerHasForeignState(composer) {
  if (!composer) return true;
  if (projectComposerText(composer).trim() !== "") return true;
  return Boolean(composer.querySelector(
    "input[type='file'], [data-testid*='attachment'], [data-file-id], [aria-label*='attachment' i], [aria-label*='file' i]"
  ));
}

function projectClaimId(launchId) {
  let value = projectClaims.get(launchId);
  if (!value) {
    value = typeof crypto.randomUUID === "function" ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
    projectClaims.set(launchId, value);
  }
  return value;
}

function projectTabInstanceId() {
  if (projectTabInstance) return projectTabInstance;
  try {
    const existing = sessionStorage.getItem(PROJECT_TAB_INSTANCE_KEY);
    if (typeof existing === "string" && PROJECT_UUID_RE.test(existing)) {
      projectTabInstance = existing;
      return projectTabInstance;
    }
    projectTabInstance = typeof crypto.randomUUID === "function" ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
    sessionStorage.setItem(PROJECT_TAB_INSTANCE_KEY, projectTabInstance);
  } catch (_error) {
    projectTabInstance = typeof crypto.randomUUID === "function" ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
  }
  return projectTabInstance;
}

function projectValidLaunch(value) {
  return Boolean(
    value && typeof value === "object" && !Array.isArray(value) &&
    value.schema === PROJECT_LAUNCH_SCHEMA &&
    typeof value.launch_id === "string" && PROJECT_UUID_RE.test(value.launch_id) &&
    typeof value.repo_alias === "string" && /^[a-z][a-z0-9-]{0,31}$/.test(value.repo_alias) &&
    typeof value.prompt === "string" && value.prompt.trim() !== "" && value.prompt.length <= 50000 &&
    typeof value.auto_send === "boolean" && typeof value.created_at === "string" && typeof value.expires_at === "string"
  );
}

async function projectReadBindings() {
  try {
    const stored = await chrome.storage.local.get(PROJECT_BINDINGS_KEY);
    const value = stored[PROJECT_BINDINGS_KEY];
    return value && typeof value === "object" && !Array.isArray(value) ? { ...value } : {};
  } catch (_error) {
    return {};
  }
}

async function projectWriteBinding(launch, claimId, conversationId, state, extra = {}) {
  const bindings = await projectReadBindings();
  const now = Date.now();
  bindings[launch.launch_id] = {
    schema: "bdb-vnext-project-launch-binding-v1",
    launch_id: launch.launch_id,
    conversation_id: conversationId,
    tab_instance_id: projectTabInstanceId(),
    claim_id: claimId,
    repo_alias: launch.repo_alias,
    project_id: launch.project_id || null,
    plan_version: launch.plan_version || null,
    task_id: launch.task_id || null,
    execution_binding_id: launch.execution_binding_id || null,
    correlation_id: launch.correlation_id || null,
    command_id: launch.command_id || null,
    expected_repo_head_before: launch.expected_repo_head_before || null,
    auto_send: launch.auto_send === true,
    state,
    ...extra,
    updated_at: now
  };
  const entries = Object.entries(bindings)
    .filter(([, value]) => value && typeof value === "object" && Number.isFinite(value.updated_at))
    .sort((left, right) => left[1].updated_at - right[1].updated_at)
    .slice(-PROJECT_BINDINGS_LIMIT);
  await chrome.storage.local.set({ [PROJECT_BINDINGS_KEY]: Object.fromEntries(entries) });
}

function projectBindingFor(bindings, launchId, conversationId) {
  const value = bindings[launchId];
  return value && value.conversation_id === conversationId && value.tab_instance_id === projectTabInstanceId() && PROJECT_UUID_RE.test(value.claim_id || "") ? value : null;
}

function projectAnnounce(message, state = "neutral") {
  let output = document.querySelector(".bdb-vnext-project-launch-status");
  if (!(output instanceof HTMLElement)) {
    output = document.createElement("div");
    output.className = "bdb-vnext-project-launch-status";
    output.setAttribute("role", "status");
    output.setAttribute("aria-live", "polite");
    const composer = projectFindComposer();
    if (composer) composer.insertAdjacentElement("beforebegin", output);
  }
  output.textContent = message;
  output.dataset.state = state;
}

function projectExactPromptHtml(prompt) {
  return prompt
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;")
    .replaceAll("\n", "<br>");
}

function projectInsertExact(composer, prompt) {
  if (!composer || projectComposerHasForeignState(composer)) return false;
  composer.focus();
  if (composer instanceof HTMLTextAreaElement || composer instanceof HTMLInputElement) {
    const prototype = Object.getPrototypeOf(composer);
    const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
    if (!setter) return false;
    setter.call(composer, prompt);
    composer.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: prompt }));
    composer.dispatchEvent(new Event("change", { bubbles: true }));
  } else {
    let inserted = false;
    try {
      inserted = document.execCommand("insertHTML", false, projectExactPromptHtml(prompt));
    } catch (_error) {
      inserted = false;
    }
    if (!inserted || projectComposerText(composer) !== prompt) {
      composer.textContent = prompt;
      composer.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: prompt }));
    }
  }
  return projectComposerText(composer) === prompt;
}

function projectFindSendControl(composer) {
  if (!composer) return null;
  const form = typeof composer.closest === "function" ? composer.closest("form") : null;
  const roots = form ? [form, document] : [document];
  const selectors = [
    "button[data-testid='send-button']",
    "button[aria-label='Send prompt']",
    "button[aria-label*='Send' i]",
    "button[type='submit']"
  ];
  for (const root of roots) {
    for (const selector of selectors) {
      const matches = typeof root.querySelectorAll === "function" ? Array.from(root.querySelectorAll(selector)).filter(projectVisible) : [];
      if (matches.length === 1) return matches[0];
      if (matches.length > 1 && selector === "button[data-testid='send-button']") return null;
    }
  }
  return null;
}

function projectExactUserMessageCount(prompt) {
  if (typeof prompt !== "string" || prompt === "") return 0;
  let count = 0;
  for (const node of document.querySelectorAll("[data-message-author-role='user']")) {
    if (!(node instanceof HTMLElement)) continue;
    if (typeof node.closest === "function" && node.closest(".bdb-vnext-project-launch-status, .bdb-vnext-project-execution-panel")) continue;
    const value = typeof node.innerText === "string" ? node.innerText : node.textContent || "";
    if (value === prompt) count += 1;
  }
  return count;
}

function projectSendControlEnabled(send) {
  return Boolean(
    send && send.disabled !== true &&
    (typeof send.getAttribute !== "function" || send.getAttribute("aria-disabled") !== "true")
  );
}

function projectSendEffectObserved(prompt, baselineCount, conversationId) {
  if (!conversationId || projectConversationId() !== conversationId) return false;
  const composer = projectFindComposer();
  if (!composer || projectComposerText(composer) !== "") return false;
  return projectExactUserMessageCount(prompt) > baselineCount;
}

function projectDelay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function projectAutoSendInserted(launch, claimId, prompt, insertedComposer, token) {
  if (projectAutoState.phase === "stopped") return false;
  const epoch = projectAutoEpoch;
  projectAutoState = { phase: "awaiting_send_ready", launch_id: launch.launch_id, execution_binding_id: launch.execution_binding_id, token };
  for (let attempt = 0; attempt < 20; attempt += 1) {
    if (epoch !== projectAutoEpoch || projectAutoState.phase === "stopped") return false;
    const composer = projectFindComposer();
    if (!composer || projectComposerText(composer) !== prompt) {
      projectAutoStop("composer_changed_before_send");
      projectAnnounce("BDB AUTO zatrzymane: composer został zmieniony przez użytkownika.", "warning");
      return false;
    }
    const status = await projectExecutionStatusFor(launch.project_id, launch.execution_binding_id, projectConversationId());
    if (!status || status.current_binding_id !== launch.execution_binding_id || status.current_task_id !== launch.task_id || !status.milestone_auto || status.milestone_auto.status !== "RUNNABLE" || status.binding?.conversation_id !== projectConversationId()) {
      projectAutoStop("canonical_auto_send_gate_rejected");
      projectAnnounce("BDB AUTO zatrzymane: canonical milestone nie jest już RUNNABLE.", "warning");
      return false;
    }
    const send = projectFindSendControl(composer);
    if (projectSendControlEnabled(send) && projectComposerText(composer) === prompt && epoch === projectAutoEpoch) {
      const conversationId = projectConversationId();
      const baselineCount = projectExactUserMessageCount(prompt);
      projectAutoState.phase = "sending_prompt";
      if (typeof send.click !== "function") return false;
      await projectWriteBinding(launch, claimId, conversationId, "SEND_ATTEMPTED", {
        send_baseline_count: baselineCount,
        send_attempt_token: token
      });
      try {
        send.click();
      } catch (_error) {
        projectAutoState.phase = "error";
        projectAnnounce("BDB AUTO zatrzymane: kontrolka Send odrzuciła próbę.", "warning");
        return false;
      }
      projectAutoState.phase = "verifying_send_effect";
      for (let verifyAttempt = 0; verifyAttempt < 50; verifyAttempt += 1) {
        if (epoch !== projectAutoEpoch || projectAutoState.phase === "stopped") return false;
        if (projectSendEffectObserved(prompt, baselineCount, conversationId)) {
          await projectWriteBinding(launch, claimId, conversationId, "SEND_CONFIRMED", {
            send_baseline_count: baselineCount,
            send_attempt_token: token
          });
          projectAutoState.phase = "sent";
          projectAnnounce("BDB AUTO: wysłanie promptu potwierdzone.", "success");
          return true;
        }
        await projectDelay(100);
      }
      projectAutoState.phase = "error";
      projectAnnounce("BDB AUTO zatrzymane: brak potwierdzonego efektu Send; ponowna próba jest zablokowana.", "warning");
      return false;
    }
    await projectDelay(100);
  }
  projectAutoState.phase = "error";
  projectAnnounce("BDB AUTO zatrzymane: kontrolka Send nie stała się dostępna.", "warning");
  return false;
}

async function projectPeek() {
  const result = await chrome.runtime.sendMessage({ type: "bdb-vnext-project-launch-peek" });
  if (!result || result.ok !== true || !result.response || result.response.status !== "project_launch") return null;
  return projectValidLaunch(result.response.launch) ? result.response.launch : null;
}

async function projectClaim(launch, claimId, conversationId) {
  const result = await chrome.runtime.sendMessage({
    type: "bdb-vnext-project-launch-claim",
    launch_id: launch.launch_id,
    claim_id: claimId,
    ...(conversationId ? { conversation_id: conversationId } : {})
  });
  if (!result || result.ok !== true || !result.response || result.response.status !== "claimed") return null;
  return projectValidLaunch(result.response.launch) ? result.response.launch : null;
}

async function projectAck(launchId, claimId, handoff = null) {
  const result = await chrome.runtime.sendMessage({
    type: "bdb-vnext-project-launch-ack",
    launch_id: launchId,
    claim_id: claimId,
    ...(handoff ? { handoff } : {})
  });
  return Boolean(result && result.ok === true && result.response && result.response.status === "acknowledged");
}

async function projectHandleLaunch(launch, { selectedByUser = false, automatic = false } = {}) {
  if (projectInsertionActive || !projectPageEligible({ selectedByUser })) return false;
  const conversationId = projectConversationId();
  const blankNewChat = !conversationId && selectedByUser && projectBlankNewChat();
  const autoMode = automatic || launch.auto_send === true;
  const composer = projectFindComposer();
  if ((!conversationId && !blankNewChat) || !composer) return false;
  const bindings = await projectReadBindings();
  const existing = conversationId ? projectBindingFor(bindings, launch.launch_id, conversationId) : null;
  if (!existing && autoMode && projectExactUserMessageCount(launch.prompt) > 0 && projectComposerText(composer) === "") {
    projectAnnounce("BDB AUTO zatrzymane: istnieje wysłana identyczna wiadomość bez lokalnego dowodu próby.", "warning");
    return false;
  }
  if (!existing && projectComposerHasForeignState(composer)) {
    if (projectComposerText(composer) === launch.prompt) {
      // Storage may have been lost after insertion but before ACK. The exact
      // canonical pending prompt is safe to acknowledge without re-inserting.
    } else {
      projectAnnounce("BDB vNext: composer is not empty; launch left pending.", "warning");
      return false;
    }
  }
  const claimId = existing ? existing.claim_id : projectClaimId(launch.launch_id);
  const claimed = await projectClaim(launch, claimId, conversationId);
  if (!claimed) return false;
  const sameSelection = conversationId
    ? projectConversationId() === conversationId
    : !projectConversationId() && projectBlankNewChat();
  if (!projectPageEligible({ selectedByUser }) || !sameSelection) return false;
  let canonicalStatus = null;
  if (autoMode) {
    canonicalStatus = await projectExecutionStatusFor(claimed.project_id, claimed.execution_binding_id, conversationId);
    if (!canonicalStatus || canonicalStatus.current_binding_id !== claimed.execution_binding_id || canonicalStatus.current_task_id !== claimed.task_id || !canonicalStatus.binding || canonicalStatus.binding.project_id !== claimed.project_id || canonicalStatus.binding.task_id !== claimed.task_id || canonicalStatus.binding.conversation_id !== conversationId || !canonicalStatus.milestone_auto || canonicalStatus.milestone_auto.status !== "RUNNABLE") {
      projectAutoStop("canonical_launch_gate_rejected");
      return false;
    }
  }
  if (existing?.state === "ACKED") return true;
  if (autoMode && existing?.state === "SEND_CONFIRMED") {
    const acknowledged = await projectAck(claimed.launch_id, claimId, { project_id: claimed.project_id, execution_binding_id: claimed.execution_binding_id, conversation_id: conversationId });
    if (acknowledged) await projectWriteBinding(claimed, claimId, conversationId, "ACKED");
    return acknowledged;
  }
  if (autoMode && existing?.state === "SEND_ATTEMPTED") {
    const baseline = existing.send_baseline_count;
    if (Number.isInteger(baseline) && baseline >= 0 && projectSendEffectObserved(claimed.prompt, baseline, conversationId)) {
      await projectWriteBinding(claimed, claimId, conversationId, "SEND_CONFIRMED", {
        send_baseline_count: baseline,
        send_attempt_token: existing.send_attempt_token || null
      });
      const acknowledged = await projectAck(claimed.launch_id, claimId, { project_id: claimed.project_id, execution_binding_id: claimed.execution_binding_id, conversation_id: conversationId });
      if (acknowledged) await projectWriteBinding(claimed, claimId, conversationId, "ACKED");
      return acknowledged;
    }
    projectAnnounce("BDB AUTO zatrzymane: poprzednia próba Send pozostaje niepewna; duplicate Send zablokowany.", "warning");
    return false;
  }
  if (conversationId) await projectWriteBinding(claimed, claimId, conversationId, "CLAIMED");
  projectInsertionActive = true;
  try {
    const currentComposer = projectFindComposer();
    const currentText = projectComposerText(currentComposer);
    if (currentText !== claimed.prompt) {
      if (projectComposerHasForeignState(currentComposer) || !projectInsertExact(currentComposer, claimed.prompt)) {
        projectAnnounce("BDB vNext: launch not inserted; composer changed.", "warning");
        return false;
      }
    }
    const finalSelection = conversationId
      ? projectConversationId() === conversationId
      : !projectConversationId() && projectBlankNewChat();
    if (!projectPageEligible({ selectedByUser }) || !finalSelection || projectComposerText(projectFindComposer()) !== claimed.prompt) return false;
    if (autoMode) {
      if (canonicalStatus?.launch_handoff?.status === "SENT") {
        projectAutoState = { phase: "sent", launch_id: claimed.launch_id, execution_binding_id: claimed.execution_binding_id, token: "recovered-sent" };
        const acknowledged = await projectAck(claimed.launch_id, claimId, { project_id: claimed.project_id, execution_binding_id: claimed.execution_binding_id, conversation_id: conversationId });
        if (acknowledged && conversationId) await projectWriteBinding(claimed, claimId, conversationId, "ACKED");
        return acknowledged;
      }
      if (projectAutoState.phase === "stopped") return false;
      const token = `${claimed.launch_id}:${claimed.execution_binding_id}:${Date.now()}`;
      projectAutoState = { phase: "inserting_prompt", launch_id: claimed.launch_id, execution_binding_id: claimed.execution_binding_id, token };
      const sent = await projectAutoSendInserted(claimed, claimId, claimed.prompt, projectFindComposer(), token);
      if (!sent) return false;
      const acknowledged = await projectAck(claimed.launch_id, claimId, { project_id: claimed.project_id, execution_binding_id: claimed.execution_binding_id, conversation_id: conversationId });
      if (acknowledged && conversationId) await projectWriteBinding(claimed, claimId, conversationId, "ACKED");
      return acknowledged;
    }
    const acknowledged = await projectAck(claimed.launch_id, claimId);
    if (!acknowledged) return false;
    if (conversationId) await projectWriteBinding(claimed, claimId, conversationId, "ACKED");
    projectAnnounce("BDB vNext: project prompt inserted (not sent).", "success");
    return true;
  } finally {
    projectInsertionActive = false;
  }
}

async function projectInsertSelectedLaunch() {
  if (!projectPageEligible({ selectedByUser: true })) {
    return { ok: false, code: "conversation_not_eligible" };
  }
  const launch = await projectPeek();
  if (!launch) {
    return { ok: false, code: "no_pending_prompt" };
  }
  const inserted = await projectHandleLaunch(launch, { selectedByUser: true });
  return inserted
    ? { ok: true, code: "inserted", launch_id: launch.launch_id }
    : { ok: false, code: "project_prompt_not_inserted", launch_id: launch.launch_id };
}

async function projectPoll() {
  if (projectPollActive) return;
  projectPollActive = true;
  try {
    const launch = await projectPeek();
    // Manual launches remain owned by the explicit popup action in the tab
    // selected by the user.  Only canonical AUTO launches may be consumed by
    // the background poller without a focus heuristic.
    if (launch?.auto_send === true) await projectHandleLaunch(launch, { automatic: true });
  } catch (_error) {
    // A transient Native/DOM failure leaves the canonical launch pending.
  } finally {
    projectPollActive = false;
  }
}

if (typeof chrome === "object" && chrome.runtime && chrome.runtime.onMessage) {
  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message && message.type === PROJECT_AUTO_STOP_MESSAGE) {
      projectAutoStop("user_stop");
      sendResponse({ ok: true, code: "auto_stopped" });
      return false;
    }
    if (!message || message.type !== PROJECT_LAUNCH_INSERT_MESSAGE) return false;
    projectInsertSelectedLaunch()
      .then(sendResponse)
      .catch(() => sendResponse({ ok: false, code: "project_prompt_not_inserted" }));
    return true;
  });
}

function codeBlocks(root) {
  if (!root) return [];
  const blocks = [];
  if (typeof root.matches === "function" && root.matches("pre code")) {
    blocks.push(root);
  }
  if (typeof root.querySelectorAll === "function") {
    for (const block of root.querySelectorAll("pre code")) {
      if (!blocks.includes(block)) blocks.push(block);
    }
  }
  return blocks;
}

function canonicalResultText(block) {
  return typeof block.textContent === "string" ? block.textContent.trim() : "";
}

function isCanonicalResultCandidate(block) {
  const text = canonicalResultText(block);
  return Boolean(
    text &&
    text.length <= MAX_SUBMISSION_TEXT &&
    (text.includes(PROJECT_EXECUTION_SCHEMA) || text.includes(SUBMISSION_SCHEMA))
  );
}

function scanBlock(block) {
  if (!(block instanceof HTMLElement)) {
    return;
  }
  if (!isCanonicalResultCandidate(block)) {
    return;
  }
  const projectResult = parseProjectExecutionResult(block);
  if (projectResult) {
    decorateProjectExecution(block, projectResult);
    return;
  }
  const submission = parseSubmission(block);
  if (submission) {
    decorate(block, submission);
  }
}

function scan(root = document) {
  for (const block of codeBlocks(root)) {
    scanBlock(block);
  }
}

function sweepCanonicalResults() {
  if (document.visibilityState !== "visible") {
    return;
  }
  const blocks = codeBlocks(document);
  const limit = Math.min(blocks.length, MAX_CANONICAL_SWEEP_BLOCKS);
  for (let index = 0; index < limit; index += 1) {
    scanBlock(blocks[index]);
  }
}

function publishRuntimeFingerprint() {
  const root = document && document.documentElement;
  if (root && root.dataset) {
    root.dataset.bdbVnextContentAdapter = CONTENT_ADAPTER_RUNTIME_FINGERPRINT;
  }
}

publishRuntimeFingerprint();
scan(document);
const canonicalSweepTimer = setInterval(sweepCanonicalResults, CANONICAL_RESULT_SWEEP_MS);
if (canonicalSweepTimer && typeof canonicalSweepTimer.unref === "function") canonicalSweepTimer.unref();

const STREAM_SCAN_DEBOUNCE_MS = 50;
const pendingMutationRoots = new Set();
let pendingMutationScan = null;

function mutationScanRoot(node) {
  let element = node;
  if (!element || element.nodeType !== 1) {
    element = element && (element.parentElement || element.parentNode);
  }
  if (!element) return document;
  if (typeof element.closest === "function") {
    return element.closest("[data-message-author-role='assistant']") || element;
  }
  return element;
}

function scheduleMutationScan(node) {
  pendingMutationRoots.add(mutationScanRoot(node));
  if (pendingMutationScan !== null) return;
  pendingMutationScan = setTimeout(() => {
    pendingMutationScan = null;
    const roots = Array.from(pendingMutationRoots);
    pendingMutationRoots.clear();
    for (const root of roots) scan(root);
  }, STREAM_SCAN_DEBOUNCE_MS);
}

scan(document);
const observer = new MutationObserver((records) => {
  for (const record of records) {
    scheduleMutationScan(record.target);
    for (const node of record.addedNodes || []) {
      scheduleMutationScan(node);
    }
  }
});
if (document.documentElement) {
  observer.observe(document.documentElement, { childList: true, subtree: true, characterData: true });
}

if (typeof chrome === "object" && chrome.runtime && typeof chrome.runtime.sendMessage === "function") {
  void projectPoll();
  const projectTimer = setInterval(projectPoll, PROJECT_POLL_MS);
  if (projectTimer && typeof projectTimer.unref === "function") projectTimer.unref();
}

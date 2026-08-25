from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_vnext_project_auto_chain_is_exactly_once_and_fail_closed(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the Browser AUTO contract")
    harness = tmp_path / "project-auto-chain.cjs"
    harness.write_text(
        textwrap.dedent(
            r'''
            "use strict";
            const assert = require("node:assert/strict");
            const fs = require("node:fs");
            const vm = require("node:vm");

            const mode = process.argv[3];
            const conversationId = "abcdef12-3456-4789-abcd-abcdef123456";
            const bindingId = "binding-1";
            const projectId = "project-1";
            const taskId = "P0-01";
            const launchId = "11111111-1111-4111-8111-111111111111";
            const nextLaunchId = "22222222-2222-4222-8222-222222222222";
            const prompt = "Canonical P0-02 prompt";
            const result = {
              schema: "bdb-project-execution-submission-v1",
              project_id: projectId,
              plan_version: "1",
              task_id: taskId,
              execution_binding_id: bindingId,
              correlation_id: "corr-1",
              command_id: "command-1",
              repo_alias: "execution-fixture",
              head_before: "a".repeat(40),
              head_after: "b".repeat(40),
              execution_status: "PASS",
              validation_status: "PASS",
              promotion_status: "NOT_RUN",
              result_summary: "done",
              evidence_refs: [],
              criteria: []
            };
            const nextLaunch = {
              schema: "bdb-project-launch-v1",
              launch_id: nextLaunchId,
              repo_alias: "execution-fixture",
              prompt,
              auto_send: true,
              project_id: projectId,
              plan_version: "1",
              task_id: "P0-02",
              execution_binding_id: "binding-2",
              correlation_id: "corr-2",
              command_id: "command-2",
              created_at: "2026-01-01T00:00:00Z",
              expires_at: "2999-01-01T00:00:00Z"
            };
            const panels = [];
            const messages = [];
            const events = [];
            let stopListener = null;
            let observerCallback = null;
            let sweepCallback = null;
            let sendClicks = 0;
            let codeText = "";
            const userMessages = [];
            const localStorage = {};

            class Element {
              constructor(kind = "div", text = "") {
                this.nodeType = 1;
                this.kind = kind;
                this.children = [];
                this.parentElement = null;
                this.parentNode = null;
                this.dataset = {};
                this.style = {};
                this.listeners = {};
                this.className = "";
                this._text = text;
              }
              get isConnected() { return Boolean(this.parentElement); }
              get textContent() { return this.children.length ? this.children.map((child) => child.textContent || "").join("") : this._text; }
              set textContent(value) { this.children = []; this._text = String(value); }
              get innerText() { return this.textContent; }
              set innerText(value) { this.textContent = value; }
              getBoundingClientRect() { return { width: 600, height: 30 }; }
              append(...items) {
                for (const item of items) {
                  this.children.push(item);
                  item.parentElement = this;
                  item.parentNode = this;
                }
                if (this.kind === "assistant") panels.push(...items.filter((item) => item.className));
              }
              appendChild(item) { this.append(item); return item; }
              insertAdjacentElement(_where, item) { this.append(item); }
              remove() { if (this.parentElement) this.parentElement.children = this.parentElement.children.filter((item) => item !== this); this.parentElement = null; }
              addEventListener(type, callback) { this.listeners[type] = callback; }
              setAttribute() {}
              getAttribute() { return null; }
              focus() {}
              dispatchEvent() {}
              matches(selector) { return selector === "pre code" && this.kind === "code" && this.parentElement?.kind === "pre"; }
              closest(selector) {
                if (selector === "form") return this.kind === "composer" ? form : null;
                let current = this;
                while (current) {
                  if (selector === "pre" && current.kind === "pre") return current;
                  if (selector.includes("assistant") && current.kind === "assistant") return current;
                  current = current.parentElement;
                }
                return null;
              }
              querySelector() { return null; }
              querySelectorAll(selector) {
                if (selector === "*") {
                  const found = [];
                  const visit = (item) => {
                    for (const child of item.children || []) {
                      found.push(child);
                      visit(child);
                    }
                  };
                  visit(this);
                  return found;
                }
                if (this.kind === "form" && selector.startsWith("button")) return selector === "button[data-testid='send-button']" && mode !== "nosend" ? [sendButton] : [];
                const found = [];
                const visit = (item) => {
                  for (const child of item.children || []) {
                    if (selector === "pre code" && child.kind === "code" && item.kind === "pre") found.push(child);
                    visit(child);
                  }
                };
                visit(this);
                return found;
              }
            }
            const assistant = new Element("assistant");
            class Composer extends Element {
              constructor(initial) { super("composer"); this._value = initial; }
              get value() { return this._value; }
              set value(value) {
                this._value = String(value);
                if (mode === "edited" && this._value === prompt) setTimeout(() => { this._value = "user edited this"; }, 0);
                if (mode === "stopped" && this._value === prompt) setTimeout(() => stopListener?.({ type: "bdb-vnext-project-auto-stop" }, null, () => {}), 0);
              }
            }
            const pre = new Element("pre");
            const code = new Element("code");
            const composer = new Composer(mode === "nonempty" ? "user draft" : "");
            const form = new Element("form");
            const sendButton = new Element("button");
            sendButton.disabled = false;
            sendButton.click = () => {
              sendClicks += 1;
              events.push("send");
              if (mode !== "noeffect") setTimeout(() => {
                composer.value = "";
                const message = new Element("user", mode === "collapsed" ? "" : prompt);
                if (mode === "collapsed") {
                  message.append(new Element("content", prompt), new Element("button", "Pokaż więcej"));
                }
                userMessages.push(message);
                documentElement.append(message);
              }, 0);
            };
            pre.append(code);
            assistant.append(pre);
            code.textContent = codeText;

            class FakeMutationObserver {
              constructor(callback) { observerCallback = callback; }
              observe(_target, options) { assert.equal(options.characterData, true); }
            }
            const documentElement = new Element("html");
            documentElement.append(assistant);
            const context = {
              console,
              HTMLElement: Element,
              HTMLTextAreaElement: Composer,
              HTMLInputElement: class extends Element {},
              InputEvent: class {},
              Event: class {},
              TextEncoder,
              Set,
              Map,
              crypto: { randomUUID: () => "33333333-3333-4333-8333-333333333333" },
              sessionStorage: { getItem: () => null, setItem() {} },
              window: { getComputedStyle: () => ({ visibility: "visible", display: "block" }) },
              location: { protocol: "https:", hostname: "chatgpt.com", pathname: `/c/${conversationId}` },
              document: {
                visibilityState: "visible",
                activeElement: { kind: "body" },
                hasFocus: () => false,
                documentElement,
                querySelector(selector) {
                  if (selector === "#prompt-textarea") return composer;
                  if (selector === ".bdb-vnext-project-launch-status") return null;
                  return null;
                },
                querySelectorAll(selector) {
                  if (selector === "pre code") return codeText ? [code] : [];
                  if (selector === "#prompt-textarea") return [composer];
                  if (selector === "[data-message-author-role='user']") return userMessages;
                  return [];
                },
                createElement: (kind) => new Element(kind),
                execCommand: () => false
              },
              MutationObserver: FakeMutationObserver,
              setTimeout,
              clearTimeout,
              setInterval: (callback, interval) => { if (interval === 750) sweepCallback = callback; return { unref() {} }; },
              chrome: {
                storage: { local: {
                  async get(key) { return { [key]: localStorage[key] }; },
                  async set(value) { Object.assign(localStorage, value); }
                } },
                runtime: {
                  onMessage: { addListener(callback) { stopListener = callback; } },
                  async sendMessage(message) {
                    messages.push(message);
                    if (message.type === "bdb-vnext-project-launch-peek") return { ok: true, response: { status: "empty" } };
                    if (message.type === "bdb-vnext-project-execution-status") {
                      await new Promise((resolve) => setTimeout(resolve, mode === "stopped" || mode === "edited" ? 5 : 0));
                      if (mode === "stale" || mode === "wrong") return { ok: true, response: {
                        status: "project_execution_status", current_binding_id: mode === "wrong" ? "foreign-binding" : bindingId,
                        current_task_id: taskId, binding: { project_id: projectId, execution_binding_id: bindingId, task_id: taskId, launch_id: launchId, conversation_id: conversationId, status: "ACTIVE", superseded: false },
                        milestone_auto: { status: "STOPPED", milestone_run_id: "run-1", current_task_id: taskId }
                      }};
                      const next = message.execution_binding_id === "binding-2";
                      return { ok: true, response: {
                        status: "project_execution_status", current_binding_id: next ? "binding-2" : bindingId, current_task_id: next ? "P0-02" : taskId,
                        binding: { project_id: projectId, execution_binding_id: next ? "binding-2" : bindingId, task_id: next ? "P0-02" : taskId, launch_id: next ? nextLaunchId : launchId, conversation_id: conversationId, status: "ACTIVE", superseded: false },
                        milestone_auto: { status: "RUNNABLE", milestone_run_id: "run-1", current_task_id: next ? "P0-02" : taskId },
                        launch_handoff: mode === "sent" && next ? { status: "SENT" } : { status: "PENDING" }
                      }};
                    }
                    if (message.type === "bdb-vnext-project-execution-submit") {
                      const failed = mode === "fail" || mode === "replay-fail";
                      return { ok: true, receipt: {
                      accepted: !failed, result_status: failed ? "FAIL" : "PASS", task_status: failed ? "blocked" : "completed",
                      task_id: taskId, replayed: mode === "replay-fail", milestone_status: mode === "completed" ? "MILESTONE_COMPLETED" : "RUNNABLE",
                      current_task_id: "P0-02", next_launch: mode === "completed" || failed ? null : nextLaunch
                      }};
                    }
                    if (message.type === "bdb-vnext-project-launch-claim") return { ok: true, response: { status: "claimed", launch: nextLaunch } };
                    if (message.type === "bdb-vnext-project-launch-ack") { events.push("ack"); return { ok: true, response: { status: "acknowledged" } }; }
                    return { ok: false, error: "unexpected message" };
                  }
                }
              }
            };
            context.globalThis = context;
            vm.createContext(context);
            vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), context, { filename: "content_adapter.js" });

            codeText = JSON.stringify(result);
            code.textContent = codeText;
            const mutation = [{ type: "characterData", target: code, addedNodes: [] }];
            observerCallback(mutation);
            observerCallback(mutation);
            sweepCallback();
            setTimeout(() => {
              const submitMessages = messages.filter((message) => message.type === "bdb-vnext-project-execution-submit");
              const claimMessages = messages.filter((message) => message.type === "bdb-vnext-project-launch-claim");
              const ackMessages = messages.filter((message) => message.type === "bdb-vnext-project-launch-ack");
              if (mode === "happy" || mode === "collapsed" || mode === "completed") {
                assert.equal(submitMessages.length, 1, "one result submit despite observer+sweep");
                assert.equal(claimMessages.length, mode === "completed" ? 0 : 1);
                assert.equal(ackMessages.length, mode === "completed" ? 0 : 1);
                assert.equal(sendClicks, mode === "happy" || mode === "collapsed" ? 1 : 0);
                assert.equal(composer.value, "");
                if (mode === "happy" || mode === "collapsed") assert.deepEqual(events, ["send", "ack"]);
              } else if (mode === "sent") {
                assert.equal(submitMessages.length, 1);
                assert.equal(claimMessages.length, 1);
                assert.equal(ackMessages.length, 1);
                assert.equal(sendClicks, 0, "already-sent handoff must not send twice");
                assert.deepEqual(events, ["ack"]);
              } else if (mode === "nonempty") {
                assert.equal(submitMessages.length, 1);
                assert.equal(claimMessages.length, 0, "foreign composer must prevent claim");
                assert.equal(sendClicks, 0);
                assert.equal(composer.value, "user draft");
              } else if (mode === "edited") {
                assert.equal(submitMessages.length, 1);
                assert.equal(sendClicks, 0, "user edit must cancel send");
                assert.equal(composer.value, "user edited this");
              } else if (mode === "stopped") {
                assert.equal(submitMessages.length, 1);
                assert.equal(sendClicks, 0, "STOP must cancel a pending auto-send");
                assert.equal(ackMessages.length, 0);
              } else if (mode === "nosend") {
                assert.equal(submitMessages.length, 1);
                assert.equal(claimMessages.length, 1);
                assert.equal(ackMessages.length, 0, "missing Send must not ACK/consume");
                assert.equal(sendClicks, 0);
              } else if (mode === "noeffect") {
                assert.equal(submitMessages.length, 1);
                assert.equal(claimMessages.length, 1);
                assert.equal(ackMessages.length, 0, "click without physical send effect must not ACK");
                assert.equal(sendClicks, 1, "exactly one Send attempt is permitted");
                assert.equal(composer.value, prompt);
              } else if (mode === "fail" || mode === "replay-fail") {
                assert.equal(submitMessages.length, 1, "transport success must still expose semantic failure");
                assert.equal(claimMessages.length, 0, "failed result must not claim a next launch");
                assert.equal(ackMessages.length, 0, "failed result must not ACK a next launch");
                assert.equal(sendClicks, 0, "failed result must not send a next prompt");
                const nodes = [];
                const visit = (item) => { nodes.push(item); for (const child of item.children || []) visit(child); };
                visit(assistant);
                const output = nodes.find((item) => item.className === "bdb-vnext-project-execution-output");
                const resultButton = nodes.find((item) => item.className === "bdb-vnext-project-execution-submit");
                assert.doesNotMatch(resultButton?.textContent || "", /Result accepted/);
                assert.match(output?.textContent || "", mode === "replay-fail" ? /Replayed FAIL/ : /Failed:/);
              } else {
                assert.equal(submitMessages.length, 0, "stale/wrong gate must not submit");
                assert.equal(sendClicks, 0);
              }
            }, mode === "noeffect" ? 5300 : 350);
            '''
        ),
        encoding="utf-8",
    )
    for mode in ("happy", "collapsed", "completed", "nonempty", "edited", "stopped", "nosend", "noeffect", "sent", "fail", "replay-fail", "stale", "wrong"):
        completed = subprocess.run(
            [node, str(harness), str(ROOT / "browser_extension_vnext" / "content_adapter.js"), mode],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr


def test_vnext_project_auto_contract_preserves_manual_fallback_and_stops_at_boundaries() -> None:
    adapter = (ROOT / "browser_extension_vnext" / "content_adapter.js").read_text(encoding="utf-8")
    worker = (ROOT / "browser_extension_vnext" / "transport_worker.js").read_text(encoding="utf-8")
    native = (ROOT / "bdb_vnext" / "m9b_native_host.py").read_text(encoding="utf-8")
    assert "projectAutoSubmissions" in adapter
    assert 'phase: "detected"' in adapter
    for phase in ("submitting_result", "result_accepted", "awaiting_next_launch", "inserting_prompt", "awaiting_send_ready", "sending_prompt", "verifying_send_effect", "sent", "stopped", "error"):
        assert f'"{phase}"' in adapter
    assert 'type: "bdb-vnext-project-auto-stop"' in adapter or 'PROJECT_AUTO_STOP_MESSAGE = "bdb-vnext-project-auto-stop"' in adapter
    assert '"bdb-vnext-project-execution-status"' in worker
    assert '"project_execution_status"' in native
    assert '"BDB vNext: Result accepted"' in adapter
    assert '"BDB vNext: Result failed"' in adapter
    assert 'result_not_accepted' in adapter
    assert 'button.textContent = "BDB vNext: Retry result"' in adapter
    assert 'projectAutoStop("canonical_launch_gate_rejected")' in adapter
    assert 'const sent = await projectAutoSendInserted' in adapter
    assert "document.hasFocus" not in adapter
    assert '"SEND_ATTEMPTED"' in adapter
    assert '"SEND_CONFIRMED"' in adapter
    assert 'projectAck(claimed.launch_id, claimId, {' in adapter
    send_index = adapter.index('const sent = await projectAutoSendInserted')
    assert send_index < adapter.index('const acknowledged = await projectAck(claimed.launch_id, claimId, {', send_index)
    assert 'handoff_status: "SENT"' in worker
    assert 'launch_handoff' in native

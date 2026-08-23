from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bdb_vnext.composition import BROWSER_EXTENSION_ID, PROTOCOL_GENERATION
from bdb_vnext.m9b_native_host import M9B_NATIVE_REQUEST_SCHEMA, VNextNativeConfig, handle_message
from bdb_vnext.project_launch import ProjectLaunchQueueAdapter, ProjectLaunchQueueError


ROOT = Path(__file__).resolve().parents[1]


def _native_message(action: str, **extra: object) -> dict[str, object]:
    return {
        "schema": M9B_NATIVE_REQUEST_SCHEMA,
        "request_id": "project-launch-test",
        "action": action,
        "protocol_generation": PROTOCOL_GENERATION,
        "browser_extension_id": BROWSER_EXTENSION_ID,
        **extra,
    }


def test_vnext_queue_claim_lease_ack_preserves_enriched_metadata(tmp_path: Path) -> None:
    queue = ProjectLaunchQueueAdapter(tmp_path / "project-launch-queue.json")
    launch = queue.enqueue(
        repo_alias="giclee-project",
        prompt="line one\nline two",
        project_id="project-1",
        plan_version="3",
        task_id="task-1",
        execution_binding_id="binding-1",
        correlation_id="correlation-1",
        command_id="command-1",
        expected_repo_head_before="a" * 40,
    )
    claim_id = str(uuid.uuid4())
    assert queue.claim(launch_id=launch.launch_id, claim_id=claim_id) == launch
    assert queue.claim(launch_id=launch.launch_id, claim_id=str(uuid.uuid4())) is None
    document = json.loads((tmp_path / "project-launch-queue.json").read_text(encoding="utf-8"))
    assert document["pending"]["prompt"] == "line one\nline two"
    assert document["pending"]["auto_send"] is False
    assert document["pending"]["project_id"] == "project-1"
    assert document["claim"]["claim_id"] == claim_id
    assert queue.acknowledge(launch_id=launch.launch_id, claim_id=str(uuid.uuid4())) is False
    assert queue.acknowledge(launch_id=launch.launch_id, claim_id=claim_id) is True
    assert queue.peek() is None


def test_vnext_queue_expired_claim_can_be_reclaimed_without_duplicate_launch(tmp_path: Path) -> None:
    now = datetime(2026, 8, 22, tzinfo=timezone.utc)
    clock = [now]
    queue = ProjectLaunchQueueAdapter(tmp_path / "project-launch-queue.json", now_fn=lambda: clock[0])
    launch = queue.enqueue(repo_alias="demo-project", prompt="bounded")
    first = str(uuid.uuid4())
    second = str(uuid.uuid4())
    assert queue.claim(launch_id=launch.launch_id, claim_id=first, lease_seconds=5) == launch
    clock[0] = now + timedelta(seconds=6)
    assert queue.claim(launch_id=launch.launch_id, claim_id=second, lease_seconds=5) == launch
    assert queue.acknowledge(launch_id=launch.launch_id, claim_id=first) is False
    assert queue.acknowledge(launch_id=launch.launch_id, claim_id=second) is True


def test_vnext_native_project_launch_operations_reuse_one_queue_and_do_not_require_intake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    queue_path = tmp_path / "shared" / "project-launch-queue.json"
    queue = ProjectLaunchQueueAdapter(queue_path)
    launch = queue.enqueue(repo_alias="demo-project", prompt="Create\ncalculator", project_id="p", task_id="t")
    monkeypatch.setattr("bdb_vnext.m9b_native_host.default_project_launch_queue_path", lambda: queue_path)
    config = VNextNativeConfig(
        runtime_root=tmp_path / "runtime",
        legacy_runtime_root=tmp_path / "legacy",
        bootstrap_authority_root=tmp_path / "bootstrap",
    )
    peek = handle_message(config, _native_message("project_launch_peek"))
    assert peek["status"] == "project_launch"
    assert peek["launch"]["project_id"] == "p"
    claim_id = str(uuid.uuid4())
    claimed = handle_message(config, _native_message("project_launch_claim", launch_id=launch.launch_id, claim_id=claim_id))
    assert claimed["status"] == "claimed"
    denied = handle_message(config, _native_message("project_launch_claim", launch_id=launch.launch_id, claim_id=str(uuid.uuid4())))
    assert denied["status"] == "busy_or_missing"
    acknowledged = handle_message(config, _native_message("project_launch_ack", launch_id=launch.launch_id, claim_id=claim_id))
    assert acknowledged["status"] == "acknowledged"
    assert queue.peek() is None


def test_vnext_project_launch_browser_path_is_bounded_and_submission_path_remains() -> None:
    adapter = (ROOT / "browser_extension_vnext" / "content_adapter.js").read_text(encoding="utf-8")
    worker = (ROOT / "browser_extension_vnext" / "transport_worker.js").read_text(encoding="utf-8")
    assert '"bdb-vnext-project-launch-peek"' in worker
    assert '"bdb-vnext-project-launch-claim"' in worker
    assert '"bdb-vnext-project-launch-ack"' in worker
    assert 'native("project_launch_peek")' in worker
    assert 'native("project_launch_claim"' in worker
    assert 'native("project_launch_ack"' in worker
    assert "message.conversation_id" in worker
    assert 'value.auto_send === false' in adapter
    assert "document.hasFocus()" in adapter
    assert "document.visibilityState === \"visible\"" in adapter
    assert "tab_instance_id" in adapter
    assert "insertText" in adapter
    assert ".click(" not in adapter
    assert "bdb-vnext-submission-v1" in adapter


def test_vnext_project_execution_result_is_json_only_and_has_separate_submit_surface(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the Browser content contract")
    harness = tmp_path / "project-execution-content.cjs"
    harness.write_text(
        textwrap.dedent(
            r'''
            "use strict";
            const assert = require("node:assert/strict");
            const fs = require("node:fs");
            const vm = require("node:vm");
            class Element {
              constructor(kind = "div", text = "") { this.kind = kind; this.textContent = text; this.children = []; this.dataset = {}; this.style = {}; this.parentElement = null; this.listeners = {}; }
              get innerText() { return this.textContent; }
              set innerText(value) { this.textContent = value; }
              getBoundingClientRect() { return { width: 600, height: 30 }; }
              querySelector() { return null; }
              querySelectorAll() { return []; }
              closest(selector) { if (selector === "pre") return host; if (selector.includes("assistant")) return assistant; return null; }
              addEventListener(type, fn) { this.listeners[type] = fn; }
              append(...items) { this.children.push(...items); }
              insertAdjacentElement(_where, item) { inserted.push(item); }
              setAttribute() {}
              focus() {}
              dispatchEvent() {}
            }
            const mode = process.argv[3];
            const valid = JSON.stringify({ schema: "bdb-project-execution-submission-v1", project_id: "project-1", plan_version: "1", task_id: "P0-01", execution_binding_id: "binding-1", correlation_id: "corr-1", command_id: "command-1", repo_alias: "premium-calculator", head_before: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", head_after: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", execution_status: "PASS", validation_status: "PASS", promotion_status: "NOT_RUN", result_summary: "done", evidence_refs: [], criteria: [] });
            const generic = JSON.stringify({ schema: "bdb-vnext-submission-v1", submission_key: "submission-1", intent_revision: {}, intent: {}, conversation_binding: {}, consumer_binding: {} });
            const text = mode === "valid" ? valid : mode === "generic" ? generic : "BDB_SUBMISSION:\nschema: bdb-project-execution-submission-v1\nstatus: COMPLETED";
            const assistant = new Element("assistant");
            const host = new Element("pre");
            const block = new Element("code", text);
            const inserted = [];
            const context = {
              console, HTMLElement: Element, HTMLTextAreaElement: class extends Element {}, HTMLInputElement: class extends Element {},
              InputEvent: class {}, Event: class {}, TextEncoder, Set, Map, crypto: { randomUUID: () => "11111111-1111-4111-8111-111111111111" },
              window: { getComputedStyle: () => ({ visibility: "visible", display: "block" }) },
              location: { protocol: "https:", hostname: "chatgpt.com", pathname: "/c/abcdef12-3456-4789-abcd-abcdef123456" },
              document: { visibilityState: "visible", hasFocus: () => true, documentElement: {}, querySelector: () => null, querySelectorAll: (selector) => selector === "pre code" ? [block] : [], createElement: (kind) => new Element(kind), execCommand: () => false },
              MutationObserver: class { observe() {} }, setInterval: () => ({ unref() {} }),
              chrome: { storage: { local: { async get() { return {}; }, async set() {} } }, runtime: { onMessage: { addListener() {} }, async sendMessage(message) { if (message.type === "bdb-vnext-project-launch-peek") return { ok: true, response: { status: "empty" } }; return { ok: false, error: "not invoked" }; } } }
            };
            context.globalThis = context;
            vm.createContext(context);
            vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), context, { filename: process.argv[2] });
            setTimeout(() => {
              if (mode === "valid") assert.equal(inserted[0]?.children[0]?.textContent, "BDB vNext: Submit result");
              else if (mode === "generic") assert.equal(inserted[0]?.children[0]?.textContent, "BDB vNext: Submit");
              else assert.equal(inserted.length, 0, "YAML must never create a project result panel");
            }, 25);
            '''
        ),
        encoding="utf-8",
    )
    for mode in ("valid", "generic", "yaml"):
        completed = subprocess.run([node, str(harness), str(ROOT / "browser_extension_vnext" / "content_adapter.js"), mode], capture_output=True, text=True, check=False, timeout=10)
        assert completed.returncode == 0, completed.stdout + completed.stderr
    adapter = (ROOT / "browser_extension_vnext" / "content_adapter.js").read_text(encoding="utf-8")
    worker = (ROOT / "browser_extension_vnext" / "transport_worker.js").read_text(encoding="utf-8")
    assert '"bdb-project-execution-submission-v1"' in adapter
    assert 'type: "bdb-vnext-project-execution-submit"' in adapter
    assert 'native("project_execution_submit"' in worker
    assert "JSON.parse(text)" in adapter


def test_vnext_project_execution_result_is_detected_from_streamed_dom_mutations(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the Browser content contract")
    harness = tmp_path / "project-execution-stream.cjs"
    harness.write_text(
        textwrap.dedent(
            r'''
            "use strict";
            const assert = require("node:assert/strict");
            const fs = require("node:fs");
            const vm = require("node:vm");

            class TextNode {
              constructor(data, parent = null) { this.nodeType = 3; this.data = data; this.parentElement = parent; this.parentNode = parent; }
              get textContent() { return this.data; }
              set textContent(value) { this.data = String(value); }
            }

            class Element {
              constructor(kind = "div", text = "") {
                this.nodeType = 1;
                this.kind = kind;
                this._text = text;
                this.children = [];
                this.parentElement = null;
                this.parentNode = null;
                this.dataset = {};
                this.style = {};
                this.listeners = {};
              }
              get textContent() {
                return this.children.length ? this.children.map((child) => child.textContent || "").join("") : this._text;
              }
              set textContent(value) {
                this.children = [];
                this._text = String(value);
              }
              get innerText() { return this.textContent; }
              set innerText(value) { this.textContent = value; }
              append(...items) {
                for (const item of items) {
                  this.children.push(item);
                  item.parentElement = this;
                  item.parentNode = this;
                }
              }
              appendChild(item) { this.append(item); return item; }
              querySelectorAll(selector) {
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
              querySelector() { return null; }
              matches(selector) { return selector === "pre code" && this.kind === "code" && this.parentElement?.kind === "pre"; }
              closest(selector) {
                let current = this;
                while (current) {
                  if (selector === "pre" && current.kind === "pre") return current;
                  if (selector.includes("assistant") && current.kind === "assistant") return current;
                  current = current.parentElement;
                }
                return null;
              }
              getBoundingClientRect() { return { width: 600, height: 30 }; }
              addEventListener(type, fn) { this.listeners[type] = fn; }
              insertAdjacentElement(_where, item) { inserted.push(item); }
              setAttribute() {}
              focus() {}
              dispatchEvent() {}
            }

            const mode = process.argv[3];
            const project = JSON.stringify({ schema: "bdb-project-execution-submission-v1", project_id: "project-1", plan_version: "1", task_id: "P0-01", execution_binding_id: "binding-1", correlation_id: "corr-1", command_id: "command-1", repo_alias: "premium-calculator", head_before: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", head_after: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", execution_status: "PASS", validation_status: "PASS", promotion_status: "NOT_RUN", result_summary: "done", evidence_refs: [], criteria: [] });
            const generic = JSON.stringify({ schema: "bdb-vnext-submission-v1", submission_key: "submission-1", intent_revision: {}, intent: {}, conversation_binding: {}, consumer_binding: {} });
            const invalid = mode === "yaml" || mode === "sweep-yaml" ? "BDB_SUBMISSION:\nschema: bdb-project-execution-submission-v1\nstatus: COMPLETED" : "{\"schema\":\"bdb-project-execution-submission-v1\"}";
            const finalText = mode === "project" || mode === "duplicate" || mode === "sweep" || mode === "delayed" || mode === "hidden" ? project : mode === "generic" || mode === "sweep-generic" ? generic : invalid;
            const safetyMode = new Set(["sweep", "delayed", "hidden", "sweep-generic", "sweep-partial", "sweep-yaml"]).has(mode);
            const assistant = new Element("assistant");
            const pre = new Element("pre");
            const code = new Element("code");
            const text = new TextNode("");
            assistant.append(pre);
            pre.append(code);
            code.append(text);
            const documentElement = new Element("html");
            documentElement.append(assistant);
            const inserted = [];
            let observerCallback = null;
            let observerOptions = null;
            let observerInvoked = false;
            let sweepCallback = null;
            const documentState = { visibility: "visible" };
            class FakeMutationObserver {
              constructor(callback) { observerCallback = (...args) => { observerInvoked = true; callback(...args); }; }
              observe(_target, options) { observerOptions = options; }
            }
            const context = {
              console,
              HTMLElement: Element,
              HTMLTextAreaElement: class extends Element {},
              HTMLInputElement: class extends Element {},
              InputEvent: class {},
              Event: class {},
              TextEncoder,
              Set,
              Map,
              crypto: { randomUUID: () => "11111111-1111-4111-8111-111111111111" },
              window: { getComputedStyle: () => ({ visibility: "visible", display: "block" }) },
              location: { protocol: "https:", hostname: "chatgpt.com", pathname: "/c/abcdef12-3456-4789-abcd-abcdef123456" },
              document: {
                get visibilityState() { return documentState.visibility; },
                hasFocus: () => true,
                documentElement,
                querySelector: () => null,
                querySelectorAll: (selector) => selector === "pre code" ? documentElement.querySelectorAll(selector) : [],
                createElement: (kind) => new Element(kind),
                execCommand: () => false
              },
              MutationObserver: FakeMutationObserver,
              setTimeout,
              clearTimeout,
              setInterval: (callback, interval) => {
                if (interval === 750) sweepCallback = callback;
                return { unref() {} };
              },
              chrome: {
                storage: { local: { async get() { return {}; }, async set() {} } },
                runtime: {
                  onMessage: { addListener() {} },
                  async sendMessage(message) {
                    if (message.type === "bdb-vnext-project-launch-peek") return { ok: true, response: { status: "empty" } };
                    return { ok: false, error: "not invoked" };
                  }
                }
              }
            };
            context.globalThis = context;
            vm.createContext(context);
            vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), context, { filename: "content_adapter.js" });
            assert.equal(observerOptions.characterData, true, "stream observer must include characterData");
            assert.equal(documentElement.dataset.bdbVnextContentAdapter, "bdb-vnext-content-adapter-live-sweep-v1");
            assert.equal(typeof sweepCallback, "function", "bounded canonical-result sweep must be scheduled");
            assert.equal(inserted.length, 0, "partial stream must not create a panel");

            if (safetyMode) {
              const assertSafetyResult = () => {
                const expectedButton = mode === "sweep-generic" ? "BDB vNext: Submit" : mode === "sweep-partial" || mode === "sweep-yaml" ? null : "BDB vNext: Submit result";
                if (expectedButton) {
                  assert.equal(inserted.length, 1, "safety sweep detects one canonical result");
                  assert.equal(inserted[0].children[0].textContent, expectedButton);
                } else {
                  assert.equal(inserted.length, 0, "invalid JSON/YAML must stay undecorated during safety sweep");
                }
              };
              const runSafetySweep = () => {
                if (mode === "hidden") {
                  documentState.visibility = "hidden";
                  sweepCallback();
                  assert.equal(inserted.length, 0, "hidden document must not run the canonical-result sweep");
                  documentState.visibility = "visible";
                }
                sweepCallback();
                sweepCallback();
                assert.equal(observerInvoked, false, "safety sweep must not depend on a MutationObserver callback");
                setTimeout(assertSafetyResult, 5);
              };
              if (mode === "delayed") {
                setTimeout(() => {
                  const delayedPre = new Element("pre");
                  const delayedCode = new Element("code");
                  delayedPre.append(delayedCode);
                  delayedCode.append(new TextNode(finalText));
                  assistant.append(delayedPre);
                  setTimeout(runSafetySweep, 5);
                }, 5);
              } else {
                text.data = finalText;
                runSafetySweep();
              }
            } else {

              for (const chunk of [finalText.slice(0, 11), finalText.slice(0, 37), finalText]) {
                text.data = chunk;
                observerCallback([{ type: "characterData", target: text, addedNodes: [] }]);
              }
              observerCallback([{ type: "childList", target: code, addedNodes: [text] }]);

              setTimeout(() => {
                const expectedButton = mode === "project" || mode === "duplicate" ? "BDB vNext: Submit result" : mode === "generic" ? "BDB vNext: Submit" : null;
                if (expectedButton) {
                  assert.equal(inserted.length, 1, "a streamed canonical result gets one panel");
                  assert.equal(inserted[0].children[0].textContent, expectedButton);
                } else {
                  assert.equal(inserted.length, 0, "invalid JSON/YAML must stay undecorated");
                }
                if (mode === "duplicate") {
                  text.data = finalText;
                  observerCallback([{ type: "characterData", target: text, addedNodes: [] }]);
                  setTimeout(() => assert.equal(inserted.length, 1, "a decorated block must not receive a duplicate panel"), 60);
                }
              }, 80);
            }
            '''
        ),
        encoding="utf-8",
    )
    for mode in ("project", "duplicate", "generic", "json", "yaml", "sweep", "delayed", "hidden", "sweep-generic", "sweep-partial", "sweep-yaml"):
        completed = subprocess.run(
            [node, str(harness), str(ROOT / "browser_extension_vnext" / "content_adapter.js"), mode],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr


def test_vnext_project_find_composer_uses_ordered_selector_priority_and_fails_closed(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the Browser content contract")
    harness = tmp_path / "project-composer-resolution.cjs"
    harness.write_text(
        textwrap.dedent(
            r'''
            "use strict";
            const assert = require("node:assert/strict");
            const fs = require("node:fs");
            const vm = require("node:vm");

            class Element {
              constructor({ id = "", owner = null } = {}) {
                this.id = id;
                this.owner = owner;
                this.style = {};
                this.dataset = {};
                this.textContent = "";
              }
              get innerText() { return this.textContent; }
              set innerText(value) { this.textContent = value; }
              getBoundingClientRect() { return { width: 600, height: 30 }; }
              closest() { return this.owner; }
            }

            class TextArea extends Element {}

            function resolveComposer(source, scenario) {
              const precise = new Element({ id: "prompt-textarea" });
              const assistant = new Element({ owner: null });
              assistant.closest = () => assistant;
              const genericOne = new Element();
              const genericTwo = new Element();
              const fallback = new Element();
              const candidates = {
                "#prompt-textarea": scenario === "priority" ? [precise] : [],
                "textarea[data-testid='textbox']": [],
                "textarea[placeholder*='Message']": [],
                "[contenteditable='true'][role='textbox']": scenario === "ambiguous"
                  ? [genericOne, genericTwo]
                  : scenario === "fallback" ? [fallback] : [],
                "[contenteditable='true']": scenario === "priority"
                  ? [assistant]
                  : scenario === "ambiguous" ? [genericOne, genericTwo]
                  : scenario === "fallback" ? [fallback] : []
              };
              const context = {
                console,
                HTMLElement: Element,
                HTMLTextAreaElement: TextArea,
                HTMLInputElement: class extends Element {},
                InputEvent: class {},
                Event: class {},
                TextEncoder,
                Set,
                Map,
                crypto: { randomUUID: () => "22222222-2222-4222-8222-222222222222" },
                window: { getComputedStyle: () => ({ visibility: "visible", display: "block" }) },
                location: { protocol: "https:", hostname: "chatgpt.com", pathname: "/c/abcdef12-3456-4789-abcd-abcdef123456" },
                document: {
                  visibilityState: "visible",
                  hasFocus: () => true,
                  documentElement: {},
                  querySelector: () => null,
                  querySelectorAll: (selector) => selector === "pre code" ? [] : (candidates[selector] || []),
                  createElement: () => new Element(),
                  execCommand: () => false
                },
                MutationObserver: class { observe() {} },
                setInterval: (fn) => ({ unref() {}, fn }),
                chrome: {}
              };
              context.globalThis = context;
              vm.createContext(context);
              vm.runInContext(source, context, { filename: "content_adapter.js" });
              return { result: context.projectFindComposer(), precise, fallback };
            }

            const source = fs.readFileSync(process.argv[2], "utf8");
            const priority = resolveComposer(source, "priority");
            assert.equal(priority.result, priority.precise, "precise composer must win over generic assistant contenteditable");
            const ambiguous = resolveComposer(source, "ambiguous");
            assert.equal(ambiguous.result, null, "multiple matches for one fallback selector must fail closed");
            const fallback = resolveComposer(source, "fallback");
            assert.equal(fallback.result, fallback.fallback, "one valid fallback textbox must be selected");
            '''
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [node, str(harness), str(ROOT / "browser_extension_vnext" / "content_adapter.js")],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_vnext_project_launch_inserts_exact_multiline_prompt_without_send(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the Browser content contract")
    harness = tmp_path / "project-launch-content.cjs"
    harness.write_text(
        textwrap.dedent(
            r'''
            "use strict";
            const assert = require("node:assert/strict");
            const fs = require("node:fs");
            const vm = require("node:vm");
            class Element {
              constructor(kind = "div") { this.kind = kind; this.textContent = ""; this.children = []; this.dataset = {}; this.style = {}; }
              get innerText() { return this.textContent; }
              set innerText(value) { this.textContent = value; }
              getBoundingClientRect() { return { width: 600, height: 30 }; }
              querySelector() { return null; }
              querySelectorAll(selector) { return selector.includes("contenteditable") ? [this] : []; }
              focus() { this.focused = true; }
              dispatchEvent() {}
              insertAdjacentElement() {}
              setAttribute() {}
            }
            const composer = new Element();
            composer.textContent = process.argv[3] || "";
            const messages = [];
            const store = {};
            const launchId = "11111111-1111-4111-8111-111111111111";
            const claimId = "22222222-2222-4222-8222-222222222222";
            const launch = { schema: "bdb-project-launch-v1", launch_id: launchId, repo_alias: "demo-project", prompt: "line one\nline two", auto_send: false, created_at: "2026-08-22T00:00:00Z", expires_at: "2026-08-22T00:10:00Z" };
            const context = {
              console,
              HTMLElement: Element,
              HTMLTextAreaElement: class extends Element {},
              HTMLInputElement: class extends Element {},
              InputEvent: class {},
              Event: class {},
              TextEncoder,
              Set,
              Map,
              crypto: { randomUUID: () => claimId },
              window: { getComputedStyle: () => ({ visibility: "visible", display: "block" }) },
              location: { protocol: "https:", hostname: "chatgpt.com", pathname: "/c/abcdef12-3456-4789-abcd-abcdef123456" },
              document: {
                visibilityState: "visible",
                hasFocus: () => true,
                documentElement: {},
                querySelector: () => null,
                querySelectorAll: (selector) => selector === "pre code" ? [] : (selector.includes("contenteditable") ? [composer] : []),
                createElement: () => new Element(),
                execCommand: () => false
              },
              MutationObserver: class { observe() {} },
              setInterval: (fn) => ({ unref() {}, fn }),
              chrome: {
                storage: { local: {
                  async get(key) { return Object.prototype.hasOwnProperty.call(store, key) ? { [key]: store[key] } : {}; },
                  async set(values) { Object.assign(store, values); }
                }},
                runtime: { async sendMessage(message) {
                  messages.push(message);
                  if (message.type === "bdb-vnext-project-launch-peek") return { ok: true, response: { status: "project_launch", launch } };
                  if (message.type === "bdb-vnext-project-launch-claim") return { ok: true, response: { status: "claimed", launch } };
                  if (message.type === "bdb-vnext-project-launch-ack") return { ok: true, response: { status: "acknowledged" } };
                  throw new Error("unexpected message");
                }}
              }
            };
            context.globalThis = context;
            vm.createContext(context);
            vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), context, { filename: process.argv[2] });
            setTimeout(() => {
              if (process.argv[3] === "user is typing") {
                assert.equal(composer.textContent, process.argv[3]);
                assert.deepEqual(messages.map((item) => item.type), ["bdb-vnext-project-launch-peek"]);
              } else {
                assert.equal(composer.textContent, launch.prompt);
                assert.deepEqual(messages.map((item) => item.type), [
                  "bdb-vnext-project-launch-peek",
                  "bdb-vnext-project-launch-claim",
                  "bdb-vnext-project-launch-ack"
                ]);
              }
              assert.equal(messages.some((item) => item.type === "bdb-vnext-submit"), false);
            }, 25);
            '''
        ),
        encoding="utf-8",
    )
    for initial in ("", "user is typing", "line one\nline two"):
        completed = subprocess.run(
            [node, str(harness), str(ROOT / "browser_extension_vnext" / "content_adapter.js"), initial],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr


def test_vnext_popup_inserts_pending_prompt_into_user_selected_conversation(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the Browser content contract")
    harness = tmp_path / "project-launch-popup-selection.cjs"
    harness.write_text(
        textwrap.dedent(
            r'''
            "use strict";
            const assert = require("node:assert/strict");
            const fs = require("node:fs");
            const vm = require("node:vm");
            class Element {
              constructor() { this.textContent = ""; this.dataset = {}; this.style = {}; }
              get innerText() { return this.textContent; }
              set innerText(value) { this.textContent = value; }
              getBoundingClientRect() { return { width: 600, height: 30 }; }
              querySelector() { return null; }
              querySelectorAll(selector) { return selector.includes("contenteditable") ? [this] : []; }
              focus() { this.focused = true; }
              dispatchEvent() {}
              insertAdjacentElement() {}
              setAttribute() {}
            }
            const composer = new Element();
            const messages = [];
            const listeners = [];
            const store = {};
            const launchId = "11111111-1111-4111-8111-111111111111";
            const claimId = "22222222-2222-4222-8222-222222222222";
            const launch = {
              schema: "bdb-project-launch-v1",
              launch_id: launchId,
              repo_alias: "demo-project",
              prompt: "initial project prompt",
              auto_send: false,
              created_at: "2026-08-22T00:00:00Z",
              expires_at: "2026-08-22T00:10:00Z"
            };
            const context = {
              console,
              HTMLElement: Element,
              HTMLTextAreaElement: class extends Element {},
              HTMLInputElement: class extends Element {},
              InputEvent: class {},
              Event: class {},
              TextEncoder,
              Set,
              Map,
              crypto: { randomUUID: () => claimId },
              window: { getComputedStyle: () => ({ visibility: "visible", display: "block" }) },
              location: { protocol: "https:", hostname: "chatgpt.com", pathname: "/c/abcdef12-3456-4789-abcd-abcdef123456" },
              document: {
                visibilityState: "visible",
                hasFocus: () => false,
                documentElement: {},
                querySelector: () => null,
                querySelectorAll: (selector) => selector.includes("contenteditable") ? [composer] : [],
                createElement: () => new Element(),
                execCommand: () => false
              },
              MutationObserver: class { observe() {} },
              setInterval: () => ({ unref() {} }),
              chrome: {
                storage: { local: {
                  async get(key) { return Object.prototype.hasOwnProperty.call(store, key) ? { [key]: store[key] } : {}; },
                  async set(values) { Object.assign(store, values); }
                }},
                runtime: {
                  onMessage: { addListener(listener) { listeners.push(listener); } },
                  async sendMessage(message) {
                    messages.push(message);
                    if (message.type === "bdb-vnext-project-launch-peek") return { ok: true, response: { status: "project_launch", launch } };
                    if (message.type === "bdb-vnext-project-launch-claim") return { ok: true, response: { status: "claimed", launch } };
                    if (message.type === "bdb-vnext-project-launch-ack") return { ok: true, response: { status: "acknowledged" } };
                    throw new Error("unexpected message");
                  }
                }
              }
            };
            context.globalThis = context;
            vm.createContext(context);
            vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), context, { filename: process.argv[2] });
            setTimeout(async () => {
              assert.equal(listeners.length, 1, "content adapter must expose the explicit popup insertion action");
              messages.length = 0;
              const result = await new Promise((resolve) => listeners[0]({ type: "bdb-vnext-project-launch-insert" }, {}, resolve));
              assert.equal(result.ok, true);
              assert.equal(result.code, "inserted");
              assert.equal(result.launch_id, launchId);
              assert.equal(composer.textContent, launch.prompt, "the selected conversation composer receives the pending prompt");
                assert.deepEqual(messages.map((item) => item.type), [
                  "bdb-vnext-project-launch-peek",
                  "bdb-vnext-project-launch-claim",
                  "bdb-vnext-project-launch-ack"
                ]);
                assert.equal(messages[1].conversation_id, "abcdef12-3456-4789-abcd-abcdef123456");
                assert.equal(messages.some((item) => item.type === "bdb-vnext-submit"), false);
            }, 25);
            '''
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [node, str(harness), str(ROOT / "browser_extension_vnext" / "content_adapter.js")],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr

    popup = (ROOT / "browser_extension_vnext" / "popup.html").read_text(encoding="utf-8")
    popup_js = (ROOT / "browser_extension_vnext" / "popup.js").read_text(encoding="utf-8")
    assert 'id="insert-project-prompt"' in popup
    assert "Wstaw prompt początkowy" in popup
    assert "nowej bez /c/" in popup
    assert 'chrome.tabs.query({ active: true, currentWindow: true })' in popup_js
    assert 'type: "bdb-vnext-project-launch-insert"' in popup_js
    assert "nowa bez /c/" in popup_js


def test_vnext_popup_inserts_pending_prompt_into_genuinely_new_chat(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the Browser content contract")
    harness = tmp_path / "project-launch-new-chat.cjs"
    harness.write_text(
        textwrap.dedent(
            r'''
            "use strict";
            const assert = require("node:assert/strict");
            const fs = require("node:fs");
            const vm = require("node:vm");
            class Element {
              constructor() { this.textContent = ""; this.dataset = {}; this.style = {}; }
              get innerText() { return this.textContent; }
              set innerText(value) { this.textContent = value; }
              getBoundingClientRect() { return { width: 600, height: 30 }; }
              querySelector() { return null; }
              querySelectorAll(selector) { return selector.includes("contenteditable") ? [this] : []; }
              focus() { this.focused = true; }
              dispatchEvent() {}
              insertAdjacentElement() {}
              setAttribute() {}
            }
            const composer = new Element();
            const messages = [];
            const listeners = [];
            const store = {};
            const launchId = "11111111-1111-4111-8111-111111111111";
            const claimId = "22222222-2222-4222-8222-222222222222";
            const launch = {
              schema: "bdb-project-launch-v1",
              launch_id: launchId,
              repo_alias: "demo-project",
              prompt: "initial project prompt",
              auto_send: false,
              created_at: "2026-08-22T00:00:00Z",
              expires_at: "2026-08-22T00:10:00Z"
            };
            const context = {
              console,
              HTMLElement: Element,
              HTMLTextAreaElement: class extends Element {},
              HTMLInputElement: class extends Element {},
              InputEvent: class {},
              Event: class {},
              TextEncoder,
              Set,
              Map,
              crypto: { randomUUID: () => claimId },
              window: { getComputedStyle: () => ({ visibility: "visible", display: "block" }) },
              location: { protocol: "https:", hostname: "chatgpt.com", pathname: "/" },
              document: {
                visibilityState: "visible",
                hasFocus: () => false,
                documentElement: {},
                querySelector: () => null,
                querySelectorAll: (selector) => selector.includes("contenteditable") ? [composer] : [],
                createElement: () => new Element(),
                execCommand: () => false
              },
              MutationObserver: class { observe() {} },
              setInterval: () => ({ unref() {} }),
              chrome: {
                storage: { local: {
                  async get(key) { return Object.prototype.hasOwnProperty.call(store, key) ? { [key]: store[key] } : {}; },
                  async set(values) { Object.assign(store, values); }
                }},
                runtime: {
                  onMessage: { addListener(listener) { listeners.push(listener); } },
                  async sendMessage(message) {
                    messages.push(message);
                    if (message.type === "bdb-vnext-project-launch-peek") return { ok: true, response: { status: "project_launch", launch } };
                    if (message.type === "bdb-vnext-project-launch-claim") return { ok: true, response: { status: "claimed", launch } };
                    if (message.type === "bdb-vnext-project-launch-ack") return { ok: true, response: { status: "acknowledged" } };
                    throw new Error("unexpected message");
                  }
                }
              }
            };
            context.globalThis = context;
            vm.createContext(context);
            vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), context, { filename: process.argv[2] });
            setTimeout(async () => {
              assert.equal(listeners.length, 1, "new-chat content adapter must expose popup insertion");
              messages.length = 0;
              const result = await new Promise((resolve) => listeners[0]({ type: "bdb-vnext-project-launch-insert" }, {}, resolve));
              assert.equal(result.ok, true);
              assert.equal(result.code, "inserted");
              assert.equal(composer.textContent, launch.prompt);
              assert.deepEqual(messages.map((item) => item.type), [
                "bdb-vnext-project-launch-peek",
                "bdb-vnext-project-launch-claim",
                "bdb-vnext-project-launch-ack"
              ]);
              assert.equal(messages.some((item) => item.type === "bdb-vnext-submit"), false);
            }, 25);
            '''
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [node, str(harness), str(ROOT / "browser_extension_vnext" / "content_adapter.js")],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr

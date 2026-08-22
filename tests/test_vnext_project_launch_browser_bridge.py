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
    assert 'value.auto_send === false' in adapter
    assert "document.hasFocus()" in adapter
    assert "document.visibilityState === \"visible\"" in adapter
    assert "tab_instance_id" in adapter
    assert "insertText" in adapter
    assert ".click(" not in adapter
    assert "bdb-vnext-submission-v1" in adapter


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
    assert 'chrome.tabs.query({ active: true, currentWindow: true })' in popup_js
    assert 'type: "bdb-vnext-project-launch-insert"' in popup_js

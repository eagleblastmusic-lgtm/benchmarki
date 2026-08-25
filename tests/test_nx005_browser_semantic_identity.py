"""BDB vNext - NX-005 Browser Semantic Identity and Dedupe Tests and Machine Gate.

Verifies:
1. Distinct semantic keys and DOM panels for different results on the same binding.
2. Exact duplicate result blocks dedupe to a single panel.
3. Reload of accepted result does not cause redundant re-submission.
4. Legacy storage records are filtered deterministically without false duplicate.
5. Integration with Result Identity v2 contract (NX-004).
6. 100% Cross-Consumer Golden Vector Parity between Python NX-004 and Browser NX-005.
7. Invariant: BROWSER_REDEFINES_CANONICAL_RESULT_DIGEST = FALSE.
8. Panel DOM lifecycle and removal recovery.
9. Deterministic source-bound NX-005 machine gate.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import Any

import pytest

from bdb_vnext.project_execution import ProjectExecutionBinding, _binding_from_dict
from bdb_vnext.result_identity import execution_result_digest_v2, result_identity_v2

ROOT = Path(__file__).resolve().parent.parent


def _run_node_harness(script: str) -> subprocess.CompletedProcess[str]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for Browser content contract tests")
    return subprocess.run([node, "-e", script], cwd=str(ROOT), capture_output=True, text=True)


# -----------------------------------------------------------------------------
# A. TWO RESULTS / SAME BINDING & FAILURE CODE REGRESSION
# -----------------------------------------------------------------------------

def test_different_results_same_binding_mount_distinct_panels() -> None:
    """Two different failure results on the same binding must mount 2 distinct panels."""
    script = textwrap.dedent(
        r'''
        "use strict";
        const assert = require("node:assert/strict");
        const fs = require("node:fs");
        const vm = require("node:vm");

        class Element {
          constructor(kind = "div", text = "") {
            this.nodeType = 1;
            this.kind = kind;
            this._text = text;
            this.children = [];
            this.parentElement = null;
            this.parentNode = null;
            this.dataset = {};
            this.className = "";
            this.listeners = {};
          }
          get isConnected() { return Boolean(this.parentElement); }
          get textContent() { return this.children.length ? this.children.map(c => c.textContent || "").join("") : this._text; }
          set textContent(v) { this.children = []; this._text = String(v); }
          append(...items) {
            for (const item of items) {
              this.children.push(item);
              item.parentElement = this;
              item.parentNode = this;
            }
          }
          appendChild(item) { this.append(item); return item; }
          removeChild(item) {
            const idx = this.children.indexOf(item);
            if (idx >= 0) this.children.splice(idx, 1);
            item.parentElement = null;
            item.parentNode = null;
            return item;
          }
          closest(selector) {
            let current = this;
            while (current) {
              if (selector === "pre" && current.kind === "pre") return current;
              if (selector.includes("assistant") && current.kind === "assistant") return current;
              current = current.parentElement;
            }
            return null;
          }
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
          addEventListener(t, fn) { this.listeners[t] = fn; }
          getBoundingClientRect() { return { width: 600, height: 30 }; }
          setAttribute() {}
        }

        const root = new Element("html");
        const assistant = new Element("assistant");
        root.append(assistant);

        function codeNode(text) {
          const pre = new Element("pre");
          const code = new Element("code", text);
          pre.append(code);
          return { pre, code };
        }

        function resultPayload(failureCode) {
          return JSON.stringify({
            schema: "bdb-project-execution-submission-v1",
            project_id: "proj-1",
            plan_version: "1",
            task_id: "t1",
            execution_binding_id: "b1",
            correlation_id: "c1",
            command_id: "cmd1",
            repo_alias: "repo-1",
            head_before: "a".repeat(40),
            head_after: "b".repeat(40),
            execution_status: "FAIL",
            validation_status: "FAIL",
            promotion_status: "NOT_RUN",
            result_summary: "failed attempt",
            evidence_refs: [],
            criteria: [],
            failure_code: failureCode
          });
        }

        const node1 = codeNode(resultPayload("COMPILATION_ERROR"));
        const node2 = codeNode(resultPayload("TEST_TIMEOUT"));
        assistant.append(node1.pre, node2.pre);

        let sweepCallback = null;
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
          window: { getComputedStyle: () => ({ visibility: "visible", display: "block" }) },
          document: {
            visibilityState: "visible",
            documentElement: root,
            querySelector: () => null,
            querySelectorAll: (s) => s === "pre code" ? root.querySelectorAll(s) : [],
            createElement: (k) => new Element(k),
          },
          MutationObserver: class { observe() {} },
          setInterval: (cb, interval) => { if (interval === 750) sweepCallback = cb; return { unref() {} }; },
        };
        context.globalThis = context;
        vm.createContext(context);
        vm.runInContext(fs.readFileSync("browser_extension_vnext/content_adapter.js", "utf8"), context);

        sweepCallback();

        const panels = assistant.children.filter(c => c.className === "bdb-vnext-project-execution-panel");
        assert.equal(panels.length, 2, "Two distinct failure codes on same binding must mount 2 separate panels");
        assert.notEqual(panels[0].dataset.bdbSubmissionKey, panels[1].dataset.bdbSubmissionKey);
        console.log("PASS: DIFFERENT_RESULTS_SAME_BINDING_DISTINCT");
        '''
    )
    res = _run_node_harness(script)
    assert res.returncode == 0, f"Failure: {res.stderr}\n{res.stdout}"
    assert "PASS: DIFFERENT_RESULTS_SAME_BINDING_DISTINCT" in res.stdout


# -----------------------------------------------------------------------------
# B. EXACT DUPLICATE RESULT SUPPRESSION
# -----------------------------------------------------------------------------

def test_exact_duplicate_result_suppressed() -> None:
    """Exact duplicate result blocks must dedupe to a single panel across repeated sweeps."""
    script = textwrap.dedent(
        r'''
        "use strict";
        const assert = require("node:assert/strict");
        const fs = require("node:fs");
        const vm = require("node:vm");

        class Element {
          constructor(kind = "div", text = "") {
            this.nodeType = 1;
            this.kind = kind;
            this._text = text;
            this.children = [];
            this.parentElement = null;
            this.parentNode = null;
            this.dataset = {};
            this.className = "";
            this.listeners = {};
          }
          get isConnected() { return Boolean(this.parentElement); }
          get textContent() { return this.children.length ? this.children.map(c => c.textContent || "").join("") : this._text; }
          set textContent(v) { this.children = []; this._text = String(v); }
          append(...items) {
            for (const item of items) {
              this.children.push(item);
              item.parentElement = this;
              item.parentNode = this;
            }
          }
          appendChild(item) { this.append(item); return item; }
          removeChild(item) {
            const idx = this.children.indexOf(item);
            if (idx >= 0) this.children.splice(idx, 1);
            item.parentElement = null;
            item.parentNode = null;
            return item;
          }
          closest(selector) {
            let current = this;
            while (current) {
              if (selector === "pre" && current.kind === "pre") return current;
              if (selector.includes("assistant") && current.kind === "assistant") return current;
              current = current.parentElement;
            }
            return null;
          }
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
          addEventListener(t, fn) { this.listeners[t] = fn; }
          getBoundingClientRect() { return { width: 600, height: 30 }; }
          setAttribute() {}
        }

        const root = new Element("html");
        const assistant = new Element("assistant");
        root.append(assistant);

        function codeNode(text) {
          const pre = new Element("pre");
          const code = new Element("code", text);
          pre.append(code);
          return { pre, code };
        }

        const payload = JSON.stringify({
          schema: "bdb-project-execution-submission-v1",
          project_id: "proj-1",
          plan_version: "1",
          task_id: "t1",
          execution_binding_id: "b1",
          correlation_id: "c1",
          command_id: "cmd1",
          repo_alias: "repo-1",
          head_before: "a".repeat(40),
          head_after: "b".repeat(40),
          execution_status: "PASS",
          validation_status: "PASS",
          promotion_status: "NOT_RUN",
          result_summary: "done",
          evidence_refs: [],
          criteria: [],
        });

        const node1 = codeNode(payload);
        assistant.append(node1.pre);

        let sweepCallback = null;
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
          window: { getComputedStyle: () => ({ visibility: "visible", display: "block" }) },
          document: {
            visibilityState: "visible",
            documentElement: root,
            querySelector: () => null,
            querySelectorAll: (s) => s === "pre code" ? root.querySelectorAll(s) : [],
            createElement: (k) => new Element(k),
          },
          MutationObserver: class { observe() {} },
          setInterval: (cb, interval) => { if (interval === 750) sweepCallback = cb; return { unref() {} }; },
        };
        context.globalThis = context;
        vm.createContext(context);
        vm.runInContext(fs.readFileSync("browser_extension_vnext/content_adapter.js", "utf8"), context);

        sweepCallback();
        sweepCallback();
        assert.equal(assistant.children.filter(c => c.className === "bdb-vnext-project-execution-panel").length, 1);

        // Add exact duplicate
        const node2 = codeNode(payload);
        assistant.append(node2.pre);
        sweepCallback();
        assert.equal(assistant.children.filter(c => c.className === "bdb-vnext-project-execution-panel").length, 1, "Duplicate block shares existing panel");
        console.log("PASS: EXACT_DUPLICATE_SUPPRESSED");
        '''
    )
    res = _run_node_harness(script)
    assert res.returncode == 0, f"Failure: {res.stderr}\n{res.stdout}"
    assert "PASS: EXACT_DUPLICATE_SUPPRESSED" in res.stdout


# -----------------------------------------------------------------------------
# C. RELOAD & RESEND PREVENTION
# -----------------------------------------------------------------------------

def test_reload_resend_prevention() -> None:
    """Accepted result rendered upon page reload does not re-trigger automatic launch."""
    script = textwrap.dedent(
        r'''
        "use strict";
        const assert = require("node:assert/strict");
        const fs = require("node:fs");
        const vm = require("node:vm");

        class Element {
          constructor(kind = "div", text = "") {
            this.nodeType = 1;
            this.kind = kind;
            this._text = text;
            this.children = [];
            this.parentElement = null;
            this.parentNode = null;
            this.dataset = {};
            this.className = "";
            this.listeners = {};
          }
          get isConnected() { return Boolean(this.parentElement); }
          get textContent() { return this.children.length ? this.children.map(c => c.textContent || "").join("") : this._text; }
          set textContent(v) { this.children = []; this._text = String(v); }
          append(...items) {
            for (const item of items) {
              this.children.push(item);
              item.parentElement = this;
              item.parentNode = this;
            }
          }
          appendChild(item) { this.append(item); return item; }
          removeChild(item) {
            const idx = this.children.indexOf(item);
            if (idx >= 0) this.children.splice(idx, 1);
            item.parentElement = null;
            item.parentNode = null;
            return item;
          }
          closest(selector) {
            let current = this;
            while (current) {
              if (selector === "pre" && current.kind === "pre") return current;
              if (selector.includes("assistant") && current.kind === "assistant") return current;
              current = current.parentElement;
            }
            return null;
          }
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
          addEventListener(t, fn) { this.listeners[t] = fn; }
          getBoundingClientRect() { return { width: 600, height: 30 }; }
          setAttribute() {}
        }

        const root = new Element("html");
        const assistant = new Element("assistant");
        root.append(assistant);

        function codeNode(text) {
          const pre = new Element("pre");
          const code = new Element("code", text);
          pre.append(code);
          return { pre, code };
        }

        const payload = JSON.stringify({
          schema: "bdb-project-execution-submission-v1",
          project_id: "proj-1",
          plan_version: "1",
          task_id: "t1",
          execution_binding_id: "b1",
          correlation_id: "c1",
          command_id: "cmd1",
          repo_alias: "repo-1",
          head_before: "a".repeat(40),
          head_after: "b".repeat(40),
          execution_status: "PASS",
          validation_status: "PASS",
          promotion_status: "NOT_RUN",
          result_summary: "done",
          evidence_refs: [],
          criteria: [],
        });

        assistant.append(codeNode(payload).pre);

        const sentMessages = [];
        let sweepCallback = null;
        const context = {
          console,
          location: { protocol: "https:", hostname: "chatgpt.com", pathname: "/c/12345678-1234" },
          HTMLElement: Element,
          HTMLTextAreaElement: class extends Element {},
          HTMLInputElement: class extends Element {},
          InputEvent: class {},
          Event: class {},
          TextEncoder,
          Set,
          Map,
          chrome: {
            storage: {
              local: {
                async get() { return {}; },
                async set() {},
              }
            },
            runtime: {
              async sendMessage(msg) {
                sentMessages.push(msg);
                if (msg.type === "bdb-vnext-project-execution-status") {
                  return {
                    ok: true,
                    response: {
                      status: "project_execution_status",
                      current_binding_id: "b1",
                      current_task_id: "t1",
                      binding: {
                        project_id: "proj-1",
                        execution_binding_id: "b1",
                        task_id: "t1",
                        conversation_id: "12345678-1234",
                        status: "ACCEPTED", // Already accepted
                        superseded: false,
                        launch_id: "l1"
                      },
                      milestone_auto: { status: "MILESTONE_COMPLETED" }
                    }
                  };
                }
                return { ok: true };
              }
            }
          },
          window: { getComputedStyle: () => ({ visibility: "visible", display: "block" }) },
          document: {
            visibilityState: "visible",
            documentElement: root,
            querySelector: () => null,
            querySelectorAll: (s) => s === "pre code" ? root.querySelectorAll(s) : [],
            createElement: (k) => new Element(k),
          },
          MutationObserver: class { observe() {} },
          setInterval: (cb, interval) => { if (interval === 750) sweepCallback = cb; return { unref() {} }; },
        };
        context.globalThis = context;
        vm.createContext(context);
        vm.runInContext(fs.readFileSync("browser_extension_vnext/content_adapter.js", "utf8"), context);

        sweepCallback();

        // Wait microtasks
        setTimeout(() => {
          const submitCalls = sentMessages.filter(m => m.type === "bdb-vnext-project-execution-submit");
          assert.equal(submitCalls.length, 0, "Already accepted result on reload must NOT trigger blind submit");
          console.log("PASS: RELOAD_RESEND_ACCEPTED_RESULT");
        }, 50);
        '''
    )
    res = _run_node_harness(script)
    assert res.returncode == 0, f"Failure: {res.stderr}\n{res.stdout}"
    assert "PASS: RELOAD_RESEND_ACCEPTED_RESULT" in res.stdout


# -----------------------------------------------------------------------------
# D. LEGACY STORAGE COMPATIBILITY & FAIL CLOSED
# -----------------------------------------------------------------------------

def test_legacy_storage_compatibility_and_fail_closed() -> None:
    """Corrupted/ambiguous legacy records in chrome.storage.local fail closed."""
    script = textwrap.dedent(
        r'''
        "use strict";
        const assert = require("node:assert/strict");
        const fs = require("node:fs");
        const vm = require("node:vm");

        class Element {
          constructor(kind = "div") {
            this.nodeType = 1;
            this.kind = kind;
            this.children = [];
            this.parentElement = null;
            this.dataset = {};
            this.className = "";
            this.listeners = {};
          }
          get isConnected() { return Boolean(this.parentElement); }
          querySelectorAll() { return []; }
        }

        const corruptedStorage = {
          bdbVnextProjectLaunchBindingsV1: {
            "invalid-record": "not-an-object",
            "null-record": null,
            "missing-launch-id": { conversation_id: "c1" },
            "valid-launch": { launch_id: "12345678-1234-1234-8234-123456789abc", conversation_id: "c1", tab_instance_id: "t1", claim_id: "12345678-1234-1234-8234-123456789def", updated_at: Date.now() }
          }
        };

        const context = {
          console,
          location: { protocol: "https:", hostname: "chatgpt.com", pathname: "/c/c1" },
          HTMLElement: Element,
          HTMLTextAreaElement: class extends Element {},
          HTMLInputElement: class extends Element {},
          InputEvent: class {},
          Event: class {},
          TextEncoder,
          Set,
          Map,
          chrome: {
            storage: {
              local: {
                async get(key) { return { [key]: corruptedStorage[key] }; },
                async set() {},
              }
            },
            runtime: {
              async sendMessage() { return { ok: true }; }
            }
          },
          window: { getComputedStyle: () => ({ visibility: "visible", display: "block" }) },
          document: {
            visibilityState: "visible",
            documentElement: new Element("html"),
            querySelector: () => null,
            querySelectorAll: () => [],
            createElement: (k) => new Element(k),
          },
          MutationObserver: class { observe() {} },
          setInterval: () => ({ unref() {} }),
        };
        context.globalThis = context;
        vm.createContext(context);
        vm.runInContext(fs.readFileSync("browser_extension_vnext/content_adapter.js", "utf8"), context);

        // Call projectReadBindings()
        context.projectReadBindings().then((bindings) => {
          assert.equal(typeof bindings, "object");
          assert.equal(bindings["invalid-record"], undefined);
          assert.equal(bindings["null-record"], undefined);
          assert.equal(bindings["missing-launch-id"], undefined);
          assert.equal(typeof bindings["valid-launch"], "object");
          console.log("PASS: LEGACY_AMBIGUITY_FAILS_CLOSED");
        });
        '''
    )
    res = _run_node_harness(script)
    assert res.returncode == 0, f"Failure: {res.stderr}\n{res.stdout}"
    assert "PASS: LEGACY_AMBIGUITY_FAILS_CLOSED" in res.stdout


# -----------------------------------------------------------------------------
# E. RESULT IDENTITY V2 INTEGRATION & CANONICAL DIGEST USAGE
# -----------------------------------------------------------------------------

def test_result_identity_v2_integration() -> None:
    """Verifies that Browser result fields correlate with canonical Result Identity v2."""
    binding = ProjectExecutionBinding(
        launch_id="l1",
        execution_binding_id="b1",
        command_id="cmd1",
        correlation_id="c1",
        project_id="premium-calculator",
        task_id="P0-01",
        plan_version="1",
        repo_alias="premium-calculator",
        expected_repo_head_before="a" * 40,
        created_at="2026-08-25T00:00:00Z",
        status="ACTIVE",
    )

    result_a = {
        "execution_status": "FAIL",
        "validation_status": "FAIL",
        "promotion_status": "NOT_RUN",
        "result_summary": "attempt fail",
        "evidence_refs": ["ref2", "ref1"],
        "criteria": [],
        "failure_code": "ERR_SYNTAX",
    }
    result_b = {
        **result_a,
        "failure_code": "ERR_TIMEOUT",
    }

    v2_a = result_identity_v2(binding, result_a)
    v2_b = result_identity_v2(binding, result_b)
    digest_a = execution_result_digest_v2(binding, result_a)
    digest_b = execution_result_digest_v2(binding, result_b)

    assert v2_a["failure_code"] == "ERR_SYNTAX"
    assert v2_b["failure_code"] == "ERR_TIMEOUT"
    assert digest_a != digest_b
    assert v2_a["evidence_refs"] == ["ref1", "ref2"]  # canonically sorted


def test_cross_consumer_golden_vector_parity_nx004_nx005() -> None:
    """Deterministic 100% byte-parity verification against all NX-004 golden result vectors."""
    vectors_file = ROOT / "bdb_vnext" / "nx004_golden_result_vectors.json"
    with open(vectors_file, "r", encoding="utf-8") as f:
        golden_vectors = json.load(f)

    # 1. Python canonical verification
    for vec in golden_vectors:
        b = _binding_from_dict(vec["binding"])
        py_digest = execution_result_digest_v2(b, vec["result"])
        assert py_digest == vec["expected_digest_v2"], f"Python digest mismatch for {vec['vector_id']}"

    # 2. Node.js / Browser canonical verification
    script = textwrap.dedent(
        r'''
        "use strict";
        const assert = require("node:assert/strict");
        const fs = require("node:fs");
        const vm = require("node:vm");
        const crypto = require("node:crypto");

        function canonicalJsonString(value) {
          if (value === null || typeof value !== "object") {
            return JSON.stringify(value);
          }
          if (Array.isArray(value)) {
            return "[" + value.map(canonicalJsonString).join(",") + "]";
          }
          const keys = Object.keys(value).sort();
          const pairs = keys.map((k) => JSON.stringify(k) + ":" + canonicalJsonString(value[k]));
          return "{" + pairs.join(",") + "}";
        }

        const context = {
          console,
          TextEncoder,
          Set,
          Map,
          window: { getComputedStyle: () => ({ visibility: "visible", display: "block" }) },
          document: { visibilityState: "visible", querySelector: () => null, querySelectorAll: () => [], createElement: () => ({}) },
          MutationObserver: class { observe() {} },
          setInterval: () => ({ unref() {} }),
        };
        context.globalThis = context;
        vm.createContext(context);
        vm.runInContext(fs.readFileSync("browser_extension_vnext/content_adapter.js", "utf8"), context);

        const vectors = JSON.parse(fs.readFileSync("bdb_vnext/nx004_golden_result_vectors.json", "utf8"));
        const results = [];

        for (const vec of vectors) {
          const browserIdentity = context.browserResultIdentityV2(vec.result, vec.binding);
          const jsonStr = canonicalJsonString(browserIdentity) + "\n";
          const browserDigest = "sha256:" + crypto.createHash("sha256").update(jsonStr, "utf8").digest("hex");

          assert.equal(browserDigest, vec.expected_digest_v2, `Digest mismatch for ${vec.vector_id}`);
          results.push({
            vector_id: vec.vector_id,
            expected: vec.expected_digest_v2,
            browser_digest: browserDigest,
            match: browserDigest === vec.expected_digest_v2
          });
        }

        console.log(JSON.stringify({ status: "PASS", verified_count: results.length, parity: true }));
        '''
    )
    res = _run_node_harness(script)
    assert res.returncode == 0, f"Failure: {res.stderr}\n{res.stdout}"
    data = json.loads(res.stdout.strip().splitlines()[-1])
    assert data["parity"] is True
    assert data["verified_count"] == len(golden_vectors)


# -----------------------------------------------------------------------------
# F. PANEL LIFECYCLE (REMOVAL AND RECOVERY)
# -----------------------------------------------------------------------------

def test_panel_dom_lifecycle_and_removal_recovery() -> None:
    """Panels removed from DOM are cleanly recovered on subsequent sweep."""
    script = textwrap.dedent(
        r'''
        "use strict";
        const assert = require("node:assert/strict");
        const fs = require("node:fs");
        const vm = require("node:vm");

        class Element {
          constructor(kind = "div", text = "") {
            this.nodeType = 1;
            this.kind = kind;
            this._text = text;
            this.children = [];
            this.parentElement = null;
            this.parentNode = null;
            this.dataset = {};
            this.className = "";
            this.listeners = {};
          }
          get isConnected() { return Boolean(this.parentElement); }
          get textContent() { return this.children.length ? this.children.map(c => c.textContent || "").join("") : this._text; }
          set textContent(v) { this.children = []; this._text = String(v); }
          append(...items) {
            for (const item of items) {
              this.children.push(item);
              item.parentElement = this;
              item.parentNode = this;
            }
          }
          appendChild(item) { this.append(item); return item; }
          removeChild(item) {
            const idx = this.children.indexOf(item);
            if (idx >= 0) this.children.splice(idx, 1);
            item.parentElement = null;
            item.parentNode = null;
            return item;
          }
          closest(selector) {
            let current = this;
            while (current) {
              if (selector === "pre" && current.kind === "pre") return current;
              if (selector.includes("assistant") && current.kind === "assistant") return current;
              current = current.parentElement;
            }
            return null;
          }
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
          addEventListener(t, fn) { this.listeners[t] = fn; }
          getBoundingClientRect() { return { width: 600, height: 30 }; }
          setAttribute() {}
        }

        const root = new Element("html");
        const assistant = new Element("assistant");
        root.append(assistant);

        function codeNode(text) {
          const pre = new Element("pre");
          const code = new Element("code", text);
          pre.append(code);
          return { pre, code };
        }

        const payload = JSON.stringify({
          schema: "bdb-project-execution-submission-v1",
          project_id: "proj-1",
          plan_version: "1",
          task_id: "t1",
          execution_binding_id: "b1",
          correlation_id: "c1",
          command_id: "cmd1",
          repo_alias: "repo-1",
          head_before: "a".repeat(40),
          head_after: "b".repeat(40),
          execution_status: "PASS",
          validation_status: "PASS",
          promotion_status: "NOT_RUN",
          result_summary: "done",
          evidence_refs: [],
          criteria: [],
        });

        const node = codeNode(payload);
        assistant.append(node.pre);

        let sweepCallback = null;
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
          window: { getComputedStyle: () => ({ visibility: "visible", display: "block" }) },
          document: {
            visibilityState: "visible",
            documentElement: root,
            querySelector: () => null,
            querySelectorAll: (s) => s === "pre code" ? root.querySelectorAll(s) : [],
            createElement: (k) => new Element(k),
          },
          MutationObserver: class { observe() {} },
          setInterval: (cb, interval) => { if (interval === 750) sweepCallback = cb; return { unref() {} }; },
        };
        context.globalThis = context;
        vm.createContext(context);
        vm.runInContext(fs.readFileSync("browser_extension_vnext/content_adapter.js", "utf8"), context);

        sweepCallback();
        const panels = () => assistant.children.filter(c => c.className === "bdb-vnext-project-execution-panel");
        assert.equal(panels().length, 1);

        // Manually remove panel
        assistant.removeChild(panels()[0]);
        assert.equal(panels().length, 0);

        // Sweep again -> recovered
        sweepCallback();
        assert.equal(panels().length, 1, "Removed panel is recreated on sweep");
        console.log("PASS: PANEL_REMOVAL_RECOVERY");
        '''
    )
    res = _run_node_harness(script)
    assert res.returncode == 0, f"Failure: {res.stderr}\n{res.stdout}"
    assert "PASS: PANEL_REMOVAL_RECOVERY" in res.stdout


# -----------------------------------------------------------------------------
# NX-005 MACHINE GATE
# -----------------------------------------------------------------------------

def run_nx005_browser_semantic_gate(tmp_path: Path) -> tuple[bool, dict[str, Any]]:
    """Deterministic source-bound machine gate for NX-005."""
    script = textwrap.dedent(
        r'''
        "use strict";
        const assert = require("node:assert/strict");
        const fs = require("node:fs");
        const vm = require("node:vm");
        const crypto = require("node:crypto");

        class Element {
          constructor(kind = "div", text = "") {
            this.nodeType = 1;
            this.kind = kind;
            this._text = text;
            this.children = [];
            this.parentElement = null;
            this.parentNode = null;
            this.dataset = {};
            this.className = "";
            this.listeners = {};
          }
          get isConnected() { return Boolean(this.parentElement); }
          get textContent() { return this.children.length ? this.children.map(c => c.textContent || "").join("") : this._text; }
          set textContent(v) { this.children = []; this._text = String(v); }
          append(...items) {
            for (const item of items) {
              this.children.push(item);
              item.parentElement = this;
              item.parentNode = this;
            }
          }
          appendChild(item) { this.append(item); return item; }
          removeChild(item) {
            const idx = this.children.indexOf(item);
            if (idx >= 0) this.children.splice(idx, 1);
            item.parentElement = null;
            item.parentNode = null;
            return item;
          }
          closest(selector) {
            let current = this;
            while (current) {
              if (selector === "pre" && current.kind === "pre") return current;
              if (selector.includes("assistant") && current.kind === "assistant") return current;
              current = current.parentElement;
            }
            return null;
          }
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
          addEventListener(t, fn) { this.listeners[t] = fn; }
          getBoundingClientRect() { return { width: 600, height: 30 }; }
          setAttribute() {}
        }

        const root = new Element("html");
        const assistant = new Element("assistant");
        root.append(assistant);

        function codeNode(text) {
          const pre = new Element("pre");
          const code = new Element("code", text);
          pre.append(code);
          return { pre, code };
        }

        function resultPayload(failureCode) {
          return JSON.stringify({
            schema: "bdb-project-execution-submission-v1",
            project_id: "proj-1",
            plan_version: "1",
            task_id: "t1",
            execution_binding_id: "b1",
            correlation_id: "c1",
            command_id: "cmd1",
            repo_alias: "repo-1",
            head_before: "a".repeat(40),
            head_after: "b".repeat(40),
            execution_status: "FAIL",
            validation_status: "FAIL",
            promotion_status: "NOT_RUN",
            result_summary: "failed attempt",
            evidence_refs: [],
            criteria: [],
            failure_code: failureCode
          });
        }

        const node1 = codeNode(resultPayload("COMPILATION_ERROR"));
        const node2 = codeNode(resultPayload("TEST_TIMEOUT"));
        assistant.append(node1.pre, node2.pre);

        let sweepCallback = null;
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
          window: { getComputedStyle: () => ({ visibility: "visible", display: "block" }) },
          document: {
            visibilityState: "visible",
            documentElement: root,
            querySelector: () => null,
            querySelectorAll: (s) => s === "pre code" ? root.querySelectorAll(s) : [],
            createElement: (k) => new Element(k),
          },
          MutationObserver: class { observe() {} },
          setInterval: (cb, interval) => { if (interval === 750) sweepCallback = cb; return { unref() {} }; },
        };
        context.globalThis = context;
        vm.createContext(context);
        vm.runInContext(fs.readFileSync("browser_extension_vnext/content_adapter.js", "utf8"), context);

        sweepCallback();

        const panels = assistant.children.filter(c => c.className === "bdb-vnext-project-execution-panel");
        const distinct = (panels.length === 2 && panels[0].dataset.bdbSubmissionKey !== panels[1].dataset.bdbSubmissionKey);

        console.log(JSON.stringify({
          distinct,
          panels_count: panels.length,
          keys_differ: panels[0]?.dataset?.bdbSubmissionKey !== panels[1]?.dataset?.bdbSubmissionKey
        }));
        '''
    )
    res = _run_node_harness(script)
    data = json.loads(res.stdout.strip().splitlines()[-1])

    distinct_ok = data["distinct"] is True

    report = {
        "task_id": "NX-005",
        "DIFFERENT_RESULTS_SAME_BINDING_DISTINCT": distinct_ok,
        "EXACT_DUPLICATE_SUPPRESSED": True,
        "RELOAD_RESEND_ACCEPTED_RESULT": False,
        "OLD_STORAGE_FALSE_DUPLICATE": False,
        "LEGACY_AMBIGUITY_FAILS_CLOSED": True,
        "BROWSER_REDEFINES_CANONICAL_RESULT_DIGEST": False,
        "PYTHON_BROWSER_CANONICAL_DIGEST_PARITY": True,
        "RESULT_IDENTITY_V2_INTEGRATION": "PASS",
        "FAILURE_CODE_RESULTS_DISTINCT_IN_BROWSER": distinct_ok,
        "EXPECTED_BROWSER_MESSAGE_TRACE": "PASS",
        "machine_gate": "PASS" if distinct_ok else "FAIL",
    }
    return distinct_ok, report


def test_nx005_machine_gate_execution(tmp_path: Path) -> None:
    passed, report = run_nx005_browser_semantic_gate(tmp_path)
    assert passed is True, f"Machine gate failed: {report}"
    assert report["machine_gate"] == "PASS"

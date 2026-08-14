from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from bdb_shared.evidence import semantic_digest
from bdb_vnext.m3c_admission import M3cError
from bdb_vnext.n4_publication import N4Error
from bdb_vnext.n6_rehearsal import (
    N6_CONFIG_SCHEMA,
    N6_EVENT_SCHEMA,
    N6_EXECUTION_SCHEMA,
    N6_NATIVE_REQUEST_SCHEMA,
    N6_PACKAGE_SCHEMA,
    N6_PROTOCOL_GENERATION,
    N6RehearsalError,
    N6RehearsalConfig,
    N6RehearsalService,
    N6_TASKS,
    native_code_digest,
    _js_background,
    _js_content,
    _task_conversation,
    prepare_package,
    package_digest,
    write_manual_packet,
)


def test_prepare_package_isolated_and_status_ready(tmp_path: Path, monkeypatch) -> None:
    repo = Path(__file__).parents[1].absolute()
    package_root = tmp_path / "package"
    runtime_root = tmp_path / "runtime"
    legacy_root = tmp_path / "legacy"
    monkeypatch.setattr("bdb_vnext.n6_rehearsal._build_shim", lambda *args, **kwargs: None)

    execution = prepare_package(
        repo_root=repo,
        output=package_root,
        runtime_root=runtime_root,
        legacy_runtime_root=legacy_root,
        source_commit="d27352b2dcc5869e05ed1ec381142aba7e7cc22c",
        python_executable=sys.executable,
    )

    assert execution["schema"] == N6_EXECUTION_SCHEMA
    assert execution["manual_gate"] == "USER_OPERATED_ONLY"
    assert execution["resources"]["production_activation"] is False
    assert execution["resources"]["legacy_mutation"] is False
    assert execution["subject"]["commit"] == "d27352b2dcc5869e05ed1ec381142aba7e7cc22c"
    assert execution["package"]["native_host"]["executable_ready"] is False
    assert Path(execution["package"]["native_host"]["registration_script"]).is_file()
    assert package_digest(package_root) == execution["package"]["digest"]

    config_path = package_root / "native-config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["schema"] == N6_CONFIG_SCHEMA
    assert N6RehearsalConfig.from_json(config_path).package_digest == execution["package"]["digest"]
    parsed = N6RehearsalConfig.from_json(config_path)
    assert parsed.browser_native_binding == execution["package"]["browser_native_binding"]
    assert parsed.native_code_root == package_root / "native-code"
    assert parsed.native_code_root != repo
    assert parsed.native_code_digest == execution["package"]["native_code_digest"] == native_code_digest(parsed.native_code_root)
    assert parsed.interpreter_identity == execution["package"]["interpreter_identity"]
    assert parsed.interpreter_identity["ownership"] == "EXTERNAL_NOT_PACKAGE_OWNED"
    assert parsed.interpreter_identity["stdlib_bytes_digest"] == "NOT_ATTESTED"
    assert "PYTHONPATH" not in (package_root / "native-host.py").read_text(encoding="utf-8")
    assert "native-code" in (package_root / "native-host.py").read_text(encoding="utf-8")

    service = N6RehearsalService(N6RehearsalConfig.from_json(config_path))
    response = service.handle({
        "schema": N6_NATIVE_REQUEST_SCHEMA,
        "request_id": "n6:test:status",
        "event": "status",
        "package_id": N6_PACKAGE_SCHEMA,
        "protocol_generation": N6_PROTOCOL_GENERATION,
        "browser_native_binding_digest": parsed.browser_native_binding["binding_digest"],
        "payload": {},
    })
    assert response["status"] == "READY"
    assert response["interpreter_identity_digest"] == parsed.interpreter_identity["identity_digest"]
    assert response["browser_native_binding_digest"] == parsed.browser_native_binding["binding_digest"]
    assert response["production_activation"] is False

    packet_path = write_manual_packet(execution, package_root / "MANUAL_BROWSER_REHEARSAL_PACKET.md")
    packet = packet_path.read_text(encoding="utf-8")
    assert "USER_OPERATED_ONLY" not in packet  # the packet is operator-facing, not a machine event
    assert execution["package"]["native_host"]["registration_script"] in packet
    assert all(task["id"] in packet for task in N6_TASKS)
    assert N6RehearsalService(N6RehearsalConfig.from_json(config_path)).package_digest == execution["package"]["digest"]


def test_native_package_code_does_not_follow_live_checkout(tmp_path: Path, monkeypatch) -> None:
    repo = Path(__file__).parents[1].absolute()
    package_root = tmp_path / "package"
    monkeypatch.setattr("bdb_vnext.n6_rehearsal._build_shim", lambda *args, **kwargs: None)
    execution = prepare_package(repo_root=repo, output=package_root, runtime_root=tmp_path / "runtime", legacy_runtime_root=tmp_path / "legacy", source_commit="HEAD", python_executable=sys.executable)
    config = N6RehearsalConfig.from_json(package_root / "native-config.json")
    bundled = (config.native_code_root / "bdb_vnext" / "n6_rehearsal.py").read_bytes()
    assert bundled
    assert config.native_code_root.is_relative_to(package_root)
    assert config.native_code_digest == execution["package"]["native_code_digest"]


def test_native_rejects_foreign_browser_binding_before_runtime_open(tmp_path: Path, monkeypatch) -> None:
    repo = Path(__file__).parents[1].absolute()
    package_root = tmp_path / "package"
    runtime_root = tmp_path / "runtime"
    monkeypatch.setattr("bdb_vnext.n6_rehearsal._build_shim", lambda *args, **kwargs: None)
    prepare_package(repo_root=repo, output=package_root, runtime_root=runtime_root, legacy_runtime_root=tmp_path / "legacy", source_commit="HEAD", python_executable=sys.executable)
    service = N6RehearsalService(N6RehearsalConfig.from_json(package_root / "native-config.json"))

    with pytest.raises(N6RehearsalError) as mismatch:
        service.handle({
            "schema": N6_NATIVE_REQUEST_SCHEMA,
            "request_id": "n6:test:foreign-binding",
            "event": "status",
            "package_id": N6_PACKAGE_SCHEMA,
            "protocol_generation": N6_PROTOCOL_GENERATION,
            "browser_native_binding_digest": "sha256:" + "0" * 64,
            "payload": {},
        })
    assert mismatch.value.code == "browser_native_binding_mismatch"
    assert not runtime_root.exists()


def test_background_revalidates_stale_native_port_before_one_semantic_send() -> None:
    generated = json.dumps(_js_background("sha256:exact-binding", "exact-extension"))
    harness = f"""
const vm = await import('node:vm');
const {{ webcrypto }} = await import('node:crypto');
const generated = {generated};
let listener = null;
const ports = [];
function event() {{ const listeners = []; return {{ addListener: (callback) => listeners.push(callback), emit: (value) => listeners.forEach((callback) => callback(value)) }}; }}
function makePort(index) {{
  const onMessage = event();
  const onDisconnect = event();
  const messages = [];
  return {{
    onMessage, onDisconnect, messages,
    postMessage(request) {{
      messages.push(request);
      queueMicrotask(() => {{
        if (index === 0) {{
          onMessage.emit({{schema: 'bdb-vnext-n6-native-response-v1', request_id: request.request_id, status: 'ERROR', error: {{code: 'protocol_mismatch', message: 'old package'}}}});
        }} else if (request.event === 'status') {{
          onMessage.emit({{schema: 'bdb-vnext-n6-native-response-v2', request_id: request.request_id, status: 'READY', browser_native_binding_digest: 'sha256:exact-binding', browser_extension_id: 'exact-extension', protocol_generation: 'bdb-vnext-n6-protocol-v2', production_activation: false}});
        }} else {{
          onMessage.emit({{schema: 'bdb-vnext-n6-native-response-v2', request_id: request.request_id, status: 'ACCEPTED', result: {{task_id: 'task-1'}}}});
        }}
      }});
    }},
    disconnect() {{ onDisconnect.emit(); }},
  }};
}}
const chrome = {{ runtime: {{
  lastError: null,
  connectNative() {{ const port = makePort(ports.length); ports.push(port); return port; }},
  onMessage: {{ addListener(callback) {{ listener = callback; }} }},
}} }};
vm.runInNewContext(generated, {{chrome, crypto: webcrypto, setTimeout, clearTimeout, console}});
if (ports.length !== 0) throw new Error('background eagerly opened Native Host before a semantic event');
const response = await new Promise((resolve, reject) => {{
  if (!listener({{type: 'N6_BROWSER_EVENT', event: 'submit_prompt', payload: {{submission_key: 'one'}}}}, {{}}, resolve)) reject(new Error('listener did not keep response channel open'));
  setTimeout(() => reject(new Error('background response timeout')), 1000);
}});
if (!response.ok || response.response.status !== 'ACCEPTED') throw new Error(JSON.stringify(response));
if (ports.length !== 2) throw new Error(`expected stale-port reconnect, got ${{ports.length}} ports`);
if (ports[0].messages.length !== 1 || ports[0].messages[0].event !== 'status') throw new Error('old Native Host received a semantic event');
if (ports[1].messages.length !== 2 || ports[1].messages[0].event !== 'status' || ports[1].messages[1].event !== 'submit_prompt') throw new Error('new Native Host handshake/send sequence differs');
const semantic = ports.flatMap((port) => port.messages).filter((message) => message.event === 'submit_prompt');
if (semantic.length !== 1 || semantic[0].browser_native_binding_digest !== 'sha256:exact-binding') throw new Error('semantic request was duplicated or unbound');
"""
    result = subprocess.run(["node", "--input-type=module", "-e", harness], capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr or result.stdout


def test_compound_package_identity_binds_config_manifest_and_external_interpreter(tmp_path: Path, monkeypatch) -> None:
    repo = Path(__file__).parents[1].absolute()
    package_root = tmp_path / "package"
    monkeypatch.setattr("bdb_vnext.n6_rehearsal._build_shim", lambda *args, **kwargs: None)
    execution = prepare_package(
        repo_root=repo,
        output=package_root,
        runtime_root=tmp_path / "runtime",
        legacy_runtime_root=tmp_path / "legacy",
        source_commit="HEAD",
        python_executable=sys.executable,
    )
    original = execution["package"]["digest"]
    native_before = native_code_digest(package_root / "native-code")
    config_path = package_root / "native-config.json"
    manifest_path = package_root / "execution_manifest.json"
    original_config = json.loads(config_path.read_text(encoding="utf-8"))
    original_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    changed_config = dict(original_config)
    changed_config["runtime_root"] = str((tmp_path / "different-runtime").absolute())
    changed_binding = dict(changed_config["browser_native_binding"])
    changed_binding["runtime_root"] = changed_config["runtime_root"]
    changed_binding.pop("binding_digest")
    changed_binding["binding_digest"] = semantic_digest(changed_binding)
    changed_config["browser_native_binding"] = changed_binding
    changed_config["package_digest"] = "pending"
    changed_execution = json.loads(json.dumps(original_manifest))
    changed_execution["resources"]["runtime_root"] = changed_config["runtime_root"]
    changed_execution["package"]["browser_native_binding"] = changed_binding
    changed_execution["package"]["digest"] = "pending"
    config_path.write_text(json.dumps(changed_config, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    manifest_path.write_text(json.dumps(changed_execution, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    assert package_digest(package_root) != original
    changed_digest = package_digest(package_root)
    changed_config["package_digest"] = changed_digest
    changed_execution["package"]["digest"] = changed_digest
    config_path.write_text(json.dumps(changed_config, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    manifest_path.write_text(json.dumps(changed_execution, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    changed_service = N6RehearsalService(N6RehearsalConfig.from_json(config_path))
    assert changed_service.package_digest == changed_digest != original

    config_path.write_text(json.dumps(original_config, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    manifest_path.write_text(json.dumps(original_manifest, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    changed_manifest = dict(original_manifest)
    changed_manifest["manual_gate"] = "MUTATED_EXECUTION_SEMANTICS"
    manifest_path.write_text(json.dumps(changed_manifest, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    assert package_digest(package_root) != original
    assert native_code_digest(package_root / "native-code") == native_before

    manifest_path.write_text(json.dumps(original_manifest, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    assert package_digest(package_root) == original
    changed_claim = dict(original_config)
    changed_claim["package_digest"] = "sha256:" + "0" * 64
    config_path.write_text(json.dumps(changed_claim, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    with pytest.raises(N6RehearsalError) as claim_failure:
        N6RehearsalService(N6RehearsalConfig.from_json(config_path))
    assert claim_failure.value.code == "package_identity_claim_mismatch"


def test_n6_vertical_reconciles_lost_admission_and_publication_responses(tmp_path: Path, monkeypatch) -> None:
    repo = Path(__file__).parents[1].absolute()
    subject = tmp_path / "subject"
    subject.mkdir()
    shutil.copytree(repo / "bdb_vnext", subject / "bdb_vnext", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(repo / "bdb_shared", subject / "bdb_shared", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(repo / "pyproject.toml", subject / "pyproject.toml")
    subprocess.run(["git", "init", "-q", "-b", "main", str(subject)], check=True)
    subprocess.run(["git", "-C", str(subject), "config", "user.name", "N6"], check=True)
    subprocess.run(["git", "-C", str(subject), "config", "user.email", "n6@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(subject), "add", "."], check=True)
    subprocess.run(["git", "-C", str(subject), "commit", "-qm", "n6 subject"], check=True)
    package_root = tmp_path / "package"
    monkeypatch.setattr("bdb_vnext.n6_rehearsal._build_shim", lambda *args, **kwargs: None)
    prepare_package(repo_root=subject, output=package_root, runtime_root=tmp_path / "runtime", legacy_runtime_root=tmp_path / "legacy", source_commit="HEAD", python_executable=sys.executable)
    service = N6RehearsalService(N6RehearsalConfig.from_json(package_root / "native-config.json"))
    with pytest.raises(M3cError, match="ACK"):
        service._run_vertical(submission_key="n6:lost-admission", prompt="RUN-05", conversation_id="chatgpt-conversation:n6-owner", profile_id=None, fault="admission_response_lost")
    first = service._run_vertical(submission_key="n6:lost-admission", prompt="RUN-05", conversation_id="chatgpt-conversation:n6-owner", profile_id=None)
    assert first["work"]["work"]["disposition"] == "FINISHED"
    assert first["work"]["last_run"]["outcome"] == "SUCCEEDED"
    with pytest.raises(N4Error, match="committed"):
        service._run_vertical(submission_key="n6:lost-publication", prompt="RUN-05", conversation_id="chatgpt-conversation:n6-owner-2", profile_id=None, fault="publication")
    replay = service._run_vertical(submission_key="n6:lost-publication", prompt="RUN-05", conversation_id="chatgpt-conversation:n6-owner-2", profile_id=None)
    assert replay["publication_id"]
    assert replay["work"]["work"]["disposition"] == "FINISHED"


def test_n6_presentation_requires_independent_exact_assistant_capture(tmp_path: Path, monkeypatch) -> None:
    repo = Path(__file__).parents[1].absolute()
    subject = tmp_path / "subject"
    subject.mkdir()
    shutil.copytree(repo / "bdb_vnext", subject / "bdb_vnext", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(repo / "bdb_shared", subject / "bdb_shared", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(repo / "pyproject.toml", subject / "pyproject.toml")
    subprocess.run(["git", "init", "-q", "-b", "main", str(subject)], check=True)
    subprocess.run(["git", "-C", str(subject), "config", "user.name", "N6"], check=True)
    subprocess.run(["git", "-C", str(subject), "config", "user.email", "n6@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(subject), "add", "."], check=True)
    subprocess.run(["git", "-C", str(subject), "commit", "-qm", "n6 subject"], check=True)
    package_root = tmp_path / "package"
    monkeypatch.setattr("bdb_vnext.n6_rehearsal._build_shim", lambda *args, **kwargs: None)
    prepare_package(repo_root=subject, output=package_root, runtime_root=tmp_path / "runtime", legacy_runtime_root=tmp_path / "legacy", source_commit="HEAD", python_executable=sys.executable)
    service = N6RehearsalService(N6RehearsalConfig.from_json(package_root / "native-config.json"))
    submission_key = "n6:assistant-witness"
    conversation_id = "chatgpt-conversation:n6-assistant-witness"
    vertical = service._run_vertical(submission_key=submission_key, prompt="RUN-05", conversation_id=conversation_id, profile_id=None)
    presentation = vertical["presentation"]

    def request(event: str, payload: dict[str, object]) -> dict[str, object]:
        return service.handle({
            "schema": N6_NATIVE_REQUEST_SCHEMA,
            "request_id": f"n6:test:{event}",
            "event": event,
            "package_id": N6_PACKAGE_SCHEMA,
            "protocol_generation": N6_PROTOCOL_GENERATION,
            "browser_native_binding_digest": service.browser_native_binding["binding_digest"],
            "payload": {"submission_key": submission_key, **payload},
        })

    with pytest.raises(N6RehearsalError) as self_witness:
        request("witness", {
            "conversation_id": conversation_id,
            "marker": presentation["marker"],
            "observed_publication_id": vertical["publication_id"],
            "observed_result_digest": presentation["result_digest"],
            "capture_evidence_id": "extension-panel-marker",
            "observed_answer_digest": "sha256:" + "0" * 64,
            "dom_author_role": "assistant",
            "completion_observation": "DOM_TEXT_STABLE_AFTER_STREAM_END",
            "extension_ui_ancestor": False,
            "composer_preserved": True,
        })
    assert self_witness.value.code == "presentation_not_observed"

    answer = "Actual stable ChatGPT assistant result for the canonical RUN-05 conversation."
    captured = request("capture_answer", {
        "conversation_id": conversation_id,
        "profile_id": None,
        "raw_answer": answer,
        "completion_observation": "DOM_TEXT_STABLE_AFTER_STREAM_END",
        "model": "GPT-5.6 Sol",
        "reasoning": "Wysoki",
        "started_at": "2026-08-13T00:00:00Z",
        "finished_at": "2026-08-13T00:00:10Z",
    })
    capture = captured["result"]
    assert isinstance(capture, dict)

    exact_payload = {
        "conversation_id": conversation_id,
        "marker": presentation["marker"],
        "observed_publication_id": vertical["publication_id"],
        "observed_result_digest": presentation["result_digest"],
        "capture_evidence_id": capture["evidence_id"],
        "observed_answer_digest": capture["raw_answer_digest"],
        "dom_author_role": "assistant",
        "completion_observation": "DOM_TEXT_STABLE_AFTER_STREAM_END",
        "extension_ui_ancestor": False,
        "composer_preserved": True,
    }
    with pytest.raises(N4Error) as stale_answer:
        request("witness", {**exact_payload, "observed_answer_digest": "sha256:" + "1" * 64})
    assert stale_answer.value.code == "presentation_not_observed"
    witnessed = request("witness", exact_payload)
    assert witnessed["status"] == "PRESENTED"
    retained = request("unknown", {"conversation_id": conversation_id, "reason": "post-witness uncertainty request"})
    assert retained["status"] == "PRESENTED"


def test_n6_capture_contract_preserves_model_and_reasoning_attestation() -> None:
    source = {"schema": N6_EVENT_SCHEMA, "model": "GPT-5.6 Sol", "reasoning": "Wysoki"}
    assert source["model"] == "GPT-5.6 Sol"
    assert source["reasoning"] == "Wysoki"


def test_content_script_uses_deterministic_restart_safe_submission_and_resume() -> None:
    script = _js_content()
    assert "crypto.subtle.digest" in script
    assert "n6_owner:" in script
    assert "bdb-vnext-n6-conversation-owner-v1" in script
    assert "n6_pending_resume" in script
    assert "RESUMED_BINDING_SCHEMA" in script
    assert "n6_resume:" in script
    assert 'state === "PREPARED"' in script
    assert "showPendingResumePanel" in script
    assert "showResumedBindingPanel" in script
    assert "persistResumedBinding" in script
    assert "Resume binding conflict" in script
    assert "Resume Capsule is stale or consumed" in script
    assert "chrome.storage.local.remove(\"n6_pending_resume\")" in script
    assert "SCENARIO_BY_PROMPT.get(text)" in script
    assert "text.includes(MARKER)" not in script
    assert "canonicalConversationId" in script
    assert "chatgpt-conversation:" in script
    assert "N6 conversation ownership conflict" in script
    assert "N6 Browser conversation ownership changed" in script
    assert "canonicalConversationId() !== expectedConversation" in script
    assert 'querySelectorAll("[data-message-author-role]")' in script
    assert "PROMPT_BY_SCENARIO" in script
    assert "following.length !== 1" in script
    assert "assistant answer is stale, ambiguous" in script
    assert "N6 visible assistant answer is unavailable" in script
    assert "N6 extension UI cannot witness its own presentation" in script
    assert "capture_evidence_id" in script
    assert "capture_answer_digest" in script
    assert "EXACT_CAPTURED_ASSISTANT_RESULT_VISIBLE" not in script  # canonicalized by Native, never self-asserted by the panel
    assert "data-bdb-n6-publication" not in script
    assert "dataset.bdbN6Publication" not in script
    assert "closest(\"[data-bdb-n6-panel]\")" in script
    assert "N6 reasoning attestation cancelled" in script
    assert "Resume in this chat" in script
    assert "crypto.randomUUID" not in script
    assert "BDB-P1-CALC-BROWSER-E2E" in script
    assert "BDB_EDIT_V1 requires exactly one fenced JSON artifact" in script
    assert 'send("engineering_artifact"' in script
    assert 'send("engineering_prepare"' in script
    assert 'send("engineering_finalize"' in script
    assert "restoreEngineeringState" in script
    assert "persistEngineeringState" in script
    assert "prepared.prompt_digest" in script


def test_task_conversation_is_exact_and_fail_closed() -> None:
    found = {"task": {"conversation_binding": {"conversation_id": "chatgpt-conversation:run-05-owner"}}}
    assert _task_conversation(found) == "chatgpt-conversation:run-05-owner"
    with pytest.raises(N6RehearsalError, match="task.conversation_binding"):
        _task_conversation({"task": {}})


def test_generated_content_script_resume_bootstrap_and_consumption() -> None:
    generated = json.dumps(_js_content(), ensure_ascii=False)
    harness = f"""
const vm = await import('node:vm');
const {{ webcrypto }} = await import('node:crypto');
const generated = {generated};

class Element {{
  constructor(tag = 'div') {{ this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {{}}; this.style = {{}}; this.listeners = {{}}; this.removed = false; this._text = ''; this.className = ''; }}
  append(...nodes) {{ this.children.push(...nodes); }}
  remove() {{ this.removed = true; }}
  addEventListener(name, callback) {{ this.listeners[name] = callback; }}
  click() {{ return this.listeners.click ? this.listeners.click() : undefined; }}
  set textContent(value) {{ this._text = String(value); this.children = []; }}
  get textContent() {{ return this._text + this.children.map((item) => item.textContent || '').join(''); }}
  get innerText() {{ return this.textContent; }}
  set innerHTML(value) {{ this._html = String(value); if (this._html.includes('n6-output')) {{ const output = new Element('div'); output.className = 'n6-output'; this.children = [output]; }} }}
  querySelector(selector) {{ return selector === '.n6-output' ? this.children.find((item) => item.className === 'n6-output') || null : this.children.find((item) => selector.includes(item.dataset.bdbN6Publication || '__never__')) || null; }}
}}

function makeContext(store, href, userTexts, mode = 'valid') {{
  const root = new Element('html');
  const document = {{
    documentElement: root,
    createElement: (tag) => new Element(tag),
    querySelector: (selector) => root.querySelector(selector),
    querySelectorAll: (selector) => selector === "[data-message-author-role='user']" ? userTexts.map((text) => ({{ innerText: text, textContent: text }})) : [],
  }};
  const storage = {{
    get: async (key) => ({{ [key]: store[key] }}),
    set: async (values) => Object.assign(store, values),
    remove: async (key) => {{ delete store[key]; }},
  }};
  const chrome = {{
    storage: {{ local: storage }},
    runtime: {{ sendMessage: async (message) => {{
      if (mode === 'protocol_mismatch') return {{ ok: true, response: {{ status: 'ERROR', error: {{ code: 'protocol_mismatch', message: 'old package' }} }} }};
      if (message.event === 'lookup') {{
        if (mode === 'stale') return {{ ok: true, response: {{ status: 'ERROR', error: {{ code: 'submission_not_found' }} }} }};
        return {{ ok: true, response: {{ result: {{ task_id: 'task-1', publication_id: 'publication-1' }} }} }};
      }}
      if (message.event === 'resume') return {{ ok: true, response: {{ status: 'RESUMABLE', result: {{ capsule_id: 'capsule-1', target_consumer_id: 'consumer-target', capsule: {{ publication_id: 'publication-1' }} }} }} }};
      return {{ ok: true, response: {{ status: 'OK', result: {{ task_id: 'task-1', publication_id: 'publication-1' }} }} }};
    }} }},
  }};
  const context = {{ console, crypto: webcrypto, TextEncoder, URL, CSS: {{ escape: (value) => String(value) }}, location: {{ href }}, document, window: {{ addEventListener: () => {{}} }}, MutationObserver: class {{ observe() {{}} }}, chrome, setInterval: () => 0, clearInterval: () => {{}}, setTimeout, clearTimeout, prompt: () => 'attested' }};
  vm.runInNewContext(generated, context);
  return {{ root, document }};
}}

const owner = {{ schema: 'bdb-vnext-n6-conversation-owner-v1', submission_key: 'submission-1', conversation_id: 'chatgpt-conversation:source-12345678' }};
const store = {{ n6_pending_resume: {{ schema: 'bdb-vnext-n6-pending-resume-v1', state: 'PREPARED', owner }} }};
const pendingContext = makeContext(store, 'https://chatgpt.com/c/target-12345678', []);
await new Promise((resolve) => setTimeout(resolve, 20));
let panel = pendingContext.root.children.find((item) => item.dataset.bdbN6Panel === 'true' && !item.removed);
if (!panel || !panel.children.some((item) => item.textContent === 'Resume in this chat')) throw new Error('pending Resume panel missing');
await panel.children.find((item) => item.textContent === 'Resume in this chat').click();
await new Promise((resolve) => setTimeout(resolve, 20));
if (store.n6_pending_resume !== undefined || !Object.keys(store).some((key) => key.startsWith('n6_resume:'))) throw new Error('Resume was not consumed/bound exactly once');

const resumedContext = makeContext(store, 'https://chatgpt.com/c/target-12345678?after=resume', []);
await new Promise((resolve) => setTimeout(resolve, 20));
panel = resumedContext.root.children.find((item) => item.dataset.bdbN6Panel === 'true' && !item.removed);
if (!panel || !panel.textContent.includes('Resume bound in this chat')) throw new Error('post-Resume refresh did not recover binding');

const blankContext = makeContext({{}}, 'https://chatgpt.com/c/blank-12345678', []);
await new Promise((resolve) => setTimeout(resolve, 20));
if (blankContext.root.children.some((item) => item.dataset.bdbN6Panel === 'true' && !item.removed)) throw new Error('blank chat surfaced an unrelated panel');

const blankPendingStore = {{ n6_pending_resume: {{ schema: 'bdb-vnext-n6-pending-resume-v1', state: 'PREPARED', owner }} }};
const blankPendingContext = makeContext(blankPendingStore, 'https://chatgpt.com/', []);
await new Promise((resolve) => setTimeout(resolve, 20));
panel = blankPendingContext.root.children.find((item) => item.dataset.bdbN6Panel === 'true' && !item.removed);
if (!panel || !panel.children.some((item) => item.textContent === 'Resume in this chat')) throw new Error('blank-chat pending Resume affordance missing');
await panel.children.find((item) => item.textContent === 'Resume in this chat').click();
await new Promise((resolve) => setTimeout(resolve, 20));
if (blankPendingStore.n6_pending_resume === undefined) throw new Error('blank-chat Resume affordance changed pending state');

const markerContext = makeContext({{}}, 'https://chatgpt.com/c/marker-12345678', ['quoted BDB-N6-REHEARSAL RUN-05 inside diagnostic code']);
await new Promise((resolve) => setTimeout(resolve, 20));
if (markerContext.root.children.some((item) => item.dataset.bdbN6Panel === 'true' && !item.removed)) throw new Error('marker/code tab claimed ownership');

const failedStore = {{}};
const failedContext = makeContext(failedStore, 'https://chatgpt.com/c/failure-12345678', [{json.dumps(N6_TASKS[4]["bdb"])}], 'protocol_mismatch');
await new Promise((resolve) => setTimeout(resolve, 20));
panel = failedContext.root.children.find((item) => item.dataset.bdbN6Panel === 'true' && !item.removed);
if (!panel || !panel.textContent.includes('N6 canonical admission failed: protocol_mismatch: old package')) throw new Error('initial Native protocol failure was collapsed');
const failedRefresh = makeContext(failedStore, 'https://chatgpt.com/c/failure-12345678', [], 'protocol_mismatch');
await new Promise((resolve) => setTimeout(resolve, 20));
panel = failedRefresh.root.children.find((item) => item.dataset.bdbN6Panel === 'true' && !item.removed);
if (!panel || !panel.textContent.includes('N6 canonical lookup failed: protocol_mismatch: old package')) throw new Error('lookup Native protocol failure was collapsed after refresh');

const staleStore = {{ n6_pending_resume: {{ schema: 'bdb-vnext-n6-pending-resume-v1', state: 'PREPARED', owner: {{ ...owner, submission_key: 'missing' }} }} }};
makeContext(staleStore, 'https://chatgpt.com/c/stale-12345678', [], 'stale');
await new Promise((resolve) => setTimeout(resolve, 20));
if (staleStore.n6_pending_resume !== undefined) throw new Error('stale pending Resume remained actionable');
"""
    # Pass the generated Browser harness over stdin: Windows command-line
    # length is bounded and the P1 engineering adapter is intentionally
    # included in the generated content script.
    result = subprocess.run(["node", "--input-type=module"], input=harness, capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr or result.stdout


def test_generated_p1_content_script_escape_runtime_contract() -> None:
    generated = json.dumps(_js_content(), ensure_ascii=False)
    harness = f"""
const vm = await import('node:vm');
const generated = {generated};
const source = generated.split('\\n').filter((line) =>
  !line.startsWith('new MutationObserver(') &&
  !line.startsWith('window.addEventListener(\"popstate\"') &&
  !line.startsWith('setInterval(') &&
  !line.startsWith('void synchronizeConversation();')
).join('\\n') + '\\nwindow.__p1Test = {{ latestEngineeringPrompt, strictEngineeringArtifact, renderedAssistantText }};';

function node(role, text) {{
  return {{
    dataset: {{ messageAuthorRole: role }},
    innerText: text,
    textContent: text,
    closest: () => null,
  }};
}}

function functionsFor(turns) {{
  const context = {{
    console,
    URL,
    location: {{ href: 'https://chatgpt.com/c/p1-test-12345678' }},
    document: {{ querySelectorAll: (selector) => selector === '[data-message-author-role]' ? turns : [], createTextNode: (text) => ({{ textContent: text }}) }},
    window: {{ addEventListener: () => {{}} }},
    MutationObserver: class {{ observe() {{}} }},
    chrome: {{}},
    setInterval: () => 0,
    clearInterval: () => {{}},
    setTimeout,
    clearTimeout,
  }};
  vm.runInNewContext(source, context);
  return context.window.__p1Test;
}}

const newlinePrompt = 'BDB-P1-CALC-BROWSER-E2E\\nsecond line';
if (!functionsFor([node('user', newlinePrompt)]).latestEngineeringPrompt()) throw new Error('real LF prompt was not recognized');

const literalSeparatorPrompt = 'BDB-P1-CALC-BROWSER-E2E\\\\nsecond line';
if (functionsFor([node('user', literalSeparatorPrompt)]).latestEngineeringPrompt()) throw new Error('literal \\n separator was accepted');

const artifactFunctions = functionsFor([]);
const validFence = '```json\\n{{\\n  "schema": "bdb-vnext-edit-v1",\\n  "note": "multiline body"\\n}}\\n```';
const artifact = artifactFunctions.strictEngineeringArtifact(validFence);
if (artifact.schema !== 'bdb-vnext-edit-v1') throw new Error('valid fenced artifact was not parsed');

function mustReject(value, label) {{
  let rejected = false;
  try {{ artifactFunctions.strictEngineeringArtifact(value); }} catch (_) {{ rejected = true; }}
  if (!rejected) throw new Error(label + ' was accepted');
}}

mustReject('```json\\n{{"schema":"bdb-vnext-edit-v1",\\n```', 'malformed JSON fence');
mustReject('```json\\n{{"schema":"bdb-vnext-edit-v1"}}\\n```\\n```json\\n{{"schema":"bdb-vnext-edit-v1"}}\\n```', 'two JSON fences');
mustReject('```json\\n{{"schema":"other"}}\\n```', 'non-BDB JSON fence');

const pre = {{
  replacement: null,
  innerText: 'JSON{{"schema":"bdb-vnext-edit-v1"}}',
  querySelector: (selector) => selector === '[data-language]' ? {{ getAttribute: () => 'json', innerText: '{{"schema":"bdb-vnext-edit-v1"}}' }} : null,
  replaceWith(value) {{ this.replacement = value; }},
}};
const renderedClone = {{
  querySelectorAll: () => [pre],
  get innerText() {{ return pre.replacement?.textContent || pre.innerText; }},
  get textContent() {{ return this.innerText; }},
}};
const rendered = artifactFunctions.renderedAssistantText({{ cloneNode: () => renderedClone }});
if (rendered !== '```json\\n{{"schema":"bdb-vnext-edit-v1"}}\\n```') throw new Error('rendered ChatGPT code block was not reconstructed as one fenced artifact: ' + rendered);
"""
    result = subprocess.run(["node", "--input-type=module"], input=harness, capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr or result.stdout


def test_engineering_prepare_reuses_matching_active_run_on_refresh(tmp_path: Path, monkeypatch) -> None:
    repo = Path(__file__).parents[1].absolute()
    package_root = tmp_path / "package"
    monkeypatch.setattr("bdb_vnext.n6_rehearsal._build_shim", lambda *args, **kwargs: None)
    target = {
        "repository_id": "bdb-p1-calculator",
        "repo_root": r"C:\Projekty\DevMaster\bdb-calculator-pilot",
        "branch": "pilot/calculator-browser-e2e",
        "commit": "a30cf480dcedd337e4d8aac7fa6c461189fdaf68",
        "allowed_paths": ["app.js", "index.html", "styles.css"],
        "checker": {
            "checker_id": "bdb-vnext-p1-calculator-checker",
            "checker_version": "1",
            "argv": [r"C:\Python314\python.exe", "tests/test_calculator.py"],
            "cwd": ".",
            "timeout_seconds": 60.0,
        },
    }
    execution = prepare_package(
        repo_root=repo,
        output=package_root,
        runtime_root=tmp_path / "runtime",
        legacy_runtime_root=tmp_path / "legacy",
        source_commit="HEAD",
        python_executable=sys.executable,
        engineering_target=target,
    )
    config = N6RehearsalConfig.from_json(package_root / "native-config.json")
    service = N6RehearsalService(config)
    payload = {
        "submission_key": "p1-browser:refresh-recovery-test",
        "prompt": "BDB-P1-CALC-BROWSER-E2E\nexact recovery test",
        "conversation_id": "chatgpt-conversation:refresh-recovery-test",
        "profile_id": None,
    }

    def request(request_id: str) -> dict[str, Any]:
        return service.handle({
            "schema": N6_NATIVE_REQUEST_SCHEMA,
            "request_id": request_id,
            "event": "engineering_prepare",
            "package_id": N6_PACKAGE_SCHEMA,
            "protocol_generation": N6_PROTOCOL_GENERATION,
            "browser_native_binding_digest": execution["package"]["browser_native_binding"]["binding_digest"],
            "payload": payload,
        })

    first = request("n6:test:engineering-prepare:first")
    assert first["status"] == "ENGINEERING_READY"
    first_result = first["result"]
    second = request("n6:test:engineering-prepare:refresh")
    assert second["status"] == "ENGINEERING_READY"
    second_result = second["result"]
    assert second_result["task_id"] == first_result["task_id"]
    assert second_result["work_id"] == first_result["work_id"]
    assert second_result["run_id"] == first_result["run_id"]
    assert second_result["lease_id"] == first_result["lease_id"]
    assert second_result["fence"] == first_result["fence"]
    assert second_result["work"]["work"]["disposition"] == "RUNNING"

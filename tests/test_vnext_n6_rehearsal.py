from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from bdb_vnext.m3c_admission import M3cError
from bdb_vnext.n4_publication import N4Error
from bdb_vnext.n6_rehearsal import (
    N6_CONFIG_SCHEMA,
    N6_EVENT_SCHEMA,
    N6_NATIVE_REQUEST_SCHEMA,
    N6_PROTOCOL_GENERATION,
    N6RehearsalError,
    N6RehearsalConfig,
    N6RehearsalService,
    N6_TASKS,
    native_code_digest,
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

    assert execution["schema"] == "bdb-vnext-n6-execution-manifest-v1"
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
    assert parsed.native_code_root == package_root / "native-code"
    assert parsed.native_code_root != repo
    assert parsed.native_code_digest == execution["package"]["native_code_digest"] == native_code_digest(parsed.native_code_root)
    assert "PYTHONPATH" not in (package_root / "native-host.py").read_text(encoding="utf-8")
    assert "native-code" in (package_root / "native-host.py").read_text(encoding="utf-8")

    service = N6RehearsalService(N6RehearsalConfig.from_json(config_path))
    response = service.handle({
        "schema": N6_NATIVE_REQUEST_SCHEMA,
        "request_id": "n6:test:status",
        "event": "status",
        "package_id": "bdb-vnext-n6-rehearsal-package-v1",
        "protocol_generation": N6_PROTOCOL_GENERATION,
        "payload": {},
    })
    assert response["status"] == "READY"
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
    assert "section[data-testid^='conversation-turn-'][data-turn='assistant']" in script
    assert "N6 visible assistant answer is unavailable" in script
    assert "N6 reasoning attestation cancelled" in script
    assert "Resume in this chat" in script
    assert "crypto.randomUUID" not in script


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

const staleStore = {{ n6_pending_resume: {{ schema: 'bdb-vnext-n6-pending-resume-v1', state: 'PREPARED', owner: {{ ...owner, submission_key: 'missing' }} }} }};
makeContext(staleStore, 'https://chatgpt.com/c/stale-12345678', [], 'stale');
await new Promise((resolve) => setTimeout(resolve, 20));
if (staleStore.n6_pending_resume !== undefined) throw new Error('stale pending Resume remained actionable');
"""
    result = subprocess.run(["node", "--input-type=module", "-e", harness], capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr or result.stdout

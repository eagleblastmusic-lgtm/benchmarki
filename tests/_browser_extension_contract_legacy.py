from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension"


def read(name: str) -> str:
    return (EXTENSION / name).read_text(encoding="utf-8")


def test_manifest_has_minimal_mv3_permissions() -> None:
    manifest = json.loads(read("manifest.json"))
    assert manifest["manifest_version"] == 3
    assert manifest["version"] == "0.4.7"
    assert manifest["permissions"] == ["nativeMessaging", "storage"]
    assert manifest["host_permissions"] == ["https://chatgpt.com/*"]
    assert manifest["background"] == {"service_worker": "background_full_entry.js"}
    assert manifest["content_scripts"][0]["world"] == "ISOLATED"
    assert manifest["content_scripts"][0]["js"] == [
        "content.js",
        "content_rerender.js",
        "content_auto_send.js",
        "content_auto_retry.js",
        "content_project_launcher.js",
        "content_project_tab_binding.js",
        "content_repair_retry.js",
        "content_health.js",
    ]
    serialized = json.dumps(manifest)
    for forbidden in ("<all_urls>", "tabs", "debugger", "webRequest", "downloads"):
        assert forbidden not in serialized


def test_manual_content_handler_remains_assisted() -> None:
    content = read("content.js")
    assert "bdb-action-v1" in content
    assert "BDB: Wykonaj" in content
    assert "Przygotuj kontynuację" in content
    start = content.index('button.addEventListener("click", async () => {')
    end = content.index("panel.append(button, output);", start)
    manual_handler = content[start:end]
    assert "button.click()" not in manual_handler
    assert "send-button" not in manual_handler
    assert "autoSend(" not in manual_handler
    assert "MutationObserver" in content
    assert "navigator.clipboard.writeText" in content


def test_manual_click_reparses_current_code_block_before_submission() -> None:
    content = read("content.js")
    start = content.index('button.addEventListener("click", async () => {')
    end = content.index("panel.append(button, output);", start)
    manual_handler = content[start:end]
    assert "const currentAction = parseAction(codeBlock);" in manual_handler
    assert 'type: "BDB_SUBMIT_ASSISTED"' in content
    assert "bdbRunAssistedAction(currentAction)" in manual_handler
    assert '"BDB_POLL_ASSISTED"' in content
    assert 'type: "BDB_SUBMIT_ACTION", action: currentAction' not in manual_handler
    assert "Blok BDB zmienił się" in manual_handler


def test_background_accepts_only_versioned_actions_and_native_host() -> None:
    background = read("background.js")
    assert 'const HOST_NAME = "com.bartosz.dev_bridge"' in background
    assert 'const ACTION_SCHEMA = "bdb-action-v1"' in background
    assert 'action: "submit_action"' in background
    assert 'const BDB_EXTENSION_VERSION = "0.4.7"' in background
    assert "validateNativeVersion" in background
    assert "sendNativeSubmission" in background
    assert "chrome.runtime.sendNativeMessage" in background
    assert "eval(" not in background
    assert "new Function" not in background


def test_workspace_context_uses_native_context_without_new_permissions() -> None:
    background = read("background.js")
    content = read("content.js")
    css = read("content.css")
    assert 'const WORKSPACE_CONTEXT_OPERATION = "workspace_context"' in background
    assert 'action: "context"' in background
    assert "return await workspaceContext(action);" in background
    assert 'presentation.mode === "compact"' in content
    assert 'codeBlock.classList.add("bdb-action-source-hidden")' in content
    assert ".bdb-action-source-hidden" in css
    assert 'status: "completed"' in background
    assert 'operation: WORKSPACE_CONTEXT_OPERATION' in background


def test_required_promotion_blocks_auto_until_receipt_matches_command() -> None:
    background = read("background.js")
    assert 'promotion.mode === "required"' in background
    assert "waitForRequiredPromotion(action, response)" in background
    assert "context.latest_promotion" in background
    assert "receipt.command_id === commandId" in background
    assert "Array.isArray(context.source_changes)" in background
    assert "context.source_changes.length === 0" in background
    assert 'reason: "promotion_not_observed"' in background
    assert 'status: "needs_user"' in background
    assert "PROMOTION_WAIT_ATTEMPTS" in background
    assert 'schema: "bdb-post-action-verification-v1"' in background
    assert '"verified"' in background


def test_auto_continues_only_after_verified_rollback_profile_failure() -> None:
    background = read("background.js")
    assert "continue_on_failure" in background
    assert "isRecoverableProfileFailure" in background
    assert 'result.status === "failed" || result.status === "timeout"' in background
    assert 'data.operation === "multi_file_patch"' in background
    assert "data.rollback_performed === true" in background
    assert 'data.checkpoint_state === "rolled_back"' in background
    assert "recoverableFailure || recoverableReadFailure || recoverableNativeError" in background
    assert 'response.error.code === "internal_error"' in background
    assert "recoverableFailure," in background


def test_auto_is_milestone_scoped_and_explicitly_opt_in() -> None:
    background = read("background.js")
    content = read("content.js")
    assert "autoEnabled: false" in background
    assert "autoMaxIterations" not in background
    assert "autoMaxMinutes" not in background
    assert "autoMilestoneProgress" in background
    assert '"milestone_completed"' in background
    assert "now - state.startedAt" not in background
    assert 'automation.mode !== "auto"' in content
    assert "BDB_CONSIDER_AUTO" in content
    assert "BDB_AUTO_RESULT" in content
    assert "{0,127}" in background


def test_auto_entry_synchronizes_loop_state_and_recovers_replay_claims() -> None:
    entry = read("background_entry.js")
    full_entry = read("background_full_entry.js")
    background = read("background.js")
    polling = read("background_async_result.js")
    recovery = read("background_auto_recovery.js")
    popup = read("popup.js")
    assert 'importScripts("background.js")' in entry
    assert '"background_entry.js"' in full_entry
    assert '"background_async_result.js"' in full_entry
    assert '"background_auto_recovery.js"' in full_entry
    assert '"background_project_launcher.js"' in full_entry
    assert '"background_action_preflight.js"' in full_entry
    assert '"background_repair_correlation.js"' in full_entry
    assert '"background_conversation_binding.js"' in full_entry
    assert '"background_task_controller.js"' in full_entry
    assert "canonicalAutoStateKey" in entry
    assert "chrome.storage.session.get(null)" in entry
    assert "legacyAutoStateEntries" in entry
    assert "if (isStoredAutoState(canonical))" in entry
    assert "const current = await chrome.storage.session.get(canonicalKey);" in entry
    assert 'reason = "iteration_already_processed"' in entry
    assert "expectedIteration" in entry
    assert "claimAutoReplay" in background
    assert "AUTO_REPLAY_GUARD_KEY" in background
    assert "BDB_AUTO_REPLAY_LEASE_MS" in recovery
    assert "claimRecoverableAutoReplay" in recovery
    assert "bdbReleaseReplayClaim" in recovery
    assert "bdbCompleteReplayClaim" in recovery
    assert 'reason: "iteration_in_progress"' in recovery
    assert 'reason: "iteration_already_processed"' in recovery
    assert 'action: "result"' in polling
    assert "waitForRequiredPromotion(action, latest)" in polling
    assert "BDB_ASYNC_RESULT_ATTEMPTS" in polling
    assert "Oczekiwana iteracja" in popup


def test_auto_decision_retry_is_bounded_and_waits_for_processing_claim() -> None:
    retry = read("content_auto_retry.js")
    assert "BDB_AUTO_DECISION_RETRY_ATTEMPTS" in retry
    assert "BDB_AUTO_DECISION_RETRY_MS" in retry
    assert '"non_sequential_iteration"' in retry
    assert '"iteration_in_progress"' in retry
    assert "BDB_AUTO_TRANSIENT_REASONS.has(auto.reason)" in retry
    assert "auto.expectedIteration <= iteration" in retry
    assert 'type: "BDB_CONSIDER_AUTO", action' in retry
    assert "retryExhausted: true" in retry
    assert "claimAutoReplay" not in retry
    assert "chrome.storage" not in retry
    assert "sendNativeMessage" not in retry


def test_auto_send_requires_confirmed_multi_strategy_submission() -> None:
    companion = read("content_auto_send.js")
    assert "BDB_AUTO_SEND_STRATEGIES" in companion
    assert '"button_click"' in companion
    assert '"request_submit"' in companion
    assert '"enter_key"' in companion
    assert "bdbWaitForSendConfirmation" in companion
    assert "bdbUserMessageContains" in companion
    assert "form.requestSubmit" in companion
    assert 'new KeyboardEvent("keydown"' in companion
    assert "send_not_confirmed" in companion
    assert "markerStillPresent" in companion
    assert "confirmedVia" in companion


def test_project_creator_handoff_is_leased_local_and_confirmed() -> None:
    background = read("background_project_launcher.js")
    content = read("content_project_launcher.js")
    tab_binding = read("content_project_tab_binding.js")
    assert 'action: "project_launch_peek"' in background
    assert '"project_launch_claim"' in background
    assert '"project_launch_ack"' in background
    assert '"project_conversation_bind"' in background
    assert "binding.tab_id === tabId" in read("background_conversation_binding.js")
    assert 'operation: "project_conversation_bind"' in tab_binding
    assert "conversation_id: conversationId" in tab_binding
    assert "sendNative(" in background
    assert "fetch(" not in background
    assert "XMLHttpRequest" not in background
    assert "crypto.randomUUID()" in content
    assert "bdbClaimProjectLaunch" in content
    assert "bdbAcknowledgeProjectLaunch" in content
    assert "bdbWaitForSendConfirmation" in content
    assert "BDB_AUTO_SEND_STRATEGIES" in content
    assert 'currentText.trim() !== ""' in content
    assert "claim expires" in content
    assert "navigator.clipboard" not in content


def test_repair_retry_is_explicit_correlated_and_same_chat() -> None:
    background = read("background_repair_correlation.js")
    content = read("content_repair_retry.js")
    assert 'schema: "bdb-repair-correlation-v1"' in background
    assert 'role: "initial"' in background
    assert 'role: "repair"' in background
    assert "predecessor_session_id" in background
    assert "awaiting_corrected_action" in background
    assert "Napraw i uruchom ponownie" in content
    assert "bdbContentRepairHashes" in content
    assert "bdbContentRepairRequestCorrection" in content
    assert "prepareContinuation(text, { requireEmpty: true })" in content
    assert 'type: "BDB_SUBMIT_ACTION"' in content
    assert "execCommand" not in background
    assert "sendNativeMessage" not in content


def test_extension_contains_no_remote_scripts_or_inline_script() -> None:
    manifest = json.loads(read("manifest.json"))
    files = [path for path in EXTENSION.rglob("*") if path.is_file()]
    assert files
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert "http://" not in text
        assert "https://" not in text or path.name in {"manifest.json", "README.md"}
    popup = read("popup.html")
    assert '<script src="popup.js"></script>' in popup
    assert "<script>" not in popup

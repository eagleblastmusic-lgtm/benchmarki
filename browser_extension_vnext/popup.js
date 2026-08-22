"use strict";

const statusNode = document.getElementById("status");
const insertProjectPromptButton = document.getElementById("insert-project-prompt");
const resumeButton = document.getElementById("resume");

function render(message) {
  statusNode.textContent = message;
}

async function refresh() {
  try {
    const result = await chrome.runtime.sendMessage({ type: "bdb-vnext-status" });
    if (!result || result.ok !== true || !result.response) {
      throw new Error(result && result.error ? result.error : "Canonical vNext route unavailable");
    }
    const activation = result.response.activation || {};
    render(
      `Generation: ${result.response.generation_id}\n` +
      `Protocol: ${result.response.protocol_generation}\n` +
      `Native: ${result.response.native_host_name}\n` +
      `Activation: ${activation.state || "UNKNOWN"}\n` +
      `Production intake: ${activation.production_acceptance === true ? "ON" : "OFF"}`
    );
  } catch (error) {
    render(error instanceof Error ? error.message : String(error));
  }
}

insertProjectPromptButton.addEventListener("click", async () => {
  insertProjectPromptButton.disabled = true;
  render("Sprawdzam aktywną rozmowę ChatGPT…");
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || !Number.isInteger(tab.id)) {
      throw new Error("Nie znaleziono aktywnej karty ChatGPT");
    }
    const result = await chrome.tabs.sendMessage(tab.id, {
      type: "bdb-vnext-project-launch-insert"
    });
    if (!result || result.ok !== true) {
      const code = result && result.code ? result.code : "project_prompt_not_inserted";
      const messages = {
        conversation_not_eligible: "Wybierz zwykłą rozmowę ChatGPT (nowa bez /c/... też jest obsługiwana) i pozostaw pusty composer.",
        no_pending_prompt: "Brak oczekującego promptu. Najpierw wybierz projekt w BDB i kliknij „Wstaw prompt planu”.",
        composer_not_empty: "Composer nie jest pusty — nic nie nadpisano.",
        project_prompt_not_inserted: "Prompt nie został wstawiony; niczego nie wysłano."
      };
      throw new Error(messages[code] || `Wstawianie zatrzymane: ${code}`);
    }
    render("Prompt początkowy wstawiony do wybranej rozmowy. Wyślij go ręcznie w ChatGPT.");
  } catch (error) {
    render(error instanceof Error ? error.message : String(error));
  } finally {
    insertProjectPromptButton.disabled = false;
  }
});

resumeButton.addEventListener("click", async () => {
  resumeButton.disabled = true;
  render("Looking up non-terminal vNext outbox entries…");
  try {
    const result = await chrome.runtime.sendMessage({ type: "bdb-vnext-resume-outbox" });
    if (!result || !Array.isArray(result)) {
      throw new Error(result && result.error ? result.error : "Resume failed closed");
    }
    const acked = result.filter((item) => item && item.receipt).length;
    const unresolved = result.length - acked;
    render(`Resume complete. Canonical ACKs: ${acked}. Still uncertain: ${unresolved}.`);
  } catch (error) {
    render(error instanceof Error ? error.message : String(error));
  } finally {
    resumeButton.disabled = false;
  }
});

refresh();

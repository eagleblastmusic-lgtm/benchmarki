"use strict";

const statusNode = document.getElementById("status");
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

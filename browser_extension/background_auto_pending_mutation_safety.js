"use strict";

// A bounded async poll can end while Native Host still owns a mutating command.
// Treat only genuinely ambiguous terminal responses as uncertain. Deterministic
// validation failures such as invalid_payload remain safe to correct and retry.
const BDB_AUTO_AMBIGUOUS_MUTATION_STATUSES = new Set([
  "accepted",
  "pending"
]);
const BDB_AUTO_AMBIGUOUS_MUTATION_ERROR_CODES = new Set([
  "internal_error",
  "timeout"
]);
const bdbConsiderAutoBeforePendingMutationSafety = considerAuto;

function bdbAutoMutationDecisionIsAmbiguous(decision) {
  if (
    !decision ||
    decision.executed !== true ||
    !decision.response ||
    typeof decision.response !== "object" ||
    Array.isArray(decision.response)
  ) {
    return false;
  }

  const response = decision.response;
  if (response.async_poll_exhausted === true) {
    return true;
  }
  if (BDB_AUTO_AMBIGUOUS_MUTATION_STATUSES.has(response.status)) {
    return true;
  }

  const errorCode = response.error && response.error.code;
  return Boolean(
    response.status === "failed" &&
    typeof errorCode === "string" &&
    BDB_AUTO_AMBIGUOUS_MUTATION_ERROR_CODES.has(errorCode)
  );
}

function bdbAutoMutationAmbiguousResponseDetail(response) {
  const status = response && response.status ? response.status : "unknown";
  const errorCode = response && response.error && response.error.code;
  const commandId = response && response.command_id;
  return [
    `Native mutation result remained ambiguous (${status})`,
    errorCode ? `error=${errorCode}` : null,
    commandId ? `command_id=${commandId}` : null,
    response && response.async_poll_exhausted === true
      ? "async_poll_exhausted=true"
      : null
  ].filter(Boolean).join("; ").slice(0, 240);
}

considerAuto = async function considerAutoWithPendingMutationSafety(action, tabId) {
  const metadata = automationMetadata(action);
  const mutating = Boolean(
    metadata &&
    bdbAutoMutationIsUnsafeToReplay(action)
  );
  const decision = await bdbConsiderAutoBeforePendingMutationSafety(action, tabId);

  if (!mutating || !bdbAutoMutationDecisionIsAmbiguous(decision)) {
    return decision;
  }

  return bdbAutoRecordMutationReconciliation(
    action,
    tabId,
    metadata,
    new Error(bdbAutoMutationAmbiguousResponseDetail(decision.response))
  );
};

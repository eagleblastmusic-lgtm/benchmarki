"use strict";

// Load the base message router, bounded result recovery, project-launch adapter,
// client-side preflight, explicit repair correlation, then durable conversation
// correlation. The task controller is last so it observes the final, fully
// wrapped AUTO and submission paths without weakening their safety gates.
importScripts(
  "background_entry.js",
  "background_async_result.js",
  "background_auto_recovery.js",
  "background_project_launcher.js",
  "background_action_preflight.js",
  "background_repair_correlation.js",
  "background_conversation_binding.js",
  "background_auto_mutation_safety.js",
  "background_auto_pending_mutation_safety.js",
  "background_task_controller.js"
);

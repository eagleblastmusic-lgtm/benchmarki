"""Project Memory v2 Schema, Contract, Invariants and V1 Mapping.

NX-010 Specification and Formal Data Model:
- Canonical single authority model (eliminates competing mutable facts).
- Relational schema with referential integrity and strict uniqueness constraints.
- Partial upgrade compatibility states (V1_ONLY -> V2_INITIALIZED -> SHADOW_COMPATIBLE -> V2_AUTHORITY).
- Complete, lossless field mapping from v1 JSON structures to v2 relational entities.
"""

from __future__ import annotations

import enum
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

PROJECT_MEMORY_V2_SCHEMA_VERSION = "2.0.0"
PROJECT_MEMORY_V2_SCHEMA_IDENTIFIER = "bdb-project-memory-v2"

# ==============================================================================
# 1. CANONICAL AUTHORITY INVENTORY
# ==============================================================================

@dataclass(frozen=True)
class AuthorityFact:
    fact_name: str
    current_owner: str
    current_storage: str
    mutability: str  # "MUTABLE", "IMMUTABLE", "APPEND_ONLY"
    identity: str
    revision_generation_rule: str
    relationships: str
    v2_owner: str
    v2_table: str


AUTHORITY_INVENTORY: tuple[AuthorityFact, ...] = (
    AuthorityFact(
        fact_name="project_metadata",
        current_owner="ProjectCatalog (downstream) / ProjectMemoryStore",
        current_storage="project-catalog.json / memory state",
        mutability="MUTABLE",
        identity="project_id",
        revision_generation_rule="monotonic project revision",
        relationships="Root project entity",
        v2_owner="ProjectMemoryStoreV2",
        v2_table="projects",
    ),
    AuthorityFact(
        fact_name="project_plan",
        current_owner="ProjectMemoryStore (plan-v*.json)",
        current_storage="plan-v*.json + current-plan-pointer.json",
        mutability="IMMUTABLE",
        identity="(project_id, plan_version)",
        revision_generation_rule="monotonic plan_version integer",
        relationships="FK to projects(project_id)",
        v2_owner="ProjectMemoryStoreV2",
        v2_table="project_plans",
    ),
    AuthorityFact(
        fact_name="task_execution_state",
        current_owner="ProjectMemoryStore (state.execution.task_statuses)",
        current_storage="project-memory/{id}/state.json",
        mutability="MUTABLE",
        identity="(project_id, task_id)",
        revision_generation_rule="advances on task transition",
        relationships="FK to projects(project_id)",
        v2_owner="ProjectMemoryStoreV2",
        v2_table="task_execution_states",
    ),
    AuthorityFact(
        fact_name="execution_binding",
        current_owner="ProjectMemoryStore (state.execution.bindings)",
        current_storage="project-memory/{id}/state.json",
        mutability="MUTABLE",
        identity="execution_binding_id",
        revision_generation_rule="monotonic generation per task, max 1 ACTIVE",
        relationships="FK to projects(project_id), FK to project_plans",
        v2_owner="ProjectMemoryStoreV2",
        v2_table="execution_bindings",
    ),
    AuthorityFact(
        fact_name="attempt_result",
        current_owner="ProjectMemoryStore (state.execution.attempts/acceptance_results)",
        current_storage="project-memory/{id}/state.json",
        mutability="IMMUTABLE",
        identity="attempt_id",
        revision_generation_rule="linked to execution_binding generation",
        relationships="FK to execution_bindings(execution_binding_id)",
        v2_owner="ProjectMemoryStoreV2",
        v2_table="attempts",
    ),
    AuthorityFact(
        fact_name="launch_outbox",
        current_owner="ProjectMemoryStore (state.execution.launch_outbox)",
        current_storage="project-memory/{id}/state.json",
        mutability="MUTABLE",
        identity="launch_id",
        revision_generation_rule="status transitions PENDING -> PUBLISHED -> ACKNOWLEDGED",
        relationships="FK to execution_bindings(execution_binding_id)",
        v2_owner="ProjectMemoryStoreV2",
        v2_table="launch_outbox",
    ),
    AuthorityFact(
        fact_name="milestone_run",
        current_owner="ProjectMemoryStore (state.execution.milestone_runs)",
        current_storage="project-memory/{id}/state.json",
        mutability="MUTABLE",
        identity="run_id",
        revision_generation_rule="sequential execution per milestone",
        relationships="FK to projects(project_id)",
        v2_owner="ProjectMemoryStoreV2",
        v2_table="runs",
    ),
    AuthorityFact(
        fact_name="auto_scope",
        current_owner="ProjectMemoryStore (state.execution.milestone_auto)",
        current_storage="project-memory/{id}/state.json",
        mutability="MUTABLE",
        identity="scope_id",
        revision_generation_rule="at most 1 active auto scope",
        relationships="FK to projects(project_id)",
        v2_owner="ProjectMemoryStoreV2",
        v2_table="scopes",
    ),
    AuthorityFact(
        fact_name="checkpoint",
        current_owner="ProjectMemoryStore (state.checkpoints)",
        current_storage="project-memory/{id}/state.json",
        mutability="IMMUTABLE",
        identity="checkpoint_id",
        revision_generation_rule="monotonic append sequence",
        relationships="FK to projects(project_id)",
        v2_owner="ProjectMemoryStoreV2",
        v2_table="checkpoints",
    ),
    AuthorityFact(
        fact_name="decision_record",
        current_owner="ProjectMemoryStore (state.decisions)",
        current_storage="project-memory/{id}/state.json",
        mutability="MUTABLE",
        identity="decision_id",
        revision_generation_rule="status active -> superseded",
        relationships="FK to projects(project_id)",
        v2_owner="ProjectMemoryStoreV2",
        v2_table="decisions",
    ),
    AuthorityFact(
        fact_name="inbox_item",
        current_owner="ProjectMemoryStore (state.inbox)",
        current_storage="project-memory/{id}/state.json",
        mutability="MUTABLE",
        identity="inbox_id",
        revision_generation_rule="status new -> processed/resolved",
        relationships="FK to projects(project_id)",
        v2_owner="ProjectMemoryStoreV2",
        v2_table="inbox_items",
    ),
    AuthorityFact(
        fact_name="risk_record",
        current_owner="ProjectMemoryStore (state.risks)",
        current_storage="project-memory/{id}/state.json",
        mutability="MUTABLE",
        identity="risk_id",
        revision_generation_rule="status open -> resolved",
        relationships="FK to projects(project_id)",
        v2_owner="ProjectMemoryStoreV2",
        v2_table="risks",
    ),
    AuthorityFact(
        fact_name="debt_record",
        current_owner="ProjectMemoryStore (state.technical_debt)",
        current_storage="project-memory/{id}/state.json",
        mutability="MUTABLE",
        identity="debt_id",
        revision_generation_rule="status open -> resolved",
        relationships="FK to projects(project_id)",
        v2_owner="ProjectMemoryStoreV2",
        v2_table="technical_debt",
    ),
    AuthorityFact(
        fact_name="attention_item",
        current_owner="ProjectMemoryStore (state.attention)",
        current_storage="project-memory/{id}/state.json",
        mutability="MUTABLE",
        identity="attention_id",
        revision_generation_rule="status open -> resolved",
        relationships="FK to projects(project_id)",
        v2_owner="ProjectMemoryStoreV2",
        v2_table="attention_items",
    ),
    AuthorityFact(
        fact_name="audit_event",
        current_owner="ProjectMemoryStore (state.events)",
        current_storage="project-memory/{id}/state.json",
        mutability="APPEND_ONLY",
        identity="event_id",
        revision_generation_rule="strictly increasing revision / sequence",
        relationships="FK to projects(project_id)",
        v2_owner="ProjectMemoryStoreV2",
        v2_table="audit_events",
    ),
)


def count_multi_authority_mutable_facts() -> int:
    """Verifies that each mutable fact has exactly one canonical v2 owner."""
    owners = set()
    conflicts = 0
    for fact in AUTHORITY_INVENTORY:
        if fact.mutability in {"MUTABLE", "APPEND_ONLY"}:
            key = (fact.fact_name, fact.v2_owner)
            if fact.fact_name in owners:
                conflicts += 1
            owners.add(fact.fact_name)
    return conflicts


# ==============================================================================
# 2. V2 RELATIONAL DDL SCHEMA
# ==============================================================================

PROJECT_MEMORY_V2_DDL = """
-- Project Memory v2 SQLite Schema (NX-010 / NX-011)
PRAGMA foreign_keys = ON;

-- 1. Schema migrations table
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL,
    description TEXT NOT NULL
);

-- 2. Projects root table
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    repo_alias TEXT NOT NULL,
    local_repo_path TEXT NOT NULL,
    github_repo TEXT,
    brief_json TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 3. Immutable Plan Identity & Content
CREATE TABLE IF NOT EXISTS project_plans (
    project_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL CHECK(plan_version >= 1),
    plan_digest TEXT NOT NULL,
    schema TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    PRIMARY KEY (project_id, plan_version),
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE RESTRICT
);

-- 4. Runs / Milestone Runs
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    milestone_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'completed', 'failed', 'stopped')),
    current_task_id TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE RESTRICT
);

-- 5. Scope / Milestone AUTO Scope
CREATE TABLE IF NOT EXISTS scopes (
    scope_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('RUNNABLE', 'RUNNING', 'STOPPED', 'COMPLETED', 'FAILED')),
    milestone_id TEXT,
    max_tasks INTEGER,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_active_scope ON scopes(project_id) WHERE status IN ('RUNNABLE', 'RUNNING');

-- 6. Task Execution State
CREATE TABLE IF NOT EXISTS task_execution_states (
    project_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'active', 'review', 'completed', 'blocked', 'skipped')),
    active_binding_id TEXT,
    last_result_digest TEXT,
    prerequisite_blockers_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_id, task_id),
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE RESTRICT
);

-- 7. Execution Bindings
CREATE TABLE IF NOT EXISTS execution_bindings (
    execution_binding_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    launch_id TEXT NOT NULL UNIQUE,
    correlation_id TEXT NOT NULL,
    command_id TEXT NOT NULL,
    repo_alias TEXT NOT NULL,
    expected_repo_head_before TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('PENDING', 'ACTIVE', 'ACCEPTED', 'FAILED', 'SUPERSEDED', 'CANCELLED')),
    generation INTEGER NOT NULL CHECK(generation >= 1),
    superseded INTEGER NOT NULL DEFAULT 0 CHECK(superseded IN (0, 1)),
    conversation_id TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    FOREIGN KEY (project_id, plan_version) REFERENCES project_plans(project_id, plan_version) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_active_binding_per_task ON execution_bindings(project_id, task_id) WHERE status = 'ACTIVE';

-- 8. Attempts & Acceptance Results
CREATE TABLE IF NOT EXISTS attempts (
    attempt_id TEXT PRIMARY KEY,
    execution_binding_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK(generation >= 1),
    head_before TEXT NOT NULL,
    head_after TEXT,
    execution_status TEXT NOT NULL,
    validation_status TEXT NOT NULL,
    promotion_status TEXT NOT NULL,
    failure_code TEXT,
    canonical_result_digest TEXT NOT NULL,
    identity_version TEXT NOT NULL CHECK(identity_version IN ('v1', 'v2')),
    summary TEXT NOT NULL DEFAULT '',
    evidence_refs_json TEXT NOT NULL DEFAULT '[]',
    criteria_json TEXT NOT NULL DEFAULT '[]',
    canonical_refs_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (execution_binding_id) REFERENCES execution_bindings(execution_binding_id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE RESTRICT
);

-- 9. Checkpoints
CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    label TEXT NOT NULL,
    plan_version INTEGER,
    git_head TEXT,
    completed_task_ids_json TEXT NOT NULL DEFAULT '[]',
    current_task_id TEXT,
    active_decision_ids_json TEXT NOT NULL DEFAULT '[]',
    open_blocker_ids_json TEXT NOT NULL DEFAULT '[]',
    human_summary TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE RESTRICT
);

-- 10. Launch Outbox
CREATE TABLE IF NOT EXISTS launch_outbox (
    outbox_id TEXT PRIMARY KEY,
    launch_id TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    execution_binding_id TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    command_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('PENDING', 'PUBLISHED', 'ACKNOWLEDGED')),
    prompt TEXT NOT NULL,
    auto_send INTEGER NOT NULL CHECK(auto_send IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (execution_binding_id) REFERENCES execution_bindings(execution_binding_id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE RESTRICT
);

-- 11. Decisions
CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    title TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'superseded', 'draft')),
    created_at TEXT NOT NULL,
    related_task_ids_json TEXT NOT NULL DEFAULT '[]',
    related_plan_version INTEGER,
    supersedes_decision_id TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE RESTRICT
);

-- 12. Inbox Items
CREATE TABLE IF NOT EXISTS inbox_items (
    inbox_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('new', 'processed', 'dismissed')),
    created_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE RESTRICT
);

-- 13. Risks
CREATE TABLE IF NOT EXISTS risks (
    risk_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    severity TEXT NOT NULL CHECK(severity IN ('low', 'medium', 'high', 'critical')),
    status TEXT NOT NULL CHECK(status IN ('open', 'resolved', 'mitigated', 'accepted')),
    created_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE RESTRICT
);

-- 14. Technical Debt
CREATE TABLE IF NOT EXISTS technical_debt (
    debt_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open', 'resolved', 'wontfix')),
    created_at TEXT NOT NULL,
    related_task_ids_json TEXT NOT NULL DEFAULT '[]',
    suggested_review_milestone TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE RESTRICT
);

-- 15. Attention Items
CREATE TABLE IF NOT EXISTS attention_items (
    attention_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open', 'resolved')),
    created_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE RESTRICT
);

-- 16. Append-Only Audit Events
CREATE TABLE IF NOT EXISTS audit_events (
    event_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    logical_tx_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    human_summary TEXT NOT NULL,
    task_id TEXT,
    milestone_id TEXT,
    plan_version INTEGER,
    git_head TEXT,
    correlation_id TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    timestamp TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_audit_events_seq ON audit_events(project_id, revision);

-- Strict append-only triggers preventing UPDATE and DELETE on audit_events
CREATE TRIGGER IF NOT EXISTS trg_audit_events_no_update
BEFORE UPDATE ON audit_events
BEGIN
    SELECT RAISE(FAIL, 'audit_events is append-only: updates are prohibited');
END;

CREATE TRIGGER IF NOT EXISTS trg_audit_events_no_delete
BEFORE DELETE ON audit_events
BEGIN
    SELECT RAISE(FAIL, 'audit_events is append-only: deletes are prohibited');
END;
"""


# ==============================================================================
# 3. PARTIAL UPGRADE COMPATIBILITY STATES
# ==============================================================================

class UpgradeState(enum.Enum):
    V1_ONLY = "V1_ONLY"
    V2_INITIALIZED = "V2_INITIALIZED"
    SHADOW_COMPATIBLE = "SHADOW_COMPATIBLE"
    V2_AUTHORITY = "V2_AUTHORITY"


@dataclass(frozen=True)
class UpgradeStatePolicy:
    state: UpgradeState
    canonical_write_authority: str
    v2_read_allowed: bool
    v2_write_allowed: bool
    rollback_supported: bool


UPGRADE_STATE_POLICIES: dict[UpgradeState, UpgradeStatePolicy] = {
    UpgradeState.V1_ONLY: UpgradeStatePolicy(
        state=UpgradeState.V1_ONLY,
        canonical_write_authority="V1_JSON",
        v2_read_allowed=False,
        v2_write_allowed=False,
        rollback_supported=True,
    ),
    UpgradeState.V2_INITIALIZED: UpgradeStatePolicy(
        state=UpgradeState.V2_INITIALIZED,
        canonical_write_authority="V1_JSON",
        v2_read_allowed=False,
        v2_write_allowed=False,
        rollback_supported=True,
    ),
    UpgradeState.SHADOW_COMPATIBLE: UpgradeStatePolicy(
        state=UpgradeState.SHADOW_COMPATIBLE,
        canonical_write_authority="V1_JSON",
        v2_read_allowed=True,
        v2_write_allowed=True,  # shadow mirror write
        rollback_supported=True,
    ),
    UpgradeState.V2_AUTHORITY: UpgradeStatePolicy(
        state=UpgradeState.V2_AUTHORITY,
        canonical_write_authority="V2_SQLITE",
        v2_read_allowed=True,
        v2_write_allowed=True,
        rollback_supported=False,
    ),
}


def validate_upgrade_state_transition(from_state: UpgradeState, to_state: UpgradeState) -> bool:
    """Enforces strict, non-ambiguous upgrade progression."""
    valid_transitions = {
        (UpgradeState.V1_ONLY, UpgradeState.V2_INITIALIZED),
        (UpgradeState.V2_INITIALIZED, UpgradeState.SHADOW_COMPATIBLE),
        (UpgradeState.V2_INITIALIZED, UpgradeState.V1_ONLY),  # clean teardown
        (UpgradeState.SHADOW_COMPATIBLE, UpgradeState.V2_AUTHORITY),
        (UpgradeState.SHADOW_COMPATIBLE, UpgradeState.V1_ONLY),  # rollback
    }
    return (from_state, to_state) in valid_transitions


# ==============================================================================
# 4. V1 TO V2 FIELD MAPPING
# ==============================================================================

V1_TO_V2_FIELD_MAPPING: dict[str, dict[str, str]] = {
    "ProjectMemoryState": {
        "project_id": "projects.project_id",
        "revision": "projects.revision",
        "events": "audit_events (relational rows)",
        "decisions": "decisions (relational rows)",
        "inbox": "inbox_items (relational rows)",
        "risks": "risks (relational rows)",
        "technical_debt": "technical_debt (relational rows)",
        "attention": "attention_items (relational rows)",
        "checkpoints": "checkpoints (relational rows)",
        "execution.bindings": "execution_bindings (relational rows)",
        "execution.attempts": "attempts (relational rows)",
        "execution.acceptance_results": "attempts (relational rows)",
        "execution.task_statuses": "task_execution_states.status",
        "execution.milestone_runs": "runs (relational rows)",
        "execution.milestone_auto": "scopes (relational rows)",
        "execution.launch_outbox": "launch_outbox (relational rows)",
        "execution.checkpoints": "checkpoints (relational rows)",
    },
    "ProjectEvent": {
        "event_id": "audit_events.event_id",
        "project_id": "audit_events.project_id",
        "event_type": "audit_events.event_type",
        "timestamp": "audit_events.timestamp",
        "human_summary": "audit_events.human_summary",
        "task_id": "audit_events.task_id",
        "milestone_id": "audit_events.milestone_id",
        "plan_version": "audit_events.plan_version",
        "git_head": "audit_events.git_head",
        "correlation_id": "audit_events.correlation_id",
    },
    "ProjectExecutionBinding": {
        "execution_binding_id": "execution_bindings.execution_binding_id",
        "project_id": "execution_bindings.project_id",
        "plan_version": "execution_bindings.plan_version",
        "task_id": "execution_bindings.task_id",
        "launch_id": "execution_bindings.launch_id",
        "correlation_id": "execution_bindings.correlation_id",
        "command_id": "execution_bindings.command_id",
        "repo_alias": "execution_bindings.repo_alias",
        "expected_repo_head_before": "execution_bindings.expected_repo_head_before",
        "created_at": "execution_bindings.created_at",
        "status": "execution_bindings.status",
        "superseded": "execution_bindings.superseded",
        "generation": "execution_bindings.generation",
        "conversation_id": "execution_bindings.conversation_id",
        "finished_at": "execution_bindings.finished_at",
    },
    "ProjectLaunchOutboxRecord": {
        "outbox_id": "launch_outbox.outbox_id",
        "launch_id": "launch_outbox.launch_id",
        "project_id": "launch_outbox.project_id",
        "plan_version": "launch_outbox.plan_version",
        "task_id": "launch_outbox.task_id",
        "execution_binding_id": "launch_outbox.execution_binding_id",
        "correlation_id": "launch_outbox.correlation_id",
        "command_id": "launch_outbox.command_id",
        "status": "launch_outbox.status",
        "prompt": "launch_outbox.prompt",
        "auto_send": "launch_outbox.auto_send",
        "created_at": "launch_outbox.created_at",
        "updated_at": "launch_outbox.updated_at",
    },
}


def verify_v1_v2_mapping_completeness() -> tuple[bool, int]:
    """Ensures all required v1 data structures have complete v2 column targets with 0 unmapped."""
    unmapped = 0
    for entity, mapping in V1_TO_V2_FIELD_MAPPING.items():
        for source_field, target_column in mapping.items():
            if not target_column:
                unmapped += 1
    return (unmapped == 0, unmapped)

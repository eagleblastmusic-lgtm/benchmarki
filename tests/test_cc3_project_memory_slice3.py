from __future__ import annotations

import json
from pathlib import Path

import pytest

from bdb_vnext.project_catalog import PROJECT_PLAN_SCHEMA, ProjectBrief, ProjectCatalog, ProjectPlan, ProjectTask, ProjectMilestone, new_project_record, validate_project_plan
from bdb_vnext.project_memory import ProjectMemoryError, ProjectMemoryStore, build_handoff_prompt, project_health, project_status_sentence, resolve_next_action, semantic_plan_diff


def plan_document(project_id: str = "memory-project", version: int = 1, *, completed: bool = False, title: str = "Task one", supersedes: int | None = None) -> dict[str, object]:
    result: dict[str, object] = {
        "schema": PROJECT_PLAN_SCHEMA,
        "project_id": project_id,
        "project_name": "Memory Project",
        "plan_version": version,
        "milestones": [{"id": "m1", "title": "Foundation", "description": "Foundation", "status": "active"}],
        "tasks": [{"id": "t1", "milestone_id": "m1", "title": title, "description": "Do one thing", "status": "completed" if completed else "active", "dependencies": [], "acceptance_criteria": ["works", "safe"]}],
        "current_task_id": "t1",
    }
    if supersedes is not None:
        result.update({"supersedes_version": supersedes, "revision_reason": "clarification", "revision_summary": "A bounded revision"})
    return result


def make_store(tmp_path: Path) -> tuple[ProjectMemoryStore, object]:
    store = ProjectMemoryStore(tmp_path / "runtime", "memory-project")
    plan = validate_project_plan(plan_document())
    store.ensure_initial_plan(plan)
    return store, plan


def test_plan_v1_to_v2_is_immutable_and_exact_successor(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    candidate = validate_project_plan(plan_document(version=2, title="Updated", supersedes=1))
    preview = store.preview_update(candidate)
    assert preview.accepted is True
    applied = store.apply_update(candidate, preview)
    assert applied.plan_version == "2"
    assert tuple(item.plan_version for item in store.plan_versions()) == ("1", "2")
    assert store.current_plan() == applied
    assert json.loads(store.current_pointer.read_text(encoding="utf-8"))["plan_digest"]


def test_wrong_successor_or_completed_reconciliation_is_fail_closed(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    assert store.preview_update(validate_project_plan(plan_document(version=3, supersedes=1))).reason_code == "plan_successor_required"
    completed = validate_project_plan(plan_document(version=2, completed=True, supersedes=1))
    store.apply_update(completed)
    downgrade = validate_project_plan(plan_document(version=3, completed=False, supersedes=2))
    preview = store.preview_update(downgrade)
    assert preview.accepted is False
    assert any(item.startswith("completed_task_downgrade") for item in preview.completed_protection)


def test_plan_diff_uses_stable_ids_and_ignores_reordering(tmp_path: Path) -> None:
    old = validate_project_plan(plan_document())
    changed_doc = plan_document(title="Task one")
    changed_doc["tasks"] = [{**changed_doc["tasks"][0], "acceptance_criteria": ["safe", "works"]}]
    diff = semantic_plan_diff(old, validate_project_plan(changed_doc))
    assert all(item.kind == "UNCHANGED" for item in diff.all_items)
    changed_doc["tasks"] = [{**changed_doc["tasks"][0], "title": "Changed"}]
    assert any(item.kind == "MODIFIED" and item.subject == "task" for item in semantic_plan_diff(old, validate_project_plan(changed_doc)).all_items)


def test_orphan_plan_version_remains_visible_when_pointer_activation_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, _ = make_store(tmp_path)
    candidate = validate_project_plan(plan_document(version=2, supersedes=1))
    monkeypatch.setattr(store, "_activate_pointer", lambda plan: (_ for _ in ()).throw(OSError("simulated crash")))
    with pytest.raises(OSError):
        store.apply_update(candidate)
    assert store.current_plan().plan_version == "1"
    assert tuple(item.plan_version for item in store.plan_versions()) == ("1", "2")


def test_event_log_decisions_inbox_risks_debt_and_checkpoint_are_bounded_and_append_only(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    first = store.append_event("TASK_STARTED", "Started t1", task_id="t1", plan_version="1")
    decision = store.add_decision(title="Storage", decision="SQLite", reason="Local bounded app")
    replacement = store.add_decision(title="Storage", decision="SQLite", reason="Still local", supersedes_decision_id=decision.decision_id)
    inbox = store.add_inbox(title="Export", description="Consider CSV later")
    store.update_inbox(inbox.inbox_id, "later")
    risk = store.add_risk(title="Scope", description="Scope can expand", severity="high")
    store.resolve_risk(risk.risk_id)
    debt = store.add_debt(title="Refactor", description="Review later")
    store.resolve_debt(debt.debt_id, "planned")
    checkpoint = store.create_checkpoint(label="After memory", plan_version="1", git_head="a" * 40, completed_task_ids=(), current_task_id="t1")
    state = store.read_state()
    assert next(event for event in state.events if event.event_type == "TASK_STARTED").event_id == first.event_id
    assert state.events[-1].event_type == "CHECKPOINT_CREATED"
    assert state.decisions[0].status == "superseded" and state.decisions[-1].decision_id == replacement.decision_id
    assert checkpoint.git_head == "a" * 40
    assert "SQLite" in store.memory_path.read_text(encoding="utf-8")


def test_next_action_health_status_and_bounded_handoff(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    brief = ProjectBrief("Memory Project", "goal", "description", "tool")
    project = new_project_record(project_id="memory-project", display_name="Memory Project", repo_alias="memory-project", local_repo_path=tmp_path / "repo", github_repo=None, brief=brief)
    project = type(project)(**{**project.__dict__, "plan_imported": True, "plan_version": "1", "total_tasks": 1, "current_milestone": "Foundation", "current_task": "t1"})
    state = store.read_state(); plan = store.current_plan()
    assert resolve_next_action(project, plan, state).code == "CONTINUE_TASK"
    assert project_health(state, plan) == "OK"
    assert "plan v1" in project_status_sentence(project, plan, state)
    prompt = build_handoff_prompt(project, plan, state, mode="NEW_CHAT_PROJECT_HANDOFF", git_head="b" * 40)
    assert "memory-project" in prompt and "C:/" not in prompt and "\\" not in prompt and "Send" not in prompt


def test_catalog_import_uses_memory_history_and_review_status(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"; catalog = ProjectCatalog(runtime)
    brief = ProjectBrief("Memory Project", "goal", "description", "tool")
    project = new_project_record(project_id="memory-project", display_name="Memory Project", repo_alias="memory-project", local_repo_path=tmp_path / "repo", github_repo=None, brief=brief); catalog.upsert(project)
    first = tmp_path / "v1.json"; first.write_text(json.dumps(plan_document()), encoding="utf-8")
    updated, imported = catalog.import_plan(project.project_id, first)
    assert updated.plan_version == "1" and imported.created_at
    second_doc = plan_document(version=2, supersedes=1); second_doc["tasks"][0]["status"] = "review"
    second = tmp_path / "v2.json"; second.write_text(json.dumps(second_doc), encoding="utf-8")
    updated, imported = catalog.import_plan(project.project_id, second)
    assert updated.plan_version == "2" and imported.tasks[0].status == "review"
    assert tuple(item.plan_version for item in ProjectMemoryStore(runtime, project.project_id).plan_versions()) == ("1", "2")

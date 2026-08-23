from __future__ import annotations

import json
from pathlib import Path

import pytest

from bdb_vnext.project_catalog import (
    PROJECT_PLAN_SCHEMA,
    ProjectBrief,
    ProjectCatalog,
    ProjectCatalogError,
    ProjectMilestone,
    ProjectPlan,
    ProjectTask,
    new_project_record,
    validate_project_plan,
)
from bdb_vnext.project_launch import ProjectLaunchQueueAdapter
from bdb_vnext.project_workflow import (
    CommandResult,
    ProjectWorkflow,
    ProjectWorkflowError,
    build_continue_prompt,
    build_plan_prompt,
)


def brief() -> ProjectBrief:
    return ProjectBrief(
        "Demo Project",
        "Build a bounded tool",
        "A small project used by tests.",
        "web",
        ("Python", "HTML"),
        ("one feature", "another feature"),
        ("no secrets in prompts",),
    )


def project(tmp_path: Path):
    return new_project_record(
        project_id="project-demo-1",
        display_name="Demo Project",
        repo_alias="demo-project",
        local_repo_path=tmp_path / "repo",
        github_repo="owner/demo-project",
        brief=brief(),
    )


def plan_document(project_id: str = "project-demo-1") -> dict[str, object]:
    return {
        "schema": PROJECT_PLAN_SCHEMA,
        "project_id": project_id,
        "project_name": "Demo Project",
        "plan_version": "1.0",
        "milestones": [{"id": "m1", "title": "First", "description": "First milestone", "status": "active"}],
        "tasks": [{"id": "t1", "milestone_id": "m1", "title": "Task one", "description": "Do one thing", "status": "active", "dependencies": [], "acceptance_criteria": ["works"]}],
        "current_task_id": "t1",
    }


def test_catalog_persists_and_reopens_without_legacy_state(tmp_path: Path) -> None:
    catalog = ProjectCatalog(tmp_path / "runtime")
    record = project(tmp_path)
    catalog.upsert(record)
    reopened = ProjectCatalog(tmp_path / "runtime")
    assert reopened.get(record.project_id) == record
    assert (tmp_path / "runtime" / "control" / "project-catalog.json").is_file()


def test_plan_import_validates_identity_dependencies_and_progress(tmp_path: Path) -> None:
    catalog = ProjectCatalog(tmp_path / "runtime")
    record = project(tmp_path)
    catalog.upsert(record)
    plan_path = tmp_path / "project-plan.json"
    plan_path.write_text(json.dumps(plan_document()), encoding="utf-8")
    updated, imported = catalog.import_plan(record.project_id, plan_path)
    assert imported.current_task_id == "t1"
    assert updated.plan_imported is True
    assert updated.total_tasks == 1
    assert updated.completed_tasks == 0
    assert updated.current_milestone == "First"
    with pytest.raises(ProjectCatalogError) as mismatch:
        catalog.import_plan(record.project_id, tmp_path / "wrong.json")
    assert mismatch.value.code == "plan_path_invalid"
    with pytest.raises(ProjectCatalogError) as wrong_id:
        validate_project_plan(plan_document("other-project"), expected_project_id=record.project_id)
    assert wrong_id.value.code == "plan_project_mismatch"


def test_plan_rejects_circular_dependencies() -> None:
    document = plan_document()
    document["tasks"] = [
        {"id": "t1", "milestone_id": "m1", "title": "One", "description": "First", "status": "active", "dependencies": ["t2"], "acceptance_criteria": []},
        {"id": "t2", "milestone_id": "m1", "title": "Two", "description": "Second", "status": "pending", "dependencies": ["t1"], "acceptance_criteria": []},
    ]
    with pytest.raises(ProjectCatalogError) as error:
        validate_project_plan(document)
    assert error.value.code == "plan_dependency_cycle"


def test_prompts_are_bounded_and_exclude_absolute_local_path() -> None:
    record = project(Path("C:/private-user-repo"))
    record = type(record)(**{**record.__dict__, "plan_imported": True, "plan_version": "1.0", "total_tasks": 4, "completed_tasks": 2, "current_milestone": "M2", "current_task": "T3"})
    plan_prompt = build_plan_prompt(record)
    continue_prompt = build_continue_prompt(record)
    assert "C:/private-user-repo" not in plan_prompt
    assert "project-demo-1" in plan_prompt
    assert "planning directive" in plan_prompt
    assert "prompt dla ChatGPT Work" not in plan_prompt
    assert "Plan version: 1.0" in continue_prompt
    assert "Postęp: 2/4" in continue_prompt
    assert "Send" not in continue_prompt


def test_queue_is_atomic_bounded_and_never_auto_sends(tmp_path: Path) -> None:
    queue = ProjectLaunchQueueAdapter(tmp_path / "project-launch-queue.json")
    launch = queue.enqueue(repo_alias="demo-project", prompt="bounded prompt")
    document = json.loads((tmp_path / "project-launch-queue.json").read_text(encoding="utf-8"))
    assert document["pending"]["launch_id"] == launch.launch_id
    assert document["pending"]["auto_send"] is False


class FakeRunner:
    def run(self, args, *, cwd=None, timeout_seconds=120.0):
        return CommandResult(tuple(str(item) for item in args), 0, "", "")


class FakeGitHub:
    def create_private_repository(self, *, local_repo: Path, repo_name: str) -> str:
        return f"owner/{repo_name}"


class FailingGitHub:
    def create_private_repository(self, *, local_repo: Path, repo_name: str) -> str:
        raise ProjectWorkflowError("github_auth_required", "login required")


def test_github_adapter_is_mockable_and_registers_only_after_success(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    workflow = ProjectWorkflow(root, command_runner=FakeRunner(), github=FakeGitHub())
    result = workflow.create_new(display_name="Demo", repo_alias="demo", projects_root=projects_root, brief=brief(), github_name="demo")
    assert result.ok is True
    assert result.project is not None
    assert workflow.catalog.get(result.project.project_id) is not None
    assert (projects_root / "demo" / ".bdb" / "project-brief.md").is_file()


def test_github_failure_is_typed_fail_closed_and_not_registered(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    workflow = ProjectWorkflow(tmp_path / "runtime", command_runner=FakeRunner(), github=FailingGitHub())
    result = workflow.create_new(display_name="Demo", repo_alias="demo", projects_root=projects_root, brief=brief(), github_name="demo")
    assert result.ok is False
    assert result.error_code == "github_auth_required"
    assert workflow.catalog.read() == ()
    assert (projects_root / "demo" / "README.md").is_file()


def test_project_workflow_does_not_import_legacy_operator_or_bridge() -> None:
    for path in (Path("bdb_gui/project_center.py"), Path("bdb_vnext/project_workflow.py"), Path("bdb_vnext/project_catalog.py")):
        source = path.read_text(encoding="utf-8")
        assert "bdb_operator" not in source
        assert "from bdb_bridge" not in source


def test_tracked_start_launcher_is_non_installing_and_non_admin() -> None:
    content = Path("Start-BDB.ps1").read_text(encoding="utf-8")
    assert "pip install" not in content.lower()
    assert "Start-Process -Verb RunAs" not in content
    assert "bdb_gui.app" in content


def test_project_center_empty_start_and_advanced_smoke(tmp_path: Path) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from bdb_gui.project_center import PROJECT_PAGE_NAMES, ProjectCenterWindow

    app = QApplication.instance() or QApplication(["cc3-project-test"])
    window = ProjectCenterWindow(runtime_root=tmp_path / "runtime")
    window.start_bootstrap()
    report = window.smoke_report()
    assert tuple(report["page_names"]) == PROJECT_PAGE_NAMES
    assert report["project_count"] == 0
    assert report["auto_send"] is False
    assert report["mutation_operations_invoked"] == 0
    window.select_page("Advanced")
    window._open_advanced()
    assert window._advanced is not None
    assert window._advanced.smoke_report()["semantic_source"] == "bdb_vnext.control_center_query"
    window.close(); window._advanced.close(); app.processEvents()

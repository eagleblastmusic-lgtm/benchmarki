"""Project-centric vNext workflow services.

Side effects are explicit and bounded: repository creation is behind a typed
Git/GitHub adapter, while prompt insertion only enqueues a no-auto-send launch
for the existing Browser/Native transport.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from .project_catalog import ProjectBrief, ProjectCatalog, ProjectCatalogError, ProjectPlan, ProjectRecord, new_project_record
from .project_launch import ProjectLaunch, ProjectLaunchQueueAdapter, ProjectLaunchQueueError


_ALIAS_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


@dataclass(frozen=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def run(self, args: Sequence[str], *, cwd: str | Path | None = None, timeout_seconds: float = 120.0) -> CommandResult: ...


class GitHubRepositoryAdapter(Protocol):
    def create_private_repository(self, *, local_repo: Path, repo_name: str) -> str: ...


class SubprocessCommandRunner:
    def run(self, args: Sequence[str], *, cwd: str | Path | None = None, timeout_seconds: float = 120.0) -> CommandResult:
        options: dict[str, object] = {}
        if os.name == "nt":
            options["creationflags"] = 0x08000000
        result = subprocess.run([str(item) for item in args], cwd=str(cwd) if cwd is not None else None, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace", shell=False, check=False, timeout=timeout_seconds, **options)
        return CommandResult(tuple(str(item) for item in args), result.returncode, result.stdout, result.stderr)


class GhRepositoryAdapter:
    def __init__(self, runner: CommandRunner | None = None) -> None:
        self.runner = runner or SubprocessCommandRunner()

    def create_private_repository(self, *, local_repo: Path, repo_name: str) -> str:
        version = self.runner.run(("gh", "--version"), timeout_seconds=30)
        if version.returncode != 0:
            raise ProjectWorkflowError("github_cli_unavailable", "GitHub CLI is unavailable")
        auth = self.runner.run(("gh", "auth", "status"), timeout_seconds=30)
        if auth.returncode != 0:
            raise ProjectWorkflowError("github_auth_required", "GitHub CLI is not authenticated")
        result = self.runner.run(("gh", "repo", "create", repo_name, "--private", "--source", str(local_repo), "--remote", "origin", "--push"), cwd=local_repo, timeout_seconds=300)
        if result.returncode != 0:
            raise ProjectWorkflowError("github_create_failed", result.stderr.strip() or "GitHub repository creation failed")
        for line in (result.stdout + "\n" + result.stderr).splitlines():
            candidate = line.strip().rstrip("/")
            if candidate.startswith("https://github.com/"):
                return candidate.removeprefix("https://github.com/").removesuffix(".git")
        remote = self.runner.run(("git", "remote", "get-url", "origin"), cwd=local_repo, timeout_seconds=30)
        if remote.returncode == 0 and remote.stdout.strip():
            value = remote.stdout.strip().replace("https://github.com/", "").replace(".git", "")
            return value
        raise ProjectWorkflowError("github_identity_missing", "GitHub repository identity was not returned")


class ProjectWorkflowError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ProjectCreationResult:
    ok: bool
    project: ProjectRecord | None = None
    error_code: str | None = None
    error_message: str | None = None
    local_repo_path: str | None = None
    github_repo: str | None = None


def brief_markdown(brief: ProjectBrief, *, github_repo: str | None = None, local_repo_identity: str | None = None) -> str:
    lines = [f"# {brief.name}", "", "## Cel", brief.goal, "", "## Opis", brief.description, "", "## Typ", brief.project_type]
    if brief.technologies:
        lines.extend(["", "## Preferowane technologie", *[f"- {item}" for item in brief.technologies]])
    if brief.features:
        lines.extend(["", "## Najważniejsze funkcje", *[f"- {item}" for item in brief.features]])
    if brief.constraints:
        lines.extend(["", "## Ograniczenia", *[f"- {item}" for item in brief.constraints]])
    if github_repo:
        lines.extend(["", "## GitHub", github_repo])
    if local_repo_identity:
        lines.extend(["", "## Repozytorium lokalne", local_repo_identity])
    return "\n".join(lines) + "\n"


def build_plan_prompt(project: ProjectRecord) -> str:
    brief = project.brief
    # Deliberately exclude absolute local paths and credentials from model text.
    lines = [
        "Zapoznaj się z poniższym bounded project briefem.",
        "Nie implementuj jeszcze kodu.",
        "Przeanalizuj projekt, wskaż brakujące decyzje i zaprojektuj architekturę.",
        "Następnie przygotuj szczegółowy prompt dla ChatGPT Work, który utworzy Project Plan w formacie bdb-project-plan-v1.",
        "",
        f"Projekt: {project.display_name}",
        f"Project ID: {project.project_id}",
        f"Repo alias: {project.repo_alias}",
        f"GitHub repo: {project.github_repo or 'jeszcze nie utworzone'}",
        f"Cel: {brief.goal}",
        f"Opis: {brief.description}",
        f"Typ: {brief.project_type}",
    ]
    if brief.technologies:
        lines.append("Technologie: " + ", ".join(brief.technologies))
    if brief.features:
        lines.append("Funkcje: " + "; ".join(brief.features))
    if brief.constraints:
        lines.append("Ograniczenia: " + "; ".join(brief.constraints))
    return "\n".join(lines)


def project_plan_markdown(plan: ProjectPlan) -> str:
    """Render the canonical JSON plan for human review; never parse Markdown."""

    lines = [f"# {plan.project_name}", "", f"Plan version: {plan.plan_version}", "", "## Milestones"]
    for milestone in plan.milestones:
        lines.extend([f"### {milestone.title} ({milestone.status})", milestone.description, ""])
        for task in (item for item in plan.tasks if item.milestone_id == milestone.milestone_id):
            lines.extend([f"- **{task.task_id} — {task.title}**: {task.status}", f"  {task.description}"])
    lines.extend(["", f"Current task: {plan.current_task_id or 'none'}", ""])
    return "\n".join(lines)


def build_start_prompt(project: ProjectRecord) -> str:
    return _build_execution_prompt(project, "Rozpoczynamy projekt")


def build_continue_prompt(project: ProjectRecord) -> str:
    return _build_execution_prompt(project, "Kontynuujemy projekt")


def _build_execution_prompt(project: ProjectRecord, heading: str) -> str:
    if not project.plan_imported:
        raise ProjectWorkflowError("project_plan_required", "Project Plan must be imported before Start/Continue")
    completed = project.completed_tasks
    total = project.total_tasks
    lines = [
        heading + f" {project.display_name}.",
        f"Project ID: {project.project_id}",
        f"Repo alias: {project.repo_alias}",
        f"GitHub repo: {project.github_repo or 'not configured'}",
        f"Plan version: {project.plan_version}",
        f"Postęp: {completed}/{total}",
        f"Aktualny milestone: {project.current_milestone or 'nieustalony'}",
        f"Aktualne zadanie: {project.current_task or 'nieustalone'}",
        "Najpierw pobierz aktualny bounded context przez BDB.",
        "Nie wykonuj ponownie ukończonych zadań.",
        "Sprawdź zgodność HEAD/plan/current task, a następnie pracuj nad aktualnym zadaniem.",
    ]
    return "\n".join(lines)


class ProjectWorkflow:
    """Canonical catalog workflow used by the project-centric GUI."""

    def __init__(self, runtime_root: str | Path, *, catalog: ProjectCatalog | None = None, command_runner: CommandRunner | None = None, github: GitHubRepositoryAdapter | None = None, queue: ProjectLaunchQueueAdapter | None = None) -> None:
        self.catalog = catalog or ProjectCatalog(runtime_root)
        self.runner = command_runner or SubprocessCommandRunner()
        self.github = github or GhRepositoryAdapter(self.runner)
        self.queue = queue or ProjectLaunchQueueAdapter()

    def register_existing(self, *, display_name: str, repo_alias: str, local_repo_path: str | Path, brief: ProjectBrief, github_repo: str | None = None) -> ProjectRecord:
        source = Path(local_repo_path).expanduser().absolute()
        if source.is_symlink() or not source.is_dir() or not source.joinpath(".git").exists():
            raise ProjectWorkflowError("repository_invalid", "existing project must be a Git checkout")
        if github_repo is None:
            remote = self.runner.run(("git", "remote", "get-url", "origin"), cwd=source, timeout_seconds=30)
            if remote.returncode == 0:
                candidate = remote.stdout.strip().replace("https://github.com/", "").replace(".git", "")
                if "/" in candidate:
                    github_repo = candidate
        record = new_project_record(project_id=None, display_name=display_name, repo_alias=repo_alias, local_repo_path=source, github_repo=github_repo, brief=brief)
        self.catalog.upsert(record)
        return record

    def create_new(self, *, display_name: str, repo_alias: str, projects_root: str | Path, brief: ProjectBrief, github_name: str) -> ProjectCreationResult:
        if _ALIAS_RE.fullmatch(repo_alias.strip().lower()) is None:
            return ProjectCreationResult(False, error_code="repo_alias_invalid", error_message="repo_alias is unsafe")
        if _REPO_NAME_RE.fullmatch(github_name.strip()) is None:
            return ProjectCreationResult(False, error_code="github_name_invalid", error_message="GitHub repository name is unsafe")
        parent = Path(projects_root).expanduser().absolute()
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return ProjectCreationResult(False, error_code="projects_root_invalid", error_message=str(exc))
        if parent.is_symlink() or not parent.is_dir():
            return ProjectCreationResult(False, error_code="projects_root_invalid", error_message="projects root must be a regular directory")
        source = (parent / repo_alias.strip().lower()).absolute()
        try:
            source.relative_to(parent)
        except ValueError:
            return ProjectCreationResult(False, error_code="repository_path_escape", error_message="project path escapes projects root")
        if source.exists():
            return ProjectCreationResult(False, error_code="repository_exists", error_message="project directory already exists")
        try:
            source.mkdir()
            (source / ".bdb").mkdir()
            (source / "README.md").write_text(f"# {display_name.strip()}\n\nCreated by BDB vNext Project Center.\n", encoding="utf-8", newline="\n")
            (source / ".gitignore").write_text(".venv/\nnode_modules/\n.env\n", encoding="utf-8", newline="\n")
            (source / ".bdb" / "project-brief.md").write_text(brief_markdown(brief), encoding="utf-8", newline="\n")
            self._run(("git", "init", "--initial-branch=main"), cwd=source)
            self._run(("git", "config", "user.name", "Bartosz Dev Bridge"), cwd=source)
            self._run(("git", "config", "user.email", "bdb@localhost.invalid"), cwd=source)
            self._run(("git", "add", "--", "README.md", ".gitignore", ".bdb/project-brief.md"), cwd=source)
            self._run(("git", "commit", "-m", "chore: initialize project"), cwd=source)
            github_repo = self.github.create_private_repository(local_repo=source, repo_name=github_name.strip())
            record = new_project_record(project_id=None, display_name=display_name, repo_alias=repo_alias, local_repo_path=source, github_repo=github_repo, brief=brief)
            self.catalog.upsert(record)
            return ProjectCreationResult(True, record, local_repo_path=str(source), github_repo=github_repo)
        except ProjectWorkflowError as exc:
            return ProjectCreationResult(False, error_code=exc.code, error_message=str(exc), local_repo_path=str(source))
        except (OSError, ValueError) as exc:
            return ProjectCreationResult(False, error_code="project_creation_failed", error_message=str(exc), local_repo_path=str(source))

    def import_plan(self, project_id: str, plan_path: str | Path) -> tuple[ProjectRecord, ProjectPlan]:
        try:
            return self.catalog.import_plan(project_id, plan_path)
        except ProjectCatalogError as exc:
            raise ProjectWorkflowError(exc.code, str(exc)) from exc

    def queue_plan_prompt(self, project_id: str) -> ProjectLaunch:
        return self._queue(project_id, build_plan_prompt)

    def queue_start_prompt(self, project_id: str) -> ProjectLaunch:
        return self._queue(project_id, build_start_prompt)

    def queue_continue_prompt(self, project_id: str) -> ProjectLaunch:
        return self._queue(project_id, build_continue_prompt)

    def _queue(self, project_id: str, builder: Callable[[ProjectRecord], str]) -> ProjectLaunch:
        project = self.catalog.get(project_id)
        if project is None:
            raise ProjectWorkflowError("project_not_found", "project is not in the canonical catalog")
        try:
            launch = self.queue.enqueue(repo_alias=project.repo_alias, prompt=builder(project), ttl_minutes=10)
        except ProjectLaunchQueueError as exc:
            raise ProjectWorkflowError(exc.code, str(exc)) from exc
        updated = ProjectRecord(**{**project.__dict__, "last_launch_id": launch.launch_id})
        self.catalog.upsert(updated)
        return launch

    def _run(self, args: Sequence[str], *, cwd: Path) -> CommandResult:
        result = self.runner.run(args, cwd=cwd, timeout_seconds=120)
        if result.returncode != 0:
            raise ProjectWorkflowError("git_command_failed", result.stderr.strip() or "Git command failed")
        return result


__all__ = [
    "CommandResult",
    "GhRepositoryAdapter",
    "GitHubRepositoryAdapter",
    "ProjectCreationResult",
    "ProjectWorkflow",
    "ProjectWorkflowError",
    "SubprocessCommandRunner",
    "brief_markdown",
    "build_continue_prompt",
    "build_plan_prompt",
    "build_start_prompt",
    "project_plan_markdown",
]

"""Project-centric vNext workflow services.

Side effects are explicit and bounded: repository creation is behind a typed
Git/GitHub adapter, while prompt insertion only enqueues a no-auto-send launch
for the existing Browser/Native transport.
"""

from __future__ import annotations

import os
import json
import re
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from .project_catalog import PROJECT_PLAN_MAX_BYTES, ProjectBrief, ProjectCatalog, ProjectCatalogError, ProjectPlan, ProjectRecord, new_project_record, validate_project_plan
from .project_launch import ProjectLaunch, ProjectLaunchQueueAdapter, ProjectLaunchQueueError
from .project_memory import HANDOFF_MODES, PlanUpdatePreview, ProjectMemoryError, ProjectMemoryState, ProjectMemoryStore, available_project_tasks, build_handoff_prompt
from .project_execution import ProjectExecutionBinding, ProjectExecutionCoordinator, ProjectExecutionError, ProjectLaunchOutboxRecord
from .work_planning import WorkPlanningPrompt, WorkPlanningPromptBuilder, WorkPlanningPromptError


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
    if brief.pinned_files:
        lines.extend(["", "## Ważne pliki", *[f"- {item}" for item in brief.pinned_files]])
    if brief.environment_hints:
        lines.extend(["", "## Środowisko (wskazówki)", *[f"- {item}" for item in brief.environment_hints]])
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
        "Przygotuj merytoryczną, bounded planning directive dla Work: co należy zaplanować, poprawić i zachować. Nie twórz jeszcze project-plan.json ani kolejnego promptu.",
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


def build_start_prompt(project: ProjectRecord, *, plan: ProjectPlan | None = None, state: ProjectMemoryState | None = None, git_head: str | None = None, binding: ProjectExecutionBinding | None = None) -> str:
    return _build_execution_prompt(project, "Rozpoczynamy projekt", plan=plan, state=state, git_head=git_head, binding=binding)


def build_continue_prompt(project: ProjectRecord, *, plan: ProjectPlan | None = None, state: ProjectMemoryState | None = None, git_head: str | None = None, binding: ProjectExecutionBinding | None = None) -> str:
    return _build_execution_prompt(project, "Kontynuujemy projekt", plan=plan, state=state, git_head=git_head, binding=binding)


def _build_execution_prompt(project: ProjectRecord, heading: str, *, plan: ProjectPlan | None = None, state: ProjectMemoryState | None = None, git_head: str | None = None, binding: ProjectExecutionBinding | None = None) -> str:
    if not project.plan_imported:
        raise ProjectWorkflowError("project_plan_required", "Project Plan must be imported before Start/Continue")
    current_plan = plan
    current_task = None
    available_task_ids: tuple[str, ...] = ()
    execution = state.execution if state is not None and isinstance(state.execution, Mapping) else {}
    milestone_run = execution.get("active_milestone_run") if isinstance(execution.get("active_milestone_run"), Mapping) else None
    if current_plan is not None:
        current_task_id = state.execution.get("current_task_id") if state is not None and isinstance(state.execution, Mapping) else None
        current_task = next((item for item in current_plan.tasks if item.task_id == current_task_id), None) if current_task_id else None
        if current_task is None:
            available = available_project_tasks(
                current_plan,
                state or ProjectMemoryState(project.project_id),
                str(milestone_run.get("milestone_id")) if milestone_run and milestone_run.get("status") in {"running", "review", "blocked"} else None,
            )
            available_task_ids = tuple(item.task_id for item in available)
            if len(available) == 1:
                current_task = available[0]
            elif not available and (state is None or state.execution.get("task_statuses", {}).get(current_plan.current_task_id, current_plan.current_task.status if current_plan.current_task else "pending") not in {"completed", "skipped"}):
                current_task = current_plan.current_task
    task_statuses = state.execution.get("task_statuses", {}) if state is not None and isinstance(state.execution, Mapping) else {}
    completed = sum(str(task_statuses.get(task.task_id, task.status)) in {"completed", "skipped"} for task in current_plan.tasks) if current_plan else project.completed_tasks
    total = len(current_plan.tasks) if current_plan else project.total_tasks
    lines = [
        heading + f" {project.display_name}.",
        f"Project ID: {project.project_id}",
        f"Repo alias: {project.repo_alias}",
        f"GitHub repo: {project.github_repo or 'not configured'}",
        f"Plan version: {current_plan.plan_version if current_plan else project.plan_version}",
        f"Postęp: {completed}/{total}",
        f"Aktualny milestone: {current_task.milestone_id if current_task else project.current_milestone or 'nieustalony'}",
        f"Aktualne zadanie: {current_task.task_id if current_task else project.current_task or 'nieustalone'}",
        f"Cel zadania: {current_task.description if current_task else 'potwierdź canonical task przez BDB'}",
        f"Zależności: {', '.join(current_task.dependencies) if current_task and current_task.dependencies else 'brak'}",
        "Acceptance criteria: " + ("; ".join(current_task.acceptance_criteria) if current_task and current_task.acceptance_criteria else "brak jawnych kryteriów"),
        f"Repo HEAD przed wykonaniem: {git_head or 'unknown'}",
        f"Execution binding: {binding.execution_binding_id if binding else 'prepared by BDB before execution'}",
        f"Task ID (copy exactly): {binding.task_id if binding else (current_task.task_id if current_task else 'prepared by BDB before execution')}",
        f"Correlation ID (copy exactly): {binding.correlation_id if binding else 'prepared by BDB before execution'}",
        f"Command ID (copy exactly): {binding.command_id if binding else 'prepared by BDB before execution'}",
        f"Gotowe zadania do wyboru: {', '.join(available_task_ids) if available_task_ids else 'brak lub jednoznaczne zadanie'}",
        "Najpierw pobierz aktualny bounded context przez BDB.",
        "Nie wykonuj ponownie ukończonych zadań.",
        "Sprawdź zgodność HEAD/plan/current task, a następnie pracuj nad aktualnym zadaniem.",
        "Walidację wykonuj najpierw lokalnie: uruchom tylko szybkie, wystarczające testy/typecheck/build/cargo check zgodne z repozytorium.",
        "GitHub Actions pozostaw jako końcową niezależną walidację albo fallback, gdy lokalny toolchain nie istnieje; nie twórz workflow/PR tylko dla pojedynczego testu.",
        "Przed powtórzeniem kosztownej kontroli oceń, czy ostatnia zmiana mogła unieważnić poprzedni PASS.",
        "Nie ma globalnego limitu czasu taska, liczby iteracji ani milestone AUTO; timeouty dotyczą wyłącznie pojedynczych operacji technicznych.",
        "Jeśli czekasz na aktywne zewnętrzne CI/build, opisz WAITING_EXTERNAL i odnośnik zamiast wykonywać bezproduktywny polling.",
        "Nie wykonuj więcej niż trzech kolejnych status polls bez zmiany; stosuj backoff i przejdź do WAITING_EXTERNAL, jeśli zewnętrzna operacja nadal trwa.",
        "Po zatrzymaniu lub braku postępu kontynuuj ten sam project_id, plan_version, task_id, execution_binding_id i milestone_run_id; nie twórz nowego zadania.",
        "Na końcu zwróć dokładnie jeden blok JSON (Nie YAML i bez dodatkowych BDB_SUBMISSION bloków) zgodny ze schematem bdb-project-execution-submission-v1.",
        "Skopiuj project_id, plan_version, task_id, execution_binding_id, correlation_id, command_id, repo_alias i head_before dokładnie z tego promptu/bindingu; nie wymyślaj ich.",
        "Wynik JSON musi zawierać: schema, project_id, plan_version, task_id, execution_binding_id, correlation_id, command_id, repo_alias, head_before, head_after, execution_status, validation_status, promotion_status, result_summary, evidence_refs i criteria; failure_code oraz canonical_refs dodaj tylko gdy istnieją.",
        "Nie umieszczaj w tym wyniku komentarzy ani markdown poza jednym fenced code blockiem json. Nie wysyłaj formularza za pomocą BDB.",
    ]
    if milestone_run and milestone_run.get("status") == "running":
        lines.extend([
            "Tryb: MILESTONE AUTO — wykonuj wyłącznie bieżący milestone, sekwencyjnie i bez równoległości.",
            f"Milestone run ID: {milestone_run.get('milestone_run_id')}",
            f"Milestone ID: {milestone_run.get('milestone_id')}",
            "Po machine acceptance przejdź do następnego runnable tasku tego samego milestone'u; po jego ukończeniu zwróć MILESTONE_COMPLETED i zatrzymaj się. Nie uruchamiaj następnego milestone'u.",
        ])
    if state is not None:
        active_decisions = [item.title + ": " + item.decision for item in state.decisions if item.status == "active"]
        attention = [item.type + ": " + item.title for item in state.attention if item.status == "open"]
        lines.extend(["Aktywne decyzje: " + ("; ".join(active_decisions) if active_decisions else "brak"), "Otwarte uwagi: " + ("; ".join(attention) if attention else "brak")])
    return "\n".join(lines)


class ProjectWorkflow:
    """Canonical catalog workflow used by the project-centric GUI."""

    def __init__(self, runtime_root: str | Path, *, catalog: ProjectCatalog | None = None, command_runner: CommandRunner | None = None, github: GitHubRepositoryAdapter | None = None, queue: ProjectLaunchQueueAdapter | None = None, work_planning_builder: WorkPlanningPromptBuilder | None = None) -> None:
        self.catalog = catalog or ProjectCatalog(runtime_root)
        self.runner = command_runner or SubprocessCommandRunner()
        self.github = github or GhRepositoryAdapter(self.runner)
        self.queue = queue or ProjectLaunchQueueAdapter(self.catalog.runtime_root / "control" / "project-launch-queue.json")
        self.execution = ProjectExecutionCoordinator(self.catalog.runtime_root, catalog=self.catalog)
        self.work_planning_builder = work_planning_builder or WorkPlanningPromptBuilder()

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
        self._ensure_project_memory(record)
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
            self._ensure_project_memory(record)
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

    def memory(self, project_id: str) -> ProjectMemoryStore:
        if self.catalog.get(project_id) is None:
            raise ProjectWorkflowError("project_not_found", "project is not in the canonical catalog")
        return ProjectMemoryStore(self.catalog.runtime_root, project_id)

    def work_planning_state(self, project_id: str) -> dict[str, str | None]:
        project = self.catalog.get(project_id)
        if project is None:
            raise ProjectWorkflowError("project_not_found", "project is not in the canonical catalog")
        current = self.memory(project_id).current_plan() if project.plan_imported else None
        if project.plan_imported and current is None:
            raise ProjectWorkflowError("planning_current_plan_unavailable", "active project plan could not be read from Project Memory")
        expected, supersedes = self.work_planning_builder.expected_versions(current)
        return {"mode": self.work_planning_builder.mode_for(current), "current_plan_version": current.plan_version if current else None, "expected_plan_version": expected, "supersedes_version": supersedes}

    def build_work_prompt(self, project_id: str, planning_directive: str) -> WorkPlanningPrompt:
        project = self.catalog.get(project_id)
        if project is None:
            raise ProjectWorkflowError("project_not_found", "project is not in the canonical catalog")
        current = self.memory(project_id).current_plan() if project.plan_imported else None
        if project.plan_imported and current is None:
            raise ProjectWorkflowError("planning_current_plan_unavailable", "active project plan could not be read from Project Memory")
        try:
            return self.work_planning_builder.build(mode=self.work_planning_builder.mode_for(current), project=project, brief=project.brief, current_plan=current, planning_directive=planning_directive)
        except WorkPlanningPromptError as exc:
            raise ProjectWorkflowError(exc.code, str(exc)) from exc

    def read_memory(self, project_id: str) -> ProjectMemoryState:
        return self.memory(project_id).read_state()

    def set_gate_status(self, project_id: str, gate_id: str, status: str) -> str:
        try:
            return self.memory(project_id).set_gate_status(gate_id, status)
        except ProjectMemoryError as exc:
            raise ProjectWorkflowError(exc.code, str(exc)) from exc

    def pass_gate(self, project_id: str, gate_id: str) -> str:
        return self.set_gate_status(project_id, gate_id, "passed")

    def reopen_gate(self, project_id: str, gate_id: str) -> str:
        return self.set_gate_status(project_id, gate_id, "pending")

    def set_open_question_status(self, project_id: str, question_id: str, status: str) -> str:
        try:
            return self.memory(project_id).set_open_question_status(question_id, status)
        except ProjectMemoryError as exc:
            raise ProjectWorkflowError(exc.code, str(exc)) from exc

    def resolve_open_question(self, project_id: str, question_id: str) -> str:
        return self.set_open_question_status(project_id, question_id, "resolved")

    def reopen_open_question(self, project_id: str, question_id: str) -> str:
        return self.set_open_question_status(project_id, question_id, "open")

    def preview_plan_update(self, project_id: str, plan_path: str | Path) -> PlanUpdatePreview:
        project = self.catalog.get(project_id)
        if project is None:
            raise ProjectWorkflowError("project_not_found", "project is not in the canonical catalog")
        try:
            plan = self._read_plan(plan_path, project_id)
            return self.memory(project_id).preview_update(plan)
        except ProjectWorkflowError:
            raise
        except (ProjectCatalogError, OSError) as exc:
            raise ProjectWorkflowError(getattr(exc, "code", "plan_path_invalid"), str(exc)) from exc

    def apply_plan_update(self, project_id: str, plan_path: str | Path, preview: PlanUpdatePreview | None = None) -> tuple[ProjectRecord, ProjectPlan]:
        if self.catalog.get(project_id) is None:
            raise ProjectWorkflowError("project_not_found", "project is not in the canonical catalog")
        candidate = self._read_plan(plan_path, project_id)
        current_preview = self.memory(project_id).preview_update(candidate)
        if preview is not None and (preview.project_id != project_id or preview.candidate_plan_digest != current_preview.candidate_plan_digest or preview.current_plan_digest != current_preview.current_plan_digest):
            raise ProjectWorkflowError("plan_preview_mismatch", "plan preview no longer matches canonical state")
        if not current_preview.accepted:
            raise ProjectWorkflowError(current_preview.reason_code or "plan_update_rejected", "plan update was not accepted")
        # Reuse the catalog's canonical import path so summary and immutable
        # plan activation cannot diverge.
        return self.import_plan(project_id, plan_path)

    def queue_handoff_prompt(self, project_id: str, mode: str, *, git_head: str | None = None) -> ProjectLaunch:
        if mode not in HANDOFF_MODES:
            raise ProjectWorkflowError("handoff_mode_invalid", "handoff mode is unsupported")
        project = self.catalog.get(project_id)
        if project is None:
            raise ProjectWorkflowError("project_not_found", "project is not in the canonical catalog")
        memory = self.memory(project_id)
        prompt = build_handoff_prompt(project, memory.current_plan(), memory.read_state(), mode=mode, git_head=git_head or self.current_repo_head(project))
        try:
            launch = self.queue.enqueue(repo_alias=project.repo_alias, prompt=prompt, ttl_minutes=10)
        except ProjectLaunchQueueError as exc:
            raise ProjectWorkflowError(exc.code, str(exc)) from exc
        event = memory.append_event("HANDOFF_CREATED", f"Utworzono handoff {mode}", correlation_id=launch.launch_id)
        def mark_cursor(state: ProjectMemoryState) -> tuple[ProjectMemoryState, None]:
            execution = dict(state.execution) if isinstance(state.execution, Mapping) else {}
            execution["last_handoff_event_id"] = event.event_id
            return replace(state, execution=execution), None
        memory.execution_transaction(mark_cursor)
        self.catalog.upsert(ProjectRecord(**{**project.__dict__, "last_launch_id": launch.launch_id}))
        return launch

    @staticmethod
    def _read_plan(plan_path: str | Path, project_id: str) -> ProjectPlan:
        source = Path(plan_path).expanduser().absolute()
        if source.is_symlink() or not source.is_file():
            raise ProjectWorkflowError("plan_path_invalid", "project-plan.json must be a regular file")
        payload = source.read_bytes()
        if len(payload) > PROJECT_PLAN_MAX_BYTES:
            raise ProjectWorkflowError("plan_too_large", "project plan exceeds its bound")
        try:
            document = json.loads(payload.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProjectWorkflowError("plan_json_invalid", "project plan is not valid JSON") from exc
        return validate_project_plan(document, expected_project_id=project_id)

    def queue_plan_prompt(self, project_id: str) -> ProjectLaunch:
        return self._queue(project_id, build_plan_prompt)

    def queue_start_prompt(self, project_id: str) -> ProjectLaunch:
        return self._queue_execution_prompt(project_id, "start")

    def queue_continue_prompt(self, project_id: str) -> ProjectLaunch:
        return self._queue_execution_prompt(project_id, "continue")

    def _ensure_auto_next_launch(self, project_id: str, *, completed_task_id: str | None = None) -> tuple[ProjectLaunch | None, str]:
        """Reconcile one canonical AUTO handoff without using Browser state as authority."""
        snapshot = self.execution.snapshot(project_id)
        auto = snapshot.get("milestone_auto") or {}
        state = self.memory(project_id).read_state()
        active_run = state.execution.get("active_milestone_run") if isinstance(state.execution, Mapping) else None
        if not isinstance(active_run, Mapping) or active_run.get("status") != "running" or auto.get("status") != "RUNNABLE":
            return None, "not_runnable"
        current_task_id = auto.get("current_task_id") or auto.get("next_task_id")
        if not isinstance(current_task_id, str) or not current_task_id:
            return None, "milestone_completed"
        if snapshot.get("current_task_id") not in (None, current_task_id):
            raise ProjectWorkflowError("execution_recovery_ambiguous", "AUTO current task disagrees with canonical execution cursor")
        if completed_task_id is not None:
            if snapshot.get("task_statuses", {}).get(completed_task_id) != "completed":
                return None, "completed_task_not_accepted"
            if current_task_id == completed_task_id:
                return None, "current_task_not_advanced"
        if snapshot.get("task_statuses", {}).get(current_task_id) in {"completed", "skipped"}:
            return None, "milestone_completed"
        if any(item.get("task_id") == current_task_id and item.get("result_status") not in {"STALE_RESULT"} for item in snapshot.get("attempts", [])):
            return None, "attempt_exists"

        pending = self.queue.peek()
        if pending is not None:
            if pending.project_id != project_id or pending.task_id != current_task_id:
                raise ProjectWorkflowError("queue_pending", "project launch queue contains a different canonical task")
            if pending.auto_send is not True or not pending.execution_binding_id:
                raise ProjectWorkflowError("launch_recovery_required", "current AUTO launch is not a canonical AUTO handoff")
            binding = self.execution.binding(project_id, pending.execution_binding_id)
            if binding.task_id != current_task_id or snapshot.get("current_binding_id") != binding.execution_binding_id:
                raise ProjectWorkflowError("execution_recovery_ambiguous", "pending launch does not match the current canonical binding")
            handoff = self.execution.launch_handoff(project_id, binding.execution_binding_id)
            if handoff is not None and handoff.get("status") == "SENT":
                return None, "already_sent"
            self.execution.mark_launch_handoff_pending(project_id, binding)
            return pending, "ready"

        binding_id = snapshot.get("current_binding_id")
        binding = self.execution.binding(project_id, binding_id) if binding_id else self.execution.current_task_binding(project_id, current_task_id)
        if binding is not None:
            if binding.task_id != current_task_id:
                raise ProjectWorkflowError("execution_recovery_ambiguous", "current binding does not match the AUTO task")
            if snapshot.get("current_binding_id") is None:
                try:
                    binding = self.execution.restore_current_binding(project_id, binding.execution_binding_id)
                except ProjectExecutionError as exc:
                    raise ProjectWorkflowError(exc.code, str(exc)) from exc
            handoff = self.execution.launch_handoff(project_id, binding.execution_binding_id)
            if handoff is not None and handoff.get("status") == "SENT":
                return None, "already_sent"
        launch = self._queue_execution_prompt(project_id, "continue", binding_override=binding)
        return launch, "ready"

    def submit_project_execution_result(self, result: Mapping[str, Any], *, conversation_id: str, launch_id: str) -> dict[str, Any]:
        """Record one Browser project result and, for AUTO, queue the next task."""
        from .project_execution import ProjectExecutionSubmission

        submission = ProjectExecutionSubmission.from_mapping(result)
        project = self.catalog.get(submission.project_id)
        if project is None:
            raise ProjectWorkflowError("project_not_found", "project is not in the canonical catalog")
        binding = self.execution.binding(submission.project_id, submission.execution_binding_id)
        if binding.launch_id != launch_id:
            raise ProjectWorkflowError("execution_launch_mismatch", "result launch does not match the canonical binding")
        if binding.conversation_id is not None and binding.conversation_id != conversation_id:
            raise ProjectWorkflowError("execution_conversation_mismatch", "result conversation does not match the canonical binding")
        replay_attempt = self.execution.existing_result(submission.project_id, submission.to_dict())
        if replay_attempt is not None:
            snapshot = self.execution.snapshot(submission.project_id)
            milestone = snapshot.get("milestone_auto") or {}
            next_launch, next_launch_status = (None, "not_runnable")
            if replay_attempt.result_status == "PASS":
                next_launch, next_launch_status = self._ensure_auto_next_launch(submission.project_id, completed_task_id=replay_attempt.task_id)
                snapshot = self.execution.snapshot(submission.project_id)
                milestone = snapshot.get("milestone_auto") or {}
            return {
                "schema": "bdb-project-execution-receipt-v1",
                "accepted": replay_attempt.result_status == "PASS",
                "replayed": True,
                "project_id": submission.project_id,
                "task_id": replay_attempt.task_id,
                "execution_binding_id": replay_attempt.execution_binding_id,
                "attempt_id": replay_attempt.attempt_id,
                "task_status": snapshot.get("task_statuses", {}).get(replay_attempt.task_id),
                "current_task_id": snapshot.get("current_task_id"),
                "milestone_run_id": milestone.get("milestone_run_id"),
                "milestone_status": milestone.get("status"),
                "milestone_progress": {"completed_tasks": milestone.get("completed_tasks", 0), "total_tasks": milestone.get("total_tasks", 0)},
                "result_status": replay_attempt.result_status,
                "next_launch": next_launch.to_dict() if next_launch is not None else None,
                "next_launch_status": next_launch_status,
            }
        current_snapshot = self.execution.snapshot(submission.project_id)
        if current_snapshot.get("current_binding_id") != binding.execution_binding_id or binding.status != "ACTIVE" or binding.superseded:
            raise ProjectWorkflowError("execution_binding_stale", "result binding is not the current canonical binding")
        if binding.conversation_id is None:
            # Legacy launch records predate canonical conversation binding. The
            # first exact result establishes this binding once; subsequent
            # results must match it. Browser UI exposes this only for the
            # conversation that owns the local launch projection.
            binding = self.execution.bind_conversation(submission.project_id, binding.execution_binding_id, conversation_id)
        replay = False
        attempt = self.execution.record_result(submission.project_id, submission.to_dict())
        snapshot = self.execution.snapshot(submission.project_id)
        next_launch: ProjectLaunch | None = None
        next_launch_status = "not_runnable"
        auto = snapshot.get("milestone_auto")
        if attempt.result_status == "PASS" and auto and auto.get("status") == "RUNNABLE" and snapshot.get("current_task_id"):
            next_launch, next_launch_status = self._ensure_auto_next_launch(submission.project_id, completed_task_id=attempt.task_id)
            snapshot = self.execution.snapshot(submission.project_id)
            auto = snapshot.get("milestone_auto")
        elif attempt.result_status == "PASS" and auto and auto.get("status") == "MILESTONE_COMPLETED":
            next_launch_status = "milestone_completed"
        milestone = snapshot.get("milestone_auto") or {}
        return {
            "schema": "bdb-project-execution-receipt-v1",
            "accepted": attempt.result_status == "PASS",
            "replayed": replay,
            "project_id": submission.project_id,
            "task_id": attempt.task_id,
            "execution_binding_id": attempt.execution_binding_id,
            "attempt_id": attempt.attempt_id,
            "task_status": snapshot.get("task_statuses", {}).get(attempt.task_id),
            "current_task_id": snapshot.get("current_task_id"),
            "milestone_run_id": milestone.get("milestone_run_id"),
            "milestone_status": milestone.get("status"),
            "milestone_progress": {"completed_tasks": milestone.get("completed_tasks", 0), "total_tasks": milestone.get("total_tasks", 0)},
            "result_status": attempt.result_status,
            "next_launch": next_launch.to_dict() if next_launch is not None else None,
            "next_launch_status": next_launch_status,
        }

    def current_repo_head(self, project: ProjectRecord) -> str:
        result = self.runner.run(("git", "rev-parse", "HEAD"), cwd=Path(project.local_repo_path), timeout_seconds=30)
        if result.returncode != 0 or not re.fullmatch(r"[0-9a-fA-F]{40,64}", result.stdout.strip()):
            raise ProjectWorkflowError("repo_head_unavailable", "registered project HEAD could not be read")
        return result.stdout.strip().lower()

    def publish_outbox_launch(self, project_id: str, launch_id: str) -> ProjectLaunch:
        """Project a prepared canonical outbox launch into the project launch queue."""
        outbox_rec = self.execution.launch_outbox_record(project_id, launch_id)
        if outbox_rec is None:
            raise ProjectWorkflowError("launch_outbox_not_found", f"outbox launch '{launch_id}' does not exist in canonical memory")

        pending = self.queue.peek()
        if pending is not None:
            if pending.launch_id == launch_id:
                self.execution.mark_outbox_published(project_id, launch_id)
                return pending
            # Check if existing queue item is an orphan
            is_orphan = True
            if pending.project_id and pending.execution_binding_id:
                try:
                    q_outbox = self.execution.launch_outbox_record(pending.project_id, pending.launch_id)
                    if q_outbox is not None and q_outbox.status in {"PENDING", "PUBLISHED"}:
                        is_orphan = False
                except Exception:
                    pass
            if is_orphan:
                # Clear orphan projection fail-closed
                with self.queue._lock():
                    self.queue._write_state_unlocked(None, None)
            else:
                raise ProjectWorkflowError("queue_pending", "project launch queue already contains another canonical binding")

        launch = self.queue.enqueue(
            repo_alias=outbox_rec.repo_alias,
            prompt=outbox_rec.prompt,
            auto_send=outbox_rec.auto_send,
            ttl_minutes=10,
            launch_id=outbox_rec.launch_id,
            project_id=outbox_rec.project_id,
            plan_version=outbox_rec.plan_version,
            task_id=outbox_rec.task_id,
            execution_binding_id=outbox_rec.execution_binding_id,
            correlation_id=outbox_rec.correlation_id,
            command_id=outbox_rec.command_id,
            expected_repo_head_before=outbox_rec.expected_repo_head_before,
        )
        self.execution.mark_outbox_published(project_id, launch_id)
        return launch

    def reconcile_launch_outbox(self, project_id: str | None = None) -> dict[str, Any]:
        """Reconcile canonical launch outbox records against downstream queue projection.

        - Rebuilds/republishes any PENDING outbox records.
        - Identifies and clears orphan queue entries that lack canonical outbox authority.
        """
        projects = [self.catalog.get(project_id)] if project_id else self.catalog.read()
        reconciled_count = 0
        orphans_cleared = 0

        for project in projects:
            if project is None or not project.plan_imported:
                continue
            pending_records = self.execution.pending_outbox_records(project.project_id)
            for rec in pending_records:
                q_pending = self.queue.peek()
                if q_pending is None:
                    try:
                        self.publish_outbox_launch(project.project_id, rec.launch_id)
                        reconciled_count += 1
                    except (ProjectWorkflowError, ProjectLaunchQueueError):
                        q_current = self.queue.peek()
                        if q_current is not None and q_current.launch_id == rec.launch_id:
                            self.execution.mark_outbox_published(project.project_id, rec.launch_id)
                            reconciled_count += 1
                elif q_pending.launch_id == rec.launch_id:
                    self.execution.mark_outbox_published(project.project_id, rec.launch_id)
                    reconciled_count += 1

        # Check for orphan queue projection
        q_pending = self.queue.peek()
        if q_pending is not None:
            orphan = True
            if q_pending.project_id and q_pending.execution_binding_id:
                try:
                    outbox = self.execution.launch_outbox_record(q_pending.project_id, q_pending.launch_id)
                    if outbox is not None:
                        orphan = False
                except Exception:
                    orphan = True
            if orphan:
                with self.queue._lock():
                    self.queue._write_state_unlocked(None, None)
                orphans_cleared += 1

        return {
            "status": "reconciled",
            "reconciled_count": reconciled_count,
            "orphans_cleared": orphans_cleared,
        }

    def _queue_execution_prompt(self, project_id: str, kind: str, *, binding_override: ProjectExecutionBinding | None = None) -> ProjectLaunch:
        project = self.catalog.get(project_id)
        if project is None:
            raise ProjectWorkflowError("project_not_found", "project is not in the canonical catalog")
        memory = self.memory(project_id); plan = memory.current_plan(); state = memory.read_state()
        if plan is None:
            raise ProjectWorkflowError("project_plan_required", "Project Plan must be imported before Start/Continue")
        head = self.current_repo_head(project)
        try:
            binding = binding_override or self.execution.new_binding(project_id, expected_repo_head_before=head)
            prompt = build_start_prompt(project, plan=plan, state=state, git_head=head, binding=binding) if kind == "start" else build_continue_prompt(project, plan=plan, state=state, git_head=head, binding=binding)
            auto = self.execution.milestone_auto_snapshot(project_id)
            auto_send = (
                auto.get("status") == "RUNNABLE" and
                auto.get("current_task_id") == binding.task_id and
                isinstance(auto.get("milestone_run_id"), str) and
                bool(auto.get("milestone_run_id"))
            )
            # Step 1: ATOMIC PREPARE (binding + PENDING outbox) in Project Memory
            persisted_binding, outbox_rec = self.execution.prepare_launch(
                project_id,
                binding=binding,
                prompt=prompt,
                auto_send=auto_send,
                ttl_minutes=10,
            )
            # Step 2: QUEUE PROJECTION
            launch = self.publish_outbox_launch(project_id, outbox_rec.launch_id)
        except (ProjectExecutionError, ProjectLaunchQueueError) as exc:
            raise ProjectWorkflowError(getattr(exc, "code", "project_execution_binding_failed"), str(exc)) from exc
        updated = ProjectRecord(**{**project.__dict__, "last_launch_id": launch.launch_id, "last_correlation_id": binding.correlation_id})
        self.catalog.upsert(updated)
        return launch

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

    def _ensure_project_memory(self, project: ProjectRecord) -> None:
        memory = ProjectMemoryStore(self.catalog.runtime_root, project.project_id)
        if not memory.read_state().events:
            memory.append_event("PROJECT_CREATED", f"Zarejestrowano projekt {project.display_name}")


__all__ = [
    "CommandResult",
    "GhRepositoryAdapter",
    "GitHubRepositoryAdapter",
    "ProjectCreationResult",
    "ProjectExecutionBinding",
    "ProjectExecutionCoordinator",
    "ProjectExecutionError",
    "ProjectLaunchOutboxRecord",
    "ProjectWorkflow",
    "ProjectWorkflowError",
    "SubprocessCommandRunner",
    "WorkPlanningPrompt",
    "WorkPlanningPromptBuilder",
    "WorkPlanningPromptError",
    "HANDOFF_MODES",
    "brief_markdown",
    "build_continue_prompt",
    "build_plan_prompt",
    "build_start_prompt",
    "project_plan_markdown",
]

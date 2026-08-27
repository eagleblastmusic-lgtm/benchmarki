from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from .journal import Journal
from .models import (
    BridgeErrorCode,
    CommandState,
    ExecutionOutcome,
    OperationEffectRecord,
    OperationPlanRecord,
    ProfileRunOutcome,
    RecoveryDecision,
)
from .protocol import BridgeError, sanitize_diagnostics
from .recovery_journal import compute_operation_effect_sha256, compute_operation_plan_sha256, sha256_bytes
from .workspace_manager import WorkspaceManager, changed_paths


@dataclass(frozen=True)
class RecoveryAssessment:
    decision: RecoveryDecision
    reason_code: str
    diagnostic: dict[str, object]
    plan: OperationPlanRecord | None = None
    effect: OperationEffectRecord | None = None
    verified_temp_path: str | None = None


class SystemCrash(BaseException):
    """Fault-injection crash that deliberately bypasses generic Exception handlers."""


_MAX_EXACT_REPLACEMENTS = 16
_TERMINAL_DIAGNOSTIC_EVENT = "command.terminal_diagnostic"


def _replacement_pairs(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """Return one bounded replacement batch while retaining legacy old/new input."""

    raw_batch = payload.get("replacements")
    has_legacy = "old" in payload or "new" in payload
    if raw_batch is not None and has_legacy:
        raise BridgeError(
            BridgeErrorCode.INVALID_PAYLOAD,
            "Use either old/new or replacements, not both",
        )
    if raw_batch is None:
        old_text = payload.get("old")
        new_text = payload.get("new")
        if not isinstance(old_text, str) or not isinstance(new_text, str):
            raise BridgeError(
                BridgeErrorCode.INVALID_PAYLOAD,
                "path, old and new must be strings",
            )
        raw_batch = [{"old": old_text, "new": new_text}]
    if (
        not isinstance(raw_batch, list)
        or isinstance(raw_batch, (str, bytes))
        or not 1 <= len(raw_batch) <= _MAX_EXACT_REPLACEMENTS
    ):
        raise BridgeError(
            BridgeErrorCode.INVALID_PAYLOAD,
            f"replacements must contain 1-{_MAX_EXACT_REPLACEMENTS} items",
        )
    pairs: list[tuple[str, str]] = []
    for index, item in enumerate(raw_batch, start=1):
        if not isinstance(item, dict):
            raise BridgeError(
                BridgeErrorCode.INVALID_PAYLOAD,
                f"replacement {index} must be an object",
            )
        old_text = item.get("old")
        new_text = item.get("new")
        if not isinstance(old_text, str) or not isinstance(new_text, str) or not old_text:
            raise BridgeError(
                BridgeErrorCode.INVALID_PAYLOAD,
                f"replacement {index} requires non-empty old and string new",
            )
        pairs.append((old_text, new_text))
    return pairs


def _uniform_line_ending(text: str) -> str | None:
    without_crlf = text.replace("\r\n", "")
    has_crlf = "\r\n" in text
    has_lf = "\n" in without_crlf
    has_cr = "\r" in without_crlf
    kinds = int(has_crlf) + int(has_lf) + int(has_cr)
    if kinds != 1:
        return None
    if has_crlf:
        return "\r\n"
    return "\n" if has_lf else "\r"


def _to_lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _apply_exact_replacements(text: str, pairs: list[tuple[str, str]]) -> str:
    """Apply a bounded batch, accepting LF/CRLF variants but preserving file EOLs."""

    current = text
    target_eol = _uniform_line_ending(text)
    for index, (old_text, new_text) in enumerate(pairs, start=1):
        count = current.count(old_text)
        if count == 1:
            replacement = new_text
            if target_eol and ("\n" in new_text or "\r" in new_text):
                replacement = _to_lf(new_text).replace("\n", target_eol)
            current = current.replace(old_text, replacement, 1)
            continue

        normalized_old = _to_lf(old_text)
        can_compare_eol_agnostic = (
            count == 0
            and target_eol is not None
            and ("\n" in old_text or "\r" in old_text)
        )
        normalized_current = _to_lf(current) if can_compare_eol_agnostic else current
        normalized_count = (
            normalized_current.count(normalized_old) if can_compare_eol_agnostic else count
        )
        if normalized_count != 1:
            raise BridgeError(
                BridgeErrorCode.REPLACE_MISMATCH,
                f"Replacement {index}: expected exactly one match, found {normalized_count}",
            )
        normalized_new = _to_lf(new_text)
        current = normalized_current.replace(normalized_old, normalized_new, 1)
        current = current.replace("\n", target_eol)
    return current


def sanitized_test_environment() -> dict[str, str]:
    import sys
    allowed = ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0"})
    env["PYTHONPATH"] = os.environ.get("PYTHONPATH", os.pathsep.join(sys.path))
    return env


def _diagnostic(wm: WorkspaceManager, *, reason: str, extra: dict[str, object] | None = None) -> dict[str, object]:
    value: dict[str, object] = {
        "reason": reason[:200],
        "workspace": wm.path.name,
        "base_sha": wm.base_sha,
    }
    if extra:
        value.update({str(k)[:80]: v for k, v in list(extra.items())[:15]})
    return value


def make_recovery_decision(
    command_id: str,
    cmd_state: CommandState,
    journal: Journal,
    wm: WorkspaceManager,
) -> RecoveryAssessment:
    if cmd_state == CommandState.CLAIMED:
        return RecoveryAssessment(RecoveryDecision.EXECUTE, "claimed_new", _diagnostic(wm, reason="new claimed command"))
    plan = journal.get_operation_plan(command_id)
    if plan is None:
        return RecoveryAssessment(RecoveryDecision.DIVERGED, "plan_missing", _diagnostic(wm, reason="persisted plan missing"))
    workspace = journal.get_workspace(plan.session_id)
    if workspace is None:
        return RecoveryAssessment(RecoveryDecision.DIVERGED, "workspace_record_missing", _diagnostic(wm, reason="workspace journal record missing"), plan=plan)
    if Path(workspace.workspace_path) != wm.path or workspace.base_sha.lower() != wm.base_sha:
        return RecoveryAssessment(RecoveryDecision.DIVERGED, "workspace_identity_mismatch", _diagnostic(wm, reason="workspace path/base mismatch"), plan=plan)
    expected_temp = wm.temp_path_for(plan)
    try:
        foreign = [] if wm.direct_checkout else wm.unauthorized_changed_paths(expected_temp=expected_temp)
        if foreign:
            return RecoveryAssessment(
                RecoveryDecision.DIVERGED,
                "foreign_workspace_paths",
                _diagnostic(wm, reason="foreign workspace paths", extra={"paths": foreign[:20]}),
                plan=plan,
            )
        target = wm.read_exact_bytes(plan.target_path)
        actual_state = wm.compute_state_hash()
    except BridgeError as exc:
        return RecoveryAssessment(RecoveryDecision.DIVERGED, str(exc.code), _diagnostic(wm, reason=str(exc)), plan=plan)
    except Exception as exc:
        return RecoveryAssessment(
            RecoveryDecision.DIVERGED,
            "recovery_assessment_internal_error",
            _diagnostic(wm, reason=type(exc).__name__),
            plan=plan,
        )
    if cmd_state == CommandState.EXECUTING:
        if (
            workspace.revision == plan.workspace_revision_before
            and workspace.state_hash == plan.workspace_state_hash_before
            and target == plan.before_content
            and actual_state == plan.workspace_state_hash_before
        ):
            try:
                temp = wm.verify_expected_temp(plan)
            except BridgeError as exc:
                return RecoveryAssessment(RecoveryDecision.DIVERGED, str(exc.code), _diagnostic(wm, reason=str(exc)), plan=plan)
            return RecoveryAssessment(
                RecoveryDecision.EXECUTE,
                "before_with_verified_temp" if temp else "before",
                _diagnostic(wm, reason="verified BEFORE state", extra={"revision": workspace.revision, "state_hash": actual_state}),
                plan=plan,
                verified_temp_path=temp.name if temp else None,
            )
        if (
            workspace.revision == plan.workspace_revision_before
            and workspace.state_hash == plan.workspace_state_hash_before
            and target == plan.planned_after_content
            and actual_state == plan.planned_after_state_hash
            and not expected_temp.exists()
            and journal.get_operation_effect(command_id) is None
        ):
            return RecoveryAssessment(
                RecoveryDecision.RECOVER_PLANNED_AFTER,
                "planned_after_without_effect",
                _diagnostic(wm, reason="verified PLANNED-AFTER state", extra={"state_hash": actual_state}),
                plan=plan,
            )
        return RecoveryAssessment(
            RecoveryDecision.DIVERGED,
            "executing_state_diverged",
            _diagnostic(
                wm,
                reason="EXECUTING state is neither BEFORE nor PLANNED-AFTER",
                extra={"journal_revision": workspace.revision, "journal_state_hash": workspace.state_hash, "actual_state_hash": actual_state},
            ),
            plan=plan,
        )
    if cmd_state == CommandState.EFFECT_RECORDED:
        effect = journal.get_operation_effect(command_id)
        if effect is None:
            return RecoveryAssessment(RecoveryDecision.DIVERGED, "effect_missing", _diagnostic(wm, reason="effect row missing"), plan=plan)
        if (
            workspace.revision == effect.workspace_revision_after
            and workspace.state_hash == effect.workspace_state_hash_after
            and target == plan.planned_after_content
            and actual_state == effect.workspace_state_hash_after
            and not expected_temp.exists()
        ):
            return RecoveryAssessment(
                RecoveryDecision.IDEMPOTENT_REPLAY,
                "effect_verified",
                _diagnostic(wm, reason="verified EFFECT state", extra={"revision": workspace.revision, "state_hash": actual_state}),
                plan=plan,
                effect=effect,
            )
        return RecoveryAssessment(
            RecoveryDecision.DIVERGED,
            "effect_state_diverged",
            _diagnostic(wm, reason="physical/journal state differs from persisted effect"),
            plan=plan,
            effect=effect,
        )
    return RecoveryAssessment(
        RecoveryDecision.DIVERGED,
        "unsupported_command_state",
        _diagnostic(wm, reason=f"unsupported recovery state {cmd_state.value}"),
        plan=plan,
    )


class ExecutionCoordinator:
    def __init__(self, config: Any, journal: Journal, fault_hook: Callable[[str], None] | None = None) -> None:
        self.config = config
        self.journal = journal
        self.fault_hook = fault_hook

    def _fault(self, point: str) -> None:
        if self.fault_hook:
            self.fault_hook(point)

    def _manual_outcome(
        self,
        *,
        session_id: str,
        command_id: str,
        wm: WorkspaceManager,
        reason_code: str,
        summary: str,
        diagnostic: dict[str, object],
    ) -> ExecutionOutcome:
        self.journal.mark_workspace_recovery_blocked(
            session_id=session_id,
            command_id=command_id,
            reason_code=reason_code,
            diagnostic=diagnostic,
            fault_hook=self.fault_hook,
        )
        workspace = self.journal.get_workspace(session_id)
        revision = workspace.revision if workspace else 0
        state_hash = workspace.state_hash if workspace else ""
        return ExecutionOutcome(
            status="manual_reconciliation_required",
            error_code=BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED,
            summary=summary,
            workspace_revision_before=revision,
            workspace_revision_after=revision,
            workspace_state_hash_before=state_hash,
            workspace_state_hash_after=state_hash,
            changed_files=[],
            diff="",
            manual_reconciliation_details=wm.preserve_workspace(),
        )

    def _terminal_claimed_outcome(
        self,
        command_id: str,
        new_state: CommandState,
        status: str,
        code: BridgeErrorCode,
        summary: str,
        revision: int,
        state_hash: str,
    ) -> ExecutionOutcome:
        now = self.journal._now_fn()
        with self.journal._transaction():
            row = self.journal._transition_command_in_transaction(
                command_id,
                CommandState.CLAIMED,
                new_state,
                now,
            )
            code_value = str(getattr(code, "value", code))
            detail = sanitize_diagnostics(summary, limit=500) or code_value
            self.journal._append_event_in_transaction(
                session_id=row[1],
                command_id=command_id,
                event_type=_TERMINAL_DIAGNOSTIC_EVENT,
                payload={"error_code": code_value, "detail": detail},
                created_at=now,
            )
        return ExecutionOutcome(
            status=status,
            error_code=code,
            summary=summary,
            workspace_revision_before=revision,
            workspace_revision_after=revision,
            workspace_state_hash_before=state_hash,
            workspace_state_hash_after=state_hash,
            changed_files=[],
            diff="",
        )

    @staticmethod
    def _parse_command(command_json: str) -> tuple[dict[str, Any], str, str]:
        try:
            document = json.loads(command_json)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "command_json is not valid JSON") from exc
        if not isinstance(document, dict):
            raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "command_json must be an object")
        payload = document.get("payload")
        if not isinstance(payload, dict):
            raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "payload must be an object")
        operation = document.get("operation")
        if operation != "replace_exact_and_test":
            raise BridgeError(BridgeErrorCode.UNSUPPORTED_OPERATION, f"Operation {operation!r} is not supported by GHB0-4")
        profile_id = payload.get("profile_id")
        allowed_profiles = {"poc_pytest", "shopify_theme_check"}
        if profile_id not in allowed_profiles:
            raise BridgeError(
                BridgeErrorCode.POLICY_DENIED,
                "payload.profile_id must be one of: poc_pytest, shopify_theme_check",
            )
        return payload, operation, profile_id

    def execute_or_recover(self, command_id: str) -> ExecutionOutcome:
        self.journal._ensure_open()
        command = self.journal.get_command(command_id)
        if command is None:
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, f"Command {command_id} not found")
        session = self.journal.get_session(command.session_id)
        if session is None:
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, f"Session {command.session_id} not found")
        ingestion = self.journal.get_session_ingestion(session.session_id)
        if ingestion is None:
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Session ingestion manifest is missing")
        try:
            manifest = json.loads(ingestion.manifest_json)
            manifest_paths = manifest.get("allowed_paths")
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise BridgeError(BridgeErrorCode.JOURNAL_CORRUPT, "Persisted manifest is invalid JSON") from exc
        if not isinstance(manifest_paths, list) or not all(isinstance(v, str) for v in manifest_paths):
            raise BridgeError(BridgeErrorCode.JOURNAL_CORRUPT, "Persisted manifest allowed_paths is invalid")
        wm = WorkspaceManager(self.config, session.session_id, session.base_sha, manifest_paths)
        try:
            wm.ensure_workspace(self.journal)
        except BridgeError as exc:
            if exc.code in {
                BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED,
                BridgeErrorCode.DIRTY_SOURCE_CHECKOUT,
                BridgeErrorCode.UNKNOWN_BASE_SHA,
                BridgeErrorCode.UNSAFE_WORKTREE_PATH,
                BridgeErrorCode.GIT_ERROR,
            }:
                return self._manual_outcome(
                    session_id=session.session_id,
                    command_id=command_id,
                    wm=wm,
                    reason_code=str(exc.code),
                    summary=f"Workspace attachment failed: {exc}",
                    diagnostic=_diagnostic(wm, reason=str(exc)),
                )
            raise
        assessment = make_recovery_decision(command_id, command.state, self.journal, wm)
        if assessment.decision == RecoveryDecision.DIVERGED:
            return self._manual_outcome(
                session_id=session.session_id,
                command_id=command_id,
                wm=wm,
                reason_code=assessment.reason_code,
                summary="Workspace state diverged from persisted recovery contract",
                diagnostic=assessment.diagnostic,
            )
        if assessment.decision == RecoveryDecision.IDEMPOTENT_REPLAY:
            assert assessment.plan is not None and assessment.effect is not None
            self._fault("BEFORE_PROFILE")
            profile = self._run_profile(wm, assessment.plan.profile_id)
            return self._effect_outcome(wm, assessment.plan, assessment.effect, profile, "Idempotent effect replay")
        if assessment.decision == RecoveryDecision.RECOVER_PLANNED_AFTER:
            assert assessment.plan is not None
            effect = self._effect_for(assessment.plan)
            self.journal.record_operation_effect(effect, fault_hook=self.fault_hook)
            self._fault("AFTER_EFFECT_COMMIT_BEFORE_PROFILE")
            self._fault("BEFORE_PROFILE")
            profile = self._run_profile(wm, assessment.plan.profile_id)
            return self._effect_outcome(wm, assessment.plan, effect, profile, "Recovered PLANNED-AFTER effect")
        assert assessment.decision == RecoveryDecision.EXECUTE
        plan = assessment.plan
        if plan is None:
            workspace = self.journal.get_workspace(session.session_id)
            if workspace is None:
                return self._manual_outcome(
                    session_id=session.session_id,
                    command_id=command_id,
                    wm=wm,
                    reason_code="workspace_record_missing",
                    summary="Workspace journal record is missing before execution",
                    diagnostic=_diagnostic(wm, reason="workspace record missing"),
                )
            try:
                payload, operation, profile_id = self._parse_command(command.command_json)
            except BridgeError as exc:
                if exc.code in {BridgeErrorCode.POLICY_DENIED, BridgeErrorCode.UNSUPPORTED_OPERATION, BridgeErrorCode.INVALID_PAYLOAD}:
                    return self._terminal_claimed_outcome(
                        command_id,
                        CommandState.POLICY_DENIED,
                        "policy_denied",
                        exc.code,
                        str(exc),
                        workspace.revision,
                        workspace.state_hash,
                    )
                raise
            try:
                wm.validate_preplan_gate(
                    workspace,
                    expected_revision=command.expected_revision if command.expected_revision is not None else -1,
                    expected_state_hash=command.expected_state_hash,
                )
            except BridgeError as exc:
                if exc.code == BridgeErrorCode.STALE_REVISION:
                    return self._terminal_claimed_outcome(command_id, CommandState.STALE_REVISION, "stale_revision", exc.code, str(exc), workspace.revision, workspace.state_hash)
                if exc.code == BridgeErrorCode.STATE_MISMATCH:
                    return self._terminal_claimed_outcome(command_id, CommandState.STATE_MISMATCH, "state_mismatch", exc.code, str(exc), workspace.revision, workspace.state_hash)
                if exc.code in {BridgeErrorCode.POLICY_DENIED, BridgeErrorCode.SCOPE_VIOLATION, BridgeErrorCode.UNSAFE_PATH}:
                    return self._terminal_claimed_outcome(command_id, CommandState.POLICY_DENIED, "policy_denied", exc.code, str(exc), workspace.revision, workspace.state_hash)
                return self._manual_outcome(
                    session_id=session.session_id,
                    command_id=command_id,
                    wm=wm,
                    reason_code=str(exc.code),
                    summary=f"Pre-plan workspace gate failed: {exc}",
                    diagnostic=_diagnostic(wm, reason=str(exc)),
                )
            relative_path = payload.get("path")
            if not isinstance(relative_path, str):
                return self._terminal_claimed_outcome(command_id, CommandState.POLICY_DENIED, "policy_denied", BridgeErrorCode.INVALID_PAYLOAD, "path must be a string", workspace.revision, workspace.state_hash)
            try:
                replacements = _replacement_pairs(payload)
            except BridgeError as exc:
                return self._terminal_claimed_outcome(command_id, CommandState.POLICY_DENIED, "policy_denied", exc.code, str(exc), workspace.revision, workspace.state_hash)
            try:
                wm.resolve_allowed_path(relative_path)
            except BridgeError as exc:
                return self._terminal_claimed_outcome(command_id, CommandState.POLICY_DENIED, "policy_denied", exc.code, str(exc), workspace.revision, workspace.state_hash)
            before = wm.read_exact_bytes(relative_path)
            try:
                before_text = before.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Target file is not strict UTF-8") from exc
            try:
                after_text = _apply_exact_replacements(before_text, replacements)
            except BridgeError as exc:
                return self._terminal_claimed_outcome(
                    command_id,
                    CommandState.POLICY_DENIED,
                    "policy_denied",
                    exc.code,
                    str(exc),
                    workspace.revision,
                    workspace.state_hash,
                )
            after = after_text.encode("utf-8")
            candidate = OperationPlanRecord(
                command_id=command_id,
                session_id=session.session_id,
                operation=operation,
                target_path=relative_path,
                profile_id=profile_id,
                expected_revision=workspace.revision,
                expected_state_hash=command.expected_state_hash,
                workspace_revision_before=workspace.revision,
                workspace_state_hash_before=workspace.state_hash,
                before_content=before,
                before_content_sha256=sha256_bytes(before),
                planned_after_content=after,
                planned_after_content_sha256=sha256_bytes(after),
                planned_after_state_hash=wm.compute_state_hash_with_override(relative_path, after),
                plan_sha256="",
                created_at=self.journal._now_fn(),
            )
            plan = replace(candidate, plan_sha256=compute_operation_plan_sha256(candidate))
            self.journal.record_operation_plan(plan)
        self._fault("AFTER_PLAN_COMMIT_BEFORE_WRITE")
        wm.apply_planned_bytes(plan, on_temp_written=lambda: self._fault("AFTER_TEMP_WRITE_BEFORE_REPLACE"))
        if wm.read_exact_bytes(plan.target_path) != plan.planned_after_content:
            raise BridgeError(BridgeErrorCode.JOURNAL_CORRUPT, "Target bytes differ after atomic replace")
        actual_after = wm.compute_state_hash()
        if actual_after != plan.planned_after_state_hash:
            raise BridgeError(BridgeErrorCode.JOURNAL_CORRUPT, "Workspace state differs after atomic replace")
        self._fault("AFTER_FILE_REPLACE_BEFORE_EFFECT_COMMIT")
        effect = self._effect_for(plan)
        self.journal.record_operation_effect(effect, fault_hook=self.fault_hook)
        self._fault("AFTER_EFFECT_COMMIT_BEFORE_PROFILE")
        self._fault("BEFORE_PROFILE")
        profile = self._run_profile(wm, plan.profile_id)
        return self._effect_outcome(wm, plan, effect, profile, "Command effect recorded")

    def _effect_for(self, plan: OperationPlanRecord) -> OperationEffectRecord:
        candidate = OperationEffectRecord(
            command_id=plan.command_id,
            session_id=plan.session_id,
            plan_sha256=plan.plan_sha256,
            target_path=plan.target_path,
            workspace_revision_before=plan.workspace_revision_before,
            workspace_revision_after=plan.workspace_revision_before + 1,
            workspace_state_hash_before=plan.workspace_state_hash_before,
            workspace_state_hash_after=plan.planned_after_state_hash,
            before_content_sha256=plan.before_content_sha256,
            after_content_sha256=plan.planned_after_content_sha256,
            effect_sha256="",
            recorded_at=self.journal._now_fn(),
        )
        return replace(candidate, effect_sha256=compute_operation_effect_sha256(candidate))

    def _effect_outcome(
        self,
        wm: WorkspaceManager,
        plan: OperationPlanRecord,
        effect: OperationEffectRecord,
        profile: ProfileRunOutcome,
        summary: str,
    ) -> ExecutionOutcome:
        return ExecutionOutcome(
            status=profile.status,
            error_code=None if profile.status == "success" else profile.status,
            summary=summary,
            workspace_revision_before=effect.workspace_revision_before,
            workspace_revision_after=effect.workspace_revision_after,
            workspace_state_hash_before=effect.workspace_state_hash_before,
            workspace_state_hash_after=effect.workspace_state_hash_after,
            changed_files=[plan.target_path],
            diff=wm.git.run(["diff", "--", plan.target_path]).stdout,
            profile_run=profile,
        )

    def _run_profile(self, wm: WorkspaceManager, profile_id: str = "poc_pytest") -> ProfileRunOutcome:
        if profile_id != "poc_pytest":
            return ProfileRunOutcome("internal_error", None, "", "Profile is not locally allowed", 0)
        executable = Path(self.config.python_executable)
        if not executable.is_file():
            return ProfileRunOutcome("internal_error", None, "", "Configured Python executable does not exist", 0)
        started = time.monotonic()
        try:
            completed = subprocess.run(
                [str(executable), "-m", "pytest", "-q"],
                cwd=wm.path,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
                timeout=self.config.test_timeout_seconds,
                env=sanitized_test_environment(),
                shell=False,
            )
            status = "success" if completed.returncode == 0 else "failed"
            return ProfileRunOutcome(status, completed.returncode, completed.stdout, completed.stderr, int((time.monotonic() - started) * 1000))
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            return ProfileRunOutcome("timeout", None, stdout, stderr, int((time.monotonic() - started) * 1000))
        except (FileNotFoundError, OSError, UnicodeError) as exc:
            return ProfileRunOutcome("internal_error", None, "", type(exc).__name__, int((time.monotonic() - started) * 1000))

    def _get_changed_files(self, wm: WorkspaceManager) -> list[str]:
        return changed_paths(wm.git.run(["status", "--porcelain=v1"]).stdout)

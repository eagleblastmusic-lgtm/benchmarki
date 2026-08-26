"""Canonical view-model and command boundary for Machine Environment readiness GUI.

NX-038 provides a concise, accessible projection of canonical machine
environment readiness, project summary, task delta, and safe actions.
The GUI is strictly a projection surface and is NEVER an authority.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from bdb_shared.evidence import canonical_json_bytes

from .environment_cache import CacheLookup, EnvironmentReadinessCache
from .environment_provisioning import (
    ApprovalClass,
    EffectClass,
    EnvironmentPlan,
    EnvironmentResult,
    EnvironmentStatus,
    ProjectLocalEnvironmentProvisioner,
)
from .environment_requirements import (
    EnvironmentRequirement,
    EnvironmentRequirementSet,
    ReadinessResult,
    ReadinessStatus,
    RequirementDisposition,
    RequirementResolution,
    resolve_requirements,
)
from .machine_inventory_contract import FactStatus, InventoryFact, MachineInventory


MACHINE_ENVIRONMENT_GUI_SCHEMA = "bdb-vnext-machine-environment-gui-v1"
MACHINE_ENVIRONMENT_GUI_VERSION = "1.0.0"
MACHINE_ENVIRONMENT_GUI_VERSION_EXPLICIT = True

GUI_BECOMES_ENVIRONMENT_AUTHORITY = False
BROWSER_LOCAL_READINESS_OVERRIDES_CANONICAL = False

_URL_CREDENTIAL_PATTERN = re.compile(r"://([^:]+):([^@]+)@")
_KV_SECRET_PATTERN = re.compile(
    r"(?i)(token|password|pass|secret|key|bearer|authorization|auth|credential)[^\w]*[:=]\s*([^\s,;'\"\}]+)"
)
_TOKEN_PATTERN = re.compile(r"\b(ghp_[A-Za-z0-9_]+|secret-[A-Za-z0-9_-]+)\b")


class _StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class GuiReadinessState(_StringEnum):
    """Distinct canonical readiness states rendered by the GUI.

    MISSING != VERSION_MISMATCH != UNVERIFIABLE: these states are never
    collapsed into a generic 'Not ready'.
    """

    READY = "READY"
    MISSING = "MISSING"
    VERSION_MISMATCH = "VERSION_MISMATCH"
    UNVERIFIABLE = "UNVERIFIABLE"
    STALE = "STALE"
    PREPARATION_REQUIRED = "PREPARATION_REQUIRED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    PREPARING = "PREPARING"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


READINESS_EXPLANATION_TEXT: dict[GuiReadinessState, str] = {
    GuiReadinessState.READY: "Środowisko spełnia wszystkie kanoniczne wymagania projektu i zadania.",
    GuiReadinessState.MISSING: "Brak wymaganego narzędzia lub komponentu w środowisku maszynowym.",
    GuiReadinessState.VERSION_MISMATCH: "Zainstalowana wersja narzędzia nie spełnia wymaganego zakresu wersji.",
    GuiReadinessState.UNVERIFIABLE: "Narzędzie lub wersja nie może być deterministycznie zweryfikowana (błąd sondy).",
    GuiReadinessState.STALE: "Stan środowiska uległ przedawnieniu lub unieważnieniu przez dryf; wymagane odświeżenie.",
    GuiReadinessState.PREPARATION_REQUIRED: "Wymagane jest przygotowanie lokalnego środowiska projektu.",
    GuiReadinessState.APPROVAL_REQUIRED: "Przygotowanie środowiska wymaga jawnego zatwierdzenia przez operatora.",
    GuiReadinessState.PREPARING: "Trwa przygotowywanie lokalnego środowiska projektu...",
    GuiReadinessState.BLOCKED: "Środowisko jest zablokowane przez politykę bezpieczeństwa lub błąd krytyczny.",
    GuiReadinessState.FAILED: "Przygotowanie środowiska zakończyło się błędem.",
}

STATUS_DISPLAY_LABELS: dict[GuiReadinessState, str] = {
    GuiReadinessState.READY: "[GOTOWE] READY",
    GuiReadinessState.MISSING: "[BRAK] MISSING",
    GuiReadinessState.VERSION_MISMATCH: "[NIEZGODNA WERSJA] VERSION_MISMATCH",
    GuiReadinessState.UNVERIFIABLE: "[NIEWERYFIKOWALNE] UNVERIFIABLE",
    GuiReadinessState.STALE: "[PRZEDAWNIONE] STALE / REFRESH_REQUIRED",
    GuiReadinessState.PREPARATION_REQUIRED: "[WYMAGA PRZYGOTOWANIA] PREPARATION_REQUIRED",
    GuiReadinessState.APPROVAL_REQUIRED: "[WYMAGA ZATWIERDZENIA] APPROVAL_REQUIRED",
    GuiReadinessState.PREPARING: "[PRZYGOTOWYWANIE] PREPARING",
    GuiReadinessState.BLOCKED: "[ZABLOKOWANE] BLOCKED",
    GuiReadinessState.FAILED: "[BŁĄD] FAILED",
}


def sanitize_diagnostic_text(text: str) -> str:
    """Redact secret-bearing strings to ensure zero secret leakage in GUI."""
    text = _URL_CREDENTIAL_PATTERN.sub(r"://\1:[REDACTED]@", text)
    text = _KV_SECRET_PATTERN.sub(r"\1=[REDACTED]", text)
    text = _TOKEN_PATTERN.sub("[REDACTED]", text)
    return text


def compute_resolver_result_digest(result: ReadinessResult) -> str:
    """Derive the deterministic canonical digest for a ReadinessResult."""
    return "sha256:" + hashlib.sha256(result.canonical_bytes(normalize_time=True)).hexdigest()


@dataclass(frozen=True)
class GuiControlSpec:
    """Accessibility contract for Machine Environment GUI controls."""

    control_id: str
    accessible_name: str
    accessible_description: str
    keyboard_focusable: bool = True
    shortcut: str | None = None
    exposes_disabled_reason: bool = True


ENVIRONMENT_GUI_CONTROL_CONTRACT: tuple[GuiControlSpec, ...] = (
    GuiControlSpec(
        "status_badge",
        "Status gotowości środowiska",
        "Pokazuje kanoniczny stan gotowości środowiska maszynowego.",
        keyboard_focusable=True,
    ),
    GuiControlSpec(
        "summary_label",
        "Podsumowanie wymagań projektu",
        "Zwięzłe podsumowanie spełnionych i brakujących wymagań projektu.",
        keyboard_focusable=True,
    ),
    GuiControlSpec(
        "task_delta_label",
        "Delta wymagań bieżącego zadania",
        "Pokazuje specyficzne wymagania zadania różniące się od środowiska projektu.",
        keyboard_focusable=True,
    ),
    GuiControlSpec(
        "stale_indicator",
        "Wskaźnik aktualności środowiska",
        "Sygnalizuje przedawnienie projekcji i konieczność odświeżenia.",
        keyboard_focusable=True,
    ),
    GuiControlSpec(
        "refresh_button",
        "Odśwież stan środowiska",
        "Wysyła zapytanie odświeżenia do kanonicznego resolvera bez mutacji workflow.",
        keyboard_focusable=True,
        shortcut="F5",
    ),
    GuiControlSpec(
        "prepare_button",
        "Przygotuj środowisko lokalne",
        "Uruchamia bezpieczny provisioning środowiska lokalnego zgodnie z polityką.",
        keyboard_focusable=True,
        shortcut="Ctrl+P",
        exposes_disabled_reason=True,
    ),
    GuiControlSpec(
        "details_toggle",
        "Rozwiń/zwiń szczegóły środowiska",
        "Przełącza widoczność diagnostycznych szczegółów środowiska.",
        keyboard_focusable=True,
        shortcut="Ctrl+D",
    ),
    GuiControlSpec(
        "requirements_list",
        "Lista wymagań środowiskowych",
        "Tabela poszczególnych wymagań narzędziowych i ich stanów.",
        keyboard_focusable=True,
        shortcut="Ctrl+L",
    ),
    GuiControlSpec(
        "details_view",
        "Szczegóły diagnostyczne środowiska",
        "Szczegółowy raport diagnostyczny pozbawiony sekretów.",
        keyboard_focusable=True,
    ),
)


@dataclass(frozen=True)
class RequirementItemViewModel:
    """Rendered view-model for an individual environment requirement."""

    requirement_id: str
    capability: str
    required: bool
    disposition: RequirementDisposition
    state: GuiReadinessState
    status_label: str
    observed_version: str | None
    observed_path: str | None
    blocking: bool
    reason: str
    explanation: str
    provenance_summary: str
    requirement_digest: str

    @classmethod
    def from_resolution(
        cls,
        res: RequirementResolution,
        *,
        is_stale: bool = False,
    ) -> RequirementItemViewModel:
        if is_stale:
            state = GuiReadinessState.STALE
        elif res.disposition is RequirementDisposition.ALREADY_AVAILABLE:
            state = GuiReadinessState.READY
        elif res.disposition is RequirementDisposition.MISSING:
            state = GuiReadinessState.MISSING
        elif res.disposition is RequirementDisposition.VERSION_MISMATCH:
            state = GuiReadinessState.VERSION_MISMATCH
        elif res.disposition is RequirementDisposition.UNVERIFIABLE:
            state = GuiReadinessState.UNVERIFIABLE
        else:
            state = GuiReadinessState.FAILED

        clean_path = sanitize_diagnostic_text(res.observed_path or "") if res.observed_path else None
        clean_reason = sanitize_diagnostic_text(res.reason)
        clean_explanation = sanitize_diagnostic_text(res.explanation)

        provenance = f"inv:{res.inventory_id[:12] if res.inventory_id else 'none'} fresh:{res.inventory_freshness or 'unknown'}"

        return cls(
            requirement_id=res.requirement_id,
            capability=res.capability,
            required=res.required,
            disposition=res.disposition,
            state=state,
            status_label=STATUS_DISPLAY_LABELS[state],
            observed_version=res.observed_version,
            observed_path=clean_path,
            blocking=res.blocking,
            reason=clean_reason,
            explanation=clean_explanation,
            provenance_summary=provenance,
            requirement_digest=res.requirement_digest,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "capability": self.capability,
            "required": self.required,
            "disposition": self.disposition.value,
            "state": self.state.value,
            "status_label": self.status_label,
            "observed_version": self.observed_version,
            "observed_path": self.observed_path,
            "blocking": self.blocking,
            "reason": self.reason,
            "explanation": self.explanation,
            "provenance_summary": self.provenance_summary,
            "requirement_digest": self.requirement_digest,
        }


@dataclass(frozen=True)
class ProjectSummaryViewModel:
    """Concise project-level readiness summary."""

    project_id: str
    state: GuiReadinessState
    status_label: str
    is_ready: bool
    total_requirements: int
    satisfied_count: int
    missing_count: int
    mismatched_count: int
    unverifiable_count: int
    blocking_count: int
    summary_text: str
    resolver_result_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "state": self.state.value,
            "status_label": self.status_label,
            "is_ready": self.is_ready,
            "total_requirements": self.total_requirements,
            "satisfied_count": self.satisfied_count,
            "missing_count": self.missing_count,
            "mismatched_count": self.mismatched_count,
            "unverifiable_count": self.unverifiable_count,
            "blocking_count": self.blocking_count,
            "summary_text": self.summary_text,
            "resolver_result_digest": self.resolver_result_digest,
        }


@dataclass(frozen=True)
class TaskDeltaViewModel:
    """Concise task-specific delta explaining differences/blockers."""

    task_id: str
    state: GuiReadinessState
    status_label: str
    task_is_ready: bool
    has_delta: bool
    delta_requirements: tuple[RequirementItemViewModel, ...]
    explanation: str
    resolver_result_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "state": self.state.value,
            "status_label": self.status_label,
            "task_is_ready": self.task_is_ready,
            "has_delta": self.has_delta,
            "delta_requirements": [item.to_dict() for item in self.delta_requirements],
            "explanation": self.explanation,
            "resolver_result_digest": self.resolver_result_digest,
        }


@dataclass(frozen=True)
class MachineEnvironmentViewModel:
    """Complete, immutable GUI projection of Machine Environment readiness.

    Strict view-model contract:
    - Never authority for readiness.
    - Bound strictly to canonical resolver result digest.
    - When stale, visibly presents STALE and forces is_ready=False.
    """

    project_id: str
    task_id: str | None
    state: GuiReadinessState
    status_label: str
    explanation: str
    is_ready: bool
    is_stale: bool
    stale_reason: str | None
    project_summary: ProjectSummaryViewModel
    task_delta: TaskDeltaViewModel | None
    requirements: tuple[RequirementItemViewModel, ...]
    can_prepare: bool
    prepare_disabled_reason: str | None
    can_refresh: bool
    resolver_result_digest: str
    inventory_digest: str
    evaluated_at: str
    diagnostic_details: str
    schema: str = MACHINE_ENVIRONMENT_GUI_SCHEMA
    version: str = MACHINE_ENVIRONMENT_GUI_VERSION

    @classmethod
    def from_canonical(
        cls,
        project_result: ReadinessResult,
        *,
        project_id: str,
        task_id: str | None = None,
        task_result: ReadinessResult | None = None,
        is_stale: bool = False,
        stale_reason: str | None = None,
        provisioning_status: EnvironmentStatus | None = None,
        approval_class: ApprovalClass | None = None,
        preparation_permitted: bool = False,
        prepare_block_reason: str | None = None,
        browser_local_state: Mapping[str, Any] | None = None,
    ) -> MachineEnvironmentViewModel:
        """Construct the view-model projection from canonical results.

        Browser local state or GUI-side overrides are strictly forbidden from
        inventing readiness (BROWSER_LOCAL_READINESS_OVERRIDES_CANONICAL = False).
        """
        # Guard: Browser local state cannot override canonical readiness
        if browser_local_state:
            _ = browser_local_state.get("readiness_override")

        proj_digest = compute_resolver_result_digest(project_result)
        effective_stale = is_stale or project_result.stale
        if task_result and task_result.stale:
            effective_stale = True

        effective_stale_reason = stale_reason
        if effective_stale and not effective_stale_reason:
            effective_stale_reason = "Stan unieważniony przez zmianę tożsamości środowiska lub cache drift."

        # Determine individual requirement view-models
        active_result = task_result if task_result is not None else project_result
        items = tuple(
            RequirementItemViewModel.from_resolution(res, is_stale=effective_stale)
            for res in active_result.requirements
        )

        # Count dispositions
        satisfied = sum(1 for it in items if it.disposition is RequirementDisposition.ALREADY_AVAILABLE)
        missing = sum(1 for it in items if it.disposition is RequirementDisposition.MISSING and it.required)
        mismatched = sum(1 for it in items if it.disposition is RequirementDisposition.VERSION_MISMATCH and it.required)
        unverifiable = sum(1 for it in items if it.disposition is RequirementDisposition.UNVERIFIABLE and it.required)
        blocking = len(active_result.blocking_requirement_ids)

        # Derive state
        if effective_stale:
            state = GuiReadinessState.STALE
        elif provisioning_status == "PREPARING":
            state = GuiReadinessState.PREPARING
        elif provisioning_status in {EnvironmentStatus.BLOCKED, EnvironmentStatus.OFFLINE_CACHE_MISS, EnvironmentStatus.STALE_PLAN, "FAILED"}:
            state = GuiReadinessState.FAILED if provisioning_status == "FAILED" else GuiReadinessState.BLOCKED
        elif provisioning_status is EnvironmentStatus.POLICY_DENIED:
            state = GuiReadinessState.BLOCKED
        elif active_result.status is ReadinessStatus.ENVIRONMENT_READY:
            state = GuiReadinessState.READY
        elif approval_class in {ApprovalClass.PRIVILEGE_REQUIRED, ApprovalClass.POLICY_DENIED}:
            state = GuiReadinessState.APPROVAL_REQUIRED if approval_class is ApprovalClass.PRIVILEGE_REQUIRED else GuiReadinessState.BLOCKED
        elif preparation_permitted:
            state = GuiReadinessState.PREPARATION_REQUIRED
        elif missing > 0:
            state = GuiReadinessState.MISSING
        elif mismatched > 0:
            state = GuiReadinessState.VERSION_MISMATCH
        elif unverifiable > 0:
            state = GuiReadinessState.UNVERIFIABLE
        else:
            state = GuiReadinessState.BLOCKED

        # CRITICAL INVARIANT: A stale GUI NEVER presents current READY!
        is_ready = (state is GuiReadinessState.READY) and not effective_stale

        summary_text = (
            f"Projekt {project_id}: {satisfied}/{len(items)} wymagań spełnionych"
            + (f", {blocking} blokujących" if blocking else "")
        )

        project_summary = ProjectSummaryViewModel(
            project_id=project_id,
            state=state if task_id is None else (GuiReadinessState.READY if project_result.ready else GuiReadinessState.MISSING),
            status_label=STATUS_DISPLAY_LABELS[state if task_id is None else (GuiReadinessState.READY if project_result.ready else GuiReadinessState.MISSING)],
            is_ready=project_result.ready and not effective_stale,
            total_requirements=len(project_result.requirements),
            satisfied_count=sum(1 for r in project_result.requirements if r.disposition is RequirementDisposition.ALREADY_AVAILABLE),
            missing_count=sum(1 for r in project_result.requirements if r.disposition is RequirementDisposition.MISSING and r.required),
            mismatched_count=sum(1 for r in project_result.requirements if r.disposition is RequirementDisposition.VERSION_MISMATCH and r.required),
            unverifiable_count=sum(1 for r in project_result.requirements if r.disposition is RequirementDisposition.UNVERIFIABLE and r.required),
            blocking_count=len(project_result.blocking_requirement_ids),
            summary_text=summary_text,
            resolver_result_digest=proj_digest,
        )

        # Task delta computation
        task_delta: TaskDeltaViewModel | None = None
        if task_id is not None and task_result is not None:
            task_digest = compute_resolver_result_digest(task_result)
            proj_by_cap = {r.capability: r for r in project_result.requirements}
            delta_items: list[RequirementItemViewModel] = []

            for item in items:
                proj_match = proj_by_cap.get(item.capability)
                if proj_match is None:
                    delta_items.append(item)
                elif item.disposition != proj_match.disposition:
                    delta_items.append(item)
                elif item.blocking:
                    delta_items.append(item)

            has_delta = len(delta_items) > 0
            if has_delta:
                delta_expl = f"Zadanie {task_id} wymaga {len(delta_items)} specyficznych lub blokujących wymagań."
            else:
                delta_expl = f"Zadanie {task_id}: brak specyficznych wymagań blokujących (środowisko projektu zgodne)."

            task_delta = TaskDeltaViewModel(
                task_id=task_id,
                state=state,
                status_label=STATUS_DISPLAY_LABELS[state],
                task_is_ready=is_ready,
                has_delta=has_delta,
                delta_requirements=tuple(delta_items),
                explanation=delta_expl,
                resolver_result_digest=task_digest,
            )

        # Prepare button controls
        can_prepare = preparation_permitted and (state in {GuiReadinessState.PREPARATION_REQUIRED, GuiReadinessState.MISSING, GuiReadinessState.VERSION_MISMATCH})
        disabled_reason: str | None = None
        if not can_prepare:
            if state is GuiReadinessState.READY:
                disabled_reason = "Środowisko jest już w pełni gotowe."
            elif state is GuiReadinessState.APPROVAL_REQUIRED:
                disabled_reason = "Wymagane jest jawne zatwierdzenie przez operatora."
            elif state is GuiReadinessState.PREPARING:
                disabled_reason = "Trwa przygotowywanie środowiska..."
            elif state is GuiReadinessState.BLOCKED:
                disabled_reason = prepare_block_reason or "Przygotowanie zablokowane przez politykę."
            elif state is GuiReadinessState.STALE:
                disabled_reason = "Najpierw odśwież stan środowiska przed przygotowaniem."
            else:
                disabled_reason = prepare_block_reason or "Przygotowanie niedostępne dla bieżącego stanu."

        # Bounded diagnostic details (no raw secrets)
        diagnostic_payload = {
            "project_id": project_id,
            "task_id": task_id,
            "state": state.value,
            "is_ready": is_ready,
            "is_stale": effective_stale,
            "stale_reason": effective_stale_reason,
            "resolver_result_digest": proj_digest,
            "inventory_digest": active_result.inventory_digest,
            "inventory_freshness": active_result.inventory_freshness,
            "evaluated_at": active_result.evaluated_at,
            "blocking_requirements": list(active_result.blocking_requirement_ids),
            "requirements": [
                {
                    "id": r.requirement_id,
                    "capability": r.capability,
                    "required": r.required,
                    "disposition": r.disposition.value,
                    "observed_version": r.observed_version,
                    "observed_path": r.observed_path,
                    "blocking": r.blocking,
                    "reason": r.reason,
                }
                for r in items
            ],
        }
        details_text = sanitize_diagnostic_text(json.dumps(diagnostic_payload, indent=2, sort_keys=True))

        return cls(
            project_id=project_id,
            task_id=task_id,
            state=state,
            status_label=STATUS_DISPLAY_LABELS[state],
            explanation=READINESS_EXPLANATION_TEXT[state],
            is_ready=is_ready,
            is_stale=effective_stale,
            stale_reason=effective_stale_reason,
            project_summary=project_summary,
            task_delta=task_delta,
            requirements=items,
            can_prepare=can_prepare,
            prepare_disabled_reason=disabled_reason,
            can_refresh=True,
            resolver_result_digest=proj_digest,
            inventory_digest=active_result.inventory_digest,
            evaluated_at=active_result.evaluated_at,
            diagnostic_details=details_text,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "state": self.state.value,
            "status_label": self.status_label,
            "explanation": self.explanation,
            "is_ready": self.is_ready,
            "is_stale": self.is_stale,
            "stale_reason": self.stale_reason,
            "project_summary": self.project_summary.to_dict(),
            "task_delta": self.task_delta.to_dict() if self.task_delta else None,
            "requirements": [it.to_dict() for it in self.requirements],
            "can_prepare": self.can_prepare,
            "prepare_disabled_reason": self.prepare_disabled_reason,
            "can_refresh": self.can_refresh,
            "resolver_result_digest": self.resolver_result_digest,
            "inventory_digest": self.inventory_digest,
            "evaluated_at": self.evaluated_at,
            "diagnostic_details": self.diagnostic_details,
        }


@dataclass(frozen=True)
class EnvironmentCommandReceipt:
    """Receipt returned by canonical environment commands."""

    action: str
    project_id: str
    accepted: bool
    status: str
    message: str
    effects_count: int = 0
    result_digest: str | None = None
    diagnostics: tuple[str, ...] = ()


class CanonicalEnvironmentCommands:
    """Safe, non-authoritative command boundary for GUI actions.

    - Refresh: requests canonical inventory/resolver update; NEVER mutates task status.
    - Prepare: delegates strictly to ProjectLocalEnvironmentProvisioner; 0 effects if denied.
    """

    def __init__(
        self,
        project_root: Path | str,
        *,
        resolver_factory: Callable[[], ReadinessResult] | None = None,
        provisioner_factory: Callable[[], ProjectLocalEnvironmentProvisioner] | None = None,
        task_status_mutator: Callable[[str, str], None] | None = None,
    ) -> None:
        self.project_root = Path(project_root)
        self._resolver_factory = resolver_factory
        self._provisioner_factory = provisioner_factory
        self._task_status_mutator = task_status_mutator
        self.prepare_effects_executed: int = 0
        self.global_mutations_triggered: int = 0
        self.refresh_calls: int = 0

    def refresh(
        self,
        project_id: str,
        task_id: str | None = None,
    ) -> EnvironmentCommandReceipt:
        """Perform a canonical refresh.

        GUI_REFRESH_MUTATES_WORKFLOW_STATUS = FALSE is strictly preserved:
        this method never touches task or workflow execution status.
        """
        self.refresh_calls += 1
        if self._task_status_mutator is not None:
            pass  # Strictly not called!

        result_digest: str | None = None
        if self._resolver_factory is not None:
            res = self._resolver_factory()
            result_digest = compute_resolver_result_digest(res)

        return EnvironmentCommandReceipt(
            action="REFRESH",
            project_id=project_id,
            accepted=True,
            status="REFRESHED",
            message="Stan środowiska został odświeżony przez kanoniczny resolver.",
            effects_count=0,
            result_digest=result_digest,
        )

    def prepare(
        self,
        project_id: str,
        plan: EnvironmentPlan,
        *,
        current_source_head: str,
        current_source_tree: str,
        operator_approved: bool = False,
    ) -> EnvironmentCommandReceipt:
        """Invoke project-local environment provisioning through NX-036.

        - DENIED_PREPARE_EFFECTS = 0
        - GUI_PREPARE_BYPASSES_POLICY = FALSE
        - GUI_TRIGGERED_GLOBAL_MUTATIONS = 0
        """
        # Check approval requirement
        if plan.approval_class is ApprovalClass.POLICY_DENIED:
            return EnvironmentCommandReceipt(
                action="PREPARE",
                project_id=project_id,
                accepted=False,
                status="POLICY_DENIED",
                message="Przygotowanie środowiska zablokowane przez politykę bezpieczeństwa.",
                effects_count=0,
                diagnostics=("POLICY_DENIED",),
            )
        if plan.approval_class is ApprovalClass.PRIVILEGE_REQUIRED:
            if not operator_approved:
                return EnvironmentCommandReceipt(
                    action="PREPARE",
                    project_id=project_id,
                    accepted=False,
                    status="APPROVAL_REQUIRED",
                    message="Przygotowanie środowiska wymaga jawnego zatwierdzenia przez operatora.",
                    effects_count=0,
                    diagnostics=("OPERATOR_APPROVAL_MISSING",),
                )

        provisioner = (
            self._provisioner_factory()
            if self._provisioner_factory is not None
            else ProjectLocalEnvironmentProvisioner(self.project_root)
        )

        result = provisioner.provision(
            plan,
            current_source_head=current_source_head,
            current_source_tree=current_source_tree,
        )

        effects_count = len(result.actual_effects)
        self.prepare_effects_executed += effects_count

        # Global mutations invariant check
        for effect in result.actual_effects:
            if effect.effect_class is not EffectClass.SAFE_PROJECT_LOCAL_MUTATION:
                self.global_mutations_triggered += 1

        accepted = result.status in {EnvironmentStatus.PROVISIONED, EnvironmentStatus.ALREADY_READY, EnvironmentStatus.REBUILT}
        return EnvironmentCommandReceipt(
            action="PREPARE",
            project_id=project_id,
            accepted=accepted,
            status=result.status.value,
            message="Provisioning zakończony" if accepted else "Provisioning odrzucony lub nieudany.",
            effects_count=effects_count,
            result_digest=result.manifest_digest,
            diagnostics=result.diagnostics,
        )

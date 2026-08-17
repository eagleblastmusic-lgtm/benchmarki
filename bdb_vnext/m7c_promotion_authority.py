"""M7c canonical Git promotion closure for inactive BDB vNext.

M7c does not create another Git writer.  M7a remains the only physical ref CAS
adapter and M7b remains a separate checkout synchronization effect.  M7c binds
an active M6c flow and exact capability Evidence to each prepared M7a effect,
then installs a durable cutover marker that M7a itself enforces immediately
before its external CAS.

Once a matching M7c cutover marker exists, direct M7a calls cannot fall back to
the pre-cutover M6a-only path.  ACTIVE requires current M6c authorization;
PAUSED fails closed.  The absence of a marker is retained only for the already
proven standalone M7a build-only unit tests and pre-cutover compatibility.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, NoReturn

from bdb_shared.evidence import canonical_json_bytes, semantic_digest
from bdb_vnext.control_store import (
    ControlStoreError,
    begin_control_write,
    commit_control_write,
    rollback_control_write,
)
from bdb_vnext.m6c_validation_authority import CanonicalValidationAuthority
from bdb_vnext.m7a_git_cas import CommitMetadataPolicy, PreparedGitCasAdapter, PreparedGitEffect
from bdb_vnext.m7b_checkout_sync import CheckoutSyncAdapter


M7C_CUTOVER_SCHEMA = "bdb-vnext-m7c-promotion-cutover-v1"
M7C_BINDING_SCHEMA = "bdb-vnext-m7c-promotion-binding-v1"
M7C_QUERY_SCHEMA = "bdb-vnext-m7c-promotion-query-v1"
M7C_AUTHORITY = "devmaster.bdb.vnext.git-promotion-authority"
M7C_MODE = "BUILD_ONLY_CANONICAL"


class M7cError(RuntimeError):
    """Typed fail-closed M7c promotion closure failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def _fail(
    code: str,
    message: str,
    *,
    details: Mapping[str, Any] | None = None,
) -> NoReturn:
    raise M7cError(code, message, details=details)


def _text(value: object, field: str, *, maximum: int = 1024) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or "\x00" in value
        or any(ord(char) < 32 for char in value)
    ):
        _fail("invalid_promotion_authority_input", f"{field} must be bounded non-empty text")
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class PromotionCutover:
    flow_id: str
    flow_revision_id: str
    policy_digest: str
    plan_digest: str
    registry_digest: str
    scope: str
    target_ref_prefix: str
    state: str
    activated_at: str
    updated_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": M7C_CUTOVER_SCHEMA,
            "authority": M7C_AUTHORITY,
            "mode": M7C_MODE,
            "flow_id": self.flow_id,
            "flow_revision_id": self.flow_revision_id,
            "policy_digest": self.policy_digest,
            "plan_digest": self.plan_digest,
            "registry_digest": self.registry_digest,
            "scope": self.scope,
            "target_ref_prefix": self.target_ref_prefix,
            "state": self.state,
            "activated_at": self.activated_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class PromotionBinding:
    effect_id: str
    flow_id: str
    flow_revision_id: str
    capability_bindings: Mapping[str, Mapping[str, str]]
    binding_digest: str
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": M7C_BINDING_SCHEMA,
            "authority": M7C_AUTHORITY,
            "effect_id": self.effect_id,
            "flow_id": self.flow_id,
            "flow_revision_id": self.flow_revision_id,
            "capability_bindings": {
                key: dict(value)
                for key, value in sorted(self.capability_bindings.items())
            },
            "binding_digest": self.binding_digest,
            "created_at": self.created_at,
        }


class CanonicalGitPromotionAuthority:
    """Bind M6c validation truth to M7a ref truth without becoming a ref writer."""

    def __init__(
        self,
        *,
        validation_authority: CanonicalValidationAuthority,
        m7a_adapter: PreparedGitCasAdapter,
        m7b_adapter: CheckoutSyncAdapter,
    ) -> None:
        if validation_authority is None or m7a_adapter is None or m7b_adapter is None:
            _fail("m7c_dependencies_required", "M7c requires M6c, M7a and M7b")
        connection = getattr(m7a_adapter, "_connection", None)
        if connection is None:
            _fail("m7c_dependencies_required", "M7c requires the unified vNext Control DB")

        def main_db_path(db_connection: Any) -> str:
            rows = db_connection.execute("PRAGMA database_list").fetchall()
            for _sequence, name, filename in rows:
                if str(name) == "main" and str(filename):
                    return str(Path(str(filename)).resolve())
            _fail("m7c_control_store_mismatch", "cannot establish exact main Control DB path")

        control_paths = {
            main_db_path(connection),
            main_db_path(validation_authority._connection),
            main_db_path(m7b_adapter._connection),
        }
        if len(control_paths) != 1:
            _fail(
                "m7c_control_store_mismatch",
                "M6c/M7a/M7b must use the same physical Control DB",
                details={"control_db_paths": sorted(control_paths)},
            )
        self.validation_authority = validation_authority
        self.m7a_adapter = m7a_adapter
        self.m7b_adapter = m7b_adapter
        self._connection = connection
        # M7a owns the physical ref call and must itself enforce M6c after the
        # cutover.  This is dependency wiring, not a second writer.
        m7a_adapter.canonical_validation_authority = validation_authority
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS m7c_promotion_cutovers (
              flow_id TEXT PRIMARY KEY,
              flow_revision_id TEXT NOT NULL,
              policy_digest TEXT NOT NULL,
              plan_digest TEXT NOT NULL,
              registry_digest TEXT NOT NULL,
              scope TEXT NOT NULL,
              target_ref_prefix TEXT NOT NULL,
              state TEXT NOT NULL,
              activated_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS m7c_promotion_bindings (
              effect_id TEXT PRIMARY KEY,
              flow_id TEXT NOT NULL,
              flow_revision_id TEXT NOT NULL,
              capability_bindings_json BLOB NOT NULL,
              binding_digest TEXT NOT NULL UNIQUE,
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS m7c_bindings_by_flow
              ON m7c_promotion_bindings(flow_id, flow_revision_id);
            """
        )

    def _begin(self) -> None:
        try:
            begin_control_write(self._connection)
        except ControlStoreError as exc:
            _fail(exc.code, str(exc))

    def _commit(self) -> None:
        try:
            commit_control_write(self._connection)
        except ControlStoreError as exc:
            _fail(exc.code, str(exc))

    def _rollback(self) -> None:
        try:
            rollback_control_write(self._connection)
        except ControlStoreError as exc:
            _fail(exc.code, str(exc))

    @staticmethod
    def _cutover_from_row(row: tuple[Any, ...]) -> PromotionCutover:
        return PromotionCutover(
            flow_id=str(row[0]),
            flow_revision_id=str(row[1]),
            policy_digest=str(row[2]),
            plan_digest=str(row[3]),
            registry_digest=str(row[4]),
            scope=str(row[5]),
            target_ref_prefix=str(row[6]),
            state=str(row[7]),
            activated_at=str(row[8]),
            updated_at=str(row[9]),
        )

    @staticmethod
    def _binding_from_row(row: tuple[Any, ...]) -> PromotionBinding:
        raw = json.loads(bytes(row[3]).decode("utf-8"))
        bindings = {
            str(key): {
                "obligation_id": str(value["obligation_id"]),
                "evidence_id": str(value["evidence_id"]),
            }
            for key, value in raw.items()
        }
        return PromotionBinding(
            effect_id=str(row[0]),
            flow_id=str(row[1]),
            flow_revision_id=str(row[2]),
            capability_bindings=bindings,
            binding_digest=str(row[4]),
            created_at=str(row[5]),
        )

    def get_cutover(self, flow_id: str) -> PromotionCutover | None:
        flow_id = _text(flow_id, "flow_id", maximum=256)
        row = self._connection.execute(
            "SELECT flow_id,flow_revision_id,policy_digest,plan_digest,registry_digest,scope,"
            "target_ref_prefix,state,activated_at,updated_at FROM m7c_promotion_cutovers WHERE flow_id=?",
            (flow_id,),
        ).fetchone()
        return self._cutover_from_row(row) if row else None

    def get_binding(self, effect_id: str) -> PromotionBinding | None:
        effect_id = _text(effect_id, "effect_id", maximum=192)
        row = self._connection.execute(
            "SELECT effect_id,flow_id,flow_revision_id,capability_bindings_json,binding_digest,created_at "
            "FROM m7c_promotion_bindings WHERE effect_id=?",
            (effect_id,),
        ).fetchone()
        return self._binding_from_row(row) if row else None

    def activate_cutover(
        self,
        *,
        flow_id: str,
        target_ref_prefix: str = "refs/bdb-vnext/",
    ) -> PromotionCutover:
        flow_id = _text(flow_id, "flow_id", maximum=256)
        target_ref_prefix = _text(target_ref_prefix, "target_ref_prefix", maximum=512)
        if not target_ref_prefix.startswith("refs/bdb-vnext/"):
            _fail("production_ref_forbidden", "M7c build-only cutover may cover only refs/bdb-vnext/")
        flow = self.validation_authority.get_active_flow(flow_id)
        if flow is None:
            _fail("validation_flow_inactive", "M7c cutover requires an active M6c flow")

        # Overlapping cutover namespaces would create two policy heads for the
        # same physical ref class.  Fail closed rather than choose one by order.
        rows = self._connection.execute(
            "SELECT flow_id,target_ref_prefix FROM m7c_promotion_cutovers WHERE flow_id<>?",
            (flow_id,),
        ).fetchall()
        for other_flow, other_prefix in rows:
            other = str(other_prefix)
            if target_ref_prefix.startswith(other) or other.startswith(target_ref_prefix):
                _fail(
                    "promotion_cutover_overlap",
                    "another M7c flow already covers an overlapping ref namespace",
                    details={"other_flow_id": str(other_flow), "other_prefix": other},
                )

        now = _now()
        current = self.get_cutover(flow_id)
        activated_at = current.activated_at if current else now
        self._begin()
        try:
            self._connection.execute(
                "INSERT INTO m7c_promotion_cutovers VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(flow_id) DO UPDATE SET "
                "flow_revision_id=excluded.flow_revision_id,policy_digest=excluded.policy_digest,"
                "plan_digest=excluded.plan_digest,registry_digest=excluded.registry_digest,"
                "scope=excluded.scope,target_ref_prefix=excluded.target_ref_prefix,state='ACTIVE',"
                "updated_at=excluded.updated_at",
                (
                    flow.flow_id,
                    flow.revision_id,
                    flow.policy_digest,
                    flow.plan_digest,
                    flow.registry_digest,
                    flow.scope,
                    target_ref_prefix,
                    "ACTIVE",
                    activated_at,
                    now,
                ),
            )
            self._commit()
        except Exception:
            if self._connection.in_transaction:
                self._rollback()
            raise
        result = self.get_cutover(flow_id)
        assert result is not None
        return result

    def pause_cutover(self, flow_id: str) -> PromotionCutover:
        current = self.get_cutover(flow_id)
        if current is None:
            _fail("promotion_cutover_missing", "M7c cutover does not exist")
        now = _now()
        self._begin()
        try:
            self._connection.execute(
                "UPDATE m7c_promotion_cutovers SET state='PAUSED',updated_at=? WHERE flow_id=?",
                (now, current.flow_id),
            )
            self._commit()
        except Exception:
            if self._connection.in_transaction:
                self._rollback()
            raise
        result = self.get_cutover(current.flow_id)
        assert result is not None
        return result

    def _normalize_bindings(
        self,
        *,
        flow_id: str,
        capability_bindings: Mapping[str, Mapping[str, str]],
    ) -> tuple[Any, dict[str, dict[str, str]]]:
        flow = self.validation_authority.get_active_flow(flow_id)
        if flow is None:
            _fail("validation_flow_inactive", "M7c requires the active M6c flow")
        required = set(flow.required_capabilities)
        if set(capability_bindings) != required:
            _fail(
                "promotion_evidence_coverage_mismatch",
                "M7c capability binding set must exactly equal the active M6c flow",
                details={
                    "required_capabilities": sorted(required),
                    "provided_capabilities": sorted(capability_bindings),
                },
            )
        normalized: dict[str, dict[str, str]] = {}
        for capability in sorted(required):
            value = capability_bindings.get(capability)
            if not isinstance(value, Mapping):
                _fail("invalid_promotion_binding", "each capability binding must be a mapping")
            normalized[capability] = {
                "obligation_id": _text(value.get("obligation_id"), "obligation_id", maximum=192),
                "evidence_id": _text(value.get("evidence_id"), "evidence_id", maximum=192),
            }
        return flow, normalized

    def prepare(
        self,
        *,
        flow_id: str,
        capability_bindings: Mapping[str, Mapping[str, str]],
        candidate: Any,
        repository: Any,
        work_id: str,
        run_id: str,
        target_ref: str,
        expected_old_oid: str,
        metadata: CommitMetadataPolicy,
        intent_revision_id: str,
        fault: str | None = None,
    ) -> PreparedGitEffect:
        cutover = self.get_cutover(flow_id)
        if cutover is None:
            _fail("promotion_cutover_missing", "M7c cutover must be installed before preparing canonical promotion")
        if cutover.state != "ACTIVE":
            _fail("promotion_cutover_paused", "M7c promotion cutover is paused")
        flow, normalized = self._normalize_bindings(
            flow_id=flow_id,
            capability_bindings=capability_bindings,
        )
        if flow.revision_id != cutover.flow_revision_id:
            _fail("promotion_policy_stale", "M6c flow revision changed after M7c cutover")
        if not target_ref.startswith(cutover.target_ref_prefix):
            _fail("promotion_ref_outside_cutover", "target ref is outside the active M7c namespace")

        effect = self.m7a_adapter.prepare(
            candidate=candidate,
            repository=repository,
            work_id=work_id,
            run_id=run_id,
            target_ref=target_ref,
            expected_old_oid=expected_old_oid,
            metadata=metadata,
            intent_revision_id=intent_revision_id,
            validation_policy_digest=flow.policy_digest,
            check_plan_digest=flow.plan_digest,
            obligation_ids=tuple(
                normalized[capability]["obligation_id"]
                for capability in sorted(normalized)
            ),
            scope=flow.scope,
            fault=fault,
        )

        material = {
            "schema": M7C_BINDING_SCHEMA,
            "effect_id": effect.effect_id,
            "flow_id": flow.flow_id,
            "flow_revision_id": flow.revision_id,
            "capability_bindings": normalized,
        }
        binding_digest = semantic_digest(material)
        existing = self.get_binding(effect.effect_id)
        if existing is not None:
            if existing.binding_digest != binding_digest:
                _fail("promotion_binding_conflict", "M7a effect already has a different M7c evidence binding")
            return effect

        created_at = _now()
        self._begin()
        try:
            self._connection.execute(
                "INSERT INTO m7c_promotion_bindings VALUES (?,?,?,?,?,?)",
                (
                    effect.effect_id,
                    flow.flow_id,
                    flow.revision_id,
                    canonical_json_bytes(normalized),
                    binding_digest,
                    created_at,
                ),
            )
            self._commit()
        except sqlite3.IntegrityError:
            if self._connection.in_transaction:
                self._rollback()
            replay = self.get_binding(effect.effect_id)
            if replay is None or replay.binding_digest != binding_digest:
                _fail("promotion_binding_conflict", "M7c promotion binding identity conflicted")
        except Exception:
            if self._connection.in_transaction:
                self._rollback()
            raise
        return effect

    def apply_if_safe(
        self,
        *,
        effect_id: str,
        approval_id: str,
        now: str | None = None,
        fault: str | None = None,
    ) -> PreparedGitEffect:
        binding = self.get_binding(effect_id)
        if binding is None:
            _fail("promotion_binding_missing", "canonical M7c promotion binding does not exist")
        cutover = self.get_cutover(binding.flow_id)
        if cutover is None or cutover.state != "ACTIVE":
            _fail("promotion_cutover_paused", "canonical promotion cutover is not active")
        flow = self.validation_authority.get_active_flow(binding.flow_id)
        if flow is None or flow.revision_id != binding.flow_revision_id or flow.revision_id != cutover.flow_revision_id:
            _fail("promotion_policy_stale", "active M6c flow no longer matches the prepared M7c binding")
        return self.m7a_adapter.apply_if_safe(
            effect_id=effect_id,
            approval_id=approval_id,
            now=now,
            fault=fault,
        )

    def _checkout_for_source(self, effect_id: str) -> Any | None:
        row = self._connection.execute(
            "SELECT effect_id FROM m7b_checkout_effects WHERE source_promotion_effect_id=?",
            (effect_id,),
        ).fetchone()
        if row is None:
            return None
        return self.m7b_adapter.get(effect_id=str(row[0]))

    def query(self, effect_id: str) -> dict[str, Any]:
        binding = self.get_binding(effect_id)
        if binding is None:
            _fail("promotion_binding_missing", "M7c promotion binding does not exist")
        cutover = self.get_cutover(binding.flow_id)
        source = self.m7a_adapter.get(effect_id=effect_id)
        if source is None:
            _fail("git_effect_missing", "M7c-bound M7a effect does not exist")
        checkout = self._checkout_for_source(effect_id)
        payload: dict[str, Any] = {
            "schema": M7C_QUERY_SCHEMA,
            "authority": M7C_AUTHORITY,
            "mode": M7C_MODE,
            "production_activation": False,
            "cutover": cutover.as_dict() if cutover else None,
            "binding": binding.as_dict(),
            "source_promotion": source.as_dict(),
            "checkout_sync": checkout.as_dict() if checkout else {
                "state": "NOT_PREPARED",
                "effect_certainty": "NOT_ASSESSED",
            },
        }
        payload["query_digest"] = semantic_digest(payload)
        return payload


__all__ = [
    "CanonicalGitPromotionAuthority",
    "M7C_AUTHORITY",
    "M7C_BINDING_SCHEMA",
    "M7C_CUTOVER_SCHEMA",
    "M7C_MODE",
    "M7C_QUERY_SCHEMA",
    "M7cError",
    "PromotionBinding",
    "PromotionCutover",
]

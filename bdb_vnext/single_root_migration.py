"""Plan-bound migration from the retired AppData runtime to the repo runtime.

The old runtime remains authoritative until the migration result is complete.
Copying bytes does not activate the target: Browser verification, Native route,
Bootstrap and M9b remain independent canonical gates. Source retirement is a
separate operation which is permitted only after all of those gates agree.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, NoReturn

from bdb_shared.evidence import canonical_json_bytes, semantic_digest
from bdb_vnext.bootstrap import BootstrapError
from bdb_vnext.control_store import (
    CONTROL_DB_USER_VERSION,
    CONTROL_SCHEMA_CHECKSUM,
    ControlStoreError,
    ensure_layout_identity,
    expected_identity,
    finalize_control_store,
    prepare_control_store,
)
from bdb_vnext.m11c_active_reader import observe_bootstrap_activation
from bdb_vnext.m11c_windows_clients import (
    M11cClientError,
    observe_windows_native_routes,
    query_client_plan,
    require_client_verification,
)
from bdb_vnext.m3c_admission import (
    CanonicalVNextAdmissionAuthority,
    M3C_AUTHORITY_ID,
    M3C_CONTROL_SCHEMA,
    M3C_KILL_SWITCH_SCHEMA,
    M3C_PROTOCOL_GENERATION,
    M3C_WRITER_ID,
)
from bdb_vnext.m9b_activation import read_activation
from bdb_vnext.project_catalog import ProjectCatalog


MIGRATION_PLAN_SCHEMA = "bdb-vnext-single-root-migration-plan-v1"
MIGRATION_STATE_SCHEMA = "bdb-vnext-single-root-migration-state-v1"
MIGRATION_RESULT_SCHEMA = "bdb-vnext-single-root-migration-result-v1"
MIGRATION_RETIREMENT_SCHEMA = "bdb-vnext-single-root-retirement-v1"
_MAX_FILES = 4096
_MAX_FILE_BYTES = 64 * 1024 * 1024
_MAX_TOTAL_BYTES = 512 * 1024 * 1024
_DIGEST_PREFIX = "sha256:"
_TRANSIENT_NAMES = {"control.db-wal", "control.db-shm", "execution.lock"}
_KNOWN_INITIALIZING_TABLES = {
    "m3a_consumer_bindings",
    "m3a_intent_revisions",
    "m3a_submissions",
    "m3a_tasks",
    "m3c_kill_switch",
    "vnext_control_metadata",
}


class SingleRootMigrationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> NoReturn:
    raise SingleRootMigrationError(code, message)


def _absolute(value: str | Path, *, field: str) -> Path:
    path = Path(value).expanduser().absolute()
    if not path.is_absolute():
        _fail("migration_path_invalid", f"{field} must be absolute")
    return path


def _identifier(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 96:
        _fail("migration_identity_invalid", f"{field} is invalid")
    if any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_." for ch in value):
        _fail("migration_identity_invalid", f"{field} is invalid")
    return value


def _sha40(value: object, *, field: str) -> str:
    if not isinstance(value, str) or len(value) != 40 or any(ch not in "0123456789abcdef" for ch in value):
        _fail("migration_identity_invalid", f"{field} must be a lowercase Git SHA")
    return value


def _digest(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.startswith(_DIGEST_PREFIX) or len(value) != 71:
        _fail("migration_identity_invalid", f"{field} must be an exact sha256 digest")
    if any(ch not in "0123456789abcdef" for ch in value[7:]):
        _fail("migration_identity_invalid", f"{field} must be an exact sha256 digest")
    return value


def _document_digest(value: Mapping[str, Any]) -> str:
    return semantic_digest(dict(value))


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return _DIGEST_PREFIX + digest.hexdigest()


def _is_reparse(path: Path) -> bool:
    stat_value = path.lstat()
    attributes = getattr(stat_value, "st_file_attributes", 0)
    reparse = getattr(__import__("stat"), "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(attributes & reparse)


def _assert_plain_tree(root: Path) -> None:
    current = root
    while True:
        if current.exists() and _is_reparse(current):
            _fail("migration_reparse_point", f"reparse point is forbidden: {current}")
        if current.parent == current:
            break
        current = current.parent
    if root.exists():
        for path in root.rglob("*"):
            if _is_reparse(path):
                _fail("migration_reparse_point", f"reparse point is forbidden: {path}")


def _under(path: Path, root: Path) -> bool:
    try:
        path.absolute().relative_to(root.absolute())
        return True
    except ValueError:
        return False


def _inventory(root: Path) -> list[dict[str, Any]]:
    if not root.is_dir():
        _fail("migration_source_missing", "source runtime root is missing")
    _assert_plain_tree(root)
    records: list[dict[str, Any]] = []
    total = 0
    files = sorted((item for item in root.rglob("*") if item.is_file()), key=lambda item: item.as_posix().casefold())
    for path in files:
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        if size > _MAX_FILE_BYTES:
            _fail("migration_file_too_large", f"{relative} exceeds the migration bound")
        total += size
        if total > _MAX_TOTAL_BYTES:
            _fail("migration_subject_too_large", "source runtime exceeds the migration bound")
        records.append({"path": relative, "size_bytes": size, "sha256": _file_digest(path)})
        if len(records) > _MAX_FILES:
            _fail("migration_file_count_exceeded", "source runtime contains too many files")
    return records


def _classify(records: Sequence[Mapping[str, Any]], migration_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    migrated: list[dict[str, Any]] = []
    obsolete: list[dict[str, Any]] = []
    required = {
        "control/control.db",
        "control/control.db.seal.json",
        "control/m3c-control.json",
        "control/m3c-kill-switch.json",
        "control/project-catalog.json",
        "config/m3a-shadow.json",
        "config/m9b-activation.json",
    }
    observed = {str(item["path"]) for item in records}
    missing = sorted(required - observed)
    if missing:
        _fail("migration_subject_incomplete", f"required source state is missing: {', '.join(missing)}")
    for raw in records:
        item = dict(raw)
        relative = PurePosixPath(str(item["path"]))
        parts = relative.parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            _fail("migration_path_invalid", "source inventory contains an unsafe path")
        if parts[0] == "control":
            if relative.name in _TRANSIENT_NAMES or relative.name.endswith(".tmp"):
                _fail("migration_source_not_quiesced", f"transient source state is present: {relative}")
            if len(parts) == 2 or parts[1] == "project-memory":
                item["target_path"] = relative.as_posix()
                item["category"] = "CANONICAL_MUTABLE_STATE"
                migrated.append(item)
                continue
        if relative.as_posix() in {
            "config/bdb-vnext.json",
            "config/m3a-shadow.json",
            "config/m9b-activation.json",
        }:
            item["target_path"] = relative.as_posix()
            item["category"] = "CANONICAL_RUNTIME_STATE"
            migrated.append(item)
            continue
        if len(parts) >= 2 and parts[0] == "browser" and parts[1] == "outbox":
            if relative.name in _TRANSIENT_NAMES or relative.name.endswith(".tmp"):
                _fail("migration_source_not_quiesced", f"transient source state is present: {relative}")
            item["target_path"] = relative.as_posix()
            item["category"] = "CANONICAL_TRANSPORT_RECOVERY_STATE"
            migrated.append(item)
            continue
        if parts[0] == "recovery":
            suffix = PurePosixPath(*parts[1:]).as_posix()
            item["target_path"] = f"recovery/appdata-legacy/{migration_id}/{suffix}"
            item["category"] = "IMMUTABLE_RECOVERY_EVIDENCE"
            migrated.append(item)
            continue
        if parts[0] == "clients" or relative.as_posix() == "config/native-host.json":
            item["category"] = "OBSOLETE_DEPLOYED_COPY"
            obsolete.append(item)
            continue
        _fail("migration_unknown_source_state", f"unclassified source file must not be dropped: {relative}")
    if not any(str(item["path"]).startswith("control/project-memory/") for item in migrated):
        _fail("migration_subject_incomplete", "Project Memory is missing from the source runtime")
    return migrated, obsolete


def _read_json(path: Path, *, field: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SingleRootMigrationError("migration_record_invalid", f"{field} cannot be read") from exc
    if not isinstance(value, Mapping):
        _fail("migration_record_invalid", f"{field} must contain an object")
    return dict(value)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json_bytes(dict(value))
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _write_immutable(path: Path, value: Mapping[str, Any]) -> None:
    payload = canonical_json_bytes(dict(value))
    if path.exists():
        if path.read_bytes() != payload:
            _fail("migration_record_conflict", f"immutable record differs: {path.name}")
        return
    _atomic_json(path, value)


def _record_root(target: Path) -> Path:
    return target / "recovery" / "single-root-migration"


def _plan_path(target: Path, migration_id: str) -> Path:
    return _record_root(target) / f"{migration_id}.plan.json"


def _state_path(target: Path, migration_id: str) -> Path:
    return _record_root(target) / f"{migration_id}.state.json"


def _result_path(target: Path, migration_id: str) -> Path:
    return _record_root(target) / f"{migration_id}.result.json"


def _retirement_path(target: Path, migration_id: str) -> Path:
    return _record_root(target) / f"{migration_id}.retirement.json"


@contextmanager
def _migration_lock(target: Path, migration_id: str) -> Iterator[None]:
    lock_path = _record_root(target) / f"{migration_id}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        handle.seek(0)
        if handle.read(1) == b"":
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise SingleRootMigrationError("migration_busy", "single-root migration is already running") from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise SingleRootMigrationError("migration_busy", "single-root migration is already running") from exc
        yield
    finally:
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _client_subject(target: Path, *, expected_plan_sha256: str, source_head: str, source_tree: str) -> dict[str, Any]:
    try:
        plan = dict(query_client_plan(runtime_root=target)["plan"])
    except (M11cClientError, KeyError) as exc:
        raise SingleRootMigrationError(getattr(exc, "code", "migration_client_unavailable"), str(exc)) from exc
    if plan.get("client_plan_sha256") != expected_plan_sha256:
        _fail("migration_client_mismatch", "target client plan differs")
    if plan.get("source_head") != source_head or plan.get("source_tree") != source_tree:
        _fail("migration_client_mismatch", "target clients bind another source")
    if plan.get("production_activation_performed") is not False:
        _fail("migration_client_mismatch", "target client plan claims activation")
    for field in ("browser_bundle_root", "native_manifest_path", "native_host_executable", "native_config_path"):
        value = plan.get(field)
        if not isinstance(value, str) or not _under(Path(value), target):
            _fail("migration_client_path_escape", f"{field} escapes the target runtime")
    return plan


def _load_plan(target: Path, migration_id: str, expected_plan_sha256: str | None = None) -> dict[str, Any]:
    plan = _read_json(_plan_path(target, migration_id), field="migration plan")
    if plan.get("schema") != MIGRATION_PLAN_SCHEMA or plan.get("migration_id") != migration_id:
        _fail("migration_plan_invalid", "migration plan identity differs")
    supplied = _digest(plan.get("plan_sha256"), field="plan_sha256")
    payload = dict(plan)
    payload.pop("plan_sha256", None)
    if _document_digest(payload) != supplied:
        _fail("migration_plan_digest_mismatch", "migration plan digest differs")
    if expected_plan_sha256 is not None and supplied != _digest(expected_plan_sha256, field="expected_plan_sha256"):
        _fail("migration_plan_stale", "operator approval binds another migration plan")
    return plan


def prepare_single_root_migration(
    *,
    source_runtime_root: str | Path,
    target_runtime_root: str | Path,
    legacy_runtime_root: str | Path,
    migration_id: str,
    source_head: str,
    source_tree: str,
    expected_client_plan_sha256: str,
) -> dict[str, Any]:
    source = _absolute(source_runtime_root, field="source_runtime_root")
    target = _absolute(target_runtime_root, field="target_runtime_root")
    legacy = _absolute(legacy_runtime_root, field="legacy_runtime_root")
    migration_id = _identifier(migration_id, field="migration_id")
    source_head = _sha40(source_head, field="source_head")
    source_tree = _sha40(source_tree, field="source_tree")
    client_sha = _digest(expected_client_plan_sha256, field="expected_client_plan_sha256")
    if _under(source, target) or _under(target, source) or _under(target, legacy):
        _fail("migration_root_overlap", "source, target and Legacy roots must remain isolated")
    if not target.is_dir():
        _fail("migration_target_missing", "repo-local target runtime is missing")
    _assert_plain_tree(target)
    existing_plan = _plan_path(target, migration_id)
    if existing_plan.exists():
        return {"status": "PREPARED", "plan": _load_plan(target, migration_id), "replayed": True}
    _client_subject(target, expected_plan_sha256=client_sha, source_head=source_head, source_tree=source_tree)
    records = _inventory(source)
    migrated, obsolete = _classify(records, migration_id)
    for item in migrated:
        destination = target.joinpath(*PurePosixPath(item["target_path"]).parts)
        if destination.exists():
            _fail("migration_target_not_empty", f"target state already exists: {item['target_path']}")
    payload = {
        "schema": MIGRATION_PLAN_SCHEMA,
        "migration_id": migration_id,
        "source_runtime_root": str(source),
        "target_runtime_root": str(target),
        "legacy_runtime_root": str(legacy),
        "source_head": source_head,
        "source_tree": source_tree,
        "client_plan_sha256": client_sha,
        "source_subject_sha256": _document_digest({"files": records}),
        "migrated_files": migrated,
        "obsolete_deployed_files": obsolete,
        "source_file_count": len(records),
        "source_size_bytes": sum(int(item["size_bytes"]) for item in records),
        "activation_performed": False,
        "source_retired": False,
    }
    plan = {**payload, "plan_sha256": _document_digest(payload)}
    _write_immutable(existing_plan, plan)
    state_payload = {"schema": MIGRATION_STATE_SCHEMA, "migration_id": migration_id, "plan_sha256": plan["plan_sha256"], "phase": "PREPARED"}
    _atomic_json(_state_path(target, migration_id), {**state_payload, "state_sha256": _document_digest(state_payload)})
    return {"status": "PREPARED", "plan": plan, "replayed": False}


def _copy_exact(source: Path, target: Path, *, size: int, digest: str) -> None:
    if target.exists():
        if not target.is_file() or target.stat().st_size != size or _file_digest(target) != digest:
            _fail("migration_target_conflict", f"target file differs: {target}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.migration-partial")
    if temporary.exists():
        _fail("migration_partial_conflict", f"partial target already exists: {temporary}")
    try:
        with source.open("rb") as reader, temporary.open("xb") as writer:
            while block := reader.read(1024 * 1024):
                writer.write(block)
            writer.flush()
            os.fsync(writer.fileno())
        if temporary.stat().st_size != size or _file_digest(temporary) != digest:
            _fail("migration_copy_mismatch", f"copied bytes differ: {target}")
        os.replace(temporary, target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _complete_known_empty_initializing_control(target: Path, legacy: Path, migration_id: str) -> dict[str, Any]:
    """Finish only the exact empty historical initialization found in AppData.

    This is deliberately not a general repair path.  It accepts one closed
    legacy shape with no semantic records, preserves its exact bytes as
    immutable recovery evidence, and lets the existing domain stores create
    the current layout before the normal Control DB seal is finalized.
    """

    database = target / "control" / "control.db"
    seal_path = target / "control" / "control.db.seal.json"
    seal = _read_json(seal_path, field="initializing Control DB seal")
    supplied = dict(seal)
    seal_digest = supplied.pop("seal_digest", None)
    expected = expected_identity()
    if (
        seal_digest != _document_digest(supplied)
        or set(supplied) != {
            "schema",
            "state",
            "instance_id",
            "store_id",
            "generation_id",
            "config_generation",
            "schema_checksum",
            "user_version",
        }
        or supplied.get("schema") != "bdb-vnext-control-seal-v1"
        or supplied.get("state") != "INITIALIZING"
        or supplied.get("user_version") != 0
        or not isinstance(supplied.get("instance_id"), str)
        or not supplied["instance_id"]
        or supplied.get("store_id") != expected["store_id"]
        or supplied.get("generation_id") != expected["generation_id"]
        or supplied.get("config_generation") != expected["config_generation"]
        or supplied.get("schema_checksum") != CONTROL_SCHEMA_CHECKSUM
    ):
        _fail("migration_control_initialization_unknown", "historical Control DB seal is not the exact known initialization")

    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro&immutable=1", uri=True)
    try:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        identity = {
            str(row[0]): str(row[1])
            for row in connection.execute("SELECT key,value FROM vnext_control_metadata ORDER BY key").fetchall()
        }
        semantic_counts = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in sorted(_KNOWN_INITIALIZING_TABLES - {"vnext_control_metadata", "m3c_kill_switch"})
        }
        kill = connection.execute(
            "SELECT schema,authority_id,protocol_generation,writer_id,admission_enabled "
            "FROM m3c_kill_switch ORDER BY id"
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise SingleRootMigrationError("migration_control_initialization_unknown", "historical Control DB cannot be verified") from exc
    finally:
        connection.close()
    if (
        integrity != "ok"
        or violations
        or version != 0
        or tables != _KNOWN_INITIALIZING_TABLES
        or identity != expected
        or any(semantic_counts.values())
        or kill != [(M3C_KILL_SWITCH_SCHEMA, M3C_AUTHORITY_ID, M3C_PROTOCOL_GENERATION, M3C_WRITER_ID, 1)]
    ):
        _fail("migration_control_initialization_unknown", "historical Control DB contains non-empty or foreign authority state")

    archive = target / "recovery" / "single-root-migration" / f"{migration_id}.initializing-control"
    _copy_exact(database, archive / "control.db", size=database.stat().st_size, digest=_file_digest(database))
    _copy_exact(seal_path, archive / "control.db.seal.json", size=seal_path.stat().st_size, digest=_file_digest(seal_path))

    from bdb_vnext.candidate import CandidateStore
    from bdb_vnext.content_store import DurableBindingStore
    from bdb_vnext.engineering_loop import EditorPort
    from bdb_vnext.m3c_admission import _open_vnext_admission_composition
    from bdb_vnext.m4a_work_kernel import WorkKernelStore
    from bdb_vnext.m4c_evidence import EvidenceStore
    from bdb_vnext.n4_publication import PublicationStore

    bindings = admission = work_kernel = candidate = evidence = publication = None
    try:
        bindings = DurableBindingStore(target)
        admission = _open_vnext_admission_composition(
            target,
            legacy_root=legacy,
            existing_outbox=(target / "browser" / "outbox" / "anchor.json").is_file(),
        )
        work_kernel = WorkKernelStore.open(target, task_authority=admission.authority, legacy_root=legacy)
        candidate = CandidateStore(target, content_store=bindings.content_store, work_kernel=work_kernel)
        evidence = EvidenceStore(target, content_store=bindings.content_store, candidate_store=candidate)
        EditorPort(candidate, evidence_store=evidence)
        publication = PublicationStore(
            target,
            content_store=bindings.content_store,
            task_authority=admission.authority,
            work_kernel=work_kernel,
            candidate_store=candidate,
            evidence_store=evidence,
        )
        ensure_layout_identity(work_kernel._connection)
        current_seal = _read_json(seal_path, field="completed Control DB seal")
        if current_seal.get("state") == "INITIALIZING":
            finalize_control_store(work_kernel._connection)
    finally:
        for store in (publication, evidence, candidate, work_kernel, admission, bindings):
            if store is not None:
                store.close()
    return {
        "upgraded_from_initializing": True,
        "initializing_database_sha256": _file_digest(archive / "control.db"),
        "initializing_seal_sha256": _file_digest(archive / "control.db.seal.json"),
    }


def _verify_control(target: Path, legacy: Path, *, migration_id: str) -> dict[str, Any]:
    upgrade: dict[str, Any] = {"upgraded_from_initializing": False}
    try:
        database = prepare_control_store(target)
    except ControlStoreError as exc:
        if exc.code != "control_initialization_incomplete":
            raise
        upgrade = _complete_known_empty_initializing_control(target, legacy, migration_id)
        database = prepare_control_store(target)
    authority = CanonicalVNextAdmissionAuthority.open(target, legacy_root=legacy)
    authority.close()
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()
    if integrity != "ok" or violations or version != CONTROL_DB_USER_VERSION:
        _fail("migration_control_invalid", "migrated Control DB failed integrity/version verification")
    marker = _read_json(target / "control" / "m3c-control.json", field="M3c control")
    if marker.get("schema") != M3C_CONTROL_SCHEMA or "production_intake" in marker:
        _fail("migration_m3c_invalid", "M3c did not migrate to the unambiguous v2 contract")
    catalog = ProjectCatalog(target)
    projects = catalog.read()
    return {
        **upgrade,
        "database_sha256": _file_digest(database),
        "integrity_check": integrity,
        "foreign_key_violations": len(violations),
        "user_version": version,
        "m3c_control_sha256": _file_digest(target / "control" / "m3c-control.json"),
        "project_catalog_sha256": _file_digest(catalog.path),
        "project_count": len(projects),
    }


def apply_single_root_migration(
    *,
    target_runtime_root: str | Path,
    migration_id: str,
    expected_plan_sha256: str,
    operator_approved: bool,
    fault_hook: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    if operator_approved is not True:
        _fail("migration_approval_required", "single-root migration requires exact operator approval")
    target = _absolute(target_runtime_root, field="target_runtime_root")
    migration_id = _identifier(migration_id, field="migration_id")
    with _migration_lock(target, migration_id):
        plan = _load_plan(target, migration_id, expected_plan_sha256)
        result_path = _result_path(target, migration_id)
        if result_path.exists():
            return {"status": "COMPLETED", "result": _read_json(result_path, field="migration result"), "replayed": True}
        source = _absolute(plan["source_runtime_root"], field="plan.source_runtime_root")
        legacy = _absolute(plan["legacy_runtime_root"], field="plan.legacy_runtime_root")
        current = _inventory(source)
        if _document_digest({"files": current}) != plan["source_subject_sha256"]:
            _fail("migration_source_changed", "source runtime changed after immutable planning")
        if fault_hook:
            fault_hook("before_first_copy")
        state_payload = {"schema": MIGRATION_STATE_SCHEMA, "migration_id": migration_id, "plan_sha256": plan["plan_sha256"], "phase": "COPYING"}
        _atomic_json(_state_path(target, migration_id), {**state_payload, "state_sha256": _document_digest(state_payload)})
        for index, item in enumerate(plan["migrated_files"]):
            source_path = source.joinpath(*PurePosixPath(item["path"]).parts)
            target_path = target.joinpath(*PurePosixPath(item["target_path"]).parts)
            _copy_exact(source_path, target_path, size=int(item["size_bytes"]), digest=str(item["sha256"]))
            if fault_hook:
                fault_hook(f"after_copy_{index}")
        if _document_digest({"files": _inventory(source)}) != plan["source_subject_sha256"]:
            _fail("migration_source_changed", "source runtime changed during migration")
        control = _verify_control(target, legacy, migration_id=migration_id)
        expected_database = next(item["sha256"] for item in plan["migrated_files"] if item["path"] == "control/control.db")
        if control["upgraded_from_initializing"] is not True and control["database_sha256"] != expected_database:
            _fail("migration_control_copy_mismatch", "Control DB bytes changed during canonical verification")
        mutable_during_upgrade = {"control/m3c-control.json"}
        if control["upgraded_from_initializing"] is True:
            mutable_during_upgrade.update({"control/control.db", "control/control.db.seal.json"})
        for item in plan["migrated_files"]:
            if item["path"] in mutable_during_upgrade:
                continue
            destination = target.joinpath(*PurePosixPath(item["target_path"]).parts)
            if destination.stat().st_size != int(item["size_bytes"]) or _file_digest(destination) != item["sha256"]:
                _fail("migration_readback_mismatch", f"migrated readback differs: {item['target_path']}")
        result_payload = {
            "schema": MIGRATION_RESULT_SCHEMA,
            "migration_id": migration_id,
            "plan_sha256": plan["plan_sha256"],
            "source_subject_sha256": plan["source_subject_sha256"],
            "target_runtime_root": str(target),
            "migrated_file_count": len(plan["migrated_files"]),
            "obsolete_file_count": len(plan["obsolete_deployed_files"]),
            "control": control,
            "source_preserved": True,
            "source_retired": False,
            "activation_performed": False,
        }
        result = {**result_payload, "result_sha256": _document_digest(result_payload)}
        _write_immutable(result_path, result)
        state_payload = {"schema": MIGRATION_STATE_SCHEMA, "migration_id": migration_id, "plan_sha256": plan["plan_sha256"], "phase": "COMPLETED"}
        _atomic_json(_state_path(target, migration_id), {**state_payload, "state_sha256": _document_digest(state_payload)})
        return {"status": "COMPLETED", "result": result, "replayed": False}


def retire_single_root_source(
    *,
    authority_root: str | Path,
    target_runtime_root: str | Path,
    migration_id: str,
    expected_plan_sha256: str,
    operator_approved: bool,
) -> dict[str, Any]:
    """Remove the exact old root only after every canonical live gate agrees."""

    if operator_approved is not True:
        _fail("retirement_approval_required", "source retirement requires exact operator approval")
    authority = _absolute(authority_root, field="authority_root")
    target = _absolute(target_runtime_root, field="target_runtime_root")
    migration_id = _identifier(migration_id, field="migration_id")
    with _migration_lock(target, migration_id):
        plan = _load_plan(target, migration_id, expected_plan_sha256)
        result = _read_json(_result_path(target, migration_id), field="migration result")
        if result.get("schema") != MIGRATION_RESULT_SCHEMA or result.get("plan_sha256") != plan["plan_sha256"]:
            _fail("migration_not_complete", "exact migration result is unavailable")
        retirement_path = _retirement_path(target, migration_id)
        if retirement_path.exists():
            return {"status": "RETIRED", "retirement": _read_json(retirement_path, field="retirement result"), "replayed": True}
        source = _absolute(plan["source_runtime_root"], field="source_runtime_root")
        if _document_digest({"files": _inventory(source)}) != plan["source_subject_sha256"]:
            _fail("retirement_source_changed", "old runtime changed after migration")
        client = _client_subject(target, expected_plan_sha256=plan["client_plan_sha256"], source_head=plan["source_head"], source_tree=plan["source_tree"])
        try:
            verification = require_client_verification(runtime_root=target, expected_client_plan_sha256=plan["client_plan_sha256"])
            routes = observe_windows_native_routes(runtime_root=target)
        except (M11cClientError, BootstrapError) as exc:
            raise SingleRootMigrationError(exc.code, str(exc)) from exc
        if routes.get("target_registered") is not True or routes.get("target_conflict") or routes.get("legacy_route_present"):
            _fail("retirement_route_not_exclusive", "Native route is not exact and Legacy-free")
        bootstrap = observe_bootstrap_activation(authority_root=authority)
        slots = bootstrap.get("slots")
        state = bootstrap.get("state")
        if bootstrap.get("status") != "ACTIVE" or not isinstance(slots, Mapping) or not isinstance(state, Mapping):
            _fail("retirement_bootstrap_invalid", "Bootstrap ACTIVE observation is incomplete")
        active = slots.get("ACTIVE")
        previous = slots.get("PREVIOUS")
        if not isinstance(active, Mapping) or not isinstance(previous, Mapping):
            _fail("retirement_bootstrap_invalid", "Bootstrap recovery slots are incomplete")
        if active.get("source_commit") != plan["source_head"] or state.get("production_activation_performed") is not True:
            _fail("retirement_bootstrap_mismatch", "Bootstrap ACTIVE differs from the repo-local source")
        for slot in (active, previous):
            root = slot.get("bundle_root")
            if isinstance(root, str) and _under(Path(root), source):
                _fail("retirement_source_referenced", "Bootstrap still references the old runtime")
        m9b = read_activation(target)
        if m9b is None or m9b.state != "ACTIVE" or m9b.source_head != plan["source_head"] or not m9b.writer_enabled or not m9b.intake_enabled:
            _fail("retirement_m9b_mismatch", "M9b is not ACTIVE for the repo-local source")
        control = _verify_control(
            target,
            _absolute(plan["legacy_runtime_root"], field="legacy_runtime_root"),
            migration_id=migration_id,
        )
        if verification.get("client_plan_sha256") != client["client_plan_sha256"]:
            _fail("retirement_browser_mismatch", "Browser verification differs from the target client")
        source_size = int(plan["source_size_bytes"])
        shutil.rmtree(source)
        if source.exists():
            _fail("retirement_delete_failed", "old runtime still exists after bounded removal")
        payload = {
            "schema": MIGRATION_RETIREMENT_SCHEMA,
            "migration_id": migration_id,
            "plan_sha256": plan["plan_sha256"],
            "source_runtime_root": str(source),
            "target_runtime_root": str(target),
            "source_subject_sha256": plan["source_subject_sha256"],
            "reclaimed_bytes": source_size,
            "bootstrap_state_sha256": state.get("state_sha256"),
            "client_verification_sha256": verification.get("verification_sha256"),
            "m9b_record_sha256": m9b.record_digest,
            "m3c_control_sha256": control["m3c_control_sha256"],
            "legacy_route_present": False,
            "retired": True,
        }
        retirement = {**payload, "retirement_sha256": _document_digest(payload)}
        _write_immutable(retirement_path, retirement)
        return {"status": "RETIRED", "retirement": retirement, "replayed": False}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bdb-vnext-single-root")
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    for name in ("source-runtime-root", "target-runtime-root", "legacy-runtime-root", "migration-id", "source-head", "source-tree", "client-plan-sha256"):
        prepare.add_argument(f"--{name}", required=True)
    apply = sub.add_parser("apply")
    for name in ("target-runtime-root", "migration-id", "plan-sha256"):
        apply.add_argument(f"--{name}", required=True)
    apply.add_argument("--approve", action="store_true")
    retire = sub.add_parser("retire")
    for name in ("authority-root", "target-runtime-root", "migration-id", "plan-sha256"):
        retire.add_argument(f"--{name}", required=True)
    retire.add_argument("--approve", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "prepare":
            output = prepare_single_root_migration(
                source_runtime_root=args.source_runtime_root,
                target_runtime_root=args.target_runtime_root,
                legacy_runtime_root=args.legacy_runtime_root,
                migration_id=args.migration_id,
                source_head=args.source_head,
                source_tree=args.source_tree,
                expected_client_plan_sha256=args.client_plan_sha256,
            )
        elif args.command == "apply":
            output = apply_single_root_migration(
                target_runtime_root=args.target_runtime_root,
                migration_id=args.migration_id,
                expected_plan_sha256=args.plan_sha256,
                operator_approved=args.approve,
            )
        else:
            output = retire_single_root_source(
                authority_root=args.authority_root,
                target_runtime_root=args.target_runtime_root,
                migration_id=args.migration_id,
                expected_plan_sha256=args.plan_sha256,
                operator_approved=args.approve,
            )
        print(canonical_json_bytes(output).decode("utf-8"))
        return 0
    except (SingleRootMigrationError, OSError, sqlite3.DatabaseError) as exc:
        error = {"status": "BLOCKED", "error_code": getattr(exc, "code", "single_root_migration_failed"), "error": str(exc)}
        print(canonical_json_bytes(error).decode("utf-8"))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "MIGRATION_PLAN_SCHEMA",
    "MIGRATION_RESULT_SCHEMA",
    "MIGRATION_RETIREMENT_SCHEMA",
    "SingleRootMigrationError",
    "apply_single_root_migration",
    "prepare_single_root_migration",
    "retire_single_root_source",
]

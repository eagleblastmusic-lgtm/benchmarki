"""Windows trust-boundary preflight for the external BDB Next Bootstrap.

M11a defines the authority policy but does not activate a candidate.  The
external authority lives under ProgramData and is writable only by SYSTEM or
Administrators; ordinary Users receive read/execute only.  Candidate/runtime
code is expected to run non-elevated, so possession of the Python package does
not imply authority to mutate the external slot pointer.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NoReturn

from bdb_shared.evidence import semantic_digest
from bdb_vnext.bootstrap import _absolute_path, _overlaps
from bdb_vnext.composition import GENERATION_ID, RUNTIME_ID


WINDOWS_TCB_SCHEMA = "bdb-vnext-bootstrap-windows-tcb-v1"
WINDOWS_ACL_WITNESS_SCHEMA = "bdb-vnext-bootstrap-windows-acl-v1"

SYSTEM_SID = "S-1-5-18"
ADMINISTRATORS_SID = "S-1-5-32-544"
USERS_SID = "S-1-5-32-545"
AUTHENTICATED_USERS_SID = "S-1-5-11"
EVERYONE_SID = "S-1-1-0"

_WRITE_RIGHTS = frozenset(
    {
        "FullControl",
        "Modify",
        "Write",
        "WriteData",
        "CreateFiles",
        "AppendData",
        "CreateDirectories",
        "WriteExtendedAttributes",
        "WriteAttributes",
        "Delete",
        "DeleteSubdirectoriesAndFiles",
        "ChangePermissions",
        "TakeOwnership",
    }
)
_REQUIRED_WRITERS = frozenset({SYSTEM_SID, ADMINISTRATORS_SID})
_LOW_PRIVILEGE = frozenset({USERS_SID, AUTHENTICATED_USERS_SID, EVERYONE_SID})
_MAX_ACL_OUTPUT_BYTES = 64 * 1024


class M11aWindowsTcbError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> NoReturn:
    raise M11aWindowsTcbError(code, message)


def default_windows_authority_root(program_data: str | Path | None = None) -> Path:
    value = program_data if program_data is not None else os.environ.get("PROGRAMDATA")
    if not value:
        _fail("programdata_unavailable", "ProgramData is required for the Windows Bootstrap authority")
    return Path(value).expanduser().absolute() / "BartoszDevBridge-Next" / "bootstrap"


def _sid(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.startswith("S-"):
        _fail("invalid_acl_witness", f"{field} must be a Windows SID")
    return value


def _rights(value: object) -> frozenset[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        _fail("invalid_acl_witness", "ACL rights must be a string list")
    return frozenset(value)


def validate_acl_witness(value: Mapping[str, Any]) -> dict[str, Any]:
    """Machine-check the exact DACL policy without trusting localized names."""

    if set(value) != {"schema", "owner_sid", "inheritance_protected", "entries"}:
        _fail("invalid_acl_witness", "ACL witness fields differ")
    if value.get("schema") != WINDOWS_ACL_WITNESS_SCHEMA:
        _fail("invalid_acl_witness", "ACL witness schema differs")
    owner = _sid(value.get("owner_sid"), field="owner_sid")
    if owner not in _REQUIRED_WRITERS:
        _fail("unsafe_authority_owner", "Bootstrap authority owner must be Administrators or SYSTEM")
    if value.get("inheritance_protected") is not True:
        _fail("unsafe_acl_inheritance", "Bootstrap authority must not inherit writable parent ACLs")
    entries = value.get("entries")
    if not isinstance(entries, list):
        _fail("invalid_acl_witness", "ACL entries must be a list")

    write_sids: set[str] = set()
    users_rx = False
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(entries):
        if not isinstance(raw, Mapping) or set(raw) != {"sid", "type", "rights", "inherited"}:
            _fail("invalid_acl_witness", f"ACL entry {index} fields differ")
        sid = _sid(raw.get("sid"), field=f"entries[{index}].sid")
        kind = raw.get("type")
        if kind not in {"Allow", "Deny"}:
            _fail("invalid_acl_witness", "ACL access type is unsupported")
        rights = _rights(raw.get("rights"))
        inherited = raw.get("inherited")
        if inherited is not False:
            _fail("unsafe_acl_inheritance", "Bootstrap authority contains an inherited ACE")
        if kind == "Allow" and rights.intersection(_WRITE_RIGHTS):
            write_sids.add(sid)
            if sid not in _REQUIRED_WRITERS:
                _fail("candidate_write_authority", f"non-TCB SID has Bootstrap write authority: {sid}")
        if kind == "Allow" and sid == USERS_SID and (
            "ReadAndExecute" in rights or {"Read", "ReadData"}.intersection(rights)
        ):
            users_rx = True
        normalized.append(
            {"sid": sid, "type": kind, "rights": sorted(rights), "inherited": False}
        )

    if not _REQUIRED_WRITERS.issubset(write_sids):
        _fail("tcb_writer_missing", "SYSTEM and Administrators must both retain Bootstrap write authority")
    if not users_rx:
        _fail("candidate_read_boundary_missing", "ordinary Users require read/execute access for observation")
    if write_sids.intersection(_LOW_PRIVILEGE):
        _fail("candidate_write_authority", "a low-privilege SID can mutate Bootstrap authority")
    return {
        "schema": WINDOWS_ACL_WITNESS_SCHEMA,
        "owner_sid": owner,
        "inheritance_protected": True,
        "entries": normalized,
    }


def _powershell_executable() -> str:
    # Prefer the same modern PowerShell used by the packaging/CI surface, but
    # retain Windows PowerShell as a supported fallback on stock installations.
    for candidate in ("pwsh.exe", "powershell.exe"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    _fail("powershell_unavailable", "PowerShell is required for Windows ACL observation")


def query_windows_acl(path: str | Path) -> dict[str, Any]:
    """Read one Windows ACL into a locale-independent SID-based witness."""

    if os.name != "nt":
        _fail("windows_required", "Windows ACL observation requires Windows")
    target = _absolute_path(path, field="authority_root")
    if not target.is_dir():
        _fail("authority_root_missing", "Bootstrap authority root is missing")
    script = r'''
$ErrorActionPreference = 'Stop'
$acl = Get-Acl -LiteralPath $env:BDB_VNEXT_TCB_PATH
$ownerIdentity = New-Object System.Security.Principal.NTAccount($acl.Owner)
$owner = $ownerIdentity.Translate([System.Security.Principal.SecurityIdentifier]).Value
$entries = @($acl.Access | ForEach-Object {
  $sid = $_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value
  $rights = @($_.FileSystemRights.ToString().Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ })
  [pscustomobject]@{ sid=$sid; type=$_.AccessControlType.ToString(); rights=$rights; inherited=[bool]$_.IsInherited }
})
[pscustomobject]@{ schema='bdb-vnext-bootstrap-windows-acl-v1'; owner_sid=$owner; inheritance_protected=[bool]$acl.AreAccessRulesProtected; entries=$entries } | ConvertTo-Json -Depth 6 -Compress
'''
    environment = dict(os.environ)
    environment["BDB_VNEXT_TCB_PATH"] = str(target)
    try:
        completed = subprocess.run(
            [_powershell_executable(), "-NoProfile", "-NonInteractive", "-Command", script],
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise M11aWindowsTcbError("acl_observation_failed", "Windows ACL observation could not run") from exc
    if len(completed.stdout) > _MAX_ACL_OUTPUT_BYTES or len(completed.stderr) > _MAX_ACL_OUTPUT_BYTES:
        _fail("acl_observation_unbounded", "Windows ACL observation exceeded its bounded output contract")
    if completed.returncode != 0:
        diagnostic = completed.stderr.decode("utf-8", errors="replace").strip().replace("\r", " ").replace("\n", " ")
        if len(diagnostic) > 512:
            diagnostic = diagnostic[:512] + "…"
        suffix = f": {diagnostic}" if diagnostic else ""
        _fail("acl_observation_failed", "Windows ACL observation returned a failure" + suffix)
    try:
        value = json.loads(completed.stdout.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise M11aWindowsTcbError("invalid_acl_witness", "Windows ACL observation was not JSON") from exc
    if not isinstance(value, Mapping):
        _fail("invalid_acl_witness", "Windows ACL observation must be an object")
    return validate_acl_witness(value)


def validate_windows_tcb_topology(
    *,
    authority_root: str | Path,
    program_data: str | Path,
    runtime_root: str | Path,
    legacy_runtime_root: str | Path,
    mutable_roots: Sequence[str | Path] = (),
) -> dict[str, str]:
    authority = _absolute_path(authority_root, field="authority_root")
    expected_root = default_windows_authority_root(program_data)
    runtime = _absolute_path(runtime_root, field="runtime_root")
    legacy = _absolute_path(legacy_runtime_root, field="legacy_runtime_root")
    if authority != expected_root:
        _fail("authority_root_mismatch", "Bootstrap authority is not the exact ProgramData root")
    if _overlaps(authority, runtime) or _overlaps(authority, legacy):
        _fail("authority_overlap", "Bootstrap authority overlaps runtime state")
    for index, root in enumerate(mutable_roots):
        mutable = _absolute_path(root, field=f"mutable_root[{index}]")
        if _overlaps(authority, mutable):
            _fail("authority_overlap", "Bootstrap authority overlaps candidate/mutable bytes")
    return {
        "authority_root": str(authority),
        "runtime_root": str(runtime),
        "legacy_runtime_root": str(legacy),
    }


def build_windows_tcb_witness(
    *,
    authority_root: str | Path,
    program_data: str | Path,
    runtime_root: str | Path,
    legacy_runtime_root: str | Path,
    mutable_roots: Sequence[str | Path] = (),
    acl_witness: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    topology = validate_windows_tcb_topology(
        authority_root=authority_root,
        program_data=program_data,
        runtime_root=runtime_root,
        legacy_runtime_root=legacy_runtime_root,
        mutable_roots=mutable_roots,
    )
    acl = validate_acl_witness(acl_witness) if acl_witness is not None else query_windows_acl(authority_root)
    payload = {
        "schema": WINDOWS_TCB_SCHEMA,
        "runtime_id": RUNTIME_ID,
        "generation_id": GENERATION_ID,
        "authority_boundary": "external_programdata_bootstrap",
        "topology": topology,
        "acl": acl,
        "candidate_token": "STANDARD_USER_NON_ELEVATED",
        "candidate_may_write_authority": False,
        "activation_operation_available": False,
        "activation_deferred_to": "M11c",
    }
    return {**payload, "witness_sha256": semantic_digest(payload)}


__all__ = [
    "ADMINISTRATORS_SID",
    "M11aWindowsTcbError",
    "SYSTEM_SID",
    "USERS_SID",
    "WINDOWS_ACL_WITNESS_SCHEMA",
    "WINDOWS_TCB_SCHEMA",
    "build_windows_tcb_witness",
    "default_windows_authority_root",
    "query_windows_acl",
    "validate_acl_witness",
    "validate_windows_tcb_topology",
]

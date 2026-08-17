from __future__ import annotations

from pathlib import Path


path = Path("bdb_vnext/m11c_cutover.py")
text = path.read_text(encoding="utf-8")


def replace_once(old: str, new: str) -> None:
    global text
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected exactly one replacement, found {count}: {old[:100]!r}")
    text = text.replace(old, new, 1)


replace_once(
    "from bdb_vnext.m3c_admission import CanonicalVNextAdmissionAuthority\n",
    "from bdb_vnext.m9a_handoff import (\n"
    "    M9aHandoffError,\n"
    "    revalidate_side_by_side_digest,\n"
    "    verify_side_by_side_report,\n"
    ")\n"
    "from bdb_vnext.m3c_admission import CanonicalVNextAdmissionAuthority\n",
)

replace_once(
    "    program_data = os.environ.get(\"PROGRAMDATA\")\n"
    "    if not program_data:\n"
    "        _fail(\"programdata_unavailable\", \"PROGRAMDATA is required for production M11c\")\n"
    "    prepared = query_prepared_activation(authority_root=authority_root, preparation_id=preparation_id)\n",
    "    program_data = os.environ.get(\"PROGRAMDATA\")\n"
    "    if not program_data:\n"
    "        _fail(\"programdata_unavailable\", \"PROGRAMDATA is required for production M11c\")\n"
    "    try:\n"
    "        freeze_digest = verify_side_by_side_report(runtime_root=runtime_root, report=m9a_report)\n"
    "        revalidate_side_by_side_digest(\n"
    "            runtime_root=runtime_root,\n"
    "            legacy_runtime_root=legacy_runtime_root,\n"
    "            freeze_digest=freeze_digest,\n"
    "        )\n"
    "    except M9aHandoffError as exc:\n"
    "        raise M11cCutoverError(exc.code, str(exc)) from exc\n"
    "    prepared = query_prepared_activation(authority_root=authority_root, preparation_id=preparation_id)\n",
)

replace_once(
    "    except (M11cClientError, BootstrapError) as exc:\n"
    "        raise M11cCutoverError(exc.code, str(exc)) from exc\n\n"
    "    observed = observe_bootstrap_activation(authority_root=authority)\n",
    "    except (M11cClientError, BootstrapError) as exc:\n"
    "        raise M11cCutoverError(exc.code, str(exc)) from exc\n\n"
    "    try:\n"
    "        revalidate_side_by_side_digest(\n"
    "            runtime_root=runtime,\n"
    "            legacy_runtime_root=legacy,\n"
    "            freeze_digest=plan[\"m9a_freeze_digest\"],\n"
    "        )\n"
    "    except M9aHandoffError as exc:\n"
    "        raise M11cCutoverError(exc.code, str(exc)) from exc\n\n"
    "    observed = observe_bootstrap_activation(authority_root=authority)\n",
)

path.write_text(text, encoding="utf-8", newline="\n")

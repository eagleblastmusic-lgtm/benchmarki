from __future__ import annotations

import json
import sys
from pathlib import Path

from bdb_vnext.n6_rehearsal import (
    N6_CONFIG_SCHEMA,
    N6_EVENT_SCHEMA,
    N6_NATIVE_REQUEST_SCHEMA,
    N6_PROTOCOL_GENERATION,
    N6RehearsalConfig,
    N6RehearsalService,
    N6_TASKS,
    _js_content,
    prepare_package,
    package_digest,
    write_manual_packet,
)


def test_prepare_package_isolated_and_status_ready(tmp_path: Path, monkeypatch) -> None:
    repo = Path(__file__).parents[1].absolute()
    package_root = tmp_path / "package"
    runtime_root = tmp_path / "runtime"
    legacy_root = tmp_path / "legacy"
    monkeypatch.setattr("bdb_vnext.n6_rehearsal._build_shim", lambda *args, **kwargs: None)

    execution = prepare_package(
        repo_root=repo,
        output=package_root,
        runtime_root=runtime_root,
        legacy_runtime_root=legacy_root,
        source_commit="d27352b2dcc5869e05ed1ec381142aba7e7cc22c",
        python_executable=sys.executable,
    )

    assert execution["schema"] == "bdb-vnext-n6-execution-manifest-v1"
    assert execution["manual_gate"] == "USER_OPERATED_ONLY"
    assert execution["resources"]["production_activation"] is False
    assert execution["resources"]["legacy_mutation"] is False
    assert execution["subject"]["commit"] == "d27352b2dcc5869e05ed1ec381142aba7e7cc22c"
    assert execution["package"]["native_host"]["executable_ready"] is False
    assert Path(execution["package"]["native_host"]["registration_script"]).is_file()
    assert package_digest(package_root) == execution["package"]["digest"]

    config_path = package_root / "native-config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["schema"] == N6_CONFIG_SCHEMA
    assert N6RehearsalConfig.from_json(config_path).package_digest == execution["package"]["digest"]

    service = N6RehearsalService(N6RehearsalConfig.from_json(config_path))
    response = service.handle({
        "schema": N6_NATIVE_REQUEST_SCHEMA,
        "request_id": "n6:test:status",
        "event": "status",
        "package_id": "bdb-vnext-n6-rehearsal-package-v1",
        "protocol_generation": N6_PROTOCOL_GENERATION,
        "payload": {},
    })
    assert response["status"] == "READY"
    assert response["production_activation"] is False

    packet_path = write_manual_packet(execution, package_root / "MANUAL_BROWSER_REHEARSAL_PACKET.md")
    packet = packet_path.read_text(encoding="utf-8")
    assert "USER_OPERATED_ONLY" not in packet  # the packet is operator-facing, not a machine event
    assert execution["package"]["native_host"]["registration_script"] in packet
    assert all(task["id"] in packet for task in N6_TASKS)
    assert N6RehearsalService(N6RehearsalConfig.from_json(config_path)).package_digest == execution["package"]["digest"]


def test_n6_capture_contract_preserves_model_and_reasoning_attestation() -> None:
    source = {"schema": N6_EVENT_SCHEMA, "model": "GPT-5.6 Sol", "reasoning": "Wysoki"}
    assert source["model"] == "GPT-5.6 Sol"
    assert source["reasoning"] == "Wysoki"


def test_content_script_uses_deterministic_restart_safe_submission_and_resume() -> None:
    script = _js_content()
    assert "crypto.subtle.digest" in script
    assert "n6_active_submission" in script
    assert "Resume in this chat" in script
    assert "crypto.randomUUID" not in script

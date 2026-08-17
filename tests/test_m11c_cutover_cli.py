from __future__ import annotations

import json

import pytest

import bdb_vnext.m11c_cutover_cli as cli


PLAN_SHA = "sha256:" + "a" * 64


def test_apply_parser_requires_exact_plan_approval_token() -> None:
    parser = cli._parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["apply", "--authority-root", "C:/authority", "--cutover-id", "final-1"])


def test_apply_has_no_yes_or_force_shortcut() -> None:
    parser = cli._parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "apply",
                "--authority-root",
                "C:/authority",
                "--cutover-id",
                "final-1",
                "--yes",
            ]
        )


def test_apply_forwards_only_explicit_plan_sha_as_operator_approval(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    captured: dict[str, object] = {}

    def fake_apply(**kwargs: object):
        captured.update(kwargs)
        return {
            "status": "ACTIVE",
            "production_activation_performed": True,
            "bootstrap": {},
            "client_gate": {},
            "m3c_intake_enabled": True,
        }

    monkeypatch.setattr(cli, "apply_windows_cutover", fake_apply)
    code = cli.main(
        [
            "apply",
            "--authority-root",
            "C:/authority",
            "--cutover-id",
            "final-1",
            "--approve-plan-sha256",
            PLAN_SHA,
        ]
    )
    assert code == 0
    assert captured == {
        "authority_root": "C:/authority",
        "cutover_id": "final-1",
        "expected_plan_sha256": PLAN_SHA,
        "operator_approved": True,
    }
    output = json.loads(capsys.readouterr().out)
    assert output["production_activation_performed"] is True


def test_stage_clients_never_reports_activation(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(
        cli,
        "stage_client_plan",
        lambda **_: {
            "plan": {
                "client_plan_sha256": PLAN_SHA,
                "browser_extension_id": "mopnolkjddkmgojfjkenjobehhmmklll",
                "browser_bundle_root": "C:/runtime/clients/browser-extension",
                "native_manifest_path": "C:/runtime/clients/native-host/host.json",
            }
        },
    )
    code = cli.main(
        [
            "stage-clients",
            "--runtime-root",
            "C:/runtime",
            "--legacy-runtime-root",
            "C:/legacy",
            "--authority-root",
            "C:/ProgramData/BartoszDevBridge-Next/bootstrap",
            "--browser-source-root",
            "C:/repo/browser_extension_vnext",
            "--native-host-executable",
            "C:/Scripts/bdb-vnext-native-host.exe",
            "--source-head",
            "a" * 40,
            "--source-tree",
            "b" * 40,
        ]
    )
    assert code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "STAGED_NOT_ACTIVATED"
    assert output["browser_operator_action_required"] is True
    assert output["production_activation_performed"] is False


def test_cli_failure_is_machine_readable_and_fail_closed(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from bdb_vnext.m11c_cutover import M11cCutoverError

    def fail(**_: object):
        raise M11cCutoverError("bootstrap_not_active", "blocked")

    monkeypatch.setattr(cli, "apply_windows_cutover", fail)
    code = cli.main(
        [
            "apply",
            "--authority-root",
            "C:/authority",
            "--cutover-id",
            "final-1",
            "--approve-plan-sha256",
            PLAN_SHA,
        ]
    )
    assert code == 2
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "BLOCKED"
    assert output["error_code"] == "bootstrap_not_active"
    assert output["production_activation_performed"] is False

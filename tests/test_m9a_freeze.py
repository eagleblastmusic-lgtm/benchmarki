from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from bdb_vnext.m9a_freeze import (
    M9aFreezeError,
    ProfileSpec,
    _native_registry_suffixes,
    _parse_profile_spec,
    _validate_probe,
)


DIGEST = "sha256:" + "1" * 64


def safe_probe() -> dict[str, object]:
    return {
        "probe_digest": DIGEST,
        "wake_event_present": False,
        "native": {
            "profile_bound": True,
            "arm": {"effective_armed": False},
        },
        "journal": {
            "service_candidates": {
                "count": 1,
                "truncated": False,
                "items": [{"pid": 123, "pid_alive": False}],
            },
            "unresolved_truncated": False,
            "capability_counts": {
                "ACKNOWLEDGEMENT_ONLY_ON_SERVICE_RESTART": 191,
                "MANUAL_ONLY": 1,
            },
        },
        "spool": {
            "truncated": False,
            "classification_counts": {"NEW_INGRESS_IF_SERVICE_RESTARTS": 14},
        },
        "receipts": {
            "status": "VALID_LEGACY_COMPAT_SHAPE",
            "issues": [],
        },
        "promoter": {
            "status": "VALID_LEGACY_COMPAT",
            "issues": [],
            "truncated": False,
        },
        "vnext_activation_allowed": False,
        "m9b_allowed": False,
    }


def test_safe_ack_only_and_manual_probe_is_accepted() -> None:
    profile = ProfileSpec("bdb-self", Path("bridge-config.json"), DIGEST)

    _validate_probe(profile, safe_probe())


def test_probe_digest_is_an_exact_fence() -> None:
    profile = ProfileSpec("bdb-self", Path("bridge-config.json"), "sha256:" + "2" * 64)

    with pytest.raises(M9aFreezeError, match="fresh probe digest changed"):
        _validate_probe(profile, safe_probe())


def test_live_service_pid_blocks_freeze() -> None:
    probe = safe_probe()
    probe["journal"]["service_candidates"]["items"][0]["pid_alive"] = True  # type: ignore[index]
    profile = ProfileSpec("bdb-self", Path("bridge-config.json"), DIGEST)

    with pytest.raises(M9aFreezeError, match="live legacy service PID"):
        _validate_probe(profile, probe)


def test_write_capable_recovery_blocks_freeze() -> None:
    probe = safe_probe()
    probe["journal"]["capability_counts"] = {"RECOVERY_WRITE_OR_DIVERGENCE": 1}  # type: ignore[index]
    profile = ProfileSpec("bdb-self", Path("bridge-config.json"), DIGEST)

    with pytest.raises(M9aFreezeError, match="write-capable or unknown"):
        _validate_probe(profile, probe)


def test_armed_native_host_blocks_freeze() -> None:
    probe = safe_probe()
    probe["native"]["arm"]["effective_armed"] = True  # type: ignore[index]
    profile = ProfileSpec("bdb-self", Path("bridge-config.json"), DIGEST)

    with pytest.raises(M9aFreezeError, match="native host is armed"):
        _validate_probe(profile, probe)


def test_unknown_spool_class_blocks_freeze() -> None:
    probe = safe_probe()
    probe["spool"]["classification_counts"] = {"UNSAFE_FILENAME": 1}  # type: ignore[index]
    profile = ProfileSpec("bdb-self", Path("bridge-config.json"), DIGEST)

    with pytest.raises(M9aFreezeError, match="unsafe spool classes"):
        _validate_probe(profile, probe)


def test_native_registry_suffixes_are_exact() -> None:
    assert _native_registry_suffixes() == (
        ("chrome", r"Software\Google\Chrome\NativeMessagingHosts\com.bartosz.dev_bridge"),
        ("edge", r"Software\Microsoft\Edge\NativeMessagingHosts\com.bartosz.dev_bridge"),
    )


def test_profile_cli_spec_binds_path_and_probe_digest() -> None:
    value = _parse_profile_spec(f"bdb-self::C:/runtime/bridge-config.json::{DIGEST}")

    assert value.profile_id == "bdb-self"
    assert value.bridge_config_path == Path("C:/runtime/bridge-config.json")
    assert value.expected_probe_digest == DIGEST


def test_profile_rejects_unbounded_identity() -> None:
    with pytest.raises(ValueError):
        ProfileSpec("bad/profile", Path("bridge-config.json"), DIGEST)


def test_freeze_wrapper_can_be_invoked_directly() -> None:
    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [sys.executable, str(root / "scripts" / "run_m9a_freeze.py"), "--help"],
        cwd=root.parent,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--apply" in completed.stdout
    assert "--profile" in completed.stdout

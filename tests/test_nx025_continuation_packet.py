"""NX-025: content-addressed continuation identity and minimal packet v1."""

from __future__ import annotations

import ast
import hashlib
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bdb_shared.evidence import canonical_json_bytes
from bdb_vnext.auto_scope_contract import AutoScope, ScopeAction
from bdb_vnext.continuation_packet import (
    CONTINUATION_IDENTITY_FIELDS,
    CONTINUATION_PACKET_VERSION as PACKET_VERSION_VALUE,
    CONTINUATION_PACKET_VERSION_EXPLICIT as PACKET_VERSION_EXPLICIT_CONTRACT,
    DEFAULT_PACKET_MAX_BYTES,
    PACKET_BECOMES_SECOND_AUTHORITY as SECOND_AUTHORITY_CONTRACT,
    PACKET_CONTENT_ADDRESSED as CONTENT_ADDRESSED_CONTRACT,
    PACKET_SIZE_LIMIT_EXPLICIT as SIZE_LIMIT_EXPLICIT_CONTRACT,
    ContinuationAuthoritySnapshot,
    ContinuationPacket,
    ContinuationPacketError,
    build_packet,
    compute_packet_digest,
    deserialize_packet,
    packet_size_bytes,
    redact_secrets,
    serialize_packet,
    validate_packet,
)


ISSUED_AT = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)
EXPIRES_AT = ISSUED_AT + timedelta(hours=1)
PLAN_DIGEST = "sha256:" + "1" * 64
STATE_DIGEST = "sha256:" + "2" * 64
EVIDENCE_DIGEST = "sha256:" + "3" * 64
HEAD = "a" * 40


def _packet(**overrides: object) -> ContinuationPacket:
    values: dict[str, object] = {
        "project_id": "bdb-vnext-next-iteration",
        "plan_identity": "plan:nx-m2",
        "plan_version": 1,
        "plan_digest": PLAN_DIGEST,
        "scope": AutoScope.UNTIL_STOPPED,
        "run_id": "run:nx025-1",
        "scope_epoch": 4,
        "current_milestone_id": "NX-M2",
        "current_task_id": "NX-025",
        "execution_binding_id": "binding:nx025-1",
        "expected_repo_head_before": HEAD,
        "state_revision": 7,
        "state_digest": STATE_DIGEST,
        "allowed_next_action": ScopeAction.LAUNCH_TASK,
        "budget_summary": {"remaining_attempts": 2, "remaining_retry_budget": 1},
        "evidence_refs": [EVIDENCE_DIGEST],
        "issued_at": ISSUED_AT,
        "expires_at": EXPIRES_AT,
        "attempt_id": "attempt:nx025-1",
        "expected_tree": "b" * 40,
        "conversation_binding_policy": "EXISTING_CHAT_ONLY",
    }
    values.update(overrides)
    return build_packet(**values)  # type: ignore[arg-type]


def _authority(**overrides: object) -> ContinuationAuthoritySnapshot:
    values: dict[str, object] = {
        "project_id": "bdb-vnext-next-iteration",
        "plan_identity": "plan:nx-m2",
        "plan_version": 1,
        "plan_digest": PLAN_DIGEST,
        "scope": AutoScope.UNTIL_STOPPED,
        "run_id": "run:nx025-1",
        "scope_epoch": 4,
        "current_milestone_id": "NX-M2",
        "current_task_id": "NX-025",
        "execution_binding_id": "binding:nx025-1",
        "expected_repo_head_before": HEAD,
        "state_revision": 7,
        "state_digest": STATE_DIGEST,
        "allowed_next_action": ScopeAction.LAUNCH_TASK,
        "budget_summary": {"remaining_attempts": 2, "remaining_retry_budget": 1},
        "evidence_refs": [EVIDENCE_DIGEST],
        "status": "ACTIVE",
        "task_status": "IN_PROGRESS",
        "stop_requested": False,
        "plan_approved": True,
        "attempt_id": "attempt:nx025-1",
        "expected_tree": "b" * 40,
        "conversation_binding_policy": "EXISTING_CHAT_ONLY",
    }
    values.update(overrides)
    return ContinuationAuthoritySnapshot(**values)  # type: ignore[arg-type]


def _mutated_identity_document(packet: ContinuationPacket, field: str) -> dict[str, object]:
    document = packet.as_dict()
    mutations: dict[str, object] = {
        "project_id": "bdb-vnext-other-project",
        "plan_identity": "plan:nx-m2-revised",
        "plan_version": 2,
        "plan_digest": "sha256:" + "4" * 64,
        "scope": AutoScope.PROJECT.value,
        "run_id": "run:nx025-2",
        "scope_epoch": 5,
        "current_milestone_id": "NX-M3",
        "current_task_id": "NX-026",
        "execution_binding_id": "binding:nx025-2",
        "expected_repo_head_before": "c" * 40,
        "state_revision": 8,
        "state_digest": "sha256:" + "5" * 64,
        "allowed_next_action": ScopeAction.WAIT_CI_WAITING.value,
    }
    document[field] = mutations[field]
    return document


def _source_readback(repo_root: Path) -> tuple[str, str, bool]:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, text=True, check=True
    ).stdout.strip()
    tree = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=repo_root, capture_output=True, text=True, check=True
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo_root, capture_output=True, text=True, check=True
    ).stdout
    unstaged = subprocess.run(["git", "diff", "--quiet"], cwd=repo_root)
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo_root)
    return head, tree, not status.strip() and unstaged.returncode == 0 and staged.returncode == 0


class TestDigestAndSerialization:
    def test_golden_digest_is_content_addressed_and_replay_stable(self) -> None:
        packet = _packet()
        semantic = {key: packet[key] for key in sorted(packet) if key not in {"continuation_id", "packet_digest"}}
        expected = "sha256:" + hashlib.sha256(canonical_json_bytes(semantic)).hexdigest()
        assert packet.digest == expected
        assert packet.continuation_id == expected
        assert compute_packet_digest(packet.as_dict()) == expected

        replay = _packet()
        assert replay.digest == packet.digest
        assert serialize_packet(replay) == serialize_packet(packet)
        decoded = deserialize_packet(serialize_packet(packet))
        assert decoded.digest == packet.digest

    def test_json_key_reordering_is_semantically_invariant(self) -> None:
        packet = _packet()
        reordered = {key: packet[key] for key in reversed(list(packet))}
        assert compute_packet_digest(reordered) == packet.digest
        assert ContinuationPacket.from_mapping(reordered).digest == packet.digest

        noncanonical = b'{"packet_digest": "' + packet.digest.encode("ascii") + b'"}'
        with pytest.raises(ContinuationPacketError):
            deserialize_packet(noncanonical)

    @pytest.mark.parametrize("field", sorted(CONTINUATION_IDENTITY_FIELDS))
    def test_each_identity_field_changes_digest(self, field: str) -> None:
        packet = _packet()
        assert compute_packet_digest(_mutated_identity_document(packet, field)) != packet.digest


class TestLiveAuthorityValidation:
    def test_packet_is_valid_with_current_live_authority(self) -> None:
        result = validate_packet(_packet(), _authority(), now=ISSUED_AT + timedelta(minutes=5))
        assert result.valid is True
        assert result.code == "VALID"
        assert result.packet_digest == _packet().digest

    @pytest.mark.parametrize(
        ("field", "value", "code"),
        [
            ("expected_repo_head_before", "c" * 40, "STALE_HEAD"),
            ("execution_binding_id", "binding:other", "STALE_BINDING"),
            ("scope_epoch", 5, "STALE_SCOPE_EPOCH"),
            ("run_id", "run:other", "STALE_RUN"),
            ("plan_identity", "plan:other", "STALE_PLAN_IDENTITY"),
            ("plan_version", 2, "STALE_PLAN_VERSION"),
            ("plan_digest", "sha256:" + "4" * 64, "STALE_PLAN_DIGEST"),
            ("state_revision", 8, "STALE_STATE_REVISION"),
            ("state_digest", "sha256:" + "5" * 64, "STALE_STATE_DIGEST"),
            ("current_task_id", "NX-026", "STALE_TASK"),
            ("allowed_next_action", ScopeAction.WAIT_CI_WAITING, "STALE_ALLOWED_ACTION"),
        ],
    )
    def test_stale_identity_is_rejected(self, field: str, value: object, code: str) -> None:
        result = validate_packet(_packet(), _authority(**{field: value}), now=ISSUED_AT + timedelta(minutes=5))
        assert result.valid is False
        assert result.code == code

    def test_accepted_task_and_stop_are_rejected(self) -> None:
        accepted = validate_packet(
            _packet(), _authority(task_status="ACCEPTED"), now=ISSUED_AT + timedelta(minutes=5)
        )
        stopped = validate_packet(
            _packet(), _authority(stop_requested=True, status="STOPPED"), now=ISSUED_AT + timedelta(minutes=5)
        )
        assert accepted.valid is False
        assert accepted.code == "TASK_ALREADY_ACCEPTED"
        assert stopped.valid is False
        assert stopped.code == "STOP_CANONICAL"

    def test_validator_is_read_only(self) -> None:
        packet = _packet()
        authority = _authority()
        packet_before = packet.as_dict()
        before = authority
        result = validate_packet(packet, authority, now=ISSUED_AT + timedelta(minutes=5))
        assert result.valid is True
        assert authority == before
        assert packet.as_dict() == packet_before


class TestMalformedBoundariesAndRedaction:
    def test_missing_malformed_and_unknown_version_fail_closed(self) -> None:
        packet = _packet().as_dict()
        packet.pop("run_id")
        with pytest.raises(ContinuationPacketError) as missing:
            ContinuationPacket.from_mapping(packet)
        assert missing.value.code == "MISSING_REQUIRED_FIELD"

        malformed = _packet().as_dict()
        malformed["plan_digest"] = "not-a-digest"
        with pytest.raises(ContinuationPacketError) as digest:
            ContinuationPacket.from_mapping(malformed)
        assert digest.value.code == "MALFORMED_DIGEST"

        future = _packet().as_dict()
        future["packet_version"] = "v99"
        with pytest.raises(ContinuationPacketError) as version:
            ContinuationPacket.from_mapping(future)
        assert version.value.code == "UNKNOWN_PACKET_VERSION"

        unknown = _packet().as_dict()
        unknown["future_field"] = "unsupported"
        with pytest.raises(ContinuationPacketError) as field:
            ContinuationPacket.from_mapping(unknown)
        assert field.value.code == "UNKNOWN_PACKET_FIELD"

    @pytest.mark.parametrize(
        "field",
        ["conversation_transcript", "chat_history", "full_plan", "raw_ci_logs", "raw_test_logs"],
    )
    def test_transcript_plan_and_raw_log_fields_are_rejected(self, field: str) -> None:
        document = _packet().as_dict()
        document[field] = "raw content that cannot be part of a minimal packet"
        with pytest.raises(ContinuationPacketError) as error:
            ContinuationPacket.from_mapping(document)
        assert error.value.code == "FULL_TRANSCRIPT_NOT_ALLOWED"

    @pytest.mark.parametrize(
        ("field", "secret"),
        [
            ("access_token", "access-token-fixture"),
            ("authorization", "Bearer authorization-fixture"),
            ("password", "password-fixture"),
            ("api_key", "api-key-fixture"),
            ("cookie", "session=fixture-cookie"),
            ("private_key", "-----BEGIN PRIVATE KEY-----fixture-----END PRIVATE KEY-----"),
        ],
    )
    def test_secret_classes_are_rejected_without_leaking_values(self, field: str, secret: str) -> None:
        with pytest.raises(ContinuationPacketError) as error:
            _packet(budget_summary={field: secret})
        assert error.value.code == "SECRET_MATERIAL_REJECTED"
        assert secret not in str(error.value)
        assert redact_secrets({field: secret})[field] == "[REDACTED]"

    def test_evidence_is_integrity_checkable_and_bounded(self) -> None:
        content_ref = {
            "type": "ci-report",
            "schema": "ci-report-v1",
            "semantic_digest": EVIDENCE_DIGEST,
            "raw_digest": "sha256:" + "6" * 64,
        }
        packet = _packet(evidence_refs=[content_ref])
        assert packet["evidence_refs"][0]["semantic_digest"] == EVIDENCE_DIGEST

        with pytest.raises(ContinuationPacketError) as raw:
            _packet(evidence_refs=[{"payload": "raw evidence"}])
        assert raw.value.code == "UNBOUNDED_RAW_EVIDENCE"

        with pytest.raises(ContinuationPacketError) as giant:
            _packet(evidence_refs=["x" * 513])
        assert giant.value.code in {"MALFORMED_DIGEST", "UNBOUNDED_RAW_EVIDENCE"}

    def test_exact_serialized_byte_limit(self) -> None:
        packet = _packet()
        size = packet_size_bytes(packet)
        assert size > 0
        with pytest.raises(ContinuationPacketError) as below:
            serialize_packet(packet, max_bytes=size - 1)
        assert below.value.code == "PACKET_TOO_LARGE"
        assert len(serialize_packet(packet, max_bytes=size)) == size
        assert len(serialize_packet(packet, max_bytes=size + 1)) == size

        with pytest.raises(ContinuationPacketError) as decoded_below:
            deserialize_packet(serialize_packet(packet), max_bytes=size - 1)
        assert decoded_below.value.code == "PACKET_TOO_LARGE"


class TestExpiry:
    def test_virtual_clock_expiry_and_restart_do_not_extend_packet(self) -> None:
        packet = _packet()
        authority = _authority()
        before_expiry = validate_packet(packet, authority, now=EXPIRES_AT - timedelta(microseconds=1))
        at_expiry = validate_packet(packet, authority, now=EXPIRES_AT)
        restarted = deserialize_packet(serialize_packet(packet))
        after_restart = validate_packet(restarted, authority, now=EXPIRES_AT)
        assert before_expiry.valid is True
        assert at_expiry.valid is False
        assert at_expiry.code == "EXPIRED_PACKET"
        assert restarted["expires_at"] == packet["expires_at"]
        assert after_restart.valid is False
        assert after_restart.code == "EXPIRED_PACKET"


def inspect_nx025_gate_for_hardcoded_results() -> tuple[bool, list[str]]:
    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    gate = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "run_nx025_machine_gate"
        ),
        None,
    )
    if gate is None:
        return False, ["run_nx025_machine_gate_missing"]
    fields = {
        "CONTINUATION_PACKET_VERSION_EXPLICIT",
        "PACKET_CONTENT_ADDRESSED",
        "PACKET_BECOMES_SECOND_AUTHORITY",
        "IDENTITY_FIELDS_TESTED",
        "IDENTITY_FIELD_MUTATIONS_WITH_SAME_DIGEST",
        "CANONICAL_SERIALIZATION_DIVERGENCES",
        "IDENTICAL_PACKET_REPLAY_DIGEST_DIVERGENCES",
        "PACKET_VALID_WITH_LIVE_AUTHORITY",
        "STALE_PACKET_ACCEPTED",
        "MISSING_REQUIRED_FIELD_ACCEPTED",
        "UNKNOWN_PACKET_VERSION_ACCEPTED",
        "FULL_TRANSCRIPT_FIELDS_PRESENT",
        "FULL_PLAN_EMBEDDED",
        "SECRET_FIXTURES_TESTED",
        "SECRET_FIXTURES_LEAKED",
        "UNBOUNDED_RAW_EVIDENCE_ACCEPTED",
        "PACKET_SIZE_LIMIT_EXPLICIT",
        "OVERSIZED_PACKET_ACCEPTED",
        "EXPIRED_PACKET_ACCEPTED",
        "RESTART_EXTENDS_PACKET_EXPIRY",
        "GOLDEN_VECTOR_DIVERGENCES",
        "NO_HARDCODED_GATE_RESULTS",
        "SOURCE_BOUND_MACHINE_GATE",
        "NX025_STATUS",
    }
    hardcoded: list[str] = []
    for node in ast.walk(gate):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in fields:
                if isinstance(node.value, ast.Constant) and node.value.value in {
                    True,
                    False,
                    0,
                    1,
                    "PASS",
                    "FAIL",
                }:
                    hardcoded.append(target.id)
    return not hardcoded, hardcoded


def run_nx025_machine_gate() -> dict[str, object]:
    """Derive the NX-025 gate from vectors, validators, and source readback."""

    packet = _packet()
    authority = _authority()
    identity_fields = sorted(CONTINUATION_IDENTITY_FIELDS)
    identity_mutation_results = [
        compute_packet_digest(_mutated_identity_document(packet, field)) != packet.digest
        for field in identity_fields
    ]
    reordered = {key: packet[key] for key in reversed(list(packet))}
    replay = _packet()
    live_result = validate_packet(packet, authority, now=ISSUED_AT + timedelta(minutes=5))
    stale_cases = (
        _authority(expected_repo_head_before="c" * 40),
        _authority(execution_binding_id="binding:stale"),
        _authority(scope_epoch=5),
        _authority(run_id="run:stale"),
        _authority(plan_identity="plan:stale"),
        _authority(plan_version=2),
        _authority(plan_digest="sha256:" + "4" * 64),
        _authority(state_revision=8, state_digest="sha256:" + "5" * 64),
        _authority(task_status="ACCEPTED"),
        _authority(stop_requested=True, status="STOPPED"),
        _authority(allowed_next_action=ScopeAction.WAIT_CI_WAITING),
    )
    stale_results = [
        validate_packet(packet, candidate, now=ISSUED_AT + timedelta(minutes=5)) for candidate in stale_cases
    ]

    missing_document = packet.as_dict()
    missing_document.pop("current_task_id")
    try:
        ContinuationPacket.from_mapping(missing_document)
        missing_accepted = True
    except ContinuationPacketError:
        missing_accepted = False

    unknown_version_document = packet.as_dict()
    unknown_version_document["packet_version"] = "v99"
    try:
        ContinuationPacket.from_mapping(unknown_version_document)
        unknown_version_accepted = True
    except ContinuationPacketError:
        unknown_version_accepted = False

    forbidden_fields = {
        "arbitrary_chat_history",
        "chat_history",
        "conversation_history",
        "conversation_transcript",
        "full_plan",
        "plan",
        "raw_ci_logs",
        "raw_test_logs",
        "test_logs",
        "transcript",
    }
    full_transcript_fields_present = len(forbidden_fields.intersection(packet))
    forbidden_field_acceptances = 0
    for field in sorted(forbidden_fields):
        forbidden_document = packet.as_dict()
        forbidden_document[field] = "forbidden raw content"
        try:
            ContinuationPacket.from_mapping(forbidden_document)
        except ContinuationPacketError:
            pass
        else:
            forbidden_field_acceptances += 1
    full_transcript_fields_present += forbidden_field_acceptances
    full_plan_embedded = "plan" in packet or "full_plan" in packet

    secret_fixtures = (
        ("access_token", "access-token-fixture"),
        ("authorization", "Bearer authorization-fixture"),
        ("password", "password-fixture"),
        ("api_key", "api-key-fixture"),
        ("cookie", "session=fixture-cookie"),
        ("private_key", "-----BEGIN PRIVATE KEY-----fixture-----END PRIVATE KEY-----"),
    )
    secret_leaks = 0
    for key, secret in secret_fixtures:
        try:
            _packet(budget_summary={key: secret})
        except ContinuationPacketError as error:
            secret_leaks += int(secret in str(error))
        else:
            secret_leaks += 1

    evidence_raw_accepted = True
    try:
        _packet(evidence_refs=[{"payload": "raw evidence"}])
    except ContinuationPacketError:
        evidence_raw_accepted = False

    size = packet_size_bytes(packet)
    oversized_accepted = True
    try:
        serialize_packet(packet, max_bytes=size - 1)
    except ContinuationPacketError:
        oversized_accepted = False

    expired = validate_packet(packet, authority, now=EXPIRES_AT)
    restarted = deserialize_packet(serialize_packet(packet))
    no_hardcoded, hardcoded = inspect_nx025_gate_for_hardcoded_results()
    head, tree, clean = _source_readback(Path(__file__).resolve().parent.parent)

    CONTINUATION_PACKET_VERSION_EXPLICIT = bool(PACKET_VERSION_EXPLICIT_CONTRACT and PACKET_VERSION_VALUE == "v1")
    PACKET_CONTENT_ADDRESSED = bool(CONTENT_ADDRESSED_CONTRACT and packet.digest == packet.continuation_id)
    PACKET_BECOMES_SECOND_AUTHORITY = bool(SECOND_AUTHORITY_CONTRACT)
    IDENTITY_FIELDS_TESTED = len(identity_fields)
    IDENTITY_FIELD_MUTATIONS_WITH_SAME_DIGEST = len(
        [result for result in identity_mutation_results if not result]
    )
    CANONICAL_SERIALIZATION_DIVERGENCES = int(compute_packet_digest(reordered) != packet.digest)
    IDENTICAL_PACKET_REPLAY_DIGEST_DIVERGENCES = int(replay.digest != packet.digest)
    PACKET_VALID_WITH_LIVE_AUTHORITY = bool(live_result.valid)
    STALE_PACKET_ACCEPTED = int(any(result.valid for result in stale_results))
    MISSING_REQUIRED_FIELD_ACCEPTED = int(missing_accepted)
    UNKNOWN_PACKET_VERSION_ACCEPTED = int(unknown_version_accepted)
    FULL_TRANSCRIPT_FIELDS_PRESENT = full_transcript_fields_present
    FULL_PLAN_EMBEDDED = bool(full_plan_embedded)
    SECRET_FIXTURES_TESTED = len(secret_fixtures)
    SECRET_FIXTURES_LEAKED = secret_leaks
    UNBOUNDED_RAW_EVIDENCE_ACCEPTED = int(evidence_raw_accepted)
    PACKET_SIZE_LIMIT_EXPLICIT = bool(SIZE_LIMIT_EXPLICIT_CONTRACT and DEFAULT_PACKET_MAX_BYTES > 0)
    OVERSIZED_PACKET_ACCEPTED = int(oversized_accepted)
    EXPIRED_PACKET_ACCEPTED = int(expired.valid)
    RESTART_EXTENDS_PACKET_EXPIRY = int(restarted["expires_at"] != packet["expires_at"])
    GOLDEN_VECTOR_DIVERGENCES = int(compute_packet_digest(packet) != compute_packet_digest(replay))
    HARDCODED_GATE_RESULT_FIELDS = hardcoded
    NO_HARDCODED_GATE_RESULTS = no_hardcoded
    SOURCE_BOUND_MACHINE_GATE = "PASS" if len(head) == 40 and len(tree) == 40 and clean else "FAIL"
    NX025_STATUS = "PASS" if (
        CONTINUATION_PACKET_VERSION_EXPLICIT
        and PACKET_CONTENT_ADDRESSED
        and not PACKET_BECOMES_SECOND_AUTHORITY
        and IDENTITY_FIELDS_TESTED == len(CONTINUATION_IDENTITY_FIELDS)
        and IDENTITY_FIELD_MUTATIONS_WITH_SAME_DIGEST == 0
        and CANONICAL_SERIALIZATION_DIVERGENCES == 0
        and IDENTICAL_PACKET_REPLAY_DIGEST_DIVERGENCES == 0
        and PACKET_VALID_WITH_LIVE_AUTHORITY
        and STALE_PACKET_ACCEPTED == 0
        and MISSING_REQUIRED_FIELD_ACCEPTED == 0
        and UNKNOWN_PACKET_VERSION_ACCEPTED == 0
        and FULL_TRANSCRIPT_FIELDS_PRESENT == 0
        and not FULL_PLAN_EMBEDDED
        and SECRET_FIXTURES_TESTED == len(secret_fixtures)
        and SECRET_FIXTURES_LEAKED == 0
        and not UNBOUNDED_RAW_EVIDENCE_ACCEPTED
        and PACKET_SIZE_LIMIT_EXPLICIT
        and not OVERSIZED_PACKET_ACCEPTED
        and not EXPIRED_PACKET_ACCEPTED
        and not RESTART_EXTENDS_PACKET_EXPIRY
        and GOLDEN_VECTOR_DIVERGENCES == 0
        and NO_HARDCODED_GATE_RESULTS
        and SOURCE_BOUND_MACHINE_GATE == "PASS"
    ) else "FAIL"
    return {
        "CONTINUATION_PACKET_VERSION_EXPLICIT": CONTINUATION_PACKET_VERSION_EXPLICIT,
        "PACKET_CONTENT_ADDRESSED": PACKET_CONTENT_ADDRESSED,
        "PACKET_BECOMES_SECOND_AUTHORITY": PACKET_BECOMES_SECOND_AUTHORITY,
        "IDENTITY_FIELDS_TESTED": IDENTITY_FIELDS_TESTED,
        "IDENTITY_FIELD_MUTATIONS_WITH_SAME_DIGEST": IDENTITY_FIELD_MUTATIONS_WITH_SAME_DIGEST,
        "CANONICAL_SERIALIZATION_DIVERGENCES": CANONICAL_SERIALIZATION_DIVERGENCES,
        "IDENTICAL_PACKET_REPLAY_DIGEST_DIVERGENCES": IDENTICAL_PACKET_REPLAY_DIGEST_DIVERGENCES,
        "PACKET_VALID_WITH_LIVE_AUTHORITY": PACKET_VALID_WITH_LIVE_AUTHORITY,
        "STALE_PACKET_ACCEPTED": STALE_PACKET_ACCEPTED,
        "MISSING_REQUIRED_FIELD_ACCEPTED": MISSING_REQUIRED_FIELD_ACCEPTED,
        "UNKNOWN_PACKET_VERSION_ACCEPTED": UNKNOWN_PACKET_VERSION_ACCEPTED,
        "FULL_TRANSCRIPT_FIELDS_PRESENT": FULL_TRANSCRIPT_FIELDS_PRESENT,
        "FULL_PLAN_EMBEDDED": FULL_PLAN_EMBEDDED,
        "SECRET_FIXTURES_TESTED": SECRET_FIXTURES_TESTED,
        "SECRET_FIXTURES_LEAKED": SECRET_FIXTURES_LEAKED,
        "UNBOUNDED_RAW_EVIDENCE_ACCEPTED": UNBOUNDED_RAW_EVIDENCE_ACCEPTED,
        "PACKET_SIZE_LIMIT_EXPLICIT": PACKET_SIZE_LIMIT_EXPLICIT,
        "OVERSIZED_PACKET_ACCEPTED": OVERSIZED_PACKET_ACCEPTED,
        "EXPIRED_PACKET_ACCEPTED": EXPIRED_PACKET_ACCEPTED,
        "RESTART_EXTENDS_PACKET_EXPIRY": RESTART_EXTENDS_PACKET_EXPIRY,
        "GOLDEN_VECTOR_DIVERGENCES": GOLDEN_VECTOR_DIVERGENCES,
        "HARDCODED_GATE_RESULT_FIELDS": HARDCODED_GATE_RESULT_FIELDS,
        "NO_HARDCODED_GATE_RESULTS": NO_HARDCODED_GATE_RESULTS,
        "SOURCE_HEAD": head,
        "SOURCE_TREE": tree,
        "WORKTREE_CLEAN": clean,
        "SOURCE_BOUND_MACHINE_GATE": SOURCE_BOUND_MACHINE_GATE,
        "NX025_STATUS": NX025_STATUS,
    }


def test_nx025_machine_gate_execution() -> None:
    gate = run_nx025_machine_gate()
    assert gate["CONTINUATION_PACKET_VERSION_EXPLICIT"] is True
    assert gate["PACKET_CONTENT_ADDRESSED"] is True
    assert gate["PACKET_BECOMES_SECOND_AUTHORITY"] is False
    assert gate["IDENTITY_FIELDS_TESTED"] == len(CONTINUATION_IDENTITY_FIELDS)
    assert gate["IDENTITY_FIELD_MUTATIONS_WITH_SAME_DIGEST"] == 0
    assert gate["CANONICAL_SERIALIZATION_DIVERGENCES"] == 0
    assert gate["IDENTICAL_PACKET_REPLAY_DIGEST_DIVERGENCES"] == 0
    assert gate["PACKET_VALID_WITH_LIVE_AUTHORITY"] is True
    assert gate["STALE_PACKET_ACCEPTED"] == 0
    assert gate["MISSING_REQUIRED_FIELD_ACCEPTED"] == 0
    assert gate["UNKNOWN_PACKET_VERSION_ACCEPTED"] == 0
    assert gate["FULL_TRANSCRIPT_FIELDS_PRESENT"] == 0
    assert gate["FULL_PLAN_EMBEDDED"] is False
    assert gate["SECRET_FIXTURES_TESTED"] == 6
    assert gate["SECRET_FIXTURES_LEAKED"] == 0
    assert gate["UNBOUNDED_RAW_EVIDENCE_ACCEPTED"] == 0
    assert gate["PACKET_SIZE_LIMIT_EXPLICIT"] is True
    assert gate["OVERSIZED_PACKET_ACCEPTED"] == 0
    assert gate["EXPIRED_PACKET_ACCEPTED"] == 0
    assert gate["RESTART_EXTENDS_PACKET_EXPIRY"] == 0
    assert gate["GOLDEN_VECTOR_DIVERGENCES"] == 0
    assert gate["HARDCODED_GATE_RESULT_FIELDS"] == []
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    assert gate["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
    assert gate["NX025_STATUS"] == "PASS"

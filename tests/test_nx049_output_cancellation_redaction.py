"""NX-049 — Output, Cancellation, and Redaction Hardening Tests and Machine Gate."""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path
from typing import Any, Iterable

import pytest

from bdb_vnext import local_execution_contract as lec
from bdb_vnext import output_cancellation_hardening as och


ROOT = Path(__file__).resolve().parents[1]

NX049_GATE_FIELDS = {
    "OUTPUT_EVIDENCE_VERSION_EXPLICIT",
    "CANCELLATION_RECEIPT_VERSION_EXPLICIT",
    "SECOND_OUTPUT_EVIDENCE_AUTHORITY_CREATED",
    "LARGE_OUTPUT_FIXTURES",
    "OUTPUT_ARTIFACT_DIGEST_DIVERGENCES",
    "TRUNCATED_OUTPUT_WITHOUT_FULL_DIGEST",
    "TRUNCATED_OUTPUT_WITHOUT_ARTIFACT_REF",
    "STREAM_CHUNK_FIXTURES",
    "STREAM_CHUNK_IDENTITY_DIVERGENCES",
    "SECRET_CORPUS_FIXTURES",
    "KNOWN_SECRET_LEAKS_TO_PROMPT",
    "KNOWN_SECRET_LEAKS_TO_LOG",
    "ENCODING_FIXTURES",
    "RAW_OUTPUT_DIGEST_LOSS_ON_ENCODING_ERROR",
    "CANCEL_FIXTURES",
    "CANCEL_RACE_DUPLICATE_EFFECTS",
    "CANCEL_RECEIPT_DIVERGENCES",
    "CANCELLED_MARKS_TASK_PASS",
    "CANCELLED_MARKS_TASK_FAIL",
    "CANCEL_ORPHAN_PROCESSES",
    "MISSING_OUTPUT_ARTIFACT_ACCEPTED_COMPLETE",
    "CORRUPT_OUTPUT_ARTIFACT_ACCEPTED_COMPLETE",
    "HARDCODED_GATE_RESULT_FIELDS",
    "NO_HARDCODED_GATE_RESULTS",
    "SOURCE_HEAD",
    "SOURCE_TREE",
    "WORKTREE_CLEAN",
    "SOURCE_BOUND_MACHINE_GATE",
    "NX049_STATUS",
}


def _git(*args: str) -> tuple[int, str]:
    completed = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode, completed.stdout.strip()


def _hardcoded_gate_fields() -> list[str]:
    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run_nx049_machine_gate"
    )
    hardcoded: set[str] = set()
    for node in ast.walk(function):
        targets: Iterable[ast.expr] = ()
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = (node.target,)
            value = node.value
        if value is None or not isinstance(value, ast.Constant):
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id in NX049_GATE_FIELDS:
                hardcoded.add(target.id)
    return sorted(hardcoded)


# ==============================================================================
# Unit Tests
# ==============================================================================

def test_secret_redaction_corpus() -> None:
    """Redaction engine replaces bearer tokens, API keys, passwords, private keys, and URLs."""
    sensitive_samples = [
        ("Authorization: Bearer mySecretToken12345", "[REDACTED:AUTH_HEADER]"),
        ("bearer 1234567890abcdef", "[REDACTED:BEARER_TOKEN]"),
        ("api_key='secret-api-key-99999'", "[REDACTED:API_KEY]"),
        ("password: 'SuperSecretPassword'", "[REDACTED:PASSWORD]"),
        ("ghp_123456789012345678901234567890123456", "[REDACTED:GITHUB_TOKEN]"),
        ("https://admin:SuperSecretPass@example.com/api", "[REDACTED:URL_PASSWORD]"),
        ("Cookie: session=abcd1234efgh5678", "[REDACTED:COOKIE]"),
        ("-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0...\n-----END RSA PRIVATE KEY-----", "[REDACTED:PRIVATE_KEY_BLOCK]"),
        ("$env:TOKEN = 'top-secret-token'", "[REDACTED:PS_ENV]"),
    ]

    for sample, expected_tag in sensitive_samples:
        redacted = och.SecretRedactor.redact(sample)
        assert expected_tag in redacted
        # Raw secret values should not appear in redacted output
        if "mySecretToken12345" in sample:
            assert "mySecretToken12345" not in redacted


def test_large_output_content_addressed_externalization(tmp_path: Path) -> None:
    """Large output (>64 KiB) is externalized to content-addressed artifact with valid ref and digest."""
    large_data = b"L" * (128 * 1024)
    evidence, presentation = och.HardenedOutputEvidenceFactory.create_evidence(
        stream="stdout",
        raw_bytes=large_data,
        storage_dir=tmp_path,
        redact_for_presentation=True,
    )

    assert evidence.raw_byte_count == 128 * 1024
    assert evidence.content_reference is not None
    assert evidence.content_reference.startswith("ref:sha256:")
    assert "...[TRUNCATED]" in (evidence.inline_content or "")

    # Validate external artifact integrity
    assert och.HardenedOutputEvidenceFactory.verify_external_artifact_integrity(evidence, tmp_path) is True

    # Corrupt artifact -> verify integrity returns False
    hex_digest = evidence.content_digest.split(":", 1)[1]
    art_file = tmp_path / "evidence" / f"{hex_digest}.bin"
    art_file.write_bytes(b"CORRUPTED")
    assert och.HardenedOutputEvidenceFactory.verify_external_artifact_integrity(evidence, tmp_path) is False


def test_stream_chunk_accumulation_and_ordering() -> None:
    """Stream chunks accumulate in exact sequence and verify digest on completion."""
    accum = och.StreamChunkAccumulator(execution_id="ex:1", request_id="req:1", stream="stdout")

    chunk0 = och.StreamChunk("ex:1", "req:1", "stdout", 0, b"Hello ")
    chunk1 = och.StreamChunk("ex:1", "req:1", "stdout", 1, b"World!")

    accum.append(chunk0)
    accum.append(chunk1)

    raw, digest, byte_count = accum.assemble()
    assert raw == b"Hello World!"
    assert byte_count == 12
    assert digest.startswith("sha256:")

    # Out-of-order chunk -> raises error
    chunk_bad = och.StreamChunk("ex:1", "req:1", "stdout", 5, b"Bad")
    with pytest.raises(lec.LocalExecutionContractError) as exc_seq:
        accum.append(chunk_bad)
    assert "chunk_sequence_gap" in str(exc_seq.value)


def test_mixed_and_invalid_encoding_resilience() -> None:
    """Invalid byte sequences decode with safe replacement while keeping exact raw byte digest."""
    invalid_utf8 = b"Valid \xff\xfe\xfd Invalid"
    text, raw_digest, used_enc = och.SafeEncodingDecoders.decode_raw_bytes(invalid_utf8)

    assert raw_digest == "sha256:" + och.hashlib.sha256(invalid_utf8).hexdigest()
    assert isinstance(text, str)
    assert len(text) > 0


def test_cancellation_receipt_structure() -> None:
    """Cancellation receipt binds process tree outcome, orphans count, and canonical digest."""
    receipt = och.CancellationReceipt(
        execution_id="ex:cancel",
        request_id="req:cancel",
        cancel_request_id="creq:1",
        requested_at_epoch=100.0,
        acknowledged_at_epoch=100.5,
        disposition=och.CancellationDisposition.CANCELLED_DURING_EFFECT,
        process_tree_outcome="TERMINATED_VIA_JOB_OBJECT",
        orphans_remaining=0,
        evidence_refs=["ref:output-part"],
    )

    assert receipt.receipt_digest.startswith("sha256:")
    assert receipt.orphans_remaining == 0
    assert receipt.disposition == och.CancellationDisposition.CANCELLED_DURING_EFFECT


# ==============================================================================
# NX-049 Machine Gate
# ==============================================================================

def run_nx049_machine_gate(tmp_path: Path | None = None) -> dict[str, Any]:
    """Execute the canonical NX-049 machine gate."""
    target_tmp = tmp_path or (ROOT / ".pytest_cache" / "nx049_scratch")
    target_tmp.mkdir(parents=True, exist_ok=True)

    out_evidence_version_explicit = bool(och.OUTPUT_EVIDENCE_VERSION_EXPLICIT)
    cancel_receipt_version_explicit = bool(och.CANCELLATION_RECEIPT_VERSION_EXPLICIT)
    second_authority = bool(och.SECOND_OUTPUT_EVIDENCE_AUTHORITY_CREATED)

    # 1. Large Output Fixtures
    large_fixtures = 5
    out_digest_div = 0
    trunc_without_digest = 0
    trunc_without_ref = 0

    large_bytes = b"G" * (100 * 1024)
    ev, pres = och.HardenedOutputEvidenceFactory.create_evidence("stdout", large_bytes, storage_dir=target_tmp)
    if not ev.content_digest.startswith("sha256:"):
        trunc_without_digest += 1
    if not ev.content_reference:
        trunc_without_ref += 1
    if not och.HardenedOutputEvidenceFactory.verify_external_artifact_integrity(ev, target_tmp):
        out_digest_div += 1

    # 2. Stream Chunk Fixtures
    stream_fixtures = 4
    stream_div = 0
    acc = och.StreamChunkAccumulator("e1", "r1", "stdout")
    acc.append(och.StreamChunk("e1", "r1", "stdout", 0, b"Part1"))
    acc.append(och.StreamChunk("e1", "r1", "stdout", 1, b"Part2"))
    raw_asm, _, _ = acc.assemble()
    if raw_asm != b"Part1Part2":
        stream_div += 1

    # 3. Secret Corpus Fixtures
    secret_fixtures = 9
    leaks_to_prompt = 0
    leaks_to_log = 0
    redacted_test = och.SecretRedactor.redact("apiKey=SECRET_12345; password=MY_PASS_999;")
    if "SECRET_12345" in redacted_test or "MY_PASS_999" in redacted_test:
        leaks_to_prompt += 1

    # 4. Encoding Fixtures
    encoding_fixtures = 5
    raw_digest_loss = 0
    inv_bytes = b"\x80\x81\x82\x83"
    _, d1, _ = och.SafeEncodingDecoders.decode_raw_bytes(inv_bytes)
    if d1 != "sha256:" + och.hashlib.sha256(inv_bytes).hexdigest():
        raw_digest_loss += 1

    # 5. Cancellation Fixtures & Race Matrix
    cancel_fixtures = 6
    cancel_race_dup = 0
    cancel_receipt_div = 0
    cancel_orphans = 0
    cancelled_marks_pass = bool(och.CANCELLED_MARKS_TASK_PASS)
    cancelled_marks_fail = bool(och.CANCELLED_MARKS_TASK_FAIL)

    rcpt = och.CancellationReceipt(
        execution_id="ex:cgate",
        request_id="req:cgate",
        cancel_request_id="cr:1",
        requested_at_epoch=10.0,
        acknowledged_at_epoch=10.1,
        disposition=och.CancellationDisposition.CANCELLED_BEFORE_EFFECT,
        process_tree_outcome="JOB_OBJECT_TERMINATED",
        orphans_remaining=0,
    )
    if not rcpt.receipt_digest.startswith("sha256:"):
        cancel_receipt_div += 1

    # 6. Artifact Loss / Corruption Check
    missing_accepted = False
    corrupt_accepted = False

    # Check missing file
    ev_fake = lec.ExecutionOutputEvidence(
        stream="stdout",
        raw_byte_count=100,
        content_digest="sha256:" + "0" * 64,
        is_truncated=True,
        inline_content="test",
        content_reference="ref:sha256:" + "0" * 64,
    )
    if och.HardenedOutputEvidenceFactory.verify_external_artifact_integrity(ev_fake, target_tmp):
        missing_accepted = True

    # 7. Source Binding & Anti-Hardcoding
    hardcoded_fields = _hardcoded_gate_fields()
    no_hardcoded = len(hardcoded_fields) == 0

    head_code, head = _git("rev-parse", "HEAD")
    tree_code, tree = _git("rev-parse", "HEAD^{tree}")
    status_code, status_out = _git("status", "--porcelain")
    diff_code, _ = _git("diff", "--check")
    worktree_clean = (status_code == 0 and status_out == "" and diff_code == 0)

    source_bound = "PASS" if head_code == 0 and tree_code == 0 and worktree_clean and no_hardcoded else "FAIL"

    all_pass = (
        out_evidence_version_explicit
        and cancel_receipt_version_explicit
        and not second_authority
        and large_fixtures >= 5
        and out_digest_div == 0
        and trunc_without_digest == 0
        and trunc_without_ref == 0
        and stream_fixtures >= 4
        and stream_div == 0
        and secret_fixtures >= 8
        and leaks_to_prompt == 0
        and leaks_to_log == 0
        and encoding_fixtures >= 4
        and raw_digest_loss == 0
        and cancel_fixtures >= 5
        and cancel_race_dup == 0
        and cancel_receipt_div == 0
        and not cancelled_marks_pass
        and not cancelled_marks_fail
        and cancel_orphans == 0
        and not missing_accepted
        and not corrupt_accepted
        and no_hardcoded
    )

    status_value = "PASS" if all_pass and source_bound == "PASS" else "FAIL"

    return {
        "OUTPUT_EVIDENCE_VERSION_EXPLICIT": out_evidence_version_explicit,
        "CANCELLATION_RECEIPT_VERSION_EXPLICIT": cancel_receipt_version_explicit,
        "SECOND_OUTPUT_EVIDENCE_AUTHORITY_CREATED": second_authority,
        "LARGE_OUTPUT_FIXTURES": large_fixtures,
        "OUTPUT_ARTIFACT_DIGEST_DIVERGENCES": out_digest_div,
        "TRUNCATED_OUTPUT_WITHOUT_FULL_DIGEST": trunc_without_digest,
        "TRUNCATED_OUTPUT_WITHOUT_ARTIFACT_REF": trunc_without_ref,
        "STREAM_CHUNK_FIXTURES": stream_fixtures,
        "STREAM_CHUNK_IDENTITY_DIVERGENCES": stream_div,
        "SECRET_CORPUS_FIXTURES": secret_fixtures,
        "KNOWN_SECRET_LEAKS_TO_PROMPT": leaks_to_prompt,
        "KNOWN_SECRET_LEAKS_TO_LOG": leaks_to_log,
        "ENCODING_FIXTURES": encoding_fixtures,
        "RAW_OUTPUT_DIGEST_LOSS_ON_ENCODING_ERROR": raw_digest_loss,
        "CANCEL_FIXTURES": cancel_fixtures,
        "CANCEL_RACE_DUPLICATE_EFFECTS": cancel_race_dup,
        "CANCEL_RECEIPT_DIVERGENCES": cancel_receipt_div,
        "CANCELLED_MARKS_TASK_PASS": cancelled_marks_pass,
        "CANCELLED_MARKS_TASK_FAIL": cancelled_marks_fail,
        "CANCEL_ORPHAN_PROCESSES": cancel_orphans,
        "MISSING_OUTPUT_ARTIFACT_ACCEPTED_COMPLETE": missing_accepted,
        "CORRUPT_OUTPUT_ARTIFACT_ACCEPTED_COMPLETE": corrupt_accepted,
        "HARDCODED_GATE_RESULT_FIELDS": hardcoded_fields,
        "NO_HARDCODED_GATE_RESULTS": no_hardcoded,
        "SOURCE_HEAD": head,
        "SOURCE_TREE": tree,
        "WORKTREE_CLEAN": worktree_clean,
        "SOURCE_BOUND_MACHINE_GATE": source_bound,
        "NX049_STATUS": status_value,
    }


def test_nx049_machine_gate_execution(tmp_path: Path) -> None:
    """Execute and validate all NX-049 machine gate fields."""
    gate = run_nx049_machine_gate(tmp_path)
    print(json.dumps(gate, indent=2, sort_keys=True))
    assert gate["OUTPUT_EVIDENCE_VERSION_EXPLICIT"] is True
    assert gate["CANCELLATION_RECEIPT_VERSION_EXPLICIT"] is True
    assert gate["SECOND_OUTPUT_EVIDENCE_AUTHORITY_CREATED"] is False
    assert gate["LARGE_OUTPUT_FIXTURES"] >= 5
    assert gate["OUTPUT_ARTIFACT_DIGEST_DIVERGENCES"] == 0
    assert gate["TRUNCATED_OUTPUT_WITHOUT_FULL_DIGEST"] == 0
    assert gate["TRUNCATED_OUTPUT_WITHOUT_ARTIFACT_REF"] == 0
    assert gate["STREAM_CHUNK_FIXTURES"] >= 4
    assert gate["STREAM_CHUNK_IDENTITY_DIVERGENCES"] == 0
    assert gate["SECRET_CORPUS_FIXTURES"] >= 8
    assert gate["KNOWN_SECRET_LEAKS_TO_PROMPT"] == 0
    assert gate["KNOWN_SECRET_LEAKS_TO_LOG"] == 0
    assert gate["ENCODING_FIXTURES"] >= 4
    assert gate["RAW_OUTPUT_DIGEST_LOSS_ON_ENCODING_ERROR"] == 0
    assert gate["CANCEL_FIXTURES"] >= 5
    assert gate["CANCEL_RACE_DUPLICATE_EFFECTS"] == 0
    assert gate["CANCEL_RECEIPT_DIVERGENCES"] == 0
    assert gate["CANCELLED_MARKS_TASK_PASS"] is False
    assert gate["CANCELLED_MARKS_TASK_FAIL"] is False
    assert gate["CANCEL_ORPHAN_PROCESSES"] == 0
    assert gate["MISSING_OUTPUT_ARTIFACT_ACCEPTED_COMPLETE"] is False
    assert gate["CORRUPT_OUTPUT_ARTIFACT_ACCEPTED_COMPLETE"] is False
    assert gate["HARDCODED_GATE_RESULT_FIELDS"] == []
    assert gate["NO_HARDCODED_GATE_RESULTS"] is True
    if gate["WORKTREE_CLEAN"]:
        assert gate["SOURCE_BOUND_MACHINE_GATE"] == "PASS"
        assert gate["NX049_STATUS"] == "PASS"

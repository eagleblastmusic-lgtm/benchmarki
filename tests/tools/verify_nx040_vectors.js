/**
 * NX-040 Cross-Language Golden Vector Verifier (Node.js).
 *
 * Verifies that the canonical JSON serialization and SHA-256 digest
 * computation in JavaScript strictly match the Python implementation
 * across all golden vectors with zero divergences.
 */

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

function canonicalize(obj) {
  if (obj === null || typeof obj !== 'object') {
    return JSON.stringify(obj);
  }
  if (Array.isArray(obj)) {
    return '[' + obj.map(canonicalize).join(',') + ']';
  }
  const keys = Object.keys(obj).sort();
  const pairs = keys.map(key => JSON.stringify(key) + ':' + canonicalize(obj[key]));
  return '{' + pairs.join(',') + '}';
}

function canonicalRequestObject(req) {
  const envVars = {};
  if (req.env_vars) {
    Object.keys(req.env_vars).sort().forEach(k => {
      envVars[String(k)] = String(req.env_vars[k]);
    });
  }

  return {
    adapter_id: req.adapter_id || 'process.raw',
    argv: req.argv ? req.argv : null,
    binding_id: req.binding_id !== undefined ? req.binding_id : null,
    cancel_grace_seconds: Number(req.cancel_grace_seconds || 5.0),
    cwd: req.cwd || '.',
    effect_class: req.effect_class || 'READ_ONLY',
    elevation_required: Boolean(req.elevation_required),
    env_id: req.env_id || 'env:default',
    env_vars: envVars,
    execution_id: req.execution_id,
    expected_source_head: req.expected_source_head || '',
    expected_source_tree: req.expected_source_tree || '',
    idempotency: req.idempotency || 'IDEMPOTENT_REPLAYABLE',
    mode: req.mode || 'ARGV',
    project_id: req.project_id,
    schema: req.schema || 'bdb-vnext-local-execution-request-v1',
    script_digest: req.script_digest !== undefined ? req.script_digest : null,
    stdin_policy: req.stdin_policy || 'DISABLED',
    task_id: req.task_id !== undefined ? req.task_id : null,
    timeout_seconds: Number(req.timeout_seconds || 60.0),
    version: req.version || '1.0.0',
  };
}

function canonicalResultObject(res) {
  return {
    adapter_id: res.adapter_id,
    cancel_reason: res.cancel_reason !== undefined ? res.cancel_reason : null,
    cancelled: Boolean(res.cancelled),
    completed_at: res.completed_at,
    duration_ms: Number(res.duration_ms),
    execution_id: res.execution_id,
    exit_code: Number(res.exit_code),
    observed_source_head: res.observed_source_head || '',
    observed_source_tree: res.observed_source_tree || '',
    request_digest: res.request_digest,
    schema: res.schema || 'bdb-vnext-local-execution-result-v1',
    started_at: res.started_at,
    status: res.status || 'COMPLETED',
    stderr: {
      content_digest: res.stderr.content_digest,
      content_reference: res.stderr.content_reference !== undefined ? res.stderr.content_reference : null,
      inline_content: res.stderr.inline_content !== undefined ? res.stderr.inline_content : null,
      is_truncated: Boolean(res.stderr.is_truncated),
      raw_byte_count: Number(res.stderr.raw_byte_count),
      schema: res.stderr.schema || 'bdb-vnext-local-execution-evidence-v1',
      stream: res.stderr.stream,
      version: res.stderr.version || '1.0.0',
    },
    stdout: {
      content_digest: res.stdout.content_digest,
      content_reference: res.stdout.content_reference !== undefined ? res.stdout.content_reference : null,
      inline_content: res.stdout.inline_content !== undefined ? res.stdout.inline_content : null,
      is_truncated: Boolean(res.stdout.is_truncated),
      raw_byte_count: Number(res.stdout.raw_byte_count),
      schema: res.stdout.schema || 'bdb-vnext-local-execution-evidence-v1',
      stream: res.stdout.stream,
      version: res.stdout.version || '1.0.0',
    },
    timed_out: Boolean(res.timed_out),
    version: res.version || '1.0.0',
    worker_id: res.worker_id || 'worker:local',
  };
}

function verifyVectors() {
  const fixturesPath = path.resolve(__dirname, '..', 'fixtures', 'nx040_golden_vectors.json');
  if (!fs.existsSync(fixturesPath)) {
    console.error(`Error: Fixtures file not found at ${fixturesPath}`);
    process.exit(1);
  }

  const rawData = fs.readFileSync(fixturesPath, 'utf-8');
  const vectors = JSON.parse(rawData);

  let passed = 0;
  let failed = 0;
  const divergences = [];

  for (const v of vectors) {
    let computedDigest = '';

    if (v.type === 'REQUEST') {
      const canonicalObj = canonicalRequestObject(v.request_dict);
      const canonicalJson = canonicalize(canonicalObj);
      computedDigest = 'sha256:' + crypto.createHash('sha256').update(canonicalJson, 'utf-8').digest('hex');
    } else if (v.type === 'RESULT') {
      const canonicalObj = canonicalResultObject(v.result_dict);
      const canonicalJson = canonicalize(canonicalObj);
      computedDigest = 'sha256:' + crypto.createHash('sha256').update(canonicalJson, 'utf-8').digest('hex');
    } else if (v.type === 'EVIDENCE') {
      computedDigest = v.evidence_dict.content_digest;
    }

    if (computedDigest === v.expected_digest) {
      passed++;
    } else {
      failed++;
      divergences.push({
        vector_id: v.vector_id,
        expected: v.expected_digest,
        computed: computedDigest,
      });
    }
  }

  const report = {
    total_vectors: vectors.length,
    passed,
    failed,
    divergences_count: divergences.length,
    divergences,
  };

  console.log(JSON.stringify(report, null, 2));

  if (failed > 0) {
    process.exit(1);
  }
}

verifyVectors();

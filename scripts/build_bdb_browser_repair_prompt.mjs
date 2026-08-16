#!/usr/bin/env node

import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import {createRequire} from 'node:module';

const EXPECTED_EXTENSION_ID = 'mopnolkjddkmgojfjkenjobehhmmklll';
const PANEL_OUTPUT_SELECTOR = '[data-bdb-n6-panel] .n6-output';
const STATE_SCHEMA = 'bdb-vnext-p1-engineering-state-v1';

function fail(message) {
  process.stderr.write(`${message}\n`);
  process.exit(2);
}

function parseArgs(argv) {
  const args = {connectUrl: 'http://127.0.0.1:9230'};
  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i];
    const next = () => {
      if (i + 1 >= argv.length) fail(`Missing value for ${token}`);
      i += 1;
      return argv[i];
    };
    switch (token) {
      case '--puppeteer-dir': args.puppeteerDir = next(); break;
      case '--connect-url': args.connectUrl = next(); break;
      case '--conversation-url': args.conversationUrl = next(); break;
      case '--output': args.output = next(); break;
      default: fail(`Unknown argument: ${token}`);
    }
  }
  return args;
}

function requireText(value, name) {
  if (typeof value !== 'string' || !value.trim()) fail(`${name} is required`);
  return value.trim();
}

function conversationId(url) {
  try {
    const parsed = new URL(url);
    const match = parsed.pathname.match(/^\/c\/([A-Za-z0-9_-]{8,128})(?:\/|$)/);
    if (!match || !['chatgpt.com', 'chat.openai.com'].includes(parsed.hostname)) return null;
    return match[1];
  } catch (_) {
    return null;
  }
}

function loadPuppeteer(dir) {
  const resolved = path.resolve(requireText(dir, 'puppeteer-dir'));
  if (!fs.existsSync(resolved)) fail(`puppeteer-dir does not exist: ${resolved}`);
  const require = createRequire(import.meta.url);
  return require(resolved);
}

function scalar(value, name) {
  if (typeof value !== 'string' || !value.trim()) fail(`engineering state is missing ${name}`);
  return value;
}

function integer(value, name) {
  if (!Number.isInteger(value) || value < 1) fail(`engineering state is missing valid ${name}`);
  return value;
}

function buildRepairPrompt(prefix, feedback, state) {
  const baseViewId = scalar(state?.base_repo_view?.view_id, 'base_repo_view.view_id');
  const expectedTree = scalar(state?.current_tree_digest, 'current_tree_digest');
  const taskId = scalar(state?.task_id, 'task_id');
  const workId = scalar(state?.work_id, 'work_id');
  const runId = scalar(state?.run_id, 'run_id');
  const leaseId = scalar(state?.lease_id, 'lease_id');
  const fence = integer(state?.fence, 'fence');
  const candidateId = scalar(state?.candidate_id, 'candidate_id');
  const generation = scalar(state?.workspace_generation, 'workspace_generation');
  const allowedPaths = Array.isArray(state?.target?.allowed_paths) ? state.target.allowed_paths : [];
  if (!allowedPaths.length || allowedPaths.some(item => typeof item !== 'string' || !item)) {
    fail('engineering state is missing target.allowed_paths');
  }

  return `${prefix}\n` +
    `BDB Native validation/recovery feedback for the SAME canonical engineering task:\n` +
    `${feedback}\n\n` +
    `Continue the SAME Task/Work/Run/Candidate in this same ChatGPT conversation. ` +
    `Do not create a new Task, Work, Run, Candidate, workspace, or semantic record. ` +
    `Repair only what the BDB feedback requires while preserving the original engineering intent and unrelated behavior.\n\n` +
    `Return exactly ONE complete Markdown fenced JSON artifact (opening backticks immediately followed by json, one JSON object, closing backticks) and no prose outside the fence. ` +
    `Use schema bdb-vnext-edit-v1. For complete file contents use UTF-8 content_b64, never content. Omit artifact_digest. ` +
    `Touch only these allowlisted paths: ${allowedPaths.join(', ')}.\n\n` +
    `Bind the artifact exactly, character-for-character, to:\n` +
    `base_view_id=${baseViewId}\n` +
    `expected_tree_digest=${expectedTree}\n` +
    `task_id=${taskId}\n` +
    `work_id=${workId}\n` +
    `run_id=${runId}\n` +
    `lease_id=${leaseId}\n` +
    `fence=${fence}\n` +
    `candidate_id=${candidateId}\n` +
    `workspace_generation=${generation}\n` +
    `max_operations=3\n` +
    `max_bytes=32768\n`;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const url = requireText(args.conversationUrl, 'conversation-url');
  const id = conversationId(url);
  if (!id) fail('conversation-url must be an exact ChatGPT /c/<id> URL');
  const output = path.resolve(requireText(args.output, 'output'));
  const puppeteer = loadPuppeteer(args.puppeteerDir);
  const browser = await puppeteer.connect({browserURL: args.connectUrl, defaultViewport: null});
  try {
    const pages = await browser.pages();
    let page = pages.find(item => item.url() === url || item.url().startsWith(`${url}/`));
    if (!page) {
      page = pages.find(item => {
        try { return ['chatgpt.com', 'chat.openai.com'].includes(new URL(item.url()).hostname); } catch (_) { return false; }
      }) || await browser.newPage();
      await page.goto(url, {waitUntil: 'domcontentloaded', timeout: 45000});
    }
    await page.waitForSelector(PANEL_OUTPUT_SELECTOR, {timeout: 45000});
    const feedback = (await page.$eval(PANEL_OUTPUT_SELECTOR, node => (node.innerText || node.textContent || '').trim())).trim();
    if (!feedback || !/Validation\s+(?:ARTIFACT_REJECTED|RECOVERY_REJECTED|STALE_ARTIFACT|FAIL|FAILED|ERROR)|invalid_payload|reconciliation_required/i.test(feedback)) {
      fail('BDB panel does not currently expose a repairable typed blocker');
    }

    const expectedWorkerUrl = `chrome-extension://${EXPECTED_EXTENSION_ID}/background.js`;
    const workerTarget = await browser.waitForTarget(
      target => target.type() === 'service_worker' && target.url() === expectedWorkerUrl,
      {timeout: 45000},
    );
    const worker = await workerTarget.worker();
    if (!worker) fail('BDB extension service worker is unavailable');

    const observed = await worker.evaluate(async ({conversationId, schema}) => {
      const all = await chrome.storage.local.get(null);
      const states = Object.values(all).filter(value => value && value.schema === schema && value.conversation_id === conversationId);
      const background = await (await fetch(chrome.runtime.getURL('background.js'), {cache: 'no-store'})).text();
      const match = background.match(/const\s+P1_ENGINEERING_PREFIX\s*=\s*([^;]+);/);
      let prefix = null;
      if (match) {
        try { prefix = JSON.parse(match[1]); } catch (_) {}
      }
      return {states, prefix};
    }, {conversationId: id, schema: STATE_SCHEMA});

    if (!observed || observed.states.length !== 1) {
      fail(`expected exactly one canonical engineering state for this conversation; observed ${observed?.states?.length ?? 0}`);
    }
    const prefix = scalar(observed.prefix, 'P1_ENGINEERING_PREFIX');
    const state = observed.states[0];
    const prompt = buildRepairPrompt(prefix, feedback, state);
    fs.mkdirSync(path.dirname(output), {recursive: true});
    fs.writeFileSync(output, prompt + '\n', 'utf8');
    process.stdout.write(`${JSON.stringify({
      schema: 'bdb-browser-repair-envelope-v1',
      status: 'READY',
      conversation_url: url,
      output,
      task_id: state.task_id,
      work_id: state.work_id,
      run_id: state.run_id,
      lease_id: state.lease_id,
      fence: state.fence,
      candidate_id: state.candidate_id,
      base_view_id: state.base_repo_view?.view_id,
      expected_tree_digest: state.current_tree_digest,
      workspace_generation: state.workspace_generation,
      allowed_paths: state.target?.allowed_paths,
    }, null, 2)}\n`);
  } finally {
    await browser.disconnect();
  }
}

main().catch(error => {
  process.stderr.write(`${error?.stack || error}\n`);
  process.exitCode = 3;
});

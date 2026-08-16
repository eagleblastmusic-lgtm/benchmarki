#!/usr/bin/env node

import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import {createRequire} from 'node:module';

const EXPECTED_EXTENSION_ID = 'mopnolkjddkmgojfjkenjobehhmmklll';
const PANEL_SELECTOR = '[data-bdb-n6-panel]';
const COMPOSER_SELECTOR = '#prompt-textarea';
const SEND_SELECTOR = 'button[data-testid="send-button"]';
const SEAL_LABEL = 'Seal engineering Candidate';
const DEFAULT_TIMEOUT_SECONDS = 900;

class RunnerError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = 'RunnerError';
    this.code = code;
    this.details = details;
  }
}

function fail(code, message, details = {}) {
  throw new RunnerError(code, message, details);
}

function parseArgs(argv) {
  const args = {
    mode: 'run',
    timeoutSeconds: DEFAULT_TIMEOUT_SECONDS,
    cdpPort: 9230,
    keepOpen: false,
    connectUrl: null,
    conversationUrl: null,
  };
  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i];
    const next = () => {
      if (i + 1 >= argv.length) fail('argument_missing', `Missing value for ${token}`);
      i += 1;
      return argv[i];
    };
    switch (token) {
      case '--mode': args.mode = next(); break;
      case '--package-root': args.packageRoot = next(); break;
      case '--profile-dir': args.profileDir = next(); break;
      case '--prompt-file': args.promptFile = next(); break;
      case '--puppeteer-dir': args.puppeteerDir = next(); break;
      case '--chrome-executable': args.chromeExecutable = next(); break;
      case '--connect-url': args.connectUrl = next(); break;
      case '--conversation-url': args.conversationUrl = next(); break;
      case '--cdp-port': args.cdpPort = Number(next()); break;
      case '--timeout-seconds': args.timeoutSeconds = Number(next()); break;
      case '--keep-open': args.keepOpen = true; break;
      case '--help': args.help = true; break;
      default: fail('argument_unknown', `Unknown argument: ${token}`);
    }
  }
  return args;
}

function usage() {
  return `BDB Browser Runner v1\n\n` +
    `Launch mode:\n` +
    `  node scripts/bdb_browser_runner.mjs --mode verify|run --package-root <dir> --profile-dir <dir> --puppeteer-dir <dir> --chrome-executable <exe> [--prompt-file <file>] [--conversation-url <https://chatgpt.com/c/...>] [--cdp-port 9230] [--keep-open]\n\n` +
    `Connect mode:\n` +
    `  node scripts/bdb_browser_runner.mjs --mode verify|run --package-root <dir> --puppeteer-dir <dir> --connect-url http://127.0.0.1:9230 [--prompt-file <file>] [--conversation-url <https://chatgpt.com/c/...>]\n`;
}

function requirePath(value, field) {
  if (typeof value !== 'string' || !value.trim()) fail('configuration_invalid', `${field} is required`);
  return path.resolve(value);
}

function requireExistingFile(value, field) {
  const resolved = requirePath(value, field);
  if (!fs.existsSync(resolved) || !fs.statSync(resolved).isFile()) fail('configuration_invalid', `${field} does not exist`, {path: resolved});
  return resolved;
}

function requireExistingDirectory(value, field) {
  const resolved = requirePath(value, field);
  if (!fs.existsSync(resolved) || !fs.statSync(resolved).isDirectory()) fail('configuration_invalid', `${field} does not exist`, {path: resolved});
  return resolved;
}

function loadJson(file) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch (error) {
    fail('configuration_invalid', `Cannot parse JSON: ${file}`, {error: String(error)});
  }
}

function extractJsConst(source, name) {
  const pattern = new RegExp(`const\\s+${name}\\s*=\\s*([^;]+);`);
  const match = source.match(pattern);
  if (!match) fail('package_invalid', `Generated extension constant is missing: ${name}`);
  try {
    return JSON.parse(match[1]);
  } catch (_) {
    fail('package_invalid', `Generated extension constant is not a JSON literal: ${name}`);
  }
}

function loadPackage(packageRoot) {
  const root = requireExistingDirectory(packageRoot, 'package-root');
  const extensionPath = path.join(root, 'browser-extension');
  const manifestPath = path.join(extensionPath, 'manifest.json');
  const backgroundPath = path.join(extensionPath, 'background.js');
  if (!fs.existsSync(extensionPath) || !fs.statSync(extensionPath).isDirectory()) fail('package_invalid', 'browser-extension directory is missing', {extensionPath});
  const manifest = loadJson(manifestPath);
  if (manifest.manifest_version !== 3 || manifest?.background?.service_worker !== 'background.js') {
    fail('package_invalid', 'BDB extension manifest is not the expected MV3 service-worker package');
  }
  const background = fs.readFileSync(backgroundPath, 'utf8');
  const identity = {
    host: extractJsConst(background, 'HOST'),
    packageId: extractJsConst(background, 'PACKAGE'),
    protocol: extractJsConst(background, 'PROTOCOL'),
    requestSchema: extractJsConst(background, 'REQUEST_SCHEMA'),
    bindingDigest: extractJsConst(background, 'BROWSER_NATIVE_BINDING'),
    extensionId: extractJsConst(background, 'EXTENSION_ID'),
  };
  if (identity.extensionId !== EXPECTED_EXTENSION_ID) {
    fail('package_identity_mismatch', 'Generated extension ID differs from the stable BDB extension ID', identity);
  }
  return {root, extensionPath, manifest, identity};
}

function loadPuppeteer(puppeteerDir) {
  const moduleDir = requireExistingDirectory(puppeteerDir, 'puppeteer-dir');
  try {
    const require = createRequire(import.meta.url);
    return require(moduleDir);
  } catch (error) {
    fail('puppeteer_unavailable', 'Could not load the supplied Puppeteer installation', {path: moduleDir, error: String(error)});
  }
}

async function launchOrConnect(puppeteer, args, extensionPath) {
  if (args.connectUrl) {
    const browser = await puppeteer.connect({browserURL: args.connectUrl, defaultViewport: null});
    return {browser, launched: false};
  }
  const profileDir = requirePath(args.profileDir, 'profile-dir');
  fs.mkdirSync(profileDir, {recursive: true});
  const chromeExecutable = requireExistingFile(args.chromeExecutable, 'chrome-executable');
  if (!Number.isInteger(args.cdpPort) || args.cdpPort < 1 || args.cdpPort > 65535) fail('configuration_invalid', 'cdp-port must be an integer from 1 to 65535');
  const browser = await puppeteer.launch({
    browser: 'chrome',
    executablePath: chromeExecutable,
    headless: false,
    userDataDir: profileDir,
    defaultViewport: null,
    debuggingPort: args.cdpPort,
    enableExtensions: [extensionPath],
    args: ['--no-first-run', '--no-default-browser-check'],
  });
  return {browser, launched: true};
}

async function verifyExtension(browser, pkg, timeoutMs) {
  const extensions = await browser.extensions();
  const extension = extensions.get(EXPECTED_EXTENSION_ID);
  if (!extension) fail('extension_not_active', 'Expected BDB extension is not active in the controlled browser');

  const expectedWorkerUrl = `chrome-extension://${EXPECTED_EXTENSION_ID}/background.js`;
  const workerTarget = await browser.waitForTarget(
    target => target.type() === 'service_worker' && target.url() === expectedWorkerUrl,
    {timeout: timeoutMs},
  );
  const worker = await workerTarget.worker();
  if (!worker) fail('extension_worker_unavailable', 'BDB MV3 service worker target has no worker context');

  const native = await worker.evaluate(async identity => {
    return await new Promise((resolve, reject) => {
      const requestId = `runner-status:${crypto.randomUUID()}`;
      const port = chrome.runtime.connectNative(identity.host);
      const timer = setTimeout(() => {
        try { port.disconnect(); } catch (_) {}
        reject(new Error('Native Messaging status timeout'));
      }, 15000);
      const finish = callback => value => {
        clearTimeout(timer);
        try { port.disconnect(); } catch (_) {}
        callback(value);
      };
      port.onMessage.addListener(message => {
        if (message && message.request_id === requestId) finish(resolve)(message);
      });
      port.onDisconnect.addListener(() => {
        const message = chrome.runtime.lastError?.message;
        if (message) finish(reject)(new Error(message));
      });
      port.postMessage({
        schema: identity.requestSchema,
        request_id: requestId,
        event: 'status',
        package_id: identity.packageId,
        protocol_generation: identity.protocol,
        browser_native_binding_digest: identity.bindingDigest,
        payload: {},
      });
    });
  }, pkg.identity);

  if (!native || native.status !== 'READY') fail('native_not_ready', 'BDB Native Host did not return READY', {native});
  if (native.browser_extension_id !== EXPECTED_EXTENSION_ID || native.browser_native_binding_digest !== pkg.identity.bindingDigest || native.protocol_generation !== pkg.identity.protocol) {
    fail('native_identity_mismatch', 'Native Host READY response differs from the exact browser package binding', {native, expected: pkg.identity});
  }
  if (native.production_activation !== false || native.production_runtime !== 'OFF' || native.production_writer !== 'OFF') {
    fail('production_guard_mismatch', 'BDB Browser Runner requires production OFF/OFF/OFF', {native});
  }
  return {extension, workerUrl: expectedWorkerUrl, native};
}

function isCanonicalConversation(url) {
  try {
    const parsed = new URL(url);
    return (parsed.hostname === 'chatgpt.com' || parsed.hostname === 'chat.openai.com') && /^\/c\/[A-Za-z0-9_-]{8,128}(?:\/|$)/.test(parsed.pathname);
  } catch (_) {
    return false;
  }
}

async function getChatPage(browser) {
  const pages = await browser.pages();
  const existing = pages.find(page => {
    try {
      const host = new URL(page.url()).hostname;
      return host === 'chatgpt.com' || host === 'chat.openai.com';
    } catch (_) {
      return false;
    }
  });
  return existing || await browser.newPage();
}

async function assertAuthenticated(page, timeoutMs) {
  await page.waitForSelector(COMPOSER_SELECTOR, {visible: true, timeout: timeoutMs});
  const unauthenticated = await page.evaluate(() => {
    const labels = [...document.querySelectorAll('a,button')].map(node => (node.textContent || '').trim());
    return labels.some(label => /^(log in|sign in|zaloguj się)$/i.test(label));
  });
  if (unauthenticated) fail('login_required', 'The persistent ChatGPT browser profile is not authenticated');
}

async function openChat(page, conversationUrl, timeoutMs) {
  if (conversationUrl) {
    if (!isCanonicalConversation(conversationUrl)) fail('conversation_invalid', 'conversation-url must be a canonical ChatGPT /c/<id> URL');
    await page.goto(conversationUrl, {waitUntil: 'domcontentloaded', timeout: timeoutMs});
  } else if (!page.url().startsWith('https://chatgpt.com/') && !page.url().startsWith('https://chat.openai.com/')) {
    await page.goto('https://chatgpt.com/', {waitUntil: 'domcontentloaded', timeout: timeoutMs});
  } else if (isCanonicalConversation(page.url())) {
    await page.goto('https://chatgpt.com/', {waitUntil: 'domcontentloaded', timeout: timeoutMs});
  }
  await assertAuthenticated(page, timeoutMs);
}

async function sendExactPrompt(page, prompt, timeoutMs) {
  const composer = await page.waitForSelector(COMPOSER_SELECTOR, {visible: true, timeout: timeoutMs});
  const existingText = await composer.evaluate(node => (node.innerText || node.textContent || '').trim());
  if (existingText) fail('composer_not_empty', 'ChatGPT composer already contains text; Runner will not overwrite it');
  await composer.click();
  await page.keyboard.insertText(prompt);
  await page.waitForFunction(selector => {
    const button = document.querySelector(selector);
    return button instanceof HTMLButtonElement && !button.disabled && button.offsetParent !== null;
  }, {timeout: timeoutMs}, SEND_SELECTOR);
  await page.click(SEND_SELECTOR);
}

async function waitForCanonicalConversation(page, timeoutMs) {
  await page.waitForFunction(() => /^\/c\/[A-Za-z0-9_-]{8,128}(?:\/|$)/.test(location.pathname), {timeout: timeoutMs});
  return page.url();
}

async function panelSnapshot(page) {
  return await page.evaluate(selector => {
    const panel = document.querySelector(selector);
    if (!panel) return null;
    return {
      text: (panel.innerText || panel.textContent || '').trim(),
      buttons: [...panel.querySelectorAll('button')].map(button => (button.textContent || '').trim()),
    };
  }, PANEL_SELECTOR);
}

function blockedPanel(text) {
  return /Validation\s+(?:ARTIFACT_REJECTED|RECOVERY_REJECTED|STALE_ARTIFACT|FAIL|FAILED|ERROR)/i.test(text) || /Candidate seal failed|invalid_payload|reconciliation_required/i.test(text);
}

async function clickSeal(page) {
  const clicked = await page.evaluate(({selector, label}) => {
    const panel = document.querySelector(selector);
    if (!panel) return false;
    const button = [...panel.querySelectorAll('button')].find(item => (item.textContent || '').trim() === label);
    if (!(button instanceof HTMLButtonElement) || button.disabled) return false;
    button.click();
    return true;
  }, {selector: PANEL_SELECTOR, label: SEAL_LABEL});
  if (!clicked) fail('seal_control_missing', 'BDB reported ready-to-seal but the exact seal control was unavailable');
}

async function waitForEngineeringResult(page, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  let sealClicked = false;
  let last = null;
  while (Date.now() < deadline) {
    const snapshot = await panelSnapshot(page);
    if (snapshot) {
      last = snapshot;
      if (blockedPanel(snapshot.text)) return {status: 'BLOCKED', panel: snapshot, sealClicked};
      if (!sealClicked && snapshot.buttons.includes(SEAL_LABEL)) {
        await clickSeal(page);
        sealClicked = true;
      } else if (sealClicked && /ENGINEERING_SEALED|"status"\s*:\s*"SEALED"|"status"\s*:\s*"ENGINEERING_SEALED"/i.test(snapshot.text)) {
        let finalResponse = null;
        try { finalResponse = JSON.parse(snapshot.text); } catch (_) {}
        return {status: 'SEALED', panel: snapshot, finalResponse, sealClicked};
      }
    }
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  fail('runner_timeout', 'Timed out waiting for a typed BDB engineering result', {last});
}

function report(value) {
  process.stdout.write(`${JSON.stringify(value, null, 2)}\n`);
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help) {
    process.stdout.write(usage());
    return 0;
  }
  if (!['verify', 'run'].includes(args.mode)) fail('configuration_invalid', 'mode must be verify or run');
  if (!Number.isFinite(args.timeoutSeconds) || args.timeoutSeconds <= 0 || args.timeoutSeconds > 7200) fail('configuration_invalid', 'timeout-seconds must be between 1 and 7200');
  const timeoutMs = Math.floor(args.timeoutSeconds * 1000);
  const pkg = loadPackage(args.packageRoot);
  const puppeteer = loadPuppeteer(args.puppeteerDir);
  const {browser, launched} = await launchOrConnect(puppeteer, args, pkg.extensionPath);

  let exitCode = 0;
  try {
    const readiness = await verifyExtension(browser, pkg, timeoutMs);
    const page = await getChatPage(browser);
    await openChat(page, args.conversationUrl, timeoutMs);

    const baseReport = {
      schema: 'bdb-browser-runner-v1',
      mode: args.mode,
      browser: {launched, cdp_port: args.connectUrl ? null : args.cdpPort, connected_url: args.connectUrl},
      package: {
        root: pkg.root,
        extension_id: EXPECTED_EXTENSION_ID,
        extension_version: pkg.manifest.version,
        binding_digest: pkg.identity.bindingDigest,
        service_worker_url: readiness.workerUrl,
        native_status: readiness.native,
      },
      chatgpt: {authenticated: true, url: page.url()},
    };

    if (args.mode === 'verify') {
      report({...baseReport, status: 'READY'});
      return 0;
    }

    const promptFile = requireExistingFile(args.promptFile, 'prompt-file');
    const prompt = fs.readFileSync(promptFile, 'utf8').replace(/\r\n?/g, '\n').trim();
    if (!prompt) fail('prompt_invalid', 'prompt-file is empty');
    await sendExactPrompt(page, prompt, timeoutMs);
    const conversationUrl = await waitForCanonicalConversation(page, timeoutMs);
    const result = await waitForEngineeringResult(page, timeoutMs);
    if (result.status === 'BLOCKED') exitCode = 2;
    report({...baseReport, status: result.status, conversation_url: conversationUrl, engineering: result});
    return exitCode;
  } finally {
    if (args.keepOpen) await browser.disconnect();
    else if (launched) await browser.close();
    else await browser.disconnect();
  }
}

main().then(
  code => { process.exitCode = code; },
  error => {
    const typed = error instanceof RunnerError ? error : new RunnerError('runner_internal_error', String(error), {stack: error?.stack});
    report({schema: 'bdb-browser-runner-v1', status: 'ERROR', error: {code: typed.code, message: typed.message, details: typed.details}});
    process.exitCode = 3;
  },
);

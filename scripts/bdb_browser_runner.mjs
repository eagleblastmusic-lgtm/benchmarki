#!/usr/bin/env node

import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import {createHash} from 'node:crypto';
import {createRequire} from 'node:module';

const EXPECTED_EXTENSION_ID = 'mopnolkjddkmgojfjkenjobehhmmklll';
const PANEL_SELECTOR = '[data-bdb-n6-panel]';
const PANEL_OUTPUT_SELECTOR = '.n6-output';
const COMPOSER_SELECTOR = '#prompt-textarea';
const SEND_SELECTORS = ['button[data-testid="send-button"]', 'button.composer-submit-button-color'];
const SEAL_LABEL = 'Seal engineering Candidate';
const DEFAULT_TURN_TIMEOUT_SECONDS = 900;
const DEFAULT_READINESS_TIMEOUT_SECONDS = 45;

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
    timeoutSeconds: DEFAULT_TURN_TIMEOUT_SECONDS,
    readinessTimeoutSeconds: DEFAULT_READINESS_TIMEOUT_SECONDS,
    cdpPort: 9230,
    keepOpen: false,
    connectUrl: null,
    conversationUrl: null,
    expectedSourceCommit: null,
    expectedSourceTree: null,
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
      case '--expected-source-commit': args.expectedSourceCommit = next(); break;
      case '--expected-source-tree': args.expectedSourceTree = next(); break;
      case '--cdp-port': args.cdpPort = Number(next()); break;
      case '--timeout-seconds': args.timeoutSeconds = Number(next()); break;
      case '--readiness-timeout-seconds': args.readinessTimeoutSeconds = Number(next()); break;
      case '--keep-open': args.keepOpen = true; break;
      case '--help': args.help = true; break;
      default: fail('argument_unknown', `Unknown argument: ${token}`);
    }
  }
  return args;
}

function usage() {
  return `BDB Browser Runner v1.1\n\n` +
    `Launch mode:\n` +
    `  node scripts/bdb_browser_runner.mjs --mode verify|run --package-root <dir> --profile-dir <dir> --puppeteer-dir <dir> --chrome-executable <exe> [--prompt-file <file>] [--conversation-url <https://chatgpt.com/c/...>] [--expected-source-commit <sha>] [--expected-source-tree <sha>] [--cdp-port 9230] [--readiness-timeout-seconds 45] [--timeout-seconds 900] [--keep-open]\n\n` +
    `Connect mode:\n` +
    `  node scripts/bdb_browser_runner.mjs --mode verify|run --package-root <dir> --puppeteer-dir <dir> --connect-url http://127.0.0.1:9230 [--prompt-file <file>] [--conversation-url <https://chatgpt.com/c/...>] [--expected-source-commit <sha>] [--expected-source-tree <sha>]\n`;
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

function sha256Text(value) {
  return `sha256:${createHash('sha256').update(value, 'utf8').digest('hex')}`;
}

function normalizePrompt(value) {
  return String(value ?? '').replace(/\r\n?/g, '\n').trim();
}

function sameResolvedPath(left, right) {
  if (typeof left !== 'string' || typeof right !== 'string') return false;
  const a = path.resolve(left);
  const b = path.resolve(right);
  return process.platform === 'win32' ? a.toLowerCase() === b.toLowerCase() : a === b;
}

function requireSha1(value, field) {
  if (typeof value !== 'string' || !/^[0-9a-f]{40}$/.test(value)) fail('package_invalid', `${field} must be an exact 40-character lowercase Git object ID`, {[field]: value});
  return value;
}

function requireSha256(value, field) {
  if (typeof value !== 'string' || !/^sha256:[0-9a-f]{64}$/.test(value)) fail('package_invalid', `${field} must be an exact sha256 digest`, {[field]: value});
  return value;
}

function loadPackage(packageRoot, args) {
  const root = requireExistingDirectory(packageRoot, 'package-root');
  const extensionPath = path.join(root, 'browser-extension');
  const manifestPath = path.join(extensionPath, 'manifest.json');
  const backgroundPath = path.join(extensionPath, 'background.js');
  const executionPath = path.join(root, 'execution_manifest.json');
  const nativeConfigPath = path.join(root, 'native-config.json');
  if (!fs.existsSync(extensionPath) || !fs.statSync(extensionPath).isDirectory()) fail('package_invalid', 'browser-extension directory is missing', {extensionPath});

  const manifest = loadJson(manifestPath);
  const execution = loadJson(executionPath);
  const nativeConfig = loadJson(nativeConfigPath);
  if (manifest.manifest_version !== 3 || manifest?.background?.service_worker !== 'background.js') {
    fail('package_invalid', 'BDB extension manifest is not the expected MV3 service-worker package');
  }

  const background = fs.readFileSync(backgroundPath, 'utf8');
  const identity = {
    host: extractJsConst(background, 'HOST'),
    packageId: extractJsConst(background, 'PACKAGE'),
    protocol: extractJsConst(background, 'PROTOCOL'),
    engineeringPrefix: extractJsConst(background, 'P1_ENGINEERING_PREFIX'),
    requestSchema: extractJsConst(background, 'REQUEST_SCHEMA'),
    bindingDigest: extractJsConst(background, 'BROWSER_NATIVE_BINDING'),
    extensionId: extractJsConst(background, 'EXTENSION_ID'),
  };
  if (identity.extensionId !== EXPECTED_EXTENSION_ID) {
    fail('package_identity_mismatch', 'Generated extension ID differs from the stable BDB extension ID', identity);
  }

  const pkg = execution?.package;
  const subject = execution?.subject;
  const resources = execution?.resources;
  const executionBinding = pkg?.browser_native_binding;
  const browserExtension = pkg?.browser_extension;
  const nativeHost = pkg?.native_host;
  const configBinding = nativeConfig?.browser_native_binding;
  if (!pkg || !subject || !resources || !executionBinding || !browserExtension || !nativeHost || !configBinding) {
    fail('package_invalid', 'execution_manifest.json/native-config.json is missing exact package identity fields');
  }

  const packageDigest = requireSha256(pkg.digest, 'package.digest');
  const sourceCommit = requireSha1(subject.commit, 'subject.commit');
  const sourceTree = requireSha1(subject.tree, 'subject.tree');
  requireSha256(executionBinding.binding_digest, 'package.browser_native_binding.binding_digest');

  const mismatches = [];
  const check = (name, left, right) => { if (left !== right) mismatches.push({name, left, right}); };
  check('native_config.package_digest', nativeConfig.package_digest, packageDigest);
  check('extension.id', browserExtension.extension_id, identity.extensionId);
  check('binding.digest.execution', executionBinding.binding_digest, identity.bindingDigest);
  check('binding.digest.config', configBinding.binding_digest, identity.bindingDigest);
  check('protocol.execution', pkg.protocol_generation, identity.protocol);
  check('protocol.config', nativeConfig.protocol_generation, identity.protocol);
  check('native_host.execution', nativeHost.name, identity.host);
  check('native_host.config', nativeConfig.native_host_name, identity.host);
  check('source.commit.binding', executionBinding.source_commit, sourceCommit);
  check('source.tree.binding', executionBinding.source_tree, sourceTree);
  check('source.commit.config_binding', configBinding.source_commit, sourceCommit);
  check('source.tree.config_binding', configBinding.source_tree, sourceTree);
  check('source.commit.config', nativeConfig.source_commit, sourceCommit);
  check('production.execution', resources.production_activation, false);
  check('production.binding', executionBinding.production_activation, false);
  check('production.config', nativeConfig.production_activation, false);
  if (!sameResolvedPath(browserExtension.manifest, manifestPath)) mismatches.push({name: 'browser_extension.manifest_path', left: browserExtension.manifest, right: manifestPath});
  if (!sameResolvedPath(executionBinding.package_root, root)) mismatches.push({name: 'binding.package_root', left: executionBinding.package_root, right: root});
  if (!sameResolvedPath(nativeConfig.package_root, root)) mismatches.push({name: 'native_config.package_root', left: nativeConfig.package_root, right: root});

  if (args.expectedSourceCommit && args.expectedSourceCommit !== sourceCommit) {
    mismatches.push({name: 'expected_source_commit', left: sourceCommit, right: args.expectedSourceCommit});
  }
  if (args.expectedSourceTree && args.expectedSourceTree !== sourceTree) {
    mismatches.push({name: 'expected_source_tree', left: sourceTree, right: args.expectedSourceTree});
  }
  if (mismatches.length) fail('package_identity_mismatch', 'Generated package documents do not bind one exact execution subject', {mismatches});

  return {
    root,
    extensionPath,
    manifest,
    manifestPath,
    background,
    backgroundDigest: sha256Text(background),
    identity,
    execution,
    nativeConfig,
    packageDigest,
    sourceCommit,
    sourceTree,
  };
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

  const loadedExtension = await worker.evaluate(async () => {
    const response = await fetch(chrome.runtime.getURL('background.js'), {cache: 'no-store'});
    if (!response.ok) throw new Error(`background.js read failed: ${response.status}`);
    return {background: await response.text(), manifest: chrome.runtime.getManifest()};
  });
  const loadedBackgroundDigest = sha256Text(loadedExtension.background);
  if (loadedBackgroundDigest !== pkg.backgroundDigest) {
    fail('extension_package_mismatch', 'Active MV3 service worker bytes differ from the exact package-root background.js', {loadedBackgroundDigest, expected: pkg.backgroundDigest});
  }
  if (loadedExtension.manifest?.version !== pkg.manifest.version || loadedExtension.manifest?.manifest_version !== pkg.manifest.manifest_version) {
    fail('extension_package_mismatch', 'Active extension manifest differs from the exact package-root manifest', {loaded: loadedExtension.manifest, expected: pkg.manifest});
  }

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
  const nativeMismatch = [];
  const nativeCheck = (name, left, right) => { if (left !== right) nativeMismatch.push({name, left, right}); };
  nativeCheck('extension_id', native.browser_extension_id, EXPECTED_EXTENSION_ID);
  nativeCheck('binding_digest', native.browser_native_binding_digest, pkg.identity.bindingDigest);
  nativeCheck('protocol_generation', native.protocol_generation, pkg.identity.protocol);
  nativeCheck('package_digest', native.package_digest, pkg.packageDigest);
  nativeCheck('native_code_digest', native.native_code_digest, pkg.execution.package.native_code_digest);
  nativeCheck('interpreter_identity_digest', native.interpreter_identity_digest, pkg.execution.package.interpreter_identity?.identity_digest);
  nativeCheck('native_host_name', native.native_host_name, pkg.identity.host);
  if (nativeMismatch.length) fail('native_identity_mismatch', 'Native Host READY response differs from the exact generated package', {mismatches: nativeMismatch});
  if (native.production_activation !== false || native.production_runtime !== 'OFF' || native.production_writer !== 'OFF') {
    fail('production_guard_mismatch', 'BDB Browser Runner requires production OFF/OFF/OFF', {native});
  }
  return {extension, workerUrl: expectedWorkerUrl, loadedBackgroundDigest, native};
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

function validateEngineeringPrompt(prompt, pkg) {
  if (!prompt.startsWith(`${pkg.identity.engineeringPrefix}\n`)) {
    fail('prompt_prefix_mismatch', 'Prompt does not start with the exact engineering prefix required by this package', {expected_prefix: pkg.identity.engineeringPrefix});
  }
}

async function sendExactPrompt(page, prompt, timeoutMs) {
  const composer = await page.waitForSelector(COMPOSER_SELECTOR, {visible: true, timeout: timeoutMs});
  const existingText = normalizePrompt(await composer.evaluate(node => node.innerText || node.textContent || ''));
  if (existingText) fail('composer_not_empty', 'ChatGPT composer already contains text; Runner will not overwrite it', {existingText});
  await composer.click();
  await page.keyboard.insertText(prompt);
  const echoed = normalizePrompt(await composer.evaluate(node => node.innerText || node.textContent || ''));
  if (echoed !== prompt) {
    fail('composer_echo_mismatch', 'ChatGPT composer does not contain the exact prompt after insertion; Runner will not send it', {
      expected_digest: sha256Text(prompt),
      echoed_digest: sha256Text(echoed),
      expected_length: prompt.length,
      echoed_length: echoed.length,
    });
  }
  await page.waitForFunction(selectors => selectors.some(selector => {
    const button = document.querySelector(selector);
    return button instanceof HTMLButtonElement && !button.disabled && button.offsetParent !== null;
  }), {timeout: timeoutMs}, SEND_SELECTORS);
  const clicked = await page.evaluate(selectors => {
    for (const selector of selectors) {
      const button = document.querySelector(selector);
      if (button instanceof HTMLButtonElement && !button.disabled && button.offsetParent !== null) {
        button.click();
        return selector;
      }
    }
    return null;
  }, SEND_SELECTORS);
  if (!clicked) fail('send_control_missing', 'Exact ChatGPT send control became unavailable before submission');
}

async function waitForCanonicalConversation(page, timeoutMs) {
  await page.waitForFunction(() => /^\/c\/[A-Za-z0-9_-]{8,128}(?:\/|$)/.test(location.pathname), {timeout: timeoutMs});
  return page.url();
}

async function panelSnapshot(page) {
  return await page.evaluate(({panelSelector, outputSelector}) => {
    const panel = document.querySelector(panelSelector);
    if (!panel) return null;
    const output = panel.querySelector(outputSelector);
    return {
      text: (panel.innerText || panel.textContent || '').trim(),
      output: (output?.innerText || output?.textContent || '').trim(),
      buttons: [...panel.querySelectorAll('button')].map(button => (button.textContent || '').trim()),
    };
  }, {panelSelector: PANEL_SELECTOR, outputSelector: PANEL_OUTPUT_SELECTOR});
}

function blockedPanel(output) {
  return /Validation\s+(?:ARTIFACT_REJECTED|RECOVERY_REJECTED|STALE_ARTIFACT|FAIL|FAILED|ERROR)/i.test(output) || /Candidate seal failed|invalid_payload|reconciliation_required/i.test(output);
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

function parseFinalResponse(output) {
  try {
    const value = JSON.parse(output);
    if (value && typeof value === 'object' && !Array.isArray(value)) return value;
  } catch (_) {}
  return null;
}

async function waitForEngineeringResult(page, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  let sealClicked = false;
  let last = null;
  while (Date.now() < deadline) {
    const snapshot = await panelSnapshot(page);
    if (snapshot) {
      last = snapshot;
      if (blockedPanel(snapshot.output)) {
        return {status: 'BLOCKED', panel: snapshot, sealClicked, repair_required: true, repair_feedback: snapshot.output};
      }
      if (!sealClicked && snapshot.buttons.includes(SEAL_LABEL)) {
        await clickSeal(page);
        sealClicked = true;
      } else if (sealClicked) {
        const finalResponse = parseFinalResponse(snapshot.output);
        const finalStatus = finalResponse?.status;
        if (finalStatus === 'ENGINEERING_SEALED' || finalStatus === 'SEALED') {
          return {status: 'SEALED', panel: snapshot, finalResponse, sealClicked};
        }
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
  if (!Number.isFinite(args.readinessTimeoutSeconds) || args.readinessTimeoutSeconds <= 0 || args.readinessTimeoutSeconds > 300) fail('configuration_invalid', 'readiness-timeout-seconds must be between 1 and 300');
  const turnTimeoutMs = Math.floor(args.timeoutSeconds * 1000);
  const readinessTimeoutMs = Math.floor(args.readinessTimeoutSeconds * 1000);
  const pkg = loadPackage(args.packageRoot, args);
  const puppeteer = loadPuppeteer(args.puppeteerDir);
  const {browser, launched} = await launchOrConnect(puppeteer, args, pkg.extensionPath);

  let exitCode = 0;
  try {
    const readiness = await verifyExtension(browser, pkg, readinessTimeoutMs);
    const page = await getChatPage(browser);
    await openChat(page, args.conversationUrl, readinessTimeoutMs);

    const baseReport = {
      schema: 'bdb-browser-runner-v1.1',
      mode: args.mode,
      browser: {launched, cdp_port: args.connectUrl ? null : args.cdpPort, connected_url: args.connectUrl},
      package: {
        root: pkg.root,
        package_digest: pkg.packageDigest,
        source_commit: pkg.sourceCommit,
        source_tree: pkg.sourceTree,
        extension_id: EXPECTED_EXTENSION_ID,
        extension_version: pkg.manifest.version,
        extension_background_digest: readiness.loadedBackgroundDigest,
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
    const prompt = normalizePrompt(fs.readFileSync(promptFile, 'utf8'));
    if (!prompt) fail('prompt_invalid', 'prompt-file is empty');
    validateEngineeringPrompt(prompt, pkg);
    await sendExactPrompt(page, prompt, readinessTimeoutMs);
    const conversationUrl = await waitForCanonicalConversation(page, readinessTimeoutMs);
    const result = await waitForEngineeringResult(page, turnTimeoutMs);
    if (result.status === 'BLOCKED') exitCode = 2;
    report({
      ...baseReport,
      status: result.status,
      conversation_url: conversationUrl,
      prompt_digest: sha256Text(prompt),
      engineering: result,
      next_action: result.status === 'BLOCKED' ? 'SUPPLY_EXACT_PREFIXED_REPAIR_PROMPT_TO_SAME_CONVERSATION' : 'NONE',
    });
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
    report({schema: 'bdb-browser-runner-v1.1', status: 'ERROR', error: {code: typed.code, message: typed.message, details: typed.details}});
    process.exitCode = 3;
  },
);

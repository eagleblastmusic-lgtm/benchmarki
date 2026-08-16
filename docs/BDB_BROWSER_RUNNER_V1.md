# BDB Browser Runner v1.1

## Purpose

`BDB Browser Runner v1.1` is a deliberately mechanical browser operator for the existing BDB vNext Browser-first engineering path.

It does **not** own engineering semantics, Task authority, Candidate authority, Evidence, or Publication. It never authors repository code and never constructs `BDB_EDIT_V1` locally.

Its responsibilities are limited to:

1. launch or attach to a controlled Chrome for Testing instance;
2. load and verify the exact generated BDB browser-extension package;
3. verify the MV3 service worker and the real extension -> Chrome Native Messaging -> Native Host `READY` handshake;
4. verify the package execution manifest, source commit/tree, Browser/Native binding and Native package digest agree;
5. open an authenticated normal `chatgpt.com` session;
6. submit an **exact prompt supplied by the caller** only after an exact composer echo check;
7. observe the BDB extension panel;
8. click the exact `Seal engineering Candidate` control only when BDB exposes it;
9. return a typed JSON result or a bounded blocker.

The Runner does not synthesize repair prompts. When BDB returns a typed blocker, v1.1 returns the exact panel feedback plus the canonical conversation URL. A caller may then provide an exact prefixed repair prompt in a second Runner invocation against the **same canonical conversation**.

## Authority boundary

The intended flow remains:

```text
prepared exact prompt
  -> BDB Browser Runner
  -> normal ChatGPT web
  -> GPT-authored BDB_EDIT_V1
  -> BDB extension
  -> Chrome Native Messaging
  -> Native Host
  -> BDB Core / EditorPort / Candidate
  -> validation
  -> seal / Evidence / Evaluation / Disposition / Publication
```

The Runner must not replace any arrow after the model response with an in-process shortcut.

## v1.1 hardening

Compared with the initial v1 branch commit, v1.1 adds four bounded protections learned from the second real Giclée Browser-first pilot:

- **exact package identity:** `execution_manifest.json`, `native-config.json`, generated extension constants, active service-worker bytes and Native `READY` identity must all agree; optional expected BDB source commit/tree values can be supplied by the caller;
- **exact composer echo:** after inserting the prompt, the Runner reads the ChatGPT composer back and compares the normalized text before it is allowed to click Send;
- **structured final result:** the Runner reads only `.n6-output`, not the whole floating panel, so the final Native JSON can be parsed without the panel heading/buttons contaminating it;
- **bounded readiness:** Browser/extension/login readiness has its own short timeout (45 seconds by default), while the GPT/BDB engineering turn keeps the longer 900-second default.

The second real pilot also proved that repair turns are necessary in practice. v1.1 therefore makes the transport contract explicit: a blocked result returns `repair_feedback`, `conversation_url`, and `next_action=SUPPLY_EXACT_PREFIXED_REPAIR_PROMPT_TO_SAME_CONVERSATION`. The Runner still does not invent or rewrite repair semantics.

## Requirements

- Node.js;
- Puppeteer with extension support (`enableExtensions`, `browser.extensions()`); Puppeteer 25.x is known to provide these APIs;
- Chrome for Testing compatible with that Puppeteer installation;
- a generated BDB execution package containing `browser-extension/`, `execution_manifest.json`, and `native-config.json`;
- the matching Native Host package registered for the stable BDB extension ID;
- a persistent Chrome profile that can be manually authenticated to ChatGPT.

The Runner deliberately does not install Puppeteer or Chrome. Their exact local paths are caller configuration.

## Configuration

The PowerShell wrapper accepts paths directly or these environment variables:

```powershell
$env:BDB_PUPPETEER_DIR   = 'C:\path\to\node_modules\puppeteer'
$env:BDB_CFT_EXECUTABLE  = 'C:\path\to\chrome-for-testing\chrome.exe'
$env:BDB_BROWSER_PROFILE = 'C:\path\to\persistent-bdb-profile'
```

`PackageRoot` must point to the exact generated package root whose `browser-extension` directory and Native package are intended for this run.

For a run that must be pinned to one exact BDB source subject, also pass:

```powershell
-ExpectedSourceCommit '<40-char-commit>' `
-ExpectedSourceTree '<40-char-tree>'
```

The Runner fails closed if these values differ from the package execution manifest and Browser/Native binding.

## Readiness verification

```powershell
.\scripts\Invoke-BDBBrowserRunner.ps1 `
  -Mode verify `
  -PackageRoot 'C:\path\to\bdb-package' `
  -ExpectedSourceCommit '<commit>' `
  -ExpectedSourceTree '<tree>' `
  -KeepOpen
```

The Runner verifies:

- stable BDB extension ID `mopnolkjddkmgojfjkenjobehhmmklll`;
- `execution_manifest.json` and `native-config.json` bind the same package/source/binding;
- optional caller-supplied source commit/tree match the package;
- active MV3 `background.js` bytes hash exactly to the generated package file;
- exact generated MV3 service worker target;
- real `chrome.runtime.connectNative(...)` status request;
- Native package digest, code digest, interpreter digest, binding/protocol/extension identity;
- production remains `OFF/OFF/OFF`;
- ChatGPT composer is available and the session does not present a login/sign-in control.

If the profile needs a one-time manual login, use `-KeepOpen`, authenticate in the visible CFT window, then rerun verification by connecting to its CDP endpoint.

## Execute one real engineering turn

The prompt is a file produced outside the Runner. The Runner normalizes CRLF to LF and trims only the outer boundary. It then requires the prompt to start with the exact engineering prefix embedded in the generated package.

Before clicking Send it performs an **exact composer echo**. If ChatGPT's composer does not contain the same normalized prompt text, the Runner exits fail-closed with `composer_echo_mismatch` and does not submit anything.

```powershell
.\scripts\Invoke-BDBBrowserRunner.ps1 `
  -Mode run `
  -PackageRoot 'C:\path\to\bdb-package' `
  -PromptFile 'C:\path\to\prepared-engineering-prompt.txt' `
  -ExpectedSourceCommit '<commit>' `
  -ExpectedSourceTree '<tree>' `
  -KeepOpen
```

## Mechanical same-chat repair transport

A BDB/model validation failure is returned with exit code `2`, exact `repair_feedback`, the canonical `conversation_url`, and:

```text
next_action = SUPPLY_EXACT_PREFIXED_REPAIR_PROMPT_TO_SAME_CONVERSATION
```

A higher layer or operator prepares the exact repair prompt. The Runner then transports that prompt without semantic rewriting:

```powershell
.\scripts\Invoke-BDBBrowserRunner.ps1 `
  -Mode run `
  -PackageRoot 'C:\path\to\bdb-package' `
  -PromptFile 'C:\path\to\exact-repair-prompt.txt' `
  -ConversationUrl 'https://chatgpt.com/c/<conversation-id>' `
  -ExpectedSourceCommit '<commit>' `
  -ExpectedSourceTree '<tree>' `
  -KeepOpen
```

The repair prompt must carry the same exact engineering prefix required by the package. The Runner does not derive a new fence, alter a model artifact, fabricate `BDB_EDIT_V1`, or infer how the code should be repaired.

## Connect to an already-running controlled CFT instance

```powershell
.\scripts\Invoke-BDBBrowserRunner.ps1 `
  -Mode verify `
  -PackageRoot 'C:\path\to\bdb-package' `
  -PuppeteerDir 'C:\path\to\node_modules\puppeteer' `
  -ConnectUrl 'http://127.0.0.1:9230' `
  -ExpectedSourceCommit '<commit>' `
  -ExpectedSourceTree '<tree>'
```

In connect mode the Runner never launches or closes the existing browser; it disconnects when finished.

## Timeouts

Two independent bounds are intentional:

- `ReadinessTimeoutSeconds` / `--readiness-timeout-seconds`: default `45`, maximum `300`; used for extension worker, ChatGPT composer, login readiness, prompt-send readiness and canonical conversation routing;
- `TimeoutSeconds` / `--timeout-seconds`: default `900`, maximum `7200`; used for the real GPT/BDB engineering result.

A missing service worker or login therefore fails quickly instead of consuming the full model-turn timeout.

## Result and exit codes

The Runner writes one JSON report to stdout using schema `bdb-browser-runner-v1.1`.

- exit `0`: readiness verified or Candidate was sealed successfully;
- exit `2`: BDB reported a typed engineering blocker/rejection; exact panel output remains available as `repair_feedback`;
- exit `3`: Runner/package/browser configuration or mechanical execution error.

A successful seal includes the parsed final Native response from `.n6-output`, which preserves the structured Candidate/Evidence/Evaluation/Disposition/Publication result instead of attempting to parse the whole panel text.

## Intentional v1.1 limits

This version intentionally does not provide:

- autonomous semantic repair-prompt generation;
- multi-worker scheduling;
- cloud execution;
- browser/provider abstraction;
- direct repository mutation;
- direct calls to `N6RehearsalService.handle()` or `engineering_artifact`;
- fallback artifact construction;
- automatic model selection or reasoning-setting changes;
- CAPTCHA/rate-limit/protection bypasses.

Those omissions preserve the existing BDB authority split while replacing the slow ad-hoc browser harness with one repeatable mechanical Runner.

# BDB Browser Runner v1

## Purpose

`BDB Browser Runner v1` is a deliberately mechanical browser operator for the existing BDB vNext Browser-first engineering path.

It does **not** own engineering semantics, Task authority, Candidate authority, Evidence, or Publication. It never authors repository code and never constructs `BDB_EDIT_V1` locally.

Its only responsibilities are:

1. launch or attach to a controlled Chrome for Testing instance;
2. load/verify the exact generated BDB browser-extension package;
3. verify the MV3 service worker and the real extension -> Chrome Native Messaging -> Native Host `READY` handshake;
4. open an authenticated normal `chatgpt.com` session;
5. submit an **exact prompt supplied by the caller**;
6. observe the BDB extension panel;
7. click the exact `Seal engineering Candidate` control only when BDB exposes it;
8. return a typed JSON result or a bounded blocker.

The Runner does not synthesize repair prompts. A repair iteration is a new Runner invocation against the same canonical ChatGPT `/c/<id>` conversation with a repair prompt supplied by BDB/the operator.

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

## Requirements

- Node.js
- Puppeteer with extension support (`enableExtensions`, `browser.extensions()`); Puppeteer 25.x is known to provide these APIs.
- Chrome for Testing compatible with the Puppeteer installation.
- a generated BDB execution package containing `browser-extension/` and the currently registered matching Native Host package;
- a persistent Chrome profile that can be manually authenticated to ChatGPT.

The Runner deliberately does not install Puppeteer or Chrome. Their exact local paths are caller configuration.

## Configuration

The PowerShell wrapper accepts paths directly or these environment variables:

```powershell
$env:BDB_PUPPETEER_DIR   = 'C:\path\to\node_modules\puppeteer'
$env:BDB_CFT_EXECUTABLE  = 'C:\path\to\chrome-for-testing\chrome.exe'
$env:BDB_BROWSER_PROFILE = 'C:\path\to\persistent-bdb-profile'
```

`PackageRoot` must point to the exact generated package root whose `browser-extension` directory is to be loaded.

## Readiness verification

```powershell
.\scripts\Invoke-BDBBrowserRunner.ps1 `
  -Mode verify `
  -PackageRoot 'C:\path\to\bdb-package' `
  -KeepOpen
```

The Runner verifies:

- stable BDB extension ID `mopnolkjddkmgojfjkenjobehhmmklll`;
- exact generated MV3 service worker target;
- real `chrome.runtime.connectNative(...)` status request;
- Native response binding/protocol/extension identity;
- production remains `OFF/OFF/OFF`;
- ChatGPT composer is available and the session does not present a login/sign-in control.

If the profile needs a one-time manual login, use `-KeepOpen`, authenticate in the visible CFT window, then rerun verification by connecting to its CDP endpoint.

## Execute one real engineering turn

The prompt is a file produced outside the Runner. The Runner sends it byte-for-text (after CRLF -> LF normalization and outer trim) and does not modify its engineering content.

```powershell
.\scripts\Invoke-BDBBrowserRunner.ps1 `
  -Mode run `
  -PackageRoot 'C:\path\to\bdb-package' `
  -PromptFile 'C:\path\to\prepared-engineering-prompt.txt' `
  -KeepOpen
```

For a repair turn in the same normal ChatGPT conversation:

```powershell
.\scripts\Invoke-BDBBrowserRunner.ps1 `
  -Mode run `
  -PackageRoot 'C:\path\to\bdb-package' `
  -PromptFile 'C:\path\to\exact-repair-prompt.txt' `
  -ConversationUrl 'https://chatgpt.com/c/<conversation-id>' `
  -KeepOpen
```

The Runner will never derive the repair prompt from model output itself.

## Connect to an already-running controlled CFT instance

```powershell
.\scripts\Invoke-BDBBrowserRunner.ps1 `
  -Mode verify `
  -PackageRoot 'C:\path\to\bdb-package' `
  -PuppeteerDir 'C:\path\to\node_modules\puppeteer' `
  -ConnectUrl 'http://127.0.0.1:9230'
```

In connect mode the Runner does not launch or close the existing browser; it disconnects when finished.

## Result and exit codes

The Runner writes one JSON report to stdout using schema `bdb-browser-runner-v1`.

- exit `0`: readiness verified or Candidate was sealed successfully;
- exit `2`: BDB reported a typed engineering blocker/rejection in its panel;
- exit `3`: Runner/package/browser configuration or mechanical execution error.

A model or BDB validation failure is not locally repaired. The exact BDB panel feedback remains in the JSON report so a higher layer can prepare the next exact prompt.

## Intentional v1 limits

This first version is intentionally small. It does not provide:

- autonomous semantic repair-prompt generation;
- multi-worker scheduling;
- cloud execution;
- browser/provider abstraction;
- direct repository mutation;
- direct calls to `N6RehearsalService.handle()` or `engineering_artifact`;
- fallback artifact construction.

Those omissions preserve the existing BDB authority split while replacing the slow ad-hoc browser harness with one repeatable mechanical runner.

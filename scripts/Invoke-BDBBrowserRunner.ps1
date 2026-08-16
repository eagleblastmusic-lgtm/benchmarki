[CmdletBinding()]
param(
    [ValidateSet('verify', 'run')]
    [string]$Mode = 'run',

    [Parameter(Mandatory = $true)]
    [string]$PackageRoot,

    [string]$ProfileDir = $env:BDB_BROWSER_PROFILE,
    [string]$PromptFile,
    [string]$PuppeteerDir = $env:BDB_PUPPETEER_DIR,
    [string]$ChromeExecutable = $env:BDB_CFT_EXECUTABLE,
    [string]$ConnectUrl,
    [string]$ConversationUrl,
    [string]$ExpectedSourceCommit,
    [string]$ExpectedSourceTree,
    [int]$CdpPort = 9230,
    [int]$ReadinessTimeoutSeconds = 45,
    [int]$TimeoutSeconds = 900,
    [switch]$KeepOpen
)

$ErrorActionPreference = 'Stop'
$runner = Join-Path $PSScriptRoot 'bdb_browser_runner.mjs'
if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) {
    throw "BDB Browser Runner script is missing: $runner"
}
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    throw 'Node.js is required to run BDB Browser Runner v1.1.'
}

$arguments = @(
    $runner,
    '--mode', $Mode,
    '--package-root', $PackageRoot,
    '--puppeteer-dir', $PuppeteerDir,
    '--readiness-timeout-seconds', [string]$ReadinessTimeoutSeconds,
    '--timeout-seconds', [string]$TimeoutSeconds
)

if (-not [string]::IsNullOrWhiteSpace($ConnectUrl)) {
    $arguments += @('--connect-url', $ConnectUrl)
} else {
    $arguments += @(
        '--profile-dir', $ProfileDir,
        '--chrome-executable', $ChromeExecutable,
        '--cdp-port', [string]$CdpPort
    )
}

if (-not [string]::IsNullOrWhiteSpace($PromptFile)) {
    $arguments += @('--prompt-file', $PromptFile)
}
if (-not [string]::IsNullOrWhiteSpace($ConversationUrl)) {
    $arguments += @('--conversation-url', $ConversationUrl)
}
if (-not [string]::IsNullOrWhiteSpace($ExpectedSourceCommit)) {
    $arguments += @('--expected-source-commit', $ExpectedSourceCommit)
}
if (-not [string]::IsNullOrWhiteSpace($ExpectedSourceTree)) {
    $arguments += @('--expected-source-tree', $ExpectedSourceTree)
}
if ($KeepOpen) {
    $arguments += '--keep-open'
}

& node @arguments
exit $LASTEXITCODE

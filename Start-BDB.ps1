param(
    [string]$RuntimeRoot = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
Set-Location -LiteralPath $RepoRoot

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    $Python = (Get-Command python -ErrorAction SilentlyContinue).Source
}
if (-not $Python) {
    throw "Python environment not found. Activate the project environment and retry."
}

$Arguments = @("-m", "bdb_gui.app")
if ($RuntimeRoot) {
    $Arguments += @("--runtime-root", $RuntimeRoot)
}
& $Python @Arguments
if ($LASTEXITCODE -ne 0) {
    throw "BDB Control Center exited with code $LASTEXITCODE."
}

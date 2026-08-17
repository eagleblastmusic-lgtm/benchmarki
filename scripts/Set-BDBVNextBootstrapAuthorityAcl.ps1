[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)]
    [string]$Root,

    [switch]$Apply,

    # CI may exercise the exact ACL mechanics below RUNNER_TEMP. Production
    # callers must omit this switch and use the canonical ProgramData root.
    [switch]$AllowNonProgramDataForTest
)

$ErrorActionPreference = 'Stop'

if (-not $IsWindows) {
    throw 'M11a Windows Bootstrap ACL policy requires Windows.'
}

$fullRoot = [System.IO.Path]::GetFullPath($Root)
$programData = [System.IO.Path]::GetFullPath($env:ProgramData)
$expectedRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $programData 'BartoszDevBridge-Next\bootstrap')
)

if (-not $AllowNonProgramDataForTest -and $fullRoot -ne $expectedRoot) {
    throw "Bootstrap authority root must be exactly $expectedRoot"
}

if (-not $Apply) {
    if (-not (Test-Path -LiteralPath $fullRoot -PathType Container)) {
        throw 'Bootstrap authority root does not exist.'
    }
    Get-Acl -LiteralPath $fullRoot
    return
}

if ($PSCmdlet.ShouldProcess($fullRoot, 'Create/harden BDB Next Bootstrap authority ACL')) {
    New-Item -ItemType Directory -Force -Path $fullRoot | Out-Null

    # SIDs avoid localized account names. The owner and write-capable ACLs are
    # restricted to the external TCB; ordinary runtime/candidate processes get
    # only read/execute through BUILTIN\Users.
    & icacls.exe $fullRoot /inheritance:r | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Failed to disable inherited Bootstrap ACLs.' }

    & icacls.exe $fullRoot /setowner '*S-1-5-32-544' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Failed to set Bootstrap owner to Administrators.' }

    & icacls.exe $fullRoot /grant:r `
        '*S-1-5-18:(OI)(CI)F' `
        '*S-1-5-32-544:(OI)(CI)F' `
        '*S-1-5-32-545:(OI)(CI)RX' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Failed to apply Bootstrap TCB ACLs.' }
}

Get-Acl -LiteralPath $fullRoot

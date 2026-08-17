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

    # Do not remove inherited rights before the external TCB has an explicit
    # foothold. On Windows a newly-created directory may rely entirely on
    # inherited rights for the current elevated caller; stripping inheritance
    # first can lock that caller out before owner/grants are applied.
    #
    # SIDs avoid localized account names. The final DACL still contains only
    # SYSTEM/Admin write authority and Users read/execute.
    & icacls.exe $fullRoot /grant:r `
        '*S-1-5-18:(OI)(CI)F' `
        '*S-1-5-32-544:(OI)(CI)F' `
        '*S-1-5-32-545:(OI)(CI)RX' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Failed to establish explicit Bootstrap TCB ACLs.' }

    & icacls.exe $fullRoot /setowner '*S-1-5-32-544' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Failed to set Bootstrap owner to Administrators.' }

    & icacls.exe $fullRoot /inheritance:r | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Failed to disable inherited Bootstrap ACLs.' }

    # Re-apply the exact final ACL after inherited ACE removal so the published
    # authority boundary is deterministic and idempotent.
    & icacls.exe $fullRoot /grant:r `
        '*S-1-5-18:(OI)(CI)F' `
        '*S-1-5-32-544:(OI)(CI)F' `
        '*S-1-5-32-545:(OI)(CI)RX' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Failed to finalize Bootstrap TCB ACLs.' }
}

Get-Acl -LiteralPath $fullRoot

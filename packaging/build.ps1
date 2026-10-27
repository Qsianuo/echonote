# Builds the portable Windows bundle, tests it, and zips it.
#
#   powershell -ExecutionPolicy Bypass -File packaging/build.ps1
#
# CI runs exactly this script, so a local build and a release build cannot
# drift apart. Kept ASCII-only on purpose: Windows PowerShell 5.1 reads .ps1
# files as ANSI unless they carry a BOM, and mojibake in build tooling is
# hard to spot.

param(
    [string]$Version = "0.12.1",
    [switch]$SkipTest,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

# Native tools (pyinstaller, python) report progress on stderr. With
# $ErrorActionPreference = "Stop", Windows PowerShell promotes that to a
# terminating NativeCommandError, so a perfectly good build looks like a
# failure. Relax it for the call and decide on $LASTEXITCODE instead.
function Invoke-Native([string]$What, [scriptblock]$Body) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $code = 0
    try {
        & $Body 2>&1 | Write-Host
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($code -ne 0) { throw "$What failed (exit $code)" }
}

Write-Host "==> Building EchoNote $Version" -ForegroundColor Cyan

# Single source of truth for the version: app/config.py reads this instead of
# carrying its own literal, so the exe, the zip name and the UI cannot drift.
$env:ECHONOTE_APP_VERSION = $Version

if (Test-Path build) { Remove-Item build -Recurse -Force }
if (Test-Path dist) { Remove-Item dist -Recurse -Force }

Invoke-Native "pyinstaller" {
    & $Python -m PyInstaller packaging/echonote.spec --noconfirm --distpath dist --workpath build --log-level WARN
}
$out = Join-Path $root "dist\EchoNote"
Copy-Item README.md, LICENSE -Destination $out
Copy-Item packaging\extras\*.bat -Destination $out

if (-not $SkipTest) {
    Write-Host "==> Smoke testing the frozen build" -ForegroundColor Cyan
    Invoke-Native "smoke test" {
        & $Python packaging/smoke_test.py (Join-Path $out "EchoNote.exe")
    }
}

Write-Host "==> Packaging zip" -ForegroundColor Cyan
$name = "EchoNote-v$Version-windows-x64"
if (Test-Path "$name.zip") { Remove-Item "$name.zip" -Force }
Compress-Archive -Path $out -DestinationPath "$name.zip" -CompressionLevel Optimal

$size = [math]::Round((Get-Item "$name.zip").Length / 1MB, 1)
Write-Host ""
Write-Host "OK  $name.zip  ($size MB)" -ForegroundColor Green
Write-Host "    bundle: $out"

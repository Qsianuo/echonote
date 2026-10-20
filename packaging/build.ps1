# Builds the portable Windows bundle, tests it, and zips it.
#
#   powershell -ExecutionPolicy Bypass -File packaging/build.ps1
#
# CI runs exactly this script, so a local build and a release build cannot
# drift apart. Kept ASCII-only on purpose: Windows PowerShell 5.1 reads .ps1
# files as ANSI unless they carry a BOM, and mojibake in build tooling is
# hard to spot.

param(
    [string]$Version = "0.9.0",
    [switch]$SkipTest,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

Write-Host "==> Building EchoNote $Version" -ForegroundColor Cyan

if (Test-Path build) { Remove-Item build -Recurse -Force }
if (Test-Path dist) { Remove-Item dist -Recurse -Force }

& $Python -m PyInstaller packaging/echonote.spec --noconfirm --distpath dist --workpath build --log-level WARN *>&1 | Write-Host
if ($LASTEXITCODE -ne 0) { throw "pyinstaller failed (exit $LASTEXITCODE)" }

$out = Join-Path $root "dist\EchoNote"
Copy-Item README.md, LICENSE -Destination $out
Copy-Item packaging\extras\start-mirror.bat -Destination $out

if (-not $SkipTest) {
    Write-Host "==> Smoke testing the frozen build" -ForegroundColor Cyan
    & $Python packaging/smoke_test.py (Join-Path $out "EchoNote.exe") *>&1 | Write-Host
    if ($LASTEXITCODE -ne 0) { throw "smoke test failed (exit $LASTEXITCODE)" }
}

Write-Host "==> Packaging zip" -ForegroundColor Cyan
$name = "EchoNote-v$Version-windows-x64"
if (Test-Path "$name.zip") { Remove-Item "$name.zip" -Force }
Compress-Archive -Path $out -DestinationPath "$name.zip" -CompressionLevel Optimal

$size = [math]::Round((Get-Item "$name.zip").Length / 1MB, 1)
Write-Host ""
Write-Host "OK  $name.zip  ($size MB)" -ForegroundColor Green
Write-Host "    bundle: $out"

# SPDX-License-Identifier: GPL-3.0-or-later
# Builds Login Relay for Windows: runs the Core tests, publishes one self-contained LoginRelay.exe into
# windows\publish, puts the phone app next to it, and zips the lot.
#
#   powershell -ExecutionPolicy Bypass -File windows\build.ps1
#
# Environment (all optional):
#   LOGINRELAY_APK   the phone app to bundle (default ..\android\build\login-relay.apk; skipped with a warning if missing)
#   PHONE_VERSION    the phone app's version (default: VERSION_NAME= in ..\android\build.sh)
#   SKIP_TESTS=1     publish without running the tests first
#
# Then, to run the end-to-end checks against the result:
#   python tests\e2e\run_e2e.py -- windows\publish\LoginRelay.exe
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$Version = '0.4.0'
$Publish = Join-Path $PSScriptRoot 'publish'
$Zip = Join-Path $Publish "Login-Relay-Windows-x64-$Version.zip"

if ($env:SKIP_TESTS -ne '1') {
    Write-Host '== Core tests =='
    dotnet test (Join-Path $PSScriptRoot 'tests\LoginRelay.Core.Tests\LoginRelay.Core.Tests.csproj') -c Release
    if ($LASTEXITCODE -ne 0) { throw "dotnet test failed (exit code $LASTEXITCODE)" }
}

Write-Host '== Publish =='
if (Test-Path -LiteralPath $Publish) { Remove-Item -LiteralPath $Publish -Recurse -Force }
dotnet publish (Join-Path $PSScriptRoot 'src\LoginRelay.App\LoginRelay.App.csproj') -c Release -o $Publish
if ($LASTEXITCODE -ne 0) { throw "dotnet publish failed (exit code $LASTEXITCODE)" }

$Exe = Join-Path $Publish 'LoginRelay.exe'
if (-not (Test-Path -LiteralPath $Exe)) { throw "publish did not produce $Exe" }

# The phone app, so the tray menu can install it.
$Apk = if ($env:LOGINRELAY_APK) { $env:LOGINRELAY_APK } else { Join-Path $PSScriptRoot '..\android\build\login-relay.apk' }
$Files = @($Exe)
if (Test-Path -LiteralPath $Apk) {
    $ApkTarget = Join-Path $Publish 'login-relay.apk'
    Copy-Item -LiteralPath $Apk -Destination $ApkTarget -Force
    $Files += $ApkTarget

    $PhoneVersion = $env:PHONE_VERSION
    if (-not $PhoneVersion) {
        $BuildSh = Join-Path $PSScriptRoot '..\android\build.sh'
        if (Test-Path -LiteralPath $BuildSh) {
            $Match = Select-String -LiteralPath $BuildSh -Pattern '^VERSION_NAME=(.+)$' | Select-Object -First 1
            if ($Match) { $PhoneVersion = $Match.Matches[0].Groups[1].Value.Trim().Trim('"') }
        }
    }
    if ($PhoneVersion) {
        $VersionFile = Join-Path $Publish 'phone-app-version.txt'
        Set-Content -LiteralPath $VersionFile -Value $PhoneVersion -NoNewline -Encoding ascii
        $Files += $VersionFile
    } else {
        Write-Warning 'Could not work out the phone app version; the app will not offer updates.'
    }
} elseif ($env:GITHUB_ACTIONS -eq 'true') {
    # A release without the phone app can't install it; don't let CI ship one quietly.
    throw "$Apk not found; refusing to build a package without the phone app inside."
} else {
    Write-Warning "$Apk not found; building without the phone app inside (run android\build.sh first)."
}

Write-Host '== Zip =='
if (Test-Path -LiteralPath $Zip) { Remove-Item -LiteralPath $Zip -Force }
Compress-Archive -LiteralPath $Files -DestinationPath $Zip -Force

Write-Host "built $Exe ($Version)"
Write-Host "      $Zip"

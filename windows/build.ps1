# SPDX-License-Identifier: GPL-3.0-or-later
# Builds Login Relay for Windows: runs the Core tests, publishes one self-contained LoginRelay.exe into
# windows\publish, puts the phone app (for convenience: the Windows app itself never installs it) and libusb next to it, publishes the USB probe tool into windows\publish\probe,
# and zips the lot.
#
#   powershell -ExecutionPolicy Bypass -File windows\build.ps1
#
# Environment (all optional):
#   LOGINRELAY_APK   the phone app to bundle (default ..\android\build\login-relay.apk; skipped with a warning if missing)
#   SKIP_TESTS=1     publish without running the tests first
#   GITHUB_TOKEN     (or GH_TOKEN) used for the GitHub API call that verifies the libusb download (avoids rate limits)
#
# libusb-1.0.dll (x64, LGPL-2.1-or-later, linked dynamically) is downloaded into windows\libs\ the first time, after
# its SHA-256 has been checked against the digest GitHub reports for the release asset. It is copied next to
# LoginRelay.exe, next to the probe, and into the zip, together with its licence text.
#
# Then, to run the end-to-end checks against the result:
#   python tests\e2e\run_e2e.py -- windows\publish\LoginRelay.exe
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$Version = '0.6.0'
$Publish = Join-Path $PSScriptRoot 'publish'
$Zip = Join-Path $Publish "Login-Relay-Windows-x64-$Version.zip"

$Libs = Join-Path $PSScriptRoot 'libs'
$LibusbDll = Join-Path $Libs 'libusb-1.0.dll'
$LibusbLicense = Join-Path $PSScriptRoot 'third-party\libusb-COPYING.txt'  # LGPL-2.1 text, kept in the repo (the release archive has none)

function Get-Libusb {
    if (Test-Path -LiteralPath $LibusbDll) { return }
    $Tag = 'v1.0.30'
    $AssetName = 'libusb-1.0.30.7z'
    $Url = "https://github.com/libusb/libusb/releases/download/$Tag/$AssetName"
    $Api = "https://api.github.com/repos/libusb/libusb/releases/tags/$Tag"
    New-Item -ItemType Directory -Force -Path $Libs | Out-Null
    $Work = Join-Path ([System.IO.Path]::GetTempPath()) ('libusb-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $Work | Out-Null
    try {
        Write-Host "== libusb: downloading $AssetName =="
        $Archive = Join-Path $Work $AssetName
        Invoke-WebRequest -Uri $Url -OutFile $Archive -UseBasicParsing

        # The expected hash comes from GitHub, not from this script: the release asset's own digest.
        $Headers = @{ 'User-Agent' = 'login-relay-build'; 'Accept' = 'application/vnd.github+json' }
        $Token = if ($env:GITHUB_TOKEN) { $env:GITHUB_TOKEN } else { $env:GH_TOKEN }
        if ($Token) { $Headers['Authorization'] = "Bearer $Token" }
        $Expected = $null
        try {
            $Release = Invoke-RestMethod -Uri $Api -Headers $Headers
            $Asset = $Release.assets | Where-Object { $_.name -eq $AssetName } | Select-Object -First 1
            if ($Asset -and $Asset.digest -and ($Asset.digest -match '^sha256:([0-9a-fA-F]{64})$')) {
                $Expected = $Matches[1].ToLowerInvariant()
            }
        } catch {
            throw "Could not verify the libusb download: the GitHub API call to $Api failed ($($_.Exception.Message)). Try again later, or set GITHUB_TOKEN to avoid rate limits."
        }
        if (-not $Expected) {
            throw "Could not verify the libusb download: GitHub reports no sha256 digest for $AssetName at $Api. Refusing to use an unverified file."
        }
        $Actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $Archive).Hash.ToLowerInvariant()
        if ($Actual -ne $Expected) {
            throw "libusb download failed verification: SHA-256 is $Actual, GitHub says $Expected. Refusing to use it."
        }
        Write-Host "   SHA-256 matches GitHub's digest ($Actual)"

        $Out = Join-Path $Work 'extracted'
        New-Item -ItemType Directory -Path $Out | Out-Null
        $SevenZip = Get-Command 7z -ErrorAction SilentlyContinue
        if ($SevenZip) {
            & $SevenZip.Source x -y "-o$Out" $Archive | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "7z could not extract $AssetName (exit code $LASTEXITCODE)" }
        } else {
            # Windows 10+ ships bsdtar, which reads .7z. Use that one, not a GNU tar that may be earlier on PATH.
            $Tar = Join-Path $env:SystemRoot 'System32\tar.exe'
            if (-not (Test-Path -LiteralPath $Tar)) { $Tar = 'tar' }
            & $Tar -xf $Archive -C $Out
            if ($LASTEXITCODE -ne 0) { throw "tar could not extract $AssetName (exit code $LASTEXITCODE); install 7-Zip and try again" }
        }
        $Dll = Join-Path $Out 'VS2022\MS64\dll\libusb-1.0.dll'
        if (-not (Test-Path -LiteralPath $Dll)) { throw "$AssetName does not contain VS2022\MS64\dll\libusb-1.0.dll" }
        Copy-Item -LiteralPath $Dll -Destination $LibusbDll -Force
        Write-Host "   libusb-1.0.dll is in $Libs"
    } finally {
        Remove-Item -LiteralPath $Work -Recurse -Force -ErrorAction SilentlyContinue
    }
}

if ($env:SKIP_TESTS -ne '1') {
    Write-Host '== Core tests =='
    dotnet test (Join-Path $PSScriptRoot 'tests\LoginRelay.Core.Tests\LoginRelay.Core.Tests.csproj') -c Release
    if ($LASTEXITCODE -ne 0) { throw "dotnet test failed (exit code $LASTEXITCODE)" }
}

Get-Libusb

Write-Host '== Publish =='
if (Test-Path -LiteralPath $Publish) { Remove-Item -LiteralPath $Publish -Recurse -Force }
dotnet publish (Join-Path $PSScriptRoot 'src\LoginRelay.App\LoginRelay.App.csproj') -c Release -o $Publish
if ($LASTEXITCODE -ne 0) { throw "dotnet publish failed (exit code $LASTEXITCODE)" }

$Exe = Join-Path $Publish 'LoginRelay.exe'
if (-not (Test-Path -LiteralPath $Exe)) { throw "publish did not produce $Exe" }

# libusb (USB accessory mode) and its licence, next to the exe.
Copy-Item -LiteralPath $LibusbDll -Destination (Join-Path $Publish 'libusb-1.0.dll') -Force
Copy-Item -LiteralPath $LibusbLicense -Destination (Join-Path $Publish 'libusb-COPYING.txt') -Force
$Files = @($Exe, (Join-Path $Publish 'libusb-1.0.dll'), (Join-Path $Publish 'libusb-COPYING.txt'))

# The USB probe: a test tool to run on a real PC with a phone (not in the zip).
Write-Host '== Publish the USB probe =='
$Probe = Join-Path $Publish 'probe'
dotnet publish (Join-Path $PSScriptRoot 'tools\LoginRelay.UsbProbe\LoginRelay.UsbProbe.csproj') -c Release -o $Probe
if ($LASTEXITCODE -ne 0) { throw "dotnet publish (probe) failed (exit code $LASTEXITCODE)" }
Copy-Item -LiteralPath $LibusbDll -Destination (Join-Path $Probe 'libusb-1.0.dll') -Force
Copy-Item -LiteralPath $LibusbLicense -Destination (Join-Path $Probe 'libusb-COPYING.txt') -Force

# The phone app, bundled for convenience (the Windows app doesn't install it).
$Apk = if ($env:LOGINRELAY_APK) { $env:LOGINRELAY_APK } else { Join-Path $PSScriptRoot '..\android\build\login-relay.apk' }
if (Test-Path -LiteralPath $Apk) {
    $ApkTarget = Join-Path $Publish 'login-relay.apk'
    Copy-Item -LiteralPath $Apk -Destination $ApkTarget -Force
    $Files += $ApkTarget
} elseif ($env:GITHUB_ACTIONS -eq 'true') {
    # A release without the phone app would be incomplete; don't let CI ship one quietly.
    throw "$Apk not found; refusing to build a package without the phone app inside."
} else {
    Write-Warning "$Apk not found; building without the phone app inside (run android\build.sh first)."
}

Write-Host '== Zip =='
if (Test-Path -LiteralPath $Zip) { Remove-Item -LiteralPath $Zip -Force }
Compress-Archive -LiteralPath $Files -DestinationPath $Zip -Force

Write-Host "built $Exe ($Version)"
Write-Host "      $Zip"
Write-Host "      $Probe\LoginRelay.UsbProbe.exe (run it on a PC with a phone: list, then run)"

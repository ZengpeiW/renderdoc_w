param(
    [ValidateSet('arm64', 'arm32', 'both', 'setup', 'clean', 'help')]
    [string]$Target = 'both'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$depsRoot = if ($env:RENDERDOC_ANDROID_DEPS) {
    [IO.Path]::GetFullPath($env:RENDERDOC_ANDROID_DEPS)
} else {
    Join-Path $env:LOCALAPPDATA 'RenderDocBuildDeps'
}
$msysRoot = if ($env:MSYS2_ROOT) { $env:MSYS2_ROOT } else { 'C:\msys64' }
$buildJobs = if ($env:BUILD_JOBS) { $env:BUILD_JOBS } else { $env:NUMBER_OF_PROCESSORS }
if (-not $buildJobs) { $buildJobs = '8' }

$jdkVersion = '8u432-b06'
$jdkDirectoryName = 'jdk8u432-b06'
$jdkArchiveName = 'OpenJDK8U-jdk_x64_windows_hotspot_8u432b06.zip'
$jdkUrl = "https://github.com/adoptium/temurin8-binaries/releases/download/jdk8u432-b06/$jdkArchiveName"
$jdkSha256 = '7780436b818b3bf8be7b56832f9e3f1ab74233736c4c0e34b3ae7c32c95e966e'

$commandLineToolsVersion = '11076708'
$commandLineToolsChannel = '12.0'
$commandLineToolsArchive = "commandlinetools-win-${commandLineToolsVersion}_latest.zip"
$commandLineToolsUrl = "https://dl.google.com/android/repository/$commandLineToolsArchive"
$commandLineToolsSha1 = '3d2917302740f476999a091bc5558837c7a863c5'

$ndkVersion = '26.3.11579264'
$buildToolsVersion = '26.0.1'
$androidPlatformVersion = '23'

function Write-Section([string]$Title) {
    Write-Host ''
    Write-Host ('=' * 60)
    Write-Host $Title
    Write-Host ('=' * 60)
}

function Invoke-Checked([string]$FilePath, [string[]]$Arguments) {
    & $FilePath @Arguments | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $($Arguments -join ' ')"
    }
}

function Get-VerifiedDownload(
    [string]$Url,
    [string]$Destination,
    [ValidateSet('SHA1', 'SHA256')][string]$Algorithm,
    [string]$ExpectedHash
) {
    if (Test-Path -LiteralPath $Destination) {
        $actualHash = (Get-FileHash -LiteralPath $Destination -Algorithm $Algorithm).Hash
        if ($actualHash -ieq $ExpectedHash) {
            Write-Host "[OK] Using cached $(Split-Path -Leaf $Destination)"
            return
        }

        Remove-Item -LiteralPath $Destination -Force
    }

    $partial = "$Destination.part"
    Write-Host "Downloading $Url"
    Invoke-Checked 'curl.exe' @(
        '--ssl-no-revoke', '--fail', '--location', '--retry', '3', '--retry-all-errors',
        '--continue-at', '-', '--output', $partial, $Url
    )
    Move-Item -LiteralPath $partial -Destination $Destination -Force

    $actualHash = (Get-FileHash -LiteralPath $Destination -Algorithm $Algorithm).Hash
    if ($actualHash -ine $ExpectedHash) {
        Remove-Item -LiteralPath $Destination -Force
        throw "Hash verification failed for $Destination. Expected $ExpectedHash, got $actualHash."
    }
}

function Find-AndroidSDK {
    $candidates = @(
        $env:ANDROID_SDK_ROOT,
        $env:ANDROID_HOME,
        $env:ANDROID_SDK,
        (Join-Path $env:LOCALAPPDATA 'Android\Sdk')
    ) | Where-Object { $_ }

    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            return [IO.Path]::GetFullPath($candidate)
        }
    }

    return Join-Path $depsRoot 'android-sdk'
}

function Find-SDKManagerJava([string]$BuildJavaHome) {
    $candidates = @(
        $env:ANDROID_STUDIO_JAVA_HOME,
        'D:\AndroidStudio\jbr',
        (Join-Path $env:ProgramFiles 'Android\Android Studio\jbr'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Android Studio\jbr'),
        $BuildJavaHome
    ) | Where-Object { $_ }

    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath (Join-Path $candidate 'bin\java.exe')) {
            return [IO.Path]::GetFullPath($candidate)
        }
    }

    throw 'No Java runtime was found for sdkmanager.'
}

function Convert-ToMsysPath([string]$Path) {
    $fullPath = [IO.Path]::GetFullPath($Path)
    if ($fullPath.Length -lt 3 -or $fullPath[1] -ne ':') {
        throw "Only local drive paths can be converted to MSYS2 paths: $fullPath"
    }

    $drive = $fullPath.Substring(0, 1).ToLowerInvariant()
    $rest = $fullPath.Substring(2).Replace('\', '/')
    return "/$drive$rest"
}

function Quote-Bash([string]$Value) {
    return "'" + $Value.Replace("'", "'\''") + "'"
}

function Install-Toolchain {
    Write-Section 'Preparing RenderDoc Android toolchain'
    Write-Host "Dependency cache: $depsRoot"

    $msysShell = Join-Path $msysRoot 'msys2_shell.cmd'
    if (-not (Test-Path -LiteralPath $msysShell)) {
        throw "MSYS2 was not found at '$msysRoot'. Install MSYS2 or set MSYS2_ROOT."
    }

    New-Item -ItemType Directory -Path $depsRoot -Force | Out-Null
    $downloadRoot = Join-Path $depsRoot 'downloads'
    New-Item -ItemType Directory -Path $downloadRoot -Force | Out-Null

    $buildJavaHome = if ($env:RENDERDOC_JAVA_HOME) {
        [IO.Path]::GetFullPath($env:RENDERDOC_JAVA_HOME)
    } else {
        Join-Path $depsRoot $jdkDirectoryName
    }

    if (-not (Test-Path -LiteralPath (Join-Path $buildJavaHome 'bin\java.exe'))) {
        Write-Host "Installing Temurin JDK $jdkVersion..."
        $jdkArchive = Join-Path $downloadRoot $jdkArchiveName
        Get-VerifiedDownload $jdkUrl $jdkArchive SHA256 $jdkSha256
        Expand-Archive -LiteralPath $jdkArchive -DestinationPath $depsRoot -Force
    }

    if (-not (Test-Path -LiteralPath (Join-Path $buildJavaHome 'bin\java.exe'))) {
        throw "The JDK archive did not produce '$buildJavaHome'."
    }
    Write-Host "[OK] Build Java: $buildJavaHome"

    $androidSdk = Find-AndroidSDK
    New-Item -ItemType Directory -Path $androidSdk -Force | Out-Null

    $sdkManager = Join-Path $androidSdk "cmdline-tools\$commandLineToolsChannel\bin\sdkmanager.bat"
    if (-not (Test-Path -LiteralPath $sdkManager)) {
        Write-Host "Installing Android command-line tools $commandLineToolsVersion..."
        $toolsArchive = Join-Path $downloadRoot $commandLineToolsArchive
        Get-VerifiedDownload $commandLineToolsUrl $toolsArchive SHA1 $commandLineToolsSha1

        $toolsStage = Join-Path $depsRoot "command-line-tools-$commandLineToolsVersion"
        Expand-Archive -LiteralPath $toolsArchive -DestinationPath $toolsStage -Force

        $toolsSource = Join-Path $toolsStage 'cmdline-tools'
        $toolsDestination = Join-Path $androidSdk "cmdline-tools\$commandLineToolsChannel"
        New-Item -ItemType Directory -Path $toolsDestination -Force | Out-Null
        Copy-Item -Path (Join-Path $toolsSource '*') -Destination $toolsDestination -Recurse -Force
    }

    if (-not (Test-Path -LiteralPath $sdkManager)) {
        throw "sdkmanager.bat was not found at '$sdkManager'."
    }
    Write-Host "[OK] Android command-line tools: $sdkManager"

    $androidNdk = Join-Path $androidSdk "ndk\$ndkVersion"
    $packagesReady =
        (Test-Path -LiteralPath (Join-Path $androidNdk 'build\cmake\android.toolchain.cmake')) -and
        (Test-Path -LiteralPath (Join-Path $androidSdk "build-tools\$buildToolsVersion\aapt.exe")) -and
        (Test-Path -LiteralPath (Join-Path $androidSdk "platforms\android-$androidPlatformVersion\android.jar"))

    if (-not $packagesReady) {
        $sdkManagerJava = Find-SDKManagerJava $buildJavaHome
        $savedJavaHome = $env:JAVA_HOME
        $savedPath = $env:PATH
        try {
            $env:JAVA_HOME = $sdkManagerJava
            $env:PATH = "$(Join-Path $sdkManagerJava 'bin');$savedPath"

            Write-Host 'Accepting Android SDK licences...'
            1..200 | ForEach-Object { 'y' } |
                & $sdkManager "--sdk_root=$androidSdk" '--licenses' | Out-Host
            if ($LASTEXITCODE -ne 0) {
                throw "sdkmanager --licenses failed with exit code $LASTEXITCODE."
            }

            Write-Host 'Installing Android platform, build-tools, platform-tools and NDK...'
            Invoke-Checked $sdkManager @(
                "--sdk_root=$androidSdk",
                'platform-tools',
                "platforms;android-$androidPlatformVersion",
                "build-tools;$buildToolsVersion",
                "ndk;$ndkVersion"
            )
        } finally {
            $env:JAVA_HOME = $savedJavaHome
            $env:PATH = $savedPath
        }
    }

    if (-not (Test-Path -LiteralPath (Join-Path $androidNdk 'build\cmake\android.toolchain.cmake'))) {
        throw "Android NDK installation is incomplete at '$androidNdk'."
    }
    if (-not (Test-Path -LiteralPath (Join-Path $androidSdk "build-tools\$buildToolsVersion\aapt.exe"))) {
        throw "Android build-tools $buildToolsVersion are missing."
    }
    if (-not (Test-Path -LiteralPath (Join-Path $androidSdk "platforms\android-$androidPlatformVersion\android.jar"))) {
        throw "Android platform $androidPlatformVersion is missing."
    }
    Write-Host "[OK] Android SDK: $androidSdk"
    Write-Host "[OK] Android NDK: $androidNdk"

    $requiredMsysTools = @(
        (Join-Path $msysRoot 'mingw64\bin\cmake.exe'),
        (Join-Path $msysRoot 'mingw64\bin\ninja.exe'),
        (Join-Path $msysRoot 'mingw64\bin\c++.exe'),
        (Join-Path $msysRoot 'usr\bin\git.exe')
    )
    $missingMsysTools = $requiredMsysTools | Where-Object {
        -not (Test-Path -LiteralPath $_)
    }

    if ($missingMsysTools) {
        Write-Host 'Installing missing MSYS2 build packages...'
        $pacmanLock = Join-Path $msysRoot 'var\lib\pacman\db.lck'
        if ((Test-Path -LiteralPath $pacmanLock) -and
            -not (Get-Process pacman -ErrorAction SilentlyContinue)) {
            Remove-Item -LiteralPath $pacmanLock -Force
        }

        # The bundled MSYS2 CA store can be stale on a fresh installation. Use Windows curl for
        # package downloads while retaining pacman's normal signature verification.
        $pacmanConfig = Join-Path $depsRoot 'pacman-renderdoc.conf'
        $pacmanConfigText = @'
[options]
Architecture = auto
CheckSpace
SigLevel = Required
LocalFileSigLevel = Optional
XferCommand = /c/Windows/System32/curl.exe --ssl-no-revoke --fail --location --retry 3 --continue-at - --output %o %u

[mingw64]
Include = /etc/pacman.d/mirrorlist.mingw

[msys]
Include = /etc/pacman.d/mirrorlist.msys
'@
        [IO.File]::WriteAllText($pacmanConfig, $pacmanConfigText,
                                [Text.UTF8Encoding]::new($false))
        $pacmanConfigMsys = Convert-ToMsysPath $pacmanConfig

        Invoke-Checked $msysShell @(
            '-mingw64', '-defterm', '-no-start', '-c',
            "pacman --config $(Quote-Bash $pacmanConfigMsys) -Sy --needed --noconfirm mingw-w64-x86_64-cmake mingw-w64-x86_64-ninja mingw-w64-x86_64-gcc git"
        )
    }

    foreach ($toolPath in $requiredMsysTools) {
        if (-not (Test-Path -LiteralPath $toolPath)) {
            throw "$(Split-Path -Leaf $toolPath) is still missing from MSYS2."
        }
    }
    Write-Host '[OK] MSYS2 CMake, Ninja, GCC/C++ and Git are installed.'

    return [pscustomobject]@{
        JavaHome = $buildJavaHome
        AndroidSdk = $androidSdk
        AndroidNdk = $androidNdk
        MsysShell = $msysShell
    }
}

function Invoke-AndroidBuild($Toolchain, [bool]$BuildArm64, [bool]$BuildArm32) {
    $sdkMsys = Convert-ToMsysPath $Toolchain.AndroidSdk
    $ndkMsys = Convert-ToMsysPath $Toolchain.AndroidNdk
    $javaMsys = Convert-ToMsysPath $Toolchain.JavaHome

    $scriptLines = @(
        '#!/usr/bin/env bash',
        'set -euo pipefail',
        "export ANDROID_HOME=$(Quote-Bash $sdkMsys)",
        "export ANDROID_SDK_ROOT=$(Quote-Bash $sdkMsys)",
        "export ANDROID_SDK=$(Quote-Bash $sdkMsys)",
        "export ANDROID_NDK_HOME=$(Quote-Bash $ndkMsys)",
        "export ANDROID_NDK_ROOT=$(Quote-Bash $ndkMsys)",
        "export ANDROID_NDK=$(Quote-Bash $ndkMsys)",
        "export JAVA_HOME=$(Quote-Bash $javaMsys)",
        'export PATH="$JAVA_HOME/bin:/mingw64/bin:/usr/bin:$PATH"',
        'java -version',
        'cmake --version | head -n 1',
        'ninja --version'
    )

    if ($BuildArm64) {
        $scriptLines += @(
            'echo "============================================================"',
            'echo "Building ARM64 arm64-v8a"',
            'echo "============================================================"',
            "cmake -S . -B build-android-arm64 -G Ninja -DBUILD_ANDROID=On -DANDROID_ABI=arm64-v8a -DANDROID_PLATFORM=android-21 -DANDROID_BUILD_TOOLS_VERSION=$buildToolsVersion -DAPK_TARGET_ID=android-$androidPlatformVersion -DCMAKE_BUILD_TYPE=Release -DSTRIP_ANDROID_LIBRARY=On",
            "cmake --build build-android-arm64 --parallel $buildJobs",
            'test -f build-android-arm64/bin/org.renderdoc.renderdoccmd.arm64.apk'
        )
    }

    if ($BuildArm32) {
        $scriptLines += @(
            'echo "============================================================"',
            'echo "Building ARM32 armeabi-v7a"',
            'echo "============================================================"',
            "cmake -S . -B build-android-arm32 -G Ninja -DBUILD_ANDROID=On -DANDROID_ABI=armeabi-v7a -DANDROID_PLATFORM=android-21 -DANDROID_BUILD_TOOLS_VERSION=$buildToolsVersion -DAPK_TARGET_ID=android-$androidPlatformVersion -DCMAKE_BUILD_TYPE=Release -DSTRIP_ANDROID_LIBRARY=On",
            "cmake --build build-android-arm32 --parallel $buildJobs",
            'test -f build-android-arm32/bin/org.renderdoc.renderdoccmd.arm32.apk'
        )
    }

    $temporaryScript = Join-Path $env:TEMP "renderdoc_android_build_$PID.sh"
    [IO.File]::WriteAllText($temporaryScript, ($scriptLines -join "`n") + "`n",
                            [Text.UTF8Encoding]::new($false))

    # MinGW's native linker inherits the active Windows code page and cannot reliably consume the
    # repository's non-ASCII path. Build through a temporary ASCII-only drive mapping instead.
    $buildDrive = if ($env:RENDERDOC_BUILD_DRIVE) { $env:RENDERDOC_BUILD_DRIVE } else { 'R:' }
    if ($buildDrive -notmatch '^[A-Za-z]:$') {
        throw "RENDERDOC_BUILD_DRIVE must be a drive letter such as R:. Got '$buildDrive'."
    }
    if (Test-Path -LiteralPath "$buildDrive\") {
        throw "Build drive $buildDrive is already in use. Set RENDERDOC_BUILD_DRIVE to a free drive letter."
    }

    Invoke-Checked 'subst.exe' @($buildDrive, $repoRoot)

    try {
        foreach ($directoryName in @(
            $(if ($BuildArm64) { 'build-android-arm64' }),
            $(if ($BuildArm32) { 'build-android-arm32' })
        ) | Where-Object { $_ }) {
            $cacheFile = Join-Path (Join-Path $repoRoot $directoryName) 'CMakeCache.txt'
            if (Test-Path -LiteralPath $cacheFile) {
                $expectedHome = "CMAKE_HOME_DIRECTORY:INTERNAL=$buildDrive/"
                if (-not (Select-String -LiteralPath $cacheFile -SimpleMatch $expectedHome -Quiet)) {
                    Write-Host "Removing incompatible CMake cache in $directoryName..."
                    Remove-Item -LiteralPath (Split-Path -Parent $cacheFile) -Recurse -Force
                }
            }
        }

        $temporaryScriptMsys = Convert-ToMsysPath $temporaryScript
        Invoke-Checked $Toolchain.MsysShell @(
            '-mingw64', '-defterm', '-no-start', '-where', "$buildDrive\",
            '-c', "bash $(Quote-Bash $temporaryScriptMsys)"
        )
    } finally {
        Remove-Item -LiteralPath $temporaryScript -Force -ErrorAction SilentlyContinue
        & subst.exe $buildDrive /D | Out-Null
    }
}

try {
    if ($Target -eq 'help') {
        Write-Host 'Usage: build_android.bat [arm64|arm32|both|setup|clean]'
        Write-Host ''
        Write-Host '  setup  Download and configure dependencies without building.'
        Write-Host '  clean  Remove build-android-arm32 and build-android-arm64.'
        exit 0
    }

    if ($Target -eq 'clean') {
        foreach ($directoryName in @('build-android-arm32', 'build-android-arm64')) {
            $targetPath = Join-Path $repoRoot $directoryName
            if (Test-Path -LiteralPath $targetPath) {
                Remove-Item -LiteralPath $targetPath -Recurse -Force
                Write-Host "Removed $targetPath"
            }
        }
        exit 0
    }

    $toolchain = Install-Toolchain

    Write-Section 'Toolchain ready'
    Write-Host "Java:       $($toolchain.JavaHome)"
    Write-Host "Android SDK:$($toolchain.AndroidSdk)"
    Write-Host "Android NDK:$($toolchain.AndroidNdk)"
    Write-Host "MSYS2:      $msysRoot"

    if ($Target -eq 'setup') {
        Write-Host ''
        Write-Host 'Setup completed. Run build_android.bat arm64, arm32, or both to build APKs.'
        exit 0
    }

    Invoke-AndroidBuild $toolchain ($Target -in @('arm64', 'both')) ($Target -in @('arm32', 'both'))

    Write-Section 'Build completed successfully'
    if ($Target -in @('arm64', 'both')) {
        Write-Host (Join-Path $repoRoot 'build-android-arm64\bin\org.renderdoc.renderdoccmd.arm64.apk')
    }
    if ($Target -in @('arm32', 'both')) {
        Write-Host (Join-Path $repoRoot 'build-android-arm32\bin\org.renderdoc.renderdoccmd.arm32.apk')
    }
} catch {
    Write-Host ''
    Write-Error $_
    exit 1
}

[CmdletBinding()]
param(
    [switch]$CheckOnly,
    [switch]$Repair,
    [switch]$StartMediaHub
)

# Keep this file ASCII-only: Windows PowerShell 3.0 reads UTF-8 files without BOM incorrectly.
$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$toolsDirectory = Join-Path $projectRoot ".tools"
$ffmpegDirectory = Join-Path $toolsDirectory "ffmpeg"
$script:Failures = @()
$script:Warnings = @()
$script:Changes = @()

function Write-Check([string]$Name, [bool]$Passed, [string]$Details) {
    if ($Passed) {
        Write-Host ("[ OK ] {0}: {1}" -f $Name, $Details) -ForegroundColor Green
    } else {
        Write-Host ("[FAIL] {0}: {1}" -f $Name, $Details) -ForegroundColor Red
    }
}

function Add-Failure([string]$Message) {
    $script:Failures += $Message
    Write-Check "Error" $false $Message
}

function Add-Warning([string]$Message) {
    $script:Warnings += $Message
    Write-Host ("[WARN] {0}" -f $Message) -ForegroundColor Yellow
}

function Add-Change([string]$Message) {
    $script:Changes += $Message
    Write-Host ("[DONE] {0}" -f $Message) -ForegroundColor Cyan
}

function Enable-Tls12 {
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    } catch {
        Add-Warning "TLS 1.2 could not be enabled. Downloads may fail on this system."
    }
}

function Download-File([string]$Url, [string]$Destination) {
    Enable-Tls12
    Write-Host ("Downloading {0}" -f $Url) -ForegroundColor Cyan
    $client = New-Object System.Net.WebClient
    $client.Headers.Add("User-Agent", "MediaHub dependency installer")
    try {
        $client.DownloadFile($Url, $Destination)
    } finally {
        $client.Dispose()
    }
}

function Get-Sha256([string]$Path) {
    $stream = [System.IO.File]::OpenRead($Path)
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hash = $algorithm.ComputeHash($stream)
        return ([System.BitConverter]::ToString($hash)).Replace("-", "").ToLowerInvariant()
    } finally {
        $algorithm.Dispose()
        $stream.Dispose()
    }
}

function Get-OsInfo {
    try {
        return Get-WmiObject -Class Win32_OperatingSystem -ErrorAction Stop
    } catch {
        try {
            $registry = Get-ItemProperty -LiteralPath "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion" -ErrorAction Stop
            $reportedVersion = [Environment]::OSVersion.Version.ToString()
            return New-Object PSObject -Property @{
                Caption = $registry.ProductName
                Version = $reportedVersion
                OSArchitecture = $(if ([Environment]::Is64BitOperatingSystem) { "64-bit" } else { "32-bit" })
            }
        } catch {
            return $null
        }
    }
}

function Test-Python([string]$Executable) {
    if (-not $Executable -or -not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
        return $false
    }
    & $Executable -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
    return $LASTEXITCODE -eq 0
}

function Find-SystemPython {
    $candidates = @()
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        try {
            $launchedPath = (& $launcher.Source -3 -c "import sys; print(sys.executable)" 2>$null | Select-Object -First 1)
            if ($launchedPath) { $candidates += $launchedPath.Trim() }
        } catch {}
    }
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pythonCommand) { $candidates += $pythonCommand.Source }
    $candidates += (Join-Path $env:LocalAppData "Programs\Python\Python311\python.exe")
    $candidates += (Join-Path $env:LocalAppData "Programs\Python\Python312\python.exe")
    $candidates += (Join-Path $env:LocalAppData "Programs\Python\Python313\python.exe")
    foreach ($candidate in $candidates) {
        if (Test-Python $candidate) { return $candidate }
    }
    return $null
}

function Install-Python {
    if (-not [Environment]::Is64BitOperatingSystem) {
        throw "Automatic Python and FFmpeg installation requires 64-bit Windows."
    }
    $version = "3.11.9"
    $installer = Join-Path $env:TEMP "mediahub-python-$version-amd64.exe"
    $url = "https://www.python.org/ftp/python/$version/python-$version-amd64.exe"
    Download-File $url $installer

    $signature = Get-AuthenticodeSignature -FilePath $installer
    if ($signature.Status -ne "Valid") {
        Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
        throw ("The Python installer signature is not valid: {0}" -f $signature.Status)
    }
    $arguments = "/quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 InstallLauncherAllUsers=0 Include_pip=1 Include_test=0"
    $process = Start-Process -FilePath $installer -ArgumentList $arguments -Wait -PassThru
    Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
    if ($process.ExitCode -ne 0) {
        throw ("Python installer returned exit code {0}" -f $process.ExitCode)
    }
    $installed = Join-Path $env:LocalAppData "Programs\Python\Python311\python.exe"
    if (-not (Test-Python $installed)) {
        throw "Python was installed but a compatible python.exe could not be found."
    }
    Add-Change "Python $version installed for the current user."
    return $installed
}

function Find-FfmpegBin {
    $ffmpegCommand = Get-Command ffmpeg.exe -ErrorAction SilentlyContinue
    $ffprobeCommand = Get-Command ffprobe.exe -ErrorAction SilentlyContinue
    if ($ffmpegCommand -and $ffprobeCommand) {
        try {
            & $ffmpegCommand.Source -version 1>$null 2>$null
            $ffmpegOk = $LASTEXITCODE -eq 0
            & $ffprobeCommand.Source -version 1>$null 2>$null
            if ($ffmpegOk -and $LASTEXITCODE -eq 0) { return (Split-Path $ffmpegCommand.Source -Parent) }
        } catch {}
    }
    if (Test-Path -LiteralPath $ffmpegDirectory) {
        $local = Get-ChildItem -LiteralPath $ffmpegDirectory -Filter ffmpeg.exe -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($local) {
            $probe = Join-Path $local.Directory.FullName "ffprobe.exe"
            if (Test-Path -LiteralPath $probe) { return $local.Directory.FullName }
        }
    }
    $wingetPackages = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
    if (Test-Path -LiteralPath $wingetPackages) {
        $wingetFfmpeg = Get-ChildItem -LiteralPath $wingetPackages -Filter ffmpeg.exe -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -like "*Gyan.FFmpeg*" } | Select-Object -First 1
        if ($wingetFfmpeg) {
            $wingetProbe = Join-Path $wingetFfmpeg.Directory.FullName "ffprobe.exe"
            if (Test-Path -LiteralPath $wingetProbe) { return $wingetFfmpeg.Directory.FullName }
        }
    }
    return $null
}

function Install-Ffmpeg([version]$OsVersion) {
    if (-not [Environment]::Is64BitOperatingSystem) {
        throw "The supported FFmpeg builds require 64-bit Windows."
    }
    $archive = Join-Path $env:TEMP "mediahub-ffmpeg.zip"
    $staging = Join-Path $env:TEMP ("mediahub-ffmpeg-" + [Guid]::NewGuid().ToString("N"))
    $expectedHash = $null
    if ($OsVersion.Major -lt 10) {
        $url = "https://github.com/GyanD/codexffmpeg/releases/download/6.1/ffmpeg-6.1-full_build.zip"
        $expectedHash = "b39c5a040aecb343e079e6e33ab0360832b9f0c8369c82dc4b7c825e558dfe24"
        Add-Warning "Installing FFmpeg 6.1, the compatible build selected for Windows 8/8.1."
    } else {
        $url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
        $checksumFile = Join-Path $env:TEMP "mediahub-ffmpeg.sha256"
        Download-File "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip.sha256" $checksumFile
        $checksumText = [System.IO.File]::ReadAllText($checksumFile)
        $match = [regex]::Match($checksumText, "[A-Fa-f0-9]{64}")
        Remove-Item -LiteralPath $checksumFile -Force -ErrorAction SilentlyContinue
        if (-not $match.Success) { throw "The FFmpeg checksum could not be read." }
        $expectedHash = $match.Value.ToLowerInvariant()
    }

    Download-File $url $archive
    $actualHash = Get-Sha256 $archive
    if ($actualHash -ne $expectedHash) {
        Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
        throw "The FFmpeg archive checksum does not match. Installation was cancelled."
    }

    New-Item -ItemType Directory -Path $staging -Force | Out-Null
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::ExtractToDirectory($archive, $staging)
    $binary = Get-ChildItem -LiteralPath $staging -Filter ffmpeg.exe -Recurse | Select-Object -First 1
    if (-not $binary) { throw "ffmpeg.exe was not found in the downloaded archive." }
    $packageRoot = Split-Path (Split-Path $binary.FullName -Parent) -Parent
    if (-not (Test-Path -LiteralPath $toolsDirectory)) {
        New-Item -ItemType Directory -Path $toolsDirectory | Out-Null
    }
    if (Test-Path -LiteralPath $ffmpegDirectory) {
        Remove-Item -LiteralPath $ffmpegDirectory -Recurse -Force
    }
    Move-Item -LiteralPath $packageRoot -Destination $ffmpegDirectory
    Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
    Add-Change "FFmpeg installed locally in .tools\ffmpeg."
    return Find-FfmpegBin
}

function Test-ProjectPackages([string]$Python) {
    if (-not (Test-Python $Python)) { return $false }
    Push-Location $projectRoot
    try {
        & $Python -c "import flask, flask_login, flask_sqlalchemy, flask_wtf, cryptography, PIL, waitress; from mediahub import create_app" 2>$null
        return $LASTEXITCODE -eq 0
    } finally {
        Pop-Location
    }
}

Write-Host "MediaHub dependency check" -ForegroundColor White
Write-Host ("Project: {0}" -f $projectRoot)
Write-Host ("Mode: {0}" -f $(if ($CheckOnly) { "check only" } else { "check and install" }))
Write-Host ""

$powerShellOk = $PSVersionTable.PSVersion.Major -ge 3
Write-Check "PowerShell" $powerShellOk $PSVersionTable.PSVersion.ToString()
if (-not $powerShellOk) { Add-Failure "Windows PowerShell 3.0 or newer is required." }

$os = Get-OsInfo
if ($os) {
    $osVersion = [version]$os.Version
    Write-Check "Windows" ($osVersion.Major -ge 6) ("{0}, version {1}, {2}" -f $os.Caption, $os.Version, $os.OSArchitecture)
    if ($osVersion.Major -eq 6 -and $osVersion.Minor -eq 2) {
        Add-Warning "This is Windows 8.0. Python 3.11 officially supports Windows 8.1 and newer; installation will be attempted but is not guaranteed."
    }
} else {
    $osVersion = [Environment]::OSVersion.Version
    Add-Warning ("Windows edition could not be identified; reported version is {0}." -f $osVersion)
}

Write-Check "Architecture" ([Environment]::Is64BitOperatingSystem) $(if ([Environment]::Is64BitOperatingSystem) { "64-bit" } else { "32-bit (unsupported by the bundled FFmpeg installer)" })

$systemPython = Find-SystemPython
if ($systemPython) {
    $pythonVersion = (& $systemPython --version 2>&1 | Select-Object -First 1)
    Write-Check "System Python" $true ("{0} at {1}" -f $pythonVersion, $systemPython)
} elseif (-not (Test-Python $venvPython)) {
    Write-Check "System Python" $false "Python 3.11 or newer was not found."
    if (-not $CheckOnly) {
        try { $systemPython = Install-Python } catch { Add-Failure $_.Exception.Message }
    } else {
        Add-Failure "Python 3.11 or newer must be installed."
    }
} else {
    Write-Check "System Python" $true "Not required; the existing virtual environment is usable."
}

if (-not (Test-Python $venvPython)) {
    Write-Check "Virtual environment" $false ".venv is missing or incompatible."
    if (-not $CheckOnly -and $systemPython) {
        try {
            if (Test-Path -LiteralPath (Join-Path $projectRoot ".venv")) {
                $backup = Join-Path $projectRoot (".venv.backup-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
                Move-Item -LiteralPath (Join-Path $projectRoot ".venv") -Destination $backup
                Add-Warning ("The incompatible environment was moved to {0}." -f $backup)
            }
            & $systemPython -m venv (Join-Path $projectRoot ".venv")
            if ($LASTEXITCODE -ne 0) { throw "Python could not create the virtual environment." }
            Add-Change "Virtual environment created."
        } catch { Add-Failure $_.Exception.Message }
    } elseif ($CheckOnly) {
        Add-Failure "The MediaHub virtual environment is not ready."
    }
} else {
    Write-Check "Virtual environment" $true $venvPython
}

$packagesOk = Test-ProjectPackages $venvPython
if ($packagesOk -and -not $Repair) {
    Write-Check "Python packages" $true "Flask, database, image and server packages are available."
} elseif (-not $CheckOnly -and (Test-Python $venvPython)) {
    Write-Check "Python packages" $false $(if ($Repair) { "Repair requested." } else { "One or more packages are missing." })
    Push-Location $projectRoot
    try {
        & $venvPython -m pip install -e .
        if ($LASTEXITCODE -ne 0) { throw "pip could not install the project dependencies." }
        if (-not (Test-ProjectPackages $venvPython)) { throw "Dependency imports still fail after installation." }
        Add-Change "Python packages installed and verified."
    } catch { Add-Failure $_.Exception.Message } finally { Pop-Location }
} else {
    Write-Check "Python packages" $false "Required packages are missing."
    Add-Failure "Run this script without -CheckOnly to install Python packages."
}

$ffmpegBin = Find-FfmpegBin
if ($ffmpegBin) {
    Write-Check "FFmpeg" $true $ffmpegBin
} else {
    Write-Check "FFmpeg" $false "ffmpeg.exe and ffprobe.exe were not found."
    if (-not $CheckOnly) {
        try { $ffmpegBin = Install-Ffmpeg $osVersion } catch { Add-Failure $_.Exception.Message }
    } else {
        Add-Failure "FFmpeg must be installed for video playback."
    }
}

if ($ffmpegBin) {
    $env:MEDIAHUB_FFMPEG = Join-Path $ffmpegBin "ffmpeg.exe"
    $env:MEDIAHUB_FFPROBE = Join-Path $ffmpegBin "ffprobe.exe"
    & $env:MEDIAHUB_FFMPEG -version 1>$null 2>$null
    $ffmpegOk = $LASTEXITCODE -eq 0
    & $env:MEDIAHUB_FFPROBE -version 1>$null 2>$null
    $ffprobeOk = $LASTEXITCODE -eq 0
    Write-Check "FFmpeg executables" ($ffmpegOk -and $ffprobeOk) "ffmpeg and ffprobe started successfully."
    if (-not ($ffmpegOk -and $ffprobeOk)) { Add-Failure "FFmpeg is present but cannot start on this Windows version." }
}

$hlsFile = Join-Path $projectRoot "mediahub\static\vendor\hls.min.js"
Write-Check "Browser player" (Test-Path -LiteralPath $hlsFile) $hlsFile
if (-not (Test-Path -LiteralPath $hlsFile)) { Add-Failure "The local hls.js player file is missing." }

$portLine = netstat -ano -p tcp 2>$null | Select-String -Pattern ":8080\s+.*LISTENING"
if ($portLine) {
    Add-Warning "TCP port 8080 is already in use. MediaHub may already be running."
} else {
    Write-Check "TCP port 8080" $true "Available."
}

Write-Host ""
Write-Host "Summary" -ForegroundColor White
Write-Host ("Changes: {0}; warnings: {1}; failures: {2}" -f $script:Changes.Count, $script:Warnings.Count, $script:Failures.Count)
if ($script:Failures.Count -gt 0) {
    foreach ($failure in $script:Failures) { Write-Host (" - {0}" -f $failure) -ForegroundColor Red }
    exit 1
}

Write-Host "All required MediaHub dependencies are ready." -ForegroundColor Green
if ($StartMediaHub) {
    & (Join-Path $PSScriptRoot "start-windows.ps1")
}

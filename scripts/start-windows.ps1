param([switch]$Install)

# Keep this file ASCII-only for Windows PowerShell 3.0 compatibility.
$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $projectRoot

$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$mediahubCommand = Join-Path $projectRoot ".venv\Scripts\mediahub.exe"

if ($Install -or -not (Test-Path -LiteralPath $venvPython)) {
    $arguments = @()
    if ($Install) { $arguments += "-Repair" }
    & (Join-Path $PSScriptRoot "check-dependencies.ps1") @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Dependency installation failed. Review the messages above."
    }
}

$ffmpegCommand = Get-Command ffmpeg.exe -ErrorAction SilentlyContinue
$ffprobeCommand = Get-Command ffprobe.exe -ErrorAction SilentlyContinue
if (-not $ffmpegCommand -or -not $ffprobeCommand) {
    $localFfmpeg = Get-ChildItem -LiteralPath (Join-Path $projectRoot ".tools") -Filter ffmpeg.exe -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($localFfmpeg) {
        $localProbe = Join-Path $localFfmpeg.Directory.FullName "ffprobe.exe"
        if (Test-Path -LiteralPath $localProbe) {
            $env:MEDIAHUB_FFMPEG = $localFfmpeg.FullName
            $env:MEDIAHUB_FFPROBE = $localProbe
            $ffmpegCommand = $localFfmpeg
            $ffprobeCommand = Get-Item -LiteralPath $localProbe
        }
    }
}

if (-not $ffmpegCommand -or -not $ffprobeCommand) {
    $wingetPackages = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
    if (Test-Path -LiteralPath $wingetPackages) {
        $wingetFfmpeg = Get-ChildItem -LiteralPath $wingetPackages -Filter ffmpeg.exe -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -like "*Gyan.FFmpeg*" } | Select-Object -First 1
        if ($wingetFfmpeg) {
            $wingetProbe = Join-Path $wingetFfmpeg.Directory.FullName "ffprobe.exe"
            if (Test-Path -LiteralPath $wingetProbe) {
                $env:MEDIAHUB_FFMPEG = $wingetFfmpeg.FullName
                $env:MEDIAHUB_FFPROBE = $wingetProbe
                $ffmpegCommand = $wingetFfmpeg
                $ffprobeCommand = Get-Item -LiteralPath $wingetProbe
            }
        }
    }
}

if (-not $ffmpegCommand -or -not $ffprobeCommand) {
    Write-Warning "FFmpeg was not found. MediaHub will start, but video playback will fail."
    Write-Host "Run .\scripts\check-dependencies.ps1 to install it locally." -ForegroundColor Yellow
}

if (-not (Test-Path -LiteralPath $mediahubCommand)) {
    throw "MediaHub is not installed in .venv. Run .\scripts\check-dependencies.ps1 -Repair"
}

$existingListener = netstat -ano -p tcp 2>$null | Select-String -Pattern ":8080\s+.*LISTENING" | Select-Object -First 1
if ($existingListener) {
    throw "TCP port 8080 is already in use. Stop the existing MediaHub process before starting another instance."
}

Write-Host "MediaHub is starting at http://localhost:8080" -ForegroundColor Green
& $mediahubCommand --host 0.0.0.0 --port 8080

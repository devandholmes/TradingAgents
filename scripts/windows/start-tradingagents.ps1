<#
.SYNOPSIS
  Start the dashboard in Docker: Docker Desktop if it is not running, then `docker compose up -d postgres web`.

.DESCRIPTION
  Run at sign-in by the Startup-folder shortcut that install-autostart.ps1 creates, or by hand.
  Waits up to -TimeoutMinutes for the Docker engine. Logs to %LOCALAPPDATA%\TradingAgents\autostart.log.
#>
param([int]$TimeoutMinutes = 10)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$logDir = Join-Path $env:LOCALAPPDATA 'TradingAgents'
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir 'autostart.log'

function Write-Log([string]$message) {
    $line = '{0:yyyy-MM-dd HH:mm:ss} {1}' -f (Get-Date), $message
    Add-Content -Path $log -Value $line -Encoding utf8
    Write-Host $line
}

function Test-DockerEngine {
    & docker info --format '{{.ServerVersion}}' *> $null
    return $LASTEXITCODE -eq 0
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Log 'docker is not on PATH: install Docker Desktop first'
    exit 1
}

if (-not (Test-DockerEngine)) {
    $desktop = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
    if (-not (Get-Process 'Docker Desktop' -ErrorAction SilentlyContinue)) {
        if (-not (Test-Path $desktop)) {
            Write-Log "Docker Desktop not found at $desktop"
            exit 1
        }
        Write-Log 'starting Docker Desktop'
        Start-Process -FilePath $desktop
    }
    $deadline = (Get-Date).AddMinutes($TimeoutMinutes)
    while (-not (Test-DockerEngine)) {
        if ((Get-Date) -gt $deadline) {
            Write-Log "the Docker engine did not start within $TimeoutMinutes minutes"
            exit 1
        }
        Start-Sleep -Seconds 5
    }
}

Write-Log "docker compose up -d postgres web (in $repo)"
Push-Location $repo
try {
    & docker compose up -d postgres web
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}
Write-Log "docker compose exited with $code"
exit $code

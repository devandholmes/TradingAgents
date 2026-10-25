<#
.SYNOPSIS
  Remove the sign-in shortcut that install-autostart.ps1 created. The containers keep running;
  stop them with `docker compose stop web`.
#>
$ErrorActionPreference = 'Stop'
$link = Join-Path ([Environment]::GetFolderPath('Startup')) 'TradingAgents.lnk'
if (Test-Path $link) {
    Remove-Item $link
    Write-Host "Removed $link"
} else {
    Write-Host "Nothing to remove: $link does not exist"
}

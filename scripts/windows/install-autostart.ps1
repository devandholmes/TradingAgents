<#
.SYNOPSIS
  Start the dashboard at every sign-in: a shortcut in your Startup folder runs start-tradingagents.ps1.

.DESCRIPTION
  No admin rights needed; affects only the current Windows user. Remove it with uninstall-autostart.ps1.
  The shortcut points at this checkout, so run the installer from the checkout you want started.
#>
$ErrorActionPreference = 'Stop'
$script = Join-Path $PSScriptRoot 'start-tradingagents.ps1'
$link = Join-Path ([Environment]::GetFolderPath('Startup')) 'TradingAgents.lnk'

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($link)
$shortcut.TargetPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`""
$shortcut.WorkingDirectory = $PSScriptRoot
$shortcut.WindowStyle = 7   # minimized
$shortcut.Description = 'Start TradingAgents (Docker) at sign-in'
$shortcut.Save()

Write-Host "Installed $link"
Write-Host "It runs $script at sign-in; its log is $env:LOCALAPPDATA\TradingAgents\autostart.log"

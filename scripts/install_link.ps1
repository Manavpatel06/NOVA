<#
.SYNOPSIS
  Make this laptop a NOVA Link device (ADR 0024): NOVA lives on the hub; this
  laptop only runs its skills and passes "Nova, ..." from its mic.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File D:\NOVA\scripts\install_link.ps1
      Asks before adding "NOVA Link" to your sign-in apps, then starts it now.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File D:\NOVA\scripts\install_link.ps1 -Remove
      Stops starting it at sign-in. (The old NOVA app is untouched either way.)
#>
[CmdletBinding()]
param([switch]$Remove)

$ErrorActionPreference = 'Stop'
$Root     = Split-Path -Parent $PSScriptRoot
$PythonW  = Join-Path $Root '.venv\Scripts\pythonw.exe'
$Startup  = [Environment]::GetFolderPath('Startup')
$Shortcut = Join-Path $Startup 'NOVA Link.lnk'

function Good([string]$m) { Write-Host "  OK  $m" -ForegroundColor Green }
function Bad([string]$m)  { Write-Host "  !!  $m" -ForegroundColor Red }

if ($Remove) {
    if (Test-Path -LiteralPath $Shortcut) { Remove-Item -LiteralPath $Shortcut; Good 'NOVA Link no longer starts at sign-in' }
    else { Good 'it was not set to start at sign-in' }
    exit 0
}

if (-not (Test-Path -LiteralPath $PythonW)) { Bad "NOVA's Python isn't at $PythonW"; exit 1 }
$hub = (Select-String -LiteralPath (Join-Path $Root 'config\nova.yaml') -Pattern '^\s*host:\s*"?([^"\s]*)"?' |
        Select-Object -First 1).Matches.Groups[1].Value
if ([string]::IsNullOrWhiteSpace($hub)) { Bad 'config\nova.yaml has no hub.host yet'; exit 1 }

Write-Host ''
Write-Host "NOVA Link connects this laptop to NOVA on $hub."
Write-Host 'It uses about 90 MB (no AI models here), runs the laptop skills NOVA asks for after you approve'
Write-Host 'them on the hub, and listens for "Nova, ..." on this mic.'
Write-Host 'Do not run the old NOVA app at the same time (both would listen to the mic).'
$answer = Read-Host 'Start NOVA Link automatically when you sign in? (y/n)'
if ($answer -match '^(y|yes)$') {
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($Shortcut)
    $lnk.TargetPath = $PythonW
    $lnk.Arguments = '-m nova.link'
    $lnk.WorkingDirectory = $Root
    $lnk.Description = 'NOVA Link: this laptop, connected to NOVA on the hub'
    $lnk.Save()
    Good 'NOVA Link will start when you sign in'
} else {
    Good 'not added to sign-in (start it any time with this script)'
}

$running = Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
           Where-Object { $_.CommandLine -like '*nova.link*' }
if ($running) { Good 'NOVA Link is already running' }
else {
    Start-Process -FilePath $PythonW -ArgumentList '-m', 'nova.link' -WorkingDirectory $Root -WindowStyle Hidden
    Good 'NOVA Link started'
}
Write-Host "   Say ""Nova, what time is it?"" -- the answer comes from the hub. Its log: $Root\data\nova.log"

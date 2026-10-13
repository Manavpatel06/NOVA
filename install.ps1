# NOVA installer -- turns the repo at D:\NOVA into an installed Windows app.
# It creates shortcuts; it never moves or modifies the code.
#
#   .\install.ps1              Start Menu shortcut (search "NOVA" to launch)
#   .\install.ps1 -AutoStart   ...and start NOVA at every login
#   .\install.ps1 -Uninstall   remove both shortcuts
param([switch]$AutoStart, [switch]$Uninstall)

$Root      = "D:\NOVA"
$PyW       = Join-Path $Root ".venv\Scripts\pythonw.exe"
$Py        = Join-Path $Root ".venv\Scripts\python.exe"
$StartMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\NOVA.lnk"
$Startup   = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup\NOVA.lnk"

if ($Uninstall) {
  Remove-Item $StartMenu, $Startup -ErrorAction SilentlyContinue
  Write-Host "NOVA shortcuts removed. The code at $Root is untouched."
  exit 0
}

if (-not (Test-Path $PyW)) {
  Write-Error "Python venv not found at $PyW -- run QUICKSTART.md section 1 first."
  exit 1
}

# One-second dependency guard (the F19 lesson: a broken venv fails silently).
& $Py -m pip check
if ($LASTEXITCODE -ne 0) {
  Write-Warning "pip check reported problems above -- NOVA may still run, but fix these."
}

function New-NovaShortcut([string]$Path) {
  $sh = (New-Object -ComObject WScript.Shell).CreateShortcut($Path)
  $sh.TargetPath       = $PyW          # pythonw = no console window; NOVA lives in the tray
  $sh.Arguments        = "-m nova"
  $sh.WorkingDirectory = $Root
  $sh.WindowStyle      = 7
  $sh.IconLocation     = "$Py,0"
  $sh.Description      = "NOVA voice assistant"
  $sh.Save()
}

New-NovaShortcut $StartMenu
Write-Host "Installed: press Win, type NOVA, Enter. It runs in the tray with no console."
if ($AutoStart) {
  New-NovaShortcut $Startup
  Write-Host "Auto-start at login: ON"
} else {
  Write-Host "Auto-start at login: off (re-run with -AutoStart to enable)."
}
Write-Host "Console mode with visible logs: D:\NOVA\NOVA.bat"

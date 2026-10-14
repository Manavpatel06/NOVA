# NOVA installer -- turns the repo at D:\NOVA into an installed Windows app.
# Safe to re-run any time: it updates the ONE NOVA entry in place, never
# creates duplicates, and removes any stray NOVA shortcuts it finds.
#
# NOVA is a windowed app (2026-10-14): open it from the Start Menu, close
# its window to quit. It does not start at login and never runs hidden.
#
#   .\install.ps1              install/update the Start Menu entry, then open NOVA
#   .\install.ps1 -Uninstall   remove the Start Menu entry
param([switch]$Uninstall)

$Root      = "D:\NOVA"
$Py        = Join-Path $Root ".venv\Scripts\python.exe"
$PyW       = Join-Path $Root ".venv\Scripts\pythonw.exe"
$Exe       = $PyW   # windowless launcher (the renamed NOVA.exe copy failed to start; see APPIFY_PLAN)
$Icon      = Join-Path $Root "assets\nova.ico"
$StartMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\NOVA.lnk"
$RunKey    = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"

# --- cleanup: wipe every previous NOVA entry, then install the one entry ----
# Old installs left shortcuts in several places (Startup, Desktop, numbered
# duplicates like "NOVA (2).lnk"). Sweep them all, every run.
$places = @(
  (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"),
  (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup"),
  [Environment]::GetFolderPath("Desktop"),
  "$env:PUBLIC\Desktop",
  "$env:ProgramData\Microsoft\Windows\Start Menu\Programs"
)
foreach ($dir in $places) {
  Get-ChildItem $dir -Filter "NOVA*.lnk" -ErrorAction SilentlyContinue | ForEach-Object {
    Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue
    if (-not (Test-Path $_.FullName)) { Write-Host "Removed old shortcut: $($_.FullName)" }
  }
}
# Older installs could start NOVA at login; a windowed NOVA never does.
Remove-ItemProperty -Path $RunKey -Name "NOVA" -ErrorAction SilentlyContinue

if ($Uninstall) {
  Remove-Item $StartMenu -ErrorAction SilentlyContinue
  Remove-ItemProperty -Path $RunKey -Name "NOVA" -ErrorAction SilentlyContinue
  Write-Host "NOVA removed from the Start Menu. The code at $Root is untouched."
  exit 0
}

if (-not (Test-Path $PyW)) {
  Write-Error "Python venv not found at $PyW -- run QUICKSTART.md section 1 first."
  exit 1
}

# Remove the experimental renamed launcher from 13 Oct -- it failed to start NOVA.
Remove-Item (Join-Path $Root ".venv\Scripts\NOVA.exe") -ErrorAction SilentlyContinue

# One-second dependency guard (the F19 lesson: a broken venv fails silently).
& $Py -m pip check
if ($LASTEXITCODE -ne 0) {
  Write-Warning "pip check reported problems above -- NOVA may still run, but fix these."
}

# The one shortcut, written to the SAME path every time (updates in place).
$sh = (New-Object -ComObject WScript.Shell).CreateShortcut($StartMenu)
$sh.TargetPath       = $Exe
$sh.Arguments        = "-m nova"
$sh.WorkingDirectory = $Root
$sh.WindowStyle      = 7
$sh.IconLocation     = $Icon
$sh.Description      = "NOVA voice assistant"
$sh.Save()
Write-Host "Installed/updated: press Win, type NOVA, Enter. Close the NOVA window to quit."

Write-Host "Console mode with visible logs: D:\NOVA\NOVA.bat"

# --- start NOVA now and confirm it really came up ---------------------------
$log = Join-Path $Root "data\nova.log"
$before = if (Test-Path $log) { (Get-Item $log).Length } else { 0 }
Start-Process -FilePath $Exe -ArgumentList "-m nova" -WorkingDirectory $Root
Write-Host "Opening NOVA (first start can take ~30 s while it checks your microphone)..."
$ok = $false
for ($i = 0; $i -lt 45 -and -not $ok; $i++) {
  Start-Sleep -Seconds 2
  if (Test-Path $log) {
    $fs = [IO.File]::Open($log, 'Open', 'Read', 'ReadWrite'); $fs.Seek($before, 'Begin') | Out-Null
    $new = (New-Object IO.StreamReader($fs)).ReadToEnd(); $fs.Close()
    if ($new -match "NovaApp ready") { $ok = $true }
    elseif ($new -match "already running") { Write-Host "NOVA was already open -- close its window (or end pythonw.exe in Task Manager), then re-run." -ForegroundColor Yellow; break }
  }
}
if ($ok) { Write-Host "NOVA is open. Say 'Nova, what time is it?' -- or type in its window. Close the window to quit." -ForegroundColor Green }
else { Write-Host "NOVA did not report ready within 90 s. Run D:\NOVA\NOVA.bat to see the error." -ForegroundColor Yellow }

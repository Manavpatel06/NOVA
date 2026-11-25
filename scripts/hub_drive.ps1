<#
.SYNOPSIS
  Connect this laptop to the hub's shared drive (ADR 0022), and optionally back folders up to it every night.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File D:\NOVA\scripts\hub_drive.ps1 -User manav
      Maps H: to \\nova-hub\nova. Windows asks for the shared-drive password once (the one you chose
      with "nova-hub share") and remembers it.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File D:\NOVA\scripts\hub_drive.ps1 -User manav -Backup "C:\Users\manav\Documents","D:\Projects"
      Also asks whether to add a daily 2:30 am task that copies those folders to \\nova-hub\nova\backups\<name>.
      It is a mirror, not a history: a file you delete here is deleted from the backup on the next run.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File D:\NOVA\scripts\hub_drive.ps1 -Remove
      Disconnects the drive, forgets the saved password and removes the backup task. Nothing on the hub is deleted.
#>
[CmdletBinding()]
param(
    [string]$User,
    [ValidatePattern('^[D-Zd-z]$')][string]$Letter = 'H',
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9.-]*$')][string]$Hub = 'nova-hub',
    [string[]]$Backup = @(),
    [switch]$Remove,
    [switch]$RunBackup
)

$ErrorActionPreference = 'Stop'
$Share    = "\\$Hub\nova"
$TaskName = 'NOVA hub backup'
$Skip     = @('.venv', 'venv', 'node_modules', '__pycache__', '.cache', 'cache', 'models', 'updates')

function Say([string]$m)  { Write-Host "==> $m" -ForegroundColor Cyan }
function Good([string]$m) { Write-Host "  OK  $m" -ForegroundColor Green }
function Bad([string]$m)  { Write-Host "  !!  $m" -ForegroundColor Red }

function Get-BackupName([string]$path) {
    $leaf = Split-Path -Path $path -Leaf
    if ([string]::IsNullOrWhiteSpace($leaf) -or $leaf -match ':') { $leaf = 'drive-' + $path.Substring(0, 1) }
    return ($leaf -replace '[^A-Za-z0-9 ._-]', '_')
}

function Invoke-Backup {
    # Runs nightly from the scheduled task, as you, so it uses the saved password. Copies to the
    # share by its \\ name (a mapped letter isn't visible to scheduled tasks).
    $root = Join-Path $Share 'backups'
    if (-not (Test-Path -LiteralPath $root)) { New-Item -ItemType Directory -Path $root | Out-Null }
    $worst = 0
    foreach ($src in $Backup) {
        if (-not (Test-Path -LiteralPath $src -PathType Container)) { Bad "skipped (not found): $src"; continue }
        $dst = Join-Path $root (Get-BackupName $src)
        & robocopy.exe $src $dst /MIR /COPY:DAT /DCOPY:DAT /XJ /R:1 /W:2 /NP /NFL /NDL /XD @Skip | Out-Null
        if ($LASTEXITCODE -gt $worst) { $worst = $LASTEXITCODE }
    }
    # robocopy: 0-7 is success (files copied/extra/mismatched), 8+ means something failed
    exit ([int]($worst -ge 8))
}

if ($RunBackup) { Invoke-Backup }

if ($Remove) {
    Say "Disconnecting $Letter`: and forgetting the hub's password (nothing on the hub is deleted)"
    & net.exe use "$Letter`:" /delete /y 2>$null | Out-Null
    & cmdkey.exe "/delete:$Hub" 2>$null | Out-Null
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Good 'backup task removed'
    }
    Good 'done'
    exit 0
}

if (-not $User) { Bad 'Give your hub user name, e.g.  -User manav  (it is printed by "nova-hub share")'; exit 1 }

# 1. reach the hub (Tailscale must be on)
Say "Looking for $Hub over Tailscale"
if (-not (Test-NetConnection -ComputerName $Hub -Port 445 -InformationLevel Quiet -WarningAction SilentlyContinue)) {
    Bad "Can't reach $Hub on the file-sharing port. Is Tailscale on here, and did you run 'nova-hub share' on the hub?"
    exit 1
}
Good "$Hub answers"

# 2. remember the password (Windows asks for it; it is never seen or stored by NOVA)
$saved = (& cmdkey.exe "/list:$Hub") -join "`n"
if ($saved -notmatch [regex]::Escape($User)) {
    Say "Type the shared-drive password you chose with 'nova-hub share'"
    & cmdkey.exe "/add:$Hub" "/user:$User" '/pass'
    if ($LASTEXITCODE -ne 0) { Bad 'password not saved'; exit 1 }
}

# 3. map the drive
$current = Get-PSDrive -Name $Letter -ErrorAction SilentlyContinue
if ($current -and $current.DisplayRoot -ne $Share) { Bad "$Letter`: is already used for something else; pick another with -Letter"; exit 1 }
if (-not $current) {
    & net.exe use "$Letter`:" $Share /persistent:yes | Out-Null
    if ($LASTEXITCODE -ne 0) { Bad "couldn't connect $Share (wrong password? run with -Remove, then try again)"; exit 1 }
}
Good "$Letter`: is your hub drive ($Share)"

# 4. optional nightly backup
if ($Backup.Count -gt 0) {
    $names = @{}
    foreach ($p in $Backup) {
        if (-not (Test-Path -LiteralPath $p -PathType Container)) { Bad "not a folder: $p"; exit 1 }
        $n = Get-BackupName $p
        if ($names.ContainsKey($n)) { Bad "two folders would back up to the same name '$n'; back up one of them separately"; exit 1 }
        $names[$n] = $p
    }
    Write-Host ''
    Write-Host 'Every night at 2:30 am, these folders will be mirrored to the hub:'
    foreach ($n in $names.Keys) { Write-Host "   $($names[$n])  ->  $Share\backups\$n" }
    Write-Host '(A mirror: files deleted here are deleted from the backup too. Big caches and venvs are skipped.)'
    $answer = Read-Host 'Create this daily backup task? (y/n)'
    if ($answer -notmatch '^(y|yes)$') { Good 'no backup task created'; exit 0 }

    $quoted = ($Backup | ForEach-Object { "'" + ($_ -replace "'", "''") + "'" }) -join ','
    $command = "& '" + ($PSCommandPath -replace "'", "''") + "' -RunBackup -Hub '$Hub' -Backup $quoted"
    $action  = New-ScheduledTaskAction -Execute 'powershell.exe' `
               -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -Command `"$command`""
    $trigger  = New-ScheduledTaskTrigger -Daily -At '2:30am'
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 3)
    $who      = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $who -Force | Out-Null
    Good "backup task '$TaskName' added (runs daily at 2:30 am, or at next login if the laptop was off)"
    Write-Host "   Run it now:  Start-ScheduledTask -TaskName '$TaskName'"
}

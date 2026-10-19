<#
NOVA hub setup for Windows 11 -- turns a freshly reset laptop into NOVA's always-on home.

Run in an ADMIN PowerShell (right-click Start -> Terminal (Admin)):

    irm https://raw.githubusercontent.com/Manavpatel06/NOVA/main/hub/setup_hub.ps1 | iex

Safe to run more than once. What it does:
  1. names the machine "nova-hub" (takes effect after the restart at the end)
  2. never sleeps on the charger -- including with the lid closed
  3. Windows Update restarts only at night (active hours 5 am - 11 pm)
  4. installs Git, Python 3.13, Tailscale and Claude Code (via winget)
  5. downloads NOVA to C:\NOVA
  6. network locked down: Wi-Fi treated as Public (no sharing), SSH reachable ONLY over Tailscale
#>
$ErrorActionPreference = 'Stop'
function Say($t)  { Write-Host "`n== $t" -ForegroundColor Cyan }
function Ok($t)   { Write-Host "   ok  $t" -ForegroundColor Green }
function Warn($t) { Write-Host "   !!  $t" -ForegroundColor Yellow }

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { Write-Host "Open PowerShell as Administrator (right-click Start -> Terminal (Admin)) and run this again."; return }
$build = [int](Get-CimInstance Win32_OperatingSystem).BuildNumber
if ($build -lt 22000) { Write-Host "This script is for Windows 11."; return }

Say "1/6  Naming this machine nova-hub"
if ($env:COMPUTERNAME -ne 'NOVA-HUB') {
    Rename-Computer -NewName 'nova-hub' -Force -WarningAction SilentlyContinue
    Ok "renamed (applies after restart)"
} else { Ok "already nova-hub" }

Say "2/6  Never sleep on the charger, lid can be closed"
powercfg /change standby-timeout-ac 0     | Out-Null
powercfg /change hibernate-timeout-ac 0   | Out-Null
powercfg /change monitor-timeout-ac 0     | Out-Null   # Modern Standby laptops sleep when the screen times out; the closed lid turns the panel off anyway
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0 | Out-Null   # lid closed on charger: do nothing
powercfg /setdcvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 1 | Out-Null   # on battery (charger pulled): sleep, to save it
powercfg /setactive SCHEME_CURRENT | Out-Null
Ok "on charger: never sleeps, lid closed = keep running"

Say "3/6  Windows Update restarts only at night"
$ux = 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings'
New-Item -Path $ux -Force | Out-Null
Set-ItemProperty -Path $ux -Name SmartActiveHoursState -Type DWord -Value 0
Set-ItemProperty -Path $ux -Name ActiveHoursStart -Type DWord -Value 5
Set-ItemProperty -Path $ux -Name ActiveHoursEnd   -Type DWord -Value 23
Ok "active hours 5:00-23:00 -- update restarts happen between 11 pm and 5 am"

Say "4/6  Installing Git, Python, Tailscale, Claude Code (a few minutes)"
if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    Write-Host "winget is missing. Open Microsoft Store, update 'App Installer', then run this again."; return
}
$apps = @(
    @{ id = 'Git.Git';              name = 'Git' },
    @{ id = 'Python.Python.3.13';   name = 'Python 3.13' },
    @{ id = 'Tailscale.Tailscale';  name = 'Tailscale' },
    @{ id = 'Anthropic.ClaudeCode'; name = 'Claude Code' }
)
foreach ($a in $apps) {
    $have = winget list --id $a.id -e --accept-source-agreements 2>$null | Select-String -SimpleMatch $a.id
    if ($have) { Ok "$($a.name) already installed"; continue }
    winget install --id $a.id -e --silent --accept-source-agreements --accept-package-agreements | Out-Null
    if ($LASTEXITCODE -eq 0) { Ok "$($a.name) installed" } else { Warn "$($a.name) install returned $LASTEXITCODE -- run this script again" }
}
# pick up the new PATH entries in this window
$env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')

Say "5/6  Downloading NOVA to C:\NOVA"
if (Test-Path 'C:\NOVA\.git') {
    git -C 'C:\NOVA' pull --ff-only | Out-Null; Ok "updated"
} else {
    git clone https://github.com/Manavpatel06/NOVA.git 'C:\NOVA' | Out-Null; Ok "downloaded"
}
New-Item -ItemType Directory -Force -Path 'C:\NOVA\data' | Out-Null

Say "6/6  Locking the network down"
Get-NetConnectionProfile | Where-Object { $_.InterfaceAlias -notlike '*Tailscale*' } |
    ForEach-Object { Set-NetConnectionProfile -InterfaceIndex $_.InterfaceIndex -NetworkCategory Public }
Ok "home Wi-Fi treated as Public: no file/printer sharing, nothing visible to other devices"
if ((Get-WindowsCapability -Online -Name 'OpenSSH.Server*').State -ne 'Installed') {
    Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0' | Out-Null
}
Set-Service -Name sshd -StartupType Automatic
Start-Service sshd
Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue | Disable-NetFirewallRule
if (-not (Get-NetFirewallRule -Name 'NOVA-SSH-Tailscale' -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -Name 'NOVA-SSH-Tailscale' -DisplayName 'NOVA: SSH over Tailscale only' `
        -Direction Inbound -Protocol TCP -LocalPort 22 -RemoteAddress '100.64.0.0/10' -Action Allow | Out-Null
}
Ok "SSH on, reachable only from your Tailscale devices (100.64.0.0/10)"

Write-Host @"

Done. Three things left, they need you:

  1. Tailscale:  click the Tailscale icon by the clock (or open Tailscale from Start) -> Log in.
                 Use the SAME account on your main laptop and phone.
                 Then in the Tailscale icon menu -> Preferences -> turn on "Run unattended"
                 (keeps the link up even when nobody is signed in to Windows).

  2. Claude:     open a NEW normal PowerShell (not Admin) and run:   claude
                 choose "Claude account with subscription", sign in with your Max account, then type /exit

  3. Battery:    open your laptop maker's app (Lenovo Vantage / MyASUS / HP Support Assistant /
                 Dell Power Manager) and turn on the battery charge limit (~80%) if it has one.

  Then restart once:   Restart-Computer

"@ -ForegroundColor White

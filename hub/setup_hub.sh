#!/usr/bin/env bash
# NOVA hub setup -- turns a fresh Ubuntu laptop into NOVA's always-on home.
#
#   bash ~/NOVA/hub/setup_hub.sh
#
# Safe to run more than once. Run as your normal user (it asks for your
# password for the parts that need sudo). What it does:
#   1. updates Ubuntu and installs the basics (SSH, git, Python, TLP)
#   2. names the machine "nova-hub"
#   3. never sleeps -- not on idle, not when the lid closes
#   4. protects the battery (stops charging at 80% where the laptop supports it)
#   5. installs security updates automatically (reboots at 4:30 am only when needed)
#   6. firewall: nothing reachable from the internet or your Wi-Fi; only
#      your own devices over Tailscale
#   7. installs Tailscale (private link to your laptop + phone) and Claude Code
#   8. keeps NOVA's background jobs running even when nobody is logged in
set -euo pipefail

say()  { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
ok()   { printf '   \033[32mok\033[0m  %s\n' "$*"; }
warn() { printf '   \033[33m!!\033[0m  %s\n' "$*"; }

if [[ $EUID -eq 0 ]]; then
  echo "Run this as your normal user, not with sudo:  bash ~/NOVA/hub/setup_hub.sh"; exit 1
fi
if ! grep -qi ubuntu /etc/os-release; then
  echo "This script is for Ubuntu."; exit 1
fi
sudo -v

say "1/8  Updating Ubuntu and installing basics (a few minutes)"
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -qq
sudo apt-get -y -qq full-upgrade
sudo apt-get -y -qq install openssh-server git curl ca-certificates python3 python3-venv \
  tlp tlp-rdw unattended-upgrades ufw jq htop
ok "packages installed"

say "2/8  Naming this machine nova-hub"
if [[ "$(hostnamectl --static)" != "nova-hub" ]]; then
  sudo hostnamectl set-hostname nova-hub
fi
ok "hostname: $(hostnamectl --static)"

say "3/8  Never sleep (idle or lid closed)"
sudo systemctl mask --quiet sleep.target suspend.target hibernate.target hybrid-sleep.target
sudo mkdir -p /etc/systemd/logind.conf.d
sudo tee /etc/systemd/logind.conf.d/90-nova-hub.conf >/dev/null <<'CONF'
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
IdleAction=ignore
CONF
if command -v gsettings >/dev/null 2>&1 && [[ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ]]; then
  gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing' || true
  gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-battery-type 'nothing' || true
  gsettings set org.gnome.desktop.session idle-delay 300 || true   # screen still turns off after 5 min
fi
ok "sleep disabled; lid can be closed (applies after the reboot at the end)"

say "4/8  Battery care"
sudo tee /etc/tlp.d/90-nova-hub.conf >/dev/null <<'CONF'
# Plugged in 24/7: hold the battery between 75% and 80% (where supported).
START_CHARGE_THRESH_BAT0=75
STOP_CHARGE_THRESH_BAT0=80
START_CHARGE_THRESH_BAT1=75
STOP_CHARGE_THRESH_BAT1=80
CONF
sudo systemctl enable --now tlp >/dev/null 2>&1 || true
if sudo tlp-stat -b 2>/dev/null | grep -qiE "charge_control_end_threshold|stop_charge_thresh.*= *80"; then
  ok "charge limit 80% active"
else
  warn "couldn't confirm an 80% charge limit -- if the BIOS has a 'battery care' or 'charge limit' option, turn it on"
fi

say "5/8  Automatic security updates"
sudo tee /etc/apt/apt.conf.d/52nova-hub >/dev/null <<'CONF'
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:30";
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
CONF
sudo systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true
ok "security updates on; reboots only when an update needs it, at 4:30 am"

say "6/8  Tailscale (private link between your devices)"
if ! command -v tailscale >/dev/null 2>&1; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi
ok "tailscale installed"

say "7/8  Firewall: only your own devices (via Tailscale) can reach this machine"
sudo ufw --force reset >/dev/null
sudo ufw default deny incoming >/dev/null
sudo ufw default allow outgoing >/dev/null
sudo ufw allow in on tailscale0 >/dev/null
sudo ufw --force enable >/dev/null
ok "firewall on"

say "8/8  Claude Code + always-on background jobs"
if ! command -v claude >/dev/null 2>&1 && [[ ! -x "$HOME/.local/bin/claude" ]]; then
  curl -fsSL https://claude.ai/install.sh | bash
fi
grep -q 'HOME/.local/bin' "$HOME/.bashrc" || echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
sudo loginctl enable-linger "$USER"
ok "claude installed: $("$HOME/.local/bin/claude" --version 2>/dev/null || claude --version 2>/dev/null || echo '?')"
ok "background jobs keep running without a login"

mkdir -p "$HOME/NOVA-data"
say "Done. Two sign-ins left (they need you):"
cat <<'NEXT'

  1. Tailscale:   sudo tailscale up --ssh
                  -> open the link it prints, sign in (same account on your laptop and phone)

  2. Claude:      claude
                  -> choose "Claude account with subscription", sign in with your Max account,
                     then type /exit

  Then restart once:   sudo reboot

NEXT

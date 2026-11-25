#!/usr/bin/env bash
# NOVA hub setup -- turns a fresh Ubuntu laptop into NOVA's always-on home.
#
# Command 1 (this script):
#   wget -qO setup_hub.sh https://raw.githubusercontent.com/Manavpatel06/NOVA/master/hub/setup_hub.sh && bash setup_hub.sh
# Command 2 (the sign-ins, when this finishes):
#   nova-hub login
#
# Safe to run more than once. Run as your normal user (it asks for your
# password for the parts that need sudo). What it does:
#   1. updates Ubuntu and installs the basics (SSH, git, Python, TLP)
#   2. names the machine "nova-hub"
#   3. never sleeps -- not on idle, not when the lid closes
#   4. protects the battery (stops charging at 80% where the laptop supports it)
#   5. installs security updates automatically (reboots at 4:30 am only when needed)
#   6. installs Tailscale (private link to your laptop + phone)
#   7. firewall: nothing reachable from the internet or your Wi-Fi; only
#      your own devices over Tailscale
#   8. installs Claude Code and keeps NOVA running even when nobody is logged in
#   9. installs Ollama and NOVA's local model (qwen3.5:2b, on this machine only)
#  10. downloads NOVA, starts the hub service and the nightly memory backup
#  11. sets up the workshop where NOVA builds and tests its own upgrades
set -euo pipefail

say()  { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
ok()   { printf '   \033[32mok\033[0m  %s\n' "$*"; }
warn() { printf '   \033[33m!!\033[0m  %s\n' "$*"; }

if [[ $EUID -eq 0 ]]; then
  echo "Run this as your normal user, not with sudo:  bash setup_hub.sh"; exit 1
fi
if ! grep -qi ubuntu /etc/os-release; then
  echo "This script is for Ubuntu."; exit 1
fi
sudo -v

say "1/11  Updating Ubuntu and installing basics (a few minutes)"
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -qq
sudo apt-get -y -qq full-upgrade
sudo apt-get -y -qq install openssh-server git curl ca-certificates python3 python3-venv \
  tlp tlp-rdw unattended-upgrades ufw jq htop zstd
ok "packages installed"

say "2/11  Naming this machine nova-hub"
if [[ "$(hostnamectl --static)" != "nova-hub" ]]; then
  sudo hostnamectl set-hostname nova-hub
fi
ok "hostname: $(hostnamectl --static)"

say "3/11  Never sleep (idle or lid closed)"
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

say "4/11  Battery care"
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

say "5/11  Automatic security updates"
sudo tee /etc/apt/apt.conf.d/52nova-hub >/dev/null <<'CONF'
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:30";
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
CONF
sudo systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true
ok "security updates on; reboots only when an update needs it, at 4:30 am"

say "6/11  Tailscale (private link between your devices)"
if ! command -v tailscale >/dev/null 2>&1; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi
ok "tailscale installed"

say "7/11  Firewall: only your own devices (via Tailscale) can reach this machine"
sudo ufw --force reset >/dev/null
sudo ufw default deny incoming >/dev/null
sudo ufw default allow outgoing >/dev/null
sudo ufw allow in on tailscale0 >/dev/null
sudo ufw --force enable >/dev/null
ok "firewall on"

say "8/11  Claude Code + always-on background jobs"
if ! command -v claude >/dev/null 2>&1 && [[ ! -x "$HOME/.local/bin/claude" ]]; then
  curl -fsSL https://claude.ai/install.sh | bash
fi
grep -q 'HOME/.local/bin' "$HOME/.bashrc" || echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
sudo loginctl enable-linger "$USER"
ok "claude installed: $("$HOME/.local/bin/claude" --version 2>/dev/null || claude --version 2>/dev/null || echo '?')"
ok "background jobs keep running without a login"

say "9/11  Ollama + NOVA's local model (a few minutes: ~2.7 GB download)"
if ! command -v ollama >/dev/null 2>&1; then
  curl -fsSL https://ollama.com/install.sh | sh
fi
sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo tee /etc/systemd/system/ollama.service.d/90-nova.conf >/dev/null <<'CONF'
# NOVA hub: Ollama stays on this machine only, one model at a time, small context
[Service]
Environment="OLLAMA_HOST=127.0.0.1:11434"
Environment="OLLAMA_MAX_LOADED_MODELS=1"
Environment="OLLAMA_NUM_PARALLEL=1"
Environment="OLLAMA_CONTEXT_LENGTH=4096"
Environment="OLLAMA_KEEP_ALIVE=-1"
CONF
sudo systemctl daemon-reload
sudo systemctl enable --now ollama >/dev/null 2>&1 || true
sudo systemctl restart ollama
for _ in $(seq 1 30); do curl -fsS http://127.0.0.1:11434/api/version >/dev/null 2>&1 && break; sleep 1; done
ollama pull qwen3.5:2b
ok "Ollama running on this machine only (127.0.0.1), model qwen3.5:2b ready"

say "10/11  NOVA: download, hub service, nightly backup"
if [[ -d "$HOME/NOVA/.git" ]]; then
  git -C "$HOME/NOVA" pull --ff-only -q || warn "couldn't update ~/NOVA (local changes?) -- using it as is"
else
  git clone -q https://github.com/Manavpatel06/NOVA.git "$HOME/NOVA"
fi
python3 -m venv "$HOME/NOVA/.venv"
"$HOME/NOVA/.venv/bin/pip" install -q --upgrade pip
"$HOME/NOVA/.venv/bin/pip" install -q "PyYAML==6.0.3"
"$HOME/NOVA/.venv/bin/pip" install -q --no-deps -e "$HOME/NOVA"
mkdir -p "$HOME/.local/bin" "$HOME/.config/systemd/user" "$HOME/NOVA/data"
install -m 755 "$HOME/NOVA/hub/nova-hub" "$HOME/.local/bin/nova-hub"
cp "$HOME"/NOVA/hub/systemd/nova-hub.service "$HOME"/NOVA/hub/systemd/nova-backup.service \
   "$HOME"/NOVA/hub/systemd/nova-backup.timer "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now nova-hub.service nova-backup.timer >/dev/null
ok "NOVA in ~/NOVA; hub service running; memory backup every night at 03:30"

say "11/11  Workshop for NOVA's self-upgrades (a few minutes)"
sudo apt-get -y -qq install gh bubblewrap libportaudio2 libsndfile1 >/dev/null || warn "couldn't install gh/bubblewrap/audio libraries -- upgrades may not work"
if bwrap --ro-bind / / --unshare-all --dev /dev --proc /proc true 2>/dev/null; then
  ok "test sandbox works (upgrades are tested with no network and no access to your files)"
else
  warn "the test sandbox (bubblewrap) doesn't run here -- NOVA won't build upgrades until it does"
fi
FORGE="$HOME/NOVA-forge"
mkdir -p "$FORGE/work"
python3 -m venv "$FORGE/.venv"
"$FORGE/.venv/bin/pip" install -q --upgrade pip
"$FORGE/.venv/bin/pip" install -q numpy PyYAML psutil pytest pytest-asyncio sounddevice soundfile \
  onnxruntime kokoro-onnx faster-whisper || warn "some test packages failed to install -- upgrades may not test"
ok "workshop ready in ~/NOVA-forge (NOVA's tests run here before any upgrade reaches you)"
systemctl --user restart nova-hub.service

say "Done. One more command (sign-ins, they need you):"
cat <<'NEXT'

    source ~/.bashrc && nova-hub login

  It signs in to Tailscale, Claude and GitHub, publishes the hub to your own
  devices, checks every part, and offers to restart. After that, any time:

    nova-hub status    is everything working?
    nova-hub voice     give NOVA on the hub ears and a voice (talk from your phone)

NEXT

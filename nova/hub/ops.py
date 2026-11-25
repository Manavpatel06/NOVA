"""Two things NOVA on the hub may start by voice (2026-11-25), and nothing else:

  * "update": `nova-hub update` (pull the latest NOVA from GitHub, reinstall,
    restart), run as its own transient systemd unit so restarting NOVA's
    service doesn't cut the update off halfway;
  * "reboot": restart the hub computer, through a sudoers rule Manav turns on
    himself with `nova-hub voice-reboot on` (sudo -n: never a password prompt).

Fixed argument lists only: nothing Manav or Claude says becomes part of a
command. Both skills are CONFIRM ("Nova, confirm"). On the pinned process
allowlist (tests/test_no_network_or_process_spawn.py).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

UNIT = "nova-hub-update"


def _commands() -> dict[str, list[str]]:
    hub = str(Path.home() / ".local" / "bin" / "nova-hub")
    return {
        "update": ["systemd-run", "--user", "--collect", f"--unit={UNIT}", "--", hub, "update"],
        "reboot": ["sudo", "-n", "/usr/bin/systemctl", "reboot"],
    }


def start(what: str) -> tuple[bool, str]:
    """Start one of the fixed commands; (started, why not)."""
    argv = list(_commands().get(what) or [])
    if not argv:
        return False, "not something I can do"
    if shutil.which(argv[0]) is None:
        return False, f"{argv[0]} isn't installed"
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, shell=False)
        try:
            _, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            return True, ""        # still going (a reboot in progress)
    except OSError as exc:
        return False, str(exc)
    if proc.returncode != 0:
        msg = (err or b"").decode("utf-8", "replace").strip().splitlines()
        return False, (msg[-1] if msg else f"exit {proc.returncode}")[:160]
    return True, ""

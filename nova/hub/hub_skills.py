"""The hub itself, by voice (2026-11-25): "Nova, how's the hub?", "Nova,
restart yourself".

Manav: "if I want to do something in my hub, I want to be able to manage
that as well ... with voice". These are hub-only skills (local_skills of the
hub's NOVA), never offered on the laptop:

  * hub_status (READ): up time, battery and charging (the HP runs on its
    battery when the power blinks), temperature, memory, free space, the
    connected apps, whether the laptop is connected, and this month's API
    credit. Read from /proc and /sys -- nothing is run.
  * restart_nova (ACT): NOVA on the hub restarts itself (systemd brings it
    back), e.g. after `nova-hub connect` or when something seems stuck.
  * update_nova (CONFIRM): `nova-hub update` -- the latest NOVA from GitHub.
  * reboot_hub (CONFIRM): restart the hub computer (after `nova-hub
    voice-reboot on`). Both through nova/hub/ops.py's fixed commands.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from nova.security.tiers import Tier


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _duration(secs: float) -> str:
    mins = int(secs // 60)
    days, rem = divmod(mins, 1440)
    hours, mins = divmod(rem, 60)
    if days:
        return f"{days} day{'s' if days != 1 else ''} {hours} hour{'s' if hours != 1 else ''}"
    if hours:
        return f"{hours} hour{'s' if hours != 1 else ''} {mins} minute{'s' if mins != 1 else ''}"
    return f"{mins} minute{'s' if mins != 1 else ''}"


def facts(root: str = "/") -> dict[str, Any]:
    out: dict[str, Any] = {}
    up = _read("/proc/uptime").split()
    if up:
        out["up_s"] = float(up[0])
    mem = {}
    for line in _read("/proc/meminfo").splitlines():
        k, _, v = line.partition(":")
        if v.strip().endswith("kB"):
            mem[k] = int(v.split()[0]) * 1024
    if "MemTotal" in mem and "MemAvailable" in mem:
        out["mem_used_pct"] = round(100 * (1 - mem["MemAvailable"] / mem["MemTotal"]))
    try:
        du = shutil.disk_usage(Path.home())
        out["disk_free_gb"] = round(du.free / 1e9)
    except OSError:
        pass
    for bat in sorted(Path("/sys/class/power_supply").glob("BAT*")):
        cap = _read(str(bat / "capacity"))
        if cap.isdigit():
            out["battery_pct"] = int(cap)
            out["battery_status"] = _read(str(bat / "status")).lower()
            break
    temps = []
    for z in Path("/sys/class/thermal").glob("thermal_zone*"):
        t = _read(str(z / "temp"))
        if t.lstrip("-").isdigit():
            temps.append(int(t) / 1000)
    if temps:
        out["temp_c"] = round(max(temps))
    try:
        out["load"] = round(os.getloadavg()[0], 1)
    except (OSError, AttributeError):  # AttributeError: not on Windows (tests run there too)
        pass
    return out


def status_text(f: dict[str, Any], *, connectors: Any = None, devices: Any = None, spend: Any = None) -> str:
    parts = []
    if "up_s" in f:
        parts.append(f"The hub's been up {_duration(f['up_s'])}")
    if "battery_pct" in f:
        state = f.get("battery_status", "")
        tail = ", charging" if state == "charging" else (", on battery -- is it unplugged?" if state == "discharging" else ", plugged in")
        parts.append(f"battery {f['battery_pct']} percent{tail}")
    if "temp_c" in f:
        parts.append(f"{f['temp_c']} degrees" + (" (running hot)" if f["temp_c"] >= 85 else ""))
    if "mem_used_pct" in f:
        parts.append(f"memory {f['mem_used_pct']} percent used")
    if "disk_free_gb" in f:
        parts.append(f"{f['disk_free_gb']} gigabytes free")
    text = ", ".join(parts) + "." if parts else "The hub is running."
    if devices is not None:
        text += " Your laptop is " + ("connected." if devices.online("laptop") else "not connected.")
    if connectors is not None:
        try:
            st = connectors.status()
        except Exception:  # noqa: BLE001
            st = []
        if st:
            ready = [s["name"] for s in st if s.get("state") == "ready"]
            broken = [s["name"] for s in st if s.get("state") == "failed"]
            if ready:
                text += f" Connected apps: {', '.join(ready)}."
            if broken:
                text += f" Not working: {', '.join(broken)} (nova-hub connect test)."
    if spend is not None:
        try:
            st = spend.status()
            text += f" API credit: {st['usd']:.2f} of {st['budget_usd']:.0f} dollars used this month."
        except Exception:  # noqa: BLE001
            pass
    return text


def make(*, connectors: Any = None, devices: Any = None, spend: Callable[[], Any] | None = None,
         restart: Callable[[float], None] | None = None) -> dict[str, Any]:
    from nova.skills.registry import Reply, SkillSpec

    async def hub_status(ctx):
        f = await asyncio.to_thread(facts)
        return Reply(text=status_text(f, connectors=connectors, devices=devices, spend=spend() if spend else None))

    async def restart_nova(ctx):
        if restart is None:
            return Reply(text="I can't restart myself here.", success=False)
        restart(6.0)
        return Reply(text="Restarting. I'll be back in about twenty seconds.")

    async def update_nova(ctx):
        from nova.hub import ops

        ok, why = await asyncio.to_thread(ops.start, "update")
        if not ok:
            return Reply(text=f"I couldn't start the update ({why}). Run nova-hub update at the hub.", success=False)
        return Reply(text="Updating from GitHub now. I'll restart when it's done -- give me a minute.")

    async def reboot_hub(ctx):
        from nova.hub import ops

        ok, why = await asyncio.to_thread(ops.start, "reboot")
        if not ok:
            hint = " Turn it on once with nova-hub voice-reboot on." if "password" in why or "sudo" in why else ""
            return Reply(text=f"I couldn't restart the hub ({why}).{hint}", success=False)
        return Reply(text="Restarting the hub. I'll be back in about two minutes.")

    return {
        "update_nova": SkillSpec(
            name="update_nova", tier=Tier.CONFIRM,
            examples=("update yourself from github", "pull the latest nova", "update nova on the hub"),
            description="Install the latest NOVA from GitHub on the hub and restart (not building new skills: "
                        "that's upgrade_nova).",
            trusts_input=False, handler=update_nova, confirm_phrase="update NOVA on the hub from GitHub"),
        "reboot_hub": SkillSpec(
            name="reboot_hub", tier=Tier.CONFIRM,
            examples=("reboot the hub", "restart the hub computer", "restart the old laptop"),
            description="Restart the hub computer itself (NOVA is away ~2 minutes). Not the main laptop.",
            trusts_input=False, handler=reboot_hub, confirm_phrase="restart the hub computer"),
        "hub_status": SkillSpec(
            name="hub_status", tier=Tier.READ,
            examples=("how's the hub", "hub status", "is the hub okay", "how's the hub's battery"),
            description="How the hub (the always-on HP laptop NOVA lives on) is doing: up time, battery, "
                        "temperature, memory, disk, connected apps, the laptop link and API credit used.",
            trusts_input=False, handler=hub_status),
        "restart_nova": SkillSpec(
            name="restart_nova", tier=Tier.ACT,
            examples=("restart yourself", "restart nova on the hub", "reboot your service"),
            description="Restart NOVA's own service on the hub (comes back in ~20 s). Not the laptop.",
            trusts_input=False, handler=restart_nova, confirm_phrase="restart NOVA on the hub"),
    }

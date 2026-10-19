"""Screen brightness -- set / step / read, via WMI (root/WMI).

`WmiMonitorBrightness` / `WmiMonitorBrightnessMethods.WmiSetBrightness` only
exist for panels the OS drives directly (a laptop's built-in screen). An
external monitor (or a laptop with its lid shut on a dock) reports "Not
supported" -- the script prints `unsupported` and NOVA says so plainly
instead of pretending.
"""
from __future__ import annotations

import asyncio
import re

from nova.log import get_logger
from nova.security.tiers import Tier
from nova.skills.registry import Reply, SkillCtx, skill
from nova.skills.volume import LEVEL, LEVEL_OR_EXTREME, PERCENT, parse_level, parse_step
from nova.system import winops

_log = get_logger("skills.display")

# op: get | set (value 0..100) | step (signed delta). Prints the brightness
# after the change, or `unsupported`. Same stdin layout rule as volume.py:
# a blank line after every multi-line statement, none inside one.
_BRIGHTNESS_PS = r"""$op = [string]$NovaArgs.op
$value = 0
if ($NovaArgs.value) { $value = [int]$NovaArgs.value }
try {
    $cur = Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightness -ErrorAction Stop | Where-Object { $_.Active } | Select-Object -First 1
    $set = Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods -ErrorAction Stop | Where-Object { $_.Active } | Select-Object -First 1
} catch {
    if ("$($_.Exception.Message)" -match 'not supported') { Write-Output 'unsupported'; exit 0 }
    throw
}

if (-not $cur -or -not $set) { Write-Output 'unsupported'; exit 0 }
$level = [int]$cur.CurrentBrightness
if ($op -eq 'set') { $level = $value } elseif ($op -eq 'step') { $level = $level + $value }
$level = [Math]::Max(0, [Math]::Min(100, $level))
if ($op -ne 'get') { $null = $set.WmiSetBrightness(1, [byte]$level) }
Write-Output $level
"""


async def _brightness(op: str, value: int = 0) -> int | None:
    """Brightness after the operation, or None if this display can't be
    controlled (external monitor)."""
    out = await asyncio.to_thread(winops.powershell, _BRIGHTNESS_PS, timeout=20.0, op=op, value=value)
    last = (out or "").strip().splitlines()[-1:] or [""]
    if last[0].strip() == "unsupported":
        return None
    m = re.fullmatch(r"\s*(\d{1,3})\s*", last[0])
    if m is None:
        raise winops.WinOpsError(f"unexpected brightness output {out[-80:]!r}")
    return int(m.group(1))


_UNSUPPORTED = "I can't control the brightness on this display -- it looks like an external monitor."


async def _change(op: str, value: int) -> Reply:
    try:
        now = await _brightness(op, value)
    except (winops.WinOpsUnavailable, winops.WinOpsError, ValueError) as exc:
        _log.warning("brightness %s failed: %s", op, exc)
        return Reply(text="I couldn't change the brightness.", success=False)
    if now is None:
        return Reply(text=_UNSUPPORTED, success=False)
    return Reply(text=f"Brightness is at {now}.")


_BR = r"(?:the )?(?:screen |display |monitor )?brightness"
_SCREEN = r"(?:the |my )?(?:screen|display|monitor)"
_BIT = r"(?: (?:a )?(?:bit|little|tad|touch|notch)(?: more)?)?"
_BY = rf"(?: by (?P<amount>{LEVEL}){PERCENT})?"


@skill(
    name="set_brightness",
    tier=Tier.ACT,
    examples=["set brightness to 60", "brightness 60 percent", "set the screen brightness to 80", "max brightness"],
    description="Set the built-in screen's brightness to an exact level. One parameter: level (0-100).",
    trusts_input=False,
    patterns=[
        rf"(?:set|change|put|turn|make|bring) {_BR} (?:to |at |up to |down to )?(?P<level>{LEVEL_OR_EXTREME}){PERCENT}",
        rf"{_BR} (?:to |at )?(?P<level>{LEVEL_OR_EXTREME}){PERCENT}",
        r"(?P<level>max|maximum|full|min|minimum) (?:screen )?brightness",
    ],
)
async def set_brightness(ctx: SkillCtx, level: str) -> Reply:
    n = parse_level(level)
    if n is None or not 0 <= n <= 100:
        return Reply(text="Brightness goes from 0 to 100 -- what level do you want?", success=False)
    return await _change("set", n)


@skill(
    name="brightness_up",
    tier=Tier.ACT,
    examples=["brightness up", "make the screen brighter", "turn up the brightness", "increase brightness"],
    description="Raise the built-in screen's brightness by a step (default 10). Optional parameter: amount.",
    trusts_input=False,
    patterns=[
        rf"{_BR} up{_BIT}{_BY}",
        rf"turn {_BR} up{_BIT}{_BY}",
        rf"turn up {_BR}{_BIT}{_BY}",
        rf"(?:raise|increase|boost) {_BR}{_BIT}{_BY}",
        rf"(?:brighten|light up) {_SCREEN}{_BIT}{_BY}",
        rf"(?:make )?(?:{_SCREEN} |it )?(?:a bit |a little )?brighter{_BIT}{_BY}",
    ],
)
async def brightness_up(ctx: SkillCtx, amount: str = "") -> Reply:
    step = parse_step(amount)
    if step is None:
        return Reply(text="I can raise it by 1 to 100 -- how much?", success=False)
    return await _change("step", step)


@skill(
    name="brightness_down",
    tier=Tier.ACT,
    examples=["brightness down", "dim the screen", "turn down the brightness", "make the screen dimmer"],
    description="Lower the built-in screen's brightness by a step (default 10). Optional parameter: amount.",
    trusts_input=False,
    patterns=[
        rf"{_BR} down{_BIT}{_BY}",
        rf"turn {_BR} down{_BIT}{_BY}",
        rf"turn down {_BR}{_BIT}{_BY}",
        rf"(?:lower|decrease|reduce|drop) {_BR}{_BIT}{_BY}",
        rf"dim {_SCREEN}{_BIT}{_BY}",
        rf"(?:make )?(?:{_SCREEN} |it )?(?:a bit |a little )?(?:dimmer|darker){_BIT}{_BY}",
        rf"{_SCREEN} is too bright",
    ],
)
async def brightness_down(ctx: SkillCtx, amount: str = "") -> Reply:
    step = parse_step(amount)
    if step is None:
        return Reply(text="I can lower it by 1 to 100 -- how much?", success=False)
    return await _change("step", -step)


@skill(
    name="get_brightness",
    tier=Tier.READ,
    examples=["what's the brightness", "brightness level", "how bright is the screen"],
    description="Report the built-in screen's current brightness level.",
    trusts_input=False,
    patterns=[
        rf"(?:what'?s|what is|whats) {_BR}(?: level)?(?: at| set to)?(?: right now)?",
        rf"(?:current )?{_BR} level",
        r"(?:the )?current (?:screen )?brightness",
        rf"how bright is {_SCREEN}(?: right now)?",
    ],
)
async def get_brightness(ctx: SkillCtx) -> Reply:
    try:
        now = await _brightness("get")
    except (winops.WinOpsUnavailable, winops.WinOpsError, ValueError) as exc:
        _log.warning("brightness get failed: %s", exc)
        return Reply(text="I couldn't read the brightness.", success=False)
    if now is None:
        return Reply(text=_UNSUPPORTED, success=False)
    return Reply(text=f"Brightness is at {now}.")

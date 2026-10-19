"""Battery level / charging state (READ), via psutil.sensors_battery()."""
from __future__ import annotations

import asyncio

from nova.log import get_logger
from nova.security.tiers import Tier
from nova.skills.registry import Reply, SkillCtx, skill

_log = get_logger("skills.battery")


def _read_battery():
    import psutil  # lazy: keeps import cost off the cold path

    return psutil.sensors_battery()


def _duration(secs: int) -> str:
    minutes = max(1, round(secs / 60))
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours} hour{'s' if hours != 1 else ''} {mins} minute{'s' if mins != 1 else ''}"
    if hours:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{mins} minute{'s' if mins != 1 else ''}"


@skill(
    name="battery",
    tier=Tier.READ,
    examples=["how much battery do I have", "battery level", "am I plugged in", "is my laptop charging"],
    description="Report the battery percentage, whether it's plugged in, and time left on battery.",
    trusts_input=False,
    patterns=[
        r"(?:how much|what'?s (?:my |the )?|what is (?:my |the )?|whats (?:my |the )?)?(?:battery|charge)(?: do i have| left| is left| level| percentage| status| life)?(?: left)?",
        r"how(?:'?s| is) (?:my |the )?battery(?: doing| looking)?",
        r"(?:how much )?battery (?:do i have|have i got)(?: left)?",
        r"(?:check|show) (?:my |the )?battery(?: level| status)?",
        r"am i (?:plugged in|charging|on battery|on power)",
        r"is (?:my |the )?(?:laptop|computer|pc|battery|charger) (?:plugged in|charging)",
        r"is (?:the |my )?charger (?:connected|plugged in|working)",
        r"how long (?:will|does) (?:my |the )?battery last",
        r"how much (?:battery )?time (?:do i have )?left(?: on (?:my |the )?battery)?",
    ],
)
async def battery(ctx: SkillCtx) -> Reply:
    try:
        info = await asyncio.to_thread(_read_battery)
    except Exception as exc:  # noqa: BLE001 -- psutil missing / sensor error -> honest refusal
        _log.warning("battery read failed: %s", exc)
        return Reply(text="I couldn't read the battery.", success=False)
    if info is None:
        return Reply(text="I can't find a battery -- this machine seems to run on mains power only.", success=False)
    pct = int(round(info.percent))
    if info.power_plugged:
        if pct >= 100:
            return Reply(text="Battery's full, and you're plugged in.")
        return Reply(text=f"Battery's at {pct} percent, and you're plugged in and charging.")
    secs = info.secsleft
    if isinstance(secs, (int, float)) and 0 < secs < 7 * 24 * 3600:
        return Reply(text=f"Battery's at {pct} percent, on battery power -- about {_duration(int(secs))} left.")
    return Reply(text=f"Battery's at {pct} percent, on battery power.")

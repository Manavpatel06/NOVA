"""Screenshot (ACT): Win+PrintScreen, which Windows saves to
Pictures\\Screenshots by itself -- no image library, no file I/O here."""
from __future__ import annotations

import asyncio

from nova.log import get_logger
from nova.security.tiers import Tier
from nova.skills.registry import Reply, SkillCtx, skill
from nova.system import winops

_log = get_logger("skills.screenshot")


@skill(
    name="screenshot",
    tier=Tier.ACT,
    examples=["take a screenshot", "screenshot", "capture the screen"],
    description="Take a full-screen screenshot, saved to the Pictures\\Screenshots folder.",
    trusts_input=False,
    patterns=[
        r"(?:take|grab|get|make|snap) (?:a |me a )?(?:screen ?shot|screen capture|screen grab)(?: of (?:my |the )?(?:screen|display|desktop))?",
        r"(?:screen ?shot|screen capture|screen grab)(?: (?:my |the )?(?:screen|display|desktop))?",
        r"(?:capture|snap) (?:my |the )?(?:screen|display|desktop)",
        r"print screen",
    ],
)
async def screenshot(ctx: SkillCtx) -> Reply:
    try:
        await asyncio.to_thread(winops.press_keys, "win", "snapshot")
    except (winops.WinOpsUnavailable, winops.WinOpsError, KeyError) as exc:
        _log.warning("screenshot failed: %s", exc)
        return Reply(text="I couldn't take a screenshot.", success=False)
    return Reply(text="Screenshot saved to your Screenshots folder.")

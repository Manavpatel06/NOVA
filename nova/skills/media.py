"""media -- ACT tier: control whatever is playing (2026-10-15).

Windows' global media keys, sent with `winops.press_keys`, so this works
for Spotify, YouTube in the browser, VLC, ... -- whichever app currently
owns the system media session. Nothing here knows or asks which app that
is.

Honest limits:
  - Windows has ONE play/pause key, a toggle. "pause" when nothing is
    playing will start playback instead; NOVA can't tell the difference
    without a much heavier media-session query, so it doesn't pretend to.
  - Bare "stop" IS claimed (the lead's call: "stop" -> media stop). If a
    "stop talking" / cancel skill ever lands, the two will collide and the
    router will ask -- move bare "stop" to whichever matters more then.
"""

from __future__ import annotations

import asyncio

from nova.log import get_logger
from nova.security.tiers import Tier
from nova.skills.registry import Reply, SkillCtx, skill
from nova.system import winops

logger = get_logger("skills.media")

_WHAT = r"(?: (?:the |this |my )?(?:music|song|track|video|playback|audio|podcast|spotify|youtube|it))?"


async def _press(key: str, done: str) -> Reply:
    try:
        await asyncio.to_thread(winops.press_keys, key)
    except winops.WinOpsUnavailable:
        return Reply(text="I can only control media on Windows.", success=False)
    except (winops.WinOpsError, OSError) as exc:
        logger.error("media key %s failed: %s", key, type(exc).__name__)
        return Reply(text="I couldn't reach the media controls.", success=False)
    return Reply(text=done)


@skill(
    name="media_play_pause",
    tier=Tier.ACT,
    examples=["pause the music", "resume the music", "play music", "unpause"],
    description="Play or pause whatever media is playing (Spotify, YouTube, ...). Toggle; no parameters.",
    trusts_input=False,
    patterns=[
        rf"(?P<action>pause){_WHAT}",
        rf"(?P<action>resume|unpause|continue playing|keep playing){_WHAT}",
        r"(?P<action>continue) (?:the |my )?(?:music|song|video|playback)",
        r"(?P<action>play)(?: (?:the |some |my )?(?:music|song|video|playback))?",
        r"(?P<action>hit play|press play|hit pause|press pause)",
    ],
)
async def media_play_pause(ctx: SkillCtx, action: str = "pause") -> Reply:
    """Toggle play/pause; the spoken reply follows what was asked for."""
    asked_pause = "pause" in action and "unpause" not in action
    return await _press("media_play_pause", "Paused." if asked_pause else "Playing.")


@skill(
    name="media_next",
    tier=Tier.ACT,
    examples=["next song", "skip this song", "next track", "skip"],
    description="Skip to the next track in whatever media is playing. No parameters.",
    trusts_input=False,
    patterns=[
        r"(?:play )?(?:the )?next(?: (?:song|track|video|one|episode))?",
        r"skip(?: (?:this|the|that|to the next))?(?: (?:song|track|video|one))?",
        r"skip ahead|go to the next (?:song|track|video)",
    ],
)
async def media_next(ctx: SkillCtx) -> Reply:
    """Next track."""
    return await _press("media_next", "Skipping.")


@skill(
    name="media_previous",
    tier=Tier.ACT,
    examples=["previous track", "previous song", "play the last song again", "back one song"],
    description="Go back to the previous track in whatever media is playing. No parameters.",
    trusts_input=False,
    patterns=[
        r"(?:play )?(?:the )?previous(?: (?:song|track|video|one|episode))?",
        r"(?:play )?(?:the )?last (?:song|track|video)(?: again)?",
        r"(?:go )?back (?:a|one) (?:song|track|video)",
        r"go (?:back )?to the (?:previous|last) (?:song|track|video)",
        r"(?:replay|restart) (?:the |this )?(?:song|track)",
    ],
)
async def media_previous(ctx: SkillCtx) -> Reply:
    """Previous track (most players restart the current track if it's past a few seconds)."""
    return await _press("media_prev", "Going back.")


@skill(
    name="media_stop",
    tier=Tier.ACT,
    examples=["stop the music", "stop playback", "stop playing music"],
    description="Stop whatever media is playing. No parameters.",
    trusts_input=False,
    patterns=[
        r"stop (?:the |this |my )?(?:music|song|track|video|playback|audio|podcast)",
        r"stop playing(?: (?:the |this )?(?:music|song|video))?",
        r"stop",
    ],
)
async def media_stop(ctx: SkillCtx) -> Reply:
    """Stop playback."""
    return await _press("media_stop", "Stopped.")

"""time — READ tier. No parameters, no side effects (docs/CONTRACTS.md T0.5)."""

from __future__ import annotations

from datetime import datetime

from nova.security.tiers import Tier
from nova.skills.registry import Reply, SkillCtx, skill


@skill(
    name="time",
    tier=Tier.READ,
    examples=[
        "what time is it",
        "what's the time",
        "tell me the time",
        "current time",
    ],
    description="Report the current local time. No parameters.",
    # 2026-10-15: exact shapes. Only ever about "time" -- dates belong to
    # nova/skills/date.py, and neither skill's patterns mention the other's word.
    patterns=[
        r"(?:what's|what is|whats) the (?:current )?time(?: right now| now)?",
        r"what time is it(?: right now| now| here)?",
        r"(?:tell me |give me )?(?:the )?(?:current )?time",
        r"(?:do you (?:know|have) |have you got |got )(?:the time|what time it is)",
        r"what time(?: is it)? do you have",
    ],
    trusts_input=False,
)
async def time_skill(ctx: SkillCtx) -> Reply:
    """Report the current local time."""
    now = datetime.now().strftime("%I:%M %p").lstrip("0")
    return Reply(text=f"It's {now}.")

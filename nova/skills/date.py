"""date -- READ tier: today's date (2026-10-15).

"what's the date" -> "It's Wednesday, October 15th." Also answers
"what day is it tomorrow" / "what was yesterday's date". The clock is the
machine's local time, the same source nova/skills/time.py reads; time-of-day
questions stay with that skill (its patterns only ever mention "time").
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from nova.security.tiers import Tier
from nova.skills.registry import Reply, SkillCtx, skill

_WHEN = r"(?: (?P<which>today|tomorrow|yesterday))?"


def ordinal(n: int) -> str:
    """1 -> '1st', 2 -> '2nd', 11 -> '11th', 22 -> '22nd'."""
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def spoken_date(d: date) -> str:
    """'Wednesday, October 15th' -- no year; a spoken date rarely needs one."""
    return f"{d.strftime('%A')}, {d.strftime('%B')} {ordinal(d.day)}"


def _today() -> date:
    return datetime.now().date()


@skill(
    name="date",
    tier=Tier.READ,
    examples=["what's the date", "what day is it", "what's today's date", "what's the date today"],
    description="Say today's date (or tomorrow's/yesterday's). No parameters needed.",
    trusts_input=False,
    patterns=[
        rf"(?:what's|what is|whats|tell me) (?:the )?(?:date|day)(?: is it)?{_WHEN}",
        rf"(?:what's|what is|whats|tell me) (?P<which>today|tomorrow|yesterday)'?s? date",
        r"(?:what's|what is|whats) the date (?:going to be |gonna be )?(?P<which>tomorrow)",
        rf"what (?:day|date) (?:of the week |of the month )?is it{_WHEN}",
        r"what (?:day|date) (?:is|was|will be) (?P<which>today|tomorrow|yesterday)",
        r"(?P<which>today|tomorrow|yesterday)'?s date",
        r"(?:the )?date(?: today)?",
        r"what's today|what is today|whats today",
        r"do you know (?:the date|what day it is|what the date is)",
    ],
)
async def date_skill(ctx: SkillCtx, which: str = "today") -> Reply:
    """Report the date for `which` (today / tomorrow / yesterday)."""
    words = set((which or "today").lower().replace("'s", "").split())
    when = next((w for w in ("tomorrow", "yesterday", "today") if w in words), None)
    if when is None and words <= {"date", "day", "current", "todays", "is", "it", "now"}:
        when = "today"  # a fuzzy match's leftover filler ("date"), not a different day
    today = _today()
    if when == "tomorrow":
        return Reply(text=f"Tomorrow is {spoken_date(today + timedelta(days=1))}.")
    if when == "yesterday":
        return Reply(text=f"Yesterday was {spoken_date(today - timedelta(days=1))}.")
    if when is None:
        return Reply(text="I can tell you today's, tomorrow's or yesterday's date.", success=False)
    return Reply(text=f"It's {spoken_date(today)}.")

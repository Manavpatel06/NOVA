"""Reminders and timers -- kept and fired by the hub (2026-11-24).

"Nova, remind me to call mom at 7" / "set a timer for 10 minutes": the time
is understood here (nova.when, local clock), and the hub stores it and makes
the iPhone ring when it's due (nova.hub.reminders / notify). The hub is the
one that's always on, so a reminder still fires when this laptop is closed.

Urgent reminders ("remind me urgently ...", or urgent="yes" from the brain)
ring through silent mode and repeat every minute until tapped.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from nova.brain.hub_tier import HubUnavailable, hub_post
from nova.log import get_logger
from nova.security.tiers import Tier
from nova.skills.registry import Reply, SkillCtx, skill
from nova.when import parse_duration, parse_when, say_when

logger = get_logger("skills.reminders")

NO_HUB = "Reminders live on the hub, and I can't reach it right now."
_WHEN = r"(?:in|at|tomorrow|tonight|today|this) [\w :.'-]+?"
_YES = {"yes", "true", "1", "urgent", "urgently", "important"}


def _note(alerts_ready: bool) -> str:
    return "" if alerts_ready else " Your phone alerts aren't set up yet, so I can't ring you."


@skill(
    name="remind_me",
    tier=Tier.ACT,
    examples=["remind me to call mom at 7", "remind me in 20 minutes to take the laundry out",
              "remind me tomorrow at 9 to submit the assignment"],
    description=("Set a reminder that rings Manav's iPhone at a time, even if the laptop is off. Params: text (what "
                 "to remind him of, e.g. 'call mom'), when (as he said it: 'in 20 minutes', 'at 5:30 pm', "
                 "'tomorrow at 9', 'tonight at 8'), urgent ('yes' only if he says it's urgent or important: it then "
                 "rings through silent mode until he taps it), call ('yes' only if he asks NOVA to call/phone him "
                 "then: NOVA also phones him, when phone calls are set up)."),
    trusts_input=False,
    patterns=[
        # "remind me tomorrow at 9 to ..." first, so its time isn't read as the task
        rf"remind me (?P<when>{_WHEN}) (?:to |about )(?P<text>.+)",
        rf"remind me (?:to |about )?(?P<text>.+?) (?P<when>{_WHEN})",
    ],
)
async def remind_me(ctx: SkillCtx, text: str, when: str, urgent: str = "", call: str = "") -> Reply:
    now = datetime.now()
    due = parse_when(when, now)
    if due is None:
        return Reply(text="When should I remind you?", success=False)
    text = " ".join(text.strip().split())[:200]
    if not text:
        return Reply(text="What should I remind you about?", success=False)
    is_urgent = urgent.strip().lower() in _YES
    is_call = call.strip().lower() in _YES | {"call", "phone"}
    try:
        out = await hub_post(ctx.config, ctx.egress, "/v1/reminders",
                             {"text": text, "due": due.timestamp(), "urgent": is_urgent, "call": is_call})
    except HubUnavailable as exc:
        logger.warning("remind_me: hub unavailable (%s)", exc)
        return Reply(text=NO_HUB, success=False)
    logger.info("remind_me: set for %s (%s)", due.isoformat(timespec="minutes"), "urgent" if is_urgent else "normal")
    how = "call you" if is_call else "remind you"
    return Reply(text=f"Okay, I'll {how} {say_when(due, now)}.{_note(out.get('alerts_ready', True))}")


@skill(
    name="set_timer",
    tier=Tier.ACT,
    examples=["set a timer for 10 minutes", "start a 5 minute timer", "timer for an hour and a half"],
    description=("Start a timer that rings Manav's iPhone when it's done. One param: duration, as he said it "
                 "('10 minutes', 'an hour and a half')."),
    trusts_input=False,
    patterns=[
        r"(?:set|start)(?: me)? (?:a |an )?timer for (?P<duration>.+)",
        r"(?:set|start)(?: me)? (?:a |an )?(?P<duration>.+?) timer",
        r"timer for (?P<duration>.+)",
    ],
)
async def set_timer(ctx: SkillCtx, duration: str) -> Reply:
    secs = parse_duration(duration.replace("-", " "))
    if secs is None:
        return Reply(text="How long should the timer be?", success=False)
    now = datetime.now()
    due = now + timedelta(seconds=secs)
    label = " ".join(duration.strip().split())[:60]
    try:
        out = await hub_post(ctx.config, ctx.egress, "/v1/reminders",
                             {"text": f"Your {label} timer is done.", "due": due.timestamp(), "urgent": False})
    except HubUnavailable as exc:
        logger.warning("set_timer: hub unavailable (%s)", exc)
        return Reply(text=NO_HUB, success=False)
    return Reply(text=f"Timer set for {label}.{_note(out.get('alerts_ready', True))}")


@skill(
    name="list_reminders",
    tier=Tier.READ,
    examples=["what are my reminders", "list my reminders", "do I have any reminders"],
    description="Say Manav's upcoming reminders and timers (the next few). No params.",
    trusts_input=False,
    patterns=[
        r"(?:what are|list|read|tell me) (?:all )?my (?:reminders|timers)",
        r"(?:do i have|are there) any (?:reminders|timers)(?: set)?",
        r"what (?:reminders|timers) do i have",
    ],
)
async def list_reminders(ctx: SkillCtx) -> Reply:
    try:
        out = await hub_post(ctx.config, ctx.egress, "/v1/reminders/list", {})
    except HubUnavailable:
        return Reply(text=NO_HUB, success=False)
    items = out.get("reminders") if isinstance(out, dict) else None
    if not items:
        return Reply(text="You don't have any reminders.")
    now = datetime.now()
    parts = []
    for r in items[:3]:
        try:
            parts.append(f"{str(r['text'])[:80]} {say_when(datetime.fromtimestamp(float(r['due'])), now)}")
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    more = f", and {len(items) - 3} more" if len(items) > 3 else ""
    return Reply(text=f"You have {len(items)}: " + "; ".join(parts) + more + ".")


@skill(
    name="cancel_reminders",
    tier=Tier.ACT,
    examples=["cancel all my reminders", "clear my reminders", "cancel my timer"],
    description="Cancel all of Manav's upcoming reminders and timers. No params.",
    trusts_input=False,
    patterns=[
        r"(?:cancel|clear|delete|remove) (?:all )?(?:of )?my (?:reminders|timers|timer|reminder)",
        r"(?:cancel|stop) (?:the|my) timer",
    ],
)
async def cancel_reminders(ctx: SkillCtx) -> Reply:
    try:
        out = await hub_post(ctx.config, ctx.egress, "/v1/reminders/cancel", {"id": "all"})
    except HubUnavailable:
        return Reply(text=NO_HUB, success=False)
    n = int(out.get("cancelled", 0)) if isinstance(out, dict) else 0
    return Reply(text="Done, nothing's left." if n else "There weren't any to cancel.")

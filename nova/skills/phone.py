"""NOVA phones Manav (ADR 0025): "Nova, call me" -- from NOVA's own Twilio
number, through the hub. Needs `nova-hub phone` set up on the hub; to talk
on the call, Manav types his PIN first."""
from __future__ import annotations

from nova.brain.hub_tier import HubUnavailable, hub_post
from nova.log import get_logger
from nova.security.tiers import Tier
from nova.skills.registry import Reply, SkillCtx, skill

logger = get_logger("skills.phone")


@skill(
    name="call_me",
    tier=Tier.ACT,
    examples=["call me", "call my phone", "give me a call"],
    description=("Phone Manav's own phone right now from NOVA's number and say a short message first; he can "
                 "then talk to NOVA on the call. Params: message (what to say when he picks up). For a call at a "
                 "later time use remind_me with call='yes'."),
    trusts_input=False,
    patterns=[r"(?:call|phone|ring) me(?: now| please)?", r"give me a (?:call|ring)"],
)
async def call_me(ctx: SkillCtx, message: str = "You asked me to call you.") -> Reply:
    try:
        await hub_post(ctx.config, ctx.egress, "/v1/phone/call", {"message": message[:300]}, timeout_s=25.0)
    except HubUnavailable as exc:
        logger.warning("call_me: %s", exc)
        if "set up" in str(exc):
            return Reply(text="Phone calls aren't set up yet. Run 'nova-hub phone' on the hub.", success=False)
        return Reply(text="I couldn't place the call right now.", success=False)
    return Reply(text="Calling you now.")

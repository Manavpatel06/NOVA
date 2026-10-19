"""remember / recall / forget -- NOVA's long-term memory as tools (2026-10-19).

The brain calls `remember` when Manav states something lasting ("I use
Brave", "my sister's in India") or asks it to ("Nova, remember that ...").
Stored in plain files (nova/memory/store.py), never in Claude's chat memory.
"""
from __future__ import annotations

import asyncio

from nova.log import get_logger
from nova.memory.store import default_store
from nova.security.tiers import Tier
from nova.skills.registry import Reply, SkillCtx, skill

logger = get_logger("skills.memory")


@skill(
    name="remember",
    tier=Tier.ACT,
    examples=["remember that I use brave", "remember my sister lives in india"],
    description=("Save one lasting fact about Manav to NOVA's long-term memory (facts.json). Use for stable "
                 "preferences, people, places, routines, projects -- not one-off requests, and never health, "
                 "finances, passwords or other sensitive details. One parameter: fact (str), a short third-person "
                 "sentence, e.g. 'Manav uses the Brave browser'."),
    trusts_input=False,
    patterns=[r"remember (?:that )?(?P<fact>.{3,200})"],
    confirm_phrase="remember that {fact}",
)
async def remember(ctx: SkillCtx, fact: str) -> Reply:
    try:
        saved, created = await asyncio.to_thread(default_store().remember, fact)
    except ValueError as exc:
        return Reply(text=f"I couldn't save that: {exc}.", success=False)
    except OSError as exc:
        logger.error("remember: write failed (%s)", exc)
        return Reply(text="I couldn't write to my memory file.", success=False)
    logger.info("remember: %s fact %s", "new" if created else "updated", saved.id)
    return Reply(text="Got it, I'll remember that." if created else "Updated what I remember.")


@skill(
    name="recall",
    tier=Tier.READ,
    examples=["what do you remember about me", "what do you know about my sister"],
    description=("Search NOVA's long-term memory for facts about a topic. One parameter: topic (str); use 'everything' "
                 "for a general summary. Returns the matching remembered facts."),
    trusts_input=False,
    patterns=[r"what do you (?:remember|know) about (?P<topic>.{2,80})"],
)
async def recall(ctx: SkillCtx, topic: str) -> Reply:
    q = "" if topic.strip().lower() in ("me", "everything", "anything", "all") else topic
    hits = await asyncio.to_thread(default_store().search, q, 8)
    if not hits:
        return Reply(text="I don't have anything saved about that yet.")
    return Reply(text="Here's what I remember: " + "; ".join(f.text for f in hits) + ".")


@skill(
    name="forget",
    tier=Tier.ACT,
    examples=["forget that I use brave", "forget what I said about my sister"],
    description="Remove facts from NOVA's long-term memory that match what Manav describes. One parameter: what (str).",
    trusts_input=False,
    patterns=[r"forget (?:that |about |what i said about )?(?P<what>.{3,200})"],
    confirm_phrase="forget {what}",
)
async def forget(ctx: SkillCtx, what: str) -> Reply:
    gone = await asyncio.to_thread(default_store().forget, what)
    if not gone:
        return Reply(text="I couldn't find that in my memory.", success=False)
    logger.info("forget: removed %d fact(s)", len(gone))
    return Reply(text="Done, I've forgotten that." if len(gone) == 1 else f"Done, I've forgotten {len(gone)} things.")

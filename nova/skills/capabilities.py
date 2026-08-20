"""capabilities -- READ tier, self-knowledge (docs/M2_OPEN_BRAIN_PLAN.md T2.1).

Answers "what can you do" from the *live* skill registry -- never a
hardcoded list. A hardcoded second list of skill names/descriptions would
drift the moment someone adds a skill, which is exactly the kind of
duplication the constitution's "one source of truth" rule exists to rule
out. This module reads `nova.skills.registry.all_skills()` at call time --
the same live view `nova/brain/router.py` reads to route requests -- there
is no other place a skill's existence is recorded, so this is the only
input the reply can drift out of sync with.

The reply is spoken aloud, so it is built as one short natural sentence
rather than a bulleted dump, and it degrades gracefully as the registry
grows: past a handful of skills it names a few by example and gives a
count for the rest, rather than reading an ever-longer list aloud every
time someone asks.
"""

from __future__ import annotations

from nova.security.tiers import Tier
from nova.skills.registry import Reply, SkillCtx, all_skills, skill

# How many example phrasings to read aloud in full before summarizing the
# rest as a count -- keeps the spoken reply one short sentence even once
# the registry has grown well past M0's two skills (module docstring's
# "write for the ear" note).
_MAX_SPOKEN_EXAMPLES = 4


def _speakable_examples() -> list[str]:
    """One representative phrase per *other* registered skill.

    This skill's own examples are meta-questions about capabilities, not
    things to read back as one of "the things I can do", so `capabilities`
    excludes itself by name. Sorted by skill name for a stable spoken
    order -- otherwise a dict-iteration-order change could reshuffle the
    reply between calls for no reason a listener could tell.
    """
    others = sorted(
        (spec for spec in all_skills().values() if spec.name != "capabilities"),
        key=lambda spec: spec.name,
    )
    return [spec.examples[0] for spec in others]


def _build_reply() -> str:
    examples = _speakable_examples()
    if not examples:
        # Only reachable with a fixture registry that registers nothing but
        # this skill -- never in production, where time/open_app always
        # exist too -- but an honest answer beats a crash either way.
        return "I don't have any other skills registered right now."

    shown = examples[:_MAX_SPOKEN_EXAMPLES]
    remaining = len(examples) - len(shown)

    quoted = [f'"{example}"' for example in shown]
    listed = quoted[0] if len(quoted) == 1 else ", ".join(quoted[:-1]) + f", or {quoted[-1]}"

    if remaining > 0:
        plural = "s" if remaining != 1 else ""
        return (
            f"You can ask me things like {listed}, plus {remaining} other "
            f"thing{plural} besides -- just ask and I'll tell you if I can help."
        )
    return f"You can ask me things like {listed}."


@skill(
    name="capabilities",
    tier=Tier.READ,
    examples=[
        "what can you do",
        "what can you help me with",
        "what are your skills",
        "what are you capable of",
        "what do you know how to do",
        "help",
        "help me",
        "list your skills",
        "what commands do you understand",
    ],
    description="Report what NOVA can currently do, read live from the skill registry. No parameters.",
    trusts_input=False,
)
async def capabilities(ctx: SkillCtx) -> Reply:
    """Report what NOVA can currently do, read live from the skill registry."""
    return Reply(text=_build_reply())

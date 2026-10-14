"""Contract: nova/skills/capabilities.py (T2.1) — self-knowledge, READ tier.

Four things the module's own docstring promises and this file pins:

  1. It reads the *live* registry (`nova.skills.registry.all_skills()`) at
     call time -- never a second, hand-maintained list. Proven both
     directions: a fake skill added to the registry appears in the reply,
     and removing it makes it disappear again -- not just "it appeared once".
  2. It excludes itself by name, so "capabilities" never lists its own
     meta-examples as one of "the things I can do".
  3. Past four other skills it names four by example and summarizes the
     rest as a count, so the spoken reply stays one short sentence no
     matter how large the registry grows.
  4. All nine of its own registered phrasings route to it, above the
     confidence floor, through the real Router against the real registry.

`_speakable_examples()`/`_build_reply()` are private module functions, but
this module is a thin, order-sensitive string builder with no side effects
-- testing it directly (in addition to the end-to-end path) is the only way
to pin the exact "four then a count" shape without depending on exactly
which other skills happen to be registered in production.
"""
from __future__ import annotations

import nova.skills.capabilities as capabilities_mod
from nova.brain.router import Router
from nova.config import Config
from nova.security.audit import AuditLog
from nova.security.ladder import Ladder
from nova.security.tiers import Tier
from nova.skills.capabilities import _build_reply, _speakable_examples, capabilities
from nova.skills.registry import Reply, SkillCtx, SkillSpec, get_skill


async def _noop_handler(ctx):
    return Reply(text="noop")


def _fake_spec(name: str, example: str, tier: Tier = Tier.READ) -> SkillSpec:
    return SkillSpec(description="test skill", name=name, tier=tier, examples=(example,), trusts_input=False, handler=_noop_handler)


class _UnusedLadder:
    """Placeholder for tests that call the handler directly, bypassing
    SkillSpec.invoke() -- ctx.ladder is never read on this path."""

    async def authorize(self, skill, params):
        raise AssertionError("ctx.ladder.authorize() was called but this test never expected it to be")


# ---------------------------------------------------------------------------
# 1. Reads the live registry -- proven in both directions
# ---------------------------------------------------------------------------


def test_a_fake_skill_added_to_the_registry_appears_in_the_reply(monkeypatch):
    fixture = {
        "other_skill": _fake_spec("other_skill", "juggle the quantum flux capacitor"),
        "capabilities": _fake_spec("capabilities", "what can you do"),
    }
    monkeypatch.setattr(capabilities_mod, "all_skills", lambda: fixture)

    reply = _build_reply()

    assert "quantum flux capacitor" in reply


def test_removing_that_same_fake_skill_makes_it_disappear_again(monkeypatch):
    """The other half of the liveness proof: presence isn't a fluke of
    string construction -- absence from the registry means absence from the
    reply, in the same process, same test run."""
    with_it = {
        "other_skill": _fake_spec("other_skill", "juggle the quantum flux capacitor"),
        "capabilities": _fake_spec("capabilities", "what can you do"),
    }
    without_it = {
        "capabilities": _fake_spec("capabilities", "what can you do"),
    }

    monkeypatch.setattr(capabilities_mod, "all_skills", lambda: with_it)
    reply_with = _build_reply()
    monkeypatch.setattr(capabilities_mod, "all_skills", lambda: without_it)
    reply_without = _build_reply()

    assert "quantum flux capacitor" in reply_with
    assert "quantum flux capacitor" not in reply_without


def test_a_registry_with_nothing_but_itself_gives_an_honest_fallback(monkeypatch):
    fixture = {"capabilities": _fake_spec("capabilities", "what can you do")}
    monkeypatch.setattr(capabilities_mod, "all_skills", lambda: fixture)

    reply = _build_reply()

    assert "don't have any other skills" in reply


# ---------------------------------------------------------------------------
# 2. Excludes itself by name
# ---------------------------------------------------------------------------


def test_excludes_its_own_examples_by_name(monkeypatch):
    fixture = {
        "capabilities": _fake_spec("capabilities", "a distinctive unique meta phrase xyz"),
        "other_skill": _fake_spec("other_skill", "do the other thing"),
    }
    monkeypatch.setattr(capabilities_mod, "all_skills", lambda: fixture)

    examples = _speakable_examples()
    reply = _build_reply()

    assert "a distinctive unique meta phrase xyz" not in examples
    assert "a distinctive unique meta phrase xyz" not in reply
    assert "do the other thing" in reply


def test_excludes_itself_against_the_real_live_registry():
    import nova.skills  # noqa: F401  (import triggers discover_skills())

    own_spec = get_skill("capabilities")
    assert own_spec is not None

    examples = _speakable_examples()

    for own_example in own_spec.examples:
        assert own_example not in examples


# ---------------------------------------------------------------------------
# 3. Caps at four named examples, then "plus N other things besides"
# ---------------------------------------------------------------------------


def _fixture_with_n_other_skills(n: int) -> dict:
    fixture = {"capabilities": _fake_spec("capabilities", "what can you do")}
    for i in range(n):
        letter = chr(ord("a") + i)
        fixture[f"skill_{letter}"] = _fake_spec(f"skill_{letter}", f"do thing {letter}")
    return fixture


def test_four_or_fewer_skills_lists_all_of_them_with_no_remaining_count(monkeypatch):
    monkeypatch.setattr(capabilities_mod, "all_skills", lambda: _fixture_with_n_other_skills(4))

    reply = _build_reply()

    for letter in "abcd":
        assert f"do thing {letter}" in reply
    assert "plus" not in reply
    assert "besides" not in reply


def test_more_than_four_skills_names_four_then_says_plus_n_other_things_besides(monkeypatch):
    monkeypatch.setattr(capabilities_mod, "all_skills", lambda: _fixture_with_n_other_skills(7))

    reply = _build_reply()

    # Sorted by skill name (skill_a..skill_g) -> first four shown are a-d.
    for letter in "abcd":
        assert f"do thing {letter}" in reply
    for letter in "efg":
        assert f"do thing {letter}" not in reply
    assert "plus 3 other things besides" in reply


def test_exactly_one_remaining_uses_the_singular_thing_not_things(monkeypatch):
    monkeypatch.setattr(capabilities_mod, "all_skills", lambda: _fixture_with_n_other_skills(5))

    reply = _build_reply()

    assert "plus 1 other thing besides" in reply
    assert "1 other things" not in reply


def test_reply_stays_a_single_spoken_sentence_even_with_a_large_registry(monkeypatch):
    """Spoken output: no newlines, and it doesn't balloon into a bulleted
    dump no matter how many skills are registered."""
    monkeypatch.setattr(capabilities_mod, "all_skills", lambda: _fixture_with_n_other_skills(50))

    reply = _build_reply()

    assert "\n" not in reply
    assert "plus 46 other things besides" in reply


# ---------------------------------------------------------------------------
# 4. All nine registered phrasings route to it, above the floor, through the
# real Router against the real, live registry -- not a fixture standing in
# for it.
# ---------------------------------------------------------------------------


def test_all_nine_registered_phrasings_route_to_capabilities_above_the_floor():
    import nova.skills  # noqa: F401  (import triggers discover_skills())

    own_spec = get_skill("capabilities")
    assert own_spec is not None
    assert len(own_spec.examples) == 9, (
        "capabilities' own example count changed -- update this test's "
        "expectation, this isn't a hardcoded duplicate of the registry"
    )

    router = Router()  # real registry, no skills= override
    floor = router._confidence_floor

    for phrase in own_spec.examples:
        match = router.route(phrase)
        assert match.spec is own_spec, f"{phrase!r} did not route to capabilities (got {match.spec})"
        assert match.confidence >= floor, f"{phrase!r} scored {match.confidence}, below the floor {floor}"
        assert match.clarification is None


def test_an_unrelated_phrase_does_not_route_to_capabilities():
    """Negative check: a phrase clearly aimed at a different skill must not
    land on capabilities just because capabilities' examples are broad
    ('help', 'what can you do')."""
    import nova.skills  # noqa: F401

    router = Router()
    own_spec = get_skill("capabilities")

    for phrase in ["open notepad", "what time is it", "launch calculator"]:
        match = router.route(phrase)
        assert match.spec is not own_spec, f"{phrase!r} unexpectedly routed to capabilities"


# ---------------------------------------------------------------------------
# End to end: real handler, real ladder, READ tier, audited
# ---------------------------------------------------------------------------


async def test_handler_reports_something_derived_from_the_real_registry():
    import nova.skills  # noqa: F401

    ctx = SkillCtx(config=Config(), ladder=_UnusedLadder())
    reply = await capabilities(ctx)

    assert reply.success is True
    assert reply.text
    assert "\n" not in reply.text


async def test_end_to_end_through_the_ladder_is_read_tier_allowed_and_audited(tmp_path):
    import nova.skills  # noqa: F401

    audit = AuditLog(path=tmp_path / "audit.log")
    ladder = Ladder(audit)
    ctx = SkillCtx(config=Config(), ladder=ladder)
    spec = get_skill("capabilities")
    assert spec is not None
    assert spec.tier is Tier.READ

    reply = await spec.invoke(ctx)

    assert reply.success is True
    status = audit.verify()
    assert status.ok is True
    assert status.entries == 1

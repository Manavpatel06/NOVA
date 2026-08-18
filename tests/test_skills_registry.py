"""Contract: docs/CONTRACTS.md T0.5 — skill registry.

    @skill(name=..., tier=Tier..., examples=[...], trusts_input=False)
    async def handler(ctx: SkillCtx, **params) -> Reply: ...

Import-time discovery of nova/skills/*.py is the *only* path a skill's
tier/examples/trusts_input reach the router/ladder — no hand-wiring.
Duplicate names raise loudly at import time, never silently overwrite.

"The injection suite enumerates trusts_input=True skills and blocks any
that lacks hostile-content tests" (CONTRACTS.md) describes the *test
suite's* job, not a runtime check inside registry.py — nova/skills/
registry.py itself has no such gate (confirmed by reading it: `skill()`
only validates tier type, non-empty examples, and duplicate names). So
test_every_trusts_input_true_skill_has_a_paired_injection_test_module below
IS that enumeration mechanism. It is currently vacuous (M0 ships zero
trusts_input=True skills) — it will start doing real work the moment a
mail/calendar-style skill lands, and it's written now so that landing is a
paired-file requirement, not an afterthought.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nova.security.tiers import Tier
from nova.skills.registry import (
    Reply,
    SkillError,
    SkillSpec,
    _reset_registry_for_tests,
    all_skills,
    get_skill,
    skill,
)

TESTS_DIR = Path(__file__).resolve().parent


async def _dummy_handler(ctx, **params):
    return Reply(text="ok")


@pytest.fixture
def clean_registry():
    """Snapshot and restore the module-global registry so a test exercising
    duplicate-name detection or a throwaway skill doesn't poison every other
    test (or the real app) that imports nova.skills afterward."""
    import nova.skills.registry as registry_mod

    snapshot = dict(registry_mod._REGISTRY)
    yield
    registry_mod._REGISTRY.clear()
    registry_mod._REGISTRY.update(snapshot)


# ---------------------------------------------------------------------------
# M0's real skills are actually discovered
# ---------------------------------------------------------------------------


def test_time_and_open_app_are_both_discovered():
    import nova.skills  # noqa: F401  (import triggers discover_skills())

    names = set(all_skills())
    assert {"time", "open_app"} <= names


def test_time_skill_is_read_tier():
    spec = get_skill("time")
    assert spec is not None
    assert spec.tier is Tier.READ
    assert spec.examples
    assert spec.trusts_input is False


def test_open_app_skill_is_act_tier():
    spec = get_skill("open_app")
    assert spec is not None
    assert spec.tier is Tier.ACT
    assert spec.examples
    assert spec.trusts_input is False


def test_unknown_skill_name_returns_none():
    assert get_skill("this_skill_does_not_exist") is None


# ---------------------------------------------------------------------------
# Duplicate names: hard error, never a silent overwrite
# ---------------------------------------------------------------------------


def test_duplicate_skill_name_raises_and_keeps_the_first_registration(clean_registry):
    @skill(name="__test_dup__", tier=Tier.READ, examples=["x"], trusts_input=False)
    async def first(ctx, **params):
        return Reply(text="first")

    with pytest.raises(SkillError, match="__test_dup__"):
        @skill(name="__test_dup__", tier=Tier.READ, examples=["y"], trusts_input=False)
        async def second(ctx, **params):
            return Reply(text="second")

    spec = get_skill("__test_dup__")
    assert spec is not None
    assert spec.handler is first  # the first registration was never overwritten


def test_skill_decorator_rejects_a_non_tier_value(clean_registry):
    with pytest.raises(SkillError):
        @skill(name="__test_bad_tier__", tier="read", examples=["x"], trusts_input=False)  # type: ignore[arg-type]
        async def handler(ctx, **params):
            return Reply(text="x")


def test_skill_decorator_rejects_empty_examples(clean_registry):
    with pytest.raises(SkillError):
        @skill(name="__test_no_examples__", tier=Tier.READ, examples=[], trusts_input=False)
        async def handler(ctx, **params):
            return Reply(text="x")


# ---------------------------------------------------------------------------
# Only the decorator + directory scan populates the registry
# ---------------------------------------------------------------------------


def test_a_plain_function_not_decorated_does_not_appear_in_the_registry():
    async def undecorated(ctx, **params):
        return Reply(text="sneaky")

    assert "undecorated" not in all_skills()
    assert get_skill("undecorated") is None


def test_all_skills_returns_a_copy_not_the_live_registry():
    """Mutating the returned dict must not corrupt the registry — routers
    and the ladder read this, and a caller mutating it by accident must not
    be able to add/remove skills for everyone else."""
    snapshot = all_skills()
    snapshot["__injected__"] = SkillSpec(
        name="__injected__", tier=Tier.READ, examples=("x",), trusts_input=False,
        handler=_dummy_handler,
    )
    assert "__injected__" not in all_skills()


# ---------------------------------------------------------------------------
# trusts_input=True skills must be paired with a hostile-content test module
# (CONTRACTS.md's "injection suite enumerates... and blocks" — this test IS
# that mechanism).
# ---------------------------------------------------------------------------


def test_every_trusts_input_true_skill_has_a_paired_injection_test_module():
    import nova.skills  # noqa: F401

    missing = []
    for name, spec in all_skills().items():
        if not spec.trusts_input:
            continue
        candidates = [
            TESTS_DIR / f"test_skill_{name}_injection.py",
            TESTS_DIR / f"test_{name}_injection.py",
        ]
        if not any(c.exists() for c in candidates):
            missing.append((name, [str(c) for c in candidates]))

    assert not missing, (
        f"trusts_input=True skill(s) with no paired hostile-content test module: {missing}"
    )


def test_no_m0_skill_currently_declares_trusts_input_true():
    """Pin the current state explicitly so the test above's vacuous-pass
    doesn't go unnoticed: M0 ships two skills, both trusts_input=False. The
    day this flips, the test above starts doing real enforcement."""
    import nova.skills  # noqa: F401

    trusting = {name for name, spec in all_skills().items() if spec.trusts_input}
    assert trusting == set()

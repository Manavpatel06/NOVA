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

from nova.config import Config
from nova.security.tiers import Tier
from nova.security.untrusted import Untrusted
from nova.skills.registry import (
    Reply,
    SkillCtx,
    SkillError,
    SkillSpec,
    _find_untrusted,
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
    @skill(name="__test_dup__", tier=Tier.READ, examples=["x"], description="test skill", trusts_input=False)
    async def first(ctx, **params):
        return Reply(text="first")

    with pytest.raises(SkillError, match="__test_dup__"):
        @skill(name="__test_dup__", tier=Tier.READ, examples=["y"], description="test skill", trusts_input=False)
        async def second(ctx, **params):
            return Reply(text="second")

    spec = get_skill("__test_dup__")
    assert spec is not None
    assert spec.handler is first  # the first registration was never overwritten


def test_skill_decorator_rejects_a_non_tier_value(clean_registry):
    with pytest.raises(SkillError):
        @skill(name="__test_bad_tier__", tier="read", examples=["x"], description="test skill", trusts_input=False)  # type: ignore[arg-type]
        async def handler(ctx, **params):
            return Reply(text="x")


def test_skill_decorator_rejects_empty_examples(clean_registry):
    with pytest.raises(SkillError):
        @skill(name="__test_no_examples__", tier=Tier.READ, examples=[], description="test skill", trusts_input=False)
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
    snapshot["__injected__"] = SkillSpec(description="test skill", 
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


# ---------------------------------------------------------------------------
# F23: layer 2 of docs/SECURITY_SPEC.md §1.3 -- SkillSpec.invoke()'s own
# runtime guard, independent of whatever guard the caller (today, only the
# router) may or may not have already run. Landed at 953ebfd, previously
# untested. The assertion that matters in every test below is not just
# "raises TypeError" -- a guard that raised *after* dispatching to the
# handler would still pass a test that only checks for the exception. Every
# test here also asserts the handler never ran, and the ladder/audit-log
# guard tests assert authorize() was never reached either, matching
# registry.py's own comment that this must run before Ladder.authorize()
# specifically because authorize() records params into the audit log.
# ---------------------------------------------------------------------------


class _NeverCalledLadder:
    """Fails the test if authorize() is ever reached. The guard must run
    BEFORE ladder authorization -- reaching the ladder with an Untrusted
    value still in `params` would leak it into the audit log via
    Ladder._record before the TypeError is ever raised."""

    async def authorize(self, spec, params):
        pytest.fail("ctx.ladder.authorize() was called -- the Untrusted guard did not run first")


def _make_probe_spec(name: str, handler_ran: list) -> SkillSpec:
    async def handler(ctx, **params):
        handler_ran.append(params)
        return Reply(text="ok")

    return SkillSpec(description="test skill", name=name, tier=Tier.READ, examples=("probe",), trusts_input=False, handler=handler)


async def test_invoke_rejects_a_top_level_untrusted_parameter_before_the_handler_runs(clean_registry):
    handler_ran: list = []
    spec = _make_probe_spec("untrusted_probe_top_level", handler_ran)
    ctx = SkillCtx(config=Config(), ladder=_NeverCalledLadder())

    with pytest.raises(TypeError):
        await spec.invoke(ctx, text=Untrusted("attacker-controlled body", source="mail.body"))

    assert handler_ran == [], "handler ran despite an Untrusted parameter -- layer 2 did not block dispatch"


async def test_invoke_rejects_untrusted_nested_inside_a_dict_parameter(clean_registry):
    """_find_untrusted's own docstring: a shallow check would pass
    params={"opts": {"app": Untrusted(...)}} straight through -- the same
    container shape that made the asdict() bypass (F12) serious."""
    handler_ran: list = []
    spec = _make_probe_spec("untrusted_probe_nested_dict", handler_ran)
    ctx = SkillCtx(config=Config(), ladder=_NeverCalledLadder())

    with pytest.raises(TypeError):
        await spec.invoke(ctx, opts={"app": Untrusted("attacker body", source="mail.body")})

    assert handler_ran == []


async def test_invoke_rejects_untrusted_nested_inside_a_list_parameter(clean_registry):
    handler_ran: list = []
    spec = _make_probe_spec("untrusted_probe_nested_list", handler_ran)
    ctx = SkillCtx(config=Config(), ladder=_NeverCalledLadder())

    with pytest.raises(TypeError):
        await spec.invoke(ctx, items=["clean", Untrusted("attacker body", source="mail.body")])

    assert handler_ran == []


async def test_invoke_rejects_untrusted_nested_several_containers_deep(clean_registry):
    handler_ran: list = []
    spec = _make_probe_spec("untrusted_probe_deep", handler_ran)
    ctx = SkillCtx(config=Config(), ladder=_NeverCalledLadder())

    with pytest.raises(TypeError):
        await spec.invoke(ctx, opts={"items": [{"nested": Untrusted("deep attacker body", source="web.fetch")}]})

    assert handler_ran == []


async def test_invoke_allows_ordinary_trusted_parameters_through_to_the_handler(tmp_path, clean_registry):
    """Negative control: the guard must not block ordinary parameters, or
    every clean skill call would break, not just hostile ones. Uses a real
    Ladder + AuditLog (not a stand-in) so this also proves the guard doesn't
    interfere with the real authorize()-then-handler path."""
    from nova.security.audit import AuditLog
    from nova.security.ladder import Ladder

    handler_ran: list = []
    spec = _make_probe_spec("clean_probe", handler_ran)
    audit = AuditLog(path=tmp_path / "audit.log")
    ladder = Ladder(audit)
    ctx = SkillCtx(config=Config(), ladder=ladder)

    reply = await spec.invoke(ctx, text="an ordinary trusted string", count=3)

    assert reply.success is True
    assert handler_ran == [{"text": "an ordinary trusted string", "count": 3}]


# ---------------------------------------------------------------------------
# F29: _find_untrusted missed Untrusted values used as dict KEYS, not just
# values. Untrusted defines __hash__, so it's perfectly legal as a dict key
# -- params={"filters": {Untrusted(...): "x"}} reached the handler with the
# key intact, unflagged and un-audited, because the original implementation
# only ever recursed into value.items()'s second element. Found by
# nova-security, reproduced independently before the fix landed (46e6be9)
# and re-confirmed against the fixed code below, same non-vacuity discipline
# as the F22/F23 pins.
#
# No test for an Untrusted key at the TOP level of `params`: handler(ctx,
# **params) requires string keys, so Python itself rejects that shape before
# any of our code runs. A test asserting that would pin CPython's behavior,
# not ours -- exactly the kind of test that looks like coverage and isn't.
# The real gap was always in a NESTED dict, which has no such protection.
# ---------------------------------------------------------------------------


def test_find_untrusted_detects_a_value_sitting_at_a_dict_key():
    u = Untrusted("attacker body", source="mail.body")
    assert _find_untrusted({"k": u}, "") == "k"


def test_find_untrusted_detects_untrusted_used_as_a_dict_key():
    """The exact bypass nova-security demonstrated: pre-fix, this returned
    None -- the key was walked right past."""
    u = Untrusted("attacker body", source="mail.body")
    hit = _find_untrusted({u: "harmless"}, "")
    assert hit is not None


def test_find_untrusted_detects_untrusted_used_as_a_key_nested_inside_a_dict():
    u = Untrusted("attacker body", source="mail.body")
    hit = _find_untrusted({"filters": {u: "x"}}, "")
    assert hit is not None


async def test_invoke_rejects_untrusted_used_as_a_dict_key_nested_inside_a_parameter(clean_registry):
    """The invoke()-level version of the same bypass: a skill-parameter
    binder that only checked values would let this through to the handler,
    unaudited, exactly like F12's asdict() bypass let content out through a
    shape nobody had checked."""
    handler_ran: list = []
    spec = _make_probe_spec("untrusted_probe_dict_key", handler_ran)
    ctx = SkillCtx(config=Config(), ladder=_NeverCalledLadder())
    hostile = Untrusted("attacker-controlled filter", source="mail.body")

    with pytest.raises(TypeError):
        await spec.invoke(ctx, filters={hostile: "x"})

    assert handler_ran == [], "handler ran with an Untrusted dict KEY still in params"

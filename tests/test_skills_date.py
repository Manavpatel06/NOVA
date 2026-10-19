"""date (nova/skills/date.py) -- READ tier -- and its border with time.py.

Time-independent: the clock is monkeypatched to fixed dates, and the one
real-clock test asserts only the reply's shape."""
from __future__ import annotations

import re
from datetime import date

import pytest

import nova.skills  # noqa: F401
import nova.skills.date as date_mod
from nova.brain.router import Router
from nova.config import Config
from nova.security.audit import AuditLog
from nova.security.ladder import Ladder
from nova.security.tiers import Tier
from nova.skills.registry import SkillCtx, get_skill


@pytest.fixture
def ctx(tmp_path):
    return SkillCtx(config=Config(), ladder=Ladder(AuditLog(path=tmp_path / "audit.log")))


@pytest.fixture(scope="module")
def router():
    return Router()


@pytest.mark.parametrize("n, s", [(1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"), (11, "11th"), (12, "12th"),
                                  (13, "13th"), (21, "21st"), (22, "22nd"), (23, "23rd"), (30, "30th"), (31, "31st")])
def test_ordinal(n, s):
    assert date_mod.ordinal(n) == s


def test_spoken_date():
    assert date_mod.spoken_date(date(2026, 10, 15)) == "Thursday, October 15th"
    assert date_mod.spoken_date(date(2025, 10, 15)) == "Wednesday, October 15th"


@pytest.mark.parametrize("text, which", [
    ("what's the date", None),
    ("what's the date?", None),
    ("what day is it", None),
    ("what's today's date", "today"),
    ("what is the date today", "today"),
    ("today's date", "today"),
    ("what day is it tomorrow", "tomorrow"),
    ("what's tomorrow's date", "tomorrow"),
    ("what day was yesterday", "yesterday"),
    ("what day of the week is it", None),
    ("date", None),
    ("can you tell me the date please", None),
])
def test_date_routes(router, text, which):
    m = router.route(text)
    assert m.spec is not None and m.spec.name == "date", m.clarification
    assert m.params.get("which") == which


@pytest.mark.parametrize("text", ["what time is it", "what's the time", "what is the time", "time",
                                  "tell me the time", "current time", "what time is it now",
                                  "do you know what time it is", "whats the time"])
def test_time_questions_go_to_time_not_date(router, text):
    m = router.route(text)
    assert m.spec is not None and m.spec.name == "time", m.clarification


def test_date_is_read_tier():
    assert get_skill("date").tier is Tier.READ


async def test_reply_for_today_tomorrow_yesterday(ctx, monkeypatch):
    monkeypatch.setattr(date_mod, "_today", lambda: date(2025, 10, 15))
    spec = get_skill("date")
    assert (await spec.invoke(ctx)).text == "It's Wednesday, October 15th."
    assert (await spec.invoke(ctx, which="tomorrow")).text == "Tomorrow is Thursday, October 16th."
    assert (await spec.invoke(ctx, which="yesterday")).text == "Yesterday was Tuesday, October 14th."


async def test_month_and_year_rollover(ctx, monkeypatch):
    monkeypatch.setattr(date_mod, "_today", lambda: date(2026, 12, 31))
    assert (await get_skill("date").invoke(ctx, which="tomorrow")).text == "Tomorrow is Friday, January 1st."


async def test_fuzzy_leftover_filler_still_means_today(ctx, monkeypatch):
    monkeypatch.setattr(date_mod, "_today", lambda: date(2025, 10, 15))
    assert (await get_skill("date").invoke(ctx, which="date")).text == "It's Wednesday, October 15th."


async def test_unknown_day_is_not_answered_as_today(ctx):
    reply = await get_skill("date").invoke(ctx, which="thanksgiving")
    assert not reply.success


async def test_real_clock_shape(ctx):
    reply = await get_skill("date").invoke(ctx)
    assert re.fullmatch(r"It's [A-Z][a-z]+day, [A-Z][a-z]+ \d{1,2}(st|nd|rd|th)\.", reply.text)

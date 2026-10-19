"""Battery skill (nova/skills/battery.py) -- psutil is monkeypatched."""
from __future__ import annotations

from collections import namedtuple

import pytest

import nova.skills  # noqa: F401
from nova.brain.router import Router
from nova.config import Config
from nova.security.audit import AuditLog
from nova.security.ladder import Ladder
from nova.security.tiers import Tier
from nova.skills import battery as battery_mod
from nova.skills.registry import SkillCtx, get_skill

Batt = namedtuple("Batt", "percent secsleft power_plugged")


@pytest.fixture
def ctx(tmp_path):
    return SkillCtx(config=Config(), ladder=Ladder(AuditLog(path=tmp_path / "audit.log")))


def _set(monkeypatch, value):
    def read():
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr(battery_mod, "_read_battery", read)


@pytest.mark.parametrize("text", [
    "how much battery do I have", "battery level", "am I plugged in", "is my laptop charging",
    "How's my battery?", "battery", "what's my battery at", "how much battery is left",
    "how long will my battery last", "battery percentage",
])
def test_routing(text):
    m = Router().route(text)
    assert m.spec is not None and m.spec.name == "battery", m.clarification
    assert m.params == {}


def test_open_battery_settings_is_not_the_battery_read():
    assert Router().route("open battery settings").spec.name == "open_settings"


def test_tier_is_read():
    assert get_skill("battery").tier is Tier.READ


async def test_discharging_with_time_left(ctx, monkeypatch):
    _set(monkeypatch, Batt(63.4, 2 * 3600 + 15 * 60, False))
    reply = await get_skill("battery").invoke(ctx)
    assert reply.success
    assert reply.text == "Battery's at 63 percent, on battery power -- about 2 hours 15 minutes left."


async def test_discharging_short_time(ctx, monkeypatch):
    _set(monkeypatch, Batt(8, 600, False))
    assert (await get_skill("battery").invoke(ctx)).text.endswith("about 10 minutes left.")


async def test_discharging_unknown_time(ctx, monkeypatch):
    _set(monkeypatch, Batt(40, -1, False))  # psutil.POWER_TIME_UNKNOWN
    assert (await get_skill("battery").invoke(ctx)).text == "Battery's at 40 percent, on battery power."


async def test_plugged_in_charging(ctx, monkeypatch):
    _set(monkeypatch, Batt(80, -2, True))  # POWER_TIME_UNLIMITED
    assert (await get_skill("battery").invoke(ctx)).text == "Battery's at 80 percent, and you're plugged in and charging."


async def test_plugged_in_full(ctx, monkeypatch):
    _set(monkeypatch, Batt(100, -2, True))
    assert (await get_skill("battery").invoke(ctx)).text == "Battery's full, and you're plugged in."


async def test_no_battery(ctx, monkeypatch):
    _set(monkeypatch, None)
    reply = await get_skill("battery").invoke(ctx)
    assert reply.success is False and "can't find a battery" in reply.text


async def test_sensor_error(ctx, monkeypatch):
    _set(monkeypatch, RuntimeError("sensor"))
    reply = await get_skill("battery").invoke(ctx)
    assert reply.success is False and reply.text == "I couldn't read the battery."


async def test_real_psutil_path_via_monkeypatched_sensors(ctx, monkeypatch):
    psutil = pytest.importorskip("psutil")
    monkeypatch.setattr(psutil, "sensors_battery", lambda: Batt(55, 3600, False), raising=False)
    assert (await get_skill("battery").invoke(ctx)).text == "Battery's at 55 percent, on battery power -- about 1 hour left."

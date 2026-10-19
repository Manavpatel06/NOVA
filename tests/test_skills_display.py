"""Brightness skills (nova/skills/display.py). winops.powershell is always
monkeypatched -- nothing touches the real display."""
from __future__ import annotations

import pytest

import nova.skills  # noqa: F401
from nova.brain.router import Router
from nova.config import Config
from nova.security.audit import AuditLog
from nova.security.ladder import Ladder
from nova.security.tiers import Tier
from nova.skills import display
from nova.skills.registry import SkillCtx, get_skill
from nova.system import winops


@pytest.fixture
def ctx(tmp_path):
    return SkillCtx(config=Config(), ladder=Ladder(AuditLog(path=tmp_path / "audit.log")))


@pytest.fixture
def fake_ps(monkeypatch):
    state = {"level": 50, "supported": True, "calls": []}

    def fake(script, *, timeout=15.0, **args):
        assert script is display._BRIGHTNESS_PS
        state["calls"].append(dict(args))
        if not state["supported"]:
            return "unsupported"
        op, value = args["op"], int(args.get("value", 0))
        if op == "set":
            state["level"] = value
        elif op == "step":
            state["level"] = max(0, min(100, state["level"] + value))
        return str(state["level"])

    monkeypatch.setattr(winops, "powershell", fake)
    return state


@pytest.mark.parametrize("text,skill,params", [
    ("set brightness to 60", "set_brightness", {"level": "60"}),
    ("brightness 60 percent", "set_brightness", {"level": "60"}),
    ("set the screen brightness to eighty", "set_brightness", {"level": "eighty"}),
    ("max brightness", "set_brightness", {"level": "max"}),
    ("brightness up", "brightness_up", {}),
    ("turn up the brightness", "brightness_up", {}),
    ("make the screen brighter", "brightness_up", {}),
    ("increase brightness by 20", "brightness_up", {"amount": "20"}),
    ("brightness down", "brightness_down", {}),
    ("dim the screen", "brightness_down", {}),
    ("dim the screen a bit", "brightness_down", {}),
    ("make the screen dimmer", "brightness_down", {}),
    ("what's the brightness", "get_brightness", {}),
    ("brightness level", "get_brightness", {}),
    ("how bright is the screen", "get_brightness", {}),
])
def test_brightness_sentences_route_with_params(text, skill, params):
    m = Router().route(text)
    assert m.clarification is None, m.clarification
    assert m.spec is not None and m.spec.name == skill
    assert m.params == params


@pytest.mark.parametrize("text,owner", [
    ("open display settings", "open_settings"),
    ("turn it up", "volume_up"),
    ("open notepad", "open_app"),
])
def test_brightness_patterns_do_not_steal_other_commands(text, owner):
    m = Router().route(text)
    assert m.spec is not None and m.spec.name == owner


def test_tiers():
    for name in ("set_brightness", "brightness_up", "brightness_down"):
        assert get_skill(name).tier is Tier.ACT
    assert get_skill("get_brightness").tier is Tier.READ


async def test_set_brightness(ctx, fake_ps):
    reply = await get_skill("set_brightness").invoke(ctx, level="60")
    assert reply.success and reply.text == "Brightness is at 60."
    assert fake_ps["calls"] == [{"op": "set", "value": 60}]


async def test_step_up_down_default_and_amount(ctx, fake_ps):
    assert (await get_skill("brightness_up").invoke(ctx)).text == "Brightness is at 60."
    assert fake_ps["calls"][-1] == {"op": "step", "value": 10}
    assert (await get_skill("brightness_down").invoke(ctx, amount="30")).text == "Brightness is at 30."
    assert fake_ps["calls"][-1] == {"op": "step", "value": -30}


async def test_get_brightness(ctx, fake_ps):
    reply = await get_skill("get_brightness").invoke(ctx)
    assert reply.text == "Brightness is at 50."
    assert fake_ps["calls"] == [{"op": "get"}] or fake_ps["calls"] == [{"op": "get", "value": 0}]


@pytest.mark.parametrize("bad", ["120", "bright"])
async def test_out_of_range_never_calls_windows(ctx, fake_ps, bad):
    reply = await get_skill("set_brightness").invoke(ctx, level=bad)
    assert reply.success is False and "0 to 100" in reply.text
    assert fake_ps["calls"] == []


async def test_external_monitor_is_reported_honestly(ctx, fake_ps):
    fake_ps["supported"] = False
    for name in ("set_brightness", "get_brightness"):
        kwargs = {"level": "40"} if name == "set_brightness" else {}
        reply = await get_skill(name).invoke(ctx, **kwargs)
        assert reply.success is False
        assert "external monitor" in reply.text


@pytest.mark.parametrize("exc", [winops.WinOpsUnavailable("no"), winops.WinOpsError("boom")])
async def test_failures_are_spoken_refusals(ctx, monkeypatch, exc):
    def boom(*a, **k):
        raise exc
    monkeypatch.setattr(winops, "powershell", boom)
    assert (await get_skill("brightness_up").invoke(ctx)).text == "I couldn't change the brightness."
    assert (await get_skill("get_brightness").invoke(ctx)).text == "I couldn't read the brightness."


async def test_garbled_output_is_a_failure(ctx, monkeypatch):
    monkeypatch.setattr(winops, "powershell", lambda *a, **k: "WARNING: weird")
    assert (await get_skill("get_brightness").invoke(ctx)).success is False


def test_script_uses_wmi_and_reads_level_only_from_novaargs():
    s = display._BRIGHTNESS_PS
    assert "WmiMonitorBrightness" in s and "WmiSetBrightness" in s and "root/WMI" in s
    assert "$NovaArgs.value" in s and "unsupported" in s

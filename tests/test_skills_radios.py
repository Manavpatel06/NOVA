"""Wi-Fi / Bluetooth radio skill (nova/skills/radios.py)."""
from __future__ import annotations

import pytest

import nova.skills  # noqa: F401
from nova.brain.router import Router
from nova.config import Config
from nova.security.audit import AuditLog
from nova.security.ladder import Ladder
from nova.security.tiers import Tier
from nova.skills import radios
from nova.skills.registry import SkillCtx, get_skill
from nova.system import winops


@pytest.fixture
def ctx(tmp_path):
    return SkillCtx(config=Config(), ladder=Ladder(AuditLog(path=tmp_path / "audit.log")))


@pytest.fixture
def calls(monkeypatch):
    rec = {"ps": [], "settings": [], "result": "ok"}

    def fake_ps(script, *, timeout=15.0, **args):
        assert script is radios._RADIO_PS
        rec["ps"].append(dict(args))
        if isinstance(rec["result"], Exception):
            raise rec["result"]
        return rec["result"]

    monkeypatch.setattr(winops, "powershell", fake_ps)
    monkeypatch.setattr(winops, "open_settings_page", lambda page: rec["settings"].append(page))
    return rec


@pytest.mark.parametrize("text,radio,state", [
    ("turn off wifi", "wifi", "off"),
    ("turn on bluetooth", "bluetooth", "on"),
    ("disable wi-fi", "wi-fi", "disable"),
    ("Disable Wi-Fi.", "wi-fi", "disable"),
    ("turn the wifi off", "wifi", "off"),
    ("enable bluetooth", "bluetooth", "enable"),
    ("switch bluetooth off", "bluetooth", "off"),
    ("wifi on", "wifi", "on"),
])
def test_routing(text, radio, state):
    m = Router().route(text)
    assert m.spec is not None and m.spec.name == "set_radio", m.clarification
    assert m.params == {"radio": radio, "state": state}


@pytest.mark.parametrize("text,owner", [
    ("open wifi settings", "open_settings"),
    ("open bluetooth settings", "open_settings"),
    ("turn off the sound", "mute"),
])
def test_does_not_steal(text, owner):
    assert Router().route(text).spec.name == owner


def test_tier():
    assert get_skill("set_radio").tier is Tier.ACT


@pytest.mark.parametrize("radio,state,key,want,spoken", [
    ("wifi", "off", "wifi", "off", "Wi-Fi's off."),
    ("wi-fi", "disable", "wifi", "off", "Wi-Fi's off."),
    ("bluetooth", "on", "bluetooth", "on", "Bluetooth's on."),
    ("bluetooth", "enable", "bluetooth", "on", "Bluetooth's on."),
])
async def test_success(ctx, calls, radio, state, key, want, spoken):
    reply = await get_skill("set_radio").invoke(ctx, radio=radio, state=state)
    assert reply.success and reply.text == spoken
    assert calls["ps"] == [{"radio": key, "state": want}]
    assert calls["settings"] == []


@pytest.mark.parametrize("result", ["denied", "none", winops.WinOpsError("boom")])
async def test_failure_falls_back_to_settings_page_and_says_so(ctx, calls, result):
    calls["result"] = result
    reply = await get_skill("set_radio").invoke(ctx, radio="bluetooth", state="off")
    assert calls["settings"] == ["bluetooth"]
    assert reply.success is False
    assert "opened Bluetooth settings" in reply.text


async def test_wifi_fallback_page(ctx, calls):
    calls["result"] = "denied"
    await get_skill("set_radio").invoke(ctx, radio="wifi", state="on")
    assert calls["settings"] == ["network-wifi"]


async def test_off_windows_no_fallback_attempt(ctx, calls):
    calls["result"] = winops.WinOpsUnavailable("no")
    reply = await get_skill("set_radio").invoke(ctx, radio="wifi", state="off")
    assert reply.success is False and reply.text == "I couldn't turn Wi-Fi off."
    assert calls["settings"] == []


async def test_fallback_itself_failing(ctx, calls, monkeypatch):
    calls["result"] = "denied"

    def boom(page):
        raise winops.WinOpsError("nope")
    monkeypatch.setattr(winops, "open_settings_page", boom)
    reply = await get_skill("set_radio").invoke(ctx, radio="wifi", state="off")
    assert reply.success is False and reply.text == "I couldn't turn Wi-Fi off."


@pytest.mark.parametrize("radio,state", [("cellular", "on"), ("wifi", "maybe")])
async def test_bad_params_never_reach_windows(ctx, calls, radio, state):
    reply = await get_skill("set_radio").invoke(ctx, radio=radio, state=state)
    assert reply.success is False
    assert calls["ps"] == [] and calls["settings"] == []


def test_script_reads_choice_only_from_novaargs():
    s = radios._RADIO_PS
    assert "$NovaArgs.radio" in s and "$NovaArgs.state" in s
    assert "Windows.Devices.Radios.Radio" in s and "SetStateAsync" in s and "AsTask" in s

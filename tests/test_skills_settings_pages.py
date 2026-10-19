"""open_settings (nova/skills/settings_pages.py) -- closed map, never a guess."""
from __future__ import annotations

import re

import pytest

import nova.skills  # noqa: F401
from nova.brain.router import Router
from nova.config import Config
from nova.security.audit import AuditLog
from nova.security.ladder import Ladder
from nova.security.tiers import Tier
from nova.skills import settings_pages
from nova.skills.registry import SkillCtx, get_skill
from nova.system import winops


@pytest.fixture
def ctx(tmp_path):
    return SkillCtx(config=Config(), ladder=Ladder(AuditLog(path=tmp_path / "audit.log")))


@pytest.fixture
def opened(monkeypatch):
    pages = []
    monkeypatch.setattr(winops, "open_settings_page", lambda page: pages.append(page))
    return pages


@pytest.mark.parametrize("text,page", [
    ("open wifi settings", "wifi"),
    ("open bluetooth settings", "bluetooth"),
    ("open sound settings", "sound"),
    ("open display settings", "display"),
    ("Open the Wi-Fi settings.", "wi-fi"),
    ("show me battery settings", "battery"),
    ("go to the settings for bluetooth", "bluetooth"),
])
def test_routing(text, page):
    m = Router().route(text)
    assert m.spec is not None and m.spec.name == "open_settings", m.clarification
    assert m.params == {"page": page}


@pytest.mark.parametrize("text,owner", [("open notepad", "open_app"), ("turn off wifi", "set_radio")])
def test_does_not_steal(text, owner):
    assert Router().route(text).spec.name == owner


def test_tier():
    assert get_skill("open_settings").tier is Tier.ACT


def test_every_mapped_page_is_a_valid_ms_settings_name():
    for spoken, (page, said) in settings_pages.SETTINGS_PAGES.items():
        assert re.fullmatch(r"[a-z0-9-]{1,60}", page), spoken
        assert said


@pytest.mark.parametrize("page,uri,spoken", [
    ("wifi", "network-wifi", "Opening Wi-Fi settings."),
    ("wi-fi", "network-wifi", "Opening Wi-Fi settings."),
    ("bluetooth", "bluetooth", "Opening Bluetooth settings."),
    ("sound", "sound", "Opening sound settings."),
    ("display", "display", "Opening display settings."),
    ("windows update", "windowsupdate", "Opening Windows Update settings."),
])
async def test_opens_mapped_page(ctx, opened, page, uri, spoken):
    reply = await get_skill("open_settings").invoke(ctx, page=page)
    assert reply.success and reply.text == spoken
    assert opened == [uri]


@pytest.mark.parametrize("page", ["foo", "registry", "network-wifi", "../system32", "ms-settings:about"])
async def test_unknown_page_is_an_honest_refusal_and_opens_nothing(ctx, opened, page):
    reply = await get_skill("open_settings").invoke(ctx, page=page)
    assert reply.success is False and reply.text == "I don't know that settings page."
    assert opened == []


async def test_open_failure(ctx, monkeypatch):
    def boom(page):
        raise winops.WinOpsUnavailable("no")
    monkeypatch.setattr(winops, "open_settings_page", boom)
    reply = await get_skill("open_settings").invoke(ctx, page="sound")
    assert reply.success is False and reply.text == "I couldn't open sound settings."

"""Screenshot skill (nova/skills/screenshot.py) -- press_keys is monkeypatched."""
from __future__ import annotations

import pytest

import nova.skills  # noqa: F401
from nova.brain.router import Router
from nova.config import Config
from nova.security.audit import AuditLog
from nova.security.ladder import Ladder
from nova.security.tiers import Tier
from nova.skills.registry import SkillCtx, get_skill
from nova.system import winops


@pytest.fixture
def ctx(tmp_path):
    return SkillCtx(config=Config(), ladder=Ladder(AuditLog(path=tmp_path / "audit.log")))


@pytest.mark.parametrize("text", [
    "take a screenshot", "screenshot", "Take a screen shot, please.", "capture the screen",
    "grab a screenshot of my screen", "print screen",
])
def test_routing(text):
    m = Router().route(text)
    assert m.spec is not None and m.spec.name == "screenshot", m.clarification


def test_tier():
    assert get_skill("screenshot").tier is Tier.ACT


def test_keys_exist_in_vk_table():
    assert "win" in winops.VK and winops.VK["snapshot"] == 0x2C


async def test_presses_win_printscreen(ctx, monkeypatch):
    pressed = []
    monkeypatch.setattr(winops, "press_keys", lambda *names, repeat=1: pressed.append(names))
    reply = await get_skill("screenshot").invoke(ctx)
    assert pressed == [("win", "snapshot")]
    assert reply.success and reply.text == "Screenshot saved to your Screenshots folder."


@pytest.mark.parametrize("exc", [winops.WinOpsUnavailable("no"), winops.WinOpsError("x")])
async def test_failure(ctx, monkeypatch, exc):
    def boom(*a, **k):
        raise exc
    monkeypatch.setattr(winops, "press_keys", boom)
    reply = await get_skill("screenshot").invoke(ctx)
    assert reply.success is False and reply.text == "I couldn't take a screenshot."

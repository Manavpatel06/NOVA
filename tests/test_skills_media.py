"""media_play_pause / media_next / media_previous / media_stop
(nova/skills/media.py) -- ACT tier, media virtual keys via winops.press_keys
(monkeypatched; no real key is ever pressed)."""
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


@pytest.fixture
def keys(monkeypatch):
    pressed: list[tuple] = []
    monkeypatch.setattr(winops, "press_keys", lambda *names, repeat=1: pressed.append(names))
    return pressed


@pytest.fixture(scope="module")
def router():
    return Router()


@pytest.mark.parametrize("text, skill", [
    ("pause", "media_play_pause"),
    ("pause the music", "media_play_pause"),
    ("pause it", "media_play_pause"),
    ("pause the video", "media_play_pause"),
    ("resume", "media_play_pause"),
    ("resume the music", "media_play_pause"),
    ("unpause", "media_play_pause"),
    ("play", "media_play_pause"),
    ("play music", "media_play_pause"),
    ("continue the music", "media_play_pause"),
    ("next song", "media_next"),
    ("next", "media_next"),
    ("skip", "media_next"),
    ("skip this song", "media_next"),
    ("play the next song", "media_next"),
    ("next track please", "media_next"),
    ("previous track", "media_previous"),
    ("previous song", "media_previous"),
    ("go back a song", "media_previous"),
    ("play the last song again", "media_previous"),
    ("stop the music", "media_stop"),
    ("stop playback", "media_stop"),
    ("stop playing", "media_stop"),
    ("stop", "media_stop"),
])
def test_media_routes(router, text, skill):
    m = router.route(text)
    assert m.spec is not None and m.spec.name == skill, m.clarification


@pytest.mark.parametrize("text", ["play lofi beats on youtube", "continue", "go back", "volume up",
                                  "open spotify", "stop the timer"])
def test_media_does_not_steal_neighbouring_commands(router, text):
    m = router.route(text)
    assert m.spec is None or not m.spec.name.startswith("media_"), text


def test_all_media_skills_are_act_tier():
    for name in ("media_play_pause", "media_next", "media_previous", "media_stop"):
        assert get_skill(name).tier is Tier.ACT


@pytest.mark.parametrize("skill, params, key, text", [
    ("media_play_pause", {"action": "pause"}, "media_play_pause", "Paused."),
    ("media_play_pause", {"action": "resume"}, "media_play_pause", "Playing."),
    ("media_play_pause", {"action": "unpause"}, "media_play_pause", "Playing."),
    ("media_play_pause", {}, "media_play_pause", "Paused."),
    ("media_next", {}, "media_next", "Skipping."),
    ("media_previous", {}, "media_prev", "Going back."),
    ("media_stop", {}, "media_stop", "Stopped."),
])
async def test_each_skill_presses_its_media_key(ctx, keys, skill, params, key, text):
    reply = await get_skill(skill).invoke(ctx, **params)
    assert reply.success and reply.text == text
    assert keys == [(key,)]
    assert key in winops.VK


async def test_not_windows_is_an_honest_refusal(ctx, monkeypatch):
    def unavailable(*a, **k):
        raise winops.WinOpsUnavailable("no")
    monkeypatch.setattr(winops, "press_keys", unavailable)
    reply = await get_skill("media_next").invoke(ctx)
    assert not reply.success and "windows" in reply.text.lower()


async def test_key_failure_is_spoken(ctx, monkeypatch):
    def broken(*a, **k):
        raise winops.WinOpsError("boom")
    monkeypatch.setattr(winops, "press_keys", broken)
    reply = await get_skill("media_stop").invoke(ctx)
    assert not reply.success and "couldn't" in reply.text


async def test_real_press_keys_off_windows_raises_unavailable_not_crash(ctx):
    """No monkeypatch: on Linux the real doorway refuses, and the skill says so."""
    import sys
    if sys.platform == "win32":
        pytest.skip("would press a real media key")
    reply = await get_skill("media_play_pause").invoke(ctx, action="pause")
    assert not reply.success

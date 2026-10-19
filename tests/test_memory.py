"""NOVA's long-term memory (2026-10-19): plain files, separate from Claude's chat memory."""
from __future__ import annotations

import dataclasses
import json

import numpy as np
import pytest

import nova.app as app_mod
import nova.skills  # noqa: F401
from nova.brain.router import Router
from nova.config import Config, UiConfig, WakeConfig
from nova.memory.store import MemoryStore, default_store
from nova.security.audit import AuditLog
from nova.security.ladder import Ladder
from nova.security.tiers import ArmedBy
from nova.skills.registry import SkillCtx, get_skill


def test_facts_roundtrip_dedupe_and_forget(tmp_path):
    m = MemoryStore(tmp_path)
    f, new = m.remember("Manav uses the Brave browser")
    assert new and m.facts()[0].text == "Manav uses the Brave browser"
    _, new2 = m.remember("Manav uses the Brave browser.")
    assert not new2 and len(m.facts()) == 1
    m.remember("Manav's sister lives in India")
    assert [x.text for x in m.search("sister")] == ["Manav's sister lives in India"]
    gone = m.forget("brave browser")
    assert [g.text for g in gone] == ["Manav uses the Brave browser."]  # the dedupe kept the newer wording
    assert [x.text for x in m.facts()] == ["Manav's sister lives in India"]
    data = json.loads((tmp_path / "facts.json").read_text(encoding="utf-8"))
    assert data["facts"][0]["text"] == "Manav's sister lives in India"


def test_forget_needs_a_real_match(tmp_path):
    m = MemoryStore(tmp_path)
    m.remember("Manav uses the Brave browser")
    assert m.forget("pizza") == [] and len(m.facts()) == 1


def test_corrupt_facts_file_is_left_alone(tmp_path):
    (tmp_path / "facts.json").write_text("{not json", encoding="utf-8")
    m = MemoryStore(tmp_path)
    assert m.facts() == []
    assert (tmp_path / "facts.json").read_text(encoding="utf-8") == "{not json"


def test_journal_and_context_block(tmp_path):
    m = MemoryStore(tmp_path)
    m.log_turn("open youtube in brave", "Opening YouTube in Brave.", ["open_website"])
    text = m.journal_path().read_text(encoding="utf-8")
    assert "Manav: open youtube in brave" in text and "did: open_website" in text
    m.remember("Manav uses the Brave browser")
    (tmp_path / "from_chat.md").write_text("Manav is building NOVA with Claude in Cowork.", encoding="utf-8")
    block = m.context_block()
    assert "Manav uses the Brave browser" in block
    assert "building NOVA" in block and "not instructions" in block
    assert "open youtube in brave" in block


def test_chat_card_is_capped(tmp_path):
    m = MemoryStore(tmp_path)
    (tmp_path / "from_chat.md").write_text("x" * 10000, encoding="utf-8")
    assert len(m.chat_card()) == 4000


@pytest.mark.parametrize("text,skill,param", [
    ("remember that I use brave", "remember", {"fact": "i use brave"}),
    ("what do you remember about my sister", "recall", {"topic": "my sister"}),
    ("forget that I use brave", "forget", {"what": "i use brave"}),
])
def test_memory_commands_route(text, skill, param):
    m = Router().route(text)
    assert m.spec is not None and m.spec.name == skill and m.params == param


async def test_skills_use_the_store(tmp_path):
    ctx = SkillCtx(config=Config(), ladder=Ladder(AuditLog(path=tmp_path / "a.log")))
    r = await get_skill("remember").invoke(ctx, fact="Manav uses the Brave browser")
    assert r.success and default_store().facts()[0].text == "Manav uses the Brave browser"
    r = await get_skill("recall").invoke(ctx, topic="browser")
    assert "Brave" in r.text
    r = await get_skill("forget").invoke(ctx, what="brave browser")
    assert r.success and default_store().facts() == []


def test_memory_reaches_the_brains_prompt():
    default_store().remember("Manav uses the Brave browser")
    from nova.brain.max_brain import MaxBrain

    b = MaxBrain(dataclasses.replace(Config()), session_factory=lambda p: None)
    assert "Manav uses the Brave browser" in b.system_prompt()


class _Synth:
    def say(self, text):
        return np.zeros(4, dtype=np.float32), 16000


class _Player:
    async def play(self, pcm, sr, *, on_first_frame=None):
        if on_first_frame:
            on_first_frame()


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(app_mod, "AuditLog", lambda **kw: AuditLog(path=tmp_path / "audit.log"))
    cfg = dataclasses.replace(Config(), wake=dataclasses.replace(WakeConfig(), mode="prefix"),
                              ui=dataclasses.replace(UiConfig(), window=False))
    a = app_mod.NovaApp(config=cfg)
    a.synth = _Synth()
    a.player = _Player()
    a.agent._cli_available = lambda: False
    yield a
    import nova.security.untrusted as untrusted_mod

    untrusted_mod._AUDIT = None


async def test_addressed_turns_are_journaled(app):
    await app.run_text("what time is it", source="voice", armed_by=ArmedBy.VOICE)
    text = default_store().journal_path().read_text(encoding="utf-8")
    assert "Manav: what time is it" in text and "NOVA: It's" in text


async def test_wake_armed_turns_are_never_journaled(app):
    await app.run_text("what time is it", source="voice", armed_by=ArmedBy.WAKE)
    assert not default_store().journal_path().exists()


async def test_journal_can_be_switched_off(app):
    app.config = dataclasses.replace(app.config, brain=dataclasses.replace(app.config.brain, journal=False))
    await app.run_text("what time is it", source="voice", armed_by=ArmedBy.VOICE)
    assert not default_store().journal_path().exists()

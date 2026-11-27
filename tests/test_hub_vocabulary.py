"""Words NOVA should hear right (ADR 0034)."""
from __future__ import annotations

import asyncio

import numpy as np
import pytest

from nova.hub import hub_skills
from nova.hub.vocabulary import MAX_CHARS, Vocabulary, clean
from nova.hub.voice import HubVoice


def test_learn_forget_and_the_prompt(tmp_path):
    v = Vocabulary(tmp_path / "vocabulary.txt", names=["nova"], apps=["visual_studio_code", "spotify"])
    assert v.learn("  HydraWav3 ") == "HydraWav3"
    assert v.learn("hydrawav3") == "hydrawav3" and v.words() == ["HydraWav3"]   # no duplicates
    v.learn("CSE 576")
    p = v.prompt()
    assert p.startswith("Words: Nova, Manav,") and "HydraWav3, CSE 576" in p and "Visual Studio Code" in p
    assert v.forget("hydrawav3") and v.words() == ["CSE 576"]
    assert not v.forget("never there")


@pytest.mark.parametrize("bad", ["", "x" * 41, "rm -rf /;", "<script>", "a\nb"])
def test_odd_words_are_refused(tmp_path, bad):
    assert clean(bad) is None or "\n" not in clean(bad)
    v = Vocabulary(tmp_path / "v.txt")
    if clean(bad) is None:
        assert v.learn(bad) is None


def test_the_prompt_stays_short_and_his_words_come_first(tmp_path):
    v = Vocabulary(tmp_path / "v.txt", apps=[f"app{i}" for i in range(200)])
    v.learn("Ghosh")
    p = v.prompt()
    assert len(p) <= MAX_CHARS and "Ghosh" in p


def test_course_codes_come_from_canvas(tmp_path):
    class _Conn:
        _entries = {"google": {"accounts": {"personal": "a@b.c", "school": "d@e.f"}}}

        def status(self):
            return [{"name": "canvas", "state": "ready"}]

        def call(self, name, tool, args):
            assert (name, tool) == ("canvas", "list_courses")
            return "CSE 576: Natural Language Processing\nCSE575 Statistical ML\nASU 101", False

    v = Vocabulary(tmp_path / "v.txt", connectors=_Conn())
    v.terms()
    import time
    for _ in range(50):
        if v._courses:
            break
        time.sleep(0.02)
    terms = v.terms()
    assert {"CSE 576", "CSE 575", "ASU 101", "Personal", "School"} <= set(terms)


def test_hearing_uses_beam_5_and_the_word_list(monkeypatch):
    seen = {}

    class _Model:
        def transcribe(self, audio, **kw):
            seen.update(kw)
            return [], None

    voice = HubVoice(prompt=lambda: "Words: Nova, HydraWav3.")
    monkeypatch.setattr(voice, "_load_stt", lambda: _Model())
    voice.transcribe((np.ones(16000, dtype="<i2") * 500).tobytes())
    assert seen["beam_size"] == 5 and seen["initial_prompt"] == "Words: Nova, HydraWav3."


def test_a_broken_word_list_never_stops_hearing(monkeypatch):
    seen = {}

    class _Model:
        def transcribe(self, audio, **kw):
            seen.update(kw)
            return [], None

    def boom():
        raise OSError("disk")

    voice = HubVoice(prompt=boom)
    monkeypatch.setattr(voice, "_load_stt", lambda: _Model())
    voice.transcribe((np.ones(16000, dtype="<i2") * 500).tobytes())
    assert seen["initial_prompt"] is None and seen["hotwords"] == "Nova Manav"


def test_learn_word_by_voice(tmp_path):
    v = Vocabulary(tmp_path / "v.txt")
    skills = hub_skills.make(vocabulary=v)
    assert skills["learn_word"].tier.name == "ACT" and skills["my_words"].tier.name == "READ"
    assert asyncio.run(skills["learn_word"].handler(None, word="Ghosh")).text == "Got it, I'll listen for Ghosh."
    assert "Ghosh" in asyncio.run(skills["my_words"].handler(None)).text
    assert asyncio.run(skills["forget_word"].handler(None, word="ghosh")).success

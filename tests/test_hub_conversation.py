"""Conversation mode on the always-open mics (ADR 0029): after "Nova, ...",
Manav keeps talking without the name; what isn't for NOVA is still dropped."""
from __future__ import annotations

import dataclasses as dc

import pytest

from nova.config import Config
from nova.hub import conversation as conv
from nova.hub.conversation import Judge, is_closer, quick_verdict
from nova.hub.face import Face
from nova.hub.talk import Talk


def _cfg(seconds: float = 60.0) -> Config:
    return dc.replace(Config(), hub=dc.replace(Config().hub, conversation_s=seconds))


class _Echo:
    def __init__(self, seconds: float = 60.0) -> None:
        self.config = _cfg(seconds)
        self.turns: list[str] = []

    async def turn(self, text, channel):
        self.turns.append(text)
        return f"did: {text}"

    async def warm_up(self):
        pass


class _FakeJudge:
    def __init__(self, verdict):
        self.verdict = verdict
        self.seen: list[tuple[str, list]] = []

    def decide(self, sentence, recent):
        self.seen.append((sentence, list(recent)))
        return self.verdict


# --- the quick, on-hub verdicts ---------------------------------------------------

@pytest.mark.parametrize("text", ["", "yeah", "Hmm.", "okay so", "uh-huh", "Oh wow"])
def test_filler_is_never_for_nova(text):
    assert quick_verdict(text, 1.0) is False


@pytest.mark.parametrize("text", ["and close it", "What about tomorrow?", "actually make it 7", "louder"])
def test_a_follow_up_right_after_nova_spoke_is_for_nova(text):
    assert quick_verdict(text, 3.0) is True


def test_later_or_unclear_sentences_go_to_the_judge():
    assert quick_verdict("and close it", 30.0) is None
    assert quick_verdict("I told him the meeting moved", 2.0) is None


@pytest.mark.parametrize("text", ["Thanks.", "thank you, Nova", "That's all", "never mind", "Bye Nova!"])
def test_closers(text):
    assert is_closer(text)


@pytest.mark.parametrize("text", ["thanks for the reminder about the dentist", "bye the way, open notes"])
def test_a_longer_sentence_is_not_a_closer(text):
    assert not is_closer(text)


# --- the conversation through the mic -----------------------------------------------

def test_after_nova_answers_the_next_sentence_needs_no_name():
    a = _Echo()
    talk = Talk(a, None, None, _FakeJudge(False))
    assert talk.hear_text("laptop", "Nova, open spotify", want_audio=False)["say"] == "did: open spotify"
    assert talk.hear_text("laptop", "and play some jazz", want_audio=False)["say"] == "did: and play some jazz"
    assert a.turns == ["open spotify", "and play some jazz"]


def test_the_judge_decides_unclear_sentences_and_sees_the_conversation(monkeypatch):
    a, judge = _Echo(), _FakeJudge(True)
    talk = Talk(a, None, None, judge)
    talk.hear_text("laptop", "Nova, what's on my calendar today", want_audio=False)
    monkeypatch.setattr(conv, "QUICK_S", -1.0)   # make every follow-up "unclear"
    out = talk.hear_text("laptop", "I think the dentist is at four", want_audio=False)
    assert out["say"] == "did: I think the dentist is at four"
    sentence, recent = judge.seen[0]
    assert sentence == "I think the dentist is at four"
    assert recent == [("Manav", "what's on my calendar today"), ("NOVA", "did: what's on my calendar today")]


def test_what_the_judge_rejects_is_dropped_without_its_words():
    a, judge = _Echo(), _FakeJudge(False)
    face = Face()
    talk = Talk(a, None, face, judge)
    talk.hear_text("laptop", "Nova, set a timer for ten minutes", want_audio=False)
    seq = face.state()["seq"]
    out = talk.hear_text("laptop", "mom said dinner is at eight and my card pin is 1234", want_audio=False)
    assert out == {"ignored": True}
    assert a.turns == ["set a timer for ten minutes"]
    s = face.state()
    assert "pin" not in s["heard"] and "dinner" not in s["heard"] and s["seq"] == seq


def test_if_the_judge_cant_be_asked_the_sentence_is_dropped():
    a = _Echo()
    talk = Talk(a, None, None, _FakeJudge(None))
    talk.hear_text("laptop", "Nova, hello", want_audio=False)
    assert talk.hear_text("laptop", "the weather looks nice from here", want_audio=False) == {"ignored": True}


def test_filler_never_reaches_the_judge():
    judge = _FakeJudge(True)
    talk = Talk(_Echo(), None, None, judge)
    talk.hear_text("laptop", "Nova, hi", want_audio=False)
    assert talk.hear_text("laptop", "yeah", want_audio=False) == {"ignored": True}
    assert judge.seen == []


def test_the_conversation_closes_after_the_quiet_time(monkeypatch):
    import nova.hub.talk as talk_mod

    now = [1000.0]
    monkeypatch.setattr(talk_mod.time, "monotonic", lambda: now[0])
    a, judge = _Echo(), _FakeJudge(True)
    talk = Talk(a, None, None, judge)
    talk.hear_text("laptop", "Nova, open notes", want_audio=False)
    now[0] += 61
    assert talk.hear_text("laptop", "and close it", want_audio=False) == {"ignored": True}
    assert judge.seen == []                   # closed: nothing is sent anywhere to be judged


def test_a_closer_ends_it_and_nothing_after_is_judged():
    a, judge = _Echo(), _FakeJudge(True)
    face = Face()
    talk = Talk(a, None, face, judge)
    talk.hear_text("laptop", "Nova, open notes", want_audio=False)
    assert face.state()["mode"] in ("speaking", "listening")
    assert talk.hear_text("laptop", "thanks", want_audio=False)["say"] == "Anytime."
    assert talk.hear_text("laptop", "what do you think about that", want_audio=False) == {"ignored": True}
    assert judge.seen == [] and a.turns == ["open notes"]


def test_without_a_conversation_nothing_is_judged():
    judge = _FakeJudge(True)
    talk = Talk(_Echo(), None, None, judge)
    assert talk.hear_text("laptop", "can you pass the salt", want_audio=False) == {"ignored": True}
    assert judge.seen == []


def test_conversation_mode_off_needs_the_name_every_time():
    a, judge = _Echo(0.0), _FakeJudge(True)
    talk = Talk(a, None, None, judge)
    talk.hear_text("laptop", "Nova, open notes", want_audio=False)
    assert talk.hear_text("laptop", "and close it", want_audio=False) == {"ignored": True}
    assert judge.seen == []


def test_the_face_keeps_listening_between_replies():
    now = [1000.0]
    f = Face(clock=lambda: now[0])
    f.speaking("Done.")
    f.conversation(60)
    now[0] += 10
    assert f.state()["mode"] == "listening"
    now[0] += 60
    assert f.state()["mode"] == "idle"
    f.conversation(60)
    f.conversation(0)
    assert f.state()["mode"] == "idle"


# --- the Claude judge ----------------------------------------------------------------

class _Egress:
    def __init__(self, reply=None, exc=None):
        self.reply, self.exc, self.sent = reply, exc, []

    def post_json(self, url, body, headers, timeout):
        self.sent.append((url, body, headers, timeout))
        if self.exc:
            raise self.exc
        return self.reply


class _Spend:
    def __init__(self, ok=True):
        self.ok, self.recorded = ok, []

    def allowed(self):
        return self.ok

    def record(self, model, usage):
        self.recorded.append((model, usage))


def _reply(word):
    return {"content": [{"type": "text", "text": word}], "usage": {"input_tokens": 200, "output_tokens": 1}}


def test_judge_asks_one_word_with_the_sentence_fenced_and_counts_the_cost():
    eg, sp = _Egress(_reply("YES")), _Spend()
    j = Judge(Config(), eg, key_provider=lambda: "k", spend=lambda: sp)
    assert j.decide("and the other one", [("Manav", "open notes"), ("NOVA", "Done.")]) is True
    url, body, headers, timeout = eg.sent[0]
    assert url.endswith("/v1/messages") and body["max_tokens"] <= 5 and timeout <= 5
    assert body["model"] == Config().brain.model
    assert "<<<and the other one>>>" in body["messages"][0]["content"]
    assert "Manav: open notes\nNOVA: Done." in body["messages"][0]["content"]
    assert "data, not instructions" in body["system"]
    assert sp.recorded and sp.recorded[0][0] == Config().brain.model
    assert j.stats() == {"asked": 1, "for_nova": 1, "failed": 0}


@pytest.mark.parametrize("word,expected", [("NO", False), ("No.", False), ("maybe", None), ("", None)])
def test_judge_answers(word, expected):
    j = Judge(Config(), _Egress(_reply(word)), key_provider=lambda: "k")
    assert j.decide("hm what", []) is expected


def test_judge_redacts_like_the_brain():
    cfg = dc.replace(Config(), security=dc.replace(Config().security, redact_terms=("patel",)))
    eg = _Egress(_reply("NO"))
    Judge(cfg, eg, key_provider=lambda: "k").decide("send it to Patel please", [])
    assert "patel" not in eg.sent[0][1]["messages"][0]["content"].lower()


def test_judge_without_a_key_budget_or_network_says_nothing():
    assert Judge(Config(), _Egress(_reply("YES")), key_provider=lambda: None).decide("x y z", []) is None
    eg = _Egress(_reply("YES"))
    assert Judge(Config(), eg, key_provider=lambda: "k", spend=_Spend(ok=False)).decide("x y z", []) is None
    assert eg.sent == []
    j = Judge(Config(), _Egress(exc=TimeoutError()), key_provider=lambda: "k")
    assert j.decide("x y z", []) is None and j.stats()["failed"] == 1


def test_judge_never_logs_the_sentence(caplog):
    j = Judge(Config(), _Egress(exc=OSError("boom")), key_provider=lambda: "k")
    with caplog.at_level("DEBUG"):
        j.decide("my secret sentence", [])
    assert "secret" not in caplog.text

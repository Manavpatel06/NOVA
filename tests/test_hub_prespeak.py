"""Speaking while Claude is still writing (2026-11-26): the first pieces of a
reply are voiced as soon as they can't change, and never made twice."""
from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest

from nova.hub.talk import Talk, _Prespeak, split_speech

REPLY = ("Sure, here's the short version of how that works. "
         "The longer explanation follows in this second sentence, which keeps going for a while. "
         "And a third one to finish.")


class _Voice:
    def __init__(self, delay=0.0):
        self.spoken = []
        self.delay = delay
        self.lock = threading.Lock()

    def ready(self):
        return True

    def speak(self, text):
        time.sleep(self.delay)
        with self.lock:
            self.spoken.append(text)
            self.when = getattr(self, "when", {})
            self.when[text] = time.monotonic()
        return b"RIFF" + text.encode()

    def transcribe(self, pcm):
        return ""


def test_only_pieces_that_cant_change_are_made_ahead():
    v = _Voice()
    p = _Prespeak(v)
    first = split_speech(REPLY)[0]
    p.feed(REPLY[:30])                       # first piece still growing: nothing yet
    p.feed(REPLY[:len(first) + 25])          # first piece complete, second started
    assert p.take(first) == b"RIFF" + first.encode()
    assert v.spoken == [first]
    assert p.take("something never fed") is None


def test_a_piece_that_doesnt_end_up_in_the_reply_is_ignored():
    v = _Voice()
    p = _Prespeak(v)
    p.feed("Let me check that for you right now, it'll take just a second. Then")
    p.close()
    assert p.take("Your meeting is at 3 PM.") is None


class _Streaming:
    """An assistant whose brain writes the reply in chunks, like the API brain does."""

    async def turn(self, text, channel):
        on_text = getattr(channel, "on_text", None)
        for i in range(10, len(REPLY) + 10, 10):
            if on_text:
                on_text(REPLY[:i])
            await asyncio.sleep(0.02)
        self.finished = time.monotonic()
        return REPLY

    async def warm_up(self):
        pass


def test_talk_uses_the_voice_made_ahead_and_never_makes_a_piece_twice():
    v = _Voice(delay=0.05)
    brain = _Streaming()
    talk = Talk(brain, v, None)
    out = talk.handle({"session": "prespeak-test-1", "text": "explain it", "speak": True})
    parts = split_speech(REPLY)
    assert v.when[parts[0]] < brain.finished          # the first piece was ready before Claude finished
    assert out["say"] == REPLY and out["parts"] == len(parts) - 1
    for i in range(len(parts) - 1):
        talk.more({"id": out["more"], "part": i})
    assert sorted(v.spoken) == sorted(parts)          # every piece made exactly once


def test_no_voice_wanted_means_nothing_made_ahead():
    v = _Voice()
    talk = Talk(_Streaming(), v, None)
    talk.handle({"session": "prespeak-test-2", "text": "explain it", "speak": False})
    time.sleep(0.1)
    assert v.spoken == []


# --- the API brain reads the reply as it streams ---------------------------------------

def _events(texts, tool=None):
    yield {"type": "message_start", "message": {"usage": {"input_tokens": 50, "cache_read_input_tokens": 10}}}
    yield {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}
    for t in texts:
        yield {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": t}}
    yield {"type": "content_block_stop", "index": 0}
    if tool:
        yield {"type": "content_block_start", "index": 1,
               "content_block": {"type": "tool_use", "id": "tu1", "name": tool, "input": {}}}
        yield {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"ap'}}
        yield {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": 'p": "x"}'}}
        yield {"type": "content_block_stop", "index": 1}
    yield {"type": "message_delta", "delta": {"stop_reason": "tool_use" if tool else "end_turn"},
           "usage": {"output_tokens": 12}}
    yield {"type": "message_stop"}


class _StreamEgress:
    def __init__(self, streams):
        self.streams = list(streams)
        self.bodies = []

    def post_stream(self, url, body, *, headers, timeout):
        self.bodies.append(body)
        return self.streams.pop(0)

    def post_json(self, *a, **k):
        raise AssertionError("should stream")


def test_the_api_brain_streams_and_rebuilds_the_reply():
    from tests.test_brain_agent import make_agent, recorder

    agent, _, _ = make_agent([])
    eg = _StreamEgress([_events(["Opening ", "it."], tool="open_app"), _events(["Done, ", "it's open."])])
    agent._egress = eg
    seen = []
    invoke, calls = recorder()
    out = asyncio.run(agent.respond("open x", invoke, on_text=seen.append))
    assert out.text == "Done, it's open."
    assert calls[0][:2] == ("open_app", {"app": "x"})
    assert seen[-1] == "Done, it's open." and "Opening it." in seen
    assert all(b["stream"] is True for b in eg.bodies)


def test_spend_is_counted_from_the_streamed_usage():
    from tests.test_brain_agent import make_agent, recorder

    agent, _, _ = make_agent([])
    agent._egress = _StreamEgress([_events(["Hi."])])

    class _S:
        rec = []

        def allowed(self):
            return True

        def record(self, model, usage):
            self.rec.append(usage)

    agent._spend = _S()
    invoke, _ = recorder()
    asyncio.run(agent.respond("hello", invoke, on_text=lambda t: None))
    assert agent._spend.rec[0] == {"input_tokens": 50, "cache_read_input_tokens": 10, "output_tokens": 12}


def test_without_a_listener_the_brain_does_not_stream():
    from tests.test_brain_agent import make_agent, recorder, say

    agent, eg, _ = make_agent([say("ok")])
    invoke, _ = recorder()
    assert asyncio.run(agent.respond("hi", invoke)).text == "ok"
    assert "stream" not in eg.bodies[0]


def test_hearing_goes_before_voicing_the_rest_of_an_earlier_reply(monkeypatch):
    import numpy as np

    from nova.hub.voice import HubVoice

    order = []

    class _Stt:
        def transcribe(self, audio, **kw):
            order.append("hear")
            time.sleep(0.1)
            return [], None

    class _Tts:
        def create(self, text, **kw):
            order.append(text)
            time.sleep(0.5)                            # a long piece
            return np.zeros(10, dtype="float32"), 24000

    v = HubVoice()
    monkeypatch.setattr(v, "_load_stt", lambda: _Stt())
    monkeypatch.setattr(v, "_load_tts", lambda: _Tts())
    rest = threading.Thread(target=lambda: [v.speak_later(p) for p in ("p1", "p2")])
    rest.start()
    time.sleep(0.05)                                   # p1 is being voiced
    t0 = time.monotonic()
    v.transcribe((np.ones(16000, dtype="<i2") * 1000).tobytes())
    heard_in = time.monotonic() - t0
    rest.join()
    assert heard_in < 0.35                             # not stuck behind p1's half second
    assert order == ["p1", "hear", "p2"]               # and p2 waited for the hearing

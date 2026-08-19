"""Contract: nova/app.py (T0.6b) — NovaApp, the turn loop.

    hotkey -> capture -> Whisper -> router -> ladder -> skill -> reply text
    -> Kokoro -> speakers

Per the module's own docstring, this is a genuine integration target, not
another focused unit module: STT/TTS/playback are mocked (they touch real
hardware/models), but the router, ladder, AuditLog, and the real M0 skill
registry all run for real — so these tests exercise the actual wiring
between SkillSpec.invoke(), Ladder.authorize(), and the router's
clarification path, not a plausible-looking stand-in for it.

The two promises the lead asked to have pinned specifically:
  - a below-floor/ambiguous input invokes ZERO skills and produces ZERO
    audit records (the clarification path is a normal outcome, not an
    error path — the router asking a question must never look, to the
    audit trail, like nothing happened OR like a skill ran);
  - NOVA never goes mute: if synthesis fails, run_turn() still completes
    and returns a Reply rather than raising or hanging.

AuditLog's path isn't injectable through NovaApp's constructor (no
parameter for it — it always resolves to the real, hardcoded
data/audit.log), so every fixture here patches nova.app.AuditLog to build
one pointed at tmp_path before constructing NovaApp, the same seam used
throughout this suite for hardcoded-by-contract paths.
"""
from __future__ import annotations

import asyncio

import numpy as np
import pytest

import nova.app as app_mod
from nova.config import Config
from nova.governor import TOPIC_TURN_END, TOPIC_TURN_START
from nova.security.audit import AuditLog
from nova.security.tiers import Tier
from nova.skills.registry import Reply, SkillSpec


class _FakeTranscriber:
    def __init__(self, text="what time is it", raises=None):
        self.text = text
        self.raises = raises
        self.calls: list[np.ndarray] = []

    def transcribe(self, pcm):
        self.calls.append(pcm)
        if self.raises is not None:
            raise self.raises
        return self.text


class _FakeSynth:
    def __init__(self, raises=None, raise_on_text=None):
        self.said: list[str] = []
        self.raises = raises
        self.raise_on_text = raise_on_text

    def say(self, text):
        self.said.append(text)
        if self.raises is not None:
            raise self.raises
        if self.raise_on_text is not None and text == self.raise_on_text:
            raise RuntimeError("both Kokoro and SAPI failed")
        return np.zeros(4, dtype=np.float32), 16000


class _FakePlayer:
    def __init__(self, raises=None):
        self.played: list[tuple] = []
        self.raises = raises

    async def play(self, pcm, samplerate):
        self.played.append((pcm, samplerate))
        if self.raises is not None:
            raise self.raises


@pytest.fixture
def app(tmp_path, monkeypatch):
    # nova/app.py now threads config.security.redact_terms into AuditLog
    # (F22 — see nova/app.py's own comment on why omitting it is dangerous),
    # so this stand-in must accept and forward it too, not just `path`.
    monkeypatch.setattr(
        app_mod, "AuditLog",
        lambda **kw: AuditLog(path=tmp_path / "audit.log", redact_terms=kw.get("redact_terms", ())),
    )
    application = app_mod.NovaApp(config=Config())
    application.transcriber = _FakeTranscriber()
    application.synth = _FakeSynth()
    application.player = _FakePlayer()
    yield application
    import nova.security.untrusted as untrusted_mod

    untrusted_mod._AUDIT = None  # don't leak this test's binding into others


def _turn_events(app_instance):
    """Subscribes a recorder to TOPIC_TURN_START/END and returns the list
    it appends to, in order."""
    events: list[str] = []

    async def on_start(_payload):
        events.append("start")

    async def on_end(_payload):
        events.append("end")

    app_instance.bus.subscribe(TOPIC_TURN_START, on_start)
    app_instance.bus.subscribe(TOPIC_TURN_END, on_end)
    return events


async def _wait_until(predicate, timeout=2.0):
    import time

    start = time.monotonic()
    while not predicate():
        if time.monotonic() - start > timeout:
            raise TimeoutError("condition never became true")
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# Happy path: STT -> real router -> real ladder+skill -> TTS -> playback
# ---------------------------------------------------------------------------


async def test_happy_path_reads_time_speaks_and_plays_it(app):
    app.transcriber = _FakeTranscriber(text="what time is it")

    reply = await app.run_turn(np.zeros(16000, dtype=np.float32))

    assert reply is not None
    assert reply.success is True
    assert app.synth.said == [reply.text]
    assert len(app.player.played) == 1
    assert app.player.played[0][1] == 16000  # samplerate came from synth.say's return


async def test_skill_invocation_produces_exactly_one_audit_record_not_double_authorized(app):
    """app.py's own docstring: it never calls ladder.authorize() itself,
    because SkillSpec.invoke() already does — calling both would double-
    authorize and double-audit. Pinned via the audit record count, not
    just 'it worked'."""
    app.transcriber = _FakeTranscriber(text="what time is it")

    await app.run_turn(np.zeros(16000, dtype=np.float32))

    status = app.audit.verify()
    assert status.ok is True
    assert status.entries == 1  # exactly one ladder.authorize record


async def test_act_tier_skill_also_produces_exactly_one_audit_record(app):
    """Same promise, ACT tier this time (open_app) -- config.security.
    allowed_apps is empty by default, so the skill itself refuses the
    launch, but the LADDER still only authorizes once."""
    app.transcriber = _FakeTranscriber(text="open notepad")

    reply = await app.run_turn(np.zeros(16000, dtype=np.float32))

    assert reply is not None
    assert reply.success is False  # the skill's own refusal (not configured), not a ladder deny
    assert app.audit.verify().entries == 1


# ---------------------------------------------------------------------------
# The lead's specific ask: below-floor / ambiguous -> zero skills, zero audit
# ---------------------------------------------------------------------------


async def test_low_confidence_text_invokes_zero_skills_and_produces_zero_audit_records(app):
    app.transcriber = _FakeTranscriber(text="asdkjhaslkdjh gibberish nonsense")

    reply = await app.run_turn(np.zeros(16000, dtype=np.float32))

    assert reply is None  # clarification, not a routed skill result
    assert app.audit.verify().entries == 0
    assert len(app.synth.said) == 1
    assert "rephrase" in app.synth.said[0].lower()


async def test_clarification_is_spoken_not_silently_dropped(app):
    app.transcriber = _FakeTranscriber(text="what is the weather today")

    await app.run_turn(np.zeros(16000, dtype=np.float32))

    assert len(app.synth.said) == 1
    assert len(app.player.played) == 1  # the clarification itself gets spoken and played


# ---------------------------------------------------------------------------
# STT / empty-text failure paths: fail loudly, still speak something
# ---------------------------------------------------------------------------


async def test_stt_failure_speaks_apology_invokes_no_skill_zero_audit(app):
    app.transcriber = _FakeTranscriber(raises=RuntimeError("cuda OOM"))

    reply = await app.run_turn(np.zeros(16000, dtype=np.float32))

    assert reply is None
    assert app.audit.verify().entries == 0
    assert app.synth.said == ["Sorry, I had trouble hearing that."]


async def test_empty_transcription_speaks_i_didnt_catch_that(app):
    app.transcriber = _FakeTranscriber(text="   ")

    reply = await app.run_turn(np.zeros(16000, dtype=np.float32))

    assert reply is None
    assert app.audit.verify().entries == 0
    assert app.synth.said == ["I didn't catch that."]


# ---------------------------------------------------------------------------
# Skill-handler failure: ladder already authorized (real audit record),
# but the handler itself blowing up must not crash the turn
# ---------------------------------------------------------------------------


async def test_skill_handler_exception_is_caught_and_apologized_for(app):
    async def exploding_handler(ctx, **params):
        raise RuntimeError("handler bug")

    boom_spec = SkillSpec(
        name="time", tier=Tier.READ, examples=("what time is it",),
        trusts_input=False, handler=exploding_handler,
    )
    app.router = app_mod.Router(skills={"time": boom_spec})
    app.transcriber = _FakeTranscriber(text="what time is it")

    reply = await app.run_turn(np.zeros(16000, dtype=np.float32))

    assert reply is None
    assert app.synth.said == ["Sorry, something went wrong with time."]
    # The ladder still ran and audited the ALLOW before the handler raised --
    # a broken handler is a different fact from an unauthorized call.
    assert app.audit.verify().entries == 1


# ---------------------------------------------------------------------------
# Router failure (defensive: Router.route() itself raising)
# ---------------------------------------------------------------------------


async def test_router_exception_is_caught_and_apologized_for(app, monkeypatch):
    def boom(text):
        raise RuntimeError("router bug")

    monkeypatch.setattr(app.router, "route", boom)
    app.transcriber = _FakeTranscriber(text="anything")

    reply = await app.run_turn(np.zeros(16000, dtype=np.float32))

    assert reply is None
    assert app.audit.verify().entries == 0
    assert "went wrong" in app.synth.said[0].lower()


# ---------------------------------------------------------------------------
# NOVA never goes mute: run_turn() completes even when TTS fails
# ---------------------------------------------------------------------------


async def test_final_speak_failure_does_not_crash_the_turn_or_lose_the_reply(app):
    """Synthesizer already falls back to SAPI internally; this simulates
    the case where BOTH engines failed for this utterance -- run_turn()
    must still return the Reply object rather than raising, even though
    nothing was actually spoken."""
    app.transcriber = _FakeTranscriber(text="what time is it")
    app.synth = _FakeSynth(raises=RuntimeError("both Kokoro and SAPI failed"))

    reply = await app.run_turn(np.zeros(16000, dtype=np.float32))  # must not raise

    assert reply is not None
    assert reply.success is True
    assert app.player.played == []  # never reached — synth failed first


async def test_player_failure_does_not_crash_the_turn(app):
    app.transcriber = _FakeTranscriber(text="what time is it")
    app.player = _FakePlayer(raises=RuntimeError("no output device"))

    reply = await app.run_turn(np.zeros(16000, dtype=np.float32))  # must not raise

    assert reply is not None


# ---------------------------------------------------------------------------
# TOPIC_TURN_START/END bracket every path, including every early return
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,extra_setup",
    [
        ("what time is it", None),
        ("asdkjhaslkdjh gibberish", None),
        ("   ", None),
        (None, "stt_raises"),
    ],
    ids=["happy", "clarification", "empty", "stt_failure"],
)
async def test_turn_events_bracket_every_path(app, text, extra_setup):
    events = _turn_events(app)
    if extra_setup == "stt_raises":
        app.transcriber = _FakeTranscriber(raises=RuntimeError("boom"))
    else:
        app.transcriber = _FakeTranscriber(text=text)

    await app.run_turn(np.zeros(16000, dtype=np.float32))

    assert events == ["start", "end"]


async def test_turn_end_is_published_even_when_the_skill_handler_raises(app):
    async def exploding_handler(ctx, **params):
        raise RuntimeError("handler bug")

    boom_spec = SkillSpec(
        name="time", tier=Tier.READ, examples=("what time is it",),
        trusts_input=False, handler=exploding_handler,
    )
    app.router = app_mod.Router(skills={"time": boom_spec})
    app.transcriber = _FakeTranscriber(text="what time is it")
    events = _turn_events(app)

    await app.run_turn(np.zeros(16000, dtype=np.float32))

    assert events == ["start", "end"]


async def test_turns_are_serialized_not_overlapping(app):
    """A second run_turn() must wait for the first to finish (the internal
    asyncio.Lock) -- proven by having the first turn's fake transcriber
    block until released, and confirming the second doesn't start early."""
    release = asyncio.Event()
    order: list[str] = []

    class _SlowTranscriber:
        def transcribe(self, pcm):
            order.append("first-start")
            return "what time is it"

    app.transcriber = _SlowTranscriber()

    async def slow_speak(text):
        order.append("first-speak-start")
        await release.wait()
        order.append("first-speak-end")

    app._speak = slow_speak  # type: ignore[method-assign]

    first = asyncio.create_task(app.run_turn(np.zeros(16000, dtype=np.float32)))
    await _wait_until(lambda: "first-speak-start" in order)

    import threading

    second_started = threading.Event()  # set from to_thread()'s worker thread

    def mark_second(pcm):
        # transcribe() runs via asyncio.to_thread(), so this must be a
        # plain sync callable, not a coroutine function.
        second_started.set()
        return "what time is it"

    async def run_second():
        app.transcriber = type("T", (), {"transcribe": staticmethod(mark_second)})()
        await app.run_turn(np.zeros(16000, dtype=np.float32))

    second = asyncio.create_task(run_second())
    await asyncio.sleep(0.05)
    assert not second_started.is_set(), "second turn started before the first released the lock"

    release.set()
    await first
    await second
    assert second_started.is_set()

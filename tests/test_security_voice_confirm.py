"""Voice approval (Manav, 2026-10-15): "Should I open Notepad?" -> "yes";
risky (CONFIRM-tier) actions need "Nova, confirm"."""
from __future__ import annotations

import asyncio
import dataclasses
import types

import numpy as np
import pytest

import nova.app as app_mod
from nova.config import Config, UiConfig, WakeConfig
from nova.security.audit import AuditLog
from nova.security.ladder import ConfirmTimeout
from nova.security.tiers import ArmedBy, Tier
from nova.security.voice_confirm import VOICE_UNAVAILABLE, VoiceConfirmChannel, decide, describe


def _spec(name="open_app", tier=Tier.ACT, phrase=None):
    return types.SimpleNamespace(name=name, tier=tier, confirm_phrase=phrase)


# -- decide -------------------------------------------------------------------

@pytest.mark.parametrize("said", ["yes", "Yes.", "yeah", "Sure!", "do it", "go ahead", "Nova, yes", "yes please", "okay"])
def test_plain_yes_approves_normal_actions(said):
    assert decide(said, risky=False) is True


@pytest.mark.parametrize("said", ["no", "nope", "cancel", "yes but open chrome instead", "no wait yes", "I guess",
                                  "", "what", "don't", "yes no", "so I told him yes"])
def test_anything_else_declines_normal_actions(said):
    assert decide(said, risky=False) is False


@pytest.mark.parametrize("said", ["Nova, confirm", "nova confirm", "Hey Nova, confirm.", "Nova, I confirm"])
def test_risky_needs_name_plus_confirm(said):
    assert decide(said, risky=True) is True


@pytest.mark.parametrize("said", ["yes", "confirm", "yeah do it", "Nova, yes", "Nova", "Nova confirm the other thing"])
def test_risky_refuses_plain_yes_or_confirm_without_name(said):
    assert decide(said, risky=True) is False


def test_learned_name_spelling_counts_for_risky():
    assert decide("Noah, confirm", risky=True, names=["nova", "noah"]) is True


def test_describe_uses_confirm_phrase_then_falls_back():
    assert describe(_spec(phrase="open {app}"), {"app": "notepad"}) == "open notepad"
    assert describe(_spec(name="set_volume"), {"level": "40"}) == "set volume 40"
    assert describe(_spec(phrase="close {app}"), {}) == "open app"  # bad format -> name fallback


# -- channel ------------------------------------------------------------------

async def test_channel_asks_and_approves():
    asked = []

    async def ask(prompt):
        asked.append(prompt)
        return "yes"

    ch = VoiceConfirmChannel(ask)
    assert await ch.request(_spec(phrase="open {app}"), {"app": "notepad"}) is True
    assert asked == ["Should I open notepad?"]


async def test_channel_risky_prompt_mentions_confirm_and_declines_yes():
    asked = []

    async def ask(prompt):
        asked.append(prompt)
        return "yes"

    ch = VoiceConfirmChannel(ask)
    assert await ch.request(_spec(name="shutdown_computer", tier=Tier.CONFIRM, phrase="shut down the computer"), {}) is False
    assert "Nova, confirm" in asked[0]


async def test_no_answer_is_a_timeout_not_a_decline():
    async def ask(prompt):
        return None

    with pytest.raises(ConfirmTimeout):
        await VoiceConfirmChannel(ask).request(_spec(), {"app": "x"})


async def test_unavailable_voice_uses_fallback_dialog():
    class Dialog:
        calls = 0

        async def request(self, skill, params):
            Dialog.calls += 1
            return True

    async def ask(prompt):
        return VOICE_UNAVAILABLE

    assert await VoiceConfirmChannel(ask, fallback=Dialog()).request(_spec(), {"app": "x"}) is True
    assert Dialog.calls == 1


async def test_unavailable_voice_without_fallback_fails_closed():
    async def ask(prompt):
        return VOICE_UNAVAILABLE

    with pytest.raises(ConfirmTimeout):
        await VoiceConfirmChannel(ask).request(_spec(), {})


# -- inside the app ---------------------------------------------------------------

class _Synth:
    def __init__(self):
        self.said = []

    def say(self, text):
        self.said.append(text)
        return np.zeros(4, dtype=np.float32), 16000


class _Player:
    async def play(self, pcm, sr, *, on_first_frame=None):
        if on_first_frame:
            on_first_frame()


class _Transcriber:
    def __init__(self, text):
        self.text = text

    def transcribe(self, pcm):
        return self.text


class _Seg:
    """Returns one utterance on the first process() call."""

    def __init__(self):
        self.in_speech = False
        self.pending = np.zeros(1600, dtype=np.float32)

    def process(self, frame):
        out, self.pending = self.pending, None
        return out

    def reset(self):
        pass


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(app_mod, "AuditLog", lambda **kw: AuditLog(path=tmp_path / "audit.log"))
    cfg = dataclasses.replace(
        Config(), wake=dataclasses.replace(WakeConfig(), mode="prefix"),
        ui=dataclasses.replace(UiConfig(), window=False),
    )
    a = app_mod.NovaApp(config=cfg)
    a.synth = _Synth()
    a.player = _Player()
    a.vad = types.SimpleNamespace(available=True)
    a.duplex_gate = types.SimpleNamespace(mic_open=lambda: True)  # app not start()ed in tests
    a._confirm_timeout_s = 0.5
    yield a
    import nova.security.untrusted as untrusted_mod

    untrusted_mod._AUDIT = None


async def _ask_while_holding_lock(app, feed):
    async with app._turn_lock:
        task = asyncio.create_task(app._ask_by_voice("Should I open notepad?"))
        for _ in range(50):
            if app._confirm_future is not None:
                break
            await asyncio.sleep(0.01)
        await feed()
        return await task


async def test_spoken_answer_is_heard_while_the_asking_turn_holds_the_lock(app):
    app.transcriber = _Transcriber("Yes.")
    app.segmenter = _Seg()

    async def feed():
        app._feed_prefix_listener(np.zeros(480, dtype=np.float32))
        await asyncio.sleep(0.05)

    assert await _ask_while_holding_lock(app, feed) == "Yes."
    assert app.synth.said[-1] == "Should I open notepad?"
    assert app._confirm_future is None


async def test_typed_answer_also_counts(app):
    app.transcriber = _Transcriber("unused")

    async def feed():
        await app._on_typed_text("yes")

    assert await _ask_while_holding_lock(app, feed) == "yes"


async def test_silence_times_out(app):
    app.transcriber = _Transcriber("unused")

    async def feed():
        pass

    assert await _ask_while_holding_lock(app, feed) is None


async def test_muted_mic_means_voice_unavailable(app):
    app._listening = False
    assert await app._ask_by_voice("Should I?") is VOICE_UNAVAILABLE


async def _mic(app):
    """Stands in for the capture loop: one frame every 10 ms."""
    while True:
        app._feed_prefix_listener(np.zeros(480, dtype=np.float32))
        await asyncio.sleep(0.01)


async def test_claude_proposal_is_approved_by_voice_end_to_end(app, monkeypatch):
    """The 2026-10-14 case: a misheard command -> Claude proposes an ACT
    skill -> NOVA asks out loud -> "yes" -> it runs. No dialog involved."""
    import nova.system.winops as winops

    pressed = []
    monkeypatch.setattr(winops, "press_keys", lambda *k, **kw: pressed.append(k))

    async def proposal(*_a, **_kw):
        return app_mod.ClaudeProposal(skill_name="media_play_pause", params={})

    monkeypatch.setattr(app_mod.claude_arm, "ask", proposal)
    app.transcriber = _Transcriber("yes")
    app.segmenter = _Seg()
    feeder = asyncio.create_task(_mic(app))
    reply = await app.run_text("pores the moosic", source="voice", armed_by=ArmedBy.VOICE)
    feeder.cancel()
    assert any(s.startswith("Should I") for s in app.synth.said), app.synth.said
    assert pressed, "approved action never ran"
    assert reply is not None and reply.success


async def test_claude_proposal_declined_by_voice_does_not_run(app, monkeypatch):
    import nova.system.winops as winops

    pressed = []
    monkeypatch.setattr(winops, "press_keys", lambda *k, **kw: pressed.append(k))

    async def proposal(*_a, **_kw):
        return app_mod.ClaudeProposal(skill_name="media_play_pause", params={})

    monkeypatch.setattr(app_mod.claude_arm, "ask", proposal)
    app.transcriber = _Transcriber("no")
    app.segmenter = _Seg()
    feeder = asyncio.create_task(_mic(app))
    await app.run_text("pores the moosic", source="voice", armed_by=ArmedBy.VOICE)
    feeder.cancel()
    assert pressed == []


class _TwoUtteranceSeg(_Seg):
    """Returns two utterances, on the first two process() calls."""

    def __init__(self):
        super().__init__()
        self.queue = [np.zeros(1600, dtype=np.float32), np.ones(1600, dtype=np.float32)]

    def process(self, frame):
        return self.queue.pop(0) if self.queue else None


class _OrderedTranscriber:
    """First utterance ("no wait") is slow to transcribe, the second ("yes")
    is fast -- the shape of a real long-then-short pair."""

    def transcribe(self, pcm):
        import time as _t

        if float(pcm[0]) == 0.0:
            _t.sleep(0.15)
            return "No wait, don't."
        return "yes"


async def test_two_answers_in_one_window_are_decided_in_the_order_spoken(app):
    """Security review 2026-10-15: each utterance in the confirm window got
    its own concurrent transcription and whichever FINISHED first decided,
    so a later "yes" could override an earlier "no wait". The first thing
    said must decide."""
    app.transcriber = _OrderedTranscriber()
    app.segmenter = _TwoUtteranceSeg()
    app._confirm_timeout_s = 2.0

    async def feed():
        app._feed_prefix_listener(np.zeros(480, dtype=np.float32))
        app._feed_prefix_listener(np.zeros(480, dtype=np.float32))
        await asyncio.sleep(0.4)

    assert await _ask_while_holding_lock(app, feed) == "No wait, don't."

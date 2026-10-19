"""Contract: nova/app.py prefix-wake flow (Manav, 2026-10-14).

While the window is open, every utterance is transcribed locally and acted
on ONLY if it starts with NOVA's name (nova/brain/address.py) or arrives
inside the follow-up window after a bare "Nova". Everything else is dropped
unread: no turn, no speech, no audit record, and its text never reaches a
log line.

Fixture pattern copied from tests/test_app_turn_loop.py: AuditLog -> tmp_path,
fake transcriber/synth/player, and nova.app.claude_arm.ask stubbed so the
real `claude` CLI is never spawned. The clock is faked by swapping the
`time` reference inside nova.app only (never the global time module -- the
asyncio loop reads it), and nothing sleeps for real.
"""
from __future__ import annotations

import dataclasses
import logging
import time as _real_time
from pathlib import Path

import numpy as np
import pytest

import nova.app as app_mod
from nova.config import Config, UiConfig, WakeConfig, load_config
from nova.security.audit import AuditLog
from nova.security.tiers import ArmedBy

REPO_ROOT = Path(__file__).resolve().parent.parent
OVERHEARD = "so I told him the meeting was moved"


class _FakeTranscriber:
    def __init__(self, text="what time is it"):
        self.text = text
        self.calls = 0

    def transcribe(self, pcm):
        self.calls += 1
        return self.text


class _FakeSynth:
    def __init__(self):
        self.said: list[str] = []

    def say(self, text):
        self.said.append(text)
        return np.zeros(4, dtype=np.float32), 16000


class _FakePlayer:
    def __init__(self):
        self.played = []

    async def play(self, pcm, samplerate, *, on_first_frame=None):
        self.played.append((pcm, samplerate))
        if on_first_frame is not None:
            on_first_frame()


class _FakeClock:
    """Stand-in for the `time` module as seen from nova.app only."""

    def __init__(self, start=1000.0):
        self.now = start

    def monotonic(self):
        return self.now

    def __getattr__(self, name):
        return getattr(_real_time, name)


class _SpySegmenter:
    def __init__(self, returns=None):
        self.process_calls = 0
        self.reset_calls = 0
        self.in_speech = False
        self._returns = returns

    def process(self, frame):
        self.process_calls += 1
        return self._returns

    def reset(self):
        self.reset_calls += 1
        self.in_speech = False


def _prefix_config(**wake_kw) -> Config:
    return dataclasses.replace(
        Config(),
        wake=dataclasses.replace(WakeConfig(), mode="prefix", **wake_kw),
        ui=dataclasses.replace(UiConfig(), window=False),
    )


@pytest.fixture
def clock(monkeypatch):
    c = _FakeClock()
    monkeypatch.setattr(app_mod, "time", c)
    return c


@pytest.fixture
def app(tmp_path, monkeypatch, clock):
    monkeypatch.setattr(
        app_mod, "AuditLog",
        lambda **kw: AuditLog(path=tmp_path / "audit.log", redact_terms=kw.get("redact_terms", ())),
    )

    async def _no_real_claude(*_a, **_kw):
        return app_mod.ClaudeFailure(reason="cli_not_found", detail="stubbed in tests")

    monkeypatch.setattr(app_mod.claude_arm, "ask", _no_real_claude)
    application = app_mod.NovaApp(config=_prefix_config(follow_up_ms=8000))
    application.transcriber = _FakeTranscriber()
    application.synth = _FakeSynth()
    application.player = _FakePlayer()

    # spy on run_text without replacing its behaviour
    application.run_text_calls = []
    real_run_text = application.run_text

    async def spy_run_text(text, **kw):
        application.run_text_calls.append((text, kw))
        return await real_run_text(text, **kw)

    application.run_text = spy_run_text
    yield application
    import nova.security.untrusted as untrusted_mod

    untrusted_mod._AUDIT = None


def _audit_lines(tmp_path) -> list[str]:
    p = tmp_path / "audit.log"
    if not p.exists():
        return []
    return [l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def _pcm():
    return np.zeros(16000, dtype=np.float32)


def _frame():
    return np.zeros(480, dtype=np.float32)


# ---------------------------------------------------------------------------
# setup sanity
# ---------------------------------------------------------------------------


def test_fixture_is_really_in_prefix_mode_without_a_window(app):
    assert app._prefix_mode is True
    assert app.window is None


# ---------------------------------------------------------------------------
# addressed / not addressed
# ---------------------------------------------------------------------------


async def test_addressed_utterance_runs_a_voice_armed_turn_with_the_name_stripped(app, tmp_path):
    app.transcriber.text = "Nova, what time is it"
    await app._on_utterance(_pcm())

    assert app.transcriber.calls == 1
    assert len(app.run_text_calls) == 1
    text, kw = app.run_text_calls[0]
    assert text == "what time is it"
    assert kw["armed_by"] is ArmedBy.VOICE
    # the real turn ran: the time skill answered and was audited
    assert len(app.synth.said) == 1
    assert app.synth.said[0] not in ("Yes?", "I didn't catch that.")
    assert len(_audit_lines(tmp_path)) == 1


async def test_hey_nova_variant_also_runs(app):
    app.transcriber.text = "Hey, Nova! what time is it"
    await app._on_utterance(_pcm())
    assert [t for t, _ in app.run_text_calls] == ["what time is it"]


async def test_overheard_speech_is_dropped_unread_and_unlogged(app, tmp_path, caplog):
    app.transcriber.text = OVERHEARD
    with caplog.at_level(logging.DEBUG):
        await app._on_utterance(_pcm())

    # the target path actually ran: it was transcribed and judged ...
    assert app.transcriber.calls == 1
    assert any("not addressed" in r.getMessage() for r in caplog.records), (
        "positive control: caplog must capture nova.app's ignore message, "
        "or the absence check below proves nothing"
    )
    # ... and dropped
    assert app.run_text_calls == []
    assert app.synth.said == []
    assert app.player.played == []
    assert _audit_lines(tmp_path) == []
    for rec in caplog.records:
        assert OVERHEARD not in rec.getMessage()
        assert "meeting was moved" not in rec.getMessage()
    assert "meeting was moved" not in caplog.text


async def test_mention_not_address_is_dropped(app):
    app.transcriber.text = "I told Nova yesterday to open notepad"
    await app._on_utterance(_pcm())
    assert app.transcriber.calls == 1
    assert app.run_text_calls == []
    assert app.synth.said == []


async def test_overheard_text_does_not_reach_ui_heard_topic(app):
    heard = []

    async def on_heard(payload):
        heard.append(payload)

    app.bus.subscribe(app_mod.TOPIC_UI_HEARD, on_heard)
    app.transcriber.text = OVERHEARD
    await app._on_utterance(_pcm())
    assert heard == []

    # positive control: an addressed utterance does reach it
    app.transcriber.text = "Nova what time is it"
    await app._on_utterance(_pcm())
    assert heard == ["what time is it"]


async def test_transcription_failure_is_dropped_quietly(app, tmp_path):
    class Boom:
        calls = 0

        def transcribe(self, pcm):
            Boom.calls += 1
            raise RuntimeError("stt down")

    app.transcriber = Boom()
    await app._on_utterance(_pcm())
    assert Boom.calls == 1
    assert app.run_text_calls == []
    assert app.synth.said == []
    assert _audit_lines(tmp_path) == []
    assert not app._turn_lock.locked()


# ---------------------------------------------------------------------------
# bare "Nova." -> "Yes?" -> follow-up window
# ---------------------------------------------------------------------------


async def test_bare_name_says_yes_then_unnamed_follow_up_is_a_command(app, clock):
    app.transcriber.text = "Nova."
    await app._on_utterance(_pcm())
    assert app.synth.said == ["Yes?"]
    assert app.run_text_calls == []

    clock.now += 7.9  # inside the 8 s window
    app.transcriber.text = "what time is it"
    await app._on_utterance(_pcm())
    assert len(app.run_text_calls) == 1
    text, kw = app.run_text_calls[0]
    assert text == "what time is it"
    assert kw["armed_by"] is ArmedBy.VOICE


async def test_follow_up_window_is_one_shot(app, clock):
    app.transcriber.text = "Nova"
    await app._on_utterance(_pcm())
    app.transcriber.text = "what time is it"
    await app._on_utterance(_pcm())
    assert len(app.run_text_calls) == 1

    # a second unnamed sentence right after is overheard speech again
    clock.now += 1.0
    app.transcriber.text = "open notepad"
    await app._on_utterance(_pcm())
    assert len(app.run_text_calls) == 1


async def test_follow_up_after_window_expires_is_ignored(app, clock):
    app.transcriber.text = "Nova."
    await app._on_utterance(_pcm())
    assert app.synth.said == ["Yes?"]

    clock.now += 8.01  # past follow_up_ms = 8000
    app.transcriber.text = "open notepad"
    await app._on_utterance(_pcm())
    assert app.transcriber.calls == 2
    assert app.run_text_calls == []
    assert app.synth.said == ["Yes?"]


async def test_follow_up_without_name_open_notepad_is_treated_as_command(app, clock):
    app.transcriber.text = "Nova."
    await app._on_utterance(_pcm())
    clock.now += 2.0
    app.transcriber.text = "open notepad"
    await app._on_utterance(_pcm())
    assert [t for t, _ in app.run_text_calls] == ["open notepad"]
    assert app.run_text_calls[0][1]["armed_by"] is ArmedBy.VOICE


async def test_mic_toggle_cancels_a_pending_follow_up(app, clock):
    app.transcriber.text = "Nova."
    await app._on_utterance(_pcm())
    await app._on_mic_toggle(False)
    await app._on_mic_toggle(True)
    app.transcriber.text = "what time is it"
    await app._on_utterance(_pcm())
    assert app.run_text_calls == []


# ---------------------------------------------------------------------------
# turn-lock, typed text
# ---------------------------------------------------------------------------


async def test_utterance_while_turn_lock_held_is_dropped(app):
    app.transcriber.text = "Nova, what time is it"
    await app._turn_lock.acquire()
    try:
        await app._on_utterance(_pcm())
    finally:
        app._turn_lock.release()
    assert app.transcriber.calls == 0
    assert app.run_text_calls == []
    assert app.synth.said == []

    # positive control: same utterance with the lock free does run
    await app._on_utterance(_pcm())
    assert len(app.run_text_calls) == 1


async def test_typed_text_runs_a_typed_armed_turn(app, tmp_path):
    await app._on_typed_text("what time is it")
    assert len(app.run_text_calls) == 1
    text, kw = app.run_text_calls[0]
    assert text == "what time is it"
    assert kw["armed_by"] is ArmedBy.TYPED
    assert app.transcriber.calls == 0  # no STT for typed text
    assert len(app.synth.said) == 1
    assert len(_audit_lines(tmp_path)) == 1


@pytest.mark.parametrize("blank", ["", "   ", None])
async def test_blank_typed_text_runs_nothing(app, blank):
    await app._on_typed_text(blank)
    assert app.run_text_calls == []
    assert app.synth.said == []


# ---------------------------------------------------------------------------
# _feed_prefix_listener gating
# ---------------------------------------------------------------------------


@pytest.fixture
def spy_seg(app):
    spy = _SpySegmenter()
    app.segmenter = spy
    app.vad.available = True  # so the VAD check is not what short-circuits
    return spy


def test_feed_reaches_the_segmenter_when_listening_positive_control(app, spy_seg):
    app._feed_prefix_listener(_frame())
    assert spy_seg.process_calls == 1


async def test_mic_toggle_off_stops_feeding_the_segmenter(app, spy_seg):
    await app._on_mic_toggle(False)
    for _ in range(20):
        app._feed_prefix_listener(_frame())
    assert spy_seg.process_calls == 0

    await app._on_mic_toggle(True)
    app._feed_prefix_listener(_frame())
    assert spy_seg.process_calls == 1


def test_feed_does_nothing_while_vad_unavailable(app, spy_seg):
    app.vad.available = False
    app._feed_prefix_listener(_frame())
    assert spy_seg.process_calls == 0


async def test_feed_does_nothing_while_turn_lock_held(app, spy_seg):
    await app._turn_lock.acquire()
    try:
        app._feed_prefix_listener(_frame())
    finally:
        app._turn_lock.release()
    assert spy_seg.process_calls == 0


def test_feed_does_nothing_while_nova_is_speaking(app, spy_seg):
    """NOVA must not hear its own voice: while DuplexGate says the mic is
    closed (NOVA speaking), frames must not reach the segmenter."""
    app.duplex_gate.on_speak_start()
    assert app.duplex_gate.mic_open() is False  # precondition, via the gate's own API
    for _ in range(10):
        app._feed_prefix_listener(_frame())
    assert spy_seg.process_calls == 0, (
        "_feed_prefix_listener fed the segmenter while NOVA was speaking -- "
        "nova/app.py checks `self.duplex_gate.mic_open` (the bound method, "
        "always truthy) instead of calling `mic_open()`"
    )


def test_feed_resets_an_in_progress_utterance_when_nova_starts_speaking(app, spy_seg):
    spy_seg.in_speech = True
    app.duplex_gate.on_speak_start()
    app._feed_prefix_listener(_frame())
    assert spy_seg.reset_calls >= 1
    assert spy_seg.process_calls == 0


async def test_feed_emitting_an_utterance_schedules_on_utterance(app):
    spy = _SpySegmenter(returns=_pcm())
    app.segmenter = spy
    app.vad.available = True
    app.transcriber.text = "Nova, what time is it"
    app._feed_prefix_listener(_frame())
    tasks = list(app._utterance_tasks)
    assert len(tasks) == 1
    await tasks[0]
    assert [t for t, _ in app.run_text_calls] == ["what time is it"]


# ---------------------------------------------------------------------------
# ACT tier reachable by voice (Manav's 2026-10-14 ruling), end to end
# ---------------------------------------------------------------------------


async def test_voice_armed_open_notepad_launches_and_wake_still_refused(tmp_path, monkeypatch, clock):
    import nova.skills.open_app as open_app_mod
    from nova.config import SecurityConfig

    calls = []

    class FakePopen:
        def __init__(self, argv, **kw):
            calls.append(argv)

    monkeypatch.setattr(open_app_mod, "subprocess", type("S", (), {"Popen": FakePopen})())
    monkeypatch.setattr(
        app_mod, "AuditLog",
        lambda **kw: AuditLog(path=tmp_path / "audit.log", redact_terms=kw.get("redact_terms", ())),
    )

    async def _no_real_claude(*_a, **_kw):
        return app_mod.ClaudeFailure(reason="cli_not_found", detail="stubbed in tests")

    monkeypatch.setattr(app_mod.claude_arm, "ask", _no_real_claude)
    trusted = tmp_path / "Trusted"
    trusted.mkdir()
    exe = trusted / "notepad.exe"
    exe.write_bytes(b"fake")
    cfg = dataclasses.replace(
        _prefix_config(),
        security=SecurityConfig(allowed_apps={"notepad": str(exe)}, allowed_app_dirs=[str(trusted)]),
    )
    application = app_mod.NovaApp(config=cfg)
    application.transcriber = _FakeTranscriber("Nova, open notepad")
    application.synth = _FakeSynth()
    application.player = _FakePlayer()
    try:
        await application._on_utterance(_pcm())
        assert len(calls) == 1, "voice-armed ACT skill should have launched"
        assert application.synth.said != ["I can't do that unless you press the hotkey."]

        # negative control on the same app: WAKE-armed is still capped at READ
        await application.run_text("open notepad", source="wake", armed_by=ArmedBy.WAKE)
        assert len(calls) == 1
        assert application.synth.said[-1] == "I can't do that unless you press the hotkey."
    finally:
        import nova.security.untrusted as untrusted_mod

        untrusted_mod._AUDIT = None


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_shipped_config_selects_prefix_wake_and_window(tmp_path):
    cfg = load_config(
        REPO_ROOT / "config" / "nova.yaml",
        learned_apps_path=tmp_path / "learned_apps.json",
        audit_log_path=tmp_path / "audit.log",
    )
    assert cfg.wake.mode == "prefix"
    assert [n.lower() for n in cfg.wake.names] == ["nova"]
    assert cfg.ui.window is True


def test_unknown_wake_mode_fails_closed_to_the_restrictive_model_mode(tmp_path, monkeypatch, caplog):
    src = (REPO_ROOT / "config" / "nova.yaml").read_text(encoding="utf-8")
    assert "  mode: prefix" in src
    cfg_path = tmp_path / "nova.yaml"
    cfg_path.write_text(src.replace("  mode: prefix", "  mode: banana", 1), encoding="utf-8")
    cfg = load_config(
        cfg_path, learned_apps_path=tmp_path / "learned_apps.json", audit_log_path=tmp_path / "audit.log",
    )
    assert cfg.wake.mode == "banana"

    monkeypatch.setattr(
        app_mod, "AuditLog",
        lambda **kw: AuditLog(path=tmp_path / "audit.log", redact_terms=kw.get("redact_terms", ())),
    )
    cfg = dataclasses.replace(cfg, ui=dataclasses.replace(cfg.ui, window=False))
    with caplog.at_level(logging.WARNING):
        application = app_mod.NovaApp(config=cfg)
    try:
        # fail CLOSED: a typo must never enable action-capable voice commands
        assert application._prefix_mode is False
        assert any("banana" in r.getMessage() and "restrictive" in r.getMessage() for r in caplog.records)
    finally:
        import nova.security.untrusted as untrusted_mod

        untrusted_mod._AUDIT = None


def test_model_mode_is_not_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(
        app_mod, "AuditLog",
        lambda **kw: AuditLog(path=tmp_path / "audit.log", redact_terms=kw.get("redact_terms", ())),
    )
    cfg = dataclasses.replace(Config(), wake=dataclasses.replace(WakeConfig(), mode="model"))
    application = app_mod.NovaApp(config=cfg)
    try:
        assert application._prefix_mode is False
    finally:
        import nova.security.untrusted as untrusted_mod

        untrusted_mod._AUDIT = None


# ---------------------------------------------------------------------------
# Security review 2026-10-14 regressions
# ---------------------------------------------------------------------------


async def test_noise_during_follow_up_cannot_keep_the_window_open(app, clock):
    """Finding #1: empty transcripts (noise, dropped hallucinations) used to
    re-arm the follow-up window, so a TV kept NOVA in no-name-needed mode
    forever. Now the deadline set after "Yes?" is a hard limit."""
    app.transcriber.text = "Nova."
    await app._on_utterance(_pcm())
    assert app.synth.said == ["Yes?"]

    for _ in range(5):  # five noise utterances, 7.9 s apart
        clock.now += 7.9
        app.transcriber.text = ""
        await app._on_utterance(_pcm())
    assert app.synth.said == ["Yes?"]  # never re-armed

    app.transcriber.text = "close all my windows"  # overheard, well past 8 s
    await app._on_utterance(_pcm())
    assert app.run_text_calls == []


async def test_repeated_bare_name_inside_follow_up_does_not_extend_it(app, clock):
    app.transcriber.text = "Nova"
    await app._on_utterance(_pcm())
    clock.now += 7.0
    app.transcriber.text = "Nova"
    await app._on_utterance(_pcm())
    assert app.synth.said == ["Yes?"]
    clock.now += 1.5  # 8.5 s after the first "Yes?" -- expired
    app.transcriber.text = "open notepad"
    await app._on_utterance(_pcm())
    assert app.run_text_calls == []


async def test_yes_is_spoken_under_the_turn_lock(app):
    """Finding #3: nothing may talk over the acknowledgement."""
    held = []

    async def spy_speak(text):
        held.append(app._turn_lock.locked())

    app._speak = spy_speak
    app.transcriber.text = "Nova"
    await app._on_utterance(_pcm())
    assert held == [True]


async def test_utterance_finishing_after_mute_does_nothing(app):
    """Finding #6: muting wins over an utterance already being transcribed."""
    app._listening = False
    app.transcriber.text = "Nova, open notepad"
    await app._on_utterance(_pcm())
    assert app.run_text_calls == []


def test_window_that_dies_on_its_own_shuts_nova_down():
    """Finding #2: NOVA only listens while its window is open. If the window
    cannot open (no tkinter, build error) it must request shutdown rather
    than leave a live microphone with nothing on screen."""
    import asyncio as _asyncio

    from nova.bus import EventBus
    from nova.interfaces.window import NovaWindow

    closed = []

    async def _noop(*_a):
        return None

    async def main():
        w = NovaWindow(EventBus(), on_close=lambda: closed.append(1), on_text=_noop, on_mic_toggle=_noop)
        w._run_tk = lambda: None  # the window failed to open
        w.start(loop=_asyncio.get_running_loop())
        w._thread.join(timeout=2)
        await _asyncio.sleep(0.05)  # let the posted callback run

    _asyncio.run(main())
    assert closed == [1]


def test_window_closed_by_app_shutdown_does_not_request_close_again():
    import asyncio as _asyncio

    from nova.bus import EventBus
    from nova.interfaces.window import NovaWindow

    closed = []

    async def _noop(*_a):
        return None

    async def main():
        w = NovaWindow(EventBus(), on_close=lambda: closed.append(1), on_text=_noop, on_mic_toggle=_noop)
        w._app_stopping = True
        w._run_tk = lambda: None
        w.start(loop=_asyncio.get_running_loop())
        w._thread.join(timeout=2)
        await _asyncio.sleep(0.05)

    _asyncio.run(main())
    assert closed == []


async def test_calibrate_logs_only_the_first_word_of_ignored_speech(tmp_path, monkeypatch, clock, caplog):
    monkeypatch.setattr(
        app_mod, "AuditLog",
        lambda **kw: AuditLog(path=tmp_path / "audit.log", redact_terms=kw.get("redact_terms", ())),
    )

    async def _no_real_claude(*_a, **_kw):
        return app_mod.ClaudeFailure(reason="cli_not_found")

    monkeypatch.setattr(app_mod.claude_arm, "ask", _no_real_claude)
    application = app_mod.NovaApp(config=_prefix_config(calibrate=True))
    application.transcriber = _FakeTranscriber(text="Noah, what time is it")
    application.synth = _FakeSynth()
    application.player = _FakePlayer()
    try:
        with caplog.at_level(logging.DEBUG):
            await application._on_utterance(_pcm())
        logged = " ".join(r.getMessage() for r in caplog.records)
        assert "'noah'" in logged
        assert "what time" not in logged
    finally:
        import nova.security.untrusted as untrusted_mod

        untrusted_mod._AUDIT = None


def test_app_hints_whisper_with_novas_name():
    import nova.app as _m

    seen = {}

    class _T:
        def __init__(self, **kw):
            seen.update(kw)

    orig = _m.Transcriber
    _m.Transcriber = _T
    try:
        a = _m.NovaApp(config=_prefix_config())
    finally:
        _m.Transcriber = orig
        import nova.security.untrusted as untrusted_mod

        untrusted_mod._AUDIT = None
    assert seen["hotwords"].startswith("Nova, ")  # the name always first
    assert "Notepad" in seen["hotwords"]  # plus command vocabulary (2026-10-15)


async def test_window_greets_out_loud_once_models_are_loaded(app):
    app.config = dataclasses.replace(app.config, ui=dataclasses.replace(app.config.ui, greet=True))
    await app._prewarm()
    assert app.synth.said == ["NOVA's ready."]

"""End-to-end smoke test of prefix wake through the REAL audio path.

Real microphone frames are replaced by a recorded voice (tests/fixtures/
speech_espeak_16k.wav, "Nova, what time is it? Please open notepad for me.")
pushed through a fake Capture. Everything after that is real: NovaApp.start(),
the continuous capture loop, DuplexGate, the real Silero VAD model, the
UtteranceSegmenter, _on_utterance and run_text. Only Whisper (too big for a
unit test), speech output and Claude are faked.

This is the test that would have caught the 2026-10-14 bug where the VAD was
fed wrongly and NOVA, window open for 26 minutes, never heard a word.
Skipped where the real VAD model file is absent.
"""
from __future__ import annotations

import asyncio
import dataclasses
import queue
from pathlib import Path

import numpy as np
import pytest

import nova.app as app_mod
from nova import VAD_MODEL_PATH
from nova.config import Config, UiConfig, WakeConfig
from nova.security.audit import AuditLog
from nova.security.tiers import ArmedBy

SPEECH = Path(__file__).parent / "fixtures" / "speech_espeak_16k.wav"

pytestmark = pytest.mark.skipif(not VAD_MODEL_PATH.exists(), reason="real silero_vad.onnx not on this machine")


class _ScriptedCapture:
    """Serves a fixed recording as 30 ms frames, then silence."""

    def __init__(self, audio: np.ndarray):
        self._frames: queue.Queue[np.ndarray] = queue.Queue()
        for i in range(0, len(audio) - 480, 480):
            self._frames.put(audio[i:i + 480].astype(np.float32))
        self.started = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def read(self, timeout=None):
        try:
            return self._frames.get_nowait()
        except queue.Empty:
            return np.zeros(480, dtype=np.float32)


class _FakeTranscriber:
    def __init__(self):
        self.lengths_s: list[float] = []

    def transcribe(self, pcm):
        self.lengths_s.append(len(pcm) / 16000)
        return "Nova, what time is it?"

    def load(self):
        pass

    def unload(self):
        pass


class _Quiet:
    def __init__(self):
        self.said = []

    def say(self, text):
        self.said.append(text)
        return np.zeros(4, dtype=np.float32), 16000

    def load(self):
        pass

    def unload(self):
        pass


class _Player:
    async def play(self, pcm, samplerate, *, on_first_frame=None):
        if on_first_frame:
            on_first_frame()


async def test_spoken_nova_command_through_the_real_vad_reaches_a_voice_turn(tmp_path, monkeypatch):
    import soundfile as sf

    speech, sr = sf.read(str(SPEECH), dtype="float32")
    assert sr == 16000
    rng = np.random.default_rng(0)
    audio = np.concatenate([np.zeros(16000, np.float32), speech, np.zeros(32000, np.float32)])
    audio = audio + (rng.standard_normal(len(audio)) * 0.003).astype(np.float32)

    monkeypatch.setattr(app_mod, "AuditLog", lambda **kw: AuditLog(path=tmp_path / "audit.log"))

    async def _no_claude(*_a, **_kw):
        return app_mod.ClaudeFailure(reason="cli_not_found")

    monkeypatch.setattr(app_mod.claude_arm, "ask", _no_claude)
    cfg = dataclasses.replace(
        Config(),
        wake=dataclasses.replace(WakeConfig(), mode="prefix"),
        ui=dataclasses.replace(UiConfig(), window=False),
    )
    app = app_mod.NovaApp(config=cfg)
    app._capture = _ScriptedCapture(audio)
    app.transcriber = _FakeTranscriber()
    app.synth = _Quiet()
    app.player = _Player()
    app.hotkey.start = lambda loop=None: None  # no global keyboard hook in tests
    turns = []
    real_run_text = app.run_text

    async def spy(text, **kw):
        turns.append((text, kw["armed_by"]))
        return await real_run_text(text, **kw)

    app.run_text = spy

    await app.start()
    try:
        assert app.vad.available, "real VAD model failed to load"
        for _ in range(200):  # up to ~10 s of wall clock, normally < 2 s
            if turns:
                break
            await asyncio.sleep(0.05)
    finally:
        await app.stop()
        import nova.security.untrusted as untrusted_mod

        untrusted_mod._AUDIT = None

    assert app.transcriber.lengths_s, "the VAD never cut an utterance out of real speech"
    assert 3.0 < app.transcriber.lengths_s[0] < 7.0  # ~4.2 s of speech + pre-roll + tail
    assert turns == [("what time is it?", ArmedBy.VOICE)]
    assert any(said.startswith("It's") for said in app.synth.said)  # the time skill answered

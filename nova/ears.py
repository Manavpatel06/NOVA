"""NOVA's ears on a device (ADR 0024): an always-open mic whose sentences go
to NOVA on the hub, and a speaker for the replies.

Used by the laptop helper (nova/link.py, sentences sent over Tailscale) and by
the hub's own mic (phase 3, handed over in-process). Only a small speech
detector (Silero VAD, ~2 MB) runs here; no speech-to-text, no voice model.

Every finished utterance goes to the hub, which transcribes it and acts only
when it starts with NOVA's name (or answers NOVA's question). The hub drops
everything else unlogged and unshown; this side never sees words at all.
The mic is ignored while NOVA is speaking (it must not hear itself) and
while a sentence is being handled.
"""
from __future__ import annotations

import base64
import io
import queue
import threading
import time
import wave
from typing import Any, Callable

from nova.log import get_logger

_log = get_logger("ears")

AFTER_SPEECH_S = 0.4   # keep ignoring the mic this long after NOVA stops talking


def to_pcm16(audio: Any) -> bytes:
    import numpy as np

    return (np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def from_wav(data: bytes):
    import numpy as np

    with wave.open(io.BytesIO(data)) as w:
        if w.getsampwidth() != 2:
            raise ValueError("expected 16-bit audio")
        frames = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
        if w.getnchannels() > 1:
            frames = frames.reshape(-1, w.getnchannels()).mean(axis=1)
        return frames, w.getframerate()


class Ears:
    def __init__(
        self,
        send: Callable[[bytes], dict],
        *,
        listening: Callable[[], bool] = lambda: True,
        silence_ms: int = 800,
        max_ms: int = 15000,
        min_speech_ms: int = 300,
        play: Callable[[bytes], None] | None = None,
    ) -> None:
        self._send = send
        self._listening = listening
        self._seg_args = dict(silence_ms=silence_ms, max_ms=max_ms, min_speech_ms=min_speech_ms)
        self._play = play or play_wav
        self._busy = threading.Event()
        self._deaf_until = 0.0
        self._work: "queue.Queue[bytes]" = queue.Queue(maxsize=2)

    # --- the loop (its own thread) ------------------------------------------

    def run(self, stop: threading.Event) -> None:
        from nova.audio.capture import Capture
        from nova.audio.utterance import UtteranceSegmenter
        from nova.audio.vad import Vad

        vad = Vad()
        vad.load()
        if not vad.available:
            _log.error("Ears are off: the speech detector (models/vad/silero_vad.onnx) is missing")
            return
        seg = UtteranceSegmenter(vad, **self._seg_args)
        try:
            cap = Capture()
            cap.start()
        except Exception as exc:  # noqa: BLE001 -- no mic: say so, keep everything else running
            _log.error("Ears are off: couldn't open the microphone (%s)", exc)
            return
        worker = threading.Thread(target=self._worker, args=(stop,), name="ears-send", daemon=True)
        worker.start()
        _log.info("Listening for \"Nova, ...\"")
        try:
            while not stop.is_set():
                try:
                    frame = cap.read(timeout=0.5)
                except queue.Empty:
                    continue
                if not self._listening() or self._busy.is_set() or time.monotonic() < self._deaf_until:
                    if seg.in_speech:
                        seg.reset()
                    continue
                utterance = seg.process(frame)
                if utterance is not None:
                    self.feed(to_pcm16(utterance))
        finally:
            cap.stop()

    def feed(self, pcm16: bytes) -> None:
        """One finished utterance (16 kHz mono int16)."""
        try:
            self._busy.set()
            self._work.put_nowait(pcm16)
        except queue.Full:
            pass

    def _worker(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                pcm16 = self._work.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.handle(pcm16)
            finally:
                self._busy.clear()

    def handle(self, pcm16: bytes) -> dict:
        try:
            out = self._send(pcm16)
        except Exception as exc:  # noqa: BLE001 -- the hub may be restarting
            _log.warning("Couldn't reach NOVA on the hub (%s)", type(exc).__name__)
            return {}
        if not isinstance(out, dict) or out.get("ignored"):
            return out if isinstance(out, dict) else {}
        audio = out.get("audio")
        if isinstance(audio, str) and audio:
            try:
                self._play(base64.b64decode(audio))
            except Exception as exc:  # noqa: BLE001
                _log.warning("Couldn't play NOVA's reply (%s)", type(exc).__name__)
            finally:
                self._deaf_until = time.monotonic() + AFTER_SPEECH_S
        return out


def play_wav(data: bytes) -> None:
    """Play a WAV on the default speaker and wait until it's done."""
    import sounddevice as sd

    frames, rate = from_wav(data)
    sd.play(frames, rate)
    sd.wait()

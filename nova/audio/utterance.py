"""Cut the continuous microphone stream into utterances (prefix wake).

Feeds every 16 kHz frame through Silero VAD (`nova.audio.vad.Vad`) and
returns one complete utterance -- pre-roll + speech + a little trailing
silence -- each time somebody stops talking. Pure and synchronous: no
threads, no bus, so it is trivially testable with a scripted VAD.

What happens to the utterance (transcribe, check for "Nova ...", discard)
is the app's decision, not this module's.
"""
from __future__ import annotations

from collections import deque
from typing import Protocol

import numpy as np

SAMPLERATE = 16000


class _VadLike(Protocol):
    def process(self, frame: np.ndarray) -> float: ...
    def reset(self) -> None: ...


class UtteranceSegmenter:
    """Streaming speech segmenter.

    * Speech starts after `start_frames` consecutive frames at or above
      `threshold` (debounces clicks).
    * `preroll_ms` of audio before that point is kept, so the first syllable
      -- usually the name "Nova" -- is never clipped.
    * Speech ends after `silence_ms` of continuous non-speech, or is cut at
      `max_ms` so a TV left on cannot grow one utterance forever.
    * Utterances with less than `min_speech_ms` of actual speech are dropped
      (coughs, door slams).
    """

    def __init__(
        self,
        vad: _VadLike,
        *,
        silence_ms: int = 800,
        max_ms: int = 15000,
        min_speech_ms: int = 300,
        preroll_ms: int = 300,
        threshold: float = 0.5,
        start_frames: int = 3,
    ) -> None:
        self._vad = vad
        self._silence_ms = silence_ms
        self._max_ms = max_ms
        self._min_speech_ms = min_speech_ms
        self._preroll_ms = preroll_ms
        self._threshold = threshold
        self._start_frames = max(1, start_frames)

        self._preroll: deque[np.ndarray] = deque()
        self._preroll_len_ms = 0.0
        self._pending: list[np.ndarray] = []  # candidate start frames, not yet confirmed speech
        self._in_speech = False
        self._buf: list[np.ndarray] = []
        self._buf_ms = 0.0
        self._speech_ms = 0.0
        self._silence_run_ms = 0.0

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    def reset(self) -> None:
        """Drop everything buffered (e.g. NOVA started speaking)."""
        self._preroll.clear()
        self._preroll_len_ms = 0.0
        self._pending = []
        self._in_speech = False
        self._buf = []
        self._buf_ms = 0.0
        self._speech_ms = 0.0
        self._silence_run_ms = 0.0
        try:
            self._vad.reset()
        except Exception:  # noqa: BLE001 -- a VAD that can't reset must not take the mic loop down
            pass

    def process(self, frame: np.ndarray) -> np.ndarray | None:
        """Feed one frame; returns a finished utterance or None."""
        if frame is None or len(frame) == 0:
            return None
        frame_ms = 1000.0 * len(frame) / SAMPLERATE
        try:
            prob = float(self._vad.process(frame))
        except Exception:  # noqa: BLE001
            prob = 0.0
        is_speech = prob >= self._threshold

        if not self._in_speech:
            if is_speech:
                self._pending.append(frame)
                if len(self._pending) >= self._start_frames:
                    self._in_speech = True
                    self._buf = list(self._preroll) + self._pending
                    self._buf_ms = self._preroll_len_ms + frame_ms * len(self._pending)
                    self._speech_ms = frame_ms * len(self._pending)
                    self._silence_run_ms = 0.0
                    self._pending = []
                    self._preroll.clear()
                    self._preroll_len_ms = 0.0
                return None
            # not speech: any pending candidate frames were a blip -> preroll
            for f in self._pending:
                self._push_preroll(f)
            self._pending = []
            self._push_preroll(frame)
            return None

        # in speech
        self._buf.append(frame)
        self._buf_ms += frame_ms
        if is_speech:
            self._speech_ms += frame_ms
            self._silence_run_ms = 0.0
        else:
            self._silence_run_ms += frame_ms

        if self._silence_run_ms >= self._silence_ms or self._buf_ms >= self._max_ms:
            return self._finish()
        return None

    def _push_preroll(self, frame: np.ndarray) -> None:
        self._preroll.append(frame)
        self._preroll_len_ms += 1000.0 * len(frame) / SAMPLERATE
        while self._preroll and self._preroll_len_ms > self._preroll_ms:
            old = self._preroll.popleft()
            self._preroll_len_ms -= 1000.0 * len(old) / SAMPLERATE

    def _finish(self) -> np.ndarray | None:
        buf, speech_ms = self._buf, self._speech_ms
        self.reset()
        if speech_ms < self._min_speech_ms or not buf:
            return None
        return np.concatenate(buf).astype(np.float32, copy=False)

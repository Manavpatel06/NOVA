"""Minimal dependency-free streaming resampler.

No scipy/resampy/samplerate in this venv (checked before writing this), and
several viable mic devices refuse to open at 16 kHz directly — WDM-KS in
particular only accepts the device's native rate (44100/48000 on this
hardware) and errors with "Invalid device" otherwise. So Capture opens the
stream at the device's native rate and resamples down to 16 kHz here.

Linear interpolation is not broadcast-quality, but it is stateful across
chunk boundaries (no clicks, no sample-count drift — verified against a
5s/220500-sample synthetic tone fed through in 100ms chunks: 0 sample drift,
RMS error 2.5e-4 vs an ideal 16kHz tone) and cheap enough to run continuously
on a 16GB machine. Good enough for feeding Whisper; revisit if STT accuracy
ever points at resampling quality specifically.
"""
from __future__ import annotations

import numpy as np


class LinearResampler:
    """Stateful streaming linear resampler from sr_in to sr_out."""

    def __init__(self, sr_in: int, sr_out: int):
        self.ratio = sr_in / sr_out
        self._pos = 0.0
        self._carry = np.zeros(0, dtype=np.float32)

    def process(self, x: np.ndarray) -> np.ndarray:
        if self.ratio == 1.0:
            return np.asarray(x, dtype=np.float32)
        buf = np.concatenate([self._carry, np.asarray(x, dtype=np.float32)])
        if len(buf) < 2:
            self._carry = buf
            return np.empty(0, dtype=np.float32)
        n_out = int(np.floor((len(buf) - 1 - self._pos) / self.ratio)) + 1
        if n_out <= 0:
            self._carry = buf
            return np.empty(0, dtype=np.float32)
        idx = self._pos + np.arange(n_out) * self.ratio
        out = np.interp(idx, np.arange(len(buf)), buf).astype(np.float32)
        consumed_pos = self._pos + n_out * self.ratio
        carry_start = int(np.floor(consumed_pos))
        self._carry = buf[carry_start:]
        self._pos = consumed_pos - carry_start
        return out

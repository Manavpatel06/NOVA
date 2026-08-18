"""Playback, genuinely cancellable mid-utterance — barge-in (the user
interrupting NOVA while it's talking) depends on cancel() actually cutting
audio within one output callback block, not at the end of the buffer."""
from __future__ import annotations

import asyncio
import threading

import numpy as np
import sounddevice as sd

from nova.log import get_logger

_log = get_logger(__name__)


class Player:
    """Plays PCM audio through the default output device. cancel() may be
    called from any thread and stops playback within one callback block."""

    def __init__(self):
        self._stream: sd.OutputStream | None = None
        self._cancel_event = threading.Event()
        self._lock = threading.Lock()

    async def play(self, pcm: np.ndarray, samplerate: int) -> None:
        pcm = np.asarray(pcm, dtype=np.float32)
        if pcm.ndim == 1:
            pcm = pcm.reshape(-1, 1)
        n_frames, n_channels = pcm.shape

        loop = asyncio.get_running_loop()
        done: asyncio.Future[None] = loop.create_future()
        self._cancel_event.clear()
        pos = 0

        def _callback(outdata, frames, time_info, status):
            nonlocal pos
            if status:
                _log.warning("Player stream status: %s", status)
            if self._cancel_event.is_set():
                outdata[:] = 0
                raise sd.CallbackStop
            remaining = n_frames - pos
            if remaining <= 0:
                outdata[:] = 0
                raise sd.CallbackStop
            chunk = min(frames, remaining)
            outdata[:chunk] = pcm[pos : pos + chunk]
            if chunk < frames:
                outdata[chunk:] = 0
            pos += chunk
            if pos >= n_frames:
                raise sd.CallbackStop

        def _finished_callback():
            if not done.done():
                loop.call_soon_threadsafe(done.set_result, None)

        with self._lock:
            self._stream = sd.OutputStream(
                samplerate=samplerate,
                channels=n_channels,
                dtype="float32",
                callback=_callback,
                finished_callback=_finished_callback,
            )
            self._stream.start()

        try:
            await done
        finally:
            with self._lock:
                if self._stream is not None:
                    self._stream.close()
                    self._stream = None

    def cancel(self) -> None:
        """Signal the in-flight play() (if any) to stop immediately. Safe to
        call with no playback active, and safe to call from any thread."""
        self._cancel_event.set()

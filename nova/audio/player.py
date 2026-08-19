"""Playback, genuinely cancellable mid-utterance — barge-in (the user
interrupting NOVA while it's talking) depends on cancel() actually cutting
audio within one output callback block, not at the end of the buffer."""
from __future__ import annotations

import asyncio
import threading

import numpy as np
import sounddevice as sd

from nova.log import get_logger
from nova.threadsafe import post_callback

_log = get_logger(__name__)


class Player:
    """Plays PCM audio through the default output device. cancel() may be
    called from any thread and stops playback within one callback block.

    Not safe for concurrent play() calls: `self._stream` is only guarded
    at the two points it's assigned/torn down (stream creation and
    close()), not across the whole call. Two overlapping play() calls on
    the same Player will race on `self._stream` and `_cancel_event`. Fine
    for M0/M1's turn-taking model (NOVA speaks one utterance at a time,
    never two at once), but the next caller should not have to discover
    this against a live mic -- serialize calls, or use one Player per
    concurrent utterance, if that assumption ever changes (nova-security,
    T0.2 review)."""

    def __init__(self, device: int | None = None):
        self._stream: sd.OutputStream | None = None
        self._cancel_event = threading.Event()
        self._lock = threading.Lock()
        # Finding F18: taking the OS default output endpoint unverified is the
        # output-side twin of the input-side trap this package exists to avoid.
        # This machine exposes a television over NVIDIA HDMI alongside its local
        # sound card, and the OS default moves between them -- landing on the
        # screen makes NOVA inaudible while synthesis succeeds perfectly.
        # `device=None` means "ask the prober", not "trust the default".
        # Resolved lazily on first play(), NOT here. Probing opens a real output
        # stream and writes a real buffer, and doing that in a constructor means
        # merely *constructing* a Player touches hardware -- which made
        # tests/test_audio_capture_player.py's own "no real audio device is
        # opened anywhere in this file" claim false on a cold cache, without
        # anything failing to say so. Found by nova-tester while testing
        # output.py; the constructor-does-I/O smell was mine.
        self._device_explicit = device
        self._device_resolved = False
        self._device: int | None = device

    def _resolve_device(self) -> None:
        """Pick an output endpoint once, on first use rather than at construction."""
        if self._device_resolved or self._device_explicit is not None:
            self._device_resolved = True
            return
        try:
            from nova.audio.output import probe_output_device

            self._device = probe_output_device().index
        except Exception as exc:  # never let device selection stop NOVA speaking
            _log.warning(
                "Output device probe failed (%s); falling back to the OS default. "
                "If NOVA is silent, run scripts/probe_output.py.",
                exc,
            )
            self._device = None
        self._device_resolved = True

    async def play(self, pcm: np.ndarray, samplerate: int) -> None:
        self._resolve_device()
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
            # sounddevice invokes this from its own audio thread, which can
            # outlive `loop` (e.g. NovaApp.stop() closing the loop while a
            # stream is still tearing down) -- post_callback (F30) drops
            # that case at DEBUG instead of raising RuntimeError on a
            # thread with nothing to catch it.
            if not done.done():
                post_callback(loop, done.set_result, None)

        with self._lock:
            self._stream = sd.OutputStream(
                samplerate=samplerate,
                channels=n_channels,
                dtype="float32",
                device=self._device,
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

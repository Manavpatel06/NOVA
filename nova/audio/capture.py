"""Continuous microphone capture, resampled to 16 kHz mono float32 — the
format Whisper (and Silero VAD) expect. Reads happen at the device's native
sample rate (several viable devices on this hardware refuse to open at
16 kHz directly) and are resampled in a background-thread callback."""
from __future__ import annotations

import queue

import numpy as np
import sounddevice as sd

from nova.audio.devices import DeviceProfile, probe_input_devices
from nova.audio.resample import LinearResampler
from nova.config import AudioConfig, load_config
from nova.log import get_logger

_log = get_logger(__name__)


def _default_audio_config() -> AudioConfig:
    """AudioConfig is the one source of truth for target samplerate/frame_ms
    (config/nova.yaml). Degrade loudly rather than silently using
    hardcoded values if the config file is missing or malformed."""
    try:
        return load_config().audio
    except Exception as exc:
        _log.warning(
            "Could not load config/nova.yaml (%s); using built-in AudioConfig "
            "defaults (samplerate=16000, channels=1, frame_ms=30)",
            exc,
        )
        return AudioConfig()


class Capture:
    """Yields 16 kHz mono float32 frames from a probed input device.

    Usage:
        cap = Capture()
        cap.start()
        frame = cap.read()   # blocks until frame_ms of audio is ready
        cap.stop()
    """

    def __init__(
        self,
        profile: DeviceProfile | None = None,
        frame_ms: int | None = None,
        channels: int | None = None,
        queue_maxsize: int = 500,
    ):
        self._profile = profile or probe_input_devices()
        if self._profile.index < 0:
            _log.warning(
                "Capture constructed with no usable input device (probe found "
                "nothing). start() will fail loudly rather than pretend to work."
            )
        cfg = _default_audio_config()
        self.target_sr = cfg.samplerate
        self._channels = channels if channels is not None else cfg.channels
        frame_ms = frame_ms if frame_ms is not None else cfg.frame_ms
        self._frame_samples = int(self.target_sr * frame_ms / 1000)
        self._stream: sd.InputStream | None = None
        self._raw_q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=queue_maxsize)
        self._resampler = LinearResampler(self._profile.samplerate, self.target_sr)
        self._out_buffer = np.empty(0, dtype=np.float32)
        self._running = False

    @property
    def profile(self) -> DeviceProfile:
        return self._profile

    def start(self) -> None:
        if self._running:
            return
        if self._profile.index < 0:
            raise RuntimeError("Capture: no usable input device (see probe warnings)")

        def _callback(indata, frames, time_info, status):
            if status:
                _log.warning("Capture stream status: %s", status)
            # Contract output is always mono; downmix defensively in case
            # AudioConfig.channels is ever set above 1.
            mono = indata[:, 0] if indata.shape[1] == 1 else indata.mean(axis=1)
            try:
                self._raw_q.put_nowait(mono.astype(np.float32, copy=True))
            except queue.Full:
                _log.warning("Capture queue full; dropping audio (consumer too slow)")

        blocksize = max(1, int(self._profile.samplerate * 0.05))  # ~50ms native blocks
        self._stream = sd.InputStream(
            device=self._profile.index,
            samplerate=self._profile.samplerate,
            channels=self._channels,
            dtype="float32",
            blocksize=blocksize,
            callback=_callback,
        )
        self._stream.start()
        self._running = True
        _log.info(
            "Capture started on device %d (%s) at %d Hz -> resampling to %d Hz",
            self._profile.index, self._profile.name, self._profile.samplerate, self.target_sr,
        )

    def read(self, timeout: float | None = None) -> np.ndarray:
        """Block until one frame (frame_ms worth of 16kHz mono float32) is
        available and return it."""
        if not self._running:
            raise RuntimeError("Capture.read() called before start()")
        while len(self._out_buffer) < self._frame_samples:
            raw = self._raw_q.get(timeout=timeout)  # queue.Empty propagates on timeout
            resampled = self._resampler.process(raw)
            if len(resampled):
                self._out_buffer = np.concatenate([self._out_buffer, resampled])
        frame = self._out_buffer[: self._frame_samples]
        self._out_buffer = self._out_buffer[self._frame_samples :]
        return frame

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        self._running = False
        with self._raw_q.mutex:
            self._raw_q.queue.clear()
        self._out_buffer = np.empty(0, dtype=np.float32)
        _log.info("Capture stopped")

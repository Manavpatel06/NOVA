"""Text-to-speech: Kokoro ONNX, falling back to Windows SAPI.

NOVA must never go silently mute. If Kokoro fails to load, or fails at
synthesis time for a particular utterance, this falls back to Windows SAPI
via COM (in-process, no subprocess spawned -- keeps this module clear of the
T0.5 no-spawn rule entirely) and logs a WARNING naming Kokoro, every time,
so a degraded voice is never a silent one.
"""

from __future__ import annotations

import gc
import os
import tempfile
import wave
from pathlib import Path

import numpy as np

from nova.log import get_logger
from nova.tts._paths import KOKORO_MODEL_PATH, KOKORO_VOICES_PATH

logger = get_logger(__name__)

DEFAULT_KOKORO_VOICE = "af_heart"
DEFAULT_KOKORO_LANG = "en-us"

# Standard Windows SAPI SpeechLib enum values (SpeechAudioFormatType /
# SpStreamFormatEnum), confirmed against this machine's SAPI via
# win32com.client.gencache.EnsureDispatch("SAPI.SpVoice") during development.
# Hardcoded rather than resolved through win32com.client.constants at
# runtime, which requires EnsureDispatch() to generate and cache a
# type-library wrapper on first use -- extra disk I/O and startup latency for
# values that are stable, documented Microsoft constants.
_SAFT_16KHZ_16BIT_MONO = 18
_SSFM_CREATE_FOR_WRITE = 3


class Synthesizer:
    """Loads Kokoro ONNX (or falls back to Windows SAPI) and synthesizes
    text to PCM audio."""

    def __init__(self) -> None:
        self._kokoro = None
        self._backend: str | None = None  # "kokoro" | "sapi" | None (unloaded)

    @property
    def backend(self) -> str | None:
        """Which engine actually served the last load() -- 'kokoro', 'sapi',
        or None before load() has run."""
        return self._backend

    def load(self) -> None:
        if self._backend is not None:
            return
        try:
            from kokoro_onnx import Kokoro

            self._kokoro = Kokoro(str(KOKORO_MODEL_PATH), str(KOKORO_VOICES_PATH))
            self._backend = "kokoro"
            logger.info("Synthesizer loaded Kokoro ONNX from %s", KOKORO_MODEL_PATH)
        except Exception as exc:
            logger.warning(
                "Kokoro TTS failed to load (%s); falling back to Windows SAPI. "
                "NOVA must never go silently mute.",
                exc,
            )
            self._backend = "sapi"

    def unload(self) -> None:
        if self._backend is None:
            return
        self._kokoro = None
        self._backend = None
        gc.collect()
        logger.info("Synthesizer unloaded")

    def say(self, text: str) -> tuple[np.ndarray, int]:
        if self._backend is None:
            self.load()

        if self._backend == "kokoro":
            try:
                pcm, samplerate = self._kokoro.create(
                    text, voice=DEFAULT_KOKORO_VOICE, lang=DEFAULT_KOKORO_LANG
                )
                return np.asarray(pcm, dtype=np.float32), int(samplerate)
            except Exception as exc:
                logger.warning(
                    "Kokoro TTS failed at synthesis time (%s); falling back to "
                    "Windows SAPI for this utterance. NOVA must never go "
                    "silently mute.",
                    exc,
                )
                # Fall through to SAPI for this call only -- don't
                # permanently downgrade the backend over one bad utterance.

        return _say_sapi(text)


def _say_sapi(text: str) -> tuple[np.ndarray, int]:
    """Synthesize via Windows SAPI. In-process COM only -- never spawns a
    process, so this stays outside the T0.5 no-spawn allowlist entirely."""
    import pythoncom
    import win32com.client

    # Safe to call from any thread; a second CoInitialize on the same
    # thread/apartment is a harmless no-op (COM ref-counts it internally).
    pythoncom.CoInitialize()
    try:
        voice = win32com.client.Dispatch("SAPI.SpVoice")
        stream = win32com.client.Dispatch("SAPI.SpFileStream")

        tmp_path = Path(tempfile.gettempdir()) / f"nova_sapi_{os.getpid()}_{id(stream)}.wav"
        try:
            stream.Format.Type = _SAFT_16KHZ_16BIT_MONO
            stream.Open(str(tmp_path), _SSFM_CREATE_FOR_WRITE)
            voice.AudioOutputStream = stream
            voice.Speak(text)
            stream.Close()

            with wave.open(str(tmp_path), "rb") as wav_file:
                samplerate = wav_file.getframerate()
                raw = wav_file.readframes(wav_file.getnframes())
            pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            return pcm, samplerate
        finally:
            tmp_path.unlink(missing_ok=True)
    finally:
        pythoncom.CoUninitialize()

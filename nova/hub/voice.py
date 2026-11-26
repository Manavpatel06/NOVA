"""NOVA's ears and voice on the hub (ADR 0023).

Speech to text: faster-whisper on the CPU (int8; `hub.stt_model`, small.en by
default -- the laptop's large-v3-turbo is far too slow without a GPU).
Text to speech: Kokoro, the same voice as on the laptop.

Both models are read from ~/NOVA/models only (`nova-hub voice` downloads
them once); nothing here ever downloads, and audio is never written to disk.

Audio in: 16 kHz mono 16-bit PCM (what the talk page sends). Audio out: a
WAV file's bytes. One sentence at a time (a lock), and both models load on
first use, or ahead of it with warm_up().
"""
from __future__ import annotations

import io
import re
import threading
import time
import wave

from nova import MODELS_DIR
from nova.log import get_logger

_log = get_logger("hub.voice")

SAMPLE_RATE = 16_000
MAX_SECONDS = 60
#: where `nova-hub voice` puts the Whisper model (Hugging Face cache layout)
STT_DIR = MODELS_DIR / "hf"


class VoiceUnavailable(RuntimeError):
    pass


class HubVoice:
    def __init__(self, *, stt_model: str = "small.en", voice: str = "af_heart", threads: int = 4) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,39}", stt_model or ""):
            raise ValueError(f"not a Whisper model name: {stt_model!r}")
        self.stt_model = stt_model
        self.voice = voice
        self._threads = threads
        self._stt = None
        self._tts = None
        self._lock = threading.Lock()

    # --- readiness --------------------------------------------------------

    def missing(self) -> list[str]:
        """What `nova-hub voice` still has to install, in plain words."""
        from nova import TTS_MODEL_PATH, TTS_VOICES_PATH

        out = []
        for module, what in (("numpy", "numpy"), ("faster_whisper", "faster-whisper"), ("kokoro_onnx", "kokoro-onnx")):
            try:
                __import__(module)
            except Exception:  # noqa: BLE001 -- any import failure means "not usable"
                out.append(what)
        if not TTS_MODEL_PATH.exists() or not TTS_VOICES_PATH.exists():
            out.append("the Kokoro voice files")
        if not any(STT_DIR.glob(f"models--*faster-whisper-{self.stt_model}*")):
            out.append(f"the Whisper {self.stt_model} model")
        return out

    def ready(self) -> bool:
        return not self.missing()

    def warm_up(self) -> None:
        """Load both models now so the first sentence isn't slow."""
        with self._lock:
            self._load_stt()
            self._load_tts()

    # --- speech to text ---------------------------------------------------

    def _load_stt(self):
        if self._stt is None:
            try:
                from faster_whisper import WhisperModel
            except Exception as exc:  # noqa: BLE001
                raise VoiceUnavailable(f"speech recognition isn't installed ({type(exc).__name__})") from exc
            t0 = time.monotonic()
            try:
                self._stt = WhisperModel(self.stt_model, device="cpu", compute_type="int8", cpu_threads=self._threads,
                                         download_root=str(STT_DIR), local_files_only=True)
            except Exception as exc:  # noqa: BLE001
                raise VoiceUnavailable(f"the Whisper model isn't downloaded ({type(exc).__name__})") from exc
            _log.info("Whisper %s loaded on the CPU in %.1fs", self.stt_model, time.monotonic() - t0)
        return self._stt

    def transcribe(self, pcm16: bytes) -> str:
        """16 kHz mono int16 PCM -> text ("" for silence)."""
        import numpy as np

        from nova.stt.transcriber import _is_silence_hallucination

        if len(pcm16) % 2:
            pcm16 = pcm16[:-1]
        if len(pcm16) > SAMPLE_RATE * 2 * MAX_SECONDS:
            raise ValueError(f"audio longer than {MAX_SECONDS} seconds")
        audio = np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0
        if audio.size < SAMPLE_RATE // 4:  # under a quarter second: nothing was said
            return ""
        with self._lock:
            model = self._load_stt()
            # greedy decoding (beam 1) and no timestamps: about twice as fast on the hub's CPU, and the
            # sentences are short commands (2026-11-26: 7.9 s for 1.8 s of speech before this)
            segments, _info = model.transcribe(audio, language="en", condition_on_previous_text=False,
                                               hotwords="Nova Manav", vad_filter=True, beam_size=1,
                                               without_timestamps=True)
            return "".join(s.text for s in segments if not _is_silence_hallucination(s)).strip()

    # --- text to speech ---------------------------------------------------

    def _load_tts(self):
        if self._tts is None:
            from nova import TTS_MODEL_PATH, TTS_VOICES_PATH

            try:
                from kokoro_onnx import Kokoro

                self._tts = Kokoro(str(TTS_MODEL_PATH), str(TTS_VOICES_PATH))
            except Exception as exc:  # noqa: BLE001
                raise VoiceUnavailable(f"the Kokoro voice isn't installed ({type(exc).__name__})") from exc
            _log.info("Kokoro voice loaded")
        return self._tts

    def speak(self, text: str) -> bytes:
        """Text -> a WAV file's bytes."""
        import numpy as np

        text = " ".join((text or "").split())[:2000]
        if not text:
            return b""
        with self._lock:
            pcm, rate = self._load_tts().create(text, voice=self.voice, lang="en-us")
        samples = np.clip(np.asarray(pcm, dtype=np.float32), -1.0, 1.0)
        return to_wav((samples * 32767.0).astype("<i2").tobytes(), int(rate))


def to_wav(pcm16: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm16)
    return buf.getvalue()

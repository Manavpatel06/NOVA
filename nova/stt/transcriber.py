"""Real speech-to-text via faster-whisper / CTranslate2.

Uses large-v3-turbo on whichever device/compute type `probe_gpu()` actually
verified works on this machine — never a compute type merely advertised as
supported. See `nova.stt.gpu` for why that distinction matters.
"""

from __future__ import annotations

import gc
import time

import numpy as np

from nova.log import get_logger
from nova.stt._dll import ensure_dll_directories
from nova.stt._paths import HF_HUB_CACHE_DIR
from nova.stt.gpu import GpuProfile, probe_gpu

logger = get_logger(__name__)

_MODEL_NAME = "large-v3-turbo"


class Transcriber:
    """Loads large-v3-turbo on the best verified device/compute type and
    transcribes 16 kHz mono float32 PCM to text."""

    def __init__(self, gpu_profile: GpuProfile | None = None) -> None:
        ensure_dll_directories()
        self._profile = gpu_profile or probe_gpu()
        self._model = None

    @property
    def profile(self) -> GpuProfile:
        return self._profile

    def load(self) -> None:
        if self._model is not None:
            return
        from faster_whisper import WhisperModel

        if self._profile.verified_model and self._profile.verified_model != _MODEL_NAME:
            logger.info(
                "GPU profile was verified against %r, not %r -- device/compute_type "
                "worked on the smaller probe model; this load is the first real "
                "test of that combination against %r.",
                self._profile.verified_model, _MODEL_NAME, _MODEL_NAME,
            )

        t0 = time.monotonic()
        self._model = WhisperModel(
            _MODEL_NAME,
            device=self._profile.device,
            compute_type=self._profile.compute_type,
            download_root=str(HF_HUB_CACHE_DIR),
            local_files_only=True,
        )
        logger.info(
            "Transcriber loaded %s on %s/%s in %.2fs",
            _MODEL_NAME, self._profile.device, self._profile.compute_type,
            time.monotonic() - t0,
        )

    def unload(self) -> None:
        if self._model is None:
            return
        self._model = None
        gc.collect()
        logger.info(
            "Transcriber unloaded (%s/%s)", self._profile.device, self._profile.compute_type
        )

    def transcribe(self, pcm: np.ndarray) -> str:
        if self._model is None:
            self.load()
        segments, _info = self._model.transcribe(pcm)
        return "".join(segment.text for segment in segments).strip()

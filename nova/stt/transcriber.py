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
        """Drop the loaded model and reclaim what CTranslate2 will actually
        give back.

        VRAM recovers cleanly and repeatably (2479 -> 367 MiB, docs/
        GOVERNOR_MEASUREMENTS.md). Host RSS does not, once `transcribe()`
        has run at least once in this process: it settles at ~740-780 MB and
        stays there for the rest of the process's life, through this method
        and through `gc.collect()`. Investigated and confirmed exhausted,
        not unimplemented: this drops the last Python reference (the
        strongest release available -- stronger than calling the
        underlying `ctranslate2.models.Whisper.unload_model(to_cpu=False)`
        directly, which was tried and measured byte-identical: 741.7 -> 741.7
        MB RSS, no change). The retained memory is below any public
        ctranslate2/faster-whisper API's reach -- almost certainly a
        native-side (CTranslate2/MKL/CUDA host-pinned) allocator arena the
        C++ runtime keeps rather than returns to the OS. Nothing left to try
        here without patching CTranslate2 itself. `nova.governor.Governor`
        (T1.2) is calibrated around this floor (`rss_ceiling_mb=900`, per
        docs/GOVERNOR_MEASUREMENTS.md) rather than assuming this method
        returns the process to cold-idle.
        """
        if self._model is None:
            return
        self._model = None
        gc.collect()
        logger.info(
            "Transcriber unloaded (%s/%s) -- VRAM is fully reclaimed; host RSS "
            "is not, once transcribe() has run (see this method's docstring)",
            self._profile.device, self._profile.compute_type,
        )

    def transcribe(self, pcm: np.ndarray) -> str:
        if self._model is None:
            self.load()
        segments, _info = self._model.transcribe(pcm)
        return "".join(segment.text for segment in segments).strip()

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
        has run at least once in this *process*: it jumps ~480-525 MB on the
        very first real inference and stays there, through this method and
        through `gc.collect()`.

        Investigated in depth (lead's request after nova-dev-core's governor
        measurements), not just re-measured. Findings, each independently
        controlled:

        - **One-time, not cumulative** -- 8 consecutive `transcribe()` calls
          in one process: 263 -> 770 MB on call #1, then flat at 739-741 MB
          for calls #2-8 (noise-level variance, no growth trend). This is
          the crux fact for an always-on assistant: it is a fixed floor, not
          a per-call leak.
        - **Independent of audio duration** -- tested 0.1s/1s/5s/30s/60s
          audio as the *first* transcription in a fresh process each time:
          delta stayed ~498-526 MB regardless of length.
        - **Independent of model size** -- `tiny.en` (~39M params) pays
          essentially the same one-time cost (~507 MB) as `large-v3-turbo`
          (~800M params). Not proportional to model weights.
        - **Independent of `compute_type`/`beam_size`** -- float16 vs.
          int8_float16, beam_size 1 vs. 5: same ballpark delta throughout.
        - **CUDA-specific, not general CTranslate2 behavior** -- the same
          first-inference test on CPU (`tiny.en`, int8) costs ~35 MB, not
          ~500 MB. This isolates the cost to the CUDA code path.
        - **Two CTranslate2-exposed CUDA allocator controls, tried and
          ruled out**: `CT2_CUDA_ALLOCATOR=cuda_malloc_async` (vs. the
          default `cub_caching`) and a deliberately tight
          `CT2_CUDA_CACHING_ALLOCATOR_CONFIG` -- neither changed the host
          RSS delta at all. `ctranslate2.dll` contains `cudaHostAlloc`/
          `cudaMallocHost`/`cudaFreeHost` symbols, so this is very likely
          CUDA pinned-host-memory / cuDNN+cuBLAS handle and workspace
          initialization done once per process on first real kernel launch
          -- a layer CTranslate2's own (device-memory-only) allocator knobs
          do not reach, and neither does `unload_model(to_cpu=False)`
          (tried directly: 741.7 -> 741.7 MB, no change) or a full `del` of
          the model object instead of just dropping our reference (also no
          change).
        - **Process-scoped, not instance-scoped** -- confirmed against this
          actual class: load -> transcribe -> unload -> load -> transcribe
          -> unload -> load -> transcribe, three full cycles, one process:
          777 -> 795 -> 759 MB, flat within noise. The cost is paid once per
          process's life, not once per `Transcriber` instance or per
          load/unload cycle -- so the governor unloading and later reloading
          this component costs latency, never another ~500 MB.

        Conclusion: no config or public API reduces this floor. It is
        genuinely irreducible in-process. The only way to actually return
        this memory to the OS is killing the process that holds the CUDA
        context -- i.e. running STT in a subprocess, if reclaiming it ever
        matters more than the added latency/complexity of doing so.
        `nova.governor.Governor` (T1.2) is calibrated around this floor
        (`rss_ceiling_mb=900`, per docs/GOVERNOR_MEASUREMENTS.md) rather
        than assuming this method returns the process to cold-idle.
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

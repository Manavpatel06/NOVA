"""GPU capability probing for CTranslate2 / faster-whisper.

`ctranslate2.get_supported_compute_types("cuda")` is a compile-time list, not
a runtime test. On this project's RTX 5060 (Blackwell, compute capability
12.0) it advertises `int8` and `int8_bfloat16` support; the INT8 GEMM path is
not implemented for compute capability >= 12, so trusting the list is a
CUBLAS_STATUS_NOT_SUPPORTED failure waiting to happen on first inference.
`probe_gpu()` trusts nothing: it loads a real model and runs a real
transcription for each candidate compute type, in order, and keeps the first
that genuinely completes. `int8` and `int8_bfloat16` are refused outright as
candidates on compute capability >= 12, never merely deprioritized.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import asdict, dataclass

import numpy as np

from nova.stt._dll import ensure_dll_directories
from nova.stt._paths import DATA_DIR, GPU_PROFILE_PATH, HF_HUB_CACHE_DIR

logger = logging.getLogger("nova.stt.gpu")

_PROBE_MODEL = "tiny.en"  # fast; used for probing only, never for real transcription

# Candidate compute types to try on GPU, in order. Never widen this without
# re-verifying on real hardware per compute capability.
_GPU_CANDIDATES = ["float16", "int8_float16"]
_REFUSED_ON_CC12 = frozenset({"int8", "int8_bfloat16"})
_REFUSED_MIN_COMPUTE_CAPABILITY = 12.0

_UNSUPPORTED_KERNEL_MARKERS = (
    "cublas_status_not_supported",
    "no kernel image is available",
    "cublas_status_arch_mismatch",
)
_OOM_MARKERS = (
    "out of memory",
    "cuda_error_out_of_memory",
    "cublas_status_alloc_failed",
)


@dataclass(frozen=True)
class GpuProfile:
    device: str
    compute_type: str
    verified: bool
    gpu_name: str
    driver: str
    ct2_version: str
    dll_directories: list[str]


def _run_nvidia_smi(query: str) -> str | None:
    try:
        result = subprocess.run(
            ["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    lines = result.stdout.strip().splitlines()
    return lines[0].strip() if lines else None


def _gpu_identity() -> tuple[str | None, str | None, float | None]:
    """Returns (gpu_name, driver_version, compute_capability) via nvidia-smi,
    or (None, None, None) if no NVIDIA GPU / driver is visible."""
    combined = _run_nvidia_smi("name,driver_version,compute_cap")
    if not combined:
        return None, None, None
    parts = [p.strip() for p in combined.split(",")]
    if len(parts) < 3:
        return None, None, None
    name, driver, cc_raw = parts[0], parts[1], parts[2]
    try:
        compute_capability = float(cc_raw)
    except ValueError:
        compute_capability = None
    return name, driver, compute_capability


def _load_cache() -> GpuProfile | None:
    if not GPU_PROFILE_PATH.exists():
        return None
    try:
        data = json.loads(GPU_PROFILE_PATH.read_text(encoding="utf-8"))
        return GpuProfile(**data)
    except Exception:
        logger.warning("data/gpu_profile.json is corrupt or stale; ignoring cache and re-probing")
        return None


def _save_cache(profile: GpuProfile) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    GPU_PROFILE_PATH.write_text(json.dumps(asdict(profile), indent=2), encoding="utf-8")


def _classify_failure(exc: Exception) -> str:
    msg = str(exc).lower()
    if any(marker in msg for marker in _UNSUPPORTED_KERNEL_MARKERS):
        return "unsupported_kernel"
    if any(marker in msg for marker in _OOM_MARKERS):
        return "oom"
    return "unknown"


def _probe_audio() -> np.ndarray:
    """One second of synthetic tone at 16 kHz. Not speech — the probe only
    needs to exercise the real encode/decode path, not produce a sensible
    transcript."""
    sr = 16000
    t = np.linspace(0, 1.0, sr, endpoint=False)
    return (0.1 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def _try_transcribe(device: str, compute_type: str) -> None:
    """Load the probe model on (device, compute_type) and run one real
    transcription. Raises on any failure; the caller classifies it."""
    from faster_whisper import WhisperModel

    model = WhisperModel(
        _PROBE_MODEL,
        device=device,
        compute_type=compute_type,
        download_root=str(HF_HUB_CACHE_DIR),
        local_files_only=True,
    )
    try:
        segments, _info = model.transcribe(_probe_audio())
        list(segments)  # force the generator so encode/decode actually run
    finally:
        del model


def _probe_cpu(ct2_version: str, dll_dirs: list[str]) -> GpuProfile:
    logger.warning(
        "Falling back to CPU for speech-to-text. large-v3-turbo uses "
        "roughly 1.9 GB of RAM on CPU, on a 16 GB machine Manav also uses."
    )
    for compute_type in ("int8", "float32"):
        try:
            _try_transcribe("cpu", compute_type)
        except Exception as exc:
            logger.warning("CPU compute type %r failed: %s", compute_type, exc)
            continue
        return GpuProfile(
            device="cpu",
            compute_type=compute_type,
            verified=True,
            gpu_name="",
            driver="",
            ct2_version=ct2_version,
            dll_directories=dll_dirs,
        )
    logger.warning("No CPU compute type could be verified either; returning unverified profile")
    return GpuProfile(
        device="cpu",
        compute_type="float32",
        verified=False,
        gpu_name="",
        driver="",
        ct2_version=ct2_version,
        dll_directories=dll_dirs,
    )


def probe_gpu(force: bool = False) -> GpuProfile:
    """Probe this machine for a verified-working device/compute type and
    cache the result to data/gpu_profile.json, keyed by (GPU name, driver
    version, ctranslate2 version) — any of those changing forces a re-probe.

    Never trusts ctranslate2's advertised compute type support: always loads
    a real model and runs a real transcription before marking a candidate
    verified.
    """
    dll_dirs = ensure_dll_directories()

    import ctranslate2

    ct2_version = ctranslate2.__version__
    gpu_name, driver, compute_capability = _gpu_identity()

    if not force:
        cached = _load_cache()
        if (
            cached is not None
            and cached.gpu_name == (gpu_name or "")
            and cached.driver == (driver or "")
            and cached.ct2_version == ct2_version
        ):
            logger.info(
                "Using cached GPU profile: device=%s compute_type=%s verified=%s",
                cached.device, cached.compute_type, cached.verified,
            )
            return cached

    if gpu_name is None or ctranslate2.get_cuda_device_count() == 0:
        profile = _probe_cpu(ct2_version, dll_dirs)
        _save_cache(profile)
        return profile

    candidates = list(_GPU_CANDIDATES)
    if compute_capability is not None and compute_capability >= _REFUSED_MIN_COMPUTE_CAPABILITY:
        refused = [c for c in candidates if c in _REFUSED_ON_CC12]
        if refused:
            logger.info(
                "Refusing %s outright on compute capability %.1f (>= %.1f)",
                refused, compute_capability, _REFUSED_MIN_COMPUTE_CAPABILITY,
            )
        candidates = [c for c in candidates if c not in _REFUSED_ON_CC12]

    last_error: Exception | None = None
    for compute_type in candidates:
        try:
            _try_transcribe("cuda", compute_type)
        except Exception as exc:
            kind = _classify_failure(exc)
            if kind == "oom":
                logger.warning(
                    "GPU compute type %r ran out of memory; trying next "
                    "(lower-memory) candidate: %s", compute_type, exc,
                )
            elif kind == "unsupported_kernel":
                logger.warning(
                    "GPU compute type %r has no supported kernel on this "
                    "card (compute capability %.1f); trying next candidate: %s",
                    compute_type, compute_capability or -1.0, exc,
                )
            else:
                logger.warning("GPU compute type %r failed (%s): %s", compute_type, kind, exc)
            last_error = exc
            continue

        profile = GpuProfile(
            device="cuda",
            compute_type=compute_type,
            verified=True,
            gpu_name=gpu_name,
            driver=driver or "",
            ct2_version=ct2_version,
            dll_directories=dll_dirs,
        )
        _save_cache(profile)
        return profile

    logger.warning(
        "No GPU compute type could be verified on %s (driver %s): %s",
        gpu_name, driver, last_error,
    )
    profile = _probe_cpu(ct2_version, dll_dirs)
    _save_cache(profile)
    return profile

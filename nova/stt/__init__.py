"""Speech-to-text: GPU probing + transcription (T0.3).

Importing this package adds the CUDA runtime DLL directories to the process
search path before anything imports ctranslate2 — see nova.stt._dll for why
that ordering is load-bearing on Windows.
"""

from __future__ import annotations

from nova.stt._dll import ensure_dll_directories

ensure_dll_directories()

from nova.stt.gpu import GpuProfile, probe_gpu  # noqa: E402
from nova.stt.transcriber import Transcriber  # noqa: E402

__all__ = ["GpuProfile", "probe_gpu", "Transcriber"]

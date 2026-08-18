"""Shared filesystem locations for the tts package.

Not part of the T0.4 contract surface. Sourced from nova/__init__.py (the
one place that knows where things live on disk) rather than recomputed
independently here: constitution, "one source of truth per invariant."
"""

from __future__ import annotations

from nova import TTS_MODEL_PATH as KOKORO_MODEL_PATH, TTS_VOICES_PATH as KOKORO_VOICES_PATH

__all__ = ["KOKORO_MODEL_PATH", "KOKORO_VOICES_PATH"]

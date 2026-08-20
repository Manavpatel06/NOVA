"""NOVA — voice-first, always-on personal assistant.

This module is the one place that knows where things live on disk. No other
module may hardcode a path; everything downstream imports these constants or
reads them from Config (nova/config.py).
"""

from __future__ import annotations

from pathlib import Path

__version__ = "0.1.0"

# ---------------------------------------------------------------------------
# Root layout
# ---------------------------------------------------------------------------

#: D:\NOVA — the repo root, derived from this file's location so it is
#: correct regardless of cwd or how NOVA is invoked.
ROOT: Path = Path(__file__).resolve().parent.parent

CONFIG_DIR: Path = ROOT / "config"
DATA_DIR: Path = ROOT / "data"
MODELS_DIR: Path = ROOT / "models"
CACHE_DIR: Path = ROOT / "cache"

#: The one config file (docs/CONTRACTS.md).
DEFAULT_CONFIG_PATH: Path = CONFIG_DIR / "nova.yaml"

# ---------------------------------------------------------------------------
# Model weights — already on disk, salvaged and byte-verified.
# Never download at import time (docs/CONTRACTS.md).
# ---------------------------------------------------------------------------

TTS_MODEL_PATH: Path = MODELS_DIR / "tts" / "kokoro-v1.0.onnx"
TTS_VOICES_PATH: Path = MODELS_DIR / "tts" / "voices-v1.0.bin"
VAD_MODEL_PATH: Path = MODELS_DIR / "vad" / "silero_vad.onnx"
WAKE_MODEL_DIR: Path = MODELS_DIR / "wake"

#: faster-whisper models are resolved through the local HF cache — never
#: fetched over the network at runtime.
HF_CACHE_DIR: Path = CACHE_DIR / "hf"
HF_HUB_DIR: Path = HF_CACHE_DIR / "hub"

# ---------------------------------------------------------------------------
# Runtime state (data/ is gitignored; created lazily by whatever writes first)
# ---------------------------------------------------------------------------

LOG_PATH: Path = DATA_DIR / "nova.log"
AUDIO_PROFILE_PATH: Path = DATA_DIR / "audio_profile.json"
GPU_PROFILE_PATH: Path = DATA_DIR / "gpu_profile.json"
AUDIT_LOG_PATH: Path = DATA_DIR / "audit.log"

#: T2.3a's machine-written allowlist additions (nova.skills.add_app) --
#: deliberately NOT under config/, and NOT git-tracked. config/nova.yaml is
#: hand-authored and carries comments a runtime dump-and-rewrite would
#: destroy (the wake threshold's whole derivation lives in one); this file
#: is exclusively machine-written, same lifecycle as audit.log and the two
#: profile caches above. nova/config.py's load_config() merges this file's
#: entries into Config.security.allowed_apps, with nova.yaml always winning
#: on a name conflict -- see its module docstring for the merge rule.
LEARNED_APPS_PATH: Path = DATA_DIR / "learned_apps.json"

"""Shared filesystem locations for the stt package.

Not part of the T0.3 contract surface — just avoids hardcoding the same
paths twice across gpu.py and transcriber.py. Computed relative to this
file rather than read from the environment, since nova/config.py does not
exist yet (T0.1 is building it) and this package must stay runnable
standalone.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HF_HUB_CACHE_DIR = REPO_ROOT / "cache" / "hf" / "hub"
DATA_DIR = REPO_ROOT / "data"
GPU_PROFILE_PATH = DATA_DIR / "gpu_profile.json"

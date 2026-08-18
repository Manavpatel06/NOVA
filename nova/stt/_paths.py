"""Shared filesystem locations for the stt package.

Not part of the T0.3 contract surface -- just avoids hardcoding the same
names twice across gpu.py and transcriber.py. Sourced from nova/__init__.py
(the one place that knows where things live on disk) rather than recomputed
independently here: constitution, "one source of truth per invariant" -- a
test and a config file describing the same path differently is a promise
nobody can act on.
"""

from __future__ import annotations

from nova import DATA_DIR, GPU_PROFILE_PATH, HF_HUB_DIR as HF_HUB_CACHE_DIR

__all__ = ["DATA_DIR", "GPU_PROFILE_PATH", "HF_HUB_CACHE_DIR"]

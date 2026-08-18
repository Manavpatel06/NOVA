"""DLL directory setup for CUDA-dependent extension modules on Windows.

Windows does not search PATH for extension-module DLL dependencies. The CUDA
runtime libraries ctranslate2 needs (cublas64_12.dll, cudnn_ops64_9.dll, ...)
ship inside the nvidia-*-cu12 wheels under `site-packages/nvidia/*/bin`, which
is on nobody's search path. Skipping this produces a failure that never
mentions CUDA or a path — on this machine it surfaces as
`RuntimeError: Library cublas64_12.dll is not found or cannot be loaded`,
raised lazily on the first real inference (model *load* succeeds either way).
Confirmed by direct reproduction on the RTX 5060 in this project.

`ensure_dll_directories()` must run before anything imports `ctranslate2` or
`faster_whisper` (which imports ctranslate2 internally).
"""

from __future__ import annotations

import os
import site
import sys
from pathlib import Path

_added_dirs: list[str] = []
_done = False


def _site_roots() -> list[Path]:
    """All site-packages-like roots worth searching, de-duplicated."""
    roots: list[Path] = []
    seen: set[Path] = set()

    def add(candidate: str | Path | None) -> None:
        if not candidate:
            return
        path = Path(candidate).resolve()
        if path not in seen and path.is_dir():
            seen.add(path)
            roots.append(path)

    try:
        for p in site.getsitepackages():
            add(p)
    except Exception:
        pass
    add(Path(sys.prefix) / "Lib" / "site-packages")
    try:
        # Respect venv semantics: don't pull in an unrelated interpreter's
        # user site-packages (e.g. a different Python install entirely) when
        # user site is disabled, as it normally is inside a venv.
        if site.ENABLE_USER_SITE:
            add(site.getusersitepackages())
    except Exception:
        pass
    return roots


def ensure_dll_directories() -> list[str]:
    """Add every `nvidia/*/bin` (and `torch/lib`, if present) directory to
    the process DLL search path via `os.add_dll_directory()`.

    Idempotent and cheap to call repeatedly from every module that might be
    the first to import ctranslate2. Nvidia wheel directories are added
    ahead of any torch-bundled CUDA runtime so the wheel's own version wins
    if both are installed. Returns the cumulative list of directories added.
    """
    global _done
    if _done:
        return list(_added_dirs)
    _done = True

    if not hasattr(os, "add_dll_directory"):
        # Not Windows — nothing to do.
        return list(_added_dirs)

    nvidia_dirs: list[Path] = []
    torch_dirs: list[Path] = []
    for root in _site_roots():
        nvidia_root = root / "nvidia"
        if nvidia_root.is_dir():
            for pkg_dir in sorted(nvidia_root.iterdir()):
                bin_dir = pkg_dir / "bin"
                if bin_dir.is_dir():
                    nvidia_dirs.append(bin_dir)
        torch_lib = root / "torch" / "lib"
        if torch_lib.is_dir():
            torch_dirs.append(torch_lib)

    for directory in nvidia_dirs + torch_dirs:
        os.add_dll_directory(str(directory))
        _added_dirs.append(str(directory))

    return list(_added_dirs)

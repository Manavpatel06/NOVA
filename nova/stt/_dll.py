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

This grants directories authority to feed the process's DLL loader, so it
gets the same realpath discipline CLAUDE.md requires for filesystem writes
("lexical path checks are bypassable with `..` and junctions"): a directory
name found under `nvidia/*/bin` is restricted to a named allowlist of the
packages CTranslate2 actually needs, then `.resolve()`d and checked to still
land inside the site-packages root it was discovered under before being
trusted. Windows resolves a junction at DLL-load time, not at discovery
time — a junction planted at a trusted-looking path is exactly the TOCTOU
shape a lexical check would miss.
"""

from __future__ import annotations

import os
import site
import sys
from pathlib import Path

_added_dirs: list[str] = []
_done = False

# The nvidia-*-cu12 wheel packages CTranslate2's CUDA build actually needs.
# A directory under `nvidia/` that isn't on this list is never granted DLL
# search authority, no matter what it's named or contains.
_ALLOWED_NVIDIA_PACKAGES = frozenset(
    {
        "cublas",
        "cudnn",
        "cuda_runtime",
        "cuda_nvrtc",
        "cufft",
        "curand",
        "cusolver",
        "cusparse",
        "nvjitlink",
    }
)


def _site_roots() -> list[Path]:
    """All site-packages-like roots worth searching, de-duplicated. Each is
    resolved -- these become the trust boundary later paths are checked
    against."""
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


def _resolved_descendant(path: Path, root: Path) -> Path | None:
    """Resolve `path` and return it only if the resolution still lands
    inside `root` (already resolved). A junction anywhere in `path` --
    at the package directory or the bin directory -- resolves away from
    `root` and is rejected here rather than trusted lexically."""
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    return resolved


def ensure_dll_directories() -> list[str]:
    """Add every allowlisted `nvidia/*/bin` (and `torch/lib`, if present)
    directory to the process DLL search path via `os.add_dll_directory()`.

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
                if pkg_dir.name not in _ALLOWED_NVIDIA_PACKAGES:
                    continue
                resolved_bin = _resolved_descendant(pkg_dir / "bin", root)
                if resolved_bin is not None and resolved_bin.is_dir():
                    nvidia_dirs.append(resolved_bin)

        resolved_torch_lib = _resolved_descendant(root / "torch" / "lib", root)
        if resolved_torch_lib is not None and resolved_torch_lib.is_dir():
            torch_dirs.append(resolved_torch_lib)

    for directory in nvidia_dirs + torch_dirs:
        os.add_dll_directory(str(directory))
        _added_dirs.append(str(directory))

    return list(_added_dirs)

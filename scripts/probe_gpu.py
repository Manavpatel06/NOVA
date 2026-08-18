#!/usr/bin/env python
"""Human-runnable GPU/STT diagnostic. Run this first when speech-to-text breaks.

Probes for a genuinely working CUDA compute type (never trusting
ctranslate2's advertised support), prints the DLL directories added on
Windows, and runs a real transcription with the resulting Transcriber so you
can see the device, compute type, and latency this machine actually gets.

    D:\\NOVA\\.venv\\Scripts\\python.exe scripts\\probe_gpu.py
    D:\\NOVA\\.venv\\Scripts\\python.exe scripts\\probe_gpu.py --force   # ignore cached gpu_profile.json
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from nova.stt import GpuProfile, Transcriber, probe_gpu
from nova.stt._dll import ensure_dll_directories


def print_dll_directories() -> None:
    print("=" * 100)
    print("DLL DIRECTORIES ADDED (os.add_dll_directory, before any ctranslate2 import)")
    print("=" * 100)
    dirs = ensure_dll_directories()
    if not dirs:
        print("  (none — not Windows, or no nvidia/torch CUDA wheels found in site-packages)")
    for d in dirs:
        print(f"  {d}")
    print()


def print_profile(profile: GpuProfile) -> None:
    print("=" * 100)
    print("GPU PROFILE (cached in data/gpu_profile.json)")
    print("=" * 100)
    print(f"  device        = {profile.device}")
    print(f"  compute_type  = {profile.compute_type}")
    print(f"  verified      = {profile.verified}")
    print(f"  gpu_name      = {profile.gpu_name or '(none)'}")
    print(f"  driver        = {profile.driver or '(none)'}")
    print(f"  ct2_version   = {profile.ct2_version}")
    print(f"  dll_dirs      = {len(profile.dll_directories)} directories")
    if not profile.verified:
        print("  WARNING: no verified working compute type. This is a best-effort fallback.")
    if profile.device == "cpu":
        print("  WARNING: running STT on CPU. large-v3-turbo uses ~1.9 GB RAM.")
    print()


def transcription_smoke_test(profile: GpuProfile) -> None:
    print("=" * 100)
    print("REAL TRANSCRIPTION SMOKE TEST (large-v3-turbo)")
    print("=" * 100)
    print(
        "NOTE: no speech audio fixture is available to this script, so this "
        "uses a synthetic tone. It proves load()/transcribe()/unload() run "
        "end-to-end on real hardware with a measured latency -- it does NOT "
        "prove transcription accuracy on real speech."
    )
    sr = 16000
    t = np.linspace(0, 3.0, sr * 3, endpoint=False)
    tone = (0.1 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)

    transcriber = Transcriber(gpu_profile=profile)

    t0 = time.monotonic()
    transcriber.load()
    load_s = time.monotonic() - t0

    t0 = time.monotonic()
    text = transcriber.transcribe(tone)
    transcribe_s = time.monotonic() - t0

    print(f"  load()       took {load_s:.2f}s")
    print(f"  transcribe() took {transcribe_s:.2f}s for 3.0s of audio")
    print(f"  result text  = {text!r}")

    transcriber.unload()
    print("  unload() called")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="ignore cached gpu_profile.json")
    parser.add_argument("--no-transcribe", action="store_true", help="skip the transcription smoke test")
    args = parser.parse_args()

    print_dll_directories()
    profile = probe_gpu(force=args.force)
    print_profile(profile)

    if not args.no_transcribe:
        transcription_smoke_test(profile)


if __name__ == "__main__":
    main()

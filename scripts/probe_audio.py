#!/usr/bin/env python
"""Human-runnable audio diagnostic. Run this first when audio breaks.

Prints every input-capable device on the system, what a real ~1s recording
measured for it (RMS, whether it was exact digital zero), and which device
the prober selected as the working microphone. Also does a quick, genuinely
cancellable playback smoke test through the default output device.

    D:\\NOVA\\.venv\\Scripts\\python.exe scripts\\probe_audio.py
    D:\\NOVA\\.venv\\Scripts\\python.exe scripts\\probe_audio.py --force   # ignore cache
    D:\\NOVA\\.venv\\Scripts\\python.exe scripts\\probe_audio.py --no-playback
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import sounddevice as sd

from nova.audio.devices import enumerate_and_measure, probe_input_devices, HOSTAPI_PRIORITY
from nova.audio.player import Player


def print_device_table() -> None:
    print("=" * 100)
    print("EMPIRICAL INPUT DEVICE PROBE (records ~1s from every candidate)")
    print("=" * 100)
    measurements = enumerate_and_measure()
    if not measurements:
        print("No input-capable devices found at all.")
        return
    header = f"{'idx':>4} {'hostapi':<18} {'sr':>6} {'verified':>8} {'rms':>14} {'all_zero':>9}  name"
    print(header)
    print("-" * len(header))
    for m in sorted(measurements, key=lambda m: (HOSTAPI_PRIORITY.get(m.hostapi, 99), -m.rms)):
        status = "FAILED" if m.error else ("PASS" if m.verified else "reject")
        print(
            f"{m.index:>4} {m.hostapi:<18} {m.samplerate:>6} {status:>8} "
            f"{m.rms:>14.8f} {str(m.all_zero):>9}  {m.name}"
            + (f"   [error: {m.error}]" if m.error else "")
        )
    print()


def print_winner(force: bool) -> None:
    profile = probe_input_devices(force=force)
    print("=" * 100)
    print("SELECTED DEVICE (cached in data/audio_profile.json)")
    print("=" * 100)
    print(f"  index      = {profile.index}")
    print(f"  name       = {profile.name}")
    print(f"  hostapi    = {profile.hostapi}")
    print(f"  samplerate = {profile.samplerate}")
    print(f"  verified   = {profile.verified}")
    print(f"  rms        = {profile.rms:.8f}")
    if not profile.verified:
        print("  WARNING: no verified working microphone. This is a best-effort fallback.")
    print()


async def playback_smoke_test() -> None:
    print("=" * 100)
    print("PLAYBACK SMOKE TEST (default output device)")
    print("=" * 100)
    sr = 16000
    dur = 1.5
    t = np.arange(int(sr * dur)) / sr
    tone = (0.2 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)

    player = Player()
    print(f"Playing a {dur}s 440Hz tone at 20% volume through the default output device...")
    t0 = time.monotonic()
    await player.play(tone, sr)
    elapsed = time.monotonic() - t0
    print(f"play() returned after {elapsed:.2f}s (expected ~{dur}s)")

    print("Playing a 3s tone and cancelling it after 0.5s (barge-in simulation)...")
    tone3 = (0.2 * np.sin(2 * np.pi * 440 * np.arange(int(sr * 3.0)) / sr)).astype(np.float32)

    async def _cancel_soon():
        await asyncio.sleep(0.5)
        player.cancel()

    t0 = time.monotonic()
    await asyncio.gather(player.play(tone3, sr), _cancel_soon())
    elapsed = time.monotonic() - t0
    print(f"play() returned after {elapsed:.2f}s (expected ~0.5s, NOT 3.0s, if cancel worked)")
    if elapsed < 1.0:
        print("  -> cancel() cut playback early: PASS")
    else:
        print("  -> cancel() did NOT cut playback early: FAIL")
    print()
    print("NOTE: this script cannot confirm audio was actually AUDIBLE -- that")
    print("needs a human with ears near the speakers. Elapsed-time checks above")
    print("only prove the stream started/stopped on schedule.")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="ignore cached audio_profile.json")
    parser.add_argument("--no-playback", action="store_true", help="skip the playback smoke test")
    args = parser.parse_args()

    print_device_table()
    print_winner(args.force)

    if not args.no_playback:
        asyncio.run(playback_smoke_test())


if __name__ == "__main__":
    main()

"""Human-runnable output-device diagnostic — the first thing to run if NOVA is silent.

    D:\\NOVA\\.venv\\Scripts\\python.exe scripts\\probe_output.py

Prints every output endpoint this machine exposes, which ones are display/HDMI
paths, which one NOVA will use and why, then plays a short tone through it.

If you hear the tone, playback works. If you do not, the endpoint NOVA chose is
not the one you are listening to — change the default in Windows sound settings,
or pass an explicit index to Player(device=...).

Not part of the test suite: this one needs ears.
"""
from __future__ import annotations

import asyncio
import sys

import numpy as np
import sounddevice as sd

from nova.audio.output import is_display_endpoint, probe_output_device
from nova.audio.player import Player


def main() -> int:
    hostapis = sd.query_hostapis()
    print("=" * 78)
    print("OUTPUT ENDPOINTS")
    print("=" * 78)
    try:
        default_index = int(sd.query_devices(kind="output")["index"])
    except Exception:
        default_index = -1

    for i, d in enumerate(sd.query_devices()):
        if d["max_output_channels"] <= 0:
            continue
        flags = []
        if i == default_index:
            flags.append("OS-DEFAULT")
        if is_display_endpoint(d["name"]):
            flags.append("DISPLAY/HDMI")
        api = hostapis[d["hostapi"]]["name"]
        print(f"  {i:3}  {api:<22} {d['name'][:44]:<44} {' '.join(flags)}")

    profile = probe_output_device(force=True)
    print()
    print("=" * 78)
    print("NOVA WILL USE")
    print("=" * 78)
    print(f"  index      = {profile.index}")
    print(f"  name       = {profile.name}")
    print(f"  hostapi    = {profile.hostapi}")
    print(f"  samplerate = {profile.samplerate}")
    print(f"  verified   = {profile.verified}   (opened and accepted a buffer)")
    print(f"  reason     = {profile.reason}")

    if not profile.verified:
        print()
        print("  The chosen endpoint did not accept a buffer. NOVA will be silent.")
        return 1

    print()
    print("Playing a 1-second 440 Hz tone. You should hear it now.")
    sr = profile.samplerate
    tone = (0.15 * np.sin(2 * np.pi * 440 * np.arange(int(sr * 1.0)) / sr)).astype(np.float32)
    asyncio.run(Player().play(tone, sr))
    print()
    print("Did you hear it?  yes -> playback is good, record it in docs/EVIDENCE.md")
    print("                  no  -> the endpoint above is not the one you listen to")
    return 0


if __name__ == "__main__":
    sys.exit(main())

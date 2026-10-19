#!/usr/bin/env python
"""Does NOVA actually hear you? A 30-second microphone check.

Records 5 seconds from each real microphone endpoint on this machine while
you say "Nova, what time is it?", then reports for each one how loud your
voice was compared with the room (signal-to-noise) and how much of it the
speech detector (Silero VAD, exactly as NOVA uses it) recognised as speech.

The recordings are saved to data\\mic_check\\ (local only, overwritten on the
next run) so they can be inspected. Nothing leaves this machine.

    D:\\NOVA\\.venv\\Scripts\\python.exe scripts\\mic_check.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import sounddevice as sd
import soundfile as sf

from nova import DATA_DIR
from nova.audio.devices import is_loopback_endpoint
from nova.audio.resample import LinearResampler
from nova.audio.vad import Vad

SECONDS = 5.0
OUT = DATA_DIR / "mic_check"


def _candidates() -> list[tuple[int, str, str, int]]:
    apis = sd.query_hostapis()
    out = []
    for idx, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] < 1 or is_loopback_endpoint(d["name"]):
            continue
        api = apis[d["hostapi"]]["name"]
        if api == "Windows DirectSound":  # same endpoints as MME, skip to keep it short
            continue
        out.append((idx, d["name"], api, int(d["default_samplerate"] or 16000)))
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    vad = Vad()
    vad.load()
    cands = _candidates()
    print(f"\nFound {len(cands)} microphone endpoints. For EACH one you'll get 5 seconds.")
    print('When it says SPEAK, say clearly: "Nova, what time is it?"\n')
    rows = []
    for n, (idx, name, api, sr) in enumerate(cands, 1):
        print(f"[{n}/{len(cands)}] {api}: {name}")
        for c in (3, 2, 1):
            print(f"   starting in {c}...", end="\r", flush=True)
            time.sleep(1)
        print("   SPEAK NOW                    ", flush=True)
        try:
            rec = sd.rec(int(sr * SECONDS), samplerate=sr, channels=1, dtype="float32", device=idx)
            sd.wait()
        except Exception as exc:  # noqa: BLE001
            print(f"   could not record: {exc}\n")
            rows.append((idx, api, name, None, None, None, None))
            continue
        x = rec.reshape(-1).astype(np.float32)
        y = LinearResampler(sr, 16000).process(x).astype(np.float32)  # same resampler NOVA's Capture uses
        sf.write(str(OUT / f"dev{idx:02d}.wav"), y, 16000, subtype="FLOAT")
        frame_rms = np.array([np.sqrt(np.mean(y[i:i + 480] ** 2)) for i in range(0, len(y) - 480, 480)])
        noise = float(np.percentile(frame_rms, 10)) + 1e-9
        peak = float(np.percentile(frame_rms, 95))
        snr_db = 20 * np.log10(peak / noise)
        vad.reset()
        probs = [vad.process(y[i:i + 480]) for i in range(0, len(y) - 480, 480)]
        speech_pct = 100.0 * sum(p > 0.5 for p in probs) / max(1, len(probs))
        dc = float(np.mean(y))
        rows.append((idx, api, name, peak, snr_db, speech_pct, dc))
        verdict = "HEARS YOU" if speech_pct >= 10 and snr_db >= 10 else "does NOT hear speech"
        print(f"   voice/room ratio {snr_db:5.1f} dB   speech detected {speech_pct:5.1f}%   -> {verdict}\n")

    print("=" * 90)
    print(f"{'idx':>4}  {'host api':<16} {'loudness':>9} {'SNR dB':>7} {'speech%':>8} {'DC':>8}  name")
    for idx, api, name, peak, snr, sp, dc in rows:
        if peak is None:
            print(f"{idx:>4}  {api:<16} {'failed':>9}")
            continue
        print(f"{idx:>4}  {api:<16} {peak:9.4f} {snr:7.1f} {sp:8.1f} {dc:8.4f}  {name}")
    print("=" * 90)
    print(f"Recordings saved in {OUT} (local only). Tell Claude 'mic check done'.")


if __name__ == "__main__":
    main()

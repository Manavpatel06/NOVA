#!/usr/bin/env python
"""Human-runnable TTS diagnostic. Run this first when speech output breaks.

Loads the Synthesizer (Kokoro ONNX, falling back to Windows SAPI if Kokoro
fails), synthesizes a couple of phrases, and writes each to a WAV file so a
human can listen and judge quality -- this script cannot confirm audio
sounds right, only that real, non-silent PCM came back.

    D:\\NOVA\\.venv\\Scripts\\python.exe scripts\\probe_tts.py
    D:\\NOVA\\.venv\\Scripts\\python.exe scripts\\probe_tts.py --force-sapi   # test the fallback path
"""
from __future__ import annotations

import argparse
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from nova.tts import Synthesizer

OUT_DIR = Path(__file__).resolve().parents[1] / "data"

PHRASES = ["what time is it", "open notepad"]


def write_wav(path: Path, pcm: np.ndarray, samplerate: int) -> None:
    pcm_int16 = np.clip(pcm, -1.0, 1.0)
    pcm_int16 = (pcm_int16 * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(samplerate)
        wav_file.writeframes(pcm_int16.tobytes())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force-sapi", action="store_true", help="point Kokoro at a bad path to exercise the fallback"
    )
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    synth = Synthesizer()
    if args.force_sapi:
        import nova.tts.synthesizer as synth_mod

        synth_mod.KOKORO_MODEL_PATH = Path("does-not-exist.onnx")

    print("=" * 100)
    print("SYNTHESIZER LOAD")
    print("=" * 100)
    t0 = time.monotonic()
    synth.load()
    print(f"  load() took {time.monotonic() - t0:.2f}s")
    print(f"  backend = {synth.backend}")
    if synth.backend != "kokoro":
        print("  WARNING: not running on Kokoro -- see the WARNING log above naming why.")
    print()

    print("=" * 100)
    print("SYNTHESIS")
    print("=" * 100)
    for i, phrase in enumerate(PHRASES):
        t0 = time.monotonic()
        pcm, sr = synth.say(phrase)
        elapsed = time.monotonic() - t0
        rms = float(np.sqrt(np.mean(np.square(pcm)))) if pcm.size else 0.0
        out_path = OUT_DIR / f"tts_probe_{i}_{synth.backend}.wav"
        write_wav(out_path, pcm, sr)
        print(f"  {phrase!r}")
        print(f"    backend={synth.backend} sr={sr} samples={pcm.shape[0]} rms={rms:.4f}")
        print(f"    say() took {elapsed:.2f}s")
        print(f"    wrote {out_path}")
        if rms < 1e-4:
            print("    WARNING: output is silent or near-silent -- listen and investigate.")
        print()

    synth.unload()
    print("unload() called")
    print()
    print("Open the WAV file(s) above in any player and listen -- this script")
    print("cannot judge whether the voice actually sounds right.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python
"""Human-runnable TTS -> STT round-trip diagnostic.

No real human speech fixture exists in this repo yet (see T0.3's report).
This closes that gap partway: Kokoro-synthesized speech is not a human
voice, but it *is* speech, so feeding it straight into the Transcriber
proves Whisper returns real words on this machine rather than a stray '.'
on silence. Not a substitute for testing against an actual microphone
recording -- report any mismatch, a near-miss is a finding, not noise.

Handles the sample-rate seam explicitly: Kokoro emits 24 kHz (SAPI emits
16 kHz already), Whisper wants 16 kHz mono float32. Reuses
nova.audio.resample.LinearResampler (already landed for T0.2) rather than
writing a second resampler.

    D:\\NOVA\\.venv\\Scripts\\python.exe scripts\\probe_roundtrip.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nova.audio.resample import LinearResampler
from nova.stt import Transcriber
from nova.tts import Synthesizer

PHRASES = ["what time is it", "open notepad"]


def main() -> None:
    synth = Synthesizer()
    synth.load()
    print(f"Synthesizer backend: {synth.backend}")

    transcriber = Transcriber()
    print(
        f"Transcriber device/compute_type: "
        f"{transcriber.profile.device}/{transcriber.profile.compute_type} "
        f"(verified={transcriber.profile.verified})"
    )
    transcriber.load()
    print()

    all_ok = True
    for phrase in PHRASES:
        t0 = time.monotonic()
        pcm, sr = synth.say(phrase)
        synth_s = time.monotonic() - t0

        if sr != 16000:
            resampler = LinearResampler(sr_in=sr, sr_out=16000)
            pcm_16k = resampler.process(pcm)
        else:
            pcm_16k = pcm

        t0 = time.monotonic()
        result = transcriber.transcribe(pcm_16k)
        transcribe_s = time.monotonic() - t0

        match = result.strip().lower().rstrip(".!?") == phrase.strip().lower()
        all_ok = all_ok and match
        print(f"  said:        {phrase!r}")
        print(f"  transcribed: {result!r}")
        print(f"  exact match: {match}")
        print(f"  synth {synth_s:.2f}s ({sr} Hz -> 16000 Hz) / transcribe {transcribe_s:.2f}s")
        print()

    synth.unload()
    transcriber.unload()
    print("ALL EXACT MATCHES" if all_ok else "AT LEAST ONE MISMATCH -- see above")


if __name__ == "__main__":
    main()

#!/usr/bin/env python
"""Human-runnable turn-loop diagnostic (docs/TASKS.md T0.6b).

No human is available to press the hotkey and speak into the mic from this
seat, so this exercises the *exact* production code path -- NovaApp.run_turn(),
the same method the real ptt.start/ptt.end handlers call -- with one
substitution: the "captured" audio is Kokoro-synthesized speech (resampled
24kHz -> 16kHz, same as scripts/probe_roundtrip.py) instead of a live mic
recording. That's a real limitation, stated plainly, not hidden: it proves
the whole wired pipeline (STT -> router -> ladder-gated skill -> TTS ->
speakers) runs correctly on real hardware with real measured latency: it does
NOT prove the hotkey-to-mic path with an actual human voice, which is the
lead's demo to run.

Three turns:
  1. "what time is it"   -> time (READ), should be answered
  2. "open notepad"      -> open_app (ACT), should actually launch Notepad
  3. "what's the weather today" -> below the confidence floor for both M0
     skills -> NOVA must ask a clarifying question, not guess

Also reports idle vs. mid-turn RSS and VRAM, and measured stage latency for
a real turn.

    D:\\NOVA\\.venv\\Scripts\\python.exe scripts\\probe_turn.py
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psutil

from nova.app import NovaApp
from nova.audio.resample import LinearResampler


def _rss_mb() -> float:
    return psutil.Process(os.getpid()).memory_info().rss / 1e6


def _vram_used_mb() -> str:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() if out.returncode == 0 else "(nvidia-smi failed)"
    except (FileNotFoundError, subprocess.SubprocessError):
        return "(nvidia-smi unavailable)"


def _synthesized_16k_pcm(app: NovaApp, phrase: str):
    """Uses the app's own Synthesizer to produce speech-shaped 16kHz audio,
    standing in for a live mic capture (see module docstring)."""
    pcm, sr = app.synth.say(phrase)
    if sr != 16000:
        pcm = LinearResampler(sr_in=sr, sr_out=16000).process(pcm)
    return pcm


def main() -> None:
    print("=" * 100)
    print("IDLE STATE (before NovaApp construction)")
    print("=" * 100)
    print(f"  RSS  = {_rss_mb():.1f} MB")
    print(f"  VRAM = {_vram_used_mb()}")
    print()

    app = NovaApp()

    print("=" * 100)
    print("AFTER NovaApp() CONSTRUCTION (no model loaded yet -- lazy)")
    print("=" * 100)
    print(f"  RSS           = {_rss_mb():.1f} MB")
    print(f"  VRAM          = {_vram_used_mb()}")
    print(f"  STT profile   = {app.transcriber.profile.device}/{app.transcriber.profile.compute_type}")
    print(f"  synth backend = {app.synth.backend!r} (None == not loaded yet)")
    print()

    turns = [
        ("what time is it", "expect: time skill answers"),
        ("open notepad", "expect: open_app launches Notepad for real"),
        ("what's the weather today", "expect: below confidence floor -> spoken clarification"),
    ]

    for phrase, expectation in turns:
        print("=" * 100)
        print(f"TURN: synthesized phrase = {phrase!r}  ({expectation})")
        print("=" * 100)
        pcm = _synthesized_16k_pcm(app, phrase)

        pids_before = {p.pid for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() == "notepad.exe"}

        t0 = time.monotonic()
        reply = asyncio.run(app.run_turn(pcm, source="probe_turn"))
        elapsed = time.monotonic() - t0
        if reply is not None:
            print(f"  reply.text = {reply.text!r}  success={reply.success}")
        else:
            print("  reply = None (clarification spoken, or a caught failure -- see log line above)")
        print(f"  wall time for run_turn() = {elapsed:.2f}s")
        print(f"  RSS after this turn = {_rss_mb():.1f} MB")

        pids_after = {p.pid for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() == "notepad.exe"}
        new_pids = pids_after - pids_before
        if new_pids:
            print(f"  VERIFIED: Notepad actually launched, pid(s)={sorted(new_pids)} -- closing it/them now")
            for pid in new_pids:
                try:
                    psutil.Process(pid).terminate()
                except psutil.Error as exc:
                    print(f"  (could not terminate pid {pid}: {exc})")
        print()

    print("=" * 100)
    print("MID-TURN / POST-LOAD STATE (both models loaded)")
    print("=" * 100)
    print(f"  RSS  = {_rss_mb():.1f} MB")
    print(f"  VRAM = {_vram_used_mb()}")
    print()

    asyncio.run(app.stop())
    print(f"  RSS after app.stop() (models unloaded) = {_rss_mb():.1f} MB")
    print(f"  VRAM after app.stop()                  = {_vram_used_mb()}")


if __name__ == "__main__":
    main()

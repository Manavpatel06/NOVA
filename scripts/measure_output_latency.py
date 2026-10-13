#!/usr/bin/env python
"""F52 -- price the PortAudio output-buffering tradeoff. Measurement only,
per the lead's explicit instruction: **do not apply this.** NOVA is inside
budget (1.105s steady-state against a 1.5s target); this exists so that if
the budget ever tightens, whoever picks it up has a number instead of an
experiment to run under pressure.

T2.2 found `synth_returned -> first_audio_out` (the gap between
`Synthesizer.say()` returning and `Player`'s audio callback actually
receiving real frames) is a stable ~182ms, and that no `OutputStream`
anywhere in this tree passes an explicit `latency=` -- so PortAudio's
default applies, unchosen. This script does not modify
`nova/audio/player.py`; it opens `sd.OutputStream`s directly, matching
Player's real parameters (device resolved via the same
`probe_output_device()` NOVA actually uses, samplerate matching Kokoro's
real output rate) so the numbers are representative without touching
production code.

Three questions, three sections below:
  1. How much of the 182ms is recoverable -- measured across several
     `latency=` values PortAudio actually accepts on this device, not just
     'low' vs default.
  2. What it costs -- underrun count at each setting, over enough real
     playbacks to say something, both idle and under real GPU load
     (a warm Transcriber.transcribe() looping concurrently).
  3. A recommendation with both numbers. No code changes.

    D:\\NOVA\\.venv\\Scripts\\python.exe scripts\\measure_output_latency.py
"""
from __future__ import annotations

import statistics
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import sounddevice as sd

from nova.audio.output import probe_output_device
from nova.stt.transcriber import Transcriber
from nova.tts.synthesizer import Synthesizer

N_REPEATS = 10
#: None = current production behavior (no latency= passed anywhere today).
#: 'low' = PortAudio's other named preset. The rest span below PortAudio's
#: own 'low' preset (0.0933s measured on this device) down toward zero, to
#: find where underruns actually start appearing rather than guessing.
#: 0.005 was tried in an earlier run and dropped -- 0.01 already produces
#: catastrophic, constant underrun on this MME device (see docs), and 0.005
#: only made each play take longer to (fail to) settle without adding
#: information.
LATENCIES: list[float | str | None] = [None, "low", 0.03, 0.02, 0.01]

#: Settings still worth testing under GPU load -- 0.01 is excluded here
#: because the idle run already disqualifies it on underrun grounds alone;
#: retesting a setting that already glitches idle, under load, adds no
#: decision-relevant information and doubles the runtime for nothing.
UNDER_LOAD_LATENCIES: list[float | str | None] = [None, "low", 0.03, 0.02]


def _play_once(
    pcm: np.ndarray, samplerate: int, device: int, latency, *, clip_duration_s: float
) -> tuple[float, int, int, bool]:
    """Play `pcm` once on `device` at `latency`. Returns (seconds from
    stream.start() to the first real callback, number of callbacks that
    reported output_underflow, total callback count, stalled) -- mirrors
    Player.play()'s real callback shape (chunked writes, CallbackStop at
    end) with underrun tracking added, which production Player does not
    have today.

    `stalled=True` means `finished_callback` never fired within a bounded
    wait (clip duration + 3s margin, not a flat generous timeout) -- an
    aggressive `latency=` value pathologically glitching hard enough that
    the stream never cleanly finishes is itself a data point (this
    happened during development at latency=0.005s and would otherwise
    stall the whole measurement run for up to 30s per play), not something
    to wait out indefinitely.
    """
    pcm = np.asarray(pcm, dtype=np.float32).reshape(-1, 1)
    n_frames = len(pcm)
    pos = 0
    first_frame_t: list[float] = []
    underflow_count = 0
    callback_count = 0
    done = threading.Event()
    t_start = time.perf_counter()

    def _callback(outdata, frames, time_info, status):
        nonlocal pos, underflow_count, callback_count
        callback_count += 1
        if status.output_underflow:
            underflow_count += 1
        remaining = n_frames - pos
        if remaining <= 0:
            outdata[:] = 0
            raise sd.CallbackStop
        chunk = min(frames, remaining)
        outdata[:chunk] = pcm[pos : pos + chunk]
        if chunk < frames:
            outdata[chunk:] = 0
        if not first_frame_t and chunk > 0:
            first_frame_t.append(time.perf_counter())
        pos += chunk
        if pos >= n_frames:
            raise sd.CallbackStop

    def _finished():
        done.set()

    stream = sd.OutputStream(
        samplerate=samplerate, channels=1, dtype="float32", device=device,
        latency=latency, callback=_callback, finished_callback=_finished,
    )
    stream.start()
    stalled = not done.wait(timeout=clip_duration_s + 3.0)
    stream.stop()
    stream.close()

    first_latency = (first_frame_t[0] - t_start) if first_frame_t else float("nan")
    return first_latency, underflow_count, callback_count, stalled


def _run_condition(pcm, samplerate, device, latency, n, *, label: str) -> dict:
    clip_duration_s = len(pcm) / samplerate
    first_latencies = []
    plays_with_underrun = 0
    total_underflows = 0
    stalls = 0
    for i in range(n):
        first_lat, underflows, _cbs, stalled = _play_once(
            pcm, samplerate, device, latency, clip_duration_s=clip_duration_s
        )
        if stalled:
            stalls += 1
            continue  # excluded from the latency stats below -- see note in printed line
        first_latencies.append(first_lat)
        if underflows:
            plays_with_underrun += 1
            total_underflows += underflows
        time.sleep(0.05)  # brief gap between plays, avoids device-reopen thrash

    if first_latencies:
        stats = (
            f"first-callback: mean={statistics.mean(first_latencies)*1000:6.1f}ms "
            f"min={min(first_latencies)*1000:6.1f}ms max={max(first_latencies)*1000:6.1f}ms"
        )
    else:
        stats = "first-callback: n/a (every play stalled)"
    print(
        f"  [{label}] latency={latency!r:8} n={n:2d}  {stats}  "
        f"underrun: {plays_with_underrun}/{n - stalls} completed plays, {total_underflows} total callbacks"
        + (f"  STALLED: {stalls}/{n} plays never finished" if stalls else ""),
        flush=True,
    )
    return {
        "label": label, "latency": latency, "n": n,
        "mean_ms": statistics.mean(first_latencies) * 1000 if first_latencies else float("nan"),
        "min_ms": min(first_latencies) * 1000 if first_latencies else float("nan"),
        "max_ms": max(first_latencies) * 1000 if first_latencies else float("nan"),
        "plays_with_underrun": plays_with_underrun,
        "total_underflows": total_underflows,
        "stalls": stalls,
    }


def main() -> None:
    print("=" * 100)
    print("Resolving the real production output device (same probe NOVA uses)")
    print("=" * 100)
    profile = probe_output_device()
    print(f"  device: {profile}")
    device = profile.index

    print()
    print("=" * 100)
    print("Synthesizing one representative utterance (reused across every trial --")
    print("keeps audio content constant so only `latency=` varies between conditions)")
    print("=" * 100)
    synth = Synthesizer()
    pcm, samplerate = synth.say("It's three o'clock.")
    print(f"  {len(pcm)/samplerate:.2f}s of audio at {samplerate}Hz")

    print()
    print("=" * 100)
    print("What stream.latency actually resolves to for each requested value")
    print("(sanity check -- PortAudio rounds to buffer-size multiples)")
    print("=" * 100)
    for lat in LATENCIES:
        s = sd.OutputStream(samplerate=samplerate, channels=1, dtype="float32", device=device, latency=lat)
        s.start()
        print(f"  requested={lat!r:8} -> granted={s.latency*1000:.1f}ms")
        s.stop()
        s.close()

    results = []

    print()
    print("=" * 100)
    print(f"IDLE -- {N_REPEATS} plays per setting, nothing else running")
    print("=" * 100)
    for lat in LATENCIES:
        results.append(_run_condition(pcm, samplerate, device, lat, N_REPEATS, label="idle"))

    print()
    print("=" * 100)
    print(f"UNDER LOAD -- {N_REPEATS} plays per setting, a warm Transcriber.transcribe()")
    print("looping concurrently on the GPU (simulates real contention, not real turn order --")
    print("see module docstring)")
    print("=" * 100)
    transcriber = Transcriber()
    transcriber.load()
    load_audio = np.random.default_rng(0).uniform(-0.1, 0.1, size=16000 * 3).astype(np.float32)
    stop_load = threading.Event()

    def _load_loop():
        while not stop_load.is_set():
            transcriber.transcribe(load_audio)

    load_thread = threading.Thread(target=_load_loop, daemon=True)
    load_thread.start()
    time.sleep(0.3)  # let the load loop actually get going before measuring
    try:
        for lat in UNDER_LOAD_LATENCIES:
            results.append(_run_condition(pcm, samplerate, device, lat, N_REPEATS, label="under_load"))
    finally:
        stop_load.set()
        load_thread.join(timeout=10)
        transcriber.unload()

    print()
    print("=" * 100)
    print("SUMMARY")
    print("=" * 100)
    baseline_idle = next(r for r in results if r["label"] == "idle" and r["latency"] is None)
    for r in results:
        recovered_ms = baseline_idle["mean_ms"] - r["mean_ms"]
        print(
            f"  {r['label']:10} latency={str(r['latency']):8} "
            f"mean_first_callback={r['mean_ms']:6.1f}ms  "
            f"recovered_vs_default={recovered_ms:6.1f}ms  "
            f"underrun_plays={r['plays_with_underrun']}/{r['n']}"
        )


if __name__ == "__main__":
    main()

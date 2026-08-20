#!/usr/bin/env python
"""T2.2 latency measurement -- real hardware, real models, real speakers.

Runs the exact production path (`NovaApp.run_turn()`) the same way
`scripts/probe_turn.py` does: no human is available at this seat to press
the hotkey and speak, so "captured" audio is Kokoro-synthesized speech
(resampled 24kHz -> 16kHz) standing in for a live mic recording. That
proves the whole wired pipeline (STT -> router -> ladder-gated skill -> TTS
-> speakers) with real measured latency; it does NOT re-measure the
hotkey-to-mic capture path itself (F16/F21-style device concerns are out of
scope here).

Reports first-turn (cold: nothing loaded yet) separately from steady-state
(N repeats in the same warm process) -- conflating them hides both, per the
T2.2 assignment. Every steady-state number is a range over multiple runs,
never a single sample (this project has been burned by single-run figures
twice already -- see docs/TASKS.md).

Reads T2.2 latency marks straight off the bus (`nova.telemetry`), the same
marks NOVA logs in production, rather than re-timing anything itself --
one source of truth for what "latency" means here.

    D:\\NOVA\\.venv\\Scripts\\python.exe scripts\\measure_latency.py
"""
from __future__ import annotations

import asyncio
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nova.app import NovaApp
from nova.audio.resample import LinearResampler
from nova.telemetry import TOPIC_LATENCY_MARK, LatencyMark
from nova.tts.synthesizer import Synthesizer

STEADY_STATE_RUNS = 8
PHRASE = "what time is it"  # READ-tier skill, matches the plan's budget target


class _MarkCollector:
    """Subscribes alongside NovaApp's own LatencyLog and keeps every mark,
    keyed by turn_id, so this script can compute exact deltas instead of
    re-parsing log lines."""

    def __init__(self) -> None:
        self.turns: dict[str, dict[str, LatencyMark]] = defaultdict(dict)

    async def handle(self, mark: LatencyMark) -> None:
        self.turns[mark.turn_id][mark.stage] = mark


def _synthesized_16k_pcm(input_synth: Synthesizer, phrase: str):
    """Uses a *separate* Synthesizer instance to produce the fake "captured
    audio" input, deliberately not `app.synth` -- `app.synth` is the one
    the turn loop's own `_speak()` uses for the reply, and this script
    times how long *that* one takes to cold-load on the first real turn.
    Generating the input phrase through `app.synth` first would load Kokoro
    into it before timing starts, silently erasing the exact number T2.2
    asked for. (Caught by inspecting this script's own first-turn output:
    `skill_returned->synth_returned` on turn 0 came back matching the
    steady-state number almost exactly, which is only possible if Kokoro
    was already warm.)"""
    pcm, sr = input_synth.say(phrase)
    if sr != 16000:
        pcm = LinearResampler(sr_in=sr, sr_out=16000).process(pcm)
    return pcm


def _breakdown(marks: dict[str, LatencyMark]) -> dict[str, float]:
    """stage->stage deltas in observed-timestamp order, seconds."""
    ordered = sorted(marks.values(), key=lambda m: m.t)
    out: dict[str, float] = {}
    for prev, cur in zip(ordered, ordered[1:]):
        out[f"{prev.stage}->{cur.stage}"] = round(cur.t - prev.t, 4)
    return out


def _total(marks: dict[str, LatencyMark]) -> float:
    ordered = sorted(marks.values(), key=lambda m: m.t)
    return round(ordered[-1].t - ordered[0].t, 4)


async def _run_one_turn(
    app: NovaApp, input_synth: Synthesizer, collector: _MarkCollector, turn_no: int
) -> tuple[str, float]:
    pcm = _synthesized_16k_pcm(input_synth, PHRASE)
    turn_id = f"measure{turn_no}"
    t0 = time.monotonic()
    reply = await app.run_turn(pcm, source="measure_latency", turn_id=turn_id)
    wall = time.monotonic() - t0
    assert reply is not None and reply.success, f"turn {turn_no} did not succeed: {reply}"
    return turn_id, wall


async def _main() -> None:
    app = NovaApp()
    # A throwaway Synthesizer, used only to fabricate the "captured audio"
    # input phrase -- see _synthesized_16k_pcm's docstring for why this
    # must not be app.synth.
    input_synth = Synthesizer()
    collector = _MarkCollector()
    app.bus.subscribe(TOPIC_LATENCY_MARK, collector.handle)

    print("=" * 100)
    print(f"FIRST TURN (cold -- Whisper/Kokoro load happens inside this call), phrase={PHRASE!r}")
    print("=" * 100)
    turn_id, wall = await _run_one_turn(app, input_synth, collector, 0)
    marks = collector.turns[turn_id]
    print(f"  wall time (run_turn() return, i.e. full reply finished speaking) = {wall:.3f}s")
    print(f"  total (first mark -> first_audio_out, i.e. time to first sound) = {_total(marks):.3f}s")
    print(
        f"  (gap = {wall - _total(marks):.3f}s -- run_turn() doesn't return until playback "
        f"finishes; the wall/total gap is speech duration, not hidden latency)"
    )
    for k, v in _breakdown(marks).items():
        print(f"    {k:<32} {v:.3f}s")
    print()

    print("=" * 100)
    print(f"STEADY STATE -- {STEADY_STATE_RUNS} more turns, same warm process, phrase={PHRASE!r}")
    print("=" * 100)
    totals: list[float] = []
    walls: list[float] = []
    per_stage: dict[str, list[float]] = defaultdict(list)
    for i in range(1, STEADY_STATE_RUNS + 1):
        turn_id, wall = await _run_one_turn(app, input_synth, collector, i)
        marks = collector.turns[turn_id]
        total = _total(marks)
        totals.append(total)
        walls.append(wall)
        bd = _breakdown(marks)
        for k, v in bd.items():
            per_stage[k].append(v)
        print(f"  run {i}: total={total:.3f}s wall={wall:.3f}s  {bd}")

    print()
    print("-" * 100)
    print(f"STEADY STATE SUMMARY over {STEADY_STATE_RUNS} runs (seconds)")
    print("-" * 100)
    print(
        f"  total (first mark -> first_audio_out): "
        f"min={min(totals):.3f} max={max(totals):.3f} mean={statistics.mean(totals):.3f} "
        f"median={statistics.median(totals):.3f}"
    )
    for k, vs in per_stage.items():
        print(
            f"  {k:<32} min={min(vs):.3f} max={max(vs):.3f} mean={statistics.mean(vs):.3f}"
        )

    await app.stop()


if __name__ == "__main__":
    asyncio.run(_main())
